# Ehm_Pipeline.forward：从图像 patch 到参数字典

## 1. 本讲目标

在 u2-l2 里，我们把 `inference_wo_detect.py` 走读了一遍，但其中这一行当时是当作黑盒处理的：

```python
outputs = ehm_model(img_patch)
```

本讲就打开这个黑盒。读完本讲，你应该能够：

1. 说清 `Ehm_Pipeline` 在构造时创建了哪些子模块、各自由哪段配置驱动。
2. 解释 `forward` 内部的三步流程：ImageNet 归一化 → `x[:, :, :, 32:-32]` 把 256×256 裁成 256×192 → 骨干提特征 → 解码头出参数，并能推导每一步的张量形状。
3. 解释为什么裁剪是「硬约束」：位置编码的长度决定了输入必须正好是 256×192。
4. 说出 `pear_model.pt` 的两段式结构（`backbone` / `head`），以及三个入口脚本共用的 `strict=False` 加载约定，并能从 checkpoint 反推出模型结构。
5. 独立写一个脚本：下载权重、加载模型、对一张真实图片跑前向，并打印 `body_param` / `flame_param` 各键的形状与 `pd_cam` 的 4×4 矩阵。

本讲刻意停在「接口层」：骨干内部的 Block、解码头内部的 cross-attention 细节分别在 u3-l1 与 u3-l2 精读；相机矩阵的数学推导在 u3-l4；`ehm()` 把参数变成网格的过程在 u4-l4。

## 2. 前置知识

- **LightningModule 是什么**：`lightning.LightningModule` 是 PyTorch Lightning 库对 `torch.nn.Module` 的封装，一般用来承载训练逻辑（训练步、优化器配置等）。`Ehm_Pipeline` 继承了它，但**推理时只用到了普通 `nn.Module` 的能力**——`forward()` 就是普通的逐层调用。训练外壳（Fabric/DDP）在 `models/pipeline/pipeline.py` 的 `OurPipeline` 里，属 u5-l2 的内容。
- **state dict 是什么**：PyTorch 把一个模块的所有可学习参数（和部分缓冲区）按 `参数名 → 张量` 的字典形式保存，称为 state dict。`load_state_dict()` 按**参数名和形状**做匹配还原。`strict=False` 表示「能匹配的就载入，匹配不上的不报错」，其返回值是 `(missing_keys, unexpected_keys)` 两个列表。
- **torchvision Normalize**：对输入按通道做标准化
  \[ x_c' = \frac{x_c - \mu_c}{\sigma_c} \]
  其中 \(\mu, \sigma\) 用的是 ImageNet 数据集在 RGB 三通道上的统计值。它要求输入取值在 \([0,1]\)，这就是入口脚本里 `img_patch/255` 的由来。
- **ViT（Vision Transformer）直觉**：把图像切成 16×16 的小块（patch），每块线性投影成一个 token，加上位置编码后送入一摞 Transformer Block，输出仍是 token 序列。本讲只用这个直觉，内部结构见 u3-l1。
- **hf_hub_download**：HuggingFace Hub 的文件下载函数，首次调用会把文件缓存到本地，再次调用直接返回缓存路径。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [models/pipeline/ehm_pipeline.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/ehm_pipeline.py) | 本讲主角：`Ehm_Pipeline` 类，构造 + `forward` 三步流程 |
| [configs/infer.yaml](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/configs/infer.yaml) | 推理配置：BACKBONE / HEAD / TRAIN 三段驱动模型构造 |
| [utils/pipeline_utils.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/pipeline_utils.py) | 输入侧工具 `to_tensor`（另有 `perspective_projection` 等训练用函数） |
| [models/backbones/vit.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/backbones/vit.py) | ViT 骨干：`PatchEmbed`、位置编码、`forward_features`（本讲只看形状链条） |
| [models/smplx/smplx_head.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/smplx_head.py) | `SMPLXTransformerDecoderHead`：9 个线性解码器 → 参数字典 |
| [models/smplx/pose_transformer.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/pose_transformer.py) | `TransformerDecoder`：零 token + cross-attention（本讲只看调用方式） |
| [app.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py) / [inference_wo_detect.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py) / [inference_images.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_images.py) | 三个入口对权重加载与输入构造的「复制式」用法 |

## 4. 核心概念与源码讲解

### 4.1 Ehm_Pipeline 的构造：一个外壳，两个子模块

#### 4.1.1 概念说明

`Ehm_Pipeline` 本身**几乎不含计算逻辑**，它是一个「外壳」：把一个图像骨干（backbone）和一个参数解码头（head）拼在一起，再附一个归一化变换。真正干活的是两个子模块：

- `self.backbone = ViT(**cfg.BACKBONE)` —— 把 `configs/infer.yaml` 的 BACKBONE 段**整段解包**成构造参数，输出图像特征图；
- `self.head = SMPLXTransformerDecoderHead(cfg.HEAD, cfg.TRAIN.batch_size)` —— 用 HEAD 段构造，把特征图翻译成 SMPL-X / FLAME / 相机三组参数。

注意第二个参数来自 **TRAIN 段**（`cfg.TRAIN.batch_size`，infer.yaml 里是 2）——这是 u2-l1 结论的又一例证：推理配置也复用 TRAIN 段来建模型。

#### 4.1.2 核心流程

构造函数做的事情可以列成：

1. 存下 `cfg`，初始化几个**训练/调试用**属性（`_dump_dir`、`_total_iters`、`_check_interval`、`_visual_train_interval`、`_debug`、`body_image_size`、`head_image_size`）——这些在 `forward` 里完全不参与计算。
2. `ViT(**cfg.BACKBONE)` 建骨干。
3. `SMPLXTransformerDecoderHead(...)` 建解码头。**构造解码头时会读仓库自带的 [assets/SMPLX/smpl_mean_params.npz](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/smplx_head.py#L164-L185) 做均值参数初始化**，所以即使只做推理、不加载任何权重，也需要这个资产文件（好消息：它随仓库自带，无需下载）。
4. 创建 ImageNet 归一化变换 `self.normalize`。

#### 4.1.3 源码精读

构造函数全文（[models/pipeline/ehm_pipeline.py:L13-L26](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/ehm_pipeline.py#L13-L26)）：

```python
class Ehm_Pipeline(L.LightningModule):
    def __init__(self, cfg):
        super(Ehm_Pipeline, self).__init__()
        self.cfg = cfg
        self._dump_dir =  os.path.join('outputs', "test", ...)   # 训练/调试用
        self._total_iters = cfg.TRAIN.train_iter                 # 训练/调试用
        self._check_interval = cfg.TRAIN.check_interval          # 训练/调试用
        ...
        self.backbone = ViT(**cfg.BACKBONE)                      # 骨干：BACKBONE 段整段解包
        self.head = SMPLXTransformerDecoderHead(cfg.HEAD, cfg.TRAIN.batch_size)
        self.normalize = torchvision.transforms.Normalize(
            mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])
```

对照配置看两个子模块由什么驱动（[configs/infer.yaml:L10-L21](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/configs/infer.yaml#L10-L21) 与 [configs/infer.yaml:L23-L31](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/configs/infer.yaml#L23-L31)）：

- BACKBONE 段：`embed_dim: 1280`、`depth: 32`、`num_heads: 16`、`patch_size: 16`、`img_size: [256, 192]`——一个 32 层、通道 1280 的巨型 ViT，**设计输入分辨率 256（高）×192（宽）**，这一点是 4.2 节裁剪的伏笔。
- HEAD 段：`context_dim: 1280`（吃骨干的 1280 通道特征）、`depth: 6`、`heads: 8`、`dim_head: 64`、`mlp_dim: 1024`——一个 6 层的 cross-attention 解码器。注意 HEAD 段里**没有 `dim` 键**，解码器自身宽度 `dim=1024` 是 `smplx_head.py` 里的默认值（见 4.2.3）。

#### 4.1.4 代码实践

**实践：零权重「干跑」构造，验证配置到结构的映射**（CPU 即可，无需下载权重）。

1. 目标：确认「BACKBONE 段 → 32 个 Block」「HEAD 段 → 9 个线性解码器」的映射关系。
2. 操作步骤：激活 u1-l2 建好的 `pear` 环境，在仓库根目录运行下面的示例代码（可直接 `python -c` 或存成临时脚本）：

```python
# 示例代码：dry_run_construct.py
import torch
from models.pipeline.ehm_pipeline import Ehm_Pipeline
from utils.general_utils import ConfigDict, add_extra_cfgs

meta_cfg = add_extra_cfgs(ConfigDict(model_config_path='configs/infer.yaml'))
model = Ehm_Pipeline(meta_cfg)          # 只需 assets/SMPLX/smpl_mean_params.npz（仓库自带）

print('backbone blocks 数量:', len(model.backbone.blocks))          # 期望 32（depth）
print('backbone pos_embed 形状:', model.backbone.pos_embed.shape)   # 期望 (1, 193, 1280)
print('head 解码器数量:', sum(1 for n in model.head.children()
                              if n.endswith('decoder')))             # 期望 9 个 nn.Linear
print('head 参数量(约):', sum(p.numel() for p in model.head.parameters()) / 1e6, 'M')
```

3. 需要观察的现象：`pos_embed` 的第二维是 **193 = 192 + 1**（192 个 patch 位置 + 1 个 cls 位置），记住这个数字，4.2 节它会变成硬约束。
4. 预期结果：构造成功且无需网络；block 数、解码器数与配置一致。参数量数值**待本地验证**（它取决于具体权重形状，此处不做断言）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `configs/infer.yaml` 的 `BACKBONE.depth` 从 32 改成 16，构造出的模型哪一项会变？加载 `pear_model.pt` 时会发生什么？

> 答案：`model.backbone.blocks` 变成 16 层；加载 `state['backbone']` 时，checkpoint 里 `blocks.16.` ~ `blocks.31.` 的键会变成 unexpected keys。因为脚本用 `strict=False` 且丢弃返回值，**不会报错**，但模型实际上丢了一半骨干，输出会完全错误——这就是 `strict=False` 的风险：它掩盖结构不匹配。

**练习 2**：`Ehm_Pipeline.__init__` 里 `self._dump_dir`、`cfg.TRAIN.train_iter` 这些属性在推理时用到了吗？

> 答案：没有。它们只在训练/可视化链路（u5-l2 的 `OurPipeline`）中有意义。读源码时应当把「构造时初始化」与「forward 时使用」分开看，前者不等于后者。

### 4.2 forward 三步流程：normalize → 裁剪入骨干 → 解码头出参数

#### 4.2.1 概念说明

`forward` 只有三个动作：

1. **归一化**：对 \([0,1]\) 的输入做 ImageNet 逐通道标准化，让像素分布与骨干预训练时一致。
2. **裁剪 + 骨干**：`x[:, :, :, 32:-32]` 把 256×256 的输入左右各裁掉 32 列变成 256×192，再送 ViT 得到特征图 `(B, 1280, 16, 12)`。
3. **解码头**：特征图被重排成 192 个 token，一个零初始化的查询 token 通过 6 层 cross-attention 从图像 token 里「问」出信息，最终被 9 个线性层翻译成参数字典。

为什么裁成 256×192？两个原因，一个是设计选择，一个是硬约束：

- **设计上**：256:192 = 4:3，是人体姿态/网格恢复领域（包括 PEAR 使用的 ViTPose 预训练骨干）最常见的输入纵横比，竖长的人体在 4:3 画布里浪费的像素最少。入口脚本的 `pad_and_resize` 先 letterbox 成正方形，模型内部再统一裁成 4:3，两步合起来保证「任何长宽比的输入都收敛到同一个模型入口」。
- **硬约束上**：`configs/infer.yaml` 里 `img_size: [256, 192]`，ViT 据此算出 patch 网格 16×12 = **192 个位置**，位置编码表长度是 192+1=193。`forward_features` 里直接做 `x + self.pos_embed[:, 1:]`（见下文），如果你喂 256×256，patch 网格变成 16×16 = 256 个 token，192 长的位置编码无法广播到 256 个 token，**直接形状报错**。所以这个裁剪不是可选优化，是模型能跑的前提。

#### 4.2.2 核心流程

以 batch=1、`B=1` 为例的完整形状链条（每一步都可在源码中对应到行）：

```text
入口脚本:  cv2.imread → pad_and_resize(256)        (256,256,3) uint8 BGR
           to_tensor + /255 + permute + unsqueeze  (1,3,256,256) float [0,1]
---------------------------------------------------------------- forward 内部
第 1 步     self.normalize(x)                       (1,3,256,256)
第 2 步     x[:,:,:,32:-32]                         (1,3,256,192)   ← 左右各裁 32 列
           PatchEmbed: Conv2d(3→1280,k=16,s=16,pad=2)
                                                  → (1,1280,16,12)  ← 16×12=192 个 token
           + pos_embed[:,1:] + pos_embed[:,:1]      (1,192,1280)
           32 × Block + last_norm                   (1,192,1280)
           reshape 回二维网格                        (1,1280,16,12)   ← feats
第 3 步     head: rearrange 'b c h w -> b (h w) c'  (1,192,1280)    ← context
           零 token (1,1,1) → Linear(1→1024)        (1,1,1024)
           6 层 cross-attention(context=192 token)  (1,1,1024)
           squeeze → 9 个 nn.Linear(1024→…)         (1,1024)
---------------------------------------------------------------- 输出
outputs['body_param']  11 个键的字典（见 4.2.3 输出表）
outputs['flame_param']  6 个键的字典
outputs['pd_cam']      (1,4,4)  RT 矩阵
```

其中卷积输出尺寸用标准公式
\[ H_{out} = \left\lfloor \frac{H + 2p - k}{s} \right\rfloor + 1 \]
代入 \(H=256,\ p=2,\ k=16,\ s=16\) 得 \(\lfloor 244/16 \rfloor + 1 = 16\)；代入 \(W=192\) 得 \(\lfloor 180/16 \rfloor + 1 = 12\)。

#### 4.2.3 源码精读

**forward 全文**（[models/pipeline/ehm_pipeline.py:L29-L52](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/ehm_pipeline.py#L29-L52)）：

```python
def forward(self, x: torch.Tensor):
    # save_image(x[:, :, :, 32:-32],"input.jpg")   ← 作者留下的调试行：可以打印裁剪后的输入
    x = self.normalize(x)                          # 第 1 步：ImageNet 归一化
    feats = self.backbone(x[:, :, :, 32:-32])      # 第 2 步：裁剪 + ViT → (B,1280,16,12)
    outputs = self.head(feats)                     # 第 3 步：解码头 → 参数字典
    return outputs
```

三个要点：

- 函数 docstring 写返回 `'pd_cam': shape (B,3)` 和 `'pd_params'`、`'focal_length'`（[ehm_pipeline.py:L37-L44](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/ehm_pipeline.py#L37-L44)），但真实返回键是 `pd_cam (B,4,4)`、`body_param`、`flame_param`（见下文 head 的 `all_out`）。**注释过时，以代码为准**——这与 u2-l2 在 `inference_wo_detect.py` 里的发现一致。注释上面那行被注释掉的 `save_image` 恰好是观察裁剪输入的最直观证据。
- 归一化在裁剪**之前**作用于整张 256×256；由于 Normalize 是逐像素运算，先裁后归一在数学上等价，只是写法顺序问题。
- 输入必须是 \([0,1]\) 的 RGB 张量。u2-l2 已指出 `cv2.imread` 读进来是 BGR 且脚本未转换就送入模型，这是仓库的遗留不一致，颜色通道会错位，但网络是在同样错位的数据上训练的，所以结果「自洽地正确」。

**第 2 步内部：裁剪为什么必须发生**（[models/backbones/vit.py:L307-L326](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/backbones/vit.py#L307-L326)）：

```python
def forward_features(self, x):
    B, C, H, W = x.shape
    x, (Hp, Wp) = self.patch_embed(x)      # 卷积切块 → (B,192,1280)，网格 (16,12)
    if self.pos_embed is not None:
        x = x + self.pos_embed[:, 1:] + self.pos_embed[:, :1]   # ← 位置编码硬约束
    for blk in self.blocks:                # 32 层 Block
        x = blk(x)
    x = self.last_norm(x)
    xp = x.permute(0, 2, 1).reshape(B, -1, Hp, Wp).contiguous() # token 序列还原成特征图
    return xp                              # (B, 1280, 16, 12)
```

`self.pos_embed[:, 1:]` 形状是 `(1,192,1280)`，要求 token 序列恰好 192 个。位置编码表在构造时按 `img_size: [256,192]` 生成（[models/backbones/vit.py:L228](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/backbones/vit.py#L228)：`nn.Parameter(torch.zeros(1, num_patches + 1, embed_dim))`，`num_patches = (192//16)×(256//16) = 192`）。另有一个有趣的细节：预训练模型带 cls token，这里没有保留它，而是把它的嵌入 `pos_embed[:, :1]` **加到每个 patch token 上**。切块卷积的定义在 [models/backbones/vit.py:L155](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/backbones/vit.py#L155)（`Conv2d(3, 1280, kernel_size=16, stride=16, padding=4+2*(1//2-1)=2)`）。

**第 3 步内部：零 token → 9 个解码器 → 参数字典**（[models/smplx/smplx_head.py:L255-L319](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/smplx_head.py#L255-L319)）：

```python
def forward(self, x, **kwargs):
    B = x.shape[0]
    x = einops.rearrange(x, 'b c h w -> b (h w) c')   # 特征图 → 192 个 context token
    token = x.new_zeros(B, 1, 1)                      # 查询 token 从全零出发
    token_out = self.transformer(token, context=x)    # 6 层 cross-attention
    token_out = token_out.squeeze(1)                  # (B, 1024)
    ...                                                # 9 个 nn.Linear 切片、rot6d_to_rotmat
    all_out = {}
    all_out['pd_cam'] = RT                             # (B,4,4)
    all_out['body_param'] = body_param_dict
    all_out['flame_param'] = flame_param_dict
    return all_out
```

`self.transformer` 是 `TransformerDecoder`：`to_token_embedding = nn.Linear(token_dim=1, dim=1024)` 把标量零 token 投到 1024 维，加位置嵌入后进 6 层 `TransformerCrossAttn`（自注意力 + 对 context 的 cross-注意力 + FFN），定义见 [models/smplx/pose_transformer.py:L306-L362](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/pose_transformer.py#L306-L362)。注意 1024 这个宽度来自 `smplx_head.py` 构造时的默认 `dim=1024`（HEAD 段没有 `dim` 键去覆盖它），而 context 是 1280 维，`CrossAttention` 里用 `to_kv = nn.Linear(1280, …)` 完成两个宽度的桥接。

9 个解码器全部定义在 [models/smplx/smplx_head.py:L129-L147](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/smplx_head.py#L129-L147)，输出的切片组装在 [L270-L300](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/smplx_head.py#L270-L300)。汇总成输出表（`B` 为 batch，推理时 =1）：

| 输出键 | 子键 | 形状 | 来源解码器 |
| --- | --- | --- | --- |
| `body_param` | `global_pose` | (B,1,3,3) | `smplx_poses_decoder`（Linear 1024→312）前 6 维经 6D→旋转矩阵 |
| | `body_pose` | (B,21,3,3) | 同上，接下来 126 维 |
| | `left_hand_pose` | (B,15,3,3) | 同上 |
| | `right_hand_pose` | (B,15,3,3) | 同上 |
| | `hand_scale` | (B,3) | `smplx_scale_decoder`（1024→6）前半 |
| | `head_scale` | (B,3) | 同上后半 |
| | `eye_pose` / `jaw_pose` / `joints_offset` | `None` | 占位（`smplx_joint_decoder` 虽存在但未启用） |
| | `exp` | (B,50) | `smplx_expression_decoder` |
| | `shape` | (B,200) | `smplx_shape_decoder` |
| `flame_param` | `eye_pose_params` | (B,6) | `flame_poses_decoder`（1024→14）切片 |
| | `pose_params` | (B,3) | 同上 |
| | `jaw_params` | (B,3) | 同上 |
| | `eyelid_params` | (B,2) | 同上 |
| | `expression_params` | (B,50) | `flame_expression_decoder` |
| | `shape_params` | (B,300) | `flame_shape_decoder` |
| `pd_cam` | — | (B,4,4) | `cam_decoder`（1024→3）+ 固定 R 组装成的 RT 矩阵 |

关于 `pd_cam` 的来历只讲结论：`cam_decoder` 输出 3 维，第三维加上偏置后按 \(z = f/s = 24/s\) 换算成深度（[models/smplx/smplx_head.py:L303-L306](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/smplx_head.py#L303-L306)），再与固定的对角旋转 \(R = \mathrm{diag}(-1,-1,1)\) 拼成 4×4 的 RT（[models/smplx/smplx_head.py:L205-L231](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/smplx_head.py#L205-L231)）。推导留给 u3-l4。

**一个对实践很重要的坑**：`get_proj_matrix` 里投影矩阵被硬编码 `.to("cuda")`（[models/smplx/smplx_head.py:L73](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/smplx_head.py#L73)）。因此**head 的 forward 必须在 CUDA 上跑**；纯 CPU 会在 `get_full_proj` 里因设备不匹配报错。这也是三个入口都先 `ehm_model.cuda()` 的底层原因之一。

**输入侧的 `to_tensor`**（[utils/pipeline_utils.py:L62-L88](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/pipeline_utils.py#L62-L88)）：它只做「numpy/tensor/list → 指定设备的 tensor」的类型统一，**不做** HWC→CHW、**不做** /255 缩放。所以入口脚本要自己补这两步（[inference_wo_detect.py:L80-L85](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L80-L85)）：

```python
resized = pad_and_resize(img, target_size=256)
img_patch = to_tensor(resized, 'cuda:0')
img_patch = torch.permute(img_patch/255, (2,0,1)).unsqueeze(0)   # → (1,3,256,256)
outputs = ehm_model(img_patch)
```

app.py 中的对应写法见 [app.py:L381-L385](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L381-L385)。

#### 4.2.4 代码实践

**实践：分步复现 forward，亲眼验证每一步形状**。

1. 实践目标：不调用 `ehm_model(...)`，而是手动执行 forward 的三步，确认形状链条与 4.2.2 的表一致。
2. 操作步骤（示例代码，需 CUDA 环境；无 GPU 时第 3 步的 head 部分受上文 `.to("cuda")` 限制，只跑前两步亦可）：

```python
# 示例代码：step_by_step.py
import torch
from models.pipeline.ehm_pipeline import Ehm_Pipeline
from utils.general_utils import ConfigDict, add_extra_cfgs

meta_cfg = add_extra_cfgs(ConfigDict(model_config_path='configs/infer.yaml'))
model = Ehm_Pipeline(meta_cfg).cuda().eval()

x = torch.rand(1, 3, 256, 256).cuda()          # 模拟 [0,1] 输入
with torch.no_grad():
    x1 = model.normalize(x);  print('normalize 后:', x1.shape)        # (1,3,256,256)
    x2 = x1[:, :, :, 32:-32]; print('裁剪后:', x2.shape)              # (1,3,256,192)
    feats = model.backbone(x2); print('backbone 输出:', feats.shape)  # (1,1280,16,12)
    out = model.head(feats)
    print('pd_cam:', out['pd_cam'].shape)                              # (1,4,4)
    print('body_pose:', out['body_param']['body_pose'].shape)          # (1,21,3,3)
    print('shape_params:', out['flame_param']['shape_params'].shape)   # (1,300)
```

3. 需要观察的现象：各形状与本讲表格逐项对应；再用 `torch.rand(1,3,256,256)` **不做裁剪**直接喂 `model.backbone`，观察报错信息。
4. 预期结果：不裁剪时在 `x + self.pos_embed[:, 1:]` 处出现形状不匹配错误（`192` 对 `256`），这就从实验上坐实了「裁剪是硬约束」。具体报错文本**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`pad_and_resize` 把一张 400×300（宽×高）的横图 letterbox 成 256×256 后，模型内部裁掉的是内容还是黑边？

> 答案：横图（宽>高）时 letterbox 的黑边在**上下**两侧，而裁剪固定裁**左右**各 32 列，所以裁的是**内容**（且裁掉后画面中心 192 列保留）。对竖长人像图则近似裁掉黑边。这是该预处理对宽幅图像不友好的原因，也是 u2-l3 用检测框 + 仿射裁剪生成 patch 的动机之一。

**练习 2**：head 里 `smplx_joint_decoder = nn.Linear(dim, 165)`（55×3）存在，为什么输出表里 `joints_offset` 是 `None`？

> 答案：[models/smplx/smplx_head.py:L298](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/smplx_head.py#L298) 写死 `body_param_dict['joints_offset'] = None`，解码器被注释停用。它仍会出现在 `state_dict` 里（有参数就会被保存/加载），但前向不用——读 checkpoint 键列表时不要因此误判模型行为。

**练习 3**：`cfg.TRAIN.batch_size` 在 infer.yaml 里是 2，而推理 batch 是 1，为什么前向不报错？

> 答案：batch_size 只用于把 head 的 `focal_length` 缓冲区 expand 成 (2,2)（[models/smplx/smplx_head.py:L159-L161](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/smplx_head.py#L159-L161)）；实际组投影矩阵时 `get_full_proj` 会取 `proj_mat[0].repeat(B,1,1)` 适配真实 batch（[models/smplx/smplx_head.py:L224-L226](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/smplx_head.py#L224-L226)）。

### 4.3 权重加载约定：pear_model.pt 的两段式 state dict

#### 4.3.1 概念说明

u1-l3 已经从模块划分角度说过「checkpoint 只含 backbone 与 head 两段，EHM_v2 无可学习参数」。本讲从**加载代码**角度把这个约定讲透：

- `pear_model.pt` 顶层是**两个键**：`'backbone'` 和 `'head'`，各是一个完整的 state dict。
- 加载是**两段式**：分别 `load_state_dict` 到 `ehm_model.backbone` 和 `ehm_model.head`，而不是加载整个 `Ehm_Pipeline`。
- 三个入口脚本用完全相同的四行代码完成这件事（复制式组装，u1-l3 的结论再次出现）。
- `strict=False` 且**丢弃返回值**：含义是「容忍键不匹配、也不打印差异」。它让加载永不失败，代价是结构不匹配时静默出错（见练习 4.1.5 第 1 题）。

为什么要两段式？因为 `Ehm_Pipeline` 上还挂着 `normalize`（无可学习参数）以及 LightningModule 的各种缓冲；而 EHM_v2、渲染器压根不在 `Ehm_Pipeline` 里。保存时按子模块分段，加载时按子模块还原，是最不容易出错的做法。

#### 4.3.2 核心流程

```text
hf_hub_download("BestWJH/PEAR_models", "pear_model.pt")   → 本地缓存路径
torch.load(path, map_location='cpu', weights_only=True)   → {'backbone': {...}, 'head': {...}}
ehm_model.backbone.load_state_dict(_state['backbone'], strict=False)
ehm_model.head.load_state_dict(_state['head'],     strict=False)
ehm_model.cuda()                                          → 推理（head 需要 CUDA，见 4.2.3）
```

`weights_only=True` 是安全选项：只允许反序列化纯张量，避免 pickle 任意代码执行的风险。

#### 4.3.3 源码精读

app.py 的加载段（[app.py:L139-L144](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L139-L144)）：

```python
ehm_basemodel = hf_hub_download(repo_id="BestWJH/PEAR_models", filename="pear_model.pt", repo_type="model")
ehm_model = Ehm_Pipeline(meta_cfg)
_state = torch.load(ehm_basemodel, map_location='cpu', weights_only=True)
ehm_model.backbone.load_state_dict(_state['backbone'], strict=False)
ehm_model.head.load_state_dict(_state['head'], strict=False)
```

inference_wo_detect.py 同款（[inference_wo_detect.py:L58-L63](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L58-L63)），inference_images.py 同款（[inference_images.py:L265-L269](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_images.py#L265-L269)）。

从 checkpoint 反推结构的一个技巧（无需加载进模型）：

```python
state = torch.load(ckpt_path, map_location='cpu', weights_only=True)
[k for k in state['backbone'] if k.startswith('blocks.0.')]   # 看 Block 内部参数名
state['head']['smplx_poses_decoder.weight'].shape             # → torch.Size([312, 1024])
```

键名就是模块树路径（`blocks.0.attn.qkv.weight`、`flame_shape_decoder.bias`……），形状就是结构。比如 `smplx_poses_decoder.weight` 的形状 `(312, 1024)` 直接印证了「解码器输入宽度 1024、输出 312 维 pose 参数」。

最后澄清 `backbone_ckpt` 字段：BACKBONE 段里的 `backbone_ckpt: "data_inputs/backbone/vitpose_backbone.pth"`（[configs/infer.yaml:L21](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/configs/infer.yaml#L21)）**不在推理链路生效**。它只被训练管线消费（[models/pipeline/pipeline.py:L561-L562](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/pipeline.py#L561-L562)）；`ViT.__init__` 虽接受这个形参但并不使用，而 [models/backbones/vit.py:L337-L353](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/backbones/vit.py#L337-L353) 的 `_init_backbone` 引用了 `self.cfg`、`self.backbone` 这些 ViT 上不存在的属性，是从别的代码库搬来的死代码。推理时 ViTPose 的预训练知识已经**烘焙在 `pear_model.pt` 里**，不需要这个文件。

#### 4.3.4 代码实践

**实践：审计 strict=False 到底忽略了什么**。

1. 实践目标：搞清 `strict=False` 在这个仓库里是否真的有键不匹配，还是仅仅「保险写法」。
2. 操作步骤：在综合实践脚本（第 5 节）的加载段后追加：

```python
# 示例代码：接在 load_state_dict 之后
miss_b, unexp_b = ehm_model.backbone.load_state_dict(state['backbone'], strict=False)
miss_h, unexp_h = ehm_model.head.load_state_dict(state['head'], strict=False)
print('backbone 缺失键:', miss_b, ' 多余键:', unexp_b)
print('head     缺失键:', miss_h, ' 多余键:', unexp_h)
```

3. 需要观察的现象：四个列表是空还是有内容；若有内容，具体是哪些键（是 buffer 类的 `init_body_pose`，还是参数类）。
4. 预期结果：**待本地验证**——这正是不可能从源码静态推断、必须实际下载 checkpoint 才能回答的问题。把结论记下来，你会对「strict=False 是保险还是掩盖」有自己的判断。

#### 4.3.5 小练习与答案

**练习 1**：为什么加载目标是 `ehm_model.backbone` / `ehm_model.head`，而不是 `ehm_model.load_state_dict(_state)`？

> 答案：`_state` 的顶层键是 `'backbone'`、`'head'`，恰好对应 `Ehm_Pipeline` 两个子模块的名字，所以理论上 `ehm_model.load_state_dict({'backbone': ..., 'head': ...})` 也能对上命名。但仓库选择分段加载，职责更清晰，也让 backbone/head 可以来自不同来源（训练管线的 backbone_ckpt 就是单独给 backbone 用的）。

**练习 2**：`pear_model.pt` 里会有 `smplx_joint_decoder` 的权重吗？会有 EHM_v2 的权重吗？

> 答案：前者会——它被构造出来且未被 `requires_grad=False`，所以随 head 一起保存（尽管 forward 不用它）；后者不会——EHM_v2 是独立于 `Ehm_Pipeline` 构造的无参数人体模型（u1-l3 结论）。

## 5. 综合实践

把三个模块串起来，完成规格指定的任务：**下载 `pear_model.pt`、按 app.py 的方式加载、对一张 `pad_and_resize` 到 256 的真实图片跑前向，打印 `body_param` / `flame_param` 各键的 shape 以及 `pd_cam` 的 4×4 矩阵**。

准备工作：u1-l2 的 `pear` 环境 + 仓库自带的 `assets/SMPLX/smpl_mean_params.npz` + 网络可达 HuggingFace + **一块 GPU**（原因见 4.2.3 末尾）。在仓库根目录创建 `inspect_ehm_output.py`（示例代码，读者自建）：

```python
# 示例代码：inspect_ehm_output.py —— 在仓库根目录运行 python inspect_ehm_output.py
import numpy as np, cv2, torch
from huggingface_hub import hf_hub_download
from models.pipeline.ehm_pipeline import Ehm_Pipeline
from utils.general_utils import ConfigDict, add_extra_cfgs
from utils.pipeline_utils import to_tensor

assert torch.cuda.is_available(), 'head 的 get_proj_matrix 硬编码 .to("cuda")，需要 GPU'

# 1) 构造模型（cfg 三段解包；head 构造会读 assets/SMPLX/smpl_mean_params.npz）
meta_cfg = add_extra_cfgs(ConfigDict(model_config_path='configs/infer.yaml'))
ehm_model = Ehm_Pipeline(meta_cfg)

# 2) 下载 + 两段式加载（与 app.py L139-L144 完全一致的约定）
ckpt = hf_hub_download(repo_id='BestWJH/PEAR_models', filename='pear_model.pt', repo_type='model')
state = torch.load(ckpt, map_location='cpu', weights_only=True)
print('checkpoint 顶层键:', list(state.keys()))          # 期望 ['backbone', 'head']
ehm_model.backbone.load_state_dict(state['backbone'], strict=False)
ehm_model.head.load_state_dict(state['head'], strict=False)
ehm_model = ehm_model.cuda().eval()

# 3) 构造输入：与 inference_wo_detect.py L80-L82 相同的三步
def pad_and_resize(img, target_size=256):
    h, w = img.shape[:2]
    scale = min(target_size / h, target_size / w)
    new_w, new_h = int(w * scale), int(h * scale)
    resized = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
    padded = np.zeros((target_size, target_size, 3), dtype=np.uint8)
    padded[(target_size - new_h)//2:(target_size - new_h)//2 + new_h,
           (target_size - new_w)//2:(target_size - new_w)//2 + new_w] = resized
    return padded

img = cv2.imread('example/images/00000.png')              # 任选一张人像
img_patch = to_tensor(pad_and_resize(img, 256), 'cuda')
img_patch = torch.permute(img_patch / 255, (2, 0, 1)).unsqueeze(0)   # (1,3,256,256) [0,1]

# 4) 前向 + 打印
with torch.no_grad():
    outputs = ehm_model(img_patch)

for k, v in outputs['body_param'].items():
    print(f'body_param[{k:16s}]', None if v is None else tuple(v.shape))
for k, v in outputs['flame_param'].items():
    print(f'flame_param[{k:20s}]', tuple(v.shape))
print('pd_cam 4x4 矩阵:\n', outputs['pd_cam'][0].detach().cpu().numpy())
```

预期输出（形状部分由源码确定；具体数值**待本地验证**）：

```text
checkpoint 顶层键: ['backbone', 'head']
body_param[global_pose     ] (1, 1, 3, 3)
body_param[body_pose       ] (1, 21, 3, 3)
body_param[left_hand_pose  ] (1, 15, 3, 3)
body_param[right_hand_pose ] (1, 15, 3, 3)
body_param[hand_scale      ] (1, 3)
body_param[head_scale      ] (1, 3)
body_param[eye_pose        ] None
body_param[jaw_pose        ] None
body_param[joints_offset   ] None
body_param[exp             ] (1, 50)
body_param[shape           ] (1, 200)
flame_param[eye_pose_params      ] (1, 6)
flame_param[pose_params          ] (1, 3)
flame_param[jaw_params           ] (1, 3)
flame_param[eyelid_params        ] (1, 2)
flame_param[expression_params    ] (1, 50)
flame_param[shape_params         ] (1, 300)
pd_cam 4x4 矩阵: [[-1,0,0,tx],[0,-1,0,ty],[0,0,1,tz],[0,0,0,1]]   # 左上 3×3 恒为 diag(-1,-1,1)
```

观察两个点：① `pd_cam` 左上 3×3 恒为 \(\mathrm{diag}(-1,-1,1)\)，真正随图片变化的只有最后一列的平移 \((t_x, t_y, t_z)\)，其中 \(t_z = 24/s\)（弱透视尺度换深度，u3-l4 展开）；② 把 `torch.no_grad()` 去掉后再跑一次并用 `outputs['body_param']['shape'].sum().backward()` 反传，可确认这条链路是可微的——这正是 PEAR 能端到端训练的基础（u5-l2）。

## 6. 本讲小结

- `Ehm_Pipeline` 是 `backbone = ViT(**cfg.BACKBONE)` + `head = SMPLXTransformerDecoderHead(cfg.HEAD, cfg.TRAIN.batch_size)` + `normalize` 的外壳；构造 head 依赖仓库自带的 `assets/SMPLX/smpl_mean_params.npz`。
- `forward` 三步：ImageNet 归一化 → `x[:,:,:,32:-32]` 把 256×256 裁成 256×192 进 ViT → head 输出参数字典；裁剪不是优化而是**硬约束**——位置编码长度 193 要求 token 序列恰为 192（16×12 网格）。
- 形状链条：`(1,3,256,256)` → 裁剪 `(1,3,256,192)` → PatchEmbed 卷积 `(1,1280,16,12)` → reshape 回特征图 → head 内 rearrange 成 192 个 context token，零 token 经 6 层 cross-attention 变成 `(1,1024)`，再由 9 个 `nn.Linear(1024→…)` 解码。
- 返回字典三个键：`body_param`（11 键，其中 3 键恒为 `None`）、`flame_param`（6 键）、`pd_cam`（(B,4,4) RT 矩阵）；docstring 写的 `(B,3)` 已过时。
- 权重约定：`pear_model.pt` 顶层只有 `'backbone'`、`'head'` 两段，三个入口用相同的四行代码以 `strict=False` 分段加载并丢弃返回值；`BACKBONE.backbone_ckpt` 只服务训练管线，推理时 ViTPose 预训练已烘焙在 `pear_model.pt` 内。
- 两个实操要点：`to_tensor` 不做通道重排和 /255，需调用方补齐；`get_proj_matrix` 硬编码 `.to("cuda")`，故前向必须有 GPU。

## 7. 下一步学习建议

本讲把 `ehm_model(img_patch)` 拆到了「接口 + 形状」这一层，接下来按数据流继续下钻：

- **u3-l1（ViT 骨干）**：进 `models/backbones/vit.py` 的 Block 内部，弄清 Attention/Mlp/DropPath 的组织与 6.3 亿级参数量的来源。
- **u3-l2（TransformerDecoder）**：本讲里「一个零 token 问 192 个图像 token」的机制细节，`DropTokenDropout` 与 `ZeroTokenDropout` 的区别。
- **u3-l3（参数解码器全家桶）**：本讲输出表里每个维度的语义、`rot6d_to_rotmat` 的实现与单测、`set_smpl_init` 的均值残差设计。
- **u3-l4（相机模型）**：`pd_cam` 从 3 维弱相机参数到 4×4 RT 的完整推导。
- 若更关心结果如何变成网格：直接跳 u4-l4（EHM_v2 融合），再回头看 u3 系列。

建议先把第 5 节的脚本跑通并保存输出，后续讲义中的实践会反复复用这份参数字典。
