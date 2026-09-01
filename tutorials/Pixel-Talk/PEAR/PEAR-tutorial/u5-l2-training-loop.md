# 训练主循环：OurPipeline 与 Lightning Fabric DDP

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `train_ehms.py` 的 `train()` 函数按什么顺序组装一次训练：配置 → 随机种子 → 设备 → 数据集 → DataLoader → 输出目录 → 模型 → 断点权重 → 主循环。
2. 解释 `OurPipeline.__init__` 如何用 `lightning.Fabric` + `DDPStrategy` 完成设备搬运、DDP 包装和 DataLoader 的分布式改造，以及为什么 optimizer 必须在 `fabric.setup()` 之前创建。
3. 逐步走读 `run_fit` 的单步训练：前向 → EHM 网格重建 → 关键点投影 → 五项损失 → `fabric.backward` 反向 → `optimizer.step`。
4. 说明 checkpoint 里保存了什么（backbone / head / meta_cfg / global_iter）、在哪里保存、以及如何用 `--ehm_model` 恢复。
5. 识别本模块的几处「死配置」与遗留代码：`TRAIN.check_interval` 与 `self.loss_weight` 被读取但从未消费、`forward()` 是迁移残留的僵尸方法——这也是研究代码阅读训练中最重要的免疫力。

## 2. 前置知识

### 2.1 训练循环与推理循环的差别

前面单元里我们反复走的是推理链路：图像进去、参数出来、渲染落盘，全程 `torch.no_grad()` 都不需要——因为根本不反传。训练循环在「前向」之外多了三件事：

1. **算损失**：用一个可微分的标量度量「预测离标注有多远」。
2. **反向传播**：调用 `loss.backward()`，PyTorch 自动求出损失对每个可学习参数的梯度。
3. **优化器更新**：`optimizer.step()` 沿梯度反方向更新参数，然后 `zero_grad()` 清空梯度，进入下一轮。

写成伪代码就是：

```text
for iter in range(total_iters):
    batch = next(dataloader)
    outputs = model(batch)            # 前向
    loss = loss_fn(outputs, batch)    # 损失
    optimizer.zero_grad()             # 清梯度
    loss.backward()                   # 反向
    optimizer.step()                  # 更新
```

本讲的 `run_fit` 就是这段伪代码的工业版：多了 epoch 耗尽后的迭代器重建、TensorBoard 记录、周期性可视化 / 验证 / 存档。

### 2.2 Lightning Fabric 是什么，为什么不用 Lightning Trainer

[PyTorch Lightning](https://lightning.ai/docs/pytorch/stable/) 有两层 API：

- **Trainer 模式**：你把模型写成 `LightningModule`（实现 `training_step` 等钩子），交给 `Trainer.fit()`，框架接管整个循环。省心但控制粒度粗。
- **Fabric 模式**：只借它的**分布式脚手架**——设备搬运、DDP 包装、梯度 all-reduce、checkpoint 聚合——循环仍然由你手写。

PEAR 选了 Fabric。原因注释里写得很直白：[models/pipeline/pipeline.py:122](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/pipeline.py#L122) `# Manually control the optimization since we have an adversarial process.`（历史上曾有对抗训练过程，需要手动控制优化节奏。）于是 `OurPipeline` 是一个**普通 Python 类**，不是 `LightningModule`——第 1 行 `import pytorch_lightning as pl` 只在 `print_summary` 里用到。

### 2.3 DDP 数据并行最小科普

DDP（DistributedDataParallel）是最常用的多卡训练方式：

- 每张卡各持有一份完整模型副本，各自吃不同的小批量数据，各自前向反传；
- 反传结束时 DDP 自动做一次 **梯度 all-reduce**，把各卡的梯度平均后再更新，从而所有卡的参数保持一致：

\[
\nabla \theta_{\text{global}} = \frac{1}{N}\sum_{i=1}^{N} \nabla \theta_i
\]

- `find_unused_parameters=True`（[pipeline.py:73](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/pipeline.py#L73)）允许某些参数在某次前向中不参与计算图。PEAR 确实有这类参数（u3-l3 提过 `joint` 解码器构造而未被调用），所以这个开关是必要的，代价是每次前向后要多做一次图遍历、略降速度。

一个对单卡用户重要的细节：即使 `-d 0` 只有一张卡，`DDPStrategy` 依然会初始化进程组并把模型包进 DDP。这就是为什么 `run_fit` 里可以放心调用 `dist.get_rank()`（[pipeline.py:323](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/pipeline.py#L323)）——分布式环境总是已就绪。

### 2.4 与前面讲义的衔接

- **u5-l1** 讲清了 batch 里每个字段的来源与形状（`ehm_image`、`dwpose_kp2d`、`smpl_kp2d`、`smpl_kp3d`、`smplx_coeffs`、`flame_coeffs`、`smpl_kp`）。本讲把这些字段当作损失函数的输入契约直接使用。
- **u3-l3 / u3-l4** 讲清了 head 输出的 `body_param`（11 键）、`flame_param`（6 键）与 `pd_cam`（(B,4,4) RT 矩阵）。本讲把它们喂进损失。
- **u4-l4** 讲清了 `EHM_v2` 把参数变成 10595 顶点 / 145 关节。本讲中它处于**前向计算图内部**——虽然自身零可学习参数，但梯度要穿过它流回 head 与 backbone，所以它必须待在 GPU 上参与 autograd。
- **u2-l1** 讲清了 `ConfigDict` / `add_extra_cfgs` / `device_parser`。本讲直接复用。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [train_ehms.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/train_ehms.py) | 训练入口脚本：装配配置、数据、模型，触发 `run_fit`；也负责断点权重加载 |
| [models/pipeline/pipeline.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/pipeline.py) | `OurPipeline` 类：Fabric/DDP 装配、`run_fit` 主循环、`save_checkpoints`、`run_val` |
| [configs/train.yaml](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/configs/train.yaml) | 训练配置：`TRAIN` 段（batch_size / train_iter / check_interval）、`DATASET` 段（tar 分片与权重） |
| [dataset/webdata_loader.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/dataset/webdata_loader.py) | `build_web_tracked_data`（u5-l1 已精读，本讲只作为装配的一环引用） |
| [utils/general_utils.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/general_utils.py) | `ConfigDict`、`device_parser`、`rtqdm`（rich 版 tqdm）、`calc_parameters` |
| [models/pipeline/loss.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/loss.py) | 各损失类实现（下一讲 u5-l3 的主角，本讲只看调用方式） |

**训练相对推理额外需要的磁盘资产**（u1-l2 说过「assets/SMPL 与 SMPLX2SMPL 仅训练链路使用」，本讲正是消费它们的地方）：

- `data_inputs/backbone/vitpose_backbone.pth`：ViTPose 预训练骨干（`_init_backbone` 用）；
- `assets/SMPLX2SMPL/body_models/smplx2smpl.pkl` 与 `assets/SMPLX2SMPL/SMPL_to_J19.pkl`：SMPL-X 顶点 → SMPL 44 关节的转换矩阵与额外回归器；
- `assets/SMPL/SMPL_NEUTRAL.pkl`：SMPL 中性模型；
- `ehm_datasets/000000.tar`：README 提供的示例训练分片（train.yaml 中 train 与 valid 默认都指向它）。

## 4. 核心概念与源码讲解

### 4.1 `train_ehms.train` 入口：装配顺序与断点恢复

#### 4.1.1 概念说明

入口脚本的职责是「把一次训练需要的所有零件按依赖顺序摆好，最后按下启动键」。依赖顺序很关键：DataLoader 要在模型之前建好（`OurPipeline` 构造时要接管它们），输出目录要在写入任何文件之前存在，断点权重要在 `run_fit` 之前加载进模型。`train()` 函数就是这份顺序清单的代码化。

#### 4.1.2 核心流程

```text
train(config_name, ehm_basemodel, devices)
├─ 1. meta_cfg = ConfigDict(configs/<config_name>.yaml) + add_extra_cfgs
├─ 2. lightning.fabric.seed_everything(10)      # 固定全局随机种子
├─ 3. target_devices = device_parser(devices)   # '0' / '0,2-3' / 'cpu' → 设备列表
├─ 4. train_dataset / val_dataset = build_web_tracked_data(...)  # u5-l1 的数据管线
├─ 5. timestamp = now()%Y%m%d_%H；outputs/<timestamp>/ 建立，并备份 config.yaml
├─ 6. train/val DataLoader（batch_size=cfg.TRAIN.batch_size，num_workers=1）
├─ 7. ehm_model = OurPipeline(meta_cfg, train_dl, val_dl, devices)
├─ 8. 若指定 --ehm_model：加载 backbone/head 两段权重（断点微调入口）
└─ 9. ehm_model.run_fit()                       # 进入主循环
```

注意第 5 步和第 7 步**各算了一次时间戳**：`train()` 用 `datetime.now().strftime("%Y%m%d_%H")`（[train_ehms.py:49](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/train_ehms.py#L49)），`OurPipeline.__init__` 内部又算了一次（[pipeline.py:48-49](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/pipeline.py#L48-L49)）。两次调用若跨过整点，config 备份和 writers/checkpoint 会落在**两个不同的** `outputs/<时间戳>/` 目录里——训练时间长了必然遇到，找文件时要有心理准备。

#### 4.1.3 源码精读

**配置与全局状态。** [train_ehms.py:28-39](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/train_ehms.py#L28-L39)：`train()` 先读 YAML 成 `ConfigDict` 并补 `add_extra_cfgs`，然后 `seed_everything(10)` 固定随机种子保证可复现，`device_parser` 把命令行的 `-d` 参数解析成设备列表（u2-l1 已验证 `'0,2-3'` → `[0, 2, 3]`）。第 38 行的 `init_iter = 1` 是个从未被使用的局部变量（`run_fit` 用自己的默认参数 `init_iter=0`），可以忽略。

**数据集与 DataLoader。** [train_ehms.py:45-56](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/train_ehms.py#L45-L56)：训练 / 验证集分别用 `split='train'` / `split='valid'` 构建（读的是 train.yaml 的 `datasets` 与 `val_datasets` 两段）。两个 DataLoader 的 `shuffle` 都被注释掉了——这不是 bug：u5-l1 讲过 WebDataset 管线内部已经做了 `shuffle(1000)` 样本级打乱，外层再 shuffle 反而多余。`num_workers=1` 意味着数据解码与 GPU 计算只有很有限的预取并行，是训练提速的明显候选点。

**输出目录与配置备份。** [train_ehms.py:58-60](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/train_ehms.py#L58-L60)：建 `outputs/<时间戳>/` 并把本次用的 YAML 原样拷贝为 `config.yaml`。这是复现实验的最低要求——配合 checkpoint 里的 `meta_cfg` 双保险。

**模型装配与断点恢复。** [train_ehms.py:62-72](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/train_ehms.py#L62-L72)：构造 `OurPipeline` 后，如果命令行给了 `--ehm_model`，就 `torch.load(..., weights_only=True)` 加载一份旧 checkpoint，把其中 `backbone`、`head` 两段分别 `load_state_dict(..., strict=False)` 灌进模型，最后调 `run_fit()`。这正是官方发布的 `pear_model.pt` 的消费方式（u2-l5 已确认其顶层就是 backbone / head 两段 state dict）——所以这个参数既是「从官方权重继续微调」的入口，也是「从自己存的 checkpoint 续训」的入口。

**命令行入口。** [train_ehms.py:85-94](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/train_ehms.py#L85-L94)：四个参数——`-c/--config_name`（对应 `configs/<name>.yaml`，必填）、`-d/--devices`（默认 `'0'`）、`--ehm_model`（默认 None）、`--debug`（store_true）。最后一行 `torch.set_float32_matmul_precision('high')` 允许 PyTorch 在 Ampere 以上显卡用 TF32/BF16 加速矩阵乘，是免费的小提速。

#### 4.1.4 代码实践

**实践目标**：不动 GPU、不下数据，先做一次「装配流程静态走查 + CLI 确认」。

**操作步骤**：

1. 在仓库根目录运行 `python train_ehms.py --help`，确认四个参数与上表一致。
2. 打开 `train_ehms.py`，对照 4.1.2 的伪代码，在纸上给 [train_ehms.py:28-72](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/train_ehms.py#L28-L72) 每一行标注它对应伪代码的哪一步。
3. 单独验证设备解析：

```python
# 示例代码：验证 device_parser
from utils.general_utils import device_parser
print(device_parser('0'))        # 期望 [0]
print(device_parser('0,2-3'))    # 期望 [0, 2, 3]
print(device_parser('cpu'))      # 期望 ['cpu']
```

**需要观察的现象**：三个输出与注释里的期望一致。

**预期结果**：一致即通过；若 `cpu` 分支返回 `['cpu']` 之外的形态，回头细读 [utils/general_utils.py:271-284](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/general_utils.py#L271-L284)（注意该函数在文件里定义了两次，256 行与 271 行各一份，后者覆盖前者，两份实现完全相同——又一个研究代码痕迹）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `train()` 里 DataLoader 的 `shuffle=True` 被注释掉是合理的？

**答案**：训练集是 WebDataset 流式管线（u5-l1），管线内部已有 `shuffle(1000)` 做样本级打乱，且 `build_web_tracked_data` 用 `RandomMix` 按权重混采多个 tar 分片；外层 DataLoader 的 shuffle 只能打乱「批次到达顺序」，对流式迭代器意义不大。另外后面 `fabric.setup_dataloaders(use_distributed_sampler=True)` 会接管采样器，此时显式 shuffle 反而可能冲突。

**练习 2**：`--ehm_model` 加载权重时为什么对 backbone 和 head **分别**调用 `load_state_dict`，而不是整个模型一次加载？

**答案**：因为 PEAR 的可学习部分只有 backbone（ViT）和 head（SMPLXTransformerDecoderHead）两段，checkpoint 顶层就是这两段 state dict（`'backbone'`、`'head'` 两个键）；`OurPipeline` 又不是 `nn.Module`（普通类），没有整体的 `state_dict` 可言。`strict=False` 则容忍键不完全匹配（如 head 结构微调后继续用旧骨干）。

**练习 3**：`torch.set_float32_matmul_precision('high')` 大概会影响什么？

**答案**：它允许 PyTorch 在支持 Tensor Core 的 GPU 上用降低精度的矩阵乘（如 TF32）替换 FP32 矩阵乘，通常带来明显提速、精度损失极小；对 ViT-Huge 这种矩阵乘占比极高的骨干收益最大。它不影响加法、逐元素运算等非 matmul 路径。

---

### 4.2 `OurPipeline.__init__`：Fabric/DDP 装配与训练专属资产

#### 4.2.1 概念说明

`OurPipeline.__init__` 是整个训练的「总装车间」。它做四类事：

1. **建可学习部件**：backbone、head、optimizer——全部在 CPU 上、Fabric 介入之前完成；
2. **加载 ViTPose 预训练**：训练不是从零开始，骨干先吃 ViTPose 姿态预训练权重；
3. **Fabric 接管**：创建 Fabric → `launch()` → `setup()` 模型与优化器 → `setup_dataloaders()` 改造数据加载；
4. **搬训练专属资产上 GPU**：EHM_v2、SMPL 转换矩阵、渲染器、相机、损失层。

这里的顺序纪律是 Fabric 的核心约定：**optimizer 必须在模块 `setup()` 之前创建**（这样 Fabric 才能把参数搬家与优化器状态正确关联），**权重加载要在 `setup()` 之前做**（在 CPU 上加载更稳，也避免 DDP 包装后键名带 `module.` 前缀的麻烦）。

#### 4.2.2 核心流程

```text
__init__(cfg, train_dl, val_dl, devices)
├─ A. 可学习部件（CPU）
│    ├─ self.backbone = ViT(**cfg.BACKBONE)          # 6.3 亿参数，u3-l1
│    ├─ self.head    = SMPLXTransformerDecoderHead() # u3-l3
│    ├─ self.optimizer = configure_optimizers()       # AdamW(lr=1e-5, wd=1e-4)
│    └─ _init_backbone()                              # 灌 ViTPose 预训练
├─ B. Fabric 接管
│    ├─ Fabric(accelerator='cuda', DDPStrategy(find_unused_parameters=True), devices)
│    ├─ fabric.launch()
│    ├─ backbone, optimizer = fabric.setup(backbone, optimizer)  # 搬 GPU + DDP 包装
│    ├─ head = fabric.setup(head)
│    └─ train_dl, val_dl = fabric.setup_dataloaders(..., use_distributed_sampler=True)
└─ C. 训练专属资产 → fabric.device
     ├─ GS_Camera（焦距 24、画布 1024，与推理侧常数一致，u3-l4）
     ├─ EHM_v2("assets/FLAME", "assets/SMPLX")        # 零可学习参数但必须在图内
     ├─ smplx2smpl 矩阵 + SMPL 模型 + J_regressor_extra
     ├─ BodyRenderer（可视化用渲染器）
     └─ 六个损失层 + L1 metric
```

#### 4.2.3 源码精读

**可学习部件与优化器。** [pipeline.py:48-59](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/pipeline.py#L48-L59)：先记录输出目录与迭代节奏（`_total_iters = cfg.TRAIN.train_iter`，train.yaml 里是 200000），然后按 backbone → head → optimizer 的顺序构建。优化器在 [pipeline.py:231-237](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/pipeline.py#L231-L237)：单个 `torch.optim.AdamW(lr=1e-05, weight_decay=0.0001)`，参数来自 `_params_main()`（[pipeline.py:529-530](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/pipeline.py#L529-L530)，head + backbone 的全部 `requires_grad` 参数）。**注意学习率是硬编码的 1e-5**——train.yaml 里那个 `OPTIMIZE.learning_rate: 1.0e-4` 段在本文件中没有任何消费者，属于高斯泼溅旧管线遗留（同段里 `lambda_l1`、`lambda_ssim` 等也全是渲染侧词汇）。想改学习率要改代码，不是改 YAML。

**ViTPose 预训练加载。** [pipeline.py:67-69](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/pipeline.py#L67-L69) 调用 `_init_backbone`，后者在 [pipeline.py:560-563](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/pipeline.py#L560-L563)：读取 `cfg.BACKBONE.backbone_ckpt`（train.yaml 中为 `data_inputs/backbone/vitpose_backbone.pth`），`torch.load(...)['state_dict']` 取出权重后 `load_state_dict(state_dict)`——**没有传 `strict=False`，即 strict 默认 True**，缺键多键都会直接报错。u3-l1 说过 `ViT.__init__` 自己接收但忽略 `backbone_ckpt` 形参（[models/backbones/vit.py:205](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/backbones/vit.py#L205)），真正消费这个路径的是这里的 `_init_backbone`。

**Fabric 创建与 setup。** [pipeline.py:72-82](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/pipeline.py#L72-L82)：`lightning.Fabric(accelerator='cuda', strategy=DDPStrategy(find_unused_parameters=True), devices=devices)` 然后 `launch()`。接着 `setup(backbone, optimizer)` 把两者一起搬上 GPU 并把 backbone 包进 DDP；head 单独 setup。两个 DataLoader 用 `setup_dataloaders(use_distributed_sampler=True)` 接管——多卡时每张卡只看到全局 batch 的一部分（DDP 语义），单卡时行为基本不变。setup 之后，`self.backbone` 已经是 DDP 包装后的对象，但调用方式不变（DDP 是透明的 `nn.Module` 代理）。

**相机与 EHM。** [pipeline.py:85-92](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/pipeline.py#L85-L92)：`build_cameras_kwargs(batch_size, focal_length=24)`（实现见 [pipeline.py:207-214](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/pipeline.py#L207-L214)，主点为零、画布 `body_image_size=1024`）构造 `GS_Camera`——焦距 24 与画布 1024 正是 u3-l4 / u4-l5 反复强调的全仓常数，训练侧同样遵守。`EHM_v2` 从本地资产构造后 `.to(fabric.device)`：它没有任何可学习参数（u4-l4），但它出现在 `run_fit` 的前向链路里，梯度必须**穿过**它流回 head，所以它必须与计算图同设备。

**SMPL 转换资产。** [pipeline.py:96-100](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/pipeline.py#L96-L100)：加载 `smplx2smpl.pkl` 的回归矩阵（`(1, 6890, 10475)` 形状的映射）、`SMPL_NEUTRAL.pkl` 中性模型与 `SMPL_to_J19.pkl` 额外回归器。这三件资产只在训练损失里用（把 SMPL-X 顶点转成 SMPL 44 关节去对齐标注），推理链路完全不需要——u1-l2 的结论在这里落地。

**损失层与遗留标志。** [pipeline.py:104-124](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/pipeline.py#L104-L124)：实例化 `BodyParameterLoss`、`HeadParameterLoss`、`CameraLoss`、`Keypoint3DLoss(l1)`、`Keypoint2DLoss(l1)`、`ParameterLoss` 与 `torch.nn.L1Loss(reduction='sum')`（即代码里的 `self.metric`）。第 123 行 `self.automatic_optimization = False` 是 LightningModule 的属性约定，写在普通类上没有任何效果——同理还有 `set_data_adaption()`（第 124 行）设置的 `self.adapt_batch`，它的唯一消费者是下面这个**从不被调用的方法**。

> **僵尸方法警示**：[pipeline.py:133-174](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/pipeline.py#L133-L174) 的 `forward()` 引用了 `self.skel_model`、`self.cfg.policy.img_patch_size` 等**本类中根本不存在**的属性——一旦调用必然 `AttributeError`。它是从 HSMR 风格代码库迁移时的残留（注释里的输出形状 `(B, Q=44, 3)`、`poses (B, 46)`、`betas (B, 10)` 全是 SKEL 模型的词汇）。真正的前向是 [pipeline.py:176-203](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/pipeline.py#L176-L203) 的 `forward_step`：normalize → `x[:,:,:,32:-32]` 裁剪 → backbone → head，与推理侧 `Ehm_Pipeline.forward`（u2-l5）完全同构。**读研究代码时，永远以「被谁调用」为准，而不是「长得像入口」。**

#### 4.2.4 代码实践

**实践目标**：不启动训练，完成一次「训练装配清单」静态盘点，确认本机缺哪些训练资产。

**操作步骤**：在仓库根目录运行以下脚本（CPU 即可）：

```python
# 示例代码：训练装配静态检查
import os
from utils.general_utils import ConfigDict, add_extra_cfgs, device_parser

meta_cfg = add_extra_cfgs(ConfigDict(model_config_path='configs/train.yaml'))
print('batch_size =', meta_cfg.TRAIN.batch_size)
print('train_iter =', meta_cfg.TRAIN.train_iter)
print('check_interval =', meta_cfg.TRAIN.check_interval)
print('devices =', device_parser('0'))

# 训练相对推理额外需要的文件
extra_assets = [
    meta_cfg.BACKBONE.backbone_ckpt,                # ViTPose 预训练骨干
    'assets/SMPLX2SMPL/body_models/smplx2smpl.pkl', # SMPL-X -> SMPL 顶点映射
    'assets/SMPLX2SMPL/SMPL_to_J19.pkl',            # 额外关节回归器
    'assets/SMPL/SMPL_NEUTRAL.pkl',                 # SMPL 中性模型
    'ehm_datasets/000000.tar',                      # 示例训练分片
]
for p in extra_assets:
    print(('OK    ' if os.path.exists(p) else 'MISS  ') + p)
```

**需要观察的现象**：打印出的五个 `TRAIN` 数值（40 / 200000 / 10000 / [0]）与 train.yaml 一致；资产清单逐项标出 OK 或 MISS。

**预期结果**：按 u1-l2 只准备了推理资产的机器上，五项里只有（可能）SMPL/SMPLX/FLAME 相关的部分就绪，`vitpose_backbone.pth`、`SMPLX2SMPL` 两件与 `000000.tar` 大概率 MISS——这就是「能推理」与「能训练」的资产差距。脚本本身不依赖 GPU，预期可直接运行；若 `ConfigDict` 构造报 KeyError，检查是否漏了 `add_extra_cfgs`（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 optimizer 要在 `fabric.setup(backbone, optimizer)` 之前创建、ViTPose 权重要在 `setup` 之前加载？

**答案**：Fabric 的 `setup` 会把参数搬到 GPU 并把模块包进 DDP， optimizer 若在此之前创建，Fabric 能把「参数搬家」和优化器内部持有的参数引用一并对齐；若之后创建也无妨，但官方推荐先建 optimizer 再 setup。而权重加载放在 setup 前是因为：(1) `map_location='cpu'` 加载更稳；(2) DDP 包装后 `state_dict` 的键会带上 `module.` 前缀，直接加载会全部失配。

**练习 2**：`EHM_v2` 没有可学习参数，为什么还要 `.to(self.lightning_fabric.device)`？

**答案**：因为训练前向里 `run_fit` 调用 `self.ehm(body_param, flame_param)` 得到关节与顶点再算损失，梯度要穿过 EHM 内部的 LBS / FLAME 运算流回 head 输出的参数。PyTorch 要求计算图中所有参与运算的张量同设备，它的 buffer（模板、蒙皮权重等）不在 GPU 上就会在第一次前向时报设备不匹配错误。

**练习 3**：如果 `find_unused_parameters=False`，训练会在哪里出问题？

**答案**：head 里存在「构造了但前向未被调用」的参数（u3-l3 审计过的 `joint` 解码器等），这些参数不参与计算图。DDP 默认要求所有被包装参数都收到梯度，否则反向时挂起或报错；`find_unused_parameters=True` 让 DDP 每次前向后扫描计算图、跳过未使用参数的同步。代价是每次迭代多一次图遍历，略降吞吐。

---

### 4.3 `run_fit` 主循环：前向 → EHM → 投影 → 损失 → 反向

#### 4.3.1 概念说明

`run_fit` 是 PEAR 训练的心脏，一个手动优化循环。它把 u5-l1 提供的标注 batch、u3 系列提供的网络输出、u4 系列提供的人体模型，用五项损失焊在一起：

| 损失 | 监督信号 | 计算路径 | 权重 |
| --- | --- | --- | --- |
| `loss_dwpose_2d` | DWPose 134 点 2D | 145 关节 → dwpose 映射 → 投影 | 0.01 |
| `loss_smpl_2d` | SMPL 44 点 2D | 顶点 → smplx2smpl 回归 44 关节 → 投影 | 0.01 |
| `loss_smpl_3d` | SMPL 44 点 3D | 顶点 → smplx2smpl 回归 44 关节 | 0.05 |
| `loss_param_smplx` | `smplx_coeffs` 全套 | 直接对 `body_param` 逐项比对 | 1 |
| `loss_param_flame` | `flame_coeffs` 全套 | 直接对 `flame_param` 逐项比对 | 1 |

设计哲学是「几何监督 + 参数监督」双轨：2D/3D 关键点损失约束网格「摆得对」，参数损失约束「回归得像标注分布」（防止不同参数组合凑出相同几何的退化）。损失类内部实现（置信度加权、有效性门控、GMoF）留给 u5-l3，本讲只关心它们如何被组装。

#### 4.3.2 核心流程

```text
run_fit(init_iter=0)
├─ 准备：进度条(tqdm/rtqdm)、train_iter、_set_state(train=True)、SummaryWriter
└─ for iter_idx in [0, train_iter]:
   ├─ batch = next(train_iter)     # StopIteration 则重建迭代器（无限训练）
   ├─ ① 前向：img_patch = to_tensor(batch['ehm_image'])；outputs = forward_step(img_patch)
   ├─ ② 网格：pd_smplx_dict = ehm(body_param, flame_param)   # 10595 顶点 / 145 关节
   ├─ ③ 投影一：145 关节 → dwpose 134 点 → perspective_projection(R,T) → loss_dwpose_2d
   ├─ ④ 投影二：顶点 → smplx2smpl_joints → SMPL 44 关节
   │      ├─ 投影到 2D → loss_smpl_2d
   │      └─ 直接 3D 对齐(骨盆对齐) → loss_smpl_3d
   ├─ ⑤ 参数：loss_param_smplx + loss_param_flame
   ├─ ⑥ loss_main = 五项求和；每 50 步写 6 条 TensorBoard 标量
   ├─ ⑦ 反向：optimizer.zero_grad() → fabric.backward(loss_main) → optimizer.step()
   ├─ 每 1000 步：可视化（原图 | GT 网格 | 预测网格 三联图）
   ├─ 每 5000 步：run_val
   └─ 每 40000 步：save_checkpoints('ehm_model.pt')
```

epoch 耗尽的处理值得注意：`run_fit` **不用** PyTorch 的多 epoch 写法，而是「迭代器抛 `StopIteration` 就重建」（[pipeline.py:263-267](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/pipeline.py#L263-L267)）。配合 u5-l1 讲过的训练集 `resampled=True` 无限重采样，外层这个重建其实只是兜底——但语义一致：**训练按迭代数（200000 步）计数，不按 epoch 计数**。

#### 4.3.3 源码精读

**循环准备。** [pipeline.py:250-259](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/pipeline.py#L250-L259)：进度条在 `--debug` 时用普通 `tqdm`、否则用 `rtqdm`（[utils/general_utils.py:125](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/general_utils.py#L125) 起定义的 rich 富文本版 tqdm）；`_set_state(train=True)`（[pipeline.py:239-245](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/pipeline.py#L239-L245)）把 backbone/head 切到 `train()` 模式（DropPath 等只在训练生效，u3-l1）；`SummaryWriter` 指向 `outputs/<时间戳>/writers/`。

**前向与网格重建。** [pipeline.py:270-280](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/pipeline.py#L270-L280)：`to_tensor` 把 `batch['ehm_image']`（u5-l1 的 256 网络输入）搬到 GPU；`forward_step`（[pipeline.py:176-203](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/pipeline.py#L176-L203)）内部做 ImageNet 归一化、`x[:,:,:,32:-32]` 裁成 256×192、骨干提特征、head 出参数——与推理完全同一份代码。随后 `self.ehm(body_param, flame_param, pose_type='aa')` 重建 10595 顶点与 145 关节（u4-l4）。关节先经 `smplx_joints_to_dwpose` 映射成 DWPose 134 点，再用 `self.cameras.perspective_projection(pred_kps3d, R=outputs['pd_cam'][:,:3,:3], T=outputs['pd_cam'][:,:3,3])` 投到 1024×1024 像素平面——R、T 直接取自 `pd_cam` 的 4×4 矩阵切片（u3-l4 讲过的「弱透视参数 → 透视相机」换算在此生效）。

**三路关键点损失。** [pipeline.py:282-290](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/pipeline.py#L282-L290)：

- `kps2d_mask = batch['dwpose_kp2d'][:,:,2] > 0.7`：只用置信度高于 0.7 的点做监督（`dwpose_kp2d` 第三通道是置信度，u5-l1）；
- `loss_dwpose_2d = self.metric(pred_kps2d[..., :2][kps2d_mask] / 1024, batch['dwpose_kp2d'][..., :2][kps2d_mask]) * 0.01`：`self.metric` 是 `L1Loss(reduction='sum')`，预测除以 1024 把像素坐标归一到 [0,1]（GT 在数据侧已归一，可视化时 `* 1024` 还原即为佐证）；

\[ \mathcal{L}_{\text{dwpose}} = 0.01 \cdot \sum_{(b,k)\,:\,m_{bk}=1} \left\| \hat{p}_{bk}/1024 - p_{bk} \right\|_1 \]

- SMPL 侧：`smplx2smpl_joints(vertices, smplx2smpl, smpl, J_regressor_extra, 'H36M-VAL-P2')` 用 `__init__` 装载的三件资产从 SMPL-X 顶点回归出 SMPL 44 关节（3D 损失以 `pelvis_id=39` 对齐骨盆消除全局平移自由度），再投影得 2D 损失。2D 权重 0.01、3D 权重 0.05。

**参数损失与总损失。** [pipeline.py:292-296](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/pipeline.py#L292-L296)：`body_params_loss` / `head_params_loss` 分别比对预测参数字典与 `batch['smplx_coeffs']` / `batch['flame_coeffs']`（u5-l1 讲过 GT 参数补零至 200/300 维、`has_*` 软门控），五项直接相加成 `loss_main`。**注意权重全部硬编码在循环体内**——`__init__` 里的 `self.loss_weight` 字典（[pipeline.py:62](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/pipeline.py#L62)，含 `kp3d: 0.05` 等五项）自赋值后**从未被读取**，是个漂亮的「配置看起来可调、其实改了没用」陷阱：想调权重必须改 `run_fit` 里的常数。

**记录与反向。** [pipeline.py:299-311](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/pipeline.py#L299-L311)：每 50 步写六条标量——`Loss/train_total`、`Loss/param_smplx`、`Loss/param_flame`、`Loss/loss_hmr_2d`、`Loss/loss_hmr_3d`、`Loss/dwpose_2d`；然后经典三连 `zero_grad()` → `self.lightning_fabric.backward(loss_main)` → `self.optimizer.step()`。`fabric.backward` 与裸 `loss.backward()` 的区别就在多卡：它保证梯度 all-reduce 在合适的时机发生（DDP 语义下即反向过程中逐桶同步），单卡时两者等价。进度条描述（[pipeline.py:315-317](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/pipeline.py#L315-L317)）实时打印五项损失，方便肉眼监控。

#### 4.3.4 代码实践

**实践目标**：用示例数据真正跑几十步训练，从 tqdm 与 TensorBoard 两条通道观察损失。

**操作步骤**：

1. 前置（对照 4.2.4 的盘点结果）：下载 README 提供的示例 `000000.tar` 放到 `ehm_datasets/`；准备 `data_inputs/backbone/vitpose_backbone.pth`（ViTPose 预训练）与 `assets/SMPLX2SMPL`、`assets/SMPL`。train.yaml 默认 train/valid 都指向同一个 `000000.tar`，所以一个分片即可启动。
2. 启动：`python train_ehms.py -c train -d 0`（`-c train` 对应 `configs/train.yaml`）。
3. 观察终端 tqdm 的 loss 描述行，跑 50~100 步后 Ctrl-C 停止（`check_interval` 与存档的关系见 4.4 的实验，此处先不指望自动存档）。
4. 另开终端：`tensorboard --logdir outputs/`，在浏览器打开提示的端口，找最新时间戳目录下 `writers/` 里的六条曲线。

**需要观察的现象**：tqdm 描述里 `loss` 总值与 hmr 3D / hmr 2D / 2D / Params 分项都在打印；TensorBoard 中 `Loss/train_total` 与 `Loss/param_smplx` 每隔 50 步各出现一个点（几十步内只有一两个点，属正常）。

**预期结果**：损失数值不为 NaN、曲线有值即可。示例分片只有 1000 个伪 epoch 样本，几十步内看不到明显收敛趋势，不要过度解读。本实践需要 GPU 与全部训练资产；在只有推理资产的机器上会停在 4.2.4 盘点出的第一个 MISS 文件处（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 2D 损失要把预测坐标除以 1024，而 GT 不乘 1024？

**答案**：不对称是表面现象，本质是两边单位不同：预测来自 `perspective_projection`，输出是 1024×1024 画布上的像素坐标；GT 在数据侧（u5-l1 的 `example_formatter`）已归一化到 [0,1]。所以把预测 `/1024` 归一到同一尺度再比 L1。可视化代码里 `draw_landmarks(_landmark_dwp * 1024, ...)`（GT 乘回 1024）正好是反向操作，可交叉验证。

**练习 2**：`loss_smpl_3d` 为什么传 `pelvis_id=39`？

**答案**：预测网格与 GT 标注的全局位置不可直接比较（网络预测的深度有尺度歧义，u3-l4 讲过弱透视相机的 s 与 z 换算）。以骨盆（44 点 SMPL 布局中的 39 号）为根，把双方都减去骨盆坐标后再算 L1，等价于只约束「相对骨架形状」而不约束全局平移。

**练习 3**：`fabric.backward(loss_main)` 与 `loss_main.backward()` 在单卡 `-d 0` 下有区别吗？多卡呢？

**答案**：单卡下基本等价（Fabric 转发给 `loss.backward()`）。多卡下必须用 `fabric.backward`：它配合 DDP 的梯度钩子在反向过程中完成 all-reduce 平均，保证各卡参数更新一致；裸 `backward` 会让各卡梯度各走各的，参数迅速发散。

---

### 4.4 可视化、验证与 `save_checkpoints`

#### 4.4.1 概念说明

训练循环里最容易被忽略、却最能救命的三个旁路：**可视化**（训练中每 1000 步把「原图 | GT 网格 | 预测网格」三联图落盘，肉眼判断模型在学什么）、**验证**（每 5000 步在 valid split 上跑两个 batch 渲染对比图，但不计算指标）、**存档**（每 40000 步保存 checkpoint）。本节还要揭示本模块最重要的一处「死配置」：`TRAIN.check_interval` 被 `__init__` 读取存进 `self._check_interval`，但 `run_fit` 的存档触发是**硬编码的 `% 40000`**——改 YAML 里的 `check_interval` 不会改变任何行为。

#### 4.4.2 核心流程

```text
每 1000 步（可视化）：
  取 batch 第 0 个样本 → 原图放大到 1024
  → 绿色画 GT 关键点、红色画预测关键点（smpl_kp 标志选 44 点或 134 点通道，u5-l1）
  → 分别用 pd_cam 与 GT 的 camera_RT_params 渲染预测 / GT 网格
  → 拼三联图（原图 | GT 叠加 | 预测叠加）→ 缩小一半存 outputs/<ts>/visual_train/

每 5000 步（验证 run_val）：取 valid 前 2 个 batch（batch_size=1）→ 同样渲染对比 → visual_val/
每 40000 步（存档 save_checkpoints）：
  state = {'backbone': DDP包装的backbone模块, 'head': head模块,
           'meta_cfg': cfg._dump(整份YAML的dict快照), 'global_iter': iter_idx}
  → fabric.save(outputs/<ts>/stage1_checkpoints/ehm_model.pt, state)
```

#### 4.4.3 源码精读

**可视化块。** [pipeline.py:319-362](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/pipeline.py#L319-L362)：在 `torch.no_grad()` 下，`img_indices = np.linspace(0, n_imgs-1, 1, dtype=int)` 只取 batch 的第 0 个样本（行尾注释 `# 5` 暗示曾经画 5 个）。按 `batch['smpl_kp']` 的真假选监督通道画点：真则用 SMPL 44 点（预测取 `pred_smpl_2d`），假则用 DWPose 134 点——绿 GT、红预测，正是 u5-l1「一真一零双通道」设计的消费现场。网格渲染复用推理侧的全部知识：`GS_Camera` + `body_renderer.render_mesh` + `PointLights(location=[[0,-1,-10]])`（u4-l5），预测用 `outputs['pd_cam']` 的 R/T，GT 用 `batch['smplx_coeffs']['camera_RT_params']`——GT 侧也走 EHM（`self.ehm(batch['smplx_coeffs'], batch['flame_coeffs'])`，[pipeline.py:352](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/pipeline.py#L352)），保证两边网格表达一致，只比参数差异。最终 `cv2.addWeighted(_img, 0.3, mesh, 0.7, 0)` 混合、横向拼接、存 `visual_train/smplx_stp_<iter>_<idx>_<rank>.png`。

**验证与存档触发。** [pipeline.py:364-369](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/pipeline.py#L364-L369)：两个触发条件都带 `and iter_idx != 0`（第 0 步不触发）。注意这里用的是**字面量** 5000 / 40000，不是 `cfg.TRAIN.check_interval`。还有一个隐蔽的作用域地雷：`rank` 变量是在 `% 1000` 的可视化块里赋值的（[pipeline.py:323](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/pipeline.py#L323)），而 `run_val(iter_idx, rank)` 在 `% 5000` 分支里使用它——之所以不炸，纯粹因为 5000 是 1000 的倍数，`rank` 恰好总是已定义。若有人把验证间隔改成 3000，就会在 3000 步收到 `NameError: name 'rank' is not defined`。改这类「魔法数字」前先看清变量的诞生地。

**`run_val`。** [pipeline.py:410-421](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/pipeline.py#L410-L421)：只取 valid DataLoader 的前 2 个 batch（`sample_batches = 2`，而该 loader `batch_size=1`），渲染存图到 `visual_val/`。**没有任何数值指标**（无 MPJPE、无 PCK）——它只是「肉眼中检」，定量评测走仓库外的评测代码。

**`save_checkpoints`。** [pipeline.py:376-396](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/pipeline.py#L376-L396)：`state` 四件套——`'backbone'`、`'head'`（**直接放了模块对象**，不是 `.state_dict()`）、`'meta_cfg'`（`cfg._dump`，即 [utils/general_utils.py:37-39](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/general_utils.py#L37-L39) 返回的整份配置 dict 快照）、`'global_iter'`；`optimizer=True` 时可额外存优化器（当前调用没传）。最后 `self.lightning_fabric.save(path, state)`——Fabric 的 save 由 rank 0 落盘并负责分布式聚合。`name.startswith('best')` 的清理逻辑为「只保留最新 best」而设，当前调用名固定 `ehm_model.pt`，不会触发。

**存与取的错位（值得警惕）。** 对照 [train_ehms.py:65-70](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/train_ehms.py#L65-L70) 的恢复方式：`torch.load(..., weights_only=True)` 后对 `_state['backbone']` 调 `load_state_dict`——这假设 checkpoint 里存的是 **state dict（纯张量字典）**。而 `save_checkpoints` 放进去的是 **module 对象**；两者能否对上，取决于所用 lightning 版本的 `Fabric.save` 是否在内部把 module 转成 state dict（官方发布的 `pear_model.pt` 顶层确为两段 state dict，u2-l5 已确认，说明发布前做过转换或该版本 Fabric 做了转换）。**不要想当然，用 4.4.4 的实验亲手验证你这份环境的实际行为。**

#### 4.4.4 代码实践

**实践目标**：亲手验证「`check_interval` 是死配置」这一论断，并让一份 `ehm_model.pt` 在几十步内诞生，再检查它的内部结构。

**操作步骤**：

1. **对照组**：把 `configs/train.yaml` 的 `check_interval` 从 10000 改成 10（本实践都在你自己的工作副本上改），启动 `python train_ehms.py -c train -d 0`，跑到第 40 步左右 Ctrl-C，检查 `outputs/<时间戳>/stage1_checkpoints/`——**预期没有文件**，因为触发条件是 [pipeline.py:368](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/pipeline.py#L368) 的 `iter_idx % 40000 == 0`。
2. **实验组**：在你的副本里把 [pipeline.py:368](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/pipeline.py#L368) 的 `40000` 改成 `50`；顺手把 [pipeline.py:299](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/pipeline.py#L299) 的 `% 50` 改成 `% 10`、[pipeline.py:321](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/pipeline.py#L321) 的 `% 1000` 改成 `% 20`，让曲线和三联图也尽快出现。重新跑到第 60 步以上再停。
3. **验收**：确认 `outputs/<时间戳>/stage1_checkpoints/ehm_model.pt` 生成、`visual_train/` 下有三联图、TensorBoard 曲线点变密。
4. **结构检查**（在仓库根目录跑）：

```python
# 示例代码：检查训练产出的 checkpoint 结构
import torch
state = torch.load('outputs/<你的时间戳>/stage1_checkpoints/ehm_model.pt',
                   map_location='cpu', weights_only=False)  # 存的是模块对象时必须 False
print(type(state), list(state.keys()))
for k in ('backbone', 'head'):
    v = state[k]
    print(k, type(v))
```

**需要观察的现象**：对照组无 checkpoint；实验组 60 步左右出现 `ehm_model.pt`；结构检查打印出顶层键与 `backbone`/`head` 的实际类型（是 `dict`（state dict）还是模块对象）。

**预期结果**：若打印显示 `dict`，说明你的 lightning 版本的 `Fabric.save` 已把模块转成 state dict，与 `train_ehms.py` 的恢复路径兼容；若是模块对象，则 `weights_only=True` 的恢复会失败——这正解释了为什么官方发布权重必须做一层转换。两种结果都是有效结论，记下你环境的实际行为（待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：`save_checkpoints` 里的 `meta_cfg` 存的是什么？为什么值得存？

**答案**：`self.cfg._dump` 是 `ConfigDict.__getattr__('_dump')` 返回的 `dict(self)`——整份 YAML 的纯 dict 快照（含自动追加的 `EXP_STR`/`TIME_STR`）。它让 checkpoint 自带「我当时用什么结构、什么超参训的」的完整描述，配合 `train_ehms.py` 已另行备份的 `config.yaml`，构成复现实验的双保险。

**练习 2**：`run_val` 有什么「名不副实」之处？

**答案**：它不计算任何验证指标，只是把 valid split 前 2 个样本的 GT / 预测网格渲染成对比图存到 `visual_val/`。叫「可视化验证」更准确；定量评测（MPJPE 等）不在这条链路上。

**练习 3**：如果把验证间隔从 5000 改成 3000，除了触发更频繁，还可能引发什么 bug？

**答案**：`NameError`。`run_val(iter_idx, rank)` 用的 `rank` 只在 `% 1000` 的可视化块里赋值；3000 不是 1000 的倍数，第 3000 步执行到验证分支时 `rank` 尚未定义。修复办法是把 `rank = dist.get_rank()` 提到循环开头或用 `self.lightning_fabric.global_rank`。

---

## 5. 综合实践

**任务：一次「短跑式」训练 + 产物全链路验收。**

1. **准备**：按 4.2.4 的盘点补齐五项训练资产（示例 tar、ViTPose 骨干、SMPLX2SMPL 两件、SMPL 模型），确保 `python train_ehms.py --help` 可用。
2. **快跑配置**：在你的工作副本上做三处临时修改（做完实验可还原）：[pipeline.py:299](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/pipeline.py#L299) 记录间隔 `% 50` → `% 10`；[pipeline.py:321](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/pipeline.py#L321) 可视化间隔 `% 1000` → `% 20`；[pipeline.py:368](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/pipeline.py#L368) 存档间隔 `% 40000` → `% 50`。
3. **跑**：`python train_ehms.py -c train -d 0`，跑到 100 步以上，Ctrl-C 停止。
4. **验收清单**（逐项打勾）：
   - [ ] `outputs/<时间戳>/config.yaml` 存在（`train()` 备份）；
   - [ ] `outputs/<时间戳>/writers/` 下 TensorBoard 能看到 `Loss/train_total`、`Loss/param_smplx` 等六条曲线；
   - [ ] `outputs/<时间戳>/visual_train/` 下有 `smplx_stp_*.png` 三联图，肉眼确认红点（预测关键点）与绿点（GT）随训练逐渐靠近（示例数据上幅度有限）；
   - [ ] `outputs/<时间戳>/stage1_checkpoints/ehm_model.pt` 生成，且用 4.4.4 的脚本查明了内部结构。
5. **断点恢复实验**：把上一步的 `ehm_model.pt` 路径传给 `--ehm_model`，再次启动训练，确认终端打印 `Load base model from: ...` 且能正常起跑——这验证了「存得下、取得回」的闭环。
6. **思考题**（写进你的笔记）：这次你改了三个硬编码间隔才让产物快速出现。如果要把它们变成真正可配置的，`check_interval` 应该接回 `self._check_interval`（它已经读进来了），另外两个间隔该加在 `TRAIN` 段的哪里？`self.loss_weight` 字典又该如何接回 `run_fit` 的五处常数？

## 6. 本讲小结

- **装配顺序**：`train()` 按配置 → 种子 → 设备 → 数据集 → DataLoader → 输出目录 → `OurPipeline` → 断点权重 → `run_fit` 的顺序组装；`train()` 与 `OurPipeline.__init__` 各算一次时间戳，产物可能分落在两个 `outputs/<时间戳>/` 目录。
- **Fabric 而非 Trainer**：`OurPipeline` 是普通类，借 `lightning.Fabric` + `DDPStrategy(find_unused_parameters=True)` 完成设备搬运、DDP 包装与 DataLoader 改造；optimizer 与 ViTPose 权重都必须在 `setup()` 之前就位；学习率 1e-5 硬编码，`OPTIMIZE` YAML 段无消费者。
- **单步训练**：`forward_step`（与推理同一份代码）出参数 → `EHM_v2` 重建 10595 顶点网格（梯度穿过）→ 双通道投影（DWPose 134 点置信度门控 + SMPL 44 点骨盆对齐）→ 参数损失 → `fabric.backward` 反向；五项损失权重全部硬编码。
- **产物三旁路**：每 1000 步三联图可视化、每 5000 步 `run_val` 渲染（无指标）、每 40000 步 `fabric.save` 存 `{backbone, head, meta_cfg, global_iter}`。
- **死配置免疫**：`TRAIN.check_interval`、`self._visual_train_interval`、`self.loss_weight` 都是「读了没用」的假开关；`forward()` 是引用不存在属性的僵尸方法；改行为前先 grep 消费点。
- **存取错位**：`save_checkpoints` 放入的是模块对象，而 `--ehm_model` 恢复假设是 state dict（`weights_only=True`），实际行为取决于 lightning 版本——以本地验证为准。

## 7. 下一步学习建议

1. **u5-l3（损失函数设计）**：本讲把 `BodyParameterLoss` / `HeadParameterLoss` / `Keypoint2DLoss` / `Keypoint3DLoss` 当黑盒调用，下一讲拆开它们的置信度加权、`has_hand`/`has_flame` 门控与 GMoF 鲁棒度量，并做权重消融实验。
2. **续读源码**：[models/pipeline/loss.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/models/pipeline/loss.py) 与 [utils/smplx2smpl_joints.py](https://github.com/Pixel-Talk/PEAR/blob/230fa1534367c9f357c1c192a328cdc87ab4491c/utils/smplx2smpl_joints.py)——后者是本讲反复调用的 SMPL-X → SMPL 44 关节回归器。
3. **横向对照**：把 `run_fit` 与 `forward_step`、推理侧 `Ehm_Pipeline.forward`（u2-l5）并排读，体会「训练与推理共享同一份前向代码、只在外围加减部件」的工程模式——这也是你自己复用 PEAR 时最该模仿的结构。
