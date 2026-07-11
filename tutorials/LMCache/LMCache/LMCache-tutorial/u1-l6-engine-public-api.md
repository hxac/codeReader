# LMCacheEngine 公共 API：store / retrieve / lookup

## 1. 本讲目标

本讲聚焦 LMCache 对外暴露的最核心类 `LMCacheEngine`，以及描述「用户可控缓存行为」的 `LMCacheModelRequest`。读完本讲，你应当能够：

- 读懂 `LMCacheEngine` 的构造参数与「构造 → post_init → 使用 → close」生命周期。
- 说明 `store` / `retrieve` / `lookup` 三个主链路 API 的输入、输出与语义。
- 理解 `lookup` 返回的「前缀命中 token 数」含义，以及配套的 `pin` / `lookup_unpin` 引用计数机制。
- 区分 `LMCacheModelRequest`（用户意图的结构化描述）与实际驱动行为的 `request_configs` 字典。

本讲是 u1 单元的收尾：u1-l5 讲了配置如何加载，本讲回答「拿到配置之后，引擎到底对外提供哪些调用」。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**KV cache 与「算完即扔」。** 推理引擎在做 attention 时，会为每个历史 token 计算并保存一对 Key/Value 向量，这就是 KV cache。它随上下文线性增长，是非常昂贵的中间结果。传统引擎在请求结束后就丢弃它，下一次遇到相同前缀（比如同样的系统提示词）还要重新算一遍 prefill。LMCache 的价值正是把这部分「算完即扔」的资产保留下来复用。

**三大主链路。** LMCache 对外的核心动作只有三个：

| 动作 | 方向 | 典型时机 |
|------|------|----------|
| `store` | GPU KV → LMCache | prefill 算完后，把新算出的 KV 存下来 |
| `lookup` | LMCache → 计数 | 调度前，先问「这段 token 前缀命中了多少」 |
| `retrieve` | LMCache → GPU KV | 真正把命中的 KV 灌回引擎的 KV buffer |

数据流是 `store → lookup → retrieve`，与 u1-l1 介绍的「算完即扔变资产」一致。

**chunk 与 mask。** LMCache 不会把整条序列当作一个整体来存取，而是按 `chunk_size`（见 u1-l5 的配置）切成一个个 chunk，每个 chunk 用一个 `CacheEngineKey` 唯一标识。同时，引擎传进来的 token 序列常常带一个布尔 `mask`，约定形如 `FFFFFTTTTTTT`：

- `True` 表示「这个 token 需要 match（参与存取）」。
- `False` 永远在前缀位置，表示「这段已经被引擎算过/已有缓存，跳过」。

这个 mask 约定在 `store` / `retrieve` / `lookup` 三个 API 的 docstring 里反复出现，是理解参数语义的关键。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `lmcache/v1/cache_engine.py` | 定义 `LMCacheEngine`（三大 API + 生命周期）和工厂 `LMCacheEngineBuilder`。本讲绝对主角。 |
| `lmcache/v1/cache_interface.py` | 定义 `LMCacheModelRequest`，描述用户对单次请求的缓存意图（是否存、TTL）。 |
| `lmcache/v1/token_database.py` | `process_tokens` 把 token 序列切分成 `(start, end, key)` chunk 流，三大 API 都依赖它。 |
| `lmcache/utils.py` | 定义 `CacheEngineKey`，是 chunk 的唯一标识（模型名/world_size/worker_id/chunk_hash/dtype…）。 |

后续单元（u2）会深入 `storage_manager`、`gpu_connector` 等被本讲 API 调用的内部组件；本讲只把它们当作「黑盒下游」来看。

## 4. 核心概念与源码讲解

### 4.1 LMCacheEngine 的构造与生命周期

#### 4.1.1 概念说明

`LMCacheEngine` 是 LMCache 的门面类（facade）。它的 docstring 写得很清楚：存的时候把 GPU 上的 KV cache 转成驻留在 CPU 的 `MemoryObj`，再异步写入各级 `StorageBackend`；取的时候反过来，从后端取出 `MemoryObj`，再由专门的 `GPUConnector` 转回引擎能理解的 GPU KV 布局。

它本身不直接管「磁盘怎么读写」「张量怎么搬运」，这些职责被委托给三个协作者：

- `token_database`：负责把 token 切成 chunk 并生成 key。
- `gpu_connector`：负责 GPU KV buffer 与 `MemoryObj` 之间的格式转换。
- `storage_manager`：负责实际的分级存储（CPU/磁盘/远端）。

#### 4.1.2 核心流程

`LMCacheEngine` 的生命周期分四步：

```text
1. 构造 __init__       保存 config/metadata/connector，但 storage_manager 暂不创建
2. post_init           按需创建 storage_manager（延迟初始化）
3. 使用                反复调用 store / retrieve / lookup / ...
4. close               关闭 lmcache_worker、storage_manager、hidden_state_store
```

为什么要把 `storage_manager` 的创建推迟到 `post_init`？因为多卡/多 worker 场景下，是否在本 rank 上创建存储后端取决于 `save_only_first_rank`、`use_layerwise`、`lookup_server_worker_ids` 等条件，这些条件在构造时已确定，但实际分配资源放到 `post_init` 可以让集成层（如 vLLM connector）在更晚的时机、拿到更完整信息后再触发。

#### 4.1.3 源码精读

构造函数签名（注意参数列表）：[lmcache/v1/cache_engine.py:100-108](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/cache_engine.py#L100-L108)

```python
def __init__(
    self,
    config: LMCacheEngineConfig,
    metadata: LMCacheMetadata,
    token_database: TokenDatabase,
    gpu_connector: Optional[GPUConnectorInterface],
    broadcast_fn: Callable[[torch.Tensor, int], None],
    broadcast_object_fn: Callable[[Any, int], Any],
):
```

- `config`：u1-l5 讲过的引擎配置对象。
- `metadata`：模型层面的元信息（kv_shape、kv_dtype、world_size、worker_id、是否 MLA 等）。
- `gpu_connector`：可为 `None`（CLI/诊断场景），但 `store`/`retrieve` 时会断言非空。
- `broadcast_fn` / `broadcast_object_fn`：多卡广播回调，用于 `save_only_first_rank` 模式下把 leader rank 的 KV 同步给其它 rank。

构造函数里有两个本讲要记住的状态字段：[lmcache/v1/cache_engine.py:214-219](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/cache_engine.py#L214-L219)

```python
# NOTE(ApostaC): we haven't support lookup-cache yet
self.lookup_cache: dict[CacheEngineKey, Any] = {}
# lookup_id -> {location -> [pinned keys]}
self.lookup_pins: dict[str, dict[str, list]] = defaultdict(...)
```

`lookup_pins` 是 `pin` 机制的「账本」：每次带 `pin=True` 的 `lookup` 会把命中的 key 记在这里，等 `retrieve` 用完后再由 `lookup_unpin` 释放。这是 4.4 节的关键。

`post_init` 决定是否在本 rank 上真正建起存储后端：[lmcache/v1/cache_engine.py:301-333](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/cache_engine.py#L301-L333)（核心是其中 [324-330](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/cache_engine.py#L324-L330) 行创建 `StorageManager`）。

通常你不直接 `LMCacheEngine(...)`，而是用工厂 `LMCacheEngineBuilder.get_or_create`，它会按 `instance_id` 做单例缓存，并对同一 id 重复传入不同 config 报 `ValueError`：[lmcache/v1/cache_engine.py:2092-2143](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/cache_engine.py#L2092-L2143)。配套还有 `get(instance_id)` 取实例、`destroy(instance_id)` 关闭并清理。

`close` 负责释放资源，按 hidden_state_store → lmcache_worker → storage_manager 的顺序关闭，且每步都 `try/except` 防止一处失败影响其余清理：[lmcache/v1/cache_engine.py:1614-1641](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/cache_engine.py#L1614-L1641)。

#### 4.1.4 代码实践

**目标：** 用工厂方法理解实例的生命周期与单例约束。

**步骤：**

1. 打开 `cache_engine.py`，定位 `LMCacheEngineBuilder.get_or_create`（2092 行起）。
2. 阅读它的 `if instance_id not in cls._instances` 分支与 `else` 分支，注意 `else` 里对 config/metadata 一致性的校验。
3. 写一段伪代码描述生命周期（无需真实 GPU）：

```python
# 示例代码（仅描述调用形态，未在 GPU 环境运行）
engine = LMCacheEngineBuilder.get_or_create(
    instance_id="my_vllm",
    config=config,            # 来自 u1-l5 的 load_engine_config_with_overrides
    metadata=metadata,
    gpu_connector=connector,  # 来自 u2-l2 的 GPU 连接器
    broadcast_fn=bcast,
    broadcast_object_fn=bcast_obj,
)
engine.post_init()           # 触发 storage_manager 创建
# ... 反复 store / lookup / retrieve ...
LMCacheEngineBuilder.destroy("my_vllm")  # 等价于 engine.close() + 清理单例表
```

**观察现象：** 在伪代码里标注哪一步对应 `__init__`、哪一步真正创建 `storage_manager`、哪一步释放。

**预期结果：** 你应当能说清楚——构造阶段不建后端，`post_init` 才建，`destroy`/`close` 负责释放。真实运行结果：待本地验证（需要真实模型 metadata 与 GPU 连接器）。

#### 4.1.5 小练习与答案

**练习 1：** 为什么 `storage_manager` 的创建放在 `post_init` 而不是 `__init__`？

**参考答案：** 因为是否在本 rank 创建后端取决于 `save_only_first_rank`、`use_layerwise`、`lookup_server_worker_ids` 等运行期条件；把资源分配推迟到 `post_init`，让集成层（vLLM connector 等）在拿到更完整信息后再触发，避免在构造期就为不该持有的 rank 分配资源。

**练习 2：** 对同一个 `instance_id` 用不同 config 调两次 `get_or_create` 会发生什么？

**参考答案：** 第一次创建并缓存；第二次命中已有实例，但检测到 config/metadata 不一致，抛出 `ValueError`（见 [2135-2142](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/cache_engine.py#L2135-L2142) 行）。

---

### 4.2 store：把 KV cache 写入缓存

#### 4.2.1 概念说明

`store` 的职责是：拿到一段 token（或其 hash），连同引擎算出的 KV cache，切成 chunk 存进 LMCache。它是「让算完即扔变资产」的写入入口。

#### 4.2.2 核心流程

`store` 的执行步骤：

```text
1. 健康检查 is_healthy()         不健康直接 return（不抛错，降级）
2. 断言 gpu_connector 非空        没有 connector 无法从 GPU 取 KV
3. _is_passive() 被动 rank 跳过   save_only_first_rank 场景下非 leader 不写
4. 计算 num_to_store_tokens      由 mask / tokens / (hashes+offsets) 三选一
5. freeze 模式跳过                冻结期只读不写，保护热缓存
6. process_tokens 切 chunk        得到 (start, end, key) 流
7. 逐 chunk allocate MemoryObj    内存不足则提前 break
8. batched_from_gpu               GPU KV buffer → CPU MemoryObj
9. batched_put                    MemoryObj → 各级 StorageBackend
10. 统计 + 日志
```

注意第 7 步：内存压力下 `allocate` 会返回 `None`，此时 `store` **不抛错**，而是「能存多少存多少」并打 warning 后 break（[507-514](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/cache_engine.py#L507-L514) 行）。这是 LMCache「不阻塞推理」设计哲学的体现。

#### 4.2.3 源码精读

函数签名与 docstring：[lmcache/v1/cache_engine.py:388-414](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/cache_engine.py#L388-L414)

```python
def store(
    self,
    tokens: Optional[Union[torch.Tensor, list[int]]] = None,
    hashes: Optional[List[int]] = None,
    offsets: Optional[List[int]] = None,
    mask: Optional[torch.Tensor] = None,
    **kwargs,
) -> None:
```

- `tokens` 与 `hashes` 二选一（代码末尾有断言 `Either 'tokens' or 'hashes' must be provided`）。
- `mask`：前述 `FFFFFTTTTTTT` 约定，`False` 必须在前缀。
- `**kwargs`：透传给 `gpu_connector`，通常包含 paged KV buffer、page table、`slot_mapping` 等引擎专属信息。

健康检查与降级（不健康只是 warning + return）：[lmcache/v1/cache_engine.py:415-418](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/cache_engine.py#L415-L418)

token 切分主循环（核心三步：allocate → 收集 → 后续批量搬运）：[lmcache/v1/cache_engine.py:485-521](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/cache_engine.py#L485-L521)

```python
for start, end, key in self.token_database.process_tokens(
    tokens, hashes, offsets, mask, request_configs=request_configs,
):
    num_tokens = end - start
    kv_shapes = self.metadata.get_shapes(num_tokens)
    memory_obj = self.storage_manager.allocate(kv_shapes, ...)
    if memory_obj is None:
        break  # 内存不足，能存多少存多少
    keys.append(key); memory_objs.append(memory_obj)
```

这里的 `(start, end, key)` 由 `ChunkedTokenDatabase.process_tokens` 产出（[token_database.py:368](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/token_database.py#L368) 起），`key` 是 `CacheEngineKey`（[utils.py:399-407](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/utils.py#L399-L407)）。

真正搬运与写入：[lmcache/v1/cache_engine.py:557-569](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/cache_engine.py#L557-L569)

```python
self.gpu_connector.batched_from_gpu(memory_objs, starts, ends, **kwargs)  # GPU→CPU
self.storage_manager.batched_put(keys, memory_objs, transfer_spec=..., location=self.store_location)
```

`store_location`（[198](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/cache_engine.py#L198) 行 `self.store_location = config.store_location`）决定写入哪一层后端，PD 分离场景下尤其重要（见 u4-l7）。

#### 4.2.4 代码实践

**目标：** 通过阅读 docstring + examples，理解一次 `store` 的完整入参。

**步骤：**

1. 打开 [examples/cache_interface/README.md](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/examples/cache_interface/README.md)，这是「用户可控缓存」示例，背后调用的就是 `store`。
2. 阅读其中正常请求的日志期望：`INFO: Storing KV cache for 13 out of 13 tokens`。
3. 对照 `store` 源码，回答：那「13 out of 13」里的两个数字分别来自代码里的哪个变量？

**需要观察的现象：** 日志里 `Stored %d out of total %d tokens`（[577-589](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/cache_engine.py#L577-L589) 行）。

**预期结果：** 第一个数是 `tot_token_num`（实际成功切分并分配了 MemoryObj 的 token 数），第二个数是 `num_to_store_tokens`（请求要求存储的总 token 数）。两者不一致通常是因为内存压力触发了 break。真实运行结果：待本地验证（需要启动 vLLM + LMCache）。

#### 4.2.5 小练习与答案

**练习 1：** 当 CPU 内存吃紧、`allocate` 返回 `None` 时，`store` 会抛异常吗？

**参考答案：** 不会。它会打 warning 并 `break`，把已经分配好的 chunk 继续存下去（[507-514](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/cache_engine.py#L507-L514) 行），保证不阻塞推理。

**练习 2：** `mask` 形如 `FFFFFTTTTTTT`，如果 `False` 出现在中间会怎样？

**参考答案：** docstring 明确要求 `Falses will ALWAYS be at the PREFIX`；若违反该约定，`process_tokens` 的切分语义会错乱（mask False 部分会被跳过），属于调用方违约，行为不符合预期。

---

### 4.3 retrieve：把 KV cache 取回并灌回 GPU

#### 4.3.1 概念说明

`retrieve` 是 `store` 的逆操作：给定 token，把命中的 KV 从各级后端取出来，灌回引擎的 GPU KV buffer。它的返回值不是 KV 本身，而是一个**布尔 mask**，告诉你「哪些位置的 token 被成功取回了」。

#### 4.3.2 核心流程

```text
1. 健康检查            不健康 → 返回全 False 的 mask（不抛错）
2. 断言 gpu_connector
3. 计算 num_required_tokens
4. 初始化 ret_mask = zeros(len(tokens))
5. _process_tokens_internal（或 async 版）拿到命中的 reordered_chunks
6. （save_only_first_rank 时）广播/接收 MemoryObj
7. batched_to_gpu       MemoryObj → GPU KV buffer
8. 释放 MemoryObj        remove_after_retrieve 则 remove；否则 unpin + ref_count_down
9. 返回 ret_mask（bool，CPU）
```

关键点：第 5 步是「真的去后端拿数据」，第 7 步是「把数据搬到 GPU」。`retrieve` 把这两步串在一起，集成层只需调一次。

#### 4.3.3 源码精读

签名与 docstring（注意返回值是 bool mask）：[lmcache/v1/cache_engine.py:780-806](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/cache_engine.py#L780-L806)

健康检查返回全 False mask（与 `store` 的「return None」不同，这里要返回一个合法 mask 让上层继续）：[lmcache/v1/cache_engine.py:807-810](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/cache_engine.py#L807-L810)

取数据 + 灌 GPU：[lmcache/v1/cache_engine.py:877-912](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/cache_engine.py#L877-L912)

```python
if len(reordered_chunks) > 0:
    _, memory_objs, starts, ends = zip(*reordered_chunks)
    self.gpu_connector.batched_to_gpu(memory_objs_for_togpu, list(starts), list(ends), **kwargs)
```

注意 docstring 里 [944-956](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/cache_engine.py#L944-L956) 行的注释：取回的 token 数可能**大于**实际需要的 token 数（因为 chunk 与 page 边界不对齐），这是正常现象。

资源释放（理解 `remove_after_retrieve` 与 `unpin` 的分叉）：[lmcache/v1/cache_engine.py:925-937](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/cache_engine.py#L925-L937)

```python
for key, memory_obj, _, _ in reordered_chunks:
    if self.remove_after_retrieve and not self._is_passive():
        self.storage_manager.remove(key, self.retrieve_locations)  # PD receiver：取完即删
    else:
        if memory_obj.is_pinned:
            memory_obj.unpin()
        memory_obj.ref_count_down()  # 引用计数 -1
```

`remove_after_retrieve` 仅在 PD 分离的 receiver 端为 True（[194](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/cache_engine.py#L194) 行），表示「KV 传给 decode worker 后就不再保留」。普通场景则是「解 pin + 引用计数减一」，让内存池回收。

#### 4.3.4 代码实践

**目标：** 理解 `retrieve` 返回 mask 的形状与含义。

**步骤：**

1. 阅读 docstring 的 `:return:`（[801-803](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/cache_engine.py#L801-L803) 行）。
2. 在源码里找到 `ret_mask` 的初始化（[836](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/cache_engine.py#L836) 行）与赋值点（在 `_process_tokens_internal` 里 `ret_mask[start:end] = True`）。
3. 用伪代码演示调用：

```python
# 示例代码
ret_mask = engine.retrieve(tokens=prompt_tokens, mask=need_mask,
                           slot_mapping=..., block_table=..., ...)
# ret_mask 是与 tokens 等长的 bool 张量；True 表示该 token 的 KV 已从缓存灌回 GPU
hit_tokens = int(ret_mask.sum())
```

**需要观察的现象：** 日志 `Retrieved %d out of %d required tokens`（[958-969](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/cache_engine.py#L958-L969) 行）。

**预期结果：** 你应当能解释为何 retrieved 可能略大于 required（chunk/page 边界对齐）。真实运行结果：待本地验证。

#### 4.3.5 小练习与答案

**练习 1：** `retrieve` 在引擎不健康时返回什么？为什么不是 `None`？

**参考答案：** 返回 `torch.zeros(len(tokens), dtype=torch.bool)`（[807-810](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/cache_engine.py#L807-L810) 行）。因为上层（集成层）依赖这个 mask 判断「哪些 token 已就绪、哪些要重算」，返回 `None` 会破坏调用契约，所以降级为「全未命中」。

**练习 2：** 普通 retrieve 之后，取出的 `MemoryObj` 是如何被回收的？

**参考答案：** 对每个对象先 `unpin()`（若被 pin），再 `ref_count_down()`（[934-937](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/cache_engine.py#L934-L937) 行）；引用计数归零后由内存池回收。

---

### 4.4 lookup 与 lookup_unpin：查询命中与 pin 机制

#### 4.4.1 概念说明

`lookup` 只「问」不「取」：给定 token，返回它们在 LMCache 里**前缀命中了多少个 token**。它的价值在于让调度器在真正 `retrieve` 之前就知道「这段 prompt 有多少能复用」，从而决定要不要把请求识别为「部分命中」、要不要触发异步预取。

`lookup` 还支持 `pin=True`：把命中的 KV「钉住」，保证它们在 `retrieve` 之前不会被淘汰。配套的 `lookup_unpin` 在用完后释放。

#### 4.4.2 核心流程

`lookup` 的「前缀命中」语义（非 layerwise 分支）：

```text
1. 健康检查            不健康 → 返回 0
2. search_range 默认取 retrieve_locations
3. process_tokens 切出 (start, end, key) 列表
4. batched_contains(keys, search_range, pin) → (hit_chunks, block_mapping)
5. pin=True 时把 block_mapping 记入 lookup_pins[lookup_id]
6. 遍历 chunk_info：前 hit_chunks 个连续命中则 res=end，第一个未命中就 return res
```

命中数是一个**前缀长度**——只在连续命中的 chunk 上累加，一旦某个 chunk 没命中就停止。这是因为复用必须是连续前缀，中间断档无法复用。

#### 4.4.3 源码精读

签名与 docstring（返回 `int`：前缀命中 token 数）：[lmcache/v1/cache_engine.py:1130-1164](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/cache_engine.py#L1130-L1164)

```python
def lookup(
    self,
    tokens=None, hashes=None, offsets=None,
    search_range: Optional[List[str]] = None,
    lookup_id: Optional[str] = None,
    pin: bool = False,
    request_configs: Optional[dict] = None,
) -> int:
```

- `search_range`：限定查哪些后端，必须是 `["LocalCPUBackend", "LocalDiskBackend"]` 的子集；`None` 表示查全部（默认取 `self.retrieve_locations`）。
- `pin`：为 True 时**必须**提供 `lookup_id`。

核心查询与命中计数（非 layerwise 分支）：[lmcache/v1/cache_engine.py:1217-1243](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/cache_engine.py#L1217-L1243)

```python
hit_chunks, block_mapping = self.storage_manager.batched_contains(
    keys, search_range, pin
)
if pin and block_mapping:
    self.lookup_pins[lookup_id] = block_mapping      # 记账，供 retrieve/unpin 使用
for idx, (start, end, key) in enumerate(chunk_info_list):
    if idx < hit_chunks:
        res = end          # 连续命中：累加前缀长度
        continue
    return res             # 第一个未命中：立即返回当前前缀长度
```

`lookup_unpin` 释放被 pin 的 key（若无 pin 记录则尝试清理异步预取的对象）：[lmcache/v1/cache_engine.py:1544-1555](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/cache_engine.py#L1544-L1555)

```python
def lookup_unpin(self, lookup_id: str) -> None:
    if lookup_id in self.lookup_pins:
        for location, keys in self.lookup_pins.pop(lookup_id).items():
            self.storage_manager.batched_unpin(keys, [location])
    elif ...:  # 异步加载路径
        self.cleanup_memory_objs(lookup_id)
```

这就是 pin 的完整闭环：`lookup(pin=True, lookup_id=X)` 记账 → `retrieve` 取用 → `lookup_unpin(X)` 解锁。如果忘记 `lookup_unpin`，被 pin 的 KV 永远不会被淘汰，相当于内存泄漏。

#### 4.4.4 代码实践

**目标：** 在源码里走一遍 `lookup → pin → unpin` 的状态变化。

**步骤：**

1. 在 `__init__` 里找到 `self.lookup_pins` 的定义（[217-219](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/cache_engine.py#L217-L219) 行），确认它是 `lookup_id -> {location -> [keys]}` 的嵌套字典。
2. 在 `lookup` 里找到写入 `lookup_pins` 的两处（layerwise [1213](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/cache_engine.py#L1213) 行、非 layerwise [1235](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/cache_engine.py#L1235) 行）。
3. 在 `lookup_unpin` 里找到「弹出并 batched_unpin」的逻辑（[1547-1548](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/cache_engine.py#L1547-L1548) 行）。

**需要观察的现象：** 用伪代码标注字典状态变化：

```python
# 示例代码
n1 = engine.lookup(tokens=T, search_range=["LocalCPUBackend"], lookup_id="req1", pin=True)
# 此刻 engine.lookup_pins["req1"] == {"LocalCPUBackend": [key0, key1, ...]}
engine.retrieve(tokens=T, ...)          # 取用
engine.lookup_unpin("req1")             # lookup_pins 弹出 "req1"，keys 被 unpin
```

**预期结果：** `lookup` 返回的前缀命中数 `n1` 应等于 `T` 中连续命中的 token 数；`lookup_unpin` 后 `lookup_pins` 不再包含 `"req1"`。真实运行结果：待本地验证。

#### 4.4.5 小练习与答案

**练习 1：** 假设 chunk_size=256，某 prompt 命中第 0、1 个 chunk 但第 2 个 chunk 未命中，`lookup` 返回多少？

**参考答案：** 返回 512（前两个 chunk 的 `end` 累加，即 `res = end` 取到第二个 chunk 的结束位置）。第三个未命中即停止累加（[1236-1240](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/cache_engine.py#L1236-L1240) 行）。

**练习 2：** 用 `pin=True` 调 `lookup` 但忘了 `lookup_unpin`，会有什么后果？

**参考答案：** 被 pin 的 KV 的引用不会被释放，淘汰策略无法回收它们，长期看相当于内存泄漏，热缓存空间会被逐步挤占。

---

### 4.5 LMCacheModelRequest：用户可控的缓存行为

#### 4.5.1 概念说明

`LMCacheModelRequest` 是「用户对单次请求缓存意图」的结构化描述：要不要存（`store_cache`）、存活多久（`ttl`）。它体现的设计是——缓存行为不应只在全局配置里写死，还应能按请求动态控制（比如「这条一次性问答不要进缓存」）。

需要诚实说明：在当前 v1 引擎里，真正驱动 `store`/`lookup` 行为的是 `**kwargs` 里透传的 `request_configs` 字典（如 `lmcache.skip_save`、`lmcache.tag.*`），而 `LMCacheModelRequest` 这个 struct 目前更多是「意图的规范描述」。两者关系如下：

```text
HTTP 请求里的 kv_transfer_params（如 lmcache.skip_save）
        │  （集成层翻译）
        ▼
request_configs: dict  ──► 透传进 store/lookup 的 **kwargs
        │                        │
        │                        ▼
        │              token_database.process_tokens / CacheEngineKey
        ▼
LMCacheModelRequest（store_cache / ttl 的结构化意图描述）
```

#### 4.5.2 核心流程

`LMCacheModelRequest` 本身是一个轻量数据结构（基于 `msgspec.Struct`），没有复杂流程。`request_configs` 的流转则是：

```text
1. 用户在 HTTP 请求里带 kv_transfer_params（例如 {"lmcache.skip_save": true}）
2. 集成层（如 vllm_v1_adapter）把它收进 request_configs
3. request_configs 作为 kwarg 透传进 engine.store / engine.lookup
4. process_tokens 用它生成带 tag 的 CacheEngineKey；store 据此决定是否真正写入
```

#### 4.5.3 源码精读

`LMCacheModelRequest` 定义：[lmcache/v1/cache_interface.py:9-20](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/cache_interface.py#L9-L20)

```python
class LMCacheModelRequest(
    msgspec.Struct,
    array_like=True,
    omit_defaults=True,
):
    """User-provided information to control the cache behavior."""
    store_cache: bool = True   # Whether to store the cache
    ttl: Optional[float] = None  # Time to live
```

- `msgspec.Struct`：比 `dataclass` 更快、内存更省的结构化类型，`omit_defaults=True` 让序列化时省略默认值。
- `store_cache=True`：默认即存；设为 False 表示「这次不要存」。
- `ttl`：可选的存活时间（秒），过期后缓存失效。

`store` 接收 `request_configs` 并透传：[lmcache/v1/cache_engine.py:479-491](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/cache_engine.py#L479-L491)

```python
request_configs = kwargs.get("request_configs")
...
for start, end, key in self.token_database.process_tokens(
    tokens, hashes, offsets, mask, request_configs=request_configs,
):
```

`request_configs` 还会进入 `CacheEngineKey`（`lmcache.tag.*` 前缀的键会被解析成 tag，见 [utils.py:410-421](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/utils.py#L410-L421)），从而影响 key 的相等性/哈希——不同 tag 的请求互不混淆。

集成层把 `lmcache.skip_save` 翻译成是否跳过存储的例子：[lmcache/integration/vllm/vllm_v1_adapter.py:339-344](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/integration/vllm/vllm_v1_adapter.py#L339-L344)

```python
request_skip = (tracker.request_configs or {}).get("lmcache.skip_save", False)
skip_save = tracker.disagg_spec is None and (
    tracker.skip_save or (tracker.num_saved_tokens > 0 and input_token_len < chunk_boundary)
)
```

#### 4.5.4 代码实践

**目标：** 跑通 examples 里的「用户可控缓存」示例，观察 `store_cache=False` 的效果。

**步骤：**

1. 按 [examples/cache_interface/README.md](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/examples/cache_interface/README.md) 用 vLLM 启动一个带 LMCache connector 的服务（需 1 张 GPU）。
2. 先发一个**普通**请求（不带 `kv_transfer_params`），观察日志 `Storing KV cache for 13 out of 13 tokens`。
3. 再发一个带 `"kv_transfer_params": {"lmcache.skip_save": true}` 的请求，观察日志 `User has specified not to store the cache (store_cache: false)`。

**需要观察的现象：** 第二次请求不会产生 store 日志，对应 `LMCacheModelRequest.store_cache=False` 的语义。

**预期结果：** 两次请求都正常返回生成结果，但只有第一次被写入缓存。真实运行结果：待本地验证（需 GPU 与模型权重）。

> 说明：本实践依赖 GPU 环境。若本地无 GPU，可退化为「源码阅读型实践」——在 `vllm_v1_adapter.py` 里跟踪 `request_configs` 如何从 HTTP 参数流到 `engine.store`，画出数据流图。

#### 4.5.5 小练习与答案

**练习 1：** `LMCacheModelRequest` 与 `request_configs` 字典是什么关系？

**参考答案：** `LMCacheModelRequest`（`store_cache`/`ttl`）是用户缓存意图的结构化规范描述；当前 v1 引擎实际驱动行为的是 `request_configs` 字典（如 `lmcache.skip_save`），集成层负责把 HTTP 参数翻译进这个字典并透传给 `store`/`lookup`。

**练习 2：** 为什么 `request_configs` 里 `lmcache.tag.*` 前缀的键会影响缓存命中？

**参考答案：** 因为它们会被解析进 `CacheEngineKey.tags`（[utils.py:410-421](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/utils.py#L410-L421)），而 tag 参与 key 的哈希与相等判断，所以不同 tag 的请求即便 token 相同也会得到不同 key、互不复用。

---

## 5. 综合实践

把三大 API 串起来，完成一次「存一次 prefix → 第二次 lookup 命中 → retrieve 取回」的端到端跟踪。

**任务：**

1. 准备一段固定 prefix（比如系统提示词），用伪代码描述三轮调用：

```python
# 示例代码（描述调用形态）
PROMPT = [101, 102, 103, ...]   # 固定前缀 token

# 第一轮：cold，store 把 KV 写入
engine.store(tokens=PROMPT, mask=all_true_mask, slot_mapping=..., block_table=...)

# 第二轮：warm，先 lookup 问命中多少
hit = engine.lookup(tokens=PROMPT, search_range=["LocalCPUBackend"],
                    lookup_id="r2", pin=True)
assert hit == len(PROMPT)        # 期望前缀全部命中

# 第三轮：retrieve 把命中的 KV 灌回 GPU，用完 unpin
ret_mask = engine.retrieve(tokens=PROMPT, mask=all_true_mask,
                           slot_mapping=..., block_table=...)
assert int(ret_mask.sum()) == len(PROMPT)
engine.lookup_unpin("r2")
```

2. 对照源码，在每一行旁边标注它命中了 `cache_engine.py` 的哪个函数、调用了哪个下游（`token_database` / `storage_manager` / `gpu_connector`）。
3. 回答两个问题：
   - 如果第二论的 `lookup` 漏掉了 `pin=True`，第三轮 `retrieve` 还能成功吗？为什么？
   - 如果第三轮漏掉了 `lookup_unpin("r2")`，长期运行会出现什么问题？

**预期答案要点：**

- `retrieve` 不依赖 pin 也能取数据（pin 只是防淘汰），但高并发/内存压力下未 pin 的 KV 可能在 `lookup` 与 `retrieve` 之间被淘汰，导致 `retrieve` 实际取回数小于 `lookup` 报告数。
- 漏掉 `lookup_unpin` 会导致被 pin 的 KV 永不回收（见 4.4 节），热缓存空间被逐步挤占。

真实运行结果：待本地验证（需 GPU + 模型 + 连接器）；无 GPU 时退化为源码跟踪作业。

## 6. 本讲小结

- `LMCacheEngine` 是 LMCache 的门面类，三大主链路 API 是 `store`（写）、`lookup`（问命中）、`retrieve`（取）；生命周期为 `__init__ → post_init → 使用 → close/destroy`。
- 构造期不创建存储后端，`post_init` 才按 rank 条件创建 `storage_manager`；推荐用 `LMCacheEngineBuilder.get_or_create` 单例工厂创建实例。
- `store` 把 token 切成 chunk、为每个 chunk 分配 `MemoryObj`、从 GPU 搬到 CPU、再 `batched_put` 写入后端；内存不足时「能存多少存多少」而不抛错。
- `retrieve` 返回与 token 等长的 **bool mask**（不是 KV 本身），不健康时降级为全 False；用完按 `remove_after_retrieve` 或 `unpin + ref_count_down` 释放对象。
- `lookup` 返回**前缀命中 token 数**（连续命中才累加），`pin=True` + `lookup_id` 把命中 key 记入 `lookup_pins`，必须配对 `lookup_unpin` 释放，否则内存泄漏。
- `LMCacheModelRequest`（`store_cache`/`ttl`）是用户缓存意图的结构化描述，当前 v1 引擎实际靠 `request_configs` 字典（如 `lmcache.skip_save`、`lmcache.tag.*`）驱动行为并影响 `CacheEngineKey`。

## 7. 下一步学习建议

本讲把 `store/retrieve/lookup` 当作门面看，下游用的 `storage_manager`、`gpu_connector`、`token_database` 都是黑盒。接下来建议：

- **u2-l2 GPU 连接器层**：搞清楚 `batched_from_gpu` / `batched_to_gpu` 如何在不同引擎（vLLM/SGLang）的 KV 布局与 LMCache 的 `MemoryObj` 之间转换。
- **u2-l3 存储后端层次结构** 与 **u2-l4 StorageManager**：理解 `batched_put` / `batched_get` / `batched_contains` 背后的 CPU/磁盘/远端分层与异步调度。
- **u2-l5 vLLM 集成适配器**：看真实集成层如何把引擎的一次 forward 翻译成对本讲三大 API 的调用顺序（`get_num_new_matched_tokens` → `lookup`，`request_finished` → `store` 等）。

读完 u2 这几篇，你就能把「一次推理请求如何穿过 LMCache」从门面一路追到底层存储与 GPU 搬运。
