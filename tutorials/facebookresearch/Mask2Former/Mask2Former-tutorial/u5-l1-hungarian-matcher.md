# 讲义 u5-l1：匈牙利匹配器 HungarianMatcher

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚 Mask2Former 为什么需要「一对一匹配」：mask classification 范式下，模型输出固定 Q 个 query 预测，而每张图的 GT 数量 M 各不相同，必须先解决「哪个 query 对哪个 GT」才能算损失。
2. 读懂 [mask2former/modeling/matcher.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/matcher.py#L1-L190) 的全部代码：三项代价（class / mask / dice）如何拼成代价矩阵，`scipy.optimize.linear_sum_assignment` 如何求解匹配。
3. 掌握「随机采样点近似 mask 代价」这项 memory-efficient 技巧：为什么所有 mask 共享同一组随机点、归一化坐标如何化解预测 mask 与 GT mask 的分辨率差异。
4. 理解 `@torch.no_grad()` 与 `autocast(enabled=False)` 两个工程细节背后的原因。
5. 动手用玩具数据（10 个 query、3 个 GT）跑通匹配器，并手工复现代价矩阵验证与官方实现结果一致。

## 2. 前置知识

阅读本讲前，建议你已经理解以下内容（来自前置讲义）：

- **mask classification 范式**（u3-l1、u4-l4）：模型输出 `pred_logits`（形状 `(B, Q, K+1)`，Q=100 个 query，K 是数据集类别数，最后一维多出的 1 是 no-object 通道）和 `pred_masks`（形状 `(B, Q, H/4, W/4)`，1/4 分辨率的 mask logits，由 `einsum("bqc,bchw->bqhw")` 生成）。
- **prepare_targets 的产物**（u3-l1）：训练时 GT 被整理成 `targets: list[dict]`，每个 dict 含 `labels`（`(M,)` 类别 id）和 `masks`（`(M, H_pad, W_pad)` 二值 mask，**左上角对齐 zero-pad 到 padding 后的完整输入尺寸**，见 [mask2former/maskformer_model.py:L264-L278](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L264-L278)）。
- **深监督机制**（u4-l3、u4-l4）：解码器每组中间输出都会产生一组预测（`aux_outputs`），因此匹配会被执行多次。
- **集合预测与 DETR 的渊源**：本文件改自 DETR 的 `models/matcher.py`（见文件头 [mask2former/modeling/matcher.py:L1-L5](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/matcher.py#L1-L5)），核心思想一脉相承：**先匹配、后算损失**。

两个本讲要用的基础概念：

- **LSAP（Linear Sum Assignment Problem，线性分配问题）**：给定代价矩阵 \( C \in \mathbb{R}^{Q \times M} \)，从每行每列最多选一个元素，选出一组「行-列」配对使总代价最小。匈牙利算法是它的经典解法，`scipy.optimize.linear_sum_assignment` 是工业级实现。
- **point_sample（PointRend 式采样）**：给定一组**归一化坐标**（最后一维是 `(y, x)`，取值 `[0,1]`），在特征图上做双线性插值取值（`align_corners=False` 时坐标经 \( 2x-1 \) 映射）。它来自 detectron2 的 point_rend 项目，见导入语句 [mask2former/modeling/matcher.py:L12](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/matcher.py#L12)。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [mask2former/modeling/matcher.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/matcher.py#L1-L190) | 本讲主角：`batch_dice_loss`、`batch_sigmoid_ce_loss`、`HungarianMatcher` 全部在此，共 190 行 |
| [mask2former/maskformer_model.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L96-L161) | `from_config` 中用配置权重构建 `HungarianMatcher` 并注入 `SetCriterion` |
| [mask2former/modeling/criterion.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/criterion.py#L212-L247) | `SetCriterion.forward` 是匹配器的唯一调用方（主输出一次 + 每层 aux 一次），下一讲 u5-l2 精读 |
| [mask2former/config.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/config.py#L32-L37) | 三个代价权重与采样点数的默认值注册处 |
| [configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml#L19-L39) | COCO 全景分割实际训练用的权重覆盖值 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：先讲清楚「为什么必须一对一匹配」（问题），再依次精读两个批成对代价函数（材料），最后走通 `memory_efficient_forward` 主流程（装配与求解）。

### 4.1 集合预测与一对一匹配：为什么需要匈牙利算法

#### 4.1.1 概念说明

mask classification 范式下，模型不再逐像素分类，而是输出固定 Q=100 个「query 预测」：每个 query 一份类别 logits（K+1 维，含 no-object）和一张 mask。可是一张图的 GT 对象数 M 是不定的（可能 3 个，可能 30 个）。于是一个尴尬的问题出现了：

> **损失函数怎么知道第 7 个 query 应该对上第 2 个 GT？**

如果用朴素的办法——比如「每个 GT 挑概率最高的 query」——会出现两个 GT 抢同一个 query 的冲突；如果允许多对一，模型会学到「重复预测同一物体也没关系」，推理时就会出大量冗余 mask。DETR 给出的答案是**集合预测 + 匈牙利匹配**：

- 把模型的 Q 个预测看成一个**集合**，GT 的 M 个对象也看成一个集合；
- 匹配是**一对一的、且对排列不变**（permutation-invariant）：无论 query 内部怎么排序，只要预测内容相同，损失不变；
- 未被匹配的 query 一律被视作 no-object，由分类损失的 no-object 通道（带 `empty_weight` 降权，见 u5-l2）去压制成背景。

类文档字符串里说得非常直白：为了效率，targets 不包含 no_object，因此预测数通常多于目标数，做 1-to-1 的最优匹配，其余预测视为 non-object，见 [mask2former/modeling/matcher.py:L70-L76](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/matcher.py#L70-L76)。

#### 4.1.2 核心流程

给定单张图的代价矩阵 \( C \in \mathbb{R}^{Q \times M} \)（如何构造是 4.2/4.3/4.4 的事），匹配就是求解 LSAP：

\[ \min_{\sigma} \; \sum_{i=1}^{M} C_{\sigma(i),\,i} \]

其中 \(\sigma\) 是从 GT 下标 \(i\) 到 query 下标 \(\sigma(i)\) 的**单射**（injective，一对一）。当 \(Q \ge M\) 时（常态，Q=100），解出 M 对；当 GT 比 query 还多时，解出 Q 对——返回值长度满足 `len(index_i) = len(index_j) = min(num_queries, num_target_boxes)`（[mask2former/modeling/matcher.py:L176-L177](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/matcher.py#L176-L177)）。

为什么贪心不行？「每个 GT 各自挑代价最小的 query」是局部最优，不是全局最优——一个 query 可能在两列上都是最小值，让出它反而总代价更低。下一节的实践会构造一个具体反例。

整个训练期的匹配流程：

```text
MaskFormer.forward (训练分支)
  └─ criterion(outputs, targets)                # SetCriterion.forward
       ├─ indices = self.matcher(主输出, targets)   # 匹配第 0 组预测（learnable query 的直接预测）
       ├─ 计算 loss_labels / loss_masks（在匹配结果上）
       └─ 对 aux_outputs 中每一层:
            indices = self.matcher(aux输出, targets)  # 每层独立重新匹配
            计算带 _i 后缀的损失
```

注意：**每一层 decoder 输出都重新匹配一次**（[mask2former/modeling/criterion.py:L239-L245](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/criterion.py#L239-L245)），即 DEC_LAYERS=10 时一次前向要跑 10 次匹配器。

#### 4.1.3 源码精读

**匹配器的构建与注入。** 在 `MaskFormer.from_config` 中，三个损失权重同时充当匹配代价权重：

[mask2former/maskformer_model.py:L106-L116](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L106-L116) —— 从 cfg 读出 `CLASS_WEIGHT / DICE_WEIGHT / MASK_WEIGHT`，用它们实例化 `HungarianMatcher(cost_class=..., cost_mask=..., cost_dice=..., num_points=TRAIN_NUM_POINTS)`，随后同一个权重三元组也写入 `weight_dict` 用于损失加权。这是一个有意的设计：**匹配时认为重要的东西，算损失时也同样重要**。

两个层面的默认值（COCO 全景配置覆盖了全局默认）：

| 配置键 | [config.py 默认值](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/config.py#L32-L37) | [COCO 全景 yaml](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml#L22-L26) |
| --- | --- | --- |
| `CLASS_WEIGHT` | 1.0 | 2.0 |
| `MASK_WEIGHT` | 1.0 | 5.0 |
| `DICE_WEIGHT` | 1.0 | 5.0 |
| `TRAIN_NUM_POINTS` | 112×112 = 12544（[config.py:L106-L108](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/config.py#L106-L108)） | 12544（[yaml:L37](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml#L37)） |

**构造函数与防御性断言。**

[mask2former/modeling/matcher.py:L78-L93](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/matcher.py#L78-L93) —— `__init__` 只存四个超参：三个代价权重和采样点数 `num_points`；并断言三项权重不全为 0，否则代价矩阵恒为 0、匹配退化为任意排列。

**调用点。**

[mask2former/modeling/criterion.py:L222](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/criterion.py#L222) —— 对去掉 `aux_outputs` 的主输出调用一次匹配器；[mask2former/modeling/criterion.py:L241](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/criterion.py#L241) —— 对每个中间层输出再各匹配一次。

#### 4.1.4 代码实践

**实践 1：用 scipy 体验「贪心 vs 全局最优」（不需要 detectron2，只需 numpy/scipy）**

1. **实践目标**：亲眼看到「每个 GT 各自挑最便宜 query」的贪心策略可能比匈牙利算法的解更差，理解为什么要用 LSAP 求解器。
2. **操作步骤**：把下面的示例代码存成 `greedy_vs_hungarian.py` 并运行。

```python
# 示例代码：贪心逐列取最小 vs 线性分配全局最优
import numpy as np
from scipy.optimize import linear_sum_assignment

C = np.array([
    [1.0, 1.5, 2.0],   # query 0 对 3 个 GT 的代价
    [1.1, 5.0, 6.0],   # query 1
    [3.0, 1.6, 9.0],   # query 2
])

# 贪心：按 GT 顺序，每个 GT 挑当前还没被占用的、代价最小的 query
used = set()
greedy_pairs, greedy_total = [], 0.0
for j in range(C.shape[1]):                 # 逐列（GT）
    cands = [(C[i, j], i) for i in range(C.shape[0]) if i not in used]
    cost, i = min(cands)
    used.add(i); greedy_pairs.append((i, j)); greedy_total += cost

row, col = linear_sum_assignment(C)          # 匈牙利
opt_total = C[row, col].sum()

print("贪心:", greedy_pairs, "总代价 =", round(greedy_total, 4))
print("匈牙利:", list(zip(row, col)), "总代价 =", round(opt_total, 4))
```

3. **需要观察的现象**：贪心先给 GT0 选了 query 0（代价 1.0，虽然 query 1 只贵 0.1），导致 GT2 只能接手代价 6.0 的 query 1。
4. **预期结果**：贪心总代价 = 1.0 + 1.6 + 6.0 = **8.6**；匈牙利最优解为 query1→GT0、query2→GT1、query0→GT2，总代价 = 1.1 + 1.6 + 2.0 = **4.7**。两者相差近一倍——局部最优 ≠ 全局最优。（此矩阵的最优解可手工枚举 6 种排列验证。）
5. 结果为纯确定性数值计算，可放心核对；若与你手算不一致，以你的手算枚举为准。

#### 4.1.5 小练习与答案

**练习 1**：Q=100、某图只有 M=3 个 GT 时，匹配产生几对？剩下 97 个 query 由什么损失监督？
**答案**：3 对（`min(Q, M)`）；剩余 query 被视为 no-object，由分类损失中对 no-object 通道的监督压制（其权重被 `eos_coef=0.1` 降权，详见下一讲 u5-l2）。

**练习 2**：匹配结果对 query 的排列是否敏感？为什么这很重要？
**答案**：不敏感。代价矩阵只依赖「预测内容 vs GT 内容」，query 换个顺序只是代价矩阵换行，LSAP 求出的配对关系（哪个预测对哪个 GT）不变，因此损失对预测集合的排列不变——这正是集合预测需要的性质。

**练习 3**：把 `cost_class = cost_mask = cost_dice = 0` 传入 `HungarianMatcher` 会发生什么？
**答案**：构造函数直接断言失败，抛出 `AssertionError: all costs cant be 0`（[mask2former/modeling/matcher.py:L91](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/matcher.py#L91)）——代价全 0 时任何匹配都「最优」，匹配失去意义。

---

### 4.2 batch_sigmoid_ce_loss：批成对 mask BCE 代价

#### 4.2.1 概念说明

匹配需要回答「query n 的 mask 和 GT m 的 mask 有多不像」。最直接的度量是二值交叉熵（BCE）：把预测 mask logits 过 sigmoid 后与 0/1 的 GT mask 逐点比对。但注意匹配需要的是**所有 (query, GT) 两两组合**的代价——Q×M 个数，而普通 `F.binary_cross_entropy_with_logits(inputs, targets)` 只在相同形状的一对张量上逐元素算。`batch_sigmoid_ce_loss` 的作用就是把 BCE「批量化、成对化」：一次前向得到整张 `(Q, M)` 的 BCE 代价子矩阵。

它有个关键技巧：BCE 对标签是线性的，可以拆成正、负两部分再用 `einsum` 批量组合。

#### 4.2.2 核心流程

对 logit \( x \) 和标签 \( y \in \{0,1\} \)，逐点 BCE 可分解为：

\[ \mathrm{BCE}(x, y) = y \cdot \mathrm{BCE}(x, 1) + (1-y) \cdot \mathrm{BCE}(x, 0) \]

于是对 N 个预测、M 个目标、各 K 个采样点（`pos`/`neg` 是对全 1 / 全 0 标签的逐点 BCE）：

\[ \mathrm{cost}_{bce}[n, m] = \frac{1}{K} \sum_{k=1}^{K} \Big( pos[n,k]\, t[m,k] + neg[n,k]\,(1 - t[m,k]) \Big) \]

这正是代码里两个 `einsum("nc,mc->nm", ...)` 的含义：`c` 维（采样点）上做内积，输出 `(n, m)` 成对矩阵。

流程伪代码：

```text
输入: inputs (N, K) 预测 mask 在采样点上的 logits
      targets (M, K) GT mask 在同一组采样点上的 0/1 值
pos = BCE_with_logits(inputs, 全1)        # (N, K)
neg = BCE_with_logits(inputs, 全0)        # (N, K)
cost = pos @ targets.T + neg @ (1-targets).T   # (N, M)
return cost / K
```

#### 4.2.3 源码精读

[mask2former/modeling/matcher.py:L38-L62](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/matcher.py#L38-L62) —— `batch_sigmoid_ce_loss` 完整实现：

- 第 49 行 `hw = inputs.shape[1]`：变量名 `hw` 暴露了 DETR 血统——DETR 在完整 H×W 特征图上算代价，而 Mask2Former 传入的是采样后的 (N, K)，所以这里 `hw` 实际就是**采样点数 K**；
- 第 51-56 行：分别对全 1 / 全 0 标签算 `binary_cross_entropy_with_logits`（带 logit 版本数值稳定，内部用 log-sum-exp 技巧，不需要先 sigmoid）；
- 第 58-60 行：两个 `einsum("nc,mc->nm")` 把逐点误差按标签加权聚合成 `(N, M)` 矩阵；
- 第 62 行：除以点数取平均。

[mask2former/modeling/matcher.py:L65-L67](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/matcher.py#L65-L67) —— `torch.jit.script` 包装成脚本化模块，纯逐元素 + einsum 运算适合 JIT 融合加速（下节 dice 同理）。

与训练损失的区别（对照着记）：匹配用的 BCE 是「成对矩阵」版本；真正算损失时用的是 `criterion.py` 里的逐对版本 `sigmoid_ce_loss`（[mask2former/modeling/criterion.py:L48-L65](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/criterion.py#L48-L65)），只对匹配上的 query-GT 对计算——u5-l2 会精读。

#### 4.2.4 代码实践

**实践 2：验证批成对 BCE 等价于朴素双循环（需要能 `import mask2former`，环境要求见下）**

1. **实践目标**：确认 `batch_sigmoid_ce_loss(x, y)[n, m]` 就等于 `F.binary_cross_entropy_with_logits(x[n], y[m], reduction="mean")`，从而理解 einsum 拆分的正确性。
2. **操作步骤**：在仓库根目录运行下面脚本（`from mask2former...` 会先执行 [mask2former/__init__.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/__init__.py#L1-L27)，连带加载数据集注册、模型与 CUDA 算子扩展，因此需要按 u1-l2 完成完整安装。**若尚未编译 CUDA 扩展**，可把 matcher.py 中这三个函数复制进独立脚本练习，效果相同）。

```python
# 示例代码：批成对 BCE 的等价性验证
import torch
import torch.nn.functional as F
from mask2former.modeling.matcher import batch_sigmoid_ce_loss

torch.manual_seed(0)
N, M, K = 4, 3, 10
x = torch.randn(N, K)                            # 预测 mask 的采样 logits
y = torch.randint(0, 2, (M, K)).float()          # GT mask 的采样值

fast = batch_sigmoid_ce_loss(x, y)               # (N, M)

naive = torch.zeros(N, M)
for n in range(N):
    for m in range(M):
        naive[n, m] = F.binary_cross_entropy_with_logits(x[n], y[m], reduction="mean")

print("形状:", fast.shape)                        # 期望 torch.Size([4, 3])
print("最大绝对误差:", (fast - naive).abs().max().item())
```

3. **需要观察的现象**：两种实现的输出形状同为 `(4, 3)`，逐元素误差在浮点精度内为 0。
4. **预期结果**：最大绝对误差应为 0 或 1e-7 量级（浮点求和顺序差异）。具体数值待本地验证。
5. 若误差显著大于 1e-5，说明你复制的实现与源码不一致，请逐行对照第 58-62 行。

#### 4.2.5 小练习与答案

**练习 1**：输入 `(Q, K)` 与 `(M, K)`，输出形状是什么？`einsum("nc,mc->nm")` 里被求和掉的维度是什么？
**答案**：输出 `(Q, M)`；求和掉的是 `c`（K 个采样点）维——即对两个 mask 在同一组采样点上的取值做内积。

**练习 2**：为什么不先 `sigmoid` 再算 BCE？
**答案**：`binary_cross_entropy_with_logits` 把 sigmoid 与 log 合并为 log-sum-exp 形式，避免 \( \log\sigma(x) \) 在 \( x \) 很大/很小时的下溢与上溢，数值稳定且省一次显式 sigmoid。

**练习 3**：`loss / hw` 中 `hw` 在 Mask2Former 语境下等于什么？
**答案**：等于采样点数 K（`inputs.shape[1]`）。名字沿用 DETR（在完整 H×W 图上计算），本仓库传入的是 point_sample 后的点值。

---

### 4.3 batch_dice_loss：批成对 Dice 代价

#### 4.3.1 概念说明

BCE 对「每个点独立打分」，而分割更关心**整体重叠度**。Dice 系数度量两个集合的重合程度：

\[ \mathrm{Dice}(p, t) = \frac{2\,|p \cap t|}{|p| + |t|} \]

对概率化的预测（sigmoid 后）与 0/1 目标，交集用内积近似、并集用 L1 范数之和近似。Dice 对正负样本极不平衡（mask 前景占比常常只有几个百分点）比 BCE 更稳健：一个全零预测在 BCE 下大部分像素都「对了」，而 Dice 会直接给 1（最差）。匹配中把 BCE 与 Dice 两种代价加权相加，兼顾逐点精度与整体形状。

#### 4.3.2 核心流程

对 N 个预测、M 个目标（均已 point_sample 到 K 个点）：

\[ \mathrm{cost}_{dice}[n, m] = 1 - \frac{2 \langle p_n, t_m \rangle + 1}{\lVert p_n \rVert_1 + \lVert t_m \rVert_1 + 1} \]

- \( \langle p_n, t_m \rangle = \sum_k p_{nk} t_{mk} \)：概率化预测与 GT 在 K 个点上的内积（软交集），用 `einsum("nc,mc->nm")` 一次算完整张矩阵；
- 分子分母各 **+1** 是拉普拉斯平滑：防止空 mask（全 0）时分母为 0；
- 取 `1 −` 把「相似度」变成「代价」。

伪代码：

```text
p = sigmoid(inputs).flatten(1)          # (N, K)
t = targets.flatten(1)                  # (M, K)
numerator   = 2 * einsum("nc,mc->nm", p, t)          # (N, M)
denominator = p.sum(-1)[:, None] + t.sum(-1)[None, :]  # (N, 1)+(1, M) 广播
return 1 - (numerator + 1) / (denominator + 1)
```

#### 4.3.3 源码精读

[mask2former/modeling/matcher.py:L15-L30](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/matcher.py#L15-L30) —— `batch_dice_loss` 完整实现：

- 第 25 行：`inputs.sigmoid()` 概率化；
- 第 27 行：`einsum("nc,mc->nm")` 一次算出 N×M 个软交集；
- 第 28 行：广播相加得到 N×M 个软并集；
- 第 29 行：平滑 + 取 `1 −`；
- 第 33-35 行（[链接](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/matcher.py#L33-L35)）：JIT 脚本化。

对照记忆：训练损失里的逐对版本 `dice_loss` 在 [mask2former/modeling/criterion.py:L21-L45](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/criterion.py#L21-L45)，公式完全相同（同样 +1 平滑），只是分子分母用逐元素乘积/求和而非成对内积，最后 `.sum() / num_masks` 聚合成标量。

#### 4.3.4 代码实践

**实践 3：验证批成对 Dice 等价于朴素双循环**

1. **实践目标**：确认 `batch_dice_loss` 的 `(n, m)` 元素等于手工算的平滑 Dice 代价，理解 einsum 即「软交集」。
2. **操作步骤**：接实践 2 的环境，运行：

```python
# 示例代码：批成对 Dice 的等价性验证
import torch
from mask2former.modeling.matcher import batch_dice_loss

torch.manual_seed(1)
N, M, K = 4, 3, 10
x = torch.randn(N, K)
y = torch.randint(0, 2, (M, K)).float()

fast = batch_dice_loss(x, y)             # (N, M)

p, t = x.sigmoid(), y
naive = torch.zeros(N, M)
for n in range(N):
    for m in range(M):
        inter = (p[n] * t[m]).sum()
        naive[n, m] = 1 - (2 * inter + 1) / (p[n].sum() + t[m].sum() + 1)

print("最大绝对误差:", (fast - naive).abs().max().item())
```

3. **需要观察的现象**：误差为浮点精度量级。
4. **预期结果**：最大绝对误差应为 0 或 1e-7 量级。具体数值待本地验证。
5. 思考并验证：把 `y` 某一行全置 0（空 GT），对应列的代价是否仍是有限值？（平滑项保证了不会除零。）

#### 4.3.5 小练习与答案

**练习 1**：分子分母的 `+1` 起什么作用？
**答案**：拉普拉斯平滑。当预测与 GT 都是空 mask（全 0）时分母为 0，会得到 NaN；+1 保证分母恒正，代价有界。

**练习 2**：匹配中的 `batch_dice_loss` 与训练损失中的 `dice_loss` 有哪两点不同？
**答案**：① 形状：前者输出 `(N, M)` 成对矩阵（用于挑配对），后者对已配对的 (R, K) 逐元素算再 `.sum() / num_masks` 聚成标量；② 调用时机：前者在匹配器内对采样点算，后者在损失里对不确定度引导的采样点算（u5-l2 详述）。

**练习 3**：为什么一个全背景的预测（sigmoid 输出全接近 0）在 dice 代价下接近 1，而在 BCE 代价下可能不太大？
**答案**：Dice 为 1 − (0+1)/(0+|t|+1)，当 GT 前景点数不多时代价接近 1（最差）；而 BCE 对每个背景点都给小损失，前景点占比小则平均值不大。两者互补，所以匹配中同时使用（权重 5.0 + 5.0）。

---

### 4.4 memory_efficient_forward：匹配主流程

#### 4.4.1 概念说明

`HungarianMatcher.forward` 直接转调 `memory_efficient_forward`（[mask2former/modeling/matcher.py:L158-L179](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/matcher.py#L158-L179)），后者是 DETR 匹配器的省显存改造版。它要解决的矛盾是：

- 代价需要 mask 之间的比较，而 **GT mask 被 pad 到完整输入尺寸**（LSJ 下 1024×1024 ≈ 105 万像素），预测 mask 也在 1/4 分辨率（256×256）；
- 若逐像素算 Q×M 对代价，内存与计算都不可接受；
- **解法：只在 K=12544 个随机点上比较**——约为 GT 全像素的 1.2%，且用**同一组点**采样所有 mask，比较才公平、einsum 才能批处理。

「memory efficient」还体现在 `@torch.no_grad()`：匹配是离散指派，不需要也不应该有梯度，整个匹配过程不建计算图。

#### 4.4.2 核心流程

对 batch 中每张图 b（逐图独立匹配）：

```text
1. 分类代价:
   out_prob   = pred_logits[b].softmax(-1)        # (Q, K+1)
   cost_class = -out_prob[:, tgt_ids]             # (Q, M)，取 GT 类别列的负概率
2. mask 代价（共享随机采样点）:
   point_coords = torch.rand(1, K, 2)             # 一组归一化 (y, x) 坐标
   out_mask: (Q, H/4, W/4) --point_sample--> (Q, K)   # 预测 mask，1/4 分辨率
   tgt_mask: (M, H_pad, W_pad) --point_sample--> (M, K)  # GT mask，全分辨率
3. fp32 下计算两项批成对代价:
   cost_mask = batch_sigmoid_ce_loss(out_mask, tgt_mask)   # (Q, M)
   cost_dice = batch_dice_loss(out_mask, tgt_mask)         # (Q, M)
4. 加权合成 + 求解:
   C = w_mask·cost_mask + w_class·cost_class + w_dice·cost_dice
   C = C.reshape(Q, -1).cpu()
   indices.append(linear_sum_assignment(C))       # scipy 求解 LSAP
返回: [(index_i(int64), index_j(int64)), ...]  每张图一对张量
```

三个值得咀嚼的设计：

- **归一化坐标化解分辨率差**：`point_coords` 在 `[0,1]` 归一化空间采样，`point_sample` 按各自网格插值——预测 mask（256×256）和 GT mask（1024×1024）取到的是**同一相对位置**的值，代码里 `tgt_mask.to(out_mask)` 只对齐 dtype/device，完全不管尺寸。
- **分类代价是 NLL 的近似**：softmax 概率的负值 `-p[目标类]` 与交叉熵 \( -\log p \) 单调相关，省掉 log 且不改匹配结果（注释见 [L108-L110](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/matcher.py#L108-L110)）；softmax 在 K+1 维（含 no-object）上归一化，但只取真实类别列，no-object 通道不参与代价。
- **fp32 强制**：`autocast(enabled=False)` 包住两个损失计算，先 `.float()` 再算。

#### 4.4.3 源码精读

**入口与逐图循环。**
[mask2former/modeling/matcher.py:L95-L103](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/matcher.py#L95-L103) —— `@torch.no_grad()` 装饰（不建图、省显存）；取出 batch 与 query 数后 `for b in range(bs)` 逐图处理——每张图的 GT 集合独立，匹配自然按图为单位。

**分类代价。**
[mask2former/modeling/matcher.py:L105-L111](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/matcher.py#L105-L111) —— `softmax(-1)` 得 `(Q, K+1)` 概率；`-out_prob[:, tgt_ids]` 高级索引一步取出每个 query 对每个 GT 类别的负概率，得 `(Q, M)`。

**共享随机采样点 + point_sample。**
[mask2former/modeling/matcher.py:L113-L132](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/matcher.py#L113-L132) —— 两行关键的注释与代码：

- 第 117-118 行把预测/GT mask 各加一维变成 `(·, 1, H, W)`，这是 `point_sample` 期望的「带通道维」格式；
- 第 119-120 行 `point_coords = torch.rand(1, self.num_points, 2)` 只生成**一组**点，注释原话 "all masks share the same set of points for efficient matching!"——共享点使所有 mask 在同一坐标系下比较，且 `point_coords.repeat(...)` 后可一次批量插值；
- 第 122-132 行对预测与 GT 分别 `point_sample(...).squeeze(1)`，得到 `(Q, K)` 与 `(M, K)`。

**fp32 下的两项代价。**
[mask2former/modeling/matcher.py:L134-L141](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/matcher.py#L134-L141) —— `with autocast(enabled=False):` 内先 `.float()` 再调两个 JIT 代价函数。为什么：混合精度训练时（AMP 全局 autocast 开启），fp16 的 sigmoid/exp 动态范围只有约 ±65504、最小正规数约 6e-5，BCE 里 \( \log\sigma(x) \) 极易溢出/下溢；代价矩阵里几个 ulp 的误差就可能让本应匹配的对换位，造成监督目标抖动。所以这里显式退出 autocast 并升到 fp32（`from torch.cuda.amp import autocast` 见第 10 行）。

**合成、求解、返回。**
[mask2former/modeling/matcher.py:L143-L156](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/matcher.py#L143-L156) —— 三项代价加权求和成 `C`；`C.reshape(num_queries, -1).cpu()` 保证交给 scipy 的是 CPU 上的 2D 矩阵（此刻形状本来就是 `(Q, M)`，reshape 是防御性写法）；`linear_sum_assignment(C)` 返回 numpy 的 `(row_ind, col_ind)`；最后统一转成 `torch.int64` 张量对列表——`index_i` 是 query 下标、`index_j` 是 GT 下标，供 `SetCriterion` 的 `_get_src_permutation_idx / _get_tgt_permutation_idx`（[mask2former/modeling/criterion.py:L192-L202](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/criterion.py#L192-L202)）拆包成全局索引使用。

#### 4.4.4 代码实践

**实践 4（本讲主实践）：10 query × 3 GT 玩具匹配 + 手工复现 + 权重放大观察**

1. **实践目标**：
   - 跑通 `HungarianMatcher`，理解输入输出格式；
   - 手工复现代价矩阵与求解过程，验证与官方实现一致；
   - 把 `cost_mask` 放大 10 倍，观察匹配结果变化。
2. **操作步骤**：在仓库根目录把以下脚本存为 `matcher_playground.py` 并运行（需按 u1-l2 装好环境；若 CUDA 扩展未编译，可将脚本中 `from mask2former...` 的导入换成把 matcher.py 三个函数 + `point_sample` 导入拼进单文件）。

```python
# 示例代码：手工复现 HungarianMatcher 并观察权重敏感性
import torch
from scipy.optimize import linear_sum_assignment
from detectron2.projects.point_rend.point_features import point_sample
from mask2former.modeling.matcher import (
    HungarianMatcher, batch_sigmoid_ce_loss_jit, batch_dice_loss_jit,
)

Q, M, NCLS, H, W, P = 10, 3, 7, 24, 24, 128    # 7 个真实类 + 1 个 no-object
W_CLASS, W_MASK, W_DICE = 2.0, 5.0, 5.0         # 取自 COCO 全景 yaml

torch.manual_seed(0)                            # 只影响初始 logits/mask 的生成
pred_logits = torch.randn(1, Q, NCLS + 1)
pred_masks = torch.randn(1, Q, H, W)
gt_masks = torch.zeros(M, H, W)                 # 3 个互不重叠的方块 GT
gt_masks[0, :8, :8] = 1.0
gt_masks[1, 8:16, 8:16] = 1.0
gt_masks[2, 16:, 16:] = 1.0
labels = torch.tensor([1, 4, 6])
targets = [{"labels": labels, "masks": gt_masks}]
outputs = {"pred_logits": pred_logits, "pred_masks": pred_masks}

matcher = HungarianMatcher(cost_class=W_CLASS, cost_mask=W_MASK,
                           cost_dice=W_DICE, num_points=P)

# (a) 官方匹配。关键技巧：调用前重置种子，使内部 torch.rand 可复现
torch.manual_seed(42)
qi, gi = matcher(outputs, targets)[0]
print("官方匹配: query", qi.tolist(), "-> gt", gi.tolist())

# (b) 手工复现代价矩阵（与 memory_efficient_forward 逐行对应）
torch.manual_seed(42)                           # 重置到同一状态 → 同一组采样点
out_prob = pred_logits[0].softmax(-1)
cost_class = -out_prob[:, labels]               # (Q, M)

out_mask, tgt_mask = pred_masks[0][:, None], gt_masks[:, None]
point_coords = torch.rand(1, P, 2)              # 此后第一个随机调用，与 matcher 内部一致
out_mask = point_sample(out_mask, point_coords.repeat(Q, 1, 1),
                        align_corners=False).squeeze(1)      # (Q, P)
tgt_mask = point_sample(tgt_mask, point_coords.repeat(M, 1, 1),
                        align_corners=False).squeeze(1)      # (M, P)
cost_mask = batch_sigmoid_ce_loss_jit(out_mask.float(), tgt_mask.float())
cost_dice = batch_dice_loss_jit(out_mask.float(), tgt_mask.float())
C = (W_MASK * cost_mask + W_CLASS * cost_class + W_DICE * cost_dice)
C = C.reshape(Q, -1).cpu()
row, col = linear_sum_assignment(C)
print("手工匹配: query", row.tolist(), "-> gt", col.tolist())
print("与官方一致:", row.tolist() == qi.tolist() and col.tolist() == gi.tolist())

# (c) cost_mask 放大 10 倍
matcher10 = HungarianMatcher(cost_class=W_CLASS, cost_mask=W_MASK * 10,
                             cost_dice=W_DICE, num_points=P)
torch.manual_seed(42)
qi10, gi10 = matcher10(outputs, targets)[0]
print("cost_mask x10 匹配: query", qi10.tolist(), "-> gt", gi10.tolist())
```

3. **需要观察的现象**：
   - 官方与手工的匹配下标完全一致（种子技巧生效的前提是：两次 `manual_seed(42)` 之后，`torch.rand` 都是各自的第一个随机操作——softmax、索引、point_sample 均不消耗随机数）；
   - 打印 `C` 观察：分类代价的量级（概率在 1/8 附近，代价约 −0.125～0 量级）远小于 mask 代价，本例中匹配主要由 mask/dice 项主导；
   - `cost_mask ×10` 后匹配可能改变（随机 logits 下两份代价矩阵的相对排序不同）。
4. **预期结果**：`(b)` 的一致性检查应打印 `True`；`(a)(c)` 的具体下标组合依赖随机权重，待本地验证——重点不是某组具体数字，而是理解「匹配 = 在加权代价矩阵上解 LSAP」。
5. 若一致性为 `False`：检查两处 `manual_seed(42)` 之间是否插入了其他随机操作（如 `torch.randn`），或对照 [L103-L151](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/matcher.py#L103-L151) 逐步核对复现顺序。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `memory_efficient_forward` 要用 `@torch.no_grad()` 装饰？匹配结果的梯度如何传回网络？
**答案**：指派本身是离散的、不可微；no_grad 使匹配过程不建计算图、省显存。梯度路径是：匹配给出 (query, GT) 配对 → 在配对上算分类/BCE/Dice 损失 → 损失对预测可微，反传时每个 query 收到「它被指派的那个 GT」的监督。

**练习 2**：所有 mask 共享同一组随机采样点，好处是什么？
**答案**：① 公平——同一坐标系下逐点对齐，比较才有意义（各自独立采样会引入纯随机方差）；② 高效——`point_coords.repeat(N,1,1)` 后可对 N 张 mask 一次批量双线性插值，且 einsum 内积要求两条序列在同一组点上取值。

**练习 3**：`autocast(enabled=False)` 加 `.float()` 防的是什么问题？
**答案**：AMP 混合精度下 fp16 的 sigmoid/BCE 数值动态范围小（最大约 65504、最小正规数约 6e-5），容易溢出或下溢；代价矩阵的微小误差可能改变 LSAP 的最优解，造成匹配抖动、监督目标不稳定。因此强制在 fp32 中计算两项 mask 代价。

**练习 4**：`C.reshape(num_queries, -1)` 中 `-1` 是多少？这一步是多余的吗？
**答案**：`M`（GT 数）。此刻 `C` 已经是 `(Q, M)` 二维，reshape 不改变数值，属于防御性写法，确保交给 `linear_sum_assignment` 的是它期望的 2D 矩阵。

---

## 5. 综合实践

**任务：代价矩阵「透视」与权重敏感性扫描**

把匹配器当成一个可解剖的黑盒：对同一组玩具预测，打印三项代价子矩阵与合成矩阵，扫描不同权重下的匹配变化，最终能用自己的话回答「谁在主导匹配」。

步骤：

1. 复用 4.4.4 实践中的 `outputs / targets` 与种子技巧，取 `torch.manual_seed(42)` 后手工构造 `cost_class / cost_mask / cost_dice` 三张 `(Q, M)` 矩阵。
2. 用 `torch.set_printoptions(precision=3, sci_mode=False)` 后分别打印三张矩阵及合成矩阵 `C`，肉眼找到每列（每个 GT）代价最小的 query。
3. 对下面 4 组权重分别求匹配并列表记录（每组求解前不重置种子也没关系——矩阵已物化，不再依赖随机采样）：

| 组 | cost_class | cost_mask | cost_dice | 预期主导因素 |
| --- | --- | --- | --- | --- |
| A | 2.0 | 5.0 | 5.0 | mask + dice（COCO 默认） |
| B | 2.0 | 50.0 | 5.0 | 逐点 BCE |
| C | 2.0 | 5.0 | 50.0 | 整体重叠 |
| D | 200.0 | 5.0 | 5.0 | 分类概率 |

4. 观察并解释：A/B/C 三组的匹配是否几乎不变（mask 两项高度相关）？D 组是否更可能改变（分类项量级被放大百倍后压过 mask 项）？
5. （可选）用 matplotlib 把 4 组合成矩阵画成热力图、匹配对画成连线；没有 matplotlib 时用文本矩阵即可。

预期现象与结论（具体匹配下标待本地验证）：BCE 与 Dice 两项都在度量 mask 相似性，单独放大其中一项通常不足以翻转匹配；而把分类权重放大两个数量级后，匹配会明显向「分类概率最高」的 query 倾斜。这也解释了官方配置为何把三个权重设为同一量级（2.0/5.0/5.0）——任何一项压倒性主导都会让另外两项失效。

## 6. 本讲小结

- Mask2Former 的 mask classification 范式输出固定 Q 个预测、GT 数量不定，必须先用**匈牙利匹配（LSAP）**把 query 与 GT 一一配对，未匹配 query 由 no-object 通道监督——匹配对预测集合的排列不变。
- 代价矩阵由三项加权合成：**分类代价**（softmax 概率取负，是 NLL 的免 log 近似）、**mask BCE 代价**与 **Dice 代价**（后两者都是 `einsum("nc,mc->nm")` 算出的 `(Q, M)` 批成对矩阵）。
- 省显存的核心技巧：**所有 mask 共享一组 `torch.rand` 归一化随机采样点**（K=12544，约为 GT 全像素的 1.2%），`point_sample` 的归一化坐标天然化解预测 mask（1/4 分辨率）与 GT mask（padding 全尺寸）的分辨率差异。
- 两个工程细节：`@torch.no_grad()` 让离散指派不建计算图；`autocast(enabled=False)` + `.float()` 强制 fp32 计算，防止混合精度误差导致匹配抖动。
- 匹配器由 `MaskFormer.from_config` 用与损失相同的权重三元组构建（COCO 全景：class 2.0 / mask 5.0 / dice 5.0），在 `SetCriterion.forward` 中对主输出与每层 aux 输出各调用一次（DEC_LAYERS=10 时一次训练前向匹配 10 次）。

## 7. 下一步学习建议

匹配只解决了「谁对谁」，下一讲 **u5-l2：SetCriterion 与逐点 mask 损失**将接续精读 [mask2former/modeling/criterion.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/criterion.py#L212-L247)：匹配结果如何被拆包成 `batch_idx / src_idx / tgt_idx`、训练损失里的采样点与匹配器的均匀随机采样有何不同（不确定度引导的 K+β 重要性采样）、`empty_weight` 如何给 no-object 降权。建议先带着一个问题去读：**为什么匹配用均匀采样、算损失却要用不确定度引导采样？**

随后可阅读 **u5-l3：深监督与损失权重流转**，看 `weight_dict` 如何展开成 30 个带 `_i` 后缀的损失键。
