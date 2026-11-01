# 深监督、辅助损失与损失权重流转

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `weight_dict` 在 `MaskFormer.from_config` 中如何由 3 个键展开成 30 个键（含 `_0`..`_8` 后缀的辅助损失键）。
2. 解释 `DEC_LAYERS` 为什么要减 1：learnable query features 的第 0 组预测也受监督，使「监督组数」恒等于 `DEC_LAYERS`。
3. 掌握 `SetCriterion.forward` 的 aux 分支：每一组辅助预测都独立地重新做一次匈牙利匹配，而不是沿用最后一层的匹配结果。
4. 理解 `MaskFormer.forward` 训练分支的「加权 + 过滤」两步：`weight_dict` 既是系数表，也是白名单——不在其中的损失键会被直接丢弃。

本讲是单元五的收尾：u5-l1 讲了「怎么匹配」，u5-l2 讲了「怎么算单项损失」，本讲把这三方（匹配器、损失项、权重）串回训练循环，回答「一次前向产生的 30 个 loss 数字是怎么走到日志里的」。

## 2. 前置知识

- **深监督（deep supervision）**：在多个中间层都接预测头并计算损失，而不是只监督网络最后一层。对 Transformer 解码器而言，每一层输出的 query 表示都可以立刻预测一组 (类别, mask)，如果只监督最后一层，前面各层的梯度通路会变长、训练更难收敛。
- **辅助损失（auxiliary loss）**：深监督的工程载体。模型把中间层的预测打包成 `aux_outputs` 列表随主输出一起返回，损失函数逐组计算，键名加 `_i` 后缀以便区分。
- **本仓库已建立的认知**（本讲直接承接，不再重复展开）：
  - u4-l3/u4-l4：解码器在「进入第 1 层之前」就用 learnable `query_feat` 过了一次预测头，产生第 0 组预测；随后每层末尾再预测一次；`_set_aux_loss` 把除最后一组外的中间预测打包。
  - u5-l1：匈牙利匹配解决「Q 个 query 对 M 个 GT」的一一分配；匹配代价权重与损失权重共用同一组 `CLASS/MASK/DICE_WEIGHT`。
  - u5-l2：`SetCriterion` 先匹配、后监督，返回的损失是**未加权**的原始值。
- **detectron2 训练循环的约定**：模型（meta architecture）在训练模式下返回一个 `dict[str, Tensor]`，宿主框架 `SimpleTrainer.run_step`（位于 detectron2 仓库的 `detectron2/engine/train_loop.py`，不在本仓库内）对字典所有值求和得到总损失并反传。因此「哪些损失参与求和、各乘多大系数」必须由模型自己在上游决定——这正是本讲的 `weight_dict` 机制。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [mask2former/maskformer_model.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py) | 顶层 meta arch：`from_config` 中构造 weight_dict 并注入 criterion；`forward` 训练分支做加权与过滤 |
| [mask2former/modeling/criterion.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/criterion.py) | `SetCriterion.forward`：主输出匹配 + aux 分支逐组重匹配，产出带 `_i` 后缀的未加权损失 |
| [mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py) | 解码器侧：`dec_layers = DEC_LAYERS - 1`、预测组的生产线、`_set_aux_loss` 打包 |
| [mask2former/config.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/config.py) | 损失权重与 `DEC_LAYERS` 的默认值 |
| [configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml) | COCO R50 基线实际取值：权重 2.0/5.0/5.0、`DEC_LAYERS: 10` |

## 4. 核心概念与源码讲解

### 4.1 weight_dict 的构造：3 个键展开成 30 个键

#### 4.1.1 概念说明

`weight_dict` 是一个「损失键名 → 系数」的字典。它有三个身份：

1. **系数表**：每个损失项最终要乘的权重（如 `loss_mask: 5.0`）。
2. **白名单**：只有出现在其中的键才能活着离开 `MaskFormer.forward`，其余被 `pop` 丢弃（见 4.4）。
3. **匹配代价的影子**：它的三个基础值同时被传给匈牙利匹配器作为 `cost_class/cost_mask/cost_dice`（u5-l1 已讲），即「匹配时觉得什么重要，监督时就给什么加权」。

#### 4.1.2 核心流程

```text
读 cfg 三项权重 (CLASS/MASK/DICE_WEIGHT)
        │
        ▼
基础 weight_dict = {loss_ce: w_c, loss_mask: w_m, loss_dice: w_d}   ← 3 个键
        │
        ▼  若 DEEP_SUPERVISION = True
for i in range(DEC_LAYERS - 1):        # COCO 基线: DEC_LAYERS=10 → i = 0..8
    每个基础键复制一份，加后缀 f"_{i}"
        │
        ▼
weight_dict = 3 个主键 + 3 × 9 = 27 个 aux 键 = 30 个键
```

即 COCO R50 基线下，训练日志里每个 iteration 应出现 30 个以 `loss_` 开头的数字——这正好解释了 u2-l1 观察到的「30 个 loss 键」。

#### 4.1.3 源码精读

读取损失参数并构建匹配器（权重三元组同时喂给匹配器）：

[mask2former/maskformer_model.py:L101-L116](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L101-L116) —— 从 `cfg.MODEL.MASK_FORMER` 读出 `DEEP_SUPERVISION`、`NO_OBJECT_WEIGHT` 与三个损失权重，并用同一组权重实例化 `HungarianMatcher`（匹配代价 = 损失系数）。

weight_dict 的三键底座与深监督展开，本讲最核心的 8 行：

[mask2former/maskformer_model.py:L118-L125](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L118-L125) —— 先构造 `{"loss_ce": class_weight, "loss_mask": mask_weight, "loss_dice": dice_weight}`；`deep_supervision` 为真时，`for i in range(dec_layers - 1)` 把每个键克隆出 `k + f"_{i}"` 的副本，最后 `weight_dict.update(aux_weight_dict)` 合并。注意后缀从 `_0` 到 `_8`（`dec_layers - 1 = 9` 个），与 4.3 中 criterion 的后缀严格对齐。

把 weight_dict 注入 criterion：

[mask2former/maskformer_model.py:L127-L138](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L127-L138) —— `losses = ["labels", "masks"]` 声明要计算的损失类别；`SetCriterion(num_classes, matcher, weight_dict, eos_coef=no_object_weight, ...)` 把权重表连同匹配器一起交给 criterion 保存（criterion 存它但不在 `forward` 里用它，见 4.4.1）。

配置侧的默认值与基线实际值：

- [mask2former/config.py:L32-L37](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/config.py#L32-L37) —— 默认 `DEEP_SUPERVISION = True`、`CLASS_WEIGHT = 1.0`、`DICE_WEIGHT = 1.0`、`MASK_WEIGHT = 20.0`（这组默认值对应 ADE20K 语义分割系配置）。
- [maskformer2_R50_bs16_50ep.yaml:L22-L26](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml#L22-L26) —— COCO 全景基线覆写为 `DEEP_SUPERVISION: True`、`CLASS_WEIGHT: 2.0`、`MASK_WEIGHT: 5.0`、`DICE_WEIGHT: 5.0`、`NO_OBJECT_WEIGHT: 0.1`。

#### 4.1.4 代码实践

**实践目标**：不依赖任何深度学习库，纯 Python 复现 `from_config` 的展开逻辑，验证「3 键 → 30 键」与后缀规律。

1. 把下面脚本保存为独立文件并运行（示例代码，逻辑逐行取自上述源码）：

```python
# 示例代码：复现 maskformer_model.py L118-L125 的展开逻辑
DEC_LAYERS = 10            # maskformer2_R50_bs16_50ep.yaml L36
CLASS_WEIGHT, MASK_WEIGHT, DICE_WEIGHT = 2.0, 5.0, 5.0   # 同 yaml L24-L26
deep_supervision = True

weight_dict = {"loss_ce": CLASS_WEIGHT, "loss_mask": MASK_WEIGHT, "loss_dice": DICE_WEIGHT}
if deep_supervision:
    aux_weight_dict = {}
    for i in range(DEC_LAYERS - 1):
        aux_weight_dict.update({k + f"_{i}": v for k, v in weight_dict.items()})
    weight_dict.update(aux_weight_dict)

print(len(weight_dict))                    # 预期 30
print(sorted(k for k in weight_dict if k.endswith("_0")))  # 预期 ['loss_ce_0', 'loss_dice_0', 'loss_mask_0']
print(sorted(k for k in weight_dict if k.endswith("_8")))  # 预期 ['loss_ce_8', 'loss_dice_8', 'loss_mask_8']
```

2. 把 `DEC_LAYERS` 改成 6（`config.py` 的默认值）再跑一次，观察键数变为 `3 + 3×5 = 18`。
3. 把 `deep_supervision` 改成 `False`，确认只剩 3 个主键——并记住这个状态对训练的实际影响（见 4.4.3 的分析）。

**需要观察的现象**：键名后缀恰好是 `_0` 到 `_8`，没有 `_9`；主键（无后缀）与 aux 键并存。

**预期结果**：`30`；`DEC_LAYERS=6` 时 `18`；`deep_supervision=False` 时 `3`。此脚本纯标准库，可直接运行验证。

#### 4.1.5 小练习与答案

**练习 1**：如果想让每层辅助损失用不同的权重（例如越早的层权重越小），需要改动哪里？

**答案**：改 `from_config` 的展开循环——把固定的 `v` 换成随 `i` 衰减的系数（如 `v * 0.9 ** i`）。由于 criterion 只按后缀产键、`MaskFormer.forward` 只按键查 `weight_dict`，只要键名不变，改系数不需要动 criterion 与解码器。

**练习 2**：为什么 `loss_ce` 的系数取 `CLASS_WEIGHT`，而不是单独一个 `CE_WEIGHT` 配置？

**答案**：因为匹配代价与损失系数刻意共用同一三元组（`cost_class=class_weight`，见 L111-L116）。分类代价在匹配中衡量「query 与 GT 类别差多远」，损失系数衡量「分类错多疼」，共用一个值等价于「按同一把尺子先配对、再惩罚」，少一个超参也避免两者失衡。

### 4.2 DEC_LAYERS 为何减 1：learnable query 的第 0 组监督

#### 4.2.1 概念说明

v1（MaskFormer）的解码器 query 是静态可学习向量，直接作为第 1 层的输入，预测组数 = 层数。v2（Mask2Former）把 query 拆成**内容嵌入 `query_feat`** 与**位置嵌入 `query_embed`** 两个可学习向量（u4-l3），并且**在进入任何解码层之前**就先让 `query_feat` 过一遍预测头。于是：

\[
\text{预测组数} = \text{解码层数} + 1
\]

为了让「监督组数」（含 aux）仍等于配置里的 `DEC_LAYERS`，构建解码器时取层数为 `DEC_LAYERS - 1`。源码注释与 yaml 注释都明说了这一点。

#### 4.2.2 核心流程

以 COCO 基线 `DEC_LAYERS = 10` 为例（构建 `num_layers = 9`）：

```text
query_feat (Q,B,C) ──预测头──► 预测组 0          → aux_outputs[0] → 后缀 _0
  │
  ├─ 解码层 1 (cross→self→FFN) ──预测头──► 预测组 1 → aux_outputs[1] → 后缀 _1
  ├─ 解码层 2 ──预测头──► 预测组 2               → 后缀 _2
  │   ……
  ├─ 解码层 8 ──预测头──► 预测组 8               → aux_outputs[8] → 后缀 _8
  └─ 解码层 9 ──预测头──► 预测组 9               → 升格为主输出 pred_logits / pred_masks（无后缀）
```

共 10 组预测 = 9 组 aux + 1 组主输出，与 weight_dict 的 `3 + 27 = 30` 键严丝合缝。

#### 4.2.3 源码精读

构建层数时减一，注释原文点明动机：

[mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py:L349-L355](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L349-L355) —— 注释写明：因为引入了需要监督的 learnable query features，解码层数减 1，使「辅助损失数恒等于解码层数；有了 learnable query features 后，辅助损失数等于层数加 1」，故 `ret["dec_layers"] = cfg.MODEL.MASK_FORMER.DEC_LAYERS - 1`，并断言 `DEC_LAYERS >= 1`。

配置文件同一口径的注释：

[maskformer2_R50_bs16_50ep.yaml:L36](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml#L36) —— `DEC_LAYERS: 10  # 9 decoder layers, add one for the loss on learnable query`：10 = 9 个真实解码层 + 1 个「作用在 query_feat 上的第 0 组监督」。

预测组的生产线（第 0 组与循环内各组）：

- [mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py:L384-L394](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L384-L394) —— 取出 `query_embed`（位置）与 `query_feat`（内容），**在进入循环前**先对 `query_feat` 调 `forward_prediction_heads`，得到第 0 组 `(outputs_class, outputs_mask)` 存入列表。
- [mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py:L396-L420](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L396-L420) —— `for i in range(self.num_layers)`：每层 cross→self→FFN 之后立刻再过预测头并 append 一组预测。

数量断言与输出组装：

[mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py:L422-L431](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L422-L431) —— 断言 `len(predictions_class) == self.num_layers + 1`；`out` 字典中最后一组升格为 `pred_logits` / `pred_masks`，其余交给 `_set_aux_loss` 打包成 `aux_outputs`。

`_set_aux_loss` 只做「去尾」：

[mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py:L450-L461](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L450-L461) —— `zip(outputs_class[:-1], outputs_seg_masks[:-1])` 把前 9 组逐组组成 `{"pred_logits": a, "pred_masks": b}` 的小字典列表；`mask_classification=False` 时（per-pixel 基线用）只保留 mask。

#### 4.2.4 代码实践

**实践目标**：验证「预测组数 = DEC_LAYERS、aux 组数 = DEC_LAYERS − 1」的计数关系。

1. 纯阅读版（无需环境）：对照上面源码数一遍——`predictions_class` 的 append 语句共出现 2 处（L393 循环外、L419 循环内），循环 `num_layers = 9` 次，故总组数 = 1 + 9 = 10，aux = 10 − 1 = 9。
2. 运行验证版（需按 u1-l2 装好环境并编译 MSDeformAttn 扩展；**待本地验证**）：

```python
# 示例代码
import torch
from detectron2.config import get_cfg
from detectron2.projects.deeplab import add_deeplab_config
from detectron2.modeling import build_model
from mask2former import add_maskformer2_config

cfg = get_cfg()
add_deeplab_config(cfg)
add_maskformer2_config(cfg)
cfg.merge_from_file("configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml")
cfg.MODEL.DEVICE = "cpu"
cfg.freeze()

model = build_model(cfg)          # 训练态即默认态
decoder = model.sem_seg_head.predictor
print(decoder.num_layers)         # 预期 9（= DEC_LAYERS 10 − 1）

feat = {"res2": torch.randn(1, 256, 32, 32), "res3": torch.randn(1, 256, 16, 16),
        "res4": torch.randn(1, 256, 8, 8),  "res5": torch.randn(1, 512, 4, 4)}
with torch.no_grad():
    out = model.sem_seg_head(feat)
print(len(out["aux_outputs"]))    # 预期 9
print(set(out["aux_outputs"][0].keys()))  # 预期 {'pred_logits', 'pred_masks'}
```

**需要观察的现象**：`num_layers = 9`；`aux_outputs` 长度 9；每个 aux 元素只有 `pred_logits` 与 `pred_masks` 两个键。

**预期结果**：如上；若报找不到 `MultiScaleDeformableAttention` 扩展，回到 u1-l2 执行 `mask2former/modeling/pixel_decoder/ops` 下的 `sh make.sh`。

#### 4.2.5 小练习与答案

**练习 1**：把 `DEC_LAYERS` 改成 11 而不动其他代码，weight_dict 会有几个键？解码器实际几层？

**答案**：weight_dict 有 `3 + 3×10 = 33` 个键（后缀 `_0`..`_9`）；解码器构建 `11 − 1 = 10` 层；训练日志相应出现 33 个 loss 数字。

**练习 2**：如果删除 `from_config` 里的 `- 1`（即层数 = DEC_LAYERS），会发生什么不一致？

**答案**：解码器会产出 `DEC_LAYERS + 1 = 11` 组预测、10 组 aux（后缀 `_0`..`_9`），而 weight_dict 只展开到 `_8`。于是后缀 `_9` 的 3 个损失在 `MaskFormer.forward` 中不在 `weight_dict` 里而被 pop 丢弃——不会报错，但第 9 组 aux 的监督被静默吞掉，层数与监督预算不符（这正是白名单机制的「宽容」面，见 4.4）。

**练习 3**：第 0 组预测的输入是什么？它收到梯度后更新了哪些参数？

**答案**：输入是未经任何解码层处理的 `query_feat`（内容嵌入）。梯度回传更新预测头（`class_embed`、`mask_embed`）、`query_feat` 本身，以及 mask 通路上的 pixel decoder 与 backbone——因此第 0 组监督同时起到了「直接塑造 query 初始化」的作用，这是 v2 引入 learnable query features 的配套设计。

### 4.3 SetCriterion.forward 的 aux 分支：逐层重新匹配

#### 4.3.1 概念说明

深监督有一个关键选择：中间层的预测**要不要重新做匹配**？本仓库的答案是**每组独立重匹配**。理由：每层预测的 mask 都在变化，上一层的最优分配对下一层未必仍最优；且逐组重匹配保持每组损失都是「当前预测与 GT 的最优配对下的损失」，与主输出语义一致。代价是匹配要跑 `DEC_LAYERS` 次，但匹配器全程 `@torch.no_grad()` 且只用 12544 个采样点（u5-l1），开销可控。

#### 4.3.2 核心流程

`SetCriterion.forward(outputs, targets)` 的完整步骤：

```text
1. 剥离 aux：outputs_without_aux = outputs 去掉 "aux_outputs"
2. 主匹配：indices = matcher(outputs_without_aux, targets)
3. 统计归一化分母：num_masks = 批内 GT 总数（跨卡 all_reduce 取平均，clamp ≥ 1）
   —— 只依赖 targets，与哪一组预测无关，故只算一次、全组共享
4. 主损失：for loss in ["labels", "masks"]: 得到 {loss_ce, loss_mask, loss_dice}
5. aux 分支：if "aux_outputs" in outputs:
       for i, aux_outputs in enumerate(outputs["aux_outputs"]):
           indices = matcher(aux_outputs, targets)      # ★ 每组重新匹配
           for loss in self.losses:
               键名加后缀 f"_{i}" 后并入 losses
6. 返回未加权的合并 dict（30 个键）
```

用公式表达总损失的结构（加权在 4.4 完成）：

\[
\mathcal{L}_{\text{total}} \;=\; \sum_{t=0}^{T-1} \; \sum_{c \,\in\, \{\mathrm{ce},\,\mathrm{mask},\,\mathrm{dice}\}} w_c \; \ell_c\!\left(\hat{Y}^{(t)},\; \sigma^{(t)}(Y)\right), \qquad T = \text{DEC\_LAYERS} = 10
\]

其中 \(\sigma^{(t)}\) 是第 \(t\) 组预测各自的匈牙利匹配（共 10 次独立求解），\(w_c\) 对所有组相同。

#### 4.3.3 源码精读

主输出匹配与归一化分母：

[mask2former/modeling/criterion.py:L219-L231](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/criterion.py#L219-L231) —— 先用字典推导剥掉 `aux_outputs` 得到主输出再做匹配（避免把列表喂进匹配器）；`num_masks` 只统计 targets 中的 GT 数并做跨卡平均，与预测组无关。

主损失计算：

[mask2former/modeling/criterion.py:L233-L236](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/criterion.py#L233-L236) —— 按 `self.losses = ["labels", "masks"]` 经 `get_loss` 分发到 `loss_labels` / `loss_masks`（u5-l2 精读过的两个函数），产出无后缀三键。

aux 分支，本模块核心：

[mask2former/modeling/criterion.py:L238-L245](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/criterion.py#L238-L245) —— 只要 outputs 里有 `aux_outputs` 就逐组循环：**每组先 `self.matcher(aux_outputs, targets)` 重新匹配**（L241），再对该组算同样的两类损失，并用 `l_dict = {k + f"_{i}": v for k, v in l_dict.items()}` 加后缀（L244）。后缀 `i` 的取值范围由解码器 `_set_aux_loss` 的「去尾」决定，为 `0..DEC_LAYERS-2`，与 4.1 的 `_0.._8` 一一对应。

补充一个细节：`weight_dict` 在 `__init__` 里只是被存下（[criterion.py:L110](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/criterion.py#L110)），`forward` 全程不读它——加权留给上层（这是本讲标题「权重流转」的第一半：权重的**定义**在 from_config，**使用**在 MaskFormer.forward）。

#### 4.3.4 代码实践

**实践目标**：用 spy 包装统计一次 criterion 调用中匹配器被调用的次数与各次的输入来源，验证「每组独立重匹配」。

（需已构建模型，接 4.2.4 的脚本环境；**待本地验证**）

```python
# 示例代码：接 4.2.4，model 已建好，batch=2、每图至少 1 个 GT
from detectron2.structures import Instances
import torch

H = W = 320
inputs = []
for _ in range(2):
    inst = Instances((H, W))
    inst.gt_classes = torch.tensor([3, 17])
    inst.gt_masks = torch.rand(2, H, W) > 0.7        # bool 张量即可
    inputs.append({"image": torch.randint(0, 255, (3, H, W), dtype=torch.uint8),
                   "instances": inst})

model.train()
targets = model.prepare_targets([x["instances"] for x in inputs],
                                __import__("detectron2.structures", fromlist=["ImageList"]).ImageList.from_tensors(
                                    [(x["image"] - model.pixel_mean) / model.pixel_std for x in inputs],
                                    model.size_divisibility))
images_norm = __import__("detectron2.structures", fromlist=["ImageList"]).ImageList.from_tensors(
    [(x["image"] - model.pixel_mean) / model.pixel_std for x in inputs], model.size_divisibility)
outputs = model.sem_seg_head(model.backbone(images_norm.tensor))

matcher = model.criterion.matcher
orig, log = matcher.forward, []
def spy(outputs_dict, targets_list):
    tag = "aux" if "aux_outputs" not in outputs_dict else "main"
    log.append(tag)
    return orig(outputs_dict, targets_list)
matcher.forward = spy

raw_losses = model.criterion(outputs, targets)
print(len(log), log.count("main"), log.count("aux"))   # 预期 10 1 9
print(len(raw_losses))                                 # 预期 30
```

说明：criterion 先对剥离 aux 的主输出调一次匹配（记 `main`），再对每组 aux 各调一次（记 `aux`）；spy 里用「入参字典是否含 `aux_outputs` 键」区分这两类——主匹配的输入是剥离后的字典，不含该键。

**需要观察的现象**：匹配器共被调用 10 次（1 次主 + 9 次 aux）；`raw_losses` 恰 30 个键且全部为有限值。

**预期结果**：`10 1 9` 与 `30`。每张图 GT 数不少于 1（匹配器与 scipy 的线性分配对空 GT 不友好，构造玩具数据时需保证）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `num_masks` 不放进 aux 循环里逐组重算？

**答案**：`num_masks` 只依赖 targets（每图 GT 数），与用哪一组预测计算损失无关；所有组共享同一个分母既省去重复的跨卡通信，也保证各组损失量纲一致（u5-l2 讲过「按批内 mask 总数归一化」的三重收益对每组同样成立）。

**练习 2**：如果某组 aux 的预测与主输出的匹配结果不同，损失还能比较吗？

**答案**：能，而且本来就该不同。每组损失衡量的是「该组预测在它自己的最优配对下」与 GT 的差距；深监督逐组比较的正是各自最优配对下的损失，这与集合预测「对排列不变」的语义一致（u5-l1）。

### 4.4 MaskFormer.forward 的损失加权：weight_dict 作为白名单

#### 4.4.1 概念说明

回看权重的完整流转路径：

```text
cfg (yaml + 默认值)
  └─► MaskFormer.from_config：展开成 weight_dict ──┬─► 注入 HungarianMatcher（作匹配代价，u5-l1）
                                                    └─► 存入 SetCriterion（仅保存/打印，不参与计算）
criterion(outputs, targets) ──► 未加权的 30 键损失 dict
  └─► MaskFormer.forward 训练分支：
        for k in list(losses.keys()):
            k 在 weight_dict 中 → losses[k] *= weight_dict[k]
            否则                → losses.pop(k)          # 白名单过滤
  └─► detectron2 SimpleTrainer：sum(losses.values()) 反传
```

分工很清晰：**criterion 负责「算」，meta arch 负责「选与乘」**。这样 criterion 保持纯函数性质（换权重不用改它），而「哪些损失进入总损失」只有一个决策点。

#### 4.4.2 核心流程

训练分支（`self.training == True`）：

```text
batched_inputs ──► 归一化 + ImageList padding ──► backbone ──► sem_seg_head ──► outputs（含 aux_outputs）
prepare_targets: Instances(gt_classes, gt_masks) ──► [{"labels", "masks"}, ...]
losses = criterion(outputs, targets)                 # 30 个未加权键
遍历 losses 键：在 weight_dict 内则乘系数，不在则删除
return losses                                        # 交给 detectron2 求和
```

#### 4.4.3 源码精读

训练分支入口与 criterion 调用：

[mask2former/maskformer_model.py:L200-L209](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L200-L209) —— 训练态下若 batch 首元素含 `"instances"` 则经 `prepare_targets`（u3-l1：GT mask 左上角对齐 zero-pad）构造 targets，随后 `losses = self.criterion(outputs, targets)`。

加权与过滤，本讲收口的关键 7 行：

[mask2former/maskformer_model.py:L211-L217](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L211-L217) —— `for k in list(losses.keys())`（先物化键列表，避免边遍历边改字典）：键在 `self.criterion.weight_dict` 中就乘上系数；否则 `losses.pop(k)` 直接删除该损失。**这就是「过滤未知损失键」的机制**：任何 criterion 新产出而 weight_dict 未登记的损失都会被静默丢弃而非报错。

由这段代码可以推出一个容易误解的事实——`DEEP_SUPERVISION = False` 的真实语义是「算了但丢弃」，而不是「不算」：

- 解码器 `forward` **无条件**产出 `aux_outputs`（[mask2former_transformer_decoder.py:L424-L431](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L424-L431)）；
- criterion **无条件**处理存在的 aux（[criterion.py:L238-L245](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/criterion.py#L238-L245)，`if "aux_outputs" in outputs` 不看任何开关）；
- `DEEP_SUPERVISION` 只控制 from_config 是否把 aux 键写进 weight_dict（[maskformer_model.py:L120-L125](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L120-L125)）。

所以关掉它省不掉 aux 的前向与损失计算，只是让这 27 个键在 L211-L216 被逐个 pop——梯度层面等价于没有深监督，计算层面照付。想真正省算力，需要改解码器或 criterion。

#### 4.4.4 代码实践

**实践目标**：完成规格要求的核对——跑一次真实前向 + criterion，验证模型返回 dict 的键与 weight_dict 一一对应，并解释 DEC_LAYERS 减 1。

（需按 u1-l2 完成安装与算子编译；**待本地验证**）

```python
# 示例代码：接 4.2.4/4.3.4 的环境与变量（model、inputs 已就绪）
model.train()

# ① 模型口径：一次前向直接返回加权后的损失
returned = model(inputs)

# ② 手动口径：分步执行 criterion 再手动加权，与 ① 对照
wd = model.criterion.weight_dict
manual = {k: v * wd[k] for k, v in raw_losses.items() if k in wd}

print(len(returned), len(wd))                     # 预期 30 30
print(set(returned.keys()) == set(wd.keys()))     # 预期 True（键完全一致）
print(all(torch.allclose(returned[k], manual[k], atol=1e-5) for k in returned))  # 预期 True
print(returned["loss_mask"], returned["loss_mask_8"])   # loss_mask_8 乘的是同一个 5.0
```

1. 运行并核对：`set(returned.keys()) == set(wd.keys())` 为 `True`——「返回键 ⊆ weight_dict 键」与「weight_dict 键 ⊆ 返回键」同时成立，说明两边都不多不少。
2. 再做对照实验：`cfg.MODEL.MASK_FORMER.DEEP_SUPERVISION = False`（在 `merge_from_file` 之后、`freeze` 之前设置）重建模型，重复①，预期返回 dict 只剩 `loss_ce / loss_mask / loss_dice` 3 个键（27 个 aux 键被 pop），但解码器输出的 `aux_outputs` 依旧长度 9——亲手验证 4.4.3 的结论。
3. 用一句话回答「DEC_LAYERS 为何减 1」：v2 的 learnable `query_feat` 在进入解码层前就产生一组受监督的预测（第 0 组），预测组数 = 层数 + 1；构建时取 `DEC_LAYERS − 1` 层，恰好让监督组数（aux 9 组 + 主输出 1 组）等于 `DEC_LAYERS = 10`，与 from_config 展开的 `_0.._8` 后缀及 yaml 注释 "9 decoder layers, add one for the loss on learnable query" 完全一致。

**需要观察的现象**：两个口径数值逐键一致；`DEEP_SUPERVISION=False` 时返回 3 键但 `aux_outputs` 仍为 9 组。

**预期结果**：如上；若数值因随机权重不同而异属正常，比较的是「两口径相等性」而非具体数值。

#### 4.4.5 小练习与答案

**练习 1**：为什么遍历用 `for k in list(losses.keys())` 而不是直接 `for k in losses.keys()`？

**答案**：循环体内执行了 `losses.pop(k)`，会改变字典大小；在 Python 中边遍历边增删字典键会抛 `RuntimeError`。先 `list(...)` 物化一份键快照即可安全删除。

**练习 2**：假设你给 `SetCriterion` 新增了一个 `loss_bbox` 损失但忘了登记权重，会发生什么？如何让它在训练中生效？

**答案**：criterion 会正常算出该键，但 `MaskFormer.forward` 发现它不在 `weight_dict` 中，会把它 pop 掉——训练不报错、日志无此项、梯度为 0，属于「静默失败」。要生效需在 `from_config` 的 weight_dict（及深监督展开之外）登记 `loss_bbox` 的系数。这提示改损失时的检查清单：losses 列表 → 损失函数 → weight_dict 三处都要对齐。

**练习 3**：训练日志中 `loss_mask_0` 与 `loss_mask`（无后缀）分别监督哪一组预测？

**答案**：`loss_mask_0` 监督第 0 组——由 learnable `query_feat` 未经任何解码层直接预测产生；`loss_mask` 监督最后一组——第 9 个解码层之后的预测，也就是推理时真正使用的那组输出（`pred_masks`）。

## 5. 综合实践

**任务：绘制并验证「一次训练 iteration 的损失链路」。**

1. **画链路图**：拿一张纸，从 `maskformer2_R50_bs16_50ep.yaml` 的 `DEC_LAYERS: 10` 出发，画出从「3 个权重配置 → from_config 展开 30 键 → 解码器产出 10 组预测 → criterion 10 次匹配 + 30 个未加权损失 → forward 加权过滤 → detectron2 求和」的完整数据流，在每条边上标注键名或数量。
2. **算一侧**：运行 4.1.4 的纯 Python 脚本（无需 GPU），确认 30 键与 `_0.._8` 后缀。
3. **验一侧**（待本地验证）：依次执行 4.2.4（aux 长度 9）、4.3.4（匹配 10 次）、4.4.4（两口径 30 键逐键相等），把每步实测数字标注回第 1 步的图上。
4. **断言收尾**：在你自己的脚本末尾加上三条断言，作为对本讲全部结论的可执行总结：

```python
# 示例代码
assert decoder.num_layers == 10 - 1                     # DEC_LAYERS 减 1
assert len(out["aux_outputs"]) == 10 - 1 and log.count("main") == 1 and log.count("aux") == 9
assert set(model(inputs).keys()) == set(wd.keys()) and len(wd) == 30
```

若三条全部通过，说明你已能把「配置 → 结构 → 损失 → 权重」四层对齐着读，这正是调试「损失键消失 / 数值不对」类问题的基本功。

## 6. 本讲小结

- `weight_dict` 在 `MaskFormer.from_config` 中由 3 个基础键（`loss_ce/loss_mask/loss_dice` × `CLASS/MASK/DICE_WEIGHT`）经 `for i in range(DEC_LAYERS - 1)` 展开成 3 + 27 = 30 个键，后缀 `_0.._8`；同一组权重还兼任匈牙利匹配的代价系数。
- v2 把 query 拆成 `query_feat`（内容）与 `query_embed`（位置），`query_feat` 在进入解码层前就产出受监督的第 0 组预测，故解码器构建 `DEC_LAYERS − 1` 层、预测组数恒为 `DEC_LAYERS`——源码注释与 yaml 注释 "9 decoder layers, add one for the loss on learnable query" 双重印证。
- `SetCriterion.forward` 对主输出与每组 aux **各自独立重新匹配**（共 DEC_LAYERS 次），键名加 `_i` 后缀；`num_masks` 只依赖 targets，全组共享。
- `MaskFormer.forward` 训练分支对 criterion 返回的未加权损失做「在白名单内则乘系数、否则 pop」两步，是损失进入 detectron2 求和前的唯一关卡；未登记的新损失会被静默丢弃。
- `DEEP_SUPERVISION = False` 的真实语义是「算了但被 pop 丢弃」：解码器与 criterion 均无条件处理 aux，开关只影响 weight_dict 的展开。
- 分工总结：**criterion 管「算」，from_config 管「定义权重」，MaskFormer.forward 管「选与乘」**，三者通过键名约定松耦合。

## 7. 下一步学习建议

本讲结束了单元五「训练监督」。接下来两个方向：

1. **单元六（推荐下一讲 u6-l1 语义分割推理分支）**：看 `MaskFormer.forward` 的另一半——`self.training == False` 时的 eval 分支，主输出 `pred_logits/pred_masks` 如何被上采样并分发到语义/实例/全景三条推理路径（aux_outputs 在推理时被完全忽略，正好与本讲呼应）。
2. **延伸阅读**：
   - [mask2former/modeling/matcher.py:L95-L130](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/matcher.py#L95-L130) 对照本讲再看一遍匹配器入口，体会「匹配权重 = 损失权重」的设计在两处源码中的镜像。
   - [mask2former_video/modeling/criterion.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/modeling/criterion.py) 视频版 `VideoSetCriterion`：同样的「匹配 + 深监督」骨架，`num_masks` 按视频归一化、mask 损失不再用逐点采样，可在学完单元九时回来对比。
