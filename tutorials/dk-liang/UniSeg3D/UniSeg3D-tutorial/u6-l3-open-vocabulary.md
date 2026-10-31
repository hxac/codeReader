# 开放词汇分割实战：类无关掩码 + Open3DIS

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清开放词汇（Open-Vocabulary, OV）分割任务「先生成类无关掩码、再用基础模型分类」的两阶段范式，以及它为什么必须外置到仓库之外完成。
2. 逐行读懂 `uniseg3d/uniseg3d.py` 中 L829-L873 的注释代码块：`mask_score × preds_iou` 的质量过滤、`[:, sp_pts_masks]` 的超点→原始点展开、`.pth` 文件的保存格式。
3. 掌握 `iou_threshold`（候选掩码筛选）与 `mask_threshold`（点级二值化）两个阈值的作用与调节方式。
4. 按 README 的 Open-Vocabulary Segmentation 章节把本仓库输出对接 Open3DIS 的掩码分类与评估步骤。

## 2. 前置知识

- **闭集 vs 开集**：UniSeg3D 的分类头（u4-l4 讲过的球面分类 `sphere_cls`）绑定在 ScanNet 20+1 类词表的原型上——训练时见过哪些类别名，推理就只能输出哪些类别名，这叫闭集（closed-vocabulary）。开放词汇分割要求模型能分割**训练词表之外**的类别（本仓库在 ScanNet200 上评估 OV，指标为 AP，见 README 模型表）。
- **两阶段范式**：既然 3D 模型自己的分类头放不下新词表，就把「分割」与「分类」拆开——第一阶段只输出**不带类别标签的物体掩码候选**（class-agnostic masks，也叫 proposal），第二阶段用词表无关的基础模型（CLIP 类图文对齐、2D 分割大模型的多视角特征等）给每个候选掩码打类别分。Open3DIS 就是第二阶段的现成工具箱。
- **掩码天然类无关**：回顾 u4-l3，解码器每个查询输出**一条**掩码 logits（形状为「查询数 × 超点数」），类别信息只存在于 `cls_preds` 分类分支里，掩码分支本身与类别无关。这是 OV 第一阶段可以「白拿」模型输出的根本原因。
- **IoU 预测分支**：回顾 u5-l4，`pred_iou_proj` 三层 MLP 从查询向量回归「预测掩码与真值的交并比」，推理时产出 `x['preds_iou']`，形状为 `(n_queries, 1)`。本讲的过滤逻辑要把它乘进掩码质量分。
- **超点映射**：回顾 u6-l1，模型掩码定义在超点上，`sp_pts_mask` 是逐点的超点编号张量；`mask_sigmoid[:, sp_pts_mask]` 这种花式索引可把超点级分数「广播」回每个原始点（点继承其所属超点的分数）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `uniseg3d/uniseg3d.py` | 核心文件。`predict()` 中 L829-L873 是被注释掉的类无关掩码生成与保存代码块；`get_pred_iou()`（L499-L518）提供 `preds_iou`；`predict_by_feat_instance()`（L93-L158）提供对照的实例推理过滤链 |
| `README.md` | L187-L199 的 Open-Vocabulary Segmentation 章节给出两步操作说明；L200-L254 模型表给出 OV AP 指标 |
| `configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py` | `pred_iou = True`（L13）是注释块生效的硬前提；L127-L133 的 `test_cfg` 含对照阈值 |
| `uniseg3d/unified_metric.py` | 对照用：五任务（PS/SS/IS/交互/指代）在仓库内评估，OV 是六任务中唯一不在其中评估的任务 |

## 4. 核心概念与源码讲解

### 4.1 开放词汇两阶段范式与代码块的插入位置

#### 4.1.1 概念说明

UniSeg3D 论文宣称统一六任务，但读者读完 u6-l1、u6-l2 后会发现一个「缺口」：`predict()` 一次前向回写了五任务结果，`PromptSupportedUnifiedSegMetric` 评估了五任务指标——开放词汇既没有专门的推理分支，也没有专门的评估分支。

原因在于范式不同：前五任务的类别空间都是训练词表的子集，模型可以「自己分类」；OV 要求在 ScanNet200（200 个细粒度类，远超训练的 20 类）上分割，模型的球面分类头（原型来自 `scannet_cls_embedding.pth`，见 u4-l4）根本覆盖不了这个词表。于是作者采取最小代价方案：**复用实例分支的掩码输出，跳过分类，把候选掩码落盘，交给外部的 Open3DIS 做分类与评估**。这就是为什么 OV 代码在仓库里只是一段「可选注释」——它不是一条新链路，而是实例掩码输出的一条旁路导出。

#### 4.1.2 核心流程

```text
dist_test.sh（batch_size=1 的验证/测试前向）
  └─ UniSeg3D.predict()
       ├─ 构造 batch_offsets / sp_pts_masks（L767-L785）
       ├─ collate → 稀疏张量 → extract_feat → 超点查询（L780-L789）
       ├─ 注入点/文本提示查询（L796-L799）
       ├─ decoder 前向（L817）
       ├─ 拆分提示查询：x 只剩「原始查询」输出（L819-L824）
       ├─ if self.pred_iou: x = get_pred_iou(x)   ← 给 x 补上 'preds_iou' 键（L826-L827）
       ├─ 【注释块 L829-L873】类无关掩码生成与保存   ← 本讲主角
       └─ predict_by_feat(x, sp_pts_masks)         ← 五任务常规推理，不受影响（L875）
```

三个关键前置条件：

1. `pred_iou=True`：注释块嵌在 `if self.pred_iou:` 分支**内部**，且读取 `x['preds_iou']`——该键由 `get_pred_iou()` 写入。主配置 L13 显式 `pred_iou = True`，默认满足。
2. `batch_size=1`：块内全部用 `[0]` 硬索引（`x['preds_iou'][0]`、`batch_data_samples[0]`），即只处理批内第一个场景。主配置的 `val_dataloader`/`test_dataloader` 未覆盖 `batch_size`（L258-L266 只改 dataset 字段），继承自 mmdet3d 基础数据集配置的默认值 1（待确认：以本地安装的 mmdet3d 版本为准，可在打印的 dataloader 配置里核对）。
3. 只做旁路导出：注释块不修改 `x`、不产出新的 `results_list`，五任务的常规输出完全不受影响。

#### 4.1.3 源码精读

代码块在 `predict()` 中的位置——先过 IoU 头，再进注释块，最后照常走五任务推理：

- [uniseg3d/uniseg3d.py:826-827](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L826-L827)：`if self.pred_iou: x = self.get_pred_iou(x)`。这一步把解码器逐层保存的 `contras_embeds` 过 `pred_iou_proj` 回归出每个查询的预测 IoU，写入 `x['preds_iou']`（详见 u5-l4）。
- [uniseg3d/uniseg3d.py:829-838](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L829-L838)：作者留下的使用说明注释——替换 `save_path`、按需调 `iou_threshold` 与 `mask_threshold`、取消整块注释。
- [uniseg3d/uniseg3d.py:875](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L875)：注释块结束后的 `results_list = self.predict_by_feat(x, sp_pts_masks)`。注意 `x` 未被注释块污染（块内只读不写），五任务推理照旧。

`get_pred_iou` 的输出形状（注释块的直接输入）：

- [uniseg3d/uniseg3d.py:499-518](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L499-L518)：逐样本把 `contras_embeds` 过 `pred_iou_proj`，`preds_iou[i]` 形状 `(n_queries_i, 1)`。注释块只取 `preds_iou_[0]`。

`pred_iou_proj` 的定义（u5-l4 已精读，此处仅定位）：

- [uniseg3d/uniseg3d.py:332-338](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L332-L338)：`pred_iou = True` 时构建 256→256→256→1 的三层 MLP。

配置侧的硬前提与对照阈值：

- [configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:13](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L13)：`pred_iou = True`，随后透传给模型（L31、L89）。
- [configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:127-133](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L127-L133)：`test_cfg` 里的 `topk_insts=600`、`inst_score_thr=0.0`、`pan_score_thr=0.5`、`npoint_thr=100`、`sp_score_thr=0.4`——实例/全景路径的过滤阈值，本讲 4.2 会与 OV 块对照。

OV 为什么不在 `unified_metric.py` 里评估（对照点）：

- [uniseg3d/unified_metric.py:205-218](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_metric.py#L205-L218)：`panoptic_seg_eval`（PQ）、`seg_eval`（mIoU）、`instance_seg_eval`（mAP）三路在仓库内完成；OV 的分类与 AP 评估依赖外部词表，交给 Open3DIS 的步骤 5。
- [README.md:185](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/README.md#L185)：官方明说「PS, SS, IS, Interactive, Referring 五任务一次推理内评估」——OV 被排除在这句话之外。

#### 4.1.4 代码实践

**实践目标**：不改任何代码，纯阅读确认注释块的三个前置条件与调用链位置。

**操作步骤**：

1. 在仓库根目录执行 `grep -n "your_save_folder_path_here" uniseg3d/uniseg3d.py`，确认它在 L843，且整段带 `#` 前缀。
2. 向上滚动确认三层嵌套：`def predict(self, ...)`（L746）→ `if self.pred_iou:`（L826）→ 注释块（L829-L873）。
3. 执行 `grep -n "pred_iou" configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py`，确认 L13 `pred_iou = True` 且模型构造处透传。
4. 查看 [configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:258-266](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L258-L266)，确认 val/test dataloader 未覆盖 `batch_size`。

**需要观察的现象**：注释块确实嵌在 `if self.pred_iou:` 内部、位于 `predict_by_feat` 之前；配置中 `pred_iou=True`。

**预期结果**：若把 `pred_iou` 改为 `False`，L826 分支不执行，`x['preds_iou']` 不存在，恢复注释后会在 `x['preds_iou'][0]` 处抛 `KeyError`——这就是「pred_iou=True 是硬前提」的代码依据。待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：OV 的第一阶段为什么可以完全不用 `cls_preds`？

**答案**：掩码分支每个查询只输出一条与类别无关的掩码 logits，「这是什么类别」的信息全部在 `cls_preds` 分类分支里。OV 恰恰要抛弃训练词表的分类，只保留「哪里有一个物体」的定位信息，所以类无关掩码只需 `masks` 与质量分，分类交给外部基础模型。

**练习 2**：六任务中哪五个在仓库内评估？OV 的指标从哪来？

**答案**：全景（PQ）、语义（mIoU）、实例（mAP）、交互式（AP）、指代（mIoU/precision）由 `PromptSupportedUnifiedSegMetric` 在仓库内评估（u6-l2）；OV 的分类标签与 AP 评估由 Open3DIS 的步骤 1/2 与步骤 5 完成，README 模型表报告的 OV AP 为 19.6 / 19.7（ScanNet200）。

**练习 3**：恢复注释后，训练过程中的周期性验证（ValLoop）会受影响吗？

**答案**：会。ValLoop 同样调用 `predict()`，注释块会在每次验证前向时都执行并写文件。因此恢复注释后跑训练会产生大量中间掩码文件；该块更适合只在离线推理（`dist_test.sh`）时启用。

### 4.2 mask_score × preds_iou 的质量过滤与掩码保存

#### 4.2.1 概念说明

一次前向的原始查询数等于场景超点数（数千条），全部存盘既浪费又充满低质量碎片。注释块用**两个信号的乘积**给每条查询的掩码打质量分：

- `mask_score`（掩码内部一致性）：掩码 logits 为正（即 sigmoid > 0.5）的那些超点上，sigmoid 分数的平均值。直觉：一个「自信」的掩码，其内部超点的分数应当普遍高。
- `preds_iou`（定位质量）：u5-l4 训练的 IoU 头预测的「这条掩码与真实物体的预期重叠度」。

二者相乘得到 `mask_iou_score`，再用 `iou_threshold` 截断——只保留「既自信又定位准」的候选掩码。存盘前还有第二个阈值 `mask_threshold`，负责把超点级 sigmoid 分数二值化成点级 0/1 掩码。

值得注意：`mask_score` 的公式与实例推理路径里的 `obj_normalization` **逐字符相同**（见 4.2.3），即 OV 块复用了实例分支已有的掩码打分技巧，只是用途从「修正分类分数」换成「分类前的候选过滤」。

#### 4.2.2 核心流程

设某场景有 \( Q \) 条原始查询、\( S \) 个超点、\( N \) 个原始点。掩码 logits \( z \in \mathbb{R}^{Q \times S} \)，\( \sigma \) 为 sigmoid，\( \hat{I}_q \) 为第 \( q \) 条查询的预测 IoU。

掩码质量分：

\[
\text{mask\_score}_q
= \frac{\sum_{s \in \mathcal{P}_q} \sigma(z_{q,s})}{|\mathcal{P}_q| + 10^{-6}},
\qquad
\mathcal{P}_q = \{\, s \mid z_{q,s} > 0 \,\}
\]

联合质量分与过滤：

\[
\text{mask\_iou\_score}_q = \text{mask\_score}_q \cdot \hat{I}_q,
\qquad
\text{保留集合} = \{\, q \mid \text{mask\_iou\_score}_q > \text{iou\_threshold} \,\}
\]

点级展开与二值化（\( g(n) \in \{0,\dots,S-1\} \) 为点 \( n \) 的超点编号，即 `sp_pts_mask`）：

\[
\text{ins}_{q,n} = \mathbb{1}\big[\, \sigma(z_{q,\,g(n)}) > \text{mask\_threshold} \,\big]
\]

流程伪代码：

```text
输入: preds_iou_ (Q,1), pred_masks_ (Q,S), sp_pts_mask (N,), 两个阈值
1. pred_masks_sigmoid_ = sigmoid(pred_masks_)            # (Q,S)
2. mask_score_   = 正logit超点上的sigmoid均值(每行)          # (Q,)
3. mask_iou_score_ = mask_score_ * preds_iou_.squeeze(-1) # (Q,)
4. idx = mask_iou_score_ > iou_threshold                   # (Q,) bool
5. ins  = (pred_masks_sigmoid_[idx][:, sp_pts_mask] > mask_threshold)  # (K,N) uint8
6. conf = mask_iou_score_[idx]                             # (K,)
7. torch.save({'ins': ins, 'conf': conf}, save_path/场景名.pth)
```

#### 4.2.3 源码精读

逐行精读注释代码块（保留 `#` 便于与原文件对照）：

- [uniseg3d/uniseg3d.py:840-848](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L840-L848)：配置区——`save_path`（L843，需替换）、`iou_threshold = 0.6`（L844，候选掩码过滤）、`mask_threshold = 0.4`（L845，点级二值化），`os.makedirs` 建目录。注意这两个阈值是**硬编码**的，不走 `test_cfg`。
- [uniseg3d/uniseg3d.py:851-853](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L851-L853)：`clone()` 取第一个样本（batch_size=1 假设）的预测 IoU `(Q,1)` 与掩码 logits `(Q,S)`，再对 logits 过 sigmoid 得到 `(Q,S)` 概率。注意此处**没有**做 topk、没有分类分数——全部查询参与打分。
- [uniseg3d/uniseg3d.py:856](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L856)：`mask_score_ = (pred_masks_sigmoid_ * (pred_masks_ > 0)).sum(1) / ((pred_masks_ > 0).sum(1) + 1e-6)`——布尔掩码 `(pred_masks_ > 0)` 与 sigmoid 逐元素相乘后按行求和，除以正 logit 超点数，即上面公式中的「正 logit 超点上的 sigmoid 均值」。
- [uniseg3d/uniseg3d.py:857](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L857)：`mask_iou_score_ = mask_score_ * preds_iou_.squeeze(-1)`——内部一致性 × 预测 IoU，得到每条查询的联合质量分 `(Q,)`。
- [uniseg3d/uniseg3d.py:860](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L860)：`idx = mask_iou_score_ > iou_threshold`——严格大于 0.6 才保留。
- [uniseg3d/uniseg3d.py:863-864](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L863-L864)：用 `batch_data_samples[0].lidar_path` 的 basename 把 `.bin` 替换成 `.pth` 作为输出文件名（`lidar_path` 由 mmdet3d 父类 `Pack3DDetInputs` 的 metainfo 键机制打包，本仓库 `Pack3DDetInputs_` 继承不改）。
- [uniseg3d/uniseg3d.py:867-870](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L867-L870)：保存 `dict(ins=..., conf=...)`。`ins = (pred_masks_sigmoid_[idx][:, sp_pts_masks] > mask_threshold).cpu().numpy().astype('uint8')`——先按 `idx` 选行得 `(K,S)`，再花式索引 `[:, sp_pts_mask]` 展开为 `(K,N)`（每个点取其超点的分数），与 0.4 比较、转 uint8；`conf = mask_iou_score_[idx]` 是 `(K,)` 的每掩码置信度，交给下游分类/评估使用。
- [uniseg3d/uniseg3d.py:872](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L872)：打印保存路径，方便确认。

与实例推理路径的对照（同一技巧的两种用途）：

- [uniseg3d/uniseg3d.py:132-135](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L132-L135)：`obj_normalization` 里的 `mask_scores` 公式与 L856 完全一致；区别是实例路径用它**乘分类分数修正排序**，OV 块用它**单独过滤候选**。
- [uniseg3d/uniseg3d.py:142-143](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L142-L143)：实例路径同样先 `[:, superpoints]` 展开再 `> sp_score_thr`（配置为 0.4）二值化——与 OV 块 L868 的 `mask_threshold=0.4` 同值同型，说明 OV 的点级二值化阈值就是沿用了实例路径的超点分数阈值。
- [uniseg3d/uniseg3d.py:116-118](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L116-L118)：实例/全景路径中 `preds_iou` 由 `inst_test_iou`/`pano_test_iou` 开关决定是否乘进分数（u5-l4、u6-l1）；OV 块则无开关，恢复注释即固定生效。

#### 4.2.4 代码实践

**实践目标**：不跑模型、不用 GPU，用合成张量逐行复现 L851-L869 的过滤与保存逻辑，验证输出形状与语义。

**操作步骤**：

1. 写一个独立脚本 `ov_block_sim.py`（示例代码，放在仓库外或临时目录，勿改源码）：

   ```python
   # 示例代码：复现 uniseg3d.py L851-L869 的类无关掩码过滤逻辑
   import torch

   torch.manual_seed(0)
   Q, S, N = 100, 50, 800                     # 查询数/超点数/原始点数
   iou_threshold, mask_threshold = 0.6, 0.4

   preds_iou_ = torch.rand(Q, 1)              # 模拟预测 IoU ∈ (0,1)
   pred_masks_ = torch.randn(Q, S)            # 模拟掩码 logits
   sp_pts_mask = torch.randint(0, S, (N,))    # 模拟逐点超点编号

   pred_masks_sigmoid_ = pred_masks_.sigmoid()
   mask_score_ = (pred_masks_sigmoid_ * (pred_masks_ > 0)).sum(1) \
                 / ((pred_masks_ > 0).sum(1) + 1e-6)
   mask_iou_score_ = mask_score_ * preds_iou_.squeeze(-1)
   idx = mask_iou_score_ > iou_threshold

   ins  = (pred_masks_sigmoid_[idx][:, sp_pts_mask] > mask_threshold) \
          .numpy().astype('uint8')
   conf = mask_iou_score_[idx].numpy()
   print('保留掩码数 K =', idx.sum().item(), '/', Q)
   print('ins:', ins.shape, ins.dtype, '取值集合:', set(ins.flatten().tolist()))
   print('conf:', conf.shape, '范围: %.3f ~ %.3f' % (conf.min(), conf.max()))
   ```

2. 运行 `python ov_block_sim.py`。
3. 把 `iou_threshold` 依次改为 0.3 / 0.6 / 0.8，对比 `idx.sum()`。
4. 把 `mask_threshold` 改为 0.5，观察 `ins` 每行 1 的数量变化。

**需要观察的现象**：`ins` 形状 `(K, 800)`、dtype `uint8` 且取值只有 {0,1}；`conf` 形状 `(K,)` 且全部大于 0.6；`iou_threshold` 越高 K 越小；`mask_threshold` 越高每个掩码覆盖的点越少。

**预期结果**：K 远小于 Q（0.6 的阈值较严）；同一行内 1 的位置呈「超点块状」分布（同一超点的所有点取值相同）——这正是 `[:, sp_pts_mask]` 花式索引的效果，也是「掩码定义在超点上」的直接证据。本实践已可用合成数据本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`mask_score` 为什么要以「logit > 0」而不是全部超点求均值？

**答案**：logit > 0 等价于 sigmoid > 0.5，即模型已经倾向划入掩码的超点。只在这部分超点上取均值，衡量的是「掩码核心区域的自信程度」；若对所有超点求均值，掩码外大量接近 0 的分数会把分数拉低，区分度变差。这与实例路径 `obj_normalization` 的设计动机一致。

**练习 2**：`pred_masks_sigmoid_[idx][:, sp_pts_masks]` 这一步花式索引做了什么？为什么它能把 `(K,S)` 变成 `(K,N)`？

**答案**：`sp_pts_masks` 是长度 N 的逐点超点编号张量，用作列索引。花式索引 `[:, sp_pts_masks]` 让第 n 列取「第 g(n) 个超点」的分数，即每个点继承其超点的分数，形状从 `(K,S)` 变 `(K,N)`。这依赖 u3 系列建立的事实：超点是点的划分，一个点恰属一个超点。

**练习 3**：`iou_threshold=0.6` 与 `mask_threshold=0.4` 分别过滤什么？把前者调低会发生什么？

**答案**：`iou_threshold` 过滤**查询（候选掩码）**——决定哪些掩码有资格进入第二阶段分类；`mask_threshold` 过滤**点**——决定每个保留掩码覆盖哪些原始点。把 `iou_threshold` 调低会保留更多低质量候选（召回升高、精度下降、下游分类开销增大），调高则相反；README 报告的 OV AP 19.6/19.7 对应默认 0.4/0.6 组合。

### 4.3 Open3DIS 对接步骤与输出格式

#### 4.3.1 概念说明

Open3DIS（VinAIResearch 开源）是 3D 实例分割/开放词汇工具箱，其掩码分类阶段的核心思路是「3D 候选掩码 × 2D 多视角基础模型特征」：对每个 3D 掩码候选，把其覆盖的 3D 点投影回多视角 2D 图像，用 2D 大模型（SAM 类分割、CLIP 类图文对齐）提取特征并给出类别分布，从而实现与词表无关的分类。本仓库的对接方式是「各取所长」：UniSeg3D 贡献高质量 3D 类无关候选（`ins` + `conf`），Open3DIS 贡献词表无关的分类与评估。

需要强调：README 引用的是 Open3DIS 的 RUN 文档，具体命令与输入文件格式**以 Open3DIS 仓库为准**，本讲不编造其内部接口，只把本仓库侧的输出格式与对接动线讲清。

#### 4.3.2 核心流程

README 给出的两步动线：

```text
Step 1  类无关掩码生成（本仓库）
  ├─ 取消 uniseg3d.py L840-L872 注释
  ├─ 把 save_path 改为目标输出目录
  └─ 用 dist_test.sh 在 ScanNet 上推理 → 每场景一个 .pth

Step 2  分类与评估（Open3DIS，外部仓库）
  ├─ 按其 RUN.md 的步骤 1) & 2)：对候选掩码做掩码分类 → 得到类别标签
  └─ 按其步骤 5)：指标评估 → 得到 OV AP（README 报告 19.6 / 19.7）
```

本仓库侧每个 `.pth` 文件的内容契约：

| 键 | 形状 | dtype | 语义 |
| --- | --- | --- | --- |
| `ins` | `(K, N)` | uint8（取值 0/1） | K 个类无关掩码在 N 个原始点上的二值掩码；行序与 `conf` 对齐 |
| `conf` | `(K,)` | float32 | 每个掩码的 `mask_score × preds_iou` 联合质量分，全部 > `iou_threshold` |

文件名由 `lidar_path` 的 basename 把 `bin` 替换为 `pth` 得到（如 `scene0000_00.bin` → `scene0000_00.pth`），因此可与原场景一一对应。

#### 4.3.3 源码精读

README 的 Open-Vocabulary 章节（唯一官方对接说明）：

- [README.md:187-191](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/README.md#L187-L191)：Step 1「Class-agnostic Mask Generation」——取消 `uniseg3d/uniseg3d.py` L840-L872 的注释并把 `save_path` 改为你的输出目录。README 的链接直接锚定到 [L840](https://github.com/dk-liang/UniSeg3D/blob/main/uniseg3d/uniseg3d.py#L840)-[L872](https://github.com/dk-liang/UniSeg3D/blob/main/uniseg3d/uniseg3d.py#L872)，与本讲 4.2 精读的行号一致。
- [README.md:193-198](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/README.md#L193-L198)：Step 2「Classification Steps」——用 Open3DIS 的 [RUN 文档](https://github.com/VinAIResearch/Open3DIS/blob/main/docs/RUN.md)给每个类无关掩码分配类别标签；原文说明其流程含多个步骤，「本工作只使用 1)、2)、5)」：步骤 1/2 做掩码分类，步骤 5 做指标评估。
- [README.md:217](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/README.md#L217) 与 [README.md:239](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/README.md#L239)：模型表的 OV 列（指标 AP、数据集 ScanNet200）——UniSeg3D 19.6、UniSeg3D\* 19.7（后者对应 `model_best_2.pth`，见 [README.md:251](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/README.md#L251)）。

推理入口（与 u6-l1 相同，只是多了落盘副作用）：

- [README.md:180-184](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/README.md#L180-L184)：`CUDA_VISIBLE_DEVICES=0 ./tools/dist_test.sh configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py work_dirs/ckpts/model_best_2.pth 1`——恢复注释后，这条命令在评估五任务的同时向 `save_path` 逐场景写 `.pth`。

#### 4.3.4 代码实践

**实践目标**：写出「检查 `.pth` 内容 + 转换为下游分类输入」的最小脚本骨架，建立格式契约的动手认知。

**操作步骤**：

1. 检查脚本（示例代码，可在仅有合成 `.pth` 时先跑通逻辑）：

   ```python
   # 示例代码：检查 UniSeg3D 类无关掩码 .pth 的内容契约
   import torch, glob

   for p in sorted(glob.glob('your_save_folder_path_here/*.pth')):
       info = torch.load(p)
       ins, conf = info['ins'], info['conf']
       scene_id = p.split('/')[-1].replace('.pth', '')
       print(scene_id, 'ins:', ins.shape, ins.dtype,
             '每掩码点数 min/max: %d/%d' % (ins.sum(1).min(), ins.sum(1).max()),
             'conf: %.3f~%.3f, K=%d' % (conf.min(), conf.max(), len(conf)))
   ```

2. 转换脚本骨架——把 `(ins, conf)` 组织成「带占位类别的候选清单」，供对接 Open3DIS 掩码分类阶段（具体字段名以 Open3DIS 文档为准，标注待确认）：

   ```python
   # 示例代码：UniSeg3D 输出 → 下游分类器候选清单的转换骨架
   import torch, glob, json

   proposals = []
   for p in sorted(glob.glob('your_save_folder_path_here/*.pth')):
       info = torch.load(p)
       proposals.append(dict(
           scene_id=p.split('/')[-1].replace('.pth', ''),
           masks=info['ins'].tolist(),        # (K,N) 0/1
           confs=info['conf'].tolist(),       # (K,) 质量分，可作排序/截断
           labels=[None] * len(info['conf']), # 占位：待 Open3DIS 分类填充
       ))
   # 后续：按 Open3DIS RUN.md 的掩码分类输入格式对齐字段名（待确认）
   ```

3. 阅读 Open3DIS 仓库 `docs/RUN.md`，把其掩码分类阶段要求的输入字段与本清单逐项对照，补齐差异字段。

**需要观察的现象**：每个 `.pth` 的 `ins` 行数（K）远小于场景超点数；`conf` 全部大于 0.6；`ins` 每行点数（`ins.sum(1)`）不少于数百点（0.6 质量分过滤后残留的通常是完整物体）；不同场景 K 值差异明显（物体数不同）。

**预期结果**：能对任意一个 `.pth` 说出三个事实——掩码数 K、每掩码覆盖点数范围、conf 分布；转换脚本产出的候选清单字段完整、行序与 conf 对齐。Open3DIS 侧的具体输入字段名与命令待本地验证（以其 RUN.md 为准）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `conf` 必须与 `ins` 一起保存，而不能事后从 `ins` 反推？

**答案**：`conf = mask_score × preds_iou`，其中 `preds_iou` 来自 IoU 头对查询向量的回归，`mask_score` 依赖原始 sigmoid 分数；落盘的 `ins` 已经过二值化，概率信息全部丢失，无法从 0/1 掩码反推质量分。下游分类与 AP 评估都需要连续置信度做排序，所以必须与掩码同序保存。

**练习 2**：`.replace('bin', 'pth')` 这个文件名转换有什么潜在风险？为什么在本场景恰好安全？

**答案**：`str.replace` 会替换子串的所有出现且不校验扩展名。若 basename 中除扩展名外还含 `bin` 子串（如假设的场景名 `scene0bin00.bin`）会被误替换。ScanNet 点文件命名为 `sceneXXXX_XX.bin`，`bin` 只在扩展名出现一次，故恰好安全；更稳妥的写法是基于后缀替换（如 `os.path.splitext`）。

**练习 3**：如果只想给一个验证场景导出掩码（而不是整个验证集），有哪些可行办法？

**答案**：办法一，改小 `ann_file`（构造只含目标场景的子集 pkl）；办法二，用 `--cfg-options` 覆盖 val 数据集的 `ann_file` 或 `data_prefix`；办法三，导出后只保留目标 `sceneXXXX_XX.pth`。注意不能靠改 `batch_size` 解决（块内硬编码 `[0]` 索引，批内只处理第一个样本）。

## 5. 综合实践

**任务**：完整跑通一次「恢复注释 → 推理落盘 → 检查契约 → 格式转换」的 OV 第一阶段流水线。

1. **准备**：按 u1-l4 备好 ScanNet 验证数据与 `uniseg3d_infos_val.pkl`，按 u1-l2 下载 checkpoint（OV 指标 19.7 对应 `model_best_2.pth`）与 CLIP 预训练权重。
2. **改代码**：取消 [uniseg3d/uniseg3d.py:840-872](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L840-L872) 的注释（仅此一处，注意保留缩进与 `if self.pred_iou:` 的嵌套层级），把 `save_path` 设为 `work_dirs/ov_masks`。
3. **推理**：运行 `CUDA_VISIBLE_DEVICES=0 ./tools/dist_test.sh configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py work_dirs/ckpts/model_best_2.pth 1`。观察终端每次前向打印的 `Class-agnostic masks saved to: ...`。
4. **检查契约**：用 4.3.4 的检查脚本抽查 2-3 个 `.pth`：`ins` 形状 `(K, N)`、uint8、每行点数分布，`conf` 长度 K 且全部 > 0.6；记录不同场景的 K 值并解释差异。
5. **阈值敏感性**：把 `iou_threshold` 改为 0.4 与 0.8 各重跑一个场景，记录 K 的变化，结合 4.2 的公式解释「联合质量分」对候选数量的影响。
6. **转换对接**：用 4.3.4 的转换脚本生成候选清单，对照 Open3DIS 的 RUN.md 补齐其掩码分类阶段要求的字段，走通其步骤 1/2（分类）与步骤 5（评估）。
7. **回归检查**：确认 `dist_test.sh` 结束时五任务指标（PQ/mIoU/mAP 等）与注释前一致——验证「旁路导出不影响常规推理」。

**预期结果**：`work_dirs/ov_masks` 下每个验证场景一个 `.pth`，内容满足 4.3.2 的契约表；五任务指标与官方日志（README 提供 `model_best.log` 下载）量级一致。第 3-6 步需要本地 GPU 与数据，**待本地验证**；第 2、7 步的代码层面的预期（嵌套关系、旁路不改 `x`）可从源码直接确认。

## 6. 本讲小结

- 开放词汇是六任务中唯一「分类外置」的任务：第一阶段在仓库内生成**类无关掩码候选**（一段可选注释代码，位于 `predict()` 的 `get_pred_iou` 之后、`predict_by_feat` 之前），第二阶段交给 Open3DIS 用基础模型分类并评估 AP。
- 类无关掩码来自实例分支的掩码 logits 复用：不用 `cls_preds`，全量原始查询参与，每条查询得到一条 `(Q,S)` 的超点级掩码。
- 质量过滤是**乘积信号**：`mask_score`（正 logit 超点上的 sigmoid 均值，公式与实例路径 `obj_normalization` 完全一致）× `preds_iou`（IoU 头回归值），`iou_threshold=0.6` 筛候选、`mask_threshold=0.4` 做点级二值化（与 `test_cfg.sp_score_thr` 同值）。
- 保存契约：每场景一个 `.pth`，内含 `dict(ins=(K,N) uint8, conf=(K,))`，文件名由 `lidar_path` 的 `bin→pth` 替换得到；`[:, sp_pts_mask]` 花式索引负责超点→原始点展开。
- 两个硬前提：`pred_iou=True`（否则 `x['preds_iou']` 不存在）与验证 `batch_size=1`（块内硬编码 `[0]` 索引）；该块是纯旁路，不影响其余五任务的推理与评估。

## 7. 下一步学习建议

- 下一讲 u6-l4「二次开发指南」将把本系列的认知收束为扩展能力：新增提示类型四件套、接入新数据集、替换骨干/解码器——本讲的「硬编码阈值与 `[0]` 索引」正是可以重构为 `test_cfg` 配置项的现成案例。
- 建议精读 Open3DIS 仓库的掩码分类实现（其 RUN.md 步骤 1/2 对应代码），理解 2D 多视角特征如何投影回 3D 掩码完成词表无关分类，与本讲的 4.1 范式互为印证。
- 回顾 u5-l4（IoU 分支的训练监督）与 u6-l1（实例路径七道工序），体会 `mask_iou_score` 这同一信号在「实例分数修正」「全景分数修正」「OV 候选过滤」三处的一致设计。
- 若要复现论文 OV AP 19.6/19.7，按 README 下载 `model_best_2.pth` 与训练日志，对照本讲综合实践的阈值默认值（0.6/0.4）逐项核对。
