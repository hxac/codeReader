# u3-l3 Swin Transformer Backbone

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `D2SwinTransformer` 如何把一份从 Swin-Transformer-Semantic-Segmentation 移植来的 Swin 实现包装成 detectron2 的 `Backbone`，满足 `output_shape()` 与 `size_divisibility` 两个接口契约。
2. 跟踪 `SwinTransformer.forward` 的完整链路：patch embedding → 四个 stage（`BasicLayer` + `PatchMerging`）→ 输出命名 `res2`–`res5`，并解释为什么通道数逐 stage 翻倍、分辨率逐 stage 减半。
3. 解释 window attention（W-MSA / SW-MSA）与 relative position bias 的实现，理解 `window_partition` / `window_reverse` 这对互逆操作和 shifted window 的 attention mask 兜底逻辑。
4. 会用 `tools/convert-pretrained-swin-model-to-d2.py` 把官方 Swin 分类权重转成 detectron2 可加载的 `.pkl`，并说清它的 key 对齐规则其实依赖 `matching_heuristics`，而不是脚本里显式改名。

## 2. 前置知识

承接 u3-l2：`MaskFormerHead` 是「backbone → pixel decoder → Transformer decoder」三段式的组装器，它通过 detectron2 的 `build_backbone(cfg)` 从 `BACKBONE_REGISTRY` 取出名字等于 `cfg.MODEL.BACKBONE.NAME` 的类来构建 backbone，并靠 backbone 的 `output_shape()` 拿到每个特征的通道数与 stride，去推导 pixel decoder / Transformer decoder 的输入通道。本讲就往三段式的第一段里看。

在阅读源码前，先建立几个直觉：

- **为什么需要层级式（hierarchical）backbone。** Mask2Former 的像素解码器要吃 **1/32、1/16、1/8（以及 1/4 的 mask_features）** 多个分辨率的特征金字塔。原始 ViT 全程只有一种分辨率，而 Swin 像 CNN 一样分四个 stage 逐级下采样，天然产出金字塔，正好与 R50 的 `res2`–`res5` 对齐。
- **为什么需要 window attention。** 全图 self-attention 的计算量与像素数平方成正比。若特征图有 \(N = H \times W\) 个 token，全图注意力的复杂度是 \(O(N^2)\)；Swin 把特征图切成 \(w \times w\)（默认 \(7\times7\)）的小窗，注意力只在窗内进行，复杂度降为 \(O(N \cdot w^2)\)，与 \(N\) 线性相关。
- **为什么还要 shift（滑动）窗口。** 固定窗口会让每个窗永远看不到别的窗。Swin 在相邻 block 间把特征图 cyclic shift 半个窗口（`window_size // 2`），让注意力跨窗流通。shift 会把一个完整窗口切成「来自图像不同区域的碎片」，因此需要一个 attention mask 阻止碎片之间互相注意。
- **relative position bias（相对位置偏置）。** 每个注意力头持有一张可学习表格，按「窗内两个 token 的相对位置（行差、列差）」查表得到加在注意力分数上的偏置 \(B\)。它是 Swin 位置信息的主要来源（本仓库默认 `APE=False`，不使用绝对位置编码）。
- **detectron2 `Backbone` 接口。** 一个合法 backbone 需要：`forward(x)` 返回 `dict[str, Tensor]`；`output_shape()` 返回 `dict[str, ShapeSpec]`（名字 → 通道数/stride）；提供 `size_divisibility`（输入尺寸必须是它的倍数，供 `ImageList` padding 使用）。u3-l2 已见过 `MaskFormer.forward` 里的 `ImageList.from_tensors(..., size_divisibility=self.backbone.size_divisibility)`，当时是 32；本讲会看到 Swin 版本同样是 32。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `mask2former/modeling/backbone/swin.py` | Swin backbone 全部实现：`PatchEmbed`、`WindowAttention`、`SwinTransformerBlock`、`PatchMerging`、`BasicLayer`、`SwinTransformer`，以及包装类 `D2SwinTransformer`。文件头注明移植自 Microsoft 的 Swin-Transformer-Semantic-Segmentation（mmseg 版本） |
| `mask2former/modeling/__init__.py` | 仅 5 行 import，其中 `from .backbone.swin import D2SwinTransformer` 是触发注册的入口（`backbone/__init__.py` 本身只有版权注释，是空文件） |
| `mask2former/config.py` | `add_maskformer2_config` 中注册全部 `cfg.MODEL.SWIN.*` 配置键 |
| `tools/convert-pretrained-swin-model-to-d2.py` | 把官方 Swin 分类 checkpoint 包装成 detectron2 可加载的 pickle |
| `configs/coco/panoptic-segmentation/swin/maskformer2_swin_tiny_bs16_50ep.yaml` | swin-tiny 训练配置：切换 `BACKBONE.NAME` 并覆盖 `MODEL.SWIN.*` |

## 4. 核心概念与源码讲解

### 4.1 D2SwinTransformer：把 Swin 包成 detectron2 Backbone

#### 4.1.1 概念说明

`swin.py` 里 500 多行是「纯 PyTorch 的 Swin」，对 detectron2 一无所知；只有最后 85 行的 `D2SwinTransformer` 负责「适配」。它同时继承 `SwinTransformer`（拿到全部计算能力）与 detectron2 的 `Backbone`（拿到接口契约），并用 `@BACKBONE_REGISTRY.register()` 把自己登记进 backbone 注册表。这是典型的**适配器模式**：算法实现与框架接口解耦，同一个 Swin 类理论上可以被别的框架复用。

#### 4.1.2 核心流程

`D2SwinTransformer` 的生命周期：

1. `import mask2former` 时，`mask2former/modeling/__init__.py` import `D2SwinTransformer`，装饰器执行，类名进入 `BACKBONE_REGISTRY`。
2. 构建 `MaskFormer` 时（u3-l1），`build_backbone(cfg)` 按名字 `"D2SwinTransformer"` 查表并调用 `D2SwinTransformer(cfg, input_shape)`。
3. `__init__` 逐项从 `cfg.MODEL.SWIN.*` 读参数，转交给父类 `SwinTransformer.__init__` 搭网络；随后补上 detectron2 需要的三样东西：`_out_features`、`_out_feature_strides`、`_out_feature_channels`。
4. 前向时 `forward` 调父类得到 `{"res2":…, "res3":…, "res4":…, "res5":…}`，再按 `_out_features` 过滤（默认四层全输出）。
5. 下游 `MaskFormerHead.from_config` 调 `output_shape()` 拿通道数与 stride。

#### 4.1.3 源码精读

注册与导入链：注册发生在类定义处——

[mask2former/modeling/backbone/swin.py:L686-L687](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/backbone/swin.py#L686-L687) 装饰器把 `D2SwinTransformer` 登记进 `BACKBONE_REGISTRY`，类同时继承 `SwinTransformer` 与 `Backbone`。

[mask2former/modeling/__init__.py:L2](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/__init__.py#L2) 这一行 import 是注册的触发点；注意 `mask2former/modeling/backbone/__init__.py` 只有一行版权注释，注册完全依赖本文件的 import 副作用。

构造函数读配置并转交父类——

[mask2former/modeling/backbone/swin.py:L690-L726](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/backbone/swin.py#L690-L726) 从 `cfg.MODEL.SWIN.*` 逐项取出 `EMBED_DIM`、`DEPTHS`、`NUM_HEADS`、`WINDOW_SIZE`、`DROP_PATH_RATE` 等，按位置参数传给 `SwinTransformer.__init__`；`input_shape` 形参只是为了匹配 `build_backbone` 的调用签名 `(cfg, input_shape)`，函数体内并未使用。

[mask2former/config.py:L74-L90](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/config.py#L74-L90) `add_maskformer2_config` 注册全部 `MODEL.SWIN` 键，默认值 `EMBED_DIM=96`、`DEPTHS=[2,2,6,2]`、`NUM_HEADS=[3,6,12,24]` 恰好就是 swin-tiny 的形状（注意 `DROP_PATH_RATE` 默认 0.3，比 swin.py 里 0.2 的函数默认值激进，配置层最终生效）。

接口契约的三张表——

[mask2former/modeling/backbone/swin.py:L728-L741](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/backbone/swin.py#L728-L741) 硬编码 stride 表 `{"res2":4, "res3":8, "res4":16, "res5":32}`，通道表则从父类算出的 `num_features`（`embed_dim * 2^i`）取值。`res2`–`res5` 的命名故意沿用 ResNet 时代（`res` = ResNet stage），使下游 `SEM_SEG_HEAD.IN_FEATURES` 在 R50 与 Swin 之间无需改动。

[mask2former/modeling/backbone/swin.py:L743-L758](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/backbone/swin.py#L743-L758) 前向断言输入是 4 维 `(N,C,H,W)`，调父类 `forward` 后按 `_out_features` 过滤输出字典。

[mask2former/modeling/backbone/swin.py:L760-L770](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/backbone/swin.py#L760-L770) `output_shape()` 把两张表拼成 `ShapeSpec(channels=…, stride=…)` 字典（被 u3-l2 的 `MaskFormerHead.from_config` 消费）；`size_divisibility = 32`，因为最深特征是 1/32 分辨率，输入必须被 32 整除，否则最深层会丢像素。

另有 [mask2former/modeling/backbone/swin.py:L680-L683](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/backbone/swin.py#L680-L683) 覆写 `train()`：切训练模式后重新执行 `_freeze_stages()`，保证被冻结的 stage 不会被 `model.train()` 唤醒。

#### 4.1.4 代码实践

**实践：不用训练，只验证「注册 + 接口契约」。**

1. 实践目标：确认 `import mask2former` 后 `D2SwinTransformer` 出现在 `BACKBONE_REGISTRY`，并打印它的 `output_shape()`。
2. 操作步骤（示例代码，保存为独立脚本运行）：

   ```python
   # 示例代码：inspect_swin_registry.py
   from detectron2.config import get_cfg
   from detectron2.modeling import BACKBONE_REGISTRY
   from mask2former import add_maskformer2_config  # import mask2former.* 即触发注册

   cfg = get_cfg()
   add_maskformer2_config(cfg)
   cfg.MODEL.BACKBONE.NAME = "D2SwinTransformer"

   cls = BACKBONE_REGISTRY.get("D2SwinTransformer")
   print("注册表中的类:", cls)
   model = cls(cfg, None)  # 等价于 build_backbone(cfg)
   for name, spec in model.output_shape().items():
       print(name, "channels =", spec.channels, "stride =", spec.stride)
   print("size_divisibility =", model.size_divisibility)
   ```

3. 需要观察的现象：注册表能查到 `D2SwinTransformer`；`output_shape()` 输出四个条目。
4. 预期结果（swin-tiny 默认配置下，通道数为确定性推导）：

   ```
   res2 channels = 96  stride = 4
   res3 channels = 192 stride = 8
   res4 channels = 384 stride = 16
   res5 channels = 768 stride = 32
   size_divisibility = 32
   ```

5. 需要真实环境（PyTorch + detectron2 + timm 已装）才能运行，待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `cfg.MODEL.SWIN.OUT_FEATURES` 改成 `["res3", "res4", "res5"]`，`output_shape()` 和下游会发生什么？
**答案**：`output_shape()` 只剩三个条目（stride 8/16/32）。但注意 Base 配置里 `SEM_SEG_HEAD.IN_FEATURES` 默认包含 `res2`，`MaskFormerHead.from_config` 会按 `IN_FEATURES` 过滤 `input_shape`，缺 `res2` 时相关构建（FPN lateral、mask_features）将因取不到该键而报错——backbone 的 `OUT_FEATURES` 必须覆盖 head 的 `IN_FEATURES`。

**练习 2**：`size_divisibility` 为什么是 32 而不是 4（patch size）？
**答案**：`patch_embed` 下采样 4 倍，随后 3 个 `PatchMerging` 各再降 2 倍，最深特征是输入的 1/32。虽然实现内部对非整除尺寸有 pad 兜底，但 detectron2 在 `ImageList.from_tensors` 时按 `size_divisibility` 预 pad 到 32 的倍数，可保证四个 stage 分辨率都是整数，避免逐层 pad 的尺寸漂移。

### 4.2 SwinTransformer 主体：patch embedding 与四个 stage

#### 4.2.1 概念说明

`SwinTransformer`（`swin.py` 中间的纯 PyTorch 类）回答的问题是：如何把一张图变成四层金字塔。它由三部分组成：

- `PatchEmbed`：一个 `kernel_size = stride = 4` 的卷积，把每个 4×4×3 的像素块线性投影成一个 96 维 token——一步完成「切块 + 降采样到 1/4 + 升维」，对应 CNN 的 stem。
- 4 个 `BasicLayer`（stage）：每个 stage 先跑 `depth[i]` 个 `SwinTransformerBlock`（分辨率、通道不变），再由 `PatchMerging`（最后一个 stage 除外）把 2×2 邻域的 4 个 token 拼接成 4C 维再线性降为 2C——分辨率减半、通道翻倍，对应 CNN的 downsample。
- 每个被输出的 stage 末尾接一个独立的 `LayerNorm`（`norm0`–`norm3`），稳定送入 decoder 的特征。

#### 4.2.2 核心流程

```text
输入 (B, 3, H, W)                       H、W 是 32 的倍数
  └─ PatchEmbed: Conv2d(k=4, s=4)      → (B, 96, H/4, W/4)
  └─ flatten(2).transpose(1,2)         → (B, H/4·W/4, 96)   序列格式，APE=False 不加绝对位置编码
  └─ pos_drop
  ├─ stage0: 2 blocks (W-MSA/SW-MSA)   → res2: (B, 96,  H/4,  W/4)   + PatchMerging
  ├─ stage1: 2 blocks                  → res3: (B, 192, H/8,  W/8)   + PatchMerging
  ├─ stage2: 6 blocks                  → res4: (B, 384, H/16, W/16)  + PatchMerging
  └─ stage3: 2 blocks                  → res5: (B, 768, H/32, W/32)
输出 outs = {"res2":…, "res3":…, "res4":…, "res5":…}
```

通道数的通式：第 \(i\) 个 stage 的通道数为 \(C_i = C_{\text{embed}} \cdot 2^{i}\)（tiny: 96→192→384→768）；`PatchMerging` 中 4C→2C 的线性层是「通道翻倍」的唯一来源。

#### 4.2.3 源码精读

patch embedding——

[mask2former/modeling/backbone/swin.py:L465-L477](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/backbone/swin.py#L465-L477) `PatchEmbed` 用 `nn.Conv2d(in_chans, embed_dim, kernel_size=4, stride=4)` 实现 patch 切分（kernel 等于 stride，块与块不重叠）。

[mask2former/modeling/backbone/swin.py:L479-L495](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/backbone/swin.py#L479-L495) 前向先对非 4 整除的边做右/下 pad，卷积后若 `patch_norm=True` 则先摊平成序列做 LayerNorm 再还原回空间布局——这个「先变序列再 norm」的顺序与后续 block 的序列格式保持一致。

四个 stage 的组装——

[mask2former/modeling/backbone/swin.py:L588-L605](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/backbone/swin.py#L588-L605) 循环构建 `BasicLayer`：`dim=int(embed_dim * 2 ** i_layer)` 实现通道逐级翻倍；`drop_path` 用 `torch.linspace(0, drop_path_rate, sum(depths))` 切片传入，让深层 block 有更大的随机深度率（stochastic depth decay rule）；前 3 个 stage 的 `downsample=PatchMerging`，最后一个为 `None`。

[mask2former/modeling/backbone/swin.py:L610-L614](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/backbone/swin.py#L610-L614) 为每个输出 stage 动态挂一个 `norm{i_layer}` 模块——这四个 norm 是分割版新增的，分类预训练 checkpoint 里没有对应权重。

PatchMerging——

[mask2former/modeling/backbone/swin.py:L327-L335](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/backbone/swin.py#L327-L335) 用步长 2 的四个切片 `[:, 0::2, 0::2, :]` 等取 2×2 邻域，沿通道拼接成 4C，再 `norm` + `reduction`（`nn.Linear(4*dim, 2*dim, bias=False)`，见 [L308](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/backbone/swin.py#L308)）降为 2C；奇数边在 [L323-L325](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/backbone/swin.py#L323-L325) 补零。

顶层 forward 与输出命名——

[mask2former/modeling/backbone/swin.py:L651-L678](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/backbone/swin.py#L651-L678) 这是本讲主目标之一。`patch_embed` 后摊平成序列；`APE=False` 分支直接 `flatten(2).transpose(1, 2)`，不加绝对位置编码（Swin 的位置感来自窗内相对偏置）。每个 stage 返回**双份**结果 `(x_out, H, W, x, Wh, Ww)`：`x_out` 是本 stage 降采样前的输出（过 `norm{i}` 后 reshape 回 `(B, C, H, W)` 并存入 `outs["res{i+2}"]`），`x` 是过 `PatchMerging` 后给下一个 stage 的输入。`"res{}".format(i + 2)` 就是 res2–res5 命名的出处。

[mask2former/modeling/backbone/swin.py:L448-L453](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/backbone/swin.py#L448-L453) `BasicLayer.forward` 的双返回值实现：有 downsample 时返回 `(x, H, W, x_down, (H+1)//2, (W+1)//2)`，否则第二个三元组复用自身——最后一个 stage 不再降采样。

[mask2former/modeling/backbone/swin.py:L442-L447](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/backbone/swin.py#L442-L447) 逐 block 前向；`use_checkpoint=True`（`cfg.MODEL.SWIN.USE_CHECKPOINT`）时用梯度检查点换取显存、牺牲重算时间；注意 `blk.H, blk.W = H, W` 把空间尺寸塞进 block 实例——因为 checkpoint 只接受张量参数，H/W 无法显式传参。

#### 4.2.4 代码实践

**实践：跟踪一次 224×224 前向的形状变化。**

1. 实践目标：亲眼验证「分辨率减半、通道翻倍」和 res2–res5 的 stride。
2. 操作步骤（示例代码）：

   ```python
   # 示例代码：trace_swin_stages.py
   import torch
   from detectron2.config import get_cfg
   from mask2former import add_maskformer2_config
   from mask2former.modeling.backbone.swin import D2SwinTransformer

   cfg = get_cfg(); add_maskformer2_config(cfg)
   cfg.MODEL.BACKBONE.NAME = "D2SwinTransformer"
   # swin-tiny 形状（与本仓库 config.py 默认值一致）
   cfg.MODEL.SWIN.EMBED_DIM = 96
   cfg.MODEL.SWIN.DEPTHS = [2, 2, 6, 2]
   cfg.MODEL.SWIN.NUM_HEADS = [3, 6, 12, 24]

   model = D2SwinTransformer(cfg, None).eval()
   n_params = sum(p.numel() for p in model.parameters())
   print(f"总参数量: {n_params/1e6:.2f}M")

   with torch.no_grad():
       outs = model(torch.randn(1, 3, 224, 224))   # 224 = 32×7，满足 size_divisibility
   for k, v in outs.items():
       print(k, tuple(v.shape))
   ```

3. 需要观察的现象：四个输出的空间尺寸依次 56→28→14→7。
4. 预期结果（形状由 patch_size=4 与三次 2 倍降采样确定性推出）：

   ```
   res2 (1, 96, 56, 56)    # 224/4
   res3 (1, 192, 28, 28)   # 224/8
   res4 (1, 384, 14, 14)   # 224/16
   res5 (1, 768, 7, 7)     # 224/32
   ```

   总参数量约 27–28M（swin-tiny 去掉分类头后的规模，精确数值待本地验证）。
5. 本实践需要真实环境，待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：swin-base 是 `EMBED_DIM=128, DEPTHS=[2,2,18,2]`，各 stage 通道数是多少？参数量相对 tiny 会大幅增加的原因主要在哪一项？
**答案**：通道 128→256→512→1024。参数量增幅主要来自 `EMBED_DIM`（注意力 qkv/proj 与 MLP 的参数对 dim 是平方级），stage2 从 6 层加深到 18 层是次要因素。

**练习 2**：`BasicLayer` 返回 `(x_out, H, W, x, Wh, Ww)` 两份结果，为什么不能只返回降采样后的 `x`？
**答案**：`res2`–`res5` 要求的是**每个 stage 降采样前**的特征（res2 是 1/4 分辨率，恰在第一次 PatchMerging 之前）。若只保留降采样后的序列，最高分辨率输出就变成 1/8，金字塔底层缺失，像素解码器的 mask_features（1/4）无从构建。

### 4.3 Window Attention、Shifted Window 与相对位置偏置

#### 4.3.1 概念说明

- `window_partition` / `window_reverse`：一对纯张量重排函数，把 `(B, H, W, C)` 的特征图切成 `(B·num_windows, w, w, C)` 的窗批，或逆向还原。它们不含任何可学习参数，是 W-MSA 的「进出包装」。
- `WindowAttention`：窗内多头自注意力。与标准注意力唯一的结构性差别是分数矩阵上加了一个按相对位置查表的偏置 \(B\)：

  \[ \mathrm{Attention}(Q,K,V) = \mathrm{Softmax}\!\left( Q K^{\top}/\sqrt{d} + B \right) V \]

- `SwinTransformerBlock`：一个标准 pre-norm Transformer block（`norm1→attn→残差`，`norm2→mlp→残差`），偶数块 `shift_size=0`（W-MSA），奇数块 `shift_size=window_size//2`（SW-MSA，循环移位）。
- 移位的兜底 mask：shift 后一个窗里的 token 来自图像不同区域，`BasicLayer` 预先算出 `(num_windows, N, N)` 的 `attn_mask`，跨区域位置填 `-100`（softmax 后近似为 0 注意力），同区域填 0。

#### 4.3.2 核心流程

SW-MSA block 的一次前向（`SwinTransformerBlock.forward`）：

```text
x: (B, H·W, C)
1. norm1 → reshape (B, H, W, C)
2. 右/下 pad 到 window_size 的整数倍            → Hp, Wp
3. 若 shift_size>0: torch.roll(-shift) 并取 attn_mask；否则不移位、mask=None
4. window_partition        → (B·nW, w, w, C) → view (B·nW, w², C)
5. WindowAttention(x_windows, mask=attn_mask)
   ├ qkv 一次线性投影拆成 q,k,v
   ├ attn = q·kᵀ·scale + relative_position_bias[相对位置索引]
   ├ attn += mask（跨区域 -100）
   └ softmax → dropout → 加权 v → proj
6. window_reverse          → (B, Hp, Wp, C)
7. 反向 roll(+shift)，裁掉 pad，reshape 回 (B, H·W, C)
8. 残差 + norm2 + Mlp + 残差
```

相对位置索引的构造（一次性预计算，注册为 buffer）：窗内坐标 \(c = (h, w)\)，两 token 的相对坐标 \(\Delta = c_i - c_j \in ([-(w{-}1), w{-}1])^2\)，平移到非负后编码成一维索引：

\[ \text{idx}(\Delta h, \Delta w) = (\Delta h + w - 1)\cdot(2w - 1) + (\Delta w + w - 1) \]

于是 \(w=7\) 时表格形状为 \(13 \times 13 = 169\) 行、每头一列。

#### 4.3.3 源码精读

窗口切分与还原——

[mask2former/modeling/backbone/swin.py:L44-L55](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/backbone/swin.py#L44-L55) `window_partition` 用 `view + permute` 把 `(B,H,W,C)` 重排成 `(B·(H/w)(W/w), w, w, C)`：先按「窗行、窗内行、窗列、窗内列」六维 view，再 permute 让窗内两维相邻。

[mask2former/modeling/backbone/swin.py:L58-L71](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/backbone/swin.py#L58-L71) `window_reverse` 是严格互逆操作，从窗批数量反推 batch：`B = windows.shape[0] / (H·W / w²)`。

相对位置偏置表与索引——

[mask2former/modeling/backbone/swin.py:L106-L121](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/backbone/swin.py#L106-L121) 建表 `nn.Parameter(zeros((2w-1)², num_heads))`（169×nH），用 `meshgrid + flatten + 广播相减` 算出 `Wh*Ww × Wh*Ww` 的成对相对坐标，按上面的公式编码成一维索引并 `register_buffer`（非学习、随 checkpoint 存取但可确定性重建）。这正是 u2-l4 里 build_optimizer 按名字匹配 `relative_position_bias_table` 免 weight decay 的对象。

窗内注意力前向——

[mask2former/modeling/backbone/swin.py:L137-L156](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/backbone/swin.py#L137-L156) `qkv` 一个线性层产出三份并 reshape 分头；`attn = q @ kᵀ * scale` 后，用 `relative_position_index.view(-1)` 从表格**gather** 出 `(N, N, nH)` 的偏置加到分数上——查表代替了显式位置编码。

[mask2former/modeling/backbone/swin.py:L158-L171](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/backbone/swin.py#L158-L171) mask 分支把 `(B/nW, nW, nH, N, N)` 的分数加上广播的 mask 后 softmax；随后 `attn @ v`、合并头、`proj` 输出。注意 -100 而非 -inf：数值上足够小且对 fp16 更友好。

移位与掩码的配套——

[mask2former/modeling/backbone/swin.py:L250-L263](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/backbone/swin.py#L250-L263) block 内先 pad 到窗整数倍；`shift_size > 0` 时 `torch.roll(shifts=(-s, -s))` 做循环移位并启用 `mask_matrix`，否则原样切窗。

[mask2former/modeling/backbone/swin.py:L376-L387](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/backbone/swin.py#L376-L387) `BasicLayer` 里 `shift_size = window_size // 2`，且第 `i` 个 block 的 shift 按 `0 if (i % 2 == 0) else window_size // 2` 交替——W-MSA 与 SW-MSA 成对出现。

[mask2former/modeling/backbone/swin.py:L413-L440](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/backbone/swin.py#L413-L440) 移位掩码的构造：在 pad 到 `Hp×Wp` 的画布上，把区域按「(窗口整倍数区, 边缘窗, 移位缝)」三段切成 3×3=9 个编号区域，窗编号不同的位置对填 -100、相同填 0。这个 mask 每个 stage 前向都重算一次（依赖当前 H、W）。

[mask2former/modeling/backbone/swin.py:L277-L289](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/backbone/swin.py#L277-L289) 注意力计算后 `window_reverse` 还原、反向 roll、裁掉 pad 恢复 `(B, H·W, C)`。

#### 4.3.4 代码实践

**实践：验证 partition/reverse 互逆与相对位置索引。**

1. 实践目标：确认两个重排函数互逆；亲手算一个相对位置索引，与 buffer 对账。
2. 操作步骤（示例代码，只需 torch，不需要 detectron2）：

   ```python
   # 示例代码：window_sanity.py
   import torch
   from mask2former.modeling.backbone.swin import window_partition, window_reverse, WindowAttention

   x = torch.arange(2 * 8 * 8 * 4, dtype=torch.float32).reshape(2, 8, 8, 4)  # B=2,H=W=8,C=4
   w = window_partition(x, 4)          # (2*4, 4, 4, 4)
   x2 = window_reverse(w, 4, 8, 8)
   print("互逆:", torch.equal(x, x2))   # 预期 True

   attn = WindowAttention(dim=8, window_size=(4, 4), num_heads=2)
   idx = attn.relative_position_index                  # (16, 16)
   # 窗内 (0,0) 与 (0,1) 的相对坐标 Δ=(0,-1)，按公式:
   manual = (0 + 4 - 1) * (2 * 4 - 1) + (-1 + 4 - 1)   # = 3*7+2 = 23
   print("手工索引:", manual, " buffer 索引:", idx[0, 1].item())
   print("偏置表形状:", tuple(attn.relative_position_bias_table.shape))  # 预期 (49, 2)
   ```

3. 需要观察的现象：`互逆: True`；手工索引与 buffer 中 `(0,0)→(0,1)` 位置的索引相等。
4. 预期结果：`manual == idx[0,1] == 23`；表形状 `(2*4-1)²=49` 行、`num_heads=2` 列。索引部分为确定性推导，可直接核对；运行需真实环境，待本地验证。
5. 若把 `window_size` 换 7，可自行重算 `(0,0)→(0,1)` 的索引并核对表格应为 169 行。

#### 4.3.5 小练习与答案

**练习 1**：为什么移位掩码里用 `-100` 而不是 `-inf`？
**答案**：softmax 后两者效果几乎一样（\(e^{-100}\approx 0\)），但 `-inf` 在 fp16/AMP 下容易产生 NaN（0×inf 等未定义运算），且要求整行不全为 `-inf`。用大负数更稳健——这也是工程上常见的写法。

**练习 2**：`relative_position_bias_table` 的形状依赖 `window_size`。若把 `WINDOW_SIZE` 从 7 改成 12，加载旧 checkpoint 会发生什么？
**答案**：表格从 169 行变成 529 行，形状不匹配，直接加载会报 size mismatch 错误；这类参数在 `matching_heuristics` 的形状匹配中也会因形状不同而失配，最终保持新初始化。所以改窗口大小不能复用旧预训练。

**练习 3**：为什么 `WindowAttention` 的 mask 形状是 `(num_windows, N, N)` 而 softmax 前又要 view 成 `(B/nW, nW, nH, N, N)`？
**答案**：切窗后输入批维是 `B·nW`（窗被摊平进 batch），mask 只按「窗」区分而与图像无关，因此按 `B/nW` 组、每组 `nW` 个窗广播，让同一图像内所有窗共享各自窗号对应的 mask 切片。

### 4.4 权重转换：convert_pretrained_swin_to_d2 与 key 对齐规则

#### 4.4.1 概念说明

Mask2Former 不从零训练 Swin，而是加载官方 ImageNet-1K 分类预训练权重。但两边的 state_dict 不能直接对接：官方 checkpoint 是为分类模型保存的普通 `torch.save` 文件（key 不带任何前缀），而 detectron2 的 `DetectionCheckpointer` 期望一个 pickle 字典、且模型内 key 都挂在 `backbone.` 前缀下（`MaskFormer.backbone` 是 Swin 所在子模块）。转换脚本的任务就是补上这层「包装」。

**关键认知**：这个脚本**不改任何 key 名**。它只做两件事——取出 `torch.load(input)["model"]`，再包上 `{"model": obj, "__author__": "third_party", "matching_heuristics": True}`。真正的 key 对齐发生在加载时：detectron2 的 `DetectionCheckpointer` 看到 `matching_heuristics=True`，会用「形状一致 + 名字组分相似度」的匹配算法把 checkpoint key 对齐到模型 key 上，效果上等价于自动补 `backbone.` 前缀。

#### 4.4.2 核心流程

```text
官方 swin_tiny_patch4_window7_224.pth          （torch.save 的分类模型 state_dict）
  │ torch.load(...)["model"]
  ▼
{ "model": obj,                                （key 原样：patch_embed.*、layers.*、norm.*、head.*）
  "__author__": "third_party",
  "matching_heuristics": True }                ← 对齐开关
  │ pickle 到 .pkl
  ▼
MODEL.WEIGHTS: swin_tiny_patch4_window7_224.pkl
  │ DetectionCheckpointer 加载
  ▼
形状匹配：checkpoint.layers.0.blocks.0.norm1.weight  ↭  model.backbone.layers.0.blocks.0.norm1.weight
忽略：norm.*（分类末端 norm）、head.*（分类头）
留空：backbone.norm0..norm3.*（分割版新增，随机初始化）
```

#### 4.4.3 源码精读

[tools/convert-pretrained-swin-model-to-d2.py:L22-L31](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/tools/convert-pretrained-swin-model-to-d2.py#L22-L31) 全部逻辑仅 9 行：`torch.load(input, map_location="cpu")["model"]` 取出参数字典（官方 checkpoint 顶层有 `model` 键），原样放入 `res["model"]`，附上 `__author__` 与 `matching_heuristics: True`，pickle 写出。文件头注释给出完整用法：下载 `swin_tiny_patch4_window7_224.pth`、转换成 `.pkl`、config 里设 `MODEL.WEIGHTS` 与 `INPUT.FORMAT: "RGB"`。

[tools/convert-pretrained-swin-model-to-d2.py:L9-L20](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/tools/convert-pretrained-swin-model-to-d2.py#L9-L20) 脚本 docstring 写明了三步用法与配套 config 片段。

[configs/coco/panoptic-segmentation/swin/maskformer2_swin_tiny_bs16_50ep.yaml:L2-L15](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/panoptic-segmentation/swin/maskformer2_swin_tiny_bs16_50ep.yaml#L2-L15) 真实配置的用法：`BACKBONE.NAME: "D2SwinTransformer"` 切换 backbone，`MODEL.SWIN.*` 覆盖 tiny 形状，`WEIGHTS: "swin_tiny_patch4_window7_224.pkl"` 指向转换产物（继承自 Base 的 `INPUT.FORMAT: "RGB"` 与官方 Swin 的预处理一致，`PIXEL_MEAN/STD` 为 ImageNet 统计）。

**key 映射规则**（效果上的对应关系，由 `matching_heuristics` 在加载时达成，而非脚本显式改名）：

| 官方分类 checkpoint key | Mask2Former 模型内 key | 结果 |
| --- | --- | --- |
| `patch_embed.proj.*` / `patch_embed.norm.*` | `backbone.patch_embed.*` | 匹配加载 |
| `layers.{i}.blocks.{j}.norm1/norm2/attn.qkv/attn.proj/attn.relative_position_bias_table/mlp.*` | `backbone.layers.{i}.blocks.{j}...`（同名展开） | 匹配加载 |
| `layers.{i}.downsample.reduction/norm.*` | `backbone.layers.{i}.downsample.*` | 匹配加载 |
| `norm.*`（分类末端最终 norm，形状 [768]） | `backbone.norm0..norm3.*` 形状各不相同 | 无同形对，忽略 |
| `head.*`（768→1000 分类头） | 模型中不存在 | 忽略 |
| ——（checkpoint 中没有） | `backbone.norm0..norm3.*`（4.2 讲过的分割版新增） | 随机初始化 |

匹配的准入条件是**张量形状一致**——这也解释了 4.3 练习 2：改 `WINDOW_SIZE` 后偏置表形状变化，匹配自动失效。

#### 4.4.4 代码实践

**实践：完成一次转换并核对 key。**

1. 实践目标：产出 detectron2 可加载的 `.pkl`，并用程序验证上面的映射表。
2. 操作步骤：

   ```bash
   # 1) 下载官方分类权重（约 110MB）
   wget https://github.com/SwinTransformer/storage/releases/download/v1.0.0/swin_tiny_patch4_window7_224.pth
   # 2) 转换（在仓库 tools/ 目录下执行）
   python convert-pretrained-swin-model-to-d2.py swin_tiny_patch4_window7_224.pth swin_tiny_patch4_window7_224.pkl
   ```

   ```python
   # 示例代码：check_swin_keys.py
   import pickle, torch

   ckpt = pickle.load(open("swin_tiny_patch4_window7_224.pkl", "rb"), encoding="latin1")
   print("顶层键:", list(ckpt.keys()))            # 预期 ['model', '__author__', 'matching_heuristics']
   keys = ckpt["model"].keys()
   print("带 backbone. 前缀的 key 数:", sum(k.startswith("backbone.") for k in keys))  # 预期 0
   print("head 键:", [k for k in keys if k.startswith("head")][:2])                    # 预期存在 → 证明未改名
   ```

3. 需要观察的现象：pkl 顶层有三个键；`model` 内 **没有任何** `backbone.` 前缀；仍保留 `head.*`、`norm.*` 分类键。
4. 预期结果：如上三行注释；这直接证明「前缀对齐交给 `matching_heuristics`」的结论。需要下载权重与真实环境，待本地验证。
5. 想再进一步，可构建完整 `MaskFormer` 后用 `DetectionCheckpointer(model, "swin_tiny_patch4_window7_224.pkl").load_or_resolve()`，观察日志中「匹配/未匹配」的参数统计（日志中未匹配的 `backbone.norm*` 会保持初始化）。

#### 4.4.5 小练习与答案

**练习 1**：对比 `tools/convert-torchvision-to-d2.py`（R50 用）与 `tools/convert-pretrained-swin-model-to-d2.py`（Swin 用），转换策略有何本质不同？
**答案**：R50 的 torchvision checkpoint 顶层结构不同、且 key 命名与 detectron2 ResNet 约定不同，转换脚本需要**显式改写/挑选 key**；Swin 脚本不改 key，只包装并打开 `matching_heuristics`，把对齐工作交给 detectron2 的形状匹配。（建议对照阅读两个脚本确认细节。）

**练习 2**：为什么转换后 config 还要保证 `INPUT.FORMAT: "RGB"` 与 ImageNet 的 `PIXEL_MEAN/STD`？
**答案**：预训练权重的数值分布绑定其预处理方式。官方 Swin 用 RGB、ImageNet 均值方差归一化；若沿用 BGR 或错误统计量，特征分布会系统性偏移，预训练收益受损。swin tiny yaml 里显式重写了这两项正是为此。

## 5. 综合实践

**任务：构建 swin-tiny 版 Mask2Former backbone，输出「形状—参数—转换规则」三件套报告。**

1. 实践目标：把本讲四个模块串起来——注册机制、stage 结构、window attention 参数规模、权重对齐规则。
2. 操作步骤：
   1. 按 4.2.4 的脚本构建 `D2SwinTransformer`（swin-tiny：`EMBED_DIM=96, DEPTHS=[2,2,6,2], NUM_HEADS=[3,6,12,24]`），`eval()` 模式对 `torch.randn(2, 3, 224, 224)` 前向（batch 取 2 顺便验证 batch 维无关性）。
   2. 打印每个输出的形状，与 `output_shape()` 中 `ShapeSpec` 声明的 channels/stride 对账。
   3. 统计总参数量，并单独统计所有 `relative_position_bias_table` 的参数总量（提示：`sum(p.numel() for n,p in model.named_parameters() if "relative_position_bias_table" in n)`）。
   4. 把 `WINDOW_SIZE` 改成 12 再构建一次，观察偏置表形状变化与参数量差异，用一句话解释为什么旧预训练失效。
   5. 阅读 [tools/convert-pretrained-swin-model-to-d2.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/tools/convert-pretrained-swin-model-to-d2.py) 并写出 4.4.3 的映射表，标注每行是「加载 / 忽略 / 随机初始化」。
3. 需要观察的现象：输出形状严格符合 4-8-16-32 的 stride 梯度；`WINDOW_SIZE=12` 时偏置表从 169 行变 529 行。
4. 预期结果：形状 `(2,96,56,56)/(2,192,28,28)/(2,384,14,14)/(2,768,7,7)`；tiny 总参数约 27–28M；四个 stage 各一张偏置表（3/6/12/24 头），合计约 `(169)×(3+6+12+24)×4 个stage内全部block` 量级（每个 block 一张，精确数待本地验证）。
5. 全程 CPU 即可完成，无需下载数据集；精确参数量与偏置表统计待本地验证。

## 6. 本讲小结

- `D2SwinTransformer` 是「纯 Swin 实现 + detectron2 适配器」：注册进 `BACKBONE_REGISTRY`，用硬编码的 stride 表和 `num_features` 通道表兑现 `output_shape()` / `size_divisibility=32` 契约，`res2`–`res5` 命名使 head 配置与 R50 完全通用。
- `SwinTransformer.forward` = `PatchEmbed`(k=s=4 卷积，1/4) + 4 个 `BasicLayer`（块内不变分辨率，`PatchMerging` 减半分辨率/翻倍通道）+ 每个 stage 末端 `norm{i}`；输出取的是**各 stage 降采样前**的特征。
- window attention 把全图 \(O(N^2)\) 注意力切成窗内 \(O(N w^2)\)；`window_partition`/`window_reverse` 是一对无参数的互逆重排；位置信息来自按相对位置索引查表的 `relative_position_bias_table`（buffer 存索引，可确定性重建）。
- shifted window 靠 `torch.roll` 半窗移位打通跨窗信息，配套的 `attn_mask` 用 -100 屏蔽移位产生的跨区域注意；偶数块 W-MSA、奇数块 SW-MSA 交替。
- 权重转换脚本不改 key，只设置 `matching_heuristics: True`，由 `DetectionCheckpointer` 按「形状一致 + 名字相似」把无前缀分类权重对齐到 `backbone.*`；分类头与末端 norm 被忽略，`norm0..3` 随机初始化。

## 7. 下一步学习建议

- 下一讲（u3-l4）向三段式的第二段进发：`BasePixelDecoder` 如何把本讲输出的 res2–res5 金字塔（经 `input_shape` 声明的通道/stride）做自顶向下融合，产出 1/4 的 `mask_features` 与三个尺度特征——重点看 [mask2former/modeling/pixel_decoder/fpn.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/fpn.py)。
- 若想加固 window attention 直觉，可回头对照 u4-l2 的 `SelfAttentionLayer`（全图注意力 + 正弦位置编码）与本讲窗内注意力 + 相对偏置的差异。
- 想做 backbone 替换的读者可提前浏览 `ADVANCED_USAGE.md` 的自定义 backbone 流程，并以 `D2SwinTransformer` 的注册与 `output_shape()` 实现作为模板（u8-l1 会正式展开）。
- 官方论文：Swin Transformer（arXiv:2103.14030）第 3.2 节给出了 W-MSA/SW-MSA 复杂度公式与相对位置偏置的消融，可与本讲 4.3 的源码逐条对照。
