# u7-l2 novel-view 轨迹生成

## 1. 本讲目标

上一讲（u7-l1）我们走通了 `render.py --validate` 的评估链路；本讲转向它的另一个模式：`--traj`，即**渲染一条新视角轨迹视频**。

学完本讲，你应该能够：

1. 说出 `generate_path` 的完整数据流：从 `scene.getAllCameras()` 拿到全部相机，到产出一串带新位姿与新 timestamp 的 `Camera` 对象。
2. 解释三种轨迹模式 `ellipse` / `interpolate` / `arc` 各自如何插值出新视角，以及 `scale_factor` 在哪里生效。
3. 推导轨迹 timestamp 公式 \( t_i = 10 \cdot i / n_{frames} \) 与数据侧 timestamp 公式 \( t = 10f/(F+1) \) 之间的换算关系（`total_frames` 的角色）。
4. **通过实测源码行为纠正两个直觉偏差**：`selected_frame` 冻结的是视角而非时间；`fix_time=True` 在 `total_frames=300` 时是空操作。

## 2. 前置知识

本讲假设你已读过 u2-l3（Camera 的四个矩阵）与 u7-l1（render.py 结构）。这里补三个新概念：

- **novel view（新视角）**：训练时每台相机的位姿是固定的几个点；轨迹渲染要在这些点之间"无中生有"地插值出连续移动的虚拟相机，检验 4D 高斯场是否真正学会了三维几何——只会记忆训练视角的模型会在新视角上立刻穿帮。
- **look-at 视图矩阵**：轨迹相机的位姿由"站在哪（position）+ 看向哪（lookdir）+ 头顶朝向（up）"三要素决定，`viewmatrix` 函数负责把三要素正交化成旋转矩阵。本讲所有轨迹都让相机始终看着场景焦点。
- **坐标系往返**：COLMAP/OpenGL 相机系（z 向后、y 向下）与 NeRF/LLFF 相机系（z 向前、y 向上）相差一个轴翻转 `diag(1,-1,-1,1)`。轨迹生成算法（PCA、椭圆拟合）在 NeFF 系里做，算完再翻回 COLMAP 系存进 Camera。这与 u2-l5 讲过的 `poses_bounds.npy` 轴系转换是同一套约定。

另有两个本讲反复用到的记号：

- \( F \)：每台相机的总帧数（N3V 数据集为 300，即命令行 `--total_frames`，也是帧号的最大值 +1）。
- \( n \)：轨迹帧数，`render.py` 中硬编码为 `n_frames = 480`。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [utils/render_utils.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/render_utils.py) | 轨迹生成与视频合成工具库 | `generate_path` 及三种路径生成器 |
| [render.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/render.py) | 推理入口 | `validation()` 的轨迹渲染段与相关命令行参数 |
| [scene/\_\_init\_\_.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/__init__.py) | 场景装配 | `getAllCameras` |
| [scene/cameras.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/cameras.py) | Camera 渲染对象 | timestamp 的存储与四个矩阵的重建 |
| [utils/data_utils.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/data_utils.py) | CameraDataset | `__getitem__` 返回 `(图像, Camera)` 元组这一约定 |
| [gaussian_renderer/\_\_init\_\_.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py) | 渲染入口 | timestamp 的消费端 |

## 4. 核心概念与源码讲解

### 4.1 getAllCameras：轨迹的原料从哪来

#### 4.1.1 概念说明

轨迹插值需要"锚点"——若干真实相机的位姿。`generate_path` 的锚点来自 `scene.getAllCameras()`，它返回**训练相机 + 测试相机**的全部帧。这个选择有两层含义：

- 用**全部相机**（而不仅是训练相机）做锚点，让轨迹覆盖整个采集装置张成的空间，包括从未参与训练的留出相机方向；
- 返回的是**全部帧**（4 台相机 × 300 帧 = 1200 个 `Camera`），但固定相机下每台相机所有帧的位姿完全相同，所以 `generate_path` 第一步就要做"去重采样"——每台相机只留一个位姿模板。

#### 4.1.2 核心流程

```text
Scene 构造（render.py 中 shuffle=False）
  ├─ train_cameras[1.0]：按 image_name（camXX_YYYY）字典序 → 相机分组、组内按帧号升序
  └─ test_cameras[1.0]：同上
getAllCameras() = train 列表 + test 列表        # train 在前，test 在后
       ↓
generate_path 内部：range(0, len, total_frames) 步长采样
       ↓
每台相机恰好留下一个位姿模板（第 0 帧）
```

#### 4.1.3 源码精读

[scene/\_\_init\_\_.py:L126-L127](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/__init__.py#L126-L127) 中 `getAllCameras` 把训练相机与测试相机拼接后包成 `CameraDataset` 返回：

```python
def getAllCameras(self, scale=1.0):
    return CameraDataset(self.train_cameras[scale].copy() + self.test_cameras[scale].copy(), self.white_background)
```

顺序保证来自两处。其一，[scene/dataset_readers.py:L274](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L274) 把相机×帧的列表按 `image_name`（形如 `cam00_0123`）做字典序排序，结果是**相机优先、组内按帧号升序**的连续块。其二，[render.py:L48](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/render.py#L48) 构造 Scene 时显式传 `shuffle=False`（训练入口 train.py 不传此参数、默认打乱）——如果相机被打乱，"每 `total_frames` 个取一个"的采样就会采到错乱的帧，因此这一行是轨迹正确性的隐性前提。

采样端消费的是 [utils/render_utils.py:L386](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/render_utils.py#L386)：

```python
viewpoint_cameras = [viewpoint_cameras[i] for i in range(0, len(viewpoint_cameras), total_frames)]
```

`viewpoint_cameras[i]` 是对 `CameraDataset` 的下标访问，触发 [utils/data_utils.py:L12-L19](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/data_utils.py#L12-L19) 的 `__getitem__`，返回 `(viewpoint_image, viewpoint_cam)` 二元组——这解释了后文代码里 `cam[1].world_view_transform` 的 `[1]` 下标：**轨迹生成只关心元组里的 Camera，`cam[0]` 的图像被丢弃**。顺带一个低效细节：当 `dataloader=True`（N3V 配置默认）时 Camera 是 `meta_only` 的，`__getitem__` 会真的从磁盘 `cv2.imread` 读图再被扔掉；锚点数量少（相机台数），代价可忽略。

#### 4.1.4 代码实践

**实践目标**：不跑训练，静态推演"步长采样"取到了哪些帧。

**操作步骤**：

1. 阅读上述三处源码。
2. 用纸笔或 Python 算：`training_view='1,10,13,20'`（4 台训练相机）且 `testing_view` 未指定时，N3V 300 帧数据在 `eval=True` 下 `getAllCameras()` 的长度是多少？`range(0, len, 300)` 采到哪些索引？这些索引各落在哪台相机的哪一帧？

**需要观察的现象 / 预期结果**：`len = 4×300（train）+ N_test×300（test）`；采样索引为 `0, 300, 600, ...`，每个索引恰好是某台相机的块内第 0 帧。若你的数据集每台相机不是 300 帧（例如抽稀过），而不相应修改 `--total_frames`，采样会漂移到错误相机——这正是该参数存在的意义。本实践为纯推演，结论可直接从排序代码得出；含真实数据的核对**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：`getTrainCameras` 与 `getAllCameras` 返回的相机集合有何区别？轨迹渲染为什么用后者？

**答案**：前者只有训练相机（参与优化的视角），后者是训练 + 测试相机的并集。轨迹渲染追求覆盖整个采集装置张成的空间（包括留出相机方向），用全部相机做插值锚点能让轨迹弧段更完整。

**练习 2**：若把 render.py 的 `Scene(..., shuffle=False, ...)` 改回默认 `shuffle=True`，`generate_path` 会出什么问题？

**答案**：相机列表被打乱后，"每 `total_frames` 个取一个"的等距采样不再对应"每台相机的第 0 帧"，采到的可能是任意相机的任意帧组合。由于固定相机下每台相机所有帧位姿相同，只要采样**恰好**落在各相机块内仍能侥幸正确，但一般情形下锚点集合会错乱，拟合出的椭圆/弧也就不可信。

### 4.2 generate_path 骨架：步长采样、坐标系往返与模式分发

#### 4.2.1 概念说明

`generate_path` 是一个"总装函数"：它不自己插值，而是负责**取锚点 → 换坐标系 → 按模式调用路径生成器 → 换回坐标系 → 用锚点相机的内参模板造出 n 个新 Camera**。理解它的关键是一条坐标系往返链和"新相机克隆自旧相机"的模板机制。

#### 4.2.2 核心流程

```text
输入 viewpoint_cameras（全部相机全部帧）
  1. 步长采样 total_frames → 每台相机一个位姿模板（4.1 已讲）
  2. world_view_transform(W2C，转置存储) --转置还原--> inv --> C2W
  3. C2W @ diag(1,-1,-1,1)  →  NeRF 系位姿 poses
  4. 按 traj 分发：
       ellipse     → PCA 重居中 + 整椭圆 → 变换回世界系
       interpolate → 周期样条穿过锚点
       arc         → 拟合椭圆 + 取锚点张成的弧段
  5. 新位姿 @ diag(1,-1,-1,1)  →  回 COLMAP 系
  6. （可选）selected_frame ≥ 0 → 把某一个位姿复制 n 份
  7. 逐位姿：deepcopy 锚点相机 → 覆盖四个矩阵与 timestamp → 新 Camera
输出 traj：长度 n_frames 的 Camera 列表
```

#### 4.2.3 源码精读

坐标系切换在 [utils/render_utils.py:L388-L391](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/render_utils.py#L388-L391)：Camera 存的 `world_view_transform` 是 W2C 且做了转置存储（u2-l3 讲过的 CUDA 列主序约定），所以先 `.T` 还原成数学矩阵、求逆得 C2W，再右乘轴翻转矩阵进入 NeRF 系：

```python
c2ws = np.array([np.linalg.inv(np.asarray((cam[1].world_view_transform.T).cpu().numpy())) 
                 for cam in viewpoint_cameras])
poses = c2ws[:, :3, :] @ np.diag([1, -1, -1, 1])
```

模式分发与回程在 [utils/render_utils.py:L393-L405](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/render_utils.py#L393-L405)，`scale_factor` 只透传给 `ellipse` 与 `arc` 两种模式：

```python
if traj == 'ellipse':
    pose_recenter, colmap_to_world_transform = transform_poses_pca(poses)
    new_poses = generate_ellipse_path(poses=pose_recenter, n_frames=n_frames, scale_factor=scale_factor)
    new_poses = np.linalg.inv(colmap_to_world_transform) @ pad_poses(new_poses)
elif traj == 'interpolate':
    new_poses = generate_smooth_interpolation_path(poses=poses, n_frames=n_frames)
elif traj == 'arc':
    new_poses = generate_arc_path(poses=poses, n_frames=n_frames, scale_factor=scale_factor, clockwise=False)
else:
    raise ValueError(f'Trajectory type {traj} not supported.')
new_poses = new_poses @ np.diag([1, -1, -1, 1])
```

新相机的组装在 [utils/render_utils.py:L407-L433](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/render_utils.py#L407-L433)。三段逻辑值得分别读：

```python
if selected_frame >= 0:
    new_poses = [new_poses[selected_frame]] * n_frames
```

这一行（L407-L408）把第 `selected_frame` 个**位姿**复制成 n 份——注意它冻结的是视角，时间戳仍在第 4.4 节的循环里照常流动，得到的是"固定机位看完整动态"的视频，而非"转视角看静止瞬间"（与参数名的直觉相反，详见 4.4）。

接着（L413-L428）以**第一台锚点相机**为模板 `deepcopy`，这就是轨迹相机的内参来源——焦距、主点、投影矩阵全部继承锚点，轨迹只改变外参：

```python
cam = copy.deepcopy(viewpoint_cameras[0][1])
cam.image_height = int(cam.image_height / 2) * 2
cam.image_width = int(cam.image_width / 2) * 2
...
cam.world_view_transform = torch.from_numpy(np.linalg.inv(pose_3x4).T).float().cuda()
cam.full_proj_transform = (cam.world_view_transform.unsqueeze(0).bmm(
    cam.projection_matrix.cuda().unsqueeze(0))).squeeze(0)
cam.camera_center = cam.world_view_transform.inverse()[3, :3]
```

四个要点：宽高取偶是为后续 h264 视频编码（YUV420 需要 2 的倍数）；`world_view_transform` 存回时同样转置并直接 `.cuda()`；`full_proj_transform` 与 `camera_center` 用与 [scene/cameras.py:L71-L72](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/cameras.py#L71-L72) 完全相同的公式重建——`generate_path` 必须手工维护这四个矩阵的自洽，因为它们平时只在 `Camera.__init__` 里计算一次，这里绕过了构造函数。`traj_archived != 'ellipse'` 分支（L418-L421）先把 3×4 位姿补齐成 4×4 再求逆，与 ellipse 分支直接对 4×4 求逆在数学上等价，只是两种模式返回的矩阵形状不同（3×4 对 4×4）。

调用端在 [render.py:L63-L75](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/render.py#L63-L75)：`n_frames` 硬编码 480，随后交给 `GaussianExtractor.reconstruction` 逐相机渲染（u7-l3 详讲）。

#### 4.2.4 代码实践

**实践目标**：验证"坐标系往返"是恒等变换，即步骤 2→3→5 走一圈应还原出原始 W2C。

**操作步骤**（示例代码，可保存为独立脚本在无 GPU 环境运行）：

```python
import numpy as np
D = np.diag([1.0, -1.0, -1.0, 1.0])
rng = np.random.default_rng(0)
A = rng.normal(size=(4, 4))
w2c = A / np.linalg.norm(A, axis=0)          # 任取一个满秩矩阵当 W2C（示例代码）
c2w = np.linalg.inv(w2c)
poses = c2w @ D                              # 进入 NeRF 系
back = np.linalg.inv(poses @ D)              # 步骤 5 后求逆回到 W2C
print(np.allclose(back, w2c))                # 期望 True
```

**需要观察的现象**：输出 `True`；再把 `D` 换成单位阵重复实验，理解轴翻转若只做一次会发生什么（位姿会被镜像，出现左右翻转的"镜面世界"）。

**预期结果**：往返恒等成立。此脚本只依赖 numpy，可直接运行；**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：轨迹相机的内参从哪来？为什么 `generate_path` 不需要自己算投影矩阵？

**答案**：来自 `deepcopy(viewpoint_cameras[0][1])`，即第一台锚点相机的 `projection_matrix`（含 FoV、焦距、主点）。`generate_path` 只重算外参相关的 `world_view_transform`，再把模板的投影矩阵与新的视图矩阵相乘得到 `full_proj_transform`。

**练习 2**：为什么循环里要写 `cam.image_height = int(cam.image_height / 2) * 2`？

**答案**：把宽高向下取偶。`create_videos` 用 `codec: 'h264'` 合成视频，YUV420 色度抽样要求图像两个维度都是偶数，奇数分辨率的帧会导致编码失败。

**练习 3**：如果三种模式都不满足，`generate_path` 的行为是？

**答案**：抛出 `ValueError(f'Trajectory type {traj} not supported.')`（L402-L403），合法取值仅 `ellipse` / `interpolate` / `arc`。注意 render.py 侧 `--traj` 默认 `None`，而 `validation()` 里用 `if traj:` 判断，所以空字符串同样不会触发轨迹渲染。

### 4.3 三种轨迹模式：ellipse、interpolate 与 arc

#### 4.3.1 概念说明

三种模式回答同一个问题——"在锚点之间怎么走"，但胆量不同：

| 模式 | 路径形状 | 是否离开锚点张成的区域 | 典型用途 |
| --- | --- | --- | --- |
| `ellipse` | 绕场景一整圈（360°）的完整椭圆 | 是，会绕到没有观测的背面 | 全向漫游演示 |
| `interpolate` | 周期样条平滑穿过所有锚点位置 | 否，严格贴着锚点 | 温和的视角巡游 |
| `arc` | 锚点张成的椭圆弧 ± 10% 外扩 | 基本不离开 | 4 相机稀疏视角（README 推荐） |

三种模式共享两个几何基元：**焦点** `focus_point_fn`（所有相机光轴的公垂线最近点，即"大家都在看的那一点"）与 **look-at 视图矩阵** `viewmatrix`（站在 p、看着 focus、头顶朝 up）。

#### 4.3.2 核心流程

三个生成器都以 `poses`（NeRF 系 C2W）为输入，输出 n 个 look-at 位姿：

```text
ellipse:  焦点 center → PCA 重居中（主成分对齐坐标轴）→ 半轴取 90 分位半径×scale_factor
          → theta ∈ [0, 2π] 均匀取 n+1 点、丢弃重复末点（闭环）
          → up 取「平均 up 向量绝对值最大的坐标轴」→ look-at
interpolate: 锚点位置绕焦点按极角排序成闭环 → splprep 周期三次样条拟合（s=smoothness）
          → splev 在 u ∈ [0,1) 均匀采 n 点 → up 用 estimate_up_vector → look-at
arc:      SVD 拟合锚点所在平面与椭圆长短轴 → 计算每台锚点相机的椭圆极角
          → 判定是否跨 0° 环绕 → 两端外扩 |arc_extension|·span（默认 10%）
          → theta 在弧段内均匀取 n 点（generate_path 固定逆时针）→ look-at
```

`scale_factor` 的作用点：ellipse 中直接缩放椭圆半轴（`sc = np.percentile(...) * scale_factor`），arc 中缩放拟合出的 `semi_major/semi_minor`，大于 1 表示"把相机轨道向外推"，小于 1 表示贴近场景；interpolate 不接收该参数，锚点在哪路径就在哪。

#### 4.3.3 源码精读

**共享基元**。[utils/render_utils.py:L39-L45](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/render_utils.py#L39-L45) 的 `viewmatrix` 用两次叉积把 `lookdir/up/position` 正交化成旋转矩阵，三列分别是相机的右、上、前方向加平移：

```python
def viewmatrix(lookdir, up, position):
    vec2 = normalize(lookdir)
    vec0 = normalize(np.cross(up, vec2))
    vec1 = normalize(np.cross(vec2, vec0))
    m = np.stack([vec0, vec1, vec2, position], axis=1)
    return m
```

[utils/render_utils.py:L47-L53](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/render_utils.py#L47-L53) 的 `focus_point_fn` 对每台相机构造投影矩阵 \( I - d d^{\top} \)（\(d\) 为单位视线方向，该矩阵把任意点投到过光心、垂直视线的直线上），再最小化所有相机上投影距离平方和，得到所有视线的"最近交汇点"：

\[ p^* = \left( \frac{1}{N}\sum_i (I - d_i d_i^{\top}) \right)^{-1} \cdot \frac{1}{N}\sum_i (I - d_i d_i^{\top})\, o_i \]

**ellipse**。[utils/render_utils.py:L169-L181](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/render_utils.py#L169-L181) 用三角函数在 x-y 界定的包围盒内画椭圆，`theta` 采 `n_frames+1` 个点后丢弃重复的末点形成闭环，半轴由锚点半径的 90 分位乘 `scale_factor` 决定（L149）：

```python
theta = np.linspace(0, 2. * np.pi, n_frames + 1, endpoint=True)
positions = get_positions(theta)
positions = positions[:-1]          # 闭环：末点与首点重合，丢掉
...
return np.stack([viewmatrix(p - center, up, p) for p in positions])
```

注意它运行在 `transform_poses_pca`（[L103-L132](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/render_utils.py#L103-L132)）重居中后的坐标系里：PCA 把锚点位置的主成分对齐到坐标轴，"在 x-y 平面画椭圆"才有意义，画完再左乘 `inv(colmap_to_world_transform)` 变回世界系（generate_path L397）。

**interpolate**。[utils/render_utils.py:L221-L233](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/render_utils.py#L221-L233) 先把锚点位置按绕焦点的极角排序并首尾相接成闭环，再用 scipy 的周期样条 `splprep(per=True)` 拟合、`splev` 均匀采样——路径严格穿过每台锚点相机的位置，`smoothness=0.0` 时不做任何平滑：

```python
tck, u = splprep([positions_loop[:, 0], positions_loop[:, 1], positions_loop[:, 2]], 
                 s=smoothness, per=True)
u_new = np.linspace(0, 1, n_frames, endpoint=False)
interpolated = splev(u_new, tck)
```

**arc**。[utils/render_utils.py:L265-L268](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/render_utils.py#L265-L268) 用 SVD 拟合锚点所在平面与长短轴（`fit_ellipse_to_points`，L75-L101，最小奇异向量即平面法向），随后在 [L290-L332](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/render_utils.py#L290-L332) 计算每台锚点相机的椭圆极角 `arctan2(y/semi_minor, x/semi_major)`、判定极角是否跨过 0°（wrap-around），并把弧段两端各外扩 `angle_span × |arc_extension|`（默认 `arc_extension=-0.1`，即 10%）：

```python
arc_padding = angle_span * abs(arc_extension)
arc_start = min_angle - arc_padding
arc_end = max_angle + arc_padding
```

对 4C4D 的 4 相机输入，锚点只占椭圆的一小段，arc 模式生成的轨迹就在这段弧上往返滑动，**不会绕到完全没有观测约束的背面**——这就是 README 第 4 节推荐 `--traj arc` 的原因（[README.md:L169-L178](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/README.md#L169-L178)）。该函数其余大量 `print`（L252-L379）是作者留下的执行过程自检日志，会打印焦点、椭圆参数、弧段角度范围与旋转方向验证。

#### 4.3.4 代码实践

**实践目标**：对比三种模式对同一组锚点生成的相机中心轨迹。

**操作步骤**：

1. 复用 4.4 节综合实践里的假相机脚本，把 `traj='arc'` 分别换成 `'ellipse'` 与 `'interpolate'` 各跑一次。
2. 收集三种模式下的 `camera_center` 序列，用 matplotlib 在同一坐标系里画散点（锚点位置用红色大点标出）。

**需要观察的现象**：ellipse 的中心点构成一个绕原点的**完整闭环**，且会经过锚点从未覆盖的背面区域；interpolate 的路径**穿过**每个红色锚点；arc 的点只落在锚点弧段附近（含 10% 外扩），数量同为 `n_frames` 个。

**预期结果**：三张图直观呈现上表的"是否离开锚点区域"。需要 GPU 与 matplotlib 环境，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 4C4D 的 README 推荐用 `arc` 而不是 `ellipse` 渲染轨迹视频？

**答案**：4 台相机只在采集环上占一小段弧。`ellipse` 会绕行 360°，其中大部分区域没有任何训练视角约束，4D 高斯场在这些方向的外推质量没有保证，视频会出现明显穿帮；`arc` 把轨迹限制在锚点张成的弧段（外扩 10%），属于视角间的内插，质量可靠。

**练习 2**：`interpolate` 模式下把 `--scale 2.0` 传入会发生什么？

**答案**：没有任何效果。`generate_path` 只在 `ellipse` 与 `arc` 两个分支把 `scale_factor` 传给路径生成器，`interpolate` 分支的调用不含该参数，路径永远贴着锚点。

**练习 3**：`focus_point_fn` 为什么不直接用锚点位置的均值当焦点？

**答案**：均值只反映"相机在哪"，与相机朝向无关。`focus_point_fn` 利用每台相机的视线方向构造投影，求的是所有视线的最近交汇点，反映"大家共同看向哪"。固定相机都朝场景中心看时两者接近，但相机不对称摆放或朝向不一致时只有后者能保证 look-at 目标合理。

### 4.4 Camera timestamp：total_frames 换算与 fix_time / selected_frame

#### 4.4.1 概念说明

轨迹相机是 4D 的：除了"站在哪"，还要回答"渲染哪个时刻"。timestamp 沿用 u2-l2 建立的归一化域——数据侧把帧号 \( f \) 映射为

\[ t = \frac{10\,f}{F+1} \in [0, 10) \]

（[scene/dataset_readers.py:L204](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L204)，`time_duration=[0,10]` 下成立），Camera 把它存为普通 Python float（[scene/cameras.py:L74](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/cameras.py#L74)），渲染时经 [gaussian_renderer/\_\_init\_\_.py:L48](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L48) 进入光栅化设置，决定 4D 高斯被"切片"的时刻。轨迹侧则是**人为指定**一条时间线，让虚拟相机在移动的同时播放整个动态过程。

#### 4.4.2 核心流程

时间戳赋值在 [utils/render_utils.py:L429-L432](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/render_utils.py#L429-L432)，与位姿循环合在一起：

```python
if not fix_time:
    cam.timestamp = 10.0 / n_frames * i
else:
    cam.timestamp = 10.0 / n_frames * i * total_frames / 300.0
```

两条时间线：

- `fix_time=False`（默认）：\( t_i = 10i/n \)，\( i \in [0, n) \)。480 帧时 \( t_i \in [0, 9.979) \)，把整个 `time_duration` 均匀走一遍。
- `fix_time=True`：\( t_i = \dfrac{10\,i}{n} \cdot \dfrac{F}{300} \)。**当 \( F = 300 \)（N3V 默认）时与默认分支完全相同**，是一个空操作；只有 \( F \neq 300 \) 时才把时间轴整体缩放——\( F = 150 \) 时视频只覆盖 \( [0, 5) \)（前半段动态），\( F = 600 \) 时会超出 10 越过 `time_duration` 上界。

由 \( t_i \) 反解"虚拟帧号"可得两条时间线的对应关系：

\[ f_i = \frac{(F+1)\, i}{n} \]

N3V（\( F = 299, n = 480 \)）时 \( f_i = 0.625\,i \)：480 帧视频在 300 个真实时刻之间**连续采样**，相邻轨迹帧之间的动态由 4D 高斯的连续时间建模（`_t`、时间球谐）插值出来——这正是 4D 表示相对逐帧 3D 重建的本质优势，也是轨迹视频比"拼接原始帧"更平滑的原因。

`selected_frame` 与时间无关：如 4.2 所述，它把第 `selected_frame` 个**位姿**复制 n 份，timestamp 仍按上式递增，产出"固定机位观看完整动态"的视频。若想要相反的效果（"转视角、时间冻结"），源码现状下需要自行把 timestamp 固定为某个 \( t \)（见综合实践的扩展任务）。

#### 4.4.3 源码精读

消费端印证 timestamp 的两种用途。[gaussian_renderer/\_\_init\_\_.py:L48](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L48) 把它写进 `GaussianRasterizationSettings`，供 CUDA 核按时刻切片 4D 协方差（u3-l3 的 Schur 补条件协方差）；[gaussian_renderer/\_\_init\_\_.py:L67](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L67) 在衰减分支用它调 `get_marginal_t(viewpoint_camera.timestamp)` 判断时间可见性——不过该分支仅在传入 `args` 且训练迭代时激活（u6-l3），推理渲染不触发，因此轨迹渲染只受 checkpoint 中已累积的衰减状态影响，不产生新衰减。

命令行入口在 [render.py:L139](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/render.py#L139)、[L150](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/render.py#L150)、[L152](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/render.py#L152)：`--total_frames`（默认 300）、`--fix_time`（store_true）、`--selected_frame`（默认 -1）。

还有一个硬编码陷阱值得记录：timestamp 公式里的 `10.0` 假定 `time_duration` 长度为 10。configs/dynerf 全部配置的 `time_duration: [0.0, 10.0]` 与之吻合；但 render.py 的命令行默认值是 `[-0.5, 0.5]`，若忘记带 `--config`（u7-l1 已强调），轨迹 timestamp 会整体落在真实时间域之外。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：写一个只调用 `generate_path` 的小脚本，用假 Camera 打印生成轨迹的相机位置与 timestamp 序列，实测 `fix_time` 与 `selected_frame` 的真实行为。

**操作步骤**：

1. 把下面的示例代码保存为仓库外的 `traj_probe.py`（或 4C4D-tutorial 目录下均可，不要改动源码）：

```python
# 示例代码：用 4 台假相机探测 generate_path 的行为（需 GPU，待本地验证）
import numpy as np
from scene.cameras import Camera
from utils.render_utils import generate_path

def make_cam(i):
    """在 XY 平面上绕原点放一台 look-at 原点的相机（模拟 4 相机弧）。"""
    ang = np.pi / 3 * i                                   # 0°, 60°, 120°, 180°
    pos = np.array([np.cos(ang) * 4.0, np.sin(ang) * 4.0, 0.5])
    forward = -pos / np.linalg.norm(pos)
    up = np.array([0.0, 0.0, 1.0])
    right = np.cross(up, forward); right /= np.linalg.norm(right)
    true_up = np.cross(forward, right)
    c2w = np.eye(4)
    c2w[:3, 0], c2w[:3, 1], c2w[:3, 2], c2w[:3, 3] = right, true_up, forward, pos
    w2c = np.linalg.inv(c2w)
    # Camera 的约定（见 u2-l1/u2-l3）：R 存 C2W 旋转，T 存 W2C 平移
    return Camera(colmap_id=i, R=w2c[:3, :3].T, T=w2c[:3, 3],
                  FoVx=np.pi / 2, FoVy=np.pi / 2, image=None, gt_alpha_mask=None,
                  image_name=f"cam{i:02d}_0000", uid=i,
                  resolution=(64, 64), timestamp=0.0, meta_only=True)

# 模拟 (image, cam) 二元组结构；generate_path 只会取 cam[1]
cams = [(None, make_cam(i)) for i in range(4)]

for fix_time in (False, True):
    traj = generate_path(cams, n_frames=12, traj='arc', total_frames=300,
                         fix_time=fix_time, selected_frame=-1)
    ts = np.array([c.timestamp for c in traj])
    centers = np.stack([c.camera_center.cpu().numpy() for c in traj])
    print(f"fix_time={fix_time}: ts={np.round(ts, 3)}")
    print(f"  首末相机位置 {np.round(centers[0], 2)} -> {np.round(centers[-1], 2)}")

traj = generate_path(cams, n_frames=12, traj='arc', total_frames=300,
                     fix_time=False, selected_frame=3)
centers = np.stack([c.camera_center.cpu().numpy() for c in traj])
print("selected_frame=3: 所有位姿相同 =", np.allclose(centers, centers[0]),
      " ts 仍在流动 =", traj[0].timestamp != traj[-1].timestamp)
```

2. 在仓库根目录用 `python traj_probe.py` 运行（脚本通过 `from scene...` / `from utils...` 导入，需在根目录；`generate_path` 内部有 `.cuda()` 调用，需要一块 GPU）。

**需要观察的现象**：

1. `fix_time=False` 与 `fix_time=True` 两行打印的 `ts` **完全相同**（因为 `total_frames=300`）——这是"fix_time 在 N3V 默认参数下是空操作"的直接证据。
2. 把两处 `total_frames=300` 改成 `150` 再跑：`fix_time=True` 的 `ts` 只走到 5.0 为止，而 `fix_time=False` 仍走到约 9.17。
3. `selected_frame=3` 一行输出"所有位姿相同 = True，ts 仍在流动 = True"。
4. `centers` 序列沿锚点弧段移动，起点与终点不重合（arc 不闭环）。

**预期结果**：与上述四条一致；若现象 1/3 与你读参数名产生的预期（"fix_time 冻结时间"、"selected_frame 选中某一帧的时间"）不符，说明你抓住了本讲最想传达的结论——**这两个开关的实际语义要以源码为准**。本脚本在无 GPU 环境无法运行（`torch.from_numpy(...).cuda()` 会报错），完整运行**待本地验证**；无 GPU 时可退化为纯数值实验：单独打印 `10.0 / 12 * np.arange(12)` 与其乘 `150/300` 后的序列，验证时间线换算。

#### 4.4.5 小练习与答案

**练习 1**：N3V 数据 `F=299`、`n_frames=480`、fps 48。轨迹视频第 100 帧对应哪个原始帧号？视频时间流速是原始视频（按 30fps 播放）的多少倍？

**答案**：\( f_{100} = 300 \times 100 / 480 = 62.5 \)，即落在原始第 62 与 63 帧之间，由 4D 高斯连续时间建模插值。视频时长 \( 480/48 = 10 \) 秒，原始视频 \( 300/30 = 10 \) 秒，时长相同，轨迹以 1.6 倍的帧数覆盖同样的时间域，相当于时间轴上的超采样而非快放。

**练习 2**：想渲染"视角沿弧移动、但画面定格在 \( t = 5.0 \)"的视频，在不动源码的前提下能否用现有参数组合出来？

**答案**：不能。`selected_frame` 只复制位姿（L407-L408），timestamp 无条件走 L429-L432 的递增公式；`fix_time` 在 `total_frames=300` 下不改变任何值。现有参数只能得到"固定机位 + 时间流动"。要定格时间需要修改 `generate_path`（例如增加一个 `fix_timestamp` 参数把 timestamp 固定为常数）——可作为二次开发小练习。

**练习 3**：为什么 `generate_path` 里 timestamp 公式硬编码 `10.0` 而不读 `time_duration`？

**答案**：它与数据侧公式（dataset_readers.py L204 的 `((max_timestamp + 1.0) / 10.0)` 分母）以及 configs/dynerf 的 `time_duration: [0.0, 10.0]` 三方约定一致，作者把"时间域长度为 10"当成了全局常量。代价是：一旦数据或配置改成别的时间域（例如 render.py 不带 `--config` 时的默认 `[-0.5, 0.5]`），轨迹 timestamp 会整体错位到真实时间域之外，渲染出的动态与训练时间轴对不上。这是典型的隐式耦合，二次开发时应改为从 `time_duration` 计算域长。

## 5. 综合实践

**任务：给你的 4 相机场景产出一个"慢镜头弧线视频"，并写一份轨迹说明书。**

前置：一个已训练完成的输出目录（含 `chkpnt30000.pth`）与对应 yaml。整体分三步：

1. **基线渲染**。执行 README 第 4 节的命令（[README.md:L169-L178](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/README.md#L169-L178)）：

   ```bash
   python render.py \
     --config configs/dynerf/flame_steak.yaml \
     --training_view 1,10,13,20 \
     --output_dir traj_test \
     --traj arc \
     --start_checkpoint output/N3V/flame_steak/chkpnt30000.pth
   ```

2. **参数扫描**。基于 4.4 节的换算 \( f_i = (F+1)i/n \)，先用纸面计算回答：`--scale 1.2` 与 `--scale 0.8` 分别把相机轨道外推/内收多少？`--selected_frame 240` 会固定在弧上哪个位置的机位？然后分别渲染 `--scale 1.2`、`--selected_frame 240` 两个变体。
3. **写轨迹说明书**。在输出目录（`traj/ours_30000/`）中核对：视频帧数是否为 480、`renders/` 下 PNG 命名是否为 5 位零填充序号（`create_videos` 的 `zpad` 逻辑，[utils/render_utils.py:L443-L448](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/render_utils.py#L443-L448)），并用 4.4.4 的探查脚本记录本场景轨迹的 timestamp 序列与相机中心序列，整理成"第 i 帧 ↔ 虚拟帧号 ↔ 相机位置"三列对照表。

**验收标准**：三个视频都能播放；`--selected_frame` 版本机位完全静止而画面动态持续；对照表能解释任意一帧渲染的是哪个时刻、站在哪个位置。整个流程需要 GPU 与训练产物，**待本地验证**。

## 6. 本讲小结

- 轨迹锚点来自 `getAllCameras()`（train + test 全部帧），`generate_path` 以 `total_frames` 为步长采样、每台固定相机只留一个位姿模板，正确性依赖 render.py 的 `shuffle=False` 与 `image_name` 字典序排布。
- `generate_path` 是总装函数：W2C→C2W→NeRF 系→按模式插值→回 COLMAP 系，再以锚点相机为模板 `deepcopy` 重建 `world_view_transform` / `full_proj_transform` / `camera_center` 三个矩阵（内参原样继承），宽高取偶以适配 h264。
- 三种模式胆量递减：`ellipse` 绕整圈会进入无观测的背面、`interpolate` 样条贴着锚点、`arc` 只走锚点弧段 ±10% 外扩——稀疏 4 视角下 README 推荐 `arc`；`scale_factor` 只对 ellipse/arc 生效。
- 时间线：轨迹 timestamp \( t_i = 10i/n \) 与数据侧 \( t = 10f/(F+1) \) 同域，反解 \( f_i = (F+1)i/n \) 即"虚拟帧号"；公式硬编码 10.0，隐式要求 `time_duration` 长度为 10。
- 两个反直觉行为务必记住：`selected_frame` 冻结的是**视角**（时间照常流动）；`fix_time=True` 在 `total_frames=300` 时是**空操作**，仅当 \( F \neq 300 \) 时缩放时间轴。
- 推理路径上 timestamp 经 `GaussianRasterizationSettings` 进入 CUDA 核完成 4D 切片，衰减分支（`get_marginal_t`）在推理渲染中不激活，只读 checkpoint 中已累积的不透明度状态。

## 7. 下一步学习建议

下一讲（u7-l3）将补完轨迹链路的最后一段：`GaussianExtractor` 如何包装 `render()` 批量渲染 `cam_traj` 并导出 PNG，`create_videos` 如何把帧合成 mp4（含深度伪彩色、turbo colormap），以及 `traj/ours_N` 输出目录的完整结构。建议提前浏览 [utils/mesh_utils.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/mesh_utils.py) 中 `reconstruction` 与 `export_image` 两个方法，并思考：为什么轨迹渲染必须绕过训练循环、直接以无梯度方式调用 `render()`？学完本单元后，可进入单元 8 的 CUDA 光栅化器内部实现（u8-l1），从渲染管线的最底层闭环整个学习路线。
