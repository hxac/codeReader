# 讲义 u5-l2：SetCriterion：分类损失与逐点 mask 损失

## 1. 本讲目标

学完本讲，你应该能够：

1. 读懂 [mask2former/modeling/criterion.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/criterion.py#L1-L263) 的全部代码：`SetCriterion.forward` 如何「先匹配、后监督」，`loss_labels` 与 `loss_masks` 两个损失函数如何计算。
2. 理解 **K+β 重要性采样**：`get_uncertain_point_coords_with_randomness` 如何用「oversample 候选 + 不确定度 top-k + 随机补齐」三步，把全图百万像素的 mask 比较压缩到 12544 个采样点上。
3. 解释 **为什么损失按「批内 GT mask 总数」而非「像素数」归一化**——这涉及尺度不变性、每个 mask 平等贡献、以及分布式训练下的梯度正确性三个层面。
4. 掌握 **no-object 类别降权**（`empty_weight`）：Q=100 个 query 里通常只有几个匹配到 GT，其余全被贴上 no-object 标签，不平衡比例可达几十比一。
5. 动手用随机预测 + 3 张大小不一的 GT mask 跑通 `loss_masks`，观察 `point_sample` 前后的张量形状，并做采样参数扫描实验。

本讲承接种子讲义 u5-l1（匈牙利匹配器）：匹配器回答「哪个 query 对哪个 GT」，本讲的 `SetCriterion` 是它的**唯一调用方**，负责在匹配结果之上算出真正的损失。

## 2. 前置知识

阅读本讲前，建议你已经理解以下内容（来自前置讲义）：

- **mask classification 范式的输出**（u3-l1、u4-l4）：模型训练分支输出一个 dict，含 `pred_logits`（`(B, Q, K+1)`，Q=100 个 query，K=133 个类别，最后一维是 no-object 通道）和 `pred_masks`（`(B, Q, H/4, W/4)`，**1/4 分辨率**的 mask logits），以及 `aux_outputs`（深监督的各组中间预测，u4-l4 的 `_set_aux_loss` 产出）。
- **匈牙利匹配**（u5-l1）：`HungarianMatcher` 用 class/mask/dice 三项代价对每张图解一个 LSAP，返回 `indices = [(src_idx, tgt_idx), ...]`，长度等于 batch size，每个元素是一对「query 下标 / GT 下标」张量。匹配器内部已经用过一次 `point_sample` 随机采样点近似 mask 代价——本讲会把同一个工具用在正式损失上，但升级为**不确定度引导**的采样。
- **targets 的格式**（u3-l1）：`prepare_targets` 产出的 `targets: list[dict]`，每个 dict 含 `labels`（`(M,)` 连续类别 id）与 `masks`（`(M, H_pad, W_pad)` 二值 GT mask，左上角对齐 zero-pad 到 padding 后的完整输入尺寸）。
- **point_sample**（u5-l1）：给一组归一化坐标（最后一维 `(y, x)`，取值 `[0,1]`），在特征图上做双线性插值取值。**归一化坐标对任意分辨率的图都成立**——这是本讲「预测 1/4 分辨率、GT 全分辨率却能直接比」的关键。

本讲要用的两个基础概念：

- **PointRend**（CVPR 2020，Kirillov 等）：分割中「点」的看法——mask 分割本质是对每个像素做二分类，那么训练时没必要盯住全部像素，盯住「最难分的那些点」性价比最高。detectron2 把它的采样工具收录在 `projects/point_rend/point_features.py`，Mask2Former 直接 import 使用（[mask2former/modeling/criterion.py:L13-L16](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/criterion.py#L13-L16)）。
- **重要性采样（importance sampling）**：想估计「损失在像素上的分布」，与其均匀撒点，不如按「不确定度」倾斜撒点——大部分点放在模型最犹豫的边界附近，留少量点均匀覆盖全局。代价是估计有偏，但训练信号更集中。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [mask2former/modeling/criterion.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/criterion.py#L1-L263) | 本讲主角：`dice_loss` / `sigmoid_ce_loss` / `calculate_uncertainty` / `SetCriterion` 全部在此，共 263 行 |
| [mask2former/utils/misc.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/utils/misc.py#L1-L111) | `nested_tensor_from_tensor_list`（GT mask 批内 padding）与 `is_dist_avail_and_initialized`（分布式判断） |
| [mask2former/maskformer_model.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L96-L161) | `from_config` 构建 `SetCriterion` 并注入 `MaskFormer`；`forward` 的损失加权过滤 |
| [mask2former/config.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/config.py#L106-L114) | 三个 point loss 配置键的默认值注册处 |
| [configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml#L19-L46) | COCO 全景分割实际训练用的损失权重与采样参数 |
| detectron2 `projects/point_rend/point_features.py`（**外部依赖**，不在本仓库） | `get_uncertain_point_coords_with_randomness` 与 `point_sample` 的实现，[GitHub 链接](https://github.com/facebookresearch/detectron2/blob/main/projects/point_rend/point_features.py)，以本地安装的 detectron2 版本源码为准 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：先看 `SetCriterion.forward` 的总调度（含深监督循环与 `num_masks` 归一化），再精读 `loss_labels`（分类损失），然后把 `loss_masks` 拆成上下两场——上半场讲采样点怎么来，下半场讲采样点上的 BCE/Dice 损失与归一化策略。

### 4.1 SetCriterion：先匹配、后监督的总调度（forward）

#### 4.1.1 概念说明

`SetCriterion` 是 DETR 式的「集合预测损失器」。它的类文档字符串写明了两步流程：先在预测与 GT 之间做匈牙利分配，再对每对匹配监督类别与 mask（[mask2former/modeling/criterion.py:L90-L95](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/criterion.py#L90-L95)）。它是 `MaskFormer` 的一个子模块（`self.criterion`），在训练分支被一行调用：

[mask2former/maskformer_model.py:L209-L217](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L209-L217) —— `losses = self.criterion(outputs, targets)` 拿到**未加权**的损失 dict，随后按 `self.criterion.weight_dict` 逐键乘权重；不在 `weight_dict` 里的键直接删掉。所以本讲关注的 `SetCriterion` 返回的是「裸损失」，乘权重发生在 `MaskFormer.forward`（那是 u5-l3 的主题，本讲只交代边界）。

一个容易混淆的点：`SetCriterion` 自己**也持有** `weight_dict`（[mask2former/modeling/criterion.py:L110](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/criterion.py#L110)），但它在 `forward` 里**从不使用**它来加权——它只被 `MaskFormer.forward` 读取用于乘权重与过滤，以及 `__repr__` 里打印（[mask2former/modeling/criterion.py:L254](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/criterion.py#L254)）。真正用于匹配的权重在匹配器里（u5-l1 已讲）。

#### 4.1.2 核心流程

`SetCriterion.forward(outputs, targets)` 的流程可以概括为：

```text
输入: outputs = {pred_logits, pred_masks, aux_outputs?}, targets = list[dict]
1. 剥掉 aux_outputs，用主输出跑一次匹配器 → indices
2. 统计 num_masks = 批内所有图 GT 数之和（跨 GPU all_reduce 后取平均，clamp ≥ 1）
3. 对 self.losses 里每一项（["labels", "masks"]）调用对应损失函数
   → 得到 {"loss_ce":…, "loss_mask":…, "loss_dice":…}
4. 若有 aux_outputs：对每一组中间输出【重新匹配】再算一遍，
   键名加后缀 "_i" → {"loss_ce_0":…, "loss_mask_0":…, …}
返回: 未加权的损失 dict（乘权重在 MaskFormer.forward）
```

几个关键决定值得逐条理解：

**（a）`num_masks` 是跨 GPU 的全局平均。** 代码见 [mask2former/modeling/criterion.py:L224-L231](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/criterion.py#L224-L231)。单机单卡时它就是「批内 GT mask 总数」；多卡时先 `all_reduce` 求和再除以 `get_world_size()`，得到**全局批的平均每卡 GT 数**。`torch.clamp(..., min=1)` 兜底防止某个极端 batch（GT 全空）除零。为什么必须是全局值而不是本卡局部值，4.4 节详细推导。

**（b）深监督 = 逐层「重新匹配 + 重新算损失」。** [mask2former/modeling/criterion.py:L239-L245](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/criterion.py#L239-L245)：对 `aux_outputs` 的每一组，先 `self.matcher(aux_outputs, targets)` 独立匹配，再逐损失计算并加 `_i` 后缀。注意 `num_masks` 只在主输出那里算一次并复用——它是 targets 的属性，与预测无关，逐层不变。DEC_LAYERS=10 时（含 learnable query 的第 0 组），一次前向共 10 次匹配 + 10 组损失，日志里 30 个 loss 键就是这么来的（u2-l1 已经数过）。

**（c）损失项列表只有两项。** `losses = ["labels", "masks"]` 在 `from_config` 里硬编码（[mask2former/maskformer_model.py:L127](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L127)）。`get_loss` 用一个 dict 分发到 `loss_labels` / `loss_masks`，传进未知名字会直接 assert 报错（[mask2former/modeling/criterion.py:L204-L210](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/criterion.py#L204-L210)）。对比 DETR 还有 boxes/boxes_loss 等项——Mask2Former 没有 box 损失，纯靠 mask 监督。

#### 4.1.3 源码精读

**构造：`empty_weight` 与采样参数的注入。** `__init__` 接收 8 个参数，其中 4 个与逐点 mask 损失相关（[mask2former/modeling/criterion.py:L97-L120](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/criterion.py#L97-L120)）：

[mask2former/modeling/criterion.py:L113-L115](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/criterion.py#L113-L115) —— 构造长度为 `num_classes + 1` 的权重向量，前 K 个类别全为 1，最后一项（no-object 通道）设为 `eos_coef`（默认 0.1），注册为 buffer。buffer 意味着它随模型 `.to(device)` 一起搬移，也会出现在 `state_dict` 里（键名 `empty_weight`）——下次在 checkpoint 里看到它不要惊讶。

[mask2former/modeling/criterion.py:L117-L120](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/criterion.py#L117-L120) —— 存下 `num_points / oversample_ratio / importance_sample_ratio` 三个采样超参，4.3 节的主角。

**构建现场：`MaskFormer.from_config`。**

[mask2former/maskformer_model.py:L111-L116](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L111-L116) —— 匹配器复用三个损失权重作为代价权重，并共用 `TRAIN_NUM_POINTS`（u5-l1 已精读）。

[mask2former/maskformer_model.py:L129-L138](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L129-L138) —— `SetCriterion` 的实例化：`num_classes` 来自 `sem_seg_head.num_classes`（COCO 全景为 133），`eos_coef` 来自 `NO_OBJECT_WEIGHT`，三个采样参数来自 `TRAIN_NUM_POINTS / OVERSAMPLE_RATIO / IMPORTANCE_SAMPLE_RATIO`。注意 `eos_coef` 的默认值是 0.1（[mask2former/config.py:L34](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/config.py#L34)），COCO 配置又显式写了一遍 0.1（[configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml:L23](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml#L23)）。

**forward 主体。**

[mask2former/modeling/criterion.py:L212-L247](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/criterion.py#L212-L247) —— 完整 forward。逐段看：

- L219：`outputs_without_aux` 剥掉 `aux_outputs`——主输出的匹配不能被 aux 干扰。
- L222：匹配一次。
- L225-L231：`num_masks` 统计与跨卡平均，`is_dist_avail_and_initialized` 来自 [mask2former/utils/misc.py:L106-L111](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/utils/misc.py#L106-L111)（分布式可用且已初始化才 all_reduce）。
- L234-L236：主损失循环，`losses.update` 把两个损失函数的 dict 合并。
- L239-L245：aux 循环，注意 L241 的重新匹配与 L244 的 `_i` 后缀。

**两个下标工具。** 损失函数要从 `indices` 里还原「批内展平的 (batch_idx, query_idx) / (batch_idx, gt_idx)」：

[mask2former/modeling/criterion.py:L192-L202](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/criterion.py#L192-L202) —— `_get_src_permutation_idx` 把每张图的 `src_idx` 前面拼上图号，`cat` 成一个元组 `(batch_idx, src_idx)`，正好可以直接做 `(B, Q, ...)` 张量的 fancy index；`_get_tgt_permutation_idx` 对 GT 做同样的事。匹配对齐靠这两个函数完成：第 i 张图的第 j 个 query 对上第 i 张图的第 k 个 GT，用同一个 batch_idx 保证不串图。

#### 4.1.4 代码实践

**实践目标**：不跑完整模型，直接构造 `SetCriterion` 并喂随机输入，验证 forward 返回的损失键名与深监督数量公式。

**操作步骤**（示例代码，需按 u1-l2 装好 detectron2 后在仓库根目录运行）：

```python
# practice_5_2_1.py —— 验证 SetCriterion.forward 的键名与数量
import torch
from mask2former.modeling.criterion import SetCriterion
from mask2former.modeling.matcher import HungarianMatcher

torch.manual_seed(0)
K, Q, B = 133, 100, 3

matcher = HungarianMatcher(cost_class=2.0, cost_mask=5.0, cost_dice=5.0, num_points=12544)
criterion = SetCriterion(
    num_classes=K, matcher=matcher,
    weight_dict={"loss_ce": 2.0, "loss_mask": 5.0, "loss_dice": 5.0},
    eos_coef=0.1, losses=["labels", "masks"],
    num_points=12544, oversample_ratio=3.0, importance_sample_ratio=0.75,
)

# 3 张图、每图 2 个 GT，mask 尺寸故意不同（模拟非 LSJ 的批）
sizes = [(200, 300), (256, 256), (320, 180)]
targets = [
    {"labels": torch.randint(0, K, (2,)),
     "masks": (torch.rand(2, h, w) > 0.7).float()}
    for (h, w) in sizes
]

# 手工构造 2 组 aux（模拟 DEC_LAYERS=3：主输出 + 2 组中间输出）
def rnd_outputs():
    return {"pred_logits": torch.randn(B, Q, K + 1),
            "pred_masks": torch.randn(B, Q, 64, 64)}

outputs = rnd_outputs()
outputs["aux_outputs"] = [rnd_outputs() for _ in range(2)]

losses = criterion(outputs, targets)
print(len(losses), "个损失键")
for k in sorted(losses):
    print(f"{k:15s} {losses[k].item():.4f}")
print("state_dict 里能看到:", [k for k in criterion.state_dict() if "empty" in k])
```

**需要观察的现象**：

1. 键名形如 `loss_ce / loss_mask / loss_dice` 加 `loss_ce_0 / loss_mask_0 / loss_dice_0 / loss_ce_1 / ...`。
2. 总键数 = 3（主输出）+ 2 组 aux × 3 = **9 个**，即 \( 3 \times (1 + \text{aux组数}) \)。
3. `state_dict` 中出现 `empty_weight`（buffer 被持久化）。

**预期结果**：9 个损失键，全部为标量张量；数值本身来自随机输入，无参考意义（待本地验证）。真实训练里 DEC_LAYERS=10 时这个数字是 30，与 u2-l1 观察到的日志一致。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `outputs["aux_outputs"]` 设为 `[]`（空列表），forward 返回几个键？训练会有什么变化？

**答案**：返回 3 个键（只有主输出组）。空列表使 L239 的 `if "aux_outputs" in outputs` 为真但循环体不执行。等价于关掉深监督（`DEEP_SUPERVISION: False` 时 `from_config` 不展开 aux 键，`MaskFormer.forward` 的加权过滤还会把多余键删掉）——监督信号只剩最后一层输出，收敛通常更慢。

**练习 2**：`num_masks` 为什么 clamp 到最小 1？如果某卡 batch 里所有图 GT 数为 0 会怎样？

**答案**：GT 全空时 `num_masks` 为 0，`dice_loss` / `sigmoid_ce_loss` 的 `.sum() / num_masks` 会除零产生 NaN/inf。clamp(min=1) 把分母托底为 1；此时匹配器返回空匹配，`loss_masks` 里被选中的 mask 数为 0，`.sum()` 为 0，损失退化为 0/1=0，训练不崩。

**练习 3**：`weight_dict` 在 `SetCriterion` 里存着却不在 `forward` 里用，为什么还要传进来？

**答案**：它被外部（`MaskFormer.forward` L211-L216）经 `self.criterion.weight_dict` 读取，用于逐键乘权重并过滤未知键；同时 `__repr__`（L254）打印它方便日志排查。把配置相关的信息集中在 criterion 对象上，是 DETR 一脉的接口惯例。

### 4.2 loss_labels：no-object 降权的交叉熵

#### 4.2.1 概念说明

分类损失监督的是每个 query 的 `pred_logits`（K+1 维）。匈牙利匹配之后，每个 query 的「正确答案」只有两种可能：

- 被匹配到 GT → 答案是该 GT 的类别 id；
- 未被匹配 → 答案是 no-object（下标 `num_classes`，即 K+1 维里的最后一个通道）。

问题在于比例：Q=100 个 query，一张 COCO 图平均只有七八个 GT 对象，也就是说 **90 多个 query 的标签都是 no-object**。如果一视同仁地算交叉熵，损失会被「压制成背景」这一项主导，模型会学得过度保守——所有 query 都倾向输出 no-object，正类别信号被淹没。

DETR 的解法是给 no-object 类别一个小于 1 的权重 \( w_{\varnothing} \)（即 `eos_coef` / `NO_OBJECT_WEIGHT`，默认 0.1），让「压制背景」的总贡献与「学对前景」量级匹配：

\[ L_{\text{ce}} = -\frac{1}{N}\sum_{q} w_{y_q} \log p_q(y_q), \qquad w_{c} = \begin{cases} \text{eos\_coef}, & c = \varnothing \\ 1, & \text{ otherwise} \end{cases} \]

这正是 `F.cross_entropy(..., weight=...)` 里 `weight` 参数的语义（按类别加权而非按样本加权）。

#### 4.2.2 核心流程

```text
输入: pred_logits (B, Q, K+1), indices (匹配结果), targets[i]["labels"]
1. src_logits = outputs["pred_logits"].float()          # 防 fp16 精度损失
2. 构造 target_classes: 全部初始化为 num_classes（no-object 的下标）
3. 在匹配位置 (batch_idx, src_idx) 上填入对应 GT 的类别 id
4. F.cross_entropy(src_logits.transpose(1,2), target_classes, weight=empty_weight)
输出: {"loss_ce": 标量}
```

注意 `transpose(1, 2)`：`F.cross_entropy` 期望 `(N, C, d_1, ...)`，即类别维在第 2 维；`pred_logits` 是 `(B, Q, K+1)`，转置成 `(B, K+1, Q)` 后 `target_classes` 的 `(B, Q)` 正好逐位置对应。

#### 4.2.3 源码精读

[mask2former/modeling/criterion.py:L122-L138](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/criterion.py#L122-L138) —— `loss_labels` 全文，逐行解读：

- **L127**：`.float()` 把 logits 提到 fp32。混合精度训练（AMP autocast）下交叉熵在 fp16 容易溢出/丢精度，显式升精度是防御性写法（u5-l1 的匹配器里也见过同样的 `autocast(enabled=False)` 处理）。
- **L129-L134**：`_get_src_permutation_idx(indices)` 得到 `(batch_idx, src_idx)`；L130 从每张图 targets 的 `labels` 里按 `J`（GT 下标）挑出被匹配者的类别，`cat` 起来；L131-L133 把 `(B, Q)` 的目标张量整体填成 `self.num_classes`（no-object 下标），再用 fancy index 把匹配位置覆盖成真实类别。**先铺默认、再点覆盖**——这一招把「多数 query 是背景」变成了初始化而非分支判断。
- **L136**：`F.cross_entropy` 的第三个位置参数就是 `weight`（类别权重），传入 `self.empty_weight`。
- **L137**：返回单键 dict。

与配置的对应关系：

| 代码量 | 配置键 | 默认（config.py） | COCO 全景 yaml |
| --- | --- | --- | --- |
| `eos_coef` | `MODEL.MASK_FORMER.NO_OBJECT_WEIGHT` | 0.1（[config.py:L34](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/config.py#L34)） | 0.1（[yaml:L23](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml#L23)） |
| `num_classes` | `SEM_SEG_HEAD.NUM_CLASSES` | （无全局默认，由 yaml 指定） | 133（[yaml:L8](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml#L8)） |
| `loss_ce` 的权重 | `MODEL.MASK_FORMER.CLASS_WEIGHT` | 1.0（[config.py:L35](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/config.py#L35)） | 2.0（[yaml:L24](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml#L24)） |

一个常见疑问：**为什么加权和 CE 内建在 `F.cross_entropy` 里而不是事后除以个数？** 因为 `weight` 模式下 PyTorch 会自动按加权平均归一化（分母是 \(\sum_q w_{y_q}\)），无需手工补偿——no-object 占比变了，损失量级仍然稳定。

#### 4.2.4 代码实践

**实践目标**：用极小的玩具验证 `loss_labels` 的数值行为，特别是 `eos_coef` 对损失的影响方向。

**操作步骤**（示例代码）：

```python
# practice_5_2_2.py —— eos_coef 对 loss_ce 的影响
import torch
import torch.nn.functional as F

torch.manual_seed(0)
K, Q = 5, 10
# 让 logits 对类别 0 略有偏好（模拟"模型把一切都猜成前景"的病态）
logits = torch.zeros(1, Q, K + 1)
logits[:, :, 0] = 3.0

# 假设匹配结果：只有 query 3 对上类别 2 的 GT，其余 9 个全是 no-object
target = torch.full((1, Q), K, dtype=torch.long)
target[0, 3] = 2

for eos in [0.1, 0.5, 1.0]:
    w = torch.ones(K + 1); w[-1] = eos
    loss = F.cross_entropy(logits.transpose(1, 2), target, w)
    print(f"eos_coef={eos}: loss_ce={loss.item():.4f}")
```

**需要观察的现象**：`eos_coef` 从 0.1 调到 1.0 时 loss 单调变化；`eos_coef=1.0`（不降权）时损失显著更大，因为 9 个 no-object 位置的预测（logit=0，即概率约 1/(K+1)）都被全权重惩罚。

**预期结果**：三个数字递增（`eos_coef` 越大，背景项罚得越狠）。具体数值待本地验证；可再试试把 `logits` 全置 0（完全不确定），观察三种 `eos_coef` 下 loss 是否都接近 \(\log(K+1)\) 的某个加权重整。

#### 4.2.5 小练习与答案

**练习 1**：`target_classes` 为什么填 `self.num_classes` 而不是 0 或 -1？

**答案**：因为 `pred_logits` 的最后一维是 `num_classes + 1`，下标 `num_classes` 正是 no-object 通道（u4-l4 讲过 `class_embed` 的输出维度是 K+1）。填 0 会把背景 query 全判成「类别 0」，填 -1 会越界。

**练习 2**：`empty_weight` 的长度是多少？为什么是它？

**答案**：`num_classes + 1`（L113）。`F.cross_entropy` 的 `weight` 必须覆盖所有类别下标，包括 no-object 通道；向量前 K 项为 1，最后一项为 `eos_coef`。

**练习 3**：如果不做 no-object 降权（`eos_coef=1`），训练早期模型最可能塌缩成什么状态？

**答案**：塌缩成「所有 query 都输出 no-object」。因为 90%+ 的监督信号都在鼓励背景通道，梯度会迅速把 no-object logit 推高；`eos_coef=0.1` 把这部分梯度缩到 1/10，前景类别才有机会竞争。

### 4.3 loss_masks（上）：K+β 重要性采样点从哪来

#### 4.3.1 概念说明

mask 损失要比较「预测 mask」与「GT mask」。最直接的做法是把预测上采样到 GT 尺寸，逐像素算 BCE 和 Dice。算一笔内存账就知其不可行（以 COCO + LSJ 为例）：

- GT mask 在 padding 后是 \( 1024 \times 1024 \)，单张 fp32 mask 约 4 MB；
- Q=100 个 query 的预测若全部物化到全分辨率：\( 100 \times 1024 \times 1024 \times 4\text{B} \approx 400\text{MB} \)，**一张图一组预测**；
- 深监督有 10 组，即使只对匹配上的 mask 算，高分辨率激活也会反复进出显存。

Mask2Former（继承自 PointRend 与 MaskFormer）的做法：**不比全图，只比 K 个采样点**。\( K = 12544 = 112^2 \)，一个 mask 只需 \( 12544 \times 4\text{B} \approx 49\text{KB} \)，内存骤降四个数量级；而且点是用**归一化坐标**采的，预测停在 1/4 分辨率即可参与比较——源码里那行注释就是这个意思：

> `# No need to upsample predictions as we are using normalized coordinates :)`（[criterion.py:L156](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/criterion.py#L156)）

采样不是均匀的，而是 **K + β 重要性采样**（PointRend 的训练策略）：

1. **先均匀撒 β·K 之外的候选**：随机采 \( k \cdot K \) 个候选点（\( k \) 是 `oversample_ratio`=3，共 \( 3 \times 12544 = 37632 \) 个）；
2. **不确定度排序**：在候选点上取预测 mask 的 logit，算「不确定度」，挑出最不确定的 \( \beta K \) 个（\( \beta \) 是 `importance_sample_ratio`=0.75，即 \( 9408 \) 个）；
3. **随机补齐**：剩下 \( (1-\beta)K = 3136 \) 个点从候选里随机抽，保持全局覆盖。

其中「不确定度」由 `calculate_uncertainty` 定义：

\[ u(x) = -|{\rm logit}(x)| \]

即 **logit 越接近 0（sigmoid 越接近 0.5，模型最犹豫）的点，不确定度越高**（[mask2former/modeling/criterion.py:L73-L87](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/criterion.py#L73-L87)）。直觉：mask 的内部与外部都是「容易的点」（logit 远离 0），真正决定分割质量的是**边界带**——重要性采样把 3/4 的训练火力集中在边界附近。

#### 4.3.2 核心流程

`get_uncertain_point_coords_with_randomness` 的行为（实现位于 detectron2 的 `projects/point_rend/point_features.py`，**外部依赖**，不在本仓库；以下为依据该实现的伪代码摘要，精确行号请对照本地安装版本）：

```text
输入: uncertainty_map (N,1,H,W)  ← 这里就是 src_masks（预测 mask logits）
      uncertainty_function      ← calculate_uncertainty
      num_points = K, oversample_threshold = k, importance_sample_ratio = β
1. num_uncertain = int(β·K);  num_random = K − num_uncertain
2. 候选: point_coords = torch.rand(N, k·K, 2)          # 归一化 (y,x)
3. 候选点上的 logit: point_sample(uncertainty_map, point_coords)
4. 对每个 mask 在 k·K 个候选里取不确定度 top-(βK)     # 最不确定的点
5. 再从候选里随机抽 (1−β)K 个                          # 保底覆盖
6. 返回 (N, K, 2)：随机点与不确定点拼接成一行（拼接顺序以本地 detectron2 源码为准；对损失无影响，两组点地位相同）
```

`loss_masks` 里采样相关的调用链（[mask2former/modeling/criterion.py:L161-L181](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/criterion.py#L161-L181)）：

```text
with torch.no_grad():                       # 采样过程不建图
    point_coords = get_uncertain_point_coords_with_randomness(
        src_masks, calculate_uncertainty, K, k, β)      # (N, K, 2)
    point_labels = point_sample(target_masks, point_coords).squeeze(1)  # (N, K) GT 值
point_logits = point_sample(src_masks, point_coords).squeeze(1)          # (N, K) 预测值，带梯度
```

三个细节：

- **`@torch.no_grad()` 只包住「选点 + 取 GT 标签」**（L161-L175）：选点是个离散决策，本来就不可导；GT 是常数。而 `point_logits`（L177-L181）在 no_grad **之外**采样——梯度要沿「采样点上的预测 logit」回传到整个网络。这是「决策视为常数、数值保留梯度」的标准强化学习式写法。
- **两次 `point_sample` 用同一组坐标**：一次采 GT（全分辨率 padded mask），一次采预测（1/4 分辨率 logits）。归一化坐标把两者的分辨率差异完全抹平。
- **N 是「批内被匹配的 mask 总数」**，不是 Q：`loss_masks` 先用 `_get_src_permutation_idx` 把 `pred_masks` 从 `(B, Q, h, w)` 筛成只剩匹配上的那些（下一节看代码），未匹配 query 根本不进 mask 损失——它们的监督全在 `loss_ce` 的 no-object 通道里。

#### 4.3.3 源码精读

[mask2former/modeling/criterion.py:L140-L160](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/criterion.py#L140-L160) —— `loss_masks` 的前半段，准备「配对的预测 mask 与 GT mask」：

- **L146-L149**：`_get_src_permutation_idx` 得到 `(batch_idx, src_idx)`，`src_masks = src_masks[src_idx]` 把 `(B, Q, h, w)` 筛成 `(N, h, w)`——**只保留被匹配的 query**。Q=100 而典型 N≈10，这一步直接砍掉 90% 的 mask。
- **L150-L154**：收集各图 GT mask，`nested_tensor_from_tensor_list` 把尺寸不一的 mask 批 padding 到统一尺寸，再用 `tgt_idx` 筛出被匹配者。注意 `decompose()` 返回的第二个值 `valid`（padding 区域标记）**被丢弃了**——L151 的 TODO 注释明说「用 valid 屏蔽 padding 区域」是未完成事项：目前 padding 出来的 0 被当作背景标签参与损失（LSJ 下 batch 内都是 1024×1024，实际无 padding，问题不大）。
- **L158-L159**：`[:, None]` 给两侧都加一个通道维，变 `(N, 1, H, W)`——这是 `point_sample`（内部 `grid_sample`）与 `get_uncertain_point_coords_with_randomness`（`assert logits.shape[1] == 1`）要求的形状。GT 虽然是单通道二值图，也要走同一个形状协议。

[mask2former/utils/misc.py:L48-L70](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/utils/misc.py#L48-L70) —— `nested_tensor_from_tensor_list`：`_max_by_axis`（[L16-L22](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/utils/misc.py#L16-L22)）算出批内最大高宽，开全零张量逐图左上角拷贝（L63-L66），mask 张量标记真实区域为 False、padding 为 True（L67）。返回 `NestedTensor`，`decompose()` 取回 `(tensors, mask)`。与 u3-l1 的 `prepare_targets` 不同：那里 pad 到「网络输入的 padding 尺寸」，这里只 pad 到「批内最大 mask 尺寸」——因为归一化坐标采样不需要对齐网络输入，只要批内对齐即可。

[mask2former/modeling/criterion.py:L73-L87](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/criterion.py#L73-L87) —— `calculate_uncertainty`：`assert logits.shape[1] == 1` 保证逐 mask 单通道输入，返回 `-|logit|`。注意它 `clone()` 了一下再取绝对值——避免原地操作影响后续梯度。

采样参数的配置来源：

[mask2former/config.py:L106-L114](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/config.py#L106-L114) —— 三个 point loss 配置键的注册：`TRAIN_NUM_POINTS = 112 * 112`、`OVERSAMPLE_RATIO = 3.0`（注释写明是 PointRend 论文的参数 `k`）、`IMPORTANCE_SAMPLE_RATIO = 0.75`（参数 `β`）。COCO 全景训练配置原样覆盖了一遍（[configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml:L37-L39](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml#L37-L39)）。值得提醒：**匹配器（u5-l1）与损失共用同一个 `TRAIN_NUM_POINTS`**（[maskformer_model.py:L115](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L115)），但它俩的采样并不共享随机数——匹配器是纯均匀随机采样，损失是不确定度引导采样。

#### 4.3.4 代码实践

**实践目标**：复刻 `loss_masks` 的采样段，亲眼看到 `point_sample` 前后的张量形状。

**操作步骤**（示例代码）：

```python
# practice_5_2_3.py —— 观察采样前后的形状
import torch
from detectron2.projects.point_rend.point_features import (
    get_uncertain_point_coords_with_randomness, point_sample)
from mask2former.modeling.criterion import calculate_uncertainty
from mask2former.utils.misc import nested_tensor_from_tensor_list

torch.manual_seed(0)
# 模拟：批内 3 张图各匹配上 2 个 mask → N=6
# 预测 mask 在 1/4 分辨率（例：输入 1024 → 256）
src_masks = torch.randn(6, 1, 256, 256)
# GT mask 尺寸不一：200x300 / 256x256 / 320x180，每图 2 张
sizes = [(200, 300), (256, 256), (320, 180)]
gt_list = [(torch.rand(2, h, w) > 0.7).float() for (h, w) in sizes]

target_masks, valid = nested_tensor_from_tensor_list(gt_list).decompose()
print("padding 后 GT 批:", target_masks.shape, " valid:", valid.shape, valid.float().mean().item())

K, k, beta = 12544, 3.0, 0.75
with torch.no_grad():
    point_coords = get_uncertain_point_coords_with_randomness(
        src_masks, lambda x: calculate_uncertainty(x), K, k, beta)
    point_labels = point_sample(target_masks, point_coords, align_corners=False).squeeze(1)
point_logits = point_sample(src_masks, point_coords, align_corners=False).squeeze(1)

print("point_coords:", point_coords.shape, " 取值范围 [%.3f, %.3f]" %
      (point_coords.min(), point_coords.max()))
print("point_labels:", point_labels.shape, " 取值集合:", point_labels.unique())
print("point_logits:", point_logits.shape)
```

**需要观察的现象**：

1. `target_masks` 形状为 `(6, 320, 300)`——批内最大高 × 最大宽（`_max_by_axis` 的结果）；`valid` 为 `(6, 320, 300)`，True 占比即 padding 比例。
2. `point_coords` 形状 `(6, 12544, 2)`，坐标全在 `[0, 1]`。
3. `point_labels` 与 `point_logits` 形状都是 `(6, 12544)`；`point_labels` 的取值集合是 `{0.0, 1.0}` 附近（双线性插值会引入中间值，padding 区会贡献一些 0）。

**预期结果**：形状如上。`(6, 12544, 2)` 与 `(6, 12544)` 就是「K 个点」的落地产物——后续所有 mask 损失都在这两个 `(N, K)` 张量上进行。数值待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：候选点有多少个？其中多少个进入「不确定度 top-k」？多少个随机补齐？

**答案**：候选 \( kK = 3 \times 12544 = 37632 \) 个；不确定点 \( \beta K = 9408 \) 个；随机点 \( (1-\beta)K = 3136 \) 个。三者满足 \( K = 9408 + 3136 \)。

**练习 2**：为什么先 oversample 3 倍再挑 top-k，而不是直接在全图上取不确定度 top-K？

**答案**：全图 top-K 会几乎全落在同一条边界上（相邻高不确定像素高度相关），采样点退化、覆盖面差；先均匀撒 3K 候选保证空间分散性，再在候选里挑最不确定的，是「覆盖」与「聚焦」的折中。这也是 PointRend 论文消融实验确认的配置。

**练习 3**：`point_logits` 为什么必须放在 `torch.no_grad()` 外面？

**答案**：采样点上的预测 logit 是损失的计算输入，梯度必须从 BCE/Dice 损失回传到 `point_logits`，再经 `grid_sample` 的双线性插值权重回传到 `src_masks`（1/4 分辨率特征）乃至整个网络。若放进 no_grad，`loss_mask`/`loss_dice` 将没有梯度，mask 分支学不到任何东西（分类损失仍能训练）。

### 4.4 loss_masks（下）：逐点 BCE、Dice 与 num_masks 归一化

#### 4.4.1 概念说明

采样得到 `(N, K)` 的 `point_logits` 与 `point_labels` 后，损失由两个互补函数计算：

- **BCE（`sigmoid_ce_loss`）**：逐点二分类交叉熵，监督每个点的绝对概率，量级随点数稳定（先对 K 个点取平均）；
- **Dice（`dice_loss`）**：区域级重叠度度量，\( 1 - \frac{2|A \cap B|}{|A| + |B|} \)，天然与前景占比无关——小物体不会被大量背景像素稀释。两者搭配是分割标准配方（MaskFormer/Mask2Former 均以 `MASK_WEIGHT=5.0, DICE_WEIGHT=5.0` 等权使用）。

**归一化是本讲最重要的设计。** 两个函数的最后一行都是 `.sum() / num_masks`：

\[ L = \frac{1}{\text{num\_masks}} \sum_{n=1}^{N} \ell_n, \qquad \ell_n^{\text{bce}} = \frac{1}{K}\sum_{j} {\rm BCE}(x_{nj}, y_{nj}), \qquad \ell_n^{\text{dice}} = 1 - \frac{2\sum_j \sigma(x_{nj}) y_{nj} + 1}{\sum_j \sigma(x_{nj}) + \sum_j y_{nj} + 1} \]

分母是「批内 GT mask 总数（跨卡平均）」而**不是像素数、不是点数、也不是 batch size**。三层理由：

1. **尺度不变性**：损失量级不随输入分辨率、`TRAIN_NUM_POINTS`、batch size 变化——换数据集/改 LSJ 尺寸后学习率不用重调，日志里的 loss 曲线也跨配置可比。
2. **每个 mask 平等投票**：BCE 若按像素（点）平均，一张 1000 点为正的大 mask 会比 10 个小 mask 更有话语权；按 mask 平均后大 mask 的逐点均值与小 mask 的逐点均值地位相同，与 Dice（本身就是 per-mask 度量）保持一致语义。
3. **分布式正确性**：detectron2 的 DDP 会对各卡梯度取平均。若每卡用自己的局部 \( N_{\text{local}} \) 归一化，平均后的梯度是「各卡均值的均值」，与全局批的均值不等（GT 数多的卡被隐性加权）。用 `all_reduce` 得到的全局平均 mask 数 \( \bar{N} = \frac{1}{W}\sum_g N_g \) 做分母，则 \( \frac{1}{W}\sum_g \frac{\sum_{n \in g} \ell_n}{\bar N} = \frac{\sum_{\text{全局}} \ell_n}{\sum_g N_g} \)，恰好等于全局批均值——DETR 同款技巧（[mask2former/modeling/criterion.py:L229-L231](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/criterion.py#L229-L231)）。

#### 4.4.2 核心流程

```text
输入: point_logits (N, K) 预测, point_labels (N, K) GT, num_masks 标量
loss_mask = sigmoid_ce_loss_jit(point_logits, point_labels, num_masks)
    → F.binary_cross_entropy_with_logits(reduction="none")   # 数值稳定（log-sum-exp 合并）
    → .mean(1) 每 mask 对 K 点取平均 → .sum()/num_masks
loss_dice = dice_loss_jit(point_logits, point_labels, num_masks)
    → logits.sigmoid() → flatten(1)
    → 1 − (2·交 + 1)/(|pred| + |gt| + 1)      # +1 平滑防 0/0
    → .sum()/num_masks
返回: {"loss_mask":…, "loss_dice":…}
```

两者都被 `torch.jit.script` 包装成 `*_jit` 版本（[L43-L45](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/criterion.py#L43-L45)、[L68-L70](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/criterion.py#L68-L70)），在 `loss_masks` 里实际调用的是 jit 版本（[L184-L185](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/criterion.py#L184-L185)）——小算子融合省 kernel launch 开销（每个训练 step 调 10 次×2 函数）。

#### 4.4.3 源码精读

[mask2former/modeling/criterion.py:L21-L40](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/criterion.py#L21-L40) —— `dice_loss`：L35 先 sigmoid（输入是 logits），L36 `flatten(1)` 把 `(N, K)` 摊成每 mask 一行；L37-L39 分子是 2 倍逐点积之和（\(\sigma(x)\cdot y\) 在 0/1 标签下就是软交集），分母是两者之和；**+1 平滑项**保证空 mask（如极小目标被采样漏掉）不会 0/0；L40 `.sum() / num_masks`。

[mask2former/modeling/criterion.py:L48-L65](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/criterion.py#L48-L65) —— `sigmoid_ce_loss`：用 `binary_cross_entropy_with_logits` 而非 `sigmoid + BCE`，把 sigmoid 融进 log-sum-exp，避免小概率下溢（教科书级数值稳定写法）；`reduction="none"` 后自己控制归一化顺序：先 `.mean(1)`（每 mask 的 K 点平均）再 `.sum() / num_masks`。**顺序不能换**——先 `.sum()/N/K` 会变成逐点平均，回到大 mask 主导的问题。

[mask2former/modeling/criterion.py:L183-L190](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/criterion.py#L183-L190) —— 组装两个损失并返回；L188-L189 `del src_masks, target_masks` 提前释放高分辨率中间张量的引用，缓解显存峰值（配合 Python 引用计数尽快让 allocator 回收）。

回到 `num_masks` 的计算现场：

[mask2former/modeling/criterion.py:L224-L231](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/criterion.py#L224-L231) —— L225 用 `sum(len(t["labels"]) for t in targets)` 数 GT（`labels` 长度即该图 GT mask 数）；L226-L228 包成 tensor 放到输出所在 device；L229-L231 多卡时 `all_reduce` 求和、除以 world size、clamp 最小 1，最后 `.item()` 变回 Python float 作为分母。

最后把权重对应表补全（`MaskFormer.forward` 会用 `weight_dict` 乘上去，u5-l3 展开）：

| 损失键 | 计算函数 | config 默认权重 | COCO 全景 yaml 权重 |
| --- | --- | --- | --- |
| `loss_ce` | `loss_labels` | `CLASS_WEIGHT` 1.0 | 2.0 |
| `loss_mask` | `loss_masks`（BCE 部分） | `MASK_WEIGHT` 20.0 | 5.0 |
| `loss_dice` | `loss_masks`（Dice 部分） | `DICE_WEIGHT` 1.0 | 5.0 |

注册处 [mask2former/config.py:L32-L37](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/config.py#L32-L37)，yaml 覆盖处 [configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml:L23-L26](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml#L23-L26)。注意 `MASK_WEIGHT` 全局默认 20.0、COCO 系全改为 5.0——匹配代价权重与损失权重共用这三个数（u5-l1），改它们会同时改变匹配行为。

#### 4.4.4 代码实践

**实践目标**：完整跑一遍随机预测 + 3 张大小不一 GT 的 `loss_masks`，然后扫描三个采样参数，记录并解释 `loss_mask` / `loss_dice` 的变化。

**操作步骤**（示例代码，实践主体）：

```python
# practice_5_2_4.py —— loss_masks 全流程 + 采样参数扫描
import torch
from mask2former.modeling.criterion import SetCriterion
from mask2former.modeling.matcher import HungarianMatcher

torch.manual_seed(0)
K, Q, B = 133, 100, 3
sizes = [(200, 300), (256, 256), (320, 180)]
targets = [{"labels": torch.randint(0, K, (2,)),
            "masks": (torch.rand(2, h, w) > 0.7).float()} for (h, w) in sizes]

def run(num_points, oversample, imp_ratio, seed=0):
    torch.manual_seed(seed)
    matcher = HungarianMatcher(cost_class=2.0, cost_mask=5.0, cost_dice=5.0,
                               num_points=num_points)
    crit = SetCriterion(K, matcher, {}, 0.1, ["labels", "masks"],
                        num_points, oversample, imp_ratio)
    outputs = {"pred_logits": torch.randn(B, Q, K + 1),
               "pred_masks": torch.randn(B, Q, 64, 64)}   # 1/4 分辨率示例
    # 只取主输出的 mask 损失（不走 aux）
    indices = matcher(outputs, targets)
    num_masks = sum(len(t["labels"]) for t in targets)
    return {k: v.item() for k, v in crit.loss_masks(outputs, targets, indices, num_masks).items()}

print("基准 (K=12544, k=3.0, β=0.75):", run(12544, 3.0, 0.75))
print("K=4900                              :", run(4900, 3.0, 0.75))
print("K=12544, k=1.0(无 oversample)       :", run(12544, 1.0, 0.75))
print("K=12544, β=0.0(纯均匀采样)          :", run(12544, 3.0, 0.0))
print("K=12544, β=1.0(纯不确定度采样)      :", run(12544, 3.0, 1.0))
```

**需要观察的现象**（固定随机种子后每行数字可复现）：

1. `K` 从 12544 降到 4900：两个损失**只有微小波动**——它们是同一个期望的蒙特卡洛估计，点数只影响方差。
2. `k` 从 3.0 降到 1.0：候选池变小，top-k 选出的「不确定点」质量下降，损失通常略微变化。
3. `β` 从 0.75 拨到 0.0（纯均匀）与 1.0（纯不确定度）：**β 越大，loss_mask 越大**——不确定点是模型最犹豫的点，采样越偏向它们，采到的「难例」越多，BCE 数值越高。这不是变差，而是把训练信号集中到了边界。

**预期结果**：定性趋势如上；具体数值待本地验证（随机初始化下 `loss_mask` 大约在 0.5～0.9 区间、`loss_dice` 大约在 0.4～0.8 区间，仅作量级参考）。若观察到 β 变化对 `loss_dice` 的影响小于对 `loss_mask` 的影响，思考一下 Dice 的分母随采样分布如何变化即可解释。

#### 4.4.5 小练习与答案

**练习 1**：`sigmoid_ce_loss` 里为什么是 `.mean(1).sum() / num_masks` 而不是 `.sum() / (N * K)`？

**答案**：`.mean(1)` 先在「每个 mask 内部」对 K 个点平均，得到每个 mask 的平均逐点损失；再对 mask 求和除以 mask 数，等价于「mask 均值的均值」。若写成 `.sum()/(N*K)`，两者数值相同（当每行点数一致时），但语义上必须先固定「每 mask 一个标量」这一层级，才能与 `dice_loss`（天然 per-mask）相加时量纲一致。更关键的是当批内 mask 数 N 与 num_masks 相等时二者等价，而源码选择显式分两层表达意图。

**练习 2**：Dice 公式里 `+1` 的平滑项解决什么问题？

**答案**：防止「预测与 GT 双双为空」时 0/0。比如一个极小 GT 恰好在 K 个采样点上全为 0（没采到前景），此时分子分母都接近 0；+1 让损失退化为一个有限值而不是 NaN。

**练习 3**：为什么 `loss_masks` 里被监督的只有 N 个匹配 mask，未匹配 query 却也要参与 `loss_labels`？

**答案**：未匹配 query 的「正确行为」是输出 no-object——这本身是分类损失要教的（把它们推成背景），但它们的 mask 无对应 GT，无从比 mask。所以分工是：分类损失管全部 Q 个 query（含背景压制），mask 损失只管匹配上的 N 个。若不给背景压制，Q=100 个 query 会倾向重复预测同一批前景目标（一对一匹配的约束会被绕开），推理时出现大量冗余 mask。

## 5. 综合实践

把本讲四个模块串起来：**手工复刻一次完整的「匹配 → 采样 → 损失」并产出一张参数分析表**。

任务要求：

1. 用 4.1.4 的脚本为骨架，构造 `B=2、Q=20、K=5` 的玩具问题：图 A 给 3 个「方块 GT」（在 `(h, w)` 网格里手工画 3 个不重叠的实心矩形），图 B 给 1 个「大椭圆 GT」（用参数方程生成）。预测分两种：
   - `perfect`：`pred_logits` 在匹配类别上给 +10、其余 -10，`pred_masks` 直接把 GT 下采样到 1/4 分辨率（用 `F.interpolate`）；
   - `random`：全随机。
2. 对两种预测分别调用 `criterion(outputs, targets)`，记录 30 组（含 aux 可选）或 3 组损失，验证：`perfect` 的 `loss_ce/loss_mask/loss_dice` 都显著低于 `random`（`loss_dice` 应接近 0）。
3. 在 `random` 预测上做 4.4.4 的三参数扫描，把结果整理成表（行=参数组合，列=loss_mask/loss_dice），用本讲原理解释每行变化。
4. 进阶（选做）：给 `SetCriterion` 增加一个自定义损失项（例如「匹配 query 的 mask sigmoid 均值与 GT 占比的一致性损失」），在 `get_loss` 的 `loss_map` 里注册并加入 `losses` 列表，验证 forward 返回新键；再观察 `MaskFormer.forward` 的加权过滤（[mask2former/maskformer_model.py:L211-L216](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L211-L216)）会因新键不在 `weight_dict` 里而把它**删掉**——由此理解为什么自定义损失必须同时改 `from_config` 的 `weight_dict`（u5-l3 的伏笔）。

预期：`perfect` 情形的 `loss_dice` 接近 0（采样点上的预测与 GT 完全一致时 dice≈1，loss≈0），`loss_ce` 接近 0；`random` 情形对应 u4 各实践中的量级。所有数值待本地验证。

## 6. 本讲小结

- `SetCriterion.forward`（[criterion.py:L212-L247](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/criterion.py#L212-L247)）执行「先匹配、后监督」两步：主输出匹配一次，`aux_outputs` 每层**重新匹配**再算，键名加 `_i` 后缀；`num_masks` 跨 GPU all_reduce 取平均并 clamp ≥ 1。
- `loss_labels` 用 `empty_weight`（no-object 权重 `eos_coef`=0.1）对 `(B, Q, K+1)` 的 logits 做类别加权交叉熵，化解「90% query 都是背景」的极端不平衡；目标张量「先铺 no-object、再点覆盖匹配类别」。
- `loss_masks` 只监督匹配上的 N 个 mask：`get_uncertain_point_coords_with_randomness` 做 K+β 重要性采样（12544 点 = 9408 个不确定度 top-k + 3136 个随机，来自 37632 个候选），不确定度定义为 \(-|\text{logit}|\)；归一化坐标让 1/4 分辨率预测与全分辨率 GT 直接对采（`point_sample` 两次、同一组坐标），**全程无需上采样**。
- 逐点损失 = BCE（`binary_cross_entropy_with_logits`，每 mask 对 K 点 mean）+ Dice（软交集 + 1 平滑），两者都以 `.sum() / num_masks` 收尾——按「批内 GT mask 总数（跨卡平均）」归一化带来尺度不变性、每 mask 平等、分布式梯度正确三重收益。
- 采样与 GT 标签在 `@torch.no_grad()` 内，`point_logits` 在外——决策不可导视为常数，数值保留梯度回传。
- 工程细节：两个损失函数被 `torch.jit.script` 包装；`nested_tensor_from_tensor_list` 只 pad 到批内最大尺寸且 `valid` mask 被丢弃（TODO 未完成）；`del` 提前释放大张量。

## 7. 下一步学习建议

本讲结束时，「损失怎么算」已经闭环，但还有一个悬而未决的问题：**30 个损失键的权重从哪来、怎么流回训练循环**。这正是下一讲 u5-l3《深监督、辅助损失与损失权重流转》的内容：`from_config` 里 `weight_dict` 如何从 3 键展开成 30 键（含 `_0.._8` 后缀）、`DEC_LAYERS` 为何要减 1（u4-l3 埋的伏笔）、`MaskFormer.forward` 的加权过滤如何裁掉未知键。

阅读建议：

1. 先读 [mask2former/maskformer_model.py:L96-L161](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L96-L161)，把 `weight_dict` 展开循环与本讲的键名规则对上。
2. 对照 detectron2 的 [point_rend/point_features.py](https://github.com/facebookresearch/detectron2/blob/main/projects/point_rend/point_features.py)（外部依赖）读一遍 `get_uncertain_point_coords_with_randomness` 的真实实现，验证 4.3.2 的伪代码摘要（重点核对：候选点数、top-k 选取、随机补齐三步，以及 `int(β·K)` 的取整方式）。
3. 有余力可读 PointRend 论文（Kirillov et al., CVPR 2020）第 3 节的训练采样策略，理解 β 的消融；再对照 Mask2Former 论文（Cheng et al., CVPR 2022）的「point-based losses」小节，看它如何把这套采样从 MaskFormer 继承下来并成为默认配置。
