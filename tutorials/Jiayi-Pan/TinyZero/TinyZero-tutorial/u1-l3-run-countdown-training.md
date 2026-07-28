# 跑通第一次训练：Countdown 任务

## 1. 本讲目标

学完本讲后，你应该能够：

- 独立走通「生成数据 → 设置环境变量 → 执行训练脚本」的 countdown 任务端到端流程。
- 读懂 `examples/data_preprocess/countdown.py` 如何把原始任务数据加工成 RL 训练用的 parquet。
- 读懂 `scripts/train_tiny_zero.sh`，理解它如何用一条 `python3 -m verl.trainer.main_ppo` 命令配合一组 Hydra 配置覆盖来启动训练。
- 区分「单卡（≤1.5B）」与「3B+ 模型」两套配置在 GPU 数量、张量并行度、实验命名上的差异。

## 2. 前置知识

承接前两讲，你已经知道：TinyZero = veRL 框架 + 任务数据 + 规则奖励；仓库里 `verl/` 是 vendored 进来的框架核心，`examples/` 是 TinyZero 自己贡献的数据预处理脚本，`scripts/` 是入口脚本。

本讲会用到下面几个概念：

- **parquet**：一种列式存储文件格式。veRL 的数据加载器（`RLHFDataset`，详见 u2-l3）期望训练数据以 parquet 形式提供，每条样本含 `prompt`、`reward_model.ground_truth`、`data_source` 等字段。
- **Hydra 配置覆盖**：veRL 用 Hydra + OmegaConf 管理配置（详见下一讲 u1-l4）。简单说，存在一份默认 yaml，命令行可以用 `a.b.c=value` 这种「点路径」覆盖任意子项；用 `+a.b.c=value` 则可以新增一个原本不存在的键。
- **环境变量替换**：shell 脚本里 `$N_GPUS`、`$BASE_MODEL` 等是你在运行前用 `export` 设置的环境变量，脚本执行时会原样替换进命令行。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `examples/data_preprocess/countdown.py` | countdown 任务的数据预处理脚本：把 HuggingFace 上的原始任务数据加工成训练用 parquet。 |
| `scripts/train_tiny_zero.sh` | 训练入口脚本：用一组 Hydra 配置覆盖调用 `verl.trainer.main_ppo`。 |
| `README.md` | 给出数据预处理命令、单卡/多卡的环境变量设置示例。 |
| `verl/trainer/main_ppo.py` | 训练真正的 Python 入口（本讲只确认它存在并作为调用终点，内部拼装留到 u4-l1）。 |
| `verl/utils/hdfs_io.py` | 数据预处理脚本里可选用于把结果上传到 HDFS 的工具函数。 |

## 4. 核心概念与源码讲解

### 4.1 Countdown 数据预处理与 parquet 生成

#### 4.1.1 概念说明

强化学习训练需要「输入 + 标准答案」的成对数据：输入是 prompt（给模型的问题），标准答案用来在训练时由规则奖励函数打分。countdown 任务的规则是：给定一个目标数和一组数字，用四则运算把这些数字拼成一个等于目标的算式。

数据预处理脚本 `countdown.py` 的职责，就是把原始任务数据加工成 veRL 数据加载器期望的 parquet 格式。这一步是「跑通训练」的第一个动作——没有 parquet 就无法启动训练。

#### 4.1.2 核心流程

用伪代码描述主流程（`if __name__ == '__main__'` 块）：

```
1. 解析命令行参数（local_dir、train_size、test_size、template_type 等）
2. 从 HuggingFace 加载原始数据集 Jiayi-Pan/Countdown-Tasks-3to4
3. 切分成 train / test 两份
4. 对每条样本调用 process_fn：
     a. make_prefix(...)  → 生成 prompt 文本
     b. 组装 data_source / prompt / reward_model.ground_truth / extra_info
5. 写出 train.parquet / test.parquet 到 local_dir
6. （可选）上传到 HDFS
```

这里要特别提醒一个「读代码别想当然」的点：脚本顶部定义了一个 `gen_dataset()` 函数，看起来像是「自己随机生成数据」，但**主流程并没有调用它**——真正取数据靠的是 `load_dataset('Jiayi-Pan/Countdown-Tasks-3to4')`。`gen_dataset` 是一段未在主路径使用的代码，不要被它的名字误导。

#### 4.1.3 源码精读

先看参数定义。[countdown.py:70-81](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/examples/data_preprocess/countdown.py#L70-L81) 用 argparse 定义了 `--local_dir`（parquet 输出目录）、`--train_size=327680`、`--test_size=1024`、`--template_type='base'` 等默认值。

主流程的取数入口在这里：[countdown.py:88](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/examples/data_preprocess/countdown.py#L88)——`load_dataset('Jiayi-Pan/Countdown-Tasks-3to4', split='train')` 才是真正下载数据的地方。

prompt 模板由 `make_prefix` 生成：[countdown.py:53-66](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/examples/data_preprocess/countdown.py#L53-L66)。注意两点：

1. prompt 末尾以 `<think>` 结尾——这是在「引导」模型直接开始写推理过程（相当于续写的起手），和 R1 Zero「让模型自己学会思考」的思路一致。
2. `base` 模板适合任意基座模型；`qwen-instruct` 模板用 `<|im_start|>` / `<|im_end|>` 这套 Qwen 对话格式，留给 instruct 版模型做对照实验（见 README 的 Instruct Ablation）。

每条样本的最终结构在 `process_fn` 里组装：[countdown.py:94-118](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/examples/data_preprocess/countdown.py#L94-L118)，产出 5 个字段：

| 字段 | 含义 |
|---|---|
| `data_source` | 固定字符串 `'countdown'`，训练时用它路由到对应的规则奖励函数 |
| `prompt` | 一个 `[{role:'user', content: ...}]` 列表，内容是 `make_prefix` 的输出 |
| `ability` | `'math'`，任务类别标记 |
| `reward_model` | `{"style":"rule", "ground_truth":{"target":..., "numbers":...}}`，标准答案 |
| `extra_info` | `split`、`index`，便于排查 |

最后写出 parquet：[countdown.py:126-131](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/examples/data_preprocess/countdown.py#L126-L131)——`to_parquet` 写本地，当 `hdfs_dir` 非空时用 `verl.utils.hdfs_io` 的 `copy` / `makedirs` 上传。`hdfs_io.copy` 本质是对 `shutil.copy` 的封装，只有当路径以 `hdfs://` 开头时才走 HDFS 命令：[hdfs_io.py:84-110](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/hdfs_io.py#L84-L110)。

#### 4.1.4 代码实践

实践目标：观察数据预处理「输入什么、输出什么」。

操作步骤：

1. 准备环境（承接 u1-l2 已装好 `datasets`、`verl`），激活 conda 环境。
2. 对照 README 执行：

   ```bash
   python ./examples/data_preprocess/countdown.py --local_dir ./data/countdown
   ```

3. 运行结束后查看 `./data/countdown/` 下是否生成 `train.parquet` 和 `test.parquet`。
4. 用 Python 加载 parquet 打印一条样本，核对上面表格的字段结构：

   ```python
   import pandas as pd
   df = pd.read_parquet('./data/countdown/train.parquet')
   print(df.iloc[0].to_dict())
   ```

需要观察的现象 / 预期结果：`data_source` 为 `countdown`；`prompt[0].content` 以 `<think>` 结尾；`reward_model.ground_truth` 含 `target` 与 `numbers`。

说明：这一步需要从 HuggingFace 下载 `Jiayi-Pan/Countdown-Tasks-3to4` 数据集，需要联网。如果你暂时无法下载，可改为「源码阅读型实践」——直接阅读 [countdown.py:94-118](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/examples/data_preprocess/countdown.py#L94-L118) 的 `process_fn`，确认输出字段结构与上表一致即可。

> 待本地验证：实际 parquet 文件大小、样本条数（应为 train_size=327680、test_size=1024）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 prompt 以 `<think>` 结尾，而不是以 `<answer>` 结尾？
**答案**：因为这是 base 模型的「续写」任务，末尾的 `<think>` 是在告诉模型「请从这里开始写推理」，把模型直接送进思考链路。

**练习 2**：如果把 `--template_type` 改成 `qwen-instruct`，`data_source` 会变吗？
**答案**：不会。`data_source` 在 [countdown.py:84](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/examples/data_preprocess/countdown.py#L84) 写死为 `'countdown'`，与模板无关。模板只影响 prompt 文本，不影响奖励路由。

**练习 3**：`gen_dataset` 函数在主流程里被调用了吗？
**答案**：没有。主流程用的是 `load_dataset('Jiayi-Pan/Countdown-Tasks-3to4')`（[countdown.py:88](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/examples/data_preprocess/countdown.py#L88)），`gen_dataset` 是一段未在主路径使用的代码。

### 4.2 训练脚本 train_tiny_zero.sh：配置覆盖与 main_ppo 入口

#### 4.2.1 概念说明

`scripts/train_tiny_zero.sh` 整个文件只有一条命令：调用 `python3 -m verl.trainer.main_ppo`，后面跟一大串 `key=value` 形式的参数。这些参数不是普通命令行参数，而是 **Hydra 配置覆盖**——它们会覆盖 `verl/trainer/config/ppo_trainer.yaml` 里的默认值（详见下一讲 u1-l4）。

换句话说，这个脚本本身不含任何训练逻辑，它只是一个「把环境变量和一组超参填进 main_ppo」的薄壳。

#### 4.2.2 核心流程

```
1. 读取环境变量：$DATA_DIR $BASE_MODEL $ROLLOUT_TP_SIZE $N_GPUS $EXPERIMENT_NAME
2. 执行 python3 -m verl.trainer.main_ppo，附带 Hydra 覆盖：
     - data.*              数据相关
     - actor_rollout_ref.*  actor/rollout/ref 三合一 worker
     - critic.*            价值模型
     - algorithm.*         算法（如 KL 系数）
     - trainer.*           训练控制（GPU 数、保存频率、epoch）
3. 输出重定向到 verl_demo.log
```

`python3 -m verl.trainer.main_ppo` 中的 `-m` 表示「把 `verl/trainer/main_ppo.py` 当作模块入口运行」，其 `if __name__=='__main__'` 处即是真正的训练驱动。这是本讲的调用终点；main_ppo 内部如何拼装各组件，留到 u4-l1。

#### 4.2.3 源码精读

整段脚本就一条命令：[train_tiny_zero.sh:1-31](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/scripts/train_tiny_zero.sh#L1-L31)。按分组拆解关键参数：

**数据分组（data.\*）**——[train_tiny_zero.sh:2-7](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/scripts/train_tiny_zero.sh#L2-L7)：

- `data.train_files=$DATA_DIR/train.parquet` / `data.val_files=$DATA_DIR/test.parquet`：正是上一节产出的 parquet。
- `data.train_batch_size=256`：每次训练更新用 256 条 prompt。
- `data.val_batch_size=1312`：验证时的 batch 大小。
- `data.max_prompt_length=256` / `data.max_response_length=1024`：prompt 最多 256 token，模型生成的回答最多 1024 token。

**Actor/Rollout/Ref 分组（actor_rollout_ref.\*）**——[train_tiny_zero.sh:8-17](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/scripts/train_tiny_zero.sh#L8-L17)：

- `actor_rollout_ref.model.path=$BASE_MODEL`：基座模型路径（Qwen2.5）。
- `actor_rollout_ref.actor.optim.lr=1e-6`：actor 学习率。
- `actor_rollout_ref.actor.ppo_mini_batch_size=64` / `ppo_micro_batch_size=8`：PPO 的梯度累积切分（mini→micro）。
- `actor_rollout_ref.rollout.tensor_model_parallel_size=$ROLLOUT_TP_SIZE`：rollout（vLLM 生成）的张量并行度。
- `actor_rollout_ref.rollout.gpu_memory_utilization=0.4`：vLLM 占用显存比例，留出显存给训练侧。

**Critic 与算法分组**——[train_tiny_zero.sh:18-21](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/scripts/train_tiny_zero.sh#L18-L21)：

- `critic.model.path=$BASE_MODEL` / `critic.optim.lr=1e-5`：critic 同样基于该基座，学习率比 actor 高一个量级（1e-5 vs 1e-6）。
- `algorithm.kl_ctrl.kl_coef=0.001`：KL 惩罚系数，防止策略跑离基座模型太远——这是 R1 Zero 里很关键的「缰绳」。

**Trainer 分组**——[train_tiny_zero.sh:22-31](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/scripts/train_tiny_zero.sh#L22-L31)：

- `trainer.logger=['wandb']`：用 wandb 记录实验。
- `+trainer.val_before_train=False`：注意前缀 `+`，表示新增/强制设置这个键，跳过「训练前先验证」。
- `trainer.n_gpus_per_node=$N_GPUS` / `trainer.nnodes=1`：单节点、`$N_GPUS` 张卡。
- `trainer.save_freq=100` / `test_freq=100`：每 100 步存 checkpoint 和验证一次。
- `trainer.total_epochs=15`：共训 15 个 epoch。
- 末尾 `2>&1 | tee verl_demo.log`：把标准输出和错误都写入日志文件。

#### 4.2.4 代码实践

实践目标：读懂脚本向 main_ppo 传递的关键参数，并能定位每个参数属于哪个分组。

操作步骤：

1. 打开 [train_tiny_zero.sh:1-31](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/scripts/train_tiny_zero.sh#L1-L31)。
2. 按下表补全「值」与「含义」两列：

   | 参数（点路径） | 分组 | 值（脚本里） | 含义 |
   |---|---|---|---|
   | `data.train_batch_size` | data | ? | ? |
   | `data.max_response_length` | data | ? | ? |
   | `actor_rollout_ref.actor.optim.lr` | actor_rollout_ref | ? | ? |
   | `algorithm.kl_ctrl.kl_coef` | algorithm | ? | ? |
   | `trainer.total_epochs` | trainer | ? | ? |

3. 在脚本里数一下一共有多少处 `actor_rollout_ref.` 开头的覆盖。

预期结果：你能不查文档说出 `kl_coef` 控制的是 KL 惩罚强度、`max_response_length=1024` 限制了模型一次回答最多生成多少 token。

> 说明：本实践为纯源码阅读型，无需运行即可完成。

#### 4.2.5 小练习与答案

**练习 1**：`+trainer.val_before_train=False` 前面的 `+` 是什么意思？
**答案**：这是 Hydra 语法。`+` 表示「新增一个默认配置里可能没有的键」并赋值；不带 `+` 的 `key=value` 只能覆盖已存在的键。

**练习 2**：actor 学习率（1e-6）和 critic 学习率（1e-5）哪个大？
**答案**：critic 更大（1e-5 > 1e-6）。本脚本里 critic 的学习率比 actor 高一个数量级。

**练习 3**：脚本末尾 `2>&1 | tee verl_demo.log` 的作用？
**答案**：`2>&1` 把 stderr 合并进 stdout，`tee` 把合并后的输出同时写到屏幕和 `verl_demo.log`，方便事后排查训练日志。

### 4.3 单卡 vs 多卡（3B）：环境变量与显存策略

#### 4.3.1 概念说明

同一段 `train_tiny_zero.sh` 既能跑小模型也能跑 3B 模型，区别全在你运行前 `export` 的环境变量。README 给出了两套典型配置。理解这套差异，是判断「我这台机器能跑哪个模型」的关键。

#### 4.3.2 核心流程

```
运行前 export：
  N_GPUS          → trainer.n_gpus_per_node
  BASE_MODEL      → actor_rollout_ref.model.path / critic.model.path
  DATA_DIR        → data.train_files / data.val_files
  ROLLOUT_TP_SIZE → actor_rollout_ref.rollout.tensor_model_parallel_size
  EXPERIMENT_NAME → trainer.experiment_name
  VLLM_ATTENTION_BACKEND=XFORMERS  → vLLM 进程直接读取的环境变量（不进 Hydra）

然后：bash ./scripts/train_tiny_zero.sh
```

注意 `VLLM_ATTENTION_BACKEND=XFORMERS` 不进 Hydra，它是直接给 vLLM 进程读的环境变量，用来指定注意力后端。

#### 4.3.3 源码精读

**单卡配置（model ≤ 1.5B）**——README 明确说明 Qwen2.5-0.5B 在该尺度学不会推理：[README.md:54-57](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/README.md#L54-L57)。对应的环境变量在 [README.md:60-67](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/README.md#L60-L67)：`N_GPUS=1`、`ROLLOUT_TP_SIZE=1`、`EXPERIMENT_NAME=countdown-qwen2.5-0.5b`。

**3B+ 配置**——README 明确说明该尺度能发展出复杂推理能力：[README.md:70-72](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/README.md#L70-L72)。对应环境变量在 [README.md:73-80](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/README.md#L73-L80)：`N_GPUS=2`、`ROLLOUT_TP_SIZE=2`、`EXPERIMENT_NAME=countdown-qwen2.5-3b`。

两套配置对照：

| 项 | 单卡（≤1.5B） | 3B+ |
|---|---|---|
| `N_GPUS` | 1 | 2 |
| `ROLLOUT_TP_SIZE` | 1 | 2 |
| `EXPERIMENT_NAME` | countdown-qwen2.5-0.5b | countdown-qwen2.5-3b |
| `VLLM_ATTENTION_BACKEND` | XFORMERS | XFORMERS |
| 预期 | 0.5B 学不会推理 | 涌现复杂推理 |

显存不够时，README 给出的兜底建议是在脚本里加 `critic.model.enable_gradient_checkpointing=True`：[README.md:52](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/README.md#L52)。

#### 4.3.4 代码实践

实践目标：根据你手头的 GPU，判断该用哪套配置，并实际 `export` 后启动训练（或至少跑通到日志开始打印）。

操作步骤：

1. 估算你单张 GPU 的显存（如 24G / 40G / 80G）。
2. 若只有 1 张卡：照 [README.md:60-67](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/README.md#L60-L67) export 单卡变量。
3. 若有 2 张卡、想跑 3B：照 [README.md:73-80](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/README.md#L73-L80) export 3B 变量。
4. 执行 `bash ./scripts/train_tiny_zero.sh`，观察终端是否进入 wandb 初始化和 Ray 启动阶段。
5. 若 OOM，按 README 建议加入 `critic.model.enable_gradient_checkpointing=True`，并考虑调小 `ppo_micro_batch_size`。

需要观察的现象 / 预期结果：日志里出现 Ray worker 启动、模型加载、第一步 rollout 生成的输出；`verl_demo.log` 持续写入。

> 待本地验证：实际能否在你的硬件上完成第一步训练（取决于显存与模型大小）。如果完全跑不起来也没关系——本讲的核心是「读懂流程」，真正的算法细节在后续讲义。

#### 4.3.5 小练习与答案

**练习 1**：为什么 3B 配置里 `ROLLOUT_TP_SIZE=2`，而单卡是 1？
**答案**：3B 模型较大，单卡放不下完整的 rollout 推理，需要用张量并行（tensor parallel）把模型切到 2 张卡上；单卡跑小模型则不需要切分。

**练习 2**：`EXPERIMENT_NAME` 在训练流程里起什么作用？
**答案**：它被填进 `trainer.experiment_name`，主要用于 wandb 实验命名与 checkpoint 目录区分，便于区分 0.5b / 3b / instruct 等不同实验。

**练习 3**：如果 3B 训练 OOM，README 建议的第一个调整是什么？
**答案**：加入 `critic.model.enable_gradient_checkpointing=True`，用「以计算换显存」的方式降低显存占用。

## 5. 综合实践

把本讲三节串起来，完成一次「端到端走查」：

1. 运行 `python ./examples/data_preprocess/countdown.py --local_dir ./data/countdown`（或阅读源码确认输出字段），得到 `train.parquet` / `test.parquet`。
2. 选定一套配置（单卡或 3B），用 `export` 设置 5 个核心环境变量（`N_GPUS`、`BASE_MODEL`、`DATA_DIR`、`ROLLOUT_TP_SIZE`、`EXPERIMENT_NAME`）。
3. 在 `train_tiny_zero.sh` 里，用不同颜色或注释分别标出 data、actor_rollout_ref、algorithm、trainer 四组覆盖参数。
4. 写一句话总结数据流：从「一条命令」到「训练开始」，数据是怎么从 parquet 流进 main_ppo 的（提示：`data.train_files` 指向 parquet → main_ppo 读取 → 内部数据加载器加载）。

交付物：一张标注好的脚本（截图或文本注释）+ 一段数据流说明。

## 6. 本讲小结

- countdown 数据预处理在 `examples/data_preprocess/countdown.py`，真正取数靠 `load_dataset('Jiayi-Pan/Countdown-Tasks-3to4')`，而非看起来会生成数据的 `gen_dataset`（后者未被主流程调用）。
- 预处理产物是 parquet，含 `data_source`、`prompt`、`reward_model.ground_truth` 等字段；`data_source='countdown'` 用于后续奖励路由。
- `scripts/train_tiny_zero.sh` 是一个 Hydra 配置覆盖的薄壳，真正的入口是 `python3 -m verl.trainer.main_ppo`。
- 关键超参包括 `data.train_batch_size=256`、`data.max_response_length=1024`、`algorithm.kl_ctrl.kl_coef=0.001`、`trainer.total_epochs=15`。
- 单卡与 3B 的区别全在运行前的环境变量：`N_GPUS` / `ROLLOUT_TP_SIZE` 决定并行度，`EXPERIMENT_NAME` 区分实验。
- 硬件不够时可开 `critic.model.enable_gradient_checkpointing=True` 兜底。

## 7. 下一步学习建议

到这里你已经能把训练「跑起来」（或至少读懂跑起来的流程）。但要真正理解「为什么这样配」，下一讲 **u1-l4 配置系统：Hydra 与 ppo_trainer.yaml** 会拆开 `verl/trainer/config/ppo_trainer.yaml` 这份默认配置文件，讲清 Hydra 的分组结构与命令行覆盖的对应关系。

更长远地，建议之后沿着 **u2（数据与任务定义）→ u3（DataProto 与单控制器）→ u4（PPO 主流程）** 的顺序，逐步进入 `main_ppo` 内部，看清「这条命令到底做了什么」。
