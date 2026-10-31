# u1-l4 仓库目录结构与源码地图

## 1. 本讲目标

学完本讲，你应该能够：

1. 画出 `mmdet3d/` 包内 `datasets / structures / models / engine / evaluation / visualization` 六大子模块的依赖关系图，并说出每个子模块的职责。
2. 说出 `tools/` 目录下 `train.py`、`test.py`、`create_data.py`、`analysis_tools/`、`misc/` 各脚本的用途。
3. 明白 `configs/_base_/` 的「四件套」组织规律，以及 `configs/<算法名>/` 下配置文件命名（如 `pointpillars_hv_secfpn_8xb6-160e_kitti-3d-3class.py`）每一节的含义。
4. 掌握「从 config 里的 `type='xxx'` 反查源码文件」这一贯穿整本手册的源码定位方法。

本讲是一张「地图」：后续每一讲都会在这张地图上标注位置。现在多花十分钟理解目录分工，以后读任何模块都能立刻知道「该打开哪个文件」。

## 2. 前置知识

- **Python 包（package）**：一个含 `__init__.py` 的文件夹，可以通过 `import mmdet3d.datasets` 这样的语法导入。仓库里的 `mmdet3d/` 就是主包，`mmdet3d/models/` 是它的子包。
- **配置驱动 + 注册器路由**（u1-l1 已介绍）：MMDetection3D 中所有实验都由一个 Python 配置文件描述，配置里的 `dict(type='VoxelNet')` 会被「注册器」翻译成真正的类。本讲会看到 17 个注册节点分别「管辖」哪些目录。
- **依赖方向**：读大型仓库时，先弄清「谁 import 谁」。被依赖最多的模块在最底层（如 `structures`），依赖别人的模块在最顶层（如 `apis`）。顺着依赖方向读代码，就不会迷路。
- **Runner**：MMEngine 提供的训练/测试总调度器。`tools/train.py` 和 `tools/test.py` 本身几乎不含算法逻辑，它们只负责「读配置 → 构建 Runner → 调用 `runner.train()` 或 `runner.test()`」。

## 3. 本讲源码地图

| 文件 / 目录 | 作用 |
| --- | --- |
| `mmdet3d/registry.py` | 定义全库 17 个注册节点，声明「每个注册节点自动扫描哪些目录」 |
| `mmdet3d/__init__.py` | 包入口，只做版本断言（u1-l2 详讲过） |
| `mmdet3d/structures/` | 底层数据结构：3D 框、点云、Det3DDataSample |
| `mmdet3d/datasets/` | 数据集类与数据管线 transforms |
| `mmdet3d/models/` | 全部模型组件：检测器、骨干网络、头部、损失等 |
| `mmdet3d/engine/` | 挂在 Runner 上的 Hook |
| `mmdet3d/evaluation/` | 各数据集的评估指标 |
| `mmdet3d/visualization/` | 3D 框与点云可视化 |
| `tools/train.py` | 训练命令行入口 |
| `tools/test.py` | 测试 / 评估命令行入口 |
| `tools/create_data.py` | 把原始数据集转成 info pkl |
| `configs/_base_/default_runtime.py` | 运行时基础配置（hooks、日志、环境） |
| `configs/_base_/models/pointpillars_hv_secfpn_kitti.py` | PointPillars 模型基配置 |
| `configs/pointpillars/pointpillars_hv_secfpn_8xb6-160e_kitti-3d-3class.py` | 一个完整的 PointPillars 实验配置 |
| `mmdet3d/models/detectors/voxelnet.py` | VoxelNet 检测器（综合实践的主角） |

## 4. 核心概念与源码讲解

### 4.1 仓库顶层全景：九大目录各管什么

#### 4.1.1 概念说明

MMDetection3D 的仓库根目录可以分成三组：

- **「实现」**：`mmdet3d/` 是真正被 `pip install` 后 `import` 的 Python 包；`projects/` 是前沿项目孵化区（BEVFusion、DETR3D、PETR 等），它们像插件一样借用主库的注册器。
- **「描述」**：`configs/` 用 Python 文件描述每一个实验（用什么模型、什么数据、训练多久）；`demo/` 是最小使用示例。
- **「入口与文档」**：`tools/` 是命令行入口（训练、测试、数据转换、分析）；`docs/` 是文档；`tests/` 是单元测试；`requirements/`、`setup.py`、`docker/` 负责构建安装。

#### 4.1.2 核心流程

仓库顶层目录树（只列关键项）：

```text
mmdetection3d/
├── mmdet3d/            # 核心 Python 包：所有算法实现
│   ├── apis/           #   高层推理封装（inference / inferencers）
│   ├── datasets/       #   数据集类 + 数据管线 transforms
│   ├── engine/         #   Hook（挂接训练循环）
│   ├── evaluation/     #   评估指标（NuScenesMetric、KittiMetric…）
│   ├── models/         #   模型组件（检测器、backbone、head、loss…）
│   ├── structures/     #   底层数据结构（3D 框、点云、Det3DDataSample）
│   ├── visualization/  #   可视化（Det3DLocalVisualizer）
│   ├── registry.py     #   17 个注册节点（全库路由中枢）
│   ├── testing/        #   测试用的伪数据工具
│   └── utils/          #   通用工具（类型别名、环境收集等）
├── configs/            # 实验描述：_base_/ + 各算法目录
├── tools/              # 命令行入口：train/test/create_data/分析工具
├── projects/           # 前沿项目：BEVFusion、DETR3D、PETR、TPVFormer…
├── demo/               # 推理示例脚本与示例数据
├── docs/               # 文档源文件
├── tests/              # pytest 单元测试
├── requirements/       # 分级依赖（runtime/train/optional…）
└── setup.py            # 安装脚本
```

一条数据在仓库中的完整旅程（记住这条主线，本手册其余各讲都在展开它的某一段）：

```text
原始数据集(KITTI/nuScenes/ScanNet)
   │  tools/create_data.py + tools/dataset_converters/
   ▼
info pkl + GT 数据库（data/ 目录）
   │  mmdet3d/datasets/（数据集类 + transforms 管线）
   ▼
Det3DDataSample（由 mmdet3d/structures/ 定义）
   │  mmdet3d/models/（data_preprocessor → 检测器 → head → loss）
   ▼
预测结果（仍是 Det3DDataSample）
   │  mmdet3d/evaluation/（算指标） + mmdet3d/visualization/（画图）
   ▼
mmdet3d/apis/（把以上流程打包成一行的推理接口）
```

#### 4.1.3 源码精读

包入口 [mmdet3d/__init__.py:L9-L36](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/__init__.py#L9-L36) 里没有一行算法代码，只做三件事：导入 `mmcv / mmdet / mmengine`、比较版本号、断言在兼容区间内。这印证了 u1-l2 的结论——mmdet3d 是站在三个库肩膀上的「上层应用」。

安装脚本 `setup.py` 在打包时会把 `configs` 和 `tools` 软链接进 `.mim` 目录（u1-l2 已讲），所以即使 `pip install` 安装了 mmdet3d，这两个目录也能通过 MIM 找到——它们和 `mmdet3d/` 包同等重要。

#### 4.1.4 代码实践

**实践目标**：用 5 分钟把根目录「逛」一遍，验证上面的目录树。

**操作步骤**：

1. 在仓库根目录执行 `ls`，对照 4.1.2 的目录树逐个确认。
2. 执行 `ls projects/`，数一数有多少个独立项目（应有 10 个，含 `example_project`）。
3. 执行 `ls demo/`，找到 u1-l3 用过的 `pcd_demo.py` 和 `data/` 目录。

**需要观察的现象**：根目录的顶层文件（README、LICENSE、setup.py、model-index.yml、dataset-index.yml 等）都很小，真正的代码量集中在 `mmdet3d/`、`configs/`、`tools/`、`projects/` 四处。

**预期结果**：`projects/` 下能看到 BEVFusion、CENet、CenterFormer、DETR3D、DSVT、NeRF-Det、PETR、TPVFormer、TR3D、example_project 共 10 项；`demo/` 下有 `pcd_demo.py`、`mono_det_demo.py`、`multi_modality_demo.py`、`pcd_seg_demo.py` 与 `inference_demo.ipynb`。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `projects/` 里的 BEVFusion 不合并进 `mmdet3d/models/`？

**参考答案**：`projects/` 是前沿算法的孵化区，允许依赖非主库的额外环境（如 BEVFusion 依赖自己的融合算子）、独立演进、单独维护 README 与权重；成熟稳定的基础模块才进主库。它们通过 `mmdet3d/registry.py` 的注册节点接入主库生态（u8-l4 详解）。

**练习 2**：想找「nuScenes 评估指标怎么算」，应该去哪个目录？

**参考答案**：`mmdet3d/evaluation/metrics/nuscenes_metric.py`。评估相关实现都在 `mmdet3d/evaluation/` 下（`metrics/` 放指标类，`functional/` 放指标函数）。

### 4.2 mmdet3d 包：六大子模块与 registry.py 路由中枢

#### 4.2.1 概念说明

`mmdet3d/` 包内最重要的六个子模块按「依赖深度」分层：

| 子模块 | 职责 | 依赖谁 |
| --- | --- | --- |
| `structures/` | 定义 3D 框（BaseInstance3DBoxes）、点云（BasePoints）、Det3DDataSample | 几乎不依赖其他子模块（最底层） |
| `datasets/` | 数据集类（Det3DDataset 及各数据集子类）+ transforms 数据管线 | `structures`（管线产出点云/框/样本） |
| `models/` | 检测器、backbone、neck、dense_head、loss、task_modules 等全部模型组件 | `structures`（输入输出都是它） |
| `evaluation/` | 各数据集 metric 与 evaluator | `structures`（比较 gt 与 pred） |
| `visualization/` | Det3DLocalVisualizer，画 3D 框/点云投影 | `structures` |
| `engine/` | Hook：CheckpointHook 之外 3D 特有的钩子（可视化、benchmark 等） | `visualization`、间接使用 `datasets`/`models` |

另有三个辅助模块：`apis/`（顶层推理封装）、`testing/`（构造伪数据的工具）、`utils/`（类型别名等）。

**关键点：依赖关系是「星形」的**——`structures` 在中心被大家依赖；`registry.py` 则是「路由中枢」，六个子模块都要 `from mmdet3d.registry import MODELS`（或 TRANSFORMS 等）来注册自己的类。理解了这两点，就理解了全库的骨架。

#### 4.2.2 核心流程

六大子模块 + registry 的关系图（箭头表示 import / 使用方向）：

```text
                    ┌───────────────────────────┐
                    │   mmdet3d/registry.py     │
                    │  （17 个注册节点，路由中枢） │
                    └──────┬────────────────────┘
                           │ 被所有子模块 import 用于注册
        ┌──────────────────┼──────────────────┐
        ▼                  ▼                  ▼
   datasets/ ──产出──> structures/ <──消费── models/
        │              （3D 框/点云/            │
        │               Det3DDataSample）       │
        │                   ▲            │     │
        └──(gt 样本)─────────┘            ▼     ▼
                              evaluation/  visualization/
                                   ▲            ▲
                                   └── engine/hooks 横切训练循环 ──┘

  顶层封装：apis/（inference_detector、Inferencer）串起 datasets+models+visualization
```

一条训练样本的类型流转（数据结构视角）：

```text
numpy 点云 ──LoadPointsFromFile──> LiDARPoints（structures/points/）
KITTI 标注 ──LoadAnnotations3D──> LiDARInstance3DBoxes（structures/bbox_3d/）
两者打包 ──Pack3DDetInputs──> Det3DDataSample（structures/det3d_data_sample.py）
     ──> 进入 models/ 前向 ──> 预测仍是 Det3DDataSample ──> evaluation/ 算指标
```

#### 4.2.3 源码精读

`registry.py` 开头的注释直接说明了它的设计——每个注册节点都是 MMEngine 根注册器的子节点：

[mmdet3d/registry.py:L1-L7](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/registry.py#L1-L7) 声明「MMDetection3D 提供 17 个注册节点，每个节点都是 MMEngine 根注册器的孩子」。

四个最常用的节点，注意每个节点的 `locations` 参数正好指向它管辖的目录：

- [mmdet3d/registry.py:L58-L59](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/registry.py#L58-L59)：`DATASETS` 注册节点，`parent` 挂在 MMENGINE_DATASETS 上，`locations=['mmdet3d.datasets']` 表示按名字找不到模块时会自动去 `mmdet3d/datasets/` 里 import。
- [mmdet3d/registry.py:L65-L68](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/registry.py#L65-L68)：`TRANSFORMS` 节点，locations 指向 `mmdet3d.datasets.transforms`，即数据管线目录。
- [mmdet3d/registry.py:L71-L72](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/registry.py#L71-L72)：`MODELS` 节点，管辖整个 `mmdet3d.models`——backbone、head、loss 全部注册在这里。
- [mmdet3d/registry.py:L109-L113](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/registry.py#L109-L113)：`METRICS` 与 `EVALUATOR` 节点，指向 `mmdet3d.evaluation`。

以及两个「跨目录」的例子：[mmdet3d/registry.py:L120-L123](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/registry.py#L120-L123) 的 `VISUALIZERS` 指向 `mmdet3d.visualization`；[mmdet3d/registry.py:L115-L117](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/registry.py#L115-L117) 的 `TASK_UTILS`（管理 anchor 生成器、bbox coder 等任务模块）则与 MODELS 共享 `mmdet3d.models` 这个 locations。

**读法总结**：`locations` 就是「注册节点 → 源码目录」的官方对照表。看到 config 里 `type='XXX'`，先想它属于哪类节点（模型？transform？metric？），再到对应目录找文件。反过来，`grep -rn "class XXX(" mmdet3d/` 可以一步定位（4.4.4 实践会用到）。

#### 4.2.4 代码实践

**实践目标**：验证「`MODELS` 注册节点真的能列出 models/ 目录下的所有类」。

**操作步骤**：

1. 确认已完成 u1-l2 的安装（能 `import mmdet3d`）。
2. 在仓库根目录运行（以下为示例代码，非项目自带脚本）：

```python
# 示例代码：列出 MODELS 注册表中的部分模块名
import mmdet3d  # 触发各子包 __init__ 的批量导入，从而完成注册
from mmdet3d.registry import MODELS

names = sorted(MODELS.module_dict.keys())
print('MODELS 注册总数:', len(names))
print([n for n in names if n in
       ('VoxelNet', 'PointPillarsScatter', 'SECOND', 'Anchor3DHead')])
```

3. 把 `MODELS` 换成 `TRANSFORMS`、`METRICS` 再各跑一次。

**需要观察的现象**：`import mmdet3d` 之后注册表里已经有上百个名字；换不同注册节点，名字集合不同（TRANSFORMS 里是 `LoadPointsFromFile` 等，METRICS 里是 `NuScenesMetric` 等）。

**预期结果**：`MODELS 注册总数` 为一个三位数（当前版本约 200+），且列表包含 `VoxelNet`、`PointPillarsScatter`、`SECOND`、`Anchor3DHead`。若环境未装好会报 `ModuleNotFoundError`，回到 u1-l2 排查。具体注册总数随版本变化，待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`structures/` 为什么几乎不依赖其他子模块？

**参考答案**：它定义的是最基础的「数据契约」（3D 框、点云、Det3DDataSample），datasets 产它、models 吃它、evaluation 比它、visualization 画它。如果它反过来依赖上层模块，会形成循环依赖。所以它必须保持最底层、最纯净。

**练习 2**：config 里写了 `type='NuScenesMetric'`，它是通过哪个注册节点、在哪个目录被找到的？

**参考答案**：`METRICS` 节点，其 `locations=['mmdet3d.evaluation']`（[mmdet3d/registry.py:L109-L110](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/registry.py#L109-L110)），类实现在 `mmdet3d/evaluation/metrics/nuscenes_metric.py`。

**练习 3**：`engine/` 目录下只有 `hooks/` 一个子目录，为什么它还能影响训练过程？

**参考答案**：Hook 是 MMEngine Runner 提供的「横切机制」，在训练循环的固定时机（每个 iter 前/后、每个 epoch 前/后）被回调。`mmdet3d/engine/hooks/` 里的 `Det3DVisualizationHook`、`BenchmarkHook`、`DisableObjectSampleHook` 就是这样挂进训练流程的，它们不需要改动 Runner 本身。

### 4.3 tools/ 目录：训练、测试、数据三大命令行入口

#### 4.3.1 概念说明

`tools/` 是「命令行世界」的大门。按用途分五组：

| 路径 | 用途 |
| --- | --- |
| `train.py` / `test.py` | 训练 / 测试评估的 Python 入口 |
| `dist_train.sh`、`dist_test.sh`、`slurm_train.sh`、`slurm_test.sh` | 多卡分布式 / SLURM 集群启动脚本（内部转调 train.py/test.py） |
| `create_data.py` | 数据集转换总入口（生成 info pkl 与 GT 数据库） |
| `dataset_converters/` | 各数据集的具体转换器（kitti、nuscenes、scannet、waymo…） |
| `analysis_tools/`（analyze_logs、benchmark、get_flops）、`misc/`（browse_dataset、print_config、visualize_results、fuse_conv_bn） | 分析与调试 |
| `model_converters/`、`deployment/`、`update_data_coords.py` | 权重转换、TorchServe 部署、旧数据坐标更新 |

#### 4.3.2 核心流程

`train.py` 的主流程只有五步（这也是 MMEngine 系工具的通用套路）：

```text
parse_args() 解析命令行
   → Config.fromfile(config) 读入配置
   → 用 CLI 参数覆盖 cfg（work_dir / amp / sync_bn / resume / cfg-options）
   → Runner.from_cfg(cfg) 构建总调度器（内部按 cfg 构建 dataloader/model/optimizer…）
   → runner.train() 启动训练
```

`test.py` 与之几乎对称，只是多了可视化开关处理，最后调 `runner.test()`。

#### 4.3.3 源码精读

- [tools/train.py:L73](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/train.py#L73)：`cfg = Config.fromfile(args.config)` —— 一切从读配置开始；[tools/train.py:L80-L81](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/train.py#L80-L81) 把 `--cfg-options` 的键值对合并进配置，实现「不改文件改实验」。
- [tools/train.py:L83-L90](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/train.py#L83-L90)：work_dir 的优先级逻辑——命令行 > 配置文件内 > 默认用配置文件名（存到 `./work_dirs/` 下）。
- [tools/train.py:L131-L141](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/train.py#L131-L141)：构建 Runner 并启动训练。没有 `runner_type` 时用默认 `Runner.from_cfg(cfg)`，否则从 `RUNNERS` 注册节点构建自定义 Runner。**注意 `train.py` 里没有任何模型或数据集代码**——它们都由 cfg 驱动、由注册器路由。
- [tools/test.py:L122](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/test.py#L122)：`cfg.load_from = args.checkpoint`，把命令行传入的 checkpoint 塞进配置。
- [tools/test.py:L70-L96](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/test.py#L70-L96)：`trigger_visualization_hook`——当命令行带 `--show/--show-dir` 时，改写配置里的 `default_hooks.visualization`，打开 `draw` 并要求 `--task` 指定五种任务之一（mono_det / multi-view_det / lidar_det / lidar_seg / multi-modality_det）。这正是 4.2 里「engine/hooks 横切训练循环」在命令行上的入口。
- [tools/test.py:L135-L145](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/test.py#L135-L145)：与 train.py 完全同构的「build Runner → `runner.test()`」。

数据入口 `tools/create_data.py` 是一个「转发器」：它把 `--dataset-name` 映射到 `dataset_converters/` 里的具体转换函数（kitti_converter、nuscenes_converter、indoor_converter 等），u3-l2 会精读。

#### 4.3.4 代码实践

**实践目标**：不看正文，自己从源码找出 `--task` 参数有哪几个合法取值。

**操作步骤**：

1. 打开 [tools/test.py:L33-L41](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/test.py#L33-L41)。
2. 读 `--task` 参数的 `choices` 列表。
3. 再运行 `python tools/test.py --help`（不需要真实 checkpoint，argparse 会先打印帮助并退出）。

**需要观察的现象**：`--help` 输出的 `--task` 一行与源码 choices 完全一致；同时还能看到 `--tta`、`--score-thr` 等参数说明。

**预期结果**：合法取值为 `mono_det, multi-view_det, lidar_det, lidar_seg, multi-modality_det` 五种，正好对应 MMDetection3D 支持的任务类型（u1-l1 的四大任务加上多视角细分）。`--help` 命令本身待本地验证（需要环境已装好 mmengine）。

#### 4.3.5 小练习与答案

**练习 1**：想在 8 张 GPU 上训练，应该敲哪条命令？

**参考答案**：`bash tools/dist_train.sh <config> 8`。`dist_train.sh` 内部用 `torch.distributed.launch`（或 torchrun）以不同 rank 多次调用 `tools/train.py`，并自动设置 `--launcher pytorch`。

**练习 2**：`tools/train.py` 里找不到 `train_loop`、`optimizer` 的实现代码，它们在哪？

**参考答案**：在 MMEngine 的 `Runner` 里。train.py 只负责「配置加工 + 交给 Runner」；optimizer、dataloader、loop 都由 `Runner.from_cfg(cfg)` 按 cfg 中的 `optim_wrapper`、`train_dataloader`、`train_cfg` 等键构建（u7-l1 详解）。

### 4.4 configs/ 的组织规律：_base_ 四件套与算法目录

#### 4.4.1 概念说明

`configs/` 采用「继承 + 覆盖」的组织方式，顶层先分两类：

- **`configs/_base_/`**：可复用的基础配置，分四类（俗称「四件套」）：
  - `models/`：模型结构（30 余个，如 `pointpillars_hv_secfpn_kitti.py`、`votenet.py`、`fcos3d.py`）
  - `datasets/`：数据集与数据管线（`kitti-3d-3class.py`、`nus-3d.py`、`scannet-3d.py`、`semantickitti.py`…）
  - `schedules/`：训练日程（优化器、学习率策略、epoch 数，如 `cyclic-40e.py`、`schedule-2x.py`）
  - `default_runtime.py`：运行时环境（hooks、日志、断点续训）
- **`configs/<算法名>/`**：每个算法一个目录（`pointpillars/`、`second/`、`centerpoint/`、`votenet/`…共 30 余个），目录内是「具体实验」，文件名通常遵循 `{模型}_{缩写}_{-}xbs{N}-{epochs}e_{数据集}` 的命名法。

以 `pointpillars_hv_secfpn_8xb6-160e_kitti-3d-3class.py` 为例拆解命名：`pointpillars` 算法 / `hv` 表示 horizonal-vertical 体素（柱体）/ `secfpn` 骨干后的 FPN 变体 / `8xb6` 8 卡每卡 batch 6 / `160e` 训练 160 epoch / `kitti-3d-3class` 数据集与类别数。

#### 4.4.2 核心流程

一个完整实验配置 = 继承四件套 + 局部覆盖：

```text
configs/pointpillars/pointpillars_hv_secfpn_8xb6-160e_kitti-3d-3class.py
  _base_ = [ models/pointpillars_hv_secfpn_kitti.py,   # 用什么模型
             datasets/kitti-3d-3class.py,               # 用什么数据与管线
             schedules/cyclic-40e.py,                   # 怎么训
             default_runtime.py ]                       # 在什么环境里跑
  + 覆盖 db_sampler / train_pipeline / test_pipeline / optim_wrapper / train_cfg…
```

MMEngine 的 Config 会递归读入 `_base_` 再做字典深度合并：子文件里出现过的键覆盖父文件，没出现的键原样继承。所以「一个实验 = 四个基础文件的组合 + 一小段差异」。

#### 4.4.3 源码精读

- [configs/pointpillars/pointpillars_hv_secfpn_8xb6-160e_kitti-3d-3class.py:L1-L5](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/pointpillars/pointpillars_hv_secfpn_8xb6-160e_kitti-3d-3class.py#L1-L5)：`_base_` 列表一次性继承四件套，顺序为 model → dataset → schedule → runtime。
- [configs/_base_/models/pointpillars_hv_secfpn_kitti.py:L3-L12](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/pointpillars_hv_secfpn_kitti.py#L3-L12)：模型基配置的骨架——顶层 `type='VoxelNet'` 决定检测器类，`data_preprocessor` 说明进模型前先做体素化（`voxel_layer` 的 `max_num_points=32`、`point_cloud_range`、`voxel_size` 都在这一层配置）。
- [configs/_base_/models/pointpillars_hv_secfpn_kitti.py:L13-L32](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/pointpillars_hv_secfpn_kitti.py#L13-L32)：`voxel_encoder`（PillarFeatureNet）、`middle_encoder`（PointPillarsScatter）、`backbone`（SECOND）、`neck`（SECONDFPN）四个组件的 type 与超参——**每个 `type` 都对应 `mmdet3d/models/` 下一个真实类**（见 4.4.4 实践）。
- [configs/_base_/models/pointpillars_hv_secfpn_kitti.py:L33-L62](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/pointpillars_hv_secfpn_kitti.py#L33-L62)：`bbox_head`（Anchor3DHead）内部又嵌套了 `anchor_generator`、`bbox_coder`、三个 loss——注意 loss 用了 `mmdet.FocalLoss` 这样的「跨库前缀」，表示从 MMDetection 的注册表借用（u1-l1 讲过的组件复用）。
- [configs/_base_/default_runtime.py:L1-L9](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/default_runtime.py#L1-L9)：`default_scope = 'mmdet3d'` 声明「默认到 mmdet3d 的注册节点找模块」（这正是 4.2 注册器机制的配置侧开关）；`default_hooks` 里 `Det3DVisualizationHook` 就是 `mmdet3d/engine/hooks/` 里的类。
- [configs/_base_/default_runtime.py:L11-L21](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/default_runtime.py#L11-L21)：`env_cfg`（cudnn/多进程/分布式后端）、`log_processor`、`load_from`、`resume` 等运行时杂项。

#### 4.4.4 代码实践

**实践目标**：把 `_base_/models/pointpillars_hv_secfpn_kitti.py` 里出现的每个 `type='XXX'` 反查到 `mmdet3d/` 下的源码文件，建立「config → 源码」的条件反射。

**操作步骤**：

1. 列出配置中所有 type：

```bash
grep -n "type = '\|type='" configs/_base_/models/pointpillars_hv_secfpn_kitti.py
```

2. 对每个类名执行反查（以 4 个关键组件为例）：

```bash
grep -rn "class VoxelNet(" mmdet3d/
grep -rn "class PillarFeatureNet(" mmdet3d/
grep -rn "class PointPillarsScatter(" mmdet3d/
grep -rn "class Anchor3DHead(" mmdet3d/
```

3. 把结果整理成「type → 源码文件 → 所属子目录」三列的表格。

**需要观察的现象**：每个 grep 都恰好命中一个文件；命中的文件全部落在 `mmdet3d/models/` 的不同子目录里（detectors、voxel_encoders、middle_encoders、dense_heads、task_modules…）。

**预期结果**（笔者已在当前 HEAD 验证）：

| config 中的 type | 源码文件 | 子目录 |
| --- | --- | --- |
| `VoxelNet` | `mmdet3d/models/detectors/voxelnet.py:12` | detectors |
| `Det3DDataPreprocessor` | `mmdet3d/models/data_preprocessors/data_preprocessor.py:23` | data_preprocessors |
| `PillarFeatureNet` | `mmdet3d/models/voxel_encoders/pillar_encoder.py:14` | voxel_encoders |
| `PointPillarsScatter` | `mmdet3d/models/middle_encoders/pillar_scatter.py:11` | middle_encoders |
| `SECOND` | `mmdet3d/models/backbones/second.py:15` | backbones |
| `SECONDFPN` | `mmdet3d/models/necks/second_fpn.py:12` | necks |
| `Anchor3DHead` | `mmdet3d/models/dense_heads/anchor3d_head.py:23` | dense_heads |
| `AlignedAnchor3DRangeGenerator` | `mmdet3d/models/task_modules/anchor/anchor_3d_generator.py:236` | task_modules |
| `DeltaXYZWLHRBBoxCoder` | `mmdet3d/models/task_modules/coders/delta_xyzwhlr_bbox_coder.py:10` | task_modules |
| `Max3DIoUAssigner` | `mmdet3d/models/task_modules/assigners/max_3d_iou_assigner.py:11` | task_modules |

注意 `mmdet.FocalLoss`、`mmdet.SmoothL1Loss` 在 `mmdet3d/` 里查不到——它们来自 MMDetection 包，这是「跨库前缀」的直接证据。

#### 4.4.5 小练习与答案

**练习 1**：`configs/_base_/schedules/cyclic-40e.py` 属于四件套中的哪一件？它通常包含哪些键？

**参考答案**：属于「训练日程」。包含 `train_cfg`（max_epochs）、`val_cfg`、`test_cfg`、`optim_wrapper`、`param_scheduler` 等，描述「怎么训」（u7-l1 精讲）。

**练习 2**：同一个 `_base_/models/` 下的模型配置为什么会同时存在 `centerpoint_pillar02_...nus.py` 和 `centerpoint_voxel01_...nus.py` 两个文件？

**参考答案**：CenterPoint 有柱体（pillar，0.2m 分辨率）和体素（voxel，0.1m 分辨率）两种实现，模型组件差异较大（middle_encoder、backbone 不同），拆成两个基础文件便于各自被上层实验继承，避免在实验文件里写大量覆盖。

**练习 3**：如果把 `default_runtime.py` 里的 `default_scope` 从 `'mmdet3d'` 改掉，会发生什么？

**参考答案**：注册器查找模块时不再默认限定在 mmdet3d 的注册节点（先查 MMEngine/MMCV 根注册表），配置里未加前缀的 `type='LoadPointsFromFile'` 等将找不到目标而报 KeyError/ModuleNotFoundError。跨库复用时才用 `mmdet.XXX`、`mmcv.XXX` 前缀显式指定 scope。

## 5. 综合实践

**任务：手工绘制一张「VoxelNet 类检测器的模块依赖图」**，把本讲四个模块的知识串起来。这是本讲规格中规定的实践任务。

**第一步：画出主数据流图**（在纸上或任何画图工具），按下面的骨架补全：

```text
原始点云(.bin)
  → [哪个目录负责加载？] LoadPointsFromFile
  → [哪个目录定义它的类型？] LiDARPoints
  → [哪个目录做体素化？] Det3DDataPreprocessor
  → 检测器 VoxelNet
       ├─ voxel_encoder: PillarFeatureNet
       ├─ middle_encoder: PointPillarsScatter
       ├─ backbone: SECOND
       ├─ neck: SECONDFPN
       └─ bbox_head: Anchor3DHead（内含 anchor_generator / bbox_coder / loss）
  → [预测结果的类型是什么？在哪个目录定义？] Det3DDataSample
  → [哪个目录负责算 mAP？] evaluation/
  → [哪个目录负责画 3D 框？] visualization/
```

**第二步：为每个节点标注「目录职责」**。参考答案骨架（先自己填再看）：

- `datasets/`：数据集类 + 管线（加载与增强）
- `structures/`：点云/3D 框/Det3DDataSample 的定义（被数据侧和模型侧共同使用）
- `models/data_preprocessors/`：batch 整理与体素化
- `models/voxel_encoders/` + `models/middle_encoders/`：柱体编码与伪图像散射
- `models/backbones/` + `models/necks/` + `models/dense_heads/`：2D 卷积特征提取、多尺度融合、出框
- `models/task_modules/`：anchor 生成、bbox 编码、正负样本分配
- `evaluation/` + `visualization/` + `engine/hooks/`：指标、画图、训练期钩子

**第三步：验证图与源码一致**。用 4.4.4 的 `grep -rn "class XXX(" mmdet3d/` 命令逐个核对图上的组件，确保每个箭头两端的类型都真实存在；再对照 [mmdet3d/models/detectors/voxelnet.py:L36-L48](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/voxelnet.py#L36-L48) 的 `extract_feat`（依次调用 `voxel_encoder → middle_encoder → backbone → neck`）确认你画的组件顺序与代码执行顺序一致。

**预期结果**：一张覆盖 `mmdet3d/` 至少 8 个子目录的依赖图；图上「VoxelNet 涉及的子目录」应包括：`detectors`、`data_preprocessors`、`voxel_encoders`、`middle_encoders`、`backbones`、`necks`、`dense_heads`、`task_modules`（以及训练时被 `datasets/`、`structures/`、`evaluation/`、`engine/hooks/`、`visualization/` 环绕）。这张图可以直接当 u4、u5 单元的阅读导航。

## 6. 本讲小结

- 仓库分三组目录：**实现**（`mmdet3d/`、`projects/`）、**描述**（`configs/`、`demo/`）、**入口与文档**（`tools/`、`docs/`、`tests/`）。
- `mmdet3d/` 六大子模块呈星形依赖：`structures/` 是被大家共用的底层数据契约；`registry.py` 用 17 个注册节点把 config 里的 `type` 路由到真实类，每个节点的 `locations` 指向它管辖的目录。
- `tools/train.py` 与 `tools/test.py` 都是「读配置 → 覆盖参数 → `Runner.from_cfg` → `train()/test()`」的五步流程，本身不含算法代码；`create_data.py` + `dataset_converters/` 负责原始数据到 info pkl 的转换。
- `configs/_base_/` 四件套 = 模型 + 数据集 + 日程 + 运行时；算法目录下的实验文件用 `_base_` 继承再做增量覆盖，文件名编码了算法/精度变体/卡数×batch/epoch/数据集信息。
- 「`grep -rn "class XXX(" mmdet3d/` 反查源码位置」是贯穿整本手册的基本功；带 `mmdet.` 前缀的 type 表示从 MMDetection 跨库借用。
- VoxelNet 类检测器一次贯穿 `detectors / data_preprocessors / voxel_encoders / middle_encoders / backbones / necks / dense_heads / task_modules` 八个模型子目录。

## 7. 下一步学习建议

下一讲 **u1-l5 配置系统入门** 将在本讲的 `_base_` 四件套基础上深入：讲解 MMEngine 配置继承语法、`--cfg-options` 覆盖，以及如何用 `tools/misc/print_config.py` 把 PointPillars 配置展开成完整字典。

在进入下一讲之前，建议先自己动手：

1. 通读 [mmdet3d/registry.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/registry.py)，数出 17 个注册节点并记下常用四五个（MODELS/TRANSFORMS/DATASETS/METRICS/VISUALIZERS）。
2. 用本讲的 grep 方法，反查 `configs/_base_/datasets/kitti-3d-3class.py` 里出现的每个 type（如 `RepeatDataset`、`LoadPointsFromFile`、`Pack3DDetInputs`），提前熟悉 u3 单元要讲的数据管线。
3. 浏览 `mmdet3d/structures/` 目录，找到 `bbox_3d/`、`points/`、`det3d_data_sample.py` 三个位置——它们是 u2 单元全部四讲的主角。
