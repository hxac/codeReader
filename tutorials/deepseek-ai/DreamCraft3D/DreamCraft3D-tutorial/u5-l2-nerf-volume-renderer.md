# 讲义 u5-l2：NeRF 体渲染器：nerfacc ray marching 与 proposal network

## 1. 本讲目标

上一讲（u5-l1）我们读完了 coarse 阶段的几何组件 `implicit-volume`：它把空间任一点 \(\mathbf{x}\) 映射为「密度 + 特征」。本讲读它的下游消费者——**体渲染器 `nerf-volume-renderer`**，回答三个问题：

1. 一条射线上的连续积分，如何被离散成一串采样点并求和成像素颜色？（ray marching）
2. 为什么有两种采样表示：**紧凑的 `ray_indices` 打包格式**与**逐像素全射线的稠密矩阵**？它们分别出现在哪里？
3. 渲染器自己的 `update_step` 钩子都在干什么——占用网格（occupancy grid）如何剪枝？proposal network 的 `proposal_requires_grad_fn` 又在调度什么？

读完本讲，你应当能独立读懂 `forward()` 里每一步的张量形状，并理解 `num_samples_per_ray` 这类参数在显存、速度与渲染质量之间的取舍。

> 术语对照：大纲里提到的 `render_nerf/render_nerf_grad` 是上游 threestudio 旧版拆分出的函数名，**本仓库没有这两个函数**。DreamCraft3D 的实现是单个 `forward()`，内部按「采样器（occgrid/proposal/importance）」与「训练/评估（是否 `chunk_batch`）」两个维度分成几套路径，本讲按真实代码讲解。

## 2. 前置知识

- **体渲染（volume rendering）**：NeRF 类方法不显式建面，而是假设空间中充满带颜色的"雾"（密度 σ、颜色 c）。相机发出一条射线，像素颜色等于沿途所有雾点的加权积分。
- **离散化**：沿射线取 N 个小区间，第 i 个区间的长度为 Δtᵢ，则

  \[ \alpha_i = 1 - \exp(-\sigma_i \Delta t_i), \qquad T_i = \exp\Big(-\sum_{j<i}\sigma_j \Delta t_j\Big), \qquad w_i = T_i\,\alpha_i \]

  其中 \(w_i\) 是该区间的渲染权重（贡献占比），\(T_i\) 是透射率（光线"活着"到达这里的概率）。最终像素色 \(\hat{C} = \sum_i w_i c_i + (1-O)\, c_{bg}\)，不透明度 \(O = \sum_i w_i\)。这几行公式就是本讲全部代码的数学底座。
- **nerfacc**：一个高性能 CUDA 库，把上面的 ray marching、权重计算、沿射线 scatter-add 全部做成核函数。本渲染器是 nerfacc 的"编排层"。
- **注册机制与组件契约**（u1-l3、u3-l3）：yaml 中 `renderer_type: "nerf-volume-renderer"` 是注册名，`renderer:` 段是构造参数；system 的 `configure` 会把同一个 geometry/material/background 实例注入渲染器。
- **Updateable 生命周期**（u3-l2）：`update_step` 在每个训练 batch 开始前被递归调用，本讲的占用网格更新就挂在这个钩子上。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [threestudio/models/renderers/nerf_volume_renderer.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/nerf_volume_renderer.py) | 本讲主角。采样、查网络、合成像素、输出契约、占用网格更新全在这一个文件 |
| [threestudio/models/renderers/base.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/base.py) | `Renderer` 基类：持有 geometry/material/background 引用与 `bbox` |
| [threestudio/models/networks.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/networks.py) | `create_network_with_input_encoding`：proposal 网络的工厂函数 |
| [threestudio/models/estimators.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/estimators.py) | `ImportanceEstimator`：第三种采样器（用主几何自身做重要性采样） |
| [threestudio/utils/ops.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/ops.py) | `chunk_batch`、`get_activation`、`validate_empty_rays` 三个工具 |
| [configs/dreamcraft3d-coarse-nerf.yaml](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml) | coarse 阶段配置，`renderer:` 段是本渲染器的唯一实战用例 |

在四阶段流水线里，只有 **coarse-nerf 阶段**使用本渲染器（见 [configs/dreamcraft3d-coarse-nerf.yaml:81-86](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L81-L86)）；coarse-neus 起换成 NeuS 渲染器，geometry/texture 阶段换成光栅化渲染器。

## 4. 核心概念与源码讲解

### 4.1 渲染器骨架：基类装配与三种 estimator 的选择

#### 4.1.1 概念说明

渲染器是流水线里"最后拍板"的组件：几何给密度、材质给颜色、背景给环境色，渲染器决定**在哪里采样、怎么积分成图像**。`Renderer` 基类做两件小事：

1. **持有三个兄弟组件的引用**。注意它用了一个不起眼但关键的技巧——把引用包进 dataclass `SubModules`，注释写明"避免被注册成子模块"：因为 geometry/material/background 已经被 system 注册为子模块了，渲染器若再用 `self.geometry = geometry` 这种普通属性赋值，它们会**第二次**出现在 `nn.Module` 树里，参数会被 optimizer 重复收集。包进 dataclass 后，PyTorch 的模块遍历看不见它们，引用却照常可用。
2. **用 `radius` 构造包围盒 `bbox`**：\([-r, r]^3\)。这是 ray marching 的"感兴趣区域"（ROI），采样不会出这个盒子（`far_plane=1e10` 时尤其重要，否则射线要打到无穷远）。

`NeRFVolumeRenderer` 在此之上只多做一件事：**选一个采样器（estimator）**。采样器决定了"沿射线在哪里放采样点"，是性能与质量的核心旋钮。

#### 4.1.2 核心流程

```
NeRFVolumeRenderer.configure(geometry, material, background)
├── super().configure(...)            # 存引用 + 建 bbox（基类）
├── estimator == "occgrid"            # ← coarse-nerf 默认走这条
│   ├── nerfacc.OccGridEstimator(resolution=32, levels=1, roi_aabb=bbox)
│   ├── 若 grid_prune=False：格子全部标记为占用（退化为均匀采样）
│   └── render_step_size = √3 · 2r / num_samples_per_ray   # 均匀步长
├── estimator == "importance"         # 用主几何自身做重要性采样
│   └── self.estimator = ImportanceEstimator()
└── estimator == "proposal"           # 额外训练一个轻量密度网络做采样
    ├── prop_net = create_network_with_input_encoding(...)
    ├── prop_optim / prop_scheduler   # proposal 网络有自己的优化器！
    ├── nerfacc.PropNetEstimator(prop_optim, prop_scheduler)
    └── proposal_requires_grad_fn     # 梯度间歇回传调度器（见 4.4）
```

`render_step_size` 的来历：bbox 是边长 \(2r\) 的立方体，射线穿过它的最长路径是对角线 \(2r\sqrt{3}\)（代码里 1.732 就是 √3 的近似）。用它除以 `num_samples_per_ray`，得到"让 N 个采样点恰好铺满对角线"的均匀步长。粗阶段 `radius=2.0`、`num_samples_per_ray=512` 时步长约 \(1.732 \times 4 / 512 \approx 0.0135\)。

#### 4.1.3 源码精读

基类的装配与 bbox（注释直说了 namedtuple 的用意）：

- [threestudio/models/renderers/base.py:22-48](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/base.py#L22-L48)：`Renderer.configure` 把三个组件包进 `SubModules` dataclass（避免重复注册为子模块），并把 `[-radius, radius]^3` 注册为 buffer `bbox`。
- [threestudio/models/renderers/base.py:53-72](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/base.py#L53-L72)：`geometry/material/background` 三个 property 从 `sub_modules` 里取引用，另提供 `set_geometry` 等替换接口（导出、阶段衔接时会用到）。

子类的 Config——注意所有可调参数都集中在这个 dataclass 里：

- [threestudio/models/renderers/nerf_volume_renderer.py:22-48](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/nerf_volume_renderer.py#L22-L48)：`Config` 定义 `num_samples_per_ray=512`、`eval_chunk_size=160000`、`randomized=True`、近/远平面、两个法向输出开关，以及 `estimator ∈ ["occgrid", "proposal", "importance"]` 三选一及其各自参数段。**注意参数名是 `num_samples_per_ray`**（大纲任务描述里的 `num_samples_per_rank` 是笔误）。

configure 的三分支：

- [threestudio/models/renderers/nerf_volume_renderer.py:59-69](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/nerf_volume_renderer.py#L59-L69)：occgrid 分支——32³ 单层占用网格、可选全格占用（`grid_prune=False` 时把 `occs`/`binaries` 全填 True，即"不剪枝，纯均匀采样"）、按对角线算均匀步长。
- [threestudio/models/renderers/nerf_volume_renderer.py:70-71](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/nerf_volume_renderer.py#L70-L71)：importance 分支——一行，实例化自定义的 `ImportanceEstimator`（[threestudio/models/estimators.py:16-36](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/estimators.py#L16-L36)），它直接拿**主几何网络**当 proposal 用。
- [threestudio/models/renderers/nerf_volume_renderer.py:72-88](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/nerf_volume_renderer.py#L72-L88)：proposal 分支——用工厂函数建 `prop_net`，并**为它单独配了优化器与调度器**（`prop_optim`/`prop_scheduler`），因为 proposal 网络需要随主几何一起在线训练。

proposal 网络的工厂：

- [threestudio/models/networks.py:382-401](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/networks.py#L382-L401)：`create_network_with_input_encoding`——若编码/MLP 是 PyTorch 实现（`VanillaMLP`、`ProgressiveBandHashGrid` 等）则手搓 `编码 + MLP`（[threestudio/models/networks.py:352-358](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/networks.py#L352-L358) 的 `NetworkWithInputEncoding`），否则走 tiny-cuda-nn 的融合实现。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：确认"配置 → Config 字段 → 代码行为"三者一一对应。
2. **操作步骤**：
   - 打开 [configs/dreamcraft3d-coarse-nerf.yaml:81-86](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L81-L86)，数一数生效的键：`radius`、`num_samples_per_ray`、`return_normal_perturb`、`return_comp_normal`。
   - 在 [nerf_volume_renderer.py:22-48](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/nerf_volume_renderer.py#L22-L48) 的 `Config` 里逐一找到这四个字段的默认值，记录哪些被配置覆盖、哪些吃默认值（例如 `estimator` 吃默认 `"occgrid"`）。
   - 追一下 `radius: ${system.geometry.radius}` 的插值：它最终等于几何段的 `radius: 2.0`（[configs/dreamcraft3d-coarse-nerf.yaml:46](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L46)）——渲染器的 ROI 与几何的尺度必须一致，这里用插值强绑定了这一点。
3. **需要观察的现象**：配置里只写了 5 个键，但 `Config` 有十几个字段——体会"未写的字段全部落默认值"这一 OmegaConf parse_structured 行为（回顾 u2-l2）。
4. **预期结果**：得到一张「配置键 → Config 字段 → 默认值/覆盖值 → 代码落点」四列小表。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Renderer.configure` 要把三个组件包进 dataclass，而不是直接 `self.geometry = geometry`？
**答案**：直接赋值会让组件第二次挂进 `nn.Module` 子模块树，参数被 optimizer 重复收集、`state_dict` 出现重复键。包进普通 dataclass 后模块遍历不可见，但 property 仍能拿到引用。

**练习 2**：`estimator` 三个选项中，哪些需要额外的可训练参数？分别是什么？
**答案**：`proposal` 需要额外训练 `prop_net`（自带优化器 `prop_optim`）；`occgrid` 有网格 buffer 但不含需梯度下降的参数（占用率由 `occ_eval_fn` 现场重估）；`importance` 完全复用主几何网络，零额外参数。

---

### 4.2 采样三选一：ray marching 与两种采样表示

#### 4.2.1 概念说明

`forward()` 的第一步是**展平射线**：把 `[B, H, W, 3]` 的 `rays_o/rays_d` 摊平成 `Nr = B·H·W` 条射线。接下来"在哪里放采样点"有三种策略，对应两种**采样点组织格式**——这是本讲最重要的辨析点：

| 格式 | 形状 | 含义 | 谁产出 |
| --- | --- | --- | --- |
| **紧凑/打包（packed/compact）** | 一维拼接 + `ray_indices` 记归属 | 每条射线只保留"命中了占用格子"的采样点，全部拼接成一维长向量，第 i 个点属于第 `ray_indices[i]` 条射线 | occgrid 分支（nerfacc 原生返回） |
| **稠密（dense）** | `[Nr, Ns]` 矩阵 | 每条射线固定 `Ns` 个采样点，对齐成矩阵，大量位置可能是空格子 | proposal / importance 分支（返回 `[Nr, Ns]`，再手工摊平） |

紧凑格式的收益：粗阶段初期几何是一团雾，占用网格会把大片空区域剪掉，紧凑格式只对"有东西"的采样点查网络，省算力也省显存。稠密格式的收益：重要性采样天然按"每条射线采 N 个"组织，且方便做 CDF 重采样；代价是空格子也占内存。

**本仓库粗阶段实际走的是 occgrid 分支**，所以你训练时看到的是紧凑格式；proposal/importance 分支是框架保留的另外两条路，代码同样完整，读它们能帮你理解"逐像素全射线"是什么样子。

#### 4.2.2 核心流程

```
输入 rays_o/rays_d: [B,H,W,3]
   │ 展平
   ▼
rays_o_flatten/rays_d_flatten: [Nr,3]        (Nr = B·H·W)
   │ estimator.sampling(...)
   ▼
┌─ occgrid ──────────────────────────────┐
│ grid_prune=False: 均匀步长采样所有射线    │
│ grid_prune=True:  先用占用网格剪掉空格子,  │
│   再用 sigma_fn + alpha_thre=0.01 跳过    │
│   低密度区间（两轮筛选）                  │
│ 输出: ray_indices[N], t_starts_[N],      │
│       t_ends_[N]  ← 一维紧凑格式          │
└────────────────────────────────────────┘
┌─ proposal / importance ─────────────────┐
│ 按 CDF 重要性采样, 输出 [Nr, Ns] 稠密矩阵 │
│ 再用 arange().expand().flatten() 构造     │
│ ray_indices, 摊平成一维紧凑格式           │
└────────────────────────────────────────┘
   │ validate_empty_rays 兜底
   ▼
按 ray_indices 反查每点的射线原点/方向/光源
positions = t_origins + t_dirs · (t_starts+t_ends)/2
```

occgrid 分支的"两轮筛选"值得注意：第一轮是**粗粒度**的占用网格（32³ 格子级），第二轮是**细粒度**的 `sigma_fn`——在采样过程中现场查询几何密度，把 \(\alpha < 0.01\) 的区间整段跳过（nerfacc 的 `alpha_thre` 语义）。`sigma_fn` 在 `torch.no_grad()` 里执行，只做采样决策，不进计算图。

#### 4.2.3 源码精读

- [threestudio/models/renderers/nerf_volume_renderer.py:126-134](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/nerf_volume_renderer.py#L126-L134)：展平射线与光源位置——`rays_o` 从 `[B,H,W,3]` 变 `[Nr,3]`；光源 `[B,3]` 经 `expand` 到每个像素再展平，保证后面能用 `ray_indices` 一次 gather。
- [threestudio/models/renderers/nerf_volume_renderer.py:136-150](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/nerf_volume_renderer.py#L136-L150)：`grid_prune=False` 时的采样——`sigma_fn=None`、`alpha_thre=0.0`、`early_stop_eps=0`，即"不做任何剪枝的均匀步长 ray marching"，同时保留 `stratified`（分层抖动）做随机化。
- [threestudio/models/renderers/nerf_volume_renderer.py:151-167](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/nerf_volume_renderer.py#L151-L167)：`sigma_fn` 定义——由区间中点 \(t\) 反算世界坐标 `positions = t_origins + t_dirs * t_positions`，训练时直接查 `geometry.forward_density`，评估时套 `chunk_batch` 分块。注意它只返回密度 σ，不查特征，开销可控。
- [threestudio/models/renderers/nerf_volume_renderer.py:169-180](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/nerf_volume_renderer.py#L169-L180)：带剪枝的 occgrid 采样——`alpha_thre=0.01` 配合上面的 `sigma_fn`（仅当 `prune_alpha_threshold=True` 时启用）；`cone_angle=0.0` 表示不用锥形射线（每区间等宽）。
- [threestudio/models/renderers/nerf_volume_renderer.py:183-205](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/nerf_volume_renderer.py#L183-L205)：proposal 分支的 `prop_sigma_fn`——把采样点归一化到 `[0,1]`（bbox 外的点被 `selector` 置零密度），用轻量 `proposal_network` 出密度，激活函数是 `shifted_trunc_exp`（[threestudio/utils/ops.py:96-97](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/ops.py#L96-L97)），即 \(\exp(\mathrm{clip}(x-1, \max=15))\)，防爆炸的截断指数。
- [threestudio/models/renderers/nerf_volume_renderer.py:207-217](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/nerf_volume_renderer.py#L207-L217)：`PropNetEstimator.sampling`——`prop_sigma_fns` + `prop_samples`（先采 64 个）+ `num_samples`（最终 512 个）构成"粗采 → 估密度 → 按权重重采"的重要性采样链；`requires_grad` 来自 `vars_in_forward`（4.4 讲它的调度）。
- [threestudio/models/renderers/nerf_volume_renderer.py:218-225](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/nerf_volume_renderer.py#L218-L225)：**稠密转紧凑的关键四行**——`arange(n_rays).unsqueeze(-1).expand(-1, Ns).flatten()` 生成形如 `[0,0,...,0,1,1,...,1,...]` 的 `ray_indices`，与摊平的 `t_starts_/t_ends_` 对齐。对比 occgrid 分支由 nerfacc 直接返回不规则的紧凑索引，这里是"规则矩阵摊平"。
- [threestudio/models/renderers/nerf_volume_renderer.py:226-265](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/nerf_volume_renderer.py#L226-L265)：importance 分支——结构与 proposal 几乎相同，唯一区别是 `proposal_network` 传的是 `self.geometry`（主几何自己），且包在 `torch.no_grad()` 里（采样阶段不反传主几何）。两支的 `ray_indices` 构造完全一样，印证"稠密→摊平"的模式。
- [threestudio/models/renderers/nerf_volume_renderer.py:269-279](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/nerf_volume_renderer.py#L269-L279)：三支汇合——`validate_empty_rays` 兜底（[threestudio/utils/ops.py:453-459](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/ops.py#L453-L459)：若一条有效采样都没有——训练早期几何全空时会发生——就塞一个假采样点避免崩溃并打印警告）；随后统一 gather 出每个采样点的原点、方向、光源、世界坐标 `positions` 与区间长 `t_intervals`。

#### 4.2.4 代码实践

1. **实践目标**：把 `render_step_size` 与采样点数量的关系算清楚，为综合实践的降采样实验建立理论预期。
2. **操作步骤**（以下为**示例代码**，可在任意有 Python 的机器运行，无需 GPU）：

   ```python
   # 示例代码：步长与理论采样点数估算
   radius = 2.0                       # configs: system.geometry.radius
   for n in [512, 256, 128]:
       step = 1.732 * 2 * radius / n  # nerf_volume_renderer.py:66-68 的公式
       print(f"num_samples_per_ray={n:4d}  render_step_size={step:.5f}")
   # 每个像素的理论查询点数 ≈ 射线在 bbox 内长度 / step
   # 对角线最长 2*radius*sqrt(3) ≈ 6.93，典型穿过长度按 ~4.0 估：
   for n in [512, 256, 128]:
       step = 1.732 * 2 * radius / n
       print(f"n={n:4d} -> 每射线查询点约 {4.0/step:.0f} 个")
   ```

   然后对照源码：在 [nerf_volume_renderer.py:126-134](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/nerf_volume_renderer.py#L126-L134) 与 [nerf_volume_renderer.py:269-279](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/nerf_volume_renderer.py#L269-L279) 旁白处写下每行产出的形状（`Nr` 取 `B=1, H=W=128`，即 `Nr=16384`）。
3. **需要观察的现象**：步长随 `n` 反比放大（512→0.0135，128→0.054）；每射线查询点数近似随 `n` 线性下降，显存占用（`positions`、`geo_out` 均为 `[N, ...]`）应同步近似线性下降。
4. **预期结果**：一张「n → 步长 → 每射线点数 → 预期显存变化」的推算表；GPU 上的实测验证放在第 5 节综合实践（**待本地验证**）。

#### 4.2.5 小练习与答案

**练习 1**：`grid_prune=True` 时采样经历了哪两轮筛选？各自粒度是什么？
**答案**：第一轮是 32³ 占用网格的格子级筛选（哪些格子可能非空）；第二轮是 `sigma_fn` + `alpha_thre=0.01` 的区间级筛选（采样 marching 过程中现场查密度，把不透明度贡献低于 1% 的整段跳过）。

**练习 2**：proposal 分支返回 `[Nr, Ns]` 稠密矩阵后，代码如何把它变成与 occgrid 分支同构的紧凑格式？
**答案**：`ray_indices = arange(n_rays).unsqueeze(-1).expand(-1, Ns).flatten()`，同时 `t_starts_/t_ends_` 也 `flatten()`，三者的第 i 个元素对应同一采样点（见 [nerf_volume_renderer.py:218-225](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/nerf_volume_renderer.py#L218-L225)）。

**练习 3**：importance 分支为什么把几何查询包在 `torch.no_grad()` 里，而训练时的正式查询（4.3 节）不包？
**答案**：importance 采样阶段只为了决定"采样点放哪"，不应向主几何回传梯度；正式查询的密度/特征要参与损失计算，梯度必须流通。

---

### 4.3 体渲染合成：权重、累积与输出契约

#### 4.3.1 概念说明

采样点到手后，渲染分三步：

1. **查网络**：训练时一次性把所有 `positions` 喂给 geometry/material；评估时（验证/导出，分辨率高、射线多）用 `chunk_batch` 按 160000 点分块，防止单卡装不下。
2. **算权重并累积**：nerfacc 的 `render_weight_from_density` 一次算出所有采样点的 \(w_i\) 与透射率 \(T_i\)；`accumulate_along_rays` 是 scatter-add，把每条射线的 \(w_i \cdot v_i\) 加总回 `[Nr, ...]`。颜色、深度、不透明度全用这一个原语。
3. **over 合成背景**：\(\hat{C} = C_{fg} + c_{bg}(1-O)\)——前景没挡住的部分透出背景。

输出契约（`out` 字典）是渲染器与 system 之间的接口：system 的 `training_substep` 只认这些键名。粗阶段配置里 `return_normal_perturb: true`、`return_comp_normal`（由 `cmaxgt0:${system.loss.lambda_normal_smooth}` 联动，`lambda_normal_smooth=1.0>0` 故为 true）决定了训练时 `out` 里会多出 `normal_perturb` 与 `comp_normal` 两项。

#### 4.3.2 核心流程

```
positions: [N,3]（N = 实际采样点总数）
   │ 训练: geometry(positions); material(...); background(rays_d)
   │ 评估: 同上但各套 chunk_batch(160000)
   ▼
geo_out{density[N,1], features[N,F], normal[N,3]?}, rgb_fg_all[N,3], comp_rgb_bg[B,H,W,3]
   │ nerfacc.render_weight_from_density(t_starts, t_ends, density, ray_indices, n_rays)
   ▼
weights_[N], trans_[N]
   │ nerfacc.accumulate_along_rays(w, values, ray_indices, n_rays)   ← 同一原语用三次
   ├── values=None            → opacity [Nr,1]
   ├── values=t_positions     → depth    [Nr,1]      （加权平均深度 Σwᵢtᵢ）
   ├── values=rgb_fg_all      → comp_rgb_fg [Nr,3]
   └── values=(t-t_depth)²    → z_variance [Nr,1]    （深度的加权方差）
   │ over 合成
   ▼
comp_rgb = comp_rgb_fg + bg_color · (1 - opacity)   → reshape 回 [B,H,W,3]
```

`bg_color` 有个为 Zero123 服务的分支：system 传来的背景可以是 `[B,3]` 的常数随机色（Zero123 训练时的数据增广惯例），渲染器把它 `unsqueeze→expand` 成整幅图像大小再参与合成（代码注释原话："constant random color used for Zero123"）。

#### 4.3.3 源码精读

- [threestudio/models/renderers/nerf_volume_renderer.py:281-292](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/nerf_volume_renderer.py#L281-L292)：**训练路径**——`self.geometry(positions, output_normal=self.material.requires_normal)` 一次前向拿全部几何量，`self.material(...)` 消费 `geo_out`（no-material 场景下就是 `sigmoid(features)`，回顾 u5-l1），背景直接吃整图 `rays_d`。梯度全程流通，这就是"可微渲染"的主干。
- [threestudio/models/renderers/nerf_volume_renderer.py:293-310](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/nerf_volume_renderer.py#L293-L310)：**评估路径**——geometry/material/background 三者都套 [threestudio/utils/ops.py:113-146](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/ops.py#L113-L146) 的 `chunk_batch`（按首个张量的第 0 维切块、逐块前向、再把 dict/tensor 拼回），块大小 `eval_chunk_size=160000`。这就是本仓库对"训练/评估两套渲染前向"的真实实现。
- [threestudio/models/renderers/nerf_volume_renderer.py:312-322](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/nerf_volume_renderer.py#L312-L322)：权重计算——`render_weight_from_density` 输入一维的 `t_starts/t_ends/density` 加 `ray_indices/n_rays`，输出 `weights_` 与透射率 `trans_`；后者仅在 proposal 训练时缓存（`vars_in_forward["trans"]`），供 4.4 的 `update_step_end` 使用。
- [threestudio/models/renderers/nerf_volume_renderer.py:323-332](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/nerf_volume_renderer.py#L323-L332)：三次 `accumulate_along_rays`——同一个 scatter-add 原语分别累积出不透明度（values=None）、深度（values=采样点 t）与前景颜色（values=rgb）。紧凑格式的优势在此体现：`ray_indices` 就是 scatter 的目标索引。
- [threestudio/models/renderers/nerf_volume_renderer.py:334-341](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/nerf_volume_renderer.py#L334-L341)：`z_variance`——先把每条射线的深度 `t_depth` 广播回每个采样点（`depth[ray_indices]`），再累积 \((t_i - d)^2\) 的加权期望。它衡量"这条射线的重量有多分散"，是 HiFA 论文提出的 floater 抑制信号。
- [threestudio/models/renderers/nerf_volume_renderer.py:343-356](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/nerf_volume_renderer.py#L343-L356)：背景合成——`bg_color=None` 时用背景网络输出；`[B,3]` 常数色（Zero123 用）先扩展成 `[B,H,W,3]`；最后统一摊平成 `[Nr,3]` 并做 `comp_rgb_fg + bg_color * (1 - opacity)` 的 over 合成。
- [threestudio/models/renderers/nerf_volume_renderer.py:358-365](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/nerf_volume_renderer.py#L358-L365)：**输出契约**——`comp_rgb/comp_rgb_fg/comp_rgb_bg/opacity/depth/z_variance` 六个键全部 reshape 回 `[B,H,W,...]`，这是所有下游损失消费的图像级接口。
- [threestudio/models/renderers/nerf_volume_renderer.py:367-378](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/nerf_volume_renderer.py#L367-L378)：**训练期专属输出**——`weights/t_points/t_intervals/t_dirs/ray_indices/points` 外加 `**geo_out`（含 `normal`）。这些是"点级"张量，专供正则损失使用；评估时不需要，直接省掉。
- [threestudio/models/renderers/nerf_volume_renderer.py:379-403](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/nerf_volume_renderer.py#L379-L403)：两个法向输出——`comp_normal`（把点级法向按权重累积成图像级法向图、归一化后 \((n+1)/2\cdot O\) 映到可视色域）与 **`normal_perturb`**：把采样点沿随机方向扰动 \(10^{-2}\) 再查一次几何法向。直觉：真实表面上相邻两点法向应一致，扰动前后法向差异大说明表面"毛糙"，这正是 3D 法向平滑损失的比较对象。
- [threestudio/models/renderers/nerf_volume_renderer.py:404-418](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/nerf_volume_renderer.py#L404-L418)：评估分支的 `comp_normal`——同样累积，但不受两个开关控制（评估时只要有法向就输出）。

**消费现场**（system 侧，帮助理解契约为何这么设计）：

- [threestudio/systems/dreamcraft3d.py:253-265](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L253-L265)：orient 正则吃 `out["weights"]`、`out["normal"]`、`out["t_dirs"]`（惩罚背向相机的法向，压 floater）。
- [threestudio/systems/dreamcraft3d.py:267-274](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L267-L274)：sparsity/opaque 正则吃 `out["opacity"]`（压雾状半透明）。
- [threestudio/systems/dreamcraft3d.py:287-291](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L287-L291)：z_variance 正则吃 `out["z_variance"]`（对不透明度 >0.5 的像素压深度分散）。
- [threestudio/systems/dreamcraft3d.py:239-250](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L239-L250)：3d_normal_smooth 正则同时要求 `out["normal"]` 与 `out["normal_perturb"]`——缺失时直接 `raise ValueError`，这就是配置里 `return_normal_perturb: true` 的原因（缺了训练即崩）。

#### 4.3.4 代码实践（源码阅读型）

1. **实践目标**：默写出 `out` 字典的完整形状契约。
2. **操作步骤**：取 `B=1, H=W=128`，`N` 记实际采样点数、`F` 记特征维（粗阶段 no-material 下 `F=3`）。逐行读 [nerf_volume_renderer.py:358-403](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/nerf_volume_renderer.py#L358-L403)，填写下表：

   | 键 | 形状 | 级别（图像/点） | 仅训练时输出？ | 被哪项损失消费 |
   | --- | --- | --- | --- | --- |
   | comp_rgb | `[1,128,128,3]` | 图像 | 否 | rgb / SDS 引导输入 |
   | opacity | `[1,128,128,1]` | 图像 | 否 | mask / sparsity / opaque |
   | depth | ... | 图像 | 否 | depth / depth_rel |
   | z_variance | ... | 图像 | 否 | z_variance |
   | weights | `[N,1]` | 点 | 是 | orient |
   | normal_perturb | `[N,3]` | 点 | 是 | 3d_normal_smooth |
   | ... | ... | ... | ... | ... |
3. **需要观察的现象**：图像级键的形状与分辨率绑定（128 步训练时 16384 像素、3000 步翻倍到 384 后变 147456 像素），点级键的形状与采样数绑定——两类键的尺寸随训练的放大因子不同。
4. **预期结果**：完整表格一份；对照 4.3.3 列出的四个消费现场核对"被哪项损失消费"列。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `opacity/depth/comp_rgb_fg` 能用同一个 `accumulate_along_rays` 原语计算？
**答案**：三者数学形式相同，都是 \(\sum_i w_i v_i\)：values=None 时退化为 \(\sum w_i = O\)；values=tᵢ 时是加权平均深度；values=cᵢ 时是加权平均颜色。scatter-add 一个原语通吃。

**练习 2**：`z_variance` 大说明什么？对应的正则想解决什么视觉缺陷？
**答案**：说明该射线的权重分散在很深的跨度上（前后多个"半透明层"），典型症状是 floater/雾状物。HiFA 式的 z_variance 正则把不透明区域的深度方差压小，逼几何"实心化"。

**练习 3**：训练与评估的网络查询路径差在哪？为什么评估要用 `chunk_batch`？
**答案**：训练路径一次性前向（梯度需要连通，且分辨率低点数少）；评估路径对 geometry/material/background 各套 `chunk_batch`（按 `eval_chunk_size=160000` 切块），因为验证/导出分辨率高（如 512² 甚至 1024²）、射线数暴涨，不分块会 OOM。

---

### 4.4 update_step：占用网格更新与 proposal 梯度调度

#### 4.4.1 概念说明

采样器不是配置一次就完事：几何在训练中不断变化，采样策略必须跟着变。这一切挂在渲染器的两个 Updateable 钩子上（回顾 u3-l2：batch 开始前 `update_step`、batch 结束后 `update_step_end`，由 system 递归分发）。

- **occgrid**：占用网格记录"哪些格子可能有物体"。几何变了，网格就要重估——重估方式是把格子中心喂给 `geometry.forward_density`，用**一阶泰勒近似** \(1 - e^{-\sigma \Delta t} \approx \sigma \Delta t\) 算占用概率，比精确指数省一次 exp。
- **proposal**：两个机制。其一，`update_step_end` 里用前向缓存的透射率 `trans` 对 proposal 网络做一步在线训练（nerfacc 内部完成前向、求损失、调 `prop_optim`），让轻量网络持续模仿主几何的密度分布；其二，`proposal_requires_grad_fn` 决定**采样算子是否对 proposal 网络回传梯度**——它实现的是"间歇回传"调度：计数器 `steps_since_last_grad` 超过阈值才放行一次梯度，且阈值 \( \min(s/1000, 1) \times 5 \) 随步数从 0 线性升到 5。直观效果：训练早期几乎每步都回传（proposal 还不准，需要快速学），后期平均约 6 步才回传一次（proposal 已跟上主几何，省下梯度开销）。

#### 4.4.2 核心流程

```
每个训练 batch 开始前 → update_step(epoch, global_step)
├── occgrid + grid_prune:
│     occ_eval_fn(x) = density(x) · render_step_size      # 泰勒近似的占用概率
│     estimator.update_every_n_steps(global_step, occ_eval_fn)
│       └── nerfacc 内部按自身周期重估全部格子（真实重估节奏待确认，与安装版本有关）
├── proposal（训练）:
│     requires_grad = proposal_requires_grad_fn(global_step)
│     vars_in_forward["requires_grad"] = requires_grad    # 传给本 batch 的 sampling
└── proposal（评估）: requires_grad = False

每个训练 batch 结束后 → update_step_end(epoch, global_step)
└── proposal（训练）:
      estimator.update_every_n_steps(trans, requires_grad, loss_scaler=1.0)
        └── nerfacc 用前向缓存的透射率训练 prop_net 并步进 prop_optim
```

另外 `train()/eval()` 两个覆写把 `randomized`（分层抖动采样）在评估时强制关掉，保证验证/导出确定性。

#### 4.4.3 源码精读

- [threestudio/models/renderers/nerf_volume_renderer.py:90-108](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/nerf_volume_renderer.py#L90-L108)：`get_proposal_requires_grad_fn` 闭包工厂——`schedule(s) = min(s/1000, 1) × 5` 是阈值曲线；`proposal_requires_grad_fn` 用闭包变量 `steps_since_last_grad` 计数，超过阈值时返回 True 并清零（放行一次梯度），否则累加。**注意 True/False 语义**：返回 True 表示"本步采样要对 proposal 回传梯度"。
- [threestudio/models/renderers/nerf_volume_renderer.py:320-322](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/nerf_volume_renderer.py#L320-L322)：前向中把透射率 `trans_` reshape 成 `[n_rays, -1]` 存进 `vars_in_forward["trans"]`——这是 batch 间传递状态的桥梁（`vars_in_forward` 在 [nerf_volume_renderer.py:116](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/nerf_volume_renderer.py#L116) 初始化）。
- [threestudio/models/renderers/nerf_volume_renderer.py:422-436](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/nerf_volume_renderer.py#L422-L436)：`update_step` 的 occgrid 分支——`occ_eval_fn` 用 `density × render_step_size` 近似 \(1-e^{-\sigma\Delta t}\)（注释原话："approximate ... based on taylor series"）；仅在训练且非 `on_load_weights`（加载权重恢复渐进状态的那次调用不重估，回顾 u3-l2）时调用 `estimator.update_every_n_steps`。
- [threestudio/models/renderers/nerf_volume_renderer.py:437-442](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/nerf_volume_renderer.py#L437-L442)：`update_step` 的 proposal 分支——把调度结果写入 `vars_in_forward["requires_grad"]`，供本 batch 的 `estimator.sampling(requires_grad=...)`（[nerf_volume_renderer.py:216](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/nerf_volume_renderer.py#L216)）消费；评估时恒为 False。
- [threestudio/models/renderers/nerf_volume_renderer.py:444-450](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/nerf_volume_renderer.py#L444-L450)：`update_step_end`——batch 结束后用缓存的 `trans` 与本步的 `requires_grad` 更新 proposal 网络；优化器与调度器在 configure 时就已注入 estimator（[nerf_volume_renderer.py:76-88](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/nerf_volume_renderer.py#L76-L88)），nerfacc 内部完成参数更新。
- [threestudio/models/renderers/nerf_volume_renderer.py:452-462](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/nerf_volume_renderer.py#L452-L462)：`train()/eval()` 覆写——`randomized = mode and cfg.randomized`，评估时强制关闭分层抖动，保证同输入同输出。

#### 4.4.4 代码实践

1. **实践目标**：不依赖 GPU，精确复刻 `proposal_requires_grad_fn` 的调度行为，看清"间歇回传"的节奏。
2. **操作步骤**（**示例代码**，纯 Python 逐行转写 [nerf_volume_renderer.py:90-108](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/nerf_volume_renderer.py#L90-L108) 的闭包）：

   ```python
   # 示例代码：proposal 梯度间歇调度模拟
   def get_proposal_requires_grad_fn(target=5.0, num_steps=1000):
       schedule = lambda s: min(s / num_steps, 1.0) * target
       steps_since_last_grad = 0
       def fn(step):
           nonlocal steps_since_last_grad
           target_steps = schedule(step)
           requires_grad = steps_since_last_grad > target_steps
           if requires_grad:
               steps_since_last_grad = 0
           steps_since_last_grad += 1
           return requires_grad
       return fn

   fn = get_proposal_requires_grad_fn()
   hits = [fn(s) for s in range(1000)]
   print("前 20 步:", hits[:20])
   print("步 0-99 回传比例:", sum(hits[:100]) / 100)
   print("步 900-999 回传比例:", sum(hits[900:]) / 100)
   ```
3. **需要观察的现象**：前 20 步 True/False 交替（阈值从 0 缓慢爬升）；前 100 步回传比例接近 1/2，最后 100 步比例接近 1/6。
4. **预期结果**：打印出"早期约每 2 步一次、晚期约每 6 步一次"的渐变节奏；对照 4.4.1 的直觉解释（早期 proposal 需要快学、后期省开销）。此模拟与 GPU 训练中的实际调用共享同一份逻辑，但训练中的端到端效果**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`occ_eval_fn` 为什么用 `density * render_step_size` 而不是精确的 `1 - exp(-density * render_step_size)`？
**答案**：一阶泰勒近似 \(1-e^{-x} \approx x\)（x 小时误差可忽略）。占用网格只需要"这个格子要不要保留"的粗判断，省掉全网格的 exp 计算更划算。代码注释也写明了这是 taylor series 近似。

**练习 2**：`update_step` 的 occgrid 分支为什么有 `not on_load_weights` 条件？
**答案**：`on_load_weights=True` 表示这次调用来自"加载检查点后恢复渐进状态"（u3-l2 讲过 BaseModule 装权后会补一次 `do_update_step`）。此时几何刚恢复，不该顺带做网格重估这类带副作用的在线更新——真正的重估等下一个训练 batch 的正常 `update_step`。

**练习 3**：`vars_in_forward` 这个普通 dict 承担了什么角色？跨了哪两个时机？
**答案**：它是渲染器内部的批次状态总线，跨"batch 开始的 `update_step`（写 requires_grad）"与"batch 内的 `forward`（读 requires_grad、写 trans）"以及"batch 结束的 `update_step_end`（读 trans）"三个时机，把调度决策与前向产物串起来。

## 5. 综合实践

**任务：给 `forward()` 做一份带形状注释的"解剖报告"，再做一次降采样实验。**

**Part A（无需 GPU，必做）——形状解剖：**

1. 通读 [nerf_volume_renderer.py:118-420](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/nerf_volume_renderer.py#L118-L420) 的 `forward()`，在每行旁用铅笔记形状。固定 `B=1, H=W=128`（故 `Nr=16384`），`N` 记实际采样点总数，重点标注：
   - 展平段（L126-134）：`[1,128,128,3] → [16384,3]`；
   - occgrid 采样段（L136-180）：`ray_indices/t_starts_/t_ends_` 均为 `[N]`，且 `N << Nr×512`（剪枝的直观证据）；
   - 网络查询段（L281-310）：`geo_out["density"]` 为 `[N,1]`、`rgb_fg_all` 为 `[N,3]`；
   - 累积段（L312-341）：`weights_[N] → opacity/depth/z_variance [16384,1]`、`comp_rgb_fg [16384,3]`；
   - 输出段（L358-403）：图像级键回到 `[1,128,128,...]`，点级键保持 `[N,...]`。
2. 用 4.2.4 的示例脚本算出 `num_samples_per_ray ∈ {512, 256, 128}` 对应的步长，并在报告里预测：`N` 大致按什么比例缩放？显存里最大的张量（`positions` 及 `geo_out["features"]`）按什么比例缩放？

**Part B（需 GPU 与已装好的环境，选做；无法运行则标注"待本地验证"并只交理论预测）——降采样对比实验：**

1. 以 README 的 coarse 训练命令为基础，在命令行覆盖采样数（回顾 u2-l2 的点号覆盖语法）：

   ```bash
   python launch.py --config configs/dreamcraft3d-coarse-nerf.yaml --train \
     --gpu 0 \
     system.prompt_processor.prompt="a delicious hamburger" \
     system.renderer.num_samples_per_ray=128
   ```

2. 分别以默认 512 与 128 各跑数百步（可用 `trainer.max_steps=500` 缩短），记录：
   - 显存峰值（`nvidia-smi` 或 TensorBoard 的 memory 曲线）；
   - 每步耗时；
   - `save/` 下渲染图的噪声与 floater 情况（步长放大 4 倍后，密度场的细微结构更容易被"步进跨越"，且分层抖动的相对扰动变大）。
3. **预期结果（待本地验证）**：显存与每步耗时近似线性下降；渲染噪声与 floater 增多——因为 \(\Delta t\) 变大令 \(\alpha = 1-e^{-\sigma\Delta t}\) 对 σ 的分辨率变粗，几何细节（尤其 `lambda_sparsity`、`lambda_opaque` 在 2000 步拉高权重后的"实心化"过程）更难收敛。这正是官方配置选 512 的 trade-off：粗阶段几何质量是整条流水线的地基。

## 6. 本讲小结

- `Renderer` 基类用 dataclass `SubModules` 持有三兄弟组件引用（避免重复挂载进模块树），并按 `radius` 建 `bbox` 作为采样 ROI；`NeRFVolumeRenderer.configure` 的核心是按 `estimator` 三选一装配采样器。
- 采样有两种表示：occgrid 产出**紧凑的一维打包格式**（`ray_indices` 记归属、空格子不占内存）；proposal/importance 产出 `[Nr, Ns]` **稠密矩阵**再手工摊平成紧凑格式——下游代码因此可以统一用 scatter-add 消费。
- 本仓库的"两套渲染路径"实为 `forward()` 内训练/评估两分支：训练一次性前向保梯度连通，评估用 `chunk_batch` 按 160000 点分块防 OOM。
- 合成核心是三个公式三行代码：\(\alpha_i = 1-e^{-\sigma_i\Delta t_i}\)、\(w_i = T_i\alpha_i\)、\(\hat{C} = \sum w_i c_i + (1-O)c_{bg}\)；`accumulate_along_rays` 一个原语算出 opacity/depth/颜色/z_variance。
- 输出契约分图像级六键与训练期点级键（`weights/normal_perturb/...`），后者专供 orient/sparsity/opaque/z_variance/3d_normal_smooth 等正则消费，配置开关 `return_normal_perturb/return_comp_normal` 与损失权重经 `cmaxgt0` 联动。
- 渲染器自身也是 Updateable：occgrid 在 `update_step` 里用泰勒近似重估占用网格；proposal 在 `update_step_end` 用前向缓存的透射率在线训练轻量网络，梯度经 `proposal_requires_grad_fn` 间歇回传（早期约每 2 步、后期约每 6 步一次）。

## 7. 下一步学习建议

- **下一讲 u5-l3**：coarse-nerf 收尾后几何要换表示——读 `implicit-sdf` 与 `neus-volume-renderer`，重点关注它如何复用本讲的 `render_weight_from_density/accumulate_along_rays`，只是把"密度"换成 SDF 经 LearnedVariance 转换后的 NeuS α。
- **再下一讲 u5-l4**：DMTet 与 `nvdiff-rasterizer`——从"沿射线积分"切换到"先抽网格再光栅化"，对比两种渲染范式在梯度传播上的差异。
- **横向回看**：带着本讲的输出契约重读 [threestudio/systems/dreamcraft3d.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py) 的正则段（L226-291），每一项损失都能落到 `out` 的某个键上，这会是单元六的入口。
- **工具库延伸**：nerfacc 的 `OccGridEstimator`/`PropNetEstimator` 文档值得一读，本讲刻意回避了其内部实现细节（网格重估周期等标注了"待确认"），读完可以回来补全 4.4 的流程图。
