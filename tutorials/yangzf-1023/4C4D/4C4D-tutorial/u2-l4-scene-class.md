# u2-l4 Scene 类：数据集分发与场景装配

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `Scene` 在 4C4D 中的角色：它是「磁盘上的数据集」与「可训练/可渲染的 4D 高斯场景」之间的装配器，`train.py` 与 `render.py` 共用这一个入口。
2. 解释 `Scene` 如何通过 `sceneLoadTypeCallbacks` 回调表，根据目录特征把数据集自动分发给 Colmap 或 Blender 两种读取器。
3. 手工推导 `cameras_extent`（即 `nerf_normalization["radius"]`）的计算过程，并说出它的两大用途：缩放位置学习率、参与致密化的 clone/split 判定。
4. 列出一次训练在 `model_path` 下会生成哪些文件，其中哪些是 `Scene` 在构造阶段写出的（`input.ply`、`cameras.json`）。
5. 说出高斯初始化的三岔路：`loaded_pth` → `create_from_pth`；`load_iteration` → `load_ply`；否则冷启动 `create_from_pcd`。

本讲承接 u2-l2（`readColmapSceneInfo` 与 `SceneInfo` 容器）和 u2-l3（`Camera` 对象与懒加载），把数据链路的最后一环补上：这些零散的信息如何被装配成一个 `Scene`。

## 2. 前置知识

- **SceneInfo / CameraInfo / BasicPointCloud**：u2-l2 讲过的三个 NamedTuple 容器。`readColmapSceneInfo` 的返回值 `SceneInfo` 打包了点云、训练/测试相机列表、场景归一化信息和点云路径。本讲只消费它，不再深入其构造细节。
- **Camera 对象**：u2-l3 讲过 `Camera` 把 `CameraInfo` 加工成渲染可用的投影矩阵；`meta_only=True`（即 `dataloader=True`）时不持有真实图像像素。
- **回调表（callback table）**：就是一个字典，键是数据集类型名，值是「能读取该类型数据的函数」。调用方先探测数据集特征，再查表调用对应函数。这是策略模式的最简实现，新增数据集格式只需往表里加一项（u8-l4 会专门讲）。
- **相机光心（camera center）**：相机在世界坐标系下的位置。若 \(W2C = [R|T]\) 是世界到相机的变换，则光心 \(C = -R^{\top}T\)（本仓库 `R` 存的是转置后的 C2W 旋转，`getWorld2View2` 负责拼回）。
- **场景归一化的直觉**：不同数据集的物理尺度差异巨大（桌面物体可能半径不到 1 米，街区可能几十米）。把「所有训练相机光心到其均值的最大距离 × 1.1」压成一个标量，就能让学习率、致密化阈值等超参数跨场景通用。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [scene/__init__.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/__init__.py) | 定义 `Scene` 类 | 构造流程、产物写出、初始化三岔路、相机获取接口 |
| [scene/dataset_readers.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py) | 数据集读取器 | `sceneLoadTypeCallbacks` 回调表、`getNerfppNorm`、`SceneInfo` |
| [utils/camera_utils.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/camera_utils.py) | 相机工具 | `cameraList_from_camInfos`、`loadCam`、`camera_to_JSON` |
| [utils/system_utils.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/system_utils.py) | 系统工具 | `searchForMaxIteration`（找最新迭代号） |
| [scene/gaussian_model.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py) | 高斯模型 | `create_from_pcd` / `create_from_pth` / `load_ply` 的入口、`spatial_lr_scale` 的消费位置 |
| [train.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py) | 训练入口 | `Scene` 的调用处、`model_path` 目录的创建与文件清单 |
| [render.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/render.py) | 推理入口 | `Scene` 的第二个调用处（`shuffle=False`） |

## 4. 核心概念与源码讲解

### 4.1 Scene 类：训练与推理共用的场景装配器

#### 4.1.1 概念说明

`Scene` 是一个「装配器（assembler）」：它自己不解析任何数据格式、不定义任何高斯属性，而是把三样东西组装到一起：

1. **一批相机**（从数据集读取器拿到的 `CameraInfo` 列表，转成 `Camera` 对象）；
2. **一份初始点云**（决定高斯的出生状态）；
3. **一个输出目录 `model_path`**（把初始化证据快照下来，供排查与复现）。

`train.py` 的 `training()` 在创建 `GaussianModel` 之后立刻构造 `Scene`（见 [train.py:62-67](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L62-L67)），然后才调用 `gaussians.training_setup(opt)`。也就是说：**高斯的「出生」发生在 Scene 里，高斯的「入学（优化器）」发生在 Scene 之外**。这个顺序是理解本讲的关键。

#### 4.1.2 核心流程

`Scene.__init__` 从头到尾做十件事，按执行顺序：

```text
1. 记录 model_path / loaded_iter / gaussians / white_background
2. 若指定 load_iteration：
     -1 → 在 model_path/point_cloud/ 下找最大 iteration_N
     其他 → 直接用该数字                 （推理/续训模式）
3. 探测数据集类型 → 查 sceneLoadTypeCallbacks → 得到 scene_info
4. 若是冷启动（无 loaded_iter）：
   4a. 把 scene_info.ply_path 复制为 model_path/input.ply
   4b. 把 test + train 相机逐个转成 JSON，写 model_path/cameras.json
5. 若 shuffle：随机打乱 train/test 相机顺序
6. self.cameras_extent = scene_info.nerf_normalization["radius"]
7. 对每个 resolution_scale（默认只有 1.0）：
     train_cameras[scale] = cameraList_from_camInfos(train, scale, args)
     test_cameras[scale]  = cameraList_from_camInfos(test,  scale, args)
8. 高斯初始化三岔路：
     args.loaded_pth 非空 → gaussians.create_from_pth(pth, cameras_extent)
     loaded_iter 非空     → gaussians.load_ply(model_path/point_cloud/iteration_N/point_cloud.ply)
     否则（冷启动）       → gaussians.create_from_pcd(scene_info.point_cloud, cameras_extent, redundant_ratio)
```

注意第 4 步在第 8 步**之前**执行——这一点在综合实践里会用到：即使第 8 步因缺少 GPU 而失败，`input.ply` 和 `cameras.json` 也已经写出，可以用来观察。

#### 4.1.3 源码精读

**构造函数签名**——参数远比 `train.py` 直接传的多，因为有大量默认值：

[scene/__init__.py:27-30](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/__init__.py#L27-L30)

这段定义了 `Scene` 的全部可调项：`load_iteration`（加载已训练模型）、`shuffle`（是否打乱相机顺序）、`resolution_scales`（多分辨率训练）、`num_pts`/`num_pts_ratio`（初始点云规模，见 u2-l2）、`time_duration`（时间域）、`training_view`/`testing_view`（视角划分）、`redundant_ratio`（时间冗余）、`downsample_method`（random/fps）。其中 `training_view` 的默认值 `['cam10', 'cam01', 'cam20', 'cam13']` 与 u2-l2 讲过的 N3V/DyNeRF 数据集约定一致。

**推理模式的迭代号探测**：

[scene/__init__.py:39-46](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/__init__.py#L39-L46)

当 `load_iteration == -1` 时，用 `searchForMaxIteration` 扫描 `model_path/point_cloud/` 下的目录名（形如 `iteration_7000`），取数字最大者：

[utils/system_utils.py:27-29](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/system_utils.py#L27-L29)

这行代码把每个子目录名按 `_` 切开取最后一段转 int 再取 max——它假设该目录下**只有** `iteration_N` 形式的目录，混入其他名字会直接报错。

**相机容器是「按分辨率组织的字典」**：

[scene/__init__.py:48-49](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/__init__.py#L48-L49)

`self.train_cameras` 与 `self.test_cameras` 是 `{resolution_scale: [Camera, ...]}` 的字典而非普通列表，对应签名里的 `resolution_scales=[1.0]`（4C4D 实际只用 1.0，但保留了 3DGS 的多分辨率机制）。

**对外接口**——训练循环和推理脚本只通过这四个方法取相机，返回的都是 `CameraDataset`（懒加载包装，见 u2-l3）：

[scene/__init__.py:114-127](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/__init__.py#L114-L127)

`getTrainCameras`/`getTestCameras` 对列表做了 `.copy()`（浅拷贝，防止外部 shuffle 污染内部顺序），`getAllCameras` 把两者拼接（u7-l2 的轨迹生成 `generate_path` 就靠它拿全部相机），`getValidationCameras` 用切片 `::num` 隔帧抽样。

**保存接口**（本讲只认识它，细节留给 u5-l5）：

[scene/__init__.py:109-112](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/__init__.py#L109-L112)

每次保存写两个东西：`chkpntN.pth`（`gaussians.capture()` 的元组 + 迭代号，用于续训）和 `point_cloud/iteration_N/point_cloud.ply`（高斯属性快照，用于推理）。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：对比两个入口对 `Scene` 的调用方式，理解「训练需要打乱、推理需要保序」。
2. **操作步骤**：
   - 打开 [train.py:64-66](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L64-L66)，记下它传了哪些关键字参数（`num_pts`、`training_view`、`redundant_ratio`、`downsample_method` 等，`shuffle` 用默认值 `True`）。
   - 打开 [render.py:48-49](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/render.py#L48-L49)，注意它显式传了 `shuffle=False`，且没有传 `load_iteration`。
   - 再看 [render.py:52-52](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/render.py#L52)：`Scene` 内部虽然走了 `create_from_pcd` 冷启动，但紧接着 `torch.load(checkpoint)` + `gaussians.restore` 用检查点内容**整体覆盖**了高斯参数。
3. **需要观察的现象**：`render.py` 里 `Scene` 构造打印的 `Creating gaussians from initial point cloud input.ply` 与随后的 restore 是两次独立的初始化，前者产出的高斯寿命只有几行代码。
4. **预期结果**：你能说出为什么 `render.py` 必须 `shuffle=False`——测试帧要按时间顺序逐帧渲染与对比指标，打乱会破坏帧序；而训练打乱是为了每个 epoch 的视角序列不同。
5. 本实践为纯阅读，无需运行（待本地验证的只有你对打印顺序的预测）。

#### 4.1.5 小练习与答案

**练习 1**：如果我把 `train.py` 里构造 `Scene` 与 `gaussians.training_setup(opt)` 两行交换顺序，会发生什么？

**答案**：`training_setup` 会立即失败。它第一行就要 `self.get_xyz.shape[0]`（[scene/gaussian_model.py:481-482](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L481-L482)），而 `_xyz` 只有在 `Scene` 内部调用 `create_from_pcd`/`load_ply` 之后才被赋值；交换后 `_xyz` 还是初始化时的 `None`，抛 `AttributeError`/`TypeError`。这印证了「高斯的出生在 Scene 里」。

**练习 2**：`scene.getTrainCameras()` 返回的是 `CameraDataset` 而不是 `list[Camera]`，结合 u2-l3，说出一个原因。

**答案**：`CameraDataset` 配合 `DataLoader`（`collate_fn=lambda x: x`，[train.py:104-105](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L104-L105)）实现多 worker 懒加载：图像只有在 `__getitem__` 被调用时才 `cv2.imread` 读入，避免上千帧全量常驻显存（u2-l3 估算约 37 GiB）。直接返回 list 会迫使构造期读完全部图像。

### 4.2 sceneLoadTypeCallbacks：两种数据集的分发

#### 4.2.1 概念说明

`Scene` 不写 `if/elif` 直接调用某个读取函数，而是先「看目录长什么样」，再查一张注册表。这张表就是 `sceneLoadTypeCallbacks`。好处是：`Scene` 对数据集格式无感，新增格式（例如自己的合成数据）只要注册一个新键值对，`Scene` 一行都不用改——这是 u8-l4 二次开发的基础。

探测规则很朴素：

- 源路径下有 `sparse/` 目录 → 认定为 **Colmap** 数据集；
- 否则有 `transforms_train.json` → 认定为 **Blender**（NeRF 合成格式）数据集；
- 两者都没有 → `assert False`，报 `Could not recognize scene type!`。

#### 4.2.2 核心流程

```text
os.path.exists(source_path/sparse)  ──yes──> sceneLoadTypeCallbacks["Colmap"](path, images, eval,
        │                                   num_pts_ratio, training_cam, testing_cam,
        │                                   num_pts, time_duration, downsample_method)
        no
        ↓
os.path.exists(source_path/transforms_train.json) ──yes──> sceneLoadTypeCallbacks["Blender"](path,
        │                                                   white_background, eval, num_pts,
        │                                                   time_duration, extension,
        │                                                   num_extra_pts, frame_ratio, dataloader)
        no
        ↓
assert False  "Could not recognize scene type!"
```

两个回调都返回同一种 `SceneInfo`，所以下游代码完全一致——这正是「注册表 + 统一返回类型」的价值。

#### 4.2.3 源码精读

**注册表本体**只有两行：

[scene/dataset_readers.py:535-538](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L535-L538)

`"Colmap"` 映射到 `readColmapSceneInfo`（u2-l2 已精读），`"Blender"` 映射到 `readNerfSyntheticInfo`。注意这张表写在 dataset_readers.py 的**末尾**——因为值就是上面定义的两个函数对象，必须先定义后引用。

**Scene 侧的分发代码**：

[scene/__init__.py:51-60](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/__init__.py#L51-L60)

Colmap 分支把 u2-l2 讲过的所有点云加工参数原样透传；Blender 分支传的则是另一组参数（`white_background`、`extension`、`num_extra_pts`、`frame_ratio`、`dataloader`）——两个回调的**函数签名不同**，`Scene` 必须分别传参，这是注册表模式里容易踩的坑（注册新格式时，调用处的参数列表要同步扩展）。

**统一的返回容器**：

[scene/dataset_readers.py:60-65](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L60-L65)

`SceneInfo` 五个字段：初始点云、训练相机、测试相机、场景归一化、点云文件路径。Colmap 与 Blender 的差异被吸收在这个统一结构里。

**两个回调的两处关键差异**（Blender 侧速览，不必精读）：

- 初始点云来源：Colmap 用真实重建点云（`sparse/0/points3D.ply`，首次运行时从 bin/txt 转换，见 [scene/dataset_readers.py:293-306](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L293-L306)）；Blender 合成场景没有 SfM 点云，直接在 \([-1.3, 1.3]^3\) 立方体里**随机撒点**（[scene/dataset_readers.py:465-475](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L465-L475)），`ply_path` 也从 `sparse/0/points3D.ply` 变成数据集根目录的 `points3d.ply`（[scene/dataset_readers.py:465-465](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L465-L465)）。
- 相机来源：Colmap 走 qvec/tvec（u2-l1），Blender 走 `transform_matrix`（C2W，含 OpenGL→COLMAP 轴向翻转，[scene/dataset_readers.py:376-384](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L376-L384)）。

#### 4.2.4 代码实践（源码阅读 + 可选运行）

1. **实践目标**：亲手摸一下这张注册表，并核对两个回调的签名差异。
2. **操作步骤**：
   - 在能 import 该模块的环境（需先编译 pointops2 等扩展，见 u1-l2）执行：
     ```python
     from scene.dataset_readers import sceneLoadTypeCallbacks
     import inspect
     for k, fn in sceneLoadTypeCallbacks.items():
         print(k, inspect.signature(fn))
     ```
   - 无编译环境时做纯阅读：对照 [scene/dataset_readers.py:255-258](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L255-L258) 与 [scene/dataset_readers.py:452-452](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L452-L452) 两处函数签名手抄参数表。
3. **需要观察的现象**：两个回调第一个位置参数之后几乎没有任何共同参数（除了 `eval`、`num_pts`、`time_duration`）。
4. **预期结果**：你会得出结论——`Scene` 的分发代码（L52/L58）之所以要写两串很长的实参列表，正是因为注册表只统一了「返回值类型」，没有统一「入参协议」。
5. 运行部分依赖扩展编译，无 GPU/扩展环境时结果**待本地验证**；签名阅读部分可直接完成。

#### 4.2.5 小练习与答案

**练习 1**：为什么探测顺序是先查 `sparse/` 再查 `transforms_train.json`？如果某个目录两者都有会怎样？

**答案**：顺序由 `if/elif` 写死（L51→L56），两者都有时 `sparse/` 胜出、走 Colmap 分支。先查 `sparse/` 是因为 4C4D 的主数据链路（N3V/DyNeRF 多相机视频）被 `n3v2colmap.py` 转成 COLMAP 格式（u2-l5），Colmap 是第一公民；Blender 分支是继承自 3DGS 的合成数据支持。

**练习 2**：`readNerfSyntheticInfo` 里 `if not eval: train_cam_infos.extend(test_cam_infos)`（[scene/dataset_readers.py:459-461](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L459-L461)）与 Colmap 分支的 `eval` 处理（u2-l2）有什么共同设计意图？

**答案**：共同意图是「不评估时把全部数据用于训练」。Colmap 分支 `eval=False` 时 `train_cam_infos = cam_infos; test_cam_infos = []`；Blender 分支把 test 并回 train。区别在于 Blender 的 test 集来自独立的 `transforms_test.json`（本来就不重叠），Colmap 的划分按相机名（`training_view`）进行。

### 4.3 场景装配产物：input.ply、cameras.json 与 cameras_extent

#### 4.3.1 概念说明

`Scene` 在冷启动时会把两份「初始化证据」快照进 `model_path`：

- `input.ply`：初始点云的完整拷贝。训练出问题时，第一件事就是看它——初始点云烂，后面全烂。
- `cameras.json`：所有相机的位姿与内参的人类可读版本，方便用外部工具（如可视化脚本）核对相机摆放。

随后 `Scene` 从 `scene_info.nerf_normalization["radius"]` 提取 `cameras_extent`——整个场景物理尺寸的一个标量摘要。它不是日志摆设，而是**两处关键超参数的缩放因子**（见 4.3.2），理解它就理解了为什么同一套默认学习率能跑不同尺度的场景。

#### 4.3.2 核心流程

**cameras_extent 的计算**（在读取器内完成，`Scene` 只取结果）：

对每个**训练**相机 \(i\)（注意：不含测试相机），先求光心 \(C_i\)（C2W 矩阵的平移列），然后：

\[
\text{center} = \frac{1}{N}\sum_{i=1}^{N} C_i, \qquad
\text{diagonal} = \max_i \lVert C_i - \text{center} \rVert_2, \qquad
\text{radius} = 1.1 \times \text{diagonal}
\]

1.1 是安全系数：让半径略微超出最远相机，避免边界效应。

**cameras_extent 的两大下游**：

1. **位置学习率缩放**。它作为 `spatial_lr_scale` 传入高斯模型，作用于三处（详见 4.5.3）：
   \[
   \text{lr}_{xyz} = \text{position\_lr\_init} \times \text{cameras\_extent}, \qquad
   \text{lr}_{t} = \text{position\_t\_lr\_init} \times \text{cameras\_extent}
   \]
   以及指数衰减调度器的 `lr_init`/`lr_final` 同比缩放。直觉：场景越大，高斯需要移动的距离越远，步子就该迈得越大。
2. **致密化的 clone/split 判据**。`densify_and_split` 里「该分裂」要求高斯最大尺度**大于** `percent_dense × scene_extent`（[scene/gaussian_model.py:681-684](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L681-L684)），`densify_and_clone` 里「该克隆」要求**小于等于**它（[scene/gaussian_model.py:725-728](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L725-L728)）。直觉：大高斯分裂成小块、小高斯原地复制，而「大小」必须相对场景尺寸衡量。

#### 4.3.3 源码精读

**产物写出的完整代码**：

[scene/__init__.py:62-76](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/__init__.py#L62-L76)

要点：整段被 `if not self.loaded_iter:` 包住——加载已训练模型时**不**重写这两份文件（目录里已有训练期的版本）；相机列表先 test 后 train 拼接（`camlist.extend`），因此 `cameras.json` 的前半段是测试相机、后半段是训练相机，`id` 是拼接后的下标。

**camera_to_JSON 还原位姿**：

[utils/camera_utils.py:79-99](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/camera_utils.py#L79-L99)

注意这里的三个变换方向：`camera.R` 存的是**转置过的** C2W 旋转（u2-l1 的约定），所以先 `camera.R.transpose()` 拼出 C2W 矩阵 `Rt`，再 `np.linalg.inv(Rt)` 得到 W2C，从中取 `pos`（光心）与 `rot`。焦距则由视场角反推：`fx = fov2focal(FovX, width)`。这提醒我们：`cameras.json` 里的 `position`/`rotation` 是 **C2W** 语义（`W2C` 变量名的反转矩阵）。

**getNerfppNorm 本体**：

[scene/dataset_readers.py:67-88](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L67-L88)

内层函数 `get_center_and_diag`：`cam_centers` 是 3×N 矩阵（`np.hstack` 拼接每个 3×1 光心），`center` 逐行求均值，`dist` 逐列求欧氏范数，`diagonal` 取最大值。L84 的 `radius = diagonal * 1.1` 就是最终 `cameras_extent`。返回的 `translate`（= -center）在本仓库中**没有任何消费者**，是 3DGS 的遗留字段。

**调用点与消费点**：

[scene/dataset_readers.py:291-291](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L291-L291)

Colmap 分支只用 `train_cam_infos` 算归一化（Blender 分支同理，[scene/dataset_readers.py:463-463](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L463-L463)）——`eval=False` 时训练集是全部相机，`eval=True` 时只有 `training_view` 选中的相机，所以**同一个数据集开不开 `--eval` 会得到不同的 cameras_extent**，进而影响学习率。

[scene/__init__.py:78-84](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/__init__.py#L78-L84)

打乱在前、取 radius 在后；`self.cameras_extent` 保存后立刻在两处使用：多分辨率装配（L86-91）和 4.5 节的初始化三岔路（作为 `spatial_lr_scale` 传入）。

**model_path 文件全景**（`Scene` 负责前两项，其余由 train.py 各段落写出）：

| 文件/目录 | 写出者 | 内容 |
| --- | --- | --- |
| `input.ply` | `Scene`（L63-65） | 初始点云快照 |
| `cameras.json` | `Scene`（L74-76） | 全部相机的位姿/内参 JSON |
| `cfg_args` | [train.py:294-295](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L294-L295) | ModelParams 等 Namespace 的字符串 |
| `training_params.txt` | [train.py:476-479](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L476-L479) | 合并 yaml 后的最终参数快照 |
| `events.out.tfevents.*` | [train.py:298-300](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L298-L300) | TensorBoard 日志 |
| `point_cloud/iteration_N/point_cloud.ply` | `Scene.save`（L111-112） | 各保存点的高斯属性 |
| `chkpntN.pth` / `chkpnt_best.pth` | `Scene.save`（L110）/[train.py:276](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L276-L276) | capture 元组 + 迭代号 |
| `rendered_images/` | [train.py:107-108](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L107-L108) | 训练中间渲染图 |

#### 4.3.4 代码实践（纯 numpy，无需 GPU 与项目依赖）

1. **实践目标**：用 numpy 手工复现 `getNerfppNorm`，验证你对公式的理解。
2. **操作步骤**（以下为**示例代码**，可存成独立小脚本运行）：
   ```python
   import numpy as np

   # 造 4 个相机光心，模拟 4 台相机的典型摆放（都看向原点附近）
   C = np.array([[ 2.0,  0.0,  1.5],
                 [-2.0,  0.5, 1.5],
                 [ 0.0,  2.2, 1.6],
                 [ 0.1, -2.1, 1.4]]).T          # 3xN，等价于 np.hstack(cam_centers)

   center    = C.mean(axis=1, keepdims=True)    # 对应 avg_cam_center
   dist      = np.linalg.norm(C - center, axis=0, keepdims=True)
   diagonal  = dist.max()
   radius    = diagonal * 1.1                   # 对应 L84
   print("center   =", center.flatten())
   print("diagonal =", diagonal)
   print("radius   =", radius)                  # 这就是 cameras_extent
   ```
3. **需要观察的现象**：`radius` 约等于最远相机到平均光心距离的 1.1 倍；把任何一个相机挪远，`radius` 单调不减。
4. **预期结果**：上述数据下 `diagonal ≈ 2.24` 左右、`radius ≈ 2.47` 左右（取决于具体数值，手算可核对）。再用 `radius` 乘 3DGS 默认 `position_lr_init = 0.00016`（[arguments/__init__.py:83](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/arguments/__init__.py#L83)），得到 xyz 参数组的初始学习率约 `4e-4` 量级——这就是「场景尺度缩放学习率」的直接体现。
5. 数值可自行验证；与真实数据集的对照**待本地验证**（需跑综合实践或真实训练）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `getNerfppNorm` 只用训练相机、不用测试相机？

**答案**：`cameras_extent` 的职责是刻画「优化器要走的舞台大小」，学习率与致密化阈值都应只由训练数据决定；若混入测试相机，评估设置（`--eval` 与否、留出哪几台）会额外扰动优化超参数，让实验不可比。Colmap/Blender 两个分支都遵守这一点（L291、L463）。

**练习 2**：若两台相机光心重合（固定机位数据里常见冗余），会对 `radius` 产生什么影响？这是 bug 吗？

**答案**：没有影响，也不算 bug。重合光心到均值的距离相同，`radius` 由**最远**光心决定（取 max），个别相机重合只影响 `center` 的均值位置，通常可以忽略。真正的极端情况是只有一台训练相机——此时 dist 全为 0，`radius` 为 0，学习率被缩放成 0，训练直接失效（4C4D 的 4 相机设定天然避开这一点）。

**练习 3**：`cameras.json` 里 `rotation` 字段存的是 W2C 还是 C2W 的旋转？

**答案**：C2W。代码先把 `camera.R.transpose()`（还原成标准 C2W 旋转）拼进 `Rt`，再 `np.linalg.inv(Rt)` 得到 W2C 并从中取 `rot`——所以 `rot` 是 W2C 旋转矩阵的逆，即 C2W 旋转；`position` 同理是 C2W 的平移部分（光心）。

### 4.4 cameraList_from_camInfos：从 CameraInfo 到多分辨率 Camera

#### 4.4.1 概念说明

`Scene` 拿到的 `scene_info.train_cameras` 是一列 **CameraInfo**（纯元数据：R、T、Fov、宽高、timestamp、图像路径）。渲染需要的是 **Camera** 对象（带投影矩阵、分辨率协商结果、可选的真实像素）。`cameraList_from_camInfos` 就是这条流水线：对每个 CameraInfo 调一次 `loadCam`，产出一个 Camera。它还吃一个 `resolution_scale` 参数，配合 `Scene` 的 `resolution_scales` 列表支持多分辨率训练（4C4D 实际只用 `[1.0]`）。

#### 4.4.2 核心流程

```text
for id, cam_info in enumerate(cam_infos):
    Camera = loadCam(args, id, cam_info, resolution_scale)

loadCam 内部：
1. 与 args.resolution 协商出目标分辨率与缩放系数 scale
     - resolution ∈ {1,2,3,4,8}：整数倍下采样，scale = resolution_scale × resolution
     - resolution == -1 且宽 > 1600：自动缩到 1.6K（只警告一次）
     - 其他数值：按 宽/分辨率 比例缩放
2. cx/cy/fl_x/fl_y 同除 scale（内参随图像同步缩放，视场角不变——u2-l3 的结论）
3. dataloader=False 时读真实图像并 resize；True 时只传元数据（image 为 None）
4. 构造 Camera(..., meta_only=args.dataloader)
```

#### 4.4.3 源码精读

**列表转换本体**——一个薄薄的循环：

[utils/camera_utils.py:71-77](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/camera_utils.py#L71-L77)

注意 `id` 是**新列表里的下标**（0,1,2,...），而不是 COLMAP 的 `uid`；后者经 `colmap_id=cam_info.uid` 传入 Camera 保存。所以 `Camera.uid` 与 `Camera.colmap_id` 是两个不同的编号。

**Scene 侧的调用（多分辨率外层循环）**：

[scene/__init__.py:86-91](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/__init__.py#L86-L91)

对每个 `resolution_scale` 各建一套 train/test 相机，存进 4.1.3 提到的字典。打印语句会报告每套相机的帧数，这是排查「相机数量是否符合预期」的第一现场。

**loadCam 的分辨率协商**：

[utils/camera_utils.py:19-45](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/camera_utils.py#L19-L45)

三个分支（整数倍 / -1 自动 / 显式宽度）在 u2-l3 已详细讲过；本讲只需记住：**`args.resolution` 的最终值受 u1-l4 讲过的「`--res` 恒真守卫覆盖 yaml」问题影响**，排查分辨率问题时先看 `training_params.txt` 里 `resolution` 的实际值。

**懒加载开关**：

[utils/camera_utils.py:47-55](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/camera_utils.py#L47-L55)

`args.dataloader=True` 时 `gt_image = cam_info.image`（Colmap 路径下是 `None`），配合 `meta_only=True`，Camera 构造完全不接触像素——这就是 Colmap/N3V 数据必须开 `dataloader` 的原因（u2-l3）。

#### 4.4.4 代码实践（纸面计算型）

1. **实践目标**：掌握分辨率协商的三个分支，能对任意输入算出输出尺寸。
2. **操作步骤**：对「原始图像 1920×1080、`resolution_scale=1.0`」分别计算 `args.resolution` 取 `1 / 2 / -1 / 960` 时的输出分辨率与 `scale`，对照 [utils/camera_utils.py:19-45](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/camera_utils.py#L19-L45) 的代码写下来。
3. **需要观察的现象**：`-1` 分支因 `orig_w=1920 > 1600` 触发自动缩放并打印一次警告。
4. **预期结果**（待本地验证的纸面推算）：
   - `resolution=1`：`(1920, 1080)`，`scale=1`
   - `resolution=2`：`(960, 540)`，`scale=2`
   - `resolution=-1`：`global_down = 1920/1600 = 1.2`，输出 `(1600, 900)`，`scale=1.2`
   - `resolution=960`：`global_down = 1920/960 = 2`，输出 `(960, 540)`，`scale=2.0`
5. 若有环境，可在 `loadCam` 的 return 前临时 `print(resolution, scale)` 验证（改完记得还原，不要提交对源码的修改）。

#### 4.4.5 小练习与答案

**练习 1**：`resolution_scales=[1.0, 0.5]` 时，`scene.train_cameras[0.5]` 与 `[1.0]` 里的 Camera 是什么关系？

**答案**：两套独立构造的 Camera 对象，同一批 `CameraInfo` 的两种分辨率版本。`0.5` 那套经 `loadCam` 的 `scale = global_down × 0.5` 得到**更大**的图像（分辨率 scale 是除数因子，`0.5` 意味着只缩到一半程度，图像反而比 `1.0` 套大）。每套都有自己的 `uid` 下标序列。这是 3DGS 的 coarse-to-fine 机制遗留，4C4D 默认只用 `[1.0]`。

**练习 2**：`cameraList_from_camInfos` 里 `enumerate` 的下标 `id` 传给了 Camera 的 `uid`。训练循环里 `viewpoint_cam.uid` 能用来做什么？

**答案**：它是「这套相机列表内的稳定序号」。DataLoader shuffle 后仍可通过 `uid` 追踪某一帧（例如把渲染误差映射回具体帧）；而 `colmap_id` 才能映射回 COLMAP 的 image_id。u5-l3 讲多视角 batch 时会看到按相机组织统计信息的场景，这两个编号的区分就很关键。

### 4.5 高斯初始化三岔路：create_from_pth / load_ply / create_from_pcd

#### 4.5.1 概念说明

`Scene` 装配完相机后，最后一件事是给 `GaussianModel` 一个「起点」。代码按优先级排成三条路：

| 优先级 | 触发条件 | 调用 | 典型场景 |
| --- | --- | --- | --- |
| 1 | `args.loaded_pth` 非空 | `create_from_pth(pth, cameras_extent)` | 从外部 4D 高斯字典（如另一段预训练）热启动 |
| 2 | `load_iteration` 非空 | `load_ply(model_path/point_cloud/iteration_N/point_cloud.ply)` | 加载本目录已训练结果 |
| 3 | 都没有 | `create_from_pcd(scene_info.point_cloud, cameras_extent, redundant_ratio)` | 冷启动（正常训练） |

三条路都接收/使用 `cameras_extent`（前两条作为 `spatial_lr_scale` 存进模型），这把 4.3 节的「场景尺度」与后续优化器永久绑定在一起。

#### 4.5.2 核心流程

```text
if args.loaded_pth:                 # 路径 1：外部 4D 高斯
    create_from_pth(loaded_pth, cameras_extent)
elif loaded_iter:                   # 路径 2：本目录已训练 ply
    load_ply(model_path/point_cloud/iteration_{loaded_iter}/point_cloud.ply)
else:                               # 路径 3：冷启动
    create_from_pcd(scene_info.point_cloud, cameras_extent, redundant_ratio)
```

路径 3 的内部细节（distCUDA2 定空间尺度、无时间信息时随机采样 `_t`、时间尺度取 duration/5）属于 u3-l5 的主题，本讲只关注入口与 `spatial_lr_scale` 的去向。

#### 4.5.3 源码精读

**三岔路本体**：

[scene/__init__.py:93-107](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/__init__.py#L93-L107)

三个分支的打印语句（`Loading gaussians from ...` / `Creating gaussians from initial point cloud input.ply`）是训练日志里判断「走了哪条路」的最快依据。

**`create_from_pcd` 的签名与 spatial_lr_scale 的第一站**：

[scene/gaussian_model.py:406-409](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L406-L409)

`spatial_lr_scale` 在这里只是被**存起来**（L407），真正的消费发生在 `training_setup`。同函数接下来把点云转 CUDA 张量并做 SH 颜色转换（L408-409）——这也是路径 3 **必须有 GPU** 的原因。

**`create_from_pth` 与 `load_ply` 的入口**：

[scene/gaussian_model.py:450-453](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L450-L453)

`create_from_pth` 有硬前提 `assert self.gaussian_dim == 4 and self.rot_4d`——它加载的字典必须含全套 4D 属性（`t`、`scaling_t`、`rotation_r`），3D 模式直接断言失败。

[scene/gaussian_model.py:281-287](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L281-L287)

`load_ply` 逐字段从 PLY 读回高斯属性（字段名前缀匹配的细节见 u3-l5 与 u5-l5）。注意它**不接收** `spatial_lr_scale` 参数——沿用模型里已有的值；冷启动路径存进去的那份在这里被复用。

**`spatial_lr_scale`（= cameras_extent）的消费现场**：

[scene/gaussian_model.py:484-499](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L484-L499)

两处相乘：`xyz` 参数组（L485）与 4D 模式新增的 `t` 参数组（L496，`position_t_lr_init < 0` 时回退为 `position_lr_init`，u3-l2 讲过）。其余参数组（f_dc、opacity、scaling、rotation）**不**乘场景尺度——位置是唯一与物理尺寸同单位的属性，时间维 `t` 也被同等对待。

[scene/gaussian_model.py:506-509](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L506-L509)

指数衰减调度器的起点与终点学习率同比缩放，保证整个训练过程的位置步长都与场景尺度成比例。

#### 4.5.4 代码实践（源码阅读型）

1. **实践目标**：跟踪 `load_iteration=-1` 时迭代号如何被解析成具体路径。
2. **操作步骤**：
   - 从 [scene/__init__.py:39-44](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/__init__.py#L39-L44) 出发，假设 `model_path/point_cloud/` 下有 `iteration_7000`、`iteration_30000` 两个目录，写出 `self.loaded_iter` 的值。
   - 继续跟到 [scene/__init__.py:97-102](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/__init__.py#L97-L102)，拼出最终 `load_ply` 的完整路径。
   - 对照 `Scene.save`（L109-112）确认该路径正是当初 `save_ply` 写出的位置——读写闭环。
3. **需要观察的现象**：`searchForMaxIteration` 对目录名的解析完全依赖 `iteration_` 前缀 + `_` 切分取末段。
4. **预期结果**：`loaded_iter = 30000`，路径为 `<model_path>/point_cloud/iteration_30000/point_cloud.ply`。
5. 可在有历史输出的目录上用 `python -c "from utils.system_utils import searchForMaxIteration; print(searchForMaxIteration('<model_path>/point_cloud'))"` 验证（待本地验证）。

#### 4.5.5 小练习与答案

**练习 1**：`render.py` 的 `validation()` 没有传 `load_iteration`，也没有传 `loaded_pth`，那它的高斯从哪来？

**答案**：先走路径 3 冷启动（`create_from_pcd`，用的是 `scene_info.point_cloud`，且因 `ply_path` 已存在不会重复转换），然后 [render.py:52-52](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/render.py#L52-L52) `torch.load(checkpoint)` + `gaussians.restore` 用检查点整体覆盖参数。冷启动产物只活了几行代码，它的真正作用是让 `restore` 有一个结构正确的宿主模型。

**练习 2**：假设你把数据集相机整体外移 10 倍距离（场景变大 10 倍），`position_lr_init` 不变，xyz 的实际初始学习率会怎么变？这合理吗？

**答案**：`cameras_extent` 约变大 10 倍，`lr = position_lr_init × cameras_extent` 也变大 10 倍。合理——高斯需要挪动的距离同样大了约 10 倍，学习率同步放大才能在相同迭代数内收敛；这也是为什么同一套默认超参数能通吃桌面级与场景级数据。副作用是 `percent_dense × scene_extent` 的 clone/split 阈值也同步放大，致密化的「大小」判据保持相对意义。

**练习 3**：为什么 `load_ply` 不像 `create_from_pcd` 那样接收 `spatial_lr_scale`？如果连续两次调用 `load_ply`，学习率缩放会出错吗？

**答案**：`spatial_lr_scale` 是模型级状态而非每次加载都要变的输入——它描述的是「这个输出目录对应场景的尺度」，与加载哪一次迭代的 ply 无关。连续 `load_ply` 不改变它，学习率缩放保持一致；只有换数据集（重新构造 `Scene`/`create_from_pcd`）才会更新。潜在风险是：用 A 场景的 `loaded_pth` 去初始化 B 场景时，`create_from_pth` 会用 B 的 `cameras_extent` 覆盖缩放系数，与 A 原始尺度脱钩——迁移训练时要意识到这一点。

## 5. 综合实践：用假 COLMAP 目录实例化 Scene

这是本讲的主实践，把 4.1–4.5 全部串起来：**手工构造一个最小的 COLMAP 文本格式数据集（2 台相机 × 10 帧），实例化 `Scene`（`gaussian_dim=4`），观察 `model_path` 下 `Scene` 写出的两份文件，打印 `cameras_extent` 并算出它对学习率的实际影响。**

> 前置：需要按 u1-l2 编译好四个 CUDA 扩展（import 链要求 pointops2、simple-knn、diff-gaussian-rasterization 都可导入）。**完整跑到 `create_from_pcd` 需要 GPU**；无 GPU 时按步骤 5 的降级路径执行，产物观察部分依然有效（因为 `input.ply`/`cameras.json` 在高斯初始化**之前**写出）。以下脚本均为**示例代码**，建议存为仓库外的独立文件运行（不要写进仓库）。

**步骤 1：生成假数据集**。利用 `readColmapSceneInfo` 的「bin 优先、失败回退 txt」逻辑（[scene/dataset_readers.py:259-268](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L259-L268)），只提供 txt 三件套即可；注意 `read_intrinsics_text` 断言只接受 `PINHOLE` 模型（[scene/colmap_loader.py:159-159](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/colmap_loader.py#L159-L159)），且 `Scene` 靠 `sparse/` 目录探测触发 Colmap 分支：

```python
# build_fake_colmap.py（示例代码）
import os, numpy as np

root = "/tmp/fake_4c4d"
os.makedirs(f"{root}/sparse/0", exist_ok=True)
os.makedirs(f"{root}/images", exist_ok=True)

# cameras.txt：camera_id MODEL width height fx fy cx cy（PINHOLE 四参数）
with open(f"{root}/sparse/0/cameras.txt", "w") as f:
    f.write("1 PINHOLE 320 240 300.0 300.0 160.0 120.0\n")
    f.write("2 PINHOLE 320 240 300.0 300.0 160.0 120.0\n")

# images.txt：image_id qw qx qy qz tx ty tz camera_id name + 空的 points2D 行
qvec_tvec = [
    "0.9962 0.0 0.0 0.0872  -1.0  0.2  0.5",   # 相机 1 外参（cam00）
    "0.9962 0.0 0.0 -0.0872  1.0 -0.2  0.5",   # 相机 2 外参（cam01）
]
with open(f"{root}/sparse/0/images.txt", "w") as f:
    f.write(f"1 {qvec_tvec[0]} 1 cam00_0000.png\n\n")
    f.write(f"2 {qvec_tvec[1]} 2 cam01_0000.png\n\n")

# images/：camXX_YYYY.png（4 位帧号）。dataloader=True 时不会真的读像素，空文件即可，
# 但目录里只能有这些 png（数量断言：len(cam_infos) == len(os.listdir(images))）
for cam in ("cam00", "cam01"):
    for frame in range(10):
        open(f"{root}/images/{cam}_{frame:04d}.png", "w").close()

# points3D.txt：point_id x y z r g b error track...
rng = np.random.default_rng(0)
with open(f"{root}/sparse/0/points3D.txt", "w") as f:
    for i in range(500):
        x, y, z = rng.uniform(-1, 1, 3)
        r, g, b = rng.integers(0, 255, 3)
        f.write(f"{i+1} {x:.4f} {y:.4f} {z:.4f} {r} {g} {b} 1.0\n")

print("fake dataset ready at", root)
```

**步骤 2：实例化 Scene**。用 `ModelParams`/`OptimizationParams` 的正规解析链（u1-l4）拿到参数对象；**必须 `--eval` + `--dataloader`**，前者让 train/test 划分与 `nerf_normalization` 生效，后者避免 `loadCam` 读取空图像文件：

```python
# run_scene.py（示例代码，需在仓库根目录下运行）
import sys
from argparse import ArgumentParser
from arguments import ModelParams, OptimizationParams
from scene import Scene, GaussianModel

sys.path.insert(0, ".")
parser = ArgumentParser()
lp = ModelParams(parser)
op = OptimizationParams(parser)
args = parser.parse_args(["-s", "/tmp/fake_4c4d", "-m", "/tmp/fake_out",
                          "--eval", "--dataloader", "--white_background"])
dataset = lp.extract(args)
opt = op.extract(args)

gaussians = GaussianModel(dataset.sh_degree, gaussian_dim=4,
                          time_duration=[0, 10], rot_4d=True)
scene = Scene(dataset, gaussians, num_pts=1_000_000,      # 点数小于上限 → 跳过下采样
              training_view=["cam00"], testing_view=["cam01"])
```

**步骤 3：观察 `model_path` 产物**（此步无需 GPU 成功——文件在高斯初始化前已写出）：

```bash
ls /tmp/fake_out          # 应看到 input.ply 与 cameras.json
head -c 400 /tmp/fake_out/cameras.json
python -c "from plyfile import PlyData; \
  p = PlyData.read('/tmp/fake_out/input.ply'); \
  print(p['vertex'].data.dtype.names, len(p['vertex'].data))"
```

预期：`input.ply` 的 vertex 字段为 `x y z nx ny nz red green blue`（由 `storePly` 写出，**无 time 属性**，所以高斯初始化会走「随机采样 `_t` + redundant_ratio」分支——u3-l5 的主题）；`cameras.json` 共 20 条（10 测试在前、10 训练在后，4.3.3 的拼接顺序），每条含 `position/rotation/fx/fy`。

**步骤 4：打印 cameras_extent 与学习率影响**：

```python
print("cameras_extent =", scene.cameras_extent)          # Scene L84
print("spatial_lr_scale =", gaussians.spatial_lr_scale)  # 与上面相等（create_from_pcd L407）
print("xyz lr =", opt.position_lr_init * scene.cameras_extent)        # L485 公式
print("t   lr =", opt.position_lr_init * scene.cameras_extent)       # L496 回退分支
```

有 GPU 时再加两行验证到优化器层面（需 `training_setup`，其内部张量建在 cuda 上）：

```python
gaussians.training_setup(opt)   # 仅 GPU 环境可执行
for g in gaussians.optimizer.param_groups:
    if g["name"] in ("xyz", "t"):
        print(g["name"], g["lr"])
```

**步骤 5（无 GPU 降级路径）**：跳过 `Scene`，直接调用回调函数观察 `SceneInfo`，同样能看到 `nerf_normalization`（此路径不触碰 GaussianModel，但仍需扩展编译完成 import）：

```python
from scene.dataset_readers import sceneLoadTypeCallbacks
info = sceneLoadTypeCallbacks["Colmap"]("/tmp/fake_4c4d", "images", True,
                                        num_pts=1_000_000, training_cam=["cam00"],
                                        time_duration=[0, 10])
print(info.nerf_normalization, len(info.train_cameras), len(info.test_cameras))
```

**预期结果**（数值**待本地验证**）：`train_cameras` 10 帧、`test_cameras` 10 帧；`cameras_extent` 为一个由两台相机光心距离决定的正数（本例外参下约为光心间距量级 × 1.1）；`xyz lr` 等于 `0.00016 × cameras_extent`。全程应看到 `Found sparse folder ... assuming Colmap data set!`、`Converting point3d.bin to .ply...`（首次）、`Copying input.ply ...`、`Writing cameras.json ...` 等打印，它们与 4.1.2 的十步流程一一对应。

## 6. 本讲小结

- `Scene` 是装配器而非解析器：它把「数据集读取器返回的 `SceneInfo`」加工成「相机字典 + 初始化完成的 GaussianModel + 输出目录快照」，`train.py` 与 `render.py` 共用；高斯的出生在 `Scene` 里，优化器的建立在 `Scene` 外。
- 数据集分发靠 `sceneLoadTypeCallbacks` 回调表：探测 `sparse/` → Colmap、`transforms_train.json` → Blender、否则断言失败；两个回调返回统一的 `SceneInfo` 但签名不同，`Scene` 需分别传参。
- `cameras_extent = nerf_normalization["radius"] = 1.1 × 训练相机光心到均值光心的最大距离`，只由训练相机决定；它既是 `xyz`/`t` 参数组与位置学习率调度器的乘法因子（`spatial_lr_scale`），又是致密化 clone/split 的尺寸判据（`percent_dense × scene_extent`）。
- 冷启动时 `Scene` 在高斯初始化**之前**写出 `input.ply`（初始点云快照）与 `cameras.json`（全部相机位姿/内参，test 在前 train 在后）；加上 train.py 写的 `cfg_args`、`training_params.txt`、TensorBoard 事件、`chkpntN.pth`、`point_cloud/iteration_N/` 等，构成一次训练的完整输出目录。
- 高斯初始化按优先级三岔：`loaded_pth` → `create_from_pcd`（要求 gaussian_dim=4 且 rot_4d）；`load_iteration`（-1 表示自动找最大迭代号）→ `load_ply`；否则 `create_from_pcd` 冷启动；三条路都把 `cameras_extent` 带进模型。
- `cameraList_from_camInfos` 把元数据 `CameraInfo` 逐个经 `loadCam`（分辨率协商 + 内参同步缩放 + 懒加载开关）变成渲染就绪的 `Camera`，按 `resolution_scale` 组织成字典（4C4D 只用 1.0）。

## 7. 下一步学习建议

本讲之后，数据链路（u2 全部）已经闭环：COLMAP 文件 → `SceneInfo` → `Camera`/`Scene` → 初始化好的 4D 高斯。接下来两条路：

1. **主线（推荐）**：进入单元 3 深入 `GaussianModel` 本身。u3-l1 讲 3D 继承属性（`_xyz`/`_scaling`/`_rotation`/`_opacity` 与激活函数 property），u3-l2 讲 4D 新增的 `_t`/`_scaling_t`/`_rotation_r`，u3-l5 会展开本讲只点了入口的 `create_from_pcd`（distCUDA2 定尺度、随机 `_t` 与 `redundant_ratio`、`duration/5` 时间尺度）——正好解释综合实践中 `input.ply` 没有 time 属性时发生的事情。
2. **支线**：如果你更关心「训练目录里还有什么、怎么读回来」，可先跳 u5-l5（检查点与 `capture`/`restore` 元组），再回头看本讲 4.5 的三岔路会更有体感。

建议同步阅读的源码：`scene/__init__.py` 全文（128 行，半小时可精读一遍）与 `utils/camera_utils.py`，边读边对照本讲 4.1.2 的十步流程勾选。
