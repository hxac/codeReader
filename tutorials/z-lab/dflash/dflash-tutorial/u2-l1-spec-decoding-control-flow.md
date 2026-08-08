# 投机解码全局视图与生成控制流

## 1. 本讲目标

第一单元里，你已经能用 `draft.spec_generate(...)` 把 DFlash 跑起来，但 `dflash_generate` 这个内核函数一直是个黑盒。本讲的目标就是**打开这个黑盒，建立一张全局地图**。

学完本讲你应该能够：

1. 说出 DFlash 推理的**三个阶段**（prefill / 块起草 / 验证），以及它们在一次生成里各执行多少次。
2. 解释 `dflash_generate` 里 `while` 循环每一轮**产出多少个 token**，以及这个数字是怎么由「接受长度」决定的。
3. 说清为什么需要**两套 KV cache**（target 一套、draft 一套），它们各自在什么时候被「裁剪」（crop）。
4. 看懂 `return_stats=True` 时**首 token 时间**和**每 token 时间**是怎么测出来的，为什么测之前要先 `synchronize`。

本讲只画**全局控制流**。至于「草稿模型内部怎么去噪」「注意力怎么拼 context」「采样细节」这些更深的机制，留给后续讲义（u2-l2 / u2-l3 / u2-l4）。本讲把草稿模型 `model(...)` 和目标模型 `target(...)` 都先当成两个函数调用来看。

## 2. 前置知识

在进入源码前，先用三段话补齐本讲需要的几个基础概念。

### 2.1 投机解码的「起草 + 验证」回路

这是 u1-l1 已经建立的心智模型，这里只做一句话回顾并强调**产出公式**：

> 草稿模型先快速「猜」出一串候选 token，目标模型只做**一次前向**就并行验证这串候选。验证后保留草稿猜对的前缀，再补一个目标模型自己给的「兜底 token」。

因此每一轮验证步**产出的 token 数 = 接受长度 a + 1**。这里 `+1` 就是那个永远不会错的兜底 token。生成越快，等价于平均接受长度 E\[a\] 越大。

### 2.2 KV cache 与 DynamicCache

自回归生成时，每生成一个 token，注意力层都要用到「之前所有 token 的 Key/Value」。如果每步都重算全部历史，开销巨大。**KV cache** 把每层算过的 K/V 存下来，下一步只算新 token 的 K/V 追加进去。

Transformers 提供了 [`DynamicCache`](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L19)（在文件顶部 import），它是一个长度可动态增长的缓存对象，常用方法有：

- `update(k, v, layer_idx, ...)`:把新 token 的 K/V 追加进第 `layer_idx` 层。
- `get_seq_length()`:返回当前缓存里存了多少个 token 的序列长度。
- `crop(up_to)`:**把缓存截短到只保留前 `up_to` 个 token**——这一招是投机解码回滚的关键，本讲会反复用到。

DFlash 里维护**两套** `DynamicCache`：一套给 target，一套给 draft。为什么需要两套、它们怎么配合，正是本讲核心问题之一。

### 2.3 CUDA 是异步的，所以计时要先 synchronize

GPU 上的算子是**异步**的：调用 `output = target(input_ids)` 后，Python 这一行立刻返回，真正的 GPU 计算可能还没跑完。如果紧接着用 `time.perf_counter()` 取时间，测到的是「发起 kernel」的时间，而不是「算完」的时间。

所以 `_cuda_time()` 先调用 `torch.cuda.synchronize()` **阻塞等待 GPU 把活干完**，再读墙上时钟。理解这一点，才能看懂后面的计时代码。

## 3. 本讲源码地图

本讲几乎全部围绕一个文件：

| 文件 | 作用 | 本讲关注点 |
|---|---|---|
| `dflash/model.py` | Transformers/PyTorch 参考实现 | 核心函数 `dflash_generate` 的控制流 |
| `dflash/benchmark.py` | 评测 CLI | 只看它**如何调用** `dflash_generate(..., return_stats=True)`，用来佐证计时字段的含义 |

入口关系（回顾 u1-l4）：`spec_generate` 只是 `dflash_generate` 的薄封装，真正干活的是后者：

[spec_generate → dflash_generate 的转发](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L349-L366) —— 注意它把 `block_size`、`mask_token_id`、`return_stats` 都用默认值藏起来了，所以本讲要直接读内核函数本身。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块，对应规格里的三个模块（prefill 段、decode 循环、双缓存与计时），其中第一个模块先给全局鸟瞰。

### 4.1 控制流全局鸟瞰：三阶段与状态变量

#### 4.1.1 概念说明

在钻进任何一段代码之前，先建立一张「俯瞰图」。`dflash_generate` 一次完整生成由三个阶段组成：

1. **prefill（预填充）**：把整段 prompt 喂给 target，算出第一个输出 token，并（在块扩散模式下）取出 target 的多层隐藏状态，作为草稿模型的「上下文」。**这一步只执行一次。**
2. **块起草 + 验证（decode 循环）**：反复执行。每轮里，草稿模型先对一整块位置并行去噪得到候选 token，target 再做一次前向验证，求出接受长度，提交命中的 token，回滚未命中的。
3. **收尾**：把输出张量裁剪到 `max_new_tokens` 或第一个停止 token 处。

贯穿全程的关键**状态变量**有三个：`output_ids`（预分配的输出张量）、`start`（当前已提交到哪个位置）、以及两套 KV cache。

#### 4.1.2 核心流程

用伪代码画出整张地图：

```
输入: draft(草稿模型), target(目标模型), input_ids(prompt), max_new_tokens, temperature
─────────────────────────────────────────────────────
[阶段0: 初始化]
  output_ids    ← 预分配, 全部填 mask_token_id (长度 max_length + block_size)
  position_ids  ← 0,1,2,...,max_length+block_size-1
  创建两个空缓存: past_key_values_target, past_key_values_draft

[阶段1: prefill]                          ← 只跑 1 次
  target(input_ids)  ──→  得到首 token
                      └─→ (block_size>1 时) 取 target_hidden 作为草稿上下文

[阶段2: decode 循环]  while start < max_length:
  每一轮迭代:
   ├─ [块起草] draft 借 target 的 embed/lm_head, 对一块 mask 并行去噪 → 草稿 token
   │           past_key_values_draft.crop(start)
   ├─ [验证]   target 一次前向处理整块草稿 → posterior
   ├─ [接受]   cumprod 求最长公共前缀 → acceptance_length
   │           写回 accepted+1 个 token;  start += acceptance_length + 1
   ├─ [裁剪]   past_key_values_target.crop(start)
   ├─ [更新]   target_hidden ← 本轮 target 隐藏状态(切片到 accepted+1)
   └─ [停止]   命中 stop_token_ids 则 break

[阶段3: 收尾]
  裁剪 output_ids 到 max_length / 首个 stop token
  return output_ids   (或 return_stats=True 时返回统计对象)
```

记住三个「次数」的关系，这是后面实践要验证的核心结论：

- prefill 执行 **1 次**；
- 「块起草」和「验证」执行次数**相同**，等于循环迭代次数 N；
- 每轮产出 `acceptance_length + 1` 个 token，所以 `sum(每轮产出) == num_output_tokens`，且 `N == len(acceptance_lengths)`。

#### 4.1.3 源码精读

整个函数的入口签名如下，先看清它有哪些参数：

[`dflash_generate` 的函数签名](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L62-L73)（这段代码用 `@torch.inference_mode()` 关掉自动求导以加速推理；`model` 是草稿、`target` 是目标模型；`block_size` 与 `mask_token_id` 默认从草稿模型 config 取）。

紧接着是初始化段，把整张地图里的「状态变量」一次性建好：

[初始化：预分配输出张量与两套缓存](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L74-L84)。

这里有两个容易忽略但很重要的细节：

```python
output_ids = torch.full(
    (1, max_length + block_size), mask_token_id, dtype=torch.long, device=target.device,
)
```

- 输出张量长度是 `max_length + block_size`，**多预留了 `block_size` 个位置**。原因：循环里每次取一个 `block_size` 大小的块（`output_ids[:, start : start + block_size]`），多预留的余量保证最后一块也不会越界。这些位置初始全是 `mask_token_id`，最后会在阶段 3 裁掉。
- 紧接着创建了两套 `DynamicCache`（`past_key_values_target` / `past_key_values_draft`），一开始都是空的。

#### 4.1.4 代码实践（源码阅读型）

**目标**：在不运行代码的前提下，靠「数循环」建立直觉。

**步骤**：

1. 打开 [dflash/model.py#L107-L148](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L107-L148) 这段 `while` 循环。
2. 用笔在每一轮迭代里数：草稿前向（`model(...)`）出现几次？target 前向出现几次？`start` 增加多少？
3. 自己回答：如果 `max_new_tokens = 100`、平均每轮接受长度 `a = 3`，循环大概要跑多少轮？

**预期结果**：草稿前向 1 次、target 前向 1 次、`start += a + 1`。因此约需 `100 / (3 + 1) = 25` 轮。这就是「块扩散」相对逐 token 起草能减少串行步数的直觉来源。

#### 4.1.5 小练习与答案

**Q1**：如果 `block_size == 1`，阶段 1 的 prefill 里 `output_hidden_states=block_size > 1` 会变成什么？这意味着什么？

**答案**：变成 `False`，即 target 不返回隐藏状态、也不取 `target_hidden`。这其实就把 DFlash 退化成了「纯目标模型自回归解码」，这正是 benchmark 里测量 **baseline** 的方式（见 4.4.3）。

**Q2**：为什么 `output_ids` 要预分配成 `max_length + block_size` 而不是 `max_length`？

**答案**：循环里按块切片 `output_ids[:, start : start + block_size]`，多出的 `block_size` 余量防止最后一块越界；超出的部分在阶段 3 统一裁掉。

---

### 4.2 prefill 段：取 target 隐藏状态并产出首 token

#### 4.2.1 概念说明

prefill（预填充）是投机解码里的标准动作：在开始「逐 token」生成前，先把整段 prompt 一次性喂进 target，让它的 KV cache 装满历史信息，并算出**第一个输出 token**。

DFlash 给 prefill 多加了一件事：在块扩散模式（`block_size > 1`）下，还要从 target 的多层隐藏状态里**抽取若干层的特征**，拼成 `target_hidden`，作为后续草稿模型去噪时要参照的「上下文」。换句话说，草稿模型不是凭空生成，而是「看着 target 在 prompt 上的中间层表示」来还原整块 token。

#### 4.2.2 核心流程

prefill 段做四件事，顺序很关键：

```
1. (可选) 记下 prefill 起始时间
2. target(input_ids, logits_to_keep=1, output_hidden_states=block_size>1)
     → output.logits:        只算最后一个位置的 logits(省算力)
     → output.hidden_states: 各层隐藏状态(仅 block_size>1 时才有)
3. 把 prompt 和首 token 写进 output_ids
4. (block_size>1 时) 从 hidden_states 抽取多层 → target_hidden
   (可选) 算出 time_to_first_token
```

#### 4.2.3 源码精读

[prefill 段完整代码](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L86-L100)。逐点说明：

```python
output = target(
    input_ids,
    position_ids=position_ids[:, :num_input_tokens],
    past_key_values=past_key_values_target,   # ← 填充 target 缓存
    use_cache=True,
    logits_to_keep=1,                         # ← 只算最后 1 个位置的 logits
    output_hidden_states=block_size > 1,      # ← block_size==1 时省掉隐藏状态
)
output_ids[:, :num_input_tokens] = input_ids
output_ids[:, num_input_tokens:num_input_tokens + 1] = sample(output.logits, temperature)
if block_size > 1:
    target_hidden = extract_context_feature(output.hidden_states, model.target_layer_ids)
time_to_first_token = _cuda_time() - prefill_start if return_stats else None
```

三个要点：

- `logits_to_keep=1`：target 是大模型，全词表 logits 很贵。这里只要预测下一个 token，所以只保留最后 1 个位置的 logits。这是 prefill 的一个重要性能优化。
- `past_key_values=past_key_values_target`：这一步在**填充 target 的 KV cache**。prefill 之后，target 缓存里已经存好了整段 prompt 的 K/V。
- `extract_context_feature(...)`：从 target 多层隐藏状态里，挑出 `model.target_layer_ids` 指定的那几层，按特征维拼接（[extract_context_feature 实现](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L39-L45)，注意里面有个 `offset = 1`，是因为 `hidden_states` 列表第 0 项是嵌入层）。拼接的具体含义留给 u2-l3，本讲只把它当成「一个产生 `target_hidden` 的函数」。

> 衔接 u1-l4 的「两个借」：这里 target 同时担起了「给草稿当上下文」和「产出首 token」两件事。首 token 直接来自 target，所以它一定是「真值」。

#### 4.2.4 代码实践

**目标**：观察 prefill 只执行一次。

**步骤**：在 [model.py:100 行附近](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L86-L100) 的 `time_to_first_token = ...` 这一行**之后**，加一句打印：

```python
print(f"[PREFILL] done; first token written at position {num_input_tokens}")
```

然后跑一次小生成（参考 README 的 Transformers Quick Start，`max_new_tokens=64` 即可）。

**需要观察的现象**：不管你让它生成 64 还是 2048 个 token，`[PREFILL]` 这行**只打印一次**。

**预期结果**：prefill 是一次性事件，打印次数恒为 1。

**说明**：本实践需要在 GPU 机器上加载 target + draft（见 README 安装步骤）；若本地无 GPU，标注「待本地验证」。

#### 4.2.5 小练习与答案

**Q1**：为什么 `logits_to_keep=1` 是安全的？target 其它位置的 logits 没算会不会丢信息？

**答案**：prefill 阶段只需要预测「prompt 之后的第一个 token」，即最后一个位置的 next-token 分布；中间位置的 logits 本来就不会被用作输出，不算它们只省算力、不丢信息。

**Q2**：`target_hidden` 是在哪一行、由什么条件决定的？

**答案**：在 [L98-L99](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L98-L99)，仅当 `block_size > 1` 时才计算。所以 baseline 模式（`block_size == 1`）下根本不存在 `target_hidden`。

---

### 4.3 decode while 循环：块起草 + 验证 + 接受 + 裁剪

这是整个函数的心脏，也是本讲篇幅最大的部分。

#### 4.3.1 概念说明

prefill 之后进入 `while start < max_length` 循环，每一轮做四件事，正好对应投机解码的一个完整「起草—验证」回合：

1. **块起草**：草稿模型对一块（`block_size` 个）位置**并行**去噪，得到一串候选 token。
2. **验证**：target 对这一整块做**一次**前向，得到每个位置「应该是什么 token」的判断。
3. **接受长度计算**：比较草稿与 target，找出最长公共前缀长度 `a`。
4. **提交 + 裁剪**：把命中的 token 写进 `output_ids`，回滚没命中的，并**裁剪 KV cache** 保持一致性。

注意「并行起草」是块扩散相对传统逐 token 起草的核心优势：草稿本身也是一次性算出一整块，而不是一个一个串行猜。

#### 4.3.2 核心流程

把一轮迭代展开（设当前已提交到位置 `start`，块大小为 `B`）：

```
# ── 块起草 ──
block = output_ids[start : start+B].clone()   # 1 个真锚点 + (B-1) 个 mask
if B > 1:
    noise_emb = target.embed_tokens(block)     # 借 target 的嵌入层
    draft_logits = target.lm_head( draft(target_hidden, noise_emb, ...) )
    past_key_values_draft.crop(start)
    block[1:] = sample(draft_logits)           # 填入草稿 token; block[0] 仍是真锚点

# ── 验证 ──
out = target(block, past_key_values=past_key_values_target)   # 一次前向处理整块
posterior = sample(out.logits, temperature)

# ── 接受长度 ──
a = (block[1:] == posterior[:-1]).cumprod().sum()    # 最长公共前缀

# ── 提交 + 裁剪 ──
output_ids[start : start+a+1] = block[: a+1]         # 命中前缀
output_ids[start+a+1]          = posterior[a]        # 兜底 token (来自 target, 必真)
start += a + 1
past_key_values_target.crop(start)                   # 丢弃被拒草稿的 KV
```

**接受长度的数学表达**：设块大小为 B，草稿给出了 B−1 个候选，target 给出 B 个预测。逐位比较草稿候选与 target 预测是否相等，接受长度就是「从头开始的连续命中数」：

\[
a \;=\; \sum_{k=0}^{B-2}\;\prod_{j=0}^{k}\;\mathbb{1}\!\left[\,\text{draft}_j = \text{target}_j\,\right]
\]

累积乘 \(\prod\) 的作用是：**一旦某个位置不命中（乘进去一个 0），后面所有项全归零**，从而只保留前缀。每轮产出 `a + 1` 个 token，期望产出为 \(\mathbb{E}[a] + 1\)。

#### 4.3.3 源码精读

**块起草段**（[model.py#L107-L124](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L107-L124)）：

```python
block_output_ids = output_ids[:, start : start + block_size].clone()
block_position_ids = position_ids[:, start : start + block_size]
if block_size > 1:
    noise_embedding = target.model.embed_tokens(block_output_ids)   # 借 target embed
    draft_logits = target.lm_head(model(                            # 借 target lm_head
        target_hidden=target_hidden,
        noise_embedding=noise_embedding,
        position_ids=position_ids[:, past_key_values_draft.get_seq_length(): start + block_size],
        past_key_values=past_key_values_draft,
        use_cache=True,
        is_causal=False,
    )[:, 1 - block_size :, :])
    past_key_values_draft.crop(start)
    block_output_ids[:, 1:] = sample(draft_logits)
```

要点：

- `block_output_ids` 切出来时是 `[真锚点, mask, mask, ..., mask]`（共 B 个，第一个是位置 `start` 上已提交的真 token，其余是预填的 mask）。
- `noise_embedding = target.model.embed_tokens(...)`：**第一个「借」**——草稿模型没有自己的嵌入层，借 target 的。整块 mask 经嵌入就是「噪声」，草稿的任务就是把这些噪声还原成真实 token。
- `draft_logits = target.lm_head(model(...))`：草稿算出隐藏状态后，用 **target 的 lm_head**（第二个「借」）转成 logits。`is_causal=False` 表示草稿对上下文是双向注意（去噪任务天然非因果）。
- `[:, 1 - block_size :, :]`：对草稿输出做切片，丢弃第一个位置、保留后 B−1 个位置的 logits（用来填 `block[1:]`）。
- `past_key_values_draft.crop(start)`：起草完把草稿缓存截到 `start`，让每轮起草都从一个干净的草稿上下文重新开始。
- `block_output_ids[:, 1:] = sample(draft_logits)`：把草稿 token 填进块的非锚点位置。注意 `block_output_ids[0]`（锚点）**不动**。

**验证 + 接受 + 裁剪段**（[model.py#L126-L140](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L126-L140)）：

```python
output = target(
    block_output_ids,
    position_ids=block_position_ids,
    past_key_values=past_key_values_target,   # ← target 缓存增长 B
    use_cache=True,
    output_hidden_states=block_size > 1,
)
posterior = sample(output.logits, temperature)
acceptance_length = (block_output_ids[:, 1:] == posterior[:, :-1]).cumprod(dim=1).sum(dim=1)[0].item()
output_ids[:, start : start + acceptance_length + 1] = block_output_ids[:, : acceptance_length + 1]
output_ids[:, start + acceptance_length + 1] = posterior[:, acceptance_length]
start += acceptance_length + 1
past_key_values_target.crop(start)
acceptance_lengths.append(acceptance_length + 1)
```

逐行：

- target 对整块 `block_output_ids` 做**一次**前向（这一步让 target 缓存增长 B），`output.logits` 给出每个位置的 next-token 预测。
- `block_output_ids[:, 1:]` 是 B−1 个草稿候选；`posterior[:, :-1]` 是 target 对应位置「应该是什么」的预测。逐位相等比较 → `cumprod(dim=1)` 取最长公共前缀 → `sum` 得到 `acceptance_length`（范围 0 ~ B−1）。
- 命中前缀 `block[: a+1]` 写回输出（含锚点，重复写无害）；**兜底 token** `posterior[a]` 写到 `start+a+1`——这一位一定来自 target，所以一定正确，这就是 `+1` 的来源。
- `start += a + 1`；`past_key_values_target.crop(start)` **把 target 缓存截回新前沿**，丢弃被拒草稿位置的 KV（否则下一轮 target 会基于错误的历史去算）。
- `acceptance_lengths.append(a + 1)`：记录本轮产出，供统计用。

**手动演算示例**（示例代码，非项目原有）：

设 `block_size = 4`，`block_output_ids = [锚点, 5, 7, 9]`，target 算出 `posterior = [5, 7, 8, 2]`：

- `block[1:] = [5, 7, 9]`，`posterior[:-1] = [5, 7, 8]`
- 相等比较 = `[1, 1, 0]`，`cumprod = [1, 1, 0]`，`sum = 2` → `acceptance_length = 2`
- 提交 `block[:3] = [锚点, 5, 7]`，兜底 `posterior[2] = 8` 写到下一个位置
- 本轮产出 3 个 token（5、7、8），`start += 3`

可见第 3 个草稿 `9` 被 target 否决（target 认为应是 8），从该处起回滚，用 target 的 8 接上。

**循环收尾**：[L142-L148](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L142-L148) 更新 `target_hidden`（切片到 `acceptance_length + 1`，只留命中部分作为下一轮上下文），并在命中 stop token 时 `break`。

#### 4.3.4 代码实践

**目标**：验证「块起草次数 == 验证次数 == 循环轮数」，且「每轮产出之和 == 总输出 token 数」。

**步骤**：在循环里加两行打印（位置见上面的源码精读）：

```python
# 在 block_output_ids[:, 1:] = sample(draft_logits) 之后(约 L121 后):
print(f"[DRAFT] iteration, start={start.item() if hasattr(start,'item') else start}")

# 在 acceptance_lengths.append(...) 之后(约 L140 后):
print(f"[VERIFY] accepted={acceptance_length}, yield={acceptance_length + 1}")
```

跑一次 `max_new_tokens=64` 的小生成，数三种打印的次数。

**需要观察的现象**：

- `[DRAFT]` 和 `[VERIFY]` 出现次数**完全相同**，等于循环轮数 N。
- 把所有 `[VERIFY]` 的 `yield` 加起来，应等于实际生成的 token 数（即 `output_ids` 长度减去 prompt 长度）。

**预期结果**：`N(草稿) == N(验证)`，且 `sum(yield) == num_output_tokens`。具体数值「待本地验证」。

**免改源码的交叉验证**：直接用 `return_stats=True` 调用，会返回带 `acceptance_lengths` 字段的对象（见 4.4），于是 `len(res.acceptance_lengths) == 轮数`、`sum(res.acceptance_lengths) == res.num_output_tokens`，可用来核对上面的打印。

#### 4.3.5 小练习与答案

**Q1**：如果某一轮草稿全错（`acceptance_length = 0`），这一轮还会产出 token 吗？产出几个？

**答案**：会，且产出 1 个。因为兜底 token `posterior[0]` 来自 target、一定正确，`start += 0 + 1 = 1`。所以即便草稿全错，DFlash 也不会「卡住」，最差退化为和纯 target 一样一步一个 token（外加一次白跑的草稿前向开销）。

**Q2**：为什么 `past_key_values_target.crop(start)` 必须在 `start += acceptance_length + 1` **之后**执行？

**答案**：`crop` 的参数是新前沿位置。`start` 更新后正好指向「下一个待生成位置」，把 target 缓存截到 `start` 就能精确丢弃被拒草稿的 KV，同时保留所有已提交 token 的 KV。顺序反了会让缓存长度与已提交 token 数不一致。

**Q3**：`block_output_ids[0]`（块的第一个位置）在起草阶段为什么不会被覆盖？

**答案**：因为 `block_output_ids[:, 1:] = sample(draft_logits)` 只写 `1:` 即后 B−1 位；第 0 位是从 `output_ids` 切出来的真锚点，始终保留，作为这一块去噪的「起点」。

---

### 4.4 DynamicCache 双缓存与 `_cuda_time` 计时

#### 4.4.1 概念说明

本模块回答两个问题：

1. **为什么需要两套 KV cache？** 因为 target 和 draft 是两个独立模型、各自有自己的层。target 缓存放 target 各层的 K/V，draft 缓存放 draft 各层的 K/V，二者不能混用。更关键的是，它们被裁剪的**时机和目的不同**：target 缓存是为了「回滚被拒草稿」，draft 缓存是为了「让每轮起草从干净上下文重启」。

2. **`return_stats=True` 时那些时间字段是怎么测的？** 因为 CUDA 异步，每个时间点都得先 `synchronize`。`time_to_first_token` 测的是 prefill；`time_per_output_token` 测的是稳态 decode，且**故意排除了第一次草稿前向的 prefill 开销**。

#### 4.4.2 核心流程

两套缓存的「一生」：

```
past_key_values_target:                past_key_values_draft:
  prefill: 填入 prompt 全部 KV           创建后为空
  每轮验证: 追加 B 个草稿位置的 KV         每轮起草: 处理一块, 追加 KV
            → crop(start) 截回前沿                → crop(start) 截回 start
  作用: 避免重算 prompt/历史               作用: 维持草稿自己的上下文边界
       + 回滚被拒草稿
```

计时流程：

```
prefill_start = _cuda_time()            # synchronize + 时钟
... prefill ...
time_to_first_token = _cuda_time() - prefill_start

decode_start = _cuda_time()
进入循环:
  第一轮块起草完成后(draft_prefill):
      decode_start = _cuda_time()        # ← 重置! 排除首次草稿 prefill
循环结束:
total_decode_time = _cuda_time() - decode_start
time_per_output_token = total_decode_time / num_output_tokens
```

#### 4.4.3 源码精读

**两套缓存的创建**（[L83-L84](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L83-L84)）：两个独立的 `DynamicCache()` 实例。

**`_cuda_time` 的实现**（[L57-L59](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L57-L59)）：

```python
def _cuda_time() -> float:
    torch.cuda.synchronize()   # 阻塞, 等 GPU 干完
    return time.perf_counter() # 再读高精度墙上时钟
```

没有 `synchronize`，测到的是 kernel 启动时间，结果会严重偏小。

**`draft_prefill` 重置 decode_start**（[L104-L105 与 L122-L124](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L102-L124)）：

```python
decode_start = _cuda_time() if return_stats else None
...
draft_prefill = True
...
while start < max_length:
    ...
    if block_size > 1:
        ...
        if draft_prefill and return_stats:
            draft_prefill = False
            decode_start = _cuda_time()   # 第一次草稿前向后重置计时起点
```

为什么要重置？第一次进循环时草稿缓存是空的，`model(...)` 这一次相当于草稿的 **prefill**（要把上下文一次性灌进草稿缓存），是一次性大开销。把它排除掉，`time_per_output_token` 才能反映「稳态 decode」的每 token 成本，这样和 baseline 比较加速比才公平。

**统计返回**（[L157-L169](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L157-L169)）：

```python
if not return_stats:
    return output_ids
num_output_tokens = output_ids.shape[1] - num_input_tokens
total_decode_time = _cuda_time() - decode_start
return SimpleNamespace(
    output_ids=output_ids,
    num_input_tokens=num_input_tokens,
    num_output_tokens=num_output_tokens,
    time_to_first_token=time_to_first_token,
    time_per_output_token=total_decode_time / num_output_tokens,
    acceptance_lengths=acceptance_lengths,
)
```

不要求统计就直接返回 `output_ids`；要求统计则返回一个 `SimpleNamespace`，含首 token 时间、每 token 时间、和每轮接受长度列表。

**谁在消费这些统计？** benchmark 正是靠 `block_size=1` vs `block_size=B` 两次调用对比加速比（[benchmark.py#L243-L254](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/benchmark.py#L243-L254)）：

```python
for bs in [1, block_size]:
    response[bs] = dflash_generate(
        draft_model, target=target, input_ids=input_ids, ...,
        block_size=bs, return_stats=True,
    )
```

`bs=1` 时整个 `block_size > 1` 分支被跳过，DFlash 退化成纯 target 自回归——这就是 baseline。然后 [benchmark.py#L120-L132](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/benchmark.py#L120-L132) 用 `baseline_tpot / dflash_tpot` 算加速比，并对 `acceptance_lengths` 画直方图。

#### 4.4.4 代码实践

**目标**：用 `return_stats=True` 直接拿到统计对象，验证 4.3 里关于「轮数」和「接受长度」的结论，无需改源码。

**步骤**：

```python
from dflash.model import DFlashDraftModel, dflash_generate   # 示例代码
# draft / target / input_ids 的准备同 README Transformers Quick Start
res = dflash_generate(
    draft, target=target, input_ids=input_ids,
    max_new_tokens=64, stop_token_ids=[tokenizer.eos_token_id],
    temperature=0.0, return_stats=True,
)
print("轮数(草稿=验证):", len(res.acceptance_lengths))
print("每轮产出之和   :", sum(res.acceptance_lengths))
print("num_output_tokens:", res.num_output_tokens)
print("time_to_first_token:", res.time_to_first_token)
print("time_per_output_token:", res.time_per_output_token)
```

**需要观察的现象**：`sum(res.acceptance_lengths)` 与 `res.num_output_tokens` **相等**（可能在命中 stop token 时有 ±1 的边界差异，取决于裁剪）；`time_to_first_token` 通常明显大于 `time_per_output_token`（因为 prefill 要处理整段 prompt）。

**预期结果**：上述两个等式关系成立；时间字段为正数。具体数值「待本地验证」。

#### 4.4.5 小练习与答案

**Q1**：如果不调用 `torch.cuda.synchronize()` 直接读 `time.perf_counter()`，`time_to_first_token` 会偏大还是偏小？为什么？

**答案**：偏小。CUDA 算子异步，`perf_counter` 在 kernel 还在 GPU 上排队/执行时就返回了，测到的是「发起」耗时而非真实计算耗时，加速比会被高估。

**Q2**：为什么 benchmark 用 `block_size=1` 作为 baseline？它和「不加载草稿模型」有什么区别？

**答案**：`block_size=1` 时 `dflash_generate` 跳过所有草稿分支，每轮 target 只处理 1 个 token、接受长度恒为 0、产出 1 个 token，等价于纯 target 自回归解码。它与「不加载草稿」效果相同，但复用了同一段代码路径，保证 baseline 与 DFlash 的计时口径一致、可比。

**Q3**：`draft_prefill` 这个标志在整个生成里会被置为 `False` 几次？

**答案**：恰好 1 次。它在循环前初始化为 `True`，第一次进入 `block_size > 1` 起草分支时被置为 `False` 并重置 `decode_start`，之后永远不再触发。这就是「排除一次性草稿 prefill 开销」的实现。

## 5. 综合实践

把本讲三个最小模块串起来，完成规格里要求的主实践。

**任务**：在 `dflash_generate` 中为 **prefill / 块起草 / 验证** 三个阶段各加一行打印，跑一次小生成，用输出验证「各阶段执行次数与产出 token 数之间的关系」。

**操作步骤**：

1. 按 README 的 Transformers Quick Start 准备 `draft`、`target`、`tokenizer`、`input_ids`（需 GPU）。
2. 在 [prefill 段末尾（L100 后）](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L86-L100) 加：
   ```python
   print("[PHASE] prefill done")
   ```
3. 在 [块起草末尾（L121 `block_output_ids[:, 1:] = sample(draft_logits)` 后）](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L107-L124) 加：
   ```python
   print("[PHASE] draft")
   ```
4. 在 [验证接受末尾（L140 `acceptance_lengths.append(...)` 后）](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L126-L140) 加：
   ```python
   print(f"[PHASE] verify accepted={acceptance_length} yield={acceptance_length+1}")
   ```
5. 用 `max_new_tokens=64`、`temperature=0.0` 跑一次，统计三种 `[PHASE]` 各打印多少次，以及所有 `yield` 之和。

**需要观察并解释的关系**：

| 观察 | 预期 |
|---|---|
| `prefill` 打印次数 | 恒为 1 |
| `draft` 打印次数 == `verify` 打印次数 | 相等，记为 N（循环轮数） |
| `sum(yield)` 与实际生成 token 数 | 相等（命中 stop 时可能有 ±1 边界差） |
| `N` 与 `max_new_tokens` 的关系 | `N ≈ max_new_tokens / (平均 a + 1)` |

**预期结果**：prefill 一次；draft 与 verify 次数相同；每轮 yield 之和等于输出 token 数。平均接受长度越大，N 越小，生成越快。具体数值「待本地验证」。

> 提示：如果你没有 GPU，可改为「源码阅读型实践」——在 4.3.3 的手动演算示例基础上，自己造一组 `block_output_ids` 与 `posterior`，手算 `acceptance_length`，验证 cumprod 求前缀的逻辑。

## 6. 本讲小结

- DFlash 推理分三阶段：**prefill（1 次）→ decode 循环（块起草 + 验证，反复）→ 收尾裁剪**。
- 每轮循环产出 **`acceptance_length + 1`** 个 token，其中 `+1` 是来自 target 的兜底 token；接受长度用 `cumprod` 求最长公共前缀得到。
- 维护**两套 `DynamicCache`**：target 缓存用于回滚被拒草稿（`crop(start)` 截回新前沿），draft 缓存用于每轮起草从干净上下文重启。
- 草稿模型在块起草阶段**两次借用 target**：`embed_tokens` 把 mask 变噪声嵌入，`lm_head` 把草稿隐藏状态转成 logits。
- 计时必须先 `torch.cuda.synchronize()`；`time_to_first_token` 测 prefill，`time_per_output_token` 刻意排除了首次草稿 prefill 开销，以反映稳态 decode。
- benchmark 用 `block_size=1` 作 baseline（此时 DFlash 退化成纯 target 自回归），与 `block_size=B` 对比算加速比。

## 7. 下一步学习建议

本讲建立了**全局控制流**，但故意把几样东西当黑盒：

- 「草稿模型 `model(...)` 内部到底怎么去噪」→ 下一讲 **u2-l2（草稿模型架构与配置）** 和 **u2-l3（DFlash 注意力与块扩散机制）**，会拆开 `DFlashDraftModel.forward` 和 `Qwen3DFlashAttention`，讲清 `target_hidden` 如何作为注意力的 key/value、`fc`/`hidden_norm` 的作用。
- 「`sample()` 的两个分支、`extract_context_feature` 的多层拼接细节」→ **u2-l4（验证接受循环与采样）**。
- 「`build_target_layer_ids` 怎么决定取 target 哪几层」→ **u2-l2**。

建议阅读顺序：u2-l2 → u2-l3 → u2-l4 → u2-l5，把本讲这张全局地图里的每个黑盒逐一打开。
