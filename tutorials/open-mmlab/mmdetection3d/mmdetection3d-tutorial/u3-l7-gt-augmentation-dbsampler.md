# GT 数据库采样：ObjectSample 与 DataBaseSampler

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚 GT 数据库（GT database / dbinfos）是什么、它由哪个脚本离线生成、里面的每一条记录长什么样。
2. 追踪 `DataBaseSampler` 从「加载 dbinfos → prepare 过滤 → 按类抽名额 → 碰撞测试 → 平移贴回场景」的完整在线流程，并能指出 `filter_by_min_points` 到底在哪个时刻生效。
3. 阅读 `ObjectSample.transform`，解释它如何把采样目标拼进当前帧的点云、3D 框与标签，并回答一个经典问题：**粘贴了新的 gt 之后，anchor 与 gt 的对应关系为什么不需要修复**。
4. 亲手用仓库自带的微型数据（或自造的迷你数据库）跑通一次「建库 → 采样 → 粘贴」闭环。

## 2. 前置知识

**GT-Augmentation（GT-Aug，真值增广）是什么？**

自动驾驶点云场景有一个顽固的毛病：类别极度不均衡，而且很多帧里目标太少。KITTI 一帧点云里可能有 15 辆车、1 个行人、0 个骑行者。如果只靠随机旋转、缩放这类「几何增强」，帧内「行人太少」这个事实改变不了。GT-Aug 的思路非常直接——**把别的帧里抠出来的真实目标点云，连同它的 3D 框和类别，直接「贴」到当前帧里**。因为是真实目标而非合成目标，点云形态天然逼真；这一招由 SECOND 首创，PointPillars、CenterPoint 等几乎全部室外检测器的标配。

它分两步：

- **离线建库**：训练前跑一次脚本，把训练集中每个标注框内的点云抠出来存成独立 `.bin` 文件，并把框的位置、类别、点数等信息记进一个 `dbinfos_train.pkl`。这就是上一讲（u3-l2）里 `create_data.py` 三步曲中的第三步「生成 GT 数据库」。
- **在线采样粘贴**：训练时数据管线里的 `ObjectSample` transform，每一帧都从库里抽若干目标贴进来。

本讲需要用到的前置认知（来自前面几讲）：

- **info pkl 与数据集类的分工**（u3-l2 / u3-l1）：converter 离线算好派生量，数据集类在线只做轻量加工。GT 数据库正是这一思想的极致体现——连「每个目标长什么样」都离线存好了。
- **LoadPointsFromFile 的输入契约**（u3-l5）：它从 `results['lidar_points']['lidar_path']` 读 `.bin`，`load_dim` 是文件每点物理列数、`use_dim` 是保留哪几列，读完后包装成 `LiDARPoints` 对象。本讲会看到 `DataBaseSampler` 复用这同一个加载器来读「单个目标」的点云。
- **LiDARInstance3DBoxes 的 7 自由度**（u2-l2）：\((x, y, z, dx, dy, dz, yaw)\)，锚点在底面中心。dbinfos 里存的 `box3d_lidar` 就是这个格式。
- **管线中变换的同步性**（u3-l6）：`ObjectSample` 位于增强链的最前端（几何增强之前），所以它贴进来的目标会被后面的 `RandomFlip3D`、`GlobalRotScaleTrans` 一并变换，不需要单独处理。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| `tools/dataset_converters/create_gt_database.py` | 离线建库：`create_groundtruth_database`（串行版）与 `GTDatabaseCreater`（并行版，Waymo 用） |
| `tools/create_data.py` | 总调度：各数据集的 prep 函数在末尾调用建库 |
| `mmdet3d/datasets/transforms/dbsampler.py` | 在线采样核心：`BatchSampler`（按类队列）与 `DataBaseSampler`（加载/过滤/碰撞/粘贴） |
| `mmdet3d/datasets/transforms/transforms_3d.py` | `ObjectSample`：把 `DataBaseSampler` 的结果合入当前帧 |
| `mmdet3d/datasets/transforms/data_augment_utils.py` | `box_collision_test`：BEV 旋转框碰撞判定 |
| `mmdet3d/structures/ops/box_np_ops.py` | `points_in_rbbox`（点在旋转框内判定）、`center_to_corner_box2d`（中心角表示转四角点） |
| `configs/pointpillars/pointpillars_hv_secfpn_8xb6-160e_kitti-3d-3class.py` | 真实配置样例：`db_sampler` 怎么写、`ObjectSample` 挂在哪 |
| `mmdet3d/models/dense_heads/anchor3d_head.py` | 证明 anchors 是在 loss 时现算的（回答学习目标 3） |
| `mmdet3d/engine/hooks/disable_object_sample_hook.py` | `ObjectSample.disabled` 开关的消费方 |

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：**离线建库 → 采样器构建与过滤 → sample_all 碰撞粘贴 → ObjectSample 合并与 anchors 真相**。

### 4.1 离线建库：create_groundtruth_database 如何「抠」出每个目标

#### 4.1.1 概念说明

GT 数据库的本质是一个「目标素材库」：

```
data/kitti/
├── kitti_gt_database/            # 每个目标一个 .bin 文件
│   ├── 000001_Car_0.bin          # 帧 000001 的第 0 个 Car 的点云
│   ├── 000001_Pedestrian_1.bin
│   ├── 000003_Car_0.bin
│   └── ...
└── kitti_dbinfos_train.pkl       # 每个目标的元信息索引
```

关键设计决定（务必记住，后面在线侧全靠它）：**`.bin` 里存的点云是「以目标框中心为原点」的相对坐标，而 `dbinfos` 里存的 `box3d_lidar` 是建库那一帧里的绝对雷达坐标**。也就是说，点的位置和框的位置是「分离」存的——粘贴时先用框的绝对坐标决定贴到哪，再把点平移过去。这样同一个目标素材可以贴到任何位置。

#### 4.1.2 核心流程

```text
对训练集的每一帧 j：
  1. dataset.get_data_info(j)      → 拿到原始 info 字典
  2. dataset.pipeline(data_info)   → 只跑「加载」管线（Load 点 + Load 标注）
  3. points_in_rbbox(points, boxes)→ (N_points, N_obj) 布尔矩阵：每个点落在哪个框里
  4. 对第 i 个目标：
       gt_points = points[point_indices[:, i]]   # 抠出框内的点
       gt_points[:, :3] -= box[i, :3]            # 平移到以框中心为原点
       存为 {image_idx}_{name}_{i}.bin
       记录 db_info：name/path/box3d_lidar/num_points_in_gt/difficulty/group_id
  5. 全部帧处理完后 pickle.dump(all_db_infos) → {info_prefix}_dbinfos_train.pkl
```

注意第 2 步：建库复用了数据集类自己的 `pipeline`，只是这个 pipeline 是**专门拼出来的极简版**（只加载，不增强）。

#### 4.1.3 源码精读

**入口与各数据集的极简管线。** 函数按数据集名拼出 `dataset_cfg`，KITTI 用 `velodyne_reduced`（去地面点云，u3-l2 讲过的训练硬依赖）+ 4 维点；nuScenes 额外聚合 10 帧 sweep；Waymo 是 6 维点：

- [tools/dataset_converters/create_gt_database.py:L147-L170](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/dataset_converters/create_gt_database.py#L147-L170) — 为 `KittiDataset` 拼出只含 `LoadPointsFromFile` 与 `LoadAnnotations3D` 的管线，读 `training/velodyne_reduced` 下的 4 维点。
- [tools/dataset_converters/create_gt_database.py:L172-L193](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/dataset_converters/create_gt_database.py#L172-L193) — nuScenes 分支：`LoadPointsFromMultiSweeps(sweeps_num=10)`，保证建库时每个目标吃到与训练一致的「当前帧 + 历史 sweep」点，避免分布错位。
- [tools/create_data.py:L46-L53](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/create_data.py#L46-L53) — `kitti_data_prep` 在生成并升级 info 之后调用 `create_groundtruth_database('KittiDataset', ...)`；`with_mask=(version == 'mask')` 控制是否同时裁 2D 图像块（多模态 cut-and-paste 用）。

**主循环：一帧 → 若干目标文件。** 这是整个建库的心脏：

- [tools/dataset_converters/create_gt_database.py:L239-L257](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/dataset_converters/create_gt_database.py#L239-L257) — 对每帧先跑极简管线拿到 `points`（点云对象）、`gt_boxes_3d`（numpy 化的框）、类别名，再用 `box_np_ops.points_in_rbbox(points, gt_boxes_3d)` 得到点-框归属矩阵。
- [tools/dataset_converters/create_gt_database.py:L296-L309](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/dataset_converters/create_gt_database.py#L296-L309) — `gt_points = points[point_indices[:, i]]` 抠出第 i 个目标的点，`gt_points[:, :3] -= gt_boxes_3d[i, :3]` 把坐标平移到框中心（**注意只平移前 3 列，强度等属性列原样保留**），然后 `gt_points.tofile(f)` 存 `.bin`。

**db_info 记录的字段。** 每个目标一条：

- [tools/dataset_converters/create_gt_database.py:L311-L334](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/dataset_converters/create_gt_database.py#L311-L334) — 记录 `name`（类别名）、`path`（相对 `{info_prefix}_gt_database` 的路径）、`image_idx`/`gt_idx`（溯源用）、`box3d_lidar`（建库帧的绝对 7 维框）、`num_points_in_gt`（这个目标有几个点，**这是后面 `min_points` 过滤的依据**）、`difficulty`、`group_id`。最后按类别名分桶收集进 `all_db_infos`。

**落盘。**

- [tools/dataset_converters/create_gt_database.py:L336-L340](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/dataset_converters/create_gt_database.py#L336-L340) — 打印每类条数并把 `all_db_infos` pickle 成 `{info_prefix}_dbinfos_train.pkl`。注意顶层结构是 **「类别名 → 该类目标的 db_info 列表」** 的字典，而不是平铺列表。

**并行版 `GTDatabaseCreater`（Waymo 专用）。**

- [tools/dataset_converters/create_gt_database.py:L403-L504](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/dataset_converters/create_gt_database.py#L403-L504) — `create_single` 处理单帧，逻辑与串行版主循环一致。
- [tools/dataset_converters/create_gt_database.py:L609-L639](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/dataset_converters/create_gt_database.py#L609-L639) — 用 `track_parallel_progress` 多进程跑 `create_single`，再合并各 worker 的结果并**全局重排 group_id**（各 worker 的局部编号会撞，需要加偏移）。
- [tools/create_data.py:L245-L252](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/create_data.py#L245-L252) — Waymo 走并行版（数据量大），`num_worker=workers`。

两个值得注意的源码事实（阅读源码时要诚实对待遗留代码）：

- `create_groundtruth_database` 签名里的 `relative_path`、`add_rgb`、`lidar_only`、`bev_only`、`coors_range` 等形参在函数体内**并未被使用**（见 [tools/dataset_converters/create_gt_database.py:L111-L124](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/dataset_converters/create_gt_database.py#L111-L124) 与函数体对照），是历史遗留参数。`db_info['path']` 实际永远存相对路径（L293），在线侧由 `data_root + path` 拼接还原。

#### 4.1.4 代码实践

**实践目标**：不需要跑通整个建库，先靠「读命令 + 读文件」建立对产物的具体认识。

1. **操作步骤**：
   - 阅读 [tools/create_data.py:L35-L53](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/create_data.py#L35-L53)，写出为 KITTI 建库的完整命令（含 GT 数据库那一步）：

     ```bash
     python tools/create_data.py kitti \
         --root-path ./data/kitti \
         --out-dir ./data/kitti \
         --extra-tag kitti
     ```

   - 如果本地已有 KITTI 数据：跑完后 `ls data/kitti/kitti_gt_database | head` 看文件命名规律，`ls data/kitti/kitti_gt_database | wc -l` 数目标总数，并与日志里 `load N Car database infos` 等各行数字对照。
   - 用下面的脚本读 dbinfos（示例代码，本地运行）：

     ```python
     import pickle
     with open('data/kitti/kitti_dbinfos_train.pkl', 'rb') as f:
         db = pickle.load(f)
     print(type(db), list(db.keys()))          # 顶层：类别名 → 列表
     car = db['Car'][0]
     print(car['name'], car['path'], car['num_points_in_gt'])
     print(car['box3d_lidar'])                 # 7 个数：绝对雷达坐标
     ```

2. **需要观察的现象**：顶层键是类别名；`Car` 的条目通常远多于 `Pedestrian`；`box3d_lidar` 的 z 分量是负数或接近 0（雷达系下地面附近）。
3. **预期结果**：能手工画出「一帧 → N 个目标 → N 个 `.bin` + N 条 db_info」的对应关系。
4. 若本地暂无 KITTI 数据，可以只做源码阅读部分，并观察仓库自带的微型库：`tests/data/waymo/kitti_format/waymo_gt_database/0_Car_0.bin` 与 `waymo_dbinfos_train.pkl`（仅 1 个 Car，616 字节）。其字段结构与上完全一致——**待本地验证**（该微型文件是手工裁剪的，点云维度请先按文件大小推断再 reshape 验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `.bin` 里的点要减去框中心坐标存，而不是存绝对坐标？

**答案**：素材库里的目标要贴到**任意**新位置。存相对坐标后，`box3d_lidar` 决定「贴到哪」，点云只需平移同一个偏移量即可；如果存绝对坐标，每次粘贴还得先减去建库时的旧位置再加新位置，等价但多一步，且容易在多次变换中引入误差。

**练习 2**：`num_points_in_gt` 是在建库时算的。为什么不在训练时现场数？

**答案**：现场数需要对每个 db 目标重新做一次 `points_in_rbbox`，而建库时这个矩阵本来就算过了（L257），顺手记录成标量成本为零。这是「converter 离线算派生量、在线轻取」契约的又一例。

**练习 3**：KITTI 建库为什么用 `velodyne_reduced` 而不是原始 `velodyne`？

**答案**：`velodyne_reduced` 是去掉了训练感知范围外点的裁剪版点云（u3-l2 讲过，训练配置硬依赖它）。建库与训练用同一份点云，保证「库里的目标点云形态」与「训练时当前帧的形态」分布一致。

### 4.2 采样器构建：dbinfos 加载、prepare 过滤与 BatchSampler

#### 4.2.1 概念说明

`DataBaseSampler` 是在线侧的「仓库管理员」。它在**初始化时**（数据集构建管线时，只发生一次，每个 worker 进程各有一份）做三件事：

1. 加载 dbinfos pkl；
2. 执行 `prepare` 里声明的过滤函数，把质量差的目标剔除；
3. 为每个类别建一个 `BatchSampler`（洗好牌的队列）。

**`min_points` 过滤就在初始化时生效**——它是一个「入库门槛」，不是每次采样时的动态条件。点太少的目标（比如只有两三个反射点的远处行人）贴进场景只会引入噪声监督，所以直接不让它进库。

`BatchSampler` 解决的问题是：同一帧里可能要连续抽好几个同类目标，希望「一轮之内尽量不重复抽样，抽完一轮再重洗」。这就是一个无放回的洗牌索引队列。

#### 4.2.2 核心流程

`BatchSampler` 的状态机：

```text
初始化: indices = shuffle([0..N-1])   # 洗牌
_sample(num):
  若 剩余数量 <= num:  返回剩余全部 → reset(重新洗牌, idx=0)
  否则:                返回 indices[idx:idx+num], idx += num
```

注意一个细节：条件是 `self._idx + num >= self._example_num`（**大于等于**），即剩余数量恰好等于 `num` 时也会触发 reset 并只返回剩余部分——不会跨界拼接两轮的元素。

`DataBaseSampler.__init__` 的构建链：

```text
读 pkl → db_infos
  ↓ 对 prepare 中每个 (函数名, 参数): db_infos = getattr(self, 函数名)(db_infos, 参数)
  ↓ sample_groups → sample_classes / sample_max_nums 两个平行列表
  ↓ 每个类别建 BatchSampler(db_infos[类], shuffle=True)
```

#### 4.2.3 源码精读

**配置长什么样（先看用法再看实现）。** PointPillars KITTI 配置里的 `db_sampler`：

- [configs/pointpillars/pointpillars_hv_secfpn_8xb6-160e_kitti-3d-3class.py:L15-L30](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/pointpillars/pointpillars_hv_secfpn_8xb6-160e_kitti-3d-3class.py#L15-L30) — `info_path` 指向 dbinfos，`rate=1.0`（名额按 100% 兑现），`prepare` 声明两个过滤函数：`filter_by_difficulty=[-1]`（剔除难度 -1 的目标）与 `filter_by_min_points=dict(Car=5, Pedestrian=5, Cyclist=5)`（每类至少 5 个点），`sample_groups=dict(Car=15, Pedestrian=15, Cyclist=15)` 是每类每帧的粘贴上限，`points_loader` 复用 `LoadPointsFromFile` 读 4 维 `.bin`。

**prepare 的分发机制：函数名反射调用。**

- [mmdet3d/datasets/transforms/dbsampler.py:L124-L141](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/dbsampler.py#L124-L141) — 读 pkl 后，`for prep_func, val in prepare.items(): db_infos = getattr(self, prep_func)(db_infos, val)`。也就是说 `prepare` 字典的键**必须与类上的静态方法同名**，这是一种轻量的插件式约定：想加新过滤条件，给类加一个静态方法即可在配置里启用。

**两个内置过滤函数。**

- [mmdet3d/datasets/transforms/dbsampler.py:L161-L178](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/dbsampler.py#L161-L178) — `filter_by_difficulty`：剔除 `info['difficulty']` 在黑名单里的条目（KITTI 难度 -1 表示无有效难度信息）。
- [mmdet3d/datasets/transforms/dbsampler.py:L180-L200](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/dbsampler.py#L180-L200) — `filter_by_min_points`：对每个类，`if min_num > 0` 才过滤，保留 `info['num_points_in_gt'] >= min_num` 的条目。**这就是本讲实践任务要指认的 min_points 生效位置：初始化阶段、一次性、按类阈值。** 注意 `min_num=0` 意味着不过滤——后面实践会用到这个特性。

**sample_groups 解析与每类 BatchSampler。**

- [mmdet3d/datasets/transforms/dbsampler.py:L143-L158](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/dbsampler.py#L143-L158) — `sample_groups=dict(Car=15)` 被拆成平行列表 `sample_classes=['Car']`、`sample_max_nums=[15]`；再对 `db_infos` 里**实际存在**的每个类建 `BatchSampler`。注意：如果某个类被过滤到一条不剩，这里仍会建一个空队列的 sampler（后续 `sample_class_v2` 里 `np.stack` 空列表会报错）——配置里声明的类应确保过滤后非空。

**BatchSampler 本体。**

- [mmdet3d/datasets/transforms/dbsampler.py:L15-L79](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/dbsampler.py#L15-L79) — `_sample` 实现上面状态机：[L44-L59](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/dbsampler.py#L44-L59) 是核心，`_reset` 在 [L61-L67](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/dbsampler.py#L61-L67) 重洗牌。`sample(num)`（[L69-L79](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/dbsampler.py#L69-L79)）返回条目本身而非索引。

#### 4.2.4 代码实践

**实践目标**：用纯 Python 验证 `BatchSampler` 的无放回/重洗牌行为，不依赖任何数据。

1. **操作步骤**（示例代码，本地运行）：

   ```python
   import numpy as np
   from mmdet3d.datasets.transforms.dbsampler import BatchSampler

   np.random.seed(0)
   samples = [{'id': i} for i in range(5)]
   bs = BatchSampler(samples, name='Car', shuffle=True)

   print(bs.sample(2))   # 第 1 次抽 2 个
   print(bs.sample(2))   # 第 2 次抽 2 个（与第 1 次不重叠）
   print(bs.sample(2))   # 只剩 1 个 → 返回 1 个并触发 reset
   print(bs.sample(2))   # 重洗牌后再抽
   ```

2. **需要观察的现象**：前两次抽到的 `id` 互不重复；第三次只剩一个；第四次开始新的一轮。
3. **预期结果**：输出形如 `[{'id': 2}, {'id': 4}] / [{'id': 1}, {'id': 3}] / [{'id': 0}] / ...`（具体顺序取决于随机种子），能解释「为什么 GT-Aug 用队列而不是每次 `np.random.choice`」——避免同一帧反复抽到同一个目标，也让库被均匀消费。
4. 本实践只依赖 numpy，可直接运行验证。

#### 4.2.5 小练习与答案

**练习 1**：把 PointPillars 配置里的 `filter_by_min_points` 从 `dict(Car=5, ...)` 改成 `dict(Car=0, ...)`，行为变了吗？

**答案**：变了。`min_num > 0` 才过滤（L194），所以 `Car=0` 等于对 Car 完全不过滤，所有 Car 条目（包括只有 1 个点的）都进库。`min_num` 只能起到「提高门槛」的作用，不能用来表达「必须恰好 N 个点」。

**练习 2**：`prepare=dict(filter_by_difficulty=[-1])` 中的 `-1` 从哪来？

**答案**：来自建库时记录的 `db_info['difficulty']`（create_gt_database.py L319），而它又源自 KITTI 标注的 difficulty 字段（-1 表示未标注难度/无效）。过滤它是为了让粘贴目标都是「真实可学的目标」。

**练习 3**：为什么 `DataBaseSampler` 里要同时保存 `cat2label` 和 `label2cat` 两张映射表？

**答案**：dbinfos 里 `name` 是类别名字符串，而训练管线里 `gt_labels_3d` 是整数标签。`cat2label` 用于把采样到的 `name` 转回标签（sample_all L282），`label2cat` 提供反向查询。`classes` 参数必须与数据集配置的 `metainfo.classes` 一致，否则标签编号会错位。

### 4.3 sample_all：名额计算、碰撞测试与平移贴回

#### 4.3.1 概念说明

`sample_all` 是每次 `ObjectSample.transform` 都会调用的在线入口，它回答四个问题：

1. **贴几个？**——按类计算名额：配置上限减去当前帧已有的该类数量，再乘 `rate`。
2. **贴哪个？**——从该类的 `BatchSampler` 抽，抽到后做**碰撞测试**：新框不许与现有 gt 框、已粘贴框在 BEV 下重叠。
3. **点从哪来？**——用 `points_loader` 读该目标自己的 `.bin`，再平移 `box3d_lidar[:3]`（把「库内相对坐标」还原成「场景绝对坐标」）。
4. **贴在地上吗？**——若给了地面平面（KITTI 的 `plane`），按平面方程校正 z，避免目标悬空或入地。

碰撞测试是 GT-Aug 正确性的关键：如果贴进来的车与真实存在的车重叠，一个点就会同时属于两个框，监督信号自相矛盾。

#### 4.3.2 核心流程

```text
sample_all(gt_bboxes, gt_labels, img=None, ground_plane=None):
  # ① 名额
  对每个 (class_name, max_num):
      n = round(rate * (max_num - 当前帧该类已有个数))      # 已够数则不贴
  # ② 逐类采样 + 碰撞
  avoid_coll_boxes = gt_bboxes                              # 初始障碍 = 现有 gt
  对每个有名额的类:
      sampled_cls = sample_class_v2(类名, n, avoid_coll_boxes)
      avoid_coll_boxes = concat(avoid_coll_boxes, 本轮成功粘贴的框)  # 后贴的让先贴的
  # ③ 还原点云位置
  对每个成功样本: 读 .bin → points.translate(box3d_lidar[:3])
  # ④ 地面校正（可选）
  dz = a·x + b·y + c·z + d     （当前帧平面方程在框中心处的值）
  框 z 与点 z 同减 dz
  # ⑤ 打包返回
  {gt_labels_3d, gt_bboxes_3d, points(cat), group_ids}
```

碰撞判定在 BEV（鸟瞰图）下做，数学上是对两个旋转矩形做**分离轴测试（SAT）**：两个凸多边形不相交，当且仅当存在一条轴（取两个矩形各自四条边的法向）使两者在该轴上的投影不重叠。`box_collision_test` 先用「外接正矩形快速排除」再逐对做 SAT，降低计算量。

#### 4.3.3 源码精读

**① 名额计算。**

- [mmdet3d/datasets/transforms/dbsampler.py:L226-L237](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/dbsampler.py#L226-L237) — `sampled_num = int(max_sample_num - 当前帧该类 gt 数)`，再 `np.round(self.rate * sampled_num)`。语义是**「补到上限」**：某帧已有 12 辆车、上限 15，就只补 3 辆；已超上限则不补。`rate` 是名额兑现率（0.5 表示每帧最多贴一半名额），常用于控制增强强度。

**② 逐类采样与障碍累积。**

- [mmdet3d/datasets/transforms/dbsampler.py:L239-L260](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/dbsampler.py#L239-L260) — `avoid_coll_boxes` 初始为当前帧 gt，每类粘贴成功后把新框并入障碍集合，**先贴的优先，后贴的让位**。

**③ 读点与平移还原。**

- [mmdet3d/datasets/transforms/dbsampler.py:L268-L280](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/dbsampler.py#L268-L280) — 对每个样本拼 `data_root + info['path']`，构造 `dict(lidar_points=dict(lidar_path=...))` 喂给 `self.points_loader`（正是 u3-l5 讲过的 `LoadPointsFromFile` 的 v2 输入契约），拿到 `LiDARPoints` 后 `s_points.translate(info['box3d_lidar'][:3])` 把相对坐标还原到建库时的绝对位置。**注意：粘贴位置 = 建库时的原位置**，本实现不主动换位，靠碰撞测试保证合法性。

**④ 地面校正。**

- [mmdet3d/datasets/transforms/dbsampler.py:L285-L291](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/dbsampler.py#L285-L291) — 平面系数 \((a,b,c,d)\) 满足平面方程 \(a x + b y + c z + d = 0\)，代码算 \(dz = a x + b y + c z + d\)（即该点相对平面的有符号高度），然后框的 z 与每个点的 z 同减 dz，把目标「压」到当前帧的地面上。

**⑤ 返回值。**

- [mmdet3d/datasets/transforms/dbsampler.py:L293-L305](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/dbsampler.py#L293-L305) — 返回四个键：标签、7 维框数组、`s_points_list[0].cat(s_points_list)` 拼接后的点对象、`group_ids`（从 gt 数量开始连续编号，标记「哪些框是本帧粘贴的」）。

  一个诚实的观察：[mmdet3d/datasets/transforms/transforms_3d.py:L430-L433](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/transforms_3d.py#L430-L433) 中 `ObjectSample` 只消费了前三个键，`group_ids` 在主库管线中**未被使用**，属于留给扩展（如按组控制重复粘贴）的接口。

**碰撞测试 sample_class_v2。**

- [mmdet3d/datasets/transforms/dbsampler.py:L307-L345](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/dbsampler.py#L307-L345) — 完整流程：
  - L319-L320：从该类 `BatchSampler` 抽 `num` 条并 `deepcopy`（防止后续修改污染库）。
  - L323-L333：用 `box_np_ops.center_to_corner_box2d`（定义见 [mmdet3d/structures/ops/box_np_ops.py:L97-L109](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/ops/box_np_ops.py#L97-L109)）把「中心 + 尺寸 + 角」表示转成 BEV 四角点，gt 与采样框拼成 `total_bv`。
  - L334-L336：`data_augment_utils.box_collision_test(total_bv, total_bv)` 得 \((N+M, N+M)\) 布尔碰撞矩阵，对角线置 False（自己不算撞自己）。
  - L338-L344：按顺序检查每个采样框：若与任何存活框碰撞 → **丢弃它并把它的行列清零**（于是后面只与它相撞、与其他框不撞的样本得以保留）；否则收进 `valid_samples`。

- [mmdet3d/datasets/transforms/data_augment_utils.py:L32-L61](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/data_augment_utils.py#L32-L61) — 碰撞测试本体：先算每个旋转矩形的轴对齐外接矩形 `standup`，投影区间不重叠（`iw <= 0` 或 `ih <= 0`）直接判不撞；否则逐边做分离轴判定。

**配套的 points_in_rbbox。** 在线侧「移除被粘贴框罩住的原有点云」也用它（4.4 节），定义在：

- [mmdet3d/structures/ops/box_np_ops.py:L354-L366](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/structures/ops/box_np_ops.py#L354-L366) — 输入 `(N, 3+dim)` 的点与 `(M, 7)` 的框，返回逐点逐框布尔矩阵；`z_axis` 与 `origin` 参数适配不同坐标系的框约定。

#### 4.3.4 代码实践

**实践目标**：手工构造一个 2 条目的迷你 GT 数据库，真实跑通 `sample_all`，亲眼看到「名额计算 → 碰撞丢弃 → 平移还原」。

1. **操作步骤**（示例代码，保存为脚本本地运行；不依赖任何公开数据集）：

   ```python
   import os, pickle, numpy as np

   # ---------- ① 自造迷你 GT 数据库 ----------
   root = '/tmp/mini_db'
   os.makedirs(f'{root}/mini_gt_database', exist_ok=True)

   def make_car_bin(path, n=30, seed=0):
       rng = np.random.default_rng(seed)
       pts = rng.uniform(-1.5, 1.5, size=(n, 3))   # 车内相对坐标（以框中心为原点）
       intens = np.full((n, 1), 0.5)               # 第 4 列强度
       np.concatenate([pts, intens], axis=1).astype(np.float32).tofile(path)

   make_car_bin(f'{root}/mini_gt_database/000001_Car_0.bin', seed=1)
   make_car_bin(f'{root}/mini_gt_database/000002_Car_0.bin', seed=2)

   db = {'Car': [
       dict(name='Car', path='mini_gt_database/000001_Car_0.bin',
            image_idx='000001', gt_idx=0,
            box3d_lidar=np.array([10., 0., -1., 4., 2., 1.5, 0.]),   # 贴这里会撞
            num_points_in_gt=30, difficulty=0, group_id=0),
       dict(name='Car', path='mini_gt_database/000002_Car_0.bin',
            image_idx='000002', gt_idx=0,
            box3d_lidar=np.array([50., 20., -1., 4., 2., 1.5, 0.]),  # 贴这里安全
            num_points_in_gt=30, difficulty=0, group_id=1),
   ]}
   with open(f'{root}/mini_dbinfos.pkl', 'wb') as f:
       pickle.dump(db, f)

   # ---------- ② 构造采样器 ----------
   from mmdet3d.datasets.transforms.dbsampler import DataBaseSampler
   sampler = DataBaseSampler(
       info_path=f'{root}/mini_dbinfos.pkl',
       data_root=root,
       rate=1.0,
       prepare=dict(filter_by_min_points=dict(Car=0)),   # 0 = 不过滤，保证库非空
       sample_groups=dict(Car=2),
       classes=['Car', 'Pedestrian'],                     # 采样类必须在内
       points_loader=dict(type='LoadPointsFromFile',
                          coord_type='LIDAR', load_dim=4, use_dim=[0, 1, 2, 3]))

   # ---------- ③ 当前帧：在 (10, 0) 处已有一个行人（不是 Car）----------
   # 故意用 Pedestrian：名额按类算，Car 名额 = 2 - 0 = 2，
   # 两条库记录都会被抽出，结论不依赖洗牌顺序
   gt_bboxes = np.array([[10., 0., -1., 4., 2., 1.5, 0.]])
   gt_labels = np.array([1])            # 1 = Pedestrian（见 classes 顺序）
   ret = sampler.sample_all(gt_bboxes, gt_labels)

   print('粘贴目标数:', 0 if ret is None else len(ret['gt_labels_3d']))
   print('粘贴框:\n', None if ret is None else ret['gt_bboxes_3d'])
   print('粘贴点云前 3 个点:\n', None if ret is None else ret['points'].tensor[:3])
   ```

2. **需要观察的现象**：名额计算给出 2（上限 2 减当前帧 Car 数 0）；两条库记录都被抽出；但 `000001_Car_0` 的目标位置与现有 gt（那个行人）在 BEV 下完全重叠，被碰撞测试丢弃——**碰撞判定只看几何重叠，不看类别**；最终只粘贴 1 个目标，其框中心在 `(50, 20, -1)`，且点云坐标已平移到该位置附近（`x ≈ 50±1.5, y ≈ 20±1.5`）。
3. **预期结果**：`粘贴目标数: 1`，`ret['gt_bboxes_3d']` 形状 `(1, 7)`，点云约 30 个点、坐标已还原为绝对坐标。若把当前帧 gt 的位置改到 `(80, 80)`（与两个素材都不撞），则应粘贴 2 个目标；若把它改成 `gt_labels=np.array([0])`（Car），名额降为 1，结果就取决于抽到哪条记录——这正好演示了「补到上限」的按类名额语义。
4. **环境提示（重要）**：[mmdet3d/datasets/transforms/dbsampler.py:L282-L283](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/dbsampler.py#L282-L283) 使用了 `np.long`，该别名在 numpy 1.24 中已被移除；若你的 numpy 较新，本实践会在该行报 `AttributeError`，需临时降级 numpy 或本地改用 `np.int64` 验证——**待本地验证（取决于 numpy 版本）**。

#### 4.3.5 小练习与答案

**练习 1**：`rate` 从 1.0 降到 0.5，直觉上训练会发生什么变化？

**答案**：每帧每类的粘贴名额减半（`round(0.5 × (上限 − 已有))`），增强强度下降，训练分布更接近原始数据。GT-Aug 是把「长尾类的出现频率」强行拉平的手段，`rate` 变小意味着拉平力度变小，小样本类（行人、骑行者）的召回可能下降。

**练习 2**：为什么碰撞矩阵里被丢弃的框要「把自己的行和列都清零」？

**答案**：一个采样框被丢弃后，它不再占据空间。若只清行不清列，后续框与这个「已不存在」的框碰撞仍会被误杀，导致粘贴数量低于必要水平。清零行列等价于「把它从场景里真正移走」。

**练习 3**：粘贴目标是按 `box3d_lidar` 原位置贴回的。那么「目标位置多样性」从哪里来？

**答案**：来自数据库本身条目的多样性（不同帧、不同位置的目标）加上随机抽取；再往后还靠管线下游的 `GlobalRotScaleTrans`/`RandomFlip3D`（u3-l6）对整帧做全局几何增强。这套实现刻意不做「把目标挪到随机位置再贴」，因为目标与地面、周边环境的空间关系是真实分布的一部分，硬挪容易产生物理上不合理的样本。

### 4.4 ObjectSample.transform：合入当前帧，以及 anchors 对应关系的真相

#### 4.4.1 概念说明

`ObjectSample` 是管线里的那个 transform（`db_sampler` 是它的一个参数，二者不要混淆：**`ObjectSample` 是「什么时候做」，`DataBaseSampler` 是「怎么做」**）。它拿到 `sample_all` 的结果后做三件事：

1. 标签拼接：`gt_labels_3d = concat(原标签, 粘贴标签)`；
2. 框重建：用 `gt_bboxes_3d.new_box(concat(原框, 粘贴框))` 生成新的框对象；
3. 点云「先删后拼」：**先删掉**落在粘贴框内部的当前帧原有点（防止原场景的点被误算作新目标的点），再把粘贴目标点云与剩余点云 `cat` 起来。

最后回答学习目标 3 的经典问题：「贴了新 gt，anchors 怎么办？」——答案是**在这个版本的架构里根本不需要修**，原因见 4.4.2。

#### 4.4.2 核心流程

```text
ObjectSample.transform(input_dict):
  取 gt_bboxes_3d / gt_labels_3d / points（/ plane）
  sampled_dict = db_sampler.sample_all(...)
  若采到了：
    gt_labels_3d ← concat(旧标签, 新标签)
    gt_bboxes_3d ← new_box(concat(旧框numpy, 新框))
    points ← remove_points_in_boxes(points, 新框)     # 删掉新框内的原有点
    points ← cat(粘贴点云, 剩余点云)                    # 粘贴点放前面
  写回三个键，返回
```

**为什么 anchors 不需要修复**：在 SECOND 的原始实现里，anchors 是预先按场景算好、随样本一起在数据 dict 里流转的，粘贴目标后必须同步更新 anchors 和正负样本标记。而在 mmdet3d 1.x 的配置驱动架构里，数据管线的出口（`Pack3DDetInputs` 的 `keys`）只有 `['points', 'gt_labels_3d', 'gt_bboxes_3d']`（见 [configs/pointpillars/pointpillars_hv_secfpn_8xb6-160e_kitti-3d-3class.py:L51-L52](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/pointpillars/pointpillars_hv_secfpn_8xb6-160e_kitti-3d-3class.py#L51-L52)），**anchors 不在数据流里**。anchors 由检测头在每次 forward 算 loss 时根据当前特征图尺寸现场生成，随后 assigner 对「本 batch 的 gt（已含粘贴目标）」重新做正负分配。也就是说，「anchor↔gt 对应关系」从「管线维护的持久状态」变成了「每次前向即时计算的临时结果」，粘贴新 gt 之后它自动就对了——修复动作被架构性消解了。

#### 4.4.3 源码精读

**注册与构建。**

- [mmdet3d/datasets/transforms/transforms_3d.py:L327-L370](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/transforms_3d.py#L327-L370) — `@TRANSFORMS.register_module()` 注册 `ObjectSample`。注意 [L367-L369](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/transforms_3d.py#L367-L369)：若配置里没写 `type`，自动补 `type='DataBaseSampler'`——这解释了为什么 PointPillars 配置的 `db_sampler` 字典里**不需要写 type**。`self.disabled = False` 是给 `DisableObjectSampleHook` 用的开关（见本节末尾）。

**地面平面断言。**

- [mmdet3d/datasets/transforms/transforms_3d.py:L406-L411](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/transforms_3d.py#L406-L411) — `use_ground_plane=True` 时断言 `input_dict['plane']` 存在。`plane` 正是 u3-l3 里讲过的 KITTI 独有字段（由 `KittiDataset.parse_data_info` 注入 info、随 data dict 流转），PointPillars 配置 [L41](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/pointpillars/pointpillars_hv_secfpn_8xb6-160e_kitti-3d-3class.py#L41) 开启了它。nuScenes 没有平面文件，其配置不开启。

**调用采样器。**

- [mmdet3d/datasets/transforms/transforms_3d.py:L424-L428](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/transforms_3d.py#L424-L428) — 把框 numpy 化后调 `sample_all`。

  这里有一个必须诚实指出的源码事实：`sample_2d=True` 的分支（[L414-L422](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/transforms_3d.py#L414-L422)）调用 `sample_all(..., gt_bboxes_2d=..., img=...)`，但主库 `DataBaseSampler.sample_all` 的签名（[dbsampler.py:L202-L206](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/dbsampler.py#L202-L206)）**不接受 `gt_bboxes_2d` 参数**——即主库采样器不支持 2D 图块粘贴，`sample_2d=True` 需要自行提供扩展版的采样器才能工作。

**合并框与标签。**

- [mmdet3d/datasets/transforms/transforms_3d.py:L435-L438](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/transforms_3d.py#L435-L438) — 标签直接 `np.concatenate`；框先 numpy 化拼接，再用 `gt_bboxes_3d.new_box(...)` 重建为与原来同类的框对象（u2-l2 讲过 `new_box` 是框家族的配套工厂方法，这里保证 LiDAR 系子类不变）。

**先删后拼。**

- [mmdet3d/datasets/transforms/transforms_3d.py:L440-L442](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/transforms_3d.py#L440-L442) — 先 `remove_points_in_boxes`（实现在 [L373-L387](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/transforms_3d.py#L373-L387)：`points_in_rbbox` 得掩码后布尔索引取反），再 `points.cat([sampled_points, points])`。**删除是必要的**：粘贴框的位置本来就可能罩住当前帧的一些背景点或别的目标的点，不删的话这些点会同时落在两个 gt 框里，点级监督与框级监督互相矛盾。
- [mmdet3d/datasets/transforms/transforms_3d.py:L452-L454](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/transforms_3d.py#L452-L454) — 写回 `gt_bboxes_3d`、`gt_labels_3d`（转 `int64`）、`points`。

**anchors 在 loss 时现算的证据。**

- [mmdet3d/models/dense_heads/anchor3d_head.py:L390-L403](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/anchor3d_head.py#L390-L403) — `loss_by_feat` 先从 `cls_scores` 取 `featmap_sizes`，再 `anchor_list = self.get_anchors(featmap_sizes, batch_input_metas, ...)`，接着 `anchor_target_3d`（内部走 u5-l6 将讲的 assigner/sampler）对 batch 内全部 gt 生成回归目标。anchors 与 gt 的对应关系**每一次前向都重新建立**。
- [mmdet3d/models/dense_heads/anchor3d_head.py:L221-L241](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/models/dense_heads/anchor3d_head.py#L221-L241) — `get_anchors` 用 `self.prior_generator.grid_anchors` 按特征图网格铺 anchor（u1-l5 讲过配置里 `prior_generator=Anchor3DRangeGenerator` 那一层）。

**`disabled` 开关与训练后期的「关增强」。**

- [mmdet3d/datasets/transforms/transforms_3d.py:L400-L401](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/transforms_3d.py#L400-L401) — `if self.disabled: return input_dict`，直接跳过采样。
- [mmdet3d/engine/hooks/disable_object_sample_hook.py:L37-L54](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/engine/hooks/disable_object_sample_hook.py#L37-L54) — `DisableObjectSampleHook` 在第 `disable_after_epoch` 个 epoch 开始时遍历管线，把所有 `ObjectSample` 的 `disabled` 置 True；若 dataloader 开了 `persistent_workers`，还要强制重启 worker 才能让改动生效（注释里也自嘲了这是 hacky 做法）。官方文档把它作为自定义 Hook 的教学示例（`docs/en/advanced_guides/customize_runtime.md`），通过 `custom_hooks=[dict(type='DisableObjectSampleHook', disable_after_epoch=15)]` 挂载——训练后期关掉 GT-Aug、让模型回到真实分布上收敛，是常见调参手段。

#### 4.4.4 代码实践

本讲实践任务的核心就在这里：**阅读 `ObjectSample.transform`，画出采样一个 Car 目标的完整流程图，并标注 `min_points` 的生效位置**。

1. **操作步骤**：
   - 对照 4.4.3 的源码链接通读一遍 `transform`，然后画出下面的流程图（用你自己的工具，Mermaid/纸笔均可）：

     ```text
     【离线·一次性】create_groundtruth_database
        训练帧 → 抠框内点(相对坐标) → xxx.bin + dbinfos
                                          │
        ┌─────────────── filter_by_min_points 在这里生效（DataBaseSampler.__init__，入库门槛）
        ▼
     【在线·每个 worker 初始化一次】DataBaseSampler
        读 pkl → prepare 过滤 → 每类 BatchSampler
                                          │
     【在线·每帧】ObjectSample.transform
        当前帧 (points, gt_bboxes_3d, gt_labels_3d)
          → sample_all:
              名额 = round(rate × (上限 − 帧内已有))      ① 选名额
              BatchSampler 抽条目 → BEV 碰撞测试 → valid    ② 选样本
              读 .bin → translate(box3d_lidar[:3])         ③ 拼接点
              (可选) 地面平面校正 z                          ④
          → new_box(concat(旧框, 新框))                     ⑤ 平移框(合入)
          → remove_points_in_boxes + points.cat            ⑥ 先删后拼
          → 管线后续: RandomFlip3D / GlobalRotScaleTrans / RangeFilter
          → Pack3DDetInputs → Det3DDataSample
          → 模型 forward: get_anchors(featmap) + assigner   ⑦ "修 anchors"＝无需修，重建
     ```

   - 在图中明确标出：`min_points` 过滤发生在** DataBaseSampler 初始化**（[dbsampler.py:L180-L200](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/transforms/dbsampler.py#L180-L200)），依据是建库时写下的 `num_points_in_gt` 字段（[create_gt_database.py:L318](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/dataset_converters/create_gt_database.py#L318)）。
   - 用 4.3.4 的迷你数据库进一步做端到端验证（示例代码，本地运行）：

     ```python
     import torch, numpy as np
     from mmdet3d.datasets.transforms import ObjectSample
     from mmdet3d.structures import LiDARInstance3DBoxes, LiDARPoints

     obj_sample = ObjectSample(
         db_sampler=dict(                       # 复用 4.3.4 自造的迷你库
             data_root='/tmp/mini_db',
             info_path='/tmp/mini_db/mini_dbinfos.pkl',
             rate=1.0, prepare=dict(filter_by_min_points=dict(Car=0)),
             sample_groups=dict(Car=2), classes=['Car', 'Pedestrian']),
         use_ground_plane=False)                # 迷你库没有 plane，必须关

     input_dict = dict(
         points=LiDARPoints(torch.zeros(0, 4)),                     # 空场景点云
         gt_bboxes_3d=LiDARInstance3DBoxes(torch.zeros(0, 7)),     # 空 gt
         gt_labels_3d=np.zeros(0, dtype=np.int64))
     out = obj_sample.transform(input_dict)
     print('框数:', out['gt_bboxes_3d'].tensor.shape)
     print('标签:', out['gt_labels_3d'])
     print('点数:', out['points'].tensor.shape)
     ```

2. **需要观察的现象**：空场景下名额为 2、两个素材都不与空 gt 碰撞，于是两个目标都被粘贴；`gt_bboxes_3d` 变为 `(2, 7)`，`gt_labels_3d` 为 `[0, 0]`，点数约 60；`remove_points_in_boxes` 在空点云上删不掉任何点（可观察其不变）。
3. **预期结果**：流程图中 ①~⑦ 各步都能对应到一条已读过的源码行；迷你实验输出与上述一致。
4. 若在空场景实践中遇到 `np.long` 报错，处理方式同 4.3.4 的环境提示——**待本地验证（取决于 numpy 版本）**。

#### 4.4.5 小练习与答案

**练习 1**：`points.cat([sampled_points, points])` 把粘贴点放在拼接列表的前面。这有讲究吗？

**答案**：对训练本身无实质影响（后续 `PointShuffle` 会打乱顺序，且模型对点序不敏感），但它是一个稳定约定：粘贴目标集中在点云数组头部，便于调试时快速定位「哪些点是贴进来的」。反过来说，下游若做逐点分割类监督（如 seg 分支），保持「删旧点在前、拼新点在后」的一致顺序才能让掩码对得上。

**练习 2**：如果 `ObjectSample` 不是放在增强链最前端，而是放在 `GlobalRotScaleTrans` 之后，会有什么问题？

**答案**：两点。其一，粘贴目标使用的是**原始雷达系**的 `box3d_lidar`，而当前帧已经整体旋转缩放过，粘贴位置与变换后的场景不再对齐（例如目标可能贴到已经旋转出感知范围的位置），需要额外做逆变换。其二，管线末端的范围过滤（`PointsRangeFilter`/`ObjectRangeFilter`，u3-l6 讲过必须放最后）与 GT-Aug 的先后关系会被打乱，粘贴动作可能把目标放到过滤之后又超界的位置。放在最前端让粘贴目标接受与原场景完全相同的后续增强，是最省事也最一致的设计。

**练习 3**：为什么 `DisableObjectSampleHook` 要特殊处理 `persistent_workers=True` 的情况？

**答案**：`persistent_workers` 开启时，DataLoader 的 worker 进程在整个训练期间存活，pipeline 对象是在 worker 进程里被持续使用的副本。主进程里改 `transform.disabled=True` 不会自动同步到已存活的 worker。Hook 通过把 dataloader 的初始化标志置 False 强制重建 worker，代价是重建开销——源码注释也承认这是「very hacky approach」。

## 5. 综合实践

**任务：把「建库」和「粘贴」两半亲手接起来，验证契约的每一环。**

1. **造数据**：写一个脚本，生成 3 个「虚拟 Car」素材：每个是 `(40, 4)` 的 float32 数组（前 3 列在 `±2m` 内随机、第 4 列为强度），按 `{idx}_Car_0.bin` 存进 `gt_database/`；再生成 `dbinfos.pkl`，其中 3 条 `Car` 记录的 `num_points_in_gt` 分别写 `40`、`3`、`40`，`box3d_lidar` 分别放在 `(10, 0)`、`(30, 0)`、`(50, 0)`（均 z=-1）。
2. **验证过滤**：用 `prepare=dict(filter_by_min_points=dict(Car=5))` 构造 `DataBaseSampler`，确认第 2 条（3 个点）被挡在库外——对比日志中 `load N Car database infos` 过滤前后的数字。
3. **验证名额**：构造「当前帧已有 1 辆车」的输入，`sample_groups=dict(Car=2)`，确认只补 1 个。
4. **验证碰撞**：把当前帧的车放在 `(50, 0)`，确认第 3 条素材被丢弃、只剩 1 个粘贴目标。
5. **验证地面校正**：给 `ObjectSample(use_ground_plane=True)` 传入 `plane=np.array([0, 0, 1, 1.8])`（地平面 \(z = -1.8\)，因方程 \(z + 1.8 = 0\) 对应 `d=1.8`），观察粘贴框与点的 z 是否按 4.3.2 的公式整体减去 `dz`。
6. **产出**：一张覆盖 ①~⑦ 的流程图 + 每步的观察记录表（实验值 vs 预期值）。全程除 numpy/torch 外不依赖任何数据集；数值结论标注「已本地验证」或「待本地验证」。

## 6. 本讲小结

- GT-Aug 分两半：**离线建库**（`create_groundtruth_database`：每目标一个居中坐标的 `.bin` + 一条含绝对 `box3d_lidar`、`num_points_in_gt` 的 db_info）与**在线粘贴**（`ObjectSample` → `DataBaseSampler.sample_all`）。
- 点存相对坐标、框存绝对坐标的「分离」设计，是粘贴可复用的关键；在线侧用 `points_loader`（复用 `LoadPointsFromFile`）读点后 `translate(box3d_lidar[:3])` 还原。
- `min_points` 过滤在 `DataBaseSampler.__init__` 的 `filter_by_min_points` 中**一次性**生效（入库门槛），依据是建库时记下的 `num_points_in_gt`；`rate` 与 `sample_groups` 共同决定每帧每类的粘贴名额（补到上限）。
- 粘贴合法性由 BEV 分离轴碰撞测试保证，先贴优先、后贴让位；`ObjectSample` 合入时对点云「先删（粘贴框内原有点）后拼（粘贴点在前）」。
- **anchors 无需修复**：数据管线不携带 anchors，`Anchor3DHead.loss_by_feat` 每次前向按特征图现算 anchor 并由 assigner 重新分配，粘贴的新 gt 自动进入下一次分配。
- 阅读源码也要看清遗留接口：`group_ids` 返回但未被 `ObjectSample` 消费、`sample_2d=True` 在主库采样器下不可用、`create_groundtruth_database` 签名里存在未使用形参、`np.long` 依赖旧版 numpy。

## 7. 下一步学习建议

- **u3-l8（Pack3DDetInputs）**：顺着本讲结尾看粘贴后的 `gt_bboxes_3d`/`gt_labels_3d`/`points` 如何被打包进 `Det3DDataSample`，完成数据管线三部曲的最后一环。
- **u5-l4（Anchor3DHead 与 3D 锚框）**：本讲 4.4.2 只给出了「anchors 现算」的证据链，anchor 如何按网格铺开、assigner 如何把粘贴目标分配给 anchor，在那一讲展开。
- **u5-l1（VoxelNet/PointPillars 全链路）**：观察 GT-Aug 后的点数上限（`max_points_per_voxel` 等体素化配置）为什么需要为粘贴留余量。
- **延伸阅读**：SECOND 论文（Yan et al., 2018）第 3.2 节是 GT-Aug 的原始出处，可对照理解「点数门槛、难度过滤、碰撞测试」这三件套的论文动机。
