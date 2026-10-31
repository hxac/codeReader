# 匹配器 SparseMatcher 与代价函数

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清「匹配（matching）」在集合预测式分割里要解决什么问题，以及为什么 UniSeg3D 的超点查询**不需要**传统 DETR 式的全局匈牙利匹配。
2. 读懂三种代价函数 `QueryClassificationCost`、`MaskBCECost`、`MaskDiceCost` 的数学定义与源码实现，理解配置中三个 `weight` 的作用。
3. 逐行理解 `SparseMatcher` 的两个核心技巧：**inf 掩码**（把 `query_masks` 禁止的组合填成 1e8）与 **topk+1 阈值**（用第 `topk+1` 小的代价做严格小于筛选），并掌握其返回格式。
4. 能用一个小矩阵手工算出匹配结果，并与代码输出互相验证。

## 2. 前置知识

**匹配是什么？** 掩码预测模型每次前向会输出 N 条查询（query）的预测：每条查询有一个分类 logits 和一个掩码 logits。而一个场景里只有 K 个真实物体（GT）。训练时要回答：「哪条查询的预测对到哪个 GT 上去算损失？」——这个「预测↔GT」的指派问题就叫匹配。

**二分图匹配与匈牙利算法。** 在 DETR 一类的方法里，查询是可学习参数，与物体没有任何先验关联，任何查询都可能预测任何物体。因此匹配被建模为二分图上的最优指派：构造一个 N×K 的**代价矩阵**（cost matrix），每个元素是把查询 q 指派给 GT g 的代价；再求一个「每个 GT 恰好分到一条查询、每条查询至多分给一个 GT」的全局最优方案。经典解法是匈牙利算法（`scipy.optimize.linear_sum_assignment`），复杂度约 O(n³)。

**代价函数。** 代价矩阵的每个元素怎么算？通常取「负的预测质量」：预测分类越像该 GT 的类别、预测掩码越像该 GT 的掩码，代价越小。DETR 系常用分类代价 + 掩码（BCE/Dice）代价的加权和。

**UniSeg3D 的先验。** 前面讲义（u4-l1、u4-l3）已经建立：UniSeg3D 的查询直接采样自超点特征（「超点即查询」），因此**每条查询天生知道自己在空间里属于哪个（哪些）物体**。这个归属信息就是 `query_masks`：一个形状为 `(n_gts, n_queries)` 的布尔矩阵，`query_masks[g, q] = True` 表示查询 q 的超点落在 GT 物体 g 内。有了它，匹配问题从「全局二分匹配」退化成「每个 GT 在它覆盖的少量查询里挑最好的」，这正是本讲的主角 `SparseMatcher` 名字中 Sparse（稀疏）的含义。

**几个张量术语。** logits 指未归一化的原始输出；softmax 把一组 logits 变成概率分布（用于分类）；sigmoid 把单个 logits 压到 (0,1)（用于逐点二分类）；BCE（binary cross-entropy）衡量二分类预测与 0/1 标签的差异；Dice 衡量两个集合的重叠度（1 表示完全重合）。

## 3. 本讲源码地图

| 文件 | 本讲关注点 |
| --- | --- |
| `uniseg3d/instance_criterion.py` | 三种代价类与 `SparseMatcher` 全部在此文件；另有它们在 `OVInstanceCriterion` 中的调用点 |
| `uniseg3d/unified_criterion.py` | 损失总调度如何把 `query_masks` 切片后传给实例损失 |
| `uniseg3d/uniseg3d.py` | `_select_queries`：`query_masks` 的出生地 |
| `configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py` | matcher / ov_matcher 的配置与 `query_thr`、`iter_matcher` |

一条数据流贯穿本讲：

```text
_select_queries (模型侧)
   └─ 随机采样查询 → query_masks = sp_masks[:, ids]
ScanNetUnifiedCriterion (总调度)
   └─ 切掉末尾 21 个语义行 → inst_gt.query_masks
OVInstanceCriterion (实例损失)
   ├─ matcher(pred, gt)      ← base 实例：3 种代价
   └─ ov_matcher(pred, gt)   ← novel 伪掩码：2 种代价
        两者都是 SparseMatcher 实例
```

## 4. 核心概念与源码讲解

### 4.1 超点查询自带归属：为什么可以不用匈牙利算法

#### 4.1.1 概念说明

DETR 式匹配之所以需要匈牙利算法，根源在于**可学习查询与物体零先验**：代价矩阵里所有 N×K 个组合都必须纳入考虑，还要附加「一对一」约束，这只能靠全局最优指派求解。

UniSeg3D 的查询是从超点特征直接采样来的（u4-l3）。一个超点是空间上一小块连贯表面，标注构造（u3-l3）已经决定了它属于哪个实例。于是对每个 GT g，合法候选查询只有「落在 g 内的那几十上百个超点」，而不是全部数千查询。匹配的搜索空间从 N×K 的大矩阵塌缩成 K 个小集合——**每个 GT 独立地在自己覆盖的查询里挑代价最小者即可，GT 之间互不干扰**。代价矩阵上绝大多数格子根本不用看，这就是「稀疏匹配」。

一个佐证藏在源码顶部：`instance_criterion.py` 第一方依赖里 import 了匈牙利算法，但整个文件（以及 `point_prompt_instance_criterion.py`、`text_prompt_instance_criterion.py`、`contrastive_criterion.py`）从未调用它——这是从匈牙利匹配版本演化到稀疏匹配后留下的痕迹。

#### 4.1.2 核心流程

`query_masks` 的生成与传递链路：

1. 模型 `loss()` 前向中调用 `_select_queries`：训练时按比例 `query_thr`（配置为 0.5）到 1 之间随机保留一部分超点作为查询（上限 3500）。
2. 被选中的超点 id 记为 `ids`，GT 掩码 `sp_masks`（形状 `(n_gts, n_全部超点)`）按这些 id 取**列子集**，得到 `query_masks`（形状 `(n_gts, n_存活查询)`）。
3. 总调度 `ScanNetUnifiedCriterion` 把 `query_masks` 连同 `sp_masks`、`labels_3d`、`is_novel` 一起切掉末尾 21 个语义行，只留实例行，装入 `inst_gt`。
4. `OVInstanceCriterion` 把 `query_masks` 挂到 `gt_instances` 上传给 matcher。

写成公式：设存活查询集合为 \( Q \)，则

\[
\text{query\_masks}[g, q] = \text{sp\_masks}[g, q] \iff q \in Q
\]

即 query_masks 就是 GT 掩码在「存活查询坐标系」上的投影。这里有个重要的结构性质（承接 u3-l3）：`gt_sp_masks` 的每一列（每个超点）在实例行中**至多一个 True**——超点不跨实例。因此 `query_masks.T` 的每一行（每条查询）也至多一个 True，base 实例的匹配天然近似一对一。

#### 4.1.3 源码精读

query_masks 的出生地——`_select_queries`：

[uniseg3d/uniseg3d.py:L243-L256](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L243-L256)

```python
for i in range(len(x)):
    if self.query_thr < 1:
        n = (1 - self.query_thr) * torch.rand(1) + self.query_thr
        n = (n * len(x[i])).int()
        n = torch.as_tensor(min(n, 3500)).item()
        ids = torch.randperm(len(x[i]))[:n].to(x[i].device)
        queries.append(x[i][ids])
        gt_instances[i].query_masks = gt_instances[i].sp_masks[:, ids]
    else:
        queries.append(x[i])
        gt_instances[i].query_masks = gt_instances[i].sp_masks
```

这段代码按随机比例（本配置 `query_thr=0.5`，见 [configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:L28](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L28)）保留超点作查询，关键是 `sp_masks[:, ids]` 这一列切片——查询随机抽样之后，GT 必须同步换到同一个坐标系，否则监督信号就指错了对象。

总调度的切片传递：

[uniseg3d/unified_criterion.py:L35-L41](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_criterion.py#L35-L41)

```python
inst_gt = InstanceData_()
inst_gt.sp_masks = insts[i].sp_masks[:-n - 1, :]
inst_gt.labels_3d = insts[i].labels_3d[:-n - 1]
inst_gt.is_novel = insts[i].is_novel[:-n - 1]
if insts[i].get('query_masks') is not None:
    inst_gt.query_masks = insts[i].query_masks[:-n - 1, :]
```

u5-l1 讲过 `[:-n-1]` 是把末尾 21 个语义行切掉、只留实例行；`query_masks` 与 `sp_masks` 行对齐，做同样的行切片后一起下发。

匈牙利算法的残留 import：

[uniseg3d/instance_criterion.py:L4](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L4)

```python
from scipy.optimize import linear_sum_assignment
```

整个仓库没有任何地方调用它，可以视为历史痕迹——它提示这套损失代码的祖先是标准 DETR 式匈牙利匹配。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：亲手确认「query_masks 是 sp_masks 的列子集视图」以及「每条查询至多属于一个 base 实例」。
2. **操作步骤**：
   - 阅读 `_select_queries`（上文链接），注意 `ids` 同时被用于两处：`x[i][ids]` 取查询特征、`sp_masks[:, ids]` 取 GT 列子集。
   - 用 `grep -n "linear_sum_assignment" uniseg3d/*.py` 确认它只出现在 import 行。
3. **需要观察的现象**：`ids` 在两处出现保证查询与 GT 对齐；grep 结果应只有 4 个文件的 import 行。
4. **预期结果**：确认匹配所需的一切先验（归属关系）在进入 matcher 之前就已就位；匈牙利算法确实未被使用。
5. 结论可直接从源码得出，无需运行（grep 部分待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：如果 `query_thr=1.0`，`query_masks` 会是什么？

**答案**：走 `else` 分支，`query_masks = sp_masks` 全量赋值——所有超点都当查询，无需列切片，query_masks 与 sp_masks 完全相同。

**练习 2**：为什么查询随机采样后 GT 必须做 `[:, ids]` 列切片，而不是照抄原始 `sp_masks`？

**答案**：matcher 输出的查询下标 `idx_q` 是相对于**存活查询序列**的编号（如第 0~999 个），下游 `mask[idx_q]` 用它索引预测掩码的行。若 GT 仍用全量超点坐标系，同一个数字会指向错误的超点，监督信号整体错位。

**练习 3**：传统 DETR 匹配要附加「一对一」约束，SparseMatcher 形式上允许一个查询出现在多个 (q, g) 对里。为什么在 base 实例上这几乎不会发生？

**答案**：u3-l3 的超点标注构造（one-hot → scatter_mean → 严格 >0.5）保证每个超点至多属于一个实例，即 `query_masks` 的每列在 base 实例行内至多一个 True；一个查询（一个超点）合法候选至多一个 base GT。novel 伪掩码行来自外部分割（SAM3D），可能与 base 行共享超点，这一交叉由 u5-l3 将讲的「先 base 后 novel」两阶段匹配规避（源码中有 `assert torch.isin(...)` 断言两路查询不相交）。

### 4.2 三种代价函数：给每个候选组合打分

#### 4.2.1 概念说明

matcher 需要一个标量来回答「把查询 q 指派给 GT g 有多不合适」。UniSeg3D 用三个可加权求和的部分：

1. **QueryClassificationCost**：查询 q 的分类分布里，GT 类别概率越高，代价越低。它只用分类 logits 与 GT 标签。
2. **MaskBCECost**：查询 q 的掩码 logits 与 GT g 的掩码做逐点二值交叉熵，衡量「逐点预测的贴合度」。
3. **MaskDiceCost**：掩码层面的整体重叠度（Dice），弥补 BCE 偏向逐点、对整体形状不敏感的弱点。

三者都是「可调用对象」，接口统一为 `(pred_instances, gt_instances) -> Tensor(n_queries, n_gts)`，各自只从 InstanceData_ 里取自己需要的字段。注意三者都作用在**原始 logits**上，不做 argmax 之类的不可导操作——不过匹配本身在 `torch.no_grad()` 下执行（见 4.3.3），代价只是打分，不回传梯度。

#### 4.2.2 核心流程

分类代价（对单组合 (q, g)）：

\[
c^{cls}_{qg} = -w_{cls} \cdot \mathrm{softmax}(z_q)_{l_g}
\]

其中 \( z_q \) 是查询 q 的分类 logits，\( l_g \) 是 GT g 的类别 id，\( w_{cls} \) 是配置权重。概率 ∈ (0,1)，故该项 ∈ (-w_cls, 0)。

掩码 BCE 代价。设 \( z_{qi} \) 为查询 q 在点 i 的掩码 logits，\( t_{gi} \in \{0,1\} \) 为 GT 掩码，\( \ell(z, y) \) 为 BCE：

\[
c^{bce}_{qg} = \frac{w_{bce}}{N} \sum_{i=1}^{N} \big[\, t_{gi}\,\ell(z_{qi}, 1) + (1 - t_{gi})\,\ell(z_{qi}, 0) \,\big]
\]

掩码 Dice 代价。\( \sigma \) 为 sigmoid，分子分母各 +1 平滑：

\[
c^{dice}_{qg} = w_{dice} \Big( 1 - \frac{2\sum_i \sigma(z_{qi})\, t_{gi} + 1}{\sum_i \sigma(z_{qi}) + \sum_i t_{gi} + 1} \Big)
\]

掩码完全重合时 Dice 代价趋于 0，完全错开时趋于 1。三路求和即总代价矩阵 \( C = c^{cls} + c^{bce} + c^{dice} \)。

关键实现技巧：三者的「逐对」计算不是双重循环，而是 **einsum 一次算出整个 N×K 矩阵**。以 BCE 为例，先把每个 (query, 点) 的正/负损失都算出来，再用 `torch.einsum('nc,mc->nm', pos, targets)` 对点维求和——这等于同时算完所有 query-GT 组合的求和项。

#### 4.2.3 源码精读

两个批量化代价的底层函数：

[uniseg3d/instance_criterion.py:L10-L27](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L10-L27)

```python
def batch_sigmoid_bce_loss(inputs, targets):
    pos = F.binary_cross_entropy_with_logits(
        inputs, torch.ones_like(inputs), reduction='none')
    neg = F.binary_cross_entropy_with_logits(
        inputs, torch.zeros_like(inputs), reduction='none')

    pos_loss = torch.einsum('nc,mc->nm', pos, targets)
    neg_loss = torch.einsum('nc,mc->nm', neg, (1 - targets))
    return (pos_loss + neg_loss) / inputs.shape[1]
```

`pos` 是「把每个点都当正样本」的逐点损失，`neg` 是「都当负样本」的；GT 为 1 的点取 pos、为 0 的点取 neg，两次 einsum 按 GT 的 0/1 加权求和恰好完成这个选择，除以点数 N 得均值。

[uniseg3d/instance_criterion.py:L30-L44](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L30-L44)

```python
def batch_dice_loss(inputs, targets):
    inputs = inputs.sigmoid()
    numerator = 2 * torch.einsum('nc,mc->nm', inputs, targets)
    denominator = inputs.sum(-1)[:, None] + targets.sum(-1)[None, :]
    loss = 1 - (numerator + 1) / (denominator + 1)
    return loss
```

einsum 算出所有组合的交集 \( \sum_i \sigma(z_{qi}) t_{gi} \)，分母按行/列广播求和得到并集。

三个代价类本体（结构完全同构：构造存权重，调用算矩阵）：

[uniseg3d/instance_criterion.py:L646-L670](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L646-L670)

```python
@TASK_UTILS.register_module()
class QueryClassificationCost:
    def __call__(self, pred_instances, gt_instances, **kwargs):
        scores = pred_instances.scores.softmax(-1)
        cost = -scores[:, gt_instances.labels]
        return cost * self.weight
```

`scores` 形状 `(n_queries, n_classes+1)`（末列是背景类），softmax 后按 GT 标签花式索引取对应类概率，取负——「越像你，配你越便宜」。

[uniseg3d/instance_criterion.py:L673-L697](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L673-L697) 的 `MaskBCECost` 核心是 `cost = batch_sigmoid_bce_loss(pred_instances.masks, gt_instances.masks.float())`；[uniseg3d/instance_criterion.py:L700-L724](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L700-L724) 的 `MaskDiceCost` 核心是 `cost = batch_dice_loss(...)`，末尾都乘 `self.weight`。

配置中的权重（同一配置文件 L76-L88）：

[configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:L76-L88](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L76-L88)

```python
matcher=dict(
    type='SparseMatcher',
    costs=[
        dict(type='QueryClassificationCost', weight=0.5),
        dict(type='MaskBCECost', weight=1.0),
        dict(type='MaskDiceCost', weight=1.0)],
    topk=1),
ov_matcher=dict(
    type='SparseMatcher',
    costs=[
        dict(type='MaskBCECost', weight=1.0),
        dict(type='MaskDiceCost', weight=1.0)],
    topk=1),
```

注意 `ov_matcher`（供 novel 伪掩码二次匹配用）**没有分类代价**——novel 行的 `labels_3d` 只是占位（u3-l3），分类不可信，损失侧也注明「ov has no cls loss」。

#### 4.2.4 代码实践

1. **实践目标**：手工复现一个 2 查询 × 2 GT 的 BCE 与 Dice 代价矩阵，与函数输出对到小数点后几位。
2. **操作步骤**（示例代码，保存为独立脚本运行）：

```python
# 示例代码：manual_cost.py
import torch
from uniseg3d.instance_criterion import batch_sigmoid_bce_loss, batch_dice_loss

torch.manual_seed(0)
z = torch.randn(2, 6)          # 2 条查询 × 6 个点的掩码 logits
t = torch.tensor([[1., 0., 1., 1., 0., 0.],
                  [0., 0., 1., 0., 1., 1.]])  # 2 个 GT 掩码

bce = batch_sigmoid_bce_loss(z, t)
dice = batch_dice_loss(z, t)

# 手工验证 (q=0, g=0)：BCE 按 GT 的 0/1 选 pos/neg 再平均
import torch.nn.functional as F
manual = (F.binary_cross_entropy_with_logits(z[0], t[0], reduction='none')).mean()
print(bce[0, 0], manual)        # 两数应一致
```

3. **需要观察的现象**：`bce[0,0]` 与 `manual` 完全相等；`dice` 的值域落在 (0, 1)。
4. **预期结果**：理解「einsum 批量化 = 对每个组合独立算损失再排成矩阵」，两种算法数值恒等。
5. 具体数值依赖随机种子，待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么需要 Dice 代价，只用 BCE 不够吗？

**答案**：BCE 是逐点平均，一个大物体掩码即使一半点预测正确，BCE 也可能不高；Dice 以整体交集/并集衡量形状重叠，对「掩码整体是否罩住物体」更敏感。两者互补，配置里权重同为 1.0。

**练习 2**：`QueryClassificationCost` 里若不取负号会怎样？

**答案**：代价变成「越像越贵」，topk 会选出**最不像**该 GT 的查询去监督它，训练方向完全反掉。匹配类代价必须是「负质量」。

**练习 3**：把 `MaskBCECost` 的 weight 从 1.0 调到 100，匹配结果会偏向什么？

**答案**：总代价几乎由 BCE 项主导，匹配结果趋向「逐点预测最贴合的查询」，分类概率与 Dice 形状的影响被淹没。这正是综合实践（第 5 节）要观察的现象。

### 4.3 SparseMatcher：inf 掩码 + topk+1 阈值

#### 4.3.1 概念说明

`SparseMatcher` 完成最后一步：在 `query_masks` 允许的组合里，为每个 GT 挑出代价最小的至多 `topk` 条查询。它不用匈牙利算法，而是两个张量技巧的组合：

- **inf 掩码**：把所有被禁止的组合的代价直接改写成 `self.inf = 1e8`。三种代价都是有限值（分类项 ∈ (-0.5, 0)，BCE/Dice 项有界），1e8 大到必然落选，等效于「这些格子不存在」。
- **topk+1 阈值**：对每个 GT 列，取代价最小的 `topk+1` 个值中的最后一个（即第 `topk+1` 小）当阈值，严格小于阈值的组合入选。入选数天然 ≤ topk。

这两个技巧让「带禁止约束的每列独立 top-k 选择」变成几行无梯度的张量操作，完全可并行。

#### 4.3.2 核心流程

设第 g 列（GT g）的**允许**代价集合为 \( C_g \)（有限值，\( m_g = |C_g| \)，即 GT g 覆盖的存活查询数），k = topk+1。topk+1 阈值的形式化定义：

\[
\tau_g = \begin{cases} C_g^{(k)} & m_g \ge k \quad(\text{第 } k \text{ 小的允许代价}) \\ +\infty & m_g < k \end{cases}
\]

最终匹配集合：

\[
M = \{\, (q, g) \;:\; c_{q,g} < \tau_g \,\}
\]

分情况验证其行为：

| 情形 | 阈值 τ_g | 该列入选数 |
| --- | --- | --- |
| 允许组合充足（m ≥ k） | 第 k 小的有限值 | 至多 k−1 = topk 个 |
| 允许组合不足（0 < m < k） | +∞（topk 结果里混入 inf） | 全部 m 个 |
| 无允许组合（m = 0） | +∞（整列 inf） | 0 个 |

两种精妙之处：

1. **inf 掩码自动适配「允许数不足」**：`torch.topk` 不足 k 个有限值时会取到被填的 inf，阈值自然变成 inf，于是所有有限代价（哪怕只有一个）都能入选——不需要单独写分支处理稀疏情况。
2. **严格小于（`<`）而非 `<=`**：若第 topk 与第 topk+1 小的代价**并列**，用 `<=` 会把并列者全部带进来超过 topk；用 `<` 则并列者全部落选，入选数可能少于 topk，但保证不超、且行为确定。

返回格式也值得注意：不是 mmdet 系常见的 `List[Tuple[src_idx, tgt_idx]]`，而是一维的 `(query_ids, gt_ids)` 两个张量，`ids[:, 0]` 是查询下标、`ids[:, 1]` 是 GT 下标，逐元素对齐。

#### 4.3.3 源码精读

完整类定义：

[uniseg3d/instance_criterion.py:L726-L776](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L726-L776)

构造函数——代价列表同样走注册器构建：

[uniseg3d/instance_criterion.py:L735-L740](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L735-L740)

```python
def __init__(self, costs, topk):
    self.topk = topk
    self.costs = []
    self.inf = 1e8
    for cost in costs:
        self.costs.append(TASK_UTILS.build(cost))
```

`costs` 是配置里的字典列表，每个经 `TASK_UTILS.build` 实例化——又一次「配置即代码」（u2-l1）。

主体（`@torch.no_grad()` 包裹，匹配不参与梯度）：

[uniseg3d/instance_criterion.py:L759-L776](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L759-L776)

```python
labels = gt_instances.labels
n_gts = len(labels)
if n_gts == 0:
    return labels.new_empty((0,)), labels.new_empty((0,))

cost_values = []
for cost in self.costs:
    cost_values.append(cost(pred_instances, gt_instances))
# of shape (n_queries, n_gts)
cost_value = torch.stack(cost_values).sum(dim=0)
cost_value = torch.where(
    gt_instances.query_masks.T, cost_value, self.inf)

values = torch.topk(
    cost_value, self.topk + 1, dim=0, sorted=True,
    largest=False).values[-1:, :]
ids = torch.argwhere(cost_value < values)
return ids[:, 0], ids[:, 1]
```

逐行解读：

- **空 GT 早退**：场景里没有 base 实例时返回两个空张量，下游 `len(idx_q) == 0` 分支会跳过掩码损失。
- **代价求和**：三种代价各得 `(n_queries, n_gts)` 矩阵，`stack().sum(0)` 加权求和（权重已乘进各代价）。
- **inf 掩码**：`query_masks` 形状 `(n_gts, n_queries)`，转置后与代价矩阵同形；`torch.where(允许, 真实代价, 1e8)` 把禁止组合一律抬到天文数字。
- **topk+1 阈值**：`torch.topk(..., k=topk+1, dim=0, largest=False, sorted=True)` 取每列最小的 k 个升序排列，`.values[-1:, :]` 切出最后一行并保持 `(1, n_gts)` 形状以便广播，即每列第 k 小值。
- **严格筛选**：`argwhere(cost < values)` 收集所有严格小于阈值的坐标，按列/行拆成两个下标张量返回。

一个隐含前提：`torch.topk` 要求 k ≤ 维度长度，这里 dim=0 是查询数。topk+1=2，只要场景查询数 ≥ 2 即满足；实际查询数是数百到 3500（u4-l1），不会触发边界。

匹配器的调用点（base 路径，位于 `OVInstanceCriterion.__call__`）：

[uniseg3d/instance_criterion.py:L384-L395](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L384-L395)

```python
indices = []
for i in range(len(base_insts)):
    pred_instances = InstanceData_(
        scores=cls_preds[i],
        masks=pred_masks[i])
    gt_instances = InstanceData_(
        labels=base_insts[i].labels_3d,
        masks=base_insts[i].sp_masks)
    if base_insts[i].get('query_masks') is not None:
        gt_instances.query_masks = base_insts[i].query_masks
    indices.append(self.matcher(pred_instances, gt_instances))
```

注意字段名的搬运：解码器输出的 `cls_preds`/`pred_masks` 被包成 `scores`/`masks`，GT 的 `labels_3d`/`sp_masks` 被包成 `labels`/`masks`——三个代价类按这些字段名各取所需。逐场景（batch 内每个样本）独立匹配。

关于迭代匹配：配置中 `iter_matcher=True`（[configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:L99](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L99)），意味着 [uniseg3d/instance_criterion.py:L632-L637](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L632-L637) 中每层 aux 输出会把 `indices=None` 传入 `get_layer_loss`，由 [uniseg3d/instance_criterion.py:L153-L164](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L153-L164) **逐层重新匹配**——解码器共出七份预测（u4-l3），每层的预测不同，匹配也随层刷新。

novel 路径的 ov_matcher 调用：

[uniseg3d/instance_criterion.py:L446-L464](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L446-L464)

```python
if insts[j].get('query_masks') is not None:
    ov_inst.query_masks = insts[j].query_masks[is_novel][:, mask_]
...
ov_indice = self.ov_matcher(ov_pred_instances, ov_gt_instances)
ori_ov_query_indice = mask_maps[j][ov_indice[0]]
```

base 匹配命中的查询先被 `mask_` 剔除，剩余查询上再做 novel 匹配；`query_masks` 必须同步做 `[:, mask_]` **列重索引**，换到「剩余查询坐标系」——与 4.1 里采样后的列切片同理。novel 匹配结果还要经 `mask_maps[j]` 映射回原始查询编号。细节留给 u5-l3。

#### 4.3.4 代码实践（本讲主实践）

1. **实践目标**：构造 5 查询 × 3 GT 的小矩阵，禁止约一半组合，手工算三类代价并运行 `SparseMatcher`，验证被禁止的组合绝不出现在返回的 indices 中。
2. **操作步骤**（示例代码，保存为独立脚本运行；需已完成 u1-l2 环境）：

```python
# 示例代码：practice_sparse_matcher.py
import torch
import uniseg3d  # 触发注册（u2-l1：注册是 import 的副作用）
from mmdet3d.registry import TASK_UTILS
from uniseg3d.structures import InstanceData_

torch.manual_seed(0)
n_q, n_g, n_pts = 5, 3, 12

# 1) 预测：scores (5, 20) 分类 logits；masks (5, 12) 掩码 logits
pred = InstanceData_(
    scores=torch.randn(n_q, 20),
    masks=torch.randn(n_q, n_pts) * 3)

# 2) GT：labels (3,)；masks (3, 12) 布尔；query_masks (3, 5) 禁止一半组合
gt_masks = torch.zeros(n_g, n_pts, dtype=torch.bool)
gt_masks[0, [0, 1, 2, 3]] = True
gt_masks[1, [4, 5, 6]] = True
gt_masks[2, [7, 8, 9, 10, 11]] = True
gt = InstanceData_(labels=torch.tensor([2, 7, 15]), masks=gt_masks)

query_masks = torch.zeros(n_g, n_q, dtype=torch.bool)
query_masks[0, [0, 1]] = True   # gt0 只许查询 0/1
query_masks[1, [2]] = True      # gt1 只许查询 2
query_masks[2, [3, 4]] = True   # gt2 只许查询 3/4
gt.query_masks = query_masks

# 3) 分别构建三个代价，手工查看代价矩阵
cost_fns = [TASK_UTILS.build(dict(type=t, weight=w)) for t, w in
            [('QueryClassificationCost', 0.5), ('MaskBCECost', 1.0),
             ('MaskDiceCost', 1.0)]]
costs = [c(pred, gt) for c in cost_fns]
total = torch.stack(costs).sum(0)
print('总代价矩阵:\n', total)

# 4) 运行 SparseMatcher
matcher = TASK_UTILS.build(dict(
    type='SparseMatcher',
    costs=[dict(type='QueryClassificationCost', weight=0.5),
           dict(type='MaskBCECost', weight=1.0),
           dict(type='MaskDiceCost', weight=1.0)],
    topk=1))
q_ids, g_ids = matcher(pred, gt)
print('匹配结果 (query_id, gt_id):', list(zip(q_ids.tolist(), g_ids.tolist())))

# 5) 断言一：被禁止的组合绝不出现
for q, g in zip(q_ids.tolist(), g_ids.tolist()):
    assert query_masks[g, q], f'禁止组合 ({q},{g}) 出现了！'
# 6) 断言二：每个 GT 列至多 topk 条
for g in range(n_g):
    assert (g_ids == g).sum() <= 1
print('全部断言通过')
```

3. **需要观察的现象**：
   - 手工 `total` 矩阵中被禁止的 9 个格子（5×3 的另一半）与允许格子数值同量级——inf 掩码是在 matcher **内部**才施加的；
   - `(2, 1)` 必然出现在结果里：gt1 只允许查询 2，m=1 < topk+1=2，阈值退化成 inf，唯一的有限代价入选；
   - gt0 在 {0,1} 中、gt2 在 {3,4} 中各选总代价较小的一条；
   - 两条断言通过。
4. **预期结果**：匹配结果共 3 对，每对都落在 `query_masks` 允许的格子里；`total` 矩阵中允许格子的最小值所在位置与匹配结果一致（可逐格对照）。哪条查询胜出依赖随机种子，待本地验证；但上述结构性断言与随机数无关，必然成立。
5. 若环境中 `import uniseg3d` 报 `mmdet3d` 缺失，说明尚未完成 u1-l2 的依赖安装，先回头装环境。

#### 4.3.5 小练习与答案

**练习 1**：把 `topk` 改成 2，本例各 GT 列的入选数分别是多少？

**答案**：k = topk+1 = 3。gt0/gt2 各允许 2 个组合（m=2 < 3），阈值退化 inf，**全部 2 条入选**；gt1 仍只有 1 条。总匹配对数 5。若某列允许组合 ≥ 3，则至多入选 topk=2 条。

**练习 2**：若某个 GT 的 `query_masks` 整行全 False（它覆盖的超点全被采样掉了），会发生什么？

**答案**：该列代价全是 1e8，第 topk+1 小也是 1e8，严格小于 1e8 的元素为 0 个——该 GT 匹配 0 条查询，不产生掩码损失；分类侧它也不进入任何 `cls_target`，等于本轮被跳过。这正是 `_select_queries` 里 GT 必须与查询同步采样的原因之一。

**练习 3**：`SparseMatcher` 为什么可以用 `@torch.no_grad()`，而代价函数里的 einsum 明明是可导运算？

**答案**：匹配只是「选边」——决定哪条查询监督哪个 GT，本身不是学习目标。真正的监督发生在匹配之后的 `mask[idx_q]` 对 `sp_masks[idx_gt]` 的 BCE/Dice 损失上，梯度走的是那条路径。匹配过程若带梯度，反而会把「选哪条查询」也变成可优化对象，引入无意义的计算与不稳定。

## 5. 综合实践

**任务：把主实践升级为「权重扭转 + 穷举断言」验证器。**

在 4.3.4 脚本的基础上扩展：

1. **穷举断言**：把随机小例子的生成放进 `for _ in range(100)` 循环（每次随机 `n_q ∈ [3, 8]`、`n_g ∈ [1, 4]`、随机 GT 掩码与 query_masks，并按 4.1.3 的多数投票直觉保证 query_masks 列不跨 GT），每次运行 `topk ∈ {1, 2}` 两组匹配，断言：(a) 所有返回组合满足 `query_masks[g, q]` 为 True；(b) 每个 GT 列入选数 ≤ topk；(c) 允许组合数 ≥ topk+1 的列恰好入选 topk 条（无并列时）。
2. **权重扭转**：固定某个随机例子，先用 4.2 的默认权重匹配一次；再把 `QueryClassificationCost` 的 weight 乘 100 重新构建 matcher 匹配一次。对比两次结果，找出发生翻转的 GT 列。
3. **解释**：对照两次的总代价矩阵，说明分类项占绝对主导后，匹配趋向「分类概率最大的允许查询」——掩码贴合度被忽略。这正是 matcher 与损失共用权重词汇表（`loss_weight` 与 costs 的 `weight` 各自独立调参）时需要警惕的耦合。

预期：断言 (a)(b) 在所有随机例子上成立（它们由 4.3.2 的数学推导保证）；断言 (c) 在无并列代价时成立；权重扭转至少扭转一列选择。具体翻转列与数值待本地验证。

## 6. 本讲小结

- UniSeg3D 的查询采样自超点特征，`query_masks`（`_select_queries` 中 `sp_masks[:, ids]` 的列子集视图）携带「查询落在哪个物体里」的归属先验，使匹配从 DETR 的全局匈牙利匹配退化为**每个 GT 独立的稀疏 top-k 选择**——`linear_sum_assignment` 的 import 是残留痕迹，全仓库未调用。
- 三种代价：`QueryClassificationCost`（负的 softmax 类别概率）、`MaskBCECost`（逐点 BCE 均值）、`MaskDiceCost`（整体重叠度），全部经 einsum 一次算出 `(n_queries, n_gts)` 矩阵，配置权重依次为 0.5/1.0/1.0；`ov_matcher` 只有掩码代价。
- `SparseMatcher` 两技巧：`torch.where(query_masks.T, cost, 1e8)` 的 **inf 掩码**抹掉禁止组合；`topk+1` 第 k 小值做**严格小于**阈值，天然满足「每列 ≤ topk、允许数不足时全取、无允许时不取」，且无需分支处理稀疏情形。
- 返回格式是一维对齐的 `(query_ids, gt_ids)`；`iter_matcher=True` 时七份预测逐层重新匹配。
- 超点标注「每列至多一个 True」使 base 实例匹配天然近似一对一；novel 与 base 的潜在交叉由先 base 后 ov 的两阶段流程规避（`ov_matcher` 在剔除 base 命中查询后的子坐标系上运行，`query_masks` 同步列重索引）。

## 7. 下一步学习建议

下一讲（u5-l3）将进入 `OVInstanceCriterion` 的完整舞台：本讲的 matcher 只是它内部的第一步，接下来你会看到 base/novel 双路如何组织——base 路先匹配并计算 cls/BCE/Dice 损失，novel 路在剩余查询上用 `ov_matcher` 二次匹配且只有掩码损失，以及 `cls_target = -1` 如何让 novel 命中的查询免于分类监督。建议带着本讲的两个问题去读：(1) `mask_maps[j]` 如何把 ov 匹配结果映射回原始查询编号（4.3.3 已埋伏笔）；(2) `assert torch.isin(idx_q, ori_ov_query_indice).sum() == 0` 这条断言为什么必然成立。
