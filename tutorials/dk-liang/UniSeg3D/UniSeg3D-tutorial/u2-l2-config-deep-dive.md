# 精读主配置：模型、数据管线与训练策略

> 本讲对应的配置文件：
> [configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py)
> 与 [configs/uniseg3d_4xb4_scannet_scanrefer_scannet200_unified.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_4xb4_scannet_scanrefer_scannet200_unified.py)

## 1. 本讲目标

学完本讲，你应该能够：

1. 把主配置里的每一个 `type='XXX'` 字符串对应到 `uniseg3d/` 包中的真实源码类，知道配置键就是这些类的 `__init__` 参数。
2. 说出 **model 五件套**（`data_preprocessor` / `backbone` / `decoder` / `lang` / `criterion`）各自实例化成什么对象、负责什么职责。
3. 逐项解释 `test_cfg` 中 `topk_insts`、`sp_score_thr`、`npoint_thr` 等推理阈值在源码中的落点与作用。
4. 对照 `train_pipeline` 与 `test_pipeline` 的每一项，解释两者为什么不同。
5. 用 `diff` 对比 1xb4 与 4xb4 两份配置，解释全部三处差异（学习率、重复行、`env_cfg`）的原因。
6. 会用 `--cfg-options` 在命令行临时覆盖配置项（如 `batch_size`、`max_epochs`），并从日志验证覆盖生效。

## 2. 前置知识

在精读配置前，先建立三个认知（承接 [u2-l1 注册器机制与 Runner 生命周期]）：

**（1）配置文件就是一段普通的 Python 代码。**
它不是 JSON/YAML，而是可以被 `exec` 执行的 `.py` 文件。所以配置里可以写变量、算术、列表推导。例如主配置第 9 行定义 `num_channels = 32`，第 37 行直接用列表推导生成骨干网络通道数：

```python
num_planes=[num_channels * (i + 1) for i in range(5)]  # 即 [32, 64, 96, 128, 160]
```

**（2）`_base_` 继承 + 递归合并。**
配置开头的 `_base_` 列表声明「父配置」。mmengine 的 `Config` 按「dict 递归深合并、list 与普通变量整体替换」的规则把父子配置合成一个大字典：父配置里 `train_dataloader.sampler` 这类**未被本配置提及的键会原样保留**。这解释了一个容易困惑的现象——本配置的 `train_dataloader` 里根本没有 `data_root`、`sampler`、`collate_fn` 等键，它们全部继承自父配置。

**（3）配置字符串 → 对象。**
u2-l1 讲过：`Registry` 是「名字 → 类」的全局字典，`MODELS.build(cfg)` 取 `cfg['type']` 查表得到类，剩余键全部作为 `__init__` 关键字参数。因此**读配置的最好方式就是拿着配置键去源码类的 `__init__` 签名里对号入座**，本讲的 4.2~4.4 节将反复使用这个方法。

几个本讲会用到、但初学者可能不熟的术语：

| 术语 | 含义 |
| --- | --- |
| pipeline（数据管线） | 一串按顺序作用于 `results` 字典的 transform，每个 `dict(type='XXX', ...)` 是一个 transform 的配置 |
| hook（钩子） | 挂在 Runner 生命周期固定时机（如每个 iter 后）执行的回调，日志、checkpoint 保存都是 hook |
| scheduler（调度器） | 让学习率随训练进度变化的策略，本配置用 `PolyLR`（多项式衰减） |
| `load_from` | 训练前加载的**预训练**权重（只加载参数值，不加载优化器状态），区别于 `resume`（断点续训） |
| DDP | DistributedDataParallel，多卡数据并行训练；`find_unused_parameters` 是它构建时的参数 |

## 3. 本讲源码地图

本讲的主角是两份配置，但配置中每个 `type` 都指向一个源码类：

| 文件 | 作用 | 本讲视角 |
| --- | --- | --- |
| `configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py` | 单卡（1 GPU × batch 4）主配置 | 逐段精读 |
| `configs/uniseg3d_4xb4_scannet_scanrefer_scannet200_unified.py` | 4 卡（4 GPU × batch 4）主配置 | diff 对比 |
| `uniseg3d/uniseg3d.py` | `UniSeg3D` 主模型，消费 `test_cfg` 各阈值 | 4.2 节 |
| `uniseg3d/unified_criterion.py` | 损失总调度，组装五路子损失 | 4.2 节 |
| `uniseg3d/spconv_unet.py` / `query_decoder.py` / `lang_module.py` / `data_preprocessor.py` | 五件套中的骨干 / 解码器 / 语言分支 / 预处理器 | 4.2 节（仅定位，深入在单元 4） |
| `uniseg3d/loading.py` / `transforms_3d.py` / `formatting.py` | 数据管线各 transform | 4.3 节（仅定位，深入在单元 3） |
| `uniseg3d/scannet_dataset.py` / `unified_metric.py` | 数据集类与统一评估器 | 4.3 / 4.4 节 |
| `tools/train.py` | 训练入口，处理 `--cfg-options` | 4.1 / 5 节 |

配置中出现的**损失类**（`ScanNetSemanticCriterion`、`OVInstanceCriterion`、`SparseMatcher`、三种 Cost、两个提示损失、`ContrastiveCriterion`）分别位于 `uniseg3d/semantic_criterion.py`、`uniseg3d/instance_criterion.py`、`uniseg3d/point_prompt_instance_criterion.py`、`uniseg3d/text_prompt_instance_criterion.py`、`uniseg3d/contrastive_criterion.py`，本讲只对照注册位置，机制精读留给单元 5。

## 4. 核心概念与源码讲解

### 4.1 配置骨架：`_base_` 继承、`custom_imports` 与全局变量

#### 4.1.1 概念说明

主配置的前 18 行是整个文件的「地基」，它决定了三件事：

1. **继承什么**：`_base_` 列出两个父配置——`mmdet3d::_base_/default_runtime.py`（日志、checkpoint 等运行时默认值）和 `mmdet3d::_base_/datasets/scannet-seg.py`（ScanNet 语义分割的 dataloader 基线）。注意这两个文件**不在本仓库里**，而在已安装的 mmdet3d 包的 `_base_` 目录下（u1-l3 讲过 mmdet3d 是版本钉死的依赖）。
2. **注册什么**：`custom_imports = dict(imports=['uniseg3d'])` 让 mmengine 在构建 Runner 前代为 `import uniseg3d`，触发全仓库 29 个 `register_module`（u2-l1 的注册链路）。没有这一行，配置里所有 `type='UniSeg3D'` 之类的字符串都会在 build 时报「KeyError: 'UniSeg3D is not in the registry'」。
3. **复用什么**：第 9~18 行的全局变量（`num_channels=32`、`num_instance_classes=18`、`num_semantic_classes=20`、`pred_iou=True`、权重系数等）在文件后面被反复引用，做到「改一处、处处生效」。

#### 4.1.2 核心流程

`tools/train.py` 里 `Config.fromfile(args.config)` 的解析过程：

```text
Config.fromfile('configs/uniseg3d_1xb4_...py')
  ├─ exec 执行本文件 → 得到本文件的顶层变量字典
  ├─ 发现 _base_ → 递归解析两个父配置（mmdet3d 包内）
  ├─ 递归合并：dict 深合并（逐键覆盖），list/标量整体替换
  └─ 得到一个完整的大字典 cfg（父子键都在其中）
随后 runner 启动前：importlib 按 custom_imports 导入 uniseg3d → 注册表就绪
```

一个直接推论：**配置文件里「没写」的键不等于「没有」**。例如 `train_dataloader` 只写了 `batch_size`、`num_workers`、`dataset` 三块，`sampler`、`shuffle`、`data_root` 等都来自父配置 `scannet-seg.py` 的同名键（其值需在本地打印确认，见 4.1.4）。

#### 4.1.3 源码精读

配置骨架：

- [configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:L1-L5](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L1-L5) — `_base_` 声明两个父配置，`custom_imports` 触发 uniseg3d 包注册，`find_unused_parameters=True` 供多卡 DDP 构建时允许部分参数不参与某次前向的反向（提示分支在无提示样本时会被跳过）。
- [configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:L8-L18](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L8-L18) — 全局变量区：`num_semantic_classes=20`（含 wall/floor 两个 stuff 类 + 18 个 thing 类），`num_instance_classes=18`，以及四个损失权重缩放系数（`inst_weight`/`sem_weight`/`hype_lambda`/`contra_hype_lambda`，全部为 1.0，方便整体调权）。

命令行覆盖的入口在训练脚本中：

- [tools/train.py:L76-L77](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/tools/train.py#L76-L77) — `cfg.merge_from_dict(args.cfg_options)`：`--cfg-options a.b.c=v` 会被解析成嵌套 dict，沿点号路径深合并进 `cfg`。这是本讲综合实践的核心机制。

#### 4.1.4 代码实践

**实践目标**：亲眼看到「递归合并」的结果，确认哪些键来自父配置。

**操作步骤**：

```bash
cd <仓库根目录>
python -c "
from mmengine.config import Config
cfg = Config.fromfile('configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py')
print('data_root =', cfg.train_dataloader.dataset.get('data_root', '未设置'))
print('sampler   =', cfg.train_dataloader.get('sampler'))
print('batch_size=', cfg.train_dataloader.batch_size)
cfg.dump('/tmp/merged_1xb4.py')   # 导出合并后的完整配置
"
```

**需要观察的现象**：

1. `batch_size` 为 4（本文件写的）。
2. `data_root`、`sampler` 等**本文件没写的键也有值**——它们来自 mmdet3d 包内的 `scannet-seg.py`。
3. `/tmp/merged_1xb4.py` 比原文件长很多，因为包含了全部继承来的键。

**预期结果**：能明确回答「`data_root` 指向哪里、`ann_file='uniseg3d_infos_train.pkl'` 这个相对路径的基准目录是什么」。父配置的具体取值随 mmdet3d 版本而定，属「待本地验证」内容——这正是本实践要你亲手确认的。

#### 4.1.5 小练习与答案

**练习 1**：把 `custom_imports` 那一行注释掉再运行训练，会发生什么？为什么？
**答案**：在 `MODELS.build(model)` 处抛出 KeyError/RegistryException，提示 `'UniSeg3D' is not registered`。因为配置里的类只有在 `import uniseg3d`（执行其 `__init__.py` 中 19 行 import）时才会被 `@register_module()` 装饰器注册进 Registry（见 u2-l1）。

**练习 2**：配置里 `find_unused_parameters = True` 为什么对本项目是必要的？
**答案**：UniSeg3D 的前向包含多个可选分支（点提示、文本提示、IoU 预测分支等），当某个 batch 没有提示数据或某分支未启用时，部分已注册参数不会参与该次反向传播；DDP 默认会因「部分参数未收到梯度」报错，`find_unused_parameters=True` 让 DDP 容忍这种情况。

**练习 3**：`num_semantic_classes=20` 与 `num_instance_classes=18` 为什么差 2？
**答案**：语义分割要分 20 类（含 `wall`、`floor` 两个 stuff 类），而实例分割只处理「可数的物体」（things），`wall`/`floor` 被排除，剩 18 类。配置后面的 `inst_mapping = sem_mapping[2:]`（[configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:L281-L283](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L281-L283)）就是这个划分：从 nyu40 语义 id 表中去掉前两个（wall=1、floor=2）。

### 4.2 model 五件套与 `test_cfg` 推理阈值

#### 4.2.1 概念说明

`model = dict(...)` 是配置的核心，共 136 行。它声明了六件事：主模型 `UniSeg3D` 与它的五件套子模块——`data_preprocessor`（批数据预处理）、`backbone`（稀疏卷积骨干）、`decoder`（查询解码器）、`lang`（CLIP 语言分支）、`criterion`（损失总调度），外加训练/测试行为开关 `train_cfg`/`test_cfg`。

按 u2-l1 的结论，这个嵌套 dict 会被递归 build 成一棵对象树：

```text
UniSeg3D
 ├─ self.unet      = SpConvUNet(...)            # backbone 键
 ├─ self.decoder   = UnifiedQueryDecoder(...)   # decoder 键
 ├─ self.lang      = LangModule(...)            # lang 键
 ├─ self.criterion = ScanNetUnifiedCriterion(...)  # criterion 键
 │    ├─ sem_criterion               = ScanNetSemanticCriterion
 │    ├─ inst_criterion              = OVInstanceCriterion（内含 matcher、ov_matcher 两个 SparseMatcher）
 │    ├─ point_prompt_inst_criterion = PointPromptInstanceCriterion
 │    ├─ text_prompt_inst_criterion  = TextPromptInstanceCriterion
 │    └─ contrastive_criterion       = ContrastiveCriterion
 ├─ data_preprocessor = Det3DDataPreprocessor_  # 由 Base3DDetector 基类持有
 └─ self.test_cfg  = dict(...)                  # 不 build，原样存为字典，推理时逐键查询
```

`test_cfg` 里的键**不是**任何类的 `__init__` 参数，而是模型推理函数在运行时用 `self.test_cfg.xxx` 读取的阈值——这是它与五件套配置的本质区别。

#### 4.2.2 核心流程

实例分割预测的阈值过滤链（本讲只讲流程与阈值含义，完整推理链路见 u6-l1）：

```text
cls_preds (n_qry, 19) → softmax 取前 18 类 → 展平成 (n_qry*18,)
  ① topk_insts=600        取分数最高的 600 个 (query, class) 对
  ② obj_normalization     用掩码内平均 sigmoid 分数重打分
  ③ nms + matrix_nms_kernel='linear'  矩阵 NMS 衰减重叠掩码分数
  ④ sp_score_thr=0.4      超点级掩码 sigmoid > 0.4 才算属于该实例
  ⑤ inst_score_thr=0.0    分数阈值（实例分支设 0，即不过滤，交给后续）
  ⑥ npoint_thr=100        掩码覆盖的原始点数 > 100 才保留
→ (masks, labels, scores)
```

训练侧还有一个 `query_thr=0.5`：每个样本随机保留 50%~100%（上限 3500）的超点查询参与训练，等价于对「查询数量」做数据增广。

#### 4.2.3 源码精读

**（1）主模型 `__init__`：配置键与参数一一对应。**

- [configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:L20-L34](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L20-L34) — `UniSeg3D` 主体配置：`in_channels=6`（XYZ+RGB）、`voxel_size=0.02`（体素化粒度，米）、`min_spatial_shape=128`、`query_thr=0.5`、`pred_iou=True`（启用 IoU 预测分支）、`set_query_mask/set_all_mask/is_type_embedding`（提示查询的融合与隔离开关，u4-l6 精讲）。
- [uniseg3d/uniseg3d.py:L285-L321](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L285-L321) — `UniSeg3D.__init__` 签名，逐个接收上述配置键；注意 `alpha=0.4, beta=0.8` 有默认值、配置未覆盖。
- [uniseg3d/uniseg3d.py:L311-L314](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L311-L314) — 五件套中的四个在此处 `MODELS.build(...)`，即「配置 dict → 子模块对象」的落点；`data_preprocessor` 则交给基类构造（第 309~310 行）。
- [uniseg3d/uniseg3d.py:L326-L338](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L326-L338) — `is_type_embedding=True` 生效：创建 3 维 `type_embedding`（区分原始查询/点提示/文本提示三类）；`pred_iou=True` 生效：创建三层 MLP 的 `pred_iou_proj` 回归 IoU（u5-l4 精讲）。

**（2）五件套子配置 → 源码类对照表。**

| 配置键 | 配置片段 | 源码类定义 | 关键参数含义（入门级） |
| --- | --- | --- | --- |
| `data_preprocessor` | [L22](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L22) | [uniseg3d/data_preprocessor.py:L8](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/data_preprocessor.py#L8) `Det3DDataPreprocessor_` | 继承 mmdet3d 版本，额外透传提示类键（u3-l2） |
| `backbone` | [L35-L38](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L35-L38) | [uniseg3d/spconv_unet.py:L95](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/spconv_unet.py#L95) `SpConvUNet` | `num_planes=[32..160]` 五层 U-Net 通道数 |
| `lang` | [L39-L46](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L39-L46) | [uniseg3d/lang_module.py:L39-L45](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/lang_module.py#L39-L45) `LangModule` | `init_cfg.checkpoint` 指向 OpenCLIP 权重路径；`fix=True` 冻结文本编码器；`out_features=32` 把 768 维文本特征投影到与查询同维 |
| `decoder` | [L47-L66](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L47-L66) | [uniseg3d/query_decoder.py:L373](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/query_decoder.py#L373) `UnifiedQueryDecoder` | `num_instance_queries=0` 表示**没有可学习查询**（超点即查询）；`sphere_cls=True` + `vocabulary_cls_embedding_path` 启用 CLIP 词表原型分类 |
| `criterion` | [L67-L124](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L67-L124) | [uniseg3d/unified_criterion.py:L7-L16](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_criterion.py#L7-L16) `ScanNetUnifiedCriterion` | 五路子损失在此逐个 `MODELS.build`，`__call__` 时按任务分发（单元 5 精讲） |

**（3）`test_cfg` 阈值 → 源码落点对照表。**

| 配置键（[L127-L136](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L127-L136)） | 值 | 源码落点 | 作用 |
| --- | --- | --- | --- |
| `topk_insts` | 600 | [uniseg3d/uniseg3d.py:L123-L124](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L123-L124) | 在 `n_query×18` 个 (query,类) 对中取分数前 600 进入后续过滤 |
| `obj_normalization` | True | [uniseg3d/uniseg3d.py:L132-L135](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L132-L135) | 用掩码内平均 sigmoid 分数二次校正分类分数 |
| `nms` / `matrix_nms_kernel` | True / 'linear' | [uniseg3d/uniseg3d.py:L137-L140](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L137-L140) | 矩阵 NMS 按重叠度衰减分数（linear 核） |
| `sp_score_thr` | 0.4 | [uniseg3d/uniseg3d.py:L142-L143](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L142-L143) | 超点掩码 sigmoid 二值化阈值 |
| `inst_score_thr` | 0.0 | [uniseg3d/uniseg3d.py:L78](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L78) | 实例分支分数阈值，设 0 即不做分数过滤 |
| `pan_score_thr` | 0.5 | [uniseg3d/uniseg3d.py:L196](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L196) | 全景分支的分数阈值（更严格，0.5） |
| `npoint_thr` | 100 | [uniseg3d/uniseg3d.py:L151-L156](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L151-L156) | 掩码覆盖点数 ≤100 的实例丢弃（滤掉碎片） |
| `stuff_classes` | [0, 1] | [uniseg3d/uniseg3d.py:L194](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L194) | wall/floor 的类别下标，全景合并时作 stuff 处理 |
| `inst_test_iou` / `pano_test_iou` | False / True | [uniseg3d/uniseg3d.py:L78](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L78)、[L196](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L196) | 是否把预测 IoU 乘进分数：全景乘、实例不乘 |

**（4）`query_thr` 的训练侧用法。**

- [uniseg3d/uniseg3d.py:L243-L254](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L243-L254) — `_select_queries`：当 `query_thr < 1` 时，以 \( n = ((1-\text{query\_thr})\cdot u + \text{query\_thr}) \cdot N \)（\(u\sim U[0,1]\)）随机采样超点查询子集（封顶 3500），并同时裁剪 GT 的 `query_masks` 保持对齐；`query_thr=0.5` 即「随机保留 50%~100%」。

#### 4.2.4 代码实践

**实践目标**：把 `test_cfg` 从「一段字典」变成「一张能定位到源码行的地图」。

**操作步骤**：

1. 打开 [configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:L127-L136](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L127-L136)，把 9 个键抄下来。
2. 在编辑器里打开 `uniseg3d/uniseg3d.py`，用搜索（Ctrl+F）逐个查找 `self.test_cfg.<键名>`，记录行号。
3. 对照上文 4.2.3 第（3）张表核对你的结果是否一致。
4. 思考实验（不必运行）：若把 `sp_score_thr` 从 0.4 提到 0.9，预测的实例掩码会变大还是变小？数量变多还是变少？

**需要观察的现象 / 预期结果**：每个 `test_cfg` 键都能在 `uniseg3d.py` 中找到至少一处 `self.test_cfg.xxx` 读取点；步骤 4 的答案是掩码变小（二值化阈值更严）、实例数变少（更多掩码点数不足 100 被 `npoint_thr` 过滤）。此为源码阅读型结论，具体指标变化「待本地验证」（可配合 u6-l1 的实践在验证集上实测）。

#### 4.2.5 小练习与答案

**练习 1**：`num_instance_queries=0`、`num_semantic_queries=0` 意味着什么？
**答案**：解码器**不创建任何可学习的查询向量**。所有查询直接取自超点特征（「超点即查询」范式，见 u1-l1），因此查询数量随每个场景的超点数动态变化，而不是固定的 100 个之类。这也解释了为什么推理阈值 `topk_insts` 要从几千个超点查询中筛选。

**练习 2**：为什么 `inst_score_thr=0.0` 而 `pan_score_thr=0.5`？
**答案**：实例分支后面还有矩阵 NMS 与 `npoint_thr` 两道过滤，分数过滤交给评估阶段的排序（mAP 对阈值不敏感），故设 0；全景分割要把 things 掩码与 stuff 掩码合并成一张全景图，低分掩码会直接「占据」像素造成错误合并，因此需要前置的 0.5 分数阈值。

**练习 3**：配置中 `lang` 的 `init_cfg.checkpoint` 与文件末尾的 `load_from` 都指向权重文件，二者有何区别？
**答案**：`init_cfg.checkpoint` 是 `LangModule` **子模块**的初始化权重（OpenCLIP 文本塔，`work_dirs/pretrained/convnext_large_d_320/open_clip_pytorch_model.bin`），由 mmengine 的权重初始化器在模型构建后注入；`load_from`（[L313](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L313)，`sstnet_scannet.pth`）是**整个 UniSeg3D** 的预训练起点（SSTNet 超点预训练），由 Runner 在训练开始前加载。

### 4.3 `train_pipeline` 与 `test_pipeline` 逐项对照

#### 4.3.1 概念说明

pipeline 是数据侧的 transform 链：数据集每取出一条样本，就把一个 `results` 字典依次喂给链上的每个 transform，每个 transform 读取若干键、写回若干键。训练与测试的 pipeline 共享前缀、分化于中段：

- **训练**要做数据增广（旋转缩放平移、弹性形变）并**随机生成**点/文本提示样本，让模型学会各任务；
- **测试**不能随机：增广全部关闭，提示改用固定策略生成（每个 GT 实例都测），保证结果可复现。

#### 4.3.2 核心流程

两条管线的对照（→ 表示 transform 链）：

```text
train_pipeline:
  LoadPointsFromFile        # 读 points/*.bin → results['points'] (N,6)
  → LoadAnnotations3D_      # 读 instance/semantic/super_points 三类掩码
  → PointSegClassMapping    # nyu40 id → 0..20 连续类别
  → GlobalRotScaleTrans     # ★训练增广：随机旋转/缩放/平移（测试无）
  → NormalizePointsColor_   # RGB 归一化：(c-127.5)/127.5
  → AddSuperPointAnnotations(merge_ov=True)   # 生成超点级 GT + 合并 SAM3D 伪掩码
  → TextPromptGeneration    # 每场景随机抽 3 个实例的 ScanRefer 描述作文本提示
  → PointPromptGeneration   # 每实例采 1 个点作点提示
  → ElasticTransfrom(p=0.5) # ★训练增广：弹性形变，另存 elastic_coords
  → Pack3DDetInputs_(keys=[...18 个键])       # 分拣打包

test_pipeline:
  LoadPointsFromFile → LoadAnnotations3D_ → PointSegClassMapping   # 同训练
  → MultiScaleFlipAug3D(  # mmdet3d 测试包装器（scale/flip 均关闭，仅作容器）
      NormalizePointsColor_
      → AddSuperPointAnnotations(merge_ov 默认 False)  # 测试不合并伪掩码
      → TextPromptTest      # 展开全部文本描述（不再随机抽 3 个）
      → PointPromptTest(max_num_point=3))
  → Pack3DDetInputs_(keys=[...14 个键])
```

注意 `Pack3DDetInputs_` 的 keys 列表是两个 pipeline 最大的落点差异：

- 训练多打包 `elastic_coords`、`is_novel`、`point_prompt_distance_norms`（增广坐标、base/novel 标记、点提示距离权重——全部只在训练用）；
- 测试多保留 `gt_labels_3d`、`gt_sp_masks`（供评估器对照 GT）。

#### 4.3.3 源码精读

- [configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:L139-L144](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L139-L144) — `data_prefix` 四个键把逻辑名映射到 u1-l4 讲过的四类 bin 目录（`points/instance_mask/semantic_mask/super_points`）。
- [configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:L146-L200](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L146-L200) — 完整 `train_pipeline`。
- [configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:L201-L244](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L201-L244) — 完整 `test_pipeline`，注意它把 4 个 transform 嵌进 `MultiScaleFlipAug3D.transforms`（该包装器本身 `flip=False, pts_scale_ratio=1`，不做任何变换，只是 mmdet3d 测试流程的标准容器）。

各 transform 对应的源码类（本讲只定位，单元 3 逐个精读）：

| 配置中的 type | 源码位置 | 备注 |
| --- | --- | --- |
| `LoadPointsFromFile` / `PointSegClassMapping` / `MultiScaleFlipAug3D` | mmdet3d 包内置 | 非本仓库代码 |
| `LoadAnnotations3D_` | [uniseg3d/loading.py:L12-L19](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/loading.py#L12-L19) | 新增 `with_sp_mask_3d` 参数加载超点掩码 |
| `NormalizePointsColor_` | [uniseg3d/loading.py:L71-L80](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/loading.py#L71-L80) | 除以 127.5 而非 255 |
| `AddSuperPointAnnotations` | [uniseg3d/transforms_3d.py:L313](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L313) | 训练传 `merge_ov=True` |
| `TextPromptGeneration` / `TextPromptTest` | [uniseg3d/transforms_3d.py:L13](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L13) / [L64](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L64) | 随机抽 vs 全展开 |
| `PointPromptGeneration` / `PointPromptTest` | [uniseg3d/transforms_3d.py:L113](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L113) / [L168](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L168) | 训练每实例 1 点 vs 测试每场景至多 3 点 |
| `ElasticTransfrom` | [uniseg3d/transforms_3d.py:L238-L249](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/transforms_3d.py#L238-L249) | `gran=[6,20]`、`mag=[40,160]`、`p=0.5` |
| `Pack3DDetInputs_` | [uniseg3d/formatting.py:L12](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/formatting.py#L12) | 分拣 18/14 个键 |
| `GlobalRotScaleTrans` | mmdet3d 内置 | 仅训练出现 |

#### 4.3.4 代码实践

**实践目标**：亲手生成两张 pipeline 的逐项对照表，量化差异。

**操作步骤**：

```bash
cd <仓库根目录>
python -c "
from mmengine.config import Config
cfg = Config.fromfile('configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py')
for name in ['train_pipeline', 'test_pipeline']:
    print(name)
    for t in cfg[name]:
        print('  ', t['type'])
print('train keys:', cfg['train_pipeline'][-1]['keys'])
print('test  keys:', cfg['test_pipeline'][-1]['keys'])
print('train-only keys:', set(cfg['train_pipeline'][-1]['keys']) - set(cfg['test_pipeline'][-1]['keys']))
print('test-only  keys:', set(cfg['test_pipeline'][-1]['keys']) - set(cfg['train_pipeline'][-1]['keys']))
"
```

**需要观察的现象**：训练 pipeline 11 个 transform、测试 9 个（外层 5 个，其中 `MultiScaleFlipAug3D` 内含 4 个）；`train-only keys` 应为 `{'elastic_coords', 'is_novel', 'point_prompt_distance_norms'}`，`test-only keys` 应为 `{'gt_labels_3d', 'gt_sp_masks'}`（后者训练也打包了，故差集可能为空——请以实际输出为准）。

**预期结果**：输出两张 type 序列表与两个 keys 集合，能回答「为什么弹性形变只在训练出现」「为什么测试要打包 GT」。运行结果「待本地验证」（依赖已安装 mmengine）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `GlobalRotScaleTrans` 与 `ElasticTransfrom` 不出现在 test_pipeline？
**答案**：它们是数据增广，引入随机性以提高模型泛化；测试要求确定性的输入，任何几何扰动都会直接改变预测结果、破坏可复现性与评估公平性。

**练习 2**：`AddSuperPointAnnotations` 在两条管线中的参数差异是什么，为什么？
**答案**：训练传 `merge_ov=True`（[L171-L176](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L171-L176)），把官方 pkl 里的 SAM3D 伪掩码合并进 GT，用于开放词汇 base/novel 双路监督（u5-l3）；测试不传（默认 False，[L226-L230](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L226-L230)），评估必须用干净的人工标注。

**练习 3**：`TextPromptGeneration(num_ins=3)` 与 `TextPromptTest(num_ins=3)` 同为 `num_ins=3`，行为一样吗？
**答案**：不一样。训练版在场景的多个描述里**随机**选 3 个实例的文本（`random_select=False` 控制选文本的方式）；测试版把场景内**全部**实例的描述展开成逐条评估样本（`num_ins` 在测试侧的含义是每场景处理的实例上限，具体行为见 u3-l5 精读）。

### 4.4 训练策略：dataloader、优化器、调度器、hooks 与 1xb4/4xb4 差异

#### 4.4.1 概念说明

配置的最后三分之一（[L246-L325](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L246-L325)）回答「怎么喂数据、怎么更新参数、训多久、何时存档」：

- **dataloader**：训练 `batch_size=4, num_workers=16`；验证/测试共用一条（`test_dataloader = val_dataloader`，Python 直接赋值）。
- **优化器**：AdamW，`lr=0.0001`，`weight_decay=0.05`，梯度裁剪 `max_norm=10`（L2 范数）。
- **调度器**：PolyLR——第 \(t\) 步学习率 \(\eta_t = \eta_0 (1 - t/T)^{0.9}\)，\(T\) 为总步数，平滑衰减到 0。
- **训练循环**：`EpochBasedTrainLoop`，`max_epochs=512`，`dynamic_intervals=[(1,16),(496,1)]`——训练前期每 16 个 epoch 验证一次，最后 16 个 epoch（512-16=496 起）每个 epoch 都验证（该参数是 mmengine 训练循环用于动态调整验证间隔的机制，具体语义以本地 mmengine 版本签名为准，待本地验证）。
- **hooks**：`EmptyCacheHook(after_iter=True)` 每 iter 清 CUDA 缓存（点云场景显存波动大）；checkpoint 每 8 epoch 存一次、最多留 3 份、按 `all_ap`/`miou`/`pq` 三个指标各存一份最优；日志每 50 iter 打一条。
- **`load_from`**：从 SSTNet 超点预训练权重热启动（u1-l2 讲过下载）。
- **`randomness`**：`seed=1` 固定随机种子，`diff_rank_seed=True` 让多卡各 rank 种子不同（保证各卡增广多样）。

#### 4.4.2 核心流程

```text
Runner 启动
 ├─ 按 optim_wrapper 构建 AdamW + 梯度裁剪
 ├─ 按 param_scheduler 构建 PolyLR（lr 随 step 衰减）
 ├─ 按 train_cfg.max_epochs=512 跑 EpochBasedTrainLoop
 │    每 epoch 末按 dynamic_intervals 决定是否验证
 │    EmptyCacheHook 在每个 iter 后清缓存
 │    CheckpointHook 每 8 epoch 存档，LoggerHook 每 50 iter 记日志
 └─ load_from 的 sstnet_scannet.pth 在首个 iter 前注入参数
```

#### 4.4.3 源码精读

- [configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:L247-L266](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L247-L266) — 三个 dataloader。`dataset.type='ScanNetUnifiedSegDataset'`（[uniseg3d/scannet_dataset.py:L11](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/scannet_dataset.py#L11)）；训练集 `ann_file='uniseg3d_infos_train.pkl'`、`scene_idxs=None`（由数据集内部的 `get_scene_idxs` 按场景点数加权采样，见 u3-l1）；验证/测试用 `test_mode=True` + `test_pipeline`。
- [configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:L284-L293](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L284-L293) — 统一评估器 `PromptSupportedUnifiedSegMetric`（[uniseg3d/unified_metric.py:L80](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/unified_metric.py#L80)）的配置：`stuff_class_inds=[0,1]`、`thing_class_inds=range(2,20)`、`id_offset=2**16`（全景合并时实例 id 加偏移避免与语义 id 撞号）、`sem_mapping` 是 20 类的 nyu40 id 表。
- [configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:L295-L311](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L295-L311) — 优化器、调度器、hooks（`epoch = 512` 在 [L300](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L300) 定义，被 `param_scheduler.end` 与 `train_cfg.max_epochs` 两处引用——变量复用的典型例子）。
- [configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:L313-L325](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L313-L325) — `load_from`、`randomness`、三个循环（`train_cfg`/`val_cfg`/`test_cfg` 顶层键由 Runner 消费；注意与 `model` 内部的 `train_cfg=dict()`、`test_cfg=...` 同名不同物，u2-l1 讲过这个陷阱）。

**1xb4 与 4xb4 的全部差异**（我在本仓库实测 `diff` 的真实输出）：

```diff
297c297
<     optimizer=dict(type='AdamW', lr=0.0001, weight_decay=0.05),
---
>     optimizer=dict(type='AdamW', lr=0.0002, weight_decay=0.05),
325c325,332
< test_cfg = dict(type='TestLoop')
\ No newline at end of file
---
> test_cfg = dict(type='TestLoop')
> test_cfg = dict(type='TestLoop')
>
> env_cfg = dict(
>     cudnn_benchmark=False,
>     mp_cfg=dict(mp_start_method='spawn', opencv_num_threads=0),
>     dist_cfg=dict(backend='nccl'),
> )
\ No newline at end of file
```

逐条解释：

1. **学习率 0.0001 → 0.0002**（[4xb4 L297](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_4xb4_scannet_scanrefer_scannet200_unified.py#L297) vs [1xb4 L297](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L297)）：README（[L162-L168](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/README.md#L162-L168)）规定 1xb4 用单卡、4xb4 用 4 卡，总 batch 从 4 变 16，按「batch 增大则 lr 上调」的经验规则翻倍。
2. **重复的 `test_cfg` 行**（[4xb4 L325-L326](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_4xb4_scannet_scanrefer_scannet200_unified.py#L325-L326)）：配置是 Python，同一变量连续赋值两次，后者覆盖前者且两次值相同，**完全无影响**，属无害笔误——这也侧面证明配置确实是普通代码。
3. **新增 `env_cfg`**（[4xb4 L328-L332](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_4xb4_scannet_scanrefer_scannet200_unified.py#L328-L332)）：多卡环境的显式声明——`dist_cfg=dict(backend='nccl')` 指定分布式后端为 NCCL（GPU 通信库，u1-l2 讲过）；`mp_start_method='spawn'` 规定 dataloader 多进程（16 个 worker）的启动方式；`opencv_num_threads=0` 关闭 OpenCV 内部多线程避免与 DataLoader 进程冲突；`cudnn_benchmark=False`（输入尺寸多变时开启 benchmark 反而有害）。1xb4 未写，沿用父配置默认。

#### 4.4.4 代码实践

**实践目标**：亲手复现上述 diff 并解释每一处。

**操作步骤**：

```bash
cd <仓库根目录>
diff -u configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py \
        configs/uniseg3d_4xb4_scannet_scanrefer_scannet200_unified.py
```

**需要观察的现象**：输出应只有上述两处差异块（lr 与文件尾），其余 320 行完全一致——两份配置 99% 相同。

**预期结果**：你能对每行 diff 说出「改了什么、为什么、不改会怎样」。例如：若 4 卡沿用 `lr=0.0001`，等效于单卡 lr 更小，收敛更慢；若删掉 `env_cfg`，多卡启动时将回退到父配置/默认的分布式设置。

#### 4.4.5 小练习与答案

**练习 1**：`save_best=['all_ap', 'miou', 'pq']` 意味着磁盘上最多可能同时存在几个 checkpoint 文件？
**答案**：`max_keep_ckpts=3` 的常规存档 + 3 个指标各自的最优存档，至多 6 个量级（mmengine 会为每个 best 指标单独命名保存；具体是否去重「待本地验证」，但语义是「三个指标各保留一份最优」）。

**练习 2**：`dynamic_intervals=[(1, 16), (epoch-16, 1)]` 中 `epoch-16` 是多少？这个设计的意图是什么？
**答案**：512-16=496。意图：训练前 496 个 epoch 每 16 个 epoch 验证一次（省时间），最后 16 个 epoch 每个 epoch 验证（密集追踪收敛末期的最佳权重，配合 `save_best` 捞住峰值）。

**练习 3**：为什么需要 `EmptyCacheHook(after_iter=True)`？
**答案**：每个场景点云大小差异很大，稀疏卷积的中间张量显存占用波动剧烈；每个 iter 结束后调用 `torch.cuda.empty_cache()` 归还缓存块，降低多卡训练时的 OOM 概率（代价是微小的速度开销，故只在点云类任务常见）。

## 5. 综合实践

把本讲全部知识串起来：**diff 对比 + 命令行覆盖 dry-run**。

**任务**：验证 `--cfg-options` 能在不改文件的情况下改写训练行为。

**步骤**：

1. 先完成 4.4.4 的 diff，确认你理解两份配置的全部差异。
2. 不启动真训练，先离线验证覆盖机制（等价于 [tools/train.py:L76-L77](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/tools/train.py#L76-L77) 做的事）：

```bash
python -c "
from mmengine.config import Config
from mmengine.utils import DictAction
cfg = Config.fromfile('configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py')
overrides = DictAction.parse(['train_dataloader.batch_size=2', 'train_cfg.max_epochs=1'])
cfg.merge_from_dict(overrides)
print('batch_size =', cfg.train_dataloader.batch_size)
print('max_epochs =', cfg.train_cfg.max_epochs)
"
```

3. 若数据已就绪（u1-l4），做真实 dry-run：

```bash
CUDA_VISIBLE_DEVICES=0 ./tools/dist_train.sh \
  configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py 1 \
  --cfg-options train_dataloader.batch_size=2 train_cfg.max_epochs=1 \
  --work-dir work_dirs/dryrun_test
```

**需要观察的现象**：

- 步骤 2 应打印 `batch_size = 2`、`max_epochs = 1`（原值 4 与 512）。
- 步骤 3 的日志中：Epoch 进度条显示 `1/1`（原来会是 `1/512`）；`Train loop` 相关行的 batch 规模减半（每 iter 样本数 2）；`work_dirs/dryrun_test/` 下会生成一份完整 dump 的配置文件，打开它与原配置 diff，能直接看到命令行覆盖被写进了哪些键。

**预期结果**：确认「CLI `--cfg-options` > 配置文件」的覆盖优先级；理解 `train_cfg.max_epochs` 覆盖的是**顶层**训练循环键而不是 `model` 内的同名键（点号路径 `train_cfg.max_epochs` 从 cfg 根出发，恰好命中顶层键）。步骤 2/3 的实际运行「待本地验证」（本环境未安装 mmengine）。

## 6. 本讲小结

- 配置是可执行的 Python：全局变量（`num_channels`、`epoch` 等）+ `_base_` 递归合并（dict 深合并、list 整体替换），**没写的键继承自 mmdet3d 包内的父配置**。
- `model` 五件套经 `MODELS.build` 递归实例化：`SpConvUNet` 骨干、`UnifiedQueryDecoder` 解码器（`num_instance_queries=0`，超点即查询）、`LangModule` 语言分支（冻结 + 投影到 32 维）、`ScanNetUnifiedCriterion` 五路损失树、`Det3DDataPreprocessor_` 预处理器。
- `test_cfg` 的 9 个键是推理阈值而非构造参数，落点全在 `uniseg3d/uniseg3d.py` 的 `predict_by_feat_*` 系列：`topk_insts`→L123、`sp_score_thr`→L143、`npoint_thr`→L153 等。
- 两条 pipeline 共享「加载→映射→归一化→超点标注→提示生成→打包」骨架，训练独有增广（旋转缩放、弹性形变）与随机提示，测试用固定策略且打包 GT 供评估。
- 训练策略：AdamW（lr 1e-4/2e-4）+ PolyLR + 512 epoch + 三指标最优存档 + SSTNet 预训练热启动；1xb4 与 4xb4 仅差学习率、无害重复行与多卡 `env_cfg`（NCCL 后端等）。
- `--cfg-options` 经 `merge_from_dict` 沿点号路径覆盖配置，优先级高于文件内容，是做实验最快的旋钮。

## 7. 下一步学习建议

本讲结束了「框架层」的配置视角。下一讲进入单元 3 的数据流主线：

- **u3-l1 数据集类 ScanNetUnifiedSegDataset**：本讲只看到 `dataset=dict(type=...)` 的配置面，下一讲深入 `uniseg3d/scannet_dataset.py`，看 `parse_data_info` 如何用 `data_prefix` 拼出四类 bin 路径、`get_scene_idxs` 如何决定采样。
- 建议顺手阅读：`uniseg3d/loading.py`（两个下划线类）与 `uniseg3d/formatting.py`，对照本讲 4.3 的对照表，为 u3-l2 做准备。
- 如果你更关心模型侧，可以先跳到 u4-l1，但需要先补 u3-l2（`Pack3DDetInputs_` 的键分拣决定了模型 `loss()` 收到什么）。
