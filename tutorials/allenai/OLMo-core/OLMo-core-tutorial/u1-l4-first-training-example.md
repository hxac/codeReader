# 跑通第一个训练例子：examples/llm/train.py

## 1. 本讲目标

学完本讲，你应该能够：

1. 读懂 `src/examples/llm/train.py` 这个自包含示例，说清一次「训练实验」由哪五个 Config 积木组成（model、dataset、data_loader、train_module、trainer）。
2. 理解 OLMo-core 的 `build()` 模式：Config 是「图纸」，`build()` 是从图纸造出真实组件（模型、优化器、数据加载器、训练器）的装配线。
3. 了解 `torchrun` 启动方式，以及 `prepare_training_environment()` / `teardown_training_environment()` 这对「环境准备/收尾」函数做了什么。
4. 跟着 `Trainer.fit()` 源码走一遍训练主循环，说出一个 step 内各阶段（前向、反向、优化器 step、回调钩子）的先后顺序。
5. 用 `--dry-run` 安全地预览一份实验配置，不下载任何数据、不占用 GPU。

本讲是单元一的收官：前三讲我们建立了目录地图和环境认知，这一讲把「积木」真正拼成一台会跑的机器。

## 2. 前置知识

### 2.1 Config：可序列化的 dataclass「图纸」

在 [u1-l3](u1-l3-source-tree-map.md) 里我们说过 OLMo-core「一切皆 Config」：几乎每个组件都有一个对应的 `@dataclass` 配置类，继承自 `src/olmo_core/config.py` 的 `Config` 基类。Config 只描述「要什么」，不负责「怎么做」；真正构造组件靠每个 Config 上的 `build()` 方法。第 2 单元会深入 Config 内部机制，本讲只需要把它当作「能打印、能存 YAML、能被命令行覆盖的嵌套字典」即可。

### 2.2 torchrun：分布式启动器

`torchrun`（PyTorch 自带）负责在每张卡（每个进程）上启动一份你的脚本，并通过环境变量（`RANK`、`WORLD_SIZE`、`LOCAL_RANK` 等）告诉每个进程「我是几号进程、总共几个进程」。你的脚本再用这些信息初始化进程组。所以 `--nproc-per-node=1` 就是「单进程跑一份」。

### 2.3 一些术语

- **rank**：进程编号，从 0 开始。rank 0 通常负责打印日志、写 checkpoint 等「主进程」工作。
- **FSDP**（Fully Sharded Data Parallel）：一种数据并行策略，把模型参数、梯度、优化器状态在多个 rank 间切分存储，显存友好。示例默认用它，本讲只需知道「train_module 的配置里选了它」。
- **global batch size / microbatch size**：一次优化器 step 全局消费的 token 数 vs 单个 rank 单次前向反向消费的 token 数。注意 OLMo-core 里这两个量**以 token 计**，不是以样本条数计。
- **meta device**：PyTorch 的「占位」设备——只记录张量形状、不分配真实内存，常用于先搭好结构再由并行策略落位参数。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/examples/llm/train.py` | 本讲主角：约 330 行的自包含训练示例，从命令行参数到 `trainer.fit()` 全流程 |
| `src/olmo_core/train/__init__.py` | 训练包门面：导出 `Trainer`/`TrainerConfig` 等，并定义 `prepare_training_environment()` 与 `teardown_training_environment()` |
| `src/olmo_core/train/trainer.py` | `Trainer` 类：`fit()`、`_fit_epoch()`、`_dry_run_batch()` 等训练循环实现 |
| `src/olmo_core/train/config.py` | `TrainerConfig`：`with_callback()`、`build()` 在这里 |
| `src/olmo_core/train/train_module/transformer/config.py` | `TransformerTrainModuleConfig.build(model)`：模型 + 优化器 + 并行设置的汇聚点 |
| `src/olmo_core/data/data_loader.py` | `NumpyDataLoaderConfig.build(dataset, dp_process_group=...)` |
| `src/olmo_core/nn/transformer/config.py` | `TransformerConfig.build(init_device=...)` 与各预置模型工厂方法 |
| `src/olmo_core/utils.py` | `seed_all()`：统一设置所有随机数种子 |

## 4. 核心概念与源码讲解

### 4.1 一份「实验」的五个积木：ExperimentConfig

#### 4.1.1 概念说明

训练一个语言模型需要回答五个问题：

1. **模型长什么样？** → `model: TransformerConfig`（多少层、多少头、词表多大……）
2. **数据在哪里、什么格式？** → `dataset: NumpyDatasetConfig`（指向预分词好的 `.npy` 文件）
3. **怎么把数据组成 batch？** → `data_loader: NumpyDataLoaderConfig`（全局 batch 多大、几个 worker）
4. **怎么更新参数？** → `train_module: TransformerTrainModuleConfig`（优化器、学习率调度、梯度裁剪、并行策略）
5. **训练怎么组织？** → `trainer: TrainerConfig`（保存目录、训练多久、挂哪些回调）

示例把这些答案打包成一个顶层配置类 `ExperimentConfig`。这就是 OLMo-core 组织实验的基本单位：**一份 ExperimentConfig = 一次可完整复现的实验**。

#### 4.1.2 核心流程

示例脚本的整体流程：

```text
命令行 (run_name, --model-factory, --sequence-length, ..., dotlist 覆盖)
        │
        ▼
build_config(opts, overrides)          # 纯内存操作：拼装五个 Config
        │  ├── TokenizerConfig.gpt2()
        │  ├── TransformerConfig.<factory>(vocab_size=...)
        │  ├── NumpyFSLDatasetConfig / NumpyDataLoaderConfig
        │  ├── TransformerTrainModuleConfig (AdamW + FSDP + CosWithWarmup)
        │  └── TrainerConfig + 一串 .with_callback(...)
        │  └── config.merge(overrides)  # 应用 dotlist 覆盖
        ▼
ExperimentConfig
        │
        ├── --dry-run ? → rich.print(config) 后直接退出（不需要分布式环境/数据）
        │
        ▼
prepare_training_environment()  →  train(config)  →  teardown_training_environment()
```

#### 4.1.3 源码精读

**① 数据在哪里找。** 脚本先探测 AI2 内部集群的常用路径，找不到就回退到 HTTP 流式下载（慢），也可以用环境变量 `OLMO_DATA_ROOT` 自己指定：

[src/examples/llm/train.py:L55-L81](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/examples/llm/train.py#L55-L81) —— 探测 `/net/nfs/...` 与 `/weka/...` 目录，回退到 `http://olmo-data.org/examples/c4-en/gpt2`，拼出训练与验证数据路径 `c4-train.00000-00099.npy`、`c4-validation.00000-00008.npy`。注释里还提示可以 `wget http://olmo-data.org/examples/c4-en/gpt2/c4-train.00000-00099.npy` 手动下载。

**② ExperimentConfig 本体。**

[src/examples/llm/train.py:L84-L106](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/examples/llm/train.py#L84-L106) —— 定义五个 Config 字段，外加三个实验级选项：

```python
@dataclass
class ExperimentConfig(Config):
    model: TransformerConfig
    dataset: NumpyDatasetConfig
    data_loader: NumpyDataLoaderConfig
    trainer: TrainerConfig
    train_module: TransformerTrainModuleConfig
    init_seed: int = 12536          # 模型权重初始化种子
    load_path: Optional[str] = None # 微调时从哪个 checkpoint 加载权重
    load_trainer_state: bool = False
```

注意它就是普通的 `@dataclass`，继承 `Config` 后自动获得 `save/load/merge/as_dict` 等能力（第 2 单元详解）。

**③ 命令行参数与 dotlist 覆盖的分离。**

[src/examples/llm/train.py:L273-L312](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/examples/llm/train.py#L273-L312) —— `parser_args()` 用 `parse_known_args()` 把参数分成两组：脚本自己的参数（`run_name`、`--model-factory` 默认 `llama2_271M`、`--sequence-length` 默认 2048、`--save-folder`、`--work-dir`、`--dry-run`）和剩余的「dotlist 覆盖」（如 `train_module.optim.lr=6e-3`），后者原样传给 `config.merge(overrides)`。

[src/examples/llm/train.py:L265-L268](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/examples/llm/train.py#L265-L268) —— `config = config.merge(overrides)` 把命令行覆盖应用到整棵配置树上。这就是 u1-l1 提到的「点号路径覆盖参数」的落点。

**④ 五个 Config 怎么拼。**

- [src/examples/llm/train.py:L154-L162](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/examples/llm/train.py#L154-L162) —— 模型配置：`getattr(TransformerConfig, opts.model_factory)` 按名字取预置工厂方法（如 `llama2_271M`、`olmo2_190M`），并传入 `vocab_size=tokenizer_config.padded_vocab_size()`（把词表补齐到 128 的倍数）。
- [src/examples/llm/train.py:L165-L176](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/examples/llm/train.py#L165-L176) —— 数据集与加载器：`NumpyFSLDatasetConfig`（FSL = 固定序列长度切分），`global_batch_size=256 * 1024`，注释强调**单位是 token 不是实例**。
- [src/examples/llm/train.py:L178-L193](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/examples/llm/train.py#L178-L193) —— 训练模块：`rank_microbatch_size=16 * 1024`（同样是 token），`AdamWConfig(lr=1e-3, ...)`，用 `OptimGroupOverride` 给 `embeddings.weight` 单独设 `weight_decay=0.0`；`dp_config` 选择 FSDP（参数 bfloat16、规约 float32）；`compile_model=True` 开启 `torch.compile`；调度器 `CosWithWarmup(warmup_steps=100)`。
- [src/examples/llm/train.py:L195-L255](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/examples/llm/train.py#L195-L255) —— 训练器：`TrainerConfig(...)` 之后连续 `.with_callback(...)` 挂了 9 个回调（gpu_monitor、checkpointer、comet、wandb、config_saver、profiler、lm_evaluator、downstream_evaluator、gap_monitor）。其中 comet/wandb/profiler/gap_monitor 默认 `enabled=False`，checkpointer 每 1000 步存正式 checkpoint、每 100 步存临时（ephemeral）checkpoint，评估器每 250 步跑一次验证集 ppl 和 hellaswag。

其中 `with_callback` 的定义在 [src/olmo_core/train/config.py:L76-L85](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/train/config.py#L76-L85) —— 它返回**新的**配置对象（`replace` + 深拷贝 callbacks 再添加），是不可变风格，所以可以链式调用。

**⑤ main()：dry-run 在环境准备之前。**

[src/examples/llm/train.py:L315-L325](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/examples/llm/train.py#L315-L325) —— `main()` 先 `build_config`；若 `--dry-run` 则 `rich.print(config)` 后**直接 return**，根本不调用 `prepare_training_environment()`。这意味着 dry-run 不需要分布式环境；而 `build_config` 只构造配置对象、不构建数据集（构建发生在后面的 `config.dataset.build()`），所以 dry-run 也不下载数据。

#### 4.1.4 代码实践

**实践：用 --dry-run 预览一份完整实验配置**

1. **实践目标**：不下载任何数据、不初始化分布式，亲眼看到五个 Config 组成的整棵配置树。
2. **操作步骤**：在仓库根目录执行（按脚本 docstring 的建议用 torchrun；由于 dry-run 提前返回，直接 `python` 也可以）：

   ```bash
   torchrun --nproc-per-node=1 src/examples/llm/train.py my-first-run --dry-run
   ```

3. **需要观察的现象**：终端用 rich 彩色打印出 `ExperimentConfig`，顶层是 `model / dataset / data_loader / trainer / train_module / init_seed / ...` 五大块加实验选项；`trainer.callbacks` 里能看到 9 个回调的名字。
4. **预期结果**：打印后正常退出，退出码 0；当前目录没有新增 checkpoint 目录（`/tmp/my-first-run` 也不会被创建，因为 trainer 尚未 build）。
5. 再试一次带覆盖的版本，观察数值变化：

   ```bash
   torchrun --nproc-per-node=1 src/examples/llm/train.py my-first-run --dry-run \
       --model-factory=olmo2_14M train_module.optim.lr=6e-3
   ```

   预期 `model` 块的字段变小、`train_module.optim.lr` 变为 `0.006`。（dotlist 覆盖行为在 `config_test.py` 中有正式断言；本条命令的打印效果待本地验证。）

#### 4.1.5 小练习与答案

**练习 1**：`--dry-run` 为什么不需要 GPU 和数据文件？
**答案**：`main()` 中 dry-run 分支在 `build_config()` 之后、`prepare_training_environment()` 之前直接 `return`；而 `build_config` 只创建配置对象（`TokenizerConfig.gpt2()` 也只是返回一个记录词表信息的 dataclass，见 [src/olmo_core/data/tokenizer.py:L122-L132](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/data/tokenizer.py#L122-L132)），数据集的真正读取发生在 `config.dataset.build()`，dry-run 走不到那一步。

**练习 2**：想把默认的 271M llama2 架构换成 OLMo-2 系列小模型，除了 `--model-factory=olmo2_14M` 还能怎么做？
**答案**：也可以在命令行传 dotlist 覆盖直接改 `model` 的结构字段，但更简单可靠的方式是换工厂方法——`TransformerConfig` 上的工厂方法（如 `olmo2_14M` 在 [src/olmo_core/nn/transformer/config.py:L587](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/config.py#L587)）封装了一组互相匹配的超参，`build_config` 里就是用 `getattr(TransformerConfig, opts.model_factory)` 按名字取用的。

**练习 3**：示例里 `global_batch_size=256*1024`，`rank_microbatch_size=16*1024`，假设 4 卡，每个 rank 每个优化器 step 要积累几个 microbatch？
**答案**：梯度累积步数 \( m = \frac{B_{global}}{W \times B_{micro}} = \frac{262144}{4 \times 16384} = 4 \)。即每个 rank 做 4 次「前向+反向+梯度累加」，才凑齐一次全局 step 的 batch。这就是「以 token 计」的 batch size 与经典「以样本计」的主要区别。

### 4.2 build() 模式：从 Config 到真实组件的装配线

#### 4.2.1 概念说明

Config 是图纸，`build()` 是施工。OLMo-core 里几乎每类组件都遵循同一约定：**Config 类提供 `build()`，输入它依赖的真实对象，输出可用的组件实例**。`build()` 之间有明确的依赖方向，示例 `train()` 函数里的五行代码就是一条装配线：

```text
TransformerConfig.build() ──────────► Transformer (meta 设备占位)
                                           │
TransformerTrainModuleConfig.build(model) ► TransformerTrainModule (含优化器/并行)
                                           │
NumpyDatasetConfig.build() ──────────► NumpyDataset
                                           │
NumpyDataLoaderConfig.build(dataset, dp_process_group=train_module.dp_process_group)
                                           │
TrainerConfig.build(train_module, data_loader) ──► Trainer
                                           │
                                      trainer.fit()
```

#### 4.2.2 核心流程

装配顺序不能颠倒，因为依赖是单向的：

1. **先造模型**（只需要自身配置）；
2. **再造 train module**（需要模型；优化器要对着模型参数建，FSDP 要切分模型参数）；
3. **再造数据集**（独立）；
4. **数据加载器需要数据集 + 数据并行进程组**——进程组在 train module 建好之后才存在（`train_module.dp_process_group`）；
5. **最后造 trainer**（需要 train module + data loader；回调里的评估器等还要拿到 trainer 引用）。

#### 4.2.3 源码精读

**① 装配线本体。**

[src/examples/llm/train.py:L109-L140](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/examples/llm/train.py#L109-L140) —— `train(config)` 函数全貌。关键五行（L116-L123）：

```python
model = config.model.build(init_device="meta")
train_module = config.train_module.build(model)
dataset = config.dataset.build()
data_loader = config.data_loader.build(dataset, dp_process_group=train_module.dp_process_group)
trainer = config.trainer.build(train_module, data_loader)
```

在调用之前还有 [L110-L114](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/examples/llm/train.py#L110-L114)：rank 0 打印配置、`seed_all(config.init_seed)` 统一所有设备的随机种子（定义在 [src/olmo_core/utils.py:L209-L218](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/utils.py#L209-L218)），保证权重初始化可复现。

**② 模型为什么建在 meta 设备上。**

[src/olmo_core/nn/transformer/config.py:L2084-L2094](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/config.py#L2084-L2094) —— `TransformerConfig.build(init_device=...)`，docstring 明确说「分布式场景下通常应设为 `"meta"`」：模型先按形状「占位」创建、不占显存，随后由 train module 里的并行设置（本例是 FSDP）统一落位/切分，避免每张卡先完整实例化一遍大模型。

**③ train module build：模型 + 优化器在此汇合。**

[src/olmo_core/train/train_module/transformer/config.py:L432-L457](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/train/train_module/transformer/config.py#L432-L457) —— `TransformerTrainModuleConfig.build(model, device=None, eval_only=False)` 把配置转成 `TransformerTrainModule`（若配置了流水线并行 `pp_config` 则返回 pipeline 变体）。`eval_only=True` 时跳过优化器构建。优化器（`AdamWConfig` 等）在内部按模型参数构建，所以模型必须先就位。

**④ 数据加载器 build：需要知道数据并行的进程组。**

[src/olmo_core/data/data_loader.py:L1121-L1137](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/data/data_loader.py#L1121-L1137) —— `NumpyDataLoaderConfig.build(dataset, *, dp_process_group=...)`：加载器要把全局 batch 切给数据并行的各个 rank，所以必须拿到进程组；示例直接传 `train_module.dp_process_group`。这是「装配顺序不可颠倒」的最直接证据。

**⑤ trainer build：组装 Checkpointer、区分两种回调。**

[src/olmo_core/train/config.py:L147-L218](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/train/config.py#L147-L218) —— `TrainerConfig.build(train_module, data_loader)`：先由内嵌的 `CheckpointerConfig` 构建 Checkpointer（继承 trainer 的 `save_overwrite`/`work_dir`），然后把 callbacks 分成两类——**已是实例**的回调直接挂上，**还是 Config** 的（如 `LMEvaluatorCallbackConfig`）在构造出 `Trainer` 之后再 `cb_config.build(trainer)`，因为它们需要 trainer 引用（L196-L216）。

**⑥ checkpoint 预加载逻辑。**

[src/examples/llm/train.py:L129-L137](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/examples/llm/train.py#L129-L137) —— 装配完成后、`fit()` 之前：若 save folder 里没有 checkpoint 且配置了 `load_path`，就从 `load_path` 加载权重（典型场景：从预训练模型微调）。`maybe_load_checkpoint()` 的返回值为 True 表示已加载，后续 `fit()` 里靠 `checkpoint_loaded` 标志避免重复加载。

#### 4.2.4 代码实践

**实践：源码阅读——亲手记录五个 build() 的输入输出**

1. **实践目标**：把 4.2.1 的装配线图变成自己验证过的事实。
2. **操作步骤**：
   - 打开 [src/examples/llm/train.py:L116-L123](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/examples/llm/train.py#L116-L123)，对每一行用「跳转到定义」找到对应 `build()` 的真实签名（本文 4.2.3 ②–⑤ 给出了四处）。
   - 做一张表：`build()` 所属 Config ｜ 输入参数 ｜ 返回类型 ｜ 定义文件与行号。
3. **需要观察的现象**：`NumpyDatasetConfig.build()` 是唯一不接收其它组件的 build；`data_loader` 的 build 是唯一需要进程组的；`trainer` 的 build 会额外构建 Checkpointer。
4. **预期结果**：五张「图纸」对应的「施工队」全部定位成功，且依赖顺序为 model → train_module → (dataset) → data_loader → trainer。
5. 本实践为源码阅读型，无需运行；表中的行号即为验证结果。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `data_loader` 的 build 要传 `train_module.dp_process_group`，而不是让 data_loader 自己建进程组？
**答案**：进程组的划分由并行策略决定（数据并行、张量并行等多维并行共享一套进程网格）。train module 是并行配置（`dp_config` 等）的持有者，进程组在那里创建；数据加载器只是「消费者」，拿到同一个组才能保证全局 batch 的切分与梯度规约的 rank 集合一致。

**练习 2**：`config.as_config_dict()`（[train.py:L126-L127](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/examples/llm/train.py#L126-L127)）的结果被塞给了哪个回调？有什么用？
**答案**：塞给了 `config_saver`（`ConfigSaverCallback`）。它会把整份实验配置随 checkpoint 一起保存（也供 W&B 记录），从而任何一次训练都能事后追溯到确切配置——这是「可复现实验」闭环的最后一块。

**练习 3**：如果把装配线第 1、2 行对调（先 build train module 再 build model）会发生什么？
**答案**：直接报错——`TransformerTrainModuleConfig.build(model)` 的第一个参数就是 `Transformer` 实例，没有模型就无法构建优化器、也无法做 FSDP 切分。Config 之间可以任意嵌套，但组件实例的构造必须按依赖顺序进行。

### 4.3 训练环境的准备与收尾：torchrun 与 prepare/teardown_training_environment

#### 4.3.1 概念说明

`torchrun` 负责拉起进程；进程内部则由 OLMo-core 的 `prepare_training_environment()` 统一完成「训练前的杂务」：设置多进程启动方式、初始化分布式进程组、配置日志、注册 cached-path 客户端（用于读远端数据）、设置随机种子。训练结束后 `teardown_training_environment()` 销毁进程组。这两个函数是所有训练脚本的标配外壳，`olmo_core.train` 包的 docstring 就把它写成了标准配方。

#### 4.3.2 核心流程

```text
torchrun 拉起 N 个进程（每进程设置 RANK/WORLD_SIZE/LOCAL_RANK 等环境变量）
        │
每个进程执行 main()
        ├── --dry-run 分支：打印配置退出（不走下面）
        ├── prepare_training_environment()
        │      ├── mp.set_start_method("spawn")       # 规避数据加载器段错误
        │      ├── init_distributed(backend="cpu:gloo,cuda:nccl")  # 建进程组 + 绑定设备
        │      ├── prepare_cli_environment(...)        # 日志/警告/异常钩子
        │      ├── add_cached_path_clients()           # 远端数据访问
        │      └── seed_all(seed)（可选）
        ├── train(config)：装配组件 + fit()
        └── teardown_training_environment()            # destroy_process_group()
```

#### 4.3.3 源码精读

**① 包 docstring 里的标准配方。**

[src/olmo_core/train/__init__.py:L18-L35](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/train/__init__.py#L18-L35) —— 官方推荐骨架：`prepare_training_environment()` → 构建 train module 与 data loader → `trainer_config.build(...)` → `trainer.fit()` → `teardown_training_environment()`。示例脚本就是这一配方的完整落地。

**② prepare_training_environment 的四个动作。**

[src/olmo_core/train/__init__.py:L85-L148](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/train/__init__.py#L85-L148) —— 函数体依次：

- L125-L129：把多进程启动方式设为 `"spawn"`（注释：规避 LUMI 上的数据加载器段错误）；
- L131-L135：`backend` 非空时调用 `init_distributed(backend=backend, ...)`（内部还会调用 `torch.cuda.set_device()` 或 `torch.set_default_device()`），`backend=None` 则走单进程路径。默认 `cpu:gloo,cuda:nccl`——docstring（L110-L115）解释了为什么要带一个 CPU 后端：让异步 checkpoint 和簿记通信走 GLOO，不阻塞 NCCL 上的训练通信；
- L137-L141：`prepare_cli_environment()` 配置日志（默认 `rank0_only`，低于 WARNING 的日志只有 rank 0 打印）与异常钩子；`add_cached_path_clients()` 注册远端文件系统客户端；
- L143-L145：若传入 `seed` 则 `seed_all(seed)`（示例没在这里传，而是在 `train()` 里用 `config.init_seed` 显式调用）。

**③ teardown。**

[src/olmo_core/train/__init__.py:L151-L156](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/train/__init__.py#L151-L156) —— `teardown_training_environment()`：分布式场景下 `dist.destroy_process_group()`，干净收尾。

#### 4.3.4 代码实践

**实践：观察 dry-run 与真实运行对环境依赖的差异**

1. **实践目标**：验证「dry-run 不触碰分布式初始化」这一设计。
2. **操作步骤**：
   - 先执行 `python src/examples/llm/train.py env-check --dry-run`，确认能正常打印并退出（不设置任何 `MASTER_ADDR` 等环境变量、不用 torchrun）。
   - 再故意用 `python` 直接去掉 `--dry-run` 跑一次：`python src/examples/llm/train.py env-check --save-folder=/tmp/env-check`，观察报错信息。
3. **需要观察的现象**：第一条命令成功；第二条命令在初始化分布式时失败（torchrun 之外缺少 `RANK`/`MASTER_ADDR` 等变量时的典型报错）。
4. **预期结果**：证明 `prepare_training_environment()` 才是分布式初始化的入口，dry-run 分支位于它之前。
5. 第二条命令的具体报错文本待本地验证（取决于 PyTorch 版本）；如果想真正启动训练，请改用 `torchrun --nproc-per-node=1 ...` 并确保数据可访问（见 5. 综合实践）。

#### 4.3.5 小练习与答案

**练习 1**：默认 backend 为什么是 `cpu:gloo,cuda:nccl` 而不是只有 `nccl`？
**答案**：docstring 说明：保留一个 CPU 后端（GLOO），异步 checkpoint 与簿记类集合通信可以在 CPU 后端上进行，不会阻塞 NCCL 上正在跑的训练通信。

**练习 2**：`prepare_training_environment(seed=42)` 与示例里 `train()` 中的 `seed_all(config.init_seed)` 是什么关系？
**答案**：`prepare_training_environment` 的 `seed` 参数是可选的，传入时会调用 `seed_all(seed)`。示例没有用这个参数，而是把种子放进 `ExperimentConfig.init_seed`，在 `train()` 里显式调用——好处是种子成为实验配置的一部分，会随 config_saver 存进 checkpoint，更容易复现。

**练习 3**：不调用 `teardown_training_environment()` 会怎样？
**答案**：进程组不会被销毁。对单次脚本运行通常只是不干净（进程退出时由操作系统回收），但在同一进程内连续跑多次训练、或使用多进程 spawn 的场景下，残留的进程组会导致后续 `init_distributed` 报「已初始化」或死锁等问题。

### 4.4 Trainer.fit()：训练主循环逐步拆解

#### 4.4.1 概念说明

`Trainer.fit()` 是整台机器的「总开关」。它负责：恢复 checkpoint（如需）→ 注册信号处理（让 Ctrl-C / `kill` 触发优雅保存）→ 调用各方 `pre_train` 钩子 → 先跑一个「dry-run batch」预热（编译模型、提前暴露 OOM）→ 循环执行 `_fit_epoch()` 直到 `training_complete` → `post_train` 钩子 → 收尾。真正的单步逻辑在 `_fit_epoch()` 里：一个 batch 的前向反向由 `train_module.train_batch()` 完成，优化器更新由 `train_module.optim_step()` 完成，Trainer 只负责编排与记账（步数、token 数、指标、回调）。

#### 4.4.2 核心流程

`fit()` 的骨架（对应源码 L687-L782）：

```text
fit()
 ├── 重置取消状态
 ├── 恢复 checkpoint（save folder 优先，其次 load_path；由 LoadStrategy 决定找不到时的行为）
 ├── barrier() 同步各 rank
 ├── 打印 callback 顺序
 ├── 注册 SIGTERM/SIGINT 处理器（收到信号 → 优雅取消）
 ├── 所有 callback.pre_train() + train_module.pre_train()
 ├── 若尚未训练完：_dry_run_batch()        # mock batch 预热，编译/抓 OOM
 ├── while not training_complete:
 │       _fit_epoch()
 ├── （异常时：callback.on_error(exc) → _shutdown(gracefully=False) → 重新抛出）
 ├── 所有 callback.post_train()
 └── _shutdown()                            # 等待簿记任务完成
```

`_fit_epoch()` 中每个 step 的顺序（对应源码 L1688-L1747）：

```text
zero_grads()
for batch in 数据加载器:
    global_step += 1；累计 tokens / petaflops
    （若命中 steps_to_skip 区间 → 本步跳过训练）
    callback.pre_step(batch)
    train_module.train_batch(batch)          # 前向 + 反向（可能含梯度累积）
    callback.pre_optim_step()
    train_module.optim_step()                # 优化器更新（含梯度裁剪）
    train_module.zero_grads()
    callback.post_train_batch()
    callback.post_step()
    （每逢 metrics_collect_interval 步 → _log_metrics()）
    若 training_complete → 提前返回
epoch += 1；data_loader.reset()
```

#### 4.4.3 源码精读

**① checkpoint 恢复策略。**

[src/olmo_core/train/trainer.py:L696-L728](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/train/trainer.py#L696-L728) —— `fit()` 先尝试从 save folder 恢复（断点续训场景，连 trainer 状态、优化器状态一起加载），找不到再看 `load_path`；若 `LoadStrategy.always` 且两处都没有 checkpoint，直接抛 `FileNotFoundError`，否则打 warning「从头训练」。

**② 信号处理与 dry-run batch。**

[src/olmo_core/train/trainer.py:L739-L757](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/train/trainer.py#L739-L757) —— 注册 SIGTERM/SIGINT 处理器（被抢占时能优雅保存）；训练前调用 `_dry_run_batch()`。

[src/olmo_core/train/trainer.py:L1660-L1669](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/train/trainer.py#L1660-L1669) —— `_dry_run_batch()` 用 `data_loader.get_mock_batch()` 造一个假 batch 跑一次 `train_batch(batch, dry_run=True)`：让 `torch.compile`（示例 `compile_model=True`）提前编译、提前暴露 OOM，避免训练几步后才炸。

**③ epoch 主循环与 step 编排。**

[src/olmo_core/train/trainer.py:L760-L761](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/train/trainer.py#L760-L761) —— `while not self.training_complete: self._fit_epoch()`：`training_complete` 由 `max_duration`（step/token/epoch 三种单位，见 [src/olmo_core/train/common.py:L36-L66](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/train/common.py#L36-L66) 的 `Duration`）驱动。

[src/olmo_core/train/trainer.py:L1691-L1734](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/train/trainer.py#L1691-L1734) —— 单个 step 的完整编排：记账（L1693-L1699）→ 跳步判定（L1701-L1706，`steps_to_skip` 区间）→ `pre_step` → `train_batch` → `pre_optim_step` → `optim_step` → `zero_grads` → `post_train_batch` → `post_step`。**Trainer 不亲自做前向反向**，全权委托 `train_module`；Trainer 的角色是「编排者 + 记账员 + 回调调度器」。回调钩子的插入位置在本讲只需建立直觉，第 6 单元会逐一展开。

**④ 异常路径。**

[src/olmo_core/train/trainer.py:L762-L781](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/train/trainer.py#L762-L781) —— 训练抛异常时：记录错误、触发 `callback.on_error(exc)`、`_shutdown(gracefully=False)`（不等簿记、不发额外集合通信，因为各 rank 已处于不一致状态，再做同步可能死锁），然后重新抛出；`finally` 恢复原信号处理器。正常路径则走 `post_train` 钩子与优雅 `_shutdown()`（L777-L782）。

#### 4.4.4 代码实践

**实践：从日志反推 fit() 的执行顺序**

1. **实践目标**：用日志证据验证 4.4.2 的流程图。
2. **操作步骤**：
   - 通读 [src/olmo_core/train/trainer.py:L730-L757](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/train/trainer.py#L730-L757)，列出 `fit()` 会输出的关键日志行（"Callback order:"、"Training for N steps"、"Starting forward/backward dry-run batch..."、"Dry-run complete"）。
   - 若本地具备运行条件（见 5. 综合实践），跑一次小训练，把这些日志行按出现顺序抄下来；不具备条件则在源码中标注每条日志对应的代码行。
3. **需要观察的现象**：日志顺序为 callback 列表 → （若设了 max_steps）训练步数 → dry-run 相关两行 → "Starting epoch 0..."。
4. **预期结果**：与 4.4.2 流程图一一对应；`_fit_epoch` 开头的 `"Starting epoch {epoch}..."` 日志（[L1683](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/train/trainer.py#L1683)）可作为 epoch 循环开始的标志。
5. 运行版本待本地验证；源码阅读版本可直接完成。

#### 4.4.5 小练习与答案

**练习 1**：`fit()` 里的 dry-run batch 和命令行的 `--dry-run` 是一回事吗？
**答案**：完全不同。命令行 `--dry-run` 在 `main()` 里打印配置后退出，什么都不训练；`_dry_run_batch()` 是 `fit()` 内部用 mock batch 做的一次真实前向+反向，用于提前编译和暴露 OOM，之后照常进入正式训练。

**练习 2**：`_fit_epoch()` 中 `pre_optim_step` 钩子为什么放在 `train_batch` 与 `optim_step` 之间，而不是 step 末尾？
**答案**：这个位置是「梯度已就绪、尚未应用更新」的窗口，适合做需要检查或修改梯度的操作（例如记录梯度范数）。放在末尾就看不到本步梯度了。回调系统正是靠这些精细切分的钩子位点实现无侵入扩展。

**练习 3**：示例配置 `metrics_collect_interval=5`（[train.py:L199](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/examples/llm/train.py#L199)）在 `_fit_epoch` 的哪里生效？
**答案**：[trainer.py:L1736-L1739](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/train/trainer.py#L1736-L1739)：`if first_batch or self.global_step % self.metrics_collect_interval == 0: self._log_metrics()`，即每 5 步（以及每个 epoch 的第一个 batch）收集一次指标，避免每步归约指标拖慢训练。

## 5. 综合实践

**任务：画出「从命令行到 trainer.fit()」的完整流程图，并完成一次最小配置的真实 dry-run（可选：进一步启动真实训练）。**

**步骤 1 —— 画流程图（必做，纯源码阅读）**

通读 [src/examples/llm/train.py](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/examples/llm/train.py) 全文，画一张覆盖以下要点的流程图：

1. 命令行如何分离「脚本参数」与「dotlist 覆盖」（L311 的 `parse_known_args`）；
2. `build_config` 内五个 Config 的构造与 `merge`（L143-L270）；
3. `main()` 中 dry-run 分支的位置（L319-L321）；
4. `train()` 装配线五行与依赖方向（L116-L123）；
5. `prepare/teardown_training_environment` 的包裹位置（L323-L325）；
6. `trainer.fit()` 内部：checkpoint 恢复 → pre_train → dry-run batch → epoch 循环（`_fit_epoch` 的单步顺序）→ post_train → shutdown。

**步骤 2 —— 最小配置 dry-run（本地零数据、零 GPU 可做）**

```bash
torchrun --nproc-per-node=1 src/examples/llm/train.py my-first-run --dry-run \
    --model-factory=olmo2_14M --sequence-length=1024 \
    data_loader.global_batch_size=16384 \
    train_module.rank_microbatch_size=16384
```

- 观察打印出的配置树：`model` 块应体现 14M 规格、`sequence_length=1024`；`data_loader.global_batch_size` 与 `train_module.rank_microbatch_size` 均为 16384（单位：token）。
- 按公式 \( m = B_{global} / (W \times B_{micro}) \) 验证：单卡时梯度累积步数应为 1。
- 预期：打印后立即退出，不产生任何文件、不联网下载数据。

**步骤 3 ——（可选）启动一次真实训练，明确数据与环境前提**

真实运行需要同时满足：

1. **数据**：`.npy` 格式的 c4 预分词数据。三选一——(a) 机器上有 AI2 内部路径；(b) 设 `OLMO_DATA_ROOT` 指向已下载文件的目录；(c) 什么都不做，让脚本从 `http://olmo-data.org/examples/c4-en/gpt2` 流式读取（慢，见 train.py L55-L64 注释，也可先 `wget http://olmo-data.org/examples/c4-en/gpt2/c4-train.00000-00099.npy`）。
2. **GPU**：默认配置是 FSDP + bfloat16 + `torch.compile`，面向 GPU 设计；CPU 上行为未定义/极慢。
3. **评估回调的数据与依赖**：`lm_evaluator`/`downstream_evaluator`（hellaswag）需要验证数据与相关依赖；若只想跑通训练循环，可在 dry-run 验证过 dotlist 语法后，尝试用覆盖禁用相关回调（具体可禁用字段名待本地验证）。
4. **保存目录**：`--save-folder` 不给时默认 `/tmp/<run_name>`。

在满足 1、2 的前提下：

```bash
torchrun --nproc-per-node=1 src/examples/llm/train.py my-first-run \
    --model-factory=olmo2_14M --sequence-length=1024 --save-folder=/tmp/my-first-run \
    data_loader.global_batch_size=16384 train_module.rank_microbatch_size=16384
```

预期现象：日志依次出现 callback 顺序、dry-run batch、`Starting epoch 0...`，随后周期性打印 loss/优化器指标；`/tmp/my-first-run` 下出现 checkpoint 目录结构。训练时长由 `trainer.max_duration` 控制（默认 `Duration.epochs(1)`，见 [src/olmo_core/train/config.py:L45](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/train/config.py#L45)；用 dotlist 把它改成 10 steps 的写法——例如 `trainer.max_duration.value=10 trainer.max_duration.unit=steps`——是否可行**待本地验证**）。

> 注意：本讲不会替你执行以上命令。所有「预期结果」均为基于源码的推断，请以本地实际输出为准，无法确定的都已标注「待本地验证」。

## 6. 本讲小结

- 一次实验 = 一份 `ExperimentConfig`：`model`/`dataset`/`data_loader`/`train_module`/`trainer` 五个 Config 积木，外加 `init_seed`、`load_path` 等实验级选项。
- **build() 模式**是 OLMo-core 的装配线：每个 Config 都有 `build()`，组件按依赖顺序构造（model → train_module → dataset → data_loader → trainer），进程组等共享资源沿这条链传递。
- 模型先在 `meta` 设备占位创建，再由 train module 的并行设置（默认 FSDP）落位/切分；batch 大小（`global_batch_size`、`rank_microbatch_size`）以 **token** 为单位。
- `--dry-run` 在 `prepare_training_environment()` 之前提前返回，所以零数据、零分布式环境即可预览整棵配置树；命令行剩余参数经 `config.merge(dotlist)` 覆盖任意嵌套字段。
- `prepare_training_environment()` 统一完成 spawn 设置、`init_distributed`、日志配置与 cached-path 注册，`teardown_training_environment()` 销毁进程组，二者是训练脚本的标配外壳。
- `Trainer.fit()` 是编排者：恢复 checkpoint → pre_train 钩子 → mock batch 预热 → `_fit_epoch()` 循环（pre_step → train_batch → pre_optim_step → optim_step → zero_grads → post_train_batch → post_step）→ post_train → 优雅收尾；前向反向与优化器更新全部委托给 `train_module`。

## 7. 下一步学习建议

至此单元一完结：你已经能读懂一个完整训练示例并安全地 dry-run 它。下一讲进入 [u2-l1-config-base-class.md](u2-l1-config-base-class.md)，拆开本讲反复使用的 `Config` 基类本身——`as_dict/save/load/merge` 的实现，以及为什么整套库建立在可序列化嵌套 dataclass 上。之后再回来精读本讲只当黑盒用的几块积木：`TransformerConfig`（u3-l2）、`TransformerTrainModuleConfig`（u6-l2）、`TrainerConfig` 与回调系统（u6-l1/u6-l3）。建议顺手浏览同目录的 `src/examples/moe/train.py` 与 `src/examples/llama_lns/train.py`——它们与 `llm/train.py` 骨架相同，只是换了个别积木，正好验证你对这套模式的理解。
