# Marching Cubes 网格提取

## 1. 本讲目标

在前面的几讲里，我们一直在「渲染」NeRF 与 SDF——也就是用相机发光线、沿光线采样、把隐式场「画」成一张张像素图。但很多下游任务（3D 打印、游戏资产、物理仿真、导入 Blender/Maya）需要的不是图片，而是一个**显式的三角网格（mesh）**：一堆顶点和三角形面片。

本讲解决的就是这个「隐式 → 显式」的转换：如何把神经网络表达的密度场（NeRF）或距离场（SDF），在 GPU 上提取成一个可保存为 OBJ/PLY 的三角网格。学完后你应当掌握：

- 理解**等值面提取（isosurface extraction）**把连续标量场变成离散三角网格的原理；
- 掌握 `get_density_on_grid` 如何在网络输出上构建一个 3D 标量场；
- 看懂 Marching Cubes 算法的分辨率（`res`）与阈值（`thresh`）两个参数，以及为何默认 `thresh = 2.5`；
- 认识 `MeshState` 里网格后处理的三个旋钮：拉普拉斯平滑（smoothing）、密度吸附（density push）、法向膨胀（inflate）。

本讲承接 [u4-l3 NeRF 光线步进与体渲染](u4-l3-nerf-ray-marching.md)（NeRF 体密度场的含义）与 [u5-l1 SDF 原语与球面追踪](u5-l1-sdf-sphere-tracing.md)（SDF 距离场），不再重复它们，只讲「把场变成网格」这一步。

## 2. 前置知识

### 2.1 隐式场（implicit field）与等值面（isosurface）

NeRF 和 SDF 都不直接存三角形。它们存的是一个**连续函数** \(f(\mathbf{x})\)：

- NeRF：\(\mathbf{x}\) 是 3D 位置，\(f(\mathbf{x})\) 是该点的**体密度** \(\sigma\)（光学上代表「光在这里被吸收/散射的强度」，见 [u4-l3](u4-l3-nerf-ray-marching.md)）。
- SDF：\(\mathbf{x}\) 是 3D 位置，\(f(\mathbf{x})\) 是到表面的**有向距离** \(d\)（外正、表面为零、内负，见 [u5-l1](u5-l1-sdf-sphere-tracing.md)）。

所谓**等值面**，就是满足 \(f(\mathbf{x}) = c\) 的所有点构成的曲面，\(c\) 叫阈值（threshold）。对 SDF 取 \(c=0\) 就是物体表面；对 NeRF 取 \(c=2.5\)（默认）就是「密度等于 2.5 的那一层壳」。

### 2.2 体素化（voxelization）

神经网络是连续的，但我们要提取离散网格。做法是先在空间里铺一个 3D 网格（体素），每个格点查询一次 \(f\)，得到一个 \(N_x \times N_y \times N_z\) 的标量数组，然后对这个数组做几何处理。这把「查询 MLP」与「提取几何」解耦，非常清晰。

### 2.3 Marching Cubes 一句话直觉

把每个体素（小立方体）的 8 个角点各自判定为「内」或「外」，这 8 个二值状态共有 \(2^8=256\) 种组合。查一张预先编好的表（`triangle_table[256]`），就能知道这个体素内部该画哪些三角形、它们的顶点落在哪条边上。把所有体素拼起来就是完整网格。下面 4.2 会逐步展开。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [include/neural-graphics-primitives/marching_cubes.h](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/marching_cubes.h) | 声明分辨率推导、GPU 提取、1ring/梯度计算、`save_mesh`、GL 绘制等接口 |
| [src/marching_cubes.cu](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/marching_cubes.cu) | Marching Cubes 核心 CUDA 实现：顶点生成、面片生成、网格优化梯度、OBJ/PLY 写盘 |
| [src/testbed.cu](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu) | `compute_and_save_marching_cubes_mesh` 编排函数、GUI「Export mesh」面板 |
| [src/testbed_nerf.cu](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu) | `get_density_on_grid`（网格采样）、`marching_cubes` 包装、`optimise_mesh_step`、顶点上色 |
| [src/testbed_sdf.cu](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_sdf.cu) | `get_sdf_gt_on_grid`（用 BVH 查真值距离，仅 SDF ground truth 导出用） |
| [src/python_api.cu](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/python_api.cu) | pyngp 绑定 `compute_and_save_marching_cubes_mesh` 与 `compute_marching_cubes_mesh` |
| [scripts/run.py](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/run.py) | `--save_mesh` 命令行导出入口 |
| [include/neural-graphics-primitives/testbed.h](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h) | `MeshState` 结构、相关成员声明 |

调用关系总览：

```
run.py --save_mesh  /  pyngp /  GUI "Mesh it!"
        │
        ▼
compute_and_save_marching_cubes_mesh(filename, res, aabb, thresh)   [testbed.cu]
        │  1. 选 aabb（NeRF→render_aabb，其余→m_aabb）
        ▼
Testbed::marching_cubes(res3d, aabb, render_aabb_to_local, thresh)  [testbed_nerf.cu]
        │  2. 在网格上采样标量场
        ├──▶ get_density_on_grid(...)        ── 网络（NeRF 的 density 头 / SDF 网络）
        │  3. GPU 提取
        ├──▶ marching_cubes_gpu(...)          ── gen_vertices + gen_faces [marching_cubes.cu]
        │  4. 装配可训练顶点 + 顶点法向 + 顶点颜色
        ├──▶ compute_mesh_1ring / compute_mesh_vertex_colors
        │
        ▼
save_mesh(verts, normals, colors, indices, filename, ...)            [marching_cubes.cu]
        └──▶ 写 OBJ 或 PLY（按扩展名）
```

后续若开启 `optimize_mesh`，每帧还会走 `optimise_mesh_step` 做后处理优化。

## 4. 核心概念与源码讲解

### 4.1 网格采样：把隐式场离散成 3D 标量数组

#### 4.1.1 概念说明

Marching Cubes 的输入是一个 3D 标量场（每个体素格点一个 float）。我们手头没有这个数组，只有神经网络，所以第一步是**采样**：在包围盒里均匀铺一个 \(N^3\) 的格点网格，逐点查询网络，得到密度（NeRF）或距离（SDF）。这一步是整个流程里最「重」的部分——它要对成千上万个格点各跑一次 MLP，因此代码用批量推理（batch inference）来摊薄开销。

#### 4.1.2 核心流程

1. 由用户给的 1D 分辨率 `res` 与包围盒 `aabb` 推导出**真实 3D 分辨率** `res3d`，且每个维度向上对齐到 16 的倍数。
2. 在 `aabb` 内均匀生成 `res3d.x * res3d.y * res3d.z` 个采样位置。
3. 分批（每批 ≤ \(2^{20}\) 个点）送入网络：
   - NeRF 模式只跑**密度头**（`m_nerf_network->density()`），因为提取几何只需要密度，不需要颜色；
   - 其余模式（SDF/Image/Volume）跑整个网络 `inference_mixed_precision()`。
4. 把网络的半精度输出转成 float，并套上 `density_activation`（NeRF 默认是 `Exponential`，即 \(\sigma=\exp(\text{raw})\)），写入结果数组。

#### 4.1.3 源码精读

**分辨率推导 `get_marching_cubes_res`**：让最长边等于 `res_1d`，按比例算另两边，再各维度向上取 16 的倍数。对齐到 16 是为了让后续 CUDA 核函数的线程块（4×4×4）能整块覆盖网格、避免边界零头。

[include/neural-graphics-primitives/marching_cubes.h:24](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/marching_cubes.h#L24) 声明；实现见 [src/marching_cubes.cu:40-47](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/marching_cubes.cu#L40-L47)——把 `(aabb.max - aabb.min)` 按 `res_1d/最长边` 缩放、四舍五入、再 `next_multiple(..., 16u)`。

**网络版采样 `get_density_on_grid`**（对 NeRF 与 SDF 都适用，是网格提取真正调用的函数）：

[src/testbed_nerf.cu:3502-3557](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L3502-L3557) 关键点：

- 第 3506 行 `batch_size = min(n_elements, 1u<<20)`：每批最多约 100 万点，控制显存峰值。
- 第 3509 行按模式取输出宽度：NeRF 用密度头宽度 `padded_density_output_width()`，其余用整网宽度。
- 第 3525 行 `generate_grid_samples_nerf_uniform` 在 `aabb` 内均匀生成采样坐标。
- 第 3536–3540 行：NeRF 走 `m_nerf_network->density()`（只算密度头），其余走 `m_network->inference_mixed_precision()`。
- 第 3541–3553 行 `grid_samples_half_to_float` 把半精度转 float 并套 `density_activation`（NeRF 默认 `Exponential`，见 [testbed.h:869](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L869)）。注意它同时还参考了 `m_nerf.density_grid` 与 `max_cascade`，用于在大场景级联下取正确的密度。

**SDF 真值版采样 `get_sdf_gt_on_grid`**（**不**用于网格提取，仅用于 SDF 的 ground truth 导 PNG 切片）：

[src/testbed_sdf.cu:1550-1576](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_sdf.cu#L1550-L1576) ——它不查网络，而是用 `m_sdf.triangle_bvh->signed_distance_gpu(...)` 直接从原始三角网格查真值有向距离（BVH 来自 [u5-l2](u5-l2-mesh-bvh-octree-optix.md)）。

> 注意区分：网格提取（`marching_cubes`）永远走 `get_density_on_grid`（采样**网络**）；只有 `compute_and_save_png_slices` 在 SDF + `m_render_ground_truth` 时才走 `get_sdf_gt_on_grid`（采样**真值**）。见 [src/testbed.cu:580-582](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L580-L582) 的三元选择。

#### 4.1.4 代码实践

实践目标：理解分辨率推导与 16 对齐。

1. 打开 [src/marching_cubes.cu:40-47](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/marching_cubes.cu#L40-L47)。
2. 假设一个**非正方体**包围盒 `aabb` 最长边在 x 方向，长宽高比为 4:2:1，用户给 `res_1d = 128`。
3. 手算：`scale = 128 / 4 = 32`，于是三个维度约为 `4*32=128`、`2*32=64`、`1*32=32`，都已是 16 的倍数，故 `res3d = (128, 64, 32)`。
4. 现在把最长边改到 z 方向（长宽高 1:2:4），重算，确认 `res3d` 会变成 `(32, 64, 128)`——说明函数会自适应让最长边对齐 `res_1d`，而不是机械地用立方网格。

预期结果：理解「分辨率 256」并不意味着三个轴各 256，而是最长边为 256、其余按比例缩放。这点在窄长场景（如走廊）能显著节省采样量与显存。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `get_density_on_grid` 在 NeRF 模式下只调用 `density()` 而不跑完整网络？

参考答案：因为提取几何只需要体密度 \(\sigma\)（决定物体在哪里），不需要 RGB 颜色。`NerfNetwork` 的密度头只跑「位置编码 → 密度 MLP」两段（见 [u4-l2](u4-l2-nerf-network-architecture.md)），比整网便宜得多；颜色留到 `compute_mesh_vertex_colors` 时再单独算。

**练习 2**：`get_sdf_gt_on_grid` 与 `get_density_on_grid` 在 SDF 模式下采样的「距离」有何不同？

参考答案：`get_sdf_gt_on_grid` 查的是原始三角网格的**真值**有向距离（经 BVH），是地面真值；`get_sdf_gt_on_grid` 不经过神经网络。而 `get_density_on_grid` 在 SDF 模式下查的是**训练后的 SDF 网络**预测的距离。前者只用于评测/可视化真值切片，后者才是网格提取与正常导出所用。

---

### 4.2 Marching Cubes 等值面提取

#### 4.2.1 概念说明

拿到 \(N^3\) 的标量场后，Marching Cubes 算法逐个体素（立方体）看它 8 个角点的值：角点值 \(>\) 阈值 `thresh` 判为「实心（内）」，否则「空心（外）」。若一个体素的 8 个角点**既有内又有外**，说明等值面穿过这个体素，于是按一张 256 项的查找表生成若干三角形。把所有体素的三角形拼起来，就是等值面的三角网格。

顶点位置怎么定？对于「一侧内、一侧外」的那条边，用**线性插值**求出 \(f=\text{thresh}\) 的确切位置：

\[ t = \frac{\text{thresh} - f_0}{f_1 - f_0} \]

顶点坐标就是 \(\mathbf{p} = (1-t)\,\mathbf{c}_0 + t\,\mathbf{c}_1\)（\(\mathbf{c}_0,\mathbf{c}_1\) 是两端点坐标）。这就是源码里反复出现的 `dt = (thresh - prevf) / (nextf - prevf)`。

#### 4.2.2 核心流程

整个 GPU 提取在 `marching_cubes_gpu` 里分**两趟（two-pass）**执行（经典的「先数后填」GPU 模式，因为顶点/三角形总数事先未知）：

1. **第一趟（计数）**：`gen_vertices` 与 `gen_faces` 用空指针 `verts_out`/`indices_out` 跑一遍，仅用 `atomicAdd` 累加计数器，统计共有多少顶点、多少三角形。
2. 拷回计数器、按顶点数分配显存（并 `next_multiple(..., BATCH_SIZE_GRANULARITY)` 向上取整，为后续 mesh 优化时的网络批处理留余量）。
3. **第二趟（生成）**：再跑一遍 `gen_vertices`/`gen_faces`，这次真正写入顶点坐标与三角形索引。

`gen_vertices` 负责在每条「跨过阈值」的边上插值出顶点；`gen_faces` 负责按 8 角点掩码查 `triangle_table` 把顶点连成三角形。两者共享一张 `vertex_grid`，记录每条边对应的顶点编号，让 `gen_faces` 能找到 `gen_vertices` 生成的顶点。

#### 4.2.3 源码精读

**主入口 `marching_cubes_gpu`（两趟执行）**：

[src/marching_cubes.cu:774-803](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/marching_cubes.cu#L774-L803)：

- 第 780–784 行：分配 `vertex_grid` 工作区，大小为 `res3d.x*y*z*3*sizeof(int)`——×3 是因为每个格点对应 **3 条边**（+x、+y、+z 方向各一条），用 `memset(-1)`（即全 0xFF）初始化为「无效」。
- 第 786–787 行：线程块 `4×4×4=64` 线程，按 `div_round_up` 划分 grid。
- 第 789–790 行：**第一趟**，传 `nullptr` 只计数。
- 第 795–798 行：分配 `verts_out`（按 `BATCH_SIZE_GRANULARITY` 取整）与 `indices_out`。
- 第 801–802 行：**第二趟**，传 `vertex_grid` 与真正的输出缓冲，真正生成。注意两趟用的是不同的计数器槽位（`counters` 与 `counters+2`），避免第二趟被第一趟的残留污染。

**顶点生成 `gen_vertices`**：

[src/marching_cubes.cu:261-309](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/marching_cubes.cu#L261-L309)。每个线程处理一个格点 `(x,y,z)`，看它向 +x/+y/+z 三个邻居方向是否「跨过阈值」：

- 第 272 行 `inside = (f0 > thresh)` 判定本格点是否实心。
- 第 273–284 行（+x 方向）：若本格点与 +x 邻居一个内一个外，则在这条边上插值出一个顶点，`dt = (thresh - prevf)/(nextf - prevf)`（第 280 行），坐标为 `vec3{x+dt, y, z} * scale + offset` 再经 `transpose(render_aabb_to_local)` 转回世界坐标（第 281 行）。
- 第 285–308 行：对 +y、+z 方向同样处理。三方向的顶点编号分别写入 `vertidx_grid[idx]`、`vertidx_grid[idx+res3]`、`vertidx_grid[idx+res3*2]`（这就是工作区「×3」的由来）。
- `atomicAdd(counters,1)`（第 276 行）给顶点分配全局唯一编号；`verts_out` 为 `nullptr` 时只计数不写坐标。

**面片生成 `gen_faces` 与查找表**：

[src/marching_cubes.cu:357-698](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/marching_cubes.cu#L357-L698)。每个线程处理一个体素（8 个角点的最小角），核心是：

- 第 654–663 行：把 8 个角点逐一与 `thresh` 比较，拼出一个 8 位 `mask`（bit 0..7 对应 8 个角点）。
- 第 666 行：`if (!mask || mask==255) return;`——8 个角点全内或全外，等值面没穿过，直接跳过。
- 第 685 行 `triangles = triangle_table[mask]`：用 `mask` 查 [src/marching_cubes.cu:381-639](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/marching_cubes.cu#L381-L639) 的 `triangle_table[256][16]`，得到这一体素该画哪些三角形（每项最多 5 个三角形、15 个顶点索引，`-1` 表示结束）。
- 第 668–682 行：`local_edges[12]` 从 `vertidx_grid` 读出 12 条边各自对应的顶点编号。
- 第 689–695 行：把 `triangles` 里的边编号翻译成实际顶点索引写入 `indices_out`（`local_edges[j]-1`，减 1 是因为 `gen_vertices` 存的是 `vidx+1`，用 0 表示「无效边」）。

> 这张 `triangle_table` 注释里写明来自 PyMCubes / Paul Bourke 的经典 Marching Cubes 实现（BSD-3），是算法界几十年的标准查找表，源码把它原样编译进内核。

**外层包装 `Testbed::marching_cubes`**：

[src/testbed_nerf.cu:3614-3652](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L3614-L3652) 把上面三步串起来：

- 第 3615–3617 行：再次把 `res3d` 各维对齐到 16。
- 第 3619–3621 行：`thresh == FLT_MAX` 时回退用 `m_mesh.thresh`（默认 2.5）。
- 第 3623–3624 行：`get_density_on_grid` 采样 → `marching_cubes_gpu` 提取，结果写入 `m_mesh.verts/indices`。
- 第 3629 行：创建 `TrainableBuffer<3,1,float>`（每个顶点一个 3 维可训练参数），为可选的 mesh 优化做准备。
- 第 3637–3646 行：构造一个 Adam 优化器（lr=1e-4）。
- 第 3648–3649 行：`compute_mesh_1ring` 算每个顶点的一环邻域质心与法向（供平滑），`compute_mesh_vertex_colors` 给顶点上色。
- 第 3651 行：返回三角形数。

#### 4.2.4 代码实践

实践目标：理解 `thresh` 与掩码判定的关系。

1. 打开 [src/marching_cubes.cu:654-666](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/marching_cubes.cu#L654-L666)。
2. 假设某体素 8 个角点的密度都 `> thresh`，回答：`mask` 等于多少？会生成三角形吗？
3. 假设 4 个角点 `> thresh`、4 个 `≤ thresh`，回答：`mask` 在 `0` 与 `255` 之间吗？是否一定生成三角形？
4. 把 `thresh` 调大（比如从 2.5 调到 8.0），用直觉推断：满足 `f > thresh` 的角点会变多还是变少？等值面包围的「实心」区域变大还是变小？

预期结果：

- 全内时 `mask == 255`，第 666 行直接 return，不生成三角形。
- 一半内一半外时 `mask` 在两者之间，会查表生成三角形。
- `thresh` 越大，「实心」判定越严，等值面包围的区域越小、网格越向高密度核心收缩；`thresh` 越小（甚至负值），越能把半透明的「绒毛」也纳入网格。这就是 `thresh` 决定等值面位置的本质。

> 待本地验证：若你已编译好 instant-ngp，加载 fox 后在 GUI 的「Export mesh / volume / slices」面板里拖动「MC density threshold」滑条并反复点「Mesh it!」，能直接看到网格随阈值膨胀/收缩。

#### 4.2.5 小练习与答案

**练习 1**：`vertex_grid` 工作区为什么是 `res3d.x*y*z*3` 而不是 `*1`？

参考答案：每个格点对应 3 条「向邻居」的边（+x、+y、+z），每条边都可能产生一个顶点，需要一个槽位存它的全局顶点编号。所以三组分别放在偏移 `idx`、`idx+res3`、`idx+res3*2` 处，共 ×3。

**练习 2**：为什么用两趟（先计数、再生成）而不是一趟？

参考答案：顶点和三角形的总数在跑完所有体素前未知，而 GPU 要写出数据又必须先分配好定长缓冲。两趟法用第一趟的 `atomicAdd` 计数器确定总量、分配，第二趟再用相同逻辑真正写入。这是 GPU 上处理「变长输出」的标准做法（NeRF 的光线压缩也是同思路，见 [u4-l4](u4-l4-nerf-training-loop.md)）。

---

### 4.3 网格导出与后处理

#### 4.3.1 概念说明

`marching_cubes` 跑完后，`m_mesh` 里已经有了顶点、法向、颜色和三角形索引（都在 GPU 显存）。这一步要做两件事：

1. **导出**：把这些数组写成一个磁盘文件，支持 OBJ 与 PLY 两种格式，可选「展开（unwrap）」生成一张棋盘格纹理与 UV。
2. **后处理优化**：Marching Cubes 直接产出的网格往往有阶梯感、毛刺、半透明漂浮碎片。instant-ngp 提供一个可选的**顶点级梯度下降**，把顶点往「真正的等值面」上拉、同时做拉普拉斯平滑与法向膨胀，让网格更光滑、更贴合物体。

#### 4.3.2 核心流程

**导出编排 `compute_and_save_marching_cubes_mesh`**：

1. 若调用者没给 `aabb`（默认空），则 NeRF 用 `m_render_aabb`、其余用 `m_aabb`，并取对应的 `render_aabb_to_local` 旋转。
2. 调 `marching_cubes(res3d, aabb, render_aabb_to_local, thresh)` 提取。
3. 调 `save_mesh(...)` 写盘，传入 NeRF 的 `scale`/`offset` 用于把网格从内部归一化坐标还原到原始 NeRF 世界坐标。

**`save_mesh` 写盘**：

1. 把顶点/法向/颜色/索引从 GPU 拷回 CPU。
2. 把 NaN/Inf 替换成合理默认值（位置→0、法向→+Y、颜色→0）。
3. 按文件扩展名分流：`.ply` 走 PLY 分支，其余走 OBJ 分支。
4. 顶点坐标统一做 `(p - nerf_offset) / nerf_scale` 变换（还原到采集时的真实尺度）。
5. 可选 `unwrap_it`：生成一张棋盘格 TGA 纹理与每顶点 UV（用于在 DCC 软件里做纹理绘制）。

**网格后处理 `optimise_mesh_step`（每步）**：

1. 在当前顶点位置查询 NeRF 的密度（`m_nerf_network->density()`）与密度对位置的梯度（`input_gradient(3, ...)`，3 表示对位置求导）。
2. `compute_mesh_1ring` 重算一环质心（用于拉普拉斯平滑）。
3. `compute_mesh_opt_gradients` 合成三股梯度（见下）。
4. Adam 优化器更新顶点位置。

合成公式（[src/marching_cubes.cu:739](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/marching_cubes.cu#L739)）：

\[
\mathbf{g} = \hat{\mathbf{n}}_{\text{grad}} \cdot \mathrm{sign}(\sigma - \text{thresh}) \cdot k_{\text{density}}
+ (\mathbf{p} - \mathbf{c}_{1\text{ring}}) \cdot k_{\text{smooth}}
- \hat{\mathbf{n}}_{\text{vert}} \cdot k_{\text{inflate}}
\]

- 第一项 \(k_{\text{density}}\)：沿密度梯度方向把顶点拉向 \(\sigma=\text{thresh}\) 的等值面（\(\mathrm{sign}\) 决定方向）。
- 第二项 \(k_{\text{smooth}}\)：拉普拉斯平滑——顶点向其一环邻居的质心靠拢，消除阶梯/毛刺。
- 第三项 \(k_{\text{inflate}}\)：沿法向「膨胀」，让薄壳网格更有体积感。

#### 4.3.3 源码精读

**导出编排**：

[src/testbed.cu:537-554](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L537-L554)：

- 第 539–542 行：`aabb.is_empty()` 时按模式选默认包围盒与旋转矩阵。
- 第 543 行：调 `marching_cubes` 提取（见 4.2）。
- 第 544–553 行：调 `save_mesh`，最后两个参数是 `m_nerf.training.dataset.scale` 与 `.offset`——把网格从内部 `[0,1]^3` 坐标还原回原始 NeRF 坐标系。

**`MeshState` 结构（所有网格状态与旋钮的家）**：

[include/neural-graphics-primitives/testbed.h:591-619](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L591-L619)：

- `thresh = 2.5f`（第 592 行）：Marching Cubes 阈值，决定等值面位置。
- `res = 256`（第 593 行）：1D 分辨率基准。
- `smooth_amount = 2048.f`、`density_amount = 128.f`、`inflate_amount = 1.f`（第 595–597 行）：上面三股梯度的权重 \(k\)。
- `optimize_mesh`（第 598 行）：是否开启后处理优化。
- 一组 `GPUMemory<...>`（第 599–604 行）：verts / vert_normals / vert_colors / verts_smoothed / indices / verts_gradient。
- `trainable_verts` + `verts_optimizer`（第 605–606 行）：把顶点当作可训练参数 + Adam，支持梯度优化。

**`save_mesh` 写盘（OBJ / PLY 分流）**：

[src/marching_cubes.cu:805-955](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/marching_cubes.cu#L805-L955)：

- 第 815–829 行：GPU→CPU 拷贝，并把非有限值替换成默认值（防止 NaN 污染文件）。
- 第 870–903 行：`.ply` 分支，写 PLY 头（顶点带位置/法向/RGB、面片为三角形列表），每个顶点坐标已做 `(p - nerf_offset)/nerf_scale` 还原（第 894 行）。
- 第 904–953 行：`.obj` 分支，写 `v`（顶点，第 913 行）、`vn`（法向）、可选 `vt`（UV）与 `f`（面），并把面片顶点序号 +1（OBJ 是 1-based）。
- 第 839–863 行：`unwrap_it` 时生成一张棋盘格 `.tga` 纹理（每两个三角形一个色块），便于在 Blender 等软件里看到 UV 展开是否正确。

**网格优化梯度 `compute_mesh_opt_gradients_kernel`**：

[src/marching_cubes.cu:708-740](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/marching_cubes.cu#L708-L740)。第 739 行就是上面三股梯度的合成（密度吸附 + 平滑 + 膨胀），注意平滑项是 `src - target`（向质心走），膨胀项是沿 `-normal`（向外鼓）。

**优化主循环 `optimise_mesh_step`**：

[src/testbed_nerf.cu:3400-3456](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L3400-L3456)。每步：用当前顶点位置构造网络输入（第 3418–3427 行）→ 查密度（第 3430 行）→ 查密度对位置的梯度（第 3432 行）→ 重算一环质心（第 3434 行）→ 合成梯度（第 3437–3449 行）→ Adam 更新顶点（第 3452–3454 行）。它在 GUI 主循环里被每帧调用一次：[src/testbed.cu:3189-3190](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L3189-L3190)（`if (m_mesh.optimize_mesh) optimise_mesh_step(1)`）。

**顶点上色 `compute_mesh_vertex_colors`**：

[src/testbed_nerf.cu:3458-3500](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L3458-L3500)。NeRF 模式下，在顶点位置跑**完整网络**（这次要颜色），用 `extract_srgb_with_activation` 把 RGB 激活并转 sRGB 写入 `vert_colors`，于是导出的网格每个顶点自带顶点色。

**Python 绑定**：

- `compute_and_save_marching_cubes_mesh`（直接存盘）绑定在 [src/python_api.cu:593-594](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/python_api.cu#L593-L594)。
- `compute_marching_cubes_mesh`（返回 numpy 数组 `V/N/C/F`，不存盘）绑定在 [src/python_api.cu:606-607](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/python_api.cu#L606-L607)，实现见 [src/python_api.cu:114-142](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/python_api.cu#L114-L142)。

**run.py 命令行入口**：

[scripts/run.py:62-64](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/run.py#L62-L64) 定义三个参数：`--save_mesh`（输出路径）、`--marching_cubes_res`（默认 256）、`--marching_cubes_density_thresh`（默认 2.5）；[scripts/run.py:319-323](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/scripts/run.py#L319-L323) 调用 `testbed.compute_and_save_marching_cubes_mesh(args.save_mesh, [res, res, res], thresh=thresh)`。

#### 4.3.4 代码实践

实践目标：用 pyngp 在 Python 里提取网格并拿到 numpy 数组（不依赖磁盘快照，便于在脚本里直接处理）。

1. 先按 [u1-l3](u1-l3-build-system.md) 编译带 Python 绑定的 `pyngp`。
2. 编写下面这段「示例代码」（非项目原有脚本，仅为演示）：

```python
# 示例代码：用 pyngp 提取 fox 的网格
import pyngp as ngp

testbed = ngp.Testbed()
testbed.mode = ngp.TestbedMode.Nerf
testbed.load_training_data("data/nerf/fox")
for _ in range(1000):           # 训练若干步让密度场成型
    testbed.frame()

# 方式 A：直接存盘（等价于 run.py --save_mesh）
testbed.compute_and_save_marching_cubes_mesh(
    "fox.obj", [256, 256, 256], thresh=2.5
)

# 方式 B：拿到 numpy 数组，自己处理（trimesh、open3d 等）
mesh = testbed.compute_marching_cubes_mesh([256, 256, 256], thresh=2.5)
print("verts:", mesh["V"].shape, "faces:", mesh["F"].shape)
```

3. 运行后用任意查看器（如 `trimesh.load("fox.obj").show()`）打开 `fox.obj`，应能看到一只带顶点色的狐狸网格。

需要观察的现象：`mesh["V"]` 的形状是 `(N, 3)`、`mesh["F"]` 是 `(M, 3)`；顶点数 `N` 远小于 \(256^3\)（因为只有等值面穿过的边才有顶点）。

预期结果：成功导出 OBJ，且文件里 `v` 行带 RGB（顶点色）、`vn` 行带法向。若 `N` 几乎为 0，多半是 `thresh` 设得太高（没有角点密度超过它），可下调 `thresh` 重试。

> 待本地验证：具体顶点/三角形数取决于训练程度与 `thresh`，本环境无法实际运行，需在有 GPU 的机器上确认。

#### 4.3.5 小练习与答案

**练习 1**：导出的网格顶点坐标为什么还要做 `(p - nerf_offset) / nerf_scale`？

参考答案：instant-ngp 内部把场景归一化到 `[0,1]^3` 的「ngp 坐标系」训练（见 [u4-l1](u4-l1-nerf-dataset.md) 的 `scale`/`offset`）。但用户导出的网格要在原始采集坐标系里使用，所以 `save_mesh` 用 `scale`/`offset` 反变换，把顶点还原回真实尺度。

**练习 2**：`smooth_amount`、`density_amount`、`inflate_amount` 三个旋钮分别让网格发生什么变化？把它们都设为 0 会怎样？

参考答案：`density_amount` 把顶点吸附到真正的 \(\sigma=\text{thresh}\) 等值面（更贴合模型）；`smooth_amount` 做拉普拉斯平滑（去毛刺/阶梯）；`inflate_amount` 沿法向膨胀（增体积感）。三者都设 0 时梯度为 0，`optimise_mesh_step` 不再改变顶点，网格保持 Marching Cubes 的原始形态。

**练习 3**：为什么 SDF 模式下若用了 Takikawa 八叉树编码，GUI 会禁用「Mesh it!」按钮？

参考答案：见 [src/testbed.cu:1933-1935](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L1933-L1935)。Takikawa 编码只在表面附近定义 SDF（靠八叉树细分，见 [u5-l2](u5-l2-mesh-bvh-octree-optix.md)），整个空间并没有连续的距离场，于是 `get_density_on_grid` 在远离表面的格点会得到无意义值，Marching Cubes 无法正常工作，故直接禁用。

## 5. 综合实践

**任务**：用 `run.py` 的 `--save_mesh` 从一个 fox 的训练结果导出 OBJ，并定量理解 `marching_cubes_res` 与 `thresh` 两个参数。

操作步骤（在有 GPU + 已编译 instant-ngp 的机器上）：

1. 先训练并保存快照（若没有现成快照）：

   ```bash
   ./build/python/run.py --mode nerf --scene data/nerf/fox \
       --n_steps 1000 --save_snapshot fox.ingp
   ```

2. 从快照导出网格（用默认 `res=256`、`thresh=2.5`）：

   ```bash
   ./build/python/run.py --mode nerf --scene data/nerf/fox \
       --load_snapshot fox.ingp --save_mesh fox.obj \
       --marching_cubes_res 256 --marching_cubes_density_thresh 2.5
   ```

   终端会打印 `Resolution=[256,256,256], Density Threshold=2.5`，并在 `marching_cubes_gpu` 里打印 `#vertices=... #triangles=...`（[src/marching_cubes.cu:793](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/marching_cubes.cu#L793)）。

3. **记录两个参数并对比**：分别用 `(res=128, thresh=2.5)`、`(res=256, thresh=2.5)`、`(res=256, thresh=8.0)`、`(res=256, thresh=0.5)` 导出四份网格，记录每份的 `#triangles` 与文件大小。

需要观察的现象与预期结果：

- `res` 128→256：网格更细腻、三角形更多，但采样与显存开销约 8 倍增长（体素数 ∝ \(res^3\)）。
- `thresh` 2.5→8.0：满足 `f > thresh` 的角点变少，等值面包围的「实心」区域缩小，网格更贴向高密度核心、三角形变少；狐狸的边缘半透明绒毛会被裁掉。
- `thresh` 2.5→0.5（或负值）：等值面外扩，网格变「胖」，可能把背景里低密度的漂浮噪声也裹进来。

**关键结论——`thresh`（默认 2.5）如何决定等值面位置**：Marching Cubes 在每个体素里查的是「哪些角点密度 `> thresh`」，再用线性插值 \(t=(\text{thresh}-f_0)/(f_1-f_0)\) 求出密度恰好等于 `thresh` 的点作为顶点。也就是说，**`thresh` 就是等值面 \(f(\mathbf{x})=\text{thresh}\) 里的那个常数 \(c\)**。NeRF 的密度经 `Exponential` 激活（\(\sigma=\exp(\text{raw})\)），故 `thresh=2.5` 对应原始网络输出 \(\approx \ln(2.5)\approx 0.92\) 处的那层壳。调高 `thresh` 收紧、调低 `thresh` 外扩。

> 待本地验证：以上数值（三角形数、文件大小）依赖训练随机性与 GPU，本环境无法实跑，需在本地确认；但 `res↑⇒面片↑`、`thresh↑⇒网格收缩` 的趋势是确定的。

## 6. 本讲小结

- **网格采样**：`get_density_on_grid` 在包围盒里均匀铺 \(N^3\) 格点，批量查询网络（NeRF 只跑密度头），套 `density_activation` 得到标量场；分辨率由 `get_marching_cubes_res` 按最长边缩放并对齐到 16。
- **Marching Cubes**：`marching_cubes_gpu` 用「先计数、再生成」两趟法；`gen_vertices` 在跨阈值的边上做线性插值 \(t=(\text{thresh}-f_0)/(f_1-f_0)\) 产顶点；`gen_faces` 用 8 角点掩码查 `triangle_table[256]` 连三角形。
- **阈值 `thresh`**：就是等值面 \(f=\text{thresh}\) 的常数，默认 2.5；调高收紧、调低外扩，决定网格在密度场里的位置。
- **导出**：`save_mesh` 按扩展名写 OBJ/PLY，顶点做 `(p-offset)/scale` 还原到原始尺度，可选 unwrap 生成棋盘格纹理与 UV。
- **后处理**：`MeshState` 提供 `optimize_mesh` 开关，`optimise_mesh_step` 用密度梯度把顶点吸附到真等值面、同时做拉普拉斯平滑（`smooth_amount`）与法向膨胀（`inflate_amount`）。
- **入口**：GUI「Mesh it!」按钮、`run.py --save_mesh`、pyngp 的 `compute_and_save_marching_cubes_mesh`/`compute_marching_cubes_mesh` 三条入口最终都汇到 `Testbed::marching_cubes`。

## 7. 下一步学习建议

- 想理解导出网格时顶点色的来源，可回看 [u4-l2 NerfNetwork 双头架构](u4-l2-nerf-network-architecture.md) 的完整前向（颜色头），以及 `compute_mesh_vertex_colors` 如何复用它。
- 想深入 SDF 真值距离为何能用 BVH 快速查询，继续读 [u5-l2 网格 BVH、八叉树与 OptiX](u5-l2-mesh-bvh-octree-optix.md)，对照 `get_sdf_gt_on_grid` 里的 `signed_distance_gpu`。
- 想把网格提取集成进自动化流水线，可结合 [u7-l2 run.py](u7-l2-run-py-script.md) 写一个「训练 → 评测 PSNR → 导出网格」的端到端脚本，并用 pyngp 的 `compute_marching_cubes_mesh` 直接拿到 numpy 数组交给 trimesh/open3d 做后处理。
- 若关注体数据而非表面网格，可阅读 `get_rgba_on_grid`、`save_rgba_grid_to_png_sequence`、`save_rgba_grid_to_raw_file`（[src/marching_cubes.cu](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/marching_cubes.cu) 末尾），把整个体素场导成 PNG 序列或裸 `.bin`。
