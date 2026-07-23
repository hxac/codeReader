# 检查点与恢复

## 1. 本讲目标

训练一个草稿模型往往要跑成千上万个 optimizer step，中途机器宕机、被抢占、或主动改参数重跑都是常态。本讲聚焦 SpecForge 如何把「训练进度」安全地落到磁盘、又如何精确地把它读回来。读完本讲你应当能够：

- 说出 SpecForge 一个检查点在磁盘上长什么样、保存时如何回卷（rewind）与轮转（rotation）。
- 区分 `training.resume_from`（完整续训）与 `model.draft_checkpoint_path`（仅权重热启动）这两种**刻意分开**的检查点操作，并说明它们各自恢复/不恢复哪些状态。
- 复述在线 disaggregated 训练「只恢复 consumer、durable ack 必须等于 checkpoint step」这条恢复契约的来龙去脉。
- 看懂 `CheckpointManager` 的核心方法，并能写出两条真实的 resume / warm-start 命令。

本讲依赖 [u6-l3 Trainer 与 TrainerController](u6-l3-trainer-loop.md) 中建立的 `global_step`、optimizer 边界、`TrainerController` 循环等概念，请先确认你已经理解「训练流自然结束必须落在 optimizer 边界」这一点。

## 2. 前置知识

- **optimizer step（全局步）**：一次完整的梯度更新。SpecForge 里所有「步数」——`global_step`、学习率/损失调度、日志、保存、Domino 的 lambda 衰减——都以**已完成的 optimizer 更新**为单位。详见 [u6-l3](u6-l3-trainer-loop.md)。
- **FSDP 与 DDP**：SpecForge 的训练后端（[u6-l4](u6-l4-fsdp-backend.md)）。关键在于：FSDP 把优化器的 fp32 master 权重**分片到每个 rank**，而 DDP 的优化器状态在所有 rank 上**完全一致**。这决定了存盘格式——前者每个 rank 各存一份，后者只存一份。
- **online / offline 与 disaggregated**：在线训练特征捕获交外部 SGLang 完成，恒为 disaggregated（producer + consumer 分离）；离线训练从预计算特征读取。详见 [u2-l1](u2-l1-first-run.md) 与 [u7-l1](u7-l1-runtime-architecture.md)。
- **durable ack**：在线 consumer 在 optimizer 边界写一笔「这些样本已被消费、梯度已提交」的持久化确认，由 rank0 单一权威写 SQLite 账本。详见 [u7-l2](u7-l2-control-plane.md) 与 [u7-l4](u7-l4-online-ref-distribution.md)。
- **原子写**：先写到 `.tmp` 再 `os.replace`，保证读者永远只看到完整的文件，不会读到写了一半的状态。

## 3. 本讲源码地图

本讲的核心是「磁盘上的检查点生命周期」，主要源码集中在以下几个文件：

| 文件 | 作用 |
| --- | --- |
| [specforge/training/checkpoint.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/checkpoint.py) | 唯一的检查点管理器 `CheckpointManager`：目录布局、保存/回卷/轮转、best 跟踪、resume 读取。 |
| [specforge/training/trainer.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/trainer.py) | `Trainer` 装配时读取 resume 状态、校验一致性、把进度喂给 `TrainerController`；`fit()` 结束时做最终保存。 |
| [specforge/training/controller.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/controller.py) | `TrainerController.save_checkpoint`：组装 state_dict、区分 rank0 共享负载与每 rank 本地负载。 |
| [specforge/training/model_loading.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/model_loading.py) | 热启动 `warm_start_draft_model`：仅读权重、不读优化器/计数器/RNG。 |
| [specforge/config/schema.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py) | `save_interval` / `max_checkpoints` / `resume_from` / `draft_checkpoint_path` 字段定义及互斥校验。 |
| [specforge/launch_plan.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch_plan.py) | 在线 consumer 恢复的数据库新鲜度校验与角色派生。 |
| [docs/basic_usage/training.md](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/docs/basic_usage/training.md) | 「两种检查点操作」「Checkpoints and resume」两节是权威行为说明。 |
| [docs/basic_usage/disaggregated_training.md](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/docs/basic_usage/disaggregated_training.md) | 在线 disaggregated 恢复与新鲜度规则。 |

## 4. 核心概念与源码讲解

### 4.1 检查点在磁盘上的布局与轮转（checkpoint 轮转）

#### 4.1.1 概念说明

SpecForge 的检查点不是单一文件，而是一个**目录**。每存一次 `step N`，就在 `output_dir` 下创建一个名为 `{run_id}-step{N}/` 的目录。除此之外，目录旁边还散落着几类「指针」和「元数据」：

```
output_dir/
├── myrun-step1000/                 # 一个完整检查点
│   ├── training_state.pt          # rank0 写的共享负载
│   └── training_state_rank0.pt    # 每 rank 各自的本地状态
│   └── training_state_rank1.pt
├── myrun-step2000/
├── myrun-latest  -> myrun-step2000   # 符号链接，指向最新完整检查点
├── myrun-best    -> myrun-step1500   # 符号链接，指向评测最优检查点
└── myrun.best_meta.json              # best 的元数据（step/score/metric）
```

这套设计的核心目标是：**可轮转（rotate）但绝不丢最优**。`max_checkpoints` 控制最多保留几个检查点，旧的会被删；但 `best` 指向的那个即便最旧也不会被删。同时，保存采用「fork/回卷」语义：保存 step S 会先删掉磁盘上所有 ≥ S 的检查点——这避免了从旧 checkpoint 分叉后又生成了新分叉造成的「时间线错乱」。

#### 4.1.2 核心流程

一次 `save(state, step)` 的流程如下（多 rank 时是一次 collective 操作）：

1. rank0 执行 `_rewind(step)`：删掉所有 ≥ step 的检查点目录；若被删范围里包含 best，则清空 best 指针与元数据。
2. `_barrier()`：所有 rank 同步，确保旧目录删干净后才开始重建。
3. 创建 `{run_id}-step{step}/` 目录。
4. 每个 rank 原子写自己的 `training_state_rank{r}.pt`（FSDP 分片的优化器/RNG）。
5. rank0 原子写共享负载 `training_state.pt`。
6. `_all_ok(err)`：用 `all_gather_object` 汇总所有 rank 的错误，**任何一个 rank 失败则全员抛错**（防止某个 rank 写失败而其他 rank 继续，造成悬挂的进程组）。
7. rank0 把 `{run_id}-latest` 指向新目录，并执行 `_rotate(keep_step=step)`。
8. 再次 `_barrier()`：保证检查点完整后才放行。

轮转逻辑很简单：把所有完整检查点按 step 排序，只保留最新的 `max_checkpoints` 个，但**永远跳过 `best_step` 和当前 `keep_step`**。

#### 4.1.3 源码精读

先看整个文件的「设计契约」注释，它把布局讲得一清二楚：

[specforge/training/checkpoint.py:9-16](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/checkpoint.py#L9-L16) —— 说明一个检查点是 `{run_id}-step{N}/` 目录，含 rank0 共享 `training_state.pt` 加每个 rank 的 `training_state_rank{r}.pt`；FSDP 优化器与 RNG 是 rank 局部的，DDP 复制的优化器状态只存一份。

目录命名只依赖 `run_id` 和 `step`：

[specforge/training/checkpoint.py:55-56](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/checkpoint.py#L55-L56) —— `checkpoint_dir(step)` 直接拼出 `{run_id}-step{step}`。

`save` 主方法是 collective 的核心：

[specforge/training/checkpoint.py:70-107](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/checkpoint.py#L70-L107) —— 先 rewind、barrier、原子写 rank 文件与共享文件、`_all_ok` 汇总错误、最后 repoint latest 并 rotate。

回卷语义的关键——保存 step S 会让 step ≥ S 的旧目录全部失效：

[specforge/training/checkpoint.py:109-129](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/checkpoint.py#L109-L129) —— `_rewind` 删除所有 `s >= step` 的目录；若 `best_step >= step`，连同 best 元数据与符号链接一起清空。这是「fork 语义」：从一个更早的点重新分叉，旧的未来必须作废。

多 rank 容错靠 `_all_ok` 取代裸 barrier：

[specforge/training/checkpoint.py:131-144](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/checkpoint.py#L131-L144) —— `all_gather_object` 让每个 rank 知道所有 rank 的结果，任一 rank 文件系统失败就在全员抛 `RuntimeError`，避免「一个 rank 挂了、其余继续」导致的进程组搁浅。

轮转永远保护 best 与当前步：

[specforge/training/checkpoint.py:444-455](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/checkpoint.py#L444-L455) —— `_rotate` 只在 `max_checkpoints > 0` 时生效，删除最旧的超出部分，但 `step == best_step` 或 `step == keep_step` 的目录 `continue` 跳过；删除失败只警告不抛错（不在 collective 之间抛异常）。

符号链接用相对目标，方便整个 `output_dir` 搬到别的挂载点：

[specforge/training/checkpoint.py:428-442](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/checkpoint.py#L428-L442) —— `_point` 创建符号链接时用 `os.path.basename(ckpt_dir)` 作相对目标；若文件系统不支持 symlink 则静默跳过，此时 step 目录与 best 元数据仍是权威来源。

「完整检查点」的判定标准是目录里必须有 `training_state.pt`：

[specforge/training/checkpoint.py:468-472](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/checkpoint.py#L468-L472) —— `_all_checkpoints` 只 yield 持有 `STATE_FILE` 的目录，一次写了一半的截断目录永远不会成为 latest/rotate/best 的目标。

best 跟踪由 `is_better`（rank0 裁决后广播）+ `update_best`（重指 best 链接并持久化元数据）配合完成：

[specforge/training/checkpoint.py:165-182](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/checkpoint.py#L165-L182) —— `update_best` 把 best 链接指向该 step 目录，并把 `run_id/step/score/metric/metrics` 原子写入 `best_meta.json`。重启时由 `_load_best_meta` 读回，防止重启后「轮转掉磁盘上的 best」或「让更差的分数覆盖最优」（见 [checkpoint.py:184-212](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/checkpoint.py#L184-L212)）。

#### 4.1.4 代码实践

**实践目标**：在不实际训练的前提下，理解 `save_interval` / `max_checkpoints` 如何控制检查点的产生与轮转。

**操作步骤**：

1. 打开 [docs/basic_usage/training.md:409-441](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/docs/basic_usage/training.md#L409-L441)，阅读「Checkpoints and resume」一节，记下三句话：①`save_interval` 控制频率；②`max_checkpoints` 控制轮转；③**一个完成的 trainer run 总会保存最终运行时检查点**，即便 `save_interval=0` 或最后一步不是 interval 边界。
2. 查看字段默认值：[specforge/config/schema.py:531](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L531)（`save_interval` 默认 0）与 [specforge/config/schema.py:535-536](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L535-L536)（`max_checkpoints` 默认 0 = 全部保留）。
3. 追踪最终保存的触发点：[specforge/training/trainer.py:528-529](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/trainer.py#L528-L529) —— `fit()` 结束后若 `step > 0 and last_checkpoint_step != step` 就调用 `save_checkpoint`，这就是「总存最终检查点」的来源。
4. 追踪 interval 与 best 触发点：[specforge/training/controller.py:611-622](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/controller.py#L611-L622) —— `interval_hit or is_best` 二者之一为真就保存，`is_best` 还会额外 `update_best`。

**需要观察的现象**：`save_interval=0`（默认）时，训练过程中途不落盘，只有结束那一次保存——所以**默认配置下中途崩溃无法 resume**。

**预期结果**：你能解释「为什么想要断点续训就必须显式设一个正的 `save_interval`」，并知道 best 检查点即便 `save_interval=0` 也会在评测改进时写盘（因为 `is_best` 触发 `save_checkpoint`）。

> 待本地验证：以上命令与字段均来自源码与文档，但「实际跑一个 `save_interval=100` 的小训练、观察目录里出现多个 `*-step*` 与 `*-latest` 符号链接」需要本地 GPU 环境验证。

#### 4.1.5 小练习与答案

**练习 1**：假设 `max_checkpoints=3`，磁盘上已有 step 1000/2000/3000，且 best 指向 step 1000。现在保存 step 2500，结束后磁盘上还剩哪几个检查点？

**答案**：保存 step 2500 会先 `_rewind` 删除 ≥ 2500 的目录（即 step 3000 被删），然后创建 step 2500。此时完整检查点为 1000/2000/2500，共 3 个，未超过 `max_checkpoints=3`，无需轮转。剩下 step 1000（best，受保护）、2000、2500。`*-latest` 指向 step2500，`*-best` 仍指向 step1000。

**练习 2**：为什么 `save` 方法里要在创建新目录前后各放一次 `_barrier()`？

**答案**：第一次 `_barrier()` 在 rewind 之后，确保所有 ≥ step 的旧目录已被 rank0 删除，任何 rank 才开始重建同名目录，避免竞态；第二次 `_barrier()` 在 latest 重指与 rotate 之后，保证检查点完整（所有 rank 都写完、指针都指好）后才放行训练继续，防止下游读到写了一半的状态。

---

### 4.2 两种检查点操作：resume vs warm start（resume vs warm start）

#### 4.2.1 概念说明

SpecForge 刻意把「恢复训练」分成两个互不相同的操作，对应两个字段，且**互斥**：

| 意图 | 配置字段 | 恢复的状态 |
| --- | --- | --- |
| 继续同一个 run | `training.resume_from` | 草稿权重 + 优化器/调度器 + epoch/step/数据位置 + 每 rank RNG |
| 用权重初始化一个新 run | `model.draft_checkpoint_path` | **仅草稿权重** |

直觉上区分：

- **resume** 是「按下播放键继续看同一部电影」：进度条（global_step、epoch、数据吃到哪）、播放器状态（优化器动量、学习率调度）、随机数种子全部原样恢复，接着上一次的 step 往下走。
- **warm start** 是「换一张新光盘，但沿用上次的播放器设置从头放」：只借用草稿权重当起点，其余全部重置——优化器从零开始、`global_step` 归零、数据从头吃、RNG 重新初始化。

为什么要分这么清？因为把它们混用是危险的：如果「只想热启动权重」却顺手恢复了优化器动量，新的训练目标会带着为旧目标调好的动量起步，行为不可预期；反过来如果「想续训」却只读了权重，会丢失数据位置导致重复训练或跳过样本。SpecForge 用两个字段把意图焊死，并在 schema 层强制互斥。

#### 4.2.2 核心流程

**resume 的读取流程**（`CheckpointManager.read_resume_state`）：

1. `resolve_resume_dir` 把用户给的路径（检查点目录、`training_state.pt`、`file://` URI、或含单个 run 的 output 根）解析到唯一的检查点目录。
2. `torch.load` 读共享负载 `training_state.pt`。
3. 多 rank 时 `all_gather_object` 交换「读到了什么」的描述符（run_id/global_step/strategy），校验**所有 rank 读到的是同一个检查点、同一个身份**，任一 rank 读不了就全员报错。
4. 读本 rank 的 `training_state_rank{r}.pt`，挂到 `state["backend"]`；DDP 复制优化器情形从共享负载的 `replicated_optimizer_state` 回填。

**resume 在装配期如何用**（`trainer.py`）：

1. `read_resume_state` 拿到 `state`。
2. 一致性校验：`strategy` 必须匹配、`world_size` 等关键契约字段必须一致、`effective_total_steps` 必须可证明匹配（否则报错让用户用原配置 resume）。
3. **在 FSDP wrap 之前**用 `load_state_dict(strict=False)` 把草稿权重塞进模型（这样优化器 fp32 master 在 build 时就从这些权重克隆）。
4. FSDP wrap 之后，`backend.load_state_dict(state["backend"])` 恢复本 rank 的优化器/RNG 分片。
5. 把 `global_step`/`epoch`/`epoch_batch`/`epoch_samples` 作为 `start_*` 喂给 `TrainerController`，并把 `last_checkpoint_step` 设为 `resume["global_step"]`（避免 no-op fit 把已完成的检查点重写成带改动的版本）。

**warm start 的读取流程**（`model_loading.warm_start_draft_model`）：

1. `_runtime_state_file` 解析出 `training_state.pt`（或识别为 HF 目录走 `from_pretrained`）。
2. 用 `weights_only=True` 的受限反序列化**只读 `draft_state_dict`**，丢弃其余一切。
3. 校验 `strategy` 一致后，把权重 load 进草稿模型。

#### 4.2.3 源码精读

文档里把这两个操作讲得最直接的是这张表：

[docs/basic_usage/training.md:163-181](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/docs/basic_usage/training.md#L163-L181) —— 明确列出两种操作的意图、字段与恢复状态，并强调 warm start 不恢复优化器/计数器/数据位置/RNG，且与 `resume_from` 互斥。

字段定义与互斥校验：

[specforge/config/schema.py:42-44](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L42-L44) —— `draft_checkpoint_path` 注释直说「只加载草稿权重，不像 `resume_from` 那样恢复优化器/调度器/计数器/数据位置/RNG」。

[specforge/config/schema.py:698-703](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L698-L703) —— 两个字段同时出现直接抛 `ValueError`，因为「weights-only warm start 与 full resume 互斥」。

resume 读取的核心——`read_resume_state`：

[specforge/training/checkpoint.py:224-320](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/checkpoint.py#L224-L320) —— 读共享负载 + 本 rank 的 backend 分片；多 rank 时用 `all_gather_object` 校验全员读到同一身份的检查点；若该 rank 无可用 per-rank 状态且 `require_full_state` 为真，则报错提示「需按原 world size resume，或传 `require_full_state=False` 只恢复权重和计数器」。

路径解析——绝不悄悄在多个 run 间选择：

[specforge/training/checkpoint.py:322-375](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/checkpoint.py#L322-L375) —— `resolve_resume_dir` 优先用唯一的 `*-latest` 指针；找不到时只在「所有候选属于同一 run_id」时才退回取最高 step；多个 run 共存则报错要求显式指定。

装配期的 resume 校验与权重加载（先于 FSDP wrap）：

[specforge/training/trainer.py:281-292](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/trainer.py#L281-L292) —— 读 resume 状态并校验 `strategy` 必须与本次训练一致；不一致直接拒绝（防止用 eagle3 的 checkpoint 续训 dflash）。

[specforge/training/trainer.py:342-358](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/trainer.py#L342-L358) —— 用 `strict=False` 加载草稿权重（仅允许 provider 声明的省略，如 EAGLE3 冻结的目标 embedding），其余任何 mismatch 都判为检查点损坏。

[specforge/training/trainer.py:416-427](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/trainer.py#L416-L427) —— **FSDP wrap 之后**才 `backend.load_state_dict(resume["backend"])` 恢复优化器/RNG 分片；wrap 必须在前，因为分片状态要在分片结构建好之后才能对号入座。

进度喂给控制器并防 no-op 重写：

[specforge/training/trainer.py:466-477](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/trainer.py#L466-L477) —— `start_step/start_epoch/start_batch/start_samples` 来自 resume；并把 `last_checkpoint_step` 设为 `resume["global_step"]`，这样若 fit() 因已完成或触及 `max_steps` 而成为 no-op，不会把已有检查点重写成 epoch 计数被改过的版本。

warm start 只读权重：

[specforge/training/model_loading.py:344-369](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/model_loading.py#L344-L369) —— `_load_specforge_draft_state` 用 `weights_only=True` 的受限 unpickler，**只接受张量与基本元数据、拒绝任意对象**，且只取 `draft_state_dict`，这就是 warm start「绝不碰优化器/计数器/RNG」的实现保证。

#### 4.2.4 代码实践

**实践目标**：写出两条真实命令，分别完成 resume 与 warm start，并说明二者恢复状态的差异。这是本讲的主线实践任务。

**操作步骤**：

1. **resume（续训一个离线 run）**：复用 [examples/configs/qwen3-8b-eagle3-offline.yaml](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/examples/configs/qwen3-8b-eagle3-offline.yaml)，用命令行覆盖追加 `training.resume_from`（来自 [docs/basic_usage/training.md:422-426](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/docs/basic_usage/training.md#L422-L426)）：

   ```bash
   specforge train \
     --config examples/configs/qwen3-8b-eagle3-offline.yaml \
     training.resume_from=./outputs/qwen3-8b-eagle3-offline/qwen3-8b-eagle3-offline-latest
   ```

2. **warm start（从旧权重热启动新 run）**：新建一个 YAML（或复用示例并改 `run_id`/`output_dir`），在 `model` 段填 `draft_checkpoint_path`（来自 [docs/basic_usage/training.md:177-181](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/docs/basic_usage/training.md#L177-L181)）：

   ```yaml
   model:
     target_model_path: Qwen/Qwen3-8B
     draft_checkpoint_path: ./outputs/base/base-step1000
   run_id: qwen3-8b-eagle3-warmstart
   output_dir: outputs/qwen3-8b-eagle3-warmstart
   ```

   ```bash
   specforge train --config my-warmstart.yaml
   ```

3. **对照差异**：按 [specforge/training/checkpoint.py:224-320](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/checkpoint.py#L224-L320) 与 [specforge/training/model_loading.py:344-369](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/model_loading.py#L344-L369) 分别列出二者读取的字段。

**需要观察的现象 / 预期结果**：

| 维度 | `resume_from` | `draft_checkpoint_path` |
| --- | --- | --- |
| 草稿权重 | ✅ 恢复 | ✅ 恢复 |
| 优化器/调度器状态 | ✅ 恢复 | ❌ 重置 |
| `global_step` | ✅ 接着上次 | ❌ 归零 |
| epoch/数据位置 | ✅ 恢复（精确到样本） | ❌ 从头 |
| 每 rank RNG | ✅ 恢复 | ❌ 重新初始化 |
| 字段位置 | `training.resume_from` | `model.draft_checkpoint_path` |
| 互斥性 | 二者不可同时出现（[schema.py:698-703](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L698-L703)） | 同左 |

一句话总结：resume 是「同一条训练时间线的延续」，warm start 是「新时间线借用旧权重」。

> 待本地验证：上述命令均摘自官方文档与示例，但实际跑通需要本地 GPU、目标模型与离线特征；在没有这些资源时可先 `--plan` 预览确认配置被正确解析。

#### 4.2.5 小练习与答案

**练习 1**：你用一个 `eagle3` 的 checkpoint，给一个 `strategy: dflash` 的 run 当 `resume_from`，会发生什么？

**答案**：装配期 [trainer.py:287-292](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/trainer.py#L287-L292) 会比对 `state["strategy"]` 与本次 `algorithm_name`，二者不等就抛 `ValueError`（「checkpoint 由 strategy 'eagle3' 写成，本次训练 'dflash'」）。SpecForge 不允许跨算法 resume。

**练习 2**：为什么 resume 时草稿权重要在 FSDP wrap **之前**加载，而优化器分片要在 wrap **之后**加载？

**答案**：FSDP wrap 时会从模型参数克隆出 fp32 master 权重构建优化器，所以权重必须先就位，优化器 master 才能从正确权重起步（注释见 [trainer.py:277-279](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/trainer.py#L277-L279)）；而优化器/RNG 的分片状态是按 FSDP 分片结构组织的，必须等分片结构建好后才能对号入座，所以 `backend.load_state_dict` 在 wrap 之后（[trainer.py:425-427](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/trainer.py#L425-L427)）。

**练习 3**：把 `draft_checkpoint_path` 指向一个 Hugging Face 草稿模型目录（而非 SpecForge checkpoint）可以吗？

**答案**：可以。warm start 接受 HF 模型目录/仓库、SpecForge checkpoint 目录、`training_state.pt` 或 run 根（见 [docs/basic_usage/training.md:170-175](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/docs/basic_usage/training.md#L170-L175)）；若该源含 `config.json`，还会顺便提供草稿架构（除非显式给了 `model.draft_model_config`）。

---

### 4.3 在线 disaggregated 的 consumer-only 恢复契约（online 恢复契约）

#### 4.3.1 概念说明

离线训练的数据是固定的特征文件，resume 时重建迭代器、seek 到上次位置即可，producer 根本不存在（或离线 producer 只是「灌一次特征」）。但在线 disaggregated 训练不同：producer 实时用 SGLang 捕获特征、consumer 实时消费。这里恢复面临一个根本难题——**到底从哪里恢复**。

SpecForge 的回答是：**只恢复 consumer，producer 永不 resume**。原因有二：

1. producer 的「状态」是外部 SGLang 服务的运行时，SpecForge 无法也不该去快照它；
2. 在线训练的「进度真相」不在 producer，而在 consumer 的 **durable ack 账本**——它精确记录了「哪些样本的梯度已被提交」。

因此在线恢复复用三样**留存物**：①rank0 的 SQLite 元数据库（含 WAL/SHM 边车）；②原始引用通道与 inbox；③Mooncake 里的特征对象；外加一个**匹配的 checkpoint**。恢复时 rank0 校验「durable optimizer marker 等于 checkpoint step」，跳过已 ack 的样本前缀，重放未 ack 的尾部。

#### 4.3.2 核心流程

在线 consumer 恢复的判定与校验分两层：

**启动计划层（`launch_plan.py`）**：

1. `_resolve_role`：disaggregated 配置若含 `resume_from`，默认 `--role auto` 会自动派生为 `consumer`；显式 `--role both` 配合 `resume_from` 被拒绝（producer 不能 resume）。
2. `_validate_consumer_database`：在线 consumer 恢复时，rank0（state owner）必须能找到留存的 SQLite 数据库；否则报错。而非恢复的新 attempt 则相反——rank0 不能发现任何残留的 db/wal/shm（freshness 契约）。

**运行时层（consumer 恢复语义）**：

1. rank0 打开留存的 SQLite 账本，读到 durable optimizer marker（= 上次最后 ack 的 step）。
2. 校验 `marker == checkpoint.global_step`：这是硬约束。若 ack 已推进到 1050、但最近 checkpoint 在 1000，说明 1000→1050 之间的样本「已消费、梯度已提交」但 checkpoint 没跟上——这种错位被**故意拒绝**，宁可失败也不拿旧 optimizer 状态重放已消费样本，或悄悄跳过 optimizer 状态。
3. 校验通过后，consumer 从 checkpoint 恢复 optimizer/权重/global_step，并跳过账本里已 ack 的样本前缀、重放未 ack 的尾部。producer 不重启。

这就是为什么文档反复强调：「因为 ack 可能在两次周期性 checkpoint 之间推进，那一窗口内的崩溃被故意拒绝；若该恢复窗口重要，请用更频繁的 `save_interval`。」

#### 4.3.3 源码精读

角色派生——resume 自动落到 consumer：

[specforge/launch_plan.py:187-211](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch_plan.py#L187-L211) —— `_resolve_role`：disaggregated 且 `cfg.training.resume_from` 存在时派生为 `consumer`（[L195-196](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch_plan.py#L195-L196)）；`--role both` 与 resume 共存被显式拒绝，提示「producer 不能 resume，请用 consumer」（[L204-206](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch_plan.py#L204-L206)）。同时 schema 层也禁止 producer 角色带 resume：[specforge/config/schema.py:832-833](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L832-L833)。

数据库新鲜度校验——恢复要留存、新 attempt 要干净：

[specforge/launch_plan.py:592-608](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch_plan.py#L592-L608) —— `_validate_consumer_database`：`resume_from` 存在时，state owner（rank0）若找不到数据库就报「consumer resume requires the retained metadata database」（[L592-598](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch_plan.py#L592-L598)）；非恢复情形则要求 db/wal/shm 全不存在，否则报「must use a fresh attempt path」（[L599-608](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch_plan.py#L599-L608)）。

权威行为说明（文档）：

[docs/basic_usage/disaggregated_training.md:410-433](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/docs/basic_usage/disaggregated_training.md#L410-L433) —— 「Resume and freshness」一节：每个新在线 attempt 的 consumer db 与 WAL/SHM 不得存在；resume 需留存数据库 + 匹配的 checkpoint；durable optimizer-step ack 必须等于 checkpoint step；ack 可能在周期性 checkpoint 之间推进，该窗口内的崩溃被故意拒绝。

[docs/basic_usage/training.md:432-438](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/docs/basic_usage/training.md#L432-L438) —— 在线 disaggregated resume 是 consumer-only：复用留存的 SQLite、原始 channel/inboxes、Mooncake 对象与匹配 checkpoint；rank0 校验 durable optimizer marker 等于 checkpoint step、跳过已 ack refs、重放未 ack 尾部；producer 不重启；当前要求相同 trainer world size（控制面 ref 重分发不等于 optimizer 状态重分片）。

> 说明：durable ack 的「两段式 collective + rank0 单一写权威」机制本身在 [u7-l4](u7-l4-online-ref-distribution.md) 详述，本讲只关注它与 checkpoint 的边界对齐契约。

#### 4.3.4 代码实践

**实践目标**：理解在线 consumer 恢复命令的形态与它所依赖的留存物，并能解释「为什么 ack 与 checkpoint step 错位会被拒绝」。

**操作步骤**：

1. 阅读在线 resume 命令形态（[docs/basic_usage/disaggregated_training.md:416-419](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/docs/basic_usage/disaggregated_training.md#L416-L419)）：

   ```bash
   specforge train -c run.yaml --role consumer \
     training.resume_from=outputs/run/run-latest
   ```

2. 列出该命令成功所必须**原样复用**的留存物（来自 [docs/basic_usage/training.md:432-438](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/docs/basic_usage/training.md#L432-L438)）：control 目录、store id、run id、output 目录、consumer 拓扑（world size）、Mooncake 对象、留存的 SQLite 账本、匹配的 checkpoint。
3. 推演一个错位场景：假设 `save_interval=500`，consumer 在 step 1000 存了 checkpoint，然后 ack 推进到 1300 才崩溃。问：用 step1000 的 checkpoint 能 resume 吗？

**需要观察的现象 / 预期结果**：

- 命令中**只起 consumer**，没有 producer 进程（producer 不 resume）。
- 第 3 步的错位场景**会被拒绝**：durable marker=1300 ≠ checkpoint step=1000。因为 step 1000→1300 之间的样本「已 ack（梯度已提交）」，但 checkpoint 的 optimizer 状态停在 1000，若强行 resume 会用 1000 的 optimizer 状态重放这 300 步已消费样本，造成重复训练。SpecForge 选择 fail-closed。
- 解法：把 `save_interval` 调小（如 100），让 checkpoint 与 ack 的差距始终在一个可接受窗口内；或在 ack 与 checkpoint 对齐的那个点崩溃才能 resume。

> 待本地验证：在线恢复需要真实 Mooncake + patched SGLang 服务栈，本实践以源码阅读与文档推演为主；仓库的 e2e 门禁 `scripts/gates/run_disaggregated_overfit_gate.sh`（见 [docs/basic_usage/disaggregated_training.md:325-334](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/docs/basic_usage/disaggregated_training.md#L325-L334)）是本地可跑的完整验证入口。

#### 4.3.5 小练习与答案

**练习 1**：为什么在线 disaggregated resume 不重启 producer？

**答案**：producer 的「状态」是外部 patched SGLang 服务的运行时，SpecForge 既无法快照也不应接管；而在线训练的进度真相（哪些样本梯度已提交）记录在 consumer 侧的 durable ack SQLite 账本里。所以恢复只需 consumer：它复用留存账本与匹配 checkpoint，跳过已 ack 前缀、重放未 ack 尾部，producer 重新跑只会再捕获一遍。

**练习 2**：把在线 consumer 的 `nproc_per_node` 从 4 改成 8 再 resume，行不行？

**答案**：当前不行。[docs/basic_usage/training.md:437-438](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/docs/basic_usage/training.md#L437-L438) 明确：optimizer/FSDP checkpoint 目前要求**相同的 trainer world size**，控制面的 ref 重分发并不等于 optimizer 状态的重分片（reshard）。要改 world size 只能 warm start 一个新 run。

---

## 5. 综合实践

把三个模块串起来，设计一个「崩溃 → resume → warm start」的端到端推演任务（源码阅读型，无需 GPU）：

**背景**：你有一个离线 EAGLE3 run，`run_id=myrun`，`save_interval=200`，`max_checkpoints=3`，已正常跑到 step 1000 后被抢占。`output_dir` 里留有 step 400/600/800/1000（注意轮转）以及 `myrun-latest`、一次评测改进产生的 `myrun-best`（指向 step 600）。

**任务**：

1. **轮转推演**：按 [checkpoint.py:444-455](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/checkpoint.py#L444-L455) 推算，`max_checkpoints=3` 下磁盘实际保留哪几个 step 目录？`myrun-best` 指向的 step 600 会不会被轮转删掉？
2. **resume 续训**：写一条命令从最新检查点续训（提示：用 `*-latest` 指针）。说明恢复后 `global_step`、优化器动量、数据位置各是什么状态。
3. **warm start 新 run**：另起一个新 `run_id=myrun-v2`，想借用 step 600（best）的权重从头训。写出 `model.draft_checkpoint_path` 配置，并说明新 run 的 `global_step`、优化器、数据位置状态。
4. **在线对照**：若这是**在线** disaggregated run 而非离线，步骤 2 的 resume 命令要加什么参数？producer 会不会一起启动？ack 与 checkpoint step 必须满足什么关系？

**参考要点**：

1. 轮转保留最新 3 个完整检查点，但 best 受保护。完整检查点为 400/600/800/1000 共 4 个 > 3，应删最旧的 step 400；但需保留 best(step600) 与 latest 指向的 step1000。最终保留 step 600（best）、800、1000，`myrun-latest`→step1000。step 400 被删。
2. `specforge train -c myrun.yaml training.resume_from=./outputs/myrun/myrun-latest`。恢复后 `global_step=1000`、优化器动量/调度器原样恢复、数据位置精确到样本（epoch/epoch_samples 恢复），接着 step 1001 往下训。
3. 新 YAML 里 `model.draft_checkpoint_path: ./outputs/myrun/myrun-best`（或 `myrun-step600`），`run_id: myrun-v2`，`output_dir: outputs/myrun-v2`。新 run `global_step=0`、优化器从零、数据从头，只草稿权重来自 step 600。
4. 在线需 `--role consumer`（或 `--role auto` 自动派生为 consumer）；producer 不启动；durable optimizer marker 必须等于 checkpoint step。

## 6. 本讲小结

- SpecForge 的一个检查点是 `output_dir/{run_id}-step{N}/` 目录，含 rank0 共享 `training_state.pt` 与每 rank 的 `training_state_rank{r}.pt`；FSDP 优化器/RNG 是 rank 局部的，DDP 复制优化器状态只存一份。
- 保存采用 **fork/回卷语义**：保存 step S 先删所有 ≥ S 的目录；`_rotate` 按 `max_checkpoints` 只留最新 N 个，但 **best 与当前步永远受保护**；多 rank 保存是 collective，任一 rank 失败全员抛错。
- 两种检查点操作刻意分开且互斥：`training.resume_from` 恢复**权重+优化器+计数器+数据位置+RNG**（续训），`model.draft_checkpoint_path` 只恢复**草稿权重**（热启动新 run）。
- resume 的一致性校验很严：strategy 必须匹配、world_size 等契约字段必须一致、`effective_total_steps` 必须可证明匹配；草稿权重在 FSDP wrap **之前**加载，优化器分片在 wrap **之后**加载。
- 在线 disaggregated 恢复是 **consumer-only**：复用留存的 SQLite 账本、channel/inboxes、Mooncake 对象与匹配 checkpoint；**durable ack step 必须等于 checkpoint step**，错位即 fail-closed；producer 不重启；当前要求相同 trainer world size。
- 想要中途可恢复，必须显式设正的 `save_interval`；但完成的 run 总会存最终检查点，且 best 检查点即便 `save_interval=0` 也会在评测改进时写盘。

## 7. 下一步学习建议

- 阅读 [u9-l2 优化器与学习率调度](u9-l2-optimizer-scheduler.md)：理解 resume 时为何要校验 `effective_total_steps`，以及 `max_steps`/`total_steps` 的 horizon 语义。
- 阅读 [u9-l4 导出、评测与基准](u9-l4-export-eval-benchmark.md)：本讲的 `*-best` 检查点与 `eval/simulated_acc_len` 指标会在那里被 `specforge export` 物化为服务目录。
- 回看 [u7-l2 控制平面与元数据账本](u7-l2-control-plane.md) 与 [u7-l4 在线引用分发](u7-l4-online-ref-distribution.md)：durable ack 的「两段式 collective + rank0 单一写权威」机制是本讲在线恢复契约的基石。
- 想做扩展实践，可阅读 [tests/test_runtime/test_checkpoint_manager.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/tests/test_runtime/test_checkpoint_manager.py) 与 [tests/test_runtime/test_checkpoint_resume.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/tests/test_runtime/test_checkpoint_resume.py)，它们用最小断言覆盖了轮转、rewind、resume 一致性等行为。
