# 控制平面与元数据账本

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `DataFlowController` 提供了哪些「元数据原语」（prompt 的注入/租赁/重试、`SampleRef` 的提交去重、训练侧的 durable ack），并解释它为何「只搬元数据、绝不碰张量」。
- 区分 `NoOpMetadataStore` / `InMemoryMetadataStore` / `SQLiteMetadataStore` 三种账本后端，并指出它们分别对应哪种部署拓扑。
- 解释在线 consumer「rank0 单一写账本」的设计，以及 `DPAckController` 如何在数据并行下保持账本的单一写入权威。
- 复述 freshness 契约与恢复契约，特别是「durable ack step 必须等于 checkpoint step」这条约束的用意。

本讲是 [u7-l1 运行时架构与四条路径](u7-l1-runtime-architecture.md) 的承接：u7-l1 给出了四平面分工的鸟瞰，本讲放大其中的「控制面」这一个平面，逐个拆开它的元数据原语与账本实现。

## 2. 前置知识

本讲假设你已经建立以下认知（来自前置讲义）：

- **四平面分工**：SpecForge 的 DataFlow 运行时按职责切成控制面（调度/记账，只传元数据）、数据面（搬运张量）、推理面（捕获特征）、训练面（算梯度）。
- **铁律「张量不得进入控制面」**：控制面消息必须可序列化、可跨节点轻量传递，张量全程待在 `FeatureStore` 里不跨进程边界。这条铁律由 `assert_no_tensors` 递归守卫焊成硬约束（见 [u5-l4 跨平面契约与 SampleRef](u5-l4-runtime-contracts.md)）。
- **三种真实拓扑**：colocated 离线、disaggregated 离线、在线 disaggregated（online 恒为 disaggregated，因为在线特征捕获必须由外部 SGLang 完成）。
- **quantum**：`quantum = dp_size × batch_size × accumulation_steps`，是 producer/consumer 窗口握手单位，也是一次 durable ack 涵盖的样本数。
- **SampleRef**：指向一个样本全部特征的纯元数据指针，自身不持任何张量；`TrainBatch` 是唯一允许携带张量的契约。

如果你对「为什么要分离控制面与数据面」还不清楚，建议先读 u7-l1 与 u5-l4。

## 3. 本讲源码地图

本讲聚焦 `specforge/runtime/control_plane/` 这个子包，它只依赖 Python 标准库（`sqlite3`、`threading`、`json`），刻意不导入 torch，使它在「无 GPU、无 torch」的场景下也能被导入与测试。

| 文件 | 作用 |
| --- | --- |
| [control_plane/controller.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/control_plane/controller.py) | 定义 `DataFlowController`——控制面的核心调度器，拥有 prompt 与 sample 的生命周期、ack 事务、worker 注册。每个接受 record 的公共方法都跑 `assert_no_tensors`。 |
| [control_plane/metadata_store.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/control_plane/metadata_store.py) | 定义账本的抽象接口 `MetadataStore` 及三种实现：`InMemoryMetadataStore`、`NoOpMetadataStore`、`SQLiteMetadataStore`。 |
| [control_plane/dp_ack.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/control_plane/dp_ack.py) | 定义 `DPAckController`——把 `ack_train_refs` 改造成一次数据并行 collective，保证账本只有 rank0 一个写入者。 |
| [control_plane/DESIGN.md](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/control_plane/DESIGN.md) | 控制面的设计文档，给出拓扑所有权表、在线 consumer 流程图、freshness 与恢复契约的权威表述。 |
| [data_plane/ref_distributor.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/ref_distributor.py) | `RefDistributor`——rank0 上的分发器，是 controller 在在线路径上的主要驱动者之一（下讲 u7-l4 详讲）。 |

控制面与数据面的边界可以这样记：**控制面记「谁欠谁、谁 ack 了谁」的账，数据面搬「真正的张量」的货**。账本（ledger）是控制面的核心数据结构。

---

## 4. 核心概念与源码讲解

### 4.1 DataFlowController：只搬元数据的调度器

#### 4.1.1 概念说明

`DataFlowController` 是控制面的「大脑」，但它是一个**很克制的大脑**：

- 它管 prompt 的注入（ingest）、租赁（lease）、完成（complete）、失败重试（fail）。
- 它管 `SampleRef` 的提交与去重（commit）。
- 它管训练侧的 durable ack（ack_train_refs）。
- 它管 worker / trainer 的注册。

但它**不管**：模型前向、张量搬运、rollout 调用、训练循环。用 DESIGN.md 的话说：

> The controller has no run loop and never calls a rollout worker or trainer.

为什么这样设计？因为控制面的消息要能跨进程、跨节点传递（producer 在一个进程池、consumer 在另一个进程池）。如果控制面里混入张量，那么每一条「记账」消息都要走重型的张量序列化，控制面就退化成了数据面。所以 controller 的每个接受 record 的公共方法入口都先跑 `assert_no_tensors`，把「张量不得进入控制面」从约定焊成硬约束。这个不变量由测试 `test_controller_carries_no_tensor` 守卫。

#### 4.1.2 核心流程

一次在线 consumer 的控制面活动可以用下面这条主线串起来（伪代码）：

```
# 1. prompt 生命周期（producer 侧 / 某些拓扑的 consumer 侧）
controller.ingest_prompts(prompts)        # 注入待捕获 prompt -> _prompt_pending
worker_id = controller.register_rollout_worker(info)
tasks = controller.lease_prompt_tasks(worker_id, n)  # 从 pending 取 n 个，记到 _prompt_leased

# 2. 捕获完成后提交 SampleRef（去重发生在账本里）
fresh = controller.commit_samples(worker_id, refs)
#   -> store.commit_samples(refs) 返回每个 ref 是否「新鲜」
#   -> 只有 fresh 的 ref 才进入 sample_queue

# 3. 训练侧消费完一个 optimizer 窗口后，durable ack
controller.ack_train_refs(trainer_id, sample_ids,
                          global_step=step, optimizer_durable=True)
#   -> store.record_train_ack(...) 把 ack ids + optimizer marker 原子写账本
#   -> sample_queue.ack(refs) 释放队列租约
```

三条核心数据结构（都在 controller 实例里、由一把 `threading.Lock` 保护）：

- `_prompts: OrderedDict[task_id, PromptTask]`：所有待处理/已租赁的 prompt。
- `_prompt_pending: deque`：尚未被租赁的 prompt 队列（FIFO）。
- `_prompt_leased: dict[task_id, worker_id]`：已被租出但尚未结案的 prompt。
- `_prompt_failed: dict[task_id, reason]`：终态失败的 prompt。

而 `SampleRef` 的去重与 ack 不落在这些 dict 里，而是落在注入的 `MetadataStore`（账本）里——这是本讲第 4.2 节的主角。

#### 4.1.3 源码精读

`DataFlowController` 的构造与状态，注意 `store` 默认是 `InMemoryMetadataStore`（[controller.py:L39-L61](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/control_plane/controller.py#L39-L61)）。`enable_sample_queue` 控制是否启用本地样本暂存队列——在线 producer 上它会被关掉（见 4.4）。

prompt 注入：把原始 dict 校验后构造成不可变的 `PromptTask`，构造放在锁外、入队放在锁内（[controller.py:L79-L110](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/control_plane/controller.py#L79-L110)）。注释明确说明：对大批 prompt，校验与对象构造很贵，所以放在全局锁之外，避免阻塞其他 worker。

租赁：从 `_prompt_pending` 弹出最多 `max_tasks` 个，记入 `_prompt_leased`（[controller.py:L112-L121](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/control_plane/controller.py#L112-L121)）。

提交与去重——这是控制面最关键的方法之一（[controller.py:L175-L201](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/control_plane/controller.py#L175-L201)）：

```python
def commit_samples(self, worker_id, refs):
    for ref in refs:
        assert_no_tensors(ref)                 # 在线 no-tensor 守卫
    freshness = self.store.commit_samples(refs)  # 去重在账本里发生
    ...
    fresh = [ref for ref, is_fresh in zip(refs, freshness) if is_fresh]
    with self._lock:
        for ref in fresh:                      # 只结案新鲜的 prompt
            if ref.source_task_id is not None:
                self._prompt_leased.pop(ref.source_task_id, None)
                self._prompts.pop(ref.source_task_id, None)
    if fresh and self.sample_queue is not None:
        self.sample_queue.put(fresh)           # 只有新鲜 ref 进暂存队列
    return fresh
```

关键点：`freshness` 是账本返回的**去重结果**，controller 不自己再查一次账本判断新鲜——方法 docstring 明确要求调用方「用这个返回值，而不是另查账本推断」，避免两次查询之间状态漂移。

durable ack（[controller.py:L205-L226](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/control_plane/controller.py#L205-L226)）：先 `store.record_train_ack` 记账（ack ids + optimizer marker 原子事务），再 `sample_queue.ack(refs)` 释放队列租约。注意顺序——先持久化、再放队列，保证「已 ack」与「已记入账本」一致。

恢复时的重放（[controller.py:L228-L266](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/control_plane/controller.py#L228-L266)）：`reconcile_on_restart` 从唯一一份持久账本重建瞬态训练队列——已被 optimizer-durable ack 覆盖的样本释放（并 abort 远端特征对象），其余已提交样本重新入队做 at-least-once 训练。操作幂等，因为 `SampleRefQueue.put` 按 sample id 去重。

#### 4.1.4 代码实践

**实践目标**：用最少代码手动驱动 `DataFlowController`，观察「提交去重」与「durable ack」如何反映在账本里。

**操作步骤**（源码阅读 + 本地运行）：

1. 阅读 [controller.py:L175-L201](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/control_plane/controller.py#L175-L201) 的 `commit_samples`，确认它把去重委托给了 `self.store`。
2. 写一个最小脚本（**示例代码**，非项目原文件）：

```python
# 示例代码：手动驱动 DataFlowController 观察去重与 ack
from specforge.runtime.control_plane.controller import DataFlowController
from specforge.runtime.control_plane.metadata_store import InMemoryMetadataStore
from specforge.runtime.contracts import SampleRef, FeatureSpec

def mk(sid):
    return SampleRef(
        sample_id=sid, run_id="run0", source_task_id=f"t-{sid}",
        feature_store_uri=f"mooncake://run0/{sid}",
        feature_keys={"hidden_state": f"{sid}/h"},
        feature_specs={"hidden_state": FeatureSpec("hidden_state", (1,8,4), "float32")},
        strategy="eagle3", num_tokens=8,
    )

ctrl = DataFlowController("run0", metadata_store=InMemoryMetadataStore())
fresh1 = ctrl.commit_samples("prod", [mk("s0"), mk("s1")])   # 两个都新鲜
fresh2 = ctrl.commit_samples("prod", [mk("s1"), mk("s2")])   # s1 重复 -> 只 s2 新鲜
print([r.sample_id for r in fresh1])  # 预期 ['s0', 's1']
print([r.sample_id for r in fresh2])  # 预期 ['s2']
ctrl.ack_train_refs("trainer0", ["s0", "s1"], global_step=1, optimizer_durable=True)
print(ctrl.status()["durable_global_step"])  # 预期 1
print(ctrl.status()["durable_acked"])        # 预期 2
```

**需要观察的现象**：第二次提交同一个 `s1` 时，账本报告它不新鲜，于是它既不进 `sample_queue`，也不会被重复 ack。

**预期结果**：`fresh1 == ['s0','s1']`、`fresh2 == ['s2']`、durable ack 后 `status()` 显示 `durable_global_step=1`、`durable_acked=2`。

**若无法本地运行**：标注「待本地验证」，改为对照 [tests/test_runtime/test_recovery.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/tests/test_runtime/test_recovery.py) 阅读断言行为（见 4.4.4）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `commit_samples` 要在方法开头对每个 ref 跑 `assert_no_tensors`，而不是只跑一次？

**参考答案**：因为去重发生在注入的 `MetadataStore` 里，而某些 store（如 `SQLiteMetadataStore`）会把 ref 序列化成 JSON 写盘。如果 ref 里混进了张量，不仅违背「控制面不传张量」铁律，还会导致 JSON 序列化失败或把巨大对象写进 SQLite。每个 ref 单独校验，能在第一个带张量的 ref 上就 fail-fast，并给出精确的面包屑路径。

**练习 2**：`commit_samples` 返回的 `fresh` 列表，与方法内 `self.sample_queue.put(fresh)` 放进队列的是同一批对象吗？为什么要强调调用方用返回值而非另查账本？

**参考答案**：是同一批。强调用返回值是为了避免「查账本得到新鲜 → 队列已变」的 TOCTOU（time-of-check-to-time-of-use）竞态：账本是唯一事实来源，去重结果应一次性返回并由调用方直接使用。

---

### 4.2 MetadataStore：三种账本后端

#### 4.2.1 概念说明

`MetadataStore` 是「账本」的抽象接口。它把两件事从 controller 的本地字典里抽出来：

1. **已提交样本的去重**（at-least-once，按 sample_id 幂等）。
2. **durable ack 事务**（ack ids + optimizer-step marker 原子提交）。

之所以要抽出来，是因为不同拓扑对「账本要不要持久、要不要跨进程共享」的要求完全不同：

- **colocated 离线**：根本没有「在线提交」这一步，特征是固定的可重迭代 ref 列表，不需要账本。
- **disaggregated 离线**：producer 只发布一个静态 manifest，训练侧不维护训练账本。
- **在线 producer**：只负责捕获与发布，不做训练记账。
- **在线 consumer**：需要一份**跨进程共享、崩溃可恢复**的账本——这就是 SQLite。

因此 SpecForge 提供三种实现，对应三种需求强度。

#### 4.2.2 核心流程

`MetadataStore` 的抽象方法分两组（[metadata_store.py:L31-L75](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/control_plane/metadata_store.py#L31-L75)）：

```
# 组一：样本提交 / 去重（at-least-once，按 sample_id 幂等）
commit_sample(ref) -> bool            # 单条；返回是否新鲜
commit_samples(refs) -> List[bool]    # 批量；可重写让整批共享一个事务
is_committed(sample_id) -> bool
get_committed(sample_id) -> SampleRef | None
committed_count() -> int
all_committed_ids() -> List[str]

# 组二：durable ack 事务
record_train_ack(sample_ids, *, global_step, optimizer_durable) -> None
durable_marker() -> {acked: set, global_step: int|None, optimizer_durable: bool}
```

`commit_samples` 的默认实现是「逐条调 `commit_sample`」（[metadata_store.py:L37-L45](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/control_plane/metadata_store.py#L37-L45)），但注释要求**持久 store 应重写此方法**，让整批在一个事务里提交——这是 SQLite 实现性能与原子性的关键。

三种实现的「记忆强度」对比：

| 实现 | 提交是否记忆 | ack 是否记忆 | 适用拓扑 |
| --- | --- | --- | --- |
| `NoOpMetadataStore` | 否（永远返回新鲜） | 否（no-op） | colocated 离线、在线 producer |
| `InMemoryMetadataStore` | 是（进程内字典） | 是（进程内） | 本地记账、非权威 DP rank |
| `SQLiteMetadataStore` | 是（落盘） | 是（落盘、WAL+FULL） | 在线 consumer（唯一持久权威） |

#### 4.2.3 源码精读

`NoOpMetadataStore` 是「什么都不记」的账本（[metadata_store.py:L139-L175](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/control_plane/metadata_store.py#L139-L175)）。它的 `commit_sample` 永远返回 `True`，`durable_marker` 永远返回空——controller 仍会照常把 ref 放进队列，但跨进程共享与恢复都不可能。docstring 明确：「Use a retaining store for cross-process runs.」

`InMemoryMetadataStore` 用进程内 `dict` + `set` + 一把 `RLock` 做记账（[metadata_store.py:L78-L136](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/control_plane/metadata_store.py#L78-L136)）。它的 `record_train_ack` 在同一把锁里更新 `{acked ids, global_step, optimizer_durable}` 三样，注释点明这是「one atomic update」——单进程内保证一致性。

`SQLiteMetadataStore` 是唯一持久后端，构造时打开一个 SQLite 文件并设置两个关键 PRAGMA（[metadata_store.py:L181-L201](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/control_plane/metadata_store.py#L181-L201)）：

```python
self._conn = sqlite3.connect(path, check_same_thread=False)
self._conn.execute("PRAGMA journal_mode=WAL")   # 持久 + 允许并发读
self._conn.execute("PRAGMA synchronous=FULL")    # ack 能扛断电，不只是进程崩溃
```

- `WAL`（Write-Ahead Logging）：写入先记日志、再落主库，读不阻塞写，适合「一个写者 + 多读者」。
- `synchronous=FULL`：每次 commit 都强制刷盘，保证 ack 事务**能扛断电**，而不只是进程崩溃。这是「durable」一词的物理含义。

三张表：`committed(sample_id PRIMARY KEY, ref_json)`、`acked(sample_id PRIMARY KEY)`、`marker(k PRIMARY KEY, v)`（存 `global_step` 与 `optimizer_durable`）。

批量去重用一个事务（[metadata_store.py:L206-L227](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/control_plane/metadata_store.py#L206-L227)）：用 `BEGIN IMMEDIATE` 立即拿写锁，逐条 `INSERT OR IGNORE`，靠 `cur.rowcount == 1` 判断「这条是不是新插入」（重复则 IGNORE、rowcount=0）。整批共享一个 FULL-synchronous 事务，把刷盘开销摊销到整批 ref 上。失败则 `rollback`。

durable ack 是「一个事务同时提交 ack ids 与 optimizer marker」（[metadata_store.py:L256-L278](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/control_plane/metadata_store.py#L256-L278)）：先 `executemany` 插 ack ids，再 `INSERT OR REPLACE` 写 marker，最后一次 `commit`。注释：「ONE transaction commits ack ids and the optimizer marker together.」——ack 集合与步数标记要么一起可见、要么都不可见，这是恢复契约能成立的基础。

#### 4.2.4 代码实践

**实践目标**：亲手感受三种 store 的「记忆强度」差异。

**操作步骤**：

1. 阅读 [metadata_store.py:L181-L201](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/control_plane/metadata_store.py#L181-L201)，确认 SQLite 建了哪三张表、设了哪两个 PRAGMA。
2. 用 **示例代码** 对比 `NoOp` 与 `InMemory`：

```python
# 示例代码
from specforge.runtime.control_plane.metadata_store import NoOpMetadataStore, InMemoryMetadataStore
from specforge.runtime.contracts import SampleRef, FeatureSpec

r = SampleRef(sample_id="s0", run_id="r", source_task_id="t",
              feature_store_uri="m://r/s0", feature_keys={"h":"s0/h"},
              feature_specs={"h": FeatureSpec("h",(1,8,4),"float32")},
              strategy="eagle3", num_tokens=8)

nop, mem = NoOpMetadataStore(), InMemoryMetadataStore()
print(nop.commit_sample(r), mem.commit_sample(r))   # True True
print(nop.commit_sample(r), mem.commit_sample(r))   # True False  <- NoOp 不记，InMemory 去重
print(nop.committed_count(), mem.committed_count()) # 0 1
```

**需要观察的现象**：第二次提交时，`NoOp` 仍说新鲜（count=0，啥也没记），`InMemory` 说不新鲜（count=1，记下了）。

**预期结果**：输出 `True True` / `True False` / `0 1`。

**若无法本地运行**：对照 [tests/test_runtime/test_noop_metadata_store.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/tests/test_runtime/test_noop_metadata_store.py) 的断言理解行为，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 SQLite 用 `INSERT OR IGNORE` + `cur.rowcount == 1` 来判断新鲜，而不是先 `SELECT` 再决定 `INSERT`？

**参考答案**：先查再插存在 TOCTOU 竞态（两条并发插入可能都查到「不存在」然后都插入）。`INSERT OR IGNORE` 是单条原子语句，配合 PRIMARY KEY 约束，重复插入被数据库层拒绝（rowcount=0），天然去重且无竞态。又因为整批在一个 `BEGIN IMMEDIATE` 事务里，写锁从一开始就被独占持有。

**练习 2**：把 `synchronous` 从 `FULL` 改成 `NORMAL` 会牺牲什么？为什么在线 consumer 不能接受？

**参考答案**：`NORMAL` 在 WAL 模式下只在 checkpoint 时刷盘，崩溃（非断电）通常不丢数据，但**断电**可能丢失最近的事务。在线 consumer 的 durable ack 是「这一批样本已经贡献了梯度」的法律凭证——若断电丢了 ack，恢复时会重放本已训练过的样本，违反恢复契约。所以必须 `FULL` 保证 ack 能扛断电。

---

### 4.3 DPAckController 与 rank0 单一写账本

#### 4.3.1 概念说明

数据并行（DP）consumer 下，每个 rank 训练自己那一片不相交的数据，但 durable marker `{acked, global_step, optimizer_durable}` 必须只有**一个写入者**。否则 N 个 rank 会把各自的局部 ack 集合交错写进同一份账本，产生破碎、不一致的状态。

`DPAckController` 就是解决这个问题的——它继承 `DataFlowController`，把 `ack_train_refs` 改造成一次 **DP collective（集合通信）**：

- **rank0（authority，`is_authority=True`）**：持有这一份 run 的唯一持久 store，负责记录「gather 后的并集」。
- **其他 rank（`is_authority=False`）**：参与 gather（集合通信需要每个 rank 都在场），但**什么都不记**，给它一个一次性的 in-memory store 即可。

这能成立，依赖一个 lockstep 不变量：`TrainerController.fit` 在**每个** optimizer 边界、在**每个** rank 上都调用 `ack_fn`，所以 `ack_train_refs` 天然是一次 collective——所有 rank 同步贡献自己那一片的 id。

#### 4.3.2 核心流程

```
每个 rank 在 optimizer 边界调用 ack_train_refs(local_ids, global_step, optimizer_durable=True)
        |
        v
local_ids = 去重本 rank 的 sample_ids
union = gather_id_union(local_ids)        # all_gather_object，rank 序去重并集
        |
        v  (只有 rank0 是 authority)
if is_authority:
    super().ack_train_refs(union, ...)    # 唯一一次写持久账本
commit_error = broadcast(rank0 的提交结果)  # 所有 rank 观察同一结果
if commit_error: raise                    # 任何 rank 失败 -> 全体失败
        |
        v  (清理：每个 rank 删自己本地的特征对象)
if optimizer_durable:
    for sid in local_ids: feature_store.abort(sid)   # 只删自己 materialize 过的
cleanup_error = gather(每 rank 的清理失败)            # 第二次 collective
if cleanup_error: raise
```

两个关键不变量：

1. **提交是单一权威**：只有 rank0 写账本，结果广播给所有 rank。
2. **清理是全秩感知的**：每个 rank 拥有独立的 feature-store client，只物化过自己那一片，所以只能删自己的；但一个非权威 rank 可能在 rank0 成功时失败，所以清理结果也要 gather——在任何一个 rank 能推进 inbox 计数之前，所有 rank 必须都观察到清理结果。

#### 4.3.3 源码精读

`gather_id_union`（[dp_ack.py:L33-L58](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/control_plane/dp_ack.py#L33-L58)）：用 `all_gather_object` 收集每个 rank 的 id 列表，按 rank 序合并去重，保留首次出现。**当 torch.distributed 不可用 / 未初始化 / world=1 时退化为恒等**——这让单 rank 路径也能用同一套代码。注释还指出一个巧妙之处：SP（序列并行）下 peer 之间会复制相同的 id，去重正好让它们塌缩。

`DPAckController.ack_train_refs`（[dp_ack.py:L140-L185](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/control_plane/dp_ack.py#L140-L185)）的核心两段：

```python
union = self._gather(local_ids)
commit_error = None
if self.is_authority:
    try:
        super().ack_train_refs(trainer_id, union, ...)   # 唯一一次写账本
    except BaseException as exc:
        commit_error = f"{type(exc).__name__}: {exc}"
commit_error = self._sync_error(commit_error)            # 广播 rank0 结果
if commit_error is not None:
    raise RuntimeError(f"durable DP acknowledgement failed: {commit_error}")
```

注意「在 rank0 提交成功之前，任何 rank 都不得物理删除特征」——这是 `super().ack_train_refs` 上面注释的硬约束：SQLite 的 ack ids + optimizer marker 是唯一的权威事实。

清理段（optimizer_durable 时）每个 rank 只 abort 自己的 `local_ids`，再 `_sync_cleanup_error` 做第二次 collective gather（[dp_ack.py:L168-L185](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/control_plane/dp_ack.py#L168-L185)）。docstring 解释为什么要第二次 collective：「一个非权威 rank 拥有不同的 Mooncake client，可能在 rank0 成功时失败；所有 rank 必须在调用方推进 inbox ack 之前观察到这个失败」。

权烕分工在 DESIGN.md 的拓扑表里也写得很清楚（[DESIGN.md:L22-L33](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/control_plane/DESIGN.md#L22-L33)）：在线 consumer 那一行写的是「Rank 0 owns the only fresh retaining ledger; every rank owns a DPAckController view」。

#### 4.3.4 代码实践

**实践目标**：理解为何「单一写账本」不能简单地让每个 rank 各写各的。

**操作步骤**（源码阅读型实践）：

1. 阅读 [dp_ack.py:L140-L166](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/control_plane/dp_ack.py#L140-L166)，确认只有 `is_authority` 分支才调 `super().ack_train_refs`。
2. 假想一个反例：若 4 个 rank 各自直接调 `SQLiteMetadataStore.record_train_ack` 写同一个 SQLite 文件，会发生什么？

**需要观察的现象 / 思考结论**：

- 4 个 rank 交错写 → `acked` 集合破碎、`global_step` 被反复覆盖，`durable_marker` 读出来的状态不可信。
- SQLite 单写者模型下并发写还会频繁 `database is locked`。
- `DPAckController` 通过 gather 得到完整并集、只让 rank0 写一次、再把结果广播，彻底消除这些问题。

**预期结果**：你能用一句话说清「为什么必须先 gather 再由单一权威写」——**因为 durable marker 是一份全局事实，而每个 rank 只看到自己的局部切片**。

#### 4.3.5 小练习与答案

**练习 1**：`gather_id_union` 在 `world=1` 时直接返回 `list(ids)`，这一退化路径有什么实际意义？

**参考答案**：在线 consumer 在 `dp_size=1` 时仍走 `DPAckController`（DESIGN.md 明确「One-rank runs use this exact path as well; there is no direct-channel consumer branch」）。退化让单 rank 不依赖分布式通信，又复用了同一套「gather→权威写→广播」代码，避免维护两条分支。

**练习 2**：为什么清理（abort 特征对象）的失败也要做一次全秩 gather，而不是只让 rank0 报告？

**参考答案**：每个 rank 拥有独立的 feature-store client，只物化过自己 DP 片的特征对象，所以只有它自己能删自己的对象。rank0 成功不代表其他 rank 成功。若不在全秩 gather 清理结果就让调用方推进 inbox 计数，一个静默失败的 rank 会泄漏远端对象、却以为已清理完毕。所以必须在任何 rank 推进之前，让所有 rank 都观察到完整的清理结果。

---

### 4.4 freshness 与恢复契约

#### 4.4.1 概念说明

账本除了「记账」，还要回答两个更难的问题：

1. **freshness**：怎样保证一个新 consumer 拿到的是一份干净的、未被污染的账本？
2. **recovery**：consumer 崩溃重启后，怎样从账本重建训练状态，既不丢样本、也不重复训练已 ack 的样本？

SpecForge 的回答是两条契约：

- **freshness 契约**：每次新的 consumer（包括 `dp_size=1`）必须用一条**全新的 SQLite 路径**，运行时会拒绝已存在的 db / WAL / SHM 文件，也拒绝任何已含 committed 行的账本。这是「fail closed」式防御——宁可拒绝启动，也不在一个来历不明的账本上继续写。
- **恢复契约**：在线 disaggregated 的恢复是 **consumer-only** 的。带着原始 SQLite 账本、channel/inboxes、Mooncake 对象与**匹配的 checkpoint**，rank0 校验 durable step、跳过已 ack 的 ref、把未 ack 的尾部重新入队。producer **绝不** resume，一条已消费的流**绝不**当作第二个 trainer epoch 迭代。

最关键、也最容易被误解的一条是：

> **The durable acknowledgement step must equal the restored checkpoint step.**

即「durable ack 的步数必须等于恢复时加载的 checkpoint 步数」。

#### 4.4.2 核心流程

freshness 契约（启动时）：

```
新 consumer 启动
  -> 检查 SQLite 路径：若 .db / .db-wal / .db-shm 任一存在 -> 拒绝（_claim_fresh_control_path）
  -> 打开全新 SQLite，建表
  -> rank0 成为唯一权威
```

恢复契约（崩溃后重启时），对应 `reconcile_on_restart`：

```
重开同一份 SQLite 账本（含已 committed 行 + acked 集合 + marker）
  -> 读取 durable_marker: {acked, global_step, optimizer_durable}
  -> 遍历 all_committed_ids:
       if optimizer_durable and sample_id in acked:
           released.append(...)        # 已 ack -> 释放，并 abort 远端特征
       else:
           sample_queue.put([ref])     # 未 ack -> 重新入队，at-least-once 训练
  -> 校验: durable ack step == checkpoint step
       否则 -> 恢复失败（fail closed，绝不拿旧 optimizer 状态重放已消费 ref）
```

为什么要让「durable ack step == checkpoint step」对齐？因为 ack 与 checkpoint 是**两份独立持久化的事实**：

- **ack** 记的是「这批样本已贡献梯度」（账本里）。
- **checkpoint** 记的是「optimizer 已经走到第几步、权重长什么样」（磁盘上）。

如果二者错位（比如 ack 已推进到 step 5，但 checkpoint 只到 step 4），那么「已 ack 的样本」对应的梯度**并没有真正落进 checkpoint 的权重里**。此时若强行恢复，等于用 step 4 的旧权重去训练「自以为已 ack」的样本——既可能漏训（ack 了但权重没更新），也可能错训。SpecForge 选择「fail closed」：宁可恢复失败，也不在这种灰色地带继续。

ack 与 checkpoint 在训练循环里天然对齐，因为它们都钉在**同一个 optimizer 边界**、用**同一个 `global_step`**：`ack_fn` 与 `save_checkpoint` 都在 `TrainerController` 的 optimizer 边界触发（见 [training/controller.py:L589-L618](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/controller.py#L589-L618)），`ack_fn` 把 `global_step` 原样传给 `record_train_ack`（见 [trainer.py:L434-L441](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/trainer.py#L434-L441)），而 `save_checkpoint(step)` 用同一个 `global_step` 落盘。

DESIGN.md 还点出一个有意为之的「不可恢复窗口」：ack 可能在最近一次周期性 checkpoint **之后**继续推进（周期 checkpoint 之间还有 ack），所以这段区间内的崩溃是**故意不可恢复**的——「recovery fails closed rather than replaying consumed refs against older optimizer state」（见 [DESIGN.md:L96-L115](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/control_plane/DESIGN.md#L96-L115)）。

#### 4.4.3 源码精读

freshness 契约的落点——拒绝复用已有控制路径（[training/disaggregated.py:L72-L77](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/disaggregated.py#L72-L77) 的 `_claim_fresh_control_path`），它会检查 db / WAL / SHM / claim 文件任一存在则拒绝。在线 consumer 侧的调用见 [training/disaggregated.py:L524](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/disaggregated.py#L524)，注释点明「producer 拥有捕获与显式 attempt 清理，consumer 必须保留特征直到 DPAckController 提交 optimizer 边界」。

恢复重建（[controller.py:L228-L266](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/control_plane/controller.py#L228-L266)）：`reconcile_on_restart` 从 `durable_marker()` 取 `{acked, global_step, optimizer_durable}`，遍历 `all_committed_ids()`，对「optimizer_durable 且在 acked 里」的样本释放（并 `feature_store.abort`），其余重新 `sample_queue.put`。注意它还会调 `feature_store.adopt(ref)`——因为重启后的权威持有一个**新的** Mooncake client，可能从未物化过属于其他 DP rank 的 ref，所以要先从持久 committed ref 种下 generation/key 账目，再让它删远端对象。

checkpoint 与 durable step 对齐：恢复加载 checkpoint 后，trainer 把 `last_checkpoint_step` 设成 resume 的 `global_step`（[trainer.py:L473-L477](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/trainer.py#L473-L477)），注释明确「loaded checkpoint already represents this durable step」。而 ack 的步数与 checkpoint 步数来自同一个 `global_step`（[trainer.py:L434-L441](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/trainer.py#L434-L441)），保证二者数值上对齐。

恢复行为的测试佐证——崩溃后重放未 ack 的样本、跳过已 ack 的前缀（[tests/test_runtime/test_recovery.py:L67-L86](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/tests/test_runtime/test_recovery.py#L67-L86)）：先 commit s0/s1/s2，模拟「SQLite 已 ack s0/s1 后进程死亡」，重开后 `reconcile_on_restart` 报告 `released=={s0,s1}`、`requeued==[s2]`，且 s0/s1 被 abort、s2 被重新入队。

#### 4.4.4 代码实践（本讲主实践任务）

**实践目标**：说明在线 consumer 为何只有 rank0 读写 SQLite 账本，并解释「durable ack step 必须等于 checkpoint step」这条恢复约束的用意。

**操作步骤**：

1. **回答 rank0 单一写账本**：阅读 [DESIGN.md:L52-L65](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/control_plane/DESIGN.md#L52-L65) 与 [dp_ack.py:L101-L116](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/control_plane/dp_ack.py#L101-L116)，组织如下回答要点：
   - SQLite 文件是单写者模型，并发写会 `database is locked`；
   - durable marker 是一份全局事实，每个 rank 只看到自己的 DP 切片，必须 gather 成并集后由单一权威写一次；
   - rank0 是唯一的记账权威（bookkeeping authority），其他 rank 只参与 collective 但不落盘。
2. **回答 durable ack step == checkpoint step**：阅读 [DESIGN.md:L109-L112](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/control_plane/DESIGN.md#L109-L112)，组织如下回答要点：
   - ack（账本）与 checkpoint（权重）是两份独立持久化的事实，恢复时必须数值对齐；
   - 若 ack 推进到 step 5 而 checkpoint 只到 step 4，意味着「已 ack 样本的梯度没落进权重」，此时恢复会拿旧权重重放已消费 ref，违反正确性；
   - 所以 SpecForge 选择 fail closed：宁可恢复失败，也不在灰色地带继续；周期 checkpoint 之后的 ack 推进窗口内的崩溃是故意不可恢复的。
3. **跑恢复测试验证**（若环境可用）：

```bash
# 在仓库根目录
python -m pytest tests/test_runtime/test_recovery.py -v
```

**需要观察的现象**：`test_crash_after_durable_ack_skips_and_releases_only_acked_prefix` 通过，证明已 ack 前缀被跳过+释放、未 ack 尾部被重放。

**预期结果**：上述测试全部通过；你能不看源码复述「为何 rank0 单写」「为何 ack step 要等于 checkpoint step」两点。

**若无法本地运行**：标注「待本地验证」，改为对照 [test_recovery.py:L67-L86](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/tests/test_runtime/test_recovery.py#L67-L86) 的断言阅读行为：`released=={s0,s1}`、`requeued==[s2]`、`aborted` 恰为 `{s0,s1}`。

#### 4.4.5 小练习与答案

**练习 1**：freshness 契约要求「每次新 consumer 必须用全新 SQLite 路径，拒绝已存在的 db/wal/shm」。为什么不允许「复用旧账本续写」？

**参考答案**：旧账本的来历不可信——它可能属于上一次失败 attempt、可能已被部分 ack、可能 WAL 还未 checkpoint。在一个被污染的账本上续写，会让 freshness（去重）与 recovery（ack 对齐）都失去前提。SpecForge 选择「新 attempt 必须全新账本」，把可恢复性收敛到「带着原始账本 + 匹配 checkpoint 显式 resume」这一条受控路径，其余一律 fail closed。

**练习 2**：假设 ack 已推进到 global_step=10，但最近一次周期 checkpoint 只到 step=8，此时进程崩溃。按恢复契约，会发生什么？

**参考答案**：恢复会失败（fail closed）。因为 durable ack step（10）≠ 可恢复的 checkpoint step（8），二者错位意味着 step 9、10 的 ack 样本的梯度没有落进任何 checkpoint 权重。SpecForge 不会拿 step 8 的权重去重放这些「自以为已 ack」的样本，而是直接报恢复失败。这正是 DESIGN.md 所说的「周期 checkpoint 之后 ack 推进窗口内的崩溃故意不可恢复」。

**练习 3**：`reconcile_on_restart` 为何要对已 ack 的样本调 `feature_store.abort`，而不只是从队列里删掉？

**参考答案**：因为已 ack 的样本对应的**远端特征对象**（如 Mooncake 里的张量）还占着存储。consumer 在 ack 时通过 `retain_on_release=True` 保留了它们（见 disaggregated.py 的 store 构造），就是为了应对「ack 后、清理前」崩溃。`reconcile_on_restart` 在确认这些样本已被 durable ack 覆盖后，显式 abort 远端对象释放存储；若只删队列不 abort，就会泄漏远端特征对象。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面这个「画一张在线 consumer 控制面时序图并配账本状态」的任务：

1. **画图**：参照 [DESIGN.md:L37-L50](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/control_plane/DESIGN.md#L37-L50) 的 mermaid 图，手绘（或用工具）一张在线 consumer 的控制面时序图，至少包含：`StreamingRefChannel → RefDistributor(rank0) → SQLite 账本(commit 去重) → 每 rank InboxChannel → trainer → DPAckController(all_gather) → rank0 写一次账本`。
2. **标注账本状态**：在图上标出三个时刻 SQLite 三张表（`committed` / `acked` / `marker`）的内容变化：
   - (a) RefDistributor commit 一批新 ref 后；
   - (b) DPAckController 完成 step=1 的 durable ack 后；
   - (c) 进程在 (b) 之后、下一次 checkpoint 之前崩溃，重启 `reconcile_on_restart` 后。
3. **写一段判断**：针对 (c)，写出「此时 durable ack step 是否等于 checkpoint step」的判断依据，并说明若不等会发生什么。

**完成标准**：你能指着图说清——commit 阶段只动 `committed` 表，ack 阶段同时动 `acked` 与 `marker`（一个事务），恢复阶段读 `marker` 决定释放/重放，且 ack step 与 checkpoint step 的对齐是恢复成立的硬前提。

## 6. 本讲小结

- `DataFlowController` 是控制面的元数据调度器，提供 prompt 注入/租赁/重试、`SampleRef` 提交去重、durable ack 三类原语，每个接受 record 的方法都跑 `assert_no_tensors`，**它没有运行循环、绝不调用 rollout 或 trainer**。
- 去重与 ack 不放在 controller 的本地字典，而是放在注入的 `MetadataStore` 账本里，使账本可替换、可持久。
- 三种账本后端对应三种拓扑：`NoOpMetadataStore`（colocated/在线 producer，啥都不记）、`InMemoryMetadataStore`（进程内记账、非权威 DP rank）、`SQLiteMetadataStore`（在线 consumer 的唯一持久权威，WAL + synchronous=FULL 保证 ack 能扛断电）。
- 在线 consumer「rank0 单一写账本」由 `DPAckController` 保证：它把 `ack_train_refs` 改造成一次 DP collective，先 gather 全秩 id 并集，再只让 rank0 写一次持久账本，最后广播结果；清理也做第二次全秩 gather。
- freshness 契约要求每个新 consumer 用全新 SQLite 路径，拒绝复用 db/wal/shm；恢复契约是 consumer-only，跳过已 ack 前缀、重放未 ack 尾部。
- **「durable ack step 必须等于 checkpoint step」**：ack（账本）与 checkpoint（权重）是两份独立事实，必须数值对齐；周期 checkpoint 之后 ack 推进窗口内的崩溃故意不可恢复（fail closed），绝不拿旧 optimizer 状态重放已消费 ref。

## 7. 下一步学习建议

本讲把控制面的「记账」讲透了，但还没讲「发货」——样本引用如何从 producer 流到每个 consumer rank。建议接着读：

- **u7-l3 数据平面 feature store 与传输**：`FeatureStore` 契约、Local/shared_dir/Mooncake 三种后端、`FeatureDataLoader` 如何把 refs 取回成 `TrainBatch`——补全「张量如何搬运」这一半。
- **u7-l4 在线引用分发与流式队列**：`RefDistributor → InboxChannel → StreamingRefQueue` 的分发链、quantum 握手、`DPAckController` 在分发链中的角色——本讲的 controller 与 dp_ack 在那里被驱动起来。
- 若想看 control plane 的完整拓扑鸟瞰，回看 [u7-l1 运行时架构与四条路径](u7-l1-runtime-architecture.md) 的四平面分工与 canonical 在线流图。
