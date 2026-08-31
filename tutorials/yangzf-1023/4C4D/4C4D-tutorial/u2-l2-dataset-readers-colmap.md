# u2-l2 readColmapSceneInfo：从相机×帧到训练集

## 1. 本讲目标

上一讲（u2-l1）我们弄清了 `colmap_loader.py` 如何把 `sparse/0` 下的三个二进制/文本文件解析成 Python 字典。本讲沿着数据链路再走一步，进入 `scene/dataset_readers.py`，搞清楚这些"原始材料"是如何被装配成训练可直接使用的 `SceneInfo` 的。

学完本讲，你应该能够：

1. 说出 `readColmapSceneInfo` 从数据集目录到 `SceneInfo` 的完整流程，以及每一步在哪几行代码里。
2. 解释相机×帧的二维数据结构是如何从"每台相机一条 COLMAP 位姿"展开成"每台相机 × 每一帧"的。
3. 手工推导 timestamp 归一化公式 \( t = f / ((F_{\max}+1)/10) \)，并解释为什么它总是把帧号映射到 \([0, 10)\)。
4. 说明 `training_view` 这个字符串参数是如何一路传到 `readColmapSceneInfo` 并决定 train/test 划分的。
5. 掌握初始点云的三道加工工序：`fetchPly` 读取、`num_pts_ratio` 增强、`num_pts` + `downsample_method`（random / fps）下采样与时间过滤。

## 2. 前置知识

### 2.1 相机×帧：动态场景数据的二维结构

静态场景的 NeRF/3DGS 数据集是一个"相机列表"：每张照片对应一条位姿。而动态场景（4D）的数据天然是二维的：

```
           帧 0000   帧 0001   ...   帧 0299
cam00     [照片]    [照片]          [照片]
cam01     [照片]    [照片]          [照片]
cam02     [照片]    [照片]          [照片]
cam03     [照片]    [照片]          [照片]
```

4C4D 的核心假设是：**这 4 台相机在整段视频里是固定不动的**（用三脚架架好的便携相机）。因此一台相机只需要一条外参（位姿），它的所有帧共用这条外参。这个假设正是 `process_camera_info` 能够"一条位姿展开成 300 帧"的前提。

### 2.2 timestamp：把离散帧号变成连续时间

4D 高斯的第四维是连续时间。但数据里只有"第 37 帧"这样的整数帧号，所以必须做一个归一化，把帧号映射到一个固定的时间区间。4C4D 选择把这个区间硬编码为 \([0, 10)\)：

\[ t(f) = \frac{f}{(F_{\max}+1)/10} = \frac{10 f}{F_{\max}+1} \]

其中 \( f \) 是 4 位帧号，\( F_{\max} \) 是数据集中最大的帧号。注意分母设计得很巧妙：不管你有多少帧，最后一帧的 timestamp \( t(F_{\max}) = 10 F_{\max}/(F_{\max}+1) \) 永远小于 10。这个 "10" 与 `train.py` 的默认 `--time_duration 0 10.0`（[train.py:391](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L391)）以及 `configs/dynerf/*.yaml` 里的 `time_duration: [0.0, 10.0]` 是配套的。

### 2.3 训练/测试视角划分（held-out camera）

4C4D 评估动态场景的方式是"留出相机"：训练时只用其中几台相机的视频，测试时在剩下的相机上渲染并算 PSNR/SSIM。所以"划分"不是按帧切（那样训练和测试看到的是同样的视角，评估会虚高），而是**按相机切**。`training_view='1,10,13,20'` 的含义是：cam01、cam10、cam13、cam20 这 4 台相机用来训练，其余相机全部留给测试。

### 2.4 初始点云与下采样：random vs fps

回顾 u1-l2：3D/4D 高斯的初始空间尺度由 `simple_knn.distCUDA2` 根据初始点云的近邻距离决定，所以初始点云的**空间均匀性**会直接影响高斯的初始大小。两种下采样策略：

| 方式 | 实现 | 索引是否重复 | 空间均匀性 | 依赖 |
|---|---|---|---|---|
| `random` | `np.random.randint(0, N, num_pts)` | **有放回，可能重复** | 服从随机抽样，可能聚集也可能留洞 | 纯 CPU |
| `fps` | 最远点采样（pointops2 CUDA 算子） | 不重复 | 每次都选"离已选点集最远"的点，覆盖均匀 | 需要 GPU |

### 2.5 三个数据容器

| 容器 | 定义位置 | 字段 | 一句话职责 |
|---|---|---|---|
| `CameraInfo` | [scene/dataset_readers.py:42-58](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L42-L58) | uid, R, T, FovY/FovX, image, depth, image_path, image_name, width, height, **timestamp**, fl_x/fl_y/cx/cy | "一个相机在某一帧"的原始描述 |
| `BasicPointCloud` | [utils/graphics_utils.py:17-21](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/graphics_utils.py#L17-L21) | points, colors, normals, **time**（默认 None） | 初始点云的四元组，`time` 可选 |
| `SceneInfo` | [scene/dataset_readers.py:60-65](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L60-L65) | point_cloud, train_cameras, test_cameras, nerf_normalization, ply_path | 整个数据集装配完成的最终产物 |

## 3. 本讲源码地图

| 文件 | 本讲关注的函数 | 行号 | 作用 |
|---|---|---|---|
| `scene/dataset_readers.py` | `readColmapSceneInfo` | 255-351 | 主入口：装配 `SceneInfo` |
| `scene/dataset_readers.py` | `process_camera_info` | 177-253 | 相机×帧展开 + timestamp 归一化 |
| `scene/dataset_readers.py` | `readColmapCameras` | 91-143 | 从 COLMAP 字典造出"每台相机一条"的 `CameraInfo`（位姿模板） |
| `scene/dataset_readers.py` | `fetchPly` / `storePly` | 145-158 / 160-175 | 点云 PLY 的读取 / 首次转换写出 |
| `scene/dataset_readers.py` | `getNerfppNorm` | 67-88 | 由训练相机位置算场景包围半径（→ `cameras_extent`） |
| `utils/general_utils.py` | `fps` / `knn` | 186-194 / 170-184 | 对 pointops2 CUDA 算子的薄封装 |
| `scene/__init__.py` | `Scene.__init__` 中的分发 | 51-55 | 检测 `sparse/` 目录后调用 `readColmapSceneInfo` |
| `train.py` | `training_view` 的解析与转换 | 403-409, 467-471 | 把 `"1,10,13,20"` 转成 `['cam01','cam10','cam13','cam20']` |

另外会用到的背景：`pointops2/functions/pointops.py` 中的 `FurthestSampling`（14-31 行，仅看签名，深入留给 u8-l2）。

## 4. 核心概念与源码讲解

### 4.1 readColmapSceneInfo：主流程与训练/测试划分

#### 4.1.1 概念说明

`readColmapSceneInfo` 是 COLMAP 类数据集的**总装配函数**。它的输入是一个数据集根目录（例如 `data/N3V/flame_steak`），输出是一个填好的 `SceneInfo`。它自己不做底层解析，而是把工作分派给三个下属：`readColmapCameras`（读位姿）、`process_camera_info`（展开成相机×帧）、`fetchPly`（读点云），最后自己完成 train/test 划分与场景归一化。

值得先记住一个命名陷阱：这个函数的形参叫 `training_cam`，而上层 `train.py` 和 `Scene` 都叫 `training_view`，`Scene.__init__` 在调用时做了 `training_cam=training_view` 的换名（[scene/__init__.py:52-54](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/__init__.py#L52-L54)）。读代码搜 `training_view` 搜不到这一层，是常见的小坑。

#### 4.1.2 核心流程

```
readColmapSceneInfo(path, images, eval, num_pts_ratio, training_cam, testing_cam,
                    num_pts, time_duration, downsample_method)
│
├─ ① 读外参/内参：优先 .bin，失败则回退 .txt          (259-268)
├─ ② readColmapCameras：每个 COLMAP image 条目 → 1 个 CameraInfo（位姿模板）(271)
├─ ③ process_camera_info：位姿模板 × 目录里的 png → 每帧一个 CameraInfo  (272)
├─ ④ 按 image_name 排序 + 两条数量断言                (274-278)
├─ ⑤ eval=True 时按相机名前缀划分 train/test          (280-289)
├─ ⑥ getNerfppNorm：训练相机位置 → 场景半径           (291)
├─ ⑦ 点云三道工序：
│     不存在 .ply 则从 .bin/.txt 转换并写出           (293-302)
│     fetchPly 读取                                    (303-306)
│     num_pts_ratio > 1.001 时追加随机点               (308-322)
│     点数 > num_pts 时 random/fps 下采样 + 时间过滤    (324-344)
└─ ⑧ 打包 SceneInfo 返回                              (346-351)
```

#### 4.1.3 源码精读

**(a) 函数签名与默认值**。注意默认 `training_cam` 写的是 N3V 的 4 台训练相机，`num_pts=100_000`，`downsample_method='random'`：

[scene/dataset_readers.py:255-258](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L255-L258) —— 定义主函数签名，四个点云相关参数（`num_pts_ratio`、`num_pts`、`time_duration`、`downsample_method`）都来自 `train.py` 的命令行 / yaml。

**(b) bin → txt 回退**。`try` 里读 `sparse/0/images.bin` 与 `cameras.bin`，任何异常（通常是文件不存在）都落到 `except` 读 `.txt` 版本。这正是 u2-l1 讲过的"六个 read_* 函数兼容 bin 与 txt"在这里被消费的方式：

[scene/dataset_readers.py:259-268](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L259-L268) —— 先尝试二进制 COLMAP 模型，失败回退文本模型；注意裸 `except` 会吞掉所有错误（包括权限问题），排错时要留意。

**(c) 两段装配 + 双重断言**：

[scene/dataset_readers.py:270-278](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L270-L278) —— `images=None` 时用默认的 `images` 子目录；先造位姿模板、再展开成帧；随后按 `image_name` 排序，并用两条断言校验：① 展开后的 `CameraInfo` 数量必须等于 `images/` 目录下的**文件总数**；② 图像名必须不重复。

第一条断言有个非常实用的推论：`os.listdir` 统计的是目录下**所有**文件（包括非 png、包括隐藏文件），而展开逻辑只认 `.png`（见 4.2.3 (a)）。所以只要你在 `images/` 里多放了一张 `.jpg`、一个 `README.md` 或者 `.DS_Store`，第一条断言就会失败，报出 `Number of cameras does not match number of images in the directory.`。这是自备数据集时最常见的报错之一。

**(d) 训练/测试划分**。这是本讲的核心之一：

[scene/dataset_readers.py:280-289](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L280-L289) —— `eval=True` 时：训练集 = `image_name.split('_')[0]`（即 `cam01` 这一段）出现在 `training_cam` 列表里的所有帧；测试集 = 显式给了 `testing_cam` 就用它，否则取**不在**训练列表里的全部相机。`eval=False` 时不划分，所有帧都进训练集、测试集为空。

这里有一个 Python 语义陷阱要特别指出：`c.image_name.split('_')[0] in training_cam` 中的 `in`，当 `training_cam` 是 **list** 时是"成员判断"，但当它是**字符串**时退化为"子串判断"。`Scene` 的默认值 `['cam10', ...]` 是 list，没问题；但 `render.py` 的 `--training_view` 默认是空字符串（[render.py:141](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/render.py#L141)），若不传参它就以 `""` 原样传进来（[render.py:158-159](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/render.py#L158-L159) 的 `if args.training_view:` 为假、不转换），此时 `'cam00' in ""` 对所有相机都是 `False`，训练集会变成空列表——这会进一步让 4.1.3 (e) 的 `getNerfppNorm` 因 `np.hstack([])` 报错。因此**用 render.py 做验证时务必显式传 `--training_view`**（该行为为代码推演，待本地验证）。

**(e) 场景归一化**：

[scene/dataset_readers.py:67-88](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L67-L88) —— `getNerfppNorm` 把每台训练相机反算出相机中心（W2C 求逆取平移），取所有中心的均值作为 `center`、最远距离作为 `diagonal`，返回 `radius = diagonal * 1.1`。这个 radius 在 `Scene` 里被赋给 `cameras_extent`（[scene/__init__.py:84](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/__init__.py#L84)），用于缩放位置学习率与致密化阈值——细节留给 u2-l4。**注意它只喂 `train_cam_infos`**，所以训练相机越少、半径越小，这也是稀疏视角下训练超参被间接改变的一个隐性来源。

**(f) 打包返回**：

[scene/dataset_readers.py:346-351](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L346-L351) —— 把点云、两组相机、归一化参数、ply 路径装进 `SceneInfo` 返回。

**(g) 上游：谁调用了它**。`Scene.__init__` 检测到 `source_path/sparse` 存在就走 Colmap 分支；`train.py` 则负责把命令行字符串转成相机名列表：

[scene/__init__.py:51-55](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/__init__.py#L51-L55) —— 数据集类型分发：有 `sparse/` 目录按 Colmap 处理，把 `training_view` 换名为 `training_cam` 传入。

[train.py:403-409](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L403-L409) —— 注册 `--training_view`（默认 `"1,10,13,20"`）与 `--testing_view`（默认空串）两个字符串参数。

[train.py:467-471](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L467-L471) —— yaml 合并完成后，把 `"1,10,13,20"` 拆开、`sorted`、`zfill(2)` 补零，得到 `['cam01', 'cam10', 'cam13', 'cam20']`。**补零到 2 位意味着相机编号必须 < 100**；同时 `sorted()` 是字符串排序，`"2"` 会排在 `"10"` 前面（对本项目默认的两位编号无影响）。

#### 4.1.4 代码实践

**实践目标**：不改任何源码，构造一个最小的假 COLMAP 数据集，直接调用 `readColmapSceneInfo`，亲眼看到 train/test 划分与 `SceneInfo` 的内容。

**操作步骤**：

1. 在 u1-l2 搭好的 `4c4d` conda 环境中，先写数据生成脚本（示例代码，放在仓库外或临时目录均可，**不要**放进 `images/`）：

```python
# 示例代码：make_fake_colmap.py —— 构造 4 相机 × 5 帧的最小 COLMAP 文本数据集
import os

root, CAMS, FRAMES = "data/fake_demo", ["cam00", "cam01", "cam02", "cam03"], 5
img_dir, sparse_dir = os.path.join(root, "images"), os.path.join(root, "sparse", "0")
os.makedirs(img_dir, exist_ok=True); os.makedirs(sparse_dir, exist_ok=True)

# images/：只放 .png，绝不放其他文件（否则触发 277 行断言）
# 本函数只扫文件名、不读图像内容（image=None），所以空文件即可
for cam in CAMS:
    for f in range(FRAMES):
        open(os.path.join(img_dir, f"{cam}_{f:04d}.png"), "wb").close()

# cameras.txt：一台物理相机一行；文本路径只接受 PINHOLE（colmap_loader.py:159 的 assert）
with open(os.path.join(sparse_dir, "cameras.txt"), "w") as fp:
    fp.write("# Camera list\n")
    for i in range(len(CAMS)):
        fp.write(f"{i+1} PINHOLE 320 240 300.0 300.0 160.0 120.0\n")  # fx fy cx cy

# images.txt：每台相机只写第 0 帧的位姿（单位四元数=无旋转）；每个条目占两行，第二行给 1 个 2D 点
with open(os.path.join(sparse_dir, "images.txt"), "w") as fp:
    fp.write("# Image list\n")
    for i, cam in enumerate(CAMS):
        fp.write(f"{i+1} 1.0 0.0 0.0 0.0 {i} 0.0 -1.0 {i+1} {cam}_0000.png\n")
        fp.write("100.0 100.0 1\n")

# points3D.txt：POINT3D_ID X Y Z R G B ERROR TRACK...
with open(os.path.join(sparse_dir, "points3D.txt"), "w") as fp:
    fp.write("# 3D point list\n")
    for i in range(50):
        fp.write(f"{i+1} 0.{i%10} 0.1 -0.2 128 128 128 1.0\n")
```

2. 再写驱动脚本（示例代码）：

```python
# 示例代码：run_fake_reader.py —— 直接调用被测函数
from scene.dataset_readers import readColmapSceneInfo

si = readColmapSceneInfo(path="data/fake_demo", images=None, eval=True,
                         training_cam=["cam00", "cam01"],   # 只用前两台训练
                         num_pts=20, time_duration=[0.0, 10.0],
                         downsample_method="random")
print("train frames:", len(si.train_cameras))     # 期望 2 相机 × 5 帧 = 10
print("test  frames:", len(si.test_cameras))      # 期望 2 相机 × 5 帧 = 10
print("points:", si.point_cloud.points.shape,
      "| times:", si.point_cloud.time)            # 期望 (20, 3) | None（见 4.3.3 (b)）
print("nerf_norm:", si.nerf_normalization)
```

3. 运行 `python run_fake_reader.py`（需在仓库根目录，且已装好四个 CUDA 扩展，因为 `dataset_readers` 顶层 `import` 链会拉起 `pointops2`，见 u1-l2）。

**需要观察的现象**：

- 控制台先打印 `Max timestamp is : 4`（来自 4.2.3 (a) 的扫描），随后是两行进度条（`Reading cameras`、`Processing additional images`）。
- `data/fake_demo/sparse/0/` 下新生成了 `points3D.ply`（首次运行时 293-302 行的转换）。
- train/test 各 10 帧；`point_cloud.time` 为 `None`。

**预期结果**：4 台相机 × 5 帧 = 20 个 `CameraInfo`，与 `images/` 下 20 个 png 相等，两条断言通过；train=10、test=10；点云被 random 下采样到 20 个点。由于自动转换出的 ply 不含 `time` 字段，`time` 为 `None`，时间过滤不会触发。以上为代码推演，待本地验证。

**如果失败怎么排查**：报 `Number of cameras does not match...` → 检查 `images/` 是否混入非 png 文件；报 `Camera model not handled` / colmap_loader 的 `assert model == "PINHOLE"` → 检查 `cameras.txt` 模型名；`Max timestamp` 不是 4 → 检查文件名是否严格是 `camXX_YYYY.png`（4 位帧号）。

#### 4.1.5 小练习与答案

**练习 1**：如果把一个 `notes.md` 放进 `images/` 目录，`readColmapSceneInfo` 会在哪一行、以什么方式失败？

答案：在 [scene/dataset_readers.py:277](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L277) 的断言失败。展开逻辑只统计 `.png`（20 个），而 `os.listdir` 统计所有文件（21 个），两边不相等，抛出 `AssertionError: Number of cameras does not match number of images in the directory.`。

**练习 2**：`eval=False` 时（且 `training_cam` 仍传了 4 台相机），train/test 各有多少帧？划分逻辑被执行了吗？

答案：以 N3V（21 相机 × 300 帧）为例，train = 6300、test = 0。划分逻辑完全没执行——[287-289 行](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L287-L289) 的 `else` 分支直接把全部 `cam_infos` 给训练集、空列表给测试集，`training_cam` 被忽略。

**练习 3**：若 `training_view='1,10,13,20'` 同时 `testing_view='1,2'`，最终 train/test 各包含哪些相机？会不会有相机同时出现在两边？

答案：train = cam01、cam10、cam13、cam20（[282 行](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L282)）；test = cam01、cam02（[283-284 行](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L283-L284) 的 `if testing_cam` 分支优先）。**cam01 会同时出现在 train 和 test**——代码不做互斥检查，这是设计评估方案时要自己避开的坑。

### 4.2 process_camera_info：相机×帧展开与 timestamp 归一化

#### 4.2.1 概念说明

`process_camera_info` 解决的问题是：COLMAP/MASt3R 给出的 `images.bin` 通常只需要（也只应该）包含**每台相机一条**位姿，而训练需要的是**每一帧**的 `CameraInfo`。这个函数扫描 `images/` 目录里的实际文件名，把每条"位姿模板"复制到该相机的所有帧上，并给每个复制品填上自己的 uid、图片路径和 timestamp。

它隐含两个约定，自备数据时必须遵守：

1. 文件名必须是 `camXX_YYYY.png`：`_` 前是相机名、`.` 前最后 4 位是帧号、扩展名必须是 `.png`。
2. 位姿模板数 × 该相机帧数不能重复计数——因此 `images.bin`/`images.txt` 里**每台相机只能有一条记录**。如果同一台相机写了两条位姿，两条模板都会各自展开一遍，帧数翻倍，直接撞上 4.1.3 (c) 的数量断言。

#### 4.2.2 核心流程

```
process_camera_info(cam_infos_unsorted, path, reading_dir)
│
├─ ① 扫描 images/ 下所有 .png：
│     cam_name = 文件名.split('_')[0]            # cam00
│     max_timestamp = max(int(文件名.split('.')[0][-4:]))
│     image_files[cam_name].append(文件名)        # 按相机分组
├─ ② uid = max(现有 uid)                          # 新 uid 从这里继续编号
├─ ③ 对每个位姿模板 cam_info、该相机的每个文件 img：
│     若 img 的主干名 == 模板的 image_name → 跳过（模板自己已存在，避免重复）
│     uid += 1
│     timestamp = int(帧号) / ((max_timestamp + 1.0) / 10.0)
│     组装 task 字典
├─ ④ 线程池执行 task → 新 CameraInfo（继承 R/T/Fov/宽高，替换 uid/路径/名字/timestamp）
└─ ⑤ 返回 模板列表 + 新增列表
```

#### 4.2.3 源码精读

**(a) 目录扫描与命名约定**：

[scene/dataset_readers.py:183-188](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L183-L188) —— 只认 `.png`；用 `img.split('.')[0][-4:]` 取**最后 4 个字符**当帧号。两个直接推论：帧号必须固定 4 位（`0000`-`0299`），超过 9999 帧时 5 位帧号会被截成后 4 位得到错误时间戳；文件名里不能有第二个 `.`（`cam00.0001.png` 这类名字会被截错）。

**(b) 双层循环与"跳过自身"**：

[scene/dataset_readers.py:192-212](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L192-L212) —— `uid` 从现有最大值继续编号避免冲突；内层循环对模板相机的每个帧文件生成一个 task；`if img.split('.')[0] == image_name: continue` 跳过模板自身对应的那一帧（它已经在 `readColmapCameras` 的产物里了）。

**(c) timestamp 归一化（本讲最重要的一行）**：

[scene/dataset_readers.py:204](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L204) —— `time_stamp = int(img.split('.')[0][-4:]) / ((max_timestamp + 1.0) / 10.0)`。

以 N3V 的 300 帧（0000-0299，\( F_{\max}=299 \)）为例：

\[ \text{divisor} = \frac{299+1}{10} = 30, \qquad t(f) = \frac{f}{30} \in \left[0,\ \frac{299}{30}\right) = [0,\ 9.9\overline{6}) \subset [0, 10) \]

三条性质值得记住：

- **区间与帧数无关**：不管 5 帧还是 300 帧，timestamp 永远铺满 \([0,10)\)，只是步长不同（\( 10/(F_{\max}+1) \)）。5 帧时步长是 2，timestamp 为 0, 2, 4, 6, 8。
- **与 `time_duration` 默认值配套**：`train.py` 默认 `--time_duration 0 10.0`、yaml 写 `time_duration: [0.0, 10.0]`，正好覆盖这个区间。若你把 `time_duration` 改成别的区间（例如 4DGS 常用的 \([0,1]\)），相机 timestamp 与点云 `time` 字段的取值域就不再对齐。
- **`frame_ratio` 会同步缩放**：[train.py:52](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L52) 在训练入口把 `time_duration` 除以 `dataset.frame_ratio` 再往下传，用于跳帧采样的数据集。

**(d) 新 `CameraInfo` 的构造**：

[scene/dataset_readers.py:214-240](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L214-L240) —— 新帧的 `R/T/FovY/FovX/width/height/fl_*/c_*` 全部从模板复制（同一台物理相机，内外参恒定），只有 `uid`、`image_path`、`image_name`、`timestamp` 是自己的。注意 `temp_image = None`：这一步**不读图**，图像真正读入显存是 `Camera`/`CameraDataset` 的事（u2-l3 的懒加载主题）。

**(e) 线程池与合并**：

[scene/dataset_readers.py:242-253](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L242-L253) —— 由于不读图，这里的"多线程"只是加速构造 namedtuple，顺序由 `as_completed` 决定，因此最终顺序是不定的——这正是外层 274 行要 `sorted(key=image_name)` 的原因。

**(f) 输入侧：`readColmapCameras` 造的模板长什么样**：

[scene/dataset_readers.py:99-101](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L99-L101) —— `R = np.transpose(qvec2rotmat(extr.qvec))`、`T = np.array(extr.tvec)`：沿用 u2-l1 讲过的约定，存**转置后的 C2W 旋转**与 **W2C 平移**。

[scene/dataset_readers.py:103-116](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L103-L116) —— 内参只支持 `SIMPLE_PINHOLE`（单焦距）与 `PINHOLE`（双焦距），由 `focal2fov` 换算成 FovX/FovY；其他模型直接抛 `ValueError`，提示数据必须先做去畸变。

[scene/dataset_readers.py:118-128](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L118-L128) —— 由 `extr.name` 拼出图片路径、取主干名作为 `image_name`（即 `cam00_0000`），`image = None`。

#### 4.2.4 代码实践

**实践目标**：验证两件事——① 300 帧数据集的 timestamp 恰好落在 \([0,10)\)；② `training_view='1,10,13,20'` 时 train 与 test 相机各贡献多少帧。

**操作步骤（方式 A：在本地副本中加调试打印，推荐先做这个）**：

1. 复制一份仓库到你的实验环境（不要在原仓库上改）。打开 `scene/dataset_readers.py`，在 `readColmapSceneInfo` 里 **274 行排序完成之后**插入（示例代码，仅用于本地观察）：

```python
tss = sorted(c.timestamp for c in cam_infos)
print(f"[debug] frames={len(cam_infos)} t_min={tss[0]:.4f} t_max={tss[-1]:.4f}  # 期望 [0, 10)")
per_cam = {}
for c in cam_infos:
    per_cam[c.image_name.split('_')[0]] = per_cam.get(c.image_name.split('_')[0], 0) + 1
print("[debug] frames per camera:", per_cam)
```

2. 再在 **289 行划分完成之后**插入：

```python
print(f"[debug] train={len(train_cam_infos)} test={len(test_cam_infos)}")
```

3. 用一个 300 帧的真实数据（如 `data/N3V/flame_steak`）跑 `python train.py --config configs/dynerf/flame_steak.yaml`，在最开始的日志里读这两行输出后即可 `Ctrl-C` 终止（`Max timestamp is : 299` 也会在 190 行打印出来，可交叉核对）。

**操作步骤（方式 B：完全不改源码的独立复算）**：

```python
# 示例代码：verify_timestamp.py —— 只复算公式与划分，不触碰仓库
frames, max_ts = range(300), 299
t = [f / ((max_ts + 1.0) / 10.0) for f in frames]
assert min(t) == 0.0 and max(t) < 10.0, "timestamp 区间不符合预期"
print(f"divisor={(max_ts+1.0)/10.0}, t range=[{min(t):.4f}, {max(t):.4f})")

train_cams = [f"cam{v.zfill(2)}" for v in sorted("1,10,13,20".split(','))]
print("training_cam ->", train_cams)                       # 复现 train.py:467-468
n_cams = 21                                                # N3V: cam00~cam20
test_cams = [f"cam{i:02d}" for i in range(n_cams) if f"cam{i:02d}" not in train_cams]
print(f"train cams={len(train_cams)} -> {len(train_cams)*300} frames")
print(f"test  cams={len(test_cams)} -> {len(test_cams)*300} frames")
```

**需要观察的现象**：方式 B 中 `divisor=30.0`，t 的最大值是 `9.9667`（= 299/30）；`training_cam -> ['cam01', 'cam10', 'cam13', 'cam20']`。

**预期结果**（N3V，21 相机 × 300 帧 = 6300 帧）：train = 4 × 300 = **1200 帧**；test = 17 × 300 = **5100 帧**。所有 timestamp 落在 \([0, 9.9667] \subset [0,10)\)，与默认 `time_duration=[0,10]` 严格对齐。方式 A 的精确输出依赖真实数据，待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：一个 1000 帧的数据集（0000-0999），timestamp 的步长和取值范围是什么？

答案：\( F_{\max}=999 \)，除数 \( =(999+1)/10=100 \)，即 \( t(f)=f/100 \)，步长 0.01，范围 \([0, 9.99)\)。再次验证"区间永远是 \([0,10)\)、步长随帧数变化"。

**练习 2**：如果数据集有 10000 帧以上（帧号 5 位，如 `cam00_10000.png`），这段代码会发生什么？

答案：[187 行](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L187) 和 [204 行](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L204) 都用 `[-4:]` 截取最后 4 位，`10000` 会被截成 `0000` → 帧号 0，得到错误的时间戳 0.0，与真正的第 0 帧混淆；同时 `max_timestamp` 也可能被低估。要么把帧号限制在 4 位以内，要么修改这两行的解析逻辑。

**练习 3**：`readColmapCameras` 和 `process_single_image` 都把 `image` 设为 `None` 而不读图，为什么？

答案：因为这个阶段要构造的是**元数据**（几千帧的位姿+时间戳），如果在展开时就逐帧 `Image.open`，会有几千次解码却只用到一个路径字符串。真正的读图被推迟到 `Camera`/`CameraDataset`（配合 `dataloader` 开关），这是 u2-l3 的懒加载主题。

### 4.3 fetchPly：点云读取、数量控制与下采样

#### 4.3.1 概念说明

初始点云决定两件事：高斯的**初始位置**，以及（经 `distCUDA2`）高斯的**初始空间尺度**。4C4D 在稀疏视角下用 MASt3R 重建的稠密点云代替 COLMAP 稀疏点云（见 u2-l5），点数往往很多，所以 `readColmapSceneInfo` 在拿到点云后还有三道加工：

1. **格式兜底**：`sparse/0/points3D.ply` 不存在时，从 `points3D.bin`/`.txt` 现场转换一份（只发生一次）。
2. **增强**（可选）：`num_pts_ratio > 1.001` 时在场景均值附近追加随机点。
3. **下采样**：点数超过 `num_pts` 时按 `random` 或 `fps` 抽取，最后（若点云带时间戳）按 `time_duration` 过滤。

#### 4.3.2 核心流程

```
ply 不存在？
├─ 是 → read_points3D_binary/bin 失败再 read_points3D_text → storePly 写出  (296-302)
└─ 否 → 跳过
fetchPly(ply_path) → BasicPointCloud(points, colors, normals, time?)      (303-306)

num_pts_ratio > 1.001 ?
└─ 是 → num_pts 被改写为 (ratio-1)*N；在 mean±[0.5, 2.0, 0.5] 的盒子里加随机点
         重新构造的 BasicPointCloud 不带 time → 时间信息被丢弃             (308-322)

points.shape[0] > num_pts 且 num_pts > 0 ?
├─ random → np.random.randint(0, N, num_pts)   # 有放回
├─ fps    → general_utils.fps(points.cuda()[None], num_pts)  # CUDA 最远点采样
└─ 其他   → ValueError                                                       (324-330)

pcd.time 非 None ?
└─ 是 → time_mask = (t < time_duration[1]) & (t > time_duration[0])
         同步过滤 points/colors/normals/times → 点数可能小于 num_pts        (331-344)
```

#### 4.3.3 源码精读

**(a) `fetchPly`：四个字段全都要，`time` 是可选的**：

[scene/dataset_readers.py:145-158](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L145-L158) —— 从 PLY 的 vertex 元素里取 xyz、rgb（除以 255 归一化）、法线（没有就置零），并且**只有当 vertex 里存在 `time` 属性时**才返回非 None 的时间列。这个 `time` 是 MASt3R/MAtCha 管线产出的带时间戳稠密点云才有的（u2-l5）；它是否为 None，直接决定后面 331-344 行的时间过滤会不会发生，也决定 `create_from_pcd` 里 `_t` 是用点云时间还是随机采样（u3-l5）。

**(b) 首次转换路径会丢掉时间戳**：

[scene/dataset_readers.py:293-302](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L293-L302) —— ply 不存在时从 `points3D.bin`/`.txt` 读出 xyz/rgb 并 `storePly` 写出。

[scene/dataset_readers.py:160-175](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L160-L175) —— `storePly` 的 dtype 只有 `x y z nx ny nz red green blue` 八列，**没有 `time`**。所以走"首次转换"路径得到的 ply 永远不含时间戳，`fetchPly` 返回 `time=None`。换句话说：想让点云时间戳生效，必须**预先放好带 `time` 字段的 `points3D.ply`**（即走 MASt3R 流程），而不是指望代码从 COLMAP 的 bin 转出来。

**(c) `num_pts_ratio`：增强分支的三副作用**：

[scene/dataset_readers.py:308-322](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L308-L322) —— 当 `num_pts_ratio > 1.001` 时：① `num_pts` 被**改写**为 \((\text{ratio}-1)\times N\)（注意语义变了：它不再表示目标点数，而表示"要追加多少随机点"，随后 (d) 的下采样阈值也跟着变）；② 在点云均值周围的盒子里（x/z 方向 ±0.5，y 方向 +0.5~+2.0，注意 y 是**单边向上**的）追加均匀随机点，颜色来自 `SH2RGB(随机值/255)`；③ 重新 `BasicCloud(points=…, colors=…, normals=…)` 时**没传 `time`**——即使原点云带时间戳，这一步也会把它丢掉，后续时间过滤随之失效。默认配置 `num_pts_ratio: 1.0` 不触发此分支。

**(d) 下采样：random 与 fps**：

[scene/dataset_readers.py:324-330](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L324-L330) —— 只有当"点数多于 `num_pts` 且 `num_pts > 0`"才下采样（点云本来就少时原样保留，这是稀疏视角下的合理默认）。`random` 用 `np.random.randint`，是**有放回**抽样，索引可能重复；`fps` 调 CUDA 最远点采样，索引不重复、空间覆盖均匀；其他值直接 `ValueError`。

**(e) `fps` 封装与 offset 约定**：

[utils/general_utils.py:186-194](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/general_utils.py#L186-L194) —— `fps(x, k)` 接收 `(b, n, 3)`，先压平成 `(n, 3)`，再用 `torch.cumsum` 构造 `offset`（每个批次段的结束位置）与 `new_offset`（=k），最后调用 pointops2 的 `furthestsampling` 取回扁平的索引数组 `(k,)`。这就是为什么 [328 行](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L328) 要先 `.cuda()[None]` 增加一个 batch 维、返回后又 `.cpu().numpy()` 直接当索引用。算子内部（[pointops2/functions/pointops.py:14-31](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/pointops2/functions/pointops.py#L14-L31)，输入 `xyz:(n,3)`、`offset:(b)`，输出 `idx:(m)`）在 u8-l2 展开。

**(f) 时间过滤：只在点云带时间时发生**：

[scene/dataset_readers.py:331-344](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L331-L344) —— 先用下采样索引同步抽取 points/colors/normals/times 四个数组（**必须同步**，漏掉任何一个都会造成行错位），再用**严格不等号** \( t < \text{dur}[1] \wedge t > \text{dur}[0] \) 过滤。注意边界：恰好等于 `time_duration[0]` 或 `[1]` 的点会被丢弃，最终点数可能**小于** `num_pts`。当 `time_duration` 与点云时间取值域对齐（如都基于 \([0,10]\)）时这个过滤近乎 no-op；它真正的用途是配合 `frame_ratio`/跳帧，只训练一段时间窗口。

#### 4.3.4 代码实践

**实践目标**：用同一个 1 万点点云，分别按两种方式下采样到 1 千点，量化"索引重复率"和"空间均匀性"的差异。

**操作步骤**（示例代码，需要 GPU 才能跑 `fps` 分支；无 GPU 可只跑 random 分支并读代码对比）：

```python
# 示例代码：compare_downsample.py —— 复现 dataset_readers.py:325-328 的两种 mask
import numpy as np, torch
from utils.general_utils import fps, knn

pts = np.random.RandomState(0).rand(10000, 3) * 10.0
N = 1000
mask_r = np.random.randint(0, pts.shape[0], N)                       # 与 random 分支一致
mask_f = fps(torch.from_numpy(pts).cuda()[None], N).cpu().numpy()   # 与 fps 分支一致

for name, mask in [("random", mask_r), ("fps", mask_f)]:
    sub = torch.from_numpy(pts[mask]).cuda()[None].float()
    idx, dist = knn(sub, sub, 2)          # k=2：第 1 近邻是自身(距离 0)，第 2 近邻才是真最近邻
    nn = dist[0, :, 1].cpu().numpy()
    print(f"{name:6s} 唯一索引 {len(set(mask.tolist()))}/{N} | "
          f"最近邻距离 mean={nn.mean():.3f} std={nn.std():.3f}")
```

**需要观察的现象**：两行的"唯一索引数"和"最近邻距离分布"。

**预期结果**：`random` 的唯一索引约 630/1000（有放回抽样重复率的经典值 \( 1-1/e \approx 63.2\% \)），最近邻距离均值较小、方差较大（有聚簇）；`fps` 唯一索引为 1000/1000，最近邻距离均值更大、方差更小（点被"撑开"、覆盖均匀）。这与 `distCUDA2` 用近邻距离初始化尺度相呼应：fps 下的初始高斯尺度更一致。具体数值待本地验证。

**源码阅读型替代实践（无 GPU）**：把 `downsample_method` 从 `random` 改成 `fps` 需要在 [328 行](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L328) 处加 `.cuda()`——请阅读 `train.py` 的 `--downsample_method`（[train.py:423](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L423)，`choices=['fps','random']`）与 `flame_steak.yaml` 未设置该项的事实，回答：默认配置下走哪条分支？若 yaml 里写 `downsample_method: fps`，按 u1-l4 的合并规则它会不会生效？（会——`assert hasattr` 白名单里有这个键。）

#### 4.3.5 小练习与答案

**练习 1**：配置 `num_pts: 300_000`，但点云只有 15 万点，最终初始点云有多少点？

答案：15 万，原样保留。[324 行](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L324) 的条件是 `points.shape[0] > num_pts and num_pts > 0`，点数不超过阈值时不做任何下采样；`num_pts` 只是**上限**不是目标值。

**练习 2**：`num_pts_ratio=1.5`、原始点云 10 万点时会追加多少随机点？追加后 `pcd.time` 是什么？

答案：`num_pts = int((1.5-1) * 100000) = 50000` 个随机点（[309 行](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L309)）。追加后 [322 行](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L322) 重建的 `BasicPointCloud` 没有传 `time`，所以 `pcd.time is None`——原点云的时间戳信息被丢弃。

**练习 3**：`time_duration=[0, 10]`、点云 `time` 字段的取值是 \([0,1]\)（4DGS 惯例），时间过滤后会发生什么？

答案：[339 行](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L339) 的条件 `t < 10 且 t > 0` 对 \((0,1)\) 内的几乎全部点都成立，只有恰好等于 0 或 1 的点被严格不等号剔除——过滤近似 no-op。反过来若 `time_duration=[0,1]` 而点云 time 在 \([0,10]\)，则只剩约 1/10 的点。这提醒我们：**相机 timestamp 的归一化区间、点云 time 字段、`time_duration` 三者必须同域**。

## 5. 综合实践

把本讲三块内容串成一个"数据体检报告"。基于 4.1.4 的假数据集脚本，扩展为 4 相机 × 10 帧（0000-0009）、`points3D.txt` 给 200 个点，然后写一个脚本（示例代码）依次回答：

```python
# 示例代码：audit_dataset.py —— 对同一个数据集做多组参数对照
from scene.dataset_readers import readColmapSceneInfo

cases = [
    dict(tag="eval=False",          eval=False, training_cam=["cam00"], num_pts=1000),
    dict(tag="4cams/2cams",         eval=True,  training_cam=["cam00", "cam01"], num_pts=1000),
    dict(tag="num_pts=50",          eval=True,  training_cam=["cam00"], num_pts=50),
    dict(tag="ratio=1.5",           eval=True,  training_cam=["cam00"], num_pts_ratio=1.5),
]
base = dict(path="data/fake_demo", images=None, time_duration=[0.0, 10.0],
            downsample_method="random")
for c in cases:
    tag, kw = c.pop("tag"), {**base, **c}
    si = readColmapSceneInfo(**kw)
    pc = si.point_cloud
    ts = sorted(c.timestamp for c in si.train_cameras)
    print(f"{tag:14s} train={len(si.train_cameras):3d} test={len(si.test_cameras):3d} "
          f"pts={pc.points.shape[0]:5d} time={'Y' if pc.time is not None else 'N'} "
          f"t∈[{ts[0]:.2f},{ts[-1]:.2f}]")
```

然后在报告中回答五个问题（每题都能在源码里指出对应行号）：

1. `eval=False` 与 `eval=True`（训练 1 台相机）两种情况下 train/test 帧数各是多少？（对应 280-289 行）
2. 训练相机从 2 台减到 1 台，`nerf_normalization["radius"]` 如何变化？这对 `cameras_extent` 意味着什么？（对应 67-88 行）
3. `num_pts=50` 时点云是多少个点？再把它改成 500（大于 200）呢？（对应 324 行的条件）
4. `num_pts_ratio=1.5` 时点云变成多少个？`time` 字段还在吗？（对应 308-322 行）
5. 10 帧时 timestamp 的步长是多少？与 300 帧时的步长相比，说明归一化区间的哪个性质？（对应 204 行）

**预期结果**（按代码推演，待本地验证）：① 40/0 与 10/30；② 相机越少 radius 越小；③ 50 与 200（不下采样）；④ 200+100=300 个点、`time=N`；⑤ 步长 1.0（`10/(9+1)`）对 0.0333（`10/300`），区间都铺满 \([0,10)\)。

## 6. 本讲小结

- `readColmapSceneInfo`（[scene/dataset_readers.py:255-351](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L255-L351)）是 COLMAP 类数据集的总装配线：读内外参（bin 优先、txt 回退）→ 位姿模板 → 展开成相机×帧 → 排序断言 → train/test 划分 → 场景归一化 → 点云三道加工 → `SceneInfo`。
- 相机×帧展开由 `process_camera_info` 完成，前提是"固定相机 + 每台相机只写一条 COLMAP 位姿"；文件名必须严格是 `camXX_YYYY.png`（4 位帧号）。
- timestamp 归一化 \( t = 10f/(F_{\max}+1) \) 把任意帧数映射到固定的 \([0,10)\)，与默认 `time_duration=[0,10]` 配套；相机 timestamp、点云 `time` 字段、`time_duration` 三者必须同域。
- train/test 是**按相机**划分的：`training_view` 字符串在 [train.py:467-468](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L467-L468) 转成 `camXX` 列表后在 280-289 行生效；`testing_cam` 与 `training_cam` 不做互斥检查；`eval=False` 时不划分。
- 点云加工：`fetchPly` 只在 PLY 带 `time` 属性时返回时间戳，而自动转换路径（`storePly`）不写 `time`；`num_pts_ratio>1.001` 会追加随机点并**丢弃时间戳**；`num_pts` 只是下采样上限，`random` 有放回、`fps` 均匀但需要 CUDA。
- 两个高频排错点：`images/` 混入非 png 文件会让 277 行断言失败；`training_cam` 若是字符串，`in` 会退化成子串匹配导致划分异常。

## 7. 下一步学习建议

- **u2-l3（Camera 对象与懒加载数据集）**：本讲的 `CameraInfo` 只是元数据，下一讲看它如何变成带 `world_view_transform`/`full_proj_transform` 的 `Camera`，以及图像何时才真正读入显存——这正好接上本讲"image=None"的伏笔。
- **u2-l4（Scene 类）**：看 `SceneInfo` 如何被 `Scene` 消费：`nerf_normalization["radius"]` 如何变成 `cameras_extent` 并缩放学习率、`input.ply`/`cameras.json` 写出什么。
- **u2-l5（数据准备脚本与 MASt3R 初始化）**：本讲反复提到"带 time 字段的点云来自 MASt3R"，下一讲看 `scripts/n3v2colmap.py` 如何准备出符合 `camXX_YYYY.png` 约定的目录。
- **u3-l5（create_from_pcd 与 PLY 持久化）**：本讲产出的 `BasicPointCloud`（含 `time` 是否为 None）如何决定 4D 高斯 `_t` 的初始化方式。
- **u8-l2（pointops2 与点云下采样策略）**：深入 `furthestsampling`/`knnquery` 的 CUDA 实现与 offset 批处理约定。
