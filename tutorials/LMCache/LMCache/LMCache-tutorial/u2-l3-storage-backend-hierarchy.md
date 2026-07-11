# 存储后端层次结构

## 1. 本讲目标

本讲是「进阶层：核心模块与主调用链」的第三篇，承接 u1-l6 中 `LMCacheEngine` 的 `store/retrieve/lookup` 三大 API。在那一讲里，我们提到引擎把「KV cache 存到哪里、从哪里取」这件事委托给了一个叫 `storage_manager` 的下游黑盒。本讲就打开这个黑盒的第一层——**存储后端（storage backend）**。

学完本讲，你应当能够：

1. 读懂 `StorageBackendInterface` 抽象类定义的「后端契约」，说出每个后端必须实现哪些方法、哪些方法有默认实现。
2. 说明 `LocalCPUBackend`、`LocalDiskBackend`、`RemoteBackend` 三层各自的职责、它们之间的依赖关系（谁给谁当缓冲区），以及数据如何在它们之间流动。
3. 理解 LMCache 的分层存储是「写穿（write-through）+ 读提升（read promotion）」模型，而不是简单的「满了才往下一层赶」的驱逐瀑布。
4. 知道 `p2p`、`pd`、`gds`、`nixl` 等特殊后端的存在与定位，为后续 u4 单元的分布式/PD 讲义埋下伏笔。

## 2. 前置知识

在进入源码前，先用通俗语言对齐几个概念（这些在 u1-l1、u1-l6 已建立，这里做最小回顾）：

- **MemoryObj**：LMCache 内部对「一块 KV cache 内存」的统一封装，记录了张量、形状、dtype、内存格式（`MemoryFormat`，如 `KV_2LTD`）、引用计数（`ref_count`）和是否被 pin。后端之间传递的就是 `MemoryObj`。
- **CacheEngineKey**：一个 KV chunk 的全局唯一键，由模型/worker/_chunk_hash 等字段组成。后端里所有的「存不存在」「取」「删」都以它为参数。
- **chunk**：LMCache 按 `chunk_size` 把一段 token 切成若干块，每块对应一个 `MemoryObj` 和一个 `CacheEngineKey`。
- **分层存储（tiered storage）**：把数据按「快而小 → 慢而大」分成多层。LMCache 的典型层级是：CPU 内存（快）→ 本地磁盘（中）→ 远端存储（慢但可跨实例共享）。
- **写穿 / 读提升**：经典的分级缓存策略。**写穿**指写入时同时写到所有层（保证不丢）；**读提升**指读取时从最快层开始找，命中即返回，若命中在慢层则顺便回填到快层，下次就快了。
- **热缓存（hot_cache）**：`LocalCPUBackend` 里真正用来「缓存命中」的字典，区别于它另一个身份「内存分配器」。

> 一个贯穿全讲的直觉：**LocalCPUBackend 有双重身份**——它既是「最快的缓存层」，又是「给所有后端分配 CPU 内存的分配器」。理解这一点，三层之间的关系就豁然开朗。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [lmcache/v1/storage_backend/abstract_backend.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/abstract_backend.py) | 定义所有后端的统一契约 `StorageBackendInterface`，以及两个扩展接口 `AllocatorBackendInterface`（能分配内存）和 `StoragePluginInterface`（可插拔）。 |
| [lmcache/v1/storage_backend/local_cpu_backend.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/local_cpu_backend.py) | `LocalCPUBackend`：最快的缓存层 + 全局内存分配器，承担热缓存命中与 LRU 淘汰。 |
| [lmcache/v1/storage_backend/local_disk_backend.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/local_disk_backend.py) | `LocalDiskBackend`：把 KV 以字节落盘，异步写入 / 并发读取，磁盘空间不够时按缓存策略淘汰。 |
| [lmcache/v1/storage_backend/remote_backend.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/remote_backend.py) | `RemoteBackend`：通过 connector 连到远端 KV 服务（如 `lmcache_server`），序列化后异步收发，支持跨实例共享。 |
| [lmcache/v1/storage_backend/\_\_init\_\_.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/__init__.py) | `CreateStorageBackends` 工厂：按配置和固定顺序把上述后端组装成 `OrderedDict`，决定分层顺序。 |
| [lmcache/v1/storage_backend/storage_manager.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/storage_manager.py) | `StorageManager`：编排所有后端，实现「写穿 / 读提升」调度。（本讲只看它与后端分层相关的方法，完整调度留到 u2-l4。） |

> 说明：截至当前 HEAD，`docs/design/` 下**没有** `v1/storage_backend/` 子目录的专门设计文档，因此本讲直接以源码为唯一事实来源。`distributed/`（L1/L2 新架构）有完整设计文档，属于 u4-l2 的范围。

## 4. 核心概念与源码讲解

### 4.1 统一契约：StorageBackendInterface

#### 4.1.1 概念说明

要支持「CPU 内存、磁盘、远端服务」这些存储介质截然不同的后端，第一步是**抽出一份公共契约**。`abstract_backend.py` 里的 `StorageBackendInterface` 就是这份契约：它规定「任何一个想被 StorageManager 调度的后端，都必须会哪些动作」。

后端的世界观非常朴素——它被当成一个**按 key 存取 `MemoryObj` 的字典**，外加几条生命周期管理方法。无论底层是内存 dict、文件系统还是 TCP socket，对上层都呈现同一套方法名。

这份契约还体现了两个层次的扩展能力：

- `AllocatorBackendInterface`（能主动分配 `MemoryObj` 的后端）：目前只有 `LocalCPUBackend` 真正实现它。
- `StoragePluginInterface`（可通过配置「即插即用」加载的后端）：留给外部插件，详见 4.5。

#### 4.1.2 核心流程

一个后端对外要回答这几类问题：

```text
存在性   contains(key, pin)         —— key 在不在？要不要顺便 pin 住？
写入     batched_submit_put_task    —— 异步把一批 MemoryObj 存进来
读取     get_blocking               —— 同步取一个；命中返回 MemoryObj，未命中返回 None
批读     batched_get_blocking       —— 同步取一批（默认实现就是循环调 get_blocking）
生命周期 pin / unpin / remove / close —— 钉住、释放、删除、关闭
内存来源 get_allocator_backend      —— 「我该去哪个后端要内存？」
```

其中有一类方法**有默认实现**（子类可覆盖），最关键的是 `batched_get_blocking` 和 `batched_contains`：

- `batched_get_blocking` 的默认实现就是逐个调 `get_blocking`；
- `batched_contains` 的默认实现做的是**前缀匹配计数**——遇到第一个不命中的 key 就 `break`，返回连续命中的 chunk 数。这正是 u1-l6 里 lookup「只数连续前缀命中」语义的来源。

#### 4.1.3 源码精读

抽象基类的定义与构造校验（构造时用 `torch.device(dst_device)` 验证目标设备合法）：

[abstract_backend.py:L27-L45](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/abstract_backend.py#L27-L45) —— 定义 `StorageBackendInterface`，`dst_device` 表示「取回来的 KV 最终要落在哪个设备」（`cpu`/`cuda`/`cuda:0` 等）。

三个最具代表性的抽象方法：

[abstract_backend.py:L47-L60](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/abstract_backend.py#L47-L60) —— `contains`：检查 key 是否存在，`pin=True` 时顺便把对应 KV 钉住以免被淘汰。

[abstract_backend.py:L71-L101](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/abstract_backend.py#L71-L101) —— `batched_submit_put_task`：批量异步写入。源码顶部那条 NOTE 点明了「提供批量接口是为了让底层实现有优化空间」（例如磁盘并发读、远端流水线）。返回 `List[Future]` 表示异步、`None` 表示同步。

[abstract_backend.py:L118-L130](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/abstract_backend.py#L118-L130) —— `get_blocking`：同步取一个 `MemoryObj`，不存在返回 `None`。

两个「有默认实现」的关键方法，理解它们就理解了 lookup 的前缀语义和批读的兜底：

[abstract_backend.py:L178-L192](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/abstract_backend.py#L178-L192) —— `batched_get_blocking` 默认实现：循环 `get_blocking`，子类若能从批量中受益（如磁盘并发 IO）可覆盖它。

[abstract_backend.py:L272-L293](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/abstract_backend.py#L272-L293) —— `batched_contains` 默认实现：**遇到第一个不命中就 break**，返回连续命中数，这就是前缀匹配。

两个扩展接口：

[abstract_backend.py:L325-L345](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/abstract_backend.py#L325-L345) —— `AllocatorBackendInterface`：在基础契约之上增加「主动分配 `MemoryObj`」的能力（`allocate` / `batched_allocate` / `initialize_allocator`）。

[abstract_backend.py:L424-L456](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/abstract_backend.py#L424-L456) —— `StoragePluginInterface`：可由配置文件即插即用加载的后端基类，构造签名带 `config/metadata/local_cpu_backend/loop`。

#### 4.1.4 代码实践

**实践目标**：把抽象契约拆成「必须实现」「有默认实现」「扩展能力」三张表，建立后续阅读具体后端时的对照基准。

**操作步骤**：

1. 打开 [abstract_backend.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/abstract_backend.py)。
2. 用 `@abc.abstractmethod` 装饰器作为分界：带装饰器的是子类必须实现的，不带的（方法体不是纯 `raise NotImplementedError`）是默认实现。
3. 建议在本地用 `grep` 统计：
   ```bash
   grep -n "abstractmethod" lmcache/v1/storage_backend/abstract_backend.py
   ```

**预期结果**：你会得到约 12 个抽象方法（`contains/exists_in_put_tasks/batched_submit_put_task/get_blocking/pin/unpin/remove/get_allocator_backend/close` 等），以及若干带默认实现的方法（`get_non_blocking/batched_get_blocking/batched_contains/batched_remove/touch_cache/cancel_request`）。

#### 4.1.5 小练习与答案

**练习 1**：`get_non_blocking` 在基类里没有 `@abc.abstractmethod`，方法体只是 `raise NotImplementedError`。这和真正的抽象方法有什么区别？

**参考答案**：没有 `@abstractmethod` 意味着**子类可以不实现它也能实例化**；它是「可选能力」。后端若支持非阻塞取（如预取），就覆盖它；不支持则保留默认抛错的行为。这是「接口里区分必备能力与可选能力」的常用手法。

**练习 2**：为什么 `batched_contains` 的默认实现要在第一个不命中的 key 处 `break`，而不是继续统计后面的命中？

**参考答案**：因为 KV cache 的复用是**前缀复用**——模型只能从 prompt 开头连续复用已算过的 token，中间断了一格后面就不能直接接上（断点之后需要重算）。所以只统计连续前缀命中数才有意义，`break` 正确反映了这个语义。

---

### 4.2 LocalCPUBackend：热缓存 + 内存分配器

#### 4.2.1 概念说明

`LocalCPUBackend` 是整个分层体系的**基石**，原因在于它的双重身份：

1. **最快的缓存层**：维护一个 `hot_cache` 字典（key → `MemoryObj`），命中即返回，无需任何 IO。
2. **全局内存分配器**：所有后端（包括磁盘、远端）在 CPU 上中转数据时需要的 `MemoryObj`，都由它内部的 `memory_allocator` 分配。

类顶部 docstring 一句话点明了这层关系的精髓：

> Even if local_cpu is False (the hot_cache is not used), contains()/insert_key()/remove()/get_blocking()/get_keys()/clear() are still callable by the storage manager.

也就是说，**即使配置关掉了热缓存（`local_cpu=False`），`LocalCPUBackend` 依然存在**——因为别的后端要靠它当缓冲区、靠它的分配器要内存。这就是为什么工厂函数里它会「无条件创建」（见 4.5）。

#### 4.2.2 核心流程

`LocalCPUBackend` 的读写路径：

```text
写入（put）:
  batched_submit_put_task(keys, objs)
    └─ 若 use_hot=False → 直接返回（不进 hot_cache）
    └─ 否则逐个 submit_put_task:
         · cpu_lock 加锁
         · ref_count_up（登记引用，防止被过早回收）
         · hot_cache[key] = memory_obj
         · cache_policy.update_on_put(key)   # 更新 LRU 等淘汰策略
         · 批量上报 ADMIT 消息给 controller

读取（get）:
  get_blocking(key)
    · cpu_lock 加锁
    · hot_cache[key] 取出，ref_count_up（给调用方兜底）
    · 返回 MemoryObj（未命中返回 None）

分配（allocate）:  # 这是它作为「分配器」身份的核心
  allocate(shapes, dtypes, fmt, eviction, busy_loop)
    · memory_allocator.allocate(...) 直接要一块
    · 要不到且 eviction=True → 用 cache_policy 选淘汰候选
      → batched_remove(evict_keys, force=False) 腾地方
      → 还要不到且 busy_loop=True → sleep 后重试
```

并发模型很简洁：一把 `cpu_lock`（`threading.Lock`）保护 `hot_cache` 与缓存策略的所有读写。淘汰发生在**分配**路径上——当内存不够时，从 `hot_cache` 里挑出可淘汰的 key 踢出去。注意 `busy_loop` 参数：retrieve 时可以 busy loop（因为只有一个 retrieve，store 会慢慢释放内存），但 store 时绝不能 busy loop（多个 store 并发 busy loop 会死锁）——源码注释把这条规则讲得很清楚。

#### 4.2.3 源码精读

类定义与 docstring（双重身份的声明）：

[local_cpu_backend.py:L42-L47](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/local_cpu_backend.py#L42-L47) —— `LocalCPUBackend(AllocatorBackendInterface)`，注意它继承的是「能分配内存」的扩展接口。

构造期的几个关键字段：

[local_cpu_backend.py:L62-L77](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/local_cpu_backend.py#L62-L77) —— `hot_cache` 来自缓存策略的 `init_mutable_mapping()`；`use_hot=config.local_cpu` 决定是否真的当缓存用；`cpu_lock` 守护并发；`memory_allocator` 在构造期就建好（或由外部传入）。

写入路径（同步、带引用计数与淘汰策略更新）：

[local_cpu_backend.py:L150-L187](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/local_cpu_backend.py#L150-L187) —— `submit_put_task`：先 `ref_count_up` 再放入 `hot_cache`，并通过 `batched_msg_sender` 把 `ADMIT` 事件批量上报给 cache controller。

读取路径（命中时给调用方做一次引用兜底）：

[local_cpu_backend.py:L211-L223](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/local_cpu_backend.py#L211-L223) —— `get_blocking`：取出后 `ref_count_up`，避免调用方还没来得及登记引用时该对象就被淘汰。

作为分配器的核心——`allocate` 的淘汰循环：

[local_cpu_backend.py:L624-L727](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/local_cpu_backend.py#L624-L727) —— `allocate`：先直接向分配器要；要不到且 `use_hot` 时，用 `cache_policy.get_evict_candidates` 选淘汰目标并 `batched_remove(force=False)`；仍要不到时根据 `busy_loop` 决定等待重试还是放弃。docstring 明确要求「StorageManager 应当永远通过 `local_cpu_backend.allocate()` 取内存，无论 `local_cpu` 是否为 True」。

「我就是分配器后端」的自证：

[local_cpu_backend.py:L956-L957](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/local_cpu_backend.py#L956-L957) —— `get_allocator_backend()` 返回 `self`：当别的后端问「我去哪要内存」时，CPU 后端指自己。

#### 4.2.4 代码实践

**实践目标**：理解 `use_hot` 开关如何让 `LocalCPUBackend` 在「缓存层」与「纯分配器」之间切换。

**操作步骤**：

1. 阅读 [local_cpu_backend.py:L189-L209](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/local_cpu_backend.py#L189-L209)（`batched_submit_put_task`），注意第一行 `if not self.use_hot: return`。
2. 思考：如果用户配置 `local_cpu: false` 但仍配了磁盘后端，一个 put 任务到达 CPU 后端时会发生什么？

**需要观察的现象 / 预期结果**：put 会被 CPU 后端「吞掉」（不入 hot_cache），但 `allocate()` 仍正常工作——磁盘后端正是靠这个分配器拿到中转 `MemoryObj`，再把字节写进文件。这印证了「双重身份」：关掉的只是缓存命中，分配器身份永远在。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `get_blocking` 取出对象后要做一次 `ref_count_up`？

**参考答案**：调用方拿到 `MemoryObj` 后还需要一点时间才能真正「接管」它（比如再 `ref_count_up`）。在这段窗口期内，如果别的线程触发淘汰把它回收了，调用方就会拿到悬空内存。预先 `ref_count_up` 给调用方兜底，调用方用完再 `ref_count_down` 即可。

**练习 2**：`allocate` 里 `busy_loop` 在 store 场景为什么必须为 False？

**参考答案**：store 是高并发场景——很多请求同时往里写。如果每个 store 在内存不足时都 busy loop 等别人释放，而释放又依赖 store 完成，就会形成循环等待即死锁。retrieve 通常一次只有一个，busy loop 等正在进行的 store 释放是安全的。

---

### 4.3 LocalDiskBackend：磁盘持久层

#### 4.3.1 概念说明

CPU 内存宝贵且易失，磁盘则**便宜、容量大、掉电不丢**。`LocalDiskBackend` 把 KV cache 以原始字节（`.pt` 文件）落到本地磁盘，承担「比 CPU 慢、但比远端快」的中间层。

它有两个关键设计：

- **自己不是分配器**：磁盘后端继承的是基础 `StorageBackendInterface`（不是 `AllocatorBackendInterface`）。它需要 CPU 内存做「中转缓冲」——读盘时先读到 CPU 内存，写盘时从 CPU 内存取字节。因此构造时**必须**注入一个 `local_cpu_backend`，由它来分配 `MemoryObj`。
- **异步写入 + 并发读取**：写盘是慢操作，不能阻塞推理，所以写入走异步执行器；读盘则利用线程池并发读取多个文件（GIL 在 `readinto` 系统调用期间会释放，能实现真正的 IO 并行）。

#### 4.3.2 核心流程

```text
写入（异步）:
  submit_put_task(key, memory_obj)
    · 先在 disk_lock 下做容量检查 + 淘汰（max_cache_size）
    · ref_count_up，把 async_save_bytes_to_disk 任务交给 disk_worker
    · disk_worker 用优先级队列调度（prefetch=0 > delete=1 > put=2）
    · 真正写文件时可选 O_DIRECT 绕过页缓存

读取（同步批量，并发）:
  batched_get_blocking(keys)
    · disk_lock 下批量查 dict 拿到每个 key 的 DiskCacheMetadata
    · 逐个 local_cpu_backend.allocate() 预分配中转 buffer
    · ThreadPoolExecutor.map 并发 readinto 把文件读进 buffer
    · 成功的更新 cache_policy（注意：失败的不更新，避免虚假命中）
```

磁盘后端的元数据存在 `self.dict`（key → `DiskCacheMetadata`），`DiskCacheMetadata` 记录了文件路径、大小、shape/dtype/fmt 等——因为磁盘上只存了裸字节，这些「怎么还原成张量」的信息必须单独保管。

#### 4.3.3 源码精读

异步执行器与任务优先级（预取优先于删除优先于写入）：

[local_disk_backend.py:L63-L71](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/local_disk_backend.py#L63-L71) —— `submit_task` 按类型分配优先级：`prefetch=0`（最急，预取要让推理尽快用上）、`delete=1`、`put=2`（写入最不急，可延后）。

类定义与对 CPU 后端的依赖：

[local_disk_backend.py:L103-L135](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/local_disk_backend.py#L103-L135) —— 构造时强制要求 `config.local_disk is not None`，并用 `PathSharder` 选定磁盘路径（支持多盘分片）；`local_cpu_backend` 作为中转缓冲被持有。

key 到文件路径的映射：

[local_disk_backend.py:L204-L208](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/local_disk_backend.py#L204-L208) —— `_key_to_path`：把 key 字符串化（`/` 替换为 `-` 防止路径穿越）后拼成 `<path>/<key>.pt`。

异步写入（含容量淘汰）：

[local_disk_backend.py:L317-L381](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/local_disk_backend.py#L317-L381) —— `submit_put_task`：在 `disk_lock` 下循环淘汰直到 `current_cache_size + required_size <= max_cache_size`，腾不出空间就放弃这次写入；否则把 `async_save_bytes_to_disk` 投递给 disk_worker。

并发批量读取（覆盖了基类的默认实现）：

[local_disk_backend.py:L452-L497](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/local_disk_backend.py#L452-L497) —— `batched_get_blocking`：一次加锁批量查元数据 → 预分配 buffer → `_read_thread_pool.map` 并发读盘。docstring 解释了「在 `readinto` 系统调用期间 GIL 释放，线程能获得真正 IO 并行」。

委托 CPU 后端当分配器：

[local_disk_backend.py:L767-L768](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/local_disk_backend.py#L767-L768) —— `get_allocator_backend()` 返回 `self.local_cpu_backend`：磁盘后端告诉 StorageManager「我要内存就找 CPU 后端」。

可选的 O_DIRECT 写入（绕过 OS 页缓存，减少一次拷贝）：

[local_disk_backend.py:L720-L734](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/local_disk_backend.py#L720-L734) —— `write_file`：当 buffer 大小是磁盘块大小（`os_disk_bs`）整数倍且开启 `use_odirect` 时，用 `os.O_DIRECT` 直写。

#### 4.3.4 代码实践

**实践目标**：追踪一次磁盘读取，看清「磁盘字节 → CPU 中转 buffer → 还原成 MemoryObj」的全过程。

**操作步骤**：

1. 打开 [local_disk_backend.py:L406-L450](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/local_disk_backend.py#L406-L450)（`get_blocking`）。
2. 注意它故意把「加锁查元数据」和「读盘」分成两段——读盘在锁外执行。
3. 回答：为什么读盘不能在 `disk_lock` 内进行？

**需要观察的现象 / 预期结果**：源码注释明说——读盘是阻塞操作（CPU 中转池分配 + 磁盘 memcpy 可能耗时），如果在锁内进行，并发的 insert/evict 就会**死锁**。这是「长耗时 IO 不能持锁」的典型例子。预期你能用自己的话复述这条规则。

#### 4.3.5 小练习与答案

**练习 1**：磁盘后端为什么要把 `shape/dtype/fmt/cached_positions` 存进 `DiskCacheMetadata`，而不是直接存张量？

**参考答案**：磁盘文件里只写了 `byte_array`（裸字节），目的是节省空间和避免反序列化开销。但读回来时要重建张量，就必须知道 shape/dtype/fmt；`cached_positions`（用于 CacheBlend 的非连续命中）也得单独记。所以「字节在文件里、元数据在内存 dict 里」是配合使用的一对。

**练习 2**：`batched_get_blocking` 里，如果某个 key 的文件读取失败了，代码会不会更新它的缓存命中（recency）？为什么？

**参考答案**：不会。成功读取后才在锁内 `update_on_hit`；失败（返回 None）的不更新。这样一次失败的读取不会制造「虚假命中」去刷新 LRU，避免污染淘汰决策——这是源码注释里明确的设计意图。

---

### 4.4 RemoteBackend：远端分布式层

#### 4.4.1 概念说明

本地 CPU 和磁盘都是**单机**资源，无法跨推理实例共享。`RemoteBackend` 通过网络连到一个远端 KV 存储服务（例如项目自带的 `lmcache_server`，见 u1-l4），让多个 vLLM 实例能**共享同一份 KV cache**——这是跨会话、跨引擎复用的关键。

它与前两层的根本不同在于**不确定性**：网络会断、会超时、远端会不可用。因此 `RemoteBackend` 的代码里有大量「连接可能为 None」「超时重连」「失败降级返回 None/False」的防御性逻辑。它的设计哲学是：**远端挂了不能拖垮本地推理**——查询失败就当未命中，绝不让推理请求阻塞在网络上。

它同样不是分配器（继承基础接口），中转内存仍由 `local_cpu_backend` 提供；并且数据上线前要先**序列化/压缩**（通过 `serializer`），下线后要**反序列化**（`deserializer`），这部分留到 u2-l7 讲。

#### 4.4.2 核心流程

```text
连接管理:
  init_connection()
    · 通过 CreateConnector(url, ...) 建一条 RemoteConnector
    · 失败时记录 failure_time，10 秒内不重连（min_reconnect_interval）

写入（异步）:
  submit_put_task(key, memory_obj)
    · connection 为 None → 返回一个已完成空 Future（不报错）
    · ref_count_up → serializer.serialize 压缩 → 投递到 event loop
    · 完成回调里 put_callback 把 key 移出 put_tasks 集合

读取（同步，带超时）:
  get_blocking(key)
    · 投递 connection.get(key) 到 event loop，future.result(timeout)
    · 超时 → 取消 future，返回 None
    · 成功 → deserializer.deserialize 还原

特性:
  · pin/unpin 是空操作（返回 True）——远端不支持钉住
  · MLA worker_id_as0 模式下，非 0 号 worker 跳过 put、查询时改用 worker_id=0
```

#### 4.4.3 源码精读

类定义与连接/序列化器初始化：

[remote_backend.py:L27-L72](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/remote_backend.py#L27-L72) —— 构造期建立 `connection`（`RemoteConnector`），并用 `CreateSerde(config.remote_serde, ...)` 生成序列化/反序列化器。支持「新插件方式」（`plugin_name`）与「legacy `remote_url`」两种寻址。

连接与重连节流（10 秒内不重复重连）：

[remote_backend.py:L119-L161](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/remote_backend.py#L119-L161) —— `init_connection`：若距上次失败不足 `min_reconnect_interval`（10s）则跳过，避免远端抖动时疯狂重连打爆自己。

防御性查询（连接为空直接返回 False，不抛错）：

[remote_backend.py:L163-L186](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/remote_backend.py#L163-L186) —— `contains`：`connection is None` 时 warning 并返回 `False`；通过 `asyncio.run_coroutine_threadsafe` 把异步的 connector 调用桥到同步结果。

异步写入（失败降级、引用计数、序列化）：

[remote_backend.py:L222-L273](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/remote_backend.py#L222-L273) —— `submit_put_task`：连接为空或 key 已在 `put_tasks` 中时返回「已完成空 Future」；否则序列化后投递到 event loop，完成回调里清理 `put_tasks`。

带超时的同步读取（超时取消、失败返回 None）：

[remote_backend.py:L341-L387](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/remote_backend.py#L341-L387) —— `get_blocking`：`future.result(self.config.blocking_timeout_secs)`，超时则 `future.cancel()` 并返回 None；成功后反序列化。还记录了 get/反序列化两段耗时便于观测。

pin/unpin 是 no-op（远端无法钉住单个 key）：

[remote_backend.py:L582-L594](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/remote_backend.py#L582-L594) —— `pin/unpin` 仅打 debug 日志并返回 `True`，符合基类契约但不做实事。

委托 CPU 后端当分配器：

[remote_backend.py:L609-L614](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/remote_backend.py#L609-L614) —— `get_allocator_backend()` 返回 `local_cpu_backend`。

#### 4.4.4 代码实践

**实践目标**：体会 RemoteBackend「绝不让远端故障拖垮本地推理」的容错设计。

**操作步骤**：

1. 浏览 [remote_backend.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/remote_backend.py) 中所有 `self.connection is None` 的判断分支。
2. 数一数有多少方法在「连接为空 / 异常」时选择 `return None` / `return False` / 返回空 Future，而不是抛异常。

**需要观察的现象 / 预期结果**：几乎所有面向 StorageManager 的方法（`contains/get_blocking/submit_put_task/remove/batched_*`）都有 try/except 兜底，失败即降级为「未命中」。预期结论：远端层是「尽力而为」的——挂了就当作没这条 KV，推理照常走 prefill 重算，只是丢了复用收益。

> 待本地验证：若你有一个可运行的部署，可故意停掉 `lmcache_server` 进程，观察推理是否仍正常（应正常，只是命中率下降、日志出现 remote connection 警告）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 RemoteBackend 的 `pin/unpin` 是 no-op，而 LocalCPUBackend 和 LocalDiskBackend 都真正实现了 pin？

**参考答案**：pin 的作用是「把对象钉在本层，防止被本地淘汰」。远端 KV 服务的生命周期和淘汰策略由远端自己管，本地客户端没有能力「钉住」远端的某个 key（除非远端协议专门支持）。所以这里 no-op 是诚实的——告诉上层「我做不到，但别报错」，让上层不至于因为没有 pin 能力而崩溃。

**练习 2**：`submit_put_task` 在连接为空时返回一个「已经 set_result(None) 的空 Future」而不是 `None`。为什么？

**参考答案**：基类契约里 put 的返回值是 `Union[List[Future], None]`——返回 Future 表示「异步、已受理」，返回 None 表示「同步完成或失败」。返回一个已完成空 Future，既维持了「异步」语义（调用方可以一致地 `add_done_callback`），又表示「这次什么都没真正做」。这是一种让接口行为对调用方更可预测的写法。

---

### 4.5 后端的创建与分层编排（写穿 + 读提升）

#### 4.5.1 概念说明

有了三个后端，还差一个把它们**按顺序组装并调度**的角色。这由两处代码完成：

- `CreateStorageBackends`（`__init__.py`）：工厂函数，按**固定顺序**把启用的后端塞进一个 `OrderedDict`。这个顺序就是分层优先级。
- `StorageManager`（`storage_manager.py`）：持有这个 `OrderedDict`，实现两条核心调度：
  - **写穿（write-through）**：一次 `batched_put` 会**同时**写入所有启用的层（CPU + 磁盘 + 远端），而不是「CPU 满了才溢出到磁盘」。
  - **读提升（read promotion）**：一次 `get` 按 CPU → 磁盘 → 远端的顺序**线性查找**，命中即返回；若命中发生在慢层（磁盘/远端），会顺便回填到 `LocalCPUBackend`，下次直接命中 CPU。

> ⚠️ 重要澄清：很多人把分层缓存想象成「逐层驱逐瀑布」（CPU 满了才把旧数据赶到磁盘，磁盘满了才赶到远端）。**LMCache 不是这样**。它的写路径是 fan-out（同时写各层），淘汰只发生在**每一层各自满了时**、由该层自己的 `cache_policy`（LRU 等）独立决定。读路径才是「按层查找 + 提升」。这条理解非常关键，否则会误读 4.5.2 的流程。

之所以采用写穿，是因为 KV cache 的写入通常是异步的（不阻塞推理），多写几层换来了「任何一层命中都能加速」的灵活性；而读提升则保证了热数据会自动「浮」到最快的 CPU 层。

#### 4.5.2 核心流程

```text
创建（CreateStorageBackends，固定顺序，OrderedDict）:
  1. PDBackend        (若 enable_pd)
  2. LocalCPUBackend  (几乎总是创建——别的后端要拿它当缓冲/分配器)
  3. P2PBackend       (若 enable_p2p)
  4. NixlStorageBackend (若 enable_nixl_storage)
  5. LocalDiskBackend (若 local_disk 且 max_local_disk_size>0，依赖 local_cpu_backend)
  6. GdsBackend       (若 gds_path)
  7. MaruBackend      (若 maru_path)
  8. RemoteBackend    (若 remote_storage_plugins 或 legacy remote_url，依赖 local_cpu_backend)

写穿（batched_put）:
  for backend in storage_backends.values():     # 按 OrderedDict 顺序
      allocator = backend.get_allocator_backend()  # 找到该后端的内存来源
      若该来源还没分配过 buffer → allocate_and_copy_objects 从 GPU 拷一份到 CPU
      backend.batched_submit_put_task(...)          # 各自异步落库
  → 一次 put，多个后端各存一份

读提升（get / batched_get）:
  for backend in get_active_storage_backends():  # CPU → 磁盘 → 远端
      mem = backend.get_blocking(key)
      if mem:
          if backend 不是 LocalCPUBackend/PDBackend/MaruBackend 且存在 LocalCPUBackend:
              local_cpu_backend.submit_put_task(key, mem)   # 回填到 CPU（提升）
          return mem
  return None
```

可以用一个简单的公式刻画一次读取的期望命中位置。设第 \(i\) 层的命中率为 \(h_i\)（按从快到慢排列），则在第 \(i\) 层命中的概率为：

\[
P(\text{命中于第 }i\text{ 层}) = (1 - h_1)(1 - h_2)\cdots(1 - h_{i-1})\cdot h_i
\]

读取的期望时延近似为各层时延按命中概率的加权和。LMCache 通过「读提升」不断提高 \(h_1\)（CPU 层命中率），从而把期望时延压低。

#### 4.5.3 源码精读

工厂函数与「LocalCPUBackend 总是创建」的关键注释：

[\_\_init\_\_.py:L145-L188](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/__init__.py#L145-L188) —— 顶部 NOTE：「The local_cpu backend is always created because other backends might need it as a buffer.」随后按条件创建各后端，磁盘与远端的创建都要求 `local_cpu_backend` 非空。

磁盘后端的创建（显式注入 `local_cpu_backend`）：

[\_\_init\_\_.py:L217-L233](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/__init__.py#L217-L233) —— `LocalDiskBackend` 构造时把 `local_cpu_backend` 作为第三个位置参数传入，建立「磁盘依赖 CPU 当缓冲」的依赖。

StorageManager 构造期建立分层视图：

[storage_manager.py:L243-L262](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/storage_manager.py#L243-L262) —— `storage_backends` 是 `OrderedDict`，`non_allocator_backends` 是「真正参与存储」的后端名单（`local_cpu=False` 时 CPU 后端会被排除，因为它只当分配器）。

写穿调度（fan-out 到所有后端）：

[storage_manager.py:L384-L433](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/storage_manager.py#L384-L433) —— `batched_put`：遍历 `storage_backends`，对每个后端通过 `get_allocator_backend()` 找到内存来源，按需从 GPU 拷一份 CPU buffer，再调各自的 `batched_submit_put_task`。这就是「一次 put、多层各存一份」。

读提升调度（线性查找 + 命中后回填 CPU）：

[storage_manager.py:L435-L459](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/storage_manager.py#L435-L459) —— `get`：按 `get_active_storage_backends()` 顺序逐层 `get_blocking`，命中后若来源不是 CPU/PD/Maru 层，则 `submit_put_task` 回填到 `LocalCPUBackend`。

按 OrderedDict 顺序枚举（freeze/bypass 过滤）：

[storage_manager.py:L1165-L1193](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/storage_manager.py#L1165-L1193) —— `get_active_storage_backends`：按 `storage_backends.items()` 顺序 yield，受 freeze（只留 CPU）和 bypass（健康检查失败时跳过某层）两种模式过滤。「按 OrderedDict 顺序」就是「按层优先级」。

「真正存储」后端名单的判定：

[storage_manager.py:L1195-L1210](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/storage_manager.py#L1195-L1210) —— `get_non_allocator_backends`：`local_cpu=False` 时把 `LocalCPUBackend` 排除（纯分配器），`PDBackend` sender 角色时排除——其余才计入「存储层」。

> 旁路：除了三层主链，`__init__.py` 里还能看到 `P2PBackend`（跨进程点对点）、`GdsBackend`（GPU Direct Storage，GPU 直读磁盘绕过 CPU）、`NixlStorageBackend`（基于 NIXL 的跨节点传输）、`MaruBackend`、以及 PD 场景的 `PDBackend`/`PDBackendAsync`。这些属于专家层内容，分别在 u4-l7（PD/传输）、u4-l3（L2 适配器）等讲义展开，本讲只需知道它们「插在同一个 OrderedDict 里、遵循同一套 `StorageBackendInterface`」。

#### 4.5.4 代码实践

**实践目标**：给 LocalCPU/LocalDisk/Remote 各写一句话职责，并画出 GPU → CPU → Disk → Remote 的数据流向图（这是本讲的总实践，详见第 5 节综合实践）。

**操作步骤**：

1. 重新打开 [\_\_init\_\_.py 的 CreateStorageBackends](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/__init__.py#L111-L336)，按出现顺序抄下后端清单。
2. 对照 [storage_manager.py 的 batched_put](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/storage_manager.py#L384-L433) 与 [get](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/storage_backend/storage_manager.py#L435-L459)，分别画出「写路径」与「读路径」两张小图。

**预期结果**：你能用一句话准确区分三层（见第 5 节答案模板），并能复述「写是 fan-out、读是线性 + 提升」这一关键事实，而**不是**误以为它是逐层驱逐瀑布。

#### 4.5.5 小练习与答案

**练习 1**：假设配置同时开了 `local_cpu`、磁盘和远端，一次 `batched_put` 会让 KV 最终存在几个地方？

**参考答案**：三处都有——CPU 的 `hot_cache`、磁盘文件、远端服务各存一份（写穿）。它们的写入是各自异步进行的，互不阻塞。注意 CPU buffer 是共享的（同一份 `MemoryObj` 经引用计数被多处持有），但「持久化形态」是三份。

**练习 2**：`get_non_allocator_backends` 在 `local_cpu=False` 时会把 `LocalCPUBackend` 排除。这意味着此时 CPU 后端完全不工作吗？

**参考答案**：不是。它仍然作为**分配器**在工作——磁盘和远端要中转内存都得找它。被排除的只是「它作为存储层」这个身份（不进 `non_allocator_backends` 名单、put 时不往 `hot_cache` 写）。这正是「双重身份」的精确体现：关掉一个身份，另一个还在。

---

## 5. 综合实践

把本讲的知识串起来，完成下面这个贯穿性小任务。

### 任务

你是一名新加入的工程师，被要求向团队介绍 LMCache 的存储后端分层。请完成两份产出：

**产出 A：三层一句话职责表**

阅读 4.2/4.3/4.4 的源码精读，给三层各写一句不超过 20 字的职责说明。参考模板（请你用自己的话改写，不要照抄）：

| 后端 | 职责（示例，待你改写） | 是否分配器 | 命中后是否回填 CPU |
| --- | --- | --- | --- |
| LocalCPUBackend | 最快热缓存 + 全局内存分配器 | 是 | ——（自己就是 CPU） |
| LocalDiskBackend | KV 字节落本地磁盘，异步写并发读 | 否（靠 CPU 分配） | 是 |
| RemoteBackend | 经网络连远端 KV 服务，跨实例共享 | 否（靠 CPU 分配） | 是 |

**产出 B：数据流向图**

在纸上或任意画图工具里画出两张图：

1. **写路径（store / batched_put）**：标注「GPU KV → 经 gpu_connector 转成 MemoryObj → StorageManager.batched_put → 同时 fan-out 到 LocalCPU / LocalDisk / Remote」三条并行支线，并在磁盘支线标「异步线程池」、远端支线标「序列化 + 网络」。
2. **读路径（retrieve / get）**：标注「按 LocalCPU → LocalDisk → Remote 顺序线性查找，首个命中返回；慢层命中时回填 LocalCPU（提升）」。

并务必在图旁写一句批注：「写穿 + 读提升，**非**逐层驱逐瀑布」。

### 验证

- 若你的写路径图画成了「CPU 满 → 溢出到磁盘 → 磁盘满 → 溢出到远端」的单线瀑布，说明你误解了模型，请重读 4.5.1 的澄清段落。
- 若你给 `LocalDiskBackend` 标了「是分配器」，请回到 4.3.1 修正：它继承的是基础 `StorageBackendInterface`，`get_allocator_backend()` 返回的是 `local_cpu_backend`。

> 待本地验证（可选）：若环境允许，可写一段最小脚本，配置一个只含 `LocalCPUBackend` + `LocalDiskBackend` 的引擎，连续 `store` 超过 `max_local_cpu_size` 的 KV，观察：CPU 层会触发 LRU 淘汰（日志 `Evicting N chunks from cpu memory`），而磁盘层因写穿仍然保留这些 KV——从而验证「淘汰是各层独立的、不是把数据赶到下一层」。

## 6. 本讲小结

- `StorageBackendInterface` 是所有后端的统一契约，规定了 `contains/get_blocking/batched_submit_put_task/pin/unpin/remove/get_allocator_backend/close` 等方法；其中 `batched_contains`（前缀计数）和 `batched_get_blocking`（循环 get）有默认实现，`AllocatorBackendInterface` 和 `StoragePluginInterface` 是两个扩展方向。
- `LocalCPUBackend` 有双重身份——最快的缓存层（`hot_cache`）+ 全局内存分配器（`allocate`），因此即使 `local_cpu=False` 也总是被创建；淘汰发生在它的 `allocate` 路径上。
- `LocalDiskBackend` 把 KV 以字节落盘，异步优先级写入（prefetch>delete>put）、线程池并发读取；它不是分配器，依赖 `local_cpu_backend` 做中转缓冲，`O_DIRECT` 可绕过页缓存。
- `RemoteBackend` 经 `RemoteConnector` 连远端 KV 服务，写入前序列化、读取后反序列化；它的核心设计是「绝不让远端故障拖垮本地」——连接为空或异常一律降级为未命中，pin/unpin 是 no-op。
- 工厂 `CreateStorageBackends` 按固定顺序把后端组装成 `OrderedDict`，`StorageManager` 在此之上实现**写穿（fan-out 到所有层）+ 读提升（线性查找、慢层命中回填 CPU）**——这是分级缓存，不是逐层驱逐瀑布。
- 除三层主链外，`P2PBackend`、`GdsBackend`、`NixlStorageBackend`、`MaruBackend`、`PDBackend` 等特殊后端插在同一个 `OrderedDict`、遵守同一契约，细节留到 u4 单元。

## 7. 下一步学习建议

- **紧接 u2-l4**：本讲刻意只看了 `StorageManager` 与分层相关的 `batched_put/get` 几个方法。下一讲会完整拆解 `StorageManager`，包括 `AsyncMultiSerializer`/`AsyncSingleSerializer` 异步序列化与 `WeightedSemaphore` 并发资源控制——那是写穿路径真正的并发引擎。
- **横向 u2-l7**：本讲反复提到「远端写入前序列化」「读回后反序列化」，这套 SERDE 机制（legacy `cachegen` serde 与 v1 `kv_codec`）将在 u2-l7 专讲。
- **纵向 u4-l2 / u4-l3**：本讲的 `v1/storage_backend/` 是**单机分层**；u4 单元的 `v1/distributed/` 是新的 **L1/L2 分布式存储**与可插拔 L2 适配器（Redis/S3/NIXL…），是这套分层思想的演进版，建议学完 u2 后对照阅读 `docs/design/v1/distributed/`。
- **代码导读**：若想立刻动手，建议按 `abstract_backend.py → local_cpu_backend.py → __init__.py(CreateStorageBackends) → storage_manager.py(batched_put/get)` 的顺序通读一遍，这条路径能让你把本讲所有结论在源码里一一印证。
