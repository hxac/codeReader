# PEAR 是什么：像素对齐的 expressive 人体网格恢复

> 本讲是 PEAR 学习手册的第一讲。读完本讲，你应该能用自己的话说清楚：PEAR 解决什么问题、输入输出是什么、代码里哪几行定义了这一切。

## 1. 本讲目标

学完本讲，你将能够：

1. 说出 **expressive human mesh recovery（表达性人体网格恢复）** 这个任务的目标，以及 SMPL-X 与 FLAME 两个人体模型各自负责的部分。
2. 列出 PEAR 推理的输入（一个 256×256 的人体 patch）与输出（`body_param`、`flame_param`、`pd_cam` 三个关键字段，以及 EHM_v2 生成的 10475 顶点网格）。
3. 找到 README 中的论文、项目页、YouTube 演示、HuggingFace Live Demo 等资源入口。
4. 沿着「图像 → ViT → Head → EHM → Renderer」画出 PEAR 的数据流图，并能在源码中指出每一环对应的文件。

本讲**不要求你跑通任何代码**（环境搭建是下一讲的内容），所有实践都是源码阅读型的。

## 2. 前置知识

### 2.1 什么是人体网格恢复（Human Mesh Recovery, HMR）

给定一张包含人的照片，算法要回答两个问题：

- 这个人的**三维姿态**是什么？（胳膊抬到哪、腿弯到哪）
- 这个人的**体型**是什么？（高矮胖瘦）

直接回归一整个三角网格（几万个顶点的 xyz 坐标）太难了，所以主流做法是：先回归一组**低维参数**，再用一个**参数化人体模型**把参数"翻译"成网格。这个过程就像先写出"身高、臂长、关节角度"这几十个数，再让一个标准人体模板照着这些数摆出姿势。

### 2.2 SMPL-X 和 FLAME 是什么

- **SMPL-X**：一个覆盖全身（身体 + 手 + 头）的参数化人体模型。它有 10475 个顶点、55 个关节，输入一组 pose/shape 参数，输出一个完整的身体网格。
- **FLAME**：一个专门建模**头部**的参数化模型，有 5023 个顶点，参数里包括下颌开合（jaw）、眼球转动（eye）、眼睑开闭（eyelid）、表情（expression）这些 SMPL-X 头部刻画不了细节的量。
- **EHM（Expressive Human Model）**：PEAR 自己的统一表达——用 FLAME 生成的、更精细的头部网格，去**替换** SMPL-X 网格中对应的头顶点，得到一个"SMPL-X 的身体 + FLAME 的头"的混合网格。仓库里的 `EHM_v2` 类就是它的实现。

所谓 **expressive**，就是"不止身体姿态，还包括手指、脸部表情、眼睑这些细粒度表达"。

### 2.3 什么是「像素对齐（pixel-aligned）」

模型输出的 3D 网格最终要投影回原图，让人看看"估得准不准"。**像素对齐**指的是：网络在预测人体参数的同时，还预测一个相机参数，使得把 3D 关键点投影回图像平面后，能和图像里真实的人体轮廓、关节位置对得上。PEAR 名字里的 "Pixel-aligned" 强调的正是这一点——后面你会看到输出字典里的 `pd_cam` 就是干这个用的。

### 2.4 你需要的一点 PyTorch 常识

- `torch.Tensor` 的 shape 写法 `(B, C, H, W)`：Batch、Channel、高、宽。
- 字典（`dict`）是 PEAR 各模块之间传递结果的主要容器，比如 `outputs['body_param']`。
- `nn.Module.forward()` 是 PyTorch 模型的"入口函数"，`model(x)` 等价于 `model.forward(x)`。

## 3. 本讲源码地图

本讲涉及的关键文件如下（均为仓库真实路径）：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [README.md](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/README.md) | 项目门面：定位、资源入口、Quick Start | Overview 段落与三个推理/训练命令 |
| [models/pipeline/ehm_pipeline.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/ehm_pipeline.py) | 把 ViT 骨干和 SMPLX 解码头拼成可训练的 `LightningModule` | `forward()` 的输入输出 |
| [models/smplx/smplx_head.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/smplx_head.py) | 解码头：从图像特征回归全部人体参数 | `forward()` 末尾组装的返回字典 |
| [models/modules/ehm/EHM_v2.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py) | EHM 统一人体模型（FLAME 头 + SMPL-X 身体） | `forward()` 的输入参数与输出网格 |
| [app.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py) | Gradio 视频演示入口 | `mesh_inference` 中逐帧调用链 |
| [inference_wo_detect.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py) | 单人无检测图像推理入口 | 一张图从读入到渲染的最短路径 |
| [configs/infer.yaml](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/configs/infer.yaml) | 推理配置 | `BACKBONE.img_size` 等关键字段 |

一句话概括它们的关系：

```
app.py / inference_wo_detect.py   ← 入口：读图、调模型、存结果
        │
        ▼
models/pipeline/ehm_pipeline.py   ← Ehm_Pipeline：图像 patch → 参数字典
        │  (内部由 configs/infer.yaml 驱动)
        ▼
models/modules/ehm/EHM_v2.py      ← EHM_v2：参数字典 → 10475 顶点网格
        │
        ▼
models/modules/renderer/…         ← Renderer2：网格 → 可视化图像
```

## 4. 核心概念与源码讲解

本讲的三个最小模块：

1. README 概览与 Quick Start
2. `Ehm_Pipeline.forward` 的输入输出
3. 输出字典三个关键字段

---

### 4.1 README 概览与 Quick Start

#### 4.1.1 概念说明

读一个研究项目，README 永远是第零层源码。PEAR 的 README 承担三件事：

1. **定位**：一句话讲清楚 PEAR 是什么、快到什么程度。
2. **资源入口**：论文、项目页、视频、在线 Demo。
3. **Quick Start**：装环境、放资产、跑推理/训练的最短命令序列。

#### 4.1.2 核心流程

按 README 自顶向下读的顺序：

1. 看徽章（badges）拿到论文和 Demo 链接。
2. 看 Overview 确认任务定义和性能主张。
3. 看 Quick Start 的 Preparation → Inference → Training 三段，得到三个可直接复制的命令。
4. 看 Acknowledgements 了解它站在哪些工作之上。

#### 4.1.3 源码精读

**① 任务定位。** README 用一句话定义了 PEAR：

[README.md:L46-L52](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/README.md#L46-L52) 中写道：PEAR 是一个用于**实时 expressive 3D 人体网格恢复的统一框架**，是第一个能以 **100 FPS** 同时预测 EHM 参数的方法。这一句是整个仓库的"需求文档"——后面所有代码都在服务这两个关键词：*expressive*（输出含 FLAME 头部细节）和 *real-time*（100 FPS，意味着网络结构必须是一次前向、无迭代优化）。

**② 资源入口。** [README.md:L24-L27](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/README.md#L24-L27) 的四个徽章分别是：

- arXiv 论文：`https://www.arxiv.org/abs/2601.22693`
- 项目页：`https://wujh2001.github.io/PEAR//`
- YouTube 演示视频
- HuggingFace 在线 Demo：`https://huggingface.co/spaces/BestWJH/PEAR`

不想本地装环境时，直接点 HuggingFace Demo 就能体验同款功能（`app.py` 就是这个 Space 的本地版）。

**③ Quick Start 中的三个命令。** [README.md:L56-L74](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/README.md#L56-L74) 是环境准备：`conda create`、`pip install -r requirements.txt`，以及两个需要 `--no-build-isolation` 的特殊依赖 `pytorch3d` 和 `chumpy`（下一讲详述）。[README.md:L97-L111](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/README.md#L97-L111) 给出推理命令，[README.md:L115-L133](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/README.md#L115-L133) 给出训练命令。注意 README 明确写了"**All pretrained models will be downloaded automatically**"——预训练权重会在首次运行时从 HuggingFace 自动下载，你不需要手动找 checkpoint。

**④ 它和 HMR / OSX / Multi-HMR 等工作的关系。** [README.md:L149-L162](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/README.md#L149-L162) 的致谢段落列出了 PEAR 借力的工作，可以按类型分三类记忆：

| 相关工作 | 与 PEAR 的关系 |
| --- | --- |
| FLAME / SMPL-X / SMPL / MANO | 底层**参数化人体模型**，PEAR 在其上构建 EHM |
| OSX / Multi-HMR / AiOS / Harmer 等 | 同属**全身/多人网格恢复**方法，PEAR 与它们是对比与继承关系（如 OSX 也是一阶段 whole-body 恢复，Multi-HMR 关注多人单_shot_） |
| HMR2 / HSMR / SMPLest-X | 经典 **HMR 主干思路**（回归式、判别式）的源头，PEAR 的"ViT 骨干 + Transformer 解码头"沿袭了这条路线 |

一个直观的差异化定位：传统 HMR 输出 SMPL/SMPL-X 参数；expressive 方法（OSX、AiOS、PEAR）进一步输出手部和脸部的细粒度参数；PEAR 的独特之处是把 FLAME 头**直接融合进** SMPL-X 网格形成 EHM，并用一个大 ViT 一次前向完成，从而做到 100 FPS。

#### 4.1.4 代码实践

**实践目标**：把 README 的信息变成一张属于自己的「资源速查卡」。

**操作步骤**：

1. 打开 [README.md](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/README.md)，通读一遍（不用看懂安装细节）。
2. 新建一个本地笔记文件（例如 `PEAR-tutorial/notes.md`，属于你自己的学习笔记，不属于本讲义产物），抄录：
   - 论文 arXiv 编号；
   - HuggingFace Demo 地址；
   - 三条命令：视频推理、图像推理、训练。
3. 点开 HuggingFace Demo 上传任意一段单人视频，观察输出的网格视频。

**需要观察的现象**：Demo 输出的是一段**只有人体网格、没有背景**的视频，并且只处理视频的前几秒。

**预期结果**：你会看到网格随人体动作而动，手部手指和面部（下颌、眨眼）也有细节变化——这就是 "expressive" 的直观体现。

**待本地验证**：若网络受限无法访问 HuggingFace，跳过第 3 步不影响本讲学习。

#### 4.1.5 小练习与答案

**练习 1**：README 中说 PEAR "以 100 FPS 同时预测 EHM-s 参数"。请从工程角度说明这个数字对网络结构提出了什么约束？

**参考答案**：100 FPS 意味着单帧前向预算约 10 毫秒。这排除了任何"迭代优化式"（如优化拟合 SMPL 参数的多轮梯度下降）或"多阶段检测→裁剪→逐部位回归"的串行流水线，要求**单次前向**就输出全部参数。这与代码中 `Ehm_Pipeline.forward` 只有 backbone → head 两步、且 `EHM_v2` 也是一次可微前向的事实一致。

**练习 2**：如果你只想看效果、完全不想装环境，README 里哪条路径可以做到？

**参考答案**：点击 [README.md:L24-L27](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/README.md#L24-L27) 中的 HuggingFace Live Demo 徽章（`https://huggingface.co/spaces/BestWJH/PEAR`），在浏览器里即可体验，其后台跑的就是本仓库的 `app.py`。

**练习 3**：PEAR 输出的人体网格里，脸部细节（表情、下颌、眼睑）来自哪个底层模型？

**参考答案**：来自 FLAME（[README.md:L79](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/README.md#L79) 要求下载 `generic_model.pkl`），身体部分来自 SMPL-X。两者通过 `EHM_v2` 融合。

---

### 4.2 `Ehm_Pipeline.forward` 的输入输出

#### 4.2.1 概念说明

`Ehm_Pipeline` 是 PEAR 的**推理主干**：它把"看图"的 ViT 骨干和"翻译成人体参数"的解码头装配在一起，对外只暴露一个 `forward(x)`。所有入口脚本（`app.py`、`inference_wo_detect.py`、`inference_images.py`）都不直接碰 backbone 或 head，而是 `outputs = ehm_model(img_patch)` 一行拿结果。

它继承自 `lightning.LightningModule`，所以既能当普通 `nn.Module` 推理，也能被 Lightning 的训练框架调度（训练相关内容在第 u5 单元）。

#### 4.2.2 核心流程

`forward` 只有三步，伪代码如下：

```
输入 x: (B, 3, 256, 256) 的归一化前图像 patch（值域 0~1，RGB）

1. x = normalize(x)                  # 按 ImageNet 均值方差归一化
2. x = x[:, :, :, 32:-32]            # 宽度方向裁掉左右各 32 像素 → (B, 3, 256, 192)
3. feats = backbone(x)               # ViT 提特征 → (B, embed_dim, H', W')
4. outputs = head(feats)             # 解码头回归参数 → dict
return outputs
```

为什么第 2 步要裁剪？因为配置里骨干的输入尺寸是 `[256, 192]`（宽 192），而入口脚本统一产出 256×256 的正方形 patch，所以在模型内部把左右各 32 列裁掉，凑成 256×192。这一个小细节是初学者最容易被形状报错卡住的地方，值得单独记住。

数据流全景图（本讲的综合实践会让你亲手画一遍）：

```
原始图像/视频帧
   │  pad_and_resize → 256×256，等比缩放并居中补黑边
   ▼
img_patch (B,3,256,256)
   │  Ehm_Pipeline.forward
   │     ├─ normalize + 裁剪到 256×192
   │     ├─ ViT backbone（configs/infer.yaml: embed_dim=1280, depth=32）
   │     └─ SMPLXTransformerDecoderHead
   ▼
outputs = { 'body_param': …, 'flame_param': …, 'pd_cam': … }
   │  EHM_v2(body_param, flame_param)   ← 参数 → 统一网格
   ▼
pd_smplx_dict['vertices'] (B,10475,3)
   │  GS_Camera(R,T 取自 pd_cam) + Renderer2
   ▼
1024×1024 渲染图（mesh_*.jpg / mesh_video.mp4）
```

#### 4.2.3 源码精读

**① 装配：骨干 + 头。** [models/pipeline/ehm_pipeline.py:L13-L26](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/ehm_pipeline.py#L13-L26) 中，`__init__` 从配置对象 `cfg` 读出 `BACKBONE`、`HEAD`、`TRAIN` 三段，分别实例化 `ViT` 骨干、`SMPLXTransformerDecoderHead` 解码头和一个 ImageNet 风格的 `normalize` 变换。注意 `self.body_image_size = 1024` 与 `self.head_image_size = 512` 这两个字段记录的是**渲染**用的图像尺寸，而不是网络输入尺寸——网络输入尺寸在 `configs/infer.yaml` 的 `BACKBONE.img_size` 里。

**② forward 三步。** [models/pipeline/ehm_pipeline.py:L29-L52](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/ehm_pipeline.py#L29-L52) 是本模块核心。这一段真实代码（去掉了 docstring）只有四行：

- `x = self.normalize(x)` —— 用 ImageNet 均值 `mean=[0.485, 0.456, 0.406]`、方差 `std=[0.229, 0.224, 0.225]` 做标准化（见 [ehm_pipeline.py:L26](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/ehm_pipeline.py#L26)），这是几乎所有 ImageNet 预训练视觉骨干的标准约定。
- `feats = self.backbone(x[:, :, :, 32:-32])` —— 切片 `32:-32` 把宽度从 256 裁到 192，对应 [configs/infer.yaml:L14](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/configs/infer.yaml#L14) 的 `img_size : [256, 192]`。
- `outputs = self.head(feats)` —— 解码头返回参数字典，行尾注释 `# pd_params [B,46], pd_cam [B,3]` 是**过时的**（见下面的提醒）。
- `return outputs`。

> ⚠️ **一个重要的读码习惯**：[ehm_pipeline.py:L37-L44](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/ehm_pipeline.py#L37-L44) 的 docstring 写的返回键是 `'pd_cam': (B,3)`、`'pd_params'`、`'focal_length'`，但**实际代码并不是这样**。真实的返回键由 [models/smplx/smplx_head.py:L314-L319](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/smplx_head.py#L314-L319) 决定：`'pd_cam'` 是 `(B,4,4)`，另外两个键是 `'body_param'` 和 `'flame_param'`。所有入口脚本（如 [app.py:L388-L390](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L388-L390)）取的正是后者。**docstring 会过期，调用方不会说谎**——判断接口真实形状时，永远以调用处和实现处为准。

**③ 输入从哪里来。** 以 [inference_wo_detect.py:L77-L86](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L77-L86) 为例，一张图进入模型前的四步：`cv2.imread` 读图 → `pad_and_resize(img, target_size=256)` 等比缩放并居中补黑边成正方形 → `to_tensor` 转 tensor、除以 255 归到 0~1 → `permute(...).unsqueeze(0)` 变成 `(1,3,256,256)`。这段代码在 [inference_wo_detect.py:L80-L85](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L80-L85)，`app.py` 的逐帧处理在 [app.py:L378-L385](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L378-L385) 完全一致。

**④ 模型怎么被实例化和加载权重。** [app.py:L127-L146](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L127-L146) 展示了标准三件套：`ConfigDict(model_config_path="configs/infer.yaml")` 读配置 → `Ehm_Pipeline(meta_cfg)` 建模型 → `torch.load` 后分别给 `backbone` 和 `head` 加载 `pear_model.pt` 里的两段权重（`strict=False`），最后再实例化 `EHM_v2("assets/FLAME", "assets/SMPLX")`。注意权重文件是通过 `hf_hub_download(repo_id="BestWJH/PEAR_models", filename="pear_model.pt")` 自动下载的，印证了 README 的"All pretrained models will be downloaded automatically"。

#### 4.2.4 代码实践

**实践目标**：不改一行代码，用三处源码交叉验证"PEAR 的输入是 256×256 patch"这件事。

**操作步骤**：

1. 打开 [inference_wo_detect.py:L80](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L80)，确认 `pad_and_resize(img, target_size=256)`。
2. 打开 [app.py:L381](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L381)，确认视频帧同样用 `target_size=256`。
3. 打开 [configs/infer.yaml:L10-L21](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/configs/infer.yaml#L10-L21)，找到 `img_size : [256, 192]`。
4. 回到 [ehm_pipeline.py:L47](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/ehm_pipeline.py#L47)，解释 `x[:, :, :, 32:-32]` 把哪个维度从多少裁到多少。

**需要观察的现象**：三个入口/配置对输入尺寸的描述能拼成一个自洽的故事：入口产 256×256 → 模型内部裁成 256×192 → 骨干期望 `[256, 192]`。

**预期结果**：你应该能写出这样的结论——`x` 的 shape 是 `(B, C, H, W)`，切片作用在最后一维 `W` 上，把宽度 256 裁成 \(256 - 32 \times 2 = 192\)，得到 `(B, 3, 256, 192)`，与 `img_size: [256, 192]` 一致。

**待本地验证**：想实际打印形状，需要先完成下一讲的环境搭建；届时可在 `forward` 里临时加 `print(x.shape)` 验证。

#### 4.2.5 小练习与答案

**练习 1**：`Ehm_Pipeline.forward` 里为什么要把 `normalize` 定义在 `__init__` 而不是写在入口脚本里？

**参考答案**：`normalize` 是模型对输入的**约定**（取决于骨干用了哪种预训练），把它放进模型内部可以保证所有入口脚本（app.py、inference_wo_detect.py、inference_images.py）行为一致，入口只需负责把图像变成 0~1 的 tensor。这是一种"数据约定内聚到模型"的设计。

**练习 2**：如果把 `x[:, :, :, 32:-32]` 改成 `x[:, :, 32:-32, :]`，会发生什么？

**参考答案**：切片会作用在 `H` 维（高）上，得到 `(B, 3, 192, 256)`——高 192、宽 256，与骨干期望的 `[256, 192]`（高 256、宽 192）宽高颠倒，PatchEmbed 的形状对不上，前向会直接报维度错误。这个练习帮助你记住 PyTorch 图像 tensor 是 `(B, C, H, W)` 且**高在前宽在后**。

**练习 3**：入口脚本传给模型的是 `(1, 3, 256, 256)` 的 float tensor，值域是多少？进入 backbone 前又经历了什么变换？

**参考答案**：入口处除以 255，值域 0~1（见 [inference_wo_detect.py:L82](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L82)）；进入 backbone 前先被 `self.normalize` 按 ImageNet 均值/方差标准化，再被 `32:-32` 裁成 `(1, 3, 256, 192)`。

---

### 4.3 输出字典三个关键字段

#### 4.3.1 概念说明

`Ehm_Pipeline.forward` 返回的 `outputs` 是一个**嵌套字典**，真正被下游使用的是三个顶层键：

| 键 | 含义 | 形状 |
| --- | --- | --- |
| `body_param` | 身体部分参数（全局姿态、21 个身体关节、双手各 15 个关节、手/头缩放、表情、体型） | dict，含 11 个键 |
| `flame_param` | 头部（FLAME）参数（眼球、头部姿态、下颌、眼睑、表情、脸型） | dict，含 6 个键 |
| `pd_cam` | "predicted camera"，一个 4×4 的刚体变换（旋转 R + 平移 T），把 3D 网格搬进相机坐标系 | `(B, 4, 4)` |

这三个字段就是 PEAR 与外界交互的**全部输出**：`body_param` + `flame_param` 喂给 `EHM_v2` 得到网格；`pd_cam` 用来构造渲染相机，实现"像素对齐"。

#### 4.3.2 核心流程

字典的组装发生在 `SMPLXTransformerDecoderHead.forward` 的末尾，流程是：

```
ViT 特征 (B, C, H', W')
   │ rearrange 成 token 序列 (B, N, C)
   │ 一个零初始化 token 做 cross-attention 查询 → token_out (B, C)
   ▼
┌─ smplx_poses_decoder ──► 6D 旋转 → body_param 的 4 个姿态键
├─ smplx_scale_decoder ───► body_param 的 hand_scale / head_scale
├─ smplx_expression/shape_decoder ► body_param 的 exp / shape
├─ flame_poses_decoder ───► 切片成 flame_param 的 4 个姿态键
├─ flame_expression/shape_decoder ► flame_param 的 expression/shape
└─ cam_decoder ───────────► +bias、算深度 24/s ► pd_cam (B,4,4)
   ▼
all_out = {'pd_cam': RT, 'body_param': …, 'flame_param': …}
```

关于 `pd_cam` 的一点原理：网络先输出一个 3 维弱相机向量，其中第三维被换算成"深度"。换算式为

\[
z = \frac{f}{s}, \qquad f = 24
\]

其中 \(s\) 是网络预测的缩放因子、\(f\) 是渲染用的固定焦距（24，与 [app.py:L133](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L133) 中 `focal_length=24.0` 一致）。直觉是：预测的人越小（\(s\) 越小），说明人离相机越远（\(z\) 越大），这与透视成像中"近大远小"一致。随后 `get_full_proj` 把它拼成一个 4×4 的 RT 矩阵。详细推导放在 u3-l4「相机模型」一讲。

#### 4.3.3 源码精读

**① 唯一权威：head 的返回语句。** [models/smplx/smplx_head.py:L314-L319](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/smplx_head.py#L314-L319) 明确写下三个键并返回：

```python
all_out =  {}
all_out['pd_cam'] = RT  # [4,4]
all_out['body_param'] = body_param_dict
all_out['flame_param'] = flame_param_dict
```

这就是"输出字典三个关键字段"的出处。`Ehm_Pipeline.forward` 原样把它透传出来。

**② `body_param` 的 11 个键。** 由 [smplx_head.py:L281-L300](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/smplx_head.py#L281-L300) 逐个填入，配合 [smplx_head.py:L130-L137](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/smplx_head.py#L130-L137) 的解码器维度定义，可整理成下表（设 batch 为 B）：

| 键 | 形状 | 语义 |
| --- | --- | --- |
| `global_pose` | (B, 1, 3, 3) | 全局朝向（旋转矩阵） |
| `body_pose` | (B, 21, 3, 3) | 21 个身体关节旋转 |
| `left_hand_pose` / `right_hand_pose` | (B, 15, 3, 3) | 每只手 15 个关节旋转 |
| `hand_scale` / `head_scale` | (B, 3) | 手/头缩放 |
| `exp` | (B, 50) | SMPL-X 表情系数 |
| `shape` | (B, 200) | SMPL-X 体型系数 |
| `eye_pose` / `jaw_pose` / `joints_offset` | None | 推理时显式置空（见 [smplx_head.py:L296-L298](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/smplx_head.py#L296-L298)），这些量交给 FLAME 分支处理 |

维度自检：`smplx_poses_decoder = nn.Linear(dim, 312)`（[smplx_head.py:L130](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/smplx_head.py#L130)），而 \(1 + 21 + 15 + 15 = 52\) 个关节、每个 6D 旋转正好 \(52 \times 6 = 312\)，切片 `[:, :6]`、`[:, 6:132]`、`[:, 132:222]`、`[:, 222:312]` 与之严丝合缝。

**③ `flame_param` 的 6 个键。** [smplx_head.py:L270-L278](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/smplx_head.py#L270-L278) 先用 `flame_poses_decoder = nn.Linear(dim, 14)` 输出 14 维，再按区间切片：

| 键 | 形状 | 语义 |
| --- | --- | --- |
| `eye_pose_params` | (B, 6) | 双眼各 3 维旋转 |
| `pose_params` | (B, 3) | 头部全局姿态 |
| `jaw_params` | (B, 3) | 下颌开合 |
| `eyelid_params` | (B, 2) | 左右眼睑 |
| `expression_params` | (B, 50) | FLAME 表情 |
| `shape_params` | (B, 300) | FLAME 脸型 |

维度自检：\(6 + 3 + 3 + 2 = 14\)，与 `nn.Linear(dim, 14)`（[smplx_head.py:L141](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/smplx_head.py#L141)）一致。

**④ `pd_cam` 的构造。** [smplx_head.py:L303-L309](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/smplx_head.py#L303-L309)：`cam_decoder` 输出 3 维 → 加上 bias `[0, 0, 1.5]` → 第三维换算成 \(24/s\) → `get_full_proj` 返回 `(full_project, RT)`，其中 RT 就是最终的 `pd_cam`，形状 `(B, 4, 4)`。

**⑤ 下游如何消费这三个字段。** 两处标准用法：

- [inference_wo_detect.py:L85-L93](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L85-L93)：`pd_smplx_dict = ehm(outputs['body_param'], outputs['flame_param'], pose_type='aa')` 把参数变成网格；`GS_Camera(..., R = outputs['pd_cam'][0:1,:3,:3], T = outputs['pd_cam'][0:1,:3,3])` 从 4×4 里切出旋转和平移构造渲染相机。
- [app.py:L385-L390](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L385-L390)：视频版逐帧把 `outputs['body_param']`、`outputs['flame_param']`、`outputs['pd_cam']` 分别 append 进三个序列，留待时序平滑后再渲染（详见 u5-l4）。

**⑥ 参数最终变成多少顶点。** [models/modules/ehm/EHM_v2.py:L34-L45](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L34-L45) 是 `EHM_v2.forward` 的签名——它接收的正是上面两个参数字典；[EHM_v2.py:L204-L209](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L204-L209) 的返回字典给出了最终网格规模：

```python
prediction = {  #  [B,10475,3]
    'vertices': vertices,
    'joints': joints,  # [B,145,3]
    ...
}
```

即 **10475 个顶点、145 个关节**。值得强调：FLAME 的头顶点不是"拼"在 SMPL-X 后面，而是通过 `smplx2flame_ind` 索引**替换**SMPL-X 对应的头顶点（见 [EHM_v2.py:L153-L158](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L153-L158)），所以顶点总数维持 10475 不变。融合细节在 u4-l4 专讲。

#### 4.3.4 代码实践

**实践目标**：不看讲义，仅凭源码写出 PEAR 输出字典的三个关键字段、它们的形状与去向。

**操作步骤**：

1. 用编辑器打开 `models/smplx/smplx_head.py`，跳到最后一个 `def forward`（[L255](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/smplx_head.py#L255) 附近），从 `all_out = {}` 往上读，抄下三个顶层键。
2. 对每个键，沿着赋值语句往上追一层，记录它由哪个 `*_decoder` 产生、`nn.Linear` 的输出维度是多少。
3. 打开 `inference_wo_detect.py` 第 85~93 行，写下这三个键分别被传给了哪个函数。
4. 打开 `models/modules/ehm/EHM_v2.py` 第 204~209 行，记录最终 `vertices` 与 `joints` 的形状注释。
5. 把以上内容整理成一张三行的表格，和本节 4.3.3 的表格对照。

**需要观察的现象**：`body_param` 是嵌套字典而非一个张量；`pd_cam` 是 `(B,4,4)` 而非 docstring 声称的 `(B,3)`；`EHM_v2` 的返回里 `vertices` 形状注释为 `[B,10475,3]`。

**预期结果**：你应当得到与下表一致的结论——

| 字段 | 形状 | 去向 |
| --- | --- | --- |
| `body_param` | dict（global_pose (B,1,3,3)、body_pose (B,21,3,3)、left/right_hand_pose (B,15,3,3)、hand_scale/head_scale (B,3)、exp (B,50)、shape (B,200)、eye_pose/jaw_pose/joints_offset = None） | `ehm(body_param, flame_param)` → 顶点 |
| `flame_param` | dict（eye (B,6)、pose (B,3)、jaw (B,3)、eyelid (B,2)、expression (B,50)、shape (B,300)） | 同上 |
| `pd_cam` | (B, 4, 4) | `GS_Camera(R = pd_cam[:,:3,:3], T = pd_cam[:,:3,3])` → 渲染相机 |

**待本地验证**：形状数值来自源码注释与线性层维度推导，尚未实际运行打印。下一讲搭好环境后，可在 [inference_wo_detect.py:L85](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L85) 后插入 `print({k: (v.shape if torch.is_tensor(v) else v) for k, v in outputs['body_param'].items()}); print(outputs['pd_cam'].shape)` 验证。

#### 4.3.5 小练习与答案

**练习 1**：`body_param['body_pose']` 的形状是 (B, 21, 3, 3)，为什么每个关节是 3×3 的矩阵而不是 3 个数？

**参考答案**：网络对每个关节实际输出 6 维的"6D 旋转表示"（`nn.Linear(dim, 312)` 对应 52 关节 × 6 维），再经 `rot6d_to_rotmat`（[smplx_head.py:L287](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/smplx_head.py#L287)）正交化成 \(3 \times 3\) 旋转矩阵。6D 表示是深度学习中表征旋转的经典技巧，比欧拉角无奇异性、比四元数无双重覆盖歧义。细节在 u3-l3 展开。

**练习 2**：为什么需要 `flame_param` 单独一套参数，而不是把头部细节塞进 `body_param`？

**参考答案**：因为头部由 FLAME 建模、身体由 SMPL-X 建模，两者的参数空间不同：FLAME 需要眼睑、下颌、眼球、50 维表情、300 维脸型这类 SMPL-X 没有的量。分成两个字典正好与 `EHM_v2.forward(body_param_dict, flame_param_dict)` 的两个形参一一对应，边界清晰。

**练习 3**：如果有人告诉你"`ehm_model(x)` 返回 `outputs['pd_params']`"，你怎么快速证伪？

**参考答案**：直接查实现处的返回语句 [smplx_head.py:L314-L318](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/smplx_head.py#L314-L318)（键为 `pd_cam`/`body_param`/`flame_param`，没有 `pd_params`），以及调用处 [app.py:L388-L390](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L388-L390)。`pd_params` 只出现在 `ehm_pipeline.py` 过期的 docstring 和注释里。

## 5. 综合实践

**任务：亲手绘制 PEAR 的端到端数据流图，并标注每一步的源码位置与张量形状。**

这是贯穿本讲三个模块的收口任务。请按以下步骤完成：

1. **画主干**。在纸或任意画图工具里画出五个节点：

   ```
   原始图像 → ViT 骨干 → SMPLX 解码头 → EHM_v2 → Renderer2
   ```

   注意在「原始图像」和「ViT 骨干」之间补上两个预处理小步骤：`pad_and_resize` 和 `normalize + 裁剪`。

2. **标形状**。给每条边标注张量形状：

   - 原始图像 → patch：`(B,3,256,256)`，值域 0~1（依据 [inference_wo_detect.py:L80-L82](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L80-L82)）
   - patch → 骨干输入：`(B,3,256,192)`（依据 [ehm_pipeline.py:L47](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/ehm_pipeline.py#L47) 与 [configs/infer.yaml:L14](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/configs/infer.yaml#L14)）
   - 骨干 → 头：特征图（依据 [ehm_pipeline.py:L47-L50](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/ehm_pipeline.py#L47-L50)）
   - 头 → EHM_v2：`body_param` / `flame_param` 两个嵌套字典（依据 [smplx_head.py:L314-L319](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/smplx_head.py#L314-L319)）
   - EHM_v2 → Renderer：`vertices (B,10475,3)`、`joints (B,145,3)`（依据 [EHM_v2.py:L204-L209](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L204-L209)）

3. **标文件**。在五个节点下方分别写上：入口脚本（`app.py` / `inference_wo_detect.py`）、`models/pipeline/ehm_pipeline.py`、`models/smplx/smplx_head.py`、`models/modules/ehm/EHM_v2.py`、`models/modules/renderer/body_renderer.py`。

4. **补一条相机支路**。从「解码头」另画一条支路指向「Renderer」，标上 `pd_cam (B,4,4)`，并注明它在渲染前被拆成 `R = pd_cam[:,:3,:3]`、`T = pd_cam[:,:3,3]`（依据 [inference_wo_detect.py:L91](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L91)）。

5. **自检**。合上讲义，回答三个问题：(a) 模型内部为什么从 256 裁到 192？(b) `flame_param` 里哪两个键控制眨眼和下颌？(c) 最终网格有多少顶点、来自哪两个底层模型的融合？

**预期结果**：一张自洽的数据流图，任何一条边都能在 30 秒内说出对应的源码文件与行号。这张图将是你阅读后续所有讲义的"地图"。

**待本地验证**：图中标注的形状来自源码注释与维度推导；运行级验证留到下一讲环境搭好后，用第 4.3.4 节的打印语句完成。

## 6. 本讲小结

- PEAR 是一个**实时 expressive 人体网格恢复**框架：单张图像/视频帧输入，一次前向同时预测 SMPL-X 身体参数、FLAME 头部参数和相机参数，论文口径达到 100 FPS（[README.md:L46-L52](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/README.md#L46-L52)）。
- 推理输入是一个 `(B,3,256,256)`、值域 0~1 的人体 patch；模型内部先 ImageNet 归一化，再把宽度裁成 192，匹配骨干的 `img_size: [256,192]`（[ehm_pipeline.py:L26-L47](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/ehm_pipeline.py#L26-L47)）。
- 输出字典的三个关键字段是 `body_param`（11 键嵌套字典，身体与手部）、`flame_param`（6 键嵌套字典，头部细节）、`pd_cam`（(B,4,4) 相机 RT 矩阵），其权威定义在 [smplx_head.py:L314-L319](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/smplx_head.py#L314-L319)，而非 `ehm_pipeline.py` 里过期的 docstring——**判断接口要看实现和调用方，不能只看注释**。
- 两个参数字典喂给 `EHM_v2` 得到 `vertices (B,10475,3)`、`joints (B,145,3)` 的统一网格；FLAME 头是通过索引替换进 SMPL-X 网格的，所以顶点总数不变。
- `pd_cam` 的第三维按 \(z = 24/s\) 换算成深度，体现"预测的人越小、离相机越远"的透视直觉，是"像素对齐"的关键。
- 资源入口全部集中在 README 顶部徽章：arXiv 2601.22693、项目页、YouTube、HuggingFace Demo；预训练权重 `pear_model.pt` 会在首次运行时从 `BestWJH/PEAR_models` 自动下载。

## 7. 下一步学习建议

- **下一讲（u1-l2）**：环境搭建与人体模型资产准备。你需要装好 `pytorch3d` 和 `chumpy` 这两个特殊依赖，并把 `SMPL_NEUTRAL.pkl`、`SMPLX_NEUTRAL_2020.npz`、`generic_model.pkl` 放进 `assets/` 对应目录——本讲里所有"待本地验证"的形状，下一讲结束后都可以实际跑出来。
- **再下一讲（u1-l3）**：仓库目录结构与代码地图，把本讲提到的文件放回完整的目录树里。
- **提前预读的源码**（可选）：把 [inference_wo_detect.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py) 从头到尾读一遍，它是所有入口里最短的一条完整链路，只有约 130 行，本讲的数据流图在其中每一行都能找到落点。
- **阅读顺序提醒**：本手册单元三、单元四会分别深入「网络结构」和「人体模型」，如果你对 Transformer 或 3D 旋转还不熟，建议在进入单元三前先补一下 6D 旋转表示（`rot6d_to_rotmat`）与线性混合蒙皮（LBS）的背景知识。
