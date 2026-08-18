# 置信度头评估：ECE、AUROC、Brier 与可靠性图

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释 DSpark 置信度头在推理期的两个用途：按阈值提前截断低置信提议、以及离线度量自身校准质量。
2. 读懂 `ConfidenceHeadRecorder` 的完整生命周期：`start → observe → finish → report/plot/table`。
3. 手算 ECE、Brier score（含 Murphy 分解）与直方图版 AUROC，并说清 `PerPositionConfidenceMetrics` 为什么用「逐位置直方图 + all_reduce」的实现方式。
4. 说明 `--confidence-threshold` 非 0 时为什么关闭校准记录（审查/选择偏置）。
5. 会看 `reliability_diagram.png` 与 `metrics.json`，能据 `ece@pos`、`auc@pos`、`brier@pos` 判断草稿模型置信度的可用性。

## 2. 前置知识

### 2.1 从 u4-l4 承接：置信度头在训练时学什么

u4-l4 已讲过：DSpark 的置信度头 `AcceptRatePredictor` 是一个单线性层，训练时以 detach 后的逐槽位接受率 \( \alpha_t = 1 - \mathrm{TV}(p_t, q_t) \) 为 BCE 目标（[deepspec/modeling/dspark/loss.py:156-163](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/loss.py#L156-L163)），其中接受率由草稿/目标分布的 L1 距离算出（[deepspec/modeling/dspark/loss.py:60-70](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/loss.py#L60-L70)）。也就是说，头被训练成「预测当前这个槽位的 token 有多大概率会被目标模型接受」。

本讲的的问题是：**训练完之后，我们怎么知道这个头预测得准不准？** 这就是概率校准（calibration）评估。

### 2.2 什么是校准

一个二分类器输出概率 \( p \in (0,1) \)，若在所有输出 \( p \approx 0.8 \) 的场合里，真实正例频率确实约 0.8，则称其「校准良好」。校准好不代表分辨力强（全输出基率也校准好），所以通常要同时看：

- **分辨力**：能否把正负例排开 —— AUROC。
- **校准误差**：预测概率与观测频率差多少 —— ECE、Brier。

### 2.3 本讲用到的三个指标

把 \([0,1]\) 切成 \( B \) 个桶，第 \( b \) 桶落入 \( n_b \) 个样本，桶内平均预测 \( \bar p_b \)、观测正例率 \( \bar y_b \)，\( N=\sum_b n_b \)：

\[ \mathrm{ECE} = \sum_{b=1}^{B} \frac{n_b}{N}\,\left| \bar p_b - \bar y_b \right| \]

\[ \mathrm{BS} = \frac{1}{N}\sum_{i=1}^{N} (p_i - y_i)^2 \]

Brier 可分解为（Murphy 分解，二分类情形）：

\[ \mathrm{BS} = \underbrace{\frac{1}{N}\sum_b n_b(\bar p_b-\bar y_b)^2}_{\text{可靠性 RELI}} - \underbrace{\frac{1}{N}\sum_b n_b(\bar y_b-\bar y)^2}_{\text{分辨力 RESOL}} + \underbrace{\bar y(1-\bar y)}_{\text{不确定度 UNC}} \]

直觉：RELI 越小越准、RESOL 越大越能区分、UNC 是数据本身的难度下限。

AUROC 的概率含义：随机取一正一负，正例预测值高于负例的概率（相等记 0.5）。本仓库用细桶直方图近似计算（见 4.2.3）。

### 2.4 为什么评估对象是 cumprod 前缀概率

u6-l3 已证明：验证阶段第 \( t \) 个槽位被接受是独立概率事件（条件于前缀），且 `accept_prefix_mask` 由逐位接受掩码 `cumprod` 得到（[deepspec/eval/base_evaluator.py:240-258](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L240-L258)）。因此：

- 头的逐槽位预测 \( \sigma_t \)（sigmoid 概率）的连乘 \( \prod_{i \le t}\sigma_i \) 是「前 t+1 个草稿全被接受」这一事件的预测概率；
- 该事件的真值是一个 0/1 标签，就是 `accept_prefix_mask[:, t]`；
- 二者恰好构成一组 (预测概率, 0/1 标签) 对，可套用上面全部校准指标。数学上 \( \mathbb{E}[\text{prefix\_mask}_t] = \prod_{i\le t}\alpha_i \)，与 cumprod 预测同构，这是「逐位置评估 cumprod 预测」合理性的根源。

训练侧其实已经埋了对应的监控量 `confidence_cumprod_bias`（[deepspec/modeling/dspark/loss.py:172-181](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/loss.py#L172-L181)），本讲是它在真实解码动态下的完整评估版。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [deepspec/eval/dspark/confidence_head.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/confidence_head.py) | 本讲主角：`PerPositionConfidenceMetrics`（指标累积与计算）、`ConfidenceHeadRecorder`（生命周期编排）、`summarize_confidence_row`/`build_table`（汇总）、`plot_reliability_diagram`（画图） |
| [deepspec/eval/dspark/evaluator.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/evaluator.py) | 把记录器挂进 DSpark 评估器：`_build_confidence_head_recorder` 决定是否启用，`_post_verify` 钩子喂数据，`evaluate` 编排每个数据集的生命周期 |
| [deepspec/eval/dspark/draft_ops.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/draft_ops.py) | 阈值早停侧：`_predict_confidence_logits`、`_confident_prefix_length`、`build_dspark_proposal` 的截断逻辑 |
| [deepspec/modeling/dspark/qwen3/modeling.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py) | 建模侧置信度头的构造（`AcceptRatePredictor`）与 `predict_confidence_step` 前向 |
| [deepspec/modeling/dspark/common.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/common.py) | `AcceptRatePredictor` 定义（结构极简的单线性层） |
| [deepspec/eval/base_evaluator.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py) | `VerificationResult` 数据合同、`accept_prefix_mask`/`effective_proposal_length` 的产生、`post_verify` 钩子的调用点 |
| [eval.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/eval.py) | `--confidence-threshold` 命令行参数（默认 0.0） |

## 4. 核心概念与源码讲解

### 4.1 ConfidenceHeadRecorder：把校准评估挂进解码循环

#### 4.1.1 概念说明

`ConfidenceHeadRecorder` 是一个「旁路观测器」：它不参与解码，只在每次验证结束后被动地看一眼 (提议, 验证结果)，把置信度预测与真实接受前缀对齐记录下来。这样设计的好处是：

- 解码主循环（u6-l2 的 `generate_decoding_sample`）完全不用改，只要通过既有的 `post_verify` 钩子回调即可；
- 记录器自己管理「每个数据集一套累积器」的生命周期，多卡各自累积、结束时一次性 all_reduce 汇总。

它的启用条件由评估器决定，而不是自己决定——这是一个「谁构造谁负责策略」的分工。

#### 4.1.2 核心流程

```text
Qwen3DSparkEvaluator.__init__
  └─ _build_confidence_head_recorder()          # 三个条件决定返回 None 还是记录器
evaluate()  # 对每个数据集
  ├─ recorder.start()                           # 新建一套 PerPositionConfidenceMetrics
  ├─ run_dataset(...)                           # 逐样本解码
  │    └─ generate_decoding_sample(..., post_verify=self._post_verify)
  │         └─ verify_draft_tokens(...)         # 产出 VerificationResult
  │         └─ post_verify → recorder.observe(proposal, verification)
  ├─ recorder.finish(dataset_name, metric_summary)
  │    ├─ metrics.all_reduce()                  # 六个张量跨 rank 求和
  │    └─ rank0 组装 row（per_position 列表）
  └─ recorder.report_dataset(...)               # 打 JSON、写 metrics.json、画可靠性图
全部数据集结束后
  ├─ recorder.print_results()                   # PrettyTable 汇总表
  └─ recorder.log_tensorboard()                 # 逐位置标量写入 TB
```

#### 4.1.3 源码精读

**启用条件——两个 None。** 评估器构造时决定是否建记录器：

- [deepspec/eval/dspark/evaluator.py:44-49](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/evaluator.py#L44-L49)：如果草稿模型没有置信度头（DFlash / 未开 `enable_confidence_head` 的配置），或者 `--confidence-threshold != 0.0`，直接返回 `None`，后续所有钩子都变成空操作。
- [deepspec/eval/dspark/evaluator.py:50-66](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/evaluator.py#L50-L66)：构造记录器。桶数是模块级常量 `CONFIDENCE_NUM_BINS = 20`（粗桶，服务 ECE 与可靠性图）和 `CONFIDENCE_NUM_FINE_BINS = 1000`（细桶，服务 AUROC），定义在 [deepspec/eval/dspark/evaluator.py:28-29](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/evaluator.py#L28-L29)。只有传了 `--tensorboard-dir` 才设 `artifact_root`（`tensorboard_dir/artifacts/step_{step}`），否则不落盘文件。

`--confidence-threshold` 参数本身定义在 [eval.py:36-41](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/eval.py#L36-L41)，默认 0.0，help 文本明确写了「只在 0.0 时才收集校准指标」。取值范围在建模处断言为 [0,1]（[deepspec/eval/dspark/evaluator.py:81](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/evaluator.py#L81)）。

**喂数据的钩子。** [deepspec/eval/dspark/evaluator.py:149-160](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/evaluator.py#L149-L160)：`_post_verify` 只做类型断言和转发。它在主循环中的调用点是 [deepspec/eval/base_evaluator.py:404-405](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L404-L405)——紧接 `verify_draft_tokens` 之后、游标推进之前，保证看到的是「刚验证完」的最原始结果。

**observe：cumprod 预测 vs 二元前缀标签。** 这是本讲最核心的 20 行：

- [deepspec/eval/dspark/confidence_head.py:345-359](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/confidence_head.py#L345-L359)：两次提前返回——空提议（`draft_token_count <= 0`，u6-l4 讲过的置信度截到 0 的情形）和 `effective_proposal_length <= 0`。然后断言 `confidence_logits` 与 `accept_prefix_mask` 非空。
- [deepspec/eval/dspark/confidence_head.py:360-371](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/confidence_head.py#L360-L371)：`bsz=1` 所以 `squeeze(0)` 只去掉单序列维；先截断到 `effective_proposal_length` 再算——源码注释说明这是为了跳过「已接受 EOS 之后」的位置（那是 u6-l3 里 `effective_proposal_length` 因停止 token 截短提议的情形，属于合法截断）。随后 `sigmoid → float64 → cumprod` 得到前缀预测，`accept_prefix_mask` 转 float64 得到 0/1 标签。
- [deepspec/eval/dspark/confidence_head.py:372-375](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/confidence_head.py#L372-L375)：调用 `dataset_metrics.update`。

注意一个精妙的对应：训练时头的 BCE 目标是**逐槽位**接受率 \( \alpha_t \)，而这里评估的是**连乘后的前缀概率**对**前缀 0/1 标签**。两者不是重复——逐槽位概率在推理时无法直接观测（单个 token 要么接受要么不接受，没有频率可言），而前缀事件在大量提议上 empirically 有频率，所以连乘后才可评估。

**数据合同。** 记录器不 import `DSparkDraftProposal`，而是依赖一个结构化协议：

- [deepspec/eval/dspark/confidence_head.py:20-22](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/confidence_head.py#L20-L22)：`ConfidenceProposal` 只要求 `draft_token_count` 和 `confidence_logits` 两个属性。任何满足该形状的提议类型都能被记录，评估侧耦合度降到最低。

`DSparkDraftProposal` 本体在 [deepspec/eval/dspark/draft_ops.py:17-19](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/draft_ops.py#L17-L19)，只是给通用 `DraftProposal` 加了一个可选 `confidence_logits` 字段；`VerificationResult` 的字段清单见 [deepspec/eval/base_evaluator.py:174-183](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L174-L183)。

**finish 与产物落盘。** [deepspec/eval/dspark/confidence_head.py:377-397](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/confidence_head.py#L377-L397)：先 all_reduce，再把 `self.dataset_metrics` 置 None（防复用），只有 rank 0 且样本数非零才组装 row。row 结构由 [deepspec/eval/dspark/confidence_head.py:399-413](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/confidence_head.py#L399-L413) 给出：`dataset / sample_count / proposal_count / draft_name / per_position`。

[deepspec/eval/dspark/confidence_head.py:415-440](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/confidence_head.py#L415-L440) 的 `report_dataset` 打印一行紧凑 JSON（`compact_row` 会去掉 `reliability` 明细、并追加 `summarize_confidence_row` 的加权均值，见 [deepspec/eval/dspark/confidence_head.py:222-229](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/confidence_head.py#L222-L229)）；[deepspec/eval/dspark/confidence_head.py:460-481](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/confidence_head.py#L460-L481) 的 `write_dataset_outputs` 把 `{config, spec, confidence, confidence_summary}` 写进 `metrics.json` 并顺手画可靠性图。

评估器侧的生命周期编排集中在 [deepspec/eval/dspark/evaluator.py:181-211](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/evaluator.py#L181-L211)（`start` → `run_dataset` → `finish` → `report_dataset`），收尾的 TB 与表格入口在 [deepspec/eval/dspark/evaluator.py:213-221](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/evaluator.py#L213-L221)。

#### 4.1.4 代码实践

**实践目标**：不跑真模型，直接用真实的 `ConfidenceHeadRecorder` 类复现一次 `observe`，验证「cumprod 预测 vs 前缀标签」的对齐方式。

**操作步骤**（以下为示例代码，保存为仓库外任意路径如 `/tmp/observe_demo.py` 运行）：

```python
import torch
from types import SimpleNamespace
from deepspec.eval.base_evaluator import VerificationResult
from deepspec.eval.dspark.confidence_head import ConfidenceHeadRecorder

torch.manual_seed(0)
B = 7  # block_size，模拟 max_proposal_tokens

# 模拟一次提议：3 个槽位的 confidence logits
proposal = SimpleNamespace(
    draft_token_count=3,
    confidence_logits=torch.tensor([[2.2, 1.4, -0.7]]),  # [1, 3]
)
# 模拟一次验证：前两个槽位被接受、第三个被拒
prefix = torch.tensor([[1, 1, 0]])
verification = VerificationResult(
    target_output=None, target_probs=torch.empty(1),
    accept_prefix_mask=prefix,               # [1, 3]
    accepted_draft_tokens=2, next_token=torch.tensor([5]),
    effective_proposal_length=3,
)

recorder = ConfidenceHeadRecorder(
    device=torch.device("cpu"), max_proposal_tokens=B,
    num_bins=20, num_fine_bins=1000,
    draft_name_or_path="toy/draft", tensorboard_dir=None,
    step=None, artifact_root=None,
)
recorder.start()
recorder.observe(proposal=proposal, verification=verification)
rows = recorder.dataset_metrics.compute()   # 不走 finish()，绕开 all_reduce
for r in rows[:3]:
    print(r["position"], r["total_weight"], round(r["pred_mean"], 4), r["target_mean"])
```

**需要观察的现象**：位置 0 的 `pred_mean ≈ sigmoid(2.2) ≈ 0.90`、`target_mean = 1.0`；位置 1 `pred_mean ≈ 0.90 × sigmoid(1.4) ≈ 0.84`、`target_mean = 1.0`；位置 2 `pred_mean ≈ 0.84 × sigmoid(-0.7) ≈ 0.21`、`target_mean = 0.0`。每个位置的 `total_weight` 都是 1（一次提议在每个位置贡献权重 1）。

**预期结果**：三条记录的 `brier` 分别约为 0.01、0.03、0.04（平方误差）；由于单样本 ECE 只有 0/1 两个极端桶，看 `pred_mean`/`target_mean` 的对齐比看 ECE 更直观。数值可用手算 sigmoid 验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `observe` 里 `confidence_logits` 只有 `[1, L]` 却要 `squeeze(0)` 而不是 `reshape(-1)`？

**答案**：`squeeze(0)` 只移除 batch 维（源码注释说明 `generate_one_sample` 强制 bsz=1），保留序列维以便 `cumprod(dim=0)` 沿槽位方向连乘；`reshape(-1)` 在未来若放宽 batch 会静默打平错误维度，且丢失「沿哪一维连乘」的语义。

**练习 2**：如果把 `observe` 中「截断到 `effective_proposal_length`」这行去掉，指标会怎么错？

**答案**：当一个块内先被接受的 token 里出现停止符时，`effective_proposal_length` 会小于块长（[deepspec/eval/base_evaluator.py:262-276](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L262-L276)），其后的位置在解码意义上根本不存在，但 `accept_prefix_mask`/`confidence_logits` 张量上仍有值。不截断就会把这些「生成从未发生」的位置计入直方图，污染 `auc@pos`、`ece@pos`。

---

### 4.2 PerPositionConfidenceMetrics：直方图式 ECE / AUROC / Brier

#### 4.2.1 概念说明

`PerPositionConfidenceMetrics` 解决的问题是：**在不保存任何原始样本的前提下，跨数万次提议、跨多卡累积出逐位置的校准指标**。如果每轮把 (概率, 标签) 全存下来再算，内存和通信都不可行；而 ECE、Brier、直方图 AUROC 都可以由「桶计数」充分统计量（sufficient statistics）增量计算——这正是 u3-l6「分子分母分离累积」思想的再次应用。

「逐位置」是因为块内槽位 0..B-1 的统计特性天然不同：越靠后的槽位累积接受率越低、预测概率的分布也越偏。混在一起算会掩盖这一点，所以每个位置独立维护一整套直方图。

#### 4.2.2 核心流程

累积（每 rank 本地，`update` 每次一条提议）：

```text
输入: probs[B']（cumprod 前缀预测）, targets[B']（0/1 前缀标签），B' ≤ B
1. probs 钳制到 [1e-8, 1-1e-8]，全部转 float64
2. 粗桶 idx = floor(probs * 20)，scatter_add 累积三个量:
   coarse_count[pos, b] += 1
   coarse_pred[pos, b]  += p
   coarse_target[pos, b]+= y
3. 细桶 idx = floor(probs * 1000)，累积:
   fine_pos[pos, b] += y      （正例计数）
   fine_neg[pos, b] += 1 - y  （负例计数）
4. brier_num[pos] += (p - y)^2
```

汇总（`finish` 时一次性）：

```text
1. 六个张量 all_reduce(SUM)
2. 对每个位置 pos:
   N      = Σ_b coarse_count[pos, b]
   avg_pred / avg_target = 桶内求和 / 桶计数
   ece    = Σ_b (n_b/N) |avg_pred_b - avg_target_b|
   brier  = brier_num[pos] / N
   auc    = 直方图法（见 4.2.3）
   reliability = [(bin, range, avg_pred, avg_target, weight), ...]
```

#### 4.2.3 源码精读

**累积器的形状。** [deepspec/eval/dspark/confidence_head.py:30-61](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/confidence_head.py#L30-L61)：六个 float64 张量——粗桶三元组 `coarse_count/coarse_pred/coarse_target` 形状 `[B, 20]`、细桶二元组 `fine_pos/fine_neg` 形状 `[B, 1000]`、Brier 分子 `[B]`。float64 是为了在巨大计数下不损失精度。

**update：一次 scatter_add 三连。** [deepspec/eval/dspark/confidence_head.py:63-75](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/confidence_head.py#L63-L75)：概率先 `clamp(EPS_PROB, 1-EPS_PROB)`（`EPS_PROB = 1e-8`，[deepspec/eval/dspark/confidence_head.py:16-17](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/confidence_head.py#L16-L17)），避免 p=0/1 落到越界桶；权重全为 1（`weights = torch.ones_like`），即每次提议在每个位置权重 1。

[deepspec/eval/dspark/confidence_head.py:77-92](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/confidence_head.py#L77-L92)：分桶公式 `(probs * num_bins).long().clamp_(0, num_bins-1)`，再用 `pos_idx * num_bins + bin_idx` 把二维下标压平成一维 scatter_add。细桶同理累积正/负例计数，Brier 分子按位置直接累加平方误差。注意 `update` 断言 `pos_count <= block_size`，即调用方必须先截到有效长度。

**直方图 AUROC。** [deepspec/eval/dspark/confidence_head.py:105-114](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/confidence_head.py#L105-L114)：静态方法 `_auroc_from_hist`，公式为

\[ \mathrm{AUC} = \frac{\sum_b \text{pos}_b \cdot \text{cumneg}_{<b} + \tfrac12\sum_b \text{pos}_b\cdot\text{neg}_b}{P \cdot N} \]

分子第一项是「正例严格高于负例」的对数，第二项把同桶（近似同分）的对数记 0.5——这是 Mann-Whitney U 统计量的分组近似。桶宽 1/1000 让同桶平局几乎只发生在真正相近的概率上。任一类为空返回 `nan`（如最深槽位可能全是被拒后的 0 标签，正例极少）。

**compute：逐位置产出字典。** [deepspec/eval/dspark/confidence_head.py:116-141](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/confidence_head.py#L116-L141)：`total_weight` 近零的位置输出全 `nan` 占位（空槽直接跳过）；否则按 4.2.2 的公式算 `ece/auc/brier/pred_mean/target_mean`。[deepspec/eval/dspark/confidence_head.py:143-171](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/confidence_head.py#L143-L171) 再附上 `reliability` 明细（只含非空桶，记录 `bin/range/avg_pred/avg_target/weight`）——这正是画可靠性图的原料。

**all_reduce。** [deepspec/eval/dspark/confidence_head.py:94-103](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/confidence_head.py#L94-L103)：六个张量统一 SUM。因为桶计数是线性的充分统计量，先本地累积再一次性归约与集中计算完全等价，同时把通信从 O(样本数) 降到 O(6·B·1000)。

**跨位置汇总。** [deepspec/eval/dspark/confidence_head.py:175-212](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/confidence_head.py#L175-L212)：`summarize_confidence_row` 对 `per_position` 按各自 `total_weight` 加权平均得到 `ece_mean/auc_mean/brier_mean/pred_mean/target_mean`。注意 `auc_mean` 单独用非 nan 位置的权重和归一（`auc_w`），避免 nan 传染。

**两张输出视图。** [deepspec/eval/dspark/confidence_head.py:541-600](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/confidence_head.py#L541-L600) 的 `build_table` 生成 PrettyTable：前几列是 `dataset/draft_model/samples/proposals` 加五个加权均值，后面拼上每个位置的 `ece@pos` 与 `auc@pos` 列（无数据的位置打 `-`）；[deepspec/eval/dspark/confidence_head.py:483-539](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/confidence_head.py#L483-L539) 的 `log_tensorboard` 把同样的量写成 `confidence/{dataset}/ece@{pos}` 等标量（nan 经 [deepspec/eval/dspark/confidence_head.py:307-310](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/confidence_head.py#L307-L310) 的 `add_tensorboard_scalar` 过滤，避免 TB 报错）。

#### 4.2.4 代码实践

**实践目标**：用 500 个自造 (预测概率, 0/1 标签) 对手写 10 桶 ECE 与 Brier，并和「逐位置分别算再加权平均」的结果对比，体会 `PerPositionConfidenceMetrics` 的两种聚合层次。

**操作步骤**（示例代码）：

```python
import torch

torch.manual_seed(42)

def ece_brier(probs, targets, num_bins=10):
    probs = probs.clamp(1e-8, 1 - 1e-8)
    idx = (probs * num_bins).long().clamp_(0, num_bins - 1)
    n = torch.zeros(num_bins, dtype=torch.float64)
    sp = torch.zeros(num_bins, dtype=torch.float64)
    sy = torch.zeros(num_bins, dtype=torch.float64)
    n.scatter_add_(0, idx, torch.ones_like(probs, dtype=torch.float64))
    sp.scatter_add_(0, idx, probs.double())
    sy.scatter_add_(0, idx, targets.double())
    total = n.sum()
    avg_p, avg_y = sp / n.clamp_min(1), sy / n.clamp_min(1)
    ece = ((avg_p - avg_y).abs() * n).sum() / total
    brier = ((probs.double() - targets.double()) ** 2).mean()
    return float(ece), float(brier)

# 造两“位置”：位置 0 校准良好且偏自信，位置 1 系统性过自信
p0 = torch.distributions.Beta(6, 2).sample((250,)).double()
p1 = torch.distributions.Beta(3, 3).sample((250,)).double() * 0.5 + 0.3
y0 = (torch.rand(250) < p0).double()
y1 = (torch.rand(250) < (p1 - 0.25).clamp(0.02, 0.98)).double()  # 真实频率低于预测

e0, b0 = ece_brier(p0, y0)
e1, b1 = ece_brier(p1, y1)
ep, bp = ece_brier(torch.cat([p0, p1]), torch.cat([y0, y1]))
w = 250 / 500
print(f"per-pos:  ece@0={e0:.4f} ece@1={e1:.4f}  加权={w*e0 + w*e1:.4f}")
print(f"brier@0={b0:.4f} brier@1={b1:.4f}  加权={w*b0 + w*b1:.4f}")
print(f"pooled :  ece={ep:.4f}  brier={bp:.4f}")
```

**需要观察的现象**：`ece@0` 很小（位置 0 是按校准良好的机制造的），`ece@1` 明显更大；**合并后 pooled ECE 通常大于逐位置加权 ECE**——因为两个位置的预测分布整体错位（位置 1 整体过自信），混合后同一桶里两群样本互相拉偏，类似辛普森效应。

**预期结果**：具体数值随机种子下可复现（seed=42 固定），定性结论稳定：`pooled ece > 0.5·ece@0 + 0.5·ece@1`、`brier` 两种聚合相等（Brier 是逐样本量，线性可加，与分桶无关——这本身就是一个值得写进笔记的对照点）。这解释了为什么该模块坚持逐位置维护直方图而不是合并一个大直方图。

#### 4.2.5 小练习与答案

**练习 1**：手算。预测 \( p = [0.9, 0.8, 0.7, 0.3] \)，标签 \( y = [1, 0, 1, 0] \)。求 10 桶 ECE、Brier，并做 Murphy 分解验证。

**答案**：每个点各占一桶（0.9→桶9，0.8→桶8，0.7→桶7，0.3→桶3），每桶 \( n_b=1 \)。
- ECE = ¼(|0.9−1| + |0.8−0| + |0.7−1| + |0.3−0|) = (0.1+0.8+0.3+0.3)/4 = **0.375**
- Brier = (0.01+0.64+0.09+0.09)/4 = **0.2075**
- 分解：RELI = 0.2075（每桶单点，(p−y)² 即桶偏差）；RESOL = ¼Σ(ȳ_b−ȳ)² = ¼(0.25×4) = 0.25；UNC = 0.5×0.5 = 0.25。验证 0.2075 − 0.25 + 0.25 = 0.2075 ✓。

**练习 2**：同样的 4 个点，手算 AUROC。

**答案**：正例 {0.9, 0.7}，负例 {0.8, 0.3}，共 4 个 (正,负) 对：0.9>0.8 ✓、0.9>0.3 ✓、0.7<0.8 ✗、0.7>0.3 ✓。AUC = **3/4 = 0.75**。若改用 1000 细桶直方图法，四点分属不同桶、无同桶平局，结果一致。

**练习 3**：为什么粗桶 20 个而 AUROC 用 1000 个细桶？

**答案**：ECE 与可靠性图需要每桶有足够样本量，桶太多则每桶样本稀疏、`avg_target` 噪声大；AUROC 依赖「正例比负例高多少」的相对排序，桶太粗会把大量本可分出高低的样本对当同分（0.5 记分），系统性压低 AUC，因此用细桶减少平局损失。两个粒度各服务各的指标，互不将就。

---

### 4.3 可靠性图与阈值早停

#### 4.3.1 概念说明

可靠性图（reliability diagram）是把每个桶的 (avg_pred, avg_target) 画成折线、并以柱状图叠加桶权重的诊断图：完全校准时折线落在对角线上，折线在对角线下方即「过自信」。本仓库为**每个槽位单独画一张子图**，标题直接带上该位置的 ECE/AUC/均值——一眼看出「第几个 token 开始不可信」。

阈值早停是置信度头的推理期应用：逐槽位看 sigmoid 置信度，遇到第一个低于阈值的槽位就把块截断在那里，只把高置信前缀送去验证。动机：被拒的草稿 token 会让目标模型的 KV cache 白白计算再被 crop 掉（u6-l2），低置信提议是纯浪费。

#### 4.3.2 核心流程

阈值早停（发生在 `build_dspark_proposal` 内，先于验证）：

```text
1. 采样整块 draft tokens（markov 头逐槽位修正后的采样）
2. 若有置信度头:
     prev_token_ids = [锚点, sampled[:-1]]        # 与训练时 teacher forcing 对齐
     confidence_logits = predict_confidence_step(...)
     k = _confident_prefix_length(confidence_logits, threshold)
        # k = 第一个 sigmoid < threshold 的下标；threshold<=0 时 k = B
3. k == 0 → 空提议（只验证锚点，目标直采 bonus token）
4. 否则 verify_input_ids / draft_probs / confidence_logits 全部截到前 k 个
```

可靠性图绘制（发生在 `finish` 之后的 rank 0）：

```text
1. n 个槽位 → 最多 3 列的子图网格
2. 每个子图: 对角虚线参考 + (avg_pred→avg_target) 折线 + 右轴桶权重柱
3. 标题写 pos / ECE / AUC / mean_pred / mean_target
4. 存为 <artifact_root>/<dataset>/reliability_diagram.png
```

#### 4.3.3 源码精读

**置信度 logits 的产生。** [deepspec/eval/dspark/draft_ops.py:57-79](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/draft_ops.py#L57-L79)：`_predict_confidence_logits` 先拼 `prev_token_ids = [draft_input_ids[:, :1], sampled_tokens[:, :-1]]`——锚点 token 打头、采样结果右移，与 u4-l3 讲的训练期 teacher forcing 前驱完全同构；随后调用模型的 `predict_confidence_step` 并 reshape 成 `[1, block_size]`。

建模侧 [deepspec/modeling/dspark/qwen3/modeling.py:292-307](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L292-L307)：`confidence_head_with_markov` 开启时特征是「块隐状态 ⊕ markov 前驱 embedding」拼接，否则只用隐状态；训练 forward 里的同款逻辑见 [deepspec/modeling/dspark/qwen3/modeling.py:504-516](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L504-L516)，头本身的构造在 [deepspec/modeling/dspark/qwen3/modeling.py:254-267](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L254-L267)（输入维度按是否带 markov 加 `markov_rank`）。头结构是货真价实的一个线性层：[deepspec/modeling/dspark/common.py:43-49](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/common.py#L43-L49) 的 `AcceptRatePredictor`，`nn.Linear(input_dim, 1)` 输出 logit。

**截断规则。** [deepspec/eval/dspark/draft_ops.py:82-93](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/draft_ops.py#L82-L93)：`_confident_prefix_length` 注意是**逐槽位** sigmoid 与阈值比较（不是 cumprod 后再比）——因为每个槽位被独立接受，前缀置信度由验证方逐位判定，截断只需找到第一个「这一步就不自信」的位置。`threshold <= 0.0` 直接返回整块长，即阈值功能整体关闭。

**截到 0 = 空提议。** [deepspec/eval/dspark/draft_ops.py:115-134](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/draft_ops.py#L115-L134)：`proposal_draft_tokens` 初始为 `block_size`，有头时被覆盖为置信前缀长度；等于 0 时走 `_empty_dspark_proposal`——`verify_input_ids` 只剩锚点、`draft_probs=None`，主循环退化为「仅验证锚点 + 目标直采 bonus token」（u6-l4 已分析过该路径）。[deepspec/eval/dspark/draft_ops.py:136-153](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/draft_ops.py#L136-L153) 是最终截断后的组装，注意返回的 `confidence_logits` 也截到前 k 位——所以阈值开启时记录器本来也拿不到被截掉位置的 logits（这是 4.3.5 讨论「为何关闭记录」的代码证据）。

**为什么 threshold ≠ 0 就关闭记录器。** 回到 [deepspec/eval/dspark/evaluator.py:44-49](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/evaluator.py#L44-L49)。两个原因：

1. **审查（censoring）**：阈值把每个块截到第一个低置信槽位为止，其后的位置既不出现在 `confidence_logits` 里、也永远不会被提议和验证。低置信桶因此几乎没有样本，`ece@pos`、`auc@pos` 在这些桶上不再是该校准意义的估计。
2. **选择偏置**：存活下来的 (预测, 结果) 对是「模型自己挑过的」条件分布——被观测到的提议以高置信为前提，其接受率分布与无条件分布不同。用它算出的 ECE/AUROC 会系统性偏乐观，恰恰在最需要诊断（头到底什么时候不可信）的场景失真。

对比 4.1.3 里 `observe` 对 `effective_proposal_length` 的截断：那是「生成确实终止」的客观截断，删掉的是逻辑上不存在的位置；而阈值截断是主动丢弃「可能会拖累指标」的样本，二者性质完全不同。所以代码把两者分开处理：EOS 截断照做，阈值开启则干脆不记录。

**画图。** [deepspec/eval/dspark/confidence_head.py:232-247](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/confidence_head.py#L232-L247)：`plot_reliability_diagram` 用 Agg 后端（无显示环境）、按最多 3 列排子图网格；[deepspec/eval/dspark/confidence_head.py:249-278](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/confidence_head.py#L249-L278)：每个子图先画对角参考线，再以桶中心为 x、`avg_target` 为 y 画折线，`twinx` 双轴叠加桶权重柱（半透明），右轴隐藏刻度只作直方图示意；[deepspec/eval/dspark/confidence_head.py:284-304](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/confidence_head.py#L284-L304)：标题两行写全 `pos/ECE/AUC/mean_pred/mean_target`，空槽关轴，最终存为 `dataset_dir/reliability_diagram.png`（文件名常量 [deepspec/eval/dspark/confidence_head.py:16-17](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/confidence_head.py#L16-L17)）。

读图口诀：**折线贴对角线 = 校准好；整体沉在对角线下方 = 过自信（这正是要配阈值截断的形态）；柱状集中在低概率区 = 该位置基本不产出可用草稿；AUC 低于 0.6 = 头在该位置几乎没有分辨力，阈值策略收益有限。**

#### 4.3.4 代码实践

**实践目标**：跑一次 `--confidence-threshold 0.6` 的评测，与 0.0 基线对比 `accept_len` / `verify_rate` 的变化，并确认阈值模式下校准输出消失。

**操作步骤**（需要 GPU 与已发布的 DSpark checkpoint，参照 [scripts/eval/eval.sh](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/eval/eval.sh) 的启动方式）：

1. 先跑基线（阈值 0.0，等价于不传该参数）：
   ```bash
   CUDA_VISIBLE_DEVICES=0 python eval.py \
     --target_name_or_path Qwen/Qwen3-4B \
     --draft_name_or_path deepseek-ai/dspark_qwen3_4b_block7
   ```
2. 再跑阈值版，额外传 `--confidence-threshold 0.6`。
3. 为控制变量，可临时把 `eval.py` 的 `TASKS` 改为 `[("gsm8k", 30)]` 缩短时长（实验后还原，不要提交该改动）。

**需要观察的现象**：

- 阈值版的终端输出中**不再出现** `Confidence head reliability metrics:` 表格和每数据集的紧凑 JSON 行（因为记录器为 `None`，[deepspec/eval/dspark/evaluator.py:218-221](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/evaluator.py#L218-L221) 的分支被跳过）。
- 常规投机解码指标表仍在。重点对比三列：`accept_len`（每轮平均提交数）、`verify_rate`（草稿命中率，满足 u6-l1 的恒等式 `verify_rate=(accept_len−1)/(n̄+1)`）、有效提议长度 `n̄`。

**预期结果**：待本地验证。定性预期：0.6 的阈值会截短提议（n̄ 下降），`verify_rate` 上升（留下的都是高置信 token），`accept_len` 的净变化取决于截断是否「砍得准」——阈值偏小时 accept_len 可能不升反降。这正是 DSpark 论文里置信度调度所权衡的量，建议在 0.0 / 0.4 / 0.6 / 0.8 四档上各跑一次再下结论。

#### 4.3.5 小练习与答案

**练习 1**：`_confident_prefix_length` 为什么对**逐槽位** sigmoid 比较，而不是对 cumprod 前缀概率比较？

**答案**：验证方的接受是逐槽位独立判定（u6-l3），一个前缀能否全保留取决于每一位；若用 cumprod 比较，阈值含义会随位置漂移（越靠后 cumprod 越小，同样的 0.6 在深位置几乎必然触发），截断会系统性地只保留浅前缀。逐槽位比较让「每一步的自信度」标准一致，与训练时逐槽位 BCE 目标语义对齐。

**练习 2**：假设某模型 `auc@0 = 0.91` 但 `auc@4 = 0.55`，`ece@4 = 0.3`。你会怎么用这组数字？

**答案**：头在前几槽位分辨力强、校准好，适合设阈值（如 0.6）截断；第 4 槽位起头接近随机猜测且严重失准，置信度信息不可用——应把 `max_proposal_tokens`（即 `block_size`）直接调小到 4~5，而不是依赖阈值在第 4 位之后止损。可靠性图各槽位子图正是为这种「逐位置找拐点」的读法设计的。

---

## 5. 综合实践

**任务：给一份 DSpark checkpoint 出一份「置信度体检报告」。**

1. **基线评测**：按 4.3.4 的命令对 `deepseek-ai/dspark_qwen3_4b_block7` 跑一次阈值 0.0 的评测，带 `--tensorboard-dir /tmp/conf_report --step 0`，让 `artifact_root` 生效。
2. **读 JSON**：打开 `/tmp/conf_report/artifacts/step_0/<dataset>/metrics.json`，找到 `confidence.per_position` 与 `confidence_summary`，抄下每个位置的 `total_weight / ece / auc / brier / pred_mean / target_mean`，列成表。
3. **读图**：查看同目录的 `reliability_diagram.png`，逐子图判断：折线是否贴对角线？从第几个位置开始整体沉到对角线下方（过自信）？柱状权重在哪里的断层最大？
4. **交叉验证一致性**：用 `pred_mean` 与 `target_mean` 逐位置对比——理论上 `pred_mean@t ≈ target_mean@t`（cumprod 预测的均值应匹配前缀接受频率）；再对照 u6-l1 的 `accept_rate@k` 表，验证 `target_mean@t` 与 `accept_rate@t` 数量级一致（前者是前缀事件的频率、后者是槽位接受率，二者应同向衰减）。
5. **给结论**：基于数据回答两个问题——(a) 这只头最深可信到第几个槽位？(b) 若要设阈值，0.5 和 0.7 哪个更可能是甜点？然后用 4.3.4 的流程实测验证你的选择。

（第 1、5 步需要 GPU 与模型权重，属「待本地验证」；第 2~4 步只消费已有产物，可离线完成。）

## 6. 本讲小结

- `ConfidenceHeadRecorder` 是挂在 `post_verify` 钩子上的旁路观测器：`observe` 把逐槽位置信 logits 的 `sigmoid → cumprod` 前缀预测与 `accept_prefix_mask` 的 0/1 前缀标签配对，不参与解码。
- `PerPositionConfidenceMetrics` 用「粗桶 20（ECE/可靠性图）+ 细桶 1000（AUROC）+ Brier 分子」六张 float64 直方图作充分统计量，本地累积、`finish` 时一次 all_reduce，等价于集中计算但通信只有 O(B·1000)。
- 直方图 AUROC 是 Mann-Whitney U 的分组近似：正例×前缀负例计数 + ½×同桶对，细桶让平局损失可忽略。
- 可靠性图逐槽位出子图，标题内嵌 ECE/AUC/均值，用于找「可信深度」的拐点。
- 阈值早停是逐槽位 sigmoid 与阈值比较、截到第一个低置信位置；截到 0 退化为空提议（只验证锚点、目标直采）。
- 阈值非 0 时关闭记录器，因为截断造成审查与选择偏置：低置信桶样本缺失、存活样本分布被条件化，ECE/AUROC 会系统性失真——这与 EOS 造成的 `effective_proposal_length` 合法截断性质不同。

## 7. 下一步学习建议

- 下一讲 u6-l6 转向 Eagle3 评估器：链式逐 token 提议、草稿缓存错位预填充——对比 DSpark 的块提议，体会「无置信度头」的算法如何用 `extend_draft_cache` 约束替代早停策略。
- 回读 [deepspec/modeling/dspark/loss.py:146-181](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/loss.py#L146-L181) 中训练侧的 `confidence_cumprod_bias` 监控，与本讲评估侧的 cumprod 指标对照，理解「训练时监控 → 评估时体检」的闭环。
- 若你对校准理论感兴趣，可延伸阅读 temperature scaling 等后验校准方法，并思考为什么本仓库选择「直接训一个头」而非事后缩放（提示：接受率逐槽位、逐上下文变化，单一温度系数表达不了位置依赖）。
- 第 7 单元的 u7-l3 毕业实战会把本讲的评测解读纳入端到端流程，届时可把 4.3.4 的阈值扫描作为其中一个实验维度。
