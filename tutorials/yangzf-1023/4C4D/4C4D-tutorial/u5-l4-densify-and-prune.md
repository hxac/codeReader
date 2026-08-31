# u5-l4 自适应致密化与剪枝（4D 版）

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚 `densify_and_clone` 与 `densify_and_split` 的判定条件差异：同一条梯度阈值，如何被「空间尺度是否超过 `percent_dense × scene_extent`」分成两条互补的路径。
2. 理解 `rot_4d=True` 时 split 如何在 xyzt 四维空间中联合采样子高斯位置，以及它与「xyz、t 独立采样」的本质区别。
3. 掌握 `densification_postfix` 如何把新点「落库」，并理解它为什么必须清零全部梯度统计。
4. 掌握 `cat_tensors_to_optimizer` / `_prune_optimizer` / `replace_tensor_to_optimizer` 三个工具如何在张量增删时同步 Adam 的动量状态。
5. 逐条解释 `prune_mask` 中 opacity / 屏幕尺寸 / 世界尺寸三个剪枝条件的含义，以及 4C4D 默认配置下哪些条件实际生效。

本讲承接 u5-l3：上一讲我们搞清楚了「梯度如何被跨视角合并、如何累计进 `xyz_gradient_accum` / `t_gradient_accum`」，本讲就回答「这些统计最终如何变成加点和删点」。

## 2. 前置知识

### 2.1 为什么需要致密化与剪枝

3DGS/4DGS 的点数不是一开始就定死的。初始点云来自 COLMAP/MASt3R，覆盖往往不足。训练中会出现两类典型失败：

- **欠重建（under-reconstruction）**：某个区域该有物体但没有足够的高斯覆盖，渲染结果「缺一块」。信号是：该区域高斯的屏幕空间梯度持续偏大，且高斯本身很小。
- **过重建（over-reconstruction）**：一个高斯把大片不该覆盖的区域糊住了。信号是：屏幕空间梯度大，且高斯本身很大。

对策分别是：

- 欠重建 → **clone（克隆）**：原样复制一份小高斯，让两份一起慢慢挪到位。
- 过重建 → **split（分裂）**：把一个大高斯删掉，换成 N 个更小的高斯，采样位置在原高斯内部。

「梯度大」的度量就是 u5-l3 讲过的 `xyz_gradient_accum / denom`：被统计的那些 iteration 上、可见视角上的平均屏幕空间梯度范数。

### 2.2 Adam 优化器的动量状态

PyTorch 的 Adam 为每个参数维护两份状态（[torch 文档](https://pytorch.org/docs/stable/generated/torch.optim.Adam.html)）：

- `exp_avg`：一阶动量（梯度的指数滑动平均）；
- `exp_avg_sq`：二阶动量（梯度平方的指数滑动平均）。

关键机制：**`optimizer.state` 以 Parameter 对象本身为键**。一旦你用一个新的 `nn.Parameter` 替换旧对象，旧状态就成了孤儿。所以每次加/删高斯后，必须手动把动量状态与新参数对齐——这正是本讲 4.3 节三个函数存在的理由。

### 2.3 布尔掩码索引

PyTorch 中 `tensor[mask]`（`mask` 为同长度 `bool` 张量）会取出 `True` 位置的行。本讲中所有「选中哪些高斯」的逻辑都用这个写法，例如 `self._xyz[selected_pts_mask]`。

### 2.4 4D 高斯的属性回顾（u3-l2）

`gaussian_dim=4` 时，模型在 3D 属性之外还有时间中心 `_t`、时间尺度 `_scaling_t`（log 存储），`rot_4d=True` 时还有右四元数 `_rotation_r`。属性 `get_scaling_xyzt` 把空间与时间尺度拼成一个 4 维向量（[scene/gaussian_model.py:201-203](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L201-L203)），这是本讲 xyzt 采样的基础。

## 3. 本讲源码地图

| 文件 | 行号 | 作用 |
|---|---|---|
| `scene/gaussian_model.py` | 676-721 | `densify_and_split`：大高斯分裂为 N 个小高斯 |
| `scene/gaussian_model.py` | 723-746 | `densify_and_clone`：小高斯克隆 |
| `scene/gaussian_model.py` | 644-674 | `densification_postfix`：新点写入模型并清零统计 |
| `scene/gaussian_model.py` | 622-642 | `cat_tensors_to_optimizer`：追加参数并同步 Adam 状态 |
| `scene/gaussian_model.py` | 543-559 | `_prune_optimizer`：按掩码删参数并同步 Adam 状态 |
| `scene/gaussian_model.py` | 561-582 | `prune_points`：删除高斯（含 4D 属性与统计） |
| `scene/gaussian_model.py` | 748-768 | `densify_and_prune`：入口，串起 clone/split/prune |
| `scene/gaussian_model.py` | 528-541 | `replace_tensor_to_optimizer`：整体替换参数（reset_opacity 用） |
| `train.py` | 234-256 | 训练循环中的致密化调度：时间窗、阈值、prune_only |
| `train.py` | 448-452, 473-474 | 参数派生：`--max_num_pts`、opacity_decay 联动 |
| `arguments/__init__.py` | 94-103 | 致密化相关超参默认值 |

## 4. 核心概念与源码讲解

### 4.1 致密化判定：densify_and_clone 与 densify_and_split

#### 4.1.1 概念说明

clone 和 split 用**同一个梯度阈值** \(\tau_{grad}\)（`densify_grad_threshold`，默认 0.0002）筛出「重建不佳」的高斯，再用**空间尺度分界线**决定处置方式：

\[ \text{尺度分界} = \text{percent\_dense} \times \text{scene\_extent} = 0.01 \times \text{cameras\_extent} \]

- 梯度大 **且** 尺度小（三轴最大尺度 ≤ 分界线）→ **clone**：说明点不够，复制一份。
- 梯度大 **且** 尺度大（三轴最大尺度 > 分界线）→ **split**：说明点太糊，拆成 N=2 个小高斯。

两个条件在「尺度」维度上互补，所以一个高斯不会同时被 clone 和 split。

`scene_extent` 就是 u2-l4 讲过的 `cameras_extent`（训练相机光心到场景中心的最大距离 ×1.1），它让阈值自适应场景尺度：大场景里「大高斯」的绝对尺寸标准也更宽。

#### 4.1.2 核心流程

```
grads = xyz_gradient_accum / denom          # (N,1) 平均屏幕空间梯度（u5-l3）
grads[NaN] = 0                              # denom=0 的点置零

clone_mask[i]  = |grads[i]| ≥ τ_grad  且  max(s_xyz[i]) ≤ 0.01·extent
split_mask[i]  = |grads[i]| ≥ τ_grad  且  max(s_xyz[i]) > 0.01·extent

clone：全部属性（含 _t/_scaling_t/_rotation_r）按下标复制一份
split：删除父高斯，生成 N=2 个子高斯
       子尺度   s' = s / (0.8·N)
       子位置   非 rot_4d：xyz 与 t 各自独立高斯采样
                rot_4d  ：在 4 维旋转后的椭球内联合采样
```

split 的子高斯位置采样，两条路径的数学形式不同：

- 非 `rot_4d`（xyz 与 t 独立）：

\[ p'_{xyz} = \mu_{xyz} + R_3\,(\mathbf{s}_{xyz} \odot \epsilon_3), \quad \epsilon_3 \sim \mathcal{N}(0, I_3) \]
\[ t' = t + s_t \cdot \epsilon_t, \quad \epsilon_t \sim \mathcal{N}(0, 1) \]

- `rot_4d`（xyzt 联合）：

\[ p'_{xyzt} = \mu_{xyzt} + R_4\,(\mathbf{s}_{xyzt} \odot \epsilon_4), \quad \epsilon_4 \sim \mathcal{N}(0, I_4) \]

其中 \(R_4\) 是由左右双四元数（`_rotation`、`_rotation_r`）组装的 4×4 旋转矩阵，\(\mathbf{s}_{xyzt} = (\exp(\_scaling), \exp(\_scaling\_t))\)。

**为什么 rot_4d 要联合采样？** u3-l3 讲过，4D 协方差的空间维与时间维之间存在耦合项 \(\Sigma_{xt}\)。独立采样等于假设时空不相关，会把一个「斜躺」在时空中的高斯拆成两个方向错误的块；联合采样在旋转后的 4D 椭球内取点，保留了时空耦合的方向性。

子尺度取 \(s / (0.8N)\)：N=2 时即 \(s/1.6\)。若要求两个子高斯体积之和等于父高斯，三维尺度应取 \(s/\sqrt[3]{2} \approx s/1.26\)，时间维再参与时更复杂；\(0.8N\) 是一个略偏小的经验系数，让子高斯「略大一点点」，给后续优化留收缩余地。

#### 4.1.3 源码精读

**clone 的判定与复制**（[scene/gaussian_model.py:723-746](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L723-L746)）：

```python
selected_pts_mask = torch.where(torch.norm(grads, dim=-1) >= grad_threshold, True, False)
selected_pts_mask = torch.logical_and(selected_pts_mask,
                                      torch.max(self.get_scaling, dim=1).values <= self.percent_dense*scene_extent)
...
new_xyz = self._xyz[selected_pts_mask]
```

第一行按梯度筛（`grads` 形状 (N,1)，对最后一维取范数即取绝对值）；第二行加上「尺度足够小」条件，注意用的是**激活后**尺度 `get_scaling`（`exp` 后的物理尺度）与 `<=`。随后所有属性——包括 4D 的 `_t`、`_scaling_t`，以及 `rot_4d` 时的 `_rotation_r`——都按下标原样复制（[L740-744](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L740-L744)）。注意 `new_t` 等先初始化为 `None`，仅 `gaussian_dim == 4` 时填充，所以 3D 模式也能走同一入口。

值得指出：**clone 的形参 `grads_t` 与 `grad_t_threshold` 在函数体内从未被使用**——时间梯度阈值目前是「传进来但没接线」的参数（u5-l3 也确认了 `grads_t` 未被 clone/split 消费），判定完全依赖屏幕空间梯度。

**split 的判定**（[scene/gaussian_model.py:676-684](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L676-L684)）：

```python
n_init_points = self.get_xyz.shape[0]
padded_grad = torch.zeros((n_init_points), device="cuda")
padded_grad[:grads.shape[0]] = grads.squeeze()
selected_pts_mask = torch.where(padded_grad >= grad_threshold, True, False)
selected_pts_mask = torch.logical_and(selected_pts_mask,
                                      torch.max(self.get_scaling, dim=1).values > self.percent_dense*scene_extent)
```

这里有个精妙的细节——**为什么要 `padded_grad` 补零？** 调用顺序是先 clone 后 split（见 4.4 节 `densify_and_prune`）。clone 的 `densification_postfix` 会把点数从 \(N\) 变成 \(N + n_{clone}\)，但 `grads` 是 clone **之前**算好的、长度为 \(N\) 的张量。补零对齐后，新 clone 出来的点梯度为 0，本轮不会被再次 split。这是一种「一轮之内防止刚出生的点被立刻拆掉」的保护。

**split 的两条采样路径**（[scene/gaussian_model.py:692-716](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L692-L716)）：

```python
if not self.rot_4d:
    stds = self.get_scaling[selected_pts_mask].repeat(N,1)
    ...
    new_xyz = torch.bmm(rots, samples.unsqueeze(-1)).squeeze(-1) + self.get_xyz[selected_pts_mask].repeat(N, 1)
    ...
    samples_t = torch.normal(mean=means_t, std=stds_t)
    new_t = samples_t + self.get_t[selected_pts_mask].repeat(N, 1)
else:
    stds = self.get_scaling_xyzt[selected_pts_mask].repeat(N,1)
    means = torch.zeros((stds.size(0), 4),device="cuda")
    samples = torch.normal(mean=means, std=stds)
    rots = build_rotation_4d(self._rotation[selected_pts_mask], self._rotation_r[selected_pts_mask]).repeat(N,1,1)
    new_xyzt = torch.bmm(rots, samples.unsqueeze(-1)).squeeze(-1) + self.get_xyzt[selected_pts_mask].repeat(N, 1)
    new_xyz = new_xyzt[...,0:3]
    new_t = new_xyzt[...,3:4]
```

读法：`repeat(N,1)` 把选中的行复制 N 份（每份是一个子高斯）；`torch.normal(mean=0, std=stds)` 在各维独立采高斯噪声；`torch.bmm` 用旋转矩阵把「轴对齐椭球内的采样」旋转到高斯的实际朝向。非 rot_4d 分支里 xyz 走 3×3 旋转、t 独立加噪；rot_4d 分支拼成 4 维一起变换，最后再切开成 `new_xyz` 与 `new_t`。

**子高斯尺度与属性**（[scene/gaussian_model.py:686-690](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L686-L690) 与 [L715-716](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L715-L716)）：

```python
new_scaling = self.scaling_inverse_activation(self.get_scaling[selected_pts_mask].repeat(N,1) / (0.8*N))
new_scaling_t = self.scaling_inverse_activation(self.get_scaling_t[selected_pts_mask].repeat(N,1) / (0.8*N))
```

激活后尺度除以 \(0.8N\)，再用 `scaling_inverse_activation`（即 `log`）写回裸值存储。颜色、不透明度、旋转（含 rot_4d 的 `_rotation_r`）则原样复制给两个子高斯。

**最后删掉父高斯**（[scene/gaussian_model.py:720-721](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L720-L721)）：

```python
prune_filter = torch.cat((selected_pts_mask, torch.zeros(N * selected_pts_mask.sum(), device="cuda", dtype=bool)))
self.prune_points(prune_filter)
```

`densification_postfix` 刚刚追加了 \(N \cdot n_{split}\) 个子高斯，所以现在的点数是 \(n_{init} + N \cdot n_{split}\)。掩码前 \(n_{init}\) 位标记被 split 的父高斯、后 \(N \cdot n_{split}\) 位全 False，一次 `prune_points` 把父删掉，实现「一换二」。

顺带一提：两处 `print(f"num_to_densify_pos: ...")`（[L684](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L684)、[L728](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L728)）会在每次致密化时打印「过阈值点数 / 实际 clone 或 split 点数」，是零成本的观测窗口，综合实践会用到它。

#### 4.1.4 代码实践

**实践目标**：不动源码，用一段独立脚本在 CPU 上复现 clone/split 的判定逻辑，直观感受两个掩码如何互补。

**操作步骤**（示例代码，可在任意有 PyTorch 的环境运行）：

```python
# practice_clone_split_mask.py（示例代码，独立于仓库）
import torch

N, extent = 1000, 4.0
tau_grad, percent_dense = 0.0002, 0.01

grads = torch.rand(N, 1) * 0.0006            # 假装是 xyz_gradient_accum / denom
scaling = torch.exp(torch.randn(N, 3))        # 假装是 get_scaling（激活后）

clone_mask = (torch.norm(grads, dim=-1) >= tau_grad) & \
             (scaling.max(dim=1).values <= percent_dense * extent)
split_mask = (grads.squeeze() >= tau_grad) & \
             (scaling.max(dim=1).values > percent_dense * extent)

print("过梯度阈值:", (torch.norm(grads, dim=-1) >= tau_grad).sum().item())
print("clone:", clone_mask.sum().item(), " split:", split_mask.sum().item())
print("clone ∩ split:", (clone_mask & split_mask).sum().item())  # 恒为 0
```

**需要观察的现象**：`clone ∩ split` 恒为 0；把 `extent` 调小（如 1.0），clone 数量下降、split 数量上升——分界线左移，原来算「小」的高斯被划进「大」的阵营。

**预期结果**：两个掩码在尺度维度上互补，验证 4.1.1 的判定公式。若在仓库内对照真实训练，观察控制台 `num_to_densify_pos` 与 `num_to_clone_pos` / `num_to_split_pos` 的差值随训练推进的变化（早期 clone 占多、后期 split 上升），待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 clone 复制的是 `_scaling`（裸值）而 split 要先 `get_scaling` 再 `scaling_inverse_activation`？

**答案**：clone 希望子高斯与父高斯完全一样，直接复制存储的裸值（log 空间）即可；split 希望子高斯尺度是父的 \(1/(0.8N)\)，这个除法必须作用在物理尺度（exp 后）上，除完再取 log 存回，即 `log(exp(_scaling)/1.6)`。若直接对裸值除以 1.6，等价于物理尺度开 1.6 次方，语义完全错误。

**练习 2**：`padded_grad` 若改成直接 `grads.squeeze()`（不补零），会发生什么？

**答案**：`grads` 长度是 clone 前的点数，而 `selected_pts_mask` 相关的 `self.get_scaling` 长度是 clone 后的点数，两者做 `logical_and` 会因形状不符直接报错；即便形状碰巧一致，语义上也会让「刚 clone 出的点」带着错误索引的梯度参与 split 判定。补零既对齐了长度，又让新点本轮梯度为 0、天然不被 split。

**练习 3**：split 后子高斯的不透明度是多少？会不会导致渲染整体变亮？

**答案**：子高斯原样继承父的 `_opacity`，两个子高斯各拿一份同样的不透明度（[L690](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L690)）。短时间局部覆盖确实可能偏亮，但由于子高斯尺度缩小、且不透明度会继续被优化（以及被 prune 的 opacity 条件约束），通常在若干迭代内自行修正；这也是 3DGS 原版设计中 `percent_dense`、`thresh_opa_prune` 需要配合调参的原因之一。

### 4.2 densification_postfix：新点落库与统计清零

#### 4.2.1 概念说明

clone 和 split 算出「新点的属性张量」之后，需要把它们**并入模型**：模型的 `_xyz` 等属性要从 `nn.Parameter` 换成「旧值拼接新值」的新 `nn.Parameter`，优化器参数组里的参数对象也要同步替换。`densification_postfix` 就是这层「落库」手续，它同时负责把上一阶段累积的梯度统计全部清零重建。

#### 4.2.2 核心流程

```
1. 组装字典 d：键 = 优化器参数组名，值 = 新点张量
   3D: xyz / f_dc / f_rest / opacity / scaling / rotation
   4D 额外: t / scaling_t；rot_4d 再加: rotation_r
2. cat_tensors_to_optimizer(d) → 返回各参数组的新 nn.Parameter
3. 用返回值替换 self._xyz 等模型属性
4. 统计张量全部按新点数重建为零：
   xyz_gradient_accum、t_gradient_accum、denom、max_radii2D
```

第 4 步是理解致密化节奏的钥匙：既然 accum 与 denom 都归零，那么 `accum/denom` 度量的永远是「**距上一次致密化以来**」的平均梯度，而不是整个训练史的平均。每轮致密化都是在「最近 100 个 iteration 的证据」上做决策。

#### 4.2.3 源码精读

[scene/gaussian_model.py:644-674](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L644-L674)：

```python
d = {"xyz": new_xyz, "f_dc": new_features_dc, "f_rest": new_features_rest,
     "opacity": new_opacities, "scaling": new_scaling, "rotation": new_rotation}
if self.gaussian_dim == 4:
    d["t"] = new_t
    d["scaling_t"] = new_scaling_t
    if self.rot_4d:
        d["rotation_r"] = new_rotation_r

optimizable_tensors = self.cat_tensors_to_optimizer(d)
self._xyz = optimizable_tensors["xyz"]
...
self.xyz_gradient_accum = torch.zeros((self.get_xyz.shape[0], 1), device="cuda")
self.denom = torch.zeros((self.get_xyz.shape[0], 1), device="cuda")
self.max_radii2D = torch.zeros((self.get_xyz.shape[0]), device="cuda")
```

三个要点：

1. **字典键即参数组名**。`training_setup`（[L484-499](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L484-L499)）创建参数组时每组都带了 `"name"` 字段，`cat_tensors_to_optimizer` 靠这个名字找到「该拼接哪个张量」。若 clone 路径忘了给 `d["rotation_r"]` 赋值（非 rot_4d 时它是 `None`），拼接会失败——所以代码用 `gaussian_dim` / `rot_4d` 分支严格对齐参数组的存在性。
2. **统计清零是对所有点，不只是新点**。`max_radii2D` 也一并归零，意味着 4.4 节的「屏幕尺寸剪枝」比较的是**自上一次致密化以来**积累的最大屏幕半径。
3. **落库后旧 Parameter 对象被丢弃**，所有引用（属性、优化器参数组）统一指向新对象，不留悬空引用。

#### 4.2.4 代码实践

**实践目标**：通过阅读 + 断点式推演，确认一次 clone 事件前后各张量长度与统计状态的变化。

**操作步骤**：

1. 打开 [train.py:246-252](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L246-L252)，记下 `densify_and_prune` 的调用条件。
2. 在脑中（或纸上）跟踪第 600 次迭代（`densify_from_iter=500` 之后第一个 `densification_interval=100` 的倍数）：假设当前 10000 个高斯、clone 选中 200 个、split 选中 100 个。
3. 按顺序推演点数：`densify_and_clone` 后 10200 → `densify_and_split` 中 `postfix` 后 10200+200=10400 → 删父后 10400-100=10300。

**需要观察的现象**：控制台每次致密化打印的 `num_to_densify_pos` / `num_to_clone_pos` / `num_to_split_pos`，与进度条上 `gs_num` 的增量（train.py [L206-208](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L206-L208) 的 postfix 里有 `gs_num`）满足：净增量 = n_clone + n_split − 剪枝数。

**预期结果**：点数单调性不被保证（剪枝可能大于增生），但账目应能对上。待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么统计清零不能只对新加的点做？

**答案**：`accum/denom` 是按「点」对齐的逐点平均梯度，但致密化本身改变了「哪些点存在」这一集合的语义——被 split 的父高斯消失了，clone 出的子高斯虽然继承父的属性却没有自己的梯度记录。旧统计对新集合而言已经失去意义，逐点对应关系断裂，整体清零是最干净的做法。

**练习 2**：`denom` 清零后，下一次 `densify_and_prune` 之前必须积累多少个 iteration 的统计才稳妥？

**答案**：至少 1 个。`add_densification_stats(_grad)` 每次对可见点 `denom += 1`，而 `densify_and_prune` 里对 NaN（denom 为 0 的点）做了置零保护（[L750-751](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L750-L751)），所以即使某点一直不可见也不会产生 NaN 污染。实际节奏由 `densification_interval=100` 决定，即每轮致密化基于约 100 个 iteration 的证据。

### 4.3 优化器状态同步：cat / prune / replace 三件套

#### 4.3.1 概念说明

Adam 的 `exp_avg` / `exp_avg_sq` 与参数形状严格一致。高斯数量变化时参数行数变了，三件套分别处理三种情况：

| 函数 | 场景 | 对动量状态的处理 |
|---|---|---|
| `cat_tensors_to_optimizer` | 追加新点（clone/split） | 旧部分保留，新点补零 |
| `_prune_optimizer` | 删除点（prune） | 按掩码同步删除对应行 |
| `replace_tensor_to_optimizer` | 整体替换（`reset_opacity`） | 全部清零 |

共同套路：**删掉旧的 state 键 → 用新张量造新 `nn.Parameter` → 把修好的 state 挂回新参数**。这一删一挂，就是「绕过 PyTorch 以参数对象为键」的手工状态迁移。

#### 4.3.2 核心流程

```
cat_tensors_to_optimizer(tensors_dict):
    for 每个参数组 group:
        ext = tensors_dict[group.name]
        state = optimizer.state.get(group.params[0])       # 可能还没有（首步 step 前）
        state.exp_avg     = cat(state.exp_avg,     zeros_like(ext))
        state.exp_avg_sq  = cat(state.exp_avg_sq,  zeros_like(ext))
        del optimizer.state[旧参数]
        group.params[0] = nn.Parameter(cat(旧参数, ext).requires_grad_(True))
        optimizer.state[新参数] = state
```

新点的动量为零，等于告诉 Adam：「这些点从现在才开始学，没有历史包袱」。

#### 4.3.3 源码精读

**cat**（[scene/gaussian_model.py:622-642](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L622-L642)）：

```python
for group in self.optimizer.param_groups:
    assert len(group["params"]) == 1, print(group["params"])
    extension_tensor = tensors_dict[group["name"]]
    stored_state = self.optimizer.state.get(group['params'][0], None)
    if stored_state is not None:
        stored_state["exp_avg"] = torch.cat((stored_state["exp_avg"], torch.zeros_like(extension_tensor)), dim=0)
        stored_state["exp_avg_sq"] = torch.cat((stored_state["exp_avg_sq"], torch.zeros_like(extension_tensor)), dim=0)
        del self.optimizer.state[group['params'][0]]
        group["params"][0] = nn.Parameter(torch.cat((group["params"][0], extension_tensor), dim=0).requires_grad_(True))
        self.optimizer.state[group["params"][0]] = stored_state
    else:
        group["params"][0] = nn.Parameter(torch.cat((group["params"][0], extension_tensor), dim=0).requires_grad_(True))
```

`else` 分支处理「优化器还没 step 过、state 尚未初始化」的情况——只拼参数即可。开头的 `assert` 保证每个参数组恰好一个张量参数，这是整个「按行对齐」方案的前提。

**prune**（[scene/gaussian_model.py:543-559](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L543-L559)）：

```python
stored_state["exp_avg"] = stored_state["exp_avg"][mask]
stored_state["exp_avg_sq"] = stored_state["exp_avg_sq"][mask]
...
group["params"][0] = nn.Parameter((group["params"][0][mask].requires_grad_(True)))
```

注意进入函数的 `mask` 语义是「保留哪些点」（`prune_points` 里传的是 `~删除掩码`），布尔索引同时作用于参数与两份动量，行对齐保持一致。

**replace**（[scene/gaussian_model.py:528-541](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L528-L541)）：形状不变、只换数值，动量整体 `zeros_like` 清零。它的唯一调用方是 `reset_opacity`（[L523-526](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L523-L526)）：把不透明度统一压到 0.01 让模型「重学」透明度——但在 4C4D 中 `--reset_opacity` 默认 False 且 opacity_decay 开启时被显式跳过（见 4.4 节），所以这条路径默认不激活。

**prune_points**（[scene/gaussian_model.py:561-582](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L561-L582)）在优化器之外还要同步模型侧的所有逐点张量：

```python
valid_points_mask = ~mask
optimizable_tensors = self._prune_optimizer(valid_points_mask)
self._xyz = optimizable_tensors["xyz"]
...
self.xyz_gradient_accum = self.xyz_gradient_accum[valid_points_mask]
self.denom = self.denom[valid_points_mask]
self.max_radii2D = self.max_radii2D[valid_points_mask]
if self.gaussian_dim == 4:
    ...
    self.t_gradient_accum = self.t_gradient_accum[valid_points_mask]
```

任何逐点张量漏剪一行，后续 `accum[filter] += ...` 就会形状错位报错——4D 分支里 `t_gradient_accum` 与属性一起被剪，正是这个原因。

#### 4.3.4 代码实践

**实践目标**：用最小 Adam 例子验证「以参数对象为键」的行为，理解为什么必须手工迁移状态。

**操作步骤**（示例代码，CPU 可运行）：

```python
# practice_optimizer_state.py（示例代码，独立于仓库）
import torch
from torch import nn

p = nn.Parameter(torch.zeros(4, 1))
opt = torch.optim.Adam([p], lr=0.1)

p.grad = torch.ones_like(p)
opt.step()
print("step 后有状态:", len(opt.state[p]) > 0, "exp_avg[:2]:", opt.state[p]["exp_avg"][:2].flatten())

# 模拟 cat：直接改 p.data 会怎样？——先看正确的做法
state = opt.state[p]
state["exp_avg"] = torch.cat([state["exp_avg"], torch.zeros(2, 1)])
del opt.state[p]
p2 = nn.Parameter(torch.cat([p.detach(), torch.ones(2, 1)]).requires_grad_(True))
opt.state[p2] = p2                     # 故意挂错：挂的是参数本身而非 state
opt.param_groups[0]["params"][0] = p2
p2.grad = torch.ones_like(p2)
try:
    opt.step()
except Exception as e:
    print("挂错状态时 step 报错:", type(e).__name__)
```

把 `opt.state[p2] = p2` 改成 `opt.state[p2] = state` 再跑一次。

**需要观察的现象**：挂错时 Adam 在 step 内部访问 `state["exp_avg"]` 会抛 `KeyError`/`TypeError`；挂对后 step 成功，且旧 4 行动量保留、新 2 行为零（可打印 `opt.state[p2]["exp_avg"]` 确认）。

**预期结果**：印证 4.3.1 的结论——参数对象换了，state 必须显式迁移；新行动为零即「新点无历史动量」。

#### 4.3.5 小练习与答案

**练习 1**：如果 `cat_tensors_to_optimizer` 里不做 `del self.optimizer.state[...]`，只替换 `group["params"][0]`，会怎样？

**答案**：旧 state 仍挂在旧参数对象上成为孤儿（内存层面等 GC 回收），而新参数在 state 中查无记录。Adam 首次 step 时会按「无状态」初始化新参数的动量——旧点辛苦积累的动量全部丢失，等价于所有点的 Adam 历史被静默重置，训练节奏被打断。

**练习 2**：为什么 `_prune_optimizer` 里有一个 `stored_state is not None` 的 else 分支，而 prune 也要处理「没有状态」的情况？

**答案**：`optimizer.state` 是惰性初始化的——在第一次 `step()` 之前它为空。致密化最早发生在第 501 次迭代（`densify_from_iter=500`）之后，理论上早已 step 过；但代码作为通用库保留了对「尚无状态」的兼容：此时只需用掩码索引参数本身，无需迁移状态。

**练习 3**：clone 新增的点在接下来第一次 `optimizer.step()` 时，Adam 的更新步长和「老点」一样吗？

**答案**：基本一样。Adam 的单步更新是 \(\eta \cdot \hat{m}/(\sqrt{\hat{v}}+\epsilon)\)（偏差修正后约为当前梯度方向的符号化步长），新点动量虽为零，但 step 会立刻用当前梯度填入，经偏差修正后量级与老点相近；差别主要从第二步起才显现（老点带历史滑动平均）。另外本仓库 Adam 使用极小的 `eps=1e-15`（[L505](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L505)），进一步弱化了零动量的影响。

### 4.4 三重剪枝与 train.py 的致密化调度

#### 4.4.1 概念说明

`densify_and_prune` 是致密化的总入口：先（可选地）做 clone+split，再统一剪枝。剪枝掩码由三个条件取或得到：

1. **opacity 条件（透明度）**：\(\alpha < 0.005\)。近乎透明的高斯对渲染贡献可忽略，留着只浪费显存与算力。阈值 `thresh_opa_prune` 是激活后（sigmoid 后）的不透明度。
2. **屏幕尺寸条件（big_points_vs）**：`max_radii2D > max_screen_size`（20 像素）。一个高斯在屏幕上的投影半径超过 20 像素，说明它大到「糊」的程度，通常是漂移到相机前的雾状点。
3. **世界尺寸条件（big_points_ws）**：\(\max_i s_i > 0.1 \times \text{extent}\)。物理世界中三轴最大尺度超过场景半径的十分之一，属于病态大高斯。

后两个条件只在 `max_screen_size` 为真值时启用（`if max_screen_size:`），而 4C4D 默认配置下 `size_threshold` 会被置为 `None`（见下文源码），所以**默认实际生效的只有 opacity 条件**。这是 4C4D 相对 3DGS 的一个策略变化：配合 opacity decay，靠「衰减不透明度 → 透明度剪枝自然淘汰」来控制点数，而不是靠尺寸剪枝。

#### 4.4.2 核心流程

train.py 侧的调度逻辑（每 iteration 判定一次）：

```
若 iteration < densify_until_iter:
    max_radii2D[可见] = max(max_radii2D[可见], radii[可见])     # 累计屏幕半径
    add_densification_stats(_grad)(...)                          # 累计梯度（u5-l3）
    若 iteration > densify_from_iter 且 iteration % densification_interval == 0:
        size_threshold = 20  (仅当 iteration > opacity_reset_interval 且 add_size_threshold)
        若 opacity_decay: size_threshold = None                  # 4C4D 默认走这里
        prune_only = densify_until_num_points > 0 且 当前点数 ≥ densify_until_num_points
        densify_and_prune(densify_grad_threshold, thresh_opa_prune,
                          cameras_extent, size_threshold, densify_grad_t_threshold,
                          prune_only=prune_only)
    （reset_opacity 分支：默认关闭，见源码精读）
```

`prune_only` 是「点数刹车」：当点数达到 `densify_until_num_points`（官方 yaml 为 4 200 000）后，跳过 clone/split、只执行剪枝，防止显存爆炸。

#### 4.4.3 源码精读

**`densify_and_prune` 总入口**（[scene/gaussian_model.py:748-768](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L748-L768)）：

```python
if not prune_only:
    grads = self.xyz_gradient_accum / self.denom
    grads[grads.isnan()] = 0.0
    if self.gaussian_dim == 4:
        grads_t = self.t_gradient_accum / self.denom
        grads_t[grads_t.isnan()] = 0.0
    ...
    self.densify_and_clone(grads, max_grad, extent, grads_t, max_grad_t)
    self.densify_and_split(grads, max_grad, extent, grads_t, max_grad_t)

prune_mask = (self.get_opacity < min_opacity).squeeze()
if max_screen_size:
    big_points_vs = self.max_radii2D > max_screen_size
    big_points_ws = self.get_scaling.max(dim=1).values > 0.1 * extent
    prune_mask = torch.logical_or(torch.logical_or(prune_mask, big_points_vs), big_points_ws)
self.prune_points(prune_mask)
torch.cuda.empty_cache()
```

三个条件对应的正是 4.4.1 的列表；`get_opacity` 是 sigmoid 后的激活值，`max_radii2D` 由 train.py 逐 iteration 累计（见下）。末尾 `torch.cuda.empty_cache()` 在大量删点后归还显存给驱动。

**train.py 的调度段**（[train.py:234-256](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L234-L256)）：

```python
if iteration < opt.densify_until_iter:
    gaussians.max_radii2D[visibility_filter] = torch.max(
        gaussians.max_radii2D[visibility_filter], radii[visibility_filter])
    if batch_size == 1:
        gaussians.add_densification_stats(viewspace_point_tensor, visibility_filter, ...)
    else:
        gaussians.add_densification_stats_grad(batch_viewspace_point_grad, visibility_filter, ...)
    if iteration > opt.densify_from_iter and iteration % opt.densification_interval == 0:
        size_threshold = 20 if iteration > opt.opacity_reset_interval and args.add_size_threshold else None
        if args.opacity_decay:
            size_threshold = None
        prune_only = opt.densify_until_num_points > 0 and gaussians.get_xyz.shape[0] >= opt.densify_until_num_points
        gaussians.densify_and_prune(opt.densify_grad_threshold, opt.thresh_opa_prune, scene.cameras_extent,
                                    size_threshold, opt.densify_grad_t_threshold, prune_only=prune_only)
    if ((iteration % opt.opacity_reset_interval == 0 and not args.opacity_decay) or (
        dataset.white_background and iteration == opt.densify_from_iter)) and args.reset_opacity:
        gaussians.reset_opacity()
```

四个与 4C4D 策略强相关的联动（承接 u5-l1 的结论，这里看到具体代码）：

1. **致密化时间窗被 opacity_decay 拉长到全程**：[train.py:473-474](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L473-L474) 在参数合并之后执行 `if args.opacity_decay: args.densify_until_iter = args.iterations`——注意它**晚于 yaml 合并**（[L434-443](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L434-L443)），所以 yaml 里写的 `densify_until_iter: 15_000` 在默认 `opacity_decay=True`（[L412](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L412)，`action="store_true"` 且 `default=True`，CLI 无法关闭、只能靠 yaml 写 `opacity_decay: false`）时不生效，真正的刹车只剩 `densify_until_num_points`。
2. **size_threshold 双重压制**：`add_size_threshold` 默认 False；即使打开，`opacity_decay` 也会强制 `size_threshold = None`。所以默认训练里 `max_screen_size` 恒为 None，三重剪枝退化为「仅 opacity」。
3. **reset_opacity 默认关闭**：`--reset_opacity` 默认 False（[L428](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L428)），且 opacity_decay 时其触发条件也被排除——3DGS 的周期性透明度重置与 Neural Decaying Function 的持续衰减互斥（详细动机在 u6-l4）。
4. **`--max_num_pts` 是 `densify_until_num_points` 的 CLI 直通门**：[train.py:451-452](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L451-L452) 把它写入 `opt.densify_until_num_points`，做消融或显存受限时用它给点数封顶非常方便。

**默认值来源**（[arguments/__init__.py:94-103](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/arguments/__init__.py#L94-L103)）：

```python
self.percent_dense = 0.01
self.thresh_opa_prune = 0.005
self.densification_interval = 100
self.opacity_reset_interval = 3000
self.densify_from_iter = 500
self.densify_until_iter = 15_000
self.densify_grad_threshold = 0.0002
self.densify_grad_t_threshold = 0.0002 / 40
self.densify_until_num_points = -1
```

官方 dynerf yaml（如 [configs/dynerf/flame_steak.yaml:50-56](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/configs/dynerf/flame_steak.yaml#L50-L56)）把这些键全部覆写了一遍，其中 `densify_until_num_points: 4200000` 是唯一与代码默认（-1，即不封顶）实质不同的值。

#### 4.4.4 代码实践

**实践目标**：搞清楚「默认配置下三重剪枝实际有几个在干活」，并学会用 `--max_num_pts` 做点数封顶。

**操作步骤**：

1. 静态推演：对照 [train.py:247-249](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L247-L249)，写出 `args.add_size_threshold=False`、`args.opacity_decay=True`、`args.reset_opacity=False` 三个默认值下，`size_threshold`、`prune_mask` 的生效条件、`reset_opacity` 的触发情况。
2. 阅读一个输出目录的 `training_params.txt`（train.py [L476-479](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L476-L479) 写出），确认 `opacity_decay=True`、`densify_until_iter` 是否已被改成 `iterations`。

**需要观察的现象**：`training_params.txt` 中 `densify_until_iter=30000`（而非 yaml 的 15000），证明 [train.py:473-474](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L473-L474) 的覆盖确实发生。

**预期结果**：默认配置下剪枝只有 opacity 一个条件在起作用；`--max_num_pts 1000000` 可让超过 100 万点后进入 prune_only 模式。待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：opacity 剪枝条件用 `get_opacity`（激活后）而不是 `_opacity`（裸值），有什么区别？

**答案**：`_opacity` 是 sigmoid 的反函数域上的裸值，理论上无界；0.005 的阈值只有在 sigmoid 之后才有「透明度百分比」的物理含义。若误用裸值，sigmoid(0) = 0.5，会把大量不透明度高斯误判为「透明」而剪掉。

**练习 2**：`big_points_vs` 与 `big_points_ws` 都在防「大高斯」，为什么要分屏幕和世界两套？

**答案**：`max_radii2D` 是**相机相关**的量——同一个高斯离相机越近投影越大；`get_scaling.max()` 是**世界系**的物理尺度。世界系大高斯未必在某台相机里投影大（比如远处的大背景椭球），屏幕大高斯也未必物理大（近处小点漂移）。两个条件互补，分别拦截「对某视角显脏」和「物理病态」两类异常。4C4D 默认两者都关，把淘汰权交给 opacity decay 体系。

**练习 3**：`prune_only=True` 的那一轮，`grads` 还会被计算吗？

**答案**：不会。`densify_and_prune` 用 `if not prune_only:` 包住了梯度计算与 clone/split（[L749-759](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L749-L759)），prune_only 轮直接跳到剪枝段。但注意 train.py 侧 `add_densification_stats` 仍在每 iteration 累计统计——只是这些统计不再被消费。

## 5. 综合实践

**任务**：修改 `densification_interval` 与 `densify_grad_threshold` 各一档，先**写下**对高斯数量曲线的预测，再用 TensorBoard 的 `total_points` 曲线验证；同时书面解释 `prune_mask` 三个条件的含义与默认配置下的生效情况。

**操作步骤**：

1. **建立基线**。复制官方配置做两组实验（有 GPU 时）：

   ```bash
   cp configs/dynerf/flame_steak.yaml configs/dynerf/flame_steak_dense.yaml
   # 实验组只改两处：
   #   densification_interval: 200        # 原 100
   #   densify_grad_threshold: 0.0004     # 原 0.0002
   python train.py --config configs/dynerf/flame_steak.yaml --model_path output/baseline
   python train.py --config configs/dynerf/flame_steak_dense.yaml --model_path output/dense
   ```

   注意 u1-l4 的结论：yaml 中的这些键属于 OptimizationParams 白名单，`recursive_merge` 会无条件覆盖命令行默认值，所以改 yaml 即生效，无需加命令行参数。

2. **先写预测**（在跑之前！）：

   | 改动 | 对 total_points 曲线的预测 | 理由 |
   |---|---|---|
   | interval 100→200 | 增长斜率约减半 | 致密化机会减半；`accum/denom` 是均值，阈值语义不变 |
   | threshold 2e-4→4e-4 | 每轮选中点数明显减少，最终点数更低 | 过阈值点比例下降（`num_to_densify_pos` 打印可直接对照） |
   | 两者叠加 | 增长显著放缓，更晚（甚至永远不）触及 4.2M 的 prune_only 上限 | 两个因素同向 |

   同时预测一个容易忽略的点：由于默认 `opacity_decay=True`，致密化窗口是全部 30 000 迭代（train.py [L473-474](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L473-L474) 的覆盖），曲线在 15 000 之后**仍会继续变化**——不要拿 3DGS「15 000 后点数冻结」的直觉来预测。

3. **验证**：

   ```bash
   tensorboard --logdir output
   ```

   对比两条 `total_points` 曲线（该标量由 [train.py:311](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L311) 每 100 迭代记录一次），并对照控制台每轮致密化打印的 `num_to_densify_pos` / `num_to_clone_pos` / `num_to_split_pos`。

4. **书面解释三重剪枝**（无 GPU 也必须完成的部分）：按 4.4.1 的表格，用自己的话写清楚 opacity（激活后透明度 < 0.005，剔除无贡献点）、屏幕尺寸（max_radii2D > 20px，剔除贴脸糊屏点）、世界尺寸（max scaling > 0.1×extent，剔除物理病态大点）三条件，并注明默认 `size_threshold=None` 时只有第一条生效。

**需要观察的现象**：两组曲线的斜率差、最终点数差、是否触及 4.2M 平台（prune_only 生效时曲线变成锯齿状缓降——只剪不增）；`opacity_histogram`（[train.py:312](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L312)）随衰减推进整体左移，与 opacity 剪枝的联动。

**预期结果**：预测表逐条核对；若实验组 PSNR 明显下降，说明点数不足是瓶颈，可回退 threshold。无 GPU 环境请把第 1-2 步写成完整实验设计文档（含预测表），标注「待本地验证」。

## 6. 本讲小结

- clone 与 split 用同一条梯度阈值 `densify_grad_threshold`，靠 `max(scaling) ≤ / > percent_dense × cameras_extent` 分流：小而差 → 复制，大而差 → 一换二（子尺度 \(s/(0.8N)\)）。
- `rot_4d=True` 时 split 在旋转后的 4D 椭球内**联合**采样 xyzt 偏移，保留时空耦合；否则 xyz 与 t 独立采样。时间梯度阈值 `densify_grad_t_threshold` 虽被一路传参，但 clone/split 函数体内均未消费，属未接线参数。
- `densification_postfix` 负责新点落库，并把 `xyz_gradient_accum`、`t_gradient_accum`、`denom`、`max_radii2D` 全部清零重建——每轮致密化决策只基于最近一个 interval 的梯度证据。
- `cat_tensors_to_optimizer` / `_prune_optimizer` / `replace_tensor_to_optimizer` 解决「Adam 状态以参数对象为键」的问题：增点补零动量、删点同步裁动量、整体替换清零动量；`prune_points` 还要同步剪模型侧所有逐点张量。
- 剪枝三条件：opacity < 0.005（默认唯一生效）、屏幕半径 > 20px、世界尺度 > 0.1×extent（后两者依赖 `size_threshold`，默认为 None）。
- 4C4D 默认（opacity_decay=True）的三条联动：致密化窗口拉满全程、尺寸剪枝关闭、reset_opacity 关闭；点数上限靠 `densify_until_num_points`（yaml 4.2M，或 CLI `--max_num_pts`）+ prune_only 兜底。

## 7. 下一步学习建议

- 下一讲 **u5-l5 检查点、日志与输出目录**：本讲反复出现的 `total_points` / `opacity_histogram` 曲线、以及致密化清零后优化器状态如何进入 `capture()` 的 21 元组，都将在检查点一讲中收口。
- 建议回头精读 [train.py:234-261](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L234-L261)，把「致密化块」与「optimizer.step」的先后顺序（先致密化、后 step）与自己记忆中的调用链核对一遍。
- 进入单元 6 前的思考题：opacity decay 让不透明度持续小幅衰减，本讲的「opacity < 0.005 剪枝」会因此变得更频繁还是更罕见？带着答案去读 **u6-l2（opacity_decay 模式族）** 与 **u6-l4（联合优化与训练策略联动）**，检验你的直觉。
