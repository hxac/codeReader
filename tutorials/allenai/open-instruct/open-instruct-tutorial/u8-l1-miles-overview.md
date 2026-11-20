# u8-l1 MILES 总览与架构分层

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 MILES 与 `grpo.py`（Core/vLLM）、`grpo_fast.py`（DeepSpeed/vLLM）三条 RL 训练路线的关系与各自适用范围。
2. 理解 MILES 的所有权分层：open-instruct 包装层负责什么、MILES 运行时负责什么、Core 适配器经 OLMo-core 与 SGLang 又负责什么。
3. 会用 `python -m open_instruct.miles` 的五个子命令（`plan` / `validate` / `train` / `run` / `status`）以及 `records` 记录分析工具。
4. 知道维护示例按模型家族组织在 `configs/miles/examples/tiny/` 与 `configs/miles/examples/small/` 下：tiny 家族是力学/学习示例，small 家族在上一轮（PR #2280）从 dev 占位副本升级为实测的 OLMo 3.5 Small（A4B）B300 三档配方（two-node-mech / four-node / eight-node），且全部维护示例的损失基线已翻转为 token-mean（PR #2271）。
5. 掌握本轮（PR #2289，提交 27f868159）固化的**学习示例恢复保存基线**：Tiny node/three-node 与 Small four/eight-node 统一为每 10 更新保存、保留最近 2 份、外加 50/100 更新里程碑与最终存档，Small four/eight-node 从「禁用保存、不自动恢复」翻转为启用；本轮 #2293 还把 Small 三份配方的心跳超时从 120 升到 480 秒、MILES 运行时 pin 升至 9eff46a4。
6. 了解文档地图的扩展脉络：`grading.md` 与 `abstention.md`（上一轮）、`olmo35-small.md`（上一轮）及其本轮新增的「Checkpoint settings in the current starters」一节，以及 examples README 本轮新增的「Recovery checkpoints and preserved milestones」共享策略节。

本讲是 MILES 单元（u8）的第一篇，只做总览不深入细节；配置系统、工作流执行、奖励体系分别在 u8-l2、u8-l3、u8-l6 及之后各讲展开，Small B300 配方的显存账目与吞吐审计另立一讲（u8-l15）。

## 2. 前置知识

阅读本讲前，建议先具备以下概念（不熟悉也没关系，下面用通俗语言解释）：

- **RLVR / GRPO**：可验证奖励强化学习（RL with Verifiable Rewards）。对数学题、代码这类有标准答案的任务，可以用一个「验证器」自动判分作为奖励，而不需要训练额外的奖励模型。GRPO 是一种组相对策略优化算法：对同一个 prompt 采样一组回答，按组内奖励的相对高低计算优势。已在 u6-l1、u6-l2 讲过。
- **rollout（rollout 编排）**：RL 训练的一步是「让当前策略生成一批回答 → 判分 → 用结果更新策略」。生成这批回答的过程及其调度（队列、准入、引擎管理）称为 rollout 编排。
- **Ray actor**：Ray 分布式框架中的常驻进程，持有 GPU 资源并响应远程调用。MILES 运行时用 Ray actor 承载训练器与推理引擎。
- **OLMo-core 与 SGLang**：OLMo-core 是 Allen AI 的模型与分布式训练库（提供模型结构、checkpoint、分布式原语）；SGLang 是推理服务框架。MILES 用前者训练、后者服务。
- **TOML run 文件**：MILES 用一个结构化 TOML 文件描述一次完整运行（模型、数据、拓扑、启动、追踪等），是所有子命令的输入。
- **token-mean 与 response-mean 损失加权**：一个批次里有长有短的回答。response-mean（按响应平均）给每个回答同等权重，长回答里的每个 token 影响被稀释；token-mean（按 token 平均，`calculate_per_token_loss=true`）给每个 token 同等权重。维护示例统一采用 token-mean，但省略该设置时库默认仍是 response-mean，以兼容既有 run。
- **有界恢复保存与永久里程碑（bounded recovery saves / preserved milestones）**：RL 训练一次运行数小时到数天，被抢占或中断后要能从最近的完整存档恢复。「有界」指普通恢复保存滚动保留最近 N 份（磁盘占用不随时间无限增长）；「里程碑」（`checkpoint_keep_every`）指每隔更长的更新间隔**永久**保留一份，供扩展运行与事后回溯；另有 `save_final_checkpoint` 在结束时落最终存档。本轮 #2289 把学习型模板统一到「每 10 更新保存、保留最近 2 份、50 或 100 更新里程碑 + 最终存档」。
- **心跳超时（heartbeat_timeout）与受保护最短运行时（min_runtime）**：Beaker 集群上驱动进程靠心跳向监控证明自己还活着，超时（本轮 Small 配方从 120 秒升至 480 秒）会被判定为僵死；`min_runtime` 是调度器保证分配的受保护时长。恢复保存的节奏必须与这两者配合——首个完整恢复保存应落在受保护时长之内。
- **B300 与 MoE**：B300 是 NVIDIA 的 Blackwell Ultra 数据中心 GPU（AI2 的 Holmes 集群使用）；MoE（混合专家）模型每个 token 只激活部分专家参数。OLMo 3.5 Small（A4B）是 256 专家、约 0.8B 激活参数的小型 MoE 模型。
- **Beaker**：AI2 的实验平台，MILES 的 `run` 子命令把 TOML 编译成 Beaker 实验提交。不熟悉 Beaker 也不影响本讲，详见 u8-l8。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [docs/miles/index.md](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/index.md) | MILES 文档首页：定位声明、入门顺序、文档地图表、权威性与维护规则 |
| [docs/miles/architecture.md](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/architecture.md) | 架构与开发：分层所有权表、运行时锁与镜像、启动依赖、本地开发检查、适配器包布局 |
| [open_instruct/miles/__main__.py](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/__main__.py) | CLI 入口：`python -m open_instruct.miles`，加载 run 文件并分发 plan/validate/train/run/status 与 records |
| [docs/miles/grpo.md](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/grpo.md) | MILES GRPO 指南：首次运行步骤、配置约定、判分/截断/未判分语义、发布模式 |
| [configs/miles/examples/README.md](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/README.md) | 维护示例的导读：按模型家族分组（tiny/small），六份模板的用途、基线设置，以及本轮新增的「Recovery checkpoints and preserved milestones」共享恢复策略 |
| [configs/miles/examples/tiny/dev.toml](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/tiny/dev.toml) | 单 GPU 共置（colocated）微型模型力学检查模板，本讲实践对象 |
| [docs/miles/olmo35-small.md](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/olmo35-small.md) | OLMo 3.5 Small B300 实测布局、显存账目、吞吐审计与失败记录；本轮新增「Checkpoint settings in the current starters」一节 |
| [configs/miles/moe_configs/README.md](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/moe_configs/README.md) | SGLang fused-MoE tile 调优文件的生成、分发与版本匹配规则 |
| [docs/miles/grading.md](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/grading.md) | 共享判分输入（ResponseParts、objective/scope/require_response 契约）与行为变更登记 |
| [docs/miles/abstention.md](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/abstention.md) | Nemotron 弃答式 QA 奖励的语义、rubric 与裁判服务复用 |

## 4. 核心概念与源码讲解

### 4.1 CLI 入口：`python -m open_instruct.miles`

#### 4.1.1 概念说明

MILES 在 open-instruct 中的公开入口只有一条命令：`python -m open_instruct.miles`。它把「规划、校验、本地执行、Beaker 提交、状态查询」以及「推理记录分析」全部收拢到一个入口后面。

为什么这样设计？

- **研究者的全部意图浓缩在一个 TOML run 文件里**。同一条命令对不同子命令复用同一份配置，避免「训练用一套脚本、提交用另一套脚本」的漂移。
- **运行时导入是延迟的**。GPU/MILES 运行时只在真正需要的子命令里才 import，因此 `plan` / `validate` 这类规划命令在没有 GPU、没有安装 MILES 私有运行时的笔记本电脑上也能运行。这一点是文档明确承诺的：[docs/miles/index.md:L21-L23](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/index.md#L21-L23) 写明 "Planning does not require GPUs or mounted checkpoints"。

#### 4.1.2 核心流程

CLI 的一次执行流程：

```text
python -m open_instruct.miles COMMAND CONFIG.toml [--set SECTION.KEY=VALUE ...]
        │
        ├─ sys.argv[1] == "records"？
        │    ├─ "select"        → open_instruct.miles.datasets.record_selection
        │    ├─ "replay-pool"   → open_instruct.miles.datasets.replay_summary
        │    └─ 其他            → miles.utils.record_summary（需 MILES 运行时）
        │
        └─ 否则：argparse 解析 COMMAND ∈ {plan, validate, train, run, status}
             │
             ├─ 读取 TOML → payload
             ├─ 判断结构化：payload 含 schema_version 或 model 字段？
             │    ├─ 是 → specs.from_dict(...) 解析为 RunSpec（结构化 run 文件）
             │    └─ 否 → RunConfig.from_dict(...)（低层原始配置）
             │
             └─ 按命令分发：
                  plan     → config.plan()，打印计划 JSON + 提示（advisories）
                  validate → 结构化：plan() 全量校验；原始：parse_runtime()
                  run      → execution.launch.run()（提交 Beaker，仅限结构化）
                  status   → execution.launch.status()（仅限结构化）
                  train    → workflow.execute(config)（结构化）/ train_config（原始）
                             （OPDRunSpec 走独立的 distillation.opd_runtime 路径）
```

注意两个设计点：

1. **结构化 run 文件与低层配置双轨**。`run`/`status` 只接受 `schema_version=1` 的结构化文件；低层原始配置只支持 `plan`/`validate`/`train`。这让「研究者视角的完整 run」与「底层调参视角的 CoreConfig」都能进门，但提交必须走结构化路线。
2. **错误处理友好**。输入错误（`InputError`）默认被转成一行 `parser.error` 提示而不是一长串 traceback，加 `--debug` 才展开。

#### 4.1.3 源码精读

> 近两轮提交（#2271/#2280/#2286 与本轮 #2289/#2293）均未改动此文件，行号与上一版讲义一致。

模块 docstring 即是命令速查，明确写出五个子命令与 records 的用法：[open_instruct/miles/__main__.py:L1-L12](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/__main__.py#L1-L12)。其中 L4-L5 说明「运行时导入延迟到需要它的命令，规划无需 GPU 栈」这一关键约束。

`main()` 的第一步先拦截 `records` 分支，按子命令名选择实现模块；若缺 `miles` 包则给出「需在锁定运行时中运行」的退出提示：[open_instruct/miles/__main__.py:L26-L44](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/__main__.py#L26-L44)。这段代码同时示范了「延迟导入」模式——`importlib.import_module` 只在确定目标后才执行。

主解析器只有三个位置参数：命令、配置路径与可重复的 `--set` 覆盖：[open_instruct/miles/__main__.py:L45-L57](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/__main__.py#L45-L57)。L46 的 `choices=("plan", "validate", "train", "run", "status")` 就是「五个子命令」的出处。

`execute()` 读取 TOML 文档后，用 `schema_version in payload or "model" in payload` 判定结构化与否，再分别构造 `RunSpec`（经 `specs.from_dict`）或 `RunConfig`：[open_instruct/miles/__main__.py:L66-L73](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/__main__.py#L66-L73)。这是「TOML → RunSpec → RunConfig」编译链的入口（编译链细节在 u8-l2 精读）。

`plan` 分支调用 `config.plan()` 并打印 JSON 与 advisories，不做任何 GPU 操作：[open_instruct/miles/__main__.py:L74-L78](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/__main__.py#L74-L78)。

`run`/`status` 要求结构化文件并延迟导入 `execution.launch`：[open_instruct/miles/__main__.py:L79-L87](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/__main__.py#L79-L87)。L81 的报错信息直接写明「raw configs support plan/validate/train」。

`validate` 与 `train` 的分发：结构化校验会连物理分配一起 `plan()`，而训练交给 `workflow.execute`（细节在 u8-l3）：[open_instruct/miles/__main__.py:L96-L109](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/__main__.py#L96-L109)。OPD（蒸馏）规格有独立分支（L88-L95），本讲不展开。

#### 4.1.4 代码实践

**实践目标**：在无 GPU、无 MILES 运行时的机器上，用 `plan` 验证一份维护示例配置，体会「规划不需要 GPU 栈」。

**操作步骤**：

1. 在仓库根目录执行（open-instruct 用 uv 管理依赖，见 u1-l2）：

   ```bash
   uv run python -m open_instruct.miles plan configs/miles/examples/tiny/dev.toml
   ```

2. 观察输出的 JSON 计划（包含解析后的拓扑、批次几何与编译选项）以及 advisories 提示。
3. 再试一次带覆盖的规划，观察字段被就地替换：

   ```bash
   uv run python -m open_instruct.miles plan configs/miles/examples/tiny/dev.toml \
     --set training.num_rollouts=2
   ```

4. 故意打错命令名验证 argparse 的 choices 约束：

   ```bash
   uv run python -m open_instruct.miles plans configs/miles/examples/tiny/dev.toml
   ```

**需要观察的现象**：步骤 1/2 输出计划 JSON 与提示；步骤 3 中 `training.num_rollouts` 从模板里的 4 变为 2；步骤 4 报 `invalid choice` 错误并列出五个合法命令。

**预期结果**：`plan` 只做 CPU 侧的配置解析与物理分配规划，不触发 GPU 运行时导入；本讲撰写时未在本地实际执行该命令，输出样例**待本地验证**（文档承诺见 [docs/miles/index.md:L21-L23](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/index.md#L21-L23)）。注意：模板中的 `/weka/.../YOUR_USERNAME/...` 是占位路径，个别校验可能对其报提示，属正常现象；`plan` 不要求挂载这些路径。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `run`/`status` 拒绝低层原始配置（`RunConfig`），只接受 `schema_version=1` 的结构化文件？

**参考答案**：`run` 要把「模型、数据、拓扑、启动、挂载」完整编译成 Beaker 实验，这些信息只有结构化 RunSpec 才齐备；低层 `RunConfig` 只描述训练器参数，缺少提交所需的 launch/output 等段落。源码在 [open_instruct/miles/__main__.py:L79-L81](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/__main__.py#L79-L81) 显式报错。

**练习 2**：`python -m open_instruct.miles records summarize` 与 `plan` 对运行环境的依赖有何不同？

**参考答案**：`records summarize` 分发到 MILES 包内的 `miles.utils.record_summary`，需要安装 MILES 私有运行时（缺失时报「requires the MILES package」）；`plan` 只依赖 open-instruct 的 CPU 配置代码，无需 GPU 栈。见 [open_instruct/miles/__main__.py:L27-L44](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/__main__.py#L27-L44)。

**练习 3**：`--set name=my-run` 这样的覆盖发生在哪个阶段？与直接改 TOML 有何区别？

**参考答案**：覆盖作为 `overrides` 参数传给 `specs.from_dict` / `RunConfig.from_dict`（[open_instruct/miles/__main__.py:L69-L73](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/__main__.py#L69-L73)），在解析时叠加到 TOML 文档之上。区别：TOML 文件保持原样（可继续作为版本化的基准），覆盖只影响这一次调用，适合临时调参、sweep 与调试。

### 4.2 架构分层：三层所有权与适配器包布局

#### 4.2.1 概念说明

MILES 不是「又一个训练脚本」，而是把一次 RL 运行拆给三个所有者：

- **open-instruct 包装层**：研究者面对的一切——配置校验、数据准备、验证器/奖励、Beaker 提交与回执、运行产物。
- **MILES 运行时**（私有、按锁定制）：rollout actor、SGLang 推理引擎、采样/优势/损失机制、共享传输。
- **Core 适配器**（位于 MILES 内）：模型构建、打分与训练前向、梯度同步、优化器与 checkpoint、权重导出；训练走 OLMo-core，服务走 SGLang。

为什么要有这条边界？因为 open-instruct 是公开研究仓库，而 MILES 运行时（含其 pinned fork）是独立发布的私有组件。open-instruct 负责「研究者想要什么」，MILES 负责「RL 机制怎么做」，Core 适配器负责「在 OLMo-core 上真正训练」。理解这条边界后，你在 open-instruct 仓库里看到的每个 `open_instruct/miles/` 子包都能归位到某一层。

同时要记住：**MILES 不取代既有 GRPO 训练器**。`grpo.py`（Core/vLLM）与 `grpo_fast.py`（DeepSpeed/vLLM，u6-l4 讲过其 Ray 拓扑）仍是稠密模型训练与历史复现的现行选择；MILES 面向受支持的 Olmo MoE 模型（也提供稠密 Olmo 3 适配器，但验证较少）。

#### 4.2.2 核心流程

一次 MILES 运行的纵向视角：

```text
研究者编写 run.toml（schema_version=1）
        │  python -m open_instruct.miles plan / validate      ← open-instruct：CPU 校验与规划
        ▼
RunSpec ──编译──▶ RunConfig（含 CoreConfig 与原生 MILES 选项）
        │  python -m open_instruct.miles run                  ← open-instruct：编译 Beaker 实验并提交
        ▼
Beaker 上的应用镜像（按 runtime/miles/runtime.lock.json 构建）
        │
        ▼
MILES 运行时启动：Ray 集群、裁判加载、工作流准备并发启动
        │
        ▼
Core 适配器（OLMoCoreTrainRayActor）：SGLang 服务策略 → rollout → 打分 →
OLMo-core 训练前向/反向 → 权重发布（refresh/barrier/engine_drain）→ checkpoint/导出
```

分层所有权表（五层，前三层是所有权划分，后两层是上游基座）：

| 层 | 拥有 |
|---|---|
| Open Instruct 包装层 | 配置校验、准备、验证器、提交/回执、运行产物 |
| MILES 运行时 | rollout actor、SGLang 引擎、采样/优势/损失机制、共享传输助手 |
| Core 适配器（在 MILES 中） | 模型构建、文档/回放对齐、打分与训练前向、梯度同步、优化器/checkpoint、张量导出 |
| OLMo-core | 模型与分布式训练原语、注意力/专家执行 |
| olmo-sglang | 服务架构注册与模型专属执行 |

#### 4.2.3 源码精读

> 近两轮提交均未改动 `docs/miles/index.md` 与 `docs/miles/architecture.md`，下列行号与上一版讲义一致；但本轮 #2293 把运行时锁里的 MILES revision 向前推进了（见本节末尾）。

定位声明与三句式所有权划分写在文档首页开头：[docs/miles/index.md:L3-L6](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/index.md#L3-L6) ——「Open Instruct owns the researcher configuration and data/reward integration; MILES owns rollout orchestration and shared RL machinery; the adapter trains through OLMo-core and serves through SGLang」。

与既有训练器的关系（不弃用、仍可用、按模型与负载选择）：[docs/miles/index.md:L8-L13](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/index.md#L8-L13)。GRPO 指南开头同样强调「recommended path for supported Olmo MoE models…Existing Core/vLLM and DeepSpeed/vLLM workflows remain available」：[docs/miles/grpo.md:L3-L8](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/grpo.md#L3-L8)。

架构页第一段给出「RunSpec 编译成 RunConfig（含 CoreConfig 与原生 MILES 选项）」的链条，并指出 Python API 也可供集成使用：[docs/miles/architecture.md:L3-L6](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/architecture.md#L3-L6)。

上文的五层所有权表出自：[docs/miles/architecture.md:L8-L14](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/architecture.md#L8-L14)。Core 训练 actor 的边界（`miles.backends.core_utils.actor.OLMoCoreTrainRayActor` 实现训练边界，模型/checkpoint 代码住在 pinned MILES fork 中）见：[docs/miles/architecture.md:L16-L21](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/architecture.md#L16-L21)。

运行时依赖的「事实源」是 `runtime/miles/runtime.lock.json`——锁定依赖 commit 与不可变二进制基座，构建时不打补丁、不跟随分支尖端；open-instruct 自身的普通环境保持独立版本：[docs/miles/architecture.md:L25-L30](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/architecture.md#L25-L30)。这就是「MILES 训练镜像 ≠ open-instruct 开发环境」的原因（对启动与镜像的影响见 u8-l8）。本轮 #2293 给了一个活例子：锁中 MILES 运行时的 revision 从 22d2fb4c 前进到 9eff46a4（[runtime/miles/runtime.lock.json:L13-L16](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/runtime/miles/runtime.lock.json#L13-L16)）——open-instruct 侧代码零改动，但镜像必须按新锁重建才能带上 writer-only HF 导出存储与启动权重检查等修复。

open-instruct 侧的代码按职责分包，`configuration`/`execution` 等八个包与 MILES 侧 `backends/core_utils` 的分工表在：[docs/miles/architecture.md:L271-L298](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/architecture.md#L271-L298)。L288-L291 特别澄清了三个容易混淆的数据模块：MILES `backends/core_utils/data.py` 适配样本给训练器；`rollout/data_source.py` 拥有运行时数据源行为；`datasets/run_data.py` 准备研究者输入。

后端训练器与托管 rollout 模块**不导入 open-instruct**；open-instruct 以回调形式供给 `core_records_factory`（逐题记录）与 `core_evaluation_snapshot`（按需 HF 导出）：[docs/miles/architecture.md:L304-L318](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/architecture.md#L304-L318)。这是三层边界的代码级体现：依赖箭头从 MILES 指向接口，而不是反过来。（本轮 #2293 把快照回调更名为 `mid_rollout_snapshot` 并把边界导出职责移给 driver，详见 u8-l3/u8-l9。）

训练生命周期检查引用的示例路径为 `configs/miles/examples/tiny/dev.toml`（示例重组后的位置）：[docs/miles/architecture.md:L212-L217](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/architecture.md#L212-L217)。

#### 4.2.4 代码实践

**实践目标**：把五层所有权表落到 open-instruct 仓库的实际目录上，绘制一张三层架构图。

**操作步骤**：

1. 列出 open-instruct 侧的 MILES 包结构：

   ```bash
   ls open_instruct/miles/
   ```

2. 对照 [docs/miles/architecture.md:L276-L291](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/architecture.md#L276-L291) 的包职责表，把每个子目录（`configuration`、`execution`、`rollout`、`rewards`、`datasets`、`evaluation`、`infrastructure`…）标注到五层表中的一层。
3. 画一张纵向架构图：顶层「研究者 run.toml」，其下三个所有权框（Open Instruct 包装层 / MILES 运行时 / Core 适配器），底座两个上游框（OLMo-core、olmo-sglang）；用箭头标注 RunSpec→RunConfig 编译、rollout 编排、权重发布与 checkpoint 导出的流向。
4. 用只读 git 命令回顾示例目录与运行时锁的近期演变：

   ```bash
   git log --oneline --follow -- configs/miles/examples/tiny/dev.toml | head -5
   git log --oneline --diff-filter=D -- configs/miles/examples/small/dev.toml  # 上一轮：删除 dev 占位（PR #2280）
   git log --oneline -3 -- configs/miles/examples/README.md                    # 本轮：恢复保存策略（PR #2289）
   git log --oneline -2 -- runtime/miles/runtime.lock.json                     # 本轮：pin 升级（PR #2293）
   ```

**需要观察的现象**：步骤 1 列出的子目录与文档职责表一一对应；步骤 4 中 `--follow` 历史显示 dev.toml 从 `configs/miles/examples/` 移入 `examples/tiny/` 的轨迹，`small/dev.toml` 在提交 92776a730（PR #2280）被删除，README 最近一次实质改动来自 27f868159（PR #2289，恢复保存策略），运行时锁最近一次前进来自 2063f854b（PR #2293）。

**预期结果**：得到一张可复述的三层架构图；能口头回答「`open_instruct/miles/rewards/` 属于哪一层」（包装层：验证器与奖励路由）与「权重发布代码住在哪一层」（MILES `backends/core_utils/publication`，open-instruct 仓库中不存在该代码）。步骤 3 的图为手工产物，无唯一标准答案；步骤 4 命令为只读 git 操作，可安全执行。

#### 4.2.5 小练习与答案

**练习 1**：MILES 与 `grpo_fast.py` 都用 Ray + 多进程做 RL，本质区别是什么？

**参考答案**：`grpo_fast.py` 是 open-instruct 仓库内的一个完整训练脚本（DeepSpeed 训练器 + vLLM 引擎，代码全部在本仓库，u6-l4）；MILES 把「rollout 编排与共享 RL 机制」交给私有 MILES 运行时（按 `runtime.lock.json` 锁定），open-instruct 只保留配置/数据/奖励集成与提交，训练走 OLMo-core、服务走 SGLang。另见 [docs/miles/index.md:L8-L13](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/index.md#L8-L13)：引入 MILES 不弃用既有 GRPO 训练器。

**练习 2**：如果要在 MILES 训练中新增一种自定义验证器，改动应落在哪一层？为什么？

**参考答案**：落在 open-instruct 包装层的 `open_instruct/miles/rewards/`（验证器、奖励路由与托管裁判），以可信注册表条目（factory + 配置）暴露给 MILES。因为后端训练器与托管 rollout 模块不导入 open-instruct（[docs/miles/architecture.md:L304-L313](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/architecture.md#L304-L313)），MILES 侧只通过约定路径消费应用侧回调与注册表。（验证器细节见 u8-l6。）

**练习 3**：为什么 open-instruct 的普通 `uv.lock` 环境跑不了 MILES 训练，而 `plan` 却能跑？本轮运行时锁前进后，旧镜像还能继续用吗？

**参考答案**：训练需要 MILES 私有运行时、SGLang 与锁定版本的 OLMo-core，这些由 `runtime/miles/runtime.lock.json` 与专用应用镜像提供（[docs/miles/architecture.md:L25-L30](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/architecture.md#L25-L30)、[docs/miles/architecture.md:L179-L193](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/architecture.md#L179-L193)）；`plan` 只走 CPU 配置代码，CLI 把运行时 import 延迟到需要它的命令（[open_instruct/miles/__main__.py:L4-L5](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/__main__.py#L4-L5)）。旧镜像不可靠：锁的 revision 已从 22d2fb4c 升到 9eff46a4（[runtime/miles/runtime.lock.json:L13-L16](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/runtime/miles/runtime.lock.json#L13-L16)），examples README 明确提醒 launcher 只核对镜像 ID、不核对镜像内源码是否匹配当前锁，应核对镜像构建记录或直接重建（[configs/miles/examples/README.md:L83-L91](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/README.md#L83-L91)）。

### 4.3 文档地图与近期扩展

#### 4.3.1 概念说明

`docs/miles/` 是 MILES 的权威文档目录：当前指令都住在这里，示例 TOML 表达起始配方，配置代码 + pinned 原生解析器快照 + `runtime/miles/runtime.lock.json` 共同定义「接受哪些设置、运行时来源是什么」。理解文档地图的意义在于：MILES 的文档是**分层且部分自动生成**的——若干参考页由 MkDocs 从配置源在内存中生成、不入库，手写文档与生成文档的维护方式不同。

文档地图近几轮连续扩展，都与本单元后续讲义直接相关：

1. **上一轮新增 `docs/miles/grading.md`（共享判分输入）与 `docs/miles/abstention.md`（Nemotron 弃答 QA）**：grading.md 统一了 MILES 的判分输入——历史上各判分器自己选答案区域，思考里的正确答案可能拿到分而交付答案却是错的；现在由共享的 `ResponseParts` 一次切分 thinking 与交付响应，并用 objective（answer/process/format/composite）× scope × require_response 声明每个验证器的输入契约（深入讲解在 u8-l12）。abstention.md 记录原生弃答式 QA 奖励链路（深入讲解在 u8-l13）。
2. **上一轮新增 `docs/miles/olmo35-small.md`**（PR #2280）：OLMo 3.5 Small（A4B）B300 的实测布局、显存账目、从 Beaker 工件独立重算的吞吐审计与失败记录，是 `configs/miles/examples/small/` 三档配方的配套文档（深入讲解在 u8-l15）。
3. **上一轮的基线措辞翻转**（PR #2271）：`grpo.md` 的 Truncated responses 一节、`development-defaults.md` 的 Loss weighting 条目、`run-controls.md` 与 `throughput-profiles.md` 中的示例数字，全部从 per-response（response-mean）损失改为 token-mean，Tiny 学习示例统一为 256 prompts × 8、batch 2048、恒定 LR 6e-6。
4. **上一轮的裁判规则登记**（PR #2286）：`managed-judges.md` 记录「绑定表可以覆盖比所选数据子集更多的源，未使用绑定被允许」（详见 u8-l6/u8-l13）。
5. **本轮（2f7889da9 → 2063f854b，PR #2289/#2293）**：examples README 新增共享恢复策略节「Recovery checkpoints and preserved milestones」；`olmo35-small.md` 改写资格检查段并新增「Checkpoint settings in the current starters」一节；`development-defaults.md` 的 Native checkpoints 条目改写为「每 10 更新、目标约 1-2 小时一次、保留最近两份 + 里程碑 + 最终存档」。#2293 的代码侧变化（mid_rollout_snapshot、driver 边界导出、恢复宽容键）属于 u8-l3/u8-l9 的范围，本讲只在示例文件里看到它的心跳超时痕迹（120→480）。

#### 4.3.2 核心流程

文档地图的检索路径：

```text
docs/miles/index.md（首页）
  ├─ Start here 五步入门顺序（L15-L23）
  ├─ Documentation map 表：按「什么时候用」组织的主表（L25-L53，25 行）
  └─ Authority and maintenance：权威性与生成式文档的维护规则（L55-L66）

mkdocs.yml 导航（/miles/ 一节）
  ├─ "Models and training" 组的 "OLMo 3.5 Small on B300" 条目（L110，上一轮）
  └─ "Data and evaluation" 组含 "Grading inputs and changes"（L121，上一轮）

正文中的上下文链接
  ├─ grpo.md → grading.md（判分契约，L107/L177）
  └─ source-preparation.md → abstention.md（弃答 QA，L578）

configs/miles/examples/README.md
  └─ 末尾新增「Recovery checkpoints and preserved milestones」共享策略节（L328-L364，本轮）
```

要点：**首页文档表不是穷举**。`grading.md`、`abstention.md`、`olmo35-small.md` 都不作为首页表格的行出现，而是经由站点导航与上下文链接接入。查文档时要两条路径都走。

维护规则方面，自动生成参考页用 `python -m scripts.miles.generate_docs` 生成、`--check` 校验；普通文档构建与本地 `plan`/`validate` 都不需要安装 MILES：[docs/miles/index.md:L55-L66](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/index.md#L55-L66)。

#### 4.3.3 源码精读

入门五步顺序（从 GRPO 指南开始，到复制结构化示例、读 workflow、follow 启动指南、用 operations 判完成）：[docs/miles/index.md:L15-L23](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/index.md#L15-L23)。注意 L20 指向按家族重组后的示例目录（`configs/miles/examples/README.md`）。

首页的文档地图主表（「Document | Use it when」两列，共 25 行）：[docs/miles/index.md:L25-L53](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/index.md#L25-L53)。这张表没有 grading.md / abstention.md / olmo35-small.md 的行——三页经由下面的链接接入。

GRPO 指南「Truncated responses」一节（上一轮改写为 token-mean 基线）：示例使用 token-mean 损失加权与截断零奖励，并显式给出两个设置，截断零奖励不可与其他奖励后处理组合，「最终答案选择与显式验证器例外见 grading.md」：[docs/miles/grpo.md:L88-L107](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/grpo.md#L88-L107)。其中 L96-L102 的 TOML 片段把 `reward_zero_truncated = true` 与 `calculate_per_token_loss = true` 写成可复制的基线。

「Final-answer-only rewards」一节：`core.reward_final_answer_only=true` 只判最后一个字面 `</think>` 之后的文本；缺失闭合标记、空白最终答案或重开思考会拿到「要求响应的验证器的地板分」而不调用它；共享 `ResponseParts` 区分 thinking 与交付的答案/动作：[docs/miles/grpo.md:L161-L177](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/grpo.md#L161-L177)。消费者迁移契约（`selected_input=True` 直通、`async_call_parts` 复合接口）在 [docs/miles/grpo.md:L179-L189](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/grpo.md#L179-L189)。这两节是 u8-l12 的门牌。

`docs/miles/grading.md` 自身开门见山给出动机——「历史上各判分器自选答案区域，思考里的正确答案可能得分而交付答案错误或未完成」——并声明该页登记相对基线 commit 的有效判分变更：[docs/miles/grading.md:L1-L4](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/grading.md#L1-L4)。

弃答 QA 文档（经 source-preparation 链接接入）：`nemo_abstention_v1` 源适配器保留原始 system/user 消息并输出结构化 question/reference 目标，训练需命名裁判：[docs/miles/abstention.md:L1-L4](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/abstention.md#L1-L4)；接入点在 [docs/miles/source-preparation.md:L578](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/source-preparation.md#L578)。

Small 配方文档首页声明三个配置（two-node-mech / four-node / eight-node）、当前配方使用 PR #2271 的 Tiny batch/LR/loss 基线而历史测量用旧设置：[docs/miles/olmo35-small.md:L1-L10](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/olmo35-small.md#L1-L10)。**读时注意**：L9-L10 的「without checkpoints」描述的是历史计时运行；本轮 #2289 之后 four/eight-node 已启用有界恢复保存，权威表述在文末两处——资格检查段的改写（[docs/miles/olmo35-small.md:L217-L219](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/olmo35-small.md#L217-L219)：历史计时运行关闭保存，当前 four/eight-node 起步模板启用有界恢复保存）与新增的「Checkpoint settings in the current starters」一节（[docs/miles/olmo35-small.md:L233-L244](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/olmo35-small.md#L233-L244)：30 更新预算在 10/20/30 保存、50 里程碑为扩展预留、two-node-mech 仍禁用、历史计时不含保存开销）。这是一个典型的「同一文档内旧措辞与新章节并存」的例子：**应以更具体、更晚写入的章节为准**。

本轮 #2286 在裁判文档登记的规则：绑定表可以覆盖比所选数据子集更多的源，未使用绑定被允许，但被引用的裁判服务与 rubric 仍须有效：[docs/miles/managed-judges.md:L64-L66](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/managed-judges.md#L64-L66)。

本轮 #2289 改写的 development-defaults 条目：Native checkpoints 从「每 25 更新」改为「学习起步模板每 10 更新，按实测墙钟时间瞄准约 1-2 小时一次临时恢复保存；保留最近两份、按模板指定的 50/100 更新永久里程碑与最终存档；检查首个完整保存是否从容落在受保护运行时内；验证真实保存、有界保留与恢复后的优化器/游标一致性——自动重启本身不等于恢复」：[docs/miles/development-defaults.md:L43](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/development-defaults.md#L43)。

站点导航的接入点：`mkdocs.yml` 的 "Models and training" 组含 "OLMo 3.5 Small on B300" 条目（L110）：[mkdocs.yml:L107-L117](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/mkdocs.yml#L107-L117)；grading.md 仍在 "Data and evaluation" 组（L121）：[mkdocs.yml:L118-L125](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/mkdocs.yml#L118-L125)。

#### 4.3.4 代码实践

**实践目标**：走通文档地图的两条检索路径，定位近几轮新增的文档及其接入点，并用只读 git 命令核对两轮文档与示例的联动。

**操作步骤**：

1. 打开 [docs/miles/index.md](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/index.md)，通读 "Start here" 与 "Documentation map" 表，确认表中没有 grading / abstention / olmo35-small 的行。
2. 在仓库内全局检索三页的引用与导航接入：

   ```bash
   grep -rn "grading.md" docs/miles/*.md
   grep -rn "abstention.md" docs/miles/*.md
   grep -n "olmo35-small" mkdocs.yml docs/miles/*.md
   ```

3. 打开 [docs/miles/grpo.md](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/grpo.md) 的 "Truncated responses" 一节（L88-L107），记下 token-mean 基线的两个显式设置；再对照 [docs/miles/development-defaults.md](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/development-defaults.md) 的 "Loss weighting"（L23）、"Tiny learning batch and LR"（L18-L21）与改写后的 "Native checkpoints"（L43）条目。
4. 打开 [mkdocs.yml](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/mkdocs.yml) 的 `/miles/` 导航段，确认 olmo35-small.md（L110）与 grading.md（L121）在导航中的位置；可用 `uv run mkdocs serve`（u1-l2 讲过）在本地浏览。
5. 分别用上一轮与本轮的 diff 窗口核对文档与示例的联动：

   ```bash
   # 上一轮（token-mean 翻转 + Small 配方落地）
   git diff --stat 28c1816664491493242d453c2c7280ee56e6c20a..2f7889da9248629f9415a80a45ba882544b82f1f -- docs/miles/ configs/miles/
   # 本轮（有界恢复保存 + 心跳超时 + pin 升级）
   git diff --stat 2f7889da9248629f9415a80a45ba882544b82f1f..2063f854b37cd380e55f5509df7ea50b6d8a1033 -- docs/miles/ configs/miles/ runtime/miles/
   ```

**需要观察的现象**：步骤 2 中 grading.md 的引用落在 grpo.md（判分语义两处），abstention.md 的引用落在 source-preparation.md，olmo35-small 出现在 mkdocs.yml 导航与 examples README 中；步骤 5 的第一个窗口显示新增 `docs/miles/olmo35-small.md` 与 `configs/miles/moe_configs/`、删除 `small/dev.toml`、新增 small/ 三份配方及 token-mean 措辞翻转；第二个窗口显示 `configs/miles/examples/README.md`（约 +43 行，恢复策略节）、`docs/miles/olmo35-small.md`（约 +16 行，checkpoint 一节）、tiny 两份学习模板（各 +6 行）、small four/eight-node（各 +14 行）与 `runtime/miles/runtime.lock.json` 的 revision 前进。

**预期结果**：得出结论——新增文档通过「上下文链接 + mkdocs 导航」接入，首页表格是精选而非穷举；本轮的文档变化（README 恢复策略节、olmo35-small 的 checkpoint 一节、development-defaults 的 Native checkpoints 条目）与示例/代码变化（save_interval/checkpoint_keep_*/save_final_checkpoint/auto_resume、心跳 120→480、pin 升级）是同批联动的。步骤 1-5 均为只读操作；步骤 4 的本地站点为可选步骤，效果**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：想在本地修改 `docs/miles/configuration.md` 中的一个字段默认值，直接编辑该文件对吗？

**参考答案**：不对。`configuration.md` 等生成式参考页由 MkDocs 从配置源在内存中生成、不入库；应编辑源定义与审校描述，再用 `python -m scripts.miles.generate_docs` 重新生成、`--check` 校验。见 [docs/miles/index.md:L55-L63](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/index.md#L55-L63)。

**练习 2**：`docs/miles/olmo35-small.md` 为什么不加入 `docs/miles/index.md` 的文档地图表？它经由什么路径被读者找到？

**参考答案**：首页表格是按「什么时候用」组织的精选主表，面向通用工作流；olmo35-small.md 是特定模型（OLMo 3.5 Small）+ 特定硬件（B300）的实测记录，读者群更窄。它经由 mkdocs 站点导航的 "Models and training" 组接入（[mkdocs.yml:L110](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/mkdocs.yml#L110)），并被 `configs/miles/examples/README.md` 与 small 配方文件的注释引用。检索文档时不能只看首页表。

**练习 3**：`core.reward_final_answer_only` 的库默认值与维护示例值分别是什么？为什么示例要显式写出？

**参考答案**：库默认 `false`（保留历史判分行为），维护示例显式设 `true`（思考型策略只判交付响应）。示例是配置基线——除非 run 明确声明，应以示例为准，显式写出避免「沉默改变目标」。见 [docs/miles/grading.md:L7-L12](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/grading.md#L7-L12) 与 [configs/miles/examples/tiny/dev.toml:L71-L77](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/tiny/dev.toml#L71-L77)。

### 4.4 示例家族划分：tiny 与 small

#### 4.4.1 概念说明

维护示例（maintained examples）是 MILES 的「配置基线」：除非 run 明确声明，应以示例为准。示例按**模型家族**重组为 `tiny/` 与 `small/` 两个目录，近两轮这两个家族的分工与基线发生了两次实质变化：

- **tiny 家族**：微型模型的力学/学习示例——`dev.toml`（单 GPU 共置力学检查）、`node.toml`（单节点学习）、`three-node.toml`（三节点学习 + 后台评估）。上一轮统一了 Tiny 学习基线：256 prompts × 8 responses、batch 2048、恒定 LR 6e-6 无 warmup、token-mean loss；本轮 #2289 又统一了恢复保存基线：每 10 更新保存、保留最近 2 份、node 按 50 更新留里程碑（与既有的 50 更新开发面板对齐）、three-node 按 100 更新留里程碑，外加最终存档。
- **small 家族**：上一轮删除 `small/dev.toml` 占位，换成**实测的 OLMo 3.5 Small（A4B，KDA + 256 专家 MoE）B300 三档配方**：`two-node-mech.toml`（两节点力学检查，4 次更新，仍不保存 checkpoint）、`four-node.toml` 与 `eight-node.toml`（30 更新起步预算布局，本轮从「禁用保存、不自动恢复」翻转为启用与 Tiny 相同的有界恢复保存 + `auto_resume = true`）。

同时，**全部维护示例的损失基线从 per-response 翻转为 token-mean**：`miles.calculate_per_token_loss = true`。注意省略该设置时库默认仍是 `false`（response-mean），这是为兼容既有 run 而刻意保留的——所以每个示例都把它显式写出。

为什么按模型家族分目录？因为 serving 几何、显存账目、准入参数都是模型专属的：Small 文档明确告诫「不要把 Tiny 的 12.5B 推理 preset、评估 profile 或缓存几何复制进 Small」。

本轮还有一个容易被忽略的运维细节：#2293 把三份 Small 配方的 `heartbeat_timeout` 从 120 秒升到 480 秒——启用恢复保存与自动重启后，保存/恢复期间驱动可能长时间不发包，过紧的心跳会被误判为僵死（恢复语义详见 u8-l3）。

#### 4.4.2 核心流程

当前六份维护示例的分工（与 README 表格一致，最后一列是本轮 #2289 固化的恢复保存基线）：

| 配置 | 家族 | 用途 | 恢复保存基线 |
|---|---|---|---|
| `tiny/dev.toml` | tiny | 单 GPU、训练/推理共置；GSM8K 短力学检查 | 独立短跑设置（保留原样） |
| `tiny/node.toml` | tiny | 单个 8 GPU 节点：2 训练 GPU（EP2）+ 6 推理引擎；无裁判 | 每 10 更新；保留 2 份 + 50 更新里程碑 + 最终存档 |
| `tiny/three-node.toml` | tiny | 三节点学习：8 训练 GPU（EP8）、15 推理引擎 + 1 裁判；24 GPU，另加后台评估 | 每 10 更新；保留 2 份 + 100 更新里程碑 + 最终存档 |
| `small/two-node-mech.toml` | small | OLMo 3.5 Small 力学检查：8 训练 GPU（EP8）、7 引擎、1 裁判；4 次更新 | 不保存、不自动恢复（保持不变） |
| `small/four-node.toml` | small | Small 起步预算布局：8 训练 GPU（EP8）、23 引擎（R=96）、1 裁判；审计中位数约 302 s/更新 | 每 10 更新；保留 2 份 + 50 里程碑 + 最终存档；`auto_resume = true` |
| `small/eight-node.toml` | small | Small 起步预算布局：16 训练 GPU（2 × EP8）、47 引擎（R=96）、1 裁判；晚期窗口中位数约 185 s/更新 | 同 four-node |

近两轮基线变化的要点：

```text
损失加权（上一轮 #2271）：
            per-response（response-mean）      →  token-mean（calculate_per_token_loss = true）
           （省略该设置时库默认仍为 response-mean，兼容既有 run）

Tiny 学习（上一轮 #2271）：
            128 prompts × 8，batch 1024        →  256 prompts × 8，batch 2048
学习率：    （模板未显式设置）                  →  learning_rate = 6e-6，恒定、无 warmup

small 家族（上一轮 #2280）：
            small/dev.toml（tiny dev 的占位副本）→  三份实测 B300 配方 + docs/miles/olmo35-small.md
                                                +  configs/miles/moe_configs/（fused-MoE tile 调优）

学习示例恢复保存（本轮 #2289）：
            Tiny 学习模板 save_interval = 25    →  save_interval = 10 + checkpoint_keep_last = 2
                                                    + checkpoint_keep_every（node/Small=50，three-node=100）
                                                    + save_final_checkpoint = true
            Small four/eight-node：              →  启用上述保存基线 + auto_resume = true
            save_checkpoints = false、auto_resume = false

心跳超时（本轮 #2293）：
            Small 三份配方 heartbeat_timeout     →  480 秒（原 120 秒）
```

两个容易踩的陷阱：

1. **历史上的 Small 吞吐计时用的是旧设置**（batch 1024、LR 1e-6、response-mean，且未启用 code grading），且全部是**关闭保存**的运行——不含保存 I/O 开销。新基线（含每 10 更新的保存）不可直接沿用旧数字。
2. **里程碑间隔是「为扩展预留」的**：30 更新起步预算下，50/100 里程碑不会在本次运行内触发；它们只在把 `num_rollouts` 扩大后才生效。README 因此提醒：无 checkpoint 的计时实验要显式关掉保存与自动恢复并删掉 `save_interval`，且不要把该覆盖带进学习 run。

#### 4.4.3 源码精读

示例 README 开头声明家族划分：`small` 家族持有实测的 OLMo 3.5 Small（A4B）B300 布局并链接配方文档：[configs/miles/examples/README.md:L1-L6](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/README.md#L1-L6)。

六份配置的用途表：[configs/miles/examples/README.md:L8-L15](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/README.md#L8-L15)。紧接着的两段给出选择规则与基线对齐告诫：力学检查用 `tiny/dev.toml`（Tiny）或 `small/two-node-mech.toml`（Small），**Small 没有单 GPU 训练配方、两节点是其最小已测配置**（L17-L19）；三份 Small 配方统一采用 PR #2271 的 Tiny 学习基线（256 × 8、batch 2048、恒定 LR 6e-6、token-mean），而报告的 Small 计时用的是 batch 1024 / LR 1e-6 / response-mean（L21-L25）。

基线声明段：除非明确声明否则以示例为准，尤其保持 `miles.calculate_per_token_loss=true`、`core.reward_zero_truncated=true`、`core.reward_final_answer_only=true` 与 `core.reward_reasoning_mode="required"`：[configs/miles/examples/README.md:L31-L42](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/README.md#L31-L42)。

**本轮新增的共享恢复策略节**是理解 checkpoint 基线的入口：开头一段总括「Tiny 学习与 Small four/eight-node 模板启用每 10 更新的原生恢复保存、保留最近两份、永久里程碑 + 最终存档、启用自动恢复；短力学示例保持各自的独立设置」：[configs/miles/examples/README.md:L328-L333](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/README.md#L328-L333)。随后是一张四列表，逐模板给出恢复间隔、里程碑间隔与理由（node 的 50 对齐既有开发面板、three-node 的 100 保留更长学习里程碑、Small 的 50 为扩展 30 更新预算预留）：[configs/miles/examples/README.md:L335-L339](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/README.md#L335-L339)。间隔选择指南在 L341-L348：按实测墙钟更新时间选间隔、目标约 1-2 小时一次保存（Tiny seed17 数据在 batch 2048 下约 5-7 分钟/更新，即每 10 更新约 50-70 分钟）、这是工作负载特定的证据而非普适吞吐声明、Small 历史计时不含保存 I/O。启动前预算与恢复验证清单在 L350-L364：分开预算启动/编译/生成等待/训练/评估/保存时间，**确保首个完整恢复保存从容落在受保护最短运行时之内**；恢复后核对完成清单、最新指针与数据游标哈希、首个恢复后优化器计数与游标一致性；「自动任务重启本身不证明模型/优化器恢复」。

tiny dev 模板近两轮唯一的变化是上一轮在 `[miles]` 段加入 `calculate_per_token_loss = true`（其上一行注释说明共置冒烟测试保留 retract 模式）：[configs/miles/examples/tiny/dev.toml:L84-L87](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/tiny/dev.toml#L84-L87)。其 `[core]` 段的判分基线（`reward_final_answer_only = true`、`reward_reasoning_mode = "required"`、`reward_zero_truncated = true`，带注释）未动：[configs/miles/examples/tiny/dev.toml:L71-L82](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/tiny/dev.toml#L71-L82)。

Tiny 学习示例的基线三件套 + 本轮新增的恢复保存（以 `node.toml` 为例）：`[training]` 段 `learning_rate = 6e-6`、`num_rollouts = 200`，本轮把 `save_interval` 从 25 改为 10 并附注释「恢复目标约 1-2 小时；启动前重查墙钟时间与首次保存成本」：[configs/miles/examples/tiny/node.toml:L46-L51](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/tiny/node.toml#L46-L51)。`[inference]` 段的批次几何 `rollout_batch_size = 256`、`samples_per_prompt = 8`、`global_batch_size = 2048`：[configs/miles/examples/tiny/node.toml:L94-L97](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/tiny/node.toml#L94-L97)。异步段 `calculate_per_token_loss = true`：[configs/miles/examples/tiny/node.toml:L137-L138](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/tiny/node.toml#L137-L138)。`[core]` 段本轮新增的保存三元组（注释「保留最近两份原生保存 + 永久里程碑 + 最终保存」，`checkpoint_keep_last = 2`、`checkpoint_keep_every = 50`、`save_final_checkpoint = true`）：[configs/miles/examples/tiny/node.toml:L118-L121](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/tiny/node.toml#L118-L121)。`three-node.toml` 同步了同一组值，仅里程碑为 100：训练段 [configs/miles/examples/tiny/three-node.toml:L62-L66](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/tiny/three-node.toml#L62-L66)、保存三元组 [configs/miles/examples/tiny/three-node.toml:L264-L267](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/tiny/three-node.toml#L264-L267)、批次几何 L241-L243 与 token-mean L279。

Small 配方文档的布局与显存表（三档配方的节点数、训练 GPU 划分、引擎数、裁判）：[docs/miles/olmo35-small.md:L34-L47](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/olmo35-small.md#L34-L47)；配方对齐表（当前 Small 基线 vs 历史测量的五项差异：256×8 vs 128×8、2048 vs 1024、6e-6 vs 1e-6、token-mean vs response-mean、std 归一化均关）：[docs/miles/olmo35-small.md:L134-L150](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/olmo35-small.md#L134-L150)。不要把 Tiny 的模型专属设置复制进 Small 的告诫在 L159-L161。本轮新增的「Checkpoint settings in the current starters」一节把 four/eight-node 的新基线串成一段：30 更新预算在 10/20/30 保存、50 里程碑为扩展预留、two-node-mech 仍禁用、历史计时不含保存开销也不验证恢复正确性、启动前实测 batch 2048 墙钟与保存成本：[docs/miles/olmo35-small.md:L233-L244](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/olmo35-small.md#L233-L244)。

Small 配方自身的文件头就是一份摘要（以 two-node-mech 为例）：前 15 行注释写明布局、用途、基线匹配 Tiny、历史力学运行用旧设置、起始 checkpoint 与关键参数，并指向 olmo35-small.md：[configs/miles/examples/small/two-node-mech.toml:L1-L15](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/small/two-node-mech.toml#L1-L15)。它的 `[training]` 段保持力学检查定位：`num_rollouts = 4`、`save_checkpoints = false`：[configs/miles/examples/small/two-node-mech.toml:L82-L87](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/small/two-node-mech.toml#L82-L87)。三份 Small 配方同样显式设置 `learning_rate = 6e-6`、`rollout_batch_size = 256`、`global_batch_size = 2048`、`calculate_per_token_loss = true`（two-node-mech 的 L83、L99-L101、L149）。

four-node 的本轮翻转最完整地记录在文件里：`[launch]` 段把 `auto_resume` 从 `false` 翻为 `true`，预算注释改为「历史预算基于旧 batch 且无保存；重测当前墙钟与保存成本」：[configs/miles/examples/small/four-node.toml:L44-L47](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/small/four-node.toml#L44-L47)；`[launch.coordination]` 心跳 480：[configs/miles/examples/small/four-node.toml:L52-L54](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/small/four-node.toml#L52-L54)；`[training]` 段删掉 `save_checkpoints = false` 换成 `save_interval = 10`（注释「短起步预算；仅在测量运行时、保存成本与恢复后再扩展」）：[configs/miles/examples/small/four-node.toml:L96-L103](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/small/four-node.toml#L96-L103)；`[core]` 段的保存三元组（keep 2 / every 50 / final）：[configs/miles/examples/small/four-node.toml:L149-L152](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/small/four-node.toml#L149-L152)。eight-node 对应 L40-L43（auto_resume）、L48-L50（心跳）、L95-L98（save_interval）、L146-L149（保存三元组）；two-node-mech 只有心跳一处变化：[configs/miles/examples/small/two-node-mech.toml:L41-L42](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/small/two-node-mech.toml#L41-L42)。

配套的 fused-MoE tile 调优目录：镜像把 `configs/miles` 拷贝到 `/opt/core-rl/configs/miles`，Small 示例把 `SGLANG_MOE_CONFIG_DIR` 指向其中的 `moe_configs`；目录名必须匹配镜像的 Triton 版本，`_down` 文件缺失时复用 tile：[configs/miles/moe_configs/README.md:L15-L22](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/moe_configs/README.md#L15-L22)（实测收益与失效条件详见 u8-l15）。

「former topology tier named small is retired」的澄清段：已退役的是旧的 small **拓扑层级**，与上面的 Olmo 3.5 Small **模型配方**是两回事：[configs/miles/examples/README.md:L130-L133](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/README.md#L130-L133)。

#### 4.4.4 代码实践

**实践目标**：通过只读命令核对两个家族的分工、token-mean 基线与本轮恢复保存基线，理解「示例即基线」。

**操作步骤**：

1. 列出两个家族的文件并确认占位已删除：

   ```bash
   ls configs/miles/examples/tiny configs/miles/examples/small
   git log --oneline --diff-filter=D -- configs/miles/examples/small/dev.toml
   ```

2. 全局核对 token-mean 基线与恢复保存基线：

   ```bash
   grep -n "calculate_per_token_loss" configs/miles/examples/*/*.toml
   grep -n "save_interval\|save_checkpoints\|checkpoint_keep_every\|save_final_checkpoint\|auto_resume" \
     configs/miles/examples/*/*.toml
   grep -n "heartbeat_timeout" configs/miles/examples/small/*.toml
   ```

3. 对照 Tiny 学习基线与 Small 的共享值（以 node 与 two-node-mech 为例）：

   ```bash
   grep -n "learning_rate\|rollout_batch_size\|global_batch_size" \
     configs/miles/examples/tiny/node.toml configs/miles/examples/small/two-node-mech.toml
   ```

4. 阅读 [configs/miles/examples/README.md](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/README.md) 的家族表（L8-L19）、恢复策略节（L328-L364）与 [docs/miles/olmo35-small.md](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/olmo35-small.md) 的 Recipe alignment（L134-L150）与 Checkpoint settings（L233-L244）两节，用自己的话写四行：tiny 家族管什么、small 家族管什么、为什么 Small 计时不能直接沿用旧数字、为什么首个完整恢复保存必须落在受保护最短运行时之内。
5. 用本轮 diff 核对翻转轨迹：

   ```bash
   git diff 2f7889da9248629f9415a80a45ba882544b82f1f..2063f854b37cd380e55f5509df7ea50b6d8a1033 \
     -- configs/miles/examples/small/four-node.toml
   ```

6. （可选，CPU 即可）按 olmo35-small.md 的资格检查流程把 two-node-mech 复制到被忽略的 `runs/` 后做规划：

   ```bash
   mkdir -p runs && cp configs/miles/examples/small/two-node-mech.toml runs/small-mech.toml
   uv run python -m open_instruct.miles plan runs/small-mech.toml
   ```

**需要观察的现象**：步骤 1 中 small/ 下只有三份 B300 配方，`--diff-filter=D` 显示 `small/dev.toml` 在 92776a730（PR #2280）被删除；步骤 2 中六份示例全部 `calculate_per_token_loss = true`；`save_interval = 10` 出现在 tiny/node、tiny/three-node、small/four-node、small/eight-node 四份，`checkpoint_keep_every` 为 50/100/50/50，`auto_resume = true` 覆盖全部 Tiny 模板与 Small four/eight-node，而 two-node-mech 仍是 `save_checkpoints = false` + `auto_resume = false`；三份 Small 配方 `heartbeat_timeout` 均为 480；步骤 3 中 tiny/node 与 small/two-node-mech 的 `learning_rate`/`rollout_batch_size`/`global_batch_size` 完全一致（6e-6 / 256 / 2048）；步骤 5 的 diff 显示 four-node 的三组翻转（auto_resume、save_checkpoints→save_interval、新增保存三元组）与心跳 120→480。

**预期结果**：能复述两个家族的分工（Tiny 力学/学习示例 vs Small B300 实测配方）、共享的学习基线与各自 checkpoint 策略的差异；步骤 1-5 为只读操作，可安全执行；步骤 6 的 `plan` 输出（含占位路径的 advisory）**待本地验证**——文档承诺规划无需 GPU，但本讲未实际运行。

#### 4.4.5 小练习与答案

**练习 1**：`tiny/` 与 `small/` 两个家族各自的分工是什么？为什么 Small 没有单 GPU 训练配方？

**参考答案**：`tiny/` 是 Tiny 微型模型的力学检查（dev）与学习示例（node/three-node）；`small/` 是实测的 OLMo 3.5 Small（A4B）B300 布局：two-node-mech 做力学检查，four/eight-node 做起步预算布局。Small 没有单 GPU 训练配方、两节点（8 训练 GPU + 7 引擎 + 1 裁判）是其最小已测配置——见 [configs/miles/examples/README.md:L17-L19](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/README.md#L17-L19)。

**练习 2**：示例都写了 `calculate_per_token_loss = true`，为什么库默认仍是 `false`？这两者矛盾吗？

**参考答案**：不矛盾。库默认保留 response-mean 是为了兼容既有 run——省略该设置的老配置行为不变；维护示例显式设 `true` 是把新基线写清楚，避免「沉默改变目标」。development-defaults 同样写明「Omitted library settings retain response-mean weighting for compatibility」（Loss weighting 条目，其上一条即 Tiny learning batch and LR）：[docs/miles/development-defaults.md:L18-L23](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/development-defaults.md#L18-L23)。

**练习 3**：Small four/eight-node 的 30 更新预算下，保存点落在哪里？50 更新里程碑有什么用？为什么 README 要求「首个完整恢复保存应落在受保护最短运行时之内」？

**参考答案**：`save_interval = 10` 且启用最终存档，30 更新预算在 10/20/30 三处保存；`checkpoint_keep_every = 50` 在本次起步预算内不会触发，它是为把 `num_rollouts` 扩大后的长运行预留的永久里程碑（见 [docs/miles/olmo35-small.md:L235-L238](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/olmo35-small.md#L235-L238)）。受保护最短运行时是调度器保证的连续执行窗口：若首个完整保存来不及在窗口内落盘，一次抢占就会让整个 run 从零开始，恢复保存形同虚设；因此间隔要从实测墙钟时间选（目标约 1-2 小时一次，Tiny seed17 约 5-7 分钟/更新即约 50-70 分钟/10 更新），放不进就缩短间隔或修改预算（[configs/miles/examples/README.md:L341-L356](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/README.md#L341-L356)）。另注意历史 Small 计时是关闭保存的运行，不含保存 I/O 开销，不能直接当作当前配方的 checkpoint 预测。

## 5. 综合实践

**任务：为你的第一次 MILES 冒烟运行制作一页「所有权笔记」，并在 tiny 与 small 两个家族之间做出正确选择。**

1. **复制模板**：把 [configs/miles/examples/tiny/dev.toml](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/tiny/dev.toml) 复制到被 git 忽略的 `runs/my-dev.toml`（目录不存在则创建；不要直接改模板——配置约定见 [docs/miles/grpo.md:L42-L49](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/grpo.md#L42-L49)）。
2. **改占位符**：替换 `YOUR_USERNAME` 与模型/输出路径（README 的准备步骤见 [configs/miles/examples/README.md:L55-L70](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/README.md#L55-L70)）。
3. **无 GPU 规划**：运行 `uv run python -m open_instruct.miles plan runs/my-dev.toml`，把输出 JSON 里你认识的段落（model/data/training/inference/core）与 4.2 节架构图逐一连线。
4. **家族对照**：并排打开 `tiny/dev.toml`、`tiny/node.toml` 与 `small/two-node-mech.toml`、`small/four-node.toml` 的文件头与 `[launch]`/`[training]`/`[core]` 段，记录四类差异：模型与 checkpoint、拓扑规模、共享的 6e-6/2048/token-mean 学习基线、**各自的恢复保存策略**（dev 独立短跑设置、node 每 10 更新 + 50 里程碑、two-node-mech 不保存、four-node 每 10 更新 + 50 里程碑 + auto_resume + 心跳 480），并回答：如果只是想验证「MILES 在我的环境能跑通」，该选哪份？如果想测 OLMo 3.5 Small 在 B300 上的吞吐呢？如果要把 Small 跑成一次可中断恢复的学习 run 呢？（依次：tiny/dev；先 two-node-mech 力学检查再 four/eight-node 计时；four/eight-node 起步并在实测墙钟后扩展 `num_rollouts`，依赖其已启用的有界恢复保存。）
5. **写一页笔记**，回答三个问题：
   - 这个 run 里哪些工作由 open-instruct 包装层完成（配置校验、数据准备、GSM8K 验证器、提交回执）？
   - 哪些由 MILES 运行时/Core 适配器完成（SGLang rollout、OLMo-core 训练、权重发布、checkpoint 保存与恢复）？
   - `[core]` 段的 `reward_final_answer_only`/`reward_reasoning_mode`（[configs/miles/examples/tiny/dev.toml:L71-L77](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/tiny/dev.toml#L71-L77)）、`[miles]` 段的 `calculate_per_token_loss`（[configs/miles/examples/tiny/dev.toml:L84-L87](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/tiny/dev.toml#L84-L87)）与 `[core]` 段的 `checkpoint_keep_*`/`save_final_checkpoint`（[configs/miles/examples/tiny/node.toml:L118-L121](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/tiny/node.toml#L118-L121)）最终影响的是哪一层的哪个环节？（提示：判分输入选择见 [docs/miles/grading.md](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/grading.md)；损失加权语义见 [docs/miles/grpo.md:L88-L107](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/grpo.md#L88-L107)；checkpoint 保留语义见 [docs/miles/models-and-checkpoints.md](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/models-and-checkpoints.md)。）
6. **验证笔记**：用只读命令抽查你的连线，例如 `ls open_instruct/miles/rewards/`（验证器确实在包装层）、`grep -n "runtime.lock.json" docs/miles/architecture.md`（运行时锁的权威位置）、`grep -n "save_interval\|checkpoint_keep_every" configs/miles/examples/*/*.toml`（恢复保存基线的落点）。

预期产物：一张三层架构图 + 一页所有权笔记 + 一份 `plan` 输出的对照标注 + 四行家族对照。`plan` 的具体输出本讲未实际执行，**待本地验证**；若 `plan` 因占位路径报提示，记录提示内容并继续——规划本身不需要那些挂载存在。

## 6. 本讲小结

- **入口唯一**：`python -m open_instruct.miles` 暴露五个子命令 `plan/validate/train/run/status`（另有 `records` 记录分析）；运行时导入延迟到需要的命令，`plan`/`validate` 无 GPU、无 MILES 运行时即可运行。
- **结构化双轨**：含 `schema_version` 或 `model` 的 TOML 编译为 RunSpec（`run`/`status` 只接受这种）；低层 RunConfig 仅供 `plan`/`validate`/`train`。
- **三层所有权**：open-instruct 包装层管配置/数据/奖励/提交；MILES 运行时管 rollout 编排与共享 RL 机制；Core 适配器经 OLMo-core 训练、SGLang 服务。后端不导入 open-instruct，open-instruct 以回调与可信注册表注入能力。
- **不弃用旧路线**：`grpo.py` 与 `grpo_fast.py` 仍是稠密模型与历史复现的现行选择；MILES 面向受支持的 Olmo MoE 模型。
- **文档地图持续扩展**：`grading.md`（共享判分输入）、`abstention.md`（Nemotron 弃答 QA）、`olmo35-small.md`（B300 实测配方文档，入 mkdocs 导航）、`managed-judges.md` 的未使用绑定规则、grpo/development-defaults/run-controls/throughput-profiles 的 token-mean 措辞；本轮再添 examples README 的「Recovery checkpoints and preserved milestones」节、olmo35-small 的「Checkpoint settings in the current starters」节与 development-defaults 的 Native checkpoints 改写。首页表格是精选而非穷举，且同一文档内应以更晚、更具体的章节为准。
- **示例家族与基线**：`small/dev.toml` 占位已删除，small 家族是实测 OLMo 3.5 Small B300 三档配方（two-node-mech/four-node/eight-node，配套 moe_configs tile 调优）；全部维护示例统一 token-mean loss，Tiny 学习基线为 256 prompts × 8、batch 2048、恒定 LR 6e-6。本轮 #2289 为学习型模板固化有界恢复保存（每 10 更新、保留 2 份、50/100 里程碑、最终存档，Small four/eight-node 翻转为启用 + `auto_resume=true`），#2293 把 Small 心跳升至 480 秒并推进 MILES 运行时 pin（9eff46a4）。配置字段细节见 u8-l2，恢复语义见 u8-l3，B300 深读见 u8-l15。

## 7. 下一步学习建议

下一讲 **u8-l2（配置系统：TOML → RunSpec → RunConfig）** 将拆开本讲只碰了一下的编译链：`configuration/run_spec.py` 的加载与编译、CoreConfig/RunConfig 如何编译出参数列表并挂接奖励/数据钩子、constraints 驱动的校验，以及示例基线（`calculate_per_token_loss=true` 与本轮的 `save_interval`/`checkpoint_keep_last`/`checkpoint_keep_every`/`save_final_checkpoint` 字段族）在配置层的含义。

在进入下一讲之前，建议先做两件源码阅读：

1. 浏览 `open_instruct/miles/configuration/` 目录（`ls` 即可），对照 [docs/miles/architecture.md:L276-L291](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/architecture.md#L276-L291) 的职责表建立印象。
2. 通读 [docs/miles/workflow.md](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/workflow.md)（配合 u8-l3），了解一次 run 从准备、训练到导出的阶段划分与本轮 #2293 的恢复/心跳变化，为后续所有 MILES 讲义提供骨架。

若想直接深入 Small B300 配方的显存账目、R=96 准入联动、吞吐审计与 checkpoint 起步预算，可跳读 **u8-l15**（依赖 u8-l2 与 u8-l10）；若想横向对比两条 RL 路线的架构差异，可回看 u6-l4（`grpo_fast.py` 的 Ray 拓扑）。
