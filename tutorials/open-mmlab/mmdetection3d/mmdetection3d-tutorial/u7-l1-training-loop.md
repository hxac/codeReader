# train.py 与 Runner 训练全流程

## 1. 本讲目标

前面六个单元里，我们已经把「数据如何变成 `dict(inputs, data_samples)`」「模型如何前向、算损失」讲透了，但始终有一个问题悬着：**这些东西是谁串起来、一圈一圈跑起来的？** 本讲回答这个问题。学完本讲，你应该能够：

1. 按顺序说出 `python tools/train.py config.py` 从解析命令行、读取配置、改写配置，到构建 Runner、启动训练主循环的完整步骤。
2. 知道 `--amp`、`--sync_bn`、`--resume`、`--cfg-options`、`--auto-scale-lr` 这五个常用参数各自在 `tools/train.py` 的哪几行被处理、分别改写了配置的哪个字段。
3. 读懂 `configs/_base_/schedules/cyclic-40e.py` 中 `optim_wrapper` 与 `param_scheduler` 的组织方式，理解循环式（cyclic）学习率的两段式设计。
4. 读懂 `configs/_base_/default_runtime.py` 中 6 个默认 hook、`env_cfg` 与 `log_processor` 的职责。
5. 独立完成一次 1 epoch 的最小化训练，在 `work_dir` 中找到日志、checkpoint 与 `vis_data`，并能解释日志里 `time`、`data_time`、`memory`、`eta`、`lr`、`loss` 各字段的含义与来源。

## 2. 前置知识

本讲第一次离开 mmdet3d 自己的代码、进入「训练引擎」的视角。先补几个基础概念，都不需要你写过训练框架，只要能用 PyTorch 跑过模型即可。

- **训练循环（training loop）**：深度学习训练的最外层结构——「取一个 batch → 模型前向算损失 → 反向传播 → 优化器更新参数」，重复若干次为一个 epoch，再重复若干个 epoch 直到结束。手写 PyTorch 时这是你自己在 `for` 循环里做的事；在 OpenMMLab 体系里，这个循环由 **MMEngine 的 `Runner`** 统一实现，用户只负责用配置文件「声明」循环的每个零件。
- **Hook（钩子）**：插在训练循环各个时间点上的回调对象，比如「每个 iteration 结束时记一次日志」「每个 epoch 结束时存一次权重」。Runner 在固定的时机（`before_run`、`before_train_epoch`、`before_train_iter`、`after_train_iter`、`after_train_epoch`……）调用所有已注册 hook 的同名方法。hook 机制让「训练本体」和「训练周边事务」（日志、断点、调度器、可视化）解耦——这和单元 4 讲过的注册器一样，是贯穿 OpenMMLab 的设计模式。
- **优化器包装器（optim_wrapper）**：PyTorch 的 `optimizer` 只管更新参数；`OptimWrapper` 在它外面包了一层，负责梯度裁剪（`clip_grad`）、混合精度、`update_params(loss)`（把「损失 → 梯度 → step」三步打包）等事务。配置里的 `optim_wrapper` 字段描述它。
- **参数调度器（param_scheduler）**：控制学习率/动量随训练进程变化的策略。MMEngine 允许把多个调度器排成一个列表，各自声明生效区间 `begin`/`end`，拼接出复杂曲线（本讲的 cyclic 调度就是两段余弦拼起来的）。
- **AMP（自动混合精度）**：让部分算子用 float16 计算以省显存、提速度，用 `AmpOptimWrapper` 配合动态 loss scaling 防止小梯度下溢。
- **SyncBN（同步批归一化）**：多卡训练时把各卡的 BN 统计量跨卡同步，等效于用全局 batch 算 BN。单卡训练用不到。
- **断点续训（resume）**：从之前保存的 checkpoint（含模型权重、优化器状态、已训练轮数）接着训，而不是从头再来。`load_from` 与 `resume` 是两个字段：前者只加载权重（如加载 ImageNet 预训练），后者恢复完整训练状态。
- **work_dir**：一次实验的全部产物（日志、配置转储、checkpoint、可视化数据）落地的目录。

前置讲义方面：本讲直接承接 [u4-l3](u4-l3-data-preprocessor-voxelize.md)（`Det3DDataPreprocessor` 是 `train_step` 内的第一站）与 [u3-l8](u3-l8-pack3d-det-inputs.md)（数据管线出口产出 `dict(inputs, data_samples)`）；配置继承语法请回忆 [u1-l5](u1-l5-config-system-basics.md)，注册器路由请回忆 [u4-l1](u4-l1-registry-mechanism.md)。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| `tools/train.py` | 训练命令行入口：解析参数、读配置、按 CLI 改写配置、构建 Runner、启动训练。全库训练的「总开关」，仅 146 行 |
| `configs/_base_/schedules/cyclic-40e.py` | 训练日程基座：`optim_wrapper`（AdamW + 梯度裁剪）、4 段拼接的 `param_scheduler`、`train_cfg`（40 epoch）与 `auto_scale_lr` |
| `configs/_base_/default_runtime.py` | 运行时基座：`default_scope`、6 个默认 hook、`env_cfg`、`log_processor`、`log_level`、`load_from`/`resume` |
| `configs/pointpillars/pointpillars_hv_secfpn_8xb6-160e_kitti-3d-3class.py` | 综合示例配置：四类 `_base_` 继承后如何增量覆盖日程与数据管线，本讲实践的主角 |
| `configs/_base_/datasets/kitti-3d-3class.py` | 数据侧基座：三个 dataloader、评估器与可视化器，Runner 要构建的另一半零件 |
| `mmdet3d/engine/hooks/visualization_hook.py` | `Det3DVisualizationHook`：默认 hook 之一的 3D 版实现，可窥 hook 的编写方式 |
| `tools/dist_train.sh` | 多卡训练启动脚本：用 `torch.distributed.launch` 拉起多个 `train.py` 进程 |
| `docs/en/user_guides/train_test.md` | 官方训练/测试使用文档 |

外部依赖提示：`Runner`、`Config`、`DictAction`、各个默认 hook 的实现都在 **mmengine** 包里（`mmengine/runner/runner.py`、`mmengine/hooks/` 等）。本讲对 mmengine 内部只讲行为与概念，源码精读与永久链接均落在 mmdet3d 仓库自身文件上；想深挖 mmengine 时，直接看你安装环境里的 `python -c "import mmengine; print(mmengine.__file__)"` 路径即可。

## 4. 核心概念与源码讲解

### 4.1 tools/train.py：命令行入口与配置预加工

#### 4.1.1 概念说明

`tools/train.py` 是全库统一的训练入口，但它**不包含任何训练逻辑**。它做的事情只有两类：

1. **翻译**：把命令行参数翻译成对配置字典的修改（`--amp` → 把 `optim_wrapper.type` 改成 `AmpOptimWrapper`；`--resume` → 把 `cfg.resume` 置 `True`……）。
2. **交接**：把最终配置交给 `Runner.from_cfg(cfg)`，然后调用 `runner.train()` 扬长而去。

这个「薄入口 + 厚引擎」的分工是 MMEngine 架构的核心约定：所有实验差异都体现在配置文件里，入口脚本对所有算法一视同仁。这也是为什么 [u1-l4](u1-l4-repository-structure-map.md) 说 tools 目录只是「命令行入口」。

#### 4.1.2 核心流程

`main()` 的执行流程可以概括为五步（与 [u1-l4](u1-l4-repository-structure-map.md) 的总结一致）：

```
① parse_args()          解析命令行参数
② Config.fromfile()     读取并合并配置（含 _base_ 继承链）
③ CLI → cfg 改写        launcher / cfg-options / work_dir / amp /
                         sync_bn / auto_scale_lr / resume 依次处理
④ 构建 Runner           默认 Runner.from_cfg(cfg)；若配置含
                         runner_type 则走 RUNNERS.build(cfg)
⑤ runner.train()        启动训练主循环（4.4 节）
```

#### 4.1.3 源码精读

**参数定义**：入口支持 10 个参数，逐个过一遍（建议对照源码阅读）：

[tools/train.py:L15-L66](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/train.py#L15-L66) 定义 `parse_args`：`config` 位置参数是配置文件路径；`--work-dir` 指定产物目录；`--amp` 开混合精度；`--sync_bn` 只接受 `none/torch/mmcv` 三种取值；`--auto-scale-lr` 按全局 batch 自动缩放学习率；`--resume` 用 `nargs='?', const='auto'` 实现了「带值=指定 checkpoint、不带值=自动找最新」的双语义；`--ceph` 把数据后端换成对象存储；`--cfg-options` 用 mmengine 的 `DictAction` 解析 `key=value` 对（回忆 [u1-l5](u1-l5-config-system-basics.md) 的 `merge_from_dict`）；`--launcher` 选分布式启动方式；`--local_rank` 兼容 torch 2.x 传入的 `--local-rank` 并写回 `LOCAL_RANK` 环境变量。

**配置读取与合并**：

[tools/train.py:L72-L81](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/train.py#L72-L81) `Config.fromfile(args.config)` 执行配置文件并完成 `_base_` 递归继承合并；若 `--ceph` 则经 `replace_ceph_backend`（定义于 [mmdet3d/utils/misc.py:L4](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/utils/misc.py#L4)）替换 backend_args；随后 `cfg.launcher = args.launcher`，`--cfg-options` 经 `cfg.merge_from_dict(args.cfg_options)` 免改文件地覆盖任意配置项。

**work_dir 三级优先级**：

[tools/train.py:L83-L90](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/train.py#L83-L90) 注释写明优先级「CLI > 配置文件中的 work_dir > 配置文件名」：命令行给了就用命令行的；否则配置里已有就用配置的；都没有则落到 `./work_dirs/<配置文件名去后缀>`。所以你平时不传 `--work-dir` 时，产物都出现在 `work_dirs/pointpillars_hv_secfpn_8xb6-160e_kitti-3d-3class/` 这样的目录里。

**--amp 的改写逻辑**：

[tools/train.py:L92-L105](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/train.py#L92-L105) 传 `--amp` 时先检查 `cfg.optim_wrapper.type`：若已是 `AmpOptimWrapper` 说明配置本身开了 AMP，打个警告了事；若不是 `OptimWrapper`（比如自定义包装器）则直接断言失败；否则改写为 `AmpOptimWrapper` 并设 `loss_scale='dynamic'`（动态损失缩放）。注意：**AMP 的开启是「改配置」而不是「改代码」**——Runner 只是照着改完的配置构建优化器包装器而已。

**--sync_bn / --auto-scale-lr / --resume**：

[tools/train.py:L107-L109](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/train.py#L107-L109) 把 `cfg.sync_bn` 设为 `'torch'` 或 `'mmcv'`，真正的 BN 层转换发生在 Runner 初始化模型时（读取该字段决定用 `torch.nn.SyncBatchNorm` 还是 mmcv 的实现）。

[tools/train.py:L111-L121](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/train.py#L111-L121) `--auto-scale-lr` 要求配置里**同时存在** `auto_scale_lr.enable` 与 `auto_scale_lr.base_batch_size`，否则抛 `RuntimeError`——这是一个很好的 fail-fast 范例：功能依赖的配置前提不满足时，宁可早失败也不静默用错学习率。满足后才把 `enable` 置 `True`，由 Runner 按「实际全局 batch / base_batch_size」比例缩放学习率。`base_batch_size` 的默认值就在日程配置里（见 4.2.3）。

[tools/train.py:L123-L129](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/train.py#L123-L129) resume 的优先级是「显式指定 > 自动恢复」：`--resume` 不带值（const 使其等于 `'auto'`）时 `cfg.resume=True` 且 `cfg.load_from=None`（Runner 会自己去 work_dir 找最新 checkpoint）；`--resume path.pth` 时 `cfg.resume=True` 且 `cfg.load_from=path.pth`。注意 `auto` 分支会把 `load_from` 强制清空——如果配置里原本写了预训练权重，`--resume` 会把它覆盖掉，续训语义优先。

**构建并启动**：

[tools/train.py:L131-L141](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/train.py#L131-L141) 若配置没有 `runner_type` 键就走默认 `Runner.from_cfg(cfg)`；否则从 `RUNNERS` 注册节点按 `runner_type` 构建自定义 Runner（这一分支服务于 projects 里替换训练行为的场景）。最后 `runner.train()` 一行，控制权交给引擎。

#### 4.1.4 代码实践

**实践目标**：在不启动训练的前提下，复刻 `train.py` 的「配置读取 + CLI 改写」前半段，直观看到 `--amp`、`--cfg-options` 到底改了配置里的什么。

**操作步骤**：

1. 在仓库根目录新建 `inspect_cfg.py`（示例代码，只需安装 mmengine 与 mmdet3d，无需数据集和 GPU）：

```python
# 示例代码：inspect_cfg.py —— 复刻 train.py 的配置预加工（不构建 Runner、不训练）
from mmengine.config import Config

# ② 读配置（等价于 train.py L73）
cfg = Config.fromfile(
    'configs/pointpillars/pointpillars_hv_secfpn_8xb6-160e_kitti-3d-3class.py')

# ③ 模拟 --cfg-options train_cfg.max_epochs=1 train_dataloader.batch_size=2
#    DictAction 会把 'a.b=c' 解析成嵌套 dict，这里直接写嵌套 dict 等价
cfg.merge_from_dict(dict(
    train_cfg=dict(max_epochs=1),
    train_dataloader=dict(batch_size=2),
))

# 模拟 --amp（等价于 train.py L92-L105 的 else 分支）
assert cfg.optim_wrapper.type == 'OptimWrapper'
cfg.optim_wrapper.type = 'AmpOptimWrapper'
cfg.optim_wrapper.loss_scale = 'dynamic'

# 观察合并结果
print('optim_wrapper =', cfg.optim_wrapper)
print('train_cfg     =', cfg.train_cfg)
print('batch_size    =', cfg.train_dataloader.batch_size)
print('dataset times =', cfg.train_dataloader.dataset.times)  # RepeatDataset 的 times
print('param_scheduler 段数 =', len(cfg.param_scheduler))
```

2. 运行 `python inspect_cfg.py`（只需安装了 mmengine 与 mmdet3d，无需数据集和 GPU）。
3. 再对比运行一次官方工具：`python tools/misc/print_config.py configs/pointpillars/pointpillars_hv_secfpn_8xb6-160e_kitti-3d-3class.py | head -50`，看未改写的原始合并结果。

**需要观察的现象**：

- `optim_wrapper` 的 `type` 从 `OptimWrapper` 变为 `AmpOptimWrapper`，且多了 `loss_scale='dynamic'`；
- `train_cfg.max_epochs` 从 80（PointPillars 配置覆盖了 cyclic-40e 的 40）变为 1；
- `train_dataloader.dataset.times` 是 2——这就是 [u1-l5](u1-l5-config-system-basics.md) 说过的「PointPillars 160e = 80 轮 × RepeatDataset(2)」的出处。

**预期结果**：打印出的字段与上述一致。若断言失败，说明该配置的 `optim_wrapper.type` 不是 `OptimWrapper`（本配置继承自 cyclic-40e，类型正确，应通过）。运行结果与本地 mmengine 版本相关，具体打印格式**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：`--resume` 不带任何值和带一个 checkpoint 路径，分别把配置改成了什么样？

答案：不带值时 `args.resume == 'auto'`，于是 `cfg.resume=True`、`cfg.load_from=None`，Runner 自动去 work_dir 找最新 checkpoint 恢复；带路径时 `cfg.resume=True`、`cfg.load_from=<路径>`，从指定文件恢复。两种情况都会覆盖配置里原有的 `load_from`（auto 分支显式置 `None`）。

**练习 2**：为什么 `--auto-scale-lr` 在配置缺少 `auto_scale_lr` 字段时直接抛异常，而不是静默忽略？

答案：因为学习率缩放依赖 `base_batch_size` 基准值，缺了它无法计算缩放比例；静默忽略会导致用户以为学习率被缩放了而实际没有，训练结果与预期不符。这是 fail-fast 设计原则的体现（源码 [tools/train.py:L111-L121](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/train.py#L111-L121)）。

**练习 3**：如果我想用注册在 `RUNNERS` 里的自定义 Runner 类训练，配置需要怎么写？

答案：在配置中添加 `runner_type = 'MyRunner'` 键（值为注册名），`train.py` 会走 [tools/train.py:L135-L138](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/train.py#L135-L138) 的 `RUNNERS.build(cfg)` 分支而非默认 `Runner.from_cfg`。

### 4.2 训练日程：cyclic-40e.py 的 optim_wrapper 与 param_scheduler

#### 4.2.1 概念说明

「日程（schedule）」配置回答三个问题：**用什么优化器、学习率怎么变、训多少轮**。mmdet3d 把它们放在 `configs/_base_/schedules/` 下作为可继承基座，KITTI 系模型普遍使用本讲的 `cyclic-40e.py`——一种**循环式（cyclic）**日程：学习率先升后降，动量先降后升，都由两段余弦调度拼接而成。相比单调递减的 step 日程，cyclic 在早期用较大学习率帮助跳出局部极小，是 SECOND 原论文沿用的训练技巧。

#### 4.2.2 核心流程

余弦退火调度把参数从基准值平滑过渡到 `eta_min`：

\[
\eta_t = \eta_{\min} + (\eta_{\text{base}} - \eta_{\min}) \cdot \frac{1 + \cos(\pi \cdot t / T_{\max})}{2}
\]

cyclic 日程把 40 个 epoch 切成两段，学习率与动量各配两个调度器：

- 第 1 段（epoch 0→16）：学习率从 `lr`（0.0018）沿余弦升到 `lr×10`（0.018）；动量调度器的 `eta_min=0.85/0.95`，即动量（AdamW 的 beta1，基准 0.95）朝低处走；
- 第 2 段（epoch 16→40）：学习率从峰值沿余弦降到 `lr×1e-4`；动量调度目标回升到 1（回到基准动量）。

一句话记忆：**学习率与动量反向行进**——学习率爬坡时压低动量，学习率退火时恢复动量，这正是配置注释所描述的两段式行为。

四个调度器以 `[begin, end)` 区间排他生效，`by_epoch=True` 声明区间单位是 epoch，`convert_to_iter_based=True` 再把按 epoch 设计的曲线**插值到 iteration 粒度**连续执行——所以日志里 `lr` 每个 iteration 都在变，而不是每 epoch 跳变一次。

#### 4.2.3 源码精读

**优化器包装器**：

[configs/_base_/schedules/cyclic-40e.py:L5-L11](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/schedules/cyclic-40e.py#L5-L11) 用 `lr = 0.0018` 作为初始学习率（文件头注释特别说明：cyclic 里写的 lr 是**初始值**而非峰值，峰值是它的 10 倍）；优化器是 PyTorch 官方 `AdamW`，`betas=(0.95, 0.99)` 与 `weight_decay=0.01` 沿袭 SECOND.Pytorch 的设置；`clip_grad=dict(max_norm=10, norm_type=2)` 是 L2 范数梯度裁剪——注意它配在 `OptimWrapper` 层而不是 optimizer 层，因为裁剪发生在反向传播之后、参数更新之前，正是包装器管的事。

**两段式学习率调度**：

[configs/_base_/schedules/cyclic-40e.py:L13-L33](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/schedules/cyclic-40e.py#L13-L33) 两个 `CosineAnnealingLR`：第一个 `begin=0, end=16, T_max=16, eta_min=lr*10` 负责升温段；第二个 `begin=16, end=40, T_max=24, eta_min=lr*1e-4` 负责退火段。注意第二个的 `T_max=24` 恰等于区间长度 `40-16`——`T_max` 必须与 `end-begin` 一致才能保证曲线在区间端点恰好到达 `eta_min`。

**配套动量调度**：

[configs/_base_/schedules/cyclic-40e.py:L34-L52](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/schedules/cyclic-40e.py#L34-L52) 两个 `CosineAnnealingMomentum` 与学习率镜像：学习率升时动量降、学习率降时动量升。AdamW 的动量（一阶矩系数）与学习率反向调节是大 batch 检测训练的常见搭配。

**训练轮数与验证间隔**：

[configs/_base_/schedules/cyclic-40e.py:L55-L61](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/schedules/cyclic-40e.py#L55-L61) `train_cfg = dict(by_epoch=True, max_epochs=40, val_interval=1)`：按 epoch 训练、最多 40 轮、每轮验证一次。注释点破一个易错点：由于该日程通常配合 `RepeatDataset` 使用（repeat 倍数为 N），**实际跑的 epoch 数是 N×40**——epoch 计数器数的是「被重复放大后的数据集」的轮数。`val_cfg = dict()` 与 `test_cfg = dict()` 留空 dict，由 Runner 补上默认的 `ValLoop`/`TestLoop` 类型。

**自动学习率缩放的基准**：

[configs/_base_/schedules/cyclic-40e.py:L63-L67](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/schedules/cyclic-40e.py#L63-L67) `auto_scale_lr = dict(enable=False, base_batch_size=48)` 即 8 卡 × 每卡 6 样本。这正是 4.1 中 `--auto-scale-lr` 校验的两个必需键；开启后若你实际全局 batch 是 12，学习率会按 48/12=4 缩放。

**子配置如何覆盖它**：PointPillars 的 KITTI 配置在继承 cyclic-40e 后整体重写了日程——

[configs/pointpillars/pointpillars_hv_secfpn_8xb6-160e_kitti-3d-3class.py:L85-L128](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/pointpillars/pointpillars_hv_secfpn_8xb6-160e_kitti-3d-3class.py#L85-L128) 把 `lr` 降为 0.001、`epoch_num=80`、梯度裁剪放宽到 `max_norm=35`、四个调度器区间改为 32/48 两段、`train_cfg.max_epochs=80, val_interval=2`。回忆 [u1-l5](u1-l5-config-system-basics.md) 的合并规则：`optim_wrapper` 整体是 dict 递归合并（`optimizer.lr` 覆盖而 `betas` 保留），`param_scheduler` 是 list 则**整体替换**为新的 4 段。

#### 4.2.4 代码实践

**实践目标**：把两段式 cyclic 调度「画」出来，确认你理解了 begin/end/eta_min/T_max 四个参数的几何含义。

**操作步骤**：

1. 纯手算：用 4.2.2 的余弦公式，取 `lr=0.0018`，分别计算第一段（`eta_min=0.018, T_max=16`）在 \( t=0, 4, 8, 16 \) 处与第二段（以 0.018 为起点、`eta_min=1.8e-7, T_max=24`）在 \( t'=8, 16, 24 \) 处的学习率。
2. 代码验证（示例代码，需本地已装 mmengine/torch）：

```python
# 示例代码：用 mmengine 调度器复现 cyclic 学习率曲线
import torch
from mmengine.optim import OptimWrapper, build_param_scheduler

model = torch.nn.Linear(4, 2)
optim_wrapper = OptimWrapper(torch.optim.AdamW(model.parameters(), lr=0.0018,
                                               betas=(0.95, 0.99)))
from mmengine.config import ConfigDict
lr = 0.0018
sched_cfgs = [
    ConfigDict(type='CosineAnnealingLR', T_max=16, eta_min=lr * 10,
               begin=0, end=16, by_epoch=True, convert_to_iter_based=True),
    ConfigDict(type='CosineAnnealingLR', T_max=24, eta_min=lr * 1e-4,
               begin=16, end=40, by_epoch=True, convert_to_iter_based=True),
]
schedulers = build_param_scheduler(sched_cfgs, optim_wrapper)
# 模拟推进 40 个 epoch，每个 epoch 假设 10 个 iter
for epoch in range(40):
    for it in range(10):
        for s in schedulers:
            s.step()          # 注意：iter 级推进的调用方式以本地 mmengine 文档为准
    print(epoch, optim_wrapper.param_groups[0]['lr'])
```

3. 对照手算值检查脚本输出是否先升后降。

**需要观察的现象**：学习率曲线先从 0.0018 平滑爬升到 0.018（epoch 16 附近），再下降到约 1.8e-7（epoch 39 附近）；曲线光滑无跳变（`convert_to_iter_based=True` 的效果）。

**预期结果**：手算第 1 段 \( t=8 \) 处 \( \eta = 0.018 + (0.0018-0.018)\cdot\frac{1+\cos(\pi/2)}{2} = 0.018 - 0.0162 \times 0.5 = 0.0099 \) 左右量级；脚本输出应与之同趋势。调度器的逐步调用接口在不同 mmengine 版本略有差异，**待本地验证**；若嫌麻烦，可在 4.4 的真实训练日志里直接观察 `lr:` 字段随 iteration 的连续变化，效果等价。

#### 4.2.5 小练习与答案

**练习 1**：为什么第二个 `CosineAnnealingLR` 的 `T_max=24` 而不是 40？

答案：`T_max` 是**本段区间内**余弦曲线的周期参数，必须等于区间长度 `end - begin = 40 - 16 = 24`，这样在区间末端 \( t = T_{\max} \) 时余弦项取 -1，学习率恰好到达 `eta_min`。写成 40 会让曲线在区间末尾只走完 24/40 的相位，到不了目标值。

**练习 2**：PointPillars 配置里 `train_cfg.max_epochs=80`，为什么大家说它训了 160 个 epoch？

答案：因为其 `train_dataloader.dataset` 是 `RepeatDataset(times=2)`（见 [configs/_base_/datasets/kitti-3d-3class.py:L108-L110](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/datasets/kitti-3d-3class.py#L108-L110)），每个「epoch」实际遍历两遍训练集，故真实数据轮数是 80×2=160；而 epoch 计数器、调度器、断点保存看到的都是 80。

**练习 3**：`clip_grad` 配在 `optim_wrapper` 里而不是 `optimizer` 里，反映了什么分工？

答案：梯度裁剪发生在「损失反传出梯度之后、优化器 step 之前」，这段流程由 `OptimWrapper.update_params` 统一编排；PyTorch 原生 optimizer 没有「step 前插钩子」的配置位，所以裁剪属于包装器职责（[configs/_base_/schedules/cyclic-40e.py:L8-L11](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/schedules/cyclic-40e.py#L8-L11)）。

### 4.3 default_runtime.py：hooks、环境与日志策略

#### 4.3.1 概念说明

`default_runtime.py` 是全库共享的「运行时环境」基座，几乎每个配置都继承它。它不描述模型也不描述数据，只描述**训练过程周边的事务**：默认挂哪些 hook、用什么分布式后端、日志多长打一次、初始权重从哪来。你可以把它理解成 Runner 的「出厂设置」。

#### 4.3.2 核心流程

Runner 初始化时会遍历 `default_hooks`（加上配置里可能的 `custom_hooks`），逐个经 HOOKS 注册节点构建并按优先级排序；训练循环推进到各时间点时，依次调用这些 hook 的同名方法。六个默认 hook 的分工：

| hook | 触发时机 | 职责 |
| --- | --- | --- |
| `IterTimerHook` | 每次 iter 前后 | 计时，产出日志中的 `time`（迭代总耗时）与 `data_time`（等数据耗时） |
| `LoggerHook` | 每次迭代/epoch 后（每 50 iter 汇总一次） | 汇总 loss、lr、耗时、显存并写日志，同时把标量写入 visualizer 的 vis_data |
| `ParamSchedulerHook` | 每次 iter/epoch 后 | 推进 4.2 的 param_scheduler（`step()`），学习率才会动 |
| `CheckpointHook` | 每个 epoch（或 iter）后 | 保存 checkpoint 到 work_dir 供断点续训 |
| `DistSamplerSeedHook` | 每个 epoch 前 | 多卡时改变分布式采样器种子，保证每轮 shuffle 不同 |
| `Det3DVisualizationHook` | 每次 val/test iter 后 | 按需绘制 3D 预测结果（默认关闭） |

#### 4.3.3 源码精读

**六个默认 hook**：

[configs/_base_/default_runtime.py:L3-L9](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/default_runtime.py#L3-L9) 一口气注册了上表的六个 hook。两个值得注意的取值：`logger=dict(type='LoggerHook', interval=50)` 表示**每 50 个 iteration 打一条训练日志**；`checkpoint=dict(type='CheckpointHook', interval=-1)` 表示默认**不按固定周期**保存 checkpoint（想要每 N 轮一存，需自行覆盖 interval，如 `--cfg-options default_hooks.checkpoint.interval=5`，具体保存行为以本地 mmengine 版本为准，本讲实践会直接观察它）。

**运行环境**：

[configs/_base_/default_runtime.py:L11-L15](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/default_runtime.py#L11-L15) `env_cfg` 三件事：`cudnn_benchmark=False`（输入尺寸固定时可改 True 换卷积提速）；`mp_cfg` 规定多进程启动方式为 `fork` 且 OpenCV 单线程（避免 dataloader worker 线程爆炸）；`dist_cfg=dict(backend='nccl')` 指定分布式通信后端（GPU 训练标配）。

**日志处理器**：

[configs/_base_/default_runtime.py:L17](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/default_runtime.py#L17) `log_processor = dict(type='LogProcessor', window_size=50, by_epoch=True)`：对耗时类指标做**窗口平滑**——日志里的 `time`/`data_time` 是最近 50 个 iter 的平均值而非瞬时值，这样偶发抖动不会污染日志。

**日志级别与权重初值**：

[configs/_base_/default_runtime.py:L19-L21](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/default_runtime.py#L19-L21) `log_level='INFO'`；`load_from=None`、`resume=False`——默认从头训练，这两项正是 4.1 中 `--resume` 改写的目标字段。

**default_scope 与注册器**：

[configs/_base_/default_runtime.py:L1](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/default_runtime.py#L1) `default_scope = 'mmdet3d'` 让 Runner 构建所有组件时默认在 mmdet3d 的注册节点下查找（回忆 [u4-l1](u4-l1-registry-mechanism.md) 的 scope 路由）。值得注意的是：六个 hook 里五个直接来自 mmengine，唯有 `Det3DVisualizationHook` 是 mmdet3d 自己的实现。

**看一个真 hook 的样子**：

[mmdet3d/engine/hooks/visualization_hook.py:L19-L20](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/engine/hooks/visualization_hook.py#L19-L20) 把 `Det3DVisualizationHook` 注册进 mmdet3d 的 HOOKS 节点（同名覆盖 mmengine/mmdet 的通用版本，正是 default_scope 机制下「同名不同库」的路由结果）。它的默认参数 `draw=False`（[L57](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/engine/hooks/visualization_hook.py#L57)）意味着**默认训练期间不画任何图**；只有当 [L97-L109](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/engine/hooks/visualization_hook.py#L97-L109) 的 `after_val_iter` 被触发且 `draw=True` 时，才按 `interval` 间隔取样本、重读点云/图像（[L118-L144](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/engine/hooks/visualization_hook.py#L118-L144)）并调用 visualizer 绘制。这个类展示了 hook 的标准写法：继承 mmengine `Hook`、实现 `after_val_iter`/`after_test_iter` 等生命周期方法、通过 `Visualizer.get_current_instance()` 拿到 Runner 构建的可视化器。更多可视化细节留给 [u7-l3](u7-l3-visualization.md)。

#### 4.3.4 代码实践

**实践目标**：验证「default_hooks 是可以被 `--cfg-options` 逐项覆盖的普通配置」，并体会日志密度由谁控制。

**操作步骤**（源码阅读 + 配置改写，可与 4.4 的实践合并执行）：

1. 数一数 [configs/_base_/default_runtime.py:L3-L9](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/default_runtime.py#L3-L9) 里的 hook 键，与 4.3.2 表格逐一对号。
2. 在 4.4 的训练命令后追加两项覆盖：
   `--cfg-options default_hooks.logger.interval=10 default_hooks.checkpoint.interval=1`。
3. 观察日志行间隔从 50 iter 变为 10 iter 一条。

**需要观察的现象**：日志条数约变为原来的 5 倍；`work_dir` 中出现（或增多）`.pth` 权重文件。

**预期结果**：与上述一致。具体 checkpoint 文件名（`epoch_N.pth` 等）与保存时机由 mmengine 的 `CheckpointHook` 决定，**待本地验证**——这正是本实践要观察的对象。

#### 4.3.5 小练习与答案

**练习 1**：日志里的 `time` 和 `data_time` 有什么区别？`data_time ≈ time` 说明什么？

答案：`time` 是一个训练迭代的总耗时（取数 + 前向 + 反向 + 更新），`data_time` 是其中花在等 dataloader 出 batch 上的时间，两者都是 `window_size=50` 的窗口平均。若 `data_time` 接近 `time`，说明瓶颈在数据侧（磁盘慢、`num_workers` 太小、管线太重——回忆单元 3 的 GT 采样等重增强），加大 `train_dataloader.num_workers` 或精简管线是第一选择。

**练习 2**：`default_runtime` 里 `checkpoint.interval=-1`，那训练产物里的 `.pth` 从哪来？

答案：mmengine 的 `CheckpointHook` 在 `interval<=0` 时不做周期性保存，但训练结束时会保存最终权重、并维护供 `--resume` 自动续训用的最新权重指针；此外用户随时可用 `--cfg-options default_hooks.checkpoint.interval=N` 恢复周期保存。具体行为建议像 4.3.4 那样实际观察（以本地 mmengine 版本为准）。

**练习 3**：为什么 `Det3DVisualizationHook` 默认 `draw=False`？

答案：验证集每次都要过几百上千帧，逐帧画 3D 框（重读点云 + 渲染 + 落盘）开销大且平时无人看图；默认关闭、需要调试时再经 `--cfg-options default_hooks.visualization.draw=True`（并配 `vis_task`）打开，是性能与可观测性的折中（见 [mmdet3d/engine/hooks/visualization_hook.py:L56-L67](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/engine/hooks/visualization_hook.py#L56-L67)）。

### 4.4 从 Runner.from_cfg 到 runner.train()：主循环与 work_dir 产物

#### 4.4.1 概念说明

`Runner` 是 mmengine 提供的通用执行器，mmdet3d 没有写自己的 Runner（projects 例外）。它做两件事：

1. **组装**：`Runner.from_cfg(cfg)` 把配置描述的每个零件从注册器里构建出来——模型（`cfg.model`，[u4-l1](u4-l1-registry-mechanism.md) 的递归工厂）、三个 dataloader 与评估器（[u3-l1](u3-l1-det3d-dataset-base.md) 的数据集 + 单元 3 的管线）、优化器包装器（绑定模型参数）与调度器（4.2）、hook 们与 visualizer（4.3），再按 `train_cfg/val_cfg/test_cfg` 选定循环类型。
2. **驱动**：`runner.train()` 跑主循环，在固定时间点触发 hook。

`train_cfg = dict(by_epoch=True, ...)`（无 `type` 键）时，Runner 默认选 `EpochBasedTrainLoop`：按 epoch 组织训练、每 `val_interval` 个 epoch 跑一次验证循环；若配置写 `by_epoch=False` 则换成 `IterBasedTrainLoop`。`val_cfg = dict()` 对应默认 `ValLoop`。

#### 4.4.2 核心流程

一次 `runner.train()` 的骨架（伪代码，mmengine 概念流程）：

```
Runner.from_cfg(cfg)
  ├─ 初始化 logger / work_dir（转储合并后的配置为 <配置名>.py）
  ├─ model        = MODELS.build(cfg.model)
  ├─ dataloaders  = train / val / test 三个 dataloader（含数据集与管线）
  ├─ evaluators   = val_evaluator / test_evaluator（如 KittiMetric）
  ├─ optim_wrapper   = 按 cfg.optim_wrapper 构建，绑定 model.parameters()
  ├─ param_schedulers = 按 cfg.param_scheduler 列表构建
  ├─ hooks        = default_hooks + custom_hooks，按优先级排序
  └─ train/val/test loop = 由 train_cfg/val_cfg/test_cfg 决定类型

runner.train()
  ├─ 处理 load_from / resume：加载权重或恢复完整训练状态
  ├─ call_hook('before_run')
  └─ for epoch in range(max_epochs):            # EpochBasedTrainLoop
       ├─ call_hook('before_train_epoch')       # 如 DistSamplerSeedHook 换种子
       ├─ for data_batch in train_dataloader:
       │    ├─ call_hook('before_train_iter')   # IterTimerHook 计时
       │    ├─ outputs = model.train_step(data_batch, optim_wrapper)
       │    │     ├─ data = data_preprocessor(data_batch)   # ← u4-l3 的预处理在这里
       │    │     ├─ losses = model.loss(**inputs, data_samples)  # ← u4-l2/u5 的 loss 模式
       │    │     └─ optim_wrapper.update_params(loss)       # 反传 + 裁剪 + step
       │    └─ call_hook('after_train_iter')    # ParamSchedulerHook 推进 lr
       │                                        # LoggerHook 记日志
       ├─ if (epoch+1) % val_interval == 0:
       │    跑 ValLoop：model.test_step 逐批预测 → Evaluator 汇总指标
       └─ call_hook('after_train_epoch')        # CheckpointHook 存权重
```

对照前序讲义：`data_batch` 是 pseudo_collate 出来的「样本 dict 列表」（[u3-l8](u3-l8-pack3d-det-inputs.md)），`data_preprocessor` 负责堆叠/体素化（[u4-l3](u4-l3-data-preprocessor-voxelize.md)），`model.loss` 是 [u4-l2](u4-l2-detector-class-hierarchy.md) 讲的三模式之一——至此，「数据管线 → 预处理器 → 模型 → 损失 → 优化器」的全链路在 Runner 里闭合成环。

训练日志的典型形态（mmengine 通用格式，字段名固定）：

```
Iter(train) [50/619]  lr: 9.9100e-04  eta: 0:12:34  time: 0.3121  data_time: 0.0402
    memory: 2048  loss: 2.8765  loss_cls: 1.2010  loss_bbox: 0.9121  loss_dir: 0.4233
```

- `lr`：当前 iteration 的学习率（cyclic + `convert_to_iter_based` 使其逐 iter 变化，见 4.2）；
- `eta`：按近期平均耗时估计的剩余时间；
- `time` / `data_time`：`IterTimerHook` 计时、`LogProcessor` 做 50-iter 窗口平滑（见 4.3）；
- `memory`：CUDA 显存峰值占用（MB）；
- `loss` 及各分项：`model.loss` 返回的损失字典经加权求和与逐项记录，PointPillars 的 `Anchor3DHead` 给出分类/回归/方向三个分项（[u5-l4](u5-l4-anchor3d-head.md)）。

`work_dir` 一次训练结束后的典型结构：

```
work_dirs/pointpillars_hv_secfpn_8xb6-160e_kitti-3d-3class/
├── pointpillars_hv_secfpn_8xb6-160e_kitti-3d-3class.py   # 转储的最终合并配置
├── 20260101_120000.log                                    # 训练日志（时间戳命名）
├── 20260101_120000/
│   └── vis_data/
│       └── scalars.json          # 逐条日志的标量记录（loss/lr/耗时），可视化数据源
└── epoch_XX.pth / latest.pth 等  # checkpoint（保存策略见 4.3）
```

其中 `vis_data` 目录来自配置里的可视化器：[configs/_base_/datasets/kitti-3d-3class.py:L165-L167](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/datasets/kitti-3d-3class.py#L165-L167) 声明 `Det3DLocalVisualizer` 挂载 `LocalVisBackend`，训练标量经 LoggerHook 写入其 `scalars.json`——本单元后面的分析工具（如 `analyze_logs.py`，见 [u7-l5](u7-l5-analysis-tools.md)）吃的就是这个文件。

#### 4.4.3 源码精读

本节的主循环代码在 mmengine 中，mmdet3d 侧的锚点有三个。

**入口两行**：

[tools/train.py:L131-L141](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/train.py#L131-L141) `runner_type` 分支 + `runner.train()`，前面已精读，这里强调：mmdet3d 对训练流程的定制（如 3D 特有的 hook、可视化器、评估器）**全部通过配置注入**，而不是改写 Runner——这正是「薄入口 + 配置驱动」架构的意图。

**dataloader 的组装原料**：

[configs/_base_/datasets/kitti-3d-3class.py:L103-L123](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/datasets/kitti-3d-3class.py#L103-L123) `train_dataloader` 四要素：`batch_size=6`（对应配置名里的 `8xb6`——8 卡每卡 6 样本）、`num_workers=4`、`DefaultSampler(shuffle=True)`、内嵌 `RepeatDataset(times=2)` 包装的 `KittiDataset` 与训练管线。Runner 据此构建 dataloader；4.4.2 伪代码中每个 `data_batch` 就是从它取出的。

**验证评估器**：

[configs/_base_/datasets/kitti-3d-3class.py:L158-L163](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/datasets/kitti-3d-3class.py#L158-L163) `val_evaluator = dict(type='KittiMetric', ann_file=..., metric='bbox')`，训练中每 `val_interval` 个 epoch 被 ValLoop 驱动一次，产出 mAP 打进日志（指标计算细节是下一讲 [u7-l2](u7-l2-testing-and-metrics.md) 的主题）。

**单卡与多卡启动**：

[tools/dist_train.sh:L10-L19](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/dist_train.sh#L10-L19) 多卡训练只是用 `torch.distributed.launch` 拉起 N 个 `train.py` 进程并统一传 `--launcher pytorch`——多卡与单卡跑的是**同一个入口脚本**，差异全部被 `cfg.launcher` 与 Runner 的分布式初始化吸收。官方文档 [docs/en/user_guides/train_test.md:L150-L196](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docs/en/user_guides/train_test.md#L150-L196) 给出了单卡、多卡、Slurm 集群三种启动方式；同机多任务需错开通信端口（`PORT=29500/29501`，文档 L216-L224）。

**学习率的线性缩放提醒**：

[docs/en/user_guides/train_test.md:L147-L148](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docs/en/user_guides/train_test.md#L147-L148) 官方明确提示：配置默认学习率按 8 卡调好，改卡数/批大小理论上应按线性缩放规则调整，但由于多数 3D 模型用 Adam/AdamW，该规则未必成立，需要自行调参——这正呼应 4.2 的 `auto_scale_lr` 与 4.1 的 `--auto-scale-lr`。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：用 KITTI PointPillars 配置完成一次 1 epoch 的最小化训练，摸清 `work_dir` 三类产物（log / checkpoint / vis_data），并解释日志中 `time` 与 `data_time` 的含义。

**操作步骤**：

1. 前提：按 [u3-l2](u3-l2-data-preparation-info-pkl.md) 完成 KITTI 数据准备（`kitti_infos_train.pkl`、GT 数据库、`velodyne_reduced`），并符号链接到 `data/kitti/`；确认 GPU 环境可用（点云 3D 算子依赖 CUDA，无法 CPU 训练，见 [docs/en/user_guides/train_test.md:L168-L170](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/docs/en/user_guides/train_test.md#L168-L170)）。
2. 启动最小化训练（缩轮数、缩验证、取消 RepeatDataset 翻倍、缩 batch）：

```bash
python tools/train.py \
    configs/pointpillars/pointpillars_hv_secfpn_8xb6-160e_kitti-3d-3class.py \
    --work-dir work_dirs/pp_kitti_mini \
    --cfg-options \
        train_cfg.max_epochs=1 \
        train_cfg.val_interval=1 \
        train_dataloader.batch_size=4 \
        train_dataloader.num_workers=2 \
        train_dataloader.dataset.times=1 \
        default_hooks.logger.interval=10 \
        default_hooks.checkpoint.interval=1
```

3. 训练期间观察终端滚动的 `Iter(train) [x/N]` 日志行。
4. 训练结束后检查产物：

```bash
find work_dirs/pp_kitti_mini -maxdepth 3 | sort
```

5. 打开时间戳 `.log` 文件，找到任意一行训练日志，抄下 `time`、`data_time`、`memory`、`loss` 四个值；再打开 `<时间戳>/vis_data/scalars.json` 看前几条记录。

**需要观察的现象**：

- 终端与 `.log` 中每 10 个 iteration 出现一条 `Iter(train)` 行（因为覆盖了 `logger.interval`）；`lr` 字段随 iteration **连续变化**（cyclic 第一段 + `convert_to_iter_based` 的效果）；loss 行包含 `loss/loss_cls/loss_bbox/loss_dir` 四个值。
- epoch 结束后进入验证，日志出现 KittiMetric 的 mAP 输出。
- `work_dir` 结构与 4.4.2 的树一致：转储配置 `.py`、时间戳 `.log`、`<时间戳>/vis_data/scalars.json`、`.pth` 权重。
- `data_time` 明显小于 `time`（数据加载不是瓶颈）；若两者接近，尝试把 `num_workers` 调到 4 再跑一次对比。

**预期结果**：KITTI 训练集约 3700 帧、`times=1` 且 `batch_size=4` 时一个 epoch 约 900+ 个 iteration（以你 info pkl 的实际帧数为准）。日志字段含义对号入座：`time` 为单迭代总耗时（窗口平均）、`data_time` 为其中等待数据的时间。`scalars.json` 中每条记录含 loss 与 lr 等键。checkpoint 的具体文件数量与命名依赖 mmengine 版本，**待本地验证**——把你观察到的实际清单与 4.3.5 练习 2 的讨论对照。

#### 4.4.5 小练习与答案

**练习 1**：训练日志中 `memory: 2048` 是什么？为什么它常常只增不减？

答案：它是 CUDA 显存峰值的 MB 数（按已分配显存统计）。PyTorch 的显存缓存机制倾向保留已申请的块以便复用，且统计口径是峰值，所以该值在训练中基本单调不降——排查显存溢出（OOM）时应看它而非瞬时占用。

**练习 2**：为什么 `train_dataloader.dataset.times=1` 能把训练时间减半？它改的是哪个组件？

答案：它改的是 `RepeatDataset` 包装器的重复倍数（[configs/_base_/datasets/kitti-3d-3class.py:L108-L110](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/configs/_base_/datasets/kitti-3d-3class.py#L108-L110)），把「每个 epoch 遍历两遍数据集」变为一遍，iteration 数与墙钟时间都减半；注意这同时改变了调度器曲线在每个 epoch 内的推进速度（epoch 粒度的分段不变，但每段内的 iter 数变了），做对比实验时要意识到这一副作用。

**练习 3**：如果不小心把训练中断了，如何从断点继续而不丢训练状态？

答案：用完全相同的命令追加 `--resume`（不带值）。它会设置 `cfg.resume=True` 并让 Runner 从 `work_dir` 中最新的 checkpoint 恢复模型权重、优化器状态、调度器进度与 epoch 计数（[tools/train.py:L123-L129](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/tools/train.py#L123-L129)）。前提是之前开启了周期保存 checkpoint（如本实践中的 `default_hooks.checkpoint.interval=1`）。

## 5. 综合实践

**任务：给你的训练装上「仪表盘」，并解释每个读数的来源。**

把 4.4 的最小训练扩展成一次小型对照实验，把本讲四个模块串起来：

1. **基线跑**：按 4.4.4 命令原样训练 1 epoch。
2. **变体跑**：换 `--work-dir work_dirs/pp_kitti_amp`，追加 `--amp`，其余不变。
3. **读表**：从两个 `work_dir` 的 `.log` 中各取同一段 iteration（如 100-200），对比 `time`、`memory`、`loss` 三项——AMP 应显著降低 `memory`，`time` 视卡而定，loss 早期量级略有差异属正常（fp16 数值噪声）。
4. **读曲线**：解析基线跑的 `scalars.json`（示例代码）：

```python
# 示例代码：直接消费 vis_data/scalars.json
import json
from pathlib import Path

f = next(Path('work_dirs/pp_kitti_mini').glob('*/vis_data/scalars.json'))
records = [json.loads(line) for line in f.open() if line.strip()]
for r in records[:3] + records[-3:]:
    print({k: r[k] for k in r if 'loss' in k or k == 'lr' or k == 'step'})
```

5. **解释**：写一段话说明 `lr` 曲线为何在单个 epoch 内连续爬升（4.2 两段式调度 + `convert_to_iter_based=True`），`loss` 各分项分别来自模型的哪个分支（[u5-l4](u5-l4-anchor3d-head.md)），`time/data_time` 由哪个 hook 计时、经哪个处理器平滑（4.3），最终这些数字如何被 LoggerHook 同时写进终端、`.log` 与 `scalars.json`。

预期：你能完整回答「一条训练日志是怎么产生的」——这标志着你已经把配置、hook、Runner 主循环与模型损失四层关系打通。对照跑的结果与显存/速度差值**待本地验证**（取决于显卡型号与 mmengine/mmcv 版本）。

## 6. 本讲小结

- `tools/train.py` 是薄入口：解析参数 → `Config.fromfile` → 按 CLI 改写 cfg（work_dir/amp/sync_bn/auto_scale_lr/resume）→ `Runner.from_cfg` → `runner.train()`，五步走完，训练逻辑全在引擎与配置里。
- `--amp` 通过改 `optim_wrapper.type` 为 `AmpOptimWrapper` 实现混合精度；`--resume` 有「自动找最新」与「指定路径」双语义，且会覆盖配置里的 `load_from`；`--cfg-options` 经 `merge_from_dict` 深度合并进配置。
- `cyclic-40e.py` 用「两段余弦拼接」同时调度学习率（升 10 倍再降 4 个数量级）与动量；`T_max` 必须等于 `end-begin`；`convert_to_iter_based=True` 使曲线按 iteration 连续变化；RepeatDataset 会让真实数据轮数等于 `max_epochs × times`。
- `default_runtime.py` 贡献 6 个默认 hook（计时/日志/调度器推进/存档/分布式种子/3D 可视化）、`env_cfg` 运行环境与 50-iter 窗口平滑的 `LogProcessor`；它们全部可被 `--cfg-options` 逐项覆盖。
- Runner 主循环每个 iteration 执行「hook 计时 → `model.train_step`（内含 [u4-l3](u4-l3-data-preprocessor-voxelize.md) 的 data_preprocessor 与模型 loss 模式）→ hook 推进调度器与记日志」；每 `val_interval` 个 epoch 跑一次 ValLoop 出 mAP。
- `work_dir` 三类产物各司其职：时间戳 `.log`（人读）、`<时间戳>/vis_data/scalars.json`（机器读，分析工具的数据源）、`.pth` checkpoint（断点续训与推理）；日志中 `time` 是迭代总耗时、`data_time` 是等数据耗时，二者比值是判断「数据瓶颈还是算力瓶颈」的第一依据。

## 7. 下一步学习建议

- **下一讲 [u7-l2](u7-l2-testing-and-metrics.md)**：`tools/test.py` 与评估指标体系——本讲训练出的 checkpoint 将作为它的输入，重点读 `KittiMetric` 与 `NuScenesMetric` 的 `process → compute_metrics` 链路，理解训练日志末尾那几行 mAP 是怎么算出来的。
- 顺着本讲的 hook 机制往下读：[mmdet3d/engine/hooks/](https://github.com/open-mmlab/mmdetection3d/blob/fe25f7a51d36e3702f961e198894580d83c4387b/mmdet3d/engine/hooks/) 目录里还有 `Det3DVisualizationHook` 的同伴，配合 [u7-l3](u7-l3-visualization.md) 一起看。
- 想吃透引擎本身，在你本地环境打开 mmengine 源码的 `runner/runner.py`（`train()`、`Runner.from_cfg`）与 `hooks/` 目录，对照本讲 4.4.2 的伪代码逐行验证——本讲刻意只讲了行为契约，源码级细节值得亲手走一遍。
- 学有余力可以预习 `tools/analysis_tools/analyze_logs.py`（[u7-l5](u7-l5-analysis-tools.md)），它正是消费本讲 `vis_data/scalars.json` 画 loss 曲线的官方工具。
