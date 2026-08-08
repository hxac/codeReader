# 双编码器训练与稠密检索

## 1. 本讲目标

上一篇（u7-l1）解决了「单个编码器如何接入 P-tuning v2 前缀」——我们让 DPR 里的 BERT 改写成 `BertModelModifed`、并用 `BertPrefixModel` 复用了主项目的 `get_prompt`。本讲把镜头拉远，回答三个问题：

1. **训练阶段**：两个编码器（问题编码器、文档编码器）如何组成 BiEncoder 双塔？它们各自的 prefix 又是怎样被训练的？
2. **打分与损失**：为什么用点积打分、什么是「in-batch negatives」对比学习？
3. **推理阶段**：为什么检索时只需要跑问题编码器，文档向量又如何经 faiss 索引返回 top-k？

学完本讲，你应当能够：

- 画出 BiEncoder 的双塔前向、点积打分与 in-batch 对比损失的数据流；
- 说清 prefix 模式下 `question_model` 与 `ctx_model` 各自独立携带并注入前缀的事实；
- 复述 `train_dense_encoder.py` 的训练主循环与「只保存 prefix 参数」的 checkpoint 策略；
- 解释 `dense_retriever.py` 中「生成问题向量 → faiss 检索 → top-k 校验」的完整推理链路，并标出 prefix 编码器在哪一步被调用。

## 2. 前置知识

本讲默认你已读过 u7-l1，知道 `PrefixEncoder`、`BertModelModifed`、`get_prompt` 的 `view→permute→split` 重排是怎么回事。这里补充几个检索领域的常识：

- **稠密检索（dense retrieval）**：传统检索（如 BM25）靠词频倒排表，是「稀疏」匹配；稠密检索把问题和文档都编码成固定维度的**稠密向量**，用向量距离衡量相关度。优点是能捕捉语义近义（"开心"≈"快乐"），缺点是必须训练编码器。
- **双塔 / 双编码器（bi-encoder / dual-encoder）**：问题塔和文档塔是两个独立的编码器，各自输出一个向量；两者不交叉，只在最后算一次相似度。这恰好让**文档向量可以离线预算并建索引**，查询时只编码问题——这是它能做大规模检索的关键。
- **对比学习与 in-batch negatives**：训练时让「问题 + 它的正解文档」的相似度尽量高、同时压低「问题 + 其他文档」的相似度。把一个 batch 内其他问题的正解文档也当作负样本，就叫 in-batch negatives，能成倍增加负样本数量却不增加额外计算。
- **点积相似度（dot product）**：两个向量逐位相乘再求和。值越大越相关。它等价于「未归一化的余弦」，配合 faiss 的内积索引可直接做最近邻搜索。
- **faiss**：Facebook 开源的高维向量近似最近邻库，用于在百万级文档向量里快速找出与查询向量最相似的 top-k。

> 术语约定：本讲把「问题 / 查询（question / query）」视为同一物，把「文档 / 段落 / 上下文（context / passage / ctx）」视为同一物。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [dpr/models/biencoder.py](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/dpr/models/biencoder.py) | 定义 `BiEncoder` 双塔、`dot_product_scores` 打分、`BiEncoderNllLoss` 对比损失，以及如何把一个 batch 的原始数据组装成双塔输入。 |
| [dpr/models/hf_models.py](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/dpr/models/hf_models.py) | `get_bert_biencoder_components` 是「模型工厂」：按命令行开关创建两个编码器并包进 `BiEncoder`，prefix 模式下两个塔都是 `BertPrefixEncoder`。 |
| [train_dense_encoder.py](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/train_dense_encoder.py) | 训练入口：`BiEncoderTrainer` 负责数据迭代、前向、反向、验证与存盘。 |
| [dense_retriever.py](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/dense_retriever.py) | 检索入口：`DenseRetriever` 编码问题向量、经 faiss 取 top-k、并与正解答案做命中校验。 |
| [generate_dense_embeddings.py](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/generate_dense_embeddings.py) | 离线工具：用文档塔把全部语料编码成向量并落盘，供 `dense_retriever` 建索引。 |
| [dpr/indexer/faiss_indexers.py](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/dpr/indexer/faiss_indexers.py) | faiss 索引封装：`DenseFlatIndexer`（精确内积）与 `DenseHNSWFlatIndexer`（近似、省内存）。 |
| [run_scripts/run_train_dpr_multidata_ptv2.sh](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/run_scripts/run_train_dpr_multidata_ptv2.sh) | P-tuning v2 模式的训练配方脚本。 |

> 以下所有路径均相对仓库根目录，行号对应 HEAD `b1520c9`。

---

## 4. 核心概念与源码讲解

### 4.1 BiEncoder 双塔结构与点积打分

#### 4.1.1 概念说明

`BiEncoder` 是 DPR 的核心组件。它内部不做任何「真正的编码」，只做两件事：**持有两个编码器**（问题塔、文档塔），并在 `forward` 时**分别各跑一次**，吐回两个池化向量。真正的相似度计算和损失放在外部（`BiEncoderNllLoss`）。

这种「双塔不交叉」的设计有一个直接后果：两个塔可以独立运行。于是文档塔可以离线把整个语料预算成向量存盘，查询时只跑问题塔——这正是大规模检索可行的根本原因，也是本讲 4.4 节检索链路成立的前提。

#### 4.1.2 核心流程

双塔前向的流程可概括为：

```text
question_ids ──► question_model ──► q_pooled (batch, D)
context_ids   ──► ctx_model      ──► ctx_pooled (num_ctx, D)
                                            │
                  scores = q_pooled @ ctx_pooledᵀ   # (batch, num_ctx)
                                            │
                  log_softmax + NLL，正解下标为 positive_idx_per_question
```

打分函数选的是点积：

\[ \text{score}(q, c) = q \cdot c = \sum_{d=1}^{D} q_d \, c_d \]

对一个 batch，得到分数矩阵 \( S \in \mathbb{R}^{n \times m}\)，其中 \(n\) 是问题数、\(m\) 是该 batch 内全部候选文档数（每条问题带 1 个正解 + 若干难负例）。对比损失让 \(S\) 中每行「正解那一列」尽量大：

\[ \mathcal{L} = -\frac{1}{n}\sum_{i=1}^{n} \log \frac{\exp(S_{i, p_i})}{\sum_{j} \exp(S_{i,j})} \]

其中 \(p_i\) 是第 \(i\) 个问题的正解文档在该 batch 文档列表中的全局下标。分母里的所有其他文档（含其他问题的正解）都是负样本——这就是 in-batch negatives。

#### 4.1.3 源码精读

**双塔的 `forward`**——只跑两个塔、不做交叉：

[biencoder.py:81-89](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/dpr/models/biencoder.py#L81-L89) 把问题侧与文档侧各自送进 `get_representation`，分别得到 `q_pooled_out` 和 `ctx_pooled_out`，仅返回这两个池化向量。

`get_representation` 里有个细节：当 `fix_encoder=True` 时，前向在 `torch.no_grad()` 下执行，再手动给输出开梯度（[biencoder.py:62-79](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/dpr/models/biencoder.py#L62-L79)）。这是「冻结整个塔」的开关；在 prefix 模式下该开关默认关闭（见 4.2 节），所以两个塔的前缀都会被训练。

**点积打分函数**：

[biencoder.py:33-42](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/dpr/models/biencoder.py#L33-L42) 用一次矩阵乘法 `q_vectors @ ctx_vectors.T` 同时算出全部 \(n \times m\) 个分数。

**对比损失 `BiEncoderNllLoss.calc`**：

[biencoder.py:168-189](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/dpr/models/biencoder.py#L168-L189) 先算分数、`view` 成 `(n, -1)`，再 `log_softmax`、用 `F.nll_loss` 以 `positive_idx_per_question` 为目标下标求平均损失，并顺手统计预测正确的数量。相似度函数固定为 `dot_product_scores`（[biencoder.py:196-198](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/dpr/models/biencoder.py#L196-L198)）。

**把一个 batch 的原始数据组装成双塔输入**：

[biencoder.py:91-163](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/dpr/models/biencoder.py#L91-L163) 对每条样本取 1 个正解 + `num_hard_negatives` 个难负例 + `num_other_negatives` 个普通负例（[biencoder.py:138](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/dpr/models/biencoder.py#L138)），拼成 `all_ctxs`，记录每个正解在全局文档列表里的下标 `positive_ctx_indices`，最终返回 `BiEncoderBatch`（[biencoder.py:28-30](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/dpr/models/biencoder.py#L28-L30)）。这个全局下标就是损失里 `positive_idx_per_question` 的来源。

#### 4.1.4 代码实践

**实践目标**：理解点积打分后分数矩阵的形状，以及 in-batch negatives 的「下标对齐」。

**操作步骤**（示例代码，非项目原有）：

```python
# 示例代码：手工模拟一个 batch 的打分与损失
import torch
import torch.nn.functional as F

n, m, D = 4, 8, 768          # 4 个问题，batch 内共 8 个候选文档
q = torch.randn(n, D)        # 问题向量
c = torch.randn(m, D)        # 文档向量（含 4 个正解 + 4 个难负例）
scores = q @ c.T             # (4, 8)，等价于 dot_product_scores

positive_idx_per_question = [0, 2, 4, 6]   # 每个问题自己的正解在 c 中的下标
logp = F.log_softmax(scores, dim=1)
loss = F.nll_loss(logp, torch.tensor(positive_idx_per_question))
```

**需要观察的现象**：`scores` 形状为 `(4, 8)`；`loss` 是一个标量。

**预期结果**：把 `positive_idx_per_question` 改成「错误下标」时 loss 应明显变大，印证模型在被推向「让正解列得分最高」。

---

### 4.2 prefix 模式下双编码器的构造与各自注入

#### 4.2.1 概念说明

上一篇我们看到单个 `BertPrefixModel` 如何注入前缀。本节回答一个容易被忽略的问题：**BiEncoder 的两个塔，是共用一份前缀，还是各带一份？**

答案在 `get_bert_biencoder_components`：prefix 模式下，问题塔和文档塔是**两个分别 new 出来的 `BertPrefixEncoder` 实例**，各自拥有**独立**的 `PrefixEncoder` 参数。也就是说，模型会为「问题」和「文档」各学一套深层前缀模板。这与「问题/文档语义本就不同」的直觉一致。

#### 4.2.2 核心流程

```text
get_bert_biencoder_components(args)
   ├─ args.prefix 为真？
   │     ├─ question_encoder = BertPrefixEncoder.init_encoder(args)   # 自带 prefix_encoder
   │     └─ ctx_encoder      = BertPrefixEncoder.init_encoder(args)   # 自带另一份 prefix_encoder
   ├─ biencoder = BiEncoder(question_encoder, ctx_encoder)
   └─ 训练时：每个塔 forward 内部各自调用 get_prompt → 注入自己的前缀
```

由于每个 `BertPrefixModel` 的 `__init__` 都把主干 `requires_grad=False`（[prefix.py:166-167](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/dpr/models/prefix.py#L166-L167)），所以「训练整个 BiEncoder」实际只更新：两个塔各自的 `prefix_encoder`（以及可选的 `encode_proj`）。这正是参数高效的体现。

#### 4.2.3 源码精读

**模型工厂的 prefix 分支**：

[hf_models.py:27-50](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/dpr/models/hf_models.py#L27-L50) 当 `args.prefix` 为真，**两次**调用 `BertPrefixEncoder.init_encoder` 分别造出问题塔和文档塔（[hf_models.py:30-31](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/dpr/models/hf_models.py#L30-L31)），再 `BiEncoder(question_encoder, ctx_encoder)` 包起来（[hf_models.py:50](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/dpr/models/hf_models.py#L50)）。两个对象互不共享参数。

**配置如何进入编码器**：`BertPrefixEncoder.init_encoder` 把命令行参数手动「粘」到 config 上——

[hf_models.py:169-183](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/dpr/models/hf_models.py#L169-L183) 写入 `cfg.pre_seq_len`、`cfg.prefix_mlp`、`cfg.prefix_hidden_size`，与主项目 `get_model` 的「config 粘贴」做法一致（参见 u3-l1）。注意此处开关字段名是 `prefix_mlp`，与主项目的 `prefix_projection` 不同（u7-l1 已对比）。

**前缀在每个塔的 forward 内被独立注入**：`BertPrefixEncoder.forward` 继承自 `BertPrefixModel`，先 `get_prompt` 生成 `past_key_values`、补齐 `prefix_attention_mask`，再带着前缀调用改写过的 `BertModelModifed`（[prefix.py:191-224](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/dpr/models/prefix.py#L191-L224)）。`get_prompt` 的 `view→permute([2,0,3,1,4])→split(2)` 与主项目逐字相同（[prefix.py:177-189](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/dpr/models/prefix.py#L177-L189)），证明深度提示调优的任务无关性——它从「分类」搬到了「检索」，注入机制一行未改。

**输出向量**：检索只用到池化向量。`BertPrefixEncoder.forward` 取 `sequence_output[:, 0, :]`（即 `[CLS]`）作为该塔的向量（[hf_models.py:185-197](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/dpr/models/hf_models.py#L185-L197)），供双塔点积。

#### 4.2.4 代码实践

**实践目标**：验证「两个塔各带独立 prefix」。

**操作步骤**（源码阅读型）：在 [hf_models.py:27-50](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/dpr/models/hf_models.py#L27-L50) 的 prefix 分支里，`question_encoder` 与 `ctx_encoder` 是两次独立 `init_encoder` 调用。请在脑中（或本地 `python -c` 实例化后）确认：

```python
# 示例代码（思路验证，需在 PT-Retrieval 目录、装好依赖后运行）
# q_enc.prefix_encoder 与 c_enc.prefix_encoder 是两个不同对象
print(q_enc.prefix_encoder is c_enc.prefix_encoder)   # 预期 False
```

**预期结果**：`False`，即两份前缀参数彼此独立。结合 [train_dense_encoder.py:398-399](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/train_dense_encoder.py#L398-L399) 可知，存盘时 `question_model.prefix_encoder.*` 与 `ctx_model.prefix_encoder.*` 会分别保留。

> 待本地验证：上述实例化依赖 `bert-base-uncased` 权重与 conda `pt-retrieval` 环境。

---

### 4.3 train_dense_encoder 训练入口

#### 4.3.1 概念说明

`train_dense_encoder.py` 是 DPR 训练的「主程序」。它不继承 HuggingFace Trainer（与主项目的 `BaseTrainer` 不同），而是自写了一个 `BiEncoderTrainer` 类来掌控训练循环。这套循环要做：读 JSON 数据 → 组装双塔 batch → 前向算 in-batch 损失 → 反向 → 验证 → 存盘。

值得专门拎出来的一点是它的 **checkpoint 策略**：prefix / prompt 模式下，存盘时只挑名字里含 `prefix_encoder` 的参数（[train_dense_encoder.py:398-399](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/train_dense_encoder.py#L398-L399)）。这把「参数高效」贯彻到了产物体积——一个 checkpoint 只剩几十 MB 的前缀，而不是几百 MB 的整塔。

#### 4.3.2 核心流程

```text
main()
 ├─ 解析参数、建 GPU、设随机种子
 ├─ trainer = BiEncoderTrainer(args)
 │     ├─ init_encoder_components("dpr", args)  → tensorizer, model(BiEncoder), optimizer
 │     └─ setup_for_distributed_mode(...)        → 分布式/fp16 包装
 ├─ trainer.run_train()
 │     ├─ get_data_iterator(train_file)          → ShardedDataIterator（含上采样 trec/webq×4）
 │     └─ for epoch: _train_epoch(...)
 │            for each batch:
 │              create_biencoder_input → _do_biencoder_fwd_pass → loss.backward → optimizer.step
 │            每个 epoch 末: validate_and_save（NLL 或 average-rank 验证 + 存盘）
 └─ （无 train_file 时）仅对给定 model_file 跑两种验证
```

#### 4.3.3 源码精读

**构造与初始化**：

[train_dense_encoder.py:52-88](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/train_dense_encoder.py#L52-L88) 记录 `prefix/prompt/adapter` 三个模式开关（L55-57），通过 `init_encoder_components("dpr", args)` 拿到 `tensorizer, model, optimizer`（L72）——`"dpr"` 路由到 `get_bert_biencoder_components`，于是 prefix 开关在此处生效。

**数据迭代与多数据集上采样**：

[train_dense_encoder.py:90-114](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/train_dense_encoder.py#L90-L114) 用 `glob` 收集多个训练文件；若文件名含 `trec` 或 `webq`，则上采样 4 倍（L96-101），因为这些小数据集需要更多曝光。随后过滤掉没有正解文档的样本（L107），再交给 `ShardedDataIterator`。

**单次前向 `_do_biencoder_fwd_pass`**：

[train_dense_encoder.py:510-539](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/train_dense_encoder.py#L510-L539) 构造 attention mask、跑模型、用 `BiEncoderNllLoss` 算损失。关键一步是 `_calc_loss`：

[train_dense_encoder.py:455-507](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/train_dense_encoder.py#L455-L507) 即「in-batch negatives schema loss」。单机时直接用本卡的 `q_vector`/`ctx_vectors`；多卡（DDP）时用 `all_gather_list` 把各卡的表示汇总，并把各卡正解下标按累加的文档数偏移（L486、L491），从而让一张卡的问题能以其它卡上的文档为负样本——这是把 in-batch negatives 扩展到「in-global-batch」。

**训练主循环 `_train_epoch`**：

[train_dense_encoder.py:317-388](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/train_dense_encoder.py#L317-L388) 每个 batch 组装输入、前向、反向、梯度裁剪、按 `gradient_accumulation_steps` 累积后 `optimizer.step()`+`scheduler.step()`（L362-365），并在 epoch 末调用 `validate_and_save`（L384）。

**验证与存盘 `validate_and_save`**：

[train_dense_encoder.py:154-174](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/train_dense_encoder.py#L154-L174) 在 `val_av_rank_start_epoch` 之前用 `validate_nll`（快、基于损失），之后切换到 `validate_average_rank`（慢、但更贴近真实检索质量，[train_dense_encoder.py:211-315](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/train_dense_encoder.py#L211-L315)）。仅在验证指标创新低时记为 best（L171-174）。

**只存前缀的 checkpoint**：

[train_dense_encoder.py:390-416](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/train_dense_encoder.py#L390-L416) 当 `self.prefix or self.prompt` 为真，`model_dict` 只保留 key 含 `"prefix_encoder"` 的项（L398-399）；adapter 模式另走 `save_adapter`；其余（全量微调）才存整份 `state_dict`。加载侧 `_load_saved_state` 因此用 `strict=not self.prefix and not self.prompt`（[train_dense_encoder.py:435](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/train_dense_encoder.py#L435)），允许主干权重缺失。

**训练配方**：

[run_scripts/run_train_dpr_multidata_ptv2.sh](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/run_scripts/run_train_dpr_multidata_ptv2.sh) 用 `bs=128, epoch=40, lr=1e-2, psl=64`，主干 `bert-base-uncased`，`--hard_negatives 1`（每问 1 个难负例），从第 30 个 epoch 起切换 average-rank 验证，末尾 `--prefix --pre_seq_len $psl` 打开 P-tuning v2。注意 `lr=1e-2` 远高于全量微调常用的 2e-5——这正是参数高效方法的特点：可训练参数少，需要更大的学习率才能有效更新（呼应主项目 search_script 的超参敏感性，见 u5-l2）。

#### 4.3.4 代码实践

**实践目标**：理解「只存前缀」对产物体积的影响。

**操作步骤**（源码阅读型）：

1. 阅读 [train_dense_encoder.py:398-399](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/train_dense_encoder.py#L398-L399) 与 [train_dense_encoder.py:404-405](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/train_dense_encoder.py#L404-L405)，对比 prefix 模式与全量微调模式下 `model_dict` 的来源差异。
2. 在脑中估算：`bert-base-uncased`（约 1.1 亿参数）单塔约 440MB（fp32），双塔约 880MB；而 prefix 模式只存两份 `prefix_encoder`（每份约 `pre_seq_len × 2 × layers × hidden` ≈ 64×2×12×768 ≈ 118 万 参数，约 4.7MB）。

**预期结果**：prefix checkpoint 仅数 MB 级别，远小于全量微调的数百 MB，印证「参数高效」不仅省显存，也省存储与分发成本。

> 待本地验证：实际体积需训练后用 `ls -lh checkpoints/` 核对。

---

### 4.4 DenseRetriever 编码与 faiss 检索

#### 4.4.1 概念说明

训练完拿到 checkpoint 后，真正的「检索」由 `dense_retriever.py` 完成。它体现双塔设计的红利：**文档向量早已离线算好建进 faiss 索引，检索时只编码问题向量**，再做一次近邻搜索就得到 top-k。

这里要分清两个互补的脚本：

- `generate_dense_embeddings.py`（离线）：用**文档塔** `ctx_model` 把整个语料（如 Wikipedia 的 2100 万段落）编码成向量，分片落盘成 `.pkl`。
- `dense_retriever.py`（在线）：用**问题塔** `question_model` 编码查询，在 faiss 索引里取 top-k，再用字符串/正则匹配判断命中答案。

注意：两个脚本都加载同一个 checkpoint，但分别取 `ctx_model` 与 `question_model` 两套权重——因为 4.2 节说过，两塔的 prefix 是各自独立的。

#### 4.4.2 核心流程

```text
【离线 · generate_dense_embeddings.py】
语料 TSV ──► ctx_model(prefix) ──► (doc_id, doc_vector) ──► 落盘 .pkl（可分片）

【在线 · dense_retriever.py main()】
1. 加载 checkpoint，只取 question_model（含其 prefix_encoder）
2. 把所有 .pkl 文档向量灌进 faiss 索引（IndexFlatIP / HNSW）
3. 读 qa_file 得到 (question, answers) 列表
4. generate_question_vectors(questions)
      └─ text_to_tensor → question_encoder(含 prefix) → 取 [CLS] 池化向量
5. get_top_docs(query_vectors, top_k)
      └─ index.search_knn → 每问返回 (doc_ids, scores)
6. validate：用 answers 在 top-k 文档里做命中匹配，算 top-k accuracy
```

#### 4.4.3 源码精读

**生成问题向量 `generate_question_vectors`**：

[dense_retriever.py:54-82](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/dense_retriever.py#L54-L82) 按 `batch_size` 切片，对每条问题用 `tensorizer.text_to_tensor` 转 token id（L64-65），堆叠后构造 `attn_mask`，送进 `self.question_encoder` 取返回三元组的第二个（池化向量 `out`，L70），逐条切分后拼回 `(num_questions, D)`。**前缀正是在 L70 这次 `question_encoder(...)` 前向里被注入的**——`question_encoder` 是 `BertPrefixEncoder`，其 forward 内部会调用 `get_prompt` 生成 `past_key_values` 并拼到冻结主干前（见 4.2 节）。

**faiss 取 top-k `get_top_docs`**：

[dense_retriever.py:84-94](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/dense_retriever.py#L84-L94) 把查询向量交给 `self.index.search_knn`，返回每问的 `(doc_ids, scores)`。底层两种索引：

- [faiss_indexers.py:91-109](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/dpr/indexer/faiss_indexers.py#L91-L109) `DenseFlatIndexer` 用 `faiss.IndexFlatIP`（精确内积），`search_knn` 调 `self.index.search` 后把内部下标映射回文档 id。
- [faiss_indexers.py:112-181](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/dpr/indexer/faiss_indexers.py#L112-L181) `DenseHNSWFlatIndexer` 用 HNSW 近似图，因 HNSW 只支持 L2，需把内积转成 L2 空间（多一维 `aux_dim`，L134-146、L167-176）。

**`main` 的总装**：

[dense_retriever.py:184-265](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/dense_retriever.py#L184-L265) 是完整推理链。关键几处：

- 只取问题塔：`encoder = encoder.question_model`（[dense_retriever.py:192](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/dense_retriever.py#L192)）。
- 加载 prefix 权重：当 `--prefix` 时，从 checkpoint 里挑出 `question_model.prefix_encoder.*` 的键并裁掉前缀名，用 `strict=False` 载入（[dense_retriever.py:208-212](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/dense_retriever.py#L208-L212)）——与训练侧「只存 prefix」互为表里。
- 建索引：按 `--hnsw_index` 选 HNSW 或 Flat，先尝试反序列化已存索引，否则现场灌数据（[dense_retriever.py:216-235](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/dense_retriever.py#L216-L235)）。
- 编码 + 检索 + 校验：`generate_question_vectors`（L246）→ `get_top_docs`（L249）→ `validate`（L259）。

**离线文档编码**（补充链路另一端）：

[generate_dense_embeddings.py:41-70](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/generate_dense_embeddings.py#L41-L70) 用 `ctx_model` 给每个段落算池化向量，与 `(doc_id)` 配对；`main` 里 `encoder = encoder.ctx_model`（[generate_dense_embeddings.py:81](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/generate_dense_embeddings.py#L81)），prefix 权重同样从 `ctx_model.prefix_encoder.*` 裁取（[generate_dense_embeddings.py:98-105](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/generate_dense_embeddings.py#L98-L105)）。支持分片（`--shard_id/--num_shards`，L115-120）以并行加速。

**实际调用命令**（来自 eval 脚本）：

[eval_scripts/generate_wiki_embeddings.sh](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/eval_scripts/generate_wiki_embeddings.sh) 用 8 个 shard 并行生成 Wikipedia 向量；[eval_scripts/evaluate_on_openqa.sh](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/eval_scripts/evaluate_on_openqa.sh) 调 `dense_retriever.py`，按数据集（nq/trivia/webq/curatedtrec）选择 `--match string|regex`，并带 `--prefix --pre_seq_len 64`。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：画出从原始问题文本到 top-k 文档的完整数据流，并标注 prefix 编码器的调用位置。

**操作步骤**：

1. 阅读 [dense_retriever.py:54-82](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/dense_retriever.py#L54-L82)（`generate_question_vectors`）与 [dense_retriever.py:84-94](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/dense_retriever.py#L84-L94)（`get_top_docs`）。
2. 画出下面这张数据流图（请在你自己的笔记里补全箭头）：

   ```text
   原始问题文本 List[str]
        │  tensorizer.text_to_tensor(q)            # dense_retriever.py:64-65
        ▼
   q_ids_batch (B, seq_len)
        │  question_encoder(q_ids, q_seg, q_attn_mask)   # dense_retriever.py:70  ★prefix 在此注入★
        ▼                                          #   └─ get_prompt → past_key_values → 拼到冻结 BERT
   out = [CLS] 池化向量 (B, D)
        │  torch.cat → (N, D)
        ▼
   query_vectors.numpy()
        │  index.search_knn(query_vectors, top_k)  # dense_retriever.py:92 → faiss_indexers.py:104
        ▼
   [(doc_ids[...], scores[...]), ...]   每问一组 top-k
        │  validate(...)                        # dense_retriever.py:106-116 命中答案匹配
        ▼
   top_k_hits accuracy
   ```

3. 在图中用 ★ 标出 **prefix 编码器被调用的那一行**：即 `dense_retriever.py:70` 的 `self.question_encoder(...)`。前缀并非显式出现，而是封装在该 `BertPrefixEncoder.forward` 内部的 `get_prompt` 调用中。

**需要观察的现象**：若把 `--prefix` 去掉（退回全量微调编码器），数据流形状完全不变，只是 L70 的编码器换成了不带 `get_prompt` 的普通 `BertEncoder`。

**预期结果**：你能清楚说出「问题向量在第 70 行产生、文档向量在离线脚本里产生、两者在 faiss 内积索引里相遇」这三段分工，并指出 prefix 只参与「问题向量生成」这一在线步骤（离线文档编码同理用 ctx 塔的 prefix）。

> 待本地验证：完整运行需下载 Wikipedia 语料与训练好的 checkpoint（见 README「Checkpoints for Reproduce」），无 GPU 时可只做源码阅读与画图。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `dense_retriever.py` 的 `main` 里取 `encoder.question_model`，而 `generate_dense_embeddings.py` 取 `encoder.ctx_model`？

> **参考答案**：检索在线阶段只需编码查询，故取问题塔；文档向量是离线批量预算并建索引的，由文档塔负责。这正是双塔「不交叉」带来的可分工性。

**练习 2**：`DenseFlatIndexer` 与 `DenseHNSWFlatIndexer` 分别用什么 faiss 索引？HNSW 为什么需要多算一维 `aux_dim`？

> **参考答案**：Flat 用 `IndexFlatIP`（精确内积）；HNSW 用 `IndexHNSWFlat`，但其仅支持 L2，故用 `aux_dim = sqrt(phi - ||v||²)`（`phi` 为全局最大范数平方）把内积空间转换到 L2 空间，使「内积最近邻」等价为「L2 最近邻」。

**练习 3**：若训练时 `--prefix` 但检索时忘记带 `--prefix`，会发生什么？

> **参考答案**：`dense_retriever.py:208` 的 `if args.prefix or args.prompt` 不成立，加载时不会从 `question_model.prefix_encoder.*` 取权重，而是按全量微调方式加载，导致前缀参数缺失/错配，检索质量严重退化。两端的模式开关必须一致。

---

## 5. 综合实践

把本讲三块内容串起来，完成一次「端到端走查」：

**任务**：给定 P-tuning v2 训练命令

```bash
bash run_scripts/run_train_dpr_multidata_ptv2.sh
```

请按下面四个检查点逐一回答，并在笔记里画出一张总图：

1. **构造**：`get_bert_biencoder_components` 在 prefix 分支创建了哪两个对象？它们各自携带了什么可训练参数？（对应 4.2）
2. **训练**：`_do_biencoder_fwd_pass` → `_calc_loss` 中，单卡时分数矩阵的形状是 `(batch_size, ?)`，`?` 由哪些量决定？损失为何能自动利用其他问题的正解作负样本？（对应 4.1、4.3）
3. **存盘**：epoch 末 `_save_checkpoint` 在 prefix 模式下只保留哪些 key？这对加载侧的 `strict` 参数提出了什么要求？（对应 4.3）
4. **检索**：训练产物如何分别喂给 `generate_dense_embeddings.py`（取 `ctx_model`）与 `dense_retriever.py`（取 `question_model`）？两者加载前缀权重的代码模式是否对称？（对应 4.4）

**预期产出**：一张包含「训练循环 ↔ checkpoint（仅 prefix）↔ 离线文档编码 ↔ 在线问题编码 ↔ faiss 检索」五块拼图的总览图，并能指出 prefix 参数贯穿其中的位置——它出现在双塔各自的 `get_prompt`、被独立训练、被独立存取、被独立加载到问题塔与文档塔。

> 待本地验证：若条件允许，用 README 提供的 checkpoint 跑 `evaluate_on_openqa.sh nq 5`，观察日志里 `Encoded queries`、`index search time`、`top k documents hits accuracy` 三类输出，与上图一一对照。

## 6. 本讲小结

- **BiEncoder 是双塔不交叉**：问题塔与文档塔各跑一次，只在最后用点积打分；这种结构让文档向量可离线预算、查询时只编码问题，是大规检索可行的关键。
- **打分与损失**：`dot_product_scores` 一次矩阵乘得到 `(n, m)` 分数矩阵，`BiEncoderNllLoss` 以正解全局下标为目标做 in-batch negatives 对比学习；多卡时 `_calc_loss` 用 `all_gather` 扩成 global in-batch。
- **双塔各带独立 prefix**：prefix 模式下 `get_bert_biencoder_components` new 出两个 `BertPrefixEncoder`，各自有独立的 `PrefixEncoder`；注入机制 `view→permute→split` 与主项目分类任务逐字相同，再次印证深度提示调优的任务无关性。
- **训练入口自管循环**：`BiEncoderTrainer` 不继承 HF Trainer，自管数据迭代、前向、反向、验证（NLL→average-rank）；多数据集对小集（trec/webq）上采样 4 倍。
- **checkpoint 只存 prefix**：prefix/prompt 模式仅保留 `prefix_encoder.*`，加载侧 `strict=False`，产物体积从数百 MB 降到数 MB。
- **检索链路分工明确**：`generate_dense_embeddings.py` 用 `ctx_model` 离线编码语料建 faiss 索引；`dense_retriever.py` 用 `question_model` 在线编码查询、`search_knn` 取 top-k、`validate` 算命中；prefix 编码器在「编码问题向量」（`dense_retriever.py:70`）这一步被调用。

## 7. 下一步学习建议

- **下一讲 u7-l3（跨域泛化与校准评估）**：本讲只解决了「怎么训练、怎么检索」；u7-l3 将把这套检索器放到 BEIR（15 个跨域数据集）与 OAG-QA（87 个主题）上评测，并用 ECE 校准指标解释「为什么 prefix 调优比全量微调更跨域、更校准」。建议先记住本讲「prefix 只动很少参数」这一事实——它是下一讲结论的直接成因。
- **延伸阅读源码**：
  - `dpr/utils/data_utils.py` 的 `Tensorizer`、`ShardedDataIterator`、`normalize_question`，了解分词与分布式数据切分细节；
  - `dpr/data/qa_validation.py` 的 `calculate_matches`，了解 top-k 命中（string/regex）如何统计；
  - `colbert/` 目录，对照另一种检索范式（晚交互模型）如何同样接入 P-tuning v2。
