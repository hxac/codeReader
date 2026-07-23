# 拓扑构建器 launch

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `specforge/launch.py` 暴露的**四个拓扑构建器**（`build_offline_runtime` / `build_disagg_offline_runtime` / `build_disagg_online_producer` / `build_disagg_online_consumer`）各自负责哪一种运行拓扑。
- 复述一条真实调用链：从组合根 `build_application_run` → `build_training_run`，到按 `deployment.mode` 与 `data` 段推导出的「数据模式 × 部署模式」组合，最终落到哪一个 `build_*` 构建器。
- 解释为什么三个**承载训练器**的构建器会汇聚到同一条 `Trainer → FeatureDataLoader → TrainerController → TrainerCore` 路径，以及为什么在线 producer 是唯一的例外。
- 区分 producer 与 consumer 在「参考源（ref source）」「特征存储后端」「元数据账本」三处的差异。

本讲是 [u3-l2 启动计划 launch_plan](u3-l2-launch-plan.md) 的承接：`launch_plan` 解决「进程拓扑计划」（起几个进程、谁是 supervisor），而 `launch.py` 解决「进程内部的运行时装配」（每个进程真正要构建一个什么样的训练器或采集器）。两者名字相近但层级不同。

## 2. 前置知识

在进入源码前，先建立三个直觉。

### 2.1 两条正交的轴线

SpecForge 的运行形态由两条**正交的轴线**决定（这点在 [u2-l1 五分钟跑通一次训练](u2-l1-first-run.md) 已经建立）：

- **数据模式（online / offline）**：由 `data` 段是否填 `hidden_states_path` 决定。offline 吃预计算好的特征文件；online 让目标模型「现场」捕获特征。
- **部署模式（colocated / disaggregated）**：由 `deployment.mode` 决定。colocated 指特征捕获与训练在同一个进程里完成；disaggregated 指二者拆成 producer 与 consumer 两组进程。

两条轴线交叉出四种组合，但 SpecForge **有意不支持 online colocated**——在线捕获只能交给外部的 SGLang 服务器，所以在线训练永远 disaggregated。于是真实存在的拓扑只有三种：colocated offline、disaggregated offline、online（disaggregated）。

### 2.2 什么是「构建器（builder）」

这里的「构建器」就是一个普通的 Python 函数：输入一组已经装配好的零件（草稿模型、目标头、优化器工厂、特征存储……），输出一个可以 `.run()` / `.fit()` 的训练器（trainer），或者（在线 producer 的情况下）一个可以驱动的采集驱动器。它不做配置解析、不查算法名——这些都在上游的**组合根**里完成。`launch.py` 文件头一句话点明了它的定位：

> Internal wiring helpers used by the application composition root.
> （组合根使用的内部装配助手。）

### 2.3 为什么需要四个而不是一个

如果只有一种拓扑，一个 `build_trainer` 就够了。但三种拓扑在「特征从哪来」「要不要跨进程账本」「能不能多轮迭代」上差异很大，硬塞进一个函数会满是 `if online / if disaggregated` 分支。SpecForge 的取舍是：**把拓扑差异封进四个独立的构建器，把拓扑无关的共同训练逻辑抽到一个共享装配函数 `_assemble_trainer`**。三个承载训练器的构建器都调用它，于是「训练主循环」永远只有一份代码。

> 关键术语：**拓扑构建器（topology builder）**、**组合根（composition root）**、**参考源（ref source）**、**特征存储（feature store）**、**元数据账本（metadata ledger）**、**承载训练器的构建器（trainer-bearing builder）**。这些词会贯穿全讲。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [`specforge/launch.py`](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch.py) | **本讲主角**。暴露四个 `build_*` 拓扑构建器与共享的 `_assemble_trainer`。 |
| [`specforge/runtime/ARCHITECTURE.md`](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/ARCHITECTURE.md) | 运行时架构说明书，列出四条支持路径与「统一训练路径」结论，是本讲的权威参照。 |
| [`specforge/application/composition.py`](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/application/composition.py) | 组合根 `build_application_run`，把已解析的 run 交给 `build_training_run`。 |
| [`specforge/training/assembly.py`](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/assembly.py) | `build_training_run`：第一道分发器，colocated 离线直接调 `build_offline_runtime`，disaggregated 交给 `disaggregated.py`。 |
| [`specforge/training/disaggregated.py`](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/disaggregated.py) | 第二道分发器，按 `role` 把 producer/consumer 分别路由到剩余三个构建器。 |

## 4. 核心概念与源码讲解

### 4.1 四个构建器全景与调度链路

#### 4.1.1 概念说明

`launch.py` 用 `__all__` 明确且仅导出四个名字，这就是「四个拓扑构建器」的权威清单：

```python
__all__ = [
    "build_offline_runtime",
    "build_disagg_offline_runtime",
    "build_disagg_online_producer",
    "build_disagg_online_consumer",
]
```

`ARCHITECTURE.md` 开篇给出与之一一对应的清单和一句贯穿全讲的结论：

> The launch layer exposes exactly four topology builders … All trainer-bearing builders converge on the same `Trainer -> FeatureDataLoader -> TrainerController -> TrainerCore` path. Only the reference source and feature-store backend change.

这句话有两个信息点：

1. **四个构建器对应四种「角色 × 拓扑」**：离线 colocated（单角色）、离线 disagg consumer、在线 producer、在线 consumer。
2. **其中三个是「承载训练器的」**（offline、disagg offline、online consumer），它们汇聚到同一条训练路径；**在线 producer 是唯一例外**，它不训练，只采集。

#### 4.1.2 核心流程：谁调用谁

四个构建器**不直接被 `cli.py` 调用**，而是经过两道分发器。完整链路如下：

```text
cli._train
  └─ build_application_run(resolved).run()          # composition.py 组合根
       └─ build_training_run(cfg, algorithm)         # assembly.py 第一道分发
            ├─ deployment.mode == "disaggregated"
            │    └─ build_disaggregated_run(cfg)      # disaggregated.py 第二道分发
            │         ├─ offline + role==producer  → TrainingRun(execute=produce)  ※ 直接 ingest，不调构建器
            │         ├─ offline + role==consumer  → build_disagg_offline_runtime
            │         ├─ online  + role==producer  → build_disagg_online_producer
            │         └─ online  + role==consumer  → build_disagg_online_consumer
            └─ colocated（非 disaggregated）
                 └─ mode == "offline"  → build_offline_runtime
                 └─ mode == "online"   → 报错（online 仅服务端，不支持 colocated）
```

有一个**容易被忽略的细节**值得单独记住：离线 disaggregated 的 **producer 并不调用任何 `build_*` 构建器**。它直接走 `ingest_offline_features` 把现成的特征文件灌进跨进程存储、写一份静态 manifest（清单）就结束。只有 consumer 才用 `build_disagg_offline_runtime` 去读这份 manifest 并训练。这与在线 producer（必须用 `build_disagg_online_producer` 驱动采集池）形成对照。

#### 4.1.3 源码精读

**第一道分发：`assembly.py` 按 `deployment.mode` 切分。**

`build_training_run` 先做两道硬校验——在线必须是 disaggregated、colocated 只支持离线——然后分流：[assembly.py:573-604](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/assembly.py#L573-L604)

```python
if cfg.mode == "online" and cfg.deployment.mode != "disaggregated":
    raise ValueError("online training is server-only and requires deployment.mode='disaggregated'")
if cfg.deployment.mode == "disaggregated":
    from specforge.training.disaggregated import build_disaggregated_run
    return build_disaggregated_run(cfg, algorithm=algorithm, ...)
if cfg.mode != "offline":
    raise ValueError("colocated execution supports offline training only")
```

剩余的 colocated 离线分支直接在本进程内装配模型包并调用 [`build_offline_runtime`](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/assembly.py#L606-L633)（调用点在 [assembly.py:612](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/assembly.py#L612)）。

**第二道分发：`disaggregated.py` 按 `mode` 再按 `role` 切分。**

[`build_disaggregated_run`](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/disaggregated.py#L762-L796) 先断言 `role` 只能是 `producer` 或 `consumer`，再按 `mode` 进入 `_build_offline` 或 `_build_online`：[disaggregated.py:777-792](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/disaggregated.py#L777-L792)

```python
if cfg.mode == "offline":
    return _build_offline(cfg, ...)
return _build_online(cfg, ...)
```

而 `_build_offline` / `_build_online` 内部再用 `cfg.training.role` 把 producer 与 consumer 分别引向不同的构建器（或离线 producer 的直接 ingest 路径）。在线两端的关键调用点分别在 [disaggregated.py:610](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/disaggregated.py#L610)（producer）与 [disaggregated.py:729](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/disaggregated.py#L729)（consumer），离线 consumer 在 [disaggregated.py:444](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/disaggregated.py#L444)。

#### 4.1.4 代码实践：手工「演算」一次分发

1. **实践目标**：不看运行结果，仅凭配置字段判断一次 run 会落到哪个构建器。
2. **操作步骤**：
   - 打开 [`examples/configs/`](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/examples/configs) 下任意一个 YAML。
   - 读出三个字段：`data.hidden_states_path` 是否非空、`deployment.mode`、`training.role`（若未写则按 [u2-l2](u2-l2-config-sections.md) 的规则推导）。
   - 沿着 4.1.2 的流程图走到底，写下构建器名。
3. **需要观察的现象**：`data.hidden_states_path` 非空 → offline；为空且填了 prompts → online。
4. **预期结果**：例如 `qwen3-8b-eagle3-disaggregated.yaml` 同时填了 prompts 与 `deployment.mode: disaggregated`、`role` 省略，则推导为 online disaggregated，且 `role` 在 launch 层被展开为 producer 与 consumer 两个进程，分别对应 `build_disagg_online_producer` 与 `build_disagg_online_consumer`。
5. **待本地验证**：可用 `specforge train --config <yaml> --plan` 零开销确认你推导出的两个进程命令（参见 [u2-l3 plan 预览](u2-l3-overrides-and-plan.md)）。

#### 4.1.5 小练习与答案

- **练习 1**：为什么 `launch.py` 要导出**四个**构建器，而不是一个带 `if online` 分支的大函数？
  - **答案**：三种真实拓扑在参考源、特征存储、账本、是否可重迭代上差异巨大；拆成四个独立函数后，**拓扑差异被封在各自函数里，而拓扑无关的训练主循环只保留一份**（`_assemble_trainer`），避免分支爆炸也便于单独测试。
- **练习 2**：在线 disaggregated 的 producer 进程会调用 `build_disagg_online_producer`，那离线 disaggregated 的 producer 进程调用哪个 `build_*`？
  - **答案**：**一个都不调用**。离线 producer 直接走 `ingest_offline_features` + `write_ref_manifest` 把现成特征灌入跨进程存储并写清单（[disaggregated.py:363-432](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/disaggregated.py#L363-L432)），因为离线特征已经是预算好的，不需要「采集」。

### 4.2 离线两条路：`build_offline_runtime` 与 `build_disagg_offline_runtime`

#### 4.2.1 概念说明

两个离线构建器都吃「固定的、可重复迭代的」参考列表（refs），区别只在**特征存在哪**：

- `build_offline_runtime`（colocated 离线）：特征就在本机磁盘上的预计算文件里，用 `LocalFeatureStore` 通过 `file://` 引用直接读。
- `build_disagg_offline_runtime`（disaggregated 离线 consumer）：特征由 producer 灌进了一个跨进程存储（共享目录 `SharedDirFeatureStore` 或 Mooncake），consumer 拿到的是 producer 写好的 manifest 里的 `disagg://` 引用。

正因为特征是「固定且可重迭代的」，两条路都**不需要训练账本、不需要引用队列**：refs 列表是静态的，想重跑一个 epoch 重新切片即可，断点续训也能 seek 到已持久化的位置。`ARCHITECTURE.md` 的支持路径表把这一点写得很清楚——离线两行都标注「Re-iterable; checkpoint resume supported」。

#### 4.2.2 核心流程

两个离线构建器的装配步骤几乎一致，可以归纳为：

```text
1. 校验 tp_size == 1            # 离线 consumer 不做 trainer 张量并行
2. 解析算法专属的 collator / normalizer   # _offline_io
3. 读出固定的 source_refs        # colocated: build_reader().read()；disagg: 直接 list(refs)
4. 构造 refs_for_epoch(epoch)   # 每个 epoch 按 seed+epoch 重新分片
5. 建 DataFlowController(NoOpMetadataStore, enable_sample_queue=False)   # 无账本、无队列
6. 建 FeatureStore              # LocalFeatureStore（colocated）或外部传入（disagg）
7. _assemble_trainer(...)       # 汇入统一训练路径，durable_ack=False
```

「按 epoch 重新分片」由 `_shard_offline_refs` 实现，它复刻了 PyTorch `DistributedSampler(drop_last=False)` 的索引生成逻辑（见 [launch.py:218-238](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch.py#L218-L238)），保证多个 DP 副本看到不相交的样本、而 USP（序列并行）对等组看到同一样本。

#### 4.2.3 源码精读

**`build_offline_runtime`（colocated 离线）**：[launch.py:511-633](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch.py#L511-L633)。关键三处：

```python
_validate_offline_trainer_tp(tp_size)                       # 离线强制 tp=1
controller = DataFlowController(
    run_id, metadata_store=NoOpMetadataStore(), enable_sample_queue=False)   # 无账本
source_refs = provider.build_reader(hidden_states_path, ...).read()         # 读固定 refs
...
return _assemble_trainer(...,
    ref_source={"refs": refs, "refs_for_epoch": refs_for_epoch},            # 可重迭代
    store=..., durable_ack=False, ...)                                      # 不需要 durable ack
```

**`build_disagg_offline_runtime`（disagg 离线 consumer）**：[launch.py:636-755](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch.py#L636-L755)。它的函数文档一句话点明与 colocated 的关系：

> Same trainer assembly as the colocated offline path, so results match within determinism tolerance.
> （与 colocated 离线路径使用相同的训练器装配，因此结果在确定性容差内一致。）

差别仅有两处：refs 不再来自 `build_reader().read()`，而是直接 `list(refs)`（producer 写好的 manifest）；store 不再是 `LocalFeatureStore`，而是 caller 传入的跨进程 `feature_store`：[launch.py:685-721](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch.py#L685-L721)

```python
source_refs = list(refs)                          # 来自 manifest
...
return _assemble_trainer(...,
    store=feature_store,                          # 外部传入的跨进程存储
    ref_source={"refs": refs, "refs_for_epoch": refs_for_epoch},
    durable_ack=False, ...)
```

**离线 trainer TP 校验**：[launch.py:502-508](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch.py#L502-L508) 把 `tp_size != 1` 直接判为错误——离线 consumer 靠 DP（每个非 SP rank 各拿一份数据分片）并行，不再叠加目标模型张量并行。

#### 4.2.4 代码实践：对照两条离线路径

1. **实践目标**：确认「离线两条路训练器装配完全一致，只有 refs 来源与 store 不同」。
2. **操作步骤**：并排打开 [`build_offline_runtime`](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch.py#L511-L633) 与 [`build_disagg_offline_runtime`](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch.py#L636-L755)，逐行比对 `_assemble_trainer(...)` 调用的关键字参数。
3. **需要观察的现象**：除了 `store=` 与 refs 来源，其余参数（collate_fn、num_epochs、resume_from、checkpoint_extra、tp/sp…）是否逐项相同。
4. **预期结果**：两者的 `_assemble_trainer` 调用参数集**完全一致**（连 `checkpoint_extra` 里的 `offline_sampler_version`/`sampler_seed`/`source_dataset_size` 都一样），这正是「结果在确定性容差内一致」的工程保证。
5. **待本地验证**：仓库里有专门的等价测试 `tests/test_runtime/test_colocated_vs_disagg_equiv.py`，可阅读其断言确认这一承诺。

#### 4.2.5 小练习与答案

- **练习 1**：离线两条路都用 `NoOpMetadataStore` 与 `enable_sample_queue=False`，为什么？
  - **答案**：离线 refs 是**固定且可重迭代**的，没有「在线消费一次即销毁」的问题，自然不需要账本记录「哪些样本已被确认」、也不需要样本队列做去重与背压。账本与队列是在线 consumer 才需要的。
- **练习 2**：若用户给一份离线 YAML 配了 `training.tp_size=2`，会在哪一步、以什么报错失败？
  - **答案**：在构建器入口 [`_validate_offline_trainer_tp`](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch.py#L502-L508) 抛 `ValueError`，提示「offline feature consumers do not implement trainer tensor parallelism; keep tp_size=1」。

### 4.3 在线分离两端：`build_disagg_online_producer` 与 `build_disagg_online_consumer`

#### 4.3.1 概念说明

在线 disaggregated 把「捕获目标模型特征」与「训练草稿模型」彻底拆成两组进程：

- **producer（采集端）**：`build_disagg_online_producer` 不训练，只负责驱动若干 `RolloutWorker` 把 prompt 发给外部 SGLang 服务器、让服务器直接把张量写进 Mooncake，再把**只含元数据的引用**通过 channel 流给 consumer。它返回的是 `(workers, drive_producer)`，而不是 trainer。
- **consumer（训练端）**：`build_disagg_online_consumer` 训练，但它吃的不是固定 refs，而是一条**消费一次即销毁**的流。每个 rank 有自己的 inbox，rank 0 还额外跑引用分发器（`RefDistributor`）与唯一一份 SQLite 账本。

由于「消费一次」，在线 consumer **只迭代一遍**（`num_epochs=1`），且断点续训是 **consumer-only** 的——producer 不能续跑，只会重起一次全新的采集。

#### 4.3.2 核心流程

**producer 端**（`drive_producer`）的主循环可以概括为：

```text
1. 等待 consumer 通过 channel 广播「全局 optimizer 窗口」quantum
   quantum = dp_size * batch_size * accumulation_steps
2. 校验自身 in-flight 高/低水位都 >= quantum        # 否则双方会互相死等
3. 启动 N 个 worker（单 worker 内联；多 worker 每个一线程）
4. 循环：每个 worker 租约 prompt → 捕获 → 写存储 → 发布引用
   - 发布前按「常驻字节硬上限」与「in-flight 水位」做背压（暂停/恢复）
   - 失败的 prompt 重试，连续失败到阈值则把该 worker 踢出轮换
5. prompt 池耗尽（pending=0 且 leased=0）后正常关闭 channel；失败则发失败哨兵
```

**consumer 端**的关键是**两个不变量**：

```text
- 只有 rank 0 读 channel、只有 rank 0 写 SQLite 账本（单一写账本）
- 每个 rank 一个 inbox；optimizer 边界时所有 rank 同步 ack，由 rank 0 落一笔 durable 事务
```

`ARCHITECTURE.md` 的支持路径表把在线这一行标为「Consume once; consumer-only recovery reconciles retained state; no producer resume or second pass」——这三点直接对应上面的流程。

#### 4.3.3 源码精读

**`build_disagg_online_producer`**：[launch.py:764-1308](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch.py#L764-L1308)。两个关键点：

其一，producer **等待并校验 quantum**，这是 producer 与 consumer 的握手契约：[launch.py:977-995](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch.py#L977-L995)

```python
if in_flight_high_watermark < consumer_quantum:
    raise ValueError("producer in-flight high watermark ... is smaller than the "
                     "consumer's global optimizer-step quantum ...")
```

quantum 的值由 consumer rank 0 在装配时广播：[launch.py:1492-1495](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch.py#L1492-L1495)

```python
channel.publish_consumer_quantum(dp_size * batch_size * accumulation_steps, ...)
```

其二，producer **不构造 trainer**，而是返回 `(workers, drive_producer)`，由上层包成一个 `TrainingRun(execute=produce)`（在 [disaggregated.py:645-721](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/disaggregated.py#L645-L721) 把 `drive` 接到 `produce` 闭包里）。注意它**从不调用 `_assemble_trainer`**——这就是「四个构建器里唯一不汇聚到 Trainer」的那一个。

**`build_disagg_online_consumer`**：[launch.py:1311-1651](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch.py#L1311-L1651)。它的 preflight（起飞前校验）体现了「在线不变量」：[launch.py:1382-1401](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch.py#L1382-L1401)

```python
if metadata_store is None and metadata_db_path is None:
    raise ValueError("online consumer needs a metadata_store/metadata_db_path ...")
if getattr(feature_store, "retain_on_release", False) is not True:
    raise ValueError("online consumer feature_store must set retain_on_release=True; "
                     "features are deleted only after an optimizer-boundary durable ack")
if distributed and world != dp_size:
    raise ValueError("... every rank must own exactly one inbox")
```

四条约束分别要求：**必须有持久账本**、**特征必须在 ack 前保留**、**每个 rank 恰好一个 inbox**。`_dp_consumer_layout` 还会拒绝 `dp_size>1` 时嵌套目标 TP 或草稿 SP（[launch.py:355-361](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch.py#L355-L361)）。

最终 consumer 把一条**流式队列**交给统一训练路径，且与离线的「refs 字典」形态截然不同：[launch.py:1602-1606](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch.py#L1602-L1606)

```python
ref_source={
    "queue": queue,
    "prepositioned": resume_from is not None,
    "defer_ack_until_durable": True,
},
```

#### 4.3.4 代码实践：推算 quantum 与水位

1. **实践目标**：用一个具体配置算出 producer 与 consumer 握手的 quantum，并判断默认水位是否够用。
2. **操作步骤**：假设 `dp_size=4`、`batch_size=2`、`accumulation_steps=2`。
3. **需要观察的现象**：代入 [launch.py:1493](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch.py#L1493) 的公式 quantum = dp_size × batch_size × accumulation_steps。
4. **预期结果**：

   \[\text{quantum} = 4 \times 2 \times 2 = 16\]

   即 consumer 每个 optimizer 步需要 16 条引用。producer 默认 `in_flight_high_watermark=256`（由 `DISAGG_IN_FLIGHT_HIGH_WATERMARK` 覆盖，见 [ARCHITECTURE.md](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/ARCHITECTURE.md#L97-L107)「Optimizer-window handshake」），256 ≥ 16 满足；若用户把 high watermark 设成 8，就会在 [launch.py:977-983](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch.py#L977-L983) 报错。
5. **待本地验证**：实际 producer 启动日志会打印 `[producer-timing] ... quantum=16 ...`（见 [launch.py:996-998](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch.py#L996-L998)），可对照确认。

#### 4.3.5 小练习与答案

- **练习 1**：为什么在线 consumer 要求 `feature_store.retain_on_release=True`？
  - **答案**：在线特征「消费一次」，但只有到 **optimizer 边界的 durable ack** 之后才能安全删除。若 release 即删，一旦某个 micro-batch 在 ack 前失败，已消费的特征就丢了、无法重放。`retain_on_release=True` 保证特征活到 ack 落库之后（[launch.py:1387-1391](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch.py#L1387-L1391)）。
- **练习 2**：`build_disagg_online_producer` 为什么返回的不是 trainer？
  - **答案**：producer 只采集、不训练，没有模型、没有优化器、没有损失。它返回 `(workers, drive_producer)`，由 `disaggregated.py` 包成 `TrainingRun(execute=produce)` 复用统一生命周期钩子（`on_success/on_failure/on_finally`），但 `execute` 跑的是采集驱动而非 `trainer.fit()`。

### 4.4 统一训练路径汇聚：`_assemble_trainer` 与 `Trainer`

#### 4.4.1 概念说明

三个承载训练器的构建器（`build_offline_runtime`、`build_disagg_offline_runtime`、`build_disagg_online_consumer`）最终都调用同一个 `_assemble_trainer`，它再构造唯一的 `Trainer`。这就是 `ARCHITECTURE.md` 反复强调的那条统一路径：

```text
Trainer → FeatureDataLoader → TrainerController → TrainerCore
```

含义是：无论离线还是在线、colocated 还是 disaggregated，**训练主循环只有一份**。拓扑差异在上游被「翻译」成两个变量喂给这条路径：

- **ref_source**：refs 字典（可重迭代，离线）或 queue 字典（消费一次，在线）。
- **store / controller**：本地存储 + 空账本（离线）或 Mooncake + SQLite/DPAckController（在线）。

#### 4.4.2 核心流程

`_assemble_trainer` 的职责可以拆成三步：

```text
1. 规范化 strategy_kwargs
   - 若 resume_from 非空，必须带 provider 绑定的 StepRuntimeConfig 与模型溯源契约
   - 否则包成一个从零训练用的 StepRuntimeConfig
2. 构造 Trainer(...)     # 把 controller / store / ref_source / model / optimizer_factory
                          #   / tp / sp / 水位 / profiling / 生命周期钩子一次性注入
3. 返回 trainer           # 上层 TrainingRun.run() 调 trainer.fit()
```

关键设计是 `ref_source` 这个参数——它是一个**普通字典**，用键的不同来区分两种数据形态：

| `ref_source` 形态 | 含义 | 用于 |
| --- | --- | --- |
| `{"refs": [...], "refs_for_epoch": fn}` | 固定、可重迭代的引用列表 | 两个离线构建器 |
| `{"queue": q, "prepositioned": ..., "defer_ack_until_durable": True}` | 流式队列、消费一次 | 在线 consumer |

`Trainer` 只看这个字典的键，**不关心**张量是从本地文件还是 Mooncake 来的——这正是「trainer 对传输后端无感」的落点（详见后续 [u7-l3 数据平面](u7-l3-data-plane-stores.md)）。

#### 4.4.3 源码精读

**`_assemble_trainer` 的签名与文档**：[launch.py:39-82](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch.py#L39-L82)。它的 docstring 一句话点明它是「the one assembly」：

```python
"""Delegate to the domain ``Trainer`` (``specforge.training``) — the one
assembly (FSDP wrap, optimizer-after-wrap, per-step strategy, loader, acks)
shared by every trainer-bearing builder."""
```

**strategy_kwargs 的规范化**：[launch.py:89-111](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch.py#L89-L111)。续训路径要求必须是 provider 绑定的 `StepRuntimeConfig`，否则一个「从零训练产出的检查点」后续无法被续训——这是一个有意保留的强约束。

**构造 Trainer**：[launch.py:113-153](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch.py#L113-L153)。把 `make_step_strategy=algorithm.providers.step.build`、`controller`、`store`、`ref_source` 等全部注入，最后返回 trainer。三个承载训练器的构建器对它的调用点分别是 [launch.py:595](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch.py#L595)（colocated 离线）、[launch.py:717](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch.py#L717)（disagg 离线）、[launch.py:1598](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch.py#L1598)（在线 consumer）——三处调用同一个函数，是「统一训练路径」最直接的代码证据。

**ARCHITECTURE.md 的权威表述**：[ARCHITECTURE.md:12-14](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/ARCHITECTURE.md#L12-L14) 与支持路径表 [ARCHITECTURE.md:16-22](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/ARCHITECTURE.md#L16-L22)。

#### 4.4.4 代码实践：确认「三处调用同一个 `_assemble_trainer`」

1. **实践目标**：用源码佐证「三个承载训练器的构建器汇聚到同一装配函数」。
2. **操作步骤**：在 `launch.py` 中定位三处 `_assemble_trainer(` 调用（4.4.3 已给出行号），比较它们传给 `ref_source`、`store`、`durable_ack`、`num_epochs` 四个参数的值。
3. **需要观察的现象**：三处的「形状」是否一致？参数差异是否只集中在 `ref_source` 与 `store`？
4. **预期结果**：

   | 参数 | colocated 离线 | disagg 离线 | 在线 consumer |
   | --- | --- | --- | --- |
   | `ref_source` | `{"refs", "refs_for_epoch"}` | `{"refs", "refs_for_epoch"}` | `{"queue", "prepositioned", "defer_ack_until_durable"}` |
   | `store` | `LocalFeatureStore` | 外部 `feature_store` | Mooncake（`retain_on_release=True`） |
   | `durable_ack` | `False` | `False` | `True`（默认） |
   | `num_epochs` | 配置值（可 >1） | 配置值（可 >1） | `1` |

5. **待本地验证**：可选——在 `_assemble_trainer` 返回前临时加一行 `print(type(ref_source), type(store).__name__)`（**示例代码**，仅用于本地观察，勿提交），分别用离线与在线 YAML 跑一步，对照上表。

#### 4.4.5 小练习与答案

- **练习 1**：为什么 `_assemble_trainer` 要把 `resume_from` 与 `strategy_kwargs` 绑在一起校验？
  - **答案**：续训不仅要恢复权重，还要恢复算法专属的冻结状态（如捕获层、词表映射版本等「模型溯源」）。若允许用一个「裸字典」续训，就无法验证这些状态与检查点一致，会静默训错。所以续训强制要求 provider 绑定的 `StepRuntimeConfig`（[launch.py:89-111](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch.py#L89-L111)）。
- **练习 2**：在线 consumer 也调用 `_assemble_trainer`，但它的 `num_epochs=1`。如果用户在 YAML 里写了 `training.num_epochs=3`，在线训练会跑 3 个 epoch 吗？
  - **答案**：不会。在线 consumer 硬编码 `num_epochs=1`（[launch.py:1618](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch.py#L1618)）。`num_epochs=3` 在在线语义下被 producer 理解为「把 prompt 流重复 3 遍、每遍 mint 新的 task/sample id」，consumer 仍只消费一遍合并后的流（详见 [ARCHITECTURE.md:24-27](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/ARCHITECTURE.md#L24-L27)）。

## 5. 综合实践

**任务**：对照 [`ARCHITECTURE.md` 的支持路径表](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/ARCHITECTURE.md#L16-L22)，为下列三种模式分别指出「应调用哪个 `build_*` 构建器（或是否走非构建器路径）」，并写明该路径的「参考源」与「特征存储」。

1. **在线 disaggregated（producer 视角 / consumer 视角）**
2. **离线 colocated**
3. **离线 disaggregated（producer 视角 / consumer 视角）**

**要求**：

- 为每一种都给出构建器名（或注明「直接 ingest，无构建器」）。
- 用一句话说明该路径的参考源是固定 refs 还是流式 queue、特征存储是 Local / SharedDir / Mooncake 哪一种。
- 额外思考：三种里哪几种「可重迭代 + 支持断点续训」？哪一种是「消费一次 + 仅 consumer 可恢复」？

**参考答案**：

| 模式 | 视角 | 构建器 / 路径 | 参考源 | 特征存储 | 可重迭代？ |
| --- | --- | --- | --- | --- | --- |
| 在线 disaggregated | producer | `build_disagg_online_producer` | prompts → 服务器捕获 | Mooncake（写） | 否（采集一次） |
| 在线 disaggregated | consumer | `build_disagg_online_consumer` | 流式 queue（每 rank 一 inbox） | Mooncake（`retain_on_release=True`） | 否；consumer-only 恢复 |
| 离线 colocated | 单角色 | `build_offline_runtime` | 固定 refs（`refs_for_epoch`） | `LocalFeatureStore`（`file://`） | 是；支持续训 |
| 离线 disaggregated | producer | **直接 ingest，无构建器**（`ingest_offline_features`+`write_ref_manifest`） | 现成特征文件 | SharedDir 或 Mooncake（灌入） | — |
| 离线 disaggregated | consumer | `build_disagg_offline_runtime` | 固定 refs（manifest） | SharedDir 或 Mooncake（读） | 是；支持续训 |

「可重迭代 + 支持续训」的是两条离线 consumer 路径；「消费一次 + 仅 consumer 可恢复」的是在线 consumer。

## 6. 本讲小结

- `launch.py` 用 `__all__` 精确导出**四个拓扑构建器**，分别对应「离线 colocated / 离线 disagg consumer / 在线 producer / 在线 consumer」四种角色拓扑。
- 四个构建器**不被 `cli.py` 直接调用**，而是经两道分发器：`assembly.build_training_run` 按 `deployment.mode` 分流，`disaggregated.build_disaggregated_run` 再按 `mode` 与 `role` 路由。
- **离线 disagg producer 是特例**：它不调用任何 `build_*`，而是直接 `ingest_offline_features` 把现成特征灌入跨进程存储并写 manifest；只有 consumer 用 `build_disagg_offline_runtime`。
- 三个**承载训练器的构建器**（offline / disagg offline / online consumer）都汇聚到同一个 `_assemble_trainer → Trainer`，差异仅集中在 `ref_source`（refs 字典 vs queue 字典）与 `store`（Local / 外部 / Mooncake）两个变量上。
- **在线 producer 是唯一不汇聚到 Trainer 的构建器**，返回 `(workers, drive_producer)`；它通过 quantum 握手与水位校验与 consumer 协同，保证 optimizer 窗口对齐。
- 拓扑差异的「翻译表」：离线 = `NoOpMetadataStore` + `enable_sample_queue=False` + `durable_ack=False` + 可 `num_epochs>1`；在线 consumer = SQLite/DPAckController 账本 + `retain_on_release=True` + `durable_ack=True` + `num_epochs=1`。

## 7. 下一步学习建议

- 想看清「统一训练路径」内部到底怎么跑，进入 [u6-l1 训练装配 assembly](u6-l1-training-assembly.md) 与 [u6-l3 Trainer 与 TrainerController](u6-l3-trainer-loop.md)，看 `Trainer.fit` 如何消费 `ref_source` 与 `store`。
- 想深入「在线引用分发 + quantum 握手 + durable ack」的实现细节，进入 [u7-1 运行时架构与四条路径](u7-l1-runtime-architecture.md) 与 [u7-l4 在线引用分发与流式队列](u7-l4-online-ref-distribution.md)。
- 想理解离线特征文件如何被 `build_reader` 读成 refs、各算法的离线 schema 差异，进入 [u5-l3 离线特征生成](u5-l3-offline-feature-capture.md)。
- 建议顺手阅读 [`tests/test_runtime/`](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/tests/test_runtime) 下的等价测试（如 `test_colocated_vs_disagg_equiv.py`、`test_disagg_launch.py`），它们用最小用例印证了本讲的「同一训练路径、结果一致」结论。
