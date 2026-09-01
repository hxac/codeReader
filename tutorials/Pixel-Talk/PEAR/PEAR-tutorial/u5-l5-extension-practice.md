# 二次开发实践：参数导出、检测器替换与接入自己的数据

## 1. 本讲目标

这是学习手册的最后一讲。前面十讲我们把 PEAR 的推理链路、网络结构、人体模型、渲染器、训练循环逐层拆开，本讲把这些知识收拢成三件「带得走」的能力：

1. **入口改造模式**：看穿三个入口脚本「复制粘贴式」的组装套路，把它提炼成可复用的函数，让 PEAR 能被当成库嵌进你自己的管线。
2. **参数导出格式设计**：把每帧的 `body_param` / `flame_param` / `pd_cam` 序列化成自包含的 npz 文件，理解每个字段的形状与语义契约。
3. **离线渲染复用**：只读 npz（不加载网络、不跑前向），用 EHM_v2 + 渲染器重建网格并渲染成视频，验证导出数据的自包含性。

此外还会覆盖两个「了解级」主题：`models/vitdet` 提供的 detectron2 备选检测入口，以及如何按 webdataset 格式准备自己的训练数据。

学完本讲，你应该能独立回答：「我想用 PEAR 的输出做下游任务（驱动动画、做分析、训练别的模型），最少需要保存什么、怎么恢复？」

## 2. 前置知识

本讲是终章，默认你已读过前置讲义，这里只唤醒最关键的几条认知：

- **推理三件套**：`ehm_model(img_patch)` 输出 `body_param`（SMPL-X 身体侧参数）、`flame_param`（FLAME 头部参数）、`pd_cam`（(B,4,4) 相机 RT 矩阵）；`ehm(body_param, flame_param)` 把参数变成 10475 顶点的统一网格（u2-l5、u4-l4）。
- **权重与资产的分工**：`pear_model.pt` 只含 `backbone` 与 `head` 两段 state dict，`EHM_v2` 与 `Renderer2` 由本地资产构造、零可学习参数、不进 checkpoint（u1-l3、u4-l1）。这是「离线重建不需要网络权重」的根据。
- **全仓常数**：焦距 24、渲染画布 1024，在 `build_cameras_kwargs`、`Renderer2` 构造、`GS_Camera` 投影中必须处处一致（u3-l4、u4-l5）。
- **视频两遍结构**：`app.py` 的 `mesh_inference` 第一遍逐帧收集参数序列、平滑后第二遍重建渲染（u2-l4、u5-l4）——本讲的导出/回放正是把这个两遍结构拆成两个独立脚本。
- **弱透视相机**：`pd_cam` 的第三维由 \( z = f/s \)（f=24）从尺度换算深度，旋转固定为 diag(-1,-1,1)（u3-l4）。

不熟悉的新术语只有一个：**npz**——NumPy 的压缩存档格式，`np.savez_compressed` 把多个命名数组打进一个文件，`np.load` 按键取回，适合存「一帧多个数组、帧数可变」的参数级结果。

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
| [configs/train.yaml](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/configs/train.yaml) | 数据集注册格式与示例 tar 路径 |
| [utils/get_video.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/get_video.py) | 图片序列合成视频的工具函数 |

## 4. 核心概念与源码讲解

### 4.1 入口改造模式：把 PEAR 当库用

#### 4.1.1 概念说明

PEAR 的三个推理入口（`app.py`、`inference_images.py`、`inference_wo_detect.py`）没有共享的「推理库」，而是各自把同一套组装代码复制了一遍。这对研究代码很正常，但对二次开发是噪音：你想接自己的数据源，就得在复制的四十行里小心地改。

入口改造模式的核心观察是：**整条链路只有一段真正依赖网络权重，其余全部由静态资产驱动**。把它切成两段，就得到了可复用的形状：

- **「感知段」**：`Ehm_Pipeline` 前向。需要 `pear_model.pt` 权重、GPU、ImageNet 归一化，输入 256×256 人体 patch，输出三键参数字典。这是唯一的「黑盒」。
- **「重建段」**：`EHM_v2` 参数 → 网格 + `Renderer2` 网格 → 图像。只需要 `assets/` 下的 SMPL-X/FLAME 资产，零权重、零训练状态，且**梯度可以穿过**（训练循环正是这么用的，u5-l2）。

改造时第二个要点是**常数纪律**：焦距 24 与画布 1024 出现在至少三处（相机 kwargs、渲染器构造、投影矩阵），任何一处改动都会让像素对齐悄悄失效。

#### 4.1.2 核心流程

三个入口的组装套路可以抽象成同一份伪代码：

```text
# 一次性装配（约 20 行）
cfg        = ConfigDict('configs/infer.yaml') + add_extra_cfgs
renderer   = Renderer2("assets/SMPLX", 1024, focal_length=24.0)
ckpt       = hf_hub_download("BestWJH/PEAR_models", "pear_model.pt")
ehm_model  = Ehm_Pipeline(cfg); 加载 backbone/head 权重(strict=False); .cuda()
ehm        = EHM_v2("assets/FLAME", "assets/SMPLX").cuda()
lights     = PointLights(location=[[0, -1, -10]])

# 每帧循环（差异只在这里）
patch      = 得到 256×256 人体 patch        # ← 三个入口唯一的区别
outputs    = ehm_model(patch)               # 感知段
mesh_dict  = ehm(outputs['body_param'], outputs['flame_param'])   # 重建段(前半)
camera     = GS_Camera(focal=24, size=1024, R/T 来自 outputs['pd_cam'])
image      = renderer.render_mesh(mesh_dict['vertices'], camera, lights)  # 重建段(后半)
```

「入口」的本质就是 **patch 的获取策略**：`inference_wo_detect.py` 用 `pad_and_resize` 整图塞入（假设单人居中），`inference_images.py` 用检测框 + 仿射裁剪（多人），`app.py` 用 decord 逐帧取图再 `pad_and_resize`。改造入口 = 换掉「得到 patch」这一行，其余原样保留。

#### 4.1.3 源码精读

先看最短入口的装配段。[inference_wo_detect.py:L49-L68](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L49-L68) 依次完成：读配置、建 1024 渲染器、`hf_hub_download` 自动下载权重、构造 `Ehm_Pipeline` 并以 `strict=False` 分段加载 backbone/head、构造资产驱动的 `EHM_v2`、放 CUDA、建灯光。其中 [inference_wo_detect.py:L59-L62](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L59-L62) 这四行就是三个入口共用的权重加载约定——checkpoint 顶层只有 `backbone` 与 `head` 两段。

同一段代码在 [inference_images.py:L259-L276](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_images.py#L259-L276) 几乎逐字重复（只是多了 YOLO 初始化），在 [app.py:L127-L146](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L127-L146) 又重复第三遍——这就是「复制式组装」的实锤，也是改造模式要消除的对象。

感知段的边界在 [models/pipeline/ehm_pipeline.py:L29-L52](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/ehm_pipeline.py#L29-L52)：`forward` 只做归一化、`x[:, :, :, 32:-32]` 裁剪（256×256 → 256×192）、骨干、head 四步，返回 head 组装好的字典。注意函数注释里写的 `'pd_cam': shape (B, 3)` 已经过时——u3-l4 确认实际返回的是 (B,4,4) 的 RT 矩阵，这是「注释漂移」的又一例。

每帧循环的样板在 [inference_wo_detect.py:L79-L94](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L79-L94)：读图 → `pad_and_resize(img, 256)` → `to_tensor` 加 `/255` 与维度重排 → 前向 → `ehm()` 重建 → 用 `outputs['pd_cam']` 切出 R/T 构造 `GS_Camera` → `render_mesh`。相机常数固化在 [inference_wo_detect.py:L36-L43](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L36-L43) 的 `build_cameras_kwargs` 里（`focal_length`、1024×1024 `screen_size`）。

而视频版入口证明这套循环可以直接换数据源：[app.py:L378-L390](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L378-L390) 用 decord 逐帧取图，其余与前述完全一致，只是把每帧输出 `append` 进三条参数序列。**这五行 append 就是本讲导出格式的雏形。**

#### 4.1.4 代码实践

**实践目标**：把复制式装配提炼成一个函数，验证「感知段可整体替换、重建段保持不动」。

**操作步骤**（示例代码，非项目原有文件，建议新建 `my_pear_wrapper.py` 实验，不要改仓库源码）：

1. 从 `inference_wo_detect.py` 抄下 L49-L68 的装配段，包成 `def build_pear(): return cfg, ehm_model, ehm, renderer, lights`；
2. 抄下 L79-L94 的循环体，包成 `def run_frame(ehm_model, ehm, renderer, lights, frame_rgb) -> (outputs, mesh_dict)`；
3. 写一个五行的 `main`：用 `decord.VideoReader`（参考 [app.py:L361](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L361)）读 `example/example_1.mp4` 的前 10 帧，逐帧调 `run_frame`，打印每帧 `outputs['pd_cam']` 的形状。

**需要观察的现象**：10 帧全部打印 `(1, 4, 4)`；三个组件（ehm_model / ehm / renderer）只需构造一次，循环内没有任何权重相关的操作。

**预期结果**：确认改造后的函数与原脚本产出一致（可用同一帧的渲染图肉眼对比），装配代码从 3 份副本变成 1 份。若 `decord` 读帧与 `cv2.imread` 的颜色通道差异让你困惑，回顾 u2-l2 的遗留不一致：`inference_wo_detect.py` 读图未转 RGB，而 decord 输出即 RGB—— wrapper 里应统一成 RGB。

#### 4.1.5 小练习与答案

**练习 1**：三个入口里，哪一行代码是「入口」这个概念的全部差异所在？改造一个「从网络摄像头取流」的新入口，你需要写什么、不需要写什么？

<details><summary>参考答案</summary>

差异只在「得到 256×256 patch」：`inference_wo_detect.py` 的 `pad_and_resize`（L80）、`inference_images.py` 的检测+`generate_patch_image`（L298-L332）、`app.py` 的 decord 帧 + `pad_and_resize`（L379-L383）。摄像头入口只需把「取帧」换成 `cv2.VideoCapture.read()` 加 `pad_and_resize`；装配段、前向、重建、渲染全部照抄，无需重写。
</details>

**练习 2**：为什么说 `EHM_v2` 和 `Renderer2` 是「资产驱动」而非「权重驱动」？从 checkpoint 的角度给出证据。

<details><summary>参考答案</summary>

`EHM_v2` 的 SMPL-X/FLAME 张量全部是 `register_buffer`（u4-l1），`Renderer2` 只注册 `faces`/UV 等 buffer（body_renderer.py L114-L125）；二者没有任何可学习参数，而 `pear_model.pt` 顶层只有 `backbone` 和 `head` 两段 state dict（加载代码只调用了这两段）。所以换 checkpoint 不用重建它们，反之离线重建也不需要 checkpoint。
</details>

**练习 3**：如果把渲染画布从 1024 改成 512，至少要同步改哪几处？漏改会发生什么？

<details><summary>参考答案</summary>

`build_cameras_kwargs` 里的 `screen_size`、`Renderer2` 构造的 `image_size`，以及任何手写 `1024` 的 resize/回贴逻辑。漏改的后果不是报错而是**静默错位**：投影内参按 1024 算、画布却是 512，网格会被放到错误的位置或缩放错误（像素对齐失效），这是最难排查的一类 bug。
</details>

### 4.2 检测器替换：models/vitdet 提供的 detectron2 备选入口

#### 4.2.1 概念说明

多人推理需要一个人体检测器产出 bbox。当前 `inference_images.py` 用的是 **YOLOv8**（ultralytics），而仓库里还躺着一套备选实现 `models/vitdet`：基于 Facebook 的 **detectron2 + ViTDet**（Cascade Mask R-CNN，ViT-H 骨干）。这套代码源自 4D-Humans 等工作的通用做法，PEAR 把它保留在仓库中但主链路未使用（u1-l3 判定的「孤儿模块」）。

了解它的价值有二：一是当你想对比不同检测器对最终网格质量的影响时，入口是现成的；二是它示范了「检测器接口协议」这件事——替换检测器的全部工作就是适配协议差异。

需要注意：`detectron2` 与 `webdataset` 都**不在 requirements.txt 里**（u1-l2 安装的是推理最小集），走这条路要先自行安装 detectron2（版本兼容待本地验证）。

#### 4.2.2 核心流程

两套检测器的协议差异一览：

| 维度 | YOLOv8（现役） | vitdet / DefaultPredictor_Lazy（备选） |
| --- | --- | --- |
| 初始化 | `YOLO('./model_zoo/yolov8x.pt')`，本地权重 | `build_detector(bs, max_img_size, device)`，权重从 detectron2 官方 URL 自动下载 |
| 调用 | `detector.predict(img, classes=0, conf=0.5, ...)` | `detector([img1, ...])`，传**图像列表** |
| 返回 | `[0].boxes.xyxy` 直接拿 (N,4) 框 | `(preds, downsample_ratios)`，preds 是逐图 dict |
| 人体过滤 | `classes=0` 参数内完成 | 需手动过滤 `pred_classes == 0`（COCO person） |
| 置信度阈值 | `conf=0.5` | 配置里 `test_score_thresh = 0.25` |
| 尺寸策略 | 上游 `load_img` 放大 2 倍 | 内部超过 `max_img_size` 自动**缩小**并返回缩放比 |

#### 4.2.3 源码精读

备选入口本体极短。[models/vitdet/__init__.py:L7-L19](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/vitdet/__init__.py#L7-L19) 的 `build_detector(batch_size, max_img_size, device)` 做四件事：用 detectron2 的 `LazyConfig` 读同目录的 `cascade_mask_rcnn_vitdet_h_75ep.py`；把 `train.init_checkpoint` 指向官方 COCO 预训练 ViTDet 权重 URL（自动下载）；把三个级联 box predictor 的 `test_score_thresh` 统一调成 0.25；返回 `DefaultPredictor_Lazy` 实例。

调用协议在 [models/vitdet/utils_detectron2.py:L123-L163](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/vitdet/utils_detectron2.py#L123-L163)：构造时 `instantiate` 模型、加载 checkpoint、取出 mapper 的增广与图像格式，并断言输入格式为 RGB。真正的调用逻辑在 [models/vitdet/utils_detectron2.py:L176-L190](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/vitdet/utils_detectron2.py#L176-L190)：对每张图，若最长边超过 `max_img_size` 就按比例缩小并记录 `downsample_ratios`——**这意味着返回的框坐标在缩小后的图上，接回原图坐标时必须乘回这个比例**（也可以像 YOLO 路线那样把 `max_img_size` 设得足够大来回避）。输出整理在 [models/vitdet/utils_detectron2.py:L211-L215](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/vitdet/utils_detectron2.py#L211-L215)：每张图一个 dict，含 `pred_classes`、`scores`、`pred_boxes`（xyxy）。

对照现役 YOLO 路线：[inference_images.py:L281-L282](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_images.py#L281-L282) 从本地 `./model_zoo/yolov8x.pt` 初始化；[inference_images.py:L298-L303](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_images.py#L298-L303) 一行 `predict` 拿到过滤好的 xyxy 框。替换的适配点就是：把这一行换成「调 vitdet → 过滤 person → 按需乘回缩放比」，后续 `process_bbox` → `generate_patch_image` → 前向的流水线一字不改（[inference_images.py:L321-L337](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_images.py#L321-L337)）。

#### 4.2.4 代码实践

**实践目标**：不改主链路，用源码阅读方式确认「替换检测器只需适配一段胶水代码」。

**操作步骤**：

1. 打开 `models/vitdet/cascade_mask_rcnn_vitdet_h_75ep.py`，找到 `roi_heads.box_predictors` 相关配置，确认它确实是 Cascade Mask R-CNN 结构（读文件即可，不必运行）；
2. 对照上表写一段 10 行以内的伪代码（或真代码，若已装 detectron2）：输入一张 RGB 图，调 `build_detector(1, 512, 'cuda')` 得到的检测器，过滤出 person 框并转成 xyxy numpy 数组；
3. 检查你的代码是否处理了 `downsample_ratios`。

**需要观察的现象**：伪代码中网络前向相关的调用（`ehm_model`、`ehm`、`render_mesh`）完全不出现。

**预期结果**：替换检测器的胶水层约 15 行，且与 4.1 的 wrapper 可以自由组合。detectron2 路线的实际检测效果**待本地验证**（需要额外安装 detectron2，且注意其与 torch 2.0.1 的版本匹配）。

#### 4.2.5 小练习与答案

**练习 1**：`DefaultPredictor_Lazy` 为什么要返回 `downsample_ratios`，而 YOLO 路线不需要？

<details><summary>参考答案</summary>

因为 vitdet 路线在内部把超过 `max_img_size` 的图缩小了，框坐标落在缩小后的图上，必须乘回比例才能对齐原图；YOLO 路线反其道而行（`load_img` 先放大 2 倍再检测，检测器不改变尺寸），所以坐标天然在（放大后的）输入图坐标系里。
</details>

**练习 2**：两套检测器对人体类的置信度阈值分别是多少？分别在哪里设置？

<details><summary>参考答案</summary>

YOLO：`conf=0.5`，在调用处 [inference_images.py:L301](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_images.py#L301)。vitdet：`test_score_thresh = 0.25`，在初始化时写进三个 box predictor 的配置 [models/vitdet/__init__.py:L11-L12](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/vitdet/__init__.py#L11-L12)。一个在推理时改，一个在构造时改——替换时别找错地方。
</details>

### 4.3 参数导出格式设计：逐帧结果的 npz 序列化

#### 4.3.1 概念说明

下游复用 PEAR，最经济的中间产物不是视频、也不是逐帧顶点，而是**参数级结果**。算一笔账：一帧的 `body_param + flame_param + pd_cam` 合计约 950 个浮点数，而一帧顶点是 10475×3 ≈ 3.1 万个浮点数——

\[ \frac{312 + 6 + 200 + 50 + 14 + 300 + 50 + 16}{10475 \times 3} \approx 3\% \]

参数级导出省 30 倍存储，且保留**再编辑能力**：改一个关节的旋转、调一个表情系数、对参数序列做时序平滑（u5-l4 的 savgol 正是参数空间操作），都不需要重新跑网络。

但项目自带的导出其实**没有打通**：demo 的 `results.npz` 里 vertices 字段恒为空。本模块要设计一个真正自包含的格式。

#### 4.3.2 核心流程

导出格式的设计决策与理由：

1. **存原始（未平滑）参数**。平滑窗口是回放端的选择（u5-l4 的实验表明 3/7/21 各有取舍），把平滑烙进导出文件会剥夺这种自由。
2. **参数按字段拉直成 (T, …) 序列**。`body_param` 是 11 键的嵌套 dict，npz 是扁平的键值空间，故用 `body/global_pose`、`flame/jaw_params` 这样的带前缀扁平键名；`eye_pose`/`jaw_pose`/`joints_offset` 三键恒为 `None`（u3-l3 审计结论），不存。
3. **相机只存 `pd_cam` (T,4,4)**。它已含 RT；内参（焦距 24、画布 1024）是全仓常数，但仍写进 meta——自包含的意义就是「读文件的人不需要知道仓库常数」。
4. **faces 存一份 (20908,3)**。渲染拓扑来自 `smplx_tex.obj`，存进 npz 后回放脚本连 obj 都可以不读。
5. **meta 记录 fps、帧数、导出脚本版本**。回放合成视频时 fps 必须与源一致（u5-l4 指出 demo 硬编码 30 会变速）。

每帧字段的完整契约（形状以批大小 1、第 0 维为帧数 T）：

| npz 键 | 形状 | 语义 |
| --- | --- | --- |
| `body/global_pose` | (T,1,3,3) | 全局旋转（旋转矩阵） |
| `body/body_pose` | (T,21,3,3) | 21 个身体关节旋转 |
| `body/left_hand_pose` | (T,15,3,3) | 左手 15 关节旋转 |
| `body/right_hand_pose` | (T,15,3,3) | 右手 15 关节旋转 |
| `body/hand_scale` | (T,3) | 手部缩放（当前下游未消费） |
| `body/head_scale` | (T,3) | FLAME 头三轴缩放 |
| `body/exp` | (T,50) | SMPL-X 表情系数 |
| `body/shape` | (T,200) | SMPL-X 体型系数（前 10 维有效语义） |
| `flame/eye_pose_params` | (T,6) | 双眼球旋转 |
| `flame/pose_params` | (T,3) | FLAME 全局旋转（EHM_v2 中被置零） |
| `flame/jaw_params` | (T,3) | 下颌旋转 |
| `flame/eyelid_params` | (T,2) | 左右眼睑开合 |
| `flame/expression_params` | (T,50) | FLAME 表情 |
| `flame/shape_params` | (T,300) | FLAME 头型 |
| `cam/pd_cam` | (T,4,4) | 相机 RT（旋转恒为 diag(-1,-1,1)） |
| `faces` | (20908,3) | 渲染拓扑（覆盖前 10475 顶点） |
| `meta/fps` 等标量 | — | fps、帧数、常数快照 |

#### 4.3.3 源码精读

字段的**权威定义处**在解码头。[models/smplx/smplx_head.py:L283-L300](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/smplx_head.py#L283-L300) 组装 `body_param`：312 维姿态输出按 6/126/90/90 切片过 `rot6d_to_rotmat` 得到各旋转矩阵字段，6 维 scale 输出切成 `hand_scale`（前 3）与 `head_scale`（后 3），再加 `exp`(50)、`shape`(200) 与三个恒 `None` 键；[models/smplx/smplx_head.py:L271-L278](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/smplx_head.py#L271-L278) 组装 `flame_param` 六键；[models/smplx/smplx_head.py:L304-L317](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/smplx/smplx_head.py#L304-L317) 把相机 3 维参数换算成 `RT` 后连同两个参数字典一起放进输出——`all_out['pd_cam'] = RT  # [4,4]`，这就是导出时直接切 R/T 的依据。

「逐帧收集参数序列」的现成范式是 [app.py:L378-L390](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L378-L390)：三条序列 `body_sequence` / `flame_sequence` / `cam_sequence` 各自 append 每帧输出。随后 [app.py:L393-L402](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L393-L402) 与 [app.py:L414-L423](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L414-L423) 用两个字段名列表把嵌套 dict 逐键 `torch.cat` 成 (T,…) 张量——`fields1`/`fields2` 这两个列表就是上表键名的出处，也再次印证 8+6 个有效字段。

项目自带导出的「未打通」证据：[app.py:L472](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L472) 收集顶点的语句被注释掉，于是 [app.py:L503-L507](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L503-L507) 落盘的 `results.npz` 中 `vertices` 恒为空数组——只有 `faces`（来自 `body_renderer.faces[0]`）是真的。u5-l4 已指出这一点；我们的格式设计等于把这条断路重接，而且接到参数一级而非顶点一级。

faces 的来源在 [models/modules/renderer/body_renderer.py:L108-L114](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/renderer/body_renderer.py#L108-L114)：`Renderer2` 构造时从 `smplx_tex.obj` 读拓扑，`faces.verts_idx` 注册为 buffer。回顾 u4-l5：该拓扑 20908 个面片只覆盖前 10475 个 SMPL-X 顶点，末尾 120 个牙齿顶点不参与渲染——导出 vertices 时若想保完整网格，需自行用 trimesh 处理，本讲格式只存 faces 与参数，网格由回放端重建。

#### 4.3.4 代码实践

**实践目标**：写出导出函数 `export_video_params(video_path, out_npz)`（示例代码，非项目原有文件）。

**操作步骤**：

1. 复用 4.1 的 `build_pear()` 装配；
2. 逐帧循环（decord 取帧 + `pad_and_resize` 到 256 + 前向，照抄 [app.py:L378-L385](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L378-L385)），把每帧 14 个参数字段 `.detach().float().cpu().numpy()` 存进各自的 list；
3. 循环外 `np.stack` 成 (T,…) 并按上表的扁平键名写入 `np.savez_compressed`，附 `faces = body_renderer.faces[0].cpu().numpy()` 与 meta；
4. 打印 `np.load(out_npz)` 的每个键与形状，逐项核对与 4.3.2 契约表一致。

**需要观察的现象**：`npz.files` 共 16 个左右键；所有 `body/*`、`flame/*`、`cam/pd_cam` 的第 0 维都等于帧数；`faces.shape == (20908, 3)`。

**预期结果**：`example/example_1.mp4`（前 3 秒约 90 帧，具体帧数待本地验证）导出的 npz 在几十 MB 以内、可自由 `np.load`。若某字段第 0 维不等于帧数，说明你在循环里漏 append 或 append 了 (1,…) 之外形状的张量。

#### 4.3.5 小练习与答案

**练习 1**：为什么导出原始参数而不是平滑后的参数？给出一个具体的下游场景。

<details><summary>参考答案</summary>

平滑窗口是「抖动抑制 vs 动作失真」的取舍（u5-l4：3→7 收益显著，7→21 失真陡增），不同下游偏好不同窗口。例如做动作分析宁可少平滑保时间精度，做展示视频则可加大窗口。烙死在导出文件里就无法回头；存原始参数则回放端可任意重滤波。
</details>

**练习 2**：`flame/pose_params` (T,3) 导出了但在回放中不会生效，为什么仍然建议保留它？

<details><summary>参考答案</summary>

EHM_v2 的 forward 会把 FLAME 的 global/neck pose 强制置零（u4-l2、u4-l4：头部朝向由身体蒙皮驱动）。保留它是为了**格式完整性**——回放端重建的 flame_param_dict 必须有这个键（[EHM_v2.py:L42](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L42) 直接索引它，缺键会 KeyError），而且它忠实记录了网络原始输出，便于调试对比。
</details>

**练习 3**：如果把导出粒度从「参数级」换成「顶点级」（直接存 (T,10475,3)），会失去什么？

<details><summary>参考答案</summary>

失去再编辑能力：无法改关节/表情/相机后重建，无法做参数空间平滑（只能在顶点空间平滑，会产生 u5-l4 说过的重影与体积收缩问题）；存储大约 30 倍；且没法验证「参数 → EHM」这条链路的正确性。换来的是回放端不需要 EHM_v2 资产。
</details>

### 4.4 离线渲染复用：不跑网络重建网格与视频

#### 4.4.1 概念说明

「离线」在本讲的含义是：**不加载网络权重、不跑 ViT 前向**，只凭 npz + 本地资产完成网格重建与渲染。这验证了导出数据的自包含性——任何持有 npz 的人（将来的你、你的合作者、另一个项目）都能复现画面，而不需要 GPU 上的 6.3 亿参数骨干。

注意边界：pytorch3d 的光栅化与 `GS_Camera` 的投影矩阵仍依赖 CUDA（u3-l4），所以「离线」不等于「无 GPU」，只是「无网络」。如果需要真正无 GPU 的查看路径，可以在回放时把顶点导出为 obj（u4-l5 讲过 trimesh `process=False` 保顶点顺序），用任意网格查看器打开。

#### 4.4.2 核心流程

回放脚本的执行过程：

```text
读 npz → 逐帧 i：
  1. 从扁平键切出第 i 帧的 14 个参数字段（保留第 0 维=1，如 body/global_pose[i:i+1]）
  2. 组装 body_dict（11 键，3 个 None 键补 None）与 flame_dict（6 键）
  3. （可选）对该字段序列做 savgol 平滑
  4. mesh_dict = ehm(body_dict, flame_dict, pose_type='aa')   # 参数 → 10475 顶点
  5. camera = GS_Camera(focal=meta['focal'], size=meta['size'],
                        R=pd_cam[i:i+1,:3,:3], T=pd_cam[i:i+1,:3,3])
  6. img = renderer.render_mesh(mesh_dict['vertices'][None,0,...], camera, lights)
  7. RGB→可写帧，追加进帧列表
最后 imageio 以 meta['fps']、libx264+yuv420p+faststart 写出 mp4
```

关键点：第 2 步必须保留 batch 维（长度 1 的切片而非索引），因为 `EHM_v2.forward` 内部到处用 `shape[0]` 推 batch size；第 6 步 `[None, 0, ...]` 的写法是把 (V,3) 顶点抬成 (1,V,3)，与 [inference_wo_detect.py:L93](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L93) 一致。

#### 4.4.3 源码精读

回放的骨架在 `app.py` 第二遍循环里已经写好。[app.py:L443-L463](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L443-L463) 逐帧从平滑后的序列切片组装 `body_dict` / `flame_dict`（注意它把三个 `None` 键显式补上——这正是回放端必须照抄的细节）；[app.py:L465-L467](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L465-L467) 三行完成 `ehm()` 重建、`GS_Camera` 构造（R/T 从 4×4 矩阵切片）、`render_mesh` 渲染。你的回放脚本与它的唯一区别是数据来源：它读的是内存里平滑后的张量，你读的是 npz。

`ehm()` 的输入输出契约在 [models/modules/ehm/EHM_v2.py:L34-L45](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L34-L45)（两个参数字典各读哪些键）与 [models/modules/ehm/EHM_v2.py:L89-L99](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L89-L99)（body 侧 8 个有效键的形状注释）。一个容易踩的坑：`pose_type='aa'` 形参实际未被使用，真正决定「输入是轴角还是旋转矩阵」的是 [models/modules/ehm/EHM_v2.py:L114-L121](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/modules/ehm/EHM_v2.py#L114-L121) 的形状探测——pose 是 3 维就走 `pose2rot=True`，是 3×3 矩阵就走 `pose2rot=False`。导出的参数是旋转矩阵（(T,J,3,3)），回放时原样喂入即可落在 `pose2rot=False` 分支，与在线推理完全一致。

渲染端：`render_mesh` 的完整逻辑在 [utils/graphics_utils.py:L780-L817](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/graphics_utils.py#L780-L817)——`faces` 缺省时用 `self.faces`（即 npz 里那份拓扑的来源）、组 `TexturesVertex` 纯色、`Meshes`、`GS_MeshRasterizer` + `SoftPhongShader`。若不传 `cameras`，它会按 [utils/graphics_utils.py:L730-L742](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/graphics_utils.py#L730-L742) 的 `_build_cameras` 用 `transform_matrix` 现建相机——两条路等价，回放脚本沿用入口脚本的显式 `GS_Camera` 写法更清晰。

视频编码参数抄 [app.py:L485-L498](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L485-L498)：`libx264` + `yuv420p`（要求偶数宽高，故有裁奇数行的 `img2`）+ `faststart`（moov 前置，浏览器可流式播放）。若你的回放只要图片序列，也可以走 [utils/get_video.py:L16-L47](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/get_video.py#L16-L47) 的 `images_to_video`（按文件名中的数字排序后 `mimwrite`），但它不控制像素格式，浏览器兼容性不如前者。

#### 4.4.4 代码实践

**实践目标**：写出 `replay_npz.py`（示例代码，非项目原有文件），只依赖 npz + assets 完成重建渲染。

**操作步骤**：

1. `np.load` 导出文件；构造 `EHM_v2("assets/FLAME", "assets/SMPLX").cuda()` 与 `Renderer2("assets/SMPLX", 1024, focal_length=24.0).cuda()`——**不 import `Ehm_Pipeline`、不下载权重**；
2. 按 4.4.2 流程逐帧重建渲染，帧追加进列表；
3. 用 `meta/fps` 与 yuv420p 参数写 `replay.mp4`；
4. 与在线推理（直接跑 `inference_wo_detect.py` 或 demo）的结果逐帧对比。

**需要观察的现象**：脚本启动明显变快（跳过了 6.3 亿参数骨干的加载与下载）；回放视频与在线渲染在同一帧上肉眼无差异；尝试在回放前对 `body/global_pose` 序列做 `savgol_filter`（窗口 7），视频中动作变顺滑但快速动作略有迟滞（复现 u5-l4 的结论）。

**预期结果**：`replay.mp4` 帧数与 npz 的 T 相等、与源视频不变速（fps 取自 meta）；修改任一参数字段（如把 `flame/jaw_params` 全置零）再回放，可见下颌闭合——证明导出数据确实自包含且可编辑。完整可运行的两脚本代码见第 5 节综合实践。

#### 4.4.5 小练习与答案

**练习 1**：回放时如果把切片写成 `body/global_pose[i]`（丢掉 batch 维）会发生什么？

<details><summary>参考答案</summary>

`EHM_v2.forward` 里 `batch_size = shape_params.shape[0]` 会取到 200（shape 的特征维）或在对 (J,3,3) 的字段做 batch 维操作时直接形状不匹配报错——总之不是静默出错就是崩溃。正确写法是 `[i:i+1]` 保留长度为 1 的第 0 维，这也是 app.py 第二遍循环的写法。
</details>

**练习 2**：回放脚本能否在 CPU 机器上运行？给出依据。

<details><summary>参考答案</summary>

不能完整运行。网络部分可以（根本没加载），但 `GS_Camera` 的投影矩阵构造硬编码 CUDA（u3-l4），pytorch3d 的 GPU 光栅化也需要 GPU。CPU 上的替代路径是：回放时用 `ehm()` 在 CPU 上算顶点（EHM_v2 的张量运算本身不强制 CUDA），导出 obj 后用 trimesh/查看器看网格，绕开光栅化。
</details>

**练习 3**：为什么回放视频的 fps 必须从 meta 读取，而不能像 demo 那样硬编码 30？

<details><summary>参考答案</summary>

demo 把 fps 硬编码 30，遇到低帧率源视频会变速（u5-l4 指出的硬伤）。参数帧与源视频帧一一对应，只有用源 fps 回放才能保持真实时间节奏；这正是 4.3.2 中「meta 必须记录 fps」的设计依据。
</details>

### 4.5 接入自己的数据：webdataset tar 的样本契约

#### 4.5.1 概念说明

二次开发的另一半是「喂自己的数据」。PEAR 的训练侧用 webdataset 流式读取 tar 分片（u5-l1），所以接入自己的数据 = 打包出符合 `example_formatter` 期望的 tar。注意 README 与配置有一处**路径不一致**：README 让你把示例 tar 放在 `ehms_datasets/`，而配置里注册的是 `./ehm_datasets/000000.tar`——以 [configs/train.yaml:L130](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/configs/train.yaml#L130) 为准（或改配置），否则训练找不到数据。

另外 `webdataset`、`pycocotools`、`yacs`、`braceexpand` 等训练侧依赖不在 requirements.txt 中，跑训练前需自行安装（u5-l2 的隐含前置，版本待本地验证）。

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

一个 tar 内每个「样本」是一组同前缀文件，最少需要两类：一个图像文件（.jpg/.jpeg/.png）和一个 `annotation.pyd`（pickle 的标注列表，列表中每个元素是一个人的标注，因为一帧可以有多人）。分片命名遵循 `{000000..000014}.tar` 的花括号展开约定（[webdata_loader.py:L82-L90](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/dataset/webdata_loader.py#L82-L90) 的 `expand_urls` 用 braceexpand 展开）。

#### 4.5.3 源码精读

管线组装在 [dataset/webdata_loader.py:L321-L345](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/dataset/webdata_loader.py#L321-L345)：`load_tars_as_wds` 建立 `WebDataset → decode("rgb8") → rename → apply_example_formatter` 四级流水线。样本契约的**消费端**就是 `example_formatter`：

- [dataset/webdata_loader.py:L186-L215](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/dataset/webdata_loader.py#L186-L215)：`random.randint` 从 `annotation.pyd` 列表里随机选一人（一条数据只训一个人）；随后从标注里取 `scale`、`center` 做裁剪中心，并判断有无 `smpl_keypoints_2d` 决定走 SMPL 44 点还是 DWPose 134 点监督通道。
- [dataset/webdata_loader.py:L123-L184](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/dataset/webdata_loader.py#L123-L184)：`fet_tracking_info_from_raw` 逐键消费标注——`smplx_params`（含 `global_pose`/`body_pose`/`left_hand_pose`/`right_hand_pose`/`camera_RT_params`）、`flame_params`（六键）、`id_params`（`smplx_shape` 10 维补零到 200、`flame_shape` 补零到 300、`joints_offset`、`head_scale`、`hand_scale`）、三个有效标志 `head_valid`/`hand_valid`/`pose_valid`。
- [dataset/webdata_loader.py:L222-L246](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/dataset/webdata_loader.py#L222-L246)：图像来自 `sample["jpg"]`，可选的 `mask` 键（pycocotools RLE）拼成 RGBA 后一起进 `get_example` 做同步增广。

数据集注册格式见 [configs/train.yaml:L125-L132](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/configs/train.yaml#L125-L132)：`name` + `item.urls`（tar 路径或花括号模式）+ `epoch_size` + `weight`，多个数据集按归一化权重混采。示例 tar 的下载与放置说明在 [README.md:L117-L133](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/README.md#L117-L133)。

由此可以反推**自制 tar 的最小字段清单**：`__key__`（wds 自动）、图像文件、`annotation.pyd`（列表，每人含 `smplx_params` 五键、`flame_params` 六键、`id_params` 五键、三个 valid 标量、`center`、`scale`、一组 2D/3D 关键点，可选 `mask`）。

#### 4.5.4 代码实践

**实践目标**：不写任何打包代码，先用只读方式摸清官方示例 tar 的真实结构。

**操作步骤**：

1. 按 README 从 Google Drive 下载示例 tar，放到 `./ehm_datasets/000000.tar`（以配置路径为准）；
2. 列目录：`tar -tf ehm_datasets/000000.tar | head -40`，观察每个样本前缀下有哪些扩展名；
3. 写一个 10 行脚本（示例代码）：`wds.WebDataset('./ehm_datasets/000000.tar').decode("rgb8").rename(jpg="jpg;jpeg;png")` 取第一个样本，`print(sample.keys())`，再对照 4.5.3 的字段清单逐一勾选。

**需要观察的现象**：tar 内文件按 `前缀.jpg`、`前缀.annotation.pyd`（及其他可能的键）成组；decode 后 `sample['jpg']` 是 PIL 图像，`sample['annotation.pyd']` 是 Python 列表。

**预期结果**：字段清单与 `sample.keys()` 完全对上，你就拥有了自制 tar 的完整规格。示例 tar 的具体键集合**待本地验证**（以上契约是从消费端源码反推的，以实际文件为准）。

#### 4.5.5 小练习与答案

**练习 1**：为什么一帧多人时 `example_formatter` 只随机选一个人训练，而不是全部都用？

<details><summary>参考答案</summary>

PEAR 的网络输入是「单人 256×256 patch」，输出也是单人参数字典；训练裁剪以所选人的 `center/scale` 为几何中心。多人全用需要按人分别裁 patch 组 batch，而当前实现是一条「一图一人」的流式样本通道，随机选人等价于按人数加权采样。
</details>

**练习 2**：你的自制数据只有图像和 SMPL 参数、没有 FLAME 标注，tar 该怎么填才能不炸？

<details><summary>参考答案</summary>

`flame_params` 六键仍要给（可用全零），并把 `head_valid` 置 0——训练侧的 `has_flame` 软门控（u5-l3）会把 FLAME 参数损失的权重压到接近零，网络自动只从身体监督学习。同理 `pose_valid`/`hand_valid` 控制身体与手部门控。
</details>

## 5. 综合实践

**任务**：实现「导出—回放」闭环，验证 PEAR 参数级结果的自包含性。这是本讲的收官实践，综合了 4.1 的入口改造、4.3 的格式设计与 4.4 的离线重建。

### 第一步：`export_params.py`（示例代码，非项目原有文件）

```python
# export_params.py —— 对视频逐帧推理，导出参数级 npz
# 用法: python export_params.py --video example/example_1.mp4 --out params.npz
import argparse, numpy as np, torch, decord
from huggingface_hub import hf_hub_download
from models.pipeline.ehm_pipeline import Ehm_Pipeline
from models.modules.ehm import EHM_v2
from models.modules.renderer.body_renderer import Renderer2 as BodyRenderer
from utils.general_utils import ConfigDict, add_extra_cfgs
from utils.graphics_utils import GS_Camera
from utils.pipeline_utils import to_tensor
from pytorch3d.renderer import PointLights
from app import pad_and_resize          # 复用 4.1 提炼的预处理（RGB 输入版本）

BODY_KEYS  = ["global_pose", "body_pose", "left_hand_pose", "right_hand_pose",
              "hand_scale", "head_scale", "exp", "shape"]
FLAME_KEYS = ["eye_pose_params", "pose_params", "jaw_params",
              "eyelid_params", "expression_params", "shape_params"]

@torch.no_grad()
def main(video_path, out_path):
    # ---- 一次性装配（照抄 inference_wo_detect.py L49-L68）----
    meta_cfg = add_extra_cfgs(ConfigDict(model_config_path='configs/infer.yaml'))
    renderer = BodyRenderer("assets/SMPLX", 1024, focal_length=24.0).cuda()
    ckpt = hf_hub_download(repo_id="BestWJH/PEAR_models",
                           filename="pear_model.pt", repo_type="model")
    ehm_model = Ehm_Pipeline(meta_cfg)
    state = torch.load(ckpt, map_location='cpu', weights_only=True)
    ehm_model.backbone.load_state_dict(state['backbone'], strict=False)
    ehm_model.head.load_state_dict(state['head'], strict=False)
    ehm_model = ehm_model.cuda().eval()

    # ---- 逐帧前向：只收集参数，不做 EHM/渲染（对齐 app.py L378-L390）----
    vr = decord.VideoReader(video_path)
    fps = int(vr.get_avg_fps())
    seq = {f"body/{k}":  [] for k in BODY_KEYS}
    seq.update({f"flame/{k}": [] for k in FLAME_KEYS})
    seq["cam/pd_cam"] = []
    for i in range(len(vr)):
        frame = vr[i].asnumpy()                       # RGB
        patch = pad_and_resize(frame, target_size=256)
        patch = torch.permute(to_tensor(patch, 'cuda:0') / 255, (2, 0, 1)).unsqueeze(0)
        out = ehm_model(patch)
        for k in BODY_KEYS:  seq[f"body/{k}"].append(out['body_param'][k].detach().float().cpu().numpy())
        for k in FLAME_KEYS: seq[f"flame/{k}"].append(out['flame_param'][k].detach().float().cpu().numpy())
        seq["cam/pd_cam"].append(out['pd_cam'].detach().float().cpu().numpy())

    arrays = {k: np.stack(v, axis=0) for k, v in seq.items()}          # 各 (T, ...)
    arrays["faces"] = renderer.faces[0].detach().cpu().numpy()         # (20908, 3)
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

注意 `from app import pad_and_resize` 会触发 app.py 的模块级装配（模型在导入期就构造一次），本示例为省事接受这个副作用；更干净的做法是把 `pad_and_resize` 复制进自己的脚本。**待本地验证**。

### 第二步：`replay_npz.py`（示例代码，非项目原有文件）

```python
# replay_npz.py —— 只读 npz，不加载网络，重建网格并渲染成视频
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
        body = {k: torch.tensor(d[f"body/{k}"][i:i+1]).cuda() for k in BODY_KEYS}
        body.update({"eye_pose": None, "jaw_pose": None, "joints_offset": None})
        flame = {k: torch.tensor(d[f"flame/{k}"][i:i+1]).cuda() for k in FLAME_KEYS}

        mesh = ehm(body, flame, pose_type='aa')                 # 参数 → 10475 顶点
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
        writer.append_data(f[: h - (h % 2), : w - (w % 2)])     # yuv420p 需偶数宽高
    writer.close()

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument('--npz', default='params.npz')
    p.add_argument('--out', default='replay.mp4')
    main(**vars(p.parse_args()))
```

### 验收清单

1. `export_params.py` 打印的 14 个参数键形状第 0 维全部等于帧数，`faces` 为 (20908,3)；
2. `replay_npz.py` 全程未 import `Ehm_Pipeline`、未触碰 `pear_model.pt`；
3. `replay.mp4` 与直接跑 demo 得到的 `mesh_video.mp4`（未平滑时）逐帧一致；
4. 把 `flame/jaw_params` 置零后重放，下颌闭合；对 `body/global_pose` 做 savgol(7) 后重放，抖动减轻——数据可编辑、可再处理；
5. 整个 npz 可以拷到另一台只装了 assets 的机器上重放（自包含性）。

## 6. 本讲小结

- **入口即取 patch 的策略**：三个入口共享「装配 → 前向 → 重建 → 渲染」骨架，差异只在如何得到 256×256 人体 patch；把装配提炼成函数、把「感知段（要权重）」与「重建段（要资产）」切开，PEAR 就成了可嵌入的库。
- **检测器可替换**：主链路用 YOLOv8（`predict` 一行拿 xyxy），`models/vitdet` 提供 detectron2/ViTDet 备选入口 `build_detector`，协议差异在返回结构、person 过滤位置与内部缩放比，适配层约 15 行。
- **参数级导出是最优中间产物**：约 950 个浮点/帧（顶点级的 3%），保留再编辑与参数空间平滑能力；格式为 14 个参数字段 + `pd_cam` + `faces` + meta（fps/焦距/画布），键名契约直接来自 `smplx_head.py` 的输出组装与 `app.py` 的字段列表。
- **离线回放 = 资产 + npz**：不需要网络权重与前向，但 pytorch3d 光栅化与 `GS_Camera` 投影仍需 CUDA；切片保留 batch 维、fps 取自 meta 是两个最易踩的坑。
- **项目自带的参数导出未打通**（`results.npz` 的 vertices 恒空），本讲的导出/回放闭环是对这条断路的工程化补全。
- **接入自己的训练数据 = 打包 webdataset tar**：每个样本一个图像文件 + 一个 `annotation.pyd`（多人标注列表），字段契约从 `example_formatter` 的消费端反推；注意 README 的 `ehms_datasets/` 与配置的 `./ehm_datasets/` 路径不一致，以配置为准。

## 7. 下一步学习建议

本手册到此完结，三条继续深挖的路线供参考：

1. **向上游读论文与相关工作**：PEAR 的 EHM 表达、ViTPose 预训练骨干、detectron2 检测分别来自不同工作，arXiv:2601.22693 与项目页（README 中有链接）是起点；对照 4D-Humans（`utils_detectron2.py` 注释里标注的来源）看多人 HMR 的通用范式。
2. **在本讲闭环上做增量实验**：给 `replay_npz.py` 加交互式查看（trimesh 场景）、把导出格式升级为按人分组的多人版本（结合 `inference_images.py` 的 bbox 流水线）、或给导出参数接一个动作重定向（retargeting）下游。
3. **读训练侧剩余源码**：`dataset/dataset_utils.py` 的 `get_example` 增广细节、`models/pipeline/loss.py` 的鲁棒度量（u5-l3 未展开的 GMoF），以及 `train_ehms.py` 的断点续训逻辑——如果要用自己的数据微调，这些是必经之路。
