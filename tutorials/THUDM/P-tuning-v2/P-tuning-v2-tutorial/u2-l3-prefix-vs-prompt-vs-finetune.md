# 三种调优模式对比：Prefix / Prompt / FullFineTune

## 1. 本讲目标

前两讲我们集中拆解了 P-tuning v2 的核心零件：`PrefixEncoder`（u2-l1）负责「造前缀」，`get_prompt` + `forward`（u2-l2）负责「把前缀逐层注入冻结的主干」。但 P-tuning v2 仓库里其实同时实现了**三种**调优模式，靠命令行开关切换：

- **Prefix（深层注入）**：本项目的招牌，每层都注入前缀 key/value。
- **Prompt（浅层提示）**：只在最底层嵌入层前面拼一段提示，对应论文里的 prompt tuning。
- **FullFineTune（全量微调）**：不解冻也不加前缀，回退成普通微调，作为对照基线。

学完本讲，你应当能够：

1. 看懂 `model/utils.py` 里 `get_model` 的三个分支如何根据 `--prefix` / `--prompt` 开关路由到不同的模型类。
2. 说清浅层 **Prompt** 模式和深层 **Prefix** 模式在「提示生成、输入方式、pooler 重建」三处的实现差异。
3. 理解三种模式各自如何统计、打印可训练参数量，以及为什么参数量差别如此巨大。

---

## 2. 前置知识

本讲建立在前两讲（u2-l1、u2-l2）之上，不重复细节，只回顾两个最关键的结论：

- **`past_key_values`**：HuggingFace Transformer 里每层注意力的「历史 key/value 缓存」。P-tuning v2 的深层 Prefix 模式正是伪造一份前缀缓存，让每层都以为「前面还有 `pre_seq_len` 个 token」，从而把提示渗透到每一层。
- **冻结主干**：前两种模式都在模型构造函数里把 `bert`/`roberta` 的 `requires_grad` 全部置为 `False`，只有前缀编码器和分类头可训练。

还需要一点 Transformer 基础：BERT 这类模型的最底层是一个 **嵌入层（embeddings）**，把 `input_ids`（整数 token 序列）变成连续向量序列 `inputs_embeds`，再逐层送进 Transformer。深层 Prefix 模式绕过嵌入层、直接从第二层起注入；浅层 Prompt 模式则「挤进」嵌入层、和真实 token 的向量拼在一起。理解这个区别，是本讲的核心。

下面三个命令行开关（定义在 [arguments.py:125-154](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/arguments.py#L125-L154)）决定走哪条路：

| 开关 | 默认 | 作用 |
| --- | --- | --- |
| `--prefix` | `False` | 启用 P-tuning v2（深层注入） |
| `--prompt` | `False` | 启用浅层 prompt tuning |
| 二者都不加 | — | 全量微调 |
| `--pre_seq_len` | `4` | 前缀/提示长度（运行脚本里常覆盖为 128） |

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [model/utils.py](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/utils.py) | 模型工厂：三张注册表（`PREFIX_MODELS`/`PROMPT_MODELS`/`AUTO_MODELS`）与分发函数 `get_model`，是三种模式的「总开关」。 |
| [model/sequence_classification.py](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/sequence_classification.py) | 分类任务的三套模型实现都在这里：`BertForSequenceClassification`（全量微调基类）、`BertPrefixForSequenceClassification`（深层）、`BertPromptForSequenceClassification`（浅层），以及 RoBERTa/DeBERTa 的对应版本。 |
| [model/prefix_encoder.py](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/prefix_encoder.py) | `PrefixEncoder`，只有深层 Prefix 模式会用到；浅层 Prompt 模式用的是一张普通 `nn.Embedding`。 |

---

## 4. 核心概念与源码讲解

### 4.1 get_model 的三种分支与模式开关

#### 4.1.1 概念说明

`get_model` 是一个**模型工厂（factory）**：它接收「用户想要哪种调优模式」和「任务类型」，返回一个装配好的模型实例。三种模式对应三张注册表：

- `PREFIX_MODELS`：深层注入模型，覆盖四种任务、多种主干。
- `PROMPT_MODELS`：浅层提示模型，只覆盖 BERT/RoBERTa 的「分类」和「多选」两类任务（任务覆盖面比 Prefix 窄）。
- `AUTO_MODELS`：HuggingFace 原生 `AutoModelFor*`，用于全量微调。

设计上，`get_model` 是一个二维路由：**模型类型（model_type）× 任务类型（task_type）→ 具体模型类**。开关 `prefix` / `prompt` 决定查哪张表。

#### 4.1.2 核心流程

```text
get_model(model_args, task_type, config, fix_bert)
│
├── model_args.prefix == True ?
│     ├── 写入 config: hidden_dropout_prob / pre_seq_len / prefix_projection / prefix_hidden_size
│     ├── model_class = PREFIX_MODELS[model_type][task_type]
│     └── model = model_class.from_pretrained(...)
│           （冻结主干、统计参数、打印，全部发生在模型类 __init__ 内）
│
├── model_args.prompt == True ?            # 只在 prefix 为 False 时才判断
│     ├── 写入 config: pre_seq_len         # 注意：只写 pre_seq_len，不写 prefix_projection
│     ├── model_class = PROMPT_MODELS[model_type][task_type]
│     └── model = model_class.from_pretrained(...)
│
└── else（都不加）：全量微调
      ├── model_class = AUTO_MODELS[task_type]   # HF 原生 Auto
      ├── model = model_class.from_pretrained(...)
      ├── 若 fix_bert：把主干 requires_grad=False
      └── 在这里统计并打印 total_param
```

注意一个细节：**`prefix` 优先级高于 `prompt`**。源码用的是 `if prefix ... elif prompt ... else ...`，所以如果两个开关都传，只会走 Prefix 分支。

#### 4.1.3 源码精读

三分支的主体在 [model/utils.py:91-142](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/utils.py#L91-L142)。

**Prefix 分支**：把四个前缀相关字段写进 `config`，然后查 `PREFIX_MODELS` 表：

[model/utils.py:92-103](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/utils.py#L92-L103) —— 写 `config.pre_seq_len`、`config.prefix_projection`、`config.prefix_hidden_size`，并据 `(model_type, task_type)` 选出模型类。主干冻结与参数统计都推迟到模型类的 `__init__` 里完成。

**Prompt 分支**：只写 `pre_seq_len`，查 `PROMPT_MODELS` 表：

[model/utils.py:104-111](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/utils.py#L104-L111) —— 没有 `prefix_projection` 这一说，因为浅层提示只用一张普通 `Embedding`。

**全量微调分支**：查 `AUTO_MODELS`，并在这里就地统计、打印参数量：

[model/utils.py:112-141](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/utils.py#L112-L141) —— 注意最后的 `print('***** total param is {} *****'.format(total_param))`，这是三种模式里**唯一**在 `get_model` 内部打印参数量的地方。`fix_bert`（默认 `False`）控制是否冻结主干：

[model/utils.py:120-140](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/utils.py#L120-L140) —— 当 `fix_bert=True` 时遍历 `model.bert`/`roberta`/`deberta` 的参数，先 `requires_grad=False` 再累加 `bert_param`，于是 `total_param = all_param - bert_param` 只剩分类头可训练。

注册表本身的覆盖差异值得一看。[PREFIX_MODELS（model/utils.py:46-71）](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/utils.py#L46-L71) 覆盖四种任务 × 四种主干；[PROMPT_MODELS（model/utils.py:73-82）](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/utils.py#L73-L82) 只覆盖 BERT/RoBERTa 的分类与多选——这从代码层面说明：**浅层 Prompt 模式只是作为对照实现，并非本项目的重点**。

#### 4.1.4 代码实践

1. **实践目标**：亲手在注册表里追踪一条路由路径，验证「开关 → 表 → 类」的对应关系。
2. **操作步骤**：
   - 打开 [model/utils.py](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/utils.py)。
   - 假设命令为 `model_type=roberta`、`--prefix`、任务为 `SEQUENCE_CLASSIFICATION`。
   - 先看 `get_model` 进 `if model_args.prefix:` 分支，查 `PREFIX_MODELS["roberta"][TaskType.SEQUENCE_CLASSIFICATION]`。
   - 在 `PREFIX_MODELS` 字典里找到 `"roberta"` 键，再找 `SEQUENCE_CLASSIFICATION` 对应的值。
3. **预期结果**：得到 `RobertaPrefixForSequenceClassification`（见 [model/utils.py:13](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/utils.py#L13) 的 import）。
4. **待本地验证**：若把 `--prefix` 换成 `--prompt`，路径应改走 `PROMPT_MODELS["roberta"][SEQUENCE_CLASSIFICATION]`，得到 `RobertaPromptForSequenceClassification`；但若任务换成 `TOKEN_CLASSIFICATION`（NER），`PROMPT_MODELS` 里没有这一项，运行时会 `KeyError`。可在本地用一张极小的 dummy config 触发并确认报错。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `get_model` 用 `if prefix ... elif prompt ... else` 而不是三个独立的 `if`？

**参考答案**：三种模式互斥——同一个模型不可能既深层注入又浅层拼提示。`if/elif/else` 表达了这种「三选一」的排他关系；若用三个独立 `if`，同时传 `--prefix` 和 `--prompt` 时 `model` 会被连续覆盖两次，既浪费又容易产生不可预期的状态。

**练习 2**：浅层 Prompt 分支为什么没有像 Prefix 分支那样写 `config.prefix_projection`？

**参考答案**：浅层 Prompt 模式只用一张 `nn.Embedding(pre_seq_len, hidden_size)` 直接查表，不存在「是否加两层 MLP 重参数化」这个选项，所以不需要 `prefix_projection`/`prefix_hidden_size`。

---

### 4.2 Prompt 模式的浅层注入：inputs_embeds 拼接与切片

#### 4.2.1 概念说明

这是本讲最核心的对比模块。同样是「在前缀位置加 `pre_seq_len` 个可训练向量」，**深层 Prefix**（u2-l2 已详述）把它们伪装成每一层的 key/value 缓存，从第二层起逐层注入；而**浅层 Prompt** 则把它们当作「假 token 的嵌入向量」，**只在最底层的嵌入层**和真实 token 的向量拼在一起，之后不再额外干预任何一层。

直觉上：深层 Prefix 像是在每一层都安插了「提示助手」；浅层 Prompt 只在最门口贴了一张「提示便签」，信息要靠网络自己一层层往后传。后者更轻、参数更少，但在困难任务上能力通常不如前者——这正是 P-tuning v2 相对 prompt tuning 的主要改进点。

因为浅层 Prompt 把提示拼进了输入序列，它带来了两个深层 Prefix 没有的「麻烦」：

1. **输入方式改变**：不能再直接喂 `input_ids`，必须先把 token 转成 `inputs_embeds`，再把提示向量拼到前面，最后把拼接结果当成输入。
2. **pooler 要手工重建**：网络输出的序列里前 `pre_seq_len` 个位置是提示，真正的 `[CLS]`（第一个真实 token）不再是位置 0，所以不能直接用 HF 自带的池化输出，要先把提示段切片切掉。

#### 4.2.2 核心流程

浅层 Prompt 的前向流程：

```text
input_ids, attention_mask
   │
   ├── raw_embedding = bert.embeddings(input_ids, position_ids, token_type_ids)
   │        # 把真实 token 变成向量序列：(batch, seq_len, hidden)
   │
   ├── prompts = get_prompt(batch_size)
   │        # 查 Embedding 表：(batch, pre_seq_len, hidden)
   │
   ├── inputs_embeds = cat([prompts, raw_embedding], dim=1)
   │        # 在序列维度前面拼提示：(batch, pre_seq_len + seq_len, hidden)
   │
   ├── attention_mask = cat([全 1 的 pre_seq_len 段, 原 attention_mask], dim=1)
   │
   ├── outputs = bert(inputs_embeds=inputs_embeds, attention_mask=...)
   │        # 不再传 input_ids，也不传 past_key_values
   │
   └── 重建 pooler：
          sequence_output = outputs[0][:, pre_seq_len:, :]   # 切掉提示段
          first_token_tensor = sequence_output[:, 0]          # 取真正的 [CLS]
          pooled = pooler.dense(first_token_tensor)           # 复用主干的池化权重
          pooled = pooler.activation(pooled)
```

对比深层 Prefix 的前向（u2-l2）：它**保留** `input_ids`，**不改** embedding，只是额外 `past_key_values=...` 注入，并且**直接用** `outputs[1]`（HF 原生池化）。三处差异一一对应。

#### 4.2.3 源码精读

浅层 Prompt 模型的类是 `BertPromptForSequenceClassification`，与 `BertPrefixForSequenceClassification` 并列。我们先看三处关键差异。

**差异一：提示生成（构造函数 + get_prompt）。** 浅层 Prompt 用的是一张普通 `nn.Embedding`，而不是 `PrefixEncoder`：

[model/sequence_classification.py:234-240](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/sequence_classification.py#L234-L240) —— `self.prefix_encoder = torch.nn.Embedding(self.pre_seq_len, config.hidden_size)`，输出维度只有 `hidden_size`；`get_prompt` 直接返回查表结果，**没有** `view/permute/split` 那一套重排，因为它根本不构成 `past_key_values`。

对照深层 Prefix 的 [model/sequence_classification.py:119 与 130-143](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/sequence_classification.py#L119-L143)：那里用的是 `PrefixEncoder(config)`，输出 `2*L*H`，还要重排成每层 (key, value)。

**差异二：输入方式。** 浅层 Prompt 不传 `input_ids`，而是手工拼 `inputs_embeds`：

[model/sequence_classification.py:257-279](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/sequence_classification.py#L257-L279) —— 先 `raw_embedding = self.embeddings(input_ids=...)`，再 `inputs_embeds = torch.cat((prompts, raw_embedding), dim=1)`，调用 `self.bert(...)` 时 `input_ids` 被注释掉、改传 `inputs_embeds`，且**没有** `past_key_values`。`attention_mask` 同样要在前面拼 `pre_seq_len` 个 1（[第 265-266 行](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/sequence_classification.py#L265-L266)），否则提示会被当 padding 屏蔽。

对照深层 Prefix 的 [model/sequence_classification.py:161-176](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/sequence_classification.py#L161-L176)：那里照常传 `input_ids`，额外多一个 `past_key_values=past_key_values`。

**差异三：pooler 重建。** 浅层 Prompt 必须手工重建池化输出：

[model/sequence_classification.py:281-289](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/sequence_classification.py#L281-L289) —— 取 `outputs[0]`（last hidden state），用 `[:, self.pre_seq_len:, :]` 把开头的提示段切掉，再取第 0 个位置（即真正的 `[CLS]`），手动调用主干的 `pooler.dense` 和 `pooler.activation` 复现池化。原代码注释里 `# pooled_output = outputs[1]` 说明：这里**不能**直接用 `outputs[1]`，因为 HF 的 pooler 拿的是「序列第 0 个位置」，而第 0 个位置现在被提示占了。

对照深层 Prefix 的 [model/sequence_classification.py:178-181](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/sequence_classification.py#L178-L181)：那里 `pooled_output = outputs[1]` 直接可用，因为深层注入没有把提示塞进输入序列，`[CLS]` 仍在位置 0。

> 顺带一提：RoBERTa 的两个对应类（[RobertaPrefix...](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/sequence_classification.py#L326-L440) 与 [RobertaPrompt...](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/sequence_classification.py#L444-L551)）实现模式与 BERT 完全一致，只是把 `self.bert` 换成 `self.roberta`。

#### 4.2.4 代码实践

1. **实践目标**：完成规格里要求的三方对照表，把 `BertPromptForSequenceClassification` 与 `BertPrefixForSequenceClassification` 在「提示生成、输入方式、pooler 重建」三处的差异写清楚。
2. **操作步骤**：
   - 打开 [model/sequence_classification.py](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/sequence_classification.py)。
   - 分别定位 `BertPrefixForSequenceClassification`（第 101 行起）和 `BertPromptForSequenceClassification`（第 217 行起）。
   - 在三个维度上逐行比对，自己先填一张表，再与下文答案核对。
3. **需要观察的现象**：两个类的 `forward` 主体几乎一样（损失计算、返回结构都相同），真正的差别全集中在「提示如何进入网络」这一段——这正说明三种模式是「同一套任务外壳 + 不同的提示机制」。
4. **预期结果（对照表）**：

   | 维度 | Prefix（深层）`BertPrefix...` | Prompt（浅层）`BertPrompt...` |
   | --- | --- | --- |
   | 提示生成 | `PrefixEncoder`，输出 `(batch, P, 2*L*H)`，再 `view/permute/split` 成每层 (key,value) | 普通 `nn.Embedding(P, H)`，输出 `(batch, P, H)`，直接用 |
   | 注入位置 | 每一层注意力，经 `past_key_values` | 仅最底层嵌入层，拼进 `inputs_embeds` |
   | 输入方式 | 照常传 `input_ids`，额外加 `past_key_values` | 不传 `input_ids`，改传拼接后的 `inputs_embeds` |
   | attention_mask | 前面拼 `P` 个 1 | 同样前面拼 `P` 个 1 |
   | pooler 重建 | 直接 `outputs[1]` | 切掉前 `P` 段后取真正 `[CLS]`，手动调 `pooler.dense/activation` |
   | 参数量 | 较大（见 4.3） | 极小 |

5. **待本地验证**：可在两个类里临时加一行 `print` 打印 `prompts.shape`/`past_key_values[0][0].shape`（Prefix 模式）来核对形状差异；本步骤只读不改，确认后删掉 print。

#### 4.2.5 小练习与答案

**练习 1**：浅层 Prompt 模式里，为什么取池化向量前必须先 `[:, self.pre_seq_len:, :]` 切片？

**参考答案**：因为提示向量被拼在了序列最前面，网络输出的第 0 个位置是提示而非真实 `[CLS]`。HF 默认 pooler 取的是序列第 0 个位置，若不切片，池化的是提示向量，分类会彻底失效。切片后第 0 个位置才恢复成真正的 `[CLS]`。

**练习 2**：深层 Prefix 模式同样也加了 `pre_seq_len` 个提示位置，为什么它**不用**做这个切片？

**参考答案**：深层 Prefix 不把提示放进输入序列，真实 token 的位置编号没变；提示是通过 `past_key_values` 在每层注意力里「虚拟地」出现在前面的，不影响 `[CLS]` 在序列里的下标。所以 `outputs[1]` 仍然正确指向真实 `[CLS]` 的池化结果。

---

### 4.3 可训练参数量统计与三者对比

#### 4.3.1 概念说明

三种模式最直观的差异是**可训练参数量**。设主干层数为 \(L\)、隐藏维为 \(H\)、前缀长度为 \(P\)、分类类别数为 \(C\)、（仅 Prefix 用到的）MLP 中间维为 \(H_m\)。三种模式「可训练」的部分：

- **深层 Prefix**：`PrefixEncoder` + 分类头。当 `prefix_projection=False` 时只有一个查表 Embedding：

  \[
  N_{\text{prefix}}^{(\text{no-mlp})} = P \cdot (2 L H) + (H\cdot C + C)
  \]

  当 `prefix_projection=True` 时多一个两层 MLP：

  \[
  N_{\text{prefix}}^{(\text{mlp})} \approx P H + H\cdot H_m + H_m \cdot (2 L H) + (H\cdot C + C)
  \]

- **浅层 Prompt**：只有一张小 Embedding + 分类头：

  \[
  N_{\text{prompt}} = P \cdot H + (H\cdot C + C)
  \]

- **全量微调**：主干全部可训练（`fix_bert=False`）或仅分类头可训练（`fix_bert=True`）。前者即整个主干参数量，约数千万到数亿。

一个很有意思的比例：浅层 Prompt 与深层 Prefix（无 MLP）的前缀参数量之比约为

\[
\frac{N_{\text{prompt}}}{N_{\text{prefix}}^{(\text{no-mlp})}} \approx \frac{P H}{P \cdot 2 L H} = \frac{1}{2L}
\]

对 \(L=24\)（large 模型）而言，浅层 Prompt 的前缀参数只有深层 Prefix 的约 \(1/48\)——参数更省，但提示只贴在最底层，能力也相应受限。

#### 4.3.2 核心流程

三种模式的参数统计与打印位置各不相同，这一点容易混淆，下面分别说明：

```text
深层 Prefix / 浅层 Prompt：
   统计与打印发生在「模型类 __init__」内部：
     bert_param  = Σ backbone.parameters().numel()
     all_param   = Σ self.parameters().numel()
     total_param = all_param - bert_param      # 排除已冻结主干
     print(...)                                  # 在 __init__ 里打印

全量微调（else）：
   统计与打印发生在「get_model」内部：
     若 fix_bert：先 backbone.requires_grad=False，再累加 bert_param
     total_param = all_param - bert_param       # fix_bert=False 时 bert_param=0
     print('***** total param is {} *****')
```

关键差异：前两种模式的 `total_param` 是「**总参数 − 冻结主干**」，因为主干在 `__init__` 里就被冻结了；全量微调分支则由 `fix_bert` 决定 `bert_param` 是否计入。

#### 4.3.3 源码精读

**深层 Prefix 模式的统计**（在 `__init__` 内）：[model/sequence_classification.py:110-128](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/sequence_classification.py#L110-L128)。第 110-111 行先把主干 `requires_grad=False`；第 121-127 行分别累加 `bert_param` 与 `all_param`；第 128 行 `print('total param is {}'.format(total_param))`，开发者还在注释里留了一个示例值 `# 9860105`（约 986 万，对应某次启用 `prefix_projection` 的实验配置，仅作参考）。

**浅层 Prompt 模式的统计**（在 `__init__` 内）：[model/sequence_classification.py:217-235](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/sequence_classification.py#L217-L235)。这里**同样冻结了主干**（第 226-227 行），但**没有**像 Prefix 那样写参数统计与打印——这是一个实现上的不对称：浅层 Prompt 不会在日志里直接告诉你可训练参数量，需要读者自行估算（用上面的公式 \(P\cdot H\)）。

**全量微调分支的统计**（在 `get_model` 内）：[model/utils.py:137-141](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/utils.py#L137-L141)。

```python
all_param = 0
for _, param in model.named_parameters():
    all_param += param.numel()
total_param = all_param - bert_param
print('***** total param is {} *****'.format(total_param))
```

注意两处 `print` 的措辞不同：模型类内部是 `total param is {}`，工厂内部是 `***** total param is {} *****`。看到带星号的，就知道走的是全量微调分支。这与 u1-l2 提到的「带 `--prefix` 时主干在构造函数里冻结、不打印 total param」完全吻合——更准确地说：**带 `--prefix` 时打印在 `__init__` 里（不带星号）；不带任何前缀开关时打印在 `get_model` 里（带星号）**；带 `--prompt` 时则根本不打印。

#### 4.3.4 代码实践

1. **实践目标**：用公式估算三种模式的可训练参数量，建立数量级直觉。
2. **操作步骤**：取一个典型配置 RoBERTa-large：\(L=24\)、\(H=1024\)、\(P=128\)、\(C=2\)（二分类），分别计算。
3. **需要观察的现象**：三种模式的可训练量应相差几个数量级。
4. **预期结果**（前缀相关部分，未含很小的分类头）：
   - 浅层 Prompt：\(128 \times 1024 = 131{,}072 \approx 0.13\text{M}\)。
   - 深层 Prefix（无 MLP）：\(128 \times 2 \times 24 \times 1024 = 6{,}291{,}456 \approx 6.29\text{M}\)。
   - 深层 Prefix（带 MLP）：还多一项 \(H_m\cdot 2LH\)，量级进一步上升到约数千万（开发者注释示例值 `9860105`）。
   - 全量微调（`fix_bert=False`）：整个 RoBERTa-large 主干，约 \(355\text{M}\)。
5. **待本地验证**：上述为按公式的估算值；若本地有 GPU 环境，可分别用 `--prefix` / `--prompt` / 都不加跑一次 `run.py`，对照日志里两条不同措辞的 `print` 核对真实数字。无 GPU 时可只做估算。

#### 4.3.5 小练习与答案

**练习 1**：日志里看到 `***** total param is 355000000 *****`（带星号），说明走的是哪个分支？`fix_bert` 大概率是 True 还是 False？

**参考答案**：带星号的打印只出现在全量微调（`else`）分支的 `get_model` 里。如此大的数值（约 3.55 亿）几乎等于整个 RoBERTa-large 主干，说明 `fix_bert=False`（主干未冻结、全部可训练）。若 `fix_bert=True`，`total_param` 会只剩分类头那几万参数。

**练习 2**：为什么浅层 Prompt 模式在 `__init__` 里冻结了主干，却没有打印参数量？这会带来什么实际影响？

**参考答案**：实现上省略了统计代码（可能是开发者为对照实验写的最小实现）。影响是：跑 `--prompt` 时日志里看不到可训练参数量，需要读者自行用 `sum(p.numel() for p in model.parameters() if p.requires_grad)` 统计，否则容易误以为「什么都没训练」。这也是 u1-l2 强调「带 `--prefix` 时需自行统计 requires_grad 参数量」的更深一层原因——浅层 Prompt 模式连那条不带星号的打印都没有。

---

## 5. 综合实践

把三种模式串起来做一次「读码 + 推理」综合练习。

**任务**：假设你要为一个二分类任务跑三组对照实验——全量微调、深层 Prefix、浅层 Prompt，使用 RoBERTa-large、`pre_seq_len=128`。请回答：

1. 三条命令分别长什么样（关键开关各是什么）？参考 u1-l2 里的运行脚本结构。
2. 三条命令各自会实例化哪个模型类？（在 `model/utils.py` 注册表里追踪。）
3. 三条命令的日志里，分别会出现哪种参数量打印（带星号 / 不带星号 / 不打印）？
4. 三条命令的可训练参数量大致是多少（用 4.3 的公式估算，按数量级排序）？

**参考思路**：

1. 全量微调：不加 `--prefix` 也不加 `--prompt`；深层 Prefix：加 `--prefix`（可配 `--pre_seq_len 128`、可选 `--prefix_projection`）；浅层 Prompt：加 `--prompt --pre_seq_len 128`。
2. 全量微调走 `AUTO_MODELS` → `AutoModelForSequenceClassification`；深层 Prefix 走 `PREFIX_MODELS["roberta"][SEQUENCE_CLASSIFICATION]` → `RobertaPrefixForSequenceClassification`；浅层 Prompt 走 `PROMPT_MODELS["roberta"][SEQUENCE_CLASSIFICATION]` → `RobertaPromptForSequenceClassification`。
3. 全量微调：带星号打印；深层 Prefix：不带星号打印（在 `__init__`）；浅层 Prompt：不打印。
4. 数量级排序（从大到小）：全量微调（约 3.55 亿）≫ 深层 Prefix 带 MLP（约数千万）> 深层 Prefix 无 MLP（约 6.3M）> 浅层 Prompt（约 0.13M）。

**待本地验证**：在有 GPU 的环境下实际跑这三条命令（可用极小 epoch），核对日志打印与参数量；无 GPU 时只完成 1-3 的推理即可。

---

## 6. 本讲小结

- `get_model` 是三种模式的总开关：`--prefix` → `PREFIX_MODELS`（深层），`--prompt` → `PROMPT_MODELS`（浅层），都不加 → `AUTO_MODELS`（全量微调），且 `prefix` 优先级高于 `prompt`。
- 深层 Prefix 与浅层 Prompt 的核心差异在「提示如何进入网络」：前者用 `PrefixEncoder` 生成每层 key/value、经 `past_key_values` 逐层注入、照常传 `input_ids`、直接用 `outputs[1]`；后者用普通 `nn.Embedding` 生成提示向量、拼进 `inputs_embeds`、只在嵌入层注入、并须手工切片重建 pooler。
- 浅层 Prompt 模式少做了两件深层 Prefix 必须做的事：没有 `view/permute/split` 重排，也没有用 `PrefixEncoder`；但它多做了一件深层 Prefix 不需要做的事——切掉提示段后手工重建 pooler。
- 三种模式的参数量打印位置与措辞不同：深层 Prefix 在模型 `__init__` 里打印（不带星号），全量微调在 `get_model` 里打印（带星号），浅层 Prompt 不打印。
- 数量级上：全量微调 ≫ 深层 Prefix（带 MLP）> 深层 Prefix（无 MLP）> 浅层 Prompt；浅层 Prompt 的前缀参数约为深层 Prefix（无 MLP）的 \(1/(2L)\)。
- `PROMPT_MODELS` 的任务覆盖面明显窄于 `PREFIX_MODELS`（只有 BERT/RoBERTa 的分类与多选），从代码层面印证浅层 Prompt 只是对照实现，深层 Prefix 才是本项目重点。

---

## 7. 下一步学习建议

本讲把「分类任务下三种调优模式」讲透了，但 `get_model` 还涉及其他任务类型（序列标注、问答、多选）和 `fix_bert`、`TaskType` 注册表的更多细节。建议：

1. 进入 **u3-l2（模型工厂 get_model 与任务注册表）**，系统精读 `TaskType` 枚举与 `(model_type, task_type)` 二维注册表，把本讲的「三分支」放到「四任务 × 四主干」的完整网格里理解。
2. 之后再进入 **u4（数据处理与任务适配）**，看不同任务的数据如何与这里的模型类对接。
3. 若想立刻看到「深层 vs 浅层」在更复杂任务上的体现，可先跳读 [model/multiple_choice.py](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/multiple_choice.py)，那里同样有成对的 `*Prefix*` / `*Prompt*` 多选模型，模式与本讲完全一致，可作为巩固练习。
