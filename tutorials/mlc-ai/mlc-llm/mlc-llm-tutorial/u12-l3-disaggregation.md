# 分离式推理（Disaggregation）

## 1. 本讲目标

本讲深入 MLC LLM 的**分离式推理（Disaggregated Inference，简称 disagg / PD 分离）**机制：把一次 LLM 推理的 prefill 阶段与 decode 阶段拆给两个不同实例，再用 GPU 间高速互联把算好的 KV cache 从「prefill 实例」搬到「decode 实例」。

学完后你应该能够：

- 说清分离式推理「为什么」能提升吞吐与显存利用，以及它解决了普通引擎里 prefill/decode 资源冲突的什么痛点；
- 读懂 `DisaggRemoteSend`（发送方）与 `DisaggPrepareReceive`（接收方）两个动作的源码，解释它们各自从 `waiting_queue` 里挑哪一类请求、调用模型的哪个接口、为什么不采样；
- 理解 disco 分布式会话与 NVSHMEM 如何在两个实例之间完成 KV cache 的跨机传输；
- 把 C++ 引擎里的两个动作、Python 的 `Router` 编排、HTTP 的三个 microserving 端点串成一条完整的请求时间线。

## 2. 前置知识

本讲是 **advanced** 层，默认你已读过下面两篇：

- **u9-l2 事件-动作循环与 Action 接口**：引擎把 prefill/decode/verify 等行为拆成「动作（`EngineActionObj`）」，`Step()` 按优先级遍历动作表，第一个返回非空者执行。本讲的两个 disagg 动作就是插在动作表最前面的新动作。
- **u10-l1 分页 KV 缓存模型接口**：KV cache 被切成固定大小的 page，序列通过 `AddNewSequence`/`ForkSequence` 申请页面。disagg 的本质就是在两个实例的分页 KV cache 之间搬页面。

此外需要三个通俗概念：

1. **prefill 与 decode 的算术强度差异**。prefill 一次性吃掉整段 prompt，计算量大、属**计算受限（compute-bound）**；decode 逐个 token 产出，每步只算一个位置，几乎全在搬权重与 KV，属**访存受限（memory-bound）**。衡量指标是算术强度

   \[
   \text{intensity} = \frac{\text{计算量（FLOPs）}}{\text{访存字节（Bytes）}}
   \]

   prefill 的 intensity 高、decode 的 intensity 低，两者对硬件的诉求截然不同。把它们塞进同一个调度循环，就会出现「一个长 prefill 卡住一批短 decode」的互相干扰。

2. **NVSHMEM 与 RDMA**。NVSHMEM 是 NVIDIA 提供的「GPU 间共享内存」库，让一张 GPU 上的 kernel 直接读写另一张 GPU 显存里的地址，底层走 NVLink/NVSwitch 的高速互联（类似 RDMA 单边读）。disagg 用它把 prefill 实例的 KV cache「推」到 decode 实例，不经过 CPU 中转。

3. **disco 会话**。TVM 的 disco 是多 GPU/多进程分布式执行框架，用一个「会话（session）」把若干 worker 进程编成一组，支持 `InitCCL`、广播、scatter 等。MLC LLM 用 disco 管理多卡与流水线并行（见 u12-l1）。disagg 的 NVSHMEM 初始化就挂在 disco 的所有 worker 上。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `cpp/serve/engine_actions/disagg_remote_send.cc` | **发送方动作**：在 prefill 实例上 prefill 后标记 KV 发送给远端 |
| `cpp/serve/engine_actions/disagg_prepare_recv.cc` | **接收方动作**：在 decode 实例上预留 KV 页面并回传元数据 |
| `cpp/serve/threaded_engine.h` | ThreadedEngine 抽象接口，后台线程驱动 `Step()` |
| `cpp/serve/config.h` | `DisaggRequestKind` 枚举与 `DisaggConfig` 结构 |
| `cpp/serve/model.h` / `cpp/serve/model.cc` | 模型层 `DisaggPrepareKVRecv` / `DisaggMarkKVSend` 接口与实现 |
| `cpp/serve/function_table.cc` | 把 KV cache 的 disagg 全局函数按名字符串解析成句柄 |
| `cpp/serve/engine.cc` | 引擎启动时按 `disaggregation` 元数据初始化 NVSHMEM |
| `cpp/serve/engine_actions/action_commons.cc` | 动作表装配：把两个 disagg 动作插到最前 |
| `python/mlc_llm/router/router.py` | Python 侧 `Router`，编排 prep_recv→remote_send→start_generate 三步 |
| `python/mlc_llm/serve/entrypoints/microserving_entrypoints.py` | 三个 HTTP 端点，把请求翻译成 `DisaggConfig` |
| `python/mlc_llm/protocol/debug_protocol.py` | Python 侧 `DisaggConfig` Pydantic 模型 |

## 4. 核心概念与源码讲解

### 4.1 总览：分离式推理的分工与三类特殊请求

#### 4.1.1 概念说明

普通引擎里，一个请求从 `waiting_queue` 进入后，先由 `NewRequestPrefill` 把整段 prompt 算成 KV，再由 `BatchDecode` 逐 token 生成。prefill 与 decode 共用同一块显存、同一条调度循环——这正是分离式推理要打破的耦合。

分离式推理的拓扑通常是「**P（prefill 实例）+ D（decode 实例）**」：

- **P 实例**只负责 prefill：吃下 prompt 前段，算出 KV，再把这段 KV 通过 NVSHMEM 发给 D；
- **D 实例**负责 decode：先在自己的分页 KV cache 里**预留**好接收位置（页号告诉 P 往哪写），等 P 把 KV 推过来后，再本地 prefill prompt 的最后一小段、随即开始流式 decode。

这样做的收益有三：

1. **独立扩缩容与调优**：P 可开大 batch 榨干算力，D 可堆更多 KV 页提升并发，两者互不抢占；
2. **消除长 prefill 对 decode 的阻塞**：D 的 `Step()` 不再被 P 的长 prefill 拖住；
3. **更大的有效 KV**：每个实例专注一相，显存利用率更高。

为了在「同一套引擎代码」上区分一个请求当前该走 P 路径还是 D 路径，MLC LLM 给请求打上一种**特殊请求种类（disagg request kind）**。

#### 4.1.2 三类 disagg 请求

引擎用一个枚举刻画三种职责，定义在 C++ 侧：

[cpp/serve/config.h:59-64](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/config.h#L59-L64)

```cpp
enum class DisaggRequestKind : int {
  kNone = 0,
  kPrepareReceive = 1,   // D：预留 KV 接收位置
  kRemoteSend = 2,        // P：prefill 后发送 KV
  kStartGeneration = 3,  // D：本地 prefill 尾段并开始 decode
};
```

三类分别对应 Python `Router` 编排的三个 HTTP 原语（见 u12-l2）：

| `DisaggRequestKind` | 对应端点 | 实例 | 动作 |
| --- | --- | --- | --- |
| `kPrepareReceive` | `/microserving/prep_recv` | D（decode） | `DisaggPrepareReceive` |
| `kRemoteSend` | `/microserving/remote_send` | P（prefill） | `DisaggRemoteSend` |
| `kStartGeneration` | `/microserving/start_generate` | D（decode） | 走普通 `NewRequestPrefill` + `BatchDecode` |

注意 `kStartGeneration` 并没有专属动作——它复用了普通 prefill/decode 动作，只是请求带了 `disagg_config`，告诉引擎「这是 D 端的尾段 prefill」。

#### 4.1.3 DisaggConfig：贯穿三段的元数据信封

请求里的 disagg 参数封装在 `DisaggConfig` 里。它在 C++ 与 Python 各有一份镜像（同名字段）：

[cpp/serve/config.h:75-95](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/config.h#L75-L95)

```cpp
class DisaggConfig {
 public:
  DisaggRequestKind kind = DisaggRequestKind::kNone;
  std::vector<Shape> kv_append_metadata;
  // "kv_window_begin"/"kv_window_end" 表示关心的 KV 区间
  // "kv_window_end" 支持类 Python 的负索引
  std::optional<int> kv_window_begin = std::nullopt;
  std::optional<int> kv_window_end = std::nullopt;
  std::optional<int> dst_group_offset = std::nullopt;
};
```

字段语义随 `kind` 变化（注释里说得很清楚）：

- **`kv_window_begin` / `kv_window_end`**：划定 `[begin:end]` 这个 KV 区间。对 `kPrepareReceive`，begin 恒为 0、`[0:end]` 是要在 P 上 prefill 的范围；对 `kRemoteSend`，`[begin:end]` 是 P 真正算并要发送的范围；对 `kStartGeneration`，end 恒为空、`[begin:]` 是 D 本地补 prefill 的尾段。
- **`kv_append_metadata`**：D 在 prep_recv 阶段回传给 P 的「接收地址」——D 的 KV cache 里预留好的页号/段信息，base64 编码成字符串跨 HTTP 传输。P 拿到它才知道该把 KV 推到 D 的哪些位置。
- **`dst_group_offset`**：D 实例在 NVSHMEM world 中的「目标 group 偏移」，告诉 P 该发给哪台机器。

`DisaggConfig` 被塞进请求的 `DebugConfig`（默认不对外暴露，需 `--enable-debug`），Python 侧同样定义在 debug 协议里：

[python/mlc_llm/protocol/debug_protocol.py:8-26](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/protocol/debug_protocol.py#L8-L26)

#### 4.1.4 两个 disagg 动作如何被插入动作表

回忆 u9-l2：引擎启动时由 `CreateEngineActions` 装配一条有序动作表。当模型的 `ModelMetadata.disaggregation` 标志为真（即 model lib 编译期就声明「我支持 disagg」），装配函数会在动作表**最前面**插入两个 disagg 动作：

[cpp/serve/engine_actions/action_commons.cc:127-135](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/action_commons.cc#L127-L135)

```cpp
if (model_metadata.disaggregation) {
  // Insert the disaggregation actions.
  Array<EngineAction> disaggregation_actions = {
      EngineAction::DisaggPrepareReceive(models, engine_config, model_configs, ...),
      EngineAction::DisaggRemoteSend(models, model_workspaces, engine_config, ...)};
  actions.insert(actions.begin(), disaggregation_actions.begin(),
                disaggregation_actions.end());
}
```

`insert(actions.begin(), ...)` 把它们插到队首，于是 `Step()` 每轮都会**先**问这两个 disagg 动作「你有事吗」。它们各自只挑自己 `kind` 的请求，没有匹配的就返回空数组、轮到下一个动作。这正是 u9-l2「职责链、第一个非空者获胜」调度模型在 disagg 场景的体现。

同时，disagg 模式下普通动作只保留 `NewRequestPrefill` + `BatchDecode`（不开推测解码、不开 jump-forward）：

[cpp/serve/engine_actions/action_commons.cc:103-112](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/action_commons.cc#L103-L112)

下面三节分别精读两个动作与 disco 协作。

### 4.2 DisaggRemoteSend：发送方（P 实例）的 prefill 与 KV 标记

#### 4.2.1 概念说明

`DisaggRemoteSend` 是 P 实例上的 prefill 动作。它的职责可以一句话概括：**对 `kRemoteSend` 请求做一次普通 prefill，但在 `AddNewSequence` 之后多调一个 `DisaggMarkKVSend`，把这段 KV 标记为「算完就发给远端 D」**。

它继承自 `BatchPrefillBaseActionObj`——也就是把普通 `NewRequestPrefill` 的 prefill 逻辑（分词、embedding、`BatchPrefill`）整个复用了，只覆盖了「挑哪些请求」和「采样与否」两处。

#### 4.2.2 核心流程

`DisaggRemoteSendActionObj::Step` 的执行骨架如下（伪代码）：

```
Step(estate):
  prefill_inputs = GetRequestStateEntriesToPrefill(estate)   # 只挑 kRemoteSend
  if 空: return {}
  for each input: MatchPrefixCache(...)                      # 命中前缀可少算
  UpdateRequestToAlive(...)                                  # pending -> alive
  for model_id in models:
      embeddings = 收集 embedding
      for each rsentry:
          if 新序列且不在 prefix cache:
              AddNewSequence(internal_id)                    # 在分页 KV 申请页
              DisaggMarkKVSend(internal_id, begin,
                               kv_append_metadata[model_id],
                               dst_group_offset)             # ★标记发送
      logits = BatchPrefill(embeddings, ids, lengths)        # 真实算 KV
  prefix_cache->CommitSequenceExtention()                    # 与 GPU 执行重叠
  DeviceAPI->StreamSync(...)                                 # ★显式同步
  RemoveProcessedRequests(...)                               # 走完即移出 waiting_queue
```

两个★是它与普通 `NewRequestPrefill` 的关键差异，下面分别解读。

#### 4.2.3 源码精读

**(a) 只挑 `kRemoteSend` 请求。** 它在重写的 `GetRequestStateEntriesToPrefill` 里显式过滤 `waiting_queue`，只保留 kind 为 `kRemoteSend` 的请求：

[cpp/serve/engine_actions/disagg_remote_send.cc:193-200](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/disagg_remote_send.cc#L193-L200)

```cpp
std::vector<Request> waiting_queue;
for (Request request : estate->waiting_queue) {
  if (request->generation_cfg->debug_config.disagg_config.kind ==
      DisaggRequestKind::kRemoteSend) {
    waiting_queue.push_back(request);
  }
}
if (waiting_queue.empty()) { return {}; }
```

这就是「同一个引擎、一个 `waiting_queue`、按 kind 分流」的实现——`DisaggPrepareReceive` 挑 `kPrepareReceive`，普通 `NewRequestPrefill` 挑其余。三者通过 kind 自然错开，无需互相感知。

**(b) 申请页面后立刻 `DisaggMarkKVSend`。** 当一条新序列第一次进 KV cache 时，先 `AddNewSequence` 拿到页面，紧接着把 `DisaggConfig` 里的参数喂给 `DisaggMarkKVSend`：

[cpp/serve/engine_actions/disagg_remote_send.cc:109-113](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/disagg_remote_send.cc#L109-L113)

```cpp
DisaggConfig disagg_config = mstate->request->generation_cfg->debug_config.disagg_config;
TVM_FFI_ICHECK(disagg_config.dst_group_offset.has_value());
models_[model_id]->DisaggMarkKVSend(
    mstate->internal_id, disagg_config.kv_window_begin.value_or(0),
    disagg_config.kv_append_metadata[model_id], disagg_config.dst_group_offset.value());
```

`kv_append_metadata` 是 D 在 prep_recv 阶段回传的「接收地址」，`dst_group_offset` 是 D 在 NVSHMEM world 里的 rank。`DisaggMarkKVSend` 的作用是告诉 KV cache：**这条序列接下来 prefill 出来的 KV，请按这份地址表直接推到那个远端 rank**。它只是「打标记」，真正的传输发生在随后的 `BatchPrefill` 写 KV 的过程中（由 NVSHMEM 在 GPU kernel 里完成，CPU 不参与）。

**(c) 跑 `BatchPrefill`，但不采样。** P 实例只算 KV、不生成 token，最后一个 token 的采样留给 D 端：

[cpp/serve/engine_actions/disagg_remote_send.cc:72-73](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/disagg_remote_send.cc#L72-L73)

> `// NOTE: we don't keep the logits as we don't run sampling in this action by design.`

代码里 `BatchPrefill` 的返回值 `logits` 仅做了形状断言（`ndim==3`、`shape[1]==num_rsentries`），随即丢弃，没有走采样器。

**(d) 显式 `StreamSync`。** 普通 prefill 动作以采样作为天然同步点；这里不采样，于是必须**手动同步**，确保 KV 真的算完、且 NVSHMEM 已把数据推出去，才能让请求离开 `waiting_queue`：

[cpp/serve/engine_actions/disagg_remote_send.cc:163-165](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/disagg_remote_send.cc#L163-L165)

```cpp
// We run synchronize to make sure that the prefill is finished.
// We need explicit synchronization because we don't do sampling in this action.
DeviceAPI::Get(device_)->StreamSync(device_, compute_stream_);
```

构造函数里也仅在 CUDA/ROCM 设备上取当前流（`compute_stream_`），因为只有这些后端才有显式的流概念：

[cpp/serve/engine_actions/disagg_remote_send.cc:36-40](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/disagg_remote_send.cc#L36-L40)

**(e) 工厂函数。** 动作对象通过 `EngineAction::DisaggRemoteSend(...)` 构造，与 `action_commons.cc` 装配处对应：

[cpp/serve/engine_actions/disagg_remote_send.cc:491-499](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/disagg_remote_send.cc#L491-L499)

#### 4.2.4 代码实践：源码阅读型——画出 P 端的「算 + 标记 + 同步」三段

1. **实践目标**：把 `DisaggRemoteSendActionObj::Step` 的关键调用顺序摸清楚，理解它为何能在普通 prefill 之上「无痛」加一层发送。
2. **操作步骤**：
   - 打开 `cpp/serve/engine_actions/disagg_remote_send.cc`，在 `Step`（第 44 行起）里定位四行：`DisaggMarkKVSend`（111）、`BatchPrefill`（152）、`CommitSequenceExtention`（161）、`StreamSync`（165）。
   - 对照 `MatchPrefixCache`（389 起）里的三种分支：`AddNewSequence`（新序列，414）、`ForkSequence`（fork 活跃序列，432）、复用回收序列（447）。注意**每个分支里都重复了 `DisaggMarkKVSend`**（420/440/461）——想想为什么不能只写一次。
3. **需要观察的现象**：三处 `DisaggMarkKVSend` 的入参完全一致，差别只在前面 `AddNewSequence`/`ForkSequence`/`RecycleId` 决定了 `internal_id` 来源。
4. **预期结果**：你能解释「无论序列是新建、fork 还是回收复用，都必须重新打发送标记」——因为标记是绑定到具体 `internal_id` 上的，换 id 就要重打。
5. **待本地验证**：若手头有多卡环境，可对照 u12-l2 用自定义 router 起一套 P+D，在 P 端日志或 NVTX trace 里确认 `DisaggMarkKVSend` 与 `BatchPrefill` 的先后与同步点。

#### 4.2.5 小练习与答案

**练习 1**：`DisaggRemoteSend` 为什么要显式 `StreamSync`，而普通 `NewRequestPrefill` 不需要？

> 参考答案：普通 prefill 紧跟采样（`SampleFromProb` 等），采样会读 logits、形成对计算流的隐式同步；`DisaggRemoteSend` 设计上不采样（最后一个 token 由 D 端采），没有这个天然同步点，于是在让请求离开 `waiting_queue` 前必须手动同步，确保 KV 已算完且 NVSHMEM 推送完毕。

**练习 2**：`kv_append_metadata[model_id]` 这个参数是 P 自己算出来的，还是 D 传过来的？

> 参考答案：是 D 传过来的。它在 D 的 prep_recv 阶段生成（见 4.3），经 Python Router 中转后塞进 P 的 `RemoteSendRequest`，P 原样读出交给 `DisaggMarkKVSend`，作为「该往 D 的哪些 KV 位置写」的地址表。

### 4.3 DisaggPrepareReceive：接收方（D 实例）的 KV 预留与元数据回传

#### 4.3.1 概念说明

`DisaggPrepareReceive` 是 D 实例上的「**接收准备**」动作。它先于 P 的发送执行：D 在自己的分页 KV cache 里为即将到来的 KV **预留页面**，然后把这些页面的「地址」打包成 `kv_append_metadata`，经引擎的流式回调回送给 Python，再由 Router 转交给 P。

它同样继承自 `BatchPrefillBaseActionObj`，但**不做任何模型前向计算**——它只调 `DisaggPrepareKVRecv` 预留位置，然后把元数据经 `request_stream_callback_` 回传。

#### 4.3.2 核心流程

```
Step(estate):
  while True:
    prefill_input = GetRequestStateEntriesToPrefill(estate)   # 只挑 kPrepareReceive
    if 无: break
    prefix_matched_length = MatchPrefixCache(...)             # D 端命中前缀则少预留
    UpdateRequestToAlive(...)
    running_queue.pop_back()                                  # ★不进 running 队列，等 P 发送
    for model_id in models:
        if 新序列: AddNewSequence / ForkSequence
        metadata = DisaggPrepareKVRecv(internal_id, prefill_length)   # ★预留页 + 返回地址
    从 waiting_queue 移除该请求
    构造 response_body {prompt_length, prefix_matched_length, kv_append_metadata(base64)}
    request_stream_callback_(Usage(extra=response_body))       # ★回传给 Python
  return processed_requests
```

#### 4.3.3 源码精读

**(a) 只挑 `kPrepareReceive`，且必须用分页 KV cache。** 过滤逻辑与 P 端对称：

[cpp/serve/engine_actions/disagg_prepare_recv.cc:204-210](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/disagg_prepare_recv.cc#L204-L210)

构造函数里硬约束 KV 状态必须是 `kKVCache`（RNN 状态不支持 KV 迁移）：

[cpp/serve/engine_actions/disagg_prepare_recv.cc:34-35](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/disagg_prepare_recv.cc#L34-L35)

```cpp
TVM_FFI_ICHECK(kv_state_kind_ == KVStateKind::kKVCache)
    << "Only PagedKVCache supports prefill preparation and KV migration";
```

**(b) `kv_window_begin` 必须为 0，且 `[0:end]` 严格小于全长。** prep_recv 阶段只预留「该由 P 算的那段」，D 自己还要本地 prefill 最后一个 token（见 `start_generate` 的 `begin=kv_window_end`）。源码用断言把这一约定钉死：

[cpp/serve/engine_actions/disagg_prepare_recv.cc:243-256](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/disagg_prepare_recv.cc#L243-L256)

```cpp
int kv_window_begin = ...disagg_config.kv_window_begin.value_or(0);
int kv_window_end   = ...disagg_config.kv_window_end.value_or(input_length);
TVM_FFI_ICHECK_EQ(kv_window_begin, 0);
...
TVM_FFI_ICHECK_LT(kv_window_end, input_length)
    << "Prefill the full input on the remote machine is not supported.";
```

`kv_window_end < input_length` 这条 ICHECK 很关键：它禁止「把整段 prompt 都让 P 算」，强制 D 至少本地 prefill 1 个 token，保证 D 的采样器能拿到正确的首 token 分布。

**(c) 调 `DisaggPrepareKVRecv` 预留页面、拿到地址。** 这是 D 端唯一的「计算」：

[cpp/serve/engine_actions/disagg_prepare_recv.cc:121-128](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/disagg_prepare_recv.cc#L121-L128)

```cpp
Shape compressed_kv_append_metadata = {0};
if (prefill_length > 0) {
  compressed_kv_append_metadata =
      models_[model_id]->DisaggPrepareKVRecv(request_internal_id, prefill_length);
}
kv_append_metadata.push_back(compressed_kv_append_metadata);
```

`compressed_kv_append_metadata` 是一个 `Shape`（即 `Array<Integer>`），编码了 D 为这段 KV 预留的页面布局。

**(d) 不进 `running_queue`，仍在等 P。** `UpdateRequestToAlive` 会顺手把请求塞进 `running_queue`，但 D 此刻还不能 decode（KV 还没到），于是立刻把它弹回：

[cpp/serve/engine_actions/disagg_prepare_recv.cc:71-74](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/disagg_prepare_recv.cc#L71-L74)

```cpp
// "UpdateRequestToAlive" may add the request to the engine's running request queue.
// We erase it since it's pending for the prefill instance to send the KV data over.
if (!estate->running_queue.empty() && estate->running_queue.back().same_as(request)) {
  estate->running_queue.pop_back();
}
```

这条请求随后从 `waiting_queue` 也被移除（138-142 行），处于「预留了页、等 KV」的悬空态，直到 P 发送完毕、Router 发来 `start_generate` 才重新进入。

**(e) 元数据格式与回传。** `compressed_kv_append_metadata` 的布局是 `[num_segments, off_1, len_1, off_2, len_2, ...]`，且校验「所有段长度之和 == prefill_length」：

[cpp/serve/engine_actions/disagg_prepare_recv.cc:156-162](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/disagg_prepare_recv.cc#L156-L162)

```cpp
int num_segments = compressed_kv_append_metadata[0];
TVM_FFI_ICHECK_EQ(compressed_kv_append_metadata.size(), num_segments * 2 + 1);
int transmission_length = 0;
for (int i = 0; i < num_segments; ++i) {
  transmission_length += compressed_kv_append_metadata[i * 2 + 2];
}
TVM_FFI_ICHECK_EQ(transmission_length, prefill_length);
```

多个模型的元数据被「拍平」成一个数组再 base64 编码，装进 `usage.extra` 经流式回调回送 Python——这正是 u11 讲过的「结果靠 `usage.extra` 跨 FFI 回传」模式（速度统计、disagg 元数据都走这条通道）：

[cpp/serve/engine_actions/disagg_prepare_recv.cc:165-177](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/disagg_prepare_recv.cc#L165-L177)

```cpp
response_body.Set("prompt_length", ...);
response_body.Set("prefix_matched_length", ...);
response_body.Set("kv_append_metadata",
                  Base64Encode(...Stringify(kv_append_metadata_arr)));
...
RequestStreamOutput stream_output =
    RequestStreamOutput::Usage(request->id, std::string(tvm::ffi::json::Stringify(usage)));
request_stream_callback_(Array<RequestStreamOutput>{stream_output});
```

#### 4.3.4 代码实践：阅读型——追踪 `kv_append_metadata` 的一生

1. **实践目标**：理解「D 预留地址 → 编码回传 → Router 中转 → P 解码使用」的完整闭环。
2. **操作步骤**：
   - 在 `disagg_prepare_recv.cc` 找到 `kv_append_metadata_arr` 的扁平化（150-154）与 base64 编码（166-167）。
   - 跳到 `python/mlc_llm/serve/entrypoints/microserving_entrypoints.py:22-45`，看 `prep_recv` 端点如何从 `response.usage.extra` 取出 `kv_append_metadata`，包进 `PrepRecvResponse` 返回。
   - 再看 `python/mlc_llm/router/router.py:274-280`，Router 把这个字符串原样塞进 `RemoteSendRequest.kv_addr_info`，发给 P。
   - 最后回到 `disagg_remote_send.cc:109-113`，确认 P 端把它读成 `disagg_config.kv_append_metadata[model_id]` 交给 `DisaggMarkKVSend`。
3. **需要观察的现象**：这个字符串全程不被解释，只在两端「打包 / 拆包」，真正的语义（页号、段长）只有 KV cache 自己懂。
4. **预期结果**：你能用一句话讲清「`kv_append_metadata` 是 D 给 P 的 KV 收件地址，Router 是送信人」。
5. **待本地验证**：若本地可起服务，在 `microserving_entrypoints.py` 的 `prep_recv` 里 `print(data["kv_append_metadata"][:32])`，对照 P 端日志确认同一字符串被消费。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `DisaggPrepareReceive` 要在 `UpdateRequestToAlive` 之后立刻 `running_queue.pop_back()`？

> 参考答案：`UpdateRequestToAlive` 会把刚激活的请求加入 `running_queue`，但 D 此时只是预留了 KV 页面、还没收到 P 的 KV，不能 decode；若留在 `running_queue`，`BatchDecode` 会因为 KV 未就绪而出错。所以立刻弹出，让请求停在「等 KV」的悬空态，直到 `start_generate` 重新把它作为新请求送入。

**练习 2**：`kv_window_end < input_length` 这条断言如果被违反（即让 P 算完整段 prompt），会出现什么问题？

> 参考答案：D 将没有任何 token 需要本地 prefill，于是 D 端没有机会对「最后一个 prompt token」采样，首 token 的对数概率分布只能来自 P——而 P 按设计不采样。这会破坏 D 端采样器对首 token 分布的控制，并使 `start_generate` 无从接续。所以强制 D 至少本地 prefill 1 个 token。

### 4.4 disco 与 NVSHMEM 远程会话协作

#### 4.4.1 概念说明

前两节讲的是「谁在什么时候调哪个接口」，但「KV 到底怎么跨机传过去」一直是个黑盒。这个黑盒由两层设施打开：

1. **模型层薄封装**：`DisaggMarkKVSend` / `DisaggPrepareKVRecv` 只是 `FunctionTable` 上两个函数句柄的转发；
2. **KV cache 内置全局函数**：真正干活的是 `vm.builtin.kv_cache_disagg_mark_send` 与 `vm.builtin.kv_cache_disagg_prepare_recv`，它们在分页 KV cache 上操作，底层用 NVSHMEM 做 GPU 间 RDMA。

而要让 NVSHMEM 工作，整个引擎必须在启动时**一次性初始化**一个跨所有实例的「world」，并由 disco 多进程会话协调。

#### 4.4.2 核心流程：从初始化到传输

```
引擎启动 (engine.cc Reload/Reset):
  [session, num_shards, ...] = CreateDiscoSession(...)         # 拉起多 worker 会话
  estate->disaggregation = models_[0].GetMetadata().disaggregation
  if disaggregation:
      读 MLC_NVSHMEM_INIT_CONFIG_JSON_STR
      DebugCallFuncOnAllAllWorker("runtime.disco.nvshmem.init_nvshmem_wrapper", cfg)
      # 所有 worker 同时初始化 NVSHMEM world

请求运行期:
  D: DisaggPrepareKVRecv(seq, len)
      -> ft_.kv_cache_disagg_prepare_recv_func_(kv_cache, seq, len)
      -> "vm.builtin.kv_cache_disagg_prepare_recv"  (返回预留页地址)
  P: DisaggMarkKVSend(seq, begin, addr, dst_group_offset)
      -> ft_.kv_cache_disagg_mark_send_func_(kv_cache, seq, begin, addr, dst)
      -> "vm.builtin.kv_cache_disagg_mark_send"     (标记 + NVSHMEM 推送)
```

#### 4.4.3 源码精读

**(a) NVSHMEM 在引擎启动时初始化。** `engine.cc` 在加载完模型后，按 `disaggregation` 元数据决定是否初始化 NVSHMEM。关键点是：**所有实例必须同时在场**才能建 world（NVSHMEM 的 `init` 是集合操作）：

[cpp/serve/engine.cc:410-425](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine.cc#L410-L425)

```cpp
n->estate_->disaggregation = n->models_[0]->GetMetadata().disaggregation;
if (n->estate_->disaggregation) {
  LOG(INFO) << "Initializing NVSHMEM";
  char* nvshmem_init_config_json_char = std::getenv("MLC_NVSHMEM_INIT_CONFIG_JSON_STR");
  TVM_FFI_ICHECK(nvshmem_init_config_json_char != nullptr)
      << "The environment variables MLC_NVSHMEM_INIT_CONFIG_JSON_STR should be set.";
  std::string f_name = "runtime.disco.nvshmem.init_nvshmem_wrapper";
  if (session != nullptr) {
    n->DebugCallFuncOnAllAllWorker(f_name, String(nvshmem_init_config_json_char));
  } else {
    static Function func = Function::GetGlobalRequired(f_name);
    func(String(nvshmem_init_config_json_char));
  }
  LOG(INFO) << "NVSHMEM initialized successfully.";
}
```

注意两个分支：有多 worker（`session != nullptr`，多卡/流水线）时用 `DebugCallFuncOnAllAllWorker` 在每个 worker 上各调一次；单进程时直接调本进程。`MLC_NVSHMEM_INIT_CONFIG_JSON_STR` 由 Python Router 在拉起每个 `PopenServer` 子进程时注入（见 u12-l2 的 `Router.__init__`，其中 `nvshmem_config` 含 `uid`/`npes`/`pe_start`）。

回看 `python/mlc_llm/router/router.py:57-91`：Router 先用 `runtime.disco.nvshmem.init_nvshmem_uid` 取得一个全局唯一 uid，再为每个 endpoint 算出它的 `pe_start`（PE = processing element，即 NVSHMEM world 里的 rank 起点），最后**并发**启动所有 server——这正是 u12-l2 强调的「子进程必须并发启动，因为 nvshmem world 需要所有 GPU 同时在场」。

**(b) `dst_group_offset` 就是 NVSHMEM 的远端 rank。** Router 在发起 `remote_send` 时，把 D 的 rank 写进请求：

[python/mlc_llm/router/router.py:274-280](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/router/router.py#L274-L280)

```python
remote_send_request = microserving_entrypoints.RemoteSendRequest(
    **original_request.model_dump(),
    begin=prefix_matched_length,
    end=kv_window_end,
    kv_addr_info=kv_append_metadata_base64,
    recv_rank=self.device_id_starts[decode_server_id],   # ← D 的 rank
)
```

`recv_rank` 经端点翻译成 `dst_group_offset`，一路传到 `DisaggMarkKVSend`，最终被 NVSHMEM 用来定位「推到哪张卡」。

**(c) 模型层只是转发。** `DisaggPrepareKVRecv` 的实现是「调全局函数、取返回值」，注意多卡时还要 `DebugGetFromRemote` 把 disco 远程对象拉回本地：

[cpp/serve/model.cc:969-986](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/model.cc#L969-L986)

```cpp
Shape DisaggPrepareKVRecv(int64_t seq_id, int length) final {
  ...
  ObjectRef ret;
  ret = ft_.kv_cache_disagg_prepare_recv_func_(kv_cache_, seq_id, length).cast<ObjectRef>();
  Shape compressed_kv_append_metadata;
  if (ft_.use_disco) {
    compressed_kv_append_metadata = ret.as_or_throw<DRef>()->DebugGetFromRemote(0).cast<Shape>();
  } else {
    compressed_kv_append_metadata = ret.as_or_throw<Shape>();
  }
  return compressed_kv_append_metadata;
}
```

`DisaggMarkKVSend` 更简单，连返回值都没有：

[cpp/serve/model.cc:988-999](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/model.cc#L988-L999)

这两条「三层同名」契约（C++ 方法 → `FunctionTable` 字段 → model lib 全局函数名字符串）已在 u9-l4 讲过，这里再补两条 KV cache 名字：

[cpp/serve/function_table.cc:259-261](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/function_table.cc#L259-L261)

```cpp
this->kv_cache_disagg_prepare_recv_func_ =
    get_global_func("vm.builtin.kv_cache_disagg_prepare_recv");
this->kv_cache_disagg_mark_send_func_ = get_global_func("vm.builtin.kv_cache_disagg_mark_send");
```

字段声明在 [cpp/serve/function_table.h:124-125](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/function_table.h#L124-L125)。这两个 `vm.builtin.kv_cache_disagg_*` 由 TVM runtime 的分页 KV cache 注册，内部用 NVSHMEM 的 put/get API 完成跨卡传输。

#### 4.4.4 代码实践：阅读型——验证「并发启动 + 集合初始化」约束

1. **实践目标**：搞清为什么 Router 必须并发拉起所有 server，以及 NVSHMEM 的 world 是怎么对齐的。
2. **操作步骤**：
   - 读 `python/mlc_llm/router/router.py:57-104`，找到 `init_nvshmem_uid`（58-59）、`pe_start`/`npes`（71-75）、并发 `thread.start()`（93-104）三处。
   - 对照 `cpp/serve/engine.cc:412-424`，确认每个子进程在 `Reload` 时都会用同一份 `uid`（来自 Router）调 `init_nvshmem_wrapper`，从而汇入同一个 world。
   - 回顾 u12-l1 的 disco `CreateDiscoSession`：`num_workers = num_shards * num_stages`，worker 进程入口是 `mlc_llm.cli.worker`。
3. **需要观察的现象**：`pe_start` 让每个 endpoint 占用 world 中互不重叠的一段 rank；`device_id_starts` 既是 GPU 起点，也是 NVSHMEM rank 起点——二者同构。
4. **预期结果**：你能解释「若串行启动 server，第一个 server 在 `init_nvshmem_wrapper` 处会一直阻塞等其它成员，直到超时」——所以 Router 用线程并发 `start`。
5. **待本地验证**：多卡机器上把 Router 的并发启动改成串行，观察首个 server 卡在 `Initializing NVSHMEM` 日志后不返回。

#### 4.4.5 小练习与答案

**练习 1**：`dst_group_offset` 与 `recv_rank` 是同一个东西吗？它最终被谁消费？

> 参考答案：是同一个语义。Python 侧叫 `recv_rank`（HTTP 字段），进入 `DisaggConfig` 后叫 `dst_group_offset`，传到 `DisaggMarkKVSend` 的第四参。它最终被 NVSHMEM 消费，用来定位 KV 推送的目标 PE（processing element）。

**练习 2**：为什么 `DisaggMarkKVSend` 在 `BatchPrefill` **之前**调用，而不是之后？

> 参考答案：它只是「打发送标记」——告诉 KV cache「这条序列后续写入的页面请同时推到 dst」。真正的写发生在 `BatchPrefill` 执行时；标记必须在写之前打好，KV cache 才能在 prefill kernel 写页面的同时触发 NVSHMEM 推送。若放在后面，则 prefill 写入的 KV 不会被推送。

## 5. 综合实践：描绘一次分离式推理的完整请求流程

把本讲三个模块串成一条端到端时间线。请按下列步骤完成「源码阅读 + 流程复述」任务。

### 实践目标

用一张时序图（文字版即可）描绘一次 disagg 请求从用户到 token 产出的全过程，并在每一步标注：**哪个进程、哪个文件/函数、哪个 `DisaggRequestKind`、跨机发生在何处**。

### 操作步骤

1. **准备模型库**：选一个支持 disagg 的模型（其 `mlc-chat-config.json` 对应的 model lib 编译时带 disagg 支持，使 `ModelMetadata.disaggregation == true`）。本练习若无多卡环境，可纯做源码阅读。
2. **阅读 Python 编排骨架**：打开 `python/mlc_llm/router/router.py:218-307`（`_handle_completion_disagg`），把它的三步：
   - `send_prepare_receive`（309-336，POST `/microserving/prep_recv`），
   - `send_remote_send`（338-355，POST `/microserving/remote_send`），
   - `send_start_generate`（357-390，POST `/microserving/start_generate`），
   
   与 `microserving_entrypoints.py:22-73` 三个端点一一对应，确认每个端点把请求的 `disagg_config.kind` 设成了什么（`prepare_receive` / `remote_send` / `start_generation`）。
3. **阅读 C++ 引擎动作**：
   - D 收到 `prep_recv` → `DisaggPrepareReceive.Step`：预留页（`DisaggPrepareKVRecv`）、回传 `kv_append_metadata` + `prefix_matched_length`（`disagg_prepare_recv.cc:121-177`）。
   - P 收到 `remote_send` → `DisaggRemoteSend.Step`：`AddNewSequence` + `DisaggMarkKVSend`（标记发送）+ `BatchPrefill`（算 KV，NVSHMEM 自动推送）+ `StreamSync`（`disagg_remote_send.cc:109-165`）。
   - D 收到 `start_generate` → 普通 `NewRequestPrefill`（本地 prefill `kv_window_end:` 尾段）+ `BatchDecode`（流式生成）。
4. **画出时序图**，形如：

   ```
   用户 ──completion──▶ Router
   Router ──prep_recv(end=-1)──▶ D ──DisaggPrepareKVRecv──▶ 预留页
   D ──kv_append_metadata+prefix_matched_length──▶ Router
   Router ──remote_send(begin,end,kv_addr_info,recv_rank)──▶ P
   P ──DisaggMarkKVSend(dst=D rank) + BatchPrefill──▶ [NVSHMEM 推 KV 到 D]
   P ──ack──▶ Router
   Router ──start_generate(begin=end)──▶ D ──NewRequestPrefill(尾段)+BatchDecode──▶ 流式 token
   ```

### 需要观察的现象

- 三步之间靠 **HTTP 请求体**传递 `kv_append_metadata` 与 `recv_rank`，靠 **NVSHMEM** 传递 KV 张量本身——前者走 CPU/网络控制面，后者走 GPU 互联数据面，二者分离。
- `prefix_matched_length` 若大于 0，说明 D 命中了前缀缓存（u10-l2 的 Radix Tree），P 的 `remote_send` 就只需 `[prefix_matched_length:end]`，少算少传（见 `router.py:268, 273-285`）。

### 预期结果

你能回答两个收尾问题：

1. **它如何提升吞吐？** prefill（计算受限）与 decode（访存受限）分到 P/D 两个实例独立调度，P 可堆大 batch 榨算力、D 可堆 KV 页提并发，且长 prefill 不再阻塞 D 的 decode 循环。
2. **它如何提升显存利用？** 每个实例只需为一相分配显存（P 少留 decode 用的连续 KV、D 不必同时承担 prefill 的瞬时峰值）；命中前缀缓存时还能省掉部分 KV 的重算与传输。

### 待本地验证

完整运行需要多 GPU + 编译好的 disagg 模型库。若不具备，可参考 `examples/python/microserving/custom_router.py` 阅读一个最小自定义 Router（其中 `decode_start = len(request.prompt) - 1`，即让 P 算除最后一个 token 外的全部 prompt）。可用 `grep -n "disaggregation" cpp/serve/*.h cpp/serve/*.cc` 确认还有哪些地方受 `estate->disaggregation` 开关影响（例如 `action_commons.cc:309-319` 的 remote_send 完成判定、`351-354` 的抢占直接 abort）。

## 6. 本讲小结

- **分离式推理**把一次推理的 prefill 与 decode 拆给 P、D 两个实例，用 NVSHMEM 把 KV cache 跨机从 P 搬到 D，目标是让算力相与带宽相独立扩缩容、消除长 prefill 对 decode 的阻塞、提升显存利用。
- 三类**特殊请求** `kPrepareReceive` / `kRemoteSend` / `kStartGeneration`（`config.h:59-64`）让同一套引擎代码在 P、D 上走不同路径；参数封装在 `DisaggConfig`（`config.h:75-95`）里，挂载于 `DebugConfig`。
- **`DisaggRemoteSend`**（P 端）复用普通 prefill，差异是 `AddNewSequence` 后调 `DisaggMarkKVSend` 打发送标记、不采样、显式 `StreamSync`；只挑 `kRemoteSend` 请求。
- **`DisaggPrepareReceive`**（D 端）不跑前向，只调 `DisaggPrepareKVRecv` 在分页 KV cache 预留页面，把页地址（`compressed_kv_append_metadata`）base64 编码后经 `usage.extra` 回传；它把请求留在「等 KV」的悬空态，不进 `running_queue`。
- **disco + NVSHMEM** 提供传输底座：引擎启动时按 `disaggregation` 元数据集合初始化 NVSHMEM world（`engine.cc:410-425`），Router 并发拉起所有 server、用 `uid`+`pe_start` 对齐 rank；模型层的 `DisaggMarkKVSend`/`DisaggPrepareKVRecv` 经 `FunctionTable` 转发到 KV cache 的 `vm.builtin.kv_cache_disagg_*` 全局函数。
- **Python Router**（`router.py:218-307`）编排 prep_recv → remote_send → start_generate 三步，控制面（HTTP 传 `kv_append_metadata`/`recv_rank`）与数据面（NVSHMEM 传 KV）分离。

## 7. 下一步学习建议

- **u12-l4 多端部署与工程化**：disagg 目前依赖 CUDA + NVSHMEM，是典型的「服务器集群」能力；下一篇会回到移动端/Web 端的打包与 bench 测试体系，对照理解 disagg 在整个部署矩阵中的位置。
- **继续阅读源码**：
  - `cpp/serve/engine_actions/action_commons.cc:309-319`、`:351-354`——disagg 模式下「remote_send 完成」与「抢占直接 abort」的特殊处理，理解为何 disagg 不复用普通抢占回退逻辑。
  - TVM runtime 里 `vm.builtin.kv_cache_disagg_mark_send` / `kv_cache_disagg_prepare_recv` 的注册与 NVSHMEM put/get 实现（在 `3rdparty/tvm` 中），把「黑盒传输」彻底打开。
  - `examples/python/microserving/custom_router.py` 与 `docs/microserving/tutorial.rst`，动手改一个自定义 `pd_balance_factor` 或 `decode_start` 的路由策略。
- 若关注「跨实例」协作的同类机制，可回顾 **u12-l1 多 GPU 与张量并行**——张量并行是「同一前向」的跨卡分工，disagg 是「不同阶段」的跨实例分工，两者正交可叠加。
