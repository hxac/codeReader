# u6-l1 分割指标 Part3DGlamm_SegMetric：mIoU 怎么算

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `qa_pred_masks` 与 `qa_gt_masks` 这两组列表是如何「按轮 1:1 对齐」的，包括行数不一致时的补零/截断规则。
2. 解释 `mask[:, superpoints]` 这一行索引为什么能把「超点级掩码」变成「点级掩码」。
3. 手工推导 `get_iou` 在多维掩码上的逐实例计算，写出 QA 级 IoU 与全局 mIoU 的公式。
4. 说明 `existence_verification` 等无 `<p>` 标签任务为什么会被自动排除在 mIoU 之外（NaN 哨兵机制），而不需要在指标里写任何任务名过滤。
5. 指出 `Part3dGlamm_SegMetric.process/compute_metrics` 各自的职责，以及哪些参数是从父类抄来的「死参数」。

本讲承接 u5-l1（解码器逐层前向的输出）与 u3-l1（`predict()` 链路概览）：u3-l1 从模型视角带过了「有 GT 才对齐、不足补零、超出截断」，本讲站在**指标消费端**把对齐规则、IoU 数学与 NaN 排除逻辑完整展开。

## 2. 前置知识

- **IoU（Intersection over Union，交并比）**：衡量两个二值掩码重合程度的基本量，\( \text{IoU} = \frac{|G \cap P|}{|G \cup P|} \)，取值 0～1，1 表示完全重合。
- **mIoU（mean IoU）**：对多个「样本」各自算出一个 IoU，再取算术平均。本讲会看到它有两级平均：先在一条 QA 内部对多个目标平均，再对所有 QA 平均。
- **超点（superpoint）与逐点掩码**：模型在几千个超点上做掩码预测，而评估需要对几十万个原始点。每个点属于唯一超点（`sp_pts_mask` 给出逐点的超点编号），因此「取超点掩码的第 j 列值」就能广播给该超点的所有点。
- **NaN 哨兵**：u2-l4 讲过，答案里没有 `<p>…</p>` 标签（如 `existence_verification` 回答 Yes/No）或取不到有效掩码时，GT 行用 `float('nan')` 填充而不是删除，以保持行对齐。本讲的关键就是指标如何把这些 NaN 行剔除。
- **mmengine 指标生命周期**：`BaseMetric` 约定两个回调——`process()` 在每个 batch 推理后被调用，把中间结果攒进 `self.results`；全部数据跑完后 `compute_metrics(self.results)` 被调用一次，返回最终的指标字典。分布式评估时，各 rank 的 `self.results` 会被收集合并后再计算（以 mmengine 实际实现为准，源码注释也明确写了这一点）。
- **`<p>…</p>` 数量契约**（u2-l4）：一条回答里有几个 `<p>` 指称，GT 就有几行、模型就提取几个 `<SEG>` 查询。训练两侧天然对齐；推理时模型是自由生成的，行数可能不一致——这正是本讲对齐规则存在的原因。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [uniseg3d/unified_metric.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/unified_metric.py) | 本讲主角：`get_iou` 函数与 `Part3dGlamm_SegMetric` 指标类（`process`/`compute_metrics`）。 |
| [uniseg3d/uniseg3d.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py) | `Grounded_LMM_main.predict()` 收集 GT、调用解码器，以及 `qa_predict_by_feat()` 完成超点→点映射与 GT 对齐。 |
| [uniseg3d/transforms_3d.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py) | `QA_Generation_Test`（评估管线如何按轮生成 `sp_gt_seg`）与 `get_seg_from_single_qa`（NaN 的诞生地）。 |
| [uniseg3d/query_decoder.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py) | 评估分支 `forward_iter_pred`：逐 QA（多轮则逐轮）跑 6 层解码，产出 `out['masks']`。 |
| [configs/ground3dlmm_eval_ground3d_scannet.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_ground3d_scannet.py) | `val_evaluator` 挂载点与 `test_cfg.sp_score_thr=0.4` 阈值。 |

数据流向一句话总结：

```
QA_Generation_Test（GT 按轮展平, NaN 填充）        解码器 infer_reason_eval（预测按轮展平）
                │                                              │
                └──────────┬───────────────────────────────────┘
                           ▼
        qa_predict_by_feat：sigmoid>0.4 → [:, superpoints] 上采样 → 行数对齐
                           ▼
        PointData(qa_pred_masks, qa_gt_masks) 写入 data_sample.pred_pts_seg
                           ▼
        Part3dGlamm_SegMetric.process：get_iou → 只存每样本的 IoU 数组
                           ▼
        compute_metrics：concatenate → QA Seg mIoU / Precision_half / Precision_quarter
```

## 4. 核心概念与源码讲解

### 4.1 超点级到点级 mask 的映射与多轮对齐

#### 4.1.1 概念说明

解码器输出的掩码是**超点级 logits**——形状 `(查询数, 超点数)`，每个超点一个分数。而 IoU 应该在**点级**上算（GT 数据集标注也是逐点的，只是先聚合到了超点）。把超点级掩码搬到点级不需要任何插值：每个原始点都有超点编号 `superpoints`（长度 = 点数），用 `mask[:, superpoints]` 做列索引，第 j 个点直接取「它所属超点」那一列的值。这是一次纯查表广播。

对齐问题则来自两侧的「按轮展平」：评估时一条多轮对话的**每一轮**都会独立生成回答、独立提取 `<SEG>` 查询、独立跑 6 层解码器（u5-l1 已讲）。于是预测侧和 GT 侧都是「一条 QA（单轮）或一轮（多轮）对应列表里的一个元素」。两侧各自展平后按索引配对；又因为模型是自由生成、GT 行数由标注里 `<p>` 个数决定，配对后还可能出现**行数不等**，需要补零/截断。

#### 4.1.2 核心流程

`qa_predict_by_feat` 对每个场景、每条（轮）预测做四步：

```text
for 每个场景 i_batch:
    for 每条预测 i_qa_pred:                    # 单轮 QA 一条；多轮对话每轮一条
        1. mask_pred = out['masks'][i_batch][i_qa_pred]        # (查询数, 超点数) logits
        2. mask_pred = sigmoid(mask_pred) > 0.4                # 先在超点级过阈值（sp_score_thr）
        3. mask_pred = mask_pred[:, superpoints]               # 列索引 → (查询数, 点数)
        4. GT 对齐（仅当 i_qa_pred < len(gt_masks[i_batch])）:
             gt = gt[:, superpoints]                           # GT 同样上采样到点级
             若 GT 行数 > 预测行数: 预测零填充到 GT 行数
             若 GT 行数 < 预测行数: 预测截断到 GT 行数
           （预测条目多于 GT 条目时：保留原始预测，只用于可视化）
```

注意第 2、3 步的**顺序**：先在超点级过 0.4 阈值再广播，等价于「点级阈值」（同一个超点内的点分数相同），但避免了先广播成几十万列再比较的内存浪费。源码中还留着一行被注释的旧写法（先 `[:, superpoints]` 再阈值），供读者对照。

#### 4.1.3 源码精读

GT 侧的「按轮展平」发生在评估管线 `QA_Generation_Test.transform`：

```python
# uniseg3d/transforms_3d.py:L651-L679（节选）
for i, qa in enumerate(select_qa_list):
    if isinstance(qa, dict):
        # 单轮 QA：按答案中 <p> 个数生成一条 (num_p_tags, 超点数) 的 GT
        i_qa_seg = get_seg_from_single_qa(...)
        sp_gt_seg.append(i_qa_seg)
    elif isinstance(qa, list):
        for round_qa in qa:
            round_seg = get_seg_from_single_qa(round_qa, round_p_tags, ...)
            # one GT mask PER ROUND, appended separately so it aligns 1:1 with the per-turn
            # predictions the decoder produces (do NOT concatenate rounds into one entry).
            sp_gt_seg.append(round_seg)
```

[uniseg3d/transforms_3d.py:L651-L679](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L651-L679) 说明：GT 列表 `sp_gt_seg` 是**按轮**追加的——多轮对话不拼成一个大张量，而是每轮一个 `(该轮 <p> 数, 超点数)` 条目，为的就是与预测侧的逐轮预测 1:1 配对；注释原文明确强调了这一点。

预测侧的「按轮展平」在解码器评估分支收尾处：

```python
# uniseg3d/query_decoder.py:L1949-L1959（节选）
                pred_masks.append(qa_pred_masks)   # 每条 QA（轮）一个条目
            final_pred_masks = [[pred_masks_i[-1] for pred_masks_i in pred_masks_b]
                                for pred_masks_b in pred_masks]
            return dict(
                original_queries=original_queries,
                masks=final_pred_masks,            # masks[i_batch] = 按 QA(轮) 展平的末层掩码
                language_losses=language_losses)
```

[uniseg3d/query_decoder.py:L1949-L1959](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1949-L1959) 说明：每条 QA 独立跑完 6 层后收集了 7 份掩码（初始 head + 6 层），这里 `[-1]` 只保留**末层**，按 batch → QA(轮) 两级列表返回，`masks[i][j]` 形状为 `(该轮查询数, 超点数)`。行数由模型生成回答里的 `<p>` 个数决定，因此与 GT 行数未必相等。

消费两侧并完成映射与对齐的是模型侧的 `qa_predict_by_feat`：

```python
# uniseg3d/uniseg3d.py:L1214-L1235（节选）
for i_batch in range(len(out['masks'])):
    pred_mask_= []
    for i_qa_pred in range(len(out['masks'][i_batch])):
        mask_pred = out['masks'][i_batch][i_qa_pred]
        mask_pred_sigmoid = mask_pred.sigmoid()
        # mask_pred_sigmoid = mask_pred_sigmoid[:, superpoints]
        mask_pred = mask_pred_sigmoid > self.test_cfg.sp_score_thr
        mask_pred = mask_pred[:, superpoints]
        # GT masks can be fewer than predicted masks: multi-turn produces one prediction per
        # turn, but GT exists only for the grounded turns. Align to GT when it is present;
        # otherwise keep the raw prediction so it is still available for visualization.
        if i_qa_pred < len(gt_masks[i_batch]):
            gt_masks[i_batch][i_qa_pred] = gt_masks[i_batch][i_qa_pred][:, superpoints.cpu()]
            if gt_masks[i_batch][i_qa_pred].shape[0] > mask_pred.shape[0]:
                mask_pred = torch.cat([mask_pred, torch.zeros((..., mask_pred.shape[1]))], dim=0)
            elif gt_masks[i_batch][i_qa_pred].shape[0] < mask_pred.shape[0]:
                mask_pred = mask_pred[:gt_masks[i_batch][i_qa_pred].shape[0]]
        pred_mask_.append(mask_pred.cpu().numpy())
```

[uniseg3d/uniseg3d.py:L1214-L1235](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1214-L1235) 说明：依次完成「sigmoid → 超点级阈值（`sp_score_thr`，评估配置里是 0.4，见 [configs/ground3dlmm_eval_ground3d_scannet.py:L98](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_ground3d_scannet.py#L98)）→ `[:, superpoints]` 点级上采样」，随后做行数对齐三分支：GT 行多则预测**补全零行**（这些行在与有效 GT 行比较时 IoU≈0，诚实惩罚「该指称却没输出」）；GT 行少则预测**截断**（多输出的目标直接丢弃）。若预测条目本身多于 GT 条目（`i_qa_pred >= len(gt_masks)`），保留原始预测只用于可视化——后续 `get_iou` 里的 `zip` 会把这些没有 GT 的条目自动截掉。

GT 的来源在 `predict()` 开头收集：

```python
# uniseg3d/uniseg3d.py:L1277-L1306（节选）
for i in range(len(batch_data_samples)):
    gt_pts_seg = batch_data_samples[i].gt_pts_seg
    ...
    sp_gt_masks.append(batch_data_samples[i].gt_pts_seg.sp_gt_seg)
...
x = self.decoder(x, scene_names=scene_names, qa_data=selected_qa_data, point_positions=point_positions)
results_list = self.qa_predict_by_feat(x, sp_pts_masks, sp_gt_masks)
```

[uniseg3d/uniseg3d.py:L1277-L1306](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1277-L1306) 说明：评估时 `sp_gt_seg`（`QA_Generation_Test` 的产物，u2-l5 打包进 `gt_pts_seg`）按场景收集为列表。一个值得注意的隐含约束：这里传给 `qa_predict_by_feat` 的 `sp_pts_masks` 是**已加偏移后 hstack 的全局超点编号**，而每个场景的掩码列数只覆盖本场景的超点数——`[:, superpoints]` 用全局编号索引之所以不越界，是因为评估配置的 dataloader `batch_size=1`（[configs/ground3dlmm_eval_ground3d_scannet.py:L215](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_ground3d_scannet.py#L215)），此时全局编号恰好等于该场景的局部编号；若把评估 batch_size 调到大于 1，这段索引会越界。最终 `PointData(qa_pred_masks, qa_gt_masks)` 写入 `data_sample.pred_pts_seg`（L1307-L1308）。

#### 4.1.4 代码实践：用纯 numpy 复现「上采样 + 对齐」

**实践目标**：不依赖 GPU 和 mmdet3d，直观验证 `[:, superpoints]` 的广播语义与行数对齐三分支。

**操作步骤**（以下脚本为**示例代码**，可保存为任意独立文件运行，只需 numpy）：

```python
import numpy as np

# 场景：8 个点，分属 3 个超点；一条双目标 QA
superpoints = np.array([0, 0, 1, 1, 1, 2, 2, 2])   # 逐点超点编号
n_sp, n_pts = 3, 8

# 1) 超点级预测 logits → sigmoid → 阈值 0.4 → bool，模拟 2 个查询
logits = np.log(np.array([[0.9, 0.1, 0.3],
                          [0.8, 0.7, 0.05]]) / (1 - np.array([[0.9, 0.1, 0.3],
                                                               [0.8, 0.7, 0.05]])))
mask_pred = (1 / (1 + np.exp(-logits))) > 0.4      # (2, 3)
print("sp-level pred:\n", mask_pred.astype(int))

# 2) 点级上采样：先阈值后广播（与源码同序）
point_pred = mask_pred[:, superpoints]             # (2, 8)
print("point-level pred:\n", point_pred.astype(int))

# 3) 行数对齐：GT 有 2 行(全 NaN 哨兵除外)，假设模型只输出了 1 行
gt_sp = np.array([[1., 0., 0.], [0., 1., 0.]])     # (2, 3)
gt_pts = gt_sp[:, superpoints]                     # (2, 8)
pred1 = point_pred[:1]                             # 模型只给 1 行
padded = np.concatenate([pred1, np.zeros((1, n_pts), dtype=bool)], axis=0)
print("after zero-pad:\n", padded.astype(int))
```

**需要观察的现象**：`point_pred` 的第 0 行在点 0、1（超点 0）处为 1；补零后预测第 1 行全 0，与 GT 第 1 行（点 2、3、4 为 1）求 IoU 会得到约 0。

**预期结果**：`sp-level pred` 为 `[[1,0,0],[1,1,0]]`（0.3、0.05 低于 0.4 被抑制）；上采样后每列值被复制到该超点的所有点；零填充行与有效 GT 行的 IoU 为 0。具体数值可自行手算核对。

若想在真实环境验证（需按 u1-l2 装好环境）：`python -c "from uniseg3d.uniseg3d import Grounded_LMM_main; import inspect; print(inspect.getsource(Grounded_LMM_main.qa_predict_by_feat))"` 打印完整源码对照——**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：如果把第 2、3 步调换成「先 `[:, superpoints]` 再阈值」，结果会变吗？
**答案**：不会。同一超点内所有点拿到相同 logit，先广播后阈值与先阈值后广播得到相同的布尔掩码；源码选择先阈值是为了少做 N 倍宽度的比较（被注释掉的旧写法正是调换版）。

**练习 2**：一条 3 轮多轮对话，只有第 2 轮答案含 `<p>`。GT 侧列表有几个条目？预测侧呢？
**答案**：两侧都是 3 个条目（`QA_Generation_Test` 每轮单独 append，解码器每轮独立生成与解码）。第 1、3 轮的 GT 是全 NaN 行（`num_p_tags=0`），在指标端被剔除（见 4.2）；这也解释了 `qa_predict_by_feat` 注释里「GT exists only for the grounded turns」的含义——按条目看 3 轮都有 GT 张量，但**有效**（非 NaN）GT 只存在于有 `<p>` 的轮。

**练习 3**：为什么零填充（而不是复制最后一行）是更合理的对齐策略？
**答案**：零填充表示「模型没有为这个指称输出目标」，与有效 GT 行比较得 IoU≈0，是对漏检的诚实惩罚；复制行则会凭空制造一个可能高分的匹配，虚高指标。

### 4.2 get_iou：逐 QA、逐实例的 IoU 计算

#### 4.2.1 概念说明

`get_iou` 是一个纯 numpy 函数，输入两个**平行列表**（GT 与预测，每个元素对应一条 QA/轮），输出每条 QA 一个 IoU 标量的数组。一条 QA 可能有多个目标行（双目标比较类问答有 2 个 `<p>`），所以元素是 `(行数, 点数)` 的二维数组：先逐行算 IoU（**逐实例**），再对「有效 GT 行」取均值，得到这条 QA 的得分。所谓有效，就是行内还有真值为 1 的点——NaN 行与全 0 行都被排除。

#### 4.2.2 核心流程

对每对 `(gt, pred)`：

```text
若 gt 是二维（多条目标行）:
    valid = gt 在点维度求和 > 0          # NaN 行的和是 NaN，NaN>0 为 False → 自动剔除
    gt, pred 转布尔
    每行: intersection = 按位与求和, union = 按位或求和
          IoU_i = intersection / (union + 1e-8)
    若存在有效行: 该 QA 得分 = mean(IoU_i over valid 行)
    否则: 跳过该 QA（不产生任何得分）
若 gt 是一维: 单目标直接算一个 IoU（gt 全空则跳过）
```

数学表达：对第 \(k\) 条 QA 的第 \(i\) 行（点级布尔向量 \(G_{k,i}, P_{k,i}\in\{0,1\}^N\)），

\[ \text{IoU}_{k,i} = \frac{\sum_j G_{k,i,j} \wedge P_{k,i,j}}{\sum_j G_{k,i,j} \vee P_{k,i,j} + 10^{-8}} \]

\[ \text{iou}_k = \frac{1}{|\mathcal{V}_k|}\sum_{i\in\mathcal{V}_k} \text{IoU}_{k,i},\qquad \mathcal{V}_k=\{i:\textstyle\sum_j G_{k,i,j} > 0\} \]

分子分母里的 \(10^{-8}\) 防止「GT 与预测都为空」时除零（严格说 GT 有效行保证分母非零，这是双保险）。

#### 4.2.3 源码精读

```python
# uniseg3d/unified_metric.py:L15-L53（节选）
def get_iou(gt_masks, pred_masks):
    ious = []
    for gt, pred in zip(gt_masks, pred_masks):
        gt = np.array(gt)
        pred = np.array(pred)
        if gt.ndim > 1:
            valid_gt_index = gt.sum(axis=-1) > 0          # ① 有效性判据：NaN/全0行出局
            gt = gt.astype(bool) if gt.dtype != bool else gt
            pred = pred.astype(bool) if pred.dtype != bool else pred
            intersection = (gt & pred).sum(axis=-1)        # ② 逐实例向量化 IoU
            union = (gt | pred).sum(axis=-1)
            instance_ious = intersection / (union + 1.e-8)
            if valid_gt_index.sum() > 0:
                iou = np.mean(instance_ious[valid_gt_index])
            else:
                continue                                   # ③ 整条 QA 无有效 GT：跳过
        else:
            if gt.sum() > 0:                               # ④ 1D 兼容分支
                ...
            else:
                continue
        ious.append(iou)
    return np.array(ious)
```

[uniseg3d/unified_metric.py:L15-L53](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/unified_metric.py#L15-L53) 说明：这就是上面伪代码的原型。三个要点：

- **① NaN 排除的机关**：`valid_gt_index = gt.sum(axis=-1) > 0` 是在 float 数组上做的。NaN 行求和得 NaN，而 `NaN > 0` 在 numpy 里是 `False`（并伴随一条 RuntimeWarning，属预期现象），于是 NaN 行被静默剔除；全 0 的 GT 行同样出局。注意此判据**必须**在 `astype(bool)` 之前计算——`float('nan')` 转布尔是 `True`，转完就再也区分不出来了。
- **② 向量化**：`(gt & pred).sum(axis=-1)` 一次算出所有行的交/并，`instance_ious` 是长度为行数的向量。
- **③ 全无效 QA 跳过**：`existence_verification`（答案 Yes/No，无 `<p>`）的 GT 是 `(1, 超点数)` 全 NaN（见下面数据侧引用），`valid_gt_index.sum()==0` → `continue`，该 QA **不产生任何 IoU**。这就是「无 mask 样本的排除逻辑」：不需要任务名黑名单，NaN 哨兵 + 行和判据就够了。同理 `scale_estimation`、`distance_estimation` 等纯数值任务也自动出局——mIoU 只统计「有有效掩码 GT」的接地任务。
- **④ 1D 分支**是兼容旧数据的防御路径；当前主链路里 GT/预测都是二维（行数最少为 1）。

NaN 的诞生地在数据侧（评估与训练共用同一函数）：

```python
# uniseg3d/transforms_3d.py:L272-L274 与 L386-L388（节选）
def get_seg_from_single_qa(single_qa, num_p_tags, num_superpoints, ...):
    if num_p_tags == 0:
        return torch.full((1, num_superpoints), float('nan'), dtype=torch.float32)
    ...
            if seg.sum() == 0:
                seg = torch.full((seg.shape[0], num_superpoints), float('nan'), dtype=torch.float32)
```

[uniseg3d/transforms_3d.py:L272-L274](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L272-L274) 说明：答案里数不出 `<p>` 标签时，GT 直接返回一行全 NaN——`existence_verification` 类 QA 走的就是这里；[L386-L388](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L386-L388) 说明：目标 ID 合法但掩码本身为空（比如物体在该帧无点）的行同样被替换成 NaN。NaN 一路穿透 `Pack`、`qa_predict_by_feat` 的 `[:, superpoints]` 上采样（索引不改变 NaN 值），最终在 `get_iou` 的行和判据处被消灭。

#### 4.2.4 代码实践：三组手工用例验证 get_iou

**实践目标**：用可手算的小例子确认你对 `get_iou` 逐实例与 NaN 排除的理解。

**操作步骤**：先**手工**推演下面三组用例的输出，再运行验证。

用例 A（完美预测）：`gt=[[1,1,0]]`，`pred=[[1,1,0]]` → IoU = 1.0
用例 B（部分重叠）：`gt=[[1,1,0]]`，`pred=[[1,0,0]]` → 交 1、并 2 → 0.5
用例 C（NaN 排除）：`gt=[[1,1,0],[nan,nan,nan]]`，`pred=[[0,0,0],[1,1,1]]` → 第 0 行 IoU=0 计入，第 1 行被剔除 → 该 QA 得分 0.0
用例 D（全 NaN 跳过）：`gt=[[nan,nan,nan]]`，`pred=[[1,1,1]]` → 无输出条目

在有环境的机器上可这样验证（`unified_metric.py` 顶层 import 了 mmdet3d，必须先装好 u1-l2 的环境）：

```python
from uniseg3d.unified_metric import get_iou
import numpy as np
print(get_iou([np.array([[1.,1.,0.]]), np.array([[1.,1.,0.],[np.nan]*3])],
              [np.array([[1,1,0]]),          np.array([[0,0,0],[1,1,1]])]))
# 期望: [1. 0.]（第二条 QA 只剩第 0 行有效，IoU=0）
print(get_iou([np.array([[np.nan]*3])], [np.array([[1,1,1]])]))
# 期望: [] （空数组，该 QA 被跳过；numpy 会打印 invalid value 警告，属预期）
```

**需要观察的现象**：用例 C 中预测第 1 行全 1 却不影响得分（该 GT 行是 NaN）；用例 D 返回空数组。

**预期结果**：如上两行注释所写。以上验证**待本地验证**（本讲义撰写环境未安装 mmdet3d）；若暂时没有环境，可把 4.2.3 中的 20 行核心逻辑抄成独立 numpy 函数先行验证，逻辑与源码逐行对应。

#### 4.2.5 小练习与答案

**练习 1**：一条 QA 的 GT 两行都有效，模型一行完美（IoU=1）、一行全空（IoU=0），该 QA 得分是多少？这体现了什么粒度？
**答案**：`(1+0)/2 = 0.5`。体现的是**QA 级（指称级）平均**——双目标问答必须两个都对才能拿满分，比「点级 IoU」更严格地反映接地完整性。

**练习 2**：如果模型对某条接地 QA 完全没有输出 `<SEG>`（预测行被零填充），这条 QA 的得分是多少？
**答案**：0（交为 0、并为 GT 大小），该 QA 仍计入分母——漏检被惩罚而不是被跳过；跳过只发生在 **GT 无效**时。

**练习 3**：为什么 `get_iou` 用 `zip` 而不是按长度断言对齐？
**答案**：`zip` 按较短列表截断，天然吸收「预测条目多于 GT 条目」的情形（`qa_predict_by_feat` 对无 GT 的预测条目原样保留）；若用断言，多轮对话中任何一侧计数不一致都会让整个评估崩溃。这是一种「评估永远能跑完」的稳健性取舍。

### 4.3 Part3DGlamm_SegMetric：process 攒账、compute 结账

#### 4.3.1 概念说明

`Part3dGlamm_SegMetric` 注册在 `METRICS` 上，继承 mmdet3d 的 `SegMetric`（后者又继承 mmengine `BaseMetric`，提供 `evaluate`/结果收集骨架）。它完全不看场景级标注（`eval_ann_info` 在代码里被注释掉了），只消费 `data_sample.pred_pts_seg` 里的 `qa_pred_masks`/`qa_gt_masks`。设计上有个省内存的关键决定：`process` 阶段就把掩码折算成 IoU 数组，`self.results` 里**只存数字不存掩码**——否则几百个场景 × 每场景几十万点 × 每 QA 若干行的布尔矩阵会把内存吃爆。

#### 4.3.2 核心流程

```text
每个 batch 推理结束:
    process(data_batch, data_samples)
        for 每个场景:
            取 pred_pts_seg 里的 qa_gt_masks / qa_pred_masks
            torch.Tensor / list[Tensor] → numpy 统一
            若 GT 列表为空 → 记一个空数组
            否则 → cur_iou = get_iou(qa_gt_masks, qa_pred_masks)
            self.results.append(cur_iou)          # 只存每场景的 IoU 数组

全部数据跑完（mmengine 收集各 rank 结果后）:
    compute_metrics(results)
        flat = np.concatenate(所有场景的 IoU 数组)  # 每个元素 = 一条有效 QA 的得分
        metrics = {
            'QA Seg mIoU':            mean(flat),
            'QA Seg Precision_half':  #{iou > 0.5} / N,
            'QA Seg Precision_quarter': #{iou > 0.25} / N }
```

注意两级平均：`get_iou` 里已经对一条 QA 的多目标行取过均值（QA 级得分），`compute_metrics` 的 `mean` 是把**每条有效 QA 当一个样本**再平均。Precision 系列用的 `>` 是严格大于。

#### 4.3.3 源码精读

指标在评估配置中的挂载（唯一的实例化入口）：

```python
# configs/ground3dlmm_eval_ground3d_scannet.py:L242-L246
val_evaluator = dict(
    type='Part3dGlamm_SegMetric',
    min_num_points=1, 
    id_offset=2**16)
test_evaluator = val_evaluator
```

[configs/ground3dlmm_eval_ground3d_scannet.py:L242-L246](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_ground3d_scannet.py#L242-L246) 说明：配置只传了 `min_num_points` 与 `id_offset` 两个参数——稍后会看到，这两个参数在本类的计算路径里**根本没有被用到**，是从同文件里更早的 `PromptSupportedUnifiedSegMetric` 抄配置时留下的遗迹。

`process` 的攒账逻辑：

```python
# uniseg3d/unified_metric.py:L411-L436（节选）
for data_sample in data_samples:
    pred_3d = data_sample['pred_pts_seg']
    # eval_ann_info = data_sample['eval_ann_info']
    qa_gt_masks = pred_3d['qa_gt_masks']
    qa_pred_masks = pred_3d['qa_pred_masks']
    if isinstance(qa_gt_masks, torch.Tensor):              # 张量/list 双兼容 → numpy
        qa_gt_masks = qa_gt_masks.cpu().numpy()
    elif isinstance(qa_gt_masks, list) and ...:
        qa_gt_masks = [m.cpu().numpy() for m in qa_gt_masks]
    ...
    if len(qa_gt_masks) == 0:
        cur_sample_prompt_IoUs_qa = np.array([])           # 场景无 QA：空账页
    else:
        cur_sample_prompt_IoUs_qa = get_iou(qa_gt_masks, qa_pred_masks)
    # Append only the IoU results, not the heavy masks
    self.results.append(cur_sample_prompt_IoUs_qa)
```

[uniseg3d/unified_metric.py:L411-L436](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/unified_metric.py#L411-L436) 说明：逐场景读取两个键（正是 4.1 中 `PointData` 写入的产物），把 GPU 张量搬到 CPU numpy（`get_iou` 只认 numpy），空场景记空数组；最后一行注释点明内存策略——`self.results` 每个元素是一个长度为「该场景有效 QA 数」的 float 数组，掩码本身即刻丢弃。

`compute_metrics` 的结账逻辑：

```python
# uniseg3d/unified_metric.py:L448-L466（节选）
logger: MMLogger = MMLogger.get_current_instance()
# results contains list of IoU arrays from all ranks
val_set_prompt_IoUs_qa_list = results
if len(val_set_prompt_IoUs_qa_list) > 0:
    val_set_prompt_IoUs_qa = np.concatenate(val_set_prompt_IoUs_qa_list)
else:
    val_set_prompt_IoUs_qa = np.array([])
metrics = dict()
if len(val_set_prompt_IoUs_qa) > 0:
    pre_half = (val_set_prompt_IoUs_qa > 0.5).sum() / len(val_set_prompt_IoUs_qa)
    pre_quarter = (val_set_prompt_IoUs_qa > 0.25).sum() / len(val_set_prompt_IoUs_qa)
    metrics.update({'QA Seg mIoU': np.mean(val_set_prompt_IoUs_qa),
                    'QA Seg Precision_half': pre_half,
                    'QA Seg Precision_quarter': pre_quarter})
return metrics
```

[uniseg3d/unified_metric.py:L448-L466](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/unified_metric.py#L448-L466) 说明：注释「results contains list of IoU arrays from all ranks」表明作者按 mmengine 的分布式约定编写——各 rank 各自 `process`，evaluate 时中间结果按 `collect_device` 收集合并后才进入这里（具体收集行为以所用 mmengine 版本实现为准；单卡时 `results` 就是逐场景数组的普通列表）。`np.concatenate` 把所有场景的账页拍平成一维数组，随后输出三个指标；若整个验证集没有任何有效 QA（例如误把 `task_type` 过滤成纯文本任务），返回空 dict，日志里不会出现这三个键——这是一个值得记住的「指标静默消失」信号。

还有一段值得留意的「考古」：

```python
# uniseg3d/unified_metric.py:L387-L396（节选）
def __init__(self,
             min_num_points,
             id_offset,
             logger_keys=[('miou',),
                          ('all_ap', 'all_ap_50%', 'all_ap_25%')],
             **kwargs):
    self.min_num_points = min_num_points
    self.id_offset = id_offset
    self.logger_keys = logger_keys
    super().__init__(**kwargs)
```

[uniseg3d/unified_metric.py:L387-L396](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/unified_metric.py#L387-L396) 说明：`__init__` 存下 `min_num_points`、`id_offset`、`logger_keys`，但 `compute_metrics` 从未读取它们——这三个是从泛分割指标 `PromptSupportedUnifiedSegMetric`（同文件 [L153-L173](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/unified_metric.py#L153-L173)）继承签名时的残留；同理，类体里的 `cal_ap_thr`/`cal_ap`/`map_inst_markup`（[L468-L523](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/unified_metric.py#L468-L523)）在本类的计算路径中从未被调用，属于复制粘贴的死代码。读源码时能识别这类「遗迹」可以省下不少困惑。

#### 4.3.4 代码实践：让三个指标键现形

**实践目标**：在真实评估中看到 `QA Seg mIoU` 等三个键，并验证 `min_num_points` 是死参数。

**操作步骤**：

1. 按 u1-l4 跑一次小范围评估（复用已下载的 checkpoint，只评一个子任务以缩短时间）：

   ```bash
   python tools/test.py configs/ground3dlmm_eval_ground3d_scannet.py <ckpt.pth> \
       --cfg-options val_evaluator.min_num_points=999
   ```

2. 观察日志末尾的指标输出块。
3. 再把 `min_num_points` 改回 `1` 跑一次，对比两次的三个 `QA Seg *` 数值。

**需要观察的现象**：日志出现 `QA Seg mIoU`、`QA Seg Precision_half`、`QA Seg Precision_quarter` 三个键（键名可能带 mmengine 的默认前缀，如 `seg/`，以实际日志为准）；两次运行的数值应逐位一致。

**预期结果**：`min_num_points=999` 与 `=1` 结果完全相同——证明该参数不参与计算。若日志里三个键缺失，先检查 `task_type/sub_task` 是否把接地任务全过滤掉了（u1-l4 讲过过滤是严格匹配、拼错静默为空）。本实践需要 GPU 与数据，**待本地验证**；无环境时的替代方案：通读 [tools/test.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/test.py) 中 `runner.test()` 之后的日志输出路径，确认指标字典由 `TestLoop` 经 `val_evaluator` 打印。

#### 4.3.5 小练习与答案

**练习 1**：100 条 QA 里 60 条是 `existence_verification`（无 `<p>`），其余 40 条 IoU 平均为 0.5。最终 `QA Seg mIoU` 是多少？
**答案**：0.5。60 条无 mask QA 在 `get_iou` 里被 `continue` 跳过，既不进分子也不进分母——mIoU 是**有效 QA 上的平均**，不是全体 QA 上的平均。

**练习 2**：`process` 为什么不直接 `self.results.append((qa_gt_masks, qa_pred_masks))`，把 IoU 留到 `compute_metrics` 再算？
**答案**：内存。掩码是 `(行数, 点数)` 布尔矩阵，一个场景几十万点 × 若干 QA 行，几百个场景累积会占用大量内存；`process` 阶段折算成每场景几十个 float，`self.results` 体积缩小几个数量级。代价是调试时无法事后重算 IoU——想拿原始掩码得走 u3-l1 讲过的 `MASK_DUMP_DIR` 落盘通道。

**练习 3**：如果想让 mIoU 按「每行（每个指称目标）等权」而不是「每条 QA 等权」，需要改哪里？
**答案**：改动 `get_iou` 的返回粒度——把 `np.mean(instance_ious[valid_gt_index])`（[L38-L39](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/unified_metric.py#L38-L39)）改为直接返回 `instance_ious[valid_gt_index]`（拼接时再拍平），`compute_metrics` 的 `np.mean` 无需改动。两种口径对双目标 QA 的权重不同，报告数字时必须注明口径。

## 5. 综合实践

**任务：手工推演一个 2 场景的 mini 评估，再验证。**

构造如下评估结果（均为点级布尔，长度 6）：

- 场景 1（2 条 QA）：
  - QA-1：接地任务，GT = `[[1,1,0,0,0,0]]`，预测 = `[[1,1,0,0,0,0]]`。
  - QA-2：双目标比较，GT = `[[1,1,0,0,0,0],[0,0,0,1,1,0]]`，预测只有 1 行 `[[1,1,0,0,0,0]]`（另一行被零填充）。
- 场景 2（2 条 QA）：
  - QA-3：`existence_verification`，GT = `[[nan]*6]`，预测 = `[[0,0,0,1,1,1]]`。
  - QA-4：接地任务，GT = `[[0,0,1,1,0,0]]`，预测 = `[[0,0,1,0,0,0]]`。

请依次回答：

1. 每条 QA 在 `get_iou` 处得到什么（得分或跳过）？
2. `process` 后 `self.results` 是什么？
3. `compute_metrics` 输出的三个指标各是多少？

**参考答案**：

1. QA-1：交 2、并 2 → **1.0**。QA-2：第 0 行交 2 并 2 → 1.0；第 1 行预测全 0（零填充），交 0 并 2 → 0.0；QA 得分 = (1.0+0.0)/2 = **0.5**。QA-3：GT 全 NaN → `continue`，**跳过**。QA-4：交 1 并 3 → **1/3 ≈ 0.333**。
2. `self.results = [array([1.0, 0.5]), array([0.333...])]`——每个场景一个小数组；场景 2 里被跳过的 QA-3 不产生任何元素，该数组只含 QA-4 的得分。
3. 拍平后 `[1.0, 0.5, 0.333]`：mIoU = 1.833/3 ≈ **0.611**；Precision_half（>0.5）= 1/3 ≈ **0.333**（只有 1.0 过线，0.5 不满足严格大于）；Precision_quarter（>0.25）= 3/3 = **1.0**（0.333 > 0.25）。

验证方式：把上面的数组按 4.2.4 的方式喂给 `get_iou`，再用 `np.concatenate` + 三行统计复现第 3 问（环境就绪时可直接调用真实函数对照；**待本地验证**）。这个练习串起了本讲全部三个模块：点级上采样的产物形态、NaN 排除、两级平均与 Precision 口径。

## 6. 本讲小结

- 掩码评估发生在**点级**，但由**超点级**预测经 `mask[:, superpoints]` 一次列索引广播得到；阈值（`sp_score_thr=0.4`）在超点级施加，先后顺序不影响结果。
- GT 与预测都**按轮展平**（多轮对话每轮一个条目），行数不一时「GT 多则预测补零、GT 少则预测截断」，无 GT 的预测条目保留仅供可视化、被 `zip` 截掉。
- `get_iou` 是两级平均：QA 内先对**有效 GT 行**（行和 > 0）逐实例平均，`existence_verification` 等无 `<p>` 任务的 GT 是全 NaN，NaN 行和为 NaN、不满足 > 0，整条 QA 被 `continue` 跳过——排除逻辑不依赖任务名。
- `Part3dGlamm_SegMetric.process` 把每个场景折算成 IoU 数组（不存掩码省内存），`compute_metrics` 拼平后输出 `QA Seg mIoU` 与严格大于口径的 `Precision_half/quarter`。
- `min_num_points`、`id_offset`、`logger_keys` 及 `cal_ap*`/`map_inst_markup` 均是从 `PromptSupportedUnifiedSegMetric` 抄来的死参数/死代码，读配置与源码时不要被它们误导。

## 7. 下一步学习建议

- 下一讲 **u6-l2 评估启动器 eval_ground3d.sh 与配置族**：本讲的指标是如何被 `--levels/--sub_tasks` 切换、如何在 joint/_3d/scannetpp 四套配置下批量跑起来的。
- 若想拿到本讲「被丢弃」的原始掩码做可视化，回顾 u3-l1 的 `MASK_DUMP_DIR` 落盘通道，并预习 **u6-l4 接地可视化**。
- 文本侧指标（APE、δ@1.25、LLM judge、GM-δ）在 **u6-l3**，与本讲的分割指标互补，共同构成完整评估体系。
- 延伸阅读：mmengine `BaseMetric` 的 `process`/`compute_metrics`/`collect_device` 约定，以及 mmdet3d `SegMetric` 的原始实现，有助于分清本指标类里哪些是继承、哪些是覆写、哪些是遗迹。
