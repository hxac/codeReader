# 扩展实践：接入自定义数据集

## 1. 本讲目标

学完本讲，你应该能够：

1. 在官方给出的三种「自有数据接入路线」中做出正确选型，并说清每条路线的工作量边界。
2. 独立实现一个最小自定义数据集类 `MyDataset`：正确声明 `METAINFO`、重写 `parse_ann_info`，把裸数组包装成 `LiDARInstance3DBoxes`。
3. 独立实现一个数据 converter，把自己格式的标注转成 v2 标准格式的 info pkl——并且知道官方文档中 `python tools/create_data.py custom` 命令在当前代码里**并不存在**这一重要事实。
4. 为自定义数据集挂接一份可训练的 PointPillars 配置，并用 `browse_dataset.py` 在训练前完成自检。
5. 解释 `CBGSDataset` 包装器解决什么问题、如何用配置启用它。

## 2. 前置知识

本讲是单元 3 数据集知识的「反向应用」：前面是读懂官方数据集怎么写，现在是自己动手写一个。开始前请确认理解以下来自前置讲义的认知：

- **契约边界**（u3-l1、u3-l2）：converter 在离线阶段把原始数据算成 info pkl，数据集类在线只做轻量加工。pkl 顶层是 `dict(metainfo=..., data_list=...)`，单条 info 分 `images`、`lidar_points`、`instances` 三段式。
- **两段解析**（u3-l1）：`parse_data_info` 加工「帧」（拼绝对路径、生成 ann_info），`parse_ann_info` 加工「实例」（instances 列表 → numpy 数组 → 框对象）。
- **底面中心锚点**（u2-l2）：`BaseInstance3DBoxes` 构造默认 `origin=(0.5, 0.5, 0)`，即 (x, y) 取框几何中心、z 取框**底面**。自己写标签时 z 填底面高度（约等于地面高程）。
- **注册器路由**（u4-l1）：配置里 `type='MyDataset'` 能找到类，前提是类被 `@DATASETS.register_module()` 注册、且所在模块被 import 过。
- **坐标系**（u2-l1）：本讲采用文档推荐的「统一 3D 坐标系」即 LiDAR 坐标系存 3D 框，避开 KITTI 那套「相机系标注 + 逆变换」的复杂度。

一个本讲反复出现的思维模型：**info pkl 是 converter 与数据集类之间的合同**。converter 多算一点（绝对框位、点数、标定），数据集类就轻松一点；反之你在 converter 里偷懒，就要在 `parse_ann_info` 里补课。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [docs/en/advanced_guides/customize_dataset.md](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docs/en/advanced_guides/customize_dataset.md) | 官方自定义数据集教程：数据格式约定、MyDataset 示例、config 示例 |
| [docs/en/user_guides/new_data_model.md](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docs/en/user_guides/new_data_model.md) | 官方新数据集接入总纲：三种路线与 Waymo 案例 |
| [mmdet3d/datasets/det3d_dataset.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/det3d_dataset.py) | 检测数据集基类：`parse_ann_info`/`parse_data_info` 默认实现 |
| [tools/create_data.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/create_data.py) | 数据准备总调度台（注意：没有 custom 分支） |
| [tools/dataset_converters/update_infos_to_v2.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/dataset_converters/update_infos_to_v2.py) | v2 标准格式的字段定义与各数据集的升级实现，是写 converter 的最佳参考 |
| [mmdet3d/datasets/dataset_wrappers.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/dataset_wrappers.py) | `CBGSDataset` 类平衡采样包装器 |
| [mmdet3d/registry.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/registry.py) | `DATASETS` 注册节点定义 |
| [mmdet3d/datasets/kitti_dataset.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/kitti_dataset.py) | 对照组：官方数据集类的 `parse_ann_info` 长什么样 |

## 4. 核心概念与源码讲解

### 4.1 三种接入路线与选型

#### 4.1.1 概念说明

官方文档 [new_data_model.md:13-19](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docs/en/user_guides/new_data_model.md#L13-L19) 明确给出支持新数据集的三种方式：

1. **重组织为既有格式**：把自有数据转成 KITTI（或 nuScenes 等）的目录与标注格式，然后直接用官方 `KittiDataset` 类。Waymo 就是这条路线的官方示范——先用 [waymo_converter](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/dataset_converters/waymo_converter.py) 把 tfrecord 转成 KITTI 格式，再让 `WaymoDataset` 继承 `KittiDataset`（见 [new_data_model.md:71-77](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docs/en/user_guides/new_data_model.md#L71-L77)）。
2. **重组织为标准格式**：转成 v2 标准格式的 info pkl，配一个很薄的自定义数据集类。`customize_dataset.md` 的 `MyDataset` 就是这条路线。
3. **完全新实现**：自己写 converter + 数据集类，适合数据组织方式与任何官方数据集都差异巨大的场合。

#### 4.1.2 核心流程

选型决策可以这样画：

```text
自有数据
  ├─ 只有 LiDAR 点云 + 简单 3D 框标注，想快点跑通
  │     → 路线 2：v2 格式 + MyDataset（本讲主线，工作量最小）
  ├─ 标注天然是相机系 + 有完整标定，想蹭 KITTI 评估体系
  │     → 路线 1：转 KITTI 格式，直接用 KittiDataset/KittiMetric
  └─ 有自己的评估协议（类似 Waymo）
        → 路线 3：converter + Dataset + Metric 三件套全自写
```

三条路线的共同点：**模型与训练代码完全不用改**，改的只是「数据进入 mmdet3d 的入口」。这正是 u1-l5 讲过的配置驱动架构的好处——数据集是注册器上的一个可替换组件。

#### 4.1.3 源码精读

先看官方对路线 1 的完整描述：

> 假设我们使用 Waymo 数据集。下载后需要实现一个函数把输入数据和标注格式都转成 KITTI 风格，然后实现继承自 `KittiDataset` 的 `WaymoDataset` 加载数据并训练，再实现继承自 `KittiMetric` 的 `WaymoMetric` 用于评估。

这段话在 [new_data_model.md:71-75](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docs/en/user_guides/new_data_model.md#L71-L75)。注意它的分工：**格式转换解决「读」，Metric 子类解决「评」**，二者是独立决策——数据用 KITTI 格式不代表评估必须用 KITTI 协议。

而路线 2 的核心卖点是：v2 标准格式是跨数据集统一的（u3-l2 已讲），所以自定义数据集类可以薄到只重写一个方法。后两个模块分别展开「类怎么写」和「pkl 怎么产」。

#### 4.1.4 代码实践

**实践目标**：审视自己的数据，完成一次选型。

1. 列出你的数据具备哪些要素：点云格式（是否 `.bin`）、标注坐标系（LiDAR/相机/其他）、是否有标定矩阵、类别数。
2. 对照上面决策树选出路线，并写下一句理由。
3. **需要观察的现象**：如果标注在相机坐标系且没有 `lidar2cam` 标定，路线 2 的「LiDAR 系直接存框」就行不通，必须退回路线 1 或先补标定。
4. **预期结果**：得到一张「我的数据 → 路线 N → 需要自写的组件清单」的对照表。本讲后续以路线 2 展开。

#### 4.1.5 小练习与答案

**练习 1**：你的数据集只有 5 个类别、点云已是 `.bin`、标注已换算到 LiDAR 坐标系，选哪条路线？

**答案**：路线 2。写一个百行以内的 converter 产出 v2 pkl，再加一个只重写 `parse_ann_info` 的 `MyDataset`；评估直接挂 `KittiMetric`（官方对自定义数据集只提供 KITTI 风格评估，见 [customize_dataset.md:496-503](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docs/en/advanced_guides/customize_dataset.md#L496-L503)）。

**练习 2**：为什么 Waymo 选择转成 KITTI 格式而不是直接实现一个全新数据集类？

**答案**：转成 KITTI 格式后，`WaymoDataset` 可以继承 `KittiDataset` 免费获得「相机系标注逆变换到雷达系」等全部解析逻辑（参考 [kitti_dataset.py:135-173](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/kitti_dataset.py#L135-L173) 的 `parse_ann_info`），自己只需要处理 `load_interval` 抽帧等差异；全新实现则要把这些逻辑重写一遍并重新踩坑。

### 4.2 最小自定义数据集类：MyDataset 与 parse_ann_info

#### 4.2.1 概念说明

数据集类管「读什么」，管线管「怎么变」（u3-l1 的核心分工）。因此 `MyDataset` 的设计目标是**尽可能薄**：基类 `Det3DDataset` 已经替你做了绝大多数工作，子类只需要补上「裸数组 → 框对象」这最后一步，以及声明自己有哪些类别。

为什么最后一步必须在子类做？因为基类不知道两件事：你的框在哪个坐标系、该包装成哪个子类的框对象。回顾对照组 KITTI 的做法（u3-l3）：它的标注在相机系，所以要先 `CameraInstance3DBoxes(...).convert_to(LiDAR, inv(lidar2cam))`；而我们的自定义数据已经统一存到 LiDAR 系，于是只剩 `LiDARInstance3DBoxes(...)` 一步包装。

#### 4.2.2 核心流程

`MyDataset` 一条样本的解析流程（基类 + 子类增量）：

```text
BaseDataset.__getitem__(idx)
  └─ Det3DDataset.prepare_data(idx)                 # u3-l1 已讲
       ├─ get_data_info(idx) → parse_data_info      # 基类默认实现即可
       │    ├─ lidar_path 拼 data_prefix['pts'] 前缀
       │    └─ 训练态: info['ann_info'] = self.parse_ann_info(info)  ← 子类接管
       │         ├─ 基类: instances 逐字段转 ndarray, 剥前缀改名
       │         ├─ 子类: 空 gt 补 (0,7) 零数组
       │         ├─ 子类: _remove_dontcare 过滤 label=-1
       │         └─ 子类: 包装 LiDARInstance3DBoxes
       └─ pipeline(input_dict) → dict(inputs, data_samples)
```

#### 4.2.3 源码精读

**第一处：基类 `parse_ann_info` 已经做了什么。** 看 [det3d_dataset.py:218-254](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/det3d_dataset.py#L218-L254)：基类维护一张改名表，把 `bbox_3d → gt_bboxes_3d`、`bbox_label_3d → gt_labels_3d`，并把每个字段堆叠成 `np.ndarray`：

```python
name_mapping = {
    'bbox_label_3d': 'gt_labels_3d',
    'bbox_label': 'gt_bboxes_labels',
    'bbox': 'gt_bboxes',
    'bbox_3d': 'gt_bboxes_3d',
    ...
}
```

同时做标签重映射——[det3d_dataset.py:237-241](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/det3d_dataset.py#L237-L241) 中凡是名字带 `label` 的字段都过一遍 `self.label_mapping`。这张映射表在构造函数里生成（[det3d_dataset.py:104-124](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/det3d_dataset.py#L104-L124)）：当配置传入 `metainfo=dict(classes=三类)` 而类属性 `METAINFO` 有八类时，未选中的类别会被映射成 `-1`（dontcare）。**这是子集训练的实现位置**——你的 converter 写入 pkl 的 `bbox_label_3d` 必须按 `METAINFO['classes']` 的顺序编号，配置里选子集时基类自动把多余类压成 -1。

**第二处：官方 MyDataset 模板。** [customize_dataset.md:211-251](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docs/en/advanced_guides/customize_dataset.md#L211-L251) 给出的模板只有三步增量：空 gt 兜底、过滤 dontcare、包装框对象。注意文档模板本身**省略了 import**（`np` 与 `LiDARInstance3DBoxes` 未导入，直接运行会 `NameError`），下面是补全并加注释的版本（示例代码，基于官方模板整理）：

```python
import numpy as np

from mmdet3d.registry import DATASETS
from mmdet3d.structures import LiDARInstance3DBoxes
from .det3d_dataset import Det3DDataset


@DATASETS.register_module()
class MyDataset(Det3DDataset):

    # 必须与 converter 写 pkl 时使用的类别顺序一致
    METAINFO = {
        'classes': ('Pedestrian', 'Cyclist', 'Car')
    }

    def parse_ann_info(self, info):
        ann_info = super().parse_ann_info(info)   # 基类: 改名 + ndarray 化
        if ann_info is None:                      # 空 gt 帧 (instances 为空列表)
            ann_info = dict()
            ann_info['gt_bboxes_3d'] = np.zeros((0, 7), dtype=np.float32)
            ann_info['gt_labels_3d'] = np.zeros(0, dtype=np.int64)

        ann_info = self._remove_dontcare(ann_info)  # 过滤 label == -1
        gt_bboxes_3d = LiDARInstance3DBoxes(ann_info['gt_bboxes_3d'])
        ann_info['gt_bboxes_3d'] = gt_bboxes_3d     # 裸数组 → 框对象
        return ann_info
```

`_remove_dontcare` 是基类现成方法（[det3d_dataset.py:158-177](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/det3d_dataset.py#L158-L177)），用布尔掩码 `gt_labels_3d > -1` 同步过滤所有数组字段。

**第三处：注册节点。** `MyDataset` 要被 `DATASETS` 节点收录（[registry.py:58-59](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/registry.py#L58-L59) 声明该节点自动扫描 `mmdet3d.datasets` 目录）。两个挂接选择：把文件放进 `mmdet3d/datasets/` 并加入其 `__init__.py`（改源码，适合长期维护）；或放在任意位置、在配置里用 `custom_imports = dict(imports=['my_project.my_dataset'], allow_failed_imports=False)` 显式导入（不改源码，下一讲 u8-l3 的 example_project 会专门讲这种模式）。

#### 4.2.4 代码实践

**实践目标**：不动数据，先验证「MyDataset 之外的一切都是现成的」。

1. 打开 [tests/test_datasets/test_kitti_dataset.py:13-62](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tests/test_datasets/test_kitti_dataset.py#L13-L62)，看官方单测如何用 `tests/data/kitti/` 下 1 条样本的迷你 pkl 实例化数据集并断言 `ann_info` 的键与类型。
2. 把上面补全版 `MyDataset` 保存为独立脚本，`from mmdet3d.datasets.det3d_dataset import Det3DDataset` 后仅执行类定义与注册两步（不实例化）。
3. **需要观察的现象**：脚本 import 成功、无注册冲突报错；若重复执行两次 import，会看到 `KeyError`/已注册警告（同一类名注册两次）。
4. **预期结果**：确认 `MyDataset` 的增量代码不超过 15 行，其余全部由基类承担。完整运行依赖第 4.3 节的 pkl，此处只验证类定义与注册链路。

#### 4.2.5 小练习与答案

**练习 1**：若你的 `METAINFO` 有 8 类，但训练配置只传 `metainfo=dict(classes=['Car'])`，标注里的一辆 `Pedestrian` 最终 label 是几？会被画进监督吗？

**答案**：是 `-1`。构造函数把未选类全部映射为 -1（[det3d_dataset.py:104-114](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/det3d_dataset.py#L104-L114)），`MyDataset.parse_ann_info` 里的 `_remove_dontcare` 随即将其整行删除，不参与损失计算。测试文件 [test_kitti_dataset.py:89-108](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tests/test_datasets/test_kitti_dataset.py#L89-L108) 正是这一行为的回归验证（`classes=['Car']` 时断言 `len(gt_labels_3d) == 0`）。

**练习 2**：为什么 `MyDataset` 里包装的是 `LiDARInstance3DBoxes` 而不是 `CameraInstance3DBoxes`？

**答案**：因为路线 2 约定「3D 框统一存在 LiDAR 坐标系」（[customize_dataset.md:60](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docs/en/advanced_guides/customize_dataset.md#L60)），且配置里 `box_type_3d='LiDAR'`。框子类决定重力轴（z 轴）与 yaw 约定（u2-l1/u2-l2），后续管线的 `rotate/flip` 增强与 PointPillars 的 anchor 都按 LiDAR 约定运作。

**练习 3**：空 gt 帧会在训练时发生什么？

**答案**：`prepare_data` 在管线前检查 `len(gt_labels_3d) == 0` 就返回 `None`（[det3d_dataset.py:381-383](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/det3d_dataset.py#L381-L383)），`__getitem__` 会随机换一条样本。所以 `parse_ann_info` 的空 gt 兜底主要是让「无标注的 val/test 帧」和 `load_eval_anns` 路径不崩。

### 4.3 自定义 converter：手工生成 v2 格式 info pkl

#### 4.3.1 概念说明

converter 的职责：读你的原始点云与标注，产出一个（或按 split 多个）info pkl。这一节有两个关键认知：

**其一，一个必须指出的文档漂移。** [customize_dataset.md:199-205](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docs/en/advanced_guides/customize_dataset.md#L199-L205) 声称可以用一条命令生成自定义数据的 info：

```bash
python tools/create_data.py custom --root-path ./data/custom --out-dir ./data/custom --extra-tag custom
```

但查看 [create_data.py:317-420](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/create_data.py#L317-L420) 的完整分派链：只有 `kitti`、`nuscenes`、`waymo`、`lyft`、`scannet`、`s3dis`、`sunrgbd`、`semantickitti` 八个分支，**没有 `custom`**，落到底会执行 [create_data.py:419-420](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/create_data.py#L419-L420) 的 `raise NotImplementedError(f'Don\'t support custom dataset.')`。所以在当前 HEAD（v1.4.0）上，自定义 converter 需要自己写——这正是 u7-l5「读源码可识破文档漂移」方法论的又一次印证。

**其二，pkl 格式以 v2 字段定义为准。** 写 converter 不需要发明格式，照抄 [update_infos_to_v2.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/dataset_converters/update_infos_to_v2.py) 里官方的空模板与 KITTI 升级实现即可。

#### 4.3.2 核心流程

一个最小 LiDAR 检测 converter 的流水线：

```text
读 ImageSets/train.txt（帧名列表）
  对每帧:
    ├─ 读 labels/{帧名}.txt，每行 8 列:
    │     x y z dx dy dz yaw category_name
    ├─ 类名 → 索引: 在 METAINFO['classes'] 中查编号，查不到记 -1
    ├─ 组装单条 info（v2 三段式最小集）:
    │     sample_idx, lidar_points{lidar_path, num_pts_feats},
    │     instances[{bbox_3d=[x,y,z,dx,dy,dz,yaw], bbox_label_3d}]
    └─ 注意 z 是底面中心高度（与 LiDARInstance3DBoxes 默认 origin 对齐）
顶层包装 dict(metainfo={'categories', 'dataset', 'info_version'}, data_list=[...])
mmengine.dump(..., 'custom_infos_train.pkl')
```

#### 4.3.3 源码精读

**字段权威定义一：单条 instance。** [update_infos_to_v2.py:28-67](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/dataset_converters/update_infos_to_v2.py#L28-L67) 的 `get_empty_instance` 列出全部合法键，注释即规范：`bbox_3d` 是「7（或 9）个数，顺序 \([x, y, z, w, h, l, yaw]\)（或加 \(v_x, v_y\)）」，`bbox_label_3d` 是 3D 类别标签，`num_lidar_pts`/`difficulty` 等可选。`clear_instance_unused_keys`（[update_infos_to_v2.py:187-192](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/dataset_converters/update_infos_to_v2.py#L187-L192)）会删掉值为 `None` 的键，所以**只写你需要的键即可**。

**字段权威定义二：单条 info 骨架。** [update_infos_to_v2.py:158-184](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/dataset_converters/update_infos_to_v2.py#L158-L184) 的 `get_empty_standard_data_info`：顶层 `sample_idx`、`lidar_points`、`instances`、可选 `images`/`lidar_sweeps` 等。LiDAR-only 检测只需要前三个。

**参考实现：KITTI 的 v2 组装。** [update_infos_to_v2.py:457-509](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/dataset_converters/update_infos_to_v2.py#L457-L509) 展示官方如何把一份旧标注逐字段填进模板，其中两段最值得抄：

```python
# 类名 → label；未收录的类记 -1（将成 dontcare）
if anns['name'][instance_id] in METAINFO['classes']:
    empty_instance['bbox_label'] = METAINFO['classes'].index(...)
else:
    empty_instance['bbox_label'] = -1
```

（[update_infos_to_v2.py:466-471](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/dataset_converters/update_infos_to_v2.py#L466-L471)）以及顶层包装（[update_infos_to_v2.py:519-529](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/dataset_converters/update_infos_to_v2.py#L519-L529)）：

```python
metainfo = dict()
metainfo['categories'] = {k: i for i, k in enumerate(METAINFO['classes'])}
metainfo['dataset'] = 'kitti'
metainfo['info_version'] = '1.1'
converted_data_info = dict(metainfo=metainfo, data_list=converted_list)
mmengine.dump(converted_data_info, out_path, 'pkl')
```

**下游消费方：路径拼接规则。** 你写入 pkl 的 `lidar_path` 应当是**相对** `data_prefix['pts']` 的文件名（KITTI 只存 `Path(...).name`，见 [update_infos_to_v2.py:431-432](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/dataset_converters/update_infos_to_v2.py#L431-L432)）。消费方是 [det3d_dataset.py:278-285](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/det3d_dataset.py#L278-L285)：`osp.join(self.data_prefix.get('pts',''), info['lidar_points']['lidar_path'])`。即最终绝对路径 = `data_root` + `data_prefix['pts']` + pkl 里的 `lidar_path`。

**标签文件的官方格式约定。** [customize_dataset.md:49-60](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docs/en/advanced_guides/customize_dataset.md#L49-L60)：

```text
# format: [x, y, z, dx, dy, dz, yaw, category_name]
1.23 1.42 0.23 3.96 1.65 1.55 1.56 Car
```

并强调「3D 框须存在统一 3D 坐标系」。结合 [base_box3d.py:43-48](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/base_box3d.py#L43-L48) 的默认 `origin=(0.5, 0.5, 0)`，z 应填框底面中心。

#### 4.3.4 代码实践

**实践目标**：写一个可直接运行的 converter（示例代码），输入官方文档约定的目录结构，输出 v2 pkl。

1. 按文档 [customize_dataset.md:81-105](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docs/en/advanced_guides/customize_dataset.md#L81-L105) 摆好目录：`data/custom/{ImageSets/train.txt, points/000000.bin, labels/000000.txt}`。点云可先从 `tests/data/kitti/training/velodyne_reduced/` 或 `demo/data/kitti/000008.bin` 复制。
2. 运行下面的 converter（示例代码）：

```python
# my_converter.py（示例代码，放任意目录）
from pathlib import Path
import mmengine

CLASSES = ('Pedestrian', 'Cyclist', 'Car')   # 顺序即 label 编号，须与 MyDataset.METAINFO 一致
root = Path('data/custom')
split = 'train'

data_list = []
for idx in mmengine.list_from_file(root / 'ImageSets' / f'{split}.txt'):
    instances = []
    label_file = root / 'labels' / f'{idx}.txt'
    if label_file.exists():
        for line in label_file.read_text().splitlines():
            parts = line.strip().split()
            if len(parts) != 8:
                continue
            x, y, z, dx, dy, dz, yaw, name = parts
            instances.append(dict(
                bbox_3d=[float(x), float(y), float(z),
                         float(dx), float(dy), float(dz), float(yaw)],
                bbox_label_3d=CLASSES.index(name) if name in CLASSES else -1))
    data_list.append(dict(
        sample_idx=int(idx),
        lidar_points=dict(
            lidar_path=f'{idx}.bin',   # 相对 data_prefix['pts']，勿写绝对路径
            num_pts_feats=4),          # .bin 每点列数
        instances=instances))

info = dict(
    metainfo=dict(
        categories={k: i for i, k in enumerate(CLASSES)},
        dataset='custom',
        info_version='1.1'),
    data_list=data_list)
mmengine.dump(info, root / 'custom_infos_train.pkl', 'pkl')
print(f'{len(data_list)} frames written')
```

3. **需要观察的现象**：pkl 生成后，用 `mmengine.load` 读回并打印 `data_list[0]` 的全部键；对照 `tests/data/kitti/kitti_infos_train.pkl`（仓库自带迷你样例）的键结构，确认 `sample_idx`/`lidar_points`/`instances` 三段都在。
4. **预期结果**：pkl 可被 pickle 正常读取，单条结构与 4.3.3 节模板一致。若标签里有类名不在 `CLASSES`，确认它被记为 `-1` 而不是抛异常。（命令输出「待本地验证」——本讲义未代跑。）

#### 4.3.5 小练习与答案

**练习 1**：为什么 converter 里 `lidar_path` 只写文件名，不写 `data/custom/points/000000.bin` 全路径？

**答案**：数据集类会用 `osp.join(data_root, data_prefix['pts'], lidar_path)` 拼绝对路径（[det3d_dataset.py:278-282](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/det3d_dataset.py#L278-L282)）。pkl 里存相对名可以让同一份数据通过改 `data_root` 在不同机器/挂载点上复用；写死绝对路径则换环境即失效。

**练习 2**：如果不小心把 `num_pts_feats` 写成 5 而点云实际每点 4 列，会在哪一步炸？

**答案**：不在数据集类，而在管线 `LoadPointsFromFile`：它按 `load_dim`/`use_dim` 切片点数组，`num_pts_feats` 只是记在 info 里的元数据。真正强一致的是**配置里的 `load_dim=4` 与 `.bin` 文件布局**（u3-l5）；`num_pts_feats` 错了通常无声通过，排查时要想到它。

**练习 3**：v2 模板里 `bbox_3d` 顺序写的是 \([x, y, z, w, h, l, yaw]\)，而 LiDAR 系框对象的张量语义是什么？

**答案**：LiDAR 系下是 \([x, y, z, dx, dy, dz, yaw]\)，即第 4/5 维直接对应 x/y 方向边长（u2-l2）。模板注释沿用了相机系的字母习惯，对 LiDAR 数据而言「w、h、l」应理解为 dx、dy、dz——文档字母与坐标系语义不对应是自定义数据集常见的混淆源，务必以 `LiDARInstance3DBoxes` 的属性文档为准。

### 4.4 配置挂接与训练前自检

#### 4.4.1 概念说明

数据侧就绪后，还差一份配置把 `MyDataset`、管线、模型、评估器连起来。官方在 [customize_dataset.md:260-485](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docs/en/advanced_guides/customize_dataset.md#L260-L485) 给了三段式模板（数据集 config / 模型 config / 总装 config），仓库里**并不存在**对应的 `configs/_base_/datasets/custom.py` 文件（已核实，Glob 无匹配），需要照模板自建。这一节讲模板中三处最易错的联动关系。

#### 4.4.2 核心流程

自定义数据集的配置联动链：

```text
点云范围 point_cloud_range ──┬─→ voxel_size ──→ 伪图像尺寸 output_shape = [ny, nx]
                             │        ny = (y_max - y_min) / voxel_y
                             │        nx = (x_max - x_min) / voxel_x
                             ├─→ PointsRangeFilter / ObjectRangeFilter（管线过滤）
                             └─→ anchor_generator.ranges（锚框铺满范围）
类别统计（anchor 尺寸）──────→ sizes 用训练集平均 长/宽/高
类别顺序 METAINFO ──────────→ num_classes / assigner 个数（逐类分配时）
```

#### 4.4.3 源码精读

**第一处：数据集 config 的关键行。** 模板 [customize_dataset.md:318-355](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docs/en/advanced_guides/customize_dataset.md#L318-L355) 中与 converter 约定咬合的三行：

```python
dataset=dict(
    type=dataset_type,                 # 'MyDataset'
    data_root=data_root,
    data_prefix=dict(pts='points'),    # 与 pkl 内相对 lidar_path 拼接
    ann_file='custom_infos_train.pkl',
    metainfo=metainfo,                 # 训练子集，经 label_mapping 生效
    box_type_3d='LiDAR')
```

`data_prefix['pts']='points'` 必须匹配你的目录；`box_type_3d='LiDAR'` 经 `get_box_type` 解析为框子类并注入每条样本（u3-l1）。

**第二处：模型侧的硬联动。** [customize_dataset.md:358-367](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docs/en/advanced_guides/customize_dataset.md#L358-L367) 明确警告：改了 `point_cloud_range` 与 `voxel_size` 后，必须同步改 `middle_encoder`（PointPillarsScatter）的 `output_shape`。文档给的算例：

\[ \text{shape} = \left[\frac{z_{max}-z_{min}}{d_z}+1,\ \frac{y_{max}-y_{min}}{d_y},\ \frac{x_{max}-x_{min}}{d_x}\right] \]

对柱体（PointPillars 的 `voxel_size=[0.16, 0.16, 4]`、range \([0,-39.68,-3,69.12,39.68,1]\)）则退化为二维：`output_shape=[496, 432]`，即 \(n_y = 79.36/0.16 = 496\)、\(n_x = 69.12/0.16 = 432\)（[customize_dataset.md:372-394](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docs/en/advanced_guides/customize_dataset.md#L372-L394)）。`anchor_generator.sizes` 建议用训练集统计的各类平均尺寸，`ranges` 的 z 值要随点云高度平移（文档专门给了 [issue #986](https://github.com/open-mmlab/mmdetection3d/issues/986) 提醒，见 [customize_dataset.md:365](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docs/en/advanced_guides/customize_dataset.md#L365)）。

**第三处：评估器限定。** [customize_dataset.md:496-503](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docs/en/advanced_guides/customize_dataset.md#L496-L503)：自定义数据集目前只提供 KITTI 风格评估——`val_evaluator = dict(type='KittiMetric', ann_file=..., metric='bbox')`。KittiMetric 的行为（difficulty 分档、41 点 AP）已在 u7-l2 讲过；注意它要求标注带 KITTI 式 difficulty 信息，纯 LiDAR 自标数据可能得到退化但仍可用的指标。

#### 4.4.4 代码实践

**实践目标**：跑通「不训练、只取一条样本」的完整配置验证（源码阅读 + 局部运行型实践）。

1. 照模板写一份最小 `custom.py`：`train_pipeline` 可以先**去掉** `ObjectSample`（GT 数据库还没建，见 u3-l7；`create_groundtruth_database` 需要 `MyDataset` 注册后才能按类名调用），保留 Load → 增强 → RangeFilter → Pack 骨架。
2. 总装 config 四件套继承（[customize_dataset.md:479-485](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docs/en/advanced_guides/customize_dataset.md#L479-L485)），先不动 schedule。
3. 验证脚本（示例代码）：

```python
from mmengine.config import Config
from mmdet3d.registry import DATASETS
import my_dataset  # 触发 MyDataset 注册（文件位置同 4.2 实践）

cfg = Config.fromfile('configs/pointpillars/pointpillars_hv_secfpn_custom.py')
ds = DATASETS.build(cfg.train_dataloader.dataset)
data = ds[0]                       # 取一条经过完整管线的样本
print(data['data_samples'].gt_instances_3d.labels_3d)
print(data['points'].shape)
```

4. **需要观察的现象**：构造数据集时终端先打印官方的类别统计表（[det3d_dataset.py:144-156](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/det3d_dataset.py#L144-L156) 的 AsciiTable）——这张表就是免费的标注正确性体检；`ds[0]` 后打印 3D 标签与点张量形状。
5. **预期结果**：`labels_3d` 均在 \([0, 3)\)（3 类）；`points.shape = (N, 4)` 且 N 小于原始点数（RangeFilter 生效）。若类别统计表某类为 0，先查 converter 的类名映射再查配置子集。（运行输出待本地验证。）
6. 训练前的官方建议：用 `tools/misc/browse_dataset.py` 可视化标注（[customize_dataset.md:487-490](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docs/en/advanced_guides/customize_dataset.md#L487-L490)），用法见 u7-l5。

#### 4.4.5 小练习与答案

**练习 1**：把 `point_cloud_range` 从 KITTI 的 `[0,-39.68,-3,69.12,39.68,1]` 改成 `[0,-20,-3,40,20,1]`，`voxel_size=[0.16,0.16,4]` 不变，`output_shape` 应填多少？还有哪两处配置必须跟着改？

**答案**：\(n_y=40/0.16=250\)，\(n_x=40/0.16=250\)，故 `output_shape=[250, 250]`。同步改：管线的 `PointsRangeFilter`/`ObjectRangeFilter` 的 `point_cloud_range`（否则管线与模型视野不一致），以及 `anchor_generator.ranges`（anchor 网格仍铺旧范围会与特征图错位）。

**练习 2**：为什么模板的 `train_pipeline` 里有 `ObjectNoise` 而建议你先删 `ObjectSample`？

**答案**：`ObjectSample` 依赖离线生成的 GT 数据库（dbinfos pkl + 每目标点云 bin，u3-l7），而 `create_groundtruth_database` 的第一个参数是**数据集类名字符串**——`MyDataset` 未注册进 `mmdet3d.datasets` 时它无法反射构建。`ObjectNoise` 只对当前帧的框与点做扰动，无外部依赖，可安全保留。等链路跑通后再补建库并加回。

**练习 3**：`browse_dataset.py` 显示的 3D 框整体偏移半个框高，最可能错在哪一环？

**答案**：z 锚点口径不一致：标签把 z 写成了**几何中心**（框中部），而 `LiDARInstance3DBoxes` 默认 `origin=(0.5,0.5,0)` 期待**底面中心**（[base_box3d.py:43-48](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/bbox_3d/base_box3d.py#L43-L48)）。修正办法二选一：converter 里 z 统一减去 `dz/2`，或构造框对象时传 `origin=(0.5,0.5,0.5)`。

### 4.5 dataset_wrappers：CBGSDataset 类平衡采样

#### 4.5.1 概念说明

自动驾驶数据集普遍长尾：nuScenes 里 `car` 的帧数远多于 `pedestrian`，按帧均匀采样会让模型见到的行人样本严重不足。`CBGSDataset`（Class-Balanced Grouping and Sampling，论文 [arXiv:1908.09492](https://arxiv.org/abs/1908.09492)）通过**重复采样含稀有类的帧**，把每个类别出现的帧数拉到接近均衡。它不改数据、不改模型，是套在任何数据集外的**透明代理**——这也是「wrapper」在配置驱动架构中的典型用法：`RepeatDataset`（重复整个数据集）与 `CBGSDataset`（按类重复）是同一模式的两种实例。

#### 4.5.2 核心流程

`CBGSDataset` 的初始化与索引映射：

```text
full_init()
  ├─ 内层数据集.full_init()           # 正常读 pkl、建管线
  └─ _get_sample_indices()
       ├─ 对每帧调 dataset.get_cat_ids(idx) → 该帧出现的类别集合
       ├─ 按类建倒排表 class_sample_idxs[c] = [出现类别 c 的帧号...]
       ├─ 计算类别分布 p_c = |class_sample_idxs[c]| / Σ|·|
       └─ 对每类采样 len(c) · ratio_c 个帧号（有放回），
            ratio_c = (1/K) / p_c，K 为类别数
__getitem__(idx) → dataset[ sample_indices[idx] ]   # 全局 idx → 原始 idx
```

采样量的直觉：若第 \(c\) 类只占分布 \(p_c\)，就把它复制 \(\frac{1/K}{p_c}\) 倍，使各类在**采样后**的出现次数都接近总量的 \(1/K\)。稀有类帧被重复多次，常见类帧被稀释。

#### 4.5.3 源码精读

**核心算法。** [dataset_wrappers.py:64-98](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/dataset_wrappers.py#L64-L98)：

```python
classes = self.metainfo['classes']
cat2id = {name: i for i, name in enumerate(classes)}
for idx in range(len(dataset)):
    sample_cat_ids = dataset.get_cat_ids(idx)
    for cat_id in sample_cat_ids:
        if cat_id != -1:          # -1 是 dontcare，不参与均衡
            class_sample_idxs[cat_id].append(idx)
...
frac = 1.0 / len(classes)
ratios = [frac / v for v in class_distribution.values()]
for cls_inds, ratio in zip(list(class_sample_idxs.values()), ratios):
    sample_indices += np.random.choice(cls_inds, int(len(cls_inds) * ratio)).tolist()
```

注意 `get_cat_ids` 正是 `Det3DDataset` 的 [det3d_dataset.py:408-423](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/det3d_dataset.py#L408-L423)——读 `ann_info['gt_labels_3d']` 返回类别集合。**这构成对自定义数据集类的一条隐式接口要求**：你的 `MyDataset` 继承基类即免费获得该实现，无需重写。

**透明代理。** [dataset_wrappers.py:125-136](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/dataset_wrappers.py#L125-L136) 的 `get_data_info` 与 [dataset_wrappers.py:138-153](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/dataset_wrappers.py#L138-L153) 的 `__getitem__` 都只做「全局 idx → `sample_indices[idx]` → 委托内层数据集」；`metainfo` 深拷贝自内层（[dataset_wrappers.py:44-51](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/dataset_wrappers.py#L44-L51)），保证 DataLoader、Evaluator 等外层组件完全无感。

**真实用例。** nuScenes 上的 CenterPoint 把训练集包了一层（[centerpoint_pillar02_second_secfpn_8xb4-cyclic-20e_nus-3d.py:140-148](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/centerpoint/centerpoint_pillar02_second_secfpn_8xb4-cyclic-20e_nus-3d.py#L140-L148)）：

```python
dataset=dict(
    type='CBGSDataset',
    dataset=dict(type=dataset_type, ...))   # 原数据集配置原封不动内嵌
```

另外两个有意设计：`__len__` 返回 `len(sample_indices)` 而非内层长度（[dataset_wrappers.py:155-162](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/dataset_wrappers.py#L155-L162)），即一个「epoch」是采样后的虚拟长度；`get_subset/get_subset_` 被显式禁用并说明原因（[dataset_wrappers.py:164-182](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/dataset_wrappers.py#L164-L182)）——对「同一帧出现多次」的数据集，子集的语义是歧义的。

#### 4.5.4 代码实践

**实践目标**：数感训练——亲手算一遍 CBGS 的采样量，再用配置启用它。

1. 设 3 类数据：`Car` 出现在 800 帧、`Pedestrian` 出现在 100 帧、`Cyclist` 出现在 100 帧（帧可重复计类）。按 `_get_sample_indices` 的公式手算三类各被采样多少帧、`len(dataset)` 变为多少。
2. 把 4.4 实践中的 config 包一层：`train_dataloader.dataset = dict(type='CBGSDataset', dataset=<原 dataset dict>)`，重新 `DATASETS.build`。
3. **需要观察的现象**：构造时间变长（每帧都要跑一次 `get_data_info` 提取类别）；`len()` 与内层数据集长度不同。
4. **预期结果**：手算得 Car/Ped/Cyc 分布为 0.8/0.1/0.1，ratios = \((1/3)/0.8, (1/3)/0.1, (1/3)/0.1\) = 0.417/3.33/3.33，采样量 ≈ 334/333/333，`len ≈ 1000`，恰好是原始 1000 帧但组成已均衡。（具体数值待本地验证。）

#### 4.5.5 小练习与答案

**练习 1**：CBGS 与 `RepeatDataset` 都会「重复」数据，本质区别是什么？

**答案**：`RepeatDataset` 对**所有帧**无差别重复 K 次，只延长 epoch、不改变类别构成（u1-l5 见过 `kitti 160e = 80e × RepeatDataset(2)`）；`CBGSDataset` 是**按类倒排表**选择性重复，目标是均衡各类出现次数，且总长度由采样公式决定而非固定倍数。

**练习 2**：为什么 `CBGSDataset` 的 `get_subset` 直接 `raise NotImplementedError`？

**答案**：[dataset_wrappers.py:164-182](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/dataset_wrappers.py#L164-L182) 的注释说明：同一原始帧在 `sample_indices` 中出现多次，对其做子集切分的语义有歧义（按虚拟索引？按原始帧？）。需要子集时应先在内层数据集上 `get_subset` 再包 CBGS。

**练习 3**：`CBGSDataset` 包装 `MyDataset` 时，`MyDataset` 需要额外实现什么吗？

**答案**：不需要。CBGS 只依赖 `get_cat_ids`（[dataset_wrappers.py:77](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/dataset_wrappers.py#L77) 调用），而 `Det3DDataset` 基类已提供默认实现（[det3d_dataset.py:408-423](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/det3d_dataset.py#L408-L423)）。继承即是接口 compliance——这也是把数据集类写得薄的长远收益。

## 5. 综合实践

**任务：从零搭建 `my_custom_dataset` 并让 PointPillars 吃下第一条训练样本。** 本任务串联本讲全部四个最小模块，数据量控制在 2~3 帧即可。

1. **造数据**（约 10 分钟）：
   - 建 `data/custom/{ImageSets,points,labels}`；从 `tests/data/kitti/training/velodyne_reduced/` 复制 2 个 `.bin` 到 `points/`（文件名改短，如 `000000.bin`）。
   - 手写 `labels/000000.txt`，两行标注（示例，LiDAR 系、底面中心锚点）：

     ```text
     15.0 0.0 -1.70 3.90 1.60 1.56 1.57 Car
     10.0 2.5 -1.73 0.80 0.60 1.73 0.00 Pedestrian
     ```

   - 写 `ImageSets/train.txt`（两行帧名）。
2. **写 converter**：直接使用 4.3.4 的 `my_converter.py`，产出 `custom_infos_train.pkl`。
3. **写数据集类**：使用 4.2.3 补全版 `MyDataset`（独立文件，不改源码树）。
4. **写配置**：照 4.4 的最小 config——注意 `voxel_size`/`output_shape`/`point_cloud_range` 三联动（KITTI 原点云范围可直接沿用，因为数据来自 KITTI）；先去掉 `ObjectSample`。
5. **验证闭环**（成功标准）：

   ```python
   # 验收脚本（示例代码）
   import my_dataset
   from mmengine.config import Config
   from mmdet3d.registry import DATASETS

   cfg = Config.fromfile('configs/pointpillars/pointpillars_hv_secfpn_custom.py')
   ds = DATASETS.build(cfg.train_dataloader.dataset)
   data = ds[0]
   s = data['data_samples']
   print(type(s.gt_instances_3d.bboxes_3d))      # 应为 LiDARInstance3DBoxes
   print(s.gt_instances_3d.labels_3d)            # 应含 0~2 内的标签
   print(data['points'].shape)                   # (N, 4)
   ```

   三行输出全部符合注释预期，即宣告「自有数据接入成功」。
6. **扩展步（可选）**：数据量增大后，为 `MyDataset` 补建 GT 数据库（参考 u3-l7 的 `create_groundtruth_database`，把第一个参数换成 `'MyDataset'`），把 `ObjectSample` 加回 train_pipeline；再把训练集包一层 `CBGSDataset` 观察类别统计表的变化。
7. 若某一步报错，按「链路逆推」排查：pkl 键名（4.3 模板）→ 类别映射（4.2 练习 1）→ 路径拼接（4.3 练习 1）→ 范围联动（4.4 练习 1）。全程**待本地验证**，本讲义提供的是经源码核实的正确链路而非代跑结果。

## 6. 本讲小结

- 官方给三条接入路线：转既有格式（Waymo→KITTI 式）、转 v2 标准格式配薄数据集类（`MyDataset` 式）、完全新实现；模型与训练代码在三条约定的任何一条下都不必改。
- `MyDataset` 的增量只有三步：空 gt 兜底、`_remove_dontcare` 过滤 -1、包装 `LiDARInstance3DBoxes`；改名、ndarray 化、子集 `label_mapping` 全由 `Det3DDataset.parse_ann_info` 基类承担，且 `METAINFO['classes']` 的顺序必须与 converter 的编号约定一致。
- **文档漂移警示**：`tools/create_data.py custom` 命令在当前代码中不存在（分派链止于 `NotImplementedError`），自定义 converter 需手写；pkl 字段以 `update_infos_to_v2.py` 的模板与 `update_kitti_infos` 为权威参考，单条最小集为 `sample_idx` + `lidar_points{lidar_path, num_pts_feats}` + `instances[{bbox_3d, bbox_label_3d}]`，z 为底面中心。
- 配置侧三大硬联动：`point_cloud_range` 与 `voxel_size` 决定 `output_shape=[(y范围)/dy, (x范围)/dx]`；管线过滤与 anchor 范围须同步；评估自定义数据集目前只能挂 `KittiMetric`。
- `CBGSDataset` 是类平衡采样包装器：按类倒排表以 \(\text{ratio}_c = (1/K)/p_c\) 重复采样稀有类帧，对内层数据集透明；`MyDataset` 继承基类即满足其唯一接口依赖 `get_cat_ids`。
- 训练前自检两板斧：数据集构造时打印的类别统计表、`browse_dataset.py` 可视化。

## 7. 下一步学习建议

- **u8-l3（自定义模型与 example_project 模板）**：本讲的 `MyDataset` 用独立脚本 + 手动 import 挂接，下一讲把同样的注册套路用到模型侧，并讲 `custom_imports` 让 config 自动导入你的扩展包——与本讲 4.2 的「两个挂接选择」呼应。
- **u8-l4（projects 生态导览）**：BEVFusion、DETR3D 等项目正是「自定义数据集 + 自定义模型 + 自定义 hook」的完整工程化范本，可对照本讲检验自己的扩展是否够「mmdet3d 味」。
- **源码延伸阅读**：把 `tools/dataset_converters/indoor_converter.py` 与 `update_infos_to_v2.py` 里的 `update_scannet_infos` 读完，你就能把本讲的 LiDAR 版 converter 推广到室内 Depth 坐标系数据（`pts_instance_mask`/`pts_semantic_mask` 两个字段是室内特有的增量，回顾 u3-l4）。
