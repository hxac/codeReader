# KV Cache 加速推理

## 1. 本讲目标

在前面的章节里，你已经能用 `generate_text_simple`（见 u4-l4）让一个自回归 GPT 模型一段一段地把文本「吐」出来。但只要稍微生成长一点，你就会发现：**越往后生成越慢**。

本讲的目标就是解决这个「越生成越慢」的问题。学完后你应当掌握：

- 理解 KV cache 的**动机与机制**——为什么自回归生成里历史 token 的 Key/Value 在重复计算，又该如何把它们缓存起来复用；
- 读懂 `MultiHeadAttention` 里 `cache_k` / `cache_v` 两个缓冲区，以及带 `use_cache` 开关的前向过程；
- 理解**位置指针**（`ptr_current_pos` / `current_pos`）如何在「增量生成」时正确地偏移因果掩码与位置嵌入；
- 掌握 `reset_kv_cache` 的清理时机，以及增量生成函数 `generate_text_simple_cached` 的完整流程，并亲手验证它带来的加速。

本讲对应第 4 章的 bonus 目录 `ch04/03_kv-cache/`，是一段**只用于推理**、不改训练逻辑的优化代码。

## 2. 前置知识

在学习 KV cache 之前，请确认你已经理解以下几点（它们来自前面几讲）：

1. **自回归生成**（u4-l4 / u5-l3）：模型每一步根据已有序列预测下一个 token，再把它拼到序列末尾，循环 `max_new_tokens` 次。每一步都用贪心 `argmax` 或采样从 logits 里挑一个 token。
2. **缩放点积注意力**（u3-l1 / u3-l3）：注意力需要三个张量——查询 `Q`（queries）、键 `K`（keys）、值 `V`（values），其中注意分数 `attn_scores = Q @ Kᵀ`，输出 `context = softmax(scores) @ V`。`K` 和 `V` 都是由输入 token 经线性层 `W_key` / `W_value` 投影得到的。
3. **因果掩码**（u3-l2）：用一个上三角掩码把「未来 token」的注意力分数置成 `-inf`，保证每个位置只看得到自己和之前的 token。
4. **GPTModel 数据流**（u4-l3）：token ID → token 嵌入 + 位置嵌入 → 堆叠的 `TransformerBlock` → 输出头 logits。其中位置嵌入 `pos_emb` 是一张「行号 = 绝对位置」的查表，最大行数 `context_length` 决定了序列上限。

> 一句话回顾：在注意力里，**每个 token 都会算出自己的一份 K 和 V**。本讲的全部精妙之处，就围绕「这些 K、V 在自回归生成中其实不需要每次都重算」这一点展开。

## 3. 本讲源码地图

本讲集中在第 4 章的 KV cache bonus 目录，涉及以下文件：

| 文件 | 作用 |
| --- | --- |
| `ch04/03_kv-cache/README.md` | KV cache 的概念讲解、实现走查、性能对比表与优化建议。 |
| `ch04/03_kv-cache/gpt_with_kv_cache.py` | **本讲主角**。把第 3、4 章代码汇总成自包含脚本，并在关键位置加入 KV cache（用 `# NEW` 标注），可直接 `python` 运行做性能对比。 |
| `ch04/03_kv-cache/gpt_ch04.py` | 不带缓存的**基线版本**（原书第 3、4 章代码汇总），用于和缓存版做速度对比。 |
| `ch04/03_kv-cache/tests.py` | 用 `torch.equal` 验证「带缓存 / 不带缓存 / 优化版」三者生成结果**逐位相同**，是判断 cache 实现正确性的关键测试。 |
| `ch04/03_kv-cache/gpt_with_kv_cache_optimized.py` | 进一步优化版（预分配内存 + 滑动窗口），本讲只作延伸提及。 |

阅读时建议同时打开 `gpt_with_kv_cache.py`，所有 `# NEW` 段落就是本讲要讲的内容。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：缓存区 `cache_k/cache_v`、带 `use_cache` 的前向、`reset_kv_cache` 清理、增量生成函数。

### 4.1 缓存区 cache_k / cache_v：把历史的 K、V 存起来

#### 4.1.1 概念说明：为什么能省掉重复计算

先看一个直觉。假设提示词是 `"Time flies"`，模型要预测下一个词。在注意力里，前两个 token 会各自算出自己的 `K` 和 `V`。

现在模型生成出 `"fast"`，序列变成 `"Time flies fast"`，进入下一轮预测。**关键观察**：在这一轮里，前两个 token `"Time"` 和 `"flies"` 的 K、V 和上一轮**完全相同**——因为它们只取决于「这个 token 是什么」以及权重 `W_key` / `W_value`，而这两者都没变。真正「新」的只有新 token `"fast"` 的 K、V。

所以朴素做法（u4-l4 的 `generate_text_simple`）每一步都把整段序列重新喂给模型、重新算一遍所有 token 的 K/V，是对历史 token 的**无谓重算**。KV cache 的做法是：把每一步算出的 K、V 累积存进缓冲区，下一步只算新 token 的 K/V，再拼到缓冲区末尾即可。

README 对此有一句精炼总结——KV cache 把中间的 K、V 存下来供推理复用，能换来显著的生成加速，代价是代码更复杂、显存占用更高，且**只能用于推理、不能用于训练**：

[README.md:10](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/03_kv-cache/README.md#L10)

#### 4.1.2 核心流程：从二次到线性

从计算量角度，缓存带来的收益非常直观。设序列长度为 \(n\)，每个 token 经 `W_key`/`W_value` 投影一次的代价量级为 \(O(d^2)\)（\(d\) 为 `emb_dim`）。

- **朴素生成**：第 \(t\) 步要把前 \(t\) 个 token 全部重算 K/V，单步 \(O(t \cdot d^2)\)；生成 \(n\) 个 token 的累积开销是

\[
\sum_{t=1}^{n} O(t \cdot d^2) = O(n^2 \cdot d^2)
\]

- **带 KV cache**：每步只算**新 token 一个**的 K/V，单步 \(O(d^2)\)（外加新 query 对缓存中 \(t\) 个 key 的注意力 \(O(t \cdot d)\)）；累积开销降为

\[
\sum_{t=1}^{n} O(d^2) = O(n \cdot d^2)
\]

也就是说，K/V 投影这一块**从平方级降到线性级**。README 用同样的口径描述了这一权衡：

[README.md:240-242](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/03_kv-cache/README.md#L240-L242)

> 代价：缓存里每来一个新 token 就要追加一份 K/V，显存随序列长度**线性增长**，长序列 + 大模型时可能吃满显存（这是后续 GQA/MLA/SWA 等变体要解决的 KV 内存问题，见 u9-l3）。

#### 4.1.3 源码精读：注册两个缓冲区

实现的第一步，是在 `MultiHeadAttention` 的构造函数里注册两个缓冲区 `cache_k` 和 `cache_v`，初始值为 `None`：

```python
self.register_buffer("cache_k", None, persistent=False)
self.register_buffer("cache_v", None, persistent=False)
self.ptr_current_pos = 0
```

[gpt_with_kv_cache.py:36-38](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/03_kv-cache/gpt_with_kv_cache.py#L36-L38) —— 注册 `cache_k` / `cache_v` 两个非持久化缓冲区，并初始化位置指针 `ptr_current_pos`。

这里沿用了 u3-l2 讲过的 `register_buffer` 机制（掩码 `mask` 也是这么注册的）。两个细节值得注意：

- `persistent=False`：缓冲区**不会**进入 `state_dict`，所以保存 / 加载模型权重（u5-l4）时不会带上缓存内容——这是对的，缓存只是推理期的一次性状态，不该和模型参数混在一起。
- 它和因果掩码 `mask`（第 28-32 行）共享同一套「随 `.to(device)` 迁移、不被优化器更新」的 buffer 语义。

#### 4.1.4 代码实践

> **实践目标**：确认两个缓冲区的「身份」——它们不是参数、不参与训练，却能随模型一起搬到 GPU。

1. 打开 `ch04/03_kv-cache/gpt_with_kv_cache.py`，定位到 `MultiHeadAttention.__init__`。
2. 在脚本末尾（或新建临时脚本，标注为「示例代码」）加几行：

```python
# 示例代码：观察 cache_k 的身份
import torch
from gpt_with_kv_cache import MultiHeadAttention

mha = MultiHeadAttention(d_in=768, d_out=768, context_length=1024,
                         dropout=0.0, num_heads=12)
print("cache_k 初始值:", mha.cache_k)            # 预期 None
print("是 buffer 吗:", "cache_k" in dict(mha.named_buffers()))  # 预期 True
print("是参数吗:", "cache_k" in dict(mha.named_parameters()))   # 预期 False
sd = mha.state_dict()
print("进入 state_dict 吗:", "cache_k" in sd)     # 预期 False（persistent=False）
```

3. **观察现象**：`cache_k` 初始为 `None`；它出现在 `named_buffers()` 里、却不在 `named_parameters()` 和 `state_dict()` 里。
4. **预期结果**：以上四个断言全部成立，验证它是「推理期临时状态」而非模型参数。
5. 若运行环境无该模块，明确标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `persistent=False` 去掉，会发生什么？这对训练 / 推理有何影响？
**答案**：`cache_k` / `cache_v` 会进入 `state_dict`。保存的权重文件会变大，而且下次加载时会把上一次推理残留的缓存内容也读进来，造成隐蔽 bug。本实现设 `persistent=False` 正是为了避免缓存污染权重存取。

**练习 2**：为什么缓存只能用于推理，不能用于训练？
**答案**：训练时每个 batch 的序列互不相同、且需要反向传播，缓存的「跨步累积」语义没有意义，还会干扰梯度计算；缓存只针对「同一个序列不断往后生成」的自回归推理场景。

---

### 4.2 use_cache 前向：从单层注意力到整个模型

#### 4.2.1 概念说明：一个开关串起三层改动

要支持缓存，不能只改 `MultiHeadAttention`——还得让上层把它一路传下去。本讲实现的思路是：**加一个 `use_cache` 布尔开关**，从 `GPTModel` → `TransformerBlock` → `MultiHeadAttention` 一层一层透传。当 `use_cache=False` 时，模型行为和原版**完全一致**（这正是 `tests.py` 能做等价对比的前提）。

这里还有两个「位置」问题必须解决，否则缓存会让结果错位：

1. **位置嵌入**：增量生成时，新 token 应该拿到它在整条序列里的**真实绝对位置**（第 `current_pos` 个位置），而不是又从 0 开始。
2. **因果掩码**：增量生成时，新 token 的 query 只有一个，而缓存里的 key 横跨所有历史位置——掩码得按 query 的真实位置区间去切片，否则会误屏蔽。

#### 4.2.2 核心流程：注意力层里的缓存逻辑

带缓存的注意力前向分为三段：投影 → 累积缓存 → 偏移掩码。

**① 投影新 token 的 K/V**（永远只算「本轮输入」`x` 的 K/V，不管缓存里有多少历史）：

```python
keys_new   = self.W_key(x)
values_new = self.W_value(x)
queries    = self.W_query(x)
```

[gpt_with_kv_cache.py:44-46](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/03_kv-cache/gpt_with_kv_cache.py#L44-L46) —— 只对当前 chunk 做投影，得到 `keys_new` / `values_new`。

**② 累积缓存**：第一次调用时初始化，之后把新 K/V 沿 token 维拼到缓存末尾：

```python
if use_cache:
    if self.cache_k is None:
        self.cache_k, self.cache_v = keys_new, values_new
    else:
        self.cache_k = torch.cat([self.cache_k, keys_new], dim=1)
        self.cache_v = torch.cat([self.cache_v, values_new], dim=1)
    keys, values = self.cache_k, self.cache_v
else:
    keys, values = keys_new, values_new
```

[gpt_with_kv_cache.py:56-64](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/03_kv-cache/gpt_with_kv_cache.py#L56-L64) —— 首次把 `cache_k/v` 设为新值，之后 `torch.cat` 沿 token 维（dim=1）追加，并把真正参与注意力的 `keys/values` 指向完整缓存。

注意此时 K/V 还在 `view` 后的形状 `(b, num_tokens, num_heads, head_dim)` 上（第 50-52 行），`cat` 沿 `dim=1`（num_tokens）拼接正好对应「序列变长」，随后第 68-70 行再统一 `transpose(1,2)` 把头提到前面。**形状口径前后一致，这是缓存能正确拼接的关键。**

**③ 偏移因果掩码**——这是最巧妙的一段：

```python
num_tokens_Q = queries.shape[-2]
num_tokens_K = keys.shape[-2]
if use_cache:
    mask_bool = self.mask.bool()[
        self.ptr_current_pos:self.ptr_current_pos + num_tokens_Q, :num_tokens_K
    ]
    self.ptr_current_pos += num_tokens_Q
else:
    mask_bool = self.mask.bool()[:num_tokens_Q, :num_tokens_K]
```

[gpt_with_kv_cache.py:77-87](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/03_kv-cache/gpt_with_kv_cache.py#L77-L87) —— 按当前 query 的全局位置区间 `[ptr_current_pos : ptr_current_pos+num_tokens_Q]` 切出掩码的对应行，列则覆盖所有缓存 key，并推进位置指针。

为什么这样切就对了？回忆因果掩码 `mask` 是一个 `context_length × context_length` 的上三角矩阵：**第 `i` 行刻画「位置 `i` 能看到哪些位置」**。带缓存时：

- **Prefill（首次喂整段 prompt，长度 L）**：`ptr_current_pos=0`、`num_tokens_Q=L`、`num_tokens_K=L`，切出 `mask[0:L, :L]`，正是标准 `L×L` 因果掩码。
- **Decode（每步只喂 1 个新 token，此时已生成到位置 t）**：`ptr_current_pos=t`、`num_tokens_Q=1`、`num_tokens_K=t+1`，切出 `mask[t:t+1, :t+1]`——第 `t` 行的前 `t+1` 列全为 `False`（位置 t 看得到 0..t 全部历史），于是这个新 query 对缓存里**所有** key 都不做屏蔽。完全正确。

同一段切片代码同时覆盖 prefill 和 decode 两种形态，这是该实现最优雅的地方。切片完成后照常用 `-inf` 填充再 softmax：

[gpt_with_kv_cache.py:90-92](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/03_kv-cache/gpt_with_kv_cache.py#L90-L92) —— `masked_fill_` 屏蔽，再除以 `sqrt(head_dim)` 做 softmax（与 u3-l1/l3 的缩放点积注意力一致）。

#### 4.2.3 核心流程：开关如何透传到全模型

`TransformerBlock` 只是把 `use_cache` 透给注意力子层（前馈层逐位置作用、与缓存无关）：

[gpt_with_kv_cache.py:176](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/03_kv-cache/gpt_with_kv_cache.py#L176) —— `self.att(x, use_cache=use_cache)`，把开关交给 `MultiHeadAttention`。

`GPTModel` 这边有三处改动。**其一**，把 `trf_blocks` 从 `nn.Sequential` 换成 `nn.ModuleList`，并新增 `current_pos`：

```python
self.trf_blocks = nn.ModuleList(
    [TransformerBlock(cfg) for _ in range(cfg["n_layers"])])
self.current_pos = 0
```

[gpt_with_kv_cache.py:203-206](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/03_kv-cache/gpt_with_kv_cache.py#L203-L206) —— 改用 `ModuleList` 以便在循环里逐块传参；记录模型级位置 `current_pos`。

> 为什么要换掉 `nn.Sequential`？因为 `Sequential` 只能调用每个子模块的 `forward(x)`、没法额外传 `use_cache` 参数；换成 `ModuleList` + 显式 `for` 循环才能把它传进去。

**其二**，按 `current_pos` 算位置嵌入，并逐块前向：

```python
if use_cache:
    pos_ids = torch.arange(self.current_pos, self.current_pos + seq_len,
                           device=in_idx.device, dtype=torch.long)
    self.current_pos += seq_len
else:
    pos_ids = torch.arange(0, seq_len, device=in_idx.device, dtype=torch.long)
pos_embeds = self.pos_emb(pos_ids).unsqueeze(0)
...
for blk in self.trf_blocks:
    x = blk(x, use_cache=use_cache)
```

[gpt_with_kv_cache.py:221-236](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/03_kv-cache/gpt_with_kv_cache.py#L221-L236) —— 缓存模式下位置嵌入从 `current_pos` 起算，保证新 token 拿到正确的绝对位置；随后逐块透传 `use_cache`。

注意 `current_pos`（模型级，喂位置嵌入表）和 `ptr_current_pos`（注意力级，切掩码）是**两个独立的计数器**，分别解决「位置嵌入」和「掩码切片」两件事，但它们在每次前向里推进的步数相同（都是 `seq_len`），保持同步。

#### 4.2.4 代码实践

> **实践目标**：用 `use_cache` 开关验证「带缓存 / 不带缓存」前向结果逐位相同。

```python
# 示例代码：开关等价性
import torch
from gpt_with_kv_cache import GPTModel

cfg = {"vocab_size": 50257, "context_length": 1024, "emb_dim": 768,
       "n_heads": 12, "n_layers": 12, "drop_rate": 0.0, "qkv_bias": False}
torch.manual_seed(123)
model = GPTModel(cfg).eval()

x = torch.randint(0, 50257, (1, 6))   # 一段 6 token 的「prompt」

# 方式 A：一次性把 6 个 token 全喂进去，不开缓存
out_plain = model(x, use_cache=False)

# 方式 B：先喂前 3 个，再「增量」喂后 3 个，开缓存
model.reset_kv_cache()
_ = model(x[:, :3], use_cache=True)
out_cached_rest = model(x[:, 3:], use_cache=True)        # 只得到后 3 个位置的 logits
print("后 3 个位置 logits 是否一致:",
      torch.allclose(out_plain[:, 3:, :], out_cached_rest, atol=1e-5))
```

1. **操作步骤**：在 `ch04/03_kv-cache/` 目录下运行上述示例脚本。
2. **观察现象**：增量模式下只输出本轮新 token 对应位置的 logits，其数值应与一次性前向对应位置一致。
3. **预期结果**：`torch.allclose` 返回 `True`（浮点误差内），说明缓存拼接没有改变注意力结果。
4. 若本地无 GPU / 运行报错，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：decode 阶段（只喂 1 个新 token）时，`mask_bool` 的形状是什么？为什么不需要屏蔽任何位置？
**答案**：形状是 `(1, num_tokens_K)`，即 `(1, t+1)`。因为这唯一一行是全局掩码的第 `t` 行前 `t+1` 列，全是 `False`——位置 `t` 对历史 0..t 全可见，故无需屏蔽。

**练习 2**：为什么 `current_pos` 和 `ptr_current_pos` 要分成两个变量，而不是共用一个？
**答案**：它们分属不同对象（`GPTModel` 与每个 `MultiHeadAttention` 各一份），职责不同：前者索引位置嵌入表，后者在该层的因果掩码上切片。模型有 12 层注意力、每层各有一个 `ptr_current_pos`，但全模型只有一个 `current_pos`；只要每次前向推进步数一致，它们就保持同步。

---

### 4.3 reset_kv_cache：独立序列之间必须清理

#### 4.3.1 概念说明：缓存是「会脏」的状态

缓存里存的是**某一条具体序列**的 K/V 历史。一旦你要生成**另一条全新序列**（比如对第二个 prompt 做生成），上一次的缓存就成了脏数据——新序列的前几个 token 会错误地「看到」上一条序列残存的 K/V，结果全错。

因此每次开始一段新的独立生成前，必须把缓存和位置指针**全部清零**。本实现提供了两级清理方法：

- `MultiHeadAttention.reset_cache()`：清单个注意力层的 `cache_k/v` 和它的 `ptr_current_pos`；
- `GPTModel.reset_kv_cache()`：遍历所有 transformer 块，把它们注意力的缓存一次性清掉，并重置模型级 `current_pos`。

#### 4.3.2 源码精读

注意力层级的清理：

```python
def reset_cache(self):
    self.cache_k, self.cache_v = None, None
    self.ptr_current_pos = 0
```

[gpt_with_kv_cache.py:106-108](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/03_kv-cache/gpt_with_kv_cache.py#L106-L108) —— 把缓存置回 `None`、位置指针归零，回到「下一次前向会重新初始化缓存」的状态。

模型层级的清理（遍历 12 个块 + 重置 `current_pos`）：

```python
def reset_kv_cache(self):
    for blk in self.trf_blocks:
        blk.att.reset_cache()
    self.current_pos = 0
```

[gpt_with_kv_cache.py:245-248](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/03_kv-cache/gpt_with_kv_cache.py#L245-L248) —— 一键清掉所有层的缓存与模型级位置指针。

注意它能写成 `blk.att.reset_cache()`，前提正是 4.2 里把 `trf_blocks` 换成了 `nn.ModuleList`——这样才能按属性名 `att` 取到每个块的注意力子层。`nn.Sequential` 是做不到的。

#### 4.3.3 代码实践

> **实践目标**：体会「忘记 reset 会得到错结果」。

```python
# 示例代码：忘记清理缓存会导致错误
import torch
from gpt_with_kv_cache import GPTModel, generate_text_simple_cached

cfg = {"vocab_size": 50257, "context_length": 64, "emb_dim": 768,
       "n_heads": 12, "n_layers": 12, "drop_rate": 0.0, "qkv_bias": False}
torch.manual_seed(123)
model = GPTModel(cfg).eval()

p1 = torch.tensor([[1, 2, 3, 4]])
p2 = torch.tensor([[5, 6, 7, 8]])

# 正确做法：每次生成都先 reset（generate_text_simple_cached 内部已 reset）
a = generate_text_simple_cached(model, p1, max_new_tokens=3)
b = generate_text_simple_cached(model, p2, max_new_tokens=3)

# 错误做法：手动跑前向却不 reset，第二条序列会沾上第一条的缓存
model.reset_kv_cache()
_ = model(p1, use_cache=True)
dirty = model(p2, use_cache=True)   # p2 的 token 被当成了 p1 之后的「第 5、6…」个位置
print("若不清理，p2 的输出形状会异常地长:", dirty.shape)  # 列数会 > len(p2)
```

1. **观察现象**：不 reset 直接续跑，第二条序列的位置嵌入和掩码会从错误的位置开始，输出列数与输入不符。
2. **预期结果**：`generate_text_simple_cached` 内部已自动 reset（见 4.4），故正常调用是安全的；只有手动操作才需注意。
3. 标注「待本地验证」。

#### 4.3.4 小练习与答案

**练习**：如果在一个长序列上持续生成直到超过 `context_length`，会发生什么？
**答案**：位置嵌入 `pos_emb` 只有 `context_length` 行，`current_pos + seq_len` 超过后 `pos_emb(pos_ids)` 会越界报错；掩码 `mask` 同样只有 `context_length` 行，`ptr_current_pos` 切片也会越界。本讲的教学版不做截断处理，真实部署需配合滑动窗口截断（见 `gpt_with_kv_cache_optimized.py`）。

---

### 4.4 增量生成：generate_text_simple_cached

#### 4.4.1 概念说明：prefill + decode 两阶段

把前三节的零件串起来，就得到增量生成函数 `generate_text_simple_cached`。它的核心思想是把生成分成两个阶段：

1. **Prefill（预填）**：第一次前向，把**整段 prompt** 一次性喂给模型，初始化并填满缓存。这一步等价于普通前向，只是顺便把 K/V 存了起来。
2. **Decode（解码）**：此后每一步**只把上一个新生成的 token**（单个）喂给模型，让它增量更新缓存并预测下一个 token。

对比 u4-l4 的 `generate_text_simple`：朴素版每一步都重算整段序列；缓存版从第二步起每步只算 1 个 token 的 K/V。这就是加速的来源。

> 该函数带一个 `use_cache` 开关：`use_cache=True` 走增量缓存路径，`use_cache=False` 则退化为「每步喂整段」的朴素路径——这让我们能用**同一个函数**做公平的速度对比。

#### 4.4.2 源码精读

先看作为对照的朴素版（与 u4-l4 一致，用于理解「它每步重算了什么」）：

[gpt_with_kv_cache.py:252-275](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/03_kv-cache/gpt_with_kv_cache.py#L252-L275) —— 朴素 `generate_text_simple`：每步把 `idx[:, -context_size:]` **整段**喂进模型、取末位 logits、`argmax`、拼接。注意第 259 行每次都重新前向整段上下文。

再看缓存版：

```python
def generate_text_simple_cached(model, idx, max_new_tokens,
                                context_size=None, use_cache=True):
    model.eval()
    ctx_len = context_size or model.pos_emb.num_embeddings
    with torch.no_grad():
        if use_cache:
            model.reset_kv_cache()                      # ① 先清理
            logits = model(idx[:, -ctx_len:], use_cache=True)  # ② prefill 整段 prompt
            for _ in range(max_new_tokens):
                next_idx = logits[:, -1].argmax(dim=-1, keepdim=True)  # ③ 贪心采样
                idx = torch.cat([idx, next_idx], dim=1)                 # ④ 拼到序列末尾
                logits = model(next_idx, use_cache=True)               # ⑤ 只喂新 token
        else:
            for _ in range(max_new_tokens):
                logits = model(idx[:, -ctx_len:], use_cache=False)     # 朴素：每步整段
                next_idx = logits[:, -1].argmax(dim=-1, keepdim=True)
                idx = torch.cat([idx, next_idx], dim=1)
    return idx
```

[gpt_with_kv_cache.py:280-304](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/03_kv-cache/gpt_with_kv_cache.py#L280-L304) —— `generate_text_simple_cached`：`use_cache=True` 分支先 reset 再 prefill 整段，之后每步只喂单个新 token；`use_cache=False` 分支退化为每步重算整段。

几处要点：

- **第 288 行 `reset_kv_cache()`**：开头必清，保证每次生成都从干净缓存开始（呼应 4.3）。
- **第 289 行 prefill**：`idx[:, -ctx_len:]` 取整段（长度受 `ctx_len` 约束，默认等于 `context_length`），一次性算出 prompt 的 K/V 并填进缓存。
- **第 297 行 decode**：`logits = model(next_idx, use_cache=True)`，注意喂的是**单个** `next_idx` 而非整段 `idx`——这正是省下重算的关键（README 第 200 行专门强调了这一点）。
- **第 293 行采样**：`logits[:, -1].argmax(...)`，与 u4-l4 贪心解码一致；因为 softmax 单调，直接对 logits 取 argmax 即可。
- `ctx_len = context_size or model.pos_emb.num_embeddings`：不传 `context_size` 时回退到位置嵌入表的行数（即 `context_length`）。

`main()` 里的计时也值得一提——它在前后用 `time.time()` 包住生成过程，并按 `len(token_ids)/total_time` 报告 tokens/sec：

[gpt_with_kv_cache.py:336-368](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/03_kv-cache/gpt_with_kv_cache.py#L336-L368) —— `main()`：构建 124M 模型 → 编码 `"Hello, I am"` → 计时调用 `generate_text_simple_cached` 生成 200 token → 解码并打印 tokens/sec（CUDA 下还会同步并报告峰值显存）。

> 注意：因为模型未训练，输出是「看着像 token、语义是乱码」的文本（与 u4-l4 的结论一致），这**不影响**验证缓存正确性——README 明确指出，只要缓存版和朴素版**输出逐位相同**，就说明缓存索引没有写错（这也是 `tests.py` 的判据）。

#### 4.4.3 代码实践：对比两种生成的速度

> **实践目标**：对比 `generate_text_simple`（朴素）与 `generate_text_simple_cached`（带缓存）的生成速度（tokens/sec），验证 KV cache 带来的加速。

**方式一（最简单，README 推荐）**：分别运行两个自包含脚本，它们各自打印 tokens/sec。

```bash
cd ch04/03_kv-cache
pip install torch tiktoken           # 仅跑这两个脚本的最小依赖
python gpt_ch04.py                   # 朴素版，main() 用 generate_text_simple
python gpt_with_kv_cache.py          # 缓存版，main() 用 generate_text_simple_cached
```

README 在 Mac Mini M4（CPU）上给出的参考结果是 27 tokens/sec（朴素）对 144 tokens/sec（缓存），约 **5× 加速**：

[README.md:218-223](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/03_kv-cache/README.md#L218-L223) —— 性能对比表：缓存版在 CPU 上约 5× 加速；并指出该实现为可读性优化、未针对 CUDA/MPS 加速。

**方式二（更可控，推荐）**：用**同一个函数、同一个模型**，只切换 `use_cache` 开关，既测速又验证结果一致（示例代码）：

```python
# 示例代码：同模型同函数，只切 use_cache，公平对比 + 等价性验证
import time, torch
from gpt_with_kv_cache import GPTModel, generate_text_simple_cached

GPT_CONFIG_124M = {"vocab_size": 50257, "context_length": 1024, "emb_dim": 768,
                   "n_heads": 12, "n_layers": 12, "drop_rate": 0.0, "qkv_bias": False}
torch.manual_seed(123)
model = GPTModel(GPT_CONFIG_124M).eval()

idx = torch.tensor([[1, 2, 3, 4]])   # 模拟一段 4-token prompt

t0 = time.time()
out_slow = generate_text_simple_cached(model, idx.clone(), 200, use_cache=False)
t_slow = time.time() - t0

t0 = time.time()
out_fast = generate_text_simple_cached(model, idx.clone(), 200, use_cache=True)
t_fast = time.time() - t0

print(f"朴素:   {200/t_slow:.1f} tokens/sec")
print(f"缓存:   {200/t_fast:.1f} tokens/sec")
print(f"加速比: {t_slow/t_fast:.1f}x")
print("结果逐位相同:", torch.equal(out_slow, out_fast))   # 关键正确性判据
```

1. **操作步骤**：在 `ch04/03_kv-cache/` 目录把上面脚本存为 `bench.py`（示例代码）后运行。
2. **观察现象**：缓存版 tokens/sec 明显更高；两份输出 `torch.equal` 为 `True`。
3. **预期结果**：CPU 上应看到数倍加速（具体倍数依机器而定）；CUDA 上该教学实现可能几乎没有加速，因为模型太小、设备传输开销盖过了缓存收益（README 第 299 行已说明）。
4. **关键判据**：`torch.equal(out_slow, out_fast)` 必须为 `True`——只有输出完全一致，才能证明缓存索引正确。这也是 `tests.py` 的核心断言。
5. 若本地无法运行，明确标注「待本地验证」。

> 阅读型实践（无法运行时）：打开 `ch04/03_kv-cache/tests.py`，看 `test_gpt_model_equivalence_cached` 如何用 `torch.equal` 比对 `GPTModelBase` / `GPTModelKV1` / `GPTModelKV2` 三者生成结果（[tests.py:65-110](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/03_kv-cache/tests.py#L65-L110)）。这套测试就是把「逐位相同」当作缓存实现正确性的验收标准。

#### 4.4.4 小练习与答案

**练习 1**：为什么缓存版的加速在小模型 + CPU 上明显，在 CUDA 上却可能消失？
**答案**：该教学实现用 `torch.cat` 反复分配并拼接张量，这种动态分配在 CPU 上尚可，但在 CUDA 上「设备间传输 + 反复显存分配」的开销会盖过省下的计算量；模型越小、序列越短，缓存收益越不明显（README 第 299 行）。生产级实现会用预分配张量 + 滑动窗口（见 `gpt_with_kv_cache_optimized.py`）。

**练习 2**：把 `generate_text_simple_cached` 的 prefill 行改成 `logits = model(idx[:, -ctx_len:], use_cache=False)`，后续 decode 仍用 `use_cache=True`，会发生什么？
**答案**：prefill 没填缓存（`cache_k/v` 仍为 `None`、`current_pos` 仍为 0），decode 第一步会把单个新 token 当成位置 0 重新初始化缓存——位置嵌入与掩码全部错位，生成结果与朴素版不再一致，`torch.equal` 会失败。

---

## 5. 综合实践

把本讲的四块知识串成一个完整任务：**给本讲实现的 KV cache 画一张「时序 + 形状」追踪图，并验证它等价于朴素生成。**

任务步骤：

1. **准备**：进入 `ch04/03_kv-cache/`，确认 `gpt_with_kv_cache.py` 可 import。
2. **追踪 prefill**：取 prompt `"Hello, I am"`（4 个 token），调用 `model.reset_kv_cache()` 后执行 `model(idx, use_cache=True)`。在 `MultiHeadAttention.forward` 的关键行（44-46、60-61、80-81）加 `print` 或用断点，记录：
   - 第一次（prefill）后 `cache_k.shape`（预期 `(1, 4, 12, 64)`，即 `batch × num_tokens × num_heads × head_dim`）；
   - `ptr_current_pos` 推进值（预期从 0 变 4）；
   - 模型级 `current_pos`（预期变 4）。
3. **追踪 decode**：手动执行一步 `model(next_idx, use_cache=True)`（`next_idx` 形状 `(1,1)`），记录：
   - `cache_k.shape` 变成 `(1, 5, 12, 64)`（沿 token 维 +1）；
   - `ptr_current_pos` 从 4 变 5；
   - `mask_bool` 形状为 `(1, 5)` 且全 `False`。
4. **等价性验证**：用 4.4.3 的 `bench.py`，确认 `torch.equal(out_slow, out_fast)` 为 `True`，并记录加速比。
5. **画图**：基于追踪数据，画一张「prefill 填满缓存 → decode 每步追加 1 个 token」的流程图，标注每步 `cache_k` 的形状变化与两个位置指针的取值。

> 完成后，你应当能口头复述：prefill 时缓存从 `None` 变成长度 L 的张量，之后每步 decode 在 token 维 +1；两个位置指针同步推进；只要每步推进步数一致、掩码按区间切片，输出就与朴素版逐位相同。

## 6. 本讲小结

- KV cache 的动机：自回归生成中，历史 token 的 K/V 每步都在被无谓重算；把它们缓存复用，可把 K/V 投影的累积开销从 \(O(n^2)\) 降到 \(O(n)\)。
- 实现手段：在 `MultiHeadAttention` 注册 `cache_k` / `cache_v` 两个**非持久化**缓冲区，通过 `use_cache` 开关控制「首次初始化 / 之后 `torch.cat` 沿 token 维追加」。
- 位置正确性：模型级 `current_pos` 喂位置嵌入表、每层 `ptr_current_pos` 切因果掩码，二者按相同步数同步推进；同一段掩码切片优雅地同时覆盖 prefill 与 decode。
- 清理时机：每次开始新的独立生成前必须 `reset_kv_cache()`，否则上一条序列的脏缓存会让结果错乱；`generate_text_simple_cached` 内部已自动 reset。
- 增量生成：`generate_text_simple_cached` 把生成分成 prefill（喂整段 prompt）+ decode（每步只喂 1 个新 token）两阶段，`use_cache=False` 时退化为朴素路径。
- 正确性判据：缓存版与朴素版的输出必须**逐位相同**（`torch.equal`），这是 `tests.py` 的验收标准；教学版在 CPU 上约 5× 加速，但因反复 `torch.cat` 在 CUDA 上收益可能消失。

## 7. 下一步学习建议

- **高效注意力实现**：KV cache 解决了「重算」问题，但朴素注意力实现本身在内存与速度上还有优化空间。下一讲 u9-l2 会对比多种多头注意力实现（含 `torch.nn.functional.scaled_dot_product_attention` / FlashAttention 风格）。
- **KV 内存权衡与现代变体**：缓存随序列长度线性增长会吃满显存。u9-l3 讲解 GQA（分组查询）、MLA（多头潜注意力）、SWA（滑动窗口）三种降低 KV 内存的现代注意力变体——其中 SWA 正是本讲提到的「滑动窗口截断缓存」的正式化。
- **生产级优化**：阅读 `ch04/03_kv-cache/gpt_with_kv_cache_optimized.py`，看预分配张量 + 滑动窗口如何规避 `torch.cat` 的反复分配；并参考 README 末尾给出的 Llama 3 / Qwen3 KV cache 编译加速 benchmark。
- **在真实模型上用 cache**：学完 u10-l1（GPT→Llama）后，可在加载了 OpenAI 预训练权重的模型上启用 KV cache，生成**连贯**文本并体验真实的推理加速。
