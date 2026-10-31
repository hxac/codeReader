# 项目全景：MMDetection3D 是什么

> 本讲是整本学习手册的第一讲，不要求任何前置知识。读完本讲，你应该能用自己的话说清楚：这个仓库是干什么的、它支持哪些任务和数据集、它依赖哪些库、四大目录（configs / projects / tools / mmdet3d）各自负责什么。

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 MMDetection3D 支持的四大任务类型（LiDAR 3D 检测、视觉 3D 检测、多模态 3D 检测、3D 语义分割），以及每类任务下的代表算法。
2. 列出它支持的常用数据集（KITTI、nuScenes、Waymo、Lyft、ScanNet、SUN RGB-D、S3DIS、SemanticKITTI 等），并知道室内/室外、点云/图像的大致划分。
3. 解释 MMDetection3D 与 MMEngine、MMCV、MMDetection 三者的依赖与复用关系，并能在 `mmdet3d/__init__.py` 中指出对应的版本约束代码。
4. 说出仓库四大目录 `configs/`、`projects/`、`tools/`、`mmdet3d/` 的分工，为后续每一讲建立一张「源码地图」。

## 2. 前置知识

本讲面向零基础读者，但有几个名词最好先有个直观印象：

- **3D 检测（3D Object Detection）**：在三维空间里找出目标物体，输出的是一个「立体的框」，通常用 7 个数描述——中心坐标 \( (x, y, z) \)、长宽高 \( (l, w, h) \)、以及绕竖直轴的朝向角 \( \theta \)。这与 2D 检测只输出平面矩形框不同。
- **点云（Point Cloud）**：LiDAR（激光雷达）扫描得到的一堆三维点，每个点有 \( (x, y, z) \) 坐标，有时还带反射强度。它是无序、稀疏、不规则的，这与图像的规则网格完全不同，也是 3D 检测算法要专门处理点云的原因。
- **单目 3D 检测（Mono3D）**：只用一张（或多张）相机图片来预测 3D 框，难度比用点云更高。
- **多模态（Multi-modal）**：同时使用点云和图像两种输入，让 LiDAR 的精确测距和相机的语义信息互补。
- **3D 语义分割**：不是框住物体，而是给点云里每个点打一个类别标签（地面、车、行人……）。
- **工具箱（toolbox）**：OpenMMLab 系列项目（MMDetection、MMSegmentation 等）的统称——把某个领域的主流算法统一实现成「可配置、可组合、可复现」的代码库，训练新模型通常只需写一个配置文件，而不是从零写训练代码。

不需要预先会 PyTorch 的具体写法，但要知道它是本项目的深度学习框架底座。

## 3. 本讲源码地图

| 文件 / 目录 | 作用 |
| --- | --- |
| [README.md](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/README.md) | 项目门面：定位、特性、支持的算法架构总表、版本动态 |
| [README_zh-CN.md](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/README_zh-CN.md) | README 的中文版，内容对应，方便中文读者对照 |
| [docs/en/model_zoo.md](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docs/en/model_zoo.md) | 模型动物园：各算法在各数据集上的指标与预训练权重入口 |
| [mmdet3d/__init__.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/__init__.py) | 包入口：导入三大依赖并做版本兼容性断言 |
| `docs/en/` 目录 | 官方文档源文件：get_started、user_guides、advanced_guides、notes |
| `configs/`、`projects/`、`tools/`、`mmdet3d/` | 仓库四大目录，本讲第 4.4 节详解 |

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：**项目定位**、**任务与数据集版图**、**依赖关系**、**四大目录分工**。

### 4.1 项目定位：一个基于 PyTorch 的通用 3D 检测工具箱

#### 4.1.1 概念说明

MMDetection3D 把自己定义为「下一代通用 3D 检测平台」。通俗地说，它解决的问题是：

- 3D 检测领域论文多、代码分散（VoteNet、PointPillars、SECOND 各有各的仓库，数据格式、训练脚本互不兼容），复现和对比实验成本极高。
- MMDetection3D 用统一的**数据结构**（3D 框、点云容器）、统一的**配置系统**、统一的**训练/评估流程**，把这些算法收编进同一个框架，让你「换算法 = 换一个配置文件」。

一句话定位：**它是 OpenMMLab 生态中的 3D 检测工具箱，既可以当命令行工具用（训练/测试/推理现成模型），也可以当 Python 库用（在它之上开发自己的项目）。**

#### 4.1.2 核心流程

虽然本讲不深入代码，但先建立一个整体印象——用 MMDetection3D 做一次 3D 检测，数据流大致是：

```
原始数据集（KITTI / nuScenes / ScanNet ...）
      │  tools/create_data.py 预处理，生成 info pkl（后续第 3 单元细讲）
      ▼
Dataset + 数据管线（加载点云/图像 → 数据增强 → 打包）
      │  由配置文件描述
      ▼
检测器模型（backbone → neck → head，由配置文件组装）
      │  tools/train.py 或 tools/test.py 驱动
      ▼
3D 框预测结果 → 评估指标（mAP / NDS 等）或 可视化
```

支撑这条链路的「骨架」是三样东西：**配置系统（描述实验）**、**注册器（把配置里的 `type='xxx'` 路由到真实类）**、**统一数据结构（3D 框与点云）**。这三样会在后续单元逐一展开。

#### 4.1.3 源码精读

**① 项目的自我定位。** README 开头 Introduction 一句话说明它是什么、属于谁：

- [README.md:L64-L68](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/README.md#L64-L68)：说明 MMDetection3D 是基于 PyTorch 的开源 3D 检测工具箱、面向下一代通用 3D 检测平台，是 OpenMMLab 项目的一员，主分支要求 PyTorch 1.8+。

**② 四大主打特性。** README 用折叠块列出主要特性：

- [README.md:L75-L81](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/README.md#L75-L81)：开箱即用的多模态/单模态检测器（MVXNet、VoteNet、PointPillars 等），以及室内外数据集支持——ScanNet、SUN RGB-D、Waymo、nuScenes、Lyft、KITTI，nuScenes 还额外支持纯图像的 nuImages。
- [README.md:L83-L85](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/README.md#L83-L85)：与 2D 检测的「天然集成」——MMDetection 的 300+ 模型、40+ 篇论文方法可以直接在本库训练和使用。这是它区别于 OpenPCDet 等纯点云框架的关键设计：3D 项目直接复用 2D 生态。
- [README.md:L87-L97](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/README.md#L87-L97)：效率对比表，训练吞吐（样本/秒，越高越好）普遍高于 votenet、Det3D、OpenPCDet 等同类代码库。

**③ 「既是工具，也是库」。**

- [README.md:L101](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/README.md#L101)：明确说像 MMDetection 和 MMCV 一样，MMDetection3D 也可以作为库（library）支撑上层项目。仓库里 `projects/` 目录下的 BEVFusion、DETR3D 等就是这种用法的活例子（见 4.4 节）。

**④ 版本动态。**

- [README.md:L103-L132](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/README.md#L103-L132)：What's New。当前版本 v1.4.0（2024-08-01 发布）重点重构了 Waymo 数据集支持、新增 DSVT 训练与 NeRF-Det。读这一段可以快速了解项目的活跃方向。

#### 4.1.4 代码实践

**实践目标**：通过「读 README + 验证仓库」建立对项目规模的真实感受，而不是只看宣传语。

**操作步骤**：

1. 打开仓库根目录的 [README.md](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/README.md)，通读 Introduction 与 Major features 两个板块（约 L64-L99）。
2. 在本地克隆仓库后，进入仓库根目录，执行 `ls configs/`，数一数有多少个算法目录。
3. 再执行 `ls projects/`，看看哪些前沿算法不在 `configs/` 里而在这里。
4. 打开 [README.md:L232-L319](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/README.md#L232-L319) 的架构总表，随机挑 3 个算法名，验证它们在 `configs/` 或 `projects/` 下确实有同名目录。

**需要观察的现象**：README 架构表里 `configs/` 与 `projects/` 的链接是混排的——大部分经典算法（second、pointpillars、votenet……）指向 `configs/`，而 BEVFusion、DETR3D、PETR、TPVFormer 等较新方法指向 `projects/`。

**预期结果**：在当前 HEAD（fe25f7a5）下，`configs/` 包含 3dssd、centerpoint、fcos3d、pointpillars、second、votenet 等 30 余个算法目录，`projects/` 包含 BEVFusion、CENet、CenterFormer、DETR3D、DSVT、NeRF-Det、PETR、TPVFormer、TR3D、example_project 共 10 个目录。（本讲编写时已在本地核对。）

#### 4.1.5 小练习与答案

**练习 1**：MMDetection3D 和 OpenPCDet 这类代码库相比，设计上最大的差异是什么？

**参考答案**：MMDetection3D 不是纯点云框架，它与 2D 检测工具箱 MMDetection 深度集成，MMDetection 的 300+ 模型可直接在本库使用；同时它覆盖点云、图像、多模态三种输入以及 3D 分割任务，而 OpenPCDet 主要聚焦 LiDAR 点云检测。

**练习 2**：README 说主分支要求 PyTorch 1.8+，这句信息在哪个文件哪几行？项目当前发布版本号是多少？

**参考答案**：在 [README.md:L68](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/README.md#L68)。当前版本为 v1.4.0，发布于 2024-08-01（见 [README.md:L111-L116](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/README.md#L111-L116)）。

### 4.2 任务版图：四大任务与数据集

#### 4.2.1 概念说明

README 的架构总表把整个项目的模型能力切成四列，也就是本手册反复引用的「四大任务」：

| 任务 | 输入 | 输出 | 代表算法 |
| --- | --- | --- | --- |
| LiDAR 3D 检测 | 点云 | 3D 框 | PointPillars、SECOND、CenterPoint、VoteNet（室内） |
| 相机 3D 检测（Vision-based） | 单目/多目图像 | 3D 框 | FCOS3D、SMOKE、PGD、MonoFlex、DETR3D、PETR |
| 多模态 3D 检测 | 点云 + 图像 | 3D 框 | MVXNet、BEVFusion、ImVoteNet |
| 3D 语义分割 | 点云 | 逐点类别标签 | MinkUNet、Cylinder3D、SPVCNN、PointNet++ |

每个任务又分**室外**（自动驾驶：道路、车辆、行人，LiDAR 坐标系）和**室内**（RGB-D 扫描的房间、家具，深度坐标系）两类场景——这个「室内外之别」会一路影响数据集类、坐标系和模型结构的设计，是 3D 领域特有的维度划分。

数据集版图可以记忆为「室外四件套 + 室内四件套 + 分割三件套」：

- 室外检测：KITTI、nuScenes（含 nuImages）、Waymo（含 Waymo-mini）、Lyft
- 室内检测：ScanNet、SUN RGB-D
- 分割：SemanticKITTI（室外）、S3DIS（室内）、ScanNet

#### 4.2.2 核心流程

如何从 README/文档定位「某个算法在哪个数据集上有什么结果、权重在哪」：

```
README 架构总表（知道有哪些算法）
      ▼
docs/en/model_zoo.md（按算法逐节给出说明与 configs 链接）
      ▼
configs/<算法名>/README.md（该算法在各数据集上的指标表 + 权重下载链接）
      ▼
docs/en/advanced_guides/datasets/<数据集>.md（数据如何下载与预处理）
```

#### 4.2.3 源码精读

**① 架构总表的四列结构。**

- [README.md:L229-L247](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/README.md#L229-L247)：表头定义了四列——LiDAR-based 3D Object Detection、Camera-based 3D Object Detection、Multi-modal 3D Object Detection、3D Semantic Segmentation。这一行就是「四大任务」最权威的定义出处。
- [README.md:L248-L314](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/README.md#L248-L314)：四列的完整内容。注意每列内部再分 Outdoor / Indoor 两组，例如 LiDAR 列室外有 SECOND、PointPillars、CenterPoint、PV-RCNN 等，室内有 VoteNet、H3DNet、Group-Free-3D、FCAF3D 等。

**② 与 2D 生态的复用关系（骨架 × 组件矩阵）。**

- [README.md:L321-L354](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/README.md#L321-L354)：一张「检测器 × 骨干网络」支持矩阵，例如 PointPillars 可配 SECOND 或 RegNetX 骨干，FCOS3D 可配 ResNet 骨干。这张表直观体现了工具箱「组件可自由组合」的核心卖点。
- [README.md:L356](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/README.md#L356)：脚注强调 MMDetection 支持的约 500+ 模型、90+ 篇论文方法都能在本代码库训练/使用。

**③ 模型动物园文档。**

- [docs/en/model_zoo.md:L1-L9](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docs/en/model_zoo.md#L1-L9)：Common settings，说明指标测量口径（分布式训练、显存按 `torch.cuda.max_memory_allocated()` 统计、推理时间用 benchmark.py 跑 2000 次取平均）。
- [docs/en/model_zoo.md:L11-L25](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docs/en/model_zoo.md#L11-L25)：每个算法一节（SECOND、PointPillars、Part-A2、VoteNet……），并标注它在哪些数据集上提供了基线，例如 PointPillars 提供 KITTI、nuScenes、Lyft、Waymo 四套基线。

**④ 数据集文档索引。**

- [README.md:L160-L172](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/README.md#L160-L172)：Advanced Guides 的数据集索引，链接到 KITTI、NuScenes、Lyft、Waymo、SUN RGB-D、ScanNet、S3DIS、SemanticKITTI 八个数据集的详细文档，以及 LiDAR 检测 / 视觉检测 / LiDAR 分解三个任务专页。

#### 4.2.4 代码实践

**实践目标**：把 README 的「纸面版图」落到仓库的「真实目录」，做到看到任一算法名就能立刻找到它的配置目录。

**操作步骤**：

1. 仿照下面的表格模板，自己填一张「任务归类表」（示例代码，仅用于整理笔记）：

   | 算法 | 任务列 | 场景 | 配置目录 |
   | --- | --- | --- | --- |
   | PointPillars | LiDAR 检测 | 室外 | `configs/pointpillars/` |
   | VoteNet | LiDAR 检测 | 室内 | `configs/votenet/` |
   | FCOS3D | ？ | ？ | ？ |
   | BEVFusion | ？ | ？ | ？ |
   | Cylinder3D | ？ | ？ | ？ |

2. 对照 [README.md:L248-L314](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/README.md#L248-L314) 把问号补全。
3. 用 `ls configs/<目录名>/` 查看每个算法目录下有多少个配置文件，注意文件名里通常编码了数据集与超参（如 `pointpillars_hv_secfpn_8xb6-160e_kitti-3d-3class.py`）。

**需要观察的现象**：文件名不是随便起的，而是「算法_骨干_批次与轮数_数据集」的命名约定；同一算法目录下会有多个数据集变体。

**预期结果**：FCOS3D → 相机检测 / 室外 / `configs/fcos3d/`；BEVFusion → 多模态 / 室外 / `projects/BEVFusion/`；Cylinder3D → 3D 分割 / 室外 / `configs/cylinder3d/`。若与你的填写不一致，回到 README 表格核对。

#### 4.2.5 小练习与答案

**练习 1**：四大任务中，哪个任务「室内」目前只有 ImVoxelNet 一个条目？依据是什么？

**参考答案**：相机 3D 检测。依据 [README.md:L283-L286](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/README.md#L283-L286)，Camera-based 列的 Indoor 小节只列出 ImVoxelNet 一项。

**练习 2**：想知道 PointPillars 在 nuScenes 上的预训练权重与 mAP，应该按什么路径找？

**参考答案**：先看 [docs/en/model_zoo.md:L15-L17](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docs/en/model_zoo.md#L15-L17) 的 PointPillers 一节，它会指向 `configs/pointpillars/` 目录；该目录下的 README 有各数据集的指标表和权重下载链接。

**练习 3**：`data/` 目录（仓库根下）是干什么用的？

**参考答案**：它是存放各数据集及预处理产物的**软链接**约定位置，方便所有配置用统一的相对路径（如 `data/kitti/`）访问数据；数据本体不进 git 仓库。具体每种数据集的目录结构在 `docs/en/advanced_guides/datasets/` 各文档中说明。

### 4.3 依赖关系：MMEngine、MMCV、MMDetection 各扮演什么角色

#### 4.3.1 概念说明

MMDetection3D 不是从零造轮子，它站在三个库的肩膀上：

| 依赖库 | 角色 | 提供给 3D 项目的关键能力 |
| --- | --- | --- |
| **MMEngine** | 训练引擎 | `Runner`（训练/验证主循环）、`Config`（配置文件解析与继承）、`Registry`（注册器，配置到类的路由）、`BaseDataset`、Hook 体系、日志与可视化后端 |
| **MMCV** | 计算机视觉基础库 | 图像读写与颜色空间转换、图像级数据增强、卷积算子（CUDA ops）、视频/文件 IO 工具 |
| **MMDetection** | 2D 检测工具箱 | 2D 检测器、2D 骨干（ResNet 等）、FPN、RoIHead、2D 检测头与损失——被 3D 项目直接 import 复用 |

三者的关系可以画成分层栈：

```
┌─────────────────────────────────────────┐
│  MMDetection3D（3D 数据结构/模型/评估）  │
├────────────────┬────────────────────────┤
│  MMDetection   │  ← 复用其 2D 组件      │
├────────────────┴────────────────────────┤
│  MMCV（视觉基础 ops 与图像工具）        │
├─────────────────────────────────────────┤
│  MMEngine（Runner / Config / Registry）  │
├─────────────────────────────────────────┤
│  PyTorch                                │
└─────────────────────────────────────────┘
```

一个重要细节：版本约束是**左闭右开区间** `[minimum, maximum)`，即「大于等于下限、小于上限」。上限的存在是因为上游大版本升级可能引入不兼容 API，工具箱需要等适配后再放开。

#### 4.3.2 核心流程

`import mmdet3d` 时发生的版本检查流程（伪代码）：

```
import mmdet3d
  ├─ import mmcv / mmdet / mmengine        # 若任一未安装，这里直接 ImportError
  ├─ 读取三个库的 __version__，用 digit_version() 归一化为可比较的数字版本
  ├─ assert mmcv   版本 ∈ [2.0.0rc4, 2.2.0)
  ├─ assert mmengine 版本 ∈ [0.8.0, 1.0.0)
  └─ assert mmdet  版本 ∈ [3.0.0rc5, 3.4.0)
        └─ 任一断言失败 → 抛出 AssertionError，提示应安装的版本范围
```

`digit_version` 的作用是把 `'2.0.0rc4'` 这类带预发布后缀的字符串转成可比较的元组/数字，避免字符串比较出错。

#### 4.3.3 源码精读

**① 导入三大依赖并声明版本区间。**

- [mmdet3d/__init__.py:L2-L4](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/__init__.py#L2-L4)：`import mmcv`、`import mmdet`、`import mmengine`——注意这一步在 `import mmdet3d` 的瞬间就执行，所以任何一个依赖缺失或版本不符，都会在导入时报错，而不是等到训练时才爆。
- [mmdet3d/__init__.py:L9-L19](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/__init__.py#L9-L19)：定义三组版本常量：mmcv 要求 `>=2.0.0rc4, <2.2.0`，mmengine 要求 `>=0.8.0, <1.0.0`，mmdet 要求 `>=3.0.0rc5, <3.4.0`，并用 `digit_version()` 把当前安装版本归一化。

**② 三个版本断言。**

- [mmdet3d/__init__.py:L21-L30](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/__init__.py#L21-L30)：分别断言 mmcv 与 mmengine 的版本落在区间内，失败时的报错信息会直接告诉你要装什么范围。
- [mmdet3d/__init__.py:L32-L36](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/__init__.py#L32-L36)：对 mmdet（MMDetection）做同样的断言。

**③ OpenMMLab 官方对各库的一句话定位。**

- [README.md:L389-L395](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/README.md#L389-L395)：MMEngine 是「训练深度学习模型的基础库」，MMCV 是「计算机视觉基础库」，MMDetection 是「检测工具箱与基准」——与 4.3.1 的分层表互相印证。

#### 4.3.4 代码实践

**实践目标**：亲手复现 `mmdet3d/__init__.py` 的版本检查逻辑，理解「为什么装错版本会在 import 时就报错」。

**操作步骤**：

1. 在已安装 mmdet3d 的环境中运行（示例代码，请保存为独立脚本执行，不要修改仓库源码）：

   ```python
   # check_env.py（示例代码）
   import mmcv, mmengine, mmdet
   from mmengine.utils import digit_version

   bounds = {
       'mmcv':    (mmcv.__version__,    '2.0.0rc4', '2.2.0'),
       'mmengine': (mmengine.__version__, '0.8.0',  '1.0.0'),
       'mmdet':   (mmdet.__version__,   '3.0.0rc5', '3.4.0'),
   }
   for name, (cur, lo, hi) in bounds.items():
       ok = digit_version(lo) <= digit_version(cur) < digit_version(hi)
       print(f'{name:9s} installed={cur:10s} required=[{lo}, {hi}) -> {"OK" if ok else "MISMATCH"}')
   ```

2. 运行 `python check_env.py`，观察三行输出。
3. 再运行 `python -c "import mmdet3d; print(mmdet3d.__version__)"`，确认能正常导入并打印版本号。

**需要观察的现象**：脚本输出的 OK/MISMATCH 与 `import mmdet3d` 是否报 AssertionError 应当一致；若某依赖 MISMATCH，`import mmdet3d` 会在这条断言处中断，报错信息与本讲引用的 [mmdet3d/__init__.py:L21-L36](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/__init__.py#L21-L36) 的提示文案一致。

**预期结果**：在依赖满足官方版本要求的环境中，三条均为 OK，且 `mmdet3d.__version__` 打印 `1.4.0`（与 [README.md:L111](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/README.md#L111) 的 v1.4.0 对应）。若你本地未装环境，本步骤「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：如果本机安装了 mmcv 2.2.0，`import mmdet3d` 会发生什么？为什么 2.2.0 明明「更新」却被拒绝？

**参考答案**：会触发 [mmdet3d/__init__.py:L21-L24](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/__init__.py#L21-L24) 的断言失败，抛出 AssertionError 并提示安装 `mmcv>=2.0.0rc4, <2.2.0`。因为版本约束是左闭右开区间，上游 2.2.0 可能包含未适配的 API 变更，工具箱在上限版本验证兼容之前先拒绝导入，这是防患于未然的保护措施。

**练习 2**：MMDetection3D 为什么必须依赖 MMDetection，而不只是「可选参考」？

**参考答案**：因为 3D 项目把 2D 检测组件当作一等公民直接复用——图像分支的骨干网络、FPN、RoIHead、2D 检测头等都来自 mmdet（这也解释了 [mmdet3d/__init__.py:L32-L36](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/__init__.py#L32-L36) 要对 mmdet 版本做严格断言）。README 也强调 mmdet 的 500+ 模型可直接在本库训练使用。

### 4.4 四大目录分工：configs / projects / tools / mmdet3d

#### 4.4.1 概念说明

整个仓库可以按「职责」切成四大目录（外加 `demo/`、`docs/`、`tests/`、`requirements/` 等辅助目录）：

| 目录 | 职责 | 一句话理解 |
| --- | --- | --- |
| `mmdet3d/` | 库本体：所有 Python 实现 | 代码住在这里 |
| `configs/` | 实验描述：用配置文件组装库里的组件 | 「菜谱」住在这里 |
| `tools/` | 入口脚本：训练、测试、数据准备、分析 | 命令行入口住在这里 |
| `projects/` | 前沿/社区项目的「自留地」 | 不污染主库的新算法先在这里孵化 |

这种「实现与配置分离」是 OpenMMLab 2.0 架构的核心风格：同一个类（如 `PointPillars`）可以通过不同配置组合出几十种实验，无需复制代码。

#### 4.4.2 核心流程

一次典型训练中四大目录的协作关系：

```
你写下/选择一个配置: configs/pointpillars/xxx_kitti-3d-3class.py
        │
        ▼
执行入口脚本: tools/train.py <config>
        │  解析配置 → 通过 MMEngine Registry 定位类
        ▼
从 mmdet3d/ 包中实例化: dataset / model / optimizer / hooks
        │
        ▼
Runner（来自 MMEngine）驱动训练循环，结果写入 work_dir/
```

#### 4.4.3 源码精读

**① `mmdet3d/`——库本体的内部结构**（当前 HEAD 下的真实子目录/文件）：

- `structures/`：3D 数据结构（3D 框、点云、`Det3DDataSample`）——第 2 单元主角。
- `datasets/`：数据集类与数据管线 transforms——第 3 单元主角。
- `models/`：检测器、骨干、颈部、检测头、损失等——第 4~6 单元主角。
- `engine/`：Hook 等训练期组件。
- `evaluation/`：各数据集的评估指标实现。
- `visualization/`：3D 可视化器。
- `apis/`：高层推理接口（`inference_detector`、Inferencer）。
- [mmdet3d/registry.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/registry.py)：全库的注册器节点定义，是「配置 → 类」路由的枢纽。
- `testing/`、`utils/`、`configs/`、`version.py`：测试辅助、通用工具、纯 Python 风格配置入口与版本信息。

**② `tools/`——命令行入口**（真实文件清单）：

- [tools/train.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/train.py) / [tools/test.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/test.py)：训练与测试的统一入口，内部把配置交给 MMEngine 的 Runner。
- [tools/create_data.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/create_data.py)：把 KITTI/nuScenes/ScanNet 等原始数据集转成统一 info pkl。
- `dataset_converters/`：create_data 调用的各数据集转换器。
- `analysis_tools/`（analyze_logs、benchmark、get_flops 等）、`misc/`（browse_dataset、print_config 等）、`model_converters/`、`deployment/`：分析、浏览、模型转换与部署辅助。
- `dist_train.sh`、`dist_test.sh`、`slurm_train.sh`、`slurm_test.sh`：分布式/集群启动脚本。

**③ `configs/`——实验描述**：顶层按算法名分目录（second、pointpillars、centerpoint、votenet、fcos3d、cylinder3d……共 30 余个），公共部分放在 `configs/_base_/` 下（models / datasets / schedules / runtime 四类基座配置，通过继承被具体实验复用）。这套继承机制是第 5 讲（u1-l5 配置系统）的主题。

**④ `projects/`——前沿项目孵化区**（真实清单：BEVFusion、CENet、CenterFormer、DETR3D、DSVT、NeRF-Det、PETR、TPVFormer、TR3D、example_project）：这些项目自带代码与配置，通过 `custom_imports` 机制借用主库的注册器和数据管线，成熟后部分会被收编进主库。`example_project/` 是官方提供的「如何做二次开发」模板，第 8 单元会用到。

#### 4.4.4 代码实践

**实践目标**：亲手绘制一张「仓库地图」，把四大目录与其内部关键子目录的职责固化成自己的笔记（后续每一讲都会用到这张图）。

**操作步骤**：

1. 在仓库根目录依次执行 `ls mmdet3d/`、`ls tools/`、`ls configs/`、`ls projects/`。
2. 对照上文的职责表，检查是否每个子目录你都能说出一句话职责。
3. 用任意画图/文本工具画出这样一张依赖示意图（示例代码，仅表示方向）：

   ```
   configs/ ──描述──▶ mmdet3d/（通过 registry 实例化）
   tools/   ──驱动──▶ configs/ + mmdet3d/（train.py/test.py）
   projects/ ──借用──▶ mmdet3d/ 的注册器与数据管线
   ```

4. 进阶：找到 `configs/_base_/` 目录，列出它下面的子目录名，猜猜每个子目录放什么（答案在第 5 讲揭晓）。

**需要观察的现象**：`mmdet3d/` 的子目录命名与四大任务高度对应（`models/` 里既有检测器也有 segmentors）；`tools/` 里几乎每个脚本都能独立用 `--help` 查看用法。

**预期结果**：`configs/_base_/` 下有 models、datasets、schedules（以及 runtime 等基座配置文件）；你能不看笔记说出「改模型结构去 `configs/_base_/models/` 找、改数据管线去 `mmdet3d/datasets/transforms/` 找、想跑训练用 `tools/train.py`」。

#### 4.4.5 小练习与答案

**练习 1**：`configs/` 与 `mmdet3d/` 是什么关系？为什么同一个 `PointPillars` 类能对应几十个配置文件？

**参考答案**：`mmdet3d/` 提供**实现**（类与函数），`configs/` 提供**描述**（用字典/列表声明用哪些类、传什么参数）。配置通过 `type='PointPillars'` 这样的字段由注册器路由到真实类（见 [mmdet3d/registry.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/registry.py)），再组合不同的骨干、头部、数据集与训练日程，就得到几十个实验变体。

**练习 2**：你想给 BEVFusion 换个骨干网络，应该去哪个目录找它的模型定义？为什么不在 `mmdet3d/models/` 里？

**参考答案**：去 `projects/BEVFusion/`。BEVFusion 属于 `projects/` 生态（见 4.4.3 第 ④ 点），自带模型实现，只复用主库的注册器与数据管线，所以它的代码不在 `mmdet3d/models/` 里。

**练习 3**：`tools/create_data.py` 处在 4.4.2 流程图的哪个位置？

**参考答案**：在流程图最上游之外——训练开始**之前**的数据准备阶段：它把原始数据集转成 info pkl，`configs/` 里的 dataset 配置再引用这些 pkl，之后的训练循环才走「configs → tools/train.py → mmdet3d」这条链。

## 5. 综合实践

**任务：制作一份属于你的《MMDetection3D 全景一页纸》**，把本讲四个模块的输出合并成一张可长期查阅的笔记，包含四块内容：

1. **依赖栈图**：手绘 MMEngine / MMCV / MMDetection / MMDetection3D / PyTorch 五层堆叠图，并在每层标注一句话职责；旁边抄录 [mmdet3d/__init__.py:L9-L19](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/__init__.py#L9-L19) 的三组版本区间。
2. **任务×数据集矩阵**：自己画一张 4 行（四大任务）× 若干列（数据集）的表格，按「室内/室外」分组填入数据集名，并各选一个代表算法及其配置目录（依据 [README.md:L229-L314](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/README.md#L229-L314) 的架构总表与 `ls configs/` 的结果核对）。
3. **目录地图**：把 4.4.4 实践画的四大目录依赖图誊抄进来，并给 `mmdet3d/` 的每个子目录标注「对应本手册第几单元」。
4. **环境自检脚本**：保存 4.3.4 的 `check_env.py`（示例代码），在笔记里记录一次运行输出。

**检验标准**：合上笔记，你能只凭记忆回答——「FCOS3D 是什么任务？用什么输入？」「mmcv 版本超上限会怎样？」「想跑训练该敲哪个命令、它去哪里找模型定义？」三问全部答对，本讲即过关。

## 6. 本讲小结

- MMDetection3D 是 OpenMMLab 生态中基于 PyTorch 的通用 3D 检测工具箱，既是命令行工具也是 Python 库，主分支要求 PyTorch 1.8+，当前版本 v1.4.0。
- 它支持四大任务：LiDAR 3D 检测、相机 3D 检测、多模态 3D 检测、3D 语义分割；每类任务再分室内/室外两种场景。
- 数据集版图：室外 KITTI/nuScenes/Waymo/Lyft，室内 ScanNet/SUN RGB-D，分割 SemanticKITTI/S3DIS，共八个主力数据集。
- 它站在 MMEngine（训练引擎）、MMCV（视觉基础库）、MMDetection（2D 检测组件）三层之上，版本约束在 `mmdet3d/__init__.py` 里以左闭右开区间断言强制执行。
- 仓库四大目录分工：`mmdet3d/` 是实现、`configs/` 是实验描述、`tools/` 是命令行入口、`projects/` 是前沿项目孵化区。
- 「配置驱动 + 注册器路由」是贯穿全库的骨架，也是后续所有讲义反复出现的模式。

## 7. 下一步学习建议

下一讲（u1-l2《环境安装与版本验证》）将动手搭建运行环境：按依赖顺序安装 mmengine → mmcv → mmdet → mmdet3d，并验证本讲的版本约束在真实安装中如何生效。

在进入下一讲之前，建议先做两件轻量阅读：

1. 浏览 [README.md:L142-L178](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/README.md#L142-L178) 的 User Guides / Advanced Guides 索引，知道官方文档的整体章节，遇到问题时知道去哪一节找答案。
2. 打开 `demo/` 目录看一眼有哪些 demo 脚本（如 `pcd_demo.py`、`mono_det_demo.py`），它们是第 3 讲「跑通第一个推理 Demo」的主角。
