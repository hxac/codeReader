# u3-l1 GaussianModel：继承自 3DGS 的属性

## 1. 本讲目标

学完本讲，你应该能够：

1. 列出 3D 高斯的全部**可优化属性**（`_xyz`、`_features_dc`、`_features_rest`、`_scaling`、`_rotation`、`_opacity`）及其张量形状，并说出每条属性对应高斯的哪个物理量。
2. 解释「原始存储 + 读取时激活」这一设计模式：`get_scaling` 为什么要过 `exp`、`get_opacity` 为什么要过 `sigmoid`、`get_rotation` 为什么要过 `normalize`。
3. 读懂 `capture()` / `restore()` 的元组结构，并能对应到训练保存的 `chkpnt_best.pth` 检查点里究竟存了什么。

本讲刻意**只讲 3D 部分**：4C4D 的 `GaussianModel` 是从 3DGS/4DGS 一路继承过来的，先把 3D 高斯的「骨架」吃透，下一讲（u3-l2）再往上挂第四维（`_t`、`_scaling_t`、`_rotation_r`）就水到渠成。

## 2. 前置知识

### 2.1 一个 3D 高斯由什么定义

回忆 u1-l1 的内容：3DGS 用几百万个「小高斯泼溅片」叠加成图像。每个高斯需要回答四个问题：

| 问题 | 高斯参数 | 直觉理解 |
| --- | --- | --- |
| 在哪里？ | 位置均值 \(\mu\)（3 个数） | 高斯椭球中心 |
| 多大多扁、朝哪？ | 协方差 \(\Sigma\)（由尺度 3 个数 + 旋转四元数 4 个数参数化） | 椭球的三个轴长和姿态 |
| 什么颜色？ | 球谐系数（DC 项 3 个 + 高阶项若干） | 基础色 + 随视角变化的部分 |
| 多不透明？ | 不透明度 \(o\)（1 个数） | alpha 合成时的权重 |

这些参数不是手填的，而是**梯度下降学出来的**——这就是「可优化属性」一词的含义：它们被包成 `nn.Parameter`，注册进优化器，每轮迭代被 loss 的梯度更新。

### 2.2 约束与激活函数

梯度下降是无约束的：一次 `lr * grad` 的更新后，参数可以是任意实数。但物理量有约束：

- 尺度必须为**正**（负长度的椭球没有意义）；
- 不透明度必须在 \((0,1)\)（它是 alpha blending 的权重）;
- 旋转四元数必须是**单位长度**（\(\|q\|=1\)），否则不再表示旋转。

3DGS 的解法是：**存一个无约束的裸值，读取时套一个激活函数把它压回合法区间**。这就是本讲反复出现的模式：

\[\text{存储值} \xrightarrow{\ \text{activation}\ } \text{物理值}\]

- 尺度：存 \(\log s\)，读时 `exp` → 恒正；
- 不透明度：存 \(\mathrm{logit}(o)=\log\frac{o}{1-o}\)，读时 `sigmoid` → 落在 \((0,1)\)；
- 旋转：存任意 4 维向量，读时 `normalize` → 单位四元数。

autograd 会自动把物理值上的梯度沿激活函数链式传回裸参数，因此**不需要任何投影/裁剪步骤**。

### 2.3 `@property` 装饰器

Python 的 `@property` 让一个方法像属性一样访问：写 `gaussians.get_xyz`（无括号）就触发函数执行。4C4D 用它实现「读时激活」——渲染代码里到处是 `gaussians.get_opacity` 这样的调用点，读到的永远是激活后的合法值，而成员变量 `_opacity` 始终保持裸值。

### 2.4 与前几讲的衔接

- u1-l3 讲过 `train.py` 中 `training()` 的组装顺序：`GaussianModel` → `Scene` → `training_setup`。本讲深入第一步里那个对象内部。
- u2 系列讲过初始点云如何从 COLMAP/MASt3R 读取成 `BasicPointCloud`（`points/colors/normals/time` 四个 numpy 数组，见 [utils/graphics_utils.py:L17-L21](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/graphics_utils.py#L17-L21)）。本讲的 `create_from_pcd` 正是它的消费者。

## 3. 本讲源码地图

| 文件 | 本讲用它做什么 |
| --- | --- |
| `scene/gaussian_model.py` | **主战场**。`GaussianModel` 的 `__init__`（属性容器）、一整套 `get_*` property（读时激活）、`create_from_pcd`（属性如何被赋初值）、`capture`/`restore`（检查点序列化） |
| `utils/general_utils.py` | `inverse_sigmoid`（logit 反函数）、`build_scaling_rotation`（尺度+旋转 → 协方差的 Cholesky 支路） |
| `utils/sh_utils.py` | `RGB2SH`（RGB 色值换算成球谐 DC 系数）、常数 `C0` |
| `utils/graphics_utils.py` | `BasicPointCloud` NamedTuple：初始化点云的数据容器 |
| `train.py` | 构造 `GaussianModel` 的调用点、`torch.save((gaussians.capture(), iteration), ...)` 检查点保存点 |
| `render.py` | `gaussians.restore(model_params, None)`：推理时从检查点恢复 |

## 4. 核心概念与源码讲解

本讲三个最小模块：**4.1 `GaussianModel.__init__`（属性容器）**、**4.2 属性 property 与激活函数**、**4.3 `capture`/`restore` 与 checkpoint**。

### 4.1 模块一：`GaussianModel.__init__` —— 属性容器

#### 4.1.1 概念说明

`GaussianModel` 不是 `nn.Module` 的子类，而是一个普通类：它**自己管理**一堆 `nn.Parameter`，并在 `training_setup` 时手工把它们注册进 Adam。构造函数只做两件事：

1. 把所有属性初始化为**空的占位张量** `torch.empty(0)`（此时还没有任何高斯，`N=0`）；
2. 调用 `setup_functions()` 装配激活函数。

真正给属性赋值（分配 `N` 个高斯）发生在后面的 `create_from_pcd` / `load_ply` / `restore`。这是一个「先造容器、后填数据」的延迟初始化设计，好处是同一个类既能在训练开头从点云冷启动，也能在推理时从检查点热恢复。

#### 4.1.2 核心流程

```text
GaussianModel(sh_degree, gaussian_dim, ...)
 ├─ 3DGS 遗产：6 个可优化属性的占位 + 3 个统计量 + 2 个优化器位
 ├─ 4D4D 新增（本讲只认脸熟）：_t / _scaling_t / _rotation_r / env_map ...
 ├─ coefficient（Neural Decaying Function 的 MLP，可为 None）
 └─ setup_functions()  # 绑定 scaling/opacity/rotation 三个激活函数
        ↓ （稍后由 Scene 触发）
create_from_pcd(pcd, spatial_lr_scale)  # 真正分配 N 个高斯
        ↓ （再由 train.py 触发）
training_setup(opt)  # 把 6 个参数挂进 Adam
```

#### 4.1.3 源码精读

构造函数签名：[scene/gaussian_model.py:L63-L96](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L63-L96)

```python
def __init__(self, sh_degree : int, gaussian_dim : int = 3, time_duration: list = [-0.5, 0.5],
             rot_4d: bool = False, force_sh_3d: bool = False, sh_degree_t : int = 0, coefficient=None):
    self.active_sh_degree = 0
    self.max_sh_degree = sh_degree
    self._xyz = torch.empty(0)
    self._features_dc = torch.empty(0)
    self._features_rest = torch.empty(0)
    self._scaling = torch.empty(0)
    self._rotation = torch.empty(0)
    self._opacity = torch.empty(0)
    self.max_radii2D = torch.empty(0)
    self.xyz_gradient_accum = torch.empty(0)
    self.denom = torch.empty(0)
    self.optimizer = None
    ...
```

这段（L64-75）声明了从 3DGS 原封不动继承的状态。逐个看：

| 成员 | 形状（N 个高斯时） | 角色 | 可优化？ |
| --- | --- | --- | --- |
| `_xyz` | `(N, 3)` | 高斯中心位置 \(\mu\) | ✅ 挂进 Adam |
| `_features_dc` | `(N, 1, 3)` | 球谐 0 阶（DC）系数，决定基础颜色 | ✅ |
| `_features_rest` | `(N, (L+1)²-1, 3)` | 高阶球谐系数，决定视角相关颜色 | ✅ |
| `_scaling` | `(N, 3)` | **log 空间**的三个轴长 | ✅ |
| `_rotation` | `(N, 4)` | 未归一化四元数 | ✅ |
| `_opacity` | `(N, 1)` | **logit 空间**的不透明度 | ✅ |
| `max_radii2D` | `(N,)` | 每个高斯在屏幕上的最大投影半径（剪枝用） | ❌ 统计量 |
| `xyz_gradient_accum` | `(N, 1)` | 累积的 viewspace 梯度（致密化判据） | ❌ 统计量 |
| `denom` | `(N, 1)` | 梯度累积的归一化分母 | ❌ 统计量 |
| `active_sh_degree` / `max_sh_degree` | 标量 | 当前/最大球谐阶数（渐进开启） | ❌ 调度 |

注意形状上的一个细节：`_features_dc` 和 `_features_rest` 的 dims 排列是 `(N, SH系数, RGB)`——SH 在中间、颜色在最后，这是 `create_from_pcd` 里 `transpose(1, 2)` 造成的（见 4.2.3），与直觉的 `(N, 3, C)` 相反。

接着是 4D 部分与收尾（本讲只需认脸熟，u3-l2 详讲）：[scene/gaussian_model.py:L80-L96](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L80-L96)

```python
    self.gaussian_dim = gaussian_dim
    self._t = torch.empty(0)
    self._scaling_t = torch.empty(0)
    self.time_duration = time_duration
    self.rot_4d = rot_4d
    self._rotation_r = torch.empty(0)
    ...
    self.coefficient = coefficient
    self.setup_functions()
```

`gaussian_dim=3` 时，`_t/_scaling_t/_rotation_r` 三个占位张量永远保持空——这是区分「3D 高斯模型」与「4D 高斯模型」的开关，代码里大量 `if self.gaussian_dim == 4:` 分支都从它发散。

激活函数的绑定在同文件 `setup_functions` 中：[scene/gaussian_model.py:L50-L61](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L50-L61)

```python
self.scaling_activation = torch.exp
self.scaling_inverse_activation = torch.log

self.opacity_activation = torch.sigmoid
self.inverse_opacity_activation = inverse_sigmoid

self.rotation_activation = torch.nn.functional.normalize
```

三对正/逆激活函数被存成**成员变量**（而不是硬编码在 property 里），激活方式因此变成可配置项——`covariance_activation` 就在同一函数里按 `rot_4d` 换成了 4D 版本（L53-56）。`inverse_opacity_activation` 的定义在 [utils/general_utils.py:L19-L20](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/general_utils.py#L19-L20)：`torch.log(x/(1-x))`，即 sigmoid 的反函数 logit。

#### 4.1.4 代码实践

见 4.2.4（合并在模块二的实践中，那里会在真实 `GaussianModel` 实例上打印这些属性）。

#### 4.1.5 小练习与答案

**练习 1**：`GaussianModel.__init__` 里为什么把属性初始化为 `torch.empty(0)` 而不是直接 `torch.zeros((1000, 3))` 之类？

**答案**：因为构造时还不知道高斯数量 `N`——它由初始化点云（COLMAP/MASt3R 点数经 `num_pts` 下采样）或检查点决定。空占位让同一个类能适配任意规模的场景；真正的形状分配延迟到 `create_from_pcd` / `load_ply` / `restore`。这也意味着**在 `create_from_pcd` 之前访问 `get_xyz.shape[0]` 会得到 0**，是常见的误用点。

**练习 2**：`_scaling` 存 log 空间、`_opacity` 存 logit 空间，两者有什么共同动机？

**答案**：都是「无约束存储 + 读取时激活」模式：`exp` 保证尺度恒正、`sigmoid` 保证不透明度落在 (0,1)，从而梯度下降可以自由更新裸参数而不违反物理约束，也省去每次更新后的投影/裁剪。

---

### 4.2 模块二：属性 property —— 读取时激活

#### 4.2.1 概念说明

渲染管线（u4-l1 详讲）消费的从来不是 `_opacity` 这类裸值，而是 `gaussians.get_opacity` 这样的 property。property 层是**裸参数世界与物理量世界之间唯一的翻译官**：任何代码想用高斯渲染一帧，都必须经过这里拿到合法的尺度、单位四元数、(0,1) 的不透明度和拼接好的完整球谐系数。

#### 4.2.2 核心流程

```text
create_from_pcd 赋初值（全部是裸值）
   _scaling = log(sqrt(dist2))        ← distCUDA2 给出的近邻距离
   _rotation = (1,0,0,0)              ← 单位四元数
   _opacity  = inverse_sigmoid(0.1)   ← logit(0.1) ≈ -2.197
        │
        ▼  读取时（property）
   get_scaling  = exp(_scaling)            → 恒正的轴长
   get_rotation = normalize(_rotation)     → 单位四元数
   get_opacity  = sigmoid(_opacity)        → (0,1)
   get_features = cat(_features_dc, _features_rest, dim=1)  → (N, (L+1)², 3)
   get_xyz      = _xyz                     → 原样（位置无约束）
```

反向传播时，梯度沿激活函数自动回传：\(\frac{\partial o}{\partial z} = \sigma(z)(1-\sigma(z))\)、\(\frac{\partial s}{\partial z} = e^{z}\)，因此裸参数的更新天然带上了激活函数的局部斜率——这正是 sigmoid 输出接近 0 或 1 时梯度变小、参数更新自动变慢的原因。

#### 4.2.3 源码精读

五个核心 property 全部集中在 [scene/gaussian_model.py:L193-L233](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L193-L233)：

```python
@property
def get_scaling(self):
    return self.scaling_activation(self._scaling)      # exp：log 尺度 → 正尺度

@property
def get_rotation(self):
    return self.rotation_activation(self._rotation)    # normalize：任意 4 向量 → 单位四元数

@property
def get_xyz(self):
    return self._xyz                                   # 位置无约束，原样返回

@property
def get_features(self):
    features_dc = self._features_dc
    features_rest = self._features_rest
    return torch.cat((features_dc, features_rest), dim=1)   # (N, (L+1)², 3)

@property
def get_opacity(self):
    return self.opacity_activation(self._opacity)      # sigmoid：logit → (0,1)
```

要点：

- **`get_xyz` 是唯一不做激活的**——3D 空间位置本身就是无约束量；
- **`get_features` 做的是拼接而非变换**：把 DC 项（1 个系数）与高阶项（\((L+1)^2-1\) 个）沿系数维拼成完整球谐，供光栅化器一次性求值视角相关颜色；
- 这些 property 每次访问都**重新计算**（没有缓存），因为裸参数每轮迭代都在变。

初值来自 `create_from_pcd`：[scene/gaussian_model.py:L406-L442](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L406-L442)

```python
fused_point_cloud = torch.tensor(np.asarray(pcd.points)).float().cuda()
fused_color = RGB2SH(torch.tensor(np.asarray(pcd.colors)).float().cuda())
features = torch.zeros((fused_color.shape[0], 3, self.get_max_sh_channels)).float().cuda()
features[:, :3, 0 ] = fused_color          # 颜色只填进 SH 的 DC 通道
...
dist2 = torch.clamp_min(distCUDA2(torch.from_numpy(np.asarray(pcd.points)).float().cuda()), 0.0000001)
scales = torch.log(torch.sqrt(dist2))[...,None].repeat(1, 3)   # ← log 空间！
rots = torch.zeros((fused_point_cloud.shape[0], 4), device="cuda")
rots[:, 0] = 1                            # 单位四元数 (1,0,0,0)
opacities = inverse_sigmoid(0.1 * torch.ones((fused_point_cloud.shape[0], 1), ...))

self._xyz = nn.Parameter(fused_point_cloud.requires_grad_(True))
self._features_dc = nn.Parameter(features[:,:,0:1].transpose(1, 2).contiguous()...)
self._features_rest = nn.Parameter(features[:,:,1:].transpose(1, 2).contiguous()...)
self._scaling = nn.Parameter(scales.requires_grad_(True))
self._rotation = nn.Parameter(rots.requires_grad_(True))
self._opacity = nn.Parameter(opacities.requires_grad_(True))
```

四个值得注意的初始化事实：

1. **尺度由近邻距离决定**：`distCUDA2`（simple-knn 子包，u1-l2 讲过）算出每点到最近邻的平方距离，`sqrt` 得距离，`log` 进裸值空间。直觉：初始椭球大小 ≈ 点与点的间距，刚好铺满点云不重叠。
2. **旋转初始化为恒等**：所有高斯初始都是「轴对齐」的椭球，姿态留给优化去学。
3. **不透明度统一初始化为 0.1**：存的是 `logit(0.1)=log(0.1/0.9)≈-2.197`。偏低的不透明度让初期每个高斯都「半透明」，避免单点遮挡整片区域。
4. **颜色经 `RGB2SH` 换算**：[utils/sh_utils.py:L225-L226](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/sh_utils.py#L225-L226) 定义 `RGB2SH(rgb) = (rgb - 0.5) / C0`，其中 `C0 = 0.28209479177387814`（[utils/sh_utils.py:L26](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/sh_utils.py#L26)）。球谐 DC 项乘 `C0` 即还原颜色，所以反着除回去就得到 DC 系数。

另外注意 `features[:,:,0:1].transpose(1, 2)` 这一步：内部临时张量是 `(N, 3, C)`（颜色在中间），转置后存成 `(N, C, 3)`（SH 在中间）。`transpose` 后内存不连续，所以紧跟 `.contiguous()`——光栅化器对内存布局有要求。

最后看这些属性如何被挂进优化器（`training_setup`，本讲只看 3D 部分）：[scene/gaussian_model.py:L484-L491](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L484-L491)

```python
l = [
    {'params': [self._xyz],          'lr': training_args.position_lr_init * self.spatial_lr_scale, "name": "xyz"},
    {'params': [self._features_dc],  'lr': training_args.feature_lr,      "name": "f_dc"},
    {'params': [self._features_rest],'lr': training_args.feature_lr / 20.0, "name": "f_rest"},
    {'params': [self._opacity],      'lr': training_args.opacity_lr,      "name": "opacity"},
    {'params': [self._scaling],      'lr': training_args.scaling_lr,      "name": "scaling"},
    {'params': [self._rotation],     'lr': training_args.rotation_lr,     "name": "rotation"}
]
```

六个裸参数各占一个参数组、各用各的学习率；`f_rest` 的学习率是 `f_dc` 的 1/20（高阶项要慢慢学，防止早期视角相关颜色压过几何）。带 `"name"` 的字典结构是后面 `replace_tensor_to_optimizer` / `_prune_optimizer` 按名字定位参数组的关键（u5-l4 详讲）。

#### 4.2.4 代码实践

**实践目标**：亲手构造一个 `gaussian_dim=3` 的 `GaussianModel`，从 1000 个随机点初始化，打印全部可优化属性的形状与数值范围，验证 4.2.2 的「裸值 → 激活」映射。

**操作步骤**：在仓库根目录新建临时脚本 `inspect_gaussian.py`（示例代码，跑完可删除；**不要**写进仓库提交）：

```python
# 示例代码：需要 GPU + 已编译的 simple-knn（create_from_pcd 内部调用 distCUDA2）
import numpy as np, torch
from scene.gaussian_model import GaussianModel
from utils.graphics_utils import BasicPointCloud

torch.manual_seed(0); np.random.seed(0)
N = 1000
pcd = BasicPointCloud(
    points=np.random.rand(N, 3).astype(np.float32) * 2,   # 随机点云，间距决定初始尺度
    colors=np.random.rand(N, 3).astype(np.float32),       # 随机颜色 ∈ [0,1)
    normals=np.zeros((N, 3), dtype=np.float32),
    time=None)

gs = GaussianModel(sh_degree=3, gaussian_dim=3)           # 本讲主角：3D 高斯
gs.create_from_pcd(pcd, spatial_lr_scale=1.0)

for name in ["get_xyz", "get_scaling", "get_rotation", "get_opacity", "get_features"]:
    t = getattr(gs, name)
    print(f"{name:12s} shape={tuple(t.shape)} "
          f"min={t.min().item():+.4f} max={t.max().item():+.4f}")

print("_scaling 裸值范围:", gs._scaling.min().item(), gs._scaling.max().item())
print("_opacity 裸值（应 ≈ logit(0.1) = -2.1972）:", gs._opacity[0].item())
print("get_opacity[0]（应 = 0.1）:", gs.get_opacity[0].item())
print("get_rotation 范数（应 = 1）:", gs.get_rotation.norm(dim=1).mean().item())
```

运行：`python inspect_gaussian.py`。

**需要观察的现象**：

1. `get_xyz` 形状 `(1000, 3)`；`get_scaling` `(1000, 3)`；`get_rotation` `(1000, 4)`；`get_opacity` `(1000, 1)`；`get_features` `(1000, 16, 3)`（`sh_degree=3` 时 \((L+1)^2=16\) 个 SH 系数，其中 DC 1 个 + rest 15 个）。
2. `get_scaling` 全为正（`exp` 的效果），而 `_scaling` 裸值可正可负。
3. `get_opacity` 恒等于 0.1（初始化统一值），`_opacity` 恒等于 \(\log(0.1/0.9)\approx-2.1972\)——这是「同一数量的两种表示」的直接证据。
4. `get_rotation` 每行范数恰为 1（恒等四元数 `(1,0,0,0)` 归一化后不变）。

**预期结果**：属性形状与上表完全一致；`get_opacity`/`_opacity` 满足 `sigmoid(logit(x))=x`。若在没有 GPU 的机器上运行，会在 `distCUDA2` 或 `.cuda()` 处报错——此时可退化为纯阅读实践：对照 [scene/gaussian_model.py:L422-L441](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L422-L441) 手算 `_opacity = log(0.1/0.9)` 并验证 `get_opacity` 公式。随机点云下的数值范围属「待本地验证」（`dist2` 依赖随机近邻距离）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `_scaling` 存 `log` 空间值而不是直接存尺度？

**答案**：两个原因。(a) **约束**：`exp` 保证读取值恒正，若直接存尺度，一次负向更新就会产生「负长度」椭球，协方差计算出错；(b) **优化几何**：尺度本质是乘性量（放大 1.1 倍比加 0.1 更自然），log 空间把乘性更新变成加性更新，且 `log` 空间中近邻距离初始化 `log(sqrt(dist2))` 数值稳定、跨量级场景（几毫米到几米）都能用同一套学习率工作。

**练习 2**：`get_features` 拼接后形状是 `(N, (L+1)², 3)`。`sh_degree=3` 时 `get_features`、`_features_dc`、`_features_rest` 各是什么形状？

**答案**：\((L+1)^2 = 16\)，所以 `get_features` 是 `(N, 16, 3)`；`_features_dc` 是 `(N, 1, 3)`（只含 DC 系数），`_features_rest` 是 `(N, 15, 3)`（\((L+1)^2-1=15\) 个高阶系数）。拼接发生在系数维 `dim=1`。

**练习 3**：初始化时所有高斯不透明度都是 0.1。如果改成 0.9，渲染初期可能出现什么现象？

**答案**：每个高斯几乎不透明，排在前面的高斯会完全遮挡后面的高斯，导致后排高斯几乎收不到有效梯度、优化难以展开；同时初期几何尚未对齐，错误的高强遮挡会让画面出现大块错误色块。0.1 的「半透明起步」让误差能分摊到多个高斯上逐步修正。（本仓库初始值见 [scene/gaussian_model.py:L434](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L434)。）

---

### 4.3 模块三：`capture` / `restore` 与 checkpoint

#### 4.3.1 概念说明

训练一个场景动辄几万次迭代，中途崩溃要能续训，训练完要能恢复出来渲染。4C4D 不把 `GaussianModel` 整个 pickle，而是提供一对互补的方法：

- `capture()`：把模型的**全部状态**按固定顺序装进一个 Python 元组；
- `restore(model_args, training_args)`：按同样顺序解包，逐个写回成员变量。

这是一个**手工序列化协议**：元组的顺序就是协议本身，两端必须严格一致。与之互补的还有 `save_ply`/`load_ply`（u3-l5 详讲）——PLY 只存几何与外观属性，**不存优化器状态**；`capture` 元组则连 Adam 的动量一起保存，这是「能续训」与「只能渲染」的分界线。

#### 4.3.2 核心流程

```text
保存（训练侧）
  gaussians.capture()  →  (active_sh_degree, _xyz, _features_dc, _features_rest,
                          _scaling, _rotation, _opacity, max_radii2D,
                          xyz_gradient_accum, denom,
                          optimizer.state_dict(), coef_optimizer.state_dict(),
                          spatial_lr_scale, coefficient.state_dict())
  torch.save((capture_tuple, iteration), "chkpnt_best.pth")

恢复（train.py 续训）                          恢复（render.py 推理）
  (model_params, it) = torch.load(ckpt)         (model_params, it) = torch.load(ckpt)
  gaussians.restore(model_params, opt)  ←training_args 有值，重建优化器并 load_state_dict
                                                gaussians.restore(model_params, None) ←跳过优化器
```

#### 4.3.3 源码精读

先看 3D 分支的 `capture`：[scene/gaussian_model.py:L98-L115](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L98-L115)

```python
def capture(self):
    if self.gaussian_dim == 3:
        return (
            self.active_sh_degree,
            self._xyz,
            self._features_dc,
            self._features_rest,
            self._scaling,
            self._rotation,
            self._opacity,
            self.max_radii2D,
            self.xyz_gradient_accum,
            self.denom,
            self.optimizer.state_dict(),
            None if self.coef_optimizer is None else self.coef_optimizer.state_dict(),
            self.spatial_lr_scale,
            None if self.coefficient is None else self.coefficient.state_dict(),
        )
```

14 个元素分四类：6 个可优化属性 + `active_sh_degree`、3 个统计量（`max_radii2D`/`xyz_gradient_accum`/`denom`）、2 个优化器状态字典（主优化器 + Coefficient 网络的 `coef_optimizer`，后者可为 `None`）、以及 `spatial_lr_scale`（场景半径，恢复续训时位置学习率调度依赖它）和 Coefficient 网络的 `state_dict`（4C4D 新增，注意 `None` 占位的写法——元组长度保持恒定）。

4D 分支在同一方法里（[L116-L139](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L116-L139)），额外塞入 `t_gradient_accum`、`_t`、`_scaling_t`、`_rotation_r`、`rot_4d`、`env_map`、`active_sh_degree_t`，共 21 个元素——u3-l2 会逐个对号。

`restore` 的解包与条件重建：[scene/gaussian_model.py:L141-L191](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L141-L191)

```python
def restore(self, model_args, training_args):
    if self.gaussian_dim == 3:
        (self.active_sh_degree, 
        self._xyz, 
        ...
        self.spatial_lr_scale,
        coefficient_dict) = model_args          # 按原顺序整体解包、直接写回成员
    ...
    if training_args is not None:
        self.training_setup(training_args)      # 重建优化器（参数组/lr）
        self.xyz_gradient_accum = xyz_gradient_accum
        self.t_gradient_accum = t_gradient_accum
        self.denom = denom
        self.optimizer.load_state_dict(opt_dict)  # 恢复 Adam 动量
        
        if coefficient_dict is not None and self.coefficient is not None:
            self.coefficient.load_state_dict(coefficient_dict)
        if self.coef_optimizer is not None:
            self.coef_optimizer.load_state_dict(coef_opt_dict)
```

三个关键点：

1. **解包即赋值**：Python 的多元组解包一行完成「按位置写回 14 个成员变量」，这正是顺序协议的风险所在——顺序错一位，全错且不报错。
2. **`training_args` 是否为 `None` 决定恢复深度**：`None` 时只还原属性（推理够用）；非 `None` 时先 `training_setup` 重建优化器骨架，再 `load_state_dict` 灌入动量，才能无损续训。
3. **`gaussian_dim` 必须两端一致**：用 `gaussian_dim=3` 构造的模型去 restore 一个 4D 元组（21 个元素）会在解包处抛 `ValueError: too many values to unpack`。这正是 u1-l3 提过的「train.py 默认 `gaussian_dim=4`、render.py 默认 3，一致性靠同一份 yaml 保证」的底层原因。

调用方两端。训练侧保存（train.py，测试指标创新高时）：[train.py:L271-L281](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L271-L281)

```python
# Save chkpnt
if (iteration in testing_iterations):
    if test_psnr >= best_psnr:
        best_psnr = test_psnr
        print("\n[ITER {}] Saving best checkpoint".format(iteration))
        torch.save((gaussians.capture(), iteration), scene.model_path + "/chkpnt_best.pth")
```

注意 `torch.save` 的对象是**二元组** `(capture元组, iteration)`——恢复端先解外层二元组拿到迭代号，再把它整个交给 `restore`。训练开头续训的入口在 [train.py:L69-L72](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L69-L72)：`gaussians.restore(model_params, opt)` 传入真实优化参数。推理侧则传 `None`：[render.py:L52-L60](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/render.py#L52-L60) 中 `gaussians.restore(model_params, None)`（u7-l1 详讲）。

补充一个容易混淆的点：`save_ply`（[L349-L398](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L349-L398)）写出的 `point_cloud/iteration_N/point_cloud.ply` 存的是**激活前的裸值**（`xyz = self._xyz.detach()...`、`opacity = self._opacity...`），`load_ply` 读回来也直接挂成裸参数——裸值才是这个代码库里一切持久化（PLY 与 checkpoint）的共同表示，激活永远只发生在读取 property 的一瞬间。

#### 4.3.4 代码实践

**实践目标**：亲手验证 capture/restore 的「顺序协议」与 `training_args=None` 的行为差异。

**操作步骤**（示例代码，接 4.2.4 的脚本继续）：

1. 在 4.2.4 的 `inspect_gaussian.py` 末尾追加：

```python
# —— 实践 B：capture 元组结构 ——
import itertools
class DummyArgs:   # training_setup 需要的最小参数集（示例代码）
    percent_dense = 0.01; position_lr_init = 1e-4; position_lr_final = 1e-6
    position_lr_delay_mult = 0.01; position_lr_max_steps = 1000
    feature_lr = 2.5e-3; opacity_lr = 5e-2; scaling_lr = 5e-3; rotation_lr = 1e-3
    position_t_lr_init = -1; coefficient_lr = 1e-3; coefficient_weight_decay = 0.

gs.training_setup(DummyArgs())               # 先建优化器，否则 capture 里 state_dict 报错
tup = gs.capture()
names = ["active_sh_degree","_xyz","_features_dc","_features_rest","_scaling",
         "_rotation","_opacity","max_radii2D","xyz_gradient_accum","denom",
         "optimizer.state_dict","coef_opt","spatial_lr_scale","coefficient_dict"]
for n, v in zip(names, tup):
    desc = tuple(v.shape) if torch.is_tensor(v) else type(v).__name__
    print(f"[{n:22s}] {desc}")

# —— 实践 C：restore 双模式 ——
gs2 = GaussianModel(sh_degree=3, gaussian_dim=3)
gs2.restore(tup, None)                       # 推理式恢复：不重建优化器
print("restore(None) 后 get_opacity 是否一致:",
      torch.allclose(gs2.get_opacity, gs.get_opacity))
print("gs2.optimizer =", gs2.optimizer)      # 预期仍是 None

gs2.training_setup(DummyArgs())              # 手工补建优化器（绕开下一行的坑，见观察 3）
print("优化器参数组数:", len(gs2.optimizer.param_groups))  # 预期 6

# 观察 3：3D 模型 + 续训式恢复（training_args 非 None）——预期在这里抛异常！
gs3 = GaussianModel(sh_degree=3, gaussian_dim=3)
gs3.training_setup(DummyArgs())
try:
    gs3.restore(tup, DummyArgs())            # 续训式恢复
    print("restore(opt) 成功")
except Exception as e:
    print("restore(opt) 抛出:", type(e).__name__, "-", e)
```

2. 运行 `python inspect_gaussian.py`。

**需要观察的现象**：

- `capture()` 返回 14 个元素：10 个张量（形状带 `N=1000`）、`optimizer.state_dict`（dict 类型）、`coef_opt=None`、`spatial_lr_scale=1.0`、`coefficient_dict=None`；
- `restore(tup, None)` 后 `gs2` 的所有 `get_*` 与原模型逐元素一致，但 `gs2.optimizer is None`（没碰优化器）；手工 `training_setup` 后优化器有 6 个参数组（对应 L484-491 的六个属性）；
- **`gs3.restore(tup, DummyArgs())` 会抛 `UnboundLocalError`，提示局部变量 `t_gradient_accum` 未定义**。原因在源码：`restore` 中 [scene/gaussian_model.py:L180-L184](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L180-L184) 无条件执行 `self.t_gradient_accum = t_gradient_accum`，而 `t_gradient_accum` 这个名字**只在 4D 分支的解包元组**（L167）里被绑定，3D 分支（L142-156）没有它——`gaussian_dim=3` 且 `training_args` 非 `None` 的组合是一条从未被 4C4D 走过的死路（train.py 训练恒为 `gaussian_dim=4`，render.py 推理恒传 `None`），所以这个潜伏错误从未暴露。这也再次印证 u1-l3 的提醒：读继承代码时要能识别遗留分支。

**预期结果**：如上。`restore(tup, None)` 那一行能跑通的前提是元组里 `opt_dict` 不是 `None`——本例中先 `training_setup` 过所以是合法 dict，只是被跳过不用。整段需要 GPU（依赖实践 A 的 `create_from_pcd`）；无 GPU 时标记「待本地验证」，改为对照源码数一遍 L100-115 的元组元素个数与顺序，并静态推演观察 3 的 `UnboundLocalError`。

#### 4.3.5 小练习与答案

**练习 1**：`chkpnt_best.pth` 和 `point_cloud/iteration_N/point_cloud.ply` 都能恢复高斯，本质区别是什么？

**答案**：`.pth` 由 `capture()` 生成，除 6 个可优化属性外还含 `optimizer.state_dict()`（Adam 的一二阶动量）、统计量、`spatial_lr_scale` 和 Coefficient 网络权重，**可以无损续训**；`.ply` 由 `save_ply` 生成，只存几何/外观裸值（x,y,z,f_dc,f_rest,opacity,scale,rot,...），**只能渲染或作为初始化，不能续训**。

**练习 2**：如果用 `gaussian_dim=4` 训练出的 `chkpnt_best.pth`，去恢复一个 `gaussian_dim=3` 的 `GaussianModel`，会发生什么？

**答案**：`capture` 的 4D 元组有 21 个元素而 `restore` 的 3D 分支只解包 14 个，解包时抛 `ValueError`（too many values to unpack）。即使元素数碰巧对上，顺序错位也会把 `_t` 之类的张量写进错误成员。所以恢复端的 `gaussian_dim/sh_degree` 等构造参数必须与保存端一致——实践中靠同一份 yaml 配置保证（见 u1-l4）。

**练习 3**：为什么 `restore` 里要先 `self.training_setup(training_args)` 再 `self.optimizer.load_state_dict(opt_dict)`，而不是直接把优化器对象存进元组？

**答案**：`training_setup` 依据**当前**高斯数量与参数结构重建优化器骨架（参数组、名字、学习率），`load_state_dict` 只负责把保存的动量灌进这个骨架。直接 pickle 优化器对象会绑定保存时的参数对象与设备，跨进程/跨设备恢复脆弱；state_dict 是与对象解耦的纯数据，更可移植。此外 `Scene`（u2-l4）在构造阶段也可能已经 `create_from_pcd`，先重建再灌入能保证形状对齐。

## 5. 综合实践

把本讲三个模块串起来，完成一份《GaussianModel 3D 属性体检报告》：

1. **建模型**：用 4.2.4 的随机点云（N=1000）构造 `gaussian_dim=3`、`sh_degree=3` 的模型并 `create_from_pcd`。
2. **填表**：写一个循环遍历 6 个可优化属性，对每个属性分别记录——裸参数（`gs._xxx`）的形状/最小值/最大值，与对应 `get_*` property 的形状/最小值/最大值，整理成一张对照表。
3. **验证激活**：任取第 0 个高斯，手工计算 `torch.exp(gs._scaling[0])`、`torch.sigmoid(gs._opacity[0])`、`gs._rotation[0]/gs._rotation[0].norm()`，确认与 `get_scaling[0]`、`get_opacity[0]`、`get_rotation[0]` 完全相等。
4. **走一遍序列化**：`training_setup` → `capture()` → 数元组元素个数（预期 14）→ 新建模型 `restore(tup, None)` → 用第 3 步同样的三个 get_* 断言恢复前后逐元素相等。
5. **回答一个开放问题**（写进报告结尾）：`save_ply` 存裸值、property 读时激活——如果你想在 PLY 里直接存「真实尺度」而非 log 值，需要改动哪两个函数？这样的 PLY 还能被本项目 `load_ply` 读回吗？

预期成果：一张属性对照表 + 一段 200 字的结论，说明「裸值存储、读时激活、捕获还原」三者如何配合。全程需要 GPU 与编译好的 `simple-knn`；不具备环境时，第 2-3 步改为源码阅读推演并标注「待本地验证」。

## 6. 本讲小结

- `GaussianModel` 采用「先造容器、后填数据」：`__init__` 只放 `torch.empty(0)` 占位，真实形状由 `create_from_pcd`/`load_ply`/`restore` 决定。
- 3D 高斯的 6 个可优化属性：`_xyz (N,3)`、`_features_dc (N,1,3)`、`_features_rest (N,(L+1)²-1,3)`、`_scaling (N,3)`、`_rotation (N,4)`、`_opacity (N,1)`，另有 `max_radii2D`/`xyz_gradient_accum`/`denom` 三个不可优化统计量。
- 一切持久化与优化都在**裸值空间**：`_scaling` 是 log 尺度、`_opacity` 是 logit、`_rotation` 未归一化；`exp/sigmoid/normalize` 三个激活函数只在 `get_*` property 读取的一瞬间施加，`get_xyz` 是唯一无激活的属性。
- 初始化约定：尺度取近邻距离 `log(sqrt(distCUDA2))`、旋转取恒等四元数、不透明度统一 `logit(0.1)≈-2.197`、颜色经 `RGB2SH=(rgb-0.5)/C0` 填入 SH 的 DC 通道。
- `capture()`/`restore()` 是一份**顺序敏感的手工序列化协议**：3D 分支 14 个元素、4D 分支 21 个；`training_args` 是否为 `None` 区分「续训恢复」与「推理恢复」；`.pth` 存优化器动量而 `.ply` 不存，这是续训能力与渲染能力的分界线。

## 7. 下一步学习建议

- **下一讲 u3-l2（第四维：`_t`、`_scaling_t` 与 `_rotation_r`）**：本讲刻意跳过的 4D 属性将逐一登场，你会看到 `gaussian_dim == 4` 分支如何贯穿 `__init__`、`capture`、`training_setup` 与致密化。
- **提前翻两处**：`setup_functions` 里的 `build_covariance_from_scaling_rotation`（[scene/gaussian_model.py:L29-L33](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L29-L33)，尺度+旋转如何变成协方差）为 u3-l3 的 4D 协方差做铺垫；`inverse_opacity_activation` 在 `reset_opacity`（[L523-L526](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L523-L526)）与 u6 的 `opacity_decay` 中反复出现——logit 空间写回是贯穿全项目的惯用法。
- 若想巩固本讲，可回头对照 u2-l4 中 `Scene` 三分支初始化（`create_from_pcd`/`load_ply`/`create_from_pth`）与本讲的属性赋值代码，确认三条路径最终都落到同一组 `nn.Parameter` 上。
