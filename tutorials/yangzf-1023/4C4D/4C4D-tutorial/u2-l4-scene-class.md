# u2-l4 Scene 类：数据集分发与场景装配

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `Scene` 类在训练流程中的位置：它是「数据集描述」与「可训练 4D 高斯」之间的装配工厂。
2. 解释 `Scene.__init__` 如何通过 `sceneLoadTypeCallbacks` 回调表，仅凭目录特征（有没有 `sparse/`、有没有 `transforms_train.json`）在 Colmap 与 Blender 两种数据集之间自动分发。
3. 推导 `cameras_extent`（即 `nerf_normalization["radius"]`）的计算公式，并追踪它如何同时影响位置学习率（`spatial_lr_scale`）与致密化中 clone/split 的尺寸分界（`percent_dense * scene_extent`）。
4. 列出一次训练在 `model_path` 下生成的全部文件，并说明各自由哪段代码写出。
5. 说清楚高斯初始化的三条分支（`loaded_pth` / `loaded_iter` / `create_from_pcd`）各自的触发条件与适用场景。

## 2. 前置知识

本讲建立在前几讲的概念之上，先用通俗语言把几个关键术语补齐：

- **SceneInfo / CameraInfo / BasicPointCloud**：u2-l2 讲过，`dataset_readers.py` 用三个 NamedTuple 容器分别描述「整个场景」（点云 + 相机列表 + 归一化信息 + ply 路径）、「一台相机在某一帧的拍摄参数」和「初始三维点云」。`Scene` 类的输入本质上就是一份 `SceneInfo`。
- **回调表（callback table）/ 策略模式**：用一个字典把「数据集类型名」映射到「读取函数」，比如 `{"Colmap": readColmapSceneInfo, "Blender": readNerfSyntheticInfo}`。调用方不写 `if/else` 硬编码，而是查表调用。这是本项目支持多种数据格式的唯一注册点，也是 u8-l4 二次开发时新增数据集的挂载点。
- **C2W / W2C**：世界到相机（W2C）变换用于把世界坐标投到图像；相机到世界（C2W）变换的平移列就是**相机光心**（相机在世界系下的位置）。计算场景半径需要的是光心，所以要先求 C2W。
- **spatial_lr_scale（空间学习率缩放因子）**：3DGS 的一个工程经验——场景越大，高斯每次该移动的距离就越大。这个因子直接乘在位置学习率上，本讲会看到它正是 `cameras_extent`。
- **model_path（输出目录）**：训练的一切产物（checkpoint、点云、相机参数、TensorBoard 日志）都写到这里。由 `--model_path` 指定，未指定时自动生成到 `./output/<uuid前10位>`。

如果你对 `timestamp` 归一化、`camXX_YYYY.png` 命名约定、`training_view` 划分还不熟悉，请先复习 u2-l2；对 Camera 的投影矩阵、懒加载还不熟悉，请先复习 u2-l3。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `scene/__init__.py` | `Scene` 类定义 | 构造流程、分发逻辑、输出文件、初始化分支、相机访问接口 |
| `scene/dataset_readers.py` | 数据集读取 | `sceneLoadTypeCallbacks` 注册表、`getNerfppNorm`、`SceneInfo` 定义 |
| `utils/camera_utils.py` | 相机工具 | `cameraList_from_camInfos`、`camera_to_JSON` |
| `utils/system_utils.py` | 系统工具 | `searchForMaxIteration`（找最新迭代号） |
| `scene/gaussian_model.py` | 高斯模型 | `create_from_pcd` / `create_from_pth` / `load_ply` 三条初始化路径，以及 `training_setup`、`densify_and_clone/split` 中对 `cameras_extent` 的消费 |
| `train.py` | 训练入口 | `Scene` 的实例化位置、`prepare_output_and_logger` 写出的文件 |

## 4. 核心概念与源码讲解

### 4.1 Scene 类：训练世界的装配工厂

#### 4.1.1 概念说明

`Scene` 是 4C4D 训练流程中的「中央装配车间」。它不训练任何东西，也不渲染任何东西，只负责把三样东西组装到位：

1. **相机集合**：把 `SceneInfo` 里的 `CameraInfo` 列表转换成真正可用于渲染的 `Camera` 对象，按训练/测试、按分辨率组织成字典。
2. **场景尺度**：从训练相机位置算出一个标量 `cameras_extent`，交给高斯模型做学习率与致密化阈值缩放。
3. **高斯初值**：决定高斯从哪来——随机初始化自点云（`create_from_pcd`）、从 `.pth` 检查点初始化（`create_from_pth`），还是加载已训练的 ply（`load_ply`）。

为什么需要这样一层？因为 `train.py` 只想问一句「给我训练相机和初始高斯」，不想关心数据是 COLMAP 还是 Blender、图像要不要懒加载、点云要不要下采样。`Scene` 把这些细节全部吸收。

#### 4.1.2 核心流程

`Scene.__init__` 的执行顺序（这条顺序本身就含有重要约束，见源码精读）：

```text
Scene.__init__(args, gaussians)
 1. 记录 model_path / loaded_iter / white_background
 2. 判断数据集类型 → 查 sceneLoadTypeCallbacks 表 → 得到 SceneInfo
      ├─ source_path 下有 sparse/            → readColmapSceneInfo
      ├─ source_path 下有 transforms_train.json → readNerfSyntheticInfo
      └─ 都没有 → assert False
 3. 若是全新训练（loaded_iter 为空）：
      a. 把 source 的 points3D.ply 复制为 model_path/input.ply
      b. 把 test+train 相机逐个转成 JSON，写出 model_path/cameras.json
 4. shuffle 打乱 train/test 相机顺序
 5. cameras_extent = SceneInfo.nerf_normalization["radius"]
 6. 对每个 resolution_scale：cameraList_from_camInfos → self.train_cameras[scale] / self.test_cameras[scale]
 7. 高斯初始化（三选一）：
      a. args.loaded_pth 非空 → create_from_pth
      b. loaded_iter 非空     → load_ply(model_path/point_cloud/iteration_N/point_cloud.ply)
      c. 否则                 → create_from_pcd(SceneInfo.point_cloud, cameras_extent, redundant_ratio)
```

#### 4.1.3 源码精读

构造函数签名与默认参数——注意 `num_pts`、`training_view`、`redundant_ratio`、`downsample_method` 这些 u2-l2 讲过的旋钮都在这里从 `train.py` 透传进数据读取层：[scene/__init__.py:27-30](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/__init__.py#L27-L30)

```python
def __init__(self, args : ModelParams, gaussians : GaussianModel, load_iteration=None, shuffle=True, 
             resolution_scales=[1.0], num_pts=100_000, num_pts_ratio=1.0, time_duration=None, 
             training_view=['cam10', 'cam01', 'cam20', 'cam13'], redundant_ratio=0.2,
             downsample_method='random', testing_view=None):
```

`load_iteration` 的处理：传 `-1` 表示「自动找最新迭代」，靠 `searchForMaxIteration` 扫描 `point_cloud/` 下的 `iteration_N` 目录名取最大值：[scene/__init__.py:39-46](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/__init__.py#L39-L46)，而 [utils/system_utils.py:27-29](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/system_utils.py#L27-L29) 就是那个三行函数：

```python
def searchForMaxIteration(folder):
    saved_iters = [int(fname.split("_")[-1]) for fname in os.listdir(folder)]
    return max(saved_iters)
```

在 `train.py` 中的实例化位置——注意**顺序**：`Scene` 必须先于 `gaussians.training_setup(opt)` 构造，因为 `create_from_pcd` 内部会写入 `self.spatial_lr_scale`（见 4.3 节），而 `training_setup` 要读它来算位置学习率：[train.py:62-67](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L62-L67)

```python
gaussians = GaussianModel(dataset.sh_degree, gaussian_dim=gaussian_dim, ...)
scene = Scene(dataset, gaussians, num_pts=num_pts, num_pts_ratio=num_pts_ratio, 
              time_duration=time_duration, training_view=args.training_view, testing_view=args.testing_view,
              redundant_ratio=args.redundant_ratio, downsample_method=args.downsample_method)
gaussians.training_setup(opt)   # ← 必须在 Scene 之后：training_setup 依赖 spatial_lr_scale
```

`Scene` 还暴露四个相机访问接口，全部返回 `CameraDataset`（u2-l3 讲过的懒加载包装），其中 `getTrainCameras` 是训练循环的数据来源、`getAllCameras` 供轨迹渲染插值使用：[scene/__init__.py:114-127](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/__init__.py#L114-L127)

```python
def getTrainCameras(self, scale=1.0):
    return CameraDataset(self.train_cameras[scale].copy(), self.white_background)
...
def getAllCameras(self, scale=1.0):
    return CameraDataset(self.train_cameras[scale].copy() + self.test_cameras[scale].copy(), self.white_background)
```

`save` 方法是检查点写出的唯一入口——一个 `.pth`（含 `capture()` 元组与迭代号）加一个 ply：[scene/__init__.py:109-112](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/__init__.py#L109-L112)

```python
def save(self, iteration):
    torch.save((self.gaussians.capture(), iteration), self.model_path + "/chkpnt" + str(iteration) + ".pth")
    point_cloud_path = os.path.join(self.model_path, "point_cloud/iteration_{}".format(iteration))
    self.gaussians.save_ply(os.path.join(point_cloud_path, "point_cloud.ply"))
```

#### 4.1.4 代码实践

**实践目标**：不跑完整训练，仅通过阅读 + 一次轻量实例化，确认 `Scene` 的构造顺序与「先 Scene 后 training_setup」的依赖关系。

**操作步骤**：

1. 打开 [scene/__init__.py:27-107](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/__init__.py#L27-L107)，在纸上（或注释里）给 `__init__` 的 7 个阶段标号（见 4.1.2 的流程图）。
2. 打开 [train.py:62-67](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L62-L67)，验证 `GaussianModel` → `Scene` → `training_setup` 的调用顺序。
3. 做一个思想实验：如果把 `train.py` 里的 `scene = Scene(...)` 与 `gaussians.training_setup(opt)` 两行对调，会发生什么？提示：沿着 `create_from_pcd`（[scene/gaussian_model.py:406-407](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L406-L407) 里 `self.spatial_lr_scale = spatial_lr_scale`）与 `training_setup`（[scene/gaussian_model.py:485](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L485) 里 `training_args.position_lr_init * self.spatial_lr_scale`）追踪。

**需要观察的现象 / 预期结果**：对调后 `training_setup` 读到的 `self.spatial_lr_scale` 仍是 `GaussianModel.__init__` 里的初值 `0`（见 [scene/gaussian_model.py:78](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L78)），位置学习率会变成 0，训练中高斯位置完全不动——一个典型的「静默失败」。此结论可由代码静态推出，**待本地验证**（需要 GPU 数据才能实际跑）。

#### 4.1.5 小练习与答案

**练习 1**：`Scene` 类自己保存了高斯张量吗？

**答案**：没有。`Scene` 只持有 `self.gaussians` 这个 `GaussianModel` 的引用（[scene/__init__.py:36](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/__init__.py#L36)），真正的高斯属性（`_xyz`、`_t` 等）都在 `GaussianModel` 里。`Scene` 的职责是「装配」而非「持有数据」。

**练习 2**：`searchForMaxIteration` 为什么能工作？它对目录命名有什么隐含要求？

**答案**：它把 `point_cloud/` 下每个子目录名按 `_` 切开取最后一段转 int 再取 max（[utils/system_utils.py:27-29](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/system_utils.py#L27-L29)）。这要求子目录必须严格命名为 `iteration_<数字>`——正是 `Scene.save` 里 `os.path.join(self.model_path, "point_cloud/iteration_{}".format(iteration))` 写出的格式。若手工在 `point_cloud/` 下放了别的名字的目录，转 int 会抛 `ValueError`。

**练习 3**：`getTrainCameras` 返回前为什么要 `.copy()`？

**答案**：`CameraDataset` 包装的是列表引用；`DataLoader` 的 `shuffle=True` 不会改列表，但训练中任何对返回列表的原位修改（append/remove）若不 copy 就会污染 `self.train_cameras[scale]`，影响后续再次取相机（例如 `training_report` 里取测试相机）的一致性。`.copy()` 是浅拷贝，代价很小。

### 4.2 sceneLoadTypeCallbacks：按目录特征自动分发数据集

#### 4.2.1 概念说明

4C4D 要支持至少两类数据：真实采集的 COLMAP 格式（N3V/ DyNeRF 类多相机视频）与 Blender 合成数据（NeRF synthetic 格式）。两者的文件组织完全不同，但 `Scene` 不想知道这些差异。解决方案是模块级字典 `sceneLoadTypeCallbacks`——一个极简的策略模式：

- **Colmap**：`sparse/` 目录 + `camXX_YYYY.png` 图像，相机固定、按帧展开（u2-l2 全讲）。
- **Blender**：`transforms_train.json` / `transforms_test.json` 声明每帧的 C2W 矩阵，无点云时随机生成初始点。

分发依据不是配置项，而是**目录探测**：`source_path` 下存在 `sparse` 目录就当 Colmap，存在 `transforms_train.json` 就当 Blender，两者都没有则直接断言失败。

#### 4.2.2 核心流程

```text
os.path.exists(source_path/sparse)          ──是──▶ sceneLoadTypeCallbacks["Colmap"](path, images, eval,
│                                                    num_pts_ratio, training_cam, testing_cam,
│                                                    num_pts, time_duration, downsample_method)
否
│
os.path.exists(source_path/transforms_train.json) ─是─▶ sceneLoadTypeCallbacks["Blender"](path, white_background,
│                                                       eval, num_pts, time_duration, extension,
│                                                       num_extra_pts, frame_ratio, dataloader)
否
└──▶ assert False, "Could not recognize scene type!"
```

注意两个回调的**参数签名不同**：Colmap 侧关心视角划分与下采样方式，Blender 侧关心背景色、文件扩展名、额外环境点（`num_extra_pts`，在远处球面上撒点当背景）与帧率缩放。分发处把各自需要的参数硬编码地传过去。

#### 4.2.3 源码精读

注册表本体，位于 `dataset_readers.py` 末尾——整份文件先定义两个读取函数，最后两行完成注册：[scene/dataset_readers.py:535-538](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L535-L538)

```python
sceneLoadTypeCallbacks = {
    "Colmap": readColmapSceneInfo,
    "Blender" : readNerfSyntheticInfo
}
```

分发逻辑：[scene/__init__.py:51-60](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/__init__.py#L51-L60)

```python
if os.path.exists(os.path.join(args.source_path, "sparse")):
    scene_info = sceneLoadTypeCallbacks["Colmap"](args.source_path, args.images, args.eval, num_pts_ratio=num_pts_ratio, 
                                                  training_cam=training_view, testing_cam=testing_view, num_pts=num_pts, time_duration=time_duration,
                                                  downsample_method=downsample_method)
    print(f"Found sparse folder in {args.source_path}, assuming Colmap data set!")
elif os.path.exists(os.path.join(args.source_path, "transforms_train.json")):
    print(f"Found transforms_train.json file in {args.source_path}, assuming Blender data set!")
    scene_info = sceneLoadTypeCallbacks["Blender"](...)
else:
    assert False, "Could not recognize scene type!"
```

两个回调的**共同产出契约**是 `SceneInfo` 这个 NamedTuple——这是它们能互换的关键：[scene/dataset_readers.py:60-65](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L60-L65)

```python
class SceneInfo(NamedTuple):
    point_cloud: BasicPointCloud
    train_cameras: list
    test_cameras: list
    nerf_normalization: dict
    ply_path: str
```

Colmap 回调在函数末尾打包返回 `SceneInfo`（点云可能已经过 `num_pts`/`num_pts_ratio` 加工）：[scene/dataset_readers.py:346-351](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L346-L351)；Blender 回调 `readNerfSyntheticInfo` 无 COLMAP 数据时在 `[-1.3, 1.3]` 立方体内随机撒 `num_pts` 个点并写成 ply：[scene/dataset_readers.py:466-475](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L466-L475)。

一个容易踩的坑：**判断优先级是 sparse 在前**。若某个数据目录同时含 `sparse/` 与 `transforms_train.json`（例如做格式转换的中间产物），会被当作 Colmap 处理，`transforms_train.json` 被静默忽略。

#### 4.2.4 代码实践

**实践目标**：用两个手工构造的最小目录，验证分发规则的三条分支。

**操作步骤**：

1. 新建空目录 `fake_colmap/`，在里面 `mkdir -p sparse/0`，再建 `fake_blender/`，放一个只含 `{"frames": []}` 的 `transforms_train.json`（内容随意，只要文件名对）。
2. 在 Python 里执行（示例代码）：

```python
import os
# 示例代码：只复现 Scene 的分发判断，不真正实例化 Scene
for p in ["fake_colmap", "fake_blender", "not_a_dataset"]:
    has_sparse = os.path.exists(os.path.join(p, "sparse"))
    has_blender = os.path.exists(os.path.join(p, "transforms_train.json"))
    kind = "Colmap" if has_sparse else ("Blender" if has_blender else "断言失败")
    print(f"{p}: {kind}")
```

3. 观察三个目录分别命中哪条分支。

**需要观察的现象 / 预期结果**：输出依次为 `Colmap`、`Blender`、`断言失败`，与 [scene/__init__.py:51-60](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/__init__.py#L51-L60) 的 if/elif/else 一一对应。注意第 2 步只复现了判断条件，并未调用读取函数——真正实例化 `Scene` 需要完整的 `sparse/0` 三件套与图像目录，那正是 4.5 节综合实践要做的事。

#### 4.2.5 小练习与答案

**练习 1**：想新增一种数据格式（例如自家的 jsonl 标定），最少要改几处？

**答案**：两处。① 在 `dataset_readers.py` 写一个返回 `SceneInfo` 的读取函数；② 在 `sceneLoadTypeCallbacks` 字典（[scene/dataset_readers.py:535-538](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L535-L538)）加一项。③ 还需在 `Scene.__init__` 的 if/elif 里加一个目录特征探测分支（[scene/__init__.py:51-60](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/__init__.py#L51-L60)）——严格说是三处，这正是 u8-l4 二次开发实践的入门任务。

**练习 2**：为什么 `Scene` 不把 `if/else` 直接写成调用 `readColmapSceneInfo` / `readNerfSyntheticInfo`？

**答案**：查表调用让 `scene/__init__.py` 只依赖「键名 → 函数」这一约定，不 import 具体读取函数的符号（实际上它只 import 了 `sceneLoadTypeCallbacks` 这一个名字，见 [scene/__init__.py:17](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/__init__.py#L17)）。新增格式时 `Scene` 的分发处仍需加分支，但读取函数的组装完全解耦，也方便在外部测试时替换注册表里的函数做 mock。

### 4.3 getNerfppNorm 与 cameras_extent：一个数字的两处消费

#### 4.3.1 概念说明

`cameras_extent` 是 `Scene` 从 `SceneInfo.nerf_normalization["radius"]` 取出的一个标量，语义是「**相机包围球的半径再放大 1.1 倍**」。它是整个训练里唯一的全局场景尺度量，被两处消费：

1. **位置学习率缩放**（`spatial_lr_scale`）：xyz 与时间位置 `_t` 的 Adam 学习率、以及 xyz 的指数衰减调度都乘上它。直觉：大场景里高斯要「走更远的路」，步子应当更大；这样同一套 `position_lr_init` 超参可以跨场景复用。
2. **致密化尺寸分界**（`percent_dense * scene_extent`）：clone 与 split 用同一个公式比较高斯的最大尺度，一个判「太小」、一个判「太大」，本讲只讲量从哪来，机制细节留给 u5-l4。

#### 4.3.2 核心流程

`getNerfppNorm` 的数学定义。设训练相机光心为 \( C_1,\dots,C_N \)（3 维列向量），均值为 \( \bar{C} \)，则：

\[
\text{radius} = 1.1 \times \max_i \lVert C_i - \bar{C} \rVert_2, \qquad \text{translate} = -\bar{C}
\]

`cameras_extent` 就是这个 `radius`。注意它**只由训练相机计算**（u2-l2 的 `getNerfppNorm(train_cam_infos)`），测试相机不参与——否则留出相机的位置会改变训练超参，评估就不再可控。

光心的求法：`CameraInfo` 里存的是 C2W 旋转 `R`（转置过的）与 W2C 平移 `T`，先拼回 W2C 再求逆得到 C2W，取其平移列即光心：

\[
C = \left(\text{W2C}^{-1}\right)_{[:3,\,3]}
\]

下游消费链（两条）：

```text
cameras_extent ──▶ create_from_pcd(pcd, spatial_lr_scale=cameras_extent)  # 存为 self.spatial_lr_scale
                        │
                        ├─▶ training_setup: xyz lr = position_lr_init × spatial_lr_scale   (L485)
                        ├─▶ training_setup: _t   lr = position_t_lr_init × spatial_lr_scale (L496)
                        └─▶ xyz_scheduler: lr_init / lr_final 同乘 spatial_lr_scale       (L506-509)

cameras_extent ──▶ train.py densify_and_prune(..., scene.cameras_extent, ...)
                        ├─▶ densify_and_clone: max_scaling ≤ percent_dense × extent → clone (L727)
                        └─▶ densify_and_split: max_scaling > percent_dense × extent → split (L683)
```

#### 4.3.3 源码精读

`getNerfppNorm` 全文——先收集光心，再算中心与最大距离，半径乘 1.1：[scene/dataset_readers.py:67-88](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L67-L88)

```python
def getNerfppNorm(cam_info):
    def get_center_and_diag(cam_centers):
        cam_centers = np.hstack(cam_centers)
        avg_cam_center = np.mean(cam_centers, axis=1, keepdims=True)
        center = avg_cam_center
        dist = np.linalg.norm(cam_centers - center, axis=0, keepdims=True)
        diagonal = np.max(dist)
        return center.flatten(), diagonal

    cam_centers = []
    for cam in cam_info:
        W2C = getWorld2View2(cam.R, cam.T)
        C2W = np.linalg.inv(W2C)
        cam_centers.append(C2W[:3, 3:4])

    center, diagonal = get_center_and_diag(cam_centers)
    radius = diagonal * 1.1
    translate = -center
    return {"translate": translate, "radius": radius}
```

`Scene` 侧只取 radius 一个键（`translate` 在本项目里没有被 `Scene` 使用）：[scene/__init__.py:84](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/__init__.py#L84)

```python
self.cameras_extent = scene_info.nerf_normalization["radius"]
```

消费点一：`create_from_pcd` 把它存进 `spatial_lr_scale`：[scene/gaussian_model.py:406-407](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L406-L407)，随后 `training_setup` 用它乘三处：[scene/gaussian_model.py:484-485](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L484-L485)（xyz 初始学习率）、[scene/gaussian_model.py:496](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L496)（时间位置 `_t` 学习率）、[scene/gaussian_model.py:506-509](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L506-L509)（指数衰减调度器）：

```python
l = [
    {'params': [self._xyz], 'lr': training_args.position_lr_init * self.spatial_lr_scale, "name": "xyz"},
    ...
]
...
l.append({'params': [self._t], 'lr': training_args.position_t_lr_init * self.spatial_lr_scale, "name": "t"})
...
self.xyz_scheduler_args = get_expon_lr_func(lr_init=training_args.position_lr_init*self.spatial_lr_scale,
                                            lr_final=training_args.position_lr_final*self.spatial_lr_scale, ...)
```

消费点二：训练循环把 `scene.cameras_extent` 传给致密化：[train.py:251](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L251)，clone 与 split 各自的尺寸判据：[scene/gaussian_model.py:723-727](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L723-L727) 与 [scene/gaussian_model.py:676-683](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L676-L683)

```python
# densify_and_clone：小高斯 + 梯度大 → 复制一份
selected_pts_mask = torch.logical_and(selected_pts_mask,
                                      torch.max(self.get_scaling, dim=1).values <= self.percent_dense*scene_extent)
# densify_and_split：大高斯 + 梯度大 → 一分为 N
selected_pts_mask = torch.logical_and(selected_pts_mask,
                                      torch.max(self.get_scaling, dim=1).values > self.percent_dense*scene_extent)
```

一个值得记住的不对称：`create_from_pth` 同样会写 `spatial_lr_scale`（[scene/gaussian_model.py:450-452](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L450-L452)），但 `load_ply` **不会**（[scene/gaussian_model.py:281-347](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L281-L347) 全文没有赋值语句）。推理路径（render.py 的 `restore` 传 `training_args=None`）不跑 `training_setup`，所以不受影响；但如果你想「从 ply 继续训练」，要意识到 `spatial_lr_scale` 还是 0，需要自己补。

#### 4.3.4 代码实践

**实践目标**：手工复算 `cameras_extent`，并量化它对位置学习率的影响。

**操作步骤**：

1. 构造 4 个相机光心（模拟 4 相机环形阵列），按 `getNerfppNorm` 公式手算 radius（示例代码）：

```python
# 示例代码：复现 getNerfppNorm 的数学定义（dataset_readers.py:67-88）
import numpy as np
C = np.array([[1,0,0], [0,1,0], [-1,0,0], [0,-1,0]], dtype=np.float64).T  # 4 个光心按列堆
center = C.mean(axis=1, keepdims=True)
radius = np.linalg.norm(C - center, axis=0).max() * 1.1
print("cameras_extent =", radius)   # 预期 1.1
```

2. 查一下 configs 里 `position_lr_init` 的值（如 `configs/dynerf/flame_steak.yaml` 中的 `position_lr_init`），代入公式
   \(\text{lr}_{xyz} = \text{position\_lr\_init} \times \text{cameras\_extent}\)
   算出实际初始学习率。
3. 把 4 个光心整体放大 10 倍再算一次，观察 `cameras_extent` 与学习率如何同比例变化。

**需要观察的现象 / 预期结果**：第 1 步输出 `1.1`（到中心的距离全是 1，乘 1.1）；第 3 步光心放大 10 倍后 `cameras_extent` 变为 `11.0`，学习率同样放大 10 倍——这正是「场景尺度自动归一化超参」的含义。第 2 步的具体数值取决于你读到的 yaml 值，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：如果留出的测试相机离场景中心比训练相机远得多，把它也算进 `getNerfppNorm` 会怎样？

**答案**：`radius` 变大 → `cameras_extent` 变大 → 位置学习率变大、clone/split 尺寸分界变大，训练行为被「评估用相机」改变。代码因此在 `readColmapSceneInfo` 里只传 `train_cam_infos`（[scene/dataset_readers.py:291](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L291)），保证训练超参只由训练视角决定。

**练习 2**：为什么半径要乘 1.1 而不是精确的最大距离？

**答案**：给包围球留 10% 的余量，避免恰好位于球面的相机让量度「贴边」；这是从 3DGS/NeRF++ 沿袭的经验值，属于工程缓冲，没有严格推导。

**练习 3**：`percent_dense` 默认约 0.01，`cameras_extent` 为 1.1 时，多大的高斯会被 clone 而不是 split？

**答案**：clone 判据是 `max_scaling ≤ percent_dense × scene_extent`（[scene/gaussian_model.py:727](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L727)），即最大尺度 ≤ 0.011 的高斯走 clone，超过的走 split——分界线随 `cameras_extent` 线性移动，大场景里「小高斯」的定义也更宽松。

### 4.4 cameraList_from_camInfos：从数据描述到渲染对象

#### 4.4.1 概念说明

`SceneInfo.train_cameras` 里躺着的是 `CameraInfo`（纯数据描述，`image` 字段还是 None——u2-l2 讲过位姿模板机制），而渲染管线需要的是 `Camera` 对象（带投影矩阵、原始图像尺寸、时间戳等）。`cameraList_from_camInfos` 就是这层转换的循环壳子，真正的转换在 `loadCam`（u2-l3 精讲过分辨率协商与懒加载）。`Scene` 用两层容器组织转换结果：

- 外层字典：键是 `resolution_scale`（默认只有 `1.0`，多分辨率训练时会有多个键）；
- 内层列表：该分辨率下的 `Camera` 对象，训练/测试分开。

`cameras.json` 的写出也发生在这个阶段附近：`camera_to_JSON` 把每个 `CameraInfo` 转成可序列化字典（重算 C2W 得到 position/rotation，焦距用 `fov2focal` 从 Fov 反推）。

#### 4.4.2 核心流程

```text
对每个 resolution_scale（默认 [1.0]）:
    self.train_cameras[scale] = cameraList_from_camInfos(scene_info.train_cameras, scale, args)
        └─ 对每个 CameraInfo: loadCam(args, id, c, scale) → Camera(...)   # u2-l3 已精读
    self.test_cameras[scale]  = cameraList_from_camInfos(scene_info.test_cameras,  scale, args)

写出 cameras.json:
    camlist = test_cameras + train_cameras（先 test 后 train，id 依次递增）
    对每个 cam: camera_to_JSON(id, cam) → dict
    json.dump 到 model_path/cameras.json
```

注意 `loadCam` 在 `dataloader=True` 时不读图像（`meta_only`），所以这一步对上千帧的 N3V 数据也很快——图像的真正读取发生在 u2-l3 讲的 `CameraDataset.__getitem__`。

#### 4.4.3 源码精读

`cameraList_from_camInfos` 本体，就是「带编号的 loadCam 循环」：[utils/camera_utils.py:71-77](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/camera_utils.py#L71-L77)

```python
def cameraList_from_camInfos(cam_infos, resolution_scale, args):
    camera_list = []
    for id, c in enumerate(cam_infos):
        camera_list.append(loadCam(args, id, c, resolution_scale))
    return camera_list
```

`Scene` 里的多分辨率循环——先打乱（两个列表各自 shuffle，但发生在转换之前，保证同一随机顺序下转换出的 Camera 序列一致）：[scene/__init__.py:78-91](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/__init__.py#L78-L91)

```python
if shuffle:
    print("Shuffling training and testing cameras")
    random.shuffle(scene_info.train_cameras)  # Multi-res consistent random shuffling
    random.shuffle(scene_info.test_cameras)  # Multi-res consistent random shuffling
    print("Shuffling done.")

self.cameras_extent = scene_info.nerf_normalization["radius"]

for resolution_scale in resolution_scales:
    print(f"Loading cameras at resolution scale {resolution_scale}")
    self.train_cameras[resolution_scale] = cameraList_from_camInfos(scene_info.train_cameras, resolution_scale, args)
    ...
    self.test_cameras[resolution_scale] = cameraList_from_camInfos(scene_info.test_cameras, resolution_scale, args)
```

注释「Multi-res consistent random shuffling」的含义：shuffle 只做一次、放在 `resolution_scales` 循环之外，所有分辨率共用同一个打乱后的 `CameraInfo` 顺序，多分辨率之间可以按索引对齐。

`camera_to_JSON`——把 R（存的 C2W 旋转）转置回 W2C 拼成 4×4，再求逆得 C2W，取平移与旋转写 JSON；焦距由 Fov 反算：[utils/camera_utils.py:79-99](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/camera_utils.py#L79-L99)

```python
def camera_to_JSON(id, camera : Camera):
    Rt = np.zeros((4, 4))
    Rt[:3, :3] = camera.R.transpose()
    Rt[:3, 3] = camera.T
    Rt[3, 3] = 1.0
    W2C = np.linalg.inv(Rt)
    pos = W2C[:3, 3]
    rot = W2C[:3, :3]
    ...
    camera_entry = {
        'id' : id, 'img_name' : camera.image_name,
        'width' : camera.width, 'height' : camera.height,
        'position': pos.tolist(), 'rotation': serializable_array_2d,
        'fy' : fov2focal(camera.FovY, camera.height),
        'fx' : fov2focal(camera.FovX, camera.width)
    }
    return camera_entry
```

`Scene` 里写出 `input.ply` 与 `cameras.json` 的完整代码段——只在「全新训练」时执行（`if not self.loaded_iter:`），加载已有模型时不覆盖：[scene/__init__.py:62-76](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/__init__.py#L62-L76)

```python
if not self.loaded_iter:
    with open(scene_info.ply_path, 'rb') as src_file, open(os.path.join(self.model_path, "input.ply") , 'wb') as dest_file:
        dest_file.write(src_file.read())          # 复制初始点云
    json_cams = []
    camlist = []
    if scene_info.test_cameras:  camlist.extend(scene_info.test_cameras)
    if scene_info.train_cameras: camlist.extend(scene_info.train_cameras)
    for id, cam in enumerate(camlist):
        json_cams.append(camera_to_JSON(id, cam))
    with open(os.path.join(self.model_path, "cameras.json"), 'w') as file:
        json.dump(json_cams, file)
```

#### 4.4.4 代码实践

**实践目标**：亲手检查 `cameras.json` 的内容结构，验证它记录的是 C2W 位姿而非 W2C。

**操作步骤**：

1. 跑通 4.5 节综合实践的假数据脚本后（或对任何一次历史训练的输出目录），打开 `model_path/cameras.json`。
2. 数一下条目数量，应等于 `len(test_cameras) + len(train_cameras)`，且前若干条是 test 相机（写出顺序 test 在前）。
3. 任取一条，检查 `position` 是否与该相机的光心一致：若手头有 `sparse/0` 的位姿，可用 u2-l1 的 `qvec2rotmat` 拼出 W2C 再求逆对照；或直接验证 `rotation × (position - X) 形式的投影关系`（示例代码）：

```python
# 示例代码：用 cameras.json 的一条记录做投影自检
import numpy as np, json
entry = json.load(open("output/xxx/cameras.json"))[0]
R_c2w = np.array(entry["rotation"]); t_c2w = np.array(entry["position"])
w2c = np.linalg.inv(np.vstack([np.hstack([R_c2w, t_c2w[:,None]]), [0,0,0,1]]))
print("W2C 平移（应接近 COLMAP 的 tvec）:", w2c[:3,3])
```

**需要观察的现象 / 预期结果**：`cameras.json` 每条含 `id/img_name/width/height/position/rotation/fy/fx` 八个键；`position` 是相机在世界系的位置（C2W 平移）。若没有 GPU 数据，第 3 步的对照**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`resolution_scales=[1.0]` 时字典里有几个键？改成 `[1.0, 0.5]` 呢？

**答案**：`self.train_cameras` 与 `self.test_cameras` 各有 1 个键 `1.0`；改成两个 scale 后各有 `1.0` 和 `0.5` 两个键，图像按 u2-l3 的 loadCam 协商分别缩放，可通过 `scene.getTrainCameras(scale=0.5)` 取出。

**练习 2**：`cameras.json` 里的 `fx/fy` 是怎么来的？为什么不用 `CameraInfo` 里现成的 `fl_x/fl_y`？

**答案**：由 `fov2focal(camera.FovY, camera.height)` / `fov2focal(camera.FovX, camera.width)` 从视场角反算（[utils/camera_utils.py:96-97](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/camera_utils.py#L96-L97)）。COLMAP 路径的 `CameraInfo` 默认 `fl_x=-1`（见 [scene/dataset_readers.py:55-58](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L55-L58) 的默认值与 `readColmapCameras` 的构造，[scene/dataset_readers.py:123-128](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L123-L128)），真正的焦距信息在 `FovX/FovY` 里，所以 JSON 从 Fov 反推。

**练习 3**：为什么 `cameras.json` 的写出顺序是先 test 后 train？

**答案**：代码先 `extend(test_cameras)` 再 `extend(train_cameras)`（[scene/__init__.py:68-71](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/__init__.py#L68-L71)），因此 `id` 编号从 0 起先是测试相机。这是从 3DGS 继承的固定顺序，本身没有功能含义，但写脚本解析 `cameras.json` 时若想区分训练/测试相机，不能靠 id 区间，只能靠 `img_name` 前缀（如 `cam09_0000`）对照 `training_view` 判断。

### 4.5 输出目录与初始化三分支

#### 4.5.1 概念说明

一次全新训练会在 `model_path` 下生成一组文件，`Scene` 负责其中三个（`input.ply`、`cameras.json`、`point_cloud/iteration_N/point_cloud.ply` + `chkpntN.pth`），`train.py` 负责其余。完整清单：

| 文件/目录 | 写出者 | 内容 | 写出时机 |
| --- | --- | --- | --- |
| `cfg_args` | `train.py` `prepare_output_and_logger` | Namespace 字符串（全部参数快照） | 训练开始前 |
| `training_params.txt` | `train.py`（[train.py:476](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L476)） | 训练参数文本 | 训练开始前 |
| `input.ply` | `Scene.__init__` | 初始点云的逐字节副本 | Scene 构造时（仅全新训练） |
| `cameras.json` | `Scene.__init__` | 全部相机的 C2W 位姿与焦距 | Scene 构造时（仅全新训练） |
| `chkpntN.pth` | `Scene.save` | `gaussians.capture()` 元组 + 迭代号 | 每个 save_iteration 与 best |
| `point_cloud/iteration_N/point_cloud.ply` | `Scene.save` | 当前高斯全体属性 | 同上 |
| TensorBoard 事件文件 | `tb_writer` | 损失/PSNR/点数曲线 | 训练全程 |
| `rendered_images/` 等 | `training_report` | 测试渲染中间结果 | 测试迭代点 |

高斯初始化的三条分支，优先级从高到低：

1. **`args.loaded_pth` 非空 → `create_from_pth`**：从 4DGS 官方格式的 `.pth` 字典加载全部属性，要求 `gaussian_dim == 4 and rot_4d`（[scene/gaussian_model.py:450-452](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L450-L452)）。用于「拿别处训练好的 4D 高斯当初始化」。
2. **`loaded_iter` 非空 → `load_ply`**：加载 `model_path/point_cloud/iteration_N/point_cloud.ply`，即从自己之前的训练续跑/评估。
3. **默认 → `create_from_pcd`**：从 `SceneInfo.point_cloud`（COLMAP/MASt3R 点云或随机点）全新初始化，细节由 u3-l5 精讲。

#### 4.5.2 核心流程

```text
if args.loaded_pth:                       # 分支 1：外部 4D 高斯
    gaussians.create_from_pth(args.loaded_pth, cameras_extent)
elif loaded_iter:                         # 分支 2：本目录已训练模型
    gaussians.load_ply(model_path/point_cloud/iteration_<N>/point_cloud.ply)
else:                                     # 分支 3：全新训练
    gaussians.create_from_pcd(scene_info.point_cloud, cameras_extent, redundant_ratio)

注意：分支 2 跳过 input.ply/cameras.json 的写出（if not self.loaded_iter 守卫）
     分支 1 不受 loaded_iter 影响，仍会写出这两个文件
```

#### 4.5.3 源码精读

三分支原文：[scene/__init__.py:93-107](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/__init__.py#L93-L107)

```python
if args.loaded_pth:
    print(f"Loading gaussians from {args.loaded_pth}")
    self.gaussians.create_from_pth(args.loaded_pth, self.cameras_extent)
else:
    if self.loaded_iter:
        print(f"Loading gaussians from trained model's point cloud at iteration {self.loaded_iter}")
        self.gaussians.load_ply(os.path.join(self.model_path, "point_cloud",
                                             "iteration_" + str(self.loaded_iter), "point_cloud.ply"))
    else:
        print(f"Creating gaussians from initial point cloud input.ply")
        self.gaussians.create_from_pcd(scene_info.point_cloud, self.cameras_extent,
                                       redundant_ratio=redundant_ratio)
```

分支 3 的被调方开头——注意第二行就把 `cameras_extent` 存为 `spatial_lr_scale`，这正是 4.3 节消费链的起点：[scene/gaussian_model.py:406-408](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L406-L408)

```python
def create_from_pcd(self, pcd : BasicPointCloud, spatial_lr_scale : float, redundant_ratio = 0.2):
    self.spatial_lr_scale = spatial_lr_scale
    fused_point_cloud = torch.tensor(np.asarray(pcd.points)).float().cuda()
```

分支 1 的被调方——同样是第一件事就写 `spatial_lr_scale`，且断言只支持 4D+rot_4d：[scene/gaussian_model.py:450-453](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L450-L453)

```python
def create_from_pth(self, path, spatial_lr_scale):
    assert self.gaussian_dim == 4 and self.rot_4d
    self.spatial_lr_scale = spatial_lr_scale
    init_4d_gaussian = torch.load(path)
```

`train.py` 侧 `model_path` 的确立与 `cfg_args` 写出：[train.py:283-295](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L283-L295)

```python
def prepare_output_and_logger(args):    
    if not args.model_path:
        ...
        unique_str = str(uuid.uuid4())
        args.model_path = os.path.join("./output/", unique_str[0:10])
    os.makedirs(args.model_path, exist_ok = True)
    with open(os.path.join(args.model_path, "cfg_args"), 'w') as cfg_log_f:
        cfg_log_f.write(str(Namespace(**vars(args))))
```

训练循环里 `scene.save` 的调用点（到达 `save_iterations` 或末尾迭代时）：[train.py:281](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L281)。

#### 4.5.4 代码实践

**实践目标**：用假 COLMAP 目录实例化 `Scene`（`gaussian_dim=4`），亲眼看到 `model_path` 下生成 `input.ply` 与 `cameras.json`，并打印 `cameras_extent`。（本讲综合实践的前置小步，这里先给出最小骨架，完整可跑版本见第 5 节。）

**操作步骤**：

1. 准备目录结构（示例代码，生成假数据）：

```python
# 示例代码：构造最小假 COLMAP 目录
# mydata/sparse/0/{cameras.bin,images.bin,points3D.bin} 用 u2-l1 的格式手工写入
# mydata/images/cam00_0000.png ... 若干纯色小图
```

2. 实例化（示例代码）：

```python
# 示例代码：最小 Scene 实例化骨架（需 GPU：create_from_pcd 内部 .cuda()）
import argparse
from scene import Scene
from scene.gaussian_model import GaussianModel
from arguments import ModelParams

parser = argparse.ArgumentParser()
mp = ModelParams(parser, sentinel=False)          # u1-l4 讲过 ParamGroup 机制
args = parser.parse_args(["--source_path", "mydata", "--model_path", "output/fake_run",
                          "--images", "images", "--eval", "1"]).extract(args)
gaussians = GaussianModel(sh_degree=3, gaussian_dim=4, time_duration=[0, 10])
scene = Scene(args, gaussians, num_pts=1000, time_duration=[0, 10],
              training_view=['cam00'], testing_view=['cam01'])
print("cameras_extent =", scene.cameras_extent)
```

3. 列出 `output/fake_run/` 下的文件，打开 `cameras.json` 数条目、对 `input.ply` 用 `plyfile` 读顶点数。

**需要观察的现象 / 预期结果**：`output/fake_run/` 下出现 `input.ply`（与 `sparse/0/points3D.ply` 字节数相同）与 `cameras.json`（条目数 = test 帧数 + train 帧数，先 test 后 train）；终端打印 `cameras_extent` 为某正数；随后 `create_from_pcd` 打印 `Number of points at initialisation : <点数>`。整条链依赖 CUDA（`create_from_pcd` 直接 `.cuda()`），无 GPU 环境**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：`load_iteration=-1` 与 `args.loaded_pth` 同时设置时会发生什么？

**答案**：`loaded_pth` 优先——分支 1 在最外层 `if`（[scene/__init__.py:93-95](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/__init__.py#L93-L95)），`loaded_iter` 只影响 else 内部。同时由于 `loaded_iter` 非空，`input.ply`/`cameras.json` 不会写出（`if not self.loaded_iter` 守卫）。

**练习 2**：为什么 `load_ply` 分支不影响继续训练的学习率？

**答案**：严格说**会影响**——`load_ply` 不写 `spatial_lr_scale`（4.3.3 节末尾指出的不对称），若在 `load_ply` 后调用 `training_setup`，位置学习率会按 `spatial_lr_scale=0` 计算。本项目推理路径 `restore(model_params, None)` 跳过 `training_setup` 所以无碍；想基于 ply 续训需要手动补 `gaussians.spatial_lr_scale = scene.cameras_extent`。

**练习 3**：`input.ply` 和 `point_cloud/iteration_30000/point_cloud.ply` 都在 `model_path` 下，区别是什么？

**答案**：前者是 `Scene.__init__` 从数据源复制的**初始点云**（[scene/__init__.py:63-65](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/__init__.py#L63-L65)，只有 x/y/z/rgb/nx/ny/nz 字段）；后者是 `Scene.save` 写出的**训练后高斯**（[scene/__init__.py:109-112](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/__init__.py#L109-L112)，含 opacity/SH/尺度/旋转/时间等全部可优化属性，字段布局见 u3-l5）。

## 5. 综合实践

**任务**：写一个独立脚本 `scene_smoke_test.py`（放在教程目录或任意非源码位置，不要放进仓库源码树），用假 COLMAP 数据走通 `Scene` 构造，并产出一份「Scene 构造报告」。

具体要求：

1. **构造假数据**（复用 u2-l1/u2-l2 的知识）：
   - `sparse/0/cameras.bin`：2 台 SIMPLE_PINHOLE 相机（宽高 64×64，焦距 64）；
   - `sparse/0/images.bin`：每台相机 1 条外参记录，名字分别取 `cam00_0000`、`cam01_0000`（注意 `readColmapCameras` 用文件名前缀匹配相机，见 u2-l2）；
   - `sparse/0/points3D.bin`：约 500 个随机点（首次运行时 `readColmapSceneInfo` 会自动转成 `points3D.ply`）；
   - `images/`：`cam00_0000.png`、`cam01_0000.png` 各一张 64×64 纯色图。
   - 写 bin 文件时参考 u2-l1 的字节布局：`struct.pack` 小端序，`images.bin` 每条记录 `qvec(4d)+tvec(3d)+camera_id(i)+文件名+c00+track数(q=0)`。
2. **实例化并报告**：按 4.5.4 的骨架构造 `ModelParams` 与 `GaussianModel(gaussian_dim=4, time_duration=[0,10])`，`training_view=['cam00']`、`testing_view=['cam01']`、`num_pts=400`，然后打印：
   - `scene.cameras_extent`；
   - `len(scene.train_cameras[1.0])` 与 `len(scene.test_cameras[1.0])`（各应为 1）；
   - 按公式 \(1.1\max_i\lVert C_i-\bar C\rVert\) 手算的 radius，与 `cameras_extent` 对照；
   - `position_lr_init × cameras_extent` 的值（`position_lr_init` 取自任一 yaml）。
3. **检查产物**：列出 `model_path` 目录树，用 `plyfile` 打开 `input.ply` 打印顶点数与字段名，`json.load` 打开 `cameras.json` 打印条目数与第一条的 `position`。

**预期结果**：`cameras_extent` 与手算 radius 完全一致；`input.ply` 顶点数等于（下采样后的）点数；`cameras.json` 恰有 2 条（test 在前）；`create_from_pcd` 打印的初始化点数与 `input.ply` 顶点数一致。若在无 GPU 环境运行，`create_from_pcd` 的 `.cuda()` 会报错——此时把报告完成到第 2 步的 cameras_extent 对照即可，其余**待本地验证**。

## 6. 本讲小结

- `Scene` 是装配工厂：查表分发数据集 → 写出 `input.ply`/`cameras.json` → 打乱相机 → 多分辨率转换 → 三分支初始化高斯，这条顺序里藏着「必须先 Scene 后 training_setup」的依赖。
- `sceneLoadTypeCallbacks` 是唯一的数据格式注册点（Colmap/Blender 二选一，靠 `sparse/` 与 `transforms_train.json` 的目录探测，sparse 优先），新增数据格式的二次开发从这里挂载。
- `cameras_extent = 1.1 × 训练相机光心到均值中心的最大距离`，只由训练相机计算；它同时缩放位置学习率（经 `spatial_lr_scale` 作用于 xyz、`_t` 与调度器）和致密化的 clone/split 尺寸分界（`percent_dense × extent`）。
- `cameraList_from_camInfos` 是 `CameraInfo → Camera` 的转换循环，外层按 `resolution_scale` 组织成字典；shuffle 只做一次保证多分辨率顺序一致。
- 初始化三分支：`loaded_pth`（外部 4D 高斯，`create_from_pth`）> `loaded_iter`（本目录 ply，`load_ply`，且会跳过两个文件写出）> 默认 `create_from_pcd`；只有前两分支之外时才写 `input.ply`/`cameras.json`。
- 一次训练的 `model_path` 产物清单：`cfg_args`、`training_params.txt`（train.py）+ `input.ply`、`cameras.json`（Scene 构造）+ `chkpntN.pth`、`point_cloud/iteration_N/point_cloud.ply`（Scene.save）+ TensorBoard 事件与中间渲染图。

## 7. 下一步学习建议

本讲把「数据 → 场景」的最后一环补齐了。接下来按依赖关系建议：

1. **u3-l1（GaussianModel：继承自 3DGS 的属性）**：本讲反复出现的 `create_from_pcd` 内部到底初始化了哪些张量？`_xyz`、`_scaling`、`_opacity` 的形状与激活函数是下一单元的起点。
2. **u3-l5（初始化与持久化）**：精读 `create_from_pcd` 的初始化策略（`distCUDA2` 定尺度、`redundant_ratio` 时间冗余、时间尺度取 duration/5）与 `save_ply/load_ply` 的字段布局——本讲 4.5 节的三分支在那里展开成完整机制。
3. **u5-l4（自适应致密化与剪枝）**：本讲只讲了 `percent_dense × scene_extent` 这个量从哪来，clone/split 的完整判定与优化器状态同步在那一讲。
4. 若你更关心推理侧：可提前浏览 `render.py` 中 `Scene(args, gaussians, load_iteration=..., shuffle=False)` 的调用方式，体会 `load_iteration=-1` 自动找最新迭代的用法。
