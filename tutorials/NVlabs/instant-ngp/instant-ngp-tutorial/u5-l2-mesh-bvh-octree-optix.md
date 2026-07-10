# 网格 BVH、八叉树与 OptiX

## 1. 本讲目标

本讲紧接 u5-l1（SDF 原语与球面追踪）。上一讲我们已经知道：SDF 训练时，网络要学的「真值距离」并不从天而降，而是由 `TriangleBvh::signed_distance_gpu` 根据原始三角网格算出来的。但那个「算出来」究竟是怎么算的？为什么需要三种不同的算法？为什么又要引入八叉树和 OptiX？本讲就回答这些问题。

具体来说，SDF 原语的核心难点是：给定一个三角网格和空间中任意一点 $\mathbf{p}$，**如何快速、正确地算出 $\mathbf{p}$ 到网格表面的「有向」距离**（带正负号，表示在表面外还是内）。这件事在数学上分为两步——先算「最近距离」（不带符号），再定「符号」（内外判定）。这两步的复杂度和正确性，正是本讲三个加速结构要解决的问题：

1. **TriangleBvh**：用层次包围盒（BVH）把「在成千上万个三角形里找最近那个」从 $O(N)$ 降到约 $O(\log N)$。
2. **EMeshSdfMode 三种内外判定**：Watertight（靠法线定符号，要求网格水密）、Raystab（射 32 条光线看能否逃逸）、PathEscape（随机游走 4 跳找出口，最鲁棒）。
3. **OptiX**：把上面那些「光线-三角形求交」交给 GPU 的硬件光线追踪单元（RT Core）去算，速度再上一个数量级；并通过 `TriangleOctree` 提供 Takikawa 编码，把网格的稀疏结构直接喂给网络。

学完后你应当掌握：

1. 理解 `TriangleBvh` 如何用层次包围盒加速「最近三角形」与「光线求交」两类查询，作为 SDF 真值的几何后端。
2. 掌握 `EMeshSdfMode`（Watertight / Raystab / PathEscape）三种「内外判定」算法的原理，以及各自对网格是否水密（watertight）的要求。
3. 看懂 `TriangleOctree` 如何既作为 SDF 渲染/采样的空间加速结构，又作为 Takikawa 编码的可学习查找表。
4. 认识 OptiX 程序（raytrace / raystab / pathescape）如何在 CMake 中被编译成 PTX、再用 bin2c 打包进 C 头文件、最后在运行时被加载。

---

## 2. 前置知识

### 2.1 为什么要算「点到网格的距离」

SDF 训练（见 u5-l1 的 `generate_training_samples_sdf`）需要监督信号：在每个采样点 $\mathbf{p}$ 上，告诉网络「正确答案」是该点到网格表面的有向距离 $d^*(\mathbf{p})$。这个 $d^*$ 无法解析得到（除非网格是球、盒子这类规则体），只能**从原始三角网格数值地计算**。网格可能有几十万个三角形，逐个比对显然太慢，于是需要空间加速结构。

### 2.2 层次包围盒（BVH）

**BVH（Bounding Volume Hierarchy，层次包围盒）** 是一种树形加速结构：

- 每个内部节点存一个**包围盒（AABB，轴对齐包围盒）**，它恰好包住该节点下所有三角形；
- 每个叶子节点存一小撮三角形；
- 查询「离 $\mathbf{p}$ 最近的三角形」时，从根出发，用一个栈维护待访问节点，**先剔除包围盒离 $\mathbf{p}$ 太远的子树**，从而只检查极少数三角形。

直觉上，这像查字典时先看部首目录，跳过整片无关区域，而不是逐页翻。

### 2.3 「距离」好算，「符号」难定

给定 $\mathbf{p}$，用 BVH 找到最近三角形后，「无符号距离」 $|d|$ 就是 $\mathbf{p}$ 到那个三角形的最近欧氏距离（`Triangle::distance_sq` 算的就是它）。但 SDF 需要**有向**距离——还得知道 $\mathbf{p}$ 在表面**外**（正）还是**内**（负）。这就是三种 `EMeshSdfMode` 分歧的根源：

- 如果网格是**水密**（封闭、无破洞、法线一致朝外），符号可以直接用最近三角形的法线方向判定；
- 如果网格**不水密**（有破洞、或只是开放曲面），法线方向就不可靠，必须改用「发射光线看能否逃出网格」的统计判定。

### 2.4 什么是「水密网格」

- **水密（watertight / closed manifold）**：网格像一个不漏水的封闭容器，任意一条边恰好被两个三角形共享，法线方向一致朝外。典型的有 `armadillo.obj`、球体等实体模型。
- **非水密**：网格有破洞、裂缝，或根本就是开放曲面（如一张纸）。此时「内部」这个概念本身可能模糊，需要用更鲁棒的光线逃逸法判定。

### 2.5 OptiX 与 RT Core

**OptiX** 是 NVIDIA 的光线追踪引擎。现代 NVIDIA GPU（RTX 系列）有专用的**RT Core（光线追踪核心）**硬件，能用一棵硬件加速结构（GAS，Geometry Acceleration Structure）极速完成「光线 vs 三角形」求交。instant-ngp 用 OptiX 把 SDF 的内外判定光线交给 RT Core 跑，比纯 CUDA 软件遍历 BVH 快得多。OptiX 程序是用专门的 `__raygen__`/`__closesthit__`/`__miss__` 入口写的，先编译成 **PTX**（NVIDIA 的并行线程汇编中间码），再在运行时加载。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [include/neural-graphics-primitives/triangle_bvh.cuh](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/triangle_bvh.cuh#L28-L63) | `TriangleBvhNode` 节点结构、`TriangleBvh` 抽象基类（声明 `signed_distance_gpu`/`ray_trace_gpu`/`build`/`build_optix` 等虚接口） |
| [src/triangle_bvh.cu](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/triangle_bvh.cu#L42-L877) | BVH 全部实现：4 叉 BVH 构建、`closest_triangle`/`ray_intersect` 遍历、Watertight/Raystab 三种距离算法、OptiX `Program`/`Gas` 封装、PTX 头文件包含 |
| [include/neural-graphics-primitives/triangle_octree.cuh](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/triangle_octree.cuh#L47-L193) | `TriangleOctree`：从 BVH 构建八叉树，生成供 Takikawa 编码使用的「对偶节点」顶点索引 |
| [include/neural-graphics-primitives/triangle_octree_device.cuh](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/triangle_octree_device.cuh#L22-L160) | 八叉树的设备端（GPU kernel 内）操作：`traverse`/`contains`/`ray_intersect` |
| [src/optix/raytrace.cu](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/optix/raytrace.cu#L26-L72) | OptiX 程序之一：追踪光线到网格表面，回写命中点位置与法线（供渲染/法线计算用） |
| [src/optix/raystab.cu](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/optix/raystab.cu#L27-L77) | OptiX 程序之二：射 32 条 Fibonacci 分布光线判定内外 |
| [src/optix/pathescape.cu](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/optix/pathescape.cu#L54-L121) | OptiX 程序之三：32 条路径 × 4 跳余弦随机游走判定内外（最鲁棒） |
| [include/neural-graphics-primitives/common.h](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/common.h#L118-L123) | `EMeshSdfMode` 枚举：`Watertight`/`Raystab`/`PathEscape` |
| [CMakeLists.txt](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/CMakeLists.txt#L294-L335) | OptiX 程序编译为 PTX、用 bin2c 打包成 `optix_ptx.h` 头文件的构建步骤 |

---

## 4. 核心概念与源码讲解

本讲围绕三个最小模块：**TriangleBvh**（距离与求交的几何后端）、**TriangleOctree**（稀疏空间结构 + Takikawa 编码）、**OptiX 程序**（硬件加速光线追踪）。三者关系是：BVH 是地基，八叉树与 OptiX 都建立在 BVH 之上；OptiX 又给 BVH 的光线查询提速。

### 4.1 TriangleBvh：SDF 真值的几何后端

#### 4.1.1 概念说明

`TriangleBvh` 是 instant-ngp 里 SDF 模式的「几何后端」。它的职责很纯粹：给定一批三角形和一批查询点，回答两类几何问题——

1. **最近距离查询**：每个点到最近三角形的距离是多少？这是 SDF 真值的核心。
2. **光线求交查询**：一条光线打没打中网格？打中哪？命中距离多少？这是渲染（球面追踪）和内外判定（射刺光线）都要用的。

`TriangleBvh` 是个抽象基类，真正干活的是 4 叉树实现 `TriangleBvh4`（`TriangleBvhWithBranchingFactor<4>`）。它对外暴露 `signed_distance_gpu`（带符号距离，SDF 训练用）和 `ray_trace_gpu`（光线求交，渲染用）两个 GPU 接口，内部根据 `EMeshSdfMode` 选择不同算法。

#### 4.1.2 核心流程

`TriangleBvh` 的一次完整使用分两阶段——**构建**（CPU 端，加载网格时做一次）和**查询**（GPU 端，每步训练都做）。

构建阶段（`build`）：

1. 把所有三角形用一个根包围盒包住；
2. 用栈做自顶向下递归：每次选「三角形质心方差最大的轴」对半切分，直到每个叶子里的三角形数 ≤ `n_primitives_per_leaf`（默认 8）；
3. 节点用 `left_idx`/`right_idx` 编码：负值表示叶子，正值表示内部子节点；
4. 把整棵树拷到 GPU（`m_nodes_gpu`）。

查询阶段（`signed_distance_gpu`，按 `EMeshSdfMode` 分发）：

```
signed_distance_gpu(n, mode, positions, distances, triangles, ...):
  if mode == Watertight:
     启动 signed_distance_watertight_kernel   # 每个 GPU 线程查一个点
  else:  # Raystab 或 PathEscape
     if OptiX 可用:
        先用 unsigned_distance_kernel 算无符号距离
        if mode == Raystab:   调 OptiX raystab 程序定符号
        if mode == PathEscape: 调 OptiX pathescape 程序定符号
     else:
        if mode == Raystab:   启动 signed_distance_raystab_kernel
        if mode == PathEscape: 抛异常（必须有 OptiX）
```

关键点：**无符号距离总是用 BVH 的 `closest_triangle` 算**（找最近三角形，再算到它的距离）；**符号（内外）才分三种策略**，这是下一节 4.1.3 的重点。

#### 4.1.3 源码精读

**节点结构与抽象接口**。`TriangleBvhNode` 只有两个索引字段，靠「负值表叶子」的约定区分内部节点与叶子：

[include/neural-graphics-primitives/triangle_bvh.cuh:28-32](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/triangle_bvh.cuh#L28-L32) — `TriangleBvhNode` 存一个包围盒 `bb` 和 `left_idx`/`right_idx`；注释明确「负值表示叶子」。这种编码省去了节点类型标记位，遍历时用 `node.left_idx < 0` 判叶子。

抽象基类 `TriangleBvh` 声明了所有对外接口：

[include/neural-graphics-primitives/triangle_bvh.cuh:40-53](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/triangle_bvh.cuh#L40-L53) — `signed_distance_gpu`（带符号距离，含 `EMeshSdfMode mode` 参数）、`ray_trace_gpu`（光线求交）、`touches_triangle`（某包围盒是否碰到三角形，建八叉树用）、`build`（软件构建）、`build_optix`（硬件 GAS 构建）。

**最近三角形遍历**。这是所有距离查询的基础。`closest_triangle` 用一个固定大小栈做 BVH 遍历，关键在于用「排序网络」把当前节点的 4 个子节点按「到查询点的距离」从远到近排序后再压栈：

[src/triangle_bvh.cu:520-569](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/triangle_bvh.cu#L520-L569) — 先把 4 个子节点的包围盒距离与索引装进 `children[]`，调用 `sorting_network<BRANCHING_FACTOR>(children)` 排序，再只把距离小于当前最优 `shortest_distance_sq` 的子节点压栈。排序网络（见 374-470 行）是一组无分支的 `compare_and_swap`，在 GPU 上比通用排序快得多。叶子节点（`left_idx < 0`）里逐个三角形调用 `triangles[i].distance_sq(point)` 更新最优。

**三种符号判定算法**。这是理解 `EMeshSdfMode` 的核心。

[common.h:118-123](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/common.h#L118-L123) — 枚举 `EMeshSdfMode { Watertight, Raystab, PathEscape }`，默认值是 `Raystab`（见 testbed.h:909）。

**Watertight 模式**——靠法线定符号：

[src/triangle_bvh.cu:621-629](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/triangle_bvh.cu#L621-L629) — 找最近三角形得到无符号距离 `p.second`，再取该点附近的「加权平均法线」`avg_normal`，最后用 `copysign(距离, dot(avg_normal, point - closest_point))` 定符号。其数学含义是：从最近点指向查询点的向量，若与外法线同向则在外（正），反向则在内（负）。

\[ \text{sign}(\mathbf{p}) = \mathrm{sign}\!\left( \bar{\mathbf{n}} \cdot (\mathbf{p} - \mathbf{p}_{\text{closest}}) \right) \]

这**要求网格水密且法线一致朝外**——因为符号完全来自局部法线方向。若网格有破洞或法线翻转，符号就会算错。它的好处是**最快**（只查一次最近三角形 + 一次局部法线平均，不需要发射任何光线）。

**Raystab 模式**——射 32 条「刺光线」看能否逃逸：

[src/triangle_bvh.cu:631-649](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/triangle_bvh.cu#L631-L649) — 先算无符号距离 `distance`；再沿 **Fibonacci 格点**（带随机偏移）的 32 个方向各射一条光线 `ray_intersect`，**只要有一条光线打空（`first < 0`，即没命中任何三角形）就判定在外，返回正距离**；只有 32 条全部命中，才认为是「被网格包住」，返回 `-distance`。它**不要求网格水密**——靠的是「从内部出发的光线迟早能从破洞溜出去」这一统计性质。代价是每个点要发射 32 次光线遍历，慢于 Watertight。

> 注意一个反直觉点：Raystab 默认假设「全部命中 = 在内部」。对于非常凹的几何或薄壁结构，外部点偶尔也可能 32 条光线全命中（被「卡」在凹腔里），导致误判为内部——这正是 PathEscape 要解决的。

**分发逻辑**。`signed_distance_gpu` 把上述算法按模式和是否启用 OptiX 串起来：

[src/triangle_bvh.cu:664-708](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/triangle_bvh.cu#L664-L708) — `Watertight` 走 `signed_distance_watertight_kernel`；`Raystab`/`PathEscape` 在有 OptiX 时先用 `unsigned_distance_kernel` 算无符号距离、再调 OptiX 程序定符号（4.3 节详述），无 OptiX 时 `Raystab` 回退到 `signed_distance_raystab_kernel`，而 `PathEscape` **直接抛异常**（它只在 OptiX 下可用）。

**BVH 构建**。看看树是怎么长出来的：

[src/triangle_bvh.cu:757-840](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/triangle_bvh.cu#L757-L840) — 用 `std::stack<BuildNode>` 自顶向下递归：对每个待分裂节点，计算三角形质心在 x/y/z 三轴上的方差，选方差最大的轴，用 `std::nth_element` 按该轴中位数把三角形对半切（785-815 行的循环把 1 个子区裂成 4 个）。叶子用负索引编码三角形区间（`left_idx = -(起始)-1`）。最后 `resize_and_copy_from_host` 把节点拷到 GPU。

`make()` 工厂固定返回 4 叉实现：

[src/triangle_bvh.cu:873-877](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/triangle_bvh.cu#L873-L877) — `using TriangleBvh4 = TriangleBvhWithBranchingFactor<4>`，`TriangleBvh::make()` 返回一个 `TriangleBvh4`。4 叉是经验上 GPU 遍历效率与节点紧凑度的折中。

#### 4.1.4 代码实践

**实践目标**：搞清楚三种 `EMeshSdfMode` 的算法差异与水密要求，并能在源码里定位它们的代码路径。

**操作步骤**：

1. 打开 [src/triangle_bvh.cu:621-649](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/triangle_bvh.cu#L621-L649)，对比 `signed_distance_watertight` 与 `signed_distance_raystab` 两个静态函数的最后一行（`return` 语句）。
2. 在 [include/neural-graphics-primitives/testbed.h:909](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L909) 确认默认 `mesh_sdf_mode = EMeshSdfMode::Raystab`。
3. 追踪 `generate_training_samples_sdf` 如何调用它：[src/testbed_sdf.cu:1524-1532](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_sdf.cu#L1524-L1532)，注意第 6 个参数 `true` 表示「把已有距离当作上界来加速查询」。

**需要观察的现象**：

- Watertight 的返回值符号来自 `dot(avg_normal, point - closest_point)`——只查一次最近三角形，**不发射光线**。
- Raystab 的返回值符号来自「32 条 Fibonacci 光线是否全部命中」——**要发射 32 次光线遍历**。
- 两者对网格的要求完全不同：Watertight 依赖法线方向（需水密），Raystab 依赖光线逃逸（不要求水密）。

**预期结果**：你能用自己的话讲清楚——为什么默认是 `Raystab` 而不是 `Watertight`？（因为大多数用户导入的网格未必水密，Raystab 更通用；只有当你确认网格水密且想要最快速度时，才在 GUI 里切到 Watertight。）

**待本地验证**：若有编译好的 `instant-ngp`，加载 `data/sdf/armadillo.obj`，在 GUI 的 SDF 面板切换 `mesh_sdf_mode`，观察训练初期 loss 曲线是否有差异（Watertight 真值更准更快）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `closest_triangle` 在压栈前要先对子节点做 `sorting_network`？

**参考答案**：排序后距离近的子节点后压栈、先弹出，于是搜索总是优先展开「更有希望命中最近三角形」的子树；同时 `if (children[i].dist <= shortest_distance_sq)` 用当前最优距离剪枝，远的子树直接跳过。排序网络是无分支的硬件友好实现，保证 warp 内线程走一致的控制流。

**练习 2**：`TriangleBvhNode` 用「负 `left_idx` 表叶子」的编码，遍历时怎么从叶子节点取出它包含的三角形区间？

**参考答案**：见 [triangle_bvh.cu:487-495](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/triangle_bvh.cu#L487-L495)：叶子区间为 `for (int i = -node.left_idx-1; i < -node.right_idx-1; ++i)`，即把负索引「取反减一」还原成全局三角形数组的 `[start, end)` 区间。`-1` 是为了避免 0 这个非负值歧义。

---

### 4.2 TriangleOctree：稀疏空间结构与 Takikawa 编码

#### 4.2.1 概念说明

`TriangleOctree`（三角网格八叉树）在 instant-ngp 里一身二任：

1. **空间加速结构**：把「网格占据哪些空间」用一棵稀疏八叉树记录下来。这样球面追踪时，光线在「明显没网格」的大片空区域可以直接用八叉树跳过；均匀采样训练点时，也可以只在八叉树覆盖的叶子里采，避免采到空气里。
2. **Takikawa 编码的载体**：这是一种**专门为网格 SDF 设计的输入编码**（来自 Takikawa et al. 2022 的论文），它沿八叉树的「对偶图」给每个顶点分配一个可学习特征——本质上和 HashGrid 编码目的一样（把高频细节存进查找表），但树形结构天然贴合网格表面、稀疏省内存。

为什么不用均匀网格？因为网格表面是二维流形嵌在三维空间里，均匀三维网格会在远离表面的空气里浪费海量格子。八叉树只细分「碰到三角形」的区域，叶子的分辨率可以很高（本项目深度 10），而总节点数却很少。

#### 4.2.2 核心流程

`TriangleOctree::build` 的输入是已建好的 BVH、原始三角形列表、最大深度 `max_depth`（默认 10）。流程：

1. 从覆盖整个 `[0,1]^3` 的根节点出发；
2. 逐层（`depth = 0 … max_depth-2`）用线程池并行处理当前层所有节点；对每个节点，把它的空间八等分成 8 个子包围盒；
3. 对每个子包围盒，调 `bvh.touches_triangle(bb, triangles)` 判断它是否碰到任何三角形——**碰到的才创建子节点，没碰到的不细分**（这是「稀疏」的来源）；
4. 同时构建「对偶节点（dual node）」：每个对偶节点的 8 个角顶点对应一个全局顶点索引；用 `unordered_map<u16vec4, uint32_t>` 去重，保证**共享同一位置的角顶点是同一个索引**（这是 Takikawa 编码的「连续性」保证——相邻叶子共享角顶点特征）；
5. 把普通节点和对偶节点都拷到 GPU。

设备端（GPU kernel 里）则提供三种操作：`traverse`（沿树下行并对每一层的对偶节点调用回调，供编码采样特征）、`contains`（判断点是否落在八叉树覆盖区内）、`ray_intersect`（光线-八叉树求交，用于跳空）。

#### 4.2.3 源码精读

**八叉树构建的主循环**——靠 `touches_triangle` 决定哪些子节点存在：

[include/neural-graphics-primitives/triangle_octree.cuh:64-110](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/triangle_octree.cuh#L64-L110) — 对深度 `depth` 的每个父节点，算出它 8 个子包围盒 `bb`（用 `size = scalbnf(1.0f, -depth-1)` 算边长，`scalbnf` 是「乘 2 的幂」），然后 `if (!bvh.touches_triangle(bb, triangles.data())) { children[i] = -1; continue; }`——**没碰到三角形的子方向直接标 -1 不再细分**。这正是稀疏性的来源：只有网格表面附近的区域才会被递归细分到第 10 层，空气区域停在浅层。

`touches_triangle` 本身是对 BVH 的递归查询：

[src/triangle_bvh.cu:727-755](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/triangle_bvh.cu#L727-L755) — 先用 `node.bb.intersects(bb)` 快速剔除不相交的子树，叶子节点再用 `bb.intersects(triangles[i])` 逐三角形检测。所以八叉树构建时，BVH 是它的「碰撞检测后端」。

**对偶节点与顶点去重**——这是 Takikawa 编码连续性的关键：

[include/neural-graphics-primitives/triangle_octree.cuh:117-164](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/triangle_octree.cuh#L117-L164) — 用一个 `unordered_map<u16vec4, uint32_t> coords`，键是 `(x, y, z, depth)` 四元组（顶点在树中的唯一坐标）。`generate_dual_coords` 对每个对偶节点的 8 个角顶点查表：`coords.insert({coord, m_n_vertices})`——若该坐标首次出现就分配新索引、`m_n_vertices++`，否则复用已有索引写入 `dual_node.vertices[i]`。于是**空间上重合的角顶点（相邻叶子共享的角）拿到同一个全局顶点索引**，对应 Takikawa 编码查找表里的同一行特征。去重后顶点总数 `m_n_vertices` 远小于「8 × 节点数」。

**设备端节点结构**：

[include/neural-graphics-primitives/triangle_octree_device.cuh:22-30](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/triangle_octree_device.cuh#L22-L30) — `TriangleOctreeNode` 存 8 个 `children` 索引（-1 表无）、自身 `pos` 与 `depth`；`TriangleOctreeDualNode` 只存 8 个 `vertices` 全局索引。注意普通节点比对偶节点少一层（构建注释里说「regular nodes one layer less deep as the dual nodes」），这样对偶节点能真正达到 `max_depth`。

**设备端遍历 `traverse`**——Takikawa 编码逐层取特征的入口：

[include/neural-graphics-primitives/triangle_octree_device.cuh:32-65](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/triangle_octree_device.cuh#L32-L65) — 对每一层，先 `fun(dual_nodes[node_idx], depth, pos)` 回调（编码在此查对偶节点的顶点特征），再用「`pos[i] >= 0.5` 则进高位、并把局部坐标 ×2 映射到子空间」的标准八叉树下行（49-56 行）找到下一个子节点。遇到 `children == -1` 提前返回。这就是 Takikawa 编码「逐层细化、逐层拼接特征」的设备端骨架。

**`contains` 与 `ray_intersect`**——渲染/采样时的跳空工具：

[include/neural-graphics-primitives/triangle_octree_device.cuh:67-160](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/triangle_octree_device.cuh#L67-L160) — `contains` 判断点是否在八叉树覆盖区内（用于 `uniform_octree_sample_kernel` 只在覆盖区采样、以及 IoU 评测）；`ray_intersect` 用栈做光线-八叉树叶子求交，命中叶子就返回交点距离（见 148-154 行：`depth == max_depth-1` 的叶子直接判定命中）。球面追踪时，光线进入八叉树前的空区域可用这个距离一次跳过（见 testbed_sdf.cu:192-194 的 `distance += octree_distance`）。

**Takikawa 编码的接入点**。八叉树怎么变成「编码」？看 `reset_network` 的 SDF 分支：

[src/testbed.cu:4335-4360](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4335-L4360) — 当 `encoding.otype == "Takikawa"` 时：先确保 `triangle_octree` 建到 `n_levels` 层深度（`octree_depth_target = encoding_config["n_levels"]`，takikawa.json 里是 10），再用 `new TakikawaEncoding<network_precision_t>(starting_level, m_sdf.triangle_octree, ...)` 构造编码，并置 `m_sdf.uses_takikawa_encoding = true`。注意 `TakikawaEncoding` 类本身在依赖库 tiny-cuda-nn 里，本仓库只负责把 `TriangleOctree` 喂给它。takikawa.json 配置见 [configs/sdf/takikawa.json](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/sdf/takikawa.json#L1-L9)（`n_levels=10, starting_level=4`）。

#### 4.2.4 代码实践

**实践目标**：理解八叉树为什么稀疏，以及 Takikawa 编码如何复用角顶点。

**操作步骤**：

1. 读 [triangle_octree.cuh:95-98](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/triangle_octree.cuh#L95-L98) 的 `if (!bvh.touches_triangle(...))` 分支，确认「不碰三角形的子方向被剪掉」。
2. 读 [triangle_octree.cuh:134-140](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/triangle_octree.cuh#L134-L140) 的 `coords.insert({coord, m_n_vertices})`，理解角顶点去重。
3. 在 [src/testbed_sdf.cu:1478-1489](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_sdf.cu#L1478-L1489) 看 `uniform_octree_sample_kernel` 如何只在八叉树覆盖区采样训练点。

**需要观察的现象**：

- 八叉树节点总数 `n_nodes()` 与对偶顶点数 `n_vertices()` 都远小于「稠密 8^10」（稠密会是天文数字），因为大量空气区域被剪掉了。
- 相邻叶子共享的角顶点共享同一个索引，所以 Takikawa 编码在表面上是连续的，不会出现接缝。

**预期结果**：你能解释「为什么 Takikawa 编码比同分辨率的稠密 3D 网格省内存」——因为八叉树只细分表面附近，且角顶点去重。

**待本地验证**：若能运行，加载 armadillo 并用 `-n takikawa` 切到 Takikawa 编码，观察启动日志里 `Built TriangleOctree: depth=10 nodes=... dual_nodes=...`，对比 `n_nodes` 与「8^10」的巨大差距。

#### 4.2.5 小练习与答案

**练习 1**：八叉树构建时为什么要传入已建好的 `TriangleBvh`，而不是直接拿原始三角形列表？

**参考答案**：因为判断「一个子包围盒是否碰到三角形」需要对三角形做空间查询——`touches_triangle` 内部正是用 BVH 做加速（[triangle_bvh.cu:727-755](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/triangle_bvh.cu#L727-L755)）。若不用 BVH、对每个子盒子遍历全部三角形，构建一个深度 10 的八叉树会慢得不可接受。BVH 是八叉树构建的「查询后端」。

**练习 2**：`uniform_octree_sample_kernel`（testbed_sdf.cu:492）只在八叉树覆盖区采样训练点，这对 SDF 训练有什么好处？

**参考答案**：远离网格表面的点距离很大、梯度对网络几乎无指导意义，且这类点在 SDF 渲染时基本看不到。只在八叉树（即网格附近）采样，能让有限的训练批量聚焦在「对表面形状有影响」的区域，提升表面精度与训练效率。

---

### 4.3 OptiX 程序：硬件加速的网格真值

#### 4.3.1 概念说明

Raystab 和 PathEscape 两种内外判定都依赖「发射大量光线、看能否命中网格」。纯 CUDA 软件遍历 BVH（4.1 节的 `ray_intersect`）虽然正确，但每条光线要遍历一棵树，32 条刺光线 × 数十万个查询点 = 巨大的开销。**OptiX 把这件事交给 RT Core 硬件**：先一次性把网格构建成一棵硬件加速结构 GAS（Geometry Acceleration Structure），之后每条光线的求交由专用硬件在常数级时间内完成，速度比软件遍历快一个数量级。

instant-ngp 实现了三个 OptiX 程序，对应三种用途：

| 程序 | 用途 | 被谁调用 |
| --- | --- | --- |
| **raytrace** | 追踪光线到表面，回写命中点位置 + 法线 | `ray_trace_gpu`（渲染、法线计算） |
| **raystab** | 32 条 Fibonacci 光线判定内外 | `signed_distance_gpu`（Raystab 模式） |
| **pathescape** | 32 路径 × 4 跳余弦游走判定内外 | `signed_distance_gpu`（PathEscape 模式） |

这些 `.cu` 文件不能像普通源文件那样直接编译进 `ngp` 库——它们要用 nvcc 编译成 **PTX**（一种与具体 GPU 架构无关的中间码），再用 `bin2c` 工具转换成 C 头文件里的字节数组，最后在运行时由 OptiX API 加载、针对当前 GPU 即时编译（JIT）成机器码。这就是本模块第二部分要讲的「PTX → 头文件打包」流程。

#### 4.3.2 核心流程

**运行时调用流程**（以 Raystab 为例）：

1. 加载网格时，`build_optix` 一次性构建 GAS（硬件加速结构）+ 三个 OptiX `Program` 对象（从 `optix_ptx.h` 头文件里的 PTX 字节数组创建）；
2. 训练时 `signed_distance_gpu` 先用普通 CUDA kernel 算无符号距离，再调 `raystab->invoke(...)`：
   - OptiX 的 `__raygen__rg` 程序对每个查询点发射 32 条光线；
   - 每条光线由 RT Core 求交：命中触发 `__closesthit__ch`、未命中触发 `__miss__ms`；
   - 只要有一条光线 `__miss__`（逃逸），程序立即返回（点在外）；32 条全命中才把距离取负（点在内）。

**构建期 PTX 打包流程**（CMake）：

1. `optix_program` 作为 OBJECT 库，把 3 个 `.cu` 用 `CUDA_PTX_COMPILATION ON` 编译成 `.ptx`；
2. `bin2c` 工具把每个 `.ptx` 文件转成一个 C 字节数组（名字是文件名去掉点）；
3. `bin2c_wrapper.cmake` 把三个数组拼接写入 `${BINARY_DIR}/optix_ptx.h`；
4. 该头文件被加入 `NGP_SOURCES`，最终 `triangle_bvh.cu` 用 `#include <optix_ptx.h>` 把 PTX 字节数组嵌进二进制；
5. 运行时 `Program` 构造函数把这些字节交给 `optixModuleCreateFromPTX` 即时编译成当前 GPU 的机器码。

#### 4.3.3 源码精读

**三个 OptiX 程序的逻辑**。

**raytrace**——最简单的求交程序，回写命中点与法线：

[src/optix/raytrace.cu:26-72](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/optix/raytrace.cu#L26-L72) — `__raygen__rg` 取出每条光线的起点/方向，调 `optixTrace(...)`（核心：交给 RT Core 求交，`OPTIX_RAY_FLAG_DISABLE_ANYHIT` 关闭 anyhit 以求最快）。命中时 `__closesthit__ch` 用 `optixSetPayload_0(optixGetPrimitiveIndex())` 把命中三角形索引写回 payload，`__miss__ms` 写 -1。回到 raygen：`t = __int_as_float(p1)` 取命中距离，更新 `ray_origins[idx] = 原点 + t·方向`（即把光线起点推到命中点），并把命中三角形法线写到 `ray_directions` 缓冲（复用方向缓冲存法线，见 57-61 行注释）。

**raystab**——OptiX 版的 32 条刺光线，与 4.1 节软件版逻辑一致但走硬件：

[src/optix/raystab.cu:27-77](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/optix/raystab.cu#L27-L77) — 同样 `N_STAB_RAYS = 32`，用 `fibonacci_dir` 生成方向，`OPTIX_RAY_FLAG_TERMINATE_ON_FIRST_HIT` 命中即止。关键差异：payload 只 1 个寄存器，`__closesthit__ch` 写 1（命中）、`__miss__ms` 写 0（逃逸）。raygen 里 `if (p0 == 0) return;`——**只要一条光线逃逸就立刻返回**（点在外，距离保持正）；32 条全命中才执行 `params->distances[idx.x] = -params->distances[idx.x]`（取负，点在内）。注意它**不重新算距离**，只是「翻转」由前面 `unsigned_distance_kernel` 算好的无符号距离的符号。

**pathescape**——最鲁棒的内外判定，处理非水密网格的破洞：

[src/optix/pathescape.cu:54-121](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/optix/pathescape.cu#L54-L121) — 对每个查询点发射 `N_PATHS = 32` 条**随机路径**，每条路径走 `N_BOUNCES = 4` 跳：每跳发射一条光线，命中后沿命中处法线做**余弦加权随机方向**（`random_dir_cosine` + `Onb` 正交基变换，27-52 行）继续走——这模拟了「在网格内部随机游走、试图找到出口」。只要累计 `n_escaped > 2`（多于 2 条路径逃逸）就判定在外、提前返回；否则最终把距离取负。为什么比 raystab 鲁棒？因为余弦游走会**沿表面滑动**，更容易从细小破洞缝隙钻出去，适合扫描得到的「千疮百孔」网格。代价是 32×4=128 次光线追踪，最慢。

**OptiX 的 C++ 封装**。`Program` 模板类把 PTX 字节变成可执行的 OptiX 流水线：

[src/triangle_bvh.cu:88-291](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/triangle_bvh.cu#L88-L291) — 构造函数接收 PTX 字节，依次：`optixModuleCreateFromPTX`（JIT 编译成机器码，116-125 行）、创建 raygen/miss/hitgroup 三个 program group（128-190 行）、`optixPipelineCreate` 链接成流水线并算栈大小（201-231 行）、构建 Shader Binding Table（234-279 行）。`invoke`（282-285 行）把参数拷到 GPU 后 `optixLaunch` 启动。`pipeline_compile_options.traversableGraphFlags = OPTIX_TRAVERSABLE_GRAPH_FLAG_ALLOW_SINGLE_GAS`（106-107 行）告诉 OptiX「只用单层 GAS、无实例化」，以生成最优代码。

`Gas` 类把三角形数组建成硬件加速结构：

[src/triangle_bvh.cu:293-347](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/triangle_bvh.cu#L293-L347) — 用 `OPTIX_BUILD_INPUT_TYPE_TRIANGLES` 把 GPU 上的三角形顶点交给 `optixAccelBuild`，输出一个 `OptixTraversableHandle`——这就是后续所有 `optixTrace` 调用要遍历的那棵硬件树。

**`build_optix` 串起一切**：

[src/triangle_bvh.cu:842-857](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/triangle_bvh.cu#L842-L857) — 先 `optix::initialize()` 初始化 OptiX（若失败则 `available=false`、后续回退到软件路径），成功后用 `optix_ptx::raystab_ptx`/`raytrace_ptx`/`pathescape_ptx` 三个字节数组构造三个 `Program`、用三角形构造 `Gas`。这三个 PTX 数组来自下一处要讲的头文件包含：

[src/triangle_bvh.cu:35-37](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/triangle_bvh.cu#L35-L37) — `namespace optix_ptx { #include <optix_ptx.h> }`，把这个由 CMake 生成的头文件包进 `optix_ptx` 命名空间，从而得到 `optix_ptx::raystab_ptx` 等符号。

**CMake 的 PTX → 头文件打包**。这是本模块第二重点。开关与定义：

[CMakeLists.txt:223-227](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/CMakeLists.txt#L223-L227) — `NGP_BUILD_WITH_OPTIX`（默认 ON，见 [CMakeLists.txt:24](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/CMakeLists.txt#L24)）开启后设 `NGP_OPTIX` 宏并加入 optix 头文件目录。整个 4.3 节的代码都被 `#ifdef NGP_OPTIX` 包裹（如 triangle_bvh.cu:22/44/675）。

把 3 个 `.cu` 编译成 PTX 并打包：

[CMakeLists.txt:294-335](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/CMakeLists.txt#L294-L335) — 关键四步：
1. `add_library(optix_program OBJECT pathescape.cu raystab.cu raytrace.cu)`（295-299 行）建 OBJECT 库；
2. `CUDA_PTX_COMPILATION ON` + `CUDA_ARCHITECTURES OFF`（301 行）——PTX 是与架构无关的中间码，所以**关掉架构特化**，只产出通用 PTX（架构特化留给运行时 OptiX JIT）；
3. `find_program(bin_to_c NAMES bin2c)`（313 行）找 bin2c 工具（在 CUDA 的 bin 目录），找不到就 `FATAL_ERROR`；
4. `add_custom_command`（322-332 行）调用 `cmake/bin2c_wrapper.cmake`，把 OBJECT 库产物（`.ptx` 文件）转成 `${BINARY_DIR}/optix_ptx.h`，并 `list(APPEND NGP_SOURCES ${OPTIX_PTX_HEADER})`（334 行）让该头文件参与编译。

`bin2c_wrapper.cmake` 的逐文件转换逻辑：

[cmake/bin2c_wrapper.cmake:29-51](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/cmake/bin2c_wrapper.cmake#L29-L51) — 遍历每个 OBJECT 产物，对扩展名为 `.ptx`/`.bin`/`.mdl` 的文件，把文件名里的 `.` 换成 `_`（如 `raystab.ptx` → `raystab_ptx`）作为 C 数组名，调 `bin2c --name raystab_ptx raystab.ptx` 生成 `const char raystab_ptx[] = {...};`，三个数组拼接写入 `optix_ptx.h`。这正好对应 triangle_bvh.cu:847-849 引用的 `optix_ptx::raystab_ptx`/`raytrace_ptx`/`pathescape_ptx`。

至此闭环：`.cu` 源码 →（nvcc）PTX →（bin2c）C 头文件字节数组 →（运行时 `optixModuleCreateFromPTX`）当前 GPU 机器码。**这就是「PTX 打包进头文件、运行时被加载」的完整链路。**

#### 4.3.4 代码实践

**实践目标**：理解 OptiX 程序从源码到运行时的完整生命周期，并能在 CMake 里定位打包步骤。

**操作步骤**：

1. 对比软件版与硬件版的 raystab：[src/triangle_bvh.cu:636-646](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/triangle_bvh.cu#L636-L646)（软件，`ray_intersect` 遍历 BVH）与 [src/optix/raystab.cu:40-66](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/optix/raystab.cu#L40-L66)（硬件，`optixTrace`）。注意两者都是「32 条 Fibonacci 光线，任一逃逸即在外」。
2. 在 [CMakeLists.txt:294-335](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/CMakeLists.txt#L294-L335) 找到 `optix_program` OBJECT 库定义、`CUDA_PTX_COMPILATION ON`、`bin2c` 查找与 `add_custom_command`、`OPTIX_PTX_HEADER` 加入源文件列表这五处。
3. 读 [cmake/bin2c_wrapper.cmake:36-46](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/cmake/bin2c_wrapper.cmake#L36-L46)，看清 `.ptx` 文件名如何变成 C 数组名。

**需要观察的现象**：

- 三个 OptiX 程序共用同一套 `__raygen__rg`/`__miss__ms`/`__closesthit__ch` 入口名，但每个文件里实现不同——OptiX 靠「program group + 入口名」绑定，所以可以各自独立编译成单独的 PTX 模块。
- `raystab` 与 `pathescape` 都**不重新算距离**，只翻转 `unsigned_distance_kernel` 预先算好的符号（raystab.cu:68、pathescape.cu:112）。
- 软件路径下 `PathEscape` 会抛异常（[triangle_bvh.cu:704](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/triangle_bvh.cu#L704)），因为它的高效实现强依赖 RT Core。

**预期结果**：你能讲清楚——为什么 OptiX 程序要先编成「与架构无关的 PTX」而不是直接编成 `.cubin`？（因为 PTX 让同一份二进制能在任何 RTX GPU 上运行，具体架构的机器码由 OptiX 运行时针对当前卡 JIT 生成；`CUDA_ARCHITECTURES OFF` 正是为了避免 nvcc 把 PTX 进一步固化成某一架构的机器码。）

**待本地验证**：编译后查看 `build/optix_ptx.h` 是否存在，确认里面有三个 `const char ..._ptx[]` 数组；运行 instant-ngp 加载 SDF 场景时，若控制台打印 `Built OptiX GAS and shaders`（[triangle_bvh.cu:850](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/triangle_bvh.cu#L850)）说明硬件路径生效，若打印 `Falling back to slower TriangleBVH::ray_intersect`（852 行）说明 OptiX 初始化失败、已回退到软件路径。

#### 4.3.5 小练习与答案

**练习 1**：`signed_distance_gpu` 里，为什么 Raystab/PathEscape 在调 OptiX 程序**之前**要先跑一遍 `unsigned_distance_kernel`？

**参考答案**：因为 raystab/pathescape 这两个 OptiX 程序**只负责判定符号（内外），不负责算距离数值**——它们的 `__raygen__rg` 只在确认「点在内部」时执行 `distances[idx] = -distances[idx]` 翻转符号（[raystab.cu:68](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/optix/raystab.cu#L68)、[pathescape.cu:112](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/optix/pathescape.cu#L112)）。所以必须先用 BVH 的 `unsigned_distance_kernel` 算出无符号距离（[triangle_bvh.cu:677-684](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/triangle_bvh.cu#L677-L684)），OptiX 程序在其基础上「加符号」。Watertight 则一步到位（距离与符号都由 `closest_triangle` + 法线给出），不需要这种两阶段。

**练习 2**：为什么 `pathescape` 用「余弦加权随机游走」而不是像 `raystab` 那样用固定方向的直线？

**参考答案**：固定方向的直线（raystab）遇到网格破洞时，32 条直线可能碰巧都「撞墙」而误判外部点为内部；而余弦加权游走（pathescape）命中表面后沿法线半球随机弹射，模拟「在内部游走寻找出口」，**更容易顺着破洞钻出去**，因此对扫描得到的非水密、多破洞网格更鲁棒。代价是 32 路径 × 4 跳 = 128 次光线追踪，开销最大。这也是它「最慢但最准」的原因。

---

## 5. 综合实践

**任务**：为一个 SDF 场景，亲手验证「网格质量 → 该选哪种 `EMeshSdfMode`」的决策链，并追踪一条完整的「真值距离计算」调用链。

**步骤**：

1. **梳理决策表**。基于 4.1 节，填写下表（答案见后）：

   | `EMeshSdfMode` | 算符号的方法 | 要求网格水密？ | 是否需要 OptiX？ | 相对速度 |
   | --- | --- | --- | --- | --- |

2. **追踪调用链**。从 `train_sdf` 出发，找到 SDF 真值的产生路径：
   - `train_sdf` → `generate_training_samples_sdf`（[testbed_sdf.cu:1449](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_sdf.cu#L1449)）
   - → `triangle_bvh->signed_distance_gpu(..., mesh_sdf_mode, ...)`（[testbed_sdf.cu:1524](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_sdf.cu#L1524)）
   - → 按 `mesh_sdf_mode` 分发到 watertight kernel / unsigned+optix / raystab kernel（[triangle_bvh.cu:664](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/triangle_bvh.cu#L664)）
   - → 内部用 `closest_triangle`（[triangle_bvh.cu:520](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/triangle_bvh.cu#L520)）查 BVH 得无符号距离，再定符号。

   画出这条链的流程图，标注每一处分发依据的是 `mesh_sdf_mode` 还是 `m_optix.available`。

3. **（可选，待本地验证）实测对比**。若有编译环境，加载 `data/sdf/armadillo.obj`（水密网格），用 `--network configs/sdf/base.json` 训练，分别在 GUI 切换三种 `mesh_sdf_mode`，记录「每步训练耗时」与「IoU 收敛值」。预期：Watertight 最快且最准（因为网格水密），Raystab 稍慢，PathEscape 最慢但若换成非水密网格则只有它最可靠。

**参考决策表**：

| `EMeshSdfMode` | 算符号的方法 | 要求水密？ | 需要 OptiX？ | 相对速度 |
| --- | --- | --- | --- | --- |
| Watertight | 最近三角形的加权平均法线方向 | **是**（法线须一致朝外） | 否 | 最快 |
| Raystab | 32 条 Fibonacci 直线能否逃逸 | 否 | 否（有则更快） | 中 |
| PathEscape | 32 路 × 4 跳余弦游走能否逃逸 | 否 | **是**（软件版直接抛异常） | 最慢但最鲁棒 |

---

## 6. 本讲小结

- **TriangleBvh 是 SDF 的几何后端**：用 4 叉层次包围盒把「最近三角形查询」与「光线求交」降到对数级复杂度，靠「负索引表叶子」与排序网络实现高效的 GPU 遍历。
- **`EMeshSdfMode` 三种模式分歧在「如何定符号」**：Watertight 靠法线（要求水密、最快），Raystab 靠 32 条直线逃逸（不要求水密），PathEscape 靠余弦随机游走（最鲁棒、专治破洞网格、但必须 OptiX）。
- **TriangleOctree 一身二任**：既是渲染/采样的稀疏空间加速结构（靠 `touches_triangle` 只细分表面附近、靠 `contains`/`ray_intersect` 跳空），又是 Takikawa 编码的载体（通过对偶节点角顶点去重，给表面顶点分配可学习特征）。
- **八叉树构建依赖 BVH**：`touches_triangle` 内部用 BVH 做碰撞检测加速；BVH 是八叉树与距离查询共同的「查询后端」。
- **OptiX 把光线求交交给 RT Core 硬件**：三个程序 raytrace/raystab/pathescape 分别用于求交、内外判定（直线）、内外判定（游走），共享 GAS 加速结构。
- **PTX 打包是关键工程链路**：`.cu` →（`CUDA_PTX_COMPILATION ON`）PTX →（bin2c + `bin2c_wrapper.cmake`）`optix_ptx.h` 字节数组 →（运行时 `optixModuleCreateFromPTX`）当前 GPU 机器码；PTX 与架构无关，特化推迟到运行时 JIT。

---

## 7. 下一步学习建议

- **若关心渲染提速的统一框架**：进入 **u8-l2 JIT 融合与全融合内核**，看 NeRF/SDF 的渲染如何用运行时编译（RTC）把编码 + MLP 融合成单一内核；本讲的 OptiX JIT 编译是同类思想（运行时生成 GPU 代码）在光线追踪上的体现。
- **若关心其他原语**：本单元剩余两篇 **u5-l3 图像原语** 与 **u5-l4 体素原语** 不再依赖 BVH/八叉树，可独立阅读。
- **若想深入网格处理**：可以阅读依赖库 tiny-cuda-nn 里 `TakikawaEncoding` 的实现（本仓库只提供 `TriangleOctree` 数据、编码逻辑在库中），对照本讲 4.2.3 的 `traverse` 理解它如何逐层插值对偶顶点特征。
- **若关心产物**：SDF 训练完可用 Marching Cubes 提取网格（**u6-l4**），那里会用到本讲的 BVH 做网格真值采样（`get_sdf_gt_on_grid`，testbed_sdf.cu:1550）。
