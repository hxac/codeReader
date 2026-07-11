# StorageManager 与异步序列化

## 1. 本讲目标

学完本讲后，你应该能够：

- 说明 `StorageManager` 在 `LMCacheEngine` 与多个存储后端之间扮演的「调度层」角色。
- 描述两条核心数据流：**写穿（write-through / fan-out put）** 和 **读提升（read promote）**。
- 解释异步序列化器（`AsyncSingleSerializer` / `AsyncMultiSerializer`）为什么存在，以及它们如何防止并发取回时的死锁。
- 用数学语言说明 `WeightedSemaphore` 的「半数预算」为什么能保证不死锁。

本讲承接 [u2-l3](./u2-l3-storage-backend-hierarchy.md)：上一篇讲的是「单个后端是什么」，本讲讲的是「如何把多个后端编排起来」。

## 2. 前置知识

阅读本讲前，建议先理解以下概念：

- **asyncio 事件循环**：Python 的异步运行时。`asyncio.Lock` / `asyncio.Condition` / `asyncio.Future` 是它的基本同步原语。本讲会反复出现。
- **信号量（Semaphore）**：一种计数器，用来限制「同时能进入临界区的任务数」。普通信号量每次加减 1，**加权信号量**每次加减的值由请求大小决定。
- **`MemoryObj` 与 `ref_count`**：LMCache 里每块 KV 缓存内存都被包成一个 `MemoryObj`，带引用计数。`ref_count_up()` 增加引用、`ref_count_down()` 减少；归零后才能真正释放。这是多后端共享同一块内存的基础（详见 u2-l3）。
- **`OrderedDict` 与前缀命中**：后端按固定顺序排列；检索采用「前缀匹配」——只有从第 0 个 chunk 开始连续命中的才算数。
- **回顾 u2-l3 的三层后端**：`LocalCPUBackend`（最快、兼当全局分配器）、`LocalDiskBackend`（落盘）、`RemoteBackend`（跨实例）。以及 `AllocatorBackendInterface`：能主动 `allocate()` 内存的后端。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `lmcache/v1/storage_backend/storage_manager.py` | **主角**。`StorageManager` 调度层、`WeightedSemaphore`、`AsyncSingleSerializer` / `AsyncMultiSerializer` 全在这里。 |
| `lmcache/v1/storage_backend/abstract_backend.py` | 后端契约 `StorageBackendInterface` / `AllocatorBackendInterface`，定义 `calculate_chunk_budget()` 抽象方法（u2-l3 已讲契约，本讲只取其中的预算接口）。 |
| `lmcache/v1/storage_backend/local_cpu_backend.py` | `calculate_chunk_budget()` 与 `get_full_chunk_size_bytes()` 的真实实现，决定并发预算上限。 |
| `lmcache/v1/storage_backend/__init__.py` | `CreateStorageBackends()` 工厂，按固定顺序组装后端 `OrderedDict`；`is_cuda_worker()` 判定角色。 |
| `lmcache/v1/cache_engine.py` | `LMCacheEngine` 调用 manager 的入口（`batched_put` / `batched_get` / `async_lookup_and_prefetch`）。 |

## 4. 核心概念与源码讲解

### 4.1 StorageManager：位于 engine 与后端之间的调度层

#### 4.1.1 概念说明

在 u1-l6 里我们看到，`LMCacheEngine` 把存储这件事委托给一个叫 `storage_manager` 的下游黑盒。本讲就打开这个黑盒。

`StorageManager` 的职责可以用三句话概括：

1. **持有一组后端**：用一个 `OrderedDict[str, StorageBackendInterface]` 保存所有后端（CPU / 磁盘 / 远端 / P2P / PD …），顺序由 `CreateStorageBackends` 工厂决定。
2. **选定一个分配器后端（allocator_backend）**：多个后端里，只有「能主动 `allocate()` 内存的那个」负责真正分配 `MemoryObj`；其余后端只负责「把数据搬过去存起来」。
3. **自己跑一个事件循环**：在独立线程里跑 `asyncio` 循环，用于驱动后端的异步操作（异步 contains、异步 get、预取回调）。

为什么要这样设计？因为 engine 不应该知道「有几层后端、每层叫什么、谁负责分配」。engine 只管说「把这批 KV 存下来」「把那批 KV 取回来」，至于**写穿到每一层**还是**从某一层提升热度**，全是 manager 的调度决策。这样后端的增删（见 `create_backends` / `close_backend` / `recreate_backend`）对 engine 透明。

#### 4.1.2 核心流程

**初始化流程**：

```
__init__
  ├─ 新建 asyncio 事件循环，在独立线程 "storage-manager-event-loop" 启动
  ├─ create_backends()           → 调 CreateStorageBackends 填充 OrderedDict
  ├─ get_non_allocator_backends()→ 算出「真正存数据」的后端名单
  ├─ _get_allocator_backend()    → 选出唯一分配器（通常 LocalCPUBackend）
  ├─ (CUDA worker) 建 internal_copy_stream → put 时跨设备拷贝用
  └─ (非 PD 且 enable_async_loading) 建 async_serializer → 异步取回的并发闸门
```

**写穿 put 流程**（`batched_put`，非阻塞）：

```
对 allocator_backend：直接把 engine 给的 memory_objs 登记进去
对每个其余后端 backend：
  ├─ 若该后端用的分配器还没拷过 → allocate_and_copy_objects() 在它自己的分配器里分配+拷贝
  └─ backend.batched_submit_put_task(...)   各自异步入库
最后：对所有拷出来的副本统一 ref_count_down()
```

注意：这是 **fan-out 同写**——同一份数据被写到所有配置开启的后端，而不是只写一层。

**读提升 get 流程**（`batched_get` / `get`，阻塞）：

```
按 OrderedDict 顺序线性遍历 active backends：
  mem = backend.batched_get_blocking(keys)
  若命中：
    ├─ 若命中层不是 CPU/PD/Maru（即来自磁盘或远端）→ 把数据写回 LocalCPUBackend（提升热度）
    └─ 返回
  否则继续下一层
全部未命中 → 返回 [None]*len
```

这条「慢层命中后顺手回填快层」的逻辑，就是 **read promote**：热门数据会自然从磁盘/远端「浮」到 CPU 热缓存。

**前缀 contains 流程**（`batched_contains`）：同样线性遍历，每个后端返回从开头起连续命中的 chunk 数，累加并把每段映射记进 `block_mapping`，凑满总 key 数即停。

#### 4.1.3 源码精读

构造函数建立事件循环、线程、后端表，并条件性地建出异步序列化器：

[storage_manager.py:224-291](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/storage_manager.py#L224-L291) —— 构造 `StorageManager`：新建并启动独占事件循环（`storage-manager-event-loop` 线程），调用 `create_backends()` 组装后端，选定 `allocator_backend`，在 `enable_async_loading` 且非 PD 时创建 `AsyncSingleSerializer`。注意 `scheduler` 角色不分配内存，故 `allocator_backend` 可能为 `None`。

分配器选择逻辑很关键——它决定了「谁负责 `allocate`」：

[storage_manager.py:312-325](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/storage_manager.py#L312-L325) —— `_get_allocator_backend`：PD 模式用 `PDBackend`；有 `MaruBackend` 时优先用 `LocalCPUBackend`（否则 Maru 自己）；默认就是 `LocalCPUBackend`。

写穿的核心是下面这个模块级辅助函数 + `batched_put`：

[storage_manager.py:64-118](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/storage_manager.py#L64-L118) —— `allocate_and_copy_objects`：在某个后端自己的分配器里，按源对象的 shape/dtype/fmt 分配新 `MemoryObj`，并在指定 GPU `stream` 上做 `copy_(non_blocking=True)`，最后 `stream.synchronize()`。若分配失败（`memory_obj is None`）立即 `break`，实现「能存多少存多少」。

[storage_manager.py:384-433](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/storage_manager.py#L384-L433) —— `batched_put`：先把 engine 传入的对象登记进分配器后端，再遍历其余后端做 `allocate_and_copy_objects` + `batched_submit_put_task`，最后统一 `ref_count_down()` 释放 engine 侧引用（让引用计数管理交还给各后端）。

读提升逻辑在 `batched_get` 里：

[storage_manager.py:480-513](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/storage_manager.py#L480-L513) —— `batched_get`：线性遍历 active 后端，首个命中即返回；若命中层属于磁盘/远端（不在 `{LocalCPUBackend, PDBackend, MaruBackend}` 名单），则把对象 `batched_submit_put_task` 写回 `LocalCPUBackend`，完成热度提升。

所有遍历都经过 `get_active_storage_backends` 这一道过滤：

[storage_manager.py:1165-1193](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/storage_manager.py#L1165-L1193) —— `get_active_storage_backends`：一个生成器，按 `freeze` 模式（只留 LocalCPU）、`bypass` 模式（健康检查失败时跳过某层）、`location` / `search_range` 过滤后逐个 `yield`。它是上面 put/get/contains 共用的「可见后端」入口。

engine 侧的调用入口（store 主链路）：

[cache_engine.py:564-569](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/cache_engine.py#L564-L569) —— engine 在 `gpu_connector.batched_from_gpu` 把 KV 从 GPU 搬进 `MemoryObj` 后，调用 `storage_manager.batched_put(...)`，把后续多后端编排完全交给 manager。

#### 4.1.4 代码实践

**实践目标**：把 `engine.store → manager.batched_put → 各后端` 这条写穿链路画清楚。

**操作步骤（源码阅读型）**：

1. 打开 `lmcache/v1/cache_engine.py`，定位 `store`/`_store_bytes` 中调用 `self.storage_manager.batched_put(...)` 的位置（约 L564）。
2. 跳到 `storage_manager.py` 的 `batched_put`（L384），跟踪它如何遍历 `self.storage_backends`。
3. 对每个非分配器后端，进入 `allocate_and_copy_objects`（L64），看清「分配 + 在 `internal_copy_stream` 上拷贝 + 同步」三步。
4. 最后看 L431-433 的 `ref_count_down()`，理解引用计数如何把内存所有权移交给后端。

**需要观察的现象**：

- 同一批 `memory_objs` 会被 `allocate_and_copy_objects` 复制成多份（每个非分配器后端一份），每份用各自的分配器分配。
- 若某后端分配失败（返回较少对象），`batched_put` 不会抛错，而是「能存多少存多少」——这与 u1-l6 讲的 store 语义一致。

**预期结果**：你能画出如下时序——

```
engine.store
  └─ gpu_connector.batched_from_gpu       (GPU KV → MemoryObj)
  └─ storage_manager.batched_put(keys, objs)
       ├─ allocator_backend.batched_submit_put_task   (直接登记，不拷贝)
       └─ for backend in 其余后端:
            ├─ allocate_and_copy_objects(backend.allocator, ...)  ← 在 stream 上拷贝
            └─ backend.batched_submit_put_task(...)               ← 异步入库
       └─ for obj in 所有副本: obj.ref_count_down()
```

> 待本地验证：若你在 `batched_put` 入口加一行 `logger.info("put to backends: %s", list(self.storage_backends))`，实际日志里能看到当前实例启用了哪几层后端。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `scheduler` 角色的 `allocator_backend` 是 `None`？`batched_put` 里如何处理？

**答案**：`scheduler` 只做 lookup 查询、不存 KV（详见 u2-l3 的角色说明），所以构造时不分配内存。`batched_put` 在 L402-404 检测到 `allocator_backend is None` 时直接 `raise RuntimeError("Batched put not available for scheduler role")`，早失败而非静默丢数据。

**练习 2**：`batched_get` 命中远端后端后，为什么要写回 `LocalCPUBackend`？

**答案**：为了让「慢层命中」的数据「浮」到最快的 CPU 热缓存，下次直接命中 CPU 层，避免重复跨网络/磁盘读取。这就是 read promote，由 manager（而非单个后端）统一决策。

---

### 4.2 异步序列化：AsyncSingleSerializer 与 AsyncMultiSerializer

#### 4.2.1 概念说明

开启 `enable_async_loading` 后，取回路径会变成异步的：engine 把一次 lookup 请求丢进 manager 的事件循环（`async_lookup_and_prefetch`），多个请求可以并发执行。每个请求内部都会调用 `backend.batched_get_non_blocking`，而它最终要调用 `LocalCPUBackend.allocate(...)` 从 CPU 池里分配 `MemoryObj`。

问题来了：**多个并发请求同时从同一个有限大的 CPU 池分配内存，可能死锁。**

典型死锁场景：池子快满了，请求 A 想分配必须先淘汰别人；但被淘汰的对象可能正被请求 B 持有（`ref_count > 1` 或被 pin），而 B 又在等 A 释放……于是大家互相等待。

`AsyncMultiSerializer` 的类文档说得很直白：

> Prevent race conditions where multiple batched_get's cause the local CPU backend to allocate memory objects in parallel and get deadlocked.

解决办法是**准入控制（admission control）**：在请求真正去分配内存之前，先用一个序列化器「挂号」，控制同时能进入分配阶段的请求总规模。项目提供了两种策略：

- **`AsyncSingleSerializer`（朴素串行）**：一把全局 `asyncio.Lock`，一次只放一个请求进去。绝对安全，但并发度为 1。
- **`AsyncMultiSerializer`（加权并发）**：用 `WeightedSemaphore` 按「chunk 数」加权放行，允许多个请求并发，只要总 chunk 数不超过预算的一半。

#### 4.2.2 核心流程

两种序列化器都暴露同一个 `run(coro, ...)` 接口，把「申请准入 → 执行协程 → 释放准入」包成 try/finally：

```python
# AsyncMultiSerializer.run（加权）
async def run(self, coro_fn, num_chunks):
    await self._sem.acquire(num_chunks)   # 按 num_chunks 加权申请
    try:
        return await coro_fn              # 真正执行后端的 batched_get_non_blocking
    finally:
        await self._sem.release(num_chunks)

# AsyncSingleSerializer.run（串行）
async def run(self, coro_fn, *args, **kwargs):
    if self.lock is None:
        self.lock = asyncio.Lock()        # 懒初始化，确保绑在当前 loop 上
    async with self.lock:
        return await coro_fn              # 一次只跑一个
```

**接入点**：在 `StorageManager.__init__` 里，当 `enable_async_loading` 为真且非 PD 时，创建的是 `AsyncSingleSerializer`：

```python
if not self.enable_pd and self.config.enable_async_loading:
    assert self.allocator_backend is not None
    self.async_serializer = AsyncSingleSerializer(self.loop)
```

**调用点**：异步取回主流程 `async_lookup_and_prefetch` 在为每个后端发起 `batched_get_non_blocking` 前，会先过一道 `async_serializer.run(...)`：

```python
get_coro = self.async_serializer.run(
    backend.batched_get_non_blocking(lookup_id, backend_keys, {...}),
    num_hit_chunks,        # 该后端这次要取的 chunk 数
)
loading_task = asyncio.create_task(get_coro)
```

关键细节：`num_hit_chunks` 这个参数，**只有 `AsyncMultiSerializer` 会用到**（拿去给信号量做加权）；`AsyncSingleSerializer.run` 用 `*args` 把它吞掉、忽略。也就是说，整条调用链已经为「多序列化器」铺好了路，但当前默认接的是「单序列化器」。这是真实代码的现状，不是推测。

#### 4.2.3 源码精读

[storage_manager.py:195-212](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/storage_manager.py#L195-L212) —— `AsyncSingleSerializer`：懒初始化 `asyncio.Lock`（注释解释了为何懒初始化——要把锁绑到调用方的事件循环上），`async with` 包住协程，强制串行。

[storage_manager.py:166-192](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/storage_manager.py#L166-L192) —— `AsyncMultiSerializer`：构造时从 `allocator_backend.calculate_chunk_budget()` 拿到总预算，建一个 `WeightedSemaphore`；`run` 用 try/finally 保证 `acquire`/`release` 配对。

[storage_manager.py:286-288](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/storage_manager.py#L286-L288) —— 当前默认接入点：`enable_async_loading` 且非 PD 时创建 `AsyncSingleSerializer`。

[storage_manager.py:758-771](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/storage_manager.py#L758-L771) —— 异步取回中调用 `self.async_serializer.run(get_coro, num_hit_chunks)`，把准入控制包在每个后端的取回协程外面。注释明确写出「num_hit_chunks is only used for the multi serializer」。

[storage_manager.py:215](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/storage_manager.py#L215) —— `AsyncSerializer = Union[AsyncSingleSerializer, AsyncMultiSerializer]`：用类型别名统一两种策略，方便 `StorageManager.async_serializer` 字段互换。

#### 4.2.4 代码实践

**实践目标**：理解两种序列化器在并发行为上的差异。

**操作步骤（源码阅读型 + 思考实验）**：

1. 阅读 `AsyncSingleSerializer.run`（L206-212），确认它完全不读 `num_chunks`，靠一把全局锁串行化。
2. 阅读 `AsyncMultiSerializer.run`（L183-192），确认它把 `num_chunks` 透传给 `WeightedSemaphore.acquire`。
3. 做一个思考实验：假设有 3 个并发请求，分别要取 10 / 10 / 10 个 chunk，CPU 池预算 80（concurrent_cap=40）。
   - 在 `AsyncSingleSerializer` 下：3 个请求**串行**执行，吞吐低但绝不争抢。
   - 在 `AsyncMultiSerializer` 下：3 个请求各 acquire(10)，总和 30 ≤ 40，**可并发**执行；若有第 5 个请求要 50（>40），它会等到独占。

**需要观察的现象**：

- 单序列化器下，异步取回的并发度恒为 1，无论配置多大内存。
- 多序列化器下，并发度随预算和请求大小动态变化。

**预期结果**：你能说清「为什么默认是单序列化器」——它最简单、最安全；而多序列化器是「在保证不死锁前提下榨取并发」的进阶选项，需要 `WeightedSemaphore` 配合。

> 待本地验证：当前代码库没有在 `__init__` 中创建 `AsyncMultiSerializer` 的分支。若你想启用它，需要自行修改接入点（这属于二次开发，不在本讲范围）。

#### 4.2.5 小练习与答案

**练习 1**：`AsyncSingleSerializer` 为什么把 `self.lock` 的创建延迟到第一次 `run` 时，而不是在 `__init__` 里直接建？

**答案**：因为 `asyncio.Lock` 必须绑定到「将要运行它的事件循环」。`AsyncSingleSerializer` 构造时拿到的 `loop` 引用，与实际调用 `run` 的协程所在循环不一定一致（manager 的事件循环在独立线程）。懒初始化能保证锁绑在「真正执行 `run` 的那个循环」上。源码注释原文：「we need to lazily initialize the lock to place it on the calling event loop」。

**练习 2**：`async_lookup_and_prefetch` 给 `async_serializer.run` 传的第二个参数是 `num_hit_chunks`，这个值是怎么来的？

**答案**：它是该后端在 `batched_async_contains` 阶段返回的「前缀连续命中 chunk 数」（见 L731-738，还会按 `keys_per_chunk` 向下取整到整 chunk 边界）。对 `AsyncMultiSerializer` 而言，这正是「这次取回将要分配多少个 `MemoryObj`」的精确度量，用来做加权准入。

---

### 4.3 WeightedSemaphore：半数预算防死锁

#### 4.3.1 概念说明

`WeightedSemaphore` 是 `AsyncMultiSerializer` 的核心。它是一个**按 chunk 数加权**的异步信号量，但有一个反直觉的设计：**它只管理总预算的一半。**

先看预算怎么来。`AsyncMultiSerializer.__init__` 调用 `allocator_backend.calculate_chunk_budget()`，这个方法在 `LocalCPUBackend` 里有真实实现，含义是「这块 CPU 池最多能装下多少个满 chunk」。

`WeightedSemaphore` 拿到这个总数 `chunk_budget` 后，却把并发上限设成一半：

```python
self._concurrent_budget_cap = chunk_budget // 2
self._chunk_budget_cap = chunk_budget
self._current_chunks = self._concurrent_budget_cap   # 从「半数」开始计数
```

类注释给出理由：当所有 chunk 等大时（即 `save_unfull_chunk=False`），内存碎片率**物理上不可能超过 50%**，因此预留一半预算给并发请求是安全的。

#### 4.3.2 核心流程与数学说明

预算的计算公式（来自 `calculate_chunk_budget` 与 `get_full_chunk_size_bytes`）：

\[
\text{chunk\_bytes} = \text{kv\_size} \times \text{num\_layers} \times \text{chunk\_tokens} \times \text{hidden\_dim} \times \text{dtype\_size}
\]

\[
\text{aligned\_chunk\_bytes} = \left\lceil \frac{\text{chunk\_bytes}}{\text{align}} \right\rceil \times \text{align}
\]

\[
\text{chunk\_budget} = \left\lfloor \frac{\text{max\_local\_cpu\_size} \times 1024^3}{\text{aligned\_chunk\_bytes}} \right\rfloor
\]

其中 `align` 是 `MixedMemoryAllocator` 的对齐字节数（通常 4096）。

并发上限：

\[
\text{concurrent\_budget\_cap} = \left\lfloor \frac{\text{chunk\_budget}}{2} \right\rfloor
\]

`acquire(n)` 分两种情况：

- **普通请求**（\(n \le \text{cap}\)）：等待直到 `_current_chunks >= n`，然后 `_current_chunks -= n`。
- **超大请求**（\(n > \text{cap}\)，但 \(n \le \text{chunk\_budget}\)）：要求**独占**——等待直到 `_current_chunks == cap`（即没有别人在用），然后把 `_current_chunks` 置 0（预留全部）。

**为什么不会死锁？** 两个不变量保证：

1. 所有普通请求的并发占用总和 \( \le \text{cap} = \text{chunk\_budget}/2 \)，所以池子里永远至少剩一半空闲，碎片（≤50%）填不满这剩下的另一半，淘汰总能进行。
2. 超大请求拿到独占后，整个池子归它用，不会和别人互相等待。

另外，`acquire` 还有一个硬上限保护：若 \(n > \text{chunk\_budget\_cap}\)（单个请求就要的比整个池子还大），直接 `raise ValueError`，提示「把 max_local_cpu_size 调大」。

#### 4.3.3 源码精读

[storage_manager.py:121-163](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/storage_manager.py#L121-L163) —— `WeightedSemaphore` 全貌：`acquire` 区分普通/超大两分支，靠 `asyncio.Condition.wait_for` 等待条件成立；`release` 对称恢复并 `notify_all` 唤醒等待者。

[local_cpu_backend.py:906-925](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/local_cpu_backend.py#L906-L925) —— `calculate_chunk_budget`：用 `max_local_cpu_size`（GB）算出总字节数，除以「对齐后的单 chunk 字节数」得到预算上限。

[local_cpu_backend.py:866-904](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/local_cpu_backend.py#L866-L904) —— `get_full_chunk_size_bytes`：从 `metadata.kv_shape`（`[num_layers, kv_size, chunk_size, num_heads, head_size]`）算出单个满 chunk 的字节数，区分 layerwise / 非 layerwise 两种布局。

[abstract_backend.py:417-421](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/abstract_backend.py#L417-L421) —— `AllocatorBackendInterface.calculate_chunk_budget` 抽象方法声明，使得 `AsyncMultiSerializer` 可以面向接口、不绑定具体后端。

#### 4.3.4 代码实践

**实践目标**：用一组具体参数手算一次 chunk 预算，建立数量直觉。

**操作步骤（纸笔计算，示例代码）**：

假设某模型配置如下（**示例参数**，非项目内置默认值）：

| 参数 | 值 |
| --- | --- |
| `max_local_cpu_size` | 5 GB |
| `chunk_size`（token 数） | 256 |
| `num_layers` | 32 |
| `kv_size` | 2（普通模型，MLA 为 1） |
| `num_heads` | 8 |
| `head_size` | 128 |
| dtype | fp16（`itemsize = 2`） |
| `align` | 4096 |

计算（非 layerwise 布局）：

```
hidden_dim      = 8 * 128 = 1024
chunk_bytes     = 2 * 32 * 256 * 1024 * 2 = 33_554_432  (32 MiB)
aligned_bytes   = ceil(33_554_432 / 4096) * 4096 = 33_554_432  (已对齐)
total_memory    = 5 * 1024^3 = 5_368_709_120
chunk_budget    = 5_368_709_120 // 33_554_432 = 160
concurrent_cap  = 160 // 2 = 80
```

**需要观察的现象**：

- 一个 5 GB 的 CPU 池，在上述模型下能装 160 个满 chunk；`WeightedSemaphore` 允许最多 80 个 chunk 同时处于「并发取回中」。
- 单个请求若想一次取超过 80 个 chunk，会触发「超大请求独占」分支；若想取超过 160 个，直接 `ValueError`。

**预期结果**：你能在脑中建立「内存大小 ↔ chunk 预算 ↔ 并发上限」的换算关系。

> 待本地验证：上述数字随模型 shape 而变。实际部署时可在 `AsyncMultiSerializer.__init__` 处打印 `self.chunk_budget` 与 `self._sem._concurrent_budget_cap` 对照。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `_current_chunks` 的初值是 `_concurrent_budget_cap`（半数），而不是 `_chunk_budget_cap`（满额）？

**答案**：因为信号量只负责「并发准入」这一半预算，另一半被刻意预留出来，作为淘汰/碎片的安全垫。把初值设为半数，意味着「这半数可以被并发请求消耗」；当它降到 0，说明并发请求已经占满了它们应得的份额，新请求必须等。这与「满额计数、用完为止」的普通信号量语义不同，是防死锁的关键。

**练习 2**：如果一个请求要 acquire 的 chunk 数正好等于 `_concurrent_budget_cap + 1`，会发生什么？它和「要 acquire `_chunk_budget_cap` 个」有何区别？

**答案**：两者都落入「超大请求」分支（`n > cap`），都要求独占——等到 `_current_chunks == cap` 后把计数置 0。区别在 `acquire` 开头的硬上限校验：只要 `n <= _chunk_budget_cap` 就合法；若 `n > _chunk_budget_cap`（比整个池还大）则 `raise ValueError`。所以 `_concurrent_budget_cap + 1` 是合法的「独占请求」，`_chunk_budget_cap + 1` 是非法请求。

**练习 3**：`save_unfull_chunk=False` 这个前提若不成立（即 chunk 大小不一时），「半数预算」还安全吗？

**答案**：不再严格安全。注释明确依赖「all of the chunks are of the same size」这一前提，此时碎片率上限才是 50%。若 chunk 大小不一，碎片率可能更高，预留一半就未必够，存在死锁风险。这正是该机制假设 `save_unfull_chunk=False` 的原因。

## 5. 综合实践

把本讲三个最小模块串起来，完成一次「**异步取回全链路追踪**」。

**任务**：以 `enable_async_loading=True` 为假设场景，画出从 engine 发起 lookup 到 KV 被取回的完整时序，并标注三个模块各自在哪一步发挥作用。

**步骤**：

1. 从 [cache_engine.py:1368-1378](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/cache_engine.py#L1368-L1378) 入手：engine 用 `asyncio.run_coroutine_threadsafe(...)` 把 `async_lookup_and_prefetch` 提交到 **manager 的事件循环**（注意：不是 engine 自己的循环，而是 `storage_manager.loop`）。
2. 进入 [storage_manager.py:653-828](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/storage_manager.py#L653-L828) 的 `async_lookup_and_prefetch`：
   - 先线性遍历 `get_active_storage_backends()`（**4.1 的可见后端过滤**）。
   - 对每个后端 `batched_async_contains` 拿到前缀命中数 `num_hit_chunks`。
   - 用 `async_serializer.run(...)` 包住 `batched_get_non_blocking`（**4.2 的准入控制**）。
3. 追踪 `async_serializer.run` 内部：若是 `AsyncMultiSerializer`，会调用 `WeightedSemaphore.acquire(num_hit_chunks)`（**4.3 的加权信号量**），等待拿到预算后才真正执行后端的取回协程。
4. 所有后端的取回任务 gather 完成后，由 [storage_manager.py:555-651](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/storage_manager.py#L555-L651) 的 `prefetch_all_done_callback` 汇总「实际取回了多少 token」并回报给 scheduler。

**产出**：一张标注了下列要素的时序图——

```
engine.retrieve(异步)
  │  run_coroutine_threadsafe → manager.loop
  ▼
StorageManager.async_lookup_and_prefetch        ← 4.1 调度层
  │  for backend in get_active_storage_backends:
  │      num_hit = backend.batched_async_contains(...)
  │      coro = async_serializer.run(backend.batched_get_non_blocking(...), num_hit)
  ▼
AsyncMultiSerializer.run / AsyncSingleSerializer.run   ← 4.2 准入控制
  │  (Multi) await WeightedSemaphore.acquire(num_hit)   ← 4.3 加权信号量
  │  await backend.batched_get_non_blocking(...)        ← 真正分配 MemoryObj
  │  (Multi) await WeightedSemaphore.release(num_hit)
  ▼
gather 所有后端结果 → prefetch_all_done_callback → 回报 scheduler
```

**验收标准**：你能指着图上的每一处，说清「为什么需要它」——调度层负责多后端编排、序列化器负责防并发死锁、信号量负责按预算限流。

## 6. 本讲小结

- `StorageManager` 是 engine 与后端之间的**调度层**：持有一组后端 `OrderedDict`、选定唯一 `allocator_backend`、自带独立事件循环线程。
- **写穿（fan-out put）**：`batched_put` 把同一批 KV 通过 `allocate_and_copy_objects` 复制到每个非分配器后端，最后统一 `ref_count_down`。
- **读提升（read promote）**：`batched_get` 线性遍历后端，慢层命中后顺手回填 `LocalCPUBackend`，让热数据浮到最快层。
- **异步序列化器**解决「并发取回争抢 CPU 池导致死锁」：`AsyncSingleSerializer` 全局串行（当前默认），`AsyncMultiSerializer` 加权并发（已铺好调用链）。
- **`WeightedSemaphore`** 的精髓是「**只管半数预算**」：在 `save_unfull_chunk=False`（chunk 等大、碎片≤50%）前提下，预留一半做安全垫，保证不死锁；超大请求走独占分支。
- `get_active_storage_backends` 是 put/get/contains 共用的「可见后端」过滤口，统一处理 `freeze`、`bypass`、`location`/`search_range`。

## 7. 下一步学习建议

- **向下深入后端内部**：本讲的 `batched_submit_put_task` / `batched_get_non_blocking` 在磁盘层如何异步落盘？建议阅读 `local_disk_backend.py` 的优先级写与线程池读，对应 u2-l3 提到的「字节落盘 + CPU 中转」。
- **向右进入集成层**：manager 的 `batched_put` / `batched_get` 是如何被 vLLM connector 周期性调用的？见 [u2-l5 vLLM 集成适配器](./u2-l5-vllm-integration.md)。
- **向上理解调用者**：engine 的 `store` / `retrieve` / `lookup` 三条主链路如何映射到 manager 的 put/get/contains，见 [u1-l6 LMCacheEngine 公共 API](./u1-l6-engine-public-api.md)。
- **专家方向**：如果你关注 PD 分离，`_get_allocator_backend` 里 PD 模式选 `PDBackend` 的分支，会在 [u4-l7 PD 分离与传输通道](./u4-l7-pd-disaggregation-transfer.md) 展开。
