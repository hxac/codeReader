# 数据准备：从原始数据集到 info pkl

## 1. 本讲目标

上一讲（u3-l1）我们学习了 `Det3DDataset` 基类，知道数据集类从 info pkl 中逐条读取 `data_list`，再回调 `parse_data_info` 把「帧信息」加工成训练样本。那么 info pkl 本身是从哪里来的？本讲回答这个问题。学完本讲你应该能够：

1. 说出 `tools/create_data.py` 作为「数据准备总调度台」的完整工作流程：它按数据集名分发到不同的 prep 函数，每个 prep 函数依次完成「生成 v1 info → 升级为 v2 标准格式 → 生成 GT 数据库」三步。
2. 独立写出为 KITTI / nuScenes / ScanNet 生成 info pkl 的完整命令，并理解每个命令行参数的去向。
3. 读懂 KITTI v1 info（`image` / `point_cloud` / `calib` / `annos`）和 v2 标准格式（`images` / `lidar_points` / `instances`）的字段差异，理解为什么要做这次「v1 → v2」的格式升级。
4. 理解 nuScenes 转换器如何组织多相机、sweeps（历史帧）和标注速度，以及室内数据集转换器的特殊产物（分割 label 权重）。
5. 理解 `data/` 目录的符号链接组织方式，以及 `velodyne_reduced`、`kitti_gt_database`、`kitti_dbinfos_train.pkl` 这些副产物各自的用途。

## 2. 前置知识

### 2.1 为什么需要 info pkl

原始数据集的形态千差万别：KITTI 是「一帧一个 `.bin` 点云文件 + 一个 `.txt` 标注文件 + 一个 `.txt` 标定文件」；nuScenes 是一个由 json 表（scene/sample/sample_data/calibrated_sensor/ego_pose…）组成的数据库；ScanNet 是一堆 Matlab 脚本预处理后的 `.ply`/`.json`。如果每次训练都要现场解析这些原始格式，数据集类会变得极其臃肿，而且解析速度慢（KITTI 有 1.5 万帧，nuScenes 有 4 万帧）。

MMDetection3D 的做法是「**离线一次性转换**」：用 `tools/create_data.py` 把原始数据扫一遍，把每帧的路径、尺寸、标定矩阵、标注框等抽取出来，序列化成一个 pickle 文件（即 info pkl）。训练时数据集类只需 `load` 这个 pkl，就能拿到结构统一的字典列表。这就是上一讲 `Det3DDataset` 里 `load_data_list` 读取的那个文件。

一句话总结：**converter 负责「读原始数据、算派生信息、存成 pkl」，dataset 负责「读 pkl、调 parse_data_info、交给 pipeline」**。两者以 info pkl 为界，互不相识。

### 2.2 pkl 与 pickle

pickle 是 Python 的对象序列化协议，`pickle.dump(obj, f)` 把任意 Python 对象（dict、list、numpy 数组）原样写进文件，`pickle.load(f)` 原样读回。MMDetection3d 的 info pkl 就是「一个被 pickle 的大 list 或大 dict」。项目中通常用 MMEngine 封装的 `mmengine.load / mmengine.dump`（自动按扩展名选择格式），它与 `pickle` 完全兼容。

### 2.3 v1 与 v2：一次格式的统一

早期（v1.0.0rc 系列）各数据集的 info 字段各不相同（KITTI 用 `point_cloud/velodyne_path`，nuScenes 用 `lidar_path`），导致每个数据集类都要写一套字段解析逻辑。v1.1.0 起官方把所有数据集的 info 统一成 OpenMMLab V2.0 标准格式：图像信息进 `images`、点云信息进 `lidar_points`、标注进 `instances`（每实例一个 dict），顶层再加 `metainfo`（类别表）与 `data_list`。这个升级由 `tools/dataset_converters/update_infos_to_v2.py` 完成，并且已经**内置**在 `create_data.py` 的流程里——所以今天跑 `create_data.py` 得到的直接就是 v2 格式 pkl。

### 2.4 前置讲义回顾

- u2-l1 讲过 `lidar2cam = R0_rect @ Tr_velo_to_cam`：本讲会看到这行矩阵乘法在转换器里的原始出处。
- u3-l1 讲过 `parse_data_info` 消费 `info['images']['CAM2']['lidar2cam']`、`info['instances']` 等字段：本讲讲的就是这些字段如何被生产出来。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [tools/create_data.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/create_data.py) | 命令行总入口：解析参数，按数据集名分发到各 `*_data_prep` 函数 |
| [tools/dataset_converters/kitti_converter.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/dataset_converters/kitti_converter.py) | KITTI（及 Waymo-KITTI 格式）的 info 生成、reduced 点云、2D 标注导出 |
| [tools/dataset_converters/kitti_data_utils.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/dataset_converters/kitti_data_utils.py) | KITTI 底层工具：路径拼接、label 解析、标定读取、`get_kitti_image_info` |
| [tools/dataset_converters/nuscenes_converter.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/dataset_converters/nuscenes_converter.py) | nuScenes info 生成：多相机、sweeps、速度、valid_flag |
| [tools/dataset_converters/indoor_converter.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/dataset_converters/indoor_converter.py) | 室内数据集（SUN RGB-D / ScanNet / S3DIS）统一入口，额外生成分割信息 |
| [tools/dataset_converters/update_infos_to_v2.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/dataset_converters/update_infos_to_v2.py) | v1 → v2 格式升级器：定义标准字段模板并逐条转换 |
| [tools/dataset_converters/create_gt_database.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/dataset_converters/create_gt_database.py) | GT 数据库生成（本讲只看产物结构，机制详解在 u3-l7） |
| [docs/en/user_guides/dataset_prepare.md](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docs/en/user_guides/dataset_prepare.md) | 官方数据准备文档：目录约定与各数据集命令 |

## 4. 核心概念与源码讲解

### 4.1 create_data.py：数据准备的总调度台

#### 4.1.1 概念说明

`create_data.py` 是一个「纯脚本」——没有类、没有注册器，就是「argparse + 一组 `*_data_prep` 函数 + `if __name__ == '__main__'` 分发」。它的价值在于把**所有数据集的准备工作收拢到一个命令行接口**：

```bash
python tools/create_data.py <数据集名> --root-path ... --out-dir ... --extra-tag ...
```

不管底层是 KITTI 的文件扫描还是 nuScenes 的数据库查询，用户面对的参数是一致的。理解这个文件的方法不是逐行读，而是抓住「**数据集名 → prep 函数 → converter 模块**」的分发表。

#### 4.1.2 核心流程

以 KITTI 为例，`create_data.py kitti` 的完整流程：

```text
命令行解析 (argparse)
    └─ dispatch: args.dataset == 'kitti'
        └─ kitti_data_prep(root_path, info_prefix, version, out_dir, with_plane)
            ├─ ① kitti.create_kitti_info_file()      # 扫描原始数据 → 4 个 v1 info pkl
            ├─ ② kitti.create_reduced_point_cloud()  # 裁剪视场外点 → velodyne_reduced/
            ├─ ③ update_pkl_infos('kitti', ...) × 4  # v1 pkl 原地升级为 v2 格式
            └─ ④ create_groundtruth_database()       # 生成 kitti_gt_database/ + dbinfos pkl
```

其中 ①② 是「转换」，③ 是「标准化」，④ 是「训练增强的原料」。四个 `update_pkl_infos` 分别处理 train / val / trainval / test 四个 pkl。

#### 4.1.3 源码精读

**（1）prep 函数与 converter 的对应关系**。文件头部集中导入各转换器模块：[tools/create_data.py:L7-L14](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/create_data.py#L7-L14)，这几行 import 就是「注册表」——本讲的 practice 任务的第一个观察点。

**（2）`kitti_data_prep` 的四步**：[tools/create_data.py:L17-L54](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/create_data.py#L17-L54)。函数体很短：先调 `create_kitti_info_file` 和 `create_reduced_point_cloud`（L35-36），再对四个 pkl 逐个调 `update_pkl_infos`（L42-45），最后调 `create_groundtruth_database`，注意它传了 `relative_path=False`（L46-53），即 GT 数据库里存的是**绝对路径**。

**（3）命令行参数定义**：[tools/create_data.py:L268-L315](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/create_data.py#L268-L315)。逐个看：

| 参数 | 默认值 | 含义 |
| --- | --- | --- |
| `dataset`（位置参数） | 无 | 数据集名，决定走哪个分支 |
| `--root-path` | `./data/kitti` | 原始数据根目录 |
| `--version` | `v1.0` | 数据集版本（nuScenes/Waymo/Lyft 必需，KITTI 不需要） |
| `--max-sweeps` | 10 | nuScenes/Waymo 每帧最多保留多少个历史 sweep |
| `--with-plane` | False | KITTI 是否把地平面方程写进 info |
| `--out-dir` | `./data/kitti` | pkl 输出目录 |
| `--extra-tag` | `kitti` | pkl 文件名前缀，即 `info_prefix` |
| `--workers` | 4 | 多进程 worker 数（Waymo/室内数据用） |
| `--only-gt-database` | False | 只生成 GT 数据库，不重新生成 info（补跑增强原料用） |

**（4）主分发逻辑**：[tools/create_data.py:L317-L337](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/create_data.py#L317-L337)。先 `init_default_scope('mmdet3d')`（因为 GT 数据库那步要 build 数据集类），然后进入 if-elif 链。KITTI 分支里还有一个「只补 GT 数据库」的捷径（L322-330）。nuScenes 分支值得注意：[tools/create_data.py:L338-L359](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/create_data.py#L338-L359) 中非 mini 版本会**连续调两次** `nuscenes_data_prep`，一次 `-trainval` 一次 `-test`，因为 nuScenes 的 trainval 和 test 是两个独立的下载包。

**（5）`data/` 目录的符号链接组织**。官方文档建议把数据集根目录软链到 `mmdetection3d/data/` 下：[docs/en/user_guides/dataset_prepare.md:L5-L6](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docs/en/user_guides/dataset_prepare.md#L5-L6)。这样做的原因：所有 config 里 `data_root` 默认都是 `data/kitti/`、`data/nuscenes/` 这样的相对路径，只要软链名字对上，磁盘上的数据实际放在哪里都无所谓。文档 L8-83 给出了完整的目录树，其中 KITTI 部分要求 `ImageSets/ + training/{calib,image_2,label_2,velodyne} + testing/{...}`，这个结构就是 converter 逐帧扫描的依据。

#### 4.1.4 代码实践

**实践 A：画出分发映射表**

1. 实践目标：不运行任何代码，仅靠阅读源码列出「数据集名 → prep 函数 → 底层 converter 函数」的完整映射。
2. 操作步骤：读 [tools/create_data.py:L321-L420](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/create_data.py#L321-L420) 的 if-elif 链，再跳进每个 prep 函数看它调了哪个 converter 的哪个函数。
3. 需要观察的现象：哪些 prep 函数会调 `update_pkl_infos`？哪些调了 `create_groundtruth_database`？哪些两者都没有？
4. 预期结果（可对照）：除 waymo 外，所有 prep 函数（kitti/nuscenes/lyft/scannet/s3dis/sunrgbd）都会调 `update_pkl_infos` 把 pkl 升级到 v2；waymo 的 `waymo_data_prep` 不调它，v2 化由 `Waymo2KITTI` 转换器内部完成（参见 [tools/create_data.py:L171-L254](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/create_data.py#L171-L254)）；只有 kitti / nuscenes / waymo 生成 GT 数据库，室内三件套与 lyft 不生成（室内算法不用 ObjectSample 增强）。

**实践 B：写出 KITTI 完整命令**（本讲核心实践，见第 5 节综合实践一起做）

KITTI 官方命令在 [docs/en/user_guides/dataset_prepare.md:L114-L118](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docs/en/user_guides/dataset_prepare.md#L114-L118)：

```bash
python tools/create_data.py kitti --root-path ./data/kitti --out-dir ./data/kitti --extra-tag kitti
```

它执行的前提是 `./data/kitti/` 下已有 `ImageSets/{train,val,test,trainval}.txt`（官方建议从 second.pytorch 仓库下载划分文件，见文档 L105-111）以及完整的 `training/` 目录。**待本地验证**：命令实际运行需要先下载好 KITTI 数据。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `--extra-tag` 参数的文档说明是「name of info pkl」？它最终出现在哪些文件名里？

答案：`extra_tag` 就是传给 prep 函数的 `info_prefix`。它决定四类输出文件名前缀：`{prefix}_infos_train.pkl`、`{prefix}_infos_val.pkl`、`{prefix}_infos_trainval.pkl`、`{prefix}_infos_test.pkl`（拼接位置在 [tools/create_data.py:L38-L41](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/create_data.py#L38-L41)），以及 GT 数据库目录 `{prefix}_gt_database/` 和 `{prefix}_dbinfos_train.pkl`。config 里 `KittiDataset` 的 `ann_file` 必须与这个前缀一致（如 `kitti_infos_train.pkl`）。

**练习 2**：如果只想补生成 GT 数据库（比如下载了官方现成标注、但要用 ObjectSample 增强），该怎么写命令？

答案：加 `--only-gt-database`，见文档 [docs/en/user_guides/dataset_prepare.md:L131-L132](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docs/en/user_guides/dataset_prepare.md#L131-L132)：`python tools/create_data.py kitti --root-path ./data/kitti --out-dir ./data/kitti --extra-tag kitti --only-gt-database`。它走 [tools/create_data.py:L322-L330](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/create_data.py#L322-L330) 的捷径分支，跳过 info 生成。

**练习 3**：为什么 `create_data.py` 主体要先 `init_default_scope('mmdet3d')`？

答案：因为流程第 ④ 步 `create_groundtruth_database` 内部会用 `DATASETS.build` 构造真正的数据集对象（[tools/dataset_converters/create_gt_database.py:L221](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/dataset_converters/create_gt_database.py#L221)），`type='KittiDataset'` 这类字符串要在 `mmdet3d` 命名空间的注册器里才能查到，不初始化 scope 会报 "KittiDataset not found"。

### 4.2 kitti_converter：从 KITTI 原始文件到 v2 info pkl

#### 4.2.1 概念说明

KITTI 原始数据的组织是「按帧分文件」：第 `i` 帧的点云在 `training/velodyne/{i:06d}.bin`，标注在 `training/label_2/{i:06d}.txt`，标定在 `training/calib/{i:06d}.txt`，图像在 `training/image_2/{i:06d}.png`。converter 要做的就是把每帧的这 4 个文件读一遍，汇总成一个 info dict，并顺手计算一个原始标注里没有的派生量：**每个 GT 框内有多少个点**（`num_points_in_gt`，后面 GT 数据库采样和难度过滤都要用它）。

整个 KITTI 转换分三层：`kitti_converter.create_kitti_info_file`（编排）→ `kitti_data_utils.get_kitti_image_info`（单帧信息收集，多线程）→ `update_infos_to_v2.update_kitti_infos`（格式标准化）。

#### 4.2.2 核心流程

```text
create_kitti_info_file(data_path)
    ├─ 读 ImageSets/{train,val,test}.txt → 得到三个帧 id 列表
    ├─ train: get_kitti_image_info(training=True)   # 多线程逐帧收集
    │        + _calculate_num_points_in_gt()         # 派生：每框点数
    │        → dump 为 {prefix}_infos_train.pkl      # v1 格式
    ├─ val: 同上 → {prefix}_infos_val.pkl
    ├─ trainval = train + val 直接拼接 → {prefix}_infos_trainval.pkl
    └─ test: get_kitti_image_info(training=False, label_info=False)  # 无标注
             → {prefix}_infos_test.pkl
随后 update_kitti_infos 把每个 pkl 逐条转成 v2 标准格式并原地覆盖
```

v1 单条 info 的结构（docstring 原文）：

```text
{
    image:       {image_idx, image_path, image_shape}
    point_cloud: {num_features: 4, velodyne_path}
    calib:       {P0, P1, P2, P3, R0_rect, Tr_velo_to_cam, Tr_imu_to_velo}
    annos:       {name, truncated, occluded, alpha, bbox, dimensions,
                  location, rotation_y, score, index, group_ids,
                  difficulty, num_points_in_gt}
}
```

#### 4.2.3 源码精读

**（1）按 ImageSets 划分逐帧生成**：[tools/dataset_converters/kitti_converter.py:L179-L182](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/dataset_converters/kitti_converter.py#L179-L182) 读入三个 id 列表；[L189-L215](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/dataset_converters/kitti_converter.py#L189-L215) 依次生成 train / val / trainval 三个 pkl，其中 **trainval 就是 train 与 val 两个 list 直接相加**（L215），不做去重——这也意味着缩减数据时只要同步缩减 `train.txt`/`val.txt` 即可。

**（2）单帧收集 `get_kitti_image_info` 的 map_func**：[tools/dataset_converters/kitti_data_utils.py:L209-L290](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/dataset_converters/kitti_data_utils.py#L209-L290)。三个关键点：

- 路径存在性检查：`get_kitti_info_path` 会 `raise ValueError('file not exist')`（L35-36），所以**列在 ImageSets 里但缺文件的帧会直接让转换崩溃**——这是缩减数据做实验时最容易踩的坑。
- 标定解析：L239-270 把 calib 文件的 7 行矩阵读出，P0-P3 与 Tr 都通过 `_extend_matrix` 扩成 4×4 齐次矩阵（L161-163），方便后续直接矩阵相乘。
- 尺寸语义换算：[L143-L146](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/dataset_converters/kitti_data_utils.py#L143-L146) 把 KITTI 标注里 `h w l` 顺序重排成相机系下的 `l h w`（`[:, [2, 0, 1]]`），这是 KITTI 数据处理的一个经典细节。

**（3）派生信息 `num_points_in_gt`**：[tools/dataset_converters/kitti_converter.py:L118-L156](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/dataset_converters/kitti_converter.py#L118-L156)。流程是：读点云 → 用标定把图像视场外的点裁掉（`remove_outside_points`）→ 把 GT 框从相机系转到雷达系（`box_camera_to_lidar`，L149-150）→ `points_in_rbbox` 统计落在每个框内的点（L151-152）。注意 DontCare 框补 `-1`（L153-155）。这里产生的 `num_points_in_gt` 之后会出现在 v2 的 `num_lidar_pts` 字段里，也是 mono3d 数据过滤的重要依据。

**（4）reduced 点云**：[tools/dataset_converters/kitti_converter.py:L307-L365](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/dataset_converters/kitti_converter.py#L307-L365) 的 `_create_reduced_point_cloud` 把每帧点云中投影落在 CAM2 图像视场之外的点删掉，存进 `velodyne_reduced/`。**这不是可选项**：KITTI 的训练配置里 `data_prefix` 指向的就是 reduced 目录，见 [configs/_base_/datasets/kitti-3d-3class.py:L115](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/datasets/kitti-3d-3class.py#L115)（`pts='training/velodyne_reduced'`）。如果只生成 info 不生成 reduced 点云，`LoadPointsFromFile` 会找不到文件。

**（5）v1 → v2 的字段搬运**：[tools/dataset_converters/update_infos_to_v2.py:L391-L529](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/dataset_converters/update_infos_to_v2.py#L391-L529)。`update_kitti_infos` 对每条 v1 info 做三件事：

- 把 `calib.P*` 搬进 `images.CAM*['cam2img']`，把 `image` 搬进 `images.CAM2`，把 `point_cloud` 搬进 `lidar_points`（L415-432）。注意 L431-432 用 `Path(...).name` 把 `training/velodyne/000008.bin` 削成裸文件名 `000008.bin`——路径前缀交给 config 里的 `data_prefix` 负责，这就是「info 与 config 分工」的关键设计。
- 计算跨库通用的复合矩阵：[L434-L445](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/dataset_converters/update_infos_to_v2.py#L434-L445) 中 `lidar2cam = rect @ Trv2c`、`lidar2img = P @ lidar2cam`，正是 u2-l1 讲过的投影链在源码中的生产位置。
- 逐实例构造 `instances` 列表（L457-509）：模板来自 `get_empty_instance()`（[L28-L67](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/dataset_converters/update_infos_to_v2.py#L28-L67)），其中有个容易忽略的几何修正：[L479-L487](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/dataset_converters/update_infos_to_v2.py#L479-L487) 把 KITTI 标注的框中心从「底面中心」（锚点 \([0.5, 1.0, 0.5]\)）平移到「几何中心」（\([0.5, 0.5, 0.5]\)），并顺手算出投影 2D 中心 `center_2d` 与深度 `depth` 供 mono3d 使用。

最后写盘：[L514-L529](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/dataset_converters/update_infos_to_v2.py#L514-L529) 把 v2 结构 `dict(metainfo, data_list)` dump 回**同名文件**（原地覆盖，函数开头还留了 5 秒警告）。`metainfo` 含 `categories`（类名 → 索引，未选中的类如 `DontCare` 映射为 -1）、`dataset` 与 `info_version='1.1'`。而 `clear_instance_unused_keys`（[L187-L192](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/dataset_converters/update_infos_to_v2.py#L187-L192)）会把值仍为 `None` 的可选键删掉，所以你最终在 pkl 里看到的 instance 键是「模板 − 未用键」。

#### 4.2.4 代码实践

**实践：用 pickle 解剖一个 KITTI info pkl**

1. 实践目标：亲手看到 v2 pkl 的三层结构（metainfo / data_list / instances），并打印一条标注记录的全部键。
2. 操作步骤（示例代码，需在生成 pkl 后运行）：

   ```python
   # inspect_kitti_pkl.py（示例代码）
   import pickle

   with open('./data/kitti/kitti_infos_trainval.pkl', 'rb') as f:
       infos = pickle.load(f)          # 等价于 mmengine.load(...)

   print(type(infos), list(infos.keys()))   # 期望: dict_keys(['metainfo', 'data_list'])
   print(infos['metainfo'])                 # categories / dataset / info_version
   print(len(infos['data_list']))           # trainval 帧数（KITTI 完整为 7481）

   info = infos['data_list'][0]
   print(list(info.keys()))                 # sample_idx / images / lidar_points / instances ...
   print(info['lidar_points']['lidar_path'])       # 期望: 形如 '000008.bin' 的裸文件名
   print(info['images']['CAM2']['lidar2cam'])      # 4x4 矩阵（list of list）

   inst = info['instances'][0]
   print(list(inst.keys()))                 # 一条 ann 记录的全部键
   print(inst['bbox_label_3d'], inst['bbox_3d'])   # 类别索引 + 7 参数框
   ```

3. 需要观察的现象：`lidar_path` 是裸文件名而非完整路径；`bbox_3d` 是 7 个数（相机系 x,y,z,l,h,w,yaw）；KITTI 里 `instances` 的键会比 nuScenes 少（例如没有 `attr_label`、`num_radar_pts`，因为模板里那些键从未被赋值、被 `clear_instance_unused_keys` 清掉了）。
4. 预期结果：能对照 4.2.3（5）的字段清单逐一找到出处。**待本地验证**：打印结果依赖真实生成的 pkl；若暂时没有 KITTI 数据，可以从官方文档「Summary of annotation files」一节提到的离线标注包下载现成 pkl 直接解剖。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `get_kitti_image_info` 生成 test 集 info 时传 `label_info=False`？

答案：KITTI 测试集没有公开标注（`testing/` 目录下没有 `label_2`），强行读会报错；同时 `with_plane=False`。生成的 test info 里没有 `annos`/`instances` 字段，只含路径与标定，仅供推理提交使用。见 [tools/dataset_converters/kitti_converter.py:L217-L225](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/dataset_converters/kitti_converter.py#L217-L225)。

**练习 2**：v1 的 `annos.dimensions` 是 `[N, 3]` 的 numpy 数组（所有实例并列），v2 的 `instances` 是 `list[dict]`（每实例一个 dict）。这种「列式 → 行式」的转换有什么好处？

答案：v1 列式存储紧凑、适合 numpy 批量运算，但取「第 i 个实例的全部属性」要遍历所有键；v2 行式存储让每条标注自包含，`parse_data_info` 里可以逐实例过滤（如按 `bbox_label_3d == -1` 丢弃忽略类）、逐实例增删字段而不破坏其他实例，也更贴近 COCO 式的通用标注范式，方便跨数据集统一处理。

**练习 3**：如果只跑 `create_kitti_info_file` 而不跑 `update_pkl_infos`，训练时会发生什么？

答案：pkl 仍是 v1 格式，顶层是裸 list 而没有 `metainfo`/`data_list` 结构，`KittiDataset.parse_data_info` 访问 `info['images']['CAM2']['lidar2cam']`、`info['instances']` 等键会直接 `KeyError`。所以 1.x 版本必须用完整流程生成的 v2 pkl（旧 pkl 可用 [tools/dataset_converters/update_infos_to_v2.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/dataset_converters/update_infos_to_v2.py) 单独升级，命令见 [docs/en/user_guides/dataset_prepare.md:L243-L255](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docs/en/user_guides/dataset_prepare.md#L243-L255)）。

### 4.3 nuscenes_converter：多传感器、sweeps 与速度

#### 4.3.1 概念说明

nuScenes 与 KITTI 的本质区别：它的原始数据不是散文件，而是一个关系型数据库（nuscenes-devkit 的 `NuScenes` 类加载一堆 json 表，通过 token 互相关联）。一帧（sample）包含 1 个 TOP 激光雷达 + 6 个相机 + 若干历史 sweep。converter 的工作就是**顺着 token 把这些关联信息物化成扁平的 info dict**，并预先算好两个昂贵的派生量：

- **sensor2lidar 变换**：把每个相机/每个历史 sweep 的数据直接变换到当前帧激光雷达坐标系的 \( R, t \)，避免训练时现算。
- **速度（gt_velocity）**：nuScenes 标注自带全局系速度，converter 把它逆变换到雷达系（`v_{lidar} = v_{global} \cdot R_{e2g}^{-1} R_{l2e}^{-1}`，方向按行向量左乘约定转置），供 CenterPoint 等算法的速度回归分支使用。

#### 4.3.2 核心流程

```text
create_nuscenes_infos(root_path, version, max_sweeps)
    ├─ NuScenes(version, dataroot) 载入数据库
    ├─ 按 nuscenes.utils.splits 取 train/val 场景 token，
    │   并过滤掉点云文件缺失的场景（get_available_scenes）
    ├─ _fill_trainval_infos: 遍历每个 sample
    │     ├─ 主 info: lidar_path / token / lidar2ego / ego2global / timestamp
    │     ├─ cams: 6 个相机各一份（data_path + sensor2lidar + cam_intrinsic）
    │     ├─ sweeps: 沿 prev 指针回溯最多 max_sweeps 帧
    │     └─ 非 test: gt_boxes / gt_names / gt_velocity /
    │                num_lidar_pts / num_radar_pts / valid_flag
    └─ dump 为 dict(infos=..., metadata=...) → {prefix}_infos_{train,val,test}.pkl
随后 update_nuscenes_infos 转成 v2（metainfo + data_list）
```

#### 4.3.3 源码精读

**（1）场景过滤**：[tools/dataset_converters/nuscenes_converter.py:L44-L74](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/dataset_converters/nuscenes_converter.py#L44-L74) 用 devkit 的官方划分（`splits.train`/`splits.val`/`splits.mini_train`…）取场景，再与实际存在的文件求交集。所以**只下载了 mini 包也能跑同一套命令**，只是可用场景变少。

**（2）单帧 info 的骨架**：[tools/dataset_converters/nuscenes_converter.py:L178-L189](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/dataset_converters/nuscenes_converter.py#L178-L189)。注意 `num_features: 5`（nuScenes 点是 5 通道：x,y,z,intensity,ring index，而 KITTI 是 4），以及四元数形式的 `lidar2ego_*` / `ego2global_*`——v2 转换时才合成 4×4 矩阵。

**（3）sweeps 回溯**：[tools/dataset_converters/nuscenes_converter.py:L216-L226](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/dataset_converters/nuscenes_converter.py#L216-L226)。`sample_data` 表的 `prev` 字段是链表指针，循环里不断 `nusc.get('sample_data', sd_rec['prev'])` 直到取满 `max_sweeps` 帧或到达序列头。这些 sweep 的信息由 `obtain_sensor2top` 统一加工。

**（4）`obtain_sensor2top` 的矩阵合成**：[tools/dataset_converters/nuscenes_converter.py:L283-L341](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/dataset_converters/nuscenes_converter.py#L283-L341)。注释里写明了变换链 `sweep→ego→global→ego'→lidar`，L329-340 把四元数转旋转矩阵后合成出 `sensor2lidar_rotation` 与 `sensor2lidar_translation`，约定 `points @ R.T + T`。训练时 `LoadPointsFromMultiSweeps` 直接用这两个量把历史帧点云拼到当前帧，不再访问 devkit。

**（5）标注与速度**：[tools/dataset_converters/nuscenes_converter.py:L228-L268](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/dataset_converters/nuscenes_converter.py#L228-L268)。四个要点：类名经 `NuScenesNameMapping` 归一（L251-254）；框尺寸重排 `dims[:, [1, 0, 2]]` 把 devkit 的 `w, l, h` 换成雷达系 `l, w, h`（L258，与 KITTI 的重排方向相反，因为源格式不同）；速度从全局系逆变换到雷达系（L243-248）；`valid_flag` = 该框内激光点数 + 毫米波点数 > 0（L239-242），训练时用它剔除「无点支撑」的困难样本。

**（6）v2 转换**：[tools/dataset_converters/update_infos_to_v2.py:L249-L340](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/dataset_converters/update_infos_to_v2.py#L249-L340)。相比 KITTI 多了三块搬运：`lidar_sweeps` 列表（每个 sweep 一份 `lidar2ego`/`ego2global`/`lidar2sensor`，L295-316）、六相机的 `images`（含 `cam2img`/`cam2ego`/`lidar2cam`，L317-339）、以及四元数到 4×4 矩阵的统一转换（`convert_quaternion_to_matrix`）。注意 L293-294 与 L326-328 的时间戳统一除以 1e6（微秒 → 秒）。

#### 4.3.4 代码实践

**实践：对比 KITTI 与 nuScenes 的 v2 info 字段（源码阅读型）**

1. 实践目标：不看数据、只看转换器源码，推导两个数据集 v2 info 的字段差异表。
2. 操作步骤：
   - 对照 [update_infos_to_v2.py:L391-L529](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/dataset_converters/update_infos_to_v2.py#L391-L529)（KITTI）与 [L249-L390](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/dataset_converters/update_infos_to_v2.py#L249-L390)（nuScenes）两个函数，逐键填写下面的表。
   - 再到 [mmdet3d/datasets/kitti_dataset.py:L92-L131](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/kitti_dataset.py#L92-L131) 与 `mmdet3d/datasets/nuscenes_dataset.py` 的 `parse_data_info` 验证：你列出的每个键是否真的被消费。
3. 需要观察的现象（预期差异表）：

   | 字段 | KITTI | nuScenes | 消费者 |
   | --- | --- | --- | --- |
   | `lidar_points.num_pts_feats` | 4 | 5 | `LoadPointsFromFile` |
   | `lidar_sweeps` | 无 | 有 | `LoadPointsFromMultiSweeps` |
   | `images` 相机数 | 4（CAM0-3，仅 CAM2 填 img_path） | 6 | `LoadMultiViewImageFromFiles` |
   | `instances[*].num_radar_pts` | 无 | 有 | 评估/过滤 |
   | `instances[*].attr_label` | 无（KITTI 无属性） | 有 | FCOS3D 等属性分类 |
   | `instances[*].truncated/occluded/alpha` | 有 | 无 | KittiMetric 难度 |

4. 预期结果：每行差异都能在两个 update 函数里找到出处；KITTI 的 instances 键更多是「评估用」（difficulty/truncated/occluded），nuScenes 的更多是「训练用」（速度、valid_flag）。

#### 4.3.5 小练习与答案

**练习 1**：`create_nuscenes_infos` dump 的 v1 pkl 顶层是 `dict(infos=..., metadata=...)`，而 KITTI v1 pkl 顶层是裸 list。为什么会有这个差别？

答案：nuScenes 转换后仍需 `metadata['version']`——v2 转换函数要重新实例化 `NuScenes(version=data_list['metadata']['version'], ...)` 来查询 2D 框等数据库信息（[update_infos_to_v2.py:L269-L272](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/dataset_converters/update_infos_to_v2.py#L269-L272)）；KITTI 无数据库依赖，裸 list 足够。这也提醒我们：nuScenes 的 `--out-dir` 需要能访问到原始数据。

**练习 2**：`--max-sweeps` 设成 3 与 10，对生成的 info 有什么影响？对训练有什么影响？

答案：只影响 `sweeps` 列表长度（[nuscenes_converter.py:L218-L225](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/dataset_converters/nuscenes_converter.py#L218-L225) 中 `while len(sweeps) < max_sweeps`），每个 sweep 都带自己的 `sensor2lidar` 变换。训练时 `LoadPointsFromMultiSweeps(sweeps_num=N)` 最多消费 N 帧；sweep 越多点云越稠密但运动模糊越重，CenterPoint 等配置常用 10。

**练习 3**：nuScenes 的 `gt_boxes` 直接存在雷达坐标系（converter 里 `locs` 来自 devkit 的全局系框经 `boxes` 变换），而 KITTI 的 `bbox_3d` 存在相机坐标系。这个差异在 dataset 类里如何被抹平？

答案：`KittiDataset.parse_ann_info` 用 `info['images']['CAM2']['lidar2cam']` 的逆矩阵把相机系框转到雷达系（见 [mmdet3d/datasets/kitti_dataset.py:L166-L171](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/kitti_dataset.py#L166-L171)）；nuScenes 的 `NuScenesDataset` 拿到的已是雷达系框，直接包成 `LiDARInstance3DBoxes`。坐标系归一永远是「converter 存原始系 + dataset 转目标系」的分工。

### 4.4 indoor_converter：室内数据集的统一入口

#### 4.4.1 概念说明

室内三件套（SUN RGB-D / ScanNet / S3DIS）的原始数据都要先用各自 `data/<name>/README.md` 里的脚本预处理成中间格式，然后 `indoor_converter.create_indoor_info_file` 把中间格式抽成 info pkl。它与室外转换器最大的不同有两个：

1. **检测与分割双产物**：ScanNet/S3DIS 除了检测 info，还要生成**分割 info**——对每个场景重采样固定点数，并统计逐类点数的 label weight（用于交叉熵加权），这是 `Cylinder3D`/`PointNet++` 分割训练必需的。
2. **划分方式不同**：S3DIS 没有固定 train/val 划分，而是按 6 个 Area 各生成一个 pkl（训练时用 dataset wrapper 组合），所以 create_data 里 `s3dis_data_prep` 要循环调 6 次 `update_pkl_infos`（[tools/create_data.py:L148-L151](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/create_data.py#L148-L151)）。

#### 4.4.2 核心流程

```text
create_indoor_info_file(data_path, pkl_prefix, save_path, workers)
    ├─ 断言 pkl_prefix ∈ {sunrgbd, scannet, s3dis}
    ├─ 检测部分：
    │    sunrgbd: SUNRGBDData(split='train'/'val').get_infos() → 2 个 pkl
    │    scannet: ScanNetData(split='train'/'val'/'test').get_infos() → 3 个 pkl
    │    s3dis:   每 Area 一个 S3DISData → 6 个 pkl
    ├─ 分割部分（仅 scannet / s3dis）：
    │    ScanNetSegData(ann_file=检测pkl).get_seg_infos()
    │      → {prefix}_infos_train_with_instance_label.pkl
    │        + {prefix}_seg_label_weight.npy
    └─ （v2 升级在 create_data.py 的 *_data_prep 里随后统一执行）
```

#### 4.4.3 源码精读

**（1）入口与断言**：[tools/dataset_converters/indoor_converter.py:L13-L34](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/dataset_converters/indoor_converter.py#L13-L34)。一个函数服务三个数据集，靠 `pkl_prefix` 分叉——这是「小而钝」的设计，好处是 create_data 侧只有一个 `scannet/sunrgbd/s3dis_data_prep` 共同入口（[tools/create_data.py:L118-L168](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/create_data.py#L118-L168)）。

**（2）检测 info 的生成**：[L37-L68](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/dataset_converters/indoor_converter.py#L37-L68)。`SUNRGBDData`/`ScanNetData` 的 `get_infos(num_workers, has_label)` 负责真正的抽取（各自实现于 `sunrgbd_data_utils.py`/`scannet_data_utils.py`，不在本讲展开），test 集传 `has_label=False`。

**（3）分割 info 与 label weight**：[L74-L92](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/dataset_converters/indoor_converter.py#L74-L92)。`ScanNetSegData` 以检测 pkl 为 `ann_file`，重采样 8192 点并按公式

\[ w_c = \frac{1}{\ln(1.2 + n_c)} \]

计算每类权重（\(n_c\) 是类 c 的点数，权重函数借鉴自 PointNet++ 官方实现，L82 的 `label_weight_func=lambda x: 1.0 / np.log(1.2 + x)`）。点数越少的类权重越大，用来对抗类别不均衡。

**（4）S3DIS 的按 Area 循环**：[L93-L111](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/dataset_converters/indoor_converter.py#L93-L111)，每个 Area 都先生成检测 info 再生成对应分割 info。

#### 4.4.4 代码实践

**实践：写出 ScanNet 的数据准备命令并预测产物清单（先纸上后验证）**

1. 实践目标：从源码推导 `create_data.py scannet` 会产生哪些文件，再与文档核对。
2. 操作步骤：
   - 先按 [data/scannet/README.md](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/data/scannet/README.md) 的脚本完成原始数据预处理（提取点云与 instance/semantic 标签）。
   - 运行：

     ```bash
     python tools/create_data.py scannet --root-path ./data/scannet --out-dir ./data/scannet --extra-tag scannet
     ```

   - 运行前后各列一次 `./data/scannet/` 目录清单（`ls` 前后对比）。
3. 需要观察的现象 / 预期产物（由源码推导，**待本地验证**）：
   - 检测：`scannet_infos_train.pkl`、`scannet_infos_val.pkl`、`scannet_infos_test.pkl`（indoor_converter L38-68），且均为 v2 格式（create_data.py L132-134 的三次 `update_pkl_infos`）；
   - 分割：`scannet_infos_train_with_instance_label.pkl` 与同名 `_val_` 版本、`scannet_seg_label_weight.npy`（`get_seg_infos` 的输出）；
   - **没有** `scannet_gt_database/`（室内数据不走 GT 数据库，create_data.py 的 `scannet_data_prep` 没有这一步）。
4. 预期结果：你的预测清单与实际 `ls` 一致；若不一致，回到源码找漏掉的分支。

#### 4.4.5 小练习与答案

**练习 1**：为什么 ScanNet 要生成两套 info（检测用与分割用），而 KITTI 只有一套？

答案：检测任务按「帧/场景」组织（一个场景一条 info，含 instances 列表）；分割任务按「采样点集」组织（每个场景重采样 8192 点、逐点标签、外加全局 label weight）。两者的数据粒度不同，混在一个 pkl 里会让检测加载背着巨大的逐点标签。KITTI 没有官方逐点语义标注，自然只有一套。

**练习 2**：`s3dis_data_prep`（[tools/create_data.py:L137-L151](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/create_data.py#L137-L151)）为什么循环 6 个 Area 而不是 train/val 两个 split？

答案：S3DIS 的官方评估协议是留一法（leave-one-out）：6 个 Area 轮流做验证集。converter 无法预知用户要留哪个 Area，干脆每个 Area 各存一份 pkl，训练时用 `dataset_wrappers`（如 `ConcatDataset`）自由组合——这是「数据划分策略上交给配置层」的例子。

**练习 3**：室内转换器不生成 GT 数据库，这对训练配置意味着什么？

答案：室内算法（VoteNet、GroupFree3D 等）的数据增强不用 `ObjectSample`（GT-Amp），它们的 config pipeline 里没有 `DataBaseSampler` 相关项；只有 KITTI/nuScenes/Waymo 的配置会引用 `*_dbinfos_train.pkl`（该机制的完整拆解放到 u3-l7）。

## 5. 综合实践

**任务：把 KITTI 缩到 10 帧，走完「原始数据 → v2 info pkl → 数据集加载」全程**（数据可缩放，无需下载完整 KITTI；若本机无任何 KITTI 数据，可改用官方离线标注 pkl 完成第 3-4 步）。

1. 实践目标：亲手触发一次完整转换链，并用 pickle 验证每一步的产物，最后接上一讲的 `Det3DDataset` 完成闭环。
2. 操作步骤：
   1. **缩数据**：备份后编辑 `data/kitti/ImageSets/train.txt` 与 `val.txt`，各只保留 5 个真实存在的帧 id（`training/` 下四类文件齐全的帧）。注意：id 缺文件会让转换在存在性检查处直接抛错（见 4.2.3（2））。
   2. **跑转换**：

      ```bash
      python tools/create_data.py kitti \
          --root-path ./data/kitti --out-dir ./data/kitti --extra-tag kitti
      ```

   3. **盘点产物**：`ls ./data/kitti/`，应看到 4 个 `kitti_infos_*.pkl`、`kitti_gt_database/`、`kitti_dbinfos_train.pkl`，以及 `training/velodyne_reduced/`。
   4. **解剖 pkl**：运行 4.2.4 的 `inspect_kitti_pkl.py`（把文件名换成 `kitti_infos_train.pkl`），确认 `data_list` 长度为 5，打印一条 instance 的全部键并逐键标注它来自 v1 的哪个字段。
   5. **接上数据集**：临时建一个小脚本，用 `KittiDataset(data_root='./data/kitti/', ann_file='kitti_infos_train.pkl', ...)` 构造数据集（参数抄 [configs/_base_/datasets/kitti-3d-3class.py:L110-L135](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/datasets/kitti-3d-3class.py#L110-L135) 中的 train_dataloader.dataset 块，把 `ann_file` 换成 train），取 `dataset[0]`，确认能拿到 `inputs` 与 `data_samples`。
3. 需要观察的现象：第 5 步成功即证明「converter 产出的字段 ←→ `parse_data_info` 消费的字段」完全对齐；若失败，报错的键名会直接指出是转换链哪一环缺了字段。
4. 预期结果：完整链路跑通，能说出「一条 KITTI 标注从 `label_2/xxxxxx.txt` 的一行文本，变成 `data_samples.gt_instances_3d.bboxes_3d` 里的一行 tensor」中间经历的全部中转（label txt → v1 annos 列式数组 → v2 instances 行式 dict → parse_ann_info 的 LiDARInstance3DBoxes → Pack3DDetInputs）。**待本地验证**（依赖本机 KITTI 数据）。

## 6. 本讲小结

- `tools/create_data.py` 是纯脚本调度台：数据集名 → prep 函数 → converter 模块，prep 函数固定按「v1 info → v2 升级 → GT 数据库」三步编排。
- info pkl 是 converter 与 dataset 的契约边界：converter 离线完成「读原始格式 + 算派生量（num_points_in_gt、sensor2lidar、lidar2cam、label weight）」，dataset 在线只做轻量加工。
- KITTI 链路：`ImageSets` 划分 → `get_kitti_image_info` 多线程收帧 → `update_kitti_infos` 搬运为 `images/lidar_points/instances` 三段式并原地覆盖 pkl；`velodyne_reduced` 是训练配置的硬依赖。
- v2 标准格式的三件套：顶层 `metainfo + data_list`、单条 `images/lidar_points(/lidar_sweeps)/instances`、instance 模板由 `get_empty_instance` 定义并清理未用键。
- nuScenes 链路的独有产物：6 相机信息、沿 `prev` 指针回溯的 sweeps、雷达系速度与 valid_flag；室内链路的独有产物：分割 info 与 `1/ln(1.2+n)` 形式的 label weight。
- `data/` 用符号链接对齐 config 的默认相对路径；`--extra-tag` 决定全部产物文件名前缀。

## 7. 下一步学习建议

至此，「数据怎么进来的」已经闭环。下一讲 **u3-l3 室外数据集** 将反过来看消费端：`KittiDataset/NuScenesDataset/WaymoDataset/LyftDataset` 的 `parse_data_info` 如何把本讲生成的 v2 字段加工成训练样本，重点比较 sweeps 聚合与多相机拆分。建议先自读：

- [mmdet3d/datasets/nuscenes_dataset.py](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/datasets/nuscenes_dataset.py) 的 `parse_data_info`，对照本讲 4.3 的字段表；
- 若对 GT 数据库意犹未尽，可提前浏览 [tools/dataset_converters/create_gt_database.py:L290-L340](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/dataset_converters/create_gt_database.py#L290-L340) 中每个目标存成单独 `.bin` 的循环，完整机制在 u3-l7 展开。
