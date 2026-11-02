# u3-l1 Det3DDataset 基类剖析

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 info pkl 的顶层结构（`metainfo` + `data_list`）和单条 info 的标准字段，并解释它们是由谁、在什么阶段写入的。
2. 解释 `Det3DDataset` 构造参数中的三大约定：`ann_file`（标注文件）、`METAINFO`/`metainfo`（类别元信息与 `label_mapping`）、`modality`（`use_lidar`/`use_camera` 模态开关）。
3. 完整追踪 `load_data_list → parse_data_info → parse_ann_info → get_data_info → prepare_data` 这条调用链，说清一条 info 字典在每个环节被改写成了什么样子。
4. 理解 pipeline 列表是如何被注册器实例化并在 `prepare_data` 中消费 data dict 的，以及 `box_type_3d` 如何从数据集流向 3D 框对象。
5. 对比 KITTI、nuScenes、ScanNet 子类对 `parse_data_info`/`parse_ann_info` 的差异化重写。

本讲是单元 3（数据集与数据管线）的地基：后面讲具体数据集、transforms 与 GT 采样时，都以本讲的调用链为骨架。

## 2. 前置知识

在读本讲之前，请先确认理解下面三个概念（均在前两单元讲过，这里做一句话复习）：

- **info pkl**：`tools/create_data.py` 跑完数据转换后，在 `data/<数据集>/` 下生成的 pickle 文件（如 `kitti_infos_train.pkl`）。它把原始数据集分散的标注、标定、文件名统一成 mmdet3d 的「标准 info 字典」列表（见 u1-l4 的 create_data 流程）。
- **Det3DDataSample**（u2-l4）：数据管线的输出契约，含 `gt_instances_3d`、`pred_instances_3d` 等字段。本讲讲的是它的「上游」——data dict 是如何被一步步加工出来的。
- **BasePoints / BaseInstance3DBoxes 家族**（u2-l2、u2-l3）：三个坐标系（LiDAR/CAM/DEPTH）各有对应的点云与 3D 框子类。数据集类的 `box_type_3d` 参数决定了真值框最终被包装成哪个子类。

还需要两个新概念：

- **数据集类（Dataset）与数据管线（pipeline）的分工**：数据集类负责「读什么」（路径解析、标注解析、元信息），pipeline 负责「怎么变」（加载、增强、过滤、打包成 DataSample）。二者通过一个普通 Python dict（本讲称 data dict）交接。
- **MMEngine 的 BaseDataset**：mmdet3d 所有数据集类的公共基类，来自外部库 mmengine（源码不在本仓库中，可在本地环境的 `site-packages/mmengine/dataset/base_dataset.py` 阅读）。它定义了「懒初始化、序列化缓存、按索引取样本」的通用骨架，把「每条标注长什么样」这个因数据集而异的问题留给子类的 `parse_data_info` 回调。

> 名词提醒：部分早期资料会提到一个名为 `MODALITIES` 的常量；在当前代码中它并不存在——模态约定完全由构造参数 `modality` 字典（`use_lidar` / `use_camera` 两个布尔键）承担，见 4.2 节。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [mmdet3d/datasets/det3d_dataset.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/det3d_dataset.py) | 本讲主角：所有 3D 检测数据集的基类 |
| [mmdet3d/datasets/utils.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/utils.py) | `get_loading_pipeline` 等工具：从完整 pipeline 中挑出「加载类」步骤 |
| [mmdet3d/datasets/kitti_dataset.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/kitti_dataset.py) | 室外单帧 LiDAR 数据集子类（差异：地平面 plane、坐标系转换） |
| [mmdet3d/datasets/nuscenes_dataset.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/nuscenes_dataset.py) | 多帧 sweep 数据集子类（差异：valid mask、velocity、多相机拆分） |
| [mmdet3d/datasets/scannet_dataset.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/scannet_dataset.py) | 室内数据集子类（差异：axis_align_matrix、语义 mask、无 yaw 框） |
| [mmdet3d/datasets/waymo_dataset.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/waymo_dataset.py) | 唯一重写了 `load_data_list` 的子类，是我们观察 mmengine 基类行为的「镜子」 |
| [tools/dataset_converters/update_infos_to_v2.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/dataset_converters/update_infos_to_v2.py) | info 字典结构的「出生证明」：标准字段都在这里定义与填充 |
| [configs/_base_/datasets/kitti-3d-3class.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/datasets/kitti-3d-3class.py) | 数据集配置四要素中的 dataset 块，是基类构造参数的来源 |
| `mmengine.dataset.BaseDataset`（外部库，不在本仓库） | 读 pkl、回调 `parse_data_info`、缓存 data_list、构建 pipeline 的通用骨架 |

## 4. 核心概念与源码讲解

### 4.1 从 info pkl 到 Dataset：三层契约

#### 4.1.1 概念说明

`Det3DDataset` 自己并不直接打开 pkl 文件——读文件的是 mmengine 的 `BaseDataset`。整个体系是一份三层契约：

1. **pkl 顶层结构**（数据转换脚本保证）：pkl 反序列化后必须是 `dict(metainfo=..., data_list=[info1, info2, ...])`，即「一个元信息字典 + 一列样本 info」。
2. **单条 info 的标准字段**（同一脚本保证）：每条 info 描述一帧数据，含 `sample_idx`、`lidar_points`、`images`、`instances` 等键。
3. **子类回调**（数据集类实现）：`BaseDataset` 逐条把 info 交给 `self.parse_data_info(raw_data_info)`，由 mmdet3d 的数据集类把相对路径、裸标注加工成可训练的 data dict。

为什么要这样设计？因为「读 pkl、缓存、按索引取样本」对所有数据集都一样，而「每条标注长什么样」千差万别。把前者上收到 mmengine、后者下放到 `parse_data_info`，新增数据集时只需写一个方法（这正是 u8-l2 自定义数据集的基础）。

一个容易困惑的点：`Det3DDataset` 及大多数子类（KITTI、nuScenes、ScanNet……）都**没有**重写 `load_data_list`，只有 Waymo 重写了。这是否意味着 KITTI 的 pkl 没人读？不是——mmengine 基类提供了默认实现。证据就在 Waymo 的重写里：它的 docstring 只说「Add the load interval」（加上采样间隔），其余逻辑就是基类默认行为的逐行拷贝（见 4.1.3）。

#### 4.1.2 核心流程

数据集初始化（一次）：

```text
configs 中的 dataset=dict(type='KittiDataset', ann_file=..., ...)
        │  注册器路由到类，实例化
        ▼
Det3DDataset.__init__          # mmdet3d 层：modality/label_mapping/box_type 预处理
        │  super().__init__(...)
        ▼
mmengine BaseDataset.__init__  # 拼 data_root、存 pipeline 配置
        │  full_init()（除非 lazy_init=True）
        ▼
load_data_list()               # 读 ann_file pkl：
        │                        校验顶层 metainfo/data_list 键
        │                        对每条 raw_info 调 self.parse_data_info(raw_info)
        ▼
self.data_list = [data_info, ...]   # 后续按索引访问的底层数据
```

每次取样本（训练时被 DataLoader 反复调用）：

```text
dataset[i] → __getitem__ → prepare_data(i)
        │  get_data_info(i)：从 self.data_list 取出第 i 条（mmengine 会做反序列化）
        │  deepcopy 防止管线原地修改底层数据
        │  注入 box_type_3d / box_mode_3d
        │  空真值样本直接丢弃（训练态）
        ▼
self.pipeline(input_dict)      # 一串 TRANSFORMS，把 dict 逐站加工
        ▼
dict(inputs=..., data_samples=Det3DDataSample)   # 交给预处理器与模型
```

#### 4.1.3 源码精读

**info pkl 顶层结构的诞生**：转换脚本最后统一写成 `dict(metainfo=..., data_list=...)` 并 dump 成 pkl。以 nuScenes 为例：

- [tools/dataset_converters/update_infos_to_v2.py:378-388](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/dataset_converters/update_infos_to_v2.py#L378-L388)——把类别索引表 `categories`、数据集名、版本号装进 `metainfo`，把逐帧转换结果 `converted_list` 装进 `data_list`，用 `mmengine.dump(..., 'pkl')` 落盘。这就是 `BaseDataset.load_data_list` 所要求的顶层结构。

**单条 info 的标准骨架**（带注释的「官方字段说明」）：

- [tools/dataset_converters/update_infos_to_v2.py:158-184](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/dataset_converters/update_infos_to_v2.py#L158-L184)——`get_empty_standard_data_info` 定义一帧的全部字段：`sample_idx`、`lidar_points`（点云路径等）、`images`（多相机图像与标定）、`instances`（目标实例列表）等。所有 `None` 的可选键最终会被 `clear_data_info_unused_keys` 删掉，所以不同数据集的 info 只包含自己用到的字段。
- [tools/dataset_converters/update_infos_to_v2.py:28-67](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/dataset_converters/update_infos_to_v2.py#L28-L67)——单个 instance 的字段定义：`bbox`（2D 框）、`bbox_3d`（7 或 9 个数：`[x, y, z, w, h, l, yaw]`，可含速度）、`bbox_label_3d`、`num_lidar_pts`、`difficulty` 等。这些键名就是后面 `parse_ann_info` 中 `name_mapping` 的输入。

**mmengine 基类行为的「镜子」**——Waymo 重写的 `load_data_list`：

- [mmdet3d/datasets/waymo_dataset.py:183-236](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/waymo_dataset.py#L183-L236)——这段代码除 `load_interval` 抽稀外，就是基类默认逻辑的拷贝：① `load(self.ann_file)` 读 pkl（Waymo 用了自己 import 的 `load`，mmengine 基类用的是等价的 mmengine I/O）；② 断言顶层是含 `data_list` 与 `metainfo` 的 dict（L192-197）；③ 文件级 `metainfo` 通过 `self._metainfo.setdefault(k, v)` 并入（L210-211），但**优先级低于**类的 `METAINFO` 与构造参数 `metainfo`；④ 对每条 `raw_data_info` 调 `self.parse_data_info(...)`（L215-217），返回 dict 或 list[dict]（后者用于一帧拆多图的场合）。读这段代码 = 读懂所有未重写 `load_data_list` 的数据集的读取行为。

**mmdet3d 侧的接线**：

- [mmdet3d/datasets/det3d_dataset.py:126-133](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/det3d_dataset.py#L126-L133)——`Det3DDataset.__init__` 在做完自己的预处理后，把 `ann_file / metainfo / data_root / data_prefix / pipeline / test_mode` 原样转交给 mmengine 基类；基类随即（非懒初始化时）执行 `full_init()` → `load_data_list()`。

#### 4.1.4 代码实践

**实践目标**：亲手看到 pkl 顶层结构与一条 info 的真实字段，验证「metainfo + data_list」契约。

**操作步骤**（需要已按 u3-l2 生成 KITTI 或 nuScenes 的 info pkl；若暂无数据，先做步骤 3 的源码阅读替代）：

1. 用一小段脚本（示例代码，非项目原有文件）检查 pkl：

   ```python
   # check_info.py（示例代码）
   import mmengine
   infos = mmengine.load('data/kitti/kitti_infos_train.pkl')
   print(type(infos), list(infos.keys()))          # 期望: dict_keys(['metainfo', 'data_list'])
   print(infos['metainfo'])                        # categories 等文件级元信息
   info = infos['data_list'][0]
   print(sorted(info.keys()))                      # 单帧字段
   print(info['lidar_points']['lidar_path'])       # 应是相对文件名，如 '000001.bin'
   print(info['instances'][0])                     # 单个目标的全部键
   ```

2. 对照 [update_infos_to_v2.py:28-67](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/dataset_converters/update_infos_to_v2.py#L28-L67) 的字段注释，逐键写出中文含义。
3. 无数据时的替代路径：直接阅读 [tools/dataset_converters/update_infos_to_v2.py:391-509](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/dataset_converters/update_infos_to_v2.py#L391-L509) 的 `update_kitti_infos`，看 KITTI 的 `location/dimensions/rotation_y` 如何被组装成 `instances[i]['bbox_3d']`，并注意 `R0_rect @ Tr_velo_to_cam` 被算成 `images['CAM2']['lidar2cam']`（L434-437）。

**需要观察的现象**：`data_list` 长度等于训练帧数；`lidar_path` 是不带目录的文件名（目录前缀由 `data_prefix` 在 `parse_data_info` 里拼）；KITTI 的 `bbox_3d` 是**相机坐标系**下的 7 参数（这解释了 4.3 节 KITTI 为何要做坐标转换）。

**预期结果**：打印出的键集合与 `get_empty_standard_data_info` 定义的字段子集一致。具体输出与数据集版本有关，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Det3DDataset` 不需要重写 `load_data_list`，而 `WaymoDataset` 需要？
**答案**：mmengine 的 `BaseDataset` 已提供默认 `load_data_list`：读 pkl 顶层 `{metainfo, data_list}` 并逐条回调 `parse_data_info`。Waymo 重写只是为了额外的 `load_interval`（按间隔抽稀大容量数据集），其函数体其余部分与基类默认行为一致。

**练习 2**：pkl 文件级 `metainfo` 与类的 `METAINFO`、构造参数 `metainfo` 冲突时，谁赢？
**答案**：类与构造参数赢。文件级 metainfo 是用 `self._metainfo.setdefault(k, v)` 并入的（见 [waymo_dataset.py:207-211](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/waymo_dataset.py#L207-L211) 的注释），`setdefault` 不会覆盖已存在的键。

**练习 3**：`data_list` 里的一条 info 是相对路径还是绝对路径？何时变成绝对路径？
**答案**：相对（转换脚本只存文件名，便于数据集整体搬家）。在 `parse_data_info` 中与 `data_prefix` 拼接成绝对路径，见 4.3 节。

### 4.2 构造参数与元信息约定：modality、METAINFO 与 label_mapping

#### 4.2.1 概念说明

`Det3DDataset.__init__` 是全库数据集的「参数规范中心」。四个约定必须掌握：

- **`ann_file`**：info pkl 的路径。注意它常与 `data_root` 配合——mmengine 基类会把 `data_root` 拼到 `ann_file` 与各 `data_prefix` 前面，所以配置里写的是相对路径 `kitti_infos_train.pkl`。
- **`modality`**：`dict(use_lidar=..., use_camera=...)` 两个布尔键。缺省的键一律补成 `False`，且两者不能同时为 `False`（断言报错）。它是 `parse_data_info` 的分支开关：只用 LiDAR 就不解析图像路径，只用相机就不解析点云。
- **`METAINFO` / `metainfo`**：类属性 `METAINFO` 声明该数据集的**全量**类别（如 KITTI 8 类），构造参数 `metainfo=dict(classes=...)` 可声明**子集**（如 KITTI-3Class 只训 3 类）。当传入了子集时，基类会构建 `label_mapping`：全量类别 id → 训练类别 id，未选中的类别映射为 -1（dontcare）。
- **`box_type_3d`**：声明本数据集的 3D 框应包装成哪个坐标系子类（`'LiDAR'/'Camera'/'Depth'`），经 `get_box_type` 解析成「框类 + Box3DMode 枚举」二元组，最终写入每条样本的 data dict。

#### 4.2.2 核心流程

```text
modality dict
   ├─ 补全缺失键为 False ──► 断言 use_lidar or use_camera
   └─ 存为 self.modality（parse_data_info 的分支开关）

metainfo 参数里有 classes？
   ├─ 是：构建 label_mapping，全量 id → 子集 id（未选中 → -1）
   └─ 否：恒等映射 label_mapping[i] = i（-1 仍 → -1）

box_type_3d 字符串 ──► get_box_type ──► (框子类, Box3DMode)
                                  └─► 注入 metainfo，供全流程共享
```

label 映射的数学关系很简单：设全量类别为 \( C = (c_0, \dots, c_{M-1}) \)，训练子集为 \( S = (s_0, \dots, s_{K-1}) \)，则

\[
\mathrm{label\_mapping}(i) =
\begin{cases}
j, & C_i = S_j \\
-1, & C_i \notin S
\end{cases}
\]

#### 4.2.3 源码精读

- [mmdet3d/datasets/det3d_dataset.py:88-100](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/det3d_dataset.py#L88-L100)——modality 补全与断言：缺的键补 `False`，`use_lidar` 与 `use_camera` 至少开一个，否则报错并提示数据集类名。
- [mmdet3d/datasets/det3d_dataset.py:104-124](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/det3d_dataset.py#L104-L124)——`label_mapping` 构建：传了子集时先把所有全量 id 初始化为 -1，再把子集中的类别改写为其子集下标；`num_ins_per_cat` 同时按子集长度清零，供初始化结束时的实例数统计。
- [mmdet3d/datasets/det3d_dataset.py:102](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/det3d_dataset.py#L102) 与 [mmdet3d/structures/bbox_3d/utils.py:150-180](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/utils.py#L150-L180)——`get_box_type` 把字符串解析成 `(LiDARInstance3DBoxes, Box3DMode.LIDAR)` 等二元组，非法值直接 `ValueError`。
- [mmdet3d/datasets/det3d_dataset.py:136-137](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/det3d_dataset.py#L136-L137)——基类初始化完成后，把 `box_type_3d` 与 `label_mapping` 写回 `self.metainfo`，让 Runner 里的其他组件（可视化、评估器）能取到同一份约定。
- [mmdet3d/datasets/det3d_dataset.py:139-156](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/det3d_dataset.py#L139-L156)——初始化收尾：用 `num_ins_per_cat` 打印「数据集长度 + 每类实例数」的 AsciiTable。你每次训练日志开头看到的那张类别统计表就来自这里（`num_ins_per_cat` 在 `parse_ann_info` 里逐帧累加）。
- [mmdet3d/datasets/kitti_dataset.py:55-60](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/kitti_dataset.py#L55-L60)——KITTI 的 `METAINFO`：8 个全量类别 + 每类可视化颜色 palette。对比 [configs/_base_/datasets/kitti-3d-3class.py:4](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/datasets/kitti-3d-3class.py#L4) 只声明 `['Pedestrian', 'Cyclist', 'Car']` 3 类，就是「全量 vs 子集」的活例子。
- [configs/_base_/datasets/kitti-3d-3class.py:108-123](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/datasets/kitti-3d-3class.py#L108-L123)——训练集 dataset 块：`data_root='data/kitti/'` + 相对 `ann_file` + `data_prefix=dict(pts='training/velodyne_reduced')` + `modality` + `metainfo` + `box_type_3d='LiDAR'`，每个键都能对应到 4.2.1 的某个约定。外层再包一层 `RepeatDataset`（重复 2 倍，见 u1-l5）。

#### 4.2.4 代码实践

**实践目标**：验证「子集训练 → label_mapping 把未选类别打为 -1」。

**操作步骤**（示例代码）：

```python
# label_mapping_check.py（示例代码）
from mmdet3d.datasets import KittiDataset
import numpy as np

pipeline = [dict(type='LoadPointsFromFile', coord_type='LIDAR',
                 load_dim=4, use_dim=4),
            dict(type='Pack3DDetInputs', keys=['points'])]
ds = KittiDataset(
    data_root='data/kitti/', ann_file='kitti_infos_train.pkl',
    data_prefix=dict(pts='training/velodyne_reduced'),
    pipeline=pipeline, modality=dict(use_lidar=True, use_camera=False),
    test_mode=True,
    metainfo=dict(classes=['Car']))          # 只训 Car 一个类
print('label_mapping:', ds.label_mapping)    # 期望 Car(2)→0，Pedestrian(0)/Cyclist(1)→-1，其余→-1
print('metainfo classes:', ds.metainfo['classes'])
print('box_type_3d:', ds.box_type_3d, ds.box_mode_3d)
```

**需要观察的现象**：`label_mapping` 中只有 `2 → 0`（Car 的全量 id 是 2），其余全量 id 都是 `-1`；`box_type_3d` 是 `LiDARInstance3DBoxes` 类对象而非字符串。

**预期结果**：与上述一致；若 mmcv/mmengine 未安装或数据缺失会报错，此时代码阅读替代：手工推演 [det3d_dataset.py:104-115](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/det3d_dataset.py#L104-L115) 对 `classes=['Car']` 的输出。运行结果**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：把 `modality=dict(use_lidar=True)` 传给 KittiDataset（漏掉 `use_camera`）会怎样？
**答案**：不会报错。缺的键在 [det3d_dataset.py:93-95](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/det3d_dataset.py#L93-L95) 被补成 `False`，因此等价于 `dict(use_lidar=True, use_camera=False)`。只有两个键都为 `False` 才触发断言。

**练习 2**：为什么 `label_mapping[-1] = -1` 必须单独写？
**答案**：-1 表示 dontcare/被忽略类别，转换脚本把不在全量类别里的目标也存成 -1（见 [update_infos_to_v2.py:469-471](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/dataset_converters/update_infos_to_v2.py#L469-L471)）。映射必须把 -1 送回 -1，后续 `_remove_dontcare` 才能统一按 `label > -1` 过滤。

**练习 3**：`self.metainfo['box_type_3d']` 写入的是字符串还是类？
**答案**：字符串（构造参数原值，[det3d_dataset.py:136](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/det3d_dataset.py#L136)）；类对象与 mode 枚举分别存在 `self.box_type_3d` / `self.box_mode_3d` 上。

### 4.3 parse_data_info 与 parse_ann_info：一条 info 的两次加工

#### 4.3.1 概念说明

这两个方法是子类最常重写的钩子，分工明确：

- **`parse_data_info(info)`**（对「帧」加工）：把相对路径拼成绝对路径、提取默认相机的标定矩阵、按 `test_mode` 决定是否附带标注。输出仍是 info 本身（原地增强后返回）。
- **`parse_ann_info(info)`**（对「实例」加工）：把 `instances` 列表（一列小 dict）拼成 `ann_info`（一列 numpy 数组），把裸的 `bbox_3d` 数组包装成 3D 框对象（`LiDARInstance3DBoxes` 等），并应用 `label_mapping`。

关键认知：**info 里的 `bbox_3d` 坐标系因数据集而异**。KITTI 存的是相机系 7 参数，nuScenes 存的是 LiDAR 系（中心在几何中心 `(0.5, 0.5, 0.5)`），ScanNet 存的是 Depth 系 6 参数（无 yaw）。所以每个子类的 `parse_ann_info` 都要做「包装 + 坐标转换 + 兜底空标注」三件事，这正是各数据集类的主要差异所在。

#### 4.3.2 核心流程

`parse_data_info`（基类版）：

```text
输入: info（来自 pkl 的一条 raw_info）
 1. use_lidar:
      lidar_points.lidar_path = data_prefix['pts'] / lidar_path   # 相对→绝对
      顶层冗余键: num_pts_feats, lidar_path                        # 方便管线直接读
      lidar_sweeps 中每帧: 按 'samples'/'sweeps' 目录分别拼前缀    # nuScenes 多帧
 2. use_camera:
      每个 images[cam].img_path = data_prefix[cam] 或 data_prefix['img'] / img_path
      default_cam_key 指定默认相机 → 顶层 img_path / lidar2cam / cam2img / lidar2img
      （lidar2img 缺失时用 cam2img @ lidar2img… 即 cam2img @ lidar2cam 补算）
 3. train 态: info['ann_info']     = parse_ann_info(info)
    test 态:  info['eval_ann_info'] = parse_ann_info(info)   # 若 load_eval_anns
输出: info（路径全绝对、可能带 ann_info / eval_ann_info）
```

`parse_ann_info`（基类版）：

```text
输入: info['instances'] = [ {bbox, bbox_label, bbox_3d, ...}, ... ]
 1. 逐键收集每个实例的同名字段 → 拼成等长 numpy 数组
 2. 名字重映射: bbox_label_3d→gt_labels_3d, bbox_3d→gt_bboxes_3d, ...（name_mapping 表）
 3. label 类字段过 label_mapping（全量 id → 训练 id）
 4. label → int64；mapped 数值字段 → float32
 5. 统计 num_ins_per_cat；instances 原始列表原样保留
输出: ann_info dict（gt_bboxes_3d 仍是裸 ndarray！包装成框对象是子类的活）
```

注意基类 `parse_ann_info` 的输出里 `gt_bboxes_3d` 是**裸 numpy 数组**；把它包成 3D 框对象（以及处理空标注、dontcare、坐标系）都在子类完成。

#### 4.3.3 源码精读

**基类 `parse_data_info`**：

- [mmdet3d/datasets/det3d_dataset.py:278-295](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/det3d_dataset.py#L278-L295)——LiDAR 分支：拼接主帧点云绝对路径；把 `num_pts_feats`、`lidar_path` 冗余复制到 info 顶层；`lidar_sweeps`（nuScenes 的历史帧）按路径里是否含 `samples` 决定用 `data_prefix['pts']` 还是 `data_prefix['sweeps']` 拼前缀。
- [mmdet3d/datasets/det3d_dataset.py:297-319](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/det3d_dataset.py#L297-L319)——相机分支：逐相机拼图像路径；若设了 `default_cam_key`（KITTI 是 `'CAM2'`），把该相机的 `img_path/lidar2cam/cam2img/lidar2img` 提升到 info 顶层，`lidar2img` 缺失时用矩阵乘 `cam2img @ lidar2cam` 补出。
- [mmdet3d/datasets/det3d_dataset.py:321-325](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/det3d_dataset.py#L321-L325)——训练态写入 `info['ann_info']`；测试态在 `load_eval_anns=True` 时写入 `info['eval_ann_info']`（供 Evaluator 使用，避免走管线被增强污染）。

**基类 `parse_ann_info`**：

- [mmdet3d/datasets/det3d_dataset.py:218-227](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/det3d_dataset.py#L218-L227)——`name_mapping` 改名表：实例字段名 → 管线/模型使用的名字，如 `bbox_3d → gt_bboxes_3d`、`bbox_label_3d → gt_labels_3d`、`velocity → velocities`。
- [mmdet3d/datasets/det3d_dataset.py:228-254](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/det3d_dataset.py#L228-L254)——逐字段拼接：`instances` 为空直接返回 `None`（空帧交给子类兜底）；名字含 `label` 的字段过 `label_mapping` 并转 `int64`，映射表内的数值字段转 `float32`。
- [mmdet3d/datasets/det3d_dataset.py:257-259](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/det3d_dataset.py#L257-L259)——按标签累加 `num_ins_per_cat`，喂给初始化结束时的类别统计表。
- [mmdet3d/datasets/det3d_dataset.py:158-177](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/det3d_dataset.py#L158-L177)——`_remove_dontcare`：按 `gt_labels_3d > -1` 的布尔掩码过滤所有数组字段（-1 即 dontcare）。

**子类差异**（本讲只看骨架，u3-l3/u3-l4 展开）：

- **KITTI**（[kitti_dataset.py:92-133](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/kitti_dataset.py#L92-L133)）：`parse_data_info` 在调用 `super()` 前，把 KITTI 的地平面方程从**相机系**换算到**雷达系**（ObjectNoise 增强需要）；[kitti_dataset.py:135-173](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/kitti_dataset.py#L135-L173) 的 `parse_ann_info` 则先兜底空标注、再 `_remove_dontcare`，最后把相机系裸框 `CameraInstance3DBoxes(...).convert_to(LiDAR, inv(lidar2cam))` 转成雷达系框对象（呼应 u2-l1 的 `lidar2cam = R0_rect @ Tr_velo_to_cam`）。
- **nuScenes**（[nuscenes_dataset.py:133-191](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/nuscenes_dataset.py#L133-L191)）：`parse_ann_info` 先用 `_filter_with_mask` 过滤无效目标（默认 `num_lidar_pts > 0`，可切 `bbox_3d_isvalid`，见 [nuscenes_dataset.py:112-131](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/nuscenes_dataset.py#L112-L131)）；`with_velocity=True` 时把速度两列拼到 7 参数后成 9 参数（NaN 清零）；框中心按 `(0.5, 0.5, 0.5)` 几何中心构造。`parse_data_info`（[nuscenes_dataset.py:193-247](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/nuscenes_dataset.py#L193-L247)）在 `mv_image_based` 模式下把一帧拆成 6 个相机的样本，返回 list[dict]。
- **ScanNet**（[scannet_dataset.py:129-154](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/scannet_dataset.py#L129-L154)）：补充 `axis_align_matrix`（轴对齐变换，缺失则警告并退化为单位阵）、`pts_instance_mask_path/pts_semantic_mask_path` 绝对路径与 `seg_label_mapping`；[scannet_dataset.py:156-174](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/scannet_dataset.py#L156-L174) 把 6 参数（无 yaw）裸框包成 `DepthInstance3DBoxes`。

#### 4.3.4 代码实践

**实践目标**（本讲主实践的上半段）：对比 KITTI 与 nuScenes 的 `parse_data_info` 输出字典，弄清「一条 info → 一条 data dict」的字段变化。

**操作步骤**：

1. 并排打开 [kitti_dataset.py:92-133](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/kitti_dataset.py#L92-L133) 与 [nuscenes_dataset.py:193-247](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/nuscenes_dataset.py#L193-L247)，各列一张「输入字段 → 输出字段」表。
2. 回答三个问题：① 两者新增了哪些基类没有的键？（KITTI：`plane`；nuScenes：`sweeps → lidar_sweeps` 的路径改写、`token`）② 相同字段中谁的来源不同？（`lidar_path`：KITTI 直接 `training/velodyne_reduced` 拼接；nuScenes 主帧在 `samples`、sweep 在 `sweeps` 目录）③ `ann_info` 中 `gt_bboxes_3d` 分别是什么类型/维度？（KITTI：`LiDARInstance3DBoxes (N,7)`，由相机系转换而来；nuScenes：`LiDARInstance3DBoxes (N,9)`，几何中心直传 + 速度拼接）
3. 有本地环境时可动态验证（示例代码）：用 `test_mode=True, lazy_init` 关闭自动初始化的思路不可行时，直接手动调用：

   ```python
   # parse_probe.py（示例代码）
   ds = KittiDataset(data_root='data/kitti/', ann_file='kitti_infos_train.pkl',
                     data_prefix=dict(pts='training/velodyne_reduced'),
                     pipeline=[], test_mode=True)  # 空 pipeline 即可观察 parse 后的原始 dict
   info = ds.data_list[0]
   print(sorted(info.keys()))
   print(type(info['eval_ann_info']['gt_bboxes_3d']))
   ```

**需要观察的现象**：`parse` 后的 info 顶层多了绝对路径键与（测试态）`eval_ann_info`；`gt_bboxes_3d` 已是框对象而非 ndarray。

**预期结果**：与步骤 2 的表格一致；`pipeline=[]` 时 mmengine 仍会执行 load_data_list，因此 `ds.data_list` 可直接观察。运行结果**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 KITTI 的 `parse_ann_info` 里要对 `lidar2cam` 取逆？
**答案**：KITTI 标注与 info 里的 `bbox_3d` 是相机系坐标，而 KittiDataset 的 `box_type_3d='LiDAR'`。`inv(lidar2cam)` 是 cam2lidar 矩阵，`CameraInstance3DBoxes.convert_to(LIDAR, inv(lidar2cam))` 借助 u2-l1 的转换引擎一步完成坐标、尺寸顺序与 yaw 符号的改写（[kitti_dataset.py:166-172](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/kitti_dataset.py#L166-L172)）。

**练习 2**：基类 `parse_ann_info` 在 `instances` 为空时返回什么？空帧最终如何变成合法 ann_info？
**答案**：返回 `None`；由各子类兜底为全零数组（如 KITTI 的 `(0,7)` float32、nuScenes 含速度时 `(0,9)`，见 [kitti_dataset.py:153-157](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/kitti_dataset.py#L153-L157)、[nuscenes_dataset.py:159-166](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/nuscenes_dataset.py#L159-L166)）。

**练习 3**：`eval_ann_info` 为什么只在测试态生成，而不是复用 `ann_info`？
**答案**：测试态数据不希望被增强改写、但评估需要原始真值；`eval_ann_info` 是 parse 阶段的原始标注快照（[det3d_dataset.py:324-325](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/det3d_dataset.py#L324-L325)），会原样进入 `Det3DDataSample.eval_ann_info` 字段供 Evaluator 使用（u2-l4）。

### 4.4 prepare_data 与 pipeline：从 data dict 到 Det3DDataSample 的最后一公里

#### 4.4.1 概念说明

`prepare_data` 是 `__getitem__` 的实际干活者，也是**数据集与数据管线的交接面**。它做四件事：取原始 data dict、深拷贝防污染、注入框类型约定、过管线。管线本身则由 mmengine 基类在初始化时用**TRANSFORMS 注册表**把配置里的 `list[dict]` 实例化成一串可调用对象（`Compose`），这正是 u1-l5「config 的 `type='xxx'` 路由到类」在数据侧的体现。

另外两个配套工具要认识：

- `get_ann_info` / `get_cat_ids`：给 Evaluator 钩子与 CBGSDataset（类别均衡采样包装器，u8-l2）用的查询接口。
- `get_loading_pipeline`（utils.py）：从完整 pipeline 中挑出「加载类」步骤。测试与可视化阶段需要用**不含增强**的管线重新加载一份干净数据（例如 TTA、可视化真值），这个函数就是抽取规则。

#### 4.4.2 核心流程

```text
prepare_data(i):
  input_dict = get_data_info(i)            # mmengine: 从 data_list 反序列化第 i 条
  input_dict = deepcopy(input_dict)        # 防止 pipeline 原地改坏底层数据
  input_dict['box_type_3d'], ['box_mode_3d'] = ...   # 告诉 LoadAnnotations3D 该包成什么框
  if 训练态 and filter_empty_gt: 无真值 → return None（让 __getitem__ 换一条）
  example = self.pipeline(input_dict)      # LoadPointsFromFile → 增强 → Pack3DDetInputs
  if 训练态 and filter_empty_gt: 增强后仍无真值 → return None
  return example                          # dict(inputs=..., data_samples=Det3DDataSample)
```

管线内一次 `train_pipeline` 的字段流转（以 KITTI 为例）：

```text
输入 dict: lidar_path, num_pts_feats, ann_info{gt_bboxes_3d(框对象), gt_labels_3d}, box_type_3d...
  LoadPointsFromFile    → +points(LiDARPoints), +lidar2cam 等
  LoadAnnotations3D    → +gt_bboxes_3d, +gt_labels_3d（从 ann_info 拷到顶层）
  ObjectSample / ObjectNoise / RandomFlip3D / GlobalRotScaleTrans → 同步变换 points 与框
  PointsRangeFilter / ObjectRangeFilter / PointShuffle → 范围过滤
  Pack3DDetInputs       → dict(inputs=points, data_samples=Det3DDataSample(gt_instances_3d=...))
```

#### 4.4.3 源码精读

- [mmdet3d/datasets/det3d_dataset.py:359-406](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/det3d_dataset.py#L359-L406)——`prepare_data` 全文：L370 取原始 dict，L373 深拷贝，L376-378 注入 `box_type_3d/box_mode_3d`（后续 `LoadAnnotations3D` 据此把 ndarray 包成对应框子类），L381-383 管线前空真值过滤，L385 过管线，L387-392 管线后空真值过滤（增强可能把所有真值移出感知范围），L394-404 可选打印管线前后实例数变化（`show_ins_var=True` 调试开关，统计逻辑在 [det3d_dataset.py:329-357](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/det3d_dataset.py#L329-L357)）。
- [configs/_base_/datasets/kitti-3d-3class.py:41-67](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/datasets/kitti-3d-3class.py#L41-L67)——`train_pipeline` 配置：这里每个 `dict(type=...)` 都会在数据集初始化时被 TRANSFORMS 注册器实例化，运行时按序作用于 data dict（各 transform 细节在 u3-l5~u3-l8）。
- [mmdet3d/datasets/det3d_dataset.py:179-198](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/det3d_dataset.py#L179-L198)——`get_ann_info`：供 evalhook 等外部组件按索引查标注的公开 API。
- [mmdet3d/datasets/det3d_dataset.py:408-423](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/det3d_dataset.py#L408-L423)——`get_cat_ids`：返回样本内类别集合。docstring 点明这是 `CBGSDataset`/`ClassBalancedDataset` 包装器对被包装数据集的硬性要求。
- [mmdet3d/datasets/utils.py:18-49](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/utils.py#L18-L49)——`is_loading_function`：判断某个 transform 是否属于「加载类」白名单（LoadPointsFromFile、LoadAnnotations3D、Pack3DDetInputs 等）；对 `MultiScaleFlipAug3D` 返回 `None` 表示需要递归看它内部包裹的 transforms。
- [mmdet3d/datasets/utils.py:52-112](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/utils.py#L52-L112)——`get_loading_pipeline`：过滤出加载类子序列，并断言至少要有一个加载步骤（否则配置错误）。它的典型消费者是 Inferencer（u1-l3 用它构造只加载不增强的管线）与可视化流程。

#### 4.4.4 代码实践

**实践目标**：亲眼看到 data dict 过管线前后的形态差异，验证 4.4.2 的流转图。

**操作步骤**（示例代码，需本地有 KITTI 数据与已安装环境；无数据则按步骤 3 做源码阅读替代）：

```python
# pipeline_probe.py（示例代码）
from mmdet3d.datasets import KittiDataset
from mmdet3d.datasets.transforms import LoadPointsFromFile, LoadAnnotations3D, Pack3DDetInputs

pipeline = [dict(type='LoadPointsFromFile', coord_type='LIDAR', load_dim=4, use_dim=4),
            dict(type='LoadAnnotations3D', with_bbox_3d=True, with_label_3d=True),
            dict(type='Pack3DDetInputs', keys=['points', 'gt_bboxes_3d', 'gt_labels_3d'])]
ds = KittiDataset(data_root='data/kitti/', ann_file='kitti_infos_train.pkl',
                  data_prefix=dict(pts='training/velodyne_reduced'),
                  pipeline=pipeline, test_mode=False)

raw = ds.get_data_info(0)                 # 管线前
print('before keys:', sorted(raw.keys()))
out = ds.prepare_data(0)                  # 管线后
print('after keys:', sorted(out.keys()))  # 期望只剩 inputs / data_samples
print(out['data_samples'].gt_instances_3d)
```

2. 对比打印：`before` 有 `lidar_path/ann_info/box_type_3d...`，`after` 只剩 `inputs` 与 `data_samples`（Pack3DDetInputs 的收口，u3-l8）。
3. 源码阅读替代：沿 [kitti-3d-3class.py:41-67](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/datasets/kitti-3d-3class.py#L41-L67) 的顺序，给每个 transform 写一行「消费了哪些键、产出了哪些键」。

**需要观察的现象**：管线是「先加字段、再改字段、最后收口成 inputs/data_samples」的单向流水；`ann_info` 里的框对象经 `LoadAnnotations3D` 变成顶层 `gt_bboxes_3d` 后继续被增强类消费。

**预期结果**：`out['data_samples'].gt_instances_3d.bboxes_3d` 是 `LiDARInstance3DBoxes`，形状 `(N, 7)`；`labels_3d` 是 `(N,)` int64 张量。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`prepare_data` 里为什么要 `copy.deepcopy(ori_input_dict)`？
**答案**：pipeline 中的 transform 会对 dict 做原地修改（如 `parse_data_info` 之后的路径覆写、增强对 points/boxes 的变换）。`self.data_list` 是全训练周期共享的底层存储，不深拷贝的话第一轮 epoch 就会把底层数据改坏（[det3d_dataset.py:372-373](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/det3d_dataset.py#L372-L373)）。

**练习 2**：`filter_empty_gt=True` 时一共在几个位置过滤空真值？为什么需要多次？
**答案**：两处：管线前（parse 出的 `ann_info` 为空，L381-383）与管线后（增强可能把真值全部移出 `point_cloud_range`，L387-392）。返回 `None` 后由 mmengine 的 `__getitem__` 随机换一条重试，保证 batch 不出现空样本。

**练习 3**：`get_loading_pipeline` 为什么单独处理 `MultiScaleFlipAug3D`？
**答案**：它本身不是加载器，而是把一组 transform 包成测试时增强的包装器（u7-l4）；判断它是否含加载步骤必须递归展开其内部 `transforms`（[utils.py:42-43](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/utils.py#L42-L43) 与 [utils.py:100-106](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/utils.py#L100-L106)）。

## 5. 综合实践

**任务**（对应本讲规格中的 practice_task）：写出「一条 info 字典 → 一条训练样本 data_dict」的完整字段变化伪代码。要求覆盖本讲全部四个最小模块：pkl 顶层结构 → 构造参数约定 → 两次 parse → 管线加工。

**参考伪代码**（以 KITTI 训练态为例，请对照源码逐行核实）：

```text
# ── 第 0 层：pkl 顶层（update_infos_to_v2.py 保证）─────────────
pkl = { metainfo: {categories, dataset, ...},        # 文件级元信息，setdefault 并入
        data_list: [ raw_info, ... ] }               # 每帧一条

# ── 第 1 层：mmengine load_data_list（waymo_dataset.py:183-236 为可读拷贝）──
raw_info = pkl['data_list'][i]
data_info = KittiDataset.parse_data_info(raw_info)   # 回调进入 mmdet3d

# ── 第 2 层：KittiDataset.parse_data_info（kitti_dataset.py:92-133）────────
  + plane: 相机系地平面 → 雷达系 4 参数              # 子类私有加工
  + lidar_points.lidar_path: '000001.bin' → 'data/kitti/training/velodyne_reduced/000001.bin'
  + 顶层冗余键 num_pts_feats, lidar_path
  （use_camera 时: 各 CAM img_path 拼前缀；CAM2 的 lidar2cam/cam2img/lidar2img 提升到顶层）
  + ann_info = parse_ann_info(info)                  # 训练态

# ── 第 3 层：Det3DDataset.parse_ann_info（det3d_dataset.py:200-261）─────────
  instances: [ {bbox, bbox_label, bbox_3d(相机系7参数), difficulty, ...}, ... ]
    → 逐键拼数组: bbox_3d → gt_bboxes_3d(ndarray, float32)
                bbox_label_3d → gt_labels_3d(int64, 已过 label_mapping: 全量id→训练id/-1)
                bbox → gt_bboxes, difficulty → difficulty ...
  （KITTI 继续加工: _remove_dontcare 剔除 label=-1；
    CameraInstance3DBoxes(gt_bboxes_3d).convert_to(LiDAR, inv(lidar2cam))
    → gt_bboxes_3d 变成 LiDARInstance3DBoxes 对象）

# ── 第 4 层：prepare_data（det3d_dataset.py:359-406）──────────────────────
data_info = deepcopy(data_info)
  + box_type_3d = LiDARInstance3DBoxes 类, box_mode_3d = Box3DMode.LIDAR
  管线前检查: gt_labels_3d 为空 → 丢弃换样

# ── 第 5 层：pipeline（kitti-3d-3class.py:41-67，TRANSFORMS 实例化）────────
  LoadPointsFromFile   + points: LiDARPoints(N,4)
  LoadAnnotations3D    + 顶层 gt_bboxes_3d / gt_labels_3d（源自 ann_info）
  ObjectSample/ObjectNoise/RandomFlip3D/GlobalRotScaleTrans  ~ 同步变换 points 与框
  PointsRangeFilter/ObjectRangeFilter/PointShuffle           ~ 范围过滤与重排
  Pack3DDetInputs      → dict(inputs=points, data_samples=Det3DDataSample(
                            gt_instances_3d=InstanceData(bboxes_3d, labels_3d)))
  管线后检查: 增强后无真值 → 丢弃换样

# 交付物：把上述伪代码抄成本地笔记，并在每一行右侧标注源码 文件:行号。
```

**验收标准**：合上讲义，你能向别人解释 `gt_bboxes_3d` 从「相机系 7 个 float」到「`LiDARInstance3DBoxes` 张量」之间经历的每一次改写发生在哪个文件的哪一行。

## 6. 本讲小结

- info pkl 的顶层契约是 `dict(metainfo, data_list)`，由转换脚本写入；读取与逐条回调 `parse_data_info` 是 mmengine `BaseDataset.load_data_list` 的职责，Waymo 的重写是观察该行为的最好副本。
- `Det3DDataset.__init__` 落实三大约定：`modality`（`use_lidar`/`use_camera` 开关，缺失补 False 且不能全关）、`METAINFO`/`metainfo`（全量类别 vs 训练子集，靠 `label_mapping` 把未选类别打成 -1）、`box_type_3d`（经 `get_box_type` 解析为框子类 + Box3DMode，并写入 `self.metainfo` 供全流程共享）。
- 加工分两级：`parse_data_info` 加工「帧」（相对路径→绝对路径、标定提升、按 test_mode 决定生成 `ann_info`/`eval_ann_info`）；`parse_ann_info` 加工「实例」（instances 列表→等长 numpy 数组、`name_mapping` 改名、`label_mapping` 换标签）。
- 子类差异集中在两点：`parse_data_info` 的私有预处理（KITTI 的 plane、nuScenes 的多相机拆分、ScanNet 的 axis_align_matrix）与 `parse_ann_info` 的框包装（坐标系转换、空标注兜底、速度拼接）。
- `prepare_data` 是数据集与管线的交接面：deepcopy → 注入 `box_type_3d/box_mode_3d` → 管线前后两次空真值过滤 → `self.pipeline(input_dict)` 产出 `dict(inputs, data_samples)`。

## 7. 下一步学习建议

- **u3-l2 数据准备**：向前追溯 info pkl 的来源，读 `tools/create_data.py` 与 `kitti_converter.py`，理解 `update_infos_to_v2.py` 产出的标准结构是怎么从原始 KITTI 标注算出来的。
- **u3-l3 / u3-l4 室内外数据集**：横向铺开各子类 `parse_data_info`/`parse_ann_info` 的完整差异表，本讲只给了骨架。
- **u3-l5 加载类 Transforms**：向后进入管线内部，第一个就是消费 `lidar_path` 的 `LoadPointsFromFile`。
- **u8-l2 自定义数据集**：学完单元 3 后回头做扩展实践——只需实现 `METAINFO` 与 `parse_ann_info` 即可接入自有数据（官方模板见 [docs/en/advanced_guides/customize_dataset.md:211-251](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docs/en/advanced_guides/customize_dataset.md#L211-L251)），这正是本讲调用链设计的回报。
