# 目录结构与源码地图

## 1. 本讲目标

前四讲我们建立了对 SpecForge 的认知与原理直觉：它是一个投机解码草稿模型训练框架，所有方法共用同一个类型化入口 `specforge train --config run.yaml`，靠配置而非不同的 Python 脚本来区分方法（见 u1-l1）；草稿模型吃目标模型的隐藏状态、输出 logits 供验证（见 u1-l4）。

但「认知」还不够，要真正读得懂源码、改得动代码，你需要一张**地图**——知道哪个目录管什么、一条训练请求从入口出发会依次穿过哪些子包。

本讲学完后，你应该能够：

1. 画出 `specforge` 源码包的顶层目录结构，并说出每个子包的职责。
2. 在源码中**快速定位**功能模块：谁负责命令解析、谁负责训练装配、谁负责在线引用分发。
3. 理解「单一类型化入口 → 组合根 → 各子包」这条分层主线，为后续进阶讲义（u3 入口链路、u6 训练主链路、u7 运行时）打好索引。

> 本讲是一张**索引讲义**：它的价值不在于讲透某个算法，而在于让你之后任何一次「我想看 X 功能的代码」都能在 30 秒内定位到正确的目录与文件。

## 2. 前置知识

本讲是纯「读目录、读模块说明」的地图课，不涉及算法细节。你只需要带上前四讲建立的几个直觉：

- **目标模型（target）与草稿模型（draft）**：目标模型当老师提供隐藏状态，草稿模型当学生学习输出（u1-l3、u1-l4）。
- **单一类型化入口**：所有草稿方法（EAGLE3 / P-EAGLE / EAGLE3.1 / DFlash / Domino / DSpark）都走 `specforge train --config`，没有方法专属的 Python 训练脚本（u1-l1）。
- **在线 / 离线两种数据模式**：在线训练时一边跑一边从 SGLang 捕获特征；离线训练时读取预先算好的特征文件（u1-l1）。
- **隐藏状态 / 特征（feature）**：草稿模型的输入，是目标模型内部某一层（或几层）的输出张量（u1-l4）。

两个读源码时会反复出现的工程概念，先在这里点一下：

- **类型化配置（typed config）**：用 Pydantic 模型（一个继承 `BaseModel` 的 Python 类）来描述一份 YAML/JSON 应该长什么样。读 YAML 时会按这个类做校验，字段写错就直接报错，而不是默默忽略。
- **组合根（composition root）**：整个项目里**唯一**一个把「配置、算法、训练器」拼装到一起的地方。理解了它，就理解了 SpecForge 的装配主轴。

## 3. 本讲源码地图

本讲主要「读」的是目录与模块说明（docstring），关键文件如下：

| 文件 | 作用 |
| --- | --- |
| `specforge/__init__.py` | 包入口，只声明「训练通过 `cli` 配置」，不向外 re-export 实现类型 |
| `specforge/cli.py` | **唯一的公开命令入口**：解析 `train`/`export`/`benchmark` 子命令并分发 |
| `specforge/config/schema.py` | 类型化运行配置 `Config` 与 `load_config`，七段配置的单一事实来源 |
| `specforge/application/__init__.py` + `composition.py` | **组合根**：把配置解析成算法注册、再装配成可执行 run |
| `specforge/training/assembly.py` | 训练装配：把草稿/目标/tokenizer/优化器/数据加载拼成 `TrainingRun` |
| `specforge/runtime/__init__.py` | DataFlow 运行时：声明控制面只传元数据、数据面才传张量这条铁律 |
| `specforge/runtime/data_plane/ref_distributor.py` | 在线引用分发：DP 在线 consumer 的集中式 dispatcher |
| `specforge/inference/__init__.py` | 推理/采集面：rollout worker、捕获适配器 |
| `docs/basic_usage/training.md` | 训练用户文档：单一入口、七段配置、支持组合表 |

## 4. 核心概念与源码讲解

### 4.1 CLI 单一入口：`specforge/cli.py`

#### 4.1.1 概念说明

很多机器学习项目为每种方法都提供一个独立脚本（`train_eagle3.py`、`train_dflash.py`……），方法越多脚本越乱。SpecForge 反其道而行：**全项目只有一个公开命令入口 `specforge`**，所有草稿方法、所有并行拓扑、所有数据模式（在线/离线/disaggregated）都从这一个入口进去，靠一份 YAML 配置区分。

`cli.py` 文件顶部的 docstring 把这一设计意图说得很直白：

[specforge/cli.py:9-22](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py#L9-L22) — 说明 `specforge train --config run.yaml` 是唯一公开入口，它会构建校验过的 `Config`、装配模型、再通过 DataFlow 启动器跑训练；并刻意声明本模块只管「命令解析 + 分布式进程生命周期」，**模型与数据的装配不在这里**，而在 `specforge.training.assembly`。

这条「入口只做解析、装配交给组合根」的边界，是后续所有子包分工的起点。

#### 4.1.2 核心流程

`specforge` 命令底下有三个子命令，职责互不重叠：

```
specforge
├── train      # 训练草稿模型（本框架的核心）
├── export     # 把运行时检查点物化成服务目录（hf / sglang）
└── benchmark  # 度量一个运行中的 SGLang 服务端吞吐 / 投机解码加速
```

其中 `train` 是最复杂的一条链。用伪代码描述它在 `cli.py` 内的分支（不含全部细节）：

```
main(argv):
    解析 argparse（train / export / benchmark）
    if command == "train":
        cfg      = load_config(config_path, overrides)   # 读 YAML + 点覆盖 + 类型校验
        resolved = resolve_run(cfg)                       # 在组合根里解析算法注册
        plan     = build_launch_plan(resolved, role, ...) # 推导进程拓扑计划
        if --plan:  print(plan.render()); 退出            # 只预览不启动
        if plan.kind == "worker":
            _train(bind_run(role_config, algorithm))     # 本进程就是 worker，直接训练
        else:
            run_commands(plan)                            # 本进程是 supervisor，派生子进程
    elif command == "benchmark": run(args)
    elif command == "export":    export_to_hf(...) 或 export_to_sglang(...)
```

注意三个分叉点：

1. **`--plan`**：只打印「解析后的进程计划」就退出，方便你检查拓扑而不真正起训练（u2-l3 会专门讲）。
2. **`plan.kind`**：决定当前进程是**亲自下场训练的 worker**，还是**派生子进程的 supervisor**（u3-l2 详解）。
3. **worker 路径**最终都汇入 `build_application_run(resolved).run()` ——也就是组合根。

#### 4.1.3 源码精读

`main()` 函数用 `argparse` 定义了三个子命令，其中 `train` 的 `-c/--config` 是必填项：

[specforge/cli.py:169-173](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py#L169-L173) — `main()` 创建 argparse、注册 `train` 子解析器，并要求 `-c/--config` 指向一份 YAML/JSON 运行配置。这是「单一类型化入口」在代码层面的落点。

命令分发后，`train` 分支是整段的核心。它做四件事：加载并校验配置、解析算法、构建启动计划、按 `plan.kind` 分叉：

[specforge/cli.py:241-267](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py#L241-L267) — `train` 命令分支：`load_config` 读配置 → `resolve_run` 解析算法 → `build_launch_plan` 推导进程拓扑 → 若 `--plan` 则渲染后退出；若 `plan.kind == "worker"` 则本进程直接 `_train(...)`，否则 `run_commands(plan)` 派生 supervisor 子进程。

worker 路径里的 `_train()` 负责「分布式环境引导 + 调用组合根」。注意它最终都收敛到同一个调用 `build_application_run(resolved).run()`：

[specforge/cli.py:113-126](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py#L113-L126) — `_train()` 设置随机种子，对 `producer` 角色直接走组合根（生产者不持有训练进程组）；否则初始化分布式、校验 world size，最终同样调用 `build_application_run(resolved).run()`。无论哪条路径，真正的训练都从组合根开始。

> 小结这一节你只需要记住一句话：**所有路径最终都汇入 `build_application_run(resolved).run()`**。下一节我们就看这个「组合根」以及它周围的子包分工。

#### 4.1.4 代码实践

**实践目标**：用 `--help` 确认 `specforge` 确实只有三个子命令，并验证「未知字段直接报错」这条 fail-fast 约定。

**操作步骤**：

1. 在装好 SpecForge 的环境里运行：

   ```bash
   specforge --help
   specforge train --help
   ```

2. 观察输出里是否有且仅有 `train` / `export` / `benchmark` 三个子命令。

3. （可选）从仓库根目录跑一次**只预览不启动**的计划，确认入口能解析配置：

   ```bash
   specforge train --plan --config examples/configs/qwen3-8b-eagle3-offline.yaml
   ```

**需要观察的现象**：

- `--help` 顶层只列出三个子命令，没有方法专属的 `train_eagle3` 之类。
- `train --help` 里能看到 `-c/--config`、`--role`、`--node-rank`、`--plan` 这些在 4.1.2 流程里出现的参数。

**预期结果**：三个子命令齐全；`--plan` 会打印一份进程计划（worker/supervisor、角色、命令）后以返回码 0 退出，不会真的起训练。

> 如果当前环境没有 GPU 或没装好依赖，`--help` 仍可运行（它只解析参数），但 `--plan` 可能需要加载配置与依赖。若报依赖缺失，标注「待本地验证」即可，**不要假装已经跑通**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 SpecForge 不为每种草稿方法单独写一个 `train_xxx.py`？
**参考答案**：因为所有方法共用同一个类型化入口 `specforge train --config`，靠 YAML 里的 `training.strategy`（如 `eagle3` / `dflash`）区分方法。这样命令面只有一个，方法之间的差异被收敛进配置与算法注册表，避免脚本泛滥。

**练习 2**：`plan.kind == "worker"` 与 否，`cli.py` 分别走哪两个分支？
**参考答案**：是 `worker` 时本进程直接调用 `_train(bind_run(...))` 下场训练；否则（supervisor）调用 `run_commands(plan)` 派生子进程。两条路径最终都进入组合根 `build_application_run(resolved).run()`。

### 4.2 子包分工：从配置到运行的分层

#### 4.2.1 概念说明

`cli.py` 把控制权交给组合根后，请求会穿过一连串子包。SpecForge 的源码大致按「**配置 → 解析算法 → 装配 → 运行时四条面**」分层，每个子包只负责一类职责。下面这张顶层目录树是你在本地 `ls specforge/` 能看到的真实结构：

```
specforge/
├── __init__.py            # 包入口，不 re-export 实现类型
├── cli.py                 # 单一命令入口（见 4.1）
├── config/                # 类型化运行配置 Config / load_config
├── algorithms/            # 算法契约 + 注册表 + 各算法 providers
├── application/           # 组合根：resolve_run / bind_run / build_application_run
├── launch.py              # 四个运行时拓扑构建器（offline / disagg_*）
├── launch_plan.py         # 进程拓扑计划推导
├── distributed.py         # init_distributed、TP/DP/SP group、shard/gather
├── training/              # 训练装配、Trainer、FSDP 后端、策略、检查点
├── runtime/               # DataFlow 运行时：控制面 + 数据面 + 跨面契约
├── inference/             # 推理/采集面：rollout worker、SGLang 捕获适配器
├── modeling/              # 草稿/目标模型结构 + auto 自动解析 + draft registry
├── data/                  # 模板渲染、预处理、loss mask、prompt 构建
├── core/                  # 损失与算子：LogSoftmaxLoss、LK loss、compact teacher
├── layers/                # 通用层：embedding / linear / lm_head / ring attention
├── eval/                  # 离线评测 evaluator
├── export/                # 导出 to_hf / to_sglang / checkpoint_io
├── benchmarks/            # 端到端基准（sglang 服务端吞吐）
├── offline_capture/       # 离线特征采集（局部 SGLang 内核）
├── optimizer.py           # 优化器工厂
├── lr_scheduler.py        # 学习率调度
├── tracker.py             # 实验跟踪（wandb/tensorboard/swanlab/mlflow）
└── utils.py               # 通用工具
```

其中四个**重型子包**还有自己的二级目录，是后续讲义的重点对象，这里先建立印象：

```
algorithms/        contracts.py  registry.py  builtin.py  model_providers.py
                   common/   eagle3/   dflash/   domino/   dspark/   peagle/
application/       composition.py（组合根）   planning.py
training/          assembly.py  backend.py  trainer.py  controller.py
                   checkpoint.py  schedule.py  profiling.py  vocab_mapping.py
                   strategies/（DraftTrainStrategy 各实现）   DESIGN.md
runtime/           contracts.py  control_plane/  data_plane/   ARCHITECTURE.md
inference/         rollout_worker.py  capture.py  adapters/server_capture.py   DESIGN.md
modeling/          auto.py  draft/（registry.py + 各草稿架构）  target/
```

#### 4.2.2 核心流程

把子包串起来，一次 `specforge train` 的请求大致按下面这条主轴流动（粗粒度，细节留给进阶讲义）：

```
cli.py (解析命令)
   │  load_config
   ▼
config/schema.py (类型化 Config：七段配置)
   │  resolve_run  ── 用 training.strategy 查算法注册表
   ▼
application/composition.py (组合根：解析算法 + 校验 + 装配)
   │  build_application_run
   ▼
launch.py (选运行时拓扑构建器：offline / disagg_online_*)
   │
   ├──► training/assembly.py   (装配 ModelBundle、优化器、数据加载 → TrainingRun)
   │        │
   │        ▼
   │    training/trainer.py + controller.py + backend.py (FSDP 训练循环)
   │        ▲
   │        │  TrainBatch（唯一携带张量的跨面契约）
   │        │
   └──► runtime/  (DataFlow：控制面元数据账本 + 数据面特征存储 + 在线引用分发)
            ▲
            │  SampleRef（只携带元数据，不持有张量）
            │
        inference/  (推理/采集面：RolloutWorker → SGLang 捕获)
```

这条主轴对应「**控制面只传元数据、数据面才传张量**」这条 SpecForge 的核心不变量。`runtime/__init__.py` 的 docstring 用一行流水线点明了运行时的职责边界：

[specforge/runtime/__init__.py:1-13](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/__init__.py#L1-L13) — 声明 DataFlow 运行时的流水线 `PromptTask → RolloutWorker → SampleRef → FeatureDataLoader → TrainBatch → Trainer`，并强调控制面只搬运元数据、大张量只走数据面（FeatureStore）。这是后续 u5-l4、u7 系列讲义的总纲。

#### 4.2.3 源码精读

**① 配置层 `config/schema.py`**

[specforge/config/schema.py:9-15](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L9-L15) — docstring 点明 schema 刻意描述的是一个「run（一次运行）」而非一个旧式 Python 脚本：模型装配、prompt 准备、拓扑、策略专属目标函数都藏在这份**经过校验的契约**背后；磁盘上的 YAML/JSON 与命令行点覆盖（`section.field=value`）都经过同一个 schema 重新校验。这就是「未知字段直接报错」的来源。

**② 组合根 `application/composition.py`**

[specforge/application/composition.py:1-17](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/application/composition.py#L1-L17) — 模块顶部声明自己是「解析并装配一次训练运行的唯一组合根」。`ResolvedRun` 是「一份已校验配置 + 它对应的那个算法注册」的不可变配对；`bind_run` 用角色投影后的配置对已有注册做校验。`application/__init__.py` 对外只导出这几个名字（resolve_run / bind_run / build_application_run），是组合根的公开边界。

**③ 训练装配 `training/assembly.py`**

[specforge/training/assembly.py:15-20](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/assembly.py#L15-L20) — docstring 说明装配由组合根驱动：本模块接收一个**不可变的算法注册**并把它贯穿到每个构建器；它**自己从不解析算法名**，也**从不构造进程内的在线目标引擎**（在线采集只来自外部 SGLang 服务，走 disaggregated 运行时）。这条边界解释了为什么 `assembly.py` 不会出现「读 `training.strategy` 字符串」的代码。

**④ 在线引用分发 `runtime/data_plane/ref_distributor.py`**

[specforge/runtime/data_plane/ref_distributor.py:9-18](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/ref_distributor.py#L9-L18) — `RefDistributor` 是 DP 在线 consumer 的集中式 dispatcher，**每个 run 只有一个**（住在 trainer DP rank 0）。它是生产者引用通道的**唯一**读者，也是消费记账的**唯一**持有者；各 rank 只读自己的私有 inbox，不做分区数学、不持账本——设计目标是「单一记账权威，而不是 N 个」。这正是本讲实践任务要找的「在线引用分发」目录。

**⑤ 推理/采集面 `inference/`**

[specforge/inference/__init__.py:1-2](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/inference/__init__.py#L1-L2) — docstring 说明 inference 是「推理 / rollout 面」，包含 rollout worker、capture 配置、适配器与目标引擎；因为这些子模块会 import 较重的模型/SGLang 代码，所以**按需显式导入**，不在包加载时全拉进来。这解释了为什么 `runtime` 的 `__init__` 只 re-export 轻量契约、把重计算面留给 `training` 与 `inference`。

#### 4.2.4 代码实践

**实践目标**：把「目录 → 职责」落到一张可查的对照表，并验证「重子包按需导入」这条边界。

**操作步骤**：

1. 在仓库根目录执行下面的命令，分别列出顶层包与两个重型子包的内容（**只读**，不改任何代码）：

   ```bash
   ls -1 specforge/
   ls -1 specforge/training/
   ls -1 specforge/runtime/data_plane/
   ```

2. 对照 4.2.1 的目录树，把下列 8 个路径填进「目录→职责」表（见下方表格框架）：

   `cli.py`、`config/schema.py`、`application/composition.py`、`training/assembly.py`、`runtime/data_plane/`、`inference/`、`modeling/draft/`、`algorithms/registry.py`

3. 打开 `specforge/runtime/__init__.py`，数一下它 `import` 的是轻量契约（`PromptTask`/`SampleRef`/`TrainBatch` 等）还是重计算模块，验证「运行时包加载时不拉重代码」。

**需要观察的现象**：

- `training/` 下既有装配（`assembly.py`）、训练循环（`trainer.py`/`controller.py`）、FSDP 后端（`backend.py`）、检查点（`checkpoint.py`），也有一个 `strategies/` 子目录（各草稿方法的训练策略）和一份 `DESIGN.md`。
- `runtime/data_plane/` 下确实有 `ref_distributor.py`、`sample_ref_queue.py`、`streaming_ref_channel.py`、`mooncake_store.py`、`offline_reader.py` 等与「在线引用分发 / 特征存储」相关的文件。
- `runtime/__init__.py` 只 re-export 契约类型，没有 `import torch` 之类重依赖的痕迹。

**预期结果**：填出的对照表应与本讲 4.3 节给出的参考表一致；`runtime/__init__.py` 保持轻量。

**「目录→职责」对照表（框架，请你填写后与 4.3 参考表核对）**：

| 目录 / 文件 | 职责（一句话） |
| --- | --- |
| `specforge/cli.py` | _请你填写_ |
| `specforge/config/schema.py` | _请你填写_ |
| `specforge/application/composition.py` | _请你填写_ |
| `specforge/training/assembly.py` | _请你填写_ |
| `specforge/runtime/data_plane/` | _请你填写_ |
| `specforge/inference/` | _请你填写_ |
| `specforge/modeling/draft/` | _请你填写_ |
| `specforge/algorithms/registry.py` | _请你填写_ |

#### 4.2.5 小练习与答案

**练习 1**：`training/assembly.py` 的 docstring 说它「从不解析算法名」。那算法名（如 `eagle3`）是在哪里被解析成算法注册的？
**参考答案**：在组合根 `application/composition.py` 的 `resolve_run` 里——它用配置中的 `training.strategy` 去算法注册表（`algorithms/registry.py`）查到对应的 `AlgorithmRegistration`，再把**这个不可变的注册**传给 `assembly.py`。装配层只消费已解析结果，不自己查名字。

**练习 2**：为什么 `runtime/__init__.py` 只 re-export `PromptTask`/`SampleRef`/`TrainBatch` 这类轻量契约，而不 import trainer 或 rollout worker？
**参考答案**：因为运行时包想保持「依赖轻」——控制面只传元数据。真正的重计算（trainer 在 `training/`、rollout worker 在 `inference/`）由调用方按需显式导入，避免一加载运行时就拉起整条 PyTorch / SGLang 依赖链。这也让「跨面契约」可以独立被测试。

### 4.3 配置与脚本目录

#### 4.3.1 概念说明

除了 `specforge` 源码包，仓库根目录还有几类「**非源码但同样重要**」的资源目录：它们决定了你**怎么写配置、怎么准备数据、怎么离线算特征**。新手最容易忽略这些目录，结果写不出能跑的 YAML。

仓库根目录的真实布局（`ls` 仓库根可得）：

```
SpecForge/  （仓库根）
├── specforge/            # 源码包（见 4.2）
├── pyproject.toml        # 包定义、依赖、console script 入口（u1-l2）
├── version.txt           # 版本号，被 pyproject 动态读取
├── requirements-rocm.txt # ROCm 专属依赖清单（u1-l2）
├── README.md             # 项目总览
├── docs/                 # 文档：get_started / basic_usage / concepts / advanced_features ...
├── examples/             # 可直接参考的样例：configs / disagg / data_regeneration
├── configs/              # 草稿模型 config.json（被 model.draft_model_config 引用）
├── scripts/              # 数据准备 / 离线特征 / 门禁脚本
├── tests/                # 分层测试（algorithms/application/config/... 见 u10-l3）
├── datasets/             # 示例数据
├── patches/              # SGLang 捕获补丁
└── assets/               # 文档配图
```

其中三个目录与日常使用关系最密切：

- **`examples/configs/`**：checked-in 的「黄金样例 YAML」，覆盖 EAGLE3/DFlash/Domino/DSpark/P-EAGLE 的在线/离线/disaggregated 组合。任何新配置都建议从复制一份样例开始改。
- **`configs/`**：草稿模型的 `config.json`（描述草稿架构，如层数、隐藏维度），被 YAML 的 `model.draft_model_config` 字段引用。
- **`scripts/`**：数据与特征准备脚本——`prepare_data.py`（造训练 jsonl）、`prepare_hidden_states.py`（离线算特征）、`regenerate_train_data.py`（数据再生），外加 `gates/`（端到端门禁脚本）。

#### 4.3.2 核心流程

「写一份能跑的训练配置」通常会走这条准备链：

```
1. 想清楚用哪个方法 + 哪种数据模式
      │
      ▼
2. 到 examples/configs/ 复制一份最接近的黄金样例 YAML
      │
      ▼
3. 按需准备数据：
   ├── 在线：scripts/prepare_data.py 造 train_data_path（对话 / 预格式化文本）
   └── 离线：scripts/prepare_data.py 造数据 → scripts/prepare_hidden_states.py 算特征
      │
      ▼
4. 在 YAML 里填好 model / data / training / deployment 等字段
   （model.draft_model_config 指向 configs/ 下的某个 config.json）
      │
      ▼
5. specforge train --config <你的 yaml>   （或先 --plan 预览）
```

> 七段配置（`model` / `data` / `training` / `tracking` / `profiling` / `runtime` / `deployment`）的字段细节是 u2-l2 的主题，本节只建立「去哪找样例、去哪造数据」的地图。

#### 4.3.3 源码精读

`docs/basic_usage/training.md` 开篇就重申了「单一入口 + 黄金样例」的工作方式：

[docs/basic_usage/training.md:1-17](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/docs/basic_usage/training.md#L1-L17) — 文档说明 SpecForge 对所有策略与拓扑只有一个公开训练入口 `specforge train --config <yaml>`，YAML 即「运行契约」；并明确这是一次**刻意的硬切换**：旧的 `scripts/train_*.py` 命令和临时 import 路径已被**删除而非弃用**，下游启动器必须迁移到类型化配置，**没有兼容分发**回到旧训练器。

紧接着文档给出黄金样例表，告诉读者「想跑某个组合，去 `examples/configs/` 抄哪个文件」：

[docs/basic_usage/training.md:196-211](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/docs/basic_usage/training.md#L196-L211) — checked-in 样例表，把「策略 × 模式」映射到一个具体 YAML 文件名，例如 EAGLE3 online → `qwen3-8b-eagle3-disaggregated.yaml`、DFlash offline → `qwen3-8b-dflash-offline.yaml`、DSpark disaggregated → `qwen3-4b-dspark-disaggregated.yaml`、Ascend NPU 上还有专属的 `qwen3.5-4b-dflash-online-npu.yaml` 等。这是「从哪个样例开始改」的权威清单。

> 这些样例文件确实存在于 `examples/configs/`（u2-l1、u2-l2 会带你看字段）。本节只确认「文档指的路真的存在」，建立对仓库布局的信任。

#### 4.3.4 代码实践

**实践目标**：把本讲实践任务要求的「三个定位」补全，并验证黄金样例文件真实存在。

**操作步骤**：

1. 在仓库根目录，**只读**地确认三个关键定位（对应本讲总实践任务）：

   ```bash
   # ① 负责命令解析的文件
   test -f specforge/cli.py && echo "cli.py 存在（命令解析入口）"

   # ② 负责训练装配的文件
   test -f specforge/training/assembly.py && echo "assembly.py 存在（训练装配）"

   # ③ 负责在线引用分发的目录
   test -d specforge/runtime/data_plane && echo "runtime/data_plane/ 存在（在线引用分发）"
   test -f specforge/runtime/data_plane/ref_distributor.py && echo "ref_distributor.py 存在"
   ```

2. 确认黄金样例确实 checked-in：

   ```bash
   ls -1 examples/configs/ | grep -E 'eagle3-offline|dflash-online|dspark-disaggregated'
   ```

3. 用本节开头给的根目录布局，把下面这张「目录→职责」参考表与你在 4.2.4 填写的表对照。

**需要观察的现象**：

- 三个 `test` 命令都打印「存在」，没有报 `No such file`。
- `examples/configs/` 下能 grep 到 `qwen3-8b-eagle3-offline.yaml`、`qwen3-8b-dflash-online.yaml`、`qwen3-4b-dspark-disaggregated.yaml` 这类文件名，与文档样例表一致。

**预期结果**：三个定位文件/目录全部存在；样例文件名与文档表对得上。

**「目录→职责」对照表（参考答案）**：

| 目录 / 文件 | 职责（一句话） |
| --- | --- |
| `specforge/cli.py` | 唯一公开命令入口，解析 train/export/benchmark 并分发（4.1） |
| `specforge/config/schema.py` | 类型化运行配置 `Config` 与 `load_config`，七段配置的单一事实来源 |
| `specforge/application/composition.py` | 组合根：解析算法、校验、装配成可执行 run（4.2） |
| `specforge/training/assembly.py` | 训练装配：拼出草稿/目标/tokenizer/优化器/数据加载 → `TrainingRun` |
| `specforge/runtime/data_plane/` | 在线引用分发 + 特征存储（`ref_distributor.py` 是 DP 集中式 dispatcher） |
| `specforge/inference/` | 推理/采集面：rollout worker + SGLang 捕获适配器 |
| `specforge/modeling/draft/` | 草稿模型架构 + `registry.py` 的 `@register_draft` 注册轴 |
| `specforge/algorithms/registry.py` | 算法注册表：把算法 spec 与 providers 绑定、`resolve` 查找 |

#### 4.3.5 小练习与答案

**练习 1**：你想跑一个 DFlash 的离线训练，应该从哪个样例 YAML 开始改？去哪个目录找？
**参考答案**：从 `examples/configs/qwen3-8b-dflash-offline.yaml` 开始改（见 docs 样例表）。它属于 offline 模式，`data` 段应指向用 `scripts/prepare_hidden_states.py` 预先算好的特征路径。

**练习 2**：YAML 里 `model.draft_model_config: configs/qwen3-8b-eagle3.json` 指向的文件在仓库哪个目录？它描述的是什么？
**参考答案**：指向仓库根的 `configs/` 目录下的 `config.json`。它描述的是**草稿模型的架构**（如 EAGLE3 一层、隐藏维度等），而不是训练超参——训练超参在 YAML 的 `training` 段。

**练习 3**：`scripts/train_*.py` 这种旧训练脚本还能用吗？
**参考答案**：不能。文档明确这是一次刻意的硬切换，旧的 `scripts/train_*.py` 命令和临时 import 路径已被**删除**（不是弃用），没有兼容分发，必须迁移到 `specforge train --config`。

## 5. 综合实践

**任务**：为「SpecForge 源码导航」制作一张属于你自己的**一页速查图**，把本讲三块内容串起来。

要求完成以下三步：

1. **画主轴**：用文本框/箭头画出从 `cli.py` 到 `Trainer` 的请求主轴（参考 4.2.2 的流程图），并在每个节点旁标注「它住在哪个目录、职责一句话」。至少包含：`cli.py → config/schema.py → application/composition.py → launch.py → training/assembly.py → training/trainer.py`，并标出 `runtime/`（数据/控制面）与 `inference/`（采集面）挂在哪里。

2. **填三张定位卡**（直接回答本讲的总实践任务）：
   - 负责命令解析的文件 = ?（给出路径 + 行号区间 permalink）
   - 负责训练装配的文件 = ?（给出路径 + docstring 的行号区间 permalink）
   - 负责在线引用分发的目录 = ?（给出目录 + 该目录里 dispatcher 文件 docstring 的 permalink）

3. **加一个「数据准备」侧栏**：写出 online 与 offline 两种模式分别要用 `scripts/` 下哪两个脚本准备数据，并指出黄金样例 YAML 在哪个目录。

**交付物**：一份 Markdown 笔记（可放进你自己的学习仓库，不要写进 `SpecForge-tutorial/`），要求所有路径、文件名、行号都来自本讲已验证的真实源码，不得编造。完成后，你应能在不看本讲义的情况下，30 秒内回答「我想看 X 功能的代码该去哪个目录」。

## 6. 本讲小结

- SpecForge 全项目只有**一个公开命令入口** `specforge`，含 `train`/`export`/`benchmark` 三个子命令，所有草稿方法都从 `specforge train --config` 进入（`cli.py`）。
- `cli.py` 刻意只做**命令解析 + 分布式进程生命周期**，模型与数据装配不在这里；所有 worker 路径最终汇入组合根 `build_application_run(resolved).run()`。
- 源码按 **配置(config) → 组合根(application) → 算法(algorithms) → 装配/训练(training) → 运行时(runtime) → 采集(inference) → 模型结构(modeling)** 分层，每个子包职责单一。
- `runtime/` 守住一条铁律：**控制面只传元数据、大张量只走数据面**；`runtime/data_plane/ref_distributor.py` 是在线 consumer 的集中式引用 dispatcher。
- 仓库根的 `examples/configs/` 是黄金样例 YAML 的来源，`configs/` 放草稿架构 `config.json`，`scripts/` 放数据与离线特征准备脚本——写配置前先抄样例。
- 旧的 `scripts/train_*.py` 已被**删除而非弃用**，没有兼容路径，必须用类型化 YAML。

## 7. 下一步学习建议

有了这张地图，接下来两条路：

1. **先把入口链路走通（推荐下一步）**：进入第 3 单元 u3-l1《CLI 入口与命令解析》，深入 `cli.py` 的 `main()`/`_train()`/`_bootstrap_single_process_env()` 与信号处理，看「命令解析 → 进程拓扑 → 组合根」每一步的真实代码。
2. **或先动手跑一次训练**：如果你更想先有体感，可先跳到 u2-l1《五分钟跑通一次训练》，用一个 checked-in 样例走通 `specforge train`，再回头读 u3。

之后依次推进：u3（入口与启动链路）→ u4（算法注册与契约）→ u6（训练主链路）→ u7（DataFlow 运行时）。本讲建立的「目录→职责」对照表，会在这些讲义里反复被用到，建议常备手边。
