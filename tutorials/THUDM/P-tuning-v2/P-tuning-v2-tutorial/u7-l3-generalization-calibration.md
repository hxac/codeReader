# 跨域泛化与校准评估

## 1. 本讲目标

PT-Retrieval 子项目（论文 *Parameter-Efficient Prompt Tuning Makes Generalized and Calibrated Neural Text Retrievers*, Findings of EMNLP 2023）的核心主张不只是「参数高效、省显存」，而是两条更强的论断：

1. **跨域/跨主题泛化更好**：把一个在 NQ/TriviaQA 等 OpenQA 数据上训练好的检索器，直接拿到从未见过的领域（新闻、生物医学、科研论文）去检索，P-tuning v2 比全量微调掉分更少。
2. **更校准（calibrated）**：检索器给出的相关度分数（经 softmax 后）与「该文档是否真的相关」的真实概率更吻合。

本讲解决一个问题：**这两条论断在代码里是如何被「评测」出来的？** 学完后你应该能够：

- 说清 PT-Retrieval 支持的三类评测数据（OpenQA / BEIR / OAG-QA）各自的脚本入口与评测流程。
- 跟踪 BEIR 评测如何把一个带 prefix 的 DPR 检索器包装进 BEIR 框架，并产出 NDCG/MAP/Recall/Precision。
- 跟踪 OAG-QA 评测如何「按主题分别评测再平均」，理解 `--oagqa` 开关为何要改命中判定的字段。
- 手算 ECE（Expected Calibration Error），读懂校准曲线，并解释为什么 prefix 调优在跨域检索上可能更校准。

## 2. 前置知识

本讲依赖 u7-l1（PT-Retrieval 的 DPR 前缀集成）与 u7-l2（BiEncoder 双塔训练与稠密检索）。在进入评测前，先建立三个概念。

### 2.1 跨域（cross-domain）与零样本检索

稠密检索（dense retrieval）用一个编码器把问题和文档都映射成向量，用点积或余弦衡量相关性。训练时往往只能在一个领域（如维基百科问答）标注数据。**跨域评测**指：训练域与评测域**不同**，且评测时**不再微调**。BEIR 就是为这种「零样本跨域」设计的基准，涵盖新闻、科学、生物医学等十几个异构数据集。如果一种方法在训练域表现好、在跨域评测掉分少，就说明它「泛化好」。

### 2.2 校准（calibration）与置信度

一个分类器（这里把「top-k 文档是否相关」当作二分类）输出概率 \(\hat{p}\)。**校准**要求：把所有预测概率落在 \([0.1, 0.2)\) 区间内的样本拿出来，其中真正为正的比例应当约等于 0.15。换言之，**模型给出的置信度应当与经验准确率一致**。一个过拟合的模型常表现为「过度自信」——给一堆实际不相关的文档也打出高分，校准就差。

### 2.3 ECE（Expected Calibration Error，期望校准误差）

把预测概率切成 \(M\) 个等宽桶（bin），对每个桶算「经验准确率」与「平均置信度」之差，再按桶内样本量加权求和：

\[
\text{ECE} = \sum_{b=1}^{M} \frac{n_b}{N}\,\bigl|\,\text{acc}(b) - \text{conf}(b)\,\bigr|
\]

其中 \(n_b\) 是落入桶 \(b\) 的样本数，\(N\) 是总样本数，\(\text{acc}(b)\) 是该桶内正例比例（经验准确率），\(\text{conf}(b)\) 是该桶内预测概率的均值（平均置信度）。ECE 越小越校准。本讲 4.3 会用项目源码逐行对应这个公式。

> 术语提示：下面会反复出现 `past_key_values`、`prefix_encoder`、双塔 `question_model`/`ctx_model`、点积打分等概念，它们在前两讲已经建立，本讲默认你已熟悉，只聚焦「评测」这一动作。

## 3. 本讲源码地图

本讲全部位于 `PT-Retrieval/` 子目录下，文件按「评测入口」与「评测内核」两类组织。

| 文件 | 作用 |
| --- | --- |
| [PT-Retrieval/README.md](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/README.md) | 三类评测数据与对应脚本命令的「说明书」 |
| [PT-Retrieval/beir_eval/evaluate_dpr_on_beir.py](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/beir_eval/evaluate_dpr_on_beir.py) | BEIR 跨域评测主脚本 |
| [PT-Retrieval/beir_eval/models.py](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/beir_eval/models.py) | `DPRForBeir` 适配器：把带 prefix 的 DPR 双塔包装成 BEIR 的稠密检索接口 |
| [PT-Retrieval/dense_retriever.py](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/dense_retriever.py) | OpenQA / OAG-QA 通用检索脚本；`--oagqa` 开关在此生效 |
| [PT-Retrieval/dpr/data/qa_validation.py](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/dpr/data/qa_validation.py) | 命中判定 `check_answer`；`index` 参数控制匹配哪个字段 |
| [PT-Retrieval/batch_dense_retrieval.py](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/batch_dense_retrieval.py) | OAG-QA 批量「逐主题评测再平均」 |
| [PT-Retrieval/calibration/calibration_ece_openqa.py](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/calibration/calibration_ece_openqa.py) | OpenQA 上的 ECE 计算入口 |
| [PT-Retrieval/calibration/utils.py](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/calibration/utils.py) | `calibration_curve_with_ece`（ECE 内核）、`calculate_ece`（BEIR 版）、`plot_calibration_curve`（画曲线） |
| `PT-Retrieval/eval_scripts/*.sh` | 三类评测的命令行封装 |

整条评测链路的分工是：**脚本（`.sh`）→ 内核脚本（`.py`）→ 复用前缀编码器 + BEIR/校准库**。前缀编码器如何在 `encode_queries`/`encode_corpus` 内部被调用，已在 u7-l1、u7-l2 讲过；本讲只看「评测如何组织、指标如何算」。

## 4. 核心概念与源码讲解

### 4.1 BEIR 跨域评测

#### 4.1.1 概念说明

BEIR 是一个**异构信息检索基准**，包含 18 个来自不同领域的数据集（如 NFCorpus 医学、SciFact 科学、FiQA 金融、ArguAna 论辩等）。PT-Retrieval 用它的目的很明确：训练只在 OpenQA 的问答对上做，评测却换到 BEIR 的多个领域，**全程不再微调**。如果一种方法跨域掉分少，就说明它没有把模型「焊死」在训练域上——这正是「参数高效 → 跨域泛化好」论断的检验场。

BEIR 的统一接口要求检索器提供两个方法：`encode_queries(queries)` 与 `encode_corpus(corpus)`，分别把问题列表和文档列表编码成向量矩阵；随后 BEIR 用向量相似度（本项目固定为 `dot` 点积）召回，并用官方 `qrels`（相关性标注）算 NDCG、MAP、Recall、Precision。

#### 4.1.2 核心流程

BEIR 评测可拆成 4 步：

1. **加载数据**：从 `beir_eval/datasets/<dataset>/` 读取 `corpus`、`queries`、`qrels`；若目录不存在则自动下载并解压。
2. **包装模型**：用 `DPRForBeir(args)` 构造一个带 prefix 的 DPR 双塔，再套一层 `DRES`（BEIR 的「精确稠密检索」包装器）。
3. **召回**：`retriever.retrieve(corpus, queries)` 内部对全部问题与文档编码，做点积相似度，返回每个问题的文档打分字典。
4. **打分**：`retriever.evaluate(qrels, results, k_values)` 对照官方标注算 NDCG@k、MAP@k、Recall@k、Precision@k。

伪代码：

```
model = DRES(DPRForBeir(args))            # 带 prefix 的双塔 + BEIR 包装
corpus, queries, qrels = GenericDataLoader(data_path).load(split)
retriever = EvaluateRetrieval(model, score_function="dot")
results = retriever.retrieve(corpus, queries)        # 召回
ndcg, _map, recall, precision = retriever.evaluate(qrels, results, k_values)  # 算分
```

#### 4.1.3 源码精读

入口脚本 [beir_eval/evaluate_dpr_on_beir.py](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/beir_eval/evaluate_dpr_on_beir.py) 极其精简，几乎就是上面 4 步的直译。关键几处：

参数解析与模型构造（[evaluate_dpr_on_beir.py:41-48](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/beir_eval/evaluate_dpr_on_beir.py#L41-L48)）：`--prefix --pre_seq_len` 等开关由 `add_tuning_params` 注册，`DPRForBeir(args)` 内部据此走 prefix 分支；`args.dpr_mode=False` 等三个赋值是把不相关的默认开关显式关掉。

```python
args = parser.parse_args()
args.dpr_mode = False
args.fix_ctx_encoder = False
args.use_projection = False
model = DRES(DPRForBeir(args), batch_size=args.batch_size)
```

数据自动下载与加载（[evaluate_dpr_on_beir.py:52-68](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/beir_eval/evaluate_dpr_on_beir.py#L52-L68)）：先拼出本地路径，不存在就调用 BEIR 的 `util.download_and_unzip` 下载官方压缩包，再用 `GenericDataLoader` 读三件套。

打分调用（[evaluate_dpr_on_beir.py:70-78](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/beir_eval/evaluate_dpr_on_beir.py#L70-L78)）：`score_function="dot"` 锁定点积（与 BiEncoder 训练目标一致）；`retrieve` 返回的 `results` 与 `qrels` 同构，于是 `evaluate` 直接对照算 4 个指标族。

真正承载「prefix + 双塔」逻辑的是适配器 [beir_eval/models.py](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/beir_eval/models.py) 的 `DPRForBeir` 类。它的 `__init__`（[models.py:19-65](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/beir_eval/models.py#L19-L65)）分别构造并加载 `question_model` 与 `ctx_model`，加载时显式处理了 prefix 的权重前缀：

```python
encoder_name = "question_model."
if args.prefix or args.prompt:
    encoder_name += "prefix_encoder."
q_state = {key[prefix_len:]: value for (key, value) in saved_state.model_dict.items()
           if key.startswith(encoder_name)}
model_to_load.load_state_dict(q_state, strict=not args.prefix and not args.prompt)
```

含义：训练时两个塔各自只保存了 `prefix_encoder.*`（u7-l2 已说明产物仅数 MB），所以这里把 state_dict 的键名剥掉前缀后、用 `strict=False` 装回冻结的 BERT 主干——主干权重来自预训练，只有 prefix 是新学的。两个编码方法 `encode_queries`（[models.py:67-81](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/beir_eval/models.py#L67-L81)）与 `encode_corpus`（[models.py:83-100](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/beir_eval/models.py#L83-L100)）就是「分词 → 前向 → 取 [CLS] 向量」，prefix 注入发生在前向内部（`get_prompt`），对 BEIR 完全透明。

封装脚本 [eval_scripts/evaluate_on_beir.sh](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/eval_scripts/evaluate_on_beir.sh)（[L14-L20](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/eval_scripts/evaluate_on_beir.sh#L14-L20)）给出真实命令：`--prefix --pre_seq_len 100`，并按数据集选择 `split`（msmarco 用 `dev`，其余用 `test`）。

#### 4.1.4 代码实践

**实践目标**：在不实际跑模型的前提下，把 BEIR 评测的「输入 → 输出」对清楚。

**操作步骤**：

1. 打开 [evaluate_dpr_on_beir.py](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/beir_eval/evaluate_dpr_on_beir.py)，对照 4.1.2 的 4 步，给第 41–78 行逐行标注属于哪一步。
2. 查阅 [README.md:131-137](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/README.md#L131-L137) 的 BEIR 段，写出在 `scifact` 数据集上评测的完整命令（需指定 `model_file`、`--prefix`、`--pre_seq_len`、`--dataset`、`--split`）。
3. 在 `models.py` 的 `encode_corpus` 中找到取 [CLS] 向量的那一行（提示：`out[i].view(-1)`），确认无论问题还是文档，最终都拿到一个固定维度的稠密向量。

**需要观察的现象**：脚本日志会依次打印 `evaluating dataset scifact...`、数据路径、`Retriever evaluation for k in: [1, 3, 5, 10, 100, 1000]`（BEIR 默认 k 值），最后是 4 组指标。

**预期结果**：4 组指标 `ndcg, _map, recall, precision` 均为字典，键是 `k_values` 里的各个 k（如 `NDCG@10`）。具体数值取决于 checkpoint，**待本地验证**——若用仓库提供的 P-tuning v2 checkpoint，跨域 NDCG@10 通常优于同等条件下从零全量微调的基线。

#### 4.1.5 小练习与答案

**练习 1**：BEIR 评测为什么固定用 `score_function="dot"` 而非余弦相似度？
**参考答案**：因为 BiEncoder 训练时用的是点积打分（`dot_product_scores`，见 u7-l2）。评测必须与训练目标一致，否则分数尺度不匹配，召回排序会失真。

**练习 2**：`DPRForBeir` 为什么要对 state_dict 的键名做 `key[prefix_len:]` 截断？
**参考答案**：checkpoint 里两个塔的权重键带有 `question_model.`/`ctx_model.`（prefix 模式下还带 `prefix_encoder.`）前缀，而装回时 `model_to_load` 已经是剥掉前缀的子模块，因此要把键名对齐，再用 `strict=False` 容忍未提供的（冻结主干）权重。

---

### 4.2 OAG-QA 主题级评测

#### 4.2.1 概念说明

OAG-QA 是**最大的公开「主题级」段落检索数据集**：17,948 条来自 22 个学科、87 个细分主题的查询，参考答案来自知乎、StackExchange 等专业社区，并被映射到 Open Academic Graph 的论文上（带摘要、标题、研究领域 FOS 等）。它和 OpenQA 的本质区别是：评测不是「一个全局大语料」，而是「**每个主题各自一个小语料（约 1 万篇论文）+ 各自的问题集**」，因此可以逐主题打分、再平均。这正是检验**跨主题泛化**的理想场景——模型从未在某些主题上训练过，却要在它们上面检索。

#### 4.2.2 核心流程

单主题评测（[eval_scripts/evaluate_on_oagqa.sh](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/eval_scripts/evaluate_on_oagqa.sh)）分两步，与 OpenQA 一致，但语料和问题都是「主题私有」的：

1. **编码主题语料**：`generate_dense_embeddings.py` 对 `data/oagqa-topic-v2/<topic>-papers-10k.tsv` 编码，落盘到 `encoded_files/`。
2. **检索 + 校验**：`dense_retriever.py` 读主题问题 `<topic>-questions.tsv`，在线编码问题向量，faiss 取 top-k，再做命中校验。

关键差异在命中校验：脚本带 `--oagqa` 开关（[evaluate_on_oagqa.sh:28](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/eval_scripts/evaluate_on_oagqa.sh#L28)）。批量评测则由 [batch_dense_retrieval.py](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/batch_dense_retrieval.py) 把 87 个主题逐个跑一遍、各自记 top-k 准确率，最后求平均（[L153-L157](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/batch_dense_retrieval.py#L153-L157)）。这个平均分就是「跨主题泛化」的综合指标。

#### 4.2.3 源码精读

`--oagqa` 如何改变命中判定？先看 [dense_retriever.py:258-260](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/dense_retriever.py#L258-L260)：

```python
check_index = 1 if args.oagqa else 0
questions_doc_hits, top_k_hits = validate(all_passages, question_answers,
                                         top_ids_and_scores, args.validation_workers,
                                         args.match, check_index)
```

`validate`（[dense_retriever.py:106-116](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/dense_retriever.py#L106-L116)）把 `check_index` 作为 `index` 透传到 `calculate_matches`，最终落到 [qa_validation.py:85](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/dpr/data/qa_validation.py#L85)：

```python
# modified
text = doc[index] # 0 for abstract, 1 for title
```

这里的 `doc` 来自 `load_passages`，格式为 `doc_id → (doc_text/abstract, title)`。于是：

- OpenQA（`index=0`）：在**摘要/正文**里做答案串匹配（如「Barack Obama」是否出现在文档中）。
- OAG-QA（`index=1`）：在**论文标题**里匹配。因为 OAG-QA 的「答案」是参考论文的标题（论文级匹配），而不是文本片段，所以必须切到 `doc[1]`。这一行 `# modified` 注释正是项目对原版 DPR 代码的改动点。

主题级平均逻辑在 [batch_dense_retrieval.py:149-157](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/batch_dense_retrieval.py#L149-L157)：

```python
for topic in eval_list:
    top_k_hits = batch_retrieval(args, topic, ctx_model, retriever, tensorizer)
    result_dict[topic] = top_k_hits[-1]          # 该主题的 top-k（k=n_docs）准确率
scores = 0
for topic, value in result_dict.items():
    scores += value
avg_score = scores / len(result_dict)            # 跨主题平均
result_dict["average"] = avg_score
```

每个主题独立编码、独立检索、独立校验，最终把 `top_k_hits[-1]`（即最大 k 处的命中率）取出来求平均，写入 `results/...json`。注意 `batch_retrieval`（[L59-L105](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/batch_dense_retrieval.py#L59-L105)）每次都用主题自己的 `ctx_file`、`qa_file` 重建索引——这正是「主题级」隔离的实现。

#### 4.2.4 代码实践

**实践目标**：跟踪 `--oagqa` 开关从命令行一路下沉到命中判定的完整链路，理解「主题级」与「字段切换」两件事。

**操作步骤**：

1. 从 [evaluate_on_oagqa.sh:28](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/eval_scripts/evaluate_on_oagqa.sh#L28) 的 `--oagqa` 出发，画出调用链：`dense_retriever.py` 的 `--oagqa`（[L293](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/dense_retriever.py#L293)）→ `check_index`（[L258](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/dense_retriever.py#L258)）→ `validate` 的 `index`（[L108](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/dense_retriever.py#L108)）→ `check_answer` 的 `doc[index]`（[qa_validation.py:85](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/dpr/data/qa_validation.py#L85)）。在每一步旁标注当前变量取值。
2. 打开 [batch_dense_retrieval.py:149-157](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/batch_dense_retrieval.py#L149-L157)，回答：如果只想得到「87 个主题各自的分数」而不要平均，应删掉哪几行？
3. 对照 [README.md:139-145](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/README.md#L139-L145)，写出在某个主题上跑单主题评测的命令（需给出 `$topic` 与 `$top-k`）。

**需要观察的现象**：单主题评测日志会打印 `top k documents hits accuracy`，是一个随 k 递增的列表；批量评测最终写出 `{"topicA": 0.xx, ..., "average": 0.xx}`。

**预期结果**：不同主题的准确率会有较大差异（热门主题高、冷门主题低），这正是「跨主题泛化」需要平均的动机；具体数值**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 OAG-QA 要在标题（`doc[1]`）上匹配，而不像 OpenQA 在正文（`doc[0]`）上匹配？
**参考答案**：OAG-QA 的「答案」是参考论文的标识（标题/论文级匹配），查询由专业社区问题映射到 OAG 中的具体论文，因此命中判定要看文档标题字段；正文是论文摘要，串匹配不合适。

**练习 2**：`batch_dense_retrieval.py` 为什么每个主题都要重建一次 faiss 索引，而不是建一次全局索引？
**参考答案**：每个主题有自己独立的候选论文集（`<topic>-papers-10k.tsv`），检索只能在主题语料内进行；主题间语料不重叠，无法共用一个全局索引。这也是「主题级」评测的定义。

---

### 4.3 ECE 校准指标与校准曲线

#### 4.3.1 概念说明

「泛化好」回答的是「**排得准不准**」，而「校准好」回答的是「**分数（置信度）可不可信**」。一个检索器若把不相关文档也打出 0.99 的（softmax 后）概率，那它的 top-1 即便碰巧正确，下游也无法靠分数做阈值过滤、置信度估计。PT-Retrieval 的第二条核心论断是：**只训练 prefix 这点参数，模型不易在训练域上过度自信，因而跨域时置信度更可信**。

ECE 是量化校准的标准指标。对检索任务，「概率」由两部分拼出：

- **置信度 \(\hat{p}\)**：对一个问题的 top-k 文档打分做 `softmax`，得到每篇文档的相对概率。
- **标签 \(y\)**：该文档是否真的相关——OpenQA 用「是否包含答案」（`hit`），BEIR 用官方 `qrels` 的相关性。

于是「top-k 文档是否相关」被建模成一串二分类样本，ECE 就在这串样本上算。

#### 4.3.2 核心流程

ECE 计算分三步，对应三个函数：

1. **造样本**：取每个问题的 top-n 文档（OpenQA 用 `--n-docs 5`，BEIR 用 `--n_ece_docs`），把它们的打分 `softmax` 成概率，逐对产出 `(y_true=是否相关, y_prob=概率)`。
2. **分桶**：把 \([0,1]\) 区间均匀切成 10 个桶，按 `y_prob` 把样本归桶。
3. **加权求和**：对每个桶算 `|经验准确率 − 平均置信度|`，按桶内样本占比加权，得到 ECE。

校准曲线则把每个桶的 `(平均置信度, 经验准确率)` 画成散点，与对角线对照——越贴近对角线越校准。

#### 4.3.3 源码精读

**OpenQA 上的 ECE 入口** [calibration/calibration_ece_openqa.py](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/calibration/calibration_ece_openqa.py) 复用 `dense_retriever.py` 的 faiss 检索拿到 `top_ids_and_scores`，再校验命中（[L117-L122](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/calibration/calibration_ece_openqa.py#L117-L122)）：

```python
check_index = 1 if args.oagqa else 0
questions_doc_hits, _ = validate(all_passages, question_answers, top_ids_and_scores,
                                 args.validation_workers, args.match, check_index)
ece = calculate_ece(top_ids_and_scores, questions_doc_hits)
print("ECE = %.3f" % ece)
```

`calculate_ece`（[calibration_ece_openqa.py:125-139](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/calibration/calibration_ece_openqa.py#L125-L139)）就是上面「造样本」的直译：

```python
for ids_and_scores, doc_hits in zip(top_ids_and_scores, questions_doc_hits):
    _, scores = ids_and_scores
    scores = scores[:args.n_docs]          # 只取 top-5
    doc_hits = doc_hits[:args.n_docs]
    scores = softmax(scores)               # softmax 成概率
    for pred_score, hit in zip(scores, doc_hits):
        y_true.append(int(hit))            # 1=相关，0=不相关
        y_prob.append(pred_score)          # softmax 概率作为置信度
_, _, ece = calibration_curve_with_ece(y_true, y_prob, n_bins=10)
```

注意它把 top-5 的**原始点积分**一起 softmax，于是 5 个概率之和为 1——这是一种「相对置信度」建模：top 文档分走的概率越高，表示模型越确信它是答案。

**ECE 内核** [calibration/utils.py](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/calibration/utils.py) 的 `calibration_curve_with_ece`（[L26-L71](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/calibration/utils.py#L26-L71)）是本讲的数学心脏。分桶与求和的关键几行（[L60-L69](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/calibration/utils.py#L60-L69)）：

```python
binids = np.searchsorted(bins[1:-1], y_prob)            # 每个样本归到哪个桶
bin_sums = np.bincount(binids, weights=y_prob, ...)     # 每桶概率总和
bin_true = np.bincount(binids, weights=y_true, ...)     # 每桶正例数
bin_total = np.bincount(binids, ...)                    # 每桶样本数
nonzero = bin_total != 0
prob_true = bin_true[nonzero] / bin_total[nonzero]      # acc(b): 经验准确率
prob_pred = bin_sums[nonzero] / bin_total[nonzero]      # conf(b): 平均置信度
ece = np.sum(np.abs(prob_true - prob_pred) * (bin_total[nonzero] / len(y_true)))
```

最后一行正是 2.3 节的公式：\(\text{ECE} = \sum_b \frac{n_b}{N}|\text{acc}(b)-\text{conf}(b)|\)，只是这里 `bin_sums/bin_total` 用求和再相除的方式算出了桶内平均置信度（等价于均值）。`strategy="uniform"` 时桶边界由 `np.linspace(0,1,n_bins+1)`（[L53](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/calibration/utils.py#L53)）给出，即等宽分桶。

**BEIR 上的 ECE** 走另一条「造样本」路径：[utils.py 的 `calculate_ece`](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/calibration/utils.py#L73-L87) 用官方 `qrels` 判定相关性（`rels = [k for k,v in qrels[qid].items() if v]`），取 top-`n_ece_docs` 同样 softmax 后送进同一个 `calibration_curve_with_ece`。入口 [calibration/calibration_beir.py:48-55](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/calibration/calibration_beir.py#L48-L55) 同时打印 ECE 与 ERCE（一种成对正负样本的校准误差，见 `calculate_erce` [L89-L127](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/calibration/utils.py#L89-L127)），并调用 `plot_calibration_curve` 画曲线。

**画曲线** `plot_calibration_curve`（[utils.py:129-159](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/calibration/utils.py#L129-L159)）的采样策略：每个问题随机抽 1 篇相关文档 + `n_non_rel` 篇不相关文档，softmax 后产出正负样本，再用 sklearn 的 `calibration_curve` 与 `CalibrationDisplay` 绘图，文件名按 `pt`（prefix）或 `ft`（finetune）区分（[L157-L159](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/calibration/utils.py#L157-L159)）：

```python
method = "pt" if args.prefix else "ft"
filename = f"{args.dataset}-{args.n_non_rel}-{method}"
plt.savefig(f'results/{filename}.png')
```

封装命令见 [eval_scripts/calibration_on_openqa.sh:17-27](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/eval_scripts/calibration_on_openqa.sh#L17-L27)（`--n-docs 5`）与 [eval_scripts/calibration_on_beir.sh:14-20](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/eval_scripts/calibration_on_beir.sh#L14-L20)。

#### 4.3.4 代码实践

**实践目标**：把本讲规格要求的两件事做扎实——(a) 列全三类评测数据与脚本；(b) 论证 prefix 为何更校准。同时手算一个 ECE。

**操作步骤**：

1. 通读 [README.md:109-176](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/README.md#L109-L176)，填写下表（DPR 部分）：

   | 评测数据 | 评测脚本命令 | 用途 |
   | --- | --- | --- |
   | OpenQA | `bash eval_scripts/evaluate_on_openqa.sh $dataset $top-k` | 训练域内检索准确率 |
   | BEIR | `bash eval_scripts/evaluate_on_beir.sh $dataset` | 跨域零样本检索 |
   | OAG-QA | `bash eval_scripts/evaluate_on_oagqa.sh $topic $top-k` | 跨主题检索 |

   并补充校准脚本：OpenQA 用 `eval_scripts/calibration_on_openqa.sh`，BEIR 用 `eval_scripts/calibration_on_beir.sh`。

2. **手算 ECE**：假设某次检索共 10 条「top 文档」样本，softmax 概率与是否相关如下（已按概率排序）：

   | \(\hat{p}\) | 0.9 | 0.8 | 0.7 | 0.6 | 0.55 | 0.45 | 0.35 | 0.25 | 0.15 | 0.05 |
   | \(y\) | 1 | 1 | 0 | 1 | 0 | 0 | 1 | 0 | 0 | 0 |

   用 2 个等宽桶 \([0,0.5)\)、\([0.5,1]\) 手算 ECE。
   - 桶1 \([0.5,1]\)：5 个样本，正例 3 个 → \(\text{acc}=3/5=0.6\)，\(\text{conf}=(0.9+0.8+0.7+0.6+0.55)/5=0.71\)，贡献 \(\frac{5}{10}|0.6-0.71|=0.055\)。
   - 桶2 \([0,0.5)\)：5 个样本，正例 1 个 → \(\text{acc}=0.2\)，\(\text{conf}=(0.45+0.35+0.25+0.15+0.05)/5=0.25\)，贡献 \(\frac{5}{10}|0.2-0.25|=0.025\)。
   - \(\text{ECE}=0.055+0.025=0.08\)。

3. **写一段话**（约 100 字）论证「prefix 为何更校准」，要求结合「可训练参数少」这一特性。

**需要观察的现象**：若你能在本地同时跑 prefix（`--prefix`）与全量微调两个 checkpoint 的 `calibration_on_openqa.sh`，会看到日志里两个 `ECE = ...` 数值，prefix 版通常更小；`results/` 下会生成 `*.png` 校准曲线，prefix 版的散点更贴近对角线。

**预期结果**：prefix 版 ECE < 全量微调版 ECE（**待本地验证**具体数值）。手算 ECE 应得到 0.08。

#### 4.3.5 小练习与答案

**练习 1**：`calibration_curve_with_ece` 里 `prob_pred` 为什么用 `bin_sums/bin_total` 而不是 `np.mean`？
**参考答案**：`bin_sums` 是该桶所有 `y_prob` 之和，除以桶内样本数 `bin_total`，得到的正是桶内预测概率的均值——与 `np.mean` 数学等价，只是用 `bincount(weights=...)` 一次性向量化完成，避免再分组取均值。

**练习 2**：如果把 `n_bins` 从 10 改成 100，ECE 通常会怎么变？这对结论有何影响？
**参考答案**：桶更细时，每桶内样本更少，估计方差变大；同时更能捕捉局部失准。ECE 数值会变化（样本充足时通常更接近真实失准度），所以比较两种方法时必须用相同的 `n_bins` 才公平。

**练习 3**：为什么 `calculate_ece`（OpenQA 版）只取 top-5 而不是全部召回结果？
**参考答案**：softmax 在很长的尾巴上会把概率压到接近 0，这些样本几乎全是不相关文档，会主导分桶、稀释信号；只取 top-5 聚焦「模型真正有信心」的那几篇，校准才有意义。

---

## 5. 综合实践

**任务**：为 PT-Retrieval 设计一份完整的「跨域泛化 + 校准」对比评测方案，比较 P-tuning v2 与全量微调两个 checkpoint。

**要求产出一张评测计划表**（无需真正运行，重在把命令与指标对上号）：

| 维度 | 数据 | 脚本命令 | 关注指标 | 预期结论 |
| --- | --- | --- | --- | --- |
| 训练域内 | OpenQA（nq/trivia/webq/curatedtrec） | `eval_scripts/evaluate_on_openqa.sh` | top-k 准确率 | 两者接近 |
| 跨域 | BEIR（如 scifact/nfcorpus/arguana） | `eval_scripts/evaluate_on_beir.sh` | NDCG@10 | prefix 掉分更少 |
| 跨主题 | OAG-QA（87 主题批量） | `oagqa_eval_scripts/batch_evaluate_*_dpr_on_oagqa.sh` | 主题平均 top-k | prefix 更高 |
| 校准 | OpenQA / BEIR | `eval_scripts/calibration_on_*.sh` | ECE、校准曲线 | prefix 的 ECE 更小、更贴对角线 |

**配套分析**：基于本讲源码，写 200 字说明——(1) 为什么 BEIR 用 `DPRForBeir` 包装、而 OpenQA/OAG-QA 直接用 `dense_retriever.py`？(2) `--oagqa` 在评测与校准脚本中各起什么作用？(3) ECE 的「置信度」与「标签」在 OpenQA 版与 BEIR 版里分别来自哪里？

**参考要点**：
1. BEIR 有自己的召回/评测框架（`EvaluateRetrieval`、`qrels`、NDCG），所以用 `DPRForBeir` 适配其 `encode_queries/encode_corpus` 接口；OpenQA/OAG-QA 用 DPR 原生的 faiss 检索 + 答案串匹配，走 `dense_retriever.py`。
2. 在评测里 `--oagqa` 把命中字段从摘要切到标题；在校准脚本 `calibration_ece_openqa.py` 里它同样经 `check_index` 影响标签 `y_true`，因此 OAG-QA 的 ECE 也是按标题命中来算的。
3. OpenQA 版：置信度＝top-5 分数 softmax，标签＝`validate` 的 `hit`；BEIR 版：置信度＝top-`n_ece_docs` 分数 softmax，标签＝`qrels` 的相关性。

> 若本地具备 GPU 与下载好的 checkpoint，可真正跑一遍并把实际数值填入上表；否则标明「待本地验证」，重点是建立起「评测数据 → 脚本 → 指标 → 论文论断」的对应关系。

## 6. 本讲小结

- PT-Retrieval 用**三类评测数据**检验两条论断：OpenQA 看训练域内、BEIR 看跨域零样本、OAG-QA 看跨主题，三者脚本入口与指标各不相同。
- BEIR 评测靠 [beir_eval/models.py](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/beir_eval/models.py) 的 `DPRForBeir` 适配器把带 prefix 的双塔接入 BEIR，产出 NDCG/MAP/Recall/Precision。
- OAG-QA 评测的关键是 `--oagqa` → `check_index=1` → `doc[1]`（标题）匹配，且每个主题独立建索引、独立检索、最后取平均，实现「跨主题」评测。
- ECE 由 [calibration/utils.py](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/calibration/utils.py) 的 `calibration_curve_with_ece` 计算：等宽分桶后对 \(|\text{acc}(b)-\text{conf}(b)|\) 按桶样本量加权求和；置信度来自 top-k 分数 softmax，标签来自命中或 `qrels`。
- 「prefix 更校准」的机理：可训练参数极少 → 不易在训练域过度自信 → 跨域时分数（softmax 概率）与真实相关性更吻合，ECE 更小、校准曲线更贴对角线。

## 7. 下一步学习建议

- **回到论文**：结合 [README.md](https://github.com/THUDM/P-tuning-v2/blob/b1520c9aa177ffe539a77b80fd8bca992e76513e/PT-Retrieval/README.md) 引用的论文（arXiv 2207.07087），对照本讲源码看「泛化」与「校准」两张结果表，把表里的数字与本讲的脚本/指标一一对应。
- **扩展到 ColBERT**：本讲只讲了 DPR 双塔的评测；`colbert/` 下还有 `scripts/evalute_on_beir.sh`（注意原仓库拼写为 `evalute`）做 ColBERT 的 BEIR 评测，可作为对比阅读。
- **下一站——架构总览**：u8-l1 将回到主项目，把「入口分派 → 任务数据 → 模型工厂 → prefix 注入 → 训练器 → 评估搜索」全链路串成一张图，并给出二次开发（新增任务/模型/指标）的扩展点清单，作为整个手册的收束。
