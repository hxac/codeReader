# 高阶推理：Inferencer 体系与 TTA

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `Base3DInferencer` 如何仅凭「一个 config 文件 + 一个 checkpoint」组装出完整的推理服务（建模型、载权重、裁剪测试管线、恢复类别与调色板元信息）。
2. 对比 LiDAR / 单目 / 多模态 / LiDAR 分割四类 Inferencer 各自对测试管线做了哪些「外科手术」。
3. 解释测试时增强（Test-Time Augmentation，TTA）的完整数据链：`MultiScaleFlipAug3D` 枚举增强视图 → 模型 `aug_test` 逐视图推理 → `merge_aug_bboxes_3d` 把各视图预测还原、拼接、逐类 NMS 去重。
4. 区分两套 TTA 配置写法：检测类（CenterPoint flip-tta，直接覆写 `test_pipeline`）与分割类（`tta_pipeline` + `tta_model`，经 `tools/test.py --tta` 启用）。
5. 辨析三个名字容易混淆的包装器：`MultiScaleFlipAug3D`（TTA 视图枚举）、mmcv 的 `TestTimeAug`（通用笛卡尔积 TTA）、`MultiViewWrapper`（多相机决策复用，与 TTA 无关）。

## 2. 前置知识

**Inferencer 是什么。** [u1-l3](u1-l3-first-inference-demo.md) 已经用过底层 API `init_model` + `inference_detector`：自己加载配置、自己建模型、自己准备数据。Inferencer 是在这之上的「一站式」封装——把配置解析、权重下载、数据预处理、前向、可视化、结果落盘全部装进一个可调用对象。它来自 MMEngine 的 `BaseInferencer` 框架（外部库），该框架约定了四段式流水线：

```
__call__ = 循环 { preprocess → forward → visualize → postprocess }
```

MMEngine 侧负责模板方法与参数分发；mmdet3d 侧（本讲主角）负责重写每个环节的 3D 细节。

**TTA 的直觉。** 训练时我们用 `RandomFlip3D`、`GlobalRotScaleTrans` 等随机增强让模型见过物体的各种「变体」；测试时也可以反过来利用这一点：把一帧点云变成 \(K\) 个**确定性**变体（原始、水平翻转、垂直翻转、双向翻转），各自推理一遍。同一个真实物体会在多个视图里被检出，把这些预测「翻译」回原始坐标系后合并去重，往往比单次推理更稳。

**为什么预测能被「翻译」回去。** 因为翻转和缩放都是可逆的刚体/相似变换，且翻转是对合（involution）变换：

\[ F_h \circ F_h = I, \qquad S_{1/s} \circ S_s = I \]

水平翻转做两次等于没做，缩放 \(s\) 倍后再缩放 \(1/s\) 倍也回到原状。所以只要记住每个视图「翻转了没有、缩放了多少」——也就是 [u3-l6](u3-l6-3d-data-augmentation.md) 讲过的三个审计键 `pcd_horizontal_flip`、`pcd_vertical_flip`、`pcd_scale_factor`——就能把任意视图里的预测框映射回原始坐标系。这正是 TTA 合并的全部几何基础。

**本讲需要的前置讲义**：[u7-l1](u7-l1-training-loop.md)（config 与 Runner 的关系）、[u7-l2](u7-l2-testing-and-metrics.md)（`tools/test.py` 是薄入口、score 阈值只影响可视化不影响 mAP）、[u1-l5](u1-l5-config-system-basics.md)（配置继承）、[u3-l8](u3-l8-pack3d-det-inputs.md)（`Pack3DDetInputs` 的打包路由）、[u5-l7](u5-l7-3d-losses-and-nms.md)（`nms_bev` / `nms_normal_bev` 两种 BEV NMS）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `mmdet3d/apis/inferencers/base_3d_inferencer.py` | 全部 3D Inferencer 的基类：模型构建、权重加载、主调用循环、结果序列化 |
| `mmdet3d/apis/inferencers/lidar_det3d_inferencer.py` | LiDAR 检测 Inferencer：替换点云加载器、重读数据做可视化 |
| `mmdet3d/apis/inferencers/mono_det3d_inferencer.py` | 单目检测 Inferencer：从 info pkl 提取标定矩阵 |
| `mmdet3d/apis/inferencers/multi_modality_det3d_inferencer.py` | 多模态检测 Inferencer：同时要求点云与图像加载器 |
| `mmdet3d/apis/inferencers/lidar_seg3d_inferencer.py` | LiDAR 分割 Inferencer：删除真值相关 transform |
| `mmdet3d/datasets/transforms/test_time_aug.py` | `MultiScaleFlipAug3D`：检测 TTA 的管线端，枚举增强视图 |
| `mmdet3d/models/test_time_augs/merge_augs.py` | `merge_aug_bboxes_3d`：TTA 的合并端 |
| `mmdet3d/structures/ops/transforms.py` | `bbox3d_mapping_back`：把增强坐标系里的框映射回原始尺度 |
| `mmdet3d/models/detectors/base.py` | `Base3DDetector.forward`：识别嵌套 data_samples 并分流到 `aug_test` |
| `mmdet3d/models/detectors/votenet.py` / `mmdet3d/models/dense_heads/anchor3d_head.py` | 仓库内两处显式的 `aug_test` 实现 |
| `mmdet3d/models/segmentors/seg3d_tta.py` | `Seg3DTTAModel`：分割 TTA 的合并策略（softmax 平均） |
| `configs/centerpoint/centerpoint_voxel0075_second_secfpn_head-dcn-circlenms_8xb4-flip-tta-cyclic-20e_nus-3d.py` | 检测 TTA 配置范例：覆写 `test_pipeline` |
| `configs/_base_/datasets/semantickitti.py` | 分割 TTA 配置范例：`tta_pipeline` + `tta_model` |
| `demo/pcd_demo.py` | LiDAR 检测 Inferencer 的命令行入口 |
| `tests/test_apis/test_inferencers/test_lidar_det3d_inferencer.py`、`tests/test_datasets/test_tta.py` | 本讲两个实践的依据 |

## 4. 核心概念与源码讲解

### 4.1 Base3DInferencer：config + checkpoint 驱动的推理引擎

#### 4.1.1 概念说明

`Base3DInferencer` 解决的问题是：**用户只想说「用哪个配置、哪份权重、对哪些数据推理」，其余全部自动**。它不训练、不评估，只做推理，因此初始化逻辑与 `tools/test.py`（[u7-l2](u7-l2-testing-and-metrics.md)）不同：

- `test.py` 走 Runner，需要完整的数据集、评估器配置；
- Inferencer 只需要 `cfg.model` 与 `cfg.test_dataloader.dataset.pipeline` 两块，其余配置（训练日程、评估器等）一律忽略。

子类家族在 [mmdet3d/apis/inferencers/__init__.py:L2-L11](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/apis/inferencers/__init__.py#L2-L11) 中导出，共 5 个：基类 + LiDAR 检测 / 单目检测 / 多模态检测 / LiDAR 分割四个具体类。

#### 4.1.2 核心流程

`Base3DInferencer` 的生命周期分两个阶段。

**初始化阶段（构造函数触发，MMEngine 模板方法逐个回调）**：

```
__init__
 ├─ init_default_scope('mmdet3d')          # 让后续 MODELS.build 能路由
 ├─ super().__init__(model, weights, ...)  # mmengine 模板：
 │    ├─ _init_model    ← mmdet3d 重写
 │    ├─ _init_pipeline ← 子类重写（4.2 讲）
 │    └─ _init_visualizer ← mmdet3d 重写
 └─ revert_sync_batchnorm(model)           # 单卡推理不需要 SyncBN
```

**调用阶段（每次 `inferencer(inputs)`）**，见 [mmdet3d/apis/inferencers/base_3d_inferencer.py:L173-L221](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/apis/inferencers/base_3d_inferencer.py#L173-L221)：

```
__call__(inputs, batch_size, return_datasamples, **kwargs)
 ├─ _dispatch_kwargs(**kwargs)      # 把散装关键字参数分拣到四个阶段
 ├─ _inputs_to_list(inputs)         # 子类实现：目录展开、标定注入
 ├─ preprocess(list)                # 用裁剪后的 pipeline 批量打包
 └─ for data in track(inputs):      # rich 进度条
      ├─ forward(data)              # 内部走 model.test_step（mmengine 提供）
      ├─ visualize(ori_inputs, preds)
      └─ postprocess(preds, vis)    # 序列化 / 落盘 / 打印
```

参数如何「分拣」？类属性 `preprocess_kwargs / forward_kwargs / visualize_kwargs / postprocess_kwargs` 四个集合（[L51-L59](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/apis/inferencers/base_3d_inferencer.py#L51-L59)）就是白名单：比如 `pred_score_thr` 属于 `visualize_kwargs`，`print_result` 属于 `postprocess_kwargs`。这是「装饰器式的参数路由」，新增可配参数时只需把名字加进对应集合。

#### 4.1.3 源码精读

**（1）构造与 SyncBN 降级**。[L61-L74](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/apis/inferencers/base_3d_inferencer.py#L61-L74)：先 `init_default_scope(scope)` 再调父类构造，最后 `revert_sync_batchnorm` 把 SyncBN 换回普通 BN——SyncBN 在单卡/单进程前向里没有意义且可能报错。

**（2）`_init_model`：五步装配**。[L91-L130](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/apis/inferencers/base_3d_inferencer.py#L91-L130) 是本模块的核心，按行拆解：

```python
self._convert_syncbn(cfg.model)   # 递归把 norm_cfg 里的 naiveSyncBN 替换成 BN
cfg.model.train_cfg = None        # 推理不需要 assigner/sampler，直接摘除
model = MODELS.build(cfg.model)   # 注册器一行滚出整棵模型树（见 u4-l1）
checkpoint = load_checkpoint(model, weights, map_location='cpu')
```

随后是一段**版本兼容的元信息恢复**（[L102-L117](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/apis/inferencers/base_3d_inferencer.py#L102-L117)）：1.x 的 checkpoint 把 `dataset_meta` 存进 `meta`；0.x 的旧权重只有 `CLASSES`/`PALETTE`，需要手工包成 `{'classes': ...}`。这些信息决定可视化时类别名与颜色的来源（呼应 [u7-l3](u7-l3-visualization.md)：3D 框颜色唯一来源是数据集 palette）。

**调色板的三级优先级**在 [L119-L125](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/apis/inferencers/base_3d_inferencer.py#L119-L125)：构造测试数据集但设 `lazy_init=True`——**不读 ann_file、不建管线，只为拿 `metainfo` 里的 palette**，用配置里的 palette 覆盖 checkpoint 里的。这正是 docstring 所说优先级 `palette 参数 → config → checkpoint` 的后两级。

**（3）`_dispatch_kwargs` 与 `out_dir` 拆分**。[L147-L171](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/apis/inferencers/base_3d_inferencer.py#L147-L171)：用户只传一个 `out_dir`，基类把它复制成 `img_out_dir`（可视化目录）与 `pred_out_dir`（预测 json 目录）两个字段分发给不同阶段。

**（4）`postprocess` 与 `pred2dict`：结果如何变成 json**。[L223-L286](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/apis/inferencers/base_3d_inferencer.py#L223-L286)：若 `return_datasample=False`（默认），逐条调 `pred2dict` 把 [u2-l4](u2-l4-det3d-data-sample.md) 讲过的 `Det3DDataSample` 压成纯 Python 类型。[L290-L346](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/apis/inferencers/base_3d_inferencer.py#L290-L346) 的转换规则：

- `pred_instances_3d` → `labels_3d` / `scores_3d` / `bboxes_3d` 三个列表；
- `pred_pts_seg`（分割）→ `pts_semantic_mask` 逐点标签列表；
- `box_mode_3d` → 字符串 `'LiDAR'` / `'Camera'` / `'Depth'`（序列化框对象本身没有坐标系信息，必须显式记录，否则 json 无法还原）；
- 落盘路径为 `<out_dir>/preds/<输入文件名>.json`。

注意 [L275-L280](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/apis/inferencers/base_3d_inferencer.py#L275-L280) 的取舍：`return_datasample=True` 与 `pred_out_dir` 互斥——`Det3DDataSample` 里含 tensor 与框对象，不是 json 可序列化的，想落盘就得压成 dict，两者不可兼得。

#### 4.1.4 代码实践

**实践目标**：验证 `_init_model` 的元信息恢复链——亲手检查一份 checkpoint 的 `meta` 里到底存了什么。

1. 操作步骤（源码阅读型 + 一小段本地脚本，权重较大可跳过下载只做步骤 3）：
   - 阅读 [L102-L125](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/apis/inferencers/base_3d_inferencer.py#L102-L125)，列出 `dataset_meta` 的三条来源分支。
   - 若已按 [u1-l3](u1-l3-first-inference-demo.md) 下载过 PointPillars 权重，运行下面脚本（示例代码）：

   ```python
   # 示例代码：检查 checkpoint 的 meta 结构
   import torch
   ckpt = torch.load('<你的 pointpillars .pth 路径>', map_location='cpu')
   print(ckpt['meta'].keys())          # 期望包含 dataset_meta（1.x 权重）
   print(ckpt['meta']['dataset_meta']) # 期望包含 classes 与 palette
   ```

   - 对照 `configs/_base_/datasets/kitti-3d-3class.py` 中 `metainfo` 的 `CLASSES` 与 `PALETTE`，确认两者一致。
2. 需要观察的现象：`meta` 里 `dataset_meta['classes']` 是 3 类（Car/Pedestrian/Cyclist），palette 是三组 RGB。
3. 预期结果：能说清「可视化时类别名和颜色是谁提供的」这条链：checkpoint meta → `model.dataset_meta` → `_init_visualizer`（[L142-L145](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/apis/inferencers/base_3d_inferencer.py#L142-L145)）→ `visualizer.dataset_meta`。
4. 若无权重或无 GPU 环境，前两步为「待本地验证」；源码阅读部分不依赖运行。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `_init_model` 里要执行 `cfg.model.train_cfg = None`？
**答案**：`train_cfg` 里装的是 assigner、sampler、sampler 等只在计算训练目标时用的配置（见 [u5-l6](u5-l6-coders-assigners-samplers.md)）；推理时若保留，`MODELS.build` 仍会构建这些组件，白白实例化一批用不到的对象，某些头还会因 train_cfg 与 test_cfg 同时存在走不同初始化分支，置 `None` 保证模型按纯推理形态构建。

**练习 2**：`_convert_syncbn` 为什么要自己写递归，而不是像构造函数里那样直接用 `mmengine.model.utils.revert_sync_batchnorm`？
**答案**：两处解决两个不同层面的问题。`revert_sync_batchnorm`（[L74](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/apis/inferencers/base_3d_inferencer.py#L74)）作用于**已构建的模型对象**，把模块树里的 SyncBN 实例换成 BN；而 `_convert_syncbn`（[L76-L89](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/apis/inferencers/base_3d_inferencer.py#L76-L89)）作用于**配置字典**，在 `MODELS.build` 之前递归把每个 `norm_cfg` 的 `type` 字符串从 `naiveSyncBN` 改成 `BN`——字符串替换发生在「配置 → 类」路由之前，从源头避免构建出 SyncBN。

**练习 3**：构造测试数据集时为什么加 `test_dataset_cfg['lazy_init'] = True`？
**答案**：这里只需要数据集类身上的 `metainfo`（palette 等约定，见 [u3-l1](u3-l1-det3d-dataset-base.md) 的 METAINFO 机制），并不需要真正读取 info pkl、构建数据管线。`lazy_init=True` 让 `BaseDataset.__init__` 跳过读文件与建管线，把一次可能很重的磁盘 IO 变成纯内存查表。

### 4.2 四个具体 Inferencer：对测试管线做「外科手术」

#### 4.2.1 概念说明

四个子类共享基类的全部骨架，各自只重写三件事：**输入如何变成列表**（`_inputs_to_list`）、**测试管线如何改造**（`_init_pipeline`）、**结果如何画出来**（`visualize`）。核心矛盾在于：配置里的测试管线是为**数据集**设计的——它从 info pkl 拿路径、读标定、加载真值标注；而 Inferencer 的输入是**裸文件路径或 numpy 数组**，没有 info pkl、没有真值。子类的职责就是把管线里「读不到的那几个 transform」替换或删掉，其余原样保留。

#### 4.2.2 核心流程

以 LiDAR 检测为例的管线手术（[mmdet3d/apis/inferencers/lidar_det3d_inferencer.py:L102-L119](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/apis/inferencers/lidar_det3d_inferencer.py#L102-L119)）：

```
配置里的 test_pipeline:                     Inferencer 实际使用的 pipeline:
  LoadPointsFromFile        ──替换──▶  LidarDet3DInferencerLoader
  LoadPointsFromMultiSweeps              LoadPointsFromMultiSweeps（不变）
  GlobalRotScaleTrans(恒等)             GlobalRotScaleTrans(恒等)（不变）
  PointsRangeFilter                     PointsRangeFilter（不变）
  Pack3DDetInputs                       Pack3DDetInputs（不变）
```

替换前顺手「偷」走 `coord_type / load_dim / use_dim` 三个参数存到 `self` 上——可视化阶段要按同样的列定义重读点云（见 4.2.3 第 3 点）。

#### 4.2.3 源码精读

**（1）注册名与别名**。[L28-L30](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/apis/inferencers/lidar_det3d_inferencer.py#L28-L30) 装饰器注册了**两个名字**：`'det3d-lidar'`（CLI/字符串检索用）与默认类名（[u4-l1](u4-l1-registry-mechanism.md) 讲过的别名注册）。这就是测试里 `LidarDet3DInferencer('pointpillars_kitti-3class')` 能从 metafile 别名初始化的前提。

**（2）输入归一化**。[L67-L100](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/apis/inferencers/lidar_det3d_inferencer.py#L67-L100)：`dict(points='目录')` 会经文件后端展开成该目录下全部文件；单个路径、numpy 数组、list 则各自包一层。测试 [tests/test_apis/test_inferencers/test_lidar_det3d_inferencer.py:L43-L84](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tests/test_apis/test_inferencers/test_lidar_det3d_inferencer.py#L43-L84) 明确验证了「路径输入与 ndarray 输入给出一致预测」「目录输入可配 batch_size」两件事。

**（3）可视化为什么要重读一遍点云**。[L121-L205](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/apis/inferencers/lidar_det3d_inferencer.py#L121-L205)：`forward` 消费的是 pipeline 产物（张量、被裁剪过的列、可能被洗牌的点序），而 [u7-l3](u7-l3-visualization.md) 讲过 `Det3DLocalVisualizer.add_datasample` 需要原始 numpy 点云。于是 `visualize` 对字符串输入用 `np.frombuffer` 按 `load_dim/use_dim` 重读 .bin（[L169-L173](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/apis/inferencers/lidar_det3d_inferencer.py#L169-L173)），最后以 `vis_task='lidar_det'` 调用可视化器，文件落在 `<out_dir>/vis_lidar/<名字>.png`。

**（4）从 json 恢复预测**。[L207-L242](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/apis/inferencers/lidar_det3d_inferencer.py#L207-L242) 的 `visualize_preds_fromfile` 是 `pred2dict` 的逆操作：读回 json，按 `box_type_3d` 字符串把裸数组重新包装成 `LiDARInstance3DBoxes` / `CameraInstance3DBoxes` / `DepthInstance3DBoxes`（4.1.3 第 4 点记录的 `box_type_3d` 字段在此刻派上用场），实现「离线补画」。

**（5）其余三个子类的差异**：

| 子类 | 管线手术 | 额外输入要求 |
| --- | --- | --- |
| `MonoDet3DInferencer` | `LoadImageFromFileMono3D` → `MonoDet3DInferencerLoader`（[mono_det3d_inferencer.py:L160-L170](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/apis/inferencers/mono_det3d_inferencer.py#L160-L170)） | 必须给 `infos` pkl；从每条 info 里提取 `cam2img`、`lidar2cam`，缺 `lidar2img` 时用 \(P_{lidar2img} = K \cdot T_{lidar2cam}\) 兜底（[L86-L158](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/apis/inferencers/mono_det3d_inferencer.py#L86-L158)），并校验图像文件名与 info 对应 |
| `MultiModalityDet3DInferencer` | 同时要求 `LoadPointsFromFile` 与 `LoadImageFromFile`；遇 `LoadMultiViewImageFromFiles` 直接告警要求移除（[multi_modality_det3d_inferencer.py:L167-L197](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/apis/inferencers/multi_modality_det3d_inferencer.py#L167-L197)） | 点云 + 单目图像成对输入 |
| `LidarSeg3DInferencer` | **删除** `LoadAnnotations3D` 与 `PointSegClassMapping`（[lidar_seg3d_inferencer.py:L99-L113](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/apis/inferencers/lidar_seg3d_inferencer.py#L99-L113)） | 无——裸点云即可 |

分割子类的「删除」值得体会：真值分割掩码（`pts_semantic_mask`）与类别重映射（`seg_label_mapping`，见 [u3-l4](u3-l4-indoor-datasets.md)）在推理时既没有也不需要，与其替换成假数据不如直接摘除。

#### 4.2.4 代码实践

**实践目标**：用 `demo/pcd_demo.py`（内部即 `LidarDet3DInferencer`）对 demo 点云推理，落盘预测 json 并解读其字段。

1. 操作步骤（命令行，GPU 环境下执行；权重 URL 与仓库测试用的一致）：

   ```bash
   python demo/pcd_demo.py \
       demo/data/kitti/000008.bin \
       configs/pointpillars/pointpillars_hv_secfpn_8xb6-160e_kitti-3d-3class.py \
       https://download.openmmlab.com/mmdetection3d/v1.0.0_models/pointpillars/hv_pointpillars_secfpn_6x8_160e_kitti-3d-3class/hv_pointpillars_secfpn_6x8_160e_kitti-3d-3class_20220301_150306-37dc2420.pth \
       --device cuda:0
   ```

   该脚本（[demo/pcd_demo.py:L11-L72](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/demo/pcd_demo.py#L11-L72)）把命令行参数拆成 `init_args`（model/weights/device）与 `call_args`（其余），正是 4.1.2 里「初始化参数 vs 调用参数」两套体系（[L75-L83](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/demo/pcd_demo.py#L75-L83)）。

2. 然后检查输出（示例代码）：

   ```python
   # 示例代码：读取落盘的预测 json
   import json
   pred = json.load(open('outputs/preds/000008.json'))
   print(pred.keys())          # 期望: bboxes_3d, scores_3d, labels_3d, box_type_3d
   print(len(pred['scores_3d']))           # 全部预测框数（未经 score 阈值过滤）
   print(pred['box_type_3d'])              # 期望: 'LiDAR'
   print(pred['bboxes_3d'][0])             # 一个 7 自由度框（u2-l2）
   ```

3. 需要观察的现象：`outputs/` 下出现 `preds/000008.json`（`vis_lidar/` 目录仅在 `--show` 可用时落盘，见测试中 [L91-L95](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tests/test_apis/test_inferencers/test_lidar_det3d_inferencer.py#L91-L95) 的 TODO 注释）；json 里框数明显多于「看起来合理的目标数」。
4. 预期结果：能解释第 3 点——`pred_score_thr`（默认 0.3）只影响 `visualize` 画哪些框（[L121-L130](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/apis/inferencers/lidar_det3d_inferencer.py#L121-L130)），json 保存的是 NMS 后全部预测，这呼应 [u7-l2](u7-l2-testing-and-metrics.md) 的结论「可视化阈值不影响 mAP」。
5. 本实践需要 GPU 与网络下载权重，结果数值属于「待本地验证」；但输出目录结构与 json 字段名可直接从源码推出。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `MonoDet3DInferencer` 必须要求用户额外提供 info pkl，而 `LidarDet3DInferencer` 不用？
**答案**：单目 3D 检测的前向与后处理都需要相机标定——head 解码深度、把 3D 框投影回图像（见 [u6-l5](u6-l5-mono3d-detectors.md)）离不开 `cam2img`/`lidar2img`，而这些矩阵只存在于数据集的 info pkl 中（[u3-l2](u3-l2-data-preparation-info-pkl.md)：converter 离线算好、在线轻取）。纯 LiDAR 检测在雷达坐标系内自洽，点云文件本身就是全部输入。

**练习 2**：`MultiModalityDet3DInferencer` 为什么对 `LoadMultiViewImageFromFiles` 发出 warning 而不是直接替换它？
**答案**：多视角加载器一次读 6 相机图像并按相机维度堆叠（[u3-l5](u3-l5-loading-transforms.md)），而 Inferencer 的输入约定是「单张图像 + 对应点云」的路径对；为多视角输入设计通用的裸路径加载器需要同时改输入协议与可视化协议，当前版本尚未支持，因此用 warning 提示用户改用单目图像加载器。

**练习 3**：若想给 `LidarDet3DInferencer` 增加「输入 .pcd 文件」的能力，应该改哪里？
**答案**：改 `LidarDet3DInferencerLoader`（`mmdet3d/datasets/transforms/loading.py`，[u1-l3](u1-l3-first-inference-demo.md) 已见过它接受裸路径或 numpy）让它在后缀为 `.pcd` 时走 pcd 读取分支；同时 `visualize`（[L168-L173](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/apis/inferencers/lidar_det3d_inferencer.py#L168-L173)）里重读文件那段也要加对应分支——两处必须同步，否则可视化与推理读到不一致的数据。

### 4.3 MultiScaleFlipAug3D：检测 TTA 的管线端

#### 4.3.1 概念说明

TTA 在 mmdet3d 中由三个角色配合完成，本模块讲第一个——**管线端的视图枚举器** `MultiScaleFlipAug3D`（[mmdet3d/datasets/transforms/test_time_aug.py:L13-L14](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/test_time_aug.py#L13-L14) 注册）。

它的设计哲学是：**自己不做任何几何变换，只做「决策枚举 + 决策注入」**。真正的翻转/缩放仍由普通训练增强类（`RandomFlip3D`、`GlobalRotScaleTrans`）执行——TTA 视图与训练增强走完全相同的代码路径，不会引入两套几何实现不一致的 bug。

实现手段是 [u3-l6](u3-l6-3d-data-augmentation.md) 已埋下的伏笔：增强类「若 results 里已有决策键则服从、否则随机」。TTA 包装器把每个视图的决策**预先写进 results**，再让同一段增强管线按给定决策执行。

#### 4.3.2 核心流程

`transform`（[L69-L112](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/test_time_aug.py#L69-L112)）就是六重循环做笛卡尔积：

```
视图总数 K = |img_scale| × |pts_scale_ratio| × |flip_aug|
             × |pcd_horizontal_flip_aug| × |pcd_vertical_flip_aug| × |flip_direction|

对每个组合:
    _results = deepcopy(results)        # 浅拷贝会共享底层 buffer，必须深拷贝
    _results['scale']             = scale
    _results['flip']              = flip
    _results['pcd_scale_factor']  = pts_scale_ratio
    _results['flip_direction']    = direction
    _results['pcd_horizontal_flip'] = pcd_horizontal_flip
    _results['pcd_vertical_flip']   = pcd_vertical_flip
    data = self.transforms(_results)    # 注入决策后跑增强子管线
    aug_data_list.append(data)
返回 List[dict]                          # K 个视图
```

两个容易忽略的细节：

- **枚举表不包含冗余原视角**：[L81-L88](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/test_time_aug.py#L81-L88) 的注释说明 `flip_aug` 从 `[False, True]` 改成了 `flip 时 [True]`——因为 `pcd_horizontal_flip_aug` 与 `pcd_vertical_flip_aug` 的枚举里已经各含 `False`，组合出的 \((h,v)=(F,F)\) 就是原视角，若 `flip_aug` 再含 `False` 会算出两个一模一样的原视角。
- **决策注入要求下游「配合演出」**：若 `transforms` 里没有 `RandomFlip3D`/`RandomFlip`，预写的 flip 键没人消费，翻转就悄悄失效——构造函数在 [L60-L67](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/test_time_aug.py#L60-L67) 对这两种误用发出 warning。

**CenterPoint flip-tta 的视图数演算**：配置 [configs/centerpoint/centerpoint_voxel0075_second_secfpn_head-dcn-circlenms_8xb4-flip-tta-cyclic-20e_nus-3d.py:L13-L46](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/centerpoint/centerpoint_voxel0075_second_secfpn_head-dcn-circlenms_8xb4-flip-tta-cyclic-20e_nus-3d.py#L13-L46) 里 `img_scale` 单值、`pts_scale_ratio=1` 单值、`flip=True`、`pcd_horizontal_flip=True`、`pcd_vertical_flip=True`、`flip_direction` 默认单值，故

\[ K = 1 \times 1 \times 1 \times 2 \times 2 \times 1 = 4 \]

四个视图恰为 \((h,v) \in \{(F,F),(F,T),(T,F),(T,T)\}\)，即原始、垂直翻转、水平翻转、双向翻转（double flip）。注意其子管线的设置：`GlobalRotScaleTrans` 的三个范围全部压成恒等（`rot_range=[0,0]`、`scale_ratio_range=[1,1]`、`translation_std=[0,0,0]`）——原因见 4.4.1：合并端只能还原 flip 与 scale，旋转/平移不可还原，因此检测 TTA 不做旋转增强。

**模型端如何被触发**。K 个视图经 `Pack3DDetInputs` 逐个打包成 K 个 `dict(inputs, data_samples)`（[mmdet3d/datasets/transforms/formating.py:L109-L116](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/formating.py#L109-L116) 的 augtest 分支：list 输入逐条 `pack_single_results`；单元素 list 退化为普通打包）。于是 `Base3DDetector.forward` 在 `mode='predict'` 时看到的 `data_samples[0]` 是 **list**，据此分流到 `aug_test` 并断言 batch_size 为 1（[mmdet3d/models/detectors/base.py:L77-L84](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/base.py#L77-L84)）——TTA 一次只处理一个样本的 K 个视图。

#### 4.3.3 源码精读

**（1）`RandomFlip3D` 如何「服从」预写决策**。[mmdet3d/datasets/transforms/transforms_3d.py:L223-L249](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/transforms_3d.py#L223-L249)：

```python
if self.sync_2d and 'img' in input_dict:          # 2D/3D 同步翻转（u3-l6）
    input_dict['pcd_horizontal_flip'] = input_dict['flip']
    input_dict['pcd_vertical_flip'] = False
else:
    if 'pcd_horizontal_flip' not in input_dict:   # ← 已预写则跳过随机
        input_dict['pcd_horizontal_flip'] = <按概率抽样>
    if 'pcd_vertical_flip' not in input_dict:     # ← 同上
        input_dict['pcd_vertical_flip'] = <按概率抽样>
```

配合基类方法 `_flip_on_direction`（[L184-L209](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/transforms_3d.py#L184-L209)）：`flip` 键已存在时直接取 `flip_direction`，不再掷硬币。CenterPoint flip-tta 配置里 `RandomFlip3D(sync_2d=False)` 正是为了走进 else 分支、让 3D 翻转完全由预写的 `pcd_horizontal_flip/pcd_vertical_flip` 决定（纯 LiDAR 管线无图像，2D 的 flip 决策无实际作用）。

**（2）`GlobalRotScaleTrans` 同样服从**。[L775-L799](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/transforms_3d.py#L775-L799)：`if 'pcd_scale_factor' not in input_dict: self._random_scale(...)`——TTA 预写的 `pts_scale_ratio` 直接生效；旋转没有服从机制，靠配置把 `rot_range` 压成 `[0,0]` 实现恒等。

**（3）两个易混淆包装器的辨析**：

- **mmcv 的 `TestTimeAug`**（分割 TTA 用，从 [mmcv.transforms.processing 导入](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/datasets/semantickitti.py#L2)）：更通用的笛卡尔积——`transforms` 是 `list[list[dict]]`，每组里任选一个串成一条管线，对所有组合各跑一遍。它不认识 3D 决策键，靠的是「把概率压成 0/1 的确定性 transform」（如 `flip_ratio_bev_horizontal=1.` 表示必翻，见 [tests/test_datasets/test_tta.py:L29-L64](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tests/test_datasets/test_tta.py#L29-L64) 用四个 `RandomFlip3D` 枚举四种翻转组合）。
- **`MultiViewWrapper`**（[transforms_3d.py:L2269-L2298](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/transforms_3d.py#L2269-L2298)）：名字里也有「多视图」，但与 TTA 无关——它把**单目图像 transform 推广到多相机**：逐相机构造伪 dict、跑同一 `Compose`、把 `scale/crop/flip` 等决策键按列表收集（[L2321-L2369](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/transforms_3d.py#L2321-L2369)）；`override_aug_config=True` 时把第一相机的随机决策（`randomness_keys`）**复用**给后续相机（[L2342-L2345](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/transforms_3d.py#L2342-L2345)），Waymo 多相机配置（`mmdet3d/configs/_base_/datasets/waymoD5_mv3d_3class.py`）在用它。与 `MultiScaleFlipAug3D` 的共性是同一族设计模式——**把随机决策从 transform 内部提升到 results 里传递**；区别是前者用决策保证多相机一致，后者用决策枚举 TTA 视图。

**（4）`pipeline` 与 `tta_pipeline` 两套配置的关系**。这是本讲最容易混淆的一点，两种 TTA 的接线方式完全不同：

| | 检测 TTA（CenterPoint flip-tta） | 分割 TTA（SemanticKITTI 等） |
| --- | --- | --- |
| 配置写法 | 专属配置文件**整体覆写** `test_pipeline`（内嵌 `MultiScaleFlipAug3D`），再经 `data = dict(val=..., test=...)` 覆盖 dataloader（[flip-tta 配置 L48-L49](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/centerpoint/centerpoint_voxel0075_second_secfpn_head-dcn-circlenms_8xb4-flip-tta-cyclic-20e_nus-3d.py#L48-L49)） | `_base_` 数据配置里**并列定义** `test_pipeline` 与 `tta_pipeline`（用 mmcv `TestTimeAug`），另加 `tta_model = dict(type='Seg3DTTAModel')`（[semantickitti.py:L129-L180](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/datasets/semantickitti.py#L129-L180)、[L224](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/datasets/semantickitti.py#L224)） |
| 启用方式 | 直接 `tools/test.py <flip-tta配置> <权重>`，无需开关 | `tools/test.py --tta`，此时 [tools/test.py:L127-L133](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/test.py#L127-L133) 把 dataset.pipeline 换成 `cfg.tta_pipeline`，并用 `ConfigDict(**cfg.tta_model, module=cfg.model)` 把原模型**包进 TTA 外壳模型** |
| 合并发生在哪 | 检测器自己的 `aug_test` 方法内（调用 `merge_aug_bboxes_3d`） | TTA 外壳模型（MMEngine `BaseTTAModel` 模式）逐视图调 `module.predict` 后调 `merge_preds`（见 4.4.3） |
| 坐标还原 | 需要（框在增强坐标系里，须 mapping back） | 不需要（逐点语义标签与坐标无关，直接平均概率） |

`tools/test.py` 的 `--tta` 分支注释（[L128-L129](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/test.py#L128-L129)）写明「目前只为 3D 分割支持 tta」——检测 TTA 不走 `tta_model` 包装这条路，而是像 CenterPoint 那样把 TTA 写死在专属配置的 `test_pipeline` 里。

#### 4.3.4 代码实践

**实践目标**：在完全不跑模型的情况下，亲手让 `MultiScaleFlipAug3D` 枚举出 4 个视图，并核对每个视图的决策键——把 CenterPoint flip-tta 配置的 \(K=4\) 演算变成可执行事实。

1. 操作步骤（CPU 即可，示例代码）：

   ```python
   # 示例代码：复现 CenterPoint flip-tta 的视图枚举
   from mmdet3d.registry import TRANSFORMS
   from mmdet3d.structures.points import LiDARPoints
   import numpy as np

   tta = TRANSFORMS.build(dict(
       type='MultiScaleFlipAug3D',
       img_scale=(1333, 800),      # 单值
       pts_scale_ratio=1,           # 单值
       flip=True,                   # flip_aug = [True]
       pcd_horizontal_flip=True,    # 枚举 [False, True]
       pcd_vertical_flip=True,      # 枚举 [False, True]
       transforms=[
           dict(type='RandomFlip3D', sync_2d=False),
       ]))

   results = dict(points=LiDARPoints(np.random.rand(100, 4).astype(np.float32), 4))
   views = tta(results)
   print(len(views))                # 期望 4
   for i, v in enumerate(views):
       print(i, v['pcd_horizontal_flip'], v['pcd_vertical_flip'],
             v['flip'], v['pcd_scale_factor'])
   ```

   （`RandomFlip3D` 的决策服从逻辑见 4.3.3 第 1 点；此处子管线刻意省去 `GlobalRotScaleTrans` 与 `PointsRangeFilter`，只观察决策注入。）
2. 需要观察的现象：打印出的 4 行按 \((h,v)\) 依次为 `(F,F) (F,T) (T,F) (T,T)`；且每个视图的 `points` 张量已真正翻转（第 2 列 y 变号）当且仅当对应 h 为 True。
3. 预期结果：与 4.3.2 的 \(K=4\) 演算一致；对照 [tests/test_datasets/test_tta.py:L57-L64](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tests/test_datasets/test_tta.py#L57-L64) 里 `TestTimeAug` 版本的四种组合断言互相印证。
4. 若本机未安装 mmdet3d 运行环境，此实践「待本地验证」，但可先在纸面上完成枚举表推演。

#### 4.3.5 小练习与答案

**练习 1**：若把 `MultiScaleFlipAug3D` 的 `img_scale` 配成 `[(1333, 800), (800, 600)]`、其余同 CenterPoint flip-tta，视图数是多少？多出的视图之间差在哪里？
**答案**：\(K = 2 \times 1 \times 1 \times 2 \times 2 \times 1 = 8\)。`img_scale` 只通过 `results['scale']` 影响**图像** resize（`self.transforms.transforms[0].scale = scale` 这行还会改写子管线第一个 transform 的 scale 属性，[L89-L91](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/test_time_aug.py#L89-L91)）；对纯 LiDAR 模型点云不变，两个尺度视图的点云输入相同（除非配了 `pts_scale_ratio`），主要服务于含图像分支的多模态 TTA。

**练习 2**：为什么 `MultiScaleFlipAug3D` 的构造函数要求 `transforms` 里必须出现 `RandomFlip3D` 或 `RandomFlip`（否则 warning）？
**答案**：它只注入 `flip / pcd_horizontal_flip / pcd_vertical_flip / pcd_scale_factor / scale` 这些决策键，几何变换全部依赖下游增强类执行。没有翻转类时键无人消费，`flip=True` 的视图与原视图完全相同，TTA 白跑一倍算力却拿不到独立预测——warning 是对这种静默失效的提醒（[L63-L67](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/test_time_aug.py#L63-L67)）。

**练习 3**：`Base3DDetector.forward` 靠什么特征区分「普通测试 batch」与「TTA 输入」？为什么 TTA 断言 batch_size 必须为 1？
**答案**：靠 `data_samples[0]` 的类型——普通测试时它是 `Det3DDataSample`，TTA 时它是一个 **list**（K 个视图各自的 sample），见 [base.py:L77-L86](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/base.py#L77-L86)。batch_size 限制为 1 是因为合并端 `merge_aug_bboxes_3d` 假设「aug_results 与 aug_batch_input_metas 一一对应、每个元素是同一份数据在某个视图下的预测」（[merge_augs.py:L34-L37](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/test_time_augs/merge_augs.py#L34-L37)）；多样本 batch 时「哪个视图属于哪个样本」需要额外的重组逻辑，当前未实现。

### 4.4 merge_aug_bboxes_3d：TTA 的合并端

#### 4.4.1 概念说明

合并端（[mmdet3d/models/test_time_augs/merge_augs.py:L11-L98](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/test_time_augs/merge_augs.py#L11-L98)）解决的问题是：K 个视图各给出一组**增强坐标系里**的预测框，如何变成原始坐标系里的一组最终预测。

策略是「先逐视图还原 → 再无差别拼接 → 最后逐类 NMS 去重」。还原只依赖三个 metainfo 键：

\[ \text{box}_{orig} = S_{1/s} \circ F_{h}^{[flip_h]} \circ F_{v}^{[flip_v]} (\text{box}_{aug}) \]

其中 \(F_h, F_v\) 是水平/垂直翻转（对合变换，再做一次即逆操作），\(S_{1/s}\) 是 `pcd_scale_factor` 的倒数缩放。**注意还原清单里没有旋转与平移**——增强可能引入 \(R, T\)，但合并端拿不到（也不可交换地消去）它们，这就是检测 TTA 配置必须把 `rot_range`、`translation_std` 压成恒等的原因（4.3.2）。这也解释了为什么 metainfo 只需随身携带 `pcd_scale_factor` 与两个翻转布尔。

#### 4.4.2 核心流程

```
merge_aug_bboxes_3d(aug_results, aug_batch_input_metas, test_cfg)
 ├─ 断言两组输入等长（每视图一对一）
 ├─ for 每个视图:
 │    ├─ 取出 scale_factor / flip 两个布尔
 │    ├─ bbox3d_mapping_back(bbox, s, flip_h, flip_v)   # 逆序还原：先翻转再除尺度
 │    └─ 收集该视图 scores / labels（原样，不需还原）
 ├─ 全视图框 cat 成 aug_bboxes；BEV 投影转 xyxyr 供 NMS
 ├─ for 每个类别:
 │    selected = nms_func(bboxes_nms_i, scores_i, nms_thr)   # nms_bev 或 nms_normal_bev
 │    保留该类选中框/分/标签
 ├─ 全类拼接 → 按分数降序 → 截断 max_num（默认 500）
 └─ bbox3d2result(...) 打包成统一结果 dict
```

为什么 NMS 是正确的合并手段？同一真实物体在 K 个视图里都被检出，还原回原坐标系后这 K 个框几乎重合——重叠高、分数相近，正是 NMS 的靶子；NMS 保留每组重叠簇中分数最高者，等价于对「同一物体的 K 次独立测量」取了一次软性的最优选择。逐类做 NMS 则避免把相邻的不同类物体（如紧挨的 car 与 pedestrian）误删。

#### 4.4.3 源码精读

**（1）逐视图还原**。[merge_augs.py:L43-L51](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/test_time_augs/merge_augs.py#L43-L51)：分数与标签直接收集；框经 `bbox3d_mapping_back` 还原。后者在 [mmdet3d/structures/ops/transforms.py:L5-L24](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/ops/transforms.py#L5-L24)，实现是 `flip('horizontal') → flip('vertical') → scale(1/s)` 三步，全部委托给框对象自身的刚体方法（[u2-l2](u2-l2-base-instance-3d-boxes.md)），克隆后操作、不改原框。

**（2）拼接与 NMS 输入格式**。[L53-L62](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/test_time_augs/merge_augs.py#L53-L62)：`aug_bboxes_for_nms = xywhr2xyxyr(aug_bboxes.bev)`——先把 7 维框压成 BEV 的 (x,y,w,h,r)（[u2-l2](u2-l2-base-instance-3d-boxes.md) 的 `bev` 属性），再转成旋转 NMS 要的四角加角度格式；按 `test_cfg.use_rotate_nms` 选择 `nms_bev`（旋转，基于 mmcv `nms_rotated` CUDA 算子）或 `nms_normal_bev`（轴对齐，基于 mmcv `nms`），两者定义于 [mmdet3d/models/layers/box3d_nms.py:L234](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/layers/box3d_nms.py#L234) 与 [L281](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/layers/box3d_nms.py#L281)（[u5-l7](u5-l7-3d-losses-and-nms.md) 已精读）。

**（3）逐类 NMS 与截断**。[L69-L96](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/test_time_augs/merge_augs.py#L69-L96)：空预测提前返回；按 `class_id` 过滤后调 NMS，阈值取 `test_cfg.nms_thr`；最后按分数降序截断到 `test_cfg.max_num`（缺省 500）。返回值经 `bbox3d2result`（[transforms.py:L51-L77](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/ops/transforms.py#L51-L77)）规范成 `bboxes_3d / scores_3d / labels_3d` 的 dict——与 [u2-l4](u2-l4-det3d-data-sample.md) 中 `add_pred_to_datasample` 期待的键名一致，TTA 结果因此能无缝走后续评估与可视化。

**（4）两个消费者**：

- `Anchor3DHead.aug_test`（[mmdet3d/models/dense_heads/anchor3d_head.py:L200-L219](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/anchor3d_head.py#L200-L219)）：逐视图 `forward + get_results`（含各视图自己的 NMS），收集 K 组结果后交 `merge_aug_bboxes_3d`，注释明确「only support aug_test for one sample」。
- `VoteNet.aug_test`（[mmdet3d/models/detectors/votenet.py:L106-L148](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/votenet.py#L106-L148)）：仓库内最完整的检测器级实现——`num_augs == 1` 时直接退化为普通 `predict`；否则逐视图前向收集 `InstanceData`，转 dict 后同样交 `merge_aug_bboxes_3d`。

需要如实指出的是：仓库内显式实现 `aug_test` 的检测器并不多——`SingleStageMono3DDetector.aug_test` 只有 `pass`（[single_stage_mono3d.py:L96-L99](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/single_stage_mono3d.py#L96-L99)），`Base3DDenseHead.aug_test` 也是空壳（[base_3d_dense_head.py:L374-L381](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/base_3d_dense_head.py#L374-L381)，源码 TODO 写着 "Support augmentation test"）。CenterPoint 系（`MVXTwoStageDetector` 及其子类，见 [u5-l5](u5-l5-centerpoint.md)）在当前 HEAD 没有自己的 `aug_test`，flip-tta 配置端到端跑通与否取决于继承链末端（mmdet `BaseDetector`）提供的行为，属「待本地验证」——读配置时请以 `MultiScaleFlipAug3D` 管线 + `Anchor3DHead/VoteNet` 两处实现为可靠参照。

**（5）分割 TTA 的对照**：`Seg3DTTAModel.merge_preds`（[mmdet3d/models/segmentors/seg3d_tta.py:L11-L35](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/segmentors/seg3d_tta.py#L11-L35)）对 K 个视图的逐点分割 logits 做 softmax 后**算术平均再 argmax**：

\[ \hat{y}_i = \arg\max_c \frac{1}{K}\sum_{k=1}^{K} p_k(c \mid x_i) \]

因为分割预测的是逐点语义标签、与该点在哪个增强视图里无关，无需任何坐标还原——这与检测 TTA 的「mapping back + NMS」形成鲜明对照，也再次解释了 4.3.3 第 4 点的表格里两种 TTA 的配置差异。

#### 4.4.4 代码实践

**实践目标**：用最小数据验证合并端的两步——「翻转还原的对合性」与「多视图拼接后 NMS 去重」，不跑任何模型。

1. 操作步骤（示例代码，CPU 部分可跑；NMS 部分依赖编译版 mmcv 的 CUDA 算子）：

   ```python
   # 示例代码：手工演练 merge 的还原与去重
   import torch
   from mmdet3d.structures import LiDARInstance3DBoxes
   from mmdet3d.structures.ops.transforms import bbox3d_mapping_back

   box = LiDARInstance3DBoxes(
       torch.tensor([[10., 5., 0., 4., 2., 1.5, 0.3]]))   # 一个 7 维框

   # 第一步：模拟「视图内预测」——把框水平翻转当作某视图的输出
   aug_box = box.clone(); aug_box.flip('horizontal')
   # 用 metainfo 还原（flip=True, scale=1）
   back = bbox3d_mapping_back(aug_box, 1.0, True, False)
   print(torch.allclose(back.tensor, box.tensor))   # 期望 True（对合性）

   # 第二步：模拟 merge——同一物体的两个视图 + 一个噪声框
   from mmdet3d.models.test_time_augs.merge_augs import merge_aug_bboxes_3d
   aug_results = [
       dict(bbox_3d=box.clone(), scores_3d=torch.tensor([0.9]),
            labels_3d=torch.tensor([0])),
       dict(bbox_3d=aug_box.clone(), scores_3d=torch.tensor([0.8]),
            labels_3d=torch.tensor([0])),
   ]
   metas = [dict(pcd_scale_factor=1.0, pcd_horizontal_flip=False,
                 pcd_vertical_flip=False),
            dict(pcd_scale_factor=1.0, pcd_horizontal_flip=True,
                 pcd_vertical_flip=False)]
   merged = merge_aug_bboxes_3d(
       aug_results, metas,
       dict(use_rotate_nms=True, nms_thr=0.1, max_num=50))
   print(merged['scores_3d'], merged['labels_3d'])   # 期望只剩 1 个框、分数 0.9
   ```

2. 需要观察的现象：第一步打印 `True`（翻转再翻转回到原位，且 yaw 按约定变号再变回）；第二步两个重叠框被 NMS 合并成一个，保留高分视图的 0.9。
3. 预期结果：合并框数 ≤ 输入总框数，且等于「按 NMS 阈值聚类后的簇数」；若把第二个视图的框移到远处（无重叠），则两个框都保留——这正是 4.4.2 的「软性最优选择」直觉。
4. 第一步纯 CPU 可验证；第二步的 `nms_bev` 走 mmcv CUDA 算子，无 GPU 环境时「待本地验证」（可退而求其次：阅读 [merge_augs.py:L72-L84](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/test_time_augs/merge_augs.py#L72-L84) 逐类过滤逻辑，在纸面上推演哪些框会被抑制）。

#### 4.4.5 小练习与答案

**练习 1**：合并端为什么先 `flip` 再 `scale(1/s)`，而不是相反？对照增强链说明。
**答案**：CenterPoint flip-tta 的子管线顺序是 `GlobalRotScaleTrans(恒等) → RandomFlip3D`，即数据先（恒等）缩放后翻转；逆变换必须**逆序**施加：先翻转（消去 Flip）再缩放（消去 Scale）。在本配置里 Scale 恒等所以顺序无感，但当 `pts_scale_ratio != 1` 且翻转同时存在时，Flip 与 Scale 不可交换（翻转是镜像，缩放是相似变换，先缩后翻的逆是先翻后缩），`bbox3d_mapping_back` 的固定顺序 [L18-L22](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/ops/transforms.py#L18-L22) 正确对应了增强链的逆。

**练习 2**：`merge_aug_bboxes_3d` 里 NMS 之前为什么不做 `limit_period`、`dir_offset` 之类的角度修正？
**答案**：那些修正发生在**每个视图自己的 head 后处理**里（[u5-l4](u5-l4-anchor3d-head.md)：方向分类折叠加 dir_offset 拼回完整 yaw），`aug_test` 收到的 `aug_results` 已经是各视图解码完成的最终框；合并端面对的是「成品框」，职责只剩还原坐标系与去重，不再触碰角度语义。

**练习 3**（批判性阅读）：仔细看 [votenet.py:L134-L137](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/detectors/votenet.py#L134-L137)，收集 metainfo 的循环用的是 `aug_id` 而不是循环变量 `aug_index`（`aug_id` 此时停留在上一轮循环的末值）。这会造成什么影响？
**答案**：`aug_id` 在该处恒为 `num_augs - 1`，于是**所有视图**的 metainfo 都取自最后一个视图。若各视图的 `pcd_scale_factor`/flip 键不同（例如做 flip TTA），`merge_aug_bboxes_3d` 会用错误的标志还原前几个视图的框，导致合并错位。VoteNet 的典型 TTA 不引入 flip/scale 差异（室内检测、变换恒等），所以平时无症状，但这是一个值得报告/验证的疑似瑕疵——建议读者在本地构造两个 flip 不同的视图复现它（承接 4.4.4 实践改造即可），这也说明了「TTA 合并的正确性完全依赖 metainfo 与视图一一对应」这条不变量。

## 5. 综合实践

**任务：给一帧 demo 点云同时跑「单视图推理」与「纸面 TTA 推演」，产出一份合并过程说明书。**

1. 单视图推理（依赖 GPU 与权重，`demo/pcd_demo.py` 用法见 4.2.4）：

   ```bash
   python demo/pcd_demo.py demo/data/kitti/000008.bin \
       configs/pointpillars/pointpillars_hv_secfpn_8xb6-160e_kitti-3d-3class.py \
       <checkpoint 或权重 URL> --device cuda:0
   ```

2. 纸面 TTA 推演（不依赖运行）：打开 [configs/centerpoint/centerpoint_voxel0075_second_secfpn_head-dcn-circlenms_8xb4-flip-tta-cyclic-20e_nus-3d.py:L13-L46](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/centerpoint/centerpoint_voxel0075_second_secfpn_head-dcn-circlenms_8xb4-flip-tta-cyclic-20e_nus-3d.py#L13-L46)，完成下表（答案已给出一行示例）：

   | 视图 | pcd_horizontal_flip | pcd_vertical_flip | mapping_back 动作 |
   | --- | --- | --- | --- |
   | 0 | False | False | 不动（scale=1） |
   | 1 | False | True | flip('vertical') |
   | 2 | True | False | flip('horizontal') |
   | 3 | True | True | flip('vertical') 后 flip('horizontal') |

3. 写说明书：以 4.4.4 的最小实验为证据，回答三个问题——(a) 4 组预测还原后为什么会两两重叠；(b) 逐类 NMS 后每个物体簇剩几个框、保留的是哪个视图的；(c) 若把 `pts_scale_ratio` 改成 `[0.95, 1.0]`，视图数变成多少（答案：\(2\times 2\times 2=8\)），`bbox3d_mapping_back` 对 0.95 视图的框要做哪两步、顺序如何（答案：先 flip（若有）再乘以 1/0.95）。
4. 产出物：`outputs/preds/000008.json` + 一页说明（表格与三问答）。第 1 步无法执行时标注「待本地验证」，第 2、3 步纯源码推演，人人可完成。

## 6. 本讲小结

- `Base3DInferencer` 用「一个 config + 一个 checkpoint」装配推理服务：`_init_model` 五步走（SyncBN 降级 → 摘 train_cfg → 注册器建模型 → 载权重 → 恢复/补全 dataset_meta），主循环按 `preprocess → forward → visualize → postprocess` 四段推进，参数经四个 kwargs 白名单集合路由。
- 四个子类对测试管线做最小外科手术：LiDAR 换加载器并顺手记下列定义、单目额外注入 info pkl 里的标定、多模态要求点云图像双加载器、分割直接删除真值相关 transform；可视化阶段按原列定义重读原始数据。
- 检测 TTA 的管线端 `MultiScaleFlipAug3D` 只做「决策枚举 + 决策注入」：六重循环笛卡尔积地预写 `flip / pcd_horizontal_flip / pcd_vertical_flip / pcd_scale_factor` 等键，几何变换由服从这些键的 `RandomFlip3D` / `GlobalRotScaleTrans` 执行——TTA 与训练增强共用同一套几何代码。
- 合并端 `merge_aug_bboxes_3d` 三步走：`bbox3d_mapping_back` 逐视图逆序还原（先翻转再除尺度，旋转平移不可还原）→ 全视图框拼接 → 逐类 BEV NMS 去重并按分截断 `max_num`；还原只依赖三个 metainfo 键，TTA 合并的正确性完全依赖「metainfo 与视图一一对应」。
- 两套 TTA 配置：检测类在专属配置里整体覆写 `test_pipeline`（CenterPoint flip-tta 的 4 视图 = 水平 × 垂直翻转）；分割类并列定义 `test_pipeline` / `tta_pipeline`（mmcv `TestTimeAug`）加 `tta_model`，经 `tools/test.py --tta` 启用，合并策略是 softmax 平均而非 NMS。
- 名字辨析：`MultiScaleFlipAug3D`（枚举 TTA 视图）、mmcv `TestTimeAug`（通用笛卡尔积 TTA）、`MultiViewWrapper`（把单目增强决策复用到多相机，与 TTA 无关但共享「决策键外置」模式）。

## 7. 下一步学习建议

- 下一讲 [u7-l5](u7-l5-analysis-tools.md) 转向实用工具集：`analyze_logs` 画曲线、`get_flops`/`benchmark` 评复杂度、`browse_dataset` 查数据——把本讲的推理能力与前面的训练评估串成日常实验闭环。
- 若想深入 TTA 的模型端，建议按顺序读：`mmdet3d/models/detectors/base.py` 的 forward 分支 → `votenet.py` 的 `aug_test` → `anchor3d_head.py` 的 `aug_test`，并思考「为什么 `SingleStageMono3DDetector.aug_test` 至今是 `pass`」（提示：单目 head 的解码依赖 `trans_mat` 与内参逆变换，多视图还原比 LiDAR 复杂）。
- 分割方向的读者可对照 `configs/_base_/datasets/semantickitti.py` 的 `tta_pipeline` 与 `mmdet3d/models/segmentors/seg3d_tta.py`，再进入单元 8 的 [u8-l1](u8-l1-3d-semantic-segmentation.md)（Cylinder3D 与 MinkUNet）。
- 对多相机 `MultiViewWrapper` 感兴趣的读者，`mmdet3d/configs/_base_/datasets/waymoD5_mv3d_3class.py` 是唯一在库内使用它的配置，可结合 [u8-l4](u8-l4-projects-ecosystem.md) 的 projects 生态（BEVFusion 的 BEV 级融合）比较「多相机图像增强」与「多传感器融合」两个不同维度的问题。
