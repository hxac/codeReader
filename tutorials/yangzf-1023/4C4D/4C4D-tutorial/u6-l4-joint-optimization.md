# 联合优化：双优化器与训练策略联动

## 1. 本讲目标

本讲是「Neural Decaying Function」单元的收尾。前三讲我们已经看清了三个孤立的事实：`Coefficient` 是一个 353 参数的两层 MLP（u6-l1）；`opacity_decay` 把衰减因子乘进有效不透明度并写回 `_opacity`（u6-l2）；渲染器在 `decay_from_iter=500` 之后、只对「空间∧时间可见」的高斯施加衰减（u6-l3）。本讲把这些事实组装成一台完整的机器——**联合优化**：

1. 解释衰减如何**放大不透明度梯度的有效信号**（注意：不是放大梯度数值，而是放大它的筛选作用）；
2. 说出 `opacity_decay` 开启后训练策略的**四个联动变化**；
3. 理解 `coef_optimizer` 中 `coefficient_lr=1e-5` 与 `weight_decay=1e-4` 对这个小网络的正则化作用。

学完本讲，你应该能回答：为什么一个小 MLP 必须和几百万个 4D 高斯**同时**训练，而不是先训高斯再拟合衰减函数？为什么开启衰减的同时要拉满致密化窗口、禁用不透明度重置？

## 2. 前置知识

本讲默认你已读过 u5-l1（训练主循环）、u6-l1~l6-l3。三个前置概念先对齐：

- **参数组（param group）与双优化器**：PyTorch 的一个 optimizer 管理一组可训练参数。`GaussianModel` 里的高斯参数（`_xyz`、`_opacity` 等）由 `self.optimizer` 管理；衰减网络 `Coefficient` 的权重是另一批参数，属于另一个 `Adam` 实例 `self.coef_optimizer`。两个优化器互不知道对方，但可以被**同一次 `loss.backward()`** 同时喂饱梯度——只要它们出现在同一张计算图上。
- **双通路写回**（u6-l2 的结论）：`opacity_decay` 返回携梯度的张量进渲染器（这是衰减网络拿梯度的唯一通路），同时用 `self._opacity.data = ...` 原位写回裸值参数、绕过 autograd（这是状态持久化通路，不参与梯度图）。本讲反复依赖这条「一进一出」的结构。
- **训练策略联动**：指一个开关（这里是 `--opacity_decay`）同时改写多条彼此独立的训练超参数分支。读懂联动，才能在改参数时预判全链条后果，而不是只改了一处。

一个需要时刻记住的数字直觉：衰减因子被限制在 \([f_{\min}, f_{\max}]=[0.996, 0.998]\) 的窄带里，单次乘法几乎不动声色；但训练数万次迭代连乘下来，\(0.1 \times 0.997^{1000} \approx 0.005\)——恰好跌破剪枝线 `thresh_opa_prune=0.005`。**联合优化的全部戏剧性都来自这个复利效应。**

## 3. 本讲源码地图

| 文件 | 本讲关注点 |
| --- | --- |
| `train.py` | `coefficient` 的构造与注入（L57-L63）、致密化/reset 联动分支（L235-L256）、双优化器交替 step（L258-L269）、`densify_until_iter` 覆盖（L473-L474）、`weight_decay` 覆盖链（L460-L461） |
| `scene/gaussian_model.py` | `training_setup` 中 `coef_optimizer` 的创建（L501-L503）、`reset_opacity`（L523-L526）、`opacity_decay`（L584-L620）、`capture/restore` 中衰减网络的状态（L112-L114、L130-L138、L187-L191） |
| `module/__init__.py` | `Coefficient` 的网络结构与 `forward` 的标准化预处理（作为梯度通路的一环回顾） |
| `arguments/__init__.py` | `coefficient_lr`、`coefficient_weight_decay` 的默认值（L92-L93） |
| `gaussian_renderer/__init__.py` | 衰减接入段（L63-L75），联合优化的「梯度交汇点」 |
| `configs/dynerf/flame_steak.yaml` | 官方配置中衰减网络与致密化的实际取值（L43-L44、L51-L56） |

## 4. 核心概念与源码讲解

### 4.1 coef_optimizer：衰减网络的独立优化器

#### 4.1.1 概念说明

「联合优化」的字面含义是：两套参数、两个损失贡献方、**两个独立的 Adam 优化器**，在同一个训练循环里交替更新。为什么不给衰减网络也塞进 `gaussians.optimizer` 的参数组列表？

因为两者在「生命周期」上完全不同步：高斯参数会被致密化机制**增删**——每次 clone/split 都要 `cat_tensors_to_optimizer` 补零动量、每次 prune 都要裁剪动量（u5-l4）；而 `Coefficient` 的 353 个权重从头到尾固定不变。混在一个优化器里，致密化的张量手术就要额外照顾一组不相关的参数；拆成两个优化器，`_prune_optimizer`/`cat_tensors_to_optimizer` 只需遍历高斯参数组，衰减网络完全不受扰动。这是「按生命周期分组」的工程设计。

#### 4.1.2 核心流程

```text
train.py 主流程
├─ args.opacity_decay == True
│    └─ coefficient = Coefficient().cuda()          # 仅训练入口构造
├─ GaussianModel(..., coefficient=coefficient)       # 注入模型
└─ gaussians.training_setup(opt)
     ├─ l = [高斯参数组 ×8~9]                        # xyz/f_dc/f_rest/opacity/scaling/rotation/t/scaling_t/rotation_r
     ├─ if self.coefficient is not None:
     │     coef_optimizer = Adam(coefficient.parameters(),
     │                            lr=coefficient_lr,            # 1e-5
     │                            eps=1e-15,
     │                            weight_decay=coefficient_weight_decay)  # 1e-4
     └─ optimizer = Adam(l, lr=0.0, eps=1e-15)       # 高斯侧
```

#### 4.1.3 源码精读

训练入口按开关决定是否构造衰减网络，并把它作为构造参数注入 `GaussianModel`——`render.py` 推理入口不走这段，所以衰减网络只在训练时存在：

[train.py:L57-L67](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L57-L67)
这段代码在 `opacity_decay` 开启时构造 `Coefficient` 并 `.cuda()`，随后把它传入 `GaussianModel`，最后 `gaussians.training_setup(opt)` 一次性创建**两个**优化器。

`GaussianModel` 侧对衰减网络的两处登记——`__init__` 里的占位与成员引用：

[scene/gaussian_model.py:L76](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L76) 与 [scene/gaussian_model.py:L95](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L95)
`self.coef_optimizer = None` 先占位（关闭衰减时保持为 `None`），`self.coefficient = coefficient` 保存网络引用。这个 `None` 约定贯穿全文件：`capture`、`restore`、训练循环都靠 `is not None` 判断衰减通路是否存在。

`training_setup` 中 `coef_optimizer` 的真正创建，紧跟在高斯参数组列表 `l` 之后：

[scene/gaussian_model.py:L501-L505](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L501-L505)
若注入了衰减网络，就用 `torch.optim.Adam` 单独为它建优化器：学习率取 `training_args.coefficient_lr`（默认 `1e-5`），`eps=1e-15` 与高斯优化器一致，另加 `weight_decay=training_args.coefficient_weight_decay`（默认 `1e-4`）。下一行的高斯优化器参数组里**没有**衰减网络的权重——两组参数彻底分家。

两个超参的默认值定义在 `OptimizationParams` 中：

[arguments/__init__.py:L92-L93](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/arguments/__init__.py#L92-L93)
`coefficient_lr = 1e-5`、`coefficient_weight_decay = 1e-4`。与高斯侧 `opacity_lr=0.05` 相差**四个数量级**——衰减函数只允许以极慢的速度演化。

这两个值如何生效？有一条容易踩的覆盖链。`train.py` 注册了脚本级参数 `--weight_decay`（默认 `1e-4`），在 yaml 合并之后执行一次赋值：

[train.py:L460-L461](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L460-L461)
`if args.weight_decay:` 为真时，把脚本级 `weight_decay` 覆盖到 `coefficient_weight_decay` 上。注意守卫是**真值判断而非非空判断**：若你运行 `--weight_decay 0`，`0` 为假值，覆盖被跳过，`coefficient_weight_decay` 保持 yaml 里的 `1e-4`——即**无法用命令行把它清零**，只能改 yaml 或给一个极小的非零值。

联合优化的状态也会被完整持久化。`capture` 元组里衰减网络占两席（以 4D 分支为例）：

[scene/gaussian_model.py:L130](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L130) 与 [scene/gaussian_model.py:L138](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L138)
第 12 项是 `coef_optimizer.state_dict()`（含 Adam 动量），第 20 项（末项）是 `coefficient.state_dict()`。续训恢复时二者被条件装回：

[scene/gaussian_model.py:L187-L191](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L187-L191)
`restore` 先由 `training_setup(training_args)` 重建两个优化器，再 `load_state_dict` 恢复网络权重与 `coef_optimizer` 动量。「联合」的完整含义包括：**从检查点续训时，两个优化器的 Adam 状态都必须回到中断现场**，否则衰减速率会因动量丢失而突变。

最后看一个正则化视角的细节——`weight_decay` 作用在什么上。`Coefficient` 的全部可训练权重：

[module/__init__.py:L17-L23](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/module/__init__.py#L17-L23)
`Linear(9,32) → ReLU → Dropout(0.1) → Linear(32,1) → Sigmoid`，参数量 \(9\times32+32 + 32\times1+1 = 353\)。PyTorch 1.12 的 `Adam` 中 `weight_decay` 等价于在梯度上加 \( \lambda \theta \)（L2 正则），即每步把权重往 0 拉 \(10^{-4}\) 比例的一小步。对 353 参数的小网络，这防止它过度拟合「当前这一批高斯」的分布——而高斯分布在致密化中每 100 迭代就变一次，输入分布持续漂移，正则让衰减函数保持平滑、缓变。`dropout_rate=0.1` 同理，是另一重防过拟合。

#### 4.1.4 代码实践

**实践目标**：亲手确认「双优化器分家」——`coef_optimizer` 只含 353 个参数、8 个参数张量之外的权重一概不碰。

1. 操作步骤（需 GPU 与已编译的 `simple-knn`，**待本地验证**）：在仓库根目录运行下面的最小脚本（示例代码）：

```python
# 示例代码：inspect_coef_optimizer.py
import sys, torch
sys.argv = ["train.py"]  # 占位，避免 ParamGroup 读到无关参数
from argparse import ArgumentParser
from arguments import ModelParams, OptimizationParams
from scene import GaussianModel
from module import Coefficient
import numpy as np
from utils.graphics_utils import BasicPointCloud

parser = ArgumentParser()
lp = ModelParams(parser); op = OptimizationParams(parser)
args = parser.parse_args(sys.argv[1:])

coefficient = Coefficient().cuda()
g = GaussianModel(sh_degree=3, gaussian_dim=4, time_duration=[0, 10], rot_4d=True,
                  force_sh_3d=False, sh_degree_t=2, coefficient=coefficient)
n = 1000
pcd = BasicPointCloud(points=np.random.rand(n, 3).astype(np.float32) * 10,
                      colors=np.random.rand(n, 3).astype(np.float32),
                      normals=np.zeros((n, 3)), time=np.random.rand(n, 1).astype(np.float32) * 10)
g.create_from_pcd(pcd, spatial_lr_scale=1.0)
g.training_setup(op.extract(args))

print("高斯优化器参数组:", [(grp["name"], grp["lr"]) for grp in g.optimizer.param_groups])
print("coef_optimizer 参数张量数:", len(list(coefficient.parameters())))
print("coef_optimizer 参数总量:", sum(p.numel() for p in coefficient.parameters()))
print("coef lr:", g.coef_optimizer.param_groups[0]["lr"],
      "weight_decay:", g.coef_optimizer.param_groups[0]["weight_decay"])
```

2. 需要观察的现象：高斯优化器有 9 个参数组（`rot_4d=True` 时含 `rotation_r`）；`coef_optimizer` 恰好 8 个参数张量、353 个参数。
3. 预期结果：两组数字与上述一致；`coef lr` 打印 `1e-05`、`weight_decay` 打印 `0.0001`。
4. 无 GPU 时的替代：直接对照 [scene/gaussian_model.py:L484-L499](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L484-L499) 手工数参数组，对照 [module/__init__.py:L17-L23](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/module/__init__.py#L17-L23) 手工算 353。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `coefficient_lr=1e-5` 比 `opacity_lr=0.05` 小四个数量级，而训练仍能在三万迭代内收敛出有用的衰减函数？

**参考答案**：衰减因子的输出区间被 `Sigmoid + [f_min,f_max] 线性映射` 双重压缩到宽仅 0.002 的窄带，网络权重微小变化对渲染结果的影响本来就小；同时衰减的价值在于**方向性与持续性**而非幅度——复利效应（u6-l3：约 750~1500 次可见迭代跌穿 0.005）把微小偏好放大成剪枝决策。小学习率保证衰减函数缓变、可被高斯参数的梯度「追上并反驳」，避免早期几何未成型时网络快速杀死大量高斯。

**练习 2**：如果把 `Coefficient` 的权重也塞进 `gaussians.optimizer` 的参数组，会发生什么？

**参考答案**：功能上仍能更新权重，但会破坏两条既有机制：(1) `_prune_optimizer` 与 `cat_tensors_to_optimizer` 会按第一维（高斯数量 N）裁剪/拼接所有参数组张量（见 [scene/gaussian_model.py:L543-L559](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L543-L559)），网络权重形状是 \(320+33\) 的扁平参数，与高斯张量语义不匹配，致密化时会被错误处理；(2) `update_learning_rate` 与 `capture/restore` 的参数组遍历逻辑都要为「非高斯参数组」打补丁。拆开是更干净的设计。

**练习 3**：`--weight_decay 0` 能把衰减网络的 L2 正则关掉吗？

**参考答案**：不能。[train.py:L460-L461](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L460-L461) 的守卫是 `if args.weight_decay:`，`0` 为假值，覆盖不发生，`coefficient_weight_decay` 仍是 yaml/默认的 `1e-4`。要关闭只能在 yaml 中写 `coefficient_weight_decay: 0.0`。

### 4.2 一次 backward、两条通路：train.py 的双优化器交替 step

#### 4.2.1 概念说明

两个优化器要都能更新，前提是 `loss.backward()` 之后**双方参数的 `.grad` 都非空**。这要求高斯参数和 `Coefficient` 权重出现在同一张计算图上——而把它们连起来的，正是 u6-l3 精读过的渲染接入段：衰减网络的输出乘进有效不透明度后进入光栅化，光栅化输出参与 loss。于是一次 backward 同时回填两组梯度，训练循环只需要「依次 step、各自清零」。

值得先想清楚一个顺序问题：`gaussians.optimizer.step()` 在前、`coef_optimizer.step()` 在后，是否有数学含义？没有。两组参数互不相交、互不依赖对方的当前值（都只依赖 backward 时的旧值），交替顺序可任意交换——代码写成先后只是可读性安排。

#### 4.2.2 核心流程

一次迭代中与联合优化相关的梯度通路：

```text
for batch_idx in range(batch_size):
    render(...)                            # 内部调用 opacity_decay → Coefficient.forward
    │
    │  高斯侧通路: _xyz/_opacity/... ──→ 光栅化输入 ──→ image ──┐
    │  网络侧通路: Coefficient权重 ──→ f ──→ 有效opacity ──→ 光栅化输入 ──┤
    │                                                        ↓
    loss = (1-λ)L1 + λ(1-SSIM);  loss/batch_size;  loss.backward()
    │        （backward 同时填 _opacity.grad 与 net 各权重 .grad）
    │
    └→ 网络侧另有一条不进梯度图的通路: _opacity.data = inverse_sigmoid(衰减后值)   # 状态持久化
```

backward 之后（train.py 主循环）：

```text
if iteration < opt.iterations:
    gaussians.optimizer.step(); gaussians.optimizer.zero_grad(set_to_none=True)
    if gaussians.coefficient is not None:
        gaussians.coef_optimizer.step(); gaussians.coef_optimizer.zero_grad(set_to_none=True)
    if pipe.env_map_res and iteration < pipe.env_optimize_until:
        env_map_optimizer.step(); env_map_optimizer.zero_grad(...)      # 第三个可选优化器
```

#### 4.2.3 源码精读

先看梯度交汇点。渲染函数中衰减网络的输出如何进入计算图：

[gaussian_renderer/__init__.py:L63-L75](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L63-L75)
三重门控通过后，`pc.opacity_decay(...)` 的**返回值** `visible_opacities` 被赋给 `opacity`，随后作为光栅化输入——这条链路上的每一步都是可微算子，所以 loss 对 `Coefficient` 权重的梯度可以从像素一路回传到网络。而 `opacity_decay` 内部的 `self._opacity.data = ...` 写回（u6-l2）绕过 autograd，不在这张图上。

训练循环中的 loss 与 backward（batch 内逐视角累积，u5-l3 已详解）：

[train.py:L143-L148](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L143-L148)
`loss.backward()` 是两条梯度通路的共同触发点：它同时计算 loss 对高斯参数与对衰减网络权重的梯度。注意 batch 模式下这行在 batch 内循环执行、梯度跨视角累积。

然后是本讲的核心四行——双优化器交替 step：

[train.py:L258-L269](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L258-L269)
守卫 `if iteration < opt.iterations` 让最后一次迭代只评估不更新。先 `gaussians.optimizer.step()` 并清零高斯梯度；随后若衰减网络存在（`gaussians.coefficient is not None`），`coef_optimizer.step()` 更新网络权重并清零其梯度。再往后还有一个可选的 `env_map_optimizer`（环境贴图背景，u4-l3），说明这套「同一次 backward、多个优化器依次 step」是仓库的通用模式——衰减网络只是其中一例。

还有一个隐蔽的配合点：渲染调用必须**把 `args` 和 `iteration` 传进去**，衰减分支才会激活：

[train.py:L138](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L138)
训练循环里唯一传 `args=args, iteration=iteration` 的渲染调用。对照 [train.py:L332](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L332)（`training_report` 里的评估渲染）不传这两者——评估渲染不触发衰减，只读已累积的不透明度状态。这保证了「联合优化的梯度只在训练视角上产生」。

#### 4.2.4 代码实践

**实践目标**：验证「一次 backward 喂饱两个优化器」，并观察衰减网络梯度的量级。

1. 操作步骤（**待本地验证**，需完整训练环境）：在 [train.py:L263](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L263) 的 `if gaussians.coefficient is not None:` 分支内、`step()` 之前插入一行调试打印（示例代码，测完删除）：

```python
# 示例代码：调试打印，勿提交
print("coef grad norm:",
      sum(p.grad.norm().item() for p in gaussians.coefficient.parameters() if p.grad is not None))
```

2. 需要观察的现象：前 500 次迭代（`decay_from_iter=500` 之前）打印值为 0——衰减分支未激活、网络不在计算图上；第 501 次迭代起变为非零。
3. 预期结果：与上述一致。这直接证明衰减网络的梯度**只来自渲染损失**，没有任何额外的监督项（没有「衰减正则损失」，网络完全靠渲染梯度学会该衰减谁）。
4. 无 GPU 时的替代：静态追踪调用链 `train.py:138 render(args) → gaussian_renderer:74 opacity_decay → gaussian_model:605 coefficient(...) → 光栅化`，在纸上画出计算图并标注每个节点。

#### 4.2.5 小练习与答案

**练习 1**：`coef_optimizer.step()` 放在 `gaussians.optimizer.step()` 之后，为什么不影响正确性？

**参考答案**：两组参数不相交。`coef_optimizer.step()` 读的是 `coefficient.parameters()` 的 `.grad`（backward 时已定）与自身动量，与高斯参数是否已被更新无关；反之亦然。两个 step 交换顺序得到完全相同的结果。

**练习 2**：若有人把 `coef_optimizer.zero_grad` 误删，会出现什么症状？

**参考答案**：网络权重的梯度会跨迭代**累加**（PyTorch 的 `.grad` 是累加语义），等效于给衰减网络一个不断增大的有效学习率，衰减函数演化失控，训练中后期可能出现不透明度整体塌缩、`total_points` 异常骤降。

**练习 3**：为什么评估渲染（`training_report` 内）不传 `args`、从而不衰减，对「联合优化」是必要的？

**参考答案**：评估渲染在 `torch.no_grad()` 外层执行但结果只用于指标计算；若它也调用 `opacity_decay`，会额外把衰减写进 `_opacity`（`_opacity.data` 写回不依赖梯度），等于测试视角也在「惩罚」高斯，测试集信息泄漏进训练状态。不传 `args` 让衰减严格只由训练视角驱动。

### 4.3 opacity_decay 开启后的训练策略联动（四个变化）

#### 4.3.1 概念说明

`--opacity_decay` 不只是「多乘一个因子」。它是一个总开关，同时改写了四条看似独立的训练分支。理解联动的原因，比记住结论更重要——四个变化都服从同一条设计原则：**让「高斯的去留」只由『梯度 + 衰减』这一对竞争机制决定，排除其他会干扰这笔账的机制。」

四个联动变化一览：

| # | 变化 | 代码位置 | 原值 → 新值 |
| --- | --- | --- | --- |
| 1 | 致密化窗口拉满 | train.py L473-L474 | `densify_until_iter: 15_000` → `iterations`（30 000） |
| 2 | 屏幕尺寸剪枝强制关闭 | train.py L248-L249 | `size_threshold` → 恒为 `None` |
| 3 | 不透明度重置禁用 | train.py L254 | `reset_opacity` 触发支路恒假 |
| 4 | 新增 `coef_optimizer` 交替 step | train.py L263-L265 | 无 → 双优化器 |

#### 4.3.2 核心流程

```text
train.py __main__ 阶段（参数解析后、training() 之前）
├─ yaml 合并 (recursive_merge)
├─ if args.opacity_decay:
│      args.densify_until_iter = args.iterations     # 联动 1
└─ 写 training_params.txt

training() 主循环内
├─ if iteration < opt.densify_until_iter:            # 联动 1 使该窗口 = 全程
│    ├─ if iteration > densify_from_iter and iteration % densification_interval == 0:
│    │     size_threshold = 20 if iteration > opacity_reset_interval and add_size_threshold else None
│    │     if args.opacity_decay: size_threshold = None    # 联动 2
│    │     prune_only = (点数 >= densify_until_num_points)
│    │     densify_and_prune(...)
│    └─ if ((iteration % opacity_reset_interval == 0 and not args.opacity_decay) or
│           (white_background and iteration == densify_from_iter)) and args.reset_opacity:
│          reset_opacity()                                 # 联动 3：第一支路恒假
└─ optimizer step：gaussians → coef → env_map              # 联动 4
```

#### 4.3.3 源码精读

**联动 1：致密化窗口拉满全程。** 这发生在训练开始前的参数改写：

[train.py:L473-L474](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L473-L474)
`opacity_decay` 开启时 `densify_until_iter` 被改写为总迭代数。对照 yaml 中的原值：

[configs/dynerf/flame_steak.yaml:L53](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/configs/dynerf/flame_steak.yaml#L53)
配置里写的是 `15_000`，但这条命令行侧的覆盖发生在 yaml 合并**之后**，所以最终生效值是 30 000（可在输出目录 `training_params.txt` 中验证）。为什么必须拉满？衰减在全程持续「软删除」无效高斯（u6-l3：约 750~1500 次可见迭代跌穿剪枝线），删掉的容量必须由致密化回填——衰减清场、致密化补员，两者是一对「呼吸」机制，只在前半程呼吸一半就会让点数预算在后期枯竭。点数失控的风险由 [train.py:L250-L252](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L250-L252) 的 `prune_only` 兜底：点数达到 `densify_until_num_points`（官方 4.2M）后只剪不增。

**联动 2：屏幕尺寸剪枝强制关闭。**

[train.py:L246-L252](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L246-L252)
常规路径下 `size_threshold=20` 需要同时满足「过了 `opacity_reset_interval`」与「`--add_size_threshold`」（默认 False，见 [train.py:L429](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L429)）；而 `opacity_decay` 开启时**无条件置 `None`**——即使用户显式传了 `--add_size_threshold` 也会被覆盖。`size_threshold=None` 传入 `densify_and_prune` 后，[scene/gaussian_model.py:L762-L765](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L762-L765) 中屏幕尺寸（`max_radii2D > 20`）与世界尺寸两支剪枝条件整块跳过，只保留透明度剪枝。设计意图（分析）：尺寸剪枝按「看起来太大」删点，与衰减机制「由梯度和透明度决定去留」的原则冲突——动态场景中火焰、烟雾这类区域用大高斯表达可能恰恰是正确且高效的。

**联动 3：不透明度重置禁用。**

[train.py:L254-L256](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L254-L256)
`reset_opacity` 的触发条件是三重的：`(周期到期 且 未开衰减) 或 (白背景特例) 且 args.reset_opacity`。开启衰减后第一支路恒为假，仅白背景特支保留，且 `--reset_opacity` 本身默认 False（[train.py:L428](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L428)）。被禁用的操作是：

[scene/gaussian_model.py:L523-L526](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L523-L526)
把所有高斯不透明度无差别截断到 ≤0.01。这与衰减在语义上直接冲突：衰减的全部价值在于**不透明度分布携带了「哪些高斯被观测确证」的信息**（欠证的高斯已被压低、正被推向剪枝线）；一次 reset 把这笔账清零，让所有高斯（无论被确证与否）重新站在同一起跑线，联合优化积累的判别信息瞬间归零。所以开启衰减的同一个开关必须同时关掉 reset——两机制只能二选一。

**联动 4：双优化器交替 step。** 即 4.2 节精读的 [train.py:L263-L265](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L263-L265)。它与其余三条不同：前三条是「关掉与衰减竞争的旧机制」，这一条是「打开衰减自身的载体」。

顺带指出一处遗留（待确认）：`train.py` 注册了 `--dropout_rate`、`--hidden_dim`（[train.py:L416-L418](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L416-L418)），但 [train.py:L58](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L58) 构造网络用的是无参 `Coefficient()`，两个命令行参数从未传入——改它们不会影响训练（欲修改需改 `module/__init__.py` 的默认值）。

#### 4.3.4 代码实践

**实践目标**：不跑完训练，仅用启动阶段写出的 `training_params.txt` 验证联动 1，并静态推演联动 2/3。

1. 操作步骤：
   - 准备数据与一份 yaml（以 `flame_steak.yaml` 为模板，改 `model_path` 为新目录）。
   - 运行 `python train.py --config configs/dynerf/flame_steak.yaml --model_path output/probe_decay`。无论后续训练是否因数据报错，参数文件都已在 [train.py:L476-L479](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L476-L479) 写出（它在 `training()` 之前执行）。
   - 打开 `output/probe_decay/training_params.txt`，检索 `densify_until_iter`。
2. 需要观察的现象：yaml 里写的是 `15_000`（[flame_steak.yaml:L53](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/configs/dynerf/flame_steak.yaml#L53)），`training_params.txt` 里却是 `30000`。
3. 预期结果：`densify_until_iter=30000`，证明联动 1 在 yaml 合并之后生效；同时 `opacity_decay=True`、`reset_opacity=False`。
4. 若无法本地验证，写明「待本地验证」，并对照 [train.py:L473-L474](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L473-L474) 说明推演依据。

#### 4.3.5 小练习与答案

**练习 1**：如果关闭 `opacity_decay`（yaml 写 `opacity_decay: False`）但不做任何其他修改，`densify_until_iter` 会是多少？训练后期 `total_points` 曲线什么形态？

**参考答案**：保持 yaml 的 15 000。[train.py:L235](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L235) 的整块致密化（含剪枝与 reset）都在 `iteration < densify_until_iter` 之内，所以 15 000 之后既不增也不删，`total_points`（[train.py:L311](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L311)）是一条水平线。开启衰减时则是全程「增长—剪枝」交替的锯齿上升曲线（受 4.2M 上限约束）。

**练习 2**：官方配置里 `opacity_reset_interval: 10000`（[flame_steak.yaml:L51](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/configs/dynerf/flame_steak.yaml#L51)）在默认训练中还有作用吗？

**参考答案**：有，但只剩两个次要作用：(1) 它仍参与 [train.py:L247](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L247) 的 `size_threshold=20` 判定（尽管衰减开启时该值随即被覆盖为 None）；(2) 若用户显式关闭衰减并加 `--reset_opacity`，它重新成为重置周期。默认配置（衰减开、reset 关）下它实际不触发任何操作。

**练习 3**：四个联动中，哪一个是「新增机制」而非「关闭旧机制」？为什么其余三个都必须是关闭性的？

**参考答案**：联动 4（`coef_optimizer` 交替 step）是新增；联动 1 表面上是「拉长窗口」，本质也是为衰减服务的重配置。其余变化都关闭旧机制，因为旧的致密化终止时刻、尺寸剪枝、不透明度重置各自携带一套「高斯去留」的判据（时间到了、太大了、全员重置），与衰减的判据（梯度竞争 + 复利沉没）冲突；两套判据并存会互相抹掉对方积累的信息，所以开启衰减必须让旧判据全部退场。

### 4.4 机制分析：联合优化为何让梯度聚焦几何学习

#### 4.4.1 概念说明

u1-l1 建立过论文的核心洞察：稀疏视角下**几何学习远难于外观学习**——4 个视角不足以强约束每个高斯的三维位置，但足够让一堆「位置错误、颜色凑合」的半透明高斯叠加出看起来不错的训练视角。后者正是稀疏视角重建的典型失败模式：训练 PSNR 尚可、新视角崩坏。

联合优化是对这个病根的对症下药。关键澄清：**衰减并不放大梯度的数值，而是放大不透明度梯度的「筛选作用」**。单步看，\(f_k\in[0.996,0.998]\) 乘在梯度链上几乎不改变任何量级；跨迭代看，衰减提供了一股与「几何是否正确」无关的、持续向下的复利压力，于是只有那些**增大不透明度真的能降低 loss** 的高斯——即位置放对了、真能解释观测的高斯——才能获得足够的不透明度梯度逆流而上；其余高斯的有效不透明度螺旋沉没，最终被 0.005 剪枝线清除。不透明度由此从「一个普通的外观参数」升格为「几何正确性的积分记录仪」。

而这套筛选必须**联合**训练：若先训高斯再拟合一个事后衰减函数，网络看到的已是过拟合完成的高斯分布，学到的只是「复述现状」；只有在训练全程与高斯对抗，衰减压力才能持续参与「哪些高斯值得存在」的在线裁决。

> 说明：本节的推导是基于源码行为的机制分析，不是论文原文的复述。

#### 4.4.2 核心流程与数学

有效不透明度（衰减分支激活时）：

\[
\tilde{o}_k \;=\; \sigma(\theta_k)\cdot f_k,\qquad
f_k \;=\; f_{\min} + (f_{\max}-f_{\min})\cdot \mathrm{Net}_\phi(o_k,\,\mathrm{xyzt}_k,\,s_k)\;\in\;[0.996,\,0.998]
\]

其中 \(\theta_k\) 是 `_opacity` 裸值，\(\sigma\) 是 sigmoid，\(\phi\) 是 `Coefficient` 权重。loss 对裸值与对网络权重的梯度分别是：

\[
\frac{\partial L}{\partial \theta_k} = \frac{\partial L}{\partial \tilde{o}_k}\cdot f_k\cdot\sigma'(\theta_k),
\qquad
\frac{\partial L}{\partial \phi} = \sum_k \frac{\partial L}{\partial \tilde{o}_k}\cdot\sigma(\theta_k)\cdot(f_{\max}-f_{\min})\cdot\frac{\partial \mathrm{Net}_\phi}{\partial \phi}
\]

注意 \(f_k\approx 1\)、\(f_{\max}-f_{\min}=0.002\)：**两条通路的单步梯度都被压缩得很小**——这就是 `coefficient_lr=1e-5` 之外的第二重「慢速保险」。真正的杠杆在跨迭代累积。设高斯 \(k\) 在第 \(i\) 次被衰减（即该视角下空间∧时间可见，u6-l3）：

\[
\tilde{o}_k^{(T)} \;\approx\; \tilde{o}_k^{(0)}\prod_{i\le T,\;k\text{ 可见}} f_k^{(i)}
\;\approx\; 0.1\times f^{\,T_{\text{vis}}}
\]

以初始不透明度 0.1（[scene/gaussian_model.py:L434](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L434)）与剪枝线 0.005 计算：

\[
T_{\text{vis}} = \frac{\ln(0.05)}{\ln f} \approx
\begin{cases}
747, & f=0.996\\
997, & f=0.997\\
1496, & f=0.998
\end{cases}
\]

于是形成两个相反的螺旋：

- **沉没螺旋（几何错误的高斯）**：\(\partial L/\partial\tilde{o}_k\) 微弱或互相抵消 → \(\theta_k\) 得不到救援 → \(\tilde{o}_k\) 变小 → 它对每个像素的贡献（\(\propto\alpha\)）变小 → \(\partial L/\partial\tilde{o}_k\) 进一步变小 → ……约 \(10^3\) 次可见迭代后跌破 0.005，被 [scene/gaussian_model.py:L761](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L761) 的 `prune_mask` 清除。
- **自救螺旋（几何正确的高斯）**：\(\partial L/\partial\tilde{o}_k\) 显著为负（增大它能降 loss）→ \(\theta_k\) 持续上升对抗衰减 → \(\tilde{o}_k\) 稳定在高水平 → 梯度持续充足。

为什么这等于「聚焦几何学习」？因为**外观拟合有一个几何拟合没有的逃生通道**：一堆位置错误的高斯可以靠叠加半透明颜色凑出训练视角，但这种解里每个高斯的 \(\partial L/\partial\tilde{o}_k\) 都很小（贡献互相抵消），在衰减压力下**集体**沉没；而「每个高斯都真实贴在表面上」的几何解，每个高斯都持有显著的不透明度梯度，能整体存活。衰减把两类在 loss 上几乎等价的解，在**存活率**上区分开来——这就是「放大不透明度梯度的有效信号」的准确含义：信（几何正确的高斯的梯度）被保留，噪（凑数高斯的小梯度）随其有效不透明度同步衰减。

最后，为什么衰减必须是**学出来的**网络而不是一个固定函数（u6-l2 的 const/exp/power 家族都是内置消融项）？因为不同区域、不同运动模式的高斯需要的压力不同；网络以 \(\mathrm{xyzt}\) 位置与尺度为输入（[scene/gaussian_model.py:L605](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L605)），能在空间上自适应分配衰减强度；而它自己又接受上式的梯度——压力的分配本身也被渲染损失监督，形成闭环。`coefficient_lr=1e-5` 与 `weight_decay=1e-4`（4.1 节）保证这个闭环缓变、平滑，不会在某个局部把整片区域的高斯一次杀光。

#### 4.4.3 源码精读

初始不透明度与剪枝线这两个「螺旋边界条件」：

[scene/gaussian_model.py:L434](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L434)
初始化时全部高斯取激活后不透明度 0.1——上式复利计算的起点。

[scene/gaussian_model.py:L761](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L761)
`densify_and_prune` 的剪枝主条件 `get_opacity < min_opacity`（`min_opacity` 即 `thresh_opa_prune=0.005`）。衰减开启时 `max_screen_size` 为 `None`，这是唯一活跃的剪枝判据——沉没螺旋的终点。

衰减因子的生成与两通路的分叉点：

[scene/gaussian_model.py:L598-L620](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L598-L620)
`net` 分支（L602-L605）调用 `self.coefficient(old_opacity, self.get_xyzt, self.get_scaling_xyzt)` 得到 \(f\) 的原料，乘回 `old_opacity`；L616-L617 用 `torch.where(mask, ...)` 只对可见高斯施加；L619 `self._opacity.data = ...` 写回（持久化通路，不进梯度图）；L620 `return opacity` 把携梯度的结果交还渲染器（梯度通路）。\(\partial L/\partial\phi\) 只走 L620 这条返回链。

网络输入的标准化（为什么联合训练需要它）：

[module/__init__.py:L33-L48](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/module/__init__.py#L33-L48)
`forward` 把 opacity 重映射到 \([-1,1]\)、位置与尺度按**当前全体高斯的批统计**做 z-score、尺度先取 log。这一步对联合优化很关键：高斯的数值范围（世界坐标可达数十、log 尺度可正可负）在训练中随致密化漂移，标准化使网络输入分布始终稳定在一个小邻域内，353 参数的小网络才可能用 `1e-5` 的学习率持续跟上演化的高斯分布。

#### 4.4.4 代码实践

**实践目标**：用纯 PyTorch（CPU 即可，不依赖仓库环境）数值复现复利公式，直观看到「沉没螺旋」与「自救螺旋」的分界。

1. 操作步骤：运行以下脚本（示例代码，独立于仓库）：

```python
# 示例代码：compound_decay_sim.py —— 模拟衰减压力下的两条螺旋
import torch

f = torch.tensor(0.997)          # 窄带中值
prune_line, steps = 0.005, 3000
theta = torch.logit(torch.tensor(0.1))   # 初始不透明度 0.1 的裸值
rescue = torch.tensor(0.0)       # 每步梯度救援强度（先设 0）

for i in range(1, steps + 1):
    o = torch.sigmoid(theta)
    o_new = o * f                             # 衰减（可见即衰减的简化假设）
    theta = torch.logit(torch.clamp(o_new, 1e-8, 1 - 1e-8))
    theta = theta + rescue                    # 救援：等价于梯度把裸值推高
    if o_new < prune_line:
        print(f"无救援: 第 {i} 次可见迭代跌破 {prune_line} (o={o_new:.5f})")
        break
# 理论值: ln(0.05)/ln(0.997) ≈ 997

# 实验 2：多大的恒定救援能维持稳态？二分搜索 rescue
lo, hi = 0.0, 0.01
for _ in range(50):
    mid = (lo + hi) / 2
    theta = torch.logit(torch.tensor(0.1))
    ok = True
    for _ in range(steps):
        o = torch.sigmoid(theta)
        theta = torch.logit(torch.clamp(o * f, 1e-8, 1 - 1e-8)) + mid
        if torch.sigmoid(theta) < prune_line:
            ok = False; break
    if ok: hi = mid
    else:  lo = mid
print(f"维持存活所需的最小恒定救援 ≈ {hi:.6f} / 步")
```

2. 需要观察的现象：实验 1 的跌破迭代号应落在 997 附近（与手算 \(\ln(0.05)/\ln(0.997)\) 吻合）；实验 2 给出一个量级在 \(10^{-3}\) 的最小救援强度。
3. 预期结果：两条都成立。实验 2 的含义：每步净救援低于该阈值的高斯必然沉没——「梯度足够」不是渐变的宽恕，而是一道硬门槛，这正是筛选作用的来源。
4. 本实践只依赖 torch 与数学库，可直接运行；若与理论值偏差大于 ±3，检查 `torch.logit` 的 clamp 边界。

#### 4.4.5 小练习与答案

**练习 1**：有人认为「衰减让所有高斯都变透明，所以渲染会越来越暗」。这个说法错在哪里？

**参考答案**：错在忽略梯度竞争。渲染输出是 alpha blending 的加权和，一个高斯被衰减掉，它的贡献缺口会立刻转化为 loss 上升，驱动**幸存高斯**（\(\partial L/\partial\tilde{o}\) 显著为负的那些）提高不透明度补位；同时致密化在全程补充新点（联动 1）。稳态是「少数高不透明度、几何正确的高斯」而非「均匀变暗」。可以在 TensorBoard 的 `scene/opacity_histogram`（[train.py:L312](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L312)）中直接观察分布形态验证。

**练习 2**：为什么说「先训练高斯、再事后拟合衰减函数」得不到同样的效果？

**参考答案**：三点。(1) 事后拟合时过拟合已经发生，位置错误的高斯已经学会了能解释训练视角的颜色与透明度，其 \(\partial L/\partial\tilde{o}\) 不再显著小于正确高斯，筛选失去判据；(2) 衰减的压力必须在训练全程**在线**参与「去留裁决」，才能阻止凑数高斯在早期建立优势（`decay_from_iter=500` 只是等初始几何稍微成形，不是等训练结束）；(3) 事后函数无法享受式 \(\partial L/\partial\phi\) 的闭环监督，只能复述终态分布。

**练习 3**：`f_min/f_max` 窄带（0.996~0.998）、小学习率（1e-5）、`weight_decay`（1e-4）三者都在限制衰减网络的「权力」。它们各自限制的是哪个维度？

**参考答案**：窄带限制**单步幅度**（输出值域宽仅 0.002，无论权重如何都不可能一步杀死高斯）；小学习率限制**演化速度**（权重缓变，高斯参数来得及用梯度反驳）；`weight_decay` 限制**复杂度**（L2 把权重往 0 拉，防止 353 参数的网络过拟合不断漂移的高斯分布、输出尖锐的衰减区域）。

## 5. 综合实践

**任务：开/关 `opacity_decay` 的 A/B 短训练对比，并用 300 字解释「为什么联合优化能让梯度更聚焦几何学习」。**

### 5.1 准备两份配置

`--opacity_decay` 是 `store_true` 且默认 True（[train.py:L412](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L412)），命令行**无法关闭**它，必须走 yaml（u1-l4 的恒真守卫坑）。复制 `configs/dynerf/flame_steak.yaml` 两份：

- `ab_off.yaml`：在顶层加 `opacity_decay: False`、`reset_opacity: True`（恢复基线的周期性重置，即题目要求的「相应恢复 reset_opacity」）；把 `OptimizationParams` 段的 `iterations` 改为 `6000`；`model_path`、`source_path` 指向你的数据；`densify_until_iter` 保持 15_000 不变。注意 6000 < 15 000，两种配置的致密化窗口其实都覆盖全程——窗口差异只在长训练（>15 000 迭代）中才表现为曲线趋平，短训练的对照重点应放在下表的剪枝事件与直方图形态上。
- `ab_on.yaml`：同数据、同 `iterations`、同 `model_path`（换目录），**不加** `opacity_decay` 键（默认开启，且 `densify_until_iter` 会被自动改写为 6000，可在 `training_params.txt` 验证）。

### 5.2 运行

```bash
python train.py --config configs/dynerf/ab_off.yaml --test_iterations 3000 6000
python train.py --config configs/dynerf/ab_on.yaml  --test_iterations 3000 6000
```

（`--test_iterations` 显式传入，避免默认值 [7000, 30000] 超出短训练范围。）

### 5.3 观察与记录

打开两个输出目录的 TensorBoard（`tensorboard --logdir output/AB`），重点看两条曲线：

| 曲线 | TensorBoard 标签 | 来源 | 预期差异 |
| --- | --- | --- | --- |
| 总点数 | `total_points` | [train.py:L311](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L311) | off：较为平滑的单调增长（无透明度沉没驱动的剪枝，6000 迭代内也触不到 reset）；on：衰减累计约 1000+ 可见迭代后出现明显的「增长—剪枝」锯齿突降 |
| 不透明度分布 | `scene/opacity_histogram` | [train.py:L312](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L312) | off：分布形态基本由优化与致密化自然塑造，6000 迭代内 reset（周期 10 000）不会触发，中间不透明度区持续堆积；on：分布持续左移+被剪枝截断，高不透明度端始终保留一群幸存者 |

同时记录两侧最终 `test/loss_viewpoint - psnr`（[train.py:L361](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L361)）。

### 5.4 交付物

写 300 字左右的分析，论证骨架建议：

1. off 组的 `opacity_histogram` 里大量高斯停留在中间不透明度——它们中的一部分是「靠叠加颜色凑视角」的凑数高斯，且没有任何机制会把它们清除（6000 迭代内 reset 与尺寸剪枝都不触发）；
2. on 组的剪枝事件（`total_points` 突降）与直方图左移对应 4.4 节的沉没螺旋——无梯度救援的凑数高斯被清除；
3. 幸存高斯的高不透明度端对应自救螺旋——只有「增大不透明度能显著降 loss」即几何放对的高斯才活下来；
4. 由此渲染梯度的有效承载者从「全体高斯」收敛到「几何正确的高斯」，外观参数（颜色）不再能为错误位置兜底——这就是梯度聚焦几何学习。

### 5.5 无 GPU 的降级方案

若无法训练：(1) 用 4.4.4 的模拟脚本生成两条不透明度轨迹图作为「螺旋」证据；(2) 对一个已有训练输出目录跑 `render.py --test` 观察质量；(3) 交付物改为基于 4.4 节数学的实验设计文档（写明哪些现象**应该**出现、为何），并标注「待本地验证」。

## 6. 本讲小结

- **双优化器分家**：`Coefficient` 的 353 个权重由 `training_setup` 中独立的 `coef_optimizer`（Adam，`coefficient_lr=1e-5`、`weight_decay=1e-4`）管理，与会被致密化增删的高斯参数组彻底分离。
- **一次 backward 喂饱两个优化器**：衰减网络的梯度唯一来源是渲染损失——`opacity_decay` 的返回值进入光栅化形成可微通路，`_opacity.data` 写回只做持久化；训练循环先 step 高斯、再 step 网络，顺序无数学含义。
- **四个联动变化**：`opacity_decay` 开启后 `densify_until_iter` 拉满 `iterations`、`size_threshold` 强制 `None`、`reset_opacity` 触发支路恒假、新增 `coef_optimizer` 交替 step——前三条关闭与衰减竞争的旧判据，最后一条打开衰减自身的载体。
- **复利是杠杆**：\(0.1\times f^{T}\) 跌破 0.005 需约 750~1500 次可见迭代；单步梯度被窄带 \(f_{\max}-f_{\min}=0.002\) 压得很小，筛选作用完全来自跨迭代累积。
- **「放大有效信号」的准确含义**：衰减不放大梯度数值，而是让「凑数高斯」的小梯度随其有效不透明度同步沉没、让「几何正确高斯」的梯度保持——两类在 loss 上近似的解被区分在存活率上。
- **正则化的三重保险**：输出窄带限单步幅度、`coefficient_lr=1e-5` 限演化速度、`weight_decay=1e-4` 限网络复杂度；另注意 `--weight_decay 0` 因真值守卫无法清零正则、`--hidden_dim/--dropout_rate` 是未接线的遗留参数。

## 7. 下一步学习建议

本讲完成了单元 6（核心创新 Neural Decaying Function）。建议两条路径：

- **转向推理侧**：进入 u7-l1（`render.py` 测试视角评估），关注 `restore` 第二实参加载 `chkpnt` 时衰减网络状态如何被条件恢复（[scene/gaussian_model.py:L187-L191](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L187-L191)），以及推理渲染为何不需要衰减网络。
- **深入稀疏视角全景**：进入 u8-l3（稀疏视角策略全景与消融设计），把本讲的衰减机制与 u2-l5 的 MASt3R 稠密初始化组合成完整的消融实验矩阵——两种策略分别补偿「几何学习的梯度瓶颈」与「初始几何覆盖不足」，是 4C4D 方法论的两条腿。
- **源码延伸阅读**：若想验证本讲的机制分析，可在 [train.py:L263-L265](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L263-L265) 前后为 `_opacity.grad` 的分位数（按 `get_opacity` 高低分组）加日志，直接观测「高不透明度高斯获得更大梯度」的自救螺旋。
