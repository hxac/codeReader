# u3-l1 ViT 骨干：PatchEmbed、Block 与位置编码

## 1. 本讲目标

上一讲（u2-l5）我们拆开了 `ehm_model(img_patch)` 这个黑盒，看到 `Ehm_Pipeline.forward` 里的一行 `feats = self.backbone(x[:, :, :, 32:-32])`。本讲就深入这一行，把 `backbone` —— 也就是 `models/backbones/vit.py` 里的 `ViT` 类 —— 完整讲透。学完本讲你应该能够：

1. 手算出一张 256×192 的人体 patch 进入 ViT 后，在哪一步变成 192 个 token、每个 token 是多少维；
2. 说清 `PatchEmbed`、`Block`（内含 `Attention` + `Mlp` + `DropPath`）、`forward_features` 三段各自做什么；
3. 对照 `configs/infer.yaml` 的 `BACKBONE` 段，解释为什么 PEAR 用的是 embed_dim=1280、depth=32、num_heads=16 这种"巨大"配置（ViT-Huge 规模，约 6.3 亿参数），以及它为什么来自 ViTPose 预训练（`backbone_ckpt` 字段）；
4. 理解 `frozen_stages` / `freeze_attn` / `freeze_ffn` 三套冻结开关的机制，以及它们在 PEAR 的实际配置中是否被启用。

## 2. 前置知识

- **ViT（Vision Transformer）**：把图像切块（patch）、每块当作一个"单词"（token）送进 Transformer 的模型。与 CNN 靠卷积核滑窗不同，ViT 靠自注意力让任意两个 token 直接交换信息。
- **token 与 embedding**：一张 256×192 的图切成 16×16 的小块后得到 16×12=192 个小块，每个小块经一次卷积被映射成一个 1280 维向量——这个向量就叫 token。192 个 token 排成序列，形状为 `(B, 192, 1280)`。
- **自注意力（self-attention）**：每个 token 生成 Query、Key、Value 三个向量；用自己 的 Q 与所有人的 K 做匹配得到权重，再加权求和所有人的 V。公式见 4.2.2 节。
- **残差连接（residual connection）**：`x = x + f(x)`，让梯度可以绕过变换直接回传，是深层网络（32 层！）能训得动的前提。
- **位置编码（positional embedding）**：注意力本身不感知空间位置，必须给每个 token 加一个"我是第几个格子"的向量，否则打乱 patch 顺序输出不变。
- **timm**：PyTorch 图像模型库，本文件从它导入 `drop_path`、`to_2tuple`、`trunc_normal_` 三个工具（`requirements.txt` 第 70 行声明了 `timm` 依赖）。
- 承接 u2-l5 的关键结论：**位置编码长度 193，token 序列必须恰为 192**——本讲会从 PatchEmbed 的卷积参数推导出这个 192 是怎么来的。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [models/backbones/vit.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/backbones/vit.py) | 本讲主角。ViT 骨干完整实现：PatchEmbed、Attention、Mlp、DropPath、Block、ViT 及冻结策略 |
| [models/backbones/\_\_init\_\_.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/backbones/__init__.py) | 仅一行 `from .vit import ViT`，所以外部 `from models.backbones import ViT` 导入的就是这个类 |
| [configs/infer.yaml](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/configs/infer.yaml) | `BACKBONE` 段（第 10–21 行）驱动 `ViT(**cfg.BACKBONE)` 的全部超参 |
| [models/pipeline/ehm_pipeline.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/ehm_pipeline.py) | 推理外壳：第 24 行构造 ViT，第 46–47 行归一化、裁剪 256×192 后调用骨干 |
| [models/pipeline/pipeline.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/pipeline.py) | 训练外壳：第 57 行同样构造 ViT，第 67–69 行调用 `_init_backbone()`（第 560–563 行）加载 ViTPose 预训练权重——这是 `backbone_ckpt` 字段的真正消费者 |

## 4. 核心概念与源码讲解

### 4.1 PatchEmbed：从 256×192 图像到 192 个 token

#### 4.1.1 概念说明

PatchEmbed 解决的问题是：Transformer 只吃"向量序列"，不吃图像。最简单的做法是用一个**卷积**把每个 16×16 的图像块线性映射成一个 `embed_dim` 维向量——`kernel_size=16, stride=16` 的卷积恰好等价于"不重叠切块 + 每块做一次线性投影"。PEAR 的 `PatchEmbed` 正是这么做的，但它的 `padding` 参数藏着一个不显眼的技巧（见 4.1.2）。

#### 4.1.2 核心流程

1. 构造时根据 `img_size=[256,192]`、`patch_size=16`、`ratio=1` 计算 `num_patches` 与 `patch_shape`；
2. 唯一的层是一个 Conv2d，代入 ratio=1 后等价于：

\[ \text{Conv2d}(3 \to 1280,\ \text{kernel}=16,\ \text{stride}=16,\ \text{padding}=4+2\cdot(1//2-1)=2) \]

3. 卷积输出空间尺寸（注意 padding=2 会往四周各补 2 个零）：

\[ H_p = \left\lfloor\frac{256 + 2\times 2 - 16}{16}\right\rfloor + 1 = 16, \qquad W_p = \left\lfloor\frac{192 + 2\times 2 - 16}{16}\right\rfloor + 1 = 12 \]

4. `flatten(2).transpose(1, 2)` 把 `(B, 1280, 16, 12)` 变成 `(B, 192, 1280)`——**192 个 token、每个 1280 维**，这就是 u2-l5 说的"token 序列必须恰为 192"的来源。

一个值得注意的细节：若 padding=0，卷积输出同样是 16×12（请代入上式自行验证）。所以 padding=2 并不改变 token 数量，它改变的是**每个 token 的感受野窗口**：第 \(i\) 个窗口覆盖输入的区间 \([16i-2,\ 16i+14)\)，即整体左移 2 像素、且首尾窗口各伸进补零区 2 像素。这也带来一个边界效应：最右侧/最下侧 2 像素的原图内容不落在任何窗口内。这是 ViTDet/ViTPose 一系代码的"pad 式"切块风格，`ViT.__init__` 里保存的 `self.patch_padding = 'pad'` 属性名即由此而来。

#### 4.1.3 源码精读

**构造函数：计算网格并创建卷积投影。**

[models/backbones/vit.py:141-155](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/backbones/vit.py#L141-L155) —— `PatchEmbed.__init__`：把 `img_size`/`patch_size` 用 `to_2tuple` 统一成二元组后计算 `num_patches = (192//16) * (256//16) * 1² = 192`、`patch_shape = (16, 12)`，然后创建唯一的层 `self.proj`，即上文的 Conv2d。`ratio` 是为高分辨率插值预留的参数，PEAR 固定为 1。

**前向：卷积 → 展平成 token 序列。**

[models/backbones/vit.py:157-163](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/backbones/vit.py#L157-L163) —— `PatchEmbed.forward`：对 `(B,3,256,192)` 做卷积得 `(B,1280,16,12)`，记录实际网格 `(Hp,Wp)=(16,12)`，再 `x.flatten(2).transpose(1,2)` 得 `(B,192,1280)`。注意它**返回的是元组** `(x, (Hp, Wp))`——网格尺寸要留给 `forward_features` 最后一步"还原成二维特征图"用。

**配置侧对照。**

[configs/infer.yaml:10-21](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/configs/infer.yaml#L10-L21) —— `BACKBONE` 段里的 `img_size: [256, 192]`、`patch_size: 16`、`embed_dim: 1280`、`ratio: 1` 四行决定了上面的全部数字。注意这里的 `img_size` 描述的是**裁剪后**送进骨干的尺寸（`Ehm_Pipeline.forward` 里的 `x[:, :, :, 32:-32]`），而不是入口脚本的 256×256。

#### 4.1.4 代码实践

1. **实践目标**：用纸笔或一行 Python 验证 192 这个数字，并亲手观察 padding 的边界效应。
2. **操作步骤**（示例代码，非项目原有代码）：

```python
import torch.nn as nn
conv = nn.Conv2d(3, 1280, kernel_size=16, stride=16, padding=4 + 2*(1//2 - 1))
x = torch.zeros(1, 3, 256, 192)
y = conv(x)
print(y.shape)          # 期望 torch.Size([1, 1280, 16, 12])
print(16 * 12)          # 192 个 token
```

再把 `padding=2` 换成 `padding=0` 重跑一次，对比输出形状。
3. **需要观察的现象**：两种 padding 下输出网格都是 `(16, 12)`。
4. **预期结果**：确认 padding 不改变 token 数；随后按窗口公式 \([16i-2, 16i+14)\) 手算第 0 行与第 15 行窗口覆盖的像素区间，体会"左移 2 像素 + 伸入补零区"的边界效应。本脚本未在此环境实际运行，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：如果 `Ehm_Pipeline.forward` 不做 `x[:, :, :, 32:-32]` 裁剪、直接把 256×256 送进骨干，PatchEmbed 会输出多少个 token？会导致什么错误？

**答案**：网格变为 \(\lfloor(256+4-16)/16\rfloor+1 = 16\) 与 \(\lfloor(256+4-16)/16\rfloor+1 = 16\)，即 16×16=256 个 token。PatchEmbed 本身不会报错，但下一步加位置编码时 `x + self.pos_embed[:, 1:]` 会因 `(1,256,1280)` 与 `(1,192,1280)` 无法广播而报 RuntimeError——这正是 u2-l5 所说"裁剪是硬约束"的机制。

**练习 2**：`patch_shape` 属性和 forward 返回的 `(Hp, Wp)` 是同一个东西吗？

**答案**：数值上相同（都是 (16,12)），但来源不同：`patch_shape` 是构造时按 `img_size` 公式算出的"理论网格"，`(Hp, Wp)` 是前向时从卷积输出的真实形状读出来的。`forward_features` 用的是后者——即使输入尺寸与配置不符（只要后续广播能通过），还原二维特征图也以实际输出为准。

---

### 4.2 Block：Attention + Mlp + DropPath 的标准 Transformer 块

#### 4.2.1 概念说明

`Block` 是 ViT 的"乐高积木"，PEAR 的骨干把这块积木**堆了 32 层**。每层做两件事，各带一次归一化和残差连接：

- **Attention（多头自注意力）**：token 之间互相"交流"，聚合全局信息；
- **Mlp（前馈网络）**：每个 token 独立地做非线性变换，"消化"交流来的信息。

**DropPath（随机深度）**是训练期的正则化手段：以一定概率把整个残差分支丢掉（`x = x + 0`），迫使网络不过度依赖某几层。它按层深线性增大丢弃概率——越深的层丢得越狠。

#### 4.2.2 核心流程

单个 Block 的前向（Pre-Norm 风格：先归一化再进子层）：

```text
x = x + DropPath( Attention( LayerNorm(x) ) )   # 交流
x = x + DropPath( Mlp(       LayerNorm(x) ) )   # 消化
```

多头注意力的数学定义（\(D=1280\) 为 embed_dim，\(h=16\) 为头数，\(d_h = D/h = 80\) 为每头维度）：

\[ \mathrm{Attention}(Q, K, V) = \mathrm{softmax}\!\left(\frac{Q K^{\top}}{\sqrt{d_h}}\right) V, \qquad \sqrt{d_h} = \sqrt{80} \approx 8.94 \]

源码中缩放实现为 `self.scale = head_dim ** -0.5`（≈0.1118），并作用在 \(Q\) 上（`q = q * self.scale`）而非除在乘积上，两者数学等价。注意力权重矩阵形状为 `(B, 16, 192, 192)`——192 个 token 两两之间的相似度。

DropPath 的逐层丢弃概率（随机深度衰减规则，`drop_path_rate=0.55`、共 32 层）：

\[ p_i = \frac{i}{31} \times 0.55, \quad i = 0, 1, \dots, 31 \]

即第 0 层不丢、第 31 层丢 55%。**推理时（`model.eval()`）DropPath 是恒等映射**，因为 timm 的 `drop_path(x, p, self.training)` 在 `training=False` 时直接返回 `x`。

#### 4.2.3 源码精读

**Attention 构造：一次线性层同时生成 Q、K、V。**

[models/backbones/vit.py:76-95](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/backbones/vit.py#L76-L95) —— `Attention.__init__`：`head_dim = dim // num_heads = 1280 // 16 = 80`；`self.qkv = nn.Linear(dim, all_head_dim * 3)` 把 1280 维投影到 3840 维（Q/K/V 拼在一起）；`qkv_bias=True`（来自 `infer.yaml` 第 18 行）使这个 Linear 带 bias。

**Attention 前向：拆头 → 缩放 → 注意力 → 拼回。**

[models/backbones/vit.py:97-113](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/backbones/vit.py#L97-L113) —— `Attention.forward`：`qkv.reshape(B, N, 3, num_heads, -1).permute(2, 0, 3, 1, 4)` 把 `(B,192,3840)` 拆成 3 份 `(B,16,192,80)`；`attn = (q * self.scale) @ k.transpose(-2,-1)` 得 `(B,16,192,192)` 后 softmax；`x = (attn @ v).transpose(1,2).reshape(B, N, -1)` 把 16 个头拼回 1280 维，再过输出投影 `self.proj`。

**Mlp：两层 Linear 夹一个 GELU。**

[models/backbones/vit.py:59-74](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/backbones/vit.py#L59-L74) —— `Mlp`：`1280 → 5120 → 1280`（`mlp_ratio=4`，即隐藏层扩 4 倍：`mlp_hidden_dim = int(1280 * 4) = 5120`）。

**DropPath：timm 随机深度的薄封装。**

[models/backbones/vit.py:46-57](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/backbones/vit.py#L46-L57) —— `DropPath.forward` 直接调用 `timm.models.layers.drop_path`，按 `self.training` 决定是否生效。

**Block：组装以上部件。**

[models/backbones/vit.py:115-138](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/backbones/vit.py#L115-L138) —— `Block.__init__` 创建 `norm1 + attn` 与 `norm2 + mlp` 两组子层；注意第 130 行 `DropPath(drop_path) if drop_path > 0. else nn.Identity()`——第 0 层的 drop_path 恰为 0，因此直接用恒等映射省一次函数调用。`Block.forward`（第 135–138 行）就是 4.2.2 开头那两行残差公式。

**32 层的堆叠与逐层 drop_path。**

[models/backbones/vit.py:230-237](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/backbones/vit.py#L230-L237) —— `ViT.__init__` 中 `dpr = torch.linspace(0, drop_path_rate, depth)` 生成从 0 到 0.55 的 32 个等间距数，作为每层的 `drop_path` 传入；`nn.ModuleList` 装 32 个结构相同（超参相同、DropPath 概率不同）的 Block。`drop_path_rate: 0.55` 见 [configs/infer.yaml:12](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/configs/infer.yaml#L12)。

#### 4.2.4 代码实践

1. **实践目标**：单独实例化一个 Block，跑通前向并验证 train/eval 两种模式下 DropPath 的行为差异。
2. **操作步骤**（示例代码，非项目原有代码）：

```python
import torch
from models.backbones.vit import Block

blk = Block(dim=1280, num_heads=16, mlp_ratio=4., qkv_bias=True, drop_path=0.55)
x = torch.randn(1, 192, 1280)

blk.eval()
with torch.no_grad():
    y1 = blk(x); y2 = blk(x)
    print(torch.equal(y1, y2))      # 期望 True：eval 下 DropPath 恒等，两次输出一致

blk.train()
with torch.no_grad():
    y1 = blk(x); y2 = blk(x)
    print(torch.equal(y1, y2))      # 期望 False：训练模式下残差分支被随机丢弃
```

3. **需要观察的现象**：eval 模式输出确定；train 模式两次前向结果不同。
4. **预期结果**：如上两个布尔值分别为 True / False。这解释了为什么 PEAR 三个推理入口都要求模型处于 eval 状态。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`qkv_bias: True`（infer.yaml 第 18 行）会让 Attention 里多了哪些参数？共多少个？

**答案**：让 `self.qkv` 这个 Linear 带 bias，参数量为 `all_head_dim * 3 = 3840` 个（`self.proj` 的 bias 本来就默认存在，1280 个）。若 `qkv_bias=False`，Attention 将完全没有 bias 项（`self.proj` 仍有）。

**练习 2**：为什么注意力矩阵是 `(B, 16, 192, 192)` 而不是 `(B, 192, 192)`？

**答案**：因为 16 个头各自独立算一套注意力。每个头在自己的 80 维子空间里做 192×192 的相似度矩阵，互不干扰；最后拼回 `(B, 192, 1280)`。多头让不同头可以关注不同模式（例如一个头盯手部、另一个头盯整体姿态）。

**练习 3**：第 0 层 Block 的 `drop_path` 是多少？对应代码里哪个分支？

**答案**：`dpr[0] = linspace(0, 0.55, 32)[0] = 0`，因此第 130 行走 `nn.Identity()` 分支——第 0 层实际上没有 DropPath 模块。

---

### 4.3 ViT.forward_features：位置编码、32 层堆叠与特征图还原

#### 4.3.1 概念说明

`forward_features` 是骨干的完整前向函数，把 PatchEmbed、位置编码、32 个 Block 和最终归一化串起来，并在结尾做一件对本项目至关重要的事：把 `(B, 192, 1280)` 的 token 序列**还原成 `(B, 1280, 16, 12)` 的二维特征图**。因为下游解码头（下一讲 u3-l2 的 TransformerDecoder）虽然按 token 序列处理，但 Ehm_Pipeline 拿到的 `feats` 是四维特征图。

`ViT.__init__` 里还有两个部件需要认识：`self.pos_embed`（长度 193 的位置编码表——192 个 patch 槽位 + 1 个 cls 槽位）和 `self.last_norm`（最终 LayerNorm）。**注意这个 ViT 没有分类头、也没有 cls token 参与计算**——它是为 ViTPose 姿态预训练骨干改造的"纯特征版"。

#### 4.3.2 核心流程

```text
输入 x: (B, 3, 256, 192)
  ├─ patch_embed(x)            →  tokens (B, 192, 1280)，网格 (16, 12)
  ├─ 加位置编码（无 cls token 的特殊加法）
  │     x = x + pos_embed[:, 1:] + pos_embed[:, :1]
  ├─ 依次通过 32 个 Block（可选用 gradient checkpoint）
  ├─ last_norm（LayerNorm）
  └─ permute + reshape 还原二维
输出: (B, 1280, 16, 12)
```

位置编码那一行值得展开。`pos_embed` 形状 `(1, 193, 1280)`：第 0 个槽位是预训练模型里 cls token 的位置编码，第 1–192 个槽位对应 192 个 patch。PEAR 不使用 cls token，于是把第 0 个槽位**广播加到所有 token 上**，其余 192 个槽位按位相加。这样既保住了 193 长度的权重表（能直接加载 ViTPose 预训练权重），又不破坏 192 长度的序列。

#### 4.3.3 源码精读

**位置编码表的创建。**

[models/backbones/vit.py:227-228](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/backbones/vit.py#L227-L228) —— `self.pos_embed = nn.Parameter(torch.zeros(1, num_patches + 1, embed_dim))`：长度 192+1=193，注释 `# since the pretraining model has class token` 直接点明这是为了兼容带 cls token 的预训练权重。第 241–242 行用 `trunc_normal_(std=.02)` 重新初始化（可学习位置编码）。

**前向主函数。**

[models/backbones/vit.py:307-326](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/backbones/vit.py#L307-L326) —— `forward_features`：第 309 行调 PatchEmbed（行尾中文注释"patch 大小为 16 * 12"即网格尺寸）；第 311–314 行按上文方式加位置编码（注释说明这种加法对 sin-cos 式位置编码无差别、并兼容多卡训练）；第 316–320 行循环过 32 个 Block，若 `use_checkpoint=True` 则用 `checkpoint.checkpoint(blk, x)` 换取省显存（`infer.yaml` 第 20 行设为 False，直接前向）；第 322 行 `last_norm`；第 324 行 `x.permute(0,2,1).reshape(B, -1, Hp, Wp)` 把序列折回 `(B, 1280, 16, 12)` 二维网格——PatchEmbed 返回的 `(Hp, Wp)` 在这里派上用场。

**forward 与 init_weights。**

[models/backbones/vit.py:328-330](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/backbones/vit.py#L328-L330) —— `forward` 仅转调 `forward_features`，所以 `Ehm_Pipeline.forward` 里的 `self.backbone(...)` 拿到的就是四维特征图。[models/backbones/vit.py:283-298](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/backbones/vit.py#L283-L298) —— `init_weights` 提供 Linear/LayerNorm 的 trunc_normal 初始化，供从零训练时使用。

**调用方上下文。**

[models/pipeline/ehm_pipeline.py:46-47](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/ehm_pipeline.py#L46-L47) —— `x = self.normalize(x)` 做 ImageNet 归一化后，`feats = self.backbone(x[:, :, :, 32:-32])` 裁掉左右各 32 列再进骨干；输出的 `feats (B,1280,16,12)` 随即交给 head。

**顺带一提：文件里的两处"化石"。** [models/backbones/vit.py:13-44](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/backbones/vit.py#L13-L44) 的 `get_abs_pos`（插值缩放位置编码）在本仓库内无任何调用；[models/backbones/vit.py:166-195](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/backbones/vit.py#L166-L195) 的 `HybridEmbed` 仅当传入 `hybrid_backbone` 才会启用，而 PEAR 的两个配置都没传。它们是自 ViTDet 复制而来的遗留代码，读源码时可跳过。

#### 4.3.4 代码实践

1. **实践目标**：单独验证"位置编码长度必须等于 token 数 + 1"这条硬约束。
2. **操作步骤**（示例代码，非项目原有代码）：

```python
import torch
from models.backbones.vit import ViT

vit = ViT(img_size=(256,192), patch_size=16, embed_dim=1280, depth=32,
          num_heads=16, mlp_ratio=4., qkv_bias=True)
print(vit.pos_embed.shape)          # 期望 (1, 193, 1280)

ok = torch.zeros(1, 3, 256, 192)
print(vit(ok).shape)                # 期望 (1, 1280, 16, 12)

bad = torch.zeros(1, 3, 256, 256)   # 不裁剪的原始 patch
try:
    vit(bad)
except RuntimeError as e:
    print('报错:', e)               # 期望广播失败
```

3. **需要观察的现象**：第三段代码抛出 RuntimeError，报错信息里出现形状不匹配的描述。
4. **预期结果**：`pos_embed` 长度 193；合法输入输出 `(1, 1280, 16, 12)`；256×256 输入在加位置编码一步报错。注意：实例化该模型约需 2.4 GB 内存（见 4.4.4 的参数量估算），机器内存紧张时请知悉。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`x + self.pos_embed[:, 1:] + self.pos_embed[:, :1]` 中，第二项加法为什么不会引发广播错误？

**答案**：`self.pos_embed[:, :1]` 形状为 `(1, 1, 1280)`，PyTorch 广播规则会把第 1 维自动扩展到 192，等价于把 cls 槽位的位置编码**同一个向量**加到每个 token 上。这在数学上是对所有 token 的一致平移。

**练习 2**：第 324 行 `x.permute(0, 2, 1).reshape(B, -1, Hp, Wp)` 中，如果 PatchEmbed 返回的 `(Hp, Wp)` 被误写死为 `(12, 16)`（即 H、W 互换），会发生什么？

**答案**：`permute` 后 x 形状为 `(B, 1280, 192)`，`reshape(B, -1, 12, 16)` 也能成功（总共 192 个元素），不报错但**空间排布错乱**——特征图被转置了，下游 head 看到的左右位置全部颠倒，属于静默 bug。这提示 reshape 类操作要格外小心维度顺序。

---

### 4.4 巨型配置、冻结策略与 ViTPose 预训练（frozen_stages / freeze_attn / freeze_ffn / backbone_ckpt）

#### 4.4.1 概念说明

先算一笔账。把 `infer.yaml` 的数字代入，这个骨干有多大？每个 Block 的参数量近似为 \(12D^2\)（\(D=1280\)）加上少量 bias 与 norm 项，精确值为：

| 组成 | 计算 | 参数量 |
| --- | --- | --- |
| 单个 Block | qkv \(3D^2{+}3D\) + proj \(D^2{+}D\) + fc1 \(4D^2{+}4D\) + fc2 \(4D^2{+}D\) + 两个 norm \(4D\) | 19,677,440 |
| 32 个 Block | 32 × 上式 | 629,678,080 |
| patch_embed.proj | \(16^2 \times 3 \times 1280 + 1280\) | 984,320 |
| pos_embed | \(193 \times 1280\) | 247,040 |
| last_norm | \(2 \times 1280\) | 2,560 |
| **合计** | | **≈ 630,912,000（约 6.31 亿）** |

embed_dim=1280、depth=32、num_heads=16 正是 **ViT-Huge** 的标准配置——这正是 ViTPose-H 使用的骨干。`backbone_ckpt: "data_inputs/backbone/vitpose_backbone.pth"`（infer.yaml 第 21 行）说明 PEAR 不是从零训练这个巨兽，而是**继承 ViTPose 在大规模人体姿态数据上预训练好的权重**：姿态估计任务要求骨干对人体关节位置极其敏感，这份先验恰好也是人体网格恢复最需要的。

`ViT` 类内置三套冻结开关（`frozen_stages` / `freeze_attn` / `freeze_ffn`），用于微调时冻结部分层。但要注意：**PEAR 的两个 YAML 都没有设置这三项**，因此全部走默认值（不冻结），骨干整体参与训练。

#### 4.4.2 核心流程

三套开关在 `_freeze_stages()` 中的行为：

```text
frozen_stages = -1（默认）      → 什么都不冻结
frozen_stages = k (k ≥ 0)      → 冻结 patch_embed；再冻结 blocks[1..k]
                                 （注意下标从 1 开始，blocks[0] 永不因该开关被冻结）
freeze_attn = True             → 冻结所有层的 attn 与 norm1（注意力通路）
freeze_ffn = True              → 冻结 pos_embed、patch_embed 以及所有层的 mlp 与 norm2（前馈通路）
```

冻结包含两个动作：`requires_grad = False`（不更新梯度）与 `.eval()`（保持推理模式，让 Dropout/DropPath 失效）。此外 `ViT.train()` 被重写为先调 `super().train(mode)` 再重跑 `_freeze_stages()`——因为 `.train()` 会把所有子模块切回训练模式，必须在每次切换后把被冻结的部分重新压回 eval。

#### 4.4.3 源码精读

**构造函数：登记所有冻结开关。**

[models/backbones/vit.py:200-217](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/backbones/vit.py#L200-L217) —— `ViT.__init__` 签名里的 `frozen_stages=-1, ... freeze_attn=False, freeze_ffn=False, backbone_ckpt=NotImplemented`：`backbone_ckpt` 这个形参**在函数体内从未被使用或保存**，它存在的唯一意义是让 `ViT(**cfg.BACKBONE)` 不因 YAML 里多出 `backbone_ckpt` 键而抛 TypeError——真正的权重加载发生在训练管线里。

**冻结实现。**

[models/backbones/vit.py:246-281](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/backbones/vit.py#L246-L281) —— `_freeze_stages`：第 248–257 行处理 `frozen_stages`（`frozen_stages >= 0` 时冻结 patch_embed，再循环 `range(1, frozen_stages+1)` 冻结 blocks[1..k]）；第 259–267 行 `freeze_attn` 分支逐层冻结 `m.attn` 与 `m.norm1`；第 269–281 行 `freeze_ffn` 分支冻结 `pos_embed`、`patch_embed` 以及各层的 `m.mlp` 与 `m.norm2`。

**train() 重写。**

[models/backbones/vit.py:332-335](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/backbones/vit.py#L332-L335) —— 每次 `train(mode)` 后重跑 `_freeze_stages()`，保证被冻结子层在 `.train()` 全局切换后仍停留在 eval 模式。

**真正的 ViTPose 权重加载器在训练管线里。**

[models/pipeline/pipeline.py:560-563](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/pipeline.py#L560-L563) —— `OurPipeline._init_backbone`：从 `self.cfg.BACKBONE.backbone_ckpt` 读取 `vitpose_backbone.pth` 的 `['state_dict']` 并 `load_state_dict` 进 `self.backbone`（即 ViT）。由 [models/pipeline/pipeline.py:67-69](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/pipeline.py#L67-L69) 在构造时调用，注释写明"推理模式用已调好的骨干 checkpoint 时不必再初始化"。

**骨干确实参与训练（未被冻结）。**

[models/pipeline/pipeline.py:530](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/pipeline.py#L530) —— `get_parameter_groups` 返回 `head.parameters() + backbone.parameters()`，优化器覆盖整个骨干，印证"PEAR 的实际配置不冻结骨干"。

**又一处化石：ViT 内部失效的 _init_backbone。**

[models/backbones/vit.py:337-353](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/backbones/vit.py#L337-L353) —— `ViT._init_backbone` 引用了 `self.cfg` 与 `self.backbone`，但 `ViT.__init__` 既不保存 cfg 也没有名为 backbone 的子模块——这个方法一旦被调用就会 AttributeError，属于自其他项目复制后未清理的死代码。加载逻辑请以 pipeline.py:560 为准。同理 [models/backbones/vit.py:303-305](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/backbones/vit.py#L303-L305) 的 `no_weight_decay()`（返回 `{'pos_embed', 'cls_token'}`，本类并无 cls_token 参数）在本仓库内也无人调用，它是 mmengine 系优化器的约定接口。

#### 4.4.4 代码实践

1. **实践目标**：用 `infer.yaml` 的 BACKBONE 段（随机权重）实例化完整 ViT，跑通前向、核对所有中间形状、统计参数量，并验证冻结开关的效果。
2. **操作步骤**（示例代码，非项目原有代码；保存为 `inspect_vit.py` 从仓库根目录运行）：

```python
import torch
from utils.general_utils import ConfigDict
from models.backbones import ViT

cfg = ConfigDict(model_config_path='configs/infer.yaml')
vit = ViT(**cfg.BACKBONE)                      # 与 Ehm_Pipeline 第 24 行完全相同的构造方式

# ① 参数统计
total = sum(p.numel() for p in vit.parameters())
print(f'总参数量: {total:,}')                   # 期望 ≈ 630,912,000

# ② 前向与中间形状
x = torch.zeros(1, 3, 256, 192)
tokens, (Hp, Wp) = vit.patch_embed(x)
print('token 序列:', tokens.shape, '网格:', Hp, Wp)   # 期望 (1,192,1280) 16 12
with torch.no_grad():
    feat = vit(x)
print('输出特征图:', feat.shape)                # 期望 (1, 1280, 16, 12)
print('位置编码:', vit.pos_embed.shape)         # 期望 (1, 193, 1280)
print('Block 数量:', vit.get_num_layers())      # 期望 32

# ③ 冻结开关实验
frozen = ViT(**cfg.BACKBONE)
frozen.frozen_stages, frozen.freeze_attn = 2, True
frozen._freeze_stages()
n_frozen = sum(1 for p in frozen.parameters() if not p.requires_grad)
print('被冻结参数张量个数:', n_frozen)           # patch_embed + blocks[1..2] 的 attn/norm1 等
```

3. **需要观察的现象**：参数量是否落在 6.3 亿附近；token 数是否恰为 192；`frozen_stages=2, freeze_attn=True` 时有多少参数张量的 `requires_grad` 变为 False。
4. **预期结果**：总参数量 ≈ 630,912,000（fp32 下约占 2.4 GB 内存/显存，实例化前请确认资源充足）；输出特征图 `(1, 1280, 16, 12)`。据此可回答：这份配置即 ViT-Huge 骨干，对应 ViTPose-H；`backbone_ckpt` 字段指向的 `data_inputs/backbone/vitpose_backbone.pth` 在训练启动时由 `OurPipeline._init_backbone` 加载（推理用的 `pear_model.pt` 里则已包含训练好的骨干权重，见 u2-l5 的权重加载约定）。由于 `vitpose_backbone.pth` 需自行获取，加载环节**待本地验证**。
5. 另一个值得记录的现象：不设 `frozen_stages`（PEAR 的默认用法）时第 ③ 步 `n_frozen` 恒为 0——印证 4.4.1 的结论"PEAR 实际不冻结骨干"。

#### 4.4.5 小练习与答案

**练习 1**：为什么 PEAR 选择加载 ViTPose 的预训练权重而不是 ImageNet 预训练权重？

**答案**：ViTPose 是在大规模人体姿态估计数据（COCO、MPII 等，带 2D 关键点标注）上训练的，其特征天然对人体关节、肢体结构敏感；而人体网格恢复的本质就是从图像回归关节与体型参数，两者的特征需求高度重合。ImageNet 分类特征更偏物体语义，不如姿态特征对口。`vitpose_backbone.pth` 文件名直接标明了来源。

**练习 2**：`frozen_stages=1` 时，blocks[0] 会被冻结吗？patch_embed 呢？

**答案**：patch_embed 会被冻结（`frozen_stages >= 0` 即触发第 249–251 行）；blocks[0] **不会**——循环是 `range(1, frozen_stages + 1)` 即 `range(1, 2) = [1]`，只含 blocks[1]。这个"从 1 开始"的下标约定沿袭自多阶段 CNN（stage 0 是 stem）的写法，读代码时容易踩坑。

**练习 3**：如果只想微调解码头、让骨干完全不动，除了把 `frozen_stages` 设为 32，还有什么更直接的办法？

**答案**：在训练管线侧不把骨干参数交给优化器即可——例如改 `pipeline.py:530` 的 `get_parameter_groups` 只返回 `head.parameters()`，或对骨干整体 `requires_grad_(False)`。`ViT._init_backbone` 末尾（vit.py:351–353）被注释保留的 `freeze_backbone` 逻辑就是这个思路，但它位于失效方法内，实际不可用。

---

## 5. 综合实践

**任务：给 ViT 骨干写一份"体检报告"。** 综合本讲的 PatchEmbed、Block、forward_features 与冻结策略，写一个脚本 `pear-tutorial` 实践（示例代码，放在仓库根目录手动运行、运行后自行删除，不要提交），完成三件事：

1. **形状追踪表**：对 `(1, 3, 256, 192)` 输入，依次打印 patch_embed 输出、加完位置编码后、第 0 个 Block 输出、第 31 个 Block 输出、last_norm 输出、最终 reshape 后的形状，整理成一张表，并在每行标注对应的源码行号（vit.py 的 309、314、316–320、322、324 行）。
2. **参数分布饼图**：分别统计 patch_embed / blocks（32 层合计）/ pos_embed / last_norm 四部分的参数量占比，验证"约 99.8% 的参数在 blocks 里"（\(629{,}678{,}080 / 630{,}912{,}000 \approx 99.8\%\)）。
3. **感受野窗口推导**：按 4.1.2 的窗口公式 \([16i-2,\ 16i+14)\)，写出 12 个列窗口各自的覆盖区间，找出**没有被任何窗口覆盖的原图列**，与 4.1 节的边界效应结论互相印证。

参考框架（在第 2 点用 `torchvision.utils` 或直接打印数字皆可）：

```python
import torch
from utils.general_utils import ConfigDict
from models.backbones import ViT

cfg = ConfigDict(model_config_path='configs/infer.yaml')
vit = ViT(**cfg.BACKBONE).eval()

parts = {
    'patch_embed': sum(p.numel() for p in vit.patch_embed.parameters()),
    'blocks':      sum(p.numel() for p in vit.blocks.parameters()),
    'pos_embed':   vit.pos_embed.numel(),
    'last_norm':   sum(p.numel() for p in vit.last_norm.parameters()),
}
for k, v in parts.items():
    print(f'{k:12s} {v:>12,}  ({v / sum(parts.values()) * 100:.2f}%)')
```

**验收标准**：形状表能对上 4.3.2 的流程图；参数占比能对上手工估算；窗口推导能解释 padding=2 的作用。全部结果**待本地验证**。

## 6. 本讲小结

- **PatchEmbed** 用一个 `Conv2d(3→1280, kernel=16, stride=16, padding=2)` 把 256×192 的 patch 变成 `(B, 192, 1280)` 的 token 序列，网格 16×12；padding=2 不改变 token 数，但让每个窗口左移 2 像素并伸入补零区，形成边界效应。
- **Block = Pre-Norm 残差 + 多头注意力 + 4 倍 MLP + DropPath**，堆 32 层；DropPath 概率从 0 线性升到 0.55，仅训练期生效。
- **位置编码长度 193 = 192 patch 槽 + 1 cls 槽**；PEAR 不用 cls token，而是把 cls 槽广播加到所有 token 上——这是为直接加载 ViTPose 预训练权重做的设计。
- `forward_features` 最后把 token 序列 `permute + reshape` 回 `(B, 1280, 16, 12)` 特征图，PatchEmbed 返回的网格尺寸在这一步复用。
- `infer.yaml` 的 BACKBONE 段是 **ViT-Huge 规模（约 6.31 亿参数）**，权重来自 `vitpose_backbone.pth` 的 ViTPose 姿态预训练；推理用的 `pear_model.pt` 已包含训练好的骨干。
- 三套冻结开关（`frozen_stages` / `freeze_attn` / `freeze_ffn`）机制完备，但 PEAR 的配置均未启用，骨干整体参与训练；vit.py 内的 `get_abs_pos`、`HybridEmbed`、`ViT._init_backbone`、`no_weight_decay` 是无调用的遗留代码，读码时可跳过。

## 7. 下一步学习建议

骨干输出的 `(B, 1280, 16, 12)` 特征图下一步去了哪里？下一讲 **u3-l2 TransformerDecoder：零 token 查询与 cross-attention** 将精读 `models/smplx/pose_transformer.py`：解码头如何把 16×12 特征图重排成 192 个 context token，再让一个**零初始化的查询 token** 通过 6 层 cross-attention 从中"拷问"出人体参数的种子表示。建议先自行浏览 `pose_transformer.py` 的 `TransformerCrossAttn` 与 `TransformerDecoder` 两个类，带着"查询 token 与 context token 各自的来源"这个问题进入下一讲。
