# 二次开发实践：参数导出、检测器替换与接入自己的数据

## 1. 本讲目标

这是学习手册的最后一讲，也是收口讲。前面四个单元把 PEAR 的推理链路、网络结构、人体模型、渲染器与训练循环逐层拆开，本讲把这些知识换成三件「带得走」的能力：

1. **入口改造模式**：看穿三个入口脚本「复制粘贴式」的组装套路，把它提炼成可复用的函数，让 PEAR 能作为库嵌进你自己的管线（逐帧取参数）。
2. **参数导出格式设计**：把每帧的 `body_param` / `flame_param` / `pd_cam` 与 `faces` 序列化成自包含的 npz 文件，理解每个字段的形状与语义契约。
3. **离线渲染复用**：只读 npz（不加载网络、不跑前向），用 `EHM_v2` + `Renderer2` 重建网格并渲染成视频，验证导出数据的自包含性。

另有两个「了解级」主题：`models/vitdet` 提供的 detectron2 备选检测入口 `build_detector`（主链路现役的是 YOLOv8），以及如何按 webdataset 格式准备自己的训练数据。

学完本讲，你应该能独立回答：「我想用 PEAR 的输出做下游任务（驱动动画、做分析、训练别的模型），最少需要保存什么、怎么恢复？」

## 2. 前置知识

本讲是终章，默认你已读过前置讲义，这里只唤醒最关键的几条认知：

- **推理三件套**：`ehm_model(img_patch)` 输出 `body_param`（SMPL-X 身体侧参数，11 键中 3 键恒 `None`）、`flame_param`（FLAME 头部参数，6 键）、`pd_cam`（(B,4,4) 相机 RT 矩阵）；`ehm(body_param, flame_param)` 把参数变成 10475 顶点的统一网格（u2-l5、u4-l4）。
- **权重与资产的分工**：`pear_model.pt` 顶层只有 `backbone` 与 `head` 两段 state dict；`EHM_v2` 与 `Renderer2` 由 `assets/` 下的 SMPL-X/FLAME 资产构造、零可学习参数、不进 checkpoint（u1-l3、u4-l1）。这是「离线重建不需要网络权重」的根据。
- **全仓常数纪律**：焦距 24、渲染画布 1024，在 `build_cameras_kwargs`、`Renderer2` 构造、投影矩阵中必须处处一致（u3-l4、u4-l5）。
- **视频两遍结构**：`app.py` 的 `mesh_inference` 第一遍逐帧只收集 body/flame/cam 三组参数序列，参数空间平滑后第二遍逐帧 EHM 重建、渲染、编码（u2-l4、u5-l4）。本讲的「导出 / 回放」正是把这个两遍结构拆成两个互相独立的脚本。
- **弱透视相机**：`pd_cam` 的深度维由 \( z = f/s \)（f=24）从尺度换算而来，旋转固定为 diag(-1,-1,1)（u3-l4）。

新术语只有两个：

- **npz**：NumPy 的压缩存档格式，`np.savez_compressed` 把多个命名数组打进一个文件，`np.load` 按键取回，适合存「一帧多个数组、帧数可变」的参数级结果。
- **detectron2**：Facebook（Meta）的目标检测框架，`models/vitdet` 用它的 LazyConfig 机制构建 ViTDet 检测器；注意它**不在 requirements.txt 里**。

## 3. 本讲源码地图

| 文件 | 在本讲中的角色 |
| --- | --- |
| [inference_wo_detect.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py) | 最短推理入口，导出脚本的主要参考模板 |
| [inference_images.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_images.py) | 多人入口：YOLOv8 检测器调用方式（vitdet 替换的对照物） |
| [app.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py) | 视频两遍处理结构与「未打通的参数导出」，回放脚本的参考模板 |
| [models/vitdet/__init__.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/vitdet/__init__.py) | detectron2 备选检测入口 `build_detector` |
| [models/vitdet/utils_detectron2.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/vitdet/utils_detectron2.py) | `DefaultPredictor_Lazy`：vitdet 检测器的调用协议 |
| [models/pipeline/ehm_pipeline.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/ehm_pipeline.py) | 网络前向的边界（导出数据的「生产端」） |
| [models/smplx/smplx_head.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/smplx_head.py) | 输出字典每个字段的形状与语义的权威定义处 |
| [models/modules/ehm/EHM_v2.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py) | 回放脚本的「消费端」：参数 → 网格 |
| [models/modules/renderer/body_renderer.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/renderer/body_renderer.py) | `Renderer2` 与 `faces` 拓扑 buffer（npz 中 faces 的来源） |
| [utils/graphics_utils.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/graphics_utils.py) | `GS_BaseMeshRenderer.render_mesh` 与相机常数 |
| [dataset/webdata_loader.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/dataset/webdata_loader.py) | webdataset 样本契约（准备自己训练数据的说明书） |
| [dataset/\_\_init\_\_.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/dataset/__init__.py) | 导入训练管线时的一个坑（引用了缺失文件） |
| [configs/train.yaml](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/configs/train.yaml) | 数据集注册格式与示例 tar 路径 |
| [utils/get_video.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/get_video.py) | 图片序列合成视频的工具函数 |

## 4. 核心概念与源码讲解

### 4.1 入口改造模式：把 PEAR 当库用

#### 4.1.1 概念说明

PEAR 的三个推理入口（`app.py`、`inference_images.py`、`inference_wo_detect.py`）没有共享的「推理库」，而是各自把同一套组装代码复制了一遍。对研究代码这很正常，但对二次开发是噪音：你想换一个数据源，就得在复制的几十行里小心翼翼地改。

入口改造模式的核心观察是：**整条链路只有一段真正依赖网络权重，其余全部由静态资产驱动**。把它切成两段，就得到可复用的形状：

- **感知段**：`Ehm_Pipeline` 前向。需要 `pear_model.pt` 权重与 GPU，输入 256×256 人体 patch，输出三键参数字典。这是唯一的「黑盒」，也是唯一需要下载权重的一段。
- **重建段**：`EHM_v2` 参数 → 网格、`Renderer2` 网格 → 图像。只需要 `assets/` 下的 SMPL-X/FLAME 资产，零权重、零训练状态，且**梯度可以穿过**（训练循环正是这么用的，u5-l2）。

改造的第二个要点是**常数纪律**：焦距 24 与画布 1024 出现在至少三处（相机 kwargs、渲染器构造、投影矩阵），任何一处改动都会让像素对齐悄悄失效。

#### 4.1.2 核心流程

三个入口的组装套路可以抽象成同一份伪代码：

```text
# 一次性装配（约 20 行）
cfg        = ConfigDict('configs/infer.yaml') + add_extra_cfgs
renderer   = Renderer2("assets/SMPLX", 1024, focal_length=24.0)
ckpt       = hf_hub_download("BestWJH/PEAR_models", "pear_model.pt")
ehm_model  = Ehm_Pipeline(cfg); 分段加载 backbone/head; .cuda()
ehm        = EHM_v2("assets/FLAME", "assets/SMPLX").cuda()
lights     = PointLights(location=[[0, -1, -10]])

# 每帧循环（三个入口的差异只在这一行）
patch      = 得到 256×256 人体 patch
outputs    = ehm_model(patch)                                    # 感知段
mesh_dict  = ehm(outputs['body_param'], outputs['flame_param']) # 重建段（前半）
camera     = GS_Camera(focal=24, size=1024, R/T 来自 outputs['pd_cam'])
image      = renderer.render_mesh(mesh_dict['vertices'], camera, lights)  # 重建段（后半）
```

「入口」的本质就是 **patch 的获取策略**：`inference_wo_detect.py` 用 `pad_and_resize` 整图塞入（假设单人居中），`inference_images.py` 用检测框 + 仿射裁剪（多人），`app.py` 用 decord 逐帧取图再 `pad_and_resize`（视频）。改造入口 = 换掉「得到 patch」这一行，其余原样保留。

#### 4.1.3 源码精读

先看最短入口的装配段。[inference_wo_detect.py:L49-L68](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L49-L68) 依次完成：读配置、建 1024 渲染器、`hf_hub_download` 自动下载权重、构造 `Ehm_Pipeline`、构造资产驱动的 `EHM_v2`、放 CUDA、建灯光。其中 [inference_wo_detect.py:L58-L62](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L58-L62) 这五行就是全仓通用的权重加载约定——下载 checkpoint、`torch.load`、再分别对 backbone 与 head 调 `load_state_dict(..., strict=False)`。

同一段代码在 [inference_images.py:L249-L276](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_images.py#L249-L276) 几乎逐字重复（只是多了 YOLO 初始化），在 [app.py:L127-L146](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L127-L146) 又重复第三遍——这就是「复制式组装」的实锤，也是改造模式要消除的对象。

感知段的边界在 [models/pipeline/ehm_pipeline.py:L29-L52](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/ehm_pipeline.py#L29-L52)：`forward` 只做归一化（L46）、`x[:, :, :, 32:-32]` 裁剪把 256×256 裁成 256×192（L47）、骨干前向、head 组装输出字典。注意 docstring 里写的 `'pd_cam': shape (B, 3)` 已经过时——实际返回 (B,4,4) 的 RT 矩阵（u3-l4），「注释漂移」又一例。

每帧循环的样板在 [inference_wo_detect.py:L77-L97](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L77-L97)：读图 → `pad_and_resize(img, 256)` → `to_tensor` 加 `/255` 与维度重排（L81-L82）→ 前向（L85）→ `ehm()` 重建（L86）→ 用 `outputs['pd_cam']` 切 R/T 构造 `GS_Camera`（L91）→ `render_mesh`（L93）。相机常数固化在 [inference_wo_detect.py:L36-L43](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L36-L43) 的 `build_cameras_kwargs` 里。

视频版入口证明这套循环可以直接换数据源：[app.py:L378-L390](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L378-L390) 用 decord 逐帧取图，前向三行与最短入口一字不差，只是把每帧输出 `append` 进 `body_sequence` / `flame_sequence` / `cam_sequence` 三条序列。**这三行 append 就是本讲导出格式的雏形。**

#### 4.1.4 代码实践

**实践目标**：把复制式装配提炼成两个函数，验证「感知段可整体替换、重建段保持不动」。

**操作步骤**（示例代码，非项目原有文件；建议新建文件实验，不要改仓库源码）：

1. 从 [inference_wo_detect.py:L49-L68](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L49-L68) 抄下装配段，包成 `def build_pear(): return ehm_model, ehm, renderer, lights`；
2. 从 [inference_wo_detect.py:L77-L94](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L77-L94) 抄下循环体，包成 `def run_frame(ehm_model, ehm, renderer, lights, frame_rgb) -> (outputs, mesh_dict)`；
3. 写一个五行 `main`：用 `decord.VideoReader`（参考 [app.py:L361](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L361)）读 `example/example_1.mp4` 前 10 帧，逐帧调 `run_frame`，打印每帧 `outputs['pd_cam'].shape`。

**需要观察的现象**：10 帧全部打印 `(1, 4, 4)`；装配只发生一次，循环内没有任何权重相关操作。

**预期结果**：wrapper 输出与原脚本一致（同一帧渲染图肉眼对比）。注意颜色通道：`inference_wo_detect.py` 读图未做 BGR→RGB（u2-l2 指出的遗留不一致），而 decord 输出即 RGB——你的 wrapper 应统一按 RGB 处理（与训练、与 `app.py` 一致）。完整运行**待本地验证**（需 GPU 与模型资产）。

#### 4.1.5 小练习与答案

**练习 1**：三个入口里，「入口」这个概念的全部差异落在哪一行？改造一个「从网络摄像头取流」的新入口，你需要写什么、不需要写什么？

<details><summary>参考答案</summary>

差异只在「得到 256×256 patch」：`inference_wo_detect.py` 的 `pad_and_resize`（L80）、`inference_images.py` 的 YOLO 检测 + `generate_patch_image`（L298-L332）、`app.py` 的 decord 帧 + `pad_and_resize`（L379-L383）。摄像头入口只需把取帧换成 `cv2.VideoCapture.read()` + `pad_and_resize`；装配段、前向、重建、渲染全部照抄，无需重写。
</details>

**练习 2**：为什么说 `EHM_v2` 和 `Renderer2` 是「资产驱动」而非「权重驱动」？从 checkpoint 角度给出证据。

<details><summary>参考答案</summary>

`EHM_v2` 的 SMPL-X/FLAME 张量全部是 `register_buffer`（u4-l1），`Renderer2` 只注册 `faces`/UV 等 buffer（[body_renderer.py:L113-L125](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/renderer/body_renderer.py#L113-L125)）；二者没有任何可学习参数，而 `pear_model.pt` 顶层只有 `backbone` 和 `head` 两段（加载代码也只调用了这两段）。所以离线重建不需要 checkpoint，反之换 checkpoint 也不用重建它们。
</details>

**练习 3**：如果把渲染画布从 1024 改成 512，至少要同步改哪几处？漏改会怎样？

<details><summary>参考答案</summary>

`build_cameras_kwargs` 里的 `screen_size`、`Renderer2` 构造的 `image_size`，以及任何手写 `1024` 的 resize/回贴逻辑。漏改的后果不是报错而是**静默错位**：投影内参按 1024 算、画布却是 512，网格位置或缩放错误，像素对齐失效——最难排查的一类 bug。
</details>

### 4.2 检测器替换：models/vitdet 的 detectron2 备选入口

#### 4.2.1 概念说明

多人推理需要一个人体检测器产出 bbox。当前 `inference_images.py` 用的是 **YOLOv8**（ultralytics 包，requirements.txt 已列出），而仓库里还留着一整套备选实现 `models/vitdet`：基于 **detectron2 + ViTDet**（Cascade Mask R-CNN，ViT-H 骨干）。这套代码源自 4D-Humans 等工作的通用做法（文件头注释标明改自 4D-Humans 的 `utils_detectron2.py`），PEAR 保留了它但主链路没有使用——u1-l3 判定过的「孤儿模块」。

了解它的价值有二：一是想对比不同检测器对最终网格质量的影响时，入口是现成的；二是它示范了「检测器接口协议」——替换检测器的全部工作就是适配协议差异。

注意成本：`detectron2` 不在 requirements.txt 中（列出的 `fvcore`、`iopath` 只是它的伴生依赖），走这条路要先自行安装 detectron2，与 torch 2.0.1 的版本兼容**待本地验证**。

#### 4.2.2 核心流程

两套检测器的协议差异一览：

| 维度 | YOLOv8（现役） | vitdet / `DefaultPredictor_Lazy`（备选） |
| --- | --- | --- |
| 初始化 | `YOLO('./model_zoo/yolov8x.pt')`，本地权重 | `build_detector(bs, max_img_size, device)`，权重从官方 URL 自动下载 |
| 调用 | `detector.predict(img, classes=0, conf=0.5, ...)` | `detector([img1, ...])`，传**图像列表** |
| 返回 | `[0].boxes.xyxy` 直接拿 (N,4) 框 | `(preds, downsample_ratios)`，preds 是逐图 dict |
| 人体过滤 | `classes=0` 参数内完成 | 需手动过滤 `pred_classes == 0`（COCO person） |
| 置信度阈值 | `conf=0.5`（调用时改） | `test_score_thresh = 0.25`（构造时写进配置） |
| 尺寸策略 | 上游 `load_img` 放大 2 倍 | 内部超过 `max_img_size` 自动**缩小**并返回缩放比 |

替换的适配层：把 YOLO 那一行 predict 换成「调 vitdet → 过滤 person → 必要时把框坐标除以 `downsample_ratio` 还原到原图尺度」，后续 `process_bbox` → `generate_patch_image` → 前向的流水线一字不改。

#### 4.2.3 源码精读

备选入口本体极短。[models/vitdet/\_\_init\_\_.py:L7-L20](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/vitdet/__init__.py#L7-L20) 的 `build_detector(batch_size, max_img_size, device)` 做四件事：用 detectron2 的 `LazyConfig` 读同目录的 `cascade_mask_rcnn_vitdet_h_75ep.py`；把 `train.init_checkpoint` 指向官方 COCO 预训练 ViTDet 权重 URL（首次运行自动下载）；把三个级联 box predictor 的 `test_score_thresh` 统一调成 0.25（[models/vitdet/\_\_init\_\_.py:L11-L12](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/vitdet/__init__.py#L11-L12)）；返回 `DefaultPredictor_Lazy` 实例。

构造协议在 [models/vitdet/utils_detectron2.py:L141-L163](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/vitdet/utils_detectron2.py#L141-L163)：`instantiate` 出模型、用 `DetectionCheckpointer` 载入权重、从 dataloader mapper 取增广与图像格式，并断言输入格式为 RGB。调用逻辑在 [models/vitdet/utils_detectron2.py:L176-L190](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/vitdet/utils_detectron2.py#L176-L190)：对每张图，若最长边超过 `max_img_size` 就按比例缩小并记录 `downsample_ratios`——**这意味着返回的框坐标在缩小后的图上，接回原图时必须除回这个比例**。输出整理在 [models/vitdet/utils_detectron2.py:L211-L215](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/vitdet/utils_detectron2.py#L211-L215)：每张图一个 dict，含 `pred_classes`、`scores`、`pred_boxes`（xyxy 像素坐标）。

对照现役 YOLO 路线：[inference_images.py:L281-L282](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_images.py#L281-L282) 从本地 `./model_zoo/yolov8x.pt` 初始化；[inference_images.py:L298-L303](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_images.py#L298-L303) 一行 `predict` 拿到已过滤的 xyxy 框。替换点就是这一行；后续 `process_bbox` → `generate_patch_image` → 前向（[inference_images.py:L321-L338](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_images.py#L321-L338)）不动。

#### 4.2.4 代码实践

**实践目标**：不改主链路，用源码阅读方式确认「替换检测器只需适配一段胶水代码」。

**操作步骤**：

1. 打开 `models/vitdet/cascade_mask_rcnn_vitdet_h_75ep.py`，找到 `roi_heads.box_predictors` 相关配置，确认它确实是 Cascade Mask R-CNN 结构（纯阅读，不必运行）；
2. 对照 4.2.2 的表格写一段 10 行以内的胶水代码（示例代码，若已装 detectron2 可真跑）：

```python
# 示例代码：把 vitdet 输出整理成 inference_images.py 期望的 xyxy numpy 数组
from models.vitdet import build_detector
detector = build_detector(batch_size=1, max_img_size=8192, device='cuda')  # 大 max_img_size 可回避内部缩小
preds, ratios = detector([img_rgb])          # img_rgb: np.ndarray (H,W,3), RGB
inst = preds[0]
person = inst['pred_classes'] == 0           # COCO person
boxes = inst['pred_boxes'][person] / ratios[0]  # 还原到原图尺度
yolo_bbox = boxes.numpy()                    # 与 .boxes.xyxy 等价的 (N,4)
```

3. 检查你的版本是否处理了 `downsample_ratios`（上面用大 `max_img_size` 回避，是一种合法简化）。

**需要观察的现象**：胶水代码里完全没出现 `ehm_model`、`ehm`、`render_mesh`——检测替换与网络前向彻底解耦。

**预期结果**：适配层约 15 行，可与 4.1 的 wrapper 自由组合。detectron2 路线的实际检测效果**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`DefaultPredictor_Lazy` 为什么要返回 `downsample_ratios`，而 YOLO 路线不需要？

<details><summary>参考答案</summary>

vitdet 路线内部把超过 `max_img_size` 的图缩小了，框坐标落在缩小后的图上，必须除回比例才能对齐原图；YOLO 路线反其道而行（`load_img` 先放大 2 倍，检测器不改变尺寸），坐标天然在输入图坐标系里。
</details>

**练习 2**：两套检测器的人体置信度阈值分别是多少？分别在哪里设置？

<details><summary>参考答案</summary>

YOLO：`conf=0.5`，在调用处 [inference_images.py:L301](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_images.py#L301)；vitdet：`test_score_thresh = 0.25`，在构造时写进三个 box predictor 的配置 [models/vitdet/\_\_init\_\_.py:L11-L12](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/vitdet/__init__.py#L11-L12)。一个在推理时改，一个在构造时改——替换时别找错地方。
</details>

### 4.3 参数导出格式设计：逐帧结果的 npz 序列化

#### 4.3.1 概念说明

下游复用 PEAR，最经济的中间产物不是视频、也不是逐帧顶点，而是**参数级结果**。算一笔账：一帧的 `body_param` + `flame_param` + `pd_cam` 合计约 1100 个浮点数，而一帧顶点是 \( 10475 \times 3 = 31425 \) 个浮点数——

\[ \frac{724 + 364 + 16}{31425} = \frac{1104}{31425} \approx 3.5\% \]

参数级导出省约 30 倍存储，且保留**再编辑能力**：改一个关节旋转、调一个表情系数、对参数序列做 savgol 时序平滑（u5-l4 正是参数空间操作），都不需要重新跑网络。

但项目自带的导出其实**没有打通**：demo 落盘的 `results.npz` 里 `vertices` 恒为空数组。本模块设计一个真正自包含的格式。

#### 4.3.2 核心流程

导出格式的五条设计决策与理由：

1. **存原始（未平滑）参数**。平滑窗口是回放端的选择（u5-l4：3/7/21 各有取舍），烙进导出文件就剥夺了这种自由。
2. **嵌套 dict 拉直成带前缀的扁平键**。`body_param` 是 11 键嵌套 dict，npz 是扁平键值空间，故用 `body/global_pose`、`flame/jaw_params` 这样的键名；`eye_pose`/`jaw_pose`/`joints_offset` 三键恒为 `None`（u3-l3 审计），不存。
3. **相机只存 `pd_cam` (T,4,4)**。它已含 RT；内参虽是全仓常数，仍写进 meta——自包含的意义就是「读文件的人不需要知道仓库常数」。
4. **faces 存一份 (20908,3)**。渲染拓扑来自 `smplx_tex.obj`，存进 npz 后回放脚本连 obj 都不必读。
5. **meta 记录 fps、焦距、画布**。回放合成视频时 fps 必须与源一致（demo 硬编码 30 会变速，u5-l4）。

每帧字段的完整契约（批大小 1，第 0 维为帧数 T）：

| npz 键 | 形状 | 语义 |
| --- | --- | --- |
| `body/global_pose` | (T,1,3,3) | 全局旋转（旋转矩阵） |
| `body/body_pose` | (T,21,3,3) | 21 个身体关节旋转 |
| `body/left_hand_pose` | (T,15,3,3) | 左手 15 关节旋转 |
| `body/right_hand_pose` | (T,15,3,3) | 右手 15 关节旋转 |
| `body/hand_scale` | (T,3) | 手部缩放（当前下游未消费） |
| `body/head_scale` | (T,3) | FLAME 头三轴缩放 |
| `body/exp` | (T,50) | SMPL-X 表情系数 |
| `body/shape` | (T,200) | SMPL-X 体型系数 |
| `flame/eye_pose_params` | (T,6) | 双眼球旋转 |
| `flame/pose_params` | (T,3) | FLAME 全局旋转（EHM_v2 中被置零） |
| `flame/jaw_params` | (T,3) | 下颌旋转 |
| `flame/eyelid_params` | (T,2) | 左右眼睑开合 |
| `flame/expression_params` | (T,50) | FLAME 表情 |
| `flame/shape_params` | (T,300) | FLAME 头型 |
| `cam/pd_cam` | (T,4,4) | 相机 RT（旋转恒为 diag(-1,-1,1)） |
| `faces` | (20908,3) | 渲染拓扑（只覆盖前 10475 顶点） |
| `meta/fps` 等标量 | — | fps、焦距 24、画布 1024 |

#### 4.3.3 源码精读

字段的**权威定义处**在解码头。[models/smplx/smplx_head.py:L283-L300](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/smplx_head.py#L283-L300) 组装 `body_param`：312 维姿态输出按 6/126/90/90 切片过 `rot6d_to_rotmat` 得各旋转矩阵字段，6 维 scale 输出切成 `hand_scale`（前 3）与 `head_scale`（后 3），再加 `exp`(50)、`shape`(200) 与三个恒 `None` 键；[models/smplx/smplx_head.py:L271-L278](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/smplx_head.py#L271-L278) 组装 `flame_param` 六键；[models/smplx/smplx_head.py:L303-L317](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/smplx_head.py#L303-L317) 先把相机 3 维参数按 \( z = 24/s \) 换算深度（L303-L306），再经 `get_full_proj` 得到 4×4 RT，连同两个参数字典放进输出——`all_out['pd_cam'] = RT`，这就是导出时直接切 R/T 的依据。

「逐帧收集参数序列」的现成范式是 [app.py:L378-L390](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L378-L390)：三条序列各自 append 每帧输出。随后 [app.py:L393-L402](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L393-L402) 与 [app.py:L414-L423](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L414-L423) 用 `fields1`/`fields2` 两个列表把嵌套 dict 逐键 `torch.cat` 成 (T,…) 张量——这两个列表就是上表键名的出处，也印证 8+6 个有效字段。

项目自带导出「未打通」的证据：[app.py:L472](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L472) 收集顶点的语句被注释掉，于是 [app.py:L505-L507](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L505-L507) 落盘的 `results.npz` 中 `vertices` 恒为空数组——只有 `faces`（取自 `body_renderer.faces[0]`）是真的。本讲格式等于把这条断路重接，且接到参数一级而非顶点一级。

faces 的来源在 [models/modules/renderer/body_renderer.py:L106-L125](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/renderer/body_renderer.py#L106-L125)：`Renderer2` 构造时从 `smplx_tex.obj` 读拓扑，`faces.verts_idx` 加一个 batch 维后注册为 buffer。回顾 u4-l5：该拓扑 20908 个面片只引用前 10475 个 SMPL-X 顶点，末尾 120 个牙齿顶点不参与渲染。

#### 4.3.4 代码实践

**实践目标**：写出导出函数（示例代码，非项目原有文件），完整脚本见第 5 节综合实践，这里先验证契约。

**操作步骤**：

1. 复用 4.1 的 `build_pear()` 装配；
2. 逐帧循环（decord 取帧 + `pad_and_resize` 到 256 + 前向，照抄 [app.py:L378-L385](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L378-L385)），把每帧 14 个参数字段 `.detach().float().cpu().numpy()` 各自 append 进 list；
3. 循环外 `np.stack` 成 (T,…) 并按 4.3.2 契约表的扁平键名 `np.savez_compressed`，附 `faces` 与 meta；
4. `np.load` 后打印每个键与形状，逐项核对契约表。

**需要观察的现象**：`npz.files` 约 17 个键；所有 `body/*`、`flame/*`、`cam/pd_cam` 的第 0 维都等于帧数 T；`faces.shape == (20908, 3)`。

**预期结果**：`example/example_1.mp4` 约 3 秒、帧数几十到近百（具体**待本地验证**），导出 npz 仅几 MB。若某字段第 0 维不等于 T，说明循环里漏 append 或 append 了带多余维度的张量。

#### 4.3.5 小练习与答案

**练习 1**：为什么导出原始参数而不是平滑后的参数？举一个具体下游场景。

<details><summary>参考答案</summary>

平滑窗口是「抖动抑制 vs 动作失真」的取舍（u5-l4：3→7 收益显著，7→21 失真陡增），不同下游偏好不同窗口。做动作分析宁可少平滑保时间精度；做展示视频可加大窗口。存原始参数则回放端可任意重滤波，烙死就无法回头。
</details>

**练习 2**：`flame/pose_params` (T,3) 在回放中不会生效，为什么仍建议保留？

<details><summary>参考答案</summary>

EHM_v2 的 forward 会把 FLAME 的 global/neck pose 强制置零（[EHM_v2.py:L64-L65](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L64-L65)，头部朝向由身体蒙皮驱动，u4-l2/u4-l4）。但回放端组装的 flame 字典必须有这个键——[EHM_v2.py:L42](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L42) 直接索引它，缺键会 KeyError；且它忠实记录网络原始输出，便于调试对比。
</details>

**练习 3**：把导出粒度从「参数级」换成「顶点级」（直接存 (T,10475,3)），会失去什么、换来什么？

<details><summary>参考答案</summary>

失去再编辑能力：无法改关节/表情/相机后重建，无法做参数空间平滑（只能在顶点空间平滑，会产生 u5-l4 讨论过的重影问题）；存储约 30 倍。换来的是回放端不需要 EHM_v2 资产、也不必关心蒙皮实现。
</details>

### 4.4 离线渲染复用：不跑网络重建网格与视频

#### 4.4.1 概念说明

「离线」在本讲的含义是：**不加载网络权重、不跑 ViT 前向**，只凭 npz + 本地资产完成网格重建与渲染。这验证导出数据的自包含性——任何持有 npz 的人（将来的你、合作者、另一个项目）都能复现画面，而不需要 GPU 上 6.3 亿参数的骨干。

注意边界：pytorch3d 光栅化与 `GS_Camera` 投影矩阵仍依赖 CUDA（u3-l4），所以「离线」≠「无 GPU」，只是「无网络」。真正无 GPU 的查看路径是回放时把顶点导出为 obj（u4-l5 讲过 trimesh `process=False` 保顶点顺序），用任意网格查看器打开。

#### 4.4.2 核心流程

回放脚本的执行过程：

```text
读 npz → 逐帧 i：
  1. 从扁平键切出第 i 帧的 14 个参数字段（保留第 0 维=1，如 body/global_pose[i:i+1]）
  2. 组装 body_dict（11 键，3 个 None 键补 None）与 flame_dict（6 键）
  3. （可选）对某字段序列做 savgol 平滑后再切
  4. mesh_dict = ehm(body_dict, flame_dict)          # 参数 → 10475 顶点
  5. camera = GS_Camera(focal=meta['focal'], size=meta['size'],
                        R=pd_cam[i:i+1,:3,:3], T=pd_cam[i:i+1,:3,3])
  6. img = renderer.render_mesh(mesh_dict['vertices'][None,0,...], camera, lights)
  7. 转成可写帧，追加进帧列表
最后 imageio 以 meta['fps']、libx264+yuv420p+faststart 写出 mp4
```

两个关键点：第 1 步必须用 `[i:i+1]` 切片保留 batch 维（不能写 `[i]`），因为 `EHM_v2.forward` 用 `shape_params.shape[0]` 推 batch size（[EHM_v2.py:L99](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L99)）；第 6 步 `[None, 0, ...]` 把 (V,3) 顶点抬成 (1,V,3)，与 [inference_wo_detect.py:L93](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L93) 的写法一致。

#### 4.4.3 源码精读

回放的骨架在 `app.py` 第二遍循环里已经写好。[app.py:L443-L463](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L443-L463) 逐帧从平滑后的序列切片组装 `body_dict` / `flame_dict`——注意它把三个 `None` 键显式补上，这正是回放端必须照抄的细节；[app.py:L465-L467](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L465-L467) 三行完成 `ehm()` 重建、`GS_Camera` 构造（R/T 从 4×4 矩阵切片）、`render_mesh` 渲染。你的回放脚本与它的唯一区别是数据来源：它读内存里平滑后的张量，你读 npz。

`ehm()` 的输入契约在 [models/modules/ehm/EHM_v2.py:L34-L48](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L34-L48)（flame 侧读哪六键、batch 从 `shape_params.shape[0]` 推出）与 [models/modules/ehm/EHM_v2.py:L89-L99](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L89-L99)（body 侧 8 个有效键及其形状注释）。一个容易踩的坑：`pose_type` 形参实际**未被使用**（forward 全文没有第二个引用），真正决定「输入是轴角还是旋转矩阵」的是 [models/modules/ehm/EHM_v2.py:L109-L121](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L109-L121) 的形状探测——`global_pose` 是 3 维（(B,1,3)）就走 `pose2rot=True`，是 3×3 矩阵（(B,1,3,3)）就走 `pose2rot=False`。导出的姿态是旋转矩阵，回放时原样喂入即落在 `pose2rot=False` 分支，与在线推理完全一致。

渲染端：`render_mesh` 的完整逻辑在 [utils/graphics_utils.py:L780](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/graphics_utils.py#L780) 起的定义——`faces` 缺省用 `self.faces`（即 npz 那份拓扑的来源）、组 `TexturesVertex` 纯色、`Meshes`、光栅化加 Phong 着色（u4-l5 精读）。若不传 `cameras`，它会按 [utils/graphics_utils.py:L730](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/graphics_utils.py#L730) 的 `_build_cameras` 用 `transform_matrix` 现建相机——两条路等价，回放脚本沿用入口脚本的显式 `GS_Camera` 写法更清晰。

视频编码参数抄 [app.py:L485-L498](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L485-L498)：`libx264` + `yuv420p`（要求偶数宽高，故有裁奇数行的 `img2`）+ `faststart`（moov 前置，浏览器可流式播放）。若只要图片序列合成视频，也可走 [utils/get_video.py:L16-L47](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/get_video.py#L16-L47) 的 `images_to_video`（按文件名中的数字排序后 `mimwrite`），但它不控制像素格式，浏览器兼容性不如前者。

#### 4.4.4 代码实践

**实践目标**：写出 `replay_npz.py`（示例代码，非项目原有文件，完整版见第 5 节），只依赖 npz + assets 完成重建渲染。

**操作步骤**：

1. `np.load` 导出文件；构造 `EHM_v2("assets/FLAME", "assets/SMPLX").cuda()` 与 `Renderer2("assets/SMPLX", 1024, focal_length=24.0).cuda()`——**不 import `Ehm_Pipeline`、不下载权重**；
2. 按 4.4.2 流程逐帧重建渲染，帧追加进列表；
3. 用 `meta/fps` 与 yuv420p 参数写 `replay.mp4`；
4. 与在线推理（跑 `inference_wo_detect.py` 或 demo）的结果逐帧对比。

**需要观察的现象**：脚本启动明显变快（跳过 6.3 亿参数骨干的下载与加载）；回放视频与在线渲染在同一帧上肉眼无差异；回放前对 `body/global_pose` 做 `savgol_filter`（窗口 7）再重放，动作变顺滑但快速动作略有迟滞（复现 u5-l4 的结论）。

**预期结果**：`replay.mp4` 帧数与 npz 的 T 相等、与源视频不变速（fps 取自 meta）；把 `flame/jaw_params` 全置零再回放可见下颌闭合——证明数据自包含且可编辑。完整运行**待本地验证**（需 GPU + assets）。

#### 4.4.5 小练习与答案

**练习 1**：回放时把切片写成 `body/global_pose[i]`（丢掉 batch 维）会发生什么？

<details><summary>参考答案</summary>

`EHM_v2.forward` 在 [EHM_v2.py:L99](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L99) 用 `shape_params.shape[0]` 推 batch：shape 变成 (200,) 一维后，L123-L126 的补零拼接会因维度不匹配直接报错——好在是显式崩溃而非静默出错。正确写法是 `[i:i+1]` 保留长度 1 的第 0 维，这也是 app.py 第二遍循环的写法。
</details>

**练习 2**：回放脚本能否在纯 CPU 机器上运行？给出依据。

<details><summary>参考答案</summary>

不能完整运行：`GS_Camera` 的投影矩阵构造硬编码 CUDA（u3-l4），pytorch3d 的 GPU 光栅化也需要 GPU。CPU 替代路径：`ehm()` 的张量运算本身不强制 CUDA，可在 CPU 上算出顶点后用 trimesh 导出 obj，用查看器看网格，绕开光栅化。
</details>

**练习 3**：为什么回放 fps 必须从 meta 读取，而不能像 demo 那样硬编码 30？

<details><summary>参考答案</summary>

demo 把输出 fps 硬编码 30，遇到低帧率源视频会变速（u5-l4 指出的硬伤）。参数帧与源视频帧一一对应，只有用源 fps 回放才保持真实时间节奏；这正是 4.3.2 中「meta 必须记录 fps」的设计依据。
</details>

### 4.5 接入自己的数据：webdataset tar 的样本契约

#### 4.5.1 概念说明

二次开发的另一半是「喂自己的数据」。PEAR 训练侧用 webdataset 流式读取 tar 分片（u5-l1），所以接入自己的数据 = 打包出符合 `example_formatter` 期望的 tar，并在 `configs/train.yaml` 注册。

动手前有三个**坑**要先排掉：

1. **路径不一致**：README 让你把示例 tar 放在 `ehms_datasets/`，而配置注册的是 `./ehm_datasets/000000.tar`——以 [configs/train.yaml:L130](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/configs/train.yaml#L130) 为准（或改配置），否则训练找不到数据。
2. **依赖缺失**：`webdataset`、`pycocotools`、`yacs`、`braceexpand`、`matplotlib` 等训练侧依赖都不在 requirements.txt 中（推理只需 ultralytics，训练管线需要上面这些），跑训练前要自行补装（版本**待本地验证**）。
3. **导入即炸**：[dataset/\_\_init\_\_.py:L1-L3](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/dataset/__init__.py#L1-L3) 引用的 `data_loader.py` / `data_loader2.py` / `data_loader3.py` 源码在仓库中**不存在**（`__pycache__` 里只有陈旧的 `.pyc`，Python 3 不会从 `__pycache__` 导入无源码模块）。于是 [train_ehms.py:L9](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/train_ehms.py#L9) 的 `from dataset.webdata_loader import build_web_tracked_data` 会在包初始化时抛 `ModuleNotFoundError`。不改源码的绕行方案见 4.5.4。

#### 4.5.2 核心流程

数据流动路径（自 tar 到训练 batch）：

```text
tar 分片（000000.tar, 000001.tar, ...）
  → wds.WebDataset 流式读取 + decode("rgb8")
  → rename(jpg="jpg;jpeg;png")          # 每个样本的图像键统一叫 jpg
  → example_formatter                    # 逐样本：随机选一人、组装 GT、裁 256 patch
  → RandomMix 按权重混采 + with_epoch + shuffle
  → DataLoader
```

一个 tar 内每个「样本」是一组同前缀文件，核心是两类：一个图像文件（.jpg/.jpeg/.png）和一个 `annotation.pyd`——`.pyd` 是 webdataset 约定的 pickle 后缀，解码后直接是 Python 列表（一帧多人时列表里每人一个元素）。分片命名遵循 `{000000..000014}.tar` 的花括号展开约定（[dataset/webdata_loader.py:L82-L90](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/dataset/webdata_loader.py#L82-L90) 的 `expand_urls` 用 braceexpand 展开）。

#### 4.5.3 源码精读

管线组装在 [dataset/webdata_loader.py:L321-L345](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/dataset/webdata_loader.py#L321-L345)：`load_tars_as_wds` 建立 `WebDataset → decode("rgb8") → rename → apply_example_formatter` 四级流水线（训练分支还开 `resampled=True` 无限重采样，u5-l1）。混采与伪 epoch 在 [dataset/webdata_loader.py:L417-L435](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/dataset/webdata_loader.py#L417-L435)：权重归一化后交给 `wds.RandomMix`，再 `with_epoch(50_000).shuffle(1000)`。

样本契约的**消费端**就是 `example_formatter`，三段精读：

- [dataset/webdata_loader.py:L186-L219](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/dataset/webdata_loader.py#L186-L219)：`random.randint` 从 `annotation.pyd` 列表随机选一人（一条样本只训一个人）；从标注取 `scale`、`center` 作裁剪几何；有无 `smpl_keypoints_2d` 决定走 SMPL 44 点还是 DWPose 134 点监督通道（u5-l1 的双通道）。
- [dataset/webdata_loader.py:L123-L166](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/dataset/webdata_loader.py#L123-L166)：`fet_tracking_info_from_raw` 逐键消费标注——`smplx_params`（`global_pose`/`body_pose`/`left_hand_pose`/`right_hand_pose`/`camera_RT_params`）、`flame_params`（六键）、`id_params`（`smplx_shape` 补零到 200、`flame_shape` 补零到 300、`joints_offset`、`head_scale`、`hand_scale`）、三个有效标志 `head_valid`/`hand_valid`/`pose_valid`（L164-L166）。
- [dataset/webdata_loader.py:L222-L246](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/dataset/webdata_loader.py#L222-L246)：图像来自 `sample["jpg"]`；可选的 `mask`（pycocotools RLE）解码后与图像拼成 RGBA，一起进 `get_example` 做同一增广下的同步变换。

数据集注册格式见 [configs/train.yaml:L127-L132](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/configs/train.yaml#L127-L132)：`name` + `item.urls`（tar 路径或花括号模式）+ `epoch_size` + `weight`，多个数据集按归一化权重混采（大量真实数据集被注释，只留 Sample 示例）。示例 tar 的下载与放置说明在 [README.md:L115-L133](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/README.md#L115-L133)。

由此反推**自制 tar 的最小字段清单**：图像文件、`annotation.pyd`（列表，每人含 `smplx_params`、`flame_params`、`id_params`、`center`、`scale`、一组 2D/3D 关键点，可选 `mask`）与三个 valid 标志字段。注意 [dataset/webdata_loader.py:L42-L46](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/dataset/webdata_loader.py#L42-L46) 的 `pt_decoder` 与 [L72-L80](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/dataset/webdata_loader.py#L72-L80) 的 `decode_images` 都**没有被接线**（`load_tars_as_wds` 只调 `decode("rgb8")`），非图像字段的实际解码由 webdataset 内置解码器完成——具体键的后缀格式以官方示例 tar 为准（**待本地验证**）。

#### 4.5.4 代码实践

**实践目标**：不写打包代码，先用只读方式摸清官方示例 tar 的真实结构，并打通数据管线的导入。

**操作步骤**：

1. 按 [README.md:L121](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/README.md#L121) 的 Google Drive 链接下载示例 tar，放到 `./ehm_datasets/000000.tar`（以配置路径为准）；
2. 列目录观察样本结构：`tar -tf ehm_datasets/000000.tar | head -40`，看每个前缀下有哪些后缀；
3. 用下面的垫片（示例代码）绕过 `dataset/__init__.py` 的坏导入，再取一个样本检查字段：

```python
# 示例代码：不修改仓库源码，临时垫掉缺失的子模块
import sys, types
for name in ("dataset.data_loader", "dataset.data_loader2", "dataset.data_loader3"):
    stub = types.ModuleType(name)
    stub.TrackedData = stub.TrackedData_infer = stub.TrackedData2 = stub.TrackedData3 = object
    sys.modules[name] = stub          # 预注册后，__init__.py 的 from-import 直接命中

from dataset.webdata_loader import build_web_tracked_data
from utils.general_utils import ConfigDict, add_extra_cfgs
cfg = add_extra_cfgs(ConfigDict(model_config_path='configs/train.yaml'))
ds = build_web_tracked_data(cfg_dataset=cfg.DATASET, split='valid')
sample = next(iter(ds))
print(sorted(sample.keys()))
```

4. 对照 4.5.3 的字段清单逐一勾选，标注哪些字段与推理输出一一对应（`smplx_coeffs` ↔ `body_param`、`flame_coeffs` ↔ `flame_param`、`w2c_cam` ↔ `pd_cam`，u5-l1 的契约）。

**需要观察的现象**：tar 内文件按 `前缀.jpg`、`前缀.annotation.pyd` 等成组；样本键里有 `ehm_image`、`smplx_coeffs`、`dwpose_kp2d` 等；没有垫片时第 3 步第一步就抛 `ModuleNotFoundError: No module named 'dataset.data_loader'`。

**预期结果**：字段清单与 `sample.keys()` 对上，即拥有自制 tar 的完整规格。示例 tar 的真实键集合**待本地验证**（以上契约是从消费端源码反推的，以实际文件为准）。

#### 4.5.5 小练习与答案

**练习 1**：一帧多人时 `example_formatter` 为什么只随机选一个人训练，而不是全部都用？

<details><summary>参考答案</summary>

PEAR 的网络输入是「单人 256×256 patch」、输出也是单人参数字典；训练裁剪以所选人的 `center/scale` 为几何中心。多人全用需按人分别裁 patch 组 batch，而当前实现是「一图一人」的流式样本通道；随机选人等价于按人数加权采样。
</details>

**练习 2**：你的自制数据只有图像和 SMPL 参数、没有 FLAME 标注，tar 怎么填才不炸？

<details><summary>参考答案</summary>

`flame_params` 六键仍要给（可全零），并把 `head_valid` 置 0——训练侧的 `has_flame` 软门控（u5-l3）会把 FLAME 参数损失的权重压到接近零，网络自动只从身体监督学习。同理 `pose_valid`/`hand_valid` 控制身体与手部门控。
</details>

## 5. 综合实践

**任务**：实现「导出—回放」闭环，验证 PEAR 参数级结果的自包含性。这是本讲的收官实践，综合 4.1 的入口改造、4.3 的格式设计与 4.4 的离线重建。两个脚本均为示例代码、非项目原有文件，从仓库根目录启动。

### 第一步：`export_params.py` —— 逐帧推理导出 npz

```python
# export_params.py
# 用法: python export_params.py --video example/example_1.mp4 --out params.npz
import argparse, numpy as np, torch, decord, cv2
from huggingface_hub import hf_hub_download
from models.pipeline.ehm_pipeline import Ehm_Pipeline
from models.modules.renderer.body_renderer import Renderer2 as BodyRenderer
from utils.general_utils import ConfigDict, add_extra_cfgs
from utils.pipeline_utils import to_tensor

BODY_KEYS  = ["global_pose", "body_pose", "left_hand_pose", "right_hand_pose",
              "hand_scale", "head_scale", "exp", "shape"]
FLAME_KEYS = ["eye_pose_params", "pose_params", "jaw_params",
              "eyelid_params", "expression_params", "shape_params"]

def pad_and_resize(img, target_size=256):          # 抄自 inference_wo_detect.py L25-L34
    h, w = img.shape[:2]
    scale = min(target_size / h, target_size / w)
    new_w, new_h = int(w * scale), int(h * scale)
    resized = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
    padded = np.zeros((target_size, target_size, 3), dtype=np.uint8)
    padded[(target_size - new_h)//2:(target_size - new_h)//2 + new_h,
           (target_size - new_w)//2:(target_size - new_w)//2 + new_w] = resized
    return padded

@torch.no_grad()
def main(video_path, out_path):
    # ---- 一次性装配（照抄 inference_wo_detect.py L49-L68，去掉渲染相关）----
    meta_cfg = add_extra_cfgs(ConfigDict(model_config_path='configs/infer.yaml'))
    renderer = BodyRenderer("assets/SMPLX", 1024, focal_length=24.0).cuda()  # 只要 faces
    ckpt = hf_hub_download(repo_id="BestWJH/PEAR_models",
                           filename="pear_model.pt", repo_type="model")
    ehm_model = Ehm_Pipeline(meta_cfg)
    state = torch.load(ckpt, map_location='cpu', weights_only=True)
    ehm_model.backbone.load_state_dict(state['backbone'], strict=False)
    ehm_model.head.load_state_dict(state['head'], strict=False)
    ehm_model = ehm_model.cuda().eval()

    # ---- 逐帧前向：只收集参数（对齐 app.py L378-L390，不做 EHM/渲染）----
    vr = decord.VideoReader(video_path)
    fps = vr.get_avg_fps()
    seq = {f"body/{k}": [] for k in BODY_KEYS}
    seq.update({f"flame/{k}": [] for k in FLAME_KEYS})
    seq["cam/pd_cam"] = []
    for i in range(len(vr)):
        frame = vr[i].asnumpy()                                  # RGB
        patch = pad_and_resize(frame, target_size=256)
        patch = torch.permute(to_tensor(patch, 'cuda:0') / 255, (2, 0, 1)).unsqueeze(0)
        out = ehm_model(patch)
        for k in BODY_KEYS:
            seq[f"body/{k}"].append(out['body_param'][k].detach().float().cpu().numpy())
        for k in FLAME_KEYS:
            seq[f"flame/{k}"].append(out['flame_param'][k].detach().float().cpu().numpy())
        seq["cam/pd_cam"].append(out['pd_cam'].detach().float().cpu().numpy())

    arrays = {k: np.stack(v, axis=0) for k, v in seq.items()}    # 各 (T, ...)
    arrays["faces"] = renderer.faces[0].detach().cpu().numpy()   # (20908, 3)
    arrays["meta/fps"] = np.float32(fps)
    arrays["meta/focal_length"] = np.float32(24.0)
    arrays["meta/image_size"] = np.int64(1024)
    np.savez_compressed(out_path, **arrays)
    print({k: v.shape for k, v in arrays.items()})

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument('--video', default='example/example_1.mp4')
    p.add_argument('--out', default='params.npz')
    main(**vars(p.parse_args()))
```

要点：`pad_and_resize` 直接内联（不从 `app` import，避免触发 app.py 模块级的整套装配与 Gradio 界面构建）；用 decord 取 RGB 帧，与 `app.py` 的视频路径一致，避开 u2-l2 指出的 BGR 遗留不一致。

### 第二步：`replay_npz.py` —— 只读 npz 离线重建渲染

```python
# replay_npz.py
# 用法: python replay_npz.py --npz params.npz --out replay.mp4
import argparse, numpy as np, torch, imageio, cv2
from models.modules.ehm import EHM_v2
from models.modules.renderer.body_renderer import Renderer2 as BodyRenderer
from utils.graphics_utils import GS_Camera
from pytorch3d.renderer import PointLights

BODY_KEYS  = ["global_pose", "body_pose", "left_hand_pose", "right_hand_pose",
              "hand_scale", "head_scale", "exp", "shape"]
FLAME_KEYS = ["eye_pose_params", "pose_params", "jaw_params",
              "eyelid_params", "expression_params", "shape_params"]

@torch.no_grad()
def main(npz_path, out_path):
    d = np.load(npz_path)
    T = d["cam/pd_cam"].shape[0]
    focal, size = float(d["meta/focal_length"]), int(d["meta/image_size"])

    # 注意：这里没有 Ehm_Pipeline，没有任何网络权重
    ehm = EHM_v2("assets/FLAME", "assets/SMPLX").cuda()
    renderer = BodyRenderer("assets/SMPLX", size, focal_length=focal).cuda()
    lights = PointLights(device='cuda:0', location=[[0.0, -1.0, -10.0]])

    frames = []
    for i in range(T):
        body = {k: torch.tensor(d[f"body/{k}"][i:i+1]).cuda() for k in BODY_KEYS}  # 保留 batch 维
        body.update({"eye_pose": None, "jaw_pose": None, "joints_offset": None})   # 补 3 个 None 键
        flame = {k: torch.tensor(d[f"flame/{k}"][i:i+1]).cuda() for k in FLAME_KEYS}

        mesh = ehm(body, flame)                                   # 参数 → 10475 顶点
        cam = d["cam/pd_cam"][i:i+1]
        camera = GS_Camera(principal_point=torch.zeros(1, 2).float(),
                           focal_length=focal,
                           image_size=torch.tensor([[size, size]]).float(),
                           device="cuda",
                           R=torch.tensor(cam[0:1, :3, :3]).float(),
                           T=torch.tensor(cam[0:1, :3, 3]).float())
        img = renderer.render_mesh(mesh['vertices'][None, 0, ...], camera, lights=lights)
        img = img[:, :3].detach().cpu().numpy().clip(0, 255).astype(np.uint8)[0].transpose(1, 2, 0)
        frames.append(cv2.cvtColor(img.copy(), cv2.COLOR_RGB2BGR))

    writer = imageio.get_writer(out_path, fps=float(d["meta/fps"]), codec="libx264",
                                pixelformat="yuv420p",
                                ffmpeg_params=["-movflags", "faststart"],
                                macro_block_size=None)
    for f in frames:
        h, w = f.shape[:2]
        writer.append_data(f[: h - (h % 2), : w - (w % 2)])       # yuv420p 需偶数宽高
    writer.close()

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument('--npz', default='params.npz')
    p.add_argument('--out', default='replay.mp4')
    main(**vars(p.parse_args()))
```

### 验收清单

1. `export_params.py` 打印的 14 个参数键第 0 维全部等于帧数，`faces` 为 (20908,3)；
2. `replay_npz.py` 全程未 import `Ehm_Pipeline`、未触碰 `pear_model.pt`；
3. `replay.mp4` 与在线渲染（不做平滑时）逐帧一致；
4. 把 `flame/jaw_params` 置零后重放，下颌闭合；对 `body/global_pose` 做 savgol(7) 后重放，抖动减轻——数据可编辑、可再处理；
5. 整个 npz 可拷到另一台只装了 assets 的机器上重放（自包含性）。

以上全流程**待本地验证**（需 GPU、`assets/` 资产与自动下载的 `pear_model.pt`）。

## 6. 本讲小结

- **入口即取 patch 的策略**：三个入口共享「装配 → 前向 → 重建 → 渲染」骨架，差异只在如何得到 256×256 人体 patch；把装配提炼成函数、把「感知段（要权重）」与「重建段（要资产）」切开，PEAR 就成了可嵌入的库。
- **检测器可替换**：主链路用 YOLOv8（`predict` 一行拿 xyxy），`models/vitdet` 提供 detectron2/ViTDet 备选入口 `build_detector`，协议差异在返回结构、person 过滤位置与内部缩放比，适配层约 15 行；detectron2 需自行安装。
- **参数级导出是最优中间产物**：约 1100 个浮点/帧（顶点级的约 3.5%），保留再编辑与参数空间平滑能力；格式为 14 个参数字段 + `pd_cam` + `faces` + meta（fps/焦距/画布），键名契约直接来自 `smplx_head.py` 的输出组装与 `app.py` 的字段列表。
- **离线回放 = 资产 + npz**：不需要网络权重与前向，但 pytorch3d 光栅化与 `GS_Camera` 投影仍需 CUDA；切片保留 batch 维、`pose_type` 形参无效（形状探测决定 pose2rot）、fps 取自 meta 是三个最易踩的坑。
- **项目自带的参数导出未打通**（demo `results.npz` 的 vertices 恒空），本讲的导出/回放闭环是对这条断路的工程化补全。
- **接入自己的训练数据 = 打包 webdataset tar**：每个样本一个图像文件 + 一个 `annotation.pyd`（多人标注列表），字段契约从 `example_formatter` 消费端反推；注意 README 与配置的 tar 路径不一致、训练依赖不在 requirements.txt、以及 `dataset/__init__.py` 引用缺失文件导致导入即炸（垫片可绕过）。

## 7. 下一步学习建议

本手册到此完结，三条继续深挖的路线供参考：

1. **向上游读论文与相关工作**：PEAR 的 EHM 表达、ViTPose 预训练骨干、detectron2/ViTDet 检测分别来自不同工作，arXiv 论文与项目页（README 中有链接）是起点；对照 4D-Humans（`utils_detectron2.py` 注释里标注的来源）看多人 HMR 的通用范式。
2. **在本讲闭环上做增量实验**：给 `replay_npz.py` 加交互式网格查看（trimesh 场景）；把导出格式升级为按人分组的多人版本（结合 `inference_images.py` 的 bbox 流水线，每帧每人一组参数）；或给导出参数接一个动作重定向（retargeting）下游。
3. **读训练侧剩余源码并用自有数据微调**：`dataset/dataset_utils.py` 的 `get_example` 增广细节、`models/pipeline/loss.py` 的鲁棒度量（u5-l3 未展开的 GMoF）、`train_ehms.py` 的断点续训逻辑——如果要按 4.5 的契约打包自己的 tar 做微调，这些是必经之路。
