# 跨任务知识共享：对比学习、排序与伪分布蒸馏

## 1. 本讲目标

前几讲我们把六路损失逐个拆开：实例（u5-l3）、语义（u5-l1）、IoU（u5-l4）、点提示与文本提示（u5-l5）。这些损失各自监督各自的输出，本质上是「各学各的」。本讲讲的是 UniSeg3D 论文最核心的设计之一——**跨任务知识共享**：让同一个前向里并存的几组查询互相教、互相对齐，把「交互式分割学到的高质量掩码」和「指代分割学到的语义」在分支之间搬运。

学完本讲，你应该能够：

1. 说清「同一物体的文本查询-点查询正样本对」是如何用 `text_object_id` / `point_object_id` 这对钥匙构造出来的。
2. 掌握三条知识迁移路径——对称 InfoNCE 对比、MarginRanking 排序、两条 BCE 伪标签蒸馏——各自的输入、损失形式与**梯度流向**（谁教谁、detach 在哪一侧）。
3. 理解可学习 `logit_scale` 温度参数的作用，以及它与解码器里固定 `logit_scale` 的区别。
4. 读懂 `ContrastiveCriterion` 与 `IntrinsicPointAndQuery` 两段源码，并动手验证梯度方向。

## 2. 前置知识

本讲是全书最「论文味」的一讲，先补齐几个深度学习通用概念，再回顾项目内部钥匙。

### 2.1 对比学习与 InfoNCE 损失

对比学习（contrastive learning）的目标是：**同一个物体的两个不同「视角」的嵌入应该相近，不同物体的嵌入应该远离**。InfoNCE（Info Noise-Contrastive Estimation）把这件事写成一个「n 选 1 分类」问题：一个批次里有 \( n \) 个文本嵌入和 \( n \) 个点提示嵌入，对每个点提示嵌入 \( p_k \)，在 \( n \) 个文本嵌入里只有一个是它的正样本 \( t_k \)，损失为：

\[
\mathcal{L}_{p \to t} = -\frac{1}{n}\sum_{k=1}^{n} \log \frac{\exp(s(p_k, t_k)/\tau)}{\sum_{j=1}^{n}\exp(s(p_k, t_j)/\tau)}
\]

其中 \( s(\cdot,\cdot) \) 是余弦相似度，\( \tau \) 是温度。**对称（双向）InfoNCE** 就是把方向反过来再算一次并取平均：既要求「给定点提示能找回正确文本」，也要求「给定文本能找回正确点提示」。

### 2.2 温度与 logit_scale

实现时通常不直接写 \( 1/\tau \)，而是存 \( \log(1/\tau) \) 再取指数：`logits = logit_scale * cos_sim`。本讲的 `ContrastiveCriterion` 里它是一个**可学习参数**（`nn.Parameter`），初始值 `np.log(1 / 0.07)`，即 \( \tau = 0.07 \)、缩放系数约 14.3。温度控制 softmax 的尖锐程度：\( \tau \) 越小，嵌入之间微小的相似度差异被放大得越厉害，模型对「难负样本」的惩罚越重——让网络自己学温度，等于让它自己决定「要多苛刻地区分正负样本」。对比一下：u4-l4 见过的解码器 `logit_scale = 4.6052`（约等于 100）是**固定**的非持久 buffer，二者一个可学一个冻结，用途也不同（分类 vs 对比）。

### 2.3 MarginRankingLoss 排序损失

`nn.MarginRankingLoss(margin=0)` 接收两个分数 \( x_1, x_2 \) 和目标 \( y = 1 \)，损失为：

\[
\mathcal{L} = \max(0,\; x_2 - x_1)
\]

即「要求 \( x_1 > x_2 \)，否则罚」。InfoNCE 约束的是 softmax 归一化后的相对概率，排序损失约束的是**逐对的绝对序关系**——正样本对的相似度必须逐一压过每个负样本对，二者互补。

### 2.4 知识蒸馏、伪标签与 detach

知识蒸馏（distillation）指用一路输出（老师）的预测作为软标签去监督另一路输出（学生）。实现要点是**老师在监督信号这一侧要 `detach()`**（切断梯度），否则学生会「反过来改老师的答案」。`tensor.detach()` 返回一个不参与计算图的新张量，反向传播时梯度止步于此——所以「detach 挂在哪一侧」直接回答了「谁教谁」。

### 2.5 项目内部的钥匙（承接 u3-l4 / u3-l5 / u5-l5）

- `text_object_id`：每条文本描述所属物体的 0 起原始 objectId（ScanRefer 物体编号）。
- `point_object_id`：由 `pts_instance_objextId_shuffle` 改名而来，每条点提示所属物体的同一套 objectId。
- `point_prompt_instance_ids`：每条点提示在 **GT 实例行**里的行号——u5-l5 已确认「提示、标签、掩码共用同一行号空间，模型侧按行号切监督真值」。
- `contras_embeds`：解码器逐层保存的 `out_norm(queries)`（256 维），u5-l4 已见它被 `pred_iou_proj` 消费；本讲它会迎来第二个消费者：对比/蒸馏损失。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `uniseg3d/contrastive_criterion.py` | 本讲主角一：对称 InfoNCE + MarginRanking + 伪分布蒸馏，共 119 行 |
| `uniseg3d/instance_criterion.py` | 本讲主角二：`OVInstanceCriterion.__call__` 中的 `IntrinsicPointAndQuery` 掩码蒸馏块 |
| `uniseg3d/query_decoder.py` | `contras_embeds` 的生产端：迭代预测时逐层保存 LayerNorm 后的查询向量 |
| `uniseg3d/unified_criterion.py` | 损失总调度：组装两把 object_id 钥匙并按序调用五路损失 |
| `uniseg3d/uniseg3d.py` | 模型侧：提示查询注入（挂钥匙）、拆分输出、把六路 pred 递给 criterion |
| `uniseg3d/transforms_3d.py` | 数据侧钥匙的产地：`PointPromptGeneration` 与 `PointPromptTest` |
| `configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py` | 三个开关与四个损失权重的取值 |

调用关系一图流（训练前向的最后两步）：

```text
decoder 前向（每层保存 contras_embeds）
   ├── split_original_and_text_prompt_queries ──→ text_prompt_pred  ─┐
   ├── split_original_and_point_prompt_queries ─→ point_prompt_pred ─┤
   └── x（原始查询输出）                                        ├─→ criterion(x, ...)
                                                                    │
ScanNetUnifiedCriterion                                             │
   ├── inst_criterion(pred, insts, point_prompt_pred, point_prompt_insts)
   │       └── IntrinsicPointAndQuery_loss   （点提示 → 原始查询，单向蒸馏）
   ├── sem_criterion / point_prompt_inst_criterion / text_prompt_inst_criterion（前几讲已覆盖）
   └── contrastive_criterion(pred, insts, point_prompt_pred, point_prompt_gts,
                             text_prompt_pred, text_prompt_gts)
           ├── contrastive_loss                 （文本 ↔ 点提示，双向对称 InfoNCE）
           ├── rank_loss                        （文本 ↔ 点提示，双向逐对排序）
           └── pseudo_ditribute_loss            （点提示 → 文本，单向分布蒸馏）
```

## 4. 核心概念与源码讲解

### 4.1 对称 InfoNCE 与 object_id 正样本对齐

#### 4.1.1 概念说明

一次训练前向里，同一场景的同一批物体同时拥有两套「专属查询」：

- **文本提示查询**：由 `LangModule` 把 ScanRefer 描述编码成 32 维嵌入、加类型嵌入后拼进查询序列，经解码器得到 256 维的 `contras_embeds`——它「知道这个物体在语言上是什么」。
- **点提示查询**：输入就是该物体内部某个超点的特征（用户点了它），同样经解码器得到 256 维 `contras_embeds`——它「知道这个物体在几何上在哪」。

两条分支各自有监督（u5-l5 的恒等匹配损失），但没有机制保证它们的嵌入空间是「对齐」的：物体 5 的文本嵌入和物体 5 的点提示嵌入可能相距十万八千里。对称 InfoNCE 就是把这两个空间拉到一起：**同 objectId 的嵌入互为正样本，不同 objectId 的互为负样本**。这样指代分支的语义知识（经 CLIP 预训练获得）与交互式分支的几何定位知识就在同一个 256 维空间里融合了——这就是「跨任务知识共享」字面意义上的「共享」：共享一个嵌入空间。

#### 4.1.2 核心流程

对批次里的每个场景（只使用**最后一层**解码输出的 `contras_embeds`）：

1. 取出 `emb_point`（n 条点提示嵌入）、`emb_text`（m 条文本嵌入）；文本为空则跳过该场景。
2. **构造正样本对**：按 `text_object_id` 的顺序，逐个到 `point_object_id` 里找相等的行，把这些点提示嵌入按文本顺序收集为 `refer_embed`。若每个物体恰好一条点提示，`refer_embed` 第 k 行与 `emb_text` 第 k 行同属一个物体——「行号即类别」。
3. 两侧 L2 归一化，点乘得两个方向的相似度矩阵，乘可学习 `logit_scale`。
4. 标签就是 `arange(n)`（对角线），做对称交叉熵：(CE(点→文) + CE(文→点)) / 2。
5. （可选）按 `point_prompt_distance_norms` 逐行加权后取均值。

写成矩阵形式（\( \tau \) 由可学习 logit_scale 控制）：

\[
L_{point} = \mathrm{CE}\!\left(\frac{1}{\tau} R K^{\top},\; \mathrm{arange}(n)\right), \qquad
L = \frac{1}{2}\left(L_{point} + L_{text}\right), \qquad R, K \text{ 均为单位向量}
\]

#### 4.1.3 源码精读

**① 嵌入从哪来**——`UnifiedQueryDecoder.forward_iter_pred` 在每层预测后保存 `out_norm` 后的查询向量（clone 断开与后续层的共享），最终输出只保留最后一层，并把前六层打包进 `aux_outputs`：

- [uniseg3d/query_decoder.py:L554](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/query_decoder.py#L554)：第一份 `contras_embeds`（进层之前）。
- [uniseg3d/query_decoder.py:L567](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/query_decoder.py#L567)：每层 transformer 块之后追加一份。
- [uniseg3d/query_decoder.py:L579-L585](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/query_decoder.py#L579-L585)：返回字典中 `contras_embeds=contras_embeds[-1]` 只含末层——`ContrastiveCriterion` 消费的正是它（u5-l4 的 IoU 分支则连 aux 层一起消费）。

**② 钥匙从哪来**——模型侧注入提示查询时挂上钥匙：

- [uniseg3d/uniseg3d.py:L674](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L674)：`add_text_prompt_queries` 把 `text_object_id`（来自 `batch_inputs_dict`）挂到文本 GT 上。
- [uniseg3d/uniseg3d.py:L730](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L730)：`add_point_prompt_queries` 把 `pts_instance_objextId_shuffle` 存为 `point_object_id`——数据侧 [uniseg3d/transforms_3d.py:L160-L163](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L160-L163) 在生成点提示时同步写出了这把钥匙（u3-l4 讲过它是「乱序的 0 起 objectId」）。
- [uniseg3d/unified_criterion.py:L43-L63](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_criterion.py#L43-L63)：总调度把两把钥匙原样透传进 `point_prompt_gts` / `text_prompt_gts`。
- [uniseg3d/unified_criterion.py:L74](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_criterion.py#L74)：六路输出按位传给 `contrastive_criterion`——注意第一参数 `pred` 是**原始查询**的输出（本损失只用它取 device 与批次长度）。

**③ 正样本对与对称损失**——`ContrastiveCriterion.__call__`：

- [uniseg3d/contrastive_criterion.py:L48-L53](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/contrastive_criterion.py#L48-L53)：`for i in range(len(pred['masks']))` 是**批次循环**（`masks` 是逐场景列表）；取末层点/文嵌入与两侧 GT。
- [uniseg3d/contrastive_criterion.py:L57-L59](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/contrastive_criterion.py#L57-L59)：无文本描述的场景直接跳过。
- [uniseg3d/contrastive_criterion.py:L67-L73](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/contrastive_criterion.py#L67-L73)：**正样本对构造的核心**——`pos_ind = point_prompt_gt.point_object_id == id_text` 按文本顺序挑选同物体点提示；同一处还顺带收集 `point_prompt_distance_norms` 与点提示 `cls_preds`（分别供 4.2、4.3 使用）。
- [uniseg3d/contrastive_criterion.py:L80-L85](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/contrastive_criterion.py#L80-L85)：拼接、L2 归一化，`logit_scale * embed @ embed.T` 得到两个方向的相似度 logits。
- [uniseg3d/contrastive_criterion.py:L86-L95](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/contrastive_criterion.py#L86-L95)：标签来自 [get_ground_truth（L117-L119）](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/contrastive_criterion.py#L117-L119) 的 `arange`——对角线即正样本；两个方向 CE 相加除二，可选距离加权后取均值。
- [uniseg3d/contrastive_criterion.py:L107-L110](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/contrastive_criterion.py#L107-L110)：无有效样本时返回标量 0，否则乘 `contrastive_loss_weight`（配置 0.1）。

**④ 可学习温度**——[uniseg3d/contrastive_criterion.py:L19](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/contrastive_criterion.py#L19)：`nn.Parameter(torch.ones([]) * np.log(1 / 0.07))`。它是 criterion 里唯一的可学习参数，会随优化器一起更新（这要求它挂在模型参数图上——`ScanNetUnifiedCriterion` 是 `nn.Module`，注册进 MODELS 后随模型一起被 DDP 收集；这也解释了配置里 `find_unused_parameters = True` 的必要性之一）。

一个需要留意的隐含假设：代码按「每条文本恰好对应一条点提示」对齐（训练时成立，因为 `PointPromptGeneration` 为**所有** base 实例各生成一条提示，`TextPromptGeneration` 的文本也只描述这些实例）。若某物体的文本没有对应点提示，`refer_embed` 行数会小于 `emb_text`，`arange` 标签将与行错位——二次开发引入新数据源时要小心这个前提。

#### 4.1.4 代码实践

**实践目标**：用假数据直接实例化 `ContrastiveCriterion`，验证「object_id 对齐正确、对角线占优时损失最小；对齐错误时损失巨大」。

**操作步骤**（在 u1-l2 搭建的 conda 环境中，仓库根目录下执行）：

```python
# 示例代码：验证 object_id 对齐决定对比损失大小
import torch
from uniseg3d.contrastive_criterion import ContrastiveCriterion
from uniseg3d.structures import InstanceData_

torch.manual_seed(0)
crit = ContrastiveCriterion(
    use_ranking=False,          # compute_losses 内部硬编码 .cuda()，CPU 演示先关闭
    contrastive_loss_weight=0.1,
    ranking_loss_weight=0.1,
    use_pseudo_cls_supervise=False)

d = 256
T = torch.randn(2, d)                      # 物体 0 / 物体 1 的文本嵌入
P = torch.empty(2, d)                      # 点提示按乱序到达：第 0 条属于物体 1
P[0], P[1] = T[1], T[0]                    # 同物体嵌入相同 → 对齐后正对相似度=1

def build(point_object_id):
    pred = {'masks': [torch.randn(50, 100)]}   # 本损失只用其 device 与批次长度
    ppp  = {'contras_embeds': [P.clone()], 'cls_preds': [torch.randn(2, 20)]}
    tpp  = {'contras_embeds': [T.clone()], 'cls_preds': [torch.randn(2, 20)]}
    pgt = InstanceData_(); pgt.point_object_id = torch.tensor(point_object_id)
    tgt = InstanceData_(); tgt.text_object_id = torch.tensor([0, 1])
    return pred, None, ppp, pgt, tpp, tgt    # 第二个参数 inst_gts 未被使用

print('对齐正确:', crit(*build([1, 0]))['contrastive_loss'])   # 文本0↔提示1、文本1↔提示0
print('对齐错误:', crit(*build([0, 1]))['contrastive_loss'])
```

**需要观察的现象**：两个损失数值的悬殊对比；`logit_scale` 初始值 `crit.logit_scale.exp()` 约 14.3。

**预期结果**：对齐正确时正对余弦相似度为 1、负对接近 0，经 14.3 倍放大后 softmax 近乎确定，损失应接近 0；对齐错误时正对错位、损失约为 `logit_scale × 权重` 的量级（约 1.4）。具体数值**待本地验证**（不同随机种子下负样本相似度略有差异）。

#### 4.1.5 小练习与答案

**练习 1**：为什么标签是 `arange(n)` 而不是显式传一张 (n,n) 的 0/1 矩阵？
**答案**：按文本顺序逐个收集同物体点提示后，`refer_embed` 第 k 行与 `emb_text` 第 k 行天然同属一个物体，「行号=列号=类别」，交叉熵的对角线标签正好等价于逐正样本对的 one-hot 监督，无需再构造矩阵。

**练习 2**：`ContrastiveCriterion` 里两侧嵌入都没有 detach，这意味着什么？
**答案**：梯度同时回流到文本分支（LangModule 适配器、解码器）与点提示分支（超点特征、解码器）——这是**双向**的知识共享：两边共同把同一物体的嵌入拉到一起，而不是单方面模仿。

**练习 3**：把 `logit_scale` 改成固定值 1（不学习），损失行为会怎么变？
**答案**：相似度范围被压缩到 [-1,1]，softmax 分布变得平坦，负样本几乎得不到惩罚（上例中对齐错误时损失上限从约 14.3 降到约 0.7），梯度信号变弱；可学习温度让模型自行决定区分的苛刻程度。

### 4.2 MarginRanking 排序损失与距离加权

#### 4.2.1 概念说明

InfoNCE 的 softmax 归一化有一个特性：只要正样本「相对最大」即可，两个相似度可以都很小。MarginRanking 补上「绝对序」的约束：对相似度矩阵的每一列 j，对角线元素 \( s_{jj} \)（正样本对）必须逐一**严格大于**同列所有非对角元素 \( s_{ij} \)：

\[
\mathcal{L}_{rank} = \sum_{j}\sum_{i \neq j} \max\left(0,\; s_{ij} - s_{jj}\right)
\]

值得注意的是，源码中排序损失用的 logits **没有乘 logit_scale**（原始余弦相似度），而 InfoNCE 用了——所以这是两个尺度上互补的约束：一个在放大后的 softmax 空间里做全局竞争，一个在原始余弦空间里做逐对序关系。

「距离加权」是给每条点提示一个置信权重的设计：直觉是提示点到实例质心的（归一化）距离反映该点的「典型性」，越典型的点提示越可信，其对比损失权重应更大。实现上就是把逐行损失乘上 `point_prompt_distance_norms` 再取均值。

#### 4.2.2 核心流程

`compute_losses(logits, gather_type='train')`：

1. 初始化累加器 `z = torch.zeros(1).cuda()`，作为 `rank_loss` 的初值。
2. 双重循环枚举所有有序对 (i, j)，i ≠ j。
3. 每对调用 `MarginRankingLoss(logits[j][j], logits[i][j], y=1)`：要求列 j 中的正对分数压过负对。
4. 累加后随主输出一起乘 `ranking_loss_weight`。

距离加权分支（`__call__` 内）：

1. 若 `point_prompt_gt` 上挂有 `point_prompt_distance_norms` 键，则收集正样本对时同步收集每条提示的权重。
2. 对称 CE 得到的逐行损失向量按权重逐元素相乘，再取均值。

#### 4.2.3 源码精读

- [uniseg3d/contrastive_criterion.py:L20-L21](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/contrastive_criterion.py#L20-L21)：`use_ranking=True` 时构造 `nn.MarginRankingLoss()`（默认 margin=0）。
- [uniseg3d/contrastive_criterion.py:L27-L38](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/contrastive_criterion.py#L27-L38)：排序损失主体。注意 L30 与 L33 的 `torch.zeros(1).cuda()` / `torch.ones(1).cuda()`——**硬编码 CUDA**，纯 CPU 环境跑原版会报错（这也是 4.1.4 实践先关闭 ranking 的原因）。
- [uniseg3d/contrastive_criterion.py:L97-L100](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/contrastive_criterion.py#L97-L100)：调用点——注意 L98 重新计算 `refer_embed@key_embed.T`，**不带** `logit_scale`。
- [uniseg3d/contrastive_criterion.py:L91-L92](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/contrastive_criterion.py#L91-L92)：距离加权——逐行 `loss = loss * point_prompt_distance_norms`。
- [uniseg3d/contrastive_criterion.py:L111-L112](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/contrastive_criterion.py#L111-L112)：`rank_loss` 汇总输出，权重 0.1。

**诚实现状核查**：距离加权在当前仓库中是「休眠」的。

- 全仓库唯一写这个键的地方是 [uniseg3d/transforms_3d.py:L198-L200](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L198-L200) 与 [L232-L233](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L232-L233)：`PointPromptTest` 在 `is_distance=True` 时写入 `point_prompt_distance_norms`，但它初始化为 `np.array([])` 后**从未被填充**，恒为空数组。
- 训练侧的 `PointPromptGeneration`（[uniseg3d/transforms_3d.py:L112-L165](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L112-L165)）完全不写这个键。
- 传输链路（[uniseg3d/formatting.py:L18](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/formatting.py#L18)、[uniseg3d/data_preprocessor.py:L100-L101](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/data_preprocessor.py#L100-L101)、[uniseg3d/uniseg3d.py:L731-L733](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L731-L733)、[uniseg3d/unified_criterion.py:L48-L49](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_criterion.py#L48-L49)）是完整的——所以主配置的训练日志里 `use_point_prompt_distance_norm` 恒为 False，加权分支不生效。这留给二次开发者一个明确的扩展位（见 u6-l4）。

#### 4.2.4 代码实践

**实践目标**：在 CPU 上复现排序损失的数学行为，确认「对角占优时为 0、无结构时为正」。

**操作步骤**：

```python
# 示例代码：CPU 复现 compute_losses 的排序约束（源码需 CUDA，这里等价重写）
import torch
import torch.nn.functional as F

def rank_loss_cpu(logits):          # logits: [n, n] 余弦相似度，对角线为正样本对
    loss = torch.zeros(1)
    for i in range(logits.size(0)):
        for j in range(logits.size(0)):
            if i != j:
                loss = loss + F.margin_ranking_loss(
                    logits[j][j].unsqueeze(0),   # 正对 s_jj
                    logits[i][j].unsqueeze(0),   # 同列负对 s_ij
                    torch.ones(1), margin=0.0)
    return loss

good = 0.9 * torch.eye(3) + 0.05 * torch.rand(3, 3)   # 对角 ≈0.9+，非对角 ≈0.05
good = torch.triu(good) + torch.triu(good, 1).T       # 对称化，保持对角优势
bad = torch.rand(3, 3)                                # 无结构
print('对角占优:', rank_loss_cpu(good).item())
print('随机矩阵:', rank_loss_cpu(bad).item())
```

**需要观察的现象**：两组数值；再执行 `grep -rn "point_prompt_distance_norms" uniseg3d/ configs/` 核对 4.2.3 的「休眠」结论。

**预期结果**：对角占优矩阵的排序损失应为 0（每个正对都严格大于同列负对）；随机矩阵大概率大于 0（部分列的对角线不是列最大）。具体数值**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：n 条提示时双重循环会产生多少项损失？n=3（配置 `TextPromptGeneration` 的 `num_ins=3`）时是多少？
**答案**：所有有序对 i≠j 共 \( n(n-1) \) 项；n=3 时 6 项。注意每项遍历的是「同一列 j 内正对 vs 每个负对」。

**练习 2**：如果想让距离加权真正生效，最小改动是什么？
**答案**：在 `PointPromptGeneration.transform` 里为每条选中的提示计算「到实例质心的归一化距离」并写进 `input_dict['point_prompt_distance_norms']`（一个与提示逐条对齐的数组）——Pack/Preprocessor/criterion 链路无需改动，这正是三站通道键名约定的好处（u3-l2）。

**练习 3**：排序损失与 InfoNCE 一个用原始相似度、一个乘温度放大，为什么这样搭配是合理的？
**答案**：InfoNCE 需要温度把 [-1,1] 的余弦差异放大到 softmax 可分辨的尺度；排序损失只关心序关系，序关系在单调放大下不变，用原始值还能让损失取值稳定在 [0,2] 量级，便于配权重。

### 4.3 IntrinsicPointAndQuery 与伪分布蒸馏

#### 4.3.1 概念说明

前两条路径连接「文本提示 ↔ 点提示」。第三条路径回答另一个问题：**交互式分支学到的东西，能不能教给「自由生长」的常规超点查询？**

回顾两路查询的处境差异：

- **点提示查询**：输入就是 GT 物体内部一个超点的特征（[uniseg3d/uniseg3d.py:L706-L708](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L706-L708) 取超点特征加类型嵌入），起点即「知道答案在哪」，其掩码预测聚焦且置信度高。
- **常规超点查询**：`_select_queries` 随机采样存活，靠匹配器事后配对学习，自由度大、监督稀疏。

`IntrinsicPointAndQuery` 损失把点提示的**掩码预测**当作伪标签，在它最自信的一小撮超点上软监督（BCE on sigmoid 概率，不是硬 0/1）与之配对的常规查询——掩码质量从交互式分支迁移到实例/全景分割分支。梯度方向单向：点提示侧 detach 当老师，常规查询当学生。

`pseudo_ditribute_loss`（源码键名即此拼写，注意 `ditribute` 少了个 s）是同族第二条蒸馏：把点提示查询的**分类分布**（19+1 类的 sigmoid 概率向量）作为伪标签监督**文本提示查询**的分类输出。直觉是：用户点击提供的物体定位证据比语言描述更「接地气」，用它来校准文本分支的类别判断；反过来文本分支的分类又经对比损失与点分支对齐——三条路径构成一个以点提示为中心的知识枢纽。

#### 4.3.2 核心流程

**IntrinsicPointAndQuery**（位于 `OVInstanceCriterion.__call__`，base 匹配之后、novel 匹配之前；只使用主输出）：

1. 前置条件检查：存在点提示；base 匹配得到至少一对；`point_prompt_instance_ids` 键存在。
2. **两套乱序对齐**：匹配结果 `(query_ids, instance_ids)` 与 `point_prompt_instance_ids` 都指向 base GT 行号空间（u5-l5 已证），但各自乱序。对 `instance_ids` 与 `point_prompt_ids` 分别 `argsort`，把「查询」与「提示」都重排成 **GT 行号升序**，随后用排序索引取出两侧掩码——第 k 行自动对应同一个 GT。若两侧行数不等（匹配不完整），整个样本跳过。
3. **选最自信的 10% 超点**：对每个超点列，取所有点提示中的最大 logit（`max` 同时返回夺冠的提示行号）；按该最大值 `topk` 选出 `ratio × n_超点` 列（`ratio=0.1`）。
4. **软蒸馏**：在同一批 (夺冠提示行, 超点列) 坐标上，取常规查询的 logit，两侧 sigmoid 后做 `BCELoss(query, point.detach())`——只对学生求梯度。
5. 批内均值乘 `IntrinsicPointAndQuery_loss_weight=0.1`，以 `IntrinsicPointAndQuery_loss` 键输出（仅主层一次，不进 aux 循环）。

**伪分布蒸馏**（位于 `ContrastiveCriterion.__call__` 内，与 4.1 同一个批次循环）：

1. 收集正样本对时同步收集点提示的 `cls_preds` 行（4.1 源码 ③ 的同一处）。
2. 文本侧 `cls_preds` 与点侧 `cls_preds` 各自 sigmoid 成概率。
3. `BCELoss(text_prob, point_prob.detach())`，键名 `pseudo_ditribute_loss`，权重 0.1。

#### 4.3.3 源码精读

- [uniseg3d/instance_criterion.py:L87-L105](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L87-L105)：`__init__` 中的三个关键参数——[L99](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L99) `IntrinsicPointAndQuery_loss_weight`、[L102](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L102) `ratio`、[L104](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L104) 蒸馏用的 `nn.BCELoss`。
- [uniseg3d/instance_criterion.py:L361](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L361)：`__call__` 签名——第四、五参数正是模型侧递来的 `point_prompt_pred` 与 `point_prompt_insts`（由 [uniseg3d/unified_criterion.py:L65](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_criterion.py#L65) 转发）。
- [uniseg3d/instance_criterion.py:L384-L395](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L384-L395)：base GT 的 `SparseMatcher` 匹配——`indices` 是本损失的对齐输入之一。
- [uniseg3d/instance_criterion.py:L397-L411](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L397-L411)：**argsort 双重对齐**。L407 `point_prompt_ids` 即提示的 GT 行号；L408-L409 两个 `argsort`；L410-L411 行数不等则跳过该样本（隐含要求：每个 base GT 都被匹配上、每个 base GT 都有一条提示——训练时 `PointPromptGeneration` 覆盖全部实例，`topk=1` 的匹配通常一一对应，条件一般成立）。
- [uniseg3d/instance_criterion.py:L414-L415](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L414-L415)：按对齐后的顺序取两侧掩码——`pred['masks'][i]` 是拆分后的**原始查询**主输出，`point_prompt_pred['masks'][i]` 是**点提示**主输出。
- [uniseg3d/instance_criterion.py:L417-L423](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L417-L423)：列最大值 + `topk` 选列 + `X = max_indices[Y]` 记录每列由哪条提示夺冠——监督只落在「点提示分支最有把握」的坐标上。
- [uniseg3d/instance_criterion.py:L425-L429](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L425-L429)：sigmoid 后 `BCELoss(query, point.detach())`——**detach 在点提示侧**，梯度单向流入常规查询。
- [uniseg3d/instance_criterion.py:L628-L630](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L628-L630)：损失并入返回字典（`pred_iou=False` 时走 [L641-L642](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L641-L642) 的镜像分支）。
- [uniseg3d/contrastive_criterion.py:L102-L105](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/contrastive_criterion.py#L102-L105)：伪分布蒸馏——`text_cls_logits`（学生）向 `point_cls_logits.detach()`（老师）做逐元素 BCE。
- [uniseg3d/contrastive_criterion.py:L113-L114](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/contrastive_criterion.py#L113-L114)：以 `pseudo_ditribute_loss` 键输出，权重 `cls_logit_loss_weight=0.1`。
- 配置开关：[configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:L13-L18](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L13-L18)（`use_pseudo_cls_supervise=True`、两个 λ 均 1.0）、[L90](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L90)（`ratio=0.1`）、[L98](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L98)（蒸馏权重 0.1）。

梯度流向总表（本讲全部四个 loss key）：

| loss key | 路径 | 老师侧 detach？ | 梯度流向 | 使用哪层输出 | 权重 |
| --- | --- | --- | --- | --- | --- |
| `contrastive_loss` | 文本 ↔ 点提示 | 否 | **双向** | 仅末层 `contras_embeds` | 0.1 |
| `rank_loss` | 文本 ↔ 点提示 | 否 | **双向** | 仅末层（原始余弦） | 0.1 |
| `pseudo_ditribute_loss` | 点提示 → 文本 | 是（点侧） | 单向 | 仅末层 `cls_preds` | 0.1 |
| `IntrinsicPointAndQuery_loss` | 点提示 → 原始查询 | 是（点侧） | 单向 | 仅主层 `masks` | 0.1 |

#### 4.3.4 代码实践

**实践目标**：把 L397-L429 的对齐与蒸馏逻辑抽成独立脚本，用假数据反向传播，亲眼确认「stop_gradient 在老师一侧」。

**操作步骤**：

```python
# 示例代码：复现 IntrinsicPointAndQuery 的对齐与蒸馏，验证梯度方向
import torch
import torch.nn.functional as F

torch.manual_seed(0)
K, n_sp, ratio = 3, 40, 0.1          # 3 个 base GT、40 个超点、10% 采样

query_ids        = torch.tensor([7, 3, 9])    # 匹配结果（任意顺序）
instance_ids     = torch.tensor([2, 0, 1])    # q7→gt2, q3→gt0, q9→gt1
point_prompt_ids = torch.tensor([1, 0, 2])    # 提示按乱序到达（打乱采样）

q_masks = torch.randn(10, n_sp).requires_grad_(True)   # 常规查询掩码 logits（学生）
p_masks = torch.randn(K,  n_sp).requires_grad_(True)   # 点提示掩码 logits（老师）

aligned_q = query_ids[torch.argsort(instance_ids)]     # GT 升序 → 查询号
aligned_p = torch.argsort(point_prompt_ids)            # GT 升序 → 提示行号
Q, P = q_masks[aligned_q], p_masks[aligned_p]          # 第 k 行同属 GT k

max_values, max_indices = torch.max(P, dim=0)          # 每列最自信的提示
n_sel = int(ratio * n_sp)                              # 4
_, Y = torch.topk(max_values, k=n_sel)                 # 最自信的列
X = max_indices[Y]                                     # 这些列的夺冠提示
loss = F.binary_cross_entropy(Q[X, Y].sigmoid(), P[X, Y].sigmoid().detach())
loss.backward()

print('学生 q_masks.grad 非零行:', (q_masks.grad.abs().sum(1) > 0).nonzero().flatten().tolist())
print('老师 p_masks.grad:', p_masks.grad)
```

**需要观察的现象**：`p_masks.grad` 是否为 `None`；`q_masks` 中哪些行收到了梯度。

**预期结果**：老师 `p_masks.grad` 为 `None`（detach 切断）；学生只有被选中的行（`aligned_q` 中那 K 行里实际用到的 `aligned_q[X]`）梯度非零。具体行号**待本地验证**（取决于随机 topk 结果）。

#### 4.3.5 小练习与答案

**练习 1**：为什么蒸馏只监督「点提示最自信的 10% 超点」，而不是全部超点？
**答案**：其一是省算力且聚焦高质量信号；其二是伪标签有噪声——点提示只见过一个超点的局部证据，在物体边界等低置信区域它的预测未必可靠，取列最大值 topk 相当于只用「老师们都抢着认领」的坐标做监督，降低错误伪标签的干扰。

**练习 2**：若把 detach 移到学生一侧（`Q[X,Y].sigmoid().detach()` 作目标、`P` 作预测），语义会变成什么？
**答案**：方向反转——变成用常规查询的掩码去教点提示分支。但常规查询的掩码靠匹配器事后配对学得，质量通常不如「起点在 GT 内部」的点提示，这样蒸馏会把噪声当老师，与论文「交互式分支是知识枢纽」的动机相悖。

**练习 3**：`pseudo_ditribute_loss` 监督的是 20 维（19 类+背景）sigmoid 概率向量的逐元素 BCE，而不是 argmax 后的硬标签交叉熵，差别在哪？
**答案**：软分布保留了「点提示分支认为 60% 是椅子、30% 是沙发」这类不确定性，文本分支学到的也是分布而不是单点判断；硬标签会丢掉这些暗知识（dark knowledge），这正是「分布蒸馏」名称的由来。它也解释了为什么用 `nn.BCELoss`（对 sigmoid 概率逐元素作用）而不是 `F.cross_entropy`。

## 5. 综合实践

**任务：搭建一个「跨任务知识共享实验台」，把本讲四个 loss key 一网打尽。**

在仓库根目录写一个脚本（示例代码），构造 batch=1、两个物体（objectId 0/1）的假前向输出，分三段运行：

1. **对齐段**：复用 4.1.4 的 `build`，但 `use_pseudo_cls_supervise=True`——此时除 `contrastive_loss` 外还应输出 `pseudo_ditribute_loss`；把点提示的 `cls_preds` 设成明显偏向某类的概率（如第 0 行 sigmoid 后椅子类为 0.9），观察文本侧被拉向同一类需要多轮优化（可用 `torch.optim.SGD([{'params': crit.parameters()}, ...], lr=0.1)` 迭代 50 步，只更新文本嵌入）。
2. **排序段**：用 4.2.4 的 `rank_loss_cpu` 分别喂「对齐正确」与「对齐错误」的相似度矩阵。
3. **蒸馏段**：跑 4.3.4 的梯度方向检查。

然后填写下面这张汇总表（答案应与 4.3.3 的表格一致）：

| loss key | 输入张量 | detach 侧 | 梯度流向 |
| --- | --- | --- | --- |
| contrastive_loss | 末层 contras_embeds ×2 | 无 | ？ |
| rank_loss | 末层 contras_embeds ×2（无温度） | 无 | ？ |
| pseudo_ditribute_loss | 末层 cls_preds ×2 | ？ | ？ |
| IntrinsicPointAndQuery_loss | 主层 masks ×2 | ？ | ？ |

最后做一次「键名考古」：`grep -rn "pseudo_ditribute_loss\|IntrinsicPointAndQuery" uniseg3d/`，并在官方训练日志（若已按 u1-l2 跑过训练，`work_dirs/` 下）里找到这四个 key——注意日志里出现的是 `pseudo_ditribute_loss` 这个带拼写错误的键名，读日志时别当成 bug。数值部分**待本地验证**。

## 6. 本讲小结

- **正样本对靠两把钥匙**：`text_object_id` 与 `point_object_id`（即数据侧的 `pts_instance_objextId_shuffle`）共用同一套 0 起 objectId，`ContrastiveCriterion` 用布尔匹配把同物体的文本/点提示嵌入按行对齐，`arange` 标签即「行号=类别」。
- **三条路径四个键**：`contrastive_loss`（对称 InfoNCE，双向）、`rank_loss`（MarginRanking 逐对排序，双向、无温度）、`pseudo_ditribute_loss`（点→文的分类分布 BCE 蒸馏，单向）、`IntrinsicPointAndQuery_loss`（点→常规查询的掩码 BCE 蒸馏，单向）；四者权重均为 0.1，且都只用最后一层/主输出。
- **谁教谁看 detach**：两条蒸馏路径的 detach 都挂在点提示（老师）一侧——交互式分支因为「输入即答案的位置」而成为知识枢纽；对比与排序无 detach，是双向对齐。
- **可学习温度**：`ContrastiveCriterion.logit_scale` 是唯一的可学习标量（初始 ln(1/0.07)），控制对比 softmax 的苛刻程度；解码器的 `logit_scale=4.6052` 则是固定 buffer，二者角色不同。
- **两个诚实的坑**：`compute_losses` 硬编码 `.cuda()`（CPU 无法原样运行排序分支）；`point_prompt_distance_norms` 的传输链路完整但生产端从不写非空值，距离加权在当前配置下休眠。
- **IntrinsicPointAndQuery 的对齐技巧**：匹配结果与提示 id 都指向 base GT 行号空间但各自乱序，用两个 `argsort` 统一重排成 GT 升序后按行对齐，行数不等则整样本跳过。

## 7. 下一步学习建议

本讲结束后，训练侧（u4 骨干与解码器 + u5 损失体系）已全部讲完，下一讲进入 **u6-l1 推理链路：一次前向输出六任务结果**——你会看到本讲的 `contras_embeds` 在推理时的另一个下游（`preds_iou` 乘进分数、开放词汇掩码过滤），以及 `topk_insts`/`sp_score_thr`/`npoint_thr` 等阈值如何在 `predict_by_feat` 系列中落地。

继续阅读源码的建议顺序：

1. [uniseg3d/contrastive_criterion.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/contrastive_criterion.py) 全文精读一遍（仅 119 行），对照本讲 4.1/4.2/4.3 的行号。
2. 回看 [uniseg3d/query_decoder.py:L544-L585](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/query_decoder.py#L544-L585)，梳理 `contras_embeds` 的全部三个消费者（pred_iou_proj、ContrastiveCriterion、以及推理时的 iou 过滤）。
3. 若对「新增第四种提示类型」感兴趣，提前浏览 u6-l4 的扩展点清单——本讲的 `point_prompt_distance_norms` 休眠分支正是最现成的练手素材。
