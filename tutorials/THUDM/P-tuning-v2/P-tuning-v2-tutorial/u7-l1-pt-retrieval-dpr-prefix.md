# PT-Retrieval 概览与 DPR 前缀集成

## 1. 本讲目标

前六单元我们一直在读根目录的主 P-tuning v2 实现：冻结 BERT/RoBERTa 主干，用 `PrefixEncoder` 生成逐层 `past_key_values`，在分类、序列标注、问答等「理解类」任务上逼近全量微调。本讲第一次进入仓库里的另一个子项目 **PT-Retrieval**，它回答一个新问题：

> P-tuning v2 这套「冻结主干 + 深度前缀」的机制，能不能搬到**稠密检索（dense retrieval）**上，而且搬过去之后检索器不仅不掉点，反而更**通用（跨域泛化）**、更**校准（置信度更可信）**？

学完本讲你应当能够：

1. 说清 PT-Retrieval 的研究动机——为什么「只训少量参数」反而有助于跨域检索与校准。
2. 列出 PT-Retrieval 支持的**五类训练模式**（finetune / prefix / bitfit / adapter / prompt），并能在源码里找到它们的路由开关。
3. 理解 PT-Retrieval 为什么必须新写一个 `BertModelModifed`、改写 `forward` 才能让 `past_key_values` 真正进入 BERT 编码器。
4. 看清 PT-Retrieval 的 `BertPrefixModel.get_prompt` 与主项目 `BertPrefixForSequenceClassification.get_prompt` **逐字一致**，体会深度提示调优的「任务无关性」。
5. 动手对比两份 `PrefixEncoder`，找出迁移时需要改的字段名与适配点。

## 2. 前置知识

本讲是 **advanced** 阶段的第一篇检索讲义，默认你已掌握前置讲义建立的认知，这里只做最小回顾，不重复细节：

- **深度提示调优（u1-l1 / u2-l1）**：在 Transformer **每一层**都注入一段可训练的连续前缀 key/value，主干冻结，可训练参数量级约 \(2 \cdot L \cdot P \cdot H\)（L 层数、P 前缀长度 `pre_seq_len`、H 隐藏维），通常不到主干 1%。
- **`past_key_values` 注入（u2-l2）**：`PrefixEncoder` 输出扁平的 `(batch, pre_seq_len, 2*L*H)`，经 `view → permute([2,0,3,1,4]) → split(2)` 重排成 HuggingFace 的逐层 KV 缓存格式，伪装成「历史 key/value」拼到每层注意力前面；同时 `attention_mask` 要在前面补 `pre_seq_len` 个 1，否则前缀会被当 padding 屏蔽。
- **三种调优模式（u2-l3）**：深层 Prefix（逐层注入）vs 浅层 Prompt（只在嵌入层拼 `inputs_embeds`）vs 全量微调。

本讲要补充的两个新概念：

- **稠密检索 / 双塔模型（BiEncoder）**：把问题和文档分别编码成一个固定维度的向量，用向量点积给「问题—文档」打分；检索时只比较向量，不必每次跑神经网络对比每对文本，这是 DPR（Dense Passage Retrieval）的核心思想。本讲只读「编码器怎么加前缀」，双塔训练与 faiss 检索留到 u7-l2。
- **跨域泛化与校准**：检索器常在一个域（如维基百科问答）上训练、在另一个域（如科学论文）上评测。参数高效方法只动一点点权重，更接近预训练模型的「通用表征」，因此在跨域评测上往往更稳；校准（calibration）指模型给出的置信度分数是否与其真实正确概率匹配，PT-Retrieval 用 ECE（Expected Calibration Error）衡量。

## 3. 本讲源码地图

PT-Retrieval 子项目位于仓库的 `PT-Retrieval/` 目录下，它是 Facebook DPR 代码库的一个改版。本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [PT-Retrieval/README.md](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/README.md) | 说明 PT-Retrieval 的研究目标、五类训练模式、数据与评测流程。 |
| [PT-Retrieval/dpr/models/prefix.py](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/dpr/models/prefix.py) | 本讲主角：迁移过来的 `PrefixEncoder`、改写过的 `BertModelModifed`、检索用的 `BertPrefixModel` / `BertPromptModel`。 |
| [PT-Retrieval/dpr/models/hf_models.py](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/dpr/models/hf_models.py) | 组件工厂：`get_bert_biencoder_components` 按 5 种开关路由到不同编码器类，并把 args 里的前缀字段写进 config。 |
| [PT-Retrieval/run_scripts/run_train_dpr_multidata_ptv2.sh](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/run_scripts/run_train_dpr_multidata_ptv2.sh) | P-tuning v2 模式下训练 DPR 双塔的启动脚本，给出真实超参。 |
| [model/prefix_encoder.py](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/prefix_encoder.py) | 主项目的 `PrefixEncoder`，用于对比。 |
| [model/sequence_classification.py](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/sequence_classification.py) | 主项目的 `BertPrefixForSequenceClassification.get_prompt`，用于对比一致性。 |

---

## 4. 核心概念与源码讲解

### 4.1 PT-Retrieval 的研究动机与五类训练模式

#### 4.1.1 概念说明

PT-Retrieval 的全称是 *Parameter-Efficient Prompt Tuning Makes Generalized and Calibrated Neural Text Retrievers*（Findings of EMNLP 2023）。它的核心主张是：**参数高效微调（PEFT）不仅省显存，还能让稠密检索器在跨域、跨主题的评测上更通用、更校准。**

为什么会有这个主张？全量微调一个检索器时，大量参数被改动，模型容易「过拟合到训练域」，换一个领域（比如从维基问答换到学术论文检索）性能就大幅下降；而 P-tuning v2 这类方法只动主干不到 1% 的参数（前缀），主干表征几乎保持预训练时的「通用语义」，因此迁移到新域时更稳。PT-Retrieval 用 BEIR（15 个跨域检索数据集）和 OAG-QA（87 个主题的学术检索）来验证这一假设。

为了做严格的对比实验，PT-Retrieval 在同一套 DPR 双塔代码里实现了**五类训练模式**，README 列出如下：

- Fine-tuning（全量微调，作为基线）
- P-tuning v2（深度前缀，本仓库的主角）
- BitFit（只训偏置 bias）
- Adapter（在每层插入小适配模块）
- Lester et al. & P-tuning（浅层提示，对应主项目的 `--prompt`）

这五类模式覆盖了「该训哪些参数」的整个谱系，是理解后续路由代码的索引。

#### 4.1.2 核心流程

PT-Retrieval 训练一个 DPR 双塔的过程可以概括为：

1. 命令行用一个开关（如 `--prefix`）指定训练模式。
2. `get_bert_biencoder_components` 读这个开关，分别构造**问题编码器**和**文档编码器**（两个编码器结构相同、各自带前缀）。
3. 用 `BiEncoder` 把两个编码器包成双塔，问题向量和文档向量做点积打分、in-batch 交叉熵训练。
4. 训练好后，把文档库预先编码成向量、建 faiss 索引；检索时只需编码问题向量、在索引里找 top-k。

本讲只聚焦第 2 步里的「编码器如何按模式构造」，特别是 `--prefix` 这一支。双塔训练（第 3 步）与检索（第 4 步）是 u7-l2 的内容。

#### 4.1.3 源码精读

README 明确列出五类模式及其训练脚本（[PT-Retrieval/README.md:L73-L85](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/README.md#L73-L85)）：

```text
Five training modes are supported in the code and the corresponding training scripts are provided:
- Fine-tuning
- P-tuning v2
- BitFit
- Adapter
- Lester et al. & P-tuning
```

P-tuning v2 模式的真实启动脚本（[run_train_dpr_multidata_ptv2.sh](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/run_scripts/run_train_dpr_multidata_ptv2.sh)）把超参编码进变量、最后用 `--prefix --pre_seq_len $psl` 打开前缀：

```bash
model_cfg=bert-base-uncased
bs=128; epoch=40; lr=1e-2; psl=64
python3 train_dense_encoder.py \
    --pretrained_model_cfg $model_cfg \
    ...
    --prefix --pre_seq_len $psl
```

注意它用的主干是 `bert-base-uncased`（而非主项目常用的 large），`psl=64`，`lr=1e-2`——和主项目 NER/分类脚本里常见的 `psl=128`、`lr` 量级一致，说明前缀方法对学习率较敏感、需要较大 lr。

五种模式到编码器类的路由发生在 `get_bert_biencoder_components`（[hf_models.py:L27-L47](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/dpr/models/hf_models.py#L27-L47)），这是 PT-Retrieval 的「组件工厂」：

```python
def get_bert_biencoder_components(args, inference_only=False, **kwargs):
    if args.prefix:
        question_encoder = BertPrefixEncoder.init_encoder(args, **kwargs)
        ctx_encoder = BertPrefixEncoder.init_encoder(args, **kwargs)
    elif args.bitfit:
        question_encoder = BertEncoder.init_encoder(args, **kwargs)
        ctx_encoder = BertEncoder.init_encoder(args, **kwargs)
    elif args.adapter:
        question_encoder = BertAdapterEncoder.init_encoder(args, **kwargs)
        ...
    elif args.prompt:
        question_encoder = BertPromptEncoder.init_encoder(args, **kwargs)
        ...
    else:  # 全量微调
        question_encoder = BertEncoder.init_encoder(args, **kwargs)
        ctx_encoder = BertEncoder.init_encoder(args, **kwargs)
    biencoder = BiEncoder(question_encoder, ctx_encoder)
```

可以用一张表把「模式 ↔ 命令行开关 ↔ 编码器类 ↔ 可训练部分」对齐：

| 训练模式 | 开关 | 编码器类 | 实际可训练部分 |
| --- | --- | --- | --- |
| P-tuning v2 | `--prefix` | `BertPrefixEncoder` | 仅 `PrefixEncoder`（+ 投影头） |
| BitFit | `--bitfit` | `BertEncoder` | 仅所有 bias 项 |
| Adapter | `--adapter` | `BertAdapterEncoder` | 注入的 adapter 模块 |
| Lester/P-tuning | `--prompt` | `BertPromptEncoder` | 仅浅层提示 embedding |
| Fine-tuning | （都不加） | `BertEncoder` | 全部参数 |

注意 BitFit 的特殊性：它复用普通的 `BertEncoder`，但在工厂里多调了一句 `_deactivate_relevant_gradients(biencoder, trainable_components=["bias"])`（[hf_models.py:L52-L53](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/dpr/models/hf_models.py#L52-L53)），把所有参数冻结、只放开名字含 `bias` 的参数（[hf_models.py:L379-L390](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/dpr/models/hf_models.py#L379-L390)）。也就是说「训什么」既可以由「换一个模型类」实现（prefix/adapter/prompt），也可以由「同一个模型 + 选择性解冻梯度」实现（bitfit）。

> 与主项目对照：主项目的 `get_model` 只有三种分支（prefix / prompt / 全量微调，见 u2-l3、u3-l2），因为主项目只关心 P-tuning v2 与全量微调的对比；PT-Retrieval 多出 BitFit、Adapter 两种，是为了在检索场景下做更完整的 PEFT 谱系对照实验。

#### 4.1.4 代码实践

**实践目标**：确认 README 列出的五类模式与 `get_bert_biencoder_components` 的五个分支一一对应。

**操作步骤**（源码阅读型）：

1. 打开 [README.md:L73-L80](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/README.md#L73-L80)，记下五类模式名称。
2. 打开 [hf_models.py:L27-L47](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/dpr/models/hf_models.py#L27-L47)，找到五个 `if/elif/else` 分支。
3. 在 [run_scripts/](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/run_scripts/) 目录下确认每种模式都有一个对应的 `.sh`（如 `run_train_dpr_multidata_ptv2.sh`、`..._bitfit.sh` 等）。

**需要观察的现象**：README 的「P-tuning v2」对应代码的 `args.prefix` 分支；「Lester et al. & P-tuning」对应 `args.prompt` 分支；「Fine-tuning」对应 `else` 分支。

**预期结果**：五类模式、五个分支、五个脚本一一对应。本实践为纯阅读，无运行结果。

#### 4.1.5 小练习与答案

**练习 1**：如果同时传了 `--prefix` 和 `--prompt`，会走哪一支？为什么？
**答案**：走 `--prefix`（P-tuning v2）一支。因为 `if args.prefix:` 在最前面，优先级最高，与主项目 `get_model` 中 prefix 优先于 prompt 的约定一致。

**练习 2**：BitFit 为什么不需要像 P-tuning v2 那样新建一个模型子类？
**答案**：BitFit 复用普通 `BertEncoder`，只在工厂里用 `_deactivate_relevant_gradients` 把除 bias 外的梯度关掉；它的「差异」在梯度层面，不在结构层面，所以不必新建模型类。

---

### 4.2 PrefixEncoder 的迁移：`prefix_mlp` 与 `prefix_projection` 的字段差异

#### 4.2.1 概念说明

PT-Retrieval 的 `PrefixEncoder`（[prefix.py:L9-L42](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/dpr/models/prefix.py#L9-L42)）就是从主项目 [model/prefix_encoder.py](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/prefix_encoder.py) 直接搬过来的。回顾 u2-l1：它把固定整数索引 `[0..P-1]` 编码成 `(batch, pre_seq_len, 2*L*H)` 的扁平向量，伪装成所有层的 prefix key/value 拼成的「一整条」。两种实现分支依旧：

- **不加 MLP（默认）**：一张 `Embedding(pre_seq_len, 2*L*H)` 直接查表，参数最少。
- **加 MLP（重参数化）**：`Embedding(pre_seq_len, hidden)` → `Linear(hidden, prefix_hidden) → Tanh → Linear(prefix_hidden, 2*L*H)`，表达力更强、参数更多。

#### 4.2.2 核心流程

迁移时结构几乎不变，但有三个可见差异需要适配：

1. **开关字段改名**：主项目读 `config.prefix_projection`；PT-Retrieval 读 `config.prefix_mlp`。这是本模块最重要的差异。
2. **多了日志**：PT-Retrieval 版本在 `__init__` 里打印 `pre_seq_len`、`prefix_mlp`、`prefix_hidden_size` 三项配置，便于排查。
3. **MLP 的中间维 `prefix_hidden_size`**：两份代码都用它，但 PT-Retrieval 把它显式写进 config（见 4.4.3），主项目则由 `model/utils.py` 写入。

输入输出形状两边完全一致：输入 `(batch, pre_seq_len)`，输出 `(batch, pre_seq_len, 2*L*H)`。

#### 4.2.3 源码精读

先看主项目的版本（[model/prefix_encoder.py:L12-L32](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/prefix_encoder.py#L12-L32)），开关字段是 `prefix_projection`：

```python
def __init__(self, config):
    super().__init__()
    self.prefix_projection = config.prefix_projection          # 主项目用这个字段名
    if self.prefix_projection:
        self.embedding = torch.nn.Embedding(config.pre_seq_len, config.hidden_size)
        self.trans = torch.nn.Sequential(
            torch.nn.Linear(config.hidden_size, config.prefix_hidden_size),
            torch.nn.Tanh(),
            torch.nn.Linear(config.prefix_hidden_size, config.num_hidden_layers * 2 * config.hidden_size)
        )
    else:
        self.embedding = torch.nn.Embedding(config.pre_seq_len, config.num_hidden_layers * 2 * config.hidden_size)
```

再看 PT-Retrieval 的版本（[prefix.py:L17-L42](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/dpr/models/prefix.py#L17-L42)），开关字段是 `prefix_mlp`，且多了日志：

```python
def __init__(self, config):
    super().__init__()
    self.prefix_mlp = config.prefix_mlp                        # PT-Retrieval 改名了
    logger.info(f" > pre_seq_len: {config.pre_seq_len}")       # 多了诊断日志
    logger.info(f" > prefix_mlp: {config.prefix_mlp}")
    logger.info(f" > prefix_hidden_size: {config.prefix_hidden_size}")
    if self.prefix_mlp:
        self.embedding = torch.nn.Embedding(config.pre_seq_len, config.hidden_size)
        self.trans = torch.nn.Sequential(                      # MLP 结构完全相同
            torch.nn.Linear(config.hidden_size, config.prefix_hidden_size),
            torch.nn.Tanh(),
            torch.nn.Linear(config.prefix_hidden_size, config.num_hidden_layers * 2 * config.hidden_size)
        )
    else:
        self.embedding = torch.nn.Embedding(config.pre_seq_len, config.num_hidden_layers * 2 * config.hidden_size)
```

两份 `forward` 也完全同构（[prefix.py:L36-L42](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/dpr/models/prefix.py#L36-L42)）：MLP 开时先查表再过 `self.trans`，否则直接查表。

**迁移要点**：如果你要从主项目迁移新的前缀逻辑到 PT-Retrieval（或反之），必须同时改字段名 `prefix_projection ↔ prefix_mlp`，否则 `config` 上读不到对应属性、会触发 `AttributeError`。MLP 的两层 `Linear` 与 `Tanh`、输出维 `num_hidden_layers * 2 * hidden_size` 都不用动。

#### 4.2.4 代码实践（本讲主实践）

**实践目标**：对比两份 `PrefixEncoder`，列出字段命名与结构异同，并说明迁移适配点。

**操作步骤**：

1. 并排打开 [model/prefix_encoder.py](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/prefix_encoder.py) 与 [PT-Retrieval/dpr/models/prefix.py:L9-L42](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/dpr/models/prefix.py#L9-L42)。
2. 逐行比对 `__init__` 与 `forward`，填下面的对照表。

**参考答案（对照表）**：

| 维度 | 主项目 `model/prefix_encoder.py` | PT-Retrieval `dpr/models/prefix.py` | 是否一致 |
| --- | --- | --- | --- |
| 类名 | `PrefixEncoder` | `PrefixEncoder` | 一致 |
| 输入形状 | `(batch, prefix-length)` | `(batch, prefix-length)` | 一致 |
| 输出形状 | `(batch, prefix-length, 2*layers*hidden)` | 同左 | 一致 |
| MLP 开关字段 | `config.prefix_projection` | `config.prefix_mlp` | **不同，需改名** |
| MLP 结构 | `Linear→Tanh→Linear` | `Linear→Tanh→Linear` | 一致 |
| 默认分支 | `Embedding(P, 2*L*H)` | `Embedding(P, 2*L*H)` | 一致 |
| 诊断日志 | 无 | 有 3 条 `logger.info` | **PT-Retrieval 多出** |
| `prefix_hidden_size` 来源 | `model/utils.py` 写入 config | `hf_models.py` 的 `init_encoder` 写入 | 写入位置不同 |

**迁移时需要适配的点**：

1. 把 `config.prefix_projection` 全部替换为 `config.prefix_mlp`（或反之）。
2. 确认 config 对象上确实被写入了 `prefix_hidden_size`（PT-Retrieval 在 [hf_models.py:L176](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/dpr/models/hf_models.py#L176) 写入），否则 MLP 分支会缺中间维。
3. 其余结构（Embedding 维度、MLP 层、输出维 `2*L*H`）原样保留。

> 本实践为源码阅读型，无运行结果；若要在本地验证字段差异，可参见 4.2.5 的练习 1。

#### 4.2.5 小练习与答案

**练习 1**：如果直接把主项目的 `model/prefix_encoder.py` 拷进 PT-Retrieval 而不改字段名，运行 `--prefix`（默认不加 MLP）时会报什么错？
**答案**：会在 `self.prefix_projection = config.prefix_projection` 处抛 `AttributeError: 'BertConfig' object has no attribute 'prefix_projection'`，因为 PT-Retrieval 只往 config 写了 `prefix_mlp`、没写 `prefix_projection`。修复方法是改名或同时写入两个字段。

**练习 2**：不加 MLP 时，`PrefixEncoder` 的可训练参数量是多少（设 `pre_seq_len=P`、层数 `L`、隐藏维 `H`）？
**答案**：\(P \times 2 \cdot L \cdot H\)，即一张 `Embedding(P, 2*L*H)` 查表矩阵的总元素数；与主项目 u2-l1 的结论一致。

---

### 4.3 BertModelModifed：为什么要改写 forward 才能注入 past_key_values

#### 4.3.1 概念说明

这是本讲最关键、也最容易困惑的一点。在主项目里，`BertPrefixForSequenceClassification.forward` 直接把 `past_key_values` 传给 `self.bert(...)`（依赖 transformers 4.11.3 的 `BertModel` 接受并使用它）。但 PT-Retrieval 用的是 [transformers==4.20.1](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/requirements.txt)，且其代码继承自 Facebook 的 DPR，而 **BERT 本质上是编码器（encoder），标准的 `BertModel.forward` 不会像解码器那样把 `past_key_values` 缓存逐层透传给 `self.encoder`**。于是 PT-Retrieval 不得不新写一个 `BertModelModifed`（[prefix.py:L45-L156](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/dpr/models/prefix.py#L45-L156)），把 `BertModel.forward` 的整段实现抄过来，再加三处与 `past_key_values` 相关的改动。

> 术语提示：`past_key_values` 在 HuggingFace 里原本是「自回归解码器」用来缓存已算过的 key/value、避免重复计算的机制。P-tuning v2 把它「借用」过来——我们并不是真有历史 token，而是把可训练的前缀伪装成历史 key/value，让 BERT 每层注意力都把它拼在自己 key/value 前面。这就是「深度注入」的实现技巧。

#### 4.3.2 核心流程

`BertModelModifed.forward` 相对于标准 `BertModel.forward` 做的三件事：

1. **接收并度量前缀长度**：从 `past_key_values[0][0].shape[2]` 读出前缀长度 `past_key_values_length`，用于在缺省 `attention_mask` 时补足长度。
2. **嵌入层显式 `past_key_values_length=0`**：调用 `self.embeddings(...)` 时把 `past_key_values_length` 写死为 0，告诉嵌入层「真实的输入 token 从位置 0 开始，不要为前缀预留位置编码」。这是因为前缀只在注意力 KV 层面注入，并不作为真实 token 进入嵌入层。
3. **把 `past_key_values` 透传给 `self.encoder`**：这是注入真正生效的一步——只有传进 encoder，每层 `BertLayer`/`BertAttention` 才会拿到前缀 key/value。

伪代码：

```text
def forward(..., past_key_values=None):
    past_len = past_key_values[0][0].shape[2] if past_key_values is not None else 0
    if attention_mask is None:
        attention_mask = ones(batch, seq_len + past_len)   # 为前缀补长度
    extended_attention_mask = get_extended_attention_mask(attention_mask, ...)
    embedding_output = self.embeddings(..., past_key_values_length=0)   # 关键：嵌入选 0
    encoder_outputs = self.encoder(
        embedding_output, attention_mask=extended_attention_mask,
        ..., past_key_values=past_key_values                              # 关键：透传到 encoder
    )
    ...
```

#### 4.3.3 源码精读

`BertModelModifed` 继承自 `BertModel`（[prefix.py:L45-L47](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/dpr/models/prefix.py#L45-L47)），其 `forward` 签名比标准 BERT 多出 `past_key_values` 形参（[prefix.py:L49-L64](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/dpr/models/prefix.py#L49-L64)）。

第一处改动——度量前缀长度并补全缺省 mask（[prefix.py:L88-L92](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/dpr/models/prefix.py#L88-L92)）：

```python
# past_key_values_length
past_key_values_length = past_key_values[0][0].shape[2] if past_key_values is not None else 0
if attention_mask is None:
    attention_mask = torch.ones(((batch_size, seq_length + past_key_values_length)), device=device)
```

第二处改动——嵌入层强制 `past_key_values_length=0`（[prefix.py:L124-L130](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/dpr/models/prefix.py#L124-L130)）：

```python
embedding_output = self.embeddings(
    input_ids=input_ids,
    position_ids=position_ids,
    token_type_ids=token_type_ids,
    inputs_embeds=inputs_embeds,
    past_key_values_length=0,      # 前缀不占嵌入层位置
)
```

第三处改动——把 `past_key_values` 传进 encoder（[prefix.py:L131-L142](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/dpr/models/prefix.py#L131-L142)）：

```python
encoder_outputs = self.encoder(
    embedding_output,
    attention_mask=extended_attention_mask,
    ...,
    past_key_values=past_key_values,   # 真正注入
    use_cache=use_cache,
    ...
)
```

理解这三处改动后，整段看似冗长的 `forward` 其实就是「标准 BERT forward + 三处 past_key_values 处理」。它的存在本身就是一个证据：**要让前缀逐层生效，光在调用方拼好 `past_key_values` 不够，底层编码器的 forward 必须愿意把它透传给每一层注意力。**

#### 4.3.4 代码实践

**实践目标**：定位三处与 `past_key_values` 相关的代码，理解每处的作用。

**操作步骤**（源码阅读型）：

1. 打开 [prefix.py:L45-L156](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/dpr/models/prefix.py#L45-L156)。
2. 找到上面列出的三处（L89、L129、L137）。
3. 思考：如果删掉 L129 的 `past_key_values_length=0`，改成读 `past_key_values_length` 变量，会发生什么？

**需要观察的现象 / 预期结果**：嵌入层若按 `past_key_values_length` 偏移位置编码，真实输入 token 的位置编号会被整体后移 `pre_seq_len`，破坏与预训练位置编码的对齐，导致表征错乱。因此这里必须写死 0。本实践为推理型，**具体数值影响待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `past_key_values_length` 在嵌入层和注意力 mask 里的处理不一样（一个写死 0，一个要加上前缀长度）？
**答案**：嵌入层处理的是「真实输入 token 的位置编码」，前缀不是真实 token、不进嵌入层，所以位置偏移为 0；注意力 mask 处理的是「注意力能看哪些位置」，前缀作为 KV 参与注意力计算，所以 mask 要把前缀那 `pre_seq_len` 个位置也标成可见（补 1）。

**练习 2**：主项目为什么不需要写 `BertModelModifed`？
**答案**：主项目依赖的 transformers 4.11.3 的 `BertModel` 在前缀场景下能接受并使用 `past_key_values`（u2-l2 直接 `self.bert(..., past_key_values=...)`）；而 PT-Retrieval 基于 DPR 代码 + transformers 4.20.1，其 `BertModel` 不会把 `past_key_values` 透传给 encoder，所以必须改写 forward。这是同一思想在不同 transformers 版本/代码基下的适配差异。

---

### 4.4 get_prompt 与 BertPrefixModel：与主项目逐字一致

#### 4.4.1 概念说明

`BertPrefixModel`（[prefix.py:L158-L224](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/dpr/models/prefix.py#L158-L224)）是 PT-Retrieval 里 P-tuning v2 的「裸主干」：它冻结 BERT、挂一个 `PrefixEncoder`、在 forward 里生成前缀并注入。它和主项目的 `BertPrefixForSequenceClassification`（[sequence_classification.py:L101-L176](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/sequence_classification.py#L101-L176)）共享同一套 `get_prompt` 注入逻辑——这正是「深度提示调优任务无关」的最强证据：同一份重排代码，既服务于分类（理解任务），也服务于检索。

两者的关键差别在「头部」：主项目版本带 `classifier` 分类头并在 forward 里算 loss；检索版本**没有分类头**，只返回 BERT 的 `sequence_output`，因为检索的「打分头」是 BiEncoder 里的向量点积，编码器只需吐出 `[CLS]` 向量即可。

#### 4.4.2 核心流程

`BertPrefixModel` 的构造与一次前向：

1. `__init__`：建 `BertModelModifed` 主干 → 冻结主干所有参数 → 记录 `pre_seq_len/n_layer/n_head/n_embd` → 建 `prefix_tokens` 与 `PrefixEncoder`。
2. `get_prompt(batch_size)`：把 `PrefixEncoder` 输出经 `view → permute([2,0,3,1,4]) → split(2)` 重排成逐层 KV，**与主项目逐字相同**。
3. `forward`：生成 `past_key_values` → 在 `attention_mask` 前补 `pre_seq_len` 个 1 → 调 `self.bert(..., past_key_values=...)` → 返回 BERT 输出。

#### 4.4.3 源码精读

构造与冻结（[prefix.py:L158-L175](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/dpr/models/prefix.py#L158-L175)）：

```python
class BertPrefixModel(BertPreTrainedModel):
    def __init__(self, config, add_pooling_layer=True):
        super().__init__(config)
        self.bert = BertModelModifed(config, add_pooling_layer=add_pooling_layer)  # 用改写过的主干
        self.dropout = torch.nn.Dropout(config.hidden_dropout_prob)
        for param in self.bert.parameters():
            param.requires_grad = False        # 冻结主干
        self.pre_seq_len = config.pre_seq_len
        self.n_layer = config.num_hidden_layers
        self.n_head = config.num_attention_heads
        self.n_embd = config.hidden_size // config.num_attention_heads
        self.prefix_tokens = torch.arange(self.pre_seq_len).long()
        self.prefix_encoder = PrefixEncoder(config)
```

`get_prompt`（[prefix.py:L177-L189](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/dpr/models/prefix.py#L177-L189)）——与主项目 [sequence_classification.py:L130-L143](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/sequence_classification.py#L130-L143) **逐字一致**：

```python
def get_prompt(self, batch_size):
    prefix_tokens = self.prefix_tokens.unsqueeze(0).expand(batch_size, -1).to(self.bert.device)
    past_key_values = self.prefix_encoder(prefix_tokens)
    past_key_values = past_key_values.view(
        batch_size, self.pre_seq_len, self.n_layer * 2, self.n_head, self.n_embd
    )
    past_key_values = self.dropout(past_key_values)
    past_key_values = past_key_values.permute([2, 0, 3, 1, 4]).split(2)
    return past_key_values
```

回顾 u2-l2 对这段重排的解释：`view` 靠恒等式 \(2L \cdot n \cdot (H/n) = 2LH\) 把扁平的最后一维拆成 `(层×2, 头数, 头维)`；`permute([2,0,3,1,4])` 把「层×2」维提到最前；`split(2)` 沿「×2」维切成两半，得到长度为 `n_layer` 的 tuple，每段是一层的 `(key, value)`，形状 `(batch, n_head, pre_seq_len, head_dim)`，正是 HuggingFace 逐层 KV 缓存格式。

`forward` 注入（[prefix.py:L206-L222](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/dpr/models/prefix.py#L206-L222)）：

```python
batch_size = input_ids.shape[0]
past_key_values = self.get_prompt(batch_size=batch_size)
prefix_attention_mask = torch.ones(batch_size, self.pre_seq_len).to(self.bert.device)
attention_mask = torch.cat((prefix_attention_mask, attention_mask), dim=1)   # 前缀标为可见
outputs = self.bert(
    input_ids, attention_mask=attention_mask, ...,
    past_key_values=past_key_values,    # 经 BertModelModifed 透传到每层
)
return outputs                          # 注意：返回原始 BERT 输出，无分类头
```

> 与主项目对照（差异）：主项目 `forward` 在拿到 `outputs[1]`（pooled）后接 `self.classifier` 算 logits 与 loss（[sequence_classification.py:L178-L181](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/sequence_classification.py#L178-L181)）；PT-Retrieval 版本没有 classifier，直接返回 BERT 输出。检索时 `[CLS]` 向量由外层 `BertPrefixEncoder.forward` 取 `sequence_output[:, 0, :]` 得到（[hf_models.py:L185-L197](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/dpr/models/hf_models.py#L185-L197)）。

#### 4.4.4 代码实践

**实践目标**：验证 PT-Retrieval 的 `get_prompt` 与主项目完全一致，并看清检索版本为何不带分类头。

**操作步骤**（源码阅读型）：

1. 并排对比 [prefix.py:L177-L189](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/dpr/models/prefix.py#L177-L189) 与 [sequence_classification.py:L130-L143](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/model/sequence_classification.py#L130-L143)，确认逐字相同。
2. 在 [prefix.py:L158-L224](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/dpr/models/prefix.py#L158-L224) 的 `BertPrefixModel` 里搜索 `classifier`，确认它不存在。
3. 看外层 [hf_models.py:L185-L197](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/dpr/models/hf_models.py#L185-L197) 如何取 `[CLS]`：`pooled_output = sequence_output[:, 0, :]`。

**需要观察的现象 / 预期结果**：两份 `get_prompt` 文本相同；检索版无 `self.classifier`，pooler 输出直接当问题/文档向量。本实践为阅读型，无运行结果。

#### 4.4.5 小练习与答案

**练习 1**：既然 `get_prompt` 逐字相同，为什么 PT-Retrieval 不直接复用主项目的 `BertPrefixForSequenceClassification`？
**答案**：两个原因——(1) 主项目版本带分类头并在 forward 算分类 loss，检索不需要；(2) 更根本的是，主项目版本直接 `self.bert = BertModel(config)`，依赖 transformers 4.11.3 的 `BertModel` 支持 `past_key_values`；PT-Retrieval 用 4.20.1 + DPR 代码，必须换成 `BertModelModifed` 才能让前缀透传到每层。所以它需要自己写一个「裸主干 + 前缀」的 `BertPrefixModel`。

**练习 2**：检索场景下，问题向量和文档向量分别从 `BertPrefixModel` 的哪里取？
**答案**：都取 `sequence_output[:, 0, :]`，即序列首 token（`[CLS]`）的隐藏向量；这发生在 `BertPrefixEncoder.forward`（[hf_models.py:L194](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/dpr/models/hf_models.py#L194)）。两个编码器结构相同、各自带前缀，分别编码问题和文档（u7-l2 详述）。

---

## 5. 综合实践

把本讲的三个核心串起来：**研究动机 → 五类模式路由 → 前缀注入**。

**任务**：假设你要为 PT-Retrieval 新增第六种「只训 LayerNorm」的训练模式（类似 BitFit 但放开对象不同），请设计接入方案，回答以下问题：

1. 在 [hf_models.py 的 `get_bert_biencoder_components`](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/dpr/models/hf_models.py#L27-L47) 里加哪个 `elif` 分支？复用哪个编码器类？
2. 参考现有 BitFit 的做法（[L52-L53](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/dpr/models/hf_models.py#L52-L53) + [`_deactivate_relevant_gradients`](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/dpr/models/hf_models.py#L379-L390)），`trainable_components` 该传什么字符串，使只有 LayerNorm 参数被放开？
3. 这种新模式**不需要**改动 `BertModelModifed` 和 `PrefixEncoder`，为什么？

**参考思路**：

1. 加 `elif args.layernorm:` 分支，复用普通 `BertEncoder`（结构上与全量微调相同，差异只在「训什么」），与 BitFit 思路一致。
2. 看 `_deactivate_relevant_gradients` 的判断逻辑是 `if component in name`，而 transformers 里 LayerNorm 参数名通常含 `LayerNorm.weight` / `LayerNorm.bias`，所以传 `trainable_components=["LayerNorm"]` 即可只放开 LayerNorm 相关参数。
3. 因为这种模式是「选择性解冻梯度」，与主干 forward 是否支持 `past_key_values` 无关——它根本不注入前缀，自然不需要 `BertModelModifed`，也不需要 `PrefixEncoder`。只有 `--prefix`（P-tuning v2）这一支才依赖前缀注入机制。

> 本任务为设计型，重在说清「模式差异落在哪一层（结构 / 梯度 / 注入）」；若要真正实现，还需在参数解析处加 `--layernorm` 开关，**完整可运行性待本地验证**。

## 6. 本讲小结

- PT-Retrieval 把 P-tuning v2 从「理解任务」搬到「稠密检索」，核心主张是：参数高效方法让检索器更跨域通用、更校准（README 五类模式 + BEIR/OAG-QA 评测）。
- 五类训练模式在 `get_bert_biencoder_components` 里用五个开关路由：`--prefix`→`BertPrefixEncoder`、`--bitfit`→`BertEncoder`+选择性解冻、`--adapter`→`BertAdapterEncoder`、`--prompt`→`BertPromptEncoder`、其余→全量微调。
- PT-Retrieval 的 `PrefixEncoder` 与主项目**结构完全相同**，唯一关键差异是开关字段名 `prefix_mlp`（PT-Retrieval）vs `prefix_projection`（主项目），迁移时必须改名。
- 为了让前缀逐层生效，PT-Retrieval 新写 `BertModelModifed`，改写 `forward` 把 `past_key_values` 透传给 `self.encoder`，并在嵌入层把 `past_key_values_length` 写死为 0。
- `BertPrefixModel.get_prompt` 与主项目 `get_prompt` **逐字一致**（`view→permute→split`），证明深度提示调优任务无关；差别仅在检索版不带分类头，编码器只吐 `[CLS]` 向量供双塔点积打分。

## 7. 下一步学习建议

本讲只解决了「PT-Retrieval 的 P-tuning v2 编码器怎么构造、前缀怎么注入」。接下来建议：

- **u7-l2 双编码器训练与稠密检索**：精读 `BiEncoder` 双塔、`train_dense_encoder` 的 in-batch 对比学习，以及 `dense_retriever` 如何编码问题向量并经 faiss 检索 top-k。重点关注两个编码器如何**各自**注入前缀。
- **u7-l3 跨域泛化与校准评估**：阅读 BEIR 跨域评测、OAG-QA 主题评测与 calibration 模块的 ECE 计算，理解「为什么参数高效更校准」的量化证据。
- 若你想对比 ColBERT 的前缀集成，可自行阅读 [PT-Retrieval/colbert/colbert/modeling/prefix.py](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/colbert/colbert/modeling/prefix.py)，验证本讲的「迁移需改字段名」结论是否同样适用于 ColBERT 分支。
