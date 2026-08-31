# 仓库目录结构与代码地图

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出根目录每个入口脚本（`app.py` / `inference_images.py` / `inference_wo_detect.py` / `train_ehms.py`）各自的用途与适用场景。
2. 把 `models/`、`dataset/`、`utils/`、`configs/` 下的文件按职责分类：入口 / 骨干 / 解码头 / 人体模型 / 渲染 / 数据 / 工具。
3. 通过 import 关系判断哪些模块在推理链路上、哪些只在训练中使用、哪些是遗留的"孤儿"模块（没有任何文件引用它）。

一句话概括本讲：**给整个仓库画一张地图，之后所有讲义都在这张地图上行走。**

> 一个诚实的更正：本讲规划时写作"五个入口脚本"，实际清点后根目录只有 **4 个**可执行入口脚本。如果把 `dataset/webdata_loader.py` 里可独立调用的数据集构建函数 `build_web_tracked_data` 算作"库入口"，才凑得上五个。本讲按事实讲解。

## 2. 前置知识

### 2.1 什么是"入口脚本"

Python 项目有两种文件：

- **入口脚本**：直接用 `python xxx.py` 运行的文件，通常底部有 `if __name__ == "__main__":`。
- **库模块**：被别人 `import` 的文件，自己不运行。

PEAR 是典型的"研究型单包项目"：没有 `setup.py` / `pyproject.toml`，不打包安装，**所有脚本都必须在仓库根目录下启动**（因为代码里写死了 `configs/...`、`assets/...` 这样的相对路径）。这一点在上一讲环境搭建中已经强调过。

### 2.2 怎么读 import 才能看懂项目结构

一行 import 就是一条依赖边。例如：

```python
from models.modules.ehm import EHM_v2   # 依赖 models/modules/ehm 这个包
```

把所有入口脚本的 import 收集起来，从入口出发做"可达性分析"，就能把仓库分成三类：

- **入口可达**：推理或训练链路上真正会用到的代码；
- **仅训练可达**：只有 `train_ehms.py` 能到达的代码；
- **不可达（孤儿模块）**：没有任何入口能到达的代码——通常是作者从其他项目（Multi-HMR、SMPLest-X 等）继承下来但暂未启用的部分。

### 2.3 本讲用到的 shell 命令

- `grep -n "pattern" file`：在文件里找文本并显示行号；
- `ls -R` 或 `tree`：列目录树（`tree` 可能需要另装，用 `ls` 也行）；
- `git ls-files`：列出 git 追踪的所有文件，比 `ls` 更干净（能避开 `__pycache__`）。

## 3. 本讲源码地图

本讲涉及的文件及其作用（均为真实存在、已核对过的文件）：

| 文件 / 目录 | 作用 |
| --- | --- |
| `README.md` | 项目主页：Quick Start、资产下载、三条运行命令 |
| `app.py` | 入口 1：Gradio 视频演示（网页界面） |
| `inference_images.py` | 入口 2：多人图像批量推理（YOLOv8 自动检测人） |
| `inference_wo_detect.py` | 入口 3：单人图像推理（不做人体检测，整图直接缩放） |
| `train_ehms.py` | 入口 4：训练入口 |
| `requirements.txt` | 依赖清单（含 ultralytics、lightning、smplx 等关键库） |
| `models/` | 全部模型代码：backbones / smplx（解码头）/ modules（人体模型与渲染）/ pipeline（组装）/ vitdet（遗留检测器） |
| `dataset/` | 训练数据管线（WebDataset） |
| `utils/` | 通用工具：配置、张量、相机、渲染原语、可视化 |
| `configs/` | `infer.yaml` 与 `train.yaml` 两份 YAML 配置 |
| `assets/` | 人体模型资产（SMPL/SMPLX/FLAME）与图片素材 |
| `example/` | 示例图片 `images/` 与两段示例视频 |

## 4. 核心概念与源码讲解

本讲的三个最小模块：

1. 入口脚本一览
2. models 子包职责划分
3. dataset 与 utils 子包职责划分

### 4.1 入口脚本一览

#### 4.1.1 概念说明

PEAR 的根目录有 4 个可执行入口，对应三种使用方式（推理 ×3 + 训练 ×1）：

| 脚本 | 用途 | 输入 | 输出 | 是否出现在 README |
| --- | --- | --- | --- | --- |
| `app.py` | Gradio 网页演示，逐帧推理视频并做时序平滑 | 浏览器上传的视频（自动截前几秒） | `mesh_video.mp4`、`results.npz`（参数下载） | 是 |
| `inference_images.py` | 多人图像批量推理：YOLOv8 检测每个人 → 逐人裁剪推理 → 网格贴回原图 | 一个图片目录（默认 `example/images`） | `mesh_*.jpg` + 拼成 `video.mp4` | 是 |
| `inference_wo_detect.py` | 单人推理（无检测）：整图等比缩放成 256×256 方块直接推理 | 一个图片目录 | `mesh_*.jpg`、`result_*.jpg` | 否（隐藏入口） |
| `train_ehms.py` | 训练：组装数据集 + `OurPipeline` + Lightning Fabric | WebDataset 的 tar 分片 | `outputs/<时间戳>/` 下的日志与 checkpoint | 是 |

README 的 Quick Start 只给出三条命令，对应 [README.md:L97-L111](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/README.md#L97-L111)（推理两条）和 [README.md:L115-L133](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/README.md#L115-L133)（训练一条）；`inference_wo_detect.py` 是README 里没写、但源码完整可用的第四个入口。

#### 4.1.2 核心流程

四个入口共享同一个"组装套路"，伪代码如下：

```
读取 configs/<name>.yaml → ConfigDict → add_extra_cfgs 补全默认段
↓
构建渲染器 Renderer2("assets/SMPLX", 1024, focal_length=24)
↓
hf_hub_download 自动下载 pear_model.pt（BestWJH/PEAR_models）
↓
Ehm_Pipeline(cfg)：backbone = ViT(**cfg.BACKBONE)，head = SMPLXTransformerDecoderHead(...)
加载 state['backbone'] / state['head'] 两段权重（strict=False）
↓
EHM_v2("assets/FLAME", "assets/SMPLX")：参数 → 10475 顶点网格
↓
【差异点】app：逐帧 + Savitzky-Golay 平滑 + Gradio 界面
         inference_images：YOLO 检测 → 每人裁 256 patch → 推理 → warpAffine 贴回
         inference_wo_detect：整图 pad_and_resize(256) → 推理 → alpha 叠加
         train：build_web_tracked_data → OurPipeline.run_fit()
```

前四个步骤在三个推理入口中几乎是逐行复制的——这是研究代码的典型特征：**复制代替抽象**。

#### 4.1.3 源码精读

**（1）`inference_wo_detect.py` 的 import 区就是一张"推理链路依赖清单"**

[inference_wo_detect.py:L1-L9](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L1-L9)

```python
from models.modules.ehm import EHM_v2
from models.pipeline.ehm_pipeline import Ehm_Pipeline
import os
import torch
from utils.pipeline_utils import to_tensor
from utils.graphics_utils import GS_Camera
from models.modules.renderer.body_renderer import Renderer2 as BodyRenderer
from pytorch3d.renderer import PointLights
import cv2
```

这 9 行告诉我们：**最小推理链路只需要 5 个项目内模块**——`EHM_v2`（人体模型）、`Ehm_Pipeline`（网络）、`to_tensor`（预处理）、`GS_Camera`（相机）、`Renderer2`（渲染）。其余全是第三方库（torch、pytorch3d、cv2）。记住这 5 个名字，单元三、单元四就是逐个把它们讲透。

**（2）权重加载的"两段式"约定**

[inference_wo_detect.py:L56-L63](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L56-L63)

```python
repo_id = "BestWJH/PEAR_models"
filename = "pear_model.pt"
ehm_basemodel = hf_hub_download(repo_id=repo_id, filename=filename, repo_type="model")
ehm_model = Ehm_Pipeline(meta_cfg)
_state = torch.load(ehm_basemodel, map_location='cpu', weights_only=True)
ehm_model.backbone.load_state_dict(_state['backbone'], strict=False)
ehm_model.head.load_state_dict(_state['head'], strict=False)
```

这段代码做三件事：从 HuggingFace Hub 自动下载权重、实例化 `Ehm_Pipeline`、把 checkpoint 里的 `backbone` 和 `head` 两段 state dict 分别装进对应子模块。注意 **checkpoint 只包含网络权重，不包含 EHM_v2**——因为 EHM_v2 是确定性的"参数→网格"映射（人体模型本体），没有可学习参数需要保存。训练脚本 [train_ehms.py:L65-L70](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/train_ehms.py#L65-L70) 用完全相同的方式加载基模型，这印证了"训练与推理共享同一套网络定义"。

**（3）`inference_images.py` 比"无检测版"多出来的东西**

[inference_images.py:L16-L29](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_images.py#L16-L29)

```python
from ultralytics import YOLO
...
from utils.general_utils import (
    ConfigDict, device_parser, add_extra_cfgs
)
from utils.get_video import images_to_video
```

多出的 `ultralytics`（YOLOv8）、`device_parser`（多卡解析）、`images_to_video`（结果拼视频）暴露了它的差异化职责：多人场景。检测器的初始化在 [inference_images.py:L280-L282](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_images.py#L280-L282)，指向本地路径 `./model_zoo/yolov8x.pt`（首次运行会由 ultralytics 自动下载到该路径）。

**（4）`train_ehms.py` 是"数据 + 装配"入口**

[train_ehms.py:L1-L13](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/train_ehms.py#L1-L13)

```python
import lightning
from models.pipeline.pipeline import OurPipeline
from dataset.webdata_loader import build_web_tracked_data
from utils.general_utils import (
    ConfigDict, rtqdm, device_parser,
    calc_parameters, biuld_logger, add_extra_cfgs
)
```

训练入口只 import 三个项目内模块：`OurPipeline`（训练装配）、`build_web_tracked_data`（数据集）、`general_utils`（配置与工具）。注意它 **没有** import `EHm_Pipeline`——训练用的网络在 `OurPipeline` 内部另行组装（见 [models/pipeline/pipeline.py:L43-L44](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/pipeline.py#L43-L44) 的 `class OurPipeline`）。数据集构建与 `run_fit()` 的启动分别在 [train_ehms.py:L45-L46](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/train_ehms.py#L45-L46) 和 [train_ehms.py:L62-L72](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/train_ehms.py#L62-L72)。

**（5）`app.py` 的开头是一段"环境自检"**

[app.py:L59-L65](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L59-L65)

```python
try:
    import chumpy
    import pytorch3d
    from pytorch3d import _C
    print(f"🎉 All systems go! PyTorch3D GPU: {hasattr(_C, 'rasterize_meshes')}")
except Exception as e:
    print(f"🚨 Validation failed: {e}")
```

上一讲已经讲过这段环境校验。从"代码地图"的角度看，它的意义是：**app.py 是四个入口中对环境要求最苛刻的一个**（还要 gradio、decord、imageio、scipy 等，见 [app.py:L68-L108](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L68-L108) 的完整 import 区）。项目内的 import（[app.py:L96-L107](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L96-L107)）与 `inference_wo_detect.py` 的 5 件套完全一致，再加上 `savgol_filter`（时序平滑，定义在 [app.py:L253](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L253) 的 `polynomial_smooth`）。Gradio 界面组装在 [app.py:L558](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L558) 的 `gr.Blocks(...)`，事件绑定在 [app.py:L854-L867](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L854-L867)。

#### 4.1.4 代码实践：用 grep 统计入口的 import

1. **实践目标**：用一条命令生成"入口 → 项目内模块"的依赖表，验证 4.1.1 的分类。
2. **操作步骤**（在仓库根目录执行）：

   ```bash
   grep -nE "^(from|import) (models|dataset|utils)" \
        app.py inference_images.py inference_wo_detect.py train_ehms.py
   ```

   再统计每个入口引用的项目内顶层包数量：

   ```bash
   for f in app.py inference_images.py inference_wo_detect.py train_ehms.py; do
       echo "== $f"
       grep -oE "^(from|import) (models|dataset|utils)[.a-z_]*" "$f" | sort -u
   done
   ```

3. **需要观察的现象**：三个推理入口引用的都是 `models.*` + `utils.*`，唯独 `train_ehms.py` 出现 `dataset.webdata_loader`；`inference_images.py` 是唯一 import `ultralytics` 的入口。
4. **预期结果**：得到一张 4 行的依赖表，其中 `models.pipeline.ehm_pipeline`、`models.modules.ehm`、`utils.general_utils` 是三个推理入口的公共依赖，而 `dataset.*` 只出现在训练入口。
5. 如果你的 shell 输出与上述描述不一致（例如未来版本重构了 import），以你本地 grep 的实际输出为准。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `pear_model.pt` 里要分成 `backbone` 和 `head` 两段 state dict，而不是一整个？

**参考答案**：因为 `Ehm_Pipeline` 是一个 LightningModule 容器，内部有两个独立子模块 `self.backbone = ViT(**cfg.BACKBONE)` 和 `self.head = SMPLXTransformerDecoderHead(...)`（见 [models/pipeline/ehm_pipeline.py:L24-L26](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/ehm_pipeline.py#L24-L26)）。分开保存使得骨干（来自 ViTPose 预训练）和解码头可以各自替换、各自加载（`strict=False` 也因此能容忍少量键不匹配）。

**练习 2**：想快速验证环境是否装好，但不跑任何模型，应该运行哪个入口？为什么？

**参考答案**：`python app.py`。它顶部就有 chumpy / pytorch3d / `_C` 的三连自检（[app.py:L59-L65](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L59-L65)），会在加载任何大模型之前打印"🎉 All systems go!"或"🚨 Validation failed"。不过注意它随后仍会继续初始化 Gradio 界面。

**练习 3**：`inference_wo_detect.py` 和 `inference_images.py` 都对图片做"变成 256×256"的操作，二者本质区别是什么？

**参考答案**：前者把**整张图**等比缩放后居中 pad 成 256×256（[inference_wo_detect.py:L25-L34](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L25-L34) 的 `pad_and_resize`），假设画面里只有一个居中人体；后者先用 YOLO 检测出每个人，再对**每个检测框**做等比扩框 + 仿射裁剪成 256×256 patch（[inference_images.py:L321-L332](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_images.py#L321-L332)），处理完还要用 `inv_trans` 把网格贴回原图（多人场景）。下一单元 u2-l2 / u2-l3 会分别逐行精读这两条链路。

### 4.2 models 子包职责划分

#### 4.2.1 概念说明

`models/` 是仓库的核心，共 5 个子包。按数据流方向（图像 → 网格）排列：

```
models/
├── backbones/      骨干网络：ViT，把图像 patch 变成特征 token 序列
├── smplx/          解码头：TransformerDecoder + 一组参数解码器（注意：名字叫 smplx，实际是"网络头部"）
├── modules/        人体模型本体与渲染器（不再含可学习参数）
│   ├── ehm/          ★ EHM / EHM_v2：SMPL-X + FLAME 融合的统一人体表达
│   ├── smplx/        SMPLX 官方层（vchoutas/smplx 改版）
│   ├── flame/        FLAME 头部模型 + 定制 lbs
│   ├── mano/         MANO 手部模型
│   ├── renderer/     pytorch3d 封装的网格渲染器
│   ├── human_matting/（孤儿）StyleMatte 人像抠图
│   └── base/         （孤儿）ONNX 推理封装
├── pipeline/       组装层：ehm_pipeline（推理用 LightningModule）、pipeline（训练装配）、loss（损失）
└── vitdet/         （孤儿）detectron2 ViTDet 检测器，来自 Multi-HMR，当前主入口改用 YOLOv8
```

一个容易混淆的点：**`models/smplx/` 和 `models/modules/smplx/` 是两个不同的东西**。前者是"解码头"（可学习网络），后者是"SMPL-X 官方模型层"（模板与蒙皮数学）。`EHM_v2` 同时用到两者。

#### 4.2.2 核心流程

推理时的模块调用链（箭头表示 import / 调用）：

```
入口脚本
 └─> models/pipeline/ehm_pipeline.py :: Ehm_Pipeline
      ├─> models/backbones :: ViT                      （特征提取）
      └─> models/smplx/smplx_head.py :: SMPLXTransformerDecoderHead
           ├─> models/smplx/pose_transformer.py :: TransformerDecoder
           │    └─> models/smplx/t_cond_mlp.py          （条件 LayerNorm 等小件）
           ├─> models/smplx/smplx_layer.py / SMPLXV2.py （头内部也要查人体模板，做均值初始化）
           └─（训练时）models/pipeline/loss.py
 └─> models/modules/ehm :: EHM_v2                       （参数 → 10475 顶点）
      ├─> models/modules/smplx :: SMPLX
      ├─> models/modules/flame :: FLAME + lbs
      ├─> models/modules/mano :: MANO
      └─> models/smplx/SMPLXV2.py :: SMPLX（SMPLX_v2）
 └─> models/modules/renderer/body_renderer.py :: Renderer2
      └─> utils/graphics_utils.py :: GS_BaseMeshRenderer（pytorch3d 光栅化）
```

训练时多一条支路：`train_ehms.py → models/pipeline/pipeline.py :: OurPipeline`，它把 backbone/head/优化器/数据加载器一起交给 Lightning Fabric。

#### 4.2.3 源码精读

**（1）两个子包的"出口"只有一行**

[models/backbones/__init__.py:L1](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/backbones/__init__.py#L1)

```python
from .vit import ViT
```

[models/modules/ehm/__init__.py:L1-L2](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/__init__.py#L1-L2)

```python
from .EHM import EHM
from .EHM_v2 import EHM_v2
```

子包用 `__init__.py` 收敛对外接口，入口脚本因此可以写 `from models.modules.ehm import EHM_v2` 而不用关心文件名。注意 `EHM.py`（v1）虽被导出，但四个入口引用的全是 `EHM_v2`——v1 是历史版本，保留供对照。

**（2）`Ehm_Pipeline` 的构造函数就是"网络结构的目录"**

[models/pipeline/ehm_pipeline.py:L1-L26](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/ehm_pipeline.py#L1-L26)

```python
from models.backbones import ViT
from models.smplx.smplx_head import SMPLXTransformerDecoderHead
...
class Ehm_Pipeline(L.LightningModule):
    def __init__(self, cfg):
        ...
        self.backbone = ViT(**cfg.BACKBONE)
        self.head = SMPLXTransformerDecoderHead(cfg.HEAD, cfg.TRAIN.batch_size)
        self.normalize = torchvision.transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[...])
```

三行赋值对应网络的两大件加一个归一化：骨干由 `cfg.BACKBONE` 的键值直接展开成 ViT 构造参数（配置驱动构建，详见 u2-l1）。

**（3）`forward` 只有一步裁剪 + 两次调用**

[models/pipeline/ehm_pipeline.py:L29-L52](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/ehm_pipeline.py#L29-L52)

```python
def forward(self, x: torch.Tensor):
    x = self.normalize(x)
    feats = self.backbone(x[:, :, :, 32:-32])
    outputs = self.head(feats)
    return outputs
```

`x[:, :, :, 32:-32]` 把 256×256 的输入沿宽度两侧各裁 32 像素，变成 256×192（匹配骨干 patch 划分）。返回的 `outputs` 字典内容在 u1-l1 已经讲过（`body_param` / `flame_param` / `pd_cam`）。

**（4）`EHM_v2` 的 import 区揭示它"集齐了所有人体模型"**

[models/modules/ehm/EHM_v2.py:L1-L12](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L1-L12)

```python
from ..mano import MANO
from ..smplx import SMPLX
from models.smplx.SMPLXV2 import SMPLX as SMPLX_v2
from ..flame import FLAME, vertices2landmarks
from ..flame.lbs import lbs, lbs_wobeta, lbs_get_transform, ...
```

这一段证实了 4.2.1 的判断：`modules/mano`、`modules/smplx`、`modules/flame`、`smplx/SMPLXV2` 都是被 `EHM_v2` 拉进推理链路的活代码，**不要**当成孤儿模块。`EHM_v2` 的 `forward` 签名在 [models/modules/ehm/EHM_v2.py:L34](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L34)（接受 `body_param_dict` / `flame_param_dict` 等），u4-l4 会精读。

**（5）训练装配与损失在 `pipeline/` 的另外两个文件**

- [models/pipeline/pipeline.py:L1-L41](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/pipeline.py#L1-L41)：`OurPipeline` 的 import 区比推理版长得多——引入了 loss 系列、`GS_Camera`、`Renderer2`、TensorBoard `SummaryWriter`、`smplx2smpl_joints`、`smpl_para2mesh` 等，说明训练循环里包含损失计算、投影、可视化全套逻辑。主循环入口 [models/pipeline/pipeline.py:L250](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/pipeline.py#L250) `def run_fit(self, init_iter=0)`，checkpoint 保存在 [models/pipeline/pipeline.py:L376](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/pipeline.py#L376)。
- [models/pipeline/loss.py:L1-L13](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/loss.py#L1-L13)：定义 `GMoF`、`Keypoint2DLoss`、`BodyParameterLoss` 等损失（u5-l3 精读）。

**（6）渲染子包的分工**

[models/modules/renderer/body_renderer.py:L1-L12](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/renderer/body_renderer.py#L1-L12)

```python
from pytorch3d.io import load_obj
from .base_renderer import BaseMeshRenderer
from utils.helper import face_vertices
from utils.graphics_utils import GS_BaseMeshRenderer
```

`body_renderer.py` 里的 `Renderer2` 是渲染的门面；真正的光栅化与着色原语在 [utils/graphics_utils.py:L701](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/graphics_utils.py#L701) 的 `GS_BaseMeshRenderer`。同目录的 `hand_renderer.py`、`head_renderer.py` 没有被任何文件 import——是孤儿模块。

**（7）vitdet：一个"被替换掉的检测器"**

[models/vitdet/__init__.py:L5-L19](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/vitdet/__init__.py#L5-L19)

```python
def build_detector(batch_size, max_img_size, device):
    cfg_path = Path(__file__).parent / 'cascade_mask_rcnn_vitdet_h_75ep.py'
    detectron2_cfg = LazyConfig.load(str(cfg_path))
    detectron2_cfg.train.init_checkpoint = "https://dl.fbaipublicfiles.com/detectron2/..."
    ...
```

这个子包提供 detectron2 的 ViTDet 级联 Mask R-CNN 检测器（README 致谢的 Multi-HMR 风格），提供 `build_detector` 工厂函数。但用 grep 检索会发现**没有任何入口脚本 import 它**——当前多人检测已改用 `ultralytics` 的 YOLOv8（[inference_images.py:L281-L282](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_images.py#L281-L282)）。它是"备选方案"，留在仓库里供二次开发（u5-l5 会再提到）。

#### 4.2.4 代码实践：画出 models 内部依赖图

1. **实践目标**：验证 4.2.2 的调用链，亲手得到 `models/` 内部的 import 边。
2. **操作步骤**：

   ```bash
   # 列出 models 内所有"项目内 import"（排除第三方库）
   grep -rnE "^(from|import) (models|utils|\.)" models/ --include=*.py \
        | grep -v __pycache__ | grep -v ipynb_checkpoints

   # 反查：谁引用了 vitdet / human_matting / hand_renderer？（预期：只有它们自己）
   grep -rn "vitdet\|human_matting\|hand_renderer\|head_renderer" \
        --include=*.py . | grep -v __pycache__ | grep import
   ```

3. **需要观察的现象**：第一条命令输出的每行形如 `models/xxx.py:行号:from models.yyy import ...`，就是一条依赖边；第二条命令几乎只命中模块自身文件。
4. **预期结果**：把第一条输出按"文件 → 被 import 的模块"整理成有向图后，应与 4.2.2 的 ASCII 图一致；`vitdet`、`human_matting`、`hand_renderer/head_renderer`、`modules/base/onnx_model` 入度为 0（无人引用）。
5. 结论以你本地 grep 输出为准；若未来某入口启用了 vitdet，这条结论就过期了。

#### 4.2.5 小练习与答案

**练习 1**：`models/smplx/` 与 `models/modules/smplx/` 各自存放什么？为什么作者要分开放？

**参考答案**：`models/smplx/` 是解码头一侧的网络代码（`smplx_head.py` 参数解码器组、`pose_transformer.py` Transformer、`smplx_layer.py`、`SMPLXV2.py` 等，含可学习参数）；`models/modules/smplx/SMPLX.py` 是从 vchoutas/smplx 移植的 SMPL-X 官方层（模板、蒙皮权重、关节回归器等确定性数学）。分开存放是因为职责不同：前者是"预测参数的网络"，后者是"把参数变成网格的函数"。`EHM_v2` 在 [EHM_v2.py:L5-L6](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L5-L6) 同时 import 了两者。

**练习 2**：判断题：`models/modules/mano/MANO.py` 是孤儿模块。对吗？

**参考答案**：不对。虽然没有任何入口脚本直接 import 它，但 [models/modules/ehm/EHM_v2.py:L4](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L4) 的 `from ..mano import MANO` 使它随 `EHM_v2` 进入推理链路。"是否孤儿"要看**传递可达性**，不是只看入口的直接 import。

**练习 3**：如果要把 PEAR 的网络部分（不含 EHM、渲染）拷到另一个项目里，最少需要复制哪些目录？

**参考答案**：`models/backbones/`、`models/smplx/`、`models/pipeline/ehm_pipeline.py`，再加上 `utils/pipeline_utils.py`（`ehm_pipeline.py` 顶部 `from utils.pipeline_utils import *`）和 `configs/infer.yaml`。注意 `smplx_head.py` 还依赖 `models/modules/ehm`（[smplx_head.py:L12-L13](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/smplx_head.py#L12-L13) import 了 `SMPLXV2` 与 `EHM_v2`），所以严格说 `models/modules/ehm` 也得带上——这说明该项目的模块耦合度不低，复制式迁移并不轻松。

### 4.3 dataset 与 utils 子包职责划分

#### 4.3.1 概念说明

**`dataset/`：训练数据管线（仅训练用）**

| 文件 | 角色 | 是否被入口引用 |
| --- | --- | --- |
| `webdata_loader.py` | WebDataset 流式加载：tar 分片 → `MixedWebDataset` 混合 → `example_formatter` 整形样本 | 是（`train_ehms.py`） |
| `dataset_utils.py` | 图像/关键点级工具（裁剪、翻转、旋转增强），改编自 EpipolarPose | 被 `webdata_loader.py` 使用 |
| `webdata_loader_render.py` | 另一版数据加载器（渲染相关） | 否（孤儿） |
| `__init__.py` | 包出口 | ⚠️ 有问题，见下 |

**`utils/`：横跨推理与训练的工具箱**，按用途分成五组：

| 分组 | 文件 | 一句话职责 |
| --- | --- | --- |
| 配置与运行 | `general_utils.py` | `ConfigDict`（YAML→只读 OmegaConf）、`add_extra_cfgs`、`device_parser`、进度条 `rtqdm` |
| 张量与几何 | `pipeline_utils.py`、`graphics.py`、`graphics_utils.py` | `to_tensor`、`perspective_projection`、`GS_Camera`、`GS_BaseMeshRenderer` |
| 旋转与人体 | `rotation_converter.py`、`smplx2smpl_joints.py`、`helper.py` | 6D↔轴角↔矩阵转换、SMPL-X→SMPL 关节映射、`face_vertices` 等 |
| 可视化与 IO | `draw.py`、`vis_mesh.py`、`get_video.py`、`get_frames.py` | 画关键点/网格、图片拼视频 |
| 数据后端 | `lmdb.py` | `LMDBEngine`（webdataset 配套的键值缓存） |
| 其他 | `ema.py`、`loss_utils.py`、`camera_utils.py`、`system_utils.py`、`render_nvdiffrast.py`、`bbox.py` | EMA、辅助损失、相机、目录、备用渲染器、**遗留 bbox 工具** |

`configs/` 虽小但关键：`infer.yaml` 只有 MODEL/BACKBONE/HEAD/TRAIN/DATASET 五段，`train.yaml` 在此之上多出 OPTIMIZE 段（优化器与学习率）。这两个文件是所有入口的"启动参数源"。

#### 4.3.2 核心流程

训练数据的流动（详见 u5-l1）：

```
ehms_datasets/000000.tar
  → wds.WebDataset 流式读取（load_tars_as_wds）
  → MixedWebDataset / RandomMix 多数据集加权混合
  → example_formatter：把 annotation 裁成 256 patch，同步变换关键点与参数
  → torch DataLoader（train_ehms.py 里 batch_size=cfg.TRAIN.batch_size）
  → OurPipeline.run_fit()
```

utils 的被依赖关系（谁在推理链路上）：

```
推理链路必用：general_utils(ConfigDict)、pipeline_utils(to_tensor)、
             graphics_utils(GS_Camera、GS_BaseMeshRenderer)、helper(face_vertices)
仅训练链路：draw.py、vis_mesh.py、smplx2smpl_joints.py、ema.py（由 pipeline.py 引入）
数据链路专用：lmdb.py（由 webdata_loader.py 引入）
无人引用（孤儿）：bbox.py、camera_utils.py、loss_utils.py、system_utils.py、
               render_nvdiffrast.py、get_frames.py（以 grep 结果为准）
```

#### 4.3.3 源码精读

**（1）配置工具：整个项目的"总开关面板"**

[utils/general_utils.py:L12](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/general_utils.py#L12) 定义 `class ConfigDict(dict)`；[utils/general_utils.py:L66](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/general_utils.py#L66) 的 `add_extra_cfgs` 负责把额外配置段并入；[utils/general_utils.py:L76](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/general_utils.py#L76) 的 `read_config` 读 YAML。四个入口脚本无一例外都经过 `ConfigDict(...) → add_extra_cfgs(...)` 两步。

一个值得玩味的细节：`device_parser` 在文件里**定义了两次**——[utils/general_utils.py:L256](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/general_utils.py#L256) 和 [utils/general_utils.py:L271](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/general_utils.py#L271)，两份实现相同，后者覆盖前者。这类"重复定义"在研究代码里不少见，读源码时要习惯以**最后一次定义为准**。

**（2）数据集构建函数：训练入口的"库入口"**

[dataset/webdata_loader.py:L394](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/dataset/webdata_loader.py#L394)

```python
def build_web_tracked_data(cfg_dataset, split, shuffle=True):
```

这是 `dataset/` 子包唯一被入口脚本引用的符号（[train_ehms.py:L9](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/train_ehms.py#L9)）。它内部串联 [L37](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/dataset/webdata_loader.py#L37) 的 `MixedWebDataset`、[L186](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/dataset/webdata_loader.py#L186) 的 `example_formatter`、[L321](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/dataset/webdata_loader.py#L321) 的 `load_tars_as_wds`。

**（3）一个必须知道的"坑"：`dataset/__init__.py` 引用了不存在的文件**

[dataset/__init__.py:L1-L3](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/dataset/__init__.py#L1-L3)

```python
from .data_loader import TrackedData, TrackedData_infer
from .data_loader2 import TrackedData2
from .data_loader3 import TrackedData3
```

`git ls-files` 显示仓库里**并不存在** `data_loader.py` / `data_loader2.py` / `data_loader3.py`，只有 `__pycache__/` 里残留的 `.pyc`。Python 的包导入机制要求 `import dataset.webdata_loader` 先执行 `dataset/__init__.py`，因此按理说训练入口会在这一步抛 `ModuleNotFoundError`——作者本地显然有这三个未提交的文件。**待本地验证**：在你克隆的仓库里运行 `python -c "import dataset"`，如果报错，把 `dataset/__init__.py` 的前三行注释掉即可绕过（这属于你本地的临时改动，请勿提交）。这是读研究型仓库最常见的体验：开源版本与作者工作区之间存在缝隙。

**（4）孤儿模块也会"伪装"成活代码**

[utils/bbox.py:L1-L4](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/bbox.py#L1-L4)

```python
import torch
from models.modules.ehm import EHM_v2
from models.pipeline.ehm_pipeline import Ehm_Pipeline
import os
```

`utils/bbox.py` 顶部 import 了 `EHM_v2` 和 `Ehm_Pipeline`，看起来像推理代码；但它后面定义的其实是一组 bbox 工具函数（`flex_resize_video`、`fit_bbox_to_aspect_ratio` 等，从 [L28](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/bbox.py#L28) 开始），而且**没有任何文件 import `utils.bbox`**。教训：判断模块死活不能只看它 import 了什么（"依赖别人"不代表"被人依赖"），要反向 grep 它自己被谁引用。

**（5）requirements.txt 印证依赖分工**

[requirements.txt:L1-L4](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/requirements.txt#L1-L4) 锁定 `torch==2.0.1+cu118` 等 PyTorch 三件套；文件后半部分能对上各入口的第三方依赖：`ultralytics`（YOLO，inference_images）、`lightning`（训练与 `Ehm_Pipeline` 的基类）、`gradio==4.44.1`（app.py）、`webdataset` 经由其他包间接引入、`smplx==0.1.28`（训练装配里 `from smplx import SMPL, SMPLX`）。pytorch3d 与 chumpy 按上一讲的说明需单独安装，不在这份清单里。

#### 4.3.4 代码实践：反查 utils 每个文件的"入度"

1. **实践目标**：为 `utils/` 下每个 `.py` 文件统计被引用次数（入度），把 4.3.1 的表格变成你自己的实证结论。
2. **操作步骤**：

   ```bash
   for f in utils/*.py; do
       mod=$(basename "$f" .py)
       n=$(grep -rnE "(from|import) +utils\.$mod\b" --include=*.py . \
           | grep -v __pycache__ | grep -v ipynb_checkpoints | grep -v "^./PEAR-tutorial" | wc -l)
       echo "$n  $f"
   done | sort -rn
   ```

   然后单独验证几个可疑对象：

   ```bash
   grep -rn "utils.bbox\|utils.camera_utils\|utils.ema" --include=*.py . | grep -v __pycache__
   grep -rn "from utils.lmdb\|utils.lmdb" --include=*.py dataset/ | grep -v __pycache__
   ```

3. **需要观察的现象**：`utils/` 内部还有自引用（如 `camera_utils.py` 引 `graphics_utils`），排序后入度最高的通常是 `graphics_utils`、`general_utils`、`pipeline_utils`；若干文件入度为 0。
4. **预期结果**：入度 0 的文件即孤儿模块；`utils/lmdb.py` 只被 `dataset/webdata_loader*.py` 引用，证明它是数据链路专用；`utils/ema.py`、`utils/draw.py` 等只被 `models/pipeline/pipeline.py` 引用，证明它们只服务训练。
5. 待本地验证：不同 shell 对 `\b` 词边界支持不同，若计数异常可用 `grep -rn "utils\.$mod" ...` 简化。

#### 4.3.5 小练习与答案

**练习 1**：`dataset/webdata_loader.py` 属于推理链路还是训练链路？依据是什么？

**参考答案**：只属于训练链路。三个推理入口（app.py、inference_images.py、inference_wo_detect.py）的 import 区都没有它，只有 [train_ehms.py:L9](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/train_ehms.py#L9) `from dataset.webdata_loader import build_web_tracked_data`。这也解释了为什么只做推理的用户不需要下载训练用的 tar 数据集。

**练习 2**：`utils/general_utils.py` 为什么会被四个入口同时引用？它提供了什么不可替代的东西？

**参考答案**：它提供 `ConfigDict` + `add_extra_cfgs` 这对配置加载入口（[utils/general_utils.py:L12](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/general_utils.py#L12)、[L66](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/general_utils.py#L66)），任何入口都要先把 YAML 变成 `meta_cfg` 才能构建模型；训练和多卡推理还需要 `device_parser`。配置是所有链路的公共上游，所以它入度最高、最稳定。

**练习 3**：你想给 PEAR 接一个自定义的数据集，应该新建文件还是改现有文件？

**参考答案**：新建。参照 `dataset/webdata_loader.py` 的结构（`load_tars_as_wds` → `MixedWebDataset` → `example_formatter` → `build_web_tracked_data`），写一个自己的构建函数返回同样的样本字段即可；`train_ehms.py` 只依赖 `build_web_tracked_data(cfg_dataset, split)` 这个函数签名，把 import 指向新文件即可切换。不要动 `dataset/__init__.py`（它当前引用的 `data_loader*.py` 本来就缺失）。

## 5. 综合实践

**任务：手绘 PEAR 仓库的"分层代码地图"，并用 grep 全程验证。**

1. **实践目标**：把本讲三个模块的结论合成一张你自己的地图，作为后续所有讲义的导航页。
2. **操作步骤**：
   1. 运行 `git ls-files "*.py" | grep -v __pycache__ | grep -v ipynb_checkpoints > all_py.txt`，得到干净的文件清单（约 50 个文件）。
   2. 在纸上或笔记软件里画目录树，把每个 `.py` 文件标上七类标签之一：**入口 / 骨干 / 解码头 / 人体模型 / 渲染 / 数据 / 工具**，另加第八类 **孤儿**（无入口可达）。
   3. 用本讲 4.1.4、4.2.4、4.3.4 的三条 grep 命令逐类验证，把验证失败的条目改到正确类别。
   4. 在地图上用三种颜色笔标出三条链路：`app.py` 链路、`inference_images.py` 链路、`train_ehms.py` 链路，公共部分（backbone → head → EHM_v2 → Renderer2）用同一种颜色。
3. **需要观察的现象**：三条链路在 `models/pipeline/ehm_pipeline.py`、`models/modules/ehm/EHM_v2.py`、`models/modules/renderer/body_renderer.py` 三处汇合；`dataset/` 只被训练链路穿越；`models/vitdet/`、`models/modules/human_matting/`、`utils/bbox.py` 等不被任何链路穿越。
4. **预期结果**：一张八分类、三链路的目录树，其中"推理公共链路"5 个关键模块（ViT、SMPLXTransformerDecoderHead、Ehm_Pipeline、EHM_v2、Renderer2）被清晰识别出来——它们正是单元三、单元四要精读的对象。
5. 本实践为源码阅读型任务，不需要 GPU，也不需要跑通模型。

## 6. 本讲小结

- 根目录有 **4 个入口脚本**：`app.py`（Gradio 视频演示）、`inference_images.py`（多人 + YOLOv8 检测）、`inference_wo_detect.py`（单人无检测）、`train_ehms.py`（训练）；README 只写了其中三条命令。
- 三个推理入口共享同一段"配置 → 渲染器 → 下载权重 → Ehm_Pipeline → EHM_v2"的复制粘贴式组装代码；最小推理链路只依赖 5 个项目内模块。
- `models/` 五个子包按数据流分工：`backbones`（ViT 骨干）→ `smplx`（解码头网络）→ `modules`（SMPL-X/FLAME/MANO 人体模型 + 渲染器）→ `pipeline`（组装与损失）；`vitdet` 是被 YOLOv8 取代的遗留检测器。
- `models/smplx/` 是"预测参数的网络头"，`models/modules/smplx/` 是"参数变网格的官方层"，两者都被 `EHM_v2` 拉入推理链路，不可混淆。
- `dataset/` 只服务训练；`utils/` 横跨两链，其中 `general_utils`、`pipeline_utils`、`graphics_utils`、`helper` 是推理必经，`draw`、`vis_mesh`、`smplx2smpl_joints` 等仅训练使用。
- 用"反向 grep 入度"识别出了一批孤儿模块（`utils/bbox.py`、`models/modules/human_matting/`、`dataset/webdata_loader_render.py` 等）；还发现 `dataset/__init__.py` 引用了仓库中不存在的 `data_loader*.py`，这是开源版本与作者工作区之间的缝隙（待本地验证）。

## 7. 下一步学习建议

- 下一讲 **u1-l4（跑通第一次推理：三个入口脚本）**：在本讲地图的指引下实际运行三个推理入口，把地图上的文件与磁盘上的输出产物一一对应。
- 若你想先理解"配置如何驱动模型构建"，可跳读 **u2-l1（ConfigDict 配置系统）**，它精读本讲反复出现的 `ConfigDict` / `add_extra_cfgs` / `device_parser`。
- 推荐继续阅读的源码（按本讲地图顺序）：[models/pipeline/ehm_pipeline.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/ehm_pipeline.py)（全文仅 50 余行，适合整读）→ [models/modules/ehm/EHM_v2.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py) 的 import 区与 `forward` 签名 → [models/modules/renderer/body_renderer.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/renderer/body_renderer.py)。
- 阅读技巧：此后每读一个新文件，先看它的 import 区（本讲反复示范的方法），30 秒内就能判断它属于哪条链路、依赖谁、是否可独立测试。
