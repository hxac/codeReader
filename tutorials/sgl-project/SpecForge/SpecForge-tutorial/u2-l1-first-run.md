# 五分钟跑通一次训练

## 1. 本讲目标

学完本讲，你应该能够：

- 用 `specforge train --config <yaml>` 这一条命令跑通一个 checked-in 训练样例。
- 准确区分 SpecForge 的两条正交轴线：**数据模式**（online / offline）与**部署模式**（local_colocated / disaggregated）。
- 读懂一个示例 YAML 的关键字段，并能说出每个字段落在配置的哪一段。
- 用 `--plan` 在不启动训练的前提下，预览一次运行会拉起哪些进程。

本讲只覆盖「怎么跑」和「配置长什么样」，不深入装配与运行时细节——那是 u3 / u6 / u7 的内容。

## 2. 前置知识

在开始前，请确认你已经具备以下认知（来自 u1-l2、u1-l5）：

- **环境已就绪**：SpecForge 是 Python ≥ 3.11 的包，安装后会注册一个名为 `specforge` 的命令行入口（console script），指向 `specforge.cli:main`。它只有 `train` / `export` / `benchmark` 三个子命令。
- **单一类型化入口**：全项目没有方法专属的 Python 训练脚本。所有方法（EAGLE3、DFlash、Domino、DSpark、P-EAGLE）都走同一条 `specforge train`，靠 YAML 里的 `training.strategy` 字段区分。
- **七段配置**：一份 run config 由 `model` / `data` / `training` / `tracking` / `profiling` / `runtime` / `deployment` 七段，外加 `run_id` 与 `output_dir` 两个顶层字段组成。
- **源码主轴**：一次 `train` 的关键路径是 `cli.py → config/schema.py → application/composition.py → launch.py → training/assembly.py → trainer.py`。

一个容易混淆、但本讲必须澄清的点：**"online / offline / disaggregated"不是三种数据模式**。真正存在的是两个独立的选择：

| 轴线 | 取值 | 由谁决定 |
| --- | --- | --- |
| 数据模式 | online / offline | `data` 段填了哪个数据字段 |
| 部署模式 | local_colocated / disaggregated | `deployment.mode` 字段 |

"disaggregated" 是**部署模式**的一种，不是第三种数据模式。本讲第 4.2 节会讲清两者的关系。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [specforge/cli.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py) | 唯一命令入口，做命令解析与进程生命周期，不做模型装配。 |
| [specforge/config/schema.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py) | 类型化配置定义，包含 online/offline 的 `mode` 派生与数据源唯一性校验。 |
| [specforge/launch_plan.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch_plan.py) | 把一份配置推导成「进程计划」（producer / consumer / torchrun），`--plan` 渲染的就是它。 |
| [docs/basic_usage/training.md](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/docs/basic_usage/training.md) | 官方训练文档，含模式对照表与支持组合表。 |
| [examples/configs/qwen3-8b-eagle3-disaggregated.yaml](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/examples/configs/qwen3-8b-eagle3-disaggregated.yaml) | EAGLE3 在线 disaggregated 黄金样例 YAML。 |
| [examples/configs/qwen3-8b-dflash-online.yaml](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/examples/configs/qwen3-8b-dflash-online.yaml) | DFlash 在线样例，综合实践会用到它。 |

## 4. 核心概念与源码讲解

### 4.1 train 子命令

#### 4.1.1 概念说明

`specforge train` 是 SpecForge **唯一的训练入口**。无论你训练哪种草稿方法、用哪种并行拓扑，对外暴露的都是同一条命令：

```bash
specforge train --config <你的 yaml> [section.field=value ...]
```

这种设计的核心动机是：**消灭方法专属脚本**。官方文档明确指出，旧的 `scripts/train_*.py` 命令是被**删除**而不是废弃的，没有任何兼容分发（见 [docs/basic_usage/training.md:14-17](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/docs/basic_usage/training.md#L14-L17)）。方法之间的差异全部收敛进 YAML，由 `training.strategy` 字段选择。

`train` 子命令接受几个关键参数：

| 参数 | 含义 |
| --- | --- |
| `-c` / `--config` | YAML 或 JSON 配置文件路径（必填）。 |
| `--role` | 启动角色：`auto` / `all` / `producer` / `consumer` / `both`，默认 `auto`。 |
| `--node-rank` | 多节点训练时本机的节点序号。 |
| `--plan` | 只打印解析后的进程计划，**不启动**任何 worker。 |
| 位置参数 `overrides` | 点号覆盖，如 `training.learning_rate=1e-4`。 |

#### 4.1.2 核心流程

`specforge train` 的一次调用，在 `main()` 内部的执行过程可以概括为：

```text
main(argv)
  ├── argparse 解析出 train 子命令及其参数
  ├── load_config(path, overrides)      # 读 YAML + 应用覆盖 + 类型校验
  ├── resolve_run(cfg)                  # 用 strategy 找到算法注册项
  ├── build_launch_plan(...)            # 推导进程计划（producer/consumer/torchrun）
  ├── 若 --plan：print(plan.render()) 后直接返回 0
  └── 否则按 plan.kind 分发：
        ├── plan.kind == "worker"  → 当前进程就是 worker，调 _train(...)
        └── 其它（supervisor 等）  → run_commands(plan) 拉起子进程
```

其中 `_train` 内部还会按角色再分一次叉：

- **producer 角色**：不初始化 CUDA、不建分布式进程组，直接 `build_application_run(resolved).run()` 发布特征引用。
- **consumer / all 角色**：先 `init_distributed(...)` 建好 TP/DP/SP 进程组，再装配并运行训练。

#### 4.1.3 源码精读

子命令注册在 `main()` 里，用 `argparse` 的 subparser 实现。`train` parser 的定义在此：

> [specforge/cli.py:172-198](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py#L172-L198) —— 注册 `train` 子命令及其全部参数（`--config` / `--role` / `--node-rank` / `--plan` / `overrides`）。

命令分发的主干在 `if args.command == "train":` 分支：

> [specforge/cli.py:241-267](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py#L241-L267) —— 依次完成「加载配置 → resolve_run → build_launch_plan → `--plan` 短路返回 → 按 `plan.kind` 分发到 `_train` 或 `run_commands`」。

注意 `--plan` 的短路：它在拉起任何进程之前就 `print(plan.render())` 并 `return 0`（见第 255-257 行），所以 `--plan` 是完全无副作用的预览。

`_train` 内部的角色分叉：

> [specforge/cli.py:113-146](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py#L113-L146) —— producer 直接运行应用；其余角色先 `init_distributed` 再运行。这段代码体现了「producer 不是 trainer」的关键事实。

#### 4.1.4 代码实践

**实践目标**：在不启动训练的前提下，验证 `train` 命令能正确解析一份真实配置，并看到它打算拉起的进程计划。

**操作步骤**：

1. 在仓库根目录执行（配置里的相对路径以仓库根为基准）：
   ```bash
   specforge train --plan \
     --config examples/configs/qwen3-8b-eagle3-disaggregated.yaml
   ```
2. 观察打印出的 JSON，重点关注 `kind`、`role`、`commands` 三个字段。

**需要观察的现象**：

- 输出是一段 JSON，而不是训练日志（因为 `--plan` 短路了）。
- `kind` 很可能是 `supervisor`，`role` 是 `both`，`commands` 里列出 `producer` 与 `consumer` 两条命令。

**预期结果**：命令立即返回，退出码 0，没有任何 GPU 被占用。若报 `unknown field` 之类错误，说明环境里的 `specforge` 版本与该 YAML 不匹配——回到 u1-l2 重新安装。

> 待本地验证：本实践假设你已在仓库根目录、且已按 u1-l2 安装好 `specforge`。若未装好，命令会报 `command not found`。

#### 4.1.5 小练习与答案

**练习 1**：如果不加 `--config`，`specforge train` 会怎样？

**答案**：`--config` 被声明为 `required=True`（见 [cli.py:173](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py#L173)），argparse 会直接报错退出，提示该参数必填。

**练习 2**：`--plan` 为什么不会占用 GPU？

**答案**：`--plan` 在 `build_launch_plan` 之后、`_train` / `run_commands` 之前就 `return 0` 了（见 [cli.py:255-257](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py#L255-L257)），根本没有进入模型装配与分布式初始化，自然不碰 GPU。

---

### 4.2 online/offline 模式表

#### 4.2.1 概念说明

「online 还是 offline」回答的是一个问题：**训练时，草稿模型需要的"目标模型特征"从哪里来？**

回忆 u1-l4：EAGLE3 这类方法要吃目标模型的隐藏状态。这些特征可以有两种来源：

- **online（在线）**：训练进行的同时，由一个外部的、打过补丁的 SGLang 服务实时捕获目标特征，通过 Mooncake 传输给训练进程。磁盘占用小，但训练期间必须保持目标推理服务在线。
- **offline（离线）**：训练开始前，先用脚本把目标特征**预计算**成检查点文件（`.ckpt`），训练时直接读盘。此时训练 GPU 上只装草稿模型，但需要更多磁盘空间。

官方文档用一张表总结了两者差异（见 [docs/basic_usage/training.md:221-224](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/docs/basic_usage/training.md#L221-L224)）：

| 模式 | 训练时的目标模型 | 磁盘占用 | data 配置字段 |
| --- | --- | --- | --- |
| Online | 外部/托管的 SGLang 捕获服务 | 低 | `train_data_path` 或 `prompts_path` |
| Offline | 不被 trainer 加载 | 高 | `hidden_states_path` |

而 **disaggregated** 是另一个维度——**部署模式**——的一个取值。它回答的是：**producer（捕获/发布特征）和 consumer（训练草稿）要不要拆成独立进程？**

- `local_colocated`：单进程本地运行（离线常用）。
- `disaggregated`：producer 与 consumer 拆开运行（所有 online 训练都必须是 disaggregated）。

#### 4.2.2 核心流程

SpecForge 在配置层用一条极简规则派生数据模式：

```text
若 data.hidden_states_path 非空  → mode = "offline"
否则                              → mode = "online"
```

派生出的 `mode` 再参与一系列硬约束校验：

```text
mode == "online"
  └── 要求 deployment.mode == "disaggregated"
  └── 要求 model.target_backend == "sglang"

data 段必须三选一（且仅选一）：
  train_data_path   (在线：原始对话)
  prompts_path      (在线：预分词 JSONL)
  hidden_states_path(离线：预计算特征)
```

正因为 online 强制要求 disaggregated + sglang，所以"在线训练"在 SpecForge 里一定是"在线 disaggregated 训练"——这也是样例文件名常把两者写在一起的原因。

#### 4.2.3 源码精读

数据模式的派生逻辑只有一行，但极其关键：

> [specforge/config/schema.py:859-861](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L859-L861) —— `mode` 属性：`return "offline" if self.data.hidden_states_path else "online"`。**online/offline 完全由是否填了 `hidden_states_path` 决定。**

数据源唯一性校验（三选一）：

> [specforge/config/schema.py:148-161](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L148-L161) —— `sum(sources) != 1` 即报错，强制 `train_data_path` / `prompts_path` / `hidden_states_path` 恰好填一个。

online → disaggregated + sglang 的硬约束：

> [specforge/config/schema.py:676-681](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L676-L681) —— `online` 模式下，部署必须是 `disaggregated`、目标后端必须是 `sglang`，否则校验失败。

支持组合矩阵（哪些方法支持哪种模式）见官方表格：

> [docs/basic_usage/training.md:234-240](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/docs/basic_usage/training.md#L234-L240) —— 例如 P-EAGLE 只支持 online（且 `batch_size=1`），不支持任何 offline。

#### 4.2.4 代码实践

**实践目标**：用源码阅读的方式，确认"文件名带 online / offline 不等于实际模式"，真正决定模式的是 `data` 段。

**操作步骤**：

1. 打开 [examples/configs/qwen3-8b-eagle3-disaggregated.yaml](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/examples/configs/qwen3-8b-eagle3-disaggregated.yaml)，看它的 `data:` 段：填的是 `train_data_path`，没有 `hidden_states_path`。
2. 打开 [examples/configs/qwen3-8b-eagle3-offline.yaml](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/examples/configs/qwen3-8b-eagle3-offline.yaml)，看它的 `data:` 段：填的是 `hidden_states_path`。

**需要观察的现象**：

- `disaggregated.yaml` 文件名里没有 "online"，但因为用了 `train_data_path`，按 `mode` 属性它是 **online**。
- `offline.yaml` 用了 `hidden_states_path`，所以是 **offline**，且它的 `deployment.mode` 是 `local_colocated`。

**预期结果**：你能用一句话说出——"文件名只是标签，真正的 online/offline 由 `data` 段填哪个字段决定，规则就一行：有 `hidden_states_path` 就是 offline，否则就是 online。"

> 待本地验证：可对两个文件分别跑 `specforge train --plan`，对比输出 JSON 中 consumer 命令的差异（online 会带 producer，offline colocated 只有 consumer）。

#### 4.2.5 小练习与答案

**练习 1**：一份 YAML 同时填了 `train_data_path` 和 `hidden_states_path`，会发生什么？

**答案**：`_exactly_one_source` 校验失败（[schema.py:148-161](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L148-L161)），直接报错"set exactly one of ..."。

**练习 2**：为什么所有 online 训练必然是 disaggregated？

**答案**：因为校验层强制 `mode == "online"` 时 `deployment.mode` 必须是 `disaggregated`（[schema.py:676-681](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L676-L681)）。online 需要一个外部 SGLang 服务实时捕获特征，这天然要求 producer（捕获）与 consumer（训练）拆成独立进程。

---

### 4.3 示例 YAML 字段

#### 4.3.1 概念说明

理解了命令和模式，接下来逐段读一份真实 YAML。我们选 EAGLE3 在线 disaggregated 样例作为标本，因为它字段最全、最具代表性。它的全貌见 [examples/configs/qwen3-8b-eagle3-disaggregated.yaml](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/examples/configs/qwen3-8b-eagle3-disaggregated.yaml)。

一份 YAML 的字段可以归到三组里：

| 组 | 关键字段 | 作用 |
| --- | --- | --- |
| **model** | `target_model_path` / `draft_model_config` / `target_backend` / `vocab_mapping_path` / `torch_dtype` | 指定目标模型、草稿模型配置、目标后端、词表映射、权重精度。 |
| **data** | `train_data_path` / `max_length` / `chat_template` | 训练数据来源、最大长度、对话模板。**这里决定 online/offline。** |
| **training** | `strategy` / `num_epochs` / `max_steps` / `batch_size` / `learning_rate` / `ttt_length` / `attention_backend` / `save_interval` | 训练算法、步数、批大小、学习率、TTT 步数、注意力后端、存盘间隔。 |
| **tracking** | `report_to` | 实验跟踪后端（`none` / `wandb` / `tensorboard` / `swanlab` / `mlflow`）。 |
| **deployment** | `mode` / `trainer.nnodes` / `trainer.nproc_per_node` / `disaggregated.*` | 部署模式、节点数、每节点进程数、disaggregated 的控制目录与 Mooncake 端点。 |
| **顶层** | `run_id` / `output_dir` | 本次运行的唯一标识与输出目录。 |

#### 4.3.2 核心流程

读 YAML 时，建议按"决定性优先"的顺序看：

```text
1. data 段 → 判定 online/offline（看有没有 hidden_states_path）
2. deployment.mode → 判定 colocated/disaggregated
3. training.strategy → 判定用哪个算法（eagle3 / dflash / domino / dspark / peagle）
4. deployment.trainer.nproc_per_node → 判定会拉起几个 consumer 训练进程
5. 其余字段 → 影响精度、步数、存盘、跟踪
```

其中第 4 步直接关系到综合实践里"用到几个训练进程"的答案。

#### 4.3.3 源码精读

样例 YAML 的 `data` 段（决定它是 online）：

> [examples/configs/qwen3-8b-eagle3-disaggregated.yaml:10-15](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/examples/configs/qwen3-8b-eagle3-disaggregated.yaml#L10-L15) —— 用 `train_data_path` 指向原始对话数据，无 `hidden_states_path`，故为 online。

`training` 段（决定算法与超参）：

> [examples/configs/qwen3-8b-eagle3-disaggregated.yaml:17-29](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/examples/configs/qwen3-8b-eagle3-disaggregated.yaml#L17-L29) —— `strategy: eagle3`、`ttt_length: 7`（对应 u1-l4 的训练时测试）、`batch_size: 1`。

`deployment` 段（决定部署与进程拓扑）：

> [examples/configs/qwen3-8b-eagle3-disaggregated.yaml:37-50](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/examples/configs/qwen3-8b-eagle3-disaggregated.yaml#L37-L50) —— `mode: disaggregated`、`nnodes: 1`、`nproc_per_node: 1`，并配置了 Mooncake 元数据/主服务地址与捕获服务 URL。

进程数怎么由 `nproc_per_node` 推导出来，逻辑在 `launch_plan.py`：

> [specforge/launch_plan.py:534-566](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch_plan.py#L534-L566) —— `world_size = nnodes * nproc_per_node`；`world_size == 1` 时直接以单进程运行，`> 1` 时套上 `torchrun --nproc_per_node ...` 拉起多个 consumer rank。

而 online disaggregated、`--role auto` 的配置最终会得到一个 `supervisor` 计划（producer + consumer 两条命令）：

> [specforge/launch_plan.py:822-827](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch_plan.py#L822-L827) —— `role == "both"` 且非 managed_local 时，返回 `LaunchPlan("supervisor", "both", commands=(producer, consumer))`。

#### 4.3.4 代码实践

**实践目标**：读懂 DFlash online 样例的关键字段，并推算它比 EAGLE3 样例多了几个训练进程。

**操作步骤**：

1. 打开 [examples/configs/qwen3-8b-dflash-online.yaml](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/examples/configs/qwen3-8b-dflash-online.yaml)。
2. 找到 `training.strategy`、`data.train_data_path`（或 `hidden_states_path`）、`deployment.trainer.nproc_per_node` 三个字段。

**需要观察的现象**：

- `strategy: dflash`。
- `data` 段用的是 `train_data_path`，无 `hidden_states_path` → **online**。
- `nproc_per_node: 8`（对比 EAGLE3 样例的 `1`）。

**预期结果**：DFlash 样例是 online disaggregated 训练，consumer 端会用 `torchrun --nproc_per_node 8` 拉起 **8 个训练进程**（数据并行 consumer rank），外加 1 个 producer 捕获进程（producer 不是 trainer）。

> 待本地验证：可对该文件运行 `specforge train --plan`，在输出 JSON 的 `commands` 里找到 consumer 命令，确认其 argv 中含 `--nproc_per_node 8`。

#### 4.3.5 小练习与答案

**练习 1**：把 `qwen3-8b-eagle3-disaggregated.yaml` 的 `data.train_data_path` 换成 `data.hidden_states_path`（指向一个离线特征目录），会发生什么？

**答案**：`mode` 由 `online` 变为 `offline`（[schema.py:859-861](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L859-L861)）。此时 `deployment.mode: disaggregated` 仍然合法（离线允许 disaggregated），但 `disaggregated` 段里 online 专属的 Mooncake 在线捕获配置就不再被按 online 路径使用。

**练习 2**：`run_id` 和 `output_dir` 有什么区别？

**答案**：`run_id` 是本次运行的**逻辑标识**，用于命名检查点指针（如 `<run_id>-latest`、`<run_id>-best`）；`output_dir` 是**物理输出目录**，所有检查点、日志、trace 都写在它下面（见 [docs/basic_usage/training.md:411-415](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/docs/basic_usage/training.md#L411-L415)）。

---

## 5. 综合实践

**任务**：复制 `qwen3-8b-dflash-online.yaml`，把 `run_id` / `output_dir` 改成你自己的，然后回答两个问题——它是 online 还是 offline？会用到几个训练进程？

**操作步骤**：

1. 复制样例到你自己的文件（示例命令，路径自定）：
   ```bash
   cp examples/configs/qwen3-8b-dflash-online.yaml \
      examples/configs/my-dflash-run.yaml
   ```
2. 编辑 `my-dflash-run.yaml`，至少改两处：
   ```yaml
   run_id: my-dflash-run
   output_dir: ./outputs/my-dflash-run
   ```
   注意：`deployment.disaggregated.control_dir` 与 `consumer_state_dir` 也引用了旧 `output_dir`，建议一并改成新路径，否则会写到旧目录下。
3. 用 `--plan` 预览（不会启动训练）：
   ```bash
   specforge train --plan --config examples/configs/my-dflash-run.yaml
   ```
4. 阅读输出 JSON，确认 `kind`、`role`、consumer 命令的 argv。

**需要回答的问题与参考答案**：

| 问题 | 参考答案 |
| --- | --- |
| 它是 online 还是 offline？ | **online**。因为 `data` 段填的是 `train_data_path: ./cache/dataset/sharegpt_train.jsonl`，没有 `hidden_states_path`。按 [schema.py:859-861](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L859-L861) 的派生规则，即为 online。 |
| 用到几个训练进程？ | **8 个 consumer 训练进程**（`nproc_per_node: 8`，由 `torchrun --standalone --nproc_per_node 8` 拉起，见 [launch_plan.py:534-566](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch_plan.py#L534-L566)），外加 1 个 producer 捕获进程。producer 不是 trainer（见 [cli.py:121-126](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py#L121-L126)），所以"训练进程"数 = 8。 |

**进阶（可选）**：再叠加两个覆盖，观察计划是否随覆盖变化：

```bash
specforge train --plan --config examples/configs/my-dflash-run.yaml \
  training.max_steps=10 training.learning_rate=5e-5
```

注意 `output_dir` 也可以用覆盖语法直接改：`output_dir=./outputs/my-dflash-run`（覆盖语法详见 u2-l3）。

> 待本地验证：本实践的 `--plan` 部分可在任何装好 `specforge` 的环境运行（不占 GPU）；真正启动训练（去掉 `--plan`）需要 8 张可用 GPU、一个运行中的 SGLang 捕获服务以及 Mooncake 服务，属于 u7 的在线 disaggregated 话题。

## 6. 本讲小结

- SpecForge 只有一个训练入口 `specforge train --config <yaml>`，所有方法靠 `training.strategy` 区分，没有方法专属脚本。
- `--plan` 能在完全不启动训练、不占 GPU 的前提下，预览一次运行会拉起的进程计划。
- online/offline 是**数据模式**，由 `data` 段决定：填 `hidden_states_path` 即 offline，填 `train_data_path` / `prompts_path` 即 online（规则仅一行，见 `schema.py:859-861`）。
- local_colocated / disaggregated 是**部署模式**（`deployment.mode`），与数据模式正交；online 强制要求 disaggregated + `target_backend=sglang`。
- `data` 段三个数据字段必须且只能填一个，否则校验直接报错。
- 训练进程数由 `deployment.trainer.nproc_per_node`（及 `nnodes`）决定，`world_size > 1` 时会套上 `torchrun` 拉起多个 consumer rank；producer 是捕获进程，不算 trainer。

## 7. 下一步学习建议

- 想系统掌握一份 YAML 每一段、每个字段的含义与默认值，请接着学 **u2-l2 配置文件七段结构**，它会带你逐段拆解 `schema.py`。
- 想了解 `section.field=value` 覆盖语法、未知字段报错机制，以及 `--plan` 渲染细节，请学 **u2-l3 命令行覆盖与 plan 预览**。
- 如果你对"配置如何变成可执行训练"感兴趣，可以跳到 **u3 入口与启动链路**，深入 `cli.py` → `composition.py` → `launch.py` 的真实调用链。
