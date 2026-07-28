# vLLM Prefill 连接器：claim 纪律、chunked prefill 与后台发送

## 1. 本讲目标

学完本讲，你应当能够：

1. 说清 `TileRTConnector` 作为 vLLM V1 `KVConnector` 插件的加载方式，以及它在 **scheduler 进程** 与 **worker 进程** 两侧各自承担的职责。
2. 解释 `_claim` 纪律为什么能让 TileRT 连接器与 vLLM 原生连接器（如 `NixlConnector`）安全共存于 `MultiConnector` 之下。
3. 看懂 chunked prefill 下，连接器如何跨调度步**累积** prompt 分块、并在 prefill 完成的瞬间**发射** `_emit`。
4. 串联 worker 侧的完整发送链路：`register_kv_caches → classify_layers → wait_for_save → extract → rdma_plan → _send`，并理解「控制平面 TCP 握手 + 数据平面 RDMA 写」的双轨设计。

本讲是 u4-l1（PD 分离架构总览与 ModelProfile 抽象）的直接下游：u4-l1 给出了「框架模型无关、模型差异收口到 ModelProfile」的全景，本讲把镜头推进到 **prefill 节点这一侧** 的具体实现。

## 2. 前置知识

- **PD 分离**（来自 u4-l1）：vLLM 做 prefill 产出首 token 并填好 KV 缓存，再经 RDMA 把 KV 状态搬到 TileRT decode 节点继续解码。本讲只看「送」的这一半。
- **vLLM V1 `KVConnector` 接口**：vLLM 把「KV 缓存的跨节点搬运」抽象成一组钩子（`build_connector_meta`、`wait_for_save`、`register_kv_caches` 等），用户实现一个插件类即可挂入 prefill/decode 流程，无需 fork vLLM。TileRT 的连接器就是这样一个插件。
- **分页 KV 缓存（paged KV cache）**：vLLM 把 KV 缓存按固定大小（page，TileRT 用 64 token/页）切成块，每个块由 `block_id` 索引。一个请求的 KV 散落在若干 page 里，靠 `block_ids` 找回。
- **MLA 潜在 KV 的 TP 复制**（来自 u4-l1）：DeepSeek/GLM 系列的 MLA 把 KV 压到 `kv_lora_rank=512` 的潜空间，这个潜 KV 在张量并行（TP）的各 rank 间是**复制**而非切分的，因此 PD 发送时只有 rank 0 需要发，省掉 7/8 的 staging 带宽（`sender_ranks={0}`）。
- **chunked prefill**：长 prompt 不会一次性算完，vLLM 的调度器把它切成若干 chunk，每个调度步只算一部分 token，边算边分配新的 KV page。这对「何时才算 prefill 完成」提出了判断需求。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [tilert/pd_vllm/prefill_connector.py](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/prefill_connector.py) | **本讲主角**。`TileRTConnector` 插件类，含 scheduler 侧（claim/chunked 跟踪）与 worker 侧（抽取/发送）全部逻辑。 |
| [tilert/pd_vllm/wire.py](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/wire.py) | 控制平面协议：`MAGIC`、长度前缀 JSON 帧的 `send_msg/recv_msg`、hello 信封 `hello_msg`、`derive_rid`。 |
| [tilert/pd_vllm/profiles/base.py](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/profiles/base.py) | `ModelProfile` Protocol。连接器只认这个协议，具体模型（GLM-5/DSv3.2）的差异都在这里收口。 |
| [tilert/pd_vllm/profiles/mla_nsa.py](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/profiles/mla_nsa.py) | GLM-5/DSv3.2 共享的数据平面：`classify_layers/staging_bytes/extract/rdma_plan` 的真正实现。 |
| [tilert/pd_vllm/transport.py](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/transport.py) | RDMA 传输抽象：`Transport` 基类 + `MooncakeTransport` / `NixlTransport` 两实现 + `make_transport` 工厂。 |

---

## 4. 核心概念与源码讲解

### 4.1 插件加载与 scheduler / worker 双侧架构

#### 4.1.1 概念说明

`TileRTConnector` 不是一个独立进程，而是**寄生在 vLLM prefill 进程内部**的插件。它通过 vLLM 标准的 `kv_connector_module_path` 机制被加载，完全不需要 fork 或 patch vLLM。README 里 Topology A 的启动参数清楚展示了这一点：

[README.md:347-354](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/README.md#L347-L354) — `--kv-transfer-config` 指明 `kv_connector=TileRTConnector`、`kv_connector_module_path=tilert.pd_vllm.prefill_connector`、`kv_role=kv_producer`，以及 `tilert_host/tilert_ctrl_port/tilert_model/tilert_max_seq_len/tilert_transport` 五项 extra config。

理解这个连接器的关键是意识到它**同时活在两个地方**，由 vLLM 的分布式架构决定：

- **scheduler 侧**：运行在 vLLM 的调度器进程里，能看到「请求长什么样、prompt 多长、分到哪些 page、算到第几个 token」，但**看不到 GPU 上的 KV 张量**。它负责判断「这个请求该不该我管」「prefill 算完没有」，并把结论打包成元数据。
- **worker 侧**：运行在每一个 TP rank 的 worker 进程里，能直接摸到 GPU 上的分页 KV 缓存张量。它负责真正把 KV 抽出来、搬到 staging 缓冲、经 RDMA 发到 decode 节点。

两侧之间靠一个**连接器元数据对象**（`TileRTMetadata`）通信：scheduler 侧在 `build_connector_meta` 里把它填好，vLLM 自动序列化并送到 worker，worker 在 `wait_for_save` 里取出来用。

#### 4.1.2 核心流程

```
vLLM 进程启动
  └─ 读 --kv-transfer-config → 实例化 TileRTConnector(vllm_config, role, ...)
       ├─ 解析 extra config（host/port/model/transport/max_seq/sync_send）
       ├─ profiles.get_profile(tilert_model) → 选定 ModelProfile
       └─ 初始化空容器（scheduler 侧 _pending、worker 侧 _kv_caches/_transport 全空）

每一步调度：
  scheduler 侧: build_connector_meta(scheduler_output) → TileRTMetadata
  worker 侧:    register_kv_caches（仅首次） → wait_for_save（每步）
```

`__init__` 里把 vLLM 传进来的 extra config 一项项读出来，其中最关键的是 `tilert_model`，它决定加载哪个 `ModelProfile`：

[tilert/pd_vllm/prefill_connector.py:73-102](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/prefill_connector.py#L73-L102) — 构造函数解析 extra config，并用 `profiles.get_profile(...)` 选定模型 profile；scheduler 与 worker 字段都先留空（worker 侧资源是懒加载的，见 4.4）。

注意构造函数本身**不连网络、不分配显存**：`_transport`、`_staging`、`_sender_thread` 都是 `None`，等真正第一次要发送时才在 `_ensure_worker_ready` 里创建。这是一种典型的「懒初始化」，避免在不需要发送的 worker（非 sender rank）上白白分配资源。

#### 4.1.3 源码精读

类声明与基类：

[tilert/pd_vllm/prefill_connector.py:70-71](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/prefill_connector.py#L70-L71) — `class TileRTConnector(KVConnectorBase_V1, SupportsHMA)`：继承 vLLM V1 连接器基类，实现它的全部钩子。

元数据容器（scheduler→worker 的桥梁）：

[tilert/pd_vllm/prefill_connector.py:54-56](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/prefill_connector.py#L54-L56) — `TileRTMetadata(KVConnectorMetadata)` 只多了一个 `requests: list`，里面装的就是 4.3 要讲的 `_ReqMeta`。

很多钩子是「空实现」，因为 TileRT 是**纯 producer**（只发 KV 出去，不接收 decode 节点回传的 KV）：

[tilert/pd_vllm/prefill_connector.py:120-124](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/prefill_connector.py#L120-L124) — `get_num_new_matched_tokens` 恒返回 `(0, False)`、`update_state_after_alloc` 为空：TileRT 不做前缀缓存匹配，也不在分配后立刻做事。

[tilert/pd_vllm/prefill_connector.py:242-249](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/prefill_connector.py#L242-L249) — `start_load_kv/wait_for_layer_load/save_kv_layer` 全空：producer 角色不需要从远端「拉回」KV 加载。

#### 4.1.4 代码实践

**实践目标**：建立「插件 = 一组钩子」的直觉，确认 TileRT 只实现了 producer 所需的最小子集。

**操作步骤**：

1. 打开 [tilert/pd_vllm/prefill_connector.py](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/prefill_connector.py)。
2. 列出 `TileRTConnector` 定义的全部方法，把它们分成三组：
   - **scheduler 侧**：`_claim`、`_params_of`、`build_connector_meta`、`_emit`、`request_finished`
   - **worker 侧**：`register_kv_caches`、`_ensure_worker_ready`、`wait_for_save`、`_sender_loop`、`_send`
   - **空实现**（producer 不需要）：`get_num_new_matched_tokens`、`update_state_after_alloc`、`start_load_kv`、`wait_for_layer_load`、`save_kv_layer`、`get_finished`
3. 对照 README 的 `--kv-transfer-config`（[README.md:347-354](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/README.md#L347-L354)），把每个 extra config 键对应回 `__init__` 里的读取行。

**需要观察的现象**：你会发现几乎所有「consumer 行为」（拉 KV、匹配前缀、加载层）都是空操作。这是 TileRT 作为纯 producer 的指纹。

**预期结果**：得到一张「钩子 → 侧 → 是否实际有逻辑」的三列表。

**待本地验证**：若你装了 vLLM，可在 Python 里 `from vllm.distributed.kv_transfer.kv_connector.v1.base import KVConnectorBase_V1; print([m for m in dir(KVConnectorBase_V1) if not m.startswith('_')])` 对照本类覆盖了哪些。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `__init__` 不在构造时就连 TCP、分配 staging？
**答案**：因为不是每个 worker rank 都会发送（只有 `sender_ranks={0}` 会发），且只有真正有请求要发时才需要这些资源。懒初始化让 7 个非 sender rank 和「还没有请求」的早期阶段零开销，见 4.4 的 `_ensure_worker_ready` 守卫 `if self._transport is not None: return`。

**练习 2**：`TileRTMetadata` 只比父类多一个 `requests` 列表，为什么这么薄？
**答案**：scheduler 侧已经把每个待发请求压成了 `_ReqMeta`（含 rid、block_ids、host 等），worker 侧拿到这个列表后自己再调 profile 去抽 KV。元数据只携带「调度信息」，不携带 KV 本身——KV 在 worker 本地 GPU 上。

---

### 4.2 claim 纪律与 MultiConnector 安全

#### 4.2.1 概念说明

vLLM V1 支持把多个连接器**组合**起来用，叫 `MultiConnector`。README 的 Topology B 就是一个典型场景：

[README.md:385-402](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/README.md#L385-L402) — 同一个 prefill 池挂了 `NixlConnector`（原生 vLLM decode）和 `TileRTConnector`（TileRT decode），背后是同一个 OpenAI 接口。**延迟敏感**的请求走 TileRT，**普通**请求走原生 vLLM decode。

这带来一个纪律问题：每来一个请求，到底该由哪个连接器管？vLLM 的做法是让每个子连接器有机会**声明（claim）**这个请求。如果两个连接器都「贪心」地认领或跟踪所有请求，就会冲突。

TileRT 的纪律非常严格，叫 **claim 纪律**：**只认 `kv_transfer_params` 里带 `tilert_host` 的请求，其余请求一律「严格 no-op」（strict no-op）**。模块开头 docstring 把这条纪律写成了铁律：

[tilert/pd_vllm/prefill_connector.py:14-16](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/prefill_connector.py#L14-L16) — 「Claim discipline (MultiConnector-safe): only requests whose `kv_transfer_params` carry `tilert_host` are claimed; everything else is a strict no-op so a native connector can coexist.」

「严格 no-op」的含义是：对不属于我的请求，TileRT 在**每一个**钩子里都表现得像不存在——不匹配 token、不分配、不跟踪 block、不发元数据、不发 KV。这样 `NixlConnector` 可以无干扰地接管其余请求。

#### 4.2.2 核心流程

```
vLLM 调度器收到新请求 new_req
  └─ MultiConnector 依次询问每个子连接器
       └─ TileRT._params_of(new_req)
            └─ 取 new_req.sampling_params.extra_args["kv_transfer_params"]
            └─ _claim(params):
                 if params.get("tilert_host"):  ← 我的！
                     return params
                 else:
                     return None                 ← 不是我的
  ┌─ None  → 后续 build_connector_meta 直接 continue（严格 no-op）
  └─ dict  → 进入 chunked 跟踪 / 发射流程（4.3）
```

为什么选 `tilert_host` 作为认领凭证？因为它是 TileRT **专用**的、且**必填**的配置项——一个请求若要送 TileRT，就必须指明 decode 节点地址。用「是否存在 tilert_host」做判据，既自然又无歧义。

#### 4.2.3 源码精读

认领逻辑的全部实现只有 4 行：

[tilert/pd_vllm/prefill_connector.py:106-111](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/prefill_connector.py#L106-L111) — `_claim(params)`：当且仅当 `params` 是 dict 且 `params.get("tilert_host")` 为真时返回 `params`，否则返回 `None`。

从请求对象里取参数的胶水：

[tilert/pd_vllm/prefill_connector.py:113-118](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/prefill_connector.py#L113-L118) — `_params_of(new_req)`：从 `new_req.sampling_params.extra_args.kv_transfer_params` 取出参数再交给 `_claim`。这一层把「vLLM 请求结构」与「判据」解耦。

「严格 no-op」在 `build_connector_meta` 里的落地：

[tilert/pd_vllm/prefill_connector.py:135-138](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/prefill_connector.py#L135-L138) — 对每个新请求调 `_params_of`，若返回 `None` 就 `continue`，注释明确写着「not ours — strict no-op (MultiConnector safety)」。该请求对 TileRT 完全透明，不会被加入任何列表。

注意 `_claim` 是 `@staticmethod`，只依赖入参 `params` 本身，不带任何实例状态——这是纪律「纯粹、可组合」的体现：任何连接器实例、任何时刻，对同一个请求的认领结论都一致。

#### 4.2.4 代码实践

**实践目标**：亲手验证 claim 纪律的二值性，并理解它对 MultiConnector 的意义。

**操作步骤**：

1. 阅读本讲模块开头 docstring [prefill_connector.py:1-22](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/prefill_connector.py#L1-L22)，找出作者对 claim 纪律的措辞。
2. 对照 Topology B 的 config（[README.md:392-402](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/README.md#L392-L402)），回答：一个**没有** `tilert_host` 的请求，会被 `NixlConnector` 还是 `TileRTConnector` 处理？为什么不会两边都抢？
3. 写一小段伪代码模拟 `MultiConnector` 的分发：

```python
# 示例代码（非项目源码，仅用于理解分发逻辑）
def multi_claim(connectors, new_req):
    for c in connectors:               # NixlConnector, TileRTConnector
        if c._claim(_params_of(new_req)):   # TileRT 用 tilert_host 判据
            return c
    return None
```

**需要观察的现象**：`_claim` 的判据是**存在性**（`params.get("tilert_host")` 真值），而非某个布尔开关。

**预期结果**：能口述「带 `tilert_host` → TileRT；不带 → 透传给 NixlConnector」，并解释这种「单一字段判据 + 其余严格 no-op」是 MultiConnector 安全的必要条件。

#### 4.2.5 小练习与答案

**练习 1**：如果 `_claim` 改成「总是返回 params」（贪心认领），Topology B 会出什么问题？
**答案**：每个请求都会被 TileRT 跟踪并发往某个 TileRT decode 节点，原生 `NixlConnector` 形同虚设，普通流量也被强行送进 TileRT 池，违背 Topology B「同一入口分流两类流量」的初衷；更糟的是非 TileRT 请求根本没有合法的 `tilert_host`，`_emit` 里的 `assert host is not None` 会直接抛错。

**练习 2**：为什么认领凭证用 `tilert_host` 而不是另设一个 `use_tilert=True` 布尔字段？
**答案**：`tilert_host` 本就是送 TileRT 的**必填**参数（不指明 decode 地址根本没法发）。用它做判据避免了冗余字段，也保证了「被认领的请求一定携带完整发送信息」这一不变量。

---

### 4.3 chunked prefill 累积

#### 4.3.1 概念说明

vLLM 的 chunked prefill 意味着一个长 prompt 会被切成多个 chunk，每个调度步只 prefill 一段。比如一个 8192 token 的 prompt，可能第一步算 2048 个、第二步算 2048 个……直到算完。每算一段，调度器就给这个请求**新分配**若干 KV page。

对 TileRT 来说，我们要在 **prefill 完整算完之后**，把这个 prompt 的全部 KV **一次性**发给 decode 节点（发半截 KV 没有意义）。因此连接器必须做两件事：

1. **累积**：跨调度步收集这个请求的 prompt token、page block id，直到判定 prefill 完成。
2. **发射**：完成的那一刻，构造一个 `_ReqMeta` 放进元数据，触发后续 worker 发送。

判定「prefill 是否完成」的判据是：**已计算 token 数 ≥ prompt 总长**。即这一步结束时，prompt 的每一个 token 都已经过前向、KV 都已落盘。

#### 4.3.2 核心流程

`build_connector_meta` 是 scheduler 侧每步必调的钩子，逻辑分三段：

```
build_connector_meta(scheduler_output):
  ① 清理：finished / preempted 的请求 → 从 _pending 删除
  ② 处理新请求 scheduled_new_reqs:
       对每个 new_req:
         若不是我们的（_params_of→None）→ continue（4.2 的 no-op）
         若 token_ids 为空 → continue
         n = 本步为该请求调度的 token 数
         if 已计算 + n ≥ prompt 总长:
             → _emit(...) 直接进元数据（短 prompt，一步算完）
         else:
             → 存进 _Pending（长 prompt，待后续 chunk 累积）
  ③ 处理恢复请求 scheduled_cached_reqs（已经在 _pending 里的多 chunk 请求）:
       把本步新分配的 block_id 追加进 p.block_ids_per_group
       if 已计算 + n ≥ total_tokens:
             → _emit(...) 并从 _pending 删除（累积完成，发射）
```

核心是**一个判断式**反复出现：

\[ \text{num\_computed\_tokens} + n \geq \text{total\_tokens} \]

其中 \(n\) 是本调度步为该请求新算的 token 数。满足即代表 prefill 完成。

#### 4.3.3 源码精读

`_Pending` 是 scheduler 侧的累积容器：

[tilert/pd_vllm/prefill_connector.py:59-67](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/prefill_connector.py#L59-L67) — `_Pending` 存 `prompt_token_ids`、`total_tokens`、`block_ids_per_group`（随 chunk 增长）、`params`。

`build_connector_meta` 的三段：

[tilert/pd_vllm/prefill_connector.py:126-153](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/prefill_connector.py#L126-L153) — 清理（L130-133）+ 新请求分流（L135-153）。注意新请求分流里：一步算完则 `_emit`，否则 `_Pending` 入队，判据是 `new_req.num_computed_tokens + n >= len(token_ids)`。

[tilert/pd_vllm/prefill_connector.py:155-171](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/prefill_connector.py#L155-L171) — 恢复请求段：把 `cached.new_block_ids[i]` 的每个 group 追加进 `p.block_ids_per_group`（L160-164），再用同一判据决定是否 `_emit` 并 `del self._pending[req_id]`。

`_emit` 构造待发请求的元数据：

[tilert/pd_vllm/prefill_connector.py:173-194](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/prefill_connector.py#L173-L194) — `_emit` 产出 `_ReqMeta`，关键字段：
- `rid = derive_rid(req_id)`：把 vLLM 内部 request id 归一化成客户端可见的 rid（router 侧也用同样函数，保证两端一致，见 [wire.py:26-43](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/wire.py#L26-L43)）。
- `num_tokens = len(token_ids)`：prompt 长度，决定要发多少 KV。
- `last_prompt_token = int(token_ids[-1])`：prompt 最后一个 token，decode 节点要靠它开始解码。
- `block_ids_per_group = groups`：按 KV cache group 分组的 page id 列表，worker 据此从分页缓存里找回 KV（见 4.4 的 `extract`）。

`_ReqMeta` 本身：

[tilert/pd_vllm/prefill_connector.py:42-51](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/prefill_connector.py#L42-L51) — 字段清单。

清理逻辑也要留意：被抢占（preempt）的请求必须从 `_pending` 拿掉，否则它恢复后会带着过期的 block id。

[tilert/pd_vllm/prefill_connector.py:130-133](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/prefill_connector.py#L130-L133) — `finished_req_ids` 与 `preempted_req_ids` 都会从 `_pending` 移除。

#### 4.3.4 代码实践

**实践目标**：跟踪一个被切成两 chunk 的长 prompt，看清它何时被发射。

**操作步骤**：

1. 假设一个 prompt 长度 `total = 4096`，vLLM 每步最多算 2048 token。设它无前缀缓存（`num_computed_tokens=0`）。
2. **第 1 步**：`scheduled_new_reqs` 含该请求，`n=2048`。代入判据 `0 + 2048 >= 4096` → False → 存入 `_Pending`（`block_ids_per_group` 记录第 1 段的 page id）。
3. **第 2 步**：该请求出现在 `scheduled_cached_reqs`，`n=2048`，`cached.num_computed_tokens=2048`，新分配的 block 追加进 `p.block_ids_per_group`。代入判据 `2048 + 2048 >= 4096` → True → `_emit` 并从 `_pending` 删除。
4. 在源码 [prefill_connector.py:135-171](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/prefill_connector.py#L135-L171) 上把这两步分别用箭头标出「进入哪一段、命中哪个分支」。

**需要观察的现象**：`block_ids_per_group` 在两步之间是**累积**的（第 1 段 + 第 2 段的 page 拼起来），这样 worker 抽 KV 时才能拿到完整的 page 列表。

**预期结果**：能画出「第 1 步入队、第 2 步出队发射」的时间线，并指出 `_emit` 只发生在第 2 步末尾。

**待本地验证**：若你在 vLLM 里实际跑，可在 `_emit` 的 `logger.info` 处加断点/日志（[L186-194](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/prefill_connector.py#L186-L194)），确认长 prompt 的发射次数恰为 1。

#### 4.3.5 小练习与答案

**练习 1**：为什么短 prompt（一步能算完）走 `_emit`，长 prompt 走 `_Pending`？
**答案**：短 prompt 这一步就满足 `num_computed + n ≥ total`，没有「后续 chunk」可等，直接发射最高效；长 prompt 这一步没算完，KV 还没全部产生，必须等后续 chunk 把剩下的 page id 累积齐再发，否则发出的 KV 是残缺的。

**练习 2**：`block_ids_per_group` 为什么要按 group 组织，而不是一个扁平的 block id 列表？
**答案**：vLLM 的 KV 缓存可能按 `kv_cache_groups` 分组（不同组的层共享不同 page 池），同一请求在不同组的 page id 序列不同。`extract` 时需要按 group 取对应 page（见 [mla_nsa.py:290](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/profiles/mla_nsa.py#L290) 的 `reg.mla_layers[0][3]` 取 group 索引），所以必须分组保留。

---

### 4.4 worker 侧 KV 抽取、staging 与后台发送

#### 4.4.1 概念说明

scheduler 侧只决定「发谁、发多少」，真正「把 KV 从 GPU 分页缓存里抠出来、塞到网络上」发生在 **worker 侧**。这一侧有三个核心概念：

1. **classify_layers（注册）**：vLLM 启动后会把本 worker 的分页 KV 缓存张量交给连接器。连接器需要弄清楚「哪一层是 MLA 层、哪一层是 NSA 索引（KI）层、它们各属于哪个 group、layer id 是几」——这是模型相关的事，于是委托给 `profile.classify_layers`。
2. **staging 缓冲**：KV 在分页缓存里是**散落**的（按 page 存），而 RDMA 发送需要**连续**显存。于是 worker 预先开一块连续的 `staging` 显存，把要发的 KV 先 `copy_` 进去凑成线性布局，再发。
3. **控制平面 + 数据平面双轨**：
   - **控制平面**：TCP 上传短 JSON 帧（hello 握手、请求元数据、done 确认），用来「告诉对端我要发什么、发到哪个地址」。
   - **数据平面**：RDMA 直接把 staging 里的字节写进对端 GPU 显存（mooncake 或 nixl），不走 CPU、不拷贝。

发送可以**同步**（`tilert_sync_send=True`，阻塞在 forward 里发完）或**异步**（默认，丢进队列由后台线程发），异步模式让 vLLM 的 prefill 计算和网络发送**重叠**，不互相拖累。

#### 4.4.2 核心流程

```
worker 侧生命周期:

(1) register_kv_caches(kv_caches)  ← vLLM 启动时调一次
      └─ profile.classify_layers(kv_caches, cfg) → _reg（层映射）

(2) 每步 forward 后: wait_for_save()
      └─ 取 TileRTMetadata.requests
      └─ _ensure_worker_ready()  ← 懒初始化（首次）
           ├─ tp_rank = get_tensor_model_parallel_rank()
           ├─ total = profile.staging_bytes(reg, tp_rank, max_seq)
           │      （非 sender rank → 4 字节占位；sender rank → 全量）
           ├─ staging = torch.zeros(total, uint8, cuda)
           ├─ transport = make_transport(name); init/register(staging)
           └─ 起后台 _sender_loop 线程（异步模式）
      └─ if tp_rank not in profile.sender_ranks: return   ← 只 rank0 发
      └─ for m in requests:
           sections = profile.extract(reg, m, tp_rank, staging, max_seq)
                       ↑ 把分页 KV 抽进 staging 连续缓冲
           job = {meta:m, sections, seq}
           if sync_send: _send(job)          ← 同步
           else:         _send_q.put(job)    ← 异步入队

(3) _send(job)  ← 同步直调 / 后台线程消费
      ├─ TCP connect(tilert_host, ctrl_port)
      ├─ hello = recv_msg()  → 校验 magic / layout_version / transport / max_seq
      ├─ send_msg({rid, rank, seq_len, last_prompt_token, sampling})
      ├─ (srcs, dsts, lens) = profile.rdma_plan(hello, sections, ...)
      ├─ transport.write(hello, srcs, dsts, lens)   ← RDMA 写
      └─ send_msg({done, rid, rank}); conn.close()
```

**sender_ranks 过滤**是省带宽的关键：MLA 的潜 KV 在 TP 间复制，8 个 rank 持有完全相同的 KV，只有 rank 0 发就够了。

[tilert/pd_vllm/profiles/mla_nsa.py:89](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/profiles/mla_nsa.py#L89) — `sender_ranks = frozenset({0})`，注释「MLA latent replicated across TP」。

对应地，非 sender rank 的 staging 几乎不分配：

[tilert/pd_vllm/profiles/mla_nsa.py:280-283](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/profiles/mla_nsa.py#L280-L283) — `staging_bytes`：非 sender rank 返回 4（占位），sender rank 返回全量 `buffer_bytes(max_seq)`。

#### 4.4.3 源码精读

**classify_layers（注册阶段）**：

[tilert/pd_vllm/prefill_connector.py:205-208](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/prefill_connector.py#L205-L208) — `register_kv_caches` 把 `kv_caches` 存下，并调 `profile.classify_layers` 产出 `_reg`。

[tilert/pd_vllm/profiles/mla_nsa.py:231-278](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/profiles/mla_nsa.py#L231-L278) — `classify_layers` 的三件事：(a) 按 group 收集 `group_of`；(b) 用正则 `\.(\d+)\.` 解析层号、`mtp.` 前缀映射到最后一层（MTP draft）；(c) 按名字把层分成 `mla_layers` 与 `ki_layers`（NSA 索引层），并校验数量等于 `num_layers`。它还**从实际缓存 stride 自动推断 MLA 缓存 dtype**（fp8_ds_mla vs bf16），因为「prefill 缓存是 ground truth」（[L258-270](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/profiles/mla_nsa.py#L258-L270)），decode 侧则通过 `--kv-cache-dtype` 告知——若两端 dtype 不一致，会在 hello 握手时被 `layout_version` 拦下（见 4.4.4）。

**懒初始化**：

[tilert/pd_vllm/prefill_connector.py:210-240](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/prefill_connector.py#L210-L240) — `_ensure_worker_ready`：取 tp_rank、算 staging 大小、分配显存、`make_transport`→`init`→`register(staging.data_ptr(), total, dev)`，并在异步模式下起守护线程 `_sender_loop`。守卫 `if self._transport is not None: return` 保证只初始化一次。

**wait_for_save（每步发送入口）**：

[tilert/pd_vllm/prefill_connector.py:251-271](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/prefill_connector.py#L251-L271) — 关键三步：(a) `_ensure_worker_ready()`；(b) **sender_ranks 过滤** `if self._tp_rank not in self._profile.sender_ranks: return`（L257-258）；(c) 对每个请求 `profile.extract(...)` 抽 KV，再把 job 同步发或入队。

**extract（抽 KV 进 staging）**：

[tilert/pd_vllm/profiles/mla_nsa.py:285-321](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/profiles/mla_nsa.py#L285-L321) — 逻辑：
1. 由 `block_ids_per_group` 算出每个 token 在分页缓存里的全局 slot（L290-295）：`slots = (offsets + block_id * PAGE_SIZE)[:seq]`，即把 page id 还原成连续 token 偏移。
2. 逐层（L300-319）把 MLA 缓存行按字节切成 `kv_merged`（含 fp8 权重 + scale）和 `pe`（64 维 bf16 的 RoPE 位置），`copy_` 进 staging 的对应偏移；再把 KI 的若干 page 拷进 staging 的 KI 区。
3. 全程**不做 dtype 转换**，原样按字节搬运（注释 L301-302 明说「no conversion here」），反量化留到 decode 侧 `convert`。

**rdma_plan（把 staging 偏移映射到对端地址）**：

[tilert/pd_vllm/profiles/mla_nsa.py:323-342](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/profiles/mla_nsa.py#L323-L342) — 对每一层生成 3 条 (src, dst, len)：kv、pe、ki。`src = staging 基址 + 本地偏移`，`dst = hello 里的 kv_base/pe_base/ki_base + 远端偏移`。这些远端基址由 decode 节点的 `hello_layout` 给出（见 u4-l1 与下一讲 u4-l3）。

**_send（控制+数据双轨发送）**：

[tilert/pd_vllm/prefill_connector.py:286-341](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/prefill_connector.py#L286-L341) — `_send` 的双轨：
- 控制平面：TCP 连上后 `recv_msg` 读 hello，校验 `magic == "tilert-pd"`、`layout_version` 匹配、`transport` 匹配、`seq ≤ remote_max_seq`（L301-312）；再 `send_msg` 发请求信封 `{rid, rank, seq_len, last_prompt_token, sampling}`（L314-323）；发完数据后 `send_msg({done, rid, rank})`（L331）。
- 数据平面：`profile.rdma_plan(hello, sections, ...)` 算出 (srcs, dsts, lens)，调 `transport.write(hello, srcs, dsts, lens)` 真正 RDMA 写（L326-329）。

控制平面协议在 wire.py：

[tilert/pd_vllm/wire.py:46-56](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/wire.py#L46-L56) — `send_msg`/`recv_msg` 用 4 字节大端长度前缀 + JSON 体组帧；`recv_msg` 限制 `n > 16<<20`（16 MiB）防恶意/异常超大帧。`_recv_exact` 循环读到正好 n 字节，连接半途断开抛 `ConnectionError`。

[tilert/pd_vllm/wire.py:69-92](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/wire.py#L69-L92) — `hello_msg` 信封：`magic/layout_version/transport/max_seq_len/busy` + transport_meta + layout（远端基址）。

传输抽象与两实现：

[tilert/pd_vllm/transport.py:9-15](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/transport.py#L9-L15) — `Transport` 基类定义 `init/register/local_meta/write` 四件套。

[tilert/pd_vllm/transport.py:40-43](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/transport.py#L40-L43) — Mooncake 的 `write` 是一次 `batch_transfer_sync_write(session_id, srcs, dsts, lens)` 同步批量写。

[tilert/pd_vllm/transport.py:74-99](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/transport.py#L74-L99) — NIXL 的 `write` 是 `initialize_xfer("WRITE", ...)` + `transfer` + 轮询 `check_xfer_state` 直到 DONE/ERR。两种后端同一签名 `(remote_meta, srcs, dsts, lens)`，故 `_send` 里能无差别调用。

**异步发送**：

[tilert/pd_vllm/prefill_connector.py:278-284](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/prefill_connector.py#L278-L284) — `_sender_loop` 守护线程从 `_send_q.get()` 取 job 调 `_send`，异常只 `logger.exception` 不抛——因为一个请求发送失败不应让 vLLM worker 崩溃。

#### 4.4.4 代码实践

**实践目标**：把整条 worker 发送链路在脑子里跑通，并理解 hello 握手四项校验各自防什么。

**操作步骤**：

1. 追踪一个请求从 scheduler 到网线的完整路径，在源码上标号：
   - `scheduled_new_reqs`（[L135](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/prefill_connector.py#L135)）→ `_emit`（[L145/L173](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/prefill_connector.py#L173-L194)）→ 元数据跨进程到 worker → `wait_for_save`（[L251](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/prefill_connector.py#L251-L271)）→ `extract`（[mla_nsa.py:285](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/profiles/mla_nsa.py#L285-L321)）→ `_send_q.put` → `_send`（[L286](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/prefill_connector.py#L286-L341)）→ `transport.write`。
2. 解释 hello 握手四项校验（[L301-312](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/prefill_connector.py#L301-L312)）各自防什么：
   - `magic == "tilert-pd"`：防止连到非 TileRT decode 节点（端口写错）。
   - `layout_version` 匹配：防止两端 MLA 缓存 dtype 不一致（fp8 vs bf16）导致字节解释错乱。
   - `transport` 匹配：防止一端 mooncake、一端 nixl，RDMA 元数据对不上。
   - `seq ≤ remote_max_seq`：防止发的 KV 超过 decode 节点接收缓冲容量。
3. 用 `StubEngine` 思路设计一个**无 GPU** 的联调（来自 u4-l1）：让 `_send` 连到一个假的 TCP 服务器，验证 hello→请求信封→（假装 RDMA 写）→done 的控制平面时序。注意：真正的 `extract`/`transport.write` 需要 GPU，控制平面帧可用纯 Python 复现。

**需要观察的现象**：异步模式下，`wait_for_save` 把 job 入队后**立即返回**，vLLM 可继续下一步 forward；真正发送在后台线程里与计算重叠。

**预期结果**：能口述「scheduler 决策 → 元数据跨进程 → worker 抽 KV 进 staging → 后台线程 TCP 握手 + RDMA 写」的完整时序，并说出 worker 侧只在 rank 0 真正发送。

**待本地验证**：若你有 8 卡 B200 环境，可把 `tilert_sync_send` 设为 `True` 对比异步模式，观察 prefill 步耗时的差异；无 GPU 时可只跑步骤 3 的控制平面桩。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `extract` 只做字节搬运、不做 fp8→bf16 反量化？
**答案**：发送要尽量轻，反量化是计算密集操作且会放大带宽（fp8 512B → bf16 1024B）。把反量化推迟到 decode 节点的 `convert`（[mla_nsa.py:149-184](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/profiles/mla_nsa.py#L149-L184)），既省网络带宽又把计算开销放在了本就要做计算的 decode 侧。

**练习 2**：异步 `_sender_loop` 里 `except Exception: logger.exception(...)` 吞掉异常，会不会丢数据？
**答案**：会「丢」这一个请求的 KV 发送（decode 节点收不到，该请求无法继续解码），但不会让 vLLM worker 进程崩溃、不影响其他请求。这是有意的权衡：PD 分离里单请求失败由上层（router/客户端超时）处理，worker 稳定性优先。

**练习 3**：`staging_bytes` 对非 sender rank 返回 4 字节而不是 0，为什么？
**答案**：`_ensure_worker_ready` 里要 `torch.zeros(total, ...)` 并 `transport.register(ptr, total, dev)`。返回 0 会让张量为空、`data_ptr` 无意义；返回一个极小占位（4 字节）既让分配/注册正常走完，又几乎不占显存，代码路径统一。

---

## 5. 综合实践

**任务**：把本讲三个最小模块（claim 纪律、chunked 累积、worker 发送链路）串成一条完整的「请求一生」时间线，并用伪代码复述。

**背景**：假设 Topology B 部署，同一时刻有两个请求到达 prefill 池：
- 请求 A：延迟敏感，`kv_transfer_params` 带 `tilert_host=10.0.0.5`，prompt 长 6000 token（会被切成 chunked prefill，每步 2048）。
- 请求 B：普通流量，`kv_transfer_params` 不带 `tilert_host`。

**要求**：

1. **分流**（对应 4.2）：写出 A 和 B 各自由哪个连接器认领。给出 `_claim` 对两者的返回值。
2. **累积与发射**（对应 4.3）：列出请求 A 在前 3 个调度步里 `_pending` 的状态变化（入队/累积 block_id/出队发射），标出 `_emit` 发生的步序和那一刻的 `num_computed_tokens + n` 与 `total_tokens`。
3. **worker 发送**（对应 4.4）：在 A 发射的那一步，写出 rank 0 与 rank 1..7 各自做了什么（谁抽 KV、谁 `return`、谁连 TCP、谁发 RDMA）。
4. **双轨协议**：把 `_send` 里「控制平面」与「数据平面」的消息按时间顺序列成两列对照表。

**参考答案要点**：

1. A 被 TileRT 认领（`_claim` 返回 params），B 被 NixlConnector 认领（TileRT 对 B 返回 None，全程 no-op）。
2. 步 1：`0+2048<6000` → 入 `_Pending`；步 2：追加 block_id，`2048+2048<6000` → 仍在 `_Pending`；步 3：追加 block_id，`4096+2048≥6000` → `_emit` 并删除。
3. rank 0：`extract` 抽 KV 进 staging → 入队 → 后台线程 `_send`（TCP 握手 + RDMA 写）；rank 1..7：`_ensure_worker_ready` 建好 4 字节占位 staging 与 transport，但 `wait_for_save` 里因 `tp_rank not in sender_ranks` 直接 `return`，不抽不发。
4. 控制平面：`recv hello` → `send {rid,rank,seq_len,last_prompt_token,sampling}` → `send {done,rid,rank}`；数据平面：在两条控制消息之间，一次 `transport.write(srcs,dsts,lens)` 的 RDMA 批量写。

**待本地验证**：无 GPU 时可用桩程序验证控制平面时序（步骤 4）；完整链路需 8 卡 B200 + 真实 decode 节点。

## 6. 本讲小结

- `TileRTConnector` 是寄生在 vLLM prefill 进程内的 **V1 KVConnector 插件**，通过 `--kv-transfer-config` 的 `kv_connector_module_path` 加载，无需 fork vLLM；它同时活在 **scheduler 侧**（决策发谁）与 **worker 侧**（真正发 KV），靠 `TileRTMetadata` 跨进程传递。
- **claim 纪律**：`_claim` 只认带 `tilert_host` 的请求，其余严格 no-op，这是 TileRT 连接器能与 `NixlConnector` 安全共存于 `MultiConnector`（Topology B）的前提。
- **chunked prefill 累积**：`build_connector_meta` 用 `_Pending` 跨步累积 prompt token 与 block id，用判据 `num_computed_tokens + n ≥ total_tokens` 判定 prefill 完成，完成后 `_emit` 构造 `_ReqMeta` 触发发送。
- **worker 发送链路**：`register_kv_caches`→`classify_layers` 建层映射；`wait_for_save`→`extract` 把分页 KV 抽进连续 staging；`_send` 经 **TCP 控制平面握手 + RDMA 数据平面写** 把 KV 推到 decode 节点。
- **sender_ranks={0}**：因 MLA 潜 KV 在 TP 间复制，只有 rank 0 发送，省 7/8 staging 带宽；非 sender rank 仅建 4 字节占位。
- **异步发送**：默认后台 `_sender_loop` 线程消费队列，让 prefill 计算与网络发送重叠；单请求失败只记日志不崩 worker。

## 7. 下一步学习建议

本讲只看了 PD 数据平面的**发送端**。要补全整张图，建议接着读：

- **u4-l3 接收服务与控制平面协议**：精读 `receive_server.py` 与 `wire.py` 的接收侧——hello 是怎么发出来的、接收缓冲如何分配、`ReceivedRequest` 如何等齐 8 卡（注意：本讲 sender 只 rank0，但接收侧的「等齐」针对的是 rid 维度而非 rank，两者别混淆）。
- **u4-l5 RDMA 传输层抽象**：深入 `MooncakeTransport` 与 `NixlTransport` 的 `write` 同步模型差异（`batch_transfer_sync_write` vs 轮询 `check_xfer_state`），以及多 NIC 绑定 `UCX_NET_DEVICES` 的实践。
- **u4-l6 引擎接口与缓存注入**：看 KV 到了 decode 节点后如何被 `inject_cache` 逐层写回、`convert` 如何把发来的 fp8 字节反量化成 bf16——与本讲 `extract` 的「只搬不转」正好是一对正反操作。

若想巩固本讲，可重读 `prefill_connector.py` 全文（仅 342 行），它是整个 PD 数据平面最浓缩的一份源码。
