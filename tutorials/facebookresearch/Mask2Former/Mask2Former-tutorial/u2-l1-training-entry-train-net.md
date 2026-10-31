# 训练入口 train_net.py 主流程

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `setup()` 中五层配置的合并顺序，以及 `freeze()` 与 `default_setup()` 各自做了什么。
2. 讲清 `main()` 的两条岔路：`--eval-only` 评估分支与正常训练分支，以及 `--resume` 参数在两条路上分别意味着什么。
3. 列出 `Trainer` 子类覆写的四个 `build_*` 类方法，并说明它们各自**被谁、在什么时机调用**。
4. 理解 `launch()` 如何用同一份代码兼容单卡、单机多卡、多机多卡三种运行形态。

本讲是「数据与训练」单元的第一讲：先站在**引擎驾驶员**的视角看清整个训练流程的骨架，后面几讲再分别深入数据注册、dataset mapper 和优化器细节。

## 2. 前置知识

- **训练脚本与 demo 脚本的区别**：u1-l4 里跑的 `demo/demo.py` 只做推理；而 `train_net.py` 要同时覆盖「训练 + 评估 + 断点续训 + 多卡」四种场景，所以它的入口逻辑更像一个调度中枢。
- **detectron2 的 DefaultTrainer**：Mask2Former 仓库本身不写训练循环。梯度前向、反向、日志、checkpoint 保存、周期性评估这些「引擎」代码全部在宿主框架 detectron2 的 `DefaultTrainer` 里；本仓库通过**继承并覆写少数几个类方法**，把 Mask2Former 特有的数据加载器、优化器、评估器「插」进去。这正是 u1-l3 讲过的「detectron2 插件式项目」在训练侧的体现。
- **类方法（classmethod）与子类覆写**：`Trainer` 覆写的 `build_evaluator`、`build_train_loader`、`build_lr_scheduler`、`build_optimizer` 都是类方法。detectron2 的引擎代码里写的是 `cls.build_xxx(...)`，`cls` 在运行时指向子类 `Trainer`，因此引擎「无意间」就调用了插件版本——这是 Python 多态在训练框架里的标准用法。
- **分布式训练三个词**：
  - `rank`：全局进程编号（从 0 开始）；
  - `world_size`：所有机器上参与训练的进程总数；
  - `dist_url`：各进程互相「接头」用的 TCP 地址（init process group 的 master 地址）。
- **resume 与 load 的区别**：
  - `resume=True`：从 `OUTPUT_DIR` 里上一次的 checkpoint **连同优化器状态、迭代步数一起**恢复，训练从中断处继续；
  - `resume=False`：只把 `MODEL.WEIGHTS` 指向的权重**载入模型参数**（比如 ImageNet 预训练的 R-50），迭代从 0 重新开始。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [train_net.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py) | 图像版训练/评估入口，本讲主角，全文约 330 行 |
| [GETTING_STARTED.md](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/GETTING_STARTED.md) | 官方训练与评估命令速查 |
| [configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml) | 模型侧配置（META_ARCHITECTURE、损失权重等） |
| [configs/coco/panoptic-segmentation/Base-COCO-PanopticSegmentation.yaml](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/panoptic-segmentation/Base-COCO-PanopticSegmentation.yaml) | 任务侧基座配置（数据集、SOLVER、mapper） |
| [mask2former/__init__.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/__init__.py) | train_net.py 第 50 行 `from mask2former import ...` 的全部来源，import 即注册 |
| [mask2former/maskformer_model.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py) | 训练分支返回的 loss dict 在这里产生（本讲只看键名，细节留给单元五） |
| [MODEL_ZOO.md](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/MODEL_ZOO.md) | 下载预训练权重，评估实践要用 |

detectron2 侧的 `DefaultTrainer` 与 `launch` 不在本仓库内（本机也未随仓库分发），本讲对它们的行为给出**时序描述与伪代码**，并附 detectron2 官方源码链接（不带行号锚点，以你安装的版本为准）：

- [detectron2/engine/launch.py](https://github.com/facebookresearch/detectron2/blob/main/detectron2/engine/launch.py)
- [detectron2/engine/defaults.py](https://github.com/facebookresearch/detectron2/blob/main/detectron2/engine/defaults.py)（`DefaultTrainer`）

## 4. 核心概念与源码讲解

### 4.1 模块一：`setup()` —— 从命令行到冻结配置

#### 4.1.1 概念说明

`setup(args)` 负责把「detectron2 默认值 + Mask2Former 扩展键 + yaml 文件 + 命令行覆盖」四路输入捏成一份最终配置，并在冻结后做基础初始化（输出目录、日志、随机种子等）。u1-l5 已经详细讲过配置系统本身，这里从**入口视角**再走一遍，并补上两个 u1-l5 未展开的点：`freeze()` 的效果和 `default_setup` / `setup_logger` 的作用。

#### 4.1.2 核心流程

```text
args（已由 default_argument_parser 解析）
   │
   ▼
cfg = get_cfg()                      # 第 1 层：detectron2 全量默认键
add_deeplab_config(cfg)              # 第 2 层：deeplab 的 poly lr 等键
add_maskformer2_config(cfg)          # 第 3 层：Mask2Former 专属键（u1-l5）
cfg.merge_from_file(args.config_file) # 第 4 层：yaml（含 _BASE_ 继承链）
cfg.merge_from_list(args.opts)       # 第 5 层：命令行 opts，优先级最高
cfg.freeze()                         # 变为只读
default_setup(cfg, args)             # 输出目录、日志、种子等框架级初始化
setup_logger(..., name="mask2former")# 额外给 mask2former 包配一个日志器
   │
   ▼
返回 cfg（整个进程后续共用这一份）
```

关键点：**五层顺序不可颠倒**。命令行 `--opts KEY VALUE` 之所以能覆盖 yaml，就是因为 `merge_from_list` 排在最后；而所有 `KEY` 必须在前面三层里已被注册，否则 `merge_from_list` 会因「未知键」报错。

#### 4.1.3 源码精读

入口的三段式骨架（本讲全篇围绕这三段展开）：

[train_net.py:281-295](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L281-L295) — `setup()` 函数全文：先 `get_cfg()` 建空配置树，再依次叠加 deeplab 键、Mask2Former 键、yaml、命令行，然后 `freeze()` 冻结、`default_setup` 做框架初始化，最后为 `mask2former` 这个 logger 名字单独配置输出，使模型内部 `logging.getLogger("mask2former")` 的日志同样落到 `OUTPUT_DIR`。

[train_net.py:49-59](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L49-L59) — `from mask2former import (...)`：把 5 种 dataset mapper、`InstanceSegEvaluator`、`SemanticSegmentorWithTTA`、`add_maskformer2_config` 一次性拿进来。这个 import 同时触发 u1-l3 讲过的「import 即注册」副作用——没有它，后面 `build_model` 会报 `Cannot find object 'MaskFormer' in registry`。对照 [mask2former/__init__.py:1-27](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/__init__.py#L1-L27) 可以看到这些名字的真正出处。

[configs/coco/panoptic-segmentation/Base-COCO-PanopticSegmentation.yaml:16-35](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/panoptic-segmentation/Base-COCO-PanopticSegmentation.yaml#L16-L35) — 第 4 层 yaml 的「任务侧」内容：训练/测试数据集名、`IMS_PER_BATCH: 16`、`OPTIMIZER: "ADAMW"`、`BACKBONE_MULTIPLIER: 0.1`、`CLIP_GRADIENTS`（full_model 裁剪）与 `AMP` 混合精度开关。这些键随后会分别被 `build_train_loader`、`build_optimizer`、DefaultTrainer 消费。

[configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml:1-46](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml#L1-L46) — 第 4 层 yaml 的「模型侧」内容：第 1 行 `_BASE_` 指向上一个文件，第 3 行 `META_ARCHITECTURE: "MaskFormer"` 决定 `build_model` 从注册表里取出哪个类，第 19-39 行的损失权重与 `DEC_LAYERS: 10`（注释写明「9 层解码器 + 1 个用于 learnable query 损失」）决定了训练日志里会出现哪些 loss 键（见 4.3.4 实践）。

`freeze()` 之后任何 `cfg.X = y` 赋值都会抛 `AttributeError`，这就是配置「全进程只读一份」的保障；`default_setup` 则负责 `OUTPUT_DIR` 创建、随机种子设置、把完整配置与命令行参数写进日志等框架初始化（行为以 detectron2 版本为准）。

#### 4.1.4 代码实践

**实践目标**：不真正启动训练，单独观察 `setup()` 的产物——最终合并出的配置与 `freeze()` 的只读约束。

**操作步骤**（需要 u1-l2 搭好的环境）：

1. 在任意可 import 本仓库的目录新建脚本 `inspect_setup.py`（**示例代码**，建议放在你自己的目录而不是仓库内）：

```python
# 示例代码：手动复刻 train_net.setup() 的前 6 行
import sys
from detectron2.config import get_cfg
from detectron2.projects.deeplab import add_deeplab_config
from mask2former import add_maskformer2_config

cfg = get_cfg()
add_deeplab_config(cfg)
add_maskformer2_config(cfg)
cfg.merge_from_file(
    "configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml"
)
print("META_ARCHITECTURE =", cfg.MODEL.META_ARCHITECTURE)
print("OPTIMIZER =", cfg.SOLVER.OPTIMIZER)
print("MAPPER =", cfg.INPUT.DATASET_MAPPER_NAME)
# 模拟第 5 层：命令行覆盖
cfg.merge_from_list(["SOLVER.MAX_ITER", "10"])
print("MAX_ITER after opts =", cfg.SOLVER.MAX_ITER)
cfg.freeze()
try:
    cfg.SOLVER.MAX_ITER = 20   # 冻结后再赋值
except AttributeError as e:
    print("frozen ->", type(e).__name__)
```

2. 运行：`python inspect_setup.py`（在仓库根目录下）。
3. 再试一次把 `merge_from_list(["NOT_A_KEY", "1"])` 加进去，观察报错信息。

**需要观察的现象**：五个打印依次给出 `MaskFormer`、`ADAMW`、`coco_panoptic_lsj`、`10`、`frozen -> AttributeError`；未知键会让 `merge_from_list` 抛 `Non-existent config key` 类错误。

**预期结果**：直观验证「五层叠加、后写覆盖先写、冻结只读、未注册键不可覆盖」四条规则。若你的环境与上述输出不符，请以实际输出为准（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `add_maskformer2_config(cfg)` 必须在 `merge_from_file` 之前调用？
**答案**：`merge_from_file` / `merge_from_list` 只能修改**已存在**的键。Mask2Former 的专属键（如 `MODEL.MASK_FORMER.*`）由 `add_maskformer2_config` 注册；若注册在后，yaml 里的这些键会因「不存在」而报错，等于配置树还没有为 yaml 做好准备。

**练习 2**：`setup()` 末尾为什么要再调用一次 `setup_logger(..., name="mask2former")`？
**答案**：`default_setup` 只配置 detectron2 默认 logger；Mask2Former 源码内部用 `logging.getLogger("mask2former")` 输出模型构建等日志，单独再设一个 logger 才能让这些日志同样按等级写入 `OUTPUT_DIR` 的 log 文件，多卡时还通过 `distributed_rank` 保证只有主进程写文件。

---

### 4.2 模块二：`main()` —— 训练与评估的岔路口

#### 4.2.1 概念说明

`main(args)` 只做一件事：根据 `args.eval_only` 决定走「评估」还是「训练」。评估分支**不实例化 Trainer**，直接以类方法的方式调用 `Trainer.build_model` 和 `Trainer.test`；训练分支才真正 `Trainer(cfg)` 构造出完整的训练器。`args.resume` 在两条路上的含义不同：评估分支里它决定 `resume_or_load` 是「恢复优化器状态」（无意义但合法）还是「仅加载权重」；训练分支里它决定是否从上次的 checkpoint 续训。

#### 4.2.2 核心流程

```text
main(args)
 ├── cfg = setup(args)
 ├──
 if args.eval_only:                        # 路线 A：只评估
 │    model = Trainer.build_model(cfg)     #   从 META_ARCHITECTURE 注册表取 MaskFormer
 │    DetectionCheckpointer(model, ...).resume_or_load(
 │        cfg.MODEL.WEIGHTS, resume=args.resume)
 │    res = Trainer.test(cfg, model)       #   遍历 DATASETS.TEST，逐个 build_evaluator + 推理
 │    if cfg.TEST.AUG.ENABLED:
 │        res.update(Trainer.test_with_TTA(cfg, model))   # 追加一次 TTA 评估
 │    if comm.is_main_process():
 │        verify_results(cfg, res)         #   只有主进程核对 EXPECTED_RESULTS
 │    return res
 │
 else:                                     # 路线 B：训练
     trainer = Trainer(cfg)                #   构造时即触发 build_model/optimizer/loader/scheduler
     trainer.resume_or_load(resume=args.resume)
     return trainer.train()                #   训练循环；按 TEST.EVAL_PERIOD 周期性调用 cls.test
```

#### 4.2.3 源码精读

[train_net.py:298-315](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L298-L315) — `main()` 全文。上半段（301-311 行）是评估分支：`Trainer.build_model(cfg)` 是从 `DefaultTrainer` 原样继承的类方法，内部走 `META_ARCHITECTURE_REGISTRY`；`DetectionCheckpointer(...).resume_or_load(cfg.MODEL.WEIGHTS, resume=args.resume)` 把模型 zoo 的 `.pkl` 权重灌进模型；`Trainer.test` 会在内部为 `cfg.DATASETS.TEST` 里的每个数据集调用 4.3 节讲的 `build_evaluator`。下半段（313-315 行）是训练分支：构造 `Trainer` 实例、恢复/加载权重、`train()` 开跑。

[train_net.py:264-278](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L264-L278) — `test_with_TTA`：把模型包进 `SemanticSegmentorWithTTA`（u10-l3 精讲），评估器输出目录换成 `inference_TTA`，结果键名统一追加 `_TTA` 后缀后合并进 `res`。注意它只在 `cfg.TEST.AUG.ENABLED` 为真时被评估分支调用，普通训练不受影响。

[GETTING_STARTED.md:54-59](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/GETTING_STARTED.md#L54-L59) — 官方评估命令模板：`python train_net.py --config-file <yaml> --eval-only MODEL.WEIGHTS /path/to/checkpoint_file`。对比 `main()` 源码可知，这条命令正对应路线 A，且 `--resume` 缺省为 False，即「只加载权重、不动迭代计数」。

[MODEL_ZOO.md:53-62](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/MODEL_ZOO.md#L53-L62) — R50 全景分割条目：表中 PQ 51.9，权重为 `model_final_94dc52.pkl`。评估实践的「预期结果」就以这行指标为参照。

两个容易忽略的细节：

- `comm.is_main_process()` 守卫 `verify_results`：多卡时只有 rank 0 做结果核对，避免每个进程重复打印/写文件。
- `verify_results(cfg, res)` 只在 `cfg.TEST.EXPECTED_RESULTS` 非空时才有实际比较动作，默认配置下基本是空操作（行为以 detectron2 版本为准）。

#### 4.2.4 代码实践

**实践目标**：完整跑一次 `--eval-only` 评估，对照 MODEL_ZOO 指标；同时核对 `resume` 语义。

**操作步骤**：

1. 按 `datasets/README.md` 准备 COCO 全景分割数据（u2-l2 精讲注册细节）。
2. 从 [MODEL_ZOO.md:61](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/MODEL_ZOO.md#L61) 的链接下载 `model_final_94dc52.pkl`。
3. 执行（完整验证集，8 卡约数小时；数据不足时可在自己注册的小子集上做冒烟验证）：

```bash
python train_net.py \
  --config-file configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml \
  --eval-only \
  MODEL.WEIGHTS /path/to/model_final_94dc52.pkl
```

4. 训练分支对照：把 `--eval-only` 去掉、加上 `--resume` 重新运行，观察日志是从 `Starting from iteration 0` 还是从上次保存的 iteration 恢复。

**需要观察的现象**：评估分支依次打印模型构建、权重加载（checkpointer 日志列出各模块加载情况）、随后按 `DATASETS.TEST` 里的 `coco_2017_val_panoptic_with_sem_seg` 同时输出全景 PQ、实例 AP、语义 mIoU 三组指标（因为 Base 配置把三个 TEST 开关都打开了，见 [configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml:40-43](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml#L40-L43)）。

**预期结果**：PQ 接近 51.9（浮点与环境会有小差异）；`--resume` 场景下 `resume_or_load` 优先找 `OUTPUT_DIR` 内的上次 checkpoint，找不到时退回加载 `MODEL.WEIGHTS`。若没有完整 COCO 数据，此实践退化为「命令与日志阅读型」，标注待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`--eval-only` 分支为什么不需要构造 `Trainer` 实例？
**答案**：评估只需要「一个模型 + 一组评估器 + 遍历测试集推理」，不需要优化器、训练数据加载器和训练循环。`Trainer.build_model` / `Trainer.test` / `Trainer.build_evaluator` 都是类方法，不依赖实例状态，所以直接以类名调用即可，省去构造训练器的开销。

**练习 2**：`--eval-only` 时误加了 `--resume` 会发生什么？
**答案**：`resume_or_load(path, resume=True)` 会尝试从 `save_dir` 里的 `last_checkpoint` 恢复（含优化器状态、迭代数），评估场景下这些状态用不上；实际效果通常等同于加载了更完整的 checkpoint 后照常评估，对指标没有正面或负面影响（以 detectron2 `DetectionCheckpointer` 实现为准）。

---

### 4.3 模块三：`Trainer` —— 四个 build_* 方法与调用时机

#### 4.3.1 概念说明

`Trainer(DefaultTrainer)` 是插件与引擎的接缝。本仓库覆写了**四个**类方法（`build_evaluator`、`build_train_loader`、`build_lr_scheduler`、`build_optimizer`），外加一个 `test_with_TTA`；而 `build_model` **没有覆写**——模型本身的组装完全交给注册表 + `from_config` 机制（u3-l1、u3-l2 精讲）。「谁在什么时候调用这四个方法」是理解整个训练流程的钥匙：**它们不是被 train_net.py 自己调用的，而是被 detectron2 引擎在特定时机回调的**。

#### 4.3.2 核心流程

`Trainer(cfg)` 构造函数完全继承自 `DefaultTrainer.__init__`，其内部按固定顺序回调（以 detectron2 版本为准，见 [detectron2/engine/defaults.py](https://github.com/facebookresearch/detectron2/blob/main/detectron2/engine/defaults.py)）：

| 顺序 | 回调方法 | 定义处 | 时机与用途 |
| --- | --- | --- | --- |
| 1 | `cls.build_model(cfg)` | 继承，未覆写 | `__init__` 一开始；从 `META_ARCHITECTURE_REGISTRY` 取 `MaskFormer` 并用 `from_config` 组装 |
| 2 | `cls.build_optimizer(cfg, model)` | **本文件 L183 覆写** | 紧随其后；按模块名分组参数、设置 lr/weight decay、套梯度裁剪 |
| 3 | `cls.build_train_loader(cfg)` | **本文件 L149 覆写** | 再下一步；按 `INPUT.DATASET_MAPPER_NAME` 选 5 种 mapper 之一 |
| 4 | （DDP 包装 + SimpleTrainer/AMPTrainer 创建） | detectron2 | 模型包分布式外壳，训练器持有 model/optimizer/loader 三件套 |
| 5 | `cls.build_lr_scheduler(cfg, optimizer)` | **本文件 L175 覆写** | 优化器就绪后；转调 deeplab 的 poly lr 调度器（u2-l4 精讲） |
| 6 | checkpoint / hooks 注册（`build_hooks`） | detectron2 | 周期写日志、按 `TEST.EVAL_PERIOD` 挂 EvalHook、周期保存 checkpoint |

评估期的调用链则是：`Trainer.test(cfg, model)`（类方法，detectron2 提供）→ 对 `cfg.DATASETS.TEST` 中**每个**数据集调用 `cls.build_evaluator(cfg, name)` → `inference_on_dataset(model, loader, evaluator)`。训练中每过 `TEST.EVAL_PERIOD` 次迭代，EvalHook 会再次走同一条链。

```text
Trainer(cfg)
 ├─ build_model            ← 覆盖：无（注册表）
 ├─ build_optimizer        ← 覆盖：L184
 ├─ build_train_loader     ← 覆盖：L150
 ├─ DDP + (AMP)SimpleTrainer
 ├─ build_lr_scheduler     ← 覆写：L176
 └─ hooks（writer / EvalHook / checkpointer）
trainer.train()  ──每 EVAL_PERIOD──▶  Trainer.test ──▶  build_evaluator（覆写：L68）
```

#### 4.3.3 源码精读

[train_net.py:62-65](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L62-L65) — `class Trainer(DefaultTrainer)` 与 docstring：明确说明这是「适配 MaskFormer 的 Trainer 扩展」，全文件唯一一个类，约 220 行都在为它添方法。

[train_net.py:149-173](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L149-L173) — `build_train_loader`：一条 if-elif 链按 `cfg.INPUT.DATASET_MAPPER_NAME` 五选一（`mask_former_semantic` / `mask_former_panoptic` / `mask_former_instance` / `coco_instance_lsj` / `coco_panoptic_lsj`），前三种是 MaskFormer 一代风格、后两种是 LSJ 新基线；最终统一交给 detectron2 的 `build_detection_train_loader(cfg, mapper=mapper)`。R50 全景配置取的是 `coco_panoptic_lsj` → `COCOPanopticNewBaselineDatasetMapper`（u2-l3 精讲）。

[train_net.py:175-181](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L175-L181) — `build_lr_scheduler`：仅一行转调 `detectron2.projects.deeplab` 的 `build_lr_scheduler`（poly 学习率策略）。覆写的意义是「换掉默认的 step/multi-step 调度器」，细节在 u2-l4。

[train_net.py:183-262](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L183-L262) — `build_optimizer`：遍历 `model.named_modules()` 逐参数建超参组——模块名含 `backbone` 则 lr 乘 `SOLVER.BACKBONE_MULTIPLIER`（Base 配置为 0.1）；参数名含 `relative_position_bias_table` / `absolute_pos_embed` 则 weight decay 置 0（Swin 位置参数免衰减）；norm 层用 `WEIGHT_DECAY_NORM`；Embedding 用 `WEIGHT_DECAY_EMBED`。L232-247 内嵌 `maybe_add_full_model_gradient_clipping`：当 `CLIP_TYPE == "full_model"` 时返回一个「step 前先对全部参数做 `clip_grad_norm_`」的优化器子类。u2-l4 会逐行拆解，本讲只需记住**它是被 `Trainer.__init__` 在第 2 步回调的**。

[train_net.py:67-147](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L67-L147) — `build_evaluator`：按数据集元数据 `evaluator_type` 与 `MODEL.MASK_FORMER.TEST` 三开关组合出评估器列表，如 `coco_panoptic_seg` 在 `PANOPTIC_ON` 下加 `COCOPanopticEvaluator`（99-100 行）、再按 `INSTANCE_ON` / `SEMANTIC_ON` 追加 `COCOEvaluator` / `SemSegEvaluator`（102-105 行）；单个则直接返回、多个则 `DatasetEvaluators` 打包（145-147 行）。分发逻辑的完整拆解在 u7-l1。

[train_net.py:313-315](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L313-L315) — 训练分支三行：`Trainer(cfg)`（触发 4.3.2 时序表的 1-6 步）→ `trainer.resume_or_load(resume=args.resume)` → `trainer.train()`。这三个调用就是四个 `build_*` 被触发的唯一入口。

#### 4.3.4 代码实践

**实践目标**：(a) 单卡短训练 10 个 iteration，观察日志与 `OUTPUT_DIR/metrics.json` 中的 loss dict 键名；(b) 用打点版 Trainer 亲自确认四个 `build_*` 的调用顺序。

**操作步骤**：

1. 数据就绪后，单卡短训练（GETTING_STARTED 提醒：单卡需自行调小 batch 与 lr）：

```bash
python train_net.py \
  --config-file configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml \
  --num-gpus 1 \
  SOLVER.IMS_PER_BATCH 2 SOLVER.BASE_LR 0.0001 \
  SOLVER.MAX_ITER 10 TEST.EVAL_PERIOD 0 \
  OUTPUT_DIR output/smoke_10iter
```

2. 打开 `output/smoke_10iter/metrics.json`（或终端日志行），列出全部 `loss_*` 键。
3. 打点验证调用顺序：**示例代码**——在仓库外新建 `trace_trainer.py`，import `train_net` 里的 `Trainer` 后包一层：

```python
# 示例代码：给四个 build_* 打点，观察 detectron2 引擎的回调顺序
import train_net

def trace(name, fn):
    def wrapper(*a, **k):
        print(f">>> enter {name}")
        out = fn(*a, **k)
        print(f"<<< exit  {name}")
        return out
    return wrapper

for m in ["build_evaluator", "build_train_loader", "build_lr_scheduler", "build_optimizer"]:
    setattr(train_net.Trainer, m, classmethod(trace(m, getattr(train_net.Trainer, m).__func__)))

import sys
sys.argv = ["train_net.py", "--config-file",
            "configs/coco/panoptic-segmentation/maskformer2_R50_bs16_50ep.yaml",
            "--num-gpus", "1", "SOLVER.IMS_PER_BATCH", "2", "SOLVER.BASE_LR", "0.0001",
            "SOLVER.MAX_ITER", "10", "TEST.EVAL_PERIOD", "0", "OUTPUT_DIR", "output/smoke_trace"]
train_net.main(train_net.default_argument_parser().parse_args())
```

4. 运行 `python trace_trainer.py`，记录打点行的先后顺序。

**需要观察的现象**：步骤 1 的日志应出现 enter/exit 序列：`build_optimizer` → `build_train_loader` → `build_lr_scheduler`（`build_model` 未打点，在最前）；若 `TEST.EVAL_PERIOD` 非 0，训练结束或周期点还会出现 `build_evaluator`。步骤 2 的 metrics.json 中 loss 键应为 `loss_ce`、`loss_mask`、`loss_dice` 及其后缀 `_0` 到 `_8` 共 9 组辅助键——合计 3 × (9+1) = 30 个键。

**预期结果**：30 个键的来源可对照 [mask2former/maskformer_model.py:118-125](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L118-L125)：`DEC_LAYERS: 10` 减 1 得 9 个中间层，`aux_weight_dict` 给三种损失各加 `_0`…`_8` 后缀；而 [mask2former/maskformer_model.py:209-217](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L209-L217) 显示前向只在 `training` 分支返回这个 dict 并按 `weight_dict` 加权、过滤未知键。若数据未就绪或输出键数不同，请以你环境的实际输出为准（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：`build_model` 没有被覆写，Mask2Former 的模型是如何被「换」成自己的？
**答案**：靠配置 + 注册表。`cfg.MODEL.META_ARCHITECTURE = "MaskFormer"`，detectron2 默认的 `build_model` 用这个名字去 `META_ARCHITECTURE_REGISTRY` 查找；而 `MaskFormer` 类早在 `import mask2former` 时（u1-l3 的「import 即注册」）就被 `@META_ARCHITECTURE_REGISTRY.register()` 登记进去了。所以换模型不需要覆写 `build_model`，只要换配置里的名字。

**练习 2**：为什么 `build_optimizer` 必须在 `build_train_loader` 之前被调用也能工作，顺序反过来行吗？
**答案**：在本仓库的组件关系里二者没有数据依赖（optimizer 只依赖 model，loader 只依赖 cfg），理论上互换不影响正确性；但 detectron2 的 `DefaultTrainer.__init__` 把顺序写死为 model → optimizer → data_loader → trainer → scheduler，插件无法改变回调顺序，只能改变每个回调的返回内容。理解这一点有助于排查「想自定义组件却不知从哪儿插」的问题。

**练习 3**：训练日志里周期性出现评估输出，是 `main()` 里哪行代码触发的？
**答案**：不是 `main()` 触发的。`main()` 只调用 `trainer.train()`；周期评估来自 detectron2 `DefaultTrainer.build_hooks` 注册的 EvalHook（按 `TEST.EVAL_PERIOD`），它在训练循环内回调 `cls.test`，进而调用本文件覆写的 `build_evaluator`。

---

### 4.4 模块四：`launch` —— 从单卡到多机多卡

#### 4.4.1 概念说明

文件末尾的 `if __name__ == "__main__"` 块是进程真正的主入口。它自己不碰 cfg，而是把 `main` 函数交给 detectron2 的 `launch`，由 `launch` 决定「在几个进程里、以什么分布式身份」运行 `main`。这样单卡、单机多卡、多机多卡共用同一份代码，训练逻辑完全无感。

#### 4.4.2 核心流程

`launch` 的行为概要（以 detectron2 [engine/launch.py](https://github.com/facebookresearch/detectron2/blob/main/detectron2/engine/launch.py) 为准）：

```text
world_size = num_machines × num_gpus
if world_size == 1:
    main(args)                         # 单进程直接执行，不起分布式
else:
    为每张卡 spawn 一个子进程：
      设置 LOCAL_RANK / 全局 rank = machine_rank * num_gpus + local_rank
      用 dist_url（未指定则自动挑端口）init_process_group
      每个子进程都各自执行一遍 main(args)
```

于是**每个进程都会完整地走一遍 `setup` → `main`**：各自读取同一份 yaml（保证一致），各自构建模型后由 DDP 同步梯度；只有 rank 0（主进程）写日志与 checkpoint，这正是前面 `comm.is_main_process()` 守卫存在的原因。

#### 4.4.3 源码精读

[train_net.py:318-328](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/train_net.py#L318-L328) — 主入口块：`default_argument_parser().parse_args()` 解析 `--config-file`、`--num-gpus`、`--num-machines`、`--machine-rank`、`--dist-url`、`--resume`、`--eval-only` 及尾部 `opts`；先打印 `Command Line Args:`（排障第一手信息，日志第一行就是它），再把 `main` 连同这些参数一起交给 `launch`。

[GETTING_STARTED.md:41-52](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/GETTING_STARTED.md#L41-L52) — 官方两种启动方式：默认 `--num-gpus 8`（配置按 8 卡撰写，`IMS_PER_BATCH: 16` 即每卡 2 图）；单卡启动需同时用命令行调小 `SOLVER.IMS_PER_BATCH` 与 `SOLVER.BASE_LR`——这正是 4.3.4 实践命令的依据，也说明 `launch` 只改变进程拓扑，不改配置语义，batch/lr 的适配要用户自己负责。

参数与形态的对应关系：

| 命令 | world_size | 进程形态 |
| --- | --- | --- |
| `--num-gpus 1`（缺省） | 1 | 当前进程直接执行 `main`，无 DDP |
| `--num-gpus 8` | 8 | 单机 spawn 8 进程，DDP 全卡 |
| `--num-gpus 8 --num-machines 2 --machine-rank 1 --dist-url tcp://主节点IP:端口` | 16 | 每台机器各 spawn 8 进程，靠 `dist_url` 会合 |

#### 4.4.4 代码实践

**实践目标**：观察 `launch` 在 world_size=1 与 >1 时的两种执行形态。

**操作步骤**：

1. 复用 4.3.4 的短训练命令（`--num-gpus 1`），在日志最前面找到 `Command Line Args:` 与 `launch` 相关输出，确认没有多进程 spawn。
2. 若有 ≥2 张卡，把 `--num-gpus` 改成 2 再跑一次，观察终端出现两份交错日志（local_rank 0/1），以及 `OUTPUT_DIR` 只由 rank 0 写入。
3. 无多卡环境时做源码阅读型实践：打开 detectron2 的 `engine/launch.py`（链接见上），画出 `_distributed_worker` 与 `launch` 的调用关系伪代码，标出 `dist_url` 自动选端口的位置。

**需要观察的现象**：单卡日志只有一份、开头无 `local_rank` 字样；多卡日志按进程交错、`setup` 的配置打印会在每个进程各出现一次（内容完全相同）。

**预期结果**：验证「每个进程各自完整执行 `main`，靠 rank 区分职责」的分布式模型；多卡部分待本地验证（依赖硬件）。

#### 4.4.5 小练习与答案

**练习 1**：`--dist-url` 什么时候必须显式指定？
**答案**：多机训练时。所有机器的进程要靠同一个 master 地址会合，通常写成 `tcp://主节点IP:端口`；单机多卡或单卡时 detectron2 会自动选择可用端口。若两台机器的 `dist_url` 不一致，`init_process_group` 会一直等不到对方而超时。

**练习 2**：为什么把训练逻辑写成 `main(args)` 函数、而不是直接写在 `if __name__ == "__main__"` 块里？
**答案**：`launch` 需要拿到一个「可调用对象 + 参数」才能在**每个子进程**里重新执行它。若逻辑直接写在主块里，spawn 出的子进程没有入口可调；同时 `if __name__ == "__main__"` 守卫还能防止 spawn 子进程 import 本文件时递归触发 launch。

---

## 5. 综合实践

把本讲四个模块串成一条「冒烟验证流水线」，产出一份属于自己的训练入口体检报告：

1. **配置层**（4.1）：用 4.1.4 的脚本打印 R50 全景配置的关键字段，手工画出五层叠加示意，并记录一次「冻结后赋值报错」的输出。
2. **评估层**（4.2）：执行 `--eval-only` + `MODEL.WEIGHTS model_final_94dc52.pkl`，把输出的 PQ / AP / mIoU 与 [MODEL_ZOO.md:53-62](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/MODEL_ZOO.md#L53-L62) 的表值对照；无数据集时改为在日志中定位「权重加载 → evaluator 构建 → 指标输出」三段输出的位置。
3. **训练层**（4.3）：跑 10-iteration 冒烟训练，从 `metrics.json` 抄下全部 30 个 loss 键，并标注它们分别对应解码器第几层（`无后缀=最终层`，`_0…_8=中间层`），与 `DEC_LAYERS: 10` 互相印证；再用 `trace_trainer.py` 记录四个 `build_*` 的实际打点顺序，和 4.3.2 的时序表逐行核对。
4. **启动层**（4.4）：分别用 `--num-gpus 1` 与（若有）`--num-gpus 2` 启动，记录日志份数与 rank 相关字样的差异。

最终交付：一页 Markdown，包含五层配置图、build_* 时序表、30 个 loss 键清单及来源解释、单卡/多卡日志差异截图或摘录。完整跑通需要 COCO 数据与（多卡部分的）多张 GPU；缺硬件时对应小节明确标注「待本地验证」即可。

## 6. 本讲小结

- `setup()` 按「get_cfg → add_deeplab_config → add_maskformer2_config → yaml → 命令行 opts」五层叠加出唯一配置，`freeze()` 后全进程只读，`default_setup` 完成输出目录/日志/种子等框架初始化。
- `main()` 是两条岔路：`--eval-only` 用类方法 `Trainer.build_model` + `Trainer.test` 免实例化完成评估（可选追加 TTA）；训练分支 `Trainer(cfg)` → `resume_or_load` → `train()`。
- `Trainer` 只覆写四个类方法：`build_train_loader`（五选一 mapper）、`build_optimizer`（参数分组 + full_model 梯度裁剪）、`build_lr_scheduler`（poly lr）、`build_evaluator`（按 evaluator_type 与三开关组合）；`build_model` 交给注册表。
- 四个 `build_*` 由 detectron2 `DefaultTrainer.__init__` 与 `Trainer.test` 在固定时机回调：model → optimizer → train_loader → scheduler（构造期），evaluator（评估期）。
- 训练日志的 30 个 loss 键（`loss_ce/loss_mask/loss_dice` × 最终层 + `_0…_8` 辅助层）由 `DEC_LAYERS: 10` 与 `weight_dict` 的 aux 后缀机制决定。
- `launch` 统一三种形态：world_size=1 直接执行 `main`，否则按卡 spawn 子进程并以 `dist_url` 会合；每个进程都完整执行一遍 `setup → main`。

## 7. 下一步学习建议

- 下一讲 **u2-l2 数据集注册与准备**：`build_train_loader` 返回的 loader 到底从哪里读到数据——深入 `DatasetCatalog` / `MetadataCatalog` 与 `datasets/` 下的准备脚本。
- 顺带阅读 detectron2 的 `DefaultTrainer`（[engine/defaults.py](https://github.com/facebookresearch/detectron2/blob/main/detectron2/engine/defaults.py)）源码，把 4.3.2 的时序表逐行对上号，你会对「插件回调」有更踏实的理解。
- 想先看损失键的含义，可跳到 **u5-l2/u5-l3**（SetCriterion 与深监督）；想看评估器分发细节则看 **u7-l1**；优化器与 lr 策略在 **u2-l4**。
