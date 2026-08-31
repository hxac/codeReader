# render.py：测试视角评估

## 1. 本讲目标

训练结束后，我们最关心的问题是：**这个 4D 高斯模型在没见过的视角上到底渲染得有多好？** 本讲沿着仓库的第二个入口 `render.py`，完整走一遍「从 checkpoint 恢复模型 → 在留出（held-out）测试相机上渲染 → 计算指标」的评估链路。学完本讲你应该能够：

1. 独立执行一次完整的测试视角评估，知道每个命令行开关的真实含义（包括 README 与源码不一致的一处坑）。
2. 说清 `gaussians.restore(model_params, None)` 与训练续训时 `restore(model_params, opt)` 的本质差异：为什么评估只需要恢复参数、不需要重建优化器。
3. 区分仓库里**三套指标口径**（训练进度条 PSNR、`training_report` 的测试 PSNR、`render.py --validate` 的 stats 报表），并能用自己写的脚本复算并与训练日志对照。

本讲承接 u5-l5（checkpoint 的 capture/restore 元组协议）与 u5-l2（损失与指标），不再重复其细节，而是在「评估」这个新场景下复用它们。

## 2. 前置知识

### 2.1 留出相机评估（held-out camera）

4C4D 的输入是十几到二十台相机的视频（N3V 数据集），训练只用其中 4 台（`--training_view 1,10,13,20`）。评估时在**其余相机**的全部帧上渲染，与真值图（ground truth，下称 GT）逐像素比较。这考验的是模型对「没见过的视角」的泛化能力，而不是记忆能力。回顾 u2-l2：train/test 的划分是**按相机、不按帧**的，划分发生在 `readColmapSceneInfo` 里。

### 2.2 恢复模型的两副面孔

u5-l5 讲过，训练产出的 `chkpntN.pth` 是一个二元组 `(capture() 的返回值, iteration)`。4D 模型的 `capture()` 返回一个 **21 元素、顺序敏感**的元组。`restore` 的第二个参数是分水岭：

- 传优化器参数 `opt` → 续训模式：重建优化器、恢复 Adam 动量与致密化统计；
- 传 `None` → 推理模式：只把参数张量回填进模型，优化器相关的四项直接被丢弃。

评估走的是第二条路——渲染一张图只需要高斯的九组属性，不需要任何梯度状态。

### 2.3 衰减已经「烤进」了不透明度

回顾 u6-l2/l3 的结论：`opacity_decay` 每次调用都会把衰减因子通过 `_opacity.data` **原位写回**裸值参数。这意味着衰减的全部历史效果都累积保存在 `_opacity` 里，checkpoint 保存的就是这份累积结果。因此评估时**不需要**重建 Coefficient 衰减网络，也不需要再执行衰减——直接读 `_opacity` 即可。本讲会在源码里验证这一点。

### 2.4 CameraDataset 的迭代产物

回顾 u2-l3：`scene.getTestCameras()` 返回一个 `CameraDataset`，迭代它的每个元素是 `(gt_image, viewpoint_camera)` 二元组（懒加载：只有迭代到才真正读图）。本讲的两个评估路径都消费这个接口。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [render.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/render.py) | 推理/评估入口：`validation()` 装配一切 |
| [train.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py) | 对照组：训练期的 `restore(model_params, opt)` 与 `training_report` 指标口径 |
| [scene/gaussian_model.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py) | `capture`/`restore` 的元组协议 |
| [scene/__init__.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/__init__.py) | `getTestCameras` / `getValidationCameras` 两个相机获取接口 |
| [utils/mesh_utils.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/mesh_utils.py) | `GaussianExtractor.reconstruction_and_export`：渲染 + 导出 + 指标 + stats json |
| [utils/image_utils.py](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/image_utils.py) | `psnr` 的定义（峰值硬编码 1.0） |
| [README.md](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/README.md) | 官方评估命令（含一处与源码不一致的参数名） |

## 4. 核心概念与源码讲解

### 4.1 render.py 与 validation()：评估入口全景

#### 4.1.1 概念说明

`render.py` 是仓库两大入口之一（另一个是 `train.py`，见 u1-l3）。它是一个**纯前向**的入口：不含优化器、不含损失反传，核心函数 `validation()` 负责把「模型恢复」和「两种输出模式」组织起来：

- `--validate`：在测试相机上渲染并计算指标（本讲主题）；
- `--traj`：在插值出的新视角轨迹上渲染视频（u7-l2、u7-l3 主题）。

两者可以同时开启，至少要开一个，否则 main 里的断言会直接终止程序。

#### 4.1.2 核心流程

`render.py` 从命令行到产出图像的完整链路：

```text
__main__
 ├─ 注册三组参数 + 脚本级参数（--config/--traj/--validate/--start_checkpoint ...）
 ├─ training_view/testing_view: "1,10,13,20" → ['cam01','cam10','cam13','cam20']
 ├─ OmegaConf.load(--config) + recursive_merge   ← yaml 无条件覆盖命令行默认值（承接 u1-l4）
 ├─ --res 覆盖 resolution；output_dir → model_path = model_path/<output_dir>/render
 ├─ setup_seed + safe_state
 ├─ assert args.traj or args.validate
 └─ validation(lp.extract(args), name, pp.extract(args), args.start_checkpoint, ...)
      ├─ bg_color ← white_background
      ├─ GaussianModel(...)                      ← 注意：不传 coefficient
      ├─ assert checkpoint 非空
      ├─ Scene(dataset, gaussians, shuffle=False, ...)   ← 会先 create_from_pcd 建一次初始高斯
      ├─ (model_params, first_iter) = torch.load(checkpoint)
      ├─ gaussians.restore(model_params, None)   ← 推理态恢复（4.2 详述）
      ├─ GaussianExtractor(gaussians, render, pipe, bg_color)
      ├─ traj 分支：generate_path → 渲染 480 帧轨迹 → 合成 mp4（本讲跳过）
      └─ validate 分支：reconstruction_and_export(scene.getTestCameras(), test_dir, test_dir)
```

注意 `Scene` 的构造发生在 `restore` **之前**：`Scene.__init__` 里 `loaded_iter` 与 `loaded_pth` 均为空时会走 `create_from_pcd`，用输入点云搭一次完整的初始高斯（这需要点云文件和 CUDA 上的 `distCUDA2`），随后 `restore` 把全部参数整体覆盖。也就是说评估也要求数据集目录完整可用——不能只拷一个 checkpoint 就跑。

#### 4.1.3 源码精读

`validation()` 的函数签名与模型、场景的装配（[render.py:L38-L61](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/render.py#L38-L61)）：

```python
def validation(dataset, name, pipe, checkpoint, gaussian_dim, time_duration, rot_4d, force_sh_3d,
               num_pts, num_pts_ratio, traj='ellipse', scale_factor=1.0, training_view=None, validate=False, testing_view=None,
               total_frames=300, fix_time=False, selected_frame=-1):
    bg_color = [1, 1, 1] if dataset.white_background else [0, 0, 0]
    gaussians = GaussianModel(dataset.sh_degree, gaussian_dim=gaussian_dim, time_duration=time_duration, 
                              rot_4d=rot_4d, force_sh_3d=force_sh_3d, sh_degree_t=2 if pipe.eval_shfs_4d else 0)
    assert checkpoint, "No checkpoint provided for validation"
    scene = Scene(dataset, gaussians, shuffle=False, num_pts=num_pts, num_pts_ratio=num_pts_ratio, time_duration=time_duration, 
                  training_view=training_view, testing_view=testing_view)
    ...
    (model_params, first_iter) = torch.load(checkpoint)
    ...
    gaussians.restore(model_params, None)
    gaussExtractor = GaussianExtractor(gaussians, render, pipe, bg_color=bg_color)
```

几个关键点：

- 这里的 `GaussianModel` 构造**没有传 `coefficient`**（对比 [train.py:L57-L63](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L57-L63) 训练时会把 `Coefficient().cuda()` 传进去），所以评估态下 `gaussians.coefficient is None`。为什么可以这样，见 4.2。
- `Scene(..., shuffle=False, ...)`：训练时 Scene 默认 `shuffle=True` 打乱训练相机顺序（多分辨率一致洗牌），评估不需要洗牌，且洗牌只影响 train 相机、不影响 test 相机列表。
- `first_iter` 来自 checkpoint 里保存的迭代号，用来命名输出子目录 `ours_{first_iter}`。

输出目录的决定逻辑（[render.py:L53-L58](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/render.py#L53-L58)）：

```python
if validate:
    test_dir = os.path.join(dataset.model_path, 'test', "ours_{}".format(first_iter))
    os.makedirs(test_dir, exist_ok=True)
if traj:
    traj_dir = os.path.join(dataset.model_path, 'traj', "ours_{}".format(first_iter))
```

评估分支的全部工作只有一行（[render.py:L77-L80](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/render.py#L77-L80)）：

```python
if validate:
    print("calculate rendered testing images ...")
    gaussExtractor.reconstruction_and_export(scene.getTestCameras(), test_dir, test_dir, stage="validation")
```

即：取**全部测试相机**（不是抽稀子集），交给 `GaussianExtractor`（4.3 详述），渲染图与 stats 都写在 `test_dir` 下。

main 中与评估直接相关的参数注册（[render.py:L118-L161](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/render.py#L118-L161)）：`--config`、`--start_checkpoint`（指向训练产出的 `.pth`）、`--traj`、`--validate`（`store_true`，默认 `False`）、`--training_view`/`--testing_view`（逗号分隔相机号）。**注意 `--validate` 才是评估开关，仓库里没有 `--test` 这个参数**——README 的评估命令在这里与源码不一致（见 4.1.4 实践）。

评估入口的两个「门」与路径拼接（[render.py:L179-L199](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/render.py#L179-L199)）：

```python
if args.exhaust_test:
    args.test_iterations = args.test_iterations + [i for i in range(0,op.iterations,500)]
if args.output_dir:
    args.model_path = os.path.join(args.model_path, args.output_dir, 'render')
    os.makedirs(args.model_path, exist_ok=True)
...
assert args.traj or args.validate, "No validation or trajectory rendering requested"
validation(lp.extract(args), name, pp.extract(args), args.start_checkpoint, ...)
```

两个值得注意的细节：

- **路径算术不对称**：训练时 `model_path = model_path/<output_dir>`（[train.py:L457-L458](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L457-L458)），评估时再多拼一层 `/render`。所以若训练用了 `--output_dir debug`，checkpoint 在 `output/N3V/scene/debug/chkpnt30000.pth`，而评估输出落在 `output/N3V/scene/debug/render/test/ours_30000/`——`--start_checkpoint` 要指向**训练目录**（带 `output_dir`、不带 `render`）。
- **`exhaust_test` 在 render.py 中是遗留死参数**：它只修改 `args.test_iterations`，而 `validation()` 根本不接收这个参数（那是 `training_report` 的抽查时间点列表，见 4.3）。官方 yaml（如 [configs/dynerf/flame_steak.yaml:L8](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/configs/dynerf/flame_steak.yaml#L8) 的 `exhaust_test: True`）在评估时不会产生任何效果。这呼应 u1-l3 提醒过的「识别遗留代码」。

还有一个入口级差异：render.py 默认 `gaussian_dim=3`、`time_duration=[-0.5, 0.5]`（[render.py:L130-L131](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/render.py#L130-L131)），而 train.py 默认 `gaussian_dim=4`、`time_duration=[0, 10.0]`。**评估 4D 模型必须靠同一份 yaml 把它们覆盖回来**（yaml 里 `gaussian_dim: 4`、`time_duration: [0.0, 10.0]`），这也是 README 命令总是带 `--config` 的原因。

#### 4.1.4 代码实践：修正 README 的评估命令

**实践目标**：发现并修复 README 与源码的参数名不一致，得到一条真正可用的评估命令。

**操作步骤**：

1. 阅读 [README.md:L180-L191](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/README.md#L180-L191)，抄下官方评估命令（注意其中的 `--test`）。
2. 在仓库根目录执行 `python render.py --help`，在输出的可选参数列表里搜索 `--test` 与 `--validate`。
3. 若环境里装了 flask 与四个 CUDA 扩展，直接照抄 README 命令执行一次，观察报错；否则跳过执行，仅做参数比对。
4. 把 `--test` 改成 `--validate`，写出修正后的命令：

   ```bash
   python render.py \
     --config configs/dynerf/flame_steak.yaml \
     --training_view 1,10,13,20 \
     --output_dir debug \
     --validate \
     --start_checkpoint output/N3V/flame_steak/debug/chkpnt30000.pth
   ```

**需要观察的现象**：

- `--help` 输出中存在 `--validate`（help 文本写的是 "Comma-separated list of cameras to use for validation" 所属的另一组参数旁边），但**不存在** `--test` 布尔开关（只有 `--test_iterations`）。
- 照抄 README 命令时，argparse 会在任何初始化之前报 `unrecognized arguments: --test` 并以非零码退出。

**预期结果**：得到一条 argparse 可接受的评估命令。两点诚实提醒：① `render.py` 顶部 [render.py:L14](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/render.py#L14) 的 `from flask import testing` 是一个可疑的遗留导入——若环境未安装 flask，连 `--help` 都会先 ImportError；② 由于模块顶层就 `from gaussian_renderer import render`，`--help` 同样要求四个 CUDA 扩展已编译安装。这两点的实际表现**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `render.py` 的 main 里要有 `assert args.traj or args.validate`？

**答案**：`--traj` 与 `--validate` 都是默认关闭的 `store_true` 开关，两个都不开时 `validation()` 什么都做不了，却仍会完整构造 GaussianModel、加载 Scene（读点云、建初始高斯）、加载 checkpoint——白白付出代价。断言把「无事可做」提前到最便宜的时点暴露，这是快速失败（fail fast）的典型用法（[render.py:L193](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/render.py#L193)）。

**练习 2**：忘记传 `--config` 会发生什么？

**答案**：`--config` 默认为 `None`，`args.config.split("/")` 在 [render.py:L163](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/render.py#L163) 就会对 `None` 调用 `.split` 抛 `AttributeError`；即便绕过这行，`OmegaConf.load(None)` 也会失败。更重要的是没有 yaml，`gaussian_dim` 会停留在默认的 3，与 4D checkpoint 不匹配——评估前就崩溃反而是好事，最怕的是「看起来跑通了」。

**练习 3**：`validation()` 里为什么先 `Scene(...)` 再 `restore`，而不是先 restore 再建 Scene？

**答案**：`GaussianModel.__init__` 只做属性占位（u3-l1/l2），真正的形状与数值要么由 `create_from_pcd`（在 `Scene.__init__` 内触发）填充、要么由 `restore` 覆盖。先建 Scene 可以让初始高斯与 checkpoint 的高斯维度、设备一致，restore 只做逐元素覆盖；此外 `Scene` 还负责写出 `input.ply`/`cameras.json` 并提供 `getTestCameras()`，评估循环离不开它。

### 4.2 gaussians.restore(model_params, None)：推理态恢复

#### 4.2.1 概念说明

`torch.load(checkpoint)` 得到 `(model_params, first_iter)`，其中 `model_params` 是 4D 模型下 21 个元素的元组（u5-l5 已列出清单）。`restore` 的第二个参数 `training_args` 决定这些元素的命运：

- **推理态**（`training_args=None`，本讲场景）：只回填参数张量与开关，一切与优化相关的内容（优化器、动量、致密化梯度统计、Coefficient 权重）被解包成局部变量后直接丢弃；
- **续训态**（`training_args=opt`）：额外重建优化器并 `load_state_dict`，恢复 Adam 动量与 `xyz_gradient_accum`/`t_gradient_accum`/`denom`。

评估之所以能走推理态，是因为**渲染一张图只需要九组高斯属性**，而衰减网络的历史作用已经通过 `_opacity.data` 原位写回累积进了 `_opacity`（见 2.3）。

#### 4.2.2 核心流程

`restore(model_args, None)` 在 4D 分支做的事，本质是一次**按位置解包赋值**：

```text
model_args（21 项，顺序即 capture() 的书写顺序）
  0  active_sh_degree     → self.active_sh_degree
  1  _xyz                 → self._xyz
  2  _features_dc         → self._features_dc
  3  _features_rest       → self._features_rest
  4  _scaling             → self._scaling
  5  _rotation            → self._rotation
  6  _opacity             → self._opacity        ← 衰减累积结果就在这里
  7  max_radii2D          → self.max_radii2D
  8  xyz_gradient_accum   → 局部变量（丢弃）
  9  t_gradient_accum     → 局部变量（丢弃）
  10 denom                → 局部变量（丢弃）
  11 optimizer state_dict → 局部变量 opt_dict（丢弃）
  12 coef_optimizer state → 局部变量 coef_opt_dict（丢弃）
  13 spatial_lr_scale     → self.spatial_lr_scale
  14 _t                   → self._t
  15 _scaling_t           → self._scaling_t
  16 _rotation_r          → self._rotation_r
  17 rot_4d               → self.rot_4d
  18 env_map              → self.env_map
  19 active_sh_degree_t   → self.active_sh_degree_t
  20 coefficient state    → 局部变量 coefficient_dict（丢弃）
training_args is None → 跳过整个优化器重建块
```

注意 4D 元组的顺序与 3D（14 项）不同且互不兼容（协议无自描述字段，u5-l5），所以用 3D 的 `restore` 解 4D checkpoint 会直接解包失败。

#### 4.2.3 源码精读

`restore` 的完整实现（[scene/gaussian_model.py:L141-L191](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L141-L191)），4D 分支解包后：

```python
elif self.gaussian_dim == 4:
    (self.active_sh_degree, 
    self._xyz, 
    ...
    self._rotation_r,
    self.rot_4d,
    self.env_map,
    self.active_sh_degree_t,
    coefficient_dict) = model_args
    
if training_args is not None:
    self.training_setup(training_args)
    self.xyz_gradient_accum = xyz_gradient_accum
    self.t_gradient_accum = t_gradient_accum
    self.denom = denom
    self.optimizer.load_state_dict(opt_dict)
    
    if coefficient_dict is not None and self.coefficient is not None:
        self.coefficient.load_state_dict(coefficient_dict)
        
    if self.coef_optimizer is not None:
        self.coef_optimizer.load_state_dict(coef_opt_dict)
```

关键观察：

1. **分水岭是一个 `if`**：`training_args is None` 时，从 `training_setup` 到 `coef_optimizer.load_state_dict` 的整块全部跳过。评估态不新建优化器，也就不需要学习率、不需要 `spatial_lr_scale` 生效——这也解释了 u2-l4 的结论为何只对训练成立（「必须先 Scene 再 training_setup，否则位置学习率为 0」），评估根本不碰学习率。
2. **`self.rot_4d` 也被恢复**：这个布尔开关与协方差的时间边缘化路径绑定（u3-l3），checkpoint 怎么训的就怎么评估，不会因为命令行忘了 `--rot_4d` 而改变计算路径（命令行的 `rot_4d` 在 restore 后被覆盖）。
3. **`coefficient_dict` 被安全丢弃**：条件 `coefficient_dict is not None and self.coefficient is not None` 双重护栏——render.py 构造的 GaussianModel 没传 coefficient，`self.coefficient` 为 `None`，即使 checkpoint 里有衰减网络权重也不会（也不需要）加载。
4. **评估期不会再衰减**：`GaussianExtractor` 用 `partial(render, pipe=pipe, bg_color=background)` 固定了渲染函数的 `pipe` 与 `bg_color`（[utils/mesh_utils.py:L93-L94](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/mesh_utils.py#L93-L94)），`args` 保持默认 `None`；而 render() 里衰减分支的第一重门控就是 `args is not None`（[gaussian_renderer/__init__.py:L63-L75](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L63-L75)）。所以评估渲染是纯前向地读取已累积的 `_opacity`。

与训练续训的对照组（[train.py:L69-L72](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L69-L72)）：

```python
if checkpoint:
    (model_params, first_iter) = torch.load(checkpoint)
    gaussians.restore(model_params, opt)      # ← 第二实参是 opt，续训态
```

训练入口在 `training_setup(opt)` 之后从 checkpoint 恢复，走的是完整协议：重建优化器、回填动量，`first_iter` 还会作为循环起点让训练从断点继续（[train.py:L88-L89](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L88-L89)）。评估则完全忽略 `first_iter` 的循环语义，只拿它当目录名。

#### 4.2.4 代码实践：离线解剖一个 checkpoint

**实践目标**：不依赖 GPU 和数据集，用纯 CPU 把 `chkpnt30000.pth` 拆开，亲眼验证 21 元组协议与 4.2.2 的清单一致。

**操作步骤**：

1. 确认训练输出目录里存在 `chkpnt30000.pth`（由 `scene.save` 在 `saving_iterations` 命中时写出，[scene/__init__.py:L109-L112](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/__init__.py#L109-L112)；没有训练产出时可先跳过执行，只读代码做纸面推演）。
2. 新建独立脚本 `inspect_ckpt.py`（**示例代码**，放在教程目录或任意临时位置，不要放进源码树）：

   ```python
   import torch

   ckpt = "output/N3V/flame_steak/debug/chkpnt30000.pth"
   model_params, iteration = torch.load(ckpt, map_location="cpu")
   print("iteration =", iteration)
   print("tuple len =", len(model_params))          # 预期 21（4D）

   names = ["active_sh_degree", "_xyz", "_features_dc", "_features_rest",
            "_scaling", "_rotation", "_opacity", "max_radii2D",
            "xyz_gradient_accum", "t_gradient_accum", "denom",
            "opt_dict", "coef_opt_dict", "spatial_lr_scale",
            "_t", "_scaling_t", "_rotation_r", "rot_4d", "env_map",
            "active_sh_degree_t", "coefficient_dict"]

   for i, (name, item) in enumerate(zip(names, model_params)):
       if torch.is_tensor(item):
           print(f"{i:2d} {name:22s} tensor {tuple(item.shape)}")
       elif isinstance(item, dict):
           print(f"{i:2d} {name:22s} dict   keys={len(item)}")
       else:
           print(f"{i:2d} {name:22s} {type(item).__name__} = {item}")
   ```

3. 运行 `python inspect_ckpt.py`。
4. 对照 [scene/gaussian_model.py:L116-L139](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/gaussian_model.py#L116-L139) 的 `capture()` 4D 分支，逐项核对名字、顺序、形状。
5. 回答：`_opacity` 的形状 `(N, 1)` 里的 N 是多少？它与你训练日志里最后一行 `total_points` 是否一致？

**需要观察的现象**：`tuple len = 21`；索引 0 是小整数（激活球谐阶数，应为 3）；`_xyz` 形状 `(N, 3)`、`_t` 形状 `(N, 1)`、`_rotation_r` 形状 `(N, 4)`；索引 11/12 是 dict（优化器状态）；索引 20 是 Coefficient 网络的 state_dict（若训练开了 `opacity_decay`，默认开启）。

**预期结果**：打印清单与 4.2.2 的表格逐行对应。这个脚本无需 import 任何项目代码——checkpoint 里只有张量、标量、dict、bool 和 None，可被裸 `torch.load` 反序列化。若手头没有训练产物，本实践为「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`restore(model_params, None)` 相比 `restore(model_params, opt)` 少做了哪四件事？

**答案**：① 不调用 `training_setup`（不新建高斯优化器）；② 不恢复 `xyz_gradient_accum`/`t_gradient_accum`/`denom` 致密化统计；③ 不 `optimizer.load_state_dict`（Adam 动量丢弃）；④ 不加载 Coefficient 权重与 `coef_optimizer` 状态。回填的只有参数张量与 `active_sh_degree`/`rot_4d`/`env_map`/`active_sh_degree_t`/`spatial_lr_scale` 这些轻量状态。

**练习 2**：render.py 的 GaussianModel 没传 coefficient，checkpoint 里的衰减网络权重（元组第 20 项）被丢弃了，为什么评估结果仍然正确？

**答案**：因为衰减不是评估期才计算的一次性乘法，而是训练期每个迭代都通过 `_opacity.data` 原位写回、**复利累积**进 `_opacity` 的（u6-l2/l3）。checkpoint 保存的 `_opacity` 已经是「初始不透明度 × 历次衰减因子」的结果，评估只需读它。同时评估渲染不传 `args`，render() 的衰减分支因 `args is not None` 不满足而整体跳过，评估期不会再衰减——两条机制共同保证推理态的纯前向语义。

**练习 3**：如果不小心用一个 3D 构造的 GaussianModel（`gaussian_dim=3`）去 restore 一个 4D checkpoint，会发生什么？

**答案**：`restore` 走 `gaussian_dim == 3` 分支，按 14 个元素解包一个 21 元素的元组，抛 `ValueError: too many values to unpack`。协议是顺序敏感且无自描述字段的（u5-l5），维度不匹配在最开始就崩——这就是评估必须用与训练同一份 yaml（保证 `gaussian_dim: 4` 被覆盖）的根本原因。

### 4.3 held-out 测试相机与三套指标口径

#### 4.3.1 概念说明

仓库里其实有**三个地方**报告测试质量，它们的相机集合、指标实现和用途都不同。混用它们是初学者最常见的困惑来源（「为什么 render.py 的 PSNR 和训练日志对不上？」），本节把它们摆在一起对照：

| 维度 | ① 训练进度条 PSNR | ② `training_report` 测试口径 | ③ `render.py --validate` |
| --- | --- | --- | --- |
| 出现位置 | train.py 的 tqdm postfix | 训练日志 `[ITER N] Evaluating test` + TensorBoard | `test/ours_N/stats/validation.json` |
| 相机集合 | 当前 batch 的**训练**视角 | `getValidationCameras(tag='test')`，`[::100]` **抽稀** | `getTestCameras()` **全量** |
| 是否 clamp | 否 | 是（`torch.clamp(image, 0, 1)` 后再算） | 是（clamp 后存成 uint8 再读回） |
| PSNR 归约 | 逐通道 PSNR 再均值 | 逐通道 PSNR 再均值 | 全局 MSE（三通道合并） |
| SSIM 实现 | 无 | `fused_ssim`（CUDA 版） | `skimage` 的 `structural_similarity`，报告为 D-SSIM |
| 额外指标 | Loss、gs_num | L1、SSIM | LPIPS(alex)、Dssim1、Dssim2 |
| 用途 | 粗略监控 | 训练期评估、**best checkpoint 唯一判据** | 最终报表 |

其中 PSNR 的定义在 [utils/image_utils.py:L17-L19](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/image_utils.py#L17-L19)，峰值硬编码为 1.0：

\[
\mathrm{PSNR} = 20\log_{10}\frac{1.0}{\sqrt{\mathrm{MSE}}} = -10\log_{10}(\mathrm{MSE})
\]

「逐通道」与「全局」的差异来自张量形状：`(C,H,W)` 经 `view(C,-1).mean(1)` 得到每通道一个 MSE、各自取 PSNR 再平均；而 `unsqueeze(0)` 后变 `(1,C,H,W)`，`view(1,-1)` 把三通道 **全部摊平成一个 MSE**。由 Jensen 不等式，逐通道 PSNR 的均值 ≥ 全局 MSE 的 PSNR（当各通道 MSE 不同时严格大于），所以口径 ③ 通常比口径 ② 略低——这不是 bug，是归约方式不同。

D-SSIM 则是 4DGS 系列论文的报告习惯，把结构相似度折算成「损失」：

\[
D_{\mathrm{SSIM}} = \frac{1-\mathrm{SSIM}}{2}
\]

#### 4.3.2 核心流程

**口径 ②：`training_report`（训练期）**

```text
iteration ∈ testing_iterations（默认 [7000, 30000]）
 ├─ validation_configs = [ {train, getValidationCameras(tag='train')},
 │                          {test,  getValidationCameras(tag='test')} ]
 ├─ 对每组相机逐个：
 │    render（不传 args，无衰减）→ image = clamp(render, 0, 1)
 │    l1_test   += l1_loss(image, gt).mean()
 │    psnr_test += psnr(image, gt).mean()          ← 逐通道
 │    ssim_test += fast_ssim(image, gt).mean()     ← CUDA 版
 │    若 test 且 idx%5==0：落盘 test_iter_N_cam_XX.png 与 _gt.png
 ├─ 三项各自 /= 相机数（按相机平均，不是按帧加权）
 ├─ print + TensorBoard 写三个标量
 └─ 返回 test 组的 PSNR → best checkpoint 的判据
```

**口径 ③：`reconstruction_and_export`（render.py --validate）**

```text
对 getTestCameras() 的每个 (gt, cam)：
 ├─ 若 renders/{idx:05d}.png 已存在 → 从磁盘读回（断点续算）
 ├─ 否则 render → NaN/Inf 检查 → clamp → 存成 uint8 png
 ├─ 指标（CPU/GPU 混合）：
 │    psnr(gt.unsqueeze(0), rgb.unsqueeze(0))      ← 全局 MSE
 │    lpips(gt, rgb, net_type='alex')
 │    Dssim1 = (1 - sk_ssim(data_range=1.0)) / 2
 │    Dssim2 = (1 - sk_ssim(data_range=2.0)) / 2
 └─ 汇总：各项取均值 + num_GS + 计数 → stats/validation.json
```

注意它有一个贴心的工程特性：**已渲染过的图直接从磁盘读回**，评估中断后重跑不会重复渲染（代价是读回的是 uint8 量化后的图，指标基于量化图计算）。

#### 4.3.3 源码精读

**相机获取的两个接口**（[scene/__init__.py:L114-L127](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/__init__.py#L114-L127)）：

```python
def getTestCameras(self, scale=1.0):
    return CameraDataset(self.test_cameras[scale].copy(), self.white_background)
    
def getValidationCameras(self, scale=1.0, tag='train', num=100):
    if tag == 'train':
        return CameraDataset(self.train_cameras[scale][::num], self.white_background)
    elif tag == 'test':
        return CameraDataset(self.test_cameras[scale][::num], self.white_background)
```

`getTestCameras` 给全量，`getValidationCameras` 用 `[::num]`（默认 `num=100`）做等距抽稀——训练期每隔 100 台抽 1 台是为了评估不拖慢训练。**同一个测试集，两个入口拿到的相机数量差一个数量级**，这是口径 ② 与 ③ 数值差异的第一来源。

**口径 ② 的指标累积**（[train.py:L316-L364](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L316-L364)）：

```python
validation_configs = (
    {'name': 'train', 'cameras': scene.getValidationCameras(tag='train')},
    {'name': 'test', 'cameras': scene.getValidationCameras(tag='test')},
)
for config in validation_configs:
    ...
    for idx, batch_data in enumerate(tqdm(config['cameras'], ncols=80)):
        gt_image, viewpoint = batch_data
        ...
        render_pkg = renderFunc(viewpoint, scene.gaussians, *renderArgs)
        image = torch.clamp(render_pkg["render"], 0.0, 1.0)
        l1_test += l1_loss(image, gt_image).mean().double()
        psnr_test += psnr(image, gt_image).mean().double()
        ssim_test += fast_ssim(image.unsqueeze(0), gt_image.unsqueeze(0)).mean().double()
        if config['name'] == 'test' and idx % 5 == 0:
            ...   # 落盘 test_iter_{iteration}_cam_{image_name}.png 与 _gt.png
    psnr_test /= len(config['cameras'])
    ...
    if config['name'] == 'test':
        psnr_test_iter = psnr_test.item()
```

三个细节：渲染调用 `renderFunc(viewpoint, scene.gaussians, *renderArgs)` 里 `renderArgs = (pipe, background)`，同样**不传 `args`**，评估渲染不做衰减；`image` 先 clamp 再算指标（训练进度条的 PSNR 不 clamp，见 [train.py:L193-L194](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L193-L194)，且取的是 batch 末视角，u5-l2 已辨析）；返回值 `psnr_test_iter` 被 [train.py:L272-L276](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L272-L276) 用来决定是否覆盖 `chkpnt_best.pth`。

**口径 ③ 的指标计算**（[utils/mesh_utils.py:L347-L376](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/mesh_utils.py#L347-L376)）：

```python
if stage == "validation" and rgb is not None:
    ...
    gt_image = gt_image.clamp(0.0, 1.0)
    rgb = rgb.clamp(0.0, 1.0)
    ...
    metrics["psnr"].append(psnr(gt_image.unsqueeze(0), rgb.unsqueeze(0)).cpu())
    metrics["lpips"].append(lpips_fn(gt_image, rgb, net_type='alex').cpu())
    rgb_np = rgb.detach().cpu().numpy()
    gt_np = gt_image.detach().cpu().numpy()
    metrics['Dssim1'].append(torch.tensor((1.0 - sk_ssim(rgb_np, gt_np, data_range=1.0, multichannel=True, channel_axis=0)) / 2.0))
    metrics['Dssim2'].append(torch.tensor((1.0 - sk_ssim(rgb_np, gt_np, data_range=2.0, multichannel=True, channel_axis=0)) / 2.0))
```

`psnr(gt.unsqueeze(0), rgb.unsqueeze(0))` 把 `(C,H,W)` 抬成 `(1,C,H,W)`，于是 `view(shape[0], -1)` 得到 `(1, C·H·W)`——**全局 MSE**，这就是与口径 ② 的第二处差异。LPIPS 用 [lpipsPyTorch](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/lpipsPyTorch/__init__.py) 的 AlexNet 感知特征距离（训练期的 `training_report` 完全不算 LPIPS）。`Dssim1`/`Dssim2` 唯一区别是 `data_range` 取 1.0 还是 2.0——skimage 的 SSIM 常数项 \(C_1=(k_1 L)^2\)、\(C_2=(k_2 L)^2\) 随 \(L\) 变大而变大，常数项起「分母」作用，所以同一对图像恒有 `Dssim2 ≤ Dssim1`，两个都存是为了方便与不同论文的约定对表。

**汇总与落盘**（[utils/mesh_utils.py:L389-L423](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/mesh_utils.py#L389-L423)）：所有指标 `torch.stack(v).mean()` 按图平均，附上 `num_GS`（高斯数量）与 `total_images`/`rendered_images`/`skipped_images` 计数，写成 `test/ours_N/stats/validation.json`，并打印 `PSNR: xx.xxx / LPIPS: x.xxxx / Number of GS: N`。

最后是 train/test 划分这个上游开关（[scene/dataset_readers.py:L280-L289](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L280-L289)）：

```python
if eval:
    train_cam_infos = [c for idx, c in enumerate(cam_infos) if c.image_name.split('_')[0] in training_cam]
    if testing_cam:
        test_cam_infos = [c for idx, c in enumerate(cam_infos) if c.image_name.split('_')[0] in testing_cam]
    else:
        test_cam_infos = [c for idx, c in enumerate(cam_infos) if c.image_name.split('_')[0] not in training_cam]
else:
    train_cam_infos = cam_infos
    test_cam_infos = []
```

**评估时漏传 `--training_view` 的后果**：CLI 默认是空串 `""`（[render.py:L141-L147](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/render.py#L141-L147)），`if args.training_view:` 为假，于是传给 Scene 的是 `""`；`"cam01" in ""` 恒为 `False`，train 集合变空、test 集合变成**全部相机**（包含训练相机，PSNR 会虚高），随后 `getNerfppNorm` 对空列表做 `np.hstack` 还会直接抛异常（[scene/dataset_readers.py:L67-L74](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/scene/dataset_readers.py#L67-L74)，异常的具体表现待本地验证）。所以**评估命令必须带与训练一致的 `--training_view`**——README 的示例命令是对的。

#### 4.3.4 代码实践：复算指标并与训练日志对照

**实践目标**：不依赖 CUDA 扩展，用纯 CPU 脚本批量复算 PSNR / D-SSIM，与训练日志（口径 ②）和 `render.py --validate` 的 stats（口径 ③）三方对照，量化「口径差异」到底有多大。

**操作步骤**：

1. **找到现成的图像对**。训练期 `training_report` 每 5 台测试相机落一对图（[train.py:L339-L353](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L339-L353)），存于 `<训练输出目录>/rendered_images/`，命名形如 `test_iter_30000_cam_cam02_0107.png`（渲染）与 `test_iter_30000_cam_cam02_0107_gt.png`（真值）。`cam02` 说明它来自留出相机。
2. 新建 `recompute_metrics.py`（**示例代码**，只用 numpy / torch / scikit-image）：

   ```python
   import glob, os, torch
   from PIL import Image
   from skimage.metrics import structural_similarity as sk_ssim

   img_dir = "output/N3V/flame_steak/debug/rendered_images"
   pairs = sorted(
       (p, p.replace(".png", "_gt.png"))
       for p in glob.glob(os.path.join(img_dir, "test_iter_30000_cam_*.png"))
       if not p.endswith("_gt.png")
   )
   print(f"found {len(pairs)} pairs")

   psnrs, dssims = [], []
   for pred_path, gt_path in pairs:
       pred = torch.from_numpy(np.asarray(Image.open(pred_path).convert("RGB"), dtype=np.float32) / 255.0).permute(2, 0, 1)
       gt   = torch.from_numpy(np.asarray(Image.open(gt_path).convert("RGB"),   dtype=np.float32) / 255.0).permute(2, 0, 1)
       # 与 mesh_utils 一致：unsqueeze(0) → 全局 MSE
       mse = ((pred - gt) ** 2).view(1, -1).mean(1)
       psnrs.append((-10 * torch.log10(mse)).item())
       # 与 training_report 一致：逐通道 PSNR 再平均
       mse_ch = ((pred - gt) ** 2).view(3, -1).mean(1)
       dssims.append((1.0 - sk_ssim(pred.numpy(), gt.numpy(), data_range=1.0, channel_axis=0)) / 2.0)

   print(f"PSNR(global, 口径③): {sum(psnrs)/len(psnrs):.3f}")
   print(f"D-SSIM (skimage):     {sum(dssims)/len(dssims):.4f}")
   ```

   （注意脚本开头补 `import numpy as np`。）
3. 打开训练日志，找最后一行 `[ITER 30000] Evaluating test: L1 ... PSNR ...`（由 [train.py:L358](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/train.py#L358) 打印），记下这个 PSNR。
4. 执行第 5 节的完整评估命令后，读 `test/ours_30000/stats/validation.json`，记下其中的 `psnr` 与 `Dssim1`。
5. 把三个数字填进对照表：

   | 来源 | 相机集合 | PSNR 归约 | 数值 |
   | --- | --- | --- | --- |
   | 训练日志（口径②） | test 的 `[::100]` 抽稀 | 逐通道 |  |
   | 本脚本（uint8 图对） | test 的每 5 台 | 全局 + 逐通道 |  |
   | stats/validation.json（口径③） | test 全量 | 全局 |  |

**需要观察的现象**：

- 本脚本的「逐通道」PSNR 与训练日志最接近（同归约方式），但仍有 0.1~0.5 dB 量级的偏差——来源是：① 训练日志在 `[::100]` 抽稀集上算，本脚本在「每 5 台」的落盘子集上算，相机集合不同；② 训练日志在 float32 张量上算，本脚本读的是量化到 uint8 的 png。
- 本脚本的「全局」PSNR 系统性低于「逐通道」PSNR（Jensen 不等式），与 stats json 的口径一致。
- `Dssim1` 与训练日志没有直接对应物（口径 ② 报告的是 `1-SSIM`，不除 2，且用 CUDA `fused_ssim`），只能与 stats json 里的 `Dssim1` 比较。

**预期结果**：三个数字两两不等但同一量级（例如 30 dB 档的模型差异在 1 dB 以内）。如果本脚本与 stats json 的全局 PSNR 差距明显，先检查相机集合是否一致（json 里的 `total_images`/`valid_images` 是多少？训练目录 `rendered_images` 里 `test_iter_30000_*` 有几对？）。若手头没有训练产物，本实践为「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `render.py --validate` 的 PSNR 与训练日志 `[ITER 30000]` 的 PSNR 天然对不齐？列出至少三个原因。

**答案**：① **相机集合不同**：训练日志用 `getValidationCameras(tag='test')` 的 `[::100]` 抽稀，render.py 用 `getTestCameras()` 全量；② **PSNR 归约不同**：训练日志逐通道算 PSNR 再平均，render.py 经 `unsqueeze(0)` 用全局 MSE；③ **数值路径不同**：训练日志在 float32 渲染张量上直接算，render.py 存成 uint8 png 后（首次渲染时）或从磁盘读回后再算，多了量化误差；④ **随机性**：若两边的 `--training_view` 不一致，划分本身就不同。

**练习 2**：`training_report` 为什么要对 test 组 `idx % 5 == 0` 才落盘图像，而不是全部保存？

**答案**：落盘只是留档抽查，指标计算用的是**全部**（抽稀后的）相机——落图与算指标是两件独立的事。每 5 台存一对已经足够人工检查渲染质量，又能避免每个评估迭代写出几百 MB 的 png。这也是为什么第 4.3.4 的实践脚本天然只能覆盖测试集的一个子集。

**练习 3**：`reconstruction_and_export` 如何支持「中断后续算」？这个设计有什么代价？

**答案**：它在渲染前检查 `renders/{idx:05d}.png` 是否已存在，存在则直接从磁盘读回当作者结果用，只对缺失的图调用渲染（[utils/mesh_utils.py:L284-L307](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/mesh_utils.py#L284-L307)），还提供了 `skip_rendering=True` 的纯复算模式。代价是：读回的是 clamp 后量化成 uint8 的图，续算部分与首算部分（若被中断前已按 float 算过——实际不会，指标都在量化图上算）口径统一了，但与训练日志的 float32 口径之间多了量化差；另外它信任磁盘上的旧文件，如果换了 checkpoint 而没换输出目录，会拿旧图算新指标。

## 5. 综合实践：一次可复现的评估 + 三方指标对账

把本讲三个模块串起来，完成一次从 checkpoint 到报表的完整评估，并用一份对账表回答「到底信哪个数」。

**任务清单**：

1. **准备**：确认一个训练完成的输出目录（含 `chkpnt30000.pth`、`training_params.txt`、`rendered_images/`）与对应 yaml；先读 `training_params.txt` 确认 `training_view`、`iterations`、`output_dir` 的实际生效值（u1-l4 的结论：这是排错第一入口）。
2. **写出正确命令**：基于 4.1.4 修正后的命令模板，把 `--config`、`--training_view`、`--output_dir`、`--start_checkpoint` 换成你的实际值。特别注意：`--start_checkpoint` 指向训练目录（`model_path/<output_dir>/chkpnt30000.pth`），而输出会落在 `model_path/<output_dir>/render/test/ours_30000/`。
3. **执行评估**：`python render.py --config <yaml> --training_view 1,10,13,20 --output_dir <dir> --validate --start_checkpoint <ckpt>`。
4. **读报表**：打开 `test/ours_30000/stats/validation.json`，记录 `psnr`、`lpips`、`Dssim1`、`Dssim2`、`num_GS`、`total_images`、`valid_images`。核对 `num_GS` 与训练日志 `total_points` 曲线末值一致（同一 checkpoint 的同一批高斯）。
5. **复算对账**：运行 4.3.4 的脚本，填出三方对照表，并用三句话解释差异来源（相机集合 / PSNR 归约 / 量化）。
6. **（可选）口径实验**：把 4.3.4 脚本里 `view(1,-1)` 改成 `view(3,-1)` 再跑一次，亲测「全局 vs 逐通道」贡献了多少 dB 的差异。

**验收标准**：能不查讲义地说出——评估入口的唯一开关是 `--validate`（不是 README 写的 `--test`）；`restore(model_params, None)` 丢弃了什么、为什么安全；三个 PSNR 数字为什么互不相等、各自适合什么时候引用。若无 GPU 环境或训练产物，第 3 步之后的执行部分标注「待本地验证」，但命令与脚本应已就绪。

## 6. 本讲小结

- `render.py` 是纯前向入口：`validation()` 按「构造无 coefficient 的 GaussianModel → Scene（先 `create_from_pcd` 搭骨架）→ `torch.load` → `restore` → `GaussianExtractor`」装配评估；`--validate` 走 `getTestCameras()` 全量测试相机，`--traj` 走轨迹渲染（下一讲）。
- **README 的 `--test` 不存在于源码**，真正的评估开关是 `--validate`；且 render.py 默认 `gaussian_dim=3`，评估 4D 模型必须带与训练同一份 `--config`，`--start_checkpoint` 要指向训练目录（`output_dir` 下、不带 `/render` 后缀）。
- `restore(model_params, None)` 是推理态恢复：21 元组中只有参数张量与开关回填到 `self`，优化器、动量、致密化统计、Coefficient 权重四类被丢弃；评估之所以安全，是因为衰减已复利累积进 `_opacity`，且评估渲染不传 `args`、render() 的衰减分支整体跳过。
- 仓库有三套指标口径：进度条 PSNR（不 clamp、batch 末视角、训练视角）、`training_report`（clamp、逐通道 PSNR、`fused_ssim`、`[::100]` 抽稀测试相机、best checkpoint 判据）、`render.py --validate`（uint8 量化图、全局 MSE 的 PSNR、LPIPS、Dssim1/2、全量测试相机、stats json 报表）。
- 评估漏传 `--training_view` 会让 train 集合退化为空、test 变成全部相机，并在 `getNerfppNorm` 处崩溃；`exhaust_test` 与 `prepare_output_and_logger` 在 render.py 中是不生效的遗留代码。

## 7. 下一步学习建议

本讲只消费了 `--validate` 分支；`--traj` 分支（在任意时刻、任意插值视角上渲染「自由视角视频」）是 4C4D 最直观的展示能力，也是下一讲的主题：

- **u7-l2 novel-view 轨迹生成**：精读 `utils/render_utils.py` 的 `generate_path`，看 `getAllCameras()` 的训练+测试相机如何被插值出 480 个新视角，`fix_time`/`selected_frame` 如何冻结时间。
- **u7-l3 GaussianExtractor 与视频导出**：本讲只用了 `GaussianExtractor` 的 validation 路径，下一讲看它的 trajectory 路径与 `create_videos` 如何把帧合成 mp4。
- 想深挖指标本身的读者，可以回到 [utils/loss_utils.py:L37-L45](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/utils/loss_utils.py#L37-L45) 的纯 PyTorch `ssim` 与 [fused-ssim-main](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/fused-ssim-main/fused_ssim/__init__.py) 的 CUDA 实现对拍（u5-l2 已铺垫）。
- 做论文对比实验前，建议把本讲的三方对账表跑一遍再引用数字——不同 4DGS 系论文的 SSIM/PSNR 约定（`data_range`、D-SSIM 与 1-SSIM、逐通道与全局）差异足以颠倒 0.1 dB 以内的排名。
