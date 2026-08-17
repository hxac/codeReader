# 训练产物：trial 目录、检查点与网格导出

## 1. 本讲目标

跑完一次训练后，`outputs/` 下会多出一个充满文件的目录；训练的最终目的则是把里面的检查点变成一个能放进 Blender / MeshLab 的 `obj` 网格。本讲结束后，你应该能够：

1. 说出 `outputs/<name>/<tag>/` 试验目录里每个子目录（`ckpts`、`save`、`code`、`configs`、`tb_logs`、`csv_logs`）分别是哪段代码、哪个回调产生的。
2. 解释 `ModelCheckpoint`、`CodeSnapshotCallback`、`ConfigSnapshotCallback` 三个回调各自保存什么、何时触发。
3. 独立写出（并逐词解释）用 `parsed.yaml + resume` 导出带纹理 obj 网格的命令。
4. 说清 `mesh-exporter` 的纹理烘焙流程，以及 `context_type`（gl / cuda）在渲染器与导出器两处分别扮演什么角色。

## 2. 前置知识

- **试验目录（trial_dir）**：一次训练对应一个独立文件夹 `outputs/<name>/<tag>@<时间戳>/`，配置、代码、日志、检查点全部收在其中。它的拼接规则在 [threestudio/utils/config.py:87-104](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/config.py#L87-L104) 中，本讲 4.1 会精读。
- **回调（Callback）**：PyTorch Lightning 的钩子对象，在训练生命周期的固定时刻（如 `on_fit_start`、`on_train_batch_end`）被自动调用。「保存快照」本质上就是挂在生命周期上的回调。
- **检查点（checkpoint，ckpt）**：把模型权重、优化器状态、`epoch`、`global_step` 等序列化成一个 `.ckpt` 文件，用于断点续训或推理。Lightning 加载时通过 `ckpt_path` 参数传入。
- **predict 模式**：Trainer 的第四种运行模式（fit / validate / test / predict）。在 DreamCraft3D 里，`--export` 并不是单独的模式，而是借用 `trainer.predict` 来执行网格导出（见 [launch.py:208-210](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py#L208-L210)）。
- **obj / mtl 格式**：Wavefront obj 是文本格式的三角网格（`v` 顶点、`vt` 纹理坐标、`vn` 法向、`f` 面）；mtl 是配套材质文件，通过 `map_Kd` 等字段引用一张烘焙好的贴图（如 `model_kd.jpg`）。
- **UV 展开**：把三维网格表面「摊平」到二维纹理平面，让每个顶点获得一个 `(u, v)` 坐标，纹理才能贴到模型上。项目用 xatlas 库完成。
- 承接前讲：u1-l4 已讲过 launch.py 的五种模式与回调挂载的全貌，u2-l2 讲过 `load_config` 与配置快照，u2-l3 讲过四阶段配置与检查点接力（`system.weights` / `geometry_convert_from`）。本讲聚焦「产物」本身。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [launch.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py) | 唯一入口：组装回调、日志器、Trainer；`--export` 分支 |
| [threestudio/utils/callbacks.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/callbacks.py) | 三个自定义回调：代码快照、配置快照、Gradio 进度 |
| [threestudio/utils/config.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/config.py) | `ExperimentConfig.__post_init__` 生成 trial_dir；`dump_config` 落盘 parsed.yaml |
| [threestudio/systems/base.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py) | `on_predict_start` 构建导出器、`on_predict_epoch_end` 调用导出并保存 |
| [threestudio/models/exporters/mesh_exporter.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/exporters/mesh_exporter.py) | `mesh-exporter`：等值面提取 + UV 展开 + 纹理烘焙 |
| [threestudio/models/exporters/base.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/exporters/base.py) | `Exporter` 基类与 `ExporterOutput` 数据类 |
| [threestudio/utils/saving.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/saving.py) | `SaverMixin.save_obj`：手工写出 obj/mtl 文本 |
| [threestudio/utils/rasterize.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/rasterize.py) | `NVDiffRasterizerContext`：gl 与 cuda 两种光栅化上下文 |
| [README.md](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/README.md) | 官方导出命令（Export Meshes 一节） |

## 4. 核心概念与源码讲解

### 4.1 试验目录的诞生：trial_dir 从哪里来

#### 4.1.1 概念说明

所有训练产物都收在一个「试验目录」里。它的路径由三部分拼接：`exp_root_dir`（默认 `outputs`）、`name`（实验名）、`tag + 时间戳`。理解这段拼接逻辑的关键回报是：**为什么用 `parsed.yaml` 做导出时，程序不会新建一个带新时间戳的目录，而是精准地写回原来的试验目录**——答案在 `timestamp` 字段的判空逻辑里。

#### 4.1.2 核心流程

```text
ExperimentConfig 数据类初始化
        │
        ▼
__post_init__（config.py:87）
  ├─ trial_name = tag
  ├─ 若 timestamp 为空：
  │     use_timestamp=True 时补一个 "@%Y%m%d-%H%M%S"
  ├─ exp_dir   = outputs/<name>
  ├─ trial_dir = outputs/<name>/<tag@时间戳>
  └─ os.makedirs(trial_dir)   ← 副作用：目录此刻被创建
```

两个场景对比：

- **训练时**：yaml 里通常没写 `timestamp` → 现场生成新时间戳 → 新目录。
- **导出/续训时**：传入的 `parsed.yaml` 是上次训练落盘的**已解析**配置，里面 `timestamp` 已经是具体字符串（如 `"@20240101-120000"`）→ 判空失败 → 不再加时间戳 → `trial_dir` 与原试验完全一致。

#### 4.1.3 源码精读

1. [threestudio/utils/config.py:87-104](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/config.py#L87-L104) — `__post_init__` 拼出 `trial_dir` 并 `os.makedirs` 创建目录。注意第 91-92 行的注释："if resume from an existing config, self.timestamp should not be None"，这正是「复用已有试验目录」的机制来源。
2. [launch.py:123](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py#L123) — `system.set_save_dir(os.path.join(cfg.trial_dir, "save"))`：无论哪种运行模式，系统的保存目录都被固定为 `<trial_dir>/save`，后面导出的网格也会落在这里。
3. [launch.py:112-118](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py#L112-L118) — `--train` 模式下的自动续训：`glob` 抓取 `ckpts/*` 后取**字典序最大**者。由于 Lightning 默认检查点文件名形如 `epoch=4-step=4999.ckpt`，而 `last.ckpt` 以字母 `l` 开头排在 `e` 之后，排序结果通常正是 `last.ckpt`——自动续训因此总能拿到最新状态。

#### 4.1.4 代码实践

1. **实践目标**：不用跑训练，纸上推演 trial_dir 的生成规则。
2. **操作步骤**：
   - 阅读上面三处源码；
   - 对下面四种参数组合，手算 `trial_dir`：
     1. `name="hamburger"`, `tag="coarse-nerf"`, `timestamp=None`, `use_timestamp=True`
     2. `name="hamburger"`, `tag="coarse-nerf"`, `timestamp="@20240101-120000"`, `use_timestamp=True`
     3. `name="hamburger"`, `tag=""`, `timestamp=None`, `use_timestamp=False`
     4. `name="hamburger"`, `tag="texture"`, `timestamp=None`, `use_timestamp=True` 且 `n_gpus=2`
3. **需要观察的现象**：组合 2 不产生新时间戳；组合 3 会先在第 88-89 行抛 `ValueError`；组合 4 会触发多卡警告且时间戳被置空（第 95-99 行）。
4. **预期结果**：
   - 1 → `outputs/hamburger/coarse-nerf@<当前时间>`
   - 2 → `outputs/hamburger/coarse-nerf@20240101-120000`（原目录）
   - 3 → 报错 "Either tag is specified or use_timestamp is True."
   - 4 → `outputs/hamburger/texture`（无时间戳，官方提示多卡时务必自定义唯一 tag，否则互相覆盖）
5. 以上为纯源码推演；如想在本地用 Python 实际验证，可在装好依赖后 import `ExperimentConfig` 直接构造，运行结果**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：如果不小心把 `--config` 指向了原始 yaml（而非 `configs/parsed.yaml`）去执行导出，会发生什么？
**答案**：原始 yaml 中没有 `timestamp` 字段值，`__post_init__` 会现场生成新时间戳，于是 `trial_dir` 变成一个**新的空试验目录**；导出虽能执行（resume 路径是命令行显式给的），但产物会写进新目录的 `save/` 下，与原试验失去关联。这也是 README 导出命令特意使用 `parsed.yaml` 的原因之一。

**练习 2**：为什么多卡训练时官方禁用时间戳？
**答案**：见 [threestudio/utils/config.py:95-99](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/config.py#L95-L99)。每个进程各自调用 `datetime.now()`，时间戳可能不一致，导致多张卡各自建出名字略异的目录、训练状态四分五裂；因此多卡时时间戳置空并要求用户手写唯一 tag。

### 4.2 训练留下了什么：回调与日志器填满 trial 目录

#### 4.2.1 概念说明

launch.py 在 `--train` 模式下挂载了一组回调和日志器，它们各自负责往 trial_dir 写一个子目录/文件。这张「目录 ↔ 产生者」对照表是本讲最重要的知识：

| 产物 | 内容 | 产生者（代码位置） |
| --- | --- | --- |
| `ckpts/*.ckpt` | 检查点（`last.ckpt` 等） | Lightning `ModelCheckpoint`，[launch.py:136-138](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py#L136-L138) |
| `code/` | 全部源码快照 | `CodeSnapshotCallback`，[launch.py:140-142](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py#L140-L142) |
| `configs/parsed.yaml`、`configs/raw.yaml` | 配置快照 | `ConfigSnapshotCallback`，[launch.py:143-148](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py#L143-L148) |
| `tb_logs/` | TensorBoard 事件文件 | `TensorBoardLogger`，[launch.py:169](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py#L169) |
| `csv_logs/` | 指标 CSV | `CSVLogger`，[launch.py:170](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py#L170) |
| `cmd.txt` | 启动命令原文 | `write_to_text`，[launch.py:172-177](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py#L172-L177) |
| `save/` | 验证/测试渲染图、**导出资产** | `system.set_save_dir` + `SaverMixin`，[launch.py:123](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py#L123) |
| `progress` | Gradio 进度百分比 | `ProgressCallback`（仅 `--gradio`），[launch.py:152](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py#L152) |
| `logs` | Gradio 文件日志（仅 `--gradio`） | `logging.FileHandler`，[launch.py:125-131](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py#L125-L131) |

注意：**回调与日志器只在 `--train` 分支中挂载**（[launch.py:134](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py#L134)、[launch.py:163](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py#L163)），所以 validate / test / export 运行不会新建快照，只会通过 `save/` 写入渲染图或网格。

#### 4.2.2 核心流程

```text
trainer.fit 启动
  │
  ├─ on_fit_start（rank 0）
  │    ├─ CodeSnapshotCallback.save_code_snapshot  → code/
  │    └─ ConfigSnapshotCallback.save_config_snapshot → configs/parsed.yaml + raw.yaml
  │
  ├─ 每个 checkpoint 间隔（yaml: every_n_train_steps）
  │    └─ ModelCheckpoint → ckpts/last.ckpt、ckpts/epoch=X-step=Y.ckpt
  │
  ├─ 每次记录指标
  │    ├─ TensorBoardLogger → tb_logs/
  │    └─ CSVLogger → csv_logs/
  │
  └─ 每 batch 末
       └─ ProgressCallback（gradio）→ 覆写 progress 文件
```

#### 4.2.3 源码精读

**（1）ModelCheckpoint 与 yaml 的 `checkpoint` 段**

[launch.py:136-138](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py#L136-L138) 把 `dirpath` 固定为 `<trial_dir>/ckpts`，其余参数由 yaml 的 `checkpoint` 段经 `**cfg.checkpoint` 整体透传。以 texture 阶段为例（四份配置写法一致）：

[configs/dreamcraft3d-texture.yaml:163-166](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml#L163-L166) — `save_last: true` 保证始终维护一个 `last.ckpt`；`save_top_k: -1` 表示不按指标筛选、全部保留；`every_n_train_steps: ${trainer.max_steps}` 引用 Trainer 的总步数，即「每个阶段结束时各存一次」（例如 coarse-nerf 的 [configs/dreamcraft3d-coarse-nerf.yaml:156-159](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L156-L159)）。这正好呼应 u2-l3 讲过的检查点接力：下一阶段用 `system.weights` 或 `geometry_convert_from` 指向上一阶段的 `ckpts/last.ckpt`。

**（2）CodeSnapshotCallback：把代码拍进快照**

- [threestudio/utils/callbacks.py:64-77](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/callbacks.py#L64-L77) — `get_file_list` 用两条 `git ls-files` 命令取并集：`git ls-files -- ":!:load/*"`（git 跟踪的文件，但排除巨大的 `load/` 权重目录）∪ `git ls-files --others --exclude-standard`（未跟踪但未被 gitignore 的文件，即你本地新写的实验代码）。
- [threestudio/utils/callbacks.py:79-86](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/callbacks.py#L79-L86) — 按原相对路径逐文件 `shutil.copyfile` 到 `code/` 下，完整还原目录树。
- [threestudio/utils/callbacks.py:88-94](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/callbacks.py#L88-L94) — 在 `on_fit_start` 触发并整体 `try/except`：不在 git 仓库里（或没装 git）只打警告，不中断训练。

**（3）ConfigSnapshotCallback：parsed 与 raw 的分工**

[threestudio/utils/callbacks.py:103-110](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/callbacks.py#L103-L110) — 同样在 `on_fit_start` 执行两件事：

- `dump_config(..., "parsed.yaml")`（实现见 [threestudio/utils/config.py:124-126](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/config.py#L124-L126)，即 `OmegaConf.save`）：写出**解析后的完整结构化配置**——包含默认值、已合并的命令行覆盖、已解析的 `timestamp`。它是后面导出命令的输入。
- `copyfile(config_path, ".../raw.yaml")`：原样拷贝你传入的 yaml，保留 `???`、`${...}` 插值等「生」状态，用于对照「我写了什么」与「程序最终用了什么」。

**（4）VersionedCallback：为什么没有 version_N 子目录**

[threestudio/utils/callbacks.py:48-57](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/callbacks.py#L48-L57) — 基类 `VersionedCallback` 支持在 `save_root` 下建 `version_0/`、`version_1/`……（`_get_next_version` 自动递增，[第 36-46 行](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/callbacks.py#L36-L46)）。但 launch.py 构造两个快照回调时都传了 `use_version=False`（[launch.py:141](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py#L141)、[launch.py:147](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py#L147)），直接写根目录——因为版本隔离已由 trial_dir 的时间戳承担，无需二次编号。

**（5）ProgressCallback 与 CustomProgressBar**

- [threestudio/utils/callbacks.py:121-156](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/callbacks.py#L121-L156) — Gradio 模式下用**单文件覆写**的方式汇报进度：每次 `write` 都 `seek(0) + truncate()`（[第 134-138 行](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/callbacks.py#L134-L138)），文件里永远只有一行当前状态（训练百分比 / Rendering validation image / Rendering video / Exporting mesh assets）。网页端轮询这一个文件即可，无需解析日志。
- [threestudio/utils/callbacks.py:113-118](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/callbacks.py#L113-L118) — 非 Gradio 模式的终端进度条，仅把 Lightning 默认输出里的 `v_num` 版本号剔除，界面更干净。

#### 4.2.4 代码实践（本讲主实践·源码阅读型）

1. **实践目标**：不依赖 GPU 与检查点，靠通读代码建立「目录 ↔ 产生者」的完整映射。
2. **操作步骤**：
   1. 通读 [launch.py:133-177](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py#L133-L177)（回调与日志器组装段）与 [threestudio/utils/callbacks.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/callbacks.py) 全文；
   2. 在纸上或 Markdown 里手绘下面这棵目录树，并把 `ckpts`、`save`、`code`、`configs`、`tb_logs`、`csv_logs` 六个条目**各标注一行「由谁产生、在什么时机写入」**；
   3. 若本地已有跑完的试验目录，用 `tree -L 2 outputs/<name>/<tag>@*/` 或 `ls` 逐项核对。

   ```text
   outputs/
   └── <name>/
       └── <tag>@<时间戳>/          ← __post_init__ 创建
           ├── ckpts/               ← ？
           ├── code/                ← ？
           ├── configs/             ← ？
           ├── save/                ← ？
           ├── tb_logs/             ← ？
           ├── csv_logs/            ← ？
           ├── cmd.txt
           └── (progress / logs，仅 --gradio)
   ```
3. **需要观察的现象**：每个目录都能唯一对应到本讲表格中的一个代码位置；`code/` 里能看到与仓库一致的目录结构但没有 `load/`；`configs/` 里 `parsed.yaml` 的 `prompt` 是具体字符串而 `raw.yaml` 里仍是 `???`。
4. **预期结果**：完成标注后的对照表应与 4.2.1 的表格一致。若本地无试验目录，本实践为纯源码阅读型任务，纸面完成即为达成；实际目录核对**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：同事说他的 `code/` 快照里混进了一个 2GB 的临时文件，可能的原因是什么？
**答案**：该文件未被 git 跟踪、也未被 `.gitignore` 忽略，于是被 `git ls-files --others --exclude-standard` 抓进快照（[callbacks.py:72-76](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/callbacks.py#L72-L76)）。修复方式：把它加进 `.gitignore` 或移出仓库；源码注释也承认这是硬编码行为，TODO 是改用配置排除（第 72 行）。

**练习 2**：为什么 `parsed.yaml` 能直接当 `--config` 用，而 `raw.yaml` 不行？
**答案**：`parsed.yaml` 是 `parse_structured` 之后的结构化配置再落盘的产物，所有默认值已补齐、所有插值（如 `${trainer.max_steps}`）与 `???` 都已解析成具体值，`timestamp` 也固化了，重新加载不会产生新目录；`raw.yaml` 仍含 `???`（必填缺失，解析即报错）与插值，且缺 `timestamp`，不能直接驱动运行。

**练习 3**：`every_n_train_steps: ${trainer.max_steps}` 意味着检查点保存频率如何？为什么这样设计也够用？
**答案**：每个阶段只在结束时保存一次编号检查点，另有 `save_last: true` 持续刷新 `last.ckpt`。因为四阶段流水线里上一阶段的**最终**状态才是下一阶段需要的输入，中间检查点意义不大；配合自动续训机制，`last.ckpt` 已足以覆盖中断恢复。

### 4.3 --export 导出链路：从命令行到 obj+mtl

#### 4.3.1 概念说明

DreamCraft3D 没有为导出写独立的脚本，而是**复用 Lightning 的 predict 循环**：`--export` 只是让 launch.py 走 `trainer.predict`，而系统（system）在 predict 生命周期钩子里构建并调用导出器（exporter）。导出器是注册机制里的又一类插件（`exporter_type`），与 geometry / renderer 同等待遇——这正是 u1-l3「`X_type` 即注册名」规律的又一次体现。

#### 4.3.2 核心流程

```text
python launch.py --config <trial>/configs/parsed.yaml --export \
                 resume=<trial>/ckpts/last.ckpt system.exporter_type=mesh-exporter
        │
        ▼ launch.py:208-210
set_system_status(system, cfg.resume)      ← 恢复 epoch/global_step
trainer.predict(system, dm, ckpt_path=cfg.resume)
        │
        ▼ systems/base.py:311-317  on_predict_start
exporter = threestudio.find(cfg.exporter_type)(cfg.exporter,
            geometry=…, material=…, background=…)   ← 共享 system 的三大组件
        │
        ▼ predict_step（base.py:319-321）
save_video=False → 什么都不做（默认）
        │
        ▼ on_predict_epoch_end（base.py:323-332）
outputs = exporter()                       ← 真正干活的时机
for out in outputs:
    self.save_obj(f"it{true_global_step}-export/{out.save_name}", **out.params)
        │
        ▼ saving.py:441-544
<trial>/save/it{N}-export/model.obj + model.mtl + model_kd.jpg
```

要点：真正的导出发生在 **epoch 结束钩子**而非 batch 步骤里——导出一次网格与「第几个 batch」无关。

#### 4.3.3 源码精读

**（1）README 官方命令逐词解剖**

来自 [README.md:171-176](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/README.md#L171-L176)：

```sh
python launch.py --config path/to/trial/dir/configs/parsed.yaml --export --gpu 0 \
  resume=path/to/trial/dir/ckpts/last.ckpt system.exporter_type=mesh-exporter
```

| 片段 | 作用 |
| --- | --- |
| `--config .../parsed.yaml` | 加载上次训练落盘的已解析配置；`timestamp` 已固化 → trial_dir 指向原试验 |
| `--export` | 选择互斥模式组中的 predict 分支（[launch.py:225-229](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py#L225-L229)） |
| `resume=...` | 命令行 extras，经 `load_config` 合入 `cfg.resume`，作为 `ckpt_path` 加载权重 |
| `system.exporter_type=mesh-exporter` | 点号覆盖（u2-l2），指定导出器注册名；其实 `parsed.yaml` 里已是默认值 `mesh-exporter`（见下），此处属显式强调 |

**（2）export 分支与状态恢复**

- [launch.py:208-210](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py#L208-L210) — `--export` 分支：先 `set_system_status` 再 `trainer.predict(..., ckpt_path=cfg.resume)`。
- [launch.py:188-192](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py#L188-L192) — `set_system_status` 读出 ckpt 头部的 `epoch` 与 `global_step`，交给 [systems/base.py:58-61](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L58-L61) 的 `set_resume_status`。没有这一步，`true_global_step` 会停在 0：导出目录名 `it0-export` 失真，且一切依赖步数的 `C()` 调度（u2-l3 讲过的四元组损失权重）都会按第 0 步取值，导出的纹理可能和训练终点不一致。
- [launch.py:179-186](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py#L179-L186) — Trainer 显式设置 `inference_mode=False`。Lightning 的 validate/test/predict 默认运行在 inference 上下文中，产生的 inference 张量不参与 autograd；而本项目大量使用 nvdiffrast 可微光栅化算子，官方选择全局关闭 inference_mode 以保证这些算子在非训练阶段照常工作。纹理烘焙里的光栅化/插值（见 4.4）正依赖这一点。

**（3）system 侧：exporter 的构建与消费**

- [threestudio/systems/base.py:237-239](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L237-L239) — `exporter_type` 默认就是 `"mesh-exporter"`，注释写明「训练时无需指定」。这就是为什么导出命令里的覆盖其实是冗余的保险。
- [threestudio/systems/base.py:311-317](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L311-L317) — `on_predict_start`：`threestudio.find(cfg.exporter_type)(cfg.exporter, geometry=…, material=…, background=…)`。注意导出器**不新建**几何/材质，而是直接引用 system 现有的三个组件——加载进 system 的 ckpt 权重因此自动对导出生效。
- [threestudio/systems/base.py:319-332](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L319-L332) — `predict_step` 仅当 `exporter.cfg.save_video=True` 时渲染视频（默认 False，见 [exporters/base.py:21](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/exporters/base.py#L21)）；真正导出在 `on_predict_epoch_end`：调用 `exporter()` 拿到 `List[ExporterOutput]`，再按 `save_type` 动态查找 `save_{save_type}` 方法（这里是 `save_obj`），保存到 `it{true_global_step}-export/` 子目录。
- [threestudio/systems/base.py:334-336](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L334-L336) — 收尾日志 "Export assets saved to ..."，路径即 `<trial_dir>/save`。

**（4）ExporterOutput 与落盘**

- [threestudio/models/exporters/base.py:11-15](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/exporters/base.py#L11-L15) — 导出器的统一返回类型：`save_name`（文件名）、`save_type`（决定调用哪个 save 函数）、`params`（透传给保存函数的参数包）。[第 55-59 行](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/exporters/base.py#L55-L59) 的 `dummy-exporter` 返回空列表，是训练阶段的占位实现。
- [threestudio/utils/saving.py:441-499](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/saving.py#L441-L499) — `SaverMixin.save_obj`：拆包 mesh 的 `v_pos / t_pos_idx / v_tex / t_tex_idx`，`save_mat=True` 时同时写 `.mtl` 并把各贴图（`map_Kd` 等）交给 `_save_mtl`。
- [threestudio/utils/saving.py:501-544](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/saving.py#L501-L544) — `_save_obj` 手工拼接 obj 文本：`v` 顶点、`vn` 法向、`vt` 纹理坐标、`f v/vt/vn` 面。注意 [第 528 行](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/saving.py#L528) 写 `vt` 时把纵坐标翻转成 `1.0 - v[1]`——obj 的纹理坐标原点在左下、图像数组原点在左上，这一行完成两个约定的换算。

#### 4.3.4 代码实践

1. **实践目标**：把「命令行的每个词」与「源码里的每一行」一一对应起来。
2. **操作步骤**：
   1. 按下面顺序跟踪调用链并各写一句注释：`launch.py:208-210`（模式分支）→ `launch.py:188-192`（状态恢复）→ `systems/base.py:311-317`（构建导出器）→ `systems/base.py:323-332`（调用并保存）→ `saving.py:441-499`（写文件）；
   2. 回答：导出的 obj 最终出现在哪个绝对路径？目录名里的数字 N 由谁决定？
   3. （可选，需要本地有 u2-l3 跑出的试验）执行 README 导出命令，用 MeshLab 打开 `model.obj` 检查贴图。
3. **需要观察的现象**：若执行了步骤 3，终端应依次出现 `Exporting textures ...`、`Perform UV padding on texture maps ...`（来自 mesh_exporter 的 `threestudio.info`）以及最后的 `Export assets saved to ...`。
4. **预期结果**：路径为 `<trial_dir>/save/it{N}-export/model.obj`（与 `.mtl`、贴图同目录），N 即恢复后的 `true_global_step`（等于该阶段 `trainer.max_steps`）。本地无 ckpt 时步骤 1-2 为纸面推导，步骤 3 **待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `--export` 换成 `--test`，还会导出网格吗？
**答案**：不会。`--test` 走 [launch.py:204-207](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py#L204-L207) 的 `trainer.test` 分支，产出的是 `save/` 下的测试渲染图/视频；只有 predict 循环才会触发 `on_predict_start` 构建导出器。唯一例外是 `--train --gradio` 组合，它在训练结束后追加一次 `trainer.predict`（[launch.py:197-199](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py#L197-L199)），让网页端训练完直接拿到网格。

**练习 2**：为什么导出命令要传 `resume=` 而不是像续训那样依赖自动查找？
**答案**：自动续训逻辑被 `if args.train and cfg.resume is None` 限定在训练模式（[launch.py:112](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py#L112)）；`--export` 模式下 `cfg.resume` 默认为 `None`，若不显式传入，`trainer.predict` 的 `ckpt_path=None` 意味着使用**随机初始化**的 system——导出的是一团未训练的噪声网格。

**练习 3**：`ExporterOutput.save_type` 为什么设计成字符串而不是直接调用保存函数？
**答案**：解耦——导出器只声明「我要保存一个 obj 类型的东西」，由 system 侧按命名约定 `save_{save_type}` 动态查找 `SaverMixin` 上的方法（[systems/base.py:328-331](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L328-L331)）。这让新增导出格式（如 glb）只需注册新导出器 + 给 SaverMixin 加 `save_glb`，二者不必互相 import。

### 4.4 mesh-exporter 纹理烘焙与 nvdiff-rasterizer-context

#### 4.4.1 概念说明

texture 阶段训练的是 DMTet 网格 + 隐式颜色场（u2-l3），导出的任务是把它变成**标准 obj 资产**：网格从几何里提取，纹理则通过「把网格摊平到 UV 平面，再在摊平图上逐像素查询颜色 MLP」烘焙成一张贴图。烘焙本身需要一次光栅化，这就要用到 `NVDiffRasterizerContext`——它有 `gl` 与 `cuda` 两种后端，且**渲染器和导出器各持有一个独立的 context 实例**，两处的 `context_type` 是两个不同的配置项，这是初学者最容易混淆的点。

#### 4.4.2 核心流程

`MeshExporter.__call__` 的烘焙流水线：

```text
geometry.isosurface()                     ← 从 SDF/DMTet 提取 Mesh
  │
  ├─ mesh.unwrap_uv(xatlas 参数)          ← UV 展开：每个顶点得到 (u,v)
  │
  ├─ uv_clip = v_tex * 2 - 1              ← UV ∈ [0,1] → 裁剪空间 [-1,1]
  ├─ ctx.rasterize_one(uv_clip4, t_tex_idx, 1024×1024)
  │       ↑ 把网格按 UV 三角形光栅化进纹理图：图上每个像素 ↔ 表面上一点
  │
  ├─ hole_mask = 未被任何三角形覆盖的像素   ← UV 岛之间的缝隙
  ├─ gb_pos = ctx.interpolate_one(v_pos, rast, t_pos_idx)
  │       ↑ 反查：纹理图每个像素对应的三维表面位置
  │
  ├─ geo_out = geometry.export(points=gb_pos)
  ├─ mat_out = material.export(points=gb_pos, **geo_out)   ← 逐像素查 MLP 得 albedo
  │
  ├─ map_Kd = uv_padding(albedo)          ← cv2.inpaint 补洞，避免接缝漏色
  │
  └─ return [ExporterOutput("model.obj", "obj", params含mesh与map_Kd)]
```

#### 4.4.3 源码精读

**（1）注册与配置**

[threestudio/models/exporters/mesh_exporter.py:17-30](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/exporters/mesh_exporter.py#L17-L30) — `@threestudio.register("mesh-exporter")`；默认配置：`fmt="obj-mtl"`（带材质贴图；`"obj"` 则退化为顶点色，见 [第 139-175 行](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/exporters/mesh_exporter.py#L139-L175)）、`texture_size=1024`、`texture_format="jpg"`、**`context_type="gl"`**（注意：与 texture 配置里渲染器的 `cuda` 不同）。

**（2）独立的光栅化上下文**

[threestudio/models/exporters/mesh_exporter.py:34-41](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/exporters/mesh_exporter.py#L34-L41) — `configure` 时用**自己的** `cfg.context_type` 新建 `NVDiffRasterizerContext`，不复用渲染器的那份。对照渲染器侧：[threestudio/models/renderers/nvdiff_rasterizer.py:21](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/nvdiff_rasterizer.py#L21)（默认同样 `"gl"`）与 [第 32 行](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/nvdiff_rasterizer.py#L32) 的创建；而 [configs/dreamcraft3d-texture.yaml:67-69](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml#L67-L69) 把**渲染器**的 context 覆盖为 `cuda`（geometry 阶段同样，见 [configs/dreamcraft3d-geometry.yaml:58-60](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-geometry.yaml#L58-L60)）。

**（3）两种后端的差异**

[threestudio/utils/rasterize.py:12-20](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/rasterize.py#L12-L20) — `gl` 创建 `RasterizeGLContext`（走 OpenGL，需要可用的显示/EGL 环境），`cuda` 创建 `RasterizeCudaContext`（纯 CUDA 实现，无需 OpenGL），其他值直接 `ValueError`。这就是 u1-l2 讲过的 Docker 场景：容器里没有 OpenGL 时，渲染器配置 `context_type: cuda` 规避报错；同理，若导出阶段在无显示环境失败，可在命令行追加 `system.exporter.context_type=cuda` 覆盖**导出器**这一份 context。

**（4）烘焙的关键三步**

- [mesh_exporter.py:69-89](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/exporters/mesh_exporter.py#L69-L89) — `unwrap_uv` 后把 `(u,v)∈[0,1]` 映射到裁剪空间并补齐四维齐次坐标（z=0、w=1），然后 `rasterize_one` 以 `texture_size×texture_size` 把 UV 三角形画进纹理图。这一次光栅化的「相机」就是 UV 平面本身。
- [mesh_exporter.py:91-110](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/exporters/mesh_exporter.py#L91-L110) — `hole_mask` 标出 UV 岛之间无人覆盖的像素；`interpolate_one` 用光栅化结果反查出纹理图每个像素对应的**世界坐标** `gb_pos`。
- [mesh_exporter.py:112-131](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/exporters/mesh_exporter.py#L112-L131) — 拿 `gb_pos` 逐像素查询几何与材质 MLP（`export` 接口输出 albedo/metallic/roughness/bump），再经 `uv_padding`（[第 93-104 行](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/exporters/mesh_exporter.py#L93-L104)，内部用 `cv2.inpaint` 的 TELEA 算法补洞）填满缝隙，最终装进 `params["map_Kd"]` 等字段。

#### 4.4.4 代码实践

1. **实践目标**：弄清「渲染器 context」与「导出器 context」是两份独立配置，并掌握无显示环境的导出对策。
2. **操作步骤**：
   1. 在两处源码分别找到默认值：导出器 [mesh_exporter.py:30](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/exporters/mesh_exporter.py#L30) 与渲染器 [nvdiff_rasterizer.py:21](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/renderers/nvdiff_rasterizer.py#L21)；
   2. 检查 texture 配置只覆盖了渲染器：[dreamcraft3d-texture.yaml:67-69](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml#L67-L69)；
   3. 写出在 Docker/无显示环境下导出的完整命令：在 README 命令基础上追加 `system.exporter.context_type=cuda`；
   4. （可选）本地导出成功后，把 `system.exporter.texture_size=512` 加进命令再导一次，对比贴图文件尺寸。
3. **需要观察的现象**：步骤 3 的命令在无 OpenGL 的容器里不再于 `RasterizeGLContext` 处报错；步骤 4 的 `model_kd.jpg` 从 1024×1024 变为 512×512。
4. **预期结果**：能口头回答「导出时的光栅化发生在哪个空间」（UV 纹理空间，不是相机空间）。步骤 3、4 的实际运行**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：纹理为什么需要 inpaint 补洞？不补会怎样？
**答案**：xatlas 展开产生多个 UV 岛，岛与岛之间以及三角形边缘存在未被覆盖的像素（`hole_mask`）。渲染时双线性采样会读到这些「无主」像素，在贴图接缝处产生黑边/漏色；`cv2.inpaint` 用邻域颜色把它们填上（[mesh_exporter.py:93-104](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/exporters/mesh_exporter.py#L93-L104)）。

**练习 2**：`fmt="obj"` 与 `fmt="obj-mtl"` 的本质区别是什么？
**答案**：`obj-mtl` 烘焙一张 1024×1024 贴图并写 mtl 引用（`export_obj_with_mtl`）；`obj` 不做 UV 光栅化，直接在每个**顶点**处查询颜色 MLP 写成顶点色（`export_obj`，见 [mesh_exporter.py:158-165](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/exporters/mesh_exporter.py#L158-L165)）。前者文件多但通用，后者单文件但分辨率受顶点数限制。

**练习 3**：训练已经用 `cuda` context 了，导出还会在 `gl` 上失败吗？
**答案**：会，因为两份 context 互相独立：渲染器的 `cuda` 只管训练时的图像渲染；导出器在 `configure` 里按自己的 `cfg.context_type`（默认 `gl`）另建上下文用于纹理烘焙。这正是导出命令可能需要单独追加 `system.exporter.context_type=cuda` 的原因。

## 5. 综合实践

**任务：给一次（真实的或假想的）texture 阶段试验写一份「产物档案」。**

1. 准备：若本地已完成 u2-l3 的四阶段训练，任选 texture 阶段的试验目录；否则在纸上按本讲知识「预演」目录内容。
2. 制作两张产物档案：
   - **档案 A · 目录清单**：列出该试验目录的全部子项，为每一项注明产生者（精确到文件与行号，例如 `code/ ← CodeSnapshotCallback.on_fit_start, callbacks.py:88-94`）、写入时机（fit 开始 / 每步 / 每阶段结束 / predict 结束）。
   - **档案 B · 导出手册**：写出从该试验导出 obj 的完整命令（含 `--config parsed.yaml`、`resume=`、无显示环境时的 `system.exporter.context_type=cuda`），并按执行顺序注明每一步会打印的关键日志（`Exporting textures ...` → `Perform UV padding ...` → `Export assets saved to ...`）与最终产物路径 `<trial>/save/it{N}-export/model.obj|.mtl|_kd.jpg`。
3. 自检问题：如果导出产物出现在 `it0-export/`，说明命令缺了什么？（答：缺 `resume=`，`true_global_step` 未恢复。）
4. 有真实 ckpt 时用 MeshLab 打开 `model.obj` 验证贴图无缝、多视角一致；无 ckpt 时档案本身即为交付物，运行部分**待本地验证**。

## 6. 本讲小结

- 试验目录 `outputs/<name>/<tag>@<时间戳>/` 由 `ExperimentConfig.__post_init__` 创建；加载 `parsed.yaml` 时 `timestamp` 已固化，因此导出/续训写回**同一个**目录。
- `ckpts`（ModelCheckpoint）、`code`（CodeSnapshotCallback，git 双命令取文件集）、`configs`（ConfigSnapshotCallback，parsed.yaml + raw.yaml）只在 `--train` 模式生成；`tb_logs`/`csv_logs` 来自两个 Lightning Logger；`save/` 承接一切渲染图与导出资产。
- 检查点策略由 yaml 的 `checkpoint` 段透传：`save_last: true` 维护 `last.ckpt`，`every_n_train_steps: ${trainer.max_steps}` 每阶段末存档，衔接 u2-l3 的四阶段接力。
- `--export` 借道 `trainer.predict`：`on_predict_start` 用注册机制构建导出器并**共享** system 的 geometry/material/background；真正导出发生在 `on_predict_epoch_end`，产物落在 `save/it{N}-export/`。
- `mesh-exporter` 的纹理烘焙 = UV 展开 → UV 空间光栅化 → 反查表面坐标 → 逐像素查询颜色 MLP → inpaint 补缝；导出器与渲染器各持一个 `NVDiffRasterizerContext`，`gl`/`cuda` 后端按环境分别配置。
- launch.py 全局 `inference_mode=False`，保证 nvdiffrast 可微算子在 validate/test/predict 阶段照常工作。

## 7. 下一步学习建议

- 下一讲进入单元三：**u3-l1 注册机制**将系统展开 `@threestudio.register` / `find` 的实现细节——本讲看到的 `find(cfg.exporter_type)` 只是它的一个消费现场。
- 想深挖导出器内部，可先读 [threestudio/models/mesh.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/mesh.py) 中 `unwrap_uv` 与 `extract_fields`/`isosurface` 的实现，理解 `Mesh` 数据结构。
- 导出与显存优化的进阶内容（xatlas 参数、分辨率调低、混合精度取舍）安排在 **u8-l3 网格导出与显存优化实战**，可在学完单元五的 DMTet 几何后回看。
- Gradio 网页如何消费本讲的 `progress` 文件与导出资产，见 **u8-l4 Gradio 界面与二次开发实践**。
