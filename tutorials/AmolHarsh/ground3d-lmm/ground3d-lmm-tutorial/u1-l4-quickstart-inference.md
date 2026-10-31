# 快速上手：用预训练权重跑一次接地推理

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚 `tools/test.py` 这条命令从「命令行参数」到「Runner 启动测试」的完整执行主线，知道每个参数最终落到了配置的哪个字段。
2. 独立完成一次真实推理的前置准备：用 `snapshot_download` 拉取预训练 checkpoint，确认数据目录就位。
3. 理解 `--cfg-options` 的点路径语法，并能正确写出 `test_dataloader.dataset.pipeline.2.transforms.2.task_level=part_qa_data` 这样的覆盖项——知道每个下标和字段分别对应配置里的什么。
4. 掌握评估配置里的三个「任务切换旋钮」：`task_level` / `task_type` / `sub_task`，以及第四个输出旋钮 `save_pred_qa_dir`。
5. 看懂推理产物：`preds/` 目录下每个场景 JSON 的层级结构与每条记录的三个字段，并区分「文本预测 JSON」和「分割 mIoU 指标」两条不同的输出通道。

## 2. 前置知识

本讲假设你已完成前两讲：

- **u1-l2（环境搭建）**：conda 环境 `ground3d` 已就绪，OpenMMLab 栈（mmengine/mmcv/mmdet/mmdet3d）、spconv、MinkowskiEngine、transformers/peft 都能 import。本讲所有命令都要在这个环境里执行。
- **u1-l3（仓库结构与注册机制）**：你已经知道 `configs/` 里只写声明、`uniseg3d/` 里只写实现，配置里的 `type='Grounded_LMM_main'` 这类字符串靠注册表解析成真实 Python 类，`custom_imports` 负责触发 `uniseg3d` 包的注册副作用。

在此基础上，本讲再补三个背景概念：

- **checkpoint（检查点）**：一个 `.pth` 文件，里面是训练好的模型权重（本项目的 Joint 变体约含 4.47B 参数的 Qwen3-VL 底座 + 约 400M LoRA 可训练参数）。推理时由 `tools/test.py` 的第二个位置参数指定。
- **snapshot_download**：HuggingFace Hub 提供的下载函数，把一个模型仓库整体拉到本地缓存并返回本地路径。它支持 `allow_patterns` 过滤，可以只下载标注子集而不是整个 150GB 数据集。
- **测试主循环（TestLoop）**：mmengine 的 `Runner` 在 `runner.test()` 里会遍历 `test_dataloader`，把每个 batch 送进 `model.predict()`，再把预测结果交给 evaluator（本项目是 `Part3DGlamm_SegMetric`）算指标。本讲要讲的「预测 JSON」并不是 evaluator 写的，而是模型前向过程中解码器自己直接写盘的——这是本项目一个容易混淆的细节。

一句话预告本讲的主线：**`tools/test.py` 是一台「配置驱动的通用测试机」，它自己不认识 Ground3D-LMM；所有模型细节都在配置里，所有临时改动都通过 `--cfg-options` 注入。**

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|---|---|---|
| `README.md` | 项目主文档 | Quick Start 一节的下载与推理命令 |
| `tools/test.py` | mmdet3d 通用测试入口 | 参数解析、配置合并、Runner 启动 |
| `configs/ground3dlmm_eval_ground3d_scannet.py` | ScanNet 评估配置（Joint 变体） | `test_pipeline` 结构、`QA_Generation_Test` 的位置、数据与指标配置 |
| `uniseg3d/transforms_3d.py` | 数据变换实现 | `QA_Generation_Test` 的三级过滤逻辑 |
| `uniseg3d/query_decoder.py` | 语言-分割解码器 | `save_pred_qa_dir` 的初始化、`infer_reason_eval` 写 JSON 的位置 |
| `uniseg3d/uniseg3d.py` | 模型主入口 `Grounded_LMM_main` | `predict()` 如何串起「写 JSON」与「算 mIoU」两条线 |
| `uniseg3d/prompt_template.py` | 提示词模板 | 问题如何被包装成模型输入 |

## 4. 核心概念与源码讲解

### 4.1 tools/test.py：参数解析与执行主线

#### 4.1.1 概念说明

`tools/test.py` 是 OpenMMLab 生态的「通用测试司机」。它第一行注释就说明了自己是 mmdet3d 官方 `tools/test.py` 的原样拷贝（[tools/test.py:L1](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/test.py#L1)）。这句话的深层含义是：**它不含任何 Ground3D-LMM 特有逻辑**——不管什么模型，只要给出「配置 + checkpoint」，它都能跑。模型是什么、数据从哪来、指标怎么算，全部由配置文件声明（回顾 u1-l3 的注册机制）。

因此阅读本文件的正确姿势不是逐行精读，而是抓住三个问题：

1. 命令行参数有哪些，各自改写配置的哪个字段？
2. 配置文件和命令行覆盖谁优先？
3. 最后怎么从一份配置字典变成一次真实测试？

#### 4.1.2 核心流程

`main()` 的执行主线可以画成：

```text
parse_args()                      # 解析命令行
    ↓
Config.fromfile(args.config)      # 执行配置文件（python 语法），递归合并 _base_
    ↓
cfg.merge_from_dict(cfg_options)  # 把 --cfg-options 的 k=v 逐点写回 cfg
    ↓
确定 work_dir                     # 优先级：CLI --work-dir > 配置段 > ./work_dirs/<配置名>
    ↓
cfg.load_from = args.checkpoint   # 位置参数 checkpoint 直接覆盖配置里的 load_from
    ↓
Runner.from_cfg(cfg)              # 按配置构建 dataloader / model / evaluator
    ↓
runner.test()                     # 遍历 test_dataloader → model.predict → evaluator
```

两个容易忽略但很关键的点：

- **CLI 永远赢**：`load_from` 在配置文件里也有一项（指向训练用的主干初始化权重），但 [tools/test.py:L131](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/test.py#L131) 在合并完 `--cfg-options` 之后无条件执行 `cfg.load_from = args.checkpoint`，所以位置参数传入的预训练 checkpoint 一定是最终生效的那个。
- **配置文件本身是 Python**：`Config.fromfile` 会真的执行这个 `.py` 文件（所以配置里可以写 `num_channels * (i + 1)` 这种表达式），再把 `_base_` 链上的字段递归合并进来（u1-l3 已讲）。

#### 4.1.3 源码精读

**参数表**。两个位置参数 + 本项目真正会用到的几个可选参数：

| 参数 | 类型 | 本项目是否用到 | 说明 |
|---|---|---|---|
| `config` | 位置参数 | ✅ | 评估配置路径，如 `configs/ground3dlmm_eval_ground3d_scannet.py` |
| `checkpoint` | 位置参数 | ✅ | checkpoint 路径，最终赋给 `cfg.load_from` |
| `--work-dir` | 可选 | ✅ | 指标 JSON 的保存目录 |
| `--cfg-options` | 可选，`nargs='+'`，`action=DictAction` | ✅ | 本讲主角：点路径覆盖配置 |
| `--launcher` | 可选 | 单卡不用 | `none` / `pytorch` / `slurm` / `mpi` |
| `--show` / `--show-dir` / `--task` / `--score-thr` | 可选 | ❌ | mmdet3d 可视化遗留参数，本模型不使用 |
| `--tta` | 可选 | ❌ | 测试时增强，配置里没有 `tta_model`，不可用 |

位置参数与 `--cfg-options` 的定义见 [tools/test.py:L18-L19](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/test.py#L18-L19) 和 [tools/test.py:L45-L53](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/test.py#L45-L53)。`--cfg-options` 的 help 文本本身就值得读：它说明了 `key="[a,b]"` 这种列表值写法、引号不可省、不允许空格——这些都是 mmengine `DictAction` 的通用语法。

**main() 主线**。加载与合并：

- [tools/test.py:L112](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/test.py#L112) — `Config.fromfile(args.config)` 执行配置并合并 `_base_`。
- [tools/test.py:L119-L120](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/test.py#L119-L120) — 只要有 `--cfg-options`，就调用 `cfg.merge_from_dict()` 逐点写回。点路径中的数字（如 `.2.`）被解释为列表下标（u1-l3 讲过的规则，4.2 节会实地演练）。

`work_dir` 的三级优先级在 [tools/test.py:L122-L129](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/test.py#L122-L129)：CLI 显式指定 > 配置文件里的 `work_dir` > 兜底值 `./work_dirs/<配置文件名去后缀>`。本评估配置没有写 `work_dir`，所以不传 `--work-dir` 时会落到 `./work_dirs/ground3dlmm_eval_ground3d_scannet/`。

checkpoint 覆盖与 Runner 启动：

- [tools/test.py:L131](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/test.py#L131) — `cfg.load_from = args.checkpoint`，位置参数一锤定音。
- [tools/test.py:L144-L154](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/test.py#L144-L154) — 没有 `runner_type` 就走默认 `Runner.from_cfg(cfg)`，最后 `runner.test()` 启动测试循环。

另外一个不起眼但有用的细节：[tools/test.py:L6](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/test.py#L6) 已经 `sys.path.append` 了仓库根目录，所以从任意位置执行 `tools/test.py` 都能 import 到 `uniseg3d`；README 命令里再加 `PYTHONPATH=.` 属于双保险。

#### 4.1.4 代码实践

**实践 1：不加载模型，只做参数层面的「干跑」**

1. **实践目标**：确认你在 u1-l2 搭好的环境里能启动 `tools/test.py` 的参数解析层，并读懂 `--cfg-options` 的官方语法说明。
2. **操作步骤**：

   ```bash
   conda activate ground3d
   cd <仓库根目录>
   python tools/test.py --help
   ```

   注意：由于文件顶部就 import 了 mmengine 和 mmdet3d（[tools/test.py:L8-L12](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/test.py#L8-L12)），即使只看 help 也要求依赖完整——这本身就是一次环境自检。如果你在 u1-l2 用「import 成功」作为验收标准，这里应当顺利通过。

3. **需要观察的现象**：help 文本中 `--cfg-options` 一段关于 `key="[a,b]"`、嵌套列表、引号与空格的说明。
4. **预期结果**：打印用法说明，进程退出，不触碰 GPU、不加载数据。（待本地验证——本讲义写作环境无法执行 Python。）

**实践 2：纸面追踪 default work_dir**

1. **实践目标**：不运行命令，只依据 [tools/test.py:L122-L129](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/test.py#L122-L129) 的逻辑推断：使用配置 `configs/ground3dlmm_eval_ground3d_scannet.py` 且**不传** `--work-dir` 时，指标会写到哪个目录。
2. **操作步骤**：阅读该配置，确认其中没有 `work_dir` 字段；再套用 L126-L129 的兜底规则。
3. **需要观察的现象**：无（纯推理）。
4. **预期结果**：`./work_dirs/ground3dlmm_eval_ground3d_scannet/`。这个目录在跑完评估后会出现一个按时间戳命名的指标 JSON 文件（由 Runner 的指标落盘机制写出，与 4.3 节的预测 JSON 是两回事）。

#### 4.1.5 小练习与答案

**练习 1**：命令行同时传了 `--work-dir work_dirs/quickstart`，而配置文件里也存在 `work_dir` 字段，最终谁生效？

答案：命令行生效。[tools/test.py:L123-L125](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/test.py#L123-L125) 只要发现 `args.work_dir is not None` 就直接覆盖 `cfg.work_dir`，根本不再看配置文件。

**练习 2**：评估配置的末尾写着 `load_from = 'work_dirs/pretrained/uniseg3d_best.pth'`，为什么实际推理加载的是命令行里那个 4B checkpoint？

答案：[tools/test.py:L131](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/test.py#L131) 在所有合并完成之后无条件执行 `cfg.load_from = args.checkpoint`。配置里的 `load_from` 是给训练脚本（`tools/train.py`）用的主干初始化路径，测试入口会用位置参数把它覆盖掉。

**练习 3**：`--show`、`--task lidar_seg` 这些参数在这个项目里能用吗？

答案：不能用。它们属于 mmdet3d 原生的可视化钩子（见 [tools/test.py:L71-L97](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/test.py#L71-L97) 的 `trigger_visualization_hook`），而本项目的评估配置里没有 `default_hooks.visualization`，强行传 `--show` 会触发 L92-L95 的 `RuntimeError`。本项目的可视化走的是另一条路（`save_viz_dir` + `tools/export_grounding_ply.py`，见 u6-l4）。

### 4.2 eval 配置与 test_pipeline 的三个切换点

#### 4.2.1 概念说明

README 快速上手的推理命令里有四行 `--cfg-options`，本模块把它们逐个讲透。前三个是「任务选择旋钮」，都落在同一个变换 `QA_Generation_Test` 上；第四个是「输出开关」`save_pred_qa_dir`。

先建立直觉：Ground3D 的 QA 标注天然是一个三层嵌套字典——

```text
qa_data
├── part_qa_data        ← 第 1 层：task_level（QA 层级：部件级 / 物体级 / 多轮）
│   ├── qa_data         ← 第 2 层：task_type（纯文本问答 / 带分割问答）
│   │   ├── scale_estimation        ← 第 3 层：sub_task（具体子任务名）
│   │   │   └── [ {question, answer}, ... ]
│   │   └── ...
│   └── qa_data_seg
│       └── ...
├── object_qa_data
│   └── ...
└── multi_turn_qa_data  ← 多轮对话是列表的列表，不走 task_type/sub_task 过滤
    └── [ [turn1, turn2, ...], ... ]
```

`task_level` / `task_type` / `sub_task` 三个参数就是这三层的过滤器；`None` 表示「该层不过滤，全要」。每次推理前你必须先选定要评哪一层、哪一类、哪个子任务——这正是 README 命令里三行覆盖的含义：**评「部件级 · 纯文本问答 · 尺度估计」**。

`task_type` 的两个取值需要特别解释，配置里的注释就是权威来源（[configs/ground3dlmm_eval_ground3d_scannet.py:L199-L200](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_ground3d_scannet.py#L199-L200)）：

- `qa_data`：纯文本问答（答案不含分割标记），如 `scale_estimation`、`existence_verification`；
- `qa_data_seg`：带接地的问答（答案含 `<p>...</p><SEG>` 标记），如 `functional_part_grounding`、`grounded_dimension_reasoning`。

第四个旋钮 `model.decoder.save_pred_qa_dir` 决定「要不要把每个场景的文本预测写成 JSON、写到哪」。配置默认没写这个字段，构造函数默认值为 `None`（[uniseg3d/query_decoder.py:L1997](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1997)），此时**不会写任何 JSON**——所以 README 命令必须显式传它。

#### 4.2.2 核心流程

数据侧的过滤链路（数据管线的完整讲解属于 u2，这里只看切换点）：

```text
Dataset 吐出 qa_data（三层嵌套字典）
    ↓
QA_Generation_Test.transform()
    ├── flatten_qa_data(qa_data)
    │     ├── task_level 过滤：只保留指定层级（None = 全部）
    │     ├── task_type  过滤：multi_turn 层跳过此过滤
    │     └── sub_task  过滤：None = 该层全部子任务
    │     └── 给每条 QA 打上 qa_level / qa_key / qa_task_type 等标签
    ├── （测试阶段不做随机采样：select_qa_list = flat_qa_list）
    └── 输出 selected_qa_data（QA 列表）+ sp_gt_seg（逐 QA 的超点级 GT 掩码）
```

关键语义：**过滤是「严格匹配 + 静默为空」**。如果指定的 `task_level` 在某场景的 `qa_data` 里不存在，过滤结果是空列表——该场景没有可评估的 QA，不报错、跳过了事。

配置侧的下标寻址是本模块的另一个重点，`test_dataloader.dataset.pipeline.2.transforms.2.task_level` 每一段的对应关系：

| 点路径片段 | 配置里的位置 |
|---|---|
| `test_dataloader` | [configs/ground3dlmm_eval_ground3d_scannet.py:L238](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_ground3d_scannet.py#L238)（`test_dataloader = val_dataloader` 的别名） |
| `.dataset.pipeline` | [configs/ground3dlmm_eval_ground3d_scannet.py:L165-L211](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_ground3d_scannet.py#L165-L211) 的 `test_pipeline` 列表 |
| `.2` | 列表第 3 个元素：`MultiScaleFlipAug3D`（[L181](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_ground3d_scannet.py#L181)） |
| `.transforms.2` | 其内部 `transforms` 列表的第 3 个：`QA_Generation_Test`（[L195](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_ground3d_scannet.py#L195)） |
| `.task_level` | 目标字段（[L196](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_ground3d_scannet.py#L196)） |

`MultiScaleFlipAugA3D` 是 mmdet3d 的「测试包装器」：推理时不做翻转/缩放（`flip=False, pts_scale_ratio=1`），只是把一组真正的变换包在里面执行。所以 QA 过滤器藏在 `pipeline[2].transforms[2]` 这个两层下标之后——这就是 README 命令里那串看起来吓人的点路径的全部秘密。

#### 4.2.3 源码精读

**配置中的 test_pipeline**。[configs/ground3dlmm_eval_ground3d_scannet.py:L165-L179](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_ground3d_scannet.py#L165-L179) 先加载点云（`LoadPointsFromFile`，6 维 = XYZ+RGB）和标注（`LoadAnnotations3D_`，含超点掩码）；[L181-L201](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_ground3d_scannet.py#L181-L201) 是 `MultiScaleFlipAug3D` 包装的三个真正变换：颜色归一化 → 超点标注准备 → **`QA_Generation_Test`（本讲主角）**。注意配置里给它的默认值是 `task_level='multi_turn_qa_data', task_type=None, sub_task=None`（[L196-L198](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_ground3d_scannet.py#L196-L198)）——也就是说**不传任何覆盖时，这条配置默认评的是全部多轮对话**。README 命令用三行覆盖把它切到「部件级 · qa_data · scale_estimation」。

**数据从哪来**。[configs/ground3dlmm_eval_ground3d_scannet.py:L229-L238](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_ground3d_scannet.py#L229-L238)：`val_dataloader` 的数据集是 `Part3DGlamm_Dataset`，`ann_file='part3d_glamm_infos_val.pkl'`，`test_mode=True`，并且 `test_dataloader = val_dataloader`。配合 [L104-L110](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_ground3d_scannet.py#L104-L110) 的 `data_root='data/scannet'` 与 `data_prefix`，可以反推出跑通本讲的硬性数据前提：

```text
data/scannet/
├── part3d_glamm_infos_val.pkl
├── points/  super_points/  part_mask/  object_mask/   # <scene>_frame_<n>.bin
└── frame_images/<scene>/<frame>.jpg                   # 仅 Joint 变体需要
```

这些目录怎么来，属于 u2-l2 的 `create_data.py` 转换流程；本讲只需确认它们存在。

**QA_Generation_Test 的构造与过滤**。[uniseg3d/transforms_3d.py:L550-L558](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L550-L558) 是构造函数：三个过滤参数的合法取值全写在注释里。真正的三层过滤在 `flatten_qa_data`：

- 第 1 层 [L565-L572](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L565-L572)：`task_level` 指定且存在 → 只看该层；指定但不存在 → 空列表；`None` → 全部层级。
- 第 2 层 [L594-L604](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L594-L604)：`task_type` 同样支持单值或列表，严格过滤。
- 第 3 层 [L609-L619](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L609-L619)：`sub_task` 规则相同。
- 多轮对话走特殊分支 [L574-L586](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L574-L586)：整段对话保持为列表并打上 `qa_keys='multi_turn'` 标签，供解码器分派到多轮处理路径。
- 每条入选 QA 都被打上五个标签（`qa_keys`/`qa_key`/`qa_task_type`/`qa_level`/`qa_type`），见 [L622-L627](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L622-L627)——这些标签在 4.3 节会原样变成预测 JSON 的层级键名。

**测试阶段不采样**。[uniseg3d/transforms_3d.py:L632-L636](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L632-L636)：训练版 `QA_Generation_v2` 会随机抽 `num_qa` 条，测试版则 `select_qa_list = flat_qa_list`——过滤出来的 QA **全部**参与评估。最终写出 `sp_gt_seg` 与 `selected_qa_data` 两个键（[L683-L684](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L683-L684)）。

**save_pred_qa_dir 的初始化**。[uniseg3d/query_decoder.py:L2005-L2016](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L2005-L2016)：解码器构造时若该参数非 `None` 就 `makedirs` 建目录；为 `None` 则保持 `None`，后面写 JSON 的代码就不会执行。README 命令传入 `work_dirs/quickstart/preds`，即在此处建目录。

#### 4.2.4 代码实践

**实践：零 GPU 验证 `--cfg-options` 到底改了哪里**

这是本讲最重要的实践：不加载 4B 模型、不碰数据，只用 mmengine 复现 `tools/test.py` 的「解析 + 合并」两步，亲眼看到三个任务旋钮落到 `QA_Generation_Test` 上。

1. **实践目标**：把 README 命令里 4 行 `--cfg-options` 在内存中重放一遍，验证合并后的配置确实是你想要的，再去看真实推理——养成「先干跑配置、再跑模型」的习惯。
2. **操作步骤**：把下面脚本存为仓库外的任意文件（示例代码，非项目原有代码，例如 `/tmp/dry_run_config.py`），在 `ground3d` 环境的仓库根目录下运行：

   ```python
   # 示例代码：零 GPU 干跑，复现 tools/test.py 的配置合并
   import argparse
   from mmengine.config import Config, DictAction

   parser = argparse.ArgumentParser()
   parser.add_argument('--cfg-options', nargs='+', action=DictAction)  # 与 tools/test.py 完全一致
   opts = parser.parse_args([
       '--cfg-options',
       'model.decoder.save_pred_qa_dir=work_dirs/quickstart/preds',
       'test_dataloader.dataset.pipeline.2.transforms.2.task_level=part_qa_data',
       'test_dataloader.dataset.pipeline.2.transforms.2.task_type=qa_data',
       'test_dataloader.dataset.pipeline.2.transforms.2.sub_task=scale_estimation',
   ])

   cfg = Config.fromfile('configs/ground3dlmm_eval_ground3d_scannet.py')
   cfg.merge_from_dict(opts.cfg_options)          # 对应 tools/test.py L119-L120

   qa_gen = cfg.test_dataloader.dataset.pipeline[2]['transforms'][2]
   print('type      =', qa_gen['type'])
   print('task_level=', qa_gen['task_level'])
   print('task_type =', qa_gen['task_type'])
   print('sub_task  =', qa_gen['sub_task'])
   print('save_pred_qa_dir =', cfg.model.decoder.save_pred_qa_dir)
   ```

3. **需要观察的现象**：打印出的 `type` 应为 `QA_Generation_Test`，三个任务字段应分别为 `part_qa_data` / `qa_data` / `scale_estimation`，输出目录应为 `work_dirs/quickstart/preds`。
4. **预期结果**：

   ```text
   type      = QA_Generation_Test
   task_level= part_qa_data
   task_type = qa_data
   sub_task  = scale_estimation
   save_pred_qa_dir = work_dirs/quickstart/preds
   ```

   脚本只解析配置、不构建模型（`Config.fromfile` 不会执行 `custom_imports`，也不会加载任何权重），普通笔记本即可运行。（待本地验证——本讲义写作环境无法执行 Python，输出为依据 mmengine 合并语义的推断。）

   加分实验：故意把下标改错成 `pipeline.2.transforms.1.task_level` 再运行，观察合并结果落到了 `AddSuperPointAnnotations_Part3DGlamm` 上（打印其 `type` 即可发现），体会「点错一层下标不会报错、只会悄悄改错对象」的坑。

#### 4.2.5 小练习与答案

**练习 1**：想评「物体级 · 全部 task_type · 全部子任务」，`--cfg-options` 应该怎么写？

答案：只需指定第一层，其余两层留 `None`：

```bash
--cfg-options "test_dataloader.dataset.pipeline.2.transforms.2.task_level=object_qa_data" \
              "test_dataloader.dataset.pipeline.2.transforms.2.task_type=None" \
              "test_dataloader.dataset.pipeline.2.transforms.2.sub_task=None"
```

注意 `None` 在 `DictAction` 里会被解析成 Python 的 `None`（而不是字符串 `"None"`），与 `flatten_qa_data` 的判断逻辑（[uniseg3d/transforms_3d.py:L594](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L594)、[L609](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L609)）匹配。更稳妥的做法是用 4.2.4 的干跑脚本先验证合并结果。

**练习 2**：为什么路径里是两个 `.2`？

答案：第一个 `.2` 是 `test_pipeline` 列表的第 3 个元素 `MultiScaleFlipAug3D`（[configs/ground3dlmm_eval_ground3d_scannet.py:L181](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_ground3d_scannet.py#L181)）；第二个 `transforms.2` 是它内部 `transforms` 列表的第 3 个元素 `QA_Generation_Test`（[L195](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_ground3d_scannet.py#L195)）。mmengine 的点路径把数字一律解释为列表下标（0 起）。

**练习 3**：如果某场景的 QA 里根本没有 `part_qa_data` 这一层，推理会崩溃吗？

答案：不会。[uniseg3d/transforms_3d.py:L565-L569](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L565-L569) 会得到空的目标层级列表，展平结果为空——该场景只是没有可评估的 QA，静默跳过。这是一种「宽进宽出」的设计，代价是：若你把子任务名拼错，同样不会有任何报错，只会得到空结果（这就是先用干跑脚本核对配置的价值）。

### 4.3 预测 JSON 输出结构

#### 4.3.1 概念说明

推理跑完后有**两条互相独立的输出通道**，初学者最容易把它们混为一谈：

| 通道 | 谁写的 | 写到哪 | 内容 |
|---|---|---|---|
| 文本预测 JSON | 解码器（模型前向过程中直接写盘） | `save_pred_qa_dir`（如 `work_dirs/quickstart/preds/`） | 每个场景帧一个 `{scene}_frame_{n}.json`：question / gt_answer / pred_answer |
| 分割 mIoU 指标 | `Part3DGlamm_SegMetric`（evaluator） | stdout / 日志，指标 JSON 由 Runner 落到 `work_dir` | 按（层级, 子任务）聚合的 mIoU |

也就是说：**预测 JSON 里没有 mask、没有 mIoU**。分割掩码的预测结果通过 `data_sample.pred_pts_seg` 走内存交给 evaluator（掩码 dump 到盘上是另一个开关 `save_viz_dir`，属于 u6-l4 的内容）。

单轮任务的 JSON 结构与 4.2 节的 `qa_data` 三层嵌套**完全同构**——这不是巧合：`flatten_qa_data` 打的标签（`qa_level`/`qa_key`/`qa_task_type`）在这里被直接用作 JSON 的层级键名：

```json
{
    "part_qa_data": {
        "qa_data": {
            "scale_estimation": [
                {
                    "question": "How wide is the sofa ...?",
                    "gt_answer": "...",
                    "pred_answer": "The model's generated answer (possibly containing <p>...</p><SEG> markers)"
                },
                ...
            ]
        }
    }
}
```

多轮任务则是「对话的列表」，每轮多一个 `turn_idx` 字段：

```json
{
    "multi_turn_qa_data": [
        [
            {"question": "...", "gt_answer": "...", "pred_answer": "...", "turn_idx": 0},
            {"question": "...", "gt_answer": "...", "pred_answer": "...", "turn_idx": 1}
        ],
        ...
    ]
}
```

#### 4.3.2 核心流程

从 `runner.test()` 到 JSON 落盘的调用链（模型内部各组件的深入讲解分别属于 u3/u4，这里只看数据怎么流到 JSON）：

```text
runner.test()
  └─ model.predict()                       # uniseg3d.py L3155
       ├─ collate + extract_feat           # 稀疏主干提特征（u3）
       ├─ decoder(x, queries)              # uniseg3d.py L3208
       │    └─（非训练分支）infer_reason_eval()   # query_decoder.py L3082 → L2860
       │         ├─ 逐场景循环：
       │         │    ├─ 拆出 scene_name / frame_id（L2873）
       │         │    ├─ 逐 QA：
       │         │    │    ├─ 单轮 → _process_single_turn_qa()
       │         │    │    │     组 prompt → generate → 记录 {question, gt_answer, pred_answer}
       │         │    │    │     → 二次前向取隐状态，抽 <SEG> 查询
       │         │    │    └─ 多轮 → _process_multi_turn_qa()
       │         │    │          逐轮「问题 + 历史回答」重新生成
       │         │    └─ json.dump(pred_qa_data) 到 {scene}_frame_{n}.json（L2896-L2898）
       │         └─ 返回 <SEG> 查询列表
       ├─ 6 层查询解码器把查询变成逐点掩码（u5）
       └─ qa_predict_by_feat() → data_sample.pred_pts_seg   # 交给 evaluator 算 mIoU
```

注意一个对初学者很反直觉的事实：**写 JSON 发生在「分割解码」之前**。解码器先跑语言模型生成回答并立刻把文本预测落盘，然后才用 `<SEG>` 隐状态去算掩码。所以即使后面分割部分出问题，文本 JSON 也已经写好了。

另外，问题在送入语言模型前会被包进一个指令模板（[uniseg3d/prompt_template.py:L36-L43](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/prompt_template.py#L36-L43)）：模板要求模型「在回答中用 `<p>名字</p><SEG>` 标注被指代的物体或部件」，占位符 `{point}` 会被重复 N 次的点 token 填充（N = 该场景超点数）。这解释了为什么 `pred_answer` 里可能出现 `<p>...</p><SEG>` 这样的标记。

#### 4.3.3 源码精读

**模型侧入口**。[uniseg3d/uniseg3d.py:L3197-L3210](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L3197-L3210)：`predict()` 先过稀疏主干拿特征（L3197-L3198），再把特征与 QA 一起交给解码器（L3208），最后 `qa_predict_by_feat()`（L3210）把掩码结果装进 `data_sample.pred_pts_seg`。若某场景过滤后 QA 为空，L3204-L3205 会把输出置为空掩码结构——与 4.2 练习 3 的「静默跳过」呼应。

**解码器分派**。[uniseg3d/query_decoder.py:L3040-L3082](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L3040-L3082)：`forward` 按 `self.training` 分流——训练走 `infer_reason_train`（u4-l3），推理（`else` 分支，L3081-L3082）走 `infer_reason_eval`，两者都在语言模型里跑一遍问答，区别是推理版只生成、不算语言损失。

**逐场景循环与写盘**。[uniseg3d/query_decoder.py:L2860-L2900](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L2860-L2900)：

- L2868-L2875：按 batch 内场景循环，用 `'_frame_'` 把 `i_scene_name`（形如 `scene0000_00_frame_0`）拆成场景名与帧号；Joint 变体据此拼出 RGB 帧路径（`image_dir` 为 `None` 的 3D-only 变体则得到 `None`，消息组装时会过滤掉空图，详见 u4-l6）。
- L2881-L2892：逐条 QA 分派——`dict` 且非 multi_turn 标签走单轮，否则（`list` 即一段对话）走多轮。
- L2896-L2898：**本讲的核心落盘点**——`json.dump(pred_qa_data, f, indent=4)` 写入 `{i_scene_name}.json`。文件名就是「场景_frame_帧号」。

**单轮记录的三字段**。[uniseg3d/query_decoder.py:L2746-L2757](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L2746-L2757)：先按 `qa_level → i_key → i_task_type` 三层逐级建字典（键名正是 `flatten_qa_data` 打的标签），再 append 一条 `{'question', 'gt_answer', 'pred_answer'}` 记录。问题模板的组装在 [L2719-L2728](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L2719-L2728)。

**多轮记录**。[uniseg3d/query_decoder.py:L2810-L2858](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L2810-L2858)：第 0 轮携带点 token（和 Joint 变体的参考帧），之后每轮把上一轮的 `pred_answer` 追加进对话历史（L2836）再重新生成；每轮记录多存一个 `turn_idx`（L2838-L2843），最后整段对话 append 到 `pred_qa_data['multi_turn_qa_data']`（L2858）。

#### 4.3.4 代码实践

**实践：盘点一个场景的预测 JSON**

前置条件：已完成第 5 节综合实践（真实跑出 `preds/` 目录）。若尚未运行推理，可先记下脚本，跑完后回来执行。

1. **实践目标**：写一个 10 行的检查脚本，回答三个问题——这个场景共评了几条 QA？分布在哪些（层级, 类型, 子任务）组合里？随便挑一条看模型答得如何。
2. **操作步骤**（示例代码，非项目原有代码）：

   ```python
   import json, glob

   path = sorted(glob.glob('work_dirs/quickstart/preds/*.json'))[0]
   data = json.load(open(path))
   print('文件:', path)

   for level, by_type in data.items():
       if level == 'multi_turn_qa_data':
           print(f'{level}: {len(by_type)} 段对话, 每段轮数 =',
                 [len(c) for c in by_type][:5])
       else:
           for ttype, by_sub in by_type.items():
               for sub, records in by_sub.items():
                   print(f'{level} / {ttype} / {sub}: {len(records)} 条')
                   r = records[0]
                   print('  Q :', r['question'][:80])
                   print('  GT:', r['gt_answer'][:80])
                   print('  PD:', r['pred_answer'][:80])
   ```

3. **需要观察的现象**：按 4.2 节的过滤设置（`part_qa_data / qa_data / scale_estimation`），应当只打印出一个组合，且问题都是「估计某物体/部件的尺寸/尺度」类提问；`pred_answer` 是模型生成的自然语言回答。
4. **预期结果**：单轮组合打印形如 `part_qa_data / qa_data / scale_estimation: N 条`，N 等于该场景过滤后的 QA 数（测试阶段不过滤不采样，全部落盘）。（待本地验证——依赖第 5 节真实推理产物。）

#### 4.3.5 小练习与答案

**练习 1**：预测 JSON 的文件名由什么决定？

答案：由 batch 里的场景帧名决定。[uniseg3d/query_decoder.py:L2873](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L2873) 先按 `'_frame_'` 拆出场景名与帧号，[L2897](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L2897) 用完整的 `i_scene_name`（如 `scene0000_00_frame_0`）作为文件名。

**练习 2**：为什么 JSON 里找不到 mIoU？

答案：JSON 只承载文本三字段（[uniseg3d/query_decoder.py:L2753-L2757](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L2753-L2757)）。掩码预测走内存：`qa_predict_by_feat` 把 `qa_pred_masks`/`qa_gt_masks` 装进 `data_sample.pred_pts_seg`（[uniseg3d/uniseg3d.py:L3210-L3215](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L3210-L3215)），由配置里注册的 `Part3DGlamm_SegMetric`（[configs/ground3dlmm_eval_ground3d_scannet.py:L242-L246](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_ground3d_scannet.py#L242-L246)）聚合成 mIoU 打印/落盘。指标细节在 u6-l1。

**练习 3**：把 `task_level` 切到 `multi_turn_qa_data` 后，预测 JSON 的形状会发生什么变化？

答案：顶层键变成 `multi_turn_qa_data`，值是「对话列表的列表」，每轮记录带 `turn_idx`——对应 [uniseg3d/query_decoder.py:L2803-L2805](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L2803-L2805) 与 [L2838-L2858](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L2838-L2858)，而不是单轮那种三层嵌套字典。

## 5. 综合实践

**任务：完整复现 README 的 Quick Start——在 `scale_estimation` 子任务上跑一次真推理，并盘点产物。**

本综合实践把 4.1–4.3 三个模块串成一条真实可执行的流水线。分为五步，前两步无 GPU 也可完成。

**Step 1 — 干跑配置（无 GPU，5 分钟）**

先做 4.2.4 的干跑脚本，确认 `--cfg-options` 的落点正确。这一步能在下载数百 GB 之前就拦住「子任务名拼错 / 下标点错」这类静默错误。

**Step 2 — 下载 checkpoint（无 GPU）**

按 README 的方式拉取 Joint 变体权重（[README.md:L77-L86](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/README.md#L77-L86)）：

```python
from huggingface_hub import snapshot_download
ckpt_dir = snapshot_download(repo_id="amolharsh/Ground3D-LMM-4B-Joint")
```

或命令行方式（[README.md:L97](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/README.md#L97)）：`huggingface-cli download amolharsh/Ground3D-LMM-4B-Joint --quiet`。注意 checkpoint 要与配置匹配：Joint 用 `ground3dlmm_eval_ground3d_scannet.py`（LoRA r=16），3D-only 用 `ground3dlmm_eval_ground3d_scannet_3d.py`（LoRA r=32）——rank 不匹配时 adapter 会被静默忽略（u1-l1 提过的坑）。

**Step 3 — 确认数据前提（可能最耗时的一步）**

对照 4.2.3 列出的清单确认 `data/scannet/` 下有 `part3d_glamm_infos_val.pkl` 与四个 `.bin` 目录。若没有，需要先走「下载数据集 → `create_data.py` 转换」流程——这是 **u2-l2** 的主题，本讲不展开。Joint 变体还需要 `frame_images/`（ScanNet 原图不可再分发，需按 `docs/DATA_IMAGES.md` 自行抽取）；**若想跳过图像准备，可改用 3D-only checkpoint + `_3d` 配置**，那是更轻量的快速上手路径。

**Step 4 — 运行推理**

执行 README 的原始命令（[README.md:L104-L111](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/README.md#L104-L111)）：

```bash
CKPT="$(huggingface-cli download amolharsh/Ground3D-LMM-4B-Joint --quiet)/pytorch_model.pth"
PYTHONPATH=. python tools/test.py \
    configs/ground3dlmm_eval_ground3d_scannet.py "$CKPT" \
    --work-dir work_dirs/quickstart \
    --cfg-options \
        "model.decoder.save_pred_qa_dir=work_dirs/quickstart/preds" \
        "test_dataloader.dataset.pipeline.2.transforms.2.task_level=part_qa_data" \
        "test_dataloader.dataset.pipeline.2.transforms.2.task_type=qa_data" \
        "test_dataloader.dataset.pipeline.2.transforms.2.sub_task=scale_estimation"
```

观察三处：① 终端逐场景滚动进度；② 结束时按子任务打印的分割 mIoU（README [L114-L116](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/README.md#L114-L116) 声明的两条产物之一）；③ `work_dirs/quickstart/preds/` 下逐场景出现的 JSON。（全流程需要 GPU 与转换后数据，**待本地验证**。）

**Step 5 — 盘点产物**

执行 4.3.4 的检查脚本，数一数 QA 条数、抽读几条 `pred_answer`，并与 `gt_answer` 对照感受模型在 `scale_estimation` 上的回答风格（数值 + 指代标记）。

**验收标准**：能说出（a）这条命令里 4 行覆盖分别改了配置的哪 4 个位置；（b）`preds/` 下文件名与配置里 `ann_file` 所指 val 集的场景帧一一对应；（c）mIoU 为什么不在 JSON 里。

## 6. 本讲小结

- `tools/test.py` 是 mmdet3d 的通用测试入口：**配置驱动、CLI 优先**——`--cfg-options` 经 `DictAction` 逐点合并进配置，位置参数 checkpoint 无条件覆盖 `cfg.load_from`，`work_dir` 遵循 CLI > 配置 > 兜底三级优先级。
- 评估任务由 `QA_Generation_Test` 的三个旋钮决定：`task_level`（part/object/multi_turn 三层 QA 层级）、`task_type`（`qa_data` 纯文本 / `qa_data_seg` 带分割）、`sub_task`（具体子任务）；`None` 表示该层不过滤，过滤是「严格匹配、静默为空」，所以子任务名拼错不会报错。
- 旋钮的配置路径是 `test_dataloader.dataset.pipeline.2.transforms.2.*`：两个 `.2` 分别索引 `test_pipeline` 里的 `MultiScaleFlipAug3D` 与其内部 `transforms` 里的 `QA_Generation_Test`。
- 第四个旋钮 `model.decoder.save_pred_qa_dir` 打开「逐场景预测 JSON」通道；不传则什么都不写。
- 推理产物有两条独立通道：**文本预测 JSON**（解码器在 `infer_reason_eval` 里直接写盘，单轮三层嵌套 + question/gt_answer/pred_answer 三字段，多轮为带 `turn_idx` 的对话列表）与**分割 mIoU**（evaluator `Part3DGlamm_SegMetric` 聚合，打印到 stdout / 落到 work_dir）。
- 务实习惯：先用「零 GPU 干跑脚本」核对 `--cfg-options` 落点，再去跑数小时的真推理。

## 7. 下一步学习建议

你现在已经能跑通一次推理并读懂产物，接下来按依赖顺序推荐：

1. **u2-l1 / u2-l2（数据集结构与 create_data.py 转换）**：本讲 Step 3 里「数据从哪来」的完整答案——HF 原始 `.npy`/QA JSON 如何变成 `infos.pkl` 与四个 `.bin` 目录，跑通它你的推理就不必依赖别人的预处理结果。
2. **u6-l2（eval_ground3d.sh 与配置族）**：本讲的 `tools/test.py` 是单次调用；想批量跑「多数据集 × 多层级 × 多子任务」，统一启动器会按 checkpoint 的 LoRA rank 自动选配置，是评估期的正确姿势。
3. **u6-l4（接地可视化）**：给推理加 `save_viz_dir`，把 `<SEG>` 掩码染成红/绿/N 色点云 PLY，直观看到「接地」到底接在了哪些点上——这是对本讲文本 JSON 的最好补充。

再往后（u3/u4）才进入模型内部：稀疏主干如何把百万点压缩成超点 token、`<SEG>` 隐状态如何变成分割查询。带着本讲建立的「配置 → 数据 → 前向 → 产物」地图去读，会顺畅得多。
