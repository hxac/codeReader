# 跑通第一个推理 Demo

## 1. 本讲目标

上一讲我们装好了环境并验证了 `import mmdet3d` 可以通过。本讲要在不动一行源码的前提下，让一个预训练 3D 检测模型真正"看见"点云、吐出 3D 框。学完本讲你应当能够：

1. 使用仓库自带的 `demo/pcd_demo.py` 对 `demo/data/` 中的单帧点云跑通完整推理，并把检测结果保存为可视化图片和预测 JSON。
2. 顺着源码说出一次推理经历了哪些步骤：命令行参数 → Inferencer 初始化 → 测试数据管线 → 模型前向 → 可视化与结果落盘。
3. 说出推理输入（一个 `.bin` 点云文件）和输出（`Det3DDataSample` 中的 `pred_instances_3d`）的大致结构。
4. 对比两条推理路线：命令行/Inferencer 一站式路线，与 `init_model` + `inference_detector` 的底层 API 路线，理解各自适合的场景。

## 2. 前置知识

- **点云文件（`.bin`）**：KITTI 等数据集把一帧 LiDAR 扫描存成二进制文件，每个点用若干个 `float32` 连续排列。KITTI 是每点 4 个浮点数（x、y、z、反射强度 intensity），所以文件字节数除以 16 就是点数。
- **3D 框的 7 个自由度**：第一讲提过，一个 3D 检测框用 7 个数描述——中心坐标 \( (x, y, z) \)、尺寸 \( (l, w, h) \)、朝向角 \( \theta \)。本讲的输出就是一堆这样的框加上类别和置信度。
- **Det3DDataSample**：MMDetection3D 里模型输出的统一容器（第四单元 u2-l4 会深入剖析）。现在只需记住：预测结果挂在它的 `pred_instances_3d` 字段下，里面有三个关键成员——`bboxes_3d`（3D 框张量）、`scores_3d`（置信度）、`labels_3d`（类别索引）。
- **数据管线（pipeline）**：配置文件里 `test_dataloader.dataset.pipeline` 是一个 transform 列表，负责把"一个文件路径"加工成"模型能吃的张量"。推理时会复用这条管线，这是本讲反复出现的概念。
- **checkpoint（权重文件）**：后缀 `.pth` 的文件，存的是训练好的网络参数，外加一些元信息（`meta` 字段里有类别名等），推理时由 `load_checkpoint` 加载进模型。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `demo/pcd_demo.py` | 点云检测命令行入口：解析参数后调用 `LidarDet3DInferencer` |
| `demo/mono_det_demo.py` | 单目 3D 检测命令行入口：调用 `MonoDet3DInferencer` |
| `demo/multi_modality_demo.py` | 点云+图像多模态检测入口：调用 `MultiModalityDet3DInferencer` |
| `mmdet3d/apis/inference.py` | 底层推理 API：`init_model`、`inference_detector` 等 |
| `mmdet3d/apis/inferencers/base_3d_inferencer.py` | 所有 3D Inferencer 的公共基类，实现"预处理→前向→可视化→后处理"主循环 |
| `mmdet3d/apis/inferencers/lidar_det3d_inferencer.py` | LiDAR 检测 Inferencer：改写测试管线、读点云、调可视化器 |
| `mmdet3d/datasets/transforms/loading.py` | 定义 `LidarDet3DInferencerLoader`，推理专用的点云加载 transform |
| `demo/data/` | 仓库自带示例数据：kitti、nuscenes、scannet、sunrgbd 四个子目录 |

## 4. 核心概念与源码讲解

### 4.1 三条推理入口与 demo 脚本家族

#### 4.1.1 概念说明

MMDetection3D 提供两类、共五条推理入口：

| 路线 | 入口 | 特点 |
| --- | --- | --- |
| 高层 Inferencer | `demo/pcd_demo.py` → `LidarDet3DInferencer` | 一条命令完成推理+可视化+保存 |
| 高层 Inferencer | `demo/mono_det_demo.py` → `MonoDet3DInferencer` | 单目图像 3D 检测 |
| 高层 Inferencer | `demo/multi_modality_demo.py` → `MultiModalityDet3DInferencer` | 点云+图像融合检测 |
| 底层 API | `init_model` + `inference_detector` | 只做"加载模型+前向"，可视化自己写 |
| 底层 API | `inference_mono_3d_detector` / `inference_multi_modality_detector` / `inference_segmentor` | 其余任务的对应 API |

高层入口省事，适合验证环境和批量出结果；底层 API 灵活，适合嵌入自己的程序（比如做一个服务）。两条路线殊途同归——最终都调用模型的 `test_step` 方法做前向。

#### 4.1.2 核心流程

`demo/pcd_demo.py` 的执行流程：

```
命令行（pcd、model、weights、可选参数）
    ↓ parse_args()
拆成两组参数：
    init_args = {model, weights, device}          → 构造 Inferencer
    call_args = {inputs, pred_score_thr, out_dir, show, ...} → 调用 Inferencer
    ↓
LidarDet3DInferencer(**init_args)   # 加载配置、建模型、载权重
inferencer(**call_args)             # 推理 + 可视化 + 落盘
```

#### 4.1.3 源码精读

demo 脚本第一行导入就点明了它的依赖：从 `mmdet3d.apis` 导入 `LidarDet3DInferencer`，见 [demo/pcd_demo.py:L8](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/demo/pcd_demo.py#L8)。`mmdet3d.apis` 这个包同时导出了高层 Inferencer 与底层函数，是官方推理能力的总出口，见 [mmdet3d/apis/__init__.py:L2-L8](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/apis/__init__.py#L2-L8)。

参数定义部分，前三个是位置参数：点云文件、配置文件、权重文件；其余是可选参数，注意默认值——设备 `cuda:0`、置信度阈值 `0.3`、输出目录 `outputs`，见 [demo/pcd_demo.py:L11-L49](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/demo/pcd_demo.py#L11-L49)。

最关键的转换在这一行：把位置参数 `pcd` 改名塞进 `inputs` 字典，变成 Inferencer 约定的输入格式 `dict(points=...)`，见 [demo/pcd_demo.py:L52](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/demo/pcd_demo.py#L52)。

接着脚本把参数一分为二：`model/weights/device` 三个是构造 Inferencer 的初始化参数（init_args），剩下的都是调用参数（call_args），见 [demo/pcd_demo.py:L57-L60](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/demo/pcd_demo.py#L57-L60)。这个"初始化参数 vs 调用参数"的二分是 Inferencer 体系的核心约定：前者决定"用哪个模型"，后者决定"怎么处理这一批输入"。

脚本还处理了远程服务器没有显示器的场景：若环境变量 `DISPLAY` 不存在，强制关闭 `--show` 弹窗，只落盘保存，见 [demo/pcd_demo.py:L65-L70](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/demo/pcd_demo.py#L65-L70)。

主函数只有两步：构造 Inferencer，再调用它，见 [demo/pcd_demo.py:L75-L87](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/demo/pcd_demo.py#L75-L87)。

另外两个 demo 脚本结构完全相同，只是输入字段不同：单目检测需要图像和标注信息文件，见 [demo/mono_det_demo.py:L58-L60](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/demo/mono_det_demo.py#L58-L60)；多模态则需要点云、图像、信息文件三样，见 [demo/multi_modality_demo.py:L59-L63](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/demo/multi_modality_demo.py#L59-L63)。

#### 4.1.4 代码实践

**实践目标**：认清示例数据的形态，理解"一个 `.bin` 文件就是一帧点云"。

1. 列出仓库自带的 KITTI 示例数据（共四个文件，本讲只用 `.bin`）：

   ```bash
   ls -l demo/data/kitti/
   # 000008.bin  000008.pkl  000008.png  000008.txt
   ```

2. 查看文件大小并估算点数。KITTI 点云每点 4 个 `float32`（16 字节）：

   ```bash
   ls -l demo/data/kitti/000008.bin
   ```

   点数计算公式：

   \[ N = \frac{\text{文件字节数}}{4 \ (\text{load\_dim}) \times 4 \ (\text{float32 字节})} \]

3. （可选）用 Python 只读方式验证：

   ```bash
   python -c "
   import numpy as np
   pts = np.fromfile('demo/data/kitti/000008.bin', dtype=np.float32).reshape(-1, 4)
   print('点数:', pts.shape[0], '每点维度:', pts.shape[1])
   print('x 范围:', pts[:, 0].min(), '~', pts[:, 0].max())
   "
   ```

**需要观察的现象**：点数约在 1.7 万左右（具体数值待本地验证）；x 范围大致在 0~70 米——这正是 KITTI 点云在车前方的探测范围。

**预期结果**：`reshape(-1, 4)` 不报错，说明文件确实按每点 4 浮点数对齐存储。

#### 4.1.5 小练习与答案

**练习 1**：`demo/pcd_demo.py` 为什么要把 `pcd` 参数改名为 `inputs` 再传给 Inferencer？

**答案**：因为 Inferencer 的调用接口约定输入是一个 `inputs` 对象（可以是 dict、str 或 list）。把点云路径包成 `dict(points=...)` 是 LiDAR 检测 Inferencer 认识的格式，它的 `_inputs_to_list` 方法会从 `points` 键取值（见 4.2 节）。

**练习 2**：如果你在无显示器的远程服务器上误加了 `--show`，会发生什么？

**答案**：脚本检测到环境变量 `DISPLAY` 不存在，打印一条 WARNING 并把 `show` 强制改为 `False`，结果仍然会保存到 `--out-dir` 目录，不会崩溃。

**练习 3**：`--pred-score-thr` 影响的是"模型输出"还是"画哪些框"？

**答案**：只影响可视化。它一路传到 `visualize()` 的 `pred_score_thr` 参数，过滤掉低置信度框不画；模型本身的预测输出（保存的 JSON）不受它影响。

### 4.2 LidarDet3DInferencer：初始化与测试管线的"偷梁换柱"

#### 4.2.1 概念说明

`LidarDet3DInferencer` 是 MMEngine `BaseInferencer` 的 3D LiDAR 检测特化版。它要解决一个核心矛盾：**配置文件里的测试管线是为"数据集里的样本"设计的**（第一个 transform 通常是 `LoadPointsFromFile`，需要从 info 字典里拿路径），**而推理时的输入只是一个裸的文件路径或 numpy 数组**。它的解法很优雅——找到管线里的 `LoadPointsFromFile`，把它原地替换成一个更聪明的加载器 `LidarDet3DInferencerLoader`，管线其余部分原封不动。

#### 4.2.2 核心流程

```
LidarDet3DInferencer(model=..., weights=..., device=...)
    ├─ init_default_scope('mmdet3d')        # 注册器定位到 mmdet3d 命名空间
    ├─ 父类 BaseInferencer.__init__:
    │    ├─ 读配置 → _init_model：建模型、载权重、补 dataset_meta
    │    └─ _init_pipeline：改写测试管线（本模块重点）
    │    └─ _init_visualizer：建可视化器并注入 dataset_meta
    └─ revert_sync_batchnorm(model)         # SyncBN → BN（单卡推理不需要跨卡同步）

_init_pipeline 改写逻辑：
    在 pipeline 配置列表中定位 type == 'LoadPointsFromFile' 的那一项
    → 记下它的 coord_type / load_dim / use_dim（供后面读点云用）
    → 把该项 type 改成 'LidarDet3DInferencerLoader'
    → 用改后的配置 Compose 出新管线
```

#### 4.2.3 源码精读

这个类通过注册器注册了两个名字：语义名 `det3d-lidar`（供 metafile/模型名路由用）和默认类名，见 [mmdet3d/apis/inferencers/lidar_det3d_inferencer.py:L28-L30](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/apis/inferencers/lidar_det3d_inferencer.py#L28-L30)。所以文档里写 `model='pointpillars_kitti-3class'` 这样的短名也能被路由到对应配置——这是第一讲"注册器路由"模式在推理侧的又一次体现。

输入预处理 `_inputs_to_list` 支持一个贴心功能：如果 `points` 给的是**目录**，会自动展开成目录下所有文件的列表，实现批量推理，见 [mmdet3d/apis/inferencers/lidar_det3d_inferencer.py:L85-L100](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/apis/inferencers/lidar_det3d_inferencer.py#L85-L100)。

管线改写发生在 `_init_pipeline` 中：先定位 `LoadPointsFromFile` 的下标（找不到就报错），摘出 `coord_type`、`load_dim`、`use_dim` 存为成员变量（4.3 节可视化读点云时会用到），然后把这个位置的类型改成 `LidarDet3DInferencerLoader`，最后 `Compose` 成可调用管线，见 [mmdet3d/apis/inferencers/lidar_det3d_inferencer.py:L102-L119](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/apis/inferencers/lidar_det3d_inferencer.py#L102-L119)。

被替换进去的加载器长什么样？它内部同时构建了两个标准加载器——处理文件路径的 `LoadPointsFromFile` 和处理 numpy 数组的 `LoadPointsFromDict`，并根据 `coord_type` 算好 `box_type_3d` / `box_mode_3d`，见 [mmdet3d/datasets/transforms/loading.py:L1106-L1112](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/loading.py#L1106-L1112)。执行时按输入类型二选一分发：字符串路径就拼一个含 `lidar_points=dict(lidar_path=...)` 的字典走文件加载，numpy 数组则走数组加载，见 [mmdet3d/datasets/transforms/loading.py:L1122-L1145](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/loading.py#L1122-L1145)。

注意它拼出的数据字典里有两个"占位符"字段：`timestamp=1` 是给 nuScenes 多帧扫描聚合（`LoadPointsFromMultiSweeps`）用的，KITTI 单帧管线会忽略；`axis_align_matrix=np.eye(4)`（4×4 单位阵）是给 ScanNet 室内数据的轴对齐用的。一个加载器同时伺候三类数据集，靠这些占位字段保持管线兼容。

模型构建部分在基类 `_init_model`：转换 SyncBN、清掉 `train_cfg`（推理不需要训练期配置）、`MODELS.build` 建模型、`load_checkpoint` 载权重，见 [mmdet3d/apis/inferencers/base_3d_inferencer.py:L91-L101](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/apis/inferencers/base_3d_inferencer.py#L91-L101)。

随后是从 checkpoint 的 `meta` 里恢复 `dataset_meta`（类别名、调色板）——优先取 mmdet3d 1.x 格式的 `dataset_meta`，旧版权重则回退到 `CLASSES` 字段，见 [mmdet3d/apis/inferencers/base_3d_inferencer.py:L101-L117](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/apis/inferencers/base_3d_inferencer.py#L101-L117)。还有一个精巧设计：用 `lazy_init=True` 构建 test 数据集——不真正读数据，只为拿它的 `metainfo`（比如调色板），见 [mmdet3d/apis/inferencers/base_3d_inferencer.py:L119-L126](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/apis/inferencers/base_3d_inferencer.py#L119-L126)。这就是可视化时"每个类别画什么颜色"的来源。

#### 4.2.4 代码实践

**实践目标**：亲眼确认"管线替换"这件事，理解推理管线与训练管线的差异。

1. 阅读配置中 test 管线的定义（在 `configs/_base_/datasets/kitti-3d-3class.py` 的 `test_pipeline` 变量中，由 PointPillars 配置继承），数一数有几个 transform。
2. 写一个最小脚本（示例代码，非项目原有）：

   ```python
   # 示例代码：观察 Inferencer 内部构建出的管线
   from mmdet3d.apis import LidarDet3DInferencer

   inferencer = LidarDet3DInferencer(
       model='configs/pointpillars/pointpillars_hv_secfpn_8xb6-160e_kitti-3d-3class.py',
       weights='hv_pointpillars_secfpn_6x8_160e_kitti-3d-3class_20220301_150306-37dc2420.pth',
       device='cpu')
   for t in inferencer.pipeline.transforms:
       print(type(t).__name__)
   print('coord_type =', inferencer.coord_type,
         'load_dim =', inferencer.load_dim,
         'use_dim =', inferencer.use_dim)
   ```

3. 对照第 1 步读到的配置，找出哪一项被换掉了。

**需要观察的现象**：打印出的 transform 列表中，第一项不是 `LoadPointsFromFile`，而是 `LidarDet3DInferencerLoader`；`coord_type` 应为 `LIDAR`，`load_dim` 应为 4（具体输出待本地验证）。

**预期结果**：除第一项外，其余 transform 与配置文件中的 test_pipeline 完全一致——证明 Inferencer 只动了加载入口，没有改变数据处理逻辑。

#### 4.2.5 小练习与答案

**练习 1**：为什么不直接在配置文件里把 test 管线第一个 transform 写成 `LidarDet3DInferencerLoader`？

**答案**：因为同一条 test 管线还要服务于 `tools/test.py` 的数据集评估流程，那里输入的是数据集样本（含真实 `lidar_path` 的 info 字典），`LoadPointsFromFile` 是正确的。运行时替换让一份配置同时适配"数据集评估"和"单文件推理"两种场景。

**练习 2**：`revert_sync_batchnorm` 为什么必要？

**答案**：多卡训练的 SyncBatchNorm 在单卡/推理环境下无法工作（没有跨卡同步通信组），MMEngine 提供的工具函数会把它换回普通 BatchNorm，保证前向不报错。

**练习 3**：如果调换顺序，先 `MODELS.build` 再设置 `cfg.model.train_cfg = None`，会出什么问题？

**答案**：会出问题。很多检测器的 `__init__` 会读取 `train_cfg` 来构建训练期的 assigner/sampler（例如 dense head）。先构建再改配置，模型内部已经按训练模式组装了训练组件，且推理时这些组件既多余又可能引发状态问题，所以必须先把 `train_cfg` 置空再构建。

### 4.3 推理主循环：从 inputs 到 preds/vis_lidar 两个目录

#### 4.3.1 概念说明

Inferencer 的调用阶段是一个标准的四段式流水线：**preprocess（过数据管线）→ forward（模型前向）→ visualize（画框）→ postprocess（转存 JSON）**。理解这一段，你就理解了命令跑完后 `outputs/` 目录里那两样产物是怎么来的。

#### 4.3.2 核心流程

```
__call__(inputs, ...)
    ├─ _dispatch_kwargs：把命令行参数分拣到四个阶段各自的 kwargs
    ├─ _inputs_to_list：统一成输入列表
    ├─ preprocess：每个输入 dict 过替换后的管线 → 批次数据
    ├─ forward：model.test_step(data) → List[Det3DDataSample]
    ├─ visualize：
    │    ├─ 自己用 np.frombuffer 读 .bin（load_dim/use_dim 在 4.2 已存好）
    │    └─ visualizer.add_datasample(..., vis_task='lidar_det')
    │         → 写 out_dir/vis_lidar/<名字>.png
    └─ postprocess：pred2dict 把 Det3DDataSample 拆成纯 Python 字典
         → 写 out_dir/preds/<名字>.json
```

输出目录结构（默认 `--out-dir outputs`）：

```
outputs/
├── preds/000008.json      # 预测结果：labels_3d / scores_3d / bboxes_3d / box_type_3d
└── vis_lidar/000008.png   # 可视化图片
```

#### 4.3.3 源码精读

主循环 `__call__` 继承结构清晰：先分拣参数，再对每个批次依次执行 forward、visualize、postprocess，把结果累计进 `predictions` 和 `visualization` 两个列表返回，见 [mmdet3d/apis/inferencers/base_3d_inferencer.py:L173-L221](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/apis/inferencers/base_3d_inferencer.py#L173-L221)。参数分拣靠类属性声明——`visualize_kwargs`、`postprocess_kwargs` 等集合约定了哪些关键字属于哪个阶段，见 [mmdet3d/apis/inferencers/base_3d_inferencer.py:L51-L59](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/apis/inferencers/base_3d_inferencer.py#L51-L59)。这就是为什么 demo 脚本可以放心地把一堆命令行参数一股脑传进来。

可视化部分，LiDAR 推理器需要"自己动手"读点云：用 `np.frombuffer` 按 `float32` 读原始字节，`reshape(-1, self.load_dim)` 后按 `self.use_dim` 取列——这两个值正是 `_init_pipeline` 阶段存下来的，见 [mmdet3d/apis/inferencers/lidar_det3d_inferencer.py:L166-L181](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/apis/inferencers/lidar_det3d_inferencer.py#L166-L181)。然后把点云和预测一起交给可视化器 `add_datasample`，指定任务类型 `lidar_det`，见 [mmdet3d/apis/inferencers/lidar_det3d_inferencer.py:L189-L201](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/apis/inferencers/lidar_det3d_inferencer.py#L189-L201)。可视化系统的内部实现留到 u7-l3 详解。

后处理的灵魂是 `pred2dict`：它把 `Det3DDataSample` 里挂着的 `pred_instances_3d` 拆成三个列表——`labels_3d`、`scores_3d`、`bboxes_3d`（后者取 `.tensor.cpu().tolist()`，即那个 (N, 7) 张量），见 [mmdet3d/apis/inferencers/base_3d_inferencer.py:L308-L315](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/apis/inferencers/base_3d_inferencer.py#L308-L315)；再根据 data_sample 的 `box_mode_3d` 记录坐标系类型字符串（`LiDAR` / `Camera` / `Depth`），见 [mmdet3d/apis/inferencers/base_3d_inferencer.py:L322-L327](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/apis/inferencers/base_3d_inferencer.py#L322-L327)；最后按输入文件名写入 `preds/*.json`，见 [mmdet3d/apis/inferencers/base_3d_inferencer.py:L329-L344](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/apis/inferencers/base_3d_inferencer.py#L329-L344)。

所以一次成功推理的输出结构可以总结为：

```python
result['predictions'][0] == {
    'labels_3d':   [2, 2, 0, ...],            # 类别索引（KITTI 3class: 0=Pedestrian 1=Cyclist 2=Car）
    'scores_3d':   [0.91, 0.87, ...],         # 置信度
    'bboxes_3d':   [[x, y, z, l, w, h, yaw], ...],  # N×7
    'box_type_3d': 'LiDAR',                    # 坐标系标记
}
```

（字段名已从源码确认；具体数值待本地验证。）

#### 4.3.4 代码实践

**实践目标**：读懂"输出 JSON 里有哪些字段、来自哪里"。

1. 打开 [mmdet3d/apis/inferencers/base_3d_inferencer.py:L290-L346](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/apis/inferencers/base_3d_inferencer.py#L290-L346)，对照函数体填写下表：

   | JSON 字段 | 取自 pred_instances_3d 的哪个成员 | 经过了什么转换 |
   | --- | --- | --- |
   | `labels_3d` | ？ | `.tolist()` |
   | `scores_3d` | ？ | ？ |
   | `bboxes_3d` | ？ | `.tensor.cpu().tolist()` |
   | `box_type_3d` | （不来自 pred，来自哪里？） | 枚举转字符串 |

2. 思考：为什么保存 JSON 时要做 `tolist()` 这种转换？

**需要观察的现象**：纯手工填写，无运行步骤。

**预期结果**：答案——`labels_3d` 取自 `labels_3d` 成员；`scores_3d` 取自 `scores_3d` 经 `.tolist()`；`bboxes_3d` 取自 `bboxes_3d`；`box_type_3d` 取自 `data_sample.box_mode_3d`（一个 `Box3DMode` 枚举）。`tolist()` 是因为 JSON 无法序列化 torch 张量，必须转成纯 Python 数值。

#### 4.3.5 小练习与答案

**练习 1**：`preprocess`、`forward`、`visualize`、`postprocess` 四个方法中，哪些定义在本仓库、哪些来自 MMEngine？

**答案**：主流程 `__call__`、`postprocess`、`pred2dict`、`_dispatch_kwargs` 定义在 `Base3DInferencer`（本仓库）；`visualize`、`_init_pipeline`、`_inputs_to_list` 被子类 `LidarDet3DInferencer` 覆写；而 `preprocess`、`forward` 的通用实现由 MMEngine 的 `BaseInferencer` 提供（`preprocess` 逐条调用 `self.pipeline`，`forward` 调用模型的测试接口）。

**练习 2**：为什么 `visualize` 里读点云不直接复用管线加载好的点，而是用 `np.frombuffer` 重读一遍？

**答案**：管线输出的点云已经经过了后续 transform 的加工（如范围裁剪、归一化、打包进 DataSample），且可视化需要的是与原始文件同名的点云数据。直接按 `load_dim/use_dim` 重读原始 `.bin` 最简单，也不依赖管线中间产物的形态。

**练习 3**：如果 `--pred-score-thr` 设成 0.9，输出 JSON 里的框数会变吗？

**答案**：不会。从 `pred2dict` 源码看，保存 JSON 时没有任何分数过滤，保存的是模型全部预测；阈值只影响 `visualize` 里画多少框。所以 JSON 里的框数 ≥ 图上的框数（可待本地验证）。

### 4.4 底层 API 路线：init_model 与 inference_detector

#### 4.4.1 概念说明

不想用 Inferencer 的"全家桶"时，可以用两个底层函数自己拼推理流程：

- `init_model(config, checkpoint, device)`：从配置文件构建模型并加载权重；
- `inference_detector(model, pcds)`：准备数据、过管线、跑前向，返回 `Det3DDataSample`。

这条路线给你完全的控制权：可以自己决定可视化方式、把推理嵌进 Web 服务、或对返回的 data_sample 做后处理。它的实现也正是 Inferencer 内部做的事，读它等于读懂推理的本质。

#### 4.4.2 核心流程

```
init_model:
    Config.fromfile → (可选 merge cfg_options)
    → convert_SyncBN（递归把 naiveSyncBN 替换成 BN）
    → config.model.train_cfg = None
    → init_default_scope('mmdet3d')
    → MODELS.build(config.model)
    → load_checkpoint + 恢复 dataset_meta（checkpoint meta 优先）
    → 从 test 数据集 metainfo 补 palette
    → model.cfg = config; model.to(device); model.eval()

inference_detector(model, pcd):
    统一成列表（单帧也包成 list）
    → 若输入是 ndarray：管线首项换成 LoadPointsFromDict
    → deepcopy 配置中的 test 管线 → Compose
    → get_box_type 拿 box_type_3d / box_mode_3d
    → 拼数据字典（lidar_path、timestamp、axis_align_matrix、box_type/mode）
    → 过管线 → pseudo_collate → model.test_step
    → 单帧返回 (results[0], data[0])   ← 注意是二元组！
```

#### 4.4.3 源码精读

`init_model` 的主体：配置加载与类型检查见 [mmdet3d/apis/inference.py:L58-L66](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/apis/inference.py#L58-L66)，其中 `convert_SyncBN` 会递归遍历配置字典，把所有 `norm_cfg` 里的 `naiveSyncBN` 字符串替换成 `BN`（实现见 [mmdet3d/apis/inference.py:L22-L35](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/apis/inference.py#L22-L35)）；置空训练配置、定位注册器作用域、构建模型这三行见 [mmdet3d/apis/inference.py:L66-L69](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/apis/inference.py#L66-L69)。

加载权重并恢复元信息的兼容逻辑：新版权重存 `meta.dataset_meta`，旧版权重存 `meta.CLASSES`（3D 分割器还有 `PALETTE`），见 [mmdet3d/apis/inference.py:L71-L89](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/apis/inference.py#L71-L89)。这与 Inferencer 基类的逻辑一致。

`inference_detector` 的两个细节值得咀嚼。其一：当传入的不是字符串路径而是 numpy 数组时，它把管线第一个 transform 的 type 换成 `LoadPointsFromDict`——这与 Inferencer 替换成 `LidarDet3DInferencerLoader` 是同一思想的不同实现（前者只支持数组，后者两种都支持），见 [mmdet3d/apis/inference.py:L143-L152](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/apis/inference.py#L143-L152)。其二：为每个输入拼装的字典里，除了 `lidar_path`，还有 `timestamp`、`axis_align_matrix`、`box_type_3d`、`box_mode_3d` 这些管线下游 transform 需要的字段，见 [mmdet3d/apis/inference.py:L157-L175](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/apis/inference.py#L157-L175)。

最后一步前向：`pseudo_collate` 只做"list-of-dict → dict-of-list"的组装而不堆叠张量，然后关梯度调用 `model.test_step`，见 [mmdet3d/apis/inference.py:L178-L182](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/apis/inference.py#L178-L182)。**注意返回值**：单帧输入返回的是 `(results[0], data[0])` 二元组——第一个是 `Det3DDataSample`，第二个是过完管线的数据字典（含元信息），见 [mmdet3d/apis/inference.py:L184-L187](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/apis/inference.py#L184-L187)。初学者最常见的 bug 就是忘了解包，拿着元组去取 `pred_instances_3d`。

两条路线的对比总结：

| 维度 | LidarDet3DInferencer | init_model + inference_detector |
| --- | --- | --- |
| 输入格式 | 文件路径 / numpy / 目录 / URL | 文件路径 / numpy |
| 输出 | dict（predictions + visualization） | `(Det3DDataSample, data_dict)` 二元组 |
| 可视化 | 自动完成并保存 PNG | 需自己调 Visualizer |
| 结果落盘 | 自动保存 JSON | 需自己写 |
| 适用场景 | 快速验证、批量处理 | 嵌入自有程序、深度定制 |

#### 4.4.4 代码实践

**实践目标**：用底层 API 十行代码完成一次推理，并正确解包返回值。

示例代码（非项目原有）：

```python
import numpy as np
from mmdet3d.apis import init_model, inference_detector

config = 'configs/pointpillars/pointpillars_hv_secfpn_8xb6-160e_kitti-3d-3class.py'
checkpoint = 'hv_pointpillars_secfpn_6x8_160e_kitti-3d-3class_20220301_150306-37dc2420.pth'

model = init_model(config, checkpoint, device='cpu')
result, data = inference_detector(model, 'demo/data/kitti/000008.bin')  # 注意二元组解包

pred = result.pred_instances_3d
print('检出框数:', len(pred.scores_3d))
print('置信度前 5:', pred.scores_3d[:5])
print('bboxes_3d 形状:', pred.bboxes_3d.tensor.shape)   # 期望 (N, 7)
print('最高分框:', pred.bboxes_3d.tensor[pred.scores_3d.argmax()])
```

操作步骤：

1. 先完成第 5 节综合实践，把权重文件下载到本地（或放在仓库根目录）。
2. 把上面的脚本存为 `my_infer.py`（放在仓库根目录，不要放进 `mmdet3d-tutorial/` 之外的教程目录规则限制内——它是你的练习脚本，与本教程产物无关）。
3. 运行 `python my_infer.py`。

**需要观察的现象**：`bboxes_3d.tensor` 形状为 (N, 7)；最高分框的 7 个数值里，前 3 个是米制坐标（几十米量级）、中间 3 个是尺寸（1~5 米量级）、最后一个是弧度制朝向角。类别索引 2 对应 Car（KITTI 3class 的类顺序为 Pedestrian、Cyclist、Car）。

**预期结果**：能打印出若干个框。若报 `TypeError: cannot unpack non-iterable...` 之外的问题，多半是权重路径不对；具体检出数量与分数待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：`init_model` 为什么要在最后调用 `model.eval()`？

**答案**：`eval()` 把模型切到评估模式，影响 BatchNorm（用累计统计量而非批次统计量）和 Dropout（关闭随机丢弃）等层的行为。推理时必须处于该模式，否则结果不确定且精度下降。

**练习 2**：`pseudo_collate` 与把所有样本堆叠成一个 batch 张量的 collate 有什么区别？

**答案**：`pseudo_collate` 只把若干字典组装成"值为列表"的字典，不做数值堆叠。因为 3D 检测里每帧点数不同、图像尺寸可能不同，无法直接堆叠；真正的堆叠交给模型的 `data_preprocessor` 在 GPU 侧完成。

**练习 3**：`init_model` 和 `Base3DInferencer._init_model` 几乎是重复代码。如果要你重构，会保留哪个？

**答案**：开放题。合理答案：两者行为几乎一致（SyncBN 转换、train_cfg 置空、build、load_checkpoint、dataset_meta 恢复），可以让 `init_model` 内部复用或反之；但作为两个稳定公共入口（一个函数式、一个类式），保留少量重复换取 API 稳定性也是工程上的常见取舍。

### 4.5 单目与多模态 demo：为什么它们还需要一个 `.pkl` 文件

#### 4.5.1 概念说明

观察 `demo/data/kitti/`：点云 demo 只用了 `000008.bin`，旁边却还躺着 `000008.pkl`、`000008.png`、`000008.txt`——它们是给另外两个 demo 用的。单目 3D 检测和多模态检测**离不开相机标定信息**：要把 3D 框投影到图像上、或把图像特征与点云对齐，必须有内参矩阵（`cam2img`）、LiDAR 到相机的变换（`lidar2img`）等。这些矩阵随样本逐帧变化，必须从数据集的 info 文件（`.pkl`）里查出来，所以这两个 demo 的命令行多了一个 `infos` 位置参数。

#### 4.5.2 核心流程

```
单目（mono_det_demo.py）：
    img + infos.pkl + model + weights
    → MonoDet3DInferencer
    → 从 pkl 的 data_list 里按 cam_type 取出该相机的 img_path/cam2img
    → 管线（图像加载、resize、归一化）→ 模型 → pred_instances_3d（相机坐标系）

多模态（multi_modality_demo.py）：
    pcd + img + infos.pkl + model + weights
    → MultiModalityDet3DInferencer
    → 同时准备 lidar_points 与 images（含 lidar2img / depth2img）
    → 模型融合两路特征 → 3D 框 + 可投影到图像可视化
```

#### 4.5.3 源码精读

两个 demo 的参数组装与 `pcd_demo` 同构：单目版本把 `img` 和 `infos` 一起塞进 `inputs` 字典，见 [demo/mono_det_demo.py:L56-L63](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/demo/mono_det_demo.py#L56-L63)；多模态版本则是 `points`、`img`、`infos` 三件套，见 [demo/multi_modality_demo.py:L57-L67](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/demo/multi_modality_demo.py#L57-L67)。

底层实现里，`inference_mono_3d_detector` 会加载 info 文件，用输入图像的文件名去 info 的 `data_list` 里匹配对应记录（对不上就报错），取出该相机的 `cam2img` 等标定信息拼进数据字典，见 [mmdet3d/apis/inference.py:L334-L352](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/apis/inference.py#L334-L352)。

多模态版本 `inference_multi_modality_detector` 更进一步：按 `box_mode_3d` 决定取哪种变换矩阵——LiDAR 坐标系取 `lidar2img`（KITTI），深度坐标系取 `depth2img`（SUN RGB-D），还支持 `cam_type='all'` 一次喂多相机图像，见 [mmdet3d/apis/inference.py:L241-L278](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/apis/inference.py#L241-L278)。这些矩阵的语义在 u2-l1 坐标系一讲会展开。

三个 demo 的输入需求对比：

| demo | 点云 | 图像 | info pkl | 原因 |
| --- | --- | --- | --- | --- |
| `pcd_demo.py` | 必需 | — | — | 纯 LiDAR 检测无需标定 |
| `mono_det_demo.py` | — | 必需 | 必需 | 相机坐标系回归需要 `cam2img` |
| `multi_modality_demo.py` | 必需 | 必需 | 必需 | 融合 + 跨传感器投影需要变换矩阵 |

#### 4.5.4 代码实践

**实践目标**：看清 info pkl 里到底装了什么，理解"标定信息"为何必须逐帧提供。

示例代码（非项目原有，只读不改）：

```bash
python -c "
import mmengine
info = mmengine.load('demo/data/kitti/000008.pkl')
d = info['data_list'][0]
print('顶层键:', list(d.keys()))
print('images 下的相机:', list(d['images'].keys()))
cam2 = d['images']['CAM2']
print('CAM2 的键:', list(cam2.keys()))
print('cam2img 矩阵:'); import numpy as np; print(np.array(cam2['cam2img']))
"
```

**需要观察的现象**：`cam2img` 是 4×4 矩阵；`CAM2` 记录里还有 `lidar2img`、`img_path` 等字段（具体字段清单待本地验证）。

**预期结果**：`cam2img` 前三行对角线上的 fx、fy、cx、cy 与 KITTI 相机内参一致（fx 约为 700+ 量级）。这个矩阵乘以相机坐标系下的齐次点即可得到像素坐标——这正是单目 demo 必须传 info 的原因。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `pcd_demo.py` 不需要 info 文件？

**答案**：LiDAR 检测的输入输出都在 LiDAR 坐标系内完成，不涉及相机投影，也就不需要任何标定矩阵；`LidarDet3DInferencerLoader` 里那句 `axis_align_matrix=np.eye(4)` 只是占位。

**练习 2**：单目 demo 中，如果把输入图像换成另一帧但 info 还是旧的，会发生什么？

**答案**：`inference_mono_3d_detector` 会用图像文件名与 info 里记录的 `img_path` 基名做匹配，不一致时直接抛出 `ValueError: the info file of ... is not provided.`——因为用错标定矩阵会让 3D 框投影错位。

**练习 3**：KITTI 的多模态 demo `--cam-type` 应该传什么？nuScenes 单目呢？

**答案**：从源码默认值与文档看：KITTI 是 `CAM2`（多模态 `inference_multi_modality_detector` 的默认），nuScenes 是 `CAM_FRONT`（`inference_mono_3d_detector` 的默认）；nuScenes 多相机时应传 `all`。

## 5. 综合实践

把本讲所有知识串起来：**下载 PointPillars KITTI 预训练权重，分别用命令行方式和 API 方式对同一帧点云推理，并核对两边结果一致**。

### 步骤 1：下载预训练权重

权重链接来自官方 model zoo（[configs/pointpillars/README.md:L26](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/pointpillars/README.md#L26)）：

```bash
wget https://download.openmmlab.com/mmdetection3d/v1.0.0_models/pointpillars/hv_pointpillars_secfpn_6x8_160e_kitti-3d-3class/hv_pointpillars_secfpn_6x8_160e_kitti-3d-3class_20220301_150306-37dc2420.pth
```

（约 20 MB 量级；下载失败时可到 model zoo 页面找镜像。）

### 步骤 2：命令行方式跑通 pcd_demo

```bash
python demo/pcd_demo.py \
    demo/data/kitti/000008.bin \
    configs/pointpillars/pointpillars_hv_secfpn_8xb6-160e_kitti-3d-3class.py \
    hv_pointpillars_secfpn_6x8_160e_kitti-3d-3class_20220301_150306-37dc2420.pth \
    --device cpu \
    --pred-score-thr 0.3 \
    --out-dir outputs_pp
```

有 GPU 的机器可去掉 `--device cpu`。官方文档中同款命令的示例见 [docs/zh_cn/user_guides/inference.md:L16](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docs/zh_cn/user_guides/inference.md#L16)。

### 步骤 3：检查输出三件套

```bash
ls outputs_pp/preds/ outputs_pp/vis_lidar/
cat outputs_pp/preds/000008.json | head -c 600
```

预期看到（具体数值待本地验证）：

- `outputs_pp/vis_lidar/000008.png`：BEV 视角的点云图，上面画着带分数的 3D 框（Car 为主）；
- `outputs_pp/preds/000008.json`：含 `labels_3d`、`scores_3d`、`bboxes_3d`（N×7）、`box_type_3d: "LiDAR"` 四个键。

### 步骤 4：API 方式复现并交叉验证

运行 4.4.4 节的 `my_infer.py`（把 `device` 改成与步骤 2 一致），比较两种方式的检出框数是否一致；再对比 `my_infer.py` 打印的 `scores_3d` 与 JSON 里的 `scores_3d`。

### 步骤 5（选做）：感受阈值的作用

把 `--pred-score-thr` 从 0.3 调到 0.6 重跑，观察 PNG 上框数量的变化，再 `cat` JSON 确认框数**没有**变化——印证 4.3 节"阈值只影响画图"的结论。

**验收标准**：两条路线框数一致；JSON 四个键齐全；能口头说出每个键对应的源码行（4.3.3 节）。

## 6. 本讲小结

- MMDetection3D 有五条推理入口，分两层：三个 demo 脚本对应的 Inferencer（高层一站式），和 `init_model` / `inference_detector` 一族的底层 API（灵活可控）；两条路线最终都走 `model.test_step`。
- `demo/pcd_demo.py` 把命令行参数拆成"初始化参数（model/weights/device）"和"调用参数（inputs/阈值/输出目录）"两组，分别用于构造和调用 `LidarDet3DInferencer`。
- Inferencer 的核心技巧是运行时"偷梁换柱"：把配置中 test 管线的 `LoadPointsFromFile` 原地替换成 `LidarDet3DInferencerLoader`，使为数据集设计的管线能直接吃裸文件路径或 numpy 数组，其余 transform 一概不动。
- 推理主循环是四段式：preprocess（过管线）→ forward（`model.test_step`）→ visualize（`np.frombuffer` 读原始 bin 后交给可视化器，产出 `vis_lidar/*.png`）→ postprocess（`pred2dict` 拆出 `labels_3d/scores_3d/bboxes_3d/box_type_3d`，产出 `preds/*.json`）。
- 推理输出统一挂在 `Det3DDataSample.pred_instances_3d` 下；底层 API `inference_detector` 单帧返回 `(result, data)` **二元组**，解包是新手第一坑。
- `--pred-score-thr` 只过滤可视化，JSON 里保存的是全部预测；单目/多模态 demo 必须额外提供 info pkl，因为相机标定矩阵（`cam2img`/`lidar2img`）逐帧不同。

## 7. 下一步学习建议

到这里，你已经能"用"这个工具箱了，但每次都要依赖模型仓库（model zoo）。要真正理解它，接下来应该：

1. **学 u1-l4（仓库目录结构与源码地图）**：本讲反复提到的 `configs/`、`mmdet3d/apis/`、`demo/` 只是仓库的一角，下一讲把全仓库的模块依赖关系画成地图，为深读源码建立索引。
2. **学 u1-l5（配置系统入门）**：本讲你已经在命令行里传了配置文件路径，但还没打开看过它。下一讲拆解 PointPillars 配置的继承结构，理解 `model`、`test_dataloader.dataset.pipeline`（本讲的管线就是从这来的）等配置块。
3. 提前预告：推理时被替换的 `LoadPointsFromFile`、被反复提到的 `LiDARInstance3DBoxes` 和 `Det3DDataSample`，分别在第三单元（数据管线）和第二单元（核心数据结构）中深入剖析。带着本讲的直观印象去读，事半功倍。
