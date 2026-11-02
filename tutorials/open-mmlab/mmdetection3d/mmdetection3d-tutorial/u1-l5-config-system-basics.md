# 配置系统入门：一个配置文件如何描述整个实验

## 1. 本讲目标

上一讲（u1-l4）我们画出了仓库地图，知道 `configs/` 目录存放的是「实验描述」。本讲就来彻底读懂这份描述：

1. 理解 MMEngine 配置的本质：配置文件就是普通 Python 文件，`dict(type='Xxx')` 是描述一切模块的统一语法。
2. 掌握 `_base_` 四件套（models / datasets / schedules / runtime）的职责划分与继承合并规则。
3. 能逐块读懂 PointPillars KITTI 配置中的 model、data、schedule、runtime 四大要素。
4. 会用 `--cfg-options` 和 `--work-dir` 在命令行临时覆盖配置，而不改动任何文件。

学完本讲，你看到任何一个 `configs/` 下的配置文件，都应该能说出「它继承了什么、改了什么、最终会实例化哪些类」。

## 2. 前置知识

在开始之前，请确认你理解以下两个来自前面几讲的概念：

- **注册器路由**（u1-l4）：MMDetection3D 中每个可配置的类都注册在注册器里，配置中的 `type='PointPillarsScatter'` 这样的字符串会被拿去注册器查表，找到真实的 Python 类再实例化。
- **四件套**（u1-l4）：`configs/_base_/` 下有 models、datasets、schedules、runtime 四类基础配置，具体实验配置通过 `_base_` 继承它们。

本讲还需要补充两个新概念：

- **配置即代码（config as code）**：OpenMMLab 的配置文件后缀是 `.py`，它会被当作 Python 模块执行一次。你在配置文件里可以定义变量、写表达式、拼接字符串——这比 JSON/YAML 灵活得多。执行后收集到的所有模块级变量，构成一个 `Config` 对象。
- **「中间变量」**：配置文件里 `voxel_size = [0.16, 0.16, 4]` 这种变量并不是 MMEngine 认识的「标准键」，它只是为了让同一份数值能被多处引用。真正被 Runner 消费的是 `model`、`train_dataloader` 这些标准键；中间变量的值在文件执行阶段就已经被嵌入到标准键的字典里了。

一个术语提醒：本讲会出现**两个不同的 `train_cfg`**——一个嵌在 `model` 字典内部（描述正负样本分配器等模型训练超参），一个是顶层键（描述训练循环跑多少个 epoch）。它们名字相同但完全无关，是初学者最容易混淆的点，4.3 节会专门辨析。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [configs/_base_/models/pointpillars_hv_secfpn_kitti.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/pointpillars_hv_secfpn_kitti.py) | 模型四件套：VoxelNet 检测器的全部组件与超参 |
| [configs/_base_/datasets/kitti-3d-3class.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/datasets/kitti-3d-3class.py) | 数据集四件套：KITTI 数据集、数据管线、dataloader、评估器 |
| [configs/_base_/schedules/cyclic-40e.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/schedules/cyclic-40e.py) | 日程四件套：优化器、学习率策略、训练轮数 |
| [configs/_base_/default_runtime.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/default_runtime.py) | 运行时四件套：默认 scope、Hook、环境、日志 |
| [configs/pointpillars/pointpillars_hv_secfpn_8xb6-160e_kitti-3d-3class.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/pointpillars/pointpillars_hv_secfpn_8xb6-160e_kitti-3d-3class.py) | 最终实验配置：继承上面四个文件并做增量覆盖 |
| [tools/misc/print_config.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/misc/print_config.py) | 展开继承后的完整配置（本讲主要实践工具） |
| [tools/train.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/train.py) | 训练入口：`--cfg-options` / `--work-dir` 的覆盖逻辑在此实现 |
| [docs/en/user_guides/config.md](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docs/en/user_guides/config.md) | 官方配置说明文档，可作为延伸阅读 |

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：配置即 Python、`_base_` 继承与合并规则、四要素逐块精读、命令行覆盖机制。

### 4.1 配置即 Python：从文件到 Config 对象

#### 4.1.1 概念说明

MMDetection3D 没有为每个模型写一个 Python 启动脚本，而是把「一个实验的全部设定」写进一个 `.py` 文件。`Config.fromfile('xxx.py')` 的做法非常直接：

1. 把该文件当作 Python 模块执行一遍；
2. 收集执行后模块里所有**非下划线开头**的顶层变量；
3. 把这些变量组织成一个 `Config` 对象（行为像 dict，但支持从文件继承、增量合并等额外能力）。

于是配置文件里可以出现任何合法 Python：定义变量、字符串拼接、列表推导。而描述「一个要被实例化的模块」的统一语法就是：

```python
dict(type='类名', 参数1=值, 参数2=值, ...)
```

`type` 之外的键全部原样作为关键字参数传给该类的 `__init__`。这就是「配置驱动」的含义：**配置文件描述结构，注册器负责把 `type` 字符串路由到真实类**（u1-l4 讲过 `grep class Xxx` 反查法）。

#### 4.1.2 核心流程

```text
xxx_config.py 文件
    │  (Config.fromfile 执行该文件)
    ▼
模块级变量集合：voxel_size, model, train_dataloader, ...
    │  (解析 _base_，递归继承合并，见 4.2)
    ▼
完整 Config 对象 cfg
    │  (Runner.from_cfg(cfg) 按标准键逐个 build)
    ├─ cfg.model          → MODELS 注册器 → VoxelNet 实例（内部再逐层 build 子组件）
    ├─ cfg.train_dataloader → DATASETS + TRANSFORMS → DataLoader 实例
    ├─ cfg.optim_wrapper  → 优化器包装器
    ├─ cfg.val_evaluator  → METRICS → KittiMetric 实例
    └─ cfg.default_hooks 等 → 各类 Hook
```

要注意两点：

- **先求值，后合并**。每个配置文件先在自己的文件里把表达式算好（比如把 `voxel_size` 嵌进 `model` 字典），然后才参与继承合并。所以「中间变量」不会跨文件共享——两个文件可以各自定义同名的 `point_cloud_range` 而互不干扰。
- **中间变量也会留在 cfg 里**。`voxel_size`、`lr` 这些变量执行后同样被收集进 `Config` 对象，`print_config` 展开后能看到它们，只是 Runner 不认识这些键，会直接忽略。

#### 4.1.3 源码精读

先看模型四件套的开头，体会「中间变量 + type 字典」的写法：

[configs/_base_/models/pointpillars_hv_secfpn_kitti.py:L1-L12](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/pointpillars_hv_secfpn_kitti.py#L1-L12)

```python
voxel_size = [0.16, 0.16, 4]

model = dict(
    type='VoxelNet',
    data_preprocessor=dict(
        type='Det3DDataPreprocessor',
        voxel=True,
        voxel_layer=dict(
            max_num_points=32,  # max_points_per_voxel
            point_cloud_range=[0, -39.68, -3, 69.12, 39.68, 1],
            voxel_size=voxel_size,
            max_voxels=(16000, 40000))),
```

第 1 行定义中间变量 `voxel_size`，第 11 行把它嵌入 `voxel_layer`。展开后的配置里 `voxel_size` 的值已经「烧进」了字典。`type='VoxelNet'` 说明这个模型整体是 `mmdet3d/models/detectors/voxelnet.py` 里的 VoxelNet 类；`data_preprocessor` 是它的第一个子组件（第 5 章会详细讲体素化，本讲只需把它当成「type 字典的嵌套」来看）。

嵌套可以任意深。继续往下看模型主体的四个组件：

[configs/_base_/models/pointpillars_hv_secfpn_kitti.py:L13-L32](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/pointpillars_hv_secfpn_kitti.py#L13-L32)

```python
    voxel_encoder=dict(
        type='PillarFeatureNet', ...),
    middle_encoder=dict(
        type='PointPillarsScatter', in_channels=64, output_shape=[496, 432]),
    backbone=dict(
        type='SECOND', ...),
    neck=dict(
        type='SECONDFPN', ...),
```

`model` 字典的每个子键（`voxel_encoder`、`middle_encoder`、`backbone`、`neck`、`bbox_head`）又是一个 `dict(type=...)`——VoxelNet 类的 `__init__` 接收这些字典，在内部再次调用注册器 build 出子模块。也就是说，**一个配置文件递归地描述了一整棵对象树**。

再看一个跨库借用的例子，模型的分类损失：

[configs/_base_/models/pointpillars_hv_secfpn_kitti.py:L52-L62](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/pointpillars_hv_secfpn_kitti.py#L52-L62)

```python
        loss_cls=dict(
            type='mmdet.FocalLoss',
            use_sigmoid=True, ...),
        loss_bbox=dict(
            type='mmdet.SmoothL1Loss', beta=1.0 / 9.0, loss_weight=2.0),
        loss_dir=dict(
            type='mmdet.CrossEntropyLoss', use_sigmoid=False,
            loss_weight=0.2)),
```

`type='mmdet.FocalLoss'` 中的 `mmdet.` 前缀表示**切换到 MMDetection 仓库的注册器**里去找 FocalLoss（u1-l4 提过这个跨库借用机制）。注意 `beta=1.0 / 9.0`——这就是普通 Python 表达式，YAML 做不到这一点。

最后看展开工具的实现，验证「配置即 Python」：

[tools/misc/print_config.py:L17-L23](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/misc/print_config.py#L17-L23)

```python
def main():
    args = parse_args()

    cfg = Config.fromfile(args.config)
    if args.options is not None:
        cfg.merge_from_dict(args.options)
    print(f'Config:\n{cfg.pretty_text}')
```

`Config` 类来自 MMEngine（`from mmengine import Config`）。`fromfile` 完成「执行 + 收集 + 继承合并」三步，`pretty_text` 把最终配置排版成可读文本。这三行就是本讲综合实践的主角。

#### 4.1.4 代码实践

**实践目标**：亲手验证「配置文件会被当作 Python 执行」，并看到中间变量确实进入了 Config 对象。

**操作步骤**（待本地验证，需要先完成 u1-l2 的环境安装）：

1. 在仓库根目录进入 Python 交互环境：

   ```bash
   python -i
   ```

2. 依次执行（示例代码，不是仓库自带脚本）：

   ```python
   from mmengine import Config
   cfg = Config.fromfile('configs/_base_/models/pointpillars_hv_secfpn_kitti.py')
   print(cfg.voxel_size)                              # 中间变量被收集
   print(cfg.model.type)                              # 'VoxelNet'
   print(cfg.model.data_preprocessor.voxel_layer.voxel_size)  # 已嵌入
   print(list(cfg.keys()))                            # 查看全部顶层键
   ```

3. 尝试在配置里用表达式：直接执行
   `cfg.model.bbox_head.loss_bbox.beta`，确认它已经是算好的浮点数 `0.111...`，而不是字符串 `"1.0 / 9.0"`。

**需要观察的现象**：

- `cfg.voxel_size` 输出 `[0.16, 0.16, 4]`，说明中间变量确实保留在 Config 中；
- `beta` 是数值而非字符串，说明表达式在文件加载时已经求值。

**预期结果**：你能说出 Config 对象里每个顶层键的来源，并理解「Runner 只消费标准键」这一约定。

#### 4.1.5 小练习与答案

**练习 1**：为什么 MMDetection3D 选择 `.py` 而不是 `.yaml` 作为配置格式？说出至少两个 YAML 做不到的用法。

**参考答案**：(1) 可以定义中间变量实现一处修改多处生效，如 `voxel_size` 同时用于 `voxel_layer` 和 `voxel_encoder`；(2) 可以写表达式，如 `beta=1.0 / 9.0`、`eta_min=lr * 1e-4`；(3) 可以做字符串拼接，如 `info_path=data_root + 'kitti_dbinfos_train.pkl'`。这些在静态 YAML 里都要靠预处理或重复书写。

**练习 2**：`type='mmdet.FocalLoss'` 和 `type='FocalLoss'`（假设去掉前缀）在查找上有什么区别？

**参考答案**：无前缀时在当前 `default_scope`（本库是 `mmdet3d`，见 4.3.4）的注册器及其父级里查找；带 `mmdet.` 前缀时显式切换到 MMDetection 的注册器作用域查找，用于直接复用 2D 检测库的组件。

### 4.2 `_base_` 四件套与继承合并规则

#### 4.2.1 概念说明

一个 3D 检测实验需要几十项设定：网络结构、数据集路径、数据增强流水线、优化器、学习率曲线、训练轮数、日志频率……如果每个实验都从零写一遍，会有大量重复。MMEngine 的解法是**模板 + 继承 + 增量覆盖**：

- `configs/_base_/` 下放四类可复用模板：
  - **models/**：只写 `model` 一个键，描述网络结构；
  - **datasets/**：写数据集、pipeline、dataloader、evaluator；
  - **schedules/**：写 `optim_wrapper`、`param_scheduler`、`train_cfg`（训练轮数）；
  - **default_runtime.py**：写 Hook、环境、日志等与具体算法无关的运行时设定。
- 具体实验配置（如 `configs/pointpillars/xxx.py`）通过 `_base_` 声明继承哪些模板，然后**只写与模板不同的部分**。

官方文档把由 `_base_` 四件套直接组合出的配置称为 _primitive_ 配置，并建议同一目录下只保留一个 primitive，其余配置再从它继承（继承层级不超过 3 层），见 [docs/en/user_guides/config.md:L379-L392](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docs/en/user_guides/config.md#L379-L392)。

#### 4.2.2 核心流程

继承合并遵循三条规则：

1. **递归合并**：`dict` 与 `dict` 合并时逐键深合并——子配置里没写的键保留父配置的值，写了的键覆盖父配置的值（子字典内部再递归合并）。
2. **列表整体替换**：`list` 不做逐元素合并，子配置一旦写了某个列表字段（如 `train_pipeline`、`param_scheduler`），就**整个替换**父配置的列表。
3. **多个 `_base_` 按序覆盖**：`_base_` 是列表时，后面的文件覆盖前面文件中的同名键；当前文件自己的键优先级最高。

```text
_base_ = [models/A.py, datasets/B.py, schedules/C.py, runtime.py]
            │              │                │             │
            └── 按 顺 序 合 并：后 者 覆 盖 前 者 ──────────┘
                              ▼
                    父配置合并结果（模板并集）
                              ▼
                   + 当前文件正文（优先级最高）
                              ▼
                        最终 Config 对象
```

一个关键推论：由于「先求值后合并」（4.1.2），父配置文件里的中间变量即使被继承下来，也**不会**反过来影响子配置文件里的字典——子文件想改 `point_cloud_range`，必须连同引用它的 `pipeline` 一起重写（列表反正也是整体替换）。

#### 4.2.3 源码精读

先看最终实验配置的 `_base_` 声明：

[configs/pointpillars/pointpillars_hv_secfpn_8xb6-160e_kitti-3d-3class.py:L1-L5](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/pointpillars/pointpillars_hv_secfpn_8xb6-160e_kitti-3d-3class.py#L1-L5)

```python
_base_ = [
    '../_base_/models/pointpillars_hv_secfpn_kitti.py',
    '../_base_/datasets/kitti-3d-3class.py',
    '../_base_/schedules/cyclic-40e.py', '../_base_/default_runtime.py'
]
```

四行 `_base_` 恰好对应四件套。文件名本身也携带信息：`8xb6` 表示 8 卡 × 每卡 batch 6，`160e` 表示实际训练 160 轮（80 轮 × RepeatDataset 重复 2 次）。

接下来看「增量覆盖」最典型的例子——dataloader：

[configs/pointpillars/pointpillars_hv_secfpn_8xb6-160e_kitti-3d-3class.py:L79-L82](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/pointpillars/pointpillars_hv_secfpn_8xb6-160e_kitti-3d-3class.py#L79-L82)

```python
train_dataloader = dict(
    dataset=dict(dataset=dict(pipeline=train_pipeline, metainfo=metainfo)))
test_dataloader = dict(dataset=dict(pipeline=test_pipeline, metainfo=metainfo))
val_dataloader = dict(dataset=dict(pipeline=test_pipeline, metainfo=metainfo))
```

对比数据集模板里的完整写法（4.3.2 会看到），这里只写了 4 行。以 `train_dataloader` 为例：

- 父配置中它是 `dict(batch_size=6, num_workers=4, ..., dataset=dict(type='RepeatDataset', ..., dataset=dict(type='KittiDataset', ...)))`；
- 子配置只写了 `dataset.dataset.pipeline` 和 `dataset.dataset.metainfo` 两处嵌套字段；
- 递归合并后，`batch_size=6`、`num_workers=4`、`sampler`、`ann_file` 等全部保留，只有 pipeline 被换成 PointPillars 自己的版本（去掉了 ObjectNoise、调整了 db_sampler 采样组数）。

注意 `dataset.dict` 的双层嵌套是因为模板中 dataloader 的 dataset 是 `RepeatDataset` 包装器，真正的 `KittiDataset` 在内层 `dataset` 键里——**增量覆盖必须精确写到要改的那一层**，这也是初学者最容易写错的地方。

再看「列表整体替换」的例子——学习率策略：

[configs/pointpillars/pointpillars_hv_secfpn_8xb6-160e_kitti-3d-3class.py:L85-L97](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/pointpillars/pointpillars_hv_secfpn_8xb6-160e_kitti-3d-3class.py#L85-L97)

```python
lr = 0.001
epoch_num = 80
optim_wrapper = dict(
    optimizer=dict(lr=lr), clip_grad=dict(max_norm=35, norm_type=2))
param_scheduler = [
    dict(
        type='CosineAnnealingLR',
        T_max=epoch_num * 0.4,
        eta_min=lr * 10,
        ...
```

`optim_wrapper` 是 dict，走递归合并：只覆盖 `optimizer.lr` 和 `clip_grad`，父配置（cyclic-40e）里的 `type='OptimWrapper'`、`optimizer.type='AdamW'`、`betas`、`weight_decay` 原样保留。而 `param_scheduler` 是 list，PointPillars 重写了全部 4 个调度器（两段 CosineAnnealingLR + 两段 CosineAnnealingMomentum，区间按 80 轮重新划分），父配置的 4 个调度器被整体丢弃。

最后看顶层对训练轮数的覆盖：

[configs/pointpillars/pointpillars_hv_secfpn_8xb6-160e_kitti-3d-3class.py:L122-L130](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/pointpillars/pointpillars_hv_secfpn_8xb6-160e_kitti-3d-3class.py#L122-L130)

```python
# PointPillars usually need longer schedule than second, we simply double
# the training schedule. Do remind that since we use RepeatDataset and
# repeat factor is 2, so we actually train 160 epochs.
train_cfg = dict(by_epoch=True, max_epochs=epoch_num, val_interval=2)
val_cfg = dict()
test_cfg = dict()
```

这里覆盖的是**顶层 `train_cfg`**（训练循环配置，来自 schedules 模板），把 `max_epochs` 从 40 改成 80；由于 dataloader 里 `RepeatDataset(times=2)` 会把每轮数据重复两遍，实际等效 160 轮——这正是文件名里 `160e` 的含义。

#### 4.2.4 代码实践

**实践目标**：用对比实验亲眼确认「dict 递归合并、list 整体替换」。

**操作步骤**（待本地验证）：

1. 展开父配置（schedules 模板）：

   ```bash
   python tools/misc/print_config.py configs/_base_/schedules/cyclic-40e.py
   ```

2. 展开完整实验配置并重定向到文件：

   ```bash
   python tools/misc/print_config.py \
     configs/pointpillars/pointpillars_hv_secfpn_8xb6-160e_kitti-3d-3class.py \
     > /tmp/pp_full.txt
   ```

3. 在 `/tmp/pp_full.txt` 中分别搜索 `optim_wrapper` 和 `param_scheduler` 两段，与第 1 步输出对照。

**需要观察的现象**：

- `optim_wrapper` 段中 `optimizer.type='AdamW'`、`betas=(0.95, 0.99)`、`weight_decay=0.01` 仍然存在（继承自模板），但 `lr` 变成 `0.001`、`clip_grad.max_norm` 变成 `35`（被子配置覆盖）；
- `param_scheduler` 段中 4 个调度器的 `T_max`、`begin`、`end` 全部是 80 轮版本（32/48），找不到 40 轮版本（16/24）的任何残留——证明列表被整体替换。

**预期结果**：你能准确预言「在子配置里改某个字段，最终配置里会剩下什么」。

#### 4.2.5 小练习与答案

**练习 1**：如果只想把 PointPillars 的 batch_size 改成 3，在继承配置里写 `train_dataloader = dict(batch_size=3)` 是否可行？会不会丢失 `dataset`、`sampler` 等字段？

**参考答案**：可行且不会丢失。因为 `train_dataloader` 是 dict，递归合并只覆盖 `batch_size` 这一个键，其余键（`num_workers`、`sampler`、`dataset` 整棵子树）原样继承。

**练习 2**：数据集模板 `kitti-3d-3class.py` 第 5 行定义 `point_cloud_range = [0, -40, -3, 70.4, 40, 1]`，而 PointPillars 顶层配置第 7 行定义 `point_cloud_range = [0, -39.68, -3, 69.12, 39.68, 1]`。最终配置里 `model` 内部用的是哪个值？模板里的 `[0, -40, ...]` 会不会把模型配置里的值覆盖掉？

**参考答案**：模型用的是 `[0, -39.68, -3, 69.12, 39.68, 1]`。因为「先求值后合并」：模型模板在它自己的文件里执行时已经把数值嵌入字典，数据集模板的中间变量 `point_cloud_range` 只影响它自己文件内的 pipeline；顶层变量合并时后者同名覆盖前者，但它只是一个孤立变量，不会被任何已经求值完成的字典引用。最终生效于 pipeline 的值以「谁写的 pipeline 用了谁的变量」为准（此处顶层重写了 train/test pipeline，用的是 -39.68 版本）。

### 4.3 四要素逐块精读：model / data / schedule / runtime

#### 4.3.1 模块一：model —— 一棵对象树的描述

##### 概念说明

`model` 键描述检测器整棵对象树：顶层是检测器类（VoxelNet），它的构造参数里又嵌着 `data_preprocessor`、`voxel_encoder`、`middle_encoder`、`backbone`、`neck`、`bbox_head` 六个子组件，每个子组件又是一个 `dict(type=...)`。此外 model 内部还带 `train_cfg` / `test_cfg` 两个**模型级**超参块。

##### 核心流程

```text
model.type = VoxelNet（检测器，负责组织前向/损失/推理）
 ├── data_preprocessor : 进模型前的 batch 级处理（含体素化）
 ├── voxel_encoder     : 每个柱体内的点 → 特征向量
 ├── middle_encoder    : 柱体特征 → 伪图像（BEV 网格）
 ├── backbone          : 伪图像 → 多尺度特征（SECOND）
 ├── neck              : 多尺度特征对齐拼接（SECONDFPN）
 ├── bbox_head         : 特征 → 分类/回归/方向预测（Anchor3DHead）
 ├── model.train_cfg   : 训练期超参（正负样本分配器 assigner）
 └── model.test_cfg    : 推理期超参（NMS 阈值、置信度阈值）
```

##### 源码精读

[configs/_base_/models/pointpillars_hv_secfpn_kitti.py:L33-L62](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/pointpillars_hv_secfpn_kitti.py#L33-L62)

```python
    bbox_head=dict(
        type='Anchor3DHead',
        num_classes=3,
        in_channels=384,
        feat_channels=384,
        use_direction_classifier=True,
        assign_per_class=True,
        anchor_generator=dict(
            type='AlignedAnchor3DRangeGenerator',
            sizes=[[0.8, 0.6, 1.73], [1.76, 0.6, 1.73], [3.9, 1.6, 1.56]],
            ...),
```

bbox_head 内部再嵌 `anchor_generator`、`bbox_coder`、`loss_cls`、`loss_bbox`、`loss_dir` 五个孙组件——`dict(type=...)` 的嵌套深度与对象树的深度一一对应。

[configs/_base_/models/pointpillars_hv_secfpn_kitti.py:L64-L98](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/models/pointpillars_hv_secfpn_kitti.py#L64-L98)

```python
    # model training and testing settings
    train_cfg=dict(
        assigner=[
            dict(  # for Pedestrian
                type='Max3DIoUAssigner', ...),
            ...
        ],
        allowed_border=0, ...),
    test_cfg=dict(
        use_rotate_nms=True,
        nms_across_levels=False,
        nms_thr=0.01,
        score_thr=0.1, ...)
```

源码注释 `# model training and testing settings` 明确提醒：这是 **model 内部**的 train_cfg/test_cfg，控制「训练时怎么分配正负样本（assigner 列表按类别各配一个）」「推理时 NMS 怎么做（score_thr=0.1 等）」。它是检测器 `__init__` 的参数，与顶层那个控制 epoch 数的 `train_cfg` 完全是两码事。

##### 小练习与答案

**练习**：`model.train_cfg` 与顶层 `train_cfg = dict(by_epoch=True, max_epochs=80)` 各自被谁消费？

**参考答案**：`model.train_cfg` 传给检测器类（VoxelNet），最终被 dense head 用来做正负样本分配；顶层 `train_cfg` 被 MMEngine 的 Runner 消费，决定训练循环类型（按 epoch）和总轮数。二者仅名字相同。

#### 4.3.2 模块二：data —— dataset / pipeline / dataloader / evaluator

##### 概念说明

数据四件套模板回答四个问题：**读什么数据**（dataset 类 + ann_file）、**怎么加工一条样本**（pipeline，一个 transform 有序列表）、**怎么组成 batch**（dataloader 的 batch_size/采样器/worker 数）、**用什么指标评估**（evaluator）。模板大量使用中间变量来减少重复。

##### 核心流程

```text
train_dataloader
 ├── batch_size / num_workers / persistent_workers / drop_last   ← DataLoader 本身的参数
 ├── sampler = dict(type='DefaultSampler', shuffle=True)         ← 采样器（支持分布式）
 └── dataset = dict(type='RepeatDataset', times=2,
        dataset=dict(type='KittiDataset',
            data_root / ann_file / data_prefix                   ← 读哪份 info pkl、点云在哪个子目录
            pipeline = train_pipeline                            ← transform 有序列表
            metainfo / modality / box_type_3d / test_mode))
val_evaluator = dict(type='KittiMetric', ann_file=..., metric='bbox')
```

##### 源码精读

先看模板头部的元信息变量：

[configs/_base_/datasets/kitti-3d-3class.py:L2-L7](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/datasets/kitti-3d-3class.py#L2-L7)

```python
dataset_type = 'KittiDataset'
data_root = 'data/kitti/'
class_names = ['Pedestrian', 'Cyclist', 'Car']
point_cloud_range = [0, -40, -3, 70.4, 40, 1]
input_modality = dict(use_lidar=True, use_camera=False)
metainfo = dict(classes=class_names)
```

`class_names` 被 `metainfo`、`db_sampler` 等多处引用——换一个类别子集只需要改一行。`input_modality` 声明这份配置只用 LiDAR 不用相机。

再看训练管线（列表的每个元素都是一个 `dict(type=...)` 的 transform）：

[configs/_base_/datasets/kitti-3d-3class.py:L41-L67](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/datasets/kitti-3d-3class.py#L41-L67)

```python
train_pipeline = [
    dict(type='LoadPointsFromFile', coord_type='LIDAR', load_dim=4, use_dim=4, ...),
    dict(type='LoadAnnotations3D', with_bbox_3d=True, with_label_3d=True),
    dict(type='ObjectSample', db_sampler=db_sampler),
    dict(type='ObjectNoise', ...),
    dict(type='RandomFlip3D', flip_ratio_bev_horizontal=0.5),
    dict(type='GlobalRotScaleTrans', ...),
    dict(type='PointsRangeFilter', point_cloud_range=point_cloud_range),
    dict(type='ObjectRangeFilter', point_cloud_range=point_cloud_range),
    dict(type='PointShuffle'),
    dict(type='Pack3DDetInputs', keys=['points', 'gt_bboxes_3d', 'gt_labels_3d'])
]
```

从上到下正是「加载 → 增强 → 过滤 → 打包」的数据流水线（第 3 单元逐个精读，本讲只需看懂结构）。注意 `db_sampler` 是在文件前面定义的另一个 `dict(type=...)`（第 24-39 行），被 `ObjectSample` 引用——GT 采样器自己也是注册模块。

然后是 dataloader 与评估器：

[configs/_base_/datasets/kitti-3d-3class.py:L103-L123](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/datasets/kitti-3d-3class.py#L103-L123)

```python
train_dataloader = dict(
    batch_size=6,
    num_workers=4,
    persistent_workers=True,
    sampler=dict(type='DefaultSampler', shuffle=True),
    dataset=dict(
        type='RepeatDataset',
        times=2,
        dataset=dict(
            type=dataset_type,     # ← 中间变量：'KittiDataset'
            data_root=data_root,
            ann_file='kitti_infos_train.pkl',
            ...
            box_type_3d='LiDAR',
            backend_args=backend_args)))
```

注意 `type=dataset_type`——type 的值也可以来自变量，只要求值结果是字符串。`RepeatDataset` 是数据集包装器：把内层数据集重复 `times=2` 遍，等效放大一个 epoch。

[configs/_base_/datasets/kitti-3d-3class.py:L158-L163](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/datasets/kitti-3d-3class.py#L158-L163)

```python
val_evaluator = dict(
    type='KittiMetric',
    ann_file=data_root + 'kitti_infos_val.pkl',
    metric='bbox',
    backend_args=backend_args)
test_evaluator = val_evaluator
```

评估器同样走注册器（METRICS 节点），`test_evaluator = val_evaluator` 又一次体现「配置即 Python」——直接用变量赋值避免重复。

##### 小练习与答案

**练习 1**：`train_dataloader.dataset` 和 `train_dataloader.dataset.dataset` 分别是什么类型？为什么要包两层？

**参考答案**：外层是 `RepeatDataset`（包装器，把数据集重复 times 遍），内层是 `KittiDataset`（真正读 info pkl 和点云的类）。包装器模式让你不改数据集类就能调整 epoch 大小，`CBGSDataset` 等其他包装器同理（u8-l2 会讲）。

**练习 2**：`test_pipeline` 里出现的 `MultiScaleFlipAug3D` 是做什么的？为什么 `flip=False`？

**参考答案**：它是测试时增强（TTA）的容器，把若干几何变换枚举成多个增强视图；此处 `flip=False` 且 rot/scale 区间都设为 0，等于「不增强」，只是保留统一接口（u7-l4 讲 TTA 时会看到真正开启多视图的配置）。

#### 4.3.3 模块三：schedule —— 优化器与学习率曲线

##### 概念说明

schedule 模板描述「怎么优化」：`optim_wrapper`（优化器包装器，含梯度裁剪、混合精度等能力）、`param_scheduler`（学习率/动量随时间的调度曲线列表）、顶层 `train_cfg`（训练循环配置）。

##### 核心流程

cyclic-40e 采用「两段余弦退火」：前 16 轮学习率从 `lr` 升到 `lr*10`，后 24 轮从 `lr*10` 降到 `lr*1e-4`，动量同步反向调度。余弦退火公式为：

\[
\eta_t = \eta_{\min} + \frac{1}{2}(\eta_{\max} - \eta_{\min})\left(1 + \cos\frac{t}{T}\pi\right)
\]

##### 源码精读

[configs/_base_/schedules/cyclic-40e.py:L5-L11](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/schedules/cyclic-40e.py#L5-L11)

```python
lr = 0.0018
optim_wrapper = dict(
    type='OptimWrapper',
    optimizer=dict(type='AdamW', lr=lr, betas=(0.95, 0.99), weight_decay=0.01),
    clip_grad=dict(max_norm=10, norm_type=2))
```

第 2-4 行注释解释了这里的 `lr=0.0018` 是**初始**学习率而非峰值。`clip_grad` 是梯度裁剪配置。

[configs/_base_/schedules/cyclic-40e.py:L59-L67](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/schedules/cyclic-40e.py#L59-L67)

```python
train_cfg = dict(by_epoch=True, max_epochs=40, val_interval=1)
val_cfg = dict()
test_cfg = dict()

# Default setting for scaling LR automatically
auto_scale_lr = dict(enable=False, base_batch_size=48)
```

这里就是**顶层** `train_cfg`：`by_epoch=True` 表示按 epoch 计数，`max_epochs=40`、每轮验证一次。`auto_scale_lr` 记录了基准 batch size（8 卡 × 6），供按比例缩放学习率用（默认关闭）。

##### 小练习与答案

**练习**：PointPillars 最终配置里 `train_cfg.max_epochs` 是多少？与文件名中的 `160e` 是什么关系？

**参考答案**：`max_epochs=80`（顶层覆盖了模板的 40）。由于 `RepeatDataset(times=2)`，一个「epoch」包含两遍原始训练集，等效于原始数据上的 160 轮，故文件名写 `160e`。

#### 4.3.4 模块四：runtime —— 与算法无关的运行环境

##### 概念说明

runtime 模板集中存放所有「换一个算法也不用改」的设定：默认 scope、默认 Hook 表、多进程/分布式环境、日志处理、断点恢复开关。它被几乎所有实验配置的 `_base_` 最后一项引用。

##### 源码精读

[configs/_base_/default_runtime.py:L1-L9](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/default_runtime.py#L1-L9)

```python
default_scope = 'mmdet3d'

default_hooks = dict(
    timer=dict(type='IterTimerHook'),
    logger=dict(type='LoggerHook', interval=50),
    param_scheduler=dict(type='ParamSchedulerHook'),
    checkpoint=dict(type='CheckpointHook', interval=-1),
    sampler_seed=dict(type='DistSamplerSeedHook'),
    visualization=dict(type='Det3DVisualizationHook'))
```

`default_scope = 'mmdet3d'` 是整个配置的「查找起点」：所有不带前缀的 `type` 都先在 mmdet3d 的注册器（及其父注册器）里找。`default_hooks` 是运行时必备 Hook 的字典，`checkpoint.interval=-1` 表示默认不按间隔存 checkpoint（由具体实验覆盖），`logger.interval=50` 表示每 50 个 iter 打一次日志。

[configs/_base_/default_runtime.py:L11-L21](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/default_runtime.py#L11-L21)

```python
env_cfg = dict(
    cudnn_benchmark=False,
    mp_cfg=dict(mp_start_method='fork', opencv_num_threads=0),
    dist_cfg=dict(backend='nccl'),
)

log_processor = dict(type='LogProcessor', window_size=50, by_epoch=True)

log_level = 'INFO'
load_from = None
resume = False
```

`env_cfg` 控制多进程启动方式与分布式后端；`load_from` 用于加载预训练权重起点（不恢复优化器状态），`resume=True` 则从 `work_dir` 里的最新断点完整恢复训练——这两个键在迁移学习和断点续训时最常被 `--cfg-options` 覆盖。

注意：数据集模板末尾的 `vis_backends` 与 `visualizer`（[configs/_base_/datasets/kitti-3d-3class.py:L165-L167](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/datasets/kitti-3d-3class.py#L165-L167)）也属于运行时要素，只是仓库把它们放在了数据集模板里。

##### 小练习与答案

**练习 1**：想在已有 checkpoint 的基础上继续训练（恢复优化器和轮数），最小改动是什么？

**参考答案**：训练命令加 `--resume`，或 `--cfg-options resume=True load_from=路径`。`resume=True` 时若 `load_from` 为 None，Runner 会自动找 `work_dir` 中最新 checkpoint。

**练习 2**：`default_hooks.checkpoint.interval=-1` 但很多实验仍会周期性保存 checkpoint，为什么？

**参考答案**：具体实验配置（或其 schedule/派生配置）会增量覆盖这个字段，例如设为 1；递归合并只改 `interval` 一个键，其余 Hook 配置继承模板。

### 4.4 命令行覆盖：`--cfg-options` 与 `--work-dir`

#### 4.4.1 概念说明

继承解决「文件之间复用」，但有时只想临时改一两个参数（换个 batch_size、换个输出目录、加载某个权重），为这写一个新配置文件太重。MMEngine 的工具链统一提供命令行覆盖：把 `键路径=值` 解析成嵌套字典后，与配置做一次和继承完全相同的递归合并。这样「改参数」不污染仓库里的任何文件。

#### 4.4.2 核心流程

```text
命令行: --cfg-options train_dataloader.batch_size=2 model.bbox_head.num_classes=2
    │  (argparse 的 DictAction 把 a.b.c=v 解析成 {'a': {'b': {'c': v}}})
    ▼
cfg.merge_from_dict(该字典)      ← 与 _base_ 继承同一套递归合并规则
    ▼
得到「只在本命令中生效」的最终配置
```

work_dir 的决定有明确优先级：**命令行 `--work-dir` > 配置文件里的 `work_dir` 键 > 默认 `./work_dirs/<配置文件名>`**。

#### 4.4.3 源码精读

先看 `tools/train.py` 中参数的定义：

[tools/train.py:L44-L53](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/train.py#L44-L53)

```python
    parser.add_argument(
        '--cfg-options',
        nargs='+',
        action=DictAction,
        help='override some settings in the used config, the key-value pair '
        'in xxx=yyy format will be merged into config file. If the value to '
        'be overwritten is a list, it should be like key="[a,b]" or key=a,b '
        ...
```

help 文本里给出了列表值的写法要点：`key="[a,b]"` 或 `key=a,b`，嵌套列表要加引号且**不允许有空格**。

再看覆盖逻辑本体：

[tools/train.py:L79-L90](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/train.py#L79-L90)

```python
    cfg.launcher = args.launcher
    if args.cfg_options is not None:
        cfg.merge_from_dict(args.cfg_options)

    # work_dir is determined in this priority: CLI > segment in file > filename
    if args.work_dir is not None:
        # update configs according to CLI args if args.work_dir is not None
        cfg.work_dir = args.work_dir
    elif cfg.get('work_dir', None) is None:
        # use config filename as default work_dir if cfg.work_dir is None
        cfg.work_dir = osp.join('./work_dirs',
                                osp.splitext(osp.basename(args.config))[0])
```

第 81 行 `merge_from_dict` 就是覆盖的全部实现；第 83 行注释直接写明优先级。`Config.fromfile` 在第 73 行（见 4.1.3 流程图）已完成继承合并，命令行覆盖发生在其后、`Runner.from_cfg` 之前。

一个小差异要留意：`tools/train.py` / `tools/test.py` 的参数名是 `--cfg-options`，而 `tools/misc/print_config.py` 的参数名是 `--options`（[tools/misc/print_config.py:L10-L11](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/misc/print_config.py#L10-L11)），二者机制相同（都是 `DictAction` + `merge_from_dict`）。

#### 4.4.4 代码实践

**实践目标**：不改任何文件，用命令行修改 `work_dir` 和 batch_size，并验证合并结果。

**操作步骤**（待本地验证）：

1. 先展开原始配置作为基线：

   ```bash
   python tools/misc/print_config.py \
     configs/pointpillars/pointpillars_hv_secfpn_8xb6-160e_kitti-3d-3class.py \
     > /tmp/before.txt
   ```

2. 带 `--options` 再展开一次：

   ```bash
   python tools/misc/print_config.py \
     configs/pointpillars/pointpillars_hv_secfpn_8xb6-160e_kitti-3d-3class.py \
     --options work_dir=./work_dirs/pp_bs2 train_dataloader.batch_size=2 \
     > /tmp/after.txt
   ```

3. 对比两份文件：

   ```bash
   diff /tmp/before.txt /tmp/after.txt
   ```

**需要观察的现象**：

- diff 只有寥寥几行：`work_dir = './work_dirs/pp_bs2'` 是**新增**的键（原配置里没有 work_dir，`merge_from_dict` 直接创建）；`train_dataloader` 段的 `batch_size` 从 6 变成 2；
- `train_dataloader` 的其余字段（`num_workers`、`sampler`、`dataset` 整棵子树）与基线完全一致——命令行覆盖与继承共用同一套递归合并规则。

**预期结果**：确认「命令行覆盖 = 一次字典深合并」，并理解为什么它不会破坏配置的其他部分。

#### 4.4.5 小练习与答案

**练习 1**：如何用一条命令同时把训练轮数减到 2、学习率降到 1e-4？

**参考答案**：

```bash
python tools/train.py configs/pointpillars/pointpillars_hv_secfpn_8xb6-160e_kitti-3d-3class.py \
  --cfg-options train_cfg.max_epochs=2 optim_wrapper.optimizer.lr=0.0001
```

注意嵌套路径用点号连接，且这里是 `tools/train.py`，参数名是 `--cfg-options`。

**练习 2**：执行 `tools/train.py xxx.py` 时不指定 `--work-dir`，日志和 checkpoint 会存在哪里？依据是什么？

**参考答案**：默认在 `./work_dirs/xxx/`（xxx 为配置文件名去掉后缀）。依据是 [tools/train.py:L87-L90](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/train.py#L87-L90) 的兜底分支：配置里没有 `work_dir` 键时用配置文件名拼出默认路径。

## 5. 综合实践

**任务：制作 PointPillars KITTI 配置的「继承链追踪表」。**

这个任务把本讲四个模块串起来：读懂 `_base_`、理解合并规则、识别四要素、验证命令行覆盖。

**操作步骤**（待本地验证）：

1. 展开完整配置：

   ```bash
   python tools/misc/print_config.py \
     configs/pointpillars/pointpillars_hv_secfpn_8xb6-160e_kitti-3d-3class.py \
     > /tmp/pp_full.txt
   ```

2. 在 `/tmp/pp_full.txt` 中定位三大块并回答问题：
   - `model` 块：列出全部 6 个子组件的 `type` 字符串，用 `grep class Xxx -r mmdet3d/` 找到每个类的源码文件（u1-l4 的反查法）；
   - `train_dataloader` 块：写出完整的嵌套结构（dataloader → RepeatDataset → KittiDataset），并数一数 `train_pipeline` 有几个 transform；
   - `val_evaluator` 块：说出它的类型与 `metric` 取值。
3. 制作追踪表：对下列每个最终值，标注它「来自哪个 `_base_` 文件、是否被顶层覆盖」：
   - `model.data_preprocessor.voxel_layer.voxel_size`
   - `train_dataloader.batch_size`
   - `train_dataloader.dataset.dataset.pipeline`（提示：顶层整体替换了模板的列表）
   - `optim_wrapper.optimizer.type` 与 `optim_wrapper.optimizer.lr`
   - `train_cfg.max_epochs`（顶层）与 `model.train_cfg.assigner`（模型内）
   - `default_hooks.logger.interval`
4. 用命令行覆盖验证你的表格：

   ```bash
   python tools/misc/print_config.py \
     configs/pointpillars/pointpillars_hv_secfpn_8xb6-160e_kitti-3d-3class.py \
     --options work_dir=./work_dirs/my_exp train_dataloader.batch_size=3 \
     default_hooks.logger.interval=10
   ```

   确认只有这三处变化，其余输出与第 1 步完全一致。

**预期结果**：一张 8 行左右的表格 + 一段 diff 验证记录。完成后，你对「配置文件 → 继承合并 → 命令行覆盖 → 待实例化对象树」的完整链路就有了可复用的分析方法，后续读任何算法的配置都能套用。

## 6. 本讲小结

- 配置文件是普通 Python：`Config.fromfile` 执行文件、收集所有模块级变量；`dict(type='类名', ...)` 是描述一切可实例化模块的统一语法，`type` 由注册器路由，`mmdet.` 前缀表示跨库借用。
- `_base_` 四件套按 models / datasets / schedules / runtime 切分职责；合并规则是「dict 递归合并、list 整体替换、后继承者覆盖先继承者」，且**先求值后合并**——中间变量不跨文件共享。
- model 块是一棵 `dict(type=...)` 嵌套的对象树（VoxelNet → 六大组件 → 更深的 loss/coder/assigner）；**model 内部的 `train_cfg`（assigner）与顶层的 `train_cfg`（epoch 数）是两个无关的同名键**。
- schedule 块由 `optim_wrapper`（含梯度裁剪）+ `param_scheduler`（两段余弦退火）+ 顶层 `train_cfg` 组成；PointPillars 把 40 轮模板改写为 80 轮 × RepeatDataset(2) = 等效 160 轮。
- runtime 块（default_scope、default_hooks、env_cfg、load_from/resume）与算法无关、几乎全库共用；`--cfg-options` 与 `--work-dir` 通过 `merge_from_dict` 和显式优先级（CLI > 配置文件 > 文件名默认）实现免改文件的临时覆盖。

## 7. 下一步学习建议

本讲结束了对「实验如何被描述」的讨论。下一讲 **u2-l1 三大坐标系与 Box3DMode/Coord3DMode** 将进入数据结构层：LiDAR / Camera / Depth 三个坐标系的方向约定，以及 3D 框和点云如何在坐标系之间转换——这是理解第 3 单元数据管线（例如本讲出现的 `coord_type='LIDAR'`、`box_type_3d='LiDAR'` 参数）的必要前提。

继续阅读建议：

- 把本讲的追踪表方法用在 `configs/_base_/models/second_hv_secfpn_kitti.py` 上，对比它与 PointPillars 模型模板的异同；
- 阅读 [docs/en/user_guides/config.md](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docs/en/user_guides/config.md) 剩余部分，其中「Config file inheritance」一节有更多 `_base_` 用法示例；
- 有兴趣可延伸读 MMEngine 的 [Config 教程](https://mmengine.readthedocs.io/en/latest/advanced_tutorials/config.html)，了解 `_delete_=True`（删除继承键而非合并）等本讲未涉及的高级语法。
