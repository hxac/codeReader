# u2-l1 COLMAP 数据格式与 colmap_loader

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `sparse/0/` 目录下 `cameras.bin`、`images.bin`、`points3D.bin` 三个文件分别存放什么内容，以及它们各自的二进制与文本格式。
2. 理解 COLMAP 用四元数（qvec）+ 平移向量（tvec）表示相机外参的约定，并能推导 `qvec2rotmat` 的旋转矩阵公式。
3. 会独立调用 `scene/colmap_loader.py` 中的 `read_extrinsics_binary` / `read_intrinsics_binary`（以及对应的 text 版本），把一个 COLMAP 重建结果读成 Python 对象。
4. 理解解析结果如何流向 `CameraInfo`（R/T 约定、焦距到视场角的换算），为下一讲 `readColmapSceneInfo` 打基础。

本讲是单元 2「数据加载与场景构建」的第一讲。4C4D 的训练入口并不直接读图片，而是先通过 `scene/colmap_loader.py` 把 COLMAP 格式的相机参数与点云解析出来——这是整条数据链路的起点。

## 2. 前置知识

本讲需要的背景概念，用最通俗的方式解释：

- **SfM（Structure from Motion，运动恢复结构）**：给一组同一场景的多张照片，同时恢复出每张照片拍摄时的相机位姿（在哪里、朝哪个方向）和场景的三维点云。COLMAP 就是最流行的开源 SfM 工具。
- **COLMAP**：一个 SfM/MVS 重建软件。它把重建结果写成固定格式的文件，本仓库直接消费这种格式，所以即使你没用过 COLMAP 软件，也要读懂它的**文件格式**。
- **相机内参（intrinsics）**：描述相机自身成像属性的参数——焦距（fx, fy）和光心（cx, cy）。它决定「三维点如何投影到照片上的像素坐标」。存放在 `cameras.bin`。
- **相机外参（extrinsics）**：描述相机在世界坐标系中位姿的参数——旋转 R 和平移 t。它决定「相机的朝向和位置」。COLMAP 用**单位四元数 qvec + 平移向量 tvec** 存储，存放在 `images.bin`。
- **四元数（quaternion）**：用 4 个数 \((w, x, y, z)\) 表示三维旋转的方式。相比 3×3 旋转矩阵（9 个数），它更紧凑、无冗余、便于插值。COLMAP 采用 **w 在前** 的约定：`qvec = (w, x, y, z)`。
- **世界坐标 / 相机坐标**：场景点的三维坐标在世界系下定义；相机成像时要把世界系坐标变换到相机系（镜头正前方为 z 轴的坐标系），这个变换就是「世界到相机（World-to-Camera, W2C）」变换 \( X_{cam} = R X_{world} + t \)。
- **struct 与小端序**：Python 的 `struct` 模块按格式字符（如 `i`=4 字节 int32、`d`=8 字节 float64、`Q`=8 字节 uint64、`B`=1 字节 uint8）把二进制字节拆成数值。COLMAP 二进制文件统一采用**小端序**（代码里 `endian_character="<"`）。

一个容易混淆的点先说明：`read_intrinsics_*` 读的是 `cameras.*`（内参），`read_extrinsics_*` 读的是 `images.*`（外参）——**函数名按「内容」命名，文件名按「主体」命名**，初学时经常对不上号。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|---|---|---|
| [scene/colmap_loader.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/colmap_loader.py) | COLMAP 格式解析器（继承自 3DGS/Inria 实现） | namedtuple 容器、相机模型表、`qvec2rotmat`、6 个 `read_*` 函数 |
| [utils/graphics_utils.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/graphics_utils.py) | 几何工具函数 | `focal2fov`/`fov2focal`、`getWorld2View2`、`BasicPointCloud` |
| [scene/dataset_readers.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py) | 数据集读取的上一层（下一讲主角） | `CameraInfo` 定义、`readColmapCameras` 如何消费本讲的解析结果 |
| [README.md](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/README.md) | 数据目录结构说明 | `sparse/0/` 三件套的文档描述 |

注意：`colmap_loader.py` 只依赖 `numpy / collections / struct`（见 [scene/colmap_loader.py:12-14](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/colmap_loader.py#L12-L14)），**不需要 GPU 和 CUDA 扩展就能单独使用**——这让本讲的所有实践都可以在纯 CPU 环境完成。

## 4. 核心概念与源码讲解

### 4.1 模块一：`sparse/0/` 三件套与 namedtuple 数据容器

#### 4.1.1 概念说明

4C4D 要求数据集按如下结构组织（README 中有完整说明，见 [README.md:64-90](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/README.md#L64-L90)）：

```
data/N3V/flame_steak/
├── images/                  # 视频帧，命名 cam{XX}_{YYYY}.png
│   ├── cam00_0000.png       #   XX=相机序号, YYYY=帧号
│   └── ...
└── sparse/0/                # COLMAP 重建结果
    ├── cameras.bin          # 相机内参（一台物理相机一条记录）
    ├── images.bin           # 相机外参（一帧位姿一条记录）
    └── points3D.bin         # 三维点云
```

三个文件各自存什么：

| 文件 | 存什么 | 一条记录代表 | 4C4D 中的用途 |
|---|---|---|---|
| `cameras.*` | 内参：相机模型名、宽高、参数（焦距/光心/畸变） | 一台物理相机 | 计算视场角 FovX/FovY、投影矩阵 |
| `images.*` | 外参：qvec、tvec、所属相机 id、图片名、2D 特征点 | 一台相机在某一帧的位姿 | 每帧的渲染视角；`camXX_YYYY` 命名由此提取 |
| `points3D.*` | 三维点：坐标、颜色、重投影误差、track | 一个三角化的三维点 | 4D 高斯的初始点云（`create_from_pcd` 的输入） |

README 特别说明（[README.md:90](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/README.md#L90)）：`.bin` 与 `.txt` 两种格式都被支持；同时（[README.md:92-94](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/README.md#L92-L94)）`points3D.*` 只从训练视角重建（它充当训练初始化点云），而 `images.*`/`cameras.*` 若要在留出视角上评估，就需要包含全部视角的位姿——这解释了为什么「相机会有 20+ 台，但点云只用其中 4 台重建」。

#### 4.1.2 核心流程

`colmap_loader.py` 的读取流程是纯函数式的「文件 → 字典」映射：

```
cameras.bin  --read_intrinsics_binary-->  {camera_id: Camera}
images.bin   --read_extrinsics_binary-->  {image_id:  Image}
points3D.bin --read_points3D_binary--->   (xyzs[N,3], rgbs[N,3], errors[N,1])
      │（.bin 不存在时自动回退到 .txt 版本，见 dataset_readers.py 的 try/except）
      ▼
Camera / Image / Point3D 均为 collections.namedtuple，字段不可变、可按名访问
```

namedtuple 是「轻量级只读结构体」：`Camera(id=1, model="PINHOLE", ...)` 既可以用 `cam.params` 按名取字段，又不像 `nn.Module` 那样有任何框架开销——解析层用这种纯数据容器是最合适的选择。

#### 4.1.3 源码精读

四个基础容器在文件头部定义（[scene/colmap_loader.py:16-23](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/colmap_loader.py#L16-L23)）：`CameraModel`、`Camera`、`BaseImage`、`Point3D`，字段与上表一一对应。随后是一张相机模型注册表（[scene/colmap_loader.py:24-40](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/colmap_loader.py#L24-L40)）：

```python
CAMERA_MODELS = {
    CameraModel(model_id=0, model_name="SIMPLE_PINHOLE", num_params=3),
    CameraModel(model_id=1, model_name="PINHOLE", num_params=4),
    CameraModel(model_id=2, model_name="SIMPLE_RADIAL", num_params=4),
    ...
}
CAMERA_MODEL_IDS = dict([(camera_model.model_id, camera_model) for ...])
CAMERA_MODEL_NAMES = dict([(camera_model.model_name, camera_model) for ...])
```

这张表的作用是**把二进制里的整数 model_id 翻译成模型名，并告诉解析器该模型有几个参数**（例如 `PINHOLE` 有 4 个参数：fx, fy, cx, cy）。它是解析 `cameras.bin` 变长参数段的关键——二进制文件里并不存储参数个数，必须查表得知。

`Image` 在 `BaseImage` 基础上补了一个实例方法（[scene/colmap_loader.py:68-70](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/colmap_loader.py#L68-L70)）：

```python
class Image(BaseImage):
    def qvec2rotmat(self):
        return qvec2rotmat(self.qvec)
```

即每个 `Image` 对象可以自己把四元数转成旋转矩阵，不必手动调用模块级函数。

#### 4.1.4 代码实践

**实践目标**：熟悉 namedtuple 容器与相机模型表，不读任何数据文件。

1. 在仓库根目录启动 `python`（只需 numpy 可用）；
2. 用 `importlib` 直接按文件路径加载 `colmap_loader.py`（避免触发 `scene/__init__.py` 里的 torch/CUDA 导入）：

```python
# 示例代码（可直接粘贴到 python 交互环境）
import importlib.util
spec = importlib.util.spec_from_file_location(
    "colmap_loader", "scene/colmap_loader.py")
cl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cl)

print(cl.CAMERA_MODELS[:2])          # 查看前两个相机模型
print(cl.Camera._fields)             # ('id','model','width','height','params')
print(cl.BaseImage._fields)          # ('id','qvec','tvec','camera_id','name','xys','point3D_ids')
print(cl.CAMERA_MODEL_IDS[1])        # PINHOLE，4 参数
```

3. **需要观察的现象**：`Camera`/`Image` 的字段名打印结果；`PINHOLE` 的 `num_params`。
4. **预期结果**：`CAMERA_MODEL_IDS[1]` 输出 `CameraModel(model_id=1, model_name='PINHOLE', num_params=4)`；`Camera._fields` 与 4.1.3 节字段一致。（待本地验证：输出格式以本地 Python 版本为准。）

#### 4.1.5 小练习与答案

**练习 1**：`SIMPLE_PINHOLE` 和 `PINHOLE` 的参数分别是什么？为什么个数差 1？
**答案**：`SIMPLE_PINHOLE` 3 个参数 (f, cx, cy)，假设 x/y 方向焦距相同；`PINHOLE` 4 个参数 (fx, fy, cx, cy)，允许两个方向焦距不同。前者是后者的特例。

**练习 2**：`read_intrinsics_text` 里有一句 `assert model == "PINHOLE"`（[scene/colmap_loader.py:159](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/colmap_loader.py#L159)），但二进制版 `read_intrinsics_binary` 却支持所有 11 种模型，矛盾吗？
**答案**：不矛盾。二进制解析层「能读出」所有模型，但下游 `readColmapCameras` 只处理 `SIMPLE_PINHOLE`/`PINHOLE`，其他模型会抛 `ValueError`（见 [scene/dataset_readers.py:112-116](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L112-L116)，错误信息写明 "Only undistorted datasets ... supported"）。text 版的 assert 只是把这个约束提前到了读取层。含义是：**输入数据必须先去过畸变**（例如用 COLMAP 的 `image_undistorter`），畸变模型（OPENCV、RADIAL 等）不被 4C4D 支持。

**练习 3**：为什么 `images.bin` 里一台相机的每一帧都是一条独立记录，而 `cameras.bin` 里一台物理相机只有一条记录？
**答案**：内参在拍摄过程中不变，是相机的固有属性；外参（位姿）随时间变化，动态场景每一帧的位姿都可能不同（手持或未完全同步的机位）。所以「相机 × 帧」的组合数等于 `images` 记录数。在 4C4D 中，一台相机 300 帧就是 300 条 image 记录，它们通过 `camera_id` 字段共享同一条内参记录。

### 4.2 模块二：二进制解析——`read_next_bytes` 与三个 `read_*` 函数

#### 4.2.1 概念说明

`.bin` 文件是紧凑的二进制流：没有字段名、没有分隔符，只有「按约定顺序排好的字节」。解析它的唯一依据是 COLMAP 的官方布局（代码注释指向 COLMAP 源码 `src/base/reconstruction.cc`，见 [scene/colmap_loader.py:113-118](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/colmap_loader.py#L113-L118)）。理解这一节的方式不是背代码，而是背**字节布局表**——代码只是把表格翻译成 `struct.unpack`。

#### 4.2.2 核心流程

三个二进制文件的布局（小端序；`i`=int32(4B)，`d`=float64(8B)，`Q`=uint64(8B)，`B`=uint8(1B)，`q`=int64(8B)）：

**cameras.bin**：

| 字段 | 格式 | 字节数 |
|---|---|---|
| 相机数量 num_cameras | `Q` | 8 |
| 每台相机：camera_id / model_id / width / height | `iiQQ` | 4+4+8+8=24 |
| 每台相机：params | `d`×num_params | 8×num_params（查 4.1 的模型表） |

**images.bin**：

| 字段 | 格式 | 字节数 |
|---|---|---|
| 位姿数量 num_reg_images | `Q` | 8 |
| 每条：image_id / qvec(w,x,y,z) / tvec / camera_id | `idddddddi` | 4+32+24+4=64 |
| 每条：图片名 | 变长字符 + `\x00` 结尾 | 变长 |
| 每条：num_points2D | `Q` | 8 |
| 每条：2D 点 (x, y, point3D_id) ×num_points2D | `ddq` | 24/点 |

**points3D.bin**：

| 字段 | 格式 | 字节数 |
|---|---|---|
| 点数量 num_points | `Q` | 8 |
| 每点：point3D_id / xyz / rgb / error | `QdddBBBd` | 8+24+3+8=43 |
| 每点：track_length | `Q` | 8 |
| 每点：track (image_id, point2D_idx) ×track_length | `ii` | 8/项 |

解析伪代码：

```
打开文件 → 读 8 字节得到记录数 N
循环 N 次:
    按 24/64/43 字节的固定头解包
    （cameras: 查表得 num_params 再读 8×num_params 字节参数）
    （images:  逐字节读到 \x00 得到文件名，再读 2D 点）
    （points3D: 再读 track，但 track 被丢弃不用）
组装 namedtuple / 预分配的 numpy 数组返回
```

#### 4.2.3 源码精读

通用字节读取函数（[scene/colmap_loader.py:72-81](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/colmap_loader.py#L72-L81)）：`read_next_bytes` 从文件句柄读 `num_bytes` 字节并按格式串解包，`endian_character="<"` 固定小端序。所有 `read_*_binary` 都建立在这个函数上。

**内参解析**（[scene/colmap_loader.py:203-229](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/colmap_loader.py#L203-L229)）：先读数量，再逐台用 `format_char_sequence="iiQQ"` 读 24 字节固定头，随后**查 `CAMERA_MODEL_IDS` 得到 `num_params`**、读 `8*num_params` 字节的 double 数组作为 `params`。关键三行：

```python
num_params = CAMERA_MODEL_IDS[model_id].num_params
params = read_next_bytes(fid, num_bytes=8*num_params, format_char_sequence="d"*num_params)
cameras[camera_id] = Camera(id=camera_id, model=model_name, ...)
```

**外参解析**（[scene/colmap_loader.py:168-200](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/colmap_loader.py#L168-L200)）：

```python
binary_image_properties = read_next_bytes(fid, num_bytes=64, format_char_sequence="idddddddi")
qvec = np.array(binary_image_properties[1:5])   # (w, x, y, z)
tvec = np.array(binary_image_properties[5:8])
camera_id = binary_image_properties[8]
current_char = read_next_bytes(fid, 1, "c")[0]
while current_char != b"\x00":                  # 逐字节读图片名直到 ASCII 0
    image_name += current_char.decode("utf-8")
    ...
x_y_id_s = read_next_bytes(fid, num_bytes=24*num_points2D, format_char_sequence="ddq"*num_points2D)
```

注意两点：图片名是**变长字段**，所以只能逐字节读到 `\x00` 终止符；2D 特征点（`xys`、`point3D_ids`）虽然被完整读出，但 4C4D 下游只用 `qvec/tvec/camera_id/name`，`xys` 与 `point3D_ids` 实际被闲置。

**点云解析**（[scene/colmap_loader.py:113-142](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/colmap_loader.py#L113-L142)）：每点先读 43 字节固定头（`QdddBBBd`），再读 track 并**直接丢弃**——4C4D 只需要 `xyz/rgb/error`。返回值是三个预分配的 numpy 数组 `xyzs[N,3] / rgbs[N,3] / errors[N,1]`，比 text 版逐行 `np.append` 的写法高效得多。

对应的 text 版本：`read_intrinsics_text`（[scene/colmap_loader.py:144-166](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/colmap_loader.py#L144-L166)）、`read_extrinsics_text`（[scene/colmap_loader.py:232-258](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/colmap_loader.py#L232-L258)）、`read_points3D_text`（[scene/colmap_loader.py:83-111](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/colmap_loader.py#L83-L111)）。它们按「每行一条记录、`#` 开头为注释、空白分隔」解析，返回结构与二进制版完全一致——这正是 4C4D 能在 `readColmapSceneInfo` 里 try bin / except txt 自由回退的前提（[scene/dataset_readers.py:259-268](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L259-L268)）。

#### 4.2.4 代码实践

**实践目标**：亲眼确认二进制布局表，建立「文件开头 8 字节 = 记录数量」的直觉。

1. 取任意一个 COLMAP 数据集的 `sparse/0/cameras.bin`（预处理的 N3V 数据，或自己跑 COLMAP 的输出）；
2. 用十六进制工具查看前 32 字节：`od -A d -t x1 sparse/0/cameras.bin | head -3`；
3. 再用 Python 只读头部：

```python
# 示例代码
import struct
with open("sparse/0/cameras.bin", "rb") as f:
    num_cameras = struct.unpack("<Q", f.read(8))[0]
    head = struct.unpack("<iiQQ", f.read(24))
print("num_cameras =", num_cameras)
print("camera_id, model_id, width, height =", head)
```

4. **需要观察的现象**：开头 8 字节按小端解释出的整数；紧随其后的 4 字节 camera_id 与 4 字节 model_id。
5. **预期结果**：例如 21 台相机时 `num_cameras = 21`；`model_id` 为 0 或 1（SIMPLE_PINHOLE/PINHOLE）；随后 width/height 与图片分辨率一致。若 `model_id=1`（PINHOLE），则接下来应有 4×8=32 字节的参数。若你手头没有 `.bin` 数据，请直接做第 5 节综合实践（构造 txt 版），并把本实践的观察点挪到那里。（待本地验证。）

#### 4.2.5 小练习与答案

**练习 1**：为什么 `read_points3D_binary` 里读 track 用 `format_char_sequence="ii"*track_length`，而 `read_next_bytes` 一次传入 `num_bytes=8*track_length`？
**答案**：track 的每一项是两个 int32（`ii`，4+4=8 字节），所以 track_length 项共 `8*track_length` 字节、格式串重复 track_length 次。因为 track 长度逐点变化，格式串必须动态拼接——这也是二进制解析中「变长段」的标准处理方式。

**练习 2**：`images.bin` 中图片名为什么要用 `\x00` 终止符而不是固定长度？
**答案**：文件名长度不一（`cam00_0000.png`、`IMG_1234.jpg`……），定长会浪费空间且限制长度。C 风格空终止字符串让解析器逐字节读到 `\x00` 即可，代价是必须逐字节读取、不能用一次 `unpack` 完成。

**练习 3**：若要用代码手工生成一个能被 `read_intrinsics_binary` 读回的 `cameras.bin`，至少要写哪些内容？
**答案**：`struct.pack("<Q", 1)` 写 1 台相机；`struct.pack("<iiQQ", 1, 1, 1920, 1080)` 写 camera_id=1、model_id=1（PINHOLE）、宽高；再 `struct.pack("<" + "d"*4, 1000.0, 1000.0, 960.0, 540.0)` 写 4 个参数。写入的顺序与 4.2.2 表格一致即可。此即「序列化/反序列化往返（round-trip）」练习，综合实践给出完整脚本。

### 4.3 模块三：四元数到旋转矩阵——`qvec2rotmat` 与 R/T 约定

#### 4.3.1 概念说明

COLMAP 不直接存 3×3 矩阵，而是存单位四元数 \(q=(w,x,y,z)\)（满足 \(w^2+x^2+y^2+z^2=1\)）加平移 \(t\)，含义是**世界到相机**的变换：

\[ X_{cam} = R(q)\, X_{world} + t \]

用四元数的好处：紧凑（4 个数）、天然无冗余（矩阵 9 个数还要满足正交约束）、数值稳定。`qvec2rotmat` 负责把四元数还原成旋转矩阵。

而 4C4D（继承 3DGS）在 `CameraInfo` 里存的是**转置后的** R 与原样的 T——约定不同，这正是初学者最容易踩的坑，本节把两套约定彻底讲清。

#### 4.3.2 核心流程

`qvec2rotmat` 实现的标准四元数→矩阵公式（\(q=(w,x,y,z)\)）：

\[
R(q) = \begin{pmatrix}
1-2(y^2+z^2) & 2(xy-wz) & 2(xz+wy) \\
2(xy+wz) & 1-2(x^2+z^2) & 2(yz-wx) \\
2(xz-wy) & 2(yz+wx) & 1-2(x^2+y^2)
\end{pmatrix}
\]

两套 R/T 约定的对照（关键！）：

| 约定 | 旋转 | 平移 | 变换方向 |
|---|---|---|---|
| COLMAP `Image` | `qvec2rotmat(qvec)` = \(R_{w2c}\) | `tvec` = \(t\) | \(X_{cam} = R_{w2c} X_{world} + t\) |
| 4C4D `CameraInfo` | \(R = R_{w2c}^{\top}\) | \(T = t\) | 存的是 C2W 的旋转、W2C 的平移（混合！） |

`CameraInfo.R` 的转置会在渲染前被 `getWorld2View2` 再转回去（[utils/graphics_utils.py:39-50](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/graphics_utils.py#L39-L50)）：`Rt[:3,:3] = R.transpose()`、`Rt[:3,3] = t`，拼回标准 W2C 矩阵 \(\begin{pmatrix} R_{w2c} & t \\ 0 & 1 \end{pmatrix}\)；相机中心则由 \( C = -R_{w2c}^{\top} t \) 给出（对 W2C 求逆取平移列）。

#### 4.3.3 源码精读

`qvec2rotmat`（[scene/colmap_loader.py:43-53](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/colmap_loader.py#L43-L53)）逐元素写出了上面的公式：

```python
def qvec2rotmat(qvec):
    return np.array([
        [1 - 2 * qvec[2]**2 - 2 * qvec[3]**2,
         2 * qvec[1] * qvec[2] - 2 * qvec[0] * qvec[3],
         2 * qvec[3] * qvec[1] + 2 * qvec[0] * qvec[2]],
        ...
```

其中 `qvec[0]=w, qvec[1]=x, qvec[2]=y, qvec[3]=z`，与公式逐项对应。同文件还有逆变换 `rotmat2qvec`（[scene/colmap_loader.py:55-66](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/colmap_loader.py#L55-L66)），通过对称矩阵 K 做特征值分解把矩阵变回四元数，并强制 \(w \ge 0\) 消除双重覆盖（\(q\) 与 \(-q\) 表示同一旋转）。

约定切换发生在 `readColmapCameras`（[scene/dataset_readers.py:99-101](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L99-L101)）：

```python
uid = intr.id
R = np.transpose(qvec2rotmat(extr.qvec))   # w2c 旋转 → 转置成 c2w 存入 CameraInfo
T = np.array(extr.tvec)                    # 平移不转置、不改号
```

这一行 `np.transpose` 就是两套约定的分界线。`getWorld2View2`（[utils/graphics_utils.py:39-50](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/graphics_utils.py#L39-L50)）在拼矩阵前又执行 `R.transpose()` 转回 \(R_{w2c}\)，并通过对 W2C 求逆得到 `cam_center`（可选地做平移/缩放归一化）。

#### 4.3.4 代码实践

**实践目标**：验证 `qvec2rotmat` 的正确性与 R/T 转置链路。

```python
# 示例代码（接 4.1.4 已加载的 cl 模块）
import numpy as np
R = cl.qvec2rotmat(np.array([1.0, 0.0, 0.0, 0.0]))   # 单位四元数
print(R)                                              # 应为单位矩阵
rng = np.random.default_rng(0)
for _ in range(5):
    q = rng.normal(size=4); q /= np.linalg.norm(q)    # 随机单位四元数
    R = cl.qvec2rotmat(q)
    print(np.allclose(R @ R.T, np.eye(3)), round(np.linalg.det(R), 6))
```

1. **实践目标**：确认单位四元数对应单位矩阵；随机单位四元数对应正交且行列式为 +1 的矩阵。
2. **操作步骤**：如上，先测恒等旋转，再随机采样。
3. **需要观察的现象**：第一组打印 `[[1,0,0],[0,1,0],[0,0,1]]`；随后每行打印 `True` 和 `1.0`。
4. **预期结果**：`R @ R.T == I` 且 `det(R) ≈ 1`（浮点误差在 1e-6 量级）。可选用 `scipy` 交叉验证：`scipy.spatial.transform.Rotation.from_quat([x, y, z, w]).as_matrix()`（注意 scipy 是 **w 在后**的 xyzw 约定）应与 `qvec2rotmat([w,x,y,z])` 一致——这一对比能让你牢牢记住两边的四元数顺序约定。（待本地验证。）

#### 4.3.5 小练习与答案

**练习 1**：`CameraInfo.R` 是 C2W 还是 W2C 的旋转？`CameraInfo.T` 呢？
**答案**：`R` 是 C2W 旋转（\(R_{w2c}^{\top}\)）；`T` 仍是 W2C 的平移 \(t\)。两者属于不同方向的变换，不能直接拼接使用——要得到 W2C 矩阵必须走 `getWorld2View2`（内部把 R 再转置回去）；要得到相机中心则用公式 \(C = -R_{w2c}^{\top} t\)（等价于对 W2C 求逆取平移列），代码里正是通过 `getWorld2View2` 的求逆实现。这也是为什么 `getNerfppNorm` 要先 `getWorld2View2(cam.R, cam.T)` 再取逆求相机中心（[scene/dataset_readers.py:79-81](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L79-L81)）。

**练习 2**：为什么 `qvec2rotmat` 没有对输入四元数做归一化？如果 `images.bin` 里的 qvec 模长不为 1 会怎样？
**答案**：COLMAP 写出的 qvec 保证是单位四元数，loader 信任输入。若模长偏离 1，得到的矩阵不再正交（\(R R^{\top} \ne I\)），投影会整体缩放失真。所以自制数据时务必写入归一化后的四元数。

**练习 3**：`rotmat2qvec(qvec2rotmat(q))` 一定返回原来的 q 吗？
**答案**：不一定完全相同。\(q\) 与 \(-q\) 是同一旋转的两个表示，`rotmat2qvec` 里 `if qvec[0] < 0: qvec *= -1` 强制取 \(w\ge0\) 的那个，所以返回值可能与原 q 相差一个负号——这在线性代数上完全等价。

### 4.4 模块四：从 COLMAP 原始数据到 `CameraInfo`

#### 4.4.1 概念说明

`colmap_loader` 的输出是「COLMAP 术语」的（qvec/tvec/params），而渲染管线需要的是「渲染术语」的（R/T/FovX/FovY/宽高/时间戳）。中间的翻译层由 `scene/dataset_readers.py` 完成，其核心数据结构是 `CameraInfo`：

```python
# 引自 scene/dataset_readers.py:42-58
class CameraInfo(NamedTuple):
    uid: int
    R: np.array          # C2W 旋转（qvec2rotmat 后转置）
    T: np.array          # W2C 平移
    FovY: np.array
    FovX: np.array
    image: np.array
    depth: np.array
    image_path: str
    image_name: str
    width: int
    height: int
    timestamp: float = 0.0
    fl_x: float = -1.0
    fl_y: float = -1.0
    cx: float = -1.0
    cy: float = -1.0
```

完整定义见 [scene/dataset_readers.py:42-58](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L42-L58)。本讲只关注其中由 COLMAP 数据直接决定的字段（R/T/FovX/FovY/宽高/名称）；`timestamp` 等 4D 字段属于下一讲。

#### 4.4.2 核心流程

翻译流程（`readColmapCameras`，完整分析留到 u2-l2）：

```
对每个 extr in cam_extrinsics:
    intr = cam_intrinsics[extr.camera_id]           # 按 camera_id 关联内参
    R = transpose(qvec2rotmat(extr.qvec))           # 约定切换（4.3）
    T = extr.tvec
    按 intr.model 分派:
        SIMPLE_PINHOLE → focal = params[0]
        PINHOLE       → focal_x = params[0], focal_y = params[1]
        其他          → raise ValueError（必须先去畸变）
    FovX = focal2fov(focal_x, width)                # 焦距(像素) → 视场角(弧度)
    FovY = focal2fov(focal_y, height)
    图片路径 = images 目录 / extr.name
    → 组装 CameraInfo
```

其中焦距与视场角互化由 `graphics_utils` 提供（[utils/graphics_utils.py:94-98](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/graphics_utils.py#L94-L98)）：\( \text{fov} = 2\arctan\frac{\text{pixels}}{2f} \)，\( f = \frac{\text{pixels}}{2\tan(\text{fov}/2)} \)。之所以要换成角度，是因为渲染端（`getProjectionMatrix`，[utils/graphics_utils.py:52-72](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/graphics_utils.py#L52-L72)）用 fov 构造投影矩阵。

#### 4.4.3 源码精读

模型分派与 FoV 计算（[scene/dataset_readers.py:103-116](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L103-L116)）：

```python
if intr.model == "SIMPLE_PINHOLE":
    focal_length_x = intr.params[0]
    FovY = focal2fov(focal_length_x, height)
    FovX = focal2fov(focal_length_x, width)
elif intr.model == "PINHOLE":
    focal_length_x = intr.params[0]
    focal_length_y = intr.params[1]
    FovY = focal2fov(focal_length_y, height)
    FovX = focal2fov(focal_length_x, width)
else:
    raise ValueError(... "Only undistorted datasets (PINHOLE or SIMPLE_PINHOLE cameras) supported!")
```

注意 `FovX` 配 `width`、`FovY` 配 `height`——视场角的方向要和像素维度对应。另外 `readColmapSceneInfo` 的 try/except 回退（[scene/dataset_readers.py:259-268](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L259-L268)）先尝试 `images.bin/cameras.bin`，任何异常都回退到 `images.txt/cameras.txt`，与 README 的「两种格式都支持」呼应。

#### 4.4.4 代码实践

**实践目标**：手工复现 `focal2fov`，验证内参到视场角的换算。

1. 设一台 PINHOLE 相机 fx=fy=1000 像素，图像 1920×1080；
2. 手算：\( \text{FovX} = 2\arctan\frac{1920}{2 \times 1000} = 2\arctan(0.96) \)；
3. 与代码对照：

```python
# 示例代码
import math
def focal2fov(focal, pixels):          # 与 graphics_utils.py:97-98 等价
    return 2 * math.atan(pixels / (2 * focal))
print(focal2fov(1000, 1920), focal2fov(1000, 1080))
```

4. **需要观察的现象**：两个方向视场角不同（宽方向更大）。
5. **预期结果**：FovX ≈ 1.530 rad（约 87.7°），FovY ≈ 0.987 rad（约 56.6°）。（待本地验证：以本地输出为准。）

#### 4.4.5 小练习与答案

**练习 1**：`readColmapCameras` 中 `FovY = focal2fov(focal_length_y, height)`，如果把 height 错写成 width 会发生什么？
**答案**：垂直视场角被按宽度计算，得到的 FovY 偏大（宽高比大于 1 时），投影矩阵纵向拉伸，渲染出的人物/场景会纵向变形。这类「方向配错」不会报错，只能通过渲染结果发现，是内参调试的经典暗坑。

**练习 2**：`CameraInfo` 里为什么同时保留 `FovX/FovY` 和 `fl_x/fl_y/cx/cy`（默认 -1）两套内参表示？
**答案**：fov 是 3DGS 渲染管线的原生输入（构造投影矩阵）；像素级焦距/光心（fl_x 等）供需要精确像素内参的路径使用，例如 `getProjectionMatrixCenterShift`（[utils/graphics_utils.py:74-92](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/graphics_utils.py#L74-L92)）支持光心不在图像中心的非对称投影。默认 -1 表示「未提供」，由数据集类型决定是否填充。

**练习 3**：`CameraInfo.timestamp` 的默认值为什么是 0.0 而不是 -1？
**答案**：静态场景（3D 数据）没有时间概念，0.0 是安全的默认时刻；4C4D 的动态数据会在 `process_camera_info` 里按帧号覆盖成 \([0,10)\) 区间的归一化时间戳（下一讲 u2-l2 详述）。用 -1 反而会污染需要数值参与计算的时间边缘化公式。

## 5. 综合实践

**任务：手工构造一个 txt 版 `sparse/0/`，用仓库自带 loader 读回并打印统计信息。** 这正对应本讲的规格化实践任务——「若没有二进制数据，则手工构造一个 txt 版本的 sparse/0 目录再读取」。全部实践只需 numpy + 标准库，无需 GPU。

### 步骤 1：构造目录与三个 txt 文件

在仓库外任选位置（例如 `/tmp/mini_colmap/`）创建：

```
mini_colmap/sparse/0/
├── cameras.txt     # 2 台 PINHOLE 相机
├── images.txt      # 2 相机 × 2 帧 = 4 条位姿
└── points3D.txt    # 3 个三维点
```

`cameras.txt`（每行：CAMERA_ID, MODEL, WIDTH, HEIGHT, PARAMS）：

```
# Camera list with one line of data per camera:
#   CAMERA_ID, MODEL, WIDTH, HEIGHT, PARAMS[]
1 PINHOLE 1920 1080 1000 1000 960 540
2 PINHOLE 1920 1080 1010 1010 960 540
```

`images.txt`（每条位姿两行：第一行 IMAGE_ID, QW, QX, QY, QZ, TX, TY, TZ, CAMERA_ID, NAME；第二行是 2D 点三元组 X, Y, POINT3D_ID）：

```
# Image list with two lines per image:
#   IMAGE_ID, QW, QX, QY, QZ, TX, TY, TZ, CAMERA_ID, NAME
#   POINTS2D[] as (X, Y, POINT3D_ID)
1 1.0 0.0 0.0 0.0 0.1 0.2 0.3 1 cam00_0000.png
100 200 -1
2 1.0 0.0 0.0 0.0 0.1 0.2 0.3 2 cam01_0000.png
300 400 -1
3 1.0 0.0 0.0 0.0 0.1 0.2 0.5 1 cam00_0001.png
100 200 -1
4 1.0 0.0 0.0 0.0 0.1 0.2 0.5 2 cam01_0001.png
300 400 -1
```

`points3D.txt`（每行：POINT3D_ID, X, Y, Z, R, G, B, ERROR, TRACK[]）：

```
# 3D point list with one line of data per point in the map:
#   POINT3D_ID, X, Y, Z, R, G, B, ERROR, TRACK[] as (IMAGE_ID, POINT2D_IDX)
1 0.1 0.2 0.3 100 150 200 1.5 1 0
2 0.4 0.5 0.6 120 130 140 1.2 2 0
3 0.7 0.8 0.9 110 160 210 0.9 3 0
```

### 步骤 2：写读取脚本

保存为 `/tmp/mini_colmap/inspect_colmap.py`（示例代码）：

```python
"""迷你 COLMAP 数据检查脚本：只依赖 numpy，按文件路径直接加载 colmap_loader，
绕开 scene/__init__.py 里的 torch/CUDA 依赖。"""
import os
import numpy as np
import importlib.util

REPO = "/path/to/4C4D"                      # 改成你的仓库路径
spec = importlib.util.spec_from_file_location(
    "colmap_loader", os.path.join(REPO, "scene/colmap_loader.py"))
cl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cl)

sparse_dir = os.path.join(os.path.dirname(__file__), "sparse/0")

# 先试 .bin，再回退 .txt —— 模仿 readColmapSceneInfo 的策略
if os.path.exists(os.path.join(sparse_dir, "cameras.bin")):
    intrinsics = cl.read_intrinsics_binary(os.path.join(sparse_dir, "cameras.bin"))
    extrinsics = cl.read_extrinsics_binary(os.path.join(sparse_dir, "images.bin"))
    xyz, rgb, err = cl.read_points3D_binary(os.path.join(sparse_dir, "points3D.bin"))
else:
    intrinsics = cl.read_intrinsics_text(os.path.join(sparse_dir, "cameras.txt"))
    extrinsics = cl.read_extrinsics_text(os.path.join(sparse_dir, "images.txt"))
    xyz, rgb, err = cl.read_points3D_text(os.path.join(sparse_dir, "points3D.txt"))

print(f"相机数量: {len(intrinsics)}")
for cid, cam in sorted(intrinsics.items()):
    print(f"  camera {cid}: model={cam.model}, {cam.width}x{cam.height}, "
          f"params={np.round(cam.params, 1)}")

print(f"图像(位姿)数量: {len(extrinsics)}")
for iid, img in sorted(extrinsics.items()):
    R = img.qvec2rotmat()
    print(f"  image {iid}: {img.name}, camera_id={img.camera_id}, "
          f"qvec={np.round(img.qvec, 2)}, tvec={np.round(img.tvec, 2)}, "
          f"R 正交: {np.allclose(R @ R.T, np.eye(3))}")

print(f"3D 点数量: {0 if xyz is None else xyz.shape[0]}")
if xyz is not None:
    print(f"  xyz shape: {xyz.shape}, rgb shape: {rgb.shape}, error shape: {err.shape}")
```

### 步骤 3：运行并观察

```bash
cd /tmp/mini_colmap && python inspect_colmap.py
```

**预期结果**（基于源码逻辑推演，待本地验证）：

```
相机数量: 2
  camera 1: model=PINHOLE, 1920x1080, params=[1000. 1000.  960.  540.]
  camera 2: model=PINHOLE, 1920x1080, params=[1010. 1010.  960.  540.]
图像(位姿)数量: 4
  image 1: cam00_0000.png, camera_id=1, qvec=[1. 0. 0. 0.], tvec=[0.1 0.2 0.3], R 正交: True
  ...
3D 点数量: 3
  xyz shape: (3, 3), rgb shape: (3, 3), error shape: (3, 1)
```

重点核对：① 4 条 image 记录通过 `camera_id` 正确关联到 2 台相机；② 单位四元数 (1,0,0,0) 对应的 R 满足正交性；③ `read_points3D_text` 返回的 errors 形状是 `(N,1)` 而非 `(N,)`（见 [scene/colmap_loader.py:126](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/colmap_loader.py#L126)）。

### 步骤 4（可选进阶）：二进制往返

把 `cameras.txt` 的内容用 `struct.pack` 按 4.2.2 的布局写成 `cameras.bin`（头 8 字节数量 + 每台 24 字节 `iiQQ` + 4 个 double），再让脚本走 `.bin` 分支读回，比较两条路径打印是否完全一致——一致即证明你对二进制布局的理解与 loader 实现吻合。若中途 `read_intrinsics_binary` 抛 `struct.error`，多半是 `num_params` 与实际写入的 double 个数不匹配。

## 6. 本讲小结

- `sparse/0/` 三件套分工明确：`cameras.*` 存内参（每台物理相机一条）、`images.*` 存外参与帧名（每相机每帧一条）、`points3D.*` 存初始点云；`.bin` 与 `.txt` 双格式支持，解析入口是 `scene/colmap_loader.py` 的 6 个 `read_*` 函数。
- 二进制解析 = 「布局表 + `read_next_bytes`」：`cameras.bin` 用 `iiQQ` 固定头 + 查表定长的参数段；`images.bin` 用 64 字节 `idddddddi` 头 + `\x00` 结尾的变长文件名 + 2D 点段；`points3D.bin` 用 43 字节 `QdddBBBd` 头 + 被丢弃的 track。全部小端序。
- COLMAP 外参是单位四元数 \(q=(w,x,y,z)\) + 平移 \(t\) 的 W2C 变换；`qvec2rotmat` 给出标准旋转矩阵，而 `CameraInfo` 里存的是转置后的 R（C2W 旋转）加未变的 T，`getWorld2View2` 会再转置拼回 W2C。
- 4C4D 只支持 `SIMPLE_PINHOLE`/`PINHOLE`：输入数据必须先去畸变；内参在 `readColmapCameras` 里经 `focal2fov` 换算成 FovX/FovY（方向与宽高一一对应）。
- `colmap_loader.py` 仅依赖 numpy/struct，可用 `importlib` 按路径单独加载做实验，不必装齐 CUDA 扩展。

## 7. 下一步学习建议

本讲拿到的还只是「散装」的 COLMAP 对象（两个字典 + 三个数组）。下一讲 **u2-l2《readColmapSceneInfo：从相机×帧到训练集》** 将把这些对象组装成训练可用的 `SceneInfo`：如何按 `camXX_YYYY` 命名把 4 台相机扩展成全部帧、timestamp 如何归一化到 \([0,10)\)、`training_view` 如何划分训练/测试相机、点云如何下采样。建议先自己通读 [scene/dataset_readers.py:255-351](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L255-L351)（`readColmapSceneInfo`），带着「本讲的哪些输出被它用了、哪些没用」的问题去读，收获最大。若想了解数据从零准备的过程（COLMAP/MASt3R），可提前浏览 **u2-l5** 与 [README.md:102-137](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/README.md#L102-L137)。
