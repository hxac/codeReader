# 两代引擎桥：LocalEngineBridge 与 SteppedEngineBridge

> 讲义 id：u4-l3 ｜ 层级：advanced ｜ 依赖：u4-l1（serve 装配骨架）、u3-l2（legacy 线类型）、u3-l6（接线与路由）

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清「桥」在架构里的位置：它把 PegaInfer 引擎**伪装成一个 vLLM EngineCore**，让上游 vllm-server 以为自己连上了一个真实的 vLLM 引擎进程。
2. 解释 zmq dealer/push 双 socket 与预绑定 listener fd 的对接方式（`TransportMode::Bootstrapped`）——谁 bind、谁 connect、握手说了什么。
3. 对比两代桥：legacy 桥把「每 token 一条」的共享打标事件流 demux 并折叠成 burst 输出；stepped 桥把每个 `StepOutputs` 消息 1:1 翻译成一个 `EngineCoreOutputs`。
4. 说出 stepped 桥如何把契约的单调 `Instant` 渲染成线上墙钟时间（每桥一个 `UnixAnchor` 锚点）。
5. 解释 `StatsTracker` 为什么要把调度器的累计计数器差分成按批 delta。

## 2. 前置知识

本讲站在 u4-l1/u4-l2（装配骨架）与 u3 单元（引擎契约）的肩膀上。先用三段话补齐尚未接触过的传输层概念。

**zmq 与两种 socket。** ZeroMQ（本项目用纯 Rust 实现 `zeromq`）是消息队列语义的 socket 库：它管连接重试、排队与帧边界，程序员只管 `send`/`recv` 整条消息（`ZmqMessage`，本身是一组帧的向量）。本讲出现两种 socket：

- **DealerSocket**（双工）：桥的「下行」通道，收 HTTP 层发来的请求。它带一个 `peer_identity`，让对端知道「我是几号引擎」。
- **PushSocket**（单向推）：桥的「上行」通道，只发不收，把输出推回 HTTP 层。

`ipc://` 地址指的是本机 Unix domain socket 文件（如 `/tmp/pgi-…-…/input.sock`），不走网络栈。

**msgpack。** vLLM EngineCore 协议用 MessagePack（`encode_msgpack` / `decode_msgpack`）序列化结构体——比 JSON 紧凑、比 bincode 跨语言，因为对端（vllm-server）不是本项目代码。zmq 消息约定为两帧：第 0 帧是请求类型（Add/Abort/Utility），第 1 帧才是 msgpack 负载。

**demux（多路分用）。** u3-l2 讲过 `TokenSink` 的设计：所有请求的事件共用**一条**打标通道（`RequestTag` 标明归属），取代每请求一条通道。代价是消费端必须自己「分路」——这就是 demux：按 tag 把混流拆回每请求的桶。本讲的 legacy 桥就是那个唯一的 demux 消费者。

**单调时钟 vs 墙钟。** `Instant`（单调时钟）只保证「先后可比、差值可测」，不受系统改时间影响，但数值本身没有含义；`SystemTime`（墙钟）是真实的年月日时分秒，但会跳变。step 契约内部全用 `Instant`（调度器测排队/调度时延最稳），vLLM 线上协议要的是墙钟秒数——4.3 节讲两者怎么换算。

再复习两个 u3 的结论，本讲反复用到：legacy 引擎经 `EngineHandle::submit(GenerateRequest)` 提交、从共享通道收 `TokenEvent`；step 引擎经 `SchedulerHandle::submit(Request)` 提交（返回 `RequestControl`）、从步流收 `StepOutputs`（每请求至多一条扁平 `RequestUpdate`）。

## 3. 本讲源码地图

| 文件 | 角色 |
|---|---|
| `../pegainfer-frontend/src/vllm/bridge.rs` | **本讲主战场**：legacy 桥 `LocalEngineBridge`（L74-L359）、demux 折叠 `dispatch_burst`/`reduce_request`（L407-L592）、`StatsTracker`（L694-L732）、共享接线 `BridgeLink`/`connect_link`（L768-L898）、输出泵 `output_loop`（L900-L923）、IPC 命名空间（L1032-L1044） |
| `../pegainfer-frontend/src/vllm/bridge/stepped.rs` | **stepped 桥**：1:1 翻译 `dispatch_step`/`reduce_update`（L222-L267、L547-L653）、每请求状态 `SteppedStream`（L464-L545）、墙钟锚点 `UnixAnchor`（L655-L678） |
| `../pegainfer-frontend/src/vllm/bridge/tests.rs` | legacy demux 的聚焦测试（无 socket、直喂事件），是 4.2 的实践对象 |
| `../pegainfer-frontend/src/vllm/mod.rs` | 装配点：预绑定两个 fd（L275-L282）、两代桥的构造（L322-L332、L350-L360）、`TransportMode::Bootstrapped`（L438-L449） |
| `../pegainfer-frontend/src/engine/wiring.rs` | 桥消费的 handle 面：`take_steps`（L97-L101）、`metrics`（L103-L107）、`exited`（L109-L113） |
| `../pegainfer-frontend/src/engine/sink.rs` | 共享打标通道的类型定义（L16） |
| `../pegainfer-frontend/examples/echo-server.rs` | 无 GPU 可跑的 stepped 桥活体标本：`EchoScheduler` 每步吐一个 token（L121-L158），本讲综合实践的观察对象 |

## 4. 核心概念与源码讲解

### 4.1 桥接点：BridgeLink、zmq 双 socket 与 Bootstrapped transport

#### 4.1.1 概念说明

上游 vllm-server 的世界里，「引擎」是一个通过 zmq 与它通信的独立进程（vLLM 官方部署里的 EngineCore 进程）。PegaInfer 不是 vLLM，但想让 HTTP 层原封不动地工作，于是写了一个**进程内的替身**：引擎桥。它向外说 vLLM EngineCore 协议（zmq + msgpack），向内说 PegaInfer 引擎契约（`EngineHandle` 或 `SchedulerHandle`）。

u4-l1 讲过装配顺序「端口可达 = 引擎就绪」，本讲补上传输层的另一半：**谁先到 socket 文件旁等着**。答案由 `TransportMode::Bootstrapped` 固化——装配骨架**先 bind** 两个 unix listener，把裸 fd 交给 vllm-server 领养；桥稍后（引擎加载完成后）才 **connect**。bind 先于一切，所以桥无论多晚上线都能连上，且 fd 的所有权（谁负责关闭）归 server。

#### 4.1.2 核心流程

```text
装配骨架 (vllm/mod.rs)                     引擎桥 (bridge.rs)
──────────────────────                     ─────────────────────
local_ipc_namespace()  创建 /tmp/pgi-<pid>-<uuid>/
bind input.sock  ──fd──► TransportMode::Bootstrapped ──► vllm-server 领养
bind output.sock ──fd──► (同上)
        ▲
        │（socket 文件已存在，connect 随时可来）
        │
        └────────────── 引擎 resolve 后，每分区铸一个桥 ──► connect_link()
                                                        1. DealerSocket.connect(input)
                                                        2. 发 EngineCoreReadyResponse 握手
                                                        3. PushSocket.connect(output)
                                                        4. 起 output_loop 子任务
                                                        ═══ BridgeLink 交回 ═══
```

握手消息 `EngineCoreReadyResponse` 是桥的「自我介绍」：最大长度、KV 块数与块大小（折算出并发上限）、DP 规格、`data_parallel_rank = engine_index` 等。其中并发上限按 vLLM 单组并发口径折算：\( \text{blocks\_per\_req} = \lceil \text{max\_model\_len} / \text{block\_size} \rceil \)，\( \text{concurrency} = \text{total\_blocks} / \text{blocks\_per\_req} \)。

`BridgeLink` 是连接建立后的交接包——**两代桥共用**：input 的 `DealerSocket`、输出侧的 `mpsc` 通道发送端、以及装载子任务的 `JoinSet`。注释原话说得很清楚：引擎数据面以北的一切，两代桥完全相同。

#### 4.1.3 源码精读

[pegainfer-frontend/src/vllm/mod.rs:L275-L282](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/mod.rs#L275-L282) — 预绑定的诞生地：`local_ipc_namespace()` 建出 `/tmp/pgi-<pid>-<uuid>/` 目录，随后 `UnixListener::bind` 两个 socket 文件并 `into_raw_fd()` 交出描述符。注释点明角色分工：vllm-server 领养已 bind 的 listener（对应上游部署中 supervisor 的职责），在骨架侧 bind 意味着桥可以任何时候来连，而描述符由 server 持有并负责关闭。

[pegainfer-frontend/src/vllm/mod.rs:L438-L449](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/mod.rs#L438-L449) — 两个 fd 的去处：`TransportMode::Bootstrapped { input_listener_fd, output_listener_fd, engine_start_index, engine_count, data_parallel_size, ready_timeout }`。`engine_count` 就是 u2-l5 讲过的「引擎加载前就必须知道的分区数」——HTTP 层据此预先注册引擎身份，桥上线只是逐个兑现。

[pegainfer-frontend/src/vllm/bridge.rs:L768-L776](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/bridge.rs#L768-L776) — `BridgeLink` 结构：握手已完成、输出泵在跑、stats 发布器已挂（若传入 watch）。文档注释写明它「被 legacy 每 token 桥与 stepped 桥共享——引擎数据面以北的一切在两者间完全一致」。

[pegainfer-frontend/src/vllm/bridge.rs:L793-L794](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/bridge.rs#L793-L794) — DealerSocket 的 `peer_identity` 设为 `EngineId::from_engine_index(engine_index)`：多分区时每个桥用不同身份注册，HTTP 层才能区分「0 号引擎」与「1 号引擎」的输出。

[pegainfer-frontend/src/vllm/bridge.rs:L810-L837](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/bridge.rs#L810-L837) — 握手负载 `EngineCoreReadyResponse` 的拼装：`num_gpu_blocks`/`block_size`/`kv_cache_size_tokens`/`kv_cache_max_concurrency` 全部从 `kv_capacity` 折算（`None` 时给保守默认值），`effective_data_parallel_size` 与 `data_parallel_rank` 告知 DP 拓扑，`supports_lora: false` 是这个通用桥的能力声明（LoRA 走 u4-l5 的另一条路）。

[pegainfer-frontend/src/vllm/bridge.rs:L843-L865](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/bridge.rs#L843-L865) — 握手三步：`input.connect` → `input.send(ready)` → `output.connect`。注意 `tokio::select!` 包了 `shutdown.cancelled()`：zeromq 对被拒绝的 connect 会重试 30 秒，如果 server 自己启动失败关掉了 listener，不抢跑的话桥要干等半分钟才报错。

[pegainfer-frontend/src/vllm/bridge.rs:L867-L891](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/bridge.rs#L867-L891) — 输出侧统一走**一条进程内 mpsc 通道**再由 `output_loop` 泵出：请求输出、stats 批次、utility 应答都进 `output_tx`，保证 zmq 上发送串行化。末尾的注释预告了 4.4 的关键分叉：legacy 引擎在此挂 watch 驱动的 stats 发布任务；stepped 桥传 `None`——它的 driver 空转自旋，每次自旋都会变成一条消息，所以它改为发批时现读。

[pegainfer-frontend/src/vllm/bridge.rs:L900-L923](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/bridge.rs#L900-L923) — `output_loop`：把队列里所有 `EngineCoreOutputs` 逐条 `encode_msgpack` 后从 PushSocket 发出；通道关闭且 `engine_dead` 已取消时，补发 `ENGINE_CORE_DEAD_SENTINEL`——让客户端把它还跟踪的每个请求（包括桥从未读到过的）判失败。

[pegainfer-frontend/src/vllm/bridge.rs:L1032-L1044](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/bridge.rs#L1032-L1044) — `local_ipc_namespace`：目录名带 pid 与 uuid 前 8 位（`PEGAINFER_IPC_DIR` 可改基目录），`ipc_endpoint` 拼出 `ipc://…` 地址。收尾的删除在 [mod.rs:L518](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/mod.rs#L518)（`remove_dir_all`，无论成败）。

#### 4.1.4 代码实践

1. **实践目标**：亲眼看到 Bootstrapped transport 的两个 socket 文件与「进程结束必删」的纪律。
2. **操作步骤**：
   - `mkdir -p /tmp/pgi-lab`，然后运行 `PEGAINFER_IPC_DIR=/tmp/pgi-lab cargo run -p pegainfer-frontend --example echo-server -- --model-path ../pegainfer-frontend/examples/echo-model`（端口默认，Ctrl-C 停止）。
   - 服务运行期间另开终端：`ls -l /tmp/pgi-lab/` 观察形如 `pgi-<pid>-<uuid>` 的目录，里面有 `input.sock` 与 `output.sock` 两个 socket 文件。
   - Ctrl-C 停止 echo-server 后再次 `ls -l /tmp/pgi-lab/`。
3. **需要观察的现象**：运行期两个 socket 文件存在且类型为 `s`（unix socket）；停止后 `pgi-*` 目录消失。
4. **预期结果**：与 [mod.rs:L518](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/mod.rs#L518) 的 `remove_dir_all` 对应。注意 echo 是 stepped 引擎（见 4.3），所以你观察到的桥是 `SteppedEngineBridge`。若本机无 Rust 工具链，此步标注「待本地验证」，改做下面的阅读练习：对照 L275-L282 与 L438-L449，用自己的话写出 `input_listener_fd` 从诞生到被领养的完整旅程（一句话即可：bind → into_raw_fd → Bootstrapped → vllm-server accept）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 listener 由装配骨架 bind，而不是让桥自己 bind、server 来 connect？
**答案**：装配骨架把「秒级的 HTTP 前端装配」与「分钟级的引擎加载」并发（u4-l1）。若由桥 bind，server 必须等引擎加载完才能拿到 socket；由骨架先 bind 并把 fd 交给 server 领养，server 可以随时 accept，桥无论多晚 connect 都有 socket 文件等着，fd 的生命周期也归 server 管理。

**练习 2**：`BridgeLink` 为什么值得单独存在、被两代桥共享？
**答案**：zmq 握手、身份注册、输出串行化泵、子任务管理这些「引擎数据面以北」的活与引擎契约的新旧无关。抽成 `connect_link` + `BridgeLink` 后，两代桥各自只写数据面翻译，传输层的修复一处生效。

**练习 3**：握手为什么要 `select!` 上 shutdown？
**答案**：zeromq 对被拒绝的 connect 自动重试 30 秒；若 server 启动失败关闭了 listener，桥不抢跑就会白白等 30 秒才报错。`biased` 分支让 shutdown 立即胜出，快速传播取消。

### 4.2 LocalEngineBridge：共享打标事件流的 demux 与 burst 折叠

#### 4.2.1 概念说明

`LocalEngineBridge` 服务仍返回 `LaunchedEngine::Handle` 的三条模型线（deepseek-v2-lite、glm52、kimi-k2，见 u3-l1 普查）。它的难题是：legacy 契约只有一条共享打标通道（u3-l2），所有请求的 `TokenEvent` 混在一起逐条到达，而 vLLM 线上协议要的是「一个批次一条 `EngineCoreOutputs` 消息，每请求一条 `EngineCoreOutput`」。桥必须**分路（demux）+ 折叠（fold）**：把一次唤醒时已就绪的事件按请求分桶、每请求折成至多一条输出、整批装进一条 zmq 消息——注释里称之为「把以前每步 N 条 per-request ZMQ 消息坍缩成一条」。

每请求状态存在 `streams: HashMap<RequestTag, RequestStreamState>`：保存 abort 原子标志、首 token 元数据（Queued/Scheduled 线上事件与 prefill 统计）、P/D 移交参数、stop 哨兵 token 等。

#### 4.2.2 核心流程

主循环（`biased` select，四个分支）：

```text
loop {
  shutdown.cancelled()        → 优雅退出
  child_tasks.join_next()     → 子任务死亡即致命（输出泵/stats 泵都不能死）
  event_rx.recv()  ─────────► dispatch_burst：demux 本轮就绪事件
  input.recv()     ─────────► handle_message：Add / Abort / Utility
}
```

`dispatch_burst` 的折叠算法：

```text
取首条事件，再 try_recv 排空「已就绪」的全部事件   ← 只排空就绪的，不等待未来
按 RequestTag 分桶（保留首见顺序与桶内到达顺序）
对每个桶：streams 里无此 tag → 丢弃（已 abort/已完成的迟到事件）
          有 → reduce_request(events) → (Option<输出>, terminated)
terminated → streams.remove + 记入 finished_requests
若 outputs 非空 → 打包成一条 RequestBatchOutputs 发出
```

`reduce_request` 的事件折叠规则：

| 事件 | 处理 |
|---|---|
| `Scheduled` | 生成 Queued/Scheduled 两条线上事件与 prefill 统计，**暂存** state（`computed = prompt − cached`） |
| `Token` | 追加 token；logprob 有值则填该位置 |
| `KvTransfer` | 暂存 state，等下一个实际输出捎给 router |
| `Finished` | 设 finish_reason；若是 `Stop` 且有哨兵，补一个可被前端删掉的终止 token（见下） |
| `Error` / `Rejected` | 折算成线上 `Error` 完结 + 文本 stop_reason |

**EOS 哨兵**是一个协议缝补：PegaInfer 调度器在边界上抑制 EOS 不外发，而 vLLM 文本解码器会**无条件删掉** Stop 输出的最后一个 token。不补哨兵的话，一次投机解码提交 `[可见 token, EOS]` 会在前端丢掉那个可见 token。

#### 4.2.3 源码精读

[pegainfer-frontend/src/vllm/bridge.rs:L74-L82](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/bridge.rs#L74-L82) — 桥的字段：`handle: EngineHandle`（legacy 接线）、`engine_index`（本桥服务的 DP 分区号）、`metrics_watch`（stats 发布的 watch 通道）。每个分区一个桥实例，由 [mod.rs:L322-L332](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/mod.rs#L322-L332) 构造并 spawn。

[pegainfer-frontend/src/vllm/bridge.rs:L106-L111](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/bridge.rs#L106-L111) — demux 的地基：一条 `mpsc::unbounded_channel` 承载**所有**请求的 `(RequestTag, TokenEvent)`，本循环是唯一消费者；每请求状态在 `streams`，键即 tag。

[pegainfer-frontend/src/vllm/bridge.rs:L139-L164](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/bridge.rs#L139-L164) — 两个数据面分支：事件通道醒来进 `dispatch_burst`（一次唤醒处理整个就绪 burst，这就是「每步一次唤醒」而非「每 token 一次唤醒」的由来）；zmq 输入醒来进 `handle_message`。

[pegainfer-frontend/src/vllm/bridge.rs:L195-L235](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/bridge.rs#L195-L235) — 请求消息的分派：第 0 帧匹配 `EngineCoreRequestType` 的 Add/Abort/Utility 三种。Abort 分支的顺序很讲究：先 `streams.remove` 再以 `Release` 语义写 abort 原子量——调度器下一次 emit 读到（`Acquire`）失败原因即退休请求；已在途的迟到事件因查不到 stream 项而被丢弃。abort 原因按 `has_emitted_tokens` 区分：出过 token 的是「流式取消」，没出过的是「断连」。

[pegainfer-frontend/src/vllm/bridge.rs:L319-L352](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/bridge.rs#L319-L352) — `start_request` 的后半：铸造 `RequestTag` 与 abort 原子量、构造 `TokenSink`（把共享通道发送端交给引擎）、按 tracing 开关造真/ noop 根 span（关链路时零分配），最后 `handle.submit(GenerateRequest { data_parallel_rank: Some(engine_index), … })`——桥**恒绑本分区 rank**（u3-l6 讲过路由语义），token 事件出口即 `token_tx`。

[pegainfer-frontend/src/vllm/bridge.rs:L407-L434](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/bridge.rs#L407-L434) — `dispatch_burst` 的分桶：`bucket` 闭包维护首见顺序的 `order` 与按 tag 的 `buckets`；`try_recv` 循环只取**已就绪**的事件（阻塞的 `recv` 只发生一次——select 分支给的那条）。文档注释点明设计意图：确定性顺序、桶内保序、N 条 per-request 消息坍缩为一条。

[pegainfer-frontend/src/vllm/bridge.rs:L436-L457](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/bridge.rs#L436-L457) — 每桶折叠与关户：无 stream 项的桶直接跳过；`reduce_request` 返回 `(输出, terminated)`；输出了 token 才置 `has_emitted_tokens`（abort 语义的分界）；terminated 时移除状态并汇入 `finished_requests`（`BTreeSet`，给上游一个确定的顺序）。

[pegainfer-frontend/src/vllm/bridge.rs:L540-L558](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/bridge.rs#L540-L558) — EOS 哨兵的补写：`FinishReason::Stop` 且有哨兵时向 `token_ids` 追加哨兵 token。注释完整解释了不对称：PegaInfer 抑制 EOS，vLLM 文本解码器却无条件删除 Stop 输出的末 token——不补的话投机步提交的可见后缀会被截掉。

[pegainfer-frontend/src/vllm/bridge.rs:L576-L591](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/bridge.rs#L576-L591) — 「无内容不发」的闸门与输出组装：没有 token 且未终结 → 返回 `(None, false)`（孤立 `Scheduled` 的元数据继续在 state 里等）；否则首 token 元数据、prefill 统计、KV 移交参数全部 `take()` 出来装上这一条输出——**恰好搭一次车**。

[pegainfer-frontend/src/vllm/bridge.rs:L168-L175](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/bridge.rs#L168-L175) — 退出时的兜底：对每个仍在途的请求置 abort（`Cancelled`），让调度器下次 emit 时退休它们，而不是往一条再无人排空的通道里推数据。

对照测试（无 socket、直喂事件）：[tests.rs:L408-L444](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/bridge/tests.rs#L408-L444) 断言两请求一个 burst 折成**一条**消息且互不串桶；[tests.rs:L236-L266](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/bridge/tests.rs#L236-L266) 断言孤立 `Scheduled` 不发任何东西、元数据等首 token 才冲刷。

#### 4.2.4 代码实践

1. **实践目标**：用真实测试验证 demux 的两条核心承诺——「N 个请求一个 burst 只发一条线上消息」与「孤立 Scheduled 零输出」。
2. **操作步骤**：
   - 在仓库根目录运行 `cargo test -p pegainfer-frontend --lib vllm::bridge::tests -- --nocapture`（`pegainfer-frontend` 不依赖任何 CUDA crate，纯 Rust 即可构建；本讲义未替你运行，构建耗时待本地验证）。
   - 挑出 `burst_batches_multiple_requests_into_one_message` 与 `lone_scheduled_defers_until_first_token` 两个用例精读。
   - 手工推演：仿照 `Demux` 帮手（[tests.rs:L21-L96](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/bridge/tests.rs#L21-L96)）的输入，假设 burst 内事件序列为 `req-a Token(1) → req-b Token(2) → req-a Token(3) → req-a Finished(Length)`，写出最终的 `new_token_ids` 与 `finished_requests`。
3. **需要观察的现象**：两个用例通过；手工推演结果为 req-a 一条输出 `new_token_ids=[1,3]` 且带 `finish_reason=Length`，req-b 一条输出 `[2]`，整批一条消息、`finished_requests={req-a}`。
4. **预期结果**：与 `dispatch_burst` 的分桶保序 + `reduce_request` 的 token 合并规则一致。若想进一步动手，可在自己的克隆里给 `Demux` 加一个 `req-c` 的桶做交叉验证（不要在阅读时改源码）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `dispatch_burst` 只 `try_recv` 排空就绪事件，而不是把通道里的都等出来？
**答案**：桥在 `select!` 分支里因「首条事件到达」醒来，此刻的任务是把**已经发生**的这一波折成一条消息发走。若继续阻塞等待，就把本批的发送延迟押给了未来事件，延迟陡增、且批的边界变得不可预期。「就绪即批」把延迟上界钉在一次唤醒的排空开销。

**练习 2**：abort 处理为什么必须「先 remove、后写原子量」，且用 `Release`/`Acquire` 语义？
**答案**：remove 之后 demux 对该 tag 的迟到事件查无 stream 项而丢弃；`Release` 写保证 remove 先于调度器 `Acquire` 读到的 abort 原因可见——两个动作对消费者的可见顺序被内存序钉死，不会出现「调度器已退休但桥又给迟到 token 开了门」的窗口。

**练习 3**：孤立 `Scheduled` 为什么不能立刻发一条「只含元数据」的输出？
**答案**：vLLM 线上以输出承载首 token 元数据；提前发一条空 token 的输出会引入一次无意义的客户端唤醒，且 prefill 统计里 `cached_tokens` 可能随后续分块信息更新。押后到「第一个真实输出」搭车，保证元数据恰好出现一次且携带最准的数（[tests.rs:L304-L361](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/bridge/tests.rs#L304-L361) 逐条断言了这一点）。

### 4.3 SteppedEngineBridge：StepOutputs 的 1:1 翻译与 UnixAnchor

#### 4.3.1 概念说明

`SteppedEngineBridge` 服务已迁移到 step 契约的模型线（qwen3、qwen35、gemma4、k3，以及本讲的活体标本 echo）。它的工作比 legacy 桥轻松得多，因为**折叠已经发生在引擎侧**：u3-l4 讲过账本把一步内的多次记账合并成每请求至多一条 `RequestUpdate`，u3-l5 讲过 drive 循环保证一步恰出一条 `StepOutputs`。于是桥退化为纯翻译器——模块文档的第一句话就是「每个 `StepOutputs` 消息 1:1 翻译成一个 `EngineCoreOutputs`：调度器已经分好批，每条 `RequestUpdate` 已经是每请求的折叠」。

两件 legacy 桥没有的事：

1. **标识双向映射**。legacy 用 `RequestTag = Arc<str>`（vLLM 的字符串 id 本身当键）；step 契约的 `RequestId` 是进程内整数（u3-l3）。所以除了 `streams: HashMap<RequestId, SteppedStream>`，还要有 `names: HashMap<String, RequestId>` 把线上字符串 id 映回整数 id——Abort 消息里只有字符串 id。
2. **时间渲染**。step 契约里排队/调度时刻是单调 `Instant`（调度器进程内最稳），线上要墙钟秒数。`UnixAnchor` 在桥启动时同时读两个钟，之后按差值换算，让所有渲染出的时间戳共享同一钟基。

#### 4.3.2 核心流程

主循环比 legacy 多两个分支：

```text
loop {
  shutdown.cancelled()     → 退出
  child_tasks.join_next()  → 子任务死亡即致命
  steps.recv()  ────────► dispatch_step：1 StepOutputs ⇒ 1 EngineCoreOutputs
  scheduler.exited() ───► 先排空步流里已 commit 的残余，再 break Err("scheduler exited")
  input.recv()  ────────► handle_message：Add / Abort / Utility
}
```

`reduce_update` 对一条 `RequestUpdate` 的翻译顺序（与 u3-l3 的字段物理排列一致）：

```text
scheduled 事实 → 线上 Queued/Scheduled 事件（锚点换算）+ 记 prompt_tokens
cached_tokens / kv_transfer → 暂存 state
prompt_echo → prompt logprobs（解码失败 ⇒ fail_prompt：abort + Error 终态）
tokens + logprobs → new_token_ids / 逐位置 logprobs
terminal 三态 → Finished（带类型化 stop_cause）/ Rejected / Failed
无 token、无 prompt logprobs、未终结 ⇒ 不发（元数据更新零输出）
```

墙钟换算：锚点启动时记 \( (t_{sys}, t_{inst}) = (\text{now\_secs\_f64}(), \text{Instant::now}()) \)，对任一契约时刻 \( t \)：

\[ \text{unix}(t) = t_{sys} \pm (t - t_{inst}) \quad(\text{取符号看 } t \ge t_{inst} \text{ 与否}) \]

一个桥一个锚点，所有时间戳同一钟基；`Instant` 的单调性免疫系统改时间，差值换算不放大误差。

#### 4.3.3 源码精读

[pegainfer-frontend/src/vllm/bridge/stepped.rs:L1-L8](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/bridge/stepped.rs#L1-L8) — 模块文档：与 legacy 桥「重折叠每 token 事件流」相对，本桥 1:1 翻译；墙钟时间戳由契约的单调 `Instant` 对每桥锚点渲染。这三句话就是本模块的全部大纲。

[pegainfer-frontend/src/vllm/bridge/stepped.rs:L84-L119](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/bridge/stepped.rs#L84-L119) — `run` 的开场：自铸 `engine_dead` 令牌；[wiring.rs:L97-L101](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/engine/wiring.rs#L97-L101) 的 `take_steps()` 只发一次步流（`Option`，第二消费者拿 `None`，注释明言「一条流一个消费者——协议栈的翻译循环」）；`connect_link` 传入 `metrics_watch = None` 与 `engine_dead = Some(…)`（legacy 桥正好相反）；连接后立刻发一条 stats-only 批次，让仪表先于第一步初始化。

[pegainfer-frontend/src/vllm/bridge/stepped.rs:L121-L123](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/bridge/stepped.rs#L121-L123) — 双表：`UnixAnchor::now()` 铸锚点；`streams`（整数 id → 状态）与 `names`（字符串 id → 整数 id，为 Abort 服务）。

[pegainfer-frontend/src/vllm/bridge/stepped.rs:L151-L169](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/bridge/stepped.rs#L151-L169) — 两个新增分支：步流醒来进 `dispatch_step`；`scheduler.exited()`（[wiring.rs:L109-L113](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/engine/wiring.rs#L109-L113)，driver 返回或线程退栈时解除）触发时**先把步流残余 commit 的事件发完**（`steps.is_empty()` 为 false 就 `continue` 回循环），再报致命错——调度器死前最后一步的账不能丢。

[pegainfer-frontend/src/vllm/bridge/stepped.rs:L222-L267](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/bridge/stepped.rs#L222-L267) — `dispatch_step`：一条 `StepOutputs` → 一条 `RequestBatchOutputs`。逐条 `RequestUpdate` 翻译，无 stream 项的（已 abort/已完成的迟到更新、信封 drop 炸弹的产物）丢弃；终结则双表都删并汇入 `finished_requests`；批次尾部盖上 `scheduler_stats`（`stats()` 现读 [wiring.rs:L103-L107](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/engine/wiring.rs#L103-L107) 的共享单元）。对比 4.2：这里**没有分桶循环**——`step.updates` 天然每请求至多一条。

[pegainfer-frontend/src/vllm/bridge/stepped.rs:L322-L461](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/bridge/stepped.rs#L322-L461) — `start_request`：校验与 legacy 同构（缺字段/不支持参数/LoRA 解析失败都**就地下发 Error 终态**而非断流）。stepped 特有部分在 L391-L424：从 `extra_args` 取 `kv_transfer_params`；按 u4-l4 的边界测量读 `GENERATION_PROMPT_TOKENS` 折 `history_tokens`——`checked_sub` 守卫「prompt 装不下的尾长不是边界」；读 `HISTORY_MARKS` 折 `history_marks`（过滤掉 0 与 ≥ history_tokens 的测量值）；保留 stop 哨兵只为「还没迁移到类型化 `StopCause` 的老生产者」。L437-L459：`scheduler.submit(Request)` 拿回 `RequestControl`（永不失败——调度器已死时由信封 drop 炸弹作答，u3-l6），随后 `names`/`streams` 双表登记。

[pegainfer-frontend/src/vllm/bridge/stepped.rs:L464-L488](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/bridge/stepped.rs#L464-L488) — `SteppedStream`：与 legacy 的 `RequestStreamState` 同名职责不同键。差别点：`control: RequestControl`（abort 即 `control.abort()`，取代手工原子量）；`prompt_tokens`/`cached_tokens`/`prefill_stats_sent` 让 prefill 统计**惰性构建**——分块 prefill 的 `cached_tokens` 可能晚于 `Scheduled` 才到（跟首块一起上报），惰性保证它仍能进统计（[L525-L544](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/bridge/stepped.rs#L525-L544)）；从未被调度的请求 `None`，如实上报「没做过 prefill」。

[pegainfer-frontend/src/vllm/bridge/stepped.rs:L551-L593](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/bridge/stepped.rs#L551-L593) — `reduce_update` 前半：`scheduled` → 两条线上事件，时刻经 `anchor.unix()` 换算（对照 legacy：`TokenEvent::Scheduled` 自带 unix 秒，桥直通——时间换算职责从调度器挪进了桥）；`prompt_echo` 解码失败走 `fail_prompt`（abort 控制 + Error 终态输出）；tokens/logprobs 逐位翻译。

[pegainfer-frontend/src/vllm/bridge/stepped.rs:L595-L634](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/bridge/stepped.rs#L595-L634) — 终态三态翻译：`Finished` 里哨兵只在「`Stop` 且 `stop_cause` 为 `None`」的老生产者形状下补（类型化 `StopCause::Token` 自带真实 token，走 `StopReason::TokenId`；`StopCause::Eos` 不产生 stop_reason）；`Rejected` 的类型化原因到此**降维成字符串**（线上只承载 string，注释明言「类型化分类到此为止，渲染出的消息才是客户端看到的」）；`Failed` 同 Error 处理。

[pegainfer-frontend/src/vllm/bridge/stepped.rs:L636-L652](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/bridge/stepped.rs#L636-L652) — 发送闸门：`token_ids` 空、无 prompt logprobs、未终结 → `(None, false)`（纯元数据更新零输出）；否则首 token 元数据、惰性 prefill 统计、KV 移交参数一并装车。

[pegainfer-frontend/src/vllm/bridge/stepped.rs:L655-L678](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/bridge/stepped.rs#L655-L678) — `UnixAnchor`：启动时同取 `sys`（墙钟 f64）与 `instant`（单调），`unix(t)` 按正负差换算。注释点明「每桥一个锚点：两个读数同时取，所有渲染出的时间戳共享一个钟基」。

[pegainfer-frontend/src/vllm/bridge/stepped.rs:L187-L211](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/bridge/stepped.rs#L187-L211) — 退出序列：abort 全部在途（`control.abort()`，调度器下次触碰即退休）、失败时 `engine_dead.cancel()`（让 `output_loop` 冲刷后补发死讯哨兵，见 4.1）、drop 输出通道、给子任务 1 秒排空窗口（`OUTPUT_DRAIN_TIMEOUT`）再 abort 收割。

装配点在 [mod.rs:L336-L362](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/mod.rs#L336-L362)：`LaunchedEngine::Stepped` 分支先做 u4-l1 讲过的计数对账，然后**每分区**铸一个桥（`scheduler.handle` 交桥，`scheduler.join` 收集起来等桥全灭后收割线程）。

测试佐证：[stepped.rs:L753-L794](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/bridge/stepped.rs#L753-L794) 验证「仅 scheduled/仅 metrics 的步也发批（带 stats、零请求输出）」；[L889-L977](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/bridge/stepped.rs#L889-L977) 三连测试分别锁住「stop 映射真实 token 且保 logprob」「EOS 无 stop_reason」「老生产者仍走哨兵」三个翻译决定。

#### 4.3.4 代码实践

1. **实践目标**：用 echo-server（stepped 桥的无 GPU 活体）对照源码验证「一步一条消息」与「每步一个 token」的对应关系。
2. **操作步骤**：
   - 运行 `cargo run -p pegainfer-frontend --example echo-server -- --model-path ../pegainfer-frontend/examples/echo-model`，按 u4-l2 用过的 curl 发一个非流式 `/v1/completions`：`curl -s http://127.0.0.1:<port>/v1/completions -H 'content-type: application/json' -d '{"model":"echo","prompt":"hello","max_tokens":5}'`。
   - 精读 [echo-server.rs:L121-L158](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/examples/echo-server.rs#L121-L158)：`EchoScheduler::step` 每步只 `push_tokens(&[token])` 一个——注释写明「每步一个 token 保持逐 token 流式路径温热，而不是把响应坍缩成一批」。
   - 数一数：`max_tokens=5` 的请求理论上经历几次调度器步、桥发出几条 zmq 消息、SSE/响应里出现几段增量（非流式则最终聚成 5 个 token）。
   - 再跑单元测试：`cargo test -p pegainfer-frontend --lib vllm::bridge::stepped`。
3. **需要观察的现象**：返回文本等于 prompt 的 token 级回声（u4-l2 讲过原因）；stepped 测试全绿（6 个用例）。
4. **预期结果**：5 个 token ⇒ 至少 5 次步（外加 admit 那一步的 scheduled 事实随首 token 搭车）⇒ 桥每步恰一条 `EngineCoreOutputs`。运行结果待本地验证；若 curl 端口不确定，以 echo-server 启动横幅打印的端口为准。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `names: HashMap<String, RequestId>` 在 stepped 桥是必须的，legacy 桥却不需要？
**答案**：Abort 消息里只有 vLLM 的字符串 request id。legacy 契约的 `RequestTag = Arc<str>` 直接拿字符串当键，查 `streams` 一步到位；step 契约的 `RequestId` 是进程内整数（外部字符串 id 留在协议栈，u3-l3），桥必须自存「字符串 → 整数」映射才能把 abort 落到正确的账户上。

**练习 2**：契约为什么坚持内部用 `Instant`、由桥统一渲染墙钟，而不是调度器直接产 unix 秒（legacy `TokenEvent::Scheduled` 就自带 unix 秒）？
**答案**：调度器测的是「排队多久、何时被调度」这类**差值**，单调时钟不受系统改时间干扰、进程内比较零成本；unix 秒只有在跨进程展示时才有意义。把换算推到桥上、每桥一个锚点，还消除了不同调度器实例各自读墙钟的钟基不一致问题。

**练习 3**：`scheduler.exited()` 分支里 `if !steps.is_empty() { continue; }` 删掉会发生什么？
**答案**：调度器线程死前最后 commit 的步（含 fail_all 写销出的 Failed 终态）可能还压在步流里没被消费，直接 break 会把这些终态吞掉——客户端等不到任何完结信号。先排空再报错，保证「应答到底」（u3-l4 的账本保证 + 这里 的桥侧兑现）。

### 4.4 StatsTracker：累计计数器与按批 delta

#### 4.4.1 概念说明

u3-l5/u4-l6 讲过：调度器的 `SchedulerMetrics` 是自画像快照，其中 spec-decode 与 prefix-cache 计数器是**累计值**——因为读者只看到最新快照、无法对每步增量求和，累计是唯一可无损传输的形态。而 vLLM 前端的 Prometheus 计数器语义是 `inc_by`（**按区间增量**）。`StatsTracker` 就是差分器：记住上一批的基线，把累计值差分成 delta，随批上报。

它还牵出两代桥的**发布通道分叉**，藏在 `connect_link` 的注释里：legacy 引擎的调度循环空闲时会 park，watch 变化节奏有界，所以挂一个独立任务「每次 watch 变化发一条 stats-only 批」；stepped 的 driver 空转自旋（u3-l5），每次自旋都会变成一条消息，发独立任务等于刷屏——所以 stepped 桥不传 watch，改为**发批时现读** `scheduler.metrics()` 盖章在每条真实批上。

#### 4.4.2 核心流程

```text
SchedulerMetrics（累计）──► StatsTracker.stats() ──► SchedulerStats（本批 delta + 当前水位 gauge）
                              │  gauge 直通：running/waiting/kv 占用
                              │  spec_decode：与基线差分；零 draft 区间丢弃（前端要除以 num_drafts）
                              │  prefix_cache：与基线差分；总数倒退 ⇒ 引擎重启 ⇒ 从零起差
                              └─ 基线推进（本批没发出去也只丢这段区间的 delta）
```

两条发布通道：

```text
legacy：watch.changed() ──► publish_scheduler_stats ──► stats-only 批（无请求输出）
stepped：dispatch_step 每发一批 ──► tracker.stats(scheduler.metrics()) 盖章随批
```

#### 4.4.3 源码精读

[pegainfer-frontend/src/vllm/bridge.rs:L694-L732](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/bridge.rs#L694-L732) — `StatsTracker` 的全部：两个字段基线（spec 与 prefix-cache）；`stats()` 组装 gauge + delta 并**推进基线**，文档注释言明「调用后放弃发送只丢这一区间的 delta」；`spec_decode_interval` 把零 draft 区间折成 `None`——前端算接受率要除以 `num_drafts`，零区间既无意义又会除零。

[pegainfer-frontend/src/vllm/bridge.rs:L598-L612](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/bridge.rs#L598-L612) — gauge 侧：running/waiting 直通，KV 占用 \( \text{used}/\text{total} \)（total 为 0 时记 0.0）。注释点出消费方：前端的 `scheduler_running`/`scheduler_waiting`/`kv_cache_usage` 与 DP 负载均衡器。

[pegainfer-frontend/src/vllm/bridge.rs:L637-L690](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/bridge.rs#L637-L690) — `prefix_cache_delta` 的三层语义：本 tier / host tier / 无法归因 tier 分别进本地 stats 与 connector stats（带 `source` 标签）；**总数倒退即引擎重启**，从零起差而不是下溢或丢新账。长注释逐条解释了前端如何把两半加总成「每个 hit 恰好一次」。

[pegainfer-frontend/src/vllm/bridge.rs:L734-L766](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/bridge.rs#L734-L766) — legacy 专属的 `publish_scheduler_stats`：先发当前快照（仪表在第一步之前初始化），此后**每次 watch 变化**恰一条 stats-only 批。注释解释为何两个输入都致命：丢了任何一个，这个引擎注册着的就是过期指标与路由黑洞。

[pegainfer-frontend/src/vllm/bridge.rs:L876-L891](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/bridge.rs#L876-L891) — 分叉的权威注释：legacy 传 watch 在此挂发布任务；stepped 传 `None`，因为它 driver 忙轮询、每次自旋都会变成一条消息，改为「发批时读 watch、把 stats 盖在本来就要发的批上」。

[pegainfer-frontend/src/vllm/bridge/stepped.rs:L216-L220](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/bridge/stepped.rs#L216-L220) — stepped 的对应物只有三行：`stats()` 现读 handle 的共享单元交给 tracker；注释点出基线锚点是「上一批发出的批」而非「上一步」——步可以不产生消息（纯元数据步也发批，但请求侧零输出时仍算发了），基线与消息对齐。

测试佐证：[tests.rs:L567-L611](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/bridge/tests.rs#L567-L611) 验证「初始快照先发、每次变化恰一条、gauge 数值正确」；[tests.rs:L627-L684](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/bridge/tests.rs#L627-L684) 验证「watch 合并了两步的计数时 delta 覆盖整个空档」——这正是传输累计值的根本理由；[tests.rs:L693-L799](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/bridge/tests.rs#L693-L799) 验证 prefix-cache 的重启起差与双 tier 归因。

#### 4.4.4 代码实践

1. **实践目标**：亲眼确认「累计进、delta 出」与「watch 只在真实变化时唤醒」。
2. **操作步骤**：
   - 运行 `cargo test -p pegainfer-frontend --lib vllm::bridge::tests::load_snapshots_become_stats_only_batches -- --nocapture` 与 `…::spec_stats_are_per_interval_deltas_that_skip_idle_intervals`。
   - 精读第二个测试的输入序列：`observe_draft(2,1)` → 无变化重发 → `observe_draft(2,2)` 两次后重发，对照断言里的 `num_drafts` 1 → None（空档）→ 2。
3. **需要观察的现象**：两个测试通过；空档区间 `spec_decoding_stats` 为 `None`；两次 observe 合并后的 delta 恰好覆盖两次。
4. **预期结果**：与 4.4.3 的规则一致。思考题随做：若把 `SchedulerMetrics` 里的计数器改成每步 delta 传输，哪两个测试场景会开始丢数？（答案见下一小节练习 3。）

#### 4.4.5 小练习与答案

**练习 1**：为什么计数器必须累计、delta 在桥侧算，而不是调度器直接报 delta？
**答案**：读者（桥/前端）只看到快照序列，且 watch 会**合并**多次变化（测试 L664-L668 专门构造了两次 observe 合并成一次 watch 唤醒）。累计值对合并无损——差分覆盖整个空档；每步 delta 一旦丢一步就永久少记，且空转步还得发「零 delta」消息。

**练习 2**：stepped 桥为什么不能复用 `publish_scheduler_stats` 任务？
**答案**：watch 的变化频率跟随生产者。stepped 的 driver 空闲也不睡（spin_loop，u3-l5），每轮迭代发布一次 metrics 就是每轮一次 watch 变化，独立任务会把它全量转成 stats 消息刷屏。legacy 调度器空闲时 park，watch 节奏有界，独立任务才划算。

**练习 3**：接 4.4.4 思考题：改成每步 delta 后哪两个场景丢数？
**答案**：(1) watch 合并场景——两步的 delta 只剩最后一笔，另一笔永久丢失（`spec_stats_are_per_interval_deltas…` 的断言 `num_drafts == 2` 会失败）；(2) 引擎重启场景——重启前的 delta 基线错乱，新引擎的第一笔要么被并进旧账要么从负数起差（`prefix_cache_stats_are_per_interval_deltas_across_a_restart` 锁定的行为被破坏）。

## 5. 综合实践

**任务：画出一个 token 的完整旅程时序图，并排两代桥。** 这是本讲规格指定的主实践，把 u3 的契约知识与本讲的翻译层串成一条线。

要求：

1. 泳道从左到右：客户端、vllm-server（HTTP 层）、引擎桥、调度器（driver 循环）。
2. 跟踪一个 token 的去程与回程：HTTP body → `EngineCoreRequest`（zmq Add 两帧）→ 桥 `start_request` → `SchedulerHandle::submit`（stepped）/ `EngineHandle::submit`（legacy）→ 调度器 step → `RequestUpdate.tokens`（stepped）/ `TokenEvent::Token`（legacy）→ `EngineCoreOutputs` → SSE chunk。
3. 在图上并排 legacy 与 stepped 两代桥，标注两处差异：**每 token 唤醒次数**（桥任务被唤醒几次）与**批处理折叠位置**（token 合并成输出这件事发生在哪一层）。
4. 用 echo-server 实跑一遍非流式与流式请求，把图中「SSE chunk」一端的真实观察（几个增量、何时到达）标注上去。

参考答案骨架（你的图应体现这些事实）：

```text
客户端        vllm-server(HTTP)          桥(每分区一个)                调度器线程
  │ POST /v1/completions │                    │                          │
  │────────────────────►│ 解析/整形(u4-l4)     │                          │
  │                     │──Add(2帧,msgpack)──►│ start_request:            │
  │                     │  (DealerSocket)     │ legacy: EngineHandle     │
  │                     │                     │   .submit(GenerateRequest)├─► submit队列
  │                     │                     │ stepped: SchedulerHandle │
  │                     │                     │   .submit(Request)──────►│ (信封+账本开户)
  │                     │                     │                          │ step() 裁决
  │                     │                     │ legacy: TokenSink 把      │
  │                     │                     │  Token(tag,event)推共享通道│
  │                     │                     │  ◄──(每token 1条,混流)────┤
  │                     │                     │ stepped: ◄─StepOutputs───┤
  │                     │                     │  (每步1条,已含每请求fold) │
  │                     │                     │ legacy: dispatch_burst    │
  │                     │                     │  demux+reduce_request     │
  │                     │                     │  (折叠发生在这里!)        │
  │                     │                     │ stepped: dispatch_step    │
  │                     │                     │  1:1翻译(折叠已在账本)    │
  │                     │◄─EngineCoreOutputs──│ (PushSocket, 每步1条)     │
  │◄──SSE chunk(逐token)│                     │                          │
```

两处差异的标准答案：

| 维度 | legacy 桥 | stepped 桥 |
|---|---|---|
| 每 token 唤醒次数 | 桥任务**每 burst 一次**（select 醒一次、`try_recv` 排空整批），不是每 token 一次；但 `TokenSink` 侧每 token 推一条进通道 | 桥任务**每步一次**（步流每步一条消息，token 数已在消息里） |
| 批处理折叠位置 | **桥内**（`dispatch_burst` 分桶 + `reduce_request` 合并） | **引擎侧账本**（`RequestUpdate` 已是每请求折叠，桥纯翻译） |

观察要点：流式请求下 SSE 增量的到达节奏应与 echo 调度器「每步一 token」一致；非流式请求则是同一条流水线、由 HTTP 层聚齐后一次性返回。

## 6. 本讲小结

- 引擎桥是**进程内的 vLLM EngineCore 替身**：向外说 zmq + msgpack 的 EngineCore 协议，向内说 PegaInfer 引擎契约；`BridgeLink` 把握手、Dealer/Push 双 socket 与输出泵抽成两代桥共享的传输层。
- Bootstrapped transport 的顺序是骨架**先 bind** 两个 unix listener、把裸 fd 交给 vllm-server 领养，桥引擎就绪后才 connect——「端口可达 = 引擎就绪」与「桥多晚都能连上」同时成立。
- legacy 桥做 **demux + 折叠**：一条共享打标通道、一次唤醒排空就绪 burst、按请求分桶折成每请求至多一条输出、整批一条 zmq 消息；孤立 `Scheduled` 与 KV 移交元数据押后到首个真实输出搭车；EOS 哨兵补齐 vLLM 文本解码器「删 Stop 末 token」的协议差异。
- stepped 桥做 **1:1 翻译**：折叠已由账本完成，`StepOutputs` → `EngineCoreOutputs` 逐条翻译；双表（`streams` 整数 id / `names` 字符串 id）服务 abort；调度器死前排空步流残余再报错。
- `UnixAnchor` 每桥一铸：契约内部单调 `Instant`，线上墙钟由锚点差值渲染，所有时间戳共享一个钟基。
- `StatsTracker` 把累计计数器差分成按批 delta（重启起差、零区间丢弃）；stats 发布通道两代分叉——legacy 挂 watch 驱动的独立任务，stepped 发批时现读盖章（driver 自旋不容刷屏）。

## 7. 下一步学习建议

- **u4-l4（请求整形）**：本讲 `start_request` 里读到的 `GENERATION_PROMPT_TOKENS` / `HISTORY_MARKS` 两个 extra_args 正是下一讲 history 边界测量的产物——去看看它们是怎么被算出来、写进 `vllm_xargs` 的。
- **u4-l5（特殊服务面）**：本讲握手里 `supports_lora: false` 与 `start_request` 里的 LoRA 适配器解析，在 LoRA 路由那条服务面里另有归宿。
- **u4-l6（可观测性）**：本讲 4.4 的 delta stats 抵达 `/metrics` 的最后一跳，以及 `disable_log_stats=false` 这条生命线。
- 源码延伸阅读：`vllm-engine-core-client`（workspace 外部协议 crate）里 `EngineCoreRequest`/`EngineCoreOutputs` 的完整定义，与 `pegainfer-sim` 的 `frontend_e2e` 集成测试（[tests.rs:L1-L8](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/bridge/tests.rs#L1-L8) 点名它是 HTTP→ZMQ→桥全链路的 CI 门）。
