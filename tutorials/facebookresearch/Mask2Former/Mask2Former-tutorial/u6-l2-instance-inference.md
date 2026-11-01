# 实例分割推理分支

## 1. 本讲目标

上一讲（u6-l1）我们走完了 `MaskFormer.forward` 推理路径的公共部分：mask 上采样、尺寸还原、三开关分发，并精读了语义分支 `semantic_inference`。本讲进入第二个分支——**实例分割推理 `instance_inference`**。

学完本讲，你应该能够：

1. 说清 mask classification 范式下「实例」是怎么被制造出来的：Q 个 query × K 个类别展开成 Q×K 个候选对，再按分数 topk 选出 N 个 (query, class) 对。
2. 解释**重打分公式**：最终分数 = 分类分数 × mask 平均概率（`result.scores = scores_per_image * mask_scores_per_image`），以及为什么要乘上第二项。
3. 理解全景模式开启时的 **thing 类过滤**（stuff 类不参与实例输出）与**全零占位 `pred_boxes`** 的来历。
4. 会动手构造玩具输入，逐步复现 topk 索引还原过程，并对比 `TEST.DETECTIONS_PER_IMAGE` 与 `panoptic_on` 取值对输出实例数的影响。

## 2. 前置知识

- **mask classification 范式回顾**：模型输出不是「每像素一个类别」，而是 Q 个 query，每个 query 给出（a）一个 (K+1) 维分类 logits（最后一维是 no-object「无对象」通道）和（b）一张 mask logits 图。上一讲的语义分支把这些概率图**加权融合**成 K 张每类概率图；本讲的实例分支则**保持每个 query 的 mask 独立**，把「query + 它属于哪个类」当成一个实例。
- **`torch.topk`**：在一维张量中取最大的 k 个元素及其**扁平索引**（在一维中的位置）。本讲的关键技巧就是「二维打平成一维 → topk → 用整除与取余把扁平索引还原成 (行, 列)」。
- **softmax 与 sigmoid 的区别**：softmax 在 K+1 个通道上做「互斥归一化」，所有通道概率和为 1；sigmoid 对每个 mask 像素独立做 0~1 压缩，像素之间不竞争。实例分支两者都用：分类用 softmax，mask 用 sigmoid。
- **thing / stuff 类别**：在全景分割的标签空间里（如 COCO panoptic 133 类、ADE20K 150 类），可数对象（人、车）叫 **thing**，不可数背景（天空、草地）叫 **stuff**。实例分割只输出 thing；stuff 归语义/全景分支管。
- **detectron2 的 `Instances` / `Boxes`**：`Instances(image_size)` 是一个「字段包」对象，可以像普通属性一样挂载 `pred_masks`、`scores`、`pred_classes`、`pred_boxes` 等字段；`Boxes(tensor)` 是 (N, 4) 框（XYXY 格式）的包装类。这套字段名是 detectron2 生态的**约定接口**，评估器和可视化器都按字段名取数。
- **`thing_dataset_id_to_contiguous_id`**：元数据里的映射表「数据集原始类别 id → 连续训练 id」，**只包含 thing 类**。它的 `.values()`（一组连续 id）就是本讲 thing 过滤的判断依据。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `mask2former/maskformer_model.py` | **本讲核心**。`instance_inference`（L344-L380）的全部逻辑；eval 分支的调用点（L257-L260）；`from_config` 中 `test_topk_per_image` 的来源（L160）与 `sem_seg_postprocess_before_inference` 的强制开启（L149-L153） |
| `mask2former/config.py` | `MODEL.MASK_FORMER.TEST.INSTANCE_ON / PANOPTIC_ON` 等推理开关的默认值注册 |
| `configs/coco/instance-segmentation/maskformer2_R50_bs16_50ep.yaml` | 纯实例分割配置：`INSTANCE_ON: True`、`PANOPTIC_ON: False`，综合实践会用到 |
| `mask2former/data/datasets/register_ade20k_panoptic.py` | `thing_dataset_id_to_contiguous_id` 的构造现场，理解 thing 过滤查的是一张什么表 |
| `demo/demo.py` / `demo/predictor.py` | instances 输出的两个下游消费者：日志计数与可视化 |
| `mask2former/evaluation/instance_evaluation.py` | instances 输出的第三个消费者：转 COCO json 算 AP（u7-l2 会精读，本讲只提一句） |

## 4. 核心概念与源码讲解

### 4.1 query×class 展开与 topk：从 Q 个 query 到 N 个实例

#### 4.1.1 概念说明

实例分割要求输出「N 个带类别、带分数、带 mask 的对象」，而 mask classification 范式的原始输出是「Q 个 query，每个 query 一张 mask + 一条 (K+1) 维类别分布」。两者的形状对不上：**query 数 ≠ 实例数，且一个 query 只有一张 mask 却有 K 个候选类别**。

MaskFormer（继承自 DETR 的后处理思路）的解法非常直接：

> 把「第 q 个 query 是第 k 类」的置信分数 \(s_{q,k}\) 排成一张 Q×K 的表，**打平后取全局 topk**。每一个入选的 (q, k) 对就是一个实例：mask 取第 q 个 query 的 mask，类别取 k，分数取 \(s_{q,k}\)。

三个值得注意的设计后果：

1. **实例数由 topk 决定**，而不是由某个阈值决定。`instance_inference` 不做分数截断，返回的就是 topk 个实例（分数可能很低），「要不要过滤」留给下游（评估器、可视化器）。
2. **同一个 query 可能入选多次**：表中 (q, k₁) 与 (q, k₂) 是两个不同的扁平索引，若都进了 topk，就会产生两个类别不同、mask 相同（或说共享同一张 query mask）的实例。训练良好的模型类别分布很集中，这种情况少见，但机制上允许——本讲的代码实践会专门构造这种现象。
3. **分数语义是条件概率**：softmax 的分母包含 no-object 通道，所以 \(s_{q,k}\) 是「query q 属于类 k（在考虑它可能谁都不是之后）」的概率，K 个类别的分数之和可以远小于 1。这避免了「把 confidently no-object 的 query 硬归到某个类」的问题。

#### 4.1.2 核心流程

```
输入: mask_cls (Q, K+1) 分类 logits      mask_pred (Q, H, W) mask logits（已是原图尺寸）
  │
  ├─ ① softmax(mask_cls, dim=-1) 后切掉最后一列 no-object → scores (Q, K)
  ├─ ② 构造 labels: [0..K-1] 按 query 行复制再 flatten → (Q*K,)，扁平索引 idx 对应 (idx//K, idx%K)
  ├─ ③ scores.flatten(0,1) → (Q*K,)，topk(N, sorted=False) 取前 N 个
  │       → scores_per_image (N,)   topk_indices (N,) 扁平索引
  ├─ ④ labels_per_image = labels[topk_indices]        # 取出每个入选对的类别 k
  ├─ ⑤ topk_indices //= K                            # 扁平索引 → query 索引 q
  └─ ⑥ mask_pred = mask_pred[topk_indices]            # 按 query 索引取 N 张 mask
输出: N 组 (score, class, mask)
```

用公式表达分数（\(z_{q,j}\) 为 logits，第 K+1 维是 no-object）：

\[ s_{q,k} = \frac{\exp(z_{q,k})}{\sum_{j=1}^{K+1}\exp(z_{q,j})}, \quad k=1,\dots,K \]

注意：**先 softmax 再切片**，no-object 参与归一化分母。若先切片再 softmax，每行 K 个类别的概率会被强制归一到和为 1，语义完全不同（见 4.1.5 练习 1）。

#### 4.1.3 源码精读

先看调用点。上一讲已知：`INSTANCE_ON` 会强制 `sem_seg_postprocess_before_inference=True`，所以进入 `instance_inference` 时 `mask_pred` 已经还原到原图尺寸（函数首行注释也说明了这一点）：

[mask2former/maskformer_model.py:L257-L260](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L257-L260)：eval 分支中实例推理的入口。`self.instance_on` 开关打开时，把整图尺寸的 `mask_cls_result`（(Q, K+1)）与 `mask_pred_result`（(Q, H, W)）交给 `instance_inference`，结果挂到返回字典的 `"instances"` 键。外层包 `retry_if_cuda_oom`：Q 张原图尺寸的 mask 是推理中最占显存的中间产物之一，OOM 时按「清缓存重试 → 转 CPU」降级。

topk 的候选数量 N 来自配置项 `cfg.TEST.DETECTIONS_PER_IMAGE`（detectron2 基础配置注册的通用键，本仓库未覆盖，默认 100；可用 `print(cfg.TEST.DETECTIONS_PER_IMAGE)` 本地验证）：

[mask2former/maskformer_model.py:L160](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L160)：`from_config` 把 `cfg.TEST.DETECTIONS_PER_IMAGE` 注入为 `test_topk_per_image`。默认配置下它与 `NUM_OBJECT_QUERIES=100` 数值相同，但二者是**独立旋钮**——调小它即可在推理时减少输出实例数。

接下来是本模块的核心六行：

[mask2former/maskformer_model.py:L344-L357](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L344-L357)：query×class 展开与 topk 选取。逐行解读：

```python
# mask_pred is already processed to have the same shape as original input
image_size = mask_pred.shape[-2:]

# [Q, K]
scores = F.softmax(mask_cls, dim=-1)[:, :-1]
```

- L346：`image_size` 取自 mask 的 (H, W)，后面构造 `Instances` 时用——实例结果的坐标系统就是**原图尺寸**。
- L349：softmax 后 `[:, :-1]` 切掉 no-object 通道，得到 (Q, K) 概率表。

```python
labels = torch.arange(self.sem_seg_head.num_classes, device=self.device).unsqueeze(0).repeat(self.num_queries, 1).flatten(0, 1)
# scores_per_image, topk_indices = scores.flatten(0, 1).topk(self.num_queries, sorted=False)
scores_per_image, topk_indices = scores.flatten(0, 1).topk(self.test_topk_per_image, sorted=False)
labels_per_image = labels[topk_indices]
```

- L350：构造与打平分数表**同构**的类别标签向量：每行都是 `[0, 1, ..., K-1]`，重复 Q 行后 flatten 成 (Q*K,)。于是扁平索引 `idx` 满足 `labels[idx] == idx % K`。
- L351（注释掉的旧行）：MaskFormer 一代写死取 `self.num_queries` 个；二代改成可配置的 `test_topk_per_image`，这行注释是版本演化的活化石。
- L352：`flatten(0, 1)` 把 (Q, K) 压成 (Q*K,) 后 `topk`。`sorted=False` 表示不必按分数降序排——因为最终分数随后还要重打分（4.3 节），此处排序没有意义。
- L353：用扁平索引从 `labels` 里「查表」取出每个入选对的类别，避免了手工整除取余。

```python
topk_indices = topk_indices // self.sem_seg_head.num_classes
# mask_pred = mask_pred.unsqueeze(1).repeat(1, self.sem_seg_head.num_classes, 1).flatten(0, 1)
mask_pred = mask_pred[topk_indices]
```

- L355：扁平索引**整除 K** 得到 query 索引。变量名被原地复用（`topk_indices` 从「扁平索引」变成了「query 索引」），阅读时要留意。
- L356（注释掉的写法）：一个直观但浪费的方案——把 mask 复制 Q×K 份再按扁平索引取。复制会物化 Q×K 张全分辨率 mask（COCO 实例配置下是 8000 张），显存与带宽都不可接受。
- L357：正确做法是**先用 query 索引去重再取 mask**。同一 query 入选多次时，索引数组里出现重复的 q，`mask_pred[topk_indices]` 天然完成「同一张 mask 被取多次」的语义，无需显式复制。

#### 4.1.4 代码实践

**实践目标**：把 `instance_inference` 的前半段（展开 + topk + 索引还原）复制成独立脚本，用 5 个 query、10 个类别的手工玩具输入，把「扁平索引 → (query, class) 对」的还原过程逐步打印出来，并观察「同一 query 入选多次」现象。

**操作步骤**（示例代码，只需 PyTorch，无需 detectron2 与数据集）：

```python
# toy_topk.py（示例代码：逐行对应 mask2former/maskformer_model.py L344-L357）
import torch
import torch.nn.functional as F

Q, K = 5, 10  # 5 个 query；类别 0-9，外加 1 列 no-object

# 手工设计分类 logits，让现象可控：
mask_cls = torch.zeros(Q, K + 1)
mask_cls[0, 0] = 3.0   # q0 -> class 0，很自信
mask_cls[1, 1] = 2.0   # q1 -> class 1，中等自信
mask_cls[2, 3] = 2.5   # q2 -> class 3 与 class 7 双高（演示同一 query 入选两次）
mask_cls[2, 7] = 2.0
mask_cls[3, K] = 4.0   # q3 -> no-object 占优（演示低分 query 混入）
mask_cls[4, 9] = 2.2   # q4 -> class 9 最高（留给 4.2 演示 stuff 过滤）

def topk_part(mask_cls, num_queries, num_classes, test_topk_per_image):
    scores = F.softmax(mask_cls, dim=-1)[:, :-1]                       # L349
    labels = (torch.arange(num_classes).unsqueeze(0)                   # L350
              .repeat(num_queries, 1).flatten(0, 1))
    scores_per_image, topk_indices = scores.flatten(0, 1).topk(        # L352
        test_topk_per_image, sorted=False)
    labels_per_image = labels[topk_indices]                            # L353

    print("打平后的 (Q*K,) 分数表形状:", scores.flatten(0, 1).shape)
    print("topk 扁平索引:", topk_indices.tolist())
    print("还原出的 (query, class) 对:",
          [(int(i) // num_classes, int(i) % num_classes) for i in topk_indices])
    print("对应类别（查表法）:", labels_per_image.tolist())
    print("对应分数:", [round(float(s), 4) for s in scores_per_image])

    return scores_per_image, topk_indices // num_classes, labels_per_image  # L355

topk_part(mask_cls, Q, K, test_topk_per_image=10)
```

**需要观察的现象**（按 softmax 手算可预先推演，精确数值待本地验证）：

1. 打平后分数表形状为 `(50,)`（5×10）。
2. top-5 应当是设计好的五个对：`(q0,c0)≈0.667`、`(q4,c9)≈0.474`、`(q2,c3)≈0.426`、`(q1,c1)≈0.425`、`(q2,c7)≈0.259`——`topk_indices` 形如 `[0, 49, 23, 11, 27, ...]`，整除/取余 10 后还原回这些对。
3. 第 6~10 名几乎全部来自 **q1 的其余类别**（每个 ≈0.058，与 q1 的行内其它类并列）：也就是说 q1 这一个 query 会带着五六个不同的类别人选，同一 query 入选多次的现象肉眼可见。
4. q3 的十个分数全部 ≈0.015（no-object 占优把行内概率整体压低），排不进前列——no-object 留在 softmax 分母里的作用在此体现。

**预期结果**：输出 10 个 (score, class, query) 三元组，其中 query=1 出现约 6 次；`labels[topk_indices]` 与 `topk_indices % K` 两种方式得到的类别一致（这正是 L350 构造 labels 的意义）。并列分数（q1 的九个同分项）具体取哪几个，取决于 `topk` 对并列值的内部顺序，属实现细节，待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么必须「先 softmax 再切掉 no-object 列」，而不是「先切掉再 softmax」？

**答案**：先 softmax，no-object 参与归一化分母，\(s_{q,k}\) 的语义是「考虑了 query 可能谁都不是之后，它是类 k 的概率」，一行 K 个分数之和可以远小于 1；一个 confidently no-object 的 query 所有类别分数都低，自然排不进 topk。若先切再 softmax，每行 K 个类别被强制归一到和为 1，即使模型认为「这是个背景 query」，也一定会有某个类别拿到接近 1 的分数，反而把垃圾 query 推上榜单。

**练习 2**：`topk_indices // K` 之后变量语义变了，为什么源码可以「整除」而不用担心取错类别？

**答案**：扁平索引的编码规则是 `idx = q * K + k`，所以 `idx // K = q`、`idx % K = k`。类别在 L353 已经通过查表 `labels[topk_indices]` 取过了（`labels[idx] == idx % K`），L355 只需要 query 索引用来取 mask，两个信息都从同一个扁平索引还原，只是取法不同。

**练习 3**：把 `TEST.DETECTIONS_PER_IMAGE` 从 100 调到 300 会发生什么？

**答案**：实例数上限变为 min(300, 有效候选数)。候选池大小是 Q×K（如 COCO 实例 100×80=8000），所以 topk(300) 本身合法；但由于 Q=100 个 query 每个只贡献一张 mask，超过 100 个实例后必然出现大量「同 query 不同类」的重复 mask，且这些额外实例分数极低，对 AP 通常没有收益还拖慢评估——这也是默认值取 100（与 Q 对齐）的原因。

### 4.2 thing 类过滤与占位 pred_boxes

#### 4.2.1 概念说明

topk 选出的 (query, class) 对里，类别可能是 stuff（天空、草地）。stuff 是「弥散在整张图里的物质」，没有「一个个实例」的概念——把「草地」当一个实例输出、算实例 AP 是没有意义的。所以当模型同时开启全景输出（`panoptic_on=True`，说明标签空间是 thing+stuff 混合的全景空间）时，实例分支要**把入选类别不在 thing 集合里的对过滤掉**。

第二个问题：detectron2 生态的 `Instances` 约定里有 `pred_boxes` 字段，很多下游（可视化器、检测式评估器）默认它存在。但 mask classification 模型**原生不预测框**——它的空间输出只有 mask。源码的处理是给一个全零的 (N, 4) 占位框，并在注释里留了一行「从 mask 提取真实框」的慢速替代方案。

#### 4.2.2 核心流程

```
topk 之后: scores_per_image (N,), labels_per_image (N,), mask_pred (N, H, W)
  │
  ├─ 若 panoptic_on:
  │    逐个检查 labels_per_image[i] 是否 ∈ thing_dataset_id_to_contiguous_id.values()
  │    → keep 布尔向量 → 三个张量按 keep 同步过滤
  │
  ├─ result = Instances(image_size)
  ├─ result.pred_masks = (mask_pred > 0).float()        # logit>0 即 sigmoid>0.5 二值化
  ├─ result.pred_boxes = Boxes(torch.zeros(N, 4))       # 全零占位框
  └─ （注释保留）BitMasks(mask_pred > 0).get_bounding_boxes()  # 慢速真实框
```

#### 4.2.3 源码精读

[mask2former/maskformer_model.py:L359-L374](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L359-L374)：thing 过滤与结果对象的组装。

```python
# if this is panoptic segmentation, we only keep the "thing" classes
if self.panoptic_on:
    keep = torch.zeros_like(scores_per_image).bool()
    for i, lab in enumerate(labels_per_image):
        keep[i] = lab in self.metadata.thing_dataset_id_to_contiguous_id.values()

    scores_per_image = scores_per_image[keep]
    labels_per_image = labels_per_image[keep]
    mask_pred = mask_pred[keep]
```

- L360：开关判断读的是**模型自己的 `panoptic_on` 配置**，不是「数据集是不是全景数据集」。这意味着：若在全景数据集配置上手动设 `PANOPTIC_ON: False` 且 `INSTANCE_ON: True`，这个过滤会被跳过，stuff 类就可能混进实例输出——一个值得注意的行为细节（见练习 2）。
- L361-L363：用 Python 循环逐个判断 `lab in ...values()`。`lab` 是 0 维张量，与字典 values 里的 int 比较得到布尔值。写法直白但有每实例一次的 Python 开销；若追求性能可预计算一张长度为 K 的 thing 布尔查找表再做向量化索引。
- L365-L367：三个张量（分数、类别、mask）必须**同步**过滤，任何漏掉一个都会造成字段错位。

判断依据的查找表在数据集注册时构造。以 ADE20K 全景为例：

[mask2former/data/datasets/register_ade20k_panoptic.py:L352-L365](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/datasets/register_ade20k_panoptic.py#L352-L365)：遍历 150 类类别表，`isthing` 为真的类别写入 `thing_dataset_id_to_contiguous_id`（「原始类别 id → 连续训练 id」）。实例分支里 `labels_per_image` 存的正是**连续训练 id**，所以与 `.values()` 直接可比。对纯实例数据集（如 COCO instance 的 80 类全是 thing），该表覆盖全部类别，过滤天然全过；只有 thing+stuff 混合空间（COCO panoptic 133 类、ADE20K 150 类）过滤才会真正裁掉条目。

```python
result = Instances(image_size)
# mask (before sigmoid)
result.pred_masks = (mask_pred > 0).float()
result.pred_boxes = Boxes(torch.zeros(mask_pred.size(0), 4))
# Uncomment the following to get boxes from masks (this is slow)
# result.pred_boxes = BitMasks(mask_pred > 0).get_bounding_boxes()
```

- L371：mask 二值化。此时 `mask_pred` 还是 **logits**（注释 "before sigmoid" 提醒这一点），`> 0` 等价于 `sigmoid > 0.5`，得到 float 的 0/1 mask，直接作为 `pred_masks`（下游 RLE 编码只认二值）。
- L372：全零 (N, 4) 占位框——所有实例的框都是原点处的退化框，**不携带任何几何信息**。
- L373-L374（注释）：`BitMasks(...).get_bounding_boxes()` 能从每张 mask 提取紧密包围框，但要遍历像素，作者标注了 "this is slow"。占位框可行的前提是：实例分割评估（COCO segm AP）只用 RLE mask，不需要框；需要真实框的用户取消这行注释即可。

#### 4.2.4 代码实践

**实践目标**：在 4.1.4 玩具脚本的基础上加入 thing 过滤，观察同一批 topk 结果在 `panoptic_on=True/False` 下的实例数差异。

**操作步骤**（示例代码，续写 `toy_topk.py`）：

```python
# 约定类别 0-4 是 thing、5-9 是 stuff，模拟全景标签空间
THING_IDS = {0, 1, 2, 3, 4}

def thing_filter_part(scores, query_idx, labels, mask_pred_dummy, panoptic_on):
    if panoptic_on:                                                    # L360
        keep = torch.zeros_like(scores).bool()
        for i, lab in enumerate(labels):
            keep[i] = int(lab) in THING_IDS                            # L363 的玩具版
        scores, labels = scores[keep], labels[keep]
        query_idx = query_idx[keep]
    n_boxes = torch.zeros(len(scores), 4)                              # L372 的玩具版
    return scores, labels, query_idx, n_boxes

# 分别打印四种组合的实例数
for topk, pan in [(10, False), (10, True), (3, False), (3, True)]:
    s, qi, lb = topk_part(mask_cls, Q, K, test_topk_per_image=topk)
    s, lb, qi, boxes = thing_filter_part(s, qi, lb, None, pan)
    print(f"topk={topk:>2}  panoptic_on={pan}  -> 实例数={len(s)}  类别={lb.tolist()}")
```

**需要观察的现象**（待本地验证，可先手算预测）：

- `(10, False)`：10 个实例，其中包含 `(q4, c9)` 这类 stuff 对。
- `(10, True)`：`(q4, c9)` 被裁掉；此外 4.1.4 中混进榜单的 q1 并列项里类别落在 5-9 的也会被裁掉，预计剩 4~7 个（取决于并列项的内部顺序）。
- `(3, False)`：前三是 `(q0,c0)、(q4,c9)、(q2,c3)`。
- `(3, True)`：只剩 2 个（`(q4,c9)` 被裁）。

**预期结果**：`panoptic_on` 的过滤只减不增；被过滤的实例一定是类别落在 stuff 集合里的那些；`boxes` 形状恒为 (实例数, 4) 且全零。

#### 4.2.5 小练习与答案

**练习 1**：为什么纯实例分割配置（如 `configs/coco/instance-segmentation/maskformer2_R50_bs16_50ep.yaml`）里这个过滤是「空转」的？

**答案**：见 [configs/coco/instance-segmentation/maskformer2_R50_bs16_50ep.yaml:L39-L44](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/instance-segmentation/maskformer2_R50_bs16_50ep.yaml#L39-L44)：该配置 `PANOPTIC_ON: False`，过滤分支根本不进入；同时 COCO instance 的 80 个类别全部是 thing，`thing_dataset_id_to_contiguous_id.values()` 覆盖所有类别 id，即便进入过滤也一个都不会裁。过滤真正起作用的是全景配置（`PANOPTIC_ON: True` 且 `INSTANCE_ON: True`，如 COCO/ADE20K 全景配置在算实例指标时）。

**练习 2**：如果把一个全景数据集配置改成 `PANOPTIC_ON: False`、`INSTANCE_ON: True`，实例输出会怎样？

**答案**：`self.panoptic_on` 为假，thing 过滤被跳过，stuff 类别的 (query, class) 对会保留在 `instances` 里。这与「实例分割只输出可数对象」的直觉相悖——源码用模型开关而非数据集元数据来决定过滤，是一个需要读者留意的行为约定。

**练习 3**：占位 `pred_boxes` 会不会让下游的 COCO bbox 评估得出错误结论？

**答案**：会——全零框没有几何意义，任何基于框的指标都会失真。但正常使用路径不会触发：实例分割评估只用 `pred_masks` 的 RLE（`InstanceSegEvaluator` 以 segm 任务评估），框不参与；只有用户自行把任务改成 bbox 或用依赖框的下游组件时才需要启用注释里的 `BitMasks(...).get_bounding_boxes()` 慢速路径。

### 4.3 mask 平均概率重打分与最终 Instances

#### 4.3.1 概念说明

topk 给出的分数 \(s_{q,k}\) 只回答了「这个 query 多像类 k」，没有回答「它的 mask 画得好不好」。一个类别分数很高、mask 却大面积模糊（sigmoid 概率在 0.5 附近徘徊）的实例，置信度应当被压低。

MaskFormer 的做法是给每个实例再算一个 **mask 平均概率**：在二值 mask 覆盖的区域内，对 mask logits 过 sigmoid 后求平均。区域清晰锋利时平均概率接近 1；边界含糊或整体不确定时接近 0.5 甚至更低。最终分数是两项相乘：

\[ \hat{s} = s_{q,k} \cdot \frac{1}{|M| + \epsilon}\sum_{p \in M}\sigma(m(p)), \qquad M = \{p : m(p) > 0\} \]

直觉上这是一个近似的联合置信度：「它是类 k」**且**「它的 mask 在自己的预测区域内确实大概率是前景」。分母加 \(\epsilon=10^{-6}\) 是为了在 mask 全空（|M|=0）时避免除零，此时实例分数退化为约 0。

#### 4.3.2 核心流程

```
mask_pred (N, H, W) logits
  ├─ pred_masks = (mask_pred > 0).float()          # 二值区域 M
  ├─ avg_prob = Σ_{p∈M} sigmoid(mask_pred) / (|M| + 1e-6)
  ├─ result.scores = scores_per_image * avg_prob    # 重打分
  ├─ result.pred_classes = labels_per_image
  └─ 返回 Instances（image_size, pred_masks, pred_boxes, scores, pred_classes）
```

#### 4.3.3 源码精读

[mask2former/maskformer_model.py:L376-L380](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L376-L380)：重打分与字段挂载。

```python
# calculate average mask prob
mask_scores_per_image = (mask_pred.sigmoid().flatten(1) * result.pred_masks.flatten(1)).sum(1) / (result.pred_masks.flatten(1).sum(1) + 1e-6)
result.scores = scores_per_image * mask_scores_per_image
result.pred_classes = labels_per_image
return result
```

- L377：一行写完「区域平均概率」。`flatten(1)` 把每张 (H, W) mask 压成长度 H·W 的向量后逐像素相乘：二值 mask 为 1 的位置保留 sigmoid 概率、为 0 的位置清零；`sum(1)` 即分子 \(\sum_{p\in M}\sigma(m(p))\)。分母 `pred_masks.flatten(1).sum(1)` 就是区域面积 |M|（全空 mask 时为 0，靠 `+1e-6` 兜底，结果趋于 0 而不是 NaN）。
- L378：最终分数 = 分类分数 × mask 平均概率。这就是 4.2 节 `sorted=False` 的原因——topk 的排序在这里才被真正改写。
- L379-L380：挂上类别字段并返回 `Instances`。至此输出对象拥有 detectron2 约定的全部四个字段：`pred_masks`（原图尺寸二值 mask）、`pred_boxes`（全零占位）、`scores`（重打分后的置信度）、`pred_classes`（连续训练 id）。

两个下游消费现场（本讲只指路，不展开）：

- [demo/predictor.py:L63-L65](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/demo/predictor.py#L63-L65)：可视化分支在 没有 `panoptic_seg` 键时调用 `draw_instance_predictions(predictions=instances)`，按 `scores`/`pred_classes`/`pred_masks` 画实例。
- [mask2former/evaluation/instance_evaluation.py:L48](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/evaluation/instance_evaluation.py#L48)：评估分支把每张图的 `"instances"` 结果串起来转 COCO json 计算 AP（细节在 u7-l2 精读）。

另外注意一个对比：`scores_per_image`（分类分数）是在 topk **之前**用于挑选实例的；而重打分后的 `result.scores` 只影响**排序与展示**，不再改变谁被选中。挑选与定序分离，是这段后处理的一个结构性特点。

#### 4.3.4 代码实践

**实践目标**：在玩具脚本中补全重打分环节，验证「分数 = 分类分数 × mask 平均概率」以及 4.1.4 练习 3 中 `TEST.DETECTIONS_PER_IMAGE` 的实际效果。

**操作步骤**（示例代码，续写 `toy_topk.py`）：

```python
torch.manual_seed(0)
mask_pred = torch.randn(Q, 24, 32)  # (Q, H, W) 随机 mask logits，模拟"边界含糊"

def rescore_part(scores, labels, query_idx, mask_pred):
    masks_all = mask_pred[query_idx]                                  # L357
    pred_masks = (masks_all > 0).float()                              # L371
    avg_prob = (masks_all.sigmoid().flatten(1) * pred_masks.flatten(1)).sum(1) / (
        pred_masks.flatten(1).sum(1) + 1e-6)                          # L377
    return scores, scores * avg_prob, avg_prob                        # L378

s, qi, lb = topk_part(mask_cls, Q, K, test_topk_per_image=10)
raw, rescored, avg = rescore_part(s, lb, qi, mask_pred)
for i in range(len(raw)):
    print(f"实例{i}: class={int(lb[i]):>2}  分类分数={float(raw[i]):.4f}  "
          f"mask平均概率={float(avg[i]):.4f}  重打分={float(rescored[i]):.4f}")
```

**需要观察的现象**（随机 logits 下可定性预测，精确数值待本地验证）：

1. `mask平均概率` 全部落在大约 0.5~0.7 之间：随机 logits 下 sigmoid 大多在 0.5 附近，且只在 logit>0（即概率>0.5）的区域内平均。
2. 每个 `重打分` 都小于对应的 `分类分数`，且比值恰好等于 `mask平均概率`——重打分只会压低分数。
3. 若把某一行 `mask_pred` 全部置为很负的值（例如 `mask_pred[0] = -10`），该 query 的 mask 全空，`pred_masks.sum()=0`，`avg_prob≈0`（分母只剩 1e-6），重打分接近 0——验证除零兜底行为。

**预期结果**：输出表格展示三列数值满足乘法关系；`TEST.DETECTIONS_PER_IMAGE`（即 `topk_part` 的第三个参数）从 10 改为 3 时实例数随之变为 3，重打分只改变排序不影响数量。

#### 4.3.5 小练习与答案

**练习 1**：为什么不直接用 `mask_pred.sigmoid().mean()`（全图平均）而非要在二值区域内平均？

**答案**：全图平均把大量背景像素（概率接近 0）也计入分母，mask 只占图像一小部分时结果被背景稀释，任何实例的 avg_prob 都会很小且彼此拉不开差距。区域内平均度量的才是「模型对自己声称的前景区域的自信程度」，与「mask 质量」直接对应。

**练习 2**：重打分发生在 topk 之后。如果一个实例分类分数高、mask 很差，它还会被选中吗？

**答案**：会。topk 只看分类分数，mask 质量不参与挑选；重打分只把它的最终 `scores` 压低。对 AP 这类按分数排序积分的指标，低分实例排在后面影响有限；但如果下游按某个分数阈值过滤（如可视化），这类实例就会被剔除——这正是重打分的主要用途。

**练习 3**：`result.pred_masks` 存的是 float 的 0/1 值而不是 bool，为什么？

**答案**：`(mask_pred > 0)` 本是 bool 张量，`.float()` 把它转成浮点。一方面与 detectron2 `Instances` 字段的常见类型约定一致（后续可整体 `.to(device)`、参与浮点运算）；另一方面评估器里会把它转成 RLE 编码（`pycocotools.mask_util.encode` 需要 uint8 输入，转换前还有一步类型处理），浮点 0/1 与 bool 在数值语义上等价，这里 float 是作者的实现习惯，功能上二者皆可。

## 5. 综合实践

**任务**：用真实模型跑一次纯实例分割推理，把本讲三个模块（topk 选取 → thing 过滤 → 重打分）串联验证。

1. **准备**：按 u1-l4 的方式下载 [configs/coco/instance-segmentation/maskformer2_R50_bs16_50ep.yaml](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/instance-segmentation/maskformer2_R50_bs16_50ep.yaml) 对应的 MODEL_ZOO 权重（该配置 `INSTANCE_ON: True`、`PANOPTIC_ON: False`，thing 过滤空转）。
2. **跑通**：`python demo/demo.py --config-file <上述yaml> --input your.jpg --opts MODEL.WEIGHTS <ckpt> MODEL.DEVICE cpu`，观察 [demo/demo.py:L118-L127](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/demo/demo.py#L118-L127) 打印的 `detected N instances`——N 应等于 `min(TEST.DETECTIONS_PER_IMAGE, 有效候选数)`。
3. **分析分数构成**：在 demo 的 `predictions` 上把 `instances.scores` 降序排列，画出直方图；再用 `instances.pred_masks` 反推每个实例的 mask 平均概率（`pred_masks` 已二值化，可配合重新前向拿到 logits，或直接用玩具脚本验证关系式）。观察高分实例是否集中在「分类分数与 mask 质量双高」的区域。
4. **扫参**：分别用 `--opts TEST.DETECTIONS_PER_IMAGE 10 / 50 / 100` 重复第 2 步，记录 `detected N instances` 与可视化图的变化；再换成 COCO **全景**配置（`PANOPTIC_ON: True` 且 `INSTANCE_ON: True`），确认 thing 过滤后实例里不再出现 stuff 类别（可在 `instances.pred_classes` 与 `metadata.thing_dataset_id_to_contiguous_id.values()` 之间做集合包含检查）。

说明：第 1~4 步依赖权重下载与运行环境，具体数值**待本地验证**；无 GPU 时可用 `MODEL.DEVICE cpu`。

## 6. 本讲小结

- `instance_inference` 把 mask classification 输出「实例化」：**(Q, K) 分数表打平 → topk 取 N 个 (query, class) 对 → 用 `idx // K` 还原 query 取 mask**，实例数由 `TEST.DETECTIONS_PER_IMAGE` 决定而非阈值决定。
- 分类分数是**先 softmax（含 no-object 分母）再切最后一列**得到的条件概率；同一 query 携带多张类别票时可以入选多次。
- `panoptic_on` 开启时按 `thing_dataset_id_to_contiguous_id.values()` **过滤 stuff 类**；纯实例数据集上该过滤天然空转，且判断依据是模型开关而非数据集类型。
- `pred_boxes` 是**全零占位框**：模型原生不预测框，segm 评估只需 RLE mask，注释里保留了从 mask 提取真实框的慢速方案。
- 最终分数经**重打分**：`scores = 分类分数 × mask 区域内平均 sigmoid 概率`（+1e-6 防除零），挑选与定序分离——topk 只看分类分数，重打分只影响排序与下游过滤。

## 7. 下一步学习建议

本讲结束了 eval 三分支中的第二个。下一讲 **u6-l3 全景分割推理分支** 将精读 `panoptic_inference`（`maskformer_model.py` L286-L342）：`OBJECT_MASK_THRESHOLD` 预过滤、argmax 像素分配、`mask_area / original_area` 重叠剔除与 stuff 段合并——它同样以「分类分数 × mask」为原料，但产出的是一张整图 id 图而非实例列表，可以与本讲的 topk 路径对照着读。之后进入单元七评估体系（u7-l1 起），看 `instances` 输出如何在 `InstanceSegEvaluator` 中变成 AP。
