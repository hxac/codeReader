# 更多点云检测器：GroupFree3D、3DSSD、H3DNet

## 1. 本讲目标

上一讲（u6-l2）我们端到端拆解了 VoteNet：PointNet++ 骨干 → 投票 → 分组聚合 → BaseConvBboxHead 出框。本讲横向对比三个在 VoteNet 之上演进的进阶点云检测器，学完后你应该能够：

1. 说出 GroupFree3D 如何用「候选点采样 + Transformer 解码器迭代精修」替代 VoteNet 的「投票 + 分组」范式，并解释 query/key/value 与两级位置编码的来源。
2. 说出 3DSSD 的三个辅助分支（投票、centerness、corner loss）各自解决什么问题，理解 F-FPS 前景采样与 `sample_mode='spec'` 聚合。
3. 说出 H3DNet 的两阶段结构：VoteNet 当 RPN 产提案，RoI 阶段用面（z/xy）与边（line）三类几何基元匹配提案并精修框。
4. 能够对比三种 head 的 `get_targets` 实现：候选点从哪来、正负样本如何分配、监督分支有哪些。

## 2. 前置知识

本讲默认你已掌握 u6-l1（PointNet++ 的 SA/FP 模块）与 u6-l2（VoteNet 链路）。先回顾 VoteNet 留下的三个问题，本讲的三个检测器正好各回答一个：

- **问题一：候选点靠 D-FPS（几何最远点采样）**。FPS 只保证空间均匀，不保证前景覆盖——物体表面点可能一个都没采到。
- **问题二：每个提案独立打分**。VoteNet 的 256 个提案之间互不通信，同一个物体可能被多个提案重复预测，提案也无法互相借鉴上下文。
- **问题三：单阶段精度有上限**。框只预测一次，没有「先粗后细」的精修机会。

三个新概念提前解释：

- **Transformer 解码器（decoder）**：一种迭代精修结构。维护一组「查询向量」（query，每个 query 对应一个候选框），每层先在 query 之间做自注意力（self-attention）交换信息，再用交叉注意力（cross-attention）从「键值集合」（key/value，这里是全部种子点特征）中提取信息，最后经前馈网络（FFN）更新 query。DETR、GroupFree3D 都是这个套路。
- **Centerness（中心度）**：借自 2D 检测 FCOS 的思想。点到框中心的「居中程度」是一个 0~1 的连续值，越居中的点预测的框越可靠，用它替代二值的 objectness 可以抑制「框边上发出的坏框」。
- **几何基元（primitive）**：H3DNet 把 3D 框分解为更小的几何部件——6 个面的面心、12 条边的边心。先让网络学会预测这些中间几何量（有显式监督），再用它们拼回完整框。这是「引入中间表示降低直接回归难度」的典型做法。

另外回顾两个 u6-l2 的结论，本讲会反复用到：室内检测器**没有 IoU assigner**，提案与真值的匹配靠「点在哪个框内」或 Chamfer 最近邻；`PartialBinBasedBBoxCoder` 把朝向和尺寸拆成「bin 分类 + 残差回归」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `mmdet3d/models/detectors/groupfree3dnet.py` | GroupFree3D 检测器薄壳，继承 SingleStage3DDetector |
| `mmdet3d/models/dense_heads/groupfree3d_head.py` | GroupFree3D 头：候选采样、初始预测、Transformer 解码器迭代、逐层监督 |
| `mmdet3d/models/layers/transformer.py` | GroupFree3DMHA 注意力与 ConvBNPositionalEncoding 位置编码 |
| `mmdet3d/models/task_modules/coders/groupfree3d_bbox_coder.py` | GroupFree3D 的框编码器（继承 PartialBinBasedBBoxCoder，支持 size_cls_agnostic） |
| `mmdet3d/models/detectors/ssd3dnet.py` | 3DSSD 检测器薄壳，继承 VoteNet |
| `mmdet3d/models/dense_heads/ssd_3d_head.py` | SSD3DHead：继承 VoteHead，新增 centerness/vote/corner 三个监督 |
| `mmdet3d/models/task_modules/coders/anchor_free_bbox_coder.py` | 3DSSD 的 anchor-free 框编码器（半尺寸编码） |
| `mmdet3d/models/detectors/h3dnet.py` | H3DNet 两阶段检测器：RPN（VoteHead）+ RoI（H3DRoIHead） |
| `mmdet3d/models/roi_heads/h3d_roi_head.py` | H3DRoIHead：组装三个 PrimitiveHead 与 H3DBboxHead |
| `mmdet3d/models/roi_heads/mask_heads/primitive_head.py` | PrimitiveHead：面/边基元的投票、聚合与预测 |
| `mmdet3d/models/roi_heads/bbox_heads/h3d_bbox_head.py` | H3DBboxHead：用基元特征匹配提案并精修框 |
| `configs/_base_/models/{groupfree3d,3dssd,h3dnet}.py` | 三个模型的基座配置 |
| `tests/test_models/test_detectors/test_{groupfree3d,3dssd,h3dnet}.py` | 三个检测器的伪数据测试，是本讲实践的依据 |

## 4. 核心概念与源码讲解

### 4.1 GroupFree3DNet：不用投票，用 Transformer 迭代出框

#### 4.1.1 概念说明

Group-Free 3D（论文 *Group-Free 3D Object Detection via Point-to-Box Queries*, 2021）的出发点是：VoteNet 的「下采样-投票-分组」链路里，候选框质量被最初的采样点限制死了——采样点没落在物体上，后面全白费。它干脆把「出框」变成一个**迭代精修**过程：

1. 从种子点里选一批**候选点**（object candidates），每个候选点先用一个小网络出一个初始框；
2. 把每个初始框（的 query 向量）送入 N 层 Transformer 解码器，每层都与全部种子点做交叉注意力，然后**重新预测一遍框**；
3. 训练时每一层都有监督（逐层深监督），推理时可以只用最后一层或最后几层的输出。

这样候选框不再是「一次定终身」，而是逐层向正确的框收敛——这就是名字里 "Group-Free" 的含义：不再需要 VoteNet 那种「投票后按点分组」的结构。

#### 4.1.2 核心流程

```text
PointNet2SASSG 骨干
  └─ fp_xyz[-1] / fp_features[-1]  →  seed 点坐标 + 特征 (B, 288, 1024)
       │
       ├─ 候选采样（训练/测试可分别选 fps 或 kps）
       │    fps: PointsSampler 几何最远点采样 256 个
       │    kps: PointsObjClsModule 给每个 seed 打 objectness 分，取 top-256
       │    → GeneralSamplingModule 按索引取回候选点坐标/特征
       │
       ├─ conv_pred（BaseConvBboxHead）出初始框，前缀 'proposal.'
       │
       └─ 循环 i = 0..5（num_decoder_layers=6）
            query_pos = ConvBNPositionalEncoding(base_bbox3d 的 6 维)   # 自注意力位置
            key_pos   = ConvBNPositionalEncoding(seed_xyz 的 3 维)      # 交叉注意力位置
            query = decoder_layers[i](query, key, value, query_pos, key_pos)
            prediction_heads[i](query) 再出框，前缀 's{i}.'
            base_bbox3d 更新为本层框的前 6 维（detach）
```

#### 4.1.3 源码精读

检测器本身是薄壳，只负责把原始点云一并传给头：

- [mmdet3d/models/detectors/groupfree3dnet.py:L8-L25](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/groupfree3dnet.py#L8-L25) —— `GroupFree3DNet(SingleStage3DDetector)` 只做 backbone + bbox_head 的组装，不含任何前向逻辑。
- [mmdet3d/models/detectors/groupfree3dnet.py:L45-L48](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/groupfree3dnet.py#L45-L48) —— `loss` 中先 `extract_feat` 再把 `points`（原始点云，用于框内点过滤）与特征一起交给 `bbox_head.loss`。`predict` 同理。这与 VoteNet 检测器的写法一致（u6-l2 讲过）。

头的 `__init__` 把三样东西组装起来：解码器、采样器、逐层预测头。

- [mmdet3d/models/dense_heads/groupfree3d_head.py:L199-L205](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/groupfree3d_head.py#L199-L205) —— 用 mmcv 的 `build_transformer_layer` 复制 6 份解码器层，并断言 `embed_dims`（288）与两套位置编码的 `num_pos_feats` 一致——这是配置里 288 反复出现的根源。
- [mmdet3d/models/dense_heads/groupfree3d_head.py:L212-L221](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/groupfree3d_head.py#L212-L221) —— 三个采样/预测组件：`GeneralSamplingModule`（按索引取样）、`Points_Sampler`（fps 模式）、`PointsObjClsModule`（kps 模式的打分网络）、初始预测头 `conv_pred`。
- [mmdet3d/models/dense_heads/groupfree3d_head.py:L241-L247](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/groupfree3d_head.py#L241-L247) —— 关键设计：**每一层解码器都配一个独立的 `BaseConvBboxHead`**（`prediction_heads`），而不是共享一个预测头。所以配置里的「L6」意味着 1 个初始头 + 6 个逐层头共 7 套预测分支。

kps（keypoint sampling）是 GroupFree3D 的招牌：让网络自己学会「哪些 seed 更可能是物体」。

- [mmdet3d/models/dense_heads/groupfree3d_head.py:L28-L82](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/groupfree3d_head.py#L28-L82) —— `PointsObjClsModule`：3 层 Conv1d + BN + ReLU，最后一层输出 1 通道 objectness logits，形状 (B, 1, num_seed)。
- [mmdet3d/models/dense_heads/groupfree3d_head.py:L347-L366](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/groupfree3d_head.py#L347-L366) —— `forward` 第一步：`fps` 走 `Points_Sampler`，`kps` 走 `points_obj_cls` 的 sigmoid 分数 `topk(num_proposal)`。`sample_mode` 是 property（[L309-L320](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/groupfree3d_head.py#L309-L320)），训练与测试可以配置不同模式——ScanNet 配置里训练和测试都用 `kps`。
- [mmdet3d/models/dense_heads/groupfree3d_head.py:L368-L375](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/groupfree3d_head.py#L368-L375) —— 候选特征经 `conv_pred` 出 cls/reg 两条分支，`split_pred` 拆成 `center/dir_class/dir_res/size/obj_scores/sem_scores` 等键（前缀 `proposal.`），再 `decode` 成初始 `bbox3d`。

Transformer 解码器循环是本模块的核心：

- [mmdet3d/models/dense_heads/groupfree3d_head.py:L377-L412](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/groupfree3d_head.py#L377-L412) —— 逐行看：`base_bbox3d` 取当前框的前 6 维（中心 + 尺寸，不含角度）且 `detach()`（位置编码不吃梯度）；`query` 由候选特征经 1×1 卷积投影而来，`key/value` 由**全部种子点**特征投影而来；每层先算两个位置编码，过一层解码器，再用该层专属的 `prediction_heads[i]` 重新出框并写回 `results[f's{i}.bbox3d']`，最后更新 `base_bbox3d` 进入下一层。
- [mmdet3d/models/layers/transformer.py:L94-L104](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/layers/transformer.py#L94-L104) —— `GroupFree3DMHA` 继承 mmcv 的 `MultiheadAttention`，唯一的改动是：根据它在层内的 `operation_name`（self_attn / cross_attn），把对应的位置编码加到 `value` 上。配合配置里的 `operation_order=('self_attn', 'norm', 'cross_attn', 'norm', 'ffn', 'norm')`（[configs/_base_/models/groupfree3d.py:L24-L38](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/groupfree3d.py#L24-L38)），即「候选框互相看 → 候选框看种子点 → FFN」三段式。
- [mmdet3d/models/layers/transformer.py:L118-L146](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/layers/transformer.py#L118-L146) —— `ConvBNPositionalEncoding`：两层 Conv1d 学出来的绝对位置编码，不是正弦编码。query 侧输入 6 维（框中心 + 尺寸），key 侧输入 3 维（点坐标）——这就是 `__init__` 默认参数里 `input_channel=6` 与 `input_channel=3` 的区别（[groupfree3d_head.py:L156-L163](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/groupfree3d_head.py#L156-L163)）。

框编码器方面，GroupFree3D 复用 u6-l2 讲过的 bin-based 编码，但多了一个 `size_cls_agnostic` 开关：

- [mmdet3d/models/dense_heads/groupfree3d_head.py:L274-L287](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/groupfree3d_head.py#L274-L287) —— 回归通道数二选一：类无关时输出 `6 + num_dir_bins*2`（中心残差 3 + 尺寸 3 + 朝向分类/残差），类相关时输出 `3 + num_dir_bins*2 + num_sizes*4`（尺寸变成「哪一类 + 每类 3 维残差」）。
- [mmdet3d/models/task_modules/coders/groupfree3d_bbox_coder.py:L171-L198](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/coders/groupfree3d_bbox_coder.py#L171-L198) —— `split_pred` 的尺寸段按 `size_cls_agnostic` 拆成 `size`（3 维）或 `size_class + size_res_norm`（num_sizes + num_sizes×3 维）；obj/sem 分数永远从 cls 分支的第 0 列与余下列切出。

#### 4.1.4 代码实践

**实践目标**：用仓库自带的伪数据测试验证 GroupFree3D 的「逐层出框」结构，确认损失键名里的层前缀。

1. 操作步骤（需要 CUDA 环境；无 GPU 见下方替代方案）：

```bash
python -m pytest tests/test_models/test_detectors/test_groupfree3d.py -q
```

2. 若无 GPU，做源码阅读型实践：展开配置观察解码器如何被复制。

```bash
python tools/misc/print_config.py configs/groupfree3d/groupfree3d_head-L6-O256_4xb8_scannet-seg.py | grep -A 3 num_decoder_layers
```

3. 再阅读 [tests/test_models/test_detectors/test_groupfree3d.py:L44-L49](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tests/test_models/test_detectors/test_groupfree3d.py#L44-L49)，注意断言的损失键：`sampling_objectness_loss`、`proposal.objectness_loss`、`s0.objectness_loss`、`s1.size_res_loss`、`s4.size_class_loss`——键名里的 `s0/s1/s4` 直接对应解码器第 0/1/4 层。

4. 需要观察的现象：测试断言「每一层都有正损失」，这正是 4.2 要讲的逐层深监督。

5. 预期结果：有 GPU 时测试全绿；无 GPU 时 print_config 能看到 `num_decoder_layers=6`、`num_proposal=256`，以及 `transformerlayers` 的完整展开。待本地验证（本讲义写作环境未执行）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `base_bbox3d = bbox3d[:, :, :6].detach().clone()` 要取前 6 维并 detach？
**答案**：前 6 维是中心 (3) + 尺寸 (3)，是「这个候选框现在在哪、多大」的几何摘要；角度对位置编码帮助不大。`detach()` 切断梯度，让位置编码只是一个「条件输入」，梯度只通过 query 主干传递，避免解码器为了方便编码而故意挪框。

**练习 2**：`fps` 与 `kps` 两种采样模式各适合什么场景？
**答案**：`fps`（几何最远点采样）不需要额外训练、分布均匀但前景覆盖差；`kps` 用 `PointsObjClsModule` 学出来的前景分数取 top-K，候选点更落在物体附近，但依赖 `sampling_objectness_loss` 先把打分网络训好。ScanNet 配置训练与测试都用 `kps`。

**练习 3**：配置里 `prediction_stages='last_three'`（[configs/groupfree3d/groupfree3d_head-L6-O256_4xb8_scannet-seg.py:L65-L70](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/groupfree3d/groupfree3d_head-L6-O256_4xb8_scannet-seg.py#L65-L70)）与基座配置里的 `'last'` 有什么区别？
**答案**：这是推理期选项（[groupfree3d_head.py:L993-L1008](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/groupfree3d_head.py#L993-L1008)）：决定推理时把哪几层的框拼起来送 NMS。`last` 只用最后一层（256 个框），`last_three` 把最后三层（768 个框）concat 后一起做 NMS，召回更高但更慢；`all` 再加上初始 proposal 层。训练不受它影响。

### 4.2 三种 head 的监督机制：get_targets 如何分配正负样本

#### 4.2.1 概念说明

单元 5 的室外检测器用 `Max3DIoUAssigner` 按 IoU 分配 anchor；室内点云检测器（VoteNet/GroupFree3D/3DSSD）走的是另一条路：**「点属于哪个框」的从属关系 + 索引查表**。理由很简单——室内框是提案点直接回归出来的，点云就在那，直接问「这个候选点落在哪个 gt 框里」比算旋转 IoU 便宜得多，也更符合点云的拓扑。

三个检测器的差别集中在三件事上：

1. **候选点从哪来**：GroupFree3D 是 seed 的 top-K（kps）；3DSSD 是 F-FPS 前景 seed 生成的投票点；H3DNet 的 RPN 就是 VoteNet，候选是投票聚合点。
2. **怎么判定正负样本**：GroupFree3D/3DSSD 都用 `points_in_boxes_*`（点在框内）判定，但 3DSSD 额外叠加了距离阈值；VoteNet/H3DNet-RPN 用 Chamfer 距离（u6-l2 讲过的 0.3/0.6）。
3. **回归目标怎么编码**：GroupFree3D 用 bin-based（类相关/类无关可切换），3DSSD 用 anchor-free 半尺寸，H3DNet 用 bin-based。

#### 4.2.2 核心流程

以 GroupFree3D 的 `_get_targets_single` 为例（3DSSD 的版本结构相同，判定规则不同）：

```text
原始点 points (N,3) + gt 框
  │
  ├─ bbox_coder.encode(gt) → center/size/dir 的 bin 编码目标
  │
  ├─ 第 0 步：points_in_boxes_part(points) → 每个点属于哪个 gt（或背景）
  │     → pts_obj_mask、pts_instance_label（每点一个 gt 编号或 -1）
  │
  ├─ 第 1 步（采样监督）：对 seed 做两件事
  │     a. 用归一化欧氏距离给每个 gt 挑 seed_points_obj_topk=4 个最近 seed
  │        → sampling_targets（PointsObjClsModule 的监督）
  │     b. 查表 seed 的 pts_instance_label
  │
  └─ 第 2/3 步（框监督）：candidate 的从属关系不再重新算几何，
        而是用 query_points_sample_inds 从 seed 的 label 查表：
        objectness_targets ← pts_obj_mask 查表
        assignment         ← pts_instance_label 查表（背景点挂到最后一个 gt）
        assigned_center/size/dir/class 目标 ← 按 assignment 索引 gt 编码目标
```

#### 4.2.3 源码精读

- [mmdet3d/models/dense_heads/groupfree3d_head.py:L758-L783](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/groupfree3d_head.py#L758-L783) —— 一切从 `self.bbox_coder.encode` 开始：`center_target` 取 `gravity_center`（几何中心口径，u6-l2 已强调），尺寸/朝向拆 bin。随后把 gt 补齐到 `max_gt_num=64`，多余框中心平移 +1000（推到无穷远，永远匹配不上）。
- [mmdet3d/models/dense_heads/groupfree3d_head.py:L791-L835](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/groupfree3d_head.py#L791-L835) —— `with_rot=True` 时（SUN RGB-D 类数据）逐点构造投票目标（与 VoteHead 同款逻辑）；ScanNet 是 `with_rot=False`（轴对齐框），走 [L837-L852](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/groupfree3d_head.py#L837-L852)：按 `pts_instance_mask` 分组，用组内点云的包围盒中心与 `center_targets` 最近邻匹配，得到每点的 `pts_instance_label`。
- [mmdet3d/models/dense_heads/groupfree3d_head.py:L868-L896](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/groupfree3d_head.py#L868-L896) —— 采样监督的构造：先把 seed 到各 gt 几何中心的距离按框尺寸归一（\(\tilde{d} = \frac{x-c}{w,h,l}\)），再只对「从属于该 gt 的 seed」取 top-4 最近者标 1。这保证了 kps 的打分网络只在「点确实属于某物体且离中心近」的位置上给高分。
- [mmdet3d/models/dense_heads/groupfree3d_head.py:L898-L936](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/groupfree3d_head.py#L898-L936) —— 框监督的查表三连：`objectness_targets` 由 `pts_obj_mask` 经 seed → candidate 两级 `torch.gather` 得到；`assignment` 由 `pts_instance_label` 同法得到（背景点挂到最后一个 gt，配合 `+1000` 的假中心使其损失被压为 0）；随后 `assigned_center_targets = center_targets[assignment]` 等按行索引生成全部回归目标。残差还要做两次归一化：朝向残差除以 bin 宽度 \(\pi/N\)，尺寸残差除以该类的 `mean_sizes`。
- [mmdet3d/models/dense_heads/groupfree3d_head.py:L503-L516](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/groupfree3d_head.py#L503-L516) —— 逐层深监督：`prefixes = ['proposal.'] + ['s0.' ... 's5.']`，每层都算一遍 objectness/center/dir_class/dir_res/size/semantic 六项损失，并**除以层数** `num_stages` 均摊权重。这就是测试里能断言 `s4.size_class_loss > 0` 的原因。

对比 3DSSD 的 `get_targets_single`，正负样本判定多了一个「近处才算正」的约束：

- [mmdet3d/models/dense_heads/ssd_3d_head.py:L385-L400](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/ssd_3d_head.py#L385-L400) —— 用 `_assign_targets_by_points_inside`（内部是 `points_in_boxes_all` + `argmax`，见 [L565-L583](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/ssd_3d_head.py#L565-L583)）得到每个聚合点的从属 gt；正样本要求「在框内 **且** 到框顶面中心的距离 < `pos_distance_thr=10.0` 米」（KITTI 室外场景框大，纯点在框内会放进来一堆离中心很远的候选），负样本是「不在任何框内」，中间地带既不正也不负。
- [mmdet3d/models/dense_heads/ssd_3d_head.py:L402-L433](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/ssd_3d_head.py#L402-L433) —— centerness 目标的计算：把聚合点搬进 gt 框的**规范坐标系**（减中心、按 `-yaw` 反旋转），取到六个面的距离 \(l_f, l_b, l_l, l_r, l_t, l_d\)（clamp 到非负），则

  \[ \text{centerness} = \sqrt[3]{ \frac{\min(l_f,l_b)}{\max(l_f,l_b)} \cdot \frac{\min(l_l,l_r)}{\max(l_l,l_r)} \cdot \frac{\min(l_t,l_d)}{\max(l_t,l_d)} } \in [0, 1] \]

  点恰在框中心时三分量全为 1，centerness=1；越贴边越小。
- [mmdet3d/models/dense_heads/ssd_3d_head.py:L442-L451](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/ssd_3d_head.py#L442-L451) —— 投票辅助分支的监督：把 gt 框按 `expand_dims_length=0.05` **放大**（且底面下移 0.05），落进放大框的 seed 才需要投票，目标是从属 gt 的 `gravity_center - seed`。用放大框是为了让「贴着物体表面的点」也能参与投票。

H3DNet 的 RPN 就是普通 `VoteHead`，其 `get_targets` 即 u6-l2 讲过的 Chamfer 0.3/0.6 匹配，本节不重复；RoI 阶段的目标分配见 4.4。

#### 4.2.4 代码实践

**实践目标**：亲手对齐「三个 head 的监督结构」，产出对比表（本讲主实践的前半部分）。

1. 操作步骤：分别打开三个 `get_targets` 的**单样本版本**并数其返回元组的长度：

   - GroupFree3D：[`_get_targets_single`](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/groupfree3d_head.py#L940-L943) 返回 11 个张量（[L940](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/groupfree3d_head.py#L940)）。
   - 3DSSD：[`get_targets_single`](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/ssd_3d_head.py#L453-L456) 返回 11 个张量。
   - VoteNet/H3DNet-RPN：`mmdet3d/models/dense_heads/vote_head.py` 的 `_get_targets_single`（u6-l2 已精读，返回 9 个）。

2. 用下面的表格模板整理（参考答案见第 5 节）：

   | 维度 | VoteNet（H3DNet-RPN） | GroupFree3D | 3DSSD |
   | --- | --- | --- | --- |
   | 候选点来源 | … | … | … |
   | 正负样本判定 | … | … | … |
   | objectness 形式 | 二值 | 二值 + 采样级二值 | 连续 centerness |

3. 需要观察的现象：三个函数的**第一行有效代码几乎都是 `gt_bboxes_3d.to(points.device)` + 空场景造假框**，这是这一族 head 的共同模板；真正的分歧出现在「从属关系怎么算」那 10 行。

4. 预期结果：你能指出 GroupFree3D 的从属关系算在**原始点**上再查表到 candidate，而 3DSSD 直接算在**聚合点**上——一个是两级 gather，一个是一级。

5. 待本地验证（纯源码阅读，无需运行）。

#### 4.2.5 小练习与答案

**练习 1**：GroupFree3D 为什么把「背景 candidate」挂到最后一个 gt，而不是像 anchor 检测器那样给个「负类」标签？
**答案**：因为它没有分类意义上的负类通道——objectness 是独立分支，背景与否由 `objectness_targets`（查 `pts_obj_mask` 得到的 0）单独监督；而回归分支需要每个 candidate 都有一个可索引的 gt 行，挂到最后一个 gt（其中心已被平移 +1000）能让回归目标成为「永远追不上的远处假框」，配合 `box_loss_weights`（只在 objectness=1 处非零）实际不产生梯度。

**练习 2**：3DSSD 的 `pos_distance_thr` 若设得非常大，训练会发生什么？
**答案**：正负判定退化回「只要点在框内就算正样本」。室内小物体没问题，但 KITTI 场景（该配置正是 `kitti-3d-car`，阈值 10 米）框可达数米长，框边缘的聚合点回归出的框误差大，会污染回归监督。这个阈值本质上是「离中心多远我还信你」的先验。

**练习 3**：为什么 GroupFree3D 的采样监督要用「框尺寸归一化后的距离」挑 top-4，而不是直接用欧氏距离？
**答案**：大物体（如橱柜）的 seed 到中心的绝对距离天然更大，用原始欧氏距离会让大物体的近中心 seed 排不进 top-4；除以框尺寸后，衡量的是「相对居中程度」，对不同大小的物体公平。

### 4.3 3DSSD：F-FPS 前景采样与三个辅助分支

#### 4.3.1 概念说明

3DSSD（论文 *3DSSD: Point-based 3D Single Stage Object Detector*, 2020）针对的是问题一（FPS 前景覆盖差），并顺带把 VoteNet 的两阶段骨干砍成单阶段：

- **F-FPS（Feature-FPS）**：FPS 的「远」不用欧氏距离，而用**特征距离**。前景物体内部点特征相似，F-FPS 会在每个物体上各留代表点，从而保住前景覆盖；D-FPS 继续负责铺背景。两者混合（`FS` 模式）按 1:1 交替采样。
- **投票降级为辅助分支**：VoteNet 的投票是「主路径」（提案就长在投票点上）；3DSSD 的投票只是给前景 seed 学一个指向物体中心的偏移（有独立 `vote_loss` 监督），真正聚合时用的是 **spec 模式**——以投票点为新中心、在全部 seed 中球查询。
- **三个新监督**：centerness（替代二值 objectness，见 4.2）、corner loss（对解码后框的 8 个角点直接回归，等价于同时约束中心/尺寸/朝向的组合误差）、vote loss。

#### 4.3.2 核心流程

```text
PointNet2SAMSG 骨干（无 FP 塔，u6-l1 讲过）
  三级 SA，fps_mods = ('D-FPS'), ('FS'), ('F-FPS','D-FPS')
  第三级出 512 点 = F-FPS 前景代表 + D-FPS 背景
       │
SSD3DHead（继承 VoteHead 的 forward 四步）
  1. VoteModule：只对前 256 个（num_points=256，即 F-FPS 段）seed 生成投票
  2. sample_mode='spec'：聚合中心 = 投票点 target_xyz，球查询空间 = 全部 seed
  3. BaseConvBboxHead 出 cls（num_classes 通道语义分）/ reg（3+3+dir_bins*2）
  4. AnchorFreeBBoxCoder.split_pred 解码
       │
损失七项：centerness + center + dir_class + dir_res + size + corner + vote
```

#### 4.3.3 源码精读

- [mmdet3d/models/detectors/ssd3dnet.py:L6-L26](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/ssd3dnet.py#L6-L26) —— `SSD3DNet(VoteNet)` 完全继承 VoteNet 的 loss/predict，连 `__init__` 都只是透传。**3DSSD 的全部个性都在 head 和骨干配置里**，这是「配置组合优于类继承」的又一次体现（对照 u4-l1 讲过的 PointPillars 之于 VoxelNet）。
- [configs/_base_/models/3dssd.py:L14-L15](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/3dssd.py#L14-L15) —— 三级 SA 的采样策略：第一级纯 D-FPS 铺满场景，第二级 FS 混合，第三级 `('F-FPS', 'D-FPS')` 且 `fps_sample_range_lists=(512, -1)`（前 512 个名额给 F-FPS、其余给 D-FPS，[L15](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/3dssd.py#L15)）。`in_channels=4`：KITTI 点带强度通道。
- [mmdet3d/models/dense_heads/vote_head.py:L351-L356](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/vote_head.py#L351-L356) —— `spec` 模式：`aggregation_inputs = dict(points_xyz=seed_points, features=seed_features, target_xyz=vote_points)`。语义是「在 seed 点集合中搜索邻居，但聚合中心用外部给定的投票点」。
- [mmdet3d/models/layers/pointnet_modules/point_sa_module.py:L116-L141](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/layers/pointnet_modules/point_sa_module.py#L116-L141) —— SA 模块内部对 `target_xyz` 的处理：给了它就不做 FPS，直接当作 `new_xyz`。这与 u6-l1 讲过的 `StackedSAModuleMSG`（PV-RCNN 外部给定中心点）是同一个机制。
- [mmdet3d/models/dense_heads/ssd_3d_head.py:L62-L80](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/ssd_3d_head.py#L62-L80) —— `SSD3DHead.__init__` 先以 `size_class_loss=None, semantic_loss=None` 调父类（说明 3DSSD 不要尺寸分类、不要语义分割分支），再新建 `corner_loss` 与 `vote_loss`，并记下 `num_candidates = vote_module_cfg['num_points']`（256）。
- [mmdet3d/models/dense_heads/ssd_3d_head.py:L82-L92](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/ssd_3d_head.py#L82-L92) —— 分支宽度：分类只出 `num_classes` 通道（**没有独立 objectness 通道**），回归出 `3 + 3 + num_dir_bins*2 = 3 + 3 + 24`（KITTI Car 配置 `num_dir_bins=12`，见 [configs/3dssd/3dssd_4xb4_kitti-3d-car.py:L94-L95](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/3dssd/3dssd_4xb4_kitti-3d-car.py#L94-L95)）。
- [mmdet3d/models/dense_heads/ssd_3d_head.py:L475-L476](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/ssd_3d_head.py#L475-L476) —— 推理时 objectness 分数直接取语义分数的最大值：`obj_scores = sem_scores.max(-1)[0]`。centerness 分支在推理中反而不用（它只做训练期的加权）——这是阅读源码才能确认的细节。
- [mmdet3d/models/dense_heads/ssd_3d_head.py:L179-L200](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/ssd_3d_head.py#L179-L200) —— corner loss 的做法很绕但值得看懂：先把**朝向分类换成 one-hot 真值**再 decode 出「朝向正确、其余用预测」的框，取其 8 角点与真值角点做 SmoothL1。等价于把「朝向离散选对了没」从回归误差里剥离，只惩罚连续量。
- [mmdet3d/models/task_modules/coders/anchor_free_bbox_coder.py:L40-L44](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/coders/anchor_free_bbox_coder.py#L40-L44) —— 尺寸编码是**半尺寸**：`size_res_target = gt_bboxes_3d.dims / 2`；解码时 [L88](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/coders/anchor_free_bbox_coder.py#L88) 乘回 2 并 `clamp(min=0.1)` 防止退化框。注意 `encode` 的中心取的仍是 `gravity_center`，且朝向残差除以 bin 宽度 \(2\pi/N\)（[L51](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/task_modules/coders/anchor_free_bbox_coder.py#L51)），与 GroupFree3D 的 \(\pi/N\) 不同——后者 `angle2class` 的周期约定不同（继承自 `PartialBinBasedBBoxCoder.angle2class`，u6-l2 已提）。
- [mmdet3d/models/dense_heads/ssd_3d_head.py:L149-L215](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/ssd_3d_head.py#L149-L215) —— `loss_by_feat` 汇总七项损失。注意权重分工：`centerness_weights` 覆盖正+负样本（二值判定要全体参与），`box_loss_weights` 只归一化正样本（回归只在正样本上算），`heading_res_loss_weight` 是 one-hot 朝向 × 正样本权重（残差只惩罚选中的 bin）——三套权重各管一段，与 4.2 的目标结构一一对应。

#### 4.3.4 代码实践

**实践目标**：跑通 3DSSD 的伪数据测试，并用配置确认「无 FP 塔 + spec 聚合」。

1. 操作步骤（需 CUDA）：

```bash
python -m pytest tests/test_models/test_detectors/test_3dssd.py -q
```

2. 无 GPU 替代：阅读 [tests/test_models/test_detectors/test_3dssd.py:L19-L39](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tests/test_models/test_detectors/test_3dssd.py#L19-L39)，注意 `create_detector_inputs(num_gt_instance=3, num_classes=1)`——**室内检测器测试套路在 3DSSD 上同样成立，只是它实际是室外 KITTI 配置**；测试只断言 `centerness_loss > 0`（3DSSD 特有的损失项）。

3. 再用 print_config 确认两件事：

```bash
python tools/misc/print_config.py configs/3dssd/3dssd_4xb4_kitti-3d-car.py | grep -E "fps_mods|sample_mode|bbox_coder" 
```

4. 需要观察的现象：`train_cfg` 里 `sample_mode='spec'`（而非 VoteNet 的 `'vote'`）；backbone 的 `fps_mods` 三级递进。

5. 预期结果：配置展开后能看到 `fps_mods=[['D-FPS'], ['FS'], ['F-FPS', 'D-FPS']]` 与 `AnchorFreeBBoxCoder(num_dir_bins=12, with_rot=True)`。待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：3DSSD 为什么把骨干的 FP 上采样塔删掉（对照 VoteNet 的 PointNet2SASSG 有 `fp_channels`）？
**答案**：VoteNet 需要 FP 塔把特征上采样回 1024 个 seed 以便投票；3DSSD 单阶段化后直接在 SA 末级的 512 点上工作（前 256 个前景代表点生成投票），FP 塔纯属浪费算力。这也是 `SSD3DHead._extract_input` 读 `sa_xyz[-1]` 而 GroupFree3DHead 读 `fp_xyz[-1]`（[ssd_3d_head.py:L94-L109](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/ssd_3d_head.py#L94-L109) 对照 [groupfree3d_head.py:L289-L307](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/groupfree3d_head.py#L289-L307)）的原因。

**练习 2**：centerness 监督在推理时没有用到，那它训练时起了什么作用？
**答案**：它是「分支间的软权重」。centerness 目标高的候选（近中心的）通过 `centerness_weights` 把语义分类分支的注意力集中在可靠候选上，等效于教语义分支「对居中候选更自信」。这是一种只用训练期、不动推理图的辅助监督。

**练习 3**：`vote_xyz_range=(3.0, 3.0, 2.0)`（[configs/_base_/models/3dssd.py:L24-L32](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/3dssd.py#L24-L32)）约束什么？
**答案**：VoteModule 的投票偏移在 x/y 方向最多 ±3 米、z 方向 ±2 米（用 tanh 压缩到范围）。防止前景点把票投到离谱远处，保证 spec 聚合的球查询（radius 4.8/6.4）能覆盖到票的邻居。

### 4.4 H3DNet：先提案，再用面/边基元精修

#### 4.4.1 概念说明

H3DNet（论文 *H3DNet: 3D Object Detection Using Geometric Sensing and Reasoning*, 2020）回答问题三：单阶段精度上限。它的思路不是堆注意力，而是**给中间几何量显式监督**：

- 一个 3D 框可以分解为 6 个**面**（法向 ±x/±y/±z 各一对）与 12 条**边**。H3DNet 把面再分成两类基元：`z` 面（法向 z 的上下水平面）与 `xy` 面（法向落在 xy 平面内的四个竖直面），加上 `line`（12 条边）共三个 PrimitiveHead。
- 整体是两阶段：RPN 就是一个标准 `VoteHead` 产 256 个提案（不做 NMS）；RoI 阶段三个 PrimitiveHead 从种子点特征预测基元中心与语义，然后把基元特征「匹配聚合」到每个提案的 6 面心 + 12 边心上，最后 `H3DBboxHead` 用这些匹配特征精修框。
- 直觉：直接回归 7 个自由度是「高维跳变」，而「预测底面在哪、侧棱在哪」是低维局部任务；基元中间监督给了网络更细粒度的纠错信号。

#### 4.4.2 核心流程

```text
MultiBackbone（4 路共享参数的 PointNet2SASSG，u6-l1 提过）
  → feats_dict: fp_xyz_net0 / hd_feature / fp_indices_net0
       │
H3DNet.loss 把特征重映射成 VoteHead 期望的 fp_xyz/fp_features/fp_indices
       │
RPN（VoteHead，sample_mode='vote'）loss_and_predict(ret_target=True)
  → 256 个提案 rpn_proposals + Chamfer 匹配 targets（0.3/0.6）
       │
H3DRoIHead
  ├─ primitive_z / primitive_xy / primitive_line 三个 PrimitiveHead
  │    每个：flag 分支（该处有无基元）+ VoteModule + PointSAModule 聚合
  │    → 基元中心 / 语义残差 / 语义分数，各自的 Chamfer/CE 损失
  └─ H3DBboxHead
       提案框 get_surface_line_center() → 6 面心 + 12 边心
       surface/line 两个 matcher 以基元中心为集合、提案面/边心为 target_xyz 聚合
       匹配分数分支 + 拼合特征 → bbox_pred 精修，键名后缀 '_optimized'
```

#### 4.4.3 源码精读

- [mmdet3d/models/detectors/h3dnet.py:L90-L94](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/h3dnet.py#L90-L94) —— 两阶段的衔接点：把 `fp_xyz_net0[-1]`/`hd_feature`/`fp_indices_net0[-1]` 三个键**改名为** `fp_xyz`/`fp_features`/`fp_indices`，让普通 VoteHead 无需修改即可当作 RPN 使用。`hd_feature` 是 4 路 backbone 融合后的特征（`MultiBackbone` 用 `aggregation_layers` 融合，配置见 [configs/_base_/models/h3dnet.py:L184-L205](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/h3dnet.py#L184-L205)）。
- [mmdet3d/models/detectors/h3dnet.py:L97-L109](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/h3dnet.py#L97-L109) —— RPN 调用 `loss_and_predict(..., ret_target=True)`：一次前向同时拿损失与提案，并把 `targets`（Chamfer 匹配结果）塞进 `feats_dict` 带给 RoI 阶段——**两阶段共享同一份提案-真值匹配**，RoI 的 bbox head 不再重新分配。注意 `rpn_proposal=dict(use_nms=False)`（[configs/_base_/models/h3dnet.py:329](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/h3dnet.py#L329)）：256 个提案全部送入第二阶段。
- [mmdet3d/models/roi_heads/h3d_roi_head.py:L34-L38](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/roi_heads/h3d_roi_head.py#L34-L38) —— `assert len(primitive_list) == 3` 后按顺序实例化 z/xy/line 三个 PrimitiveHead；配置里三者的差异只有 `num_dims`（2/1/0）与 `primitive_mode`（[configs/_base_/models/h3dnet.py:L1-L179](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/h3dnet.py#L1-L179) 三段几乎逐行复制，仅这两处不同）。
- [mmdet3d/models/roi_heads/mask_heads/primitive_head.py:L120-L122](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/roi_heads/mask_heads/primitive_head.py#L120-L122) —— 基元预测头的输出通道公式：`3 + num_dims + num_classes`——3 是基元中心偏移，`num_dims` 是基元语义信息维度（z 面存 x/y 两个方向的尺寸残差、xy 面存 1 个、line 没有尺寸语义），`num_classes` 是语义分类。
- [mmdet3d/models/roi_heads/mask_heads/primitive_head.py:L149-L186](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/roi_heads/mask_heads/primitive_head.py#L149-L186) —— `forward` 四步：① `flag_conv/flag_pred` 给每个 seed 预测「此处是否存在基元中心」（2 通道二分类）；② `VoteModule` 让 seed 投票到基元中心；③ 三种 `sample_mode`（vote/seed/random，训练用 vote、测试用 seed，配置 [L51-L59](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/h3dnet.py#L51-L59)）决定聚合 1024 个基元候选的方式；④ `conv_pred` 出中心/语义并经 `get_primitive_center` 用 flag 分数把「不像基元」的中心推到 `upper_thresh=100` 之外。
- [mmdet3d/models/roi_heads/mask_heads/primitive_head.py:L429-L512](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/roi_heads/mask_heads/primitive_head.py#L429-L512) —— 基元**真值**的构造是 H3DNet 最独特的 target 工程：对每个 gt 框，先拟合上/下水平面（要求水平，否则 `NotImplementedError`），`match_point2plane` 挑出贴面的点；`z` 模式要求贴面点数 > `num_point=100` 且距离方差 < `var_thresh` 才生成面基元目标；`line` 模式用 `match_point2line` 把贴面点归到 4 条底边/顶边。也就是说，**基元监督来自「点云贴在框的哪个面上」这一几何事实**，而非人工标注。
- [mmdet3d/models/roi_heads/bbox_heads/h3d_bbox_head.py:L241-L262](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/roi_heads/bbox_heads/h3d_bbox_head.py#L241-L262) —— 拼合 z/xy 两头的基元中心与特征，然后用 `DepthInstance3DBoxes.get_surface_line_center()` 算出每个 RPN 提案框的 6 面心与 12 边心（该方法在 [mmdet3d/structures/bbox_3d/depth_box3d.py:L233-L280](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/depth_box3d.py#L233-L280)：面心偏移 6 个 ±单位法向 × 尺寸/2，边心是 12 个 ±x±y±z 组合 × 尺寸/2，再按 yaw 旋转回世界系）。
- [mmdet3d/models/roi_heads/bbox_heads/h3d_bbox_head.py:L264-L296](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/roi_heads/bbox_heads/h3d_bbox_head.py#L264-L296) —— 两个 matcher（配置为 `PointSAModule`，`num_point=256*6` 与 `256*12`）：以基元中心集合为搜索空间、提案的面/边心为 `target_xyz`，把「提案附近的基元特征」聚到每个面/边上；随后 `matching_conv/matching_pred` 出**匹配分数**分支——显式地问「这个提案的每个面上有没有对应的基元」。
- [mmdet3d/models/roi_heads/bbox_heads/h3d_bbox_head.py:L298-L318](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/roi_heads/bbox_heads/h3d_bbox_head.py#L298-L318) —— 精修：面/边两路特征各自过一层聚合卷积后 concat，先加回 RPN 的 `original_feature`（残差式精修），再过 `bbox_pred` 卷积塔出最终的 cls/reg，解码键名统一加后缀 `_optimized`。
- [mmdet3d/models/detectors/h3dnet.py:L152-L156](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/h3dnet.py#L152-L156) —— 推理时 `roi_head.predict(..., suffix='_optimized')`：最终结果取精修后的分支；NMS 在 bbox head 内部完成（`multiclass_nms_single`，与 VoteHead 同款套路）。

损失侧的「多头监督」规模值得数一遍：RPN 六项（objectness/center/dir×2/size×2/semantic/vote）+ 每个基元四项（flag/vote/center/size+sem，见 [primitive_head.py:L276-L310](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/roi_heads/mask_heads/primitive_head.py#L276-L310)）× 3 个基元头 + bbox head 的 proposal/optimized 两阶段若干项（`get_proposal_stage_loss` 与 cues 系列损失，配置 [configs/_base_/models/h3dnet.py:L281-L324](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/h3dnet.py#L281-L324)）。这也是为什么 H3DNet 的 log 里损失键名有几十个。

#### 4.4.4 代码实践

**实践目标**：跑通 H3DNet 伪数据测试，并数清损失字典的键。

1. 操作步骤（需 CUDA）：

```bash
python -m pytest tests/test_models/test_detectors/test_h3dnet.py -q
```

2. 无 GPU 替代方案——写一个 10 行脚本打印损失键名结构（**示例代码**，不依赖数据集，只需能构建模型）：

```python
from mmengine import Config
from mmdet3d.registry import MODELS

cfg = Config.fromfile('configs/h3dnet/h3dnet_8xb3_scannet-seg.py')
model = MODELS.build(cfg.model)
# 只看 roi_head 里三个基元头的名字，不前向
print([n for n, _ in model.roi_head.named_children()])
# 预期: ['primitive_z', 'primitive_xy', 'primitive_line', 'bbox_head']
```

3. 需要观察的现象：`roi_head` 的四个子模块；再对照 [tests/test_models/test_detectors/test_h3dnet.py:L44-L46](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tests/test_models/test_detectors/test_h3dnet.py#L44-L46)，测试只断言 RPN 的 `vote_loss/objectness_loss/center_loss` 三项大于 0——如果愿意，可在本地把断言扩到 `flag_loss_z`、`center_loss_line` 等基元损失。

4. 预期结果：`named_children` 输出四个模块名；GPU 下测试通过。待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：H3DNet 的 RPN 为什么不做 NMS（`use_nms=False`）？
**答案**：NMS 会按分数筛掉提案，而第二阶段的精修可能救活低分提案（基元匹配提供的几何证据比 RPN 的语义分更可靠）。保留全部 256 个提案、把去重推迟到精修之后，是两阶段检测器的通行做法（对照 2D 的 Faster R-CNN）。

**练习 2**：为什么三个 PrimitiveHead 的监督用 `ChamferDistance` 而 RPN 的 center 损失也用 Chamfer，不用 SmoothL1？
**答案**：基元中心是无序集合（1024 个 z 面候选对 2N 个真值面心），预测与真值之间没有现成的一一对应，Chamfer 距离（每个真值找最近预测 + 每个预测找最近真值）天然处理集合匹配。SmoothL1 只适用于已经配好对的两组同长向量——u6-l2 讲 VoteNet 用 Chamfer 匹配是同一逻辑的「分配」版本，这里是「损失」版本。

**练习 3**：`primitive_mode='z'` 的 `num_dims=2`、`'xy'` 的 `num_dims=1` 分别存什么？
**答案**：`z` 面基元（水平面）在面内有两个展布方向（x/y），所以语义残差 2 维，存面沿 x、y 的尺寸残差（`size_residuals_z`，见 [primitive_head.py:L694-L696](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/roi_heads/mask_heads/primitive_head.py#L694-L696)）；`xy` 面基元（竖直面）法向确定后只有一个展布方向（高度方向之外的那个），语义残差 1 维；`line` 是一维线段，尺寸语义没有意义，`num_dims=0`，解码时干脆不产出 `size_residuals`。

## 5. 综合实践

把本讲主实践补完整——**三种 head 的 get_targets 与结构对比表**。请先自己填空，再对照参考答案。

| 维度 | VoteNet（H3DNet 的 RPN） | GroupFree3D | 3DSSD |
| --- | --- | --- | --- |
| 候选点来源 | 投票点 FPS 聚合（`sample_mode='vote'`） | seed 的 objectness top-K（kps）或 D-FPS（fps） | F-FPS 前景 seed 生成投票点，`spec` 模式以投票点为中心聚合 |
| 分组方式 | PointSAModule 球查询分组投票点 | 无分组（transformer cross-attn 全局交互） | PointSAModuleMSG 以投票点为 `target_xyz`、seed 为搜索空间 |
| 正负样本判定 | Chamfer 最近邻：中心距 <0.3 正、>0.6 负 | 点在框内（`points_in_boxes_part`/instance mask）+ seed→candidate 两级查表 | 点在框内（`points_in_boxes_all`）+ 距框顶面中心 <10 m |
| objectness 形式 | 二值（CrossEntropy） | 二值（FocalLoss）+ 采样级 `sampling_objectness_loss` | 连续 centerness（训练加权，推理不用） |
| 分类分支数 | objectness + semantic 两通道组 | (num_classes+1) 一个 cls 头 ×(1+L) 层 | 仅 num_classes 语义通道（objectness 由 sem max 代） |
| 回归分支 | bin 分类 + 残差（类相关） | bin 分类 + 残差（类相关/类无关可切换） | 半尺寸直接回归 + bin 朝向 + 角点 |
| 解码器类型 | 无（一次前向） | L 层 BaseTransformerLayer（self/cross/FFN）逐层出框 | 无（一次前向） |
| 验证用 config | `configs/h3dnet/h3dnet_8xb3_scannet-seg.py` | `configs/groupfree3d/groupfree3d_head-L6-O256_4xb8_scannet-seg.py` | `configs/3dssd/3dssd_4xb4_kitti-3d-car.py` |

**操作步骤**：

1. 分别对三个 config 跑 `python tools/misc/print_config.py <config>`，定位 `model.bbox_head`（或 `rpn_head`）块。
2. 核对表中每一行：例如「分类分支数」一行，在展开配置里数 `_get_cls_out_channels` 对应的输出通道（GroupFree3D 是 `num_classes+1=19`，3DSSD 是 `num_classes=1`）。
3. 打开三个 `get_targets*` 函数，核对「正负样本判定」一行与 4.2.3 的引用行号。
4. **预期结果**：表中所有单元格都能在源码或展开配置中找到出处；若发现出入，以源码为准修正表格。

进阶（可选）：把 H3DNet 的 `H3DBboxHead` 也加一列——它的输入是 RPN 提案而非采样点，「候选点来源」一栏应填「RPN 的 256 个提案 + 其 6 面心/12 边心」。

## 6. 本讲小结

- **GroupFree3D** 用「候选点 + L 层 Transformer 解码器」替代「投票 + 分组」：query 来自候选特征、key/value 来自全部种子点，两级 `ConvBNPositionalEncoding`（框 6 维 / 点 3 维）注入位置；每层配独立预测头、逐层深监督，推理可选 `last/last_three/all` 层输出。
- **室内检测器的正负样本分配不是 IoU**：GroupFree3D 先在原始点上算「点属于哪个框」再经 seed→candidate 两级 `torch.gather` 查表；3DSSD 直接在聚合点上判定并叠加 `pos_distance_thr` 距离约束；VoteNet/H3DNet-RPN 用 Chamfer 0.3/0.6。
- **3DSSD** 的三个辅助分支各司其职：F-FPS 保证前景覆盖、vote loss 监督候选点回归、centerness 以连续中心度加权训练、corner loss 把朝向分类剥离后约束角点；推理时 objectness 直接取语义分数最大值。
- **H3DNet** 是「VoteNet RPN + 基元 RoI」的两阶段结构：三个 PrimitiveHead（z 面/xy 面/line 边）从「点贴在框哪个面」这一几何事实构造监督；RoI 阶段以提案的 6 面心/12 边心为 `target_xyz` 匹配聚合基元特征，残差式精修出 `_optimized` 框。
- 三者共享同一套工程模板：薄壳检测器 + 厚 head、`gravity_center` 口径、bin-based 编码族（`PartialBinBasedBBoxCoder` 三个子类）、`get_targets` 的「空场景造假框」开场。

## 7. 下一步学习建议

本讲之后，室内点云检测主线（VoteNet → GroupFree3D/3DSSD/H3DNet）已经闭环。建议两个方向：

1. **两阶段室外检测器**：阅读 `mmdet3d/models/detectors/parta2.py` 与 `mmdet3d/models/roi_heads/pv_rcnn_roi_head.py`（u6-l4 将精读），对比「RoI 特征来自体素 + 关键点」与 H3DNet「RoI 特征来自几何基元」的差别——两阶段思想相同，中间表示完全不同。
2. **换个视角读本讲代码**：带着 u4-l1 的注册器知识，把三个 config 各自的 `type` 字符串逐个 grep 到类定义，画出「config → 类 → 继承链」三栏对照图；再阅读 `mmdet3d/testing/compose_data.py`（测试伪数据工厂），理解 `create_detector_inputs` 如何凭空造出带 `DepthInstance3DBoxes` 的样本——这是你为自定义检测器写单测的模板。
