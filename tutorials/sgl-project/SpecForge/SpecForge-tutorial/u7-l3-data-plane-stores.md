# 数据平面 feature store 与传输

## 1. 本讲目标

上一讲（u7-l2）我们放大了「控制面」——它只搬元数据、用 SQLite 账本记账。本讲放大与之配对的「数据面」：**特征张量到底存在哪、怎么跨进程搬、又怎么变成训练能吃的 `TrainBatch`**。

学完本讲你应该能够：

- 说清 `FeatureStore` 这一份抽象契约的五个生命周期方法（`put/get/release/abort/gc`）及其背后的「租约（lease）+ 代际（generation）」机制。
- 解释为什么 `LocalFeatureStore`（进程内）、`SharedDirFeatureStore`（共享目录）、`MooncakeFeatureStore`（RDMA 对象存储）三种后端能让 trainer 与 loader **对传输方式完全无感**。
- 指出离线 `refs` 模式（可重迭代、可续训）与在线 `queue` 模式（一次性消费、永不重放）的根本差异，并能在源码里找到这一差异的落点。

## 2. 前置知识

本讲是专家层内容，默认你已掌握以下概念（前序讲义已建立）：

- **控制面 / 数据面边界**（u5-l4、u7-l1）：控制面只传元数据，张量只在数据面流动；`SampleRef` 是纯元数据指针，`assert_no_tensors` 把这一约定焊成硬约束。
- **`SampleRef` 与 `FeatureSpec`**（u5-l4）：`SampleRef` 用 `feature_store_uri` + `feature_keys` 指向一个样本的全部特征，自己不持任何张量；`FeatureSpec` 描述单个命名张量的 `shape/dtype`。
- **四条路径与统一训练主链路**（u7-l1）：`Trainer → FeatureDataLoader → TrainerController → TrainerCore`，拓扑差异几乎全收敛成两个变量——参考源 `ref_source`（离线 refs / 在线 queue）与特征存储 `store`。
- **quantum**（u7-l1）：一次 optimizer 步对应的样本窗口大小 \( \text{quantum} = \text{dp\_size} \times \text{batch\_size} \times \text{accumulation\_steps} \)，是 producer/consumer 握手单位。

一句话回顾：**数据面的职责是「拥有张量、搬运张量、把 ref 物化成 batch」**，它绝不让张量进入控制面。

## 3. 本讲源码地图

本讲涉及的关键文件都在 `specforge/runtime/data_plane/` 子包下：

| 文件 | 作用 |
| --- | --- |
| [feature_store.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/feature_store.py) | 定义抽象契约 `FeatureStore` 与进程内实现 `LocalFeatureStore`，承载租约/代数/clone-on-fetch 等基元。 |
| [disaggregated.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/disaggregated.py) | 共享目录后端 `SharedDirFeatureStore` 与共享密钥鉴权 `AuthPolicy`。 |
| [mooncake_store.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/mooncake_store.py) | RDMA 零拷贝后端 `MooncakeFeatureStore`，在线分离式训练的主力传输。 |
| [offline_reader.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/offline_reader.py) | `OfflineManifestReader`：把预计算的 `.ckpt` 文件变成 `file://` 的 `SampleRef`（不复制张量）。 |
| [disagg_ingest.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/disagg_ingest.py) | 离线分离式 producer：把特征 `put()` 进 store，并把元数据写成 JSON manifest。 |
| [feature_dataloader.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/feature_dataloader.py) | `FeatureDataLoader`：`SampleRef + FeatureStore → TrainBatch` 的唯一桥梁，分 `refs` 与 `queue` 两模式。 |

辅助文件 `sample_ref_queue.py`（进程内元数据队列）、`ref_serialization.py`（ref 的 JSON 序列化）会在讲到 ingset 与 queue 时带出。装配侧的接线点在 [training/trainer.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/trainer.py) 与 [training/disaggregated.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/disaggregated.py)、[launch.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch.py)。

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：先讲抽象契约与进程内实现，再讲两个跨进程后端，然后讲离线特征如何变成 ref，最后讲把这些粘合成 batch 的 `FeatureDataLoader`。

### 4.1 FeatureStore 契约与 LocalFeatureStore

#### 4.1.1 概念说明

`FeatureStore` 是数据面的**唯一张量拥有者**。它的模块文档第一句就定下基调：「数据面拥有特征存储、跨进程引用传输，以及从 `SampleRef` 元数据到携带张量的 `TrainBatch` 的唯一桥梁」。控制面的 controller、SQLite 账本、manifest、JSONL 通道统统**不持有张量**——张量永远待在某个 `FeatureStore` 里。

为什么需要这样一份抽象？因为 SpecForge 要用同一套训练代码支撑三种很不一样的存储介质：

- **进程内内存**（colocated 在线、单测）；
- **共享文件目录**（离线分离式的 CPU 可测后端）；
- **RDMA 对象存储**（在线分离式跨节点零拷贝）。

如果 trainer 直接调 `torch.load` 或 Mooncake 客户端，每换一种部署形态就要改训练代码。于是 SpecForge 把「存取张量」抽象成五个生命周期方法，所有后端实现同一契约，trainer 只认契约、不认后端。

`FeatureStore` 自身的模块注释明确说它「不携带任何调度状态」（[feature_store.py:L111](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/feature_store.py#L111)）：它只管张量的生与死，不管「这个样本该给哪个 rank、是否已 ack」——那些是控制面的事。

#### 4.1.2 核心流程

`FeatureStore` 的生命周期由五个操作构成，外加 `estimate_bytes`/`health` 两个辅助：

```text
put(tensors, sample_id, metadata)
   └─► SampleRef            # 张量落库，返回纯元数据指针（不含张量）

get(sample_ref, device, names)
   └─► (tensors, FeatureHandle)   # 读出张量 + 一张「租约」令牌

release(handle, reason="consumed")
   └─► 归还租约；若是最后一个租约则真正释放（consume-once）

abort(sample_id, reason)          # 立即驱逐（失败/废弃的样本）

gc(now=None)                      # 回收背压和 release 兜不住的「流浪样本」
```

围绕这五个方法有三组关键不变量（见模块顶部 [feature_store.py:L22-L43](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/feature_store.py#L22-L43)）：

1. **代数进 URI（generation-in-URI）**：`mem://` 引用把代数写进 URI，`get()` 会拒绝代数已不匹配的旧 ref。这堵住了一个「至少一次」重投漏洞——一个过期 ref 不会悄悄别名到一个刚被重新 put 的新样本。
2. **原子租约注册**：对 `mem://`，「读出张量」与「登记租约」在同一把锁内完成，使得并发的 `abort` 无法插进「我刚读到张量」和「我刚登记借阅」之间。
3. **尽力而为的 dump**：可选的磁盘 dump 失败不会撤销一次成功的内存发布（内存是权威，磁盘只是旁路 tap）。

内存有界性由三个机制协同（文档称 M5）：

- **一次性释放（consume-once free）**：`release()` 在最后一个租约归还时释放 `mem://` 样本（稳态边界）。
- **背压（backpressure）**：`max_resident_bytes` 让「consumer 跟不上」在 `put` 时变成一个响亮的 `MemoryError`，而非静默 OOM。
- **GC / max-hold**：`gc()` 回收那些背压也释放不了的流浪样本。

「租约」和「代数」是这套设计的两个核心词。**租约**（`FeatureHandle`，u5-l4 讲过）是一次 `get` 的归还凭证；**代数**是一个全局单调递增计数器，每次 re-put 都换新代数，保证旧 ref 永不别名新数据。

#### 4.1.3 源码精读

**抽象契约**定义在 [feature_store.py:L110-L156](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/feature_store.py#L110-L156)：五个抽象方法 + `estimate_bytes`/`health` 默认实现 + `gc` 默认实现。注意 `gc` 在基类是「什么都不扫」的默认值，留给有独立回收策略的后端覆盖：

```python
class FeatureStore(abc.ABC):
    """Stores and serves large feature tensors. Carries no scheduling state."""

    @abc.abstractmethod
    def put(self, tensors, *, sample_id, metadata) -> SampleRef: ...

    @abc.abstractmethod
    def get(self, sample_ref, *, device="cpu", names=None) -> Tuple[Dict, FeatureHandle]: ...

    @abc.abstractmethod
    def release(self, handle, *, reason="consumed") -> None: ...

    @abc.abstractmethod
    def abort(self, sample_id, *, reason: str) -> None: ...

    def gc(self, *, now=None) -> Dict[str, int]: ...   # 默认不扫
```

**`put` 的背压与代数**（[feature_store.py:L319-L331](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/feature_store.py#L319-L331)）：先在锁外物化样本，再进锁检查预算，超预算在「提交前」抛 `MemoryError`（无回滚负担），然后取一个新代数：

```python
with self._lock:
    if self.max_resident_bytes is not None:
        projected = self._resident_bytes_locked() + staged_bytes
        if projected > self.max_resident_bytes:
            raise MemoryError(f"... over budget ...; consumer is behind")
    gen = next(self._gen_counter)          # 单调递增，re-put 永不复用
    self._generation[sample_id] = gen
    self._mem[sample_id] = staged
```

**`get` 的两种风味**（[feature_store.py:L377-L383](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/feature_store.py#L377-L383)）：靠 URI 前缀透明分发，loader/trainer 路径对在线/离线完全一致：

```python
uri = sample_ref.feature_store_uri
if uri.startswith("file://"):
    tensors = self._get_from_file(uri[len("file://"):], sample_ref, wanted)   # 离线：按需读盘
    handle = self._register_file_lease(sample_ref)
else:
    tensors, handle = self._get_from_mem(sample_ref, wanted)                  # 在线：读内存
```

**`_get_from_mem` 的代数校验与原子租约**（[feature_store.py:L404-L424](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/feature_store.py#L404-L424)）：resident 读与租约登记共享一把锁，代数不匹配直接 `KeyError`：

```python
expected_generation = _mem_uri_generation(ref.feature_store_uri)
with self._lock:
    if ref.sample_id not in self._mem:
        raise KeyError(...)
    gen = self._generation.get(ref.sample_id, 0)
    if expected_generation is not None and gen != expected_generation:
        raise KeyError("... generation ... is not resident ...")   # 旧 ref 拒绝别名新数据
    ...
    self._active_leases[handle.lease_token] = handle
```

**`release` 的 consume-once 逻辑**（[feature_store.py:L466-L482](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/feature_store.py#L466-L482)）：只统计「当前代数」上的租约；当最后一个当前代数租约归还时才真正释放。注意它按「当前代数」而非「handle 自己的代数」判定，这样「re-put 时旧租约还在」也能正确回收新代数（注释里有详细反例）：

```python
cur = self._generation.get(sid)
if cur is not None and handle.generation != cur:
    return  # 旧 handle（样本已 re-put）→ 空操作
if self._still_leased_locked(sid, cur):
    return  # 还有别的（当前代数）租约占着
if not self._try_physical_free(sid):
    self._release_pending.setdefault(sid, 0)   # 远程后端失败时留给 gc 重试
```

`file://` 样本从不进 `_mem`，所以对它 `release` 是无害的空操作——这正是离线特征能被多 epoch 反复读的关键（见 4.4）。

#### 4.1.4 代码实践

**实践目标**：用进程内内存模式亲手走一遍「put → get → release → 观察」，验证 consume-once 与代数守卫。

**操作步骤**（最小调用示例，标注为示例代码，需在装好 torch 的 SpecForge 环境运行）：

```python
# 示例代码：需本地验证
import torch
from specforge.runtime.data_plane import LocalFeatureStore

store = LocalFeatureStore()  # 进程内，dump_dir=None
ref = store.put(
    {"input_ids": torch.tensor([1, 2, 3])},
    sample_id="s0",
    metadata={"run_id": "demo", "strategy": "eagle3"},
)
print("uri =", ref.feature_store_uri)   # mem://<store_id>/s0?generation=1

tensors, handle = store.get(ref)
print("resident_samples =", store.health()["resident_samples"])  # 1
store.release(handle, reason="consumed")
print("resident_samples =", store.health()["resident_samples"])  # 0（最后一个租约归还即释放）

# 代数守卫：已释放的 ref 再 get 应抛 KeyError
try:
    store.get(ref)
except KeyError as e:
    print("expected KeyError:", e)
```

**需要观察的现象 / 预期结果**：`release` 后 `resident_samples` 立即归零（consume-once）；再次 `get` 同一个 ref 抛 `KeyError`（代数守卫、无 use-after-free）。`uri` 里的 `generation=1` 体现了「代数进 URI」。

> 若本机无 torch/无 SpecForge 运行环境，此为「待本地验证」。退而求其次的源码阅读型实践：阅读 [feature_store.py:L452-L482](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/feature_store.py#L452-L482) 的 `release`，解释为什么注释里那个「gen N 占着、gen N+1 借了又还、最后才还 gen N」的场景不会泄漏。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `put` 的超预算检查要放在「取代数、写入 `_mem`」之前？
**答案**：这样超预算时抛 `MemoryError`，但样本尚未提交进 `_mem`，没有任何需要回滚的状态（见 [feature_store.py:L299-L327](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/feature_store.py#L299-L327)）。这是一次「定义清晰的失败」，而非静默 OOM。

**练习 2**：`release` 里 `_still_leased_locked(sid, cur)` 为什么只数「当前代数」的租约，而不是任意租约？
**答案**：因为 re-put 会换新代数。若把旧代数的租约也算上，一个「re-put 时旧租约还在」的样本，其当前代数会在最后一个当前代数租约归还时泄漏。只数当前代数才能保证当前代数被正确回收（见 [feature_store.py:L275-L287](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/feature_store.py#L275-L287)）。

---

### 4.2 跨进程后端：SharedDir 与 Mooncake

#### 4.2.1 概念说明

`LocalFeatureStore` 只在单进程内有意义。当 producer 与 consumer 分离（无论离线分离式还是在线分离式），就需要跨进程后端。SpecForge 提供两个：

- **`SharedDirFeatureStore`**（[disaggregated.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/disaggregated.py)）：producer 和 consumer 是两个进程，只共享一个 POSIX 目录。每个代数是一个独立文件，靠原子 rename 发布。这是 CPU 可测、无需 RDMA 的分离式后端。
- **`MooncakeFeatureStore`**（[mooncake_store.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/mooncake_store.py)）：用 Mooncake 分布式对象存储做 RDMA 零拷贝跨节点传输。producer 在一个节点 `put()`，consumer 在另一节点点对点 `get()`，无需共享文件系统。

关键设计（Mooncake 模块文档 [mooncake_store.py:L26-L51](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/mooncake_store.py#L26-L51)）：这两个后端**在完全相同的 `FeatureStore` API 背后替换传输**，控制面与训练面看不到任何新东西。也就是说——**trainer 和 loader 对传输后端无感**。这是本讲最重要的结论之一。

两个后端都沿用了 4.1 的契约不变量：B5（无 use-after-free：代数守卫 + clone-on-fetch 默认开）、B9（分离式鉴权）。此外它们共享一个新开关 **`retain_on_release`**：

- `retain_on_release=True`（离线）：`release` 不真正释放，特征留在 store 里供多 epoch 反复读；
- `retain_on_release=False`（在线）：`release` 在最后一个租约归还时真正释放（consume-once）。

#### 4.2.2 核心流程

**SharedDir**：代数编码进文件名 `{sample_id}.g{gen}.ckpt`，一次 `os.replace` 原子发布。re-put 会删除被取代的旧代数文件，旧 ref 的文件消失、其 `get()` 抛 `KeyError`（[disaggregated.py:L104-L126](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/disaggregated.py#L104-L126)）。读/数据路径跨进程，而代数与租约索引是进程局部的。

**Mooncake** 的传输契约是「单数的」——每个张量作为裸 buffer 用 `put_from`/`get_into` 传输，shape 和 dtype 走 `SampleRef` 的元数据，**线上从不接受或产出序列化的张量 blob**（[mooncake_store.py:L20-L25](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/mooncake_store.py#L20-L25)）。构造期 `_require_store_api` 会拒绝任何不暴露 `is_exist/remove/put_from/get_into` 的客户端。

Mooncake 还做了三件 SharedDir 不需要的事：

1. **硬钉（hard-pin）而非 LRU**：Mooncake 默认的近似 LRU 会悄悄丢弃「已提交但未 ack」的特征（trainer 落后几小时时 `get()` 变 `KeyError`），违反 controller 的「不丢数据」承诺。所以 SpecForge 对每个对象硬钉，只在显式 `remove()` 时释放——**SpecForge 是唯一的生命周期权威，而不是 Mooncake 的 LRU**（[mooncake_store.py:L33-L43](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/mooncake_store.py#L33-L43)）。
2. **`_freed` 立即逻辑释放**：因为 Mooncake 的 `remove()` 受读租约影响、物理回收有延迟，SpecForge 在本地维护一个 `_freed` 集合，让 B5「无 use-after-free」立即生效——哪怕物理字节还在，已释放 ref 的 `get()` 也立刻抛 `KeyError`（[mooncake_store.py:L499-L516](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/mooncake_store.py#L499-L516)）。
3. **`drain_pending_removals`**：`remove()` 是真实、可能失败的 RPC。`release()` 把失败的释放放进 `_release_pending`，`gc()` 在稳态周期性重试，而生命周期关闭时调用 `drain_pending_removals` 做有界重试——**失败时大声报错，绝不静默丢掉硬钉对象**（[mooncake_store.py:L639-L660](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/mooncake_store.py#L639-L660)）。

#### 4.2.3 源码精读

**鉴权**（[disaggregated.py:L52-L66](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/disaggregated.py#L52-L66)）：`AuthPolicy` 是共享密钥鉴权，`token=None` 即关闭；attach 时与每次数据路径都 `check`：

```python
class AuthPolicy:
    def check(self, presented):
        if self.required and presented != self.token:
            raise PermissionError("disaggregated feature store: auth required ...")
```

**SharedDir 的代数文件名**（[disaggregated.py:L108-L111](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/disaggregated.py#L108-L111)）：代数在文件名里，一次 rename 发布，读者要么看到完整代数文件、要么看不到：

```python
_DATA_RE = re.compile(r"^(?P<sid>.+)\.g(?P<gen>\d+)\.ckpt$")
def _data_path(self, sample_id, gen):
    return os.path.join(self.root, f"{sample_id}.g{gen}.ckpt")
```

**Mooncake 的裸 buffer 零拷贝写**（[mooncake_store.py:L263-L286](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/mooncake_store.py#L263-L286)）：DMA 直送张量存储，注册/注销 buffer，shape/dtype 不上线：

```python
def _store_put_tensor(self, key, t):
    nb = _nbytes(t)
    try: self._store.register_buffer(t.data_ptr(), nb)
    except Exception: pass
    try:
        rc = self._store.put_from(key, t.data_ptr(), nb, self._put_config)   # 零拷贝
    finally:
        try: self._store.unregister_buffer(t.data_ptr())
        except Exception: pass
```

**Mooncake 的短读校验**（[mooncake_store.py:L306-L315](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/mooncake_store.py#L306-L315)）：`get_into` 返回实际读取字节数，短读会让新分配 buffer 尾部残留未初始化垃圾——这里宁可报 `KeyError` 也绝不把静默损坏的数据交给 trainer（B5）：

```python
if int(rc) != nb:
    raise KeyError(f"mooncake get_into short read for {key}: got {rc} of {nb} bytes")
```

**Mooncake 的 `release`**（[mooncake_store.py:L611-L627](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/mooncake_store.py#L611-L627)）：先记 `_freed` 立即逻辑释放，物理释放失败则留给 `gc`：

```python
def release(self, handle, *, reason="consumed"):
    with self._lock:
        self._active_leases.pop(handle.lease_token, None)
        if self.retain_on_release:
            return                       # 离线可重迭代集：留给下一 epoch
        ...
        self._freed.add((sid, handle.generation))   # 立即逻辑释放
        if self._try_physical_free(sid):
            self._free_bookkeeping_locked(sid)
        else:
            self._release_pending.setdefault(sid, 0)   # 远程释放延迟 → gc 重试
```

**装配侧的后端选择**（[training/disaggregated.py:L157-L177](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/disaggregated.py#L157-L177)）：`_offline_store` 用环境变量 `DISAGG_BACKEND` 在 `shared_dir` 与 `mooncake` 间切换，二者构造参数不同但返回的都是 `FeatureStore`——这就是 trainer/loader 无感的落点：

```python
def _offline_store(cfg, *, retain_on_release=False):
    backend = os.environ.get("DISAGG_BACKEND", "shared_dir")
    if backend == "mooncake":
        return _mooncake_store(cfg, retain_on_release=retain_on_release)
    ...
    return SharedDirFeatureStore(_env("DISAGG_STORE_ROOT"), ..., retain_on_release=retain_on_release)
```

#### 4.2.4 代码实践

**实践目标**：理解为什么 trainer/loader 对后端无感，以及 Mooncake 为何强制裸 buffer 传输。

**操作步骤**（源码阅读型实践）：

1. 打开 [mooncake_store.py:L116-L129](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/mooncake_store.py#L116-L129) 的 `_require_store_api`，记下它强制要求的四个方法，并解释为什么「不接受序列化 put/get」。
2. 对照 [training/disaggregated.py:L148-L177](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/disaggregated.py#L148-L177)，说明 `_mooncake_store` 与 `_offline_store(shared_dir)` 返回的对象在**类型**上唯一的共同点是什么（答：都是 `FeatureStore` 子类）。
3. 找到 trainer 构造 loader 的地方（[training/trainer.py:L145-L155](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/trainer.py#L145-L155)），确认它只把 `store` 当作 `FeatureStore` 用，没有任何 `isinstance(MooncakeFeatureStore)` 分支。

**需要观察的现象 / 预期结果**：训练代码里找不到任何针对具体后端的分支；`_require_store_api` 强制 `put_from/get_into` 是为了让 shape/dtype 走 ref、字节直传，避免在线路上序列化大张量。

#### 4.2.5 小练习与答案

**练习 1**：Mooncake 默认是近似 LRU 缓存，SpecForge 为什么要对每个对象硬钉？
**答案**：trainer 落后几小时时，LRU 会悄悄丢弃「已提交但未 ack」的特征，使 `get()` 变 `KeyError`，违反 controller 的「不丢数据」承诺。硬钉 + 显式 `remove()` 让 SpecForge 成为唯一生命周期权威（[mooncake_store.py:L33-L43](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/mooncake_store.py#L33-L43)）。

**练习 2**：`retain_on_release=True` 和 `False` 分别用于什么场景？
**答案**：`True` 用于离线可重迭代特征集（多 epoch、checkpoint 续训需要反复读同一批样本，`release` 不释放）；`False` 用于在线 rollout（consume-once，最后一个租约归还即释放）。

---

### 4.3 离线特征的 ref 来源：OfflineManifestReader 与 disagg_ingest

#### 4.3.1 概念说明

4.2 讲了「后端」，但还没讲「ref 从哪来」。离线训练有两种产生 ref 的方式：

- **colocated 离线**：trainer 直接读本地 `.ckpt` 文件。`OfflineManifestReader` 遍历一个目录，对每个文件产出一个指向 `file://` 的 `SampleRef`——**只引用、不复制**张量（[offline_reader.py:L9-L17](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/offline_reader.py#L9-L17)）。
- **分离式离线**：producer 进程把 `.ckpt` 特征 `put()` 进 `SharedDirFeatureStore`/`MooncakeFeatureStore`，再把返回的 `SampleRef` 列表序列化成一份小 JSON **manifest** 写盘。consumer 等到 producer 的完成哨兵，读这份固定 manifest（[disagg_ingest.py:L9-L21](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/disagg_ingest.py#L9-L21)）。

注意 manifest 是**控制面状态**（只有元数据），所以写盘前要 `assert_no_tensors` 强制边界（[disagg_ingest.py:L100](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/disagg_ingest.py#L100)）。这是「张量不进控制面」铁律的又一次落地。

#### 4.3.2 核心流程

**colocated 离线（file:// ref）**：

```text
for 每个 .ckpt/.ckpt.gz 文件:
    （可选）打开文件验证 feature_keys 存在、是张量
    构造 SampleRef(feature_store_uri=f"file://{abs_path}", feature_keys={k:k}, ...)
    yield ref
```

**分离式离线（ingest → manifest）**：

```text
producer:
  for 每个文件:
      raw = load_feature_file(path)
      tensors = {k: raw[k] for k in feature_keys}
      ref = store.put(tensors, sample_id=f"{run_id}:{idx:08d}", metadata={...})   # 张量落 store
      refs.append(ref); on_ref(ref)                                              # 可供生命周期清理
  assert_no_tensors(refs)                                                        # 写 manifest 前强制
  原子写 manifest（tmp + rename）

consumer:
  等待 producer 完成哨兵
  refs = read_ref_manifest(manifest_path)                                        # 只读元数据
  以 refs 模式喂给 FeatureDataLoader
```

两种方式最终都把一个 `List[SampleRef]` 喂给 `FeatureDataLoader` 的 **refs 模式**——这是「可重迭代」的入口。

#### 4.3.3 源码精读

**`OfflineManifestReader._ref_for`**（[offline_reader.py:L99-L129](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/offline_reader.py#L99-L129)）：核心是 `feature_store_uri=f"file://{path}"`——一个文件路径就足以让 `LocalFeatureStore._get_from_file` 在 `get()` 时按需读盘，张量全程不复制、不进控制面：

```python
def _ref_for(self, index, path):
    sample_id = f"{self.run_id}:{index:08d}"
    if self.validate_files:
        specs, num_tokens, estimated_bytes = _inspect_feature_file(path, self.feature_keys)
    return SampleRef(
        sample_id=sample_id,
        feature_store_uri=f"file://{path}",       # 只引用、不复制
        feature_keys={k: k for k in self.feature_keys},
        ...
    )
```

**`ingest_offline_features`**（[disagg_ingest.py:L35-L91](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/disagg_ingest.py#L35-L91)）：算法专用的 reader 由组合根注入（`build_reader`），传输层不 import 训练算法。每个文件 `put()` 进 store，`on_ref` 在每次成功 put 后立刻回调，让生命周期持有者保留清理所需的 ref：

```python
for index, path in enumerate(paths):
    raw = load_feature_file(path)
    tensors = {key: raw[key] for key in feature_keys}
    ref = store.put(tensors, sample_id=f"{run_id}:{index:08d}", metadata={...})
    refs.append(ref)
    if on_ref is not None:
        on_ref(ref)
```

**`write_ref_manifest` 的原子发布**（[disagg_ingest.py:L94-L109](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/disagg_ingest.py#L94-L109)）：先 `assert_no_tensors`，再 `tmp` + `os.replace`，读者永远不会看到半个文件：

```python
def write_ref_manifest(refs, path):
    assert_no_tensors(refs)                      # manifest 是控制面状态
    payload = {"schema_version": SCHEMA_VERSION, "refs": [ref_to_dict(r) for r in refs]}
    tmp = path + ".tmp"
    with open(tmp, "w") as f: json.dump(payload, f)
    os.replace(tmp, path)                        # 原子发布
```

#### 4.3.4 代码实践

**实践目标**：看清「离线 ref」为何天然可重迭代、可续训。

**操作步骤**（源码阅读型实践）：

1. 读 [offline_reader.py:L33](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/offline_reader.py#L33)，记下 EAGLE3 离线特征文件的四个原始 key（`input_ids/loss_mask/hidden_state/aux_hidden_state`）。
2. 追踪一个 `file://` ref 的取数路径：`OfflineManifestReader._ref_for` → `LocalFeatureStore.get` 的 `file://` 分支（[feature_store.py:L437-L449](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/feature_store.py#L437-L449)）→ `_get_from_file`。
3. 解释：为什么同一个 `.ckpt` 文件可以被同一个 trainer 在 epoch 0 和 epoch 1 各读一遍而不会「消费掉」？

**需要观察的现象 / 预期结果**：`file://` 样本从不进 `_mem`，`release` 对它是空操作，文件始终留在盘上——这就是离线可重迭代的物理基础。

#### 4.3.5 小练习与答案

**练习 1**：`ingest_offline_features` 为什么要由组合根注入 `build_reader`，而不是自己 import 算法？
**答案**：传输层（data_plane）不应依赖训练算法；算法专用的 reader（它知道该算法要哪些 feature_keys、target_repr）由组合根注入，保持传输层算法无关（[disagg_ingest.py:L47-L61](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/disagg_ingest.py#L47-L61)）。

**练习 2**：为什么 manifest 写盘前要 `assert_no_tensors`？
**答案**：manifest 是控制面状态，只能含元数据；这一守卫把「张量不进控制面」从约定焊成硬约束，防止某次改动意外把张量塞进会被跨节点 JSON 传输的 manifest（[disagg_ingest.py:L100](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/disagg_ingest.py#L100)）。

---

### 4.4 FeatureDataLoader：refs / queue 双模式桥接

#### 4.4.1 概念说明

`FeatureDataLoader` 是数据面的**收口**——把 `SampleRef`（元数据）经 `FeatureStore`（张量）物化成 `TrainBatch`（运行时唯一携带张量的契约）。它的模块文档第一句就定下职责边界（[feature_dataloader.py:L9-L15](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/feature_dataloader.py#L9-L15)）：

> 借用 ref（一次性 queue 或可重迭代的 ref 列表），施加注入的 per-sample transform 与 collate，产出 `TrainBatch`——不含任何模型知识。clone-on-fetch（默认）把张量从 store 克隆出来并立刻释放 handle，所以预取绝不会和 release 抢跑。

它有**两种输入模式**，对应离线与在线：

| 模式 | 入参 | 数据语义 | 可重迭代？ | 续训 |
| --- | --- | --- | --- | --- |
| **refs** | `refs=[SampleRef...]` | 固定列表 | ✅ 可多 epoch 反复读 | ✅ `seek` 到保存位置 |
| **queue** | `queue=SampleRefQueue/StreamingRefQueue` | 流式，一次性 | ❌ consume-once，永不作为第二 epoch 重放 | ❌ 只重建未训尾部 |

这是本讲实践任务要回答的核心区分。一句话：**离线 refs 是「数据集」，在线 queue 是「流」**。

#### 4.4.2 核心流程

`FeatureDataLoader` 的物化动作对两种模式完全相同，差别只在「ref 从哪来」与「取完要不要 ack」：

```text
构造期：强制 queue 与 refs 二选一（否则报错）

对每个 batch_size 个 ref:
    tensors, handle = store.get(ref)            # 借张量 + 租约
    若 clone_on_fetch: tensors = {k: v.clone()}  # 默认克隆，隔离 store 内部
    store.release(handle)                        # 立刻归还租约
    若有 per_sample_transform: 施加
_validate_refs(refs)                             # 守卫：strategy/schema_version/target_repr/spec 一致
collate_fn(per_sample) → batch_tensors           # 拼成 batch
return TrainBatch(sample_ids, tensors, metadata)

refs 模式：把固定列表按 batch_size 切块，可多 epoch；seek(num_batches) 跳到续训位置
queue 模式：从 queue 流式 get；drop_last 时尾部不足一个 batch 会被终态清理；
            每个 batch 物化后 queue.ack(refs)（除非 defer_ack_until_durable）
```

为什么默认 `clone_on_fetch=True`？因为预取（`LOADER_PREFETCH` 或 `num_workers`）会让「物化」和「trainer 真正用 batch」错开时间。若不克隆，store 内部的张量可能在 trainer 还没用完时就被 release/abort 回收——克隆出来后立刻释放 handle，预取就绝不会和 release 抢跑（[feature_dataloader.py:L191-L203](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/feature_dataloader.py#L191-L203)）。Mooncake 的 `get()` 本身就分配新张量（`_alloc_from_spec`），所以那条零拷贝路径可用 `CLONE_ON_FETCH=0` 跳过防御性克隆。

#### 4.4.3 源码精读

**构造期的二选一强制**（[feature_dataloader.py:L110-L113](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/feature_dataloader.py#L110-L113)）：从源头杜绝「既不是 refs 也不是 queue」或「两者都给」的歧义：

```python
if (queue is None) == (refs is None):
    raise ValueError("provide exactly one of `queue` (stream) or `refs` (re-iterable)")
```

**`_materialize` 的 get→clone→release**（[feature_dataloader.py:L191-L203](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/feature_dataloader.py#L191-L203)）：注意 `release` 放在 `finally`，物化失败也不会泄漏租约；之后才施加 transform：

```python
def _materialize(self, ref):
    tensors, handle = self.store.get(ref, device=self.device)
    try:
        if self.clone_on_fetch:
            tensors = {k: v.clone() for k, v in tensors.items()}
    finally:
        self.store.release(handle, reason="loaded")   # 立刻归还，绝不泄漏
    self._maybe_gc()
    if self.per_sample_transform is not None:
        tensors = self.per_sample_transform(tensors)
    return tensors
```

**`__iter__` 的模式分发**（[feature_dataloader.py:L249-L253](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/feature_dataloader.py#L249-L253)）：`_refs is not None` 决定走哪条迭代路径，二者产出同一个 `TrainBatch`：

```python
def __iter__(self):
    if self._refs is not None:
        yield from self._iter_refs()      # 可重迭代
    else:
        yield from self._iter_queue()     # consume-once
```

**refs 模式的可重迭代与续训**（[feature_dataloader.py:L255-L268](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/feature_dataloader.py#L255-L268)、[L297-L319](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/feature_dataloader.py#L297-L319)）：每次 `__iter__` 都重新从头切块，所以能多 epoch；`seek` 在**下一次**迭代跳过前 N 个 batch。关键：`seek` 在 queue 模式直接报错——流没有可恢复位置：

```python
def seek(self, num_batches):
    if self._refs is None:
        raise ValueError("seek() applies to refs mode only; a queue stream is consume-once")
    ...
```

**queue 模式的 consume-once 与 ack**（[feature_dataloader.py:L321-L347](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/feature_dataloader.py#L321-L347)）：从 queue 流式 get，尾部不足一个 batch 会终态清理（`_settle_incomplete_queue_batch`），每 yield 一个 batch 后 `queue.ack(refs)`（除非 `ack=False`，把 durable ack 让给 `DPAckController`）：

```python
while True:
    refs = self.queue.get(self.batch_size, timeout_s=0.0)
    if not refs:
        return
    if self.drop_last and len(refs) < self.batch_size:
        self._settle_incomplete_queue_batch(refs); return
    batch = self._make_batch(refs)
    yield batch
    if self.ack:
        self.queue.ack(refs)         # consume-once 的「已消费」标记
```

**真实接线点**：装配侧用一个 `ref_source` 字典把两种模式统一传给 loader（[training/trainer.py:L84](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/trainer.py#L84) 注释、[L145-L155](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/trainer.py#L145-L155) 用 `**ref_source` 解包）。离线传 `{"refs": [...], "refs_for_epoch": ...}`（[launch.py:L599](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch.py#L599)），在线传 `{"queue": queue, "defer_ack_until_durable": True}`（[launch.py:L1602-L1606](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch.py#L1602-L1606)）。loader 构造代码对这两种字典完全无差别地处理——这就是「trainer/loader 对模式与后端双重无感」的工程落点。

#### 4.4.4 代码实践（本讲主任务）

**实践目标**：回答本讲核心问题——(1) 为什么 trainer 与 loader 对传输后端无感；(2) 在线 queue 模式与离线 refs 模式在「是否可重迭代」上的根本差异。

**操作步骤**（源码阅读 + 推理型实践）：

1. **后端无感**：在 [training/trainer.py:L145-L155](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/trainer.py#L145-L155) 确认 `loader = FeatureDataLoader(store, ...)` 只把 `store` 当抽象 `FeatureStore` 用，全程只调 `store.get` / `store.release` / `store.gc`，没有任何 `isinstance(MooncakeFeatureStore)` 之类的分支。再对照 [training/disaggregated.py:L157-L177](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/disaggregated.py#L157-L177) 的 `_offline_store`，说明换 `DISAGG_BACKEND` 只换 `store` 的具体类，loader 与 trainer 代码一行都不用改。
2. **可重迭代的根本差异**：对照 [feature_dataloader.py:L255-L268](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/feature_dataloader.py#L255-L268)（`_iter_refs` 每次 `__iter__` 从头切块）与 [L321-L347](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/feature_dataloader.py#L321-L347)（`_iter_queue` 每条流只消费一次），并用 [L297-L306](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/feature_dataloader.py#L297-L306) 的 `seek` 报错信息佐证：queue 流没有可恢复位置。
3. **写出你的结论**（建议成一段话）：

**需要观察的现象 / 预期结果（参考答案）**：

- **后端无感**：trainer/loader 只依赖 `FeatureStore` 抽象契约的 `get/release/gc`，三种后端（`Local/SharedDir/Mooncake`）实现同一契约，后端选择发生在装配层（`_offline_store` 读 `DISAGG_BACKEND`、`_assemble_trainer` 收 `store` 参数），训练代码零分支。物化动作 `_materialize` 对 `file://`、`mem://`、`disagg://`、Mooncake key 完全一致——差异被 `store.get` 内部分发吸收。
- **可重迭代差异**：refs 模式的输入是固定 `List[SampleRef]`，每次 `__iter__` 重新切块、可多 epoch 反复读，且 `seek(num_batches)` 能跳到 checkpoint 保存的样本位置实现续训；queue 模式的输入是一条流，每条 ref 只被消费一次（`queue.ack` 标记已消费），`seek` 直接报错，在线 consumer 断点只能由控制面「跳过已 ack 前缀、重放未 ack 尾部」来重建，而不是重放整个流（这与 u7-l2 的 consumer-only 恢复契约一致）。物理基础是 4.1/4.3 讲的：`file://` 与 `retain_on_release=True` 的样本 `release` 是空操作/不释放，而在线 `retain_on_release=False` 的样本在最后一个租约归还时即被消费释放。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `clone_on_fetch` 默认是 `True`？什么情况下可以关掉？
**答案**：预取会让「物化」与「trainer 用 batch」错开，不克隆则 store 内部张量可能在 trainer 用完前被 release/abort 回收。Mooncake 的 `get()` 用 `_alloc_from_spec` 直接分配新张量，本身已是隔离副本，所以零拷贝路径可用 `CLONE_ON_FETCH=0` 跳过防御性克隆（[feature_dataloader.py:L122-L125](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/feature_dataloader.py#L122-L125)）。

**练习 2**：在线 queue 模式里，`ack=False`（`defer_ack_until_durable=True`）意味着什么？
**答案**：意味着 loader 不在物化后立刻 ack，而是把「已消费」的 durable 标记让给 `DPAckController`——在 optimizer 边界做一次 DP collective 后才记一笔 durable 账本事务（u7-l2 讲过）。这让「已 ack 样本」与「已提交梯度」严格对齐。

**练习 3**：`_validate_refs` 校验哪几项一致性？为什么必须校验？
**答案**：校验同一 batch 内所有 ref 的 `strategy`、`schema_version`、`target_repr`、feature spec 的名字与 shape/dtype 一致（[feature_dataloader.py:L151-L189](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/feature_dataloader.py#L151-L189)）。因为 collate 会把这些张量 stack 成一个 batch，规格不一致会导致形状错位或静默错误，必须在物化前拦住。

---

## 5. 综合实践

把本讲四个模块串起来，完成一张「**后端 × 数据模式 → ref_source 形态 → loader 行为**」对照表，并据此解释三种部署形态的数据通路：

| 部署形态 | store 后端 | retain_on_release | ref_source 形态 | loader 模式 | 可重迭代？ | 续训方式 |
| --- | --- | --- | --- | --- | --- | --- |
| colocated 离线 | `LocalFeatureStore`（file://） | —（file 空操作） | `{"refs": [...]}` | refs | ✅ | `seek` |
| 分离式离线 | `SharedDir`/`Mooncake` | `True` | `{"refs": [...]}`（读 manifest） | refs | ✅ | `seek` |
| 在线分离式 consumer | `Mooncake` | `False` | `{"queue": ..., "defer_ack_until_durable": True}` | queue | ❌ | 控制面重建尾部 |

**任务**：

1. 对照 [training/trainer.py:L145-L155](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/trainer.py#L145-L155)、[launch.py:L599](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch.py#L599) 与 [launch.py:L1602-L1606](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch.py#L1602-L1606)，验证表中三行的 `ref_source` 形态与代码一致。
2. 用一段话解释：为什么在线 consumer 用 `retain_on_release=False` + queue + `defer_ack_until_durable=True`，而离线用 `retain_on_release=True` + refs + `seek`？把答案落到三个机制上——(a) `release` 的 consume-once/空操作（4.1）；(b) `seek` 的模式守卫（4.4）；(c) durable ack 与 checkpoint 对齐（u7-l2）。
3. （可选，待本地验证）写一段最小示例：用 `LocalFeatureStore` 构造 3 个 `file://` ref，放进 `FeatureDataLoader(refs=...)` 跑两个 epoch，确认第二个 epoch 仍能取到全部 batch；再换成 `queue=SampleRefQueue()`，确认第二次 `iter(loader)` 取不到任何东西。

**预期结果**：你能用「数据面只对 ref_source 形态与 store 契约有反应、对具体后端与部署形态无感」这一句话，统一解释三种形态的差异，并能指出「可重迭代」的物理基础是「`release` 对 file/retain 样本是空操作或不释放」。

## 6. 本讲小结

- `FeatureStore` 是数据面唯一张量拥有者，五个生命周期方法 `put/get/release/abort/gc` + 租约/代数机制；它不携带任何调度状态。
- 三种后端 `LocalFeatureStore`/`SharedDirFeatureStore`/`MooncakeFeatureStore` 实现同一契约，使 trainer 与 loader 对传输后端完全无感；后端选择只发生在装配层。
- `retain_on_release` 开关区分在线（consume-once 释放）与离线（保留供多 epoch）；Mooncake 额外做硬钉、`_freed` 立即逻辑释放、`drain_pending_removals` 有界回收。
- 离线 ref 由 `OfflineManifestReader`（`file://`，不复制）或 `disagg_ingest`（put 进 store + 写元数据 manifest，`assert_no_tensors` 强制边界）产生。
- `FeatureDataLoader` 是 `SampleRef + FeatureStore → TrainBatch` 的唯一桥梁；refs 模式可重迭代可续训，queue 模式一次性消费、`seek` 直接报错。
- 默认 `clone_on_fetch=True` 把张量克隆出 store 并立刻释放 handle，保证预取与 release 不抢跑。

## 7. 下一步学习建议

本讲把「张量怎么存、怎么搬、怎么变 batch」讲完了，但**在线 queue 模式的 ref 是怎么从 producer 流到每个 consumer rank 的**还没展开。建议下一讲学习 **u7-l4 在线引用分发与流式队列**，重点读：

- [ref_distributor.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/ref_distributor.py)：consumer rank0 的去重与按 quantum 分发。
- [streaming_ref_channel.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/streaming_ref_channel.py)：producer→consumer 的文件系统元数据通道与背压。
- [control_plane/dp_ack.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/control_plane/dp_ack.py)：`DPAckController` 在 optimizer 边界的 durable ack（即本讲 `defer_ack_until_durable=True` 的另一端）。

之后再进入 **u7-l5 推理平面与 SGLang 捕获**，看 producer 那一端如何把特征直接写进 Mooncake、只把元数据返回——那正是本讲 `MooncakeFeatureStore.put` 的调用方。
