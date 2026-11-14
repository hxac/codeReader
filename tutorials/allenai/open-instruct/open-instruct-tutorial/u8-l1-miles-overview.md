# u8-l1 MILES 总览与架构分层

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 MILES 与 `grpo.py`（Core/vLLM）、`grpo_fast.py`（DeepSpeed/vLLM）三条 RL 训练路线的关系与各自适用范围。
2. 理解 MILES 的所有权分层：open-instruct 包装层负责什么、MILES 运行时负责什么、Core 适配器经 OLMo-core 与 SGLang 又负责什么。
3. 会用 `python -m open_instruct.miles` 的五个子命令（`plan` / `validate` / `train` / `run` / `status`）以及 `records` 记录分析工具。
4. 知道维护示例已按模型家族重组：`configs/miles/examples/tiny/` 与 `configs/miles/examples/small/`，并了解本轮文档地图的扩展（`grading.md`、`abstention.md`）。

本讲是 MILES 单元（u8）的第一篇，只做总览不深入细节；配置系统、工作流执行、奖励体系分别在 u8-l2、u8-l3、u8-l6 及之后各讲展开。

## 2. 前置知识

阅读本讲前，建议先具备以下概念（不熟悉也没关系，下面用通俗语言解释）：

- **RLVR / GRPO**：可验证奖励强化学习（RL with Verifiable Rewards）。对数学题、代码这类有标准答案的任务，可以用一个「验证器」自动判分作为奖励，而不需要训练额外的奖励模型。GRPO 是一种组相对策略优化算法：对同一个 prompt 采样一组回答，按组内奖励的相对高低计算优势。已在 u6-l1、u6-l2 讲过。
- **rollout（ rollout 编排）**：RL 训练的一步是「让当前策略生成一批回答 → 判分 → 用结果更新策略」。生成这批回答的过程及其调度（队列、准入、引擎管理）称为 rollout 编排。
- **Ray actor**：Ray 分布式框架中的常驻进程，持有 GPU 资源并响应远程调用。MILES 运行时用 Ray actor 承载训练器与推理引擎。
- **OLMo-core 与 SGLang**：OLMo-core 是 Allen AI 的模型与分布式训练库（提供模型结构、checkpoint、分布式原语）；SGLang 是推理服务框架。MILES 用前者训练、后者服务。
- **TOML run 文件**：MILES 用一个结构化 TOML 文件描述一次完整运行（模型、数据、拓扑、启动、追踪等），是所有子命令的输入。
- **Beaker**：AI2 的实验平台，MILES 的 `run` 子命令把 TOML 编译成 Beaker 实验提交。不熟悉 Beaker 也不影响本讲，详见 u8-l8。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [docs/miles/index.md](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/docs/miles/index.md) | MILES 文档首页：定位声明、入门顺序、文档地图表、权威性与维护规则 |
| [docs/miles/architecture.md](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/docs/miles/architecture.md) | 架构与开发：分层所有权表、运行时锁与镜像、启动依赖、本地开发检查、适配器包布局 |
| [open_instruct/miles/__main__.py](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/miles/__main__.py) | CLI 入口：`python -m open_instruct.miles`，加载 run 文件并分发 plan/validate/train/run/status 与 records |
| [docs/miles/grpo.md](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/docs/miles/grpo.md) | MILES GRPO 指南：首次运行步骤、配置约定、判分/截断/未判分语义、发布模式 |
| [configs/miles/examples/README.md](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/configs/miles/examples/README.md) | 维护示例的导读：按模型家族分组（tiny/small），列出各模板用途与基线设置 |
| [configs/miles/examples/tiny/dev.toml](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/configs/miles/examples/tiny/dev.toml) | 单 GPU 共置（colocated）微型模型力学检查模板，本讲实践对象 |
| [docs/miles/grading.md](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/docs/miles/grading.md) | 本轮新增：共享判分输入（ResponseParts、objective/scope/require_response 契约）与行为变更登记 |
| [docs/miles/abstention.md](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/docs/miles/abstention.md) | 本轮新增：Nemotron 弃答式 QA 奖励的语义、rubric 与裁判服务复用 |

## 4. 核心概念与源码讲解

### 4.1 CLI 入口：`python -m open_instruct.miles`

#### 4.1.1 概念说明

MILES 在 open-instruct 中的公开入口只有一条命令：`python -m open_instruct.miles`。它把「规划、校验、本地执行、Beaker 提交、状态查询」以及「推理记录分析」全部收拢到一个入口后面。

为什么这样设计？

- **研究者的全部意图浓缩在一个 TOML run 文件里**。同一条命令对不同子命令复用同一份配置，避免「训练用一套脚本、提交用另一套脚本」的漂移。
- **运行时导入是延迟的**。GPU/MILES 运行时只在真正需要的子命令里才 import，因此 `plan` / `validate` 这类规划命令在没有 GPU、没有安装 MILES 私有运行时的笔记本电脑上也能运行。这一点是文档明确承诺的：[docs/miles/index.md:L21-L23](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/docs/miles/index.md#L21-L23) 写明 "Planning does not require GPUs or mounted checkpoints"。

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

模块 docstring 即是命令速查，明确写出五个子命令与 records 的用法：[open_instruct/miles/__main__.py:L1-L12](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/miles/__main__.py#L1-L12)。其中 L4-L5 说明「运行时导入延迟到需要它的命令，规划无需 GPU 栈」这一关键约束。

`main()` 的第一步先拦截 `records` 分支，按子命令名选择实现模块；若缺 `miles` 包则给出「需在锁定运行时中运行」的退出提示：[open_instruct/miles/__main__.py:L26-L44](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/miles/__main__.py#L26-L44)。这段代码同时示范了「延迟导入」模式——`importlib.import_module` 只在确定目标后才执行。

主解析器只有三个位置：命令、配置路径与可重复的 `--set` 覆盖：[open_instruct/miles/__main__.py:L45-L57](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/miles/__main__.py#L45-L57)。L46 的 `choices=("plan", "validate", "train", "run", "status")` 就是「五个子命令」的出处。

`execute()` 读取 TOML 文档后，用 `schema_version in payload or "model" in payload` 判定结构化与否，再分别构造 `RunSpec`（经 `specs.from_dict`）或 `RunConfig`：[open_instruct/miles/__main__.py:L66-L73](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/miles/__main__.py#L66-L73)。这是「TOML → RunSpec → RunConfig」编译链的入口（编译链细节在 u8-l2 精读）。

`plan` 分支调用 `config.plan()` 并打印 JSON 与 advisories，不做任何 GPU 操作：[open_instruct/miles/__main__.py:L74-L78](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/miles/__main__.py#L74-L78)。

`run`/`status` 要求结构化文件并延迟导入 `execution.launch`：[open_instruct/miles/__main__.py:L79-L87](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/miles/__main__.py#L79-L87)。L81 的报错信息直接写明「raw configs support plan/validate/train」。

`validate` 与 `train` 的分发：结构化校验会连物理分配一起 `plan()`，而训练交给 `workflow.execute`（细节在 u8-l3）：[open_instruct/miles/__main__.py:L96-L109](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/miles/__main__.py#L96-L109)。OPD（蒸馏）规格有独立分支（L88-L95），本讲不展开。

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

**预期结果**：`plan` 只做 CPU 侧的配置解析与物理分配规划，不触发 GPU 运行时导入；本讲撰写时未在本地实际执行该命令，输出样例**待本地验证**（文档承诺见 [docs/miles/index.md:L21-L23](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/docs/miles/index.md#L21-L23)）。注意：模板中的 `/weka/.../YOUR_USERNAME/...` 是占位路径，个别校验可能对其报提示，属正常现象；`plan` 不要求挂载这些路径。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `run`/`status` 拒绝低层原始配置（`RunConfig`），只接受 `schema_version=1` 的结构化文件？

**参考答案**：`run` 要把「模型、数据、拓扑、启动、挂载」完整编译成 Beaker 实验，这些信息只有结构化 RunSpec 才齐备；低层 `RunConfig` 只描述训练器参数，缺少提交所需的 launch/output 等段落。源码在 [open_instruct/miles/__main__.py:L79-L81](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/miles/__main__.py#L79-L81) 显式报错。

**练习 2**：`python -m open_instruct.miles records summarize` 与 `plan` 对运行环境的依赖有何不同？

**参考答案**：`records summarize` 分发到 MILES 包内的 `miles.utils.record_summary`，需要安装 MILES 私有运行时（缺失时报「requires the MILES package」）；`plan` 只依赖 open-instruct 的 CPU 配置代码，无需 GPU 栈。见 [open_instruct/miles/__main__.py:L27-L44](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/miles/__main__.py#L27-L44)。

**练习 3**：`--set name=my-run` 这样的覆盖发生在哪个阶段？与直接改 TOML 有何区别？

**参考答案**：覆盖作为 `overrides` 参数传给 `specs.from_dict` / `RunConfig.from_dict`（[open_instruct/miles/__main__.py:L69-L73](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/miles/__main__.py#L69-L73)），在解析时叠加到 TOML 文档之上。区别：TOML 文件保持原样（可继续作为版本化的基准），覆盖只影响这一次调用，适合临时调参、sweep 与调试。

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

定位声明与三句式所有权划分写在文档首页开头：[docs/miles/index.md:L3-L6](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/docs/miles/index.md#L3-L6) ——「Open Instruct owns the researcher configuration and data/reward integration; MILES owns rollout orchestration and shared RL machinery; the adapter trains through OLMo-core and serves through SGLang」。

与既有训练器的关系（不弃用、仍可用、按模型与负载选择）：[docs/miles/index.md:L8-L13](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/docs/miles/index.md#L8-L13)。GRPO 指南开头同样强调「recommended path for supported Olmo MoE models…Existing Core/vLLM and DeepSpeed/vLLM workflows remain available」：[docs/miles/grpo.md:L3-L8](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/docs/miles/grpo.md#L3-L8)。

架构页第一段给出「RunSpec 编译成 RunConfig（含 CoreConfig 与原生 MILES 选项）」的链条，并指出 Python API 也可供集成使用：[docs/miles/architecture.md:L3-L6](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/docs/miles/architecture.md#L3-L6)。

上文的五层所有权表出自：[docs/miles/architecture.md:L8-L14](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/docs/miles/architecture.md#L8-L14)。Core 训练 actor 的边界（`miles.backends.core_utils.actor.OLMoCoreTrainRayActor` 实现训练边界，模型/checkpoint 代码住在 pinned MILES fork 中）见：[docs/miles/architecture.md:L16-L21](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/docs/miles/architecture.md#L16-L21)。

运行时依赖的「事实源」是 `runtime/miles/runtime.lock.json`——锁定依赖 commit 与不可变二进制基座，构建时不打补丁、不跟随分支尖端；open-instruct 自身的普通环境保持独立版本：[docs/miles/architecture.md:L25-L30](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/docs/miles/architecture.md#L25-L30)。这就是「MILES 训练镜像 ≠ open-instruct 开发环境」的原因（对启动与镜像的影响见 u8-l8）。

open-instruct 侧的代码按职责分包，`configuration`/`execution` 等八个包与 MILES 侧 `backends/core_utils` 的分工表在：[docs/miles/architecture.md:L271-L298](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/docs/miles/architecture.md#L271-L298)。L288-L291 特别澄清了三个容易混淆的数据模块：MILES `backends/core_utils/data.py` 适配样本给训练器；`rollout/data_source.py` 拥有运行时数据源行为；`datasets/run_data.py` 准备研究者输入。

后端训练器与托管 rollout 模块**不导入 open-instruct**；open-instruct 以回调形式供给 `core_records_factory`（逐题记录）与 `core_evaluation_snapshot`（按需 HF 导出）：[docs/miles/architecture.md:L304-L318](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/docs/miles/architecture.md#L304-L318)。这是三层边界的代码级体现：依赖箭头从 MILES 指向接口，而不是反过来。

本轮更新一处路径变化：训练生命周期检查引用的 dev.toml 已随示例重组改到 `configs/miles/examples/tiny/dev.toml`：[docs/miles/architecture.md:L212-L217](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/docs/miles/architecture.md#L212-L217)。

#### 4.2.4 代码实践

**实践目标**：把五层所有权表落到 open-instruct 仓库的实际目录上，绘制一张三层架构图。

**操作步骤**：

1. 列出 open-instruct 侧的 MILES 包结构：

   ```bash
   ls open_instruct/miles/
   ```

2. 对照 [docs/miles/architecture.md:L276-L291](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/docs/miles/architecture.md#L276-L291) 的包职责表，把每个子目录（`configuration`、`execution`、`rollout`、`rewards`、`datasets`、`evaluation`、`infrastructure`…）标注到五层表中的一层。
3. 画一张纵向架构图：顶层「研究者 run.toml」，其下三个所有权框（Open Instruct 包装层 / MILES 运行时 / Core 适配器），底座两个上游框（OLMo-core、olmo-sglang）；用箭头标注 RunSpec→RunConfig 编译、rollout 编排、权重发布与 checkpoint 导出的流向。
4. 用只读 git 命令确认本轮示例目录重组：

   ```bash
   git log --oneline --follow -- configs/miles/examples/tiny/dev.toml | head -5
   git show --stat 784987452 -- configs/miles/examples/ | head -20
   ```

**需要观察的现象**：步骤 1 列出的子目录与文档职责表一一对应；步骤 4 的 `--follow` 历史会显示 dev.toml 从 `configs/miles/examples/` 移动到 `configs/miles/examples/tiny/` 的轨迹（提交 784987452，PR #2255）。

**预期结果**：得到一张可复述的三层架构图；能口头回答「`open_instruct/miles/rewards/` 属于哪一层」（包装层：验证器与奖励路由）与「权重发布代码住在哪一层」（MILES `backends/core_utils/publication`，open-instruct 仓库中不存在该代码）。步骤 3 的图为手工产物，无唯一标准答案；步骤 4 命令为只读 git 操作，可安全执行。

#### 4.2.5 小练习与答案

**练习 1**：MILES 与 `grpo_fast.py` 都用 Ray + 多进程做 RL，本质区别是什么？

**参考答案**：`grpo_fast.py` 是 open-instruct 仓库内的一个完整训练脚本（DeepSpeed 训练器 + vLLM 引擎，代码全部在本仓库，u6-l4）；MILES 把「rollout 编排与共享 RL 机制」交给私有 MILES 运行时（按 `runtime.lock.json` 锁定），open-instruct 只保留配置/数据/奖励集成与提交，训练走 OLMo-core、服务走 SGLang。另见 [docs/miles/index.md:L8-L13](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/docs/miles/index.md#L8-L13)：引入 MILES 不弃用既有 GRPO 训练器。

**练习 2**：如果要在 MILES 训练中新增一种自定义验证器，改动应落在哪一层？为什么？

**参考答案**：落在 open-instruct 包装层的 `open_instruct/miles/rewards/`（验证器、奖励路由与托管裁判），以可信注册表条目（factory + 配置）暴露给 MILES。因为后端训练器与托管 rollout 模块不导入 open-instruct（[docs/miles/architecture.md:L304-L313](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/docs/miles/architecture.md#L304-L313)），MILES 侧只通过约定路径消费应用侧回调与注册表。（验证器细节见 u8-l6。）

**练习 3**：为什么 open-instruct 的普通 `uv.lock` 环境跑不了 MILES 训练，而 `plan` 却能跑？

**参考答案**：训练需要 MILES 私有运行时、SGLang 与锁定版本的 OLMo-core，这些由 `runtime/miles/runtime.lock.json` 与专用应用镜像提供（[docs/miles/architecture.md:L25-L30](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/docs/miles/architecture.md#L25-L30)、[docs/miles/architecture.md:L179-L193](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/docs/miles/architecture.md#L179-L193)）；`plan` 只走 CPU 配置代码，CLI 把运行时 import 延迟到需要它的命令（[open_instruct/miles/__main__.py:L4-L5](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/miles/__main__.py#L4-L5)）。

### 4.3 文档地图与本轮扩展

#### 4.3.1 概念说明

`docs/miles/` 是 MILES 的权威文档目录：当前指令都住在这里，示例 TOML 表达起始配方，配置代码 + pinned 原生解析器快照 + `runtime/miles/runtime.lock.json` 共同定义「接受哪些设置、运行时来源是什么」。理解文档地图的意义在于：MILES 的文档是**分层且部分自动生成**的——四个参考页由 MkDocs 从配置源在内存中生成、不入库，手写文档与生成文档的维护方式不同。

本轮（bae4209 → 8cd7c9eb，五个提交）文档地图的扩展主要有两处，都与本单元后续讲义直接相关：

1. **`docs/miles/grading.md`（共享判分输入）**：PR #2250 统一了 MILES 的判分输入——历史上各判分器自己选答案区域，思考里的正确答案可能拿到分而交付答案却是错的；现在由共享的 `ResponseParts` 一次切分 thinking 与交付响应，并用 objective（answer/process/format/composite）× scope × require_response 声明每个验证器的输入契约。GRPO 指南的「Final-answer-only rewards」一节相应改写并链接该页（深入讲解在 u8-l12）。
2. **`docs/miles/abstention.md`（Nemotron 弃答 QA）**：PR #2252 新增原生弃答式 QA 奖励链路：`nemo_abstention_v1` 源适配器 + 从 NVIDIA-NeMo/Gym 移植的 rubric + 复用既有裁判服务（深入讲解在 u8-l13）。

同时，PR #2255 把维护示例按**模型家族**重组为 `tiny/` 与 `small/` 两个目录，GRPO 指南与架构页中的所有示例路径随之更新。

#### 4.3.2 核心流程

文档地图的检索路径：

```text
docs/miles/index.md（首页）
  ├─ Start here 五步入门顺序（L15-L23）
  ├─ Documentation map 表：按「什么时候用」组织的主表（L27-L53）
  └─ Authority and maintenance：权威性与生成式文档的维护规则（L55-L66）

mkdocs.yml 导航（/miles/ 一节）
  └─ 站点导航比首页表格更全：grading.md 在 "Data and rewards" 下（L120）

正文中的上下文链接
  ├─ grpo.md → grading.md（判分契约，L107/L177）
  └─ source-preparation.md → abstention.md（弃答 QA，L578）
```

要点：**首页文档表不是穷举**。`grading.md`、`abstention.md` 等新页通过站点导航与上下文链接接入，而不是（尚）作为首页表格的行出现。查文档时要两条路径都走。

维护规则方面，自动生成参考页用 `python -m scripts.miles.generate_docs` 生成、`--check` 校验；普通文档构建与本地 `plan`/`validate` 都不需要安装 MILES：[docs/miles/index.md:L55-L66](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/docs/miles/index.md#L55-L66)。

#### 4.3.3 源码精读

入门五步顺序（从 GRPO 指南开始，到复制结构化示例、读 workflow、follow 启动指南、用 operations 判完成）：[docs/miles/index.md:L15-L23](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/docs/miles/index.md#L15-L23)。注意 L20 已经指向按家族重组后的示例目录（`configs/miles/examples/README.md`）。

首页的文档地图主表（「Document | Use it when」两列，共 25 行）：[docs/miles/index.md:L25-L53](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/docs/miles/index.md#L25-L53)。这张表本身没有 grading.md/abstention.md 的行——两页经由下面的链接接入。

GRPO 指南「Truncated responses」一节末尾引出判分契约文档：截断零奖励的基线不可与其他奖励后处理组合，「最终答案选择与显式验证器例外见 grading.md」：[docs/miles/grpo.md:L96-L107](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/docs/miles/grpo.md#L96-L107)。

本轮改写后的「Final-answer-only rewards」一节：`core.reward_final_answer_only=true` 只判最后一个字面 `</think>` 之后的文本；缺失闭合标记、空白最终答案或重开思考会拿到「要求响应的验证器的地板分」而不调用它；共享 `ResponseParts` 区分 thinking 与交付的答案/动作，可信注册表声明用 objective 标识 answer/process/format/composite：[docs/miles/grpo.md:L161-L177](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/docs/miles/grpo.md#L161-L177)。这一节是 u8-l12 的门牌。

验证器的消费者迁移契约（可选实现 `async_call_final_answer`、迁移消费者收到 `selected_input=True` 并跳过自己的思考边界逻辑、结构化消费者可用 `async_call_parts`）：[docs/miles/grpo.md:L179-L189](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/docs/miles/grpo.md#L179-L189)。

`docs/miles/grading.md` 自身开门见山给出动机——「历史上各判分器自选答案区域，思考里的正确答案可能得分而交付答案错误或未完成」——并声明该页登记相对基线 commit 的有效判分变更：[docs/miles/grading.md:L1-L4](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/docs/miles/grading.md#L1-L4)。

弃答 QA 文档（经 source-preparation 链接接入）：`nemo_abstention_v1` 源适配器保留原始 system/user 消息并输出结构化 question/reference 目标，训练需命名裁判，纯答案匹配无法复现弃答目标：[docs/miles/abstention.md:L1-L4](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/docs/miles/abstention.md#L1-L4)；接入点在 [docs/miles/source-preparation.md:L578](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/docs/miles/source-preparation.md#L578)。

站点导航中 grading.md 的条目（"Grading inputs and changes"，位于 Data and rewards 组）：[mkdocs.yml:L118-L124](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/mkdocs.yml#L118-L124)。

维护示例的模型家族重组：`tiny/` 收纳既有配方（dev/node/three-node），`small/` 目前只有复制自 tiny dev 的起始文件，为 upcoming 的模型/拓扑工作预留：[configs/miles/examples/README.md:L1-L15](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/configs/miles/examples/README.md#L1-L15)。

示例基线声明（除非明确声明否则以示例为准，尤其保持 `miles.calculate_per_token_loss=false`、`core.reward_zero_truncated=true`、`core.reward_final_answer_only=true` 与 `core.reward_reasoning_mode="required"`；process/boundary-format 奖励需声明例外，见 grading 契约）：[configs/miles/examples/README.md:L21-L32](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/configs/miles/examples/README.md#L21-L32)。

tiny dev 模板的 `[core]` 段落把上述判分基线写成了可复制的注释与字段：`reward_final_answer_only = true`（判交付响应，process/format 例外需显式声明）、`reward_reasoning_mode = "required"`：[configs/miles/examples/tiny/dev.toml:L71-L82](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/configs/miles/examples/tiny/dev.toml#L71-L82)。

#### 4.3.4 代码实践

**实践目标**：走通文档地图的两条检索路径，定位本轮新增的两页文档及其接入点。

**操作步骤**：

1. 打开 [docs/miles/index.md](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/docs/miles/index.md)，通读 "Start here" 与 "Documentation map" 表，确认表中没有 grading/abstention 的行。
2. 在仓库内全局检索两页的引用：

   ```bash
   grep -rn "grading.md" docs/miles/*.md
   grep -rn "abstention.md" docs/miles/*.md
   ```

3. 打开 [docs/miles/grading.md](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/docs/miles/grading.md) 的 "Configuration and eligibility" 一节，记下库默认值（`reward_final_answer_only=false`）与维护示例的显式开启之间的差别。
4. 打开 [mkdocs.yml](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/mkdocs.yml) 的 `/miles/` 导航段，确认 grading.md 在导航中的位置；再用 `uv run mkdocs serve`（u1-l2 讲过）在本地浏览站点导航。
5. 对照本轮 diff 看示例重组与指南改写的联动：

   ```bash
   git diff --stat bae4209fdc541914540670c200628f67a39371ea..8cd7c9eb4e4d997cd482e9f88f5249d081728d9a -- configs/miles/examples/ docs/miles/grpo.md
   ```

**需要观察的现象**：步骤 2 中 grading.md 的引用落在 grpo.md（判分语义两处）；abstention.md 的引用落在 source-preparation.md；步骤 5 的 stat 显示 dev/node/three-node 三个模板从 `examples/` 迁入 `examples/tiny/`、新增 `small/dev.toml`，以及 grpo.md 的最终答案一节被改写。

**预期结果**：得出结论——新增文档通过「上下文链接 + mkdocs 导航」接入，首页表格是精选而非穷举；示例重组（PR #2255）与指南改写（PR #2250）是同批联动的文档变更。步骤 1-5 均为只读操作；步骤 4 的本地站点为可选步骤，效果**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：想在本地修改 `docs/miles/configuration.md` 中的一个字段默认值，直接编辑该文件对吗？

**参考答案**：不对。`configuration.md` 等四个参考页由 MkDocs 从配置源在内存中生成、不入库；应编辑源定义与审校描述，再用 `python -m scripts.miles.generate_docs` 重新生成、`--check` 校验。见 [docs/miles/index.md:L55-L63](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/docs/miles/index.md#L55-L63)。

**练习 2**：`tiny/dev.toml` 与 `small/dev.toml` 目前内容几乎相同，为什么还要分两个目录？

**参考答案**：示例按**模型家族**重组——`tiny/` 收纳现有的微型模型配方（单 GPU 力学检查、单节点、三节点），`small/` 为即将开展的更大模型/拓扑工作提供起始文件占位。README 明说 small 的 dev「does not qualify a larger model on the one-GPU smoke-test layout」：[configs/miles/examples/README.md:L13-L15](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/configs/miles/examples/README.md#L13-L15)。

**练习 3**：`core.reward_final_answer_only` 的库默认值与维护示例值分别是什么？为什么示例要显式写出？

**参考答案**：库默认 `false`（保留历史判分行为），维护示例显式设 `true`（思考型策略只判交付响应）。示例是配置基线——除非 run 明确声明，应以示例为准，显式写出避免「沉默改变目标」。见 [docs/miles/grading.md:L7-L12](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/docs/miles/grading.md#L7-L12) 与 [configs/miles/examples/tiny/dev.toml:L71-L77](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/configs/miles/examples/tiny/dev.toml#L71-L77)。

## 5. 综合实践

**任务：为你的第一次 MILES 冒烟运行制作一页「所有权笔记」。**

1. **复制模板**：把 [configs/miles/examples/tiny/dev.toml](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/configs/miles/examples/tiny/dev.toml) 复制到被 git 忽略的 `runs/my-dev.toml`（目录不存在则创建；不要直接改模板——配置约定见 [docs/miles/grpo.md:L42-L49](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/docs/miles/grpo.md#L42-L49)）。
2. **改占位符**：替换 `YOUR_USERNAME` 与模型/输出路径（README 的准备步骤见 [configs/miles/examples/README.md:L45-L60](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/configs/miles/examples/README.md#L45-L60)）。
3. **无 GPU 规划**：运行 `uv run python -m open_instruct.miles plan runs/my-dev.toml`，把输出 JSON 里你认识的段落（model/data/training/inference/core）与 4.2 节架构图逐一连线。
4. **写一页笔记**，回答三个问题：
   - 这个 run 里哪些工作由 open-instruct 包装层完成（配置校验、数据准备、GSM8K 验证器、提交回执）？
   - 哪些由 MILES 运行时/Core 适配器完成（SGLang rollout、OLMo-core 训练、权重发布、checkpoint）？
   - `[core]` 段的 `reward_final_answer_only`/`reward_reasoning_mode`（[configs/miles/examples/tiny/dev.toml:L71-L77](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/configs/miles/examples/tiny/dev.toml#L71-L77)）最终影响的是哪一层的哪个环节？（提示：判分输入选择，见 [docs/miles/grading.md](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/docs/miles/grading.md)。）
5. **验证笔记**：用只读命令抽查你的连线，例如 `ls open_instruct/miles/rewards/`（验证器确实在包装层）、`grep -n "runtime.lock.json" docs/miles/architecture.md`（运行时锁的权威位置）。

预期产物：一张三层架构图 + 一页所有权笔记 + 一份 `plan` 输出的对照标注。`plan` 的具体输出本讲未实际执行，**待本地验证**；若 `plan` 因占位路径报提示，记录提示内容并继续——规划本身不需要那些挂载存在。

## 6. 本讲小结

- **入口唯一**：`python -m open_instruct.miles` 暴露五个子命令 `plan/validate/train/run/status`（另有 `records` 记录分析）；运行时导入延迟到需要的命令，`plan`/`validate` 无 GPU、无 MILES 运行时即可运行。
- **结构化双轨**：含 `schema_version` 或 `model` 的 TOML 编译为 RunSpec（`run`/`status` 只接受这种）；低层 RunConfig 仅供 `plan`/`validate`/`train`。
- **三层所有权**：open-instruct 包装层管配置/数据/奖励/提交；MILES 运行时管 rollout 编排与共享 RL 机制；Core 适配器经 OLMo-core 训练、SGLang 服务。后端不导入 open-instruct，open-instruct 以回调与可信注册表注入能力。
- **不弃用旧路线**：`grpo.py` 与 `grpo_fast.py` 仍是稠密模型与历史复现的现行选择；MILES 面向受支持的 Olmo MoE 模型。
- **文档地图本轮扩展**：`grading.md`（共享判分输入，PR #2250）与 `abstention.md`（Nemotron 弃答 QA，PR #2255 之外由 #2252 引入）经上下文链接与 mkdocs 导航接入；首页表格是精选而非穷举。
- **示例按模型家族重组**：`configs/miles/examples/tiny/`（dev/node/three-node）与 `small/`（暂为 tiny dev 的副本占位），grpo.md 与 architecture.md 中的路径已同步更新。

## 7. 下一步学习建议

下一讲 **u8-l2（配置系统：TOML → RunSpec → RunConfig）** 将拆开本讲只碰了一下的编译链：`configuration/run_spec.py` 的加载与编译、CoreConfig/RunConfig 如何编译出参数列表并挂接奖励/数据钩子、constraints 驱动的校验，以及本轮新增的判分与 score-centering 编译选项。

在进入下一讲之前，建议先做两件源码阅读：

1. 浏览 `open_instruct/miles/configuration/` 目录（`ls` 即可），对照 [docs/miles/architecture.md:L276-L291](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/docs/miles/architecture.md#L276-L291) 的职责表建立印象。
2. 通读 [docs/miles/workflow.md](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/docs/miles/workflow.md)（配合 u8-l3），了解一次 run 从准备、训练到导出的阶段划分，为后续所有 MILES 讲义提供骨架。

若想先横向扩展，可回看 u6-l4（`grpo_fast.py` 的 Ray 拓扑）对比两条 RL 路线的架构差异。
