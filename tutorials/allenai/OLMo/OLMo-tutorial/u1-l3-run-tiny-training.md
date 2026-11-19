# 跑通第一个训练：tiny 配置实战

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `scripts/train.py` 从命令行敲下回车到 `trainer.fit()` 开始训练之间，入口脚本按顺序做了哪些事。
2. 掌握两种启动方式：多卡 `torchrun` 与 Mac/单机 `python` 直启，并理解脚本如何根据硬件自动选择后端（nccl/gloo）与分布式策略（ddp/fsdp/single）。
3. 理解命令行 `--setting=value`、`--setting.sub=value` 的覆盖机制：`clean_opt` 如何清洗参数、`BaseConfig.load` 如何用 OmegaConf dotlist 合并覆盖 YAML。
4. 对照 `configs/tiny/OLMo-20M.yaml` 的每个配置段（model/optimizer/scheduler/data/evaluators……），说出它被 `main()` 中哪一段代码消费。
5. 知道 `--dry_run=true` 到底跳过了什么、没跳过什么——这是排查「训练入口卡在哪一步」的关键。

本讲是整个学习路线第一次「真正按下运行键」。跑没跑通不要紧，重点是：通过一次真实（或失败的）运行，把入口流程在脑子里走一遍。

## 2. 前置知识

### 2.1 训练入口脚本是什么

大型项目的训练代码通常不直接从库调用，而是提供一个「入口脚本」：它负责解析命令行、初始化运行环境、把配置变成一个个具体对象（模型、数据加载器、优化器），最后才把控制权交给训练循环。OLMo 的入口脚本是 `scripts/train.py`，文件第一行就写明它的设计意图——[scripts/train.py:L1](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/scripts/train.py#L1)：

```python
"""Run this script with 'torchrun'."""
```

### 2.2 torchrun 与分布式基础概念

- **torchrun**：PyTorch 官方的分布式启动器。`torchrun --nproc_per_node=8 scripts/train.py ...` 会在本机启动 8 个 Python 进程，每个进程运行同一份脚本，并自动设置 `RANK`、`WORLD_SIZE`、`LOCAL_RANK`、`MASTER_ADDR`、`MASTER_PORT` 等环境变量。
- **rank / world_size**：rank 是进程在全局的编号（0 到 world_size−1），world_size 是参与训练的进程总数。
- **进程组（process group）**：多个 rank 之间通信的底层通道。初始化用 `torch.distributed.init_process_group`，需要一个**通信后端**：
  - `nccl`：NVIDIA GPU 专用，性能最好；
  - `gloo`：CPU / MPS（Apple 芯片）可用的通用后端。
- **barrier（栅栏）**：所有 rank 都到达同一位置才继续，用于同步节奏。

### 2.3 配置文件与命令行覆盖

OLMo 用 YAML 描述一次训练的全部超参数（模型多大、数据在哪、学习率多少）。但改一个超参数就复制一份几百行的 YAML 太浪费，所以入口支持命令行覆盖（见 [README.md:L73-L81](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/README.md#L73-L81)）：

```bash
torchrun --nproc_per_node=8 scripts/train.py {path_to_train_config} \
  --setting1=value \
  --setting2=value \
  --setting3.subsetting1=value
```

`--setting3.subsetting1=value` 里的点号表示「嵌套字段」，底层由 OmegaConf 的 dotlist 机制解析（本讲 4.3 会精读）。

### 2.4 dry run（空跑）是什么

`dry_run=true` 表示「把一切准备工作做完，但不真正开始训练」。注意：它跳过的是**保存与训练**，而数据下载、模型构建这些重活**照做**——这正是本讲实践任务要观察的核心现象。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `scripts/train.py` | 训练总入口 | `__main__` 引导流程（L382-436）与 `main(cfg)`（L54-379） |
| `configs/tiny/OLMo-20M.yaml` | 官方 tiny 训练配置（约 20M 参数的小模型） | 各配置段的含义与消费位置 |
| `README.md` | 项目说明 | 官方给出的 torchrun / Mac 启动命令 |
| `olmo/config.py` | 配置基类与 `TrainConfig` | `BaseConfig.load` 如何合并 YAML 与命令行覆盖（L128-148） |
| `olmo/util.py` | 通用工具 | `clean_opt` 清洗命令行参数（L214-219） |
| `scripts/prepare_memmap_dataset.py` | 数据准备脚本（下下讲主角） | 本讲综合实践借它的自测命令生成本地 `.npy` |

## 4. 核心概念与源码讲解

### 4.1 训练入口：scripts/train.py 的两段式结构

#### 4.1.1 概念说明

`scripts/train.py` 分成两段：

1. **引导段（`__main__` 块）**：解析命令行、初始化分布式进程组、根据硬件**改写配置**（设备与分布式策略）。
2. **主流程段（`main(cfg)` 函数）**：拿到一份「已适配本机」的配置，按固定顺序组装所有组件并启动训练。

这种拆分的好处：`main(cfg)` 只依赖一个 `TrainConfig` 对象，测试或二次开发时可以绕过命令行直接调用它；而所有「环境差异」（GPU 还是 Mac、几个进程）都被隔离在引导段。

#### 4.1.2 核心流程

引导段的决策树：

```text
python / torchrun 启动 scripts/train.py
        │
        ├─ 设置多进程启动方式为 spawn
        │
        ├─ 有 CUDA？── 是 ──> init_process_group(backend="nccl")   # GPU 集群路径
        ├─ 有 MPS？ ── 是 ──> 补设 RANK/WORLD_SIZE/MASTER_ADDR/MASTER_PORT
        │                      init_process_group(backend="gloo")  # Mac 路径
        └─ 都没有 ───        init_process_group(backend="gloo")  # CPU 路径
        │
        ├─ prepare_cli_environment()   # 日志、异常钩子、警告过滤
        ├─ add_cached_path_clients()   # 注册 Weka 等远端存储客户端
        │
        ├─ sys.argv[1] = YAML 路径，sys.argv[2:] = 覆盖参数
        ├─ cfg = TrainConfig.load(yaml_path, [clean_opt(s) for s in 覆盖参数])
        │
        ├─ MPS 设备？ ──> cfg.model.init_device="mps"; cfg.distributed_strategy="single"
        ├─ CPU 设备？ ──> cfg.model.init_device="cpu"; cfg.distributed_strategy="single"
        └─ main(cfg)
```

`main(cfg)` 的主线（本讲先建立骨架，各组件内部留到后续单元）：

```text
校验 run_name → 选设备(cuda/mps/cpu) → 配置补全(批大小/梯度累积)
→ rank0 保存 config.yaml → 可选 wandb.init → 固定随机种子
→ build_train_dataloader → build_evaluators → 构建 OLMo 模型
→ 可选 torch.compile → 激活检查点 → 按 ddp/fsdp/single 包装模型
→ build_optimizer + build_scheduler → 组装 Trainer
→ （可选）自动寻找最新 checkpoint / 保存 pre-train checkpoint
→ 非 dry_run ? trainer.fit() : 打印 "Dry run complete"
```

#### 4.1.3 源码精读

**引导段：按硬件初始化进程组。** 见 [scripts/train.py:L382-L436](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/scripts/train.py#L382-L436)。三个分支的关键差异：

- CUDA 分支用 `nccl` 后端、30 分钟超时，并显式传入 `device_id` 防止 0 号卡被无谓占用（[L388-L399](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/scripts/train.py#L388-L399)）：
  ```python
  dist.init_process_group(
      backend="nccl", timeout=timedelta(minutes=30), device_id=torch.device(device_as_string)
  )
  ```
- MPS 分支在**没有** `RANK` 等环境变量时手动补齐默认值，再用 `gloo` 后端（[L400-L409](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/scripts/train.py#L400-L409)）。这就是 README 敢让 Mac 用户直接 `python scripts/train.py ...` 的原因——脚本自己补齐了 torchrun 本会注入的环境变量。
- CPU 分支同样用 `gloo`（[L411-L412](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/scripts/train.py#L411-L412)）。

**引导段：加载配置并按设备改写。** 见 [L421-L436](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/scripts/train.py#L421-L436)：

```python
yaml_path, args_list = sys.argv[1], sys.argv[2:]
cfg = TrainConfig.load(yaml_path, [clean_opt(s) for s in args_list])
if torch.backends.mps.is_available():
    cfg.model.init_device = "mps"
    cfg.distributed_strategy = "single"
if not torch.cuda.is_available() and not torch.backends.mps.is_available():
    cfg.model.init_device = "cpu"
    cfg.distributed_strategy = "single"
main(cfg)
```

注意 tiny 配置里写的是 `init_device: cuda`、`distributed_strategy: ddp`，但在 Mac/CPU 上运行时**这两项会被这里强制改写**为 `mps`/`cpu` + `single`。所以「tiny 配置在我的笔记本上怎么也能跑」的答案就在这 6 行。

**main()：配置补全——从全局批大小推导每卡批大小与梯度累积。** 见 [L79-L83](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/scripts/train.py#L79-L83)：

```python
cfg.model.precision = cfg.precision
cfg.device_train_batch_size = cfg.global_train_batch_size // get_world_size()
cfg.device_train_grad_accum = cfg.device_train_batch_size // cfg.device_train_microbatch_size
```

以 tiny 配置为例：`global_train_batch_size=1024`、`device_train_microbatch_size=16`。在 8 卡（world_size=8）下每卡批大小 = 1024/8 = 128，梯度累积 = 128/16 = 8；而在 Mac/CPU 单进程（world_size=1）下变成 1024/16 = **64 次梯度累积**——同一个配置在不同规模机器上自动伸缩。

**main()：组件组装的主线。** 以下每个环节都对应一行关键代码：

| 步骤 | 代码位置 | 说明 |
| --- | --- | --- |
| 校验 run_name | [L56-L58](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/scripts/train.py#L56-L58) | `run_name` 为空直接抛 `OLMoConfigurationError` |
| 选设备 | [L69-L77](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/scripts/train.py#L69-L77) | cuda → mps → cpu 依次探测 |
| rank0 保存 config.yaml | [L94-L108](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/scripts/train.py#L94-L108) | `dry_run` 时**不保存**；目录已有旧 config 且未开 `save_overwrite` 会报错 |
| wandb 初始化 | [L112-L124](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/scripts/train.py#L112-L124) | `cfg.wandb` 非 None 才启动 |
| 固定种子 | [L128-L129](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/scripts/train.py#L128-L129) | `seed_all(cfg.seed)` |
| 训练数据加载器 | [L131-L132](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/scripts/train.py#L131-L132) | `build_train_dataloader(cfg)` |
| 评估器 | [L134-L135](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/scripts/train.py#L134-L135) | `build_evaluators(cfg, device)` |
| 构建模型 | [L138-L143](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/scripts/train.py#L138-L143) | `OLMo(cfg.model)`，并打印总参数量/非嵌入参数量 |
| 分布式包装 | [L154-L226](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/scripts/train.py#L154-L226) | ddp / fsdp / single 三分支 |
| 优化器与调度器 | [L236-L238](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/scripts/train.py#L236-L238) | `build_optimizer` + `build_scheduler` |
| 组装 Trainer | [L249-L261](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/scripts/train.py#L249-L261) | 所有组件注入 `Trainer(...)` |
| pre-train 检查点 | [L300-L344](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/scripts/train.py#L300-L344) | 训练前先存一个 checkpoint 再读回来，提前暴露磁盘/权限问题 |
| 训练/空跑分叉 | [L374-L379](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/scripts/train.py#L374-L379) | `if not cfg.dry_run: trainer.fit() else: log.info("Dry run complete")` |

**single 分支长什么样。** Mac/CPU 上实际走的是 [L221-L226](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/scripts/train.py#L221-L226)：模型 `.to(device)` 后包一层 `SingleAccelerator`——它只是让后续代码可以用统一接口（`dist_model`）对待单机与分布式模型，不做任何并行。

#### 4.1.4 代码实践

**实践 A：给 main() 的执行步骤排序（纯源码阅读，一定能完成）。**

1. **实践目标**：不看任何资料，仅凭阅读 [scripts/train.py:L54-L379](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/scripts/train.py#L54-L379)，写（画）出 `main(cfg)` 从进入函数到 `trainer.fit()` 的完整步骤清单。
2. **操作步骤**：
   1. 打开 `scripts/train.py`，从第 54 行开始逐段向下读，遇到函数调用（如 `build_train_dataloader`）先不进入，只记下它在第几行、做什么；
   2. 把每个步骤记为「行号 + 一句话」，格式如 `L132 build_train_dataloader(cfg)：构建训练数据迭代器`；
   3. 目标是至少列出 15 个步骤。
3. **需要观察的现象**：`main()` 里所有「跨 rank 协作」的代码（如 `barrier()`、`get_global_rank() == 0` 的判断）出现的位置。
4. **预期结果**：得到一份与 4.1.2 流程图对应的清单；能回答「wandb.init 在数据加载之前还是之后」（答案：之前，L112 早于 L132）。

**实践 B：dry run 试跑（可选，取决于网络环境）。**

1. **实践目标**：体验一次（可能失败的）真实启动，并精确定位失败发生在入口流程的哪一步。
2. **操作步骤**：
   1. Mac（M 系列，PyTorch ≥ 2.5）直接运行：
      ```bash
      python scripts/train.py configs/tiny/OLMo-20M.yaml --dry_run=true --save_overwrite=true --wandb=null
      ```
   2. 纯 CPU 的 Linux 机器上建议用 torchrun 单进程启动（由 torchrun 注入 `RANK` 等环境变量）：
      ```bash
      torchrun --standalone --nproc_per_node=1 scripts/train.py configs/tiny/OLMo-20M.yaml \
        --dry_run=true --save_overwrite=true --wandb=null
      ```
   3. 参数说明：`--dry_run=true` 只做准备工作不训练；`--save_overwrite=true` 允许覆盖已有输出目录；`--wandb=null` 跳过 W&B 登录（不加它，未登录 wandb 时会在数据下载之前就卡在 L112-124 的 `wandb.init`）。
3. **需要观察的现象**：日志依次打印到哪一条为止。tiny 配置的 `data.paths` 指向 `s3://ai2-llm/...` 的远端 `.npy`（见 [configs/tiny/OLMo-20M.yaml:L218-L221](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/configs/tiny/OLMo-20M.yaml#L218-L221)），README 也提醒数据是流式拉取的（[README.md:L83-L85](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/README.md#L83-L85)）。
4. **预期结果**：
   - 若网络与权限正常：日志走完「配置打印 → 构建数据加载器（开始拉 S3 数据）→ 构建评估器 → Building model... → 打印参数量」，最后出现 `Dry run complete`；
   - 若 S3 不可达/无权限：报错应发生在 **L132 `build_train_dataloader(cfg)`** 这一步（即「Construct data loader」处），因为 `dry_run` 只跳过保存（L99、L300）与训练（L374），**不跳过数据下载**。请把你的报错栈顶所在行号记录下来，与该结论对照。
   - 具体下载行为与耗时依赖网络环境，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `main()` 里在 L67、L110、L126 多次出现 `barrier()`？如果去掉会发生什么？

**参考答案**：`barrier()` 让所有 rank 在此同步。这些栅栏通常放在「rank 0 做独占操作（写 config.yaml、初始化 wandb 目录）」的前后，确保其他 rank 等它完成再继续，避免「非 0 进程先去读一个还没写好的文件/目录」。去掉后可能出现竞态：例如某 rank 在 rank0 创建 `wandb` 目录之前就往里写东西而报错。

**练习 2**：tiny 配置写的是 `distributed_strategy: ddp`，为什么在 Mac 上运行时不会真的走 DDP 包装？

**参考答案**：引导段在加载配置后检测到 MPS 设备，直接改写 `cfg.distributed_strategy = "single"`、`cfg.model.init_device = "mps"`（[L427-L430](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/scripts/train.py#L427-L430)），CPU 同理（[L432-L435](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/scripts/train.py#L432-L435)）。因此 `main()` 中 L154 的 DDP 分支不会命中，走的是 L221 的 `SingleAccelerator` 分支。

**练习 3**：L300-344 训练开始前先保存一个 checkpoint 又马上恢复它，为什么多此一举？

**参考答案**：这是「预检」：提前验证保存路径可写、磁盘空间足够、序列化/反序列化没问题。如果保存检查点都会失败，那么训练几小时后第一次保存时才发现就损失惨重了。这是长周期分布式训练里典型的 fail-fast（尽早失败）设计。

### 4.2 tiny 配置：OLMo-20M.yaml 逐段导读

#### 4.2.1 概念说明

`configs/tiny/` 下是官方提供的「玩具规模」配置，用来在单机上复现完整训练流程。`OLMo-20M.yaml` 描述的是一个约 2000 万参数的 decoder-only Transformer（作为对比，官方 OLMo-2 1B 配置是它的 50 倍、7B 是 350 倍）。tiny 配置的价值不在模型本身，而在于：

1. **麻雀虽小五脏俱全**：model/optimizer/scheduler/data/evaluators/wandb 全部段落齐备，是学习配置体系的最佳标本；
2. **官方真正用它做过训练**：`run_name: OLMo-20M`、`wandb.project: olmo-tiny` 说明它跑过正式的 tiny 实验并上传了 checkpoint；
3. **单机可启动**：README 的 Mac 示例用的就是它（[README.md:L87-L94](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/README.md#L87-L94)）。

一个需要注意的事实：该文件共 1269 行，其中**约 1000 行是 `data.paths` 列表**——训练数据全是 S3 上的预 tokenize `.npy` 文件。所以「读懂 tiny 配置」的重点是前 220 行。

#### 4.2.2 核心流程

配置段与消费代码的对应关系（本讲最重要的一张表）：

| YAML 段（行号） | 内容摘要 | 被 main() 哪里消费 |
| --- | --- | --- |
| `run_name`/`seed`/`dry_run`（L1-3） | 运行名、随机种子、空跑开关 | L56-58 校验；L129 种子；L99/L300/L374 dry_run 分支 |
| `wandb`（L5-7） | W&B 项目名 | L112-124 `wandb.init` |
| `model`（L9-39） | d_model=256、n_heads=8、n_layers=8、rope、swiglu…… | L140 `OLMo(cfg.model)`；L80 精度；L145-152 compile/激活检查点；L158 DDP 设备校验；L174 FSDP wrap 策略 |
| `ddp`（L41-43） | 梯度同步模式等 | L154-169 DDP 包装 |
| `compile`（L45） | `null` 表示不编译 | L145-150 |
| `optimizer`（L47-57） | adamw、lr=6e-4、weight_decay=0.1 | L237 `build_optimizer`；L84-92 废弃字段迁移 |
| `scheduler`（L59-63） | cosine_with_warmup、t_warmup=5000 | L238 `build_scheduler`；L362-367 BoltOn 重 warmup |
| `tokenizer`（L65-67） | 内置 GPT-NeoX 词表 | L134-135 `build_evaluators` 内部经 `Tokenizer.from_train_config` 用于下游评估（见 [olmo/eval/__init__.py:L111](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/olmo/eval/__init__.py#L111)）；训练数据本身已预 tokenize，无需 tokenizer |
| `save_folder`/`remote_save_folder`/保存策略（L69-75） | 本地/远端输出目录、unsharded 间隔 | L99-107 存 config；L300-336 pre-train checkpoint 与 checkpoint_type；L262-275 找最新 checkpoint |
| `load_path`（L77） | 断点续训入口，`null` 表示从头训 | L351-359 |
| `max_duration`/`stop_at`（L79-80） | 1 个 epoch / 406934 步上限 | `Trainer.fit()` 内部（第 4 单元精讲） |
| `global_train_batch_size`/`device_train_microbatch_size`（L81-82） | 1024 / 16 | L81-83 推导每卡批大小与梯度累积 |
| `precision`（L84） | amp_bf16 混合精度 | L80 |
| `distributed_strategy`（L85） | ddp | L154/L170/L221 三分支选择；引导段按设备改写 |
| `max_grad_norm`（L89-90） | 1.0 | 训练循环里做梯度裁剪（第 4 单元） |
| `eval_interval` 等（L92-97） | 每 5000 步评估 | `Trainer` 评估循环（第 4 单元） |
| `evaluators`（L98-204） | 1 个困惑度 lm 评估 + 约 30 个 downstream 评估 | L134-135 `build_evaluators` |
| `data`（L206-1269） | 加载器参数 + 上千个 S3 `.npy` 路径 | L131-132 `build_train_dataloader` |

#### 4.2.3 源码精读

**顶层三行：运行身份。** 见 [configs/tiny/OLMo-20M.yaml:L1-L3](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/configs/tiny/OLMo-20M.yaml#L1-L3)：

```yaml
run_name: OLMo-20M
seed: 6198
dry_run: false
```

`run_name` 是必需项（main L56 校验），同时被 YAML 内部变量引用——`${run_name}` 插值出现在 wandb 名（L6）和 save_folder（L69）里，这是 OmegaConf 的变量引用语法：改一处 `run_name`，输出目录与监控名同步变。

**model 段：20M 参数从哪来。** 见 [L9-L39](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/configs/tiny/OLMo-20M.yaml#L9-L39)。决定规模的是四个数：`d_model=256`（隐藏维度）、`n_heads=8`、`n_layers=8`、`mlp_ratio=8`（FFN 放大倍数），加上词表 `vocab_size=50280`。注意两个容易忽略的细节：

- `embedding_size: 50304` 与 `vocab_size: 50280` 故意不一致——嵌入矩阵取 64 的倍数（50304 = 50280 向上取整到 64 倍数），对 GPU 张量核心更友好，输出层再裁剪回词表大小。第 3 单元讲模型时会展开。
- `init_device: cuda`（L36）在 Mac/CPU 上会被引导段覆盖，前文已述。

**训练时长与批大小。** 见 [L79-L82](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/configs/tiny/OLMo-20M.yaml#L79-L82)：

```yaml
max_duration: 1ep        # 训 1 个 epoch
stop_at: 406_934         # 无论数据是否走完，最多这么多步
global_train_batch_size: 1024
device_train_microbatch_size: 16
```

`max_duration` 与 `stop_at` 是双保险：数据遍历一遍、或步数达到上限，先到者结束。

**evaluators 段：两类评估。** 见 [L98-L125](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/configs/tiny/OLMo-20M.yaml#L98-L125) 与 [L127-L204](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/configs/tiny/OLMo-20M.yaml#L127-L204)。第一个 evaluator（`all-small-ppl-validation`）没有 `type` 字段，默认是 `lm` 类型——在 11 份验证 `.npy` 上算困惑度；后面一长串显式标了 `type: downstream`（piqa、hellaswag、mmlu 系列等），是下游任务评估。它们全部在 L134-135 被一次性构建。

**data 段：加载参数 + 数据清单。** 段头见 [L206-L217](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/configs/tiny/OLMo-20M.yaml#L206-L217)：`num_workers: 32`（32 个数据加载子进程，单机跑时可用命令行覆盖调小）、`instance_filter`（过滤重复 token 的实例）等；`paths:` 从 [L218](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/configs/tiny/OLMo-20M.yaml#L218) 起一直列到文件末尾，注释里标了每段数据的来源与规模（Gutenberg 图书、PE S2O 论文、Wikipedia、MegaWika、StackExchange、arXiv、数学语料、Flan……），这正是 OLMo「数据全公开」理念的体现。同一个文件出现两次（如 wiki 的 part-0/part-1 在 L252-255 重复列出）是**用重复路径实现上采样**的手法。

#### 4.2.4 代码实践

1. **实践目标**：亲手完成「配置段 ↔ 消费代码」的映射，建立读任何 OLMo 配置的能力。
2. **操作步骤**：
   1. 打开 [configs/tiny/OLMo-20M.yaml:L1-L217](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/configs/tiny/OLMo-20M.yaml#L1-L217)（前 217 行即可，后面全是路径），为每个顶层键标注「被 `scripts/train.py` 第几行消费」；
   2. 对拿不准的键（如 `tokenizer`、`max_duration`），在仓库里用文本搜索确认它真正的消费者，而不是猜；
   3. 把结果整理成你自己的表格（可与 4.2.2 的表对照查漏）。
3. **需要观察的现象**：哪些配置段在 `main()` 里出现多次（如 `save_folder` 同时影响 config 保存、indices 文件、checkpoint 查找），哪些一次都没出现（说明消费在 `Trainer` 或更深处）。
4. **预期结果**：能独立回答——「我想改输出目录，改哪个键？」（`save_folder`）；「`--data.num_workers=2` 为什么能让单机跑得更稳？」（L131-132 构建的 DataLoader 用它决定子进程数）。

#### 4.2.5 小练习与答案

**练习 1**：tiny 配置里 `save_folder: workspace/${run_name}`，`${run_name}` 是什么语法？改 `run_name` 会影响哪些位置？

**参考答案**：这是 OmegaConf 的变量插值，引用同文件 L1 定义的 `run_name`。它影响：wandb 的 name（L6）、本地保存目录（L69）、远端保存目录（L70）。所以给每次实验换个 `run_name`（例如 `--run_name=my-first-run`）就能避免覆盖旧实验的输出。

**练习 2**：`embedding_size: 50304` 大于 `vocab_size: 50280`，多余的 24 个嵌入行用来做什么？

**参考答案**：纯粹为了硬件效率：50304 是 64 的倍数，嵌入矩阵和输出投影在这个形状下对 GPU 更友好（对齐）；真正推理时只用前 `vocab_size` 行，多余的行不参与任何 token 的表示。这属于「工程友好性压过语义洁癖」的设计，第 3 单元讲 `OLMo.__init__` 时会看到对应代码。

**练习 3**：为什么 tiny 配置要配 `stop_at: 406_934` 这种奇怪的数字，而不是整数？

**参考答案**：`stop_at` 是从实际训练需求反推出来的步数上限（由 token 预算 ÷ 全局批大小得出），不是拍脑袋的整数。它与 `max_duration: 1ep` 组成双保险：正常情况下 1 个 epoch 先结束；若数据目录被重复上采样导致 epoch 变长，`stop_at` 兜底防止训过头。

### 4.3 启动方式：torchrun、python 与命令行覆盖

#### 4.3.1 概念说明

OLMo 支持两种启动方式，对应两类硬件（[README.md:L63-L95](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/README.md#L63-L95)）：

| 方式 | 命令 | 适用环境 | 进程组 |
| --- | --- | --- | --- |
| torchrun | `torchrun --nproc_per_node=8 scripts/train.py <config>` | GPU 服务器/集群 | torchrun 拉起 N 个进程并注入环境变量，nccl 后端 |
| python 直启 | `python scripts/train.py <config>` | Mac（M 系列）等单机 | 脚本自己补默认环境变量，gloo 后端，single 策略 |

两者最终执行的是同一份 `scripts/train.py`，差异只由「环境变量是否存在 + 硬件探测结果」驱动。

命令行覆盖是第三种「不改文件改行为」的手段：任何 YAML 里的键都能用 `--key=value` 覆盖，嵌套键用点号。

#### 4.3.2 核心流程

一条覆盖参数从命令行到生效的完整管线：

```text
命令行: --setting.sub=value
   │
   ├─ clean_opt(s)                      # olmo/util.py L214-219
   │    ├─ 没有 "=" ? 追加 "=True"       # 所以 --save_overwrite 等价 --save_overwrite=true
   │    ├─ 去掉前导 "-"，"-" 换 "_"      # 所以 --save-overwrite 也合法
   │    └─ 产出 "setting.sub=value"
   │
   └─ TrainConfig.load(yaml, overrides) # olmo/config.py L128-148
        ├─ schema = structured(TrainConfig)   # 用 dataclass 定义做 schema
        ├─ raw   = load(yaml)                 # 读入 YAML 文件
        ├─ conf  = merge(schema, raw)         # YAML 覆盖默认值
        ├─ conf  = merge(conf, from_dotlist(overrides))  # 命令行再覆盖 YAML
        └─ to_object(conf) → TrainConfig 实例
```

优先级：**命令行 > YAML > dataclass 默认值**。且因为有 schema 校验，写错键名（如 `--run_nmae=x`）会在启动瞬间报 `OLMoConfigurationError`，而不是训练到一半才崩。

#### 4.3.3 源码精读

**README 的官方启动命令。** GPU 复现训练用 torchrun（[README.md:L63-L69](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/README.md#L63-L69)）：

```bash
torchrun --nproc_per_node=8 scripts/train.py {path_to_train_config}
```

Mac 直启示例（[README.md:L87-L95](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/README.md#L87-L95)）：

```bash
python scripts/train.py configs/tiny/OLMo-20M.yaml --save_overwrite
```

注意 `--save_overwrite` **没带值**——能这么写全靠 `clean_opt` 补 `=True`。

**clean_opt：命令行参数清洗。** 见 [olmo/util.py:L214-L219](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/olmo/util.py#L214-L219)：

```python
def clean_opt(arg: str) -> str:
    if "=" not in arg:
        arg = f"{arg}=True"
    name, val = arg.split("=", 1)
    name = name.strip("-").replace("-", "_")
    return f"{name}={val}"
```

只做三件事：补布尔默认值、剥横杠、连字符转下划线。它不解析类型——类型由 OmegaConf 按 schema 解释。

**BaseConfig.load：三层合并。** 见 [olmo/config.py:L128-L148](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/olmo/config.py#L128-L148)，关键三行：

```python
raw = om.load(str(path))                       # L139 读 YAML
conf = om.merge(schema, raw)                   # L143 YAML 覆盖默认值
conf = om.merge(conf, om.from_dotlist(overrides))  # L145 命令行覆盖 YAML
```

任何 OmegaConf 异常都被转成 `OLMoConfigurationError`（L147-148），给用户统一的报错出口。

**引用入口：引导段第 426 行。** [scripts/train.py:L426](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/scripts/train.py#L426) 把两者串起来：

```python
cfg = TrainConfig.load(yaml_path, [clean_opt(s) for s in args_list])
```

#### 4.3.4 代码实践

1. **实践目标**：验证覆盖机制的三条规则（优先级、布尔简写、schema 校验），全程不训练。
2. **操作步骤**：
   1. 在项目根目录运行（Python 直连配置系统，不启动训练）：
      ```bash
      python -c "
      from olmo.config import TrainConfig
      cfg = TrainConfig.load('configs/tiny/OLMo-20M.yaml', ['dry_run=true', 'run_name=probe-1'])
      print('run_name   =', cfg.run_name)
      print('dry_run    =', cfg.dry_run)
      print('seed(未覆盖) =', cfg.seed)
      "
      ```
   2. 再故意写错键名，观察报错：
      ```bash
      python -c "
      from olmo.config import TrainConfig
      TrainConfig.load('configs/tiny/OLMo-20M.yaml', ['run_nmae=typo'])
      "
      ```
3. **需要观察的现象**：第一次运行应输出 `run_name = probe-1`、`dry_run = True`、`seed = 6198`（YAML 原值未被触碰）；第二次运行应立刻抛出 `OLMoConfigurationError`，报错信息会指出未知键。
4. **预期结果**：三条规则全部命中——命令行值生效、未覆盖的键保持 YAML 值、非法键被 schema 拦截。若你的环境未安装依赖（`pip install -e .[all]` 未执行），会先报 `ModuleNotFoundError`，此时按第 1 讲的安装步骤补齐后再试。（本实践涉及实际运行，具体输出**待本地验证**。）

#### 4.3.5 小练习与答案

**练习 1**：`--save_overwrite`（无值）、`--save-overwrite=true`、`--save_overwrite=true` 三种写法是否等价？

**参考答案**：等价。`clean_opt` 会给无值参数补 `=True`，剥掉前导 `--`，并把连字符替换为下划线，三种写法最终都变成 `save_overwrite=true`。

**练习 2**：tiny 配置 `num_workers: 32` 在你的 8 核笔记本上可能拖慢甚至拖垮数据加载，如何不改 YAML 解决？

**参考答案**：命令行覆盖嵌套键：`--data.num_workers=2`。dotlist 的点号会定位到 `data.num_workers` 这一层，仅覆盖这一个值，`data` 段其余设置（`drop_last`、`instance_filter` 等）原样保留。

**练习 3**：为什么 `--wandb=null` 能跳过 W&B？如果写 `--wandb=false` 会怎样？

**参考答案**：`wandb` 字段的类型是 `Optional[WandBConfig]`，`null`（YAML 的空值）会把它设为 `None`，于是 main 的 L113 `if cfg.wandb is not None` 不成立，直接跳过 `wandb.init`。而 `false` 是布尔值，与 `Optional[WandBConfig]` 的 schema 类型不匹配，OmegaConf 合并时会报配置错误——这也体现了 schema 校验的价值：错误用法在启动时就暴露。

## 5. 综合实践

**任务：把 tiny 配置改造成「本地数据、本地运行、5 步训练」，完成你的第一次端到端训练。**

综合运用本讲三个模块：启动方式（4.3）、配置覆盖（4.3）、入口流程（4.1），并提前借用下一单元的数据准备工具。

1. **准备本地数据**。用 `scripts/prepare_memmap_dataset.py` 文档里自带的测试命令（见 [scripts/prepare_memmap_dataset.py:L7-L11](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/scripts/prepare_memmap_dataset.py#L7-L11)）：
   ```bash
   python scripts/prepare_memmap_dataset.py test_fixtures/*.json.gz -o /tmp/train.npy
   ```
   它会把仓库自带的测试语料 tokenize 成内存映射数组（原理在第 2 单元精讲，本讲只当黑盒工具用）。

2. **用命令行覆盖启动一次「最小训练」**（Mac 直接 `python`；CPU Linux 加 `torchrun --standalone --nproc_per_node=1`）：
   ```bash
   python scripts/train.py configs/tiny/OLMo-20M.yaml \
     --run_name=my-first-tiny-run \
     --save_overwrite=true \
     --wandb=null \
     --data.paths=['/tmp/train.npy'] \
     --data.num_workers=0 \
     --evaluators=[] \
     --max_duration=5 \
     --stop_at=5 \
     --global_train_batch_size=4 \
     --device_train_microbatch_size=4
   ```
   每个覆盖的理由：本地 `.npy` 替换 S3 路径；关掉 wandb 与评估器（评估数据同样在远端）；批大小缩到单机可承受；只训 5 步。

3. **验证**：
   - 日志中依次出现「Saving config → Building model → 参数量 ≈ 2 千万（`Total number of parameters`）→ Starting training... → Training complete」；
   - `workspace/my-first-tiny-run/` 下出现 `config.yaml` 与 checkpoint 目录；
   - 打开保存的 `config.yaml`，确认命令行覆盖都写进去了。

4. **记录与思考**：若某一步失败，按 4.1.2 的流程图定位它发生在哪一行对应的环节（这是比成功更有价值的练习产出）。数据准备脚本的输出格式、批大小与显存的关系等运行时表现，**待本地验证**。

## 6. 本讲小结

- `scripts/train.py` 是两段式结构：`__main__` 引导段负责进程组初始化（nccl/gloo）与按硬件改写配置，`main(cfg)` 负责按固定顺序组装 dataloader → evaluators → 模型 → 分布式包装 → 优化器 → Trainer，最后 `trainer.fit()`。
- 两种启动方式本质相同：`torchrun`（多卡，注入环境变量）与 `python` 直启（Mac MPS，脚本自己补默认环境变量）；Mac/CPU 上配置会被自动改写为 `single` 策略。
- 命令行覆盖的优先级是 命令行 > YAML > dataclass 默认值，由 `clean_opt` 清洗参数、`BaseConfig.load` 的两次 `om.merge` 完成，且 schema 校验让拼写错误在启动瞬间暴露。
- tiny 配置（`configs/tiny/OLMo-20M.yaml`）麻雀虽小五脏俱全：约 20M 参数的模型 + 完整 optimizer/scheduler/evaluators 段；文件 4/5 的篇幅是 S3 数据路径清单。
- `--dry_run=true` 只跳过「保存 config、pre-train checkpoint、训练」三件事，**数据下载照做**——这就是无网络环境下 dry run 仍会卡在 `build_train_dataloader` 的原因。

## 7. 下一步学习建议

下一讲（u1-l4「配置系统：BaseConfig 与 OmegaConf」）深入本讲只是路过的配置层：`BaseConfig.new/load/save` 的完整实现、`path.glob`/`path.choose`/`path.last_checkpoint` 三个 OmegaConf resolver，以及 `scripts/init_config.py` 生成配置的方式。

如果想先热身第 2 单元，可以提前浏览 `scripts/prepare_memmap_dataset.py` 的完整实现与 `olmo/tokenizer.py`——你在综合实践里当黑盒用的那个数据准备脚本，马上会被拆开讲清楚；若对入口里一带而过的 `Trainer` 与 `SingleAccelerator` 感兴趣，可直接跳看 [olmo/train.py](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/olmo/train.py) 的 `Trainer.fit()`，第 4 单元会逐行走读。
