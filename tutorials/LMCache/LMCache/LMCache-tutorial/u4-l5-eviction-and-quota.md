# 淘汰策略与配额管理

## 1. 本讲目标

在 u4-l2 中我们看到：分布式存储用 **L1（本地内存池）/ L2（远端异步适配器）** 两级模型缓存 KV，把慢速 I/O 踢出 GPU 热路径。但缓存空间是有限的——L1 受本机内存约束，L2 受远端存储容量约束。一旦写满，就必须决定「丢掉谁」。本讲要回答四个问题：

1. **丢谁**：淘汰策略（eviction policy）如何选受害者？为什么需要 LRU、IsolatedLRU、noop 三种？
2. **什么时候丢**：谁来周期性地检查水位、触发淘汰？
3. **谁能放、放多少**：多租户场景下，如何阻止某个用户（`cache_salt`）把共享的 L1/L2 全部占满？
4. **一次丢多少**：当一次要删的 key 极多（例如把某租户配额清零）时，为什么不能「一把梭」删除，而要分块（delete cap）？

学完后你应当能够：

- 说清 `EvictionPolicy` 抽象契约与三种实现的语义差异，并用 `CreateEvictionPolicy` 工厂按配置选择。
- 跟踪 `L1EvictionController` / `L2EvictionController` 的后台调度循环，理解水位触发与比例淘汰。
- 说明 `QuotaManager` 的「双语义」配额查询（allowlist vs default-quota），以及它如何与 IsolatedLRU 协同实现按租户隔离淘汰。
- 解释删除上限（delete cap）`MAX_DELETE_BATCH` 的来源，以及 coordinator 端 `L2EvictionManager` 如何把淘汰计划分块异步分发。

## 2. 前置知识

本讲建立在 u4-l2 的概念之上，复习几个关键术语：

- **L1 / L2**：L1 是本进程的本地内存池（快、容量小）；L2 是远端异步存储适配器（慢、容量大）。读写都在 L1 落脚，L2 全程非阻塞。
- **`ObjectKey`**：内容寻址的缓存键，由 `chunk_hash` / `model_name` / `kv_rank` / `cache_salt` 等字段构成。其中 `cache_salt` 是**身份字段**（参与 `__eq__` / `__hash__`），同一份 token 序列来自不同 `cache_salt` 就是不同的 key——这就是租户隔离的根基。
- **`cache_salt` = 一个租户（用户）**：所有共享同一 `cache_salt` 的请求属于同一用户，共享一份配额与一条 LRU 链；不同 `cache_salt` 完全隔离。匿名流量（API 调用方不设 `cache_salt`）落到空串 `""` 命名空间。
- **监听器（listener）模式**：L1 管理器和 L2 适配器在 key 生命周期的关键点（存入、访问、删除）回调注册的监听器；淘汰策略正是借此被动地维护自己的记账状态，而不是主动轮询。
- **no fate-sharing**：LMCache 的设计与它的依赖（coordinator、远端存储）解耦——配额表丢失或 coordinator 宕机，推理照常运行，只是淘汰精度下降。

> 一个直觉：淘汰策略本身是**纯数据结构**——它只记账和「出主意」（给一份候选 key 清单），真正的删除动作由控制器（controller）执行。这种「决策与执行分离」是本讲反复出现的主题。

## 3. 本讲源码地图

本讲涉及的关键文件按职责分为四组：

| 文件 | 作用 |
| --- | --- |
| [`lmcache/v1/distributed/eviction.py`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/eviction.py) | 定义 `EvictionPolicy` 纯抽象基类，以及把 L1/L2 生命周期事件桥接到策略的 `L1EvictionPolicy` / `L2EvictionPolicy` 监听器。 |
| [`lmcache/v1/distributed/eviction_policy/factory.py`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/eviction_policy/factory.py) | `CreateEvictionPolicy` 工厂，按配置字符串路由到具体策略类。 |
| [`lmcache/v1/distributed/eviction_policy/lru.py`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/eviction_policy/lru.py) | `LRUEvictionPolicy`：全局单链 LRU。 |
| [`lmcache/v1/distributed/eviction_policy/isolated_lru.py`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/eviction_policy/isolated_lru.py) | `IsolatedLRUEvictionPolicy`：每 `cache_salt` 一条独立 LRU 链。 |
| [`lmcache/v1/distributed/eviction_policy/noop.py`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/eviction_policy/noop.py) | `NoOpEvictionPolicy`：不记账、不淘汰（buffer-only 模式）。 |
| [`lmcache/v1/distributed/storage_controllers/eviction_controller.py`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/storage_controllers/eviction_controller.py) | `EvictionController` 基类 + `L1EvictionController` / `L2EvictionController`：后台线程周期性检查水位并执行淘汰。 |
| [`lmcache/v1/distributed/quota_manager.py`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/quota_manager.py) | `QuotaManager`：线程安全的按 `cache_salt` 字节配额注册表，提供「双语义」配额查询。 |
| [`lmcache/v1/distributed/config.py`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/config.py) | `EvictionConfig`：策略名 + 水位 + 淘汰比例。 |
| [`lmcache/v1/distributed/internal_api.py`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/internal_api.py) | `EvictionAction` / `EvictionDestination` / `QuotaEntry` 等线缆类型，以及监听器接口。 |
| [`lmcache/v1/distributed/storage_manager.py`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/storage_manager.py) | 把 `QuotaManager` 和两个控制器组装起来（接 u4-l2）。 |
| [`lmcache/v1/mp_coordinator/cache_control/eviction_manager.py`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/cache_control/eviction_manager.py) | coordinator 端 `L2EvictionManager`：舰队级淘汰，含删除上限分块分发。 |
| [`lmcache/v1/multiprocess/cache_control/object_service.py`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/cache_control/object_service.py) | 定义 `MAX_DELETE_BATCH`（删除上限）及其 HTTP 400 拒绝逻辑。 |

> 重要边界：本讲有**两套并存的淘汰执行者**——①每个 MP server 节点内的 `L1/L2EvictionController`（节点级、本地视角），②coordinator 进程内的 `L2EvictionManager`（舰队级、跨实例视角）。它们复用同一套策略类与 `QuotaManager`，但驱动方式与删除上限处理迥异。这是理解本讲后两个模块的关键。

## 4. 核心概念与源码讲解

### 4.1 淘汰策略抽象与三种可插拔实现

#### 4.1.1 概念说明

淘汰策略要回答的核心问题是：「给我一个比例 `expected_ratio`，告诉我现在该丢哪些 key」。LMCache 把这件事抽象成一个**纯数据结构** `EvictionPolicy`——它只做两件事：

- **记账**：通过 `on_keys_created` / `on_keys_touched` / `on_keys_removed` 三个回调被动维护内部状态（谁最近用过、按什么顺序）。
- **出主意**：通过 `get_eviction_actions(expected_ratio, ...)` 返回一份「候选受害者清单」`list[EvictionAction]`。

策略**不碰真实存储**，不知道 L1 还是 L2，不知道适配器。这种「决策与执行分离」让同一套策略类既能服务 L1 也能服务 L2。

为什么需要三种实现？

- **`LRUEvictionPolicy`（全局 LRU）**：所有 key 排在一条链上，丢最久没用的。适合单租户或不在乎租户隔离的场景。缺点：多租户下，一个「大客户」的冷数据会把别人的热数据挤出去。
- **`IsolatedLRUEvictionPolicy`（隔离 LRU）**：每个 `cache_salt` 一条独立 LRU 链，淘汰时可限定「只从某个租户的链上选」。配合 `QuotaManager` 实现「谁超额只丢谁的」，是本讲的多租户主角。
- **`NoOpEvictionPolicy`（空操作）**：既不记账也不淘汰。用在 buffer-only 模式——当 L1 只当写缓冲、由 `StoreController` 在写完 L2 后立刻清掉时，维护 LRU 链纯属浪费（无意义的「插入后立即删除」循环）。

#### 4.1.2 核心流程

LRU 的核心数据结构是 `OrderedDict`：新访问的 key `move_to_end` 到尾部（most recently used, MRU），头部就是 least recently used (LRU)，淘汰从头取。一轮淘汰流程：

```
get_eviction_actions(expected_ratio, filter?, cache_salt?):
  1. clamp expected_ratio 到 [0.0, 1.0]
  2. target_count = floor( len(pool) × expected_ratio )
     若 expected_ratio > 0 且 target_count == 0 且 pool 非空 → target_count = 1
  3. 从 pool 头部(LRU 端)遍历:
       跳过 filter(key) == False 的 key（如被锁定的 key）
       收集到 target_count 个为止
  4. 返回 [EvictionAction(keys=..., destination=...)]
```

其中 `destination` 决定受害者去向：`EvictionDestination.DISCARD`（直接丢弃，当前唯一支持的路径）或 `EvictionDestination.L2_CACHE`（预留的「L1 淘汰到 L2」语义）。`destination` 选取规则：若注册过目的地则用第一个，否则用构造默认值。

有一个贯穿三种实现的**关键不变量**——前缀匹配导致的反向插入。一个请求产生的 key 序列是 `(key1, key2, key3)`，对应连续 token 段。由于命中是前缀匹配，一旦 `key1` 被丢，`key2`/`key3` 即便还在也无法命中。因此策略在 `on_keys_created` / `on_keys_touched` 中**逆序遍历** `reversed(keys)`，让序列中靠后的 key 排到 LRU 链更前面（更早被淘汰），从而在比例淘汰时倾向于成段保留前缀。

#### 4.1.3 源码精读

**抽象契约**——`EvictionPolicy` 是纯抽象基类，定义五个抽象方法加一个 `support_isolation` 属性：

[lmcache/v1/distributed/eviction.py:20-42](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/eviction.py#L20-L42) 定义基类与 `support_isolation` 属性（默认 `False`，IsolatedLRU 覆盖为 `True`）——控制器靠这个属性决定走「全局分支」还是「按租户分支」，而不是用 `isinstance` 判断。

[lmcache/v1/distributed/eviction.py:85-123](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/eviction.py#L85-L123) 声明 `get_eviction_actions` 抽象方法。注意三个参数：`expected_ratio`（比例提示）、`key_eligible_filter`（可选资格过滤，跳过被锁定的 key）、`cache_salt`（仅 `support_isolation=True` 的策略认它，用来限定作用域）。docstring 明确「这只是提示，策略可以多给或少给」，并且「淘汰动作可能异步执行，不要假设立即生效，真正删除要等 `on_keys_removed` 回调」。

**工厂路由**——选择哪种策略完全由配置字符串决定：

[lmcache/v1/distributed/eviction_policy/factory.py:13-32](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/eviction_policy/factory.py#L13-L32) 是 `CreateEvictionPolicy`：三个 `if` 分支分别对应 `"LRU"` / `"IsolatedLRU"` / `"noop"`，其余字符串直接 `raise ValueError`。这是典型的「显式分支 + 早失败」工厂，与 u4-l3 / u4-l4 的「定义即注册」自注册工厂风格不同——淘汰策略数量固定、枚举式，故用最直白的 if/elif。

**全局 LRU 实现**——`LRUEvictionPolicy` 用单条 `OrderedDict`：

[lmcache/v1/distributed/eviction_policy/lru.py:38-59](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/eviction_policy/lru.py#L38-L59) 构造：一把 `threading.Lock`、一个 `OrderedDict[ObjectKey, None]`（尾部是 MRU）、一个目的地列表、一个默认目的地。注意值是 `None`——`OrderedDict` 这里只用作有序集合。

[lmcache/v1/distributed/eviction_policy/lru.py:72-92](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/eviction_policy/lru.py#L72-L92) 是 `on_keys_created`：`reversed(keys)` 逆序插入，已存在的 `move_to_end`、新 key 加到尾部。这正是上文「前缀匹配 → 反向插入」的源码体现。

[lmcache/v1/distributed/eviction_policy/lru.py:128-199](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/eviction_policy/lru.py#L128-L199) 是 `get_eviction_actions` 的全局版本。关键计算：

```python
target_count = int(len(self._order) * expected_ratio)          # L168
if expected_ratio > 0 and target_count == 0 and len(self._order) > 0:
    target_count = 1                                            # L171 保证至少丢 1 个
for key in self._order:                                         # L181 从 LRU 端(头)遍历
    if key_eligible_filter is not None and not key_eligible_filter(key):
        continue                                                # L182 跳过被锁定的 key
    keys_to_evict.append(key)
    if len(keys_to_evict) >= target_count:
        break                                                   # L187
```

末尾 L194-199 决定 `destination`：有注册目的地取第一个，否则用默认 `DISCARD`。注意它**忽略** `cache_salt` 参数（docstring L147 明说「Ignored by LRU policy」）。

**隔离 LRU 实现**——`IsolatedLRUEvictionPolicy` 是多租户主角，每个 `cache_salt` 一条链：

[lmcache/v1/distributed/eviction_policy/isolated_lru.py:45-60](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/eviction_policy/isolated_lru.py#L45-L60) 覆盖 `support_isolation` 返回 `True`，并把状态从单个 `OrderedDict` 换成 `dict[str, OrderedDict]`，键是 `cache_salt`。

[lmcache/v1/distributed/eviction_policy/isolated_lru.py:66-81](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/eviction_policy/isolated_lru.py#L66-L81) 是 `on_keys_created`：按 `key.cache_salt` 路由到对应桶，桶不存在则新建，桶内同样逆序插入。

[lmcache/v1/distributed/eviction_policy/isolated_lru.py:106-173](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/eviction_policy/isolated_lru.py#L106-L173) 是隔离版 `get_eviction_actions`。与全局版最大区别：**强制要求 `cache_salt` 非 None**：

```python
if cache_salt is None:
    raise ValueError(...)      # L138-143 「本策略只按桶淘汰，没有全局路径」
order = self._per_salt_order.get(cache_salt)
pool = list(order.keys()) if order else []   # L146 只在该租户的桶里选
```

它「isolated only by contract」——宁可抛错也不静默退化成全局池，避免偷偷越界淘汰别人。

[lmcache/v1/distributed/eviction_policy/isolated_lru.py:92-104](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/eviction_policy/isolated_lru.py#L92-L104) 是 `on_keys_removed` 的一个小细节：桶删空后 `del self._per_salt_order[key.cache_salt]`，让快照保持紧凑（避免 `list_quotas` 时出现大量空桶）。

**空操作实现**——`NoOpEvictionPolicy` 全是 `pass` 或 `return []`：

[lmcache/v1/distributed/eviction_policy/noop.py:22-50](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/eviction_policy/noop.py#L22-L50) 每个方法体都是空。模块 docstring（L1-8）点明用途：buffer-only 模式下 L1 只是写缓冲，`StoreController` 写完 L2 立刻清 L1，此时维护 LRU 链就是「插入后立即删除」的无用功，故直接关掉。

#### 4.1.4 代码实践

**实践目标**：亲手验证 LRU 与 IsolatedLRU 在「同一个比例淘汰请求」下选出不同受害者，体会隔离语义。

**操作步骤**（纯 Python 单元实验，无需 GPU/服务）：

1. 新建一个临时脚本（**示例代码**，不要提交到仓库）：

   ```python
   # 示例代码：对比 LRU 与 IsolatedLRU 的淘汰选择
   from lmcache.v1.distributed.api import ObjectKey
   from lmcache.v1.distributed.eviction_policy.lru import LRUEvictionPolicy
   from lmcache.v1.distributed.eviction_policy.isolated_lru import IsolatedLRUEvictionPolicy

   def mk(salt, n):
       # 构造一个最小可用的 ObjectKey（chunk_hash 用 n 字节占位）
       return ObjectKey(chunk_hash=bytes([n]), model_name="m", kv_rank=0, cache_salt=salt)

   # 两个租户各存 4 个 key
   alice = [mk("alice", i) for i in range(4)]
   bob   = [mk("bob",   i) for i in range(4)]

   # ---- 全局 LRU ----
   g = LRUEvictionPolicy()
   g.on_keys_created(alice + bob)
   act = g.get_eviction_actions(0.5)           # 想丢一半
   print("global evict salts:", sorted({k.cache_salt for k in act[0].keys}))

   # ---- 隔离 LRU ----
   iso = IsolatedLRUEvictionPolicy()
   iso.on_keys_created(alice + bob)
   act_a = iso.get_eviction_actions(0.5, cache_salt="alice")  # 只丢 alice 的一半
   print("isolated evict salts:", sorted({k.cache_salt for k in act_a[0].keys}))
   print("alice tracked:", iso.get_num_tracked_keys("alice"),
         "bob tracked:", iso.get_num_tracked_keys("bob"))
   ```

2. 在仓库根目录运行：`python <你的脚本>.py`（需要已 `pip install -e .`，见 u1-l2）。

**需要观察的现象**：

- 全局 LRU 一次丢 4 个 key，且 victim 跨 alice 与 bob（具体是哪边取决于插入顺序——逆序插入后，先插入序列靠后的会先被丢）。
- 隔离 LRU 限定 `cache_salt="alice"` 时，victim **全部**来自 alice，bob 的 key 一个没动。

**预期结果**：全局分支输出 salts 含两类；隔离分支只含 `alice`，且淘汰后 bob 的计数不变。

> 若环境无法运行（无 torch 等），这是「源码阅读型实践」：直接对照 [lru.py:181](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/eviction_policy/lru.py#L181)（遍历整条 `_order`）与 [isolated_lru.py:145-146](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/eviction_policy/isolated_lru.py#L145-L146)（只取 `_per_salt_order[cache_salt]`）推导同样结论。

#### 4.1.5 小练习与答案

**练习 1**：`get_eviction_actions(0.0)` 在 pool 非空时返回什么？`get_eviction_actions(0.1)` 在 pool 只有 3 个 key 时又返回几个？

答案：`expected_ratio=0.0` 时 `target_count` 为 0，直接返回空列表 `[]`（注意 L171 的「至少 1 个」保护只在 `expected_ratio > 0` 时生效）。`0.1 × 3 = 0.3 → int = 0`，但因 `expected_ratio > 0` 触发保护，`target_count` 被抬到 1，返回 1 个 key。

**练习 2**：为什么 `IsolatedLRUEvictionPolicy.get_eviction_actions(cache_salt=None)` 要抛 `ValueError` 而不是静默返回空？

答案：隔离策略「按定义」只能按桶淘汰，没有全局路径。抛错是「大声失败」——防止调用方误以为「传 None 就能全局淘汰」，结果什么都没丢却以为清理成功。若静默返回 `[]`，控制器会以为已处理完毕，实际什么也没动，bug 难以察觉。

---

### 4.2 EvictionController：节点级后台调度循环

#### 4.2.1 概念说明

策略只「出主意」，真正周期性检查水位、调用策略、执行删除的是**控制器**（controller）。LMCache 在每个 MP server 节点内放了两类：

- **`L1EvictionController`**：盯 L1 内存用量。每个 `StorageManager` 一个。
- **`L2EvictionController`**：盯所有开了淘汰的 L2 适配器。一个控制器管多个适配器，单线程轮询。

控制器与策略之间靠**监听器桥接**粘合：策略是「纯数据结构」，但 L1 管理器 / L2 适配器发出的事件签名（`on_l1_keys_write_finished` / `on_l2_keys_stored`）与策略的方法签名（`on_keys_created`）不一致。桥接器 `L1EvictionPolicy` / `L2EvictionPolicy` 就是翻译层，用组合（而非多重继承）把前者转成后者。这样策略类完全不依赖具体是 L1 还是 L2。

#### 4.2.2 核心流程

L1 与 L2 的调度循环结构几乎相同，每秒一轮：

```
eviction_loop (后台线程, 每 1s):
  while not stop:
    sleep(1)
    取当前用量
    if 用量 < watermark:   # 还没到水位，跳过
        publish(_triggered=False); continue
    actions = policy.get_eviction_actions(eviction_ratio, [filter])
    for action in actions:
        execute_eviction_action(action)   # 调 manager.delete / adapter.delete
    publish(_triggered=True)
```

L1 与 L2 的差别在「用量怎么算」「按租户还是全局」：

- **L1**：用量是 `used_bytes / total_bytes`（一个分数），只用**全局**分支（L1 没有按 `cache_salt` 分配的语义）。
- **L2**：用量来自适配器的 `get_usage() -> AdapterUsage`。若策略 `support_isolation == True` 且注入了 `QuotaManager`，走「按租户」分支；否则走「全局聚合」分支。

这是 u4-l3 提到的「`supports_global_eviction` / `usage_fraction`」信号在淘汰侧的真正消费者。

#### 4.2.3 源码精读

**桥接器**——把 L1/L2 事件翻译成策略调用：

[lmcache/v1/distributed/eviction.py:126-167](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/eviction.py#L126-L167) 是 `L1EvictionPolicy`：`on_l1_keys_write_finished` → `on_keys_created`，`on_l1_keys_read_finished` / `on_l1_keys_accessed` → `on_keys_touched`，`on_l1_keys_deleted_by_manager` → `on_keys_removed`。注意 L154-157 的 TODO：当前不区分「新建」与「覆盖写」，统一当 created 处理。

[lmcache/v1/distributed/eviction.py:170-192](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/eviction.py#L170-L192) 是 `L2EvictionPolicy`：`on_l2_keys_stored` → `on_keys_created`，`on_l2_keys_accessed` → `on_keys_touched`，`on_l2_keys_deleted` → `on_keys_removed`。

**控制器基类**——提供共享的后台线程骨架：

[lmcache/v1/distributed/storage_controllers/eviction_controller.py:36-85](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/storage_controllers/eviction_controller.py#L36-L85) 是 `EvictionController` 抽象基类：构造时建 `threading.Event` 停止标志和 `daemon=True` 后台线程；`start` / `stop` 控制生命周期；三个抽象方法 `report_status` / `eviction_loop` / `execute_eviction_action` 留给子类。

**L1 控制器**——全局水位 + 比例淘汰：

[lmcache/v1/distributed/storage_controllers/eviction_controller.py:97-107](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/storage_controllers/eviction_controller.py#L97-L107) 是 `L1EvictionController.__init__`：用工厂 `CreateEvictionPolicy(eviction_config)` 建策略，包一层 `L1EvictionPolicy` 桥接，注册成 `l1_manager` 的监听器——于是 L1 的每次写/读/删都会自动喂给策略记账，控制器无需主动同步。

[lmcache/v1/distributed/storage_controllers/eviction_controller.py:145-181](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/storage_controllers/eviction_controller.py#L145-L181) 是 L1 的 `eviction_loop` 与 `execute_eviction_action`。核心几行：

```python
used_bytes, total_bytes = self._l1_manager.get_memory_usage()   # L151
usage = 0 if total_bytes == 0 else used_bytes / total_bytes      # L152
if usage < watermark:                                            # L153
    self._publish_skipped(usage, watermark); continue
actions = self._eviction_policy.get_eviction_actions(            # L167
    eviction_ratio,
    key_eligible_filter=self._l1_manager.is_key_evictable,       # L169 跳过被锁 key
)
for action in actions:
    self.execute_eviction_action(action)                         # L172
```

`execute_eviction_action`（L175-181）目前只认 `DISCARD`：调 `self._l1_manager.delete(action.keys)`；遇到未知目的地则记 error 并仍按 DISCARD 处理（「宁可丢也不悬空」）。

**L2 控制器**——单线程管多适配器，按租户/全局分叉：

[lmcache/v1/distributed/storage_controllers/eviction_controller.py:184-198](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/storage_controllers/eviction_controller.py#L184-L198) 是 `L2AdapterEvictionState`：每个开了淘汰的适配器一份状态（策略 + 桥接监听器 + 配置），构造时把监听器注册到适配器上。

[lmcache/v1/distributed/storage_controllers/eviction_controller.py:286-300](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/storage_controllers/eviction_controller.py#L286-L300) 是分发口 `_check_and_evict`：用 `support_isolation and quota_manager is not None` 在两条路径间二选一。

[lmcache/v1/distributed/storage_controllers/eviction_controller.py:302-328](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/storage_controllers/eviction_controller.py#L302-L328) 是**全局分支** `_check_and_evict_global`：读 `usage_fraction`，`< watermark` 跳过；否则 `get_eviction_actions(eviction_ratio)` 并 `adapter.delete`。L309-312 的注释强调 `usage_fraction == -1` 表示适配器没声明容量、不支持按用量淘汰，此时不触发——这是对 u4-l3「`max_capacity_bytes` 决定 `supports_global_eviction`」的呼应（防御性双重检查，正常情况已在 `StorageManager` 构造期过滤掉）。

[lmcache/v1/distributed/storage_controllers/eviction_controller.py:330-387](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/storage_controllers/eviction_controller.py#L330-L387) 是**按租户分支** `_check_and_evict_by_cache_salt`（多租户核心，详见 4.3、4.4）。这里先看它的两个工程要点：

1. **跨租户批量合并**（L353, L381-387）：把所有超额租户的 victim 按 `destination` 聚到 `pending` 字典，最后「每个目的地一次 `adapter.delete`」，而不是「每个(租户, 目的地)一次」。对 NIXL 句柄建立、FS sync 等单次开销大的适配器，这是真实的性能收益。

2. **删除事件上抛**（L389-410）：`_execute_eviction_action` 删完后 `publish(EventType.L2_KEYS_EVICTED, ...)`，metadata 带 `key_count_per_salt`（用 `Counter` 按 `cache_salt` 聚类）。这条事件既是可观测性（接 u3-l5）的输入，也供 coordinator 侧用量账本回填（见 4.4）。

**装配点**——`StorageManager` 把控制器和 `QuotaManager` 拼起来：

[lmcache/v1/distributed/storage_manager.py:104-134](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/storage_manager.py#L104-L134) 关键事实：`QuotaManager` **总是被创建**（L108），即使没有任何适配器用 IsolatedLRU——为了让 HTTP 层始终有稳定的 `quota_manager` 引用可服务 CRUD 端点；然后遍历适配器，对满足 `_should_enable_l2_eviction` 的建 `L2AdapterEvictionState`，最后用这些 state + `quota_manager` 构造 `L2EvictionController` 并 `start()`。

[lmcache/v1/distributed/storage_manager.py:1078-1095](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/storage_manager.py#L1078-L1095) 是 `_should_enable_l2_eviction`：`eviction_config is None` → 不接入；非 IsolatedLRU 且 `not adapter.supports_global_eviction` → 不接入并告警。换言之「IsolatedLRU 不依赖适配器声明容量」（它靠 `QuotaManager` 的字节配额，不靠适配器容量），而 LRU/noop 必须适配器声明了容量才有意义。

#### 4.2.4 代码实践

**实践目标**：跟踪「写一个 key → 监听器喂给策略记账 → 后台线程触发淘汰」的完整调用链，体会控制器的被动记账 + 主动调度。

**操作步骤**（源码阅读型实践）：

1. 在 [eviction_controller.py:145](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/storage_controllers/eviction_controller.py#L145) `eviction_loop` 起点，确认轮询周期 `time.sleep(1)`（L150）。
2. 沿 `get_eviction_actions` → `execute_eviction_action` → `_l1_manager.delete`（L177）画出 L1 路径。
3. 反向追溯记账来源：`L1EvictionController.__init__` 里 `self._l1_manager.register_listener(self._listener)`（[L107](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/storage_controllers/eviction_controller.py#L107)）→ 桥接器 [eviction.py:153-158](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/eviction.py#L153-L158) → `policy.on_keys_created`。
4. 用 `Grep` 在仓库里找 `is_key_evictable` 的实现，理解 `key_eligible_filter` 如何跳过正在被读/写的 key（被锁定的 key 不能删）。

**需要观察的现象**：记账（on_keys_*）发生在数据路径的**回调线程**，而淘汰决策发生在**控制器后台线程**——两者通过策略内部的 `threading.Lock` 串行化，互不阻塞数据路径。

**预期结果**：能画出两条时间线——数据线（store→listener→policy 记账）与控制线（sleep→查水位→policy 出主意→delete）。预期删除后又会触发 `on_l1_keys_deleted_by_manager`→`on_keys_removed`，策略状态自洽。

#### 4.2.5 小练习与答案

**练习 1**：L1 控制器为什么不需要 `QuotaManager`？

答案：L1 是本进程内存池，没有「跨租户共享 + 配额」语义——L1 的淘汰只看聚合内存水位 `used/total`，用全局 LRU 即可。配额是 L2 共享存储才需要的多租户机制。所以 `L1EvictionController` 只持有 `l1_manager` 和 `eviction_config`，构造里没有 quota 参数。

**练习 2**：`execute_eviction_action` 遇到 `destination == L2_CACHE` 会怎样？

答案：当前实现把它当错误：记 `logger.error("Unsupported eviction destination")`，然后**仍按 DISCARD 处理**（调 `delete`）。即「L1 淘汰到 L2」这条预留语义尚未实现，落地仍是直接丢弃。这是「宁可丢也不悬空」的保守选择。

---

### 4.3 QuotaManager：双语义配额注册表

#### 4.3.1 概念说明

`QuotaManager` 是一个线程安全的「`cache_salt` → 字节配额」注册表。它的存在是为了回答：「这个租户允许在共享存储里放多少字节？」但「未注册的租户算多少」这个看似简单的问题，在两种部署里有截然相反的答案——这正是本模块的核心，也是最近一次提交（#4027）引入的「双语义」设计。

两种语义（同一个注册表，两个查询方法）：

| 查询方法 | 未注册 salt 解析为 | 语义 | 使用者 |
| --- | --- | --- | --- |
| `get_limit_bytes` | `0` | **allowlist（白名单）**：未注册即 0，下一轮就被全清 | MP server 节点级 `L2EvictionController`（本地视角） |
| `effective_limit_bytes` | `_default_limit_bytes`（启动默认 `None`） | **default-quota（默认配额）**：未注册先用默认值，`None`=豁免 | coordinator 舰队级 `L2EvictionManager`（全局视角） |

为什么需要两种？因为 coordinator 的配额表是**内存态**的，重启即空。如果 coordinator 用 allowlist 语义（未注册=0=立刻清），那么一个刚重启、配额表还没被外部控制器重新同步的 coordinator，会把**所有未知租户**的缓存一次性清光——灾难性的「冷启动误杀」。default-quota 语义把启动默认设为 `None`（豁免），等外部控制器显式 `PUT /quota/config {"default_limit_gb": 0}` 来「武装」白名单执行后，才开始清理未配额租户。而 MP server 节点级控制器没有这个冷启动问题（它的配额表是节点本地的、可控的），继续用更激进的 allowlist。

#### 4.3.2 核心流程

配额生命周期（CRUD，运行时可变，下一个淘汰周期即生效，无需重启）：

```
set_quota(salt, bytes)        # 注册/更新（负数 raise ValueError，0 合法）
delete_quota(salt) -> bool    # 删除（返回是否真删了；删后生效额度落 0）
get_limit_bytes(salt) -> int  # allowlist 查询：未注册→0
effective_limit_bytes(salt)   # default-quota 查询：未注册→_default_limit_bytes
set_default_limit_bytes(x)    # None=豁免 / 0=武装白名单 / >0=每未注册租户给该额度
list_quotas() -> [QuotaEntry] # 快照（detached copy）
```

在按租户淘汰分支里，配额与用量的关系是触发条件：

\[\text{触发淘汰} \iff \text{user\_bytes} \;\geq\; \text{watermark} \times \text{limit}\]

当 `limit == 0`（allowlist 下未注册，或被显式清零），上式恒成立（`user_bytes > 0 >= 0`），于是该租户被全量淘汰（`effective_ratio = 1.0`）；否则按配置的 `eviction_ratio` 比例淘汰。这就是「白名单：只有显式配额的租户能留数据」的数学实现。

#### 4.3.3 源码精读

模块 docstring 本身就是最好的设计说明：

[lmcache/v1/distributed/quota_manager.py:1-22](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/quota_manager.py#L1-L22) 逐条对比两种语义及其使用者。读这段 docstring 比读任何二手解释都准。

[lmcache/v1/distributed/quota_manager.py:42-50](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/quota_manager.py#L42-L50) 构造：一把锁、`_limits: dict[str, int]`、`_default_limit_bytes: int | None = None`（启动默认就是豁免）。

[lmcache/v1/distributed/quota_manager.py:52-64](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/quota_manager.py#L52-L64) `set_quota`：负数 `raise ValueError`；`0` 合法且「行为上等价于没有条目」（下一轮被清），区别只在 bookkeeping（会出现在 `list_quotas` 里）。

[lmcache/v1/distributed/quota_manager.py:77-86](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/quota_manager.py#L77-L86) **allowlist 查询** `get_limit_bytes`：`self._limits.get(cache_salt, 0)`——未注册解析为 `0`，docstring 明说「0 这个默认值是故意触发未知 salt 的淘汰」。

[lmcache/v1/distributed/quota_manager.py:88-128](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/quota_manager.py#L88-L128) 是 default-quota 三件套：`set_default_limit_bytes`（L88-104，`None`/`0`/正数三态，负数 raise）、`get_default_limit_bytes`（L106-113）、`effective_limit_bytes`（L115-128，显式注册的返回其额度，否则返回 `_default_limit_bytes`）。

[lmcache/v1/distributed/quota_manager.py:130-149](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/quota_manager.py#L130-L149) `has_quota`（区分「默认 0」与「显式注册 0」）与 `list_quotas`（返回 `QuotaEntry` 快照，detached copy）。

**配置类型**——`EvictionConfig` 只有三个字段：

[lmcache/v1/distributed/config.py:209-221](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/config.py#L209-L221) `eviction_policy`（`"LRU" / "IsolatedLRU" / "noop"`）、`trigger_watermark`（默认 0.8）、`eviction_ratio`（默认 0.2）。

**线缆类型**：

[lmcache/v1/distributed/internal_api.py:149-171](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/internal_api.py#L149-L171) `EvictionDestination`（`DISCARD` / `L2_CACHE`）与 `EvictionAction`（`destination` + `keys`，frozen dataclass）。

[lmcache/v1/distributed/internal_api.py:204-209](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/internal_api.py#L204-L209) `QuotaEntry`：`cache_salt` + `limit_bytes`，用于 `list_quotas` 快照。

#### 4.3.4 代码实践

**实践目标**：验证「同一个注册表，两个查询方法对未注册 salt 给出不同答案」，并对照两个使用者理解为何如此。

**操作步骤**：

1. 写一段最小脚本（**示例代码**）：

   ```python
   # 示例代码：观察 QuotaManager 的双语义
   from lmcache.v1.distributed.quota_manager import QuotaManager
   q = QuotaManager()
   q.set_quota("alice", 2 * 1024**3)   # alice 2 GiB
   # bob 未注册
   print("alice limit (allowlist):", q.get_limit_bytes("alice"))
   print("bob   limit (allowlist):", q.get_limit_bytes("bob"))          # 0
   print("bob   limit (default-quota, boot):", q.effective_limit_bytes("bob"))  # None
   q.set_default_limit_bytes(0)          # 外部控制器「武装白名单」
   print("bob   limit (default-quota, armed):", q.effective_limit_bytes("bob"))  # 0
   ```

2. 运行后对照 [quota_manager.py:77-128](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/quota_manager.py#L77-L128) 验证三个打印值。

3. **源码对照**：在仓库里用 `Grep` 搜 `get_limit_bytes(` 与 `effective_limit_bytes(`，确认前者只被 `_check_and_evict_by_cache_salt`（[eviction_controller.py:358](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/storage_controllers/eviction_controller.py#L358)，MP server 节点级）调用，后者只被 `compute_eviction_plan`（[eviction_manager.py:120](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/cache_control/eviction_manager.py#L120)，coordinator 舰队级）调用。

**需要观察的现象**：boot 阶段 `effective_limit_bytes("bob")` 是 `None`（豁免），而 `get_limit_bytes("bob")` 始终是 `0`。

**预期结果**：三个打印依次为 `2147483648`、`0`、`None`、`0`。

> 若环境无法运行，按上文 Grep 对照确认调用点即可——重点是结论：**MP server 用 allowlist（激进清零），coordinator 用 default-quota（冷启动豁免）**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `set_quota` 允许 `limit_bytes = 0`？它和「不注册」有何区别？

答案：`0` 与「不注册」在淘汰行为上等价（都导致下一轮全清），但 `0` 是**显式注册**——会出现在 `list_quotas()` 和 `has_quota()` 为 True。用途是状态汇报：运维想表达「这个租户我已评估过，决定给 0」而非「我还没来得及配置」。两者在 `get_limit_bytes` 下都返回 0，但 `has_quota` 能区分。

**练习 2**：coordinator 刚重启、配额表为空。若它误用了 `get_limit_bytes`（allowlist）而非 `effective_limit_bytes`，会发生什么？

答案：所有租户对它都是「未注册 → limit 0」，触发条件 `user_bytes >= watermark × 0 = 0` 对任何有数据的租户都成立，于是 coordinator 在第一个淘汰周期把**全舰队所有租户**的 L2 数据一次性清光——冷启动误杀。`effective_limit_bytes` 启动默认 `None`→豁免，正是为了避免这场灾难，直到外部控制器显式武装白名单。

---

### 4.4 删除上限（delete cap）调度：节点级批量 vs 舰队级分块分发

#### 4.4.1 概念说明

策略选好 victim、控制器决定删除后，最后一公里是「把这份 key 清单真正送到删除执行者」。这里出现两种完全不同的执行模型，本模块讲透它们的差异，并解释**删除上限（delete cap）**的由来。

**节点级（MP server 内）**：`L2EvictionController` 和被删的适配器在**同一进程**，删除就是一次 Python 方法调用 `adapter.delete(keys)`。这里没有「上限」概念——key 清单可以任意长，适配器内部自己消化。4.2 已经讲过。

**舰队级（coordinator）**：coordinator 与真正持有数据的 MP server 是**不同进程、不同机器**，删除只能通过 HTTP `DELETE /cache/objects` 发过去。而 MP server 的删除端点设了一道硬上限 `MAX_DELETE_BATCH = 10_000`——单个请求超过这个数直接 HTTP 400 拒绝。这就是「delete cap」。它存在的理由（见 [object_service.py:25-27](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/cache_control/object_service.py#L25-L27) 注释）：限制单请求体大小，防止一次调用独占适配器的 I/O 循环。

问题来了：把某租户配额清零（`limit=0`）会触发 `effective_ratio=1.0`，即「全量淘汰」——一个大租户轻易有几十万 key，远超 1 万的上限。coordinator 的 `L2EvictionManager.execute_evictions` 因此必须**把计划分块**，每块不超过 `MAX_DELETE_BATCH`，逐块发独立的 DELETE。

#### 4.4.2 核心流程

coordinator 端一轮淘汰（默认每 `eviction_check_interval=5s`）：

```
execute_evictions(registry, http_client):
  plan = compute_eviction_plan()           # {cache_salt: [ObjectKey,...]}，纯计算、无网络
  if not plan: return plan
  target = registry.random_instance()      # 随机挑一个在线 MP server 承接删除
  if target is None: return plan           # 没有在线 server，放弃本轮分发
  all_keys = plan 里所有 key 摊平
  for start in range(0, len(all_keys), MAX_DELETE_BATCH):
      chunk = all_keys[start : start+MAX_DELETE_BATCH]
      asyncio.create_task(_dispatch_eviction(DELETE /cache/objects, chunk))  # fire-and-forget
  return plan                               # 立即返回，不等分发完成
```

关键设计点：

- **纯计算与执行分离**：`compute_eviction_plan` 是纯函数（无网络、无副作用），只产出计划；`execute_evictions` 负责分发。这让计划可单独测试。
- **fire-and-forget + at-least-once**：DELETE 用 `asyncio.create_task` 异步发出，函数立即返回，不等结果。失败不重试，但因为底层 delete 幂等，at-least-once 语义安全。
- **LRU 清账滞后**：计划发出时**不清** LRU，要等对应的 `DELETE` 事件经 `POST /quota/events` 回流，才调 `on_remove` 清掉——避免「以为删了其实没删」的状态不一致。
- **随机选 server**：`registry.random_instance()` 随机挑一个在线实例承接删除（共享 L2 下任意实例都能删同一份远端数据）。

#### 4.4.3 源码精读

**删除上限的定义与拒绝逻辑**：

[lmcache/v1/multiprocess/cache_control/object_service.py:25-27](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/cache_control/object_service.py#L25-L27) 定义 `MAX_DELETE_BATCH = 10_000`，注释说明它是「请求体边界，防止单调用独占适配器 I/O 循环」。

[lmcache/v1/multiprocess/cache_control/object_service.py:157-163](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/cache_control/object_service.py#L157-L163) 是端点的硬校验：`len(keys) > MAX_DELETE_BATCH` 直接 `raise InvalidRequest(...)`（HTTP 400）。这就是 coordinator 必须分块的根本原因。

**coordinator 端计划计算**：

[lmcache/v1/mp_coordinator/cache_control/eviction_manager.py:103-157](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/cache_control/eviction_manager.py#L103-L157) `compute_eviction_plan`。注意它用 `effective_limit_bytes`（L120，default-quota 语义），`limit is None` 时 `continue`（L121-124，豁免未武装的租户）；触发条件 `current_bytes < watermark × limit` 时跳过（L125）；`effective_ratio = 1.0 if limit == 0 else self._eviction_ratio`（L128）；并且传 `key_eligible_filter=lambda key: key not in self._pin_counts`（L132）跳过被 pin 的 key。

**分块分发**：

[lmcache/v1/mp_coordinator/cache_control/eviction_manager.py:159-209](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/cache_control/eviction_manager.py#L159-L209) `execute_evictions`。核心分块循环：

```python
target = registry.random_instance()                            # L182 随机挑承接者
if target is None: ...; return plan                            # L183-189 无人承接则放弃
url = f"http://{target.ip}:{target.http_port}/cache/objects"   # L191
all_keys = [k for keys in plan.values() for k in keys]         # L192 摊平
for start in range(0, len(all_keys), MAX_DELETE_BATCH):        # L194 按 cap 分块
    chunk = all_keys[start : start + MAX_DELETE_BATCH]         # L195
    body = {"keys": [asdict(k.to_encoded_object_key()) for k in chunk]}  # L196 线缆形态
    task = asyncio.create_task(self._dispatch_eviction(...))   # L197 fire-and-forget
    self._in_flight_dispatches.add(task)                       # L207 登记在飞任务
    task.add_done_callback(self._in_flight_dispatches.discard) # L208 完成即摘除
```

`MAX_DELETE_BATCH` 直接从 MP server 的 `object_service` 导入（[eviction_manager.py:30-32](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/cache_control/eviction_manager.py#L30-L32)），保证两端「上限」是同一个常量，不会脱节。

[lmcache/v1/mp_coordinator/cache_control/eviction_manager.py:215-243](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/cache_control/eviction_manager.py#L215-L243) `_dispatch_eviction`：`http_client.request("DELETE", url, json=body)`（注释 L226-227 解释为何用 `request("DELETE", ...)` 而非 `delete(...)`——后者不支持 `json=`），`raise_for_status()` 把非 2xx 转异常，失败仅 `logger.warning` 不重试。

**HTTP 配额端点**（配合 default-quota）：

[lmcache/v1/mp_coordinator/http_apis/quota_api.py:60-97](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/http_apis/quota_api.py#L60-L97) 是「武装白名单」的入口 `PUT /quota/config` 与 `GET /quota/config`：把 `default_limit_gb` 写进 `QuotaManager.set_default_limit_bytes`。注意 L73-75 把 GiB 转 bytes、`None` 透传——正是 4.3 讲的三态。

[lmcache/v1/mp_coordinator/http_apis/quota_api.py:103-146](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/http_apis/quota_api.py#L103-L146) 是 per-salt CRUD：`PUT/DELETE/GET /quota/{cache_salt}`，其中 `_default` 哨兵映射空串 salt（L38-40，因为空串不能做 URL 路径参数）。`/quota/config` 路由声明在 `/quota/{cache_salt}` 之前（见设计文档说明），避免字面量 `config` 被当成 salt 捕获。

[lmcache/v1/mp_coordinator/http_apis/quota_api.py:216-246](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/http_apis/quota_api.py#L216-L246) `POST /quota/events`：MP server 上报的 STORE/LOOKUP/DELETE 事件在这里回流——STORE→`record_stored`+`on_store`，LOOKUP→`on_lookup`，DELETE→`record_evicted`+`on_remove`。这一步正是「LRU 清账滞后」里等待的那条回流。

#### 4.4.4 代码实践

**实践目标**：理解「全量淘汰（quota=0）会超 delete cap」，并用源码确认分块逻辑能正确把它拆开。

**操作步骤**（源码阅读型实践）：

1. 假设租户 alice 在 L2 有 35_000 个 key，配额被清零（`limit=0`）。在 [eviction_manager.py:128](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/cache_control/eviction_manager.py#L128) 确认 `effective_ratio = 1.0`，于是 `get_eviction_actions` 返回全部 35_000 个 key。
2. 在 [eviction_manager.py:194](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/cache_control/eviction_manager.py#L194) 手算分块：`range(0, 35000, 10000)` 产生 `0, 10000, 20000, 30000` 四个起点 → 切成 4 块（10000+10000+10000+5000）。
3. 确认每块都 ≤ `MAX_DELETE_BATCH`，各自一个 `asyncio.create_task`，共 4 个在飞 DELETE。
4. 反向验证：若**不分块**直接发 35_000 个 key，会在 [object_service.py:159-163](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/cache_control/object_service.py#L159-L163) 命中 `InvalidRequest` → HTTP 400，整批被拒。

**需要观察的现象**：分块后最大块恰为 10_000，刚好卡在 cap 内；最后一块为 5_000 的「零头」。

**预期结果**：35_000 个 key → 4 个 DELETE 任务，块大小 10_000/10_000/10_000/5_000。结论：delete cap 把「一次大删除」变成「多次有界删除」，保护了 MP server 的 I/O 公平性。

> 待本地验证：若有 coordinator + MP server 的测试环境，可参考 `tests/v1/mp_coordinator/test_eviction_manager.py` 跑一遍，观察实际发出的 DELETE 请求数与每块大小。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `execute_evictions` 在发出 DELETE 后不立即从 LRU 里删掉这些 key？

答案：DELETE 是 fire-and-forget，发出 ≠ 成功。若发出就清账，万一请求失败，这些 key 会从 LRU 消失但数据还在，未来再也无人记得去删它们（泄漏 + 超配额）。正确做法是等 MP server 真删后、`DELETE` 事件经 `POST /quota/events` 回流，才调 `on_remove` 清账。底层 delete 幂等保证了 at-least-once 的安全性。

**练习 2**：`MAX_DELETE_BATCH` 为什么由 MP server 的 `object_service` 定义、却被 coordinator 的 `eviction_manager` 导入复用，而不是各自定义一个？

答案：delete cap 是 **MP server 端点的契约**（它在 L159-163 强制拒绝超限请求），coordinator 是这个契约的**客户端**。客户端必须知道服务端的硬限制才能正确分块，因此从服务端模块导入同一个常量，保证两端绝不脱节。若各自定义，一旦服务端调整上限而客户端没跟上，客户端要么多发浪费（上限变小）、要么触发 400（上限变大）。

---

## 5. 综合实践

把四个模块串起来：**「一个租户挤占共享 L2，如何被 quota + IsolatedLRU + 控制器 + delete cap 协同化解」**。

场景：vLLM + LMCache 多租户部署，共享 S3 作 L2。租户 bob 突发写入 30 GiB，而他的配额只有 2 GiB；alice 规矩使用。`eviction_policy = IsolatedLRU`，`trigger_watermark = 1.0`，`eviction_ratio = 0.2`。

请完成以下「画图 + 推理」任务：

1. **数据流**：画出 `vLLM 请求(带 cache_salt="bob") → MP server → L2 adapter 存入 → on_l2_keys_stored → L2EvictionPolicy 桥接 → IsolatedLRU 按 "bob" 桶记账 + L2UsageManager 累计 bytes`。标注每个组件的职责。

2. **节点级淘汰（MP server 内）**：假设 bob 的写入落到某一台 MP server，该 server 的 `L2EvictionController` 走按租户分支。写出触发判断：

   \[\text{bob\_bytes} \geq \text{watermark} \times \text{limit} = 1.0 \times 2\,\text{GiB}\]

   当 bob 在该 server 累计超过 2 GiB 时触发。指出它调 `get_limit_bytes("bob")`（[eviction_controller.py:358](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/distributed/storage_controllers/eviction_controller.py#L358)）、`effective_ratio=0.2`（因 limit≠0）、`get_eviction_actions(0.2, cache_salt="bob")`，只丢 bob 桶里最旧的 20%，alice 的 key 纹丝不动。

3. **舰队级淘汰（coordinator）**：假设采用 coordinator 模式（fleet-wide 视角，跨多台 server 合计 bob 30 GiB）。指出 coordinator 用 `effective_limit_bytes("bob")=2 GiB`（[eviction_manager.py:120](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/cache_control/eviction_manager.py#L120)），同样超配额触发。区别在执行：coordinator 算出 victim 清单后，经 `DELETE /cache/objects` 分块（每块 ≤ `MAX_DELETE_BATCH=10_000`）fire-and-forget 发给某个在线 MP server。

4. **冷启动安全**：若上述 coordinator 刚重启、配额表为空但已被外部控制器重新写入了 bob 的 2 GiB 配额，alice 还没来得及写。问：alice 的现存数据会被误清吗？为什么？写出 `effective_limit_bytes("alice")` 此时的值，并指出需要外部控制器做哪一步才会开始清理 alice（答：`PUT /quota/config {"default_limit_gb": 0}` 武装白名单）。

5. **对比总结**：用一张表对比「无配额（纯 LRU）」与「IsolatedLRU + QuotaManager」两种部署，在「bob 突发写入」时 alice 的命运差异，并解释为什么 `cache_salt` 必须是 ObjectKey 的身份字段（参与 eq/hash）才能让这一切成立。

> 预期产出：一张数据流图 + 一张节点级/舰队级对照表 + 一段关于冷启动安全与身份字段的论证。这道题若能答全，说明你已经把本讲四个模块融会贯通。

## 6. 本讲小结

- **淘汰策略是纯数据结构**：`EvictionPolicy` 只记账（`on_keys_*`）和出主意（`get_eviction_actions`），不碰存储；决策与执行分离让同一套类服务 L1/L2。
- **三种实现各司其职**：`LRUEvictionPolicy`（全局单链）、`IsolatedLRUEvictionPolicy`（每 `cache_salt` 一链，`support_isolation=True`，强制要求 `cache_salt`）、`NoOpEvictionPolicy`（buffer-only 不记账），由 `CreateEvictionPolicy` 工厂按配置字符串显式路由。
- **控制器驱动调度**：`L1EvictionController` / `L2EvictionController` 每秒查水位、调策略、执行删除；策略的记账靠 `L1EvictionPolicy` / `L2EvictionPolicy` 桥接器把生命周期事件翻译成 `on_keys_*`，用组合而非多重继承。
- **QuotaManager 双语义是关键**：`get_limit_bytes`（allowlist，未注册=0，MP server 节点级用）vs `effective_limit_bytes`（default-quota，未注册=`None` 豁免，coordinator 舰队级用）。后者避免 coordinator 冷启动误杀全舰队。
- **按租户隔离淘汰**：`L2EvictionController` 的 `_check_and_evict_by_cache_salt` 用 `user_bytes >= watermark × limit` 判定，`limit=0` 时 `effective_ratio=1.0` 全清（白名单语义），否则按 `eviction_ratio` 比例清，且跨租户按目的地批量合并。
- **删除上限（delete cap）保护共享 I/O**：`MAX_DELETE_BATCH=10_000` 是 MP server 删除端点的硬契约，coordinator 的 `L2EvictionManager.execute_evictions` 据此把全量淘汰分块、fire-and-forget 异步分发，LRU 清账滞后到 `DELETE` 事件回流，靠底层幂等保证 at-least-once 安全。

## 7. 下一步学习建议

- **回头印证 u4-l3**：本讲的 `supports_global_eviction` / `usage_fraction` / `get_usage()` 都来自 L2 适配器基类，重读 u4-l3 的 `AdapterUsage` 与 `max_capacity_bytes` 会发现淘汰侧只是它们的消费者。
- **衔接 u3-l3 / u3-l5**：coordinator 的 `L2EvictionManager` 依赖 `InstanceRegistry`（u3-l3 的会员表，`random_instance()`）与 `EventBus`（u3-l5 的 `L2_KEYS_EVICTED` 事件）。结合那两讲能看到完整的舰队级协调与可观测闭环。
- **阅读设计文档**：[`docs/design/v1/distributed/l2_adapters/l2_per_user_quota.md`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/docs/design/v1/distributed/l2_adapters/l2_per_user_quota.md)（per-user 配额全貌，含 6-PR 拆分计划）与 [`docs/design/v1/mp_coordinator/l2_usage_and_eviction.md`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/docs/design/v1/mp_coordinator/l2_usage_and_eviction.md)（舰队级用量与淘汰，含故障模式表）。
- **跑测试**：`tests/v1/distributed/test_quota_manager.py`、`tests/v1/distributed/test_isolated_lru_eviction_policy.py`、`tests/v1/mp_coordinator/test_eviction_manager.py`、`tests/v1/mp_coordinator/test_quota_api.py` 分别覆盖本讲四个模块，是验证你理解的最佳参照。
- **后续讲义**：本讲的配额与淘汰为 u4-7（PD 分离与传输通道）中跨 worker 的 KV 生命周期管理打下基础；eviction 与 pin 的交互（`key_eligible_filter` / `_pin_counts`）也将在 PD 传输的「在用 key 不可淘汰」场景再次出现。
