# u6-l1 语义分割推理分支

> 前置讲义：u3-l1（MaskFormer 顶层 Meta Architecture）、u4-l4（预测头与 Attention Mask 生成）。

## 1. 本讲目标

学完本讲，你应该能够：

1. 跟踪 `MaskFormer.forward` 在 eval 模式下的完整数据流：从 `sem_seg_head` 的原始输出，到 mask 上采样，再到逐图后处理。
2. 精读 `semantic_inference`，理解「softmax 去掉 no-object 通道 + sigmoid mask + einsum 融合」如何把 \( Q \) 个 per-mask 预测变成 \( K \) 张 per-class 概率图。
3. 说清 `sem_seg_postprocess_before_inference` 这个开关在「先上采样再推理」与「先推理再上采样」之间的内存取舍，以及它为什么在打开全景/实例分支时被强制为 True。
4. 掌握 `retry_if_cuda_oom` 的三段式降级策略（直接执行 → 清缓存重试 → 输入搬去 CPU 重试），并能数出它在语义推理路径上的全部调用点。

## 2. 前置知识

### 2.1 mask classification 范式回顾

传统语义分割是 **per-pixel classification**：每个像素直接输出一个 \( K \) 维分类结果。Mask2Former 采用 **mask classification**：模型输出 \( Q \) 个 query，每个 query 给出两样东西：

- 一组 \( K+1 \) 维的分类 logits（\( K \) 个真实类别 + 1 个「no-object / 无对象」通道）；
- 一张 mask logits 图，表示这个 query「负责」哪些像素。

语义分割结果需要把这两者重新组合回「每个像素、每个类别一个概率」，这正是本讲 `semantic_inference` 干的事。

### 2.2 三种「尺寸」不要混淆

推理链路里同一张图有三种尺寸，后面会反复用到：

| 名称 | 记号 | 含义 | 来源 |
| --- | --- | --- | --- |
| 原图尺寸 | \( (H_o, W_o) \) | 数据集中的原始图片大小 | `batched_inputs[i]["height"]/["width"]` |
| 网络输入尺寸 | \( (H_i, W_i) \) | mapper/预测器缩放后、padding 前的大小 | `images.image_sizes[i]` |
| padding 后尺寸 | \( (H_p, W_p) \) | 向上取整到 `size_divisibility`（默认 32）倍数后批量化的大小 | `images.tensor.shape[-2:]` |

### 2.3 需要的 PyTorch 基础

- `F.interpolate(..., mode="bilinear", align_corners=False)`：双线性插值 resize；
- `F.softmax(x, dim=-1)`：沿最后一维归一化成概率分布；
- `torch.einsum("qc,qhw->chw", A, B)`：爱因斯坦求和，等价于「对 q 维做加权求和」；
- `argmax`：语义分割评估/可视化前，把每像素的 \( K \) 个概率压成一个类别 id。

### 2.4 no-object 通道

训练时大多数 query 会被匹配到「无对象」（详见 u5 系列讲义），所以分类 logits 的最后一维是 no-object。`mask2former/maskformer_model.py` 中 `self.sem_seg_head.num_classes` 就是 \( K \)，no-object 永远排在最后一个通道（下文 4.2 会看到它在 `panoptic_inference` 里同样以 `num_classes` 下标出现）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [mask2former/maskformer_model.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py) | 本讲主文件：`MaskFormer.forward` 的 eval 分支、`semantic_inference`，以及三个推理分支的开关分发 |
| [mask2former/config.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/config.py) | 注册 `MODEL.MASK_FORMER.TEST.*` 全部推理开关与默认值 |
| [mask2former_video/utils/memory.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/utils/memory.py) | `retry_if_cuda_oom` 的仓库内副本（图像模型实际用的是 detectron2 里的同一份实现，见 4.4） |
| [demo/predictor.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/demo/predictor.py) | 展示 `sem_seg` 结果的下游用法：`argmax(dim=0)` 后交给可视化器 |
| [configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml) | COCO 全景配置：`NUM_CLASSES: 133`，三个推理分支全开 |
| [configs/ade20k/semantic-segmentation/maskformer2_R50_bs16_160k.yaml](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/ade20k/semantic-segmentation/maskformer2_R50_bs16_160k.yaml) | ADE20K 语义配置：只开 `SEMANTIC_ON` |
| detectron2 的 `detectron2/modeling/postprocessing.py`（外部依赖，不在本仓库内） | 提供 `sem_seg_postprocess`，由 [mask2former/maskformer_model.py:12](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L12) 导入；文件见 [detectron2/modeling/postprocessing.py](https://github.com/facebookresearch/detectron2/blob/main/detectron2/modeling/postprocessing.py)（行号随 detectron2 版本变化，**待确认**） |

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：

1. eval 分支总体流程：mask 上采样与逐图后处理；
2. `semantic_inference`：per-mask 概率融合成 per-class 概率图；
3. `sem_seg_postprocess` 与「先/后处理」的取舍；
4. `retry_if_cuda_oom`：显存不足的三段式兜底。

### 4.1 eval 分支总体流程：从模型输出到三种结果

#### 4.1.1 概念说明

训练时 `forward` 返回损失字典（u3-l1 已讲）；推理时（`self.training == False`）则要把 `sem_seg_head` 的原始输出变成用户可用的结果。`sem_seg_head` 返回的 `outputs` 字典里，本分支只关心两个键：

- `pred_logits`：形状 \( (B, Q, K+1) \) 的分类 logits；
- `pred_masks`：形状 \( (B, Q, H_p/4, W_p/4) \) 的 mask logits——注意它挂在 1/4 分辨率的 `mask_features` 上（u4-l4 讲过 einsum 生成方式），**比网络输入还小 4 倍**。

eval 分支要解决三个问题：把 mask 放大到输入尺寸；对 batch 里的每张图，把 padding/缩放还原回原图尺寸；再按 `semantic_on / panoptic_on / instance_on` 三个开关分发到对应推理函数。

#### 4.1.2 核心流程

```text
outputs = sem_seg_head(features)                # pred_logits (B,Q,K+1), pred_masks (B,Q,Hp/4,Wp/4)
├─ mask_pred_results = interpolate(pred_masks)  # 一次性上采样到 (B,Q,Hp,Wp)，双线性
├─ del outputs                                  # 提前释放，省显存
└─ for 每张图 i in batch:
     ├─ height, width ← batched_inputs[i] 的原图尺寸（缺省用网络输入尺寸）
     ├─ 若 sem_seg_postprocess_before_inference:
     │    ├─ mask_pred = sem_seg_postprocess(mask_pred, image_size_i, H_o, W_o)   # 先还原到原图
     │    └─ mask_cls  = mask_cls.to(mask_pred)                                   # 设备/精度对齐
     ├─ semantic_on  → r["sem_seg"]     = semantic_inference(mask_cls, mask_pred)
     │                  （若 before_inference 为 False，这里再补一次 sem_seg_postprocess）
     ├─ panoptic_on  → r["panoptic_seg"] = panoptic_inference(mask_cls, mask_pred)
     └─ instance_on  → r["instances"]    = instance_inference(mask_cls, mask_pred)
```

要点：mask 上采样只做**一次**（batch 级），三种推理分支共享同一对 `mask_cls_result / mask_pred_result`；每个分支调用都被 `retry_if_cuda_oom` 包裹。

#### 4.1.3 源码精读

先看 import：语义分支依赖 detectron2 的后处理函数和 OOM 重试装饰器。

- [mask2former/maskformer_model.py:12-14](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L12-L14)：导入 `sem_seg_postprocess` 与 `retry_if_cuda_oom`，本讲的两个配角在这里登场。

eval 分支从 `else` 开始：

- [mask2former/maskformer_model.py:218-227](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L218-L227)：取出 `pred_logits` / `pred_masks`，用 `F.interpolate` 把 mask 从 1/4 分辨率双线性上采样到 padding 后的网络输入尺寸 \( (H_p, W_p) \)，`align_corners=False`。注意这是对整个 batch 一次完成的。

- [mask2former/maskformer_model.py:229-236](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L229-L236)：`del outputs` 释放原字典；随后逐图循环，`height/width` 优先取数据集 dict 里的原始尺寸，取不到就用 `images.image_sizes` 的网络输入尺寸兜底。

- [mask2former/maskformer_model.py:239-243](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L239-L243)：`sem_seg_postprocess_before_inference` 为 True 时，先把 mask 从网络输入尺寸还原到原图尺寸（`sem_seg_postprocess` 内部：裁掉 padding → 插值到 \( (H_o, W_o) \)，见 4.3），再把 `mask_cls_result` 搬到与 mask 相同的设备/精度上——这一行是为了配合 `retry_if_cuda_oom` 可能把 mask 计算挪到 CPU 的情况（见 4.4）。

- [mask2former/maskformer_model.py:245-250](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L245-L250)：语义分支本体。`semantic_inference` 的输出如果还没还原到原图尺寸（即 before_inference 为 False 的情况），就在这里补一次 `sem_seg_postprocess`，最终写入结果字典的 `"sem_seg"` 键。

- [mask2former/maskformer_model.py:252-260](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L252-L260)：全景与实例分支，与语义分支并列，复用同一对输入，分别是 u6-l3、u6-l2 的主角。本讲只需记住它们的存在解释了 4.3 中那个强制开关。

三个开关的默认值在配置里：

- [mask2former/config.py:53-60](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/config.py#L53-L60)：`SEMANTIC_ON` 默认 True，`INSTANCE_ON` / `PANOPTIC_ON` 默认 False，另有本讲会用到的 `SEM_SEG_POSTPROCESSING_BEFORE_INFERENCE`（默认 False）。

两个真实配置作对照：

- [configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml:8](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml#L8)：`NUM_CLASSES: 133`（COCO 全景 = 80 thing + 53 stuff），因此该模型的 `mask_cls` 是 \( (Q, 134) \)。
- [configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml:41-43](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml#L41-L43)：三个分支全开。
- [configs/ade20k/semantic-segmentation/maskformer2_R50_bs16_160k.yaml:39-44](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/ade20k/semantic-segmentation/maskformer2_R50_bs16_160k.yaml#L39-L44)：只开 `SEMANTIC_ON`（`NUM_CLASSES: 150` 见 [第 7 行](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/ade20k/semantic-segmentation/maskformer2_R50_bs16_160k.yaml#L7)）。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：不运行任何代码，把 eval 分支中每个张量的尺寸来源填进下表，做到能「白板复述」。

2. **操作步骤**：
   - 打开 [mask2former/maskformer_model.py:218-262](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L218-L262)；
   - 对下表每一行，写出张量形状（用 \( B, Q, K, H_p, W_p, H_i, W_i, H_o, W_o \) 表示）：

     | 位置 | 张量 | 形状 |
     | --- | --- | --- |
     | L219 | `mask_cls_results` | 待填 |
     | L220（interpolate 前） | `mask_pred_results` | 待填 |
     | L222-227（interpolate 后） | `mask_pred_results` | 待填 |
     | L240 之后（before=True 时） | `mask_pred_result`（单图） | 待填 |
     | L247 | `semantic_inference` 的输出 | 待填 |

3. **需要观察的现象**：上采样发生在批级（循环外），而尺寸还原发生在图级（循环内）；`del outputs` 的位置在两者之间。

4. **预期结果**（自查答案）：依次为 \( (B,Q,K{+}1) \)、\( (B,Q,H_p/4,W_p/4) \)、\( (B,Q,H_p,W_p) \)、\( (Q,H_o,W_o) \)、before=True 时 \( (K,H_o,W_o) \)，before=False 时 \( (K,H_p,W_p) \) 再被 L249 还原成 \( (K,H_o,W_o) \)。若想跑真实模型验证，可按 u1-l4 的 demo 流程对 ADE20K 语义配置推理并打印 `predictions["sem_seg"].shape`（**待本地验证**，需要环境与权重）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `F.interpolate` 放在循环外、`sem_seg_postprocess` 放在循环内？

**答案**：interpolate 的目标尺寸 `images.tensor.shape[-2:]` 对整个 batch 相同（ImageList 已 padding 对齐），可以一次性处理；而 `sem_seg_postprocess` 的目标是**每张图各自的** \( (H_i, W_i) \to (H_o, W_o) \)，batch 内各图不同，只能在逐图循环里做。

**练习 2**：结果字典里 `"sem_seg"` 存的是概率图还是类别 id 图？

**答案**：概率图（soft prediction），形状 \( (K, H_o, W_o) \)。类别 id 图是下游用 `argmax(dim=0)` 得到的，例如 [demo/predictor.py:59-62](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/demo/predictor.py#L59-L62) 里 `predictions["sem_seg"].argmax(dim=0)` 后才交给 `draw_sem_seg` 可视化。

### 4.2 `semantic_inference`：per-mask 概率融合成 per-class 概率图

#### 4.2.1 概念说明

模型给的是「每个 query 一个类别分布 + 一张 mask」，语义分割要的是「每个类别一张概率图」。桥是这样一个思想：**类别 c 在像素 p 上的概率 ≈ 所有 query 认为自己是 c 的概率，按各自 mask 在 p 处的概率加权求和**。直觉上：一个像素属于「汽车」，当且仅当有一些 query 高置信地宣称自己是汽车、且它们的 mask 都覆盖这个像素。

用符号表示，设分类 logits \( C \in \mathbb{R}^{Q\times(K+1)} \)，mask logits \( M \in \mathbb{R}^{Q\times H\times W} \)：

\[
P = \mathrm{softmax}(C,\ \mathrm{dim}=-1)\big[\dots,\ :-1\big] \in \mathbb{R}^{Q\times K}
\]
\[
S = \sigma(M) \in \mathbb{R}^{Q\times H\times W}
\]
\[
\mathrm{semseg}_{c,h,w} \;=\; \sum_{q=1}^{Q} P_{q,c}\, S_{q,h,w}
\]

最后一步正是 `torch.einsum("qc,qhw->chw", ...)`。两点值得注意：

- softmax 先在 \( K+1 \) 维（含 no-object）上做，**再**切掉最后一列。这保证「query 是 c 的概率」是以「自己可能是任何类或无对象」为对照集算出来的，与训练时的交叉熵目标一致（u5 系列）。
- 融合结果不是归一化分布：某像素上所有类之和为 \( \sum_q S_{q,h,w}\,(1-P_{q,\text{no-object}}) \)，通常远小于 1（多数 query 的概率质量在 no-object 上）。所以下游只做 `argmax`，不把逐类概率当成联合分布使用。

#### 4.2.2 核心流程

```text
mask_cls  (Q, K+1)  --softmax(dim=-1)--> (Q, K+1) --[..., :-1]--> (Q, K)   # 丢 no-object
mask_pred (Q, H, W) --sigmoid-->         (Q, H, W)
einsum("qc,qhw->chw")  -->  semseg (K, H, W)
```

三行代码，一次矩阵乘「厚度」的加权求和，没有 argmax、没有阈值——那些是评估器/可视化器的职责。

#### 4.2.3 源码精读

- [mask2former/maskformer_model.py:280-284](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L280-L284)：`semantic_inference` 全部三行：softmax 去尾、sigmoid、einsum。注意它的 `self` 完全没被用到——这意味着它可以原样抽成一个纯函数（4.2.4 的实践正是这么做的）。

对比同文件里另外两个分支对同一份 `mask_cls` 的不同用法，能加深理解：

- [mask2former/maskformer_model.py:287-290](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L287-L290)：`panoptic_inference` 用 `softmax(...).max(-1)` 给每个 query 选一个类，且用 `labels.ne(self.sem_seg_head.num_classes)` 把 no-object 的 query 直接过滤掉——印证了「no-object 就是下标 \( K \)」。
- [mask2former/maskformer_model.py:349-352](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L349-L352)：`instance_inference` 同样 `softmax(...)[:, :-1]` 后在 \( Q\times K \) 个「query-类别」对上取 topk。

三个分支共用「softmax（含 no-object）」这一步，差别只在之后怎么用：语义分支**全部加权求和**，全景分支**每 query 取 argmax**，实例分支**全局 topk**。

#### 4.2.4 代码实践

1. **实践目标**：用玩具输入验证 `semantic_inference` 的形状契约：`mask_cls (100, 134)`、`mask_pred (100, h, w)` 进，`(133, h, w)` 出。只需 PyTorch，不需要 detectron2。

2. **操作步骤**：新建 `toy_semantic_inference.py`（**示例代码**，逐行复刻 [mask2former/maskformer_model.py:280-284](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L280-L284)）：

   ```python
   # 示例代码：mask2former semantic_inference 的纯 torch 复刻
   import torch
   import torch.nn.functional as F

   def semantic_inference(mask_cls, mask_pred):
       mask_cls = F.softmax(mask_cls, dim=-1)[..., :-1]
       mask_pred = mask_pred.sigmoid()
       semseg = torch.einsum("qc,qhw->chw", mask_cls, mask_pred)
       return semseg

   torch.manual_seed(0)
   Q, K, h, w = 100, 133, 128, 128          # COCO 全景：NUM_CLASSES=133
   mask_cls = torch.randn(Q, K + 1)          # (100, 134)
   mask_pred = torch.randn(Q, h, w)          # (100, 128, 128)

   semseg = semantic_inference(mask_cls, mask_pred)
   print(semseg.shape)                       # 期望 torch.Size([133, 128, 128])
   print(semseg.min().item(), semseg.max().item(), semseg.mean().item())
   print(semseg.sum(dim=0).mean().item())    # 每像素所有类概率之和的均值
   ```

3. **需要观察的现象**：输出形状；数值范围在 \( (0, 1) \) 内；随机输入下 `semseg.mean()` 明显不为 0（数量级在 0.3 上下），且每像素的概率和约为几十——远大于 1。

4. **预期结果**：形状确为 `(133, 128, 128)`。数值上：随机 logits 的 softmax 接近均匀分布，每个 query 把约 \( \frac{133}{134} \) 的质量平摊给 133 个类（每类约 \( 1/134 \)），乘上均值 0.5 的 sigmoid mask，故每个格点约 \( 100 \times \frac{1}{134} \times 0.5 \approx 0.37 \)，每像素类别求和约 \( 100 \times \frac{133}{134} \times 0.5 \approx 50 \)。而**训练好的模型**里大多数 query 的概率质量集中在 no-object，`semseg` 会稀疏得多、值也小得多——这正是 4.2.1 说「不是归一化分布」的原因。具体数值随随机种子不同，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：把 `[..., :-1]` 改成先切片再 softmax（即 `F.softmax(mask_cls[..., :-1], dim=-1)`），语义上会差在哪里？

**答案**：先切片再 softmax 会让 no-object 的竞争者消失，每个 query 的 \( K \) 个类被强行归一化成和为 1 的分布，等于无条件相信「这个 query 一定是某个类」。原实现保留了 no-object 的竞争，未被选中的 query 对语义图的贡献自然被压低，与训练目标（含 no-object 的交叉熵，u5 系列）一致。

**练习 2**：`einsum("qc,qhw->chw", A, B)` 等价于下面哪个循环？

```python
# (a)
out = torch.zeros(K, h, w)
for q in range(Q):
    out += A[q].view(-1, 1, 1) * B[q]
# (b)
out = torch.zeros(K, h, w)
for c in range(K):
    out[c] = A[:, c] @ B.view(Q, -1).view(Q, h, w)  # 伪代码
```

**答案**：(a)。einsum 对 \( q \) 求和，每个 query 贡献一个「标量（该类概率）× 该 query 的 mask 图」。(b) 意图相同但写法不合法（`@` 与 view 维度不匹配）；正确逐类写法是 `out[c] = (A[:, c].unsqueeze(-1) * B).sum(0)`，两者数值一致。

**练习 3**：为什么这里用 `sigmoid` 而 `mask_cls` 用 `softmax`？

**答案**：分类端是「互斥选择 + 允许弃权」（K+1 选 1，配 softmax/交叉熵）；mask 端是每个 query 独立的「像素是否属于我」（逐像素二分类，配 sigmoid/BCE）。两种归一化方式分别匹配两端的监督形式，这也是 mask classification 范式的标准做法。

### 4.3 `sem_seg_postprocess` 与「先/后处理」的取舍

#### 4.3.1 概念说明

`sem_seg_postprocess(result, img_sizes, output_height, output_width)` 是 detectron2 的通用后处理：把网络分辨率下的 `result` \( (C,H,W) \) 先裁掉 padding（裁到 `img_sizes` 即 \( (H_i, W_i) \)），再双线性插值到 \( (H_o, W_o) \)（实现见 [detectron2/modeling/postprocessing.py](https://github.com/facebookresearch/detectron2/blob/main/detectron2/modeling/postprocessing.py)，行号随 detectron2 版本变化，**待确认**）。

本仓库的巧妙之处在于：这个函数**可以发生在 einsum 之前（作用于 Q 张 mask），也可以发生在 einsum 之后（作用于 K 张类别图）**，由 `sem_seg_postprocess_before_inference` 开关控制。两条路径算术上近似等价（双线性插值是线性算子，与求和可交换到浮点误差的程度），区别全在**显存**：

| 路径 | einsum 发生在 | 高分辨率下额外物化的张量 | 适用场景 |
| --- | --- | --- | --- |
| before=True | 原图尺寸 \( (H_o,W_o) \) | \( Q \times H_o \times W_o \)（逐 query 高清 mask） | 需要原图尺寸的 mask 给全景/实例分支用 |
| before=False | 网络尺寸 \( (H_p,W_p) \) | 无（einsum 在低分辨率完成，只需插值最终 \( K \) 张图） | 纯语义分割、超高清原图 |

当 \( H_o \times W_o \) 很大（如 Mapillary 动辄数千万像素）时，\( Q=100 \) 张高清 mask 本身就是数 GB 级分配，before=True 会直接 OOM——这正是源码 docstring 里的警告。

#### 4.3.2 核心流程

```text
配置读取（from_config）:
  before = SEM_SEG_POSTPROCESSING_BEFORE_INFERENCE or PANOPTIC_ON or INSTANCE_ON
                     ↑ 默认 False（config.py）   ↑ 任一打开则强制 True

__init__ 的约束:
  not semantic_on  ⟹  assert before        # 不输出语义结果时必须先还原 mask

forward eval:
  before=True : sem_seg_postprocess(Q 张 mask) → 原图尺寸 → semantic_inference
  before=False: semantic_inference(网络尺寸) → sem_seg_postprocess(K 张类别图)
```

#### 4.3.3 源码精读

- [mask2former/maskformer_model.py:60-63](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L60-L63)：构造参数的 docstring，明确写出取舍：「先还原再推理」对 Mapillary 这类高分辨率数据会 OOM。
- [mask2former/maskformer_model.py:93-94](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L93-L94)：硬约束——`semantic_on` 为 False（只出全景/实例）时必须 `before=True`，因为全景/实例分支消费的是**原图尺寸的 mask**，没有「事后还原类别图」的机会。
- [mask2former/maskformer_model.py:149-153](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L149-L153)：`from_config` 里的布尔或：只要 `PANOPTIC_ON` 或 `INSTANCE_ON` 打开就强制 before=True；纯语义配置则完全听 `SEM_SEG_POSTPROCESSING_BEFORE_INFERENCE` 的。
- [mask2former/config.py:60](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/config.py#L60)：该开关默认 False。仓库内没有任何 yaml 覆盖它（可用 grep 验证），所以 ADE20K/Mapillary 的纯语义配置天然走「先推理后上采样」的省显存路径，COCO 全景配置则因 `PANOPTIC_ON: True`（[maskformer2_R50_bs16_50ep.yaml:41-43](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml#L41-L43)）自动切换到 before=True。
- 两条执行路径分别在 [mask2former/maskformer_model.py:239-243](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L239-L243)（before=True：先还原 mask，再对齐 `mask_cls` 的设备/精度）与 [mask2former/maskformer_model.py:248-249](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L248-L249)（before=False：einsum 之后才还原类别图）。

顺带一提 [mask2former/maskformer_model.py:235-236](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L235-L236)：原图尺寸取自数据集 dict 的 `"height"/"width"`，这是 detectron2 mapper 的约定（`forward` 的 docstring [第 176-177 行](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L176-L177)有说明），缺省回退到网络输入尺寸。

#### 4.3.4 代码实践

1. **实践目标**：亲手比较两条路径的输出差异与分配的中间张量大小，理解「取舍」到底取的是什么。

2. **操作步骤**（**示例代码**，需要已安装 detectron2；未安装时可先做步骤 3 的纸面推演）：

   ```python
   # 示例代码：两条路径对比（COCO 全景的尺寸设定）
   import torch
   from detectron2.modeling.postprocessing import sem_seg_postprocess
   from mask2former.maskformer_model import MaskFormer  # 仅为了用它的 semantic_inference

   torch.manual_seed(0)
   Q, K = 100, 133
   Ho, Wo = 720, 960        # 原图
   Hi, Wi = 512, 683        # 缩放后的网络输入（短边 512）
   Hp, Wp = 512, 704        # padding 到 32 的倍数

   mask_cls = torch.randn(Q, K + 1)
   mask_pred_low = torch.randn(Q, Hp // 4, Wp // 4)               # 模拟 1/4 分辨率输出
   mask_pred = torch.nn.functional.interpolate(
       mask_pred_low, size=(Hp, Wp), mode="bilinear", align_corners=False)  # 复刻 L222-227

   # 路径 A：before=True —— 先还原 mask，再融合
   masks_orig = sem_seg_postprocess(mask_pred, (Hi, Wi), Ho, Wo)  # (Q, Ho, Wo)
   semseg_A = MaskFormer.semantic_inference(None, mask_cls.to(masks_orig), masks_orig)

   # 路径 B：before=False —— 先融合，再还原类别图
   semseg_low = MaskFormer.semantic_inference(None, mask_cls, mask_pred)    # (K, Hp, Wp)
   semseg_B = sem_seg_postprocess(semseg_low, (Hi, Wi), Ho, Wo)             # (K, Ho, Wo)

   print(semseg_A.shape, semseg_B.shape)
   print((semseg_A - semseg_B).abs().max().item())   # 两条路径的数值差
   ```

3. **需要观察的现象**：两条路径输出形状同为 \( (133, 720, 960) \)，但最大绝对误差是**小的非零值**（插值与求和的交换只到浮点/重采样精度，不是严格相等）；路径 A 在 einsum 前额外持有 \( 100 \times 720 \times 960 \) 的 float 张量（约 276 MB），路径 B 的中间结果只有 \( 133 \times 512 \times 704 \)（约 192 MB，且发生在低分辨率阶段）。
4. **预期结果**：`semseg_A` 与 `semseg_B` 逐元素接近但不完全相等；把 `Ho, Wo` 改成 `4000, 6000` 再对比两者的峰值内存（可用 `torch.cuda.max_memory_allocated()`，无 GPU 时纸面计算元素数即可），能直观体会 docstring 里 Mapillary OOM 警告。具体数值**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `PANOPTIC_ON` 或 `INSTANCE_ON` 为 True 时必须 before=True？

**答案**：`panoptic_inference` / `instance_inference` 消费的是 `mask_pred`（逐 query 的 mask）本身，需要它们在原图尺寸上做 argmax、面积统计、topk 筛选（见 [mask2former/maskformer_model.py:286-342](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L286-L342)）；「先融合再还原」只对语义分支的类别图有意义，无法还原出逐 query mask。所以源码在 `from_config` 里用布尔或强制，且 `__init__` 里 `not semantic_on ⟹ assert before` 双保险。

**练习 2**：`sem_seg_postprocess` 为什么要先裁剪（crop 到 `img_sizes`）再插值，而不是直接从 padding 尺寸插值到原图尺寸？

**答案**：padding 区域是 ImageList 为了凑整到 `size_divisibility` 填充的（值通常为 0），并不对应真实图像内容。若不裁掉，插值会把这部分「假像素」混进边缘的真实预测里，原图边缘几个像素的类别概率会被 padding 值污染。

### 4.4 `retry_if_cuda_oom`：显存不足的三段式兜底

#### 4.4.1 概念说明

语义推理的后处理发生在**原图分辨率**上，显存开销与 \( H_o \times W_o \) 成正比，超高清图片随时可能 OOM。与其让整次推理失败，仓库选择把关键步骤包进 `retry_if_cuda_oom(func)`：它把 `func` 变成一个会自动降级重试的函数，策略分三段：

1. **直接执行**——大多数情况就此成功；
2. OOM 则 `torch.cuda.empty_cache()` **清缓存后再试一次**——很多「OOM」只是显存碎片/缓存占用；
3. 仍 OOM 则**把输入搬到 CPU（并转成 float32、关闭 autocast）再算**——慢但能出结果，代价是返回值可能变成 CPU 张量。

使用它的唯一契约：被包裹的函数必须**无状态**（可能被调用多次），输入是扁平的张量参数（不做嵌套结构转换）。

#### 4.4.2 核心流程

```text
wrapped(*args, **kwargs):
    try: return func(*args, **kwargs)                 # 第 1 次
    忽略 CUDA OOM
    torch.cuda.empty_cache()
    try: return func(*args, **kwargs)                 # 第 2 次：清缓存
    忽略 CUDA OOM
    log "Attempting to copy inputs to CPU due to CUDA OOM"
    args/kwargs 中每个 cuda 张量 → cpu + float32
    with autocast(enabled=False):
        return func(cpu_args, cpu_kwargs)             # 第 3 次：CPU 兜底
```

`_ignore_torch_cuda_oom` 上下文管理器通过匹配异常字符串 `"CUDA out of memory. "` 来区分 OOM 与其他 RuntimeError——其他异常照常抛出。

#### 4.4.3 源码精读

图像模型从 detectron2 导入该装饰器（[mask2former/maskformer_model.py:14](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L14)）；仓库内在 [mask2former_video/utils/memory.py:27-80](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/utils/memory.py#L27-L80) 保留了一份完全相同的实现（视频模型经 [mask2former_video/video_maskformer_model.py:19](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/video_maskformer_model.py#L19) 使用它）。以下精读仓库内副本，行为与 detectron2 版一致：

- [mask2former_video/utils/memory.py:12-24](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/utils/memory.py#L12-L24)：`_ignore_torch_cuda_oom` 上下文管理器——只吞 CUDA OOM，其他异常 re-raise。源码注释也坦承异常字符串可能随 PyTorch 版本变化（`NOTE: the string may change?`）。
- [mask2former_video/utils/memory.py:52-60](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/utils/memory.py#L52-L60)：`maybe_to_cpu`——只处理「有 `.device` 且 `.to`」的类张量对象；搬到 CPU 的同时**转 float32**（CPU 上 fp16 算子支持差）。
- [mask2former_video/utils/memory.py:62-78](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/utils/memory.py#L62-L78)：`wrapped` 主体，依次是「直接执行 → 清缓存重试 → 打日志搬 CPU、`autocast(enabled=False)` 重试」三段。注意 docstring（[第 44-48 行](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/utils/memory.py#L44-L48)）明确警告：返回值可能是 CPU 张量，搬回 GPU 是调用方的责任。

语义推理路径上的 5 个调用点全在 eval 分支里（均可用 grep 复核）：

| 行号 | 调用 | 作用 |
| --- | --- | --- |
| [L240](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L240) | `sem_seg_postprocess(mask_pred)` | before=True 时把 Q 张 mask 还原到原图尺寸 |
| [L247](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L247) | `self.semantic_inference(...)` | einsum 融合（可能落在 CPU 上执行） |
| [L249](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L249) | `sem_seg_postprocess(r)` | before=False 时还原 K 张类别图 |
| [L254](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L254) | `self.panoptic_inference(...)` | u6-l3 的主角 |
| [L259](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L259) | `self.instance_inference(...)` | u6-l2 的主角 |

配套细节：[mask2former/maskformer_model.py:243](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L243) 的 `mask_cls_result = mask_cls_result.to(mask_pred_result)` 就是为第 3 段降级准备的——一旦 L240 把 mask 计算挪到了 CPU（并转 float32），分类 logits 必须跟着过去，否则 L247 的 einsum 会因设备不匹配报错。

#### 4.4.4 代码实践

1. **实践目标**：亲眼看到三段降级的触发顺序（有 GPU 时），或能在纸上推演每一段（无 GPU 时）。

2. **操作步骤**（**示例代码**，需要 GPU；无 GPU 时做步骤 3）：

   ```python
   # 示例代码：故意 OOM，观察三段降级
   import logging, torch
   from mask2former_video.utils.memory import retry_if_cuda_oom  # 与图像模型用的 detectron2 版本行为一致

   logging.basicConfig(level=logging.INFO)

   @retry_if_cuda_oom
   def big_einsum(a, b):
       return torch.einsum("qc,qhw->chw", a, b)

   a = torch.randn(100, 134, device="cuda")
   b = torch.randn(100, 8000, 8000, device="cuda")   # 100×8000×8000 ≈ 25.6 GB（按需调大到你卡的两倍以上）
   out = big_einsum(a, b)
   print(out.shape, out.device, out.dtype)
   ```

3. **需要观察的现象**：前两次尝试抛出的 OOM 被静默吞掉；日志出现 `Attempting to copy inputs to CPU due to CUDA OOM`；最终 `out.device` 是 `cpu`、`dtype` 是 `float32`（`maybe_to_cpu` 的功劳）。
4. **预期结果**：形状 `(133, 8000, 8000)`，设备 CPU。注意第 3 段要求 `b` 能整体放进内存；如果连内存也不够会真正抛错。无 GPU 时，请对照 [mask2former_video/utils/memory.py:62-78](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/utils/memory.py#L62-L78) 为三段各写一行注释说明「此时输入在哪个设备、什么精度」，作为纸面推演版本。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：如果给 `retry_if_cuda_oom` 包裹一个**有状态**的函数（例如内部会 `self.count += 1`），会发生什么？

**答案**：函数可能被调用 1~3 次，状态会被重复更新，结果可能错误。这就是 docstring 强调「函数必须无状态」的原因；本仓库包的 `sem_seg_postprocess` / `semantic_inference` 等都是纯计算，天然满足。

**练习 2**：第 3 段为什么要 `autocast(enabled=False)`？

**答案**：走到第 3 段说明 GPU 显存已耗尽，输入被搬到 CPU 并转成 float32。若训练/推理开着混合精度（autast/AMP），CPU 上执行 fp16 算子既慢又可能不被支持，显式关闭 autocast 保证 CPU 回退路径用 float32 稳定计算。

**练习 3**：数一数：`semantic_on=True, panoptic_on=True, instance_on=False` 的配置下，一张图的推理会经过 4.4.3 表格中哪几个 `retry_if_cuda_oom` 调用点？

**答案**：L240（before 必为 True，还原 mask）、L247（语义 einsum）、L254（全景）。L249 不会执行（before=True 时被 `if not ...` 跳过），L259 不执行（`instance_on=False`）。

## 5. 综合实践

**任务：不用训练权重，手工复刻一条「迷你语义分割推理管线」，把三种尺寸、两条后处理路径、einsum 融合与最终类别图全部串起来。**

1. **实践目标**：把 4.1~4.4 的知识连成一条线——给定「假想的原图尺寸与缩放策略」，从随机的 `pred_logits / pred_masks` 出发，产出一张可保存的类别图，并对比两条路径的数值差。

2. **操作步骤**：
   - 设定 \( (H_o, W_o) = (720, 960) \)，按 detectron2 的短边缩放规则（短边 512、长边上限 2048）算出 \( (H_i, W_i) = (512, 683) \)，再按 `size_divisibility=32` 算出 \( (H_p, W_p) = (512, 704) \)；
   - 用 `torch.randn` 造 `mask_cls (100, 134)` 与 1/4 分辨率的 `mask_pred (100, 128, 176)`；
   - 复刻 [L222-227](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L222-L227) 的批级上采样（到 \( (H_p, W_p) \)）；
   - 分别走 4.3.4 的路径 A 与路径 B，各得到 `semseg (133, 720, 960)`；
   - 对两者分别 `argmax(dim=0)` 得到类别 id 图 `(720, 960)`，统计两张 id 图中不同像素的占比；
   - （可选）用 matplotlib/cv2 给 id 图上随机颜色并保存为 PNG。

3. **需要观察的现象**：
   - 两条路径的概率图差是小而非零的（双线性插值与求和的顺序不严格可交换）；
   - 但 `argmax` 之后的 id 图差异极小（通常 <1% 像素，集中在类别概率接近的边界处）；
   - 路径 A 额外物化的 \( Q \times H_o \times W_o \) 张量与路径 B 的中间张量大小之比约等于 \( Q \)（100）——这就是 4.3 取舍的定量形态。

4. **预期结果**：得到两张几乎一致的类别图与一张 PNG；把 \( (H_o, W_o) \) 改成 \( 4000 \times 6000 \) 量级后重复实验，路径 A 的中间分配会膨胀两个数量级，体会 Mapillary 场景为何默认走路径 B。数值结论**待本地验证**（需要 PyTorch；上色部分可选）。

5. **进阶（需要完整环境）**：按 u1-l4 的流程用 ADE20K 语义配置 + Model Zoo 权重跑 `demo/demo.py`，在 [demo/predictor.py:59-62](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/demo/predictor.py#L59-L62) 处打断点/加打印，确认真实 `sem_seg` 形状为 `(150, H_o, W_o)` 且来自路径 B（`INSTANCE_ON/PANOPTIC_ON` 均为 False，见 [configs/ade20k/semantic-segmentation/maskformer2_R50_bs16_160k.yaml:39-44](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/ade20k/semantic-segmentation/maskformer2_R50_bs16_160k.yaml#L39-L44)）。

## 6. 本讲小结

- eval 分支先做一次**批级**双线性上采样把 1/4 分辨率的 `pred_masks` 拉到 padding 后的输入尺寸，再逐图还原到原图尺寸并按 `SEMANTIC_ON / PANOPTIC_ON / INSTANCE_ON` 分发。
- `semantic_inference` 三步融合：`softmax(mask_cls, dim=-1)[..., :-1]`（丢 no-object）→ `mask_pred.sigmoid()` → `einsum("qc,qhw->chw")`，把 \( Q \) 个 per-mask 预测加权求和成 \( K \) 张类别概率图；结果是软预测，argmax 是下游的事。
- `sem_seg_postprocess_before_inference` 决定「还原尺寸」发生在 einsum 前（物化 \( Q \) 张高清 mask，全景/实例必需）还是后（einsum 在低分辨率完成，纯语义省显存，Mapillary 这类高清数据靠它避开 OOM）；`PANOPTIC_ON or INSTANCE_ON` 会强制前者。
- `retry_if_cuda_oom` 用「执行 → 清缓存重试 → 搬 CPU + float32 + 关 autocast 重试」三段式给原图分辨率后处理兜底；语义推理路径上有 L240/L247/L249/L254/L259 共 5 个包裹点。
- 理解「同一份 `mask_cls` 三种用法」：语义分支全量加权求和、全景分支逐 query argmax 过滤 no-object、实例分支全局 topk——这是 mask classification 范式一网打尽三任务的关键。

## 7. 下一步学习建议

- **u6-l2（实例分割推理分支）**：看 `instance_inference` 如何把 \( Q\times K \) 个「query-类别」对展开后 topk，并用「mask 平均概率」重打分——本讲的 softmax 基础直接复用。
- **u6-l3（全景分割推理分支）**：`panoptic_inference` 消费的正是本讲 before=True 路径产出的原图尺寸 mask，两个阈值 `OBJECT_MASK_THRESHOLD / OVERLAP_THRESHOLD` 的过滤逻辑在那里展开。
- **u10-l2（显存优化与 OOM 重试机制）**：从工程视角系统讨论高分辨率推理的内存瓶颈与 `retry_if_cuda_oom` 的更多调用点（包括视频模型把 `F.interpolate` 本身也包裹起来的做法，见 [mask2former_video/video_maskformer_model.py:210](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/video_maskformer_model.py#L210)）。
- 建议回读 [mask2former/maskformer_model.py:167-262](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L167-L262) 的完整 `forward`，做到训练/推理两条路径能在白板上默写。
