# train.py 训练主循环全景

## 1. 本讲目标

学完本讲，你应该能够：

1. 按顺序说出 `training()` 中一次迭代发生的**全部步骤**：学习率调度 → 球谐进阶 → 逐视角 render → loss → backward → 多视角梯度合并 → 日志 → 致密化 → optimizer step → 保存。
2. 解释 `DataLoader` 与 `batch_size` 如何组织训练数据：外层 `while` + 内层 `for` 的双循环、`collate_fn=lambda x: x`、`drop_last=True` 各自为什么存在。
3. 准确定位训练中控制**致密化、保存、评估**的时间点（哪个迭代号触发哪个动作），并能对照 `iterations=30_000` 的官方配置画出完整时间线。

本讲只讲「流程骨架」。batch 梯度合并的数学细节在 u5-l3，致密化算法本身在 u5-l4，检查点内容布局在 u5-l5，损失函数实现也在 u5-l2 展开。

## 2. 前置知识

- **iteration 与 epoch**：本项目的训练以「迭代」计数——处理一个 batch 就是一次迭代，共 `opt.iterations`（默认 30 000）次。一个 epoch 指把训练集完整过一遍（N3V 单场景 4 相机 × 300 帧 = 1200 个样本，`batch_size=4` 时一个 epoch 是 300 个迭代）。训练不以 epoch 而以 iteration 为终止条件。
- **梯度累积**：PyTorch 中调用 `loss.backward()` 只是把梯度**累加**到 `param.grad` 上，真正更新参数的是 `optimizer.step()`。本项目的 batch 训练正是利用这一点：batch 内每个视角各 backward 一次，梯度自动累加，最后只 step 一次。
- **参数组（param group）与学习率调度**：一个 optimizer 可以装多组参数，每组独立的学习率。`GaussianModel.training_setup` 把 `_xyz`、`_opacity`、`_scaling` 等装进不同组（u3-l2），训练循环每步调用 `update_learning_rate` 只调整其中 `xyz` 组。
- **EMA（指数滑动平均）**：日志不直接打印瞬时 loss，而是维护一个平滑量：\( \bar{L}_k = 0.4\,L_k + 0.6\,\bar{L}_{k-1} \)，让进度条数字不抖动。
- **CUDA Event 计时**：GPU 计算是异步的，CPU 侧 `time.time()` 测不准。`torch.cuda.Event` 配合 `torch.cuda.synchronize()` 才能得到真实的迭代耗时。

## 3. 本讲源码地图

| 文件 | 本讲关注点 |
|---|---|
| [train.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py) | 唯一核心：`training()`（L47-281）、`prepare_output_and_logger`（L283-303）、`training_report`（L305-367）、`setup_seed`（L369-374）、`__main__`（L376-495） |
| [scene/\_\_init\_\_.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/__init__.py) | `Scene.save`（L109-112）、`getTrainCameras`（L114-115）、`getValidationCameras`（L120-124） |
| [scene/gaussian_model.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py) | 循环每步调用的方法：`update_learning_rate`（L511-521）、`densify_and_prune`（L748-768）、`add_densification_stats(_grad)`（L770-781） |
| [configs/dynerf/flame_steak.yaml](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/configs/dynerf/flame_steak.yaml) | 官方推荐配置，是本讲所有时间点参数的实际取值来源 |
| [arguments/\_\_init\_\_.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/arguments/__init__.py) | `OptimizationParams` 中各时间点参数的代码默认值（L82-105） |

## 4. 核心概念与源码讲解

### 4.1 training() 的装配阶段：迭代开始之前发生了什么

#### 4.1.1 概念说明

`training()` 的前 60 多行是「装配区」：把参数、模型、数据、日志四样东西准备好，之后 170 行才是纯循环。理解装配区的关键，是分清哪些东西**只做一次**（如建输出目录）、哪些东西**每个迭代都会被碰**（如 `gaussians.optimizer`）。

#### 4.1.2 核心流程

装配阶段的顺序（不可随意调换）：

```text
frame_ratio 缩放 time_duration
→ prepare_output_and_logger（建目录、写 cfg_args、TensorBoard）
→ 可选：Coefficient 网络（args.opacity_decay）
→ GaussianModel（占位属性）
→ Scene（读数据、算 cameras_extent、create_from_pcd 初始化高斯）
→ gaussians.training_setup(opt)（构造 optimizer，依赖 cameras_extent）
→ 可选：从 checkpoint restore
→ background 张量、EMA 变量、进度条、env_map
→ training_dataloader
```

其中「先 Scene 后 training_setup」的顺序在 u2-l4 已解释：`training_setup` 需要 `spatial_lr_scale`（来自 `cameras_extent`）来缩放位置学习率，顺序颠倒会让位置学习率静默为 0。

#### 4.1.3 源码精读

函数签名接收三组参数对象加一批脚本级开关：

[train.py:L47-L48](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L47-L48) 定义 `training()` 的形参。注意 `opacity_decay`、`training_view`、`reset_opacity` 等开关**不在**形参里，而是函数体内直接读全局 `args`（如 [L57](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L57)、[L65](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L65)、[L248](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L248)）——这是阅读时的一个陷阱：改 `args` 的值会隔着函数边界生效。

[train.py:L57-L67](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L57-L67) 依次构造 `Coefficient`（`args.opacity_decay` 默认为 True，见 [L412](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L412)）、`GaussianModel`（`sh_degree_t` 由 `pipe.eval_shfs_4d` 决定，u3-l4）、`Scene`，最后 `gaussians.training_setup(opt)`。

[train.py:L69-L72](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L69-L72) 是续训入口：`--start_checkpoint` 指向的 `.pth` 里存着 `(capture元组, iteration)`，`restore` 后 `first_iter` 从断点继续，循环便从断点处开始。

[train.py:L80-L86](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L80-L86) 收集所有 `lambda_*` 损失权重名（排除已并入主 loss 的 `lambda_dssim`），试图为每个动态创建一个 EMA 变量。**这是遗留代码**：它依赖 `vars()` 往局部名字空间动态注入名字，且下文 [L202-L204](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L202-L204) 读取的损失变量（如 `Lrigid`）在本文件中从未被计算；官方 yaml 中 `lambda_opa_mask`/`lambda_rigid`/`lambda_motion` 均为 0，`opt.__dict__[lambda_name] > 0` 的守卫使整段跳过。自定义损失时的正确接法在 u8-l4 讨论（行为待本地验证）。

[train.py:L104-L105](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L104-L105) 构造训练 DataLoader，四个关键参数在 4.2 详解。

[train.py:L107-L108](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L107-L108) 建输出目录下的 `rendered_images/`，训练中途的渲染样张会写到这里。

#### 4.1.4 代码实践

1. **目标**：确认装配顺序，理解为什么 `training_setup` 必须在 `Scene` 之后。
2. **步骤**：通读 [train.py:L57-L67](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L57-L67)，再打开 [scene/gaussian_model.py:L505-L509](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L505-L509)，找到 `spatial_lr_scale` 在 `xyz_scheduler_args` 中的两处使用。
3. **观察**：`lr_init=position_lr_init * self.spatial_lr_scale`。
4. **预期**：若 `spatial_lr_scale` 尚未被 `Scene` 赋值（保持占位值），位置学习率初值会不对。用一句话写下这个依赖关系。

#### 4.1.5 小练习与答案

**练习 1**：`training()` 的形参里没有 `opacity_decay`，为什么函数内却能读到它？
**答案**：因为它通过 `args.opacity_decay` 读取全局命令行 Namespace。`args` 在 `__main__` 中被 yaml 合并后作为全局变量存在，`training()` 直接闭包式引用。副作用是签名看不出全部依赖。

**练习 2**：`--start_checkpoint` 续训时，`first_iter` 从哪来？
**答案**：`torch.load(checkpoint)` 返回 `(model_params, first_iter)` 二元组，迭代号与权重存在同一个文件里；[L89](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L89) 的 `first_iter += 1` 使循环从断点的**下一**迭代继续。

### 4.2 DataLoader 迭代：batch 是怎么组织出来的

#### 4.2.1 概念说明

训练数据是 `scene.getTrainCameras()` 返回的 `CameraDataset`（u2-l3 讲过它的懒加载：`__getitem__` 时才 `cv2.imread`）。`DataLoader` 负责把它按 `batch_size` 切组、打乱、并用多个 worker 进程预取。本项目对 DataLoader 做了两处**非常规**定制，直接决定了主循环的写法。

#### 4.2.2 核心流程

```text
外层 while iteration < iterations + 1:
    进入 for batch_data in training_dataloader:   # 消费一个 epoch
        iteration += 1
        若 iteration > iterations: break           # 按 batch 计数终止
        ...处理一个 batch...
    # DataLoader 耗尽（一个 epoch 结束），回到 while，重新 shuffle 开启下一个 epoch
```

关键点：**终止条件按迭代（batch 数）而非 epoch 数**。一个 epoch 结束后外层 `while` 会重新让 DataLoader 生成新迭代器（重新 `shuffle=True`），因此样本顺序每个 epoch 都不同。

#### 4.2.3 源码精读

[train.py:L104-L105](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L104-L105) 构造 DataLoader：

- `collate_fn=lambda x: x`：禁用 PyTorch 默认的「堆叠成一个大张量」行为。`CameraDataset.__getitem__` 返回 `(image, camera)` 二元组，一个 batch 就是 `[(img0, cam0), (img1, cam1), ...]` 的 **list**，不做任何堆叠——因为不同图像/相机对象无法也不需要堆叠。
- `num_workers=12 if dataset.dataloader else 0`：12 个 worker 进程在后台预解码图像，是懒加载机制（u2-l3）的吞吐保障。
- `drop_last=True`：丢掉凑不满一个 batch 的尾巴。这不仅是习惯——主循环 [L133](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L133) 用 `for batch_idx in range(batch_size)` 写死循环次数，若允许出现短 batch，`batch_data[batch_idx]` 会越界；同时 `loss / batch_size` 的平均也依赖 batch 恰好是 `batch_size` 个视角。
- `shuffle=True`：每个 epoch 重新打乱（相机×帧的全组合，跨相机跨时间混合采样）。

[train.py:L110-L115](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L110-L115) 即上述双循环骨架。注意 `iteration += 1` 放在 for 循环体开头，`break` 在超过 `opt.iterations` 时触发——最后一次迭代恰好是第 30 000 个 batch。

[train.py:L100-L103](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L100-L103) 打印是否启用 DataLoader；官方 yaml 中 `dataloader: True`。

#### 4.2.4 代码实践

1. **目标**：验证 batch 的真实形态与 drop_last 的作用。
2. **步骤**：写一个 10 行脚本，构造 `torch.utils.data.DataLoader(list(range(10)), batch_size=4, shuffle=True, collate_fn=lambda x: x, drop_last=True)`，直接 `for b in dl: print(b, len(b))` 迭代到底。
3. **观察**：打印出的每个 batch 是 list 而非 tensor；统计总共有几个 batch。
4. **预期**：`[10 个样本 / batch_size 4 = 2 个完整 batch，余下 2 个被丢弃]`——只有 2 个 batch、共 8 个样本被消费。若把 `drop_last=False`，会多出一个长度为 2 的 batch。这正是主循环敢写 `range(batch_size)` 的前提。

#### 4.2.5 小练习与答案

**练习 1**：为什么不直接 `for batch_data in training_dataloader` 一层循环写到底？
**答案**：单层 for 在 DataLoader 耗尽时会结束训练，而训练要跑满固定迭代数（可能跨越多个 epoch）。外层 `while` 让迭代器耗尽后重新构造，配合 `shuffle=True` 每个 epoch 重新打乱，同时用 `iteration > opt.iterations` 精确按 batch 计数终止。

**练习 2**：`collate_fn=lambda x: x` 去掉会怎样？
**答案**：默认 collate 会尝试把 `(image_tensor, camera)` 堆叠成张量。图像可以堆，但 `Camera` 对象不能转成 tensor，会抛异常；且堆叠后的统一 dtype/形状也与逐视角 `.cuda()`、逐视角渲染的流程不匹配。

### 4.3 迭代主循环：render → loss → backward → densify → step

#### 4.3.1 概念说明

这是本讲的主体。一次迭代 = 「batch 内逐视角前向反传」+「跨视角统计合并」+「按时间点触发的致密化/日志/保存」+「一次参数更新」。理解它的最好方式是先记住五个阶段各自的**行号区间**，再逐段看。

#### 4.3.2 核心流程

一次迭代的完整时序（行号基于当前 HEAD）：

```text
L119   update_learning_rate(iteration)          # xyz 组学习率指数衰减
L122   oneupSHdegree()（每 1000 it）            # 球谐渐进进阶
L126   debug_from 触发（可选）
─────── 以下进入 batch 循环（每个视角重复一次）────────
L133-152  for batch_idx in range(batch_size):
   L134-136  取 (gt_image, viewpoint_cam) 并 .cuda()
   L138      render() 前向                      ← ① Render
   L143-145  L1 + SSIM 组合 loss                ← ② Loss
   L147      loss /= batch_size（batch 平均）
   L148      loss.backward()                    ← ③ Backward（梯度累积）
   L150-152  收集 viewspace 梯度 / radii / visibility_filter
─────── 退出 batch 循环 ────────────────────────
L154-169  每 1500 it 保存渲染样张
L171-186  多视角合并：visibility_count、梯度归一化 ×batch_size/count
L188-232  日志：进度条（每 10 it）、training_report（每 100 it）
L234-256  致密化与剪枝                         ← ④ Densify
L258-269  optimizer.step() + zero_grad         ← ⑤ Step
L271-281  best checkpoint 与常规保存
```

损失为 \( \mathcal{L} = (1-\lambda)\,\mathcal{L}_1 + \lambda\,(1-\mathrm{SSIM}) \)，\( \lambda \) 即 `lambda_dssim`（官方 0.2）。batch 内再除以 `batch_size`，使 step 一次等价于按 batch 平均更新。

#### 4.3.3 源码精读

**每步都做的事**

[train.py:L117-L123](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L117-L123)：每 100 迭代打一次 CUDA Event 用于计时；每次迭代调用 `gaussians.update_learning_rate(iteration)`；每 `sh_increase_interval`（默认 1000）迭代升一阶球谐。

[train.py:L511-L521](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L511-L521)（gaussian_model.py）是 `update_learning_rate` 的实现：遍历参数组，只对 `name == "xyz"` 的组按指数调度函数更新学习率并返回。注意被注释掉的 L518-521——4D 时间位置 `_t` 组的学习率**不做**衰减调度，这是 u3-l2 讲过的结论。

**① Render（L133-L141）**

[train.py:L133-L141](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L133-L141)：逐视角取数据、上 GPU，调用唯一的 `render()`（u4-l1 精读过：它是参数组装器，返回含 `render` 图像、`viewspace_points`、`visibility_filter`、`radii` 等键的 dict）。`viewspace_point_tensor` 是 `requires_grad=True` 的全零容器，backward 后它的 `.grad` 就是致密化要用的屏幕空间梯度信号。

**② Loss（L143-L148）**

[train.py:L143-L148](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L143-L148)：`Ll1` 用 `l1_loss`，`Lssim = 1 - fast_ssim`（CUDA 融合实现，来自 fused-ssim 子包），加权组合后 **除以 batch_size** 再 backward。除以 `batch_size` 加上梯度累积，使多个视角的梯度等效于先求平均再更新。

**③ Backward 与跨视角合并（L150-L186）**

[train.py:L150-L152](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L150-L152) 在每个视角 backward 后立即记录它的屏幕梯度范数、`radii`、`visibility_filter`，供循环外合并。

[train.py:L171-L186](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L171-L186)：`batch_size > 1` 时把 batch 内各视角的掩码与梯度合并——可见性取「至少一个视角可见」，`radii` 取最大值，viewspace 梯度先 `sum` 再对可见高斯乘 `batch_size / visibility_count`。这个系数的意义：梯度经过 backward 已对全部视角求和，除以实际可见次数才是「按可见视角平均」，与致密化阈值（按单视角梯度标定）保持同一口径。数学细节与等效性证明在 u5-l3 展开；`batch_size == 1` 时走 else 分支直接取 `_t.grad`。

**④ Densify（L234-L256）**

[train.py:L234-L238](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L234-L238)：整个致密化块包在 `iteration < opt.densify_until_iter` 里；每步先更新 `max_radii2D`（记录每个高斯在屏幕上的最大半径，供「过大高斯」剪枝用）。

[train.py:L239-L244](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L239-L244)：按 batch 大小结算统计——`batch_size == 1` 用 `add_densification_stats`（内部自己算梯度范数，见 [gaussian_model.py:L770-L774](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L770-L774)），`batch_size > 1` 用 `add_densification_stats_grad`（接收已归一化的梯度，见 [gaussian_model.py:L776-L781](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L776-L781)）。两者都往 `xyz_gradient_accum / denom` 里累加，4D 时同时累加 `t_gradient_accum`。

[train.py:L246-L252](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L246-L252)：真正的增删发生在 `iteration > densify_from_iter` 且 `iteration % densification_interval == 0`（默认每 100 迭代一次）。三处条件值得注意：
- `size_threshold`：仅在超过 `opacity_reset_interval` 且 `add_size_threshold` 时取 20，`opacity_decay` 开启时强制 `None`（不按屏幕尺寸剪枝）；
- `prune_only`：点数超过 `densify_until_num_points` 后只剪不增（官方 yaml 设 4 200 000，作为高斯数量上限）；
- 调用的 `densify_and_prune` 在 [gaussian_model.py:L748-L768](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L748-L768)，先 clone/split 再按不透明度/尺寸剪枝。

[train.py:L254-L256](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L254-L256)：不透明度重置。默认 `reset_opacity=False`（[L428](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L428)），且 `opacity_decay` 开启时被进一步排除——3DGS 经典的「周期性把不透明度打回 0.01 以清理漂浮物」策略在 4C4D 中让位于 Neural Decaying Function（u6-l4 详讲这一联动）。

**⑤ Step 与保存（L258-L281）**

[train.py:L258-L269](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L258-L269)：一次参数更新最多涉及三个 optimizer——高斯本体必更；`Coefficient` 网络有自己的 `coef_optimizer`（双优化器交替 step，u6-l4）；`env_map` 背景在 `env_optimize_until` 之前更新。每次 step 后紧跟 `zero_grad(set_to_none=True)`，为下一次梯度累积清场。注意守卫是 `iteration < opt.iterations`：最后一次迭代不再更新参数，只做收尾保存。

[train.py:L271-L281](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L271-L281)：两类保存——`iteration in testing_iterations` 且测试 PSNR 创新高时写 `chkpnt_best.pth`；`iteration in saving_iterations` 时调 `scene.save(iteration)`。后者在 [scene/\_\_init\_\_.py:L109-L112](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/__init__.py#L109-L112) 中写出一对文件：`chkpnt{N}.pth`（含优化器动量，可续训）与 `point_cloud/iteration_{N}/point_cloud.ply`（仅属性，可渲染）。

**训练时间点总表**（`✱` 表示该值被 [L473-L474](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L473-L474) 的 `opacity_decay=True` 覆盖为 `iterations=30_000`）：

| 触发迭代 | 动作 | 控制参数 | 默认 / yaml | 代码位置 |
|---|---|---|---|---|
| 每 10 | 进度条 + EMA | — | — | L193-L216 |
| 每 100 | TensorBoard 日志 | — | — | L222-L232 |
| 每 100（>500） | 致密化 clone/split/prune | `densification_interval` / `densify_from_iter` | 100 / 500 | L246-L252 |
| 每 1000 | 球谐升阶 | `sh_increase_interval` | 1000 | L122 |
| 每 1500 或 iter=2 | 保存渲染样张 | — | — | L154-L169 |
| 每 1500（exhaust_test） | 测试集评估 | `test_per_iter` | 1500 | L445-L446 → L272 |
| < 15 000（✱ 30 000） | 致密化窗口 | `densify_until_iter` | 15_000 / ✱ | L235 |
| 每 10 000（默认关闭） | 不透明度重置 | `opacity_reset_interval` / `reset_opacity` | 10000 / False | L254-L256 |
| 7 000 / 30 000 | 常规保存 | `test_iterations` / `save_iterations` | [7000, 30000] | L385-L386, L279 |

#### 4.3.4 代码实践（本讲主实践）

**目标**：在训练循环中加一个每 1000 次迭代打印高斯数量与学习率的 hook，并用注释标出五阶段的行号范围。

**操作步骤**（这是你自己的副本实验，不要改动仓库源码；以下为示例代码）：

1. 复制 `train.py` 为 `train_hook.py`（或直接在本地副本上改）。
2. 找到 [train.py:L119](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L119) 的 `gaussians.update_learning_rate(iteration)` 一行，替换为：

```python
# --- hook: 每 1000 次迭代打印高斯数量与各组学习率（示例代码）---
lr_now = gaussians.update_learning_rate(iteration)   # 返回 xyz 组当前学习率
if iteration % 1000 == 0:
    lrs = {g["name"]: g["lr"] for g in gaussians.optimizer.param_groups}
    print(f"[hook] iter={iteration:>6d} | gaussians={gaussians.get_xyz.shape[0]:>8d} "
          f"| xyz_lr={lr_now:.3e} | opacity_lr={lrs['opacity']:.3e} "
          f"| scaling_lr={lrs['scaling']:.3e}")
```

3. 在循环内加五段注释（示例代码）：

```python
# ========== ① Render: L133-L141 ==========
# ========== ② Loss:   L143-L148 ==========
# ========== ③ Backward(含跨视角合并): L150-L186 ==========
# ========== ④ Densify: L234-L256 ==========
# ========== ⑤ Step:   L258-L269 ==========
```

4. 把配置里的 `iterations` 改为 2500（同时留意 u1-l4 讲过的坑：`save_iterations` 在合并前追加，改小 `iterations` 后收尾保存点不会自动跟随，需手动指定 `--save_iterations`）。
5. 运行：`python train_hook.py --config configs/dynerf/flame_steak.yaml`。

**需要观察的现象**：每 1000 迭代打印一行；高斯数量在 `densify_from_iter=500` 之后开始增长；`xyz_lr` 随迭代指数下降，而 `t` 组学习率（可加进打印）保持不变。

**预期结果**：约在第 500~1000 迭代之间看到高斯数量从初始 `num_pts=300_000` 开始攀升，`xyz_lr` 从 1.6e-4 量级逐步向 1.6e-6 量级衰减。**若无 GPU 环境，本实践退化为纯代码走读：把 hook 代码写在注释里，并人工推演第 1、500、1000、2500 迭代各自会触发表中哪些行。**（具体数值待本地验证）

**另一个坑**：[train.py:L463-L464](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L463-L464) 在 `model_path` 已存在时直接 `raise`。重跑短训练前先删除 `output/N3V/flame_steak`，或用 `--output_dir` 指向新子目录（注意 [L457-L458](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L457-L458) 会把它拼到 `model_path` 之后）。

#### 4.3.5 小练习与答案

**练习 1**：官方 yaml 里 `densify_until_iter: 15_000`，为什么实际致密化会一直做到 30 000 迭代结束？
**答案**：`opacity_decay` 默认为 True（train.py L412），而 [L473-L474](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L473-L474) 在 `__main__` 中把 `args.densify_until_iter` 覆盖为 `args.iterations`（30 000）。yaml 写的 15_000 先经合并生效、再被这条派生逻辑改写——这是 u1-l4「派生覆盖 > yaml」优先级的典型实例。

**练习 2**：L273 使用了 `test_psnr`，这个变量在什么条件下才有值？会不会 NameError？
**答案**：`test_psnr` 只在 [L228](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L228) 的赋值语句执行后才存在，其守卫是 `iteration % 100 == 0 or iteration in testing_iterations`；而使用它的 [L272](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L272) 守卫是 `iteration in testing_iterations`，后者蕴含前者，因此不会未定义。但这也意味着：第一个测试迭代之前 `best_psnr` 一直是 0.0。

**练习 3**：进度条上显示的 PSNR 是整个 batch 的平均吗？
**答案**：不是。[L194](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L194) 用的 `image`、`gt_image` 是 batch 循环（L133-152）结束后**泄漏出来**的最后一个视角的变量（Python 的 for 循环不引入新作用域），所以进度条 PSNR 只反映 batch 内最后一个视角。真正的多相机平均 PSNR 在 `training_report` 里算。

### 4.4 training_report：日志口径与测试评估

#### 4.4.1 概念说明

`training_report` 被「每 100 迭代或测试迭代」调用一次，干两件事：把训练指标写进 TensorBoard；若是测试迭代，则在留出相机（held-out camera，u2-l2）上完整评估一遍并把测试 PSNR 返回给主循环，用于 best checkpoint 判定。它是训练过程中**唯一**系统性接触测试数据的路径。

#### 4.4.2 核心流程

```text
写 TensorBoard：l1 / ssim / total loss、iter_time、total_points、opacity 直方图
若 iteration ∈ testing_iterations:
    对 train / test 两组相机各渲染一遍（[::100] 抽稀）
    逐相机累加 L1 / PSNR / SSIM 后取平均
    test 组每 5 个存一张渲染图与 GT
    返回 test 组平均 PSNR（否则返回 0.0）
torch.cuda.empty_cache()
```

#### 4.4.3 源码精读

[train.py:L305-L313](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L305-L313) 写训练侧指标。`total_points` 曲线（高斯数量随迭代的变化）是后续调参（改 `densification_interval`、`densify_grad_threshold`）时最常看的一条；`opacity_histogram` 则是观察 Neural Decaying Function 效果的窗口。

[train.py:L316-L320](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L316-L320) 构造 train/test 两组评估配置，相机来自 `scene.getValidationCameras(tag=...)`。

[scene/\_\_init\_\_.py:L120-L124](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/__init__.py#L120-L124)：`getValidationCameras` 用 `[::num]`（默认 `num=100`）对相机列表抽稀——评估只看每 100 个样本中的 1 个，控制评估耗时。**指标口径提醒**：训练日志里的 test PSNR 是抽稀子集上的平均，与 u7-l1 中 `render.py --test` 的全量评估不完全等价，对比论文数字时要注意口径。

[train.py:L327-L337](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L327-L337)：逐相机前向（注意这里 `renderFunc` 是作为参数传进来的 `render`，`renderArgs = (pipe, background)`），累加 L1 / PSNR / SSIM。评估渲染**不带梯度**（外层已在 `torch.no_grad()` 内），且 `torch.clamp(render, 0, 1)` 后再算指标。

[train.py:L339-L353](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L339-L353)：test 组每 5 个相机落一张渲染图与 GT 到 `rendered_images/`，文件名含迭代号与相机名，方便肉眼追踪质量演化。

[train.py:L355-L367](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L355-L367)：三指标除以相机数取平均，写 TensorBoard，返回 test 组 PSNR（`.item()` 转 Python float），最后 `torch.cuda.empty_cache()` 释放评估期间的临时显存。

#### 4.4.4 代码实践

1. **目标**：把「日志触发的迭代」与「文件产出」对应起来。
2. **步骤**：在一个已训练完成的输出目录（或按 4.3.4 的短训练产物）中执行 `ls`，对照下表填写每个文件的来源；再执行 `tensorboard --logdir <model_path>` 查看 `total_points` 与 `scene/opacity_histogram`。
3. **观察**：`rendered_images/` 下文件名的前缀差异（`iter_` 来自 L154-169 训练样张，`test_iter_` 来自 L339-353 评估样张）；`chkpnt*.pth` 与 `point_cloud/iteration_*/point_cloud.ply` 成对出现。
4. **预期**：`cfg_args`、`training_params.txt`（[L476-L479](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L476-L479)）、TensorBoard 事件文件、上述成对产物。无已训练产物时，改为在代码里标注每类文件的产生行号（待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `training_report` 结尾要 `torch.cuda.empty_cache()`？
**答案**：评估会一次性对多台相机做前向渲染，中间张量（图像、光栅化缓冲）占用大量显存；评估完立即归还缓存，避免影响后续训练迭代的显存水位（与 [L13](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L13) 设置的 `expandable_segments:True` 同属显存管理手段）。

**练习 2**：`training_report` 里的 L1/PSNR 为什么不在 `iteration % 10` 的进度条里复用？
**答案**：两者口径不同。进度条是训练视角上的瞬时 EMA（仅 batch 最后一个视角），`training_report` 是测试/训练相机子集上的无梯度全量平均；前者用于人眼盯进度，后者用于模型选择（best checkpoint）。

## 5. 综合实践

**任务：绘制一张属于你自己的「训练时间线」。**

1. 以官方 `flame_steak.yaml`（`iterations=30_000`、`batch_size=4`、`opacity_decay` 默认 True）为基准，画一条 0→30 000 的迭代数轴，标出下列事件的发生区间或触发点：
   - 球谐阶数从 (0,0) 升到 (3,2) 的五个时间点（提示：每 1000 迭代一阶，u3-l4）；
   - 致密化的「统计窗口开始」「clone/split 开始」「整个致密化结束」三个点（提示：注意 `densify_until_iter` 被 L473-L474 覆盖）；
   - 测试评估与 best checkpoint 的全部触发点（提示：`exhaust_test: True` 时 `test_iterations` 被扩展为 `range(0, 30000, 1500)` 加上默认 `[7000, 30000]`，注意去重）；
   - `xyz` 学习率从初值到终值的衰减区间（提示：`position_lr_max_steps`）。
2. 有 GPU 的话，把 `iterations` 改成 2500 跑一次短训练，用 4.3.4 的 hook 把打印结果逐行誊到时间线上，与你的推演对照；每处不一致的地方回到对应行号找原因。
3. 无 GPU 的话，改为完成「行号标注版」：在 train.py 副本中给五阶段加注释块，并为时间线上每个事件标注它在 [train.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py) 中的判定行号。

这个任务把本讲三个模块（training 装配、DataLoader 组织、training_report）与所有时间点参数串成一张图，也是后续 u5-l2~u5-l5 各讲展开细节时的「地图」。

## 6. 本讲小结

- `training()` 分两段：L47-L110 装配（Coefficient → GaussianModel → Scene → training_setup → DataLoader，顺序不可换），L110-L281 是纯循环。
- 训练按 **iteration（batch 数）** 而非 epoch 计数：外层 `while` + 内层 `for` 的双循环让 DataLoader 耗尽后重新 shuffle；`drop_last=True` 与 `for batch_idx in range(batch_size)`、`loss / batch_size` 三者互相锁定。
- 一次迭代的五阶段行号：**① Render L133-L141 → ② Loss L143-L148（含 backward）→ ③ 跨视角合并 L150-L186 → ④ Densify L234-L256 → ⑤ Step L258-L269**；batch 内逐视角 backward 累积梯度，step 只做一次。
- 致密化由四个参数共同定时：`densify_from_iter`(500) / `densification_interval`(100) / `densify_until_iter`(15_000，被 `opacity_decay` 覆盖为 30 000) / `densify_until_num_points`(4.2M 上限，超过转 prune_only)。
- `training_report` 每 100 迭代写 TensorBoard、在测试迭代于抽稀的 train/test 相机子集上评估并返回 test PSNR，是 best checkpoint 的唯一判据；其口径与 `render.py --test` 的全量评估不同。
- 若干遗留/易错点：`lambda_*` EMA 注入依赖 `vars()` 且损失变量从未定义（实际不生效）；进度条 PSNR 只反映 batch 最后一个视角；`model_path` 已存在会直接 `raise`。

## 7. 下一步学习建议

- **u5-l2 损失函数与评估指标**：深入 `l1_loss`、`fast_ssim`（fused-ssim 子包）与 `psnr` 的实现，理解本讲 L143-L145 那三行的内部。
- **u5-l3 多视角 batch 训练与梯度合并**：把本讲 L171-L186 的 `×batch_size/visibility_count` 推导清楚，理解它为何对致密化阈值公平。
- **u5-l4 自适应致密化与剪枝**：进入 `densify_and_clone/split` 与 `prune_points` 内部，理解 4D 下在 xyzt 空间采样新高斯的做法。
- **u5-l5 检查点、日志与输出目录**：拆开 `capture()` 的 21 元素元组，弄清 `chkpntN.pth` 与 `point_cloud.ply` 各存什么。
- 建议同时打开 [configs/dynerf/flame_steak.yaml](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/configs/dynerf/flame_steak.yaml) 对照本讲的时间点总表，把每个数值在代码里找到出处——这是把「读过的」变成「掌握的」最快的方式。
