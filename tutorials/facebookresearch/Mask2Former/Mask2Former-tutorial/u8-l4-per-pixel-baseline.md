# 对照实验：PerPixelBaselineHead 与 Plus 版

## 1. 本讲目标

本讲是「扩展实践」单元的收尾（前三讲 u8-l1/u8-l2/u8-l3 分别替换了三段式架构的三大组件，本讲换掉的是**整个预测范式**）。学完后你应该能够：

1. 说出 `PerPixelBaselineHead` 与 `PerPixelBaselinePlusHead` 在论文方法论中的角色：它们是隔离「mask classification 范式收益」的两级对照基线。
2. 精读 `PerPixelBaselineHead.layers`：同一像素解码器的 `mask_features` 上，一个静态 1×1 卷积逐像素分类器如何工作，head 内置的全分辨率交叉熵损失长什么样。
3. 精读 `PerPixelBaselinePlusHead`：它把 1×1 分类器换成 `StandardTransformerDecoder(mask_classification=False)`——逐类「分类核」从静态参数变成由 Transformer 解码器按输入动态生成的嵌入；并说清 `mask_classification=False` 时解码器输出、损失键、深监督的全部变化。
4. 论证一个重要的接线事实：per-pixel 基线头**不能**配本仓库的 `MaskFormer` meta arch，而要配 detectron2 的 `SemanticSegmentor`，训练 GT 来自 dataset dict 的 `sem_seg` 键。
5. 动手构建两个基线头，跑通前向，对比输出形状、损失键与参数量。

## 2. 前置知识

本讲默认你已完成 u3-l2（MaskFormerHead 组装器）与 u8-l2（像素解码器契约），这里只回顾必要概念。

- **两种分割范式**（u1-l1、u3-l1 反复出现的主线）：
  - **per-pixel classification（逐像素分类）**：对每个像素独立做 K 类 softmax，代表是 DeepLab 系列。预测头是「每个类别一个向量，与像素嵌入做点积」——最简单的实现就是 1×1 卷积。
  - **mask classification（掩码分类）**：模型输出 Q 个「实例/类别段」级别的预测 \((class_q, mask_q)\)，再在推理阶段融合。MaskFormer/Mask2Former 的主线，代价是要引入匈牙利匹配、no-object 通道、集合损失一整套机制（u5 单元）。
- **三段式架构**：backbone → pixel decoder → Transformer decoder。`MaskFormerHead` 是组装器（u3-l2），像素解码器产出 `mask_features`（1/4 分辨率逐像素嵌入）与 `multi_scale_features`（1/32、1/16、1/8 三尺度，u3-l4/u3-l5）。
- **`SEM_SEG_HEADS_REGISTRY` 与「import 即注册」**（u1-l3、u8-l2）：head 与像素解码器共用 detectron2 的这张注册表；`mask2former/modeling/__init__.py` 的 import 决定哪些类被自动注册。本讲的两个基线头也在这个 import 清单里。
- **`@configurable` + `from_config`**：外部以 `Class(cfg, input_shape)` 构建时实际走 `from_config` 翻译配置（u1-l5、u8-l1）。
- **`build_pixel_decoder` 工厂**（u8-l2）：按 `PIXEL_DECODER_NAME` 从注册表构建像素解码器，只做「有 `forward_features` 方法」的弱校验。
- **对照实验（消融，ablation）**：论文里「A 比 B 好」的结论只有在控制其他变量相同后才可信。本讲的两个基线头就是 MaskFormer 系列论文为「范式对比」专门写进代码库的受控实验装置。

一个需要先澄清的措辞问题：本讲规格里说「Plus 版把 1×1 conv 换成两层卷积」，**这与当前源码不符**。源码中 Plus 版做的是：删掉 1×1 conv 预测器，换成一个完整的 `StandardTransformerDecoder`（v1 解码器，其中生成分类核的 `mask_embed` 是三层 MLP）。本讲以源码为准讲解，并在 4.3.1 说明这一替换在论文消融中的准确含义。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [mask2former/modeling/meta_arch/per_pixel_baseline.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/meta_arch/per_pixel_baseline.py) | **本讲主线**：两个 per-pixel 基线头，共 243 行 |
| [mask2former/modeling/transformer_decoder/maskformer_transformer_decoder.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/maskformer_transformer_decoder.py) | v1 解码器 `StandardTransformerDecoder`，Plus 版的预测器；`mask_classification=False` 分支在本讲精读 |
| [mask2former/modeling/transformer_decoder/transformer.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/transformer.py) | v1 Transformer 本体（DETR 移植），Plus 版预测器的内部积木 |
| [mask2former/modeling/meta_arch/mask_former_head.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/meta_arch/mask_former_head.py) | 对照物：mask classification 头 `MaskFormerHead`（u3-l2 精读过） |
| [mask2former/maskformer_model.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py) | 顶层 meta arch，用来论证「基线头配它会崩」的两处失败点 |
| [mask2former/data/dataset_mappers/mask_former_semantic_dataset_mapper.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/dataset_mappers/mask_former_semantic_dataset_mapper.py) | 语义分割 mapper，产出 per-pixel 基线训练所需的 `sem_seg` GT 键 |
| [mask2former/modeling/__init__.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/__init__.py) | import 链：两个基线头在此被 import（即被注册） |
| [mask2former/config.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/config.py) | `TRANSFORMER_IN_FEATURE`、`DEEP_SUPERVISION`、`NUM_OBJECT_QUERIES` 等默认值 |
| [configs/ade20k/semantic-segmentation/maskformer2_R50_bs16_160k.yaml](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/ade20k/semantic-segmentation/maskformer2_R50_bs16_160k.yaml) | R50 语义分割配置：主模型一侧的取值参照（改造成基线配置的起点） |

注意一个先说结论的事实：**仓库的 `configs/` 目录里没有任何使用这两个头的 yaml**（对 `configs/` 全量搜索 `PerPixelBaseline` 与 `SEM_SEG_HEAD_NAME` 均无匹配，所有配置的 head 都是 `MaskFormerHead` 或视频版）。这两个类是「论文消融的代码化存档」——代码可用，但配置要自己写，4.4 节给出写法。

## 4. 核心概念与源码讲解

### 4.1 对照实验的动机：两级基线隔离了什么

#### 4.1.1 概念说明

MaskFormer/Mask2Former 论文的核心主张是：**mask classification 范式优于 per-pixel classification 范式**。要支撑这句话，最朴素的对比是「DeepLab 风格模型 vs Mask2Former」，但这个对比混杂了太多同时变化的因素：backbone 不同、像素解码器不同、有没有 Transformer 解码器不同、损失函数不同。审稿人（以及任何较真的读者）都可以质疑：性能提升到底来自范式，还是来自你多加的模块？

正确的做法是搭一座「控制变量的阶梯」，每级只改一个因素：

```text
第 0 级  PerPixelBaselineHead      同 backbone + 同像素解码器 + 静态 1×1 逐像素分类器
                                   （pure per-pixel classification 的下界对照）
                 │ 只加「Transformer 解码器」，分类方式仍是逐像素
                 ▼
第 1 级  PerPixelBaselinePlusHead  分类核改由 Transformer 解码器动态生成，仍是逐像素分类
                                   （中间对照：补齐解码器容量差异）
                 │ 只改「分类范式」：逐像素分类 → 掩码分类
                 ▼
第 2 级  MaskFormerHead            完整 mask classification（query × class + mask）
```

于是「第 1 级 → 第 2 级」的提升就是**纯范式收益**（backbone、像素解码器、Transformer 解码器全部相同，只有分类范式不同）；「第 0 级 → 第 1 级」则是解码器容量本身的收益。两级基线把一个笼统的「我们的模型更好」拆成了两个可归因的差值。

「变量受控」不是口头承诺，而是写在代码里的：两个基线头的 `from_config` 都调用**同一个** `build_pixel_decoder(cfg, input_shape)` 工厂，与 `MaskFormerHead` 用的一字不差。也就是说，只要配置里 `PIXEL_DECODER_NAME` 相同，三个头拿到的是结构完全相同的像素解码器。

#### 4.1.2 核心流程

从代码视角看这座阶梯的受控方式：

```text
共同部分（三个头完全一致）
  ├─ input_shape：按 stride 排序的 backbone 特征表
  ├─ build_pixel_decoder(cfg, input_shape) → 同一个像素解码器
  └─ forward_features(features) → (mask_features 1/4, 第二项, multi_scale_features)

只在「predictor」处分岔
  ├─ 第 0 级：predictor = Conv2d(mask_dim → K, 1×1)          静态核
  ├─ 第 1 级：predictor = StandardTransformerDecoder(False)   动态核（无类别头）
  └─ 第 2 级：predictor = build_transformer_decoder(..., True) 动态核 + 类别头 + 匹配损失
```

#### 4.1.3 源码精读

- [per_pixel_baseline.py:L17-L18](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/meta_arch/per_pixel_baseline.py#L17-L18) 与 [per_pixel_baseline.py:L126-L127](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/meta_arch/per_pixel_baseline.py#L126-L127)：两个头注册进 `SEM_SEG_HEADS_REGISTRY`——和 `MaskFormerHead`（[mask_former_head.py:L18-L19](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/meta_arch/mask_former_head.py#L18-L19)）共用同一张表，配置里改 `MODEL.SEM_SEG_HEAD.NAME` 即可在三者间切换。
- [modeling/__init__.py:L6](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/__init__.py#L6)：`from .meta_arch.per_pixel_baseline import PerPixelBaselineHead, PerPixelBaselinePlusHead`——`import mask2former` 时两个头自动可用，无需额外 import。
- [per_pixel_baseline.py:L90](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/meta_arch/per_pixel_baseline.py#L90)：`from_config` 里 `"pixel_decoder": build_pixel_decoder(cfg, input_shape)`。对比 [mask_former_head.py:L105](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/meta_arch/mask_former_head.py#L105)——同一工厂同一调用方式，这就是「像素解码器受控」的直接代码证据。
- [configs/ade20k/semantic-segmentation/maskformer2_R50_bs16_160k.yaml:L3-L13](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/ade20k/semantic-segmentation/maskformer2_R50_bs16_160k.yaml#L3-L13)：主模型（第 2 级）在 ADE20K 语义分割上的取值参照。把 `META_ARCHITECTURE` 与 `SEM_SEG_HEAD.NAME` 两行改掉、其余保持，就是一份受控的基线配置（4.4 节实践）。

#### 4.1.4 代码实践

**实践目标**：把「受控变量对照表」写出来，为后续跑数字做好准备。

**操作步骤**：

1. 画一张三列对照表（PerPixelBaselineHead / PerPixelBaselinePlusHead / MaskFormerHead），行为：backbone、pixel decoder、predictor 类型、损失函数、监督粒度（逐像素 / 逐 mask）、是否需要匹配器。
2. 每一行标注「三级是否相同」，相同的打勾。
3. 对每个打勾项，在源码里找到证据行号（本讲各节已给出）。

**需要观察的现象**：表里只有 predictor 类型、损失函数、监督粒度三行在三级之间不同；其余全部相同。

**预期结果**：得到一张可以直接放进实验报告的消融对照表。本实践为纸面练习，无需运行代码。

#### 4.1.5 小练习与答案

**练习 1**：为什么「第 0 级 vs 第 2 级」的对比不足以证明范式优势？

**参考答案**：两级之间同时改变了两个因素——增加了 Transformer 解码器（容量/结构变化）和切换了分类范式。性能差无法归因：可能全部来自解码器，也可能来自范式。加入第 1 级（有解码器、无范式切换）后，「1→2」的差值才把解码器差异排除在外。

**练习 2**：代码层面哪一行能证明「三个头的像素解码器受控」？

**参考答案**：三个头的 `from_config` 都调用 `build_pixel_decoder(cfg, input_shape)`：基线在 [per_pixel_baseline.py:L90](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/meta_arch/per_pixel_baseline.py#L90)，MaskFormerHead 在 [mask_former_head.py:L105](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/meta_arch/mask_former_head.py#L105)。配置相同则解码器实例结构相同。

**练习 3**：仓库里为什么搜不到 per-pixel 基线的训练配置？

**参考答案**：`configs/` 下所有图像任务的 head 都是 `MaskFormerHead`（可自行全局搜索验证）。两个基线头是论文消融的代码存档，供复现对照实验的人自行编写配置使用（4.4 节给出参考写法）。

### 4.2 PerPixelBaselineHead：layers 与静态 1×1 分类器

#### 4.2.1 概念说明

`PerPixelBaselineHead` 是第 0 级基线：像素解码器照旧产出 1/4 分辨率的逐像素嵌入 `mask_features`（形状 \((B, C_{mask}, H/4, W/4)\)），预测器是一个 **1×1 卷积**，把它变成 \((B, K, H/4, W/4)\) 的逐像素类别 logits。数学上，1×1 卷积等价于对每个像素位置 \(u\) 做

\[
\hat{y}_c(u) = w_c^{\top} \phi(u) + b_c, \qquad c = 1,\dots,K
\]

其中 \(\phi(u)\) 是像素 \(u\) 的嵌入向量，\(\{w_c, b_c\}\) 是**与输入无关**的学习参数（静态核）。损失是逐像素交叉熵（忽略 `ignore_value` 标注的像素）：

\[
\mathcal{L}_{\text{ce}} = -\frac{1}{|\Omega|}\sum_{u \in \Omega}
\log \frac{\exp\big(\hat{y}_{g(u)}(u)\big)}{\sum_{c=1}^{K}\exp\big(\hat{y}_c(u)\big)}
\]

其中 \(g(u)\) 是像素 \(u\) 的 GT 类别。注意与 mask classification 的三点对照：

- 没有 query、没有匹配器、没有 no-object 通道——u5 单元的整套机器都不需要；
- 损失在 **head 内部**计算（`self.losses`），而不是交给 meta arch 的 `SetCriterion`；
- 交叉熵在**全分辨率**上算（先 ×4 上采样），而不是采样点（对比 u5-l2 的 PointRend 式采样）。

#### 4.2.2 核心流程

```text
构造（from_config → __init__）
  1. input_shape 按 stride 排序，记下 in_features
  2. common_stride = 4（硬编码，与像素解码器契约对齐，u8-l2）
  3. build_pixel_decoder 构建像素解码器（受控变量）
  4. predictor = Conv2d(mask_dim → K, 1×1)，c2_msra_fill 初始化   ← 唯一的「范式」部件

前向 forward(features, targets=None)
  ├─ x = layers(features)
  │    ├─ mask_features, _, _ = pixel_decoder.forward_features(features)  # 三元组取第一项
  │    └─ x = predictor(mask_features)                                    # (B,K,H/4,W/4)
  ├─ 训练：return (None, self.losses(x, targets))
  │        losses 内部：x.float() → ×4 双线性上采样 → 全分辨率 cross_entropy
  └─ 推理：x ×4 双线性上采样 → return (x, {})       # head 自己完成 1/4 → 原尺寸
```

#### 4.2.3 源码精读

- [per_pixel_baseline.py:L67-L80](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/meta_arch/per_pixel_baseline.py#L67-L80)：构造器主体。L67 把 `input_shape` 按 stride 排序；L73 硬编码 `self.common_stride = 4`——与像素解码器 `mask_features` 的 1/4 分辨率契约严格配套；L76 挂上像素解码器；L77-L79 创建 **1×1 卷积预测器**：`Conv2d(self.pixel_decoder.mask_dim, num_classes, kernel_size=1)`，L80 用 `c2_msra_fill` 初始化。参数量即 \(K \times C_{mask} + K\)（ADE20K-150、\(C_{mask}=256\) 时约 38.6K）——整个头里唯一「懂类别」的部件只有这几万个参数。
- [per_pixel_baseline.py:L82-L92](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/meta_arch/per_pixel_baseline.py#L82-L92)：`from_config`。L86 按 `IN_FEATURES` 过滤输入特征；L90 调 `build_pixel_decoder`（受控证据，见 4.1.3）；其余读 `IGNORE_VALUE / NUM_CLASSES / LOSS_WEIGHT`。
- [per_pixel_baseline.py:L109-L112](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/meta_arch/per_pixel_baseline.py#L109-L112)：**本模块的核心两行**。L110 调 `forward_features` 拿三元组，只取第一项 `mask_features`（后两项丢弃——逐像素分类不需要多尺度特征，这正是它与 MaskFormerHead 最干净的分野，见 [mask_former_head.py:L118-L132](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/meta_arch/mask_former_head.py#L118-L132) 对三元组的逐路分发）；L111 过 1×1 卷积得逐像素 logits。
- [per_pixel_baseline.py:L94-L107](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/meta_arch/per_pixel_baseline.py#L94-L107)：`forward` 的双分支。训练返回 `(None, losses)` **二元组**（L102）——这是 detectron2 语义分割 head 的经典契约，与 `MaskFormerHead` 返回预测 dict 截然不同（4.4 节展开）。推理分支 L104-L106 在 **head 内部**做 ×4 双线性上采样后返回 `(x, {})`——上采样责任在 head 而非 meta arch，与 `MaskFormer.forward` 的推理上采样（[maskformer_model.py:L222-L227](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L222-L227)）分工相反。
- [per_pixel_baseline.py:L114-L123](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/meta_arch/per_pixel_baseline.py#L114-L123)：损失三连。L115 `predictions.float()` 带 upstream 注释链接（fp16 下交叉熵的数值问题）；L116-L118 先 ×4 上采样到 padding 全分辨率；L119-L121 `F.cross_entropy(..., ignore_index=self.ignore_value)`；L122 打包成单键 dict `{"loss_sem_seg": loss * self.loss_weight}`——损失计算与加权都在 head 内完成，**不经过** weight_dict 过滤（对比 u5-l3）。
- [per_pixel_baseline.py:L20-L45](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/meta_arch/per_pixel_baseline.py#L20-L45)：`_version = 2` 的旧权重迁移钩子，逻辑与 `MaskFormerHead` 的同构（u3-l2 讲过）：把不带 `pixel_decoder.` 前缀的 `sem_seg_head` 键挪进去、`predictor` 键除外。区别是这里**没有** `static_query → query_feat` 的改名（那是 v2 解码器专属，u10-l4 承接）。

**与 MaskFormerHead 的契约对照**（本讲最值得记住的表）：

| 维度 | MaskFormerHead（第 2 级） | PerPixelBaselineHead（第 0 级） |
| --- | --- | --- |
| `forward` 返回 | 预测 dict（`pred_logits`/`pred_masks`/`aux_outputs`） | `(None, losses)` / `(x, {})` 二元组 |
| `forward` 第二参数 | `mask=None`（padding 掩码） | `targets=None`（逐像素 GT） |
| 损失归属 | meta arch 的 `SetCriterion`（匹配 + BCE + Dice） | head 内置全分辨率交叉熵 |
| 监督粒度 | per-mask（集合级，需匈牙利匹配） | per-pixel（像素独立） |
| 上采样责任 | meta arch 推理分支 | head 自己（`forward` 推理分支） |
| 像素解码器 | `build_pixel_decoder`（同工厂） | `build_pixel_decoder`（同工厂） |

#### 4.2.4 代码实践

**实践目标**：构建 `PerPixelBaselineHead`，验证 eval 输出形状 \((B, K, H, W)\)、train 损失键只有 `loss_sem_seg`，并数出 predictor 参数量。

**操作步骤**（示例代码，保存为仓库根目录的 `check_baseline_head.py`，运行 `python check_baseline_head.py`；需要 u1-l2 搭好的 detectron2 环境；为避免 CUDA 算子依赖，像素解码器用 `BasePixelDecoder`）：

```python
# 示例代码
import torch
from detectron2.config import get_cfg
from detectron2.layers import ShapeSpec
from detectron2.modeling import build_sem_seg_head
from detectron2.projects.deeplab import add_deeplab_config

import mask2former  # import 即注册（modeling/__init__.py L6）
from mask2former import add_maskformer2_config

cfg = get_cfg()
add_deeplab_config(cfg)
add_maskformer2_config(cfg)
cfg.MODEL.SEM_SEG_HEAD.NAME = "PerPixelBaselineHead"
cfg.MODEL.SEM_SEG_HEAD.NUM_CLASSES = 150          # ADE20K 语义分割
cfg.MODEL.SEM_SEG_HEAD.IGNORE_VALUE = 255
cfg.MODEL.SEM_SEG_HEAD.PIXEL_DECODER_NAME = "BasePixelDecoder"  # 免 CUDA 编译即可构建

input_shape = {  # 模拟 R50 金字塔
    "res2": ShapeSpec(channels=256, stride=4),
    "res3": ShapeSpec(channels=512, stride=8),
    "res4": ShapeSpec(channels=1024, stride=16),
    "res5": ShapeSpec(channels=2048, stride=32),
}
head = build_sem_seg_head(cfg, input_shape)

B, H, W = 2, 512, 512
features = {k: torch.randn(B, c, H // s, W // s)
            for k, c, s in [("res2", 256, 4), ("res3", 512, 8),
                            ("res4", 1024, 16), ("res5", 2048, 32)]}

head.eval()
with torch.no_grad():
    out, losses = head(features)
print("eval:", out.shape, losses)          # 期望 (2, 150, 512, 512), {}

head.train()
targets = torch.randint(0, 150, (B, H, W))
targets[:, :64] = 255                       # 放一些 ignore 像素
_, losses = head(features, targets)
print("train:", {k: round(float(v), 4) for k, v in losses.items()})

for name, mod in [("pixel_decoder", head.pixel_decoder), ("predictor", head.predictor)]:
    n = sum(p.numel() for p in mod.parameters())
    print(f"{name}: {n / 1e6:.3f}M params")
```

**需要观察的现象**：eval 输出是 4 倍于 `mask_features` 的 \((2, 150, 512, 512)\)（1/4 的 128×128 ×4 = 512）；train 损失 dict 只有一个键 `loss_sem_seg`；参数量上 predictor 约 0.039M（\(150 \times 256 + 150\)），远小于 pixel_decoder（约 3.3M，以本地输出为准）。

**预期结果**：输出形状与损失键如上；若 `NUM_CLASSES` 改成 133，predictor 参数相应变为约 34K。本讲义生成环境未安装 detectron2，以上运行结果**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么说 1×1 卷积「等价于逐像素线性分类器」？

**参考答案**：1×1 卷积在每个空间位置上做的就是把该位置的 \(C_{mask}\) 维向量与核矩阵 \((K, C_{mask})\) 相乘加偏置，即 \(\hat{y}_c(u) = w_c^{\top}\phi(u) + b_c\)。空间上各像素共享同一组 \(\{w_c, b_c\}\)，且没有跨像素交互——这正是 per-pixel classification 的定义。

**练习 2**：`losses` 里为什么先 ×4 上采样再算交叉熵？这带来什么代价？

**参考答案**：GT `targets` 是 padding 后全分辨率的逐像素类别图 \((B, H, W)\)，预测在 1/4 分辨率，二者必须对齐；代码选择上采样预测（L116-L118）而不是下采样 GT。代价是交叉熵在 \(B \times K \times H \times W\) 的 logits 上计算，显存与计算都按全分辨率付出——对照 u5-l2：mask classification 范式可以只在采样点上算损失（12544 个点），这正是范式带来的训练效率红利之一。

**练习 3**：推理分支 head 为什么「自己」做 ×4 上采样，而 `MaskFormerHead` 不做？

**参考答案**：两者配套的 meta arch 责任划分不同。per-pixel 基线配 detectron2 的 `SemanticSegmentor`（见 4.4），它只做「裁掉 padding + resize 到原图」的 `sem_seg_postprocess`，不做 stride 补偿，所以 head 必须自己 ×4；`MaskFormerHead` 配本仓库 `MaskFormer`，meta arch 的推理分支统一做 mask 上采样（[maskformer_model.py:L222-L227](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L222-L227)）并交给三个推理分支。

### 4.3 PerPixelBaselinePlusHead：动态分类核与 mask_classification=False

#### 4.3.1 概念说明

`PerPixelBaselinePlusHead`（第 1 级）继承 `PerPixelBaselineHead`，只做一件事：**把静态 1×1 卷积预测器删掉，换成 `StandardTransformerDecoder(cfg, in_channels, mask_classification=False)`**。

它解决的对照问题是：MaskFormer 相比朴素基线多了一个 Transformer 解码器，批评者可以说「提升来自解码器而不是范式」。Plus 基线把同一个 v1 Transformer 解码器加回来，但**分类方式仍是逐像素**——于是「Plus 基线 → MaskFormer」的差值就只剩范式本身。

关键机制是「动态分类核」。v1 解码器维护 \(Q\) 个 query（`query_embed` 位置嵌入 + 从全零 tgt 出发），经自注意力和对图像特征的交叉注意力后，每个 query 输出一个嵌入 \(t_q\)，再经三层 MLP（`mask_embed`）得到 \(e_q \in \mathbb{R}^{C_{mask}}\)，最后与 `mask_features` 逐像素点积：

\[
\hat{y}_q(u) = e_q^{\top} \phi(u), \qquad e_q = \mathrm{MLP}(t_q)
\]

与 4.2 的静态核相比，形式仍是「每通道一个向量 · 像素嵌入」，但 \(e_q\) **随输入图像变化**（交叉注意力读的是当前图像的特征）——等价于一个由 Transformer 动态生成的 1×1 卷积核。若把 query 数设为类别数 \(Q = K\)，输出 \((B, Q, H/4, W/4)\) 就可直接当逐像素类别 logits 用。

`mask_classification=False` 的全部变化（相对 v2 的 `mask_classification=True` 用法）：

- 解码器**不创建** `class_embed`（没有 per-query 分类头，也就没有 no-object 通道）；
- 输出 dict **没有** `pred_logits`，只有 `pred_masks`；
- `aux_outputs` 的每组只剩 `pred_masks`；
- 整个 query 集合不需要一对一匹配——query 下标直接就是类别下标（\(Q=K\) 约束）。

顺带澄清规格里的说法：「Plus 版把 1×1 conv 换成两层卷积」与源码不符——源码换上的是完整 Transformer 解码器 + 三层 `mask_embed` MLP（[maskformer_transformer_decoder.py:L105](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/maskformer_transformer_decoder.py#L105)）。「给逐像素分类器加容量」的思路一致，但实现的容量远大于两层卷积。

#### 4.3.2 核心流程

```text
构造（Plus 版）
  1. super().__init__(...)            # 先按基线构造：pixel_decoder + 1×1 conv predictor
  2. del self.predictor                # 删掉 1×1 conv（从 _modules 移除，参数不残留）
  3. self.predictor = StandardTransformerDecoder(cfg, in_channels, mask_classification=False)
  4. 记录 transformer_in_feature / deep_supervision

layers（Plus 版）
  1. mask_features, transformer_encoder_features, _ = pixel_decoder.forward_features(features)
  2. transformer_in_feature == "transformer_encoder"?
     ├─ 是：x = transformer_encoder_features（断言非 None，需配 TransformerEncoderPixelDecoder）
     └─ 否（默认 "res5"）：x = features["res5"]     # 原始 backbone 特征！不经像素解码器
  3. predictions = predictor(x, mask_features)      # x 是解码器输入，mask_features 用于点积

StandardTransformerDecoder(mask_classification=False) 内部
  1. pos = 正弦位置编码(x)；src = input_proj(x)（1×1，通道 != hidden_dim 时创建）
  2. tgt = 全零 (Q, B, C)，query_embed 只作位置嵌入
  3. 6 层解码：self-attn → cross-attn(读 src) → FFN（DETR 顺序）
  4. mask_embed = MLP(hidden→hidden→mask_dim, 3 层)
  5. deep_supervision: einsum("lbqc,bchw->lbqhw") 一次性算出全部层的 mask
  6. 返回 {"pred_masks": 最后一层, "aux_outputs": [前 L-1 层]}   # 无 pred_logits

forward（Plus 版）
  ├─ 训练 + 深监督：losses["loss_sem_seg"] + 每层 aux 一个 "loss_sem_seg_i"
  └─ 推理：pred_masks ×4 上采样 → (B, Q=K, H, W)
```

注意两处与 v2 解码器（u4-l3）的有趣对照：v1 的 `tgt` 从**全零**出发、`query_embed` 只当位置嵌入（没有 v2 的可学习内容嵌入 `query_feat`）；v1 层数**不减一**——`DEC_LAYERS=6` 就建 6 层、产 6 组监督（v2 因 query_feat 先产一组预测而建 `DEC_LAYERS−1` 层）。

#### 4.3.3 源码精读

- [per_pixel_baseline.py:L180-L192](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/meta_arch/per_pixel_baseline.py#L180-L192)：Plus 版构造的关键三连。L180-L186 先完整走基线构造（会创建 1×1 conv 并初始化）；L188 `del self.predictor` 借助 `nn.Module.__setattr__/__delattr__` 的注册表机制把 1×1 conv 从 `_modules` 里移除，参数不残留；L190 换上 Transformer 预测器；L191-L192 记录 `transformer_in_feature` 与 `deep_supervision`。
- [per_pixel_baseline.py:L194-L206](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/meta_arch/per_pixel_baseline.py#L194-L206)：Plus 版 `from_config`。L196 复用父类（像素解码器等受控变量照旧）；L197-L201 推导解码器输入通道——`transformer_encoder` 取 `CONVS_DIM`，否则取 `input_shape[feature].channels`（默认 `"res5"` 时 R50 为 2048，触发解码器内部 `input_proj` 的创建）；L202-L204 **直接实例化** `StandardTransformerDecoder(cfg, in_channels, mask_classification=False)`——不走 `build_transformer_decoder` 注册表（对比 [mask_former_head.py:L108-L112](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/meta_arch/mask_former_head.py#L108-L112) 硬编码 `mask_classification=True`，见 u8-l3 的讨论）。
- [per_pixel_baseline.py:L231-L243](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/meta_arch/per_pixel_baseline.py#L231-L243)：Plus 版 `layers`。L232 解包三元组；L233-L239 二分派解码器输入：`transformer_encoder` 用 encoder 特征（带断言），**默认路径用原始 backbone 特征 `features["res5"]`**——像素解码器在这条路径上只贡献 `mask_features`；L240-L243 按深监督开关返回 `(pred_masks, aux_outputs)` 或 `(pred_masks, None)`。
- [per_pixel_baseline.py:L208-L229](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/meta_arch/per_pixel_baseline.py#L208-L229)：Plus 版 `forward`。L216-L222 深监督分支：主损失 `loss_sem_seg` 之外，每组 aux 追加键 `loss_sem_seg_{i}`（`i` 从 0 到层数−2）——**键名直接编码层数信息，且不做 weight_dict 过滤**，detectron2 训练器会把 dict 里所有损失直接求和（对照 u5-l3：MaskFormer meta arch 靠 weight_dict 白名单过滤与加权）。L226-L228 推理分支与基线一致地 ×4 上采样。
- [maskformer_transformer_decoder.py:L103-L105](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/maskformer_transformer_decoder.py#L103-L105)：`mask_classification` 的开关作用在参数上——`class_embed`（输出 \(K+1\) 维，含 no-object 通道）**只在 True 时创建**。False 时该头整个不存在，这是「无分类头」的实体证据。
- [maskformer_transformer_decoder.py:L93-L99](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/maskformer_transformer_decoder.py#L93-L99)：`query_embed` 可学习位置嵌入（\(Q \times C\)）；`input_proj` 仅在输入通道 ≠ hidden_dim 或 `enforce_input_project` 时创建——Plus 默认路径（res5 2048 维）必然创建。
- [maskformer_transformer_decoder.py:L130-L158](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/maskformer_transformer_decoder.py#L130-L158)：解码器 `forward`。L133-L136 位置编码、input_proj、进 Transformer；L138-L142 是 `mask_classification` 的第二处开关——True 时输出 `pred_logits`，**False 时 `out = {}`**，后面只填 mask 相关键；L144-L151 深监督分支用 `einsum("lbqc,bchw->lbqhw")` 一次性算出全部 \(L\) 层的 mask（`l` 是层维），取最后一层作主输出。
- [maskformer_transformer_decoder.py:L160-L171](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/maskformer_transformer_decoder.py#L160-L171)：`_set_aux_loss` 的 False 分支——aux 组只有 `{"pred_masks": b}`，没有 `pred_logits`（True 分支两组键都有）。
- [transformer.py:L61-L75](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/transformer.py#L61-L75)：v1 Transformer 的调用形态。L62-L65 把 \((B,C,H,W)\) 特征展平为 \((HW,B,C)\) 序列（u4-l2 的序列格式）；L70 `tgt = torch.zeros_like(query_embed)`——**query 内容从全零出发**，`query_embed` 只以位置嵌入身份参与（L72-L73）；L75 返回 `(hs, memory)`，`hs` 形状 \((L, B, Q, C)\) 供上面的层维 einsum 使用。解码层内部顺序为 self-attn → cross-attn → FFN（[transformer.py:L272-L289](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/transformer.py#L272-L289)），与 v2 的 cross→self→FFN 相反（u4-l3）。
- 配置默认值：`TRANSFORMER_IN_FEATURE = "res5"`（[config.py:L50](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/config.py#L50)）、`DEEP_SUPERVISION = True`（[config.py:L33](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/config.py#L33)）、`DEC_LAYERS = 6`（[config.py:L44](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/config.py#L44)）、`NUM_OBJECT_QUERIES = 100`（[config.py:L48](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/config.py#L48)）——**跑 Plus 基线时必须把 `NUM_OBJECT_QUERIES` 改成 `NUM_CLASSES`**，理由见练习 1。

#### 4.3.4 代码实践

**实践目标**：构建 `PerPixelBaselinePlusHead`（`NUM_OBJECT_QUERIES = NUM_CLASSES = 150`），验证：eval 输出 \((B, 150, H, W)\)；train 深监督损失键为 `loss_sem_seg` + `loss_sem_seg_0..4` 共 6 个；predictor 参数量比基线的 1×1 conv 大两个数量级。

**操作步骤**（示例代码，接在 4.2.4 的脚本后面，或另存为 `check_baseline_plus_head.py`）：

```python
# 示例代码（前半部分 cfg / input_shape / features 的构造与 4.2.4 相同）
cfg.MODEL.SEM_SEG_HEAD.NAME = "PerPixelBaselinePlusHead"
cfg.MODEL.SEM_SEG_HEAD.NUM_CLASSES = 150
cfg.MODEL.SEM_SEG_HEAD.IGNORE_VALUE = 255
cfg.MODEL.SEM_SEG_HEAD.PIXEL_DECODER_NAME = "BasePixelDecoder"
cfg.MODEL.MASK_FORMER.NUM_OBJECT_QUERIES = 150   # 必须等于 NUM_CLASSES（见练习 1）
cfg.MODEL.MASK_FORMER.TRANSFORMER_IN_FEATURE = "res5"  # 默认值，显式写出便于观察
cfg.MODEL.MASK_FORMER.DEC_LAYERS = 6             # 6 层 → 6 组监督
cfg.MODEL.MASK_FORMER.DEEP_SUPERVISION = True

head = build_sem_seg_head(cfg, input_shape)

head.eval()
with torch.no_grad():
    out, losses = head(features)
print("eval:", out.shape)                        # 期望 (2, 150, 512, 512)

head.train()
_, losses = head(features, targets)              # targets 同 4.2.4
print("train:", sorted(losses.keys()))           # 期望 6 个 loss_sem_seg* 键

n_total = sum(p.numel() for p in head.parameters())
n_pred = sum(p.numel() for p in head.predictor.parameters())
print(f"total {n_total/1e6:.2f}M, predictor {n_pred/1e6:.2f}M")
for n, p in head.predictor.named_parameters():   # 看 input_proj / query_embed / MLP 都在
    print(f"  {n}: {tuple(p.shape)}")
```

**需要观察的现象**：eval 输出与基线同形状（通道数 = 150 = Q）；训练损失键为 `['loss_sem_seg', 'loss_sem_seg_0', ..., 'loss_sem_seg_4']`；`predictor` 参数列表里能看到 `input_proj.weight`（\(256 \times 2048\)，因 res5 输入）、`query_embed.weight`（\(150 \times 256\)）、`mask_embed.layers.*`（三层 MLP）与 6 层解码器参数。

**预期结果**：predictor 参数量约 10M 量级（input_proj 约 0.52M + 6 层解码器约 9.5M + MLP 约 0.20M + query_embed 0.038M，估算值），远大于基线的 0.039M——「解码器容量」这个变量在 Plus 版被显式补上了。若把 `NUM_OBJECT_QUERIES` 留成默认 100，训练分支会在 `F.cross_entropy` 处报通道数不匹配。运行结果**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：Plus 版为什么必须设 `NUM_OBJECT_QUERIES = NUM_CLASSES`？指出依据的两处代码。

**参考答案**：Plus 版输出通道数是 Q 而非 K：`pred_masks` 由 `einsum("bqc,bchw->bqhw")` 得 \((B, Q, H/4, W/4)\)（[maskformer_transformer_decoder.py:L156](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/maskformer_transformer_decoder.py#L156)），而 head 的损失把它当 \((N, C, H, W)\) 类别 logits 喂给 `F.cross_entropy`（[per_pixel_baseline.py:L119-L121](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/meta_arch/per_pixel_baseline.py#L119-L121)，期望 \(C=K\) 才能和 \((N,H,W)\) 的 GT 对齐）。Q ≠ K 时维度不匹配直接报错——因为 query 下标被当作类别下标使用，没有匹配器兜底。

**练习 2**：v1 深监督的监督组数与 v2 有何不同？为什么？

**参考答案**：v1（Plus 版）`DEC_LAYERS=6` 建 6 层、返回 6 组监督（主输出 + 5 组 aux，[transformer.py:L128-L149](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/transformer.py#L128-L149) 每层收集一次中间输出）；v2 建 `DEC_LAYERS−1` 层但同样产 `DEC_LAYERS` 组，因为 v2 的可学习 `query_feat` 在进第一层之前就先产一组受监督预测（u4-l3、u5-l3）。v1 的 query 从全零出发、没有这个「第 0 组」。

**练习 3**：`del self.predictor` + 重新赋值这三行为什么能干净地换掉模块、不留下 1×1 conv 的参数？

**参考答案**：`nn.Module` 的属性赋值/删除被 `__setattr__`/`__delattr__` 拦截，`self.predictor = nn.Module(...)` 会登记进 `_modules` 字典；`del self.predictor` 把对应条目从 `_modules` 删除；再次赋值则登记新模块。因此 `parameters()` 只会遍历到新的 Transformer 预测器，旧 1×1 conv 的参数（以及它的初始化开销）不参与前向与优化。

### 4.4 接线：基线头为什么不能配 MaskFormer meta arch

#### 4.4.1 概念说明

4.2 已指出：per-pixel 基线头的 `forward` 返回 `(None, losses)` / `(x, {})` **二元组**，而本仓库的 `MaskFormer` meta arch 期望 head 返回**预测 dict**。这不是风格差异，而是两套 head 契约：

- **dict 契约**（`MaskFormerHead`）：head 只算预测，损失归 meta arch 的 `SetCriterion`（先匹配后监督，u5-l1/u5-l2），推理归 meta arch 的三个分支（u6）。
- **二元组契约**（per-pixel 基线头）：head 自己算损失（内置交叉熵），meta arch 只负责「归一化、padding、backbone、把 GT 递给 head、把输出后处理」——这正是 detectron2 自带 `SemanticSegmentor`（语义分割通用 meta arch，注册名 `"SemanticSegmentor"`）的工作方式。它以 `results, losses = self.sem_seg_head(features, targets)` 消费 head，targets 来自 dataset dict 的 `sem_seg` 键（逐像素类别图），推理则做 `sem_seg_postprocess`（裁 padding + resize 到原图）。

因此跑通 per-pixel 基线的完整模型需要三个动作：meta arch 换成 `SemanticSegmentor`、head 名换成基线头、数据 mapper 用产出 `sem_seg` 键的语义 mapper。

#### 4.4.2 核心流程

先看「错误接线」的两种失败模式（都是可预期的运行时报错）：

```text
MODEL.META_ARCHITECTURE: "MaskFormer" + SEM_SEG_HEAD.NAME: "PerPixelBaselineHead"

训练分支：
  outputs = sem_seg_head(features)        # → (None, losses_dict) 元组
  losses = self.criterion(outputs, ...)   # SetCriterion 先对 outputs 调 .items()
                                          # → AttributeError: 'tuple' object has no attribute 'items'

推理分支：
  mask_cls_results = outputs["pred_logits"]   # 对 tuple 用字符串下标
                                               # → TypeError: tuple indices must be integers
```

正确接线的配置组合：

```text
MODEL:
  META_ARCHITECTURE: "SemanticSegmentor"        # detectron2 提供，无需本仓库代码
  SEM_SEG_HEAD:
    NAME: "PerPixelBaselineHead"                # 或 "PerPixelBaselinePlusHead"
INPUT:
  DATASET_MAPPER_NAME: "mask_former_semantic"   # 产出 "sem_seg" 逐像素 GT 键
```

数据侧闭环：`MaskFormerSemanticDatasetMapper` 同时产出两套 GT——`sem_seg`（逐像素类别图，给 per-pixel 基线）与 `instances`（每类一张 mask，给 mask classification）——所以同一份数据管线可以喂两种范式，对照实验连数据侧都受控。

#### 4.4.3 源码精读

- [maskformer_model.py:L197-L209](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L197-L209)：失败模式一的现场。L198 `outputs = self.sem_seg_head(features)` 拿到元组；L209 `losses = self.criterion(outputs, targets)` 把元组传给 `SetCriterion`——其 `forward` 第一步就要对 `outputs` 调 `.items()`（u5-l2 精读过），元组没有该方法。
- [maskformer_model.py:L218-L220](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L218-L220)：失败模式二的现场——推理分支第一行 `outputs["pred_logits"]` 对元组用字符串索引，直接 `TypeError`。
- [per_pixel_baseline.py:L94-L107](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/meta_arch/per_pixel_baseline.py#L94-L107)：二元组契约的定义处——`forward(self, features, targets=None)` 的签名（第二参数是 **targets**，注意与 `MaskFormerHead.forward(features, mask=None)` 的 mask 含义不同）与两个分支的返回形态。
- [mask_former_semantic_dataset_mapper.py:L112-L123](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/dataset_mappers/mask_former_semantic_dataset_mapper.py#L112-L123)：读取 `sem_seg_file_name` 为逐像素 GT（uint16 读图转 double，随增强一起几何变换）。
- [mask_former_semantic_dataset_mapper.py:L152-L155](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/dataset_mappers/mask_former_semantic_dataset_mapper.py#L152-L155)：`dataset_dict["sem_seg"] = sem_seg_gt.long()`——per-pixel 基线训练 GT 的直接来源（`SemanticSegmentor` 从这个键取 targets 并 pad 到统一尺寸）。
- [mask_former_semantic_dataset_mapper.py:L160-L182](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/dataset_mappers/mask_former_semantic_dataset_mapper.py#L160-L182)：同一 mapper 同时把逐像素图重组为「每类一张 mask」的 `instances`（u2-l3 讲过）——一份 mapper 服务两种范式的证据。
- [configs/ade20k/semantic-segmentation/Base-ADE20K-SemanticSegmentation.yaml:L50](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/ade20k/semantic-segmentation/Base-ADE20K-SemanticSegmentation.yaml#L50)：`DATASET_MAPPER_NAME: "mask_former_semantic"`——语义任务的 mapper 选择就在这一行。
- 关于 `SemanticSegmentor` 的说明：它是 detectron2 自带的 meta arch（`detectron2.modeling.meta_arch.semantic_seg`，注册名 `"SemanticSegmentor"`），其「head 二元组契约 + `sem_seg` GT」的接口与上述基线头逐一吻合。本讲义生成环境未安装 detectron2，无法对 detectron2 源码行号做永久链接，端到端训练验证**待本地确认**（detectron2 版本差异可能影响细节）。

#### 4.4.4 代码实践

**实践目标**：写出一份可训练的 per-pixel 基线配置，并用 `--eval-only` 前的 dry-run 验证模型能被构建出来。

**操作步骤**（示例代码）：

1. 在仓库根目录新建 `configs/ade20k/semantic-segmentation/mini_per_pixel_baseline.yaml`（**注意：本讲义禁止修改源码，这个文件属于你自己的实验配置，建议放在仓库外的实验目录，或仅作练习不入库**）：

```yaml
# 示例代码：受控的 per-pixel 基线配置（承接 Base-ADE20K-SemanticSegmentation.yaml）
_BASE_: "Base-ADE20K-SemanticSegmentation.yaml"
MODEL:
  META_ARCHITECTURE: "SemanticSegmentor"   # 换 meta arch：二元组契约
  SEM_SEG_HEAD:
    NAME: "PerPixelBaselineHead"           # 换 head：静态 1×1 分类器
    NUM_CLASSES: 150
    IGNORE_VALUE: 255
    PIXEL_DECODER_NAME: "BasePixelDecoder" # 与对照的 MaskFormer 配置保持一致
  MASK_FORMER:
    TRANSFORMER_IN_FEATURE: "res5"         # 仅 Plus 版消费
```

2. dry-run 构建检查（示例代码）：

```python
# 示例代码
import torch
from detectron2.config import get_cfg
from detectron2.projects.deeplab import add_deeplab_config
from detectron2.modeling import build_model

import mask2former
from mask2former import add_maskformer2_config

cfg = get_cfg()
add_deeplab_config(cfg)
add_maskformer2_config(cfg)
cfg.merge_from_file("configs/ade20k/semantic-segmentation/mini_per_pixel_baseline.yaml")
cfg.MODEL.DEVICE = "cpu"
model = build_model(cfg)          # 能构建即说明注册表接线正确
print(type(model).__name__, type(model.sem_seg_head).__name__)
```

3. 进阶：把 `NAME` 换成 `PerPixelBaselinePlusHead` 并加 `MASK_FORMER.NUM_OBJECT_QUERIES: 150`，重复构建。

**需要观察的现象**：dry-run 打印的 meta arch 是 `SemanticSegmentor`、head 是对应基线类；换成 Plus 后能再跑通。

**预期结果**：两个头都能构建。完整训练（ADE20K 160k 迭代）需要 u1-l2 环境加数据集（u2-l2），本讲义环境不可运行，**待本地验证**。特别提示：不要把 `META_ARCHITECTURE` 留成 `MaskFormer`——按 4.4.2 的两种失败模式，前向必崩。

#### 4.4.5 小练习与答案

**练习 1**：把 `PerPixelBaselineHead` 配到 `MaskFormer` meta arch 下，训练与推理分别在哪里崩、为什么？

**参考答案**：训练在 [maskformer_model.py:L209](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L209)——criterion 收到元组，`SetCriterion.forward` 调 `.items()` 抛 `AttributeError`；推理在 [maskformer_model.py:L219](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L219)——对元组取 `outputs["pred_logits"]` 抛 `TypeError`。根因是 head 契约不匹配（二元组 vs dict）。

**练习 2**：per-pixel 基线的训练 GT 来自哪个键？由哪个 mapper 的哪几行产出？

**参考答案**：`dataset_dict["sem_seg"]`（逐像素 long 类别图），由 `MaskFormerSemanticDatasetMapper` 产出：[mask_former_semantic_dataset_mapper.py:L152-L155](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/dataset_mappers/mask_former_semantic_dataset_mapper.py#L152-L155)。该 mapper 同时还产出 `instances`（每类一张 mask）供 mask classification 使用，因此两种范式可共用同一数据管线。

**练习 3**：语义分割评估器（`SemSegEvaluator`，u7-l1 提过）评估基线输出时，为什么不需要任何「no-object 通道」的特殊处理？

**参考答案**：基线输出通道就是 \(K\) 个类别的逐像素 logits（head 的 1×1 conv 输出 \(K\) 通道；Plus 版因 \(Q=K\) 同样如此），对通道做 softmax/argmax 即得类别图，与检测式输出「\(K+1\)（含 no-object）× Q 个 query」的结构完全不同——no-object 通道是 mask classification 范式为「未匹配 query」准备的（u5-l2），per-pixel 范式里每个像素必然属于（或忽略于）某个类别，不存在「无对象」状态。

## 5. 综合实践

把本讲所有内容串成一个「对照实验小实验台」：一个脚本同时构建两个基线头（外加可选的 `MaskFormerHead` 作第 2 级），在**完全相同**的假输入上跑前向，产出一张三列对比表，并回答本讲的核心问题。

**任务**（示例代码，保存为 `compare_pixel_baselines.py`，需 u1-l2 环境）：

```python
# 示例代码
import torch
from detectron2.config import get_cfg
from detectron2.layers import ShapeSpec
from detectron2.modeling import build_sem_seg_head
from detectron2.projects.deeplab import add_deeplab_config

import mask2former
from mask2former import add_maskformer2_config

K = 150
input_shape = {"res2": ShapeSpec(channels=256, stride=4), "res3": ShapeSpec(channels=512, stride=8),
               "res4": ShapeSpec(channels=1024, stride=16), "res5": ShapeSpec(channels=2048, stride=32)}
B, H, W = 2, 512, 512
torch.manual_seed(0)
features = {k: torch.randn(B, c, H // s, W // s)
            for k, c, s in [("res2", 256, 4), ("res3", 512, 8), ("res4", 1024, 16), ("res5", 2048, 32)]}
targets = torch.randint(0, K, (B, H, W)); targets[:, :64] = 255


def make_cfg(head_name: str) -> "get_cfg 返回值":
    cfg = get_cfg()
    add_deeplab_config(cfg)
    add_maskformer2_config(cfg)
    cfg.MODEL.SEM_SEG_HEAD.NAME = head_name
    cfg.MODEL.SEM_SEG_HEAD.NUM_CLASSES = K
    cfg.MODEL.SEM_SEG_HEAD.IGNORE_VALUE = 255
    cfg.MODEL.SEM_SEG_HEAD.PIXEL_DECODER_NAME = "BasePixelDecoder"   # 受控：三者相同
    cfg.MODEL.MASK_FORMER.NUM_OBJECT_QUERIES = K                     # Plus 版需要 Q=K
    cfg.MODEL.MASK_FORMER.TRANSFORMER_IN_FEATURE = "res5"
    cfg.MODEL.MASK_FORMER.DEC_LAYERS = 6
    return cfg


rows = []
for head_name in ["PerPixelBaselineHead", "PerPixelBaselinePlusHead"]:
    head = build_sem_seg_head(make_cfg(head_name), input_shape)
    n_pred = sum(p.numel() for p in head.predictor.parameters())
    n_pd = sum(p.numel() for p in head.pixel_decoder.parameters())

    head.eval()
    with torch.no_grad():
        out, _ = head(features)
    eval_shape = tuple(out.shape)

    head.train()
    _, losses = head(features, targets)
    rows.append((head_name, eval_shape, sorted(losses.keys()), f"{n_pred/1e6:.3f}M", f"{n_pd/1e6:.3f}M"))

print(f"{'head':32s} {'eval shape':22s} {'loss keys':40s} {'pred':>8s} {'pixel_dec':>9s}")
for r in rows:
    print(f"{r[0]:32s} {str(r[1]):22s} {str(r[2]):40s} {r[3]:>8s} {r[4]:>9s}")
```

**验收要点**：

1. 两行的 `pixel_dec` 列几乎相同（同工厂同配置，浮点级差异仅来自初始化随机性不影响计数）——受控证据。
2. `pred` 列：基线约 0.039M，Plus 约 10M 量级——解码器容量差异被显式补齐。
3. `eval shape` 两行都是 `(2, 150, 512, 512)`。
4. `loss keys`：基线 1 个键；Plus 6 个键（深监督）。
5. 完成后写 150–300 字的对比说明，回答：**为什么 Mask2Former 论文需要这两个 per-pixel 基线来隔离 mask classification 范式的收益？**要求覆盖：两级阶梯各隔离了什么变量、`build_pixel_decoder` 受控的代码证据、`mask_classification=False` 在解码器里的具体含义（无 `class_embed`、无 `pred_logits`、aux 仅 `pred_masks`）、\(Q=K\) 约束的由来。

本讲义生成环境未安装 detectron2，脚本输出**待本地验证**；若 `NUM_OBJECT_QUERIES` 忘记设为 150，Plus 行会在交叉熵处报维度错误——这本身就是练习 1 结论的实验证明。

## 6. 本讲小结

- 两个基线头构成「控制变量阶梯」：`PerPixelBaselineHead`（同 backbone + 同像素解码器 + 静态 1×1 逐像素分类器）→ `PerPixelBaselinePlusHead`（再把 v1 Transformer 解码器加回来，分类仍是逐像素）→ `MaskFormerHead`（最后才换范式）；「Plus → MaskFormer」的差值即纯范式收益。
- `PerPixelBaselineHead.layers` 只有两行：取 `forward_features` 三元组的 `mask_features`（丢弃多尺度项），过 1×1 卷积得 \((B,K,H/4,W/4)\) logits；损失是 head 内置的全分辨率交叉熵（先 ×4 上采样），训练返回 `(None, losses)` 二元组。
- `PerPixelBaselinePlusHead` 用 `del self.predictor` + 换装 `StandardTransformerDecoder(mask_classification=False)`：分类核从静态参数变成「query → MLP → 与 mask_features 点积」的动态核，且必须 \(Q=K\)；深监督产生 `loss_sem_seg` + `loss_sem_seg_0..L-2` 键，不经 weight_dict 过滤。
- `mask_classification=False` 在解码器里的三处落点：不建 `class_embed`（无 no-object 通道）、输出无 `pred_logits`、`aux_outputs` 每组只剩 `pred_masks`；v1 解码器 query 从全零 tgt 出发、层数不减一，均与 v2 相反。
- 基线头返回二元组，与本仓库 `MaskFormer` meta arch 的 dict 契约不兼容（训练在 criterion 崩、推理在 `outputs["pred_logits"]` 崩）；正确接线是 detectron2 的 `SemanticSegmentor` + `sem_seg` GT（由 `mask_former_semantic` mapper 产出），仓库未附带现成基线 yaml。
- 损失侧对照：per-pixel 范式付全分辨率交叉熵的显存代价，mask classification 范式换来采样点损失（u5-l2）与实例级输出能力（u6）——范式差异不只是精度，还有训练开销与任务覆盖面。

## 7. 下一步学习建议

本讲收束了单元八「扩展点与二次开发」。后续建议：

1. **u10-l1（模型分析）**：用 `tools/analyze_model.py` 给两个基线头与 `MaskFormerHead` 的完整模型算 FLOPs/参数表，把「解码器容量」从参数量升级为计算量证据；注意可变形注意力算子在 FLOPs 表中的呈现方式。
2. **u10-l4（权重兼容）**：本讲的 `_version = 2` 迁移钩子（L20-L45）与 `MaskFormerHead` 的同构，细节在 u10-l4 统一展开；跑基线复现实验加载旧 checkpoint 时会用到。
3. **回看 u5-l2 与 u6**：把「全分辨率交叉熵 vs 采样点损失」「逐像素 softmax vs query×class 融合」放在一起，就凑齐了两种范式在损失与推理两端的完整对照。
4. **论文复现向**：对照 MaskFormer/Mask2Former 论文的消融表（ADE20K mIoU 的 per-pixel baseline / baseline+ / 主模型三行）阅读本讲源码，确认论文里的每个对照都在仓库里有对应实现；如需真实训练，按 4.4 的配置 + u2-l2 的数据准备 + `train_net.py --num-gpus 1` 起步（注意 `SemanticSegmentor` 分支下 `Trainer.build_evaluator` 的选择逻辑，u7-l1）。
