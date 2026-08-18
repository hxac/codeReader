# DMTet：tetrahedra-sdf-grid 与 nvdiffrast 光栅化

## 1. 本讲目标

在前两讲中，我们先后读过了两种「隐式」几何：coarse-nerf 阶段的密度场（implicit-volume）与 coarse-neus 阶段的符号距离场（implicit-sdf）。它们的共同点是：**表面从不显式存在**，只存在于一个连续场的零层集里，渲染必须靠沿射线逐步采样（体渲染）。

本讲进入 geometry 阶段（配置 `configs/dreamcraft3d-geometry.yaml`），几何表示换成 **DMTet（Deep Marching Tetrahedra）**：SDF 不再由 MLP 参数化，而是**直接存储在一张四面体网格的每个顶点上**，通过 marching tetrahedra 算法显式提取出三角网格，再用 nvdiffrast 做**可微光栅化**渲染。渲染器也随之从 `nerf-volume-renderer` / `neus-volume-renderer` 家族换成 `nvdiff-rasterizer`。

学完本讲你应当能够：

1. 说清 DMTet 的数据结构：`load/tets/128_tets.npz` 里的顶点与四面体索引、三张查找表的作用。
2. 解释 marching tetrahedra 的两段式实现：**拓扑查表（不可微，在 `no_grad` 下）＋ 顶点线性插值（可微，留在 autograd 图内）**，这正是 DMTet 名字里 "Deep" 的含义。
3. 掌握 `TetrahedraSDFGrid` 的参数布局：`sdf`（Nv×1）与 `deformation`（Nv×3）都是挂在网格顶点上的可训练参数，理解 `isosurface_deformable_grid=true` 时网格顶点位移的梯度从哪里来。
4. 完整追踪 `geometry_convert_from` 的数据流：上一阶段 ckpt → `load_module_weights` 抽取 geometry 权重 → 重建 NeuS → `isosurface()` 采样 → `create_from` 把 SDF 值与外观网络搬到 DMTet 网格上。
5. 读懂 `NVDiffRasterizer.forward` 的可微光栅化管线：顶点变换、光栅化、属性插值、前景/背景合成、抗锯齿，以及只在纹理阶段启用的参考视角可见性 mask。

## 2. 前置知识

### 2.1 显式网格 vs 隐式场

| | 隐式场（u5-l1 / u5-l3） | DMTet（本讲） |
|---|---|---|
| 表面表示 | 密度/SDF 的零层集，永不显式出现 | 每次前向显式提取三角网格（`v_pos` + `t_pos_idx`） |
| 可训练参数 | MLP 权重（间接控制表面） | 网格顶点上的 SDF 值与位移（直接控制表面） |
| 渲染方式 | ray marching 逐采样点查询场 | 光栅化，每个像素只处理最先命中的表面 |
| 表面法向 | 场的梯度（有限差分/解析） | 三角面叉积后按面积加权摊到顶点 |
| 单步查询点数 | 每条射线成百上千 | 每像素 1 个表面点 |

DMTet 的优势在本项目的体现：geometry/texture 阶段分辨率升到 1024×1024（见 `configs/dreamcraft3d-geometry.yaml` 中 `height/width: 1024`），体渲染在这个分辨率下显存不可行，而光栅化只查询可见表面点，可以承受。

### 2.3 什么是 marching tetrahedra

把空间剖分成四面体，每个四面体有 4 个顶点，每个顶点上的 SDF 值有正负两种情况，因此一个四面体的「符号模式」共有 \(2^4 = 16\) 种。对每一种模式，表面（SDF 符号发生变化的地方）如何穿过该四面体是可以**预先查表**的：最多生成 2 个三角形，三角形的顶点落在「一端为正、一端为负」的边上，位置由两端 SDF 值线性插值得到。

对比更常见的 marching cubes（立方体，256 种模式）：四面体只有 16 种模式且不存在歧义情形（ambiguity），这是 DMTet 选它的原因之一。

### 2.4 什么是可微光栅化（nvdiffrast）

传统光栅化用 z-buffer 决定每个像素看到哪个三角形，这个「选谁」的决策是离散的、不可微。nvdiffrast 的做法是：**决策本身不求导，但对决策结果的计算全部可微**——三角形内部某像素的颜色是三个顶点属性的重心坐标加权（对顶点位置、顶点属性都可微），silhouette（轮廓）处用解析梯度做抗锯齿（`antialias`）。这与 DMTet 的「拓扑查表不求导、插值求导」是同一种哲学。

### 2.5 与前面讲义的衔接

- u3-l3 已讲过 `geometry_convert_from` 与 `system.weights`、`--resume` 三者互斥，本讲展开它的内部实现。
- u4-l2 已讲过参考相机矩阵以 `mvp_mtx_ref` 注入 batch，本讲看它在渲染器里的消费现场。
- u5-l3 已讲过 NeuS 的 `forward_field` / `forward_level`，本讲的 `create_from` 直接复用它们。

## 3. 本讲源码地图

| 文件 | 角色 |
|---|---|
| [threestudio/models/geometry/tetrahedra_sdf_grid.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/tetrahedra_sdf_grid.py) | DMTet 几何本体：顶点参数化的 SDF/位移场、外观特征网络、`create_from` 跨表示转换 |
| [threestudio/models/isosurface.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/isosurface.py) | 等值面提取助手：`MarchingTetrahedraHelper`（可微）与 `MarchingCubeCPUHelper`（CPU、不可微，作对照） |
| [threestudio/models/renderers/nvdiff_rasterizer.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/nvdiff_rasterizer.py) | 光栅化渲染器：mesh → clip 空间 → 光栅化 → 属性插值 → 合成 |
| [threestudio/utils/rasterize.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/rasterize.py) | 对 nvdiffrast 的薄封装：context、vertex_transform、rasterize、antialias、interpolate |
| [threestudio/models/mesh.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/mesh.py) | Mesh 数据类：惰性顶点法向/切向、边集、`normal_consistency` 与 `laplacian` 正则 |
| [threestudio/systems/base.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py) | `BaseLift3DSystem.configure`：`geometry_convert_from` 的完整调用链 |
| [threestudio/utils/misc.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/misc.py) | `load_module_weights`（按前缀抽取 state_dict）与 `find_last_path`（解析 `@LAST`） |
| [configs/dreamcraft3d-geometry.yaml](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-geometry.yaml) | geometry 阶段配置：`tetrahedra-sdf-grid` + `nvdiff-rasterizer` |
| load/tets/128_tets.npz | 预生成的四面体网格（另有 32/64 两档与 `generate_tets.py`） |

## 4. 核心概念与源码讲解

本讲拆成五个最小模块：四面体网格数据结构 → 可微等值面提取 → TetrahedraSDFGrid 本体 → create_from 跨阶段数据流 → 可微光栅化。

### 4.1 四面体网格数据结构：tets.npz 与三张查找表

#### 4.1.1 概念说明

DMTet 需要一张**预先固定**的四面体网格铺满包围盒。它与训练无关、只依赖分辨率，所以 DreamCraft3D 直接把 32/64/128 三档网格以 npz 文件形式放在 `load/tets/` 下（目录内还有 `generate_tets.py` 可再生成其他档位）。配置里 `isosurface_resolution: 128` 决定加载哪一份。

这张网格一旦加载就**永不改变拓扑**：顶点数、四面体数、连接关系全部固定；训练中能动它的只有两个挂在顶点上的张量（SDF 值与顶点位移）。

#### 4.1.2 核心流程

```text
load/tets/128_tets.npz
  ├── vertices: (Nv, 3)   四面体网格顶点坐标，范围 [0, 1]
  └── indices:  (Nt, 4)   每个四面体的 4 个顶点编号
        │
        ▼
MarchingTetrahedraHelper.__init__
  ├── register_buffer("_grid_vertices")   # 网格顶点（持久化 buffer）
  ├── register_buffer("indices")          # 四面体索引
  ├── register_buffer("triangle_table")       # 16 种符号模式 → 三角形边编号（非持久化）
  ├── register_buffer("num_triangles_table")  # 16 种符号模式 → 三角形个数（非持久化）
  └── register_buffer("base_tet_edges")       # 一个四面体的 6 条边，展平成 12 个编号（非持久化）
```

#### 4.1.3 源码精读

构造函数加载 npz 并注册为 buffer：

- [threestudio/models/isosurface.py:116-126](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/isosurface.py#L116-L126)：从 `load/tets/{resolution}_tets.npz` 读入 `vertices` 与 `indices`，注册为非持久化 buffer。注意网格顶点坐标系是 `[0, 1]`（`points_range`，见 [isosurface.py:12](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/isosurface.py#L12)），后面会统一缩放到真实包围盒。

三张查找表（都注册为非持久化 buffer）：

- [threestudio/models/isosurface.py:75-100](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/isosurface.py#L75-L100)：`triangle_table` 是 16×6 的表。行号是四面体 4 个顶点符号模式的二进制编码（第 i 个顶点为正则贡献 \(2^i\)），每行最多 6 个数、即最多 2 个三角形，每个数是「边的编号」，-1 表示空位。
- [threestudio/models/isosurface.py:101-108](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/isosurface.py#L101-L108)：`num_triangles_table` 记录每种模式产生几个三角形（0/1/2）——模式 0（全负）与 15（全正）不产生表面，1 与 14 产生 1 个三角形，其余产生 2 个。
- [threestudio/models/isosurface.py:109-114](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/isosurface.py#L109-L114)：`base_tet_edges = [0,1, 0,2, 0,3, 1,2, 1,3, 2,3]`，把一个四面体的 6 条边（用顶点对表示）展平，于是「边编号 e」等价于「顶点对 `(base_tet_edges[2e], base_tet_edges[2e+1])`」。`triangle_table` 里的数字指的就是这个编号体系。

对照：同文件的 CPU 版助手走的是完全不同的路线——

- [threestudio/models/isosurface.py:19-66](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/isosurface.py#L19-L66)：`MarchingCubeCPUHelper` 调用 PyMCubes 在 **CPU numpy** 上做 marching cubes，输入前先取负（[isosurface.py:57](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/isosurface.py#L57)），全程不可微、也不支持 deformation（[isosurface.py:53-56](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/isosurface.py#L53-L56) 直接警告并忽略）。它只在导出等离线路径有意义；DMTet 训练用的是 GPU 上纯张量运算的 `MarchingTetrahedraHelper`。

#### 4.1.4 代码实践

**实践目标**：亲手摸一次四面体网格数据，建立规模感。

**操作步骤**（示例代码，只需 numpy，不需 GPU）：

```python
# 示例代码：inspect_tets.py
import numpy as np

tets = np.load("load/tets/128_tets.npz")
vertices, indices = tets["vertices"], tets["indices"]
print("vertices:", vertices.shape, vertices.dtype)
print("indices :", indices.shape, indices.dtype)
print("顶点坐标范围:", vertices.min(axis=0), vertices.max(axis=0))
print("理论立方网格顶点数 (R+1)^3 =", (128 + 1) ** 3)
```

**需要观察的现象**：`vertices.shape[0]` 与 `(R+1)^3` 是否一致；坐标是否落在 `[0, 1]`；`indices.shape[0]` 与顶点数的比例（每个立方体被剖成几个四面体）。

**预期结果**：顶点数应等于 \((R+1)^3\)（128 分辨率约 214 万），坐标范围 [0,1]；四面体数约为顶点数的 5～6 倍。具体数值待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `triangle_table` 是 16 行而不是 256 行？
**答案**：四面体只有 4 个顶点，每个顶点符号两种取值，共 \(2^4=16\) 种模式；256 是 marching cubes（8 个体素顶点，\(2^8\)）的情形。模式数少、且无歧义情形，正是用四面体做剖分的工程优势。

**练习 2**：`triangle_table` 的行 0 和行 15 为什么全是 -1？
**答案**：行 0 = 四个顶点 SDF 全为负，行 15 = 全为正；两种情况四面体内部没有符号变化，表面不穿过它，自然没有三角形（对应 `num_triangles_table` 的 0）。

**练习 3**：`_grid_vertices`、`indices` 注册为 buffer 但 `persistent=False`，这意味着什么？
**答案**：它们会随 `.to(device)` 移动、能被模块引用，但**不写入 state_dict**——因为网格完全由 `load/tets/` 的 npz 文件与分辨率决定，存进 ckpt 是纯冗余；加载 ckpt 时由 `configure()` 重新从磁盘构建。

### 4.2 MarchingTetrahedraHelper：可微的等值面提取

#### 4.2.1 概念说明

这是 DMTet 的核心算法模块。它要解决的问题是：**从「网格顶点上的 SDF 值 + 可选顶点位移」出发，产出一张可回传梯度的三角网格**。

关键设计是梯度友好的两段式切分：

- **离散决策不求导**：哪些四面体被表面穿过、生成哪些新顶点、连成哪些三角形——这些是符号模式的函数，是离散的、不可微的，全部包在 `torch.no_grad()` 里。
- **连续计算求导**：新顶点的**位置**是两端 SDF 值与两端网格顶点坐标的线性函数——这部分留在 autograd 图内。

于是损失对表面位置的梯度，可以沿着「插值公式」分解为对 SDF 参数与对 deformation 参数的梯度——这就是「Deep」Marching Tetrahedra。

#### 4.2.2 核心流程

```text
输入: level (Nv,1)  SDF 值;  deformation (Nv,3) 或 None  顶点位移
  │
  ├─ forward: 若有 deformation，先归一化并加到网格顶点上
  │     grid_vertices' = grid_vertices + Δv,  Δv = (1/R)·tanh(deformation)
  │
  └─ _forward(grid_vertices', level, indices):
       1. [no_grad] occ = level > 0；占用数 0<sum<4 的四面体为"跨界四面体"
       2. [no_grad] 收集跨界四面体的 6 条边 → 去重 → 筛出"一正一负"的穿越边
       3. [可微]  对每条穿越边做线性插值求表面点 v
       4. [no_grad] tetindex = Σ occ_i·2^i 查表得三角形个数与边编号
       5. [no_grad] gather 出 faces（顶点为步骤 3 的插值点编号）
  │
  ▼
输出 Mesh，extras 携带 grid_vertices / tet_edges / grid_level / grid_deformation
```

插值公式（设边两端为 \(p_0, p_1\)，SDF 值 \(s_0, s_1\) 异号）：

\[
v \;=\; \frac{s_1\,p_0 - s_0\,p_1}{s_1 - s_0}
\]

验证两点性质：若 \(s_0 = 0\)，则 \(v = p_0\)；若 \(s_0 = -a,\ s_1 = b\ (a,b>0)\)，则 \(v = \frac{b\,p_0 + a\,p_1}{a+b}\) 是凸组合——穿越点必然落在两端点之间。且 \(\partial v/\partial s_0\)、\(\partial v/\partial s_1\)、\(\partial v/\partial p\) 都存在，梯度可以回传。

#### 4.2.3 源码精读

先看 `forward` 入口与位移归一化：

- [threestudio/models/isosurface.py:229-241](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/isosurface.py#L229-L241)：`forward` 若收到 deformation，先把网格顶点更新为 `grid_vertices + normalize_grid_deformation(deformation)`，再调 `_forward`。
- [threestudio/models/isosurface.py:130-137](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/isosurface.py#L130-L137)：位移归一化为 \(\frac{1}{R}\tanh(d)\)（`points_range` 跨度为 1）。tanh 把每个顶点的位移限制在约一个四面体尺寸内——顶点只能「微调」而不能乱跑，否则会与固定的四面体拓扑脱节（表面判定仍基于原拓扑的符号模式）。代码中标注了 FIXME：这个激活是硬编码的。

再看 `_forward` 的三段：

- **拓扑判定（no_grad）**：[threestudio/models/isosurface.py:168-194](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/isosurface.py#L168-L194)。`occ_n = sdf_n > 0` 把每个顶点标记为正/负；`occ_sum` 在 1~3 之间的四面体是跨界四面体（[isosurface.py:173](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/isosurface.py#L173)）。随后收集这些四面体的边、`sort_edges` 规范端点顺序、`torch.unique` 去重，并用「边两端恰好一正一负」（[isosurface.py:182](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/isosurface.py#L182)）筛出**穿越边**——表面点只会诞生在这些边上。`idx_map` 把边编号映射到新顶点编号。
- **可微插值**：[threestudio/models/isosurface.py:195-202](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/isosurface.py#L195-L202)。这一段**不在 no_grad 内**：把穿越边两端的坐标与 SDF 值取出，第二端 SDF 取负后做归一化加权求和，得到新顶点 `verts`。用上一小节的公式对照代码：`edges_to_interp_sdf` 先做 `[:, -1] *= -1`，`denominator` 为两端（取负后）之和，`flip` 后相除得到两个权重，最后 `(edges_to_interp * weights).sum(1)` 就是凸组合。`verts` 对 `sdf`（参数）与 `grid_vertices`（含 deformation 贡献）都可微。
- **查表连三角形（no_grad）**：[threestudio/models/isosurface.py:204-225](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/isosurface.py#L204-L225)。`tetindex = Σ occ_i·2^i` 是 4 位二进制模式编码；`num_triangles_table[tetindex]` 给出该四面体产生几个三角形；再用 `torch.gather` 按 `triangle_table[tetindex]` 里记录的边编号，从 `idx_map` 中取出对应新顶点的编号，拼出 `faces`（形状 Nf×3）。产生的三角形分 1 个与 2 个两批 gather 后 concat。

最后打包成 Mesh：

- [threestudio/models/isosurface.py:243-252](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/isosurface.py#L243-L252)：Mesh 的 `extras` 里带上了 `grid_vertices`（位移后的）、`tet_edges`（全部去重边，供 `normal_consistency` 用）、`grid_level`（就是输入的 SDF 值）、`grid_deformation`。**这四个键是下一模块 `create_from` 的搬运来源**，请记住它们。

#### 4.2.4 代码实践

**实践目标**：用最小例子验证「查表拓扑 + 线性插值」两段逻辑，确认插值点落在边内、权重和为 1。

**操作步骤**（示例代码，纯 numpy 复刻 [isosurface.py:195-202](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/isosurface.py#L195-L202) 的插值段）：

```python
# 示例代码：verify_interp.py
import numpy as np

p0, p1 = np.array([0.0, 0.0, 0.0]), np.array([1.0, 0.0, 0.0])
s0, s1 = -0.3, 0.7                     # 一负一正，边是穿越边

E = np.stack([np.stack([p0, p1])])     # (1, 2, 3)
S = np.array([[[s0], [s1]]])           # (1, 2, 1)
S[:, -1] *= -1.0                       # 复刻代码：第二端取负
denominator = S.sum(1, keepdims=True)
W = np.flip(S, axis=1) / denominator   # 权重
v = (E * W).sum(1)                     # 插值点

print("权重:", W.ravel(), "权和 =", W.sum())
print("插值点:", v, "s0 处的解析解 =", s1 / (s1 - s0) * p0 + (-s0) / (s1 - s0) * p1)
```

**需要观察的现象**：两个权重之和恒为 1；插值点的 x 坐标等于 \(\frac{-s_0}{s_1-s_0} = 0.3\)，即从 p0 向 p1 走 30%（p0 内部 0.3 单位、p1 外部 0.7 单位，比例正确）。

**预期结果**：`v ≈ [0.3, 0, 0]`。再把 `s0` 改成 0 复跑，`v` 应精确等于 `p0`。待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `occ_n = sdf_n > 0`、边筛选、面片查表必须放在 `no_grad` 里，而插值不行？
**答案**：前三者是「符号模式 → 拓扑」的离散映射，导数几乎处处为 0、在符号翻转处无定义，对它们求导没有意义；而顶点位置是 SDF 值与网格坐标的连续函数，梯度存在且正是我们需要的（∂loss/∂sdf、∂loss/∂deformation 都经由它回传）。放进 `no_grad` 只切断前者、保留后者。

**练习 2**：`isosurface_deformable_grid` 的位移为什么要乘 `tanh` 并除以分辨率？
**答案**：marching tetrahedra 的拓扑判定基于固定四面体连接关系；若顶点位移超过约一个四面体尺寸，实际的符号分布与拓扑会脱节，产生翻转/破面。`tanh` 限制幅值、`1/R` 把幅度标定到「局部一个格子」的量级（代码注释：half tet size is approximately 1/self.resolution）。

**练习 3**：`num_triangles_table` 为什么最多是 2？
**答案**：一个四面体被平面切出的截面最多是四边形，四边形三角化恰好 2 个三角形；符号模式 1/14（单点异号）截面是三角形，只需 1 个。

### 4.3 TetrahedraSDFGrid：顶点参数化的 SDF、位移场与外观网络

#### 4.3.1 概念说明

`TetrahedraSDFGrid` 继承自 `BaseExplicitGeometry`（显式几何基类）。它与前两讲的隐式几何有本质区别：**可训练的几何参数不再是 MLP 权重，而是直接挂在四面体网格顶点上的两个张量**：

- `sdf`：形状 (Nv, 1)，每个网格顶点一个 SDF 值——128 分辨率下约 214 万个直接可优化参数；
- `deformation`：形状 (Nv, 3)，每个网格顶点一个三维位移（`isosurface_deformable_grid=true` 时）。

同时它保留了 u5-l1 的「外观侧」结构：一个 HashGrid 位置编码 + 一个小 MLP，把任意 3D 点映射为 3 维特征，交给 `no-material` 转 RGB。也就是说，**几何头（密度头/SDF 头）消失了，外观头保留**——几何由顶点参数直接表达。

#### 4.3.2 核心流程

```text
configure()
  ├── isosurface_bbox ← self.bbox（后续会被 create_from 覆盖为上一阶段的紧凑包围盒）
  ├── isosurface_helper ← MarchingTetrahedraHelper(128, load/tets/128_tets.npz)
  ├── sdf: nn.Parameter (Nv,1) 零初始化        ┐ fix_geometry=true 时
  ├── deformation: nn.Parameter (Nv,3) 零初始化 ┘ 改注册为 buffer
  ├── encoding: HashGrid（外观）
  └── feature_network: VanillaMLP → 3 维特征（外观）

每次渲染前向:
  isosurface() → helper(sdf, deformation) → v_pos 缩放到 isosurface_bbox → Mesh
  forward(points) → contract_to_unisphere → encoding → feature_network → {"features"}
```

梯度流（`isosurface_deformable_grid=true` 时 deformation 参与训练的两条路径）：

```text
loss（渲染/正则）
  │
  ▼
mesh.v_pos（= 穿越边两端 (grid_vertices + Δv) 的凸组合，权重由 sdf 决定）
  ├── 对 sdf 求导:  ∂v/∂s0、∂v/∂s1      → sdf 参数收到梯度
  └── 对位置求导:   ∂v/∂p0、∂v/∂p1
                      └── p = grid_vertices + (1/R)tanh(deformation)
                            → deformation 参数收到梯度
```

#### 4.3.3 源码精读

- [threestudio/models/geometry/tetrahedra_sdf_grid.py:25-32](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/tetrahedra_sdf_grid.py#L25-L32)：注册名 `tetrahedra-sdf-grid`（geometry/texture 两份 yaml 的 `geometry_type` 都指向它），配置含 `isosurface_resolution=128`、`isosurface_deformable_grid=True`、`isosurface_remove_outliers=False`。
- [threestudio/models/geometry/tetrahedra_sdf_grid.py:65-75](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/tetrahedra_sdf_grid.py#L65-L75)：`configure` 先注册 `isosurface_bbox` buffer（初值等于 `self.bbox`，即 radius=2.0 的立方体——见 [dreamcraft3d-geometry.yaml:48-49](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-geometry.yaml#L48-L49) 的 `radius: 2.0 # consistent with coarse`），再构建 `MarchingTetrahedraHelper`。注意 `isosurface_bbox` 与基类的 `bbox` 是两个变量：前者管等值面提取的值域，后者管外观网络的输入归一化。
- [threestudio/models/geometry/tetrahedra_sdf_grid.py:80-98](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/tetrahedra_sdf_grid.py#L80-L98)：核心参数注册。`fix_geometry=False`（geometry 阶段默认）时 `sdf` 与 `deformation` 都是 `nn.Parameter`、零初始化；`isosurface_deformable_grid=True` 才创建 `deformation`，否则置 None。
- [threestudio/models/geometry/tetrahedra_sdf_grid.py:99-113](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/tetrahedra_sdf_grid.py#L99-L113)：`fix_geometry=True`（texture 阶段，[dreamcraft3d-texture.yaml:59](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml#L59)）时同样两个张量改注册为 buffer——不再是参数、不进优化器，但仍随 state_dict 保存/加载，纹理阶段因此只训外观。
- [threestudio/models/geometry/tetrahedra_sdf_grid.py:115-123](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/tetrahedra_sdf_grid.py#L115-L123)：外观侧——`HashGrid` 编码（[tetrahedra_sdf_grid.py:36-45](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/tetrahedra_sdf_grid.py#L36-L45) 的默认配置，与 u5-l1 的 ProgressiveBandHashGrid 不同，这里就是普通 HashGrid）+ VanillaMLP 输出 3 维特征。
- [threestudio/models/geometry/tetrahedra_sdf_grid.py:237-248](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/tetrahedra_sdf_grid.py#L237-L248)：`isosurface()` 调 helper 提取网格，随后把 `v_pos` 从 [0,1] 缩放到 `isosurface_bbox`（[tetrahedra_sdf_grid.py:242-244](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/tetrahedra_sdf_grid.py#L242-L244)）。两个细节：`fix_geometry=True` 时缓存 mesh 直接返回（[tetrahedra_sdf_grid.py:239-240](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/tetrahedra_sdf_grid.py#L239-L240)，几何冻结后每次提取结果相同，省一次全网格计算）；`isosurface_remove_outliers` 默认 False（与隐式几何基类默认 True 不同，DMTet 的网格是可微的，`remove_outlier` 只对不可微网格生效，见 [mesh.py:31-34](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/mesh.py#L31-L34)）。
- [threestudio/models/geometry/tetrahedra_sdf_grid.py:250-264](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/tetrahedra_sdf_grid.py#L250-L264)：`forward` 只做外观查询：`contract_to_unisphere` 归一化 → encoding → feature_network → 返回 `{"features": ...}`。注意 [tetrahedra_sdf_grid.py:255-257](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/tetrahedra_sdf_grid.py#L255-L257) 明确禁止 `output_normal`——**DMTet 的法向来自三角面叉积（mesh.py），而不是场梯度**，这与隐式几何形成鲜明对比（u5-l1 的有限差分法向、u5-l3 的解析梯度法向都不复存在）。

#### 4.3.4 代码实践

**实践目标**：量化「参数从 MLP 搬到顶点」的规模变化，并验证 texture 阶段冻结逻辑。

**操作步骤**：

1. 通读 [tetrahedra_sdf_grid.py:80-113](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/tetrahedra_sdf_grid.py#L80-L113)，手算两个数（用 4.1 实践打印出的 Nv）：
   - `sdf` 参数量 = Nv；
   - `deformation` 参数量 = 3×Nv。
2. 对照 [configs/dreamcraft3d-texture.yaml:59](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml#L59)（`fix_geometry: true`）与 [configs/dreamcraft3d-geometry.yaml:46-50](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-geometry.yaml#L46-L50)（未设该键 → 默认 False），在纸上分别列出两阶段 `geometry.parameters()` 里包含哪些名字。

**需要观察的现象**：128 分辨率下 `sdf` + `deformation` 合计约 \(4 \times 214\) 万 ≈ 858 万参数，远大于外观网络（HashGrid 19 位表 ×16 层 ×2 特征 + 一个 64×1 隐层 MLP）。

**预期结果**：geometry 阶段 `sdf`、`deformation`、`encoding`、`feature_network` 全部可训；texture 阶段 `sdf`/`deformation` 变为 buffer，只剩外观侧参数可训。若在本地装好环境，可用 `sum(p.numel() for p in system.geometry.parameters())` 打印验证（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：DMTet 阶段法向为什么不能再用有限差分算？
**答案**：有限差分/解析法向依赖一个连续可导的场函数；DMTet 的 SDF 只定义在网格顶点上，顶点之间没有函数可查。表面法向改由提取出的三角形计算：面法向 = 叉积，顶点法向 = 邻接面法向按面积加权平均（[mesh.py:134-160](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/mesh.py#L134-L160)）。

**练习 2**：`fix_geometry=True` 时为什么还要注册 `sdf`/`deformation`（作为 buffer）而不是干脆删掉？
**答案**：纹理阶段仍需要**渲染**几何——`isosurface()` 每次前向（缓存前第一次）仍要靠这两个张量提取 mesh；改成 buffer 只是退出优化器，state_dict 依旧保存它们，`geometry_convert_from`/`system.weights` 加载时才能恢复正确的形状。

**练习 3**：`isosurface()` 里 `scale_tensor(mesh.v_pos, points_range, self.isosurface_bbox)` 这步为什么必须存在？
**答案**：helper 全程工作在 [0,1] 归一化坐标系（npz 里的顶点就在 [0,1]）；提取出的 v_pos 也是 [0,1]。而渲染器、外观查询、损失都在世界坐标系，必须用 `isosurface_bbox`（几何真正的包围盒）把顶点映射回去。

### 4.4 create_from：从上一阶段检查点初始化 DMTet 的完整数据流

#### 4.4.1 概念说明

geometry 阶段的启动命令（[README.md:118-120](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/README.md#L118-L120)）：

```bash
ckpt=outputs/dreamcraft3d-coarse-neus/$prompt@LAST/ckpts/last.ckpt
python launch.py --config configs/dreamcraft3d-geometry.yaml --train \
    system.prompt_processor.prompt="$prompt" data.image_path="$image_path" \
    system.geometry_convert_from="$ckpt"
```

配置里对应 [configs/dreamcraft3d-geometry.yaml:44-45](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-geometry.yaml#L44-L45)：`geometry_convert_from: ???`（命令行注入）与 `geometry_convert_inherit_texture: true`。

这条链路要解决的问题是：**NeuS 的几何在 MLP 权重里，DMTet 的几何在网格顶点上，两种表示无法直接 load_state_dict**。`create_from` 的做法是：先用旧配置重建旧几何 → 让旧几何自己跑一次 `isosurface()` 把场「实例化」到一张 MT 网格上 → 把网格上的 SDF 值、位移、紧凑包围盒直接抄给新几何，外观网络则按 state_dict 整体拷贝。

#### 4.4.2 核心流程

完整数据流（系统侧 + 几何侧）：

```text
system.geometry_convert_from = ".../coarse-neus/$prompt@LAST/ckpts/last.ckpt"
  │
  ▼ BaseLift3DSystem.configure()                       [systems/base.py]
  1. find_last_path: "…@LAST…" → 按字典序选最新时间戳目录   [misc.py:138-152]
  2. 三者互斥检查: geometry_convert_from 且无 weights 且非 resumed
  3. load_config(上一 trial 的 configs/parsed.yaml) → prev_system_cfg
  4. prev_geometry_cfg.update(geometry_convert_override)
  5. prev_geometry = find("implicit-sdf")(prev_geometry_cfg)   # 重建 NeuS
  6. load_module_weights(ckpt, module_name="geometry")         # 只抽 geometry.* 前缀
  7. prev_geometry.load_state_dict(..., strict=False)
  8. prev_geometry.do_update_step(epoch, global_step, on_load_weights=True)
  9. prev_geometry.to(device)
 10. self.geometry = find("tetrahedra-sdf-grid").create_from(
        prev_geometry, self.cfg.geometry, copy_net=True)
  │
  ▼ TetrahedraSDFGrid.create_from（ImplicitSDF 分支）   [tetrahedra_sdf_grid.py]
  a. 强制旧几何 isosurface_method="mt"、分辨率对齐（带警告）
  b. mesh = other.isosurface()        # 两遍提取: 全 bbox → 紧凑 bbox（+10% 边距）
  c. instance.isosurface_bbox ← mesh.extras["bbox"]          # 紧凑包围盒
  d. instance.sdf.data    ← mesh.extras["grid_level"]        # 顶点上的 SDF 值
  e. instance.deformation ← mesh.extras["grid_deformation"]  # 双方都 deformable 才有
  f. encoding / feature_network.load_state_dict(other.….)    # 外观网络整体继承
```

其中步骤 b 的「两遍提取」来自隐式几何基类（[geometry/base.py:171-188](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/base.py#L171-L188)）：第一遍在完整 bbox 上粗提取，得到表面的 min/max 后外扩 10% 得到**紧凑包围盒**，第二遍在紧凑盒内重新提取。紧凑盒随后成为 DMTet 的 `isosurface_bbox`——**同样 128³ 档的四面体网格，全部分辨率都铺在物体附近而不是整个 [-2,2]³ 立方体**，这是 DMTet 在本项目里能表现出细节的关键一步。

#### 4.4.3 源码精读

系统侧（调用链）：

- [threestudio/systems/base.py:243-250](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L243-L250)：`configure` 开头先用 `find_last_path` 解析 `geometry_convert_from` 与 `weights` 里的 `@LAST` 通配（实现见 [misc.py:138-152](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/misc.py#L138-L152)：列出同前缀目录、按字典序倒序取第一个），随后三条件互斥检查——u3-l3 讲过的「convert / weights / resume 优先级」的代码现场。
- [threestudio/systems/base.py:254-267](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L254-L267)：加载上一 trial 的 `parsed.yaml`（u2-l4 讲过的配置快照，路径硬编码为 `../configs/parsed.yaml`），解析出上一阶段的 `geometry_type`（coarse-neus 是 `implicit-sdf`）与 geometry 配置，套上 `geometry_convert_override` 后经注册机制重建旧几何实例。**旧几何是用旧配置、新代码重建的**，所以快照必须忠实。
- [threestudio/systems/base.py:268-275](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L268-L275)：`load_module_weights(..., module_name="geometry")` 从 ckpt 里只抽 `geometry.` 前缀的键——实现见 [misc.py:32-62](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/misc.py#L32-L62)，用正则 `^geometry\.(.*)$` 剥掉前缀后返回，同时返回 ckpt 里记录的 epoch/global_step。`strict=False` + `do_update_step(on_load_weights=True)` 与 u3-l3/l2 讲过的权重加载三件套一致：恢复渐进状态（如哈希编码已解锁层级），否则旧 MLP 的行为会和训练结束时不一致。
- [threestudio/systems/base.py:278-284](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L278-L284)：核心一行——`find(self.cfg.geometry_type).create_from(prev_geometry, self.cfg.geometry, copy_net=self.cfg.geometry_convert_inherit_texture)`。`copy_net` 来自 yaml 的 `geometry_convert_inherit_texture: true`，决定是否把外观网络一并搬过来。转换完 `del prev_geometry; cleanup()` 释放旧几何。

几何侧（`TetrahedraSDFGrid.create_from`，注意它是 `@staticmethod` + `@torch.no_grad()`，见 [tetrahedra_sdf_grid.py:266-273](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/tetrahedra_sdf_grid.py#L266-L273)）按源类型分三个分支：

- **ImplicitSDF 分支（coarse-neus → geometry 实际走的分支）**：[tetrahedra_sdf_grid.py:320-348](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/tetrahedra_sdf_grid.py#L320-L348)。
  - [L322-331](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/tetrahedra_sdf_grid.py#L322-L331)：强制旧几何 `isosurface_method="mt"` 且分辨率与新几何一致（不一致就地改旧配置并打警告）——因为搬运依赖 Mesh.extras，而只有 MT 助手才会写这些 extras（4.2 精读末尾的那四个键）。
  - [L332](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/tetrahedra_sdf_grid.py#L332)：`mesh = other.isosurface()`——旧 NeuS 把自己的 SDF 在（与目标相同规格的）MT 网格顶点上评估一遍，这一步就是「隐式场 → 网格值」的采样。
  - [L333-334](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/tetrahedra_sdf_grid.py#L333-L334)：`isosurface_bbox ← extras["bbox"]`（紧凑盒）、`sdf.data ← extras["grid_level"]`（顶点 SDF 值，无取负、无 clamp——源与目标共用同一套符号约定与同一 MT 助手，语义原样传递）。
  - [L335-342](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/tetrahedra_sdf_grid.py#L335-L342)：仅当**双方**都 `isosurface_deformable_grid` 才搬 `grid_deformation`。注意 coarse-neus 的 yaml 未设该项，`ImplicitSDF` 用基类默认 False（[geometry/base.py:64](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/base.py#L64)，[configs/dreamcraft3d-coarse-neus.yaml:42-44](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-neus.yaml#L42-L44) 只设了 radius），所以 DreamCraft3D 实际流水线里 **deformation 从零开始学，跨阶段继承的是 sdf、bbox 与外观网络**。
  - [L343-347](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/tetrahedra_sdf_grid.py#L343-L347)：`copy_net=True` 时把 `encoding` 与 `feature_network` 的 state_dict 整体拷入——纹理（外观）由此跨表示继承，新阶段不必重学颜色。
- **ImplicitVolume 分支（备用）**：[tetrahedra_sdf_grid.py:297-319](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/tetrahedra_sdf_grid.py#L297-L319)。密度场的 `forward_level` 是 `threshold - density`（[implicit_volume.py:224-227](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/implicit_volume.py#L224-L227)），量纲与 SDF 不同，所以搬运时多了 `.clamp(-1, 1)`（[tetrahedra_sdf_grid.py:311-313](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/tetrahedra_sdf_grid.py#L311-L313)）把极端值压回 SDF 的常见范围。
- **TetrahedraSDFGrid → TetrahedraSDFGrid 分支（geometry → texture）**：[tetrahedra_sdf_grid.py:274-296](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/tetrahedra_sdf_grid.py#L274-L296)。README 的 Stage 3 命令（[README.md:123-125](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/README.md#L123-L125)）同样传 `geometry_convert_from`，同表示之间直接 `clone` sdf/deformation 数据与 bbox（要求分辨率一致），再按 `copy_net` 拷外观网络。texture 阶段 `fix_geometry: true`，转入即冻结。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：把 4.4.2 的数据流在纸面完整走一遍，并解释 `isosurface_deformable_grid=true` 时网格顶点如何参与训练。

**操作步骤**：

1. **读命令**：对照 [README.md:118-125](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/README.md#L118-L125)，写出 Stage 2 与 Stage 3 各自的 `geometry_convert_from` 指向哪个 trial 的哪个 ckpt，标出源几何类型与目标几何类型。
2. **追代码**：从 [systems/base.py:244](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L244) 开始，为 4.4.2 流程图的每一步标注「文件:行号」，特别确认三处：
   - `load_module_weights` 如何用正则只保留 `geometry.*`（[misc.py:54-60](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/misc.py#L54-L60)）；
   - `mesh.extras` 的四个键分别在 [geometry/base.py:158-162](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/base.py#L158-L162) 与 [isosurface.py:243-252](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/isosurface.py#L243-L252) 的哪一行产生、在 [tetrahedra_sdf_grid.py:333-342](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/tetrahedra_sdf_grid.py#L333-L342) 的哪一行被消费；
   - 为什么 [systems/base.py:275](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L275) 必须在 `isosurface()` 之前调用 `do_update_step(on_load_weights=True)`。
3. **写梯度链**（文字版）：从「lambda_normal_consistency 对 mesh.v_pos 的惩罚」出发，写出梯度如何流回 `deformation`（提示：经过 4.3.2 的两条路径，先到 `v_pos`，再经过插值点的位置项到 `grid_vertices + (1/R)tanh(deformation)`）。
4. （可选，需本地环境）在 [configs/dreamcraft3d-geometry.yaml](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-geometry.yaml) 上用命令行覆盖 `system.geometry.isosurface_deformable_grid=false` 短跑数百步，与默认配置对比表面细节。

**需要观察的现象**：步骤 1 应得出「Stage 2: implicit-sdf → tetrahedra-sdf-grid（跨表示）；Stage 3: tetrahedra-sdf-grid → tetrahedra-sdf-grid（同表示，随后 fix_geometry 冻结）」。步骤 4 若可运行，预期关掉 deformable grid 后薄结构与尖锐边缘的刻画变弱（待本地验证）。

**预期结果**：能独立画出从 `last.ckpt` 到 `instance.sdf.data` 的完整箭头图，并说清 deformation 的梯度来源 = 渲染/网格正则损失 → `mesh.v_pos` → 插值位置项 → `tanh(deformation)/R`。

#### 4.4.5 小练习与答案

**练习 1**：为什么不直接 `load_state_dict` 把 NeuS 的权重灌进 DMTet？
**答案**：两者参数空间完全不同——NeuS 的几何在 `sdf_network` MLP 权重里（隐式、分辨率无关），DMTet 的几何在固定 MT 网格顶点的 `sdf` 张量里（显式、绑定网格拓扑）。键名与形状都对不上，必须经过「旧几何自己评估一次等值面」的采样桥接。

**练习 2**：`create_from` 为什么整体套 `@torch.no_grad()`？
**答案**：它是初始化代码，只在 `configure` 阶段执行一次；采样出的 sdf 初值是数据不是计算结果，不需要也不应该建立 autograd 图，`no_grad` 还能省下采样期间的显存。

**练习 3**：如果上一阶段的 `parsed.yaml` 快照丢了，会发生什么？
**答案**：[systems/base.py:254-259](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L254-L259) 的 `load_config` 会因找不到 `../configs/parsed.yaml` 直接失败——这正是 u2-l4 强调的「parsed.yaml 固化 timestamp、与 ckpt 同目录保存」的意义：跨阶段接力依赖配置快照与 ckpt 成对存在。

### 4.5 NVDiffRasterizer：可微光栅化前向与网格正则

#### 4.5.1 概念说明

`nvdiff-rasterizer` 是 DMTet 的渲染搭档（geometry/texture 两阶段的 `renderer_type`，见 [configs/dreamcraft3d-geometry.yaml:58-60](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-geometry.yaml#L58-L60)）。它对 nvdiffrast 做薄封装，把一张 Mesh 按给定相机渲染成：

- `opacity`：抗锯齿的可见性 mask；
- `comp_normal` / `comp_normal_viewspace`：世界系/视线系法向图（geometry 阶段 rgb/normal 交替渲染的法向来源）；
- `comp_rgb` / `comp_rgb_bg`：前景/背景颜色图；
- `mesh`：把本次提取的 Mesh 原样透传给系统，供网格正则消费；
- `mask`（仅 texture 阶段）：当前视角可见但**参考视角不可见**的区域。

与体渲染的本质区别：体渲染每条射线采样上百个点查询场；光栅化先用 z-buffer 找到每像素命中的三角形，再只在**可见表面点**上查询外观网络——1024 分辨率可行的原因。

#### 4.5.2 核心流程

```text
forward(mvp_mtx, camera_positions, light_positions, H, W, render_rgb, render_mask)
  │
  1. mesh = geometry.isosurface()                       # 每次前向重新提取
  2. v_pos_clip = vertex_transform(mesh.v_pos, mvp_mtx) # 可微: v_pos 与 mvp 都有梯度
  3. rast = rasterize(v_pos_clip, t_pos_idx, (H, W))    # (B,H,W,4)=[u,v,z,三角形号]
  4. mask = rast[...,3:] > 0;  opacity = antialias(mask)
  5. 法向: interpolate(v_nrm) → 归一化 → mask 内外 lerp → antialias
  6. render_rgb:
       selector = mask 展平
       gb_pos = interpolate(v_pos)                      # 可微的表面点
       geo_out = geometry(gb_pos[selector])             # 只查可见表面点!
       rgb_fg  = material(viewdirs, positions, light, features)
       rgb = lerp(background(viewdirs), rgb_fg, mask) → antialias
  7. render_mask(仅 texture): 用 mvp_mtx_ref 再光栅化一次 → 参考视角可见面标记
       → 插值回当前视角 → out["mask"] = 1 - 可见
```

#### 4.5.3 源码精读

**封装层**（[threestudio/utils/rasterize.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/rasterize.py)）：

- [rasterize.py:7-20](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/rasterize.py#L7-L20)：按 `context_type` 建立OpenGL（`gl`）或 CUDA（`cuda`）光栅化上下文。geometry/texture 配置都写 `context_type: cuda`（[dreamcraft3d-geometry.yaml:60](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-geometry.yaml#L60)），呼应 u1-l2 讲过的「无显示环境（Docker/无头）下用 cuda context」。
- [rasterize.py:22-28](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/rasterize.py#L22-L28)：`vertex_transform` 把顶点扩成齐次坐标后与 mvp 矩阵相乘——**纯 matmul，天然可微**，这是梯度从图像回到 `v_pos` 的第一段通道。
- [rasterize.py:30-37](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/rasterize.py#L30-L37)：`rasterize` 输出 (B,H,W,4)：前 3 个是命中点的重心坐标 (u,v) 与深度 z，第 4 个是 1-based 三角形号（0=背景）。
- [rasterize.py:49-56](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/rasterize.py#L49-L56)：`antialias` 在 silhouette 处做解析梯度抗锯齿，让轮廓边缘也可微。
- [rasterize.py:58-78](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/rasterize.py#L58-L78)：`interpolate/interpolate_one` 用 rast 里的重心坐标把任意顶点属性插值到像素——法向、位置、mask 都靠它。

**渲染器**（[threestudio/models/renderers/nvdiff_rasterizer.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/nvdiff_rasterizer.py)）：

- [nvdiff_rasterizer.py:17-32](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/nvdiff_rasterizer.py#L17-L32)：注册名 `nvdiff-rasterizer`；`configure` 接收 geometry/material/background 引用（u5-l2 讲过的「构造注入、不重复挂载」模式）并创建 context。
- [nvdiff_rasterizer.py:45-55](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/nvdiff_rasterizer.py#L45-L55)：前四步主链路——`mesh = self.geometry.isosurface()`（**每次前向都重新提取等值面**，所以 sdf/deformation 的变化立刻反映到下一张渲染图）、clip 变换、光栅化、`mask = rast[...,3:] > 0`、`opacity = antialias(mask)`。
- [nvdiff_rasterizer.py:116-141](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/nvdiff_rasterizer.py#L116-L141)：法向两连。世界系：插值 `mesh.v_nrm` → 归一化 → 在 mask 处 lerp 到 [0,1] 色域 → antialias，输出 `comp_normal`。视线系：`w2c = c2w[:3,:3].inverse()` 旋到相机系，背景法向取 (0,0,1)，输出 `comp_normal_viewspace`。注意 [nvdiff_rasterizer.py:143-146](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/nvdiff_rasterizer.py#L143-L146) 的 TODO：无论是否需要都算了法向——geometry 阶段需要它做 rgb/normal 交替渲染（[dreamcraft3d-geometry.yaml:93](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-geometry.yaml#L93) 的 `freq.n_rgb: 4`，详见 u6-l2）。
- [nvdiff_rasterizer.py:148-186](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/nvdiff_rasterizer.py#L148-L186)：RGB 主路径。`selector = mask[...,0]` 挑出前景像素；`gb_pos = interpolate(mesh.v_pos)` 得到**每像素的表面点**（可微）；只在 `gb_pos[selector]` 上查询 `self.geometry(positions)` 得到特征（[nvdiff_rasterizer.py:159-160](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/nvdiff_rasterizer.py#L159-L160)），再经 `self.material(...)`（no-material 把特征 sigmoid 成 RGB，见 u5-l5）得到前景色；背景由 `self.background(dirs=gb_viewdirs)` 按视线方向给出，`torch.lerp` 按 mask 混合，最后 antialias。整条链上 `v_pos → v_pos_clip → gb_pos → features/rgb` 全程可微。
- [nvdiff_rasterizer.py:57-77](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/nvdiff_rasterizer.py#L57-L77)：`render_mask` 分支（DreamCraft3D 特有）。系统侧只在 texture 阶段开启（[dreamcraft3d.py:65-69](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L65-L69)：`stage == "texture"` 才传 `render_mask=True`）。它在 `no_grad` 下用 `mvp_mtx_ref`（u4-l2 讲过的参考相机矩阵）再光栅化一次：参考视角下可见的三角形号取自 rast 第 4 通道（`-1` 转 0-based），把可见面的三个顶点在 `mesh._v_rgb` 上标 1，插值回**当前视角**得到 `mask_vis`，输出 `out["mask"] = 1 - mask_vis`——即「当前视角下参考视角看不见的区域」。该 mask 随后作为 `mask=` 参数传给 guidance（[dreamcraft3d.py:196-203](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L196-L203)），在 BSD 训练中标记「参考图没见过的区域」，细节留给 u7-l5。

**网格正则的消费现场**：

- [threestudio/systems/dreamcraft3d.py:294-297](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L294-L297)：系统直接拿渲染器透传的 `out["mesh"]` 计算 `normal_consistency` 与 `laplacian_smoothness` 两项正则——它们是 deformation/sdf 梯度的另一大来源（不经过渲染图像）。
- [threestudio/models/mesh.py:134-160](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/mesh.py#L134-L160)：顶点法向 = 邻接面 `cross(v1-v0, v2-v0)`（未归一化即面积加权）scatter_add 到顶点后归一化，退化法向兜底为 (0,0,1)。
- [threestudio/models/mesh.py:255-274](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/mesh.py#L255-L274)：`edges`（面边去重）与 `normal_consistency = mean(1 - cos(n_a, n_b))`——相邻面法向应一致，惩罚皱褶与噪声。geometry 配置给它的权重是步数调度的四元组 `[1000, 10.0, 1, 2000]`（[dreamcraft3d-geometry.yaml:111](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-geometry.yaml#L111)，C() 调度见 u2-l2/u8-l1）：第 1000 步为 10，线性衰减到第 2000 步为 1——前期强压噪声，后期放松让细节生长。
- [threestudio/models/mesh.py:276-309](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/mesh.py#L276-L309)：`laplacian` 用均匀拉普拉斯算子 \(\delta_i = \frac{1}{|N_i|}\sum_{j \in N_i}(v_j - v_i)\)（以稀疏矩阵 `L.mm(v_pos)` 实现）的范数均值做平滑正则；geometry 配置里权重为 0（[dreamcraft3d-geometry.yaml:112](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-geometry.yaml#L112)），默认关闭。

#### 4.5.4 代码实践

**实践目标**：逐行标注 `forward` 的张量形状，搞清「只在可见表面点查询几何」带来的计算量差异。

**操作步骤**：

1. 打开 [nvdiff_rasterizer.py:34-186](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/nvdiff_rasterizer.py#L34-L186)，为每个中间量写形状注释，起点：`mvp_mtx (B,4,4)`、`mesh.v_pos (Nv,3)`、`mesh.t_pos_idx (Nf,3)`、`v_pos_clip (B,Nv,4)`、`rast (B,H,W,4)`、`mask (B,H,W,1)`、`gb_pos (B,H,W,3)`、`positions (P,3)`（P=前景点数）、`rgb_fg (P,3)`、`gb_rgb_aa (B,H,W,3)`。
2. 思考题笔算：B=1、H=W=1024 时，`positions` 的上界是多少？对比 nerf-volume-renderer 每射线 `num_samples_per_rank` 量级采样点（u5-l2），两者查询几何网络的点数差多少个数量级？
3. 在 [nvdiff_rasterizer.py:179-180](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/nvdiff_rasterizer.py#L179-L180) 处脑内追踪：`gb_rgb_fg` 是全零图按 `selector` 填充的，为什么必须「先建全零图再按 mask 填」而不能直接 reshape？

**需要观察的现象**：步骤 2 应得出 P ≤ H×W ≈ 10⁶，而体渲染在 512² 图上就要 512×512×(每射线几百) ≈ 10⁸ 量级查询——两个数量级以上的差距，这就是 texture 阶段敢用 1024 分辨率的底气。

**预期结果**：完成一份带形状注释的 forward 笔记；步骤 3 的答案：`selector` 只覆盖前景点，背景像素没有对应行，必须先有与图像同形的画布再把前景散回原位。

#### 4.5.5 小练习与答案

**练习 1**：光栅化路径里，哪些环节**没有**梯度？
**答案**：z-buffer 的命中判定（哪个三角形覆盖哪个像素）与重心坐标本身是离散/解析中间量，不回传；`render_mask` 分支整体在 `no_grad` 下（它只产出离散的可见性区域，不是训练目标）。有梯度的是：顶点变换的 matmul、属性插值（对顶点属性与顶点位置）、材质/几何网络的前向、antialias 的轮廓项。

**练习 2**：`comp_normal` 为什么在 mask 外被 lerp 成 0（即 (0,0,1) 法向映射色）而不是保留插值结果？
**答案**：背景处没有命中三角形，rast 的重心坐标无意义，插出来的法向是垃圾值；置 0（对应 [0,1] 色域的 0.5 灰）保证输出良定，且下游 guidance 拿到的是干净的前景法向图。

**练习 3**：geometry 阶段为什么把 `lambda_normal_consistency` 设成随步数衰减的四元组，而不是常数？
**答案**：DMTet 初期表面噪声大，需要较强的一致性约束把表面「熨平」；随着 sdf/deformation 收敛，过强的平滑会抹掉真实细节，故从 10 线性降到 1。这是 C() 调度机制（u8-l1 专题）在网格正则上的应用。

## 5. 综合实践

**任务：为 geometry 阶段写一份「从 ckpt 到梯度」的完整交接说明书。**

假设你接手维护 DreamCraft3D，需要向新同事解释 geometry 阶段从按下回车到第一批梯度更新的全过程。请产出一页文档，包含三张图/表：

1. **初始化数据流图**：以 [README.md:118-120](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/README.md#L118-L120) 的命令为输入，画出 `last.ckpt → find_last_path → parsed.yaml → 重建 implicit-sdf → load_module_weights("geometry") → isosurface() 两遍提取 → create_from 搬运（bbox / grid_level / grid_deformation / 外观网络）→ tetrahedra-sdf-grid` 的箭头图，每个箭头标注文件与行号（以 4.4 的精读为底稿）。
2. **单步训练梯度链路表**：列出一个训练 step 中所有会向 `geometry.sdf` 与 `geometry.deformation` 写梯度的损失来源，至少包含四条路径：
   - `lambda_rgb`/`lambda_mask`（参考图监督）经 `comp_rgb → gb_pos → 插值 → v_pos`；
   - `lambda_sd`/`lambda_3d_sd`（扩散引导）经渲染图回传（同上链路）；
   - `lambda_normal_consistency` 经 `out["mesh"].normal_consistency()` 直接作用于 `v_pos`；
   - 两条链在 `v_pos` 处汇合后，分别经插值的 **sdf 权重项**（→ `sdf` 参数）与**位置项**（→ `tanh(deformation)/R` → `deformation` 参数）分流。
3. **一张对比表**：DMTet（本讲）vs NeuS（u5-l3）在「参数、表面、法向、渲染器、单步查询点数、可导出性」六行的差异。

完成后自测：能否不看讲义回答「如果删掉 `create_from` 里的 `instance.sdf.data = mesh.extras["grid_level"]` 这一行，训练会发生什么？」（答案：DMTet 的 sdf 保持全零初始化，marching tetrahedra 在全零场上提取不出任何表面——零场没有符号变化，网格为空，渲染全为背景，几何只能靠扩散引导从零重学，等于丢弃了 coarse 阶段全部几何成果。）

## 6. 本讲小结

- DMTet 把 SDF 从 MLP 权重搬到**固定的四面体网格顶点**上（`sdf` Nv×1 + `deformation` Nv×3），配合每次前向的 marching tetrahedra 显式提取三角网格，几何由「顶点参数」直接表达。
- 等值面提取是**两段式**：拓扑判定（16 种符号模式查表）在 `no_grad` 内不可微；穿越边上的**线性插值**留在 autograd 图内——\(v = \frac{s_1 p_0 - s_0 p_1}{s_1 - s_0}\) 对 sdf 与网格顶点（含 deformation）均可微，这就是 deformation 参与训练的梯度来源。
- `create_from` 完成跨表示交接：系统侧重建旧几何并抽取 ckpt 的 `geometry.*` 权重，几何侧让旧几何跑一次**两遍提取**（先粗后紧），把紧凑 bbox、顶点 SDF 值、位移与外观网络 state_dict 一并搬给 DMTet；紧凑包围盒让全部网格分辨率集中在物体附近。
- `NVDiffRasterizer` 用 nvdiffrast 实现可微光栅化：可微的顶点变换与属性插值 + 轮廓抗锯齿，**只在可见表面点**查询外观网络，使 1024×1024 渲染成为可能；texture 阶段额外输出参考视角不可见区域的 `mask` 传给 BSD guidance。
- 法向来源切换：隐式几何用场梯度，DMTet 用面法向叉积按面积加权摊到顶点；geometry 阶段的 `normal_consistency`（权重随步数 10→1 衰减）直接作用于 `mesh.v_pos`，与渲染损失共同驱动 sdf/deformation 更新。

## 7. 下一步学习建议

- **下一讲（u5-l5）**：材质、背景与 PatchRenderer——本讲渲染器里 `self.material(...)` 与 `self.background(...)` 两个调用点的内部实现（no-material 如何把特征 sigmoid 成 RGB、纹理阶段的分块高分辨率渲染思路）。
- **横向对照**：把本讲的 [isosurface.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/isosurface.py) 与 u5-l2 的 nerfacc 采样对照阅读，体会「离散决策 + 可微连续计算」这一共同模式在两个渲染范式中的重复出现。
- **向后衔接**：u6-l1/u6-l2 会把这些输出（`comp_rgb`、`comp_normal`、`mesh`）接到 dreamcraft3d-system 的训练循环里，看清 rgb/normal 交替渲染与损失调度如何消费本讲的产物；u7-l5 讲 texture 阶段 `out["mask"]` 在 BSD 里的用途；u8-l3 讲 DMTet 几何如何经 mesh-exporter 烘焙成 obj 网格。
- **延伸阅读**：DMTet 原论文（*Deep Marching Tetrahedra: a Hybrid Representation for High-Resolution 3D Shape Modeling*, NeurIPS 2021）与 nvdiffrast 文档（属性插值与 antialias 的梯度公式推导）。
