# YOLOv8 架构与训练入口

## 1. 本讲目标

本讲是第三单元「YOLOv8 训练与框架定制」的第一篇，承接 [u1-l3 端到端工作流总览](u1-l3-end-to-end-pipeline.md) 中流水线的**第二阶段——模型训练**。

学完本讲，你应当能够：

1. 说清楚 YOLOv8「骨干（backbone）—颈部（neck）—检测头（head）」三段式结构各自做什么、数据如何在三者之间流动。
2. 看懂并写出 Ultralytics 的 `yolo train / val / predict` 三条 CLI 命令，理解 `model / data / imgsz` 等关键参数的含义。
3. 说清楚本项目对上游 Ultralytics v8.2.9 做了哪**五类修改**、各自为了解决什么 SAR 船舶检测特有的问题，并知道这五类修改分别会在后续哪一篇讲义里深入。

本讲只做「地图」级别的总览，具体的归一化、PIoU2 损失、xView3 指标实现细节留给 u3-l2～u3-l5。

## 2. 前置知识

阅读本讲前，建议你已经了解：

- **目标检测（Object Detection）**：不仅回答「图里有什么」，还要回答「在哪里」——用矩形框（bounding box）标出每个目标的位置与类别。
- **YOLO 系列**：You Only Look Once，一类「单阶段（one-stage）」检测器。它不像两阶段方法（先出候选框再分类）那样分两步，而是把整张图一次性送进网络、直接输出所有框，因而速度快，适合本项目的星载实时场景。
- **端到端流水线**：回顾 [u1-l3](u1-l3-end-to-end-pipeline.md) 的七阶段。本讲对应阶段②（训练），它的输入是 [u2-l2/u2-l3](u2-l2-chip-generation.md) 生成的 800×800（或 640×640）芯片与 YOLO 标签，输出是一个训练好的 `.pt` 权重文件，供阶段③量化使用。

> 一个关键事实先说在前面：本仓库的 `software/training/` 目录里**并不包含完整的 Ultralytics 源码**，只包含四份「修改文件」——`README.md`、`metrics.py`、`prune.py`、`xview3_metrics.py`。它们是相对上游 [Ultralytics v8.2.9](https://github.com/ultralytics/ultralytics) 的**补丁/替换文件**，需要你先把上游框架装好，再用这几份文件覆盖/补充进去。因此，YOLOv8 的网络结构定义（各种 `.yaml`）在上游仓库里，而本仓库只提供「针对 SAR 改了哪些地方」。这一点贯穿后续整个第三单元。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [software/training/README.md](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/README.md) | 训练组件总说明：依赖安装、五类框架修改、`yolo` CLI 用法。**本讲的主要依据**。 |
| [assets/yolov8_diagram.jpg](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/assets/yolov8_diagram.jpg) | YOLOv8 架构图，分 (a) backbone、(b) neck、(c) head、(d) 端到端流水线四块。 |
| [software/training/metrics.py](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/metrics.py) | 修改后的 `ultralytics/utils/metrics.py`：新增 `compute_piou`（PIoU2 损失）与 xView3 指标调用入口。本讲只做总览，细节见 u3-l3/u3-l4。 |
| [software/training/prune.py](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/prune.py) | 基于 `cKDTree` 的距离阈值 NMS，对应 `ultralytics/utils/prune.py`。细节见 u3-l5。 |
| [software/training/xview3_metrics.py](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/xview3_metrics.py) | xView3-SAR 竞赛四项 F1 指标与近岸筛选的实现，对应 `ultralytics/utils/xview3_metrics.py`。细节见 u3-l4。 |

注意 `metrics.py` 顶部的导入就能印证「替换上游 utils 文件」这一点：

[software/training/metrics.py:1-7](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/metrics.py#L1-L7) —— 它 `from ultralytics.utils import ...` 并把 `compute_piou` 设计成「to be used with `ultralytics.utils.metrics.bbox_iou`」，说明它正是要被放进上游 `ultralytics/utils/` 目录、去增强 `metrics.py` 里的 `bbox_iou` 函数。

## 4. 核心概念与源码讲解

### 4.1 YOLOv8 网络结构

#### 4.1.1 概念说明

YOLOv8 是 Ultralytics 在 YOLOv5 之后推出的检测框架。它的网络可以切成三段，正好对应架构图的 (a)(b)(c)：

- **(a) Backbone 骨干网络**：负责「特征提取」。把一张输入图像（本项目是 800×800 或 640×640 的 SAR 三通道芯片）一层层下采样，逐步抽出从「低级纹理」到「高级语义」的多层特征图。核心模块是 **Conv**（卷积+归一化+激活）和 **C2f**（一种 CSP 瓶颈结构，相比 YOLOv5 的 C3 有更丰富的梯度流），末端还有一个 **SPPF**（快速空间金字塔池化）来扩大感受野。
- **(b) Neck 颈部**：负责「多尺度特征融合」。它把骨干不同层、不同分辨率的特征图拼（Concat）在一起，既保留高分辨率图的小目标细节，又保留低分辨率图的强语义。YOLOv8 采用 **FPN（自顶向下）+ PAN（自底向上）** 的双向融合结构。
- **(c) Head 检测头**：负责「输出预测」。YOLOv8 用**解耦头（decoupled head）**——分类和边界框回归各走一条分支；并且是 **anchor-free（无锚框）** 的，直接预测目标中心点；边界框回归用 **DFL（Distribution Focal Loss）** 把每条边建模成一个离散分布。检测头通常在三个尺度（P3/P4/P5，步长 8/16/32）上各输出一次，以兼顾大中小目标。
- **(d)** 则是把前三段串起来的简化端到端流水线示意。

> 架构图的四块标题在仓库 README 中有明确说明，见 [software/training/README.md:5-6](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/README.md#L5-L6)：
> *(a) feature extraction backbone, (b) multi-scale feature fusion neck, (c) detection head, (d) simplified end-to-end detection pipeline*。

对 SAR 船舶检测特别重要的一点：**船在卫星图里往往是「点目标」、非常小**。这正是后续 [u6-l3](u6-l3-postprocess-optimization.md) 会讲到的 **P2 高分辨率检测头**的动机——在更密的 P2 尺度（步长 4）上加一层预测，专门抓小目标。本讲先建立三尺度（P3/P4/P5）的标准结构认知，P2 留到推理阶段讲。

#### 4.1.2 核心流程

一张芯片在网络里的前向流动可以概括为：

```text
输入 (H×W×3, 例如 800×800×3)
        │
   ┌────▼────┐
   │ Backbone │  Conv/C2f 逐层下采样，产出多尺度特征图
   └────┬────┘  典型输出: P3(stride8) / P4(stride16) / P5(stride32)
        │
   ┌────▼────┐
   │  Neck   │  FPN 自顶向下 + PAN 自底向上，Concat 融合多尺度
   └────┬────┘
        │
   ┌────▼────┐
   │  Head   │  解耦头: 分类分支 + 回归分支(DFL)，在 P3/P4/P5 各出一次
   └────┬────┘
        │
   输出: 每个位置 [类别分数 × C, 4 条边的分布] → 经后处理(NMS)得到最终检测框
```

其中输入分辨率 \(H=W=\text{imgsz}\) 是一个关键旋钮（见 4.2）。下采样的总倍数由步长决定，P5 的感受野对应原图 \(32\times32\) 像素的区域；要检测比这更小的目标，就得靠更浅的 P3 甚至 P2 头。

#### 4.1.3 源码精读

如前所述，**网络结构的层定义并不在本仓库里**，而在上游 Ultralytics 的模型配置文件（如 `ultralytics/cfg/models/v8/yolov8.yaml`）中。本仓库 [software/training/README.md:3](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/README.md#L3) 明确指出了这一点：

> *This work uses the Ultralytics YOLOv8 framework … modified from release `v8.2.91`. For full codebase and more information on the YOLOv8 framework, see the [Ultralytics YOLOv8 repository](http://github.com/ultralytics/ultralytics).*

所以阅读架构图时，要把它和上游仓库的结构定义对照看：图里 (a) 的每个 `C2f`、`SPPF` 块，对应 `yolov8.yaml` 里 backbone 段的一行；图里 (b) 的 `Concat`，对应 neck 段；图里 (c) 的 `Detect`，对应 head 段。本仓库只改了「数据怎么进、IoU 怎么算、指标怎么评」，没改网络结构本身（量化阶段对结构的改造见第四单元）。

#### 4.1.4 代码实践

**实践目标**：把架构图的三段结构和真实模块对上号。

**操作步骤**：

1. 打开 [assets/yolov8_diagram.jpg](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/assets/yolov8_diagram.jpg)，在图上用三支不同颜色的笔圈出 (a) backbone、(b) neck、(c) head 的范围。
2. 在 (a) 中找到一个 `C2f` 模块和一个 `SPPF` 模块；在 (b) 中找到至少两个 `Concat` 融合点；在 (c) 中找到分类分支与回归分支的分流处。
3. 在讲义/笔记里写出：「P3/P4/P5 三个尺度的输出分别擅长检测大/中/小哪种目标」，并解释为什么小船更依赖浅层（P3 甚至 P2）。

**需要观察的现象**：你会看到 neck 里有「上采样（放大）+ Concat」和「下采样（缩小）+ Concat」两条相反方向的路径——这就是 FPN+PAN 的双向融合。

**预期结果**：能口头复述「backbone 抽特征 → neck 融合多尺度 → head 解耦输出分类与框」三步。

> 说明：本仓库不含完整网络定义，因此这一步是「读图 + 查上游 yaml」的源码阅读型实践，不需要运行命令。

#### 4.1.5 小练习与答案

**练习 1**：YOLOv8 的检测头为什么叫「解耦头（decoupled head）」？
**答案**：因为它把**分类**和**边界框回归**拆成两条独立的分支，各自用独立的卷积预测，不再像早期 YOLO 那样共用一套输出。这样两个任务互不干扰，收敛更好、精度更高。

**练习 2**：SAR 船舶常常只有几个像素大，标准三尺度（P3/P4/P5）可能不够，本项目的应对思路是什么？
**答案**：在更浅、分辨率更高的 **P2（步长 4）** 上再加一层检测头，专门负责小目标。这部分在推理/后处理阶段（[u6-l3](u6-l3-postprocess-optimization.md)）展开。

### 4.2 Ultralytics CLI

#### 4.2.1 概念说明

Ultralytics 把训练、验证、推理都封装进一个 `yolo` 命令行工具。你不用写 Python 脚本，直接在终端敲 `yolo train ...`、`yolo val ...`、`yolo predict ...` 即可。所有参数都可以「键=值」的形式在命令行覆盖，配置体系基于 `.yaml` 文件。

项目里的模型结构与数据集配置都以 `.yaml` 形式存放在（上游的）`ultralytics/cfg/` 下，默认参数则在 `ultralytics/cfg/default.yaml`，见 [software/training/README.md:69](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/README.md#L69) 与 [software/training/README.md:79](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/README.md#L79)。

#### 4.2.2 核心流程

三条核心命令的职责：

```text
yolo train   model=<模型yaml或预训练pt>  data=<数据集yaml>  imgsz=<800|640>  [其他覆盖项]   # 训练 → 产出 .pt
yolo val     model=<训练好的.pt>         data=<数据集yaml>  imgsz=<800|640>  split=test     # 验证 → 产出指标
yolo predict model=<训练好的.pt>         source=<单张芯片路径>                                # 推理 → 产出框
```

关键参数：

- `model`：训练时指向**模型结构 yaml**（如 `yolov8n.yaml`/`yolov8s.yaml` 等不同规模）或一个预训练 `.pt`；验证/推理时指向训练好的 `.pt`。
- `data`：指向**数据集 yaml**，里面声明了 train/val 路径、类别数与类别名（本项目是 3 类：非船/船/渔船，见 [u2-l3](u2-l3-label-conversion.md)）。
- `imgsz`：训练/推理的**输入分辨率**，本工作固定取 **800 或 640**。这是本讲最重要的旋钮。

**为什么是 800 或 640？** 因为它必须和 [u2-l2](u2-l2-chip-generation.md) 里切片用的 `imgsz` **完全一致**——芯片切多大，网络就吃多大。两个取值的权衡：

| 维度 | `imgsz=800` | `imgsz=640` |
|------|-------------|-------------|
| 单芯片像素数 | \(800^2=64\)万 | \(640^2\approx41\)万 |
| 显存/算力 | 更高 | 更低（YOLO 默认值） |
| 小目标分辨率 | 更好（同样船占更多像素） | 略差 |
| 单场景切出的芯片数 | 更少 | 更多（见 u2-l2 的裁剪数公式） |

简言之：800 用更多显存换更高的小目标分辨率，640 更轻量。这是「精度 vs 资源」的早期权衡点，和后续量化、HLS 加速的资源预算一脉相承。

#### 4.2.3 源码精读

三条命令的原文在 [software/training/README.md:71-78](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/README.md#L71-L78)：

```bash
# training
yolo train model="<path-to-model-yaml>" data="<path-to-dataset-yaml>" imgsz=<800|640> <additional-config-override>
# evaluation
yolo val model="<path-to-model.pt>" split="test" data="<path-to-dataset-yaml>" imgsz=<800|640>
# inference
yolo predict model="<path-to-model.pt>" source="<path-to-chip>"
```

注意 `imgsz=<800|640>` 这种写法表示「二选一」。命令行里所有「键=值」都会覆盖 `default.yaml` 的默认值，README 在 [software/training/README.md:79](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/README.md#L79) 提醒：默认参数在 `ultralytics/cfg/default.yaml`，可按需覆盖。

环境准备方面，README 给了两步（注意：`environment.yml` 在本仓库中**未被 git 跟踪**，需要你自行准备或参考上游说明）：
- 用 conda 安装依赖：[software/training/README.md:9-13](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/README.md#L9-L13)
- 以可编辑模式安装本包：[software/training/README.md:15-20](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/README.md#L15-L20)（`pip install -e .`，注意要把 Python 包路径加进 `PATH`）

#### 4.2.4 代码实践

**实践目标**：写出一条合法的训练命令并解释 `imgsz`。

**操作步骤**：

1. 假设你的模型结构 yaml 叫 `xview3-yolov8s.yaml`、数据集 yaml 叫 `xview3-sar.yaml`，仿照 README 写出训练命令。
2. 再写一条把 `imgsz` 从 800 改成 640 的命令，并预测它对「单张 GPU 上的 batch size 上限」和「小船召回率」的影响。

**预期结果（示例命令，非仓库原有命令，标注为示例）**：

```bash
# 示例命令：结构、数据集、分辨率均为占位，需替换为本机真实路径
yolo train model="xview3-yolov8s.yaml" data="xview3-sar.yaml" imgsz=800 epochs=100 batch=16
```

**待本地验证**：实际的 `model`/`data` yaml 文件名、可用 batch size 都取决于你的数据与显卡，本仓库未给出具体取值，需在你本地环境验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `imgsz` 必须和切片时的尺寸一致？
**答案**：因为芯片在 [u2-l2](u2-l2-chip-generation.md) 就按 `imgsz` 切好了，训练时网络直接吃整张芯片。若训练 `imgsz` 与切片尺寸不符，Ultralytics 会再次 resize，破坏 SAR 像素与标签坐标的对应关系，也浪费算力。

**练习 2**：`yolo val` 与 `yolo predict` 都能产出检测框，它们在本项目里的区别是什么？
**答案**：`val` 跑整个验证集并**计算指标**（本项目里会触发 xView3 四项 F1，见 4.3 的修改 5）；`predict` 只对单张/几张图出框，用于看效果，不算指标。

### 4.3 框架修改总览

#### 4.3.1 概念说明

把上游 Ultralytics 直接拿来训练 SAR 图像会遇到一连串「水土不服」：SAR 是 GeoTIFF 多通道、值域和普通照片完全不同、目标极小、评估指标是竞赛自定义的……于是本项目对 v8.2.91 做了**五类修改**。这一节是这五类修改的**索引**，每一条都指向后续某篇讲义的深入讲解。

这五类修改的完整列表在 [software/training/README.md:24-65](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/README.md#L24-L65)（「Framework Modifications」整节）。

#### 4.3.2 核心流程

五类修改按「数据怎么进 → 损失怎么算 → 结果怎么评」的顺序串起来：

```text
原始 SAR 芯片
   │
   ├─(修改1) 用 IMREAD_UNCHANGED 读 TIFF          [解决：cv2 默认读不了多通道 TIFF]
   │
   ├─(修改2) 自定义线性归一化 SAR→uint8            [解决：SAR 值域[-50,20]/水深[-6000,2000] 非常规照片]
   │         ↓ 产出的 uint8 图即可复用默认数据增强
   │
   ├─(训练: backbone→neck→head)
   │
   ├─(修改3) bbox_iou 里用 PIoU2 替代 CIoU         [解决：小目标框回归，PIoU 非单调聚焦更优]
   │
   └─(验证/评估)
       ├─(修改4) update_metrics 用 chip offset    [解决：把芯片内局部坐标还原成场景全局坐标]
       │         还原到全局坐标，存进 predictions 字典
       └─(修改5) get_stats 调 xview3_metrics      [解决：用竞赛四项 F1 而非默认 mAP 评估]
                 输出 Detection/Near-shore/Vessel/Fishing F1
```

#### 4.3.3 源码精读

下面逐条引用 README 的说明位置（实现细节在后续讲义）：

1. **图像加载（Image Loading）**——[software/training/README.md:26](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/README.md#L26)：给每处 `cv2.imread` 加 `cv2.IMREAD_UNCHANGED` 标志，才能正确读取（多通道、int16 的）TIFF。细节见 [u3-l2](u3-l2-sar-normalization.md)。

2. **自定义归一化（Custom Image Normalisation）**——[software/training/README.md:28-37](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/README.md#L28-L37)：在 `BaseDataset.get_image_and_label` 里、`load_image` 之后调用 `normalize`，把 SAR 波段 `[-50,20]`、bathymetry `[-6000,2000]` 线性映射到 uint8。这样就能**直接复用** Ultralytics 默认数据增强，省去重写一套增强逻辑：

   ```python
   def normalize(self, img):
       min_values = np.array([-6000, -50, -50])
       max_values = np.array([2000, 20, 20])
       return (np.clip((img - min_values) / (max_values - min_values), 0, 1) * 255).round().astype(np.uint8)
   ```

   细节（含 min/max 数组顺序为何是 `[-6000,-50,-50]`）见 [u3-l2](u3-l2-sar-normalization.md)。

3. **IoU 指标修改（Modified IoU Metrics）**——[software/training/README.md:40](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/README.md#L40)：用 PIoU2（Liu, 2024）替代默认 CIoU，做法是在 `metrics.py` 的 `bbox_iou` 函数里加入 `compute_piou` 方法。`compute_piou` 的真实实现见 [software/training/metrics.py](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/metrics.py)，数学推导见 [u3-l3](u3-l3-piou2-loss.md)。

4. **验证流程（Validation）**——[software/training/README.md:42-60](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/README.md#L42-L60)：在 `update_metrics` 中，把每个 batch 的预测收集起来，**用 chip offset 把芯片内局部坐标还原成场景全局坐标**（`detect_scene_row/column`），存进 `self.predictions` 字典供评估。关键片段：

   ```python
   if self.xview_eval:
       chip_id = batch["im_file"][si].split("/")[-1].split(".")[0]
       scene, offset_y, offset_x = self.coords_offset[chip_id]
       ...
       self.predictions["detect_scene_column"].extend((offset_x+pred_coords[:, 0]).tolist())
       self.predictions["detect_scene_row"].extend((offset_y+pred_coords[:, 1]).tolist())
       ...
   ```

   这一步和 [u3-l5](u3-l5-validation-nms.md) 的全局坐标还原、KDTree NMS 紧密相关。

5. **xView3 指标（xView3 Metrics）**——[software/training/README.md:62-65](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/README.md#L62-L65)：把 xView3-SAR 竞赛的四项 F1（Detection、Near-shore Detection、Vessel、Fishing）及推理后处理实现进 `metrics.py`，并在 `get_stats` 里调用：

   ```python
   self.xview3_metrics_out = self.xview3_metrics.process(df)
   ```

   四项 F1 与匈牙利匹配的细节见 [u3-l4](u3-l4-xview3-metrics.md)。

> 贯穿这五类修改的一条暗线，是 [u1-l3](u1-l3-end-to-end-pipeline.md) 提到的「**训推一致性**」：归一化、IoU 这两件事在 Python 训练侧和 C++ 推理侧（[u6-l1/u6-l2](u6-l1-patch-overview.md)）都各做了一遍，必须保持一致，否则就会掉精度。

#### 4.3.4 代码实践

**实践目标**：建立「五类修改 → 后续讲义」的索引表。

**操作步骤**：

1. 阅读一遍 [software/training/README.md:24-65](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/software/training/README.md#L24-L65) 的「Framework Modifications」整节。
2. 在笔记里做一张三列表格：**修改编号 | 改了哪个上游文件/方法 | 在本手册哪一篇深入讲解**。
   - 例：修改 2 | `BaseDataset.get_image_and_label` + 新增 `normalize` | u3-l2。
3. 思考：这五类修改里，哪几类是「不改就没法跑 SAR 数据」，哪几类是「不改也能跑、但指标/精度会差」？

**预期结果**：能填出类似下表（答案见 4.3.5）。

#### 4.3.5 小练习与答案

**练习 1**（对应 4.3.4 步骤 3）：把五类修改分成「不改跑不起来」与「不改能跑但效果差」两类。
**答案**：
- 不改跑不起来：①图像加载（读不了 TIFF）、②归一化（SAR 值域异常，直接喂网络会崩或严重失真）。
- 不改能跑但效果差：③IoU（CIoU 对小目标不如 PIoU2）、④验证流程（不出全局坐标就接不上 xView3 评估）、⑤xView3 指标（用默认 mAP 评不出竞赛关心的近岸/渔船 F1）。

**练习 2**：修改 2 里 `min_values`/`max_values` 数组是 3 个元素，对应哪三个通道？为什么顺序是 `[-6000,-50,-50]` 而不是按 VV/VH/bathymetry？
**答案**：对应 bathymetry、VV、VH 三个通道（值域分别约 [-6000,2000]、[-50,20]、[-50,20]）。这里的数组顺序必须和 `normalize` 接收到的 **img 波段顺序**严格对应——具体取决于芯片写出时波段的排列（见 [u2-l1](u2-l1-dataset-structure.md)：波段顺序固定为 1=VV、2=VH、3=bathymetry）。本练习留作悬念，精确对应关系在 [u3-l2](u3-l2-sar-normalization.md) 给出。

## 5. 综合实践

把本讲三块内容串起来，完成一份「训练阶段速查卡」：

1. **结构标注**：打开 [assets/yolov8_diagram.jpg](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/assets/yolov8_diagram.jpg)，在 (a)/(b)/(c) 三块旁分别写出 1 句话职责，并指出 (a) 中的 `C2f`、`SPPF`，(b) 中的 `Concat`，(c) 中的解耦分支。
2. **命令撰写**：写出一条完整的 `yolo train` 示例命令（含 `model / data / imgsz / epochs / batch`），并写一段 50 字说明，解释为什么本项目 `imgsz` 取 800 而不是 YOLO 默认的 640（从「小目标分辨率」和「显存」两方面谈）。
3. **修改索引**：列出五类框架修改，每条标注「改了哪个上游方法」与「后续讲义编号」，并指出哪两类修改在推理侧（C++）也必须一致重做。

完成这张速查卡，你就建立了第三单元的全局地图，后续 u3-l2～u3-l5 会逐一展开每条修改的实现细节。

## 6. 本讲小结

- YOLOv8 由 **backbone（C2f/SPPF 抽特征）→ neck（FPN+PAN 多尺度融合）→ head（解耦、anchor-free、DFL）** 三段构成，对小目标需要更浅的 P2 头（后续 [u6-l3](u6-l3-postprocess-optimization.md) 详述）。
- 本仓库 `software/training/` **只含 4 份修改文件**，是相对上游 Ultralytics v8.2.91 的补丁；网络结构定义在上游 `.yaml` 里。
- 训练/验证/推理用 `yolo train / val / predict` 三条 CLI，参数以「键=值」覆盖 `ultralytics/cfg/default.yaml`。
- `imgsz` 必须与切片尺寸一致，本项目取 **800 或 640**：800 用显存换小目标分辨率。
- 本项目对框架做了**五类修改**：图像加载（TIFF）、自定义归一化、PIoU2 IoU、验证全局坐标还原、xView3 指标——分别对应 u3-l2～u3-l5。
- 归一化与 IoU 这两类修改存在「训推一致性」暗线，Python 训练侧与 C++ 推理侧各做一遍必须保持一致。

## 7. 下一步学习建议

本讲是第三单元的索引篇。接下来建议按顺序学习：

- **[u3-l2 SAR 多通道图像加载与归一化](u3-l2-sar-normalization.md)**：深入五类修改中的第 1、2 条，弄懂 `IMREAD_UNCHANGED` 与 `normalize` 的细节，特别是 `min/max` 数组顺序的对应关系。
- **[u3-l3 PIoU2 边界框回归损失](u3-l3-piou2-loss.md)**：精读 `compute_piou` 的数学推导与它如何接入 `bbox_iou`。
- **[u3-l4 xView3 评估指标体系](u3-l4-xview3-metrics.md)** 与 **[u3-l5 验证流程、NMS 与全局坐标变换](u3-l5-validation-nms.md)**：吃透第 4、5 条修改。

若你想先跳到「为什么模型要量化、怎么编译到 DPU」，可先读第四单元 [u4-l1](u4-l1-quantization-ptq.md)，但建议先完成 u3-l2 的归一化部分，因为它直接决定量化与推理时的输入一致性。
