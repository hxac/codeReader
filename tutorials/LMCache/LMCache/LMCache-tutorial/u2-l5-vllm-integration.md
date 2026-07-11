# vLLM 集成适配器

## 1. 本讲目标

本讲解决一个核心问题：**LMCache 作为一个独立的 KV cache 管理层，是怎样「挂」到 vLLM 推理引擎上的？**

读完本讲，你应当能够：

1. 说明 vLLM V1 的 `KVConnectorBase_V1` 框架是什么，以及它把一次推理拆成的 **调度（scheduler）** 与 **执行（worker）** 两个阶段、两种角色。
2. 解释 `lmcache_connector_v1.py` 里的 `LMCacheConnectorV1Dynamic` 与 `vllm_v1_adapter.py` 里的 `LMCacheConnectorV1Impl` 为什么是两个类、各自负责什么。
3. 读懂 `register_kv_caches`、`get_num_new_matched_tokens`、`start_load_kv`、`save_kv_layer` 等回调在源码里的实现，并能按时间顺序写出 vLLM 一次 forward 中 LMCache 被调用的顺序。
4. 区分「进程内连接器」(`LMCacheConnectorV1`) 与「多进程连接器」(`LMCacheMPConnector`)，以及为什么后者要把适配器再拆成 scheduler / worker 两半。

本讲承接 [u1-l6](u1-l6-engine-public-api.md)（`LMCacheEngine` 的 store/retrieve/lookup 三大 API）与 [u2-l2](u2-l2-gpu-connector-layer.md)（GPU 连接器把分页 KV 与连续 MemoryObj 互转）。本讲要回答的是：**谁在什么时机调用这些 API？**

## 2. 前置知识

在进入源码前，先用大白话建立三个直觉。

### 2.1 推理引擎里的「钩子（hook）」

vLLM 在一次推理（forward）的固定位置留了一组「调用点」，就像毛坯房里预留的插座盒。第三方只要把符合规格的「插头」（一个实现了 `KVConnectorBase_V1` 的类）插进去，vLLM 就会在那些位置自动调用插头上的方法。这套机制就是 **KVConnector 框架**。LMCache 的 `integration/vllm/` 目录，本质就是给 vLLM 造的这个「插头」。

### 2.2 调度进程与执行进程

vLLM V1 把一次推理切成两段、跑在两种角色上：

- **Scheduler（调度器）**：跑在 CPU 上，决定「这一步要算哪些 request、每个 request 已经算了多少 token、要不要从外部缓存补 KV」。它的决策是「账本」级别的，不碰 GPU 上的真实张量。
- **Worker（执行器）**：跑在 GPU 上，真正做 attention 前向、持有分页 KV cache（paged KV buffer）。KV 的真实搬运（存/取）只能在这里发生。

同一个连接器类会被实例化两次，一次以 scheduler 角色、一次以 worker 角色。框架根据角色只调用对应的回调。

### 2.3 lookup ≠ retrieve

这是理解本讲最容易混淆的点，也是 [u1-l6](u1-l6-engine-public-api.md) 已经区分过的：

- **lookup（查）**：只问「这个 request 的前缀有多少 token 在 LMCache 里命中了」，不搬数据。发生在 **scheduler 侧**。
- **retrieve（取）**：把命中的 KV 真正搬进 vLLM 的分页缓冲。发生在 **worker 侧**。

vLLM 的设计哲学是：先在 scheduler 侧用 lookup 探明能省多少计算，再在 worker 侧按需 retrieve。

## 3. 本讲源码地图

本讲聚焦 `lmcache/integration/vllm/` 目录，核心是三个文件：

| 文件 | 角色 | 一句话职责 |
|------|------|-----------|
| `lmcache/integration/vllm/lmcache_connector_v1.py` | 进程内连接器的「外壳」 | 继承 vLLM 的 `KVConnectorBase_V1`，把每个回调原样转发给 Impl |
| `lmcache/integration/vllm/vllm_v1_adapter.py` | 进程内连接器的「大脑」 | `LMCacheConnectorV1Impl`：所有真实逻辑、与 `LMCacheEngine` 的对接 |
| `lmcache/integration/vllm/vllm_multi_process_adapter.py` | 多进程连接器的实现 | 拆成 scheduler / worker 两个适配器，经 ZMQ 消息队列与 MP daemon 通信 |

辅助理解（本讲会点到，但不深读）：

- `lmcache/integration/vllm/lmcache_connector_v1_085.py`：旧版 vLLM 的外壳，用来对比版本差异。
- `lmcache/integration/vllm/lmcache_mp_connector.py`：多进程连接器的 vLLM 入口，把 scheduler / worker 适配器组合起来。
- `lmcache/integration/vllm/utils.py`：连接器共用的工具（配置加载、多模态哈希等）。

## 4. 核心概念与源码讲解

### 4.1 vLLM KVConnector 框架与「调度 + 执行」两阶段

#### 4.1.1 概念说明

`KVConnectorBase_V1` 是 vLLM V1 定义的连接器基类。它声明了一组方法，分为两组：

- **Worker-side methods**：在 worker 角色上被调用，负责 KV 的真实搬运（load / save）。
- **Scheduler-side methods**：在 scheduler 角色上被调用，负责命中查询与状态记账。

连接器的本质契约是：**vLLM 在固定的生命周期点调用你，你负责把 KV 在「vLLM 分页缓冲」与「外部存储」之间搬移，并告诉 scheduler 能省下多少计算。**

#### 4.1.2 核心流程

一次 forward 中，vLLM 调用连接器的顺序可以抽象成两段：

```
【调度阶段 · Scheduler 角色】
  1. get_num_new_matched_tokens(req)   → 查 LMCache 前缀命中数
  2. （vLLM 内部）分配 GPU 分页块
  3. update_state_after_alloc(req)     → 记录「这次真的要 load」
  4. build_connector_meta(sched_out)   → 打包 ReqMeta 列表，随调度结果发给 worker

【执行阶段 · Worker 角色】
  5. register_kv_caches(kv_caches)     → （仅启动时一次）拿到 vLLM 分页 KV 张量
  6. start_load_kv(forward_ctx)        → retrieve：把命中 KV 搬进分页缓冲
  7. 每层 attention：
        save_kv_layer(layer, kv)       → store：把算完的 KV 搬出去
        wait_for_layer_load(layer)     → 等本层 load 完成（layerwise 流水）
  8. wait_for_save()                   → forward 结束前，确保所有 save 落盘

【请求生命周期】
  9. request_finished(req)  (scheduler) → 决定块是否要保留到异步 save 完成
 10. get_finished(ids)      (worker)    → 上报哪些请求的异步传输已完成
```

注意：第 5 步 `register_kv_caches` 不是每次 forward 都调，而是 worker 启动后调用一次。

#### 4.1.3 源码精读

连接器的「外壳」类 `LMCacheConnectorV1Dynamic` 直接继承自 `KVConnectorBase_V1`，并把自己分成「Worker-side methods」与「Scheduler-side methods」两组，注释清晰可见这套契约：

[lmcache/integration/vllm/lmcache_connector_v1.py:L30-L45](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/integration/vllm/lmcache_connector_v1.py#L30-L45) —— 类定义与构造。`__init__` 里调父类后，立刻创建一个 `LMCacheConnectorV1Impl` 实例挂在 `self._lmcache_engine` 上：

```python
class LMCacheConnectorV1Dynamic(KVConnectorBase_V1):
    def __init__(self, vllm_config, role, kv_cache_config=None):
        if kv_cache_config is not None:
            super().__init__(vllm_config=vllm_config, role=role,
                             kv_cache_config=kv_cache_config)
        else:
            super().__init__(vllm_config=vllm_config, role=role)
        self._lmcache_engine = LMCacheConnectorV1Impl(vllm_config, role, self)
```

构造里对 `kv_cache_config` 的 `if/else` 处理，正是为了兼容不同版本 vLLM 父类 `__init__` 的签名差异——这是「Dynamic」外壳的第一职责：**适配版本**。

外壳上每一个方法体都只有一行——转发给 Impl。例如 worker 侧的 `register_kv_caches`：

[lmcache/integration/vllm/lmcache_connector_v1.py:L50-L58](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/integration/vllm/lmcache_connector_v1.py#L50-L58) —— 把 vLLM 启动时传入的分页 KV 张量字典转发给 Impl。

#### 4.1.4 代码实践

**实践目标**：在源码层面确认「同一个类被两种角色实例化，且只调用对应方法」。

**操作步骤**：

1. 打开 `vllm_v1_adapter.py`，定位 `_init_connector_state`（约 L518）。
2. 观察它用 `if role == KVConnectorRole.SCHEDULER:` 分叉：scheduler 角色初始化 `_unfinished_requests` 字典；worker 角色初始化 `use_layerwise`、`enable_blending`、`blender` 等。
3. 对照本节 4.1.2 的流程图，确认 scheduler 侧方法（如 `get_num_new_matched_tokens`）只读写 scheduler 初始化的那些状态，worker 侧方法（如 `start_load_kv`）只读写 worker 初始化的状态。

**预期结果**：你会看到两种角色的初始化状态几乎没有交集，这正是 vLLM「调度与执行分离」在连接器里的体现。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `register_kv_caches` 属于 worker-side，而 `get_num_new_matched_tokens` 属于 scheduler-side？

> **答案**：前者要拿到 GPU 上的分页 KV 张量（`dict[str, torch.Tensor]`），只有 worker 持有 GPU 与这些张量；后者只是问「前缀命中多少 token」这种账本问题，不碰张量，scheduler 在 CPU 上就能用 `lookup_client` 答出。

**练习 2**：`build_connector_meta` 属于哪一侧？它的产物最终被谁消费？

> **答案**：属于 scheduler-side。它把每个 request 的 token_ids、slot_mapping、load/save 决策打包成 `LMCacheConnectorMetadata`，随调度输出送给 worker；worker 在 `start_load_kv` / `save_kv_layer` 里通过 `self._parent._get_connector_metadata()` 取回并消费。

---

### 4.2 Dynamic 外壳与 Impl 大脑的分工

#### 4.2.1 概念说明

为什么要把连接器拆成 `LMCacheConnectorV1Dynamic`（外壳）和 `LMCacheConnectorV1Impl`（大脑）两个类？

核心原因是 **vLLM 的连接器 API 在跨版本演化**。例如：

- 较新的 vLLM 父类 `__init__` 多了 `kv_cache_config` 参数。
- `get_num_new_matched_tokens` 的返回值，旧版是 `int`，新版是 `tuple[Optional[int], bool]`。
- 是否存在 `register_kv_caches` 这个方法本身，也随版本变化。

如果把这些版本差异和业务逻辑揉在一个类里，每跟一次 vLLM 升级就要改一大片。于是 LMCache 采用了 **适配器模式（Adapter Pattern）**：

- **外壳 `LMCacheConnectorV1Dynamic`**：只关心「满足当前 vLLM 版本的回调签名」，每个方法体就是一句转发。版本变了，换一个外壳文件即可。
- **大脑 `LMCacheConnectorV1Impl`**：与 vLLM 版本无关的纯业务逻辑，调用 `LMCacheEngine` 的 store/retrieve/lookup。这部分长期稳定。

#### 4.2.2 核心流程

转发关系是一条直线：

```
vLLM  ──调用──▶  LMCacheConnectorV1Dynamic.xxx(args)
                          │  self._lmcache_engine.xxx(args)
                          ▼
                   LMCacheConnectorV1Impl.xxx(args)
                          │  调用 LMCacheEngine
                          ▼
                     store / retrieve / lookup
```

外壳里每一个方法的签名严格匹配当前 vLLM 版本的基类，必要时做一点**返回值整形**（把 Impl 的纯业务返回值，包装成 vLLM 期望的元组）。

#### 4.2.3 源码精读

最典型的「返回值整形」例子是 scheduler 侧的 `get_num_new_matched_tokens`。Impl 版本返回的是「能从外部缓存补的 token 数」一个 `Optional[int]`：

[vllm_v1_adapter.py:L1359-L1363](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/integration/vllm/vllm_v1_adapter.py#L1359-L1363) —— Impl 的方法签名，返回 `Optional[int]`。

而新版 vLLM 要求该回调返回 `tuple[Optional[int], bool]`（第二个值表示是否「接受部分命中」之类）。于是外壳把它整形：

[lmcache/integration/vllm/lmcache_connector_v1.py:L149-L169](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/integration/vllm/lmcache_connector_v1.py#L149-L169) —— 外壳在 Impl 的返回值后面补一个 `False`，凑成元组：

```python
def get_num_new_matched_tokens(self, request, num_computed_tokens):
    return self._lmcache_engine.get_num_new_matched_tokens(
        request, num_computed_tokens
    ), False
```

对比旧版外壳 `lmcache_connector_v1_085.py`，同一方法只返回一个 int，没有元组、没有 `kv_cache_config`：

[lmcache/integration/vllm/lmcache_connector_v1_085.py:L105-L125](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/integration/vllm/lmcache_connector_v1_085.py#L105-L125) —— 旧版 vLLM（约 0.8.5）的外壳，`return self._lmcache_engine.get_num_new_matched_tokens(...)` 直接返回 int。

> 这就是 `lmcache_connector_v1.py`（新版）与 `lmcache_connector_v1_085.py`（旧版）最本质的差异：**它们是同一套逻辑面向不同 vLLM API 的两件「外壳」**，背后共用同一个 `LMCacheConnectorV1Impl` 大脑。

大脑 `LMCacheConnectorV1Impl` 的构造，展示了它如何把 vLLM 与 LMCache 两个世界接起来：

[vllm_v1_adapter.py:L453-L499](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/integration/vllm/vllm_v1_adapter.py#L453-L499) —— 构造里做三件事：(1) 读 LMCache 配置并叠加 vLLM 的 `kv_connector_extra_config`；(2) 用 `VllmServiceFactory` 建一个 `LMCacheManager`（注意是 manager，不是直接 `LMCacheEngine`，manager 内部再持有 engine）；(3) 角色相关初始化与指标挂载。

```python
config = lmcache_get_or_create_config()
self._apply_extra_config(config, vllm_config)   # 把 vLLM 的 lmcache.* 配置合并进来
self.config = config
service_factory = VllmServiceFactory(config, vllm_config, role.name.lower())
self._manager = LMCacheManager(config, service_factory, connector=self)
self._manager.start_services()
```

#### 4.2.4 代码实践

**实践目标**：亲手验证「外壳 = 纯转发、大脑 = 真逻辑」这条不变量。

**操作步骤**：

1. 在 `lmcache_connector_v1.py` 里数一下 `LMCacheConnectorV1Dynamic` 每个方法体的行数——你应该发现几乎都是「一行转发 + docstring」。
2. 然后在 `vllm_v1_adapter.py` 里找同名的 `LMCacheConnectorV1Impl` 方法（例如 `register_kv_caches` 在 L754、`start_load_kv` 在 L763、`save_kv_layer` 在 L999），对比它们的体量。

**预期结果**：外壳方法体 ≤ 1 行有效代码；Impl 对应方法几十到上百行，调用 `self.lmcache_engine.retrieve(...)` / `store_layer(...)`。这条对比能让你一眼记住两者的分工。

#### 4.2.5 小练习与答案

**练习 1**：如果未来 vLLM 又改了 `get_num_new_matched_tokens` 的返回类型（比如要求返回三元组），按现有架构应该改哪个文件、不改哪个文件？

> **答案**：只改外壳（`lmcache_connector_v1.py` 或新增一个版本化的外壳文件），在转发处把 Impl 的 `Optional[int]` 整形成新签名要求的三元组；Impl（`vllm_v1_adapter.py`）无需改动，因为它返回的是与 vLLM 版本无关的业务值。

**练习 2**：`_apply_extra_config`（vllm_v1_adapter.py L501）处理的是哪种配置？它和 u1-l5 讲的「程序内 overrides」是什么关系？

> **答案**：它把 vLLM 启动参数里 `kv_connector_extra_config` 中以 `lmcache.` 开头的键（去掉前缀后）写入 LMCache 配置对象。这相当于又增加了一条「来自 vLLM 的程序内 overrides」覆盖链，优先级落在 u1-l5 所述「程序内 overrides」这一档。

---

### 4.3 一次 forward 的完整调用顺序

#### 4.3.1 概念说明

本模块把 4.1.2 的流程图落到源码上，逐个回调讲清楚「输入是什么、做了什么、输出是什么」。重点理解三类数据结构：

- **`LoadSpec`**：描述「要不要 load、vLLM 已缓存多少、LMCache 缓存多少」。
- **`SaveSpec`**：描述「要不要 save、跳过前多少已存 token」。
- **`ReqMeta`**：一次调度里单个 request 的完整计划（token_ids + slot_mapping + load/save spec），是 scheduler 打包给 worker 的「工作单」。

#### 4.3.2 核心流程（按调用顺序）

**① `get_num_new_matched_tokens`（scheduler，查命中）**

这是 lookup 的入口。Impl 通过 `lookup_client.lookup(...)` 询问前缀命中数，记录进 `self.load_specs[req_id]`，并返回「需要新分配多少 token 的 KV」。

关键是一段 token 账本计算：

\[
\text{need\_to\_allocate} = \text{lmcache\_cached} - \text{vllm\_cached} - \text{recalc\_last}
\]

其中 `recalc_last` 在「整段 prompt 全命中」时为 1——因为这种情况下 vLLM 必须重算最后一个 token 以得到 logits，不能全部跳过。

**② `update_state_after_alloc`（scheduler，标记可加载）**

vLLM 分配完分页块后回调这里。Impl 把对应 `load_specs[req_id].can_load` 置为 `True`（前提是 vLLM 确实分到了能容纳外部 KV 的块），并清掉 lookup client 的本地缓存状态。

**③ `build_connector_meta`（scheduler，打包工作单）**

遍历这一步新调度 / 续算的 request，为每个生成 `ReqMeta`（含 `slot_mapping`——把连续 token 映射到 vLLM 分页槽位的张量），塞进 `LMCacheConnectorMetadata`。这个对象随调度结果送到 worker。

**④ `register_kv_caches`（worker，启动一次）**

worker 启动时拿到 vLLM 的分页 KV 张量字典，存到 `self.kv_caches`，并触发 `manager.post_init()`（此时才真正创建存储后端，呼应 u1-l6 所述「构造期不建后端」）。

**⑤ `start_load_kv`（worker，retrieve）**

forward 开始时，Impl 读出工作单里 `can_load=True` 的 request，调用 `self.lmcache_engine.retrieve(...)` 把命中 KV 写进 vLLM 分页缓冲。对 layerwise 模式则改用 `retrieve_layer(...)` 拿到一个逐层产出（generator），留给第 ⑦ 步逐层消费。

**⑥/⑦ `save_kv_layer` + `wait_for_layer_load`（worker，逐层存/等取）**

在每层 attention 内部被调用：`save_kv_layer` 把本层算完的 KV 经 `store_layer(...)` 存出去；`wait_for_layer_load` 阻塞到本层的 retrieve 拷贝完成。这是 layerwise 流水的关键。

**⑧ `wait_for_save`（worker，收尾）**

forward 退出前阻塞，确保所有异步 save 完成，避免分页缓冲被下一帧覆写。

**⑨/⑩ `request_finished` + `get_finished`（生命周期）**

请求结束时，scheduler 侧 `request_finished` 决定块是否要保留到异步 save 完成；worker 侧 `get_finished` 上报哪些请求的异步传输真正结束、块可以释放。

#### 4.3.3 源码精读

**① lookup 入口**：注意它把结果缓存进 `self.load_specs`，并处理「全命中需重算最后一个 token」与「低于 min_retrieve 阈值则只记账不取」两种边界：

[vllm_v1_adapter.py:L1448-L1531](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/integration/vllm/vllm_v1_adapter.py#L1448-L1531) —— 计算 `need_to_allocate`、写入 `LoadSpec`、决定最终返回值。核心片段：

```python
need_to_allocate = num_external_hit_tokens - num_computed_tokens
if num_external_hit_tokens == request.num_tokens:
    need_to_allocate -= 1          # 全命中：留最后一个 token 给 vLLM 重算
...
self.load_specs[req_id] = LoadSpec(
    vllm_cached_tokens=num_computed_tokens,
    lmcache_cached_tokens=capped_lmcache_tokens,
    can_load=False,                # 默认 False，等 update_state_after_alloc 置 True
)
```

**③ 打包工作单**：`build_connector_meta` 把新调度请求转成 `RequestTracker` 再转成 `ReqMeta`，并清理已结束请求的跟踪状态：

[vllm_v1_adapter.py:L1634-L1680](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/integration/vllm/vllm_v1_adapter.py#L1634-L1680) —— 遍历 `scheduled_new_reqs`，为每个 request 建 tracker 与 req_meta，加入 metadata。

**④ 启动注册**：

[vllm_v1_adapter.py:L754-L760](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/integration/vllm/vllm_v1_adapter.py#L754-L760) —— 记下分页 KV 字典并触发后端真正初始化：

```python
def register_kv_caches(self, kv_caches: dict[str, torch.Tensor]):
    logger.info("Registering KV caches")
    assert len(self.kv_caches) == 0 and len(kv_caches) > 0
    self.kv_caches = kv_caches
    self._manager.post_init()
```

**⑤ retrieve 取回**：`start_load_kv` 里对每个待加载 request 调用 engine 的 retrieve；注意它先构造 `token_mask`，把 vLLM 已缓存的前缀位置置 `False`（呼应 u1-l6 的 `FFFFFTTTTTTT` mask 约定）：

[vllm_v1_adapter.py:L858-L867](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/integration/vllm/vllm_v1_adapter.py#L858-L867) —— 非 layerwise 路径，直接 `self.lmcache_engine.retrieve(...)`，返回的 `ret_token_mask` 标识真正取回的位置。

**⑥ save 逐层存**：

[vllm_v1_adapter.py:L1087-L1100](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/integration/vllm/vllm_v1_adapter.py#L1087-L1100) —— 用 `store_layer(...)` 拿到一个逐层 storer（generator），每层 `next()` 推进一步。

**⑩ 上报完成**：

[vllm_v1_adapter.py:L1338-L1341](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/integration/vllm/vllm_v1_adapter.py#L1338-L1341) —— 进程内连接器的 `get_finished` 直接返回 `(None, None)`，因为没有跨进程异步传输；多进程版才有真实内容。

> **重要边界**：Impl 几乎每个方法开头都有「降级模式」判断——当 `self.lmcache_engine is None` 或 `self.lookup_client is None`（LMCache 初始化失败）时，连接器会安静地「什么都不做」，让 vLLM 退回重算，而不是抛异常崩溃 EngineCore。例如 `start_load_kv` 的 [L793-L794](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/integration/vllm/vllm_v1_adapter.py#L793-L794)、`get_num_new_matched_tokens` 的 [L1396-L1397](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/integration/vllm/vllm_v1_adapter.py#L1396-L1397)。这是「no fate-sharing」原则在连接器层的体现：LMCache 挂了不应拖垮推理。

#### 4.3.4 代码实践

**实践目标**：把本讲的「调用顺序」从流程图变成可核对的源码证据链。

**操作步骤**：

1. 在 `vllm_v1_adapter.py` 中，按下表左侧的回调名，找到它在 Impl 里的定义行号，并记录它调用的 `lmcache_engine` / `lookup_client` 方法：
   | 回调 | 侧 | 定义行 | 调用的下游 |
   |------|----|--------|-----------|
   | `get_num_new_matched_tokens` | scheduler | L1359 | `lookup_client.lookup` |
   | `update_state_after_alloc` | scheduler | L1534 | （记账，置 can_load） |
   | `build_connector_meta` | scheduler | L1612 | （生成 ReqMeta） |
   | `register_kv_caches` | worker | L754 | `manager.post_init` |
   | `start_load_kv` | worker | L763 | `lmcache_engine.retrieve` / `retrieve_layer` |
   | `save_kv_layer` | worker | L999 | `lmcache_engine.store_layer` |
2. 按本节 4.3.2 的 ①→⑧ 顺序，把这些方法重新排成一条时间线。

**预期结果**：你得到一条与 4.1.2 流程图严格对应的、每一步都有行号佐证的调用链。这是后续阅读多进程适配器（4.4）时的对照基线。

**关于运行**：完整运行需要带 GPU 的 vLLM 环境，本机不一定具备。若无法实跑，上述「源码阅读型实践」即为本讲的主实践；待本地具备 vLLM + GPU 后，可用 `examples/online_session/` 的脚本观察日志中 `Registering KV caches`、`Scheduled to load N tokens` 等行的真实出现顺序做交叉验证（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `get_num_new_matched_tokens` 在「整段 prompt 全命中」时要让 `need_to_allocate -= 1`？

> **答案**：全命中意味着所有 prompt token 的 KV 都在缓存里、vLLM 一个都不用算。但 vLLM 至少要算一个 token 才能产出下一个 token 的 logits，所以必须强制重算最后一个 token——从「可跳过」里扣掉 1。

**练习 2**：`start_load_kv` 里 `token_mask[:masked_token_count] = False` 这一行（约 L827）在做什么？

> **答案**：把 vLLM 本地已经缓存的前 `masked_token_count`（按 chunk 对齐）个位置标记为 `False`，表示这些 token 不需要从 LMCache 取——它们已经在 vLLM 自己的缓存里了。剩下的 `True` 位置才是 retrieve 要补的，正是 u1-l6 约定的 `FFFFF...TTTT` mask。

---

### 4.4 多进程适配器：scheduler / worker 再拆分

#### 4.4.1 概念说明

上面三节讲的都是**进程内连接器** `LMCacheConnectorV1`：LMCache 的 `LMCacheEngine` 与 vLLM 跑在同一个进程里，直接函数调用即可。

但 LMCache 还有一套**多进程（MP）架构**（详见 [u3 单元](u3-l1-mp-architecture-overview.md)）：KV cache 管理被独立成一个 daemon 进程，vLLM worker 通过消息队列与它通信，做到「engine 与 cache 不共命运」。这套架构对应的连接器叫 **`LMCacheMPConnector`**。

用户通过 vLLM 的 `--kv-transfer-config` 选择用哪种连接器：

- `{"kv_connector": "LMCacheConnectorV1", ...}` → 进程内（本讲 4.1–4.3）
- `{"kv_connector": "LMCacheMPConnector", "kv_connector_extra_config": {"lmcache.mp.port": 6555}, ...}` → 多进程（本节）

参见部署文档：`docs/source/mp/deployment.rst`。

#### 4.4.2 核心流程

进程内连接器只有一个 `LMCacheConnectorV1Impl`，scheduler 与 worker 共享同一个对象的不同状态。而 MP 连接器把这条链路拆成了**两个独立的适配器类**，分别住在 scheduler 与 worker 两端，中间隔着一个 daemon：

```
【scheduler 进程】                      【worker 进程】
LMCacheMPSchedulerAdapter  ──ZMQ MQ──▶  LMCache MP daemon  ◀──ZMQ MQ──  LMCacheMPWorkerAdapter
   · lookup：把 token 发给 daemon          （真正持有 LMCacheEngine）        · register_kv_caches：注册分页 KV
   · 收 chunk 命中数                                                         · load/store：经 IPC 传张量
```

之所以必须拆，是因为 MP 架构下「记账（命中查询）」和「搬数据（张量传输）」落在不同进程，没法再用同一个对象的不同方法。

#### 4.4.3 源码精读

两个适配器类的定义与职责：

[vllm_multi_process_adapter.py:L562-L651](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/integration/vllm/vllm_multi_process_adapter.py#L562-L651) —— `LMCacheMPSchedulerAdapter`：scheduler 侧。构造时为每个 daemon URL 建一个 `MessageQueueClient`（ZMQ），并立刻向 daemon 查询 `chunk_size`，要求所有 daemon 的 chunk_size 一致：

```python
self.mq_clients: dict[str, MessageQueueClient] = {
    url: MessageQueueClient(url, context) for url in self._server_urls
}
...
chunk_sizes[url] = get_lmcache_chunk_size(client, timeout=self._mq_timeout)
...
unique_sizes = set(chunk_sizes.values())
if len(unique_sizes) != 1:
    raise ValueError("All LMCache servers must share the same chunk_size ...")
```

[vllm_multi_process_adapter.py:L1042-L1043](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/integration/vllm/vllm_multi_process_adapter.py#L1042-L1043) —— `LMCacheMPWorkerAdapter`：worker 侧，负责真实的 KV 注册与跨进程传输（CUDA IPC / 共享内存），细节留待 [u3-l2](u3-l2-mp-server-client-ipc.md)。

worker 侧的 `register_kv_caches` 比进程内版本复杂得多——它要把分页 KV 注册到 daemon，并校验 chunk_size 是 block 大小的整数倍：

[vllm_multi_process_adapter.py:L1229-L1258](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/integration/vllm/vllm_multi_process_adapter.py#L1229-L1258) —— 注册时逐个 engine group 校验 `lmcache_tokens_per_chunk % tokens_per_block == 0`，不齐就抛 `ValueError`，因为 chunk 边界必须与分页边界对齐。

两个适配器还共享一份集中式的默认配置表 `ExtraConfigDefault`，所有 `lmcache.mp.*` 调参项都集中在此：

[vllm_multi_process_adapter.py:L43-L64](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/integration/vllm/vllm_multi_process_adapter.py#L43-L64) —— 用枚举集中定义 `mq_timeout`、`heartbeat_interval`、`mp_transfer_mode` 等默认值，呼应 u1-l5「一张表驱动一切」的思路。

#### 4.4.4 代码实践

**实践目标**：对比进程内与多进程两套连接器对同一个回调（`register_kv_caches`）的不同实现。

**操作步骤**：

1. 重读进程内版本 [vllm_v1_adapter.py:L754-L760](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/integration/vllm/vllm_v1_adapter.py#L754-L760)：3 行有效代码，本地赋值 + `post_init`。
2. 再读多进程版本 [vllm_multi_process_adapter.py:L1229-L1258](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/integration/vllm/vllm_multi_process_adapter.py#L1229-L1258)：要做 engine group 对齐校验、跨进程注册。

**预期结果**：你会清楚看到，从「进程内」切到「多进程」，同一个回调的复杂度从「直接赋值」涨到「跨进程协议 + 一致性校验」。这正是 u3 单元要展开的 MP 通信机制。

#### 4.4.5 小练习与答案

**练习 1**：为什么 MP 连接器要在构造时强制「所有 daemon 的 chunk_size 必须相同」？

> **答案**：scheduler 侧的命中数是跨多个 daemon 聚合（取 min 等）得到的，如果各 daemon 的 chunk_size 不同，聚合出来的命中数会混入不同粒度的 chunk，retrieve 时无法对齐。所以必须统一。

**练习 2**：进程内连接器的 `get_finished` 返回 `(None, None)`（4.3.3 ⑩），多进程版本会一样吗？

> **答案**：不一样。进程内没有跨进程异步传输，自然没有「未完成」的请求要上报；多进程版本经 ZMQ / IPC 传张量是异步的，worker 侧 `get_finished`（约 L1560）需要真实追踪并上报哪些请求的传输已经完成，scheduler 才能安全释放块。这正是两套架构的本质差别之一。

---

## 5. 综合实践

**任务**：为「vLLM 一次 forward 中 LMCache 的调用顺序」产出一份带源码行号的双栏对照表，并解释每一步为什么必须在这个时机发生。

**要求**：

1. 列出 scheduler 侧 4 个回调 + worker 侧 5 个回调（含 `register_kv_caches`）的名称、所在文件与行号、调用的下游（`lookup_client` / `lmcache_engine` / `manager` 的哪个方法）。
2. 对其中至少 3 个回调，写出「如果它被挪到另一个时机会发生什么错误」的推演（例如：把 `retrieve` 放到 `save_kv_layer` 之后会怎样？把 `lookup` 放到 `update_state_after_alloc` 之后会怎样？）。
3. 用一句话标注：进程内连接器与多进程连接器，在哪几个回调上实现差异最大（提示：`register_kv_caches`、`get_finished`）。

**提示**：这张表既是本讲的总结，也是你阅读 [u3 单元](u3-l1-mp-architecture-overview.md)（多进程架构）时的对照基线——到那时你会看到同样的回调名背后，实现从「函数调用」变成了「跨进程消息」。

## 6. 本讲小结

- vLLM V1 的 `KVConnectorBase_V1` 把一次推理拆成 **scheduler（CPU，查命中、记账）** 与 **worker（GPU，搬 KV）** 两阶段、两角色，连接器按角色只被调用对应回调。
- 连接器采用 **适配器模式**：`LMCacheConnectorV1Dynamic`（外壳）只负责匹配当前 vLLM 版本的回调签名并转发；`LMCacheConnectorV1Impl`（大脑）承载与版本无关的业务逻辑。版本差异（如 `get_num_new_matched_tokens` 返回 int 还是 tuple）只影响外壳。
- 一次 forward 的调用顺序为：scheduler 侧 `get_num_new_matched_tokens`（lookup）→ `update_state_after_alloc` → `build_connector_meta`；worker 侧 `register_kv_caches`（启动一次）→ `start_load_kv`（retrieve）→ 每层 `save_kv_layer`/`wait_for_layer_load` → `wait_for_save`；生命周期收尾 `request_finished`/`get_finished`。
- 工作单数据结构 `LoadSpec`/`SaveSpec`/`ReqMeta` 把 scheduler 的决策（含 slot_mapping）打包送给 worker；token 账本核心是 `need_to_allocate = lmcache_cached - vllm_cached - recalc_last`。
- 连接器每个回调都有「降级模式」：LMCache 初始化失败时安静退回重算，不拖垮推理，体现 no fate-sharing。
- `LMCacheMPConnector`（多进程）把适配器再拆成 `LMCacheMPSchedulerAdapter`（ZMQ 查命中）与 `LMCacheMPWorkerAdapter`（IPC 传张量），与进程内 `LMCacheConnectorV1` 通过 `kv_connector` 配置项二选一。

## 7. 下一步学习建议

- **进入 u3 单元**：本讲多次提到的 MP daemon、ZMQ 消息队列、CUDA IPC，是 [u3-l1 多进程架构总览](u3-l1-mp-architecture-overview.md) 与 [u3-l2 MP Server/Client 与进程间通信](u3-l2-mp-server-client-ipc.md) 的主题，那里会拆解 `MessageQueueClient`、`futures`、`posix_shm` 等机制。
- **横向扩展阅读**：本讲只看了 vLLM 集成；`lmcache/integration/` 下还有 SGLang、TensorRT-LLM 的连接器，可对比它们各自如何对接不同引擎的钩子模型。
- **纵向深入**：worker 侧 `retrieve`/`store_layer` 内部如何经 GPU 连接器（[u2-l2](u2-l2-gpu-connector-layer.md)）做分页↔连续格式转换，是理解 KV 搬运开销的下一步。
- **CacheBlend**：本讲 `start_load_kv` 里出现的 `self.blender.blend(...)` 分支（开启 `enable_blending` 时）属于非前缀复用，详见 [u2-l6 CacheBlend](u2-l6-cacheblend.md)。
