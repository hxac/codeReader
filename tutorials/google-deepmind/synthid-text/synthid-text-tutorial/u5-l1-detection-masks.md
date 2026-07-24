# 检测所需的掩码体系

## 1. 本讲目标

本讲是检测侧的第一讲。在前面几讲里，我们已经知道 SynthID Text 在「生成时」把水印埋进 logits，在「检测时」只拿输出的 token 序列 + 密钥**重新计算 g 值**再打分。但有一个关键问题被我们暂时忽略了：

> 并不是序列里**每一个** g 值都应该参与打分。

本讲学完后，你应该能够：

1. 说清楚为什么有些 g 值必须被「屏蔽」（mask 掉），不能计入检测分数。
2. 读懂 `compute_eos_token_mask` 的截断语义，知道它如何用第一个 EOS token 把序列截断。
3. 读懂 `compute_context_repetition_mask` 在检测侧的作用，并理解它为什么是生成侧「上下文去重」逻辑的镜像。
4. 掌握 `combined_mask` 的构造方式，尤其是两个掩码如何对齐到同一个序列长度后再相乘。

## 2. 前置知识

本讲建立在两讲之上，请确认你已经掌握：

- **u2-l3 g 值是什么**：一段长度为 \(N\) 的 token 序列，经 `compute_g_values` 后得到形状为 `[batch, L, depth]` 的 g 值，其中序列维长度 \(L = N - (ngram\_len - 1)\)。也就是说，**序列长度会在 g 值这一步缩短 `ngram_len - 1`**，因为每颗 g 值需要一个完整的 ngram 才算得出来。
- **u3-l4 上下文去重**：生成侧在 `watermarked_call` 第 5 步里，会判断「当前上下文（n-1 gram）是否之前已经出现过」，若重复就跳过水印。检测侧必须用**完全一致的判重规则**，否则生成与检测会对不齐。

如果你还记得这两个结论，本讲会非常顺。如果有点模糊，建议先回顾那两讲再继续。

此外再补充一个本讲会用到的工程背景：检测时输入的 `outputs` 通常是「补齐到固定长度」的 batch 张量。模型一旦生成 EOS（end-of-sequence）就理应停止，但为了批处理，后面会被填上无意义的 token。这些**填充 token 产生的 g 值是纯噪声**，必须屏蔽掉。

## 3. 本讲源码地图

本讲几乎全部源码集中在 `src/synthid_text/logits_processing.py`，外加 README 里一段「如何把三个掩码拼起来」的示范代码。

| 文件 | 关键函数 / 片段 | 作用 |
| --- | --- | --- |
| `src/synthid_text/logits_processing.py` | `compute_eos_token_mask` | 按 EOS 截断，生成「到 EOS 为止」的 0/1 掩码 |
| `src/synthid_text/logits_processing.py` | `compute_context_repetition_mask` | 检测侧复刻生成侧的上下文去重逻辑 |
| `src/synthid_text/logits_processing.py` | `SynthIDState`（被上面两个函数复用） | 提供全零初始化的 `context_history` 滑动窗口 |
| `src/synthid_text/logits_processing.py` | `compute_g_values` | 用来对照 g 值的序列维长度 \(L\) |
| `README.md` | 「Detecting a watermark」一节的最后一段 | 把两个掩码对齐、相乘得到 `combined_mask`，再喂给 `detector.score` |

一个贯穿全讲的关键数字：**所有掩码最终都要对齐到长度 \(L = N - (ngram\_len - 1)\)**，也就是 g 值的序列维长度。三个掩码函数在「长度」这件事上各有各的处理方式，理解它们的对齐关系是本讲的重点。

## 4. 核心概念与源码讲解

### 4.1 compute_eos_token_mask：用第一个 EOS 把序列截断

#### 4.1.1 概念说明

检测时拿到的 `outputs` 张量通常是补齐过的：模型在某一步生成了 EOS，但 batch 里其他序列还在继续生成，于是这一条序列从 EOS 之后的位置全是填充 token。这些填充 token：

- 不是模型「有意」生成的；
- 它们对应的 g 值没有任何水印意义，是纯噪声。

如果把它们也算进打分，会**稀释**水印信号、拉低检测准确率。所以需要一个掩码：**在第一个 EOS 出现之前的位置标 1（保留），从第一个 EOS 开始（含）及之后的位置标 0（丢弃）**。

为什么强调「第一个」？因为序列里可能不止一个 EOS（比如 padding 复用 EOS），但只要遇到第一个，生成事实上就已经结束了，后面全是填充，不需要再关心后面是否还有 EOS。

#### 4.1.2 核心流程

对 batch 里每一条序列独立处理：

1. 用 `input_ids == eos_token_id` 得到一个布尔张量，标记每个位置是不是 EOS。
2. 找到**第一个** True 的下标 `first_eos`。
3. 构造一个全 1 的掩码，然后把 `[first_eos:]`（含 first_eos 到末尾）全部置 0。
4. 若序列里完全没有 EOS，则整条都是 1（整条都保留）。

伪代码：

```text
noneos_mask = [1] * N
if 存在 EOS:
    noneos_mask[first_eos:] = 0
# 结果：[1, 1, ..., 1, 0, 0, ..., 0]
#                ↑ first_eos
```

注意一个**容易踩坑的点**：`compute_eos_token_mask` 返回的长度是**完整的输入长度 \(N\)**，而不是 g 值的长度 \(L\)。把它用于打分前，调用方必须额外做一次切片对齐（见 4.3）。

#### 4.1.3 源码精读

函数定义与文档字符串：[src/synthid_text/logits_processing.py:527-552](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L527-L552) —— 它接收 `input_ids (batch, N)` 和一个 `eos_token_id`，返回 `(batch, N)` 的掩码。

> 小提示：源码里这个函数的 docstring 第一行写着 `Computes repetitions mask.`，这是个复制粘贴遗留的笔误（和下一个函数撞名了），但紧接着的描述 `1 stands for ngrams that don't contain EOS tokens` 才是它的真实语义。**以源码逻辑和这句描述为准。**

核心几行：

```python
all_eos_equated = input_ids == eos_token_id          # (batch, N) 布尔，哪里是 EOS
for eos_equated in all_eos_equated:                  # 逐条序列
  nonzero_idx = torch.nonzero(eos_equated)           # 所有 EOS 的下标
  noneos_mask = torch.ones_like(eos_equated)         # 全 1 起步
  if nonzero_idx.shape[0] != 0:                      # 这条里有 EOS
    noneos_mask[nonzero_idx[0][0] :] = 0             # 只取第一个 EOS，从它开始置 0
  noneos_masks.append(noneos_mask)
return torch.stack(noneos_masks, dim=0)
```

几个关键细节：

- `nonzero_idx[0][0]` 就是**第一个** EOS 的位置；后面再有 EOS 一律忽略，因为切片 `[first_eos:]` 已经把尾部全覆盖了。
- `if nonzero_idx.shape[0] != 0` 保证了「没有 EOS 时整条保留」。
- 返回类型是布尔张量（`ones_like` 一个布尔张量得到 `True`，赋 `0` 得到 `False`）。

#### 4.1.4 代码实践

**实践目标**：手工验证「只看第一个 EOS」的截断行为，尤其是当序列里出现**两个** EOS 时。

**操作步骤**（示例代码，非项目原有，仅用于理解行为）：

```python
import torch
from synthid_text import logits_processing

processor = logits_processing.SynthIDLogitsProcessor(
    ngram_len=3, keys=[1, 2, 3],
    context_history_size=10, temperature=0.5,
    top_k=40, device=torch.device("cpu"),
)

# 注意：这条序列里 9 (EOS) 出现了两次，分别在位置 2 和 5
seq = torch.LongTensor([[4, 7, 9, 1, 2, 9, 3]])
mask = processor.compute_eos_token_mask(seq, eos_token_id=9)
print(mask)
```

**预期结果**：`tensor([[1, 1, 0, 0, 0, 0, 0]])`。第一个 EOS 在位置 2，所以从位置 2 起全为 0；位置 5 的第二个 EOS 不会改变任何东西。

**待本地验证**：请你实际运行并确认输出与上述一致，重点观察「第二个 EOS 没有任何额外影响」。

#### 4.1.5 小练习与答案

**练习 1**：如果一条序列**不含**任何 EOS token，`compute_eos_token_mask` 返回什么？
**答案**：返回一条全 1 的掩码（长度仍为 \(N\)），表示整条序列都保留。对应代码里 `nonzero_idx.shape[0] == 0` 分支，不执行置 0。

**练习 2**：为什么这个函数返回长度是 \(N\) 而不是 \(L\)？
**答案**：因为它只关心「token 本身是不是 EOS」，与 ngram 无关，所以按 token 逐位给出掩码，自然长度是输入长度。把长度压成 \(L\) 是**调用方**的职责（见 4.3 的切片），这样函数本身更通用。

---

### 4.2 compute_context_repetition_mask：检测侧的重复上下文屏蔽

#### 4.2.1 概念说明

回顾 u3-l4：生成侧在 `watermarked_call` 第 5 步会判断「当前上下文（n-1 gram）是否在 `context_history` 里出现过」，若重复就**跳过水印**（返回未加水印的原始 scores）。

这意味着：**重复上下文对应的那个 g 值，并没有真正承载水印信号**。如果检测时还把它当成有效 g 值计入分数，就会：

- **虚增有效样本数**，让分数看起来比实际更可信；
- 破坏打分公式假设的「g 值近似独立、无偏」前提，导致方差估计失真。

所以检测侧必须**复刻同一套判重规则**，把重复上下文对应的 g 值屏蔽掉。`compute_context_repetition_mask` 就是干这件事的。它输出 `1` 表示「上下文未重复 → 保留」，`0` 表示「上下文重复 → 丢弃」。

为什么生成侧和检测侧的判重结果一定一致？因为两侧共用同一套 `hash_iv` 与 `accumulate_hash`、同样的滑动窗口维护逻辑（见 u2-l2、u3-l1），所以「哪些上下文算重复」在两侧含义完全相同。

#### 4.2.2 核心流程

对一条长度为 \(N\) 的序列：

1. **新建一个全新的 `SynthIDState`**：它的 `context_history` 全零初始化，模拟「从零开始逐个看上下文」。
2. 取出所有上下文（n-1 gram）：先 `input_ids[:, :-1]` 去掉最后一个 token（它只可能是某个 ngram 的「候选」，永远不会作为上下文），再用 `unfold` 滑窗得到所有 \((ngram\_len-1)\)-gram。这一步得到的上下文数量恰好是 \(L = N - (ngram\_len-1)\)。
3. **从左到右逐个处理上下文**（与生成顺序一致）：
   - 用 `hash_iv` 哈希当前上下文，得到 `context_hash`；
   - 查 `context_history` 里是否已有这个 hash → 得到 `is_repeated`；
   - 把当前 `context_hash` 前插进 `context_history`，砍掉末列，维护滑动窗口。
4. 最后用 `torch.logical_not` 把语义**翻转**：函数返回 `1=未重复`、`0=重复`，正好和打分时「1 才计入」的约定一致。

输出长度直接就是 \(L\)，**天然和 g 值对齐**，这一点和 4.1 的 eos 掩码不同。

#### 4.2.3 源码精读

函数定义：[src/synthid_text/logits_processing.py:475-525](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L475-L525) —— 接收 `input_ids (batch, N)`，返回 `(batch, L)` 的重复掩码，其中 `L = N - (ngram_len - 1)`。

它复用的 `SynthIDState` 在这里：[src/synthid_text/logits_processing.py:96-124](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L96-L124) —— 注意它被全新创建，`context_history` 全零。

关键代码段：

```python
state = SynthIDState(...)                      # 全新的全零 context_history
contexts = input_ids[:, :-1].unfold(           # 去掉末 token 后滑窗取所有 (n-1) gram
    dimension=1, size=self.ngram_len - 1, step=1
)
_, num_contexts, _ = contexts.shape            # num_contexts = L

are_repeated_contexts = []
for i in range(num_contexts):                  # 逐个上下文，顺序与生成一致
  context = contexts[:, i, :]
  hash_result = torch.full((batch_size,), self.hash_iv, ...)  # 同一个 IV
  context_hash = hashing_function.accumulate_hash(hash_result, context)[:, None]
  is_repeated_context = (state.context_history == context_hash).any(
      dim=1, keepdim=True)                     # 查窗口里是否见过
  are_repeated_contexts.append(is_repeated_context)
  state.context_history = torch.concat(        # 前插 + 砍尾，维护滑动窗口
      (context_hash, state.context_history), dim=1)[:, :-1]

are_repeated_contexts = torch.concat(are_repeated_contexts, dim=1)
return torch.logical_not(are_repeated_contexts)  # 翻转：1=未重复, 0=重复
```

三个要点：

- **`input_ids[:, :-1]`**：去掉最后一个 token。因为最后一个 token 只会作为某个 ngram 的「候选 token」出现，永远不会进入任何上下文；去掉它后 `unfold` 得到的上下文数正好等于 \(L\)，和 g 值一一对应。
- **顺序循环 `for i in range(num_contexts)`**：必须按序列从左到右处理，`context_history` 才能正确反映「到此为止见过哪些上下文」。这和生成侧逐 token 推进是同一个意思。
- **`logical_not` 翻转**：内部 `is_repeated_context` 是「True=重复」，但打分约定「1=计入」，所以末尾翻转一次。这个翻转是 4.3 里 `combined_mask` 相乘能成立的前提。

对照生成侧：这段逻辑与 [src/synthid_text/logits_processing.py:309-325](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L309-L325)（`watermarked_call` 第 5 步的查重 + `context_history` 维护）**算法完全同源**，只是一个逐 token、一个整批逐上下文。

#### 4.2.4 代码实践

**实践目标**：手工找出一条小序列里「哪个上下文是重复的」，并验证掩码对应位置为 0。

**操作步骤**：

```python
import torch
from synthid_text import logits_processing

processor = logits_processing.SynthIDLogitsProcessor(
    ngram_len=3, keys=[1, 2, 3],
    context_history_size=10, temperature=0.5,
    top_k=40, device=torch.device("cpu"),
)

# ngram_len=3 => 上下文长度=2。序列里 [3,7] 在位置1和位置3各出现一次 => 重复
seq = torch.LongTensor([[5, 3, 7, 3, 7, 9, 1]])   # 长度 N=7
mask = processor.compute_context_repetition_mask(seq)
print(mask)   # 长度 L = 7 - 2 = 5
```

**预期结果**：`tensor([[1, 1, 1, 0, 1]])`。

手工推导（上下文 = 连续 2 个 token）：

| i | 上下文 | 是否之前见过 | 掩码值 |
| --- | --- | --- | --- |
| 0 | [5, 3] | 否 | 1 |
| 1 | [3, 7] | 否 | 1 |
| 2 | [7, 3] | 否 | 1 |
| 3 | [3, 7] | **是（同 i=1）** | **0** |
| 4 | [7, 9] | 否 | 1 |

因为相同的 token 经 `accumulate_hash` 必然得到相同 hash（函数是确定性的），所以 `[3,7]` 在位置 3 必然被判为重复。

**待本地验证**：运行确认输出为 `[1,1,1,0,1]`，并尝试把序列改成无重复上下文（例如 `[5,3,7,9,1,2,4]`），观察是否得到全 1。

#### 4.2.5 小练习与答案

**练习 1**：`context_history_size` 这个参数会影响本函数的输出吗？
**答案**：会。它是滑动窗口容量，决定「往回看多远」。若一个上下文在很久以前出现过、但已经滑出窗口，就不会被判为重复。窗口越大越接近「全序列去重」，但内存占用越高。本函数新建 state 时用的就是处理器的 `self.context_history_size`。

**练习 2**：为什么函数末尾要 `logical_not`？
**答案**：内部 `is_repeated_context` 用 `True` 表示「重复」，但下游打分（以及 `combined_mask` 相乘）的约定是「1=计入、0=丢弃」。翻转后语义统一为「1=未重复→保留、0=重复→丢弃」，便于直接参与乘法。

---

### 4.3 combined_mask：两掩码相乘与序列对齐

#### 4.3.1 概念说明

现在我们有两个掩码，分别排除两类「坏 g 值」：

- `eos_token_mask`：排除 EOS 之后的填充噪声；
- `context_repetition_mask`：排除重复上下文对应的、未真正承载水印的 g 值。

一个 g 值**只有同时满足两个条件**才该计入分数：既在 EOS 之前、又来自未重复的上下文。这恰好是逻辑「与」，对 0/1 掩码来说就是**逐元素相乘**：

```text
combined_mask = context_repetition_mask * eos_token_mask
```

任意一个为 0，结果就是 0（丢弃）；两个都为 1，结果才是 1（保留）。最终 `combined_mask` 连同 `g_values` 一起喂给 `detector.score(g_values, combined_mask)`。

#### 4.3.2 核心流程

构造 `combined_mask` 有一个**必须小心**的对齐步骤，因为两个掩码「出厂长度」不一样：

1. `compute_context_repetition_mask` 直接返回长度 \(L\)，无需处理。
2. `compute_eos_token_mask` 返回完整长度 \(N\)，需要**砍掉前 `ngram_len - 1` 个位置**，把长度压成 \(L\)。
3. 两者都变成 `(batch, L)` 后，逐元素相乘得到 `combined_mask (batch, L)`，与 `g_values` 的序列维严格对齐。

为什么 eos 掩码要砍**前面**而不是后面？砍完之后，掩码第 \(i\) 位对应原始 eos 掩码第 \(i + (ngram\_len-1)\) 位，也就是 ngram \(i\) 的**最后一个 token** 的位置。这意味着：**只要某个 ngram 的末尾 token 已经到达或越过第一个 EOS，这颗 g 值就被丢弃**。换句话说，从「以 EOS 结尾的那个 ngram」起，连同其后所有 g 值，统统不计入——这正好排除了「沾到 EOS 边界」的灰色地带，比单纯按 token 截断更稳妥。

三个长度的关系一览（\(N\) 为序列长度，\(H = ngram\_len - 1\)）：

| 量 | 长度 |
| --- | --- |
| 输入 `outputs` | \(N\) |
| `compute_eos_token_mask` 原始输出 | \(N\) |
| `compute_eos_token_mask[:, ngram_len-1:]` 切片后 | \(N - H = L\) |
| `compute_context_repetition_mask` 输出 | \(N - H = L\) |
| `compute_g_values` 输出的序列维 | \(N - H = L\) |
| `combined_mask` | \(N - H = L\) |

#### 4.3.3 源码精读

README 的「Detecting a watermark」一节给出了完整范式：[README.md:237-256](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/README.md#L237-L256) —— 三步构造掩码、再算 g 值、最后打分。

关键片段：

```python
# eos 掩码：先算全长，再砍掉前 ngram_len-1 个位置，对齐到长度 L
eos_token_mask = logits_processor.compute_eos_token_mask(
    input_ids=outputs, eos_token_id=tokenizer.eos_token_id,
)[:, CONFIG['ngram_len'] - 1 :]                      # (batch, L)

# 重复上下文掩码：本身就是长度 L
context_repetition_mask = logits_processor.compute_context_repetition_mask(
    input_ids=outputs)                                # (batch, L)

# 两个掩码相乘 = 逻辑与
combined_mask = context_repetition_mask * eos_token_mask   # (batch, L)

# g 值：序列维也是 L
g_values = logits_processor.compute_g_values(input_ids=outputs)  # (batch, L, depth)

# 打分：掩码决定哪些 g 值参与
detector.score(g_values.cpu().numpy(), combined_mask.cpu().numpy())
```

注意源码里这行注释明确点出了「跳过前 ngram_len-1 个 token」的对齐意图：

> `# Compute the end-of-sequence mask, skipping first ngram_len - 1 tokens`

对照 g 值形状，可看 `compute_g_values` 的返回说明：[src/synthid_text/logits_processing.py:458-473](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L458-L473) —— 文档写明返回 `(batch_size, input_len - (ngram_len - 1), depth)`，正是 \(L\)。

#### 4.3.4 代码实践

**实践目标**：对一条同时含「重复上下文」和「EOS」的小序列，分别算出两个掩码，再写出 `combined_mask`，解释每一位为什么是 0 或 1。

**操作步骤**（综合实践型，详见第 5 节的完整推导；此处给出可运行验证代码）：

```python
import torch
from synthid_text import logits_processing

ngram_len = 3
processor = logits_processing.SynthIDLogitsProcessor(
    ngram_len=ngram_len, keys=[1, 2, 3],
    context_history_size=10, temperature=0.5,
    top_k=40, device=torch.device("cpu"),
)

outputs = torch.LongTensor([[5, 3, 7, 3, 7, 9, 1]])   # N=7, EOS=9 在位置 5
eos_token_id = 9

eos_full = processor.compute_eos_token_mask(outputs, eos_token_id)  # (1, 7)
eos_token_mask = eos_full[:, ngram_len - 1:]                         # (1, 5)
context_repetition_mask = processor.compute_context_repetition_mask(outputs)  # (1, 5)
combined_mask = context_repetition_mask * eos_token_mask            # (1, 5)
g_values = processor.compute_g_values(outputs)                      # (1, 5, 3)

print("eos_full            :", eos_full)
print("eos_token_mask      :", eos_token_mask)
print("repetition_mask     :", context_repetition_mask)
print("combined_mask       :", combined_mask)
print("g_values.shape      :", g_values.shape)
```

**预期结果**：

```
eos_full            : tensor([[1, 1, 1, 1, 1, 0, 0]])
eos_token_mask      : tensor([[1, 1, 1, 0, 0]])
repetition_mask     : tensor([[1, 1, 1, 0, 1]])
combined_mask       : tensor([[1, 1, 1, 0, 0]])
g_values.shape      : torch.Size([1, 5, 3])
```

每一位的解释见第 5 节的综合实践表格。**待本地验证**：请实际运行确认这些 0/1 与下文表格一致。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `[:, CONFIG['ngram_len'] - 1 :]` 这段切片漏掉，直接让全长 eos 掩码和重复掩码相乘，会发生什么？
**答案**：形状不匹配（\(N\) vs \(L\)）会直接报错；即便强行广播，也会让掩码和 g 值错位，导致错误的位置被计入/排除，打分完全失去意义。这个切片是对齐的关键，不可省。

**练习 2**：`combined_mask` 的序列维长度为什么必须等于 g 值的序列维？
**答案**：因为打分函数（如 `mean_score`、`BayesianDetector.score`）是**逐 g 值**聚合的，掩码逐位决定「这颗 g 值算不算」。两者长度必须一致才能一一对应，而它们共同的长度正是 \(L = N - (ngram\_len-1)\)。

---

## 5. 综合实践

下面把 4.3 的例子完整推一遍。设定：

- `ngram_len = 3`，故上下文长度 \(H = 2\)，每个 ngram 由「2 个上下文 token + 1 个候选 token」组成。
- `outputs = [5, 3, 7, 3, 7, 9, 1]`，长度 \(N = 7\)，`eos_token_id = 9`（出现在位置 5）。
- g 值序列维长度 \(L = N - H = 7 - 2 = 5\)。

**第一步：列出每个 g 值对应的 ngram 与上下文。**

| i | ngram（token[i:i+3]） | 上下文（前 2 个） | 候选（末 token） |
| --- | --- | --- | --- |
| 0 | [5, 3, 7] | [5, 3] | 7 |
| 1 | [3, 7, 3] | [3, 7] | 3 |
| 2 | [7, 3, 7] | [7, 3] | 7 |
| 3 | [3, 7, 9] | [3, 7] | 9 |
| 4 | [7, 9, 1] | [7, 9] | 1 |

**第二步：算 `eos_token_mask`。**

原始全长掩码：EOS 在位置 5，故 `[1,1,1,1,1, 0,0]`（前 5 个为 1，从位置 5 起为 0）。
砍掉前 `ngram_len-1=2` 位：`[1,1,1,1,1,0,0][2:] = [1,1,1,0,0]`。

**第三步：算 `context_repetition_mask`。**

逐个看上下文是否之前出现过：`[3,7]` 在 i=1 出现过，i=3 再次出现 → 重复。其余都新。
结果：`[1, 1, 1, 0, 1]`。

**第四步：相乘得到 `combined_mask`。**

`[1,1,1,0,1] * [1,1,1,0,0] = [1,1,1,0,0]`。

**第五步：逐位解释。**

| i | eos 掩码 | 重复掩码 | combined | 含义 |
| --- | --- | --- | --- | --- |
| 0 | 1 | 1 | **1** | EOS 未到、上下文 [5,3] 新 → 计入 |
| 1 | 1 | 1 | **1** | EOS 未到、上下文 [3,7] 新 → 计入 |
| 2 | 1 | 1 | **1** | EOS 未到、上下文 [7,3] 新 → 计入 |
| 3 | 0 | 0 | **0** | ngram 末 token 正是 EOS（位置5），且上下文 [3,7] 重复 → 双重丢弃 |
| 4 | 0 | 1 | **0** | ngram 跨越了 EOS（含填充 token 1）→ 丢弃 |

**结论**：5 颗 g 值里只有前 3 颗（i=0,1,2）真正参与打分。i=3 因「重复 + 沾 EOS」被丢，i=4 因「越过 EOS」被丢。这正体现了两个掩码的分工：`eos_token_mask` 砍掉结尾的填充噪声，`context_repetition_mask` 砍掉中段未承载水印的重复 g 值，二者相乘只留下「干净且有水印信号」的位置。

**延伸思考**：如果这条序列完全没有 EOS（自然结束、无填充），eos 掩码会全是 1，此时 `combined_mask` 完全由重复掩码决定；反过来若序列充满重复上下文但无 EOS，则由重复掩码主导。两个掩码各管一类「坏 g 值」，互不替代。

## 6. 本讲小结

- 检测时并非每个 g 值都该计入分数；两类「坏 g 值」必须屏蔽：EOS 之后的填充噪声、重复上下文对应的未水印 g 值。
- `compute_eos_token_mask` 从**第一个** EOS 起（含）把后续位置置 0，返回的是**全长 \(N\)** 掩码。
- `compute_context_repetition_mask` 在检测侧**复刻**生成侧的上下文去重逻辑（全新 state、同 `hash_iv`、同滑动窗口），返回长度已是 \(L\)，并经 `logical_not` 把语义统一为「1=保留、0=丢弃」。
- `combined_mask = context_repetition_mask * eos_token_mask`；相乘前必须把 eos 掩码 `[:, ngram_len-1:]` 切片对齐到长度 \(L\)，使三者（两个掩码 + g 值）序列维严格一致。
- 最终 `combined_mask` 连同 `g_values` 一起喂给 `detector.score`，由打分函数在掩码允许的位置上聚合 g 值得到 [0,1] 分数。

## 7. 下一步学习建议

本讲把「检测要用哪些 g 值」讲清楚了，但还没讲「具体怎么把保留下来的 g 值变成一个分数」。建议进入下一讲 **u5-l2 Mean 与 Weighted Mean 打分**，学习 `detector_mean.py` 里 `mean_score` 与 `weighted_mean_score` 两个免训练打分公式，理解默认权重为何从 10 线性递减到 1，以及阈值如何随 token 长度与假阳率确定。

若你对「掩码如何参与更复杂的打分」感兴趣，也可以在读完 u5-l2 后直接跳到单元六的 **u6-l1 贝叶斯检测原理**，看 `BayesianDetector` 如何把 `combined_mask` 用进似然与后验的计算里。无论走哪条路，本讲的 `combined_mask` 都是它们共同的输入前提。
