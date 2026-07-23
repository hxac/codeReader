# u3-l1 CLI 入口与命令解析

## 1. 本讲目标

本讲深入 SpecForge 的唯一命令入口 `specforge/cli.py`。学完后你应该能够：

- 说清 `main()` 如何用 `argparse` 把 `specforge train/export/benchmark` 三个子命令分发到不同执行路径；
- 解释 `--role`（`auto/all/producer/consumer/both`）五个取值的含义，以及它如何决定一个 worker 进程是「只捕获特征」还是「初始化分布式并训练」；
- 理解 `_train` 内部 producer 分支与 trainer 分支的差异，以及 `_config_for_role` 为何要对配置做「角色投影」；
- 掌握 `_bootstrap_single_process_env` 如何为单卡直跑补齐分布式 rendezvous 环境变量，以及 `_worker_signal_unwind` 如何把信号翻译成正常的 Python 栈展开，让训练的 `finally` 清理块能正常执行。

本讲只关注**命令解析与进程生命周期**，不展开模型装配（那是 u6 训练主链路）与拓扑推导细节（那是 u3-l2 launch_plan）。本讲里频繁出现的「组合根 `build_application_run`」「算法注册」只是被调用方，我们把它当作黑盒，后续讲义再拆。

## 2. 前置知识

阅读本讲前，请确认你已经理解以下概念（均在前置讲义建立）：

- **单一类型化入口**：全项目只有一个命令 `specforge`，所有草稿方法（eagle3/dflash/domino/dspark/peagle）共用 `specforge train --config`，靠 `training.strategy` 区分，没有方法专属 Python 入口（u1-l5、u2-l1）。
- **七段配置**：一份 run config 由 model/data/training/tracking/profiling/runtime/deployment 七段加 `run_id`/`output_dir` 组成，未知字段直接报错（u2-l2）。
- **两条正交轴线**：数据模式 online/offline（由 `data.hidden_states_path` 是否为空决定，是 `Config.mode` 推导属性）与部署模式 `deployment.mode`（`local_colocated`/`disaggregated`）。online 强制 disaggregated + `target_backend=sglang`（u2-l1）。
- **主轴调用链**：`cli.py → config/schema.py → composition.py → launch.py → training/assembly.py → trainer.py`，旁挂 `runtime/` 与 `inference/`（u1-l5）。

几个本讲会反复用到的术语：

- **worker**：真正执行训练（或特征捕获）的进程。当 `plan.kind == "worker"` 时，cli 在**当前进程内**直接跑 `_train`，不再 spawn 子进程。
- **supervisor**：负责拉起其他进程的「父进程」。当 plan 是 `command`/`supervisor`/`managed_supervisor` 时，cli 走 `run_commands(plan)`，自己变成 supervisor。
- **rendezvous**：PyTorch 分布式启动时各 rank 互相发现的约定，靠 `RANK/WORLD_SIZE/LOCAL_RANK/MASTER_ADDR/MASTER_PORT` 五个环境变量完成（即 `env://` 初始化策略）。

## 3. 本讲源码地图

本讲几乎全部围绕一个文件，辅以两个被它调用的模块：

| 文件 | 作用 |
| --- | --- |
| [specforge/cli.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py) | **唯一核心**。命令解析、子命令分发、worker 进程生命周期（信号、单进程引导、`_train`、角色投影）。 |
| [specforge/launch_plan.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch_plan.py) | `build_launch_plan` 推导进程拓扑、`run_commands` 执行 supervisor 计划。cli 只消费它的 `LaunchPlan` 与 `plan.kind`。 |
| [specforge/application/composition.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/application/composition.py) | 组合根：`resolve_run`/`bind_run`/`build_application_run`。cli 把「已解析的配置」交给它，它产出可执行的 run。 |
| [specforge/config/schema.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py) | `Config` 类型化配置、`training.role` 字段、`validate_world_size`、`mode` 属性。 |

> 提醒：cli.py 的模块文档字符串（[specforge/cli.py:9-22](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py#L9-L22)）明确写道——本模块**故意只负责命令解析与分布式进程生命周期**，模型/数据装配在 `training.assembly`。这是本讲的精神纲领。

## 4. 核心概念与源码讲解

### 4.1 argparse 子命令与命令分发

#### 4.1.1 概念说明

`specforge` 是一个「一个可执行程序、三个子命令」的 CLI。`argparse` 的 **subparser**（子解析器）机制恰好适合这种结构：顶层的 `specforge` 只负责识别第一个位置参数是 `train`/`export`/`benchmark`，然后把剩下的参数交给对应子解析器。每个子命令有自己的参数集合，互不干扰。

这种设计的好处是：三种完全不同的功能（训练 / 导出 / 基准）共用一个安装入口（`[project.scripts]` 里注册的 `specforge = specforge.cli:main`，见 u1-l2），却不会让 `--help` 被无关参数淹没。

#### 4.1.2 核心流程

`main()` 的命令分发可以概括为：

```text
main(argv)
 ├─ 构建 parser + 三个 subparser(train / export / benchmark)
 ├─ parser.parse_args(argv)  →  args
 └─ 按 args.command 分发：
     ├─ "train"     → 加载配置 → 解析算法 → 构建计划 → 执行（见 4.1.3）
     ├─ "benchmark" → benchmarks.sglang.run(args)
     └─ (export)    → 按 args.to 选 to_hf / to_sglang
```

注意三个子命令的「分支方式」并不完全一样：

- `train` 与 `benchmark` 用 `if args.command == ...` 判断（[specforge/cli.py:241](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py#L241)、[specforge/cli.py:268](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py#L268)）。
- `export` 没有显式的 `if args.command == "export"`，而是落到函数末尾的 `if args.to == "hf" ... else ...`（[specforge/cli.py:272-294](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py#L272-L294)）。因为 `export` 的 `--to` 是 `required=True` 且只允许 `hf`/`sglang`，所以走到这里时 `args.to` 一定有值，等价于「剩下的就是 export」。

#### 4.1.3 源码精读

**① 顶层 parser 与三个子命令的注册**（[specforge/cli.py:170-198](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py#L170-L198)）：

```python
parser = argparse.ArgumentParser(prog="specforge")
sub = parser.add_subparsers(dest="command", required=True)
train = sub.add_parser("train", help="train a draft model from a typed config")
train.add_argument("-c", "--config", required=True, ...)
train.add_argument("--role",
    choices=("auto", "all", "producer", "consumer", "both"),
    default="auto", ...)
train.add_argument("--node-rank", type=int, default=None, ...)
train.add_argument("--plan", action="store_true", ...)
train.add_argument("overrides", nargs="*", ...)
```

要点：

- `dest="command", required=True` 表示必须给出一个子命令，否则直接报错——不存在「不带子命令的 specforge」。
- `--role` 的五个取值是理解整个启动行为的关键，下一节会展开。默认值 `auto` 意味着「我不指定，让计划推导」。
- `overrides` 用 `nargs="*"` 收集尾部所有 `section.field=value` 形式的位置参数，这就是 u2-l3 讲过的 dotted overrides。

**② `train` 分支的核心七步**（[specforge/cli.py:241-267](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py#L241-L267)）：

```python
if args.command == "train":
    cfg = load_config(args.config, args.overrides)          # ① 加载并校验配置
    from specforge.application import bind_run, resolve_run
    from specforge.launch_plan import build_launch_plan, run_commands

    resolved = resolve_run(cfg)                              # ② 解析算法 → ResolvedRun
    plan = build_launch_plan(                                # ③ 构建进程计划
        resolved.config, algorithm=resolved.algorithm,
        config_path=args.config, overrides=args.overrides,
        requested_role=args.role, node_rank=args.node_rank,
    )
    if args.plan:                                            # ④ --plan 预览后直接返回
        print(plan.render())
        return 0
    if plan.kind == "worker":                                # ⑤ worker：进程内执行
        os.environ.update(plan.worker_env)
        role_config = _config_for_role(resolved.config, plan.role)
        try:
            with _worker_signal_unwind():
                _train(bind_run(role_config, resolved.algorithm))
        except _WorkerTermination as received:
            return 128 + received.signum
        return 0
    return run_commands(plan)                                # ⑥ 否则：supervisor 拉子进程
```

这七步是本讲的「主旋律」，请重点记住三个分叉点：

| 分叉点 | 判断条件 | 走向 |
| --- | --- | --- |
| 配置加载 | `load_config` | 产出强类型 `Config`（未知字段 fail-fast） |
| 计划类型 | `plan.kind == "worker"` | 进程内跑 `_train`；否则 `run_commands` 当 supervisor |
| 预览 | `args.plan` | 打印 `plan.render()` 后 `return 0`，**不起进程、不占 GPU** |

**③ `--role` 五个取值的真实含义。** `choices` 来自 [specforge/cli.py:176](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py#L176)，但它们的真实语义要结合 `launch_plan._resolve_role`（[specforge/launch_plan.py:187-213](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch_plan.py#L187-L213)）才能讲清：

| 取值 | 适用部署模式 | 语义 |
| --- | --- | --- |
| `auto`（默认） | 两者皆可 | 离线 colocated → 推导为 `all`；disaggregated → 推导为 `both`（若有 `resume_from` 则推 `consumer`） |
| `all` | 仅 `local_colocated` | 单进程既装配模型又训练（典型离线训练） |
| `producer` | 仅 `disaggregated` | 特征捕获侧：发请求给 SGLang、发布特征引用，**不初始化 CUDA、不算 trainer** |
| `consumer` | 仅 `disaggregated` | 训练侧：初始化分布式、跑 trainer，消费 producer 产出的特征 |
| `both` | 仅 `disaggregated` | supervisor：一条命令拉起 producer + consumer 两个子进程 |

`_resolve_role` 还会拒绝非法组合，例如 disaggregated 不允许 `all`（[specforge/launch_plan.py:200-201](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch_plan.py#L200-L201)）、非 disaggregated 不允许 producer/consumer/both（[specforge/launch_plan.py:202-203](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch_plan.py#L202-L203)）。这些是 u3-l2 的重点，本讲只需知道「cli 把 `args.role` 原样传给 `build_launch_plan`，由它做最终裁决」。

#### 4.1.4 代码实践

**实践目标**：用 `--help` 与 `--plan` 两个零开销命令，亲眼确认三个子命令的存在与 `--role` 的取值集合。

**操作步骤**：

1. 确认已按 u1-l2 安装好 `specforge`（能 `specforge --help` 出现即可）。
2. 运行：
   ```bash
   specforge train --help
   ```
3. 运行（不占 GPU，仅解析配置与计划）：
   ```bash
   specforge train --plan -c examples/configs/qwen2.5-7b-eagle3-offline.yaml
   ```

**需要观察的现象**：

- 步骤 2 的输出里应出现 `--role` 一行，列出 `{auto,all,producer,consumer,both}`，默认 `auto`；还有 `--plan`、`--node-rank`、`overrides`。
- 步骤 3 会打印一段 JSON（`plan.render()`），其中 `kind` 与 `role` 两个字段是重点。

**预期结果**：`qwen2.5-7b-eagle3-offline.yaml` 是 `deployment.mode: local_colocated` 且 `data.hidden_states_path` 非空（离线），`nnodes=1, nproc_per_node=1`，所以 `plan.kind` 应为 `"worker"`、`role` 应为 `"all"`。若你的环境未装好或路径不同，结果以本地为准——**待本地验证**。

> 小贴士：`--plan` 复用的是真实训练前三步（`load_config` → `resolve_run` → `build_launch_plan`），所以它的输出就是「真实训练会怎么起进程」的忠实预览，详见 u2-l3。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `export` 子命令没有写 `if args.command == "export"`，却能正确分发到 `to_hf`/`to_sglang`？

> **答案**：因为 `export` 的 `--to` 参数 `required=True` 且 `choices=("hf","sglang")`（[specforge/cli.py:202](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py#L202)）。前面两个 `if` 已挡住 `train`/`benchmark`，走到函数尾部时 `args.command` 必为 `export`，`args.to` 必为 `hf` 或 `sglang`，因此用 `args.to` 直接分支即可。

**练习 2**：如果用户运行 `specforge` 不带任何子命令，会发生什么？

> **答案**：`add_subparsers(dest="command", required=True)`（[specforge/cli.py:171](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py#L171)）会让 `argparse` 直接报错退出，提示缺少子命令。这正是「单一入口、必须显式选功能」的体现。

---

### 4.2 `_train`：worker 进程的训练执行

#### 4.2.1 概念说明

当 `plan.kind == "worker"` 时，cli 不再 spawn 任何子进程，而是在**当前进程内**调用 `_train(bind_run(role_config, resolved.algorithm))`。`_train` 是「一个 worker 进程从拿到配置到跑完训练」的全部逻辑，它的核心特征是：**根据 role 分成两条截然不同的路径**——producer 路径几乎什么都不初始化，trainer 路径则要初始化整套 PyTorch 分布式。

为什么要分两条路径？因为 disaggregated 训练里，producer 只负责「把输入发给 SGLang、把产出的特征引用发布出去」，它既不持有草稿模型，也不参与梯度计算，所以**完全没有必要初始化 CUDA 和进程组**——初始化了反而是浪费甚至冲突。

#### 4.2.2 核心流程

`_train(resolved)` 的流程（[specforge/cli.py:113-146](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py#L113-L146)）：

```text
_train(resolved):
  cfg = resolved.config
  设置 FSDP_SHARDING 环境变量 + set_seed(seed)
  ├─ if cfg.training.role == "producer":        # ① producer 快速通道
  │     return build_application_run(resolved).run()   # 不初始化 CUDA / 分布式
  │
  └─ else (all / consumer):                      # ② trainer 路径
        _bootstrap_single_process_env()          #    补齐单进程 rendezvous 环境变量
        _validate_world_size(cfg, WORLD_SIZE)    #    校验 world_size 整除约束
        init_distributed(tp/sp 参数)             #    初始化进程组 + 设备网格
        try:
            _validate_world_size(cfg, dist.get_world_size())  # 用真实 world_size 再校验一次
            return build_application_run(resolved).run()
        finally:
            destroy_distributed()                #    无论成功失败都拆进程组
```

注意两个细节：

- **FSDP_SHARDING 与 seed 在分支之前设置**（[specforge/cli.py:119-120](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py#L119-L120)）：让类型化 recipe 成为后端 FSDP 分管的「权威来源」，且两条路径都享受同样的随机种子。
- **`world_size` 被校验两次**：一次用环境变量里的值（[specforge/cli.py:131](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py#L131)），一次用 `dist.get_world_size()` 的真实值（[specforge/cli.py:141](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py#L141)）。这是因为 `init_distributed` 之后，真实的 world_size 才由进程组确认；早校验是为了 fail-fast，晚校验是为了兜底。

#### 4.2.3 源码精读

**producer 快速通道**（[specforge/cli.py:121-126](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py#L121-L126)）：

```python
if cfg.training.role == "producer":
    # A server-capture/offline-ingest producer owns no trainer process
    # group and must not initialize CUDA merely to publish feature refs.
    from specforge.application import build_application_run
    return build_application_run(resolved).run()
```

注释点明了设计意图：producer **不拥有 trainer 进程组**，**不能为了发布特征引用就去初始化 CUDA**。这是 disaggregated 训练里「producer 与 consumer 解耦」的关键边界。

**trainer 路径的分布式初始化**（[specforge/cli.py:128-146](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py#L128-L146)）：

```python
from specforge.distributed import destroy_distributed, init_distributed
_bootstrap_single_process_env()
_validate_world_size(cfg, int(os.environ["WORLD_SIZE"]))
init_distributed(
    timeout=cfg.training.dist_timeout,
    tp_size=cfg.training.tp_size,
    sp_ulysses_size=cfg.training.sp_ulysses_size,
    sp_ring_size=cfg.training.sp_ring_size,
)
try:
    import torch.distributed as dist
    _validate_world_size(cfg, dist.get_world_size())
    from specforge.application import build_application_run
    return build_application_run(resolved).run()
finally:
    destroy_distributed()
```

`init_distributed` 的三个并行参数（`tp_size`/`sp_ulysses_size`/`sp_ring_size`）决定了 device mesh 的划分，这是 u8 分布式讲义的主题；本讲只关注「cli 在这里把训练交给组合根，并用 `try/finally` 保证 `destroy_distributed` 一定被执行」。

**`validate_world_size` 校验什么**（[specforge/config/schema.py:863-878](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L863-L878)）：它要求 `world_size` 必须同时被 `tp_size` 和 `sp_ulysses_size*sp_ring_size` 整除。换句话说，cli 在启动训练前就把「world_size 与并行拓扑不匹配」这类错误挡住，而不是等到训练中途崩。

> 概念澄清：`build_application_run(resolved).run()` 是组合根（[specforge/application/composition.py:133-145](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/application/composition.py#L133-L145)），它内部转调 `training.assembly.build_training_run` 装配出真正的 `TrainingRun` 并执行。无论 producer 还是 trainer，最终都汇入同一个组合根——区别只在「到达组合根之前是否初始化了分布式」。

#### 4.2.4 代码实践

**实践目标**：在源码中追踪一次 `specforge train`（worker 路径）调用，标出「配置加载、plan 构建、worker/命令分支」三个关键点。

**操作步骤**：

1. 打开 [specforge/cli.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py)，定位到 `main`（L169）。
2. 从 L241 的 `if args.command == "train":` 开始，顺着执行顺序往下读，在纸上画出调用链：
   - 配置加载点：`load_config(args.config, args.overrides)`（L242）。
   - plan 构建点：`build_launch_plan(...)`（L247）。
   - 分支点：`if plan.kind == "worker":`（L258）→ `_train(...)`（L263）；否则 `run_commands(plan)`（L267）。
3. 再进入 `_train`（L113），标出 producer 分支（L121）与 trainer 分支（L128）的入口。

**需要观察的现象**：三个关键点都在 `train` 分支内、彼此顺序固定；`_train` 的两条路径都用同一个 `build_application_run(resolved).run()` 收尾。

**预期结果**：你应当得到一张形如 `main → load_config → resolve_run → build_launch_plan → (plan.kind? worker: _train → build_application_run.run() | else: run_commands)` 的链路图。

> 说明：这是**源码阅读型实践**，不需要 GPU。若想看到真实运行，可对离线 colocated 配置跑 `specforge train --plan`，观察它打印出的 `kind=worker`、`role=all`，与上面 4.2.2 的 producer/trainer 分支对应（`all` 走 trainer 路径）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 producer 路径**不**调用 `init_distributed`，却能正常 `build_application_run(resolved).run()`？

> **答案**：producer 的职责只是发布特征引用（把输入发给 SGLang、把元数据写出去），既不持有需要分片的草稿模型，也不参与 all-reduce 等集合通信，因此没有进程组也能工作。注释（[specforge/cli.py:122-123](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py#L122-L123)）明确说它「owns no trainer process group」。

**练习 2**：trainer 路径为什么要把 `destroy_distributed()` 放在 `finally` 里？

> **答案**：训练可能因异常、信号或正常结束而退出 `try` 块。无论哪种情况，都必须销毁进程组、释放 rendezvous 资源，否则会留下僵尸进程或占用端口，影响下一次启动（[specforge/cli.py:145-146](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py#L145-L146)）。

---

### 4.3 `_config_for_role`：角色投影与配置不变量

#### 4.3.1 概念说明

在 disaggregated 训练里，producer 和 consumer **共用同一份 YAML 配置文件**（这是 u2-l1 讲过的「一份 shared disaggregated config」）。但这两个角色的需求并不相同：consumer 需要完整的 trainer 状态，producer 则要**忽略** trainer 专属状态、且通常不需要 profiling。

如果直接修改原始 `Config`，会破坏「配置是单一事实来源」的不变量。`_config_for_role` 的做法是：**把已校验的 Config 序列化成字典，按角色改几个字段，再重新 `model_validate` 出一份新的 Config**。这样原始配置不变，每个角色拿到的是「投影后的副本」。

#### 4.3.2 核心流程

`_config_for_role(cfg, role)` 的步骤（[specforge/cli.py:149-166](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py#L149-L166)）：

```text
_config_for_role(cfg, role):
  raw = cfg.model_dump()                  # ① 配置 → dict（不改原对象）
  raw["training"]["role"] = role          # ② 写入目标角色
  if disaggregated.managed_local 非空:
      raw["deployment"]["disaggregated"]["managed_local"] = None  # ③ 角色子进程不应再 own 这层栈
  if role == "producer":
      raw["profiling"]["enabled"] = False # ④ producer 关掉 profiling
  return Config.model_validate(raw)       # ⑤ 重新校验，产出新 Config
```

#### 4.3.3 源码精读

```python
def _config_for_role(cfg: Config, role: str) -> Config:
    """Resolve a launch role without changing the persisted run config."""
    raw = cfg.model_dump()
    raw["training"]["role"] = role
    disaggregated = raw["deployment"].get("disaggregated")
    if disaggregated is not None and disaggregated.get("managed_local") is not None:
        # This field describes services owned by the parent supervisor.  A role
        # child consumes the already-derived environment and must not attempt to
        # validate or own that stack again.
        disaggregated["managed_local"] = None
    if role == "producer":
        raw["profiling"]["enabled"] = False
    return Config.model_validate(raw)
```

三个要点：

- **`model_dump()` + `model_validate`**：这是 Pydantic「深拷贝并改字段」的标准手法。重新走一遍校验意味着投影后的配置依然满足所有跨字段约束（例如 `role != "all"` 时必须 `deployment == "disaggregated"`，见 [specforge/config/schema.py:686-691](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L686-L691)）。
- **`managed_local` 置空**：`managed_local` 描述的是「父 supervisor 负责托管的本地服务（Mooncake + 捕获服务器）」。当 cli 已经把角色派生为子进程后，子进程**消费已经派生好的环境**即可，不能再试图去 own 或校验这层栈（否则会重复启动服务）。这正是 cli 把它清成 `None` 的原因。
- **producer 关 profiling**：profiling 是给 trainer 用的（捕获 trace 要在训练循环里插桩），producer 不训练，所以强行关掉，与 schema 里的校验一致（[specforge/config/schema.py:705-708](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L705-L708)）。

> 调用时机：`_config_for_role` 只在 `plan.kind == "worker"` 分支里被调用一次（[specforge/cli.py:260](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py#L260)），紧随其后用 `bind_run(role_config, resolved.algorithm)` 把投影后的配置与已解析的算法重新绑定（[specforge/cli.py:263](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py#L263)）。`bind_run` 会再做一次 `validate_resolved_run`（[specforge/application/composition.py:31-37](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/application/composition.py#L31-L37)），确保角色投影后的配置对该算法仍然合法。

#### 4.3.4 代码实践

**实践目标**：理解「角色投影不改原配置」这一不变量，并能预测投影后的字段值。

**操作步骤**：

1. 读 [specforge/cli.py:149-166](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py#L149-L166)，对照 [specforge/cli.py:258-263](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py#L258-L263) 的调用点。
2. 假设有一份 disaggregated + `managed_local` 非空的配置，原始 `training.role = "auto"`、`profiling.enabled = True`。回答：当 plan 把角色派生为 `producer` 后，`_config_for_role` 返回的新 Config 里，`training.role`、`profiling.enabled`、`deployment.disaggregated.managed_local` 分别是什么？

**需要观察的现象 / 预期结果**：

- `training.role` → `"producer"`（被显式写入）。
- `profiling.enabled` → `False`（producer 强制关闭）。
- `deployment.disaggregated.managed_local` → `None`（子进程不再 own 这层栈）。
- 原始 `cfg` 对象**未被修改**（这是不变量的关键）。

> 说明：这是**源码阅读型实践**，无需运行。若想验证「原配置不变」，可在测试里 `cfg.model_dump()` 前后做断言对比——SpecForge 的 `tests/` 下有针对 cli 的类似覆盖（见 u10-l3）。

#### 4.3.5 小练习与答案

**练习 1**：为什么不直接 `cfg.training.role = role`，而要走 `model_dump` + `model_validate`？

> **答案**：两个原因。其一，Pydantic v2 的模型默认不可随意赋值未校验字段，直接改可能绕过校验；重新 `model_validate` 能保证投影后的配置依然满足所有跨字段约束。其二，`_config_for_role` 的文档字符串明确说「without changing the persisted run config」（[specforge/cli.py:150-155](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py#L150-L155)），`model_dump` 产出的是新 dict，原始 `cfg` 保持不变，符合「配置是单一事实来源」的不变量。

**练习 2**：如果一份 disaggregated 配置**没有** `managed_local`，`_config_for_role` 对 consumer 角色会改哪些字段？

> **答案**：只会把 `training.role` 设为 `"consumer"`；`managed_local` 那段 `if` 不触发（因为是 `None`），`profiling.enabled` 也只在 `role == "producer"` 时才关，所以 consumer 的 profiling 保持原值。

---

### 4.4 单进程引导与 worker 信号优雅退出

本模块对应规格里的「`_bootstrap_single_process_env` 与信号处理」最小模块，是两个相对独立但都服务于「让 worker 进程在各种环境下都能干净地跑/退」的工具函数。

#### 4.4.1 概念说明

**（A）单进程引导。** PyTorch 分布式初始化（`init_distributed`）需要一个 rendezvous 信息：我是第几个 rank（`RANK`）、一共几个 rank（`WORLD_SIZE`）、我在本机的第几张卡（`LOCAL_RANK`）、大家去哪会合（`MASTER_ADDR`/`MASTER_PORT`）。当用户用 `torchrun` 拉起多进程时，torchrun 会自动注入这五个变量；但当用户**直接** `specforge train -c xxx.yaml`（单卡、没 torchrun）时，这五个变量可能不存在。`_bootstrap_single_process_env` 就是为这种「直跑」场景补齐一套合法的单进程 rendezvous 值，让后续的 `init_distributed` 不报错。

**（B）信号优雅退出。** 当一个 supervisor（由 `run_commands` 充当）要终止 worker 进程组时，会发 `SIGTERM`。Python 对 `SIGTERM` 的默认行为是**立即退出，跳过 `finally` 块**——这对训练是灾难性的：训练循环和分布式清理都写在 `try/finally` 里（如 checkpoint flush、Mooncake drain、`destroy_distributed`），如果被跳过，就会留下脏检查点或泄漏的资源。`_worker_signal_unwind` 的作用是：**把第一个终止信号翻译成一次普通的 Python 异常（`_WorkerTermination`）**，让栈正常展开、`finally` 正常执行；清理期间忽略后续信号，清理完再恢复原始处理器。

#### 4.4.2 核心流程

**`_bootstrap_single_process_env`**（[specforge/cli.py:81-106](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py#L81-L106)）：

```text
_bootstrap_single_process_env():
  required = (RANK, WORLD_SIZE, LOCAL_RANK, MASTER_ADDR, MASTER_PORT)
  present  = 已存在的那些
  if 有部分存在但不全:
      raise ValueError("distributed environment is incomplete ...")
  if 全都存在:
      return                              # ① 已有完整分布式环境（torchrun），直接用
  # ② 全都没有：构造一套单进程默认值
  port = 绑定 127.0.0.1:0 拿到一个空闲端口
  写入 RANK=0, WORLD_SIZE=1, LOCAL_RANK=0, MASTER_ADDR=127.0.0.1, MASTER_PORT=port
```

关键设计：「**要么全有、要么全无、绝不半套**」。如果只检测到部分变量（比如只有 `RANK` 没有 `MASTER_ADDR`），直接报错并提示用 torchrun 或清掉残留变量——避免用半套错误环境跑出诡异的分布式 bug。

**`_worker_signal_unwind`**（[specforge/cli.py:43-78](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py#L43-L78)）：

```text
_worker_signal_unwind():           # 一个 contextmanager
  managed_signals = [SIGINT, SIGTERM, (SIGHUP if 存在)]
  def unwind(signum, _frame):
      把已安装的信号全部改为 SIG_IGN            # 清理期间忽略后续信号
      raise _WorkerTermination(signum)        # 翻译成异常，触发正常栈展开
  安装 unwind 为这些信号的新 handler，保存旧 handler
  try:
      yield                                    # ← 训练在这里跑（_train 在 with 块内）
  finally:
      恢复所有旧 handler
```

调用侧（[specforge/cli.py:261-265](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py#L261-L265)）：

```python
with _worker_signal_unwind():
    _train(bind_run(role_config, resolved.algorithm))
```

`_WorkerTermination` 继承自 `BaseException` 而非 `Exception`（[specforge/cli.py:36-38](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py#L36-L38)），这样它不会被业务代码里宽泛的 `except Exception` 误吞，能一路展开到 `finally` 块和 cli 的捕获点。捕获后 cli 返回 `128 + signum`（Unix 惯例：被信号终止的进程退出码为 `128 + 信号编号`）。

#### 4.4.3 源码精读

**单进程引导的端口获取**（[specforge/cli.py:95-106](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py#L95-L106)）：

```python
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as rendezvous:
    rendezvous.bind(("127.0.0.1", 0))
    port = rendezvous.getsockname()[1]
os.environ.update({
    "RANK": "0", "WORLD_SIZE": "1", "LOCAL_RANK": "0",
    "MASTER_ADDR": "127.0.0.1", "MASTER_PORT": str(port),
})
```

`bind(("127.0.0.1", 0))` 让操作系统分配一个空闲端口，`with` 退出后立即关闭 socket——这里只是「借用」一个端口号写进环境变量，真正的监听由后续 `init_distributed` 完成。这是避免硬编码端口冲突的常见技巧。

**信号展开的「先忽略、再抛异常、后恢复」三段式**（[specforge/cli.py:59-78](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py#L59-L78)）：

```python
def unwind(signum, _frame):
    for installed in previous_handlers:
        signal.signal(installed, signal.SIG_IGN)   # 收到第一个信号后，先把自己改成 IGNORE
    raise _WorkerTermination(signum)                # 再抛异常
try:
    for signum in managed_signals:
        try:
            previous_handlers[signum] = signal.signal(signum, unwind)
        except ValueError:
            # Embedded callers may execute the CLI from a non-main thread ...
            ...                                     # 非主线程不能装信号 handler，优雅降级
            break
    yield
finally:
    for signum, handler in previous_handlers.items():
        signal.signal(signum, handler)              # 无论怎样都恢复原 handler
```

两个容易忽略的细节：

- **`except ValueError` 优雅降级**：Python 只允许在**主线程**注册信号处理器，嵌入式调用（如测试、notebook）可能从非主线程跑 cli，此时 `signal.signal` 抛 `ValueError`。代码捕获它、回滚已装的 handler、然后照常 `yield`——即「装不上就装不上，训练照跑」，不会硬崩。
- **supervisor 侧的对应物**：`run_commands` 里有几乎相同的「`_ForwardedSignal` + 先 IGNORE 后抛 + finally 恢复」机制（[specforge/launch_plan.py:1074-1081](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch_plan.py#L1074-L1081)），区别是 supervisor 把信号**转发给整个进程组**而非翻译成本进程的异常。两者配合，保证「supervisor 终止 → worker 优雅清理 → 子服务优雅清理」的全链路。

> 概念澄清：`shutdown_grace_s`（[specforge/launch_plan.py:151](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch_plan.py#L151)）是 supervisor 给 worker 的「清理宽限期」。`_worker_signal_unwind` 争取的就是这段时间——在 SIGKILL 到来之前跑完 checkpoint flush 与 Mooncake drain。若清理超时，supervisor 仍会用 SIGKILL 强制收尾。

#### 4.4.4 代码实践

**实践目标**：通过阅读源码与一个最小 Python 实验，理解「信号→异常→finally」的翻译机制。

**操作步骤**：

1. 读 [specforge/cli.py:43-78](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py#L43-L78)，确认 `unwind` 先 `SIG_IGN` 再 `raise`，`finally` 恢复原 handler。
2. 在一个**示例代码**（非项目原有代码）里复刻这个模式，验证 `finally` 会被执行：

   ```python
   # 示例代码：演示「信号翻译成异常 → finally 执行」的最小模型
   import signal, threading, os, time

   class TermSignal(BaseException):
       def __init__(self, signum): self.signum = signum

   def make_unwind():
       prev = {}
       def unwind(signum, _frame):
           for s in prev: signal.signal(s, signal.SIG_IGN)
           raise TermSignal(signum)
       return prev, unwind

   prev, unwind = make_unwind()
   prev[signal.SIGUSR1] = signal.signal(signal.SIGUSR1, unwind)
   try:
       threading.Timer(0.2, lambda: os.kill(os.getpid(), signal.SIGUSR1)).start()
       time.sleep(2)            # 模拟训练长任务
       print("正常结束（不应看到这行）")
   except TermSignal as e:
       print(f"收到信号 {e.signum}，翻译为异常")
   finally:
       for s, h in prev.items(): signal.signal(s, h)
       print("finally 执行：清理分布式资源")
   ```

3. 运行这段示例代码（`python demo.py`）。

**需要观察的现象**：约 0.2 秒后打印「收到信号 N，翻译为异常」，紧接着打印「finally 执行：清理分布式资源」。

**预期结果**：`finally` 块**一定会执行**——这正是 `_worker_signal_unwind` 相比「Python 默认 SIGTERM 立即退出」的核心价值。注意这是**示例代码**，仅用于演示原理，不是 SpecForge 的一部分。

> 说明：你无法在不启动真实训练的情况下直接触发 SpecForge 的 `_worker_signal_unwind`（它只在 `with _worker_signal_unwind(): _train(...)` 内生效）。上面的示例代码用 `SIGUSR1` + 定时器隔离出同样的机制，便于观察。真实环境下「supervisor 发 SIGTERM → worker 清理」的行为，可结合 u7-l2 控制平面的恢复契约一起读。

#### 4.4.5 小练习与答案

**练习 1**：`_bootstrap_single_process_env` 为什么要区分「全都有 / 全都没有 / 半套」三种情况？

> **答案**：全都有 → 已在 torchrun 等分布式环境里，直接复用；全都没有 → 单卡直跑，补一套单进程默认值；半套 → 极可能是配置残留或错误注入，直接报错（[specforge/cli.py:85-92](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py#L85-L92)），避免用半套环境跑出难调试的分布式 bug。

**练习 2**：`_WorkerTermination` 为什么继承 `BaseException` 而不是 `Exception`？

> **答案**：业务代码（训练循环、策略、装配）里常有宽泛的 `except Exception` 兜底。如果信号翻译成普通 `Exception`，会被这些兜底吞掉，导致 `finally` 不按预期展开、退出码丢失。继承 `BaseException`（[specforge/cli.py:36-38](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py#L36-L38)）能绕过 `except Exception`，确保它一路展开到 cli 的捕获点（[specforge/cli.py:264-265](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py#L264-L265)）并返回 `128 + signum`。

---

## 5. 综合实践

**任务**：把本讲的三个分叉点串起来，徒手画出一次 `specforge train`（离线 colocated、单卡）从命令行到训练执行的完整调用链，并标注每一步「属于哪个最小模块」。

**要求**：

1. 从 `specforge train -c examples/configs/qwen2.5-7b-eagle3-offline.yaml` 这条命令出发。
2. 在链路图上至少标出以下 7 个节点，并为每个节点注明它属于本讲的哪个模块（4.1 / 4.2 / 4.3 / 4.4）：
   - `argparse` 解析出 `command="train"`、`role="auto"`（4.1）；
   - `load_config` 加载并校验 YAML（4.1）；
   - `resolve_run` + `build_launch_plan` 得到 `plan`（4.1）；
   - `plan.kind == "worker"` 判定为真，走进程内路径（4.1）；
   - `_config_for_role` 投影出 role=all 的配置副本（4.3）；
   - `_bootstrap_single_process_env` 补齐单进程 rendezvous（4.4）；
   - `_train` 走 trainer 路径：`init_distributed` → `build_application_run(resolved).run()` → `finally: destroy_distributed()`（4.2）。
3. 用一句话回答：为什么这条命令没有触发 `_worker_signal_unwind` 的异常分支，但 `with _worker_signal_unwind():` 仍然是必要的？

**参考答案要点**（第 3 问）：因为用户没有发 SIGTERM/SIGINT，`unwind` 不会被触发，训练正常结束、`_train` 正常返回；但 `with` 块仍然必要——它**提前注册**了信号处理器，保证训练期间任何时候收到终止信号都能优雅清理，而不是被 Python 默认行为立即杀死、跳过 `destroy_distributed`。

> 进阶（可选）：把同一张图再画一次，但这次命令是 disaggregated 模式下被 supervisor 拉起的 **producer 子进程**（`--role producer`）。对比两张图，指出 producer 路径**跳过**了 `_bootstrap_single_process_env` 与 `init_distributed`（4.2 的 producer 快速通道），以及 `_config_for_role` 把 `profiling.enabled` 设为 `False`（4.3）。

## 6. 本讲小结

- `specforge` 是「一个程序、三个子命令」的 CLI：`main()` 用 `argparse` subparser 分发 `train`/`export`/`benchmark`，`export` 靠 `--to` 的 required + choices 落到函数尾部分支。
- `train` 分支有固定的七步：`load_config → resolve_run → build_launch_plan → (--plan?) → (plan.kind=="worker"?) → _train / run_commands`，三个分叉点是配置加载、plan 构建、worker/命令分支。
- `--role` 五个取值（`auto/all/producer/consumer/both`）的真实语义由 `launch_plan._resolve_role` 裁决：离线 colocated 用 `all`，disaggregated 用 producer/consumer/both，非法组合直接报错。
- `_train` 按 role 分两条路径：producer 不初始化 CUDA/分布式、直接进组合根；trainer（all/consumer）先 `_bootstrap_single_process_env` + `init_distributed`，用 `try/finally` 保证 `destroy_distributed` 必执行，并对 `world_size` 校验两次。
- `_config_for_role` 用 `model_dump` + `model_validate` 做「角色投影」，不改原始配置：写入 role、清空 `managed_local`、producer 关 profiling，保证「配置是单一事实来源」的不变量。
- `_bootstrap_single_process_env` 为单卡直跑补齐 rendezvous 五变量（要么全有、要么全无、半套报错）；`_worker_signal_unwind` 把终止信号翻译成 `BaseException`，让训练的 `finally` 清理块能正常执行，退出码遵循 `128 + signum`。

## 7. 下一步学习建议

本讲把「命令解析 → worker 执行」讲透了，但故意把两个东西当黑盒：

1. **`build_launch_plan` 如何推导出 `plan.kind` 和 `plan.role`**——这正是下一讲 **u3-l2 启动计划 launch_plan** 的主题。读完它你就能预测任意配置会产生 worker / command / supervisor / managed_supervisor 中的哪一种计划。
2. **四个 `build_*` 拓扑构建器如何汇聚到统一训练路径**——见 **u3-l3 拓扑构建器 launch** 与 **u3-l4 应用组合根 composition**，那里会拆开 `resolve_run`/`bind_run`/`build_application_run` 的内部。

此外建议：

- 想深入「分布式初始化与设备网格」的 `init_distributed` 细节，直接跳到 **u8-l1 分布式初始化与设备网格**。
- 想看 supervisor 侧如何终止 worker 进程组、`run_commands` 的信号转发与宽限期，重读 [specforge/launch_plan.py:1049-1159](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch_plan.py#L1049-L1159)，它与本讲的 `_worker_signal_unwind` 是一对「父端 / 子端」的信号协议。
