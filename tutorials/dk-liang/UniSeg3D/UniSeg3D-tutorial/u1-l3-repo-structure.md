# 仓库目录结构与代码地图

## 1. 本讲目标

前两讲我们知道了 UniSeg3D 是什么（一个模型统一六类 3D 分割任务）、怎么跑起来（环境与 dist 脚本）。但从这一讲开始，我们要真正走进源码。学完本讲，你应该能够：

1. 说出仓库六大目录（`uniseg3d/`、`open_clip/`、`configs/`、`tools/`、`data/`、`content/`）各自的职责。
2. 在 `uniseg3d/` 包内按「模型 / 数据管线 / 损失 / 评估」四类快速定位任意功能的代码位置——例如「我想改实例分割的匹配代价」能立刻想到 `uniseg3d/instance_criterion.py`。
3. 彻底理解 `uniseg3d/__init__.py` 如何通过一行行 `import` 触发 29 个 `register_module()` 注册，并理解「注册发生在模块导入时，与 `__init__.py` 是否导出该名字无关」这个容易踩坑的细节。
4. 说清仓库为什么内嵌一份 `open_clip/` 第三方库、它在导入链中如何被优先使用。

这一讲不深入任何模块的算法细节，只建立「地图」。有了地图，后面单元 3～6 的每一讲都是在地图上点亮一个具体区域。

## 2. 前置知识

- **Registry（注册器）**：u1-l1 已介绍，mmengine 用一个全局字典（注册表）维护「字符串名字 → Python 类」的映射。配置文件里写 `type='UniSeg3D'`，框架就在注册表里查到 `UniSeg3D` 类并实例化它。mmdet3d 提供了多个注册表，本讲会碰到五个：`MODELS`（模型与损失）、`TRANSFORMS`（数据变换）、`DATASETS`（数据集）、`METRICS`（评估指标）、`TASK_UTILS`（匹配器等工具）。
- **装饰器（decorator）**：Python 语法糖。`@MODELS.register_module()` 写在 `class Xxx:` 上一行，等价于执行 `MODELS.register_module()(Xxx)`，即「把 Xxx 这个类登记进 MODELS 注册表」。理解成「给类贴一张可供配置文件按名字查找的标签」即可。
- **包（package）与 `__init__.py`**：Python 中带 `__init__.py` 的目录是一个包。`import uniseg3d` 时，`__init__.py` 里的代码会被执行——这正是注册的触发时机。
- **模块导入的副作用**：`from .transforms_3d import A` 会**完整执行** `transforms_3d.py` 整个文件，哪怕只导入其中一个类 A，文件里定义的其他类（及其装饰器）也都会运行。这是本讲 4.3 节的关键。
- **`PYTHONPATH`**：u1-l2 讲过 `dist_train.sh` 会把仓库根目录注入 `PYTHONPATH`，本讲会用到这个结论来解释 `import open_clip` 为什么解析到本地目录。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `uniseg3d/__init__.py` | 核心包的注册入口：19 行 import 决定了哪些模块被加载、哪些类被注册 |
| `uniseg3d/uniseg3d.py` | 主模型 `UniSeg3D`（模型层的「总装车间」），用 `MODELS.build` 组装子模块 |
| `uniseg3d/query_decoder.py` | 查询解码器：`QueryDecoder`（基类）与 `UnifiedQueryDecoder`（实际使用） |
| `uniseg3d/unified_criterion.py` | 损失总调度 `ScanNetUnifiedCriterion`，用 `MODELS.build` 组装 5 个子损失 |
| `uniseg3d/openclip_backbone.py` | OpenCLIP 文本塔封装 `OpenCLIPBackboneText`，是进入 `open_clip/` 库的桥梁 |
| `uniseg3d/lang_module.py` | 语言分支 `LangModule`，内部直接实例化 `OpenCLIPBackboneText` |
| `open_clip/__init__.py` / `open_clip/factory.py` | 内嵌 OpenCLIP 库的入口与「模型工厂」，含自己的一套 JSON 配置注册表 |
| `tools/create_data.py` | 数据预处理入口脚本（`tools/` 目录的代表） |
| `configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py` | 主配置：`custom_imports` 是配置与注册机制之间的桥梁 |
| `README.md` | 项目说明，其 L131-L150 给出了 `data/` 处理后的目录结构 |

## 4. 核心概念与源码讲解

本讲拆为 4 个最小模块：**4.1 目录职责总览**、**4.2 uniseg3d 包内模块清单与相互引用**、**4.3 注册机制深潜**、**4.4 open_clip 的定位**。

### 4.1 仓库六大目录职责总览

#### 4.1.1 概念说明

读一个中大型深度学习仓库，第一步不是看代码，而是「分地盘」：先搞清每个目录负责什么，再判断自己关心的功能落在哪一块。UniSeg3D 是基于 MMDetection3D/mmengine 构建的研究项目，它的目录组织遵循一个很典型的模式：**核心代码包 + 内嵌第三方库 + 配置 + 工具脚本 + 数据脚本**。

#### 4.1.2 核心流程

仓库顶层共 6 个目录（外加 README/LICENSE），职责划分如下：

```text
UniSeg3D/
├── README.md        # 安装、数据准备、训练/推理命令、指标表
├── LICENSE          # 许可证
├── uniseg3d/        # ★ 核心代码包：模型、数据管线、损失、评估，全部在此（23 个 py 文件）
├── open_clip/       # ★ 内嵌的 OpenCLIP 库：CLIP 文本编码器（来自 OMG-Seg 的 ext/open_clip）
├── configs/         # 2 份训练配置：1xb4（1 卡 batch 4）与 4xb4（4 卡 batch 4）
├── tools/           # train.py/test.py、dist_train.sh/dist_test.sh、数据预处理脚本
├── data/scannet/    # ScanNet 原始数据解析脚本 + meta_data/（类别映射、场景列表）
└── content/         # 纯静态资源：README 引用的图片与 GIF，不含代码
```

一次训练中，这些目录的关系可以画成一条链：

```text
configs/*.py（写清要什么）
   │  custom_imports = ['uniseg3d']
   ▼
uniseg3d/（提供类并注册）
   │  import open_clip
   ▼
open_clip/（提供 CLIP 文本塔权重加载）
   ▲
   │  读取预处理产物
data/scannet 产物（points/instance_mask/semantic_mask/super_points + infos pkl）
   ▲
   │  batch_load_scannet_data.py + create_data.py 生成
ScanNet 原始场景
```

tools/ 目录的 8 个文件分三组：

| 分组 | 文件 | 说明 |
| --- | --- | --- |
| 训练/推理入口 | `train.py`、`test.py`、`dist_train.sh`、`dist_test.sh` | u1-l2 已精读，不重复 |
| 数据预处理 | `create_data.py`、`indoor_converter.py`、`scannet_data_utils.py` | 生成 `uniseg3d_infos_train/val.pkl`，u1-l4 精读 |
| 备用工具 | `update_infos_to_v2.py` | mmdet3d 官方的 infos 格式升级脚本，本仓库保留但 `create_data.py` 中对它的调用被注释掉了 |

#### 4.1.3 源码精读

数据脚本入口 `tools/create_data.py` 的结构很简单——它把具体工作转交给 `indoor_converter`：

- [tools/create_data.py:L5-L6](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/tools/create_data.py#L5-L6)：从同目录的 `indoor_converter` 导入 `create_indoor_info_file`，说明这些脚本靠「同目录互相 import」组织，而非一个正式的包。
- [tools/create_data.py:L18-L19](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/tools/create_data.py#L18-L19)：`scannet_data_prep` 的核心只有一行——调用 `create_indoor_info_file` 生成 infos；L20-L25 是被注释掉的 `update_pkl_infos` 调用，佐证了「v2 升级脚本保留未用」。
- [tools/create_data.py:L46-L48](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/tools/create_data.py#L46-L48)：`main` 中先 `register_all_modules()` 注册 mmdet3d 全部内置模块，再支持 `scannet`/`scannet200` 两个数据集名——这行代码是「注册机制无处不在」的又一例证。

`data/scannet/` 中处理完的数据长什么样，README 给出了权威描述：

- [README.md:L131-L150](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/README.md#L131-L150)：`points/`、`instance_mask/`、`semantic_mask/`、`super_points/` 四个 `.bin` 目录，加上 `uniseg3d_infos_*.pkl` 和 `scannet_cls_embedding.pth`。本讲只需记住「四大 bin + infos pkl」，细节留给 u1-l4。

`content/` 目录 4 个文件（`introduction.png`、`comparison.png`、`visualization.png`、`MultiSegVisual-480P.gif`）全是 README 引用的演示素材，阅读源码时可完全忽略。

#### 4.1.4 代码实践

1. **实践目标**：亲手建立目录树第一印象，并与 README 的数据目录说明对照。
2. **操作步骤**：
   ```bash
   cd <仓库根目录>
   ls -la                       # 顶层目录
   ls uniseg3d/                 # 23 个 py 文件
   ls tools/ data/scannet/ data/scannet/meta_data/ configs/ content/
   ```
3. **需要观察的现象**：`uniseg3d/` 下有 23 个 `.py` 文件；`data/scannet/meta_data/` 里有 `scannet_train.txt`（场景 ID 列表）、`scannetv2-labels.combined.tsv`（原始类别总表）、`scannet_means.npz`；`content/` 只有图片/GIF。
4. **预期结果**：你能在不看讲义的情况下说出六大目录的职责，并能对照 README L131-L150 指出「处理后数据会出现哪些子目录」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `content/` 目录值得单独列出却又最不重要？
**答案**：它只存放 README 引用的静态图片资源，不含任何代码；列出它是为了告诉初学者「这个目录可以跳过」，避免在演示 GIF 上浪费时间。

**练习 2**：`tools/create_data.py` 里 `from indoor_converter import create_indoor_info_file` 为什么不带路径前缀也能成功？
**答案**：`create_data.py` 与 `indoor_converter.py` 在同一目录（`tools/`），直接 `python tools/create_data.py` 运行时，脚本所在目录会加入 `sys.path`，因此同级模块可以直接按名字导入。

### 4.2 uniseg3d 包内模块清单与相互引用

#### 4.2.1 概念说明

`uniseg3d/` 是唯一的核心代码包。它内部再按「职责」分成四类：**模型**（前向计算）、**数据管线**（把磁盘文件变成张量）、**损失**（训练监督）、**评估**（指标计算）。建立这四类的心智分区后，任何功能需求都能秒级定位到文件。

还有一个重要的架构事实：**模块之间存在两种连接方式**——

1. **Python import 连接**：如 `uniseg3d.py` 导入 `mask_matrix_nms` 函数，是硬编码依赖。
2. **配置字符串连接**：如主模型与五个子损失之间**没有任何 import 关系**，完全靠 config 里的 `type='XXX'` 字符串在运行时经 `MODELS.build` 组装。这是 mmengine 体系「配置即代码」的核心体现，也是研究代码方便替换组件的关键设计。

#### 4.2.2 核心流程

`uniseg3d/` 的 22 个文件按四类分层（加粗者为该类「入口」）：

```text
【模型层】
uniseg3d.py ── UniSeg3D（总装：backbone/decoder/lang/criterion 四件套 + 训练 loss() + 推理 predict()）
 ├── spconv_unet.py ─── SpConvUNet（稀疏卷积 U-Net 骨干）
 ├── query_decoder.py ─ UnifiedQueryDecoder（继承同文件的 QueryDecoder 基类）
 ├── lang_module.py ─── LangModule
 │    └── openclip_backbone.py ── OpenCLIPBackboneText ──▶ open_clip/（本地库）
 ├── mask_matrix_nms.py ── mask_matrix_nms()（未注册的普通函数，推理 NMS 用）
 ├── mask_pool.py ────── mask_pool()（未注册，掩码特征池化）
 └── data_preprocessor.py ─ Det3DDataPreprocessor_（批数据预处理）

【数据管线层】
scannet_dataset.py ── ScanNetUnifiedSegDataset（继承 mmdet3d 的 ScanNetSegDataset）
loading.py ────────── LoadAnnotations3D_ / NormalizePointsColor_
transforms_3d.py ──── 6 个变换（超点标注、点/文本提示生成、弹性变换）
formatting.py ─────── Pack3DDetInputs_（打包成 Det3DDataSample）
structures.py ─────── InstanceData_（未注册的数据结构，被 6 个文件 import）

【损失层】
unified_criterion.py ── ScanNetUnifiedCriterion（总调度，build 五个子损失）
 ├── semantic_criterion.py ───────── ScanNetSemanticCriterion
 ├── instance_criterion.py ───────── OVInstanceCriterion + SparseMatcher + 3 种 Cost
 ├── point_prompt_instance_criterion.py ─ PointPromptInstanceCriterion
 ├── text_prompt_instance_criterion.py ─ TextPromptInstanceCriterion
 └── contrastive_criterion.py ────── ContrastiveCriterion

【评估层】
unified_metric.py ── PromptSupportedUnifiedSegMetric（继承同文件 PromptSupportedSegMetric）
 ├── instance_seg_eval.py ──────── instance_seg_eval()（mAP）
 └── evaluate_semantic_instance.py ── scannet_eval()（ScanNet 官方评估协议）
```

注意损失层的五条「├──」线不是 Python import，而是运行时 `MODELS.build` 的组装关系——下一小节用源码证明。

#### 4.2.3 源码精读

先看「总装车间」如何用注册器组装子模块（模型层的连接方式）：

- [uniseg3d/uniseg3d.py:L259-L260](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L259-L260)：`@MODELS.register_module()` 加在 `class UniSeg3D(UniSeg3DMixin, Base3DDetector)` 上方——主模型本身也是注册进 `MODELS` 的，config 的 `type='UniSeg3D'` 就指向这里。
- [uniseg3d/uniseg3d.py:L311-L314](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L311-L314)：构造函数里连续四次 `MODELS.build(...)`，分别把 config 中的 `backbone`、`decoder`、`lang`、`criterion` 四个字典变成对象。`UniSeg3D` 与 `SpConvUNet`、`UnifiedQueryDecoder`、`LangModule`、`ScanNetUnifiedCriterion` 之间**一行 import 都没有**，全靠注册器牵手。

再看损失层同款套路（这就是「配置即代码」在损失体系的复刻）：

- [uniseg3d/unified_criterion.py:L6-L16](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_criterion.py#L6-L16)：`ScanNetUnifiedCriterion` 的构造函数接收 5 个子损失配置字典，L12-L16 逐个 `MODELS.build` 实例化。它的文件头部只 import 了 `MODELS` 和 `InstanceData_`，**没有 import 任何子损失类**——把 config 里的 `sem_criterion.type` 从 `ScanNetSemanticCriterion` 换成别的类，不需要改这行代码。
- [uniseg3d/unified_criterion.py:L65-L74](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_criterion.py#L65-L74)：五个子损失被依次调用的顺序（实例 → 语义 → 点提示 → 文本提示 → 对比），u5-l1 会展开，这里只需看到「总调度」的角色。

数据管线层的继承改造模式（mmdet3d 同名类 + 下划线后缀）：

- [uniseg3d/scannet_dataset.py:L10-L33](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/scannet_dataset.py#L10-L33)：`ScanNetUnifiedSegDataset` 继承 mmdet3d 的 `ScanNetSegDataset`，全部改动只有两处——`parse_data_info` 里给 info 拼上 `super_pts_path`（超点掩码路径），以及重写 `get_scene_idxs`。这是本仓库典型的「最小改造」风格：能继承就不重写。
- [uniseg3d/loading.py:L11-L12](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/loading.py#L11-L12)：`LoadAnnotations3D_`（注意结尾下划线）继承 mmdet3d 的 `LoadAnnotations3D`，文件首行注释 `# Adapted from mmdet3d/datasets/transforms/loading.py` 直白说明了来源。**结尾下划线是本仓库区分「改造版」与「原版」的命名约定**（`Pack3DDetInputs_`、`Det3DDataPreprocessor_`、`NormalizePointsColor_`、`InstanceData_` 同理）。

评估层的两级继承：

- [uniseg3d/unified_metric.py:L24-L25](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_metric.py#L24-L25) 与 [uniseg3d/unified_metric.py:L79-L80](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_metric.py#L79-L80)：同文件注册了两个指标类——`PromptSupportedSegMetric`（继承 mmdet3d 的 `SegMetric`）和 `PromptSupportedUnifiedSegMetric`（继承前者）。config 实际使用的是后者。

#### 4.2.4 代码实践

1. **实践目标**：用工具验证「损失层五类之间没有 import 依赖」这一论断。
2. **操作步骤**：
   ```bash
   # 查看包内所有相对导入（.xxx 开头的 import）
   grep -rn "^from \." uniseg3d/
   # 单独检查 unified_criterion.py 是否 import 了任何子损失
   grep -n "import" uniseg3d/unified_criterion.py
   ```
3. **需要观察的现象**：第一条命令会显示 `uniseg3d.py` 导入了 `.mask_matrix_nms`、`.structures`，`lang_module.py` 导入了 `.openclip_backbone`，`unified_metric.py` 导入了 `.instance_seg_eval` 等；第二条命令中 `unified_criterion.py` 只 import 了 `MODELS`、`InstanceData_`、`torch.nn` 三项。
4. **预期结果**：确认 `ScanNetUnifiedCriterion` 与五个子损失之间只存在 config 字符串连接。同理可验证 `UniSeg3D` 与 `SpConvUNet` 等也无 import 关系。

#### 4.2.5 小练习与答案

**练习 1**：想修改「实例分割的匹配代价函数（如把 Dice 换成别的）」，应该打开哪个文件？
**答案**：`uniseg3d/instance_criterion.py`。匹配器 `SparseMatcher` 与三种代价类（`QueryClassificationCost`/`MaskBCECost`/`MaskDiceCost`）都注册在此文件 L646-L727 区间，同时 config 中 `criterion.inst_criterion.matcher.costs` 列表决定启用哪些代价及权重，可能还需配合修改配置。

**练习 2**：为什么 `structures.py` 里的 `InstanceData_` 没有 `register_module()` 却被 6 个文件 import？
**答案**：它是一个纯数据结构（继承 mmdet3d 的 `InstanceData`），只在 Python 代码内部传递标注数据，不需要被配置文件按名字实例化，因此无需注册；注册是为「配置字符串 → 类」的反射查找服务的。

**练习 3**：`mask_pool` 函数同时出现在 `uniseg3d/mask_pool.py` 和 `uniseg3d/query_decoder.py`（后者 L11-L27 有一份同名实现）。这类「未注册工具函数」如何被使用？
**答案**：通过普通 Python import 使用（谁需要谁 `from .mask_pool import mask_pool`），不经过注册表。研究代码中这类小工具常有重复定义，阅读时注意以实际被调用的那份为准。

### 4.3 注册机制深潜：`__init__.py` 与 29 个 `register_module()`

#### 4.3.1 概念说明

u1-l1 说过「配置中 `type='XXX'` 字符串据此实例化为类」，本讲补上最关键的一环：**这些类是何时、被谁注册的？**答案是：mmengine 在加载配置后执行 `custom_imports`，即 `import uniseg3d`；Python 执行包的 `__init__.py`；其中的每一行 `from .xxx import Yyy` 都会完整执行 `xxx.py`；文件里的 `@XXX.register_module()` 装饰器随之运行，把类写入注册表。整条链只依赖「import 的副作用」，不需要任何显式的注册调用。

一个容易误解的点：**注册的粒度是「模块（.py 文件）」而不是「名字」**。`__init__.py` 只写 `from .transforms_3d import ElasticTransfrom, AddSuperPointAnnotations`，但 `transforms_3d.py` 里另外 4 个类（`TextPromptGeneration` 等）同样会被注册——因为整个文件被执行了。

#### 4.3.2 核心流程

```text
configs/uniseg3d_1xb4_*.py 第 5 行:  custom_imports = dict(imports=['uniseg3d'])
        │
        ▼  mmengine 解析配置后触发
import uniseg3d  →  执行 uniseg3d/__init__.py（19 行 import）
        │
        ▼  每行 from .xxx import Yyy 都完整执行 xxx.py
文件内全部 @REGISTRY.register_module() 运行
        │
        ▼
mmdet3d 五个注册表被填充（MODELS 13 / TRANSFORMS 9 / DATASETS 1 / METRICS 2 / TASK_UTILS 4，共 29 个）
        │
        ▼
Runner 组装阶段 MODELS.build / TRANSFORMS.build / ... 按字符串取类实例化
```

#### 4.3.3 源码精读

先看触发源头——配置文件的第 5 行：

- [configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:L5](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L5)：`custom_imports = dict(imports=['uniseg3d'])`。没有这一行，后面所有 `type='UniSeg3D'`、`type='SpConvUNet'` 都会报「KeyError: 'UniSeg3D is not in the MODELS registry'」之类的错误。

再看注册入口的全貌：

- [uniseg3d/__init__.py:L1-L19](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/__init__.py#L1-L19)：整个文件就是 19 行 import，一行一个模块。把这 19 行与 4.2 节的四类清单对照，会发现它就是「核心包的目录页」。特别注意 L3 导入了 `QueryDecoder` 和 `UnifiedQueryDecoder` 两个名字，L10 只从 `transforms_3d` 导入了 `ElasticTransfrom, AddSuperPointAnnotations` 两个名字——但如上所述，注册范围不受此限制。

「模块级注册」的直接证据在 transforms：

- [uniseg3d/transforms_3d.py:L12-L13](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L12-L13) 起的 6 处 `@TRANSFORMS.register_module()`（行号 12/63/112/167/237/312）：`TextPromptGeneration`、`TextPromptTest`、`PointPromptGeneration`、`PointPromptTest`、`ElasticTransfrom`、`AddSuperPointAnnotations` 全部注册。而 config 的 train_pipeline（L177、L183）确实以 `type='TextPromptGeneration'`、`type='PointPromptGeneration'` 引用了其中不在 `__init__.py` 导入清单里的类——如果注册只看 `__init__.py` 的名字，这里早就报错了。

注册表的来源（都是 mmdet3d 汇集的注册器）：

- [uniseg3d/loading.py:L8](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/loading.py#L8)：`from mmdet3d.registry import TRANSFORMS`。五个注册表全部从 `mmdet3d.registry` 导入（`MODELS`/`TRANSFORMS`/`DATASETS`/`METRICS`/`TASK_UTILS`），它们与 mmdet3d 内置类共用同一张表——所以 config 里 `LoadPointsFromFile`（mmdet3d 内置）和 `LoadAnnotations3D_`（本仓库）能混用在同一条 pipeline 里。

最后是完整注册清单（29 项，行号均已核对）。**config 引用列**指 1xb4 配置中是否出现 `type='类名'`：

**MODELS（13 个）**

| 类名 | 注册位置 | config 引用 | 说明 |
| --- | --- | --- | --- |
| `UniSeg3D` | [uniseg3d/uniseg3d.py:L259](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L259) | ✅ `model.type` | 主模型 |
| `SpConvUNet` | [uniseg3d/spconv_unet.py:L94](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/spconv_unet.py#L94) | ✅ `backbone.type` | 稀疏骨干 |
| `QueryDecoder` | [uniseg3d/query_decoder.py:L162](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/query_decoder.py#L162) | ❌ | 仅作 `UnifiedQueryDecoder` 的基类被继承 |
| `UnifiedQueryDecoder` | [uniseg3d/query_decoder.py:L372](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/query_decoder.py#L372) | ✅ `decoder.type` | 实际使用的解码器 |
| `LangModule` | [uniseg3d/lang_module.py:L38](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/lang_module.py#L38) | ✅ `lang.type` | 语言分支 |
| `OpenCLIPBackboneText` | [uniseg3d/openclip_backbone.py:L17](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/openclip_backbone.py#L17) | ❌ | 由 `LangModule` 在 [lang_module.py:L51](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/lang_module.py#L51) 直接 Python 实例化 |
| `ScanNetUnifiedCriterion` | [uniseg3d/unified_criterion.py:L6](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_criterion.py#L6) | ✅ `criterion.type` | 损失总调度 |
| `ScanNetSemanticCriterion` | [uniseg3d/semantic_criterion.py:L7](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/semantic_criterion.py#L7) | ✅ `sem_criterion.type` | 语义损失 |
| `OVInstanceCriterion` | [uniseg3d/instance_criterion.py:L85](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L85) | ✅ `inst_criterion.type` | 开放词汇实例损失 |
| `PointPromptInstanceCriterion` | [uniseg3d/point_prompt_instance_criterion.py:L48](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/point_prompt_instance_criterion.py#L48) | ✅ | 点提示损失 |
| `TextPromptInstanceCriterion` | [uniseg3d/text_prompt_instance_criterion.py:L48](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/text_prompt_instance_criterion.py#L48) | ✅ | 文本提示损失 |
| `ContrastiveCriterion` | [uniseg3d/contrastive_criterion.py:L11](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/contrastive_criterion.py#L11) | ✅ | 跨任务对比损失 |
| `Det3DDataPreprocessor_` | [uniseg3d/data_preprocessor.py:L7](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/data_preprocessor.py#L7) | ✅ `data_preprocessor.type` | 批预处理 |

**TRANSFORMS（9 个）**

| 类名 | 注册位置 | config 引用 | 说明 |
| --- | --- | --- | --- |
| `LoadAnnotations3D_` | [uniseg3d/loading.py:L11](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/loading.py#L11) | ✅ | 加载掩码 + 超点掩码 |
| `NormalizePointsColor_` | [uniseg3d/loading.py:L70](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/loading.py#L70) | ✅ | 颜色归一化 |
| `Pack3DDetInputs_` | [uniseg3d/formatting.py:L11](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/formatting.py#L11) | ✅ | 打包数据样本 |
| `TextPromptGeneration` | [uniseg3d/transforms_3d.py:L12](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L12) | ✅ train_pipeline | 文本提示（训练） |
| `TextPromptTest` | [uniseg3d/transforms_3d.py:L63](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L63) | ✅ test_pipeline | 文本提示（测试） |
| `PointPromptGeneration` | [uniseg3d/transforms_3d.py:L112](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L112) | ✅ train_pipeline | 点提示（训练） |
| `PointPromptTest` | [uniseg3d/transforms_3d.py:L167](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L167) | ✅ test_pipeline | 点提示（测试） |
| `ElasticTransfrom` | [uniseg3d/transforms_3d.py:L237](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L237) | ✅ train_pipeline | 弹性形变增广（注意类名拼写 Transfrom） |
| `AddSuperPointAnnotations` | [uniseg3d/transforms_3d.py:L312](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L312) | ✅ 双 pipeline | 超点标注构造 |

**DATASETS（1 个）**

| 类名 | 注册位置 | config 引用 |
| --- | --- | --- |
| `ScanNetUnifiedSegDataset` | [uniseg3d/scannet_dataset.py:L10](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/scannet_dataset.py#L10) | ✅ `dataset.type` |

**METRICS（2 个）**

| 类名 | 注册位置 | config 引用 | 说明 |
| --- | --- | --- | --- |
| `PromptSupportedSegMetric` | [uniseg3d/unified_metric.py:L24](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_metric.py#L24) | ❌ | 基类 |
| `PromptSupportedUnifiedSegMetric` | [uniseg3d/unified_metric.py:L79](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_metric.py#L79) | ✅ `val_evaluator.type` | 实际使用 |

**TASK_UTILS（4 个）**

| 类名 | 注册位置 | config 引用 |
| --- | --- | --- |
| `QueryClassificationCost` | [uniseg3d/instance_criterion.py:L646](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L646) | ✅ `costs` 列表 |
| `MaskBCECost` | [uniseg3d/instance_criterion.py:L673](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L673) | ✅ `costs` 列表 |
| `MaskDiceCost` | [uniseg3d/instance_criterion.py:L700](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L700) | ✅ `costs` 列表 |
| `SparseMatcher` | [uniseg3d/instance_criterion.py:L726](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/instance_criterion.py#L726) | ✅ `matcher.type` / `ov_matcher.type` |

统计结论：29 个注册项中 26 个被 config 以 `type=` 直接引用；不被引用的只有 3 个——`QueryDecoder`、`PromptSupportedSegMetric`（均为只被继承的基类）和 `OpenCLIPBackboneText`（被 `LangModule` 直接实例化）。

#### 4.3.4 代码实践（本讲主实践）

1. **实践目标**：亲手统计全部 `register_module()`，按五个注册器分类，标注文件行号，并与 config 引用对照。
2. **操作步骤**：
   ```bash
   cd <仓库根目录>

   # 步骤 1：统计总数（应为 29）
   grep -rn "register_module()" uniseg3d/ | wc -l

   # 步骤 2：列出每一处注册，带文件与行号
   grep -rn "register_module()" uniseg3d/

   # 步骤 3：配合上下文看清注册器类型与类名（-A 1 显示装饰器的下一行，即 class 定义行）
   grep -rn -A 1 "register_module()" uniseg3d/ | grep -E "register_module|^uniseg3d.*class "

   # 步骤 4：提取 config 中全部 type 引用，与注册清单对照
   grep -oE "type='[A-Za-z_0-9]+'|type=\"[A-Za-z_0-9]+\"" configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py | sort -u
   ```
3. **需要观察的现象**：步骤 2 的输出分布在 17 个文件中，注册最多的是 `transforms_3d.py`（6 处）和 `instance_criterion.py`（5 处）；步骤 4 的 type 列表里既有本仓库类（如 `AddSuperPointAnnotations`），也有 mmdet3d 内置类（如 `LoadPointsFromFile`、`PointSegClassMapping`、`GlobalRotScaleTrans`、`MultiScaleFlipAug3D`——它们在本仓库中搜不到注册来源，因为注册发生在 mmdet3d 包内部）。
4. **预期结果**：整理出与 4.3.3 节五张表一致的清单（MODELS 13 / TRANSFORMS 9 / DATASETS 1 / METRICS 2 / TASK_UTILS 4），并确认 3 个不被 config 引用的类是 `QueryDecoder`、`PromptSupportedSegMetric`、`OpenCLIPBackboneText`。
5. **进阶（需按 u1-l2 装好环境，待本地验证）**：在 Python 里直接打印注册表，与 grep 结果互相印证：
   ```bash
   python -c "
   from mmdet3d.utils import register_all_modules
   register_all_modules()
   import uniseg3d                       # 触发注册
   from mmdet3d.registry import MODELS, TRANSFORMS, DATASETS, METRICS, TASK_UTILS
   mine = lambda reg: sorted(k for k, v in reg.module_dict.items() if 'uniseg3d' in str(getattr(v, '__module__', '')))
   for name, reg in [('MODELS', MODELS), ('TRANSFORMS', TRANSFORMS), ('DATASETS', DATASETS), ('METRICS', METRICS), ('TASK_UTILS', TASK_UTILS)]:
       print(name, mine(reg))
   "
   ```
   该脚本通过类的 `__module__` 属性过滤出「定义在 uniseg3d 包里」的注册项。若环境尚未配好，此步标记为「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：假设你在 `uniseg3d/` 下新建了 `my_loss.py` 并写了 `@MODELS.register_module()` 的类，也在 config 里写了 `type='MyLoss'`，但忘了改 `__init__.py`。会发生什么？为什么？
**答案**：会报「类不在 MODELS 注册表」的错误。因为没人 import `my_loss.py`，装饰器从未执行，类没进注册表。修复方式：在 `__init__.py` 加一行 `from .my_loss import MyLoss`（或在 config 的 `custom_imports.imports` 里追加模块名）。

**练习 2**：`__init__.py` 第 10 行只从 `transforms_3d` 导入了 2 个类，为什么 config 里的 `type='TextPromptGeneration'` 仍能正常构建？
**答案**：`from .transforms_3d import ElasticTransfrom, AddSuperPointAnnotations` 会执行整个 `transforms_3d.py` 模块，文件内 6 个 `@TRANSFORMS.register_module()` 全部运行。注册的触发单位是「模块」，不是「导入的名字」。

**练习 3**：`tools/create_data.py` 第 48 行的 `register_all_modules()` 与本讲的 `custom_imports` 有何异同？
**答案**：两者都通过 import 副作用填充注册表。区别：`register_all_modules()` 由 mmdet3d 提供，注册的是 mmdet3d 自带全部模块（供数据处理脚本用）；`custom_imports = dict(imports=['uniseg3d'])` 由 mmengine 在 Runner 启动时解析，注册的是本仓库自定义模块（供训练/测试用）。

### 4.4 open_clip：内嵌第三方库的定位与导入链

#### 4.4.1 概念说明

`open_clip/` 是一份**整体拷贝进仓库的第三方库**（README 的数据准备说明注明其来源是 OMG-Seg 项目的 `ext/open_clip`，后者又源自 mlfoundations/OpenCLIP）。UniSeg3D 的指代分割与开放词汇能力都需要一个 CLIP 文本编码器，而作者需要的 `convnext_large_d_320` 模型构建逻辑与官方 pip 包版本未必一致，直接把库拷进仓库可以**锁死版本、离线可用、允许小改动**——代价是仓库变大、升级需手动同步。

它有两个容易混淆的「注册表」，务必区分：

- mmengine 的 Registry（4.3 节）：字符串 → 类，服务 config 组装。
- open_clip 自己的模型配置注册表：模型名（如 `convnext_large_d_320`）→ JSON 结构描述，服务 `create_model` 工厂。

#### 4.4.2 核心流程

从 config 到 CLIP 文本塔的完整导入链：

```text
configs（lang.type='LangModule', model_name='convnext_large_d_320'）
  ▼ MODELS.build
uniseg3d/lang_module.py : LangModule.__init__
  ▼ 直接 Python 实例化（L51）
uniseg3d/openclip_backbone.py : OpenCLIPBackboneText.__init__
  ▼ import open_clip（L14）
open_clip/__init__.py（本地目录，因根目录在 PYTHONPATH）
  ▼ from .factory import ...
open_clip/factory.py : create_model_from_pretrained(model_name, pretrained)
  ▼ 查 _MODEL_CONFIGS（启动时扫描 model_configs/*.json 建立）
open_clip/model_configs/convnext_large_d_320.json + work_dirs/pretrained/.../open_clip_pytorch_model.bin
  ▼
CLIP 模型 → OpenCLIPBackboneText 只摘取文本塔六件套
```

#### 4.4.3 源码精读

- [uniseg3d/openclip_backbone.py:L14](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/openclip_backbone.py#L14)：`import open_clip as open_clip`。由于 u1-l2 讲过 `dist_train.sh`/`dist_test.sh` 会把仓库根目录加入 `PYTHONPATH`，Python 会优先在本地 `open_clip/` 目录中找到这个包（README 的安装清单中也未 pip 安装 open_clip，本地目录是唯一来源）。
- [uniseg3d/openclip_backbone.py:L40-L51](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/openclip_backbone.py#L40-L51)：调用 `open_clip.create_model_from_pretrained(...)` 得到完整 CLIP 模型后，只把**文本塔**的六个组件摘出来挂在 self 上：`text_tokenizer`、`text_transformer`、`text_token_embedding`、`text_pe`（位置编码）、`text_ln_final`、`text_proj`。图像塔直接被丢弃——UniSeg3D 只用 CLIP 读文本。
- [open_clip/__init__.py:L3-L4](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/open_clip/__init__.py#L3-L4)：包入口从 `factory` 导出 `create_model`、`create_model_from_pretrained`、`get_tokenizer` 等工厂函数——这就是上一条代码能 `open_clip.create_model_from_pretrained` 的原因。
- [open_clip/factory.py:L24-L26](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/open_clip/factory.py#L24-L26)：`_MODEL_CONFIG_PATHS = [Path(__file__).parent / "model_configs/"]`——open_clip 自己那套注册表的搜索路径，指向同目录的 `model_configs/`（内含 40 余个 JSON，包括本仓库使用的 `convnext_large_d_320.json`）。
- [open_clip/factory.py:L33-L54](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/open_clip/factory.py#L33-L54)：`_rescan_model_configs()` 扫描所有 JSON、校验必需字段后填入 `_MODEL_CONFIGS` 字典，并在**模块导入时**（L54）立即执行一次——注意这与 mmengine Registry 的「import 副作用注册」思路如出一辙，只是载体从装饰器换成了字典。
- [open_clip/factory.py:L77-L86](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/open_clip/factory.py#L77-L86)：`get_tokenizer(model_name)` 按模型名返回分词器，被 `OpenCLIPBackboneText.__init__` 的 L44 调用。
- [open_clip/factory.py:L346](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/open_clip/factory.py#L346)：`create_model_from_pretrained` 的函数定义行——按模型名查 `_MODEL_CONFIGS` 构建网络并加载预训练权重，是 open_clip 侧最核心的工厂入口（u4-l5 会展开其内部）。

#### 4.4.4 代码实践

1. **实践目标**：验证 `import open_clip` 确实解析到仓库内嵌目录而非 pip 包，并熟悉 open_clip 的模型名注册表。
2. **操作步骤**：
   ```bash
   cd <仓库根目录>
   python -c "import open_clip; print(open_clip.__file__)"
   grep -rn "open_clip" uniseg3d/*.py          # 找出所有引用点
   grep -l "convnext" open_clip/model_configs/*.json   # 有哪些 convnext 变体
   ```
   说明：在仓库根目录下运行 `python -c` 时，当前目录位于 `sys.path` 首位，效果等同于 dist 脚本注入 `PYTHONPATH`，本地 `open_clip/` 会优先于任何 pip 安装的同名包被找到。
3. **需要观察的现象**：第一条命令应打印 `<仓库根>/open_clip/__init__.py`（若你环境里恰好 pip 装过 open_clip，则能直观看到 PYTHONPATH 优先级的效果）；第二条命令只有 `uniseg3d/openclip_backbone.py` 一处导入（其余是 `lang_module.py` 经由它间接使用）；第三条命令列出包括 `convnext_large_d_320.json` 在内的多个变体文件。
4. **预期结果**：确认「本地目录是唯一/优先的 open_clip 来源」，并且 `model_name='convnext_large_d_320'` 能在 `open_clip/model_configs/` 中找到同名 JSON。若你的环境未安装 Python 依赖（第一步报 `ModuleNotFoundError: No module named 'timm'` 等），标记为「待本地验证」——`openclip_backbone.py` 依赖 `timm.layers`，完整验证需按 u1-l2 完成环境搭建。

#### 4.4.5 小练习与答案

**练习 1**：为什么不把 open_clip 通过 `pip install open_clip` 安装，而要拷贝整个库进仓库？
**答案**：①版本锁定——研究代码需要保证构建逻辑与预训练权重（`convnext_large_d_320` 的 `open_clip_pytorch_model.bin`）严格匹配；②允许本地修改而不影响其他项目；③来源是 OMG-Seg 的定制分支，与官方发布版不完全一致。

**练习 2**：`OpenCLIPBackboneText` 注册进了 `MODELS`，但 config 里找不到 `type='OpenCLIPBackboneText'`，它如何被创建？
**答案**：由 `LangModule.__init__`（[uniseg3d/lang_module.py:L51](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/lang_module.py#L51)）直接 `OpenCLIPBackboneText(model_name=model_name, init_cfg=init_cfg)` 实例化。注册装饰器在这里是「备而未用」——保留了将来在 config 中单独配置它的可能。

**练习 3**：open_clip 的 `_MODEL_CONFIGS` 与 mmengine 的 `MODELS` 注册表解决的是同一个问题吗？
**答案**：不是。前者是「模型架构名 → 结构超参 JSON」的查找表，服务于 `create_model` 工厂按名字搭网络；后者是「类名字符串 → Python 类」，服务于 mmengine 按配置实例化任意组件。两者都靠 import 副作用初始化，但层次不同。

## 5. 综合实践

**任务：为仓库制作一张属于你自己的「代码地图」笔记。**

把本讲四个模块的产出整合成一份 `UniSeg3D-tutorial/my-code-map.md`（个人笔记，可不遵循讲义格式），包含四部分：

1. **目录职责表**：六大目录各一行职责说明（用你自己的话，不要照抄讲义）。
2. **四类模块速查表**：模型 / 数据管线 / 损失 / 评估四类，每类列出「文件 → 核心类/函数 → 一句话职责」，并标注哪些类有结尾下划线（改造版）。
3. **注册清单**：完成 4.3.4 的 grep 统计，给出 29 项的五张分类表（可精简为「注册器 → 类名@文件:行」的紧凑格式），单独标出 3 个不被 config 引用的类。
4. **三个导航问答**，检验地图是否真的可用：
   - 「我想调整训练时点提示的采样数量」→ 应改 config 的 `PointPromptGeneration` 参数还是 `uniseg3d/transforms_3d.py`？
   - 「我想新增一种匹配代价」→ 需要动哪几个文件？（提示：新类 + `__init__.py` + config 的 `costs` 列表）
   - 「CLIP 文本编码的维度是在哪里从 768 变成 32 的？」→ 沿导入链指出文件与字段。

完成后自测：合上笔记，仅凭记忆回答「语义分割损失在哪个文件」「六任务统一评估器叫什么名字」。答得上来，本讲目标达成。

参考答案要点：①点提示采样数量 `samplePoint`/`max_num_point` 在 config 中配置（train 用 `PointPromptGeneration`、test 用 `PointPromptTest`），采样策略本身在 `uniseg3d/transforms_3d.py`；②在 `uniseg3d/instance_criterion.py` 加新 Cost 类（仿 `MaskBCECost`，L673 起）+ 在 `uniseg3d/__init__.py` 补 import + 在 config `criterion.inst_criterion.matcher.costs` 加 `dict(type=..., weight=...)`；③`LangModule`（`uniseg3d/lang_module.py`）内 `Mlp` 把 `text_proj` 的 768 维投影到 `out_features=32`（config L39-L46 设置）。

## 6. 本讲小结

- 仓库六大目录分工：`uniseg3d/` 是唯一核心代码包，`open_clip/` 是锁版本的内嵌第三方库，`configs/` 提供组装说明书，`tools/` 是入口与数据脚本，`data/scannet/` 是原始数据解析，`content/` 是纯图片资源。
- `uniseg3d/` 内部按模型 / 数据管线 / 损失 / 评估四类组织；「模型 ↔ 子模块」「总损失 ↔ 五个子损失」之间是**配置字符串连接**（`MODELS.build`），不是 Python import——这是 mmengine 体系「配置即代码」的关键。
- 注册机制全链路：config L5 的 `custom_imports` → `import uniseg3d` → 执行 `__init__.py` 的 19 行 import → 各 `.py` 文件内 29 个 `@register_module()`（MODELS 13 / TRANSFORMS 9 / DATASETS 1 / METRICS 2 / TASK_UTILS 4）填充注册表。
- 注册的触发单位是**模块**而非名字：`transforms_3d.py` 的 6 个类全部被注册，尽管 `__init__.py` 只显式导入了其中 2 个。
- 29 个注册项中 26 个被 config 引用；`QueryDecoder`、`PromptSupportedSegMetric` 是仅供继承的基类，`OpenCLIPBackboneText` 被 `LangModule` 直接实例化。
- `import open_clip` 解析到仓库内嵌目录（dist 脚本注入 PYTHONPATH 所致）；open_clip 自带一套「模型名 → JSON」注册表（`factory.py` 的 `_MODEL_CONFIGS`），与 mmengine Registry 是两套机制。
- 命名约定：结尾下划线（`LoadAnnotations3D_` 等）表示「继承 mmdet3d 同名类的改造版」。

## 7. 下一步学习建议

本讲只建立了「地图」，尚未进入任何一个模块的内部。建议按以下顺序推进：

1. **下一讲 u1-l4（数据准备）**：沿着本讲认识的 `data/scannet/` 三个脚本和 `tools/create_data.py`，把 ScanNet 原始场景变成可训练的四大 bin 文件与 infos pkl，为后续所有实践准备数据。
2. **单元 2（注册器与配置深读）**：本讲只讲了「注册发生」，u2-l1 会讲 Runner 如何在生命周期中使用这些注册表；u2-l2 逐段精读 `configs/uniseg3d_1xb4_*.py`，把本讲的注册清单与 config 的每个 `type=` 一一对应。
3. **带着地图读源码的习惯**：之后阅读任何讲义（如 u4 的 `query_decoder.py`、u5 的 `instance_criterion.py`）时，先回到本讲的速查表定位该文件属于哪一类、被谁 build，再进入算法细节，理解会顺畅得多。
