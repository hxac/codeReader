# 仓库结构：detectron2 插件式项目如何组织

## 1. 本讲目标

学完本讲，你应该能够：

1. 画出 Mask2Former 仓库的目录树，并为每个子目录写一句「职责注释」。
2. 说清楚 `mask2former`（图像版）与 `mask2former_video`（视频版）两个 Python 包的分工，以及它们和 detectron2 的关系。
3. 理解「**import 即注册**」机制：为什么 `from mask2former import ...` 这一行代码就能让 detectron2 认识 `MaskFormer` 这个新模型。
4. 定位模型、数据、评估、配置、入口脚本各自所在的文件，知道改一个功能该去哪个目录找代码。

本讲不深入任何模型内部实现（那是单元三之后的事），只解决一个问题：**这个仓库的每一部分是干什么的，它们如何被组织在一起跑起来**。

## 2. 前置知识

### 2.1 detectron2 与「插件式项目」

detectron2 是 FAIR 的目标检测/分割框架，它本身只提供「引擎」：配置系统、数据加载器、训练循环（`DefaultTrainer`）、分布式启动器（`launch`）、 checkpoint 管理。它不内置 Mask2Former 这种模型。

detectron2 留出了「插槽」，通过 **Registry（注册表）** 机制实现：框架内部维护若干个全局字典，形如「名字 → 类」。比如 `META_ARCH_REGISTRY` 存放所有可选的顶层模型架构，配置文件里写 `MODEL.META_ARCHITECTURE: "MaskFormer"`，框架就会去这个字典里按名字取出 `MaskFormer` 类并实例化。

于是第三方项目（如 Mask2Former）不需要 fork detectron2，只需要：

1. 定义自己的类，用 `@某注册表.register()` 装饰器登记进去；
2. 用 `add_xxx_config` 向配置系统追加自己的配置项；
3. 写一个入口脚本（子类化 `DefaultTrainer`），剩下的训练/评估/分布式全部复用 detectron2。

这种「宿主框架 + 外挂包」的项目就叫 detectron2 插件式项目。**Mask2Former 仓库的全部代码，本质上就是一套插件 + 若干入口脚本 + 一堆配置文件。**

### 2.2 承接前两讲的认知

- 第 u1-l1 讲（语义分割推理分支）我们已经看过 `MaskFormer.forward` 的 eval 分支：结果会按 `SEMANTIC_ON / PANOPTIC_ON / INSTANCE_ON` 三个开关分发。本讲我们从「文件放在哪」的视角俯瞰整个仓库——你会看到那三个开关就定义在 `mask2former/config.py` 注册进去的配置里。
- 第 u1-l2 讲（环境搭建）我们编译了 `mask2former/modeling/pixel_decoder/ops` 下的 CUDA 算子。回忆一下：`sh make.sh` 实际执行的是 `python setup.py build install`（见 [ops/make.sh:L17](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/ops/make.sh#L17)），**只有这个 CUDA 扩展被 pip 安装**。仓库根目录没有 `setup.py`——`mask2former` 这个 Python 包是以「源码原地使用」的方式工作的：你必须站在仓库根目录运行脚本，`import mask2former` 才能通过当前目录解析成功。

### 2.3 一个容易忽略的 Python 知识点：装饰器和模块级语句在 import 时执行

「import 即注册」的全部魔法只依赖一条 Python 规则：**`import` 一个模块时，模块内的所有顶层语句会依次执行**。因此：

- `@SEM_SEG_HEADS_REGISTRY.register()` 写在类定义上方，类被定义（即模块被 import）的那一刻，注册动作就完成了；
- `register_ade20k_panoptic.py` 文件末尾直接写着函数调用 `register_all_ade20k_panoptic(_root)`，import 该文件时数据集就被登记进了 `DatasetCatalog`。

反过来也成立：**一个组件所在的模块若没被 import，它就不存在于注册表里**。这就是初学者常遇到的报错 `Cannot find object 'XXX' in registry` 的根源——通常是因为入口脚本没 import 对应的包。

## 3. 本讲源码地图

| 文件/目录 | 角色 | 本讲关注点 |
|---|---|---|
| [mask2former/__init__.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/__init__.py) | 图像版包入口 | import 链条如何触发全部注册 |
| [mask2former_video/__init__.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/__init__.py) | 视频版包入口 | 与图像版入口的对称结构 |
| [train_net.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py) | 图像版训练/评估入口 | `setup` 三层配置、`Trainer` 子类化、`launch` |
| [train_net_video.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net_video.py) | 视频版训练/评估入口 | 与图像版入口的 diff |
| `mask2former/modeling/__init__.py`、`mask2former/data/__init__.py` 等 | 子包入口 | 注册链条的中间环节 |
| `configs/`、`datasets/`、`tools/`、`demo/` | 配置、数据准备、工具、演示 | 目录职责 |

## 4. 核心概念与源码讲解

### 4.1 仓库全景：目录树与插件式项目的组织方式

#### 4.1.1 概念说明

先建立整体地图。Mask2Former 仓库根目录可以分成五类东西：

1. **两个入口脚本**：`train_net.py`（图像）、`train_net_video.py`（视频），分别对接两个模型包。
2. **两个模型包**：`mask2former/` 与 `mask2former_video/`，纯 Python 包，不含任何可执行入口。
3. **配置库** `configs/`：按「数据集 / 任务」两级目录组织的 yaml 文件，例如 `configs/coco/panoptic-segmentation/`。
4. **数据准备脚本** `datasets/`：注意这里**不放数据本身**，只放预处理脚本（如把 ADE20K 转成 detectron2 需要的目录结构）；真实数据默认也放在 `datasets/` 下（可用环境变量 `DETECTRON2_DATASETS` 改路径，见 4.2.3）。
5. **工具与演示**：`tools/`（权重转换、FLOPs 分析、PQ/boundary AP 评估）、`demo/` 与 `demo_video/`（推理演示）。

根目录还有 `predict.py` 和 `cog.yaml`，这是 Replicate 在线 demo 的推理入口（对应 git 历史中的 "replicate demo" 提交），与本地训练无关，初学时可忽略。

#### 4.1.2 核心流程

一个典型的使用动作与目录的对应关系：

```text
准备阶段：  datasets/prepare_*.py  →  把原始数据集整理到 datasets/<数据集名>/
配置阶段：  configs/<数据集>/<任务>/xxx.yaml  →  声明用哪个 backbone / 像素解码器 / 数据集
运行阶段：  python train_net.py --config-file <yaml>  →  import mask2former(注册) → 组装 cfg → 训练/评估
分析阶段：  tools/analyze_model.py 等  →  对训练产物做 FLOPs / 指标分析
```

一句话概括目录设计的指导思想：**「引擎全在 detectron2，本仓库只提供可替换的零件（模型包）+ 装配说明书（configs）+ 装配车间（入口脚本）」**。

#### 4.1.3 源码精读

仓库根目录一览（实测 `ls` 结果，略去 git 与教程目录）：

```text
Mask2Former/
├── train_net.py / train_net_video.py   # 两个入口脚本（4.4 节精读）
├── requirements.txt                    # 附加依赖：panopticapi、cityscapesscripts、timm 等
├── mask2former/                        # 图像版模型包（4.2 节精读）
├── mask2former_video/                  # 视频版模型包（4.3 节精读）
├── configs/                            # 配置库：ade20k / cityscapes / coco / mapillary-vistas / youtubevis_2019 / youtubevis_2021
├── datasets/                           # 数据集预处理脚本（README + prepare_*.py + 类别映射文件）
├── tools/                              # analyze_model.py、convert-*.py、evaluate_*.py
├── demo/ 与 demo_video/                # demo.py + predictor.py（视频版多一个 visualizer.py）
├── README.md / MODEL_ZOO.md / INSTALL.md / GETTING_STARTED.md / ADVANCED_USAGE.md
├── predict.py / cog.yaml               # Replicate 在线 demo 入口
└── LICENSE 等
```

「根目录没有 `setup.py`、源码原地使用」这一点在 [INSTALL.md](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/INSTALL.md) 的示例流程里可以印证：安装步骤是先 `pip install -e .` 装 **detectron2**，再 `pip install -r requirements.txt`，最后进入 `mask2former/modeling/pixel_decoder/ops` 执行 `sh make.sh` 编译算子——自始至终没有安装 Mask2Former 本身。

两个模型包的内部结构也是刻意对称的：

```text
mask2former/                        mask2former_video/
├── __init__.py                     ├── __init__.py
├── config.py        ← 配置项       ├── config.py        ← 视频版追加配置
├── maskformer_model.py ← 顶层模型  ├── video_maskformer_model.py
├── test_time_augmentation.py       ├── (无 TTA 文件)
├── data/             ← 数据        ├── data_video/       ← 视频数据
│   ├── datasets/     ← 数据集注册  │   ├── datasets/ (builtin.py, ytvis.py)
│   └── dataset_mappers/            │   ├── dataset_mapper.py, build.py
├── modeling/         ← 模型零件    │   └── ytvis_eval.py
│   ├── backbone/  pixel_decoder/   ├── modeling/         ← 视频版零件
│   ├── transformer_decoder/  meta_arch/                  │   ├── criterion.py, matcher.py
├── evaluation/                     │   └── transformer_decoder/
└── utils/                          └── utils/ (memory.py)
```

#### 4.1.4 代码实践：生成并注释你自己的目录树

1. **实践目标**：把「看地图」变成「自己画地图」，为后续每一讲快速定位文件打基础。
2. **操作步骤**：
   ```bash
   cd Mask2Former   # 站在仓库根目录
   find . -maxdepth 2 -type d -not -path './.git*' -not -path './Mask2Former-tutorial*' | sort
   ```
   把输出整理成树，然后为每个目录补一句中文职责注释（可参考 4.1.3 的树，但请自己写，写不出来的目录就是你需要回看的部分）。
3. **需要观察的现象**：`configs/` 下恰好是 6 个数据集目录；`mask2former/` 与 `mask2former_video/` 的子目录几乎一一对应。
4. **预期结果**：得到一张类似 4.1.3 的注释树。本讲义中的树即为参考答案（由当前 HEAD `9b0651c` 实测生成）。`find` 命令的精确输出随目录变化，属正常。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `mask2former` 不做成 `pip install` 的包，而要求站在仓库根目录运行？
**答案**：根目录没有 `setup.py`/`pyproject.toml`，唯一被安装的只有 `ops/` 下的 CUDA 扩展（[ops/make.sh:L17](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/ops/make.sh#L17) 执行 `python setup.py build install`）。站在根目录时，Python 把当前目录加入模块搜索路径，`import mask2former` 直接读源码目录——这是科研代码常见的「原地方便改」的取舍。

**练习 2**：`datasets/` 目录里没有一张图片，它存在的意义是什么？
**答案**：它存放数据集**预处理脚本**与类别映射文件（如 `prepare_ade20k_pan_seg.py`、`ade20k_instance_catid_mapping.txt`），负责把官方发布的原始数据整理成 detectron2 期望的目录结构；整理后的真实数据默认落在 `datasets/` 下（根路径由环境变量 `DETECTRON2_DATASETS` 控制，默认值见 [register_ade20k_panoptic.py:L389](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/datasets/register_ade20k_panoptic.py#L389)）。

**练习 3**：想在 `MODEL_ZOO.md` 里查 R50 全景分割模型的成绩，应该去哪个目录找它对应的训练配置？
**答案**：`configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml`（配置按 `configs/<数据集>/<任务>/` 两级组织）。

### 4.2 `mask2former/__init__.py`：import 即注册

#### 4.2.1 概念说明

`mask2former/__init__.py` 只有 27 行，却是整个图像版包的「总开关」。它的每一行 import 都不只是「让名字可用」，更是**触发注册的指令**。读懂这个文件，就读懂了 detectron2 插件项目的核心玩法。

注册分两类：

- **模型零件注册**：类定义处的装饰器，往 `META_ARCH_REGISTRY`、`SEM_SEG_HEADS_REGISTRY`、`BACKBONE_REGISTRY` 等注册表里登记类；
- **数据集注册**：模块末尾的函数调用，往 `DatasetCatalog` / `MetadataCatalog` 里登记数据集的名字、路径和元信息。

#### 4.2.2 核心流程

`from mask2former import ...`（例如入口脚本 [train_net.py:L50-L59](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L50-L59) 中那行）触发的完整链条：

```text
from mask2former import ...
 └─> 执行 mask2former/__init__.py
      ├─ from . import data                       # 链条 A：数据
      │    └─ data/__init__.py: from . import datasets
      │         └─ datasets/__init__.py 逐个 import 7 个 register_* 模块
      │              └─ 每个模块末尾直接调用 register_all_*(root)
      │                   └─ DatasetCatalog.register(...) / MetadataCatalog.get(...).set(...)
      ├─ from . import modeling                   # 链条 B：模型零件
      │    └─ modeling/__init__.py import swin / fpn / msdeformattn / meta_arch
      │         └─ 各文件中 @BACKBONE_REGISTRY.register() 等装饰器在类定义时执行
      ├─ from .config import add_maskformer2_config   # 链条 C：配置项（函数对象，尚未调用）
      ├─ 5 个 DatasetMapper 类                     # 直接 re-export，供 Trainer.build_train_loader 用
      ├─ MaskFormer / SemanticSegmentorWithTTA     # 顶层模型 + TTA 包装（re-export）
      └─ InstanceSegEvaluator                      # 评估器（re-export）
```

注意链条 C 的区别：`__init__.py` 只是把 `add_maskformer2_config` 这个函数**带出来**，真正往 `cfg` 里添加配置项要等入口脚本 `setup()` 里显式调用它（见 4.4.3）。

#### 4.2.3 源码精读

先看包入口本身——[mask2former/\_\_init\_\_.py:L1-L27](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/__init__.py#L1-L27)，全文分四段：

- [L2-L3](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/__init__.py#L2-L3)：`from . import data` 与 `from . import modeling`——这两行是注册的发动机（见下方两个子链条）。
- [L6](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/__init__.py#L6)：导出 `add_maskformer2_config`，其定义在 [config.py:L6](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/config.py#L6)，负责把 `MODEL.MASK_FORMER`、`INPUT.DATASET_MAPPER_NAME` 等 Mask2Former 专属配置节点挂到 detectron2 的 `cfg` 上。
- [L9-L19](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/__init__.py#L9-L19)：re-export 5 个 DatasetMapper（语义/全景/实例三种 mask_former 系列 + 新基线 LSJ 两种），入口脚本按 `cfg.INPUT.DATASET_MAPPER_NAME` 选择。
- [L22-L26](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/__init__.py#L22-L26)：re-export 顶层模型 `MaskFormer`、TTA 包装类和评估器 `InstanceSegEvaluator`。

**链条 A（数据注册）**：[data/\_\_init\_\_.py:L2](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/__init__.py#L2) 只有一句 `from . import datasets`；[datasets/\_\_init\_\_.py:L2-L10](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/datasets/__init__.py#L2-L10) 逐个 import 7 个 `register_*` 模块。以 ADE20K 全景分割为例，[register_ade20k_panoptic.py:L389-L390](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/datasets/register_ade20k_panoptic.py#L389-L390) 在模块最外层直接执行 `_root = os.getenv("DETECTRON2_DATASETS", "datasets")` 和 `register_all_ade20k_panoptic(_root)`——import 即注册数据集；具体的登记动作是同文件中的 [DatasetCatalog.register（L290 附近）](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/datasets/register_ade20k_panoptic.py#L290) 与 [MetadataCatalog.get(...).set（L296 附近）](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/data/datasets/register_ade20k_panoptic.py#L296)。

**链条 B（模型零件注册）**：[modeling/\_\_init\_\_.py:L2-L6](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/__init__.py#L2-L6) import 了 5 个模块，沿途触发的装饰器登记汇总成下表（本表即为 4.2.4 实践脚本的「标准答案」）：

| 注册表 | 注册的类 | 定义位置（登记点） |
|---|---|---|
| `META_ARCH_REGISTRY` | `MaskFormer` | [maskformer_model.py:L20-L21](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L20-L21)（在 `__init__.py` L22 的 import 间接触发） |
| `SEM_SEG_HEADS_REGISTRY` | `MaskFormerHead`（三大组件组装头） | [meta_arch/mask_former_head.py:L18](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/meta_arch/mask_former_head.py#L18) |
| `SEM_SEG_HEADS_REGISTRY` | `BasePixelDecoder` | [pixel_decoder/fpn.py:L37](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/fpn.py#L37) |
| `SEM_SEG_HEADS_REGISTRY` | `TransformerEncoderPixelDecoder` | [pixel_decoder/fpn.py:L204](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/fpn.py#L204) |
| `SEM_SEG_HEADS_REGISTRY` | `MSDeformAttnPixelDecoder`（默认像素解码器） | [pixel_decoder/msdeformattn.py:L164](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/msdeformattn.py#L164) |
| `SEM_SEG_HEADS_REGISTRY` | `PerPixelBaselineHead` / `PerPixelBaselinePlusHead` | [meta_arch/per_pixel_baseline.py:L17](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/meta_arch/per_pixel_baseline.py#L17) / [L126](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/meta_arch/per_pixel_baseline.py#L126) |
| `BACKBONE_REGISTRY` | `D2SwinTransformer` | [backbone/swin.py:L686-L687](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/backbone/swin.py#L686-L687) |
| 项目自建 `TRANSFORMER_DECODER_REGISTRY` | `StandardTransformerDecoder` / `MultiScaleMaskedTransformerDecoder` | [maskformer_transformer_decoder.py:L30](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/maskformer_transformer_decoder.py#L30) / [mask2former_transformer_decoder.py:L207](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L207) |

两个值得注意的设计：

1. **像素解码器没有自己的注册表**，而是复用 `SEM_SEG_HEADS_REGISTRY`——构建函数 [build_pixel_decoder（fpn.py:L23-L36）](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/fpn.py#L23-L36) 从该表取名，再校验类实现了 `forward_features` 方法。
2. **Transformer 解码器用的是项目自建注册表** [TRANSFORMER_DECODER_REGISTRY（maskformer_transformer_decoder.py:L16-L26）](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/maskformer_transformer_decoder.py#L16-L26)，因为 detectron2 没有这个插槽，项目就用 fvcore 的 `Registry` 类自己造了一个——这也示范了「插槽不够就自己开」的扩展方式。

#### 4.2.4 代码实践：用脚本验证「import 即注册」

1. **实践目标**：亲眼看到 `import mask2former` 前后注册表内容的变化，把 4.2.3 的表格从「书上的结论」变成「你机器上的事实」。
2. **操作步骤**：在仓库根目录创建脚本（以下为**示例代码**，建议放在教程目录或任意非源码目录）：

   ```python
   # list_registry.py（示例代码）
   # 前提：已完成 u1-l2 的环境安装（detectron2 可 import），并站在仓库根目录运行
   from detectron2.modeling import META_ARCH_REGISTRY, SEM_SEG_HEADS_REGISTRY

   def snapshot(reg):
       # fvcore Registry 用内部字典 _obj_map 保存「名字 -> 类」；
       # 新版 fvcore 也可用 reg.keys() 或直接迭代 reg
       return set(reg._obj_map.keys())

   # 第一步：先拍快照（此时只有 detectron2 自己注册的内容）
   meta_before = snapshot(META_ARCH_REGISTRY)
   heads_before = snapshot(SEM_SEG_HEADS_REGISTRY)

   # 第二步：import 插件包——这一行就是 train_net.py 里发生的事
   import mask2former

   # 第三步：再做快照，差集即为 Mask2Former 注册进去的组件
   print("== Mask2former 注册进 META_ARCH_REGISTRY 的组件 ==")
   for name in sorted(snapshot(META_ARCH_REGISTRY) - meta_before):
       print("  ", name)

   print("== Mask2former 注册进 SEM_SEG_HEADS_REGISTRY 的组件 ==")
   for name in sorted(snapshot(SEM_SEG_HEADS_REGISTRY) - heads_before):
       print("  ", name)
   ```

   运行：`python list_registry.py`（需在仓库根目录）。
3. **需要观察的现象**：两段差集输出；以及一个反事实实验——把 `import mask2former` 那行注释掉再跑，差集应为空。
4. **预期结果**（按源码静态分析，本讲义写作环境未安装 detectron2，**具体输出待本地验证**）：
   - `META_ARCH_REGISTRY` 新增：`MaskFormer`；
   - `SEM_SEG_HEADS_REGISTRY` 新增 6 个：`MaskFormerHead`、`BasePixelDecoder`、`TransformerEncoderPixelDecoder`、`MSDeformAttnPixelDecoder`、`PerPixelBaselineHead`、`PerPixelBaselinePlusHead`（与 4.2.3 表格一致）；
   - 快照里剩余的（差集之外的）名字是 detectron2 自身注册的组件（如 `GeneralizedRCNN`、`SemanticSegmentor`、`SemSegFPNHead` 等，以你安装的 detectron2 版本为准）。
5. **一个命名提醒**：detectron2 中该注册表的真实名字是 `META_ARCH_REGISTRY`（全称 meta architecture registry）。有些资料（包括本手册大纲的实践描述）把它口语化写成 `META_ARCHITECTURE_REGISTRY`，直接那样 import 会报 `ImportError`——以 [maskformer_model.py:L10](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L10) 的真实 import 为准。

#### 4.2.5 小练习与答案

**练习 1**：如果不执行 `import mask2former`，直接用 detectron2 的 `build_model` 构建 `MODEL.META_ARCHITECTURE: "MaskFormer"` 的配置，会发生什么？
**答案**：报错，形如 `Cannot find object 'MaskFormer' in registry`。因为 `MaskFormer` 是在 [maskformer_model.py:L20](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L20) 的装饰器里登记的，而装饰器只在模块被 import 时执行——这正是「import 即注册」的反面验证。

**练习 2**：`mask2former/__init__.py` 里 `from .config import add_maskformer2_config` 和 `from . import modeling` 触发的动作有什么本质区别？
**答案**：前者只是把一个函数对象导出（配置项要等 `setup()` 里调用该函数才写入 cfg）；后者触发一堆类定义执行，注册在 import 的瞬间完成，不需要任何显式调用。

**练习 3**：`MaskFormerHead`、`MSDeformAttnPixelDecoder` 都注册在 `SEM_SEG_HEADS_REGISTRY`，detectron2 是如何区分「顶层头」和「像素解码器」的？
**答案**：靠配置节点和构建函数分工：配置 `MODEL.SEM_SEG_HEAD.NAME` 指向 `MaskFormerHead` 由 detectron2 的 `build_sem_seg_head` 构建；而像素解码器由项目自己的 [build_pixel_decoder（fpn.py:L23-L36）](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/fpn.py#L23-L36) 从**同一个注册表**按 `MODEL.SEM_SEG_HEAD.PIXEL_DECODER_NAME` 取名构建，并额外校验 `forward_features` 方法是否存在。

### 4.3 `mask2former_video/__init__.py` 与两个包的分工

#### 4.3.1 概念说明

`mask2former_video` 把同一套架构推广到**视频实例分割**（VIS：在视频中检测、分割并跨帧关联实例，数据集为 YouTubeVIS）。它是「寄生」在图像版之上的薄层：backbone、像素解码器直接复用 `mask2former` 的注册组件，自己只重写随时间维度变化的部分（顶层模型、Transformer 解码器、匹配器与损失、数据管线）。

两个包的分工边界：

| 职责 | `mask2former` | `mask2former_video` |
|---|---|---|
| 配置项 | `add_maskformer2_config`（图像全套） | `add_maskformer2_video_config`（**追加**视频项，如输入帧数） |
| 顶层模型 | `MaskFormer` | `VideoMaskFormer` |
| backbone / 像素解码器 | 提供（swin、MSDeformAttn 等） | 复用，不重复注册 |
| Transformer 解码器 | `MultiScaleMaskedTransformerDecoder` | `VideoMultiScaleMaskedTransformerDecoder` |
| 数据管线 | `data/`（图像 mapper、数据集注册） | `data_video/`（YTVIS mapper、按视频分组的 loader、YTVIS 评估器） |
| 训练入口 | `train_net.py` | `train_net_video.py` |

关键事实：**`mask2former_video/__init__.py` 并不 import `mask2former`**，图像与视频两条线互不牵连；真正把两者连起来的是入口脚本 `train_net_video.py`（见 4.4.3）。

#### 4.3.2 核心流程

`import mask2former_video` 的触发链条（与 4.2.2 对称但更短）：

```text
import mask2former_video
 └─> mask2former_video/__init__.py
      ├─ from . import modeling                     # 注册 VideoMultiScaleMaskedTransformerDecoder
      ├─ from .config import add_maskformer2_video_config
      ├─ from .video_maskformer_model import VideoMaskFormer   # 注册进 META_ARCH_REGISTRY
      └─ from .data_video import YTVISDatasetMapper, YTVISEvaluator,
                                build_detection_train_loader, ... # 视频数据管线（含数据集注册）
```

#### 4.3.3 源码精读

[mask2former_video/\_\_init\_\_.py:L1-L18](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/__init__.py#L1-L18) 全文四个动作：

- [L2](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/__init__.py#L2)：`from . import modeling`，经 [modeling/\_\_init\_\_.py:L2](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/modeling/__init__.py#L2) 触发视频版解码器注册；同目录的 `criterion.py`、`matcher.py` 定义了 `VideoSetCriterion`、`VideoHungarianMatcher`（被顶层模型 import 使用，而非注册表组件）。
- [L5](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/__init__.py#L5)：导出 [add_maskformer2_video_config（config.py:L6）](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/config.py#L6)，在图像版配置之上追加视频专属项。
- [L8](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/__init__.py#L8)：`from .video_maskformer_model import VideoMaskFormer`，登记点在 [video_maskformer_model.py:L24-L25](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/video_maskformer_model.py#L24-L25) 的 `@META_ARCH_REGISTRY.register()`——与图像版 `MaskFormer` 挂在**同一个注册表**里。
- [L11-L17](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/__init__.py#L11-L17)：从 `data_video` 导出视频数据管线全家桶。`data_video/__init__.py` 又会 `from .datasets import *`，其中 [datasets/\_\_init\_\_.py:L4](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/data_video/datasets/__init__.py#L4) 的注释直白写着 `# ensure the builtin datasets are registered`——import 即注册在视频包同样成立（YouTubeVIS 2019/2021 的注册在 `builtin.py`）。

#### 4.3.4 代码实践：diff 两个包入口

1. **实践目标**：用工具而不是肉眼确认两个包入口的结构对称性与差异点。
2. **操作步骤**（纯阅读型，无需运行环境）：

   ```bash
   diff mask2former/__init__.py mask2former_video/__init__.py
   grep -n "META_ARCH_REGISTRY.register" mask2former/maskformer_model.py mask2former_video/video_maskformer_model.py
   ls mask2former/modeling mask2former_video/modeling
   ```

3. **需要观察的现象**：diff 输出中图像版多出的 `data`、5 个 mapper、`InstanceSegEvaluator`、`SemanticSegmentorWithTTA` 行；视频版多出的 `data_video` 导入块。
4. **预期结果**（已静态验证，当前 HEAD 下确定）：
   - 两个顶层模型都在各自文件的第 24/20 行附近用 `@META_ARCH_REGISTRY.register()` 登记（图像版 [maskformer_model.py:L20](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L20)、视频版 [video_maskformer_model.py:L24](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/video_maskformer_model.py#L24)）；
   - `mask2former_video/modeling/` 下没有 `backbone/`、`pixel_decoder/` 目录——复用图像版的最直接证据；
   - 若在已装好环境时执行 `python -c "import mask2former_video"`，应无报错且不连带注册图像版组件（**待本地验证**；可用 4.2.4 的快照差集法验证）。

#### 4.3.5 小练习与答案

**练习 1**：`train_net_video.py` 需要让 `VideoMaskFormer`、图像版的 `D2SwinTransformer` 同时可用，它必须 import 哪些包？
**答案**：两个都 import。见 [train_net_video.py:L46-L54](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net_video.py#L46-L54)：先 `from mask2former import add_maskformer2_config`（间接触发图像版全部注册），再 `from mask2former_video import ...`。

**练习 2**：为什么 `VideoMaskFormer` 和 `MaskFormer` 可以注册进同一个 `META_ARCH_REGISTRY` 而不冲突？
**答案**：注册表按「类名 → 类」存字典，两个类名不同即可共存；配置里 `MODEL.META_ARCHITECTURE` 填谁就构建谁。只有当两个同名类先后注册时 fvcore 才会 assert 失败。

**练习 3**：视频版的 `retry_if_cuda_oom` 放在 `mask2former_video/utils/memory.py`，图像版直接用 detectron2 的版本（[maskformer_model.py:L14](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L14)）。这说明了两个包怎样的关系？
**答案**：视频包对图像包是「补充而非替代」——它只携带自己需要改动或新增的代码，能复用的（detectron2 工具、backbone、像素解码器）一律不复制。这也提示我们：读视频代码时若在某目录找不到文件，先去图像包同名目录找。

### 4.4 `train_net.py` / `train_net_video.py`：入口脚本解剖

#### 4.4.1 概念说明

入口脚本是「装配车间」：它自己几乎不含算法，只做三件事——

1. **组装配置**（`setup`）：三层配置函数叠加 + yaml 文件 + 命令行覆盖；
2. **定制训练器**（`Trainer(DefaultTrainer)`）：覆写若干 `build_*` 类方法，把 detectron2 通用引擎的「插槽」接到 Mask2Former 的组件上；
3. **分发运行**（`main` + `launch`）：区分训练/评估分支，并处理多卡分布式启动。

两个入口脚本的 docstring 都自称是 detectron2 `tools/` 下训练脚本的简化版（[train_net.py:L2-L6](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L2-L6)）。

#### 4.4.2 核心流程

`python train_net.py --config-file xxx.yaml [opts]` 的执行流程：

```text
__main__ 入口
 ├─ default_argument_parser().parse_args()      # 解析 --config-file / --num-gpus / --eval-only / --resume / opts
 └─ launch(main, num_gpus, ...)                 # detectron2 分布式启动：每张卡 spawn 一个进程
      └─ main(args)
          ├─ setup(args)
          │    ├─ cfg = get_cfg()                       # 第 1 层：detectron2 默认配置
          │    ├─ add_deeplab_config(cfg)               # 第 2 层：DeepLab 插件（poly lr 等）
          │    ├─ add_maskformer2_config(cfg)           # 第 3 层：Mask2Former 专属配置
          │    ├─ cfg.merge_from_file(args.config_file) # 第 4 层：yaml 配置文件
          │    ├─ cfg.merge_from_list(args.opts)        # 第 5 层：命令行覆盖（优先级最高）
          │    ├─ cfg.freeze() + default_setup(cfg,args)
          │    └─ setup_logger(name="mask2former")      # 让本包的日志有独立 handler
          ├─ 若 --eval-only：build_model → resume_or_load → Trainer.test（→ 可选 TTA）
          └─ 否则：Trainer(cfg) → resume_or_load → trainer.train()
               └─ DefaultTrainer 训练循环中会回调 Trainer 覆写的 build_train_loader /
                  build_optimizer / build_lr_scheduler / build_evaluator 等类方法
```

视频版 [train_net_video.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net_video.py) 在此骨架上有三点不同（见 4.4.3 的最后一组链接）。

#### 4.4.3 源码精读

**（1）`setup` 的三层配置叠加**——[train_net.py:L281-L295](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L281-L295)：先 `get_cfg()` 拿 detectron2 全量默认值，再依次 `add_deeplab_config(cfg)`（借 DeepLab 项目的 poly 学习率调度等）、`add_maskformer2_config(cfg)`（Mask2Former 专属节点），然后才 `merge_from_file` 读 yaml、`merge_from_list` 接收命令行覆盖，最后 `freeze()` 锁定。**顺序不可颠倒**：yaml 里能写的键必须先被 add 函数声明过。视频版对应 [train_net_video.py:L241-L257](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net_video.py#L241-L257)，多叠一层 `add_maskformer2_video_config(cfg)`，并额外为 `mask2former_video` 配置 logger。

**（2）`Trainer` 子类的五个覆写点**——[train_net.py:L62-L278](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L62-L278)：

| 覆写方法 | 行号 | 作用 |
|---|---|---|
| `build_evaluator` | [L67-L147](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L67-L147) | 按数据集元数据 `evaluator_type` + `MODEL.MASK_FORMER.TEST` 三开关组合评估器（单元七展开） |
| `build_train_loader` | [L149-L173](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L149-L173) | 按 `INPUT.DATASET_MAPPER_NAME` 从 5 个 mapper 里选一个挂到 loader 上（单元二展开） |
| `build_lr_scheduler` | [L175-L181](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L175-L181) | 改用 deeplab 的 poly lr 调度 |
| `build_optimizer` | [L183-L262](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L183-L262) | 按模块名分组设 lr/weight_decay，含 full-model 梯度裁剪（u2-l4 展开） |
| `test_with_TTA` | [L264-L278](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L264-L278) | 用 `SemanticSegmentorWithTTA` 包装模型做测试时增强评估（u10-l3 展开） |

**（3）`main` 的分支与 `launch`**——[train_net.py:L298-L315](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L298-L315)：`--eval-only` 时构建模型、加载 `MODEL.WEIGHTS`、跑 `Trainer.test`（若 `TEST.AUG.ENABLED` 追加 TTA）；否则构造 `Trainer` 进入训练。[L318-L328](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L318-L328) 的 `launch` 负责「每机每卡一个进程」的分布式启动——单卡 `--num-gpus 1` 时它直接调用 `main`，逻辑完全一致。

**（4）视频版入口的三点差异**——[train_net_video.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net_video.py)：

- import 同时拉起两个包（[L46-L54](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net_video.py#L46-L54)）；
- `build_train_loader`/`build_test_loader` 换成视频版（[L76-L93](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net_video.py#L76-L93)），`build_evaluator` 只返回 `YTVISEvaluator`（[L62-L74](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net_video.py#L62-L74)）；
- 整个覆写了 `test` 类方法以包一层 `autocast` 混合精度（[L184-L238](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net_video.py#L184-L238)），且 `main` 里 `TEST.AUG.ENABLED` 直接 `raise NotImplementedError`（[L269-L270](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net_video.py#L269-L270)）——视频版没有 TTA 支持。

顺带一提，`demo/demo.py` 的 `setup_cfg` 也是同一套三层叠加（[demo/demo.py:L37-L38](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/demo/demo.py#L37-L38)）——「配置组装模式」在这个仓库里到处复用，下一讲（u1-l4）会用到它。

#### 4.4.4 代码实践：解剖入口脚本结构

1. **实践目标**：不读全文、只用 grep 建立 `train_net.py` 的结构索引，并核对其与视频版的差异清单。
2. **操作步骤**：

   ```bash
   # 1) 列出入口脚本的骨架（类与函数定义）
   grep -n "^class \|^def \|    def \|    @classmethod" train_net.py
   # 2) 对比视频版
   grep -n "^class \|^def \|    def \|    @classmethod" train_net_video.py
   # 3) 定位配置叠加顺序
   grep -n "add_deeplab_config\|add_maskformer2_config\|merge_from" train_net.py train_net_video.py
   ```

3. **需要观察的现象**：步骤 1 输出应与 4.4.3 表格中的五个方法一一对应；步骤 3 应显示视频版比图像版多一行 `add_maskformer2_video_config`。
4. **预期结果**（静态验证确定；grep 输出即所得）：图像版 `setup` 内的顺序为 `get_cfg → add_deeplab_config → add_maskformer2_config → merge_from_file → merge_from_list → freeze`；视频版在其间插入 `add_maskformer2_video_config(cfg)`。若想真正运行（如 `python train_net.py --help` 查看 detectron2 默认参数，或加 `--eval-only` 跑评估），需要按 u1-l2 完成环境安装并准备数据/权重，**具体运行输出待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：把 `add_maskformer2_config(cfg)` 移到 `cfg.merge_from_file(...)` 之后会怎样？
**答案**：`merge_from_file` 会因为 yaml 里的键（如 `MODEL.MASK_FORMER`）在 cfg 中不存在而报错（CfgNode 默认严格匹配未知键）。所以 add 函数必须先执行、把配置节点「声明」出来。

**练习 2**：`--eval-only` 分支里为什么用 `Trainer.build_model(cfg)` 而不是实例化 `Trainer`？
**答案**：评估不需要数据加载器和优化器，`build_model` 是 `DefaultTrainer` 的类方法，只构建模型并加载权重（[train_net.py:L301-L311](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L301-L311)），避免构造完整 Trainer 带来的多余开销。

**练习 3**：单卡运行时 `launch` 有什么用？
**答案**：它统一了单卡与多卡的代码路径。`--num-gpus 1` 时 `launch` 直接在本进程调用 `main`；多卡时它为每张卡 spawn 子进程并设置分布式环境变量，`main` 内部代码无需感知进程数差异（[train_net.py:L318-L328](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L318-L328)）。

## 5. 综合实践：绘制「注册地图」

把本讲三个知识点（目录树、import 即注册、入口脚本）串成一个任务：**为仓库生成一张「谁在 import 时向哪个注册表登记了什么」的地图**。

任务步骤：

1. 在已按 u1-l2 装好环境的机器上，基于 4.2.4 的脚本扩展：除了 `META_ARCH_REGISTRY`、`SEM_SEG_HEADS_REGISTRY`，再纳入 `BACKBONE_REGISTRY`（import 路径见 [swin.py:L18](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/backbone/swin.py#L18)：`from detectron2.modeling import BACKBONE_REGISTRY`）和项目自建的 `TRANSFORMER_DECODER_REGISTRY`（import 路径：[maskformer_transformer_decoder.py:L16](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/maskformer_transformer_decoder.py#L16)）。
2. 分别在 `import mask2former` 与 `import mask2former_video` 前后拍快照，输出四张「新增组件」清单。
3. 对照 4.2.3 的表格，把每个新增组件名映射回它的源文件与所属子目录，整理成一张 markdown 表格：`注册表 | 组件名 | 源文件 | 所在目录职责`。
4. 回答一个检验性问题：`import mask2former_video` 之后，`SEM_SEG_HEADS_REGISTRY` 的新增集是空还是非空？为什么？

**预期结果**（静态分析，运行输出**待本地验证**）：

- `import mask2former` 应新增：META_ARCH 1 个（`MaskFormer`）、SEM_SEG_HEADS 6 个（见 4.2.3 表）、BACKBONE 1 个（`D2SwinTransformer`）、TRANSFORMER_DECODER 2 个（`StandardTransformerDecoder`、`MultiScaleMaskedTransformerDecoder`）。
- `import mask2former_video` 应新增：META_ARCH 1 个（`VideoMaskFormer`）、TRANSFORMER_DECODER 1 个（`VideoMultiScaleMaskedTransformerDecoder`）。
- 检验题答案：**空**。视频包没有自己的 backbone/像素解码器代码，也不 import `mask2former`，所以不会向 `SEM_SEG_HEADS_REGISTRY` 登记任何东西——这正是 4.3.1 所说「薄层复用」的直接证据；真正让视频训练用到图像版像素解码器的是 `train_net_video.py` 同时 import 两个包。

## 6. 本讲小结

- Mask2Former 是 detectron2 **插件式项目**：仓库 = 两个模型包（`mask2former` / `mask2former_video`）+ 两个入口脚本（`train_net.py` / `train_net_video.py`）+ 配置库 `configs/` + 数据准备脚本 `datasets/` + 工具 `tools/`；根目录没有 `setup.py`，源码原地使用，唯一 pip 安装的是 `ops/` 下的 CUDA 扩展。
- **import 即注册**：`__init__.py` 里的每行 import 都在触发动作——装饰器在类定义时把组件登记进 `META_ARCH_REGISTRY` / `SEM_SEG_HEADS_REGISTRY` / `BACKBONE_REGISTRY`，`register_*` 模块末尾的函数调用把数据集登记进 `DatasetCatalog` / `MetadataCatalog`；忘了 import 就会得到 `Cannot find object ... in registry`。
- 像素解码器复用 `SEM_SEG_HEADS_REGISTRY`（构建函数 `build_pixel_decoder` 额外校验 `forward_features`）；Transformer 解码器用的是项目**自建**的 `TRANSFORMER_DECODER_REGISTRY`——插槽不够就自己开。
- `mask2former_video` 是寄生在图像版上的**薄层**：只重写顶层模型、解码器、损失匹配与数据管线，backbone/像素解码器靠 `train_net_video.py` 同时 import 两个包来复用。
- 入口脚本三段式：`setup` 做「detectron2 默认 → deeplab → Mask2Former（→ 视频）→ yaml → 命令行」的多层配置叠加；`Trainer(DefaultTrainer)` 覆写五个 `build_*` 方法接插件组件；`main` + `launch` 分流训练/评估并统一单多卡路径。

## 7. 下一步学习建议

- **下一讲（u1-l4）**：跑通第一个推理 Demo——`demo/demo.py` 的 `setup_cfg` 正是本讲 4.4.3 结尾提到的同一套配置叠加模式，你可以带着「import 即注册」的眼光去看 `DefaultPredictor` 是如何按 `MODEL.META_ARCHITECTURE` 找到 `MaskFormer` 的。
- **再下一讲（u1-l5）**：配置系统专讲——`add_maskformer2_config` 注册了哪些键、`_BASE_` yaml 继承如何工作。
- **延伸阅读源码**（配合本讲）：detectron2 的 `Registry` 实现（fvcore `common/registry.py`，体会 `_obj_map` 与 `register()` 装饰器）；`mask2former/modeling/transformer_decoder/maskformer_transformer_decoder.py` 开头 30 行（项目自建注册表与构建函数的完整范式，是单元八自定义组件的模板）。
- 单元二起我们将沿着 `train_net.py` 的 `build_train_loader` 进入数据管线；届时本讲的目录树就是你的导航图。
