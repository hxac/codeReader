# u3-l2 第四维：_t、_scaling_t 与 _rotation_r

## 1. 本讲目标

上一讲（u3-l1）我们梳理了 `GaussianModel` 中继承自 3DGS 的六个 3D 属性（`_xyz`、`_features_dc`、`_features_rest`、`_scaling`、`_rotation`、`_opacity`）以及「裸值存储 + 读时激活」的模式。本讲把时间维补上。读完本讲，你应该能够：

1. 说出 `_t`、`_scaling_t`、`_rotation_r` 各自的物理含义、张量形状、以及它们分别在什么条件下被填充。
2. 理解 `time_duration` 参数化：为什么时间必须在一个统一的区间（如 `[0, 10)`）内，相机 timestamp、点云 time 字段、模型初始化三方如何共享这个域。
3. 追踪 `gaussian_dim == 4` 分支在 `scene/gaussian_model.py` 中的全部落点——从 `__init__`、`capture`/`restore` 到 `prune_points`、`densification_postfix`。
4. 对比 `gaussian_dim=3` 与 `gaussian_dim=4` 时 `training_setup` 生成的优化器参数组，说清新参数组的学习率来源，以及 `position_t_lr_init = -1` 时的回退逻辑。

## 2. 前置知识

### 2.1 从 3D 高斯到 4D 高斯

3DGS 用一簇各向异性 3D 高斯表达静态场景，每个高斯在空间中的密度为：

\[
G(\mathbf{x}) = \exp\left(-\frac{1}{2}(\mathbf{x}-\mu)^\top \Sigma^{-1} (\mathbf{x}-\mu)\right), \quad \mathbf{x}\in\mathbb{R}^3
\]

4DGS 把高斯从 \(\mathbb{R}^3\) 搬到 \(\mathbb{R}^4\)：坐标变成 \((x, y, z, t)\)，均值变成 \((\mu_{xyz}, \mu_t)\)，协方差从 3×3 变成 4×4。于是多了三类时间相关的可优化量：

- **时间中心 \(\mu_t\)**：这个高斯「活在」哪个时刻——对应属性 `_t`；
- **时间尺度 \(\sigma_t\)**：这个高斯在时间上「活多久」——对应属性 `_scaling_t`；
- **时间旋转**：4D 协方差中空间轴与时间轴的耦合关系——对应属性 `_rotation_r`。

把 4D 高斯在固定时刻 \(t\) 「切片」得到 3D 高斯，是渲染时的事（u3-l3 详讲）。本讲只关心这些属性如何被存储、激活、优化和持久化。

### 2.2 时间高斯：`get_marginal_t` 的一维直觉

单独看时间维，4D 高斯在每个高斯上退化为一个一维高斯：

\[
p(t) \propto \exp\left(-\frac{(t-\mu_t)^2}{2\sigma_t^2}\right)
\]

这正是 [scene/gaussian_model.py:252-254](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L252-L254) 中 `get_marginal_t` 的实现——渲染时间离高斯的时间中心越远，它对这一帧的贡献按高斯衰减。记住这个公式，`_t` 与 `_scaling_t` 的物理角色就一目了然。

### 2.3 双四元数表示 4D 旋转（只需知道结论）

3D 旋转用一个单位四元数表示；4D 旋转（SO(4)）需要**两个**单位四元数（左四元数与右四元数）共同表示。项目中：

- `_rotation` 继续扮演空间部分（3D 时是唯一的旋转；4D 时是双四元数中的左四元数）；
- `_rotation_r` 是新增的右四元数，**仅当 `rot_4d=True` 时才存在**。

两者的矩阵化由 [utils/general_utils.py:113-133](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/general_utils.py#L113-L133) 的 `build_rotation_4d` 完成（`M_l @ M_r` 再翻转），本讲不展开推导。直觉上：`rot_4d=False` 时空间椭球和时间轴是「垂直」的（高斯只在自己的时间窗内淡入淡出，形状不变）；`rot_4d=True` 时椭球可以在时空里「倾斜」，从而用少量高斯表达运动模糊般的连续运动。

### 2.4 优化器参数组

PyTorch 允许给 `torch.optim.Adam` 传一个列表，每个元素是一组参数及其专属学习率：`[{'params': [...], 'lr': 0.001, 'name': 'xxx'}, ...]`。3DGS/4C4D 把每一类高斯属性放进独立的参数组，这样同一轮迭代中位置、颜色、不透明度可以用完全不同的步长更新。理解这一点是读懂 `training_setup` 的前提。

## 3. 本讲源码地图

| 文件 | 本讲关注点 |
| --- | --- |
| [scene/gaussian_model.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py) | 主战场：`__init__`、property、`training_setup`、`capture`/`restore`、`prune_points`、`densification_postfix` |
| [train.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py) | 脚本级 4D 开关（`--gaussian_dim`、`--time_duration`、`--rot_4d`、`--force_sh_3d`）及 `GaussianModel` 的构造点 |
| [arguments/__init__.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/arguments/__init__.py) | `position_t_lr_init`、`densify_grad_t_threshold` 等时间维超参的注册与默认值 |
| [configs/dynerf/flame_steak.yaml](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/configs/dynerf/flame_steak.yaml) | 一个真实的 4D 配置样例（`rot_4d: True`、`time_duration: [0, 10]`） |
| [utils/general_utils.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/general_utils.py) | `build_rotation_4d` / `build_scaling_rotation_4d`（4D 旋转的矩阵化） |
| [scene/dataset_readers.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py) | timestamp 归一化与点云 time 过滤（time_duration 的另外两处消费） |
| [utils/sh_utils.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/sh_utils.py) | `sh_channels_4d = [1, 6, 16, 33]`（`get_max_sh_channels` 的查表来源） |

## 4. 核心概念与源码讲解

### 4.1 `GaussianModel.__init__`：第四维三件属性与开关体系

#### 4.1.1 概念说明

`__init__` 是 4D 属性的「户口登记处」。与 u3-l1 讲过的六个 3D 属性并列，这里新登记了三个时间维属性和五个开关/状态量。要理解的核心是：**`__init__` 只负责「占位」和「记录开关」，真正的数值要等到 `create_from_pcd`（u3-l5）或 `load_ply` 才填进来**——这是从 3DGS 继承的延迟填充模式。

| 名称 | 形状（N 为高斯数） | 裸值含义 | 读时激活 | 填充时机 |
| --- | --- | --- | --- | --- |
| `_t` | (N, 1) | 时间中心 \(\mu_t\)（原值，log/无变换） | 无（同 `_xyz`） | `create_from_pcd` / `load_ply`，`gaussian_dim==4` 时 |
| `_scaling_t` | (N, 1) | \(\log\sigma_t\) | `torch.exp` | 同上 |
| `_rotation_r` | (N, 4) | 右四元数（未归一化） | `F.normalize` | 仅 `rot_4d=True` 时（`create_from_pcd` / `load_ply`） |

注意 `_t` 和 `_xyz` 一样是「直接存储、无需激活」的属性——时间坐标本身没有非负或饱和约束；而 `_scaling_t` 与 `_scaling` 一样存 log 空间（保证 `exp` 后尺度恒正），`_rotation_r` 与 `_rotation` 一样存裸四元数（读取时归一化到单位球）。

#### 4.1.2 核心流程

```text
GaussianModel(sh_degree, gaussian_dim, time_duration, rot_4d, force_sh_3d, sh_degree_t, coefficient)
 ├─ 1. 登记 3D 属性占位（u3-l1 已讲：_xyz/_features_*/_scaling/_rotation/_opacity = empty）
 ├─ 2. 登记 4D 开关与属性占位
 │    ├─ self.gaussian_dim = gaussian_dim          # 3 或 4
 │    ├─ self._t / self._scaling_t = empty         # 时间中心 / 时间尺度占位
 │    ├─ self.time_duration = time_duration        # 时间域 [t0, t1]
 │    ├─ self.rot_4d = rot_4d
 │    ├─ self._rotation_r = empty                  # 右四元数占位
 │    ├─ self.force_sh_3d = force_sh_3d
 │    └─ self.t_gradient_accum = empty             # 时间梯度累计器（致密化用）
 ├─ 3. 合法性断言：rot_4d 或 force_sh_3d ⇒ 必须 gaussian_dim==4
 ├─ 4. 登记 4D 球谐开关：active_sh_degree_t = 0, max_sh_degree_t = sh_degree_t
 ├─ 5. self.coefficient = coefficient（Neural Decaying Function 网络，u6 详讲）
 └─ 6. setup_functions()：按 rot_4d 选择协方差激活函数（3D 版 / 4D 版）
```

#### 4.1.3 源码精读

先看构造函数签名，七个参数里五个与第四维有关：

[scene/gaussian_model.py:63](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L63)——构造函数签名：`gaussian_dim` 默认 3、`time_duration` 默认 `[-0.5, 0.5]`、`rot_4d` / `force_sh_3d` 默认 `False`、`sh_degree_t` 默认 0。注意这些默认值只是库层面的占位；入口 `train.py` 的脚本级默认值不同（见 4.2.3）。

[scene/gaussian_model.py:80-90](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L80-L90)——4D 属性登记段：

```python
self.gaussian_dim = gaussian_dim
self._t = torch.empty(0)
self._scaling_t = torch.empty(0)
self.time_duration = time_duration
self.rot_4d = rot_4d
self._rotation_r = torch.empty(0)
self.force_sh_3d = force_sh_3d
self.t_gradient_accum = torch.empty(0)
if self.rot_4d or self.force_sh_3d:
    assert self.gaussian_dim == 4
self.env_map = torch.empty(0)
```

要点逐条说明：

- 三个新属性与 `xyz_gradient_accum` 一样先以 `torch.empty(0)` 占位，等初始化函数填充。
- `t_gradient_accum` 是 4D 专属的**致密化统计器**：3D 只累计屏幕空间梯度（`xyz_gradient_accum`），4D 还要累计 `_t` 的梯度（`add_densification_stats` 见 4.4.3）。
- 断言（第 88-89 行）：`rot_4d` 和 `force_sh_3d` 都只在 4D 下有意义，传 `gaussian_dim=3` 加任一开关会直接 `AssertionError`，而不是静默忽略。
- `env_map` 是球面背景贴图占位（u4-l3 讲），与本讲无关但同在这段登记。

[scene/gaussian_model.py:92-93](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L92-L93)——登记 4D 球谐开关：`active_sh_degree_t = 0`、`max_sh_degree_t = sh_degree_t`。它们与 `force_sh_3d` 共同决定球谐通道数（见下）。

[scene/gaussian_model.py:53-56](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L53-L56)——`setup_functions` 里按 `rot_4d` 二选一：`False` 用 3D 的 `build_covariance_from_scaling_rotation`（协方差由 3 维尺度+3D 旋转构成，时间维独立衰减）；`True` 用 4D 的 `build_covariance_from_scaling_rotation_4d`（协方差是完整 4×4，含时空耦合项，由左右双四元数构造）。这决定了渲染时「切片」公式的形状，推导留给 u3-l3。

[scene/gaussian_model.py:235-242](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L235-L242)——`get_max_sh_channels` 是开关体系影响特征张量宽度的地方：

```python
if self.gaussian_dim == 3 or self.force_sh_3d:
    return (self.max_sh_degree+1)**2                      # 纯 3D 球谐：sh_degree=3 → 16
elif self.gaussian_dim == 4 and self.max_sh_degree_t == 0:
    return sh_channels_4d[self.max_sh_degree]             # 查表 [1, 6, 16, 33] → 33
elif self.gaussian_dim == 4 and self.max_sh_degree_t > 0:
    return (self.max_sh_degree+1)**2 * (self.max_sh_degree_t + 1)
```

也就是说：`force_sh_3d=True` 时即便 `gaussian_dim=4`，球谐也退回 3D 通道数（16），时间维不引入额外颜色通道——这是一个「要时间维但不要时间相关颜色」的省钱开关。`sh_degree_t` 的来源在 [train.py:62-63](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L62-L63)：`sh_degree_t=2 if pipe.eval_shfs_4d else 0`，即只有开启 4D 球谐渲染评估时才有时间阶数。球谐本身是 u3-l4 的主题，这里只需记住「三个开关共同决定 `_features_rest` 的通道数」。

最后看入口如何传参：[train.py:390-397](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L390-L397) 注册了脚本级开关 `--gaussian_dim`（默认 4）、`--time_duration`（默认 `[0, 10.0]`）、`--rot_4d`（store_true）、`--force_sh_3d`（store_true）；而 [configs/dynerf/flame_steak.yaml:1-6](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/configs/dynerf/flame_steak.yaml#L1-L6) 的典型取值是 `gaussian_dim: 4`、`time_duration: [0.0, 10.0]`、`rot_4d: True`、`force_sh_3d: False`。按 u1-l4 讲过的优先级，yaml 会覆盖命令行默认值。

#### 4.1.4 代码实践

**实践目标**：用三个「反例」验证 `__init__` 的断言与占位行为，直观感受开关约束。

**操作步骤**（在仓库根目录执行，仅依赖 torch，无需 GPU/CUDA 扩展）：

```python
# 示例代码：probe_init.py（本讲新写，非项目原有文件）
import torch
from scene.gaussian_model import GaussianModel

# 1) 合法的 4D + rot_4d 构造
g = GaussianModel(sh_degree=3, gaussian_dim=4, time_duration=[0, 10.0], rot_4d=True)
print("gaussian_dim =", g.gaussian_dim)
print("_t shape =", g._t.shape, "_scaling_t shape =", g._scaling_t.shape, "_rotation_r shape =", g._rotation_r.shape)
print("max_sh_channels =", g.get_max_sh_channels)   # 期望 33

# 2) 非法组合：3D + rot_4d → 断言失败
try:
    GaussianModel(sh_degree=3, gaussian_dim=3, rot_4d=True)
except AssertionError as e:
    print("AssertionError as expected:", e)

# 3) force_sh_3d=True 时通道数退回 16
g2 = GaussianModel(sh_degree=3, gaussian_dim=4, force_sh_3d=True)
print("force_sh_3d channels =", g2.get_max_sh_channels)   # 期望 16
```

**需要观察的现象**：

1. 步骤 1 中三个新属性形状都是 `torch.Size([0])`——只是占位，尚未实例化任何高斯。
2. 步骤 2 抛出 `AssertionError`（注意：`assert` 语句本身没有消息文本，异常信息为空字符串）。
3. 步骤 3 输出 16，步骤 1 输出 33。

**预期结果**：三步全部如上所述。（在 CPU 环境即可验证；构造 `GaussianModel` 不触发任何 `.cuda()` 调用。若在你环境中运行结果与此不符，请以源码为准并反馈。）

#### 4.1.5 小练习与答案

**练习 1**：为什么 `_t` 不需要激活函数，而 `_scaling_t` 必须过 `torch.exp`？

**答案**：`_t` 是时间坐标，理论上可正可负、无界，直接优化原值即可（与 `_xyz` 同理）；`_scaling_t` 是尺度，必须恒正，若直接优化原值容易在梯度下降中变负，因此存 log 空间（`log σ_t`），读取时 `exp` 回来保证正性。这正是 u3-l1 讲过的「裸值存储 + 读时激活」模式在时间维的延续（见 [scene/gaussian_model.py:197-199](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L197-L199) 的 `get_scaling_t`）。

**练习 2**：`rot_4d=True` 但 `gaussian_dim=3` 会发生什么？为什么代码选择断言而不是忽略开关？

**答案**：在 `__init__` 第 88-89 行直接 `AssertionError`。因为 `_rotation_r` 只在 4D 分支被填充和消费（渲染、致密化、保存），3D 下开了开关会产生「声明了却永远为空」的属性，下游所有 `gaussian_dim == 4` 分支都会踩到空张量，与其在几百次迭代后才崩溃，不如在构造时就拒绝。

**练习 3**：`force_sh_3d` 与 `sh_degree_t=0` 都能让球谐不含时间维，二者有何区别？

**答案**：`sh_degree_t=0` 走查表分支 `sh_channels_4d[max_sh_degree]`（sh_degree=3 时为 33），这是 4DGS 原生的通道布局，通道数比纯 3D 的 16 多，为时间相关颜色预留了位置；`force_sh_3d=True` 则完全退回 \((sh\_degree+1)^2=16\) 个通道，特征张量更小、计算更省。前者仍允许时间影响颜色，后者强制颜色与时间无关。

---

### 4.2 `time_duration`：一个必须三方同域的时间参数化

#### 4.2.1 概念说明

时间本身没有天然单位：帧号是 0~299 的整数，秒是 0~10 的浮点。代码必须选定一个**统一的时间域**，让三件事落在同一坐标系里：

1. **相机的 timestamp**（渲染时问「现在是第几帧」）；
2. **点云的 time 字段**（初始化时问「这个点属于哪个时刻」）；
3. **模型的 `time_duration`**（初始化 `_t` 与时间尺度时问「整个视频的时间范围是多少」）。

4C4D 的约定是：**`time_duration = [0, 10]`，帧号 f 映射为 `10·f/(F_max+1)`，恰好铺满 `[0, 10)`**。任何一方偏离这个域（比如点云的 time 用了帧号 0~299 而不是 0~10），高斯的时间中心和相机的时间戳就永远对不上，训练会静默地学出一团糟。

#### 4.2.2 核心流程

```text
time_duration 的三处消费
 ├─ 相机侧：process_camera_info
 │    time_stamp = 帧号 / ((max_timestamp + 1) / 10)   →  落在 [0, 10)
 ├─ 点云侧：readColmapSceneInfo
 │    time_mask = (times < duration[1]) & (times > duration[0])   →  丢弃域外点
 └─ 模型侧：create_from_pcd
      ├─ 无 time 信息时：在 [t0 - r·L/2, t1 + r·L/2] 均匀随机采 _t   （L = t1 - t0, r = redundant_ratio）
      └─ 初始时间尺度 σ_t = (t1 - t0) / 5
```

其中模型侧的随机采样公式为：

\[
\mu_t \sim \mathcal{U}\left(t_0 - \frac{rL}{2},\; t_1 + \frac{rL}{2}\right), \quad L = t_1 - t_0,\; r = \text{redundant\_ratio}
\]

`redundant_ratio`（默认 0.2）让初始高斯的时间中心**略微超出**视频区间——视频开头结尾的时刻也有高斯「覆盖到」，边界帧不至于无高斯可用。

#### 4.2.3 源码精读

[scene/dataset_readers.py:204](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L204)——相机侧归一化：`time_stamp = int(img.split('.')[0][-4:]) / ((max_timestamp + 1.0) / 10.0)`。从文件名 `camXX_YYYY.png` 取 4 位帧号，除以 `(F_max+1)/10`。例如 300 帧时 `F_max=299`，第 299 帧的 timestamp 是 `299/30.0 ≈ 9.97 < 10`。（这一机制在 u2-l2 已详细讲过，这里作为「三方同域」的其中一方回顾。）

[scene/dataset_readers.py:338-344](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L338-L344)——点云侧过滤：点云自带 time 字段时，下采样后用 `time_duration` 做开区间过滤，域外的点直接丢弃。这是「点云时间域必须与 duration 一致」的强制执行点——若你的点云 time 存的是帧号（0~299）而 duration 是 [0,10]，这一步会把**所有点全部过滤掉**，下一行构造空点云，训练在初始化阶段就异常。

[scene/gaussian_model.py:413-418](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L413-L418)——模型侧随机初始化 `_t`：

```python
if self.gaussian_dim == 4:
    if pcd.time is None:
        fused_times = (torch.rand(fused_point_cloud.shape[0], 1, device="cuda") * (1.0 + redundant_ratio) - (redundant_ratio / 2.0)) * (self.time_duration[1] - self.time_duration[0]) + self.time_duration[0]
        print(f"No time information provided, using random time values with redundant ratio {redundant_ratio}.")
    else:
        fused_times = torch.from_numpy(pcd.time).cuda().float()
```

点云没有 time 字段时（例如 COLMAP 稀疏点云），在略扩大的时间域内均匀随机撒 `_t`；有 time 字段时（例如 MASt3R 按帧重建的点云）直接采用。

[scene/gaussian_model.py:426-429](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L426-L429)——初始时间尺度：`dist_t = duration/5`，再取 `log(sqrt(dist_t))` 存入 `_scaling_t` 裸值。直觉：每个初始高斯的时间标准差约为视频时长的 1/5，即每个高斯大约「覆盖」2/5 时长（±2σ），初始时约需要 5 个以上高斯在时间上接力才能覆盖整段视频——后续靠致密化与优化细化。

[train.py:51-52](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L51-L52)——`frame_ratio > 1`（抽帧训练）时把 `time_duration` 两端除以 `frame_ratio`，保持时间域与抽帧后的 timestamp 对应（Blender 数据集侧配套的除法见 [scene/dataset_readers.py:367-368](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L367-L368)）。

最后提醒默认值差异：[scene/gaussian_model.py:63](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L63) 的签名默认是 `[-0.5, 0.5]`（继承自 4DGS 上游），而 [train.py:391](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L391) 的入口默认是 `[0, 10.0]`，且 4C4D 的 N3V 数据链路按 `[0,10)` 归一化。**直接用默认构造函数而不传 `time_duration` 会得到与数据不匹配的域**——这是脱离 train.py 单独实例化 `GaussianModel` 时最容易踩的坑。

#### 4.2.4 代码实践

**实践目标**：不依赖 GPU，用 numpy 复现 `_t` 随机初始化公式，观察 `redundant_ratio` 对时间覆盖范围的影响。

**操作步骤**（示例代码，非项目原有文件）：

```python
# probe_time_duration.py
import numpy as np

rng = np.random.default_rng(0)
duration = [0.0, 10.0]          # 与 flame_steak.yaml 一致
redundant_ratio = 0.2           # train.py --redundant_ratio 默认值
N = 100000

# 复现 gaussian_model.py 第 415 行
u = rng.random((N, 1))
fused_times = (u * (1.0 + redundant_ratio) - redundant_ratio / 2.0) * (duration[1] - duration[0]) + duration[0]
print("min =", fused_times.min(), "max =", fused_times.max())   # 期望约 -1.0 ~ 11.0
print("超出 [0,10] 的比例 =", ((fused_times < 0) | (fused_times > 10)).mean())  # 期望约 0.2/1.2

# 初始时间尺度
sigma_t = (duration[1] - duration[0]) / 5
print("sigma_t =", sigma_t, " log 存储 =", np.log(np.sqrt(sigma_t)))
```

**需要观察的现象**：`min`/`max` 约为 `-1.0`/`+11.0`；域外比例约 `0.2/1.2 ≈ 16.7%`；`sigma_t = 2.0`，对应裸值 `log(sqrt(2)) ≈ 0.3466`。

**预期结果**：与上面的数值吻合即可确认你读懂了第 415 行与第 428 行。（纯 numpy，可直接运行验证。）

#### 4.2.5 小练习与答案

**练习 1**：300 帧（帧号 0000~0299）的数据，timestamp 的最小值和最大值各是多少？

**答案**：`max_timestamp = 299`，分母 `(299+1)/10 = 30.0`；第 0 帧为 0，第 299 帧为 `299/30 ≈ 9.967`。所以 timestamp 落在 `[0, 9.967] ⊂ [0, 10)`，与 `time_duration=[0,10]` 同域。

**练习 2**：如果点云 PLY 的 time 字段存的是帧号 0~299，而 `time_duration=[0,10]`，会发生什么？

**答案**：[scene/dataset_readers.py:339](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L339) 的 `time_mask` 会把 time>10 的点（帧号 11 以后的所有点）全部过滤掉，点云数量骤减甚至为空；即使侥幸剩几个点，它们的 `_t`（帧号量级）与相机 timestamp（0~10 量级）也错位两个数量级，模型学不到正确的时空对应。修复方法是把点云 time 归一化到与 timestamp 同一公式。

**练习 3**：`redundant_ratio` 为什么要让 `_t` 超出 `[t0, t1]`，而不是恰好铺满？

**答案**：边界帧（视频第一帧和最后一帧）的 timestamp 恰好在区间端点附近，若高斯时间中心只铺满 `[t0, t1]` 且初始 σ_t 有限，端点外侧没有高斯覆盖，边界帧的时间衰减 `get_marginal_t` 会整体偏小，出现「首尾帧发虚」；向两侧各扩 `r·L/2` 保证边界时刻也有足够多时间中心邻近的高斯。

---

### 4.3 `training_setup`：新增优化器参数组与学习率来源

#### 4.3.1 概念说明

`training_setup` 把所有可优化属性装配进优化器。第四维带来的变化是：**优化器参数组从 6 组（3D）增加到 8 组（4D）或 9 组（4D + rot_4d）**，并且新增了一台独立的 `coef_optimizer`（Neural Decaying Function 网络专用，u6-l4 详讲）。每个参数组的学习率来源不同，且 `t` 组的学习率有一条容易被忽略的**回退逻辑**：`position_t_lr_init` 默认为 `-1`，表示「未指定」，此时回退用空间位置的 `position_lr_init`。

#### 4.3.2 核心流程

```text
training_setup(training_args)
 ├─ 1. 准备致密化统计器：xyz_gradient_accum、denom（3D/4D 都有）
 ├─ 2. 基础参数组（6 组，3DGS 原生）：xyz / f_dc / f_rest / opacity / scaling / rotation
 ├─ 3. if gaussian_dim == 4:
 │    ├─ if position_t_lr_init < 0:  回退为 position_lr_init   ← 原位修改 training_args！
 │    ├─ 新增 t_gradient_accum
 │    ├─ 追加参数组 t        lr = position_t_lr_init × spatial_lr_scale
 │    ├─ 追加参数组 scaling_t lr = scaling_lr
 │    └─ if rot_4d: 追加参数组 rotation_r lr = rotation_lr
 ├─ 4. if coefficient is not None: 建独立的 coef_optimizer（Adam, lr=coefficient_lr, weight_decay）
 ├─ 5. self.optimizer = Adam(l, lr=0.0, eps=1e-15)   ← 顶层 lr=0 无效，真正生效的是每组自带 lr
 └─ 6. xyz_scheduler_args = 指数衰减调度器（只服务 xyz 组！）
```

#### 4.3.3 源码精读

[scene/gaussian_model.py:479-491](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L479-L491)——基础 6 组参数（u3-l1 视角的复习）：`xyz` 组的学习率乘了 `spatial_lr_scale`（即 `cameras_extent`，来自 Scene，u2-l4 讲过「必须先建 Scene 再 training_setup，否则学习率静默为 0」）；其余五组用固定学习率。

[scene/gaussian_model.py:492-499](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L492-L499)——本讲核心的 4D 分支：

```python
if self.gaussian_dim == 4: # TODO: tune time_lr_scale
    if training_args.position_t_lr_init < 0:
        training_args.position_t_lr_init = training_args.position_lr_init
    self.t_gradient_accum = torch.zeros((self.get_xyz.shape[0], 1), device="cuda")
    l.append({'params': [self._t], 'lr': training_args.position_t_lr_init * self.spatial_lr_scale, "name": "t"})
    l.append({'params': [self._scaling_t], 'lr': training_args.scaling_lr, "name": "scaling_t"})
    if self.rot_4d:
        l.append({'params': [self._rotation_r], 'lr': training_args.rotation_lr, "name": "rotation_r"})
```

四个关键点：

1. **回退逻辑**：`position_t_lr_init` 的注册默认值是 `-1.0`（[arguments/__init__.py:84](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/arguments/__init__.py#L84)，flame_steak.yaml 第 37 行同样写 `-1.0`），小于 0 视为「没配」，直接借用 `position_lr_init`（默认 `0.00016`）。所以**除非显式配置，时间中心与空间中心用同一初始学习率**，并同样乘 `spatial_lr_scale`。注意第 493 行的赋值是**对 `training_args` 的原位修改**——它改的是 `train.py` 里 `op.extract(args)` 抽出来的那个对象，不会回写到命令行 `args`，因此输出目录里 `training_params.txt`（[train.py:476-479](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L476-L479) 由 `str(args)` 写出）中记录的仍是 `-1.0`，排错时不要被它误导。注释 `# TODO: tune time_lr_scale` 说明作者也认为时间学习率值得单独调。
2. **`scaling_t` 与 `scaling` 共用** `scaling_lr`（默认 0.005），**`rotation_r` 与 `rotation` 共用** `rotation_lr`（默认 0.001）——时间维没有专属的尺度/旋转学习率配置。
3. `t_gradient_accum` 在这里按当前高斯数分配，配合 `denom` 用于时间梯度归一化（消费端见 4.4.3 与 u5-l3）。
4. `rot_4d` 决定第 9 组是否存在：`_rotation_r` 若为空张量（`rot_4d=False` 却强行加组）会让 Adam 收到空参数，所以这里必须条件追加。

[scene/gaussian_model.py:501-505](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L501-L505)——`coef_optimizer` 与主优化器：主 `Adam` 的顶层 `lr=0.0` 只是占位（每组自带 `lr` 覆盖它），`eps=1e-15` 是刻意调小的——高斯属性的梯度常在 1e-8 量级以下，默认 `eps=1e-8` 会让 Adam 的自适应步长分母被 eps 主导而失效。

[scene/gaussian_model.py:506-509](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L506-L509) 与 [scene/gaussian_model.py:511-521](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L511-L521)——调度器只服务 `xyz`：`xyz_scheduler_args` 是从 `position_lr_init` 指数衰减到 `position_lr_final` 的调度函数；`update_learning_rate` 里只匹配 `name == "xyz"` 的组。第 518-521 行有一段**被注释掉的 `t` 组调度代码**——也就是说当前实现里 `t` 的学习率从第 0 次迭代到结束**恒为初始值，不衰减**（对比：`xyz` 衰减 100 倍，`0.00016 → 0.0000016`）。这是一个有意保留的取舍还是未完成的工作，代码未说明，做时间维学习率实验时需要意识到这一点。

汇总成参数组对照表（默认值取自 [arguments/__init__.py:83-91](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/arguments/__init__.py#L83-L91) 与 flame_steak.yaml）：

| 参数组 name | 参数 | lr 公式 | flame_steak 数值（设 spatial_lr_scale = s） | 3D 有无 | 是否衰减 |
| --- | --- | --- | --- | --- | --- |
| `xyz` | `_xyz` | `position_lr_init × s` | `0.00016·s` | 有 | 是（指数衰减 100 倍） |
| `f_dc` | `_features_dc` | `feature_lr` | `0.0025` | 有 | 否 |
| `f_rest` | `_features_rest` | `feature_lr / 20` | `0.000125` | 有 | 否 |
| `opacity` | `_opacity` | `opacity_lr` | `0.05` | 有 | 否 |
| `scaling` | `_scaling` | `scaling_lr` | `0.005` | 有 | 否 |
| `rotation` | `_rotation` | `rotation_lr` | `0.001` | 有 | 否 |
| **`t`** | `_t` | `(position_t_lr_init<0 ? position_lr_init : position_t_lr_init) × s` | `0.00016·s`（回退后） | **4D 新增** | **否**（调度代码被注释） |
| **`scaling_t`** | `_scaling_t` | `scaling_lr` | `0.005` | **4D 新增** | 否 |
| **`rotation_r`** | `_rotation_r` | `rotation_lr` | `0.001` | **仅 rot_4d=True** | 否 |

#### 4.3.4 代码实践（本讲核心实践）

**实践目标**：实例化 `gaussian_dim=3` 与 `gaussian_dim=4`（含/不含 `rot_4d`）三种模型，打印 `training_setup` 生成的优化器参数组，亲手验证上表的每一行，特别是 `position_t_lr_init=-1` 的回退。

**操作步骤**（示例代码，非项目原有文件；**需要 GPU 与已编译的 simple-knn 扩展**，因为 `create_from_pcd` 调用 `distCUDA2`）：

```python
# probe_training_setup.py  ← 在仓库根目录运行：python probe_training_setup.py
import argparse
import numpy as np
import torch
from scene.gaussian_model import GaussianModel
from utils.graphics_utils import BasicPointCloud

def make_opt():
    # 与 arguments/__init__.py OptimizationParams 的默认值一致
    return argparse.Namespace(
        percent_dense=0.01, position_lr_init=0.00016, position_t_lr_init=-1.0,
        position_lr_final=0.0000016, position_lr_delay_mult=0.01, position_lr_max_steps=30000,
        feature_lr=0.0025, opacity_lr=0.05, scaling_lr=0.005, rotation_lr=0.001,
        coefficient_lr=1e-5, coefficient_weight_decay=1e-4)

def probe(gaussian_dim, rot_4d):
    N = 1000
    pcd = BasicPointCloud(
        points=np.random.rand(N, 3).astype(np.float32) * 2 - 1,
        colors=np.random.rand(N, 3).astype(np.float32),
        normals=np.zeros((N, 3), dtype=np.float32),
        time=None)
    g = GaussianModel(sh_degree=3, gaussian_dim=gaussian_dim,
                      time_duration=[0, 10.0], rot_4d=rot_4d)
    g.create_from_pcd(pcd, spatial_lr_scale=1.0)      # spatial_lr_scale=1.0 便于核对
    opt = make_opt()
    g.training_setup(opt)
    print(f"\n=== gaussian_dim={gaussian_dim}, rot_4d={rot_4d}, "
          f"共 {len(g.optimizer.param_groups)} 组 ===")
    for grp in g.optimizer.param_groups:
        print(f"name={grp['name']:<10s} lr={grp['lr']:.8f} shape={tuple(grp['params'][0].shape)}")
    print("回退后 opt.position_t_lr_init =", opt.position_t_lr_init)

probe(3, False)
probe(4, False)
probe(4, True)
```

**需要观察的现象**：

1. 三次调用分别输出 6、8、9 个参数组。
2. `gaussian_dim=4` 时 `t` 组的 `lr` 打印为 `0.00016`（回退生效），且打印后 `opt.position_t_lr_init` 已从 `-1.0` 变成 `0.00016`（原位修改的直接证据）。
3. `scaling_t` 与 `scaling` 的 lr 相同（0.005）；`rot_4d=True` 时 `rotation_r` 与 `rotation` 相同（0.001）。
4. `_t`、`_scaling_t` 形状为 `(N,1)`，`_rotation_r` 形状为 `(N,4)`。

**预期结果**：与 4.3.3 的对照表逐行一致。本实践依赖 CUDA 环境（`create_from_pcd` 内部 `.cuda()` 并调用 `distCUDA2`），**待本地验证**；无 GPU 时请完成下面的阅读型替代任务：对照 [scene/gaussian_model.py:484-499](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L484-L499) 手工推导三种配置下的参数组名称与 lr 表达式，并与 4.3.3 表格互查。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `position_t_lr_init` 的默认值设计成 `-1` 而不是直接给 `0.00016`？

**答案**：`-1` 充当「未配置」哨兵值。时间中心的合理学习率与场景时间参数化强相关（duration 长短、帧率），上游 4DGS 没有给出普适值，于是用负值表示「跟随空间位置学习率」，需要单独调参时再显式给正值。代价是：想真的把时间学习率设为 0 做消融时，传 0 是可以的（`0 < 0` 不成立，不触发回退），但负值一律被吞掉。

**练习 2**：若把 `spatial_lr_scale` 传成 0（例如忘了先构造 Scene），哪些参数组受影响？

**答案**：只有 `xyz` 和 `t` 两组受影响（lr 变 0），其余七组照常更新。于是会出现「高斯位置和时间中心冻结、颜色/尺度/不透明度仍在变」的诡异训练曲线——这正是 u2-l4 强调「Scene 必须先于 `training_setup` 构造」的原因。

**练习 3**：`update_learning_rate` 每次迭代都会被调用，`t` 组的学习率在 30000 次迭代中如何变化？

**答案**：恒为初始值（回退后的 `position_lr_init × s`）。因为调度器只匹配 `name == "xyz"` 的组（[scene/gaussian_model.py:513-517](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L513-L517)），`t` 组的调度代码在第 518-521 行被注释掉了。相比之下 `xyz` 组从 `0.00016·s` 指数衰减到 `0.0000016·s`。

---

### 4.4 `capture`/`restore` 与 `gaussian_dim==4` 分支全景

#### 4.4.1 概念说明

`capture` 把模型全部状态打包成一个**顺序敏感的元组**写入 `.pth`，`restore` 按同一顺序解包还原。u3-l1 讲过 3D 分支是 14 个元素；4D 分支扩展到 **21 个元素**，插入了 7 个时间维相关项。此外，`gaussian_dim == 4` 的 if 分支像血管一样贯穿整个文件——属性每多一件，`capture`、`restore`、`prune_points`、`densification_postfix` 就各多一处要同步的地方。本模块把这些落点一次性盘清，建立「改 4D 属性时必须同步哪些位置」的检查清单。

#### 4.4.2 核心流程

```text
capture()（4D，21 个元素，按顺序）
 1 active_sh_degree        8  max_radii2D           15 _t
 2 _xyz                    9  xyz_gradient_accum    16 _scaling_t
 3 _features_dc           10  t_gradient_accum  ★   17 _rotation_r      ★
 4 _features_rest         11  denom                 18 rot_4d
 5 _scaling               12  optimizer.state_dict  19 env_map
 6 _rotation              13  coef_opt_dict         20 active_sh_degree_t
 7 _opacity               14  spatial_lr_scale      21 coefficient_dict
                                                     （★ 为 4D 新增的属性项）

restore(model_args, training_args)
 ├─ 按 gaussian_dim 选择 14 元素或 21 元素解包
 ├─ training_args 非 None → training_setup + 恢复梯度累计器 + load optimizer 状态（续训）
 └─ training_args 为 None   → 只解包属性（纯推理，render.py 走这条路）
```

#### 4.4.3 源码精读

[scene/gaussian_model.py:116-139](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L116-L139)——4D 分支的 `capture`。对照 3D 分支（第 99-115 行），新增的不是简单追加到尾部，而是**在中间插入**：`t_gradient_accum` 插在 `xyz_gradient_accum` 之后（第 127 行），`_t`/`_scaling_t`/`_rotation_r`/`rot_4d`/`env_map`/`active_sh_degree_t` 插在 `spatial_lr_scale` 之后（第 132-137 行）。注意 `rot_4d` 这个**布尔开关本身也被存进 checkpoint**——因为 `restore` 时需要知道这份 checkpoint 是不是带时空耦合的，才能正确重建协方差激活函数（`setup_functions` 的分支选择依赖 `self.rot_4d`）。

[scene/gaussian_model.py:157-178](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L157-L178)——4D 分支的 `restore` 解包。两点值得注意：其一，顺序必须与 `capture` 逐一对齐，错位不会报错而是**静默张冠李戴**（比如把 `_t` 的值装进 `_rotation_r`）；其二，第 182-183 行无条件恢复 `t_gradient_accum`，但 3D 分支的元组里根本没有这一项——所以用 3D 模型走 `restore` 且 `training_args` 非 None 时会 `NameError`。这是 `capture`/`restore` 协议「手工维护、无 schema 校验」的固有风险（u3-l1 已指出，4D 下风险点更多）。

[scene/gaussian_model.py:577-582](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L577-L582)——`prune_points` 的 4D 增量：

```python
if self.gaussian_dim == 4:
    self._t = optimizable_tensors['t']
    self._scaling_t = optimizable_tensors['scaling_t']
    if self.rot_4d:
        self._rotation_r = optimizable_tensors['rotation_r']
    self.t_gradient_accum = self.t_gradient_accum[valid_points_mask]
```

剪枝的真实入口是 `_prune_optimizer`（第 543 行起）：它按掩码同时裁剪**参数张量本身和 Adam 的一阶/二阶动量**，返回新张量。也就是说删掉 N 个高斯时，`_t` 与 `scaling_t`（及 `rotation_r`）的 Adam 动量同步裁剪，不会出现「张量缩短了、动量还是旧长度」的错位。`t_gradient_accum` 是普通缓冲区（不在优化器里），所以单独按掩码索引。

[scene/gaussian_model.py:644-670](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L644-L670)——`densification_postfix` 的 4D 增量：新增高斯时把 `new_t`/`new_scaling_t`/`new_rotation_r` 以键名 `t`/`scaling_t`/`rotation_r` 放进字典，交给 `cat_tensors_to_optimizer` 沿第 0 维拼接并给新行补零动量。**键名必须与 `training_setup` 里参数组的 `name` 完全一致**——`cat_tensors_to_optimizer` 就是按 `group["name"]` 到字典里取张量的（第 626 行）。这正是 4.3 表格中 name 列的第二个用途：它不只是日志标签，而是致密化时张量路由的键。

[scene/gaussian_model.py:770-774](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L770-L774)——`add_densification_stats` 的 4D 增量：每次迭代把 `_t` 的梯度（`avg_t_grad`，多视角 batch 时已按可见次数归一化，见 u5-l3）累进 `t_gradient_accum`，与屏幕空间梯度并列作为「哪个高斯需要致密化」的证据。消费端在 `densify_and_prune`（[scene/gaussian_model.py:752-756](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L752-L756)）：`grads_t = t_gradient_accum / denom`，配独立阈值 `densify_grad_t_threshold`（默认 `0.0002/40`，[arguments/__init__.py:102](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/arguments/__init__.py#L102)，比空间阈值宽松 40 倍）。致密化的完整判定逻辑留待 u5-l4。

把 `gaussian_dim == 4`（及 `rot_4d`）分支在文件中的落点汇总成清单：

| 位置 | 行号 | 分支内容 |
| --- | --- | --- |
| `__init__` | [80-90](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L80-L90) | 登记 4D 属性与开关（本讲 4.1） |
| `capture` | [116-139](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L116-L139) | 21 元素元组（本模块） |
| `restore` | [157-178](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L157-L178) | 21 元素解包（本模块） |
| property 组 | [197-223](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L197-L223) | `get_scaling_t`/`get_scaling_xyzt`/`get_rotation_r`/`get_t`/`get_xyzt` |
| `get_max_sh_channels` | [235-242](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L235-L242) | 4D 球谐通道数 |
| `load_ply` | [332-338](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L332-L338) | 从 PLY 恢复 `_t`/`_scaling_t`/`_rotation_r`（u3-l5 详讲） |
| `create_from_pcd` | [413-418](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L413-L418), [426-432](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L426-L432), [444-448](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L444-L448) | 初始化 `_t`/`_scaling_t`/`_rotation_r`（u3-l5 详讲） |
| `training_setup` | [492-499](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L492-L499) | 新增参数组（本讲 4.3） |
| `prune_points` | [577-582](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L577-L582) | 剪枝同步（本模块） |
| `densification_postfix` | [652-656](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L652-L656), [665-670](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L665-L670) | 致密化拼接（本模块） |
| `densify_and_split` | [692-716](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L692-L716) | `rot_4d` 时在 xyzt 四维采样新位置（u5-l4 详讲） |
| `densify_and_clone` | [740-744](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L740-L744) | 克隆时带上时间维属性 |
| `add_densification_stats`（及 `_grad` 版） | [770-780](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L770-L780) | 时间梯度累计（本模块） |

这个清单同时是一份「二次开发检查表」：如果你给 `GaussianModel` 增加一个新属性，上述每一行都需要同步维护，漏掉任何一处轻则状态丢失、重则张量长度错位崩溃。

最后指出一个隐藏陷阱：[scene/gaussian_model.py:358-360](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L358-L360) 的 `save_ply` **无条件**访问 `self._t`、`self._scaling_t`、`self._rotation_r`。`gaussian_dim=3` 时三者永远是空张量，`_rotation_r`（形状 (0,)）转 numpy 后维度不符，写 PLY 会直接失败——也就是说 `save_ply` 实际上假定模型是 4D 的。4C4D 主线就是 4D 训练，所以此问题不常触发，但如果你想做纯 3D 对照实验（`--gaussian_dim 3`），保存阶段需要自己规避。

#### 4.4.4 代码实践

**实践目标**：不运行训练，通过「填表 + 核对」掌握 4D `capture` 元组的 21 个元素及其消费方。

**操作步骤**：

1. 打开 [scene/gaussian_model.py:116-139](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L116-L139)，把 21 个元素按顺序抄成两列：左列「元组位置 + 表达式」，右列「它是什么、restore 后被谁用」。
2. 对照 [scene/gaussian_model.py:157-178](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L157-L178) 的解包变量名，检查你抄的顺序是否与解包一一对应。
3. 用 3D 分支（第 99-115 行）做同样的事，标出哪些位置是 4D 插入的。
4. 思考题自检：如果要让 checkpoint 兼容「3D 训练 → 4D 续训」的迁移，`restore` 会先在哪一行崩溃？

**需要观察的现象**：你能在不看本讲 4.4.2 示意的情况下独立复现 21 项的顺序，并说出第 10 项（`t_gradient_accum`）和第 14 项（`coef_opt_dict`）分别是给谁用的。

**预期结果**：与 4.4.2 的示意一致；思考题答案见下方练习 3。此实践为纯源码阅读，无需运行环境，可立即完成。

#### 4.4.5 小练习与答案

**练习 1**：`capture` 的 4D 元组里为什么要存 `rot_4d` 这个布尔值？`time_duration` 为什么不用存？

**答案**：`rot_4d` 决定 `setup_functions` 选择哪个协方差激活函数、`densify_and_split` 走哪条采样分支、参数组里有没有 `rotation_r`，这些必须在 `restore` 时恢复，所以存进元组（第 135 行）。`time_duration` 不需要存：它的作用只在初始化阶段（随机采样 `_t` 的范围、初始 σ_t），一旦 `_t`/`_scaling_t` 的数值存下来了，duration 的历史使命已完成；渲染时需要 duration 的话由配置/入口另行传入。

**练习 2**：`densification_postfix` 中字典键名写成 `"rotation_r"` 以外的字符串（如 `"rot_r"`）会发生什么？

**答案**：`cat_tensors_to_optimizer` 按参数组 `name` 到字典取张量，第 626 行 `tensors_dict[group["name"]]` 会抛 `KeyError`（若该键不存在）——这是相对「幸运」的立刻崩溃；更险的是若键名撞上别的组名，会拼接形状不符的张量。这就是为什么参数组 `name` 是一套必须三处一致的隐式协议：`training_setup` 定义、`densification_postfix` 使用、`replace_tensor_to_optimizer`/`_prune_optimizer` 匹配。

**练习 3**：用 3D 配置训练出的 `chkpnt30000.pth`，误用 4D 配置去 `restore` 续训，会发生什么？

**答案**：`restore` 的 4D 分支尝试把 14 个元素解包成 21 个变量，直接 `ValueError: not enough values to unpack`。反方向（21 → 14）同理报 `too many values`。这是元组协议唯一「自动报警」的错误类型；顺序错乱但数量一致时则静默出错，所以改 `capture`/`restore` 时务必两处同步改（另见第 182-183 行：3D 分支 `training_args` 非 None 时会因 `t_gradient_accum` 未定义而 `NameError`）。

---

## 5. 综合实践

**任务：给「第四维」写一个体检脚本。** 把本讲四个模块串起来，写一个 `probe_4d.py`（示例代码，非项目原有文件），对一个合成的 4D 高斯模型完成四项检查并输出报告：

1. **构造检查**（对应 4.1）：`GaussianModel(sh_degree=3, gaussian_dim=4, time_duration=[0,10.0], rot_4d=True)`，打印 `get_max_sh_channels`（期望 33）与三个 4D 属性的形状。
2. **初始化检查**（对应 4.2）：用 1000 个随机点、`time=None` 的 `BasicPointCloud` 调 `create_from_pcd(pcd, spatial_lr_scale=1.0)`，打印 `_t` 的 min/max（期望约 -1.0/11.0，含 redundant_ratio=0.2 的外扩）与 `get_scaling_t` 的唯一值（期望 2.0，即 duration/5）。
3. **优化器检查**（对应 4.3）：按 4.3.4 的方式 `training_setup`，打印全部 9 个参数组的 name/lr，并验证 `update_learning_rate(0)` 与 `update_learning_rate(30000)` 之后 `t` 组 lr 不变、`xyz` 组 lr 从 `0.00016` 衰减到 `0.0000016`。
4. **协议检查**（对应 4.4）：调用 `capture()`，断言元组长度为 21；打印第 10 项（`t_gradient_accum`）与第 18 项（`rot_4d`）确认类型。

运行方式与依赖：在仓库根目录 `python probe_4d.py`；第 2 步起需要 GPU 与 simple-knn（`distCUDA2`），**待本地验证**。无 GPU 时的替代方案：只执行第 1 步（CPU 可跑），第 2-4 步改为「在纸上对每个期望值标注它来自哪一行源码」，然后在代码评审的意义上互相核对。完成后你应该能回答：这个模型有多少个可优化参数张量？（答案：`gaussian_dim=4, rot_4d=True` 时 9 个——6 个 3D 属性加 `_t`、`_scaling_t`、`_rotation_r`。）

## 6. 本讲小结

- 4D 高斯在 3DGS 六属性之外新增三件：时间中心 `_t`（(N,1)，无激活）、时间尺度 `_scaling_t`（(N,1)，log 空间读时 `exp`）、时间右四元数 `_rotation_r`（(N,4)，读时归一化，仅 `rot_4d=True` 存在）；`__init__` 只占位，数值由 `create_from_pcd`/`load_ply` 填充。
- `time_duration`（入口默认 `[0,10]`，帧号按 `10f/(F_max+1)` 归一化）是相机 timestamp、点云 time 字段、模型初始化三方共享的时间域；初始化时 `_t` 在略外扩的域内均匀采样（`redundant_ratio`），初始 σ_t 取 duration/5。签名默认 `[-0.5,0.5]` 是上游遗留，脱离 train.py 使用时必须显式传。
- `training_setup` 的参数组从 3D 的 6 组增至 4D 的 8 组（`t`、`scaling_t`）、`rot_4d` 时 9 组（再加 `rotation_r`）；`t` 的学习率在 `position_t_lr_init<0` 时回退为 `position_lr_init` 且同样乘 `spatial_lr_scale`，且全程不衰减（调度代码被注释）——注意回退是原位修改，`training_params.txt` 里仍是 `-1.0`。
- `capture`/`restore` 的 4D 元组有 21 个元素（3D 为 14），顺序敏感、无 schema 校验；`rot_4d` 开关本身也入 checkpoint，因为下游协方差构造与致密化分支依赖它。
- `gaussian_dim==4` 分支贯穿 `capture`/`restore`/`load_ply`/`create_from_pcd`/`training_setup`/`prune_points`/`densification_postfix`/`densify_and_clone`/`densify_and_split`/`add_densification_stats`；`prune`/`densify` 靠参数组 `name` 路由张量并同步 Adam 动量，这套 name 是必须三处一致的隐式协议——也是二次开发新增属性时的检查表。
- 陷阱备忘：`save_ply` 无条件访问 `_t`/`_scaling_t`/`_rotation_r`，纯 3D（`gaussian_dim=3`）训练无法直接保存；3D/4D checkpoint 互不兼容（解包数量不匹配会立刻报错，数量一致但顺序错时会静默张冠李戴）。

## 7. 下一步学习建议

本讲补齐了第四维的「属性、优化、持久化」，下一讲 **u3-l3《4D 协方差与时间边缘化》**将进入这些属性如何被消费：精读 `setup_functions` 中的 `build_covariance_from_scaling_rotation_4d`，推导 4D 协方差在固定时刻的 3D 条件协方差 \(\Sigma_{3D|t} = \Sigma_{11} - \Sigma_{12}\Sigma_{12}^\top/\Sigma_{tt}\) 与均值偏移——那正是 `rot_4d` 引入时空耦合的代价与收益所在。之后再按顺序读 **u3-l4（4D 球谐，`sh_degree_t` 与 `sh_channels_4d` 的展开）**与 **u3-l5（`create_from_pcd`/`save_ply`/`load_ply` 的初始化与持久化细节，包括 PLY 字段前缀匹配的坑）**。预习建议：对照 [utils/general_utils.py:113-145](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/general_utils.py#L113-L145) 的 `build_rotation_4d`/`build_scaling_rotation_4d`，先自己写一个 4×4 的 \(L L^\top\) 例子里辨认 \(\Sigma_{11}\)、\(\Sigma_{12}\)、\(\Sigma_{tt}\) 三个块的位置。
