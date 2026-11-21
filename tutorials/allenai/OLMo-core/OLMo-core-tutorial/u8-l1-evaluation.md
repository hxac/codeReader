# 评测框架：Evaluator 与 LMEvaluator

## 1. 本讲目标

训练循环里的 loss 只能告诉你「模型在训练数据上拟合得好不好」，却回答不了「模型的能力有没有真实增长」。本讲拆解 OLMo-core 的 in-loop（训练中）评测框架，学完后你应该能够：

1. 说清 `Evaluator` 抽象基类的三段式协议（`update_metrics` → `compute_metrics` → `reset_metrics`）与它的批次迭代协议。
2. 读懂 `LMEvaluator` 如何按数据集标签（label）分桶统计交叉熵损失与困惑度（PPL），以及 `MeanMetric` 如何在分布式下自动归约。
3. 理解 `task_groups.py` 如何用任务名单单组织下游评测，`DownstreamEvaluator` 如何对接外部的 `olmo_eval` 库。
4. 会用 `EvaluatorCallback` / `LMEvaluatorCallbackConfig` / `DownstreamEvaluatorCallbackConfig` 把周期性评测挂进训练循环，并知道指标最终以什么名字被记录。

本讲承接 u6-l3（回调系统）：评测在 OLMo-core 里**不是训练循环正文的一部分，而是一个回调**。如果你还不熟悉 `Callback` 的钩子体系，建议先复习那一讲。

## 2. 前置知识

### 2.1 交叉熵损失与困惑度

语言模型在每个位置输出的是「下一个 token 的概率分布」。给定一串 token \( x_1, x_2, \dots, x_T \)，模型的质量用平均负对数似然（即交叉熵，CE）衡量：

\[
\text{CE} = -\frac{1}{T}\sum_{t=1}^{T} \log p(x_t \mid x_{<t})
\]

困惑度（perplexity，PPL）是它的指数形式：

\[
\text{PPL} = e^{\text{CE}}
\]

直觉解释：PPL ≈ 「模型每次猜下一个 token 时，实际在多少个候选里犹豫」。若模型把概率质量均匀撒在 50304 个词表上，CE ≈ ln(50304) ≈ 10.83，PPL ≈ 50304；如果真实数据其实只从 256 个 token 里均匀采样（数据熵为 ln 256 ≈ 5.55），那么一个学会了这个边缘分布的模型，CE 会降到 5.55 附近，PPL 降到 256 附近——**CE 的理论下界是数据本身的熵**，这个结论在综合实践里会直接用到。

### 2.2 in-loop 评测与训练 loss 的区别

- 训练 loss：来自训练数据、带梯度、每步都算，反映「拟合进度」，会被优化器直接拉低。
- in-loop 评测 loss：来自（通常是另存的）验证/评测数据、`torch.no_grad()`、每隔 N 步才跑，反映「泛化能力」，是模型选择与早停的依据。

### 2.3 你需要回忆的前置概念

- **回调钩子**（u6-l3）：`pre_train` / `post_step` / `post_train` 等钩子在 `Trainer.fit()` 的固定位置被触发；`EvaluatorCallback` 就是挂在 `post_step` 上的。
- **Duration**（u6-l1）：以 steps/tokens/epochs 三种单位计量时长，`Duration.due()` 判定到期。它既用于训练总长，也用于**截断评测循环**。
- **指标归约**（u6-l1）：`Trainer.record_metric()` 支持 `ReduceType` 跨 rank 归约；本讲会看到另一条归约路径——评测指标在 `MeanMetric.compute()` 里自己调 `all_reduce`。
- **meta data 标签**（u4-l1）：`NumpyPaddedFSLDataset` 可以给每个数据文件附一个 `metadata` 字典，collator 会把它逐实例地带进 batch——这是 `LMEvaluator` 按数据集分桶的钥匙。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/olmo_core/eval/evaluator.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/eval/evaluator.py) | `Evaluator` 抽象基类：批次迭代协议 + 指标三段式协议 |
| [src/olmo_core/eval/metrics.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/eval/metrics.py) | `Metric` / `MeanMetric`：可分布式归约的加权均值原语 |
| [src/olmo_core/eval/lm_evaluator.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/eval/lm_evaluator.py) | `LMEvaluator`：按 label 分桶计算 CE loss 与 PPL |
| [src/olmo_core/eval/task_groups.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/eval/task_groups.py) | 预置的下游评测任务名单（OLMES 风格任务名） |
| [src/olmo_core/train/callbacks/evaluator_callback.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/evaluator_callback.py) | `EvaluatorCallback`（评测执行引擎）与 `LMEvaluatorCallbackConfig` / `DownstreamEvaluator` / `DownstreamEvaluatorCallbackConfig` |

联动文件（本讲会引用但不展开）：

| 文件 | 本讲用到的部分 |
| --- | --- |
| [src/olmo_core/data/utils.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/utils.py) | `get_labels()`：从 batch 生成评估用 labels |
| [src/olmo_core/train/train_module/transformer/train_module.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py) | `eval_batch()`：评测专用的前向（逐 token 未归约 CE） |
| [src/olmo_core/nn/lm_head.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/lm_head.py) | `LMOutputWithLoss`：前向输出的四元组 |
| [src/test/eval/lm_evaluator_test.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/eval/lm_evaluator_test.py) | `LMEvaluator` 的单元测试（实践的依据） |
| [src/examples/llm/train.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/examples/llm/train.py) | 挂载评测回调的完整示例 |

包的门面导出见 [src/olmo_core/eval/__init__.py:L5-L9](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/eval/__init__.py#L5-L9)：`Evaluator`、`LMEvaluator`、`Metric`、`MeanMetric` 四个名字。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：①`Evaluator` 抽象与 `MeanMetric` 原语；②`LMEvaluator`；③`task_groups` 与 `DownstreamEvaluator`；④`EvaluatorCallback` 及其两个配置类。

### 4.1 Evaluator 抽象基类与 MeanMetric 指标原语

#### 4.1.1 概念说明

`Evaluator` 是「一次评测任务」的抽象：它知道**评测数据从哪来**（`batches` 或 `batches_factory`）、**每个 batch 前向后如何更新指标**、**最后如何汇总出最终数字**。它是纯抽象类（ABCMeta），库内有两个实现：`LMEvaluator`（算 PPL）和 `DownstreamEvaluator`（跑下游任务）。

`MeanMetric` 是支撑它的最小指标原语：一个可跨进程归约的**加权均值累加器**。任何「对一堆 per-token / per-instance 数值求平均」的评测指标（CE loss 就是典型）都可以用它实现。

#### 4.1.2 核心流程

`Evaluator` 的生命周期由回调驱动（谁驱动见 4.4）：

```text
for batch in evaluator:            # 迭代协议：必要时 reset + reshuffle 数据加载器
    前向计算 → evaluator.update_metrics(batch, ce_loss, logits)   # 累加
evaluator.compute_metrics()        # 汇总（含跨 rank 归约）→ Dict[指标名, 张量]
evaluator.reset_metrics()          # 清零，为下一轮评测做准备
```

`MeanMetric` 的数学语义：

\[
\text{mean} = \frac{\sum_i v_i w_i}{\sum_i w_i}
\]

其中 \( v_i \) 是观测值、\( w_i \) 是权重（默认全 1）。对 CE loss 来说，把每个 token 的 loss 作为 \( v_i \)、权重取 1，就得到逐 token 平均的 CE。

#### 4.1.3 源码精读

**构造函数：数据从哪来。** [src/olmo_core/eval/evaluator.py:L34-L55](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/eval/evaluator.py#L34-L55) 要求 `batches`（现成的可迭代对象，比如一个 `NumpyDataLoader` 或普通 list）与 `batches_factory`（延迟构建的工厂，`DownstreamEvaluator` 的 `lazy=True` 用它）二选一。`deterministic=True` 时的含义在类 docstring 里讲得很细：每次评测把数据加载器 reset 后用**固定 epoch=1** 重洗牌，保证被 `eval_duration` 截断的评测轮之间读到相同批次。

**迭代协议。** [src/olmo_core/eval/evaluator.py:L57-L75](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/eval/evaluator.py#L57-L75) 是 `__iter__`：如果 `batches` 是 `DataLoaderBase`，先 `reset()`（注释明确说：先清簿记再洗牌，让被截断的评测总是从 batch 0 重新开始），按 `deterministic` 决定是否固定 epoch 洗牌；迭代完再 `reset()` 一次收尾。普通 list 则直接迭代。

**三个抽象方法。** [src/olmo_core/eval/evaluator.py:L91-L118](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/eval/evaluator.py#L91-L118) 定义协议：

- `update_metrics(batch, ce_loss, logits)`：吃一个刚前向完的 batch。docstring 约定 `ce_loss` 是**未归约**的逐 token loss，形状 `(batch_size, seq_len - 1)`。
- `compute_metrics()`：输出最终指标，文档要求「已经归约好」。
- `reset_metrics()`：清零。

**MeanMetric 的累加。** [src/olmo_core/eval/metrics.py:L67-L79](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/eval/metrics.py#L67-L79) 把值和权重广播到同形后累加 `weighted_sum` 与 `weight` 两个标量——注意它只存**两个数字**，不管来的是标量还是一个大张量，这正是它能高效处理百万级 token 的原因。

**MeanMetric 的分布式归约。** [src/olmo_core/eval/metrics.py:L81-L89](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/eval/metrics.py#L81-L89) 在 `compute()` 里对 `weighted_sum` 和 `weight` 分别做 all-reduce 再相除。底层 [src/olmo_core/distributed/utils.py:L387-L403](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/utils.py#L387-L403) 的 `all_reduce_value` 有个重要性质：**未初始化分布式时原样返回**（单进程零开销退化）。两处细节值得注意：

- 各 rank 的 `weight`（token 数）通常不同，所以「先各自累加、最后全局除总 token 数」才是语义正确的平均——等价于 u7-l2 里张量并行损失归约的「除以全局有效 token 数」思想。
- 归约发生在 `compute()` 而不是每步 `update()`，评测循环内**零集合通信**，只在每轮评测结束时通信两次。

#### 4.1.4 代码实践

**实践目标**：在不碰任何模型的情况下，手动驱动 `MeanMetric` 与迭代协议，验证加权均值语义与单进程退化行为。

**操作步骤**（保存为任意 `.py` 文件直接 `python` 运行，只需要已安装的 olmo_core 环境，CPU 即可；以下为示例代码）：

```python
import torch
from olmo_core.eval import MeanMetric
from olmo_core.eval.evaluator import Evaluator

# 1) 加权均值：手算 (1*1 + 2*2 + 3*3) / (1+2+3) = 14/6
m = MeanMetric(device=torch.device("cpu"))
m.update(torch.tensor([1.0, 2.0, 3.0]), weight=torch.tensor([1.0, 2.0, 3.0]))
print("weighted mean =", m.compute().item())   # 期望 2.3333...

# 2) 两次 update 等价于拼起来一次 update
m.reset()
m.update(torch.tensor([1.0]), weight=1.0)
m.update(torch.tensor([2.0, 3.0]), weight=torch.tensor([2.0, 3.0]))
print("streaming mean =", m.compute().item())  # 仍应 2.3333...

# 3) Evaluator 的迭代协议对普通 list 是透明的
class ListLenEvaluator(Evaluator):
    def __init__(self, batches, **kw):
        super().__init__(batches=batches, **kw)
        self.n = 0
    def update_metrics(self, batch, ce_loss, logits):
        self.n += batch["input_ids"].numel()
    def compute_metrics(self):
        return {"total_tokens": torch.tensor(float(self.n))}
    def reset_metrics(self):
        self.n = 0

ev = ListLenEvaluator(name="demo", batches=[{"input_ids": torch.ones(2, 3, dtype=torch.long)}] * 4)
print("total tokens =", {k: v.item() for k, v in ev.compute_metrics().items()})  # 期望 24
```

**需要观察的现象**：`weighted mean` 与 `streaming mean` 输出一致（流式累加与批量计算等价）；`total_tokens` 为 24（4 个 batch × 6 token）。

**预期结果**：以上断言都应成立；`MeanMetric` 在未初始化分布式的单进程下不报错（`all_reduce_value` 静默退化）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `MeanMetric` 只保存 `weighted_sum` 和 `weight` 两个标量，而不是保存所有观测值最后再平均？
**答案**：评测一轮可能流过数百万个 token 的 loss 张量，存所有值会爆内存；两个标量的累加器内存 O(1)，且天然可 all-reduce（各 rank 的和直接相加即可），代价是 `compute()` 前无法做中位数等非线性统计。

**练习 2**：如果两个 rank 各自 `update` 了不同数量的 token，`compute()` 返回的是什么平均？
**答案**：全局逐 token 平均。all-reduce 后分子是全体的 \(\sum v_i w_i\)、分母是全体的 \(\sum w_i\)，等价于把所有 rank 的 token 汇在一起求均值，而不是「各 rank 均值的再平均」。

**练习 3**：`Evaluator.__iter__` 里 `deterministic=True` 时固定 `epoch=1` 重洗牌，解决什么问题？
**答案**：评测循环可能被 `eval_duration` 截断（比如只评 50 个 batch）。固定 epoch 洗牌保证每轮截断评测读到的都是**同一批、同序**的数据，跨轮指标才可比；否则每次重洗产生不同批次顺序，截断式评测的读数会抖动。

### 4.2 LMEvaluator：按数据集分桶的困惑度评估

#### 4.2.1 概念说明

`LMEvaluator` 是最常见的评测器：对一份（或多份拼在一起的）评测数据计算 CE loss 与 PPL。它的关键设计是**按 label 分桶**：评测数据集的每个文件在 `metadata` 里带一个 `"label"` 字段（如 `"c4-validation"`、`"wiki"`），一个 `LMEvaluator` 实例可以同时评多个数据集，每个 label 各自维护一个 `MeanMetric`，输出各自的 `"{label}/CE loss"` 与 `"{label}/PPL"`。这让你能用一份配置同时跟踪「模型在普通网页 vs 代码 vs 百科上的表现」。

#### 4.2.2 核心流程

一条评测数据的完整旅程：

```text
.npy 文件（带 metadata={"label": ...}）
  → NumpyPaddedFSLDataset.__getitem__()   # 每个实例 deepcopy 一份 metadata（numpy_dataset.py L698-700）
  → DataCollator                          # 把逐实例 metadata 堆成 batch["metadata"] 列表（collator.py L143-146, L175-176）
  → EvaluatorCallback.perform_eval()
      → get_labels(batch)                 # input_ids 左移生成 labels，pad/忽略位填 -100（data/utils.py L613-628）
      → train_module.eval_batch(batch, labels)   # 前向，loss_reduction="none" → 逐 token CE
      → evaluator.update_metrics(batch, ce_loss, logits)
            # 逐实例查 metadata["label"] 找到对应 MeanMetric；
            # 若有 label_mask，只统计 mask=True 的位置
      → evaluator.compute_metrics()       # 每 label 输出 CE loss 与 PPL = exp(CE)
```

数学上，对 label 为 \( L \) 的所有有效 token 集合 \( \mathcal{T}_L \)：

\[
\text{CE}_L = \frac{1}{|\mathcal{T}_L|}\sum_{t \in \mathcal{T}_L} -\log p(x_t \mid x_{<t}), \qquad \text{PPL}_L = e^{\text{CE}_L}
\]

#### 4.2.3 源码精读

**构造：每个 label 一个 MeanMetric。** [src/olmo_core/eval/lm_evaluator.py:L29-L39](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/eval/lm_evaluator.py#L29-L39) 把 `labels` 序列变成 `{label: MeanMetric}` 字典。类 docstring（[L14-L27](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/eval/lm_evaluator.py#L14-L27)）用 `.. important::` 强调了契约：**batch 必须含 `metadata` 列表，且每个字典有 `label` 字段**。

**工厂方法：从数据集一步构建。** [src/olmo_core/eval/lm_evaluator.py:L41-L92](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/eval/lm_evaluator.py#L41-L92) 的 `from_numpy_dataset` 做四件事：①扫描数据集所有文件的 metadata 收集 label 集合，缺 `label` 直接抛 `OLMoConfigurationError`（[L60-L66](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/eval/lm_evaluator.py#L60-L66)）；②`dataset.prepare()` 生成索引缓存；③按 DP 进程组的 world size/rank 切分数据（[L71-L84](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/eval/lm_evaluator.py#L71-L84)），保证多卡评测不重不漏；④组装 `LMEvaluator`。

**update_metrics：路由与掩码。** [src/olmo_core/eval/lm_evaluator.py:L94-L107](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/eval/lm_evaluator.py#L94-L107)：

```python
if ce_loss is None:
    return
for idx, (metadata, tokens_loss) in enumerate(zip(batch["metadata"], ce_loss)):
    metric = self.metrics[metadata["label"]]
    if "label_mask" in batch:
        tokens_loss = tokens_loss.masked_select(batch["label_mask"][idx])
    metric.update(tokens_loss)
```

注意 `ce_loss is None` 的场景（[L97-L101](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/eval/lm_evaluator.py#L97-L101) 的注释）：开启上下文并行（CP）或张量并行（TP）时，收集完整 logits 没有必要，前向只产出本 rank 分片上的逐 token CE——`LMEvaluator` 只依赖 `ce_loss`，天然兼容 CP/TP。这解释了 [train_module.py:L539-L544](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L539-L544) 里 `eval_batch` 的注释：「CP 和 TP 支持 PPL 评测，需要完整 logits 的下游评测会自然失败」。

**compute_metrics：CE 与 PPL 成对输出。** [src/olmo_core/eval/lm_evaluator.py:L109-L122](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/eval/lm_evaluator.py#L109-L122) 有个容易忽略的防御动作：先对每个 label 调一次 `metric.update(0.0, 0.0)`（值为 0、**权重也为 0**），保证「本轮没被轮到的 label」也能产出 NaN 而不是除零崩溃——多数据集评测被 `eval_duration` 截断时就会发生这种情况。指标名固定为 `f"{label}/CE loss"` 与 `f"{label}/PPL"`。

**reset_metrics。** [src/olmo_core/eval/lm_evaluator.py:L124-L126](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/eval/lm_evaluator.py#L124-L126) 逐 label 清零。

**前向侧的配合**（谁产出 `ce_loss`）：[train_module.py:L539-L581](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L539-L581) 的 `eval_batch` 与训练前向的关键差异：`loss_reduction="none"`（逐 token 不归约）、`torch.no_grad` 语境（由回调包）、CP/TP 下按分片裁剪 `label_mask` 并 `return_logits=False`。返回的 [lm_head.py:L192-L200](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/lm_head.py#L192-L200) 的 `LMOutputWithLoss` 四元组中，回调只取 `logits` 与 `ce_loss`。

#### 4.2.4 代码实践

**实践目标**：跑通 `LMEvaluator` 的官方单测，并手工驱动一次「无模型」的指标计算，验证 label 路由与 PPL 公式。

**操作步骤**：

1. 运行测试：

   ```bash
   pytest -v src/test/eval/lm_evaluator_test.py
   ```

2. 阅读测试的构造辅助 [lm_evaluator_test.py:L6-L9](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/eval/lm_evaluator_test.py#L6-L9)：`batches=iter([])`、labels=[\"c4\",\"wiki\"]——注意 `LMEvaluator` 可以**完全脱离模型**测试，因为 `update_metrics` 直接吃 `ce_loss` 张量。

3. 模仿 [lm_evaluator_test.py:L65-L75](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/eval/lm_evaluator_test.py#L65-L75)（多 label 路由）与 [L78-L88](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/eval/lm_evaluator_test.py#L78-L88)（PPL 断言），自己写一个三 label 的例子（示例代码）：

   ```python
   import torch
   from olmo_core.eval.lm_evaluator import LMEvaluator

   ev = LMEvaluator(
       name="demo",
       batches=iter([]),
       labels=["c4", "wiki", "code"],
       device=torch.device("cpu"),
   )
   ce = torch.tensor([[1.0, 2.0], [3.0, 4.0], [5.0, 6.0]])
   batch = {"metadata": [{"label": "c4"}, {"label": "wiki"}, {"label": "code"}]}
   ev.update_metrics(batch, ce_loss=ce, logits=None)
   for k, v in ev.compute_metrics().items():
       print(k, v.item())
   ```

**需要观察的现象**：pytest 全绿（9 个测试）；自定义脚本输出三对指标：`c4/CE loss=1.5`、`wiki/CE loss=3.5`、`code/CE loss=5.5`，且每个 `PPL` 恰为对应 CE 的 `exp`（如 `c4/PPL ≈ 4.4817`）。

**预期结果**：如上；若把某个 label 从 batch 的 metadata 里删掉（但保留在 `labels` 参数中），该 label 的输出是 NaN（dummy update 技巧生效），其余不受影响——这正是 [lm_evaluator_test.py:L105-L115](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/eval/lm_evaluator_test.py#L105-L115) 锁定的行为。

#### 4.2.5 小练习与答案

**练习 1**：`update_metrics` 里 `masked_select(label_mask)` 过滤掉的位置会以什么方式影响最终 CE？
**答案**：被过滤的位置既不进入 `weighted_sum` 也不进入 `weight`，等价于从平均里剔除——即 padding、文档拼接边界等「不该计损失」的位置不污染指标。对照 [lm_evaluator_test.py:L118-L131](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/eval/lm_evaluator_test.py#L118-L131)：两个实例各选 2 个位置，均值是 4 个值的平均 4.5。

**练习 2**：为什么 PPL 要在 `compute_metrics` 里用 `torch.exp(ce_loss)` 算，而不是在 update 时对每个 token 先取 exp 再平均？
**答案**：PPL 定义为 \( e^{\text{CE}} \)，即**先算术平均再指数**；若先 exp 再平均得到的是「几何平均概率的倒数」的另一种（错误的）统计，两者数值差异巨大（Jensen 不等式）。

**练习 3**：CP/TP 开启时 `logits` 为 None、`ce_loss` 是本 rank 分片上的局部值，`LMEvaluator` 为什么仍然正确？
**答案**：label_mask 与 ce_loss 按同样的方式分片（[train_module.py:L548-L564](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/train_module.py#L548-L564)），每个 rank 只 update 自己分片上的 token loss；`MeanMetric.compute()` 的 all-reduce 把各 rank 的部分和与 token 数加总后相除，结果等于全序列的平均。

### 4.3 task_groups 与 DownstreamEvaluator：下游能力评测的组织

#### 4.3.1 概念说明

PPL 只衡量「像不像训练分布」，不直接衡量「会不会做题」。下游评测（downstream eval）把模型放到具体任务上——选择题（ARC、MMLU）、生成式问答（SQuAD、GSM8K）、多语言编程（MT-MBPP）——得到准确率、F1、BPB 等能力指标。

OLMo-core 对此的分工是：**任务逻辑在外部库 `olmo_eval`**（可选依赖，import 放在函数体内），OLMo-core 只负责两件事：

1. [task_groups.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/eval/task_groups.py) 维护**任务名单单**（纯字符串列表，无任何依赖）；
2. `DownstreamEvaluator`（定义在 evaluator_callback.py 里）把 `olmo_eval` 的任务数据集包装成 `Evaluator` 协议，接进 `EvaluatorCallback`。

任务名的语法大致是 `{数据集}_{split}_{指标}_{shot 数}[_fast]`，例如 `mmlu_stem_val_mc_5shot_fast`、`gsm8k_gold_bpb_5shot`。指标缩写可对照 `DownstreamEvaluator.metric_type_to_label` 映射表理解：`bpb_v1` 是 bits per byte（生成式、按字节归一化的负对数似然），`acc_v1`/`len_norm_v1`/`pmi_dc_v1` 是几种选择题打分方式（准确率、长度归一化准确率、PMI-DC 准确率）。`rc`/`mc` 是 OLMES 评测体系里两种多选作答方式的缩写（精确展开待确认，可查 olmo_eval 文档）；`_fast` 后缀表示更快的子集/实现（待确认）。

#### 4.3.2 核心流程

`DownstreamEvaluator` 的构建与使用流程：

```text
DownstreamEvaluatorCallbackConfig.build(trainer)
  → HFTokenizer(tokenizer_config.identifier)          # 用训练分词器实例化 olmo_eval 的分词器
  → for task in sorted(tasks):
        DownstreamEvaluator(task=task, batch_spec=train_module.eval_batch_spec, ...)
            → 校验 task ∈ olmo_eval.list_tasks()      # _all_tasks() 有 @cache，只查一次
            → (lazy=False 时立即) _build_data_loader():
                  build_task(task, tokenizer, model_ctx_len=...)   # 任务数据集 + 它声明的 metric_type
                  ICLMetric(metric_type=...)                        # 指标计算器
                  DistributedSampler(...)                           # DP 各 rank 切分，drop_last=False
                  DataLoader(task_dataset, collate_fn=task_dataset.collate_fn, ...)
  → EvaluatorCallback(evaluators=[...], ...)
```

评测执行时与 `LMEvaluator` 共用同一个回调循环，但 `update_metrics` 只吃 `logits`（把 ce_loss 直接 `del`）。

#### 4.3.3 源码精读

**任务名单。** [task_groups.py:L4-L77](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/eval/task_groups.py#L4-L77) 的 `FULL_TASKS_SMALL_COMPUTE` 面向「不指望模型学会做选择题」的短训练（注释原话），大量任务用 `_rc_` 变体与 BPB 生成式指标；[L80-L125](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/eval/task_groups.py#L80-L125) 的 `FULL_TASKS_LARGE_COMPUTE` 面向长训练，选择题换成 `_mc_..._fast` 变体。文件顶部的注释（如 [L8-L9](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/eval/task_groups.py#L8-L9) hellaswag 用 1K 子集、[L13-L15](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/eval/task_groups.py#L13-L15) boolq/openbookqa「噪声太大不值得跟踪」）本身就是经验之谈，值得一读。[L165-L170](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/eval/task_groups.py#L165-L170) 汇总成 `TASK_GROUPS` 字典：`full__small_compute`、`full__large_compute`、`full`（前两者并集去重，[L128](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/eval/task_groups.py#L128)）、`fast`（约快 2-3 倍的子集，[L131-L162](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/eval/task_groups.py#L131-L162)）。

**DownstreamEvaluator 的构造。** [evaluator_callback.py:L333-L363](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/evaluator_callback.py#L333-L363) 先校验任务名（`_all_tasks()` 来自 [L306-L310](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/evaluator_callback.py#L306-L310)，`@cache` 保证 `olmo_eval.list_tasks()` 只调用一次）；`lazy=True` 时把 `batches_factory` 设为 `_build_data_loader`，把「拉取任务数据」推迟到第一次评测——几十个任务的初始化成本可观，lazy 让「训练到第 N 步才第一次评测」的 run 不必在启动时就下载数据。

**数据加载器构建。** [evaluator_callback.py:L369-L446](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/evaluator_callback.py#L369-L446)：`build_task` 按 `EvalBatchSpec` 的 `max_sequence_length` 裁剪任务上下文；序列长度超限直接抛 `OLMoConfigurationError`（[L403-L410](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/evaluator_callback.py#L403-L410)）；tokens 计的 batch size 要换算成实例数（[L412-L431](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/evaluator_callback.py#L412-L431)，fixed_sequence_length 时还要求整除）；分布式时用 `DistributedSampler`（[L393-L401](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/evaluator_callback.py#L393-L401)）。`EvalBatchSpec`（评估批大小按 tokens 还是 instances 计）定义在 [train_module.py:L27-L70](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/train_module.py#L27-L70)。

**指标更新与输出。** [evaluator_callback.py:L448-L459](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/evaluator_callback.py#L448-L459) 的 `update_metrics` 与 `LMEvaluator` 恰好互补：`del ce_loss`，**必须有完整 logits**，否则抛 RuntimeError 并明确提示「CP/TP 下请关闭下游评测」。[L461-L468](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/evaluator_callback.py#L461-L468) 把 `olmo_eval` 的 ICLMetric 结果按 [L314-L331](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/evaluator_callback.py#L314-L331) 的 `metric_type_to_label` 映射成 `"{task} ({指标中文名})"` 形式的键。

#### 4.3.4 代码实践（源码阅读型）

**实践目标**：不用安装 `olmo_eval`（task_groups.py 零依赖），用一个小脚本考察任务组之间的关系。

**操作步骤**（示例代码）：

```python
from olmo_core.eval.task_groups import (
    FAST_TASKS, FULL_TASKS, FULL_TASKS_LARGE_COMPUTE,
    FULL_TASKS_SMALL_COMPUTE, TASK_GROUPS,
)

print("组名 -> 任务数:", {k: len(v) for k, v in TASK_GROUPS.items()})
only_fast = set(FAST_TASKS) - set(FULL_TASKS)
print("在 fast 但不在 full 里的任务:", sorted(only_fast))
shared = set(FULL_TASKS_SMALL_COMPUTE) & set(FULL_TASKS_LARGE_COMPUTE)
print("两个 full 组共有的任务数:", len(shared), sorted(shared)[:5])
```

**需要观察的现象**：`full` 的任务数小于两个 full 组之和（去重）；`only_fast` **非空**——例如 `arc_challenge_test_bpb_5shot` 出现在 `FAST_TASKS`（[task_groups.py:L133](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/eval/task_groups.py#L133)），但两个 FULL 组里只有它的 `rc`/`mc` 变体；两个 full 组共有的任务如 `hellaswag_rc_5shot`、`winogrande_val_rc_5shot`。

**预期结果**：如上；结论是 `fast` 并非 `full` 的严格子集，选组时要按名字而不是想当然。具体输出数值待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `DownstreamEvaluator.update_metrics` 在 CP/TP 下抛错，而 `LMEvaluator` 不受影响？
**答案**：下游选择题指标（如长度归一化准确率）需要**完整词表维度的 logits** 给每个候选答案打分，CP/TP 下收集完整 logits 代价过高，`eval_batch` 干脆不返回（`return_logits=False`）；PPL 只需逐 token CE，天然可分片。

**练习 2**：`lazy=True` 解决什么问题？
**答案**：`batches_factory` 把任务数据集的构建（含下载/预处理）推迟到第一次真正评测时。几十个下游任务如果在训练启动时全部构建，会显著拖慢启动并抢占内存；对 `eval_interval` 很大或用 `eval_on_finish` 的 run，惰性构建几乎零成本。

**练习 3**：`FULL_TASKS_SMALL_COMPUTE` 里大量任务为什么用 BPB 而不是准确率？
**答案**（结合文件注释）：短训练/小算力的模型还没学会做选择题，准确率接近随机、读数全是噪声；BPB 这类生成式指标对任何模型都有连续、低方差的读数，更适合跟踪早期趋势。

### 4.4 EvaluatorCallback：把评测接入训练循环

#### 4.4.1 概念说明

`EvaluatorCallback` 是评测的**执行引擎**：它持有若干个 `Evaluator`，在训练的特定时机（每隔 `eval_interval` 步、固定步点、开训前、结束前）驱动每个评测器完整跑一轮「迭代 → 前向 → update → compute → record」。两个配套的 `CallbackConfig`（可序列化的配方）负责从配置构建出 `LMEvaluator` / `DownstreamEvaluator` 并包进这个回调——这正是 u6-l3 讲过的「双层模式」：回调实例不可序列化，配置类才是能写进 YAML / 被 dotlist 覆盖的那一层。

#### 4.4.2 核心流程

```text
训练中每个 step 结束
  → EvaluatorCallback.post_step():
        step <= 1 直接跳过
        若 step % eval_interval == 0 或 step ∈ fixed_steps → perform_eval()
pre_train():  eval_on_startup  → perform_eval()
post_train(): eval_on_finish   → perform_eval()

perform_eval(prefix="eval"):
  for evaluator in evaluators:
      evaluator.reset_metrics()
      for batch in evaluator:                      # 迭代协议（4.1）
          batch → device；no_grad
          labels = get_labels(batch)               # 左移 + -100
          output = train_module.eval_batch(...)    # 逐 token CE
          evaluator.update_metrics(batch, ce_loss, logits)
          统计 token 数；eval_duration.due(step, tokens, epoch=1) 为真则 break  # 截断
      metrics = evaluator.compute_metrics()
      for name, value in metrics.items():
          trainer.record_metric(f"{prefix}/{evaluator.name}/{name}", value)
  记录 throughput/in-loop eval time (s) 与 eval batches（MetricMergeStrategy.sum）
  cancel_after_first_eval 为真 → trainer.cancel_run(no_sync=True)
```

指标全名 = `"{prefix}/{evaluator.name}/{指标名}"`。对 `LMEvaluatorCallbackConfig` 构建的评测器（`name="lm"`，[L287](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/evaluator_callback.py#L287)），label 为 `c4-validation` 时即 `eval/lm/c4-validation/CE loss` 与 `eval/lm/c4-validation/PPL`。

#### 4.4.3 源码精读

**回调字段。** [evaluator_callback.py:L49-L97](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/evaluator_callback.py#L49-L97)：`evaluators` 列表、`eval_interval`（默认 1000 步）、`fixed_steps`（定点评测）、`eval_on_startup` / `eval_on_finish`、`cancel_after_first_eval`（配合 `eval_on_startup=True` 可实现「只评测不训练」，常用于评测已训练的 checkpoint）、`eval_duration`（默认 1 epoch，可截断评测循环）、`log_interval`（控制台进度日志频率）。

**触发时机。** [evaluator_callback.py:L113-L120](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/evaluator_callback.py#L113-L120) 的 `post_step`：`step <= 1` 跳过（第 1 步模型刚更新一次，评了没意义且拖慢启动），之后按 interval 或 fixed_steps 触发。`pre_train`/`post_train` 见 [L105-L111](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/evaluator_callback.py#L105-L111)。`post_attach`（[L98-L103](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/evaluator_callback.py#L98-L103)）校验 train module 必须是 `TransformerTrainModule` / `OLMoDDPTrainModule`，因为它要调用 `eval_batch`。

**评测主循环。** [evaluator_callback.py:L139-L166](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/evaluator_callback.py#L139-L166)：核心四行是

```python
labels = get_labels(batch)
output = self.trainer.train_module.eval_batch(batch, labels=labels)
logits, _, ce_loss, _ = output
evaluator.update_metrics(batch, ce_loss, logits)
```

外层 `torch.no_grad()`，token 数按 `batch["input_ids"].numel() * dp_world_size` 估算（喂给 `Duration.tokens` 型截断）。截断判断在 [L161-L163](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/evaluator_callback.py#L161-L163)，`Duration.due` 的语义见 [common.py:L94-L105](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/common.py#L94-L105)（steps/tokens 用 `>=`，epochs 用严格 `>`——所以 `Duration.epochs(1)` 传 `epoch=1` 时**不**到期，跑完整个数据集才自然结束）。

**指标记录。** [evaluator_callback.py:L170-L187](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/evaluator_callback.py#L170-L187)：compute 后逐指标 `trainer.record_metric(f"{prefix}/{evaluator.name}/{name}", value)`（`record_metric` 的签名见 [trainer.py:L1244-L1266](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L1244-L1266)），随后由 ConsoleLogger/WandB 等消费者回调接管展示。这里**没有传 reduce_type**——因为 `compute_metrics()` 已经在 `MeanMetric` 里归约过了，这是与训练指标（`ReduceType.mean`）不同的归约路径。

**评测速度也是指标。** [evaluator_callback.py:L189-L211](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/evaluator_callback.py#L189-L211) 记录 `throughput/in-loop eval time (s)` 与 `throughput/in-loop eval batches`，用 `MetricMergeStrategy.sum` 合并（同名指标多次记录时求和而非告警）——评测开销可观测，方便权衡 eval_interval。

**只评不训。** [evaluator_callback.py:L213-L217](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/evaluator_callback.py#L213-L217)：`cancel_after_first_eval` 时调 `trainer.cancel_run(..., no_sync=True)`，注释说明所有 rank 同时到达这里所以无需再同步。

**LMEvaluatorCallbackConfig.build。** [evaluator_callback.py:L239-L303](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/evaluator_callback.py#L239-L303) 做三件事：①校验数据集序列长度不超过 train module 的 eval 上限（[L250-L258](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/evaluator_callback.py#L250-L258)）；②把 `EvalBatchSpec` 换算成**全局** eval batch size（tokens 直接乘 DP world size，instances 还要乘序列长度，[L260-L272](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/evaluator_callback.py#L260-L272)）；③要求 eval_dataset 构建产物是 `NumpyPaddedFSLDataset`、trainer 的 data_loader 是文本类加载器（复用它的 collator），然后 `LMEvaluator.from_numpy_dataset(...)` 并包进 `EvaluatorCallback`。

**DownstreamEvaluatorCallbackConfig.build。** [evaluator_callback.py:L489-L530](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/evaluator_callback.py#L489-L530)：要求 `tokenizer.identifier` 非空（要实例化真实的 `olmo_eval.HFTokenizer`），逐任务构建 `DownstreamEvaluator`（`sorted(tasks)` 保证确定性顺序）。

**真实用法。** [examples/llm/train.py:L229-L250](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/examples/llm/train.py#L229-L250) 展示了两个配置类并列挂载的写法：`lm_evaluator` 用 c4 验证集每 250 步评 50 个 batch（`Duration.steps(50)`），`downstream_evaluator` 每 250 步跑 hellaswag。另一个高级用法：[model_merger.py:L312-L322](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/model_merger.py#L312-L322) 的模型合并回调直接复用 `perform_eval(prefix="eval/merged")`，让合并后的模型用**不同前缀**记指标、与正常 eval 并列可比——`prefix` 参数就是为这种复用设计的。

#### 4.4.4 代码实践

**实践目标**：零成本查看评测回调的完整配置形态，验证 dry-run 不需要数据与 GPU。

**操作步骤**：

```bash
python src/examples/llm/train.py my-eval-demo --dry-run
```

在打印出的配置树中找到 `trainer.callbacks` 节点下的 `lm_evaluator` 与 `downstream_evaluator`，对照源码逐字段确认：`eval_interval=250`、`eval_duration` 的序列化形态、`eval_dataset.metadata` 里的 `[{'label': 'c4-validation'}]`。

**需要观察的现象**：配置树中能看到两个评测回调的完整字段；dry-run 在 `prepare_training_environment()` 之前直接返回（[examples/llm/train.py:L319-L321](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/examples/llm/train.py#L319-L321)），所以不需要下载数据、不需要 GPU。可再试 `--model-factory=olmo2_14M --sequence-length=1024` 观察配置联动变化。

**预期结果**：如上；`downstream_evaluator` 的 `tasks=['hellaswag']` 出现在配置里。若想真实运行 downstream 部分，需要安装可选依赖 `olmo_eval`（待本地验证其在当前环境的可安装性）。

#### 4.4.5 小练习与答案

**练习 1**：`perform_eval` 里为什么在 `update_metrics` 外层包 `cuda_sync_debug_mode(0)`、在 `compute_metrics` 外也包一次？
**答案**：`update_metrics` 里 `masked_select` 等操作配合 `.item()` 式访问会产生 host-device 同步；评测循环不是性能关键路径，源码注释明确说「这里可能有 host-device 同步，但没关系」。`cuda_sync_debug_mode(0)` 临时关闭 CUDA 同步调试告警，避免评测期间的合法同步刷屏（该工具本身在 u5-l3 的 grad_debug 语境里出现过）。

**练习 2**：想让「模型合并前 vs 合并后」的指标在同一张表里对比，怎么做？
**答案**：沿用 ModelMergerCallback 的做法：正常评测走 `perform_eval(prefix="eval")`，合并后的模型用 `perform_eval(prefix="eval/merged")`。指标名前缀不同（`eval/lm/...` vs `eval/merged/lm/...`），其余路径完全一致，天然可并列比较。

**练习 3**：`eval_duration=Duration.epochs(1)`（默认）时循环何时停？
**答案**：`Duration.due(epoch=1)` 对 epochs 单位用**严格大于**判定，`epoch=1` 时永不为真；循环会把数据加载器的全部 batch 评完自然结束。只有 steps/tokens 型 `eval_duration` 才会中途 break。

## 5. 综合实践

**任务**：造一份「有规律」的合成数据，用 `LMEvaluatorCallbackConfig` 把 `LMEvaluator` 挂进一个微型训练，观察指标 `eval/lm/synthetic-eval/CE loss` 与 `PPL` 随训练下降并逼近理论下界。

**设计思路**：词表有 50304 个 token，但合成数据只从其中前 256 个里均匀采样。随机初始化的模型 CE ≈ ln(50304) ≈ 10.83；随着模型学会边缘分布，CE 应向数据熵 ln(256) ≈ 5.55 下降、PPL 向 256 下降——这就是「评测指标可被理论解释」的最小实验。

**操作步骤**：

1. 保存以下脚本（示例代码，CPU 可跑，无需 torchrun；文件名如 `mini_eval_demo.py`，不要放进仓库源码目录）：

   ```python
   import os
   import tempfile

   import numpy as np

   from olmo_core.data import (
       NumpyDataLoaderConfig,
       NumpyFSLDatasetConfig,
       NumpyPaddedFSLDatasetConfig,
       TokenizerConfig,
   )
   from olmo_core.nn.transformer import TransformerConfig
   from olmo_core.optim import AdamWConfig
   from olmo_core.train import (
       Duration,
       TrainerConfig,
       prepare_training_environment,
       teardown_training_environment,
   )
   from olmo_core.train.callbacks import LMEvaluatorCallbackConfig
   from olmo_core.train.train_module import TransformerTrainModuleConfig
   from olmo_core.utils import seed_all

   SEQ_LEN = 64
   WORK_DIR = tempfile.mkdtemp(prefix="olmo-eval-demo-")

   def make_token_file(path: str, n_tokens: int, seed: int):
       """生成无文件头的 uint32 token 数组（必须用 np.memmap，不能用 np.save，见 u4-l1）。"""
       rng = np.random.default_rng(seed)
       arr = np.memmap(path, dtype=np.uint32, mode="w+", shape=(n_tokens,))
       arr[:] = rng.integers(0, 256, size=n_tokens)  # 只用前 256 个 token
       arr.flush()
       del arr

   def main():
       train_path = os.path.join(WORK_DIR, "train.npy")
       eval_path = os.path.join(WORK_DIR, "eval.npy")
       make_token_file(train_path, 64 * 1024, seed=0)
       make_token_file(eval_path, 8 * 1024, seed=1)

       tokenizer_config = TokenizerConfig.gpt2()
       model_config = TransformerConfig.olmo2_1M(
           vocab_size=tokenizer_config.padded_vocab_size()
       )

       dataset_config = NumpyFSLDatasetConfig(
           paths=[train_path], sequence_length=SEQ_LEN,
           tokenizer=tokenizer_config, work_dir=WORK_DIR,
       )
       data_loader_config = NumpyDataLoaderConfig(global_batch_size=SEQ_LEN * 8, seed=0)
       train_module_config = TransformerTrainModuleConfig(
           rank_microbatch_size=SEQ_LEN * 8,
           max_sequence_length=SEQ_LEN,
           optim=AdamWConfig(lr=1e-3),
       )
       trainer_config = (
           TrainerConfig(
               save_folder=os.path.join(WORK_DIR, "checkpoints"),
               save_overwrite=True,
               max_duration=Duration.steps(60),
               metrics_collect_interval=1,
           )
           .with_callback(
               "lm_evaluator",
               LMEvaluatorCallbackConfig(
                   eval_dataset=NumpyPaddedFSLDatasetConfig(
                       paths=[eval_path],
                       metadata=[{"label": "synthetic-eval"}],
                       sequence_length=SEQ_LEN,
                       tokenizer=tokenizer_config,
                       work_dir=WORK_DIR,
                   ),
                   eval_on_startup=True,              # 训练前先评一次，得到基线
                   eval_interval=20,
                   eval_duration=Duration.steps(8),    # 每轮只评 8 个 batch
               ),
           )
       )

       prepare_training_environment()   # 单进程下走兜底逻辑（u7-l3）
       seed_all(1234)
       model = model_config.build()
       train_module = train_module_config.build(model)
       dataset = dataset_config.build()
       data_loader = data_loader_config.build(
           dataset, dp_process_group=train_module.dp_process_group
       )
       trainer = trainer_config.build(train_module, data_loader)
       trainer.fit()
       teardown_training_environment()

   if __name__ == "__main__":
       main()
   ```

2. 运行 `python mini_eval_demo.py`，盯住控制台日志中 `Running lm evals...` / `Finished lm evals ... Metrics:` 的输出块。

**需要观察的现象**：

1. 评测共触发 4 次：`eval_on_startup`（pre_train，step=0 时）+ step 20 / 40 / 60（`post_step`）。
2. 每次输出 `synthetic-eval/CE loss` 与 `synthetic-eval/PPL`，以及 `[eval=lm,step=k/...]` 进度行和评测耗时统计。
3. 指标趋势：基线 CE ≈ 10.8 上下（随机模型），随训练下降，向 ln 256 ≈ 5.55 逼近；PPL 从数万量级向 256 逼近。

**预期结果**：eval 指标单调（近似）下降且与理论值方向一致；训练 loss 同步下降。若数值不降，优先检查 `metadata` 是否带 `label`（`from_numpy_dataset` 会直接抛 `OLMoConfigurationError`）。具体下降幅度与绝对数值**待本地验证**（与随机种子、步数有关）；若 CPU 上 60 步太慢，可把 `d_model` 再调小（例如给工厂方法传 `d_model=...` 覆盖，见 u3-l2 的 kwargs 覆盖模式）。

## 6. 本讲小结

- **评测是回调不是循环正文**：`EvaluatorCallback` 挂在 `post_step`（外加 `pre_train`/`post_train` 钩子），按 `eval_interval`/`fixed_steps` 驱动所有 `Evaluator`，前向计算全部委托 `train_module.eval_batch()`。
- **Evaluator 协议 = 迭代协议 + 三段式指标协议**：`__iter__` 负责数据加载器的 reset/reshuffle（`deterministic=True` 固定 epoch 保证截断式评测跨轮可比），`update_metrics → compute_metrics → reset_metrics` 定义指标生命周期。
- **LMEvaluator 按 label 分桶**：数据集 metadata 里的 `label` 经 collator 进入 batch，逐实例路由到各自的 `MeanMetric`；输出 `"{label}/CE loss"` 与 `"{label}/PPL"`（= exp(CE)）；只需逐 token CE，天然兼容 CP/TP。
- **MeanMetric 是 O(1) 内存的分布式加权均值**：只累加两个标量，`compute()` 时一次性 all-reduce——评测循环内零集合通信；单进程下静默退化。
- **下游评测外置给 olmo_eval**：task_groups.py 是零依赖的任务名单（`TASK_GROUPS` 四个组，`fast` 不是 `full` 的严格子集）；`DownstreamEvaluator` 需要完整 logits，CP/TP 下不可用，`lazy=True` 可推迟任务数据构建。
- **指标全名有固定语法**：`"{prefix}/{evaluator.name}/{指标名}"`，默认 prefix 为 `eval`，模型合并回调用 `eval/merged` 复用同一套评测；评测耗时本身也作为 `throughput/in-loop eval *` 指标被记录。

## 7. 下一步学习建议

- **u8-l2（生成与 Chat）**：本讲的评测都停留在「给序列打分」；下一讲进入 `generate/` 包，看模型如何自回归地生成文本、采样参数如何影响输出，那是评测生成能力的另一面。
- **继续阅读源码**：对照官方脚本中评测的实际用法——[src/scripts/official/OLMo3/OLMo-3-1025-7B-pretrain-1.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/scripts/official/OLMo3/OLMo-3-1025-7B-pretrain-1.py) 里 `LMEvaluatorCallbackConfig` 与 `DownstreamEvaluatorCallbackConfig`（含 `TASK_GROUPS` 的选用）如何组合进真实配方；以及 [src/olmo_core/train/callbacks/model_merger.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/model_merger.py) 如何以 `prefix` 复用 `perform_eval`。
- **动手方向**：写一个自定义 `Evaluator`（例如统计「模型对第 1 个 token 与最后 1 个 token 的 loss 差」），挂进 `EvaluatorCallback` 跑通——它只需要实现三个方法，这是检验你是否真正理解本讲协议的最好方式。
