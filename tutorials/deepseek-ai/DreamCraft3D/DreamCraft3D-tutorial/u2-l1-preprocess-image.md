# 输入图像预处理 preprocess_image.py

## 1. 本讲目标

DreamCraft3D 的训练输入不是随手一张照片，而是「同一张参考图派生出的三件套」：RGBA 去背景图、深度图、法向图。本讲逐段精读仓库根目录下的 [preprocess_image.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/preprocess_image.py)，学完后你应当能够：

1. 说清楚 carvekit 去背景、Omnidata 深度/法向预测、BLIP2 打标语这三步各自做什么、在代码哪一行发生。
2. 理解 `--recenter`、`--size`、`--border_ratio`、`--do_caption` 四个命令行参数的含义与副作用。
3. 解释为什么输出文件必须严格命名为 `xxx_rgba.png` / `xxx_depth.png` / `xxx_normal.png`（训练侧按文件名约定加载）。
4. 独立对一张自有图片跑通完整预处理，并检查产物质量。

## 2. 前置知识

- **RGBA 与 alpha 通道**：普通图片每个像素是 RGB 三个值；RGBA 多一个 alpha 通道表示不透明度。训练时 alpha > 0.5 的像素被视为「物体」，其余被视为「背景」，这就是后面反复出现的 mask 的来源。
- **深度图（depth map）**：每个像素一个数值，表示该处物体离相机多远。归一化后通常「越亮越近、越暗越远」（本项目采用近亮远暗的约定，见 4.4 节）。
- **法向图（normal map）**：每个像素一个三维向量（存成 RGB 三通道），表示物体表面在该点的朝向。它给几何优化提供了「表面应该朝哪边」的监督信号。
- **DPT（Dense Prediction Transformer）**：一种把 ViT（视觉 Transformer）中间层特征逐级上采样融合、输出逐像素预测的网络结构，常用于深度/法向估计。Omnidata 模型正是基于 DPT 实现的。
- **为什么需要这三件套**：回忆第一讲的分层生成思想——粗阶段的 NeRF/NeuS 除了靠扩散模型「想象」，还必须紧紧贴住参考视角的真实观测。RGB 监督外观、深度监督整体形状、法向监督表面细节，三者共同压制扩散先验带来的多面（Janus）与漂移问题。
- 环境方面请先完成上一讲（u1-l2）：`carvekit` 与 `gdown` **不在 [requirements.txt](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/requirements.txt) 里**，需要额外安装；两个 Omnidata 权重必须放到 `load/omnidata/` 下（目录需自建）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [preprocess_image.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/preprocess_image.py) | 预处理主脚本：`BackgroundRemoval`、`BLIP2`、`DPT` 三个工具类 + `preprocess_single_image` 主流程 + 命令行入口 |
| [threestudio/utils/dpt.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/dpt.py) | Omnidata 用的 DPT 网络定义（`DPTDepthModel`），复制自 stable-dreamfusion |
| [threestudio/data/image.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/data/image.py) | 训练侧消费方：`SingleImageDataModule` 按文件名约定加载三件套 |
| [README.md](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/README.md) | Quickstart 预处理命令与 Omnidata 权重下载说明 |
| [configs/dreamcraft3d-coarse-nerf.yaml](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml) | 通过 `requires_depth` / `requires_normal` 开关决定训练时要不要加载深度/法向 |

整体数据流：

```text
输入图片 (RGB 或 RGBA)
   │
   ├─ 若已是 4 通道 ──────────► 直接当 carved_image 用（跳过去背景）
   ├─ 否则 carvekit HiInterface ► carved_image [H,W,4]
   │                                    │
   │                                    └─ mask = alpha > 0
   ├─ Omnidata(DPT) ◄── 原始 RGB 图（含背景）
   │      ├─ depth  [H,W] ─ mask 内 min-max 归一化，背景置 0
   │      └─ normal [H,W,3] ─ 背景置 0
   ├─ (可选) --recenter：裁剪包围盒 → 等比缩放 → 居中贴到 size×size 画布
   ├─ (可选) --do_caption：BLIP2 生成描述文本
   ▼
xxx_rgba.png / xxx_depth.png / xxx_normal.png / xxx_caption.txt
   ▼
训练时 data.image_path 指向 xxx_rgba.png，另两件按文件名约定被找到
```

## 4. 核心概念与源码讲解

### 4.1 预处理流水线总览与命令行入口

#### 4.1.1 概念说明

`preprocess_image.py` 是一个独立于 threestudio 训练框架之外的脚本：它不注册任何组件、不读 yaml，就是「读图 → 产出三件套」的一次性工具。理解它只需要抓住两点：输出文件名怎么定、什么时候会跳过处理。

#### 4.1.2 核心流程

1. 解析五个命令行参数：`path`（图片或目录）、`--size`（默认 1024）、`--border_ratio`（默认 0.1）、`--recenter`、`--do_caption`。
2. 若 `path` 是目录，则递归遍历所有文件，过滤掉已经以 `_rgba.png` / `_depth.png` / `_normal.png` 结尾的旧产物，只处理其余 `.png`。
3. 对每张图调用 `preprocess_single_image`。
4. 输出文件写在**输入图所在目录**，命名规则为 `basename.split('.')[0] + '_rgba.png'` 等。

#### 4.1.3 源码精读

命令行定义在 [preprocess_image.py:L207-L216](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/preprocess_image.py#L207-L216)：`--recenter` 的 help 文本本身写着 "potentially not helpful for multiview zero123"，作者提示重居中裁剪会改变构图，可能与 Zero123 的参考视角条件不完全匹配——这是一个官方记录在案的经验取舍。

输出路径与跳过逻辑在 [preprocess_image.py:L116-L129](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/preprocess_image.py#L116-L129)：这段代码用 `os.path.basename(img_path).split('.')[0]` 取文件名主干。注意 `split('.')[0]` 是在**第一个点**处截断，若你的图片叫 `photo.v2.png`，产物会变成 `photo_rgba.png` 而不是 `photo.v2_rgba.png`。另外只要三件套已齐全就整体跳过，因此重跑脚本是安全且幂等的。

目录批量模式在 [preprocess_image.py:L218-L229](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/preprocess_image.py#L218-L229)：两行列表过滤先把旧产物剔除、再只保留 `.png`，随后逐张处理。`load/images/` 目录里现成的示例（如 `mushroom_log_rgba.png`、`groot_rgba.png`）正是这套命名规则的实例。

一个诚实提醒：四份训练配置的默认值都写着 `./load/images/hamburger_rgba.png`（见 [configs/dreamcraft3d-coarse-nerf.yaml:L8](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L8)），但该文件**并未随仓库发布**（README 的 Todo 里明确写着测试图像数据待发布），`load/images/` 里实际存在的是 mushroom_log、groot、jay-basket、puffin 等。动手实验时请以实际存在的文件为准，或用你自己的图片。

#### 4.1.4 代码实践

1. **实践目标**：不加载任何模型，先摸清命令行与批量过滤行为。
2. **操作步骤**：在仓库根目录运行 `python preprocess_image.py --help`，把五个参数的默认值抄下来；然后建一个临时目录 `/tmp/imgs`，放入两个空文件 `foo_rgba.png` 和 `bar.png`（用 `touch` 即可）。
3. **需要观察的现象**：`--help` 正常打印；若真的对目录跑脚本（需要 GPU 与权重，本步可选），控制台应只处理 `bar.png`。
4. **预期结果**：`foo_rgba.png` 被第一层过滤剔除（以 `_rgba.png` 结尾）；即使叫 `foo_depth.jpg` 也会被第二层过滤剔除（不是 `.png`）。`--help` 路径已可验证；目录模式行为为代码推断，完整运行「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么脚本要特意跳过以 `_rgba.png` 结尾的文件？
**答案**：因为脚本自身的产物就叫这个后缀。若不过滤，对目录二次运行时会把上一次的输出再当输入处理一遍，陷入「处理自己的产物」的循环。

**练习 2**：`python preprocess_image.py pics/`（不带 `--recenter`）输出图的分辨率由什么决定？
**答案**：由输入图自身决定。不带 `--recenter` 时 `final_rgba = carved_image` 原样输出（见 4.4 节对应分支），`--size` 只在重居中分支里作为画布尺寸使用。

### 4.2 背景去除：carvekit HiInterface

#### 4.2.1 概念说明

DreamCraft3D 生成的是单个物体，没有地面、没有背景布。去背景（background removal / image matting）把物体从照片里「抠」出来，产出带 alpha 通道的 RGBA 图，alpha 同时定义了后面所有监督信号的作用范围。这里用的是 carvekit 的 `HiInterface` 高层 API——它内部串联了分割网络（Tracer/U2Net）与 matting 精修网络。

#### 4.2.2 核心流程

```text
RGB ndarray [H,W,3] → 转 PIL.Image → HiInterface([image]) → 返回 RGBA PIL.Image → 转回 ndarray [H,W,4]
```

若输入图片本身已是 4 通道（BGRA），脚本直接把它当作抠好的图使用，**完全跳过** carvekit。

#### 4.2.3 源码精读

`BackgroundRemoval` 的构造与调用在 [preprocess_image.py:L15-L40](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/preprocess_image.py#L15-L40)。构造参数里值得留意：`object_type="object"`（注释提示毛发类物体应换 `"hairs-like"`）、`seg_mask_size=640`（注释说明 Tracer B7 用 640、U2Net 用 320）、`fp16=True` 半精度推理；`__call__` 上挂了 `@torch.no_grad()`，纯推理不建计算图。

分支判断在 [preprocess_image.py:L131-L145](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/preprocess_image.py#L131-L145)：`cv2.imread` 用 `IMREAD_UNCHANGED` 读原始通道数，OpenCV 默认通道序是 BGR，所以两条路径都做了到 RGB/RGBA 的转换。随后 `mask = carved_image[..., -1] > 0` 取 alpha 通道生成布尔 mask——注意阈值是 `> 0` 而非 `> 0.5`，训练侧 `threestudio/data/image.py` 里则用 `> 0.5`（见 [threestudio/data/image.py:L191-L193](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/data/image.py#L191-L193)），两处阈值不一致是阅读时容易忽略的细节。

#### 4.2.4 代码实践

1. **实践目标**：验证「4 通道输入跳过 carvekit」分支。
2. **操作步骤**：把 `load/images/groot_rgba.png` 复制为 `/tmp/test4c.png`，运行 `python preprocess_image.py /tmp/test4c.png`（不加 `--recenter`），观察控制台输出行。
3. **需要观察的现象**：是否出现 `[INFO] background removal...` 字样。
4. **预期结果**：不出现——因为输入是 4 通道图，走了跳过分支，脚本只做深度/法向预测。完整运行需 Omnidata 权重就位，「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么深度估计喂给模型的是含背景的 `image` 而不是抠完的图？
**答案**：见 [preprocess_image.py:L150](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/preprocess_image.py#L150) 与 [preprocess_image.py:L159](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/preprocess_image.py#L159)：传入的是原始 RGB `image`。Omnidata 在完整场景数据上训练，看完整图反而估计更稳；背景区域的预测结果随后用 mask 整块置 0，不会进入监督。

**练习 2**：抠图结果 alpha 是软边缘（0~255 渐变），mask 阈值 `> 0` 会带来什么？
**答案**：边缘半透明像素全部计入物体，mask 略微「膨胀」。好处是深度归一化时不丢边缘像素；代价是若抠图边缘有残留噪声，也会被当作物体。

### 4.3 Omnidata 深度/法向预测：DPT 包装与网络结构

#### 4.3.1 概念说明

`DPT` 类（在 preprocess_image.py 中）是对 Omnidata 预训练权重的轻量包装：按任务选择权重路径与预处理，加载 `threestudio.utils.dpt.DPTDepthModel` 并推理。真正的网络定义在 `threestudio/utils/dpt.py`，这是从 stable-dreamfusion 复制来的 Omnidata DPT 实现（README 第 95 行注明出处）。

#### 4.3.2 核心流程

两条任务的差异：

| 任务 | 权重文件 | 预处理 | 输出头通道 |
| --- | --- | --- | --- |
| depth | `load/omnidata/omnidata_dpt_depth_v2.ckpt` | Resize 384² → ToTensor → Normalize(mean=0.5, std=0.5) | 1 |
| normal | `load/omnidata/omnidata_dpt_normal_v2.ckpt` | Resize 384² → ToTensor（不 Normalize） | 3 |

网络本体是「ViT 编码器 + 逐级 refine 融合」：从 backbone 的 4 个中间层取特征，各自 1×1 卷积对齐通道后，自底向上（coarse→fine）经 4 个 RefineNet 融合并逐级 2 倍上采样，最后由输出头卷积到 1 或 3 通道。

#### 4.3.3 源码精读

任务分支与权重路径在 [preprocess_image.py:L60-L84](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/preprocess_image.py#L60-L84)：路径是**硬编码的相对路径**（`load/omnidata/...`），因此脚本必须在仓库根目录运行，且目录必须自建——放错位置会直接 `FileNotFoundError`。normal 分支多一个 `num_channels=3`，同时不做 Normalize（Omnidata 法向模型训练时输入就是 [0,1] 未标准化图像）。

权重加载与前缀剥离在 [preprocess_image.py:L86-L94](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/preprocess_image.py#L86-L94)：若 checkpoint 里带 `state_dict` 键，则对每个参数键做 `k[6:]` 切片——切掉的正是 Lightning 保存时加的 `model.` 六字符前缀。

推理与分辨率还原在 [preprocess_image.py:L97-L114](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/preprocess_image.py#L97-L114)：模型在 384×384 上前向，输出 `clamp(0,1)` 后用 `F.interpolate(..., mode='bicubic')` 放回原图 H×W。depth 分支 `[1,H,W]` 经 `unsqueeze(1)` 变 `[1,1,H,W]` 再插值；normal 本身是 `[1,3,384,384]` 直接插值。

网络组装入口 `_make_encoder` 在 [threestudio/utils/dpt.py:L511-L546](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/dpt.py#L511-L546)：`backbone="vitb_rn50_384"` 分支（L519-L528）经 `_make_pretrained_vitb_rn50_384`（[threestudio/utils/dpt.py:L496-L509](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/dpt.py#L496-L509)）用 timm 创建「ResNet50 前段 + ViT-Base 后段」的混合 backbone。

DPT 前向的融合主干在 [threestudio/utils/dpt.py:L884-L902](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/dpt.py#L884-L902)：四层特征先过 `layerN_rn` 对齐通道，再按 `refinenet4 → 3 → 2 → 1` 的顺序逐级融合（每一级把上一级结果与本级 skip 特征相加后上采样 2 倍），最后过输出头。

输出头定义在 [threestudio/utils/dpt.py:L904-L918](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/dpt.py#L904-L918)：`DPTDepthModel` 的 head 是「Conv → 2 倍上采样 → Conv → ReLU → 1×1 Conv 到 num_channels → ReLU（non_negative=True 时）」，最后的 ReLU 保证深度非负。`forward` 末尾的 `squeeze(dim=1)`（[threestudio/utils/dpt.py:L923-L924](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/dpt.py#L923-L924)）对 depth 的 `[1,1,H,W]` 生效，对 normal 的 `[1,3,H,W]` 无操作——所以包装层里两个分支的维度处理不同。

另外两个支持灵活输入尺寸的组件：位置编码插值 [threestudio/utils/dpt.py:L118-L132](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/dpt.py#L118-L132) 与可变分辨率前向 [threestudio/utils/dpt.py:L135-L171](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/dpt.py#L135-L171)（`forward_flex` 被动态注入 ViT 实例）。当前调用链固定 384×384，用不到这份灵活性，但读代码时不要被它们迷惑。

权重下载方式见 [README.md:L95-L100](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/README.md#L95-L100)：`cd load/omnidata` 后用两条 `gdown` 命令分别取 depth 与 normal 两个 ckpt。

#### 4.3.4 代码实践

1. **实践目标**：亲眼确认 `k[6:]` 剥离的前缀是什么。
2. **操作步骤**：权重就位后，在仓库根目录运行下面这段 10 行检查脚本（示例代码，可存为 `/tmp/check_ckpt.py`）：

   ```python
   # 示例代码：检查 omnidata checkpoint 的键名前缀
   import torch
   ckpt = torch.load('load/omnidata/omnidata_dpt_depth_v2.ckpt', map_location='cpu')
   sd = ckpt['state_dict'] if 'state_dict' in ckpt else ckpt
   keys = list(sd.keys())
   print(keys[:3])                  # 观察原始前缀
   print([k[6:] for k in keys[:3]]) # 观察剥离 6 个字符后的结果
   print(len(keys), 'params in total')
   ```

3. **需要观察的现象**：第一行打印的键以 `model.` 开头；第二行不再有该前缀。
4. **预期结果**：与 [preprocess_image.py:L87-L91](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/preprocess_image.py#L87-L91) 的逻辑对上——`len('model.') == 6`。若权重未下载则无法运行，「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 depth 预处理做 `Normalize(mean=0.5, std=0.5)` 而 normal 不做？
**答案**：两个 Omnidata 模型训练时的输入约定不同——depth 模型按 ImageNet 风格的 [-1,1] 标准化输入训练，normal 模型按 [0,1] 原始尺度输入训练。预处理必须复现各自训练时的分布，代码里分别写在 [preprocess_image.py:L71-L75](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/preprocess_image.py#L71-L75) 与 [preprocess_image.py:L80-L83](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/preprocess_image.py#L80-L83)。

**练习 2**：脚本先 `del dpt_depth_model` 再建 normal 模型（[preprocess_image.py:L154-L162](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/preprocess_image.py#L154-L162)），为什么？
**答案**：两个 DPT 模型各自占数 GB 显存，串行使用后立刻释放，避免同时驻留两张卡也吃不下的显存；对单卡小显存用户尤其必要。

### 4.4 后处理与重居中：归一化、--recenter 与 BLIP2 标语

#### 4.4.1 概念说明

DPT 的原始输出不能直接用：深度是任意尺度的相对值，需要归一化到 [0,1] 才能存成 8 位 png；物体可能只占画面一角，`--recenter` 把它裁出来放大居中，让训练时的参考视角构图统一。BLIP2 则是可选的自动打标语，帮你省掉手写 prompt。

#### 4.4.2 核心流程

深度归一化只在 mask 内做 min-max：

\[ d_{\text{norm}} = \frac{d - d_{\min}}{d_{\max} - d_{\min} + \epsilon}, \quad \epsilon = 10^{-9} \]

其中 \( d_{\min}, d_{\max} \) 只统计物体像素。归一化后物体内部近处 ≈ 1（亮）、远处 ≈ 0（暗），背景直接置 0。

重居中的几何：设 mask 的行范围 \([x_{\min}, x_{\max}]\)、列范围 \([y_{\min}, y_{\max}]\)，则

\[ h = x_{\max} - x_{\min},\ w = y_{\max} - y_{\min},\ s = \frac{\text{size} \times (1 - r)}{\max(h, w)} \]

即先把物体等比缩放到画布去掉 \( r \)（border_ratio）比例边框后能容纳的最大尺寸，再居中贴入 `size × size` 的黑底画布。

#### 4.4.3 源码精读

深度归一化三连在 [preprocess_image.py:L147-L154](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/preprocess_image.py#L147-L154)：`depth[mask] = ...` 这类花式索引赋值只改物体像素；`depth[~mask] = 0` 清背景；最后乘 255 转	uint8。分母加 `1e-9` 防止物体是纯平面（max==min）时除零。

法向后处理在 [preprocess_image.py:L156-L162](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/preprocess_image.py#L156-L162)：`(normal * 255).astype(np.uint8).transpose(1, 2, 0)` 把 `[3,H,W]` 通道在前转为 `[H,W,3]` 便于存图，背景同样置 0。

重居中主干在 [preprocess_image.py:L164-L186](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/preprocess_image.py#L164-L186)：注意 `np.nonzero(mask)` 返回的是 (行坐标数组, 列坐标数组)，所以代码里的 `x` 对应高度方向、`y` 对应宽度方向——与日常「x 是宽、y 是高」的习惯相反，是本段最易读错的点。三张图（rgba/depth/normal）用同一套坐标、同一 `cv2.INTER_AREA`（缩小场景的抗锯齿插值）同步缩放贴布，保证像素级对齐。不开启 `--recenter` 时则原样输出（[preprocess_image.py:L188-L191](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/preprocess_image.py#L188-L191)）。

写盘在 [preprocess_image.py:L193-L196](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/preprocess_image.py#L193-L196)：`cv2.imwrite` 期望 BGR 序，所以 RGBA 存盘前又做了一次 `COLOR_RGBA2BGRA`；depth 是单通道二维数组直接写。

BLIP2 标语在 [preprocess_image.py:L198-L204](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/preprocess_image.py#L198-L204)，模型定义在 [preprocess_image.py:L42-L57](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/preprocess_image.py#L42-L57)：用的是 HuggingFace 的 `Salesforce/blip2-opt-2.7b`，`max_new_tokens=20` 限制描述长度，半精度推理。代码注释直言 "it's too slow... use your brain instead"——默认不开，人工写 prompt 通常更好（`load/images/mushroom_log_caption.txt` 的内容就是一句 "a brightly colored mushroom growing on a log"）。

顺带一提：文件头 [preprocess_image.py:L1-L13](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/preprocess_image.py#L1-L13) 里 `matplotlib` 被 import 但全文件未使用，读代码时可以直接忽略。

#### 4.4.4 代码实践

1. **实践目标**：用纯 numpy 复现重居中的几何计算，理解 border_ratio 的作用。
2. **操作步骤**（示例代码，无需 GPU）：

   ```python
   # 示例代码：手算 recenter 的缩放与贴布位置
   import numpy as np
   size, border_ratio = 1024, 0.1
   h, w = 300, 700            # 假设 mask 包围盒 300 行 × 700 列
   desired = int(size * (1 - border_ratio))
   scale = desired / max(h, w)
   h2, w2 = int(h * scale), int(w * scale)
   x2_min = (size - h2) // 2
   y2_min = (size - w2) // 2
   print(f"贴布区域：行[{x2_min}, {x2_min+h2}), 列[{y2_min}, {y2_min+w2})")
   ```

3. **需要观察的现象**：输出的贴布区域是否居中、宽边是否等于 desired。
4. **预期结果**：`desired = 921`，`scale = 921/700 ≈ 1.3157`，`w2 = 921`、`h2 = 394`，行起点 `(1024-394)//2 = 315`、列起点 `(1024-921)//2 = 51`——宽边贴满、上下留黑边。可与 [preprocess_image.py:L176-L186](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/preprocess_image.py#L176-L186) 逐步对照验证。

#### 4.4.5 小练习与答案

**练习 1**：`--border_ratio 0.1` 换成 `0.3`，输出构图会怎么变？
**答案**：`desired_size = size × 0.7`，物体缩得更小，四周黑边更宽。border_ratio 就是刻意保留的边框占比，防止物体顶到画布边缘。

**练习 2**：为什么 rgba/depth/normal 三张图必须用同一组 `x_min/x_max/y_min/y_max` 和同一插值方式同步处理？
**答案**：三者是同一视角下同一物体的三种观测，训练时按像素一一对应做 loss（RGB loss、深度 loss、法向 loss）。若各自独立裁剪或插值方式不同，像素级对齐被破坏，监督信号就错位了。

**练习 3**：深度图里「亮」代表近还是远？
**答案**：代表近。min-max 归一化把最小深度（离相机最近）映射到 1（最亮）、最大深度映射到 0，所以物体最靠近相机的部分在 depth png 里最亮。

### 4.5 产物如何进入训练：文件名约定与 requires_* 开关

#### 4.5.1 概念说明

预处理脚本与训练框架之间**没有显式接口**，只靠一条文件名约定耦合：训练配置里的 `data.image_path` 指向 `xxx_rgba.png`，深度图和法向图通过字符串替换 `_rgba.png → _depth.png / _normal.png` 推导出来。理解这条约定，就能解释为什么改名会导致训练直接 assert 失败。

#### 4.5.2 核心流程

```text
yaml: data.image_path = .../xxx_rgba.png
      data.requires_depth / data.requires_normal = true/false
        │
        ▼
SingleImageDataBase.load_images()
  rgba   = imread(image_path)            # 必须存在
  depth  = imread(image_path.replace("_rgba.png", "_depth.png"))    # 仅当 requires_depth
  normal = imread(image_path.replace("_rgba.png", "_normal.png"))   # 仅当 requires_normal
        │
        ▼
batch: rgb / mask / ref_depth / ref_normal
```

#### 4.5.3 源码精读

RGBA 与 mask 加载在 [threestudio/data/image.py:L173-L196](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/data/image.py#L173-L196)：按配置的 height/width 用 `INTER_AREA` 缩放，RGB 除以 255 归一化，mask 取 `rgba[..., 3:] > 0.5`。

深度按需加载在 [threestudio/data/image.py:L199-L215](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/data/image.py#L199-L215)：`image_path.replace("_rgba.png", "_depth.png")` 就是那条约定；文件不存在时 `assert os.path.exists(depth_path)` 直接报错。法向同构，见 [threestudio/data/image.py:L217-L234](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/data/image.py#L217-L234)。

开关来自配置：coarse-nerf 里 [configs/dreamcraft3d-coarse-nerf.yaml:L16-L17](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L16-L17) 写着 `requires_depth: true`、`requires_normal: ${cmaxgt0:${system.loss.lambda_normal}}`（用 resolver 由 loss 权重是否大于 0 自动推导）；而 texture 阶段两个开关都是 false（[configs/dreamcraft3d-texture.yaml:L15-L16](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml#L15-L16)），因为纹理阶段几何已冻结，不再需要几何监督。加载结果最终进入训练 batch 的 `ref_depth` / `ref_normal` 键（[threestudio/data/image.py:L259-L281](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/data/image.py#L259-L281)）。

README 中对应的 Quickstart 命令在 [README.md:L102-L106](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/README.md#L102-L106)。

#### 4.5.4 代码实践

1. **实践目标**：体会在不跑训练的前提下验证文件名约定。
2. **操作步骤**（示例代码，无需 GPU）：

   ```python
   # 示例代码：模拟 load_images 的路径推导
   image_path = "./load/images/mushroom_log_rgba.png"
   print(image_path.replace("_rgba.png", "_depth.png"))
   print(image_path.replace("_rgba.png", "_normal.png"))
   ```

   然后核对 `load/images/` 目录下 `mushroom_log_depth.png` 与 `mushroom_log_normal.png` 是否真实存在（前面 ls 已确认存在）。

3. **需要观察的现象**：打印路径与目录内文件名逐字符一致。
4. **预期结果**：一致。再想一想：若你把预处理产物改名为 `mushroom_depth_map.png`，coarse-nerf 训练会在 setup 阶段因 assert 失败——这就是「不能随意改名」的原因。

#### 4.5.5 小练习与答案

**练习 1**：为什么 texture 阶段把 `requires_depth` / `requires_normal` 设为 false？
**答案**：texture 阶段 `fix_geometry: true`，几何已从上一阶段检查点固定，深度/法向监督只作用于几何，此阶段没有意义；关掉开关还省去加载与缩放开销。

**练习 2**：coarse-nerf 的 `requires_normal` 为什么不直接写 true，而写 `${cmaxgt0:${system.loss.lambda_normal}}`？
**答案**：这是一个 OmegaConf resolver（u2-l2 会详细讲）：当法向 loss 权重大于 0 时才加载法向图。好处是改一个 loss 权重，数据加载行为自动联动，避免「权重为 0 却还在加载文件」的不一致。

## 5. 综合实践

完整走一遍「自有图片 → 可训练三件套」：

1. 准备一张背景较干净的物体照片（例如手机随手拍的小玩偶），放到 `/tmp/my_obj.png`。
2. 确认上一讲的环境就绪：`carvekit`、`gdown` 已安装，且两个 Omnidata 权重位于 `load/omnidata/omnidata_dpt_depth_v2.ckpt` 与 `load/omnidata/omnidata_dpt_normal_v2.ckpt`（路径硬编码，务必在仓库根目录运行）。
3. 运行官方命令：

   ```sh
   python preprocess_image.py /tmp/my_obj.png --recenter
   ```

4. 在 `/tmp/` 下检查 `my_obj_rgba.png`、`my_obj_depth.png`、`my_obj_normal.png` 三件产物：
   - rgba：物体居中、四周黑边宽度约为边长 10%（border_ratio 默认 0.1）；
   - depth：物体区域内部「近亮远暗」、背景纯黑；
   - normal：物体区域呈彩色紫蓝基调（法向映射）、背景纯黑。
5. 与仓库自带示例对比：本讲义规格中原本建议对照 `load/images/hamburger_rgba.png`，但该文件未随仓库发布（README Todo 注明测试数据待发布），请改用实际存在的 `load/images/mushroom_log_rgba.png`（连同 `_depth.png` / `_normal.png`）作为效果基准，比较抠图边缘、深度渐变与法向配色是否量级一致。
6. （可选，进阶）把产物拷入 `load/images/`，用 `data.image_path=load/images/my_obj_rgba.png` 覆盖配置（命令行覆盖语法见 u1-l4），跑几十步 coarse-nerf 观察 `[INFO] single image dataset: load image ...` 与 `load depth ...` 的日志是否出现——这能直接验证 4.5 节的加载链路。
7. 本实践需要 GPU 与预训练权重，具体渲染效果「待本地验证」。

## 6. 本讲小结

- `preprocess_image.py` 是独立于训练框架的一次性脚本，产出 `xxx_rgba.png` / `xxx_depth.png` / `xxx_normal.png`（可选 `xxx_caption.txt`）三件套，写回输入图所在目录，且幂等可重跑。
- 去背景用 carvekit `HiInterface`（`object_type="object"`、分割 640、matting 2048、fp16）；输入本身是 4 通道图时会跳过抠图直接复用 alpha。
- 深度/法向来自 Omnidata 的 DPT 模型（`vitb_rn50_384` 混合 backbone + 四级 RefineNet 融合），权重路径硬编码为 `load/omnidata/`，必须从仓库根目录运行。
- 后处理三要点：深度只在 mask 内做 min-max 归一化（近亮远暗、背景置 0）；`--recenter` 按 mask 包围盒等比缩放并居中贴入 `size×size` 画布（保留 border_ratio 边框）；三张图同步裁剪保持像素对齐。
- 训练侧只认文件名约定：`image_path.replace("_rgba.png", "_depth.png"/"_normal.png")`，是否加载由 `requires_depth` / `requires_normal` 开关（常由 `cmaxgt0` resolver 从 loss 权重推导）决定。
- 配置默认的 `hamburger_rgba.png` 未随仓库发布，实验请用 `load/images/` 下实际存在的示例（如 `mushroom_log`）或自有图片。

## 7. 下一步学习建议

预处理产物就位后，下一讲（u2-l2 配置系统：OmegaConf、resolvers 与命令行覆盖）将解释本讲已经露过两脸的机制：`data.image_path=...` 这种点号覆盖是如何生效的、`cmaxgt0` 这类自定义 resolver 在 [threestudio/utils/config.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/config.py) 里如何注册。随后 u2-l3 会把四份阶段配置并排对比，你将看到本讲的三件套在不同阶段分别被哪些 loss 消费。想提前深入训练侧数据管线的读者，也可以先跳读 [threestudio/data/image.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/data/image.py)（u4-l2 的主角）。
