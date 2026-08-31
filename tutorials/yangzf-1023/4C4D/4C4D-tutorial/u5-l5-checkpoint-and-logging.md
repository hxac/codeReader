# 检查点、日志与输出目录

## 1. 本讲目标

训练一个 4C4D 场景要跑 30 000 次迭代、数小时 GPU 时间，产出的却只是一组高斯参数。如果中途断电、如果想比较两次实验、如果想把训练好的模型拿去渲染视频，都依赖训练脚本的「持久化与记录」机制。学完本讲，你应该能够：

1. 逐项说出 `chkpntN.pth` 里那个元组的 21 个元素分别是什么、形状多大；
2. 区分**常规检查点**（`chkpntN.pth` + `point_cloud.ply`）与**最优检查点**（`chkpnt_best.pth`）的触发条件与用途差异；
3. 拿到任何一个训练输出目录，能对里面每类文件说出「谁写的、何时写、拿来干什么」。

本讲是单元 5 的收尾，把前几讲的损失（u5-l2）、致密化（u5-l4）产出的状态如何「落盘」讲清楚。

## 2. 前置知识

- **检查点（checkpoint）**：把训练到某一刻的所有状态打包成一个文件。对 4C4D 来说，"所有状态"不只是高斯参数，还包括优化器的动量、球谐激活阶数等——少了任何一样，都无法从这一刻无缝续训。
- **序列化**：把内存中的 Python 对象转成磁盘字节流。PyTorch 的 `torch.save` / `torch.load` 基于 pickle，可以保存任意嵌套的元组、字典、张量。
- **`state_dict`**：PyTorch 里 `nn.Module` 自带的参数字典方法（层名 → 权重张量）。注意 `GaussianModel` **不是** `nn.Module` 的子类，它没有现成的 `state_dict`，所以作者手写了一个元组版的序列化协议（`capture` / `restore`）——这是本讲第一个核心。
- **Adam 动量**：Adam 优化器为每个参数维护一阶动量 `exp_avg` 与二阶动量 `exp_avg_sq`。它们不在模型参数里，却深刻影响后续更新方向，所以"能续训"的检查点必须把它们一并存下。
- **TensorBoard**：一个把标量、直方图写到事件文件（`events.out.tfevents.*`）的可视化工具，训练中用 `tensorboard --logdir <model_path>` 查看。
- 承接 u3-l5 的结论：`.ply` 是**有损**持久化（丢时间球谐系数），`.pth` 是**无损**的。本讲讲清 `.pth` 内部到底长什么样。

## 3. 本讲源码地图

| 文件 | 在本讲中的角色 |
|---|---|
| [scene/gaussian_model.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py) | `capture()` / `restore()` 定义检查点元组协议（本讲核心） |
| [scene/__init__.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/__init__.py) | `Scene.save()` 写出 `chkpntN.pth` 与 `point_cloud.ply`；`getValidationCameras()` 决定评估用哪些相机 |
| [train.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py) | 触发保存的两个 `if`、`training_report()` 日志函数、输出目录初始化 |
| [render.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/render.py) | 检查点的消费方：推理时 `restore(model_params, None)` |
| [module/\_\_init\_\_.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/module/__init__.py) | `Coefficient` 网络结构——元组最后一项存的就是它的 `state_dict` |
| [utils/system_utils.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/system_utils.py) | `searchForMaxIteration()`：按目录名找最大迭代号的辅助函数 |
| [configs/dynerf/flame_steak.yaml](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/configs/dynerf/flame_steak.yaml) | 官方配置：`exhaust_test: True` 决定了测试/最优检查点的评估频率 |

## 4. 核心概念与源码讲解

### 4.1 capture/restore：21 个元素的顺序敏感协议

#### 4.1.1 概念说明

`GaussianModel` 继承自 3DGS 的设计，是一个普通 Python 类而非 `nn.Module`，它的可优化参数（`_xyz`、`_opacity` 等）是手工挂上去的 `nn.Parameter`。因此它不能像普通网络那样一行 `state_dict()` 完成序列化，作者在 [scene/gaussian_model.py:98-L139](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L98-L139) 手写了 `capture()`：把所有需要落盘的状态**按固定顺序**装进一个元组；`restore()` 则按同一顺序解包回填。

这个协议解决的问题是：**训练状态 = 高斯参数 + 优化器状态 + 少量超参数开关**，三者缺一就无法精确续训。而 `.ply`（u3-l5）只存了第一类，所以只能渲染、不能续训。

`capture()` 与 `restore()` 是**顺序敏感**的：元组里没有字段名，全靠位置对齐。这意味着不同 `gaussian_dim` 的元组长度不同（3D 为 14 项、4D 为 21 项），用 3D 检查点恢复 4D 模型（或反之）会在解包时直接抛 `ValueError`。

#### 4.1.2 核心流程

```text
保存路径：
  训练循环到达 saving_iterations 中的迭代号
    → gaussians.capture()                # 现场打包 21 项状态
    → torch.save((capture元组, iteration), chkpntN.pth)   # 外面再包一层二元组

恢复路径（两种）：
  A. 续训（train.py --start_checkpoint）
     → (model_params, first_iter) = torch.load(ckpt)
     → gaussians.restore(model_params, opt)      # training_args = opt ≠ None
        解包 → training_setup 重建优化器 → load_state_dict 恢复 Adam 动量
  B. 推理（render.py）
     → (model_params, first_iter) = torch.load(ckpt)
     → gaussians.restore(model_params, None)     # training_args = None
        只解包回填参数，跳过优化器重建        # 纯前向渲染不需要动量
```

注意外层那个二元组：磁盘上的文件结构是 `(21元组, iteration)`，`iteration` 不只是标签——续训时它是 `tqdm` 进度条的起点，推理时它被拼进输出目录名 `ours_{first_iter}`。

4D 分支的 21 个元素按索引列出（以官方配置 `sh_degree=3`、`eval_shfs_4d=True`、`rot_4d=True`、`opacity_decay=True`、N 个高斯为例）：

| 索引 | 元素 | 类型 / 形状 | 含义 |
|---|---|---|---|
| 0 | `active_sh_degree` | `int` | 当前激活的空间球谐阶数 |
| 1 | `_xyz` | `(N,3)` float | 空间中心（裸值，无需激活） |
| 2 | `_features_dc` | `(N,1,3)` | 球谐 DC 系数（颜色基色） |
| 3 | `_features_rest` | `(N,47,3)` | 高阶球谐系数（48 通道减 DC） |
| 4 | `_scaling` | `(N,3)` | **log 空间**空间尺度 |
| 5 | `_rotation` | `(N,4)` | 左四元数（未归一化裸值） |
| 6 | `_opacity` | `(N,1)` | **logit** 不透明度 |
| 7 | `max_radii2D` | `(N,)` | 历史最大屏幕半径（剪枝用） |
| 8 | `xyz_gradient_accum` | `(N,1)` | 屏幕空间梯度累计（致密化证据） |
| 9 | `t_gradient_accum` | `(N,1)` | 时间维梯度累计 |
| 10 | `denom` | `(N,1)` | 梯度累计的分母（被统计次数） |
| 11 | `optimizer.state_dict()` | `dict` | 主 Adam：`param_groups`（组名、学习率）+ `state`（`exp_avg`/`exp_avg_sq`/`step`） |
| 12 | `coef_optimizer.state_dict()` | `dict` 或 `None` | Coefficient 网络的 Adam 状态；衰减关闭时为 `None` |
| 13 | `spatial_lr_scale` | `float` | 即 `cameras_extent`，位置学习率与致密化阈值的空间尺度 |
| 14 | `_t` | `(N,1)` | 时间中心 |
| 15 | `_scaling_t` | `(N,1)` | log 时间尺度 |
| 16 | `_rotation_r` | `(N,4)` | 右四元数；`rot_4d=False` 时是形状 `(0,)` 的空张量 |
| 17 | `rot_4d` | `bool` | 4D 旋转开关（协方差构造方式，见 u3-l3） |
| 18 | `env_map` | `Parameter` 或 `None` | 球面背景贴图（u4-l3）；未启用时为 `None` |
| 19 | `active_sh_degree_t` | `int` | 当前激活的时间球谐阶数 |
| 20 | `coefficient.state_dict()` | `dict` 或 `None` | **Neural Decaying Function 的网络权重** |

#### 4.1.3 源码精读

**capture 的 4D 分支**。[scene/gaussian_model.py:116-L139](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L116-L139) 按上表顺序逐项装元组。三个细节值得注意：

- [scene/gaussian_model.py:112-L114](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L112-L114)（3D 分支同样如此）：三个「可选组件」都写成 `None if xxx is None else xxx.state_dict()` 的形式——`coef_optimizer`（索引 12）与 `coefficient`（索引 20）在 `opacity_decay` 关闭时是 `None`。所以同一个 4C4D 仓库，衰减开/关产出的检查点在这两格内容不同。
- 存的是**裸值**：`_scaling` 存 log、`_opacity` 存 logit、`_rotation` 存未归一化四元数。激活（`exp`/`sigmoid`/`normalize`）发生在读取时的 property 里（u3-l1 的"裸值存储 + 读时激活"），检查点只是原样搬运裸值，因此是无损的。
- `env_map`（索引 18）初值是 `torch.empty(0)`（[scene/gaussian_model.py:90](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L90)），但训练循环里 [train.py:98](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L98) 会用 `None` 或可优化 `Parameter` 覆盖它——所以 `capture` 到的通常是二者之一，而不是空张量。

**restore 的 4D 解包与条件分支**。[scene/gaussian_model.py:157-L178](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L157-L178) 把 21 项按原名拆回局部变量并赋给 `self._xyz` 等成员；随后 [scene/gaussian_model.py:180-L191](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L180-L191) 是关键的分水岭：

- `training_args is not None`（续训）：先 `training_setup(training_args)` 用**恢复进来的参数**重建两个 Adam 优化器，再 `load_state_dict` 灌回动量与学习率（[scene/gaussian_model.py:185](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L185)、[scene/gaussian_model.py:190-L191](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L190-L191)），并把 `xyz_gradient_accum`/`t_gradient_accum`/`denom` 也还原，致密化证据得以延续。
- `training_args is None`（推理）：整个 `if` 块被跳过，`self.optimizer` 保持 `None`，只回填可渲染参数。这就是 render.py 的用法。

一个隐蔽的遗留隐患：3D 分支的解包（[scene/gaussian_model.py:142-L156](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L142-L156)）里没有 `t_gradient_accum` 这个名字，但 [scene/gaussian_model.py:183](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L183) 在 `training_args` 非 `None` 时无条件引用它——对 `gaussian_dim=3` 的模型做**续训**会触发 `NameError`。4C4D 的 train.py 默认 `gaussian_dim=4`（[train.py:390](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L390)），正常流程不会踩到，但自己改做 3D 实验时要知道这颗雷在哪。

**两个消费方对照**。续训入口在 [train.py:69-L72](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L69-L72)：`--start_checkpoint`（[train.py:388](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L388)）传入的路径被 `torch.load` 后以 `opt` 作为第二实参恢复，恢复出的 `first_iter` 随后用于 [train.py:88](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L88) 的 `tqdm(range(first_iter, opt.iterations))`——检查点里的迭代号真正决定了从哪一步继续。推理入口在 [render.py:52-L59](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/render.py#L52-L59)：同一个 `torch.load`，但 `restore(model_params, None)`，`first_iter` 只用来命名 `test/ours_{first_iter}` 与 `traj/ours_{first_iter}` 输出目录。

**元组最后一项的内容**。索引 20 存的 `coefficient.state_dict()` 对应 [module/\_\_init\_\_.py:17-L23](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/module/__init__.py#L17-L23) 定义的两层 MLP（`Linear(9,32) → ReLU → Dropout → Linear(32,1) → Sigmoid`），因此该字典恰好含 4 个张量键：`net.0.weight` `(32,9)`、`net.0.bias` `(32,)`、`net.3.weight` `(1,32)`、`net.3.bias` `(1,)`（Dropout 无参数）。网络细节留待 u6-l1 展开，本讲只需能在检查点里认出它。

#### 4.1.4 代码实践

**实践目标**：把 `chkpntN.pth` 从一个"黑盒文件"变成一张你能逐项念出来的清单，并定位 Coefficient 权重在元组中的位置。

**操作步骤**：

1. 新建脚本 `inspect_ckpt.py`（示例代码，非项目原有文件），放在仓库任意位置（不要放进源码目录）：

   ```python
   # inspect_ckpt.py —— 示例代码
   import sys
   import torch

   ckpt_path = sys.argv[1]  # 用法: python inspect_ckpt.py output/N3V/flame_steak/chkpnt30000.pth
   # map_location="cpu" 让无 GPU 的机器也能读;PyTorch >= 2.6 还需加 weights_only=False
   model_params, iteration = torch.load(ckpt_path, map_location="cpu")

   names = [
       "active_sh_degree", "_xyz", "_features_dc", "_features_rest",
       "_scaling", "_rotation", "_opacity", "max_radii2D",
       "xyz_gradient_accum", "t_gradient_accum", "denom",
       "optimizer.state_dict", "coef_optimizer.state_dict", "spatial_lr_scale",
       "_t", "_scaling_t", "_rotation_r", "rot_4d", "env_map",
       "active_sh_degree_t", "coefficient.state_dict",
   ]

   print(f"iteration = {iteration}, tuple length = {len(model_params)}")
   for i, (name, item) in enumerate(zip(names, model_params)):
       if torch.is_tensor(item):
           print(f"[{i:2d}] {name:26s} Tensor {tuple(item.shape)} {item.dtype}")
       elif isinstance(item, dict):
           print(f"[{i:2d}] {name:26s} dict, keys: {list(item.keys())}")
       else:
           print(f"[{i:2d}] {name:26s} {type(item).__name__} = {item}")
   ```

2. 对一个训练产物运行它：`python inspect_ckpt.py <model_path>/chkpnt30000.pth`。
3. 若手头没有训练产物（训练需 GPU 与数据集，见 u1-l2），用下面的零依赖版本自制一个同构文件再 inspect（示例代码）：

   ```python
   # make_fake_ckpt.py —— 示例代码：不依赖项目源码，仅复现元组结构
   import torch
   N = 1000
   tup = (
       3,                                        # active_sh_degree
       torch.zeros(N, 3), torch.zeros(N, 1, 3), torch.zeros(N, 47, 3),
       torch.zeros(N, 3), torch.zeros(N, 4), torch.zeros(N, 1), torch.zeros(N),
       torch.zeros(N, 1), torch.zeros(N, 1), torch.zeros(N, 1),
       {"state": {}, "param_groups": []},        # optimizer.state_dict()
       None,                                     # coef_optimizer
       5.0,                                      # spatial_lr_scale
       torch.zeros(N, 1), torch.zeros(N, 1), torch.zeros(N, 4),
       True, None, 2,
       {"net.0.weight": torch.zeros(32, 9), "net.0.bias": torch.zeros(32),
        "net.3.weight": torch.zeros(1, 32), "net.3.bias": torch.zeros(1)},
   )
   torch.save((tup, 30000), "fake_chkpnt30000.pth")
   ```

**需要观察的现象**：

- 第一行输出 `tuple length = 21`（4D 模型）；
- 索引 1–10、14–16 全是 `Tensor (N, ...)`，且首维 N 一致；
- 索引 11 与 20 是 `dict`：前者键为 `['state', 'param_groups']`（优化器），后者键为 `['net.0.weight', 'net.0.bias', 'net.3.weight', 'net.3.bias']`（Coefficient 网络）；
- 若训练时关了 `opacity_decay`，索引 12 与 20 会显示 `NoneType`；
- 若 `rot_4d=False`，索引 16 `_rotation_r` 形状为 `(0,)`。

**预期结果**：索引 **20**（即 `coefficient.state_dict()`，[scene/gaussian_model.py:138](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L138)）存的就是 Coefficient 网络的 `state_dict`；索引 12 存的则是**它的优化器**状态，两者不要混淆。伪造文件路径的输出与真实检查点逐行同构。真实检查点的具体数值属「待本地验证」——需要先按 u1-l2 搭好环境并完成一次训练。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `.pth` 能续训而 `.ply` 不能？
**答案**：`.ply` 只写了高斯的几何外观属性（且丢失时间球谐系数，见 u3-l5）；`.pth` 的元组还包含两个优化器的 `state_dict`（Adam 动量、学习率进度）、致密化梯度累计（索引 8–10）与各开关状态，这些是"从断点继续"的必要条件。

**练习 2**：`restore(model_params, None)` 与 `restore(model_params, opt)` 的行为差异是什么？
**答案**：第二实参即 `training_args`。为 `None` 时跳过 [scene/gaussian_model.py:180-L191](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L180-L191) 整个块：不重建优化器、不恢复动量，仅供前向渲染（render.py 的用法）；非 `None` 时重建并恢复全部训练状态，供续训（train.py 的用法）。

**练习 3**：把一个 4D 检查点喂给 `gaussian_dim=3` 构造的 `GaussianModel.restore` 会发生什么？
**答案**：3D 分支按 14 个名字解包一个 21 元组，元素数量不匹配，解包语句直接抛 `ValueError`。该协议没有任何版本/维度自描述字段，全靠调用方保证维度一致。

### 4.2 scene.save 与两类检查点的触发条件

#### 4.2.1 概念说明

训练循环里有**两套互不相同的保存逻辑**，初学者极易混淆：

- **常规检查点**：迭代号命中 `saving_iterations` 列表时触发，调用 `Scene.save(iteration)`，一次写出**一对文件**——`chkpntN.pth`（无损、可续训）与 `point_cloud/iteration_N/point_cloud.ply`（有损、可被第三方 viewer 直接打开）。文件名带迭代号，**只增不覆盖**。
- **最优检查点**：迭代号命中 `testing_iterations` 列表**且**本次测试 PSNR 不低于历史最优时触发，往**同一个** `chkpnt_best.pth` 反复覆盖，且**只写 `.pth` 不写 `.ply`**。它是"按泛化质量择优"的产物，用于挑选最终交付的模型。

`saving_iterations` 与 `testing_iterations` 是两个独立列表：前者管存档，后者管评估（以及最优检查点）。

#### 4.2.2 核心流程

```text
saving_iterations 的构成（train.py:386, 432）：
  命令行默认 [7000, 30000]  →  append(args.iterations)
  （append 发生在 yaml 合并之前，见下方陷阱）

testing_iterations 的构成（train.py:385, 445-446）：
  命令行默认 [7000, 30000]
  若 exhaust_test: 追加 range(0, iterations, test_per_iter)   # test_per_iter 默认 1500

一次训练（官方 flame_steak 配置, iterations=30000, exhaust_test=True）：
  iteration ∈ {0,1500,3000,...,28500,7000,30000}   → 评估,可能刷新 chkpnt_best.pth
  iteration ∈ {7000, 30000}                        → scene.save → chkpnt7000/30000.pth + ply
```

择优逻辑是一个简单的在线最大值跟踪：

\[
\text{best}_k = \max_{i \in T,\ i \le k} \mathrm{PSNR}_{\text{test}}(i)
\]

其中 \( T \) 是 `testing_iterations` 集合。每次评估后若 \(\mathrm{PSNR}_{\text{test}}(k) \ge \text{best}_k\)（用 `>=`，并列也覆盖）就重写 `chkpnt_best.pth`。由于 `best_psnr` 初值为 0.0（[train.py:80](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L80)），第一次评估必然落盘一次。

#### 4.2.3 源码精读

**常规保存的触发与实现**。[train.py:278-L281](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L278-L281) 是循环里的保存钩子；[scene/\_\_init\_\_.py:109-L112](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/__init__.py#L109-L112) 是实现本体：

- 第一行 `torch.save((self.gaussians.capture(), iteration), ...)` 把 4.1 节的 21 元组再包一层迭代号存成 `chkpntN.pth`；
- 后两行拼出 `point_cloud/iteration_N/` 目录并调 `save_ply`（目录创建在 [scene/gaussian_model.py:350](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L350) 内部完成）。这个目录命名约定不是随意的：`Scene` 以 `load_iteration=-1` 构造时，[scene/\_\_init\_\_.py:39-L44](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/__init__.py#L39-L44) 会调 [utils/system_utils.py:27-L29](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/system_utils.py#L27-L29) 的 `searchForMaxIteration`，在该目录下按 `iteration_N` 后缀找最大迭代号自动加载——这是"从 ply 恢复"路径的寻址机制（注意它搜索的是 `point_cloud/` 子目录，`chkpnt*.pth` 躺在 `model_path` 根下，不参与该搜索）。

**最优保存的触发**。[train.py:271-L276](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L271-L276)：外层 `if iteration in testing_iterations` 进入评估迭代，内层 `if test_psnr >= best_psnr` 判优后 `torch.save` 到固定名 `chkpnt_best.pth`。这里用的 `test_psnr` 来自 [train.py:228-L232](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L228-L232) 对 `training_report` 的调用——该调用的守卫条件是 `iteration % 100 == 0 or iteration in testing_iterations`（[train.py:222](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L222)），因此凡是进入最优判定分支的迭代，`test_psnr` 必已赋值，不会读到未定义变量。

**两个列表的装配**。默认值在 [train.py:385-L386](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L385-L386)（都是 `[7000, 30000]`）；[train.py:432](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L432) 把 `args.iterations` 追加进 `save_iterations`，保证训练终点必有存档；[train.py:445-L446](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L445-L446) 在 `exhaust_test` 开启时按 `test_per_iter`（默认 1500，[train.py:425](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L425)）把测试点铺满全程。官方配置 [configs/dynerf/flame_steak.yaml:8](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/configs/dynerf/flame_steak.yaml#L8) 设了 `exhaust_test: True`，所以官方训练每 1500 步评估一次、择优刷新 best。

**两个陷阱**（承接 u1-l4 建立的参数优先级认知）：

1. `save_iterations.append(args.iterations)` 发生在 yaml 合并**之前**（[train.py:432](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L432) 在 [train.py:434](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L434) 之前），append 的是命令行/代码默认的 `iterations`（`OptimizationParams` 默认 30 000，[arguments/\_\_init\_\_.py:82](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/arguments/__init__.py#L82)）。若你在 yaml 里把 `iterations` 改成别的值，终点保存点不会跟着变，最后一个存档可能落在错误的迭代上。
2. [train.py:463-L465](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L463-L465) 要求 `model_path` 在启动时**不存在**，否则直接 `AssertionError`。这是防覆盖的保护，但也意味着"续训"必须换一个输出目录再配合 `--start_checkpoint` 使用，不能原地接着跑。

#### 4.2.4 代码实践

**实践目标**：不运行训练，仅凭源码推导一次官方配置训练结束后 `model_path` 根下会出现哪些 `chkpnt*` 文件与 `point_cloud/` 子目录——训练前就知道自己会得到什么。

**操作步骤**：

1. 读 [train.py:385-L386](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L385-L386) 抄下两个默认列表；读 [configs/dynerf/flame_steak.yaml:8](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/configs/dynerf/flame_steak.yaml#L8) 与 [configs/dynerf/flame_steak.yaml:35](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/configs/dynerf/flame_steak.yaml#L35) 确认 `exhaust_test` 与 `iterations`。
2. 按 4.2.2 的流程分别展开 `saving_iterations` 与 `testing_iterations` 两个集合。
3. 在纸上写下：哪些迭代会调 `scene.save`（产出 `chkpntN.pth` + `iteration_N/point_cloud.ply`）、哪些迭代会参与 best 判优。
4. 有环境的话跑一次短训练验证：把 yaml 复制一份，改 `iterations: 4500`、`exhaust_test: True`、`test_per_iter: 1500`（记得同时用 `--output_dir` 指向新目录绕开陷阱 1 的错位与陷阱 2 的目录冲突），训练结束后 `ls` 输出目录对答案。

**需要观察的现象 / 预期结果**（源码可完全推导，运行属「待本地验证」）：

- 官方配置（30 000 步）：`chkpnt7000.pth`、`chkpnt30000.pth` 两个常规检查点，`point_cloud/iteration_7000/`、`point_cloud/iteration_30000/` 两个 ply 目录，外加一个 `chkpnt_best.pth`；
- `chkpnt_best.pth` 的时间戳应晚于（或等于）最后一次刷新 PSNR 新高的评估迭代——可以用 `ls -l` 看修改时间，再用 4.1.4 的脚本读出它内含的 `iteration` 值，两者应能对上；
- 改成 4500 步的短训练里，`save_iterations` 实际是 `[7000, 30000, 30000]`（append 的默认值）而 `iterations=4500`，循环根本到不了 7000——于是**一个常规检查点都不会存**，只有 `chkpnt_best.pth` 落盘。这正好实证了陷阱 1。

#### 4.2.5 小练习与答案

**练习 1**：`chkpnt_best.pth` 为什么不配套写一个 `point_cloud/iteration_best/point_cloud.ply`？
**答案**：最优检查点的写入点在 [train.py:276](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L276)，直接 `torch.save`，没有走 `Scene.save`；且 best 的迭代号不固定（取决于哪次评估创了新高），而 ply 的目录名约定 `iteration_N` 面向的是固定存档点。需要 ply 时应对 best 恢复后手动调 `save_ply`，或直接用同迭代的常规检查点。

**练习 2**：`exhaust_test` 关闭时，官方 30 000 步训练会评估几次、best 最多刷新几次？
**答案**：只在 `testing_iterations = [7000, 30000]` 两个点评估（[train.py:385](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L385)），best 至多刷新 2 次；第一次（7000）因 `best_psnr` 初值 0 必然保存一次。

**练习 3**：如何让训练**只**在终点存一个常规检查点？
**答案**：命令行传 `--save_iterations 30000` 会替换默认列表，且 [train.py:432](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L432) 还会 append 一次 `iterations`（同为 30000 时结果就是单元素列表的去重效果——文件名相同，后者覆盖前者，最终只有一个 `chkpnt30000.pth`）。注意该参数只能经命令行传入，`save_iterations` 不在 yaml 白名单键中（可用 `python train.py --help` 核对，承接 u1-l4）。

### 4.3 training_report 与输出目录全景

#### 4.3.1 概念说明

训练过程的"日志"在 4C4D 里其实有**五**种载体，各司其职：

1. **进度条**（`tqdm`）：终端里的实时 EMA 损失与 PSNR，训练结束即消失（u5-l2 已讲口径）；
2. **TensorBoard 事件文件**：`events.out.tfevents.*`，唯一的时间序列记录；
3. **中间渲染图**：`rendered_images/` 下的 PNG，直接用眼睛看重建质量随训练的演变；
4. **`cfg_args`**：`prepare_output_and_logger` 写出的参数快照（Namespace 的字符串化）；
5. **`training_params.txt`**：`__main__` 末尾写出的**合并 yaml 之后**的最终参数快照。

后两者是一对：排错时先读 `training_params.txt`（它反映实际生效值），这与 u1-l4 讲的参数优先级问题直接挂钩。

#### 4.3.2 核心流程

```text
每 100 迭代 或 测试迭代（train.py:222）：
  training_report(tb_writer, iteration, ...)
    ├─ tb_writer.add_scalar: l1 / ssim / total_loss / iter_time / total_points
    ├─ tb_writer.add_histogram: scene/opacity_histogram
    └─ 若 iteration ∈ testing_iterations：
         对 train、test 两组相机（各自 [::100] 抽稀）逐个渲染
           ├─ 累计 l1 / psnr / ssim（fast_ssim）
           ├─ test 组每 5 个相机存一张 test_iter_N_cam_XX.png
           └─ 返回 test 组的平均 PSNR（否则返回 0.0）
```

#### 4.3.3 源码精读

**TensorBoard 写入段**。[train.py:306-L312](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L306-L312) 每次调用写 5 条标量加 1 条直方图。最常看的两条：`total_points`（当前高斯总数，观察 u5-l4 致密化曲线的核心指标）与 `scene/opacity_histogram`（不透明度分布，观察衰减与剪枝如何塑造它）。写入器来自 [train.py:298-L300](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L298-L300)：`SummaryWriter(args.model_path)` 直接把事件文件落在输出目录根下；未装 tensorboard 则优雅降级为不记录（[train.py:301-L302](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L301-L302)）。

**评估段**。[train.py:316-L320](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L316-L320) 构造 `train`/`test` 两组相机，来源是 [scene/\_\_init\_\_.py:120-L124](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/__init__.py#L120-L124) 的 `getValidationCameras`——注意默认 `num=100` 即 `[::100]` 切片抽稀，train、test 两组都会抽。以 N3V 为例：68 台相机留 4 台训练（u2-l2 的 `training_view`），测试相机约 64 台 × 300 帧 ≈ 19 200 帧，抽稀后约 192 帧参与评估——这是把评估成本压到可控的手段，但也意味着日志里的 test PSNR 是**抽稀子集**上的均值。逐相机循环体（[train.py:327-L337](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L327-L337)）用 `renderFunc`（即 u4-l1 的 `render`）渲染后累计三项指标；`render` 的 `args`/`iteration` 形参有默认值 `None`/`-1`（[gaussian_renderer/\_\_init\_\_.py:19](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L19)），所以这里的四位置实参调用合法，且评估渲染不会触发衰减分支——衰减早已写入 `_opacity`（u6-l3 会展开这个区别）。指标按相机数取平均后打印（[train.py:358](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L358)）并写入 TensorBoard（[train.py:359-L362](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L359-L362)）；`test` 组的均值作为返回值（[train.py:363-L364](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L363-L364)），正是 4.2 节 best 判优吃到的那个数——**`training_report` 的返回值是 best checkpoint 的唯一判据**。非测试迭代返回 0.0（[train.py:314](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L314)）。收尾的 `torch.cuda.empty_cache()`（[train.py:366](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L366)）在评估批量渲染后回收显存。

**中间渲染图**。训练侧的保存点在 [train.py:154-L169](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L154-L169)：`iteration % 1500 == 0 or iteration == 2` 时把 batch 末视角的渲染图与 GT 图各写一张 PNG 到 `rendered_images/`（目录由 [train.py:107-L108](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L107-L108) 创建）。两点提醒：注释写的 "Save every 100 iterations" 与实际条件（1500）不符，以代码为准；保存的 `image`/`gt_image` 是 batch 循环里最后一个视角的变量，与 u5-l2 讲过的"进度条 PSNR 取 batch 末视角"同一口径。测试侧的图在 [train.py:339-L353](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L339-L353)，`idx % 5 == 0` 每 5 个测试相机存一张，文件名前缀 `test_iter_`，与训练图靠前缀区分。

**两个参数快照**。[train.py:294-L295](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L294-L295) 在 `prepare_output_and_logger` 里写 `cfg_args`；[train.py:476-L479](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L476-L479) 在 `__main__` 里、`training()` 调用之前写 `training_params.txt`。两者内容都是 `str(args)`，但**写出时机不同**：`training_params.txt` 写于 yaml 合并（[train.py:434-L443](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L434-L443)）与全部派生改写（[train.py:445-L461](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L445-L461)）之后，所以它是"最终生效参数"的权威记录；`cfg_args` 写得稍早（此时 `training_view` 还是字符串、尚未拆成 `camXX` 列表，对比 [train.py:467-L471](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L467-L471)），适合还原"当时传了什么"。

**输出目录全景**。把本讲与前几讲的所有落盘点拼成一张表（`<model_path>` 即 yaml 里的 `model_path`，如 `output/N3V/flame_steak`）：

| 路径 | 写出代码 | 时机 | 用途 |
|---|---|---|---|
| `cfg_args` | [train.py:294-L295](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L294-L295) | `training()` 开头 | 原始参数快照（合并后、少量派生前） |
| `training_params.txt` | [train.py:476-L479](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L476-L479) | 进入 `training()` 前 | **最终生效参数**，排错第一入口 |
| `input.ply` / `cameras.json` | [scene/\_\_init\_\_.py:62-L76](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/__init__.py#L62-L76) | `Scene` 构造 | 初始点云存档 / 相机参数 JSON（u2-l4） |
| `chkpnt7000.pth` 等常规检查点 | [scene/\_\_init\_\_.py:110](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/__init__.py#L110) | `iteration ∈ saving_iterations` | 无损存档，可续训可推理 |
| `point_cloud/iteration_N/point_cloud.ply` | [scene/\_\_init\_\_.py:111-L112](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/__init__.py#L111-L112) | 同上 | 有损几何外观，供 viewer 与 `load_ply` 路径 |
| `chkpnt_best.pth` | [train.py:276](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L276) | 测试迭代且 PSNR 创新高 | 择优交付模型（仅 `.pth`） |
| `rendered_images/iter_*.png` 与 `*_gt.png` | [train.py:154-L169](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L154-L169) | 每 1500 步及第 2 步 | 训练演变目视检查 |
| `rendered_images/test_iter_*.png` | [train.py:339-L353](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L339-L353) | 测试迭代，每 5 个测试相机一张 | 测试视角目视检查 |
| `events.out.tfevents.*` | [train.py:300](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L300) 起 | 每 100 步 / 测试迭代 | TensorBoard 曲线与直方图 |
| `test/ours_N/`、`traj/ours_N/` | [render.py:54-L57](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/render.py#L54-L57) | 推理阶段 | 评估图像与轨迹视频（u7 单元） |

#### 4.3.4 代码实践

**实践目标**：核对一张真实（或短训练）输出目录与本讲表格的对应关系，并学会从 TensorBoard 提取关键曲线。

**操作步骤**：

1. 对任一训练输出目录执行 `find <model_path> -maxdepth 3 | sort`，把结果与 4.3.3 的表格逐行对照。
2. 打开 `training_params.txt`，找到 `iterations`、`test_iterations`、`save_iterations`、`opacity_decay` 四个键，抄下它们的值。
3. 若装好 TensorBoard：`tensorboard --logdir <model_path>`，查看 `total_points`、`scene/opacity_histogram`、`test/loss_viewpoint - psnr` 三条曲线。
4. 无 GUI 时改用命令行抽取（示例代码）：

   ```python
   # read_tb.py —— 示例代码：从事件文件读 total_points 曲线
   from tensorboard.backend.event_processing.event_accumulator import EventAccumulator
   import glob

   ea = EventAccumulator(sorted(glob.glob("<model_path>/events.out.tfevents.*"))[-1])
   ea.Reload()
   for e in ea.Scalars("total_points"):
       print(e.step, e.value)
   ```

**需要观察的现象**：

- `total_points` 曲线呈"爬升—平台"形状：`opacity_decay` 开启时致密化窗口被拉满到 `iterations`（u6-l4），点数一路增长直到逼近 `densify_until_num_points`（flame_steak 配置为 4 200 000，[configs/dynerf/flame_steak.yaml:56](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/configs/dynerf/flame_steak.yaml#L56)）；
- `test/loss_viewpoint - psnr` 只在测试迭代处有散点（官方配置下每 1500 步一个），其峰值时刻应与 `chkpnt_best.pth` 的修改时间吻合；
- `rendered_images/` 里 `iter_2_*.png` 几乎是噪声，`iter_30000_*.png` 应接近 GT。

**预期结果**：目录清单与表格逐项对上；`training_params.txt` 里的 `test_iterations` 应是默认 `[7000, 30000]` 加上 `range(0, 30000, 1500)` 的展平结果（含一个永不出现的 0）。曲线具体数值属「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：日志里 test PSNR 是在全量测试相机上算的吗？
**答案**：不是。[scene/\_\_init\_\_.py:120-L124](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/__init__.py#L120-L124) 默认 `num=100` 做 `[::100]` 抽稀，train、test 两组都抽；test PSNR 是抽稀子集的均值。要全量评估需改用 u7-l1 的 `render.py --validate` 路径（它走 `getTestCameras`，不抽稀）。

**练习 2**：为什么说 `training_report` 的返回值是 best checkpoint 的"唯一判据"？
**答案**：[train.py:272-L276](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L272-L276) 的择优只比较 `test_psnr`，而它唯一来源是 [train.py:228](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L228) 接住的 `training_report` 返回值（test 组平均 PSNR，[train.py:363-L364](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L363-L364)）；进度条上的 PSNR（训练视角、batch 末视角、未 clamp）与它口径不同，不参与判优。

**练习 3**：`cfg_args` 与 `training_params.txt` 内容几乎相同，为什么还要两个文件？
**答案**：写出时机不同导致内容有差：`cfg_args` 写于 `training()` 入口（此时 `training_view` 仍是逗号字符串）；`training_params.txt` 写于 yaml 合并与全部派生改写之后（[train.py:476-L479](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L476-L479) 在 [train.py:445-L471](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L445-L471) 之后），反映 `num_pts`/`resolution` 等被覆盖后的**实际生效**值。复现实验以 `training_params.txt` 为准。

## 5. 综合实践：「检查点考古」

给你一个别人训练留下的输出目录（或你自己短训练的产物），只靠本讲知识完成一次完整盘点：

1. **目录树**：`find <model_path> -maxdepth 3 | sort > tree.txt`，对照 4.3.3 的表格给每个文件标注「写出代码行号 + 触发时机」；
2. **参数复原**：读 `training_params.txt`，回答：`iterations` 是多少？`opacity_decay` 开没开？`training_view` 最终是哪几台相机（应是 `camXX` 列表而非字符串）？
3. **检查点解剖**：运行 4.1.4 的 `inspect_ckpt.py` 分别处理 `chkpnt_best.pth` 与最大的 `chkpntN.pth`，记录：元组长度、N（高斯数）、两个 dict 各自的键、`rot_4d` 的值；
4. **定位衰减网络**：指出 Coefficient 的 `state_dict` 在元组中的索引（20），并用 `model_params[20]['net.0.weight'].shape` 验证应为 `(32, 9)`——9 维输入 = 不透明度 1 + xyzt 位置 4 + xyzt 尺度 4（u6-l1 将展开）；
5. **交叉验证**：从 `chkpnt_best.pth` 读出内含的 `iteration`，与 TensorBoard 的 `test/loss_viewpoint - psnr` 散点峰值位置、`chkpnt_best.pth` 的文件修改时间三方对照，确认 best 判优逻辑真的按源码运行；
6. **续训演练**（可选，需 GPU）：用 `--output_dir resume_test --start_checkpoint <model_path>/chkpnt7000.pth` 换新目录续训几百步，观察终端打印的 `Restored gaussians from checkpoint`（[train.py:72](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L72)）与进度条起点是否从 7001 开始。

产出一份不超过一页的「考古报告」。第 1–5 步纯只读、无 GPU 也能完成；第 6 步「待本地验证」。

## 6. 本讲小结

- 4D 检查点是「`(21 元组, iteration)`」的二元组：10 项高斯裸值参数 + 3 项致密化统计 + 2 个优化器 `state_dict` + 6 项开关/标量/背景，顺序敏感、无自描述字段，3D/4D 互不兼容。
- 元组**最后一项（索引 20）**是 `Coefficient` 网络的 `state_dict`（含 `net.0/net.3` 共 4 个张量），索引 12 才是它的优化器状态；`opacity_decay` 关闭时两者皆为 `None`。
- `restore` 的第二实参是分水岭：`None` 走推理（只回填参数），非 `None` 走续训（重建优化器并恢复 Adam 动量与学习率进度）；3D 分支有一处 `t_gradient_accum` 未解包的续训 `NameError` 隐患。
- 常规检查点在 `iteration ∈ saving_iterations` 时由 `Scene.save` 成对写出 `.pth` + `.ply`；`chkpnt_best.pth` 在测试迭代且 test PSNR 不低于历史最优时被反复覆盖，判据唯一来自 `training_report` 的返回值。
- `training_report` 双职责：每 100 步写 TensorBoard（`total_points`、`opacity_histogram` 等），测试迭代在 `[::100]` 抽稀的 train/test 相机子集上评估并可选落图。
- 输出目录的排错入口是 `training_params.txt`（合并后的最终参数）；`model_path` 已存在会直接报错，续训须换目录配 `--start_checkpoint`。

## 7. 下一步学习建议

- **u7-l1（render.py：测试视角评估）**：看检查点的另一半故事——`restore(model_params, None)` 之后如何在**全量**测试相机上评估并导出图像，与本讲抽稀口径的评估形成对照。
- **u6-l1（Coefficient 网络）**：本讲只让你在检查点里认出 `coefficient.state_dict`；下一单元将逐层拆开这个 9→32→1 的 MLP，理解索引 20 那 4 个张量如何决定每个高斯的衰减因子。
- **源码延伸阅读**：拿 4.1.4 的脚本对比同一训练的 `chkpnt7000.pth` 与 `chkpnt30000.pth`——观察 N 的增长（致密化）、`_opacity` 数值范围的变化（衰减写入）与 `optimizer.state_dict` 中 `step` 计数的差异，把 u5-l4 与本讲的知识在真实数据上串起来。
