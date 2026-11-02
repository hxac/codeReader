# 跑通第一个推理 Demo

## 1. 本讲目标

学完本讲，你应该能够：

1. 独立使用 `demo/demo.py` 加载 MODEL_ZOO 中的预训练权重，对任意一张图片跑通全景分割推理（含 CPU 环境）。
2. 说清楚一次端到端推理的完整链路：`setup_cfg` 组装配置 → `VisualizationDemo` 构建预测器 → `run_on_image` 完成推理与可视化 → 结果落盘。
3. 理解 `DefaultPredictor`（detectron2 提供，负责模型与推理）与 `VisualizationDemo`（本仓库提供，负责元数据与画图）的分工边界。
4. 掌握 `--opts MODEL.WEIGHTS ...`、`MODEL.DEVICE cpu`、`INPUT.MIN_SIZE_TEST` 等最常用的命令行配置覆盖方式。

本讲是"动手课"：不深入模型内部（那是单元三的事），只要求把模型当成黑盒跑起来，并看懂胶水代码。

## 2. 前置知识

### 2.1 推理（inference）与训练的区别

- **训练**：模型在数据集上迭代学习，输出的是损失值。
- **推理**：加载已经训练好的权重，对一张新图片输出预测结果（这里是分割掩码）。
- Mask2Former 的 yaml 配置文件是**为训练写的**（见 [GETTING_STARTED.md:21](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/GETTING_STARTED.md#L21)），其中默认的 `MODEL.WEIGHTS` 只是 ImageNet 预训练的 backbone 初始化权重，**不是**完整的分割模型。因此跑 demo 必须用 `--opts MODEL.WEIGHTS` 指向 MODEL_ZOO 里下载的最终 checkpoint。

### 2.2 detectron2 的 `DefaultPredictor`

`DefaultPredictor` 是 detectron2（宿主框架）提供的通用推理封装，源码在你安装的 detectron2 包的 `detectron2/engine/defaults.py` 中（不在本仓库内，属外部依赖）。它做的事：

1. 用 `build_model(cfg)` 构建模型并设为 `eval()` 模式（只构建一次，之后复用）。
2. 用 `DetectionCheckpointer` 加载 `cfg.MODEL.WEIGHTS` 指向的权重。
3. 对每张输入图：按 `cfg.INPUT.MIN_SIZE_TEST` / `INPUT.MAX_SIZE_TEST`（detectron2 默认 800 / 1333）做 `ResizeShortestEdge` 缩放 → 转 tensor → 以 `inputs=[{"image": ..., "height": 原高, "width": 原宽}]` 调用模型 → 把输出搬到 CPU。

记住分工口诀：**DefaultPredictor 管"算"，VisualizationDemo 管"画"**。

### 2.3 本仓库推理输出里有什么（回顾)

在前置讲义（语义分割推理分支）中我们已经知道：MaskFormer 推理返回的 `processed_results` 字典按三个开关（`TEST.SEMANTIC_ON` / `INSTANCE_ON` / `PANOPTIC_ON`）决定包含哪些键——`sem_seg`（每类概率图）、`instances`（带分数的实例掩码）、`panoptic_seg`（id 图 + segments_info）。本讲只需消费这些结果，不再展开其内部计算。

### 2.4 BGR 与 RGB

OpenCV 读图是 BGR 通道序，Matplotlib/detectron2 `Visualizer` 期望 RGB。`image[:, :, ::-1]` 这行反转通道的代码会反复出现，见到就要认得。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [GETTING_STARTED.md](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/GETTING_STARTED.md) | 官方入门文档，给出 demo 命令行与常用参数说明 |
| [demo/demo.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/demo/demo.py) | demo 入口脚本：解析参数 → `setup_cfg` → 循环跑图/摄像头/视频 |
| [demo/predictor.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/demo/predictor.py) | `VisualizationDemo`（含 `run_on_image` / `run_on_video`）与 `AsyncPredictor` |
| [MODEL_ZOO.md](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/MODEL_ZOO.md) | 各模型的 config 路径、指标与权重下载链接 |
| configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml | 本讲使用的 R50 全景分割模型配置（含三个 TEST 开关） |
| [mask2former/__init__.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/__init__.py) | `import mask2former` 触发的注册副作用（模型 + 数据集元数据） |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **demo.py 主流程与 `setup_cfg`**——配置如何一层层叠出来。
2. **`VisualizationDemo` 与 `run_on_image`**——预测与可视化的分工。
3. **常用 opts 与三种输入模式**——命令行怎么控制推理行为。

---

### 4.1 模块一：demo.py 主流程与 setup_cfg

#### 4.1.1 概念说明

detectron2 生态的配置是"洋葱式"的：从内到外一层层包裹默认值，越外层优先级越高。`setup_cfg` 就是把四层配置压成一个最终的 `cfg` 对象。这延续了前置讲义（仓库结构）中"引擎在 detectron2、插件在本仓库"的结论：前两层来自 detectron2（含 deeplab 项目），第三层来自本仓库的 `add_maskformer2_config`，最后两层（yaml 文件、命令行）是使用者给的。

#### 4.1.2 核心流程

```text
get_cfg()                        # 第 1 层：detectron2 基础默认配置
  ↓
add_deeplab_config(cfg)          # 第 2 层：deeplab 项目配置（语义分割相关）
  ↓
add_maskformer2_config(cfg)      # 第 3 层：Mask2Former 专属配置项
  ↓
cfg.merge_from_file(config_file) # 第 4 层：yaml 文件覆盖（含 _BASE_ 继承）
  ↓
cfg.merge_from_list(opts)        # 第 5 层：命令行 KEY VALUE 对，优先级最高
  ↓
cfg.freeze()                     # 冻结，之后只读
```

后写的键覆盖先写的键。所以命令行 `--opts MODEL.WEIGHTS xxx` 能压过 yaml 里的默认权重。

主流程（`__main__` 部分）则是线性的：

```text
解析参数 → setup_cfg(args) → VisualizationDemo(cfg)
  → 若 --input：逐图 read_image(BGR) → run_on_image → save/imshow
  → 若 --webcam / --video-input：run_on_video 逐帧处理
```

#### 4.1.3 源码精读

**路径铺垫**：demo 要在 `demo/` 目录下运行，脚本第 10 行把父目录（仓库根）插进 `sys.path`，这样才能 `import mask2former`（仓库根目录没有 setup.py，源码是原地使用的——见前置讲义"仓库结构"）。脚本所在目录本身在 `sys.path[0]`，所以 `from predictor import ...` 也直接可用：

[demo/demo.py:8-11](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/demo/demo.py#L8-L11) 把仓库根目录加入模块搜索路径。

[demo/demo.py:26-27](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/demo/demo.py#L26-L27) 导入本仓库的配置扩展函数与可视化类——注意 `import mask2former` 这个动作本身就会把模型和数据集元数据注册进 registry（[mask2former/__init__.py:2](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/__init__.py#L2) 的 `from . import data`），漏掉它后面 `build_model` 会报 "Cannot find object"。

**setup_cfg 本体**：

[demo/demo.py:34-42](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/demo/demo.py#L34-L42) 按上文五层顺序组装并冻结配置。整个函数只有 7 行，是理解 detectron2 插件项目配置组装的最小样本（`train_net.py` 的 `setup` 用的是同样的骨架，后面单元会反复见到）。

**参数默认值**：

[demo/demo.py:47-52](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/demo/demo.py#L47-L52) `--config-file` 的默认值就是 R50 全景分割配置，即 GETTING_STARTED 推荐的那个模型。

[demo/demo.py:73-78](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/demo/demo.py#L73-L78) `--opts` 用 `argparse.REMAINDER` 吃掉后面所有 `KEY VALUE` 对，交给 `merge_from_list`。

**主循环**：

[demo/demo.py:106-118](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/demo/demo.py#L106-L118) 构建 `VisualizationDemo`，逐张读图（`read_image(path, format="BGR")`，注释写明用 PIL 读取以与评估保持一致）并调用 `run_on_image`。

[demo/demo.py:129-136](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/demo/demo.py#L129-L136) `--output` 是目录就按原文件名保存，是文件名则要求只有一张输入；不给 `--output` 就弹 OpenCV 窗口显示。

**权重从哪来**：MODEL_ZOO 中 R50 全景分割那一行给出下载链接（`model_final_94dc52.pkl`）：

[MODEL_ZOO.md:53-61](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/MODEL_ZOO.md#L53-L61) 该表格行包含 config 链接、PQ/mIoU/AP 指标与权重下载地址。

#### 4.1.4 代码实践

1. **实践目标**：不跑模型，只验证配置组装与命令行覆盖是否按预期工作。
2. **操作步骤**（纯 CPU、无需权重）：

```bash
cd demo/
python -c "
from demo import setup_cfg, get_parser   # 示例代码：仅在演示时使用 -c 方式
"
```

更稳妥的做法是把下面几行存成 `demo/peek_cfg.py`（示例代码，注意它必须放在 demo/ 目录下才能复用 `sys.path` 铺垫）：

```python
# 示例代码
import sys, os
sys.path.insert(1, os.path.join(sys.path[0], '..'))
from detectron2.config import get_cfg
from detectron2.projects.deeplab import add_deeplab_config
from mask2former import add_maskformer2_config

cfg = get_cfg()
add_deeplab_config(cfg)
add_maskformer2_config(cfg)
cfg.merge_from_file("../configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml")
print("yaml 后的 WEIGHTS:", cfg.MODEL.WEIGHTS)
cfg.merge_from_list(["MODEL.WEIGHTS", "/tmp/final.pkl", "MODEL.DEVICE", "cpu"])
print("opts 后的 WEIGHTS:", cfg.MODEL.WEIGHTS)
print("DEVICE:", cfg.MODEL.DEVICE)
print("三个开关:", cfg.MODEL.MASK_FORMER.TEST.SEMANTIC_ON,
      cfg.MODEL.MASK_FORMER.TEST.INSTANCE_ON,
      cfg.MODEL.MASK_FORMER.TEST.PANOPTIC_ON)
```

3. **需要观察的现象**：`yaml 后的 WEIGHTS` 是 `detectron2://ImageNetPretrained/torchvision/R-50.pkl`；`opts 后` 变成 `/tmp/final.pkl`；三个开关全部为 `True`。
4. **预期结果**：命令行覆盖生效，验证了"第 5 层优先级最高"。权重 URL 覆盖是跑 demo 的必要操作。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `--opts MODEL.WEIGHTS ...` 忘了会怎样？
**答案**：不会报错——`DetectionCheckpointer` 会按 yaml 默认值去加载 `detectron2://ImageNetPretrained/torchvision/R-50.pkl`（仅 backbone 预训练、随机初始化的解码头），模型照常前向但输出基本是噪声。这也是 GETTING_STARTED 特意提醒"配置是为训练写的"的原因。

**练习 2**：`merge_from_file` 和 `merge_from_list` 谁覆盖谁？
**答案**：`merge_from_list`（命令行）覆盖 `merge_from_file`（yaml），因为 `setup_cfg` 中 yaml 合并在前、命令行合并在后，后写覆盖先写。

**练习 3**：为什么 `python demo.py` 必须在 `demo/` 目录下执行？
**答案**：第 10 行的 `sys.path.insert(1, os.path.join(sys.path[0], '..'))` 依赖 `sys.path[0]` 是脚本所在目录（即 `demo/`），从而把仓库根加进搜索路径。从别处执行时相对路径 `../configs/...` 也会失效。

---

### 4.2 模块二：VisualizationDemo 与 run_on_image

#### 4.2.1 概念说明

`VisualizationDemo` 是本仓库在 detectron2 原版 demo 的 `predictor.py` 基础上几乎原样复用的可视化外壳。它解决两个问题：

1. **找到画图的"图例"**：分割可视化需要类别名与颜色表，这些存在 detectron2 的 `MetadataCatalog` 里，靠 `cfg.DATASETS.TEST[0]` 对应的数据集名取回。
2. **把模型的多种输出画到图上**：模型可能返回 `panoptic_seg` / `sem_seg` / `instances` 三种结果，按优先级选择绘制方式。

它与 `DefaultPredictor` 的分工：`DefaultPredictor` 管"算"（构建模型、加载权重、缩放图片、前向、尺寸还原），`VisualizationDemo` 管"画"（元数据、Visualizer、落盘格式）。

#### 4.2.2 核心流程

`__init__` 阶段：

```text
cfg.DATASETS.TEST[0] → MetadataCatalog.get(...)   # 取类别名/颜色图
parallel=False → DefaultPredictor(cfg)            # 单进程默认
parallel=True  → AsyncPredictor(cfg, num_gpus)    # 多进程，视频吞吐用
```

`run_on_image` 阶段：

```text
BGR 图片 → self.predictor(image) → predictions 字典
        → BGR 转 RGB → Visualizer(image, metadata)
        → 若有 panoptic_seg：draw_panoptic_seg_predictions(id图, segments_info)
        → 否则：有 sem_seg 就 argmax 后 draw_sem_seg；
                 有 instances 就 draw_instance_predictions
        → 返回 (predictions, VisImage)
```

注意其中的**互斥分支**：只要 `panoptic_seg` 存在，`sem_seg` 和 `instances` 就不会被画（`else` 结构）。而 R50 全景配置把三个开关全开（[maskformer2_R50_bs16_50ep.yaml:41-43](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml#L41-L43)），所以 predictions 里三个键都有、但默认只画全景——这正是本讲实践任务要改的点。

另外，`DefaultPredictor` 内部按最短边缩放图片，规则可写成：

\[ \text{scale} = \min\left(\frac{\text{MIN\_SIZE\_TEST}}{\min(h, w)},\ \frac{\text{MAX\_SIZE\_TEST}}{\max(h, w)}\right) \]

即先按最短边凑到目标尺寸，但保证最长边不超过上限（detectron2 `ResizeShortestEdge` 的语义）。

#### 4.2.3 源码精读

[demo/predictor.py:18-37](https://github.com/facebook.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/demo/predictor.py#L18-L37) 构造函数：从 `cfg.DATASETS.TEST[0]` 取元数据、记录 CPU 设备与颜色模式，并按 `parallel` 选择 `DefaultPredictor` 或 `AsyncPredictor`。

[demo/predictor.py:26-28](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/demo/predictor.py#L26-L28) 元数据查找。R50 全景配置的 `DATASETS.TEST` 是 `coco_2017_val_panoptic_with_sem_seg`（见 [Base-COCO-PanopticSegmentation.yaml:16-18](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/panoptic-segmentation/Base-COCO-PanopticSegmentation.yaml#L16-L18)）。这个数据集名之所以能被查到，靠的还是 `import mask2former` 时的注册副作用——若在别的脚本里直接用 `VisualizationDemo` 而没有先 import mask2former，这里会抛 `KeyError`。

[demo/predictor.py:39-67](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/demo/predictor.py#L39-L67) `run_on_image` 全貌：

- [L49](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/demo/predictor.py#L49) `predictions = self.predictor(image)`——唯一一行真正"算"的代码，其余都是画图。
- [L50-52](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/demo/predictor.py#L50-L52) BGR→RGB 并创建 `Visualizer`。
- [L53-57](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/demo/predictor.py#L53-L57) 全景分支：`panoptic_seg` 是一张"每个像素一个段 id"的整型图，`segments_info` 描述每个 id 的类别，二者一起交给 `draw_panoptic_seg_predictions` 上色。
- [L59-65](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/demo/predictor.py#L59-L65) 否则分支：`sem_seg` 是每类概率图，先 `argmax(dim=0)` 压成类别图再画；`instances` 是 detectron2 `Instances` 对象（含 `scores`、`pred_masks` 等），直接画。

[demo/predictor.py:69-75](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/demo/predictor.py#L69-L75) `_frame_from_video`：把 `cv2.VideoCapture` 包装成逐帧生成器，供 `run_on_video` 使用。

[demo/predictor.py:131-219](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/demo/predictor.py#L131-L219) `AsyncPredictor`：用 `mp.Process` 在独立进程里各跑一个 `DefaultPredictor`，用任务/结果两个队列保证结果按提交顺序返回。它只为视频吞吐设计，跑静态图片用默认的 `DefaultPredictor` 即可。

#### 4.2.4 代码实践

1. **实践目标**：确认 `predictions` 字典里到底有哪些键、各自是什么形状，为下一模块的改造做准备。
2. **操作步骤**：接 4.1.4 的 `peek_cfg.py`，追加（示例代码）：

```python
import cv2, numpy as np, torch
from predictor import VisualizationDemo

cfg.merge_from_list(["MODEL.DEVICE", "cpu"])
cfg.freeze()
demo = VisualizationDemo(cfg)

img = (np.random.rand(480, 640, 3) * 255).astype("uint8")  # 假图，仅看结构
with torch.no_grad():
    predictions, vis = demo.run_on_image(img)
for k, v in predictions.items():
    print(k, type(v))
print("instances 字段:", predictions["instances"].get_fields().keys()
      if "instances" in predictions else "无")
```

注意：没有加载训练权重时这一步**不需要**下载任何模型文件也能前向（默认 backbone 权重若本地无缓存会被自动下载，无法联网时可将 `MODEL.WEIGHTS` 设为空字符串跳过加载——具体行为待本地验证）。

3. **需要观察的现象**：打印出 `panoptic_seg`（Tensor + list 二元组）、`sem_seg`（形状约 \((133, H, W)\) 的 Tensor）、`instances`（`Instances` 对象）三个键；`vis` 是 `VisImage`。
4. **预期结果**：三个键齐全（因为三个开关全 True），印证 4.2.2 的互斥分支分析——默认可视化只画了全景那张。

#### 4.2.5 小练习与答案

**练习 1**：`MetadataCatalog.get(...)` 抛 `KeyError`，最可能的原因是什么？
**答案**：对应数据集（如 `coco_2017_val_panoptic_with_sem_seg`）没被注册。本仓库靠 `import mask2former` 副作用完成注册，检查脚本是否漏了这行 import（前置讲义"仓库结构"的"import 即注册"机制）。

**练习 2**：为什么 `draw_sem_seg` 前要先 `argmax(dim=0)`？
**答案**：`sem_seg` 是 \(K \times H \times W\) 的每类概率图（softmax 概率），而画图工具需要每像素一个类别 id 的整型图，`argmax` 在类别维取最大者完成压缩。

**练习 3**：`run_on_image` 里 `image[:, :, ::-1]` 的作用？
**答案**：`read_image(format="BGR")` 读入的是 OpenCV 的 BGR 通道序，而 `Visualizer` 按 Matplotlib 的 RGB 约定取色，需要反转通道。同理 `run_on_video` 里用 `cv2.cvtColor` 做了两次方向相反的转换。

---

### 4.3 模块三：常用 opts 与三种输入模式

#### 4.3.1 概念说明

demo 的"控制面"由两部分组成：`demo.py` 自己的 argparse 参数（`--input` / `--output` / `--webcam` / `--video-input` 等），以及穿透到 cfg 的 `--opts`。理解的关键是：**模型行为归 cfg 管，IO 归 argparse 管**。

#### 4.3.2 核心流程

```text
输入路由：
  --input a.jpg b.jpg 或 glob → run_on_image 循环
  --webcam                     → cv2.VideoCapture(0) → run_on_video
  --video-input video.mp4      → cv2.VideoCapture(file) → run_on_video

常用 --opts：
  MODEL.WEIGHTS <path>     # 必填：换成训好的 checkpoint
  MODEL.DEVICE cpu         # 无 GPU 推理
  INPUT.MIN_SIZE_TEST 640  # 改测试缩放尺寸（影响速度/精度）
```

#### 4.3.3 源码精读

[GETTING_STARTED.md:14-20](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/GETTING_STARTED.md#L14-L20) 官方推荐的完整命令：`cd demo/` 后运行 `demo.py`，`--opts MODEL.WEIGHTS` 指向 checkpoint。

[GETTING_STARTED.md:24-29](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/GETTING_STARTED.md#L24-L29) 常用参数清单：webcam、视频输入、`MODEL.DEVICE cpu`、`--output` 的目录/文件语义。

[demo/demo.py:110-113](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/demo/demo.py#L110-L113) 输入只有一个元素时按 glob 展开，所以 `--input 'dir/*.jpg'` 也能一次跑整个目录。

[demo/demo.py:119-127](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/demo/demo.py#L119-L127) 日志输出：图片路径 + 检出的实例数（`len(predictions["instances"])`）+ 耗时。CPU 上单图通常要数十秒（待本地验证）。

[demo/demo.py:67-72](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/demo/demo.py#L67-L72) 一个诚实的细节：`--confidence-threshold` 被 argparse 解析了，但**整个脚本没有任何地方使用 `args.confidence_threshold`**——它继承自 detectron2 原版 demo 却没接上。实例的筛选阈值/数量实际由模型内部的 `TEST.DETECTIONS_PER_IMAGE` 等配置决定（单元六会讲）。

[demo/demo.py:142-152](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/demo/demo.py#L142-L152) 与 [demo/demo.py:153-194](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/demo/demo.py#L153-L194) webcam 与视频分支：都走 `run_on_video`，视频分支还处理了编码格式探测与 `cv2.VideoWriter` 写出。

#### 4.3.4 代码实践

1. **实践目标**：用 `MODEL.DEVICE cpu` 跑通一张真实图片的全景分割。
2. **操作步骤**：

```bash
# 1) 下载 R50 全景分割权重（约 450MB，来自 MODEL_ZOO）
wget https://dl.fbaipublicfiles.com/maskformer/mask2former/coco/panoptic/maskformer2_R50_bs16_50ep/model_final_94dc52.pkl

# 2) 跑 demo（CPU）
cd demo/
python demo.py \
  --config-file ../configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml \
  --input /path/to/your.jpg \
  --output ./output \
  --opts MODEL.WEIGHTS /path/to/model_final_94dc52.pkl MODEL.DEVICE cpu
```

3. **需要观察的现象**：终端 fvcore 日志显示 checkpoint 加载信息与各参数缺失警告；随后打印 `your.jpg: detected N instances in X.XXs`；`./output/your.jpg` 生成，图上叠加了彩色段与类别名。
4. **预期结果**：得到一张带全景分割叠加的可视化图片。GPU 机器去掉 `MODEL.DEVICE cpu` 即可，速度显著更快。若尚未编译 CUDA 算子，请先完成前置讲义（环境搭建与 MSDeformAttn 编译）——CPU 张量会自动走 PyTorch 参考实现。

#### 4.3.5 小练习与答案

**练习 1**：想缩短 CPU 推理时间，除了换小模型还能怎么改？
**答案**：降低测试分辨率，例如 `--opts ... INPUT.MIN_SIZE_TEST 512`（detectron2 默认 800），分辨率减半近似带来数倍加速，精度会略降。

**练习 2**：`--confidence-threshold 0.8` 会改变输出吗？
**答案**：不会。该参数被解析但从未被使用（见 4.3.3），实例数量由模型内部配置控制；想改行为应通过 `--opts` 修改 cfg（如 `MODEL.MASK_FORMER.TEST.DETECTIONS_PER_IMAGE`，单元六详解）。

**练习 3**：`--output` 什么时候是目录、什么时候是文件？
**答案**：若路径是已存在目录则按原文件名写入该目录；否则视为输出文件名，且此时要求输入只有一张图（[demo/demo.py:129-136](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/demo/demo.py#L129-L136)）。

---

## 5. 综合实践

**任务**：改造 `demo/predictor.py` 的副本，让一次推理同时产出全景、语义、实例三张可视化图。

**背景**：4.2 节已分析过，`run_on_image` 的 `if/else` 结构导致三个开关全开时只画全景图。我们要的副本将三种结果各画一张。

**步骤**：

1. 复制源码（**不要改原文件**）：`cp demo/predictor.py demo/predictor_all.py`。
2. 在 `predictor_all.py` 中替换 `run_on_image`（示例代码）：

```python
def run_on_image(self, image):
    predictions = self.predictor(image)
    image = image[:, :, ::-1]
    outputs = {}
    # 全景（保持原有绘制方式）
    if "panoptic_seg" in predictions:
        panoptic_seg, segments_info = predictions["panoptic_seg"]
        vis = Visualizer(image, self.metadata, instance_mode=self.instance_mode)
        outputs["panoptic"] = vis.draw_panoptic_seg_predictions(
            panoptic_seg.to(self.cpu_device), segments_info)
    # 语义：不再放在 else 里，单独一张
    if "sem_seg" in predictions:
        vis = Visualizer(image, self.metadata, instance_mode=self.instance_mode)
        outputs["semantic"] = vis.draw_sem_seg(
            predictions["sem_seg"].argmax(dim=0).to(self.cpu_device))
    # 实例：单独一张
    if "instances" in predictions:
        vis = Visualizer(image, self.metadata, instance_mode=self.instance_mode)
        outputs["instance"] = vis.draw_instance_predictions(
            predictions["instances"].to(self.cpu_device))
    return predictions, outputs
```

注意每个分支都要**新建** `Visualizer`：它把绘制结果累积在内部 `output` 画布上，复用同一实例会把三种结果叠在一张图里。

3. 写驱动脚本 `demo/run_all.py`（示例代码）：

```python
import sys, os
sys.path.insert(1, os.path.join(sys.path[0], '..'))
import cv2
from detectron2.config import get_cfg
from detectron2.projects.deeplab import add_deeplab_config
from mask2former import add_maskformer2_config
from predictor_all import VisualizationDemo   # 我们的副本

cfg = get_cfg()
add_deeplab_config(cfg)
add_maskformer2_config(cfg)
cfg.merge_from_file("../configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml")
cfg.merge_from_list([
    "MODEL.WEIGHTS", "../model_final_94dc52.pkl",
    "MODEL.DEVICE", "cpu",
])
cfg.freeze()

demo = VisualizationDemo(cfg)
img = cv2.imread("my.jpg")            # BGR
predictions, outputs = demo.run_on_image(img)
for name, vis in outputs.items():
    vis.save(f"out_{name}.jpg")
    print("saved", name)
```

4. 运行并对比三张图。

**预期结果与检查点**：

- `out_panoptic.jpg`：整图被彩色段铺满，thing 与 stuff 都有标签；
- `out_semantic.jpg`：同样是整图类别图，但每个像素只按语义类别着色（不区分实例）；
- `out_instance.jpg`：只叠加了检出实例（半透明掩码 + 分数）；
- 终端日志的 `detected N instances` 与 `out_instance.jpg` 中掩码数量一致。

## 6. 本讲小结

- `setup_cfg` 用五层叠加组装配置：`get_cfg` → `add_deeplab_config` → `add_maskformer2_config` → yaml → 命令行 `merge_from_list`，后者覆盖前者，最终 `freeze()`。
- yaml 是为训练写的，跑推理必须用 `--opts MODEL.WEIGHTS` 指向 MODEL_ZOO 下载的最终 checkpoint。
- 分工：`DefaultPredictor`（detectron2）管模型构建、权重加载、缩放与前向；`VisualizationDemo`（本仓库）管元数据查找与三种输出的绘制。
- `run_on_image` 的 `if/else` 使全景结果存在时语义/实例不再绘制——三个 `TEST.*_ON` 开关全开时默认只能拿到全景可视化。
- 元数据靠 `cfg.DATASETS.TEST[0]` 从 `MetadataCatalog` 获取，其可用性依赖 `import mask2former` 的注册副作用。
- 常用控制项：`MODEL.DEVICE cpu` 走 CPU 推理（可变形注意力自动回退 PyTorch 实现），`INPUT.MIN_SIZE_TEST` 调测试分辨率；`--confidence-threshold` 是个未接线的死参数。

## 7. 下一步学习建议

- **u1-l5（配置系统）**：本讲只把 `add_maskformer2_config` 当黑盒，下一讲展开它的全部配置项、`_BASE_` 继承规则与 `CfgNode` 机制。
- **动手预习**：把综合实践脚本里的 `cfg` 打印出来（`print(cfg)`），对照下一讲的配置清单逐项认领。
- **通往模型内部**：跑通 demo 后，你对 `predictions` 三个键的形状已有直觉；单元三的 `MaskFormer.forward` 精读（u3-l1）将解释这些键是怎么算出来的，单元六则逐个拆解三个推理分支的后处理。
