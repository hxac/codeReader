# u2-l3 Camera 对象与懒加载数据集

## 1. 本讲目标

上一讲（u2-l2）我们看到 `readColmapSceneInfo` 最终产出的三容器：`CameraInfo`、`BasicPointCloud`、`SceneInfo`。其中 `CameraInfo` 只是一个朴素的 `NamedTuple`——它有内外参和图像路径，但**还不能直接用来渲染**。本讲沿着数据链路再往前走一步，回答三个问题：

1. `CameraInfo` 是如何变成渲染器能用的 `Camera` 对象的？`world_view_transform`、`full_proj_transform`、`camera_center` 这三个矩阵/向量各自是什么、怎么算出来的？
2. `loadCam` 如何用 `resolution` 参数同时缩放图像尺寸和相机内参？为什么两者必须同步？
3. `dataloader=True` 时，图像到底在什么时刻才被读进内存/显存？为什么 4C4D 必须这样做？

学完本讲，你应该能独立构造一个 `Camera`，说清楚它携带的每一个矩阵的含义，并解释一次训练中图像数据「从磁盘到显存」的完整时间线。

## 2. 前置知识

### 2.1 从 CameraInfo 说起（承接 u2-l2）

`CameraInfo` 是一个 `NamedTuple`，字段包括 `uid / R / T / FovY / FovX / image / depth / image_path / image_name / width / height / timestamp / fl_x / fl_y / cx / cy`（见 [scene/dataset_readers.py:42-58](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L42-L58)）。回顾两个关键约定：

- `R` 存的是 **C2W（相机到世界）旋转**（在 u2-l1 中由 `qvec2rotmat(qvec).T` 得到），`T` 存的是 **W2C（世界到相机）平移**；
- `timestamp` 已归一化到 \([0, 10)\) 区间，与 `time_duration` 同域。

而 `Camera` 是渲染时真正传给 `render()` 的对象，它在 `CameraInfo` 基础上补齐了投影矩阵、图像张量等渲染所需的一切。

### 2.2 视图矩阵与投影矩阵

- **视图矩阵（view matrix，W2C）**：把世界坐标 \(x_{world}\) 变换到相机坐标 \(x_{cam}\)，即 \(x_{cam} = R_{w2c} x_{world} + t\)。写成齐次形式就是 4×4 矩阵。
- **投影矩阵（projection matrix）**：把相机坐标映射到裁剪空间（clip space），描述「相机看到的一个视锥体」——由焦距/视场角、近远裁剪面、主点位置决定。
- **两者相乘**得到 `full_proj_transform`：世界坐标一步到裁剪空间，这正是光栅化器投影高斯时需要的矩阵。

### 2.3 行主序与列主序（一个容易踩的坑）

PyTorch / NumPy 的矩阵是**行主序的数学矩阵**（列向量约定：\(y = Mx\)）。而本项目 CUDA 光栅化器按**列主序**解释传入的 4×4 缓冲。因此 `Camera` 在存储这些矩阵前统一做了 `.transpose(0, 1)`——传入的其实是「数学矩阵的转置」。本讲只要记住：**代码里的 `world_view_transform = 数学 W2C 的转置**，推导公式时先在脑子里转回来。

### 2.4 PyTorch 的 Dataset 与 DataLoader

- `torch.utils.data.Dataset`：只需实现 `__len__` 和 `__getitem__(index)`，就可以像列表一样按索引取样本。
- `DataLoader`：在 Dataset 之上提供 shuffle、多进程预取（`num_workers`）、批组织（`collate_fn`）。
- **懒加载（lazy loading）**：构造 Dataset 时不读图像，只在 `__getitem__` 被调用时才从磁盘读。这是处理「上千帧视频」的关键技巧。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [scene/cameras.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/cameras.py) | 定义 `Camera`（渲染用相机）与 `MiniCam`（轻量相机，本仓库未被调用） |
| [utils/camera_utils.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/camera_utils.py) | `loadCam`：把 `CameraInfo` 变成 `Camera`，处理分辨率缩放；`cameraList_from_camInfos` 批量转换 |
| [utils/data_utils.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/data_utils.py) | `CameraDataset`：配合 `dataloader` 开关的懒加载图像数据集 |
| [utils/graphics_utils.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/graphics_utils.py) | `getWorld2View2` / `getProjectionMatrix` / `getProjectionMatrixCenterShift` 等矩阵工具 |
| [utils/general_utils.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/general_utils.py) | `PILtoTorch`：非懒加载路径的图像读取与缩放 |
| [scene/__init__.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/__init__.py) | `Scene` 中按分辨率构造 Camera 列表、`getTrainCameras` 返回 `CameraDataset` |
| [train.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py) | 训练循环中 DataLoader 的创建与每个 batch 的 `.cuda()` 时机 |

数据链路一览（承接 u2-l2 结尾）：

```
readColmapSceneInfo ─→ CameraInfo 列表（image=None，只带 image_path）
        │
        ▼  cameraList_from_camInfos → loadCam（分辨率缩放、内参同步）
Scene.train_cameras[1.0] ─→ Camera 列表（meta_only=True 时不含图像）
        │
        ▼  scene.getTrainCameras()
CameraDataset ─→ DataLoader(num_workers=12)
        │  __getitem__ 时才 cv2.imread  ← 懒加载发生点
        ▼
train.py: gt_image.cuda() / viewpoint_cam.cuda()  ← 进显存发生点
        │
        ▼
render(viewpoint_cam, ...) 消费 world_view_transform / full_proj_transform / camera_center / timestamp
```

## 4. 核心概念与源码讲解

### 4.1 Camera：从 CameraInfo 到可渲染相机

#### 4.1.1 概念说明

`Camera` 是 4C4D 数据链路中的「渲染单元」：一个相机在一个时刻的完整描述。它把 `CameraInfo` 里的原始内外参，加工成 CUDA 光栅化器直接可用的形式：

- `world_view_transform`：W2C 视图矩阵（转置存储）；
- `projection_matrix`：投影矩阵（转置存储），有两种构造方式（FoV 或带主点偏移的内参）；
- `full_proj_transform`：两者之积，投影高斯时一步到位；
- `camera_center`：相机光心在世界坐标的位置；
- `image` / `image_width` / `image_height` / `timestamp`：真值图像（可能为 `None`，见 4.3）与渲染尺寸、时刻。

#### 4.1.2 核心流程

`Camera.__init__` 的执行顺序：

1. 存储基本字段（uid、R、T、FoV、内参、image_path、meta_only 等）；
2. 尝试解析 `data_device`，失败则回退 `cuda`；
3. 从 `resolution` 元组取图像宽高；
4. 若非 `meta_only`，把图像与 alpha 掩码相乘（无掩码则乘全 1）；
5. 设定 `znear=0.01`、`zfar=100.0`；
6. 依次计算 `world_view_transform` → `projection_matrix` → `full_proj_transform` → `camera_center`；
7. 记录 `timestamp`。

三个矩阵的数学关系（注意代码里都是转置存储，下面按数学约定写）：

\[
W2C =
\begin{bmatrix}
R_{w2c} & t \\
0 & 1
\end{bmatrix},
\qquad
full\_proj = W2C \cdot P
\]

\[
camera\_center = -R_{w2c}^{\top} t = -R_{c2w}\, t
\]

由于代码里 `R` 存的是 \(R_{c2w}\)，上式也可以直接写成 \(camera\_center = -(Camera.R \cdot Camera.T)\)，这是后面实践中要验证的关系。

#### 4.1.3 源码精读

构造函数签名（参数很多，但一半是默认值）：

[scene/cameras.py:20-25](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/cameras.py#L20-L25) 定义了 `Camera` 的全部输入。注意 `timestamp=0.0`、`cx=-1, cy=-1, fl_x=-1, fl_y=-1` 这几个默认值——`cx<=0` 正是后面选择投影矩阵构造方式的判断条件。

字段存储与设备回退：

[scene/cameras.py:27-52](https://github.com/yangzf-1023-4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/cameras.py#L27-L52) 把 `CameraInfo` 的字段逐一搬到实例属性上；`data_device` 解析失败时打印警告并回退到 `cuda`（[scene/cameras.py:44-49](https://github.com/yangzf-1023-4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/cameras.py#L44-L49)）。第 51-52 行 `self.image_width = resolution[0]`、`self.image_height = resolution[1]` 说明传入的 `resolution` 是 **(宽, 高)** 顺序的元组。

meta_only 分支——懒加载的第一个伏笔：

[scene/cameras.py:54-58](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/cameras.py#L54-L58) 中，只有 `not self.meta_only` 时才把图像乘以 alpha 掩码（无掩码则乘全 1 张量）。当 `meta_only=True` 时，`image` 可以是 `None`，`Camera` 只携带「元数据 + 投影矩阵」，这就是后面懒加载的前提。

三个矩阵的诞生：

```python
self.world_view_transform = torch.tensor(getWorld2View2(R, T, trans, scale)).transpose(0, 1)
if cx > 0:
    self.projection_matrix = getProjectionMatrixCenterShift(...).transpose(0,1)
else:
    self.projection_matrix = getProjectionMatrix(znear=..., zfar=..., fovX=..., fovY=...).transpose(0,1)
self.full_proj_transform = (self.world_view_transform.unsqueeze(0).bmm(self.projection_matrix.unsqueeze(0))).squeeze(0)
self.camera_center = self.world_view_transform.inverse()[3, :3]
```

这段代码位于 [scene/cameras.py:66-72](https://github.com/yangzf-1023-4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/cameras.py#L66-L72)。逐行解读：

- 第 66 行：`getWorld2View2` 产出数学 W2C（见下），`transpose(0,1)` 转成列主序约定；
- 第 67-70 行：**两条投影矩阵构造路径**。`cx > 0` 表示 `CameraInfo` 带了真实的 COLMAP 内参（主点不一定在图像中心），用 `getProjectionMatrixCenterShift`；否则退回用 FoV 构造（隐含主点居中假设）；
- 第 71 行：`full_proj_transform = world_view_transform @ projection_matrix`（`bmm` 是带批维度的矩阵乘，`unsqueeze(0)`/`squeeze(0)` 只是临时加去批维）；
- 第 72 行：`world_view_transform.inverse()` 是 \((W2C)^{-1} = C2W\)（同样是转置存储），取其第 3 行前 3 列。由于存储的是 \(C2W^{\top}\)，`[3, :3]` 恰好等于数学矩阵 \(C2W\) 的平移列——即相机光心。

`getWorld2View2` 的实现：

[utils/graphics_utils.py:39-50](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/graphics_utils.py#L39-L50) 先用 `R.transpose()`（把 C2W 旋转转回 W2C 旋转）和 `t` 拼出 W2C；随后**先求逆得到 C2W，对光心施加 `translate`/`scale` 再逆回去**。默认 `trans=0, scale=1` 时这两步是恒等，只有 Blender 合成数据等场景才会用到位姿归一化。

投影矩阵的两条构造路径：

[utils/graphics_utils.py:52-72](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/graphics_utils.py#L52-L72) 的 `getProjectionMatrix` 从 FoV 出发：

\[
P[0,0] = \frac{1}{\tan(FoV_x/2)}, \quad
P[1,1] = \frac{1}{\tan(FoV_y/2)}, \quad
P[3,2] = 1
\]

注意 `P[3,2] = z_sign = 1.0`，即裁剪空间的 \(w = z_{cam}\)（相机前方为正 z）；\(P[0,2]=P[1,2]=0\) 隐含「主点在图像正中心」。

[utils/graphics_utils.py:74-92](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/graphics_utils.py#L74-L92) 的 `getProjectionMatrixCenterShift` 则从像素内参出发构造视锥四条边：

```python
top    =  cy / fl_y * znear
bottom = -(h-cy) / fl_y * znear
left   = -(w-cx) / fl_x * znear
right  =  cx / fl_x * znear
```

主点偏移通过 \(P[0,2] = \frac{right+left}{right-left}\)、\(P[1,2] = \frac{top+bottom}{top-bottom}\) 进入投影。**可以验证**：当 \(cx=w/2, cy=h/2\) 时，\(right = \frac{w/2}{fl_x} z_{near} = \tan(FoV_x/2)\, z_{near}\)，两条路径完全等价（这将是 4.2 实践的验证内容）。

其余成员：

- `timestamp`：[scene/cameras.py:74](https://github.com/yangzf-1023-4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/cameras.py#L74)，来自 u2-l2 的归一化时间戳，渲染 4D 高斯时由 [gaussian_renderer/__init__.py:48](https://github.com/yangzf-1023-4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L48) 消费；
- `get_rays()`：[scene/cameras.py:76-83](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/cameras.py#L76-L83)，用 `cx/cy/fl_x/fl_y` 生成每个像素的世界空间光线，仅在 env_map 背景分支被调用（[gaussian_renderer/__init__.py:174-185](https://github.com/yangzf-1023-4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L174-L185)，用于光线与球面求交采样背景图）——因此它依赖 `fl_x > 0`，只在带内参的数据上可用；
- `cuda()`：[scene/cameras.py:85-90](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/cameras.py#L85-L90)，`deepcopy` 自身后把所有 `torch.Tensor` 属性搬到 `data_device`。训练循环里 `viewpoint_cam.cuda()`（[train.py:136](https://github.com/yangzf-1023-4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L136)）调用的就是它；
- `MiniCam`：[scene/cameras.py:93-105](https://github.com/yangzf-1023-4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/cameras.py#L93-L105)，只含宽高/FoV/两个矩阵的轻量相机。在本仓库中检索不到任何调用点，属于 3DGS 一脉的遗留代码（识别方法同 u1-l3：用 grep 找调用）。

`Camera` 的字段最终如何被渲染器消费——[gaussian_renderer/__init__.py:36-55](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L36-L55) 组装 `GaussianRasterizationSettings` 时：`image_height/image_width` 决定渲染画布尺寸，`viewmatrix=viewpoint_camera.world_view_transform`、`projmatrix=viewpoint_camera.full_proj_transform`、`campos=viewpoint_camera.camera_center`、`timestamp=viewpoint_camera.timestamp`。也就是说，**本模块产出的每个字段都有明确的下游**。

#### 4.1.4 代码实践

> 说明：本讲规格中提到的 `original_view` 在本仓库的 `Camera` 中并不存在，对应的「视图矩阵」属性名是 `world_view_transform`，实践按真实属性进行。

**实践目标**：手工构造一个 `Camera`，打印四个关键矩阵/向量的形状与数值，并验证三条数学关系。

**操作步骤**（示例代码，保存为 `inspect_camera.py`，依赖 `torch`、`kornia`——后者是 `scene/cameras.py` 顶部 import 所需）：

```python
# 示例代码：不依赖数据集，手工构造 Camera
import numpy as np, torch, math
from scene.cameras import Camera

# 1) 造一个绕 z 轴转 30° 的 C2W 旋转（CameraInfo.R 的约定：C2W 旋转）
th = math.radians(30)
R_c2w = np.array([[math.cos(th), -math.sin(th), 0],
                  [math.sin(th),  math.cos(th), 0],
                  [0, 0, 1]], dtype=np.float32)
T_w2c = np.array([1.0, -2.0, 3.0], dtype=np.float32)   # W2C 平移

W, H = 320, 240
fx = fy = 300.0
FoVx = 2 * math.atan(W / (2 * fx))                      # focal2fov
FoVy = 2 * math.atan(H / (2 * fy))

image = torch.rand(3, H, W)                             # 假的真值图（CPU 上即可）

cam = Camera(colmap_id=0, R=R_c2w, T=T_w2c, FoVx=FoVx, FoVy=FoVy,
             image=image, gt_alpha_mask=None, image_name="cam00_0000", uid=0,
             data_device="cpu", timestamp=0.0,
             resolution=(W, H), meta_only=False)

for name in ["world_view_transform", "projection_matrix",
             "full_proj_transform", "camera_center"]:
    t = getattr(cam, name)
    print(f"{name}: shape={tuple(t.shape)}\n{t}\n")

# 验证 1：camera_center == -(R_c2w @ T)
print("camera_center 校验:", torch.allclose(
    cam.camera_center, torch.from_numpy(-(R_c2w @ T_w2c)), atol=1e-5))

# 验证 2：full_proj == world_view @ projection（都是转置存储，乘法关系不变）
print("full_proj 校验:", torch.allclose(
    cam.full_proj_transform,
    cam.world_view_transform @ cam.projection_matrix, atol=1e-5))

# 验证 3：W2C 把光心映射回原点（齐次）
c_h = torch.tensor([*cam.camera_center.tolist(), 1.0])
w2c = cam.world_view_transform.t()                      # 转回数学矩阵
print("W2C@center 校验:", w2c @ c_h)                    # 期望前 3 个分量≈0
```

**需要观察的现象**：

- 三个矩阵都是 `torch.Size([4, 4])`，`camera_center` 是 `torch.Size([3])`；
- `projection_matrix` 中 `P[0,2]=P[1,2]=0`（本例走 FoV 路径，主点居中）；
- `projection_matrix` 最后一行是 `[0, 0, 1, 0]`（即 \(w=z_{cam}\)）。

**预期结果**：三条校验全部为 `True`（第 3 条的前 3 个分量接近 0）。其中 `camera_center ≈ -(R_c2w @ T)` 直接验证了 4.1.2 的公式。

**待本地验证**：本脚本未在当前环境执行，数值结果以本地运行为准。若在没有 GPU 的机器上运行，请保持 `data_device="cpu"`（默认值 `"cuda"` 在构造阶段虽然只是 `torch.device` 对象不会立刻报错，但 `cuda()`/渲染时需要真实 GPU）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Camera` 存储矩阵前要 `.transpose(0, 1)`？如果不转置直接传给渲染器会发生什么？

**答案**：PyTorch 张量按行主序的数学矩阵使用（\(y=Mx\)），而 CUDA 光栅化器把传入的 4×4 连续内存按列主序解释，等价于使用 \(M^{\top}\)。预先转置后，CUDA 侧「按列主序读」得到的正好是数学矩阵本身。若不转置，渲染器会把 \(M^{\top}\) 当成 \(M\) 用，投影结果整体错乱（典型表现是图像镜像/旋转错误或高斯全部飞出视锥）。

**练习 2**：`camera_center = world_view_transform.inverse()[3, :3]` 中，为什么要取「第 3 行」而不是「第 0-2 列组成的向量」？

**答案**：`world_view_transform` 本身是 \(W2C^{\top}\)，它的逆是 \((W2C^{\top})^{-1} = C2W^{\top}\)。\(C2W^{\top}\) 的第 3 行前 3 列 = \(C2W\) 的第 3 列前 3 行 = C2W 的平移部分 = 相机光心。两种「转置后再取」的说法是同一件事：在数学矩阵 \(C2W\) 里它就是平移列。

**练习 3**：`MiniCam` 在仓库里没有被调用，怎么确认这一点？

**答案**：在全仓库 grep `MiniCam`，命中只有定义处 [scene/cameras.py:93](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/cameras.py#L93)（u8 的轨迹渲染实际复用完整 `Camera` 并改写其矩阵，见 `utils/render_utils.py`）。识别遗留代码的方法与 u1-l3 一致：定义存在 ≠ 被使用。

### 4.2 loadCam：分辨率缩放与内参同步

#### 4.2.1 概念说明

`loadCam` 是 `CameraInfo → Camera` 的转换器，它解决两个问题：

1. **分辨率协商**：原始图像可能很大（N3V 数据常见 2028 宽），训练不一定用全分辨率。`args.resolution` 决定下采样倍数；
2. **内参同步**：图像缩放后，主点 \((cx, cy)\) 与焦距 \((fl_x, fl_y)\) 必须**按同一比例缩放**，否则投影矩阵与图像不再对应，渲染出的图会与真值错位。

#### 4.2.2 核心流程

```
输入: cam_info(CameraInfo), args.resolution, resolution_scale
1. 若 resolution ∈ {1,2,3,4,8}:                      # 整数倍下采样
      scale = resolution_scale * args.resolution
      目标分辨率 = (orig_w/scale, orig_h/scale)
   否则:
      resolution == -1 → 宽度超过 1600 时自动降到 1600（只警告一次）
      其他值          → 视为目标宽度: global_down = orig_w / resolution
2. cx, cy, fl_x, fl_y 全部除以 scale                    # 内参同步
3. 图像:
      dataloader=False → PILtoTorch(cam_info.image, resolution)  # 立即读入并缩放
      dataloader=True  → gt_image = cam_info.image (=None)        # 不读图
4. 构造 Camera(..., meta_only=args.dataloader)
```

关键不变量：\(\dfrac{fl_x^{new}}{W^{new}} = \dfrac{fl_x^{old}}{W^{old}}\)，即**归一化焦距（视场角）在缩放前后不变**——这也解释了为什么 FoV 路径的投影矩阵天然与分辨率无关，而 CenterShift 路径必须同步缩放 `fl/cx/cy/w/h`。

#### 4.2.3 源码精读

分辨率协商的两条分支：

[utils/camera_utils.py:22-24](https://github.com/yangzf-1023-4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/camera_utils.py#L22-L24) 是整数倍分支：`args.resolution` 取 1/2/3/4/8 时，宽高除以 `resolution_scale * args.resolution`，同时 `scale` 记录总缩放倍数。`resolution_scale` 来自 `Scene` 的 `resolution_scales` 列表（默认 `[1.0]`，见 [scene/__init__.py:86-91](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/__init__.py#L86-L91)，多分辨率训练时会为每个 scale 各建一套 Camera）。

[utils/camera_utils.py:25-40](https://github.com/yangzf-1023-4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/camera_utils.py#L25-L40) 是连续值分支：`-1` 表示「自动」——原始宽度超过 1600 时按 `orig_w/1600` 缩放并用模块级全局变量 `WARNED` 保证警告只打印一次（[utils/camera_utils.py:27-32](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/camera_utils.py#L27-L32)）；其他数值被当作**目标宽度**处理。

内参同步：

[utils/camera_utils.py:42-45](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/camera_utils.py#L42-L45) 把 `cx/cy/fl_x/fl_y` 全部除以 `scale`——图像缩小几倍，主点和焦距（单位：像素）就缩小几倍。这一步是 CenterShift 投影矩阵在缩放后仍然正确的前提。

图像的两条加载路径：

[utils/camera_utils.py:47-55](https://github.com/yangzf-1023-4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/camera_utils.py#L47-L55)：`not args.dataloader` 时调用 `PILtoTorch`（[utils/general_utils.py:22-28](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/general_utils.py#L22-L28)：PIL resize → `np.array` → `/255` → HWC 转 CHW，4 通道时第 4 通道作为 alpha 掩码返回）；`args.dataloader=True` 时直接把 `cam_info.image`（Colmap 路径下恒为 `None`，见下）传下去，不发生任何磁盘读取。

最后构造 `Camera`：

[utils/camera_utils.py:62-69](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/camera_utils.py#L62-L69) 把缩放后的内参、时间戳、`resolution=(w,h)`、`image_path` 一并传入，**并且 `meta_only=args.dataloader`**——`dataloader` 开关与 `meta_only` 开关在这里划上等号：开了 DataLoader，Camera 就只存元数据。

批量转换与一个必须知道的坑：

[utils/camera_utils.py:71-77](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/camera_utils.py#L71-L77) 的 `cameraList_from_camInfos` 对每个 `CameraInfo` 调一次 `loadCam`。**注意**：Colmap 路径下 `CameraInfo.image` 恒为 `None`——模板相机在 [scene/dataset_readers.py:120-128](https://github.com/yangzf-1023-4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L120-L128) 中 `image = None`（第 121 行），帧展开时 [scene/dataset_readers.py:214-237](https://github.com/yangzf-1023-4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L214-L237) 也 `temp_image = None`（第 217 行）。因此若对 Colmap 数据设 `dataloader=False`，`PILtoTorch(None, ...)` 会在 `None.resize(...)` 处抛 `AttributeError`。**换言之：N3V/COLMAP 数据实际上必须 `dataloader: True`**（yaml 里也确实如此，见 [configs/dynerf/flame_steak.yaml:23](https://github.com/yangzf-1023-4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/configs/dynerf/flame_steak.yaml#L23)）；`dataloader=False` 的 PIL 路径只有 Blender 数据集（`readCamerasFromTransforms` 会真正读 PIL 图）才能走通。

另一个坑（承接 u1-l4 的结论）：yaml 里的 `resolution: 2`（[configs/dynerf/flame_steak.yaml:15](https://github.com/yangzf-1023-4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/configs/dynerf/flame_steak.yaml#L15)）**不会生效**——train.py 在 yaml 合并之后无条件执行 `args.resolution = args.res`（[train.py:434-455](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L434-L455)，`--res` 默认值 1 定义在 [train.py:422](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L422)）。所以 `loadCam` 里 `args.resolution in [1,2,3,4,8]` 的判断，实际命中的几乎总是命令行 `--res` 的值。想改分辨率，改 `--res` 而不是 yaml。

顺带一提：[utils/camera_utils.py:79-99](https://github.com/yangzf-1023-4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/camera_utils.py#L79-L99) 的 `camera_to_JSON` 类型注解写的是 `camera: Camera`，但它实际接收的是 `CameraInfo`（调用点 [scene/__init__.py:72-73](https://github.com/yangzf-1023-4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/__init__.py#L72-L73) 传入 `scene_info.train_cameras`），用的是 `CameraInfo` 的 `width/height` 字段——`Camera` 对象上只有 `image_width/image_height`，没有 `width`。这是又一处「注解与实际不符」的遗留代码，阅读时以调用点为准。

#### 4.2.4 代码实践

**实践目标**：用假数据直接调用 `loadCam`，观察不同 `args.resolution` 下分辨率与内参的同步缩放，并验证「主点居中时两条投影矩阵路径等价」。

**操作步骤**（示例代码，依赖 `torch`、`Pillow`、`kornia`）：

```python
# 示例代码：直接驱动 loadCam
import math, types
import numpy as np, torch
from PIL import Image
from utils.camera_utils import loadCam

W, H, fx, fy = 2028, 1352, 1500.0, 1500.0
cx, cy = W/2, H/2                       # 先造主点居中的内参

class FakeInfo: pass                    # 模拟 CameraInfo（关键字够用即可）
cam_info = FakeInfo()
cam_info.uid, cam_info.R, cam_info.T = 0, np.eye(3, dtype=np.float32), np.zeros(3, np.float32)
cam_info.FovX = 2*math.atan(W/(2*fx)); cam_info.FovY = 2*math.atan(H/(2*fy))
cam_info.image = Image.new("RGB", (W, H))     # PIL 图，走 dataloader=False 路径
cam_info.depth, cam_info.image_path = None, ""
cam_info.image_name, cam_info.timestamp = "cam00_0000", 0.0
cam_info.width, cam_info.height = W, H
cam_info.fl_x, cam_info.fl_y, cam_info.cx, cam_info.cy = fx, fy, cx, cy

for res in [1, 2, -1]:
    args = types.SimpleNamespace(resolution=res, dataloader=False, data_device="cpu")
    cam = loadCam(args, 0, cam_info, resolution_scale=1.0)
    print(f"res={res}: 分辨率={cam.image_width}x{cam.image_height}, "
          f"fl_x={cam.fl_x:.1f}, cx={cam.cx:.1f}, image={tuple(cam.image.shape)}")
```

**需要观察的现象**：

| `resolution` | 结果宽×高 | `fl_x` / `cx` |
| --- | --- | --- |
| 1 | 2028×1352 | 1500 / 1014 |
| 2 | 1014×676 | 750 / 507 |
| -1 | 1267×845（2028>1600，触发自动降采样并打印一次警告） | ≈937 / ≈633.5 |

**预期结果**：每一行都满足 `fl_x/宽度` 与 `cx/宽度` 保持不变（≈0.7396 与 0.5）；`image` 形状为 `(3, H', W')`。再做等价性验证：把上面 4.1 实践里 FoV 路径的 `projection_matrix` 与主点居中时 CenterShift 路径的结果（可临时把 `cx>0` 分支手动算一遍）对比，两者应逐元素接近。

**待本地验证**：以上表格数值由公式推算，未在本地执行，以实际输出为准（尤其 `-1` 分支的取整行为）。

#### 4.2.5 小练习与答案

**练习 1**：如果把图像下采样 2 倍，却忘了同步缩放 `fl_x`，渲染会发生什么？

**答案**：投影矩阵认为焦距是 1500 像素，但渲染画布只有 1014 宽。相当于视场角变小（画面「拉近」），高斯被投影到与真值图像不同的像素位置，loss 居高不下、画面出现整体缩放错位。这正是 `loadCam` 第 42-45 行四个内参同步除以 `scale` 的意义。

**练习 2**：`args.resolution=2028` 和 `args.resolution=1` 效果一样吗？

**答案**：一样。`1` 走整数分支 `scale=1`；`2028` 不在 `[1,2,3,4,8]` 中，走连续分支 `global_down = 2028/2028 = 1`，`scale=1.0`。两条路径殊途同归，但整数分支更直接（且避免浮点取整误差）。

**练习 3**：为什么 yaml 写 `resolution: 2` 但实际训练仍是全分辨率？

**答案**：train.py 先用 OmegaConf 把 yaml 合并进 `args`（[train.py:434-443](https://github.com/yangzf-1023-4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L434-L443)），随后 [train.py:454-455](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L454-L455) 又用 `args.res`（默认 1，`is not None` 恒真）无条件覆盖 `args.resolution`。所以生效的是 `--res`，与 u1-l4 的结论一致：本仓库生效优先级是「派生覆盖 > yaml > 命令行 > 代码默认值」。

### 4.3 CameraDataset 与懒加载

#### 4.3.1 概念说明

N3V 类数据是 **4 相机 × 300 帧 = 1200 张图**。以 2028×1352 为例，一张 float32 的 CHW 张量占 \(2028 \times 1352 \times 3 \times 4 \approx 31.4\) MiB，**1200 张 ≈ 36.8 GiB**——任何一张显卡都放不下，全读进内存也很勉强。而每次迭代实际只需要 `batch_size`（默认 4）张图。

`CameraDataset` 的解法是**懒加载**：`Scene` 构造阶段只建「元数据相机」（`meta_only=True`，不含像素），训练时 DataLoader 的 worker 进程才按需 `cv2.imread` 当前 batch 的图像，用完即释放。显存里同一时刻只存在一个 batch 的图像。

#### 4.3.2 核心流程

一次训练中图像数据的完整时间线：

```
T0  Scene.__init__
     └─ cameraList_from_camInfos → loadCam(dataloader=True)
         └─ Camera(meta_only=True, image=None)         ← 只有路径字符串和矩阵，无像素
T1  training() 开头
     └─ scene.getTrainCameras() → CameraDataset(相机列表)
     └─ DataLoader(dataset, batch_size=4, shuffle=True,
                   num_workers=12, collate_fn=lambda x: x, drop_last=True)
T2  每个 iteration
     └─ worker 进程: dataset[i] → __getitem__
         └─ meta_only=True → _load_and_process_image
             └─ cv2.imread(image_path) → resize → /255 → BGR→RGB(→alpha 合成)   ← 磁盘读取发生点
     └─ 主进程拿到 [(img_cpu, cam_cpu), ...]
     └─ gt_image.cuda(); viewpoint_cam.cuda()          ← 进显存发生点
     └─ render → loss → backward → 下一个 batch（本 batch 图像失去引用，可被回收）
```

#### 4.3.3 源码精读

Dataset 本体：

[utils/data_utils.py:6-11](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/data_utils.py#L6-L11) 构造函数只存两样东西：`Camera` 对象列表和背景色（白背景 `1,1,1` 或黑背景 `0,0,0`，供 alpha 合成用）。**没有读任何图像**。

[utils/data_utils.py:12-19](https://github.com/yangzf-1023-4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/data_utils.py#L12-L19) 的 `__getitem__` 是懒加载的心脏：`meta_only=True` 走 `_load_and_process_image`（此刻才碰磁盘）；否则直接返回早就读好的 `viewpoint_cam.image`（对应 `dataloader=False` 的 eager 路径）。返回值是 `(图像张量, Camera 对象)` 二元组——Camera 本身也随样本一起返回，渲染所需的矩阵都在它身上。

[utils/data_utils.py:21-38](https://github.com/yangzf-1023-4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/data_utils.py#L21-L38) 的图像加工流水线：

```python
img = cv2.imread(viewpoint_cam.image_path, cv2.IMREAD_UNCHANGED)   # BGR/BGRA
img = cv2.resize(img, (target_w, target_h), interpolation=cv2.INTER_LINEAR)
img = img.astype(np.float32) / 255.0
if img.shape[2] == 4:
    rgb = img[:, :, 2::-1]                      # BGR -> RGB
    alpha = img[:, :, 3:4]
    blended = rgb * alpha + self.bg * (1.0 - alpha)   # alpha 合成到背景色
else:
    blended = img[:, :, 2::-1]
viewpoint_image = torch.from_numpy(blended.copy()).permute(2, 0, 1).contiguous().clamp(0.0, 1.0)
```

四个细节：

- `target_w, target_h = viewpoint_cam.resolution`（第 24 行）——`cv2.resize` 的尺寸参数顺序是 (宽, 高)，与 `Camera.resolution` 的 (w, h) 约定一致，缩放目标与 4.2 中 loadCam 算出的分辨率完全对应；
- **alpha 处理与 eager 路径不同**：这里把带 alpha 的图**合成到背景色**（`rgb*α + bg*(1-α)`）；而 `dataloader=False` 路径把 alpha 作为 `gt_alpha_mask` 交给 `Camera.__init__` 做 `image *= mask`（背景直接变黑，不与白色混合）。两条路径对透明像素的处理并不等价，是一个隐藏的行为差异；
- `permute(2,0,1)` 把 HWC 转 CHW，`clamp(0,1)` 兜底数值范围；
- `.copy()` 是因为 `img[:, :, 2::-1]` 是负步长视图，`torch.from_numpy` 要求连续内存。

[utils/data_utils.py:40-41](https://github.com/yangzf-1023-4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/data_utils.py#L40-L41) 的 `__len__` 返回相机总数，DataLoader 据此划分 epoch。

Dataset 的三个出口（`Scene` 侧）：

[scene/__init__.py:114-127](https://github.com/yangzf-1023-4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/__init__.py#L114-L127)：`getTrainCameras` / `getTestCameras` / `getValidationCameras` / `getAllCameras` 都返回 `CameraDataset`，只是包的列表不同（训练集、测试集、按步长抽样的子集、全集——`getAllCameras` 供 u7 的轨迹渲染使用）。注意传给 `CameraDataset` 的是列表的 `.copy()`，避免 Dataset 与 Scene 共享可变列表。

DataLoader 的组装（训练侧）：

[train.py:99-105](https://github.com/yangzf-1023-4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L99-L105)：

```python
training_dataset = scene.getTrainCameras()
training_dataloader = DataLoader(training_dataset, batch_size=batch_size, shuffle=True,
                                 num_workers=12 if dataset.dataloader else 0,
                                 collate_fn=lambda x: x, drop_last=True)
```

- `num_workers=12`：12 个 worker 进程并行做 imread+resize，与 GPU 计算重叠，磁盘 I/O 不拖慢训练；`dataloader=False` 时退化为单进程（数据已在内存，无需多进程）；
- `collate_fn=lambda x: x`：**禁用默认的「堆叠成一个大张量」行为**。因为样本里混着 `Camera` 对象（不可堆叠），batch 保持为「长度 = batch_size 的 `[(img, cam), ...]` 列表」。在 Linux 默认的 fork 启动方式下，worker 通过进程内存继承拿到这个 lambda，无需序列化，因此这里用 lambda 不会报 pickle 错误；
- `drop_last=True`：丢弃末尾不满 `batch_size` 的零头 batch，保证每次迭代视角数恒定（这对 u5-l3 的多视角梯度归一化很重要）。

消费点：

[train.py:112-136](https://github.com/yangzf-1023-4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L112-L136)：`for batch_data in training_dataloader` 逐 batch 取数据；`gt_image, viewpoint_cam = batch_data[batch_idx]` 解包；**第 135-136 行 `gt_image.cuda()` 与 `viewpoint_cam.cuda()` 才把数据搬进显存**（后者即 4.1 读过的 `Camera.cuda()`：deepcopy 后搬所有张量属性）。随后 `render(viewpoint_cam, ...)` 使用它的矩阵与 timestamp。

`dataloader` 开关的默认值定义在 [arguments/__init__.py:61](https://github.com/yangzf-1023-4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/arguments/__init__.py#L61)（默认 `False`），各数据集 yaml 中显式设为 `True`（如 [configs/dynerf/flame_steak.yaml:23](https://github.com/yangzf-1023-4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/configs/dynerf/flame_steak.yaml#L23)）。Blender 路径也会把它一路透传到 `readNerfSyntheticInfo`（[scene/dataset_readers.py:452-457](https://github.com/yangzf-1023-4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L452-L457)），决定读不读 PIL 图。

#### 4.3.4 代码实践

**实践目标**：用小尺寸假图对比 `dataloader=True/False` 两种模式的内存占用，验证「懒加载时 Dataset 构造阶段零像素开销」。

**操作步骤**（示例代码，依赖 `torch`、`opencv-python`、`kornia`；建议在真实数据上用 `torch.cuda.max_memory_allocated` 复测）：

```python
# 示例代码：懒加载 vs 立即加载的内存对比
import os, tempfile, types
import numpy as np, torch, cv2
from utils.camera_utils import loadCam
from utils.data_utils import CameraDataset

# 1) 造 N 张小图（真实场景请代入你的 W/H/N）
N, W, H = 40, 640, 480
tmp = tempfile.mkdtemp()
paths = []
for i in range(N):
    p = os.path.join(tmp, f"cam00_{i:04d}.png")
    cv2.imwrite(p, np.random.randint(0, 255, (H, W, 3), np.uint8))
    paths.append(p)

def build(args, path):
    ci = types.SimpleNamespace(uid=0, R=np.eye(3, dtype=np.float32),
        T=np.zeros(3, np.float32), FovX=1.0, FovY=1.0, image=None, depth=None,
        image_path=path, image_name="cam00", timestamp=0.0, width=W, height=H,
        fl_x=-1, fl_y=-1, cx=-1, cy=-1)
    return loadCam(args, 0, ci, 1.0)     # loadCam 内部按 resolution 求分辨率

# 2) 懒加载：Camera 不含像素，__getitem__ 才读图
args_lazy = types.SimpleNamespace(resolution=1, dataloader=True, data_device="cpu")
cams_lazy = [build(args_lazy, p) for p in paths]
print("懒加载 Camera.image:", cams_lazy[0].image, "| meta_only:", cams_lazy[0].meta_only)
ds_lazy = CameraDataset(cams_lazy, white_background=False)
import tracemalloc; tracemalloc.start()           # 观察 CPU 峰值
imgs = [ds_lazy[i][0] for i in range(N)]
cur, peak_lazy = tracemalloc.get_traced_memory(); tracemalloc.stop()
print(f"懒加载取完 {N} 张后峰值: {peak_lazy/1e6:.1f} MB, 单张形状 {imgs[0].shape}")

# 3) eager：把所有像素显式驻留（模拟 dataloader=False 的效果）
resident = [torch.from_numpy(
    cv2.imread(p).astype(np.float32)/255).permute(2,0,1) for p in paths]
print(f"常驻内存: {sum(t.numel()*t.element_size() for t in resident)/1e6:.1f} MB")
```

**需要观察的现象**：

- 懒加载模式下 `Camera.image` 是 `None`、`meta_only=True`，构造 40 个 Camera 几乎不占内存；
- `ds_lazy[i]` 返回的图像形状是 `(3, H, W)`、数值在 \([0,1]\)；
- 常驻所有像素的内存 ≈ `N×H×W×3×4` 字节（40 张 640×480 ≈ 141 MB），而懒加载的峰值只略高于单个 batch 的实际需求量级。

**预期结果**：把 N 换算成真实规模即可看出差距——N3V 的 1200 张 2028×1352 若全部常驻约 36.8 GiB；懒加载下同一时刻只有 `batch_size=4`（约 126 MiB）加上 12 个 worker 的预取缓冲在内存中。显存侧可用 `torch.cuda.max_memory_allocated()` 在两种模式各跑 100 个 iteration 对比（`dataloader=False` 时显存还要叠加所有 GT 图像）。

**待本地验证**：以上内存数字为按公式估算，具体数值请以本地 `tracemalloc` / `nvidia-smi` 实测为准；对 Colmap 数据设 `dataloader=False` 会因 `cam_info.image=None` 直接报错（见 4.2.3），所以显存对比请在 Blender 数据或上述模拟脚本上做。

#### 4.3.5 小练习与答案

**练习 1**：`CameraDataset.__getitem__` 返回的是 `(图像, Camera)` 二元组而不是只返回图像，为什么 Camera 必须跟着样本走？

**答案**：渲染一个视角需要该视角的 `world_view_transform / full_proj_transform / camera_center / image_width/height / timestamp`（见 [gaussian_renderer/__init__.py:36-55](https://github.com/yangzf-1023-4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L36-L55)），这些都在 Camera 上。 shuffle 打乱的是「相机×帧」组合，每个样本对应不同的位姿与时刻，图像离开 Camera 就无法渲染，也无法计算 loss。

**练习 2**：`collate_fn=lambda x: x` 去掉了什么？如果删掉这个参数会发生什么？

**答案**：默认 collate 会把 batch 内各样本的同名字段堆叠成统一形状的张量。这里样本含 Camera 对象（一堆形状各异的矩阵），无法堆叠。删掉后 DataLoader 会在尝试 `torch.stack` 时抛异常。`lambda x: x` 让 batch 保持原样的列表，训练循环再逐个解包（[train.py:133-134](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L133-L134)）。

**练习 3**：`drop_last=True` 在多视角 batch 训练里为什么重要？

**答案**：它保证每个 batch 恰好 `batch_size` 个视角。u5-l3 会讲到：batch 内各视角的 viewspace 梯度与 `_t` 梯度要按「每个高斯被多少视角看见」归一化（乘 `batch_size/visibility_count`），最后一个残缺 batch 会破坏这一归一化的前提，也让致密化统计在不同 iteration 之间不可比。

## 5. 综合实践

**任务：写一个「迷你数据链路」脚本，把本讲三个模块串起来。**

要求脚本依次完成：

1. **造数据**：在临时目录写 8 张 `cam00_0000.png` ~ `cam00_0007.png` 小图（如 320×240），并准备对应的 `CameraInfo`（可以用 `types.SimpleNamespace` 模拟，字段以 [scene/dataset_readers.py:42-58](https://github.com/yangzf-1023-4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L42-L58) 为准），`timestamp` 按 u2-l2 的公式 \(t = \dfrac{10f}{F_{max}+1}\) 计算（8 帧应得到 0, 1.25, 2.5, ..., 8.75）；
2. **过 loadCam**：`dataloader=True`，`resolution=2`，检查每个 `Camera` 的 `image_width/fl_x/timestamp` 是否都按预期缩放/归一化；
3. **过 CameraDataset**：包成 `CameraDataset`，用 `torch.utils.data.DataLoader(batch_size=2, shuffle=False, collate_fn=lambda x: x)` 取 4 个 batch，检查每张图形状是否为 `(3, 120, 160)`；
4. **验证渲染输入**：从每个样本取 `viewpoint_cam.full_proj_transform`，断言它等于 `world_view_transform @ projection_matrix`；再断言 `camera_center ≈ -(R @ T)`；
5. **回答两个问题**（写在脚本输出末尾）：
   - 如果把第 2 步的 `resolution` 改成 `-1`，320 宽的图会被缩放吗？（提示：看 [utils/camera_utils.py:26-35](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/camera_utils.py#L26-L35) 的 1600 阈值）
   - 这条链路里，磁盘读取、进显存分别发生在哪一行代码？

**预期结果**：所有断言通过；第 5 问答案——`resolution=-1` 时 320 < 1600，`global_down=1`，不缩放；磁盘读取发生在 [utils/data_utils.py:22](https://github.com/yangzf-1023-4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/data_utils.py#L22) 的 `cv2.imread`，进显存发生在 [train.py:135-136](https://github.com/yangzf-1023-4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L135-L136) 的 `.cuda()`。

**待本地验证**：本综合实践未在当前环境运行，输出以本地执行为准。

## 6. 本讲小结

- `Camera` 是渲染单元：`world_view_transform`（W2C，转置存储）、`projection_matrix`（FoV 或 CenterShift 两条构造路径）、`full_proj_transform`（两者之积）、`camera_center`（\(=-R_{c2w}T\)），外加 `timestamp` 与图像/分辨率信息；全部字段都被 `render()` 的 `GaussianRasterizationSettings` 消费。
- `loadCam` 完成 `CameraInfo → Camera` 的转换：`resolution` 决定宽高缩放，**内参 `cx/cy/fl_x/fl_y` 必须同比例除以 scale**；`meta_only=args.dataloader` 把「是否懒加载」写进 Camera 本身。
- 懒加载的时间线：Scene 构造阶段只有元数据（Colmap 路径 `CameraInfo.image` 恒为 `None`）→ DataLoader worker 在 `__getitem__` 里 `cv2.imread` → `train.py` 的 `.cuda()` 才进显存；`num_workers=12` + `collate_fn=lambda x: x` + `drop_last=True` 是训练循环的三个关键配置。
- 规模账：4 相机 × 300 帧 × 31.4 MiB ≈ 36.8 GiB，全量常驻不可行，懒加载让显存只需容纳一个 batch（≈126 MiB）。
- 两个实践性结论：对 COLMAP/N3V 数据 `dataloader` 实际必须为 `True`（否则 `PILtoTorch(None)` 报错）；yaml 的 `resolution` 会被 `--res`（默认 1）无条件覆盖，改分辨率要改 `--res`。
- 识别遗留代码三例：未被调用的 `MiniCam`、注解与实参不符的 `camera_to_JSON`、`cameras.py` 顶部无用的 `from matplotlib import scale`。

## 7. 下一步学习建议

下一讲 **u2-l4 Scene 类：数据集分发与场景装配** 将把本讲的 `cameraList_from_camInfos` 放回 `Scene.__init__` 的完整语境：`sceneLoadTypeCallbacks` 如何在 Colmap/Blender 之间分发、`getNerfppNorm` 算出的 `cameras_extent` 有什么用、`input.ply` 与 `cameras.json` 何时写出。建议先自行阅读 [scene/__init__.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/__init__.py) 的 `__init__` 前半段，并带着一个问题读：为什么 `camera_to_JSON` 要在 `loaded_iter` 为空时才执行？此外，本讲的 `resolution_scales` 多分辨率机制与 `getValidationCameras` 的抽样逻辑，将在 u5（训练日志与评估）和 u7（轨迹渲染 `getAllCameras`）中再次出现，可以提前留意。
