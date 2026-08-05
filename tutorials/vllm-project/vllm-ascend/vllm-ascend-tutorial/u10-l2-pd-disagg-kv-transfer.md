# PD 分离与 KV 传输连接器

## 1. 本讲目标

本讲讲解 vllm-ascend 在「Prefill/Decode 分离（PD disaggregation）」场景下，如何在 P 节点与 D 节点之间传输 KV cache。学完后你应当能够：

- 说清楚什么是 PD 分离、它解决什么问题、为什么要拆 P/D 两个角色。
- 读懂 `distributed/kv_transfer/` 的整体分层：连接器（connector）→ 调度器（scheduler）/工作器（worker）→ 底层传输后端。
- 区分三类连接器的定位差异：`MooncakeConnector`（P2P 直连）、`AscendStoreConnector`（KV Pool 池化）、`LMCacheAscendConnector`（lmcache 接力）。
- 理解 `register_connector()` 如何把连接器登记到 vLLM 工厂，以及 vLLM 在运行期如何按 `kv_role`/`kv_connector` 选中实现。
- 理解本次更新（#12852 配套）在 `AscendStoreConnector` 的 layerwise 模式下，`consumer_is_to_put` 让「只读 consumer」也能发布 KV 的行为及其原因。

## 2. 前置知识

阅读本讲前，建议先掌握以下概念（若不熟悉，可回看对应讲义）：

- **KV cache 与注意力**：Transformer 推理时，prefill 阶段把 prompt 的 Key/Value 缓存到设备显存，decode 阶段复用它们，避免每生成一个 token 都重算历史（详见 u5-l1）。
- **NPUWorker 与执行主链路**：每个 worker 子进程持有 KV cache 张量并执行前向（详见 u4-l1）。本讲的连接器正是「挂在 worker 上、负责跨进程/跨节点搬运 KV」的那一层。
- **vLLM v1 的 KVConnector 抽象**：上游 vLLM 定义了 `KVConnectorBase_V1` 接口，把「KV 从哪来、往哪去」从执行主链路解耦出去。连接器分两种角色：
  - `KVConnectorRole.SCHEDULER`：跑在引擎核心（scheduler 所在进程），决定「这个请求要不要去远端取/存 KV」。
  - `KVConnectorRole.WORKER`：跑在每个 worker 子进程，真正执行设备内存的读写与网络传输。
- **HCCL / 进程间通信**：P 节点和 D 节点是两个独立的 vLLM 实例（甚至不同机器），靠网络（mooncake / RDMA / 内存池后端）传 KV，而非共享内存。

> 一句话直觉：PD 分离 = 把「算 prompt」和「逐 token 解码」放到两套 NPU 上分别跑，中间用一个「连接器」把 prefill 算好的 KV 搬给 decode。

## 3. 本讲源码地图

本讲涉及的关键文件及其职责：

| 文件 | 作用 |
|------|------|
| [vllm_ascend/distributed/kv_transfer/\_\_init\_\_.py](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/distributed/kv_transfer/__init__.py) | `register_connector()`：把所有 NPU 版连接器登记进上游 `KVConnectorFactory`。 |
| [vllm_ascend/distributed/kv_transfer/kv_p2p/mooncake_connector.py](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/distributed/kv_transfer/kv_p2p/mooncake_connector.py) | `MooncakeConnector`：基于 mooncake TransferEngine 的 P2P 直连连接器，D 节点异步从 P 节点「拉」KV。 |
| [vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/ascend_store_connector.py](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/ascend_store_connector.py) | `AscendStoreConnector`：基于「KV Pool」的池化连接器，支持 layerwise 逐层传输；本次更新的主角。 |
| [vllm_ascend/distributed/kv_transfer/kv_pool/lmcache_ascend_connector.py](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/distributed/kv_transfer/kv_pool/lmcache_ascend_connector.py) | `LMCacheAscendConnector`：对上游 `LMCacheConnectorV1` 的薄封装，引入 `lmcache_ascend` 原生库。 |
| [vllm_ascend/distributed/kv_transfer/ascend_multi_connector.py](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/distributed/kv_transfer/ascend_multi_connector.py) | `AscendMultiConnector`：覆盖上游 `MultiConnector`，让多个子连接器（含 layerwise）协同。 |

> 说明：ascend_store 目录下还有 `pool_worker.py` / `pool_scheduler.py` / `layerwise_cache_layout.py` / `config_data.py` / `kv_transfer.py` / `backend/` 等数据面文件，它们承载「分层 KV 缓冲复用」的具体机制，体量较大，单独放在 **u10-l7** 讲。本讲只聚焦「连接器层的注册、选择与 save/load 行为」。

## 4. 核心概念与源码讲解

本讲拆为两个最小模块：**4.1 PD 分离架构**（讲为什么、整体怎么跑），**4.2 KV 传输连接器**（讲三类连接器的差异与登记选择，含本次 `consumer_is_to_put` 更新）。

---

### 4.1 PD 分离：Prefill/Decode 解耦架构

#### 4.1.1 概念说明

在「合体（colocated）」部署里，一个 vLLM 实例同时做 prefill（处理整段 prompt，计算密集、吃算力）和 decode（逐 token 自回归，访存密集、吃显存带宽）。这两步对硬件的诉求很不一样：

- Prefill：每张卡都在做大矩阵乘，希望 batch 大、算力打满。
- Decode：每步只产一个 token，KV cache 的读写是瓶颈，希望显存带宽大、batch 灵活。

把两者塞在同一套卡上，会出现「prefill 抢算力时 decode 卡顿、decode 占显存时 prefill 放不开」的互相干扰。**PD 分离（PD disaggregation）** 的解法是：拆成两个角色，分别跑在两套 NPU 上：

- **P 节点（Prefill，`kv_role=kv_producer`）**：专职算 prompt，算完后把该请求的 KV cache 发出去。
- **D 节点（Decode，`kv_role=kv_consumer`）**：专职逐 token 解码；当一个新请求轮到自己时，先从 P 节点把对应的 KV 拉回来填进本地显存，再开始 decode。

这样 P 和 D 各自的 batch、算力、显存都能独立调优，互不干扰。代价是要在两者之间搬一次 KV——这就是「KV 传输连接器」存在的意义。

> 还有一个 `kv_role=kv_both`，表示该实例同时是生产者和消费者（用于 P/D 混部或缓存共享拓扑）。

#### 4.1.2 核心流程

一个请求的 PD 分离端到端流程（以 D 节点视角、P2P 连接器为例）：

```text
1. 外部路由（proxy_server）把请求先发到 P 节点。
2. P 节点执行 prefill，把 prompt 的 KV 写进自己的设备显存。
3. 请求「完成 prefill」后，P 节点把 {remote_host, remote_port, remote_block_ids, ...}
   打包进 request_finished 返回的 kv_transfer_params，经路由转交 D 节点。
4. D 节点收到请求，连接器 scheduler 在 get_num_new_matched_tokens 里
   识别出 do_remote_prefill，声明「这批 token 可以从外部 KV 异步加载」。
5. D 节点连接器 worker 在 start_load_kv 异步地把 KV 从 P 节点拉到本地显存块。
6. KV 到位后，D 节点像普通 decode 一样继续生成。
```

关键点：第 5 步的「异步」是 PD 分离性能的核心——KV 传输与引擎下一步调度并行，只要在真正前向用到的层之前把对应 KV 装好即可。这也是为什么连接器要分 scheduler/worker 两层：scheduler 决策「要不要取」（轻、跑在引擎核心），worker 执行「真正取」（重、跑在 worker，可后台线程化）。

#### 4.1.3 源码精读

**连接器的双角色构造。** `MooncakeConnector` 在 `__init__` 里按 `role` 分流：scheduler 角色建 `MooncakeConnectorScheduler`，worker 角色建 `MooncakeConnectorWorker`。

[kv_p2p/mooncake_connector.py:1467-1492](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/distributed/kv_transfer/kv_p2p/mooncake_connector.py#L1467-L1492)（连接器在 __init__ 里按 KVConnectorRole 分流构造 scheduler 或 worker）—— 这里在 `__init__` 中按 `KVConnectorRole` 分别构造 scheduler 或 worker，是 vLLM v1 连接器的标准模式。

**D 节点声明「可从远端加载」。** scheduler 侧的 `get_num_new_matched_tokens` 是 PD 分离的入口：当请求带 `do_remote_prefill` 时，返回 prompt 剩余未算的 token 数，并标记为「异步加载」。

```python
def get_num_new_matched_tokens(self, request, num_computed_tokens) -> tuple[int, bool]:
    ...
    if params is not None and params.get("do_remote_prefill"):
        token_ids = request.prompt_token_ids or []
        actual = self._state_prefill_token_count(len(token_ids))
        count = max(actual - num_computed_tokens, 0)
        if count > 0:
            return count, True   # True = 异步加载，不等本引擎
    ...
    return 0, False
```

见 [mooncake_connector.py:1752-1788](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/distributed/kv_transfer/kv_p2p/mooncake_connector.py#L1752-L1788)。

**P2P 连接器「不显式保存」。** 注意 `MooncakeConnector` 的 `save_kv_layer` / `wait_for_save` 都是空操作——P2P 模式下，P 节点的 KV 在请求处理过程中由后台收发线程按需读取，而不是在每层显式 `put`。

[kv_p2p/mooncake_connector.py:1555-1563](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/distributed/kv_transfer/kv_p2p/mooncake_connector.py#L1555-L1563)（`MooncakeConnector` 的 save 系列方法为 no-op，注释明确「does not save explicitly」）。

#### 4.1.4 代码实践

**实践目标**：在不跑 NPU 的前提下，通过阅读部署示例建立「P 节点配置 / D 节点配置 / 路由」三件套的直觉。

**操作步骤**：

1. 打开 `examples/disaggregated_prefill_v1/mooncake_connector_deployment_guide.md`，阅读「Run prefill Node」与「Run decode Node」两段脚本。
2. 对比两段脚本里 `--kv-transfer-config` 的 `kv_role` 字段：P 节点是 `kv_producer`，D 节点是 `kv_consumer`；并注意 `kv_connector_extra_config` 里的 `prefill`/`decode` 子段各声明了本侧的 `tp_size`/`dp_size`（用于 TP 不对齐时的头切分计算）。
3. 打开 `examples/disaggregated_prefill_v1/load_balance_proxy_server_example.py`，确认外部请求先到 proxy，再由 proxy 转发到 P、P 完成后再交 D。

**需要观察的现象 / 预期结果**：

- 同一份 `kv-transfer-config` JSON 里，P 与 D 都填了对方的并行规模（`prefill` 段写 prefill 侧 TP/DP，`decode` 段写 decode 侧 TP/DP），这是因为 KV 按 head 切分时双方都要知道对方的头布局（见 worker 里 `tp_num_need_pulls` 的计算）。
- `kv_port` 是连接器握手端口基数，每个 (pp, pcp, tp) 设备偏移一个唯一端口。

> 待本地验证：实际端口分配与多卡数量是否匹配，需在真实环境用 `netstat` 验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 PD 分离要把 KV 「搬一次」，而不是让 D 节点直接重算 prompt 的 KV？
**参考答案**：重算 prompt 等于 D 节点把 prefill 的算力活又干一遍，完全抵消了「拆分让 P 专心算」的收益；搬一次 KV 的网络/拷贝成本远小于重算的算力成本，尤其对长 prompt。

**练习 2**：`kv_role` 有 `kv_producer` / `kv_consumer` / `kv_both` 三种，`kv_both` 适合什么部署？
**参考答案**：适合 P/D 混部（PD-mixed）或多个 decode 节点之间互相共享 KV 的拓扑——同一实例既会把本地 KV 发给别的实例，也会从别的实例拉 KV。

---

### 4.2 KV 传输连接器：注册、选择与三类后端

#### 4.2.1 概念说明

PD 分离的「搬 KV」有多种搬法，对应不同连接器：

| 连接器 | 传输模型 | 一句话定位 |
|--------|----------|------------|
| `MooncakeConnector`（P2P） | D 节点点对点「拉」P 节点的设备显存块 | 最直接，P 和 D 一一配对，mooncake TransferEngine 做 RDMA/内存直读 |
| `AscendStoreConnector`（KV Pool） | P 把 KV「发布（put）」进一个池（store），D 按需「查 + 取」 | 池化解耦，支持多 P 多 D、layerwise 逐层传输、lookup 命中复用 |
| `LMCacheAscendConnector` | 复用上游 lmcache 的存储/淘汰链路 + `lmcache_ascend` 原生库 | 走 lmcache 既有生态，适合已用 lmcache 的用户 |
| `AscendMultiConnector` | 不是独立传输后端，而是把多个子连接器串起来 | 一个实例同时挂「池化 + layerwise」等多个连接器 |

它们的共同抽象都是上游 `KVConnectorBase_V1`：实现 scheduler 侧的「决策」方法和 worker 侧的「执行」方法即可。vllm-ascend 不改这个抽象，而是**注册 NPU 版实现**到上游工厂，运行期按用户在 `--kv-transfer-config` 里写的 `kv_connector` 名字选中。

> 还有两个 NPU 化的「覆盖」：`AscendMultiConnector` 覆盖上游 `MultiConnector`，`AscendSimpleCPUOffloadConnector` 覆盖上游 `SimpleCPUOffloadConnector`（用 `aclrtMemcpyBatchAsync` + NPU stream 适配）。

#### 4.2.2 核心流程

**注册**（启动期，一次性）：

```text
vllm_ascend.register() → register_connector()
   → 对每个 NPU 连接器调 KVConnectorFactory.register_connector(name, module, cls)
   → 额外「pop 再重注册」覆盖上游同名连接器（MultiConnector / SimpleCPUOffloadConnector）
```

**选择**（运行期，每个实例）：

```text
vLLM 读 --kv-transfer-config 里的 "kv_connector" 名字
   → KVConnectorFactory 按 name 查到 (module_path, class_name)
   → 延迟 import 这个类
   → 按 role（SCHEDULER / WORKER）分别实例化
```

**layerwise 模式下的 save 决策**（本次更新重点）：

```text
AscendStoreConnector.save_kv_layer(layer):
   if not use_layerwise:           return      # 非 layerwise 不走逐层保存
   if kv_role == "kv_consumer"
      and not consumer_is_to_put:  return      # 只读 consumer 默认不发
   worker.save_kv_layer(metadata)              # 真正 put 进池
```

#### 4.2.3 源码精读

**连接器登记簿。** `register_connector()` 把每个连接器以 `name → (module, class)` 登记到上游 `KVConnectorFactory`。其中两处是「先 pop 上游同名、再注册 NPU 版」的覆盖手法。

[\_\_init\_\_.py:21-87](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/distributed/kv_transfer/__init__.py#L21-L87)（`register_connector` 把 MooncakeConnectorV1 / AscendStoreConnector / LMCacheAscendConnector 等登记进工厂，并覆盖上游 MultiConnector / SimpleCPUOffloadConnector）。

关键片段（覆盖上游 MultiConnector）：

```python
def register_connector():
    if "MultiConnector" in KVConnectorFactory._registry:
        KVConnectorFactory._registry.pop("MultiConnector")
    KVConnectorFactory.register_connector(
        "MultiConnector", "vllm_ascend.distributed.kv_transfer.ascend_multi_connector", "AscendMultiConnector"
    )
    KVConnectorFactory.register_connector(
        "AscendStoreConnector",
        "vllm_ascend.distributed.kv_transfer.kv_pool.ascend_store.ascend_store_connector",
        "AscendStoreConnector",
    )
    ...
```

> 之所以用「pop 再注册」而不是改上游源码，正是 vllm-ascend 全程坚持的「上游零侵入」策略（见 u3-l1 的 patch 哲学）。

**MultiConnector 把 layerwise 子连接器纳入调度。** `AscendMultiConnector` 覆盖 `update_state_after_alloc`：被选中的连接器正常拿 blocks，其余连接器拿空 blocks——但 `MooncakeLayerwiseConnector` 例外，永远接收真实 blocks，保证逐层传输路径不被旁路。

[ascend_multi_connector.py:32-41](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/distributed/kv_transfer/ascend_multi_connector.py#L32-L41)（分发 blocks 时，被选中的连接器或 `MooncakeLayerwiseConnector` 拿真实 blocks，其余拿空 blocks）。

**AscendStoreConnector：layerwise 与 PIECEWISE 图模式绑定。** 该连接器用类方法声明：开了 layerwise 就必须用 PIECEWISE（分段）图模式，因为逐层 save/load 需要在层边界打断图。

[ascend_store_connector.py:77-83](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/ascend_store_connector.py#L77-L83)（`requires_piecewise_for_cudagraph` 在 `use_layerwise=True` 时返回 True）。

**本次更新的主角：`consumer_is_to_put`。** 这是 `AscendStoreConnector.__init__` 从 `kv_connector_extra_config` 读出的开关，默认 `False`：

[ascend_store_connector.py:93-95](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/ascend_store_connector.py#L93-L95)（读取 `consumer_is_to_put`，默认 False）。

它直接控制 layerwise 模式下的保存行为。**本次更新（#12852 配套）的核心改动就在 `save_kv_layer` 与 `wait_for_save`**——之前只要 `kv_role == "kv_consumer"` 就无条件跳过保存，现在改为「consumer 且未开 `consumer_is_to_put`」才跳过：

[ascend_store_connector.py:234-253](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/ascend_store_connector.py#L234-L253)（`save_kv_layer` 与 `wait_for_save` 的保存守卫：`kv_consumer and not consumer_is_to_put` 才 return）。

改动后的关键片段：

```python
def save_kv_layer(self, layer_name, kv_layer, attn_metadata, **kwargs) -> None:
    if not self.use_layerwise:
        return
    if self.kv_role == "kv_consumer" and not self.consumer_is_to_put:
        # A load-only consumer does not publish KV.
        return
    self.connector_worker.save_kv_layer(self._get_connector_metadata())

def wait_for_save(self):
    if self.kv_role == "kv_consumer" and not self.consumer_is_to_put:
        # Don't do save if the role is kv_consumer
        return
    ...
```

对应 diff（`3829122510c..7201c97a61a`）：

```diff
-        if self.kv_role == "kv_consumer":
-            # Don't do save if the role is kv_consumer
+        if self.kv_role == "kv_consumer" and not self.consumer_is_to_put:
+            # A load-only consumer does not publish KV.
             return
```

> worker 侧 `pool_worker.py` 同样用这个开关决定要不要启动「保存线程」：`can_save = self.kv_role in ["kv_producer", "kv_both"] or self.consumer_is_to_put`（见 [pool_worker.py:418](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/pool_worker.py#L418) 与 [pool_worker.py:498](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/pool_worker.py#L498)）。scheduler 侧则有 `force_skip_save = self.kv_role == "kv_consumer" and not self.consumer_is_to_put`（见 [pool_scheduler.py:937](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/pool_scheduler.py#L937)）。三处口径一致。

**LMCacheAscendConnector 是薄封装。** 它只做两件事：`import lmcache_ascend`（拉起原生库，触发其注册副作用）+ 复用上游 `LMCacheConnectorV1`。

[lmcache_ascend_connector.py:1-6](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/distributed/kv_transfer/kv_pool/lmcache_ascend_connector.py#L1-L6)（仅 import `lmcache_ascend` 并复用上游 `LMCacheConnectorV1`）。

#### 4.2.4 代码实践

**实践目标**：对比 mooncake P2P 与 ascend_store 两种后端的适用场景与数据通路；并解释 ascend_store 在 layerwise 模式下为何 load-only consumer 也需要发布 KV。

**操作步骤（源码阅读型，无需 NPU）**：

1. 在 [ascend_store_connector.py:234-253](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/ascend_store_connector.py#L234-L253) 处，把 `consumer_is_to_put` 分别假设为 `False` / `True`，跟踪 `save_kv_layer` 的两条分支分别走到哪。
2. 阅读 `docs/source/user_guide/feature_guide/kv_pool.md` 第 290-315 行，对照官方对 `consumer_is_to_put` 的说明与示例 JSON 配置（MLA 模型 + `kv_role=kv_consumer` + `consumer_is_to_put=true` + `prefill_pp_size`/`prefill_pp_layer_partition`）。
3. 填写下面的对比表（答案见「预期结果」）。

**需要观察的现象 / 预期结果**：

适用场景与数据通路对比表：

| 维度 | MooncakeConnector（P2P） | AscendStoreConnector（KV Pool） |
|------|--------------------------|---------------------------------|
| 拓扑 | P↔D 一一配对直连 | P/D 经一个 store 池中转，多对多 |
| 数据通路 | D 用 TransferEngine 直接读 P 的设备显存块（`batch_transfer_sync_read`） | P 把 KV `put` 进 store，D 先 `lookup` 命中再 `get` 取回 |
| 保存触发 | 不显式保存（save_kv_layer 为 no-op，后台线程按需读） | layerwise 下逐层 `save_kv_layer` 显式 put |
| 命中复用 | 无（每次 prefill 都要现算现传） | 有（lookup 命中的 block 可跨请求复用） |
| 适合场景 | 简单 1P1D、低延迟直连 | 多 P 多 D、长上下文、要 prefix 复用与逐层 overlap |

**为何 layerwise 模式下 load-only consumer 也需要发布 KV**：

默认（`consumer_is_to_put=False`）时，D 节点只从池里「取」KV、不往池里「存」，池里只有 P 节点 prefill 出来的 KV。这对「D 节点 decode 新算出来的 token 的 KV」是丢弃的。但在 **layerwise（逐层）池化 + MLA 这类模型**下，D 节点 decode 阶段也会逐层产生新的 KV；如果开启 `consumer_is_to_put=True`，D 节点会把自己 decode 出来的 KV 也 `put` 回池，使得**后续请求（包括别的 P/D 节点）能命中并复用 D 节点扩展过的 KV**，提升池的整体命中率。换句话说：`consumer_is_to_put` 把 D 节点从「纯消费者」升级为「消费 + 回写」的双向参与者。其代价是 D 节点要多承担 put 的网络/显存开销，且当 P 节点开了 PP 时还要配 `prefill_pp_size`/`prefill_pp_layer_partition` 以正确切分回写的层。

> 待本地验证：开启 `consumer_is_to_put` 后池命中率与 D 节点 put 耗时的实际收益，需要在真实 MLA + 多 D 部署里测（参考 `docs/source/user_guide/feature_guide/kv_pool.md` 的示例配置）。

#### 4.2.5 小练习与答案

**练习 1**：`AscendStoreConnector.requires_piecewise_for_cudagraph` 在什么条件下返回 True？为什么？
**参考答案**：当 `extra_config["use_layerwise"]` 为真时返回 True。因为 layerwise 模式要在每一层 transformer 的边界执行 save/load（put/get KV），必须在层处打断计算图，所以必须用 PIECEWISE 分段图模式而非 FULL 整图模式。

**练习 2**：如果用户在 `--kv-transfer-config` 里把 `kv_connector` 写成 `MooncakeConnectorStoreV1`，会发生什么？
**参考答案**：`__init__.py` 把这个名字也登记到了 `AscendStoreConnector`，并在 `AscendStoreConnector.__init__` 里打一条 warning，提示「建议用 AscendStoreConnector，MoonCakeStoreConnector 将来会移除」。即这是旧名字的兼容入口，功能等价。

**练习 3**：`AscendMultiConnector.update_state_after_alloc` 里为什么 `MooncakeLayerwiseConnector` 即使不是被选中的连接器，也要拿真实 blocks？
**参考答案**：layerwise 逐层传输需要在请求分配块时就拿到真实的 block ids 才能逐层 put/get；如果给它空 blocks，逐层传输链路就断了。所以它被特殊对待，始终接收真实 blocks，保证多连接器混挂时 layerwise 路径仍生效。

## 5. 综合实践

把本讲两个模块串起来：为一次 PD 分离部署「选连接器 + 推 save/load 行为」。

**任务**：假设你要部署一个 MLA 模型，1 个 P 节点（TP=2, DP=2）、2 个 D 节点（TP=2, DP=2），希望 D 节点 decode 出来的 KV 也能被别的 D 节点复用，且要逐层 overlap 传输。

1. **选连接器**：根据 4.2 的对比表，选 `AscendStoreConnector`（需要池化复用 + layerwise），而非 P2P 的 `MooncakeConnector`。
2. **写 D 节点 `--kv-transfer-config` 关键字段**（示例代码，参考 `docs/source/user_guide/feature_guide/kv_pool.md`）：

   ```json
   {
     "kv_connector": "AscendStoreConnector",
     "kv_role": "kv_consumer",
     "kv_connector_extra_config": {
       "backend": "mooncake",
       "use_layerwise": true,
       "consumer_is_to_put": true,
       "prefill_pp_size": 1
     }
   }
   ```

3. **预测行为**：根据 4.2.3 的源码，逐项回答：
   - `requires_piecewise_for_cudagraph` → True（`use_layerwise=True`），所以 vLLM 会用 PIECEWISE 图模式。
   - `save_kv_layer` 守卫：`kv_consumer and not consumer_is_to_put` → `True and not True` → `False`，**不**跳过，于是 D 节点会逐层把 decode 的 KV `put` 进池。
   - `force_skip_save`（scheduler 侧）：同样为 False，scheduler 允许排队保存。
4. **验证**：在真实环境跑通后，观察池的 lookup 命中率是否随 D 节点回写而上升（待本地验证）。

> 若暂时没有 NPU，可把第 3 步的「预测行为」作为纯源码推演练习：把 `consumer_is_to_put` 改回 `false` 再推一遍，确认 save 路径会被跳过、D 节点不回写。

## 6. 本讲小结

- **PD 分离**把算力密集的 prefill 与访存密集的 decode 拆到两套 NPU，用 `kv_role`（producer/consumer/both）区分角色，中间靠 KV 传输连接器搬一次 KV。
- **连接器分 scheduler/worker 双角色**：scheduler 在引擎核心决策「取不取」，worker 在每个子进程执行「真正读写与网络传输」，KV 加载与引擎调度异步并行。
- **三类后端定位不同**：`MooncakeConnector`（P2P 直连、不显式保存）、`AscendStoreConnector`（池化、支持 layerwise 逐层 put/get、可命中复用）、`LMCacheAscendConnector`（复用 lmcache 生态的薄封装）；`AscendMultiConnector` 用来把多个子连接器串挂。
- **注册靠 `register_connector()` 登记到上游工厂**，对上游同名连接器用「pop 再注册」覆盖，全程不改上游源码。
- **本次更新核心**：`AscendStoreConnector` 的 layerwise 保存守卫从「`kv_consumer` 无条件跳过」改为「`kv_consumer and not consumer_is_to_put` 才跳过」；`consumer_is_to_put=True` 让 MLA 等 D 节点也能把 decode 出的 KV 回写进池，提升跨节点命中率（worker/scheduler 三处口径一致）。
- **layerwise 与图模式绑定**：开 `use_layerwise` 即强制 PIECEWISE 分段图模式，因为要在层边界 save/load。

## 7. 下一步学习建议

- **接 u10-l7**：`AscendStoreConnector` 在 layerwise 模式下「逐层 put/get」背后的数据面（`pool_worker` / `pool_scheduler` / `layerwise_cache_layout.py` 的 `LayerwiseCacheLayout` / `config_data` / `kv_transfer`）以及「分层 KV 缓冲复用」如何在多个 transformer 层间分时复用有限物理缓冲，是下一讲的重点。
- **接 u10-l3**：KV 卸载与睡眠模式，看 KV 如何在 CPU↔NPU 间搬运、与 PD 分离的连接器如何配合（`recompute_cpu_offload`、`simple_cpu_offload` 也是以连接器形式注册）。
- **接 u10-l6**：稀疏 KV 卸载，看 SFA/Indexer 模型如何把 KV 卸载到主机（`sparse_kv_offload` 同样在 `kv_transfer/` 下）。
- 想深入 P2P 细节，可继续读 `mooncake_connector.py` 的 `KVCacheRecvingThread` / `KVCacheSendingThread`（后台收发线程与 ZMQ 握手协议）。
