# NeRF 数据集与 transforms.json

## 1. 本讲目标

本讲是「NeRF 原语深入」单元的第一篇。在第二单元里你已经知道：拖入一个目录或 `.json` 文件，Testbed 会自动进入 `ETestbedMode::Nerf`，并把目录下的 `transforms.json` 交给 `load_nerf()` 加载。但 `transforms.json` 里到底写了什么？这些相机参数如何变成 GPU 上可训练的数据？为什么同一个场景换一台相机采集就要做坐标转换？本讲回答这些问题。

学完后你应该能够：

1. 说清 `transforms.json` 的顶层字段与 `frames` 列表里每一帧的结构。
2. 理解 `NerfDataset` 这个 C++ 结构体如何把磁盘 JSON 变成内存里（大部分在 GPU 上）的训练数据。
3. 手推 `nerf_matrix_to_ngp` / `nerf_position_to_ngp` 对相机位姿做的坐标变换，并能解释为什么需要它。
4. 掌握 `aabb_scale` 对场景包围盒与训练质量的影响，知道为什么自然场景要从 128 开始调。
5. 看懂 `colmap2nerf.py` 把一组照片变成 `transforms.json` 的完整流程。

## 2. 前置知识

在进入源码前，先建立几个直觉。

**相机位姿用一个 4×4（或存的 3×4）矩阵表示。** 在 NeRF 里通常存的是「相机到世界」（camera-to-world，c2w）矩阵 \(M\)。它的几何含义是：

- 前 3 列分别是相机自身的 **右、上、前** 三个朝向轴在世界坐标里的方向向量；
- 第 4 列是相机中心在世界坐标里的位置。

给定了这个矩阵，你就知道每张照片是从哪里、朝哪个方向拍的。`transforms.json` 的核心就是给每一张训练图存这样一个矩阵。

**NeRF 训练本质是「照片 + 位姿 → 3D 场景」的反问题。** 网络要学的不是照片本身，而是照片背后那个 3D 场景的密度与颜色场。所以喂给网络的不只是像素，还有「这条光线从哪个相机出发、穿过哪个像素」——这就要求每张图的位姿必须准确。

**不同软件用的坐标轴约定不同。** COLMAP、原始 NeRF、instant-ngp 内部各自规定「哪根轴朝上、相机看哪根轴」。同一个数值矩阵在不同约定下含义不同，因此跨软件搬运数据时必须做坐标转换——这是本讲最容易踩坑、也最值得讲清的地方。

**`aabb_scale` 控制光线步进的范围。** instant-ngp 默认只在 \([0,0,0]\) 到 \([1,1,1]\) 的单位立方体里追踪光线；真实户外场景有远景背景，超出这个盒子就装不下，于是用一个整数倍数把盒子放大。

> 承接：本讲依赖 [u2-l3](u2-l3-file-loading.md) 的「文件加载与模式自动识别」——`mode_from_scene` 把目录判为 NeRF 后，`load_training_data` 才会调用本讲的 `load_nerf()`。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [include/neural-graphics-primitives/nerf_loader.h](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_loader.h) | 声明 `NerfDataset` 结构体与全部坐标转换函数、`load_nerf` 原型 |
| [src/nerf_loader.cu](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/nerf_loader.cu) | `load_nerf()` 的实现：解析 JSON、并行加载图像到 GPU、填充 `NerfDataset` |
| [scripts/colmap2nerf.py](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/colmap2nerf.py) | 从视频/照片经 COLMAP 运动恢复结构（SfM）生成 `transforms.json` |
| [docs/nerf_dataset_tips.md](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/docs/nerf_dataset_tips.md) | 官方数据采集与 `aabb_scale` 调参建议 |
| [data/nerf/fox/transforms.json](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/data/nerf/fox/transforms.json) | 一个真实的 `transforms.json` 样例（fox 场景） |

记忆线索：**磁盘（`.json` + 图像）→ `load_nerf()` 解析 → `NerfDataset`（内存/GPU）→ 训练循环**。本讲只讲前三段，训练循环留给 [u4-l4](u4-l4-nerf-training-loop.md)。

---

## 4. 核心概念与源码讲解

### 4.1 NerfDataset：NeRF 数据集在内存中的样子

#### 4.1.1 概念说明

`NerfDataset` 是 instant-ngp 里「一个 NeRF 数据集」的完整内存表示。它是一个**普通结构体**（不是类，没有继承），把训练所需的全部信息打包在一起：每张图的像素（已搬到 GPU）、每张图的相机位姿、每张图的元数据（焦距、镜头畸变、滚动快门等）、以及全局的缩放/偏移/包围盒参数。

为什么要把这么多东西塞进一个结构体？因为 NeRF 训练时，每一步都要从「随机一张图 → 取出它的像素和位姿 → 发射光线」。把这些数据集中存放、并把热数据放在 GPU 上，才能让训练循环高效取用。你可以把它理解成「数据加载器产出的、直接喂给训练器的快照」。

#### 4.1.2 核心流程

`NerfDataset` 有两条生产路径：

```text
路径 A（离线，最常用）：transforms.json + 图像文件夹
        └─ load_nerf() 解析 JSON、并行读图、搬上 GPU、填充结构体

路径 B（在线流式）：手机实时传入带位姿的图
        └─ create_empty_nerf_dataset() 先建空壳，再逐帧 set_training_image()
```

无论哪条路径，最终都得到一个字段齐备的 `NerfDataset`。它的关键字段分三类：

- **图像数据（在 GPU 上）**：`pixelmemory`（像素）、`depthmemory`（可选深度）、`raymemory`（可选逐像素光线）。
- **位姿与元数据**：`xforms`（每张图的相机矩阵）、`metadata`（焦距/镜头等）。
- **全局几何参数**：`scale`、`offset`、`aabb_scale`、`render_aabb`、`from_mitsuba`。

#### 4.1.3 源码精读

结构体定义在头文件里。先看它持有哪些字段：

[include/neural-graphics-primitives/nerf_loader.h:54-76](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_loader.h#L54-L76) —— `NerfDataset` 的数据成员：上面三类字段都在这里。注意 `pixelmemory` / `depthmemory` / `raymemory` 都是 `GPUMemory<...>`，即数据加载完就直接落在显存。

几个最关键的字段：

- `std::vector<TrainingXForm> xforms`：每张图的相机位姿（start/end 两个，用于滚动快门插值）。
- `float scale = 1.0f;` 与 `vec3 offset`：把外部坐标映射进单位立方体的全局缩放与偏移。
- `int aabb_scale = 1;`：包围盒放大倍数（详见 4.2）。
- `bool from_mitsuba = false;`：数据来源标志，决定坐标转换走哪条分支（详见 4.3）。

文件顶部还定义了一个常量，它是默认缩放的来源：

[include/neural-graphics-primitives/nerf_loader.h:28-29](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_loader.h#L28-L29) —— `NERF_SCALE = 0.33f`。注释说得很直白：「相对于原始 NeRF 数据集把场景缩放多少倍；我们要把它塞进单位立方体」。

生产路径 A 的入口声明：

[include/neural-graphics-primitives/nerf_loader.h:171-172](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_loader.h#L171-L172) —— `load_nerf()` 接收一组 JSON 路径（支持多文件拼接），返回填好的 `NerfDataset`；`create_empty_nerf_dataset()` 是流式路径的空壳构造函数。

路径 B 的空壳实现，展示了字段的默认值：

[src/nerf_loader.cu:153-173](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/nerf_loader.cu#L153-L173) —— 这里能看到默认 `scale = NERF_SCALE`（0.33）、`offset = {0.5, 0.5, 0.5}`、每张图位姿初始化为单位矩阵。

#### 4.1.4 代码实践

**实践目标**：建立对 `NerfDataset` 字段的直观印象，分清「哪些在 CPU、哪些在 GPU」。

**操作步骤**：

1. 打开 [nerf_loader.h 的 `NerfDataset` 定义](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_loader.h#L49-L89)。
2. 把字段抄成三列的表格：字段名、类型、属于哪一类（图像数据 / 位姿元数据 / 全局几何）。
3. 特别留意 `GPUMemory<...>` 包装的字段——这些就是已经在显存里的。

**需要观察的现象**：`xforms`、`metadata`、`paths` 是 `std::vector`（在 CPU 内存），而 `pixelmemory`、`depthmemory`、`raymemory`、`metadata_gpu`、`sharpness_data` 是 `GPUMemory`（在显存）。

**预期结果**：你会发现「热路径数据（像素、光线、元数据）都在 GPU 上，控制用的列表（路径、位姿数组）在 CPU」。这正是为了让训练循环少做 CPU↔GPU 拷贝。

> 待本地验证：如果你能编译带 GUI 的版本，加载 fox 后在 GUI 里看不到这个结构体（它是 C++ 内部的），但本实践是纯源码阅读型，不需要运行。

#### 4.1.5 小练习与答案

**练习 1**：`NerfDataset` 里既有 `metadata`（`std::vector`）又有 `metadata_gpu`（`GPUMemory`），为什么同一个东西存两份？

> **参考答案**：`metadata` 是 CPU 端的权威副本，便于加载与（必要时）相机自标定时修改；`metadata_gpu` 是拷到 GPU 的镜像，供训练/渲染内核高速读取。`update_metadata()` 负责把前者同步到后者（见 [nerf_loader.cu:852-868](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/nerf_loader.cu#L852-L868)）。

**练习 2**：`TrainingXForm` 里为什么有 `start` 和 `end` 两个矩阵，而不是一个？

> **参考答案**：为了支持滚动快门（rolling shutter）与运动模糊。一帧的曝光时间内相机在运动，`start`/`end` 表示曝光起止两个瞬间的位姿，渲染时按像素的时间偏移在两者间插值（见 4.2 的 `rolling_shutter` 字段）。无滚动快门时两者相同。

---

### 4.2 transforms.json：磁盘上的数据格式

#### 4.2.1 概念说明

`transforms.json` 是 instant-ngp 的 NeRF 数据集在磁盘上的格式，**与原始 NeRF 代码库兼容**。一个目录要被识别为 NeRF 场景，核心标志就是里面有一份 `transforms.json`（这也是 [u2-l3](u2-l3-file-loading.md) 里 `mode_from_scene` 把目录判为 `Nerf` 的依据）。

它的结构很朴素：顶层是一组全局相机内参和场景参数，下面挂一个 `frames` 数组，每个元素描述一张训练图——它的文件路径与拍摄时的相机外参矩阵。可以这样理解它的角色：

```text
transforms.json = 全局相机内参（焦距/畸变/分辨率）
                + 场景参数（aabb_scale/scale/offset）
                + frames[] 每帧的外参矩阵 + 图像路径
```

#### 4.2.2 核心流程

`load_nerf()` 解析 `transforms.json` 的流程可以概括为「两遍扫描 + 并行加载」：

```text
1. 解析所有 JSON 文件，统计 n_images，按文件名自然排序 frames
2. 第一遍：读全局参数（scale/offset/aabb_scale/from_mitsuba/镜头/焦距）
           并据此把每帧的平移列先做 scale+offset，累计相机包围盒 cam_aabb
3. 第二遍（线程池并行，逐帧）：
   a. resolve_path 找到图像文件（支持自动补扩展名）
   b. 读图：stbi（普通图）/ exr（HDR）；可选加载 alpha、dynamic_mask、depth、rays
   c. 读焦距（支持 x_fov / fl_x / camera_angle_x 多种写法）
   d. 取出 transform_matrix，调用 nerf_matrix_to_ngp() 做坐标转换
4. 把每张图搬到 GPU（set_training_image），并计算清晰度图（用于剔模糊帧）
```

这里有两个值得记住的细节：图像是**多线程并行**加载的（`pool.parallel_for_async`），而坐标转换是每帧独立做的（所以转换函数必须是纯函数式、线程安全）。

#### 4.2.3 源码精读

先看一份真实的 `transforms.json` 长什么样。这是 fox 场景的顶层：

[data/nerf/fox/transforms.json:1-15](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/data/nerf/fox/transforms.json#L1-L15) —— 顶层字段：`camera_angle_x`/`camera_angle_y`（水平/垂直视场角，弧度）、`fl_x`/`fl_y`（焦距，像素单位）、`k1`/`k2`/`p1`/`p2`（OpenCV 径向/切向畸变）、`cx`/`cy`（主点）、`w`/`h`（分辨率）、`aabb_scale`、然后是 `frames` 数组。

每一帧的结构（fox 第一帧）：

[data/nerf/fox/transforms.json:16-44](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/data/nerf/fox/transforms.json#L16-L44) —— 每帧三个字段：`file_path`（图像相对路径）、`sharpness`（清晰度，由 colmap2nerf 算出，用于剔模糊帧）、`transform_matrix`（4×4 的 camera-to-world 矩阵）。

现在看 C++ 端如何解析这些字段。焦距的读取尤其灵活，支持三种历史写法：

[src/nerf_loader.cu:243-271](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/nerf_loader.cu#L243-L271) —— `read_focal_length()`：依次尝试 `<axis>_fov`（度）、`fl_<axis>`（像素）、`camera_angle_<axis>`（弧度），把它们统一换算成像素焦距。注释自嘲「x_fov 是度、camera_angle_x 是弧度，是的，很蠢」——这是为了兼容原始 NeRF 的多种历史格式。

镜头畸变与镜头模式的读取：

[src/nerf_loader.cu:175-241](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/nerf_loader.cu#L175-L241) —— `read_lens()`：根据 `k1..k4`/`p1,p2` 切到 `OpenCV`/`OpenCVFisheye` 模式；根据 `ftheta_p*` 切到鱼眼 `FTheta`；根据 `latlong`/`equirectangular`/`orthographic` 切到全景/正交模式。缺省是普通透视 `Perspective`。

`aabb_scale`、`scale`、`offset` 这三个全局参数的读取：

[src/nerf_loader.cu:497-506](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/nerf_loader.cu#L497-L506) —— 读取 `aabb_scale`（整数）、`offset`（数组或标量）。`scale` 的读取在 [src/nerf_loader.cu:474-476](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/nerf_loader.cu#L474-L476)。

每帧矩阵的提取与坐标转换调用点：

[src/nerf_loader.cu:668-703](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/nerf_loader.cu#L668-L703) —— 这里把 `transform_matrix`（或 `transform_matrix_start`/`_end`）的 3×4 部分拷进 `xforms[i].start/.end`，然后立刻调用 `nerf_matrix_to_ngp()` 把它转到 ngp 内部坐标系（转换细节见 4.3）。

#### 4.2.4 关于 aabb_scale 的深入说明

`aabb_scale` 是 instant-ngp 专属、最重要的场景参数。官方文档讲得很清楚：

[docs/nerf_dataset_tips.md:23-31](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/docs/nerf_dataset_tips.md#L23-L31) —— 默认只在 \([0,0,0]\)-\([1,1,1]\) 单位立方体内追踪光线；加载器默认把相机位置乘 `0.33`、偏移 \([0.5,0.5,0.5]\)，把数据原点映射到立方体中心；对有远景背景的自然场景，需把 `aabb_scale` 设为 2 的幂（最大 128），让光线追踪到更大的盒子（以 \([0.5,0.5,0.5]\) 为中心、边长为 `aabb_scale`）。

几何含义：设放大倍数为 \(s\)（`aabb_scale`），则实际追踪包围盒为

\[
\text{AABB} = [0.5 - s/2,\; 0.5 + s/2]^3
\]

\(s=1\) 时退化为单位立方体；\(s=128\) 时边长达 128，能装下广阔的户外背景。代价是：盒子越大，密度网格的级联（cascade）越多、训练略慢、单位体积的分辨率被摊薄。

> 这里的 `aabb_scale` 还会通过级联影响哈希编码最细层的分辨率（见 [u3-l2](u3-l2-hash-grid-encoding.md) 里 `per_level_scale` 对 `aabb_scale` 的乘法），所以它同时影响「能装多大场景」和「最细能表达多少细节」。

#### 4.2.5 代码实践

**实践目标**：亲手读一份真实 `transforms.json`，建立「字段 ↔ 含义」的对应。

**操作步骤**：

1. 打开 [fox 的 transforms.json](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/data/nerf/fox/transforms.json)。
2. 找出顶层焦距是哪个字段、单位是什么；找出 `aabb_scale` 的值。
3. 数一下 `frames` 数组大约有多少帧（每帧一个 `transform_matrix`）。
4. 对照 `read_focal_length()` 的三种写法，判断 fox 用的是哪一种。

**需要观察的现象**：fox 用 `fl_x`/`fl_y`（像素焦距）而非 `camera_angle_x`；`aabb_scale` 是个小整数（4）；`transform_matrix` 是 4 行 4 列，最后一行是 `[0,0,0,1]`。

**预期结果**：你能口述「fox 是一个有背景的小场景，焦距约 1375 像素，aabb_scale=4 意味着追踪盒子边长 4」。如果换成一个无背景的原始 NeRF 合成物体（如 lego），`aabb_scale` 应为 1。

#### 4.2.6 小练习与答案

**练习 1**：如果你的 `transforms.json` 既没有 `fl_x` 也没有 `camera_angle_x`，加载会怎样？

> **参考答案**：`read_focal_length()` 返回 `false`，`load_nerf()` 在 [src/nerf_loader.cu:684-686](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/nerf_loader.cu#L684-L686) 抛出 `"Couldn't read fov."` 异常。

**练习 2**：`aabb_scale` 设得过大（比如一律 128）有什么坏处？

> **参考答案**：盒子变大 → 级联增多、训练变慢、单位空间分辨率下降；空区域增多也可能让密度网格更稀疏。所以文档建议「先 128 保兜底，再尽量往下调」。

---

### 4.3 坐标系转换：ngp ↔ nerf

#### 4.3.1 概念说明

这是本讲最核心、也最容易出错的部分。同一个 4×4 相机矩阵，在不同软件的坐标约定下含义完全不同。原始 NeRF、COLMAP、mitsuba 渲染器各自规定「哪根轴朝上、相机沿哪根轴看」。instant-ngp 内部用的是另一套约定，所以从外部数据进到 ngp 前，必须做一次坐标转换。

`NerfDataset` 提供了一组互为逆操作的转换函数：

| 方向 | 函数 | 用途 |
| --- | --- | --- |
| nerf → ngp | `nerf_matrix_to_ngp` / `nerf_position_to_ngp` / `nerf_direction_to_ngp` | 加载数据、训练采样时用 |
| ngp → nerf | `ngp_matrix_to_nerf` / `ngp_position_to_nerf` | 导出、与原始 NeRF 工具链对接时用 |

转换分两条分支，由 `from_mitsuba` 标志决定：mitsuba 渲染器产的数据走一套简单的轴翻转；其余（包括原始 NeRF、COLMAP 产出的）走另一套「翻转 + 循环置换」。本讲重点讲后者，因为它覆盖绝大多数真实数据集。

#### 4.3.2 核心流程：非 mitsuba 数据的三步坐标轴变换

对一个 camera-to-world 矩阵 \(M\)（列 0/1/2 是相机右/上/前轴，列 3 是位置），非 mitsuba 分支做如下处理。先看默认调用（`scale_columns=false`）下，对**三个坐标轴**的变换可以拆成三步：

**第 1 步——翻转第 1 列（上轴 Y）**：`result[1] *= -1`。把相机的「上」轴方向取反。

**第 2 步——翻转第 2 列（前轴 Z）**：`result[2] *= -1`。把相机的「前」轴方向取反。

> 这两步合起来，本质是把相机的朝向从原始 NeRF 的 OpenGL 约定（看 \(-Z\)、\(+Y\) 上）翻成 ngp 内部使用的约定。注意第 0 列（右轴 X）不动。

（与此同时，第 4 列即平移分量会做 `* scale + offset`，把世界坐标压进单位立方体——这是位置变换，不属于「轴」变换，但和轴变换在同一函数里完成。）

**第 3 步——循环置换三行 xyz ← yzx**：把矩阵的三行整体循环重排。若把一行视作某个世界坐标分量（第 0 行 = x 分量、第 1 行 = y 分量、第 2 行 = z 分量），则置换规则是

\[
x' = y,\quad y' = z,\quad z' = x
\]

即新坐标 \((x',y',z')\) 由旧坐标 \((x,y,z)\) 循环左移一位得到。这一步对**位置列和三个朝向列同时生效**（行操作作用于整行），保证整个刚体变换在同一个轴置换下一致。

用矩阵表示这个行置换 \(P\)：

\[
P = \begin{bmatrix} 0 & 1 & 0 \\ 0 & 0 & 1 \\ 1 & 0 & 0 \end{bmatrix},
\qquad
\begin{bmatrix} x' \\ y' \\ z' \end{bmatrix} = P \begin{bmatrix} x \\ y \\ z \end{bmatrix}
\]

位置转换函数 `nerf_position_to_ngp` 对非 mitsuba 数据正是「先 scale+offset，再 \((x,y,z)\mapsto(y,z,x)\)」，与矩阵的第 3 步完全一致——你可以拿它交叉验证。

#### 4.3.3 源码精读

矩阵转换主函数：

[include/neural-graphics-primitives/nerf_loader.h:101-120](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_loader.h#L101-L120) —— `nerf_matrix_to_ngp()`。代码分两段：先做列符号翻转与平移缩放（L103-106），再按 `from_mitsuba` 分支做轴处理（L108-117）。非 mitsuba 分支注释 `Cycle axes xyz<-yzx`，四行 `row(...)` 完成行循环置换。

逐行解读这四行 `row` 操作：

```cpp
vec4 tmp = row(result, 0);               // 暂存第 0 行
result = row(result, 0, row(result, 1));  // 第 0 行 ← 原第 1 行
result = row(result, 1, row(result, 2));  // 第 1 行 ← 原第 2 行
result = row(result, 2, tmp);             // 第 2 行 ← 原第 0 行(暂存)
```

于是新行 0 = 旧行 1、新行 1 = 旧行 2、新行 2 = 旧行 0，正好实现 \(x'\!=y, y'\!=z, z'\!=x\)。

位置转换（更简洁，便于理解同一置换）：

[include/neural-graphics-primitives/nerf_loader.h:148-151](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_loader.h#L148-L151) —— `nerf_position_to_ngp()`：`rv = pos * scale + offset;` 然后 `return vec3{rv.y, rv.z, rv.x};`。注意它**先缩放偏移、再置换**，与矩阵函数里「先处理列 3、再行置换」的顺序一致。

逆操作：

[include/neural-graphics-primitives/nerf_loader.h:141-146](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_loader.h#L141-L146) —— `ngp_position_to_nerf()` 是反过来的置换 \((x,y,z)\mapsto(z,x,y)\)，再 `(pos-offset)/scale`，构成严格逆运算。

方向转换（不受 scale/offset 影响，只做轴置换/翻转）：

[include/neural-graphics-primitives/nerf_loader.h:91-99](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_loader.h#L91-L99) —— `nerf_direction_to_ngp()`：mitsuba 数据整体取反，非 mitsuba 做 \((x,y,z)\mapsto(y,z,x)\)。

加载器里对 `up` 向量也做了同样的置换，佐证这套约定是全局一致的：

[src/nerf_loader.cu:528-533](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/nerf_loader.cu#L528-L533) —— 注释明说「axes are permuted as for the xforms below」，把 `json["up"]` 的分量按 \((y,z,x)\) 重排存入 `result.up`。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：亲手把 `nerf_matrix_to_ngp` 对非 mitsuba 数据做的坐标轴变换讲清楚、并对一个具体数字验证。

**操作步骤**：

1. 打开 [nerf_matrix_to_ngp 源码](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_loader.h#L101-L120)。
2. 用一句话写出它对非 mitsuba 数据做的**三步坐标轴变换**。
3. 取 fox 第一帧 `transform_matrix` 的平移列 \((3.168,\,-5.479,\,-0.979)\)（见 [fox transforms.json:24,30,36](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/data/nerf/fox/transforms.json#L19-L44)），手算它在 ngp 内部坐标系下的位置：先乘 `scale=0.33`、加 `offset=(0.5,0.5,0.5)`，再做 \((x,y,z)\mapsto(y,z,x)\)。
4. 用 `nerf_position_to_ngp` 的公式交叉验证你的结果。

**参考答案（三步坐标轴变换）**：

1. **翻转第 1 列（上轴 Y）**：`result[1] *= -1`。
2. **翻转第 2 列（前轴 Z）**：`result[2] *= -1`。（这两步把相机朝向从原始 NeRF 约定翻到 ngp 约定，右轴 X 不变。）
3. **循环置换三行 xyz ← yzx**：新行 0 = 旧行 1、新行 1 = 旧行 2、新行 2 = 旧行 0，等价于对所有坐标分量做 \((x,y,z)\mapsto(y,z,x)\)。

（附带：平移列额外做 `*scale+offset`，但那是位置缩放，不算「轴」变换。）

**手算验证**：

\[
\begin{aligned}
p &= (3.168,\,-5.479,\,-0.979) \\
p\cdot 0.33 + 0.5 &= (1.545,\,-1.308,\,0.177) \\
\text{ngp 位置} &= (y,z,x) = (-1.308,\,0.177,\,1.545)
\end{aligned}
\]

这与 `nerf_position_to_ngp`（`rv.y, rv.z, rv.x`）的结果一致。

**需要观察的现象**：转换后的相机位置分量被循环重排了，且因为 scale/offset 落在了以 0.5 为中心的单位立方体附近。

**预期结果**：你能在不看答案的情况下，复述「翻转 Y、翻转 Z、循环 xyz←yzx」三步，并对任意平移列算出 ngp 坐标。

> 待本地验证：上面手算用的是默认 `scale_columns=false`，即 `load_nerf` 的真实调用方式；若有人传 `scale_columns=true`（代码里没有这种调用点），旋转列也会被 scale 缩放，结论会不同。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `from_mitsuba` 数据不需要做循环置换、只需翻转第 0、2 列？

> **参考答案**：mitsuba 渲染器的世界坐标约定与 ngp 内部约定只差一个镜像（翻转 X 和 Z），不存在轴循环错位，所以 [nerf_loader.h:108-110](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_loader.h#L108-L110) 直接 `result[0]*=-1; result[2]*=-1;`。而原始 NeRF/COLMAP 与 ngp 之间差一个轴循环，所以非 mitsuba 走循环置换分支。

**练习 2**：`nerf_position_to_ngp` 是「先缩放后置换」，如果改成「先置换后缩放」结果还一样吗？

> **参考答案**：不一样。缩放/偏移是对每个分量独立做的线性变换，置换是重排分量顺序；两者不可交换。代码选择「先 scale+offset、再置换」是因为 scale/offset 定义在**原始 NeRF 坐标系**里（见 `load_nerf` 里先对列 3 做 scale+offset、再整体行置换），必须先在原系里归一化、再换系。

---

### 4.4 colmap2nerf：从照片重建 transforms.json

#### 4.4.1 概念说明

很多情况下你手上只有一组照片或一段视频，没有现成的相机位姿。`scripts/colmap2nerf.py` 解决的就是这个问题：它调用开源的 [COLMAP](https://colmap.github.io/) 做「运动恢复结构」（Structure-from-Motion, SfM），从图像间的特征匹配反推出每张图的相机位姿与内参，再整理成 instant-ngp 能读的 `transforms.json`。

它是 instant-ngp 数据准备的**主入口**：fox、robot 等示例的 `transforms.json` 都可以这样产出。理解它的流程，你就能把自己的拍摄素材变成可训练数据。

#### 4.4.2 核心流程

```text
输入：一段视频 或 一个 images/ 文件夹
  │
  ├─ (可选) run_ffmpeg：按 video_fps 抽帧成图像（建议 50-150 张）
  │
  ├─ (可选) run_colmap：调 COLMAP 三步走
  │     feature_extractor → *_matcher → mapper → bundle_adjuster
  │     产出 cameras.txt（内参）+ images.txt（每图四元数+平移）
  │
  ├─ 解析 cameras.txt → 相机内参（fl/cx/cy/k1..k4/fov）
  ├─ 解析 images.txt → 每帧 transform_matrix
  │     · 把 COLMAP 的 qvec/tvec 组装成 4×4
  │     · 默认重定向：翻转 Y/Z、交换行、把 up 对齐到 Z 轴
  │     · 求所有相机视线的最近交点作为场景中心，平移到原点
  │     · 按平均相机距离缩放到 "nerf sized"
  │
  └─ 写出 transforms.json（带 aabb_scale、可选 dynamic_mask）
```

「求视线最近交点作为中心」这一步值得展开：脚本对所有相机两两求「中心像素光线」的最近交点并加权平均，把该点当作「兴趣物体」位置平移到原点。这正是文档里说的「脚本假设所有训练图都大致指向同一个兴趣点」。

#### 4.4.3 源码精读

`aabb_scale` 命令行参数（choices 限定为 2 的幂）：

[scripts/colmap2nerf.py:40](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/colmap2nerf.py#L40) —— `--aabb_scale` 默认 32，可选 `1..128` 的 2 的幂，注释「1=场景塞进单位立方体」。

COLMAP 调用流程（feature_extractor → matcher → mapper → bundle_adjuster → model_converter）：

[scripts/colmap2nerf.py:123-140](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/colmap2nerf.py#L123-L140) —— `run_colmap()` 的核心几条命令。`--ImageReader.single_camera 1` 假设全部图来自同一台相机；matcher 默认 `sequential`（适合视频），无序照片用 `exhaustive`。

cameras.txt 解析（把 COLMAP 相机模型翻译成 fl/cx/cy/畸变）：

[scripts/colmap2nerf.py:214-291](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/colmap2nerf.py#L214-L291) —— 支持 SIMPLE_PINHOLE/PINHOLE/SIMPLE_RADIAL/RADIAL/OPENCV 及其 FISHEYE 变体，统一换算出 `fl_x/fl_y/cx/cy/k1..k4/p1/p2` 与 `camera_angle_x/y`。

位姿重定向与缩放（默认分支，非 `--keep_colmap_coords`）：

[scripts/colmap2nerf.py:350-410](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/colmap2nerf.py#L350-L410) —— 这是把 COLMAP 坐标系调成 ngp/NeRF 友好朝向的关键：L350-354 翻转 Y/Z 并交换行；L377-384 把 up 旋到 Z 轴；L386-402 求视线交点作中心；L404-410 按平均相机距离缩放到「nerf sized」。注意这套在**脚本里**做的轴处理，与加载时 `nerf_matrix_to_ngp` 在**程序里**做的轴处理是两道独立的工序——前者产出符合 NeRF 约定的 JSON，后者再把 NeRF 约定转到 ngp 内部。

#### 4.4.4 代码实践

**实践目标**：掌握用 colmap2nerf 把自己的素材变成可训练数据集的命令。

**操作步骤**：

1. 准备一段绕物体拍摄的视频（约 1 分钟）或一个 `images/` 文件夹（50-150 张清晰照片）。
2. 确认 `colmap`、`ffmpeg`（仅视频需要）在 PATH 中，并 `pip install -r requirements.txt`。
3. 在数据目录下，针对视频运行：
   ```sh
   python <instant-ngp>/scripts/colmap2nerf.py \
       --video_in my.mp4 --video_fps 2 --run_colmap --aabb_scale 32
   ```
   或针对照片文件夹运行：
   ```sh
   python <instant-ngp>/scripts/colmap2nerf.py \
       --colmap_matcher exhaustive --run_colmap --aabb_scale 32
   ```
4. 得到 `transforms.json` 后，用 `./instant-ngp <数据目录>` 加载训练。

**需要观察的现象**：脚本会打印每张图的 `sharpness=`、相机内参、`avg camera distance from origin`；最后写出 `transforms.json`。

**预期结果**：训练约 20 秒内应明显收敛（见 [nerf_dataset_tips.md:11](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/docs/nerf_dataset_tips.md#L11) 的经验法则）。若出现「floaters」或迟迟不收敛，首先怀疑 `aabb_scale` 与相机对齐。

> 待本地验证：实际 COLMAP 是否成功取决于图像纹理与覆盖；无纹理/重复纹理场景 COLMAP 易失败，此时需改用 Record3D/NeRFCapture 路径（见 [nerf_dataset_tips.md:134-178](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/docs/nerf_dataset_tips.md#L134-L178)）。

#### 4.4.5 小练习与答案

**练习 1**：`--colmap_matcher` 该选 `sequential` 还是 `exhaustive`？

> **参考答案**：视频（相机平滑移动、帧间相邻）选 `sequential`；无序随手拍的照片选 `exhaustive`。见 [colmap2nerf.py:34](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/colmap2nerf.py#L34) 与 [nerf_dataset_tips.md:118](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/docs/nerf_dataset_tips.md#L118)。

**练习 2**：`--keep_colmap_coords` 会改变什么？

> **参考答案**：加上它后脚本**不**做重定向/居中/缩放（跳过 [colmap2nerf.py:375-410](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/colmap2nerf.py#L375-L410)），改用 L365-373 的简单 `flip_mat` 翻转，保留 COLMAP 原始坐标系。适合你已经手动调好坐标、不想被脚本动过的场景。

---

## 5. 综合实践：追踪一份 NeRF 数据从磁盘到内存的完整旅程

把本讲四个模块串起来，做一个端到端的「数据追踪」任务。

**场景**：你拿到一个目录 `my_scene/`，里面有 `transforms.json` 和 `images/`。

**任务**：

1. **模式判定**（承接 [u2-l3](u2-l3-file-loading.md)）：说明 `mode_from_scene("my_scene/")` 会返回哪个 `ETestbedMode`，依据是什么？
2. **格式核对**：打开 `transforms.json`，确认它有 `frames` 数组；判断焦距用的是 `fl_x` 还是 `camera_angle_x`；记下 `aabb_scale`。
3. **坐标转换**：任取一帧的 `transform_matrix` 平移列，用本讲 4.3.4 的三步法手算它在 ngp 内部的位置，并用 `nerf_position_to_ngp` 公式验证。
4. **aabb_scale 决策**：如果场景有明显远景背景，按 [nerf_dataset_tips.md:121](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/docs/nerf_dataset_tips.md#L121) 的建议，初始 `aabb_scale` 应设多少？为什么先大后小？
5. **加载链复述**：用一句话说出 `transforms.json` 经 `load_nerf()` 后，分别落进 `NerfDataset` 的哪些字段（像素→？ 位姿→？ 全局参数→？）。

**参考要点**：

1. 目录 → `mode_from_scene` 返回 `Nerf`（见 u2-l3 规则：目录或 `.json`→Nerf）。
2. 取决于你的文件；fox 用 `fl_x`。
3. 见 4.3.4 的手算示例。
4. 自然场景初始设 `aabb_scale=128`（最大值兜底，确保背景都在盒内、不产生边界 floater），收敛后再尽量调小以提速、提分辨率。
5. 像素→`pixelmemory`（GPU）；位姿→`xforms`（经 `nerf_matrix_to_ngp` 转换）；全局参数→`scale/offset/aabb_scale/render_aabb`。

## 6. 本讲小结

- `NerfDataset` 是 NeRF 数据集的内存表示：像素/深度/光线在 GPU（`GPUMemory`），位姿与元数据列表在 CPU，外加 `scale/offset/aabb_scale/from_mitsuba` 等全局几何参数。
- `transforms.json` 与原始 NeRF 兼容：顶层是相机内参（`fl_x`/`camera_angle_x`/`k1..`/`cx,cy`/`w,h`）与 `aabb_scale`，`frames[]` 每帧含 `file_path` 与 4×4 的 `transform_matrix`。
- `load_nerf()` 用「两遍扫描 + 线程池并行加载」把 JSON 与图像变成 `NerfDataset`，焦距读取兼容三种历史写法。
- 坐标转换是非 mitsuba 数据的三步轴变换：**翻转 Y 列、翻转 Z 列、循环置换 xyz←yzx**；`nerf_position_to_ngp` 用 `(y,z,x)` 与之严格一致，且「先 scale+offset、再置换」顺序不可换。
- `aabb_scale` 是 instant-ngp 专属的关键场景参数，控制光线追踪的包围盒边长（2 的幂，最大 128），自然场景建议从 128 起步再下调。
- `colmap2nerf.py` 经 COLMAP SfM 把照片/视频变成 `transforms.json`，含重定向、居中、缩放三道坐标工序，与加载时的 `nerf_matrix_to_ngp` 是两道独立但衔接的转换。

## 7. 下一步学习建议

数据准备好了，下一讲该看「网络怎么吃这些数据」：

- **[u4-l2 NerfNetwork 双头架构](u4-l2-nerf-network-architecture.md)**：本讲产出的 `NerfDataset` 里的相机光线如何喂进密度头 + 颜色头的两段式 MLP，这是 NeRF 的核心网络结构。
- **[u3-l2 多分辨率哈希编码](u3-l2-hash-grid-encoding.md)**：本讲提到的 `aabb_scale` 会通过级联影响哈希编码最细层分辨率，回头对照 `per_level_scale` 的推导能加深理解。
- **[u4-l3 NeRF 光线步进与体渲染](u4-l3-nerf-ray-march.md)**：相机矩阵（本讲转换得到的）如何决定光线发射方向与体渲染合成。
- 若你想先动手：用 `colmap2nerf.py` 处理一段自己的视频，再用本讲的字段对照表检查产出的 `transforms.json`，是最快的巩固方式。
