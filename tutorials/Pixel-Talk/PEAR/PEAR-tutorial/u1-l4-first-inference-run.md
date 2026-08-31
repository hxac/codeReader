# 跑通第一次推理：三个入口脚本

## 1. 本讲目标

学完本讲，你应该能够：

1. 在配好环境的机器上，分别运行三个推理入口并得到各自的输出产物：
   - `inference_images.py` → `mesh_*.jpg` + `video.mp4`
   - `inference_wo_detect.py` → `mesh_*.jpg` + `result_*.jpg`
   - `app.py` → `mesh_video.mp4` + `results.npz`
2. 说出每个入口的适用场景：多人自动检测 / 单人无检测 / 浏览器视频演示。
3. 对任何一份输出文件，能立刻指出它是由源码中的哪一行代码写出的。
4. 理解三个入口共享的「读配置 → 建渲染器 → 下权重 → 建模型 → 前向 → 渲染 → 落盘」组装套路。

本讲是**实操讲**：所有结论都以源码为依据，但运行结果需要你在自己的 GPU 机器上验证（环境准备见 u1-l2）。

## 2. 前置知识

### 2.1 回顾 PEAR 的推理链路（来自 u1-l1）

```text
输入图像
  → 预处理成 256×256 的人体 patch
  → Ehm_Pipeline（ViT 骨干 + 解码头）
      输出三个字段：body_param / flame_param / pd_cam
  → EHM_v2(body_param, flame_param)  → 10475 顶点的统一网格
  → GS_Camera(pd_cam) + Renderer2    → 渲染出人体网格图像
  → 写出文件
```

本讲不深入每个环节内部，只关心一件事：**这条链路在三个入口脚本里分别是怎么被组装、怎么把结果写到磁盘上的**。

### 2.2 本讲会遇到的几个新面孔

| 名词 | 一句话解释 |
|---|---|
| YOLOv8 | Ultralytics 提供的目标检测器，`classes=0` 表示只检测「人」这一类，返回每个人的矩形框（xyxy 格式） |
| Gradio | 一个 Python 库，用几行代码把函数包装成浏览器网页界面；`app.py` 用它做视频上传/结果播放 |
| decord | 高效视频读取库，`VideoReader[i]` 按帧号取图 |
| imageio | 图像/视频读写库，本讲里负责把帧序列编码成 mp4 |
| Savitzky–Golay 滤波 | 一种沿时间轴的多项式平滑滤波，用来消除逐帧推理的抖动（详见 u5-l4，本讲只用不求甚解） |
| alpha 混合 | 把两张图按权重叠加：\[ \text{result} = \alpha \cdot I_{\text{mesh}} + (1-\alpha) \cdot I_{\text{bg}} \]，对应 `cv2.addWeighted` |

### 2.3 运行前提（来自 u1-l2 / u1-l3）

- 已创建 `pear` conda 环境并安装 `requirements.txt`、pytorch3d、chumpy；
- `assets/FLAME`、`assets/SMPLX` 下已放好 FLAME 与 SMPL-X 模型文件（推理链路必需）；
- 所有命令都在**仓库根目录**下执行（脚本里的路径都是相对根目录写死的）；
- 网络权重 `pear_model.pt` 由 `hf_hub_download` 首次运行时自动下载，无需手动放置。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
|---|---|---|
| [inference_images.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_images.py) | 多人图像批量推理入口（YOLO 检测） | 命令行参数、主循环、产物写出 |
| [inference_wo_detect.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py) | 无检测单人推理入口（README 未列出） | 预处理、两份产物的写出 |
| [app.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py) | Gradio 视频演示入口 | 启动流程、界面事件、产物写出 |
| [utils/get_video.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/get_video.py) | 图片序列合成视频的工具 | `images_to_video` |
| [README.md](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/README.md) | 官方运行说明 | Quick Start 中的两条推理命令 |

三个入口共享的 5 个核心模块（回顾 u1-l3）：`Ehm_Pipeline`、`EHM_v2`、`Renderer2`、`GS_Camera`、`ConfigDict`——它们在不同脚本里被**复制式**地重复组装，这正是本讲要让你熟悉的部分。

## 4. 核心概念与源码讲解

### 4.1 inference_images.py：多人图像推理的命令行与主循环

#### 4.1.1 概念说明

这是 README 官方推荐的图像推理入口（[README.md:L107-L111](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/README.md#L107-L111)）。它解决的问题是：**一张图里可能有多个人**。PEAR 模型本身只吃「一个居中人体的 256×256 patch」，所以需要一个检测器先把每个人框出来，逐个裁剪、推理，再把结果贴回原图。

#### 4.1.2 核心流程

```text
python inference_images.py --input_path example/images
  │
  ├─ 1. 解析命令行参数（-c 配置名 / -d GPU / 输入输出路径）
  ├─ 2. 组装推理组件（五件套：Config → Renderer2 → 权重 → Ehm_Pipeline → EHM_v2）
  ├─ 3. 初始化 YOLOv8 人体检测器
  ├─ 4. 遍历输入目录中的所有图片：
  │     ├─ 读图并放大 2 倍（load_img, scale=2）
  │     ├─ YOLO 检测所有人框（classes=0, conf=0.5）
  │     ├─ 对每个框：
  │     │    ├─ process_bbox：等比扩框 ×1.25
  │     │    ├─ generate_patch_image：仿射裁出 256×256 patch
  │     │    ├─ ehm_model 前向 → body_param / flame_param / pd_cam
  │     │    ├─ ehm(...) → 顶点；GS_Camera + render_mesh → 1024×1024 网格图
  │     │    ├─ 缩回 256×256，warpAffine(inv_trans) 贴回原图
  │     └─ 写出 mesh_{图片名}.jpg（所有人体已叠加在这张图上）
  └─ 5. 把所有 mesh_*.jpg 按序合成 video.mp4（fps=30）
```

#### 4.1.3 源码精读

**① 命令行参数**定义在入口处，共 5 个：

[inference_images.py:L377-L388](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_images.py#L377-L388) —— `argparse` 定义了 `--config_name`（默认 `infer`，对应 `configs/infer.yaml`）、`--devices`（默认 `'0'`）、`--input_path`（默认 `example/images`）、`--output_path`（默认 `example/images_output`）和 `--debug`，最后调用 `torch.set_float32_matmul_precision('high')`（允许 TF32，提速）并进入 `inference()`。

**② 组件组装**与其他两个入口完全同构：

[inference_images.py:L259-L282](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_images.py#L259-L282) —— 依次构造 `BodyRenderer("assets/SMPLX", 1024, focal_length=24.0)`、用 `hf_hub_download` 从 HuggingFace 仓库 `BestWJH/PEAR_models` 下载 `pear_model.pt`、以 `strict=False` 分别加载 `backbone` 与 `head` 两段 state dict、构造 `EHM_v2`，最后初始化 YOLO 检测器。

注意一个细节：`model_zoo/` 目录**并不在仓库里**（fresh clone 后不存在）。u1-l3 已指出 ultralytics 首次运行会自动下载 `yolov8x.pt`；如果实际启动时报「权重不存在」，可先 `mkdir model_zoo` 再重试（下载落盘需要父目录存在），或把路径改成 `'yolov8x.pt'`。此行为**待本地验证**。

**③ 读图放大 2 倍**是一个容易被忽略的预处理：

[inference_images.py:L98-L116](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_images.py#L98-L116) —— `load_img` 默认 `scale=2`，用 `cv2.INTER_CUBIC` 把图像宽高各放大 2 倍并转成 RGB。**后续的检测框、裁剪、贴回全部发生在这个 2 倍坐标系里**，最终 `mesh_*.jpg` 的分辨率也是原图的 2 倍。

**④ 检测 + 逐人处理主循环**：

[inference_images.py:L293-L332](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_images.py#L293-L332) —— 主循环先调 `detector.predict(classes=0, conf=0.5)` 拿到所有人的 xyxy 框（L298-L303）；若一个框都没有则 `continue` 跳过该图（L307-L308）。随后对每个框转成 xywh、`process_bbox(..., ratio=1.25)` 等比放大，`generate_patch_image` 用 `cv2.warpAffine` 裁出 256×256 patch，同时返回 `trans`（正向变换）和 `inv_trans`（逆变换）——后者稍后用于贴回。

[inference_images.py:L334-L348](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_images.py#L334-L348) —— patch 除以 255 后送入 `ehm_model` 得到 `outputs` 三字段；`ehm(outputs['body_param'], outputs['flame_param'])` 得到顶点；用 `outputs['pd_cam']` 的旋转/平移部分构造 `GS_Camera`，`render_mesh` 渲染出 1024×1024 网格图，再缩放回 256×256。

**⑤ 贴回原图并写出**：

[inference_images.py:L350-L368](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_images.py#L350-L368) —— 用 `cv2.warpAffine(pd_mesh_img, inv_trans, (W, H))` 把 patch 坐标系的网格图逆变换回原图坐标系；`mask = np.any(mesh_on_orig > 0, axis=-1)` 取非黑色像素作为网格掩码，`vis_img[mask] = mesh_on_orig[mask]` 逐像素覆盖；最后 `cv2.imwrite(..., f"mesh_{img_name}.jpg", vis_img)` 写出**这一张图所有人体**的叠加结果。

**⑥ 图片序列合成视频**：

[inference_images.py:L370-L374](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_images.py#L370-L374) —— 调用 `images_to_video(output_path, output_path/video.mp4, fps=30)`。其实现见 [utils/get_video.py:L16-L47](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/get_video.py#L16-L47)：收集目录下所有 jpg/png，按文件名中的数字排序，`imageio.mimwrite` 编码成视频。所以**输入图片命名必须含序号**才能保证帧序正确。

另外两处值得留意的事实（本讲不展开）：

- 文件里的 [calculate_iou / non_max_suppression（L31-L70）](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_images.py#L31-L70) 在主循环中**并未被调用**（`detector.predict` 已返回去重后的框），属于预留的手工 NMS 工具，u2-l3 会拿它做实验。
- 同文件定义的 `pad_and_resize`（L72-L86）在本脚本主流程中也未被调用，是从无检测版脚本复制过来的痕迹。

#### 4.1.4 代码实践

**实践目标**：跑通多人图像推理，确认输出文件与源码写出位置一一对应。

**操作步骤**：

1. 在仓库根目录激活 `pear` 环境后执行：

   ```bash
   python inference_images.py --input_path example/images
   ```

   （默认输出到 `example/images_output/`；可加 `--output_path my_out` 自定义。）

2. 运行结束后列出产物：

   ```bash
   ls example/images_output | head
   ls example/images_output | wc -l
   ```

**需要观察的现象**：

- 终端先打印 `Command Line Args: ...` 和整份配置 `meta_cfg`（来自 L385-L386 的 print）；
- 首次运行会先下载 `pear_model.pt`（可能还有 `yolov8x.pt`）；
- `example/images` 中有 00000.png ~ 00346.png 共 **347 张**图（视频抽帧序列），因此应生成 347 张 `mesh_00000.jpg ... mesh_00346.jpg`，外加 1 个 `video.mp4`。

**预期结果**：每张 `mesh_*.jpg` 是**放大 2 倍的原帧 + 每个被检测到的人体网格贴回**；`video.mp4` 是这 347 张图按 30fps 播放的效果。产物与代码的对应关系：

| 产物 | 写出代码 |
|---|---|
| `mesh_{img_name}.jpg` | [inference_images.py:L368](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_images.py#L368) 的 `cv2.imwrite` |
| `video.mp4` | [inference_images.py:L370-L374](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_images.py#L370-L374) → [utils/get_video.py:L41-L45](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/get_video.py#L41-L45) 的 `imageio.mimwrite` |

本环境无 GPU 与模型资产，以上运行结果为依据源码的推断，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：往 `example/images` 里混入一张完全没有人的风景图，输出目录会多出对应的 `mesh_xxx.jpg` 吗？

**答案**：不会。L307-L308 在检测框数量 `< 1` 时直接 `continue`，该图既不渲染也不写出；由于主循环跳过它，`video.mp4` 的帧数也会比输入图片数少 1。

**练习 2**：为什么最终 `mesh_*.jpg` 的分辨率是原图的 2 倍？

**答案**：因为 `load_img` 默认 `scale=2`（L98-L116），检测、贴回、`cv2.imwrite` 全部在这个放大后的 `original_img` 坐标系里完成；`vis_img` 就是放大图的拷贝（L305）。

**练习 3**：`--devices` 参数（默认 `'0'`）被解析后实际用在哪里？

**答案**：L254 的 `target_devices = device_parser(devices)` 只做了多 GPU 字符串解析，但解析结果在后续代码中并未再被使用——模型统一用 `.cuda()` 放到默认卡上。也就是说当前脚本**单卡可用**，`-d 0,1` 并不会真正使用两张卡（`device_parser` 本身的用法详见 u2-l1）。

### 4.2 inference_wo_detect.py：无检测版的两份产物

#### 4.2.1 概念说明

这是 u1-l3 发现的「隐藏入口」（README 未列出）。它假设**输入图本身就是一张单人居中、人体占满大部分画面的图**，因此跳过检测，直接把整张图等比压进 256×256 的黑色正方形里推理。它的价值有二：一是链路最短、最适合逐行精读（u2-l2 会专门做）；二是它是**唯一会写出 `result_*.jpg` 叠加图**的入口。

#### 4.2.2 核心流程

```text
python inference_wo_detect.py --input_path <目录>
  │
  ├─ 1. 组装五件套（与 4.1 相同，但没有 YOLO）
  ├─ 2. 遍历图片：
  │     ├─ cv2.imread 原图（保持 BGR）
  │     ├─ pad_and_resize(img, 256)：等比缩放、居中、黑边补齐
  │     ├─ to_tensor → permute → /255 → (1,3,256,256)
  │     ├─ ehm_model 前向 → ehm(...) → 顶点
  │     ├─ 渲染 1024×1024 网格图
  │     ├─ 写出 mesh_{name}.jpg   ← 纯网格渲染（黑底）
  │     └─ 背景提亮 ×1.5 → alpha=0.5 加权混合
  │           写出 result_{name}.jpg ← 网格与原图叠加
  └─ （无视频合成步骤）
```

#### 4.2.3 源码精读

**① 命令行参数更精简，没有 `-d`**：

[inference_wo_detect.py:L116-L128](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L116-L128) —— 只有 `--config_name`、`--input_path`、`--output_path`（两者默认均为 `data_input/test_source_images`，**输入输出同目录**，注意别覆盖原图）和 `--debug`。

**② 预处理：整图进正方形**：

[inference_wo_detect.py:L77-L86](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L77-L86) —— `cv2.imread` 读图（BGR，未转 RGB），`pad_and_resize(img, target_size=256)` 等比缩放后贴到黑底正方形中心，`to_tensor` + `permute(/255)` 变成 `(1, 3, 256, 256)` 张量，随后 `ehm_model(img_patch)` 前向、`ehm(...)` 得到顶点。

**③ 产物一：纯网格图**：

[inference_wo_detect.py:L88-L97](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L88-L97) —— 原图被 `pad_and_resize` 到 1024 作为背景 `_img`；渲染出的 1024×1024 网格图转 BGR 后，`cv2.imwrite(os.path.join(output_path, f"mesh_{img_name}.jpg"), pd_mesh_img)` 直接写出黑底纯网格。

**④ 产物二：alpha 叠加图**：

[inference_wo_detect.py:L99-L111](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L99-L111) —— 逐像素判断非黑得到 `foreground_mask`；背景亮度 ×1.5（`np.clip` 防止溢出）让暗图里的网格更醒目；`alpha = 0.5`，`cv2.addWeighted(pd_mesh_img, alpha, bright_bg, 1-alpha, 0)` 即 \[ \text{result} = 0.5 \cdot I_{\text{mesh}} + 0.5 \cdot I_{\text{bg}} \]；最后写出 `result_{img_name}.jpg`。

一个值得留意的事实（不改变运行结果）：本脚本读图后**全程保持 BGR**，`img_patch` 是以 BGR 顺序喂给网络的；而 `inference_images.py` 的 `load_img` 会先转 RGB。两个入口对同一张图的模型输入颜色通道顺序并不一致，这是阅读源码时能发现的真实现象，影响几何待后续实验确认。

#### 4.2.4 代码实践

**实践目标**：用最短链路得到「纯网格 + 叠加」两张图，直观理解 alpha 混合。

**操作步骤**：

1. 准备一张单人居中的图片（可从示例视频抽一帧，示例代码如下）：

   ```python
   # 示例代码：从示例视频抽第 0 帧存成 png
   import decord, cv2
   frame = decord.VideoReader("example/example_1.mp4")[0].asnumpy()  # RGB
   cv2.imwrite("data_input/single/frame_0.png", cv2.cvtColor(frame, cv2.COLOR_RGB2BGR))
   ```

2. 运行（注意 `--output_path` 指向别的目录，避免与输入混放）：

   ```bash
   python inference_wo_detect.py --input_path data_input/single --output_path data_input/single_out
   ```

3. 打开 `data_input/single_out/` 查看 `mesh_frame_0.jpg` 与 `result_frame_0.jpg`。

**需要观察的现象**：同一帧得到两张图——一张黑底纯网格、一张网格半透明叠在提亮的原帧上。

**预期结果**：因为 `pad_and_resize` 假设人体居中，若画面里的人偏离中心或过小，网格会明显错位。这是该脚本的适用边界。本环境无 GPU，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`mesh_*.jpg` 和 `result_*.jpg` 里的背景有什么不同？

**答案**：`mesh_*.jpg` 是渲染器输出的黑底纯网格（L97 直接 imwrite）；`result_*.jpg` 的背景是原图 `pad_and_resize` 到 1024 后再整体提亮 1.5 倍的结果（L88、L102-L104），再与网格 0.5/0.5 加权混合。

**练习 2**：把 `alpha` 从 0.5 改成 0.9，`result_*.jpg` 会怎么变？

**答案**：按混合公式，网格权重从 0.5 升到 0.9、背景权重降到 0.1，网格更实、背景几乎被压暗到不可见。源码注释也明确写了 `# you can change the alpha to control the transparency of the mesh`（L106-L107）。

**练习 3**：为什么这个脚本不需要 YOLO，却要求输入图「单人居中」？

**答案**：因为它用 `pad_and_resize` 把**整张图**塞进模型期望的 256×256 正方形（L80），没有裁剪定位步骤；模型在训练时见到的是人体居中的 patch，输入偏离这一分布时预测会明显退化。

### 4.3 app.py：Gradio 视频演示的启动与界面流程

#### 4.3.1 概念说明

`app.py` 是 README 推荐的视频入口（[README.md:L101-L105](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/README.md#L101-L105) 对应 `python app.py`）。它用 Gradio 把「上传视频 → 推理 → 播放结果」包装成网页，同时内置了三件事：**会话临时目录管理**（多用户互不干扰、10 分钟后自动清理）、**只截取前 3 秒**（控制演示耗时）、**参数级时序平滑**（消除逐帧抖动）。它也是 HuggingFace Live Demo 的同款代码，因此带有 `spaces.GPU` 等 Spaces 适配逻辑。

#### 4.3.2 核心流程

```text
python app.py
  │
  ├─ 1. import 时即完成：环境校验 → 读配置 → 建渲染器 → 下权重 → 建 Ehm_Pipeline/EHM_v2
  ├─ 2. 定义回调：
  │     ├─ handle_video_upload（上传变化时触发）
  │     │     ├─ create_user_temp_dir：temp_local/session_<uuid8>/，600 秒后自动删除
  │     │     ├─ imageio 截取前 int(fps*3) 帧存为 <视频名>.mp4
  │     │     └─ 抽首帧、短边缩放到 336，打包成 JSON 存入 gr.State
  │     └─ launch_viz（点击按钮时触发）→ mesh_inference：
  │           ├─ decord 逐帧读视频 → 逐帧前向，收集 body/flame/cam 三组参数序列
  │           ├─ Savitzky–Golay 平滑（body/cam window=7，flame window=5）
  │           ├─ 逐帧 ehm(...) + render_mesh，收集网格帧
  │           ├─ imageio 写 mesh_video.mp4（libx264 + yuv420p + faststart）
  │           └─ np.savez_compressed 写 results.npz（faces；vertices 当前为空）
  └─ 3. gr.Blocks 组界面，绑定事件，demo.queue().launch() 起服务
```

#### 4.3.3 源码精读

**① import 阶段就把模型全部建好**（与两个脚本「函数内组装」不同）：

[app.py:L59-L65](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L59-L65) —— 三连 import 校验 chumpy、pytorch3d 及编译扩展 `_C`，成功则打印 `🎉 All systems go!`（u1-l2 已精读）。

[app.py:L127-L146](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L127-L146) —— 模块级完成 `ConfigDict` → `BodyRenderer` → `hf_hub_download` → `Ehm_Pipeline` 加载权重 → `EHM_v2`。这意味着**执行 `python app.py` 的瞬间**就开始下载/加载模型，而不是等用户点按钮。[app.py:L122](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L122) 的 `TORCH_DEVICE` 让它在无 GPU 机器上退化为 CPU 运行。

**② 会话临时目录与延迟清理**：

[app.py:L213-L226](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L213-L226) —— `create_user_temp_dir` 用 `uuid4()[:8]` 生成会话 ID，创建 `temp_local/session_<id>/`，并调度 600 秒后删除。

[app.py:L194-L210](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L194-L210) —— `delete_later` 用 `ThreadPoolExecutor` 提交「sleep(600) 再删」的任务，同时 `atexit.register` 保证进程退出时兜底清理。这就是多个用户同时上传视频也不会互相覆盖结果的原因。

**③ 上传回调：截前 3 秒**：

[app.py:L269-L341](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L269-L341) —— `handle_video_upload` 是 `video_input.change` 事件的回调。核心在 [app.py:L294-L309](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L294-L309)：`max_frames = int(fps * 3)`，用 imageio 只把前 3 秒的帧写进会话目录下的 `<视频名>.mp4`；随后抽首帧、短边缩放到 336 并取偶数边长，把 `temp_dir / video_name / video_path` 等信息打包成 JSON 返回给 `gr.State`（L329-L341）。界面提示语也写明「only supports single human-centered video inputs (3 seconds)」（L744）。

**④ 推理回调：逐帧前向 + 平滑 + 渲染**：

[app.py:L343-L390](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L343-L390) —— `mesh_inference` 带 `@spaces.GPU`（本地无 spaces 包时由 L109-L114 的 fallback 变成空装饰器）与 `@torch.no_grad()`；用 `decord.VideoReader` 逐帧取出，套用与无检测版完全相同的 `pad_and_resize(256) → to_tensor → permute(/255)` 预处理，逐帧 `ehm_model` 前向，把三组参数分别 append 进 `body_sequence / flame_sequence / cam_sequence`。

[app.py:L393-L434](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L393-L434) —— 对 body 的 8 个字段（`global_pose`、`body_pose`、左右手 `hand_pose`、`hand_scale`、`head_scale`、`exp`、`shape`）用 `polynomial_smooth(window_size=7)` 平滑；对 flame 的 6 个字段（`eye_pose_params`、`pose_params`、`jaw_params`、`eyelid_params`、expression、shape）用 `window_size=5`；相机序列同样 window=7。平滑函数实现（Savitzky–Golay）在 [app.py:L253-L266](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L253-L266)，参数细节留给 u5-l4。

[app.py:L439-L470](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L439-L470) —— 平滑后逐帧重组 `body_dict / flame_dict`，`ehm(body_dict, flame_dict, pose_type='aa')` 得到顶点，`GS_Camera` + `render_mesh` 渲染，RGB 网格帧 append 进 `all_meshes_img`。

**⑤ 产物一：mesh_video.mp4**：

[app.py:L477-L501](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L477-L501) —— 用 imageio 写 mp4，编码参数是本段代码的教学亮点：`codec="libx264"`（H.264）、`pixelformat="yuv420p"`（浏览器 HTML5 播放要求）、`ffmpeg_params=["-movflags", "faststart"]`（元数据前置，边下边播）；每帧先裁成偶数宽高（yuv420p 的硬性要求）；失败则回退 `mimwrite`。

**⑥ 产物二：results.npz（注意一个真实坑）**：

[app.py:L503-L507](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L503-L507) —— `faces = body_renderer.faces[0].detach().cpu().numpy()`，`vertices` 来自 `vertices_list`。但收集顶点的语句在 [app.py:L472](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L472) **被注释掉了**，于是 `vertices_list` 恒为空，L506 走 `np.empty((0, 0, 3))` 分支。结论：当前版本 `results.npz` 里 `faces` 有值、`vertices` 是空数组——下载后看不到顶点是预期行为，不是你操作错了。若要恢复，取消 L472 的注释即可（属于修改源码的练习，见 4.4.4）。

**⑦ 界面与事件绑定**：

- [app.py:L558-L727](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L558-L727)：`gr.Blocks(theme=gr.themes.Soft(), ...)` 内含大段自定义 CSS（固定视频区高度、示例卡片横滑等）。
- [app.py:L753-L757](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L753-L757)：`video_input` 上传组件；[app.py:L766-L777](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L766-L777)：`gr.Examples` 挂上 `example/example_1.mp4`、`example/example_2.mp4` 两个内置示例（仓库里这两个文件真实存在）。
- [app.py:L784-L791](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L784-L791)：右侧结果视频 `viz_video`（`interactive=False`，只播不放）。
- [app.py:L853-L866](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L853-L866)：两条事件绑定——`video_input.change → handle_video_upload`（上传即截 3 秒）与 `launch_btn.click → launch_viz`（点按钮才真正推理）；中间靠隐藏组件 `gr.State`（L849）传递 JSON 会话信息。
- [app.py:L870-L873](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L870-L873)：`demo.queue().launch()` 启动服务，默认监听 `http://127.0.0.1:7860`。

#### 4.3.4 代码实践

**实践目标**：跑通 Gradio 视频演示，拿到两份产物并核对其内容。

**操作步骤**：

1. 启动服务：

   ```bash
   python app.py
   ```

   等终端出现 Gradio 的 `Running on local URL: http://127.0.0.1:7860`（首次会先打印 `🎉 All systems go!` 并下载权重）。

2. 浏览器打开该地址 → 在左侧上传 `example/example_1.mp4`（或点示例卡片）→ 点击 **🚀 Start Tracking Now!**。

3. 等待右侧播放器出结果后，从下方 `📄 Download Mesh Results` 下载 `results.npz`；同时在仓库根目录找到本次会话目录：

   ```bash
   ls temp_local/            # 应看到 session_xxxxxxxx
   ls temp_local/session_*/results/   # mesh_video.mp4 与 results.npz
   ```

4. 用以下脚本检查 npz（示例代码）：

   ```python
   import numpy as np
   d = np.load("<下载路径>/results.npz")
   print(d.files)                    # ['vertices', 'faces']
   print(d["faces"].shape)           # 预期 (F, 3)，F 为网格面数
   print(d["vertices"].shape)        # 预期 (0, 0, 3)：收集语句 L472 被注释
   ```

**需要观察的现象**：上传后终端立刻打印「成功截取前 3 秒视频并保存至 ...」（L309），说明 `handle_video_upload` 已执行；点按钮后打印 `🎯 Running EHM pipeline...` 与 `✅ EHM processing completed.`（L366、L510）；约 10 分钟后会话目录被自动删除（可观察 `temp_local/` 变空）。

**预期结果**：`mesh_video.mp4` 可直接在浏览器播放（yuv420p + faststart 的功劳）；`results.npz` 的 `faces` 有内容而 `vertices` 为空（原因见 ⑥）。以上**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `python app.py` 一启动（还没人上传视频）就可能开始下载模型权重？

**答案**：因为模型组装写在模块级（L127-L146），Python import `app.py` 时这些语句立即执行；两个图像入口则是把同样的组装放进 `inference()` 函数内，调用时才执行。

**练习 2**：上传一个 10 秒的视频，实际会被推理多少秒？

**答案**：只推理前 3 秒。`max_frames = int(fps * 3)`（L299），超出部分在写临时视频时就被丢弃，后续 decord 读到的只有前 3 秒的帧。

**练习 3**：两个用户几乎同时上传视频，他们的结果会互相覆盖吗？

**答案**：不会。每次上传都调用 `create_user_temp_dir()` 生成 `temp_local/session_<uuid前8位>/`，结果写在各自会话目录的 `results/` 下；会话 ID 由 uuid4 随机产生，10 分钟后由 `delete_later` 各自清理。

### 4.4 输出产物含义与入口选择

#### 4.4.1 概念说明

三个入口共用同一套模型，差异全在「**怎么拿到 256×256 patch**」和「**结果怎么落盘**」。把产物梳理成一张表，是本讲最值得带走的成果——以后拿到任何一个 PEAR 输出文件，你能立刻反查它的出生地。

#### 4.4.2 核心流程（产物对照表）

| 入口 | 前提假设 | 产物 | 内容 | 写出代码 |
|---|---|---|---|---|
| `inference_images.py` | 图中可有多人 | `mesh_{name}.jpg` | 放大 2 倍原图 + 所有人体网格贴回 | [L368](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_images.py#L368) |
| | | `video.mp4` | 上面的图按序合成 30fps 视频 | [L370-L374](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_images.py#L370-L374) |
| `inference_wo_detect.py` | 单人居中 | `mesh_{name}.jpg` | 1024×1024 黑底纯网格 | [L97](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L97) |
| | | `result_{name}.jpg` | 网格与提亮原图 0.5/0.5 叠加 | [L111](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L111) |
| `app.py` | 单人视频前 3 秒 | `mesh_video.mp4` | 平滑后的网格动画 | [L485-L498](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L485-L498) |
| | | `results.npz` | faces 有值；vertices 当前为空 | [L505-L507](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L505-L507) |

注意一个常见误会：**`result_*.jpg` 不是 `inference_images.py` 的产物**。多人入口只写 `mesh_*.jpg`（把网格直接覆盖在原图上，不做半透明混合）；带 `result_` 前缀的叠加图只出自无检测版。两个入口恰好都用 `mesh_` 前缀但内容完全不同（一个是贴回原图的多人结果，一个是黑底纯渲染）。

#### 4.4.3 源码精读

三个入口的落盘语句汇总（均已在上文精读，此处集中引用便于反查）：

- 多人贴回写出：[inference_images.py:L365-L368](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_images.py#L365-L368)——`np.clip` 后 imwrite，注意它写在 `for bbox_id` 循环**之外**、`for img_path` 循环**之内**，所以一张图只写一次、包含全部人体。
- 纯网格 + 叠加写出：[inference_wo_detect.py:L97](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L97) 与 [inference_wo_detect.py:L111](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L111)。
- 视频与 npz 写出：[app.py:L485-L507](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L485-L507)。

另一个横向对照点：三个入口的**相机构造完全一致**——`build_cameras_kwargs(batch_size, 24)`（焦距 24、1024×1024 像 平面）配 `GS_Camera(R=pd_cam[:3,:3], T=pd_cam[:3,3])`，见 [inference_images.py:L341](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_images.py#L341)、[inference_wo_detect.py:L91](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L91)、[app.py:L466](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L466)；焦距 24、1024×1024 像平面。这为 u3-l4 讲相机模型埋下伏笔。

#### 4.4.4 代码实践

**实践目标**：亲手验证「`results.npz` 的 vertices 为空是 L472 注释导致的」这一论断。

**操作步骤**（源码阅读 + 可选修改实验）：

1. 先做只读验证：打开 [app.py:L470-L472](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L470-L472)，确认 `# vertices_list.append(...)` 整行被注释，且 `vertices_list` 在 L373 初始化后再无 append。
2. 可选实验（改完记得还原）：取消 L472 的注释重新跑一遍 `app.py`，再检查 `results.npz` 的 `vertices.shape` 是否变为 `(T, V, 3)`（T = 帧数 ≈ 3 秒 × fps，V ≈ 10475 顶点）。

**需要观察的现象 / 预期结果**：只读验证应看到注释确实存在；修改实验中 `vertices.shape` 从 `(0, 0, 3)` 变为带真实帧数与顶点数的数组。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：你拿到一批 `mesh_000xx.jpg`（放大图上贴着多个网格）和一份 `video.mp4`，它们来自哪个入口？

**答案**：`inference_images.py`。「多人网格贴回原图 + 序列合成 video.mp4」是该入口独有的产物组合。

**练习 2**：想在无 GPU 的笔记本上给同事演示 PEAR，选哪个入口？为什么？

**答案**：`app.py`。它的 `TORCH_DEVICE = cuda if available else cpu`（L122）自动退化到 CPU，且网页交互不需要命令行；两个图像入口则显式调用 `.cuda()`（如 [inference_wo_detect.py:L63](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/inference_wo_detect.py#L63)），无 GPU 会直接报错。

**练习 3**：`mesh_video.mp4` 为什么强调 `yuv420p` 和 `faststart`，而 `inference_images.py` 的 `video.mp4` 没管这些？

**答案**：`mesh_video.mp4` 要在浏览器的 HTML5 `<video>` 标签里播放：`yuv420p` 是广泛支持的像素格式，`faststart` 把元数据挪到文件头以支持边下边播（L478-L490 注释写明了意图）。`inference_images.py` 走 `imageio.mimwrite` 默认参数（[utils/get_video.py:L41-L45](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/get_video.py#L41-L45)），本地播放器通常也能打开，但浏览器兼容性不保证。

## 5. 综合实践

**任务：三个入口全跑一遍，编制一份《PEAR 产物出生证明》。**

1. **准备**：按 u1-l2 配好环境与 `assets/`，确认 `python app.py` 顶部打印 `🎉 All systems go!`。
2. **跑三个入口**：

   ```bash
   python inference_images.py --input_path example/images                       # 产物 A
   python inference_wo_detect.py --input_path data_input/single --output_path data_input/single_out   # 产物 B
   python app.py                                                                # 产物 C：上传 example/example_1.mp4
   ```

3. **收集产物**：A → `example/images_output/` 下的 `mesh_*.jpg` 与 `video.mp4`；B → `data_input/single_out/` 下的 `mesh_*.jpg` 与 `result_*.jpg`；C → `temp_local/session_*/results/` 下的 `mesh_video.mp4` 与 `results.npz`（以及界面下载的 npz）。
4. **填表**（把 4.4.2 的表抄下来，逐行补上你机器上的实际文件名、数量、分辨率）：

   | 产物 | 入口 | 写出代码（文件:行号） | 我机器上的实际数量/尺寸 |
   |---|---|---|---|
   | `mesh_*.jpg`（多人贴回） | inference_images.py | L368 | 待填 |
   | `video.mp4` | inference_images.py | L370-L374 | 待填 |
   | `mesh_*.jpg`（纯网格） | inference_wo_detect.py | L97 | 待填 |
   | `result_*.jpg` | inference_wo_detect.py | L111 | 待填 |
   | `mesh_video.mp4` | app.py | L485-L498 | 待填 |
   | `results.npz` | app.py | L505-L507 | 待填（faces/vertices shape） |

5. **核对边界情况**：给入口 A 混入一张无人图，确认对应 `mesh_*.jpg` 缺失；给入口 B 一张多人图，观察网格错位；上传超过 3 秒的视频给入口 C，确认只处理前 3 秒。
6. **收尾**：确认 `temp_local/` 在约 10 分钟后自动清空，验证 `delete_later` 生效。

完成后你拥有的不只是几张图，而是一张「文件名 ↔ 源码行」的对照网——后续任何一讲深入某个模块时，都可以回到这张网定位它在真实链路中的位置。

## 6. 本讲小结

- 三个入口共享「Config → Renderer2 → hf_hub_download 权重 → Ehm_Pipeline → EHM_v2 → GS_Camera → render_mesh」的组装套路，差异在 patch 的获取方式（YOLO 检测裁剪 / 整图 pad / 视频逐帧 pad）与落盘格式。
- `inference_images.py`：唯一支持多人的入口；`load_img` 先放大 2 倍，YOLO(`classes=0, conf=0.5`) 检测后 `process_bbox` 扩框 1.25 倍裁 256 patch，`warpAffine(inv_trans)` 贴回，写出 `mesh_*.jpg`，最后按文件名序号合成 `video.mp4`。
- `inference_wo_detect.py`：链路最短的入口，假设单人居中；它是唯一产出 `result_*.jpg`（alpha=0.5 与提亮背景混合）的地方。
- `app.py`：模型在 import 阶段装配完毕；`handle_video_upload` 截前 3 秒并存入 uuid 会话目录（600 秒自动清理），`mesh_inference` 逐帧推理后对 body/flame/cam 三组参数做 Savitzky–Golay 平滑再渲染，写出浏览器友好的 `mesh_video.mp4` 和 `results.npz`。
- 当前版本 `results.npz` 的 `vertices` 为空数组，因为收集顶点的 [app.py:L472](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/app.py#L472) 被注释——这是源码事实，不是使用错误。
- 两个易踩的坑：`model_zoo/` 不在仓库中（YOLO 权重需自动下载或手动放置）；`inference_wo_detect.py` 默认输入输出同目录，注意用 `--output_path` 隔离。

## 7. 下一步学习建议

本讲只把三个入口当黑盒跑通了，接下来按依赖顺序深入：

1. **u2-l1（ConfigDict 配置系统）**：三个入口都在用的 `ConfigDict / add_extra_cfgs / device_parser` 到底做了什么，为什么 `meta_cfg` 打印出来是那副模样。
2. **u2-l2（inference_wo_detect.py 逐行走读）**：以最短链路为标本，精读 `pad_and_resize → to_tensor → 前向 → EHM_v2 → GS_Camera → render_mesh` 的每一步张量形状变化。
3. **u2-l3（多人检测与仿射裁剪）**：本讲跳过的 `process_bbox / generate_patch_image / warpAffine` 的数学细节，以及拿 `calculate_iou / non_max_suppression` 做手工 NMS 实验。
4. **u2-l4（app.py 深入）**：Gradio 会话管理与时序平滑的完整设计。
5. 若你更关心模型本体，可直接跳 **u2-l5（Ehm_Pipeline.forward）**，再看单元三的网络结构精读。
