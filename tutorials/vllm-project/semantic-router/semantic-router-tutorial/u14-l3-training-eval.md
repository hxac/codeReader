# 训练与评估

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `src/training/` 下四大子目录（`model_classifier`、`model_embeddings`、`model_eval`、`model_selection`）各自训练「什么模型、用什么数据、产出什么文件」。
- 描述一条完整的「后训练 → 评估 → 上线」闭环：LoRA 微调的分类器如何被 `model_discovery.go` 自动发现、ML 选择模型（KNN/KMeans/SVM/MLP）如何经 CGO binding 被加载进运行时。
- 区分两类基准：`perf/` 的组件微基准（gate 每个 PR）与 `bench/` 的路由质量基准（衡量路由准确率）。
- 动手运行一个训练子目录的脚本，并解释它的输入、产出与评估指标。

本讲是手册的「专家层」收尾之一，回答的不是「路由器怎么跑」，而是「路由器里那些分类器、选择模型的权重从哪里来、怎么验证、怎么持续保住质量」。

## 2. 前置知识

本讲依赖你已经在 u8-l2 学过「域/类别分类器」：那讲解释了 MMLU-Pro 域分类模型如何输出全概率分布、如何用归一化 Shannon 熵做多类别匹配，以及决策引擎如何用 `mmlu_categories` 做别名命中。本讲正是那段的「上游」——讲清楚那个域分类模型的权重是怎么训练出来、评估达标、最终落进 CGO binding 的。

几个你需要先建立的概念：

- **后训练（post-training）**：拿到一个已经预训练好的 Transformer（如 BERT、Qwen3），在你的特定任务数据上再训练一轮，让它专门擅长某一类判断。Semantic Router（下称 SR）几乎所有「学习型信号」背后的模型都是这样来的。
- **LoRA（Low-Rank Adaptation，低秩适配）**：一种参数高效的后训练技巧。不更新模型全部权重，而是冻结原权重、只学一对很小的「低秩矩阵」叠加到上面，可训练参数减少 99%+，让消费级硬件也能训。
- **CGO binding**：SR 用 Rust（candle/ml-binding）和 C++（openvino）做推理，再通过 Go 的 CGO 机制暴露成 Go 函数。训练在 Python 侧，推理在 Go/Rust 侧，中间靠「序列化成 JSON / safetensors 文件」衔接。这部分细节在 u12-l4 已建立。
- **微基准 vs 质量基准**：「这个函数快不快、分配多少内存」是微基准（`perf/`）；「这个路由策略准不准、命中率多少」是质量基准（`bench/`）。两者关注点完全不同，SR 把它们物理隔离在两个目录。

## 3. 本讲源码地图

本讲涉及的关键文件（按职责分组）：

| 文件 | 作用 |
|------|------|
| [src/training/model_classifier/README.md](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/training/model_classifier/README.md) | LoRA 微调脚本总览（意图/PII/越狱/MMLU-Pro 求解器） |
| [src/training/model_classifier/verify_text_classification_datasets.py](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/training/model_classifier/verify_text_classification_datasets.py) | 用多 LLM 评委 + 多数投票审计数据集标签 |
| [src/training/model_selection/ml_model_selection/README.md](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/training/model_selection/ml_model_selection/README.md) | KNN/KMeans/SVM/MLP 选择模型训练全流程 |
| [src/training/model_selection/ml_model_selection/validate.go](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/training/model_selection/ml_model_selection/validate.go) | 用真实生产 Go/Rust 代码验证 ML 选择收益 |
| [src/training/model_eval/README.md](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/training/model_eval/README.md) | MoM 集合评估脚本（10 个模型的质量指标） |
| [src/training/model_eval/mmlu_pro_vllm_eval.py](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/training/model_eval/mmlu_pro_vllm_eval.py) | 对 vLLM 后端跑 MMLU-Pro 标准基准 |
| [src/training/model_eval/result_to_config.py](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/training/model_eval/result_to_config.py) | 把 MMLU-Pro 结果分析成 v0.3 配置骨架 |
| [perf/README.md](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/perf/README.md) | 组件微基准框架与 PR 回归门禁 |
| [bench/README.md](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/bench/README.md) | 路由质量基准套件总览 |
| [src/semantic-router/pkg/classification/model_discovery.go](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/classification/model_discovery.go) | 运行时自动发现 LoRA 模型 |
| [src/semantic-router/pkg/modelselection/persistence.go](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/modelselection/persistence.go) | ML 选择模型从 JSON 加载进 Rust binding |
| [src/semantic-router/pkg/selection/ml_adapter.go](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/selection/ml_adapter.go) | 把 ML 选择器适配进路由选择系统 |

## 4. 核心概念与源码讲解

### 4.1 后训练脚本：LoRA 微调与选择模型训练

#### 4.1.1 概念说明

SR 的「智能」不是凭空来的。你在 u8 系列看到的域分类器、PII 检测器、越狱检测器，以及在 u6-l2 看到的 KNN/Hybrid 选择算法，背后都有训练好的模型权重。这些权重由 `src/training/` 下的脚本离线训练产出，再被运行时加载。后训练脚本解决的核心问题是：**把一个通用预训练模型，变成一个专精某项路由判断的小专家**。

`src/training/` 下有四个主要子目录，各管一类模型：

| 子目录 | 训练什么 | 典型产出 |
|--------|---------|---------|
| `model_classifier/` | 分类器（意图/PII/越狱/事实核查/模态/MMLU-Pro 求解器） | LoRA adapter（safetensors） |
| `model_embeddings/` | 嵌入模型（缓存/领域自适应） | 微调后的 embedding 模型 |
| `model_selection/` | 模型选择算法（ML 的 KNN/KMeans/SVM/MLP，RL 的 Router-R1/GMTRouter） | JSON 模型文件 / 训练 checkpoint |
| `model_eval/` | （不训练，只评估）评估上述模型的质量 | 结果 JSON + 混淆矩阵图 |

本节聚焦 `model_classifier`（LoRA）和 `model_selection`（ML/RL），它们是路由器信号层与选择层的直接来源。

#### 4.1.2 核心流程

**LoRA 微调流水线**（`model_classifier/`）的统一形状是：每个任务一个目录，内含「Python 训练脚本 + Go 验证器 + CPU 优化训练脚本」。以三个核心任务为例：

```text
准备数据集（合成/Presidio/Toxic-chat）
   ↓
Python 脚本用 peft.LoraConfig 冻结主干、只训低秩矩阵 BA
   ↓ W = W₀ + BA,  rank=16, alpha=32
导出 adapter_model.safetensors + config.json + label_mapping.json
   ↓
Go 验证器（CGO 调 candle-binding）跑同一批样本，校验 Python↔Go 数值一致
   ↓
模型目录按 lora_{task}_{arch}_r{rank}_model_rust/ 命名，待运行时发现
```

**ML 选择模型训练流水线**（`model_selection/ml_model_selection/`）是一条四步流水线，关键是它要先「打基准」再训练——因为选择模型本质是学「哪个模型答得又好又快」，必须先有各模型的真实表现数据：

```text
Step1  准备 queries.jsonl（query + ground_truth + 可选 category）
Step2  benchmark.py：每条 query 打到所有候选 LLM，测 performance(准确率) + response_time(延迟)
Step3  train.py：特征 = query嵌入(1024维 Qwen3) ⊕ category one-hot(14维) = 1038维
            ↓ 训练 KNN/KMeans/SVM/MLP
            产出 knn/kmeans/svm/mlp_model.json（与 Rust 推理代码兼容）
Step4  （可选）upload_model.py 上传到 HuggingFace
```

打分公式所有算法共用，质量占大头、速度只占小头：

\[
\text{score} = 0.9 \times \text{quality} + 0.1 \times \text{speed\_factor}, \quad \text{speed\_factor} = \frac{1}{1 + \text{normalized\_latency}}
\]

#### 4.1.3 源码精读

**LoRA 的数学本质**。README 用一行公式点明了 LoRA 在干什么：

> LoRA decomposes weight updates into two smaller matrices: `W = W₀ + ΔW = W₀ + BA`，其中 `B` 是 d×r、`A` 是 r×k、`r` 是秩（项目用 16）。

参见 [src/training/model_classifier/README.md:21-43](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/training/model_classifier/README.md#L21-L43)，这段同时给出了超参约定（`r=16, alpha=32, dropout=0.1`，`alpha` 一般取 2×rank）。可训练参数从 1.1 亿压到约 100 万，所以能在 CPU/单 GPU 上训。

**三类任务对应三种任务类型**。同一套 LoRA 框架覆盖了序列分类与 token 分类两种范式，见 [src/training/model_classifier/README.md:256-278](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/training/model_classifier/README.md#L256-L278)：

- 意图分类：`SEQ_CLS`，类别 `business/law/psychology`，指标是 Accuracy。
- PII 检测：`TOKEN_CLS`（实体级），标签 `PERSON/EMAIL_ADDRESS/PHONE_NUMBER/...`，指标是 token 级 F1。这正是 u8-l3 PII 信号背后的模型。
- 安全检测：`SEQ_CLS`，二分类 `safe/unsafe`。

**Python↔Go 数值一致性校验**。每个训练目录都配了一个 `*_verifier.go`，用 CGO 调 Rust 推理库，对同一批样本跑出 logits，确认和 Python 侧逐数值一致。这是为了杜绝「训练用 Python 算的效果、上线用 Rust 算的却是另一回事」这种静默漂移。参见 [src/training/model_classifier/README.md:279-301](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/training/model_classifier/README.md#L279-L301)。

**ML 选择的「先打基准再训练」**。`benchmark.py` 的核心是让每条 query 对所有候选模型都跑一遍，测出 `performance`（对 ground_truth 的准确率，0~1）和 `response_time`（秒）。这两个字段是后续所有算法的训练信号，见 [src/training/model_selection/ml_model_selection/README.md:168-182](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/training/model_selection/ml_model_selection/README.md#L168-L182)。

**特征工程的 1038 维**。ML 选择不直接看 query 文本，而是把它变成定长向量：1024 维的 Qwen3 嵌入拼接 14 维的类别 one-hot。这 14 个类别和 u8-l2 域分类器的 14 个 MMLU 类别**完全一致**（biology/business/.../psychology），所以「域分类结果」会作为离散特征喂给选择模型。参见 [src/training/model_selection/ml_model_selection/README.md:530-548](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/training/model_selection/ml_model_selection/README.md#L530-L548)。

> ⚠️ README 特别提醒：配置 `values.yaml` 时域名必须**精确匹配**（如 `computer science` 带空格，不是 `computer_science`），否则 one-hot 维度对不上。

**RL 选择（进阶）**。`rl_model_selection/` 走另一条路：用强化学习训练一个路由策略网络，对应论文 Router-R1（多轮路由）和 GMTRouter（基于图的个性化路由）。训练出的 checkpoint 经 `RLDrivenSelector` 接入，配置类型是 `rl_driven`。参见 [src/training/model_selection/rl_model_selection/README.md:125-139](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/training/model_selection/rl_model_selection/README.md#L125-L139)。这部分需要 24GB+ GPU，属于研究性更强的路线。

#### 4.1.4 代码实践

> **实践目标**：跑通一个 ML 选择模型训练子目录，看清「输入数据 → 产出模型 → 评估指标」全链路。

**操作步骤**（基于 [src/training/model_selection/ml_model_selection/README.md:67-94](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/training/model_selection/ml_model_selection/README.md#L67-L94)）：

1. 进入目录并装依赖：
   ```bash
   cd src/training/model_selection/ml_model_selection
   pip install -r requirements.txt
   ```
2. 准备一个最小 queries 文件 `my_queries.jsonl`：
   ```json
   {"query": "Write a Python function to sort a list", "ground_truth": "def sort_list(lst): return sorted(lst)", "category": "computer science"}
   {"query": "What is the derivative of x^2?", "ground_truth": "2x", "category": "math"}
   {"query": "Explain photosynthesis", "ground_truth": "Process where plants convert sunlight to energy", "category": "biology"}
   ```
3. 跑基准（需要一个 OpenAI 兼容端点，可指向本地 vLLM 或 Ollama）：
   ```bash
   python benchmark.py --queries my_queries.jsonl --models my-llm-v1,my-llm-v2 \
     --endpoint http://localhost:8000/v1 --output benchmark_output.jsonl
   ```
4. 训练：
   ```bash
   python train.py --data-file benchmark_output.jsonl --output-dir models/
   ```

**需要观察的现象**：

- `benchmark.py` 会输出每条 query 对每个模型的 `performance` 与 `response_time`。
- `train.py` 结束后 `models/` 下应出现 `knn_model.json`、`kmeans_model.json`、`svm_model.json`、`mlp_model.json` 四个文件。
- 打开任一 JSON，应能看到与 Rust 推理代码兼容的结构（聚类中心 / 支持向量 / 邻居等）。

**预期结果**：拿到四个 JSON 模型文件。注意：要让训练真正有意义，queries 数量要够（README 示例用的是 109 条带 ground truth 的测试集）；3 条 query 只能验证流水线跑通，模型不会有区分力。

> 待本地验证：实际 `performance` 数值取决于你接的后端模型能力；本实践无法在缺少运行 LLM 的环境下给出确定数字。

#### 4.1.5 小练习与答案

**练习 1**：为什么 LoRA 把可训练参数从 1.1 亿降到 100 万，却「不掉精度」？
> **答案**：LoRA 冻结了原始权重 W₀（保留预训练全部知识），只额外学一对低秩矩阵 B（d×r）和 A（r×k）。当 r 远小于 d、k 时，新增可训练参数 d·r + r·k 远小于 d·k；经验上很多任务的权重更新本身就是低秩的，所以用很小的 r（如 16）就足以表达任务所需的调整。

**练习 2**：ML 选择模型的特征向量为什么是 1038 维（1024+14），而不是直接用 1024 维嵌入？
> **答案**：嵌入捕捉语义相似度，但「这个 query 属于什么领域」是极强的离散路由信号。把域分类器的 14 类结果做成 one-hot 拼上去，等于把 u8-l2 的域分类结论作为显式特征喂给选择模型，让它在「语义相近但领域不同」的 query 上也能正确分流。

**练习 3**：PII 检测用 `TOKEN_CLS`（token 分类）而非 `SEQ_CLS`（序列分类），原因是什么？
> **答案**：PII 检测要定位文本里**哪些 token** 是敏感实体（人名、邮箱、社保号等）并给出位置和类型，供后续脱敏；这是 token 级标注任务。而意图/越狱检测只需判断整句话属于哪一类，是序列级标注。任务类型决定了 LoRA 的 `task_type` 与输出头。

### 4.2 评估流程：模型质量评估与数据集审计

#### 4.2.1 概念说明

训练出来的模型不能直接上线，必须先评估。SR 的评估分三层，由浅入深：

1. **单模型质量评估**（`model_eval/mom_collection_eval.py`）：对 SR 官方维护的 10 个 MoM（Mixture of Models）模型逐个测 Accuracy/Precision/Recall/F1/延迟，回答「这个分类器够不够好」。
2. **标准学术基准**（`mmlu_pro_vllm_eval.py`、`arc_challenge_vllm_eval.py`）：把后端模型放到 MMLU-Pro / ARC Challenge 这些公认题库上跑，回答「后端模型本身能力如何」，并可把结果转成路由配置。
3. **生产路径验证**（`validate.go`）：用**真实的生产 Go/Rust 推理代码**跑选择模型，对比 ML 选择 vs 随机/固定基线的收益，回答「上了这套选择算法，到底比不上强多少」。

此外还有一条横切的**数据集审计**线：训练数据本身的标签可能标错，`verify_text_classification_datasets.py` 用多个 LLM 当评委投票找出错标样本。这是「评估的评估」——先保证训练数据可信，模型评估才有意义。

#### 4.2.2 核心流程

**MoM 集合评估**的执行流程：

```text
指定 --model feedback jailbreak fact-check intent pii
   ↓
从 HuggingFace 拉 merged / LoRA 两版模型
   ↓ 对应数据集（MMLU-Pro 用于 intent，Presidio 用于 pii）
逐 batch 推理 → 算 Accuracy/P/R/F1 + 混淆矩阵 + 延迟(avg/p50/p99)
   ↓
落 results/{model}_results.json + {model}_cm.png
```

**生产路径验证**（`validate.go`）的流程更值得精读，因为它跑的就是上线后真正走的代码：

```text
从 HuggingFace 下载 knn/kmeans/svm 模型 + 基准数据
   ↓
用 candle-binding(Qwen3) 给测试 query 生成嵌入
   ↓
用 modelselection.NewSelector 装载各算法（= 生产路径）
   ↓
对每条 query 调 selector.Select(embedding) 选模型
   ↓
对比策略：Oracle(上限) / 各 ML 算法 / Always-某模型 / Random
   ↓
输出「ML 选择比 random 质量提升多少、选中最优模型的频率」
```

#### 4.2.3 源码精读

**MoM 评估覆盖的 5 类模型**。`mom_collection_eval.py` 把 SR 官方的 10 个模型（merged + LoRA 各 5 个）组织成一张表，覆盖文本分类（feedback/jailbreak/fact-check/intent）与 token 分类（pii），见 [src/training/model_eval/README.md:26-35](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/training/model_eval/README.md#L26-L35)。指标体系见 [:17-22](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/training/model_eval/README.md#L17-L22)：Accuracy/Precision/Recall/F1/混淆矩阵/延迟(avg/p50/p99)，结果存 `results/`。

**标准基准 → 配置骨架**。`mmlu_pro_vllm_eval.py` 把后端模型放到 MMLU-Pro 题库上跑分（见 [src/training/model_eval/mmlu_pro_vllm_eval.py:1-6](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/training/model_eval/mmlu_pro_vllm_eval.py#L1-L6)），然后 `result_to_config.py` 分析结果直接生成一份 canonical v0.3 配置骨架（见 [src/training/model_eval/result_to_config.py:1-2](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/training/model_eval/result_to_config.py#L1-L2)）——也就是说，「评估」不只是打分，还能反哺配置：哪个模型在哪个领域强，就把它路由到那个领域。

**生产路径验证的关键代码**。`validate.go` 最有价值的地方是：它装载选择器用的是与运行时一模一样的入口 `modelselection.NewSelector(cfg)`，配置就是 `config.MLModelSelectionConfig{Type: alg, ModelsPath: *modelsDir}`，见 [src/training/model_selection/ml_model_selection/validate.go:213-233](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/training/model_selection/ml_model_selection/validate.go#L213-L233)：

```go
selectors := make(map[string]modelselection.Selector)
algorithms := []string{"knn", "kmeans", "svm", "mlp"}
for _, alg := range algorithms {
    cfg := &config.MLModelSelectionConfig{Type: alg, ModelsPath: *modelsDir}
    selector, err := modelselection.NewSelector(cfg)   // 与生产同路径
    ...
    selectors[alg] = selector
}
```

而它显式声明「用真实生产 Go/Rust 代码」，见 [src/training/model_selection/ml_model_selection/validate.go:93-94](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/training/model_selection/ml_model_selection/validate.go#L93-L94)：嵌入走 candle-binding（Qwen3），ML 推理走 ml-binding（Linfa）。这样验证出来的收益数字才可信——没有「Python 评估好、上线却不是那么回事」的落差。

README 给出的典型输出（见 [:422-447](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/training/model_selection/ml_model_selection/README.md#L422-L447)）显示：MLP 选择比随机基线质量提升 +47.1%，KMeans +29.9%，并附「选中最优模型的频率」对比。Oracle 是上界（每条 query 都选事后看最优的那个模型）。

**数据集审计**。`verify_text_classification_datasets.py` 用多 LLM 评委 + 多数投票找出可能标错的样本，还支持「两阶段模式」：stage1 模型先全量扫一遍，只把低置信或疑似错标的样本 escalate 到多评委 stage2 投票，以节省成本。见 [src/training/model_classifier/verify_text_classification_datasets.py:1-40](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/training/model_classifier/verify_text_classification_datasets.py#L1-L40)。

#### 4.2.4 代码实践

> **实践目标**：用 `mom_collection_eval.py` 跑一次单模型评估，读懂它的输出指标。

**操作步骤**（基于 [src/training/model_eval/README.md:53-76](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/training/model_eval/README.md#L53-L76)）：

1. 装依赖并进入目录：
   ```bash
   cd src/training/model_eval
   pip install -r requirements.txt
   ```
2. CPU 上跑一次小样本快速测试（不需 GPU）：
   ```bash
   python mom_collection_eval.py --model pii --limit 100 --device cpu
   ```
3. （有 GPU 时）评估多个模型：
   ```bash
   python mom_collection_eval.py --model feedback jailbreak intent --device cuda --parallel
   ```

**需要观察的现象**：

- 终端会逐模型打印 Accuracy/Precision/Recall/F1 与延迟分位数。
- `results/` 下生成 `pii_results.json`；文本分类任务还会生成混淆矩阵热力图 `pii_cm.png`。

**预期结果**：得到一个 JSON，内含该模型在测试集上的各项指标与平均/p50/p99 延迟。文本分类任务额外得到混淆矩阵图，可直观看哪些类别容易被混淆。

> 待本地验证：首次运行需从 HuggingFace 下载模型，指标具体数值依赖下载到的模型版本与硬件。

#### 4.2.5 小练习与答案

**练习 1**：`validate.go` 为什么要强调「用真实生产 Go/Rust 代码」做验证，而不是直接用 Python 评估模型？
> **答案**：训练和离线评估在 Python（PyTorch）侧，上线推理在 Go/Rust（candle/Linfa）侧。两边是不同实现，可能因算子实现、精度、嵌入模型加载方式不同而产生数值差异。`validate.go` 走 `modelselection.NewSelector` 这条与运行时完全相同的入口，确保「评估到的收益」就是「上线后能拿到的收益」，杜绝 Python↔Rust 漂移。

**练习 2**：`result_to_config.py` 把 MMLU-Pro 结果转成「v0.3 配置骨架」，这背后体现了 SR 怎样的设计哲学？
> **答案**：体现了「数据驱动路由配置」——评估不只是出一份报告，而是直接反哺路由决策：哪个模型在数学题上分高，就把数学类 query 路由给它。配置不是手写的静态规则，而是可以从基准评测结果自动生成的。

**练习 3**：`verify_text_classification_datasets.py` 的「两阶段模式」为什么比「全部直接多评委投票」更省成本？
> **答案**：多评委投票要对每条样本调多个 LLM，成本高。两阶段模式让一个 stage1 模型先廉价地全量扫一遍，只把低置信或疑似错标的小部分样本 escalate 到昂贵的多评委 stage2。绝大多数明确样本在 stage1 就被认可，只有少数疑难样本才付多评委的成本。

### 4.3 性能基准：perf 微基准与 bench 路由质量基准

#### 4.3.1 概念说明

SR 把「性能」严格拆成两个互不混淆的维度，放在两个目录：

- **`perf/`——组件微基准（micro-benchmark）**：测「这段代码快不快、分配多少内存」。用 Go 标准库的 benchmark 机制，对分类、决策引擎、缓存、ext_proc 热路径、Looper 等单个组件压测。**不需要运行路由器**，每个 PR 都跑，是回归门禁。
- **`bench/`——路由质量基准（routing-quality benchmark）**：测「这个路由策略准不准、命中率多少、在故障下稳不稳」。需要真实运行的 LLM 后端，跑 MMLU-Pro 等推理数据集，比较 SR 路由 vs 直连 vLLM。

这条区分极其重要，[perf/README.md:5-13](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/perf/README.md#L5-L13) 一开头就划清了边界：要测路由*质量*（准确率、会话路由、幻觉检测、grounded fusion）去 `bench/`；要测组件*性能*（延迟、内存、分配）留在 `perf/`。

#### 4.3.2 核心流程

**`perf/` 的 PR 回归门禁**流程（见 [perf/README.md:191-225](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/perf/README.md#L191-L225)）：

```text
PR 触发 performance-test.yml
   ↓ 跑组件 benchmark + Looper family，tee 到 reports/bench-output.txt
perftest --parse-bench → reports/current.json
   ↓
perftest --compare-baseline --fail-on-regression
   ↓ 对比 testdata/baselines/ 里已提交的基线
任一 benchmark 的 allocs/op 或 B/op 回归超阈值 → 检查变红、阻断 PR
   ↓（ns/op 仅 advisory 报告，从不阻断）
在 PR 上发一条汇总评论
```

**关键设计抉择：门禁只看 `allocs/op` 和 `B/op`，不看 `ns/op`**。原因见 [perf/README.md:281-287](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/perf/README.md#L281-L287)：绝对纳秒数随 CI 机器的 CPU 和「吵闹邻居」波动，跨机器对比会产生假阳性（或为避免假阳性把基线调慢，反而掩盖真回归）；而每次操作的分配数和字节数由代码路径决定，对给定构建是确定性的，能跨机器可靠对比。

**`bench/` 的路由质量矩阵**见 [bench/README.md:8-28](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/bench/README.md#L8-L28)，它用一张「我想测什么 → 用哪个脚本」的导航表组织：

| 问题 | 用哪个 |
|------|--------|
| 路由在真实数据集上准确率如何 | `router_flow/real_eval/` |
| 会话感知路由在负载/故障下稳不稳 | `agentic_routing_live_benchmark.py` |
| 幻觉检测有多好 | `hallucination/` |
| grounding-aware fusion 有没有帮助 | `grounded_fusion/` |
| Router Learning 路由质量有没有回归 | `profiles/router_learning/` |

#### 4.3.3 源码精读

**`perf/` 的目录结构**清晰反映了「组件隔离」思想，见 [perf/README.md:79-96](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/perf/README.md#L79-L96)：`benchmarks/` 下按组件分文件（`classification_bench_test.go`、`decision_bench_test.go`、`cache_bench_test.go`、`extproc_bench_test.go`），`config/thresholds.yaml` 存性能 SLO，`testdata/baselines/` 存已提交基线。

**组件基准举例**。分类基准用不同 batch size 压测，决策引擎基准测规则匹配耗时，缓存基准对比 HNSW vs 线性检索、不同条数与并发，见 [perf/README.md:99-130](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/perf/README.md#L99-L130)。这些直接对应你在 u5/u6/u9 学过的组件。

**性能 SLO 阈值**定义在 `config/thresholds.yaml`，见 [perf/README.md:160-168](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/perf/README.md#L160-L168)：

| 组件 | 指标 | 阈值 |
|------|------|------|
| 分类（batch=1） | P95 延迟 | < 10ms |
| 分类（batch=10） | P95 延迟 | < 50ms |
| 决策引擎 | P95 延迟 | < 1ms |
| 缓存（1K 条） | P95 延迟 | < 5ms |
| 缓存 | 命中率 | > 80% |

注意：这些 SLO 是「目标」（advisory 的人类可读期望），与「门禁」是两回事——门禁只卡 allocs/op 和 B/op。

**阈值配置的「首匹配优先」**。`thresholds.yaml` 按 benchmark 名字模式匹配，第一条命中生效，所以条目要按「最具体在前」排序，没匹配到的走 `default`（默认 allocs/bytes 回归 10% 阻断），见 [perf/README.md:260-279](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/perf/README.md#L260-L279)。例如决策引擎（`^Benchmark(EvaluateDecisions|PrioritySelection|Rule)`）卡得更严（5%），因为它在热路径上。

**基线生命周期**。基线是硬件无关的（因为只存 allocs/bytes），所以无论在哪台机器记录都可比对；用 `make perf-baseline-update` 刷新后提交 `testdata/baselines/`。空基线的 suite（如分类模型还没缓存时）直接跳过，不会误判失败，见 [perf/README.md:208-225](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/perf/README.md#L208-L225)。

#### 4.3.4 代码实践

> **实践目标**：跑一次组件微基准，体验 allocs/op 门禁逻辑，并对比 `ns/op`（advisory）与 `allocs/op`（阻断）的差异。

**操作步骤**（基于 [perf/README.md:29-40](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/perf/README.md#L29-L40) 与 [:328-359](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/perf/README.md#L328-L359)）：

1. 先确保 Rust 库已构建并设好库路径（微基准会经 CGO 调推理）：
   ```bash
   make rust
   export LD_LIBRARY_PATH=${PWD}/candle-binding/target/release
   ```
2. 跑决策引擎基准（最快、不依赖大模型）：
   ```bash
   cd perf
   go test -bench=BenchmarkEvaluateDecisions -benchmem ./benchmarks/
   ```
3. 对比已提交基线：
   ```bash
   make perf-compare     # 仅报告
   make perf-check       # 有回归则失败
   ```

**需要观察的现象**：

- `go test -bench` 输出每条 benchmark 的 `ns/op`、`B/op`（每次操作分配字节数）、`allocs/op`（每次操作分配次数）。
- `make perf-check` 会把当前结果与 `testdata/baselines/` 对比，对超阈值的 allocs/bytes 报 FAIL，对 ns/op 仅 WARN。

**预期结果**：决策引擎基准的 `ns/op` 很小（P95 < 1ms 的 SLO），且 `allocs/op` 通常为 0 或极少（热路径优化目标）。如果你故意在某处加一次不必要的 `fmt.Sprintf`，会看到 `allocs/op` 从 0 变 N，`make perf-check` 立即报回归——这正是门禁守护的东西。

> 待本地验证：`ns/op` 在不同机器差异大；`allocs/op` 与 `B/op` 应稳定可复现。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `perf/` 的 PR 门禁卡 `allocs/op`/`B/op` 而不卡 `ns/op`？用一句话解释「假阳性」风险。
> **答案**：`ns/op` 是绝对时间，随 CI 机器 CPU 与邻居负载波动；跨机器对比会因机器差异误报回归（假阳性），或为避免误报而故意放慢基线，反而掩盖真回归。`allocs/op`/`B/op` 由代码路径决定、对给定构建确定，跨机器可比，故适合做门禁。

**练习 2**：你在 `pkg/decision` 加了一个特性，本地跑 `BenchmarkEvaluateDecisions` 发现 `ns/op` 涨了 20%、`allocs/op` 没变。这个 PR 会被 `perf-check` 阻断吗？
> **答案**：不会。门禁只阻断 `allocs/op`/`B/op` 超阈值的回归；`ns/op` 是 advisory（默认阈值 30%，且仅报告不阻断）。20% 的 ns 涨幅会被报告为 advisory，但不让检查变红。当然，仍应排查是否真有性能问题。

**练习 3**：`perf/`（组件微基准）和 `bench/`（路由质量基准）的运行依赖有什么本质不同？
> **答案**：`perf/` 是纯 Go benchmark，不需要运行路由器或 LLM 后端，每个 PR 都能跑；`bench/` 测的是端到端路由准确率，需要真实运行的 LLM 后端来生成响应、评判质量，运行成本高，通常不在每个 PR 跑。

## 5. 综合实践

把本讲三个模块串起来，完成一次「训练 → 评估 → 上线加载」的完整追踪。

**任务**：以 `model_selection/ml_model_selection` 为对象，画出从「原始 query」到「路由器运行时调用选择模型」的完整数据流，并验证产物确实能被 Go 端加载。

**步骤**：

1. **训练侧**（模块 4.1）：写 5~10 条带 `category` 与 `ground_truth` 的 `queries.jsonl`，接一个本地 OpenAI 兼容端点，依次跑 `benchmark.py` → `train.py`，产出 `knn_model.json` 等四个文件。记录每步的输入字段与输出字段。
2. **评估侧**（模块 4.2）：用 `validate.go` 验证（需设好 candle-binding/ml-binding 的 `LD_LIBRARY_PATH`，见 [src/training/model_selection/ml_model_selection/README.md:360-386](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/training/model_selection/ml_model_selection/README.md#L360-L386)）。读懂输出表里 Oracle / ML 选择 / Random 的质量差异。
3. **加载侧**（横切）：跟踪 `knn_model.json` 如何进入运行时——
   - 阅读持久化层 [src/semantic-router/pkg/modelselection/persistence.go:456-474](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/modelselection/persistence.go#L456-L474)：`KNNSelector.Load` 读 JSON 文件，调 `ml_binding.KNNFromJSON` 装进 Rust/Linfa binding；KMeans/SVM 同构（[:542](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/modelselection/persistence.go#L542)、[:650](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/modelselection/persistence.go#L650)），MLP 走 candle-binding 的 `MLPFromJSON`（[candle-binding/semantic-router.go:4159-4160](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/candle-binding/semantic-router.go#L4159-L4160)）。
   - 阅读适配层 [src/semantic-router/pkg/selection/ml_adapter.go:28-42](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/selection/ml_adapter.go#L28-L42)：`MLSelectorAdapter` 把 `modelselection.Selector`（KNN/KMeans/SVM）适配成路由选择系统的 `selection.Selector` 接口；其 `Select` 方法把请求嵌入从 `[]float32` 转成 `[]float64` 再调底层选择器（[:51-100](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/selection/ml_adapter.go#L51-L100)）。
   - 在 [src/semantic-router/pkg/selection/factory.go:280](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/selection/factory.go#L280) 与 [:304](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/selection/factory.go#L304) 确认 KNN/MLP 被注册进选择算法 Registry。
4. **对比 LoRA 路径**：分类器的加载走另一条路——不是 JSON，而是 `model_discovery.go` 按目录命名约定自动发现 LoRA adapter。阅读 [src/semantic-router/pkg/classification/model_discovery.go:13-30](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/classification/model_discovery.go#L13-L30)（`ModelPaths` 持有三个 LoRA 分类器路径与架构名）与 [:55-75](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/classification/model_discovery.go#L55-L75)（`AutoDiscoverModelsWithRegistry` 扫描模型目录、`detectArchitectureFromPath` 识别 BERT/RoBERTa/ModernBERT）。

**产出**：一张端到端数据流图，标注「Python 训练 → JSON/safetensors 文件 → CGO binding 加载 → 选择算法/分类器调用」，并指出 LoRA 分类器（靠目录发现）与 ML 选择模型（靠 JSON 显式加载）两条加载路径的差异。

> 待本地验证：步骤 1/2 需要可用的 LLM 端点与构建好的 Rust 库；若环境受限，可只做步骤 3/4 的源码阅读部分。

## 6. 本讲小结

- `src/training/` 分四子目录：`model_classifier`（LoRA 分类器）、`model_embeddings`（嵌入）、`model_selection`（ML/RL 选择模型）、`model_eval`（评估），前两者是路由信号层的来源，第三个是选择算法的来源。
- LoRA 用 `W = W₀ + BA`（rank=16）把可训练参数砍 99%+，覆盖序列分类（意图/越狱）与 token 分类（PII）两种范式；每个训练目录配 Go 验证器做 Python↔Rust 数值一致性校验。
- ML 选择模型走「打基准（benchmark.py 测 performance+response_time）→ 训练（1038 维特征 = 1024 嵌入 + 14 类别 one-hot）→ 产出 JSON」流水线，共用 `score = 0.9·quality + 0.1·speed` 打分。
- 评估分三层：单模型质量（`mom_collection_eval.py`）、标准学术基准（MMLU-Pro/ARC）、生产路径验证（`validate.go` 用真实 Go/Rust 代码）；另有 `verify_text_classification_datasets.py` 用多 LLM 评委审计数据集标签。
- 训练产物进运行时有两条路：LoRA 分类器靠 `model_discovery.go` 按目录命名自动发现；ML 选择模型靠 `persistence.go` 显式读 JSON 经 CGO（ml-binding 的 `*FromJSON`、candle-binding 的 `MLPFromJSON`）加载，再由 `ml_adapter.go` 适配进选择系统。
- 性能基准严格二分：`perf/` 组件微基准每个 PR 跑、门禁只卡硬件无关的 `allocs/op`+`B/op`（`ns/op` 仅 advisory）；`bench/` 路由质量基准需要真实后端、测准确率与稳定性。

## 7. 下一步学习建议

- 想看清 LoRA 分类器加载后的推理细节，回到 **u12-l4（推理绑定）** 精读 candle-binding 的 CGO FFI 模式，对照本讲的 `KNNFromJSON`/`MLPFromJSON` 理解「Go 包装函数 + Rust 实现 + 配对 free」的统一纪律。
- 想理解选择算法在运行时如何被求值与排序，回到 **u6-l2（选择算法注册表）**，结合本讲的 `MLSelectorAdapter` 看 KNN/MLP 如何与 Elo/Hybrid 并列注册进同一 Registry。
- 想看路由质量基准（`bench/`）的端到端跑法，继续阅读 **u14-l1（E2E 测试框架）**——`bench/` 与 `e2e/` 共享部分集群与栈编排理念。
- 对 RL 选择感兴趣可深入 `src/training/model_selection/rl_model_selection/` 的 Router-R1/GMTRouter 训练脚本，并结合运行时 `pkg/selection/rl_driven.go` 的 `RLDrivenSelector` 阅读其在线推理路径。
