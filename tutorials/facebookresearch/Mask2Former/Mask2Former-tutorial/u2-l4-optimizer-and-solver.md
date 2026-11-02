# 优化器构建与学习率策略（u2-l4）

## 1. 本讲目标

学完本讲，你应该能够：

1. 读懂 `Trainer.build_optimizer` 中「按模块名给参数分组」的完整规则表，并能解释为什么 backbone 用更小的学习率、Swin 位置参数不做 weight decay、norm 层和 Embedding 层各用各的衰减系数。
2. 理解 `maybe_add_full_model_gradient_clipping` 用「动态生成优化器子类」实现全模型梯度裁剪的技巧，以及它和 detectron2 原生裁剪的分工。
3. 说出 ADAMW + poly 学习率调度在 Mask2Former 各配置中的组合方式，并能动手画出学习率曲线。

本讲只聚焦 `train_net.py` 中两个被 detectron2 引擎回调的类方法：`build_optimizer` 与 `build_lr_scheduler`。它们是 [train_net.py:175-262](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L175-L262) 中连续的两段代码，前者决定「每个参数怎么更新」，后者决定「学习率随时间怎么变」。

## 2. 前置知识

### 2.1 参数组（param group）：一个优化器，多种超参

PyTorch 的优化器（如 `torch.optim.AdamW`）允许把参数拆成多个「组」，每组携带自己的 `lr`、`weight_decay` 等超参。构造时传入的是一个字典列表：

```python
optimizer = torch.optim.AdamW([
    {"params": backbone_params, "lr": 1e-5, "weight_decay": 0.05},
    {"params": head_params,     "lr": 1e-4, "weight_decay": 0.05},
])
```

为什么需要分组？因为不同模块的「训练阶段」不同：backbone 用 ImageNet 预训练权重初始化，已经很好，只想被轻微微调；而解码器是随机初始化的，需要更大的步长。Mask2Former 的 `build_optimizer` 本质上就是一个「参数组工厂」，把模型的每个参数塞进一个（通常只含单个参数的）组里。

### 2.2 AdamW 与解耦 weight decay

AdamW 是 Adam 的变体，把 weight decay 从梯度更新中「解耦」出来：衰减项不经过自适应学习率缩放，直接按 \( \theta \leftarrow \theta - \eta \, \lambda \theta \) 施加。Transformer 类模型普遍搭配 AdamW + 较小 decay（Mask2Former 取 0.05），这是从 DETR / ViT 一脉相承的惯例。GETTING_STARTED 里还特别提醒：由于用 ADAMW，「如何按 batch size 缩放学习率并不明确」，单卡训练需要自己调 `SOLVER.BASE_LR`（见 [GETTING_STARTED.md:45-52](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/GETTING_STARTED.md#L45-L52)，这段话解释了为什么仓库只给出 8 卡配置）。

### 2.3 梯度裁剪：为什么 Transformer 训练离不开它

深层注意力网络的反向传播容易出现个别维度上的超大梯度，一步就把参数推飞（loss 突然变成 NaN）。梯度裁剪在每次 `optimizer.step()` 之前检查全模型梯度的 L2 范数 \( \|g\|_2 \)，若超过阈值 \( \tau \) 就整体缩放：

\[
g \leftarrow g \cdot \frac{\tau}{\max(\tau, \; \|g\|_2)}
\]

「全模型」三个字是关键：PyTorch 自带的 `torch.nn.utils.clip_grad_norm_` 天然支持把所有参数拼在一起算范数，但 detectron2 内置的裁剪是**逐参数**的——这正是 Mask2Former 要自己写一个包装类的动机（见 4.2）。

### 2.4 学习率调度：warmup 与 poly

- **warmup（预热）**：训练最初若干 iteration 用很小的学习率线性升温，避免随机初始化的模块在第一步就被大梯度破坏。
- **poly 调度**：DeepLab 系的经典做法，学习率随迭代数按幂函数衰减到 0：

\[
\text{lr}(t) = \text{lr}_0 \cdot \left(1 - \frac{t}{T}\right)^{\gamma}, \quad \gamma \approx 0.9
\]

其中 \( T \) 是总迭代数 `SOLVER.MAX_ITER`。相比 multi-step（阶梯式）衰减，poly 曲线平滑，分割任务上经验效果更好。

### 2.5 与前几讲的衔接

u2-l1 已经讲过：`Trainer` 继承 detectron2 的 `DefaultTrainer`，只覆写 `build_train_loader`、`build_optimizer`、`build_lr_scheduler`、`build_evaluator` 四个类方法，它们由引擎在构造期回调。本讲就是把这四个方法中的两个「求解器」方法彻底拆开。所有配置键的来源遵循 u1-l5 讲过的五层叠加规则：`get_cfg()` 默认值 → `add_deeplab_config` → `add_maskformer2_config` → yaml → 命令行。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [train_net.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L175-L262) | 本讲主战场：`build_lr_scheduler`（L175-181）、`build_optimizer`（L183-262），以及两个外部函数的 import（L45-46） |
| [mask2former/config.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/config.py#L22-L27) | `add_maskformer2_config` 注册的 solver 键默认值：`WEIGHT_DECAY_EMBED`、`OPTIMIZER="ADAMW"`、`BACKBONE_MULTIPLIER=0.1` |
| [configs/coco/panoptic-segmentation/Base-COCO-PanopticSegmentation.yaml](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/panoptic-segmentation/Base-COCO-PanopticSegmentation.yaml#L19-L35) | COCO 系训练的实际 SOLVER 取值（BASE_LR、WEIGHT_DECAY、CLIP_GRADIENTS 等） |
| [configs/ade20k/panoptic-segmentation/Base-ADE20K-PanopticSegmentation.yaml](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/ade20k/panoptic-segmentation/Base-ADE20K-PanopticSegmentation.yaml#L19-L35) | ADE20K 系显式启用 `WarmupPolyLR` 的范例 |
| [mask2former/modeling/backbone/swin.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/backbone/swin.py#L105-L108) | `relative_position_bias_table` / `absolute_pos_embed` 这两个「免衰减」参数的定义处 |
| [mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L314-L322) | `query_feat` / `query_embed` / `level_embed` 三个 `nn.Embedding` 的定义处（命中 Embedding 衰减规则） |
| detectron2（外部依赖） | `detectron2.projects.deeplab.build_lr_scheduler` 与 `detectron2.solver.build.maybe_add_gradient_clipping`，由 [train_net.py:45-46](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L45-L46) 导入；其内部行号不在本仓库，标「待确认」处需在自己的 detectron2 安装目录里核对 |

## 4. 核心概念与源码讲解

### 4.1 build_optimizer：按模块名分组的参数组工厂

#### 4.1.1 概念说明

`build_optimizer` 解决的问题是：**给模型里每一类参数挑合适的 lr 和 weight decay**。它一次遍历模型的所有参数，根据「参数所在模块的名字」和「参数所在模块的类型」套用四条规则，最终产出一个参数组列表交给 AdamW。

四条规则（按源码顺序，后命中者覆盖先命中者）：

| 规则 | 判断依据 | 效果 | 动机 |
| --- | --- | --- | --- |
| ① backbone 降 lr | 模块路径名包含 `"backbone"` | `lr *= BACKBONE_MULTIPLIER`（默认 0.1） | backbone 有 ImageNet 预训练，只需轻微微调 |
| ② 位置参数免衰减 | 参数名含 `relative_position_bias_table` 或 `absolute_pos_embed` | `weight_decay = 0` | 位置偏置是「坐标查表」性质的参数，衰减它等于系统性削弱位置信息（Swin 论文惯例） |
| ③ norm 层特殊衰减 | 模块是 `nn.LayerNorm` / `nn.GroupNorm` / 各种 BN / InstanceNorm | `weight_decay = WEIGHT_DECAY_NORM` | norm 层的仿射参数量少、作用关键，通常单独设 0 |
| ④ Embedding 特殊衰减 | 模块是 `nn.Embedding` | `weight_decay = WEIGHT_DECAY_EMBED`（默认 0） | query/level 嵌入表与位置编码同理，不衰减 |

还有两个前置过滤：`requires_grad=False` 的参数直接跳过（冻结参数不进优化器）；用 `memo` 集合去重，避免同一个参数（如共享权重）因出现在多个模块路径下被重复加入。

#### 4.1.2 核心流程

```text
build_optimizer(cfg, model):
    defaults = {lr: BASE_LR, weight_decay: WEIGHT_DECAY}
    params, memo = [], set()
    for (module_name, module) in model.named_modules():        # 逐模块
        for (param_name, value) in module.named_parameters(recurse=False):  # 只看直属参数
            if not value.requires_grad:  continue              # 过滤冻结参数
            if value in memo:           continue               # 去重（共享参数）
            memo.add(value)
            h = copy(defaults)
            if "backbone" in module_name:                      # 规则①
                h.lr = h.lr * BACKBONE_MULTIPLIER
            if "relative_position_bias_table" in param_name \
               or "absolute_pos_embed" in param_name:          # 规则②
                h.weight_decay = 0.0
            if isinstance(module, NORM_TYPES): h.weight_decay = WEIGHT_DECAY_NORM   # 规则③
            if isinstance(module, nn.Embedding): h.weight_decay = WEIGHT_DECAY_EMBED # 规则④
            params.append({"params": [value], **h})            # 每个参数一组
    optimizer = (带裁剪包装的 AdamW)(params, BASE_LR)
    return optimizer
```

注意三个细节：

1. **遍历的是模块，判断的是两种名字**：`module_name` 是模块路径（如 `backbone.res2`、`sem_seg_head.pixel_decoder...`），规则①按它做子串匹配；`module_param_name` 是参数在模块内的属性名（如 `weight`、`relative_position_bias_table`），规则②按它匹配。
2. **规则可叠加**：一个参数可以同时命中①和③——比如 Swin backbone 里某个 LayerNorm 的 weight，lr 会被乘 0.1，weight_decay 会被换成 `WEIGHT_DECAY_NORM`。规则②在规则③之前执行，所以如果某个位置参数恰好长在 norm 模块上，norm 的衰减会覆盖「免衰减」——实际不会发生，因为 `relative_position_bias_table` 是 `nn.Parameter` 直接挂在 block 上（见 4.1.3）。
3. **每组只有一个参数**：`{"params": [value], ...}`，因此一个 4700 万参数的 R50 模型会产生数千个 param group。对 AdamW 来说每组独立维护状态，功能等价，只是构建开销略大。

#### 4.1.3 源码精读

**入口与默认值**（[train_net.py:183-190](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L183-L190)）：先取两个特殊衰减系数，再定基底超参——所有参数默认用 `SOLVER.BASE_LR` 与 `SOLVER.WEIGHT_DECAY`，后面按规则逐个改写。

**norm 模块类型表**（[train_net.py:192-204](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L192-L204)）：一个元组列出了所有被视为「norm 层」的 PyTorch 模块类型。注意 `NaiveSyncBatchNorm` 继承自 `BatchNorm2d`，所以无需单独列出。Mask2Former 中真正会命中这个表的是：Transformer 解码器里的 `nn.LayerNorm`（含自注意力/FFN/decoder_norm）以及 `maskformer2_R50_bs16_50ep.yaml` 中 `NORM: "GN"` 指定的 GroupNorm（见 [maskformer2_R50_bs16_50ep.yaml:12](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml#L12)，它作用于 sem_seg_head/pixel decoder）。

**遍历与四条规则**（[train_net.py:206-230](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L206-L230)）：

- L208-215：双层遍历 + 两个过滤（`requires_grad` 检查、`memo` 去重）。
- L217：`hyperparams = copy.copy(defaults)`——每个参数拿到一份默认值副本，互不干扰。
- [L218-219](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L218-L219)：规则①，backbone 学习率乘 `cfg.SOLVER.BACKBONE_MULTIPLIER`。
- [L220-225](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L220-L225)：规则②，参数名含 `relative_position_bias_table` 或 `absolute_pos_embed` 时 `weight_decay=0.0`。注意 L224 有一行裸 `print(module_param_name)`——这是作者留下的调试输出，训练 Mask2Former 时终端会先刷出一串 `relative_position_bias_table`，很多人第一次都会疑惑它的来源，就是这里。
- [L226-229](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L226-L229)：规则③④，按模块类型替换 weight_decay。
- L230：`params.append({"params": [value], **hyperparams})` 收尾。

**规则②命中的参数长什么样？** 它们只在 Swin backbone 里存在：`relative_position_bias_table` 是每个 SwinTransformerBlock 上的一个直接 `nn.Parameter`（[swin.py:105-108](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/backbone/swin.py#L105-L108)，形状 `(2W_h-1)(2W_w-1) × nH`，是窗口注意力的相对位置偏置查表）；`absolute_pos_embed` 只在 `APE=True` 时存在（[swin.py:575-578](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/backbone/swin.py#L575-L578)，而默认 `APE: False`，见 [config.py:87](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/config.py#L87) 与 [swin_tiny_bs16_50ep.yaml:10](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/panoptic-segmentation/swin/maskformer2_swin_tiny_bs16_50ep.yaml#L10)）。因为它是 `nn.Parameter` 属性而非子模块，规则④的 `isinstance(module, nn.Embedding)` 抓不到它，必须靠规则②的**名字匹配**——这就是两条规则并存的原因。

**规则④命中的参数**：Transformer 解码器里的三个嵌入表——`query_feat`（可学习 query 特征）、`query_embed`（可学习 query 位置编码）、`level_embed`（尺度嵌入），见 [mask2former_transformer_decoder.py:314-322](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L314-L322)。它们的衰减系数是 `SOLVER.WEIGHT_DECAY_EMBED`，由 [config.py:22-27](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/config.py#L22-L27) 注册，默认 `0.0`——与位置参数免衰减的动机一致：这些都是「坐标/身份查表」，不该被衰减拉向 0。

**配置侧的实际取值**（[Base-COCO-PanopticSegmentation.yaml:19-35](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/panoptic-segmentation/Base-COCO-PanopticSegmentation.yaml#L19-L35)）：`BASE_LR: 0.0001`、`WEIGHT_DECAY: 0.05`、`OPTIMIZER: "ADAMW"`、`BACKBONE_MULTIPLIER: 0.1`。对照 [config.py:24-27](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/config.py#L24-L27)（`add_maskformer2_config` 的 solver 段）可以看到：yaml 里的这三项与注册默认值相同，写出来是为了显式声明。`WEIGHT_DECAY_NORM` 不是 Mask2Former 注册的键，而是 detectron2 `get_cfg()` 自带的默认键，仓库所有 yaml 都没有覆盖它，实际取值以本地安装的 detectron2 默认为准（实践里打印确认；detectron2 默认为 0.0，待本地验证）。

**一个容易忽略的事实**：R50 配置下规则②**一次都不会命中**——ResNet 没有这两个参数。所以「Swin 位置参数免衰减」这条规则的效果只能在 Swin 配置（如 [swin/maskformer2_swin_tiny_bs16_50ep.yaml](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/panoptic-segmentation/swin/maskformer2_swin_tiny_bs16_50ep.yaml#L1-L15)，它继承 R50 配置只换 backbone）上观察到。

#### 4.1.4 代码实践

**实践目标**：把 `build_optimizer` 的参数分组逻辑复制到独立脚本，构建 R50 配置的模型，用统计结果验证三条规则；再用 Swin-tiny 配置补验规则②。

**操作步骤**：

1. 写脚本 `optimizer_groups_report.py`（示例代码，放在仓库根目录或任意可 `import mask2former` 的环境）：

   ```python
   # optimizer_groups_report.py（示例代码）
   import argparse, copy
   from collections import Counter
   from typing import Any, Dict, List, Set
   import torch
   from detectron2.config import get_cfg
   from detectron2.modeling import build_model
   from detectron2.projects.deeplab import add_deeplab_config
   import mask2former  # noqa: F401  import 即注册 MaskFormer 等
   from mask2former import add_maskformer2_config


   def build_param_groups(cfg, model):
       """逐行对应 train_net.py Trainer.build_optimizer 的分组逻辑。"""
       weight_decay_norm = cfg.SOLVER.WEIGHT_DECAY_NORM
       weight_decay_embed = cfg.SOLVER.WEIGHT_DECAY_EMBED
       defaults = {"lr": cfg.SOLVER.BASE_LR, "weight_decay": cfg.SOLVER.WEIGHT_DECAY}

       norm_module_types = (
           torch.nn.BatchNorm1d, torch.nn.BatchNorm2d, torch.nn.BatchNorm3d,
           torch.nn.SyncBatchNorm, torch.nn.GroupNorm,
           torch.nn.InstanceNorm1d, torch.nn.InstanceNorm2d, torch.nn.InstanceNorm3d,
           torch.nn.LayerNorm, torch.nn.LocalResponseNorm,
       )

       params: List[Dict[str, Any]] = []
       memo: Set[torch.nn.parameter.Parameter] = set()
       n_skipped = 0
       for module_name, module in model.named_modules():
           for module_param_name, value in module.named_parameters(recurse=False):
               if not value.requires_grad:
                   n_skipped += 1
                   continue
               if value in memo:
                   continue
               memo.add(value)
               h = copy.copy(defaults)
               if "backbone" in module_name:
                   h["lr"] = h["lr"] * cfg.SOLVER.BACKBONE_MULTIPLIER
               if "relative_position_bias_table" in module_param_name \
                       or "absolute_pos_embed" in module_param_name:
                   h["weight_decay"] = 0.0
               if isinstance(module, norm_module_types):
                   h["weight_decay"] = weight_decay_norm
               if isinstance(module, torch.nn.Embedding):
                   h["weight_decay"] = weight_decay_embed
               params.append({"params": [value], "name": f"{module_name}.{module_param_name}", **h})
       return params, n_skipped


   def report(cfg_path):
       cfg = get_cfg()
       add_deeplab_config(cfg)
       add_maskformer2_config(cfg)
       cfg.merge_from_file(cfg_path)
       cfg.MODEL.DEVICE = "cpu"
       cfg.freeze()
       model = build_model(cfg)
       groups, n_skipped = build_param_groups(cfg, model)

       print(f"config = {cfg_path}")
       print(f"BASE_LR={cfg.SOLVER.BASE_LR} BACKBONE_MULTIPLIER={cfg.SOLVER.BACKBONE_MULTIPLIER} "
             f"WEIGHT_DECAY={cfg.SOLVER.WEIGHT_DECAY} WEIGHT_DECAY_NORM={cfg.SOLVER.WEIGHT_DECAY_NORM} "
             f"WEIGHT_DECAY_EMBED={cfg.SOLVER.WEIGHT_DECAY_EMBED}")
       print(f"requires_grad=False 被跳过的参数个数: {n_skipped}")

       # 按 (lr, weight_decay) 统计每组参数量
       stat = Counter()
       for g in groups:
           stat[(g["lr"], g["weight_decay"])] += g["params"][0].numel()
       for (lr, wd), numel in sorted(stat.items()):
           print(f"lr={lr:.2e}  weight_decay={wd}  参数量={numel/1e6:.2f}M")

       # 抽样打印规则命中的代表
       for key in ("relative_position_bias_table", "absolute_pos_embed"):
           hits = [g for g in groups if key in g["name"]]
           print(f"规则②命中 {key}: {len(hits)} 个, 示例 wd={hits[0]['weight_decay'] if hits else 'N/A'}")
       for g in groups:
           if isinstance(g["params"][0], torch.nn.Parameter) and "decoder_norm" in g["name"]:
               print(f"规则③示例: {g['name']}  wd={g['weight_decay']}")
               break
       emb = [g for g in groups if "query_embed" in g["name"] or "level_embed" in g["name"]]
       if emb:
           print(f"规则④示例: {emb[0]['name']}  wd={emb[0]['weight_decay']}")


   if __name__ == "__main__":
       parser = argparse.ArgumentParser()
       parser.add_argument("--config-file", required=True)
       report(parser.parse_args().config_file)
   ```

2. 跑 R50 配置：

   ```bash
   python optimizer_groups_report.py \
     --config-file configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml
   ```

3. 再跑 Swin-tiny 配置（同一脚本换参数即可，其 SOLVER 与 R50 完全一致，只换了 backbone）：

   ```bash
   python optimizer_groups_report.py \
     --config-file configs/coco/panoptic-segmentation/swin/maskformer2_swin_tiny_bs16_50ep.yaml
   ```

**需要观察的现象**：

- 打印出的 `BASE_LR=0.0001`、`BACKBONE_MULTIPLIER=0.1`、`WEIGHT_DECAY=0.05`（来自 Base yaml L21/26/28）。
- R50 输出中统计表应出现两档 lr：`1.00e-04`（非 backbone）与 `1.00e-05`（backbone，即 1e-4 × 0.1）。
- R50 输出中 `规则②命中 relative_position_bias_table: 0 个`——证明这条规则只服务 Swin。
- Swin-tiny 输出中该行变成非 0（Swin-tiny 有 4 个 stage × 各 stage block 数个 bias table），且示例 `wd=0.0`。
- `规则③示例 decoder_norm.weight` 与 `规则④示例 query_embed.weight / level_embed.weight` 的 wd 值。

**预期结果**（具体数字待本地验证）：

1. backbone 组 `lr = BASE_LR × 0.1 = 1e-5`，其余组 `1e-4` —— 规则①得证。
2. Swin 配置下 `relative_position_bias_table` 的 `weight_decay = 0.0`（R50 下无命中）—— 规则②得证；R50 下还应看到终端没有刷出任何 bias table 名字（源码 L224 的 print 无输出）。
3. norm 层（GroupNorm / LayerNorm）的 `weight_decay = WEIGHT_DECAY_NORM`（打印其值并与普通 conv/linear 层的 0.05 对比，两者不同即证明规则③生效）。
4. 额外收获：R50 的 backbone BN 参数不在任何组里——Base yaml 中 `NORM: "SyncBN"` 被注释掉（[Base-COCO-PanopticSegmentation.yaml:14](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/panoptic-segmentation/Base-COCO-PanopticSegmentation.yaml#L14)），ResNet 走 detectron2 默认的 FrozenBN，其仿射参数 `requires_grad=False`，在 L210-211 就被跳过，计入 `n_skipped`（待本地验证）。

如果本地无法运行（例如没装 detectron2），本实践退化为「源码阅读型」：对照 4.1.2 的伪代码在纸面上为 R50 模型的每个模块分类，写出预期分组表。

#### 4.1.5 小练习与答案

**练习 1**：如果把配置里 `SOLVER.BACKBONE_MULTIPLIER` 改成 1.0，参数组会发生什么变化？这等价于什么训练策略？

答案：所有参数组的 lr 都等于 `BASE_LR`，backbone 与头部用同一学习率训练（即「不降 backbone lr」的端到端微调）。规则①仍在执行，只是乘数变成 1，数值上不再有区分。

**练习 2**：`module_name` 是子串匹配。假设你自定义了一个模块命名为 `my_backbone_extra`，会发生什么？

答案：它的所有参数 lr 也会被乘 `BACKBONE_MULTIPLIER`，因为 `"backbone" in "my_backbone_extra"` 为 True。这是子串匹配的副作用，给新模块命名时要避开 `backbone` 这个词（或有意利用它）。

**练习 3**：为什么 `query_embed`（规则④，nn.Embedding）和 `relative_position_bias_table`（规则②，nn.Parameter）需要两套判断，而不是统一用一条规则？

答案：因为它们在模块树里的形态不同——`nn.Embedding` 是一个子模块，`isinstance` 能识别；而 `relative_position_bias_table` 是直接挂在 block 上的裸 `nn.Parameter`，不是模块，`isinstance(module, ...)` 抓不到，只能按参数名匹配。两条规则各管一种形态。

### 4.2 maybe_add_full_model_gradient_clipping：动态子类化实现全局梯度裁剪

#### 4.2.1 概念说明

梯度裁剪有两种粒度：

- **逐参数裁剪**（detectron2 原生 `maybe_add_gradient_clipping` 支持）：每个参数单独算范数单独缩放。优点是可与 per-parameter clip value 精细配合，缺点是「整体方向感」差——一个参数的超大梯度不会牵动其他参数一起缩。
- **全模型裁剪**（full model clipping）：把所有参数的梯度拼在一起算一个全局 L2 范数，超过阈值整体缩放。DETR 系 Transformer 训练的标准做法是这一种。

[train_net.py:233](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L232-L233) 的注释直说了动机："detectron2 doesn't have full model gradient clipping now"。所以 Mask2Former 自己实现了一个，并且实现方式很巧：**不改训练循环，而是动态生成一个优化器子类，重写 `step()`**。

另一个背景是 AMP：Base 配置都开了混合精度（`AMP.ENABLED: True`，[Base-COCO-PanopticSegmentation.yaml:34-35](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/panoptic-segmentation/Base-COCO-PanopticSegmentation.yaml#L34-L35)），fp16 数值范围小，梯度更容易溢出，全局裁剪 + 梯度缩放是标配组合。

#### 4.2.2 核心流程

```text
maybe_add_full_model_gradient_clipping(optim_class):
    if CLIP_GRADIENTS.ENABLED
       and CLIP_TYPE == "full_model"
       and CLIP_VALUE > 0:
        class FullModelGradientClippingOptimizer(optim_class):   # 动态子类化
            def step(closure=None):
                all_params = chain(所有 param_groups 的所有参数)
                clip_grad_norm_(all_params, CLIP_VALUE)   # 先全局裁剪
                super().step(closure)                     # 再正常更新
        return FullModelGradientClippingOptimizer
    else:
        return optim_class    # 原样返回，不加包装
```

调用侧的写法是 `maybe_add_full_model_gradient_clipping(torch.optim.AdamW)(params, BASE_LR)`——传入的是**类**（不是实例），返回的也是类，随后立刻实例化。这是一个「类装饰器」。

#### 4.2.3 源码精读

**包装函数本体**（[train_net.py:232-247](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L232-L247)）：

- L234-239：三个条件同时满足才启用——`CLIP_GRADIENTS.ENABLED`、`CLIP_TYPE == "full_model"`、`CLIP_VALUE > 0.0`。任何一个不满足就原样返回优化器类。
- L241-245：在函数体内 `class FullModelGradientClippingOptimizer(optim)` 定义子类——Python 允许在函数里定义类，且基类可以是运行时传入的变量，于是同一个包装函数对 SGD 和 AdamW 都适用。`step()` 先用 `itertools.chain` 把所有 param group 的参数扁平化（这正是 4.1 里「每组一个参数」的结构在这里的代价与便利：chain 拼接即可），交给 `torch.nn.utils.clip_grad_norm_` 做全局 L2 裁剪，再调用 `super().step()` 执行真正的 AdamW 更新。
- L247：启用则返回子类，否则返回原类。

**调用点与优化器分发**（[train_net.py:249-262](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L249-L262)）：

- L250-253：`OPTIMIZER == "SGD"` 分支（带 momentum），仓库配置不用它，但保留了对 detectron2 旧项目的兼容。
- L254-257：`OPTIMIZER == "ADAMW"` 分支——所有 Base yaml 都写 `OPTIMIZER: "ADAMW"`，这是实际走的路径：`maybe_add_full_model_gradient_clipping(torch.optim.AdamW)(params, cfg.SOLVER.BASE_LR)`。
- L258-259：其它名字直接抛 `NotImplementedError`。
- [L260-261](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L260-L261)：若 `CLIP_TYPE` **不是** `full_model`，则退回 detectron2 原生的 `maybe_add_gradient_clipping`（从 [train_net.py:46](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L45-L46) 导入，实现位于 detectron2 的 `solver/build.py`，外部依赖，行号待确认）。注意仓库内所有 Base yaml 的裁剪配置都是同一套（`ENABLED: True` / `CLIP_TYPE: "full_model"` / `CLIP_VALUE: 0.01` / `NORM_TYPE: 2.0`，如 [Base-COCO-PanopticSegmentation.yaml:29-33](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/panoptic-segmentation/Base-COCO-PanopticSegmentation.yaml#L29-L33)，可用 grep 验证），所以 L260-261 这条回退路径在实际配置下从不执行，只是兼容出口。

一个值得注意的小细节：配置里有 `NORM_TYPE: 2.0`，但本仓库的 `step()` 直接调用 `clip_grad_norm_`（固定 L2 范数），并没有读这个键——`NORM_TYPE` 只对 detectron2 原生裁剪路径有意义。

**为什么「包装类」比「改训练循环」好？** 因为 detectron2 的 `DefaultTrainer` 拿到优化器后会在每个 iteration 结束时自动调 `optimizer.step()`。只要 `step()` 本身带上了裁剪逻辑，训练循环一行都不用改——这是插件式项目「不侵入宿主框架」的典型手法（呼应 u1-l3 的架构主题）。

#### 4.2.4 代码实践

**实践目标**：亲眼看到「全局裁剪」生效——构造一个梯度范数远超阈值的参数，验证 step 前后梯度被缩放、参数更新量被限制。

**操作步骤**：

1. 写脚本 `clip_demo.py`（示例代码，只需 PyTorch，不需 detectron2）：

   ```python
   # clip_demo.py（示例代码）
   import itertools
   import torch

   CLIP_VALUE = 0.01  # 与 Base-COCO-PanopticSegmentation.yaml 相同

   class FullModelGradientClippingAdamW(torch.optim.AdamW):
       def step(self, closure=None):
           all_params = itertools.chain(*[x["params"] for x in self.param_groups])
           torch.nn.utils.clip_grad_norm_(all_params, CLIP_VALUE)
           super().step(closure=closure)

   w = torch.nn.Parameter(torch.tensor([1000.0]))   # 梯度将远超阈值
   opt = FullModelGradientClippingAdamW([w], lr=0.1)

   w.grad = torch.tensor([100.0])
   grad_norm_before = w.grad.norm().item()
   opt.step()                                       # step 内部先裁剪再更新
   print(f"裁剪前梯度范数: {grad_norm_before:.4f}")  # 100.0
   print(f"裁剪后参数值:  {w.item():.6f}")            # 期望 ≈ 999.9 量级
   ```

2. 运行 `python clip_demo.py`，把 `CLIP_VALUE` 改成 `1e4`（大于梯度范数）再跑一次。

**需要观察的现象**：第一次运行时梯度范数 100 远大于 0.01，裁剪把有效梯度缩到范数 0.01；第二次不触发裁剪。对比两次的 `w` 更新幅度。

**预期结果**：`CLIP_VALUE=0.01` 时更新量明显被压小（有效梯度仅 ~0.01/100 = 1e-4 量级），`CLIP_VALUE=1e4` 时更新量由原始梯度 100 主导（待本地验证具体数值；若数值与直觉不符，检查 AdamW 自适应分母对第一步更新的影响——第一步更新约为 lr，这也是很好的追问点）。

如果本地没有可运行环境，本实践退化为：在 [train_net.py:241-245](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L241-L245) 上逐行写注释，并回答「为什么 `all_params` 要用 `chain` 展开所有 param_groups 而不是只取第一组」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `FullModelGradientClippingOptimizer` 要定义成 `maybe_add_full_model_gradient_clipping` 的内嵌类，而不是模块级的固定类？

答案：因为它的基类 `optim` 是运行时参数（可能是 SGD 也可能是 AdamW），Python 的类定义在函数体内才能以变量作基类，实现「一次包装、多种优化器通用」。

**练习 2**：`torch.nn.utils.clip_grad_norm_` 裁剪的是梯度还是参数？裁剪发生在 `super().step()` 之前意味着什么？

答案：裁剪的是 `.grad`（梯度张量本身被原地缩放），发生在参数更新之前——所以传给 AdamW 更新公式的是缩放后的梯度；对 Adam 类优化器还要注意梯度会先进动量累计，裁剪影响的是进入动量的值。

**练习 3**：如果把某配置改成 `CLIP_TYPE: "value"`，训练行为会怎样变化？

答案：L235-239 的 `enable` 为 False，返回原始 AdamW 类（不带裁剪）；随后 L260-261 条件成立，改用 detectron2 原生 `maybe_add_gradient_clipping(cfg, optimizer)` 包装——从全模型裁剪切换成 detectron2 支持的裁剪方式（具体行为在其 `solver/build.py` 中实现，外部依赖，待确认）。

### 4.3 build_lr_scheduler 与 poly 学习率调度

#### 4.3.1 概念说明

`build_lr_scheduler` 决定学习率如何随 iteration 变化。Mask2Former 没有自己写调度器，而是直接复用 detectron2 **DeepLab 工程**的构建函数（[train_net.py:45](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L45) 导入的 `detectron2.projects.deeplab.build_lr_scheduler`），原因是它提供了 `WarmupPolyLR`——把 warmup 与 poly 衰减拼接的调度器：

\[
\text{lr}(t) = \text{lr}_0 \cdot \underbrace{\min\left(1,\; \frac{t}{T_w} \cdot \frac{1}{f_w}\right)}_{\text{warmup 因子（前 } T_w \text{ 步）}} \cdot \underbrace{\left(1 - \frac{t}{T}\right)^{\gamma}}_{\text{poly 因子}}, \qquad \gamma \approx 0.9
\]

其中 \( T_w \) 是 `SOLVER.WARMUP_ITERS`、\( f_w \) 与 `SOLVER.WARMUP_FACTOR` 相关、\( T \) 是 `SOLVER.MAX_ITER`。公式给出的是 poly 调度的标准形态；detectron2 deeplab 工程里的具体实现（含幂指数默认值、warmup 拼接细节）位于 `detectron2/projects/deeplab/build_solver.py`，属外部依赖，行号与精确默认值待本地确认（本仓库 yaml 未覆盖幂指数键）。

仓库内两类 Base 配置给出了两种组合方式：

| 配置族 | 调度器 | 关键键 | 行为 |
| --- | --- | --- | --- |
| ADE20K / Cityscapes / Mapillary 系 | 显式 `LR_SCHEDULER_NAME: "WarmupPolyLR"`（如 [Base-ADE20K...yaml:27](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/ade20k/panoptic-segmentation/Base-ADE20K-PanopticSegmentation.yaml#L19-L35)） | `MAX_ITER: 160000`、`WARMUP_ITERS: 0`、无 `STEPS` | 纯 poly：从 1e-4 平滑衰减到 0 |
| COCO 系 | yaml 未写调度器名 | `STEPS: (327778, 355092)`、`MAX_ITER: 368750`、`WARMUP_ITERS: 10`、`WARMUP_FACTOR: 1.0`（[Base-COCO...yaml:19-35](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/panoptic-segmentation/Base-COCO-PanopticSegmentation.yaml#L19-L35)） | 调度器名取 detectron2 默认键（实践第 1 步打印确认），`STEPS` 表明按 multi-step 阶梯衰减设计 |

注意 COCO 的 `WARMUP_FACTOR: 1.0` + `WARMUP_ITERS: 10` 实际上让 warmup 近似形同虚设（初始因子就是 1）。

#### 4.3.2 核心流程

```text
Trainer.build_lr_scheduler(cfg, optimizer):
    return detectron2.projects.deeplab.build_lr_scheduler(cfg, optimizer)
        ├─ 若 SOLVER.LR_SCHEDULER_NAME == "WarmupPolyLR":
        │      构造 warmup + poly 衰减调度器（实现于 deeplab 工程）
        └─ 否则: 回退 detectron2 原生 build_lr_scheduler
                 （支持 WarmupMultiStepLR 等，由 STEPS/MAX_ITER 驱动）
```

调度器与优化器的关系：调度器并不「拥有」学习率，它在每个 iteration 末尾（`scheduler.step()`）改写优化器各 param_group 的 `lr` 字段。与 4.1 联动后，backbone 组的 lr 恒为其它组的 0.1 倍——poly 因子对所有组同乘。

#### 4.3.3 源码精读

**Trainer 的覆写**（[train_net.py:175-181](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L175-L181)）：只是把 detectron2 默认实现替换为 deeplab 版本——docstring 写明 "It now calls detectron2.solver.build_lr_scheduler. Overwrite it if you'd like a different scheduler"（实际上调的是 deeplab 的同名函数，它内部再决定是否回退原生实现）。这个覆写是 `WarmupPolyLR` 能被识别的唯一前提。

**为什么 import 的是 deeplab？** [train_net.py:285-287](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L281-L288) 的 `setup()` 里有一行注释 "# for poly lr schedule"——`add_deeplab_config(cfg)` 除注册语义分割相关键外，还带来 poly 调度所需的支持；`build_lr_scheduler` 与之配套。两个 import 都来自 `detectron2.projects.deeplab`（[train_net.py:45](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L45)），这是一条「把别的工程的求解器借来用」的依赖线。

**配置证据**：

- ADE20K Base 的 SOLVER 段（[Base-ADE20K-PanopticSegmentation.yaml:19-35](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/ade20k/panoptic-segmentation/Base-ADE20K-PanopticSegmentation.yaml#L19-L35)）：`LR_SCHEDULER_NAME: "WarmupPolyLR"` + `MAX_ITER: 160000`，没有 `STEPS`——典型的「按迭代总数的 poly 衰减」配方。
- COCO Base 的 SOLVER 段（[Base-COCO-PanopticSegmentation.yaml:19-35](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/panoptic-segmentation/Base-COCO-PanopticSegmentation.yaml#L19-L35)）：给了 `STEPS: (327778, 355092)`（约在 89%、96% 进度处降 lr）与 `MAX_ITER: 368750`（50ep），没有写调度器名——所以 COCO 配置实际用哪个调度器取决于合并后 `cfg.SOLVER.LR_SCHEDULER_NAME` 的值（detectron2 默认键，实践第 1 步验证）。

**与优化器的组装顺序**（回顾 u2-l1）：detectron2 引擎先 `build_optimizer`（参数分组 + 裁剪包装），再 `build_lr_scheduler(cfg, optimizer)`（拿到优化器引用以便改写各组 lr）。二者都只在 Trainer 构造期调用一次。

#### 4.3.4 代码实践

**实践目标**：确认两个配置族实际使用的学习率调度器类型，并画出 ADE20K 的 poly 学习率曲线。

**操作步骤**：

1. 写脚本 `lr_curve.py`（示例代码）：

   ```python
   # lr_curve.py（示例代码）
   import argparse
   import torch
   from detectron2.config import get_cfg
   from detectron2.projects.deeplab import add_deeplab_config, build_lr_scheduler
   import mask2former  # noqa: F401
   from mask2former import add_maskformer2_config

   parser = argparse.ArgumentParser()
   parser.add_argument("--config-file", required=True)
   args = parser.parse_args()

   cfg = get_cfg()
   add_deeplab_config(cfg)
   add_maskformer2_config(cfg)
   cfg.merge_from_file(args.config_file)
   cfg.freeze()
   print(f"LR_SCHEDULER_NAME = {cfg.SOLVER.LR_SCHEDULER_NAME}")
   print(f"BASE_LR={cfg.SOLVER.BASE_LR}  MAX_ITER={cfg.SOLVER.MAX_ITER}  "
         f"STEPS={cfg.SOLVER.STEPS}  WARMUP_ITERS={cfg.SOLVER.WARMUP_ITERS}")

   w = torch.nn.Parameter(torch.zeros(1))
   optimizer = torch.optim.AdamW([w], lr=cfg.SOLVER.BASE_LR)
   scheduler = build_lr_scheduler(cfg, optimizer)

   pts = []
   step = max(1, cfg.SOLVER.MAX_ITER // 160)   # 采样 ~160 个点
   for it in range(cfg.SOLVER.MAX_ITER):
       if it % step == 0:
           pts.append((it, optimizer.param_groups[0]["lr"]))
       optimizer.step()
       scheduler.step()
   for it, lr in pts[::4]:
       print(f"iter {it:>7d}   lr {lr:.3e}")
   ```

2. 跑 ADE20K 与 COCO 各一次：

   ```bash
   python lr_curve.py --config-file configs/ade20k/panoptic-segmentation/maskformer2_R50_bs16_160k.yaml
   python lr_curve.py --config-file configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml
   ```

**需要观察的现象**：两个配置打印出的 `LR_SCHEDULER_NAME`；ADE20K 的 lr 序列是否从 1e-4 单调平滑衰减到接近 0；COCO 的 lr 序列是否在 `STEPS` 附近出现阶梯（若是 multi-step）或平滑曲线（若也是 poly）。

**预期结果**（待本地验证）：

- ADE20K：`WarmupPolyLR`，曲线满足 \(\text{lr}(t) = 10^{-4}(1 - t/160000)^{\gamma}\)——在 t=80k 处约为初始值的一半附近（\(0.5^{0.9}\approx0.54\)）。
- COCO：调度器名与曲线形态由 detectron2 默认值决定；若为 multi-step，则在前 89% 迭代保持 1e-4，在 327778 和 355092 处两次下跳。把两种曲线画在同一张图上，即可直观对比「poly vs 阶梯」两种衰减哲学。

若本地无法完整跑 16 万步循环，可把循环上限改成 `cfg.SOLVER.MAX_ITER // 100` 并同步把 `MAX_ITER` 缩小 100 倍后再构建调度器，观察曲线形状即可。

#### 4.3.5 小练习与答案

**练习 1**：为什么 ADE20K 配置不需要 `STEPS`，而 COCO 配置需要？

答案：poly 调度只需要总迭代数 `MAX_ITER`，学习率按公式连续衰减，没有「阶梯时刻」；multi-step 调度则必须显式给出每次降 lr 的迭代位置 `STEPS`。两族配置的 SOLVER 段差异直接反映了所用调度器的参数需求。

**练习 2**：`build_lr_scheduler(cfg, optimizer)` 为什么要传 optimizer？

答案：PyTorch 的学习率调度器通过修改优化器 `param_groups` 里各组的 `lr` 生效，构造时必须持有优化器引用；这也解释了 4.1 中分组设置的初始 lr 会被调度器随时间统一改写（各组按相同因子缩放，backbone 组始终保持 0.1 倍关系）。

**练习 3**：COCO Base 里 `WARMUP_FACTOR: 1.0`、`WARMUP_ITERS: 10` 的组合意味着什么？

答案：warmup 阶段极短且初始因子已是 1.0，即学习率从第一步起就是 `BASE_LR`，warmup 基本不生效。这是 COCO 大数据集长训练下「不需要小心预热」的工程选择；对比 MaskFormer 一代/其它小数据配置会更依赖 warmup。

## 5. 综合实践

**任务：产出一份《Mask2Former R50/Swin-tiny 参数分组审计报告》。**

把 4.1.4 的脚本扩展成完整审计：

1. 对 R50 与 Swin-tiny 两个 COCO 配置各跑一次参数分组统计。
2. 报告须包含四张小表：
   - 表 A：`(lr, weight_decay)` 组合 → 参数量占比（两种 backbone 各一列）；
   - 表 B：规则②命中的参数清单（模块路径 + 形状 + wd）；
   - 表 C：规则③④命中的代表参数（`decoder_norm.weight`、`query_embed.weight`、`level_embed.weight`）与其 wd 来源键；
   - 表 D：`requires_grad=False` 被跳过的参数（验证 FrozenBN 结论）。
3. 用一句话回答：从 R50 换到 Swin-tiny，优化器层面（而非模型层面）唯一的行为差异是什么？
4. 再用 4.3.4 的脚本分别记录两个配置的 lr 曲线若干采样点，附在报告末尾，并解释「backbone 组的 lr 曲线与头部组保持什么数学关系」。

参考答案（第 3、4 问）：优化器层面的唯一差异是规则②开始生效——Swin 的每个窗口注意力 block 的 `relative_position_bias_table` 进入 `weight_decay=0` 的组（R50 无此参数）；lr 曲线上，backbone 组在任意时刻都是头部组的 `BACKBONE_MULTIPLIER=0.1` 倍，因为调度器对各组施加相同缩放因子，而分组时设置的初始 lr 比例被保留（具体数值待本地验证）。

## 6. 本讲小结

- `build_optimizer` 是一个「参数组工厂」：双层遍历所有模块的直属参数，按四条规则（backbone lr × 0.1、Swin 位置参数 wd=0、norm 层用 `WEIGHT_DECAY_NORM`、Embedding 用 `WEIGHT_DECAY_EMBED`）为**每个参数单独**生成一组超参，再交给 AdamW（[train_net.py:183-230](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L183-L230)）。
- 规则②只在 Swin backbone 上命中：`relative_position_bias_table` 是裸 `nn.Parameter`，只能靠参数名匹配（[swin.py:105-108](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/backbone/swin.py#L105-L108)）；R50 配置下该规则从不触发，源码里那行裸 `print`（L224）就是它的「显形」时刻。
- 全模型梯度裁剪通过「函数内动态定义优化器子类、重写 `step()`」实现，先对链式拼接的全部参数做 `clip_grad_norm_` 再更新，不侵入 detectron2 训练循环（[train_net.py:232-247](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L232-L247)）；仓库所有 Base 配置都用 `full_model` + `CLIP_VALUE=0.01`，detectron2 原生逐参数裁剪只是兼容回退分支。
- `build_lr_scheduler` 直接复用 detectron2 deeplab 工程的构建函数以获得 `WarmupPolyLR`（warmup + \( (1-t/T)^{0.9} \) 衰减）；ADE20K/Cityscapes/Mapillary 系显式启用 poly，COCO 系则配 `STEPS` 阶梯衰减（实际调度器名需打印合并后配置确认）。
- 学习率策略总结成一句话：**ADAMW（BASE_LR=1e-4，backbone ×0.1）+ 按任务选择的 poly/step 衰减 + 全模型 L2 梯度裁剪 0.01 + AMP**，这一整套来自分割与检测两条社区经验线的组合。

## 7. 下一步学习建议

- 下一讲（u3-l1）进入模型本体：`MaskFormer.forward` 的训练/推理两条路径。你会看到本讲的 `weight_dict` 与损失 dict 如何在 meta arch 里汇合。
- 建议顺手阅读两个外部文件（在本地 detectron2 安装目录）：`detectron2/solver/build.py`（原生 `build_optimizer` 与 `maybe_add_gradient_clipping`，对比 Mask2Former 版改了什么）和 `detectron2/projects/deeplab/build_solver.py`（`WarmupPolyLR` 的真实实现），把本讲标注「待确认」的细节补全。
- 想深入 poly 调度的来历，可读 DeepLabv3 论文的 learning rate 一节；想深入 AdamW，可读 Decoupled Weight Decay Regularization（Loshchilov & Hutter, 2019）。
