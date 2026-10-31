# 推理链路：一次前向输出六任务结果

## 1. 本讲目标

学完本讲，你应该能够：

1. 顺着 `UniSeg3D.predict()` 画出一次前向的完整推理链路：前向特征提取 → 提示注入与拆分 → 四路结果生成 → 回写 `data_sample`。
2. 说出 `test_cfg` 中每个阈值（`topk_insts`、`inst_score_thr`、`pan_score_thr`、`npoint_thr`、`obj_normalization`、`sp_score_thr`、`nms`、`matrix_nms_kernel`、`stuff_classes`）在代码中的具体落点与作用。
3. 理解「超点级掩码 → 原始点级掩码」的映射技巧：用一个 `(n_raw_points,)` 的 `superpoints` 索引张量对掩码矩阵做花式索引。
4. 理解实例预测的七道过滤工序、语义预测的 argmax 映射、全景预测的 things/stuff 合并规则，以及点/文本提示结果为何可以「直通输出」。
5. 亲手做一次推理阈值消融实验，并逐行解释指标变化的代码来源。

## 2. 前置知识

本讲是「高级篇」的第一讲，默认你已读完以下内容（不熟悉请先回看）：

- **超点即查询**（u4-l1/u4-l3）：推理时全部超点特征充当查询，因此解码器输出的 `masks` 形状是 `(n_queries, n_sps)`——查询轴与超点轴是同一个轴。
- **提示查询注入与拆分**（u4-l6）：点提示、文本提示依次拼到原始查询尾部，布局为 `[原始 | 点 | 文本]`，拆分时先拆文本（最尾）再拆点。
- **球面分类**（u4-l4）：`cls_preds` 来自与 CLIP 类别原型的余弦相似度，形状 `(n_queries, n_instance_classes + 1)`，最后一列是全零原型的背景列；`objectness_flag=False` 时 `scores` 恒为 `None`。
- **IoU 预测分支**（u5-l4）：`pred_iou_proj` 从查询向量回归掩码 IoU，推理时由 `inst_test_iou` / `pano_test_iou` 两个开关决定是否把 `preds_iou` 乘进分数。
- **评估标注的 id 体系**（u3-l3）：评估用 `eval_ann_info` 中 stuff 类用 0/1、things 实例从 2 起编号——本讲会看到全景预测的输出恰好对齐这套 id 体系。

另外补充一个本讲反复用到的 PyTorch 技巧：**花式索引映射**。设 `masks` 形状为 `(n_cand, n_sps)`，`superpoints` 是形状 `(n_raw_points,)` 的长整型张量、每个元素取值在 `[0, n_sps)` 内（第 `i` 个原始点属于第 `superpoints[i]` 个超点），那么：

```python
masks[:, superpoints]   # -> (n_cand, n_raw_points)
```

结果矩阵的第 `j` 列就是 `masks` 的第 `superpoints[j]` 列。一条语句完成「超点级预测 → 原始点级预测」的广播，这是全篇推理代码把预测从超点空间搬回点空间的标准手法。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [uniseg3d/uniseg3d.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py) | 主模型。`UniSeg3DMixin` 提供 `predict_by_feat` 系列后处理；`UniSeg3D.predict()` 是推理总控；被注释的开放词汇代码块也在本文件 |
| [uniseg3d/mask_matrix_nms.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/mask_matrix_nms.py) | 从 mmdet 拷贝改写的矩阵 NMS，只改了 `masks` 的输入形状 |
| [configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py) | `test_cfg` 全部推理阈值的定义处；`inst_test_iou`/`pano_test_iou` 开关在 model 段 |
| [uniseg3d/unified_metric.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_metric.py) | 推理结果的下游消费者（下一讲 u6-l2 的主角，本讲只看它取哪些键） |
| [tools/dist_test.sh](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/tools/dist_test.sh) | 推理启动脚本，额外参数经 `${@:4}` 透传给 test.py |

## 4. 核心概念与源码讲解

本讲按推理数据的流向拆成 5 个最小模块：先看总控 `predict()`（4.1），再沿它的三次调用分别深入实例过滤链（4.2）与语义/全景预测（4.4）；矩阵 NMS 单独成节（4.3）；最后看点/文本提示的直通输出（4.5）。

### 4.1 推理总控 predict()：一次前向与四路结果分发

#### 4.1.1 概念说明

训练时模型的出口是 `loss()`，推理时的出口是 `predict()`。两者的前半段（体素化 → 稀疏骨干 → 超点聚合 → 提示注入 → 解码）几乎逐行相同，关键差异有四点：

1. **不传弹性坐标**：推理调用 `collate` 时不传 `elastic_coords`，坐标直接除以 `voxel_size` 体素化（与 u3-l6 的结论呼应：测试侧恒走无弹性分支）。
2. **不采样查询**：训练用 `_select_queries` 随机丢查询，推理用 `copy.deepcopy(x)` 取全部超点作查询。
3. **拆分用 `mode='val'`**：`masks` 是二维张量，拆提示查询时按行切片（`[-n:, :]`），而训练模式拆的是列表。
4. **出口是结果字典而非损失**：解码输出被拆成「原始查询输出 + 文本提示输出 + 点提示输出」三份，前向一次即可同时服务五类任务（全景/语义/实例/交互式/指代），开放词汇所需的类无关掩码也来自同一份原始查询输出（注释块）。

#### 4.1.2 核心流程

```text
predict(batch_inputs_dict, batch_data_samples)
├─ 1. 逐场景收集超点标签 sp_pts_mask，加全局偏移后 hstack 成 sp_pts_masks
├─ 2. collate(points)（不带弹性坐标）→ 体素坐标/特征/inverse_mapping
├─ 3. SparseConvTensor → extract_feat → 每场景超点特征列表 x
├─ 4. queries = deepcopy(x)（全量超点查询，不采样）
├─ 5. 有 point_prompts → add_point_prompt_queries（拼到尾部）
│      有 text_token   → add_text_prompt_queries（再拼到尾部）
│      布局: [原始 | 点提示 | 文本提示]
├─ 6. 构造 interaction_masks（与训练相同的三组隔离规则）
├─ 7. decoder(x, queries) 一次前向
├─ 8. 先拆文本（最尾）→ text_prompt_predict_by_feat 生成指代分割结果
│      再拆点提示        → point_prompt_predict_by_feat 生成交互式分割结果
├─ 9. pred_iou=True → get_pred_iou(x)：只为原始查询补 preds_iou
│      （紧随其后是被注释的开放词汇类无关掩码保存块，见 u6-l3）
├─10. predict_by_feat(x, sp_pts_masks)：实例 + 语义 + 全景 三任务结果
└─11. 逐样本回写 data_sample：pred_pts_seg / point_prompt_pred_pts_seg /
       point_prob / text_prompt_pred_pts_seg
```

#### 4.1.3 源码精读

先看前向段。注意第 780-781 行 `collate` 只传了 `points`，没有传 `elastic_coords`：

- [uniseg3d/uniseg3d.py:L767-L787](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L767-L787)：逐场景累加超点全局偏移、无弹性坐标的体素化、稀疏张量构建与超点特征提取。`sp_pts_masks` 最终是全部场景超点标签的水平拼接（batch_size=1 时就是本场景的 `(n_raw_points,)` 索引张量）。
- [uniseg3d/uniseg3d.py:L789-L817](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L789-L817)：`queries = copy.deepcopy(x)` 全量查询；依次注入点、文本提示并构造 `interaction_masks`；`self.decoder(x, queries, interaction_masks=...)` 完成唯一一次解码器前向。

再看拆分与分发段——这是「一次前向出多任务」的直接证据：

- [uniseg3d/uniseg3d.py:L819-L827](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L819-L827)：先以 `mode='val'` 拆出文本提示输出并立即生成指代分割结果；再拆点提示生成交互式结果；随后 `get_pred_iou(x)` 只对**拆分后**的原始查询输出补 `preds_iou` 键。查询布局是 `[原始 | 点 | 文本]`，所以必须先拆最尾部的文本、再拆点，两次切片后 `x` 只剩原始查询。
- [uniseg3d/uniseg3d.py:L829-L873](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L829-L873)：被整段注释的开放词汇代码块——用 `mask_score × preds_iou > iou_threshold` 过滤、按 `mask_threshold` 二值化后把类无关掩码存盘。README 明确指向 L840-L872 这段，详细展开留给 u6-l3。
- [uniseg3d/uniseg3d.py:L875](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L875)：`results_list = self.predict_by_feat(x, sp_pts_masks)` 生成实例/语义/全景三任务结果（4.2 与 4.4 节）。
- [uniseg3d/uniseg3d.py:L877-L888](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L877-L888)：逐样本回写。`pred_pts_seg` 是三任务共用容器；`point_prob`（L882-L884）把点提示的分类 logits 做 softmax 后，用 `torch.gather` 取出**GT 类别处**的概率，作为交互式任务 AP 排序用的置信度。

最后看 `predict_by_feat` 如何把三路子结果装进一个 `PointData`：

- [uniseg3d/uniseg3d.py:L62-L91](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L62-L91)：`pts_semantic_mask` 是长度 2 的列表 `[语义任务图, 全景语义图]`，`pts_instance_mask` 是 `[实例任务掩码, 全景实例图]`。这个「双元素列表」的设计是给评估器用的——[uniseg3d/unified_metric.py:L178-L198](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_metric.py#L178-L198) 中语义任务取 `pts_semantic_mask[0]`、实例任务取 `pts_instance_mask[0]`、全景任务取两个 `[1]`。注意实例任务调用传的是 `inst_score_thr`（0.0）且 `is_test_iou=False`，全景任务传 `pan_score_thr`（0.5）且 `is_test_iou=True`——**同一条过滤链被两个任务以不同阈值复用**。

一个值得记录的工程细节：`predict_by_feat_instance`、`point_prompt_predict_by_feat` 内部都用 `out['masks'][0]`、`point_prompt_instances[0]` 这样的硬编码下标 0（如 [uniseg3d/uniseg3d.py:L26](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L26)），而传入的 `superpoints` 却是多场景拼接的。两者只在**每卡测试 batch_size=1** 时才一致，因此推理隐含了 batch_size=1 的约定（mmdet3d 基础配置的 val/test dataloader 默认 batch_size 即为 1，与该约定吻合；具体数值可在本地 `print(dataloader.batch_size)` 验证）。

#### 4.1.4 代码实践

**实践目标**：验证「一次前向、四路回写」，亲眼看到 `data_sample` 上的四个结果键。

**操作步骤**（源码阅读 + 本地插桩，以下 diff 为示例代码，仅用于练习、不要提交）：

1. 阅读 [tools/dist_test.sh](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/tools/dist_test.sh)，确认 `${@:4}` 会把 `--cfg-options` 透传给 `tools/test.py`。
2. 在 [uniseg3d/uniseg3d.py:L888](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L888)（`return batch_data_samples` 之前）临时加两行日志：

```python
# 示例代码：临时插桩
for k in ('pred_pts_seg', 'point_prompt_pred_pts_seg',
          'point_prob', 'text_prompt_pred_pts_seg'):
    v = getattr(batch_data_samples[0], k, '<缺失>')
    print(f'[debug] {k}: {type(v).__name__}')
print(f'[debug] inst masks: {batch_data_samples[0].pred_pts_seg.pts_instance_mask[0].shape}, '
      f'sem mask: {batch_data_samples[0].pred_pts_seg.pts_semantic_mask[0].shape}')
```

3. 按 README 的推理命令跑一个场景（数据与权重齐备时）：

```bash
CUDA_VISIBLE_DEVICES=0 ./tools/dist_test.sh \
  configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py \
  work_dirs/ckpts/model_best_2.pth 1
```

**需要观察的现象**：日志里四个键全部存在且类型为 `PointData`/`Tensor`；`pts_instance_mask[0]` 形状为 `(n_preds, n_raw_points)`，`pts_semantic_mask[0]` 形状为 `(n_raw_points,)`。

**预期结果**：四键齐备、形状与上述一致；`n_preds` 通常远小于 `topk_insts=600`（后续阈值会删掉大部分候选）。待本地验证（本讲义写作环境无 GPU 与数据，未实际运行）。

#### 4.1.5 小练习与答案

**练习 1**：对照 [uniseg3d/uniseg3d.py:L419-L497](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L419-L497) 的 `loss()`，找出 `predict()` 在前向段的至少 3 处差异。

**答案**：① `predict()` 调 `collate` 时不传 `elastic_coords`（L780-781 vs L449-451）；② `predict()` 无 `_select_queries`，用 `deepcopy(x)` 全量查询（L789 vs L458）；③ `predict()` 拆分提示用 `mode='val'`（L820、L823），训练默认 `mode='train'`；④ `predict()` 末尾把 `preds_iou` 接到原始查询输出上后直接做后处理，而 `loss()` 把三路输出一起喂给 criterion。

**练习 2**：为什么拆分顺序必须「先文本、后点提示」？

**答案**：注入顺序是先点后文本（L796-799），查询布局为 `[原始 | 点 | 文本]`，文本提示位于最尾部。链式从尾部切片时只能先取 `-n_text:` 得到文本输出、剩余序列的尾部再变成点提示，`[:-n_point:]` 才正确；若先拆点，切片 `[-n_point:]` 会把文本查询误认为点提示。

**练习 3**：`pts_semantic_mask` 为什么是长度 2 的列表而不是两个独立的键？

**答案**：因为语义任务与全景任务共享同一次前向，`Pack3DDetInputs_`/`PointData` 的字段按约定组织；评估器 `PromptSupportedUnifiedSegMetric.compute_metrics` 恰好按下标 `[0]`/`[1]` 分别取「纯语义任务」与「全景任务」的图（[uniseg3d/unified_metric.py:L178-L187](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_metric.py#L178-L187)），列表结构让两套结果在一个字段里成对传输。

### 4.2 实例预测 predict_by_feat_instance：七道工序的阈值过滤链

#### 4.2.1 概念说明

解码器输出的是「每个查询 × 每个类别」的打分矩阵与「每个查询」的掩码 logits，而实例分割任务需要的是「一组带类别、带分数、互不重叠的点级二值掩码」。`predict_by_feat_instance` 就是把前者变成后者的流水线，共七道工序：

| 工序 | test_cfg 键（默认值） | 代码落点 | 作用 |
| --- | --- | --- | --- |
| ① 分数展开 | — | L113-L122 | softmax 去掉背景列，展开 query×class 候选 |
| ② topk 截断 | `topk_insts`（600） | L123-L129 | 全部候选里取前 600 个（查询，类别）对 |
| ③ obj 归一化 | `obj_normalization`（True） | L132-L135 | 分数乘上掩码正区域的平均 sigmoid |
| ④ 矩阵 NMS | `nms`（True）+ `matrix_nms_kernel`（linear） | L137-L140 | 同类重叠候选的分数衰减（4.3 节） |
| ⑤ 超点映射 | `sp_score_thr`（0.4） | L142-L143 | 掩码从超点级映射回原始点并二值化 |
| ⑥ 分数过滤 | `inst_score_thr`（0.0）/ `pan_score_thr`（0.5） | L145-L149 | 按任务各自阈值删低分候选 |
| ⑦ 点数过滤 | `npoint_thr`（100） | L151-L156 | 删点数不足的小掩码 |

阈值全部定义在 [configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:L127-L136](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L127-L136)；`is_test_iou` 开关则来自 model 段的 [L29-L30](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L29-L30)（`inst_test_iou=False`、`pano_test_iou=True`）。

#### 4.2.2 核心流程

```text
输入: cls_preds (n_q, 19), masks (n_q, n_sps), scores=None 或 (n_q,1), preds_iou (n_q,1)
① scores = softmax(cls_preds)[:, :-1]                  -> (n_q, 18)  去背景列
   labels = arange(18) 按查询平铺                       -> (n_q*18,)
② 候选展平后 topk(600)，topk_idx // 18 还原查询下标     -> (600,)
   取出 600 条掩码并 sigmoid
③ obj_normalization: scores *= 正区域平均 sigmoid
④ mask_matrix_nms(sigmoid 掩码, labels, scores, linear)  -> 衰减后的 scores
⑤ masks[:, superpoints] > 0.4                          -> (600, n_raw_points) bool
⑥ scores > score_thr 删行
⑦ mask.sum(1) > 100 删行
输出: (masks (n_preds, n_raw_points), labels (n_preds,), scores (n_preds,))
```

两个容易忽略的要点：

- **候选数是查询数 × 类别数**。一个场景约几千个超点查询、18 个 thing 类，候选总数可达数万，所以第一步就需要 topk 截断到 600。
- **`predict_by_feat_instance` 被调用两次**：实例任务用 `inst_score_thr=0.0`、不乘 `preds_iou`；全景任务用 `pan_score_thr=0.5`、乘 `preds_iou`（见 4.1.3 最后一段引用的 L77-L80 与 L195-L196）。改 `topk_insts` 或 `obj_normalization` 会**同时**影响这两个任务。

#### 4.2.3 源码精读

- [uniseg3d/uniseg3d.py:L110-L125](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L110-L125)：工序①②。`scores = F.softmax(cls_preds, dim=-1)[:, :-1]` 去掉最后一列背景（全零原型列，见 u4-l4）；`out['scores']` 在 `objectness_flag=False` 时为 `None`（[uniseg3d/query_decoder.py:L474-L476](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/query_decoder.py#L474-L476)），该分支实际不生效，实例置信度唯一来源是球面分类 logits；`is_test_iou` 为真时再把 `preds_iou` 乘进分数（u5-l4 讲过的推理应用点）。`topk` 之后用 `torch.div(topk_idx, num_classes, rounding_mode='floor')` 从「展平候选下标」还原「查询下标」，因为候选是按 `查询*18 + 类别` 的顺序展平的。
- [uniseg3d/uniseg3d.py:L127-L135](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L127-L135)：按查询下标选出 600 条掩码并 sigmoid；`obj_normalization` 计算

  \[ \text{mask\_score}_i = \frac{\sum_j \sigma(z_{ij})\cdot\mathbb{1}[z_{ij}>0]}{\sum_j \mathbb{1}[z_{ij}>0] + \epsilon} \]

  即「掩码自己声称覆盖的区域里，平均置信度有多高」，再乘进总分——一个掩码 logits 虚高的候选会在这里被压分。
- [uniseg3d/uniseg3d.py:L137-L143](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L137-L143)：工序④⑤。把**软掩码**（sigmoid 概率，非二值）交给矩阵 NMS；随后 `mask_pred_sigmoid[:, superpoints]` 用花式索引把 `(600, n_sps)` 映射成 `(600, n_raw_points)`，再以 `sp_score_thr=0.4` 二值化——每个原始点直接继承其超点的分数。
- [uniseg3d/uniseg3d.py:L145-L158](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L145-L158)：工序⑥⑦。`score_mask = scores > score_threshold` 与 `npoint_mask = mask_pointnum > npoint_thr` 两个布尔向量依次对 `scores/labels/mask_pred` 三元组同步删行，返回最终实例列表。注意 `inst_score_thr=0.0` 意味着实例任务的分数过滤形同虚设——真正裁剪实例任务候选的是 NMS 之后的 `npoint_thr` 与 topk 上限；较严格的 `pan_score_thr=0.5` 只作用于全景路径。

#### 4.2.4 代码实践

**实践目标**：用小张量手动复现「候选展开 → topk → 查询还原」这段逻辑，确认 `topk_idx // num_classes` 的正确性。

**操作步骤**（示例代码，可在任意有 torch 的环境运行，不依赖数据集）：

```python
# 示例代码
import torch
import torch.nn.functional as F

torch.manual_seed(0)
n_q, num_classes = 50, 18
cls_preds = torch.randn(n_q, num_classes + 1)          # (50, 19)

scores = F.softmax(cls_preds, dim=-1)[:, :-1]          # (50, 18)
labels = torch.arange(num_classes).unsqueeze(0).repeat(n_q, 1).flatten(0, 1)
scores, topk_idx = scores.flatten(0, 1).topk(10, sorted=False)
labels = labels[topk_idx]
query_idx = torch.div(topk_idx, num_classes, rounding_mode='floor')

# 反向验证：展平下标 = query_idx * 18 + 类别
assert (query_idx * num_classes + labels == topk_idx).all()
print('查询下标:', query_idx.tolist())
print('类别    :', labels.tolist())
```

**需要观察的现象**：断言通过；同一条查询可以贡献多个不同类别的候选（`query_idx` 中出现重复值）。

**预期结果**：断言通过即证明 `// num_classes` 还原正确；topk 越小，同查询多候选的现象越少。本段逻辑与 L119-L127 逐行对应。

#### 4.2.5 小练习与答案

**练习 1**：把 `topk_insts` 从 600 改成 50，哪些任务会受影响？语义分割会吗？

**答案**：受影响的是实例分割（IS）与全景分割（PS），因为两者都经过 `predict_by_feat_instance`。语义分割不受影响——`predict_by_feat_semantic` 只读 `sem_preds` 做 argmax（L160-L175），完全不经过 topk/NMS；交互式与指代任务的直通输出（4.5 节）同样不受影响。

**练习 2**：为什么实例任务的 `inst_score_thr` 设成 0.0（等于不过滤），而全景任务用 0.5？

**答案**：实例分割评估（mAP）本身按分数排序计算 PR 曲线，低分候选留着不损害 AP，反而保住召回；全景分割要把每个点唯一分给 stuff 或某个实例，低质量实例会污染点级全景标注，所以在进入合并规则前就用 0.5 的较高阈值砍掉弱候选，且乘上 `preds_iou` 进一步校准分数。

**练习 3**：`sp_score_thr` 从 0.4 提到 0.5，预测掩码会怎么变？哪些任务的输出会变？

**答案**：二值化阈值提高使每个掩码收缩（边界超点被剥掉），点数变少，于是更多掩码在工序⑦被 `npoint_thr=100` 删掉——实例数下降、掩码更紧。受影响的是所有用 `sp_score_thr` 的任务：实例、全景（L143），以及点/文本提示分支（L37、L58 也读同一个 `self.test_cfg.sp_score_thr`）。纯语义任务不受影响。

### 4.3 mask_matrix_nms：不打架的去重——矩阵打分衰减

#### 4.3.1 概念说明

topk 选出的 600 个候选里，同一把椅子可能被多个查询同时命中，产生高度重叠的同类别掩码。传统 NMS 逐对计算 IoU、直接删除低分框；**矩阵 NMS** 换成一次性算出所有候选两两之间的 IoU 矩阵，然后给「与更高分同类别候选重叠」的候选**衰减分数**而不是直接删除——分数被压低的候选自然会被下游的 `score_thr`/`npoint_thr` 淘汰。这种软抑制对掩码任务更平滑，且能用矩阵乘法一次完成，没有逐对循环。

本项目版本拷贝自 mmdet 的 `matrix_nms.py`，唯一改动是把 `masks` 的输入从分割图形状改成二维点掩码 `(n, m)`（见文件头注释 [uniseg3d/mask_matrix_nms.py:L1-L2](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/mask_matrix_nms.py#L1-L2)）。还有一个重要细节：传入的是 **sigmoid 软掩码**（L139-140 传 `mask_pred_sigmoid`），所以下面公式里的「交集」「面积」都是软数值——本质上是一个软 IoU 矩阵。

#### 4.3.2 核心流程

```text
输入: masks (n, m) sigmoid 软掩码, labels (n,), scores (n,)
1. 按分数降序排序
2. inter = M @ M^T                      -> 两两软交集
   iou  = inter / (area_i + area_j - inter)，取上三角（i<j，i 分数更高）
   label_eq = (标签相同) 取上三角       -> 只惩罚同类重叠
3. IoU 补偿: comp_i = max_j iou[i,j]（i 与更高分同类候选的最大重叠）
4. 衰减系数（对每个候选 j，在所有同类更高分的 i 中取最小）:
   linear:   d_j = min_i (1 - iou[i,j]) / (1 - comp_i)
   gaussian: d_j = min_i exp(-σ·iou[i,j]²) / exp(-σ·comp_i²)
5. scores *= d_j，按新分数降序返回（filter_thr/nms_pre/max_num 均未启用）
```

linear 核（本项目所用）下衰减系数的直觉：分子 \(1-\mathrm{IoU}(i,j)\) 衡量候选 `j` 与高分候选 `i` 的重叠程度（重叠越大分子越小、惩罚越重）；分母 \(1-\mathrm{comp}_i\) 是「IoU 补偿」——如果 `i` 自己也是别人的重复（`comp_i` 大），它对 `j` 的指控就不够权威，惩罚被打折，避免连锁误杀。

\[ d_j=\min_{i:\,c_i=c_j,\,i<j}\frac{1-\mathrm{IoU}(i,j)}{1-\mathrm{comp}_i},\qquad \mathrm{comp}_i=\max_{k:\,c_k=c_i,\,k<i}\mathrm{IoU}(i,k) \]

gaussian 核则是把 \(1-x\) 换成 \(e^{-\sigma x^2}\)（σ=2.0），曲线更陡。

#### 4.3.3 源码精读

- [uniseg3d/mask_matrix_nms.py:L44-L63](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/mask_matrix_nms.py#L44-L63)：空输入保护；`mask_area = masks.sum(1)` 软面积；按分数降序排序（`nms_pre=-1` 不截断）。
- [uniseg3d/mask_matrix_nms.py:L65-L78](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/mask_matrix_nms.py#L65-L78)：核心三矩阵。`torch.mm(flatten_masks, flatten_masks.transpose(1, 0))` 一条矩阵乘得两两软交集；并集 = \(area_i+area_j-inter\)，得到 IoU 矩阵后 `.triu(diagonal=1)` 只保留严格上三角（行下标小 = 分数高）；`label_matrix` 同样取上三角，标签不同的候选对被排除在惩罚之外。
- [uniseg3d/mask_matrix_nms.py:L80-L95](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/mask_matrix_nms.py#L80-L95)：IoU 补偿与两种核的衰减系数。`compensate_iou` 是 `(iou_matrix * label_matrix).max(0)`——每个候选与更高分同类候选的最大重叠；随后广播成矩阵并转置，使第 `i` 行持有 `comp_i`。linear 与 gaussian 两个分支按 4.3.2 的公式取逐列最小。
- [uniseg3d/mask_matrix_nms.py:L99-L122](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/mask_matrix_nms.py#L99-L122)：`scores = scores * decay_coefficient` 完成衰减；`filter_thr=-1`（模型调用只传了 kernel，见 [uniseg3d/uniseg3d.py:L137-L140](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L137-L140)）与 `max_num=-1` 都不触发，所以**函数只衰减分数、不删候选**——真正的删除交给调用方的 `score_thr` 与 `npoint_thr`。最后按新分数降序重排并返回 `(scores, labels, masks, keep_inds)`。

#### 4.3.4 代码实践

**实践目标**：构造三个候选（两个同类重叠、一个异类重叠），观察矩阵 NMS 只惩罚同类重叠且只压低分者的分数。

**操作步骤**（示例代码，只需 torch）：

```python
# 示例代码：直接调用项目内的 mask_matrix_nms
import torch
from uniseg3d.mask_matrix_nms import mask_matrix_nms   # 需在仓库根目录、已装 torch

m = 100
base = torch.zeros(m); base[:40] = 0.9                  # 40 点的软掩码
dup  = base.clone(); dup[30:50] = 0.9                   # 与 base 重叠 10 点、同类
diff = base.clone(); diff = diff * 0.9                  # 与 base 完全重叠但异类

masks  = torch.stack([base, dup, diff])                 # (3, 100)
labels = torch.tensor([5, 5, 7])                        # 前两个同类，第三个异类
scores = torch.tensor([0.9, 0.8, 0.7])

new_scores, new_labels, new_masks, keep = mask_matrix_nms(
    masks, labels, scores, kernel='linear')
print('原分数:', scores.tolist())
print('新分数:', [round(s, 4) for s in new_scores.tolist()])
print('标签  :', new_labels.tolist())
```

**需要观察的现象**：分数最高的 base 候选分数基本不变；与它同类重叠的 dup 被明显压分；与它完全重叠但异类的 diff 几乎不受影响（分数变化仅来自排序）。

**预期结果**：`new_scores` 中 dup 对应的值显著小于 0.8，diff 接近 0.7；返回顺序按衰减后分数降序。手工可验证：\(\mathrm{IoU}(base,dup)=10/70\approx0.143\)，\(d_{dup}=(1-0.143)/(1-0)=0.857\)（base 无更高分同类候选，comp=0）。待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `iou_matrix` 和 `label_matrix` 都要 `.triu(diagonal=1)`？

**答案**：候选已按分数降序排序，`(i,j)` 与 `(j,i)` 是对称重复信息；只保留上三角（`i<j` 即 `i` 分数更高）保证每个重叠对只被计算一次，且语义固定为「低分的 `j` 被高分的 `i` 惩罚」，对角线（自身）也要排除。

**练习 2**：把 `matrix_nms_kernel` 从 `linear` 改成 `gaussian`，对候选保留数量有什么影响？

**答案**：两种核都不直接删候选（`filter_thr` 未启用），只改变衰减幅度。gaussian 核 \(e^{-\sigma x^2}\) 在重叠不大时衰减远比 linear 温和（如 IoU=0.2 时 \(e^{-2\times0.04}\approx0.92\) vs linear 的 0.8），对轻中度重叠的候选分数压缩更少；随后被 `pan_score_thr=0.5`/`npoint_thr` 淘汰的候选也会更少，最终实例数一般会变多。待本地验证。

**练习 3**：矩阵 NMS 与逐对 NMS 相比，复杂度差在哪里？

**答案**：矩阵 NMS 的开销集中在一次 \((n\times m)(m\times n)\) 的矩阵乘与 \((n\times n)\) 逐元素运算，复杂度 \(O(n^2 m)\) 且全在 GPU 上批量完成，没有 Python 循环；逐对 NMS 需要 \(O(n^2)\) 次 IoU 计算且通常伴随「删除—重排序」的串行流程。在 n=600、m 为数万点时批量矩阵运算明显更友好。

### 4.4 语义与全景预测：argmax 映射与 things/stuff 合并

#### 4.4.1 概念说明

**语义预测**出奇地短：`sem_preds` 形状为 `(n_sps, 21)`（超点即查询，查询轴就是超点轴；21 = 20 类 + 1 unlabeled），对每个超点取 argmax 得到类别，再用 `superpoints` 索引把类别广播到该超点的所有原始点。语义任务**没有任何阈值可调**——它是纯粹的逐超点分类。

**全景预测**则是把「stuff 语义图」和「things 实例图」缝合成一张既含类别又含实例的图。全景的 id 体系刻意与评估标注对齐（u3-l3 讲过的 stuff=0/1、things 从 2 起）：

| 点的归属 | 全景语义图 `sem_map` | 全景实例图 `inst_map` |
| --- | --- | --- |
| stuff 区域 | 0=wall / 1=floor | 0 / 1（沿用语义 id） |
| things 实例区域 | 类别 id + 2（范围 2..20） | 实例序号 + 2（范围 2..2+n-1） |
| 被删除的小实例区域 | 回退为 stuff id | 回退为 stuff id |

#### 4.4.2 核心流程

```text
predict_by_feat_panoptic:
1. sem_map = predict_by_feat_semantic(out, superpoints, classes=[0,1])
      # 只在 wall/floor 两个 stuff 类中 argmax -> 每点 0 或 1
2. mask_pred, labels, scores = predict_by_feat_instance(
      out, superpoints, pan_score_thr=0.5, is_test_iou=True)
      # 复用 4.2 的整条过滤链，但阈值更严且分数乘了 preds_iou
3. 若无实例: 直接返回 (sem_map, sem_map)               # 纯 stuff 场景
4. 按分数升序排序；给第 k 个实例编号 k+2（跳过 stuff 的 0/1）
5. insts = 实例编号 × 各自掩码；逐点取 max
      # 重叠点归「编号最大」= 「分数最高」的实例；idxs 记录胜者
6. 胜者语义 = labels[idxs] + 2
7. 点数 <= npoint_thr 的小实例整体清零（其点回退 stuff 图）
8. sem_map[被实例占用] = 0；inst_map = sem_map 副本 + 实例编号
   sem_map += 胜者语义 id
```

关键技巧在第 5 步：**用「编号大小」编码「分数高低」**。先按分数升序排序，再按顺序赋 2,3,4,... 的实例编号，于是「逐点取 max 编号」等价于「逐点取分数最高的覆盖实例」，一次 `max(axis=0)` 同时完成重叠消解与胜者记录（返回的 `idxs` 就是每点的胜者下标）。

#### 4.4.3 源码精读

- [uniseg3d/uniseg3d.py:L160-L175](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L160-L175)：语义预测全部逻辑。`classes` 缺省取前 20 类；全景路径只传 `[0, 1]`（wall/floor）。注意顺序：**先 argmax 再做 `superpoints` 索引**——类别决策在超点粒度完成，点级只是广播，因此超点纯净度（u1-l4 的 segmentator 过分割质量）直接决定语义分割上限。
- [uniseg3d/uniseg3d.py:L193-L198](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L193-L198)：stuff 图来自 `stuff_classes=[0,1]` 的受限 argmax；things 图来自 4.2 过滤链的**第二次调用**（`pan_score_thr=0.5`、`is_test_iou=self.pano_test_iou=True`）；无实例时语义图同时充当两张输出图。
- [uniseg3d/uniseg3d.py:L200-L211](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L200-L211)：升序排序 + 编号偏移 + 逐点 max 的重叠消解。`inst_idxs` 从 `n_stuff_classes=2` 开始；`things_sem_mask = labels[idxs] + n_stuff_classes` 把胜者类别平移到 things 区间。
- [uniseg3d/uniseg3d.py:L213-L224](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L213-L224)：小实例清除（复用 `npoint_thr`，注意这里是 `<=` 而 4.2 的工序⑦是 `>`，两者边界一致）；随后三步缝合：实例占用点从 sem_map 清零 stuff 标签、`inst_map = sem_map.clone() + things_inst_mask`、`sem_map += things_sem_mask`。最终 `sem_map` 与 `inst_map` 分别作为 `pts_semantic_mask[1]`、`pts_instance_mask[1]` 进入全景评估。

#### 4.4.4 代码实践

**实践目标**：用合成张量验证「argmax → superpoints 广播」与「编号 max 重叠消解」两个核心机制。

**操作步骤**（示例代码）：

```python
# 示例代码
import torch

# --- 机制 1: 语义 argmax + 花式索引广播 ---
n_sps, n_raw = 5, 12
sem_preds = torch.randn(n_sps, 21)
superpoints = torch.randint(0, n_sps, (n_raw,))
sem_map = sem_preds.argmax(dim=1)[superpoints]          # (12,)
# 每个点的类别 == 它所属超点的类别
assert (sem_map == sem_preds.argmax(dim=1)[superpoints]).all()
print('超点类别:', sem_preds.argmax(dim=1).tolist())
print('点级类别:', sem_map.tolist())

# --- 机制 2: 编号 max = 分数最高者胜出 ---
masks = torch.tensor([[True, True, False],   # 实例 2 覆盖点 0,1（分数低）
                      [False, True, True]])  # 实例 3 覆盖点 1,2（分数高）
inst_idxs = torch.arange(2, 4).view(-1, 1)
insts = inst_idxs * masks
things_inst_mask, idxs = insts.max(axis=0)
print('逐点胜者编号:', things_inst_mask.tolist())       # 点1 归实例 3
```

**需要观察的现象**：机制 1 中同一超点的所有点类别相同；机制 2 中点 1 被两个实例覆盖，最终归属编号 3（分数较高者）。

**预期结果**：`things_inst_mask == [2, 3, 3]`，`idxs == [0, 1, 1]`，与 L209-L211 的行为一致。

#### 4.4.5 小练习与答案

**练习 1**：全景路径的实例为什么用 `pan_score_thr=0.5` 而不是实例任务的 0.0？为什么全景还要乘 `preds_iou`？

**答案**：全景要求每个点有唯一归属，一个错误实例会连带污染其覆盖点的语义标签，宁可提前砍掉弱候选；`preds_iou` 是校准过的掩码质量估计（u5-l4），乘进分数后让「看起来类别对但掩码差」的候选更难越过 0.5 的门槛，从而提高 PQ 中 RQ（识别质量）部分。配置用 `inst_test_iou=False, pano_test_iou=True` 双开关分别控制（[configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:L29-L30](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L29-L30)）。

**练习 2**：`stuff_classes=[0,1]` 在全景预测里出现了几次、分别起什么作用？

**答案**：两次语义不同：L193-194 作为 `classes` 参数限制 stuff 图的 argmax 范围（只在 wall/floor 中二选一）；L204 的 `n_stuff_classes = len(self.test_cfg.stuff_classes)` 作为实例编号与 things 语义 id 的**偏移量**（编号从 2 起），把 stuff 与 things 两个 id 区间拼在同一张图里。

**练习 3**：如果一个实例掩码只有 105 个点，`npoint_thr=100` 时它在实例任务与全景任务中的命运分别是什么？

**答案**：实例任务中它保留——工序⑦的保留条件是严格大于（`mask_pointnum > npoint_thr`，105 > 100 成立）；全景任务中也保留——L215 的删除条件是小于等于（`pts <= npoint_thr`，105 ≤ 100 不成立）。若恰好 100 个点，两边都会删除：`100 > 100` 为假（实例删）、`100 <= 100` 为真（全景删）。即两个任务的边界完全一致（保留 ⇔ 点数 > 100），不会出现「实例任务保留而全景删除」的错位。

### 4.5 点/文本提示结果的直通输出与置信度

#### 4.5.1 概念说明

交互式分割（点提示）与指代分割（文本提示）的推理结果不走 topk、不走 NMS、不走分数/点数过滤——因为提示查询与目标 GT 天然一一对应（u5-l5 讲过的恒等匹配在推理端的镜像）：第 `k` 条点提示的掩码就是 `masks[k]`，不存在「多候选竞争同一目标」的问题。所以这两个分支的后处理只剩三步：sigmoid → 超点映射 → `sp_score_thr` 二值化，故称「直通输出」。

同时，提示分支把 **GT 掩码也映射到点级**一并返回（`*_object_gt_masks`），评估器直接用 `get_iou` 算两者 IoU（[uniseg3d/unified_metric.py:L156-L174](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_metric.py#L156-L174)），交互式任务还额外拿到 `point_prob` 作为 AP 排序的置信度。

#### 4.5.2 核心流程

```text
point_prompt_predict_by_feat(out, superpoints, point_prompt_instances):
1. 若 out['masks'][0] 为 None（该场景无点提示）-> 空 Tensor 直接收场
2. obj_res = sigmoid(masks)[:, superpoints] > sp_score_thr    # 预测掩码, 点级
3. obj_gt  = point_prompt_instances[0].sp_masks[:, superpoints] # GT 掩码, 点级
4. 返回 PointData(point_prompt_object_pred_masks=..., point_prompt_object_gt_masks=...)

predict() 末尾（对每条点提示）:
   prob_      = softmax(cls_preds)[:, :-1]                     # (n_prompts, 18)
   point_prob = gather(prob_, 1, GT 标签列)                     # 每条提示在其 GT 类别上的概率
```

文本提示分支 `text_prompt_predict_by_feat` 与点提示分支逐行同构，只是键名换成 `text_prompt_*` 且不产出置信度——指代任务评估用 mIoU 与 Precision（u6-l2 展开），不需要排序置信度。

#### 4.5.3 源码精读

- [uniseg3d/uniseg3d.py:L20-L31](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L20-L31)：点提示结果组装。`obj_gt` 用 `point_prompt_instances[0]` 硬编码取第一个样本（再次体现 batch_size=1 约定）；预测与 GT 都做成 `(n_prompts, n_raw_points)` 的 numpy 数组放进 `PointData`。
- [uniseg3d/uniseg3d.py:L33-L39](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L33-L39)：直通三步曲——sigmoid、`[:, superpoints]` 花式索引、`> sp_score_thr`。注意它读的是**同一个** `self.test_cfg.sp_score_thr`（文本分支 [L54-L60](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L54-L60) 同理），所以调这个阈值会同时改变四个掩码类任务的输出。
- [uniseg3d/uniseg3d.py:L41-L52](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L41-L52)：文本提示结果组装，与点提示逐行同构。
- [uniseg3d/uniseg3d.py:L877-L888](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L877-L888)：回写段。`point_prob` 的计算在 L882-L884：softmax 后 `torch.gather` 抽取每条提示在**GT 类别**列的概率。这个概率随后被评估器当作置信度用于 AP 的 PR 排序（[uniseg3d/unified_metric.py:L232-L239](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_metric.py#L232-L239)）。

#### 4.5.4 代码实践

**实践目标**：用合成数据验证「恒等对应 + gather 取置信度」的行为。

**操作步骤**（示例代码）：

```python
# 示例代码
import torch
import torch.nn.functional as F

n_prompts, n_sps, n_raw = 3, 6, 20
masks = torch.randn(n_prompts, n_sps)                    # 提示分支的掩码 logits
superpoints = torch.randint(0, n_sps, (n_raw,))
sp_score_thr = 0.4
pred = (masks.sigmoid()[:, superpoints] > sp_score_thr)  # (3, 20) 直通输出

gt_sp = torch.rand(n_prompts, n_sps) > 0.5               # GT 超点掩码
gt = gt_sp[:, superpoints]

cls_preds = torch.randn(n_prompts, 19)
labels_3d = torch.tensor([3, 3, 7])                      # 每条提示的 GT 类别
prob = F.softmax(cls_preds, dim=-1)[:, :-1]
point_prob = torch.gather(prob, 1, labels_3d.unsqueeze(-1)).squeeze(-1)

# 恒等对应检验: 第 k 行预测与第 k 行 GT 描述同一目标
for k in range(n_prompts):
    inter = (pred[k] & gt[k]).sum()
    union = (pred[k] | gt[k]).sum()
    print(f'提示{k}: IoU={inter / max(union, 1):.3f}, 置信度={point_prob[k]:.3f}')
```

**需要观察的现象**：预测与 GT 逐行成对——第 `k` 行 IoU 就是第 `k` 条提示的交互式分割质量；`point_prob[k]` 等于第 `k` 行 softmax 在 `labels_3d[k]` 列的取值。

**预期结果**：`point_prob` 与 `prob[range(3), labels_3d]` 完全相等；若把 `sp_score_thr` 调高，`pred` 逐行收缩、IoU 下降——这正是综合实践中要观察的现象的微缩版。

#### 4.5.5 小练习与答案

**练习 1**：为什么提示分支不需要 NMS，而普通实例分支需要？

**答案**：实例分支的候选来自「查询 × 类别」的展开（4.2 工序①），同一目标天然被多个候选命中，必须去重；提示分支每条查询由一条提示（一个点或一句描述）显式构造，目标唯一、行序即对应关系，不存在候选竞争。

**练习 2**：`point_prob` 为什么取「GT 类别列」的概率而不是最大概率？

**答案**：交互式分割的评估中类别不是预测对象（提示已经隐含目标），置信度只用于 AP 曲线中决定命中/误报的排序次序。取 GT 类别列的概率衡量「模型认为自己点的这个东西是它真正类别的把握」，与 IoU 命中与否共同构成 PR 对；若取最大概率，置信度会系统性偏向模型自认为熟悉的类别，扭曲排序。

**练习 3**：一个验证场景没有任何 GT 实例（纯 stuff 房间），提示分支会输出什么？

**答案**：`TextPromptTest`/`PointPromptTest` 不产生提示，拆分函数把 `text_prompt_outputs['masks'][i]` 留成 `None`，于是 [uniseg3d/uniseg3d.py:L21-L23](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L21-L23)（文本分支 L42-L44 同理）走 `== None` 分支，预测与 GT 都置为空 Tensor；评估端对应场景追加空 IoU 数组（[uniseg3d/unified_metric.py:L160-L166](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_metric.py#L160-L166)），不影响其余任务的评估。

## 5. 综合实践

**任务**：在验证集（或其小子集）上做一次推理阈值消融，亲手验证 `sp_score_thr` 与 `topk_insts` 的作用面，并逐行解释指标变化。

### 步骤 1：准备

按 README「Inference」一节准备 CLIP 权重与 [model_best_2.pth](https://github.com/dk-liang/UniSeg3D/releases/download/v1.0/model_best_2.pth)，放入 `work_dirs/pretrained/convnext_large_d_320` 与 `work_dirs/ckpts`。

### 步骤 2：统计预测实例数

在 [uniseg3d/uniseg3d.py:L158](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L158)（`predict_by_feat_instance` 的 return 前）临时加一行（示例代码，练习用）：

```python
print(f'[n_inst] thr={score_threshold:.2f} keep={mask_pred.shape[0]}')
```

每次推理会打印两行（实例任务 thr=0.00 与全景任务 thr=0.50 各一次），可对照确认两个任务复用同一条链。

### 步骤 3：跑三组推理

```bash
# 基线
CUDA_VISIBLE_DEVICES=0 ./tools/dist_test.sh \
  configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py \
  work_dirs/ckpts/model_best_2.pth 1

# 变体 A: sp_score_thr 0.4 -> 0.5（影响 IS/PS/交互式/指代 四个掩码任务）
CUDA_VISIBLE_DEVICES=0 ./tools/dist_test.sh \
  configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py \
  work_dirs/ckpts/model_best_2.pth 1 \
  --cfg-options model.test_cfg.sp_score_thr=0.5

# 变体 B: topk_insts 600 -> 100（只影响 IS/PS）
CUDA_VISIBLE_DEVICES=0 ./tools/dist_test.sh \
  configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py \
  work_dirs/ckpts/model_best_2.pth 1 \
  --cfg-options model.test_cfg.topk_insts=100
```

`--cfg-options` 的点号路径覆盖由 test.py 回写进配置（u1-l2/u2-l2 已讲），`${@:4}` 负责透传。

### 步骤 4：记录与解释

把三组结果填入下表（具体数值**待本地验证**）：

| 实验 | 保留实例数（均值） | mAP | mIoU（语义） | PQ | Point Prompt AP | Text Prompt mIoU |
| --- | --- | --- | --- | --- | --- | --- |
| 基线 | — | — | — | — | — | — |
| sp_score_thr=0.5 | — | — | — | — | — | — |
| topk_insts=100 | — | — | — | — | — | — |

解释框架（对照源码逐行说明）：

- **变体 A**：[L142-L143](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L142-L143) 的二值化阈值提高 → 每个掩码收缩、点数减少 → 更多掩码死在 [L151-L156](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L151-L156) 的 `npoint_thr` → 实例数下降；掩码更紧可能提升高 IoU 阈值下的 AP 但损失召回。同时 [L37/L58](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L33-L39) 的提示分支读同一个键，Point Prompt AP 与 Text Prompt mIoU 应同步变化。**语义 mIoU 不变**。
- **变体 B**：[L123-L125](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L123-L125) 的候选池从 600 缩到 100 → 长尾低分候选直接出局 → 实例数上限被压低，mAP 在物体多的场景下降更明显；**语义、交互式、指代三列完全不变**（它们不经过 topk）。

若两条解释中有任何一条与你的观测矛盾，回到 4.2 的七道工序表逐工序定位——这是训练阈值调试时最常用的排查路径。

## 6. 本讲小结

- `predict()` 与 `loss()` 前半段共用，差异集中在：无弹性坐标、全量查询（不采样）、`mode='val'` 拆分、出口从损失换成四路结果回写；一次解码器前向同时供给 IS/SS/PS/交互式/指代五个任务，开放词汇的类无关掩码也在同一份输出上（注释块）。
- 实例预测是七道工序的过滤链：softmax 展开去背景列 → topk 600 候选 → obj 归一化 → 矩阵 NMS → 超点映射二值化 → 分数过滤 → 点数过滤；实例与全景**复用同一条链**，仅阈值（0.0 vs 0.5）与 `preds_iou` 开关不同。
- 超点级结果回到点级的唯一通道是花式索引 `masks[:, superpoints]`：列选择 + 广播，一行完成；语义任务先 argmax 再映射，类别决策粒度是超点。
- 矩阵 NMS 用一次矩阵乘得到软 IoU 矩阵，只对**同类别、与更高分候选重叠**的组合按 linear/gaussian 核衰减分数，不直接删候选；删除由下游 `score_thr` 与 `npoint_thr` 完成。
- 全景合并用「升序排序 + 编号从 2 起 + 逐点取 max」把重叠消解变成一次 max 操作，stuff 用语义 id 0/1、things 用 `类别+2`/`实例号+2`，与评估标注 id 体系严格对齐。
- 点/文本提示结果直通输出：sigmoid → 超点映射 → `sp_score_thr`，无 topk/NMS；`point_prob` 用 gather 取 GT 类别列概率作为 AP 排序置信度。推理代码多处硬编码下标 0，隐含每卡测试 batch_size=1 的约定。

## 7. 下一步学习建议

本讲把「预测怎么来」讲完了，自然的下一问是「预测怎么被打分」：

- **u6-l2（六任务统一评估）**：本讲产出的 `pts_semantic_mask[0]/[1]`、`instance_labels/scores`、提示 GT/预测对、`point_prob` 如何分别进入 `seg_eval`/`instance_seg_eval`/`panoptic_seg_eval` 与自定义的 AP@25/50、Precision 计算，`cal_ap` 的数值积分又如何实现。
- **u6-l3（开放词汇分割实战）**：本讲只点到 [L829-L873](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L829-L873) 的注释块，下一讲实操取消注释、调 `iou_threshold`/`mask_threshold`，并把输出对接 Open3DIS。
- 想加深阈值直觉的读者，可以把综合实践扩展成小网格搜索（`sp_score_thr ∈ {0.3,0.4,0.5}` × `topk_insts ∈ {100,600}`），画出各指标的敏感度曲线——你会直观看到「掩码类任务共享 `sp_score_thr`、实例候选类任务独占 `topk_insts`」这条本讲的核心结论。
