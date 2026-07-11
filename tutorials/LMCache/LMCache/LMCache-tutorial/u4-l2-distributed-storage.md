# 分布式存储架构（L1/L2）

## 1. 本讲目标

本讲进入 LMCache 的「专家层存储」核心：`lmcache/v1/distributed/`。它是新多进程（MP）架构下 cache server daemon 真正的存储大脑，和 u2-l3/u2-l4 讲过的单机 `v1/storage_backend/` 是两套并存的存储实现。

读完本讲，你应当能够：

- 说清 `v1/distributed/` 与 legacy `v1/storage_backend/` 的职责差别，知道为什么会有两套。
- 画出 **L1（本地热缓存）/ L2（远端持久/共享存储）** 的两级模型，并解释数据在两级之间如何流动。
- 复述 `StorageManager` 作为「门面」对外暴露的写/读/预取三类 API。
- 列出 `storage_controllers/` 下 store / prefetch / eviction 三类后台控制器各自的分工。
- 读懂 `ObjectKey` / `EncodedObjectKey` 如何唯一标识一个 KV 对象，以及 `Tier` 枚举如何成为跨进程的「层级词汇」。

## 2. 前置知识

本讲默认你已经学完（至少浏览过）以下讲义：

- **u1-l3 代码目录结构**：知道 `v1/` 是新架构主战场，`storage_backend/`（legacy）只剩 `serde/`。
- **u2-l3 存储后端层次结构**：理解 `StorageBackendInterface`「按 CacheEngineKey 存取 MemoryObj 的字典」契约，以及 `LocalCPUBackend / LocalDiskBackend / RemoteBackend` 的写穿 + 读提升模型。
- **u2-l4 StorageManager 与异步序列化**：理解单机 `StorageManager` 的写穿 fan-out put、读提升 read promote、以及 `WeightedSemaphore` 的资源控制。
- **u3-l1 多进程架构总览**（建议）：知道 MP 架构把 cache 管理拆成独立 daemon，本讲的 `StorageManager` 就运行在这个 daemon 进程里。

几个关键术语回顾：

- **KV cache / MemoryObj**：模型推理时 attention 层缓存的 Key/Value 张量；在 distributed 体系里被包装成 `MemoryObj`（一块连续的 CPU/GPU 内存）。
- **chunk**：按固定 token 数（`chunk_size`）切分的一段 token 序列；一个 chunk 的 KV 对应一个对象。
- **prefill / decode**：推理的两个阶段，prefill 阶段算出 KV，decode 阶段复用并追加。
- **命中（hit）/ 预取（prefetch）**：lookup 是「问在不在」，retrieve/prefetch 是「把数据搬回来」。

一个贯穿全讲的直觉：**单机存储是「一块内存 + 几个后端」，分布式存储是「一个内存池 + 一组异步 I/O 适配器 + 一组后台控制器线程」**。前者同步、fan-out；后者把所有 I/O 都异步化，用 eventfd + 后台线程把 GPU 关键路径和慢速远端存储彻底解耦。

## 3. 本讲源码地图

本讲涉及的关键文件（都在 `lmcache/v1/distributed/` 下）：

| 文件 | 作用 | 行数级别 |
|------|------|---------|
| `api.py` | 定义全分布式体系共享的数据结构：`ObjectKey`、`EncodedObjectKey`、`MemoryLayoutDesc`、`PrefetchHandle`、`TrimPolicy` 等 | 中 |
| `tiers.py` | 定义 `Tier` 枚举（L1 / L2 / ALL），作为 server 与 coordinator 之间的「层级词汇」 | 极小 |
| `storage_controller.py` | 定义 `StorageControllerInterface` 抽象基类（start/stop/report_status） | 极小 |
| `storage_manager.py` | **核心门面类 `StorageManager`**，编排 L1、L2 适配器与三类后台控制器，对外暴露写/读/预取 API | 大（~1100 行）|
| `l1_manager.py` | L1 对象生命周期状态机（read/write 锁、TTL、淘汰触发）| 大 |
| `storage_controllers/store_controller.py` | 后台线程：L1 写完 → 异步存 L2 | 大 |
| `storage_controllers/prefetch_controller.py` | 后台线程：异步从 L2 查+加载到 L1 | 大 |
| `storage_controllers/eviction_controller.py` | L1/L2 淘汰控制器（水位触发）| 中 |
| `l2_adapters/base.py` | `L2AdapterInterface` 契约：store / lookup_and_lock / load 的非阻塞原语 | 中 |
| `config.py` | `StorageManagerConfig` 等配置 dataclass，以及 CLI 参数解析 | 大 |
| `quota_manager.py` | 按 `cache_salt` 的配额注册表，供 L2 淘汰与 HTTP 端 CRUD 使用 | 小 |
| `internal_api.py` | 内部数据结构：listener 接口、`L2StoreResult`、`EvictionAction` 等 | 小 |

设计文档（读代码前先读，遵循 docs/design 镜像约定）：

- `docs/design/v1/distributed/l2_adapters/overall.md`：StoreController / PrefetchController 与 L2 适配器的总体设计，含完整数据流与时序图，本讲的「权威参考」。

## 4. 核心概念与源码讲解

### 4.1 distributed/ 与 legacy storage_backend/：为什么有两套

#### 4.1.1 概念说明

你在 u2-l3 学到的 `v1/storage_backend/` 是**单机、同步、进程内**的存储：一个 `StorageManager` 持有一组 `OrderedDict` 的后端，写穿 fan-out、读提升，淘汰各层独立。它的 `LocalCPUBackend` 既是热缓存又是全局内存分配器。

`v1/distributed/` 是为**多进程（MP）架构**重新设计的存储体系，它解决了单机存储在 MP 场景下的几个根本矛盾：

1. **同步 I/O 会阻塞 GPU 热路径**。把 KV 异步写到 Redis/S3/NVMe 时，绝不能让 vLLM 的 forward 等待。
2. **L2 后端种类繁多且不可靠**。Redis、S3、Mooncake、NIXL、文件系统……它们的延迟、可靠性、并发模型天差地别，需要一个统一的「非阻塞 I/O 抽象」。
3. **需要舰队级共享与隔离**。多个 cache server 实例要共享同一份远端 KV，还要按用户（`cache_salt`）做配额隔离。
4. **对象需要被独立编址**。单机用 `CacheEngineKey`（一个对象），分布式要把「一个 chunk 的 KV」抽象成可序列化、可跨进程传递的 `ObjectKey`。

注意：legacy `lmcache/storage_backend/` 目录现在**只剩 `serde/`**（见 u1-l3），主存储已经全面迁移到 `v1/distributed/`。本讲的 `StorageManager`（`v1/distributed/storage_manager.py`）**不是** u2-l4 的单机 `StorageManager`（`v1/storage_backend/storage_manager.py`）——两者同名但分属不同模块，服务于不同的运行模式。

#### 4.1.2 核心流程

distributed 存储的总体结构（摘自设计文档）：

```
                    ┌────────────────────────┐
                    │    StorageManager       │   ← 门面：对外 API
                    │  submit_prefetch_task   │
                    │  reserve/finish_write   │
                    └────┬──────────┬─────────┘
                         │          │
              ┌──────────┘          └──────────┐
              ▼                                ▼
   ┌────────────────────┐           ┌────────────────────┐
   │  StoreController   │           │ PrefetchController  │  ← 后台线程
   │  L1 写完 → 存 L2   │           │  查 L2 → 加载到 L1  │
   └────┬───────────────┘           └──────┬─────────────┘
        │                                  │
        ▼                                  ▼
   ┌─────────────────────────────────────────────┐
   │           L2AdapterInterface(s)             │  ← 非阻塞 I/O
   │   store / lookup_and_lock / load / unlock   │
   └─────────────────────────────────────────────┘
        ▲                                  ▲
        │          （读写都在 L1 落脚）      │
   ┌────┴──────────────────────────────────┴────┐
   │              L1Manager（内存池）            │  ← 对象状态机
   │   reserve_write / reserve_read / finish_*  │
   └─────────────────────────────────────────────┘
```

关键区别于单机存储的三点：

- **所有 L2 I/O 都是非阻塞的**：submit 任务 → 轮询 eventfd → 查询结果，绝不阻塞调用线程。
- **读写都在 L1 落脚**：写是「先写 L1，后台再异步搬 L2」；读是「先查 L1，缺的再后台从 L2 预取到 L1，最后从 L1 读」。
- **三类后台控制器各司其职**：store（L1→L2 复制）、prefetch（L2→L1 预取）、eviction（L1/L2 淘汰）。

#### 4.1.3 源码精读

确认 legacy 目录现状：`lmcache/storage_backend/` 现在确实只剩 `serde/`（见你的探查结果），而 distributed 目录下 L2 适配器已经有 20+ 种实现（`resp`/`s3`/`fs`/`dax`/`mooncake_store`/`nixl_store`/`p2p`/`aerospike`/`hfbucket`/`raw_block` 等），全部插同一个 `L2AdapterInterface` 契约。

distributed 体系的总入口 `StorageManager` 被多处实例化，最重要的两处：MP server daemon 的引擎上下文 [lmcache/v1/multiprocess/engine_context.py:196](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/engine_context.py#L196)，以及非 MP 的 EC 引擎 [lmcache/v1/ec_engine.py:90](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/ec_engine.py#L90)。这说明同一套 distributed 存储既服务 MP daemon，也服务单进程 EC 引擎——这正是「厂商中立、引擎无关」的体现。

#### 4.1.4 代码实践

**实践目标**：建立「两套存储并存」的直观印象。

**操作步骤**：

1. 列出两个存储目录的内容，对比体量：
   ```bash
   ls lmcache/storage_backend/          # legacy：应只见 serde/
   ls lmcache/v1/storage_backend/        # 单机新存储（u2-l3/u2-l4 主角）
   ls lmcache/v1/distributed/            # 本讲主角
   ```
2. 数一数 `lmcache/v1/distributed/l2_adapters/` 下有多少 `*_l2_adapter.py`，体会 L2 后端种类之多。

**需要观察的现象**：legacy 目录只剩 serde；distributed 目录下 L2 适配器超过 20 个。

**预期结果**：你会清楚看到「主存储已迁移到 distributed，legacy 仅留序列化」的现实。

**待本地验证**：具体文件数量请以本地 `ls` 输出为准。

#### 4.1.5 小练习与答案

**练习 1**：既然有了 distributed，为什么不直接删掉 `v1/storage_backend/`？

> 参考答案：两者服务不同运行模式。单机 `storage_backend/`（u2-l3/u2-l4）面向**进程内、同步、小规模**场景，接口更轻；distributed 面向 **MP daemon、异步 I/O、舰队共享**场景，抽象更重（对象状态机、后台控制器、配额）。在不需要远端共享的单进程部署里，单机存储仍是有效路径，因此两套并存。

**练习 2**：distributed 的 `StorageManager` 和单机的 `StorageManager` 同名，导入时如何区分？

> 参考答案：靠模块路径区分——`lmcache.v1.distributed.storage_manager.StorageManager` vs `lmcache.v1.storage_backend.storage_manager.StorageManager`。代码里一律用绝对导入，因此不会冲突。阅读时看 `from ... import` 的来源即可判断是哪一套。

### 4.2 对象标识与 Tier 词汇：api.py 与 tiers.py

#### 4.2.1 概念说明

要把一个 chunk 的 KV 缓存「搬」到远端、跨进程传递、按用户隔离，首先得有一个**全局唯一、可序列化的标识**。`api.py` 定义的 `ObjectKey` 就是这个标识，`EncodedObjectKey` 是它的 JSON 安全「线缆形态」。

`Tier` 枚举（`tiers.py`）则是更小但同样关键的一块：它定义了 `L1` / `L2` / `ALL` 三个层级常量，作为 cache server 与 coordinator 之间的请求参数词汇。把层级做成请求数据（`tier` / `source_tier` / `target_tier` 字段），而不是写死在 URL 路径里，可以让一套 API 表达「只在 L1 操作 / 只在 L2 操作 / 全层级操作」。

#### 4.2.2 核心流程

`ObjectKey` 的组成（见类定义）：

```
ObjectKey(frozen=True):
    chunk_hash: bytes        # 该 chunk 内容的哈希（内容寻址）
    model_name: str          # 模型名（不含 '@'）
    kv_rank: int             # 并行切片位（打包了 world_size / rank）
    object_group_id: int = 0 # 对象组索引（多对象组场景）
    cache_salt: str = ""     # 每用户/租户隔离盐
```

它的设计要点：

- **frozen dataclass**：不可变，可作 dict key、可哈希。
- **内容寻址**：`chunk_hash` 是内容哈希，相同内容（同模型、同 rank、同盐）→ 相同 key → 复用。
- **租户隔离**：`cache_salt` 不同则 key 不同，天然实现 per-user 隔离。
- **字段不变量**：`model_name` 不许含 `@`，`cache_salt` 不许含 `@/\<NUL>` 且长度 ≤ 128——因为 L2 适配器用 `@` 做序列化分隔符、用 `/` 做文件路径分隔符，这些约束保证序列化无歧义。

`kv_rank` 的打包方式（`ComputeKVRank`）：把 `world_size`、`global_rank`、`local_world_size`、`local_rank` 各用 8 bit 压进一个 int：

\[
\text{kv\_rank} = (\text{world\_size} \ll 24)\;|\;(\text{global\_rank} \ll 16)\;|\;(\text{local\_world\_size} \ll 8)\;|\;\text{local\_rank}
\]

这样一个 int 就编码了「哪个并行切片」，未来可扩展为 bitmap 形式以支持跨不同 TP/PP 设置的共享。

`ObjectKey` ↔ `EncodedObjectKey` 是一对互逆投影：`ObjectKey` 的 `chunk_hash` 是 `bytes`（不可直接 JSON 序列化），`EncodedObjectKey` 把它 hex 编码成 `str`，其余字段原样保留。跨进程/HTTP 传输时用 `EncodedObjectKey`，进入存储逻辑时用 `to_object_key()` 还原。

#### 4.2.3 源码精读

`Tier` 枚举（全文件就这一个类，是「中性词汇」单点真相）：

- [lmcache/v1/distributed/tiers.py:13-23](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/tiers.py#L13-L23)：定义 `Tier(str, Enum)`，继承 `str` 使得 `Tier.L2 == "l2"` 且 JSON 序列化就是裸字符串 `"l2"`；`ALL` 仅对显式支持多层的操作有效。

`ObjectKey` 定义与不变量校验：

- [lmcache/v1/distributed/api.py:56-118](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/api.py#L56-L118)：`ObjectKey` 的字段声明 + `__post_init__`，逐项校验 `model_name` 不含 `@`、`object_group_id >= 0`、`cache_salt` 不含禁用字符且不超长——失败即 `raise ValueError`（遵循 CLAUDE.md「不用 assert 做校验」）。
- [lmcache/v1/distributed/api.py:120-128](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/api.py#L120-L128)：`to_encoded_object_key()` 把 `chunk_hash` hex 化，产出 JSON 安全投影。
- [lmcache/v1/distributed/api.py:140-191](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/api.py#L140-L191)：`ComputeKVRank` 用位移打包并行参数，注释里给出了未来 bitmap 化的设想。
- [lmcache/v1/distributed/api.py:194-224](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/api.py#L194-L224)：`EncodedObjectKey` 的字段与 `to_object_key()` 还原（hex → bytes）。
- [lmcache/v1/distributed/api.py:336-404](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/api.py#L336-L404)：`ipc_key_to_object_keys` 把一个 IPC key + chunk 哈希列表展开成 per-object-group 的 `ObjectKey` 列表，注释强调 `cache_salt` 只从 `ipc_key` 读取（避免重复真源导致隔离 bug）。

#### 4.2.4 代码实践

**实践目标**：亲手构造 `ObjectKey`，体会内容寻址 + 租户隔离。

**操作步骤**：

写一个最小脚本（示例代码，非项目原有）：

```python
# 示例代码
from lmcache.v1.distributed.api import ObjectKey

k1 = ObjectKey(chunk_hash=b"\x01\x02", model_name="llama", kv_rank=0,
               object_group_id=0, cache_salt="userA")
k2 = ObjectKey(chunk_hash=b"\x01\x02", model_name="llama", kv_rank=0,
               object_group_id=0, cache_salt="userB")
print("同内容不同盐是否同 key:", k1 == k2)   # 预期 False（租户隔离）

enc = k1.to_encoded_object_key()
print("Encoded 形态:", enc)
print("还原一致:", enc.to_object_key() == k1)  # 预期 True

# 触发不变量校验
try:
    ObjectKey(chunk_hash=b"x", model_name="a@b", kv_rank=0)
except ValueError as e:
    print("拒绝非法 model_name:", e)
```

**需要观察的现象**：相同 chunk_hash 但 `cache_salt` 不同 → key 不等；`@` 被拒绝。

**预期结果**：打印 `False / True` 以及 ValueError 信息，验证内容寻址 + 租户隔离 + 字段校验。

**待本地验证**：该脚本未在项目内运行过，请在本地 Python 环境执行确认。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `cache_salt` 不能包含 `/`？

> 参考答案：FS（文件系统）类 L2 适配器会把 `cache_salt` 拼进文件名，`/` 是路径分隔符，会让 salt 跨越目录边界，既破坏隔离又可能越权读写。因此 `api.py` 的 `__post_init__` 显式禁用。

**练习 2**：`Tier` 为什么继承 `str` 而不是普通 `Enum`？

> 参考答案：继承 `str` 后 `Tier.L2 == "l2"` 成立，且 JSON 序列化直接输出裸字符串 `"l2"`。这样 server 与 coordinator 两端用字符串做线缆格式时无需额外转换，又能保留枚举的类型安全与可读性。

### 4.3 控制器抽象与三件套：storage_controller.py 与 storage_controllers/

#### 4.3.1 概念说明

distributed 存储把「搬运数据」的工作分给若干**后台控制器**：它们各自跑在独立线程里，按事件驱动循环工作。`storage_controller.py`（单数）定义所有控制器的公共抽象 `StorageControllerInterface`；`storage_controllers/`（复数，子包）则是具体实现。

三类核心控制器：

| 控制器 | 触发 | 方向 | 干什么 |
|--------|------|------|--------|
| **StoreController** | L1 写完成事件 | L1 → L2 | 异步把刚写进 L1 的对象复制到 L2 适配器，成功后可按策略删 L1 |
| **PrefetchController** | 外部 `submit_prefetch_request` | L2 → L1 | 异步查 L2 哪些 key 在、加锁、加载进 L1 写缓冲、转为读锁 |
| **EvictionController** | 周期心跳 / 水位 | L1 内 / L2 内 | 内存用量超 watermark 时按策略淘汰，分 L1 与 L2 两个子类 |

它们的共同点是：**不持有调用方的执行上下文**，而是通过事件（eventfd / listener 回调）被唤醒，在后台默默搬运。

#### 4.3.2 核心流程

`StorageControllerInterface` 只规定三件事：`start()`（启动后台线程）、`stop()`（停止并回收）、`report_status() -> dict`（必须含 `is_healthy: bool`）。这是一个极薄的契约——具体怎么干活由子类决定。

三类控制器的事件驱动循环骨架（详见 `l2_adapters/overall.md`）：

```
StoreController 后台循环：
  poll(StoreListener eventfd + 各 adapter store_efd)
  ├── StoreListener 唤醒：L1 刚 finish_write →
  │     按 (model_name, kv_rank) 分组 → 策略选目标 adapter →
  │     L1.reserve_read 拿对象 + 读锁 → adapter.submit_store_task
  └── store_efd 唤醒：adapter.pop_completed_store_tasks →
        释放 L1 读锁 → 按策略决定是否删 L1

PrefetchController 后台循环（每请求两阶段状态机）：
  LOOKUP 阶段：向所有 adapter 提交 lookup_and_lock_task
       → 等所有 lookup 完成，合并 bitmap
  PLAN_AND_LOAD 阶段：策略算 load plan → 修剪为连续前缀 →
       L1.reserve_write(is_temporary=True) 分配写缓冲 →
       adapter.submit_load_task → 加载完成 →
       finish_write_and_reserve_read（原子写锁→读锁）

EvictionController（L1/L2 各一）：
  周期心跳 → 取用量 vs watermark → 超阈则按 eviction_ratio 淘汰
```

**前缀加载不变量**（PrefetchController 的核心约束）：若 L2 有 key `{0,1,3,4}` 但缺 key `2`，则只加载 `{0,1}`。因为 vLLM 只能用连续前缀的 KV，加载 key 3、4 既浪费 I/O 又浪费 L1 内存。这一约束由 `build_trim_mask` + `TrimPolicy` 控制。

**TrimPolicy** 三种（定义在 `api.py`）：

- `PREFIX`：保留从 0 开始的最长连续段（默认）。
- `SEGMENTED_PREFIX`：保留 L2 命中但中途加载进 L1 失败时仍加载的 key（容忍空隙）。
- `SPARSE`：保留所有命中 key，用于有意散点复用。

#### 4.3.3 源码精读

控制器公共抽象：

- [lmcache/v1/distributed/storage_controller.py:13-39](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/storage_controller.py#L13-L39)：`StorageControllerInterface`，三个抽象方法 `start/stop/report_status`。注释点明控制器「能看到 L1 Manager 并对它操作」。

子包的自动发现（「定义即注册」模式，和 u4-l1 CLI 框架同构）：

- [lmcache/v1/distributed/storage_controllers/__init__.py:29-34](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/storage_controllers/__init__.py#L29-L34)：用 `pkgutil.iter_modules` 遍历包内所有模块并 `importlib.import_module`，让每个策略模块顶层的 `register_store_policy` / `register_prefetch_policy` 自调用——新增策略无需改任何已有文件。

StoreController：

- [lmcache/v1/distributed/storage_controllers/store_controller.py:196-217](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/storage_controllers/store_controller.py#L196-L217)：类 docstring 完整描述了它的 4 步循环。
- [lmcache/v1/distributed/storage_controllers/store_controller.py:70-133](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/storage_controllers/store_controller.py#L70-L133)：`StoreListener` 是注册到 L1Manager 的 listener，`on_l1_keys_write_finished` 在 L1 锁内被调用，**必须非阻塞**（只能 append + eventfd，不能调 L1Manager 方法，否则死锁）——这是一个关键的不变量。

PrefetchController：

- [lmcache/v1/distributed/storage_controllers/prefetch_controller.py:1-13](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/storage_controllers/prefetch_controller.py#L1-L13)：模块 docstring 列出 6 步循环（lookup → plan → reserve_write → load → 写锁转读锁 → 报告 bitmap）。
- [lmcache/v1/distributed/storage_controllers/prefetch_controller.py:73-96](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/storage_controllers/prefetch_controller.py#L73-L96)：`build_trim_mask` 实现「PREFIX 取连续前缀，其余策略全保留」的修剪逻辑。

EvictionController：

- [lmcache/v1/distributed/storage_controllers/eviction_controller.py:36-85](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/storage_controllers/eviction_controller.py#L36-L85)：`EvictionController` 抽象基类，提供共享的后台线程 + stop_flag 骨架；`L1EvictionController`（同文件 L88 起）注册 `L1EvictionPolicy` 监听 L1 事件以保持策略新鲜，周期触发淘汰。L2 淘汰由 `L2EvictionController` 负责（结合配额，见 4.5）。

#### 4.3.4 代码实践

**实践目标**：跟踪 StoreController「L1 写完 → 存 L2」的事件链，理解非阻塞 listener 不变量。

**操作步骤**（源码阅读型实践）：

1. 打开 `store_controller.py`，定位 `StoreListener.on_l1_keys_write_finished`（L121 起），确认它只做 `self._pending_keys.extend(keys)` + `self._event_fd.notify()`，**没有任何 L1Manager 调用**。
2. 在 `l1_manager.py` 找 `finish_write`（L534 起），看它末尾如何 `listener.on_l1_keys_write_finished(successful_keys)`——这一调用发生在 `@l1_mgr_synchronized` 装饰器包住的锁内。
3. 思考：如果 listener 在这里调用了 `l1_manager.reserve_read(...)`，会发生什么？

**需要观察的现象**：listener 回调发生在 `L1Manager._lock` 持有期间。

**预期结果**：你会得出结论——listener 调用任何会再次获取 `_lock` 的 L1Manager 方法都会**自死锁**；这就是为什么 listener 必须非阻塞、只入队 + 唤醒 eventfd，真正的 reserve_read 在控制器自己的后台线程里做。

#### 4.3.5 小练习与答案

**练习 1**：PrefetchController 为什么只加载「连续前缀」而不是所有命中 key？

> 参考答案：vLLM 只能用连续前缀的已计算 KV。若 L2 命中 `{0,1,3,4}` 但缺 `2`，加载 3、4 毫无用处，反而白费 I/O 带宽和 L1 内存。前缀修剪（`build_trim_mask` 的 PREFIX 分支用 `count_leading_ones`）保证只加载引擎真能用的那一段。`SPARSE` 策略是给散点复用（如 CacheBlend）开的口子。

**练习 2**：`StorageControllerInterface` 为什么不暴露 `evict_one()` 之类的方法？

> 参考答案：因为它只规定生命周期与健康报告的最小契约，具体「怎么淘汰/怎么搬运」是各子类的私有逻辑。这种「薄接口 + 厚实现」让 store/prefetch/eviction 三类控制器可以各自演化而不互相干扰，也方便新增第四类控制器。

### 4.4 StorageManager：两级编排门面

#### 4.4.1 概念说明

`StorageManager`（`storage_manager.py`）是 distributed 存储的**门面类**：它持有 L1、所有 L2 适配器、三类控制器，对外暴露面向 serving engine 的简单 API，把「先 L1 后 L2、异步搬运」的复杂度全部藏起来。

它的核心设计哲学：**读写都在 L1 落脚，L2 永远异步**。

- **写**：调用方 `reserve_write` 在 L1 分配缓冲并写入 → `finish_write` 标记完成 → StoreController 后台把数据搬到 L2。调用方全程只碰 L1，不等 L2。
- **读（预取）**：调用方 `submit_prefetch_task` → 先在 L1 查连续前缀命中（同步、立即返回）→ 缺的部分交给 PrefetchController 后台从 L2 拉 → 之后调用方 `query_prefetch_status` 轮询、`read_prefetched_results` 从 L1 读。

这把 L2 的慢速 I/O 彻底踢出 GPU 关键路径——engine forward 时 KV 要么已经在 L1，要么需要重算，绝不会卡在等 S3/Redis 上。

#### 4.4.2 核心流程

**写数据流**（reserve_write → finish_write）：

```
reserve_write(keys, layout_desc, mode)
  └── L1Manager.reserve_write(...)           # 分配 MemoryObj + 写锁
      返回 {key: MemoryObj}（部分 key 可能 OOM 失败）
      → 发 SM_WRITE_RESERVED 事件（含 succeeded/failed keys）

[调用方往 MemoryObj 里写 KV 数据]

finish_write(keys)
  └── L1Manager.finish_write(...)            # 解写锁，对象进入 ready 态
      → 发 SM_WRITE_FINISHED 事件
      → L1Manager 同时触发 listener.on_l1_keys_write_finished
         → StoreListener 入队 + 唤醒 eventfd
         → StoreController 后台：L1→L2 异步复制
```

**读/预取数据流**（submit_prefetch_task → query → read → finish）：

```
submit_prefetch_task(keys, layout_desc, policy, mode)
  ├── (LOOKUP 模式) L1Manager.reserve_read(keys)
  │     计算连续前缀命中数 hit_count
  │     前缀之后的 key 若被误加读锁 → finish_read 释放（避免悬挂锁）
  ├── 前缀命中 → 记入 handle.l1_found_indices（同步、立即）
  └── 剩余 keys[hit_count:] 非空且有 L2 适配器 →
        PrefetchController.submit_prefetch_request(剩余 keys, ...)
        返回 prefetch_request_id（记入 handle）
  返回 PrefetchHandle（含 L1 命中 + L2 请求 id）

query_prefetch_status(handle)
  ├── 若 prefetch_request_id == -1：纯 L1，直接合并返回
  └── 否则取 L2 结果 bitmap，与 L1 命中合并 →
        _combine_found：Bitmap(total).batched_set(l1_found_indices)
                        .batched_set(l2_local.gather(l2_orig_indices))
        popcount 得总命中数

read_prefetched_results(keys)  # context manager
  └── L1Manager.unsafe_read(keys)（不重新加锁，要求已 reserve_read）
      yield list[MemoryObj] | None（任一缺失则 yield None 并释放已读锁）

finish_read_prefetched(keys)
  └── L1Manager.finish_read(...)             # 释放读锁
```

**配额**：`StorageManager` 还持有一个 `QuotaManager`（按 `cache_salt` 的字节预算表），供 L2 淘汰控制器和 HTTP CRUD 端点共享。即使没有适配器用 `IsolatedLRU`，`QuotaManager` 也总是被创建，让 HTTP 层始终有一个稳定的引用。

#### 4.4.3 源码精读

`StorageManager` 的组装（构造期把所有零件搭起来）：

- [lmcache/v1/distributed/storage_manager.py:67-167](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/storage_manager.py#L67-L167)：`__init__` 依次建 `L1Manager`、`L1EvictionController`（立即 start）、L2 适配器集合（按 config 循环 `_build_l2_adapter`）、`QuotaManager`、`L2EvictionController`、`StoreController`、`PrefetchController`，最后注册 L2 用量 gauge。注释解释了「QuotaManager 总被创建」是为了给 HTTP 层稳定引用。

写路径：

- [lmcache/v1/distributed/storage_manager.py:170-225](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/storage_manager.py#L170-L225)：`reserve_write`，委托 `L1Manager.reserve_write`，过滤出成功的 key，并对 OOM 的 key 发 `L1_ALLOCATION_FAILED` 事件。
- [lmcache/v1/distributed/storage_manager.py:227-251](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/storage_manager.py#L227-L251)：`finish_write`，委托 `L1Manager.finish_write`，发 `SM_WRITE_FINISHED` 事件——这一步会间接触发 StoreController。

预取路径：

- [lmcache/v1/distributed/storage_manager.py:398-475](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/storage_manager.py#L398-L475)：`submit_prefetch_task` 的签名与 docstring（含 `TrimPolicy` / `PrefetchMode` 语义）。
- [lmcache/v1/distributed/storage_manager.py:509-591](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/storage_manager.py#L509-L591)：PREFIX 分支的主逻辑——数连续前缀命中、释放误加的读锁、把剩余 key 交给 PrefetchController、映射 `l2_orig_indices`。
- [lmcache/v1/distributed/storage_manager.py:593-609](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/storage_manager.py#L593-L609)：`_combine_found` 用 `Bitmap.gather` 把 L2 的局部 bitmap 映射回原始 key 位置——这是「L1 命中 + L2 命中」合并的关键。
- [lmcache/v1/distributed/storage_manager.py:676-721](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/storage_manager.py#L676-L721)：`query_prefetch_status`，合并 L1/L2 结果并用 `popcount`（而非 `count_leading_ones`）以兼容非连续策略。

L2 适配器构建（含 SERDE 透明包装）：

- [lmcache/v1/distributed/storage_manager.py:1050-1074](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/storage_manager.py#L1050-L1074)：`_build_l2_adapter`，若适配器 config 带 `serde_config`，就用 `SerdeL2AdapterWrapper` 把它包一层，让控制器看到的是「裸 L2 适配器」，编解码对控制器透明。

L1Manager 状态机（被 StorageManager 委托的核心对象）：

- [lmcache/v1/distributed/l1_manager.py:142-180](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/l1_manager.py#L142-L180)：`L1Manager` 类的 ASCII 状态机图——`None ↔ write_locked ↔ ready ↔ read_locked(count=N)`，每个 list 操作原子。这是理解「为什么 finish_write 能触发 store、为什么 reserve_read 要配 finish_read」的基础。

#### 4.4.4 代码实践

**实践目标**：对照 serving engine 的使用范式，画出一次「写 → 预取 → 读」的完整时序。

**操作步骤**（源码阅读型实践）：

1. 打开设计文档 `docs/design/v1/distributed/l2_adapters/overall.md`，看「Integration: StorageManager」一节给出的 4 步使用范式：
   ```python
   handle = sm.submit_prefetch_task(keys, layout_desc)
   while True:
       found = sm.query_prefetch_status(handle)
       if found is not None: break
   with sm.read_prefetched_results(keys[:found]) as objs: ...
   sm.finish_read_prefetched(keys[:found])
   ```
2. 在 `storage_manager.py` 里逐个把这 4 个方法（`submit_prefetch_task` / `query_prefetch_status` / `read_prefetched_results` / `finish_read_prefetched`）的行号标出来。
3. 画出时序图：调用方线程 vs StoreController 线程 vs PrefetchController 线程，标出每一步落在哪条线程、是否经过 L1/L2。

**需要观察的现象**：调用方线程只直接碰 L1；L2 的所有 I/O 都在后台控制器线程。

**预期结果**：你会得到一张清晰的三泳道时序图，证明「L2 I/O 不进 GPU 热路径」这一核心设计目标。

#### 4.4.5 小练习与答案

**练习 1**：`submit_prefetch_task` 里，为什么前缀之后的 key 若被 `reserve_read` 误加了读锁，必须显式 `finish_read` 释放？

> 参考答案：`reserve_read` 是按整个 keys 列表加锁的，但前缀命中计数 `hit_count` 可能在中间就断了（某个 key 不在 L1）。断点之后的 key 若实际在 L1（被 reserve_read 成功锁定）但不在连续前缀里，就会留下「悬挂读锁」——既占 L1 空间又会干扰淘汰。所以代码在 L524-531 把 `keys[hit_count:]` 中实际被锁的 key 挑出来 `finish_read` 释放。

**练习 2**：`_combine_found` 为什么用 `Bitmap.gather` 而不是直接按位或？

> 参考答案：L2 返回的 bitmap 是按「提交给 L2 的剩余 key」编号的（0-based 局部索引），而最终结果要按原始 key 位置编号。`handle.l2_orig_indices` 是「L2 局部索引 → 原始位置」的映射表，`gather` 用它把 L2 的 set bit 搬到原始位置上，再与 L1 命中合并。直接按位或会因为两套 bitmap 索引体系不同而出错。

### 4.5 两级模型的全貌：L1Manager + L2 适配器 + 配额

#### 4.5.1 概念说明

把 4.3、4.4 的零件拼起来，distributed 存储的「两级模型」全貌如下：

- **L1（本地）**：由 `L1Manager` 管理，是一块预分配的内存池（pinned DRAM / GDS slab / Device-DAX 三选一，互斥）。对象有 `ready / write_locked / read_locked(count)` 状态机，带 TTL 锁。L1 是所有读写的落脚点，也是最快的命中层。
- **L2（远端）**：由一组 `L2AdapterInterface` 实现承担，可以是 Redis/Valkey、S3、文件系统、Mooncake、NIXL、P2P、Aerospike、HuggingFace bucket 等。L2 全部异步、非阻塞，提供 store / lookup_and_lock / load 三类原语。
- **配额（quota）**：`QuotaManager` 按 `cache_salt` 维护字节预算，给 L2 淘汰控制器和 HTTP 端 CRUD 共享，实现多租户隔离下的「公平使用」。

#### 4.5.2 核心流程

L2 适配器的统一契约是「submit → poll eventfd → query result」三段式，三类原语：

```
store：       submit_store_task(keys, objects) → L2TaskId
              pop_completed_store_tasks() → {L2TaskId: L2StoreResult}

lookup+lock： submit_lookup_and_lock_task(keys) → L2TaskId      # 原子：查存在 + 加锁
              query_lookup_and_lock_result(task_id) → Bitmap | None  # 每任务只返回一次
              submit_unlock(keys) → None                          # fire-and-forget，适配器必须最终成功

load：        submit_load_task(keys, objects) → L2TaskId         # objects 是调用方提供的写缓冲
              query_load_result(task_id) → Bitmap | None
```

`lookup_and_lock` 的「查 + 锁」原子性很关键：它防止 L2 在「查到存在」和「真正加载」之间把对象淘汰掉。

配额有两种语义（`QuotaManager` 的两个方法）：

- `get_limit_bytes(salt)`：**allowlist 语义**，未注册 salt 返回 `0`（即「立即淘汰」），用于 MP server 本地淘汰。
- `effective_limit_bytes(salt)`：**默认配额语义**，未注册 salt 返回默认值（启动默认 `None` = 豁免），用于 coordinator，防止刚启动、配额表未同步时大规模误淘汰未知租户。

#### 4.5.3 源码精读

L2 适配器契约：

- [lmcache/v1/distributed/l2_adapters/base.py:78-117](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/l2_adapters/base.py#L78-L117)：`L2AdapterInterface` 类 docstring，说明三大功能（store / lookup_and_lock / load）、非阻塞三段式、错误处理（store 粗粒度、lookup/load 细粒度 bitmap）、线程安全要求。
- [lmcache/v1/distributed/l2_adapters/base.py:34-75](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/l2_adapters/base.py#L34-L75)：`AdapterUsage` 统一用量报告，`usage_fraction` 在容量未知时返回 `-1.0`（沿用 legacy「无淘汰信号」哨兵）。
- [lmcache/v1/distributed/l2_adapters/base.py:200-232](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/l2_adapters/base.py#L200-L232)：`submit_store_task` / `pop_completed_store_tasks`。
- [lmcache/v1/distributed/l2_adapters/base.py:238-297](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/l2_adapters/base.py#L238-L297)：lookup_and_lock 系列与 unlock（注释强调适配器必须保证 unlock 最终成功，控制器永不重试）。

`L2StoreResult`（把「成功 + 字节数」压进一个 int）：

- [lmcache/v1/distributed/internal_api.py:174-201](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/internal_api.py#L174-L201)：`>= 0` 表示成功（值=传输字节数），`-1` 表示失败。

配额管理：

- [lmcache/v1/distributed/quota_manager.py:34-128](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/quota_manager.py#L34-L128)：`QuotaManager`，注意模块 docstring（L8-22）讲清了 `get_limit_bytes`（allowlist）与 `effective_limit_bytes`（默认配额）两种语义的差别与各自使用方。

配置 dataclass：

- [lmcache/v1/distributed/config.py:208-251](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/config.py#L208-L251)：`EvictionConfig`（策略名 `LRU/IsolatedLRU/noop` + watermark + ratio）与 `StorageManagerConfig`（聚合 L1 配置、淘汰配置、L2 适配器列表、store/prefetch 策略名、`prefetch_max_in_flight`）。
- [lmcache/v1/distributed/config.py:595-604](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/config.py#L595-L604)：`parse_args_to_config` 从 CLI 参数组装 `StorageManagerConfig`，是 daemon 启动时的真实入口。

#### 4.5.4 代码实践（本讲主实践）

**实践目标**：画出完整的「对象 key → L1 命中？否 → 查 L2 → 回填 L1」两级存储流转图。

**操作步骤**：

1. 打开 [lmcache/v1/distributed/api.py:56-79](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/api.py#L56-L79)，复习 `ObjectKey` 五字段。
2. 在 `storage_manager.py` 跟踪 `submit_prefetch_task`（L398）里 L1 命中检测（`reserve_read` → `hit_count`）与 L2 委托（`submit_prefetch_request`）的分界。
3. 在 `prefetch_controller.py` 跟踪 L2 的 lookup_and_lock → load → 写锁转读锁三步。
4. 自己画一张图（纸笔或绘图工具），要素必须包含：
   - 5 个 `ObjectKey` 依次进入 `submit_prefetch_task`；
   - L1 命中前 2 个（`hit_count=2`），标注 `l1_found_indices={0,1}`；
   - 剩余 3 个进 L2，其中 L2 命中第 0、1 个（即原始位置 2、3），第 2 个（原始位置 4）未命中；
   - `query_prefetch_status` 用 `_combine_found` 合并，最终 bitmap `11110`（前 4 个命中）；
   - 标注每段发生在「调用方线程」还是「PrefetchController 后台线程」。

**需要观察的现象**：L1 命中立即返回；L2 部分需要轮询；最终 bitmap 用 `gather` 把 L2 局部索引映射回原始位置。

**预期结果**：一张标注清晰的流转图，能向他人讲清「为什么最终命中是前 4 个而非前 3 个」（因为 L2 命中是原始位置 2、3，加上 L1 的 0、1，组成连续前缀 0-3）。

**待本地验证**：图中具体命中位置可自行设计，关键是映射关系正确。

#### 4.5.5 小练习与答案

**练习 1**：L2 适配器的 `lookup_and_lock` 为什么要把「查存在」和「加锁」做成一个原子操作？

> 参考答案：如果先 lookup 再单独 lock，两步之间另一个线程可能把对象从 L2 淘汰掉，导致随后的 load 失败。原子地「查到就锁住」保证被查到的对象在 load 之前不会被淘汰，简化了控制器的错误处理。

**练习 2**：`QuotaManager` 的两种「未注册 salt」语义为什么不能合并成一种？

> 参考答案：因为两种使用方对「未知租户」的诉求相反。MP server 本地淘汰希望「未知就淘汰」（allowlist，默认 0），以严格执行配额；而 coordinator 在刚启动、配额表还没被外部 quota controller 同步时，希望「未知先放过」（默认配额，启动默认 None=豁免），避免大规模误淘汰。同一份配额表两种读法，分别用 `get_limit_bytes` 和 `effective_limit_bytes` 暴露。

**练习 3**：`AdapterUsage.usage_fraction` 在容量未知时为什么返回 `-1.0` 而不是 `0.0`？

> 参考答案：`0.0` 表示「容量已知且空」，会被淘汰控制器当作「不需要淘汰」；而 `-1.0` 是「无淘汰信号」哨兵，让淘汰控制器用 `< 0` 短路跳过该适配器。用负数区分「无信号」和「空」，避免把不支持全局淘汰的适配器（如 FS，假设无限磁盘）错误地纳入容量淘汰。

## 5. 综合实践

把本讲的知识串起来，完成下面这个端到端的「两级存储走查」任务：

**场景**：一个 cache server daemon，L1 是 4GB pinned DRAM，L2 是一个 Redis 适配器（`resp`）。某请求需要 6 个连续 chunk 的 KV（key 编号 0–5）。

**任务**：

1. **构造阶段**：写出这 6 个 chunk 的 `ObjectKey` 应该长什么样（假设同模型、同 rank、同 salt），用 `ComputeKVRank` 说明 `kv_rank` 如何由 `world_size=2, global_rank=1, local_world_size=2, local_rank=1` 算出（手算位移结果）。
2. **预热阶段**：假设之前已经写过 key 0–3，且 StoreController 已把它们异步复制到 L2。现在 L1 因容量压力淘汰了 key 2、3。请描述当前 L1 和 L2 各持有哪些 key。
3. **预取阶段**：调用 `submit_prefetch_task([0,1,2,3,4,5])`：
   - L1 连续前缀命中到第几个？（提示：key 2 被 L1 淘汰了）
   - 剩余哪些 key 进 L2 lookup？
   - L2 命中哪些？（key 4、5 从没写过，应未命中）
   - PrefetchController 加载哪些 key 进 L1？为什么不是全部命中？
   - 最终 `query_prefetch_status` 返回的 bitmap 是什么（6 bit）？前缀命中数是多少？
4. **读阶段**：写出 `read_prefetched_results` + `finish_read_prefetched` 的调用，说明读锁的获取与释放时机。
5. **反思**：如果 L2 是 S3（高延迟）而非 Redis，上述流程的哪一步会变慢？哪一步**不会**变慢？为什么？

**预期成果**：一份文档，含手算的 `kv_rank`、L1/L2 持有表、预取阶段的命中分析、最终 bitmap（应为 `111100`，前缀命中 4）、以及对「L2 延迟只影响后台线程、不影响 GPU 热路径」的解释。

**待本地验证**：可在本地用 `mock_l2_adapter.py` 构造一个内存 L2，写一个最小脚本验证你的分析（参考 `l2_adapters/overall.md` 给出的 4 步范式）。若无法运行，至少完成纸面推演并标注「待本地验证」。

## 6. 本讲小结

- `v1/distributed/` 是 MP 架构下的新存储体系，与单机 `v1/storage_backend/` 并存；legacy `lmcache/storage_backend/` 现在只剩 `serde/`。
- 核心是 **L1（本地内存池）/ L2（远端异步适配器）** 两级模型，读写都在 L1 落脚，L2 全程异步、非阻塞。
- `ObjectKey`（内容寻址 + `cache_salt` 租户隔离 + `kv_rank` 并行编码）是对象的全局唯一标识，`EncodedObjectKey` 是它的 JSON 安全线缆形态。
- `StorageManager` 是门面，把 L1、L2 适配器、三类后台控制器组装起来，对外只暴露 reserve/finish_write、submit/query/read/finish_prefetch 等同步语义 API。
- 三类后台控制器分工：StoreController（L1→L2 复制）、PrefetchController（L2→L1 预取，只加载连续前缀）、EvictionController（L1/L2 水位淘汰）。
- `Tier` 枚举、`QuotaManager` 双语义、`AdapterUsage` 的 `-1.0` 哨兵等小设计，体现了「线缆中立、租户隔离、优雅降级」的工程取舍。

## 7. 下一步学习建议

本讲覆盖了 distributed 存储的「骨架」。后续讲义建议按以下顺序深入：

- **u4-l3 L2 适配器与可插拔存储**：本讲把 L2 当黑盒，下一讲打开 `l2_adapters/` 各种实现（Redis/Valkey、S3、Mooncake、NIXL、P2P、FS），讲清工厂与可重配置机制。
- **u4-l4 SERDE 变换与压缩**：本讲提到的 `SerdeL2AdapterWrapper` 和 `serde/`（fp8、turboquant、multi、asym_k16_v8）是「KV 离开本机内存前的最后一道变换」，下一讲展开。
- **u4-l5 淘汰策略与配额管理**：本讲的 `EvictionController` + `QuotaManager` 在下一讲深入，讲清 LRU / IsolatedLRU / noop 与 delete cap 调度。
- **u4-l7 PD 分离与传输通道**：本讲的 L2 适配器里已有 `nixl_store`、`p2p` 等，PD 分离场景下的 KV 传输通道（`transfer_channel/`）在下一讲展开。

继续阅读建议：先重读 `docs/design/v1/distributed/l2_adapters/overall.md`（本讲多次引用的权威文档），再挑一个最简单的适配器（`mock_l2_adapter.py`）通读，把本讲的抽象映射到具体实现。
