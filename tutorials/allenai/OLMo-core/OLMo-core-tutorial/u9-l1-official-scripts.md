# 官方训练脚本解析：如何复现 OLMo-2/OLMo-3

## 1. 本讲目标

学完本讲，你应该能够：

1. 读懂 `src/scripts/official/` 下任何一份官方训练脚本：它们共享同一套「`build_config(opts, overrides)` + `main()`」骨架。
2. 说出 `ExperimentConfig` 五块积木（model / dataset / data_loader / train_module / trainer）在真实配方中各自承担的内容。
3. 理解 OLMo 系列的分阶段训练法：预训练（可能分两段）、midtrain / anneal、long-context 各阶段在**数据、学习率调度、checkpoint 恢复、并行策略**上的差异。
4. 用 `--dry-run` 零成本查看一份官方配比的完整配置，并用 dotlist 覆盖安全地修改它。

本讲是「读方」训练：前面九个单元讲的是积木怎么造，本讲讲 AI2 自己怎么用这些积木搭出 7B / 32B 模型。

## 2. 前置知识

本讲默认你已读过以下讲义（只回顾结论，不重复推导）：

- **u1-l4 / u2-l3**：一次训练实验 = 一份 `ExperimentConfig`，由五块 Config 积木组成；`--foo-bar=x` 形式的命令行参数会被 `Config.merge(dotlist)` 在「字典世界」里按点号下钻覆盖，多态子类身份（`_CLASS_` / `type` 线索）不丢失。
- **u4-l3**：`DataMix` 枚举对应 `src/olmo_core/data/mixes/` 下的 `.txt` 配方文件，每行 `label,path`；`NumpyFSLDatasetConfig.from_data_mix()` 只是记录 mix 名字，真正解析发生在 `build()` 阶段。
- **u5-l2**：学习率调度器是纯函数 `get_lr(initial_lr, current, t_max)`，`current` 取 trainer 的 `global_step`；余弦曲线相对总步数（horizon）定义，**换阶段必须重设曲线**——本讲会看到官方脚本怎么做。
- **u6-l1 / u6-l4**：`Duration` 以 steps/tokens/epochs 计量训练时长，`hard_stop` 可提前刹车而不改变 LR 曲线；`LoadStrategy`（if_available/always/never）约定断点续训行为，命中 save_folder 是续训语义（强制恢复 trainer 与优化器状态），命中 load_path 是初始化语义。
- **u7-l1 / u7-l2**：HSDP 是「副本 × 分片」的数据并行折中；上下文并行（CP）切序列维度，`llama3` 是其负载均衡模式。

本讲新术语：

| 术语 | 含义 |
|---|---|
| **midtrain / anneal（退火）** | 预训练主体结束后，换用小批量高质量数据、把学习率线性降到 0 的收尾阶段 |
| **long-context（长上下文扩展）** | 在已训模型上把序列长度从 8K 扩到 64K+ 的继续训练阶段 |
| **souping（模型汤）** | 把多次独立训练得到的 checkpoint 逐参数平均，合并成一个更强模型 |
| **hard_stop** | 训练中途提前停下的阈值，不改变学习率曲线的形状 |

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `src/scripts/official/README.md` | 官方脚本目录的总说明：统一 CLI、torchrun / Beaker 两种启动方式 |
| `src/scripts/official/OLMo3/OLMo-3-1025-7B-pretrain-1.py` | OLMo-3 7B 预训练第 1 段（本讲的主线脚本） |
| `src/scripts/official/OLMo3/OLMo-3-1025-7B-pretrain-2.py` | OLMo-3 7B 预训练第 2 段：换调度器延长训练 |
| `src/scripts/official/OLMo3/OLMo-3-1025-7B-midtrain.py` | OLMo-3 7B 中期训练阶段 |
| `src/scripts/official/OLMo3/OLMo-3-1025-7B-long-context.py` | OLMo-3 7B 长上下文扩展阶段 |
| `src/scripts/official/OLMo2/OLMo-2-0325-32B-train.py` | OLMo-2 32B 预训练（单段，含按 GPU 数分支的写法） |
| `src/scripts/official/OLMo2/OLMo-2-0325-32B-anneal.py` | OLMo-2 32B 退火阶段（演示自定义 CLI 与从 checkpoint 推断 LR） |
| `src/scripts/official/OLMo3/README.md` | OLMo-3 三阶段训练的官方文档（数据、token 数、GPU 数） |
| `src/olmo_core/script_utils.py` | `ExperimentConfig`、`get_cli_parser()`、`main()` 与 `get_lr_from_checkpoint()` |
| `src/olmo_core/optim/scheduler.py` | 三个阶段用到的调度器：`CosWithWarmup` / `HalfCosWithWarmup` / `LinearWithWarmup` |

## 4. 核心概念与源码讲解

### 4.1 官方脚本骨架：build_config() 与 ExperimentConfig

#### 4.1.1 概念说明

`src/scripts/official/` 下的每一份脚本都不是「可执行的业务逻辑」，而是**一份用 Python 写的配方**。脚本只做一件事：定义函数 `build_config(opts, overrides) -> ExperimentConfig`，把模型、数据、优化、训练四方面的 Config 积木组装起来，最后交给 `olmo_core.script_utils.main()` 去执行。

这个设计解决了大规模训练中的一个工程痛点：**配方必须可序列化、可 diff、可审计**。32B 模型一次训练花几十万 GPU 时，任何超参争议都要能回溯到一份确定的配置文件——所以官方配方不用 YAML 手写，而用 Python 代码表达（可以用循环、常量、条件分支），但在出口处收敛为一个可序列化的 `ExperimentConfig`。

`ExperimentConfig` 本身只有七个字段——五块积木加两个全局量：

```python
@dataclass
class ExperimentConfig(Config):
    model: TransformerConfig
    dataset: NumpyDatasetConfig
    data_loader: NumpyDataLoaderConfig
    train_module: TransformerTrainModuleConfig
    trainer: TrainerConfig
    init_seed: int = 12536
    load_path: Optional[str] = None
```

参见 [src/olmo_core/script_utils.py:L28-L36](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/script_utils.py#L28-L36)：前五个字段正是 u1-l4 见过的五块积木；`init_seed` 是全局随机种子；`load_path` 指向一个用来**初始化权重**的 checkpoint（阶段衔接的关键，见 4.4）。

#### 4.1.2 核心流程

每份官方脚本的主干都是同一套三段式：

```text
① 常量区      —— 本配方的关键超参提为模块级常量（LR、GLOBAL_BATCH_SIZE…）
② build_config(opts, overrides)
      ├─ 从 opts 读正式 CLI 参数（--sequence-length、--data-root…）
      ├─ 依依赖顺序构造五块 Config：
      │    tokenizer → model → dataset → data_loader → train_module → trainer
      │    （trainer 用链式 .with_callback(...) 挂回调）
      └─ return ExperimentConfig(...五块...).merge(overrides)   ← 命令行覆盖在此生效
③ if __name__ == "__main__": main(build_config)
```

注意 ② 的最后一步：`merge(overrides)` 把所有命令行 dotlist 覆盖应用到这份 Python 配方上，**然后才**返回。也就是说「代码定基线、命令行做微调」——这正是 u2-l3 讲过的机制在官方脚本里的落点。

#### 4.1.3 源码精读

以 OLMo-3 7B 预训练第 1 段为主线。模块级常量定义了这份配方的「身份证」：

参见 [src/scripts/official/OLMo3/OLMo-3-1025-7B-pretrain-1.py:L40-L42](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/scripts/official/OLMo3/OLMo-3-1025-7B-pretrain-1.py#L40-L42)：序列长度 8192，全局批大小 `8192 * 512`（以 token 计，约 4M tokens/step），学习率 3e-4。

模型一行搞定——u3-l2 的工厂方法在这里兑现价值：

参见 [src/scripts/official/OLMo3/OLMo-3-1025-7B-pretrain-1.py:L47-L52](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/scripts/official/OLMo3/OLMo-3-1025-7B-pretrain-1.py#L47-L52)：`TransformerConfig.olmo3_7B(...)` 直接给出 7B 的全部结构参数，脚本只需补两个现实约束——词表按 tokenizer 填充到 128 的倍数（`padded_vocab_size()`），注意力后端显式指定为 `flash_2`。

数据两块：

- 参见 [src/scripts/official/OLMo3/OLMo-3-1025-7B-pretrain-1.py:L54-L61](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/scripts/official/OLMo3/OLMo-3-1025-7B-pretrain-1.py#L54-L61)：数据集用 `NumpyFSLDatasetConfig.from_data_mix(DataMix.OLMo_mix_0625_official, ...)` 从官方 mix 配方构建。`from_data_mix` 只是记录 mix 名（见 [src/olmo_core/data/numpy_dataset.py:L2805-L2823](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/numpy_dataset.py#L2805-L2823)），**不接触任何数据文件**——这就是 `--dry-run` 能离线跑的原因。
- 参见 [src/scripts/official/OLMo3/OLMo-3-1025-7B-pretrain-1.py:L63-L67](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/scripts/official/OLMo3/OLMo-3-1025-7B-pretrain-1.py#L63-L67)：数据加载器只给 `global_batch_size`、`seed`、`num_workers` 三个数。

训练模块是配方的心脏，浓缩了 u5/u6/u7 三个单元的知识：

参见 [src/scripts/official/OLMo3/OLMo-3-1025-7B-pretrain-1.py:L69-L91](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/scripts/official/OLMo3/OLMo-3-1025-7B-pretrain-1.py#L69-L91)：`rank_microbatch_size=2*8192`（每 rank 每步 16K token 的 micro-batch）；优化器 `SkipStepAdamWConfig`（lr 3e-4、weight_decay 0.1、betas (0.9, 0.95)），并用 `OptimGroupOverride` 把 `embeddings.weight` 的权重衰减单独置 0（u5-l1 的参数组覆盖）；调度器 `CosWithWarmup(warmup_steps=2000)`；并行 `TransformerDataParallelConfig(name=hsdp, param_dtype=bf16, reduce_dtype=fp32, wrapping_strategy=blocks)`（u7-l1 的 HSDP）；`z_loss_multiplier=1e-5` 与 `max_grad_norm=1.0` 是稳定性双保险。

trainer 块用链式调用挂回调：

参见 [src/scripts/official/OLMo3/OLMo-3-1025-7B-pretrain-1.py:L93-L129](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/scripts/official/OLMo3/OLMo-3-1025-7B-pretrain-1.py#L93-L129)：`max_duration=Duration.tokens(5e12)` 定基调，`hard_stop=Duration.steps(597046)` 中途刹车（注释说明：原计划 5T，跑到这一步决定延长到 7T，于是有了 pretrain-2）；随后依次挂 `monkey_patcher`、`checkpointer`（每 1000 步永久保存、不开临时 checkpoint、同步保存）、`comet`/`wandb`（默认 `enabled=False`，注释提醒手动打开）、`config_saver`。注意 `comet` 和 `wandb` 同时挂着但只启用一个——两个互斥的日志后端以配置开关共存，是「可序列化配方」才能做到的优雅处理。

评估回调也在 trainer 块里：L130-L142 挂 `lm_evaluator`（困惑度监控，每 10K 步），L143-L183 挂 `downstream_evaluator`（27 个「fast」下游任务，每 10K 步），参见 [src/scripts/official/OLMo3/OLMo-3-1025-7B-pretrain-1.py:L130-L183](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/scripts/official/OLMo3/OLMo-3-1025-7B-pretrain-1.py#L130-L183)。u8-l1 讲过：评估是回调而非训练循环正文——在配方里它们看起来就是两个普通回调。

出口两行收束一切：

参见 [src/scripts/official/OLMo3/OLMo-3-1025-7B-pretrain-1.py:L186-L196](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/scripts/official/OLMo3/OLMo-3-1025-7B-pretrain-1.py#L186-L196)：`ExperimentConfig(五块).merge(overrides)`，然后 `main(build_config)`。

#### 4.1.4 代码实践（源码阅读型）

**目标**：不看输出、只靠读代码，写出一份官方配方的完整字段树。

**步骤**：

1. 打开 [src/scripts/official/OLMo3/OLMo-3-1025-7B-pretrain-1.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/scripts/official/OLMo3/OLMo-3-1025-7B-pretrain-1.py)。
2. 对五块积木各写一行摘要：`model = olmo3_7B + flash_2`、`dataset = FSL + OLMo_mix_0625_official`……
3. 对 `train_module` 再下钻两层，写出 `optim.lr`、`optim.scheduler.warmup_steps`、`dp_config.name`、`float8_config.enabled` 等叶子字段的值。
4. 数一数 trainer 挂了几个回调，按挂载顺序列出名字。

**需要观察的现象**：你会发现自己不需要理解任何训练机制，就能把这份 7B 配方完整转述——这就是「一切皆 Config」的收益。

**预期结果**：`train_module.optim.scheduler.warmup_steps` 为 2000，`dp_config.name` 为 `hsdp`，回调共 8 个（monkey_patcher、checkpointer、comet、wandb、config_saver、lm_evaluator、downstream_evaluator，外加 u6-l3 讲过的默认自动补齐回调）。

#### 4.1.5 小练习与答案

**练习 1**：官方脚本的五块积木里，为什么 `tokenizer_config` 不是独立的一块？
**答案**：tokenizer 信息以 `TokenizerConfig` 的形式嵌在 dataset（`tokenizer=` 参数）和下游评测回调（`tokenizer=` 参数）里。`TokenizerConfig` 只携带词表与特殊 token 元信息而非分词逻辑（u4-l1），数据已在离线阶段预分词完成，训练时不需要一个独立的分词组件。

**练习 2**：`GLOBAL_BATCH_SIZE = 8192 * 512` 注释写着 "~4M tokens"。请解释这个乘法的两个因子各来自哪里。
**答案**：8192 是序列长度（每实例 token 数），512 是全局的实例数（跨所有 rank、含梯度累积）。OLMo-core 的 `global_batch_size` 以 token 计（u4-l1），所以两者相乘就是每步消耗的 token 数：8192 × 512 = 4,194,304 ≈ 4M。

**练习 3**：为什么 `embeddings.weight` 要用 `OptimGroupOverride` 单独设 `weight_decay=0.0`？
**答案**：权重衰减会把参数往 0 拉。嵌入层是查表结构，把整个词表向量拉向 0 会让低频词的表示退化；而线性层权重的衰减有正则化收益。所以官方配方把 embedding 排除出衰减组（机制见 u5-l1 的 `OptimGroupOverride` + fnmatch 匹配）。

### 4.2 统一 CLI 与 main() 执行流水线

#### 4.2.1 概念说明

目录里十来份脚本共享**同一个 CLI**。[src/scripts/official/README.md:L5-L10](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/scripts/official/README.md#L5-L10) 写明：脚本用 `torchrun` 直接启动（或 Beaker 用户经 `python -m olmo_core.launch.beaker`，详见 u9-l3）；正式参数用 `--help` 查看；`--dry-run` 打印最终配置即退出；任意字段可用 dotlist 覆盖，例如 `--data_loader.prefetch_factor=4`。

统一 CLI 的实现靠 `argparse.parse_known_args` 的「分流」技巧：正式参数（`--name`、`--sequence-length`、`--data-root`、`--save-folder`、`--work-dir`、`--dry-run`、`--train-single`）被声明在 parser 里正常解析，**其余一切 `--a.b.c=v` 形式的参数不报错、原样收进 `overrides` 列表**，最终交给 `Config.merge()`。于是 CLI 表面积恒定：7 个正式参数 + 无限个覆盖项。

#### 4.2.2 核心流程

`main(build_config)` 的完整流水线：

```text
main(build_config)
 ├─ _parse_args()            → (opts, overrides)；work_dir 未给时按 save_folder 是否 URL 推断
 ├─ 若 --dry-run：prepare_cli_environment() → config = build_config(...) → rich.print(config) → return
 ├─ 若 --train-single：把 train_module 的 dp_config / tp_config 置 None（单进程没法并行）
 ├─ prepare_training_environment(...)   ← 初始化分布式进程组（torchrun 已设好环境变量）
 ├─ seed_all(config.init_seed)          ← 所有设备统一播种
 ├─ 依依赖顺序 build 五块：
 │    model(meta 设备占位) → train_module(含优化器与并行化) → dataset
 │    → data_loader(需要 dp_process_group) → trainer
 ├─ 把 config.as_config_dict() 注入 ConfigSaverCallback（存进每个 checkpoint）
 ├─ 恢复逻辑：
 │    save_folder 里有 checkpoint？→ maybe_load_checkpoint()（尊重 TrainerConfig 的
 │      load_strategy / load_trainer_state / load_optim_state 字段，续训语义）
 │    没有，但 config.load_path 有值？→ load_checkpoint(load_path, load_trainer_state=False)
 │      （初始化语义：只拿权重，不恢复训练进度）
 ├─ trainer.fit()
 └─ teardown_training_environment()
```

有一个容易忽略的细节：`fit()` 内部还有一道**兜底恢复**（参见 [src/olmo_core/train/trainer.py:L687-L716](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L687-L716)：先试 save_folder 并强制恢复 trainer+优化器状态，再回落 load_path）。`main()` 在 `fit()` 之前已尝试过一次，`checkpoint_loaded` 标志保证不会重复加载——两道恢复共用 u6-l4 讲过的 `Checkpointer` 底层。

#### 4.2.3 源码精读

CLI 参数声明：

- 参见 [src/olmo_core/script_utils.py:L39-L65](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/script_utils.py#L39-L65)：`--name`、`--sequence-length`、`--data-root`（默认公开的 `https://olmo-data.org`，AI2 内部建议走 Weka/GCS 路径——这是配方里唯一「基础设施相关」的参数）。
- 参见 [src/olmo_core/script_utils.py:L66-L74](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/script_utils.py#L66-L74)：`--save-folder` 是**唯一必填**参数，必须是所有 rank 都能访问的共享路径（NFS 或 `s3://`/`gs://`）。
- 参见 [src/olmo_core/script_utils.py:L81-L90](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/script_utils.py#L81-L90)：`--dry-run`（打印配置退出）与 `--train-single`（单 rank 调试）。

参数分流与 work_dir 推断：

参见 [src/olmo_core/script_utils.py:L94-L104](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/script_utils.py#L94-L104)：`parse_known_args` 把未知参数收进 `overrides`；save_folder 是 URL 时 work_dir 落到 `/tmp/olmo-core/dataset-cache`（数据预处理缓存放本地盘，checkpoint 落云存储）。

dry-run 分支——本讲实践的核心依据：

参见 [src/olmo_core/script_utils.py:L112-L119](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/script_utils.py#L112-L119)：dry-run 只做日志初始化、构建 config、`rich.print(config)` 后直接 return。**不初始化分布式、不 build 模型、不碰数据**——所以没有 GPU、没有数据、没有 torchrun 也能跑，是审阅配方最便宜的方式。

单 rank 调试分支：

参见 [src/olmo_core/script_utils.py:L121-L133](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/script_utils.py#L121-L133)：`--train-single` 用 `getattr` 探测式地把 `dp_config`/`tp_config` 置 None 并打警告——单进程跑不了数据/张量并行。

环境准备、播种与 build 顺序：

- 参见 [src/olmo_core/script_utils.py:L135-L142](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/script_utils.py#L135-L142)：有 CUDA 时用 `cpu:gloo,cuda:nccl` 双后端；`seed_all`（[src/olmo_core/utils.py:L209](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/utils.py#L209)）统一播种。
- 参见 [src/olmo_core/script_utils.py:L144-L149](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/script_utils.py#L144-L149)：build 顺序是 model（`init_device="meta"` 占位）→ train_module → dataset → data_loader（参数是 `train_module.dp_process_group`，依赖关系一目了然）→ trainer。这与 u1-l4 / u6-l2 讲的「并行化必须位于模型 build 之后、优化器 build 之前」一致。

配置固化与恢复：

- 参见 [src/olmo_core/script_utils.py:L151-L155](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/script_utils.py#L151-L155)：把 `config.as_config_dict()` 塞进 `ConfigSaverCallback`——u2-l1 讲过的 `as_config_dict()`（带 `_CLASS_`/`type` 双线索）在这里兑现：最终配置随每个 checkpoint 落盘，形成可复现闭环。
- 参见 [src/olmo_core/script_utils.py:L157-L163](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/script_utils.py#L157-L163)：恢复分支。注意 `load_path` 回落分支**硬编码** `load_trainer_state=False`——load_path 的定位是「拿一份权重来初始化」，而不是「接着别人往下训」；真正续训要走 save_folder 路径（尊重 `TrainerConfig.load_trainer_state` 字段）。
- 参见 [src/olmo_core/script_utils.py:L165-L169](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/script_utils.py#L165-L169)：`trainer.fit()` 与 `teardown_training_environment()` 收尾。

#### 4.2.4 代码实践（可运行）

**目标**：用 `--dry-run` 零成本展开一份 7B 官方配方的完整配置树。

**操作步骤**（前提：已按 u1-l2 完成 `pip install -e '.[all]'`；无需 GPU、无需数据、无需 torchrun）：

```bash
# 在仓库根目录
python src/scripts/official/OLMo3/OLMo-3-1025-7B-pretrain-1.py \
  --save-folder=/tmp/olmo3-7b-dryrun \
  --dry-run
```

**需要观察的现象**：终端用 rich 打印出整棵配置树（非常长，可以 `| less` 翻页）。重点找：

1. `model` 块里 `d_model`、`n_layers`、`attention` 的展开值（u3-l2 的 olmo3_7B 预设）；
2. `train_module.optim` 的 `type` 字段（多态线索，应显示 `skip_step_adamw` 一类的注册名）；
3. `trainer.callbacks` 下 8 个回调各自的配置。

**预期结果**：打印完成后进程直接退出，退出码 0，`/tmp` 下不会产生 checkpoint（dry-run 在 build 之前就返回了）。（具体打印格式待本地验证。）

**延伸**：对 OLMo-2 32B 脚本跑同样命令也可以——它 `build_config` 里调用了 `get_world_size()`（无 torchrun 时按单进程兜底返回 1，见 u7-l3），dry-run 阶段不会校验 `num_replicas` 的合法性。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `--dry-run` 不需要下载数据，而正式训练需要？
**答案**：`from_data_mix` 只记录 mix 名（4.1.3 引用的 `numpy_dataset.py:L2805-L2823`），文件清单解析与索引构建发生在 `dataset.build()`；dry-run 在任何 build 之前返回，所以离线可用。

**练习 2**：`--save-folder` 为什么是唯一必填参数？
**答案**：它是训练副作用的唯一出口：checkpoint、日志、配置固化（`ConfigSaverCallback`）全部落在 save_folder。其余一切都有合理默认（数据根有公开 URL、序列长度有脚本默认值），唯独「存到哪」无法替你决定，且多节点下还要求共享文件系统或对象存储 URL。

**练习 3**：`--train-single` 与「不用 torchrun 直接 `python` 启动」是一回事吗？
**答案**：不是。不用 torchrun 时 `prepare_training_environment` 会走单进程兜底（u7-l3）；`--train-single` 是显式声明「我只要单 rank 跑」，它会主动把 `dp_config`/`tp_config` 置 None 并打警告，防止误用并行配置。两者常组合使用：`python script.py --train-single ...`。

### 4.3 预训练配方对读：OLMo-2 32B 与 OLMo-3 7B

#### 4.3.1 概念说明

两份预训练脚本是同一骨架的两代产品。对读它们能看出：**架构演进（OLMo-2 → OLMo-3）如何落在配置差异上**，以及**同一脚本如何适配不同集群规模**。这一节也示范一个通用技能——用 `diff` 读两份同构脚本，把注意力集中在真正不同的行上。

#### 4.3.2 核心流程

对读方法：

```text
diff OLMo-2-0325-32B-train.py OLMo-3-1025-7B-pretrain-1.py
 → 忽略：import 区、回调链的共有部分、任务清单
 → 聚焦五类差异：模型工厂 / 数据 mix / 批与序列 / 优化与并行 / 集群适配分支
```

#### 4.3.3 源码精读

两份配方逐项对照：

| 维度 | OLMo-2 32B（[train.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/scripts/official/OLMo2/OLMo-2-0325-32B-train.py)） | OLMo-3 7B（[pretrain-1.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/scripts/official/OLMo3/OLMo-3-1025-7B-pretrain-1.py)） |
|---|---|---|
| 模型 | `TransformerConfig.olmo2_32B`（[L47-L49](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/scripts/official/OLMo2/OLMo-2-0325-32B-train.py#L47-L49)） | `TransformerConfig.olmo3_7B` + `flash_2` 后端（[L49-L52](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/scripts/official/OLMo3/OLMo-3-1025-7B-pretrain-1.py#L49-L52)） |
| 数据 mix | `OLMoE_mix_0824` | `OLMo_mix_0625_official`（dolma3） |
| 序列长度 | 4096 | 8192 |
| 全局批（token） | 2048×4096 ≈ 8M | 8192×512 ≈ 4M |
| micro-batch | 4×4096 | 2×8192 |
| 学习率 | 6e-4 | 3e-4 |
| 激活检查点 | `full` 模式 | 不开（7B 显存压力小） |
| 临时 checkpoint | 每 200 步、异步保存 | 不开 |
| 特有回调 | `gpu_monitor` | `monkey_patcher` |
| 训练时长 | 6.5T tokens 一段跑完 | 5T 计划、597046 步刹车分两段 |

OLMo-2 32B 的两个集群适配细节值得精读：

- 参见 [src/scripts/official/OLMo2/OLMo-2-0325-32B-train.py:L81-L89](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/scripts/official/OLMo2/OLMo-2-0325-32B-train.py#L81-L89)：HSDP 的 `num_replicas=get_world_size() // 64`——每 64 卡（一个节点）一个副本，副本数随集群规模自动伸缩，注释 `# NOTE: tune this` 提醒按实际网络调整（u7-l1：副本换通信、分片换显存）。
- 参见 [src/scripts/official/OLMo2/OLMo-2-0325-32B-train.py:L94-L100](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/scripts/official/OLMo2/OLMo-2-0325-32B-train.py#L94-L100)：**脚本内的运行时分支**——`get_world_size() >= 1024` 时把 micro-batch 减半、激活检查点从 `full` 换成只对 `blocks.*.feed_forward` 的 `selected_modules`。这说明 `build_config` 是普通 Python 函数，可以读 world size 做条件配置——配方不一定是静态的。

规模与资源可交叉验证官方文档：[src/scripts/official/OLMo3/README.md:L53-L57](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/scripts/official/OLMo3/README.md#L53-L57) 记录 OLMo-3 7B 三阶段分别用 512 / 128 / 256 张 H100；[src/scripts/official/OLMo2/README.md:L7-L13](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/scripts/official/OLMo2/README.md#L7-L13) 记录 OLMo-2 32B 两阶段及最终 souping 出的 main 分支。

#### 4.3.4 代码实践（工具型）

**目标**：用 `diff` 提取两份官方配方的真实差异。

**步骤**：

```bash
diff -u src/scripts/official/OLMo2/OLMo-2-0325-32B-train.py \
        src/scripts/official/OLMo3/OLMo-3-1025-7B-pretrain-1.py | less
```

**需要观察的现象**：diff 块大致按脚本纵向顺序排列（imports → 常量 → model → dataset → train_module → trainer）。

**预期结果**：手工核对 diff 输出与 4.3.3 表格一一对应；你会看到两份脚本约 70% 的行完全相同（回调链、优化器骨架、z_loss 等）——官方脚本之间的「代际差异」远小于表面篇幅。

#### 4.3.5 小练习与答案

**练习 1**：OLMo-3 7B 为什么不开激活检查点，而 32B 要开 `full`？
**答案**：激活检查点用重计算换显存（u6-l2 的 ac_config）。7B 模型激活内存占用小，关掉可以省去重计算的时间；32B 不开会 OOM，所以开 `full`，且在 1024 卡时进一步换成只查前馈层的 `selected_modules` 以提速。

**练习 2**：`num_replicas=get_world_size() // 64` 在 512 卡时是多少？这意味着什么？
**答案**：8 个副本、每副本 64 卡分片。副本间梯度需要 all-reduce，副本内参数分片——节点内 NVLink 带宽高，正好把分片通信锁在节点内（u7-l1 的默认「每节点一副本」策略）。

**练习 3**：两份脚本的 `--data-root` 默认值都是 `https://olmo-data.org`，为什么 AI2 内部建议换 Weka/GCS 路径？
**答案**：公开 URL 是给社区复现用的，带宽有限；Weka 是 AI2 集群的共享文件系统、GCS/S3 是云上桶，走内网/专线快得多。`--data-root` 因此成为配方里唯一强基础设施耦合的正式参数。

### 4.4 分阶段训练：两段预训练、midtrain/anneal 与 long-context

#### 4.4.1 概念说明

现代 LLM 很少一段训完。[src/scripts/official/OLMo3/README.md:L23-L26](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/scripts/official/OLMo3/README.md#L23-L26) 描述 OLMo-3 的三阶段：先在大量网页数据（dolma3）上预训练，再换小批量高质量数据（dolma3-dolmino）做中期训练，最后在含长文档的高质量数据（dolma3-longmino）上扩展上下文。OLMo-2 则两段：大量预训练 + dolmino 退火，最后 souping。

分阶段的本质是**同一个 `main()` 流水线跑多次，每次换配方**。阶段衔接靠三样东西：

1. **`load_path`**：新阶段的起点权重（初始化语义，4.2.3 已讲）；
2. **调度器重设**：u5-l2 的结论——余弦曲线相对总步数定义，换阶段必须换曲线；
3. **数据 mix 更换**：u4-l3 的 `DataMix` 机制让换数据只是换一个枚举值。

#### 4.4.2 核心流程

三个调度器的数学（完整实现见 `src/olmo_core/optim/scheduler.py`）。记峰值学习率为 \( lr_0 \)，衰减下限 \( \eta_{min} = \alpha_f \cdot lr_0 \)，去掉 warmup 后的进度 \( \tilde t = t - w \)、\( \tilde T = T - w \)：

**CosWithWarmup**（[scheduler.py:L457-L506](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/scheduler.py#L457-L506)）——完整余弦：

\[ lr(t) = \eta_{min} + (lr_0 - \eta_{min}) \cdot \frac{1 + \cos\!\left(\pi \tilde t / \tilde T\right)}{2} \]

**HalfCosWithWarmup**（[scheduler.py:L509-L564](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/scheduler.py#L509-L564)）——余弦的**后半段**。实现上（L560-L563）先做 \( \tilde t \leftarrow \tilde t + \tilde T \)、\( \tilde T \leftarrow 2\tilde T \) 再代入同一余弦公式；其 warmup 段不是升到 \( lr_0 \)，而是升到余弦中点 \( \eta_{min} + (lr_0-\eta_{min})/2 \)（L555）。类文档字符串强调：\( lr_0 \) 仍按完整余弦的峰值理解。

**LinearWithWarmup**（[scheduler.py:L356-L405](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/scheduler.py#L356-L405)）——线性衰减：

\[ lr(t) = lr_0 - (lr_0 - \eta_{min}) \cdot \tilde t / \tilde T \]

退火阶段取 \( \alpha_f = 0 \)（衰到零）且 \( w = 0 \)（不再预热）。

四阶段衔接图：

```text
pretrain-1                pretrain-2                 midtrain                long-context
CosWithWarmup    ──load──▶ HalfCosWithWarmup  ──load──▶ LinearWithWarmup ──load──▶ LinearWithWarmup
(5T 计划,597046 步刹车)    (7T 总目标,从中点续衰)      (100B,衰到 0)           (YaRN 扩到 64K,衰到 0)
   网页数据 dolma3           同一数据继续              高质量 dolmino           长文档 longmino
```

#### 4.4.3 源码精读

**pretrain-2：换调度器延长训练**。参见 [src/scripts/official/OLMo3/OLMo-3-1025-7B-pretrain-2.py:L70-L72](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/scripts/official/OLMo3/OLMo-3-1025-7B-pretrain-2.py#L70-L72)：从第一段的常量重算 `original_max_steps = 5e12 // GLOBAL_BATCH_SIZE = 1,192,092`、`original_warmup_steps = 2000`。随后：

- 参见 [src/scripts/official/OLMo3/OLMo-3-1025-7B-pretrain-2.py:L85-L87](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/scripts/official/OLMo3/OLMo-3-1025-7B-pretrain-2.py#L85-L87)：`HalfCosWithWarmup(warmup_steps=original_max_steps // 2 + original_warmup_steps // 2)`，即 warmup = 596,046 + 1,000 = **597,046——恰好等于 pretrain-1 的 hard_stop 步数**。设计意图：让恢复后的步数坐标正好落在完整余弦的中点，warmup 段「虚拟重放」已训练过的前半程，实际训练直接进入后半段余弦。LR 仍写 3e-4（HalfCos 按完整余弦峰值理解 \( lr_0 \)，起点自然落在中点值上）。
- 参见 [src/scripts/official/OLMo3/OLMo-3-1025-7B-pretrain-2.py:L100-L112](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/scripts/official/OLMo3/OLMo-3-1025-7B-pretrain-2.py#L100-L112)：`load_path` 指向发布在 HF bucket 的 step596047 checkpoint，`LoadStrategy.always`，并在 TrainerConfig 显式声明 `load_trainer_state=True`（恢复步数计数，是上述坐标对齐的前提）；`max_duration` 从 5T 改 7T、`hard_stop=Duration.epochs(1)`。注意 4.2.3 讲过 `main()` 的 load_path 回落分支硬编码 `load_trainer_state=False`，而 `fit()` 的兜底路径与 save_folder 路径会尊重该字段——两个恢复入口行为不同，实际生效哪个取决于 save_folder 是否已有 checkpoint（这正是值得用 dry-run + 日志核实的细节，**待本地验证**）。

**midtrain：高质量数据收尾**。参见 [src/scripts/official/OLMo3/OLMo-3-1025-7B-midtrain.py:L40-L44](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/scripts/official/OLMo3/OLMo-3-1025-7B-midtrain.py#L40-L44)：批缩到 2M token、总量 100B、学习率 0.000207（一个写死的小数——它就是上个阶段 checkpoint 停下时的学习率，OLMo-3 团队直接把数值抄进了脚本）；参见 [L56-L63](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/scripts/official/OLMo3/OLMo-3-1025-7B-midtrain.py#L56-L63)：数据换成 `OLMo_midtraining_mix_0625_100B`；参见 [L80](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/scripts/official/OLMo3/OLMo-3-1025-7B-midtrain.py#L80) 与 [L97-L107](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/scripts/official/OLMo3/OLMo-3-1025-7B-midtrain.py#L97-L107)：`LinearWithWarmup(warmup=0, alpha_f=0.0)` 从当前 LR 直线衰到零；`load_trainer_state=False`（新阶段从步数 0 重新计）但 `load_optim_state=True`（Adam 动量要延续）。

**anneal：OLMo-2 的同款阶段，两个新技巧**。

- **从 checkpoint 推断起点 LR**：参见 [src/scripts/official/OLMo2/OLMo-2-0325-32B-anneal.py:L83-L86](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/scripts/official/OLMo2/OLMo-2-0325-32B-anneal.py#L83-L86)：`starting_lr = get_lr_from_checkpoint(opts.checkpoint)`——这个函数（[src/olmo_core/script_utils.py:L172-L201](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/script_utils.py#L172-L201)）直接从分布式 checkpoint 的优化器状态里读出 `param_groups` 的 `lr`，兼容扁平化与非扁平化两种格式。OLMo-3 midtrain 里那个写死的小数，在这里变成了自动推断。
- **自定义 mix 与自定义 CLI**：参见 [src/scripts/official/OLMo2/OLMo-2-0325-32B-anneal.py:L50-L76](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/scripts/official/OLMo2/OLMo-2-0325-32B-anneal.py#L50-L76)：继承 `DataMixBase` 定义 `AnnealingDataMix`，从本地 txt 读配方；参见 [L256-L264](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/scripts/official/OLMo2/OLMo-2-0325-32B-anneal.py#L256-L264)：在 `get_cli_parser()` 基础上加必填参数 `--checkpoint`，再以 `main(build_config, parser=_get_parser())` 传入——统一 CLI 可扩展的官方示范。另见 [L251-L253](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/scripts/official/OLMo2/OLMo-2-0325-32B-anneal.py#L251-L253)：`merge(overrides)` 之后把 `dataset.mix` 重新包回 `AnnealingDataMix`——dotlist 覆盖走字典世界往返，枚举身份需要手工恢复，这是自定义 mix 类要付的小代价（u4-l3 讲过 mix 的 `build()` 协议）。

**long-context：并行与精度的全面换挡**。参见 [src/scripts/official/OLMo3/OLMo-3-1025-7B-long-context.py:L37-L39](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/scripts/official/OLMo3/OLMo-3-1025-7B-long-context.py#L37-L39)：序列长度 65536。四处关键改动：

- 参见 [L46-L51](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/scripts/official/OLMo3/OLMo-3-1025-7B-long-context.py#L46-L51)：`with_rope_scaling(YaRNRoPEScalingConfig(factor=8, ..., old_context_len=8192))`——u3-l4 的 YaRN 频率表改写在此启用（方法定义见 [src/olmo_core/nn/transformer/config.py:L2015](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/config.py#L2015)）。
- 参见 [L53-L62](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/scripts/official/OLMo3/OLMo-3-1025-7B-long-context.py#L53-L62)：数据集从 FSL 换成 `NumpyPackedFSLDatasetConfig`（装箱、零 padding），`generate_doc_lengths=True` 开启文档内掩码——长序列训练不希望文档互相注意。
- 参见 [L70-L94](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/scripts/official/OLMo3/OLMo-3-1025-7B-long-context.py#L70-L94)：`rank_microbatch_size=sequence_length`（每 rank 每步恰好 1 个实例——CP 按序列切分的前提，注释写明）；`cp_config=TransformerContextParallelConfig.llama3(degree=8, head_stride=4)`（u7-l2 的 llama3 负载均衡上下文并行，工厂方法见 [src/olmo_core/train/train_module/transformer/config.py:L307](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/config.py#L307)）；`ac_config=None`（长序列下重计算代价太高）；`float8_config` **打开**（`AOFloat8LinearConfig.recommended()`，u10-l1 的主题在此预演）——64K 序列的激活内存压力迫使三管齐下。
- 参见 [L96-L109](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/scripts/official/OLMo3/OLMo-3-1025-7B-long-context.py#L96-L109)：`load_path` 指向 stage2 的 step47684，`load_trainer_state=False`。

#### 4.4.4 代码实践（手算型）

**目标**：验证 pretrain-2 的「中点对齐」设计。

**步骤**：

1. 用 Python 或纸笔算：`int(5e12) // (8192 * 512)`、再 `// 2`，加上 `2000 // 2`。
2. 与 pretrain-1 的 `hard_stop=Duration.steps(int(597046))` 比较。
3. 代入 HalfCos 公式手算起点 LR：\( lr_0 = 3\mathrm{e}{-4} \)、\( \alpha_f = 0.1 \)（默认），求 \( \eta_{min} + (lr_0-\eta_{min})/2 \)。

**需要观察的现象**：三个数字严丝合缝——warmup 步数 = 第一段刹车步数；起点 LR ≈ 1.65e-4（3e-4 与 3e-5 的中点）。

**预期结果**：`1,192,092 // 2 + 1,000 = 597,046`；中点 LR = \( (3\mathrm{e}{-4} + 3\mathrm{e}{-5})/2 = 1.65\mathrm{e}{-4} \)。若与 midtrain 脚本写死的 `0.000207...` 对比会发现同量级——那个数就是上一段停下时的实际 LR。

#### 4.4.5 小练习与答案

**练习 1**：为什么 midtrain 用 `load_trainer_state=False` 而 pretrain-2 用 `True`？
**答案**：midtrain 是新阶段（新数据、新调度曲线、新时长），步数从 0 重计，只要权重和优化器动量（`load_optim_state=True`）；pretrain-2 是**同一阶段的延长**，余弦坐标必须衔接——`HalfCosWithWarmup` 的 warmup 参数编码了「前半程占了多少步」，只有步数计数延续，坐标才落在中点。

**练习 2**：long-context 阶段为什么要同时做「装箱 + 文档掩码 + CP + float8 + 关激活检查点」五件事？
**答案**：都是 64K 序列带来的资源压力的应对——装箱消除 padding 浪费、文档掩码保证语义正确性（不跨文档注意）、CP 把序列切到 8 个 rank 分摊激活、float8 压缩线性层精度开销、关激活检查点是因为重计算 64K 序列的前向太贵（改用 CP+float8 省显存）。

**练习 3**：anneal 脚本为什么把 `AnnealingDataMix` 的恢复放在 `merge()` 之后（L251-L253）而不是之前？
**答案**：`merge()` 走「降级为 dict → 下钻改值 → `from_dict` 重建」路线（u2-l3），重建时 mix 字段会被解析为注册过的类型（字符串或标准 DataMix 枚举），自定义 `DataMixBase` 子类的身份会丢；放在之后手工重包，保证命令行覆盖能力与自定义类型共存。

### 4.5 dotlist 覆盖实战：安全地复现与定制官方配置

#### 4.5.1 概念说明

「复现」不等于「原样重跑」。512 张 H100 的配方搬到 8 张卡，必须改批大小、并行度、保存频率；审阅配方时也想快速问「如果 warmup 翻倍会怎样」。dotlist 覆盖让这些改动**不碰脚本一行代码**——命令行即补丁，且补丁会被 `ConfigSaverCallback` 连同基线一起固化进 checkpoint（u2-l3 的可复现闭环）。

但「可覆盖」不等于「可乱覆盖」。安全等级分三档：

| 档位 | 例子 | 理由 |
|---|---|---|
| **安全**（只影响节奏/资源） | `--data_loader.num_workers`、`--trainer.metrics_collect_interval`、`--trainer.cancel_check_interval`、`--train_module.rank_microbatch_size`、`--train_module.optim.scheduler.warmup` | 纯 I/O / 日志 / 显存分桶参数，不改变训练语义 |
| **慎重**（改变训练但对小实验无碍） | `--trainer.max_duration`（换 LR 曲线 horizon）、`--train_module.optim.lr` | 会改变学习率轨迹，需理解调度器语义（u5-l2） |
| **危险**（结构性/一致性约束） | `--model.d_model`、`--train_module.dp_config.num_replicas`、`--data_loader.global_batch_size`（隐式改步数 horizon） | 破坏预置架构的内在约束、与 world size 不匹配、或让 token/step 换算失效 |

#### 4.5.2 核心流程

一次安全的配置修改闭环：

```text
读脚本定基线 → 选定安全字段 → 带 --dry-run 覆盖运行 → 在打印的配置树中核对
→ （可选）正式跑 → 事后从 checkpoint 的 config.json 审计实际生效值
```

#### 4.5.3 源码精读

覆盖的解析入口在 `_parse_args`（未知参数收进 `overrides`，[src/olmo_core/script_utils.py:L94-L104](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/script_utils.py#L94-L104)），应用入口在每个脚本的 `ExperimentConfig(...).merge(overrides)`（如 [src/scripts/official/OLMo3/OLMo-3-1025-7B-pretrain-1.py:L186-L192](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/scripts/official/OLMo3/OLMo-3-1025-7B-pretrain-1.py#L186-L192)）。值按 YAML 规则解析：`2` 是 int、`8192` 是 int、`true` 是 bool（u2-l3 的 `_clean_opt`）。

一个跨字段联动的例子值得记住：`rank_microbatch_size` 看似只是 micro-batch，但它决定「一个全局步在每 rank 上切多少个 micro-batch」（u6-l2 的 token 预算切分）——改小它增加梯度累积次数、减显存、略降吞吐，但**全局步的语义（4M token/步）不变**，所以 LR 曲线不动。这就是它「安全」的原因。

#### 4.5.4 代码实践（可运行，本讲核心实践）

**目标**：对 OLMo-3 7B pretrain-1 施加 5 个安全覆盖，并用 dry-run 验证生效。

**步骤**：

```bash
python src/scripts/official/OLMo3/OLMo-3-1025-7B-pretrain-1.py \
  --save-folder=/tmp/olmo3-7b-dryrun \
  --data_loader.num_workers=2 \
  --trainer.metrics_collect_interval=50 \
  --trainer.cancel_check_interval=100 \
  --train_module.rank_microbatch_size=8192 \
  --train_module.optim.scheduler.warmup=1000 \
  --dry-run
```

**需要观察的现象**：在打印出的配置树中逐项核对：

1. `data_loader` 块的 `num_workers` 应为 2（基线 8）；
2. `trainer` 块的 `metrics_collect_interval` 应为 50（基线 10）、`cancel_check_interval` 应为 100（基线 10）；
3. `train_module` 块的 `rank_microbatch_size` 应为 8192（基线 16384）；
4. `train_module.optim.scheduler` 的 `warmup` 应为 1000（基线 `warmup_steps=2000`——注意脚本用的是已弃用的 `warmup_steps` 字段名，覆盖时要用新名 `warmup`，见 [scheduler.py:L462-L464](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/optim/scheduler.py#L462-L464) 的弃用警告）。

**预期结果**：五处值全部按覆盖值显示；不覆盖的字段保持脚本基线。（rich 树的具体排版待本地验证。）

**延伸（可选）**：故意打错一个字段名（如 `--train_module.optims.lr=1`）再跑 dry-run，观察报错方式——merge 对未知路径的失败模式能帮你理解 `_set_nested` 的下钻逻辑。

#### 4.5.5 小练习与答案

**练习 1**：想临时开 WandB 记录，为什么用 `--name=my-run --trainer.callbacks.wandb.enabled=true` 可能不是最稳妥的方式？
**答案**：`callbacks` 是名字到回调对象的字典，字典值经 dotlist 覆盖要走「降级 dict → 重建」路线，回调对象（部分非 Config dataclass）的往返序列化比普通字段脆弱。更稳妥的是复制脚本改一行 `enabled=False → True`（脚本本来就是配方载体），或先 dry-run 验证覆盖后的配置树完整无误。以实际 dry-run 输出为准（**待本地验证**）。

**练习 2**：把 `--data_loader.global_batch_size=1048576`（1M token/步）加上去会发生什么语义变化？
**答案**：全局步的 token 含义变了——同样 5T token 对应的步数变为 4 倍，`CosWithWarmup` 的 warmup_steps=2000 相对全新 horizon 的占比骤变，等效于完全不同的 LR 曲线。这属于「危险」档：数值合法但训练语义大变（u5-l2 的 horizon 结论）。

**练习 3**：为什么说「命令行覆盖 + ConfigSaverCallback」构成了审计闭环？
**答案**：实际生效的配置 = Python 基线 + 命令行补丁，`main()` 把合并后的 `config.as_config_dict()` 注入 `ConfigSaverCallback`（[script_utils.py:L151-L155](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/script_utils.py#L151-L155)），随每个 checkpoint 落盘。事后无需翻 shell 历史，直接读 checkpoint 里的配置即可知道当时每一个字段的值。

## 5. 综合实践

**任务**：为 OLMo-3 7B pretrain-1 建立一份「配置档案」，并完成一次带覆盖的 dry-run 演练。

**步骤**：

1. **画配置树**：读 [src/scripts/official/OLMo3/OLMo-3-1025-7B-pretrain-1.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/scripts/official/OLMo3/OLMo-3-1025-7B-pretrain-1.py)，手绘五个分支的树（model / dataset / data_loader / train_module / trainer），train_module 下钻到 `optim.lr`、`optim.scheduler.warmup_steps`、`dp_config.name`、`dp_config.param_dtype`、`float8_config.enabled` 五个叶子，trainer 列出全部回调名。参考骨架：

   ```text
   ExperimentConfig
   ├─ model            = olmo3_7B, vocab=padding(128), attn_backend=flash_2
   ├─ dataset          = NumpyFSLDatasetConfig ← DataMix.OLMo_mix_0625_official
   ├─ data_loader      = NumpyDataLoaderConfig(gbs=8192×512, seed=34521, workers=8)
   ├─ train_module     = TTMC(mbs=2×8192, optim=SkipStepAdamW(3e-4, wd=0.1,
   │                     betas=(0.9,0.95), emb wd=0), sched=CosWithWarmup(2000),
   │                     dp=hsdp/bf16/fp32, compile=True, z_loss=1e-5, clip=1.0)
   └─ trainer          = 5T tokens, hard_stop=597046 步, callbacks=[monkey_patcher,
                         checkpointer(1000), comet(off), wandb(off), config_saver,
                         lm_evaluator(10K), downstream_evaluator(27 tasks, 10K)]
   ```

2. **选定 5 个安全覆盖字段**：从 4.5.1 的「安全」档任选五个（建议覆盖 4.5.4 那组，便于对照答案）。
3. **执行 dry-run**：跑 4.5.4 的命令，在输出树中逐项勾对你的覆盖值。
4. **对照阶段衔接**：再对 pretrain-2 跑一次 dry-run，找出与 pretrain-1 不同的字段（至少应看到 scheduler 类型、load_path、max_duration、hard_stop 四处）。
5. **（可选，需 GPU 与数据）**：用 `--train-single` 加极小的 `--trainer.max_duration` 跑通一次真实 step——没有条件就跳过，前四步已覆盖本讲全部目标。

**验收标准**：配置树的每个叶子都能在 dry-run 输出中找到对应节点；五个覆盖值全部生效；能说出 pretrain-1 → pretrain-2 的四处配置差异及其原因。

## 6. 本讲小结

- 官方脚本 = 「`build_config(opts, overrides)` + `main()`」骨架的配方文件：Python 表达基线（可用常量、分支、world size），出口收敛为可序列化的 `ExperimentConfig`，命令行 dotlist 做微调。
- 统一 CLI 只有 7 个正式参数（`--save-folder` 唯一必填），其余一切 `--a.b.c=v` 经 `parse_known_args` 分流进 `merge()`；`--dry-run` 在 build 之前返回，零 GPU、零数据即可展开完整配方。
- `main()` 流水线：环境准备 → `seed_all` → 按依赖序 build 五块 → 配置注入 `ConfigSaverCallback` → 双路径恢复（save_folder 续训 / load_path 初始化）→ `fit()` → 清理。
- 阶段化训练的三件套：`load_path` 衔接权重、调度器按新 horizon 重设（Cos → HalfCos「中点对齐」→ Linear 衰到零）、`DataMix` 换数据；long-context 阶段叠加 YaRN、装箱、CP、float8 的全面换挡。
- 覆盖分安全/慎重/危险三档；安全档（num_workers、指标频率、micro-batch、warmup）不改变训练语义；实际生效配置随 checkpoint 落盘形成审计闭环。

## 7. 下一步学习建议

- **u9-l2（内部训练脚本模板）**：官方脚本之外的第二套脚本模式——`prepare_cli_environment()` 与 `launch` / `train` / `train_single` / `dry_run` 子命令，从 `template.py` 定制自己的脚本。
- **u9-l3（Beaker 启动与 Docker 镜像）**：本讲的 torchrun 之外的另一半——`BeakerLaunchConfig` 如何把一份官方脚本送上集群（镜像、clone、pip install、torchrun 包装）。
- **源码延伸阅读**：`src/scripts/official/OLMo3/OLMo-3-1025-32B-*` 四份脚本（32B 用了 midtrain 双 ingredient + souping，与 7B 对照读）；`src/scripts/official/OLMo-hybrid/` 系列（线性混合架构变体，能看到 SFT 阶段脚本的样子）。
