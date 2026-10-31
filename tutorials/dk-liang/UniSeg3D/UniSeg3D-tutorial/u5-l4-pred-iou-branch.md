# u5-l4 IoU 预测分支：训练监督与推理应用

## 1. 本讲目标

上一讲（u5-l3）我们读完了 `OVInstanceCriterion` 的 base/novel 双路匹配，结尾留下了一个悬念：`__call__` 在 `pred_iou=True` 时会额外返回一个 `iou_inst_loss`。本讲专门拆解这个损失背后的完整机制——**IoU 预测分支**。学完本讲，你应该能够：

1. 说出 `pred_iou_proj` 三层 MLP 的结构、输入（`contras_embeds`）与输出形状，以及它在 `loss()` / `predict()` 两条链路中的接入位置。
2. 讲清训练侧监督目标的构造方式：`get_iou` 如何把「二值化预测掩码 vs GT 掩码」的交并比变成回归目标，`no_grad` 与 `iou_thr` 过滤各起什么作用，7 份深监督如何累加。
3. 解释推理侧的两个应用：实例/全景分数乘法（`inst_test_iou` 与 `pano_test_iou` 两个开关为何一关一开），以及开放词汇推理中「mask score × preds_iou」的类无关掩码过滤。
4. 能用假数据独立验证 `pred_iou=True/False` 时损失字典的键差异，并推导分数乘法对 topk 排序的影响。

## 2. 前置知识

**IoU（Intersection over Union，交并比）**：衡量两个二值掩码重叠程度的指标。对预测掩码 \( P \) 与真值掩码 \( G \)（都是点的集合）：

\[
\text{IoU}(P, G) = \frac{|P \cap G|}{|P \cup G|} = \frac{|P \cap G|}{|P| + |G| - |P \cap G|}
\]

取值范围 \([0, 1]\)：完全重合为 1，完全不交为 0。实例分割评估中的 AP@25/AP@50 就是以 IoU 阈值划分「预测是否命中」。

**分类置信度与掩码质量的错位**：分割模型的分类分支回答「这是什么」，掩码分支回答「盖住了哪里」，两者是分开预测的。一个查询可能分类很自信（softmax 分数 0.9）但掩码很糟糕（IoU 只有 0.2）。如果只用分类分数给实例排序，这些「高分低质」的实例会挤占前排，拉低 AP。IoU 预测分支的想法就是：**再训一个小头，从查询特征里直接回归出「这个掩码的 IoU 大概是多少」，推理时把两者相乘作为最终分数**——分类自信且掩码预测质量高的实例才得高分。

**MSE 损失**：均方误差 \(\text{MSE}(x, y) = \frac{1}{n}\sum_i (x_i - y_i)^2\)，是最朴素的回归损失，这里用来让标量预测逼近 IoU 目标。

**`no_grad` 与「老师-学生」**：PyTorch 中 `with torch.no_grad():` 块内产生的张量不记录梯度图。IoU 目标是由**当前预测掩码**算出来的——如果让梯度穿过目标，掩码分支会被「把 IoU 目标变得好预测」的错误方向牵着走。用 `no_grad` 切断后，目标是一个常数：掩码分支当「老师」，IoU 头只当「学生」学着读出掩码质量。

**承接前几讲的认知**：解码器 `UnifiedQueryDecoder` 逐层输出 7 份预测（1 主 + 6 aux 深监督），每份都附带 `contras_embeds`（LayerNorm 后的查询向量，u4-l3）；`OVInstanceCriterion` 用 `indices`（base 匹配）与 `ov_indices`/`ori_ov_query_indices`（novel 二次匹配与坐标还原）拿到「每个 GT 命中了哪个查询」，u5-l3 已详解，本讲直接使用其结论。

## 3. 本讲源码地图

| 文件 | 本讲涉及的内容 |
| --- | --- |
| [uniseg3d/uniseg3d.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py) | `pred_iou_proj` 的构建（`__init__`）、`get_pred_iou()`、`predict_by_feat_instance()` 的分数乘法、`predict()` 中的开放词汇代码块 |
| [uniseg3d/instance_criterion.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py) | `get_iou()`、`OVInstanceCriterion` 中 iou_loss 的目标构造与 `iou_thr` 过滤、7 层聚合 |
| [uniseg3d/query_decoder.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/query_decoder.py) | `contras_embeds` 的来源（`out_norm`），IoU 头的输入 |
| [uniseg3d/unified_criterion.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_criterion.py) | `iou_inst_loss` 如何汇入总损失字典 |
| [configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py) | `pred_iou=True`、`iou_thr=0.`、`IoU_loss_weight=0.1`、`inst_test_iou=False`、`pano_test_iou=True` |

一句话概括数据流：**解码器逐层吐出 `contras_embeds` → `get_pred_iou()` 用三层 MLP 把它压成每查询一个标量 `preds_iou` → 训练时以 `get_iou` 算出的匹配对 IoU 为目标做 MSE（`iou_inst_loss`）→ 推理时乘进实例/全景分数，或与 mask score 相乘过滤开放词汇掩码**。

## 4. 核心概念与源码讲解

### 4.1 模块一：IoU 预测头 `pred_iou_proj` 与它的输入 `contras_embeds`

#### 4.1.1 概念说明

IoU 预测头是挂在主模型 `UniSeg3D` 上（而不是解码器内）的一个小组件，仅当构造参数 `pred_iou=True` 时才创建。它吃的是解码器逐层保存的 `contras_embeds`——LayerNorm 归一化后的查询向量。选它作输入的原因在代码层面很自然：查询向量是「一个超点查询经过整层 Transformer 精炼后的总结」，掩码分类头（`_forward_head`）也从同一向量读出类别与掩码，掩码质量信息理应也藏在这里。`contras_embeds` 这个名字来自它最初为跨任务对比学习（u5-l6）而设，IoU 分支是它的第二个消费者。

#### 4.1.2 核心流程

```text
解码器前向（7 份预测，每份一份 contras_embeds）
  └─ 每层: queries → out_norm(LayerNorm) → contras_embeds[i]，形状 (n_queries_i, d_model)
loss()/predict() 在「拆分提示查询之后」调用 get_pred_iou(x)
  └─ 主输出:   x['contras_embeds'][b] ──pred_iou_proj──► x['preds_iou'][b]，形状 (n_queries_b, 1)
  └─ 每层 aux: x['aux_outputs'][l]['contras_embeds'][b] ──同一个 pred_iou_proj──►
               x['aux_outputs'][l]['preds_iou'][b]
```

注意两点：主输出与 6 层 aux **共用同一个** `pred_iou_proj`（参数共享，训练信号更密集）；调用时机在 `split_original_and_*_prompt_queries` **之后**，即只对「原始查询」计算 IoU，点/文本提示查询不参与。

#### 4.1.3 源码精读

先看头的构建。`pred_iou_proj` 是一个 `d_model → d_model → d_model → 1` 的三层 MLP，`d_model` 直接从解码器配置里读（主配置为 256），最后一层线性输出**单个标量**，不过 sigmoid——这是线性回归而非概率输出：

[uniseg3d/uniseg3d.py:332-341](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L332-L341)：`pred_iou=True` 时构建三层 `Linear+ReLU` 的 MLP，末层输出维度为 1；同时保存 `inst_test_iou`/`pano_test_iou` 两个推理开关。

```python
self.pred_iou = pred_iou
d_model = decoder['d_model']
if self.pred_iou:
    self.pred_iou_proj = nn.Sequential(
            nn.Linear(d_model, d_model), nn.ReLU(inplace=True),
            nn.Linear(d_model, d_model), nn.ReLU(inplace=True),
            nn.Linear(d_model, 1))
```

再看输入侧，`contras_embeds` 在解码器前向的收集（共 7 次：进入循环前 1 次 + 6 层各 1 次）：

[uniseg3d/query_decoder.py:554-567](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/query_decoder.py#L554-L567)：每份预测都把 `out_norm(queries[i].clone())` 存进 `contras_embeds`（`out_norm` 是 `nn.LayerNorm(d_model)`，见 [uniseg3d/query_decoder.py:209](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/query_decoder.py#L209)），最后一层进主输出、前六层进 `aux_outputs`（[uniseg3d/query_decoder.py:569-585](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/query_decoder.py#L569-L585)）。

最后是 `get_pred_iou()` 本体——主输出与 aux 逐层过同一个 MLP 并回填 `preds_iou` 键：

[uniseg3d/uniseg3d.py:499-518](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L499-L518)：`get_pred_iou` 先对主输出逐 batch 计算 `preds_iou`，再对 `num_layers` 层 aux 各算一份，全部写回输出字典。`clone()` 保证了它不污染原张量。

```python
def get_pred_iou(self, x):
    batch = len(x['contras_embeds'])
    num_layers = len(x['aux_outputs'])
    preds_iou = []
    for i in range(batch):
        contras_embed = x['contras_embeds'][i].clone()
        pred = self.pred_iou_proj(contras_embed)
        preds_iou.append(pred)
    x['preds_iou'] = preds_iou
    # ……aux 层同构循环，写入 x['aux_outputs'][i]['preds_iou']
```

两条链路的接入点：训练在 [uniseg3d/uniseg3d.py:493-496](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L493-L496)（拆分提示查询后、送 criterion 前调用），推理在 [uniseg3d/uniseg3d.py:826-827](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L826-L827)。两处都包在 `if self.pred_iou:` 里——所以 `preds_iou` 这个键**只在 `pred_iou=True` 时存在**，这也是下游代码处处判空（如 `out['preds_iou'][0] is not None`）的原因。

#### 4.1.4 代码实践

只用 PyTorch 复刻这个头，观察形状与参数量（示例代码，无需完整项目环境）：

```python
import torch
import torch.nn as nn

d_model = 256  # 与主配置 decoder.d_model 一致
pred_iou_proj = nn.Sequential(
    nn.Linear(d_model, d_model), nn.ReLU(inplace=True),
    nn.Linear(d_model, d_model), nn.ReLU(inplace=True),
    nn.Linear(d_model, 1))

x = torch.randn(500, d_model)          # 模拟 500 个超点查询的 contras_embeds
out = pred_iou_proj(x)
print(out.shape)                                      # 期望 torch.Size([500, 1])
print(sum(p.numel() for p in pred_iou_proj.parameters()))  # 参数量
print(out.min().item(), out.max().item())             # 随机初始化时的输出量级
```

1. 实践目标：确认头的形状变换是 `(n_queries, 256) → (n_queries, 1)`，并感受它相对整模型有多小。
2. 操作步骤：保存为独立脚本运行 `python script.py`。
3. 需要观察的现象：输出形状；参数量约 \(256\times256+256 + 256\times256+256 + 256\times1+1 \approx 13.2\) 万；随机初始化时输出在 0 附近小幅波动（末层 bias 初始化为 0，输出由随机权重与输入共同决定）。
4. 预期结果：`torch.Size([500, 1])`；训练早期 `preds_iou` 接近 0，随训练逐步落到 0~1 区间逼近 IoU 目标。具体数值待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：`pred_iou_proj` 为什么放在主模型 `UniSeg3D.__init__` 里，而不是解码器 `UnifiedQueryDecoder` 内部？

**答案**：从代码看，`pred_iou` 是 `UniSeg3D` 的构造参数（[uniseg3d/uniseg3d.py:297](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L297)），主配置在 model 顶层设 `pred_iou=True`、decoder 配置里没有这一项。放在外壳层让「是否启用 IoU 分支」成为一个不侵入解码器的开关；同时 `get_pred_iou` 在拆分提示查询之后才调用，这个时机由主模型掌握，解码器并不感知「原始/提示查询」的划分（那是 u4-l6 的注入-拆分协议）。

**练习 2**：`contras_embeds` 一份前向共有几份、各自由哪些查询组成？

**答案**：7 份（1 主 + 6 aux），每份形状 `(n_queries_b, 256)`。由于 `get_pred_iou` 在 `split_original_and_*_prompt_queries` 之后调用，进入 IoU 分支的 `contras_embeds` 只含原始（超点）查询；点提示与文本提示查询的 `contras_embeds` 被拆分进了各自的 prompt_outputs，不参与 IoU 监督——对照配置，点/文本提示损失（`PointPromptInstanceCriterion`/`TextPromptInstanceCriterion`）的 `loss_weight` 只有 4 项（[configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:103](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L103)、[L111](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L111)），而 `OVInstanceCriterion` 是 6 项，提示分支确实没有 IoU 项。

### 4.2 模块二：训练监督——`get_iou` 目标构造、`no_grad` 与 `iou_thr` 过滤

#### 4.2.1 概念说明

有了 `preds_iou`，训练要回答「拿什么当回归目标」。答案很直接：**对每个匹配对（查询, GT），把该查询的预测掩码二值化后与 GT 掩码算 IoU，让 `preds_iou` 用 MSE 去逼近它**。两个关键设计：

- **目标在 `no_grad` 下计算**：掩码是「老师」，目标是常数，IoU 头是「学生」。梯度只更新 `pred_iou_proj`（并经 `contras_embeds` 回传到解码器），不会通过目标扭曲掩码分支。
- **`iou_thr` 过滤**：只监督 IoU ≥ `iou_thr` 的匹配对。直觉是掩码还很差（IoU 接近 0）的匹配多处于学习早期，逼 IoU 头去精确预测一个「很糟糕」的数值收益有限；调高 `iou_thr` 可以只学「已经像样」的样本。主配置取 `iou_thr=0.`，即不过滤（IoU 恒非负，全部通过）。

#### 4.2.2 核心流程

`OVInstanceCriterion.__call__`（主层）与 `get_layer_loss`（aux 层）中的 iou_loss 逻辑相同（两份近似逐字重复的代码），对 batch 内每个场景三分支处理：

```text
对每个场景 i（拿到 base 匹配 indices[i]、novel 匹配 ov_indices[i] 及坐标还原 ori_ov_query_indices[i]）：
  if base 命中数 == 0 且 novel 命中数 == 0:  跳过（空场景不贡献）
  elif 只有 novel:  监督对 = novel 命中（用还原后的原始查询下标）
  elif 只有 base:  监督对 = base 命中
  else:            监督对 = cat(base 命中, novel 命中)，并 assert 两路查询不相交
  tgt_score = get_iou(拼接后的预测掩码, 拼接后的 GT 掩码)   # no_grad，形状 (n_pairs, 1)
  filter = tgt_score >= iou_thr                              # iou_thr 过滤
  if filter 非空: iou_loss.append(MSE(preds_iou[查询下标][filter], tgt_score[filter]))

聚合: iou_inst_loss = Σ_场景 / batch_size × IoU_loss_weight(=0.1)
深监督: 主层 + 6 层 aux 裸累加（每层重新匹配后独立计算）
```

#### 4.2.3 源码精读

先看目标函数 `get_iou`：预测侧先 sigmoid 再 `>= 0.5` 二值化（等价于 logit > 0，恰与推理处「正区域」判定 `mask_pred > 0` 一致），GT 侧 bool 直接 `> 0.5` 浮点化，然后按交并比公式计算：

[uniseg3d/instance_criterion.py:47-63](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L47-L63)：`get_iou` 对**形状相同、逐行配对**的预测/真值掩码逐行算 IoU，返回 `(n_gts,)` 分数向量。

```python
def get_iou(inputs, targets):
    inputs = inputs.sigmoid()
    binarized_inputs = (inputs >= 0.5).float()
    targets = (targets > 0.5).float()
    intersection = (binarized_inputs * targets).sum(-1)
    union = targets.sum(-1) + binarized_inputs.sum(-1) - intersection
    score = intersection / (union + 1e-6)
    return score
```

再看主层 iou_loss 的通用分支（base 与 novel 都有命中时）。回忆 u5-l3：`base_idx_q_` 是 base 匹配命中的查询下标，`ori_ov_indice_` 是 novel 匹配经 `mask_map` 还原后的**原始坐标系**查询下标，两者保证不相交：

[uniseg3d/instance_criterion.py:589-610](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L589-L610)：把 base 与 novel 的预测/真值掩码按行拼接、命中查询下标拼接，在 `no_grad` 下算 IoU 目标，经 `iou_thr` 过滤后对存活的匹配对做 MSE。

```python
total_pred_mask_ = torch.cat((base_pred_mask_, ov_pred_mask_), dim=0)
total_tgt_mask_ = torch.cat((base_tgt_mask_, ov_tgt_mask_), dim=0)
total_indice_ = torch.cat((base_idx_q_, ori_ov_indice_), dim=0)
assert torch.isin(ori_ov_indice_ , base_idx_q_).sum() == 0
with torch.no_grad():
    tgt_score_ = get_iou(total_pred_mask_, total_tgt_mask_).unsqueeze(1)

filter_id_, _ = torch.where(tgt_score_ >= self.iou_thr)
if filter_id_.numel():
    total_indice_ = total_indice_[filter_id_]
    tgt_score_ = tgt_score_[filter_id_]
    iou_loss.append(F.mse_loss(pred_iou_[total_indice_], tgt_score_))
```

三个易错点：`tgt_score_` 的 `unsqueeze(1)` 把 `(n_pairs,)` 变成 `(n_pairs, 1)`，与 `pred_iou_[total_indice_]` 的 `(n_pairs, 1)` 对齐做逐元素 MSE；`filter_id_` 过滤的是**匹配对下标**（同步过滤查询下标与目标）；两路只有一路命中时走 [L554-L587](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L554-L587) 的两个对称分支，双空则 `continue`。aux 层的同构实现见 [uniseg3d/instance_criterion.py:281-352](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L281-L352)。

聚合与深监督。逐场景的 MSE 按批次平均、乘 `IoU_loss_weight`；aux 层在 `__call__` 尾部对 6 层预测（`iter_matcher=True` 时每层重新匹配）累加：

[uniseg3d/instance_criterion.py:611-626](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L611-L626)：iou_loss 列表聚合为 `iou_inst_loss`（除以 `len(pred_masks)` 即 batch 内场景数、乘权重 0.1）；aux 循环 `iou_inst_loss += loss[2]` 裸累加；最终 `pred_iou=True` 的返回字典含 `iou_inst_loss` 键。

```python
if len(iou_loss):
    iou_inst_loss = torch.stack(iou_loss).sum() / len(pred_masks) * self.IoU_loss_weight
else:
    iou_inst_loss = torch.tensor(0., device=device)
...
return_loss = {'inst_loss': inst_loss, 'ov_inst_loss':ov_inst_loss, 'iou_inst_loss':iou_inst_loss}
```

最后汇入总损失：`ScanNetUnifiedCriterion.__call__` 直接 `loss.update(self.inst_criterion(...))`（[uniseg3d/unified_criterion.py:65-66](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_criterion.py#L65-L66)），`iou_inst_loss` 因此出现在训练日志里（u5-l1 统计的 10 个 loss key 之一）。配置侧的开关与超参：[configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:89-99](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L89-L99) 中 `pred_iou=True`、`iou_thr=0.`、`IoU_loss_weight=0.1`。

顺带一提梯度流向：`contras_embed` 在 `get_pred_iou` 里只是 `clone()` 并未 detach，所以 `iou_inst_loss` 的梯度会同时更新 `pred_iou_proj` 自身**并回传到解码器**（经 `out_norm(queries)`）——IoU 监督也在间接塑造查询表征；唯一被切断的是穿过 `tgt_score_` 的那条路。

#### 4.2.4 代码实践（本讲主实践）

用假数据分别以 `pred_iou=True/False` 组装一次损失，对比 loss key 差异。需要 u1-l2 搭好的完整环境（`import uniseg3d` 会连带加载全部依赖）。以下为示例代码：

```python
# iou_branch_practice.py —— 置于仓库根目录运行: PYTHONPATH=. python iou_branch_practice.py
import torch
import uniseg3d  # noqa: F401  触发注册
from mmdet3d.registry import MODELS
from uniseg3d.structures import InstanceData_

def build_criterion(pred_iou):
    return MODELS.build(dict(
        type='OVInstanceCriterion',
        matcher=dict(type='SparseMatcher', costs=[
            dict(type='QueryClassificationCost', weight=0.5),
            dict(type='MaskBCECost', weight=1.0),
            dict(type='MaskDiceCost', weight=1.0)], topk=1),
        ov_matcher=dict(type='SparseMatcher', costs=[
            dict(type='MaskBCECost', weight=1.0),
            dict(type='MaskDiceCost', weight=1.0)], topk=1),
        pred_iou=pred_iou, ratio=0.1, iou_thr=0.,
        loss_weight=[0.5, 1.0, 1.0, 0.5, 0.1, 0.1],
        num_classes=18, non_object_weight=0.1,
        fix_dice_loss_weight=True, IoU_loss_weight=0.1,
        IntrinsicPointAndQuery_loss_weight=0.1,
        iter_matcher=True, fix_mean_loss=True))

torch.manual_seed(0)
n_q, n_sp, n_gt = 30, 50, 6          # 30 个查询、50 个超点、6 个 GT(4 base + 2 novel)
labels = torch.tensor([3, 5, 7, 9, 0, 0])           # novel 行标签占位 0
is_novel = torch.tensor([False]*4 + [True]*2)
sp_masks, query_masks = torch.zeros(n_gt, n_sp, dtype=torch.bool), \
                        torch.zeros(n_gt, n_q, dtype=torch.bool)
for j in range(n_gt):                 # GT j 独占超点 [j*5,(j+1)*5) 与查询 [j*3,(j+1)*3)
    sp_masks[j, j*5:(j+1)*5] = True
    query_masks[j, j*3:(j+1)*3] = True
insts = [InstanceData_(labels_3d=labels, sp_masks=sp_masks,
                       query_masks=query_masks, is_novel=is_novel)]

def make_pred(with_iou):
    pred = dict(cls_preds=[torch.randn(n_q, 19)], scores=[None],
                masks=[torch.randn(n_q, n_sp)])
    if with_iou:
        pred['preds_iou'] = [torch.full((n_q, 1), 0.5)]   # 恒 0.5 便于心算 MSE
    pred['aux_outputs'] = [dict(
        cls_preds=[torch.randn(n_q, 19)], scores=[None],
        masks=[torch.randn(n_q, n_sp)],
        **({'preds_iou': [torch.full((n_q, 1), 0.5)]} if with_iou else {}))
        for _ in range(6)]
    return pred

for flag in (True, False):
    losses = build_criterion(flag)(make_pred(flag), insts)
    print(f'pred_iou={flag}:', {k: round(float(v), 4) for k, v in losses.items()})
```

1. 实践目标：验证 `pred_iou` 开关如何改变 `OVInstanceCriterion` 的返回键集合与数值含义。
2. 操作步骤：按上例保存脚本，在仓库根目录用 `PYTHONPATH=. python iou_branch_practice.py` 运行。
3. 需要观察的现象：`pred_iou=True` 输出 `{'inst_loss': …, 'ov_inst_loss': …, 'iou_inst_loss': …}`；`pred_iou=False` 输出只有前两个键。`iou_inst_loss` 是 7 层之和，每层 ≈ 0.1 × mean((0.5 − IoU 目标)²)。
4. 预期结果：键名差异由代码结构保证（[instance_criterion.py:626](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L626) 与 [L640](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L640) 两条返回路径）；具体数值待本地验证。注意假数据里预测掩码是随机 logits，IoU 目标普遍偏低，MSE 不会是小数。
5. 若无 GPU 环境跑不动 `import uniseg3d`，可退化为纯源码阅读：对照 [L545-L614](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L545-L614) 手推 `iou_inst_loss` 的表达式。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `iou_thr` 从 0. 调到 0.5，监督行为会怎么变？会不会改变 `iou_inst_loss` 的键名？

**答案**：`torch.where(tgt_score_ >= self.iou_thr)` 只保留 IoU ≥ 0.5 的匹配对，掩码尚差的匹配对（训练早期占多数）不再产生梯度，IoU 头只在「像样」的样本上学习，且 `filter_id_.numel()==0` 的场景整段跳过。键名不变——`iou_inst_loss` 恒在返回字典里，只是可能更常取到 0。

**练习 2**：为什么 `tgt_score_` 用 `get_iou`（二值化 IoU）而不是 dice 或软 IoU？

**答案**：与推理保持一致是首要原因——评估用的 AP@25/50 全部基于二值化掩码的 IoU，IoU 头学的就是「推理时会被打多少分」这个量，回归目标与评估指标同构，预测出的分数才有校准意义。软 IoU（不二值化）虽更平滑，但与评估口径脱节；dice 是训练掩码分支的损失（`dice_loss`），与这里的「读出质量」职责不同。

**练习 3**：7 层深监督下，最终日志里的 `iou_inst_loss` 是「平均」还是「求和」？

**答案**：求和（裸累加）。层内先按场景数平均并乘 `IoU_loss_weight=0.1`（[L612](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L612)），6 层 aux 在 [L620-L624](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L620-L624) 逐层 `+=`，不做 `/7`。看日志时要意识到它天然是单层损失的约 7 倍量级。

### 4.3 模块三：推理应用一——分数乘法与 `inst_test_iou` / `pano_test_iou` 双开关

#### 4.3.1 概念说明

推理时 `preds_iou` 的第一个用途是**校准实例分数**：在 `predict_by_feat_instance` 里，把每个查询所有类别的 softmax 分数统一乘上该查询的 IoU 预测，让「分类自信但掩码可疑」的实例降权。主配置给实例分割（IS）与全景分割（PS）配了**两个独立开关**：`inst_test_iou=False`、`pano_test_iou=True`（[configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:29-30](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L29-L30)）——同一个前向里，实例结果不乘 IoU，全景结果乘。从机制上可以这样理解差异：AP 评估依赖连续的分数排序（AP 是精确率-召回率曲线下面积），保留纯分类排序有利于分数多样性；全景任务要把 things 实例与 stuff 区域合并成**一张**不重叠的图，配合较高的 `pan_score_thr=0.5` 阈值，用 IoU 把低质量实例压到阈值以下、避免碎片污染全景图，收益更直接。这是作者预留的消融开关，两任务可独立取舍（哪个更优属论文实验结论，待本地验证）。

#### 4.3.2 核心流程

```text
predict_by_feat(out, superpoints)
  ├─ 实例路径:  predict_by_feat_instance(..., is_test_iou = inst_test_iou=False) → 不乘
  └─ 全景路径:  predict_by_feat_panoptic → 内部再调
                predict_by_feat_instance(..., score_threshold=pan_score_thr, is_test_iou = pano_test_iou=True) → 乘

predict_by_feat_instance 内部（乘法生效时）：
  scores = softmax(cls_preds)[:, :-1]          # (n_queries, n_classes)
  scores *= out['scores'][0]                    # objectness 分数；主配置 objectness_flag=False 时为 None，跳过
  scores *= out['preds_iou'][0]                 # (n_queries, 1) 按行广播相乘 ←—— 本讲焦点
  → flatten(0,1).topk(topk_insts=600)           # 乘法发生在 topk 之前，直接改变候选与排序
```

#### 4.3.3 源码精读

[uniseg3d/uniseg3d.py:110-127](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L110-L127)：实例预测的打分与 topk。`preds_iou` 形状 `(n_queries, 1)`，与 `(n_queries, n_classes)` 的 scores 相乘时按行广播——**每个查询的所有类别乘同一个标量**，因此查询内部类别排序不变、查询之间的排序被重塑。

```python
cls_preds = out['cls_preds'][0]
pred_masks = out['masks'][0]

scores = F.softmax(cls_preds, dim=-1)[:, :-1]
if out['scores'][0] is not None:
    scores *= out['scores'][0]
if is_test_iou:
    if out['preds_iou'][0] is not None:
        scores *= out['preds_iou'][0]
...
scores, topk_idx = scores.flatten(0, 1).topk(
    self.test_cfg.topk_insts, sorted=False)
```

两个调用点的开关来源：

- [uniseg3d/uniseg3d.py:77-80](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L77-L80)：实例分割路径传 `is_test_iou=self.inst_test_iou`（配置为 False → 分数链到此为止，后续只有 `obj_normalization` 的 mask_scores 再乘一次，见 [L132-L135](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L132-L135)）。
- [uniseg3d/uniseg3d.py:193-196](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L193-L196)：全景路径换用 `pan_score_thr` 阈值且传 `is_test_iou=self.pano_test_iou`（配置为 True → 乘）。

```python
mask_pred, labels, scores  = self.predict_by_feat_instance(
    out, superpoints, self.test_cfg.pan_score_thr, is_test_iou=self.pano_test_iou)
```

还要注意：主配置 `objectness_flag=False` 时解码器不建 `out_score`，`pred_scores` 恒为 None（[uniseg3d/query_decoder.py:474](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/query_decoder.py#L474)），所以 `scores *= out['scores'][0]` 这一步实际跳过——**`preds_iou` 是分数链上唯一生效的额外乘子**（obj_normalization 的 mask_scores 除外）。

#### 4.3.4 代码实践

先手推、再跑脚本验证。设 3 个查询、2 个实例类 + 背景，分类 logits 与 IoU 预测如下（示例代码）：

```python
import torch
import torch.nn.functional as F

cls_preds = torch.tensor([[2.2, 0.1, -1.0],   # q0: 类别倾向 0(chair)，很自信
                          [0.2, 1.6, -0.5],   # q1: 类别倾向 1(table)
                          [1.0, 0.8, -0.3]])  # q2: 类别倾向 0(chair)，略弱
preds_iou = torch.tensor([[0.30],             # q0: 掩码质量差
                          [0.90],             # q1: 掩码质量好
                          [0.80]])            # q2: 掩码质量好

scores = F.softmax(cls_preds, dim=-1)[:, :-1]      # 对应 predict_by_feat_instance L113
print('不乘 IoU（实例路径 inst_test_iou=False）:')
print(scores.flatten().topk(3))                    # 对应 L123 的 topk
print('乘 IoU（全景路径 pano_test_iou=True）:')
print((scores * preds_iou).flatten().topk(3))      # 对应 L116-L118 + L123
```

1. 实践目标：验证「分数 × preds_iou」如何重排 topk 候选。
2. 操作步骤：保存运行；对照打印的两个 topk 下标序列。
3. 需要观察的现象：不乘时 q0 的最高类别分排在首位；乘上后 q0 被两个「类别略弱但 IoU 高」的查询反超。具体位次以脚本输出为准（待本地验证），可以手算 softmax 佐证：q0 类 0 分数约 0.81，乘 0.30 后约 0.24；q1 类 1 分数约 0.62，乘 0.90 后约 0.56——排序反转。
4. 预期结果：乘法把排序从「纯分类置信度」拉向「分类 × 掩码质量」，分类自信但掩码差的查询在候选池（top-600）中的名次下沉；由于逐行同乘，同一查询内部「哪个类别得分最高」不变，`labels` 与 `topk_idx // num_classes` 的对应关系不受影响。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `scores *= out['preds_iou'][0]` 必须放在 `topk` 之前？放到 topk 之后（只调序不筛选）效果有何不同？

**答案**：`topk(self.test_cfg.topk_insts)` 是从 `n_queries × n_classes` 个 (查询, 类别) 对里截取前 600 的**硬筛选**，之后所有处理（obj_normalization、NMS、阈值过滤）只作用在这 600 个候选上。乘法放在 topk 前，低 IoU 的查询可能整体掉出候选池（相当于被质量门拦截）；放在 topk 后则只重排已入选者，垃圾候选照样占坑。代码把乘法放在 [L116-L123](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L116-L123) 之间，选择的是前者。

**练习 2**：如果把 `inst_test_iou` 也改成 True，实例分割的 AP 一定上升吗？

**答案**：不一定，这正是它被做成独立开关的原因。IoU 乘子对分数是**单调重排**（每查询一个标量），它提升排序的前提是 `preds_iou` 预测得准；训练早期或小数据下 IoU 头噪声大时，乘法反而引入排序噪声。AP 对分数排序敏感（尤其低阈值区间），而全景任务的合并机制对「过滤低质实例」更受益。最终哪个配置更优需要消融实验验证（待本地验证）。

**练习 3**：`out['scores'][0] is not None` 与 `out['preds_iou'][0] is not None` 两个判空各在防什么？

**答案**：前者防 objectness 分支缺席——`objectness_flag=False` 时解码器没有 `out_score` 模块，`scores` 恒为 None；后者防 `pred_iou=False` 时 `get_pred_iou` 根本没被调用、`preds_iou` 键不存在（[uniseg3d/uniseg3d.py:826](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L826)），或提示查询输出里该键为 None（拆分函数把提示输出的 `scores` 等初始化为 None 列表）。双重判空让推理代码对配置组合保持稳健。

### 4.4 模块四：推理应用二——开放词汇的类无关掩码过滤

#### 4.4.1 概念说明

开放词汇分割（u1-l1 讲过）采用两阶段范式：UniSeg3D 只产出**类无关掩码**（不关心类别，只圈出「这里有一个完整物体」），再交给 Open3DIS 等外部分类器打类别标签。`predict()` 里预留了一段被注释的代码块（[uniseg3d/uniseg3d.py:829-873](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L829-L873)），取消注释并设置 `save_path` 即可逐场景保存掩码。过滤的核心量是：

\[
\text{mask\_iou\_score} = \underbrace{\frac{\sum_{p \in \text{正区域}} \sigma(\text{logit}_p)}{|\text{正区域}|}}_{\text{mask score：正区域平均置信度}} \times \text{preds\_iou}
\]

即「掩码自身有多自信」乘「模型自评的 IoU 有多高」，低于 `iou_threshold`（默认 0.6）的掩码直接丢弃。注意 mask score 的公式与实例路径 `obj_normalization` 的 `mask_scores`（[L132-L135](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L132-L135)）完全一致——开放词汇分支等于复用了这个打分再叠加 IoU 维度。

#### 4.4.2 核心流程

```text
predict() 中、get_pred_iou(x) 之后：
  preds_iou      = x['preds_iou'][0]                     # (n_queries, 1)
  mask_score     = 正区域平均 sigmoid 值                  # (n_queries,)
  mask_iou_score = mask_score × preds_iou.squeeze(-1)    # (n_queries,)
  idx            = mask_iou_score > iou_threshold(0.6)   # 质量门
  info['ins']    = sigmoid 掩码[:, superpoints] > mask_threshold(0.4)，转 uint8
  info['conf']   = mask_iou_score[idx]
  torch.save(info, <场景名>.pth)                          # 供 Open3DIS 分类
```

#### 4.4.3 源码精读

[uniseg3d/uniseg3d.py:851-870](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L851-L870)：类无关掩码生成（仓库中默认整段注释）。先用与 `obj_normalization` 同式的 mask score 乘 `preds_iou` 得综合质量分，再按 `iou_threshold` 过滤，最后把存活掩码投影到原始点（`[:, sp_pts_masks]` 索引，与 [L142](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L142) 的超点映射同机制）并按 `mask_threshold` 二值化，连同置信度一起存成 `.pth`。

```python
# preds_iou_ = x['preds_iou'][0].clone()
# pred_masks_ = x['masks'][0].clone()
# pred_masks_sigmoid_ = pred_masks_.sigmoid()

# mask_score_ = (pred_masks_sigmoid_ * (pred_masks_ > 0)).sum(1) / \
#               ((pred_masks_ > 0).sum(1) + 1e-6)
# mask_iou_score_ = mask_score_ * preds_iou_.squeeze(-1)

# idx = mask_iou_score_ > iou_threshold          # 默认 0.6
# info['ins'] = (pred_masks_sigmoid_[idx][:, sp_pts_masks] > mask_threshold)...
# info['conf'] = mask_iou_score_[idx].cpu().numpy()
# torch.save(info, scene_path)
```

三个细节：`(pred_masks_ > 0)` 的正区域判定与 `get_iou` 的二值化口径一致（logit > 0 ⟺ sigmoid > 0.5）；过滤发生在**超点投影之前**（先按查询过滤再投影，省计算）；`preds_iou_.squeeze(-1)` 把 `(n_q, 1)` 压成 `(n_q,)` 以便与 mask score 逐元素相乘——与 4.3 中的广播乘法是同一个张量、两种用法。README 的开放词汇小节说明了保存后如何接 Open3DIS，具体步骤将在 u6-l3 展开。

#### 4.4.4 代码实践

用小张量模拟过滤逻辑，感受两个阈值的分工（示例代码）：

```python
import torch

n_q, n_sp = 8, 100
pred_masks = torch.randn(n_q, n_sp)                      # 模拟掩码 logits
preds_iou = torch.tensor([[.9], [.85], [.7], [.8], [.2], [.95], [.1], [.75]])
# 人为强化前 4 个查询与后 2 个查询的掩码（正区域高 logit）
for q in [0, 1, 2, 3, 6, 7]:
    pred_masks[q, q*10:(q+1)*10] = 4.0

sigmoid = pred_masks.sigmoid()
mask_score = (sigmoid * (pred_masks > 0)).sum(1) / ((pred_masks > 0).sum(1) + 1e-6)
mask_iou_score = mask_score * preds_iou.squeeze(-1)

iou_threshold, mask_threshold = 0.6, 0.4
idx = mask_iou_score > iou_threshold
print('存活查询:', idx.nonzero().flatten().tolist())
print('conf:', mask_iou_score[idx].round(decimals=3).tolist())
ins = (sigmoid[idx] > mask_threshold)
print('每个存活掩码的点数:', ins.sum(1).tolist())
```

1. 实践目标：验证 `mask_iou_score = mask_score × preds_iou` 的过滤行为——分类高分救不了低 IoU，反之亦然。
2. 操作步骤：保存运行；再分别把 `iou_threshold` 改为 0.3、`mask_threshold` 改为 0.9 各跑一次。
3. 需要观察的现象：`iou_threshold` 决定哪些查询存活（数量变化），`mask_threshold` 决定存活掩码的二值化粗细（点数变化），两者职责正交；`preds_iou=0.2/0.1` 的查询即使 mask score 高也会被丢弃。
4. 预期结果：存活集合随 `iou_threshold` 单调收缩；`mask_threshold` 越大掩码越「瘦」。具体数值待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：这一步过滤为什么对开放词汇任务尤其重要，而对闭集实例分割可以不做？

**答案**：闭集实例分割有 18 个已知类的分类分支兜底，质量差的掩码在评估时会被 IoU 匹配自然淘汰，且有 NMS 与阈值过滤兜底；开放词汇的第二阶段是外部分类器（Open3DIS），它对送进来的每张掩码都会认真分类——一张质量差的碎片掩码会被错误分类并计入结果，没有评估侧兜底。所以在源头用 `mask_score × preds_iou` 把低质量掩码拦下，`conf` 还能传给下游做置信度加权。

**练习 2**：`info['ins']` 为什么先做 `[:, sp_pts_masks]` 再二值化，而不是先在超点维度平均再二值化？

**答案**：`pred_masks_sigmoid_[idx][:, sp_pts_masks]` 是「广播索引」：`sp_pts_masks` 形状 `(n_raw_points,)`，索引结果 `(n_kept, n_raw_points)`——把每行的超点级预测值复制给该超点覆盖的所有原始点（与 [L142](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L142) 的 `mask_pred_sigmoid[:, superpoints]` 同一操作）。先投影后二值化保证每个原始点拿到的是其超点的确定值；若改成先在超点内平均再二值化，边界超点会被稀释，掩码边缘失真。

## 5. 综合实践

把 4.2.4 的脚本扩展成一个「IoU 分支体检脚本」，一次跑通三件事：

1. **键差异复验**：保持 `pred_iou=True/False` 对比，确认输出键集合分别为 3 个与 2 个。
2. **目标核对**：在脚本里手动复算监督目标——从 `make_pred(True)` 的假数据出发，调用 `from uniseg3d.instance_criterion import get_iou`，对「查询 k 的掩码 vs 某 GT 掩码」直接算一个 IoU 值；再把 `preds_iou` 里查询 k 的值人为设成这个 IoU，重跑损失，观察 `iou_inst_loss` 变小（该匹配对的 MSE 项趋于 0）——验证「MSE 目标确实是二值化 IoU」。
3. **过滤观察**：给 criterion 传 `iou_thr=0.5`（构建两份 criterion 对比），在 `make_pred` 里把部分查询的掩码 logits 设成与 GT 完全一致（IoU=1）、另一部分设成纯随机（IoU≈0），观察只有「好匹配」贡献了 `iou_inst_loss` 的下降。

预期：三步全部与 4.2.3 的源码行为吻合；第 2 步是最有力的证据——它把回归目标从代码里「请」出来与你手算对上。数值细节待本地验证。

## 6. 本讲小结

- `pred_iou_proj` 是挂在主模型上的 `256→256→256→1` 三层 MLP，输入解码器逐层保存的 `contras_embeds`；`get_pred_iou()` 让主输出与 6 层 aux **共用同一个头**，且只在拆分提示查询之后对原始查询计算。
- 训练监督是「老师-学生」式 MSE：`get_iou` 用**二值化**预测掩码与 GT 的交并比作目标（与评估口径同构），`no_grad` 切断穿过目标的梯度，`iou_thr` 可过滤低质量匹配对（主配置取 0. 即不过滤）。
- base 与 novel 命中对被拼接后统一监督，查询下标用 `ori_ov_query_indices` 还原到原始坐标系，两路不相交由 assert 保证；`iou_inst_loss` 层内按场景数平均、乘 `IoU_loss_weight=0.1`，7 层**裸累加**。
- 推理侧两个应用共用同一个 `preds_iou`：实例/全景分数乘法（`inst_test_iou=False`、`pano_test_iou=True` 双开关独立控制，乘法发生在 top-600 硬筛选之前、逐行同乘不改变查询内类别排序）；开放词汇分支用 `mask_score × preds_iou > iou_threshold(0.6)` 过滤类无关掩码并把 `conf` 存盘供 Open3DIS 使用。
- 梯度流向注意点：IoU 损失经未 detach 的 `contras_embeds` 同时训练 `pred_iou_proj` 与解码器表征；提示查询（点/文本）分支完全不参与 IoU 监督。

## 7. 下一步学习建议

- **u5-l5（提示实例损失）**：看与 `OVInstanceCriterion` 平行的两个提示分支 criterion——它们没有匹配器、没有 IoU 项，正好反衬出本讲机制在「自由查询」设定下的必要性。
- **u5-l6（跨任务对比学习）**：`contras_embeds` 的第一个消费者。本讲读完，你会对「一个中间表征被多个损失头复用」的设计有完整认识。
- **u6-l1（推理链路）**：把 4.3 的分数乘法放回 `predict()` 全链路（obj_normalization → NMS → 超点阈值 → npoint_thr），理解 preds_iou 在整条过滤链中的位置。
- **u6-l3（开放词汇实战）**：亲手取消 4.4 代码块的注释跑通保存流程，并对接 Open3DIS；届时回看本讲的 `mask_iou_score` 公式会非常亲切。
