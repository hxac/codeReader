# 流式调度器与流式 vocoder

## 1. 本讲目标

本讲打开「在生成完成之前就把音频吐出去」这一能力背后的调度机制。读完本讲你应该能够：

- 说清 `StreamingSimpleScheduler` 在 `new_request` / `stream_chunk` / `stream_done` 三类消息上的状态机：什么时候建状态、什么时候解码、什么时候收尾。
- 区分「流式调度器骨架」「流式 vocoder 模板（`StreamingVocoderBase`）」「具体 `Code2WavScheduler`」三层各自的职责边界。
- 解释 chunk 累积到阈值再解码（threshold accumulate → decode → emit）的顺序，以及 `code2wav_first_audio` 与最终 flush 分别在什么时机触发。
- 说清为什么必须有 `stream_done`，以及「先于 payload 到达的 stream_done」这条流式边（streaming edge）由谁来兜底。

本讲依赖 u4-l1（SimpleScheduler 的 inbox/outbox 契约）。你可以把本讲理解为：SimpleScheduler 那套「来一个算一个、算完丢 outbox」的循环，在输入变成「一段段流式代码」之后，需要加上什么样的请求级生命周期。

## 2. 前置知识

### 2.1 什么是流式语音合成里的「流」

一次 omni/TTS 生成不是「先把整句代码算完，再一次性合成整段音频」，而是上游的 AR 阶段（talker）一边生成离散 codec 代码，一边把代码一小段一小段地往下游的 vocoder 推。vocoder 收到几段代码就能立刻解出对应的一小段波形，提前把音频发给客户端。这样客户端听到的第一段音频（First Audio）来得越早，体感越实时。

这里的「一小段代码」就是 `StreamItem`；「上游推完了」就是 `stream_done`。

### 2.2 与 u4-l1 SimpleScheduler 的关系

回顾 u4-l1：SimpleScheduler 的主循环是 `inbox.get → compute_fn(data) → outbox.put`，适合「来一个算一个」的非 AR 阶段。但流式 vocoder 的输入不是一条请求，而是「先一个 setup（payload），再若干 chunk，最后一个 done」。SimpleScheduler 表达不了这种「一个请求横跨多条消息」的生命周期。

`StreamingSimpleScheduler` 就是为这种输入设计的：它**保留** SimpleScheduler 对 Stage 承诺的 `inbox / outbox / start / stop / abort` 五件套契约（所以 Stage 不需要为流式阶段单独写一套驱动），**额外**加上「按 request_id 追踪一段跨消息的生命周期」的状态机。非流式请求照旧走 `compute_fn` / `batch_compute_fn`，流式请求则走另一条 `on_stream_chunk` / `on_stream_done` 路径，二者井水不犯河水。

### 2.3 三个关键消息类型

来自 [messages.py:L10-L24](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/scheduling/messages.py#L10-L24)：

- 进入调度器的 `IncomingMessage.type ∈ {new_request, stream_chunk, stream_done}`。
- 离开调度器的 `OutgoingMessage.type ∈ {result, stream, error}`。

可以这样理解这三类进入消息：

| 进入消息 | 语义 | 触发的动作 |
|---|---|---|
| `new_request` | 一个请求的 payload（setup） | 建状态、锁（latch）codec 契约 |
| `stream_chunk` | 上游推来一段 codec 代码 | 累积代码，达到阈值就解码并 emit 音频 |
| `stream_done` | 上游推完了 | flush 剩余代码，发 terminal `result`，清状态 |

## 3. 本讲源码地图

| 文件 | 角色 |
|---|---|
| [sglang_omni/scheduling/streaming_simple_scheduler.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/scheduling/streaming_simple_scheduler.py) | 流式调度器骨架 `StreamingSimpleScheduler`：消息状态机 + 生命周期 + 流式/非流式分流 |
| [sglang_omni/scheduling/streaming_vocoder.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/scheduling/streaming_vocoder.py) | 流式 vocoder 模板 `StreamingVocoderBase`：模板方法骨架，把「累积→解码→emit→收尾」固化进基类 |
| [sglang_omni/models/qwen3_omni/components/code2wav_scheduler.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/components/code2wav_scheduler.py) | Qwen3-Omni 的具体 `Code2WavScheduler`：直接继承骨架，自己管代码缓冲与游标 |
| [sglang_omni/pipeline/stage/stream_queue.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/stage/stream_queue.py) | `StreamItem`：一段流式数据的载体 |
| [sglang_omni/pipeline/stage/runtime.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/stage/runtime.py) | Stage 把控制平面消息翻译成 `IncomingMessage` 投进 scheduler.inbox，含 `can_accept_stream_before_payload` 这条流式边 |
| [tests/unit_test/pipeline/test_streaming_simple_scheduler.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/tests/unit_test/pipeline/test_streaming_simple_scheduler.py) | 骨架的单测，是理解状态机最直接的入口 |

---

## 4. 核心概念与源码讲解

### 4.1 流式调度器（StreamingSimpleScheduler）

#### 4.1.1 概念说明

`StreamingSimpleScheduler` 是一个**带有请求级生命周期**的调度器骨架。它解决的问题是：流式阶段（典型是 vocoder）的输入是「一个 payload + N 个 chunk + 一个 done」这种跨消息序列，而 SimpleScheduler 只认识「一条请求算一次」。

它的设计哲学是「骨架定流程、子类填语义」：

- 骨架负责消息分发、request_id 状态追踪、abort 清理、流式/非流式分流、批处理这些与具体模型无关的杂事。
- 子类只需要覆盖几个 hook（`is_streaming_payload` / `on_streaming_new_request` / `on_stream_chunk` / `on_stream_done` / `clear_stream_state`）来表达「我这个阶段怎么判断流式、怎么处理一段 chunk、怎么收尾」。

关键不变量（承接 u4-l1）：它对 Stage 依然只暴露 `inbox / outbox / start / stop / abort`，所以 Stage 驱动它和驱动 SimpleScheduler 用的是同一套代码。

#### 4.1.2 核心流程

主循环 `start()` 在专用线程里跑，和 SimpleScheduler 一样是「取消息 → 处理」，只是 `_handle_message` 按 `type` 分三条路：

```text
inbox.get
  ├─ new_request  → 分流：流式走 on_streaming_new_request；非流式走 compute/batch
  ├─ stream_chunk → on_stream_chunk（可批量合并）
  └─ stream_done  → on_stream_done（flush + terminal result）
```

请求级状态由几个 `dict`/`set` 按 `request_id` 追踪（见 [streaming_simple_scheduler.py:L71-L78](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/scheduling/streaming_simple_scheduler.py#L71-L78)）：

- `_stream_payloads`：已收到 payload 的流式请求（payload 是「已 setup」的凭证）。
- `_pending_done`：**done 先于 payload 到达**时，临时停在这里，等 payload 来了再补 flush（见 4.1.4 的流式边）。
- `_aborted_request_ids` / `_completed_non_streaming_request_ids`：已中止 / 已完成的非流式请求，用于幂等丢弃迟到消息。

一条流式请求的「正常」时序是 `new_request → stream_chunk×N → stream_done`；但骨架还要能容忍 `stream_done` 早于 `new_request` 的乱序，这正是下一节要讲的流式边。

#### 4.1.3 源码精读

**主循环与三分发**：[streaming_simple_scheduler.py:L124-L173](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/scheduling/streaming_simple_scheduler.py#L124-L173)

`start()` 在专用线程里阻塞取消息（`inbox.get(timeout=0.1)`），对已 abort 的请求直接跳过，其余交给 `_handle_message` 按 type 分流：

```python
if msg.type == "new_request":
    self._handle_new_request_batch(self._collect_new_request_batch(msg), loop)
elif msg.type == "stream_chunk":
    ... self._on_chunk(msg.request_id, msg.data)
elif msg.type == "stream_done":
    self._on_done(msg.request_id)
```

**子类 hook 表面**：[streaming_simple_scheduler.py:L84-L116](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/scheduling/streaming_simple_scheduler.py#L84-L116)

骨架提供五个默认空实现的 hook。其中 `on_stream_chunk` / `on_stream_done` 返回 `list[OutgoingMessage]`——骨架负责把这些消息安全地 put 进 outbox（并在 put 前再查一次 abort），子类只管「算出要发什么」：

```python
def on_stream_chunk(self, request_id, item) -> list[OutgoingMessage]:
    return []
def on_stream_done(self, request_id) -> list[OutgoingMessage]:
    return []
```

**非流式批处理把流式请求挡在批次外**：[streaming_simple_scheduler.py:L280-L287](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/scheduling/streaming_simple_scheduler.py#L280-L287)

`_collect_new_request_batch` 攒非流式请求时，一旦遇到一个 `is_streaming_payload(...) == True` 的请求，就停止攒批、把那条消息退回 `_pending_messages`：

```python
if is_streaming:
    self._pending_messages.append(msg)
    break
```

这保证了流式请求永远不会被塞进非流式的批量 compute 路径——两条路物理隔离。

#### 4.1.4 流式边：`can_accept_stream_before_payload` 与 `_pending_done`

「流式边」指的是这样一个边界情况：上游的 `stream_done`（或 `stream_chunk`）可能比本阶段的 `new_request`（payload）**先到**。比如上游 talker 很快推完了几段代码并发出 done，而本阶段的 setup payload 还在跨阶段传输路上。

Stage 侧用 `can_accept_stream_before_payload` 这个开关来决定要不要容忍这种乱序（[runtime.py:L99](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/stage/runtime.py#L99)）。如果允许，Stage 会在 payload 到达前就开一个流式队列并把 `stream_done` 投进 scheduler.inbox（[runtime.py:L783-L792](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/stage/runtime.py#L783-L792)）。

调度器侧的兜底逻辑是：

1. `_handle_stream_done` 发现 `_stream_payloads` 里还没有这个 request_id（payload 还没到），就把它挂到 `_pending_done`，**先不 flush**（[streaming_simple_scheduler.py:L500-L511](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/scheduling/streaming_simple_scheduler.py#L500-L511)）：

   ```python
   if request_id not in self._stream_payloads:
       ...
       self._pending_done.add(request_id)
       return
   ```

2. 等 payload 到达，`_handle_streaming_new_request` 建好状态后，立刻检查 `_pending_done`：如果 done 已经在等，就当场补一次 `_handle_stream_done`（[streaming_simple_scheduler.py:L462-L471](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/scheduling/streaming_simple_scheduler.py#L462-L471)）：

   ```python
   self._stream_payloads[request_id] = payload
   self.on_streaming_new_request(request_id, payload)
   if request_id in self._pending_done:
       self._pending_done.discard(request_id)
       self._handle_stream_done(request_id)
   ```

这样一个「done 早到」的请求最终也会被正确 flush，顺序乱但不丢事。

#### 4.1.5 代码实践

**实践目标**：在不用 GPU 的前提下，跑通骨架的状态机，观察三类消息如何分别触发 hook。

**操作步骤**：

1. 打开 `tests/unit_test/pipeline/test_streaming_simple_scheduler.py`，看 `_TestStreamingScheduler`（[L20-L73](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/tests/unit_test/pipeline/test_streaming_simple_scheduler.py#L20-L73)）是如何只覆盖五个 hook 就把骨架跑起来的。
2. 在仓库根目录跑这一条单测（GPU-free）：

   ```bash
   pytest tests/unit_test/pipeline/test_streaming_simple_scheduler.py -q
   ```

3. 重点看 `test_streaming_simple_scheduler_done_before_payload_finalizes_later`（[L113-L123](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/tests/unit_test/pipeline/test_streaming_simple_scheduler.py#L113-L123)）：它先调 `_on_done("req")`、再调 `_on_streaming_new_request(...)`，正好复现「done 先于 payload」。

**需要观察的现象 / 预期结果**：

- `_on_done` 后请求进了 `_pending_done`、outbox 里**没有**结果。
- `_on_streaming_new_request` 之后 `_pending_done` 被清空、outbox 里出现一条 `type=="result"`、`data=={"done": "req"}`。
- 这条断言对应骨架那句 `if request_id in self._pending_done: ... self._handle_stream_done(request_id)`。

#### 4.1.6 小练习与答案

**练习 1**：如果把 `can_accept_stream_before_payload` 设为 `False`（默认），上游先发来 `stream_done` 会发生什么？

**答案**：Stage 侧 `_open_pre_payload_stream_if_allowed` 返回 `False`（[runtime.py:L783-L792](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/stage/runtime.py#L783-L792)），Stage 会 `scheduler.abort(request_id)` 并向上游 `_send_failure`，报「stream_done arrived before the request payload, but this stage is not configured to accept pre-payload stream data」。也就是说默认配置下，乱序 done 会被当成错误，而不是挂起等待。

**练习 2**：为什么 `on_stream_chunk` 让子类返回 `list[OutgoingMessage]`、由骨架统一 put 进 outbox，而不是子类自己直接 `outbox.put`？

**答案**：骨架在 put 之前会再查一次 `self._is_aborted(request_id)`（[streaming_simple_scheduler.py:L473-L478](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/scheduling/streaming_simple_scheduler.py#L473-L478)），把「子类算 chunk 的耗时」和「最终是否发出」解耦——即便子类正在处理时请求被 abort，骨架也能保证不再把它的产出投递出去。

---

### 4.2 Code2WavScheduler 与流式 vocoder 模板

#### 4.2.1 概念说明

骨架只定了「怎么分发消息」，但「收到一段 codec 代码后到底怎么变成音频」是模型相关的事。代码里有两层来填这部分语义：

1. **`StreamingVocoderBase`**（模板方法基类）：把「每个请求一份状态、累积→解码→emit→收尾、abort 释放资源、可选的跨请求合并解码」这套几乎所有流式 vocoder 都共用的流程，固化进基类；模型差异收口到几个抽象 hook（`create_stream_state` / `validate_chunk` / `ingest` / `decode_delta` / `final_result_data`）。
2. **`Code2WavScheduler`**（Qwen3-Omni 的具体实现）：**直接继承骨架 `StreamingSimpleScheduler`**，自己管代码缓冲与游标，没有走 `StreamingVocoderBase` 那套模板。这是更轻量的「一条龙」实现。

为什么要同时讲这两个？因为 `Code2WavScheduler` 直观（一个类把所有事做完），而 `StreamingVocoderBase` 是给后续接进来的 vocoder 模型（Higgs / MOSS / Fish Audio 等）准备的、可复用的模板。两者解决的是同一个问题，只是抽象层级不同。

#### 4.2.2 核心流程

一个流式 vocoder 请求的生命周期可以画成：

```text
new_request(payload)
  └─ create state; latch codec contract（采样率/通道/dtype 等只锁一次）
stream_chunk(item) ×N
  ├─ validate codes（形状/dtype）
  ├─ ingest：把 codes 追加进请求的代码缓冲
  └─ should_decode ? decode_delta(is_final=False) → emit 一段音频(stream)
stream_done
  ├─ decode_delta(is_final=True)：flush 剩余缓冲
  ├─ 若全程没 emit 过 → fallback_full_decode（整段兜底解码）
  ├─ emit 末段 stream（若有）
  └─ emit terminal result（metadata-only 或整段音频）→ clear state
```

`StreamingVocoderBase` 还封装了三条「模型无关」的纪律，初学者容易踩坑，值得记住：

- **per-request 状态注册表**：`_stream_states` 按 request_id 存状态，首条 chunk/payload 时经 `create_stream_state` 创建，完成/abort/stop 时经 `clear_stream_state`（内部调 `release_stream_resources`）弹出（见 [streaming_vocoder.py:L254-L273](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/scheduling/streaming_vocoder.py#L254-L273)）。
- **迟到 chunk 永不重建状态**：对已 abort / 已 completed 的 request_id，`_get_or_create_stream_state` 直接返回 `None`，迟到的 chunk 被丢弃而不是重新创建状态、重新抢资源（同上）。
- **chunk 元数据契约**：每个 `StreamItem` 必须带 `metadata`，且 `metadata["stream"] is True`、`metadata["modality"] ∈ {None, "audio_codes"}`、`data` 必须是 `torch.Tensor`，否则当错误处理（见 [streaming_vocoder.py:L298-L331](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/scheduling/streaming_vocoder.py#L298-L331)）。

#### 4.2.3 源码精读

**`StreamingVocoderBase` 的模板方法 `on_stream_done`**：[streaming_vocoder.py:L222-L246](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/scheduling/streaming_vocoder.py#L222-L246)

这是「收尾」的标准动作，建议逐行读：

```python
def on_stream_done(self, request_id, payload):
    state = self._get_or_create_stream_state(request_id)
    waveform = self.decode_delta(request_id, state, is_final=True)   # flush 剩余
    if waveform is None and not self._stream_has_emitted(request_id):
        waveform = self.fallback_full_decode(...)                    # 全程没 emit → 整段兜底
    messages = []
    if waveform is not None:
        self._mark_stream_emitted(request_id)
        messages.append(self._stream_chunk_message(request_id, waveform))  # 末段 stream
    messages.append(OutgoingMessage(..., type="result",
                     data=self.final_result_data(...)))              # terminal result
    self._record_completed_stream_request_id(request_id)
    return messages
```

注意顺序：先 `is_final=True` 解码剩余缓冲 → 若没 emit 过就走 fallback → 再发末段 stream → 最后才发 `result`。`result` 永远在最后，这样客户端能拿 `result` 当「整段结束」的信号。

**`Code2WavScheduler` 的 per-request 状态**：[code2wav_scheduler.py:L73-L78](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/components/code2wav_scheduler.py#L73-L78)

Qwen3-Omni 自己维护四个按 request_id 索引的字典：

```python
self._code_chunks: dict[str, list[torch.Tensor]] = {}   # 累积的代码段
self._emitted: dict[str, int] = {}                      # 已经解码到第几段
self._audio_chunks: dict[str, list[np.ndarray]] = {}    # 已解出的音频段
self._stream_enabled: dict[str, bool] = {}              # latch 自 talker 的 stream 标志
```

其中 `clear_stream_state` 把这四个键一起 pop（[code2wav_scheduler.py:L89-L93](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/components/code2wav_scheduler.py#L89-L93)），对应 `StreamingVocoderBase` 的「完成/abort/stop 都走同一条释放路径」。

**左上下文窗口解码（overlap decode）**：[code2wav_scheduler.py:L235-L260](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/components/code2wav_scheduler.py#L235-L260)

vocoder 逐段解码时不是「只喂新代码」，而是带一段左侧上下文（`left_context_size`），让模型在边界处不产生拼接伪影，再把多解出的那一段裁掉：

```python
context = min(self._left_context_size, start)
window = torch.stack(code_chunks[start - context : end], dim=0)
...
wav = self._model(codes)
trim = context * self._total_upsample      # 裁掉左上下文对应的采样点
if trim:
    wav = wav[..., trim:]
```

裁剪量是一个简单乘积：

\[ \text{trim} = \text{left\_context\_size} \times \text{total\_upsample} \]

#### 4.2.4 代码实践

**实践目标**：读懂 `StreamingVocoderBase` 与 `Code2WavScheduler` 的两套抽象如何对应。

**操作步骤**：

1. 打开 [streaming_vocoder.py:L85-L99](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/scheduling/streaming_vocoder.py#L85-L99)，列出基类要求子类实现的抽象 hook（`@abstractmethod` 装饰的那些）。
2. 对照 [code2wav_scheduler.py:L53-L78](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/components/code2wav_scheduler.py#L53-L78)，看 `Code2WavScheduler` 是不是把这些 hook 都覆盖了——你会发现它**没有**继承 `StreamingVocoderBase`，而是把等价的逻辑直接写在 `on_stream_chunk` / `on_stream_done` / `_decode_incremental` 里。

**需要观察的现象 / 预期结果**（待本地验证：需打开两份源码对照，不需 GPU）：

| `StreamingVocoderBase` 的抽象 hook | `Code2WavScheduler` 里等价的实现 |
|---|---|
| `create_stream_state` | `_ensure_request_state`（建四个 dict 的键） |
| `ingest` | `on_stream_chunk` 里的 `self._code_chunks[...].append(codes)` |
| `decode_delta` | `_decode_incremental`（带左上下文的窗口解码） |
| `final_result_data` | `on_stream_done` 末尾的 `final_data`（metadata-only 或整段音频） |

这说明：模板基类是为了让「新接的 vocoder 模型」少写胶水代码；而 `Code2WavScheduler` 作为项目内第一个实现，是直接手写的。

#### 4.2.5 小练习与答案

**练习 1**：`Code2WavScheduler.is_streaming_payload` 直接 `return True`（[L81-L83](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/components/code2wav_scheduler.py#L81-L83)），而 `StreamingVocoderBase` 却去读 `params["stream"]`（[streaming_vocoder.py:L161-L168](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/scheduling/streaming_vocoder.py#L161-L168)）。这两种写法各自适合什么场景？

**答案**：`Code2WavScheduler` 永远流式（它就是个流式 vocoder，不会有非流式 compute 需求），所以恒为 `True`、把所有请求都推进流式路径、`compute_fn=None`；`StreamingVocoderBase` 把「这个请求要不要流式」的决定权交给请求参数 `params["stream"]`，这样一个调度器既能服务流式请求，也能用 `compute_fn`/`batch_compute_fn` 服务非流式请求（一次性合成整段）。后者是更通用的设计。

**练习 2**：`_get_or_create_stream_state` 为什么对「已 completed」的 request_id 也返回 `None`、而不是再次创建？

**答案**：completed 表示这个请求已经走完 `on_stream_done`、terminal result 已经发出。如果之后又来一条迟到的 chunk，重建状态会重新申请资源（比如 codec 会话槽）、并可能再 emit 一段音频，造成「同一个请求发两遍音频」。返回 `None` 让迟到 chunk 被静默丢弃，保证「一请求一终端结果」。

---

### 4.3 chunk 累积与 flush

#### 4.3.1 概念说明

这是本讲最核心的一节：vocoder 到底在什么时机把代码变成音频吐出去。答案是「累积到阈值再解码」——不是来一段解一段（那样太碎、每段都付一次 vocoder 前向的开销），也不是等全部到齐再解（那就退化成非流式、失去「提前发声」的意义）。

涉及两个关键时机：

- **逐 chunk 解码**：每来一段代码，累加；当「已累积未解码」的段数达到 `stream_chunk_size`，就解一次，emit 一段音频。第一段 emit 出去时会打 `code2wav_first_audio` 事件，它就是衡量「首音频时延（TTFA）」的锚点。
- **最终 flush**：上游发完（`stream_done`）时，通常最后几段代码凑不满一个阈值，所以阈值逻辑不会再触发。这时必须有一个「强制把剩余缓冲全解出来」的动作，否则句尾的音频会丢。

这也直接回答了本讲的核心问题——**为什么需要 `stream_done`**：因为「上游不会再推代码了」这件事，vocoder 自己无从得知，必须靠这条消息来触发最后的 flush 和 terminal result。没有它，最后不足一个阈值的代码段永远等不到被解码。

#### 4.3.2 核心流程

`Code2WavScheduler` 的累积-阈值-解码判据是：

\[ \text{ready}(r) = |\text{code\_chunks}(r)| - \text{emitted}(r) \]

当 \(\text{ready}(r) \ge \text{stream\_chunk\_size}\) 时触发一次 `_decode_and_emit`。每次解码后 `emitted` 推进到当前末尾，于是 `ready` 又回到 0，开始攒下一批。

把整条流式路径串起来：

```text
on_stream_chunk(item):
  skip EOS 代码段
  code_chunks.append(codes)
  ready = len(code_chunks) - emitted
  if ready >= stream_chunk_size:      # 阈值到了
      _decode_and_emit()              # 解出 [emitted, len) 一段，emit stream
  # 否则只累积，不解码

on_stream_done():
  if emitted < len(code_chunks):      # 还有没解的尾巴
      _decode_and_emit()              # 最终 flush
  if 全程没解出任何音频:
      失败（code2wav produced no audio）
  emit terminal result（metadata-only 当 stream_enabled，否则整段音频）
```

`StreamingVocoderBase` 把阈值这一步抽象成了 `should_decode(state, is_final=False)` + `decode_delta`，顺序同样是「ingest → should_decode ? decode_delta → emit」，而 `is_final=True` 的 flush 在 `on_stream_done` 里**直接**调 `decode_delta`（绕过 `should_decode`），见 [streaming_vocoder.py:L404-L417](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/scheduling/streaming_vocoder.py#L404-L417) 的注释。

#### 4.3.3 源码精读

**阈值触发**：[code2wav_scheduler.py:L150-L154](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/components/code2wav_scheduler.py#L150-L154)

```python
self._code_chunks[request_id].append(codes)
ready = len(self._code_chunks[request_id]) - self._emitted[request_id]
if ready >= self._stream_chunk_size:
    return self._decode_and_emit(request_id)
return []
```

**`_decode_and_emit` 与 `code2wav_first_audio`**：[code2wav_scheduler.py:L206-L233](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/components/code2wav_scheduler.py#L206-L233)

```python
audio = self._decode_incremental(request_id, chunks, start, end)
self._emitted[request_id] = end
messages = []
if audio.size > 0:
    is_first = not self._audio_chunks[request_id]       # 是否本请求第一次产出音频
    self._audio_chunks[request_id].append(audio)
    if is_first:
        _emit_event(request_id=request_id, event_name="code2wav_first_audio", ...)
    if self._stream_enabled.get(request_id, True):
        messages.append(OutgoingMessage(..., type="stream", data=..., metadata={"modality":"audio"}))
```

关键点：

- `is_first` 用「`_audio_chunks` 此前是否为空」判断，所以 `code2wav_first_audio` **只在第一次真正解出非空音频时**触发一次，对应「客户端听到的首音频」。
- 只有 `stream_enabled` 时才把这段音频包成 `type="stream"` 发出；否则只累积、不发，等到 `on_stream_done` 一次性发整段（这就是非流式回退）。

**`StreamingVocoderBase` 的等价 `_decode_and_emit`**：[streaming_vocoder.py:L333-L342](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/scheduling/streaming_vocoder.py#L333-L342)

```python
def _decode_and_emit(self, request_id, state):
    if not self.should_decode(state, is_final=False):
        return []
    waveform = self.decode_delta(request_id, state, is_final=False)
    if waveform is None:
        return []
    self._mark_stream_emitted(request_id)
    return [self._stream_chunk_message(request_id, waveform)]
```

注意它把 `should_decode` 与 `decode_delta` 解耦：`should_decode` 是「现在要不要解」的便宜判据，`decode_delta` 是「真正去解」的贵动作。`decode_delta` 返回 `None` 表示「这次解了但没东西可发」，基类据此不发、也不标记 emitted。

**最终 flush 与 metadata-only terminal result**：[code2wav_scheduler.py:L156-L204](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/components/code2wav_scheduler.py#L156-L204)

`on_stream_done` 先 flush 剩余（`if chunks and emitted < len(chunks): _decode_and_emit()`），再拼最终 `result`。注释里点明了「terminal result 是 metadata-only」的设计意图：

```python
# Streaming clients already received per-chunk audio; final result is
# metadata-only to avoid IPC-ing full audio that the HTTP layer drops.
if self._stream_enabled.get(request_id, False):
    final_data = {"modality": "audio", "sample_rate": self._sample_rate}
else:
    final_data = self._build_audio_payload(full_audio)
```

即：流式客户端已经一段段收到音频了，terminal result 只带 `{"modality", "sample_rate"}` 当「结束信号」，避免把整段音频再走一次 relay（IPC）却在 HTTP 层被丢弃——省传输。非流式客户端则需要在 `result` 里拿到整段音频。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：对照 `StreamingVocoderBase` 与 `Code2WavScheduler`，说清 `code2wav_first_audio` 与最终 flush 分别在什么时机触发，以及为什么必须有 `stream_done`。

**操作步骤**：

1. 打开 [streaming_vocoder.py:L333-L342](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/scheduling/streaming_vocoder.py#L333-L342)（`_decode_and_emit`）和 [code2wav_scheduler.py:L206-L233](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/components/code2wav_scheduler.py#L206-L233)（`_decode_and_emit` + `code2wav_first_audio`）。
2. 在 `on_stream_chunk`（[code2wav_scheduler.py:L112-L154](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/components/code2wav_scheduler.py#L112-L154)）里找 `ready >= self._stream_chunk_size` 这一行，确认它就是「逐 chunk 解码」的唯一触发点。
3. 在 `on_stream_done`（[code2wav_scheduler.py:L156-L162](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/components/code2wav_scheduler.py#L156-L162)）里找 `if chunks and emitted < len(chunks): messages.extend(self._decode_and_emit(...))`，确认这是「最终 flush」。
4. 想一个反事实：把 `stream_chunk_size` 设得非常大（比如 100000），但 `stream_done` 仍照常发，会发生什么？

**需要观察的现象 / 预期结果**：

- `code2wav_first_audio` 在 **第一次 `_decode_and_emit` 真正产出非空音频**时触发——也就是说，它要么发生在「攒满 `stream_chunk_size` 段后」的逐 chunk 解码，要么发生在 `stream_done` 的最终 flush（如果阈值一直没达到）。`stream_chunk_size` 越小，首音频来得越早，但 vocoder 前向调用越频繁。
- 最终 flush 只在 `emitted < len(code_chunks)` 时才执行；如果上游恰好凑满阈值、尾巴为空，则 `on_stream_done` 不会再解一次、只发 terminal result。
- 反事实答案：若 `stream_chunk_size` 极大，`on_stream_chunk` 永远只累积不解码，所有音频都要等到 `stream_done` 才一次性 flush——此时「流式」退化成「非流式」，首音频时延 ≈ 整段生成完成时延。这恰好反证了 `stream_done` 的职责边界：它只负责「收尾」，不负责「提前发声」；提前发声靠的是逐 chunk 的阈值解码。`stream_done` 不可省略，否则最后不足一个阈值的尾巴会永远留在 `_code_chunks` 里、既不发音频也不发 terminal result。

> 说明：以上为源码阅读型结论，未在本机用真实模型跑（Code2Wav 需要 GPU 与权重）。若要可运行地验证，可用本讲 4.1.5 的 GPU-free 单测路径，把 `_TestStreamingScheduler` 的 `on_stream_chunk` 改成「累积到阈值才 echo」，观察 outbox 里 stream 出现的节奏。

#### 4.3.5 小练习与答案

**练习 1**：`code2wav_first_audio` 事件用 `is_first = not self._audio_chunks[request_id]` 来判断「第一次」。如果改成「用 chunk_id == 0 判断」会有什么问题？

**答案**：`chunk_id == 0` 只能说明这是上游推来的第一段代码，不代表 vocoder 已经解出了音频——第一段代码很可能因为 `ready < stream_chunk_size` 只被累积、没有解码；甚至第一段可能是 EOS（被 `on_stream_chunk` 直接 skip）。`code2wav_first_audio` 的语义是「客户端真正听到的第一段音频」，必须以「`_decode_and_emit` 产出非空音频」为准，所以用 `_audio_chunks` 是否为空判断才正确。

**练习 2**：`on_stream_done` 里，若整段请求 `stream_enabled=False`（非流式），terminal result 为什么不能也用 metadata-only？

**答案**：非流式客户端从没收到过逐 chunk 的 `stream` 音频，整段音频只可能来自 terminal `result`。如果此时也发 metadata-only，客户端就拿不到任何音频了。所以代码里 `stream_enabled` 为真才 metadata-only，否则用 `_build_audio_payload(full_audio)` 把整段波形塞进 `result`（[code2wav_scheduler.py:L186-L192](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/components/code2wav_scheduler.py#L186-L192)）。

---

## 5. 综合实践

**任务**：在不依赖 GPU 与权重的前提下，用骨架 `StreamingSimpleScheduler` 写一个「假想的阈值式 echo 调度器」，把本讲三个模块串起来——状态机（4.1）、per-request 状态与收尾（4.2）、累积到阈值再 emit 与 `stream_done` flush（4.3）。

**要求**：

1. 子类化 `StreamingSimpleScheduler`，只覆盖五个 hook：`is_streaming_payload`、`on_streaming_new_request`、`on_stream_chunk`、`on_stream_done`、`clear_stream_state`。
2. `is_streaming_payload` 读 `payload.request.params["stream"]`，让同一个调度器既能流式也能非流式（参考 [test_streaming_simple_scheduler.py:L32-L34](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/tests/unit_test/pipeline/test_streaming_simple_scheduler.py#L32-L34)）。
3. per-request 维护一个 `list` 缓冲（仿 `_code_chunks`），设阈值 `THRESHOLD=3`：`on_stream_chunk` 里 `append` 后，当 `len(buf) - emitted >= THRESHOLD` 才 emit 一条 `type="stream"`；否则只累积。
4. `on_stream_done` 里先 flush 剩余（仿 [code2wav_scheduler.py:L161-L162](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/components/code2wav_scheduler.py#L161-L162)），再发一条 `type="result"`。
5. 写一个最小驱动（直接 `put` 三条 `IncomingMessage` 进 `inbox`，再手动调 `_handle_message`，参考测试里 `_drain_results` 取 outbox），喂 5 段 chunk + 1 个 done。

**验收标准（你应当能解释清楚的现象）**：

- 前 3 段 chunk 触发第一次 emit（首段「first audio」），第 4~5 段中第 4 段触发第二次 emit，剩下第 5 段在 `stream_done` 时被 flush。
- 若把 `THRESHOLD` 调到 100，则 5 段 chunk 期间**一次都不 emit**，全部音频在 `stream_done` 一次性 flush——这复现了 4.3.4 的反事实结论。
- 若先调 `_on_done` 再调 `_on_streaming_new_request`，验证「done 先于 payload」也能正确收尾（对应 4.1.4）。

完成后，你就把「消息状态机 / per-request 生命周期 / 阈值累积与 flush」三件事用一个 GPU-free 的小调度器亲手跑通了一遍。

## 6. 本讲小结

- `StreamingSimpleScheduler` 保留 SimpleScheduler 的 `inbox/outbox/start/stop/abort` 契约，额外加上「按 request_id 追踪 `new_request → stream_chunk×N → stream_done` 生命周期」的状态机；非流式请求照旧走 `compute_fn`/`batch_compute_fn`，且会被刻意挡在流式路径之外。
- 「先于 payload 到达的 `stream_done`」由 Stage 的 `can_accept_stream_before_payload` 开关 + 调度器的 `_pending_done` 共同兜底：done 先挂起，payload 到了再补 flush。
- `StreamingVocoderBase` 是模板方法基类，把「状态注册表、ingest→decode→emit→收尾、abort 释放、迟到 chunk 丢弃」固化进基类，模型差异收口到 `create_stream_state`/`ingest`/`decode_delta`/`final_result_data` 等 hook；`Code2WavScheduler` 则是直接继承骨架、自己管缓冲与游标的具体实现。
- 解码时机是「累积到 `stream_chunk_size` 才解」：`ready = len(code_chunks) - emitted`，`ready >= stream_chunk_size` 触发 `_decode_and_emit`；第一段非空音频触发 `code2wav_first_audio`，它是首音频时延的锚点。
- `stream_done` 不可省：它负责 flush 最后不足一个阈值的尾巴、并发出 terminal `result`（流式客户端拿 metadata-only、非流式客户端拿整段音频）。没有它，句尾音频会丢失、terminal result 永不发出。

## 7. 下一步学习建议

- 顺着「上游推代码」这条线往回读：本讲的 chunk 来自 talker 阶段。建议读 u4-l3 讲的 `QwenTalkerModelRunner`（反馈式 AR）里 `post_decode` 如何把码本推进 `code2wav`，理解 `code2wav_first_audio` 之前那一段「talker 生成 → 推流」的耗时构成。
- 读其他 vocoder 模型如何复用 `StreamingVocoderBase`：[sglang_omni/models/higgs_tts/vocoder_scheduler.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/higgs_tts/vocoder_scheduler.py) 与 [sglang_omni/models/moss_tts_local/streaming_vocoder.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/moss_tts_local/streaming_vocoder.py)，对比它们实现的 `decode_delta`/游标数学与 `Code2WavScheduler` 的异同。
- 若关心跨请求合并解码（本讲提到的 `_can_batch_stream_chunks` + `select_step_participants`/`build_step_plan`/`run_step`），可细读 [streaming_vocoder.py:L354-L503](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/scheduling/streaming_vocoder.py#L354-L503) 的 coalesced pump，以及对应单测 `test_stream_chunk_batch_*`。
- 性能视角：结合 u6-l3（请求级 Profiler），用 `code2wav_first_audio` / `stage_first_stream_chunk_sent` 等事件定位一次实时请求的首音频时延瓶颈。
