# L2 适配器与可插拔存储

## 1. 本讲目标

本讲是专家层「分布式存储」的第二篇，承接 [u4-l2 分布式存储架构](u4-l2-distributed-storage.md)。在 u4-l2 里，我们把 `v1/distributed/` 的 L1/L2 两级模型当作一个整体来看，其中 **L2（远端异步适配器）** 是一个被反复提到、却没有展开的「黑盒」。本讲就打开这个黑盒。

学完本讲，你应当能够：

- 说清 `L2AdapterInterface` 这一份「适配器契约」规定了哪些方法、为什么是这样规定的（非阻塞 submit / poll / query 模式、三个互不相同的 event fd、客户端锁语义）。
- 解释工厂与「惰性自动发现」是如何做到「新增一个适配器文件，零改动接进系统」的。
- 看懂 RESP（Redis/Valkey）与 S3 两个具有代表性的实现：一个是「薄配置 + 委托给 C++ 原生连接器」，一个是「纯 Python + asyncio + awscrt 全自研」。
- 写出一段让 LMCache 把 Redis 当作 L2 的配置（`--l2-adapter` JSON），并理解它与 S3 配置在字段上的差异。
- 对比各类远端后端（Redis/S3/Mooncake/NIXL/P2P/FS 等）的适用场景，知道何时该选哪一个。

## 2. 前置知识

本讲默认你已经读过 u4-l2，熟悉以下概念（这里只做最短的回顾）：

- **L1 / L2 两级模型**：L1 是本机内存池（命中即用、读写都落脚于此）；L2 是远端/异构的异步存储层，所有读写都「非阻塞」，目的是把慢速 I/O 踢出 GPU 热路径。
- **ObjectKey**：内容寻址的缓存键，含 `model_name`、`kv_rank`、`object_group_id`、`chunk_hash`，以及用于租户隔离的 `cache_salt`（不进内容指纹，详见 [u3-l3](u3-l3-mp-coordinator.md)）。
- **三类后台控制器**：`StoreController`（L1→L2 复制）、`PrefetchController`（L2→L1 预取，仅加载连续前缀）、`EvictionController`（水位淘汰）。它们正是 L2 适配器的「调用方」。
- **submit → poll → query**：这是贯穿本讲的异步原语模式，下面会反复出现。

此外补充两个本讲会用到的底层概念：

- **event fd（事件文件描述符）**：Linux 提供的一种「可以被通知」的整数句柄。线程可以用 `select.poll()` 阻塞等待它，被通知后从内核读到一个计数值。LMCache 用它把「I/O 完成了」这一事件从后台线程传给控制器线程，实现事件驱动而非忙等。
- **Redis/Valkey**：基于 RESP（REdis Serialization Protocol）协议的内存 KV 存储；Valkey 是 Redis 的开源分叉，协议兼容。本讲的 `resp` 适配器同时支持二者。**S3**：对象存储协议，按 bucket/key 存取任意二进制对象，HTTP(S) 接口，AWS SigV4 签名。

## 3. 本讲源码地图

本讲涉及的关键文件都集中在 `lmcache/v1/distributed/l2_adapters/`：

| 文件 | 作用 |
| --- | --- |
| [base.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/l2_adapters/base.py) | 定义 `L2AdapterInterface` 契约、`AdapterUsage` 用量快照、字节账本与 listener 通知机制。 |
| [config.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/l2_adapters/config.py) | 配置基类 `L2AdapterConfigBase`、**配置类注册表**、`--l2-adapter` 命令行解析。 |
| [factory.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/l2_adapters/factory.py) | **工厂可调用对象注册表**与惰性导入（lazy import）。 |
| [\_\_init\_\_.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/l2_adapters/__init__.py) | 用 `pkgutil` 自动发现所有 `*_l2_adapter.py` 模块，对外暴露 `create_l2_adapter`。 |
| [resp_l2_adapter.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/l2_adapters/resp_l2_adapter.py) | RESP（Redis/Valkey）适配器：薄配置 + 工厂，委托给 C++ 原生连接器。 |
| [s3_l2_adapter.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/l2_adapters/s3_l2_adapter.py) | S3 适配器：纯 Python + asyncio + awscrt 全自研实现。 |

本目录下还存在大量其他适配器（`aerospike_*`、`dax_*`、`hfbucket_*`、`mooncake_store_*`、`nixl_store_*`、`p2p_*`、`fs_*`、`raw_block_*`、`plugin_*`、`fault_inject_*` 等），它们都遵循同一套契约，本讲在 4.4 节末尾给出选型对比。配套设计文档位于 `docs/design/v1/distributed/l2_adapters/`（项目约定 `docs/design/` 镜像 `lmcache/` 包树，读代码前先读同路径设计文档）。

---

## 4. 核心概念与源码讲解

### 4.1 L2 适配器要解决的问题与统一契约

#### 4.1.1 概念说明

L2 适配器要解决的核心矛盾是：**KV cache 的远端存储后端千差万别**——有的是内存型（Redis）、有的是对象存储（S3）、有的是 RDMA/共享内存（Mooncake、NIXL）、有的是对端进程的显存（P2P）。但上层的 `StoreController` / `PrefetchController` 不应该为每种后端写一套逻辑。

因此 LMCache 定义了一份统一的契约 `L2AdapterInterface`：把「把一批 KV 对象存到远端 / 在远端查它们在不在 / 把它们取回本机」这三件事，抽象成三组**非阻塞原语**。所有具体后端都来实现这份契约，控制器只面向契约编程。

这份契约有几个关键的「设计取舍」，理解它们比记忆方法名更重要：

1. **非阻塞（non-blocking）**：所有 `submit_*` 立即返回一个 `L2TaskId`，真正的 I/O 在后台异步进行。因为 L2 永远不能挡住 GPU 关键路径——哪怕远端慢到 100ms，引擎也得继续算。
2. **批量（batch）**：每次提交都是一组 key + 一组对象，而不是单个。这契合 KV cache「按 chunk 切、成串出现」的特点，减少往返。
3. **调用方管理内存生命周期**：`store` 时调用方传 `MemoryObj` 进来（适配器只读不持有），`load` 时调用方传预分配好的写缓冲进来（适配器把数据写进去）。适配器不做内存分配。
4. **粗粒度 store 错误 vs 细粒度 lookup/load 错误**：store 整批要么成功要么失败（只上报 task 级）；lookup/load 返回 `Bitmap`，逐 key 标记成功失败。
5. **锁是建议性的、客户端的**：lookup 时顺带「锁住」命中的 key，防止在 load 之前被淘汰。但远端存储（如 Redis/S3）并没有 LMCache 的淘汰概念，所以这把锁大多是「客户端 refcount 字典」模拟出来的。

#### 4.1.2 核心流程

三组原语都遵循同一个**「提交 → 轮询事件 → 查询结果」**三步模式：

```text
调用方线程                         适配器（后台）
─────────────────────────────────────────────────────────
1. task_id = submit_store_task(keys, objs)   ──▶  入队，开始异步 I/O
        │
        │  （控制器去做别的事，或 poll 其他 fd）
        ▼
2. poll( get_store_event_fd() )   ◀──  I/O 完成，notify event fd
        │
        ▼
3. result = pop_completed_store_tasks()  ──▶  取走 task_id → 结果 的映射
```

`lookup_and_lock` 与 `load` 略有不同：它们用 `query_xxx_result(task_id)` 而不是 `pop_completed_xxx`，并且**对同一个 task_id 只会返回一次非 None 结果**（一次性语义，非幂等）：

```text
submit_lookup_and_lock_task(keys) -> task_id
        │
        ▼
poll( get_lookup_and_lock_event_fd() )
        │
        ▼
bitmap = query_lookup_and_lock_result(task_id)
        # None = 还没完成；Bitmap = 完成了（只返回这一次）
```

> 数学上，「只返回一次」意味着对于任意 task_id \(t\)，序列 \(\{q_1(t), q_2(t), \dots\}\)（每次调用 `query`）中至多只有一个元素非 `None`。这个不变量让控制器无需担心重复消费。

#### 4.1.3 源码精读

契约本体在 [base.py:L78-L117](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/l2_adapters/base.py#L78-L117)，类文档把上面那些设计取舍讲得很清楚。下面挑三处最关键的代码点。

**三个互不相同的 event fd**——这是整个契约最硬的不变量：

[base.py:L151-L194](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/l2_adapters/base.py#L151-L194) 定义了 `get_store_event_fd` / `get_lookup_and_lock_event_fd` / `get_load_event_fd` 三个抽象方法，每个的 docstring 都反复强调「Must be distinct」。原因在配套设计文档 [overall.md](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/docs/design/v1/distributed/l2_adapters/overall.md) 里写得很直白：控制器建的是 `fd → adapter_index` 的映射表，如果两个 fd 撞了，事件会被静悄悄地派发给错误的适配器。

**Store 与 Load 的签名对比**——注意两者都由调用方提供 buffer，但语义相反：

[base.py:L200-L219](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/l2_adapters/base.py#L200-L219) 是 `submit_store_task`（适配器**读** `objects`），[base.py:L303-L327](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/l2_adapters/base.py#L303-L327) 是 `submit_load_task`（适配器**写** `objects`，把它们当写缓冲）。

**字节账本与 `AdapterUsage`**——这是契约里少数有「默认实现」的部分。基类在 `__init__` 里维护 `_total_bytes_used` 和按 `cache_salt` 分桶的 `_bytes_by_cache_salt`，子类只需要在存/删时调用 `_notify_keys_stored` / `_notify_keys_deleted` 报一下尺寸，基类就替你算好用量：

[base.py:L357-L381](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/l2_adapters/base.py#L357-L381) 是 `_notify_keys_stored`：先按 salt 聚合增量（大表场景下「每 salt 一次 dict 读写」而非「每 key 一次」），再在 `_usage_lock` 下更新总数，最后在锁外触发 listener——「慢 listener 不能拖累通知」。与之对称的 `_notify_keys_deleted`（[base.py:L389-L433](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/l2_adapters/base.py#L389-L433)）还做了下溢钳位：如果总数被减成负数（说明有双重删除或尺寸对不上的记账 bug），它会打告警并钳回 0，否则 `usage_fraction == -1` 这个「无淘汰信号」哨兵会永远关掉淘汰。

最终用量通过 `get_usage()`（[base.py:L499-L524](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/l2_adapters/base.py#L499-L524)）返回一个**只读快照** `AdapterUsage`（定义在 [base.py:L34-L75](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/l2_adapters/base.py#L34-L75)）。其中 `usage_fraction` 在容量未知（`max_capacity_bytes <= 0`）时返回 `-1.0`，沿用「`< 0` 即无淘汰信号」的旧约定。与之配套的 `supports_global_eviction` 属性（[base.py:L439-L457](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/l2_adapters/base.py#L439-L457)）只有声明了正的 `max_capacity_bytes` 才为 `True`——这决定了 `StorageManager` 是否给该适配器挂全局淘汰策略（详见 4.2 的 `_should_enable_l2_eviction`）。

#### 4.1.4 代码实践

**实践目标**：用集成测试里的真实用法，亲手验证「submit → poll → query」三步模式和「三个 event fd 互不相同」不变量。这是纯源码阅读 + 本地可选运行型实践。

**操作步骤**：

1. 打开集成测试 [test_resp_l2_adapter_integration.py:L133-L161](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/tests/v1/distributed/test_resp_l2_adapter_integration.py#L133-L161)，阅读 `test_event_fds_are_distinct` 与 `test_store_and_lookup`。
2. 注意它如何造对象：`create_object_key(i)` 构造 `ObjectKey`，`create_memory_obj(size=64, ...)` 构造一个填充好数据的 `TensorMemoryObj`（[test_resp_l2_adapter_integration.py:L68-L87](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/tests/v1/distributed/test_resp_l2_adapter_integration.py#L68-L87)）。
3. 注意三步模式在这里长这样：
   ```python
   store_tid = adapter.submit_store_task(keys, objs)   # 1. 提交
   assert wait_for_event_fd(store_fd)                   # 2. 轮询 event fd
   completed = adapter.pop_completed_store_tasks()      # 3. 取结果
   assert completed[store_tid].is_successful()
   ```
4. （可选，需本机有 Redis）按 [test_resp_l2_adapter_integration.py:L29-L30](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/tests/v1/distributed/test_resp_l2_adapter_integration.py#L29-L30) 的约定，起一个 Redis（默认 `localhost:6399`），然后运行：
   ```bash
   pytest tests/v1/distributed/test_resp_l2_adapter_integration.py -v
   ```
   没有 Redis 或没有 C++ 扩展时，测试会被 `@requires_redis` / `@requires_native` 自动跳过（见 [test_resp_l2_adapter_integration.py:L33-L65](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/tests/v1/distributed/test_resp_l2_adapter_integration.py#L33-L65)）。

**需要观察的现象**：

- `test_event_fds_are_distinct` 里把三个 fd 放进 `set`，断言 `len(fds) == 3`。这是「三个 event fd 必须互不相同」这条硬不变量的直接体现。
- `wait_for_event_fd` 用 `select.poll()` 阻塞等待（[test_resp_l2_adapter_integration.py:L90-L100](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/tests/v1/distributed/test_resp_l2_adapter_integration.py#L90-L100)），正是控制器线程的真实做法。

**预期结果**：若 Redis 与 C++ 扩展齐备，两条用例通过；否则 SKIPPED。无论是否运行，你都能从测试代码中确认契约的三步调用顺序。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `submit_store_task` 是「整批成功或失败」，而 `lookup_and_load` 要返回逐 key 的 `Bitmap`？

> **参考答案**：store 写入的是新数据，整批失败后调用方（`StoreController`）可以直接放弃或重试整批，不必关心单个 key；而 lookup/load 是在读「远端此刻有没有这个 key」，不同 key 的存在性彼此独立（比如前缀有、后面没有），必须逐位告诉你，调用方才能据此构造连续前缀的加载计划。

**练习 2**：`AdapterUsage.usage_fraction` 在什么情况下返回 `-1.0`？为什么不用 `None`？

> **参考答案**：当 `max_capacity_bytes <= 0`（适配器声明「我不追踪/不限总量」，如 FS 适配器假设磁盘无限）时返回 `-1.0`。这是沿用旧 `tuple[float, float]` API 时代的「`< 0` 即无淘汰信号」哨兵，让淘汰控制器可以继续用 `if usage_fraction < 0: skip` 这套老判断，无需改判空。

---

### 4.2 工厂与惰性自动发现机制

#### 4.2.1 概念说明

「统一契约」解决了「控制器如何调用」的问题，但还有两个工程问题没解决：

1. **如何根据配置字符串（如 `"resp"`、`"s3"`）选到正确的适配器类？** 而且这个映射最好能被第三方插件扩展，而不需要改核心代码。
2. **依赖隔离**：S3 适配器依赖 `awscrt`，Mooncake 依赖 `mooncake_store`，NIXL 依赖 `nixl`……如果导入 `l2_adapters` 包就一股脑全 import，用户得装齐所有依赖才能跑。这显然不合理。

LMCache 的解法是「**两张注册表 + 惰性自动发现**」：

- **配置类注册表**（在 `config.py`）：类型名 → 配置类，负责「从 JSON dict 构造配置对象」。
- **工厂注册表**（在 `factory.py`）：类型名 → 工厂可调用对象，负责「从配置对象构造适配器实例」。
- **惰性发现**（在 `__init__.py`）：用 `pkgutil` 扫描所有 `*_l2_adapter.py` 模块名，但**先不 import**；只有当某个类型名真正被请求时，才按需 import 对应模块。模块在被 import 时，通过模块底部的 `register_l2_adapter_type(...)` + `register_l2_adapter_factory(...)` 把自己登记进两张表。

这样，「新增一个适配器」=「新建一个 `xxx_l2_adapter.py` 文件」，**无需改动任何已有文件**（设计文档 [overall.md](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/docs/design/v1/distributed/l2_adapters/overall.md) 的「Implementing a New L2 Adapter」一节明确写了这一点）。

#### 4.2.2 核心流程

完整的「配置字符串 → 适配器实例」链路：

```text
--l2-adapter '{"type":"resp","host":...}'         # 命令行 JSON
        │
        ▼  json.loads + 按 "type" 查【配置类注册表】
RESPL2AdapterConfig.from_dict(d)                  # 得到配置对象
        │
        ▼  create_l2_adapter(config)
get_type_name_for_config(config) -> "resp"        # 反查类型名
        │
        ▼  ensure_adapter_loaded("resp")          # 【惰性导入】
遍历 _PENDING_MODULES，逐个 import_module，       # 模块底部自注册
直到 "resp" 出现在【工厂注册表】
        │
        ▼  _L2_ADAPTER_FACTORY_REGISTRY["resp"](config, l1_memory_desc)
NativeConnectorL2Adapter(...)                     # 得到适配器实例
```

注意这里有两张表，且它们的「填表时机」不同：配置类表在解析 JSON 时就要查，工厂表在创建实例时才查。两者都靠「模块被 import 时的副作用」来填充。

#### 4.2.3 源码精读

**自动发现，但不导入**——这是惰性的精髓：

[\_\_init\_\_.py:L32-L34](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/l2_adapters/__init__.py#L32-L34) 用 `pkgutil.iter_modules` 遍历本包下所有名字以 `_l2_adapter` 结尾的子模块，把它们的**全限定路径**塞进 `_PENDING_MODULES` 列表，注意只是「记下路径」，没有 `import`：

```python
for _finder, _module_name, _ispkg in pkgutil.iter_modules(__path__):
    if _module_name.endswith("_l2_adapter"):
        add_pending_module(f"{__name__}.{_module_name}")
```

**按需导入直到命中**——[factory.py:L101-L135](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/l2_adapters/factory.py#L101-L135) 的 `ensure_adapter_loaded`：从 `_PENDING_MODULES` 里逐个 `pop` 出模块路径来 `import_module`。某个模块若因缺第三方依赖（如没装 `awscrt`）抛 `ImportError`，就**跳过**它继续试下一个；只要目标类型名出现在注册表里就立刻返回。把所有待定模块都试完仍未命中，才把最后一个 `ImportError` 抛出去（很可能那就是根因）。这套机制让「没装 S3 依赖的用户」照样能用 Redis。

**两张注册表的写入**——[factory.py:L62-L81](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/l2_adapters/factory.py#L62-L81) 是 `register_l2_adapter_factory`（重复注册会 `raise ValueError`），[config.py:L57-L74](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/l2_adapters/config.py#L57-L74) 是对应的 `register_l2_adapter_type`。

**统一入口**——[\_\_init\_\_.py:L37-L61](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/l2_adapters/__init__.py#L37-L61) 的 `create_l2_adapter` 只是个门面，转交给 `create_l2_adapter_from_registry`（[factory.py:L165-L205](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/l2_adapters/factory.py#L165-L205)）。后者先用 `get_type_name_for_config` 反查类型名，触发惰性导入，再查工厂表调用之。

**StorageManager 如何调用**——`StorageManager._build_l2_adapter` 是真正把它们串起来的地方：[storage_manager.py:L1050-L1074](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/storage_manager.py#L1050-L1074)。它调用 `create_l2_adapter(config, self._l1_memory_desc)` 得到实例；若配置里带了 `serde_config`，还会用 `SerdeL2AdapterWrapper` 把适配器包一层（这样控制器看到的是普通适配器，SERDE 在 store/load 边界透明进行——这是与 [u4-l4 SERDE 变换](u4-l4-serde-transforms.md) 的接缝）。

#### 4.2.4 代码实践

**实践目标**：亲眼看到「新增一个适配器文件即被自动发现」这套机制工作，不写真正的 I/O，只用 mock 适配器验证发现链路。

**操作步骤**：

1. 打开 [test_l2_adapter_factory.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/tests/v1/distributed/test_l2_adapter_factory.py)，阅读它如何断言 `get_registered_l2_adapter_types()` 返回的类型名集合，以及如何用 mock 配置走通 `create_l2_adapter`。
2. 查看当前已注册的全部类型名（这会强制导入所有适配器模块，便于排查依赖）：
   ```bash
   python -c "from lmcache.v1.distributed.l2_adapters.config import get_registered_l2_adapter_types; print(get_registered_l2_adapter_types())"
   ```
3. 注意 `get_registered_l2_adapter_types` 内部调用了 `get_all_registered_names` → `load_all_adapters`（[factory.py:L138-L162](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/l2_adapters/factory.py#L138-L162)），它把所有 `_PENDING_MODULES` 全部 import 一遍——这是「CLI 帮助要列出所有类型名」时才会走的「暴力加载」路径，正常运行时不会触发。

**需要观察的现象**：

- 步骤 2 的输出应是一串按字母序排序的类型名，至少包含 `resp`、`s3`、`mock`、`fs` 等。若本机缺少某适配器的第三方依赖，该适配器会被 `load_all_adapters` 静默跳过（仅打 debug 日志），不会报错——这正是惰性机制对「依赖隔离」的兑现。

**预期结果**：能列出已注册类型名，确认 `resp` 与 `s3` 在内。如果某依赖缺失导致某个类型不在列表里，结合 debug 日志能定位到是哪个模块被跳过。若不便运行，标记「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `ensure_adapter_loaded` 在模块 `import_module` 抛 `ImportError` 时选择「跳过继续」而不是直接报错？

> **参考答案**：因为不同适配器依赖不同的第三方库（awscrt、mooncake、nixl……），用户通常只装了自己要用的那一个。若某个无关模块因依赖缺失就整体报错，用户会被迫装齐所有依赖。跳过继续 + 最后命中即返回，实现了「按需付费」的依赖隔离；只有在所有候选都试完仍找不到目标类型时，才把最后一个 ImportError 抛出作为根因提示。

**练习 2**：配置类注册表和工厂注册表为什么要分成两张，而不是一张「类型名 →（配置类，工厂）」？

> **参考答案**：它们的「被查时机」和「触发导入的职责」不同。配置类表在解析 JSON 阶段（`parse_args_to_l2_adapters_config`）就要查，且 `_ensure_config_loaded` 会反向调用 `factory.ensure_adapter_loaded` 触发惰性导入；工厂表在实例化阶段才查。分开后，单测可以只针对某一侧打桩，且配置解析与实例化两个阶段的责任边界更清晰。

---

### 4.3 RESP（Redis/Valkey）适配器：委托给原生连接器

#### 4.3.1 概念说明

 RESP 适配器是「**薄配置 + 委托**」流派的代表。它的特点是：真正的 I/O 由一个用 C++ 写的高性能 Redis 连接器完成（位于 `csrc/storage_backends/redis/`，通过 pybind 暴露为 `lmcache.lmcache_redis.LMCacheRedisClient`），Python 侧只负责「把配置变成连接器、再包成 `L2AdapterInterface`」。

之所以走 C++ 而不是用纯 Python Redis 客户端，是因为 Redis 作为 L2 时吞吐极高（内存型、微秒级 RTT），Python 的 GIL 与序列化开销会成为瓶颈；并且同一份 C++ 连接器既能在 MP 模式下经 `NativeConnectorL2Adapter` 当 L2 适配器用，也能在非 MP 模式下经 `ConnectorClientBase` 直接用——「一次实现，两种用法」（设计文档 [overall.md](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/docs/design/v1/distributed/l2_adapters/overall.md) 的「Native (C++/Rust) Storage Backends」一节）。

#### 4.3.2 核心流程

```text
RESPL2AdapterConfig(host, port, num_workers, ...)   # 薄配置
        │
        ▼  _create_resp_l2_adapter（工厂）
LMCacheRedisClient(host, port, num_workers, ...)    # C++ 连接器
        │
        ▼  包一层
NativeConnectorL2Adapter(native_client)             # 实现 L2AdapterInterface
```

`NativeConnectorL2Adapter` 的关键职责（见其模块头注释 [native_connector_l2_adapter.py:L1-L18](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/l2_adapters/native_connector_l2_adapter.py#L1-L18)）是把 C++ 连接器「单 event fd」的完成通知，**解复用（demux）**成契约要求的「三个 event fd」，并维护客户端 refcount 锁。

#### 4.3.3 源码精读

**配置类**——[resp_l2_adapter.py:L37-L95](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/l2_adapters/resp_l2_adapter.py#L37-L95) 的 `RESPL2AdapterConfig`。字段非常少：`host`、`port`、`num_workers`（C++ I/O 线程数，默认 8）、可选的 `username` / `password`、以及 `max_capacity_gb`。`from_dict` 对每个字段做严格的类型与正值校验（`host` 非空字符串、`port`/`num_workers` 正整数），符合项目 [coding_standards.md](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/docs/coding_standards.md)「用 `if/raise ValueError` 做运行时校验，不用 `assert`」的约定。

**工厂**——[resp_l2_adapter.py:L123-L171](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/l2_adapters/resp_l2_adapter.py#L123-L171) 的 `_create_resp_l2_adapter`。三件事值得注意：

1. **缺失依赖时报人话**：导入 `LMCacheRedisClient` 失败时，抛出 `RuntimeError` 并提示 `Build with: pip install -e .`（[resp_l2_adapter.py:L129-L138](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/l2_adapters/resp_l2_adapter.py#L129-L138)），而不是让用户面对裸 `ImportError`。
2. **配置/CLI 优先于环境变量**：`host = config.host or os.environ.get("LMCACHE_RESP_HOST", "")`（[resp_l2_adapter.py:L151-L154](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/l2_adapters/resp_l2_adapter.py#L151-L154)）。环境变量只是「兜底默认」，且是「创建适配器时读取、不写回配置对象」——这让密码这类敏感信息不必落到会被记录的配置里。
3. **构造连接器再包一层**：`LMCacheRedisClient(...)` → `NativeConnectorL2Adapter(native_client, max_capacity_gb=...)`。

**自注册**——[resp_l2_adapter.py:L175-L176](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/l2_adapters/resp_l2_adapter.py#L175-L176) 在模块底部把 `"resp"` 同时登记进配置类表和工厂表。这正是 4.2 说的「定义即注册」。

**委托如何工作**——以 store 为例，[native_connector_l2_adapter.py:L189-L212](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/l2_adapters/native_connector_l2_adapter.py#L189-L212) 的 `submit_store_task` 把每个 `ObjectKey` 序列化成连接器的线缆格式（`_object_key_to_string`，[native_connector_l2_adapter.py:L51-L68](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/l2_adapters/native_connector_l2_adapter.py#L51-L68)，用 `@` 分隔各字段），把 `MemoryObj` 抽成 `memoryview`，然后调用 `self._client.submit_batch_set(...)`——一个非阻塞的 C++ 调用，返回 `future_id`。这个 `future_id` 连同 op 类型被记进 `_pending_ops`，等后台 demux 线程把完成事件派发回来。

#### 4.3.4 代码实践

**实践目标**：写出一段让 LMCache 使用 Redis 作为 L2 的配置，并验证它能被配置注册表正确解析。

**操作步骤**：

1. 命令行形态（这是 MP coordinator / server 接收 L2 配置的方式，见 [config.py:L398-L437](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/l2_adapters/config.py#L398-L437) 的 `--l2-adapter`）：
   ```bash
   --l2-adapter '{"type":"resp","host":"localhost","port":6399,"num_workers":8}'
   ```
   想用密码且不想把明文写进命令行，可省略 `username`/`password`，改用环境变量兜底：
   ```bash
   LMCACHE_RESP_PASSWORD=secret --l2-adapter '{"type":"resp","host":"localhost","port":6399}'
   ```
2. 程序内形态（直接构造配置对象，便于单测）：
   ```python
   from lmcache.v1.distributed.l2_adapters.resp_l2_adapter import RESPL2AdapterConfig
   cfg = RESPL2AdapterConfig.from_dict(
       {"type": "resp", "host": "localhost", "port": 6399, "num_workers": 4}
   )
   print(cfg.host, cfg.port, cfg.num_workers)  # localhost 6399 4
   ```
3. 想启用「按总量淘汰」，加 `max_capacity_gb`（否则 `usage_fraction` 恒为 `-1.0`，不参与全局淘汰）：
   ```json
   {"type":"resp","host":"localhost","port":6399,"max_capacity_gb":10}
   ```

**需要观察的现象**：

- 步骤 2 应打印 `localhost 6399 4`，证明 `from_dict` 正确解析。
- 故意传一个非法值（如 `"port": -1`）应抛 `ValueError("port must be a positive integer")`，对应 [resp_l2_adapter.py:L73-L75](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/l2_adapters/resp_l2_adapter.py#L73-L75)。

**预期结果**：配置对象成功构造，字段与传入一致；非法值被拒。是否真正连通 Redis 取决于本机是否有 Redis 与 C++ 扩展——这一步只验证「配置解析」与「工厂能被找到」，不验证连通性（连通性见 4.1.4 的集成测试）。若无法运行 Python，标记「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：RESP 适配器为什么没有像 S3 那样自己实现 `submit_store_task` 的 I/O 细节？

> **参考答案**：因为 RESP 把 I/O 委托给了 C++ 连接器 `LMCacheRedisClient`，Python 侧通过通用的 `NativeConnectorL2Adapter` 桥接。这样新增一个 C++ 原生后端（如 RDMA、Mooncake）时，可以复用同一个桥接层，只需提供一个新的 `native_client`，不必每个后端都重写一遍 Python 异步逻辑。

**练习 2**：`LMCACHE_RESP_PASSWORD` 这类环境变量为什么「读后不写回配置对象」？

> **参考答案**：配置对象常被日志、status 上报、序列化等机制打印或落盘。密码若进了配置对象，就有泄漏风险。让环境变量只在「创建适配器时」被读取并直接交给连接器，配置对象本身保持无敏感信息，是把秘密挡在日志之外的常见做法。

---

### 4.4 S3 适配器：纯 Python 异步实现

#### 4.4.1 概念说明

S3 适配器是「**全自研**」流派的代表，与 RESP 形成鲜明对比。它没有可复用的 C++ 连接器，而是直接用 AWS 的 C 运行时 Python 绑定 `awscrt`（`s3.S3Request` / `s3.S3Client`）自己拼出 HEAD / GET / PUT / DELETE / ListObjectsV2 请求，在一个专用后台线程的 asyncio 事件循环里并发执行。

为什么 S3 要单独讲？因为它是「在远端、高延迟、可能失败」的典型：S3 的 RTT 是毫秒级（比 Redis 高 2–3 个数量级），还可能遇到限流、网络抖动。因此 S3 适配器额外实现了两样 RESP 不需要的东西：

- **熔断器（circuit breaker）**：连续失败到阈值后「断开连接」，后续 `submit_*` 直接快速失败（返回失败结果并立刻 notify event fd），不再徒劳地打 S3。
- **HEAD 结果缓存**：lookup 用 `HEAD` 探测对象是否存在并取 `Content-Length`，这个尺寸会被缓存，避免对同一 key 重复 HEAD。

#### 4.4.2 核心流程

S3 适配器的并发模型（见类 docstring [s3_l2_adapter.py:L424-L440](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/l2_adapters/s3_l2_adapter.py#L424-L440)）：**一个跑在专用守护线程里的 asyncio 事件循环**。每次 `submit_*` 用 `asyncio.run_coroutine_threadsafe` 把一个协程调度进那个循环；协程内部用 `asyncio.gather` 并发发起多个 `s3.S3Request`，全部完成后 notify 对应的 event fd。

```text
submit_store_task(keys, objs) -> task_id
        │  run_coroutine_threadsafe(_execute_store, self._loop)
        ▼
_execute_store 协程（在事件循环线程）：
  for 每个 (key, obj):  并发 PUT（_put_request）
  await asyncio.gather(...)
  把结果写进 _completed_store_tasks[task_id]
  _store_efd.notify()                  ◀── 唤醒控制器
```

`lookup` 用 HEAD、`load` 用 GET，模式完全相同，只是请求类型与「写入调用方缓冲」的方式不同。

#### 4.4.3 源码精读

**初始化**——[s3_l2_adapter.py:L445-L545](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/l2_adapters/s3_l2_adapter.py#L445-L545) 的 `__init__`。几件事：

- 第一行 `super().__init__(max_capacity_bytes=int(config.max_capacity_gb * (1024**3)))`（[s3_l2_adapter.py:L446](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/l2_adapters/s3_l2_adapter.py#L446)）把 GB 换算成字节传给基类，决定了 `supports_global_eviction` 与 `get_usage` 的行为。
- 建 `awscrt` 的 `EventLoopGroup` / `ClientBootstrap` / `S3Client`，配置 SigV4 签名、TLS、ALPN（HTTP/2 协商）。
- 建三个独立的 event notifier（[s3_l2_adapter.py:L496-L499](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/l2_adapters/s3_l2_adapter.py#L496-L499)）。
- 起一个守护线程跑 `asyncio` 事件循环（[s3_l2_adapter.py:L526-L532](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/l2_adapters/s3_l2_adapter.py#L526-L532)）。

**store 主路径**——`submit_store_task`（[s3_l2_adapter.py:L564-L586](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/l2_adapters/s3_l2_adapter.py#L564-L586)）只做「分配 task_id + 调度协程」，真正的活儿在 `_execute_store`（[s3_l2_adapter.py:L1039-L1110](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/l2_adapters/s3_l2_adapter.py#L1039-L1110））：并发 PUT、`asyncio.gather`、统计**净新增** key 的尺寸（同一 `chunk_hash` 重存是相同内容，跳过重报以防基类双重计数，[s3_l2_adapter.py:L1070-L1098](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/l2_adapters/s3_l2_adapter.py#L1070-L1098)）、用 `obj.get_size()`（逻辑尺寸而非 `get_physical_size()`，避免对齐填充虚增用量触发过早淘汰）、`_notify_keys_stored`、写 `_completed_store_tasks[task_id] = L2StoreResult(success, bytes_transferred)`、notify。

**PUT/GET/HEAD 的关键差异**——这是本讲实践任务要求对比的重点：

- `_put_request`（[s3_l2_adapter.py:L896-L925](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/l2_adapters/s3_l2_adapter.py#L896-L925）：把 `MemoryObj` 的 `byte_array` 包成 `MemoryViewStream` 作为请求 body（`PUT_OBJECT`），上传字节流；`on_done` 校验状态码 200/201。
- `_get_request`（[s3_l2_adapter.py:L870-L894](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/l2_adapters/s3_l2_adapter.py#L870-L894）：`on_body(chunk, offset)` 回调里用 `ctypes.memmove(data_ptr + offset, chunk, len(chunk))` **直接把响应字节拷进调用方 MemoryObj 的裸指针**——零中间 buffer。这是「调用方提供写缓冲」契约的具体兑现。
- `_head_request`（[s3_l2_adapter.py:L846-L868](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/l2_adapters/s3_l2_adapter.py#L846-L868）：不取 body，只从响应头读 `Content-Length`，用来判定存在性并缓存尺寸。

对比之下，RESP 侧的「get/put」是 `native_client.submit_batch_set` / `submit_batch_exists` / `submit_batch_get` 这类 C++ 非阻塞调用（见 [native_connector_l2_adapter.py:L189-L212](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/l2_adapters/native_connector_l2_adapter.py#L189-L212)），Python 侧看不到任何 HTTP/字节搬运细节。这就是「薄委托」与「全自研」的核心差异。

**客户端锁与淘汰**——`submit_unlock` 与 `delete` 都基于客户端 refcount 字典 `_locked_keys`。`delete`（[s3_l2_adapter.py:L670-L694](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/l2_adapters/s3_l2_adapter.py#L670-L694)）会**跳过任何 refcount > 0 的 key**（正在被并发 load 读取），防止「一边读一边删」。

**自注册**——[s3_l2_adapter.py:L1286-L1296](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/l2_adapters/s3_l2_adapter.py#L1286-L1296)，与 RESP 同样的「定义即注册」。

#### 4.4.4 代码实践

**实践目标**：对比 Redis(resp) 与 S3 两个适配器在 get/put 实现要点上的差异，并写出 S3 的 L2 配置。

**操作步骤**：

1. 对照阅读两段 store 路径：
   - RESP：[native_connector_l2_adapter.py:L189-L212](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/l2_adapters/native_connector_l2_adapter.py#L189-L212)（委托 C++，一行 `submit_batch_set`）。
   - S3：[s3_l2_adapter.py:L896-L925](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/l2_adapters/s3_l2_adapter.py#L896-L925) 的 `_put_request` + [s3_l2_adapter.py:L1039-L1110](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/l2_adapters/s3_l2_adapter.py#L1039-L1110) 的 `_execute_store`（自己拼 HTTP PUT + asyncio.gather + 净增尺寸记账）。
2. 写出 S3 的 L2 配置（命令行 JSON 形态）：
   ```bash
   --l2-adapter '{"type":"s3","s3_endpoint":"mybucket.s3.us-east-2.amazonaws.com","s3_region":"us-east-2","s3_num_io_threads":64}'
   ```
   - 注意 `s3_endpoint` 必须是 **virtual-hosted 风格**（bucket 名是 host 的一部分），不支持 path-style（见 [s3_l2_adapter.py:L312-L316](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/l2_adapters/s3_l2_adapter.py#L312-L316)）。
   - 不传 `aws_access_key_id`/`aws_secret_access_key` 时，走 boto3 凭证解析链（环境变量 → profile → 容器/web-identity → IMDS），见 [s3_l2_adapter.py:L324-L327](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/l2_adapters/s3_l2_adapter.py#L324-L327)。
3. 整理一张对比表（见下方「预期结果」）。

**需要观察的现象**：

- RESP 的 put/get 在 Python 侧是「一行非阻塞 C++ 调用 + future_id」，看不到字节搬运；S3 的 put 是「构造 body stream + 发起 `S3Request` + 等 `finished_future`」，get 是「`on_body` 回调里 `ctypes.memmove` 直接写裸指针」。
- 两者都把 task 结果写进各自的 `_completed_*_tasks` 字典并 notify event fd，殊途同归地满足同一份契约。

**预期结果**（对比表）：

| 维度 | RESP（Redis/Valkey） | S3 |
| --- | --- | --- |
| 实现流派 | 薄配置 + 委托 C++ 连接器 | 纯 Python + asyncio + awscrt 全自研 |
| put 细节 | `native_client.submit_batch_set(key_strs, memviews)` | `s3.S3Request(PUT_OBJECT, body_stream=MemoryViewStream)` |
| get 细节 | C++ `submit_batch_get`，字节由 C++ 写入缓冲 | `on_body` 回调 `ctypes.memmove` 直写 MemoryObj 裸指针 |
| lookup 探测 | C++ `submit_batch_exists` | HTTP `HEAD`，缓存 `Content-Length` |
| 线程模型 | C++ I/O 线程池（`num_workers`）+ Python demux 线程 | 单守护线程跑 asyncio 事件循环 |
| 容错特色 | 委托给连接器内部重试 | 客户端熔断器 + 连接错误识别 |
| 典型延迟 | 微秒级（内存型） | 毫秒级（对象存储） |

是否真正连通 S3 取决于凭证与网络；本步只验证配置能被解析（程序内 `S3L2AdapterConfig.from_dict(...)` 应成功，非法 endpoint 抛 `ValueError`）。若无法运行，标记「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：S3 的 `_get_request` 为什么用 `ctypes.memmove(data_ptr + offset, chunk, ...)` 直接写裸指针，而不是先收齐再拷？

> **参考答案**：调用方（`PrefetchController`）已经预分配好了 L1 写缓冲（`MemoryObj`），适配器的职责是把数据「写进那个缓冲」。`awscrt` 的 `on_body` 是流式回调，每个 chunk 带一个 `offset`，直接 `memmove` 到对应位置既省了一次中间 buffer，又能让大对象的下载边到边写，降低峰值内存。

**练习 2**：`S3L2AdapterConfig` 为什么把 `s3_endpoint` 设计成 virtual-hosted 风格而不支持 path-style？

> **参考答案**：virtual-hosted 风格下 bucket 名是 Host 头的一部分，请求的 SigV4 签名和路由都基于这个 Host 头计算；path-style 把 bucket 放在 URL 路径里，签名与路由方式不同。S3 适配器统一按 virtual-hosted 来签名和路由（见 docstring [s3_l2_adapter.py:L312-L316](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/l2_adapters/s3_l2_adapter.py#L312-L316)），只支持一种风格能让签名逻辑保持单一。

---

### 4.5 各类后端的差异与适用场景（速览）

上面两个适配器代表了两大流派。本目录下其他适配器也都遵循同一份契约，只是底层传输与适用场景不同。这里给一张选型速览（基于各文件名与设计文档目录 `docs/design/v1/distributed/l2_adapters/` 下的同名文档）：

| 适配器 | 底层 | 典型场景 | 备注 |
| --- | --- | --- | --- |
| `resp` | Redis/Valkey（C++ 连接器） | 低延迟、跨实例共享的热 L2 | 委托 `NativeConnectorL2Adapter` |
| `s3` | 对象存储（awscrt） | 跨可用区、大容量、可持久化的冷 L2 | 全自研，带熔断器 |
| `fs` / `fs_native` | 本地磁盘文件 | 单机落盘、断电恢复 | `fs` 假设磁盘无限，`supports_global_eviction=False` |
| `mooncake_store` | Mooncake（内存/RDMA） | 高性能跨节点 KV | RDMA 模式需注册 L1 内存 |
| `nixl_store` / `nixl_store_dynamic` | NIXL | GPU 显存直传、PD 分离 | 需单一 L1 内存区，见 [u4-l7](u4-l7-pd-disaggregation-transfer.md) |
| `p2p` | 对端 cache server | 多实例间点对点复用 | 会转发 `layout_desc` 给对端 |
| `dax` / `hfbucket` / `aerospike` | 持久内存 / HF Bucket / Aerospike | 特定硬件或既有存储 | 见各自设计文档 |
| `raw_block` | 裸块设备 | 本地高性能落盘 | 设计文档 `raw_block.md` |
| `plugin` | 用户自定义 | 二次开发接缝 | 设计文档 `plugin.md` |
| `mock` / `fault_inject` | 内存 / 故障注入 | 测试与混沌实验 | 参考实现 |

所有这些都靠 4.2 的「两张注册表 + 惰性发现」接进系统，新增任何一个都只需新建一个 `*_l2_adapter.py` 文件并在底部自注册。

此外还有两个「跨切」的扩展点（本讲只点名，细节留给后续）：

- **运行时重配置**：[reconfiguration.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/l2_adapters/reconfiguration.py) 定义了 `L2ReconfigurableAdapter` 协议；`StorageManager` 暴露 `add_l2_adapter` / `delete_l2_adapter` / `reconfigure_l2_adapter`（[storage_manager.py:L871](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/storage_manager.py#L871) 附近），可以在不重启的前提下热增删/调整 L2 后端。
- **SERDE 包装**：任何适配器配了 `serde_config` 后，会被 `SerdeL2AdapterWrapper` 包一层（[storage_manager.py:L1067-L1072](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/storage_manager.py#L1067-L1072)），在 store/load 边界透明地做量化/压缩——这是与 [u4-l4 SERDE 变换](u4-l4-serde-transforms.md) 的接缝。

---

## 5. 综合实践

**任务**：假设你要为一个多实例 vLLM 部署设计 L2 层——既要低延迟的「热共享」（实例 A 算完的 KV，实例 B 立刻能用），又要一份「冷备份」（跨可用区、能扛住实例全宕）。请用本讲学到的知识完成下面三件事，并把结论写成一份简短选型说明。

1. **选两个适配器组合成两级 L2**（`--l2-adapter` 可重复指定，顺序即适配器顺序，见 [config.py:L418-L436](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/l2_adapters/config.py#L418-L436)）。给出两条 `--l2-adapter` JSON，并说明哪条在前、为什么。
2. **为 Redis 那级启用按总量淘汰**：加 `max_capacity_gb`，并预测当用量超过水位时，`StorageManager._should_enable_l2_eviction`（[storage_manager.py:L1076-L1102](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/storage_manager.py#L1076-L1102)）会如何判定。
3. **画出一次 `PrefetchController` 预取在这些适配器上的事件流**：`submit_lookup_and_lock_task` 被发给「每一个」适配器 → 各自 lookup event fd 被 notify → `query_lookup_and_lock_result` 拿到两个 Bitmap → `PrefetchPolicy.select_load_plan` 把每个 key 分配给「最低索引、且命中」的那个适配器（见 [overall.md](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/docs/design/v1/distributed/l2_adapters/overall.md) 的 `DefaultPrefetchPolicy`）。

**参考思路**：

- 热级用 `resp`（Redis，低延迟），冷级用 `s3`（跨 AZ 持久化）。`resp` 在前：`DefaultPrefetchPolicy` 是「最低索引优先」，热级在前能优先命中低延迟层；`DefaultStorePolicy` 是「所有 key 发给所有适配器」，所以两级都会被写入（热级做共享、冷级做备份）。
- 给 `resp` 加 `"max_capacity_gb": 20`。判定路径：`supports_global_eviction` 为 `True`（因为 `max_capacity_bytes > 0`），若再配 `eviction_policy != "noop"` 就会挂上全局淘汰；`s3` 若不设 `max_capacity_gb`，即使配了 eviction 也会被 `_should_enable_l2_eviction` 跳过并打告警（[storage_manager.py:L1093-L1101](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/storage_manager.py#L1093-L1101)）。
- 事件流图应体现「lookup 广播给所有适配器、load 只发给被选中的那个」、以及「三个 event fd 在控制器侧各自独立 poll」。

> 说明：本实践是「源码阅读 + 配置设计」型，不需要真实 Redis/S3 即可完成选型与配置编写；若要端到端验证命中行为，需按 4.1.4 起真实 Redis 并配合 [u4-l8 测试与基准](u4-l8-testing-and-benchmarking.md) 的端到端脚本。

## 6. 本讲小结

- `L2AdapterInterface` 是一份面向「批量、非阻塞、调用方管内存」的统一契约：三组原语（store / lookup_and_lock / load）都走「submit → poll event fd → query/pop 结果」，且**每个适配器必须暴露三个互不相同的 event fd**，否则控制器的 fd→adapter 派发表会被静悄悄地误导。
- 基类内置了**字节账本**：子类只需在存/删时调 `_notify_keys_stored`/`_notify_keys_deleted` 报尺寸，`get_usage()` 就返回只读 `AdapterUsage` 快照；`max_capacity_bytes` 决定 `supports_global_eviction` 与 `usage_fraction` 是否给出有效淘汰信号。
- 适配器接入靠「**两张注册表（配置类 + 工厂）+ pkgutil 惰性自动发现**」：新增一个 `*_l2_adapter.py` 并在底部自注册即可，零改动既有文件；缺失第三方依赖的模块会被静默跳过，实现依赖隔离。
- RESP（Redis/Valkey）是「**薄配置 + 委托 C++ 连接器**」流派：`_create_resp_l2_adapter` 构造 `LMCacheRedisClient` 再包成 `NativeConnectorL2Adapter`，Python 侧看不到 I/O 细节；配置/CLI 优先于 `LMCACHE_RESP_*` 环境变量，且环境变量读后不写回配置对象以保护敏感信息。
- S3 是「**纯 Python + asyncio + awscrt 全自研**」流派：单守护线程跑事件循环，`_execute_*` 协程用 `asyncio.gather` 并发发起 `S3Request`；GET 用 `on_body`+`ctypes.memmove` 直写调用方缓冲，HEAD 缓存 `Content-Length`，并带客户端 refcount 锁与熔断器。
- 所有远端后端（resp/s3/fs/mooncake/nixl/p2p/dax/…）共享同一契约与发现机制；此外还有运行时重配置（`L2ReconfigurableAdapter` + `add/delete/reconfigure_l2_adapter`）与 SERDE 包装（`SerdeL2AdapterWrapper`）两个跨切扩展点。

## 7. 下一步学习建议

- **SERDE 变换**：本讲多次提到 `serde_config` 与 `SerdeL2AdapterWrapper`，下一篇 [u4-l4 SERDE 变换与压缩](u4-l4-serde-transforms.md) 会展开 `distributed/serde/` 的可插拔量化/压缩接口（fp8、turboquant、multi、asym_k16_v8）。
- **淘汰与配额**：本讲的 `supports_global_eviction`、`AdapterUsage`、`max_capacity_gb` 都是为淘汰控制器铺路；想看它们如何被消费，读 [u4-l5 淘汰策略与配额管理](u4-l5-eviction-and-quota.md) 与 `distributed/eviction_policy/`、`quota_manager.py`。
- **PD 分离与 NIXL 传输**：`nixl_store_*` 与 `pd_backend` 这类适配器是 PD 分离的关键，结合 [u4-l7 PD 分离与传输通道](u4-l7-pd-disaggregation-transfer.md) 一起读，并参考设计文档 `docs/design/v1/distributed/l2_adapters/nixl_store.md`。
- **二次开发**：想自己写一个适配器，直接照 `resp_l2_adapter.py`（薄配置）或 `s3_l2_adapter.py`（全自研）的骨架，遵循设计文档 [overall.md](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/docs/design/v1/distributed/l2_adapters/overall.md) 的「Implementing a New L2 Adapter」一节，配合 `plugin.md` 的插件机制。
