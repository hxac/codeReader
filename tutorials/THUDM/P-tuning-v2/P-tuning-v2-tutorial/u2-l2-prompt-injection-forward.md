# 前缀注入主流程：get_prompt 与 forward

## 1. 本讲目标

上一讲 [u2-l1 PrefixEncoder](u2-l1-prefix-encoder.md) 解决了「前缀怎么造出来」——它把固定索引 `[0..P-1]` 编码成形状为 `(batch, pre_seq_len, 2*L*H)` 的 `past_key_values`。本讲要回答紧接着的那个问题：**这坨 `past_key_values` 怎么真正进入被冻结的 BERT，并在每一层都生效？**

读完本讲，你应当能够：

- 逐行解释 `get_prompt` 里 `view → permute([2, 0, 3, 1, 4]) → split(2)` 这套张量重排，并说清每一步 tensor 的 5 个维度分别是什么。
- 说出 `permute + split(2)` 之后得到的为什么恰好是「每一层一对 (key, value)」，且形状刚好对上 Hugging Face Transformer 期望的 KV 缓存格式。
- 解释 `attention_mask` 为什么必须在前面拼一段长度为 `pre_seq_len` 的 1，以及不拼会怎样。
- 在源码层面确认「主干 BERT 全部 `requires_grad=False`，只有 PrefixEncoder + 分类头可训练」这一事实。

本讲只聚焦「注入」这一个动作，以 `BertPrefixForSequenceClassification` 为例。Prefix（深层 KV 注入）与 Prompt（浅层嵌入拼接）、全量微调三者对照是下一讲 [u2-l3 三种调优模式对比](u2-l3-prefix-vs-prompt-vs-finetune.md) 的内容。

## 2. 前置知识

本讲承接 u2-l1 已经建立的认知，复用其中三个关键事实，不再重新推导：

1. PrefixEncoder 输出形状恒为 `(batch, pre_seq_len, 2*L*H)`，其中 `2*L*H = 2 × num_hidden_layers × hidden_size`。
2. 维度拆解依赖恒等式 \(2L \times n \times (H/n) = 2LH\)（\(n\) 为头数、\(H/n\) 为每头维度），这正是 `view` 能无损重排的原因。
3. 「深度注入」指在**每一层**都注入前缀 key/value，区别于只在输入层加提示的浅层 prompt tuning。

进入源码前，再补一个本讲要反复用到的 Hugging Face 背景：

| 概念 | 含义 |
|------|------|
| `past_key_values` | HF Transformer 内部每层注意力使用的「历史 key/value」缓存。原意是**推理加速**（解码时不必重算前面 token 的 KV），P-tuning v2 **借用这个现成接口**，把前缀伪装成「已经算好的 KV」喂进每一层。 |
| KV 缓存的逐层格式 | `past_key_values` 是一个长度为 `num_hidden_layers` 的 **tuple**，第 `i` 个元素是第 `i` 层的 `(past_key, past_value)`，每个形状为 `(batch, num_heads, seq_len, head_dim)`。本讲 `get_prompt` 的全部重排，就是为了让输出**精确匹配**这个格式。 |

> 依赖说明：本项目锁定 `transformers==4.11.3`。该版本的 `BertModel.forward` 提供 `past_key_values` 参数（KV 缓存机制）。代码里 `self.bert(..., past_key_values=past_key_values)` 正是靠这个现成接口完成注入的——如果换个不支持该参数的版本，这段代码会直接报错。

## 3. 本讲源码地图

本讲精读一个文件、引用一个文件来定位分工：

| 文件 | 作用 | 本讲用到的部分 |
|------|------|----------------|
| `model/sequence_classification.py` | **本讲主角**，`BertPrefixForSequenceClassification` 的 `__init__`/`get_prompt`/`forward`。 | 第 101–214 行（冻结、重排、注入全流程） |
| `model/prefix_encoder.py` | 上游零件，产出 `(batch, pre_seq_len, 2*L*H)`。 | `forward`（仅回顾输出形状） |
| `model/sequence_classification.py`（Prompt 类） | 对照组，`BertPromptForSequenceClassification` 用浅层拼接，不经 KV 重排。 | 第 235、237–264 行（用于说明差异） |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块，对应 `forward` 里依次发生的三个动作：① `get_prompt` 把前缀重排成「每层一对 KV」；② `attention_mask` 拼上前缀长度；③ 把 `past_key_values` 传给**冻结的** BERT。

### 4.1 get_prompt 的张量重排：view → permute → split

#### 4.1.1 概念说明

u2-l1 留了一个「待确认」的问题：编码器吐出的是一整条 `(batch, pre_seq_len, 2*L*H)` 的「扁平」向量，而 HF 每层注意力要的是 `(batch, num_heads, seq_len, head_dim)` 形状的 key 和 value，而且要**逐层分开**（共 L 层、每层一对）。这两者之间差着一套形状变换，正是 `get_prompt` 要做的事。

可以把 `get_prompt` 理解成一个「翻译器」：它把 PrefixEncoder 那条扁平向量，重新解读、重排、切片，最终输出一个长度为 L 的 tuple，每个元素恰好是某层的 `(key, value)`。整个过程**不改任何数值，只改形状和排布**。

#### 4.1.2 核心流程

设 \(P\) 为 `pre_seq_len`、\(L\) 为层数、\(n\) 为头数、\(d=H/n\) 为每头维度。`get_prompt` 的三步重排：

```text
PrefixEncoder 输出
    (batch, P, 2*L*H)
        │  view  ── 把最后一维 2*L*H 拆成 (2L, n, d)
        ▼
    (batch, P, 2L, n, d)          # 五维: [batch, 前缀长度, 层×2, 头数, 头维]
        │  dropout ── 训练时随机置零（形状不变）
        ▼
    (batch, P, 2L, n, d)
        │  permute([2,0,3,1,4]) ── 把「层×2」轴提到最前
        ▼
    (2L, batch, n, P, d)          # 五维: [层×2, batch, 头数, 前缀长度, 头维]
        │  split(2) ── 沿第 0 维按大小 2 切
        ▼
    tuple of L，每个 (2, batch, n, P, d)   # 第 i 个 = 第 i 层的 (key_i, value_i)
```

`view` 之所以合法，靠的就是 u2-l1 的恒等式：

\[
\underbrace{(2L)}_{\text{n\_layer}\times2} \times \underbrace{n}_{\text{n\_head}} \times \underbrace{d}_{H/n} \;=\; 2LH
\]

总元素数不变，只是把一条 `2*L*H` 向量重新解读成「层 × 头 × 头维」的结构。

而 `permute + split(2)` 的目的，是让最终每个元素都变成 `(batch, num_heads, seq_len, head_dim)`——这正是 HF KV 缓存的逐元素形状。下面用源码逐行确认。

#### 4.1.3 源码精读

先看 `__init__` 里与重排有关的四个「形状参数」，它们决定了后面所有 `view/permute` 的轴：

[形状参数 pre_seq_len/n_layer/n_head/n_embd (model/sequence_classification.py:L113-L116)](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/sequence_classification.py#L113-L116) ——`n_layer = num_hidden_layers`、`n_head = num_attention_heads`、`n_embd = hidden_size // num_attention_heads`。注意 `n_embd` 在 PrefixEncoder 里没用上（u2-l1 已点明），但在这里用来拆头。

接着是 `get_prompt` 全文。下面把**当前 tensor 的 5 个维度含义**逐行注释在源码旁（这是本讲主任务要求的标注）：

[get_prompt 全流程：view → dropout → permute → split (model/sequence_classification.py:L130-L143)](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/sequence_classification.py#L130-L143) ——逐行解读如下：

```python
# 示例标注（在原码每一行旁标出 5 个维度含义）
def get_prompt(self, batch_size):
    prefix_tokens = self.prefix_tokens.unsqueeze(0).expand(batch_size, -1).to(self.bert.device)
    # shape: (batch_size, pre_seq_len)                       # 维度: [batch, 前缀长度]

    past_key_values = self.prefix_encoder(prefix_tokens)
    # shape: (batch_size, pre_seq_len, 2*L*H)                # 维度: [batch, 前缀长度, 扁平KV]

    past_key_values = past_key_values.view(
        batch_size,        # 轴 0: batch
        self.pre_seq_len,  # 轴 1: 前缀长度 P
        self.n_layer * 2,  # 轴 2: 层数×2（每层 key+value 摊开成 2L 个槽）
        self.n_head,       # 轴 3: 头数 n
        self.n_embd,       # 轴 4: 头维 d = H/n
    )
    # shape: (batch, P, 2L, n_head, n_embd)                  # 五维: [batch, P, 层×2, 头数, 头维]

    past_key_values = self.dropout(past_key_values)
    # shape 不变: (batch, P, 2L, n_head, n_embd)             # 训练时随机置零，形状不变

    past_key_values = past_key_values.permute([2, 0, 3, 1, 4]).split(2)
    # 第一步 permute([2,0,3,1,4]):
    #   原轴 [0:batch, 1:P, 2:2L, 3:n_head, 4:n_embd]
    #   → 新轴 [2, 0, 3, 1, 4] = (2L, batch, n_head, P, n_embd)
    #   shape: (2L, batch, n_head, P, n_embd)                # 五维: [层×2, batch, 头数, P, 头维]
    # 第二步 split(2): 沿第 0 维(2L)按大小 2 切 → 共 L 段
    #   每段 shape: (2, batch, n_head, P, n_embd)            # 第 i 段 = 第 i 层的 (key_i, value_i)
    return past_key_values
    # 类型: tuple，长度 L；past_key_values[i][0]=key_i，[1]=value_i
```

**为什么 `split(2)` 恰好得到每层 (key, value) 对？** 关键在 `permute` 把「层×2」轴（2L）提到了第 0 维，而这条 2L 轴的排布是**按层交错**的：槽位顺序为

\[
\underbrace{(k_0, v_0)}_{\text{第 0 层}},\ \underbrace{(k_1, v_1)}_{\text{第 1 层}},\ \ldots,\ \underbrace{(k_{L-1}, v_{L-1})}_{\text{第 L-1 层}}
\]

`split(2)` 沿这条轴按「每 2 个一段」切，正好把每一对 `(k_i, v_i)` 切在一起，得到 L 段。第 `i` 段形状为 `(2, batch, n_head, P, n_embd)`，它的 `[0]` 和 `[1]` 各是 `(batch, n_head, P, n_embd) = (batch, num_heads, seq_len, head_dim)`——这正是 HF KV 缓存里某一层 `(past_key, past_value)` 的逐元素形状。于是 `past_key_values[i]` 就能被 BERT 第 `i` 层直接当成「已经算好的 key/value」使用。

> 一点直觉：因为前缀是**从零学出来的**参数，所以「哪个槽是 key、哪个是 value」并不需要语义上预先指定——模型在训练中自己学会往每个槽里放合适的值。代码要保证的只是**形状契约**：L 段、每段 2 个、每个 `(batch, num_heads, seq_len, head_dim)`。

#### 4.1.4 代码实践（本讲主任务）

**任务**：对照上面的源码，回答「`permute([2,0,3,1,4]).split(2)` 之后得到的结构为什么恰好是每层 (key, value) 对」，并完成 5 维度标注。

1. **实践目标**：把 `get_prompt` 的三步重排在脑中跑一遍，确认最终结构是「长度 L 的 tuple，每元素一对 KV」。
2. **操作步骤**：
   - 在 `model/sequence_classification.py` 第 130–143 行的 `get_prompt` 上方/旁边，照 4.1.3 的样子为每一行标注当前 tensor 的 5 个维度含义。
   - 用一句话写出 `permute` 把哪条轴提到了第 0 维、`split(2)` 沿哪条轴切、切成几段。
3. **需要观察的现象**：标注完成后，应能看出 `permute` 后第 0 维是 `2L`、其余四维 `(batch, n_head, P, n_embd)` 恰好拼出 HF 期望的单个 KV 形状。
4. **预期结果**：`split(2)` 把 `2L` 切成 `L` 段，每段 `(2, batch, n_head, P, n_embd)`，即每层一对 `(key, value)`。类型是 **tuple**（`torch.split` 返回 tuple，不是 list）。
5. **进阶验证（可选，待本地验证）**：用 dummy config 真跑一遍重排，打印每步 shape。下面这段**示例代码**（非项目原有代码）只依赖 `torch`，可在仓库根目录运行：

```python
# 示例代码：仅验证 get_prompt 的形状重排（不加载预训练权重）
from types import SimpleNamespace
import torch

# 用 RoBERTa-large 的形状作例子：P=128, L=24, n_head=16, H=1024, d=64
P, L, n_head, d = 128, 24, 16, 64
batch = 2
H = n_head * d                                   # 1024

x = torch.randn(batch, P, 2 * L * H)             # 模拟 PrefixEncoder 输出
print("encoder out :", tuple(x.shape))           # (2, 128, 49152)

v = x.view(batch, P, L * 2, n_head, d)
print("after view  :", tuple(v.shape))           # (2, 128, 48, 16, 64)

p = v.permute([2, 0, 3, 1, 4])
print("after permute:", tuple(p.shape))          # (48, 2, 16, 128, 64)

s = p.split(2)
print("split 段数   :", len(s), "（应等于层数 L =", L, ")")
print("每段 shape   :", tuple(s[0].shape))       # (2, 2, 16, 128, 64)
print("layer0 key   :", tuple(s[0][0].shape), " value:", tuple(s[0][1].shape))
# key/value 各为 (batch=2, num_heads=16, seq_len=128, head_dim=64) —— 与 HF KV 缓存格式一致
```

预期输出：encoder out `(2,128,49152)` → view `(2,128,48,16,64)` → permute `(48,2,16,128,64)` → split 成 `24` 段，每段 `(2,2,16,128,64)`，其中 `[0]/[1]` 各为 `(2,16,128,64)`。

#### 4.1.5 小练习与答案

**练习 1**：如果作者把 `permute` 写成 `[2, 0, 3, 1, 4]` 改成 `[0, 2, 3, 1, 4]`（即不把「层×2」轴提到最前），`split(2)` 还能得到「每层一对」吗？
**答**：不能。`split(2)` 默认沿**第 0 维**切。只有把「层×2」轴（2L）放到第 0 维，`split(2)` 才会沿它切成 L 段；否则切的是别的轴，得到的结构就与 HF 的逐层 tuple 对不上。

**练习 2**：`split(2)` 切出的每个元素形状是 `(2, batch, n_head, P, n_embd)`。这里的 `P` 对应 HF KV 缓存形状里的哪一个维度？
**答**：对应 `seq_len`（序列长度）。也就是说，前缀被当成一段长度为 `P` 的「历史序列」加到每层注意力里，这就是「深度注入」的几何含义。

---

### 4.2 prefix_attention_mask：为什么要给前缀拼一段 1

#### 4.2.1 概念说明

Transformer 的 `attention_mask` 是一个 0/1 矩阵：1 表示「这个位置要看」，0 表示「这个位置是 padding，别看」。原本它的长度等于真实输入序列长度。

可是加了前缀之后，BERT 实际「看到」的序列变长了：前面多了 `pre_seq_len` 个前缀位置（它们作为 KV 被拼到每层注意力里）。如果 `attention_mask` 不把这段前缀的位置标成「要看」，注意力就会把前缀当成 padding 屏蔽掉——那前缀就**完全失效**，等价于没加。

所以 `forward` 在调用 BERT 之前，要先在 `attention_mask` 最前面拼一段长度为 `pre_seq_len` 的全 1。

#### 4.2.2 核心流程

```text
原始 attention_mask : (batch, seq_len)        # 1=真实token, 0=padding
        │
        ▼
prefix_attention_mask = torch.ones(batch, pre_seq_len)   # 全 1，表示前缀位置都要看
        │
        ▼  torch.cat((prefix_attention_mask, attention_mask), dim=1)
        │
新 attention_mask : (batch, pre_seq_len + seq_len)       # 前缀在前，真实序列在后
```

注意拼接顺序：前缀的 1 在**前面**，真实序列的 mask 在后面——这和 4.1 里 KV「前缀拼在序列前面」的方向一致（HF 内部是 `cat([past_key, key_layer], dim=seq)`，past 在前）。

#### 4.2.3 源码精读

[mask 拼接 (model/sequence_classification.py:L162-L163)](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/sequence_classification.py#L162-L163) ——第 162 行造一段全 1 的 `prefix_attention_mask`，形状 `(batch, pre_seq_len)`；第 163 行把它 `cat` 到原 `attention_mask` 的**前面**（`dim=1`）。这样 BERT 在做注意力时，前缀这 `pre_seq_len` 个位置不会被当成 padding 屏蔽。

> 对照：浅层 Prompt 模式 `BertPromptForSequenceClassification` 也做了**一模一样**的 mask 拼接（见 [model/sequence_classification.py:L265-L266](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/sequence_classification.py#L265-L266)）。区别只在「前缀怎么进网络」：Prompt 是在**嵌入层**把 prompt 向量拼到 `inputs_embeds` 前面（[L264](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/sequence_classification.py#L264)），而 Prefix 是在**每一层**通过 KV 缓存注入。两者的 mask 都要扩，因为两者都让序列变长了。三种模式的完整对照留到 u2-l3。

#### 4.2.4 代码实践

1. **实践目标**：理解「不拼 mask 前缀就失效」这一后果。
2. **操作步骤**：阅读第 162–163 行后，回答下面这个推理题——
   假设删掉第 162–163 行、直接把原始 `attention_mask`（长度 `seq_len`）和长度为 `pre_seq_len` 的 KV 一起喂给 BERT，会发生什么？
3. **需要观察的现象 / 预期结果**：存在两种隐患。其一，**形状不匹配**：HF 内部 KV 缓存对应的序列长度（`pre_seq_len + seq_len`）与 mask 长度（`seq_len`）对不上，会直接报维度错；其二，即便形状对上，前缀位置没有对应的 1，会被当成 padding 屏蔽，前缀注入形同虚设。所以这步拼接是「必须的」，不是可选的。
4. **进阶验证（可选，待本地验证）**：用下面**示例代码**感受拼接结果：

```python
# 示例代码
import torch
batch, pre_seq_len, seq_len = 2, 4, 6
attention_mask = torch.tensor([[1,1,1,1,0,0],[1,1,1,1,1,0]])   # 真实序列的 mask
prefix_attention_mask = torch.ones(batch, pre_seq_len).long()
new_mask = torch.cat((prefix_attention_mask, attention_mask), dim=1)
print(tuple(new_mask.shape))   # (2, 10) = pre_seq_len(4) + seq_len(6)
print(new_mask)
# 第 0 行: [1 1 1 1 | 1 1 1 1 0 0]  —— 前 4 个是前缀，后面是原序列
```

预期：`new_mask.shape == (2, 10)`，前 4 列全 1（前缀），后 6 列沿用原 mask。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `prefix_attention_mask` 用全 1，而不是复用原序列的 mask？
**答**：前缀是模型自己生成的连续向量，每个位置都「有意义」，不存在 padding，所以全标 1。

**练习 2**：拼接时为什么是 `cat((prefix, original))` 而不是 `cat((original, prefix))`？
**答**：因为 4.1 里 KV 也是「前缀在前、真实序列在后」（HF 内部 `cat([past_key, key_layer])`）。mask 的顺序必须与 KV 的顺序对齐，否则 attention 会把「该看前缀」错配给「真实 token 的位置」。

---

### 4.3 注入冻结的 BERT：forward 主流程与 requires_grad=False

#### 4.3.1 概念说明

前两步把 `past_key_values` 和扩展后的 `attention_mask` 都准备好了。`forward` 的剩余工作就很简单：把它们连同 `input_ids` 一起传给 `self.bert(...)`。BERT 内部每一层会把自己的 key/value 算出来，再和前缀 KV 沿**序列维度**拼接（前缀在前），于是每个 token 在每一层都能「看见」前缀——这就是「深度提示调优」的最终落地。

与此同时，本讲的另一条暗线是**冻结**：`__init__` 里把 BERT 所有参数的 `requires_grad` 置为 `False`。整个 forward 里，BERT 那几亿参数的梯度都不会被计算，反向传播只会更新 PrefixEncoder 和分类头。这就是「参数高效」在代码层面的硬保证。

#### 4.3.2 核心流程

`forward` 主流程（只看与注入相关的部分）：

```text
input_ids : (batch, seq_len)
        │
        ├─► get_prompt(batch)  → past_key_values  # 长度 L 的 tuple，每元素一对 KV
        ├─► 拼接 attention_mask                    # (batch, pre_seq_len + seq_len)
        │
        ▼
self.bert(input_ids, attention_mask=..., past_key_values=past_key_values)
        │   # BERT 每层: cat([prefix_KV, 本层KV], dim=序列) → 每个 token 每层都能看到前缀
        ▼
outputs : (last_hidden_state, pooled_output, ...)
        │
        ▼  pooled = outputs[1] → dropout → classifier
logits : (batch, num_labels) → 算 loss
```

至于「主干冻结」，发生在更早的 `__init__`：一个 `for` 循环把 `self.bert` 全部参数锁住。

#### 4.3.3 源码精读

先看**冻结**——这是「仅 prefix 可训练」的源头：

[冻结 BERT 主干 (model/sequence_classification.py:L110-L111)](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/sequence_classification.py#L110-L111) ——`for param in self.bert.parameters(): param.requires_grad = False`。此后 BERT 的所有权重都不再参与梯度更新。

紧接着是**参数量统计**，它正好印证「可训练参数 ≈ PrefixEncoder + 分类头」：

[参数量统计 total_param = all - bert (model/sequence_classification.py:L121-L128)](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/sequence_classification.py#L121-L128) ——`bert_param` 累加 BERT 的参数量，`all_param` 累加整个模型的参数量，`total_param = all_param - bert_param` 就是「非主干」参数量（主要是 PrefixEncoder，外加分类头）。注释里的 `# 9860105` 是作者在 large 模型 + MLP 投影头配置下打印的参考值（u2-l1 已解释它主要是投影头贡献），具体数字随配置变化。

再看 **forward 的注入主流程**：

[forward 注入主流程 (model/sequence_classification.py:L160-L176)](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/sequence_classification.py#L160-L176) ——第 160 行取 `batch_size`；第 161 行调用 `get_prompt` 得到 `past_key_values`；第 162–163 行拼接 `attention_mask`（4.2）；第 165–176 行调用 `self.bert(...)`，关键就是第 175 行把 `past_key_values=past_key_values` 传进去。BERT 拿到后，在每一层把前缀 KV 拼到自己算出的 KV 前面，从而完成「深度注入」。

注入之后，分类逻辑与普通 BERT 一致：

[池化 → 分类头 → 损失 (model/sequence_classification.py:L178-L181,L183-L204)](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/sequence_classification.py#L178-L204) ——第 178 行取 `outputs[1]`（pooler 输出，对应 `[CLS]`），第 180–181 行经 dropout 和 `classifier` 线性层得到 logits；第 183–204 行按 `problem_type` 选 MSE / CrossEntropy / BCE 算 loss。这部分与不带前缀的 `BertForSequenceClassification`（[L62-L98](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/sequence_classification.py#L62-L98)）完全相同——前缀只改了「输入怎么进 BERT」，分类头逻辑毫无变化。

> 一个值得注意的点：前缀对 batch 内**所有样本都是相同的**。`prefix_tokens` 只是 `[0..P-1]` 这串固定索引，按 batch 复制（[L131](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/sequence_classification.py#L131)），经同一个 PrefixEncoder 查表，所以每个样本拿到的是**同一个前缀模板**（仅在 `dropout` 处引入随机性）。这说明 P-tuning v2 的提示是「全局共享的连续模板」，而非随样本变化的实例化提示。

#### 4.3.4 代码实践

1. **实践目标**：在源码层面确认「主干被冻结、仅 prefix + 分类头可训练」。
2. **操作步骤**：
   - 阅读 `__init__` 第 110–111 行的冻结循环，再读第 121–128 行的参数量统计。
   - 回答：`total_param = all_param - bert_param` 为什么能近似代表「可训练参数量」？（提示：结合第 110–111 行。）
3. **需要观察的现象 / 预期结果**：因为第 110–111 行已把 BERT 全部参数设为 `requires_grad=False`，所以「可训练参数」≈「非 BERT 参数」≈ `total_param`（严格说还要排除 dropout 这类无参数模块，但数量级上成立）。这就解释了为什么带 `--prefix` 时打印出的 `total param` 远小于 BERT 主干（u1-l2 提到带 `--prefix` 时主干被冻结、需自行统计 `requires_grad` 参数量）。
4. **进阶验证（可选，待本地验证）**：若本地有 torch，可实例化一个随机初始化的小模型验证冻结生效（**示例代码**，不加载预训练权重）：

```python
# 示例代码
from transformers import BertConfig
from model.sequence_classification import BertPrefixForSequenceClassification

cfg = BertConfig(hidden_size=64, num_hidden_layers=2, num_attention_heads=2,
                 intermediate_size=128, num_labels=3, vocab_size=100)
cfg.pre_seq_len = 4
cfg.prefix_projection = False
model = BertPrefixForSequenceClassification(cfg)

trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
bert_total = sum(p.numel() for p in model.bert.parameters())
print("可训练参数（含分类头）:", trainable)
print("BERT 主干参数      :", bert_total)
# 预期：可训练参数远小于主干；且 model.bert 的每个 param.requires_grad 都是 False
print("BERT 是否全部冻结  :", all(not p.requires_grad for p in model.bert.parameters()))  # True
```

预期：`BERT 是否全部冻结` 打印 `True`；可训练参数仅为 PrefixEncoder 查表（`pre_seq_len × 2 × L × H`）加分类头，远小于主干。

#### 4.3.5 小练习与答案

**练习 1**：既然 BERT 被冻结，前向传播时 BERT 内部的计算还需要跑吗？为什么？
**答**：需要。`requires_grad=False` 只影响**反向传播**（不计算也不更新这些参数的梯度），不影响前向计算。BERT 仍要正常前向，把前缀 KV 和自己的 KV 拼起来算注意力，否则拿不到 logits。

**练习 2**：前缀是「每个样本各不相同」还是「全 batch 共享」？从源码哪一行能看出来？
**答**：全 batch 共享。第 131 行 `self.prefix_tokens.unsqueeze(0).expand(batch_size, -1)` 只是把同一串索引 `[0..P-1]` 复制到 batch 维，再经同一个 PrefixEncoder 查表，所以每个样本的前缀是同一个模板（dropout 除外）。

**练习 3**：分类头 `self.classifier` 的参数被冻结了吗？
**答**：没有。第 110–111 行的循环只遍历 `self.bert.parameters()`，分类头 `self.classifier`（[L108](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/sequence_classification.py#L108)）不在其中，仍可训练。所以可训练部分 = PrefixEncoder + 分类头（+ dropout 无参数）。

## 5. 综合实践

把本讲三个模块串起来：**在脑中（或用代码）完整跑一遍一次前向，标注从 `input_ids` 到 `logits` 每一步的张量形状变化，并指出哪一步发生「注入」、哪一步发生「冻结」。**

任务要求：

1. 给定 RoBERTa-large 配置：`batch=2`、`seq_len=32`、`pre_seq_len=128`、`num_hidden_layers=24`、`num_attention_heads=16`、`hidden_size=1024`、`num_labels=2`。
2. 按下表填写每一步的 shape（答案见下）：

   | 步骤 | 位置 | shape |
   |------|------|-------|
   | PrefixEncoder 输出 | `get_prompt` L132 | ? |
   | `view` 之后 | L134–140 | ? |
   | `permute` 之后 | L142 | ? |
   | `split(2)` 每段 | L142 | ? |
   | 扩展后 `attention_mask` | L163 | ? |
   | BERT 输出 `outputs[1]`（pooled） | L178 | ? |
   | `logits` | L181 | ? |

3. 指出：「注入」发生在哪一行？「冻结」发生在哪一行？
4. 预期答案：
   - PrefixEncoder 输出：`(2, 128, 2*24*1024) = (2, 128, 49152)`
   - `view` 后：`(2, 128, 48, 16, 64)`
   - `permute` 后：`(48, 2, 16, 128, 64)`
   - `split(2)` 每段：`(2, 2, 16, 128, 64)`（共 24 段）
   - 扩展后 mask：`(2, 128+32) = (2, 160)`
   - pooled：`(2, 1024)`
   - logits：`(2, 2)`
   - 注入：第 175 行 `self.bert(..., past_key_values=past_key_values)`；冻结：第 110–111 行的 `requires_grad=False` 循环。

> 待本地验证：若想实跑，可用 4.1.4 的 dummy 重排代码 + 4.3.4 的小模型实例化代码组合验证形状；本任务核心是「能在纸上推出每一步 shape」。

## 6. 本讲小结

- `get_prompt` 用 `view(batch, P, 2L, n_head, n_embd)` 把扁平的 `2*L*H` 向量重排成「层 × 头 × 头维」结构，靠恒等式 \(2L \times n \times (H/n) = 2LH\) 保证元素数不变。
- `permute([2,0,3,1,4])` 把「层×2」轴提到第 0 维，使每个元素变成 HF 期望的 `(batch, num_heads, seq_len, head_dim)`；`split(2)` 沿这条轴按 2 切，得到长度为 L 的 **tuple**，每段恰好是一层的 `(key, value)`。
- `attention_mask` 必须在前面拼一段 `pre_seq_len` 个 1：前缀作为 KV 被加进每层注意力、让序列变长了，不拼就会形状不匹配或前缀被当 padding 屏蔽而失效。
- 注入发生在 forward 的 `self.bert(..., past_key_values=past_key_values)`：BERT 每层把前缀 KV 拼在自己 KV 前面，于是每个 token 在每一层都能看见前缀——这正是「深度」二字的落地。
- 冻结发生在 `__init__` 的 `for param in self.bert.parameters(): param.requires_grad=False`；可训练部分只有 PrefixEncoder + 分类头，`total_param = all_param - bert_param` 近似刻画了它。
- 前缀是全 batch 共享的同一个连续模板（第 131 行 `expand` 只是复制索引），不是随样本变化的提示。

## 7. 下一步学习建议

你已经看清「前缀如何造、如何重排、如何注入冻结主干」。接下来值得做两件事：

1. 读 [u2-l3 三种调优模式对比](u2-l3-prefix-vs-prompt-vs-finetune.md)，把本讲的 **Prefix（深层 KV 注入）** 与 **Prompt（浅层嵌入拼接，不经 KV 重排）**、**全量微调（不冻结主干）** 三者横向对照，建立「同一任务、三种注入位置」的完整图景。
2. 回到本讲 4.1.4 的 dummy 重排代码，把 `P` 改成 128、`L` 改成 24，亲手跑一遍 `view → permute → split`，确认你能不看答案写出每一步 shape——这是检验你是否真正掌握「深度注入」几何含义的标尺。

再往后的 u3 会进入「配置体系与模型工厂」，讲清 `--prefix` 开关如何驱动 `get_model` 选到本讲的 `BertPrefixForSequenceClassification`，把命令行到模型类的整条链路补全。
