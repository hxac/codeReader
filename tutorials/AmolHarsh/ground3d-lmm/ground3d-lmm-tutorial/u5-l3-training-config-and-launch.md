# 训练配置与启动：train.sh 背后发生了什么

## 1. 本讲目标

前面五讲我们攒齐了训练的全部零件：数据管线（单元 2）、稀疏主干与超点池化（单元 3）、点云 token 注入与语言分支（单元 4）、查询解码器与损失函数（单元 5 前两讲）。本讲回答最后一个问题：**这些东西如何被组织成一次真正的训练**。

学完本讲，你应该能：

1. 逐段读懂 [configs/ground3dlmm_train.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py) 的每个配置块，并说出「改哪个字段会动到什么」。
2. 解释 `ConcatDataset` 如何把 ScanNet 与 ScanNet++ 两个数据源拼成一条训练流。
3. 完整复述 `bash train.sh` 之后的调用链：`train.sh → tools/dist_train.sh → torch.distributed.launch → tools/train.py → Runner.from_cfg → runner.train()`。
4. 理解 `paramwise_cfg` 按**参数名子串**分层学习率的机制，并知道一个源码层面的重要细节：配置里的 `'backbone'` 键实际上匹配不到任何参数（主干属性名是 `unet`）。
5. 区分 `load_from`（主干权重初始化）与 `--resume`（完整训练状态恢复）两条互斥的权重加载路径。

## 2. 前置知识

- **配置驱动训练（mmengine Config）**：OpenMMLab 2.0 把「训练一次需要什么」全部声明在一个 Python 风格的配置文件里——模型、数据、优化器、调度器、钩子。`tools/train.py` 不含任何模型逻辑，只负责把配置变成一个 `Runner`。继承、覆盖与 `_delete_` 机制已在 u1-l3 讲过，本讲直接使用。
- **Runner**：mmengine 的训练总指挥。它按配置依次构建模型、优化器、数据集与 DataLoader、各类钩子，然后驱动 `train_cfg` 声明的训练循环（本项目用 `EpochBasedTrainLoop`）。
- **DDP 分布式（torch.distributed.launch）**：多卡数据并行。每张卡跑一个完整模型副本，各卡拿不同数据，前向后梯度自动平均。启动前需要一个「主端口」让各进程互相发现。
- **参数名（named_parameters）**：PyTorch 里每个可训练张量的全名是「属性路径」，如 `decoder.reason_model.lm_head.weight`。本讲的关键机制 `paramwise_cfg` 就是在这些名字上做**子串匹配**来分组调学习率——所以「模型作者把属性起什么名」直接决定「配置里哪个键能命中它」。
- **`load_from` 与 `resume` 的区别**：`load_from` 只搬**模型权重**（非严格、缺键不报错），用于给新训练一个起点；`resume` 搬**权重 + 优化器状态 + 调度器进度 + epoch 计数**，用于中断续训。二者互斥。
- 回顾两个事实（u4-l2）：Qwen3-VL 底座在 `_init_reason_model` 里被冻结后仅注入 LoRA，且 `embed_tokens`/`lm_head` 被重新解冻——这决定了**哪些参数会进优化器**；criterion 是无可训练参数的纯组装者（u5-l2），**不会**进优化器。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [configs/ground3dlmm_train.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py) | 唯一的训练配置：模型、双数据集、优化器、调度器、钩子、预训练权重，233 行全部讲完 |
| [train.sh](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/train.sh) | 一键训练入口：选卡 + 环境变量 + 调用 dist_train.sh |
| [tools/dist_train.sh](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/dist_train.sh) | 探测空闲端口，用 torch.distributed.launch 起 N 个训练进程 |
| [tools/train.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/train.py) | mmdet3d 通用训练入口（文件头自注为原样拷贝）：CLI 合并 → 构建 Runner → 启动训练 |
| [uniseg3d/uniseg3d.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py) | `Grounded_LMM_main.__init__`——参数名前缀（`unet.`/`decoder.`/`criterion.`）的诞生地 |
| [uniseg3d/query_decoder.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py) | `Grounded_Decoder_Joint` 内 Qwen 的加载、冻结、LoRA 包装与解冻——决定优化器里有什么 |
| [README.md](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/README.md) | 「🏋️ Training」章节：数据准备、启动方式、官方调参表 |

## 4. 核心概念与源码讲解

### 4.1 训练配置全景与 ConcatDataset 双数据源

#### 4.1.1 概念说明

一份 mmengine 训练配置本质上是回答五个问题：

1. **模型是什么**——`model` 块（我们已在 u3/u4/u5 前两讲逐件拆过，本讲只看与训练行为相关的字段）；
2. **数据从哪来**——`train_dataloader.dataset`；
3. **怎么优化**——`optim_wrapper` + `param_scheduler` + `train_cfg`；
4. **从哪起步**——`load_from`；
5. **何时存档、何时记日志**——`default_hooks` / `custom_hooks`。

本项目数据侧的特殊点是**双数据源**：训练同时在 ScanNet 与 ScanNet++ 两份 Ground3D 数据上进行。mmengine 提供的 `ConcatDataset` 允许把多个数据集对象按索引拼接成一个大数据集——总长度等于两者之和，采样在这个拼接索引空间里统一洗牌，因此每个 batch 会随机混合两个域的样本。这是最简单的「多域联合训练」手段：不需要改 Dataset 类，配置里声明即可。

#### 4.1.2 核心流程

```text
配置合并（递归继承 _base_ → 覆盖 → 处理 _delete_）
  mmdet3d::_base_/default_runtime.py   ─┐
  mmdet3d::_base_/datasets/scannet-seg.py ├─→ 合并后的完整训练配置
  configs/ground3dlmm_train.py        ─┘
        │
        ▼
train_dataloader.dataset = ConcatDataset([
        Part3DGlamm_Dataset(scannet:     part3d_glamm_infos_train.pkl),
        Part3DGlamm_Dataset(scannetpp:   part3d_glamm_scannetpp_infos_train.pkl)])
  两个子数据集：同一 data_prefix 四键 + 同一 train_pipeline，只差 data_root 与 ann_file
        │
        ▼
每个训练迭代：从拼接索引空间取 batch_size=1 个样本（4 卡 DDP → 全局有效 batch=4）
  → pipeline（u2 系列）→ Det3DDataPreprocessor_（u2-l5）→ model.loss()（u3-l1）
  → Part3dGlammCriterion_v2（u5-l2）→ parse_losses 求和 → 反传 + clip_grad
```

#### 4.1.3 源码精读

**配置的骨架与注册入口**。[configs/ground3dlmm_train.py:L1-L6](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L1-L6) 继承两个 mmdet3d 基座（运行时默认值 + ScanNet 分割数据基座），`custom_imports` 触发 `uniseg3d` 包的注册副作用（u1-l3）。第 6 行 `find_unused_parameters = True` 是 DDP 标志：模型里存在参与前向但不产生损失的「死开关」分支（u3-l1 讲过的 UniSeg3D 遗产），DDP 必须放宽「所有参数都收到梯度」的检查才能启动。

**双数据源声明**。[configs/ground3dlmm_train.py:L162-L189](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L162-L189) 是本模块的核心：`train_dataloader` 写了 `batch_size=1`、`num_workers=16`，而 `dataset=dict(_delete_=True, type='ConcatDataset', datasets=[...])` 用 `_delete_` 把从 `scannet-seg.py` 基座继承来的 dataset 字典**整体铲掉重建**（否则 `type` 冲突会让合并结果四不像，u1-l3）。两个子数据集逐字段对照：

| 字段 | 子数据集 1（ScanNet） | 子数据集 2（ScanNet++） |
|---|---|---|
| `type` | `Part3DGlamm_Dataset` | 同左 |
| `ann_file` | `part3d_glamm_infos_train.pkl` | `part3d_glamm_scannetpp_infos_train.pkl` |
| `data_root` | `data/scannet` | `data/scannetpp` |
| `data_prefix` | 四键 `points`/`part_mask`/`object_mask`/`super_points` | 同左 |
| `pipeline` | `train_pipeline`（同一份） | 同左 |
| `scene_idxs` | `None` | `None` |

两个域之所以能共享同一条 pipeline 与同一套 `data_prefix`，靠的正是 u2-l3 讲过的「裸文件名契约」：pkl 里只存文件名，目录由 `data_root + data_prefix` 运行时拼出，所以换一个 `data_root` 就切换了整个域。`scene_idxs=None` 表示不做场景级重采样（u2-l3：`parse_data_info` 的场景采样为恒等）。

**显式关掉 val/test**。[configs/ground3dlmm_train.py:L191-L195](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L191-L195) 把 `val_dataloader`/`val_evaluator`/`test_dataloader`/`test_evaluator` 全部置 `None`，注释写明目的：防止从 `scannet-seg.py` 基座继承出一套指向错误标注的验证配置。这是继承式配置的卫生习惯——**继承会带进来你没写过的东西，不想要就要显式清除**。

**训练循环与节奏字段**：

- [configs/ground3dlmm_train.py:L211](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L211)：`epoch = 10`，一个变量同时喂给 `param_scheduler` 的 `end` 与 `train_cfg` 的 `max_epochs`——改训练轮数只动这一处。
- [configs/ground3dlmm_train.py:L229-L232](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L229-L232)：`EpochBasedTrainLoop` + `dynamic_intervals=[(1, 4), (epoch - 16, 1)]`。`dynamic_intervals` 控制的是**验证间隔**随训练进程变化；本配置验证已禁用，因此它是从上游项目拷来的无效果样板（且 `epoch=10` 时第二个区间起点是负数，同样不触发）。读配置时要能识别这类「无害的考古层」。
- [configs/ground3dlmm_train.py:L213](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L213)：`PolyLR` 多项式衰减，\( lr(t) = lr_{base} \times (1 - t/T)^{0.95} \)，\(T\) 为总迭代数（`begin=0, end=10` 个 epoch）。前段降得缓、末段趋零，是分割任务常用形状。
- [configs/ground3dlmm_train.py:L215](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L215)：`EmptyCacheHook(after_iter=True)` 每个 iteration 后调用一次 `torch.cuda.empty_cache()`。4B 语言模型 + 稀疏卷积 + 生成式解码（每次前向内部还有 processor 编码、二次前向）共驻显存，碎片化风险高，逐迭代清缓存是内存卫生。
- [configs/ground3dlmm_train.py:L217-L220](https://github.com/AmolHarsh/ground3dlmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L217-L220)：每个 epoch 存一次 checkpoint、只保留最新 5 份；每 50 个 iteration 记一行训练日志（loss 曲线的粒度）。
- [configs/ground3dlmm_train.py:L224-L227](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L224-L227)：`seed=1` 全局固定初始化；`diff_rank_seed=True` 让各 rank 的数据增强随机流不同（同一份点云在不同卡上吃到不同旋转/弹性形变，等于免费的数据扩增）。

**pipeline 里与训练行为最相关的两个字段**（其余已在 u2 系列精读）：[L138-L140](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L138-L140) `QA_Generation_v2(num_qa=10)` 每帧采 10 条 QA（决定每步的语言监督条数）；[L144-L149](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L144-L149) `ElasticTransfrom(p=0.5)` 一半概率生成弹性形变坐标副本（u2-l4）。

**官方调参表**：[README.md:L230-L241](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/README.md#L230-L241) 给出了最常改的字段（`qwen_model_path`、`lora_r`/`lora_alpha`、`max_point_tokens`、`batch_size`、`lr`、`epoch`、`load_from`）。建议把本节的「字段 → 效果」表与之互补阅读：README 告诉你**能改什么**，本讲告诉你**改了之后在源码里经过哪些环节生效**。

#### 4.1.4 代码实践：零 GPU 干跑训练配置

延续 u1-l4 养成的习惯：真训练之前先干跑配置，核对合并结果。

1. **实践目标**：不碰 GPU 与数据，验证 `_delete_`、`ConcatDataset`、`val=None` 三件事确实按预期落进了合并后的配置。
2. **操作步骤**：在仓库根目录（已完成 u1-l2 环境安装）运行下面脚本。

   ```python
   # 示例代码：dryrun_train_config.py
   from mmengine.config import Config

   cfg = Config.fromfile('configs/ground3dlmm_train.py')

   ds = cfg.train_dataloader.dataset
   print('dataset type  =', ds.type)          # 期望 ConcatDataset（_delete_ 生效）
   for d in ds.datasets:
       print('  sub-dataset:', d.type, '|', d.ann_file, '|', d.data_root)

   print('batch_size    =', cfg.train_dataloader.batch_size)
   print('num_workers   =', cfg.train_dataloader.num_workers)
   print('val_dataloader=', cfg.val_dataloader)   # 期望 None
   print('test_dataloader =', cfg.test_dataloader) # 期望 None
   print('max_epochs    =', cfg.train_cfg.max_epochs)
   print('load_from     =', cfg.load_from)
   print('scheduler     =', cfg.param_scheduler)
   ```

3. **需要观察的现象**：`ds.type` 是 `ConcatDataset` 而不是基座的 `ScanNetSegDataset`；子数据集恰好两行，`ann_file`/`data_root` 不同而其余字段相同；`val_dataloader` 打印 `None`。
4. **预期结果**：两个子数据集分别为 `part3d_glamm_infos_train.pkl`（`data/scannet`）与 `part3d_glamm_scannetpp_infos_train.pkl`（`data/scannetpp`）；`max_epochs=10`；`load_from='work_dirs/pretrained/sstnet_scannet.pth'`。
5. 本讲义写作环境未安装 mmengine，以上输出为**待本地验证**（脚本本身不依赖 GPU/数据/模型权重，任何装好环境的机器都能跑）。

#### 4.1.5 小练习与答案

**练习 1**：`batch_size=1`、`train.sh` 用 4 张卡，一个全局迭代（iteration）处理几个场景？若 ConcatDataset 拼接后总帧数为 F，一个 epoch 大约多少次迭代？

答：DDP 数据并行下每卡独立取自己的 batch，全局迭代 = 各卡 batch 之和 = 4 个场景。每个 epoch 约 F/4 次迭代（严格值取决于 sampler 的 drop 与对齐策略，以 mmengine `DefaultSampler` 行为为准）。

**练习 2**：两个子数据集除了 `data_root` 和 `ann_file` 还有什么不同？为什么能共用同一条 `train_pipeline`？

答：没有别的不同——`type`、`data_prefix`、`pipeline`、`metainfo`、`scene_idxs`、`test_mode` 全部相同。能共用的根基是 u2-l3 的「裸文件名契约」：infos pkl 里只存文件名，实际路径在运行时由 `data_root + data_prefix` 拼接；且两个域的帧数据布局完全一致（`points/`、`super_points/`、`part_mask/`、`object_mask/` 四目录 + 同结构 QA），pipeline 无需感知自己处理的是哪个域。

**练习 3**：为什么 [L192-L195](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L192-L195) 要显式把 `val_dataloader` 等置 `None`？不写会怎样？

答：因为 `_base_` 里的 `mmdet3d::_base_/datasets/scannet-seg.py` 定义了 val/test 数据配置，继承会把它们原样带进来——指向的标注文件、目录布局与本项目的 Ground3D 数据并不匹配（pkl 字段结构都不同）。显式置 `None` 是把「不想要」变成声明，否则 Runner 会尝试构建一个注定加载失败（或更糟：静默加载错误数据）的验证循环。

### 4.2 分布式启动与 paramwise 学习率分层

#### 4.2.1 概念说明

**启动链路**：`bash train.sh` 背后是三层薄壳——`train.sh` 定死卡数与环境变量，`dist_train.sh` 负责挑一个没人用的端口并用 `torch.distributed.launch` 起 4 个进程，每个进程都执行同一个 `tools/train.py`。后者是 mmdet3d 的原样拷贝（[第 1 行](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/train.py#L1) 注释自证），**不含任何模型特有逻辑**——所有项目差异都隔离在配置文件里，这正是「配置驱动」的好处。

**学习率分层（paramwise）**：一个模型里不同部件的「预训练充分程度」差异巨大——稀疏主干带着 ScanNet 预训练、Qwen 带着 4B 语料预训练、point2token/query_proj/6 层解码器是从零训练的新模块。统一的 `lr=1e-4` 对某些部件太猛、对某些部件太钝。mmengine 的 `paramwise_cfg.custom_keys` 允许按「参数名包含某子串」给不同参数组乘上 `lr_mult` 倍率。**匹配的对象是参数全名**——这是本模块最重要的一句话，因为它意味着模型属性命名直接决定配置键能否命中。

#### 4.2.2 核心流程

```text
bash train.sh
 └─ CUDA_VISIBLE_DEVICES=0,1,2,3 PYTHONPATH=./ ./tools/dist_train.sh configs/ground3dlmm_train.py 4
     └─ 随机生成端口 PORT，用 nc -z 探测直到找到空闲端口
     └─ python -m torch.distributed.launch --nproc_per_node=4 --master_port=PORT \
              tools/train.py configs/ground3dlmm_train.py --launcher pytorch
         └─（×4 个进程，环境变量 RANK/WORLD_SIZE/LOCAL_RANK 各不相同）
             ├─ cfg = Config.fromfile(config)          # 递归合并 _base_、执行 _delete_
             ├─ cfg.launcher = 'pytorch'；--cfg-options 逐点合并
             ├─ work_dir 优先级：CLI --work-dir > 配置段 > 按配置文件名兜底
             ├─ --amp 时把 OptimWrapper 换成 AmpOptimWrapper（动态 loss scale）
             ├─ --resume 时 cfg.resume=True 且覆盖/清空 load_from
             ├─ runner = Runner.from_cfg(cfg)
             │    ├─ init distributed process group（由环境变量推断 rank/world_size）
             │    ├─ build model（unet / decoder(含 Qwen+LoRA) / criterion）
             │    │    └─ 模型参数名由此定型：unet.* / decoder.* / decoder.reason_model.* / criterion.*
             │    ├─ build optim_wrapper：按 paramwise_cfg 对参数名做子串匹配分组
             │    │    → 各组 lr = 1e-4 × lr_mult（命中者）或 1e-4（未命中者）
             │    ├─ build ConcatDataset + DDP 采样（find_unused_parameters=True）
             │    └─ load_from 加载主干预训练（非严格）
             └─ runner.train()   # EpochBasedTrainLoop × 10 epochs
```

#### 4.2.3 源码精读

**train.sh：一行命令，三个决定**。[train.sh:L1](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/train.sh#L1) 用 `CUDA_VISIBLE_DEVICES=0,1,2,3` 选 4 张卡、`PYTHONPATH=./` 保证仓库根目录在导入路径里（`tools/train.py` 自己也会 `sys.path.append` 上级目录，二者互为保险），最后调用 `./tools/dist_train.sh configs/ground3dlmm_train.py 4`——配置路径与卡数作为位置参数传入。想换卡数或换配置，改这一行即可。

**dist_train.sh：端口探测 + 多进程拉起**。[tools/dist_train.sh:L3-L10](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/dist_train.sh#L3-L10) 用「随机拼一个大数再对 49152 取模 + 10000」生成候选端口，`nc -z` 试连本机：**连不上（状态非 0）才说明端口空闲**，连上了就换一个——避免多人同时在同一台服务器上训练时 `master_port` 冲突。[tools/dist_train.sh:L15-L17](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/dist_train.sh#L15-L17) 是真正的启动命令：`torch.distributed.launch` 以 `--nproc_per_node=$GPUS` 起 N 个子进程、`--master_port` 告知集合通信端口，每个子进程执行 `$(dirname "$0")/train.py $CONFIG --launcher pytorch`，`${@:3}` 把 dist_train.sh 收到的第三个及之后的参数原样透传（所以你可以 `bash train.sh` 之外再手动 `./tools/dist_train.sh cfg 4 --amp --cfg-options ...`）。

**tools/train.py：从 CLI 到 Runner**。逐段读：

- [tools/train.py:L17-L62](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/train.py#L17-L62) 参数表：位置参数 `config`；`--work-dir` 默认值就是 `work_dirs/ground3dlmm`（[L20](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/train.py#L20)，README 说 checkpoint 存到 `work_dirs/ground3dlmm/epoch_*.pth` 的出处）；`--amp` 混合精度；`--resume`（不带值时常量 `auto`）；`--cfg-options`；`--launcher`（none/pytorch/slurm/mpi）。
- [tools/train.py:L69-L77](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/train.py#L69-L77) `Config.fromfile` 完成所有继承合并；随后 CLI 的 `launcher` 与 `--cfg-options` 依次写回/合并进 `cfg`——CLI 永远后于文件生效（与 u1-l4 的 test.py 同一惯例）。
- [tools/train.py:L80-L86](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/train.py#L80-L86) `work_dir` 三级优先级：CLI > 配置文件里的段 > 用配置文件名兜底到 `./work_dirs/<name>`。
- [tools/train.py:L89-L101](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/train.py#L89-L101) `--amp` 的实现很朴素：确认当前是普通 `OptimWrapper` 后把它**改名为** `AmpOptimWrapper` 并启用动态 loss scale——优化器本体不变，只是换一层自动混合精度的壳。
- [tools/train.py:L116-L121](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/train.py#L116-L121) resume 分支（见 4.3.3 的展开）。
- [tools/train.py:L124-L133](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/train.py#L124-L133) 默认走 `Runner.from_cfg(cfg)` 把整个配置实例化，最后 `runner.train()` 启动训练循环。全文件没有任何一行涉及 `uniseg3d` 的模型逻辑——通用入口 + 专用配置，职责彻底分离。

**优化器与 paramwise 配置**。[configs/ground3dlmm_train.py:L197-L209](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L197-L209) 声明 AdamW（基准 `lr=1e-4`，`weight_decay=0.05`）、`clip_grad=dict(max_norm=10, norm_type=2)`（梯度 L2 范数裁剪到 10，防止语言损失偶发尖峰引爆 LoRA 权重），以及：

```python
paramwise_cfg=dict(
    bypass_duplicate=True,
    custom_keys={
        'backbone': dict(lr_mult=0.01),   # 意图：主干小步走
        'reason':   dict(lr_mult=0.1),    # 意图：语言塔中步走
        'decoder':  dict(lr_mult=1.0),    # 意图：新模块全速走
    }),
```

`bypass_duplicate=True` 允许一个参数被多个键命中时不重复加入分组（mmengine 的去重保护）。

**参数名考古：`'backbone'` 匹配不到任何参数**。custom_keys 的匹配对象是 `named_parameters()` 输出的**参数全名**，而参数全名由模型属性路径决定。看 [uniseg3d/uniseg3d.py:L956-L958](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L956-L958)：

```python
self.unet = MODELS.build(backbone)      # 参数名前缀 unet.*
self.decoder = MODELS.build(decoder)    # 参数名前缀 decoder.*
self.criterion = MODELS.build(criterion)  # 无可训练参数（u5-l2）
```

`__init__` 的**形参**叫 `backbone`，但属性起名 `self.unet`——配置键 `'backbone'` 在 `unet.*` 里找不到子串。对整个 `uniseg3d/` 包全文检索 `backbone`，命中只有 `__init__` 形参与 docstring（如 [uniseg3d.py:L947](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L947)、[uniseg3d.py:L272](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L272)），没有任何子模块属性叫 `backbone`。三个键与真实参数名的关系：

| 配置键 | 命中的参数名 | 对应部件 | 意图倍率 |
|---|---|---|---|
| `backbone` | **无**（属性名是 `unet`） | SpConvUNet 主干（落空） | 0.01（未生效） |
| `reason` | `decoder.reason_model.*` | Qwen3-VL 底座内可训练部分 | 0.1 |
| `decoder` | 一切 `decoder.*`（含 reason_model、point2token、query_proj、6 层解码器） | 整个解码器 | 1.0 |

由此得到两个从源码可推导的结论（建议本地复核，见实践）：

1. **`'backbone'` 是死键**：SpConvUNet 主干实际用的是**基准学习率 1e-4**，配置「主干 0.01 倍小步走」的意图没有落进参数名空间。若你确实想给主干降 lr，把键改成 `'unet'` 才会命中（这也可作为你的第一个二次开发实验）。
2. **`'reason'` 与 `'decoder'` 同时命中 `decoder.reason_model.*`**（LoRA 包装后参数名仍带 `decoder.reason_model.base_model.model...` 前缀，见 [uniseg3d/query_decoder.py:L1008](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1008) 的 `get_peft_model` 包装）。两个键的 `lr_mult`（0.1 与 1.0）谁最终生效，取决于 mmengine `DefaultOptimizerConstructor` 对多命中的处理顺序（它对 custom_keys 有固定的排序与覆盖规则）——**本讲义写作环境未安装 mmengine，无法代跑验证，多命中优先级标记为待本地验证**，4.2.4 的实践脚本会直接把答案打印出来。

至于**哪些参数有资格进优化器**：[uniseg3d/query_decoder.py:L971](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L971) 加载 Qwen3-VL（bf16），[L978](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L978) `requires_grad_(False)` 整体冻结，[L1000-L1008](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1000-L1008) 注入 LoRA（新增可训练旁路），[L1010-L1014](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1010-L1014) 再把 `embed_tokens`/`lm_head` 解冻、`visual` 重新冻死。最终可训练集合 = LoRA 旁路 + 词嵌入两端 + point2token/query_proj/RMSNorm + 6 层解码器 + 整个 `unet`；冻结参数即便被分组，AdamW 在 `p.grad is None` 时也不会更新（mmengine 通常会直接跳过 `requires_grad=False` 的参数，同样可用实践确认）。

#### 4.2.4 代码实践：把参数分组打印出来

1. **实践目标**：用一个小到秒建的「假模型」复刻真实参数命名，套用训练配置的 `paramwise_cfg` 构建优化器，实证三个问题——(a) `'backbone'` 键命中了几个参数组？(b) `reason_model` 参数最终 lr 是 1e-5（×0.1）还是 1e-4（×1.0）？(c) 冻结参数是否被跳过？
2. **操作步骤**：在仓库根目录运行（仅依赖 mmengine，无需 GPU/数据/Qwen）：

   ```python
   # 示例代码：param_group_probe.py
   import torch.nn as nn
   from mmengine.optim import build_optim_wrapper

   # 1) 模仿 Grounded_LMM_main 的属性命名（uniseg3d.py:956-958）
   class FakeDecoder(nn.Module):
       def __init__(self):
           super().__init__()
           self.reason_model = nn.Linear(8, 8)   # → decoder.reason_model.*
           self.point2token = nn.Linear(8, 8)    # → decoder.point2token.*
           self.query_proj  = nn.Linear(8, 8)    # → decoder.query_proj.*

   class FakeLMM(nn.Module):
       def __init__(self):
           super().__init__()
           self.unet      = nn.Linear(6, 8)      # 注意：属性名是 unet，不是 backbone
           self.decoder   = FakeDecoder()
           self.criterion = nn.Linear(8, 8)      # 真实 criterion 无参数，此处仅作对照

   model = FakeLMM()
   model.decoder.reason_model.weight.requires_grad = False  # 模拟冻结的 Qwen 底座权重

   # 2) 原样照抄 ground3dlmm_train.py L197-L209 的优化器配置
   optim_cfg = dict(
       type='OptimWrapper',
       optimizer=dict(type='AdamW', lr=1e-4, weight_decay=0.05),
       paramwise_cfg=dict(
           bypass_duplicate=True,
           custom_keys={
               'backbone': dict(lr_mult=0.01),
               'reason':   dict(lr_mult=0.1),
               'decoder':  dict(lr_mult=1.0),
           }),
       clip_grad=dict(max_norm=10, norm_type=2))

   wrapper = build_optim_wrapper(model, optim_cfg)
   name_of = {id(p): n for n, p in model.named_parameters()}
   for i, g in enumerate(wrapper.optimizer.param_groups):
       names = [name_of.get(id(p)) for p in g['params']]
       print(f'group {i}: lr={g["lr"]:.2e} wd={g["weight_decay"]} -> {names}')
   ```

3. **需要观察的现象**：每个参数落到哪个组、该组的 `lr` 是多少；`unet.weight` 所在组的 lr；`decoder.reason_model.weight`（冻结）是否出现在任何组里。
4. **预期结果**：`unet.*` 的 lr 为 1e-4（证明 `'backbone'` 未生效）；`decoder.reason_model.bias`（未冻结）出现在某组且 lr 要么 1e-5 要么 1e-4（二者择一，即 `'reason'` 与 `'decoder'` 的覆盖顺序答案）；冻结的 `reason_model.weight` 要么不出现、要么出现但梯度为 None。**待本地验证**——本讲义写作环境未安装 mmengine，无法预演输出；三个问题的答案请以你机器上的打印为准。
5. 延伸：把 `custom_keys` 里的 `'backbone'` 改成 `'unet'` 再跑一次，观察 `unet.*` 的 lr 变成 1e-6——这就是「想让主干小步走」的正确写法。

#### 4.2.5 小练习与答案

**练习 1**：`dist_train.sh` 为什么在启动前探测端口？如果两份训练同时在同一台机器上启动且端口相同会发生什么？

答：`torch.distributed.launch` 的各进程靠 `--master_port` 上的 TCP 集合通信互相发现；端口被另一份训练占用时，新进程要么握手失败直接报错，要么（更糟）与毫不相干的进程组通信错乱。随机端口 + `nc -z` 探测把冲突概率降到接近零。[tools/dist_train.sh:L3-L10](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/dist_train.sh#L3-L10) 的判定是「连不上 = 空闲 = 可用」，注意别把逻辑记反。

**练习 2**：`--amp` 加上之后，配置里的哪个字段被替换成了什么？为什么不用改优化器超参？

答：[tools/train.py:L89-L101](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/train.py#L89-L101) 把 `optim_wrapper.type` 从 `OptimWrapper` 改成 `AmpOptimWrapper`，并设置 `loss_scale='dynamic'`（动态调整 loss 缩放因子避免 fp16 下溢）。`AmpOptimWrapper` 是 `OptimWrapper` 的混合精度子类，内部仍持有同一个 AdamW 与同样的 lr/wd/paramwise 分组，所以优化超参无需改动。

**练习 3**：配置键 `'reason'` 想控制的是哪些参数？这些参数训练前经历了怎样的冻结/解冻史？

答：匹配一切名字含 `reason` 的参数，实际就是 `decoder.reason_model.*`——即 [uniseg3d/query_decoder.py:L971](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L971) 加载、[L978](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L978) 冻结、[L1008](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1008) LoRA 包装（新增旁路参数天然可训练）、[L1010-L1014](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1010-L1014) `embed_tokens`/`lm_head` 解冻而 `visual` 保持冻结。所以 `'reason'` 实际能调到的是：LoRA 旁路 + 词嵌入两端（约 400M 可训练参数的大头，u4-l2）。

### 4.3 预训练权重加载：load_from 的主干初始化

#### 4.3.1 概念说明

训练的起点不是随机的：3D 稀疏主干从一份 ScanNet 上预训练好的权重出发。mmengine 里「给训练一个权重起点」有两条互斥通道：

- **`load_from`**（配置文件里写）：加载**模型权重**，非严格模式——checkpoint 里没有的键（本项目的整个 decoder）保持各自的随机/独立初始化，多余的键被忽略，**不**恢复优化器状态与 epoch 计数。用途：预训练初始化。
- **`--resume`**（命令行给）：恢复**完整训练状态**——权重 + 优化器动量 + 调度器进度 + 从第几个 epoch 继续。用途：断点续训。

本项目的 `load_from = 'work_dirs/pretrained/sstnet_scannet.pth'`（[L222](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L222)）只服务 3D 主干；语言塔的初始化走的是**另一条完全独立的路**——`_init_reason_model` 里 `from_pretrained` 直接加载 Qwen3-VL、LoRA 旁路作为新增参数随机初始化（u4-l2）。也就是说：**`load_from` 里不含任何语言模型权重，也不含 LoRA 权重**。

#### 4.3.2 核心流程

```text
Runner 初始化阶段（每个 rank 各自执行一次）
  cfg.load_from = 'work_dirs/pretrained/sstnet_scannet.pth'
    └─ Runner.load_checkpoint(非严格)
         ├─ pth 中的键 → 与 model.state_dict() 逐键比对
         ├─ 命中：unet.* 主干权重被覆盖（主干从预训练起步）
         ├─ 缺失：decoder.*（Qwen 已由 from_pretrained 加载 / LoRA 随机 / 投影层随机）→ 不报错
         └─ 多余：pth 中模型没有的键 → 忽略

命令行 --resume 时（与 load_from 互斥，tools/train.py L116-121）
  --resume            → cfg.resume=True, cfg.load_from=None     # 自动找 work_dir 最新 checkpoint
  --resume <path>     → cfg.resume=True, cfg.load_from=<path>   # 从指定文件完整恢复
```

#### 4.3.3 源码精读

**配置声明**。[configs/ground3dlmm_train.py:L222](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L222) 一行：`load_from = 'work_dirs/pretrained/sstnet_scannet.pth'`。README 对它的说明见 [README.md:L218-L221](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/README.md#L218-L221)：训练前需要把预训练稀疏卷积权重下载到 `work_dirs/pretrained/`，`sstnet_scannet.pth`「用作点编码器初始化（point-encoder initialization）」；同一节还说明 Qwen3-VL 底座由 transformers 自动下载，想用本地副本可用 `--cfg-options model.decoder.qwen_model_path=...` 覆盖。

**权重来源**：文件名 `sstnet_scannet` 指向 SSTNet 在 ScanNet 上的预训练——SpConvUNet 正是 SPFormer/SSTNet 一脉的稀疏 U-Net 实现（u3-l2 讲过其结构血缘），用同一家族的 ScanNet 语义分割预训练来热启动主干是顺理成章的选择。**README 与 docs 均未给出该 pth 的下载链接，其确切出处待确认**；本地没有这份文件时训练会在 Runner 加载阶段报文件不存在。

**resume 的互斥逻辑**。[tools/train.py:L116-L121](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/train.py#L116-L121)：

- `--resume` 不带值（`const='auto'`）：`cfg.resume=True; cfg.load_from=None`——显式**丢掉** sstnet 初始化，Runner 改为在 work_dir 里找最新 checkpoint 完整恢复（权重、优化器、epoch 一起回来）。这也解释了一个细节：**续训时 sstnet 初始化不再生效**，它只在「从零开训」时有用。
- `--resume <path>`：`cfg.resume=True; cfg.load_from=<path>`——从指定 checkpoint 完整恢复。

对比 u1-l4 的 test.py：测试入口是「位置参数无条件覆盖 `cfg.load_from`」，训练入口是「`--resume` 抢占 `load_from`」——两种入口都坚持「命令行的意图优先于配置文件」。

**关于非严格加载的边界**：mmengine `Runner.load_checkpoint` 的公开签名默认 `strict=False`、并带 `revise_keys`（剥掉 DDP 保存时可能带的 `module.` 前缀），`load_from` 走的正是这条宽松路径，因此「pth 只覆盖主干」才能成立。具体默认值以你安装的 mmengine 版本为准（4.3.4 的实践会直接打印签名核对）。

**DDD 下的一个小事实**：4 进程各自执行一次 `load_from`，即每张卡都在自己的副本上做同样的初始化——DDP 要求各 rank 初始权重严格一致，这里天然满足（同文件、同键、确定性加载）。

#### 4.3.4 代码实践：核对加载路径

1. **实践目标**：确认 (a) mmengine `load_from` 的非严格语义在**你**的版本上成立；(b) `sstnet_scannet.pth` 的键名与模型主干键的对应关系（有权重文件时）。
2. **操作步骤**：

   ```python
   # 示例代码：check_load_path.py —— 第一步：任何装好环境的机器都能跑
   import inspect
   from mmengine.runner import Runner
   print(inspect.signature(Runner.load_checkpoint))
   # 关注两个默认参数：strict 是否为 False；revise_keys 是否剥 'module.' 前缀

   # —— 第二步（可选，需要已有 sstnet_scannet.pth）：检查权重键名
   # import torch
   # ckpt = torch.load('work_dirs/pretrained/sstnet_scannet.pth', map_location='cpu')
   # sd = ckpt.get('state_dict', ckpt)
   # keys = list(sd.keys())
   # print(len(keys)); print(keys[:5])
   # 对照：模型侧主干参数名以 'unet.' 开头（uniseg3d.py:956）
   # 两边前缀对得上多少，load_from 就能覆盖多少主干权重
   ```

3. **需要观察的现象**：签名里 `strict` 的默认值；`'state_dict'` 键的存在与键名前缀（是 `unet.` 开头还是别的）。
4. **预期结果**：`strict=False` 出现在默认参数中；pth 键名与模型 `state_dict` 的 `unet.*` 前缀存在交集。两者均为**待本地验证**（本环境无 mmengine、无权重文件，无法代跑）。
5. 顺带一问自答：如果 pth 里的键是 `backbone.*` 前缀而模型属性是 `unet`，非严格加载会发生什么？——键对不上，主干保持随机初始化且**不报错**，训练照常进行。这类「静默不命中」正是 4.2 节 `'backbone'` 死键的孪生风险，读配置与读权重时都要有这根弦。

#### 4.3.5 小练习与答案

**练习 1**：`load_from` 与 `--resume` 同时存在于一次启动中（配置写了 `load_from`，命令行加了 `--resume`），最终哪个生效？

答：resume 生效且 load_from 被清除。[tools/train.py:L116-L121](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/train.py#L116-L121) 在 `--resume`（auto 或指定路径）分支里显式重写 `cfg.load_from`——命令行意图优先。续训走「完整状态恢复」，sstnet 的主干初始化仅在从零开训时起作用。

**练习 2**：为什么 LoRA 权重不需要（也不可能）出现在 `sstnet_scannet.pth` 里？训练第一个 iteration 时它们是什么值？

答：`sstnet_scannet.pth` 是 3D 稀疏主干的预训练，产生于本项目之前；LoRA 旁路是本项目 `_init_reason_model` 里由 peft 动态创建的新增参数（[uniseg3d/query_decoder.py:L1008](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1008)），pth 里自然没有对应键——非严格加载下它们保持 peft 的初始化（A/B 矩阵的默认初始化，保证初始输出为零增量），从第一个 iteration 起才被更新。

**练习 3**：README 说 Qwen3-VL 底座「会由 transformers 自动下载」，这句话对应源码里的哪一步？它和 `load_from` 有关系吗？

答：对应 [uniseg3d/query_decoder.py:L971](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L971) 的 `PointQwen3VLForConditionalGeneration.from_pretrained(qwen_model, ...)`——在模型构建（`Runner.from_cfg` 构建 model）阶段完成，与 `load_from`（Runner 权重加载阶段）是两个独立环节。`load_from` 的 pth 里没有语言塔的键，语言塔的起点完全由 `from_pretrained` + LoRA 初始化决定。

## 5. 综合实践

**任务：写一份「训练启动体检表」脚本**（示例代码，全程无需 GPU/数据/大模型权重），把本讲三个模块串起来：

```python
# 示例代码：train_preflight.py —— 在仓库根目录运行
"""训练前体检：配置合并 / 优化器分组 / 权重加载路径 三连查"""
import inspect
from mmengine.config import Config
from mmengine.optim import build_optim_wrapper
from mmengine.runner import Runner
import torch.nn as nn

print('== 1. 配置合并检查 ==')
cfg = Config.fromfile('configs/ground3dlmm_train.py')
ds = cfg.train_dataloader.dataset
assert ds.type == 'ConcatDataset', f'意外的 dataset type: {ds.type}'
for d in ds.datasets:
    print(f'  [OK] 子数据集 {d.type} | {d.ann_file} | root={d.data_root}')
print(f'  [OK] batch_size={cfg.train_dataloader.batch_size}, workers={cfg.train_dataloader.num_workers}')
print(f'  [{"OK" if cfg.val_dataloader is None else "WARN"}] val_dataloader={cfg.val_dataloader}')
print(f'  [INFO] max_epochs={cfg.train_cfg.max_epochs}, load_from={cfg.load_from}')

print('== 2. 优化器分组检查（用假模型复刻真实参数名）==')
class FakeDecoder(nn.Module):
    def __init__(self):
        super().__init__()
        self.reason_model = nn.Linear(8, 8)
        self.point2token = nn.Linear(8, 8)
class FakeLMM(nn.Module):
    def __init__(self):
        super().__init__()
        self.unet = nn.Linear(6, 8)
        self.decoder = FakeDecoder()

model = FakeLMM()
wrapper = build_optim_wrapper(model, cfg.optim_wrapper)
name_of = {id(p): n for n, p in model.named_parameters()}
unet_lr = next(g['lr'] for g in wrapper.optimizer.param_groups
               if any('unet' in name_of.get(id(p), '') for p in g['params']))
base_lr = cfg.optim_wrapper.optimizer.lr
print(f'  [关键] 主干(unet.*)实际 lr = {unet_lr:.2e}，基准 = {base_lr:.0e}'
      f' → "backbone" 键{"未命中（死键）" if unet_lr == base_lr else "已命中"}')

print('== 3. 权重加载路径检查 ==')
print('  [INFO] Runner.load_checkpoint 签名:', inspect.signature(Runner.load_checkpoint))
import os
print(f'  [{"OK" if os.path.exists(cfg.load_from) else "缺失"}] load_from 文件: {cfg.load_from}')
```

执行后对照三个判读点：

1. 两个子数据集路径与你的 `data/` 目录是否对得上（对不上训练会在首次取数时崩）。
2. `unet.*` 的实际 lr——验证 4.2 节的「死键」结论在**你的 mmengine 版本**上是否复现；再手工把配置键改名为 `'unet'` 跑一次，看 lr 降为 1e-6。
3. `load_from` 文件是否存在、`strict` 默认值是多少——决定你的主干到底有没有吃到预训练。

最后（可选，需要 GPU 与已备好的数据/权重）做一次**最远推进演练**：`python tools/train.py configs/ground3dlmm_train.py --launcher none`，观察它依次构建模型（下载/加载 Qwen）、构建数据集、加载 `load_from`，在第一个缺失依赖处停下——报错位置就是你下一步要补的东西。**待本地验证**；演练请在独占卡上进行，不要在共享 GPU 上盲跑。

## 6. 本讲小结

- 训练配置是一张「五问清单」：`model`（是什么）、`train_dataloader`（数据哪来）、`optim_wrapper`+`param_scheduler`+`train_cfg`（怎么优化）、`load_from`（从哪起步）、`hooks`（何时存档记日志）；[configs/ground3dlmm_train.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py) 233 行给出了完整答案。
- 双域联合训练靠 `ConcatDataset`：两个 `Part3DGlamm_Dataset` 只差 `data_root`/`ann_file`，靠「裸文件名契约」共享同一条 pipeline；val/test 显式置 `None` 防止继承污染。
- 启动链是三层薄壳：`train.sh`（选卡）→ `dist_train.sh`（探测空闲端口 + `torch.distributed.launch` 起 N 进程）→ `tools/train.py`（mmdet3d 原样拷贝，CLI 合并进配置后交给 `Runner.from_cfg`）；`--amp` 只是给 `OptimWrapper` 换壳，`--resume` 与 `load_from` 互斥且命令行优先。
- `paramwise_cfg` 按**参数名子串**分组调 lr：配置键 `'reason'`/`'decoder'` 命中 `decoder.reason_model.*` 等语言塔可训练参数（LoRA + 词嵌入两端）；而 `'backbone'` 匹配不到任何参数（主干属性名是 `unet`），SpConvUNet 实际以基准 lr=1e-4 训练——源码层面可证、值得本地复核的「死键」。
- `load_from='sstnet_scannet.pth'` 以非严格方式只初始化 3D 主干；语言塔由 `from_pretrained` 独立加载、LoRA 随机起步，二者互不相关；`--resume` 时主干初始化被丢弃、完整训练状态恢复。
- 调参入口集中在 `epoch`（一处改两处生效）、`optim_wrapper.optimizer.lr`、`train_dataloader.batch_size`、`model.decoder.lora_r/lora_alpha/max_point_tokens`，与 [README.md:L230-L241](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/README.md#L230-L241) 的官方表一一对应。

## 7. 下一步学习建议

训练跑起来、checkpoint 存进 `work_dirs/ground3dlmm/` 之后，自然进入单元 6「评估与可视化」：

- 下一讲 [u6-l1 分割指标 Part3DGlamm_SegMetric：mIoU 怎么算]——训练产出的 mask 预测如何在评估端与 GT 对齐、IoU 如何逐 QA 聚合；
- 随后 [u6-l2 评估启动器 eval_ground3d.sh 与配置族] 会复用本讲的「配置继承 + CLI 优先」经验，并处理训练 lr 分层带来的姊妹问题：checkpoint 的 LoRA rank 与评估配置失配会被静默吞掉；
- 若你打算动训练配置做二次开发，建议先做本讲第 5 节的体检表脚本，再进入 u7-l2（新增子任务/数据集/换底座的改动链）。
