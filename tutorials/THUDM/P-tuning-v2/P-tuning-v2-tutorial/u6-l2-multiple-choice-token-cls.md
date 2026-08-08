# 多选题与 Token 分类模型变体

## 1. 本讲目标

本讲承接 u3-l2「模型工厂 get_model 与任务注册表」，把视角从「路由」下沉到注册表里两个尚未展开的任务模型：**多选题（Multiple Choice）** 与 **Token 分类（Token Classification）**。读完本讲，你应当能够：

- 看懂多选题模型如何把 `(batch, num_choices, seq)` 三维输入「折叠」成二维，再借助 `classifier(hidden, 1)` 把每个选项打出一个分数。
- 理解折叠之后，prefix 注入逻辑为何能**逐字复用**单句分类的实现思路（关键在 `batch_size * num_choices`）。
- 掌握 Token 分类模型输出「每个 token 一个 logits」时，如何用 `active_loss` 掩码配合 `-100` 屏蔽 padding，以及为何注入前缀后必须把 `attention_mask` 的前缀段切掉。
- 认清一个贯穿全讲的结论：`get_prompt` 是一份被三种任务模型**原样复制**的代码，体现深度提示调优的「任务无关性」。

> 阅读提示：本讲默认你已经学过 u2-l1（PrefixEncoder）、u2-l2（get_prompt 与 forward）和 u3-l2（注册表路由）。`get_prompt` 的张量重排原理本讲只做简要回顾，不再逐步推导。

## 2. 前置知识

- **多选题任务（Multiple Choice）**：每个样本给一个题干和 \(K\) 个候选答案，模型要从 \(K\) 个里选出正确的那一个。典型数据集是 SuperGLUE 的 COPA、WSC、MultiRC。它和二分类的本质区别是：**一次前向要同时处理 \(K\) 个序列**，再比较它们的得分。
- **Token 分类任务（Token Classification）**：序列标注，对输入的**每一个 token** 都预测一个标签。典型任务是命名实体识别（NER）和语义角色标注（SRL）。这与 u4-l1 的「整句一个标签」的分类完全不同——分类只输出 `(batch, num_labels)`，Token 分类输出 `(batch, seq_len, num_labels)`。
- **`active_loss` 掩码**：因为一个 batch 里不同样本长度不一，短样本会被 padding 补齐。padding 位置不该参与损失计算，于是用一个 0/1 掩码把 padding 处的标签替换成 `-100`（PyTorch `CrossEntropyLoss` 的 `ignore_index`），让这些位置对 loss 没有贡献。这与 u4-l2 讲过的「子词标签对齐用 `-100`」是同一个约定，只是这里是在**模型侧**用 attention_mask 再屏蔽一次 padding。
- **回顾 `get_prompt`**：u2-l2 讲过，`PrefixEncoder` 输出扁平的 `(batch, pre_seq_len, 2*L*H)`，经 `view → permute([2,0,3,1,4]) → split(2)` 重排成 HuggingFace `past_key_values` 的逐层 `(key, value)` 格式，注入被冻结的主干。本讲只关心「它怎么被不同任务复用」。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [model/multiple_choice.py](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/multiple_choice.py) | 多选题模型家族：含 vanilla `BertForMultipleChoice`、三个 `*PrefixForMultipleChoice`（bert/roberta/deberta）、两个 `*PromptForMultipleChoice`（浅层提示）。 |
| [model/token_classification.py](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/token_classification.py) | Token 分类模型家族：vanilla `BertForTokenClassification` 与四个 `*PrefixForTokenClassification`（bert/roberta/deberta/deberta-v2）。 |
| [model/sequence_classification.py](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/sequence_classification.py) | u2-l2 精读过的参考实现，本讲用作「单句分类」的对照基线。 |
| [model/utils.py](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/utils.py) | `TaskType` 枚举与 `PREFIX_MODELS` 二维注册表，说明本讲两类模型如何被路由进来。 |

---

## 4. 核心概念与源码讲解

### 4.1 多选题模型：reshape 折叠与 classifier(hidden,1)

#### 4.1.1 概念说明

多选题要同时给 \(K\) 个候选答案打分。最朴素的做法是：**把 \(K\) 个序列当成 \(K\) 条独立样本**，让主干一次性算完，再在每个候选上跑一个打分头，最后把 \(K\) 个分数凑回一组做 softmax。

P-tuning v2 的实现正是这个思路，分三步：

1. **折叠（reshape）**：把输入从 `(batch, num_choices, seq)` 变成 `(batch*num_choices, seq)`。这样主干收到的就是普通的「二维 batch」，一切注意力计算照旧。
2. **打分**：用一个输出维度为 1 的线性层 `classifier(hidden, 1)`，把每个候选的 pooled 向量压成**一个标量分数**。
3. **还原（reshape）**：把 `(batch*num_choices, 1)` 变回 `(batch, num_choices)`，再用交叉熵在 \(K\) 个分数上选最优。

关键直觉：**多选不是「多分类」，而是「对每个候选拟合一个分数再比较」**。所以 `classifier` 的输出维度是 `1` 而不是 `num_labels`——这点和单句分类的 `Linear(hidden, num_labels)` 形成本讲最重要的对照。

#### 4.1.2 核心流程

```text
输入  input_ids:   (batch, num_choices, seq)
                  │
                  ▼  reshape(-1, seq)   折叠成二维
        (batch*num_choices, seq)
                  │
                  ▼  注入 prefix（batch 维 = batch*num_choices）
        冻结主干前向 → pooled_output: (batch*num_choices, hidden)
                  │
                  ▼  classifier = Linear(hidden, 1)
              logits: (batch*num_choices, 1)
                  │
                  ▼  reshape(-1, num_choices)   还原成组
      reshaped_logits: (batch, num_choices)
                  │
                  ▼  CrossEntropyLoss(logits, labels)   labels: (batch,)
                loss
```

注意中间那一步「注入 prefix」：因为折叠之后真正的「batch 大小」变成了 `batch*num_choices`，所以给 `get_prompt` 传的 `batch_size` 也必须是 `batch*num_choices`，让每一个「样本-候选」对都各自拥有一份前缀。这是 4.1.3 与 4.3 的交汇点。

#### 4.1.3 源码精读

**先看 vanilla 版（不带 prefix）的折叠与打分**，它把套路讲得最干净：

[model/multiple_choice.py:L62-L68](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/multiple_choice.py#L62-L68) —— 构造函数里 `classifier` 的输出维度是 **1**（区别于单句分类的 `num_labels`）：

```python
self.classifier = torch.nn.Linear(config.hidden_size, 1)
```

[model/multiple_choice.py:L84-L94](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/multiple_choice.py#L84-L94) —— forward 入口的折叠：先记录 `num_choices`，再把四个三维张量都 `reshape(-1, seq)` 拍平成二维。

[model/multiple_choice.py:L111-L117](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/multiple_choice.py#L111-L117) —— 打分与还原：每个候选得一个标量分数，再 `reshape(-1, num_choices)` 凑回 `(batch, num_choices)`，交给 `CrossEntropyLoss` 在 \(K\) 个候选间做选择。

**再看 Prefix 版**，它在折叠之后插入了一段 prefix 注入：

[model/multiple_choice.py:L189-L201](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/multiple_choice.py#L189-L201) —— 折叠（L189-L197）之后，**关键两行**：

```python
past_key_values = self.get_prompt(batch_size=batch_size * num_choices)   # L199
prefix_attention_mask = torch.ones(batch_size * num_choices, self.pre_seq_len).to(self.bert.device)  # L200
attention_mask = torch.cat((prefix_attention_mask, attention_mask), dim=1)  # L201
```

这里 `get_prompt` 收到的 `batch_size` 是 `batch_size * num_choices`。也就是说，主干眼中根本不存在「多选」这件事——它只看到一个大了一号的 batch（`batch*num_choices` 条序列），每条序列前面都拼了一段前缀。这就是「折叠后能直接复用单句分类注入逻辑」的根因。

[model/multiple_choice.py:L203-L220](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/multiple_choice.py#L203-L220) —— 把 `past_key_values` 喂进被冻结的 `self.bert`，取 `outputs[1]`（pooled），打分、还原，与 vanilla 版完全一致。可见 prefix 只是在「主干前向」这一步多了参数，其余打分/损失逻辑原样不动。

> RoBERTa 版的写法略不同，它把折叠后的张量命名为 `flat_*`（如 [L307-L319](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/multiple_choice.py#L307-L319)），但 `self.get_prompt(batch_size=batch_size * num_choices)` 这一行的语义与 BERT 版逐字相同。

#### 4.1.4 代码实践

**实践目标**：亲手验证多选题的「折叠 → 打分 → 还原」三步，并理解为何折叠后 prefix 注入能直接复用单句分类。

**操作步骤**（这是一段**示例代码**，可直接在装好 torch 的 Python 里运行，无需下载预训练权重）：

```python
import torch

# 模拟多选题打分头的形状变化（不加载真实模型，只演示张量流）
batch, num_choices, hidden = 2, 4, 768
classifier = torch.nn.Linear(hidden, 1)           # 输出维度 = 1

# 假设主干已经为每个 (样本, 候选) 算好了 pooled 向量
pooled = torch.randn(batch * num_choices, hidden) # (8, 768)
print("pooled:", pooled.shape)

logits = classifier(pooled)                       # (8, 1) —— 每个候选一个分数
print("logits:", logits.shape)

reshaped_logits = logits.reshape(-1, num_choices) # (2, 4) —— 凑回每组
print("reshaped_logits:", reshaped_logits.shape)

labels = torch.tensor([0, 3])                     # 每题的正确选项
loss = torch.nn.CrossEntropyLoss()(reshaped_logits, labels)
print("loss:", loss.item())
```

**需要观察的现象**：

1. `pooled` 是 `(8, 768)` 而不是 `(2, 768)`——折叠已经发生。
2. `classifier` 输出 `(8, 1)`，第二维是 **1** 而不是 `num_choices`——分数是逐候选打的。
3. `reshape(-1, num_choices)` 后变回 `(2, 4)`，正好让 `CrossEntropyLoss` 在每题的 4 个候选里挑一个。

**预期结果**：三行 shape 打印依次为 `torch.Size([8, 768])`、`torch.Size([8, 1])`、`torch.Size([2, 4])`，并能算出一个标量 loss（数值随机，不影响理解）。

**回答实践核心问题**：折叠后，prefix 注入逻辑（`get_prompt` + 拼接 `prefix_attention_mask` + 喂 `past_key_values`）只认输入张量的**第一维**作为 batch 大小。折叠已经把 `(batch, num_choices, seq)` 变成了 `(batch*num_choices, seq)`，所以只要把传给 `get_prompt` 的 `batch_size` 写成 `batch_size * num_choices`，整个注入流程就和单句分类**毫无差别**——主干根本不知道「这其实是一组多选题」。

#### 4.1.5 小练习与答案

**练习 1**：如果把第 199 行的 `batch_size * num_choices` 误写成 `batch_size`，会发生什么形状错误？

**答案**：`get_prompt` 会按 `batch_size` 生成前缀，得到的 `past_key_values` 每层形状是 `(batch_size, n_head, pre_seq_len, n_embd)`；但折叠后的 `input_ids` 第一维是 `batch*num_choices`，主干内部把前缀 key/value 拼到真实 key/value 前面时，两个张量在第 0 维（batch）对不上，触发广播失败/维度不匹配错误。

**练习 2**：为什么多选题的 `classifier` 用 `Linear(hidden, 1)`，而单句分类用 `Linear(hidden, num_labels)`？

**答案**：多选的本质是「给每个候选打一个分数、再在同组的 \(K\) 个分数间做 softmax」，所以每个候选只需要 **1 个标量**，\(K\) 个标量靠 `reshape(-1, num_choices)` 自然成组，由 `CrossEntropyLoss` 在 \(K\) 维上比较；而单句分类是「一条序列对应多个类别」，一次就要输出 `num_labels` 个 logits 直接比较。两者维度选择不同，正是任务定义不同的体现。

---

### 4.2 Token 分类模型：逐 token logits 与 active_loss 掩码

#### 4.2.1 概念说明

Token 分类与多选、单句分类都不同：它要为**序列里的每一个位置**都给出一个 logits 向量。所以模型头是 `classifier = Linear(hidden, num_labels)`，输出形状是 `(batch, seq_len, num_labels)`，损失是把这些 logits 与 `(batch, seq_len)` 的逐 token 标签对齐后做交叉熵。

难点在于 **padding**：batch 内序列长短不一，短的补 0，这些 padding 位置既不是真实 token，也不该有监督信号。解决方案是 **`active_loss` 掩码**：

- 用 `attention_mask.view(-1) == 1` 得到一个布尔向量，真实 token 位置为 `True`、padding 位置为 `False`。
- 用 `torch.where` 把 padding 位置的标签替换成 `-100`（`CrossEntropyLoss.ignore_index`），让它们对 loss 完全无贡献。

而本讲比 u4-l2 多一层新问题：**注入前缀后，`attention_mask` 被拼长了 `pre_seq_len`，但它和 `logits` 的长度对不上了**。下文 4.2.3 会讲到一行 `attention_mask = attention_mask[:, self.pre_seq_len:]`，它就是为了切掉前缀段、让掩码重新与逐 token 的 logits 对齐。

#### 4.2.2 核心流程

```text
input_ids: (batch, seq)
   │
   ▼  get_prompt(batch)        生成逐层 past_key_values
   ▼  attention_mask 前面拼 pre_seq_len 个 1   →  长度 = pre_seq_len + seq
   ▼  self.bert(..., past_key_values=...)      深度注入，主干冻结
sequence_output: (batch, seq)        # 注意：前缀不产生输出 token，输出长度仍是 seq
   ▼  classifier = Linear(hidden, num_labels)
logits: (batch, seq, num_labels)
   ▼  ★ attention_mask = attention_mask[:, pre_seq_len:]   切掉前缀段 → 长度回到 seq
   ▼  active_loss = attention_mask.view(-1) == 1            padding 位置 False
   ▼  active_labels = where(active_loss, labels, -100)      padding 标签置 -100
   ▼  CrossEntropyLoss(active_logits, active_labels)
loss
```

带 ★ 的那一步是 Token 分类（与抽取式 QA）**独有**的细节：因为 `logits` 是逐 token 的，而拼了前缀的 `attention_mask` 比它长 `pre_seq_len`，必须先切掉前缀段，掩码才能和 logits 一一对应。单句分类和多选题用的是 pooled 向量（`outputs[1]`，每个样本一个向量），不存在这种长度错配，所以不需要这步。

#### 4.2.3 源码精读

[model/token_classification.py:L107-L128](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/token_classification.py#L107-L128) —— `BertPrefixForTokenClassification` 构造函数：冻结主干（L119-L120），配 prefix 字段（L122-L128）。注意 `classifier` 输出维度是 `num_labels`（L113），与多选的 `1` 形成对照。

[model/token_classification.py:L155-L186](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/token_classification.py#L155-L186) —— forward 前半段：`get_prompt` 生成前缀（L171），把 `pre_seq_len` 个 1 拼到 `attention_mask` 前（L172-L173），再连同 `past_key_values` 喂进冻结的 `self.bert`（L175-L186）。这段与单句分类的 forward 几乎逐字相同。

[model/token_classification.py:L188-L191](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/token_classification.py#L188-L191) —— **本模块最关键的一行**：

```python
logits = self.classifier(sequence_output)                              # L190
attention_mask = attention_mask[:,self.pre_seq_len:].contiguous()       # L191  ★ 切掉前缀段
```

`logits` 形状是 `(batch, seq, num_labels)`（前缀不占输出位置），而此刻 `attention_mask` 还带着前缀段、形状是 `(batch, pre_seq_len + seq)`。L191 把前缀段切掉，让它变回 `(batch, seq)`，否则下面的 `view(-1)` 长度会和 `logits` 对不上。

[model/token_classification.py:L193-L203](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/token_classification.py#L193-L203) —— **`active_loss` 掩码**（这是本模块的核心机制）：

```python
active_loss = attention_mask.view(-1) == 1                                       # L198
active_logits = logits.view(-1, self.num_labels)                                 # L199
active_labels = torch.where(                                                      # L200-L202
    active_loss, labels.view(-1), torch.tensor(loss_fct.ignore_index).type_as(labels)
)
loss = loss_fct(active_logits, active_labels)                                    # L203
```

逐行解释：

- L198 把 `(batch, seq)` 的掩码拉平成一维布尔向量，真实 token 为 `True`、padding 为 `False`。
- L200-L202 用 `torch.where`：`True` 的位置保留真实 `labels`，`False` 的位置（padding）替换成 `-100`。
- L203 用 `CrossEntropyLoss`（默认 `ignore_index=-100`）计算损失，padding 位置因标签为 `-100` 被自动忽略。

> 与 u4-l2 的呼应：数据侧已经用 `-100` 屏蔽了「非首子词」的标签；这里的 `active_loss` 是模型侧再用 `attention_mask` 屏蔽「padding」位置。两道 `-100` 共同保证：每个真实 token 的首子词恰好贡献一次监督。

> 参考：vanilla 版 [BertForTokenClassification 的同款 loss 代码](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/token_classification.py#L82-L93)（L82-L93）不含 prefix，因此没有 L191 那行切前缀——对照阅读能更清楚 L191 是 prefix 注入带来的额外补偿。

#### 4.2.4 代码实践

**实践目标**：用一小段示例代码看清 `active_loss` 如何屏蔽 padding，以及「不切前缀」会导致的长度错配。

**操作步骤**（**示例代码**，不依赖预训练模型）：

```python
import torch

batch, seq, num_labels = 1, 5, 3
# 假设第 3、4 个位置是 padding
labels       = torch.tensor([[1, 2, 0, -100, -100]])   # (1,5) 数据侧已置 -100
attention_mask = torch.tensor([[1, 1, 1, 0,   0]])     # (1,5) padding=0
logits = torch.randn(batch, seq, num_labels)           # (1,5,3)

active_loss  = attention_mask.view(-1) == 1                       # [T,T,T,F,F]
active_logits = logits.view(-1, num_labels)                       # (5,3)
active_labels = torch.where(
    active_loss, labels.view(-1),
    torch.tensor(-100).type_as(labels)
)
print("active_labels:", active_labels.tolist())
# 输出 [1, 2, 0, -100, -100] —— 即便数据侧漏标，模型侧也会把 padding 强制置 -100
```

**再演示前缀切错的情况**（理解 L191 的必要性）：

```python
pre_seq_len = 2
# 模拟“忘了切前缀”：mask 带着前缀段，长度 = 2 + 5 = 7，而 logits 仍是 (1,5,3)
mask_with_prefix = torch.cat([torch.ones(batch, pre_seq_len), attention_mask], dim=1)
print("mask 长度:", mask_with_prefix.view(-1).shape[0],  # 7
      " logits 拉平行数:", logits.view(-1, num_labels).shape[0])  # 5 —— 不等！
# 修复：切掉前缀段
fixed_mask = mask_with_prefix[:, pre_seq_len:].contiguous()
print("修复后 mask 长度:", fixed_mask.view(-1).shape[0])  # 5 —— 与 logits 对齐
```

**需要观察的现象**：

1. 第一段输出 `[1, 2, 0, -100, -100]`，padding 位置被强制设为 `-100`，即便数据侧原本没标也会被屏蔽。
2. 第二段打印出 `mask 长度: 7` 与 `logits 拉平行数: 5` 不相等，直观证明「不切前缀」会导致掩码与 logits 长度不匹配；切掉前缀段后两者都变成 5。

**预期结果**：两段打印与上面注释一致。这就解释了为何 [L191](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/token_classification.py#L191) 必须存在——它是 prefix 注入给逐 token 任务带来的「对齐税」。

#### 4.2.5 小练习与答案

**练习 1**：单句分类模型（u2-l2）也注入了前缀、也拼长了 `attention_mask`，为什么它**不需要**像 L191 那样切前缀？

**答案**：单句分类用的是 pooled 输出 `outputs[1]`，每个样本一个向量，损失直接在 `(batch, num_labels)` 的 logits 与 `(batch,)` 的标签上算，根本没有「逐位置」这一维，掩码长度是否含前缀无所谓。Token 分类（和抽取式 QA）的损失是逐 token 的，`attention_mask` 必须和 `seq` 维一一对应，所以必须切掉前缀段。

**练习 2**：如果删掉 `active_loss` 这套掩码、直接 `loss_fct(logits.view(-1, num_labels), labels.view(-1))`（即 L205 的 else 分支），在 padding 位置数据侧又没标 `-100` 时会发生什么？

**答案**：padding 位置的 logits 会被当成真实预测参与交叉熵，等于让模型去学习「把 padding 预测成某个标签」，引入大量错误监督，损失被噪声主导、训练效果下降。`active_loss` 掩码正是为了排除这些位置。

---

### 4.3 统一的 get_prompt 复用：一份代码，三种任务

#### 4.3.1 概念说明

把 4.1 和 4.2 的源码并排放，你会发现一个惊人的一致性：**多选、Token 分类、单句分类的 `get_prompt` 函数体完全相同**，连标点都一样。这不是巧合，而是 P-tuning v2 设计的核心思想——**深度提示调优是「任务无关」的**。

`get_prompt` 只做一件事：把 `PrefixEncoder` 的扁平输出重排成 HuggingFace `past_key_values` 的逐层 `(key, value)` 格式。它不关心：

- 下游任务是分类、多选还是标注；
- 输入是 `(batch, seq)` 还是 `(batch, num_choices, seq)`（后者折叠后等价于一个更大的 batch）；
- 损失怎么算、有几个标签。

正因为 `get_prompt` 只依赖「batch 大小」这一个外部量，三类任务才能**原样复制**这段代码。理解这一点，就能解释为什么本讲两个看似不同的模型，本质上只是「换了一个头 + 换了一种 loss 对齐方式」，而前缀机制分毫未改。

#### 4.3.2 核心流程

三类任务共享同一个注入骨架，区别只在「batch 怎么算」和「拿到 sequence_output 之后做什么」：

| 任务 | 传给 get_prompt 的 batch_size | 主干输出取用 | 头/损失 |
|------|------------------------------|-------------|---------|
| 单句分类 | `batch` | pooled `outputs[1]` | `Linear(hidden, num_labels)` + CE/MSE |
| 多选题 | `batch * num_choices` | pooled `outputs[1]` | `Linear(hidden, 1)` + CE（reshape 回组）|
| Token 分类 | `batch` | sequence `outputs[0]` | `Linear(hidden, num_labels)` + 逐 token CE（active_loss）|

注入骨架本身完全一致：

```text
get_prompt(batch_size)  →  past_key_values（逐层 key/value）
prefix_attention_mask = ones(batch_size, pre_seq_len)
attention_mask = cat([prefix_attention_mask, attention_mask], dim=1)
backbone(..., attention_mask=..., past_key_values=...)   # 主干冻结
```

#### 4.3.3 源码精读

**三份逐字相同的 `get_prompt`**，分别在：

- 单句分类：[model/sequence_classification.py:L130-L143](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/sequence_classification.py#L130-L143)
- 多选题：[model/multiple_choice.py:L159-L171](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/multiple_choice.py#L159-L171)
- Token 分类：[model/token_classification.py:L140-L153](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/token_classification.py#L140-L153)

三者的函数体都是同样的 `view → dropout → permute([2,0,3,1,4]) → split(2)`，唯一的差异只是 `self.bert`/`self.roberta`/`self.deberta` 这种主干属性名。

[model/token_classification.py:L140-L153](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/token_classification.py#L140-L153) —— 以 Token 分类版为例，逐行标注 5 个维度的含义：

```python
past_key_values = self.prefix_encoder(prefix_tokens)            # (B, P, 2*L*H)
past_key_values = past_key_values.view(
    batch_size,          # B  —— 这一维由调用方决定（多选时是 batch*num_choices）
    self.pre_seq_len,    # P  —— 前缀长度
    self.n_layer * 2,    # 2L —— 层数 × (key+value 两份)
    self.n_head,         # n  —— 注意力头数
    self.n_embd          # H/n—— 每头维度
)                                                              # → (B, P, 2L, n, H/n)
past_key_values = self.dropout(past_key_values)
past_key_values = past_key_values.permute([2, 0, 3, 1, 4]).split(2)
# permute → (2L, B, n, P, H/n)；split(2) → 长度 L 的 tuple，每段 (2, B, n, P, H/n) 即一层 (key, value)
return past_key_values
```

这正是 u2-l2 推导过的恒等式 \(2L \times n \times (H/n) = 2LH\) 的工程落地：`PrefixEncoder` 输出的最后一维 `2*L*H` 恰好能被 `view` 拆成上面 5 维。

**注册表把这三类模型织进同一个分发网络**：[model/utils.py:L46-L71](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/utils.py#L46-L71) 中，`PREFIX_MODELS` 是 `(model_type, TaskType)` 的二维表。多选与 Token 分类分别对应 `TaskType.MULTIPLE_CHOICE`（如 [L51](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/utils.py#L51)）与 `TaskType.TOKEN_CLASSIFICATION`（如 [L48](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/utils.py#L48)）。`get_model` 据 `--prefix` 走入这张表（[L92-L103](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/utils.py#L92-L103)），把对应模型类实例化——本讲的两类模型就是这样被路由进来的。

> 顺带注意注册表里的**空位**：`deberta-v2` 在多选（[L69](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/utils.py#L69)）等多个 TaskType 上是 `None`。若强行用 `deberta-v2` + prefix + 多选，会触发 `None.from_pretrained(...)` 报错。这是 u3-l2 提到的「非满表」陷阱在多选任务上的具体体现。

#### 4.3.4 代码实践

**实践目标**：用「源码阅读 + 对照表」的方式，亲手验证三类任务的 `get_prompt` 完全一致、区别只在 batch 维与输出头。

**操作步骤**：

1. 分别打开上面列出的三处 `get_prompt` 永久链接。
2. 逐行比对，记录任何不同（你会发现只有主干属性名 `self.bert/self.roberta/self.deberta` 不同，逻辑完全一致）。
3. 再分别打开三者的 forward，填出下表：

| 对照项 | 单句分类（u2-l2） | 多选题（4.1） | Token 分类（4.2） |
|--------|------------------|--------------|------------------|
| `get_prompt` 的 batch_size 参数 | `batch_size` | `batch_size * num_choices` | `batch_size` |
| 取用的主干输出 | `outputs[1]`（pooled） | `outputs[1]`（pooled） | `outputs[0]`（sequence） |
| 分类头输出维度 | `num_labels` | `1` | `num_labels` |
| 是否需要切前缀掩码（`[:, pre_seq_len:]`） | 否 | 否 | **是** |
| 损失 | CE/MSE（整句） | CE（reshape 回组） | 逐 token CE（active_loss） |

**需要观察的现象**：`get_prompt` 一列三类完全相同；差异集中在「batch 维怎么算」「用 pooled 还是 sequence」「头输出几维」「要不要切前缀」四项。

**预期结果**：你能用一句话概括——「前缀注入是公共底座，任务差异只体现在头与损失对齐方式上」。如果填表时发现某格不确定，回到对应的源码行号核对即可。

#### 4.3.5 小练习与答案

**练习 1**：既然 `get_prompt` 在三类任务里逐字相同，为什么不把它抽成一个公共函数/基类，而要复制三份？

**答案**：从工程整洁角度确实可以抽取（例如做成一个 `PrefixMixin` 或基类）。当前实现选择「每个模型类自包含」，好处是单个文件可独立阅读、便于按任务裁剪；代价是 `get_prompt` 与冻结/参数统计的样板代码重复多份。这是「可读性 vs. 去重」的一种典型取舍。

**练习 2**：浅层 Prompt 模式（如 `BertPromptForMultipleChoice`）也写了 `get_prompt`，它和 prefix 版的 `get_prompt` 一样吗？

**答案**：不一样。浅层 Prompt 版的 `get_prompt` 只返回一段普通嵌入 `prompts = self.prefix_encoder(prefix_tokens)`（`prefix_encoder` 是 `nn.Embedding`，见 [model/multiple_choice.py:L497-L500](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/multiple_choice.py#L497-L500)），随后拼进 `inputs_embeds`（[L533-L534](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/multiple_choice.py#L533-L534)），不产生 `past_key_values`、不做逐层注入。这与 u2-l3 讲的「深层 Prefix vs 浅层 Prompt」区分一致——只有深层 Prefix 的 `get_prompt` 才有 `view/permute/split` 那套重排。

---

## 5. 综合实践

**任务**：以「新增一个 prefix 版的多选题任务」为线索，把本讲三个最小模块串起来，画一张端到端数据流图并回答三个问题。

**背景**：假设你要用 RoBERTa + P-tuning v2 跑 SuperGLUE 的 COPA（4 选 1，`num_choices` 由数据预处理决定）。

**步骤**：

1. **路由追踪**：从 [model/utils.py:L46-L71](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/utils.py#L46-L71) 的 `PREFIX_MODELS` 出发，确认 `model_type=roberta` + `TaskType.MULTIPLE_CHOICE` 命中的是 `RobertaPrefixForMultipleChoice`（[L57](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/utils.py#L57)）。
2. **画数据流**：画出从 `input_ids (B, K, S)` 到 loss 的完整张量流，标注三处关键变形：
   - 折叠 `(B, K, S) → (B*K, S)`；
   - `get_prompt(B*K)` 生成前缀并拼掩码；
   - `classifier(hidden,1)` 打分后 `reshape(-1, K)` 还原。
3. **回答三个问题**：
   - 为什么 `get_prompt` 收到的是 `B*K` 而不是 `B`？（对应 4.1.3 的 L199）
   - 这个任务**不需要** 4.2 里 L191 那种「切前缀掩码」操作，为什么？（提示：多选用 pooled 输出）
   - 若改用浅层 `--prompt`，`get_prompt` 的返回值会变成什么、又如何进入主干？（对应 4.3.5 练习 2）

**预期产出**：一张含上述三处变形的数据流草图，以及对三个问题的简短文字回答。这道题同时检验了「折叠打分」「前缀复用」「深浅 Prompt 区别」三个模块——若三问都能答清，说明本讲内容已贯通。

---

## 6. 本讲小结

- 多选题的套路是 **折叠 → 打分 → 还原**：把 `(batch, num_choices, seq)` 折成 `(batch*num_choices, seq)`，用 `classifier = Linear(hidden, 1)` 给每个候选打一个标量分，再 `reshape(-1, num_choices)` 成组用交叉熵选最优。
- 折叠之后，prefix 注入只需把 `get_prompt` 的 `batch_size` 设为 `batch_size * num_choices`，主干眼中就成了一个普通的大 batch——注入逻辑与单句分类**逐字相同**。
- Token 分类输出 `(batch, seq, num_labels)` 的逐 token logits，用 `active_loss = attention_mask.view(-1)==1` 配合 `torch.where` 把 padding 标签替换为 `-100`，排除 padding 的监督噪声。
- Token 分类（及抽取式 QA）独有一行 `attention_mask = attention_mask[:, pre_seq_len:]`：因为逐 token 的损失要求掩码与 `seq` 维对齐，必须先切掉注入时拼上去的前缀段；用 pooled 输出的单句分类和多选题则不需要这步。
- 三类任务的 `get_prompt` **逐字相同**，区别只在「batch 维怎么算」「取 pooled 还是 sequence」「头输出几维」「要不要切前缀」——深度提示调优的任务无关性由此体现。
- 浅层 Prompt 模式的 `get_prompt` 只返回普通嵌入、拼进 `inputs_embeds`，不产生 `past_key_values`、不做逐层注入，与深层 Prefix 区分清楚。

## 7. 下一步学习建议

- **沿注册表补全图景**：u6-l1 已经讲了抽取式问答（QA），本讲补齐了多选与 Token 分类。至此 `TaskType` 的四类（序列分类 / 多选 / Token 分类 / 问答）的 prefix 模型已全部覆盖，建议回到 u3-l2 的 `PREFIX_MODELS` 注册表，确认每一格都能对应到一篇讲义。
- **进入 PT-Retrieval**：u7 系列会把同样的 prefix 机制迁移到稠密检索（DPR）的双编码器上。届时你会发现 PT-Retrieval 里的 `get_prompt` 仍是这套 `view/permute/split`——这正是本讲「任务无关性」结论的又一次印证，可作为复习点。
- **二次开发练习**：在读懂本讲后，可以尝试 u8-l1 的扩展任务——仿照 `RobertaPrefixForMultipleChoice` 给某个新主干写一个 prefix 多选模型，并把它登记进 `PREFIX_MODELS`，体会「复用 `get_prompt` + 换头」的开发节奏。
