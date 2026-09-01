# 多人场景：YOLO 检测、bbox 仿射裁剪与回贴

## 1. 本讲目标

学完本讲，你应该能够：

1. 读懂 `sanitize_bbox` / `process_bbox` 的「裁边 → 正方形化 → ratio 放大」三步逻辑，理解为什么要给检测框外扩 1.25 倍。
2. 用 `gen_trans_from_patch_cv` / `generate_patch_image` 解释仿射裁剪的数学原理，并证明 `trans` 与 `inv_trans` 互为逆变换。
3. 理解回贴链路：256×256 patch 上渲染出的网格图，如何经 `inv_trans` 的 `warpAffine` 和布尔 mask 精确覆盖回原图的对应区域。
4. 掌握本讲希望读者体会的工程套路：**「检测框 → 归一化 patch → 推理 → 逆变换回贴」** 是多人人体网格恢复的标准流水线，也是很多下游任务（多人姿态、多人重建）的通用模式。

---

## 2. 前置知识

### 2.1 回顾：PEAR 只吃「单人 patch」

在 [u2-l2](u2-l2-inference-wo-detect.md) 中我们看到，PEAR 网络的输入是一张 256×256 的**单人体 patch**，输出是 `body_param` / `flame_param` / `pd_cam` 三个字段。网络本身**不知道图里有几个人**。

- 单人场景：直接 `pad_and_resize` 把整图塞进 256×256 即可（u2-l2 的做法）。
- 多人场景：必须先**检测出每个人的框**，逐个裁成 patch 推理，再把结果**贴回原图各自的位置**。这正是本讲 `inference_images.py` 做的事。

### 2.2 bbox 的两种格式

| 格式 | 含义 | 本讲出现的位置 |
|---|---|---|
| `xyxy` | `[x1, y1, x2, y2]` 左上角 + 右下角坐标 | YOLO 检测输出 |
| `xywh` | `[x, y, w, h]` 左上角坐标 + 宽高 | `process_bbox` / `generate_patch_image` 的输入输出 |

两套函数之间需要手工换算，这是读多人处理代码时最容易跟丢的地方。

### 2.3 仿射变换：三点定一个变换

二维仿射变换是一个 \( 2 \times 3 \) 矩阵：

\[
\begin{bmatrix} x' \\ y' \end{bmatrix}
=
\begin{bmatrix} a & b & c \\ d & e & f \end{bmatrix}
\begin{bmatrix} x \\ y \\ 1 \end{bmatrix}
\]

它有 6 个未知数，因此**每对「源点 → 目标点」提供 2 个方程，3 对不共线的点恰好唯一确定一个仿射变换**。OpenCV 的 `cv2.getAffineTransform(src, dst)` 就是用 3 对点解这个线性方程组，得到「src 坐标系 → dst 坐标系」的正向映射矩阵。

`cv2.warpAffine(img, M, dsize)` 的语义是：`M` 描述「原图坐标 → 输出图坐标」的映射，函数内部对输出图的每个像素做逆采样插值，产出 `dsize`（注意是 `(宽, 高)`）大小的输出图。

### 2.4 IoU 与 NMS

交并比（IoU）衡量两个框的重叠程度：

\[
IoU(A, B) = \frac{|A \cap B|}{|A \cup B|}
\]

非极大值抑制（NMS）是检测领域的标准去重手段：按置信度从高到低排序，每次保留分数最高的框，并删掉与它 IoU 超过阈值的其余框，循环直到处理完。YOLO 内部已自带 NMS，所以本讲脚本里手写的 `calculate_iou` / `non_max_suppression` 在推理链路上**并未被调用**——它们将在 4.3.4 的实践里被我们重新利用起来。

### 2.5 布尔 mask 索引

`mask = np.any(img > 0, axis=-1)` 生成一个 `(H, W)` 的布尔数组；`a[mask] = b[mask]` 表示「只对 mask 为 True 的像素赋值」。这是回贴环节只覆盖网格像素、保留背景的关键。

---

## 3. 本讲源码地图

| 文件 | 角色 |
|---|---|
| `inference_images.py` | **本讲主角**。多人推理入口：检测、裁剪、推理、回贴、落盘、合成视频全在这一个文件里 |
| `utils/get_video.py` | 提供 `images_to_video`，把输出目录里的 `mesh_*.jpg` 按文件名序号合成 `video.mp4` |
| `models/pipeline/ehm_pipeline.py` | patch → 参数字典（u2-l5 已精读，本讲只作为链条一环） |
| `models/modules/ehm/EHM_v2.py` | 参数 → 10475 顶点网格（u4-l4 将精读） |
| `models/modules/renderer/body_renderer.py` | `Renderer2` 渲染器（u4-l5 将精读） |

`inference_images.py` 内的函数清单（本讲几乎只围绕这一个文件）：

| 函数 | 行号 | 作用 | 是否在主链路被调用 |
|---|---|---|---|
| `calculate_iou` | 31–46 | 两框 IoU（含 +1 像素老约定） | 否（保留工具） |
| `non_max_suppression` | 49–70 | 手写 NMS | 否（保留工具） |
| `pad_and_resize` | 72–86 | letterbox 到方形（u2-l2 已讲） | 否 |
| `build_cameras_kwargs` | 89–96 | 构造 GS_Camera 内参 | 是 |
| `load_img` | 98–116 | 读图 + RGB + **2 倍放大** | 是 |
| `get_bbox` | 118–138 | 由关键点算框（训练代码遗留） | 否 |
| `sanitize_bbox` | 141–152 | 框裁剪到图内、剔除退化框 | 是（经 process_bbox） |
| `process_bbox` | 154–175 | 正方形化 + 外扩 | 是 |
| `rotate_2d` | 177–183 | 二维旋转一个点 | 是（经 gen_trans） |
| `gen_trans_from_patch_cv` | 186–219 | 求裁剪/逆裁剪仿射矩阵 | 是 |
| `generate_patch_image` | 221–240 | 裁出 patch，返回 `trans` / `inv_trans` | 是 |
| `inference` | 242–374 | 主流程 | 是 |

---

## 4. 核心概念与源码讲解

先用一张伪代码图把握全貌（对应 `inference` 函数主循环）：

```text
load_img(scale=2)                        # 原图放大 2 倍, RGB float32
  └─ YOLO.predict(classes=0, conf=0.5)   # N 个 xyxy 人体框
       └─ 逐框循环:
            ① xyxy → xywh
            ② process_bbox(ratio=1.25)   # 裁边 → 正方形化 → 放大 1.25
            ③ generate_patch_image       # warpAffine → 256×256 patch (+trans/inv_trans)
            ④ ehm_model(patch)           # → body_param / flame_param / pd_cam
            ⑤ ehm(...)                   # → 10475 顶点
            ⑥ GS_Camera + Renderer2      # → 1024×1024 网格图
            ⑦ resize → 256×256
            ⑧ warpAffine(inv_trans)      # 贴回原图画布
            ⑨ mask 布尔覆盖
  └─ imwrite mesh_*.jpg → images_to_video → video.mp4
```

本讲的三个最小模块分别对应 ①②（检测调用）、②③（仿射裁剪）、⑦⑧⑨（回贴）。

### 4.1 YOLOv8 检测调用

#### 4.1.1 概念说明

PEAR 的回归网络只负责「一张 patch → 一个人」。要处理多人照片，需要一个前置检测器回答「人在哪」。`inference_images.py` 选用 ultralytics 的 **YOLOv8-x**（通过 `ultralytics` 包的 `YOLO` 类加载）：

- `classes=0`：COCO 80 类里编号 0 的是 `person`，只保留人体检测。
- `conf=0.5`：置信度阈值，低于它的框直接丢弃。这是本讲实践中最重要的可调参数。
- YOLO 的 `predict` 内部自带 NMS（其 IoU 阈值由 ultralytics 默认值决定，也可通过 `iou` 参数放宽）。

另一个容易被忽略的设计：**读图时默认放大 2 倍**。`load_img` 用 `cv2.INTER_CUBIC` 把图放大到 2 倍尺寸后，检测、裁剪、回贴全部发生在「2 倍坐标系」里。动机是帮 YOLO 检出较小的目标，并给后续 1024×1024 渲染留出像素；副作用是**输出图 `mesh_*.jpg` 的分辨率也是原图的 2 倍**——这是 4.1.4 实践中可以直接观察到的现象。

#### 4.1.2 核心流程

1. `YOLO('./model_zoo/yolov8x.pt')` 初始化检测器（仓库不含 `model_zoo` 目录，ultralytics 会按文件名自动下载该权重；若网络受限需手动放置，具体落盘行为待本地验证）。
2. `load_img(img_path)` 读图 → RGB → 放大 2 倍 → float32。
3. `detector.predict(...)` 对整图检测，取 `[0].boxes.xyxy` 转 numpy，得到 `(N, 4)` 的 xyxy 框。
4. 若 `N < 1` 直接 `continue` 跳过这张图。
5. 进入逐框循环：先把 xyxy 换算成 xywh（`process_bbox` 的约定）。

#### 4.1.3 源码精读

**检测器初始化**。[inference_images.py:280-282](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_images.py#L280-L282)：用本地路径 `./model_zoo/yolov8x.pt` 构造 `YOLO` 实例。

**读图与 2 倍放大**。[inference_images.py:98-116](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_images.py#L98-L116)：`cv2.imread` 读 BGR 后按 `order='RGB'` 通道翻转（对比 u2-l2 的 `inference_wo_detect.py` 读图未做 BGR→RGB，这里是正确的）；`scale=2` 分支把宽高各乘 2 后 `INTER_CUBIC` 插值；最后转 float32。

**检测调用**。[inference_images.py:297-303](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_images.py#L297-L303)：

```python
yolo_bbox = detector.predict(original_img,  # [h,w,3]  np
                        device='cuda', 
                        classes=0, 
                        conf=0.5, 
                        save=False, 
                        verbose=False)[0].boxes.xyxy.detach().cpu().numpy()
```

这段代码把 float32 RGB numpy 整图喂给 YOLO（ultralytics 接受 numpy 输入），`[0]` 取 batch 里第一张图的结果，`boxes.xyxy` 是 `(N, 4)` 的 xyxy 张量，`.detach().cpu().numpy()` 搬回 CPU numpy。

**可视化画布**。[inference_images.py:305-309](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_images.py#L305-L309)：`vis_img` 是原图的 BGR 拷贝（`cv2.imwrite` 需要 BGR），所有人的网格最终都画在这张画布上；`len(yolo_bbox) < 1` 时跳过整张图。

**xyxy → xywh 换算**。[inference_images.py:313-318](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_images.py#L313-L318)：

```python
yolo_bbox_xywh = np.zeros((4))
yolo_bbox_xywh[0] = yolo_bbox[bbox_id][0]           # x1
yolo_bbox_xywh[1] = yolo_bbox[bbox_id][1]           # y1
yolo_bbox_xywh[2] = abs(yolo_bbox[bbox_id][2] - yolo_bbox[bbox_id][0])  # w = |x2-x1|
yolo_bbox_xywh[3] = abs(yolo_bbox[bbox_id][3] - yolo_bbox[bbox_id][1])  # h = |y2-y1|
```

**两个不在主链路上的函数**。`get_bbox`（[inference_images.py:118-138](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_images.py#L118-L138)，由 2D 关键点算框，训练代码遗留）和 `calculate_iou` / `non_max_suppression`（[inference_images.py:31-70](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_images.py#L31-L70)）在本文件内没有任何调用点。它们是作者从别处搬来的工具函数，本讲的实践会把后两个用起来。

#### 4.1.4 代码实践：画出「YOLO 原始框 vs process_bbox 扩展框」

**实践目标**：直观看到 `process_bbox` 到底把检测框变成了什么样。

**操作步骤**（以下为示例代码，不修改仓库源码，保存为仓库根目录下的 `inspect_boxes.py` 运行；函数定义直接从 `inference_images.py` 复制，避免触发该文件顶部一串重型 import）：

```python
# 示例代码：inspect_boxes.py
import cv2, numpy as np
from ultralytics import YOLO

def sanitize_bbox(bbox, img_width, img_height):  # ← 从 inference_images.py 141-152 行复制
    ...
def process_bbox(bbox, img_width, img_height, input_img_shape, ratio=1.25):  # ← 复制 154-175 行
    ...

detector = YOLO('./model_zoo/yolov8x.pt')
img = load_img 复制版('example/images/00003.png')   # 注意保留 scale=2
H, W = img.shape[:2]
xyxy = detector.predict(img, device='cuda', classes=0, conf=0.5,
                        save=False, verbose=False)[0].boxes.xyxy.cpu().numpy()

canvas = cv2.cvtColor(img.copy().astype(np.uint8), cv2.COLOR_RGB2BGR)
for x1, y1, x2, y2 in xyxy:
    cv2.rectangle(canvas, (int(x1), int(y1)), (int(x2), int(y2)), (0, 0, 255), 3)   # 红：原始框
    xywh = np.array([x1, y1, abs(x2 - x1), abs(y2 - y1)])
    b = process_bbox(xywh, W, H, input_img_shape=[256, 256], ratio=1.25)
    cv2.rectangle(canvas, (int(b[0]), int(b[1])),
                  (int(b[0] + b[2]), int(b[1] + b[3])), (0, 255, 0), 3)            # 绿：扩展框
cv2.imwrite('bbox_compare.jpg', canvas)
```

**需要观察的现象**：

1. 绿框比红框大——先是「正方形化」再整体乘 1.25（详见 4.2）。
2. 输出图 `bbox_compare.jpg` 的分辨率是 `example/images` 原图的 **2 倍**（`load_img` 的 `scale=2` 所致）。

**预期结果**：每个红框外都套着一个同心、等比放大约 1.25 倍以上的绿框；瘦高的人体框会被扩成近正方形。具体框数与位置取决于 YOLO 权重，待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `classes=0`？改成 `classes=[0, 2]` 会发生什么？
**答案**：COCO 数据集 80 类中 0 号是 `person`，这里只要人体框；`classes=[0, 2]` 会额外保留「汽车」框，后续把汽车区域当人裁剪送去推理，产出无意义的网格。

**练习 2**：`conf=0.5` 调低到 `0.25`，`num_bbox` 通常如何变化？对最终输出有什么风险？
**答案**：阈值降低会保留更多低置信度框，`num_bbox` 变多；风险是把背景、误检区域当人体推理，输出图中出现贴在非人区域上的畸变网格，同时推理耗时随框数线性增加。

**练习 3**：`load_img` 的 `scale=2` 如果去掉，程序还能跑对吗？
**答案**：能跑通——检测、裁剪、回贴用的是同一张 `original_img` 的坐标系，自洽即可；但小目标人体可能检不出来（YOLO 对小分辨率输入的检出率下降），且输出图分辨率变为与原图相同。

---

### 4.2 process_bbox 与 generate_patch_image：仿射裁剪

#### 4.2.1 概念说明

拿到检测框后不能直接裁剪，有两件事要做：

1. **正方形化（aspect ratio preserving）**：网络输入是 256×256 的方形 patch，若直接按瘦高框裁成方形，人体会被横向拉伸变形，偏离训练分布。所以要先把框补成宽高比等于 \( W_{in}/H_{in} \) 的矩形（这里 `[256,256]` 即 1:1，正方形）。
2. **外扩 ratio=1.25**：YOLO 框往往紧贴人体轮廓，容易切掉头顶、脚尖和手臂末端。训练时的 patch 通常带上下文余量，因此推理时也要以框中心为不动点，把宽高各乘 1.25。

随后 `generate_patch_image` 用仿射变换把扩好的框「摆正」到 256×256：它支持旋转（`rot`）和镜像（`do_flip`，训练增广用），推理时均为 `rot=0`、`do_flip=False`、`scale=1.0`，退化为「平移 + 缩放」。

#### 4.2.2 核心流程

`process_bbox` 的数学（记 \( a = input_{W}/input_{H} \)，本处 \( a = 1 \)）：

\[
\begin{aligned}
&\text{if } w > a \cdot h: & h &\leftarrow w / a \\
&\text{elif } w < a \cdot h: & w &\leftarrow a \cdot h \\
&\text{最后：} & w &\leftarrow 1.25\,w, \quad h \leftarrow 1.25\,h
\end{aligned}
\]

中心 \( (c_x, c_y) \) 全程不变，最后以中心重设左上角。

`gen_trans_from_patch_cv` 用**三对点**求仿射矩阵：

| 源点（原图坐标系） | 目标点（patch 坐标系） |
|---|---|
| 框中心 | patch 中心 |
| 框中心 + 下方向半高 | patch 中心 + (0, 128) |
| 框中心 + 右方向半宽 | patch 中心 + (128, 0) |

`inv=False` 时 `cv2.getAffineTransform(src, dst)` 解出「原图 → patch」的 `trans`；`inv=True` 时把两个点集反过来传（[inference_images.py:213-216](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_images.py#L213-L216)），解出「patch → 原图」的 `inv_trans`。旋转角 `rot` 只作用于源点的「下/右」方向向量（`rotate_2d`），因此 `rot≠0` 时 patch 会是旋转摆正后的结果。

#### 4.2.3 源码精读

**sanitize_bbox：裁边与退化剔除**。[inference_images.py:141-152](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_images.py#L141-L152)：把框夹紧到图像边界内（`[0, img_width-1]` / `[0, img_height-1]`），若夹紧后面积为 0 或翻转则返回 `None`。注意：主流程 [inference_images.py:321-327](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_images.py#L321-L327) 拿到返回值后**没有判 None 就直接送 `generate_patch_image`**，理论上完全出界的检测框会触发 `None[0]` 崩溃；正常检测框不会出界，属于未设防的边界情况。

**process_bbox：三步整形**。[inference_images.py:154-175](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_images.py#L154-L175)：先 `sanitize_bbox`，再按 4.2.2 的公式正方形化 + 乘 `ratio`，中心不变重算左上角，返回 `xywh` float32。

**rotate_2d**。[inference_images.py:177-183](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_images.py#L177-L183)：标准二维旋转 \( (x\cos\theta - y\sin\theta,\; x\sin\theta + y\cos\theta) \)。

**gen_trans_from_patch_cv：三点定变换**。[inference_images.py:186-219](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_images.py#L186-L219)：核心是构造上文表格中的两个 3×2 点集后调用 `cv2.getAffineTransform`；`inv` 分支决定点集的传入顺序。`scale` 参数先把 `src_w/src_h` 乘上去（推理时为 1.0）。

**generate_patch_image：裁出 patch 并返回一对互逆矩阵**。[inference_images.py:221-240](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_images.py#L221-L240)：

```python
trans = gen_trans_from_patch_cv(bb_c_x, bb_c_y, bb_width, bb_height,
                                out_shape[1], out_shape[0], scale, rot)
img_patch = cv2.warpAffine(img, trans, (int(out_shape[1]), int(out_shape[0])), ...)
inv_trans = gen_trans_from_patch_cv(..., inv=True)
return img_patch, trans, inv_trans
```

`cv2.warpAffine` 用 `trans` 把原图中框内区域采样成 256×256；`inv_trans` 原样返回给调用者，供回贴使用（4.3）。

**调用点**。[inference_images.py:321-332](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_images.py#L321-L332)：`process_bbox(ratio=1.25, input_img_shape=[256,256])` → `generate_patch_image(out_shape=[256,256], scale=1.0, rot=0.0, do_flip=False)`。注意 patch 是**正方形** 256×256，而网络 forward 内部会再裁成 256×192（见 u2-l5 的 `x[:, :, :, 32:-32]`），两层裁剪的分工不同：前者对齐训练时的裁剪分布，后者对齐网络输入宽度。

**归一化**。[inference_images.py:334-335](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_images.py#L334-L335)：`transforms.ToTensor()` 对 float32 数组只做 HWC→CHW、**不除 255**（它只对 uint8 自动缩放），所以后面显式 `/255`；ImageNet 均值方差归一化留在 `Ehm_Pipeline.forward` 里做（u2-l5）。

#### 4.2.4 代码实践：验证 trans 与 inv_trans 互逆

**实践目标**：亲手证明 `trans` 与 `inv_trans` 是一对互逆仿射，并理解「patch 中心 ↔ 框中心」的对应关系。

**操作步骤**（示例代码，接 4.1.4 的 `inspect_boxes.py` 继续写；`gen_trans_from_patch_cv` / `generate_patch_image` 同样从 `inference_images.py` 复制）：

```python
# 示例代码
img_patch, trans, inv_trans = generate_patch_image(
    cvimg=img, bbox=b, scale=1.0, rot=0.0, do_flip=False, out_shape=[256, 256])

# 1) 官方逆 vs 脚本返回的 inv_trans
inv_official = cv2.invertAffineTransform(trans)
print('inv 误差:', np.abs(inv_official - inv_trans).max())

# 2) 框中心应映射到 patch 中心
c = np.array([b[0] + b[2]/2, b[1] + b[3]/2, 1])
print('trans(框中心) =', trans @ c)          # 期望 ≈ (128, 128)
print('inv_trans(128,128) =', inv_trans @ np.array([128, 128, 1]))  # 期望 ≈ 框中心

# 3) 往返一致：patch 贴回原图画布应与原图同区域吻合
back = cv2.warpAffine(img_patch, inv_trans, (W, H))
print('往返最大误差(框内):', np.abs(back - img).max())
```

**需要观察的现象**：三个打印值分别接近 0、(128, 128)、框中心坐标、0（插值会引入 1~2 灰度级误差）。

**预期结果**：`trans` 与 `inv_trans` 在浮点精度内互逆；框中心与 patch 中心互为映射。第 3 步在框外区域不吻合是正常的——`warpAffine` 只保证框内区域的往返一致。具体数值待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`input_img_shape` 改成 `[256, 192]`（宽 192 高 256），`process_bbox` 会把框整成什么比例？
**答案**：\( a = W_{in}/H_{in} = 192/256 = 0.75 \)，即宽:高 = 3:4 的竖长矩形；瘦高人体框基本不变，横躺的框会被加高到 \( h = w/0.75 \)。

**练习 2**：`ratio=1.25` 的作用是什么？调到 1.0 会怎样？
**答案**：以中心为不动点把框外扩 25%，为头顶/脚尖/手肘留余量，对齐训练裁剪分布；调到 1.0 后余量消失，肢体末端更容易贴出 patch 边界被切断，恢复出的网格可能在边缘出现截断伪影。

**练习 3**：为什么 `inv_trans` 由 `getAffineTransform(dst, src)` 直接求出，而不是对 `trans` 求矩阵逆？
**答案**：三点对应的正反两个方向各解一次线性方程组，数值上等价（仿射可逆时），实现上更简单且与 `rot`/`scale`/`flip` 的构造逻辑天然对称；对本用例（rot=0）两者结果一致，见 4.2.4 实践第 1 步的对比。

---

### 4.3 warpAffine 回贴：从 256 patch 回到原图

#### 4.3.1 概念说明

推理与渲染都发生在 patch 的世界里：`ehm_model` 吃 256×256 patch，`Renderer2` 在 1024×1024 画布上渲染网格。要把结果呈现在原图上，需要一个「坐标系统还原」步骤：

1. 1024×1024 渲染图缩回 256×256（与 patch 同尺寸）；
2. 用 `inv_trans` 做 `warpAffine`，把这张 256×256 图按逆仿射贴到 `(W, H)` 的原图画布上——`inv_trans` 恰好把 patch 坐标映射回「这个人的框在原图中的位置」；
3. 用布尔 mask 只把**非黑像素**（网格本体）覆盖到 `vis_img`，黑背景不动，原图背景得以保留。

注意 mask 的判定是 `np.any(mesh_on_orig > 0, axis=-1)`：渲染结果中恰好为纯黑 `(0,0,0)` 的网格像素也不会覆盖。多人场景中若两人的框有重叠，**后处理的人会覆盖先处理的人**（循环顺序按检测框序号），这是该简单策略的已知局限。

#### 4.3.2 核心流程

```text
pd_smplx_dict = ehm(body_param, flame_param)          # ④⑤ 网格顶点
pd_camera     = GS_Camera(R=pd_cam[:1,:3,:3], T=pd_cam[:1,:3,3])   # ⑥ 相机
pd_mesh_img   = body_renderer.render_mesh(...)        # ⑥ 1024×1024 RGB
pd_mesh_img   = resize → 256×256                      # ⑦ 对齐 patch 尺寸
mesh_on_orig  = cv2.warpAffine(pd_mesh_img, inv_trans, (W, H))      # ⑧ 回贴
mask          = np.any(mesh_on_orig > 0, axis=-1)
vis_img[mask] = mesh_on_orig[mask]                    # ⑨ 覆盖网格像素
cv2.imwrite(f"mesh_{img_name}.jpg", vis_img)
```

#### 4.3.3 源码精读

**相机与渲染**。[inference_images.py:337-343](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_images.py#L337-L343)：`outputs = ehm_model(img_patch)` 得参数字典；`ehm(outputs['body_param'], outputs['flame_param'], pose_type='aa')` 得顶点；`GS_Camera(**build_cameras_kwargs(1, 24), R=..., T=...)`——焦距 24、画布 1024，与 u2-l2 强调的全仓常数一致；`render_mesh` 输出 1024×1024。

**格式转换与缩回 256**。[inference_images.py:344-348](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_images.py#L344-L348)：CHW float → HWC uint8 → RGB2BGR → `INTER_AREA` 缩到 256×256（与 patch 同尺寸，`inv_trans` 才能直接用）。

**warpAffine 回贴**。[inference_images.py:350-359](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_images.py#L350-L359)：

```python
mesh_on_orig = cv2.warpAffine(
    pd_mesh_img,
    inv_trans,
    (W, H),
    flags=cv2.INTER_LINEAR,
    borderMode=cv2.BORDER_CONSTANT,
    borderValue=0
)
```

输出画布大小 `(W, H)` 是 **2 倍放大后**的原图尺寸；越界部分填 0（黑）。

**mask 覆盖与落盘**。[inference_images.py:361-368](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_images.py#L361-L368)：`vis_img[mask] = mesh_on_orig[mask]` 只覆盖网格像素；随后 `cv2.imwrite(..., f"mesh_{img_name}.jpg")`。第 365–366 行的 `if num_bbox == 0: continue` 是**死代码**——`len(yolo_bbox) < 1` 的图在第 307 行已被跳过，执行到这里 `num_bbox` 必然 ≥ 1。

**合成视频**。[inference_images.py:370-374](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_images.py#L370-L374)：调用 `images_to_video(output_path, output_path/video.mp4, fps=30)`。实现见 [utils/get_video.py:16-45](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/get_video.py#L16-L45)：收集目录下所有 jpg/png，按**文件名里最后一个数字**排序（[utils/get_video.py:6-14](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/get_video.py#L6-L14)），`imageio.mimwrite` 写成 mp4。因此输入图按 `00000.png, 00001.png, ...` 命名时（`example/images` 正是如此），输出天然按序成帧。

#### 4.3.4 代码实践：手工 NMS 实验，观察保留框数量的变化

**实践目标**：把脚本里闲置的 `calculate_iou` / `non_max_suppression` 用起来，直观感受 conf 阈值与 NMS IoU 阈值对「最终有几个人」的影响。

**操作步骤**（示例代码，函数从 `inference_images.py` 第 31–70 行复制；在 `inspect_boxes.py` 里追加）：

```python
# 示例代码：conf 扫描 × 手工 NMS 的 IoU 扫描
for conf in [0.2, 0.5, 0.7]:
    r = detector.predict(img, device='cuda', classes=0, conf=conf,
                         iou=0.99,            # 放宽 YOLO 内部 NMS，拿更"生"的框
                         save=False, verbose=False)[0]
    xyxy = r.boxes.xyxy.cpu().numpy()
    confs = r.boxes.conf.cpu().numpy()
    boxes = [[*map(float, b), float(s)] for b, s in zip(xyxy, confs)]  # [x1,y1,x2,y2,score]
    for iou_th in [0.3, 0.5]:
        kept = non_max_suppression(boxes, iou_th)
        print(f'conf={conf}, iou_th={iou_th}: 原始 {len(boxes)} 框 -> NMS 保留 {len(kept)} 框')
```

**需要观察的现象**：

1. conf 越低，原始框越多（低置信度误检混入）。
2. `iou_th` 越小，NMS 越狠，保留框越少；`iou_th=0.3` 时重叠的两人可能被合并掉一个。
3. 同一画面里 YOLO 内部 NMS（`iou` 默认值）与手工 NMS 的去重结果未必一致——两者阈值不同。

**预期结果**：得到一张「conf × iou_th → 保留框数」的表格，保留框数随 conf 升高而减少、随 iou_th 升高而增加。具体数字依赖图片内容与权重，待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么渲染结果是 1024×1024，却要先缩到 256×256 再 `warpAffine`？
**答案**：`inv_trans` 是「patch（256×256）坐标系 → 原图坐标系」的变换；渲染画布尺寸（1024）只是渲染分辨率，与坐标系无关。先把渲染图缩到 256×256 与 patch 坐标系对齐，`inv_trans` 才能直接套用；跳过这步会把网格贴到错误的位置和尺度上。

**练习 2**：两个检测框高度重叠时，最终的 `mesh_*.jpg` 会怎样？
**答案**：两个人都会被推理并渲染，但回贴按检测顺序执行，重叠区域内**后处理者的网格像素覆盖先处理者**（`vis_img[mask] = mesh_on_orig[mask]` 直接赋值）；此外重叠的两张 1.25 倍扩框 patch 会让其中一人包含另一人的局部，可能影响各自恢复质量。

**练习 3**：若把 `mask` 的判定改成 `np.all(mesh_on_orig > 0, axis=-1)`（any → all），输出会怎样变化？
**答案**：改为「三通道同时大于 0 才覆盖」，纯黑像素照旧不覆盖，但深色但非纯黑的网格像素（任一通道为 0，如暗红 `(200, 0, 0)`）也会被排除，网格会出现细小的「漏覆盖孔洞」，边缘更破碎。

---

## 5. 综合实践

**任务：做一次「检测参数 → 多人输出」的对照实验，并产出一个 bbox 可视化小工具。**

1. **准备**：完成 u1-l2 的环境与资产准备，确认 `python inference_images.py --input_path example/images`（README 第 110 行的命令）能跑出 `example/images_output/` 下的 `mesh_*.jpg` 与 `video.mp4`。
2. **可视化工具**：把 4.1.4 与 4.2.4 的代码合并成 `inspect_boxes.py`：对 `example/images` 的每张图画「红 = YOLO 原始框、绿 = process_bbox 扩展框」，并在图上标注框数，存成 `bbox_compare_*.jpg`。
3. **NMS 实验**：加入 4.3.4 的 conf × iou_th 扫描，输出统计表。
4. **端到端对照**：把 `inference_images.py` 复制为 `inference_images_my.py`（不要改原文件），将其中的 `conf=0.5` 改为 `0.25`、`ratio=1.25` 改为 `1.0`，重新推理同一批图：
   - 对比 `mesh_*.jpg` 的框数量与网格完整性（肢体是否被截断）；
   - 对比输出图尺寸与原图的关系（验证 `scale=2` 的影响）。
5. **记录结论**：用三五行文字总结 conf 与 ratio 各自的取舍——这正是多人系统里「漏检 vs 误检」「完整人体 vs 引入邻人干扰」的两对经典矛盾。

## 6. 本讲小结

- `inference_images.py` 是 PEAR 唯一支持多人的入口，流水线为：**YOLO 检测 → process_bbox 整框 → 仿射裁 256×256 patch → 逐人推理渲染 → inv_trans 回贴 → 合成视频**。
- `sanitize_bbox` 负责裁边与退化剔除；`process_bbox` 以框中心为不动点做「正方形化 + 1.25 倍外扩」，对齐训练时的裁剪分布。
- `gen_trans_from_patch_cv` 用三对点（中心/下/右）经 `cv2.getAffineTransform` 求出 `trans` 与 `inv_trans` 一对互逆仿射；`cv2.warpAffine` 的 `M` 语义是「源坐标 → 目标坐标」。
- 回贴的本质是**坐标系还原**：渲染图缩回 256 与 patch 对齐，`inv_trans` 把它精确贴回原图中该人的位置，`mask`（`np.any(>0)`）保证只覆盖网格像素。
- 三个工程细节值得记住：`load_img` 默认 2 倍放大导致输出图也是 2 倍尺寸；`calculate_iou`/`non_max_suppression`/`get_bbox` 是未被调用的保留工具；第 365 行的 `num_bbox == 0` 检查是死代码。

## 7. 下一步学习建议

- 下一讲 [u2-l4](u2-l4-gradio-video-app.md) 转向 `app.py`：Gradio 视频演示的会话临时目录管理与逐帧平滑，那里会再次看到「逐帧 patch 化推理」的思想。
- 想深挖 patch 进入网络后的旅程，回到 [u2-l5](u2-l5-ehm-pipeline-forward.md) 精读 `Ehm_Pipeline.forward` 的归一化与 256×192 裁剪。
- 渲染细节（`Renderer2`、`GS_Camera` 的透视投影）将在 u4-l5 展开；届时可以回头验证本讲第 4.3 节「1024 画布 → 256 patch」的缩放链路。
- 对训练侧裁剪感兴趣的话，可对照 `dataset/dataset_utils.py` 中同名 `gen_trans_from_patch_cv`，看训练增广（`scale`/`rot`/`do_flip` 非默认值时）与本讲推理路径的差异——那是 u5-l1 的内容。
