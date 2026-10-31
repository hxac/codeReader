# 室外数据集：KITTI、nuScenes、Waymo、Lyft

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `KittiDataset`、`NuScenesDataset`、`WaymoDataset`、`LyftDataset` 四个类的继承关系，特别是 `WaymoDataset` 继承自 `KittiDataset` 而不是 `Det3DDataset` 的原因。
2. 对比四个类在 `parse_data_info` 上的差异：KITTI 独有的地面平面 `plane`、nuScenes 的一帧拆六相机、Waymo 的抽帧与相机同步标注、Lyft 的「零覆盖」。
3. 对比四个类在 `parse_ann_info` 上的三条路线：相机系标注逆变换（KITTI）、几何中心换锚点 + 速度拼接（nuScenes/Lyft）、直接包装雷达系数组（Waymo）。
4. 解释 nuScenes 的 sweep 聚合：`lidar_sweeps` 字段如何被 `LoadPointsFromMultiSweeps` 用 `lidar2sensor` 矩阵与时间差拼接成单帧点云。
5. 说清评估链路：数据集类本身不含评估代码，评估由配置里的 `val_evaluator`（`KittiMetric`/`NuScenesMetric`）经 `process → compute_metrics` 完成。

## 2. 前置知识

本讲建立在两讲之上，先快速回顾：

- **u3-l1（Det3DDataset 基类）**：数据集类管「读什么」，pipeline 管「怎么变」。基类的 `parse_data_info` 负责三件事——把相对路径拼成绝对路径、把默认相机的标定矩阵提升到顶层、按训练/测试模式生成 `ann_info` 或 `eval_ann_info`；`parse_ann_info` 把 `instances` 列表转成 `np.ndarray` 并经 `name_mapping` 改名（`bbox_3d → gt_bboxes_3d` 等）。**本讲只讲子类在这些骨架之上改了什么，不再重复基类细节。**
- **u3-l2（info pkl 生成）**：converter 离线读原始数据并算好派生量（如 nuScenes 的 `lidar2sensor`、每框 `num_lidar_pts`，KITTI 的 `plane`），数据集类在线只做轻量加工。本讲从「消费侧」呼应这一契约。
- **u2-l1/u2-l2（坐标系）**：`convert_to(dst, rt_mat)` 可以同时改写坐标、尺寸顺序与 yaw；KITTI 标注原本在相机坐标系（CAM：z 前 y 下），需要乘 `inv(lidar2cam)` 转回 LiDAR 坐标系（x 前 z 上）。

室外自动驾驶数据集相对室内数据集（下一讲 u3-l4）有几个共性，也是本讲反复出现的词汇：

| 特性 | 含义 |
|---|---|
| LiDAR 坐标系 | 真值框最终都要转成 `LiDARInstance3DBoxes`（x 前 y 左 z 上，yaw 绕 z 轴） |
| 多传感器标定 | info 中每个相机都带 `cam2img`、`lidar2cam`，`lidar2img = cam2img @ lidar2cam` |
| sweeps（多帧扫描） | 雷达 2Hz/10Hz 采样太稀，把前若干帧点云变换到当前帧拼接增密 |
| 速度（velocity） | nuScenes 标注带目标速度，框从 7 自由度扩到 9 自由度 |
| `with_yaw` | 室外框全部带朝向角，区别于室内的轴对齐框 |

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `mmdet3d/datasets/det3d_dataset.py` | 基类 `Det3DDataset`，`parse_data_info`/`parse_ann_info` 的默认实现（对照用） |
| `mmdet3d/datasets/kitti_dataset.py` | `KittiDataset`：plane 处理 + 相机系标注逆变换 |
| `mmdet3d/datasets/nuscenes_dataset.py` | `NuScenesDataset`：多相机拆分、速度、valid flag、sweeps |
| `mmdet3d/datasets/waymo_dataset.py` | `WaymoDataset`：继承 KITTI，抽帧 `load_interval` 与相机同步标注 |
| `mmdet3d/datasets/lyft_dataset.py` | `LyftDataset`：nuScenes 的「缩水版」，只改 `parse_ann_info` |
| `mmdet3d/datasets/transforms/loading.py` | `LoadPointsFromFile`（消费 `lidar_points`）与 `LoadPointsFromMultiSweeps`（消费 `points` + `lidar_sweeps`） |
| `configs/_base_/datasets/kitti-3d-3class.py`、`configs/_base_/datasets/nus-3d.py` | 两个数据集的标准配置：data_prefix、pipeline、val_evaluator |
| `mmdet3d/evaluation/metrics/kitti_metric.py`、`nuscenes_metric.py` | 评估指标类（本讲只看接口，u7-l2 详讲计算） |
| `tests/test_datasets/test_kitti_dataset.py`、`test_nuscenes_dataset.py` | 用仓库自带迷你数据实例化数据集的测试，是本讲实践的依据 |

## 4. 核心概念与源码讲解

### 4.1 四个数据集类的继承关系与构造差异

#### 4.1.1 概念说明

四个类不是平行的四份拷贝，而是一棵小继承树。理解这棵树的关键是：**Waymo 的 info 是由 `waymo_converter` 按 KITTI 风格生成的**（同样的 `images`/`lidar_points` 结构、同样把相机系标注存进 `bbox_3d`），所以 `WaymoDataset` 直接复用 `KittiDataset` 的加工逻辑，只补充 Waymo 特有的抽帧与相机同步标注。Lyft 则是「nuScenes 换个类别表」——它同样用 `(0.5, 0.5, 0.5)` 几何中心锚点，但没有速度标注。

#### 4.1.2 核心流程

```text
Det3DDataset (mmengine BaseDataset)
├── KittiDataset          # plane 处理；相机系 GT 逆变换到雷达系
│   └── WaymoDataset      # 抽帧 load_interval；cam_sync_instances；mv 拆分自带 lidar2img 计算
├── NuScenesDataset       # 速度 9 维框；valid flag；一帧拆 6 相机；sweeps
└── LyftDataset           # 只重写 parse_ann_info（换锚点），parse_data_info 用基类
```

类别体系对比（`METAINFO` 中的 `classes`）：

| 数据集 | 类别数 | 类别 |
|---|---|---|
| KITTI | 8 | Pedestrian / Cyclist / Car / Van / Truck / Person_sitting / Tram / Misc（常用 3 类子集） |
| nuScenes | 10 | car / truck / trailer / bus / construction_vehicle / bicycle / motorcycle / pedestrian / traffic_cone / barrier |
| Waymo | 3 | Car / Pedestrian / Cyclist |
| Lyft | 9 | car / truck / bus / emergency_vehicle / other_vehicle / motorcycle / bicycle / pedestrian / animal |

#### 4.1.3 源码精读

**类声明与继承关系**：KITTI、nuScenes、Lyft 都直接继承 `Det3DDataset`，唯独 Waymo 继承 `KittiDataset`。

- [mmdet3d/datasets/kitti_dataset.py:L11-L12](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/kitti_dataset.py#L11-L12)：`@DATASETS.register_module()` 装饰 + `class KittiDataset(Det3DDataset)`。
- [mmdet3d/datasets/waymo_dataset.py:L15-L16](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/waymo_dataset.py#L15-L16)：`class WaymoDataset(KittiDataset)`——本讲最重要的一个继承点。
- [mmdet3d/datasets/nuscenes_dataset.py:L13-L14](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/nuscenes_dataset.py#L13-L14)、[mmdet3d/datasets/lyft_dataset.py:L11-L12](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/lyft_dataset.py#L11-L12)：另外两个直接子类。

**类别与元信息**：

- [mmdet3d/datasets/kitti_dataset.py:L55-L60](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/kitti_dataset.py#L55-L60)：KITTI 的 `METAINFO`，含 8 类与可视化调色板。
- [mmdet3d/datasets/nuscenes_dataset.py:L58-L76](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/nuscenes_dataset.py#L58-L76)：nuScenes 的 `METAINFO` 多一个 `version: 'v1.0-trainval'`，后续 metric 用它加载官方评估配置。
- [mmdet3d/datasets/waymo_dataset.py:L73-L80](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/waymo_dataset.py#L73-L80)、[mmdet3d/datasets/lyft_dataset.py:L44-L51](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/lyft_dataset.py#L44-L51)：Waymo 3 类、Lyft 9 类。

**构造参数差异**：

- [mmdet3d/datasets/kitti_dataset.py:L62-L90](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/kitti_dataset.py#L62-L90)：KITTI 引入 `default_cam_key='CAM2'`（KITTI 的主相机）、`load_type`（`frame_based` / `mv_image_based` / `fov_image_based` 三种加载模式）和 `pcd_limit_range`（过滤越界预测框，默认 `[0, -40, -3, 70.4, 40, 0.0]`，即 KITTI 的雷达视场）。L90 断言 `box_type_3d` 只能是 LiDAR 或 Camera。
- [mmdet3d/datasets/nuscenes_dataset.py:L78-L110](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/nuscenes_dataset.py#L78-L110)：nuScenes 独有 `with_velocity=True`（是否把速度拼进框）与 `use_valid_flag=False`（是否用 converter 算好的 `bbox_3d_isvalid` 掩码，默认退化为 `num_lidar_pts > 0`）。
- [mmdet3d/datasets/waymo_dataset.py:L82-L126](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/waymo_dataset.py#L82-L126)：Waymo 独有 `load_interval`（Waymo 以 10Hz 采样、帧数巨大，训练时常隔帧取）、`cam_sync_instances`（用 v1.3.1 起的相机同步标注）、`max_sweeps`；L108-L109 还手工构造了 `cat_ids`/`cat2label` 供纯视觉标注解析使用。
- [mmdet3d/datasets/lyft_dataset.py:L53-L71](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/lyft_dataset.py#L53-L71)：Lyft 最简，L62 断言 `box_type_3d` 只能是 `'lidar'`。

#### 4.1.4 代码实践

**实践目标**：不加载任何真实数据，用 30 行代码打印四个类的继承链（MRO）与类别数，直观验证上面的继承树。

**操作步骤**（示例代码，可在仓库根目录用 `python -c` 或临时脚本执行）：

```python
# 示例代码：打印四个数据集类的继承链与元信息
from mmdet3d.datasets import (KittiDataset, NuScenesDataset,
                              WaymoDataset, LyftDataset)

for cls in (KittiDataset, NuScenesDataset, WaymoDataset, LyftDataset):
    mro = [c.__name__ for c in cls.__mro__[:4]]
    print(f'{cls.__name__:16s} MRO={mro}')
    print(f'{"":16s} classes={len(cls.METAINFO["classes"])}: '
          f'{cls.METAINFO["classes"][:3]} ...')
```

**需要观察的现象**：

1. `WaymoDataset` 的 MRO 是 `WaymoDataset → KittiDataset → Det3DDataset → BaseDataset`，其余三个是 `XxxDataset → Det3DDataset → BaseDataset`。
2. 四个类的类别数分别为 8 / 10 / 3 / 9。

**预期结果**：终端输出四行 MRO 与类别摘要；若 `import mmdet3d` 报版本断言错误，请先完成 u1-l2 的安装。本段脚本只读类属性、不读文件，必然可运行。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `WaymoDataset` 继承 `KittiDataset` 而不是直接继承 `Det3DDataset`？

**答案**：因为 `waymo_converter` 生成的 info 采用了 KITTI 风格的数据组织（`images` 字典带 `lidar2cam`、相机系 `bbox_3d` 标注），`KittiDataset.parse_ann_info` 中「相机系框逆变换到雷达系」的逻辑对 Waymo 同样适用，直接继承可以复用；Waymo 只需在此基础上补充抽帧（`load_interval`）、相机同步标注（`cam_sync_instances`）等差异。

**练习 2**：`NuScenesDataset` 的 `use_valid_flag=True` 与 `False` 分别用什么条件过滤标注？

**答案**：`True` 时用 info 中 converter 预计算的三维有效标志 `bbox_3d_isvalid`；`False` 时退化为 `num_lidar_pts > 0`（框内至少一个雷达点）。见 [mmdet3d/datasets/nuscenes_dataset.py:L112-L131](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/nuscenes_dataset.py#L112-L131) 的 `_filter_with_mask`。

### 4.2 parse_data_info 对比：同一帧 info 的四条加工路线

#### 4.2.1 概念说明

`parse_data_info` 的输入是 info pkl 中的一条原始记录，输出是交给 pipeline 的 data dict。基类版本（u3-l1 已讲）做三件事：LiDAR 路径拼接（含 `lidar_sweeps` 的 samples/sweeps 目录分流）、相机路径与标定矩阵提升、生成 `ann_info`/`eval_ann_info`。**由于基类是就地加工并返回原 dict，原始 info 自带的键（`token`、`timestamp`、`cam_instances` 等）都会原样保留在输出里**——这一点对理解「输出字段对比」很重要。

四个子类的差异可以概括为：KITTI 加一步 `plane` 坐标转换；nuScenes 在多视觉模式下把一帧拆成六条相机样本；Waymo 加抽帧与三种加载模式分流；Lyft 完全不覆盖。

#### 4.2.2 核心流程

```text
基类 parse_data_info（四者共用底座）
  ├─ use_lidar: lidar_points.lidar_path 拼绝对路径；lidar_sweeps 按 samples/sweeps 分流重写
  ├─ use_camera: 各相机 img_path 拼前缀；默认相机的 lidar2cam/cam2img/lidar2img 提升到顶层
  └─ 训练→ann_info / 测试且 load_eval_anns→eval_ann_info

KITTI 覆盖点：先把 info['plane'] 从相机系平面转成雷达系平面，再 super()
nuScenes 覆盖点：mv_image_based 时一帧拆 6 条相机样本（sample_idx*6+idx）；否则 super()
Waymo 覆盖点：cam_sync_instances 替换 instances；
             frame_based→super()（即 KITTI 链）；fov→只留默认相机；mv→自拆且自算 lidar2img
             另外覆盖 load_data_list 用 load_interval 抽帧
Lyft：无覆盖，全用基类
```

#### 4.2.3 源码精读

**KITTI：地面平面从相机系转到雷达系**。`plane` 是 KITTI 官方提供的 4 参数地面平面方程（相机坐标系下 \( n \cdot x + d = 0 \)），PointPillars 类算法不用它，但部分几何算法（如估计地面高度）需要。转换发生在 `super().parse_data_info()` 之前：

- [mmdet3d/datasets/kitti_dataset.py:L105-L126](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/kitti_dataset.py#L105-L126)：`use_lidar` 且 info 中有 `plane` 时，取 `plane[:3]` 为法向量 \( n \)、\( p_0 = -d \cdot n \) 为平面上一一点，用 `reverse = inv(lidar2cam)`（即 cam2lidar）把两者旋转平移到雷达系，最后重组回 4 参数 `plane_lidar`。若 info 无 `plane` 键则置 `None`（Waymo 的 info 就没有该键，走的就是这个分支）。
- [mmdet3d/datasets/kitti_dataset.py:L128-L133](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/kitti_dataset.py#L128-L133)：`fov_image_based` 且需要评估标注时，用默认相机 `CAM2` 的 `cam_instances` 覆盖 `instances`，再调基类。

平面变换的数学（行内公式演示）：设 \( [R|t] = \text{lidar2cam}^{-1} \)，则雷达系下 \( n' = R\,n \)、\( p_0' = R\,p_0 + t \)、\( d' = -n' \cdot p_0' \)。

**nuScenes：一帧拆六相机（多视觉模式）**：

- [mmdet3d/datasets/nuscenes_dataset.py:L206-L222](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/nuscenes_dataset.py#L206-L222)：`mv_image_based` 时先手工完成 LiDAR 与各相机路径拼接（因为不走基类了）。
- [mmdet3d/datasets/nuscenes_dataset.py:L224-L245](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/nuscenes_dataset.py#L224-L245)：对 `info['images']` 中的每个相机生成一条独立样本：只带该相机的 `images` 与 `cam_instances`，共享 `token`/`ego2global`，且 L234 把 `sample_idx` 重编码为 `sample_idx * 6 + idx` 保证六条样本索引唯一；随后各自调用 `parse_ann_info` 生成 `ann_info`/`eval_ann_info`。
- [mmdet3d/datasets/nuscenes_dataset.py:L246-L248](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/nuscenes_dataset.py#L246-L248)：默认的 `frame_based`（LiDAR 检测所用模式）直接走基类。

**Waymo：抽帧 + 三模式分流**：

- [mmdet3d/datasets/waymo_dataset.py:L183-L205](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/waymo_dataset.py#L183-L205)：覆盖 `load_data_list`，先按 `self.load_interval` 对原始 `data_list` 切片（L200 的 `raw_data_list[::self.load_interval]`），把 Waymo 海量 10Hz 帧降到可训练规模，其余逻辑照抄基类。这是四个类中唯一覆盖 `load_data_list` 的。
- [mmdet3d/datasets/waymo_dataset.py:L242-L254](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/waymo_dataset.py#L242-L254)：`cam_sync_instances=True` 时用相机同步标注替换 `instances`；`frame_based` 走 `super()`（沿 KITTI→基类链）；`fov_image_based` 只保留默认相机 `CAM_FRONT`。
- [mmdet3d/datasets/waymo_dataset.py:L256-L289](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/waymo_dataset.py#L256-L289)：`mv_image_based` 自行拆分，与 nuScenes 不同的是它在 L276-L278 自己补算 `lidar2img = cam2img @ lidar2cam`（info 未提供时），并保留 `timestamp`/`context_name` 等 Waymo 特有键。

**Lyft：零覆盖**。`LyftDataset` 全文没有 `parse_data_info`，路径加工与 `ann_info` 生成全部由基类完成，见 [mmdet3d/datasets/lyft_dataset.py:L73-L105](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/lyft_dataset.py#L73-L105)（该文件只重写了 `parse_ann_info`）。

**基类中与本讲强相关的一段——sweeps 路径分流**：

- [mmdet3d/datasets/det3d_dataset.py:L286-L295](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/det3d_dataset.py#L286-L295)：若 info 带 `lidar_sweeps`，按路径中是否含 `samples` 把每帧 sweep 的点云文件分别重写到 `data_prefix['pts']`（关键帧目录）或 `data_prefix['sweeps']`（sweep 目录）。nuScenes 配置 `configs/_base_/datasets/nus-3d.py` L18 正是据此声明了 `data_prefix = dict(pts='samples/LIDAR_TOP', sweeps='sweeps/LIDAR_TOP')`。

#### 4.2.4 代码实践

**实践目标**：用仓库自带的迷你数据（`tests/data/`）真实实例化 `KittiDataset` 与 `NuScenesDataset`，打印 `get_data_info(0)` 输出字典的键，完成本讲开头的字段对比任务。

**操作步骤**（示例代码；无需下载任何公开数据集，`tests/data/kitti/kitti_infos_train.pkl` 与 `tests/data/nuscenes/nus_info.pkl` 已在仓库中）：

```python
# 示例代码：对比 KITTI 与 nuScenes 的 parse_data_info 输出
from mmdet3d.datasets import KittiDataset, NuScenesDataset

kitti = KittiDataset(
    data_root='tests/data/kitti',
    ann_file='kitti_infos_train.pkl',
    data_prefix=dict(pts='training/velodyne_reduced', img='training/image_2'),
    pipeline=[],                      # 空 pipeline：只看 parse 阶段的输出
    metainfo=dict(classes=['Pedestrian', 'Cyclist', 'Car']),
    modality=dict(use_lidar=True, use_camera=True),
    test_mode=False)                  # 训练模式 → 生成 ann_info

info_k = kitti.get_data_info(0)
print('KITTI keys:', sorted(info_k.keys()))

nus = NuScenesDataset(
    data_root='tests/data/nuscenes',
    ann_file='nus_info.pkl',
    data_prefix=dict(pts='samples/LIDAR_TOP', sweeps='sweeps/LIDAR_TOP'),
    pipeline=[],
    metainfo=dict(classes=['car', 'pedestrian', 'traffic_cone']),  # 取子集即可
    modality=dict(use_lidar=True, use_camera=False),
    test_mode=False)

info_n = nus.get_data_info(0)
print('nuScenes keys:', sorted(info_n.keys()))
print('sweeps 数量:', len(info_n.get('lidar_sweeps', [])))
print('gt_bboxes_3d 类型:', type(info_n['ann_info']['gt_bboxes_3d']))
print('gt_bboxes_3d 形状:', info_n['ann_info']['gt_bboxes_3d'].tensor.shape)
```

**需要观察的现象**：

1. 两者共有的键：`lidar_points`、`lidar_path`、`num_pts_feats`、`images`、`sample_idx`、`instances`、`ann_info` 等（来自基类的加工 + 原始 info 的保留键）。
2. KITTI 独有：`plane`；nuScenes 独有：`lidar_sweeps`、`timestamp`、`token`、`ego2global`、`cam_instances`。
3. nuScenes 的 `ann_info['gt_bboxes_3d']` 是 `LiDARInstance3DBoxes` 对象且 `tensor.shape` 最后一维是 **9**（7 自由度 + 2 速度），KITTI 是 **7**。

**预期结果**：打印两行键列表与 sweeps 数量（迷你数据中通常为 2 帧 sweep）、9 维框形状。若你的环境缺少依赖无法运行，可改跑 `pytest tests/test_datasets/test_kitti_dataset.py tests/test_datasets/test_nuscenes_dataset.py` 观察同样的数据链路（待本地验证具体键列表，因迷你 pkl 内容而异）。

#### 4.2.5 小练习与答案

**练习 1**：`mv_image_based` 模式下 nuScenes 一帧产生几条样本？`sample_idx` 如何避免冲突？

**答案**：六条（六相机各一条），`sample_idx` 被重编码为 `原 sample_idx * 6 + 相机序号`，见 [mmdet3d/datasets/nuscenes_dataset.py:L224-L245](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/nuscenes_dataset.py#L224-L245)。

**练习 2**：为什么 Waymo 要覆盖 `load_data_list` 而 KITTI 不需要？

**答案**：Waymo 以 10Hz 连续采集、训练集几十万帧，直接全量加载会拖慢训练且相邻帧高度冗余，`load_data_list` 用 `load_interval` 隔帧抽取（[mmdet3d/datasets/waymo_dataset.py:L200](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/waymo_dataset.py#L200)）；KITTI 帧数少得多，无需抽帧。

**练习 3**：`lidar_sweeps` 中某帧 sweep 的路径含 `samples` 与不含时，分别被重写到哪个目录？

**答案**：含 `samples` 时重写到 `data_prefix['pts']`（关键帧目录），否则重写到 `data_prefix['sweeps']`，见 [mmdet3d/datasets/det3d_dataset.py:L286-L295](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/det3d_dataset.py#L286-L295)。

### 4.3 parse_ann_info 对比：裸数组到 3D 框与 nuScenes 的速度/sweep 聚合

#### 4.3.1 概念说明

基类 `parse_ann_info` 把 `instances` 列表按字段拼成 `np.ndarray` 并改名（`bbox_3d → gt_bboxes_3d`），但**不做坐标系转换与框对象包装**——这两件事留给子类。四个子类走了三条路线：

1. **KITTI 路线（相机系标注逆变换）**：KITTI 原始标注在相机坐标系，先把裸数组包成 `CameraInstance3DBoxes`，再 `convert_to(LiDAR, inv(lidar2cam))` 转成雷达系框。
2. **nuScenes/Lyft 路线（换锚点）**：官方工具箱给的框中心是**几何中心** `(0.5, 0.5, 0.5)`，mmdet3d 统一用**底面中心** `(0.5, 0.5, 0)`，构造时用 `origin` 参数显式声明再转换；nuScenes 还要先过滤无效框、拼接速度。
3. **Waymo 路线（直接包装）**：converter 产出的 `bbox_3d` 已是雷达系底面中心格式，`frame_based` 下直接 `LiDARInstance3DBoxes(...)` 包装。

另外本模块讲清 sweep 聚合：`lidar_sweeps` 是数据集侧准备好的字段，真正的多帧拼接发生在 pipeline 的 `LoadPointsFromMultiSweeps` 里。

#### 4.3.2 核心流程

```text
parse_ann_info（以 frame_based + use_lidar 为例）
KITTI:  super()→去 DontCare→CameraInstance3DBoxes(bbox_3d)
        →convert_to(LiDAR, inv(lidar2cam))          # 相机系→雷达系
nuScenes: super()→_filter_with_mask(num_lidar_pts>0 或 bbox_3d_isvalid)
        → with_velocity 时 NaN 速度置 0 并拼接第 8/9 维
        → LiDARInstance3DBoxes(..., origin=(0.5,0.5,0.5)).convert_to(box_mode_3d)
Waymo:  Det3DDataset.parse_ann_info（跳过 KITTI 版本！）
        → frame_based: LiDARInstance3DBoxes(直接包装)
Lyft:   super()→LiDARInstance3DBoxes(..., origin=(0.5,0.5,0.5)).convert_to

sweep 聚合（pipeline 内，LoadPointsFromMultiSweeps）
当前帧点云 time-lag 列置 0 → 按 sweeps_num 采样（训练随机/测试取最近）
→ 每个 sweep: p_cur = R·p_sweep - t （R|t = sweep 的 lidar2sensor）
→ time-lag 列 = 当前帧时间戳 - sweep 时间戳
→ cat 拼接 → 按 use_dim=[0,1,2,4] 切片（丢强度、留时差）
```

#### 4.3.3 源码精读

**KITTI：相机系 → 雷达系**：

- [mmdet3d/datasets/kitti_dataset.py:L152-L165](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/kitti_dataset.py#L152-L165)：先调基类拿裸数组版 `ann_info`（空标注时手工补零数组），再做 `_remove_dontcare`（KITTI 的 `DontCare` 是占位类，需从训练标注中剔除）。
- [mmdet3d/datasets/kitti_dataset.py:L166-L172](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/kitti_dataset.py#L166-L172)：注释写明 `lidar2cam = R0_rect @ Tr_velo_to_cam`（KITTI 标定的两步复合），随后 `CameraInstance3DBoxes(gt_bboxes_3d).convert_to(self.box_mode_3d, np.linalg.inv(lidar2cam))` 一步完成坐标系、尺寸顺序（y/z 互换）与 yaw 符号的改写（正是 u2-l1 讲过的 `convert_to` 能力）。`difficulty` 等 2D 字段原样保留在 `ann_info` 中。

**nuScenes：过滤、速度、换锚点**：

- [mmdet3d/datasets/nuscenes_dataset.py:L146-L158](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/nuscenes_dataset.py#L146-L158)：先 `_filter_with_mask` 过滤无雷达点/无效的框；`with_velocity=True` 时把 `velocities` 拼接到 7 维框后（`np.concatenate(..., axis=-1)`）成为 9 维，其中速度为 NaN 的（雷达点太少、无法估计）整行置 `[0.0, 0.0]`。
- [mmdet3d/datasets/nuscenes_dataset.py:L160-L173](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/nuscenes_dataset.py#L160-L173)：空标注分支，按 `with_velocity` 补 `(0, 9)` 或 `(0, 7)` 的零框；`mv/fov` 模式还要补 2D 框、`centers_2d`、`depths`、`attr_labels`（相机检测需要）。
- [mmdet3d/datasets/nuscenes_dataset.py:L175-L189](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/nuscenes_dataset.py#L175-L189)：L175-L177 注释点明锚点问题——nuScenes 原生中心是 `(0.5, 0.5, 0.5)`（几何中心），这里构造 `LiDARInstance3DBoxes(..., origin=(0.5,0.5,0.5))` 让容器先按几何中心解读、内部换算到底面中心，再 `convert_to(self.box_mode_3d)`。

**Waymo：跳级调用 + 按模式包装**：

- [mmdet3d/datasets/waymo_dataset.py:L145](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/waymo_dataset.py#L145)：`ann_info = Det3DDataset.parse_ann_info(self, info)`——显式调「爷爷类」，跳过 KITTI 版本里的相机系逆变换，因为 Waymo info 的 `bbox_3d` 已是雷达系坐标。
- [mmdet3d/datasets/waymo_dataset.py:L168-L179](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/waymo_dataset.py#L168-L179)：`frame_based` 直接 `LiDARInstance3DBoxes(ann_info['gt_bboxes_3d'])`（默认底面中心锚点，无需 origin 参数）；否则包成 `CameraInstance3DBoxes`；最后统一组装含 2D 框/`centers_2d`/`depths` 的返回字典。

**Lyft：nuScenes 的锚点处理、无速度**：

- [mmdet3d/datasets/lyft_dataset.py:L86-L105](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/lyft_dataset.py#L86-L105)：空标注补 `(0, 7)` 零框；非空时同样以 `origin=(0.5, 0.5, 0.5)` 构造再 `convert_to`，注释与 nuScenes 一致——Lyft 的 converter 同样源自 nuScenes 工具链。

**sweep 聚合：`LoadPointsFromMultiSweeps`**（类声明 [mmdet3d/datasets/transforms/loading.py:L314-L332](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/loading.py#L314-L332)，docstring 明言「通常用于 nuScenes 利用历史 sweeps」）：

- [mmdet3d/datasets/transforms/loading.py:L412-L423](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/loading.py#L412-L423)：入口先取上一 transform 产出的 `results['points']`（关键帧点云），把其第 4 列（时间差）置 0——当前帧自己的 lag 为 0；若 data dict 没有 `lidar_sweeps` 键且 `pad_empty_sweeps=True`，就用当前帧点云重复填充。
- [mmdet3d/datasets/transforms/loading.py:L424-L432](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/loading.py#L424-L432)：采样策略三分支——sweep 总数不足 `sweeps_num` 全取；`test_mode=True` 取**最近**的 `sweeps_num` 帧（确定性）；训练时 `np.random.choice(..., replace=False)` 随机抽取。
- [mmdet3d/datasets/transforms/loading.py:L433-L448](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/loading.py#L433-L448)：逐帧读点、`reshape(-1, load_dim)`、可选 `_remove_close`（剔除离传感器 1m 内的噪声点），然后做刚体变换与时间戳标注：

  \[ p_{cur} = R_{ls}\, p_{sweep} - t_{ls}, \qquad \Delta t = t_{cur} - t_{sweep} \]

  其中 \( [R_{ls} | t_{ls}] \) 是 info 中该 sweep 的 `lidar2sensor`（converter 离线算好的「sweep 帧雷达到当前关键帧雷达」变换）；注意代码写的是减法（`points[:, :3] -= lidar2sensor[:3, 3]`）。变换后用 `points.new_point(...)` 包装成与前帧同类的点对象再入列表。
- [mmdet3d/datasets/transforms/loading.py:L450-L452](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/loading.py#L450-L452)：`points.cat(...)` 拼接全部帧，再按本类的 `use_dim`（默认 `[0, 1, 2, 4]`）切片——**丢掉强度列、保留时间差列**。所以在 nus-3d 配置中，`LoadPointsFromFile` 先产出 5 维点（x, y, z, intensity, timestamp），经本 transform 后模型实际收到的是 4 维（x, y, z, Δt），这正是 CenterPoint 等模型的输入约定。

**与 `LoadPointsFromFile` 的分工**（类声明 [mmdet3d/datasets/transforms/loading.py:L553-L565](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/loading.py#L553-L565)）：

- [mmdet3d/datasets/transforms/loading.py:L645-L681](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/loading.py#L645-L681)：`LoadPointsFromFile` 消费 `results['lidar_points']['lidar_path']`（**单帧关键帧**点云），`reshape(-1, load_dim)` 后按 `use_dim` 切片、可选 `tanh` 归一化强度/elongation（Waymo 常用）、`get_points_type(coord_type)` 包装成 `LiDARPoints` 等点对象，写入 `results['points']`。
- 即：**`points` 字段由 `LoadPointsFromFile` 生产、由 `LoadPointsFromMultiSweeps` 消费并更新；`lidar_sweeps` 字段由 `LoadPointsFromMultiSweeps` 消费**。二者在 nus-3d 配置 [configs/_base_/datasets/nus-3d.py:L35-L45](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/datasets/nus-3d.py#L35-L45) 中总是紧挨着出现。

#### 4.3.4 代码实践

**实践目标**：手工复现 sweep 变换公式，验证你对 `lidar2sensor` 与时间差的理解。

**操作步骤**（示例代码，纯 numpy，不依赖数据集）：

```python
# 示例代码：手工复现 LoadPointsFromMultiSweeps 的单帧 sweep 变换
import numpy as np

# 假设某 sweep 帧有 3 个点（load_dim=5: x,y,z,intensity,ts）
points_sweep = np.array([
    [1.0,  2.0, 0.5, 0.7, 0.0],
    [3.0, -1.0, 0.2, 0.4, 0.0],
    [0.0,  5.0, 1.0, 0.9, 0.0]], dtype=np.float32)

# converter 预计算的 lidar2sensor：sweep 雷达 → 当前关键帧雷达
lidar2sensor = np.eye(4, dtype=np.float32)
lidar2sensor[:3, :3] = np.array([[0, -1, 0], [1, 0, 0], [0, 0, 1]], dtype=np.float32)  # 绕 z 转 90°
lidar2sensor[:3, 3] = np.array([0.5, 0.0, 0.0], dtype=np.float32)

ts, sweep_ts = 1533201470.948, 1533201470.848   # 当前帧与 sweep 的时间戳（已除 1e6）
p = points_sweep.copy()
p[:, :3] = p[:, :3] @ lidar2sensor[:3, :3]   # 对应源码 L444
p[:, :3] -= lidar2sensor[:3, 3]              # 对应源码 L445
p[:, 4] = ts - sweep_ts                      # 对应源码 L446
print(p)
```

**需要观察的现象**：前三列被旋转 90° 并平移（原 `(1, 2)` 方向的点变到 `(-2, 1.5)` 附近），第 5 列时间差为 `0.1`（秒）。

**预期结果**：打印出旋转平移后的 3 个点与 0.1s 时差；与 [mmdet3d/datasets/transforms/loading.py:L443-L446](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/loading.py#L443-L446) 逐行对应。

#### 4.3.5 小练习与答案

**练习 1**：KITTI 与 Waymo 的 `parse_ann_info` 都涉及 `lidar2cam`，用法有何本质区别？

**答案**：KITTI 用 `inv(lidar2cam)` 把**相机系标注**变换到雷达系（[kitti_dataset.py:L169-L172](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/kitti_dataset.py#L169-L172)）；Waymo 的 `parse_ann_info` 根本不用 `lidar2cam` 做框转换，它显式调用 `Det3DDataset.parse_ann_info` 跳过 KITTI 逻辑，直接包装已是雷达系的数组（[waymo_dataset.py:L145](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/waymo_dataset.py#L145)、L168-L171）。Waymo 配置里的 `lidar2cam` 主要服务相机/多模态任务。

**练习 2**：nuScenes 的 `gt_bboxes_3d` 为什么可能是 9 维而不是 7 维？多出的两维在训练中如何处理 NaN？

**答案**：`with_velocity=True` 时把 `velocities`（vx, vy）拼接到 7 自由度之后成 9 维；速度为 NaN 的样本（雷达点不足）在拼接前整行置 `[0.0, 0.0]`，见 [nuscenes_dataset.py:L151-L158](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/nuscenes_dataset.py#L151-L158)。

**练习 3**：`nus-3d.py` 中 `LoadPointsFromFile(use_dim=5)` 之后模型为什么拿到的是 4 维点？

**答案**：后随的 `LoadPointsFromMultiSweeps` 默认 `use_dim=[0, 1, 2, 4]`，在 `points.cat` 之后重新切片，丢弃 intensity（第 3 列）、保留时间差（第 5 列），见 [loading.py:L450-L452](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/loading.py#L450-L452)。

### 4.4 evaluate 如何与 Metric 类配合

#### 4.4.1 概念说明

一个常见误解是「每个数据集类自己算 mAP」。实际上四个室外数据集类**都没有定义 `evaluate` 方法**——评估完全走 MMEngine 的通用机制：配置文件里的 `val_evaluator`/`test_evaluator` 构建出 Metric 对象（继承 `mmengine.BaseMetric`），测试循环把每批 `data_samples` 喂给 `metric.process` 收集，全部跑完后 `metric.compute_metrics` 汇总。数据集类的职责仅是：产出带 `sample_idx` 的样本、以及在 `test_mode=True` 且 `load_eval_anns=True`（默认开）时随 info 附带 `eval_ann_info`。

各数据集对应关系：

| 数据集类 | Metric | 官方指标 |
|---|---|---|
| `KittiDataset` | `KittiMetric` | AP / AP_R40（按难度 Easy/Moderate/Hard） |
| `NuScenesDataset` | `NuScenesMetric` | mAP、NDS、mATE/mASE 等误差项 |
| `WaymoDataset` | `WaymoMetric` | AP / APH（按难度 LEVEL_1/2） |
| `LyftDataset` | `LyftMetric` | 官方 Kaggle 评分 |

指标计算细节属于 u7-l2 的内容，本讲只看「谁在什么时候被调用」。

#### 4.4.2 核心流程

```text
tools/test.py（u1-l4 讲过的五步）
  → Runner 按 val_evaluator/test_evaluator 配置构建 Evaluator（内含 Metric 列表）
  → 测试循环: model.test_step(batch) 产出 data_samples（含 pred_instances_3d、sample_idx）
  → evaluator.process → metric.process: 逐样本把预测搬到 CPU 存入 self.results
  → 全部结束: evaluator.evaluate → metric.compute_metrics(results)
      ├─ KittiMetric:  转成 KITTI 官方格式 → 官方脚本算 AP_R40
      └─ NuScenesMetric: format_results 转 nuScenes json → 官方 devkit 算 mAP/NDS
GT 来源：Metric 构造时传入的 ann_file（自己重读 info pkl），以 sample_idx 对齐预测
```

#### 4.4.3 源码精读

**数据集侧：`eval_ann_info` 的生成条件**：

- [mmdet3d/datasets/det3d_dataset.py:L60-L62](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/det3d_dataset.py#L60-L62)：`load_eval_anns` 的文档——测试模式下是否加载标注到 `eval_ann_info`，默认 `True`（L81 的默认值）。
- [mmdet3d/datasets/det3d_dataset.py:L321-L325](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/det3d_dataset.py#L321-L325)：训练模式生成 `info['ann_info']`；测试模式且 `load_eval_anns` 时生成 `info['eval_ann_info']`。这是 `parse_data_info` 的收尾两行，四个子类共用。

**配置侧：评估器挂接**：

- [configs/_base_/datasets/kitti-3d-3class.py:L158-L163](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/datasets/kitti-3d-3class.py#L158-L163)：`val_evaluator = dict(type='KittiMetric', ann_file=data_root + 'kitti_infos_val.pkl', ...)`，`test_evaluator` 直接复用 `val_evaluator`。
- [configs/_base_/datasets/nus-3d.py:L159-L165](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/datasets/nus-3d.py#L159-L165)：`val_evaluator = dict(type='NuScenesMetric', data_root=..., ann_file=..., metric='bbox')`。

**Metric 侧：两个关键方法**：

- [mmdet3d/evaluation/metrics/nuscenes_metric.py:L24](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/metrics/nuscenes_metric.py#L24)：`class NuScenesMetric(BaseMetric)`；[L89-L123](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/metrics/nuscenes_metric.py#L89-L123) 构造函数收 `ann_file`（L91 声明、L108 存下）——GT 由 metric 自己重读 info pkl，而非从 data_samples 里拿。
- [mmdet3d/evaluation/metrics/nuscenes_metric.py:L125-L147](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/metrics/nuscenes_metric.py#L125-L147)：`process` 逐样本把 `pred_instances_3d`/`pred_instances` 的张量搬到 CPU，连同 `sample_idx` 存入 `self.results`——这就是「收集」阶段。
- [mmdet3d/evaluation/metrics/nuscenes_metric.py:L149-L170](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/metrics/nuscenes_metric.py#L149-L170)：`compute_metrics` 先经 `format_results`（L267 起）把结果转成 nuScenes 官方 json，再交给官方 devkit 计算 mAP/NDS（细节 u7-l2 展开）。
- [mmdet3d/evaluation/metrics/kitti_metric.py:L20](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/metrics/kitti_metric.py#L20)：`class KittiMetric(BaseMetric)`，[L145](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/metrics/kitti_metric.py#L145) `process`、[L170](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/metrics/kitti_metric.py#L170) `compute_metrics`，模式与 NuScenes 完全同构。

值得注意的分工：`eval_ann_info` 随 data_samples 流动，主要供可视化等场景取用；`KittiMetric`/`NuScenesMetric` 的 GT 则来自构造参数 `ann_file`，以 `sample_idx` 与预测对齐。

#### 4.4.4 代码实践

**实践目标**：跑通仓库自带的数据集测试，确认 `parse_data_info → parse_ann_info → pipeline` 链路在真实类上可执行。

**操作步骤**：

```bash
# 在仓库根目录（环境已按 u1-l2 装好）
pytest tests/test_datasets/test_kitti_dataset.py -x -q
pytest tests/test_datasets/test_nuscenes_dataset.py -x -q
```

**需要观察的现象**：两个测试各自用 `tests/data/` 下的迷你 pkl 实例化数据集，断言输出路径包含 `data_prefix` 与 `data_root`、`ann_info` 的键与类型（如 `gt_labels_3d` 为 `int64`）。

**预期结果**：全部通过。随后打开 [tests/test_datasets/test_kitti_dataset.py:L60-L81](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tests/test_datasets/test_kitti_dataset.py#L60-L81)，对照断言逐条写下「这个断言验证了 parse_data_info/parse_ann_info 的哪个输出字段」。待本地验证（取决于环境）。

#### 4.4.5 小练习与答案

**练习 1**：在 mmdet3d 1.x 中，`KittiDataset` 类里为什么搜不到 `evaluate` 方法？评估到底由谁触发？

**答案**：评估逻辑不在数据集类中，而由配置的 `val_evaluator`（`KittiMetric` 等 `BaseMetric` 子类）承担；测试循环调 `metric.process` 收集预测、结束时调 `compute_metrics` 汇总，入口是 `tools/test.py` 构建的 Runner。

**练习 2**：`eval_ann_info` 在什么条件下生成？`NuScenesMetric` 靠它拿 GT 吗？

**答案**：`test_mode=True` 且 `load_eval_anns=True`（默认）时由 `parse_data_info` 生成（[det3d_dataset.py:L324-L325](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/det3d_dataset.py#L324-L325)）；`NuScenesMetric` 不靠它，其 GT 来自构造参数 `ann_file` 重读的 info pkl（[nuscenes_metric.py:L108](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/evaluation/metrics/nuscenes_metric.py#L108)）。

## 5. 综合实践

把本讲规格中的对比任务完整做一遍——**产出一张「KITTI vs nuScenes 的 parse_data_info 输出字段对照表」并回答 sweeps/points 的消费者问题**。

**步骤**：

1. 运行 4.2.4 的示例脚本，把 `info_k` 与 `info_n` 的键分别抄下来，填入下表（参考答案已给出，请以你的实际输出校正「示例值」列）：

| 字段 | KITTI | nuScenes | 生产者 / 说明 |
|---|---|---|---|
| `lidar_points`（含绝对 `lidar_path`） | ✓ | ✓ | 基类拼前缀 |
| `lidar_path`、`num_pts_feats` | ✓ | ✓ | 基类顶层快捷键 |
| `images`（各相机路径+标定） | ✓ | ✓（use_camera 时加工） | 基类；原始 info 自带 |
| `lidar2cam`/`cam2img`/`lidar2img` | ✓（use_camera） | ✓（use_camera） | 基类提升默认相机标定 |
| `ann_info`（训练）或 `eval_ann_info`（测试） | ✓ | ✓ | 基类收尾两行 |
| `sample_idx` | ✓ | ✓ | 原始 info 保留 |
| `plane`（雷达系地面平面） | ✓ **独有** | ✗ | KITTI 覆盖点（4.2.3） |
| `lidar_sweeps` | ✗ | ✓ **独有** | 原始 info 保留，路径被基类重写 |
| `timestamp` | ✗ | ✓ | `LoadPointsFromMultiSweeps` 需要它算时差 |
| `token`、`ego2global` | ✗ | ✓ | nuScenes 元信息 |
| `cam_instances` | ✓（mono3d info 有） | ✓ | 多视觉/FOV 模式的标注源 |
| `instances` | ✓ | ✓ | 原始实例列表，`parse_ann_info` 的输入 |

2. 回答消费者问题（对照 4.3.3 源码）：
   - `lidar_points['lidar_path']`（当前帧点云）由 **`LoadPointsFromFile`** 消费，产出 `results['points']`（[loading.py:L645](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/loading.py#L645)）。
   - `lidar_sweeps`（历史帧列表）由 **`LoadPointsFromMultiSweeps`** 消费：它读取已生成的 `results['points']`，把各 sweep 用 `lidar2sensor` 变换到当前帧、打上时间差后拼接，再写回 `results['points']`（[loading.py:L412-L452](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/loading.py#L412-L452)）。
3. 补充一行验证：在 4.2.4 脚本里加 `print(info_n['ann_info']['gt_bboxes_3d'].tensor.shape[-1])`，确认 nuScenes 为 9（含速度）、KITTI 为 7。
4. 进阶（可选）：把 `LoadPointsFromMultiSweeps` 换成 `dict(type='LoadPointsFromMultiSweeps', sweeps_num=1)` 观察 sweeps 采样数量变化（需真实 nuScenes 数据，待本地验证）。

## 6. 本讲小结

- 四个室外数据集类是一棵继承树：`WaymoDataset → KittiDataset → Det3DDataset`，nuScenes 与 Lyft 直接继承 `Det3DDataset`；Lyft 甚至不覆盖 `parse_data_info`。
- `parse_data_info` 的子类差异集中在：KITTI 的 `plane` 相机系→雷达系转换、nuScenes 的一帧拆六相机（`sample_idx*6+idx`）、Waymo 的 `load_interval` 抽帧与 `cam_sync_instances`；基类负责路径拼接、sweeps 目录分流、标定提升与 `ann_info`/`eval_ann_info` 收尾。
- `parse_ann_info` 有三条路线：KITTI 用 `inv(lidar2cam)` 把相机系标注转到雷达系；nuScenes/Lyft 用 `origin=(0.5,0.5,0.5)` 换锚点（nuScenes 还拼 2 维速度成 9 维框并过滤无点框）；Waymo 跳级调用基类、直接包装雷达系数组。
- sweep 聚合发生在 pipeline 而非数据集类：`LoadPointsFromFile` 产 `points`，`LoadPointsFromMultiSweeps` 消费 `points + lidar_sweeps`，用 `lidar2sensor` 与时间差拼接，并按 `use_dim=[0,1,2,4]` 把 5 维点切成 4 维（x, y, z, Δt）。
- 评估不在数据集类里：`val_evaluator` 配置的 `KittiMetric`/`NuScenesMetric`（`BaseMetric` 子类）经 `process` 收集预测、`compute_metrics` 汇总，GT 来自 metric 自己的 `ann_file`；数据集侧只在测试模式下附带 `eval_ann_info`。

## 7. 下一步学习建议

- **u3-l4（室内数据集：ScanNet 与 SUN RGB-D）**：看「无 yaw 的轴对齐框 + Depth 坐标系 + 逐点实例分割标注」如何改写同一套基类骨架，与本讲的旋转框路线对照。
- **u3-l5（数据加载 Transforms）**：本讲只精读了 `LoadPointsFromFile`/`LoadPointsFromMultiSweeps` 两个 Load 类，下一讲系统过 `LoadAnnotations3D`、`LoadMultiViewImageFromFiles` 等一族。
- 自选阅读：`tests/test_datasets/test_waymo_dataset.py` 与 `test_lyft_dataset.py`，以及 `mmdet3d/evaluation/metrics/waymo_metric.py`，验证本讲的继承树与评估链路在另外两个数据集上如何复现。
