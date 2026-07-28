# 解码服务编排：wire-wait → convert → inject → decode

## 1. 本讲目标

本讲是「PD 分离部署」专家单元的第四篇。前三篇我们分别看清了 PD 架构总览与 `ModelProfile` 抽象（u4-l1）、vLLM prefill 连接器的发送侧（u4-l2）、接收服务与控制平面协议（u4-l3）。到现在为止，KV 状态已经被 RDMA 写进了 decode 节点的接收缓冲，但**缓冲里只是一堆字节，TileRT 的解码引擎还无法直接用它们继续解码**。

`decode_server.py` 要解决的正是这「最后一公里」：把字节还原成张量、喂进引擎、跑完自回归解码，再把生成的 token 流式吐回给调用方。学完本讲，读者应该能够：

1. 讲清 `POST /pd/decode` 的两阶段编排：prepare 阶段（wire-wait → convert → inject）与 decode 阶段，以及贯穿全程的 `wire_wait / convert / inject / decode` 四段计时。
2. 理解 bs=1 互斥锁如何用一把 `threading.Lock` 把整台 decode 节点变成「一次只服务一个请求」的单槽服务器，以及在什么情况下返回 429。
3. 看懂流式（streaming）模式下，为什么必须用 **async generator + `CancelScope(shield=True)`** 才能在客户端断连时安全回收引擎槽位，而同步生成器会让槽位永久泄漏。

## 2. 前置知识

在进入源码前，先用三段话把上下文补齐（细节都在前三篇讲义里，这里只做最小回顾）：

- **PD 分离的三进程拓扑**。Topology A 有三个进程：`pd_router`（OpenAI 入口，不碰 GPU）、vLLM prefill（做 prefill 并产出首 token，同时把 KV 状态经 RDMA 发给 decode 节点）、`decode_server`（本讲主角，接收 KV 后继续解码并流式回吐）。本讲的 `decode_server` 就是拓扑图里的 decode 节点，由 `python -m tilert.pd_vllm.decode_server` 启动（见 [README.md:329-336](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/README.md#L329-L336)）。

- **接收侧的产物**。u4-l3 讲过，`ReceiveServer` 在启动期就按 `max_seq_len` 预留一块连续显存（`server.buffer`），KV 字节被 RDMA 直接写进这块缓冲；当某个请求的所有发送 rank 都报告完成时，`ReceiveServer` 把一个 `ReceivedRequest` 对象丢进 `server.completed` 这个 `queue.Queue`。本讲要做的第一件事，就是从这个队列里把「我们的请求」取出来。

- **引擎是一层薄协议**。`decode_server` 并不直接调用 `generator.inject_cache` 这类具体方法，而是面向 `PDEngine` 协议（`inject / decode / reset`）编程。真正的 GLM-5 / DeepSeek-V3.2 适配器由 `profile.build_engine(...)` 构造；没有 GPU 时可以用 `StubEngine` 跑通整条管道。这是 u4-l1 引入的「框架模型无关」原则在本讲的具体体现。

一个贯穿全讲的直觉：**`decode_server` 本身几乎不做模型相关的事，它是一个「编排器（orchestrator）」**——它只负责按固定顺序调用「接收服务 → profile.convert → engine」三件事，并妥善处理并发、超时与取消。模型差异全部被 `ModelProfile` 与 `PDEngine` 两道缝吸收。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用到哪些部分 |
|---|---|---|
| [tilert/pd_vllm/decode_server.py](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/decode_server.py) | **本讲主角**：FastAPI 编排，定义 `/pd/decode`、`/pd/cancel`、`/health`、`/decode_status` 路由 | 几乎全文 |
| [tilert/pd_vllm/engine_iface.py](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/engine_iface.py) | 引擎缝：`PDEngine` 协议与无 GPU 的 `StubEngine` | `PDEngine` 三方法、`StubEngine` 实现 |
| [tilert/pd_vllm/receive_server.py](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/receive_server.py) | 接收服务（u4-l3 主角） | `server.completed` 队列、`server.release()`、`server.buffer/base_ptr/max_seq_len` |
| [tilert/pd_vllm/profiles/mla_nsa.py](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/profiles/mla_nsa.py) | GLM-5 / DSv3.2 共用的具体 profile 与引擎适配器 | `convert`、`ConvertedRequest`、`MlaNsaEngineAdapter.inject/decode` |
| [tilert/pd_vllm/pd_router.py](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/pd_router.py) | 路由层（理解取消链路的调用方） | `_fire_cancel`、`is_disconnected` 轮询 |

> 说明：本讲只精读前两个文件；后三个文件是「被调用方」，引用是为了讲清 `engine.inject(conv)` 这一句背后到底发生了什么，以及客户端断连时谁负责通知 decode 节点取消。

## 4. 核心概念与源码讲解

### 4.1 服务装配：把 ReceiveServer 与 PDEngine 编排成 HTTP 服务

#### 4.1.1 概念说明

`decode_server` 是一个标准的 FastAPI 应用，但它不是用全局变量装配，而是用一个**工厂函数 `build_app(server, engine)`** 把两个依赖（接收服务 `ReceiveServer` 与引擎 `engine`）注入进来，再返回构造好的 `app`。这样做有两个好处：

- 依赖通过参数传入，便于测试时替换成 `StubEngine` 和假的 `ReceiveServer`。
- `lock`（互斥锁）和 `state`（当前请求状态）作为 `build_app` 的闭包变量，被所有路由共享，天然构成单进程内的「单槽」状态机。

`main()` 则负责**进程级装配**：解析命令行参数 → 选 profile → 选引擎（stub 或 tilert）→ 构造 `ReceiveServer` → 调 `build_app` → 用 uvicorn 启动。理解这条装配链，就理解了 decode 节点上「哪些组件从哪来」。

#### 4.1.2 核心流程

进程启动的装配顺序（来自 `main()`）：

```text
解析参数 (--engine/--model/--max-seq-len/--ctrl-port/--http-port/--transport/--kv-cache-dtype ...)
   │
   ├─ profiles.get_profile(model)            # 选 ModelProfile（u4-l1 的注册表）
   ├─ profile.configure(kv_cache_dtype)      # MLA 族需要 cache dtype 来定 layout_version
   │
   ├─ 选引擎:
   │     --engine stub   →  StubEngine()                      # 无 GPU 联调
   │     --engine tilert →  profile.build_engine(weights,...) # 真实适配器(MlaNsaEngineAdapter)
   │
   ├─ ReceiveServer(profile, max_seq_len, ctrl_port, transport)  # 预留缓冲 + RDMA 注册 + 控制平面监听
   │
   ├─ app = build_app(server, engine)        # 本讲核心：HTTP 编排
   └─ uvicorn.run(sockets=[AF_INET6 双栈 socket])              # 对外提供 :http_port
```

请求侧，客户端（一般是 `pd_router`）发来一个符合 `DecodeBody` 的 JSON：

| 字段 | 类型 | 含义 |
|---|---|---|
| `rid` | str | 请求 id，必须和接收侧 `ReceivedRequest.rid` 一致 |
| `first_token_id` | int | prefill 产出的首 token（由 router 从 vLLM logprobs 抽出） |
| `max_tokens` | int | 期望生成的最大 token 数，默认 256 |
| `sampling` | dict \| None | 采样参数（temperature/top_p/top_k） |
| `timeout_s` | float | 等待 KV 传输完成的超时，默认 120s |
| `stream` | bool | 是否流式（ndjson）返回 |

#### 4.1.3 源码精读

请求模型定义在 [decode_server.py:36-42](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/decode_server.py#L36-L42)，就是上面这张表的直译。

工厂函数 `build_app` 的开头建立了本讲两个最关键的闭包变量：

```python
# decode_server.py:45-48
def build_app(server: ReceiveServer, engine) -> FastAPI:
    app = FastAPI()
    lock = threading.Lock()                       # bs=1 互斥：唯一的并发闸门
    state: dict[str, Any] = {"current_rid": None} # 当前正在服务的请求 + cancel_event
```

这一把 `lock` 是整台节点「一次只服务一个请求」的物理实现（详见 4.3）。`state` 字典在运行中还会被写入 `cancel_event` 键，供 `/pd/cancel` 路由读取。

引擎选择发生在 `main()` 里。注意 stub 与 tilert 两条路径的根本差异：

```python
# decode_server.py:308-324（节选）
if args.engine == "stub":
    engine: Any = StubEngine()                 # 不需要 GPU、不需要权重
else:
    engine = profile.build_engine(             # 构造 MlaNsaEngineAdapter，内部 from_pretrained 加载 8 卡权重
        model_weights_dir=args.model_weights_dir,
        max_seq_len=args.max_seq_len,
        with_mtp=args.with_mtp, ar_steps=8,
    )
```

> 一个容易被忽略的点：即使 `--engine stub`，`ReceiveServer` 仍会在 `cuda:0` 上分配接收缓冲（见 u4-l3 的 `torch.zeros(total, ..., device="cuda:0")`）。因此 stub 模式只省掉了「解码引擎」对 GPU 的依赖，**并不能完全脱离 GPU 运行**。这一点在做本地联调实践时要注意。

`PDEngine` 协议定义了 `inject / decode / reset` 三个方法，是 `decode_server` 唯一依赖的引擎契约：

```python
# engine_iface.py:12-32
class PDEngine(Protocol):
    def inject(self, req: Any) -> None: ...      # 把外部 prefill 的状态恢复成「已 prefill seq_len 个 token」
    def decode(self, first_token_id, max_tokens, sampling,
               on_token=None, cancel_event=None) -> list[int]: ...  # AR/MTP 解码，返回 token id 列表
    def reset(self) -> None: ...                  # 释放单次请求的状态
```

`StubEngine` 就是这三方法的最小回显实现（[engine_iface.py:35-55](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/engine_iface.py#L35-L55)）：`inject` 只记下请求，`decode` 返回固定 token 序列 `(first_token_id, 11, 22, 33)` 截到 `max_tokens`，`reset` 清空。它的价值在于：**不需要真实模型也能把 receive → convert → inject → decode 整条管道跑通**，用来验证编排逻辑。

#### 4.1.4 代码实践

**实践目标**：确认 stub 模式下的装配链路与请求模型字段。

**操作步骤**（源码阅读型，无需运行）：

1. 打开 [decode_server.py:271-346](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/decode_server.py#L271-L346) 的 `main()`，按顺序列出它构造的 4 个核心对象（profile / engine / server / app）。
2. 对照 [engine_iface.py:46-52](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/engine_iface.py#L46-L52) 的 `StubEngine.decode`，手算：当 `first_token_id=7, max_tokens=2` 时返回值是什么？

**需要观察的现象 / 预期结果**：

- 第 1 步应得到 `profiles.get_profile` → `StubEngine()` → `ReceiveServer(...)` → `build_app(server, engine)` 的顺序。
- 第 2 步：`([7] + [11,22,33])[:2]` = `[7, 11]`。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `build_app` 要做成工厂函数，而不是在模块顶层直接 `app = FastAPI()`？

**参考答案**：因为 `app` 需要 `server` 和 `engine` 两个运行期依赖，且 `lock` / `state` 必须作为闭包被所有路由共享。工厂函数让这两个依赖可注入（测试时可换 `StubEngine` 与假 `ReceiveServer`），同时把单槽状态封装在闭包里，避免全局可变状态。

**练习 2**：`--engine stub` 模式下，decode 节点是否完全不依赖 GPU？为什么？

**参考答案**：不是。stub 只让**解码引擎**脱离 GPU，但 `ReceiveServer` 构造时会在 `cuda:0` 上 `torch.zeros` 分配接收缓冲并注册给 RDMA 引擎（见 u4-l3），所以仍需一张 GPU。

---

### 4.2 两阶段 decode 编排：prepare（wire-wait/convert/inject）+ decode

#### 4.2.1 概念说明

`POST /pd/decode` 是本讲的灵魂。它把一次请求的处理严格分成两阶段：

- **prepare 阶段（与 stream 无关，两种模式共用）**：等 KV 传输完成 → 把字节还原成原生张量 → 注入引擎。这一阶段把 decode 节点「调整成仿佛它自己刚刚 prefill 完了 `seq_len` 个 token」的状态。
- **decode 阶段**：从 `first_token_id` 开始做自回归/MTP 解码，产出 token。

两阶段用四个时间戳切成四段计时 `wire_wait / convert / inject / decode`，写进响应的 `timing_ms`。这四段计时是排查 PD 延迟（尤其首 token 之后到完成）的主诊断手段——比如 `wire_wait` 过大说明 RDMA 或调度慢，`convert` 过大说明反量化重，`decode` 过大就是模型本身。

#### 4.2.2 核心流程

```text
pd_decode(body):
  lock.acquire(blocking=False)        # 4.3 讲：抢不到直接 429
  t0 = now
  ── phase 1: prepare ────────────────────────────────────────────
  while 未超时:
      cand = server.completed.get(timeout=...)   # 阻塞等接收侧投递
      if cand.rid == body.rid: req = cand; break # 命中本请求
      else: server.release(); 继续排空陈旧项      # 丢弃无人认领的旧请求
  if req is None: _cleanup(); return 504 (kv_transfer_timeout)
  t_recv = now

  conv = profile.convert(buffer, base_ptr, max_seq_len, req, num_ranks)  # 字节 → 原生 bf16 张量
  t_conv = now

  engine.inject(conv)                              # 写 KI/KV/PE 缓存 + set_cur_pos
  t_inj = now
  ── phase 2: decode ─────────────────────────────────────────────
  非 stream: tokens = engine.decode(...); 返回 {token_ids, timing_ms}
  stream:    起线程跑 engine.decode(on_token=q.put); ndjson 流式回吐
```

四段计时的算法定义为：

\[
\begin{aligned}
\text{wire\_wait} &= 1000 \times (t_{\text{recv}} - t_0) \\
\text{convert}    &= 1000 \times (t_{\text{conv}} - t_{\text{recv}}) \\
\text{inject}     &= 1000 \times (t_{\text{inj}}  - t_{\text{conv}}) \\
\text{decode}     &= 1000 \times (t_{\text{now after decode}} - t_{\text{inj}})
\end{aligned}
\]

其中 `convert` 阶段做的事最值得展开：`profile.convert` 把接收缓冲里的**原始字节**（可能是 fp8 编码）反量化还原成每层的 `(ki, kv, pe)` 原生 bf16 张量。注意 u4-l2 讲过，prefill 侧的 `extract` **只做字节搬运、不做反量化**——反量化被刻意推迟到 decode 侧的 `convert` 来做，这样能减少跨节点传输的数据量（fp8 比 bf16 小一半）。

`inject` 阶段则把这些张量写进引擎的 KV 缓存。具体到 `MlaNsaEngineAdapter`（GLM-5/DSv3.2 共用），就两行：

```python
# profiles/mla_nsa.py:377-381
def inject(self, req) -> None:
    self.gen.inject_cache(req.layers, start_pos=0)   # 逐层写 KI/KV/PE 进 caches
    self.gen.set_cur_pos(req.seq_len - 1)            # 同步 RoPE 位置（关键！）
```

为什么注入缓存后必须 `set_cur_pos`？因为 RoPE（旋转位置编码）的位置由 `cur_pos` 决定；外部 prefill 已经填了 `seq_len` 个位置，引擎必须把游标拨到 `seq_len - 1`，下一个 token 才能接在正确位置上继续旋转。这一点在 u4-l6 会详细展开。

#### 4.2.3 源码精读

prepare 阶段的核心是 [decode_server.py:105-138](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/decode_server.py#L105-L138)。先看 wire-wait 的排空逻辑：

```python
# decode_server.py:109-122（节选）
while time.time() < deadline:
    try:
        cand = server.completed.get(timeout=max(0.1, deadline - time.time()))
    except queue_mod.Empty:
        break
    if cand.rid == body.rid:
        req = cand
        break
    logger.warning("dropping unmatched request %s (waiting for %s)", cand.rid, body.rid)
    server.release()   # 丢弃陈旧请求时务必释放接收槽，否则槽位泄漏
```

这段代码处理一个现实问题：`server.completed` 队列里可能堆积了**别的请求甚至无人认领的旧请求**（比如某个 prefill 发了 KV 但调用方从没来 `/pd/decode`）。代码一边按 `rid` 匹配，一边把不匹配的项 `release` 掉，防止接收槽被占死。超时则走 [decode_server.py:123-127](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/decode_server.py#L123-L127) 返回 `504 kv_transfer_timeout`。

三段连续赋值就是四段计时里的前三段，每一行后埋一个时间戳：

```python
# decode_server.py:128-134
t_recv = time.time()
conv = server.profile.convert(
    server.buffer, server.base_ptr, server.max_seq_len, req, server.profile.num_ranks
)
t_conv = time.time()
engine.inject(conv)
t_inj = time.time()
```

`convert` 的真实实现（GLM-5/DSv3.2）在 [profiles/mla_nsa.py:149-184](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/profiles/mla_nsa.py#L149-L184)：它按层循环，把缓冲里 `kv_raw`（fp8+scale）反量化成 bf16 `[seq,512]`，读出 `pe` 与稀疏索引 `ki`，组成 `ConvertedRequest(layers=[(ki,kv,pe), ...])`。

四段计时字典在 prepare 结束后装配，decode 段在 decode 完成后补上：

```python
# decode_server.py:140-144 + 158-162
pre_timing = {
    "wire_wait": round(1000 * (t_recv - t0), 1),
    "convert":   round(1000 * (t_conv - t_recv), 1),
    "inject":    round(1000 * (t_inj - t_conv), 1),
}
# …decode 之后…
timing = {**pre_timing,
          "decode": round(1000 * (time.time() - t_inj), 1),
          **getattr(engine, "last_stats", {})}   # 合入 finish_reason 等
```

注意末尾的 `**getattr(engine, "last_stats", {})`：引擎在 `decode` 内部会设置 `last_stats = {"finish_reason": "stop"|"length"|"cancelled"}`，这里把它摊进 `timing_ms`，于是响应里既有四段计时，也有终止原因。

#### 4.2.4 代码实践

**实践目标**：追踪一次请求的四段计时来源，理解每段对应哪一步真实工作。

**操作步骤**（源码阅读型，必做）：

1. 在 [decode_server.py:104-134](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/decode_server.py#L104-L134) 标出 `t0 / t_recv / t_conv / t_inj` 四个时间戳各自落在哪一行。
2. 回答：`wire_wait` 这段计时**包含了哪些子步骤的耗时**？（提示：不只「网络传输」，还包括排空陈旧队列、阻塞在 `queue.get` 上的时间。）
3. 追踪 `engine.inject(conv)`：打开 [profiles/mla_nsa.py:377-381](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/profiles/mla_nsa.py#L377-L381)，确认 `inject` 内部调用了哪两个 generator 方法，以及它们各自的职责。

**需要观察的现象 / 预期结果**：

| 计时段 | 起点→终点 | 真实工作 |
|---|---|---|
| `wire_wait` | `t0 → t_recv` | 排空陈旧 `completed` 项 + 阻塞等待本 rid 的 `ReceivedRequest`（含 RDMA 完成） |
| `convert` | `t_recv → t_conv` | `profile.convert`：fp8 字节反量化为 bf16 张量，逐层组 `(ki,kv,pe)` |
| `inject` | `t_conv → t_inj` | `inject_cache` 逐层写缓存 + `set_cur_pos` 同步 RoPE 游标 |
| `decode` | `t_inj → 解码完成` | AR/MTP 解码循环 |

**本地验证（需 GPU，可选，待本地验证）**：在配好环境的 B200 机器上以 `--engine stub` 启动节点，用 `curl` 向 `/pd/decode` POST 一个 `{rid, first_token_id, max_tokens}`（rid 需先由接收侧投递），观察返回的 `timing_ms` 是否含全部四个键。注意 stub 仍需 GPU 分配接收缓冲，且需要先有匹配 rid 的 `ReceivedRequest`，否则会 504。

#### 4.2.5 小练习与答案

**练习 1**：为什么 prefill 侧（u4-l2）的 `extract` 只搬运字节、不反量化，而把反量化留到 decode 侧的 `convert`？

**参考答案**：因为跨节点 RDMA 传输的是 fp8（每元素 1 字节），反量化成 bf16（2 字节）会翻倍传输量。把反量化推迟到 decode 侧做，能少传一半 KV 字节，缩短 `wire_wait`；代价是 decode 节点多承担一份 `convert` 计算开销。

**练习 2**：如果 `server.completed` 队列里堆积了大量不匹配 rid 的旧请求，本讲代码会怎样处理？会不会卡死？

**参考答案**：不会卡死。wire-wait 循环每取到一个不匹配项就 `server.release()` 释放接收槽并继续，直到取到匹配项或超时。超时则返回 504。每个不匹配项都被显式 release，因此不会泄漏槽位。

---

### 4.3 bs=1 互斥锁、429 与单槽生命周期

#### 4.3.1 概念说明

TileRT 的超低延迟是以 **bs=1（batch size = 1）** 为前提的——它要在一个请求上把单 token 延迟压到毫秒级，多请求并发会破坏这一假设。因此 `decode_server` 在进程内用一把 `threading.Lock` 把自己变成「一次只服务一个请求」的单槽服务器：

- 请求进来先**非阻塞**地尝试拿锁：拿到才继续，拿不到立刻返回 `429 busy`。
- 锁一旦拿到，会**一直持有到请求彻底结束**（prepare + decode 全部完成），中间绝不释放。
- 所有结束路径（正常完成、prepare 失败、wire 超时、流式收尾）都汇聚到一个 `_cleanup()` 函数，由它统一 `engine.reset()`、`server.release()`、`lock.release()`。

`/decode_status` 端点对外暴露「忙/闲」状态，让 router 做 gated dispatch。代码注释明确指出：router 的门控分发理论上应该让 429 永远不可达，429 只是一道兜底安全网。

#### 4.3.2 核心流程

```text
请求进入 pd_decode:
  if not lock.acquire(blocking=False):           # 非阻塞抢锁
      return 429 {error: "busy", current_rid}    # 兜底：理论上 router 不会让这发生
  state["current_rid"] = body.rid
  ── 持锁执行 prepare + decode ──
  任意结束路径 ──► _cleanup():
                       engine.reset()            # 释放引擎单请求状态
                       server.release()          # 释放接收侧单槽
                       state["current_rid"] = None
                       state["cancel_event"] = None
                       lock.release()            # 放锁，下一个请求才能进来
```

关键点：**锁在 prepare 阶段（含 wire_wait）就被持有**。这意味着即便节点正在等 KV 传输（wire_wait 可能耗时几十毫秒到几百毫秒），第二个请求也会被立即 429。这是 bs=1 设计的直接后果——「正在服务等同一个请求」也算「忙」。

`_cleanup()` 是整个文件里最重要的「单一释放点」。它的存在保证：无论请求以何种方式结束，锁、接收槽、引擎状态三样资源都会被一致地归还，不会出现「锁还在手里但请求已返回」的泄漏。

#### 4.3.3 源码精读

互斥入口与 429 在 [decode_server.py:97-104](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/decode_server.py#L97-L104)：

```python
@app.post("/pd/decode")
def pd_decode(body: DecodeBody):
    if not lock.acquire(blocking=False):                       # 非阻塞
        return JSONResponse(
            {"error": "busy", "current_rid": state["current_rid"]}, status_code=429)
    state["current_rid"] = body.rid
    t0 = time.time()
```

状态查询端点用 `lock.locked()` 判断忙闲（[decode_server.py:54-57](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/decode_server.py#L54-L57)）：

```python
@app.get("/decode_status")
def decode_status():
    busy = lock.locked()
    return {"status": "busy" if busy else "idle", "current_rid": state["current_rid"]}
```

`_cleanup()` 是统一收尾点（[decode_server.py:78-86](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/decode_server.py#L78-L86)）。注意三步顺序：先 `engine.reset()`、再 `server.release()`、最后 `lock.release()`——先释放「内部资源」再放锁，保证下个请求拿到锁时引擎与接收槽都已就绪：

```python
def _cleanup():
    try:
        engine.reset()
    except Exception:
        logger.exception("engine reset failed")
    server.release()
    state["current_rid"] = None
    state["cancel_event"] = None
    lock.release()
```

`_cleanup()` 被调用的位置覆盖了所有结束路径：

| 调用位置 | 触发场景 | 返回码 |
|---|---|---|
| [L124](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/decode_server.py#L124) | wire 超时，未等到匹配 rid | 504 |
| [L137](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/decode_server.py#L137) | prepare 阶段抛异常 | 500 |
| [L174](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/decode_server.py#L173-L174) | 非流式 decode 完成/失败（`finally`） | 200 / 500 |
| [L264](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/decode_server.py#L264) | 流式生成器收尾（`finally`，见 4.4） | ndjson 结束帧 |

注意 429 路径**不调用** `_cleanup()`，因为那种情况下锁根本没被拿到，没有资源需要归还——这是正确且必要的区别。

#### 4.3.4 代码实践

**实践目标**：验证「锁贯穿 prepare + decode 全程」这一 bs=1 语义，并确认所有结束路径都会放锁。

**操作步骤**（源码阅读型）：

1. 在 [decode_server.py:97-266](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/decode_server.py#L97-L266) 全文里数一数 `lock.release()` 出现的直接调用点（提示：只在 `_cleanup` 里一次），以及 `_cleanup()` 被调用的全部位置。
2. 设想一个场景：节点正卡在 `wire_wait`（阻塞在 `server.completed.get`），此时第二个请求到达。根据 [decode_server.py:99-102](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/decode_server.py#L99-L102)，第二个请求会得到什么响应？

**需要观察的现象 / 预期结果**：

- `lock.release()` 只在 `_cleanup()` 中出现一次；`_cleanup()` 在 4 处被调用（见上表）。这种「单一释放点」设计正是为了避免遗漏放锁。
- 第二个请求会立即收到 `429 {"error":"busy", "current_rid": <第一个 rid>}`，因为锁在 wire_wait 之前就已持有。

#### 4.3.5 小练习与答案

**练习 1**：为什么用 `lock.acquire(blocking=False)`（非阻塞）而不是阻塞等待？如果改成阻塞等待会有什么后果？

**参考答案**：bs=1 设计下，节点不可能并行处理第二个请求，让第二个请求「排队等锁」没有意义——它要么去别的节点（router 应该分发到空闲节点），要么直接告诉调用方「忙」。非阻塞 + 429 让 router 立刻知道这台节点不可用，从而选择别的节点或回错；阻塞等待反而会积压请求、拖垮延迟保证。

**练习 2**：`_cleanup()` 里三步释放的顺序是 `engine.reset()` → `server.release()` → `lock.release()`。如果改成先 `lock.release()` 再 `engine.reset()`，会出什么问题？

**参考答案**：下一个请求可能在 `engine.reset()` 还没跑完时就拿到锁、开始 prepare，从而读到一个尚未清理干净的引擎状态，产生脏数据或崩溃。必须先把内部资源（引擎、接收槽）都释放干净，最后才放锁，让下一个请求看到的是完全就绪的节点。

---

### 4.4 streaming 的取消安全：async generator 与 CancelScope(shield=True)

#### 4.4.1 概念说明

流式模式下，`/pd/decode` 不是一次性返回全部 token，而是用 **ndjson**（每行一个 JSON）边算边吐：先发若干 `{"t":[token...]}` 增量帧，最后发一个 `{"done":true, ...}` 结束帧。这对长生成很关键——客户端能尽早看到输出。

但流式引入了一个棘手问题：**客户端可能中途断开连接**（关浏览器、网络抖动、用户点停止）。一旦断开，decode 节点必须：

1. 让正在 GPU 上跑的解码循环尽快停下来（否则白烧算力）。
2. **可靠地回收引擎槽位**——调用 `_cleanup()` 放锁、重置引擎、释放接收槽，让节点重新可用。

难点在于：**TCP 层检测不到客户端死亡**。代码注释反复强调「asyncio 写一个已关闭的 socket 不会抛异常」——也就是说，靠「写失败」来发现断连是不可靠的。因此 decode 节点采用**双保险**取消机制：

- **保险一（显式）**：router 在轮询发现客户端断连（`request.is_disconnected()`）后，主动 `POST /pd/cancel {rid}`，decode 节点的 `pd_cancel` 设置 `cancel_event`，解码循环看到后提前退出。
- **保险二（结构性）**：客户端断连会让 starlette **取消（cancel）整个流式响应任务**，而 `StreamingResponse` 的主体是一个 **async generator**；只有 async generator 才能在被取消时让 `finally` 块执行，从而保证 `_cleanup()` 一定跑。同步生成器在被取消时会被「静默遗弃」，`finally` 不执行，**引擎槽位永久泄漏**——代码注释明确说这是被一次「streaming-cancel drill」发现的真实坑。

即便 `finally` 能跑，它内部仍要做「等待解码线程结束 + 清理」这类可能在「已取消的作用域」里被再次打断的操作。所以最内层用 `anyio.CancelScope(shield=True)` 给清理逻辑套一层「防取消护盾」，保证清理一定完成。

#### 4.4.2 核心流程

streaming 的数据流是「生产者线程 + 消费者 async generator」的经典解耦：

```text
                         queue.Queue  q
解码线程 _run ───────────►  int token  ─────►  async generator _gen ───► ndjson 帧
  (engine.decode(            ("done",..)            批量取 token → {"t":[...]}
   on_token=q.put,                                  收到 done → {"done":true, timing_ms}
   cancel_event=cancel))                             finally: cancel.set → shield(join) → _cleanup

取消的两种送达路径：
  (A) router: request.is_disconnected() → POST /pd/cancel → pd_cancel 设置 cancel_event
                                    ↓ 解码线程看到 cancel_event → 提前 return
  (B) 客户端断连 → starlette 取消响应任务 → _gen 收到 CancelledError
                                    ↓ async generator 的 finally 执行
                                      cancel.set()                      # 双保险
                                      with CancelScope(shield=True):    # 防取消护盾
                                          await run_in_threadpool(worker.join, 120)
                                      _cleanup()                        # 放锁+reset+release
```

几个关键设计决策：

- **解码跑在线程，不跑在事件循环**。`engine.decode` 是阻塞的 GPU 调用，放在 daemon 线程 `_run` 里，通过 `queue.Queue` 把 token 喂给 async generator，避免阻塞事件循环。
- **token 先批量再发帧**。generator 内层循环把队列里能取到的 token 攒成一批，一次 `yield {"t":[...]}`，减少帧数。
- **600 秒静默看门狗**。若队列既无 token 也无 done 信号超过 600 秒，认为解码卡死，发 `{"error":"decode stalled"}` 并结束（[decode_server.py:231-233](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/decode_server.py#L231-L233)）。
- **worker.join 必须先于 engine.reset()**。因为 `engine.reset` 会动引擎状态，而解码线程可能正「半路」在 GPU 上跑 `_decode_mtp`，必须先 join 等它退出，否则会读到半成品状态。

#### 4.4.3 源码精读

显式取消端点 `/pd/cancel` 是保险一（[decode_server.py:59-76](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/decode_server.py#L59-L76)）。它只对「当前正在服务的 rid」生效，否则 404：

```python
@app.post("/pd/cancel")
def pd_cancel(body: dict):
    rid = body.get("rid")
    ev = state.get("cancel_event")
    if rid and rid == state["current_rid"] and ev is not None:
        ev.set()                       # 解码循环在 cancel_event.is_set() 处退出
        return {"cancelled": rid}
    return JSONResponse({"error": "no matching in-flight request", ...}, status_code=404)
```

注意：`cancel_event` 是在 prepare 完成后才创建的（[decode_server.py:147-148](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/decode_server.py#L147-L148)），所以取消只针对「解码阶段」——wire_wait/convert/inject 期间无法取消，这是合理的（那几步很快且不可中断）。

streaming 主体在 [decode_server.py:178-266](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/decode_server.py#L178-L266)。生产者线程：

```python
# decode_server.py:180-194（节选）
def _run():
    try:
        tokens = engine.decode(
            first_token_id=body.first_token_id, max_tokens=body.max_tokens,
            sampling=body.sampling, on_token=q.put, cancel_event=cancel)
        q.put(("done", tokens))      # 正常结束：发 done 哨兵
    except Exception as e:
        q.put(("error", str(e)))     # 异常：发 error 哨兵
worker = threading.Thread(target=_run, name="pd-decode", daemon=True)
```

async generator 的 `finally` 是本讲最关键的代码——它就是「防止槽位泄漏」的核心（[decode_server.py:196-264](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/decode_server.py#L196-L264)）。先看代码顶部那段解释「为什么必须是 async generator」的注释：

```python
# decode_server.py:197-201
async def _gen():
    # MUST be an async generator: on client disconnect starlette
    # cancels the response task, and only async generators get the
    # cancellation delivered into their frame so `finally` runs
    # (a sync generator is silently abandoned -> the engine slot
    # leaks forever; found by the streaming-cancel drill).
```

`finally` 块本身（[decode_server.py:255-264](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/decode_server.py#L255-L264)）做了三件事，顺序严格：

```python
finally:
    cancel.set()                                  # ① 先通知解码线程停（双保险：pd_cancel 可能没来）
    with anyio.CancelScope(shield=True):          # ② 护盾：清理不会被「已取消」再打断
        await run_in_threadpool(worker.join, 120) #    阻塞等解码线程退出（最多 120s）
    if worker.is_alive():
        logger.error("decode worker failed to stop for %s", body.rid)
    _cleanup()                                    # ③ 放锁 + engine.reset + server.release
```

`shield=True` 的作用：当客户端断连时，`_gen` 所在的任务已经被 starlette 标记为「取消中」，此时 `finally` 里任何 `await` 都可能立刻被 `CancelledError` 打断。但 `worker.join`（等 GPU 解码线程退出）和 `_cleanup`（释放资源）**绝不能被打断**，否则槽位照样泄漏。`CancelScope(shield=True)` 给这段清理逻辑开了一个「免取消窗口」，保证它能完整跑完。

> 为什么 router 既要轮询 `is_disconnected` 发 `/pd/cancel`，又要依赖 decode 节点自己的 async-generator 取消？因为这是两层独立的安全网：`/pd/cancel` 让解码**尽快停**（少烧 GPU），而 async-generator 取消保证 decode 节点**一定能回收槽位**（即使 router 那条 POST 因网络问题没送达）。两者缺一不可：只有 `/pd/cancel` 的话，若 POST 丢失，节点不知道客户端已走；只有 async-generator 取消的话，发现时机依赖 starlette 的取消传播，可能偏晚。

router 侧的对应实现见 [pd_router.py:286-290](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/pd_router.py#L286-L290) 的 `_fire_cancel` 与 [pd_router.py:324-327](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/pd_router.py#L324-L327) 的 `is_disconnected` 轮询，可对照阅读。

#### 4.4.4 代码实践

**实践目标**：讲清「为什么 streaming 必须用 async generator + CancelScope(shield=True) 才能避免引擎槽位泄漏」。

**操作步骤**（源码阅读型，必做）：

1. 打开 [decode_server.py:196-264](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/decode_server.py#L196-L264)，把 `_gen` 的 `finally` 块拆成三步，标注每步的目的。
2. 做一个反事实推理：**假如把 `_gen` 改成普通同步生成器（`def _gen():` 而非 `async def _gen():`）**，当客户端断连时，starlette 会怎么处理它的 `finally`？根据代码顶部注释，结果是什么？
3. 做第二个反事实推理：**假如去掉 `with anyio.CancelScope(shield=True):`**，直接 `await run_in_threadpool(worker.join, 120)`，在被取消的任务里这个 `await` 会发生什么？对 `_cleanup()` 又意味着什么？

**需要观察的现象 / 预期结果**：

- 三步依次是：①`cancel.set()` 通知解码线程停 → ②护盾内 `worker.join` 等线程退出 → ③`_cleanup()` 放锁+reset+release。
- 反事实 1：同步生成器在任务被取消时会被「静默遗弃」，`finally` 不执行 → `_cleanup()` 不跑 → 锁永远不放 → 引擎槽位永久泄漏，节点永久 busy。
- 反事实 2：没有护盾时，已取消任务里的 `await` 会立刻抛 `CancelledError`，于是 `worker.join` 中断、`_cleanup()` 根本到不了 → 同样泄漏槽位。护盾正是为了堵住这个缺口。

**本地验证（需 GPU，可选，待本地验证）**：在 `--engine stub` 节点上发起一个 `stream=True` 的 `/pd/decode`，生成中途断开连接；观察节点日志是否出现 `cancel requested for <rid>` 与随后的 `REQSTAT`（说明 `_cleanup` 跑了、节点回到 idle）。可用 `curl --no-buffer -N ... ` 加 `Ctrl-C` 模拟中途断开。

#### 4.4.5 小练习与答案

**练习 1**：`cancel_event` 是在 prepare 阶段之前还是之后创建的？这意味着取消对哪个阶段有效？

**参考答案**：在 prepare 之后、decode 之前创建（[decode_server.py:147-148](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/decode_server.py#L147-L148)）。因此 `/pd/cancel` 只对解码阶段有效；wire_wait/convert/inject 期间 `state["cancel_event"]` 还是 `None`，`pd_cancel` 会返回 404。

**练习 2**：为什么 `worker.join` 必须在 `engine.reset()` 之前？（提示：看 `_cleanup` 与解码线程的关系。）

**参考答案**：解码线程可能正卡在 GPU 上执行 `_decode_mtp`（MTP 投机解码），此时引擎内部状态正在被读写。若不先 `join` 等线程退出就直接 `engine.reset()`，reset 会改动引擎状态，解码线程可能读到半成品或触发竞争。`finally` 里 `shield(join)` 在前、`_cleanup()`（含 `engine.reset`）在后，正是为了保证「线程已完全停止」后再动引擎。

**练习 3**：ndjson 流的结束帧里有 `finish_reason`，它的可能取值有哪些？router 又会把它怎么改写？

**参考答案**：来自 `engine.last_stats["finish_reason"]`，取值为 `"stop" | "length" | "cancelled"`（见 `PDEngine.decode` 文档与 `MlaNsaEngineAdapter`）。router 会把 `"cancelled"` 改写成 `"stop"` 再回给客户端（见 [pd_router.py:345-346](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/pd_router.py#L345-L346)），因为「被取消」是内部细节，对 OpenAI 语义而言就是正常停止。

---

## 5. 综合实践

把本讲四个模块串起来，做一次「全链路追踪」。

**任务**：给定一次流式 `/pd/decode` 请求（`rid=R1, first_token_id=42, max_tokens=100, stream=True`），在客户端**生成到一半断开连接**的场景下，画出完整的时间线，要求标注：

1. **锁的生命周期**：从哪一行拿锁、到哪一行放锁、中间是否会被第二个请求抢走（结合 4.3）。
2. **四段计时**：`t0 / t_recv / t_conv / t_inj` 各落在哪一行；客户端断连发生在 decode 阶段时，响应里的 `decode` 计时是否还有意义（提示：streaming 的 `done` 帧可能根本没机会发出去）。
3. **取消的双保险**：分别画出「router 发 `/pd/cancel`」与「starlette 取消 async generator 任务」两条路径，标注它们各自让哪个对象状态发生变化（`cancel_event` / `_gen` 的 `finally`），以及最终汇合到 `_cleanup()` 的位置。
4. **资源回收**：确认断连后，`lock`、`server` 单槽、`engine` 状态三样都被归还——找出代码里保证这一点的「单一释放点」。

**交付物**：

- 一张时序图（文字描述即可），横轴是时间，纵轴是 `router / decode_server(pd_decode) / 解码线程 _run / 引擎 GPU` 四条泳道。
- 一段 150 字的说明，回答：**如果开发者在重构时不小心把 `async def _gen` 改成了 `def _gen`，这台 decode 节点在第一次遭遇客户端断连后会表现成什么症状？** （预期：该节点永久 busy，`/decode_status` 永远返回 `busy`，所有后续请求都被 429——也就是「槽位泄漏」。）

> 这是一个纯源码阅读型综合实践，不需要 GPU。它的价值在于：把「两阶段编排」「bs=1 互斥」「streaming 取消安全」三个看似独立的模块，统一到「资源生命周期的正确回收」这一条主线上——这也是 `decode_server` 作为编排器最核心的职责。

## 6. 本讲小结

- `decode_server` 是一个**编排器**：它面向 `PDEngine` 协议（`inject/decode/reset`）和 `ReceiveServer` 编程，自身不做任何模型相关计算；模型差异由 `ModelProfile` 与 `PDEngine` 两道缝吸收。
- `POST /pd/decode` 分两阶段：**prepare**（wire-wait → convert → inject）与 **decode**，对应 `wire_wait / convert / inject / decode` 四段计时，其中 `convert` 才是 fp8→bf16 反量化发生的地方（prefill 侧只搬字节）。
- **bs=1** 用一把 `threading.Lock` 实现：请求非阻塞抢锁、抢不到 429；锁贯穿 prepare+decode 全程；所有结束路径汇聚到 `_cleanup()` 这单一释放点（`engine.reset → server.release → lock.release`）。
- streaming 用「解码线程 + queue + async generator」解耦，ndjson 增量吐 token；客户端断连靠**双保险**取消：router 主动 `/pd/cancel`（尽快停 GPU）+ starlette 取消 async generator 任务（保证 `finally` 跑完回收槽位）。
- `finally` 内必须用 `anyio.CancelScope(shield=True)` 给 `worker.join` 与 `_cleanup` 套防取消护盾；且 `worker.join` 必须先于 `engine.reset`，避免在解码线程尚在 GPU 上时改动引擎状态。
- `StubEngine` 让整条 receive → convert → inject → decode 管道无需真实模型即可联调，但 `ReceiveServer` 的接收缓冲仍在 `cuda:0`，所以 stub 模式仍需一张 GPU。

## 7. 下一步学习建议

本讲把「接收完成的字节 → 解码出 token」这条管道讲透了，但留下了两个延伸方向：

1. **向引擎内部深入**：本讲把 `engine.inject` 当作黑盒。下一讲 **u4-l6「引擎接口与缓存注入」** 会拆开 `MlaNsaEngineAdapter`，精读 `inject_cache` 如何逐层把 `(ki,kv,pe)` 写进三层张量契约里的 `caches`、`set_cur_pos` 如何与 RoPE 配合、以及 MTP 模式下 `inject_last_hidden_state` 的作用。建议结合 u2-l5（三层张量契约）一起读。

2. **横向补全传输层**：本讲的 `wire_wait` 计时背后是 RDMA 传输，建议接着读 **u4-l5「RDMA 传输层抽象」**，理解 `Transport` 基类与 mooncake/nixl 两种实现为何必须在 hello 握手时两端一致。

3. **端到端复盘**：读完 u4-l6 后，建议回到本讲的四段计时，对照真实运行日志（`REQSTAT` 行）把 `wire_wait / convert / inject / decode` 与 u4-l2（发送侧）、u4-l3（接收侧）、u4-l5（传输层）的责任一一对应，建立 PD 分离的完整心智模型。
