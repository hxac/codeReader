# 上下文去重：context_history 与重复跳过

## 1. 本讲目标

学完本讲，你应该能够：

- 说清楚「为什么同一个上下文（n-1 gram）在序列里重复出现时，不应被重复水印、也不应重复计入检测分数」。
- 掌握 `context_history` 这个滑动窗口是如何被维护的（初始化、压入、淘汰）。
- 读懂生成侧 `watermarked_call` 第 5 步里 `is_repeated_context` 的判定与「跳过水印」的 `torch.where`。
- 读懂检测侧 `compute_context_repetition_mask` 的循环逻辑，并准确解释它输出里 `0` 与 `1` 的含义、以及它与 `compute_g_values` 的位置对齐关系。
- 能亲手构造一段含重复上下文的序列，运行掩码函数并指出哪些位置被标记为重复。

## 2. 前置知识

本讲承接 u3-l2（`watermarked_call` 主流程）与 u2-l3（g 值），只看其中一个「横切」细节。复习三个要点：

- **上下文（context）= ngram 的前 `ngram_len-1` 个 token**。默认 `ngram_len=5`，所以上下文长度是 4。本讲把这种长度为 `ngram_len-1` 的片段称为「n-1 gram」。
- **g 值由 (上下文 + 候选 token + 密钥) 经哈希得到一颗 0/1 比特**，形状 `[batch, seq, depth]`，是连接生成侧与检测侧的唯一桥梁（见 u2-l3）。
- **`SynthIDState` 维护三块运行时记忆**：`context`（当前上下文）、`context_history`（已见上下文的滑动窗口）、`num_calls`（调用计数），见 u3-l1。

一个关键直觉：检测打分（无论是 Mean 还是 Bayesian）都默认参与打分的 g 值是**近似独立、无偏**的随机比特。本讲要回答的问题是——**当序列里出现重复上下文时，这条独立性假设会被破坏，项目是怎么处理的？**

## 3. 本讲源码地图

本讲几乎全部代码都集中在一个文件里：

| 文件 / 符号 | 作用 |
|---|---|
| `src/synthid_text/logits_processing.py` → `SynthIDState` | 定义 `context_history` 这个滑动窗口数据结构 |
| `src/synthid_text/logits_processing.py` → `SynthIDLogitsProcessor.watermarked_call`（第 5 步） | 生成侧：判定重复上下文并跳过水印 |
| `src/synthid_text/logits_processing.py` → `SynthIDLogitsProcessor._compute_keys` | 产出「仅上下文」的哈希 `hash_result_with_just_context` |
| `src/synthid_text/logits_processing.py` → `SynthIDLogitsProcessor.compute_context_repetition_mask` | 检测侧：从输出序列重算重复掩码 |
| `src/synthid_text/hashing_function.py` → `accumulate_hash` | 两侧共同用来把上下文哈希成 int64 的工具（见 u2-l2） |
| `src/synthid_text/logits_processing_test.py` → `test_compute_context_repetition_mask_shape` | 验证掩码输出形状的测试 |
| `README.md`（检测示例段） | 展示 `context_repetition_mask` 如何与 `eos_token_mask` 相乘 |

## 4. 核心概念与源码讲解

### 4.1 context_history 维护：用滑动窗口记录「见过的上下文」

#### 4.1.1 概念说明

要在生成时判断「当前上下文是不是之前见过的」，处理器必须记住历史上下文。直接把整段历史 token 都存下来太贵，于是项目用一个**固定大小的滑动窗口** `context_history`，只保留最近 `context_history_size` 个上下文的**哈希值**（int64）。

之所以存哈希而不是存原始 token 片段，原因有二：

1. 上下文长度 `ngram_len-1` 不固定，存变长片段不方便用张量批处理；哈希后每个上下文是一个 int64，可以直接铺成 `[batch, context_history_size]` 的定长张量。
2. 两侧（生成 / 检测）用的是**同一套哈希**（同一个 `hash_iv`、同一个 `accumulate_hash`），所以「同一个上下文」在两侧会得到**完全相同的哈希值**——这正是检测侧能够复刻生成侧去重逻辑的根本原因。

#### 4.1.2 核心流程

`context_history` 的生命周期只有三种操作：

1. **初始化**：首次需要状态时，`context_history` 被建为 `[batch, context_history_size]` 的**全 0** 张量。
2. **压入（push）**：每处理一个上下文，把它的哈希**插到最前（位置 0）**。
3. **淘汰（evict）**：插入后立刻砍掉最后一列（`[:, :-1]`），保持长度不变——最老的哈希被挤出去。

写成一个固定模式就是「前插一列 + 砍掉末列」，等价于一个** newest 在前、oldest 在后、容量恒定**的 FIFO 队列：

```text
压入 h_new 前:  [h0, h1, h2, ... , h_{S-1}]      # S = context_history_size
压入 h_new 后:  [h_new, h0, h1, ..., h_{S-2}]     # h_{S-1} 被淘汰
```

#### 4.1.3 源码精读

数据结构本身定义在 `SynthIDState.__init__` 里，`context_history` 被初始化为全 0：

[logits_processing.py:119-123](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L119-L123) —— 把 `context_history` 建成 `[batch_size, context_history_size]` 的 int64 全 0 张量。「全 0」是个惰性初值：真实上下文的哈希是由非零 `hash_iv` 经 `accumulate_hash` 得到的大整数，几乎不可能等于 0，所以首条记录不会被误判为重复。

维护逻辑在 `watermarked_call` 第 5 步（4.2 节详讲判定，这里只看维护那两行）：

[logits_processing.py:316-319](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L316-L319) —— `torch.concat((新哈希, 旧history), dim=1)` 把新哈希插到最前，再 `[:, :-1]` 砍掉最后一列，长度始终回到 `context_history_size`。这就是上面「前插 + 砍尾」的实现。

> 检测侧 `compute_context_repetition_mask` 里会**一字不差地复用同样的两句维护代码**（见 4.3.3），这是两侧对齐的关键。

#### 4.1.4 代码实践（源码阅读型）

**实践目标**：看清 `context_history` 的形状与更新规则，并理解 `context_history_size` 的边界作用。

**操作步骤**：

1. 打开 `src/synthid_text/logits_processing.py`，定位 `SynthIDState.__init__`（L99-L124），确认 `context_history` 的形状是 `(batch_size, context_history_size)`。
2. 再看 `watermarked_call` 里 L316-L319 这两句，在草稿纸上画出连续压入 3 个哈希后 `context_history`（设 `context_history_size=4`）的内容变化。
3. 思考：如果同一个上下文相隔超过 `context_history_size` 步再次出现，还能被识别为重复吗？

**需要观察的现象**：每压入一个新哈希，窗口整体右移一格、末尾被丢弃；窗口里始终只有最近 `context_history_size` 个哈希。

**预期结果**：相隔超过窗口大小的重复上下文的哈希早已被挤出窗口，**检测不到**，会被当作「未重复」。这是用有限内存换取的近似——窗口越大越准、越费显存。

#### 4.1.5 小练习与答案

**练习 1**：`context_history` 初始化为全 0。为什么序列里第一个上下文永远不会被判为「重复」？

**答案**：判定时窗口还是全 0，而真实上下文的哈希是由非零 `hash_iv` 经 `accumulate_hash` 得到的大整数，与 0 相等的概率几乎为零，所以「窗口里存在相等哈希」的判断为假，第一个上下文必然判为未重复。

**练习 2**：把 `context_history_size` 从 1024 调到 4，对一段很长的序列里相隔很远的相同上下文有什么影响？

**答案**：滑动窗口只保留最近 4 个上下文哈希，相隔超过 4 步的重复上下文哈希已被挤出窗口，无法识别，会被误判为「未重复」（掩码=1）。窗口大小是「准确度 vs 内存」的旋钮。

---

### 4.2 is_repeated_context 判定：生成侧如何决定「跳过水印」

#### 4.2.1 概念说明

本节回答两个问题：**为什么重复上下文要跳过水印？生成侧具体怎么跳？**

**为什么要跳过**——水印会把概率质量系统性地推向 g=1 的 token（见 u3-l3 的得分更新）。如果同一个上下文在生成过程中反复出现，模型会被反复推向同一组 token，容易陷入**退化重复**（degenerate repetition），文本质量下降；而且这些重复位置产生的 g 值与首次出现时**高度相关甚至完全相同**（同一个 ngram → 同一个 g 值），并不携带「额外的」水印证据。

**怎么跳过**——在 `watermarked_call` 第 5 步，处理器先算出当前上下文的哈希，去 `context_history` 里查是否已存在：

- 若**已存在**（`is_repeated_context=True`）：返回**原始、未水印**的 `scores_top_k`，相当于这一步不施加水印。
- 若**不存在**：返回水印后的 `updated_scores`。

注意一个时序细节：**先判定、后压入**。判定用的是「之前见过的上下文」集合；判定完才把当前上下文记入窗口。若反过来（先压入再查），当前哈希已在窗口里，每个上下文都会被判为重复，水印就永远施加不了了。

#### 4.2.2 核心流程

`watermarked_call` 第 5 步（紧接 g 值采样与得分更新之后）的伪代码：

```text
context_hash = 哈希(state.context)                 # 仅上下文的哈希, [B]
context_hash = context_hash[:, None]               # [B, 1]

is_repeated = (context_history == context_hash).any(dim=1, keepdim=True)  # [B, 1] 布尔

# 维护窗口（4.1 节）
context_history = concat([context_hash, context_history], dim=1)[:, :-1]

# 关键：重复则用原始未水印分数，否则用水印分数
return where(is_repeated, scores_top_k, updated_scores)
```

#### 4.2.3 源码精读

当前上下文的哈希并非单独重算，而是复用了第 2 步 `_compute_keys` 的**副产物** `hash_result_with_just_context`（仅对上下文做了一次 `accumulate_hash` 的中间结果，详见 u3-l2）：

[logits_processing.py:427-429](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L427-L429) —— `_compute_keys` 里这一步把 `hash_iv` 与上下文 `n_minus_1_grams` 累加哈希，得到「仅上下文」的哈希，既用于后续算 ngram key，也作为返回值的第二项供第 5 步去重使用。

第 5 步主体：

[logits_processing.py:307-319](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L307-L319) —— 先把哈希 reshape 成 `[B,1]`，再用 `(context_history == context_hash).any(dim=1, keepdim=True)` 在窗口里查重，得到 `is_repeated_context`；紧接着维护窗口。注意查重发生在压入**之前**。

最后用 `torch.where` 选择返回哪一份分数：

[logits_processing.py:321-326](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L321-L326) —— `torch.where(is_repeated_context, input=scores_top_k, other=updated_scores)`：条件为真（重复）时取 `input`，即**原始未水印**的 `scores_top_k`；否则取水印后的 `updated_scores`。这就是「重复上下文跳过水印」的落点。

#### 4.2.4 代码实践（源码阅读型）

**实践目标**：确认「跳过水印」的语义与时序，理解 `torch.where` 的取值方向。

**操作步骤**：

1. 读 L309-L315，确认 `is_repeated_context` 的形状是 `[B, 1]`、类型是布尔。
2. 读 L321-L325 的 `torch.where(...)`，回答：当 `is_repeated_context[b]` 为 True 时，返回三元组里第一个元素（`updated_watermarked_scores[b]`）等于 `scores_top_k[b]` 还是 `updated_scores[b]`？
3. 追踪一次「查重 → 压入」的顺序，解释为什么必须先查后压。

**需要观察的现象 / 预期结果**：

- 重复时返回的是 `scores_top_k`（未水印），即该步水印被跳过。
- 若把 L316-L319 的维护代码挪到 L310 的查重**之前**，`is_repeated_context` 会恒为 True，水印彻底失效——这反过来说明「先查后压」是正确性所必需的。

#### 4.2.5 小练习与答案

**练习 1**：在 `torch.where(is_repeated_context, input=scores_top_k, other=updated_scores)` 中，当某个 batch 的上下文重复时，返回的是哪一份 scores？为什么？

**答案**：返回 `scores_top_k`（原始、未水印的 top-k 分数）。因为 `torch.where` 在 `condition=True` 时取 `input`；重复上下文应跳过水印，故用未水印分数。

**练习 2**：为什么「查重」必须发生在「把当前上下文压入 history」之前？

**答案**：若先压入再查，当前上下文的哈希已在窗口里，`any(...)` 必为 True，于是每个上下文都被判为重复，水印永远不会施加。必须先用「之前见过的上下文」判定，再把当前上下文记入。

---

### 4.3 compute_context_repetition_mask：检测侧的镜像与输出语义

#### 4.3.1 概念说明

检测阶段不信任生成时的状态，只拿到**最终的输出 token 序列**，从零重算一切（见 u1-l4）。那么检测侧如何知道哪些 g 值来自「被跳过水印的重复上下文」、不该计入分数？

答案就是 `compute_context_repetition_mask`：它**复刻**生成侧的查重逻辑——用同样的 `hash_iv + accumulate_hash` 把每个上下文哈希一遍，用同样的滑动窗口判定是否重复——从而给每个 g 值位置打一个标签。

它的输出语义由 docstring 明确规定：**`0` 表示重复上下文（应排除），`1` 表示未重复上下文（参与打分）**。注意这与内部变量 `is_repeated_context`（True=重复）正好相反——函数末尾用 `torch.logical_not(...)` 翻转了一次，让 `1` 表示「可用」，便于直接和 `eos_token_mask` 相乘。

为什么检测侧也要排除重复上下文？从统计角度看，打分本质上是

\[
\text{score}\;\approx\;\frac{\sum_i m_i\,g_i}{\sum_i m_i},
\]

其中 \(m_i\) 是掩码。若不排除重复上下文，相关的 g 值会被重复计入，等价于**虚增了有效样本数**，使分数方差的估计失真，可能让检测器在非水印文本上误报、或在水印文本上过度自信。

#### 4.3.2 核心流程

```text
state = 新建 SynthIDState(context_history 全 0)           # 检测从零开始
contexts = input_ids[:, :-1].unfold(dim=1, size=H, step=1) # [B, num_contexts, H], H=ngram_len-1

for i in 0 .. num_contexts-1:
    ctx = contexts[:, i, :]
    ctx_hash = accumulate_hash(hash_iv, ctx)[:, None]      # 与生成侧同款哈希
    is_repeated = (state.context_history == ctx_hash).any(dim=1, keepdim=True)
    记录 is_repeated
    state.context_history = concat([ctx_hash, history], dim=1)[:, :-1]   # 同款维护

return logical_not(所有 is_repeated)                        # 1=未重复(可用), 0=重复(排除)
```

两个对齐要点：

- **上下文提取**：`input_ids[:, :-1].unfold(size=ngram_len-1, step=1)`。先 `[:, :-1]` 去掉最后一个 token，是为了让「上下文数量」恰好等于「g 值数量」（都是 `input_len - (ngram_len-1)`），位置一一对应。
- **输出长度**：`(batch_size, input_len - (ngram_len - 1))`，与 `compute_g_values` 的序列维完全一致，可以直接逐位置相乘。

#### 4.3.3 源码精读

docstring 明确了输出语义与形状：

[logits_processing.py:475-488](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L475-L488) —— `0` 和 `1` 分别表示「重复」和「未重复」的上下文 n-1 gram；返回形状 `(batch_size, input_len - (ngram_len - 1))`。

函数开头新建一个**全新的、`context_history` 全 0** 的 `state`，强调检测侧不继承任何生成期状态：

[logits_processing.py:489-502](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L489-L502) —— 新建 `SynthIDState`，再用 `input_ids[:, :-1].unfold(size=ngram_len-1, step=1)` 切出所有上下文，形状 `[B, num_contexts, H]`。

主循环逐个上下文哈希、查重、压入——与 `watermarked_call` 第 5 步**逐行对应**：

[logits_processing.py:504-523](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L504-L523) —— 对每个上下文：用 `hash_iv` 经 `accumulate_hash` 算哈希（与生成侧同款）、在窗口里 `any(...)` 查重、记录、再用同样的「前插 + 砍尾」维护窗口。注意这里也是**先查后压**。

最后做一次逻辑翻转：

[logits_processing.py:525](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L525) —— `torch.logical_not(are_repeated_contexts)` 把「True=重复」翻成「1=可用 / 0=排除」，便于直接和其它掩码相乘。

生成的 `combined_mask` 会在后续打分时用到，README 给出了拼接方式：

[README.md:241-248](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/README.md#L241-L248) —— `context_repetition_mask` 与 `eos_token_mask` 相乘得到 `combined_mask`，再交给 `detector.score(g_values, combined_mask)`。重复上下文（0）会让对应位置被排除出打分。

#### 4.3.4 代码实践（可运行）

**实践目标**：构造一段含重复上下文的序列，运行 `compute_context_repetition_mask`，亲手看到哪些位置被标记为重复（值为 0）。

**操作步骤**：

1. 把下面的「示例代码」保存为脚本并运行（需先按 u1-l2 安装好依赖）。

   ```python
   # 示例代码
   import torch
   from synthid_text import logits_processing

   # 用很小的 ngram_len=3 方便手算：上下文长度 H = ngram_len-1 = 2
   processor = logits_processing.SynthIDLogitsProcessor(
       ngram_len=3,
       keys=[1, 2, 3],          # depth = len(keys) = 3
       context_history_size=16,
       temperature=0.7,         # 构造时必须合法（>0 的 float）
       top_k=10,                # 构造时必须合法（>1 的 int）
       device=torch.device('cpu'),
   )

   # 构造一段含「重复 2-gram 上下文」的序列：[1,2] 在位置 0 和 3 各出现一次
   seq = torch.tensor([[1, 2, 3, 1, 2, 4]], dtype=torch.int64)
   mask = processor.compute_context_repetition_mask(seq)
   print("mask      =", mask)        # 预期 tensor([[1, 1, 1, 0]])
   print("mask.shape=", mask.shape)  # 预期 torch.Size([1, 4]) = (1, 6-(3-1))
   ```

2. 对照下表，逐位置核对你的输出（`ngram_len=3`，所以上下文长度 H=2）：

   | g 值位置 j | 上下文（n-1 gram）`seq[j:j+2]` | 是否曾在前面出现 | mask |
   |---|---|---|---|
   | 0 | `[1, 2]` | 否（首次） | 1 |
   | 1 | `[2, 3]` | 否 | 1 |
   | 2 | `[3, 1]` | 否 | 1 |
   | 3 | `[1, 2]` | **是**（与位置 0 相同） | **0** |

3. 把 `ngram_len` 改成 `5`（默认值），重新构造一段含重复 4-gram 上下文的更长序列，再跑一次，观察 mask 里出现 `0` 的位置是否符合「首次出现=1、之后重复=0」的规律。

**需要观察的现象**：序列里 `[1, 2]` 这个上下文在位置 0 和位置 3 各出现一次；位置 0 是首次出现（mask=1），位置 3 是重复（mask=0）。其余上下文各只出现一次（mask=1）。

**预期结果**：根据代码逻辑推导，`mask` 应为 `tensor([[1, 1, 1, 0]])`，形状 `(1, 4)`。（这是按源码逻辑手工推导的结果，请本地运行确认。）位置 3 对应的 g 值会在检测时被排除出打分。

#### 4.3.5 小练习与答案

**练习 1**：`compute_context_repetition_mask` 的输出里，`1` 和 `0` 分别表示什么？它与 `compute_g_values` 的输出在序列维上如何对齐？

**答案**：`1`=未重复（参与打分），`0`=重复（排除）。两者序列维长度都是 `input_len-(ngram_len-1)`，位置一一对应：第 `j` 个 mask 值对应第 `j` 个 g 值，其上下文是 `input_ids[j:j+ngram_len-1]`。

**练习 2**：`contexts = input_ids[:, :-1].unfold(...)` 里为什么要先 `[:, :-1]` 去掉最后一个 token？

**答案**：不去掉会多出一个窗口，它对应序列末尾、后面没有候选 token 凑成完整 ngram，也就没有对应的 g 值。去掉最后一个 token 让「上下文数量」=`input_len-(ngram_len-1)`，正好等于 g 值数量，保证位置对齐。

## 5. 综合实践

把三个最小模块串起来：**维护窗口 → 判定重复 → 检测镜像**，并把它接上打分管线。

任务（设计 + 少量代码）：

1. 构造一段长一些、含若干重复 4-gram 上下文的序列（例如让某 4 个 token 的片段在序列里出现 3 次）。
2. 同时调用 `compute_g_values(seq)` 与 `compute_context_repetition_mask(seq)`，**打印二者的形状**，验证它们在序列维长度一致、位置一一对应。
3. 自行用 `eos_token_id` 调用 `compute_eos_token_mask`，再按 README 的方式算出 `combined_mask = context_repetition_mask * eos_token_mask`（注意 `eos_token_mask` 要像 README 那样先 `[:, ngram_len-1:]` 对齐到 g 值维度）。
4. 数一数：最终 `combined_mask` 里有几个 `1`？这些 `1` 的位置就是**真正会被打分器计入**的 g 值。解释为什么重复上下文对应的 g 值被排除了——它和 4.2 节「生成侧跳过水印」是如何呼应的？

**预期结论**：只有「首次出现的、且不含 EOS、确实被水印过的」上下文对应的 g 值才会进入打分；生成侧对这些位置施加水印、检测侧对这些位置计入分数，两侧通过同一套哈希与同一套查重规则保持一致。

## 6. 本讲小结

- 重复上下文（n-1 gram）会破坏 g 值的独立性假设，让打分虚增有效样本数；因此生成侧要**跳过水印**、检测侧要**排除出打分**。
- `context_history` 是一个「前插 + 砍尾」的定长滑动窗口，存的是上下文哈希（int64），大小由 `context_history_size` 决定，是「准确度 vs 内存」的旋钮。
- 生成侧 `watermarked_call` 第 5 步：用副产物 `hash_result_with_just_context` 在窗口里**先查后压**得到 `is_repeated_context`，再用 `torch.where` 在重复时返回**未水印**的 `scores_top_k`。
- 检测侧 `compute_context_repetition_mask` 用**同样的哈希与同样的窗口维护**从零复刻查重逻辑，输出 `1=可用 / 0=排除`，长度与 `compute_g_values` 一致。
- 两侧能对齐的根本原因：用的是同一套 `hash_iv + accumulate_hash`，所以「重复」在两侧含义完全相同。

## 7. 下一步学习建议

- 本讲是单元三（水印施加机制）的最后一讲。下一单元 **u4（HuggingFace 集成）** 会讲 `SynthIDLogitsProcessor` 是如何通过 Mixin 挂进 `model.generate` 的采样循环里的——你会看到本讲的 `watermarked_call`（含重复跳过）在真实生成中是如何被逐 token 调用的。
- 如果你更关心检测，可以直接跳到 **u5-l1（检测所需的掩码体系）**：那里会把本讲的 `context_repetition_mask` 与 `eos_token_mask` 合成 `combined_mask`，并讲清楚掩码如何决定哪些 g 值进入 Mean / Bayesian 打分。
- 建议同时翻一遍 `logits_processing_test.py` 里的 `test_compute_context_repetition_mask_shape`（L364-L377），它用一个形状断言锁住了本讲函数的输出契约。
