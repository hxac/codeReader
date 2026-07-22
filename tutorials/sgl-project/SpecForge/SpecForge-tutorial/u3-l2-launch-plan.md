# 启动计划 launch_plan

> 承接 [u3-l1 CLI 入口与命令解析](u3-l1-cli-entry.md)。上一讲我们追踪了 `specforge train` 从 `main` 到 `_train` 的七步主轴，并指出 `plan.kind` 决定一个进程是「在进程内直跑 `_train`」还是「充当 supervisor 去拉起子进程」。本讲就来拆解这个 `plan` 到底是怎么被构造出来的，以及拿到 plan 之后 supervisor 又是怎么执行的。

## 1. 本讲目标

读完本讲，你应当能够：

- 说清 `build_launch_plan` 的**输入**（一份已校验的 `Config` + 角色 + 节点号）和**输出**（一个不可变的 `LaunchPlan` 值对象）。
- 解释 `plan.kind` 取 `worker` / `command` / `supervisor` / `managed_supervisor` 四种值时分别对应什么进程拓扑，以及 `cli.py` 如何据此在 `_train` 与 `run_commands` 之间分流。
- 理解 `--role auto` 是如何被 `_resolve_role` 派生成具体角色（`all` / `producer` / `consumer` / `both`）的，以及 `--node-rank` 在多节点训练中的作用。
- 能用 `specforge train --plan` 在**不启动训练、不占 GPU** 的前提下体检一份配置会拉起哪些进程。

## 2. 前置知识

在进入源码前，先建立三个直觉。

### 2.1 「计划」与「执行」是分离的两步

很多人会把「我要训练」直接理解成「跑模型」。但 SpecForge 在中间插入了一个纯计算步骤：**先把一份配置折叠成一张「进程计划」，再决定怎么执行它**。这就像建筑施工前先出图纸——图纸本身不耗砖瓦，但能让你在动工前发现拓扑错误。

- 画图纸：`build_launch_plan(cfg, ...)` → 返回 `LaunchPlan`。它是**无副作用**的（不 spawn 进程、不占端口、不建目录）。
- 照图纸施工：`cli.py` 根据 `plan.kind` 走 `_train`（自己当 worker）或 `run_commands(plan)`（自己当 supervisor，拉子进程）。

这种分离是 `--plan` 预览能够「零开销」的根本原因。

### 2.2 角色（role）是 launch 概念，不是配置概念

在 [u2-l2](u2-l2-config-sections.md) 我们见过 `training.role`，它在 schema 里只允许四个值 `auto/all/producer/consumer`。但命令行 `--role` 还多了一个 **`both`**——它表示「我这个进程要同时当 producer 和 consumer 的 supervisor」。也就是说：

- **持久化在 YAML 里的 `training.role`**：`auto/all/producer/consumer`（描述「这个 run 打算怎么跑」）。
- **命令行 `--role`（launch role）**：`auto/all/producer/consumer/both`（描述「这次调用我充当哪个角色」）。

`both` 只存在于 launch 层，绝不会被写进 YAML。理解这一点能避免后面看 `_resolve_role` 时混淆。

### 2.3 worker_size 公式与分布式检测

训练的进程总数由拓扑决定：

\[
\text{world\_size} = \text{nnodes} \times \text{nproc\_per\_node}
\]

`build_launch_plan` 还会嗅探当前进程环境里有没有已经存在的 `torchrun` 痕迹（`_distributed_state` 检查 `RANK/WORLD_SIZE/LOCAL_RANK/MASTER_ADDR/MASTER_PORT` 五个变量）。如果检测到，说明自己正运行在别人拉起的 torchrun 里，plan 就不能再套一层 launcher，而应直接退化成 `worker`。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [specforge/launch_plan.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch_plan.py) | **本讲主角**。纯计划与进程监管：`LaunchPlan` 数据模型、`build_launch_plan` 构造器、`run_commands` 执行器。 |
| [specforge/cli.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py) | 调用方。`main` 调 `build_launch_plan`，再据 `plan.kind` 分流到 `_train` 或 `run_commands`。 |
| [specforge/config/schema.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py) | 提供 `deployment.mode`、`deployment.trainer`（nnodes/nproc_per_node/node_rank/master_addr）等拓扑字段。 |

本讲引用的源码点都集中在 `launch_plan.py`，`cli.py` 与 `schema.py` 只在关键衔接处点一下。

---

## 4. 核心概念与源码讲解

### 4.1 进程计划的数据模型与 plan.render

#### 4.1.1 概念说明

`build_launch_plan` 的返回值不是一个字典或一堆副作用，而是一个**不可变的值对象** `LaunchPlan`。把「计划」建模成数据有三大好处：

1. **可预览**：`--plan` 只需把这个对象序列化打印，不需要真的启动任何东西。
2. **可测试**：单元测试可以断言「给定这份配置，plan 的 kind 必须是 worker、role 必须是 all」，而无需起进程。
3. **职责清晰**：「画图」和「施工」是两个函数，`build_launch_plan` 只管画图，`run_commands` 只管施工。

#### 4.1.2 核心流程

`LaunchPlan` 的字段刻画了一张完整的进程拓扑图：

```
LaunchPlan
├── kind          # worker | command | supervisor | managed_supervisor
├── role          # all | producer | consumer | both
├── commands      # 要 spawn 的子进程命令（CommandSpec 元组）
├── worker_env    # 若 kind=worker，注入本进程的环境变量
├── services      # managed_supervisor 专属：本地 capture/Mooncake 服务
├── managed_root  # managed_supervisor 专属：控制目录
├── managed_ports # managed_supervisor 专属：需预检的端口
└── shutdown_grace_s  # SIGTERM 后留给清理的宽限秒数
```

`render()` 把上面这些字段序列化成 JSON，并对**敏感信息脱敏**（密钥、带密码的 URL），所以 `--plan` 的输出可以安全地贴到 issue 里。

#### 4.1.3 源码精读

先看两个基础字面量类型，它们定义了 plan 的全部取值空间：

[specforge/launch_plan.py:L27-L29](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch_plan.py#L27-L29) —— `LaunchRole`（命令行 `--role`，含 `both`）与 `PlanKind`（计划类型，含 `managed_supervisor`）的字面量定义。

单个子进程命令用 `CommandSpec` 描述，它只装三样东西：标签、argv、环境变量：

[specforge/launch_plan.py:L92-L103](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch_plan.py#L92-L103) —— `CommandSpec` 是个 frozen dataclass，`as_dict()` 在序列化时对 argv 和 env 做脱敏。

核心值对象 `LaunchPlan` 本体（注意它也是 `frozen=True`，构造后不可改）：

[specforge/launch_plan.py:L140-L151](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch_plan.py#L140-L151) —— `LaunchPlan` 字段定义，`shutdown_grace_s` 默认 30 秒。

`render()` 的实现很有讲究——它**只对 `managed_supervisor` 才额外打印 services/ports**，普通 plan 不打印这些空字段，保持输出干净：

[specforge/launch_plan.py:L153-L173](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch_plan.py#L153-L173) —— `render()` 序列化计划；`commands` 与 `worker_env` 都走 `_redacted_env` 脱敏。

脱敏逻辑本身值得一看，它解释了为何 `--plan` 输出里看不到你的 `wandb_key`：

[specforge/launch_plan.py:L48-L89](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch_plan.py#L48-L89) —— `_redacted` / `_redacted_env`：名字含 `auth_token/password/secret/credential/wandb_key/swanlab_key` 的变量整体替换为 `<redacted>`，URL 里的用户名密码也被抹掉。

#### 4.1.4 代码实践

**实践目标**：在不启动训练的前提下，亲眼看到一份真实的 `LaunchPlan` JSON，并确认脱敏生效。

**操作步骤**：

1. 挑一个 checked-in 的离线 colocated 配置，例如 `examples/configs/` 下任意 `*-offline*.yaml` 或不带 disaggregated 段的 eagle3 配置。
2. 运行：

   ```bash
   specforge train --config examples/configs/<你的 offline 配置>.yaml --plan
   ```

3. 再用一个带敏感环境变量的运行对比：

   ```bash
   WANDB_KEY=super-secret-abc specforge train --config <同一份配置> --plan
   ```

**需要观察的现象**：

- 输出是一段 JSON，顶层有 `kind`、`role`、`commands`、`worker_env` 四个键。
- 对离线单卡 colocated 配置，`kind` 应为 `"worker"`、`role` 应为 `"all"`，`commands` 为空数组。
- 第二条命令里，即使环境里塞了 `WANDB_KEY`，JSON 里也不会出现明文密钥。

**预期结果**：`kind=worker` 表示这次调用不会拉子进程，而是由 `cli` 自己进程内跑 `_train`。若你的配置是多卡（`nproc_per_node>1`），`kind` 会变成 `"command"`，`commands` 里会出现一条以 `torchrun` 开头的 argv——这正是 4.3 节要解释的分支。若无法本地运行，请标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：`LaunchPlan` 为什么被设计成 `frozen=True`（不可变）？

> **参考答案**：因为 plan 是「图纸」而非「施工」。不可变保证它被 `--plan` 打印、被单元测试断言、被 `run_commands` 消费时都不会被意外篡改；多处代码可以共享同一个 plan 引用而无需防御性拷贝。

**练习 2**：为什么 `render()` 对 `managed_supervisor` 才打印 `services` 字段？

> **参考答案**：只有 managed_supervisor 才会由 SpecForge 自己拉起本地 Mooncake/capture 服务（`ServiceSpec`），其它 plan 没有这些字段。条件打印避免在普通输出里堆满空列表，保持 `--plan` 输出可读。

---

### 4.2 角色派生：从 auto 到具体角色

#### 4.2.1 概念说明

用户绝大多数时候不会显式写 `--role`，而是用默认的 `auto`。`auto` 不是个真角色，它是一个**待派生的占位符**——`_resolve_role` 的职责就是结合「部署模式 + 是否续训 + 是否已在 torchrun 里」把 `auto` 翻译成一个**具体角色**。这一步是理解「同一份 YAML 为什么有时起 1 个进程、有时起 2 个进程」的关键。

#### 4.2.2 核心流程

`_resolve_role(cfg, requested, distributed)` 的派生规则（requested=`auto` 时）：

```
disaggregated?
├── 是
│   ├── YAML 里 training.role 已是 producer/consumer → 沿用它
│   ├── resume_from 非空（续训）            → consumer
│   └── 否则                                → both
└── 否（local_colocated）                    → all
```

派生之后还有一组**互斥校验**（fail-fast）：

- disaggregated 不允许 `all`（在线分离训练天然要 producer+consumer 两个角色）。
- colocated 不允许 `producer/consumer/both`（没有目标服务可分离）。
- `both` 不能续训（producer 没有可恢复的训练状态）。
- 若已身处 torchrun（`distributed=True`），disaggregated 必须显式 `consumer`，不能再套 supervisor。

#### 4.2.3 源码精读

派生规则与互斥校验全部集中在一个函数里：

[specforge/launch_plan.py:L187-L213](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch_plan.py#L187-L213) —— `_resolve_role`：`auto` 的派生分支与四条互斥错误。

派生依据之一是「部署模式」，它来自 schema：

[specforge/config/schema.py:L460-L466](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L460-L466) —— `DeploymentConfig.mode` 只允许 `local_colocated` 或 `disaggregated`，默认 `local_colocated`。

注意持久化的 `training.role` 与命令行 `--role` 的取值域不同——YAML 里没有 `both`：

[specforge/config/schema.py:L543-L546](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L543-L546) —— `TrainingConfig.role` 只允许 `auto/all/producer/consumer`；`both` 是 launch 专属。

派生的另一依据是「是否已在 torchrun 内」，由 `_distributed_state` 判断：

[specforge/launch_plan.py:L176-L184](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch_plan.py#L176-L184) —— `_distributed_state`：五个 rendezvous 变量要么全无、要么全有（否则报「不完整」），全有即视为已分布式。

与角色派生并列的还有 **node-rank 解析**。多节点训练时，每个节点必须告诉系统自己是第几号节点：

[specforge/launch_plan.py:L216-L227](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch_plan.py#L216-L227) —— `_resolved_node_rank`：优先级 `--node-rank` > `deployment.trainer.node_rank` > 环境变量 `NODE_RANK`，并校验 `0 ≤ node_rank < nnodes`。

对应的拓扑字段定义在 schema：

[specforge/config/schema.py:L238-L259](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L238-L259) —— `TrainerDeploymentConfig`：`nnodes`、`nproc_per_node`、`node_rank`、`master_addr`、`master_port`，以及 `nnodes>1` 时强制要求 `master_addr` 的校验。

#### 4.2.4 代码实践

**实践目标**：用源码阅读方式验证 `_resolve_role` 的派生表，不依赖 GPU。

**操作步骤**：

1. 打开 [specforge/launch_plan.py:L187-L213](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch_plan.py#L187-L213)。
2. 对下列三种 YAML 假设，分别推导 `requested="auto"`、`distributed=False` 时的返回值：
   - (a) `deployment.mode=local_colocated`，无 `resume_from`。
   - (b) `deployment.mode=disaggregated`，无 `resume_from`，`training.role=auto`。
   - (c) `deployment.mode=disaggregated`，`training.resume_from=<某 ckpt>`。
3. 再回答：若 (b) 的情况下进程其实跑在 torchrun 里（`distributed=True`），会发生什么？

**需要观察的现象**：

- (a) → `all`；(b) → `both`；(c) → `consumer`。
- (b) + `distributed=True` → 命中第 208-212 行的校验，抛 `ValueError`，要求显式 `--role consumer`。

**预期结果**：你应当能用一句话说出「colocated 永远是 all，disaggregated 默认 both，续训则退化成 consumer」。这条规则直接决定了下一节 `build_launch_plan` 走哪个分支。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `--role both` 不能用于续训（`resume_from`）？

> **参考答案**：续训是 consumer 的专属能力——只有 consumer 持有训练状态、检查点、SQLite 账本。producer 没有可恢复的训练状态，所以 `both`（含 producer）被禁止，必须用 `--role consumer`（见 204-207 行）。

**练习 2**：`node_rank` 有哪三个来源？优先级如何？

> **参考答案**：命令行 `--node-rank` > YAML `deployment.trainer.node_rank` > 环境变量 `NODE_RANK`。取到后必须满足 `0 ≤ node_rank < nnodes`，否则报错。多节点训练时若三者都没有，`_trainer_command` 会在 548-551 行抛「multi-node training requires --node-rank」。

---

### 4.3 build_launch_plan：把配置折叠成 LaunchPlan

#### 4.3.1 概念说明

有了角色和 node-rank，`build_launch_plan` 就能把一份 `Config`「折叠」成一个 `LaunchPlan`。这个函数是 launch 子系统的**总枢纽**，它要回答的核心问题是：

> 「我现在这个进程，到底该自己当 worker 训练，还是当 supervisor 去拉起别的进程？」

答案体现为 `plan.kind` 的四种取值，每种对应一种进程拓扑。

#### 4.3.2 核心流程

`build_launch_plan` 的主干可以这样概括（省略 managed_local/校验细节）：

```
1. 在线前置校验：online 必须 disaggregated + target_backend=sglang
2. 嗅探环境：distributed = 是否已在 torchrun 内
3. 派生角色：role = _resolve_role(cfg, requested, distributed)
4. 解析 node-rank（仅 all/consumer/both 需要）
5. 计算 disaggregated 角色环境（producer_env / consumer_env）
6. 一组跨进程校验（capture URL、consumer 数据库新鲜度）
7. ── 分支 ──
   a. distributed（已在 torchrun 内）         → LaunchPlan("worker", role, worker_env)
   b. role == producer（直跑生产者）           → LaunchPlan("worker", "producer", producer_env)
   c. role in (all, consumer):
        cmd = _trainer_command(...)
        若 cmd 就是裸 worker（单进程）          → LaunchPlan("worker", role, consumer_env)
        否则若 deployment 非空                  → LaunchPlan("command", role, [cmd], grace)
        否则                                   → LaunchPlan("command", role, [cmd])
   d. role == both（disaggregated supervisor）:
        若 managed_local 非空                  → LaunchPlan("managed_supervisor", both, [producer,consumer], services…)
        否则                                   → LaunchPlan("supervisor", both, [producer, consumer], grace)
```

关键直觉：**只要「最终要跑的训练命令」恰好就是当前进程自己（裸 worker，无需 torchrun 包装），plan.kind 就是 `worker`，cli 会进程内跑 `_train`；一旦需要套 torchrun、或要同时起 producer+consumer，plan.kind 就变成 `command`/`supervisor`，交给 `run_commands`。**

#### 4.3.3 源码精读

函数签名与文档串点明了「无副作用」的契约：

[specforge/launch_plan.py:L636-L647](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch_plan.py#L636-L647) —— `build_launch_plan` 入参：一份 `cfg` 加上 `algorithm`（managed_local 需要）、`config_path`、`overrides`、`requested_role`、`node_rank`、以及可注入的 `env/worker_prefix/torchrun_prefix`（后三者是为测试可注入性预留的接缝）。

第一步是在线模式的前置校验——online 不再支持 colocated：

[specforge/launch_plan.py:L649-L658](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch_plan.py#L649-L658) —— online 必须配 disaggregated 且 `target_backend=sglang`，否则 fail-fast。

派生角色与 node-rank 的调用点：

[specforge/launch_plan.py:L694-L705](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch_plan.py#L694-L705) —— `role = _resolve_role(...)`，并对 `both` 做单节点限制（自动 both 只支持 1 个 trainer 节点）。

**分支 a：已身处 torchrun**——退化成最简单的 worker plan，把角色对应的环境塞进去就返回：

[specforge/launch_plan.py:L737-L756](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch_plan.py#L737-L756) —— `distributed` 分支：校验 `WORLD_SIZE` 与拓扑一致后返回 `LaunchPlan("worker", role, worker_env)`。注意 producer 必须是单进程（739-743 行）。

进入「需要自己拉进程」的分支前，先确定两个命令前缀——默认走 `python -m`，且优先用 `--module` 形式让 torchrun 启动 specforge：

[specforge/launch_plan.py:L758-L765](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch_plan.py#L758-L765) —— `worker_prefix` 默认 `(python, -m, specforge.cli)`；`distributed_entry` 在未自定义 torchrun 时用 `("--module", "specforge.cli")`。

**分支 b：纯 producer**——producer 不初始化 CUDA、不建进程组，直接当 worker 进程内跑：

[specforge/launch_plan.py:L766-L767](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch_plan.py#L766-L767) —— `role=="producer"` 直接返回 worker plan。

**分支 c：all / consumer 的训练命令**——这是回答本讲核心实践题的地方。`_trainer_command` 根据 `world_size` 决定命令形态：

[specforge/launch_plan.py:L522-L539](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch_plan.py#L522-L539) —— `world_size = nnodes*nproc_per_node`；若 `world_size==1`，命令就是裸 worker（`CommandSpec(role, tuple(worker), env)`），**不带 torchrun**。

[specforge/launch_plan.py:L540-L566](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch_plan.py#L540-L566) —— `world_size>1` 时套 torchrun：单节点用 `--standalone`，多节点用 `--nnodes/--node_rank/--master_addr/--master_port`。

拿到 `command` 后，**判断它是不是裸 worker**，是则降级为 `worker` plan（进程内跑），否则升级为 `command` plan（交给 run_commands）：

[specforge/launch_plan.py:L768-L789](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch_plan.py#L768-L789) —— 关键判定 `command.argv[:len(worker_prefix)] == worker_prefix`（780-781 行）：若命令前缀就是裸 `python -m specforge.cli`，说明无需 torchrun 包装，返回 `worker` plan；否则返回 `command` plan。

> 这正是本讲实践题的答案核心：**`nnodes=1` 且 `nproc_per_node=1`（单卡 colocated）时 `world_size==1`，`_trainer_command` 返回裸 worker，于是 plan.kind=`worker`，cli 进程内跑 `_train`**；而 `nnodes=1` 但 `nproc_per_node>1`（单节点多卡）时 `world_size>1`，命令以 torchrun 开头，plan.kind=`command`，cli 调 `run_commands` 拉起 torchrun 子进程。

**分支 d：both（disaggregated supervisor）**——同时构造 producer 与 consumer 两条命令：

[specforge/launch_plan.py:L791-L827](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch_plan.py#L791-L827) —— `role=="both"`：构造 producer 命令（裸 worker）+ consumer 命令（`_trainer_command`）；若 `managed_local` 非空返回 `managed_supervisor`（附带本地 capture/Mooncake 服务列表），否则返回 `supervisor`。

`managed_supervisor` 与 `supervisor` 的区别在于：前者由 SpecForge **自己顺带拉起本地的 Mooncake 元数据服务和 SGLang capture server**（`_managed_local_services`），适合「一台机器上把 producer/consumer/服务全起起来」的本地在线实验；后者假设这些服务由外部提供，只起 producer+consumer 两个训练进程。

#### 4.3.4 代码实践

**实践目标**：回答本讲规格里指定的核心问题——「当 `deployment.trainer.nnodes=1` 且省略 `--role` 时，计划会生成哪两种角色进程，以及 `plan.kind` 如何决定 cli 走 `_train` 还是 `run_commands`」。

**操作步骤（源码阅读 + 计划预览）**：

1. 打开 [specforge/launch_plan.py:L768-L789](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch_plan.py#L768-L789) 与 [L791-L827](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch_plan.py#L791-L827)。
2. 区分三种「`nnodes=1` 且 `--role` 省略」的子情形，分别推导 plan：
   - **(I) colocated + 单卡**（`deployment.mode=local_colocated`，`nproc_per_node=1`）：`_resolve_role` 给 `all`；`world_size=1`；`_trainer_command` 返回裸 worker → `plan.kind=worker`。
   - **(II) colocated + 单节点多卡**（`nproc_per_node>1`）：`world_size>1`；命令以 torchrun 开头 → `plan.kind=command`，`commands` 里是一条 `torchrun --standalone --nproc_per_node N …`。
   - **(III) disaggregated**（在线或离线分离）：`_resolve_role` 给 `both`；构造 producer + consumer 两条命令 → `plan.kind=supervisor`（或 managed_local 时 `managed_supervisor`）。
3. 用 `--plan` 实测验证（以一个 disaggregated 配置为例）：

   ```bash
   specforge train --config examples/configs/qwen3-8b-eagle3-disaggregated.yaml --plan
   ```

4. 翻看 [specforge/cli.py:L258-L267](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py#L258-L267)，确认 `plan.kind` 的分流逻辑。

**需要观察的现象**：

- 子情形 (III) 的 `--plan` 输出里，`commands` 是**两条**：一条 `label=producer`（裸 worker，`--role producer`），一条 `label=consumer`（torchrun 包装，`--role consumer`）。**这两种角色进程就是 producer 与 consumer**。
- `kind` 在 (I) 是 `worker`，(II) 是 `command`，(III) 是 `supervisor`（或 `managed_supervisor`）。

**预期结果（对核心问题的标准答案）**：

> 省略 `--role` 即 `auto`。若配置是 **disaggregated**，`_resolve_role` 派生成 `both`，`build_launch_plan` 生成 **producer** 与 **consumer** 两种角色进程（见 [L791-L806](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch_plan.py#L791-L806)），`plan.kind` 为 `supervisor`/`managed_supervisor`，cli 据 `plan.kind != "worker"` 走 [run_commands](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py#L267)。若配置是 **colocated 且单卡**，`world_size==1`，命令退化为裸 worker，`plan.kind=worker`，cli 走 [`_train`](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py#L258-L266) 进程内训练；若 colocated 但单节点多卡，则 `plan.kind=command`，仍走 `run_commands` 拉 torchrun。

若本地无 GPU/无法运行 disaggregated 配置，请对 `--plan` 步骤标注「待本地验证」，源码推导部分不依赖运行。

#### 4.3.5 小练习与答案

**练习 1**：同样是 `nnodes=1`，为什么 colocated 单卡得到 `worker`，而单节点多卡得到 `command`？

> **参考答案**：分水岭是 `world_size = nnodes * nproc_per_node`。单卡时 `world_size==1`，`_trainer_command` 在 [L538-L539](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch_plan.py#L538-L539) 返回裸 worker（argv 前缀就是 `python -m specforge.cli`），命中 [L780-L781](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch_plan.py#L780-L781) 的判定降级为 `worker`；多卡时 `world_size>1` 必须套 torchrun，argv 前缀变成 torchrun，不再是裸 worker，故升级为 `command`。

**练习 2**：`supervisor` 与 `managed_supervisor` 的本质区别是什么？

> **参考答案**：`supervisor` 只起 producer+consumer 两个训练进程，假定 Mooncake/SGLang capture 服务由外部提供；`managed_supervisor` 额外由 SpecForge 自己拉起本地 Mooncake 元数据服务和若干 SGLang capture server（`_managed_local_services`），并带上 readiness 探测、端口预检、分阶段启动。前者用于生产/多机，后者用于「一台机器跑通在线 disaggregated」的本地实验。

**练习 3**：为什么 `build_launch_plan` 把 `env`、`worker_prefix`、`torchrun_prefix` 设计成可注入参数？

> **参考答案**：为了让单元测试可以在不污染真实 `os.environ`、不依赖具体 Python 解释器路径的前提下，构造确定性的 plan 并断言其 kind/role/commands。这是「计划无副作用」契约在测试侧的延伸（见 [L636-L647](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch_plan.py#L636-L647)）。

---

### 4.4 run_commands：执行 supervisor 计划

#### 4.4.1 概念说明

当 `plan.kind != worker` 时，当前进程的角色就从「训练者」变成了「监工（supervisor）」。`run_commands(plan)` 就是这个监工的执行体：它要按计划 spawn 子进程、等它们就绪、监控它们生死、在任何一个失败时优雅地收割其它子进程、并在收到 Ctrl-C/SIGTERM 时把信号转发给整组子进程再清理。这是 launch 子系统里**唯一有重副作用**的函数。

#### 4.4.2 核心流程

`run_commands` 的生命周期：

```
1. 守卫：plan.kind == "worker" 直接报错（worker 该由 cli 进程内跑）
2. 安装信号转发器（INT/TERM/HUP → _ForwardedSignal）
3. 若 managed_supervisor：
     a. _managed_preflight：预检 mooncake_master/patched sglang/端口可用
     b. 建 logs 目录，按 phase 分阶段 spawn 服务并等 readiness
4. spawn 所有 command（commands 列表）
5. 轮询所有子进程 poll()：
     - 任一非 0 退出 → 记 first_failure，SIGTERM 收割其余，清空 remaining
     - managed_supervisor 还监控 services 退出
6. 全部退出后：managed_supervisor 停服务
7. finally：恢复调用方信号处理器
异常路径（_ForwardedSignal / BaseException）：
     → _terminate_processes + _stop_services，再返回 128+signum 或 re-raise
```

#### 4.4.3 源码精读

入口与 worker 守卫——worker plan 绝不能进 `run_commands`：

[specforge/launch_plan.py:L1049-L1067](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch_plan.py#L1049-L1067) —— `run_commands` 签名与 worker 守卫。`popen`/`managed_preflight`/`readiness_waiter` 都是可注入接缝，便于测试用假 Popen 验证调度逻辑。

子进程 spawn 用 `start_new_session=True` 把每个子进程放进独立进程组，便于整组发信号：

[specforge/launch_plan.py:L986-L1000](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch_plan.py#L986-L1000) —— `_spawn_command`：合并父环境与命令 env，`start_new_session=True` 建独立会话/进程组。

信号转发的关键设计——**第一个信号启动优雅拆除，后续信号忽略**，避免第二次 Ctrl-C 打断清理而孤儿化 capture 孙进程：

[specforge/launch_plan.py:L1074-L1095](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch_plan.py#L1074-L1095) —— `forward_signal` 把 INT/TERM/HUP 翻译成 `_ForwardedSignal`（一个 `BaseException`），使外层 `except` 能跑清理代码。

managed_supervisor 的分阶段启动——先起 phase 0（Mooncake），等它 ready 再起 phase 1（capture servers）：

[specforge/launch_plan.py:L1096-L1108](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch_plan.py#L1096-L1108) —— 按 phase 排序，逐阶段 spawn 服务并调用 `readiness_waiter` 阻塞等待健康检查通过。

主监控循环——任一子进程失败立即收割其余，保证「要么全成功，要么快速失败」：

[specforge/launch_plan.py:L1109-L1146](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch_plan.py#L1109-L1146) —— spawn 所有 command 后轮询；首个非 0 退出码记为 `first_failure` 并触发 `_terminate_processes` 收割剩余进程；managed_supervisor 额外把「服务异常退出」也算失败。

优雅终止的实现——先 SIGTERM 给宽限期（`shutdown_grace_s`，让 worker 做 Mooncake drain、checkpoint flush），超时再 SIGKILL，还兼顾「组 leader 已被 OOM 杀但子进程仍在」的孤儿场景：

[specforge/launch_plan.py:L848-L891](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch_plan.py#L848-L891) —— `_terminate_processes`：TERM→等 grace→KILL 的两段式，并对 `exited_group_leaders` 单独处理已死 leader 的后代。

#### 4.4.4 代码实践

**实践目标**：通过阅读测试理解 `run_commands` 的「首个失败即收割」语义，而不必真起 disaggregated 训练。

**操作步骤**：

1. 在 `tests/` 下搜索针对 `run_commands` / `LaunchPlan` 的单元测试（关键词 `run_commands`、`build_launch_plan`）。
2. 找到「构造一个会失败的 command + 一个正常 command」的用例，阅读其断言：
   - 失败 command 的退出码是否被 `first_failure` 记录？
   - 正常 command 是否被提前 SIGTERM 收割？
3. 对照 [L1113-L1127](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch_plan.py#L1113-L1127) 确认轮询逻辑。

**需要观察的现象**：

- 测试通常用「假 popen」（注入一个返回预设退出码的 callable）来验证：当某个 command 返回非 0 时，`run_commands` 的返回值等于该退出码，且其它 command 的进程组收到了 SIGTERM。

**预期结果**：你能描述出 supervisor 的失败语义——「**一损俱损但有序**」：任何一个子进程失败，supervisor 不会让其它子进程继续空转，而是给它们 `shutdown_grace_s` 的宽限做清理后再 SIGKILL。这正是 disaggregated 训练里 producer/consumer 不会互相孤儿化的保障。若 `tests/` 中未找到对应用例，请标注「待确认」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `run_commands` 在收到第一个 SIGTERM 后要忽略后续信号，直到清理完成？

> **参考答案**：清理本身（Mooncake drain、checkpoint flush、发 SIGTERM 给子进程组）需要时间。若不忽略后续信号，用户的第二次 Ctrl-C 会打断 `finally` 清理，导致 capture server 孙进程被孤儿化、共享显存泄漏。忽略期间父进程仍可用 SIGKILL 强制终结（见 [L1074-L1081](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch_plan.py#L1074-L1081) 注释）。

**练习 2**：`shutdown_grace_s` 这个窗口是给谁用的？

> **参考答案**：给被 SIGTERM 的 worker 用的。worker（见 [u3-l1](u3-l1-cli-entry.md) 的 `_worker_signal_unwind`）收到 SIGTERM 后需要完成 Mooncake 排空、检查点落盘、失败哨兵发布等清理，`shutdown_grace_s`（默认 30s，managed_local 用自己的值）就是 supervisor 在 SIGKILL 之前留给这些工作的最长时间。

---

## 5. 综合实践

把本讲四条线索串起来：**配置 → 派生角色 → 折叠成 plan → 分流执行**。

任务：为下面三种运行意图，分别给出「会得到的 `plan.kind` / `role` / 进程数」并写出对应的 `--plan` 验证命令。

| 运行意图 | deployment.mode | nnodes | nproc_per_node | data 模式 | 预期 kind | 预期 role | 进程数 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| (A) 单卡离线 colocated 训练 | local_colocated | 1 | 1 | offline | ？ | ？ | ？ |
| (B) 单机 4 卡离线 colocated 训练 | local_colocated | 1 | 4 | offline | ？ | ？ | ？ |
| (C) 单机在线 disaggregated 训练 | disaggregated | 1 | 2（consumer） | online | ？ | ？ | ？ |

要求：

1. 先**只看源码**推导（参考 4.3.3 的分支），把「？」填出来。
2. 再用 `specforge train --config <对应配置> --plan` 实测对照（A/B 可用 offline 配置；C 用 disaggregated 配置，如 `examples/configs/qwen3-8b-eagle3-disaggregated.yaml`，配合 `--plan` 不占 GPU）。
3. 对 (C)，从 `--plan` 的 JSON 里数 `commands` 数组长度，确认它确实包含 producer 与 consumer **两种角色进程**，并指出哪条命令带 torchrun、哪条不带。

**参考答案（先自己推导再对照）**：

- (A) `kind=worker`，`role=all`，进程数=1（cli 进程内跑 `_train`）。
- (B) `kind=command`，`role=all`，进程数=1 条 torchrun 命令（由 torchrun 再拉起 4 个 rank）。
- (C) `kind=supervisor`（或 managed_local 时 `managed_supervisor`），`role=both`，`commands` 含 2 条：`producer`（裸 worker，无 torchrun）+ `consumer`（torchrun `--standalone --nproc_per_node 2`）。进程数=2 条命令（consumer 那条再衍生 2 个 rank）。

通过这个表，你应当彻底厘清「`plan.kind` 如何决定 cli 走 `_train` 还是 `run_commands`」：只有 `worker` 走 `_train`，其余三种（`command`/`supervisor`/`managed_supervisor`）都走 `run_commands`。

---

## 6. 本讲小结

- `build_launch_plan` 把一份已校验 `Config` **无副作用**地折叠成一个不可变的 `LaunchPlan` 值对象；`--plan` 只是把它 `render()` 成脱敏 JSON，不起进程、不占 GPU。
- `plan.kind` 有四种取值：`worker`（进程内跑）、`command`（套 torchrun 单进程组）、`supervisor`（起 producer+consumer）、`managed_supervisor`（额外起本地 Mooncake/capture 服务）。
- 角色派生在 `_resolve_role`：colocated→`all`、disaggregated→`both`、续训→`consumer`；`both` 是 launch 专属，YAML 的 `training.role` 不含它。
- `nnodes=1` 不是判断 `worker`/`command` 的依据，**`world_size = nnodes * nproc_per_node` 才是**：`world_size==1` 的裸 worker 降级为 `worker`，否则升级为 `command`。
- `cli.py` 的分流极简：`plan.kind == "worker"` → `_train`（进程内）；否则 → `run_commands(plan)`（当 supervisor 拉子进程）。
- `run_commands` 的核心语义是「首个失败即优雅收割」：任一子进程非 0 退出就 SIGTERM 其余（给 `shutdown_grace_s` 宽限做清理）再 SIGKILL，并靠「首个信号后忽略后续信号」保证清理不被打断。

## 7. 下一步学习建议

- 本讲只讲了 plan **怎么构造和执行**，但 producer/consumer 两条命令拉起后，各自的「应用装配」是如何接续的？请继续读 [u3-l4 应用组合根 composition](u3-l4-composition-root.md)，看 `build_application_run` 如何衔接到 `training.assembly`。
- 想理解四个 `build_*` 运行时构建器（offline / disagg_offline / disagg_online_producer / disagg_online_consumer）如何与 plan 的四种 kind 对应，请读 [u3-l3 拓扑构建器 launch](u3-l3-topology-builders.md)。
- 对 plan 里 `consumer_env` 注入的那些 `DISAGG_*` 环境变量（如 `DISAGG_DB`、`DISAGG_REF_CHANNEL`）如何被运行时消费，留到 [u7 DataFlow 运行时](u7-l1-runtime-architecture.md) 展开。
