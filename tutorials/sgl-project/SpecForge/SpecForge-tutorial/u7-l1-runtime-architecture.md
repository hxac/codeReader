# 运行时架构与四条路径

## 1. 本讲目标

本讲是 DataFlow 运行时（`specforge/runtime/`）的总纲。读完后你应当能够：

- 说清 SpecForge 运行时的「四条路径」分别是什么，以及它们在**参考源**与**特征存储**上的差异。
- 理解控制面 / 数据面 / 推理面 / 训练面四个「平面（plane）」各自的职责边界，以及「控制面只传元数据、数据面才传张量」这条铁律。
- 在脑中画出在线 disaggregated（online disaggregated）的标准数据通路：从 producer 池的 SGLang 捕获，到 consumer 池的 rank0 账本与每个 rank 的 inbox，再到 optimizer 边界的 durable ack。
- 知道这四条路径如何坍缩成同一条训练主链路 `Trainer → FeatureDataLoader → TrainerController → TrainerCore`。

本讲是「鸟瞰图」，**不**深入任何单个组件的实现（那是 u7-l2 ~ u7-l5 与 u8 的事），只负责建立坐标系。

## 2. 前置知识

本讲承接 **u3-l3（拓扑构建器 launch）** 与 **u5-l4（跨平面契约与 SampleRef）**，默认你已经掌握：

- 两条正交轴线：**数据模式**（online / offline，由 `data.hidden_states_path` 是否填写决定）与**部署模式**（colocated / disaggregated，由 `deployment.mode` 决定）。在线特征捕获必须交给外部 SGLang，所以 **online 恒为 disaggregated**，真实拓扑只有三种：colocated offline、disaggregated offline、online。
- 跨平面契约是一批**纯标准库、不导入 torch** 的值对象：`PromptTask`、`SampleRef`、`FeatureSpec`、`FeatureHandle` 只携带元数据；`TrainBatch` 是唯一允许携带张量的契约；`assert_no_tensors` 把「张量不得进入控制面」从约定焊成硬约束。

还需三个通俗概念：

- **平面（plane）**：把一个分布式系统按职责切成几层互不越界的子系统。SpecForge 切了四个：控制面（调度/记账）、数据面（搬运张量）、推理面（捕获特征）、训练面（算梯度）。
- **参考源（reference source / ref source）**：训练循环「下一个样本从哪里来」的抽象。离线时它是一个固定的 `SampleRef` 列表，在线时它是一条消费一次的流式队列。本讲的核心论点之一就是：**四种拓扑的差异几乎全部翻译为 ref source 与特征存储这两件事**。
- **quantum（量子）**：在线模式下，producer 一次必须凑齐、consumer 一次必须正好消费的一个「optimizer 步」所需的样本数。它是 producer 与 consumer 握手对齐的基本单位。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `specforge/runtime/ARCHITECTURE.md` | 运行时架构的权威文档：列出四个构建器、支持路径表、跨面契约、在线标准流图、离线拓扑说明。本讲的骨架。 |
| `specforge/launch.py` | 四个拓扑构建器的真实实现，以及被它们共享调用的 `_assemble_trainer` 装配器——「四路径坍缩成一条主链路」的代码证据。 |
| `specforge/training/assembly.py` | 第一道分发器 `build_training_run`：按部署模式把请求路由到 colocated 离线或 disaggregated。 |
| `specforge/training/disaggregated.py` | 第二道分发器 `build_disaggregated_run`：按数据模式 + role 路由到离线 disagg 或在线 producer/consumer。 |
| `specforge/runtime/contracts.py` | 跨平面契约与 `assert_no_tensors` 守卫的定义（u5-l4 已精读，本讲只引用）。 |

## 4. 核心概念与源码讲解

### 4.1 四条路径与四个构建器

#### 4.1.1 概念说明

SpecForge 的训练入口只有一个：`specforge train`。一份 typed run config 选定「一个算法 + 一种拓扑」，**不会**选出第二个 trainer。launch 层对外恰好暴露**四个拓扑构建器**：

[ARCHITECTURE.md:L3-L10](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/ARCHITECTURE.md#L3-L10) 开宗明义列出这四个名字，它们也正是 `launch.py` 唯一的对外导出：

[launch.py:L1654-L1659](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch.py#L1654-L1659) —— `__all__` 把这四个构建器焊死为 launch 的全部公开 API。

```
build_offline_runtime            # colocated 离线
build_disagg_offline_runtime     # disaggregated 离线（consumer 侧）
build_disagg_online_producer     # 在线 producer 侧
build_disagg_online_consumer     # 在线 consumer 侧
```

需要厘清一个容易混淆的点：**「四条路径」是四个构建器，而「支持路径表」只有三行**。原因是 online 行天然分裂为 producer / consumer 两侧，各对应一个构建器；colocated offline 与 disaggregated offline 则各占一行。于是：

| 四个构建器 | 三种支持模式 |
| --- | --- |
| `build_offline_runtime` | Colocated offline |
| `build_disagg_offline_runtime` | Disaggregated offline |
| `build_disagg_online_producer` + `build_disagg_online_consumer` | Online |

这四个构建器的对应关系不是手写的，而是由两道分发器自动路由出来的。

#### 4.1.2 核心流程

构建器的选择由「数据模式 × 部署模式」两轴决定，经两道分发器落地：

**第一道：`build_training_run` 按 `deployment.mode` 分流**

[training/assembly.py:L579-L604](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/assembly.py#L579-L604) 给出规则：`deployment.mode == "disaggregated"` 时交给第二道分发器；否则（colocated）必须是 offline，直接调 `build_offline_runtime`。这里还顺手拦了一道硬约束——online 必须是 disaggregated：

[training/assembly.py:L573-L577](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/assembly.py#L573-L577) —— `online` 且非 disaggregated 直接 `raise`。

**第二道：`build_disaggregated_run` 按 `mode` + `role` 分流**

[training/disaggregated.py:L772-L792](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/disaggregated.py#L772-L792) 把 disaggregated 请求再切成四份：offline → 离线 disagg（producer 灌特征 / consumer 训练）；online → 在线 producer / consumer。

伪代码概括这段路由：

```
build_training_run(cfg, algorithm):
    校验 world_size（producer 除外）
    if cfg.mode == "online" 且 deployment.mode != "disaggregated": 报错   # 无 colocated online
    if deployment.mode == "disaggregated":
        return build_disaggregated_run(cfg, algorithm, ...)            # → 三个 disagg 构建器之一
    if cfg.mode != "offline": 报错                                     # colocated 只支持 offline
    return build_offline_runtime(...)                                  # → colocated 离线
```

#### 4.1.3 源码精读：四路径坍缩成一条主链路

这是本讲最重要的结论。ARCHITECTURE.md 把它写成一页就一句话：

[ARCHITECTURE.md:L12-L14](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/ARCHITECTURE.md#L12-L14) —— 「所有承载训练器的构建器都汇聚到同一条 `Trainer → FeatureDataLoader → TrainerController → TrainerCore` 路径，只有参考源与特征存储后端不同。」

代码证据是 launch.py 顶部的共享装配器。文件第 34 行有一句注释点明了它的设计意图：

[launch.py:L34-L36](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch.py#L34-L36) —— 「Shared assemblers — strategy- and topology-agnostic.」（与策略、与拓扑无关的共享装配器）。

`_assemble_trainer`（[launch.py:L39-L153](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch.py#L39-L153)）是三个承载训练器的构建器（colocated offline、disagg offline、online consumer）共同调用的装配函数。它的关键参数 `ref_source` 的类型注释本身就把「四路径差异」浓缩成了一行：

[launch.py:L44](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch.py#L44) —— `ref_source: dict  # {"refs": [...]} re-iterable (offline) | {"queue": q} stream (online)`。

也就是说，整套拓扑差异最终被翻译成两个变量：

- `ref_source`：离线给 `{"refs": ...}`（可重迭代的固定列表），在线给 `{"queue": ...}`（消费一次的流）。
- `store`：离线 colocated 用 `LocalFeatureStore`，离线 disagg 用跨进程存储，在线用 Mooncake。

来看两个对照实例。colocated 离线构建器把 `ref_source` 组装成可重迭代字典：

[launch.py:L595-L599](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch.py#L595-L599) —— `ref_source={"refs": refs, "refs_for_epoch": refs_for_epoch}`，并传 `durable_ack=False`、`NoOpMetadataStore`、`enable_sample_queue=False`（无账本、无队列）。

在线 consumer 构建器则把 `ref_source` 组装成流式字典：

[launch.py:L1602-L1606](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch.py#L1602-L1606) —— `ref_source={"queue": queue, "prepositioned": ..., "defer_ack_until_durable": True}`，并强制 `num_epochs=1`、`retain_on_release=True`（消费一次、删除延迟到 durable ack）。

两个实例喂给的是**同一个** `_assemble_trainer`。这正是「四路径坍缩成一条主链路」在代码层面的落点。

> 特例提醒：**在线 producer 是唯一不汇聚到 Trainer 的构建器**。它返回的是 `(workers, drive_producer)` 而非 trainer（[launch.py:L803-L808](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch.py#L803-L808)），它只负责捕获特征、发布引用，自己不训练。

#### 4.1.4 代码实践：对照支持路径表，比较 colocated offline 与 online

这是本讲的核心实践任务（源码阅读型）。

**1. 实践目标**：亲手从支持路径表中读出两种模式的三个维度差异，并理解这些差异为何让 online「不可重迭代」。

**2. 操作步骤**：

- 打开支持路径表：

[ARCHITECTURE.md:L16-L22](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/ARCHITECTURE.md#L16-L22) —— 这是「Supported paths」表的全部三行。
- 分别读出 Colocated offline 行与 Online 行在「Consumer reference source」「Feature store」「Iteration contract」三列的值。
- 把这些值与 launch.py 的两个实例（4.1.3 中的两段 `ref_source`）对上号：哪个对应 `{"refs": ...}`，哪个对应 `{"queue": ...}`。

**3. 需要观察的现象**：两张表对得上——文档里的「Fixed SampleRef list」就是代码里的 `refs`，「Per-rank StreamingRefQueue inbox」就是代码里的 `queue`。

**4. 预期结果**（参考答案）：

| 维度 | Colocated offline | Online |
| --- | --- | --- |
| 是否可重迭代 | **可重迭代**，支持多 epoch 与 checkpoint resume | **消费一次**，不做第二次 pass，无 producer resume |
| 参考源（ref source） | 固定的 `SampleRef` 列表 | 每个 rank 一个 `StreamingRefQueue` inbox |
| 特征存储（feature store） | `LocalFeatureStore`，读 `file://` refs | Mooncake |

差异的根因：离线特征是**预先物化在盘上的文件**，trainer 想读几次读几次；在线特征是 **producer 现场捕获、流式发布、消费即删**的，根本没有第二次可读。

**5. 是否可运行**：纯源码阅读，无需运行环境。

#### 4.1.5 小练习与答案

**练习 1**：为什么 SpecForge 没有「colocated online」这第四种真实拓扑？
> **答案**：在线特征捕获必须由**外部 SGLang 服务**完成（模型推理在 server 进程里跑），不可能与 trainer 同进程 colocated。代码里这被 `assembly.py:573-577` 直接 `raise` 拦死：`online` 且 `deployment.mode != "disaggregated"` 即报错。

**练习 2**：`build_disagg_online_producer` 返回什么？它和其他三个构建器有何本质不同？
> **答案**：返回 `(workers, drive_producer)`，是四者中唯一**不汇聚到 Trainer** 的构建器。它只做捕获与引用发布，不训练；其余三个都返回一个可 `.fit()` 的 trainer（或包裹它的 `TrainingRun`）。

### 4.2 跨面契约：控制面 / 数据面 / 推理面 / 训练面

#### 4.2.1 概念说明

ARCHITECTURE.md 用「Cross-plane contracts」一节定义了四个平面的边界。这是 u5-l4 精读过的契约体系在运行时层面的分工投影：

[ARCHITECTURE.md:L29-L39](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/ARCHITECTURE.md#L29-L39) —— 四条 bullet 各对应一个平面。

四个平面的职责一句话概括：

| 平面 | 职责 | 是否携带张量 |
| --- | --- | --- |
| 控制面（control plane） | 携带 `PromptTask` 与 `SampleRef` **元数据**，负责调度、去重、租约、记账 | 否 |
| 数据面（data plane） | 在 `FeatureStore` URI 背后搬运**特征张量** | 是 |
| 推理面（inference plane） | 把模型输入发给外部 spec-capture server，采用其 Mooncake 引用，**只提交元数据** | 否（张量由 server 直写存储） |
| 训练面（training plane） | 解析算法 step provider；核心训练循环**不因 online/offline/colocated/disaggregated 而分支** | 是（仅 `TrainBatch`） |

最关键的设计哲学是最后那条 bullet 的后半句：**训练面（即核心训练循环）对部署形态无感知**。这也是 4.1 节「四路径坍缩成一条主链路」能成立的根据——训练循环根本不知道自己在跑哪种拓扑。

#### 4.2.2 核心流程

四个平面之间的数据流（以在线 disaggregated 为例）：

```
推理面 ──模型执行、张量直写存储──▶ 数据面(Mooncake)
推理面 ──只提交 SampleRef 元数据──▶ 控制面
控制面 ──去重/分发 SampleRef──▶ 控制面(consumer rank0 账本)
控制面 ──SampleRef──▶ 数据面(FeatureDataLoader: ref+store → TrainBatch)
数据面 ──TrainBatch(唯一带张量的契约)──▶ 训练面(TrainerCore.forward_loss)
```

注意箭头的性质：**张量只在「数据面 ↔ 训练面」之间流动，且只以 `TrainBatch` 形态出现**；控制面与推理面之间传的全是元数据。这条铁律由 `assert_no_tensors` 守卫强制：

[contracts.py:L156](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/contracts.py#L156) —— `def assert_no_tensors(obj, *, _path="<root>")`，递归扫描对象，发现张量就抛 `TypeError`。它被 controller 各入口及 manifest 写盘前调用，把「张量不得进入控制面」焊成硬约束（详见 u5-l4）。

#### 4.2.3 源码精读：FeatureDataLoader 是唯一桥梁

四个平面里最微妙的是「数据面如何把张量交给训练面」。ARCHITECTURE.md 给出唯一答案：

[ARCHITECTURE.md:L33-L35](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/ARCHITECTURE.md#L33-L35) —— 「`FeatureDataLoader` 是从 refs + store 到携带张量的 `TrainBatch` 的**唯一桥梁**」。

这意味着：

- 训练面永远只见到 `TrainBatch`，见不到 `FeatureStore`、见不到 `SampleRef` 的内部。
- 想换存储后端（本地文件 / 共享目录 / Mooncake），只需换 `store`，`FeatureDataLoader` 与 trainer 全程无感。
- 「张量全程待在 `FeatureStore` 不跨进程边界」这条不变量，正是因为张量只在 `FeatureDataLoader` 内部被短暂实例化成 `TrainBatch`，立刻交给训练面。

推理面那条 bullet 同样值得单独点出：

[ARCHITECTURE.md:L36-L37](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/ARCHITECTURE.md#L36-L37) —— 推理面把输入发给外部 server，采用其 Mooncake 引用，**只提交元数据**。这正是「张量不经 producer 进程」的来源：producer 只拿到 `SampleRef`（元数据指针），真正的张量由 server 直接写进 Mooncake。

#### 4.2.4 代码实践：判断一段数据该属于哪个平面

**1. 实践目标**：用四个平面的定义，给运行时里出现的几样东西归类，强化「元数据 vs 张量」的边界直觉。

**2. 操作步骤**：对下面 5 个对象，分别判断它属于哪个平面、是否携带张量、（若携带）是否合法：

- (a) `PromptTask`
- (b) `SampleRef`
- (c) `FeatureStore`（一个 Mooncake 后端实例）
- (d) `TrainBatch`
- (e) trainer 拿到的一批草稿模型梯度

**3. 需要观察的现象**：凡是被 `assert_no_tensors` 守卫的门（controller 入口、manifest 写盘）拦下的东西，必然属于会携带张量的那一类；元数据类对象可以安全序列化为 JSON 跨节点传递。

**4. 预期结果**（参考答案）：

| 对象 | 平面 | 携带张量 | 合法性 |
| --- | --- | --- | --- |
| (a) `PromptTask` | 控制面 | 否 | 合法，可 JSON 序列化 |
| (b) `SampleRef` | 控制面 | 否 | 合法，仅 `feature_store_uri`+`feature_keys` 指针 |
| (c) `FeatureStore` | 数据面 | （内部持张量） | 张量不外泄，守卫不扫它本身 |
| (d) `TrainBatch` | 数据面→训练面 | 是 | 合法，唯一带张量的契约 |
| (e) 梯度 | 训练面 | 是 | 合法，但绝不能进控制面，否则 `assert_no_tensors` 抛错 |

**5. 是否可运行**：纯源码阅读 + 归类，无需运行。

#### 4.2.5 小练习与答案

**练习 1**：为什么控制面消息可以「序列化为 JSON 跨节点轻量传递」？
> **答案**：因为控制面只携带 `PromptTask`/`SampleRef` 等**纯元数据**（`assert_no_tensors` 守卫），不含张量，体积小、可序列化，适合跨节点传输；张量始终留在数据面的 `FeatureStore` 里。

**练习 2**：训练面「对部署形态无感知」具体指什么？
> **答案**：指 `TrainerCore` 的核心循环不写 `if online / if offline / if colocated / if disaggregated` 之类的分支；它只消费 `TrainBatch`、调 `strategy.forward_loss`。online/offline 的差异被收敛进 `ref_source` 与 `store` 两个变量，在装配阶段（`_assemble_trainer`）就已经处理完毕。

### 4.3 在线 disaggregated 的标准数据通路

#### 4.3.1 概念说明

在线 disaggregated 是最复杂的一条路径，也是 ARCHITECTURE.md 用图单独画出的「canonical flow」。它分成两个进程池：

- **producer 池**：补丁过的 SGLang server 做捕获（推理面），张量直写 Mooncake（数据面），`RolloutWorker` 拿到 `SampleRef`，经 `StreamingRefChannel` 发布。producer **只负责 prompt 调度**，自己不训练，也无账本、无本地 sample queue。
- **consumer 池**：rank0 跑 `RefDistributor`，是**共享源 channel 的唯一读者**与**账本的唯一写者**；它把去重后的 ref 轮询分发到「每个 rank 一个 inbox」。每个 rank 都用相同的 `InboxChannel → StreamingRefQueue → FeatureDataLoader` 通路。

#### 4.3.2 核心流程：标准流图

ARCHITECTURE.md 用一张 mermaid 图描绘这条通路：

[ARCHITECTURE.md:L45-L80](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/ARCHITECTURE.md#L45-L80)

把图翻成文字流水（左→右）：

```
[producer 池]
  patched SGLang 捕获 ──张量写──▶ Mooncake
  RolloutWorker ──SampleRef──▶ StreamingRefChannel ──▶ (跨进程) ──▶ consumer

[consumer 池]
  RefDistributor(rank0) ──去重+durable commit──▶ fresh SQLite 账本
  RefDistributor ──complete windows──▶ InboxChannel(rank0) / InboxChannel(rankN)
  InboxChannel ──▶ StreamingRefQueue ──▶ FeatureDataLoader ──▶ DPAckController
  DPAckController ──rank0 单笔 durable 事务──▶ SQLite 账本

  (张量): Mooncake ──tensor fetch──▶ FeatureDataLoader(rank0) / FeatureDataLoader(rankN)
```

几个关键事实（文字说明在图下方）：

[ARCHITECTURE.md:L82-L95](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/ARCHITECTURE.md#L82-L95) —— producer 只管 prompt 调度，用 no-op 账本、关掉本地 sample queue；consumer rank0 是共享 channel 的唯一读者、账本的唯一写者；`RefDistributor` 去重后**轮询**分发到每 rank 一个私有 inbox；每个 rank 经 `StreamingRefQueue` 适配后喂同一个 `FeatureDataLoader` 实现。

#### 4.3.3 源码精读：optimizer 边界与 quantum 握手

在线通路最精巧的一环是 producer 与 consumer 的**窗口握手**。consumer 训练前先广播一个全局 quantum：

[ARCHITECTURE.md:L99-L107](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/ARCHITECTURE.md#L99-L107) —— quantum 的定义与水印约束。

\[ \text{quantum} = \text{dp\_size} \times \text{batch\_size} \times \text{accumulation\_steps} \]

直观解释：一个 optimizer 步要消耗 `batch_size × accumulation_steps` 个样本，乘以 DP 并行度 `dp_size`，就是 producer 必须凑齐、consumer 必须正好分发一轮的「窗口大小」。

代码两侧都对得上。consumer rank0 在 setup 末尾发布 quantum：

[launch.py:L1492-L1494](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch.py#L1492-L1494) —— `channel.publish_consumer_quantum(dp_size * batch_size * accumulation_steps, ...)`。

producer 启动时先等这个 sidecar，并校验自己的 in-flight 高水位不得小于 quantum，否则直接 `raise`（启动即失败，绝不带病跑）：

[launch.py:L977-L995](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch.py#L977-L995) —— 水印校验逻辑。默认高水位 `DISAGG_IN_FLIGHT_HIGH_WATERMARK=256`（[ARCHITECTURE.md:L106-L107](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/ARCHITECTURE.md#L106-L107)）。

分发侧的纪律：`RefDistributor` **只在凑满一个完整 quantum 窗口时**才释放 ref，保证每个 rank 每个 optimizer 步正好拿到 `batch_size × accumulation_steps` 个 ref。若 EOF 留下不足一个窗口的尾巴，这些 ref 会被标记 terminal、其 feature 对象被 abort、源计数结清，但**绝不派发一个残缺的全局 optimizer 步**：

[ARCHITECTURE.md:L109-L121](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/ARCHITECTURE.md#L109-L121) —— 完整窗口分发、部分窗口终态处理、committed-but-unacknowledged 尾巴的恢复约束。

每到一个 optimizer 边界，所有 rank **lockstep** 调 `DPAckController.ack_train_refs`，汇总 sample id 后由 rank0 记一笔 durable 事务；只有这笔提交成功，各 rank 才删除本地 feature id。这条「ack 钉在 optimizer 边界」的纪律，与 u6-l3 / u6-l4 讲过的 `optimizer_stepped` 单一边界信号一脉相承：

[ARCHITECTURE.md:L90-L95](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/ARCHITECTURE.md#L90-L95) —— optimizer 边界的 durable ack 与清理流程。

关于恢复：在线 consumer 是**唯一可恢复**的角色，且只能 **consumer-only 恢复**——复用 retained 账本 / channel / inbox / Mooncake 对象与一份精确匹配的 checkpoint，调和未确认的尾巴，**绝不重启 producer**：

[ARCHITECTURE.md:L123-L127](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/ARCHITECTURE.md#L123-L127) —— producer 必须全新、consumer 可 consumer-only 恢复。

> 关于 `training.num_epochs`：在线时它控制的是 **producer 创建几轮 prompt pass**，每轮 mint 全新的 task/sample id；consumer 仍只把这条 consume-once 流迭代一次，**绝不**把一条旧流当第二个 trainer epoch 重放（[ARCHITECTURE.md:L24-L27](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/ARCHITECTURE.md#L24-L27)）。这也呼应 4.1.3 里 consumer 的 `num_epochs=1`。

#### 4.3.4 代码实践：推算一个在线 consumer 的 quantum 与水印

**1. 实践目标**：用真实公式算出一个具体拓扑的 quantum，验证默认水位是否合法，并解释 producer 的背压。

**2. 操作步骤**：

- 给定一个 online consumer：`dp_size=4`、`batch_size=2`、`accumulation_steps=2`。
- 代入公式 \(\text{quantum} = \text{dp\_size} \times \text{batch\_size} \times \text{accumulation\_steps}\) 算出 quantum。
- 查 [launch.py:L977-L983](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch.py#L977-L983)，判断默认 `DISAGG_IN_FLIGHT_HIGH_WATERMARK=256` 是否满足 `>= quantum`。
- 回答：每个 rank 每个 optimizer 步拿到几个 ref？EOF 残留不足一个 quantum 时会怎样？

**3. 需要观察的现象**：quantum 是「一个 optimizer 步、全部 DP rank」的总样本数；每个 rank 拿到的是 `batch_size × accumulation_steps`。

**4. 预期结果**（参考答案）：

- \(\text{quantum} = 4 \times 2 \times 2 = 16\)。
- 默认高水位 256 ≥ 16，**合法**，producer 可启动。
- 每个 rank 每个 optimizer 步拿到 `batch_size × accumulation_steps = 4` 个 ref；4 个 rank 共 16，正好一个 quantum。
- EOF 残留不足 16 时：这些 ref 被标记 terminal、feature 对象 abort、源计数结清，**不派发残缺 optimizer 步**；尾巴在账本里保持 committed-but-unacknowledged，后续若要续训须用 fresh ledger（该 completed 尾巴不可 resume）。

**5. 是否可运行**：纯计算 + 源码阅读，无需运行。

#### 4.3.5 小练习与答案

**练习 1**：为什么 consumer rank0 是「共享 channel 的唯一读者」和「账本的唯一写者」？
> **答案**：避免多 rank 并发读写造成去重漏判与账本竞争。`RefDistributor` 只在 rank0 跑（即便 `dp_size=1`），由它统一去重、记 durable 事务、再轮询分发到各 rank 私有 inbox。代码见 [launch.py:L1349-L1353](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch.py#L1349-L1353)。

**练习 2**：在线 disaggregated 的「consumer-only 恢复」为什么不能重启 producer？
> **答案**：producer 捕获的是 consume-once 的流，张量随消费被删，无法重放；且 producer 重启会 mint 全新 task/sample id，与 retained 账本里已 committed 的 id 对不上。因此恢复只能复用 retained 账本 / channel / inbox / Mooncake 对象与一份**精确匹配 step 的 checkpoint**，调和未确认尾巴，把 producer 排除在外。

**练习 3**：`training.num_epochs=3` 在在线模式下到底让什么重复了 3 次？
> **答案**：让 **producer 端的 prompt pass** 重复 3 次（每轮 mint 全新 epoch-tagged task/sample id），不是让 consumer 把同一条流重放 3 遍。consumer 始终只把这条 consume-once 流迭代一次（`num_epochs=1`）。

## 5. 综合实践：把一个样本穿过四个平面

把本讲三个模块串起来。请针对**在线 disaggregated** 模式，追踪「一个被捕获的特征样本」从产生到被梯度更新的全过程，在每一步标注：它此刻属于哪个**平面**、以什么**对象**存在、是否携带**张量**。

要求产出一张表（建议格式如下），并回答末尾两个问题。

| 阶段 | 发生在哪 | 所属平面 | 承载对象 | 携带张量？ |
| --- | --- | --- | --- | --- |
| 1. 目标模型执行、隐藏状态捕获 | patched SGLang server | 推理面 | （server 内部） | 是（不外泄） |
| 2. 张量写入跨进程存储 | server → Mooncake | 数据面 | feature tensors | 是 |
| 3. producer 拿到指针并发布 | RolloutWorker → StreamingRefChannel | 控制面 | `SampleRef` | 否 |
| 4. rank0 去重、记 durable 事务 | RefDistributor → SQLite 账本 | 控制面 | `SampleRef` + sample id | 否 |
| 5. 分发到某 rank 的 inbox | InboxChannel → StreamingRefQueue | 控制面 | `SampleRef` | 否 |
| 6. 取回张量并 collate | FeatureDataLoader | 数据面 | `TrainBatch` | 是 |
| 7. 算 loss / 反向 / optimizer step | TrainerCore | 训练面 | batch + 梯度 | 是 |

**问题 A**：在第 3~5 步，张量「不在场」，那 consumer 靠什么最终取回张量？（提示：`SampleRef` 的两个字段 + 哪个对象是「唯一桥梁」。）
> **参考答案**：靠 `SampleRef` 携带的 `feature_store_uri` + `feature_keys` 定位 Mooncake 里的张量；由 `FeatureDataLoader`（refs + store → `TrainBatch` 的唯一桥梁）实际取回。

**问题 B**：整条链路里，张量**跨越了哪些进程边界**？哪些地方刻意**没有**跨进程传张量？
> **参考答案**：张量只在「server 写入 Mooncake」与「consumer FeatureDataLoader 从 Mooncake 取回」这两处进出存储，**始终不进入 producer 进程、不进入 controller、不进入 ref 分发链路**。控制面全程只传 `SampleRef` 元数据，可 JSON 序列化跨节点轻量传递——这正是 SpecForge 在线分离式训练能协同的根基。

## 6. 本讲小结

- SpecForge 运行时对外只有**四个拓扑构建器**（`build_offline_runtime` / `build_disagg_offline_runtime` / `build_disagg_online_producer` / `build_disagg_online_consumer`），由两道分发器（`build_training_run` → `build_disaggregated_run`）按「数据模式 × 部署模式」自动路由；真实拓扑只有三种（colocated offline / disagg offline / online），online 天然分裂为 producer+consumer。
- 四条路径坍缩成同一条主链路 `Trainer → FeatureDataLoader → TrainerController → TrainerCore`；拓扑差异几乎全部翻译为 `ref_source`（refs vs queue）与 `store`（Local/共享目录/Mooncake）两个变量，证据是三个承载训练器的构建器共用 `_assemble_trainer`。
- 系统按职责切成**四个平面**：控制面（调度/记账，只传元数据）、数据面（搬运张量）、推理面（捕获特征、只提交元数据）、训练面（算梯度、对部署形态无感知）；`assert_no_tensors` 把「张量不得进控制面」焊成硬约束，`FeatureDataLoader` 是数据面到训练面的唯一桥梁。
- 在线 disaggregated 的标准通路：producer 池（SGLang 捕获 → Mooncake 写张量 → 发布 `SampleRef`）+ consumer 池（rank0 的 `RefDistributor` 去重并写账本 → 每 rank 一个 inbox → `FeatureDataLoader` → `DPAckController`）。
- producer 与 consumer 靠 **quantum**（\( \text{dp\_size} \times \text{batch\_size} \times \text{accumulation\_steps} \)）窗口握手对齐，`RefDistributor` 只派发完整窗口、绝不派发残缺 optimizer 步；每边界 lockstep 一笔 durable 事务后才删 feature。
- 在线只有 **consumer-only 恢复**，复用 retained 账本与精确匹配 checkpoint，**不重启 producer**；`num_epochs` 在线时只控制 producer 的 prompt 轮数，consumer 始终 consume-once。

## 7. 下一步学习建议

本讲是运行时的鸟瞰图，后续四讲分别下钻运行时的四个子面：

- **u7-l2 控制平面与元数据账本**：精读 `DataFlowController`、`NoOp/InMemory/SQLite` 三种 metadata store、freshness 与恢复契约——把本讲的「rank0 单一写账本」讲到底。
- **u7-l3 数据平面 feature store 与传输**：精读 `FeatureStore` 契约、Local/shared_dir/Mooncake 三后端、`FeatureDataLoader` 的 refs/queue 两种模式——把本讲的「唯一桥梁」讲到底。
- **u7-l4 在线引用分发与流式队列**：精读 `RefDistributor → InboxChannel → StreamingRefQueue`、quantum 握手、`DPAckController`——把本讲的标准流图与窗口握手讲到底。
- **u7-l5 推理平面与 SGLang 捕获**：精读 `RolloutWorker`、`SGLangServerCaptureAdapter`——把本讲的「张量不经 producer 进程」讲到底。

建议在进入 u7-l2 之前，先回头重读本讲的「Cross-plane contracts」一节与综合实践那张表，确保四个平面的坐标系已经牢固。
