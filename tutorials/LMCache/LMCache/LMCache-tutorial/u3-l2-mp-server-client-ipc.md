# MP Server/Client 与进程间通信

## 1. 本讲目标

在 u3-l1 中我们已经知道：LMCache 把 KV cache 管理拆成了独立的 **daemon 进程**，vLLM/SGLang/TRT-LLM 等 worker 进程要跨进程地「查命中、搬 KV」。本讲就拆开这条跨进程通道，回答四个问题：

1. worker 发出的一个请求，是怎么穿过进程边界、被 daemon 处理、再把结果送回来的？（**消息队列 mq.py**）
2. worker 发完请求后如何「不等结果先干别的」，等到需要时再去取？（**异步 Future futures.py**）
3. 几百 MB 的 GPU KV 张量，是怎么做到**零拷贝**地从一个进程「搬」到另一个进程的？（**CUDA IPC handle / DeviceIPCWrapper**）
4. 没有 GPU 的 CPU 场景，怎么用**共享内存**完成同样的零拷贝？（**posix_shm.py**）

学完后你应该能够：

- 画出「client 发请求 → 消息队列 → server 处理 → future 返回」的完整时序；
- 说清楚 ZMQ 的 `ROUTER`/`DEALER` socket 在这里扮演的角色；
- 解释 `MessagingFuture` 与 `CUDAMessagingFuture` 的区别；
- 说清楚为什么 KV 张量本身**从不走 ZMQ**，跨进程传递的只是 IPC handle；
- 指出 CUDA IPC handle 和 POSIX 共享内存分别在数据流的哪一步跨进程传递。

## 2. 前置知识

在进入源码前，先用大白话建立几个直觉。

### 进程隔离与「不能直接传指针」

操作系统给每个进程独立的虚拟地址空间。进程 A 里的指针 `0x7fff1234`，在进程 B 里指向的可能是完全无关的内存。所以「跨进程搬一坨数据」本质上不能靠传指针。常见办法有两种：

- **消息传递（message passing）**：把数据序列化成字节流，通过 socket/管道/队列发给对方，对方反序列化。简单通用，但要拷贝、要序列化，慢。
- **共享内存（shared memory）**：让两个进程把同一块物理内存映射进各自的地址空间，于是同一块内存有两个地址，但写一边另一边立刻能看到。零拷贝、超快，但要自己管同步。

LMCache 的设计精髓在于：**控制消息走消息传递，KV 张量走共享内存**——各取所长。

### ZMQ 的 ROUTER / DEALER

[ZMQ](https://zeromq.org/) 是一个高性能消息库。本讲只需要记住两种 socket：

- `DEALER`（客户端）：连接到服务端，发请求、收响应，可以「发一堆再一起收」（异步）。
- `ROUTER`（服务端）：每条收到的消息会自动带一个**发送方身份（identity）**帧。ROUTER 靠这个身份帧把响应「原路送回」给正确的客户端，所以一个 ROUTER 能同时服务很多 DEALER。

### CUDA IPC handle

GPU 显存同样不能靠指针跨进程共享。但 CUDA 提供了一套机制：进程 A 可以把一块显存「发布」成一个 **IPC handle**（一小串字节），进程 B 拿到这串 handle 后调用 CUDA API，就能把同一块物理显存映射进自己的上下文，得到一个指向**同一块显存**的新张量。这就是 GPU 版的「共享内存」。

### Future（期程/承诺）

`Future` 是异步编程里的经典抽象：你发起一个请求，立刻拿到一个 `Future` 对象（「承诺」），它现在还没有结果；等对方处理完，结果会被「填进」这个 Future；你可以在需要时调用 `future.result()` 阻塞等待结果。这样就把「发请求」和「拿结果」解耦了。

> 承接 u3-l1：`MPCacheServer` 是个「组合器」，它组装一堆 `EngineModule`（Lookup/P2P/Management/Transfer/Blend）。本讲关心的不是这些模块的业务逻辑，而是它们**底下**那条共用的传输管道——所有模块的 handler 都挂在同一条消息队列上。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [lmcache/v1/multiprocess/server.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/server.py) | daemon 主入口与 `MPCacheServer` 组合器：建 ZMQ server、注册各模块的 handler、分配线程池。把各模块「焊」到消息队列上。 |
| [lmcache/v1/multiprocess/mq.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/mq.py) | 消息队列本体：`MessageQueueServer`（ROUTER）、`MessageQueueClient`（DEALER）、共享轮询循环 `ClientPollingLoop`、同步/阻塞两类 handler、普通/亲和两种线程池。 |
| [lmcache/v1/multiprocess/futures.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/futures.py) | 异步结果抽象：`MessagingFuture`（纯 CPU 的等待）与 `CUDAMessagingFuture`（额外挂在一个 CUDA Event 上等待）。 |
| [lmcache/v1/multiprocess/posix_shm.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/posix_shm.py) | POSIX 共享内存的薄封装：`shm_create_readwrite`/`shm_map_readwrite`/`shm_munmap`/`shm_unlink`/`shm_open_pool_as_mmap`，供 CPU 端零拷贝传输使用。 |
| lmcache/v1/multiprocess/custom_types.py | 定义跨进程的类型：缓存键 `IPCCacheServerKey`、`KVCache = list[DeviceIPCWrapper]`，以及把 `DeviceIPCWrapper` 挂进 msgspec 的定制编解码器。 |
| lmcache/v1/platform/base_ipc_wrapper.py + cuda/ipc_wrapper.py | GPU 张量的 IPC 包装器：`DeviceIPCWrapper`（基类，pickle 序列化）与 `CudaIPCWrapper`（CUDA `_share_cuda_` / `_new_shared_cuda`）。 |
| lmcache/v1/multiprocess/protocols/engine.py | 协议定义：每个 `RequestType`（STORE/RETRIEVE/LOOKUP…）的 payload 类型、响应类型、handler 类型（SYNC/BLOCKING）。 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

- **4.1 消息队列 mq.py**：请求/响应模型、ROUTER/DEALER、线程池。
- **4.2 异步 Future futures.py**：`MessagingFuture` 与 `CUDAMessagingFuture`。
- **4.3 GPU 零拷贝：CUDA IPC handle（DeviceIPCWrapper）**：为什么张量不走 ZMQ。
- **4.4 CPU 零拷贝：POSIX 共享内存 posix_shm.py**。

最后用 **4.5 综合时序** 把四者串起来，并指出 IPC handle 在哪一步跨进程传递。

---

### 4.1 消息队列：mq.py 的请求/响应模型

#### 4.1.1 概念说明

`mq.py` 实现了一条非常像「RPC over ZMQ」的通道：

- **server 端**用 `zmq.ROUTER` socket 监听一个 TCP 端口，主循环里收请求 → 查 handler 表 → 执行 → 把响应原路发回。
- **client 端**用 `zmq.DEALER` socket 连到 server，`submit_request(...)` 立刻返回一个 `MessagingFuture`，真正的收发在一个后台轮询线程里完成。

请求和响应都用 [msgspec/msgpack](https://jcristharif.com/msgspec/) 编码成紧凑的二进制。一条请求在 ZMQ 线上是**多帧（multipart）**的：

```
[ identity, request_uid, request_type, payload_0, payload_1, ... ]
```

- `identity`：ROUTER 自动塞入的发送方身份帧，server 回复时原样带回去；
- `request_uid`：客户端自增的整数编号，用来把「响应」匹配回当初的 `Future`；
- `request_type`：枚举，告诉 server 这是 STORE / RETRIEVE / LOOKUP…；
- 后面若干 `payload`：按协议规定好的类型逐个编码的业务参数。

handler 被分成两类：

- **SYNC handler**：又快又不阻塞，直接在主循环线程里跑完并回复（例如 `REGISTER_KV_CACHE` 只是把 wrapper 存进一张表）。
- **BLOCKING handler**：可能很慢（搬 KV 要等 GPU），丢进**线程池**里跑，跑完再排队发回（例如 `STORE`/`RETRIEVE`）。

线程池又分两种：普通 `ThreadPoolExecutor`（CPU 簿记，如 LOOKUP）和 `AffinityThreadPool`（GPU 搬运，如 STORE/RETRIEVE）。**亲和池**保证「同一个客户端身份的请求永远落到同一条 worker 线程」，这样就不必为共享 GPU 临时缓冲加锁。

#### 4.1.2 核心流程

```text
worker 进程 (client)                         cache daemon (server)
─────────────────────                         ──────────────────────
submit_request(type, payloads)
  ├─ 新建 MessagingFuture
  ├─ 包成 WrappedRequest(uid, future, ...)
  ├─ 丢进 input_queue
  └─ notify() 唤醒轮询线程                ┐
                                          │ ZMQ DEALER → ROUTER
ClientPollingLoop._main_loop:             │
  process_outbound_task()                 │
    ├─ pending_futures[uid] = future      │
    ├─ msgspec 编码每个 payload            │
    └─ socket.send_multipart(             │
        [uid, type, *payloads]) ──────────┤
                                          ▼
                                          _main_loop:
                                            recv_multipart()
                                            → [identity, uid, type, *payloads]
                                            查 handlers[type]
                                            _call_handler(...)
                                              SYNC   → 主循环里直接跑 + 立刻回
                                              BLOCKING→ 丢线程池，完成回调入队
                                            （读 output_queue，send_multipart 回写）
                                          ┐
                                          │ ROUTER → DEALER
process_inbound() ◀───────────────────────┤
  recv_multipart() → [uid, type, *resp]
  future = pending_futures.pop(uid)
  future.set_result(resp)                 │
                                          ▼
worker 调 future.result() 拿到结果
```

注意三个关键设计：

1. **收发不在调用线程**：`submit_request` 只入队，真正的 ZMQ 收发在 `ClientPollingLoop` 单例后台线程里。这样 worker 主线程不会因网络阻塞而卡住。
2. **`request_uid` 配对**：一个 client 可能同时有几十个在途请求，靠 uid 把每个响应精确塞回对应的 Future。
3. **回写也走队列**：BLOCKING handler 在线程池里跑，但 ZMQ socket 不是线程安全的，所以结果先入 `output_queue`，由主循环线程统一 `send_multipart`。

#### 4.1.3 源码精读

**客户端：连接 DEALER、注册到共享轮询循环。**

[`MessageQueueClient.__init__`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/mq.py#L267-L282) 创建一个 `DEALER` socket 并连到 server，同时维护两个关键结构：`input_queue`（待发送的请求）和 `pending_futures`（uid → Future 的在途账本）。然后把自己注册进单例 `ClientPollingLoop`。

**客户端：提交请求（核心入口）。**

[`submit_request`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/mq.py#L355-L383) 做三件事：建一个空 `MessagingFuture`、从自增计数器拿一个 `request_uid`、把请求包成 `WrappedRequest` 丢进 `input_queue`，最后 `notify()` 踹醒后台轮询线程。注意它**不直接发 ZMQ**，立刻返回 future。

请求的「外壳」数据结构是 [`WrappedRequest`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/mq.py#L260-L265)：把 uid、future、type、payloads 绑在一起。

**客户端：后台线程真正发包。**

真正编码和发送发生在 [`process_outbound_task`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/mq.py#L284-L323)：把 `WrappedRequest` 登记进 `pending_futures`，按协议查出每个 payload 的类型并用 `msgspec_encode` 编码，最后 `socket.send_multipart([uid, type, *payloads])`。其中有一段**防御性校验**——payload 个数对不上就抛出带有「version mismatch」提示的 `ValueError`，这是排查 client/server 版本不一致的关键线索。

**客户端：收到响应、填 Future。**

[`process_inbound`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/mq.py#L327-L353) 收到多帧响应，拆出 `uid`，从 `pending_futures` 里 `pop` 出对应 future，调用 `future.set_result(response)`。至此 worker 主线程在 `future.result()` 上的等待就被唤醒。

> 注意：`pending_futures` **只被轮询线程访问**（见 `process_inbound` 注释），所以不需要加锁。

**服务端：ROUTER socket + 输出事件通知。**

[`MessageQueueServer.__init__`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/mq.py#L490-L517) 创建 `ROUTER` socket 并 `bind`。它还建了一个 `output_queue` 配一个跨平台 `EventNotifier`（Linux 上是 eventfd）——原因是 BLOCKING handler 在线程池里完成时不能直接调 ZMQ 发送（socket 非线程安全），只能入队后让主循环线程统一发；eventfd 就是用来「踹醒」阻塞在 `poller.poll()` 里的主循环。

**服务端：主循环收发分发。**

[`_main_loop`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/mq.py#L597-L640) 是 server 的心脏。它在一个 `zmq.Poller` 上同时监听两件事：

- socket 可读 → 收请求，按 `request_type` 查 `handlers` 字典，调 `_call_handler`；
- eventfd 可读 → 说明线程池里有结果排队了，drain `output_queue` 逐条 `send_multipart` 回写。

收到的消息被拆成 `identity, b_request_uid, b_request_type, *payloads`，其中 `identity` 会连同 uid/type 一起作为 `prefix_frames` 透传给 handler，**回复时原样拼回去**，ROUTER 才知道往哪个 client 送。

**服务端：SYNC vs BLOCKING 两条执行路径。**

[`_call_blocking_handler`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/mq.py#L541-L577) 是重点：它取 `prefix_frames[0]`（client 身份）的 `hash` 作为 **affinity_key**，把任务提交到线程池，并给返回的 `concurrent.futures.Future` 挂一个 `_notify_response` 回调——回调里把结果编码、拼上 prefix、入 `output_queue`、再 `notify()` eventfd。整条「线程池完成 → 主循环发回」的异步链路就在这里接上。

对应的两种 handler 包装：[`SyncRequestHandler`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/mq.py#L406-L428) 直接在当前线程同步执行；[`BlockingRequestHandler.__call__`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/mq.py#L452-L464) 把任务 `submit` 到分配好的 executor（普通池或亲和池）。

**两种线程池。**

[`add_normal_thread_pool`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/mq.py#L780-L816) 给非 GPU 的慢操作（如 LOOKUP、END_SESSION）配普通 `ThreadPoolExecutor`；[`add_affinity_thread_pool`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/mq.py#L818-L855) 给 GPU 搬运（STORE/RETRIEVE）配 `AffinityThreadPool`。亲和的意义见 [`affinity_pool.py` 的模块文档](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/affinity_pool.py#L1-L11)：同一 vLLM 实例的所有请求落同一条线程，免去共享 GPU 临时缓冲的锁。

**server.py 如何把模块焊到队列上。**

daemon 入口 [`run_cache_server`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/server.py#L283-L309) 建好 `MessageQueueServer` 后，遍历所有 `EngineModule` 的 `get_handlers()`，逐个用 [`add_handler_helper`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/server.py#L143-L160) 注册；然后按 `HandlerSpec.pool` 把 AFFINITY 类型和 NORMAL 类型分别塞进对应的线程池（见 [server.py:L370-L390](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/server.py#L370-L390)）。也就是说，业务模块只管「我处理哪些请求、该跑在哪种池里」，传输细节全由 mq.py 兜底。

#### 4.1.4 代码实践

> **目标**：跑通「client 发 NOOP → server 回字符串」的最小端到端，确认请求/响应时序。

项目里 [`tests/v1/multiprocess/test_mq.py`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/tests/v1/multiprocess/test_mq.py) 已经把这套 client/server 封装成了 `MessageQueueTestHelper`，会自动起 server 进程和若干 client 进程。最快的实践就是直接跑它的 NOOP 用例：

```bash
# 在仓库根目录
python -m pytest tests/v1/multiprocess/test_mq.py::test_mq_noop_request -x -s
```

操作步骤：

1. 读 [`test_mq_noop_request`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/tests/v1/multiprocess/test_mq.py#L308-L323)（注册一个返回 `"NOOP_OK"` 的 handler，发一条请求，期望同名响应）。
2. 在 `MessageQueueClient.submit_request`（mq.py:L355）和 `MessageQueueServer._main_loop`（mq.py:L597）各加一行 `logger.info` 打印 uid。
3. 重跑上面的 pytest，对照日志验证：client 的 `submit_request` 入队 uid=0 → 后台线程 `send_multipart` → server `_main_loop` 收到 → handler 执行 → response 经 `output_queue` 回写 → client `process_inbound` 把结果塞进 future。

需要观察的现象：client 主线程在 `future.result(timeout=5)` 上短暂阻塞后拿到 `"NOOP_OK"`；server 日志里能看到 uid=0 的一次收发。

预期结果：测试通过，且日志显示一次完整的 uid 配对往返。

> 说明：本实践需要能 `import zmq`、`import msgspec`，但**不需要 GPU**（NOOP 是 SYNC handler，在主循环里直接跑）。如果环境缺 ZMQ，这一步即为「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `submit_request` 不直接调用 `socket.send_multipart`，而是先入 `input_queue`？

> **参考答案**：ZMQ socket 不是线程安全的，多个 worker 线程可能同时发请求；统一入队后由唯一的 `ClientPollingLoop` 后台线程串行收发，既避免并发访问 socket，又让调用线程不被网络 I/O 阻塞。

**练习 2**：如果 client 发了 10 条请求但只收到了 8 条响应，`pending_futures` 里会剩下什么？会泄漏吗？

> **参考答案**：会剩下 2 个还没 `pop` 的 `MessagingFuture`，它们在 `future.result()` 上阻塞等待。只要 server 最终回包，`process_inbound` 会按 uid 逐个 `pop` 并 `set_result`；只有当响应真的丢失（如 server 崩溃）时才会泄漏到调用方超时抛 `LMCacheTimeoutError`。

---

### 4.2 异步 Future：futures.py

#### 4.2.1 概念说明

`futures.py` 提供两个类：

- **`MessagingFuture[T]`**：纯 CPU 语义的 Future。内部就一个 `threading.Event`（`is_done_`）加一个结果槽 `result_`。`set_result` 写入结果并 `set()` 事件；`result(timeout)` 等事件被置位后返回结果，超时抛 `LMCacheTimeoutError`。
- **`CUDAMessagingFuture[T]`**：在普通 Future 之上，额外挂在一个**跨进程 CUDA Event** 上。它的「完成」不只取决于对方填了结果，还要等那块 GPU 显存真的「搬完」。

为什么要 `CUDAMessagingFuture`？因为 STORE/RETRIEVE 的响应是 `(cuda_event_handle, success)`——server 把数据拷进/拷出共享显存是在某条 CUDA Stream 上排队的，server 会回传一个**已 record 的 CUDA Event 的 IPC handle**。client 收到 handle 后必须等这个 Event 被 GPU 执行完（`synchronize`），才能保证读到的 KV 数据是完整的。普通 Future 只保证「字节到了」，不保证「GPU 算完了」。

#### 4.2.2 核心流程

```text
普通 MessagingFuture:
  set_result(v): result_ = v; is_done_.set()
  result(t):     wait(t) ? return result_ : raise Timeout

CUDAMessagingFuture（raw_future 的结果是 (event_bytes, value)）:
  result(t):
    if event_ 已重建: event_.synchronize() → 返回 value
    else: raw_future.result(t) 拿到 (event_bytes, value)
          event_ = Event.from_ipc_handle(device, event_bytes)  # 重建跨进程 Event
          event_.synchronize()                                   # 等 GPU 搬完
          返回 value
```

关键点：CUDA Event 也是**跨进程**的——server 进程在它那条 Stream 上 `record`，把 IPC handle 字节回传；client 进程用 `Event.from_ipc_handle` 重建出指向**同一个 GPU Event** 的对象，再 `synchronize`。这就是「GPU 端的完成信号」跨进程传递的方式。

#### 4.2.3 源码精读

**普通 Future 的全部语义。**

[`MessagingFuture`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/futures.py#L13-L76) 非常薄。[`result`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/futures.py#L40-L57) 就是 `wait` 事件 + 取 `result_`，超时抛 `LMCacheTimeoutError`。[`set_result`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/futures.py#L59-L69) 的文档明确写着「不应由用户直接调用，只应由消息系统在结果就绪时调用」——在 mq.py 里，调用者正是 `process_inbound`。

**CUDA Future：把「字节到达」升级成「GPU 完成」。**

[`CUDAMessagingFuture`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/futures.py#L79-L86) 持有一个原始的 `raw_future_`（其结果是 `(bytes, T)`）和重建出来的 `event_`。核心逻辑在 [`_on_raw_future_complete`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/futures.py#L99-L115)：从 raw_future 取出 `event_bytes`，用 `torch_dev.Event.from_ipc_handle(device, event_bytes)` 在本进程重建出跨进程 CUDA Event。注意它对后端做了能力检查——`from_ipc_handle` 不存在就直接 `RuntimeError`，提示「Multiprocess IPC requires CUDA」。

[`wait`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/futures.py#L117-L149) 的两段式：如果 `event_` 已重建过，直接 `event_.synchronize()`（GPU 完成信号）；否则先等 raw_future、重建 event、再 `synchronize`。两条路径都保证返回前 GPU 端的搬运已经结束。

#### 4.2.4 代码实践

> **目标**：用单进程内的线程复现 `MessagingFuture` 的「先发后等」语义，无需 GPU。

[`test_messaging_future_with_thread`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/tests/v1/multiprocess/test_futures.py#L52-L74) 正好是这种用法。你可以照抄它写个最小脚本：

```python
# 示例代码：演示 MessagingFuture 的 set/wait（仿 test_futures.py，非项目原文件）
import threading, time
from lmcache.v1.multiprocess.futures import MessagingFuture

future = MessagingFuture[str]()
def producer():
    time.sleep(0.3)
    future.set_result("Hello from thread")
threading.Thread(target=producer).start()

print("before:", future.query())        # False
print("result:", future.result(timeout=2))   # 阻塞约 0.3s 后打印
print("after:", future.query())         # True
```

操作步骤：直接 `python demo.py` 运行；或在 `future.result(timeout=2)` 处下断点，单步观察「事件未置位→阻塞→子线程 set_result→事件置位→返回」。

需要观察的现象：`query()` 在 `set_result` 前为 False、之后为 True；`result()` 会阻塞到结果就绪。

预期结果：依次打印 `before: False`、约 0.3 秒后 `result: Hello from thread`、`after: True`。

> 有 GPU 的读者可额外跑 `pytest tests/v1/multiprocess/test_futures.py::test_cuda_messaging_future_basic_usage -s`，它在子进程里创建并 record 一个 `interprocess=True` 的 CUDA Event，把 `ipc_handle()` 字节传回主进程，演示跨进程 Event 的重建与等待。

#### 4.2.5 小练习与答案

**练习 1**：既然有普通 `MessagingFuture`，为什么 RETRIEVE 还需要 `CUDAMessagingFuture`？

> **参考答案**：RETRIEVE 的数据拷贝在 server 的 CUDA Stream 上异步排队，响应到达 client 时数据可能还没真正写完。普通 Future 只表示「响应字节到了」，`CUDAMessagingFuture` 额外 synchronize 跨进程 CUDA Event，保证 client 读到的 KV 数据已经落盘到共享显存。

**练习 2**：`CUDAMessagingFuture.set_result` 为什么直接 `raise NotImplementedError`？

> **参考答案**：它的结果只能来源于内部的 `raw_future_`（由消息系统填入 `(event_bytes, value)`），外部不能直接塞结果；禁用 `set_result` 是为了防止误用绕过 CUDA 同步。

---

### 4.3 GPU 零拷贝：CUDA IPC handle 与 DeviceIPCWrapper

#### 4.3.1 概念说明

这是整条数据流里**最反直觉、也最关键**的一点，请先记住设计文档的一句话（[device_ipc_wrapper_design.md](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/docs/design/v1/multiprocess/device_ipc_wrapper_design.md#L5)）：

> 「MP 传输在 `REGISTER_KV_CACHE` 时**一次性**把整块分页 KV buffer 的 IPC handle 发给 server；此后的每次 `STORE`/`RETRIEVE` **只携带分页 block id，从不携带张量**。」

也就是说，几百 MB 的 KV 张量**从来不走 ZMQ**。跨进程传递的只是一个小小的 IPC handle（一串字节）。流程是：

1. **注册阶段（一次性）**：worker 把引擎的每层 KV 张量包装成 `DeviceIPCWrapper`（CUDA 路径下就是 `CudaIPCWrapper`），随 `REGISTER_KV_CACHE` 发给 server。server 用 `to_tensor()` 把 handle 映射成**指向同一块显存**的本地张量，存进上下文。
2. **每次存取**：`STORE`/`RETRIEVE` 只发 `(key, instance_id, gpu_block_ids, event_ipc_handle)`——其中 `gpu_block_ids` 是「在已注册 buffer 里的第几号 block」，server 据此在已映射的本地视图里读写对应 block。完全零拷贝，没有张量过网。

这就是为什么 MP 架构能扛住高频的 store/retrieve 而不被序列化拖垮。

#### 4.3.2 核心流程

```text
worker 进程                                 cache daemon
─────────                                  ────────────
REGISTER_KV_CACHE(                         register_kv_cache(...)
  instance_id,                               ├─ create_cache_context(kv_caches,...)
  kv_cache=[CudaIPCWrapper, ...],  ─ZMQ→    │   内部对每个 wrapper 调 to_tensor()
  model_name, ...                            │   → 得到指向同一显存的本地张量
)                                            └─ 存进 _cache_contexts[instance_id]

STORE(                                      store_handler(...)
  key,                                        ├─ 用 instance_id 取出已映射的本地张量
  instance_id,                                ├─ 用 gpu_block_ids 定位要写的 block
  gpu_block_ids=[[0,1,2]],         ─ZMQ→     ├─ 在 store_stream 上拷贝 block → 落盘
  event_ipc_handle                            └─ 回传 (record 过的 event_ipc_handle, True)

future = CUDAMessagingFuture                 to_cuda_future → synchronize event → 数据就绪
```

`CudaIPCWrapper` 的「发布」用 PyTorch 自带的 `UntypedStorage._share_cuda_()`（适用于 vLLM 这种走 PyTorch caching allocator 的张量）；「重建」用 `UntypedStorage._new_shared_cuda()`。对于不走 PyTorch 分配器的张量（如 TRT-LLM 的 `cudaMalloc` 池），还有兄弟类 `RawCudaIPCWrapper` 直接调 `cudaIpcGetMemHandle`/`cudaIpcOpenMemHandle`，见 [raw_cuda_ipc.md](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/docs/design/v1/multiprocess/raw_cuda_ipc.md)。

#### 4.3.3 源码精读

**协议：REGISTER 一次发 wrapper，STORE/RETRIEVE 只发 block id。**

看协议定义就能验证上面的论断。`REGISTER_KV_CACHE` 的第二个 payload 是 `KVCache`（即 `list[DeviceIPCWrapper]`）——[engine.py:L103](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/protocols/engine.py#L103)；而 `STORE`（[engine.py:L133](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/protocols/engine.py#L133)）的 payload 是 `[KeyType, int, list[list[int]], bytes]`——分别是**键、实例号、block id 列表、CUDA event handle**，**没有任何张量**。`RETRIEVE`（[engine.py:L148](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/protocols/engine.py#L148)）同理。

而 [`KVCache = list[DeviceIPCWrapper]`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/custom_types.py#L120) 这个类型别名就是「一坨 IPC wrapper」。

**wrapper 如何「发布」显存。**

[`CudaIPCWrapper.__init__`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/platform/cuda/ipc_wrapper.py#L51-L72) 先把可能的非连续视图 permute 成连续的（保证编码进 handle 的 shape/stride 反映物理布局），然后取 `untyped_storage()`，调用 `_share_cuda_()` 拿到 handle，再把 `dtype/shape/stride/storage_offset/device_uuid` 一起记下来。`device_uuid` 是为了在 server 进程里把「UUID」还原成「本进程的 GPU 序号」。

**server 端如何「重建」成张量。**

[`CudaIPCWrapper.to_tensor`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/platform/cuda/ipc_wrapper.py#L74-L91) 用 `_new_shared_cuda(device_index, *handle[1:])` 在 server 进程映射出指向**同一块物理显存**的 storage，再用 `set_(storage, storage_offset, shape, stride)` 恢复出逻辑视图。注意注释提醒：调用前要先 `torch_dev.init()` 初始化加速器（server.py 启动时会做，见 [server.py:L398-L404](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/server.py#L398-L404)）。

**wrapper 如何穿过 ZMQ：pickle + 单一 ext code。**

`DeviceIPCWrapper` 不是普通 msgspec 结构，它走「定制编解码」：[`Serialize`/`Deserialize`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/platform/base_ipc_wrapper.py#L132-L160) 用 `pickle.dumps`/`pickle.loads`，从而**保留具体子类身份**。mq.py 的 [`_SPECIAL_ENCODER_DECODERS`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/mq.py#L66-L79) 把它注册成 msgspec 的 **ext code 1**（编码逻辑见 [custom_types.py 的 get_customized_encoder](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/custom_types.py#L221-L234)）。设计上的妙处在于：所有设备后端（CUDA / RawCUDA / CPU-SHM / MUSA）共享**同一个 ext code、同一个 `list[DeviceIPCWrapper]` 线类型**，server 端 `to_tensor()` 会按反序列化出的具体子类自动分派，无需 if/else 分支（详见 [device_ipc_wrapper_design.md 的 dispatch 小节](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/docs/design/v1/multiprocess/device_ipc_wrapper_design.md#L42-L46)）。

**server 端：注册时调用 `to_tensor()`。**

[`register_kv_cache`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/modules/lmcache_driven_transfer.py#L816-L825) 把收到的 `kv_caches: KVCache`（一组 wrapper）交给 `create_cache_context(...)`——后者内部会对每个 wrapper调 `to_tensor()` 完成显存映射，再存进 `_cache_contexts[instance_id]`。注意它对**重复注册**做了幂等处理（已注册的 instance 只刷新 `last_seen`），这是 worker 崩溃恢复时不会误删缓存的关键。

#### 4.3.4 代码实践

> **目标**：在有 GPU 的机器上验证「同一块显存、两个进程、零拷贝」。

[`test_mq_register_kv_cache`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/tests/v1/multiprocess/test_mq.py#L361-L399) 正是这个场景：在 client 进程里 `torch.randn(..., device="cuda")` 造 3 个张量，分别包成 `CudaIPCWrapper`，通过 `REGISTER_KV_CACHE` 跨进程发给 server 进程，server handler 收到后即可对每个 wrapper 调 `to_tensor()` 拿到指向同一显存的本地张量。

操作步骤：

1. 读该测试和它依赖的 `tests/v1/multiprocess/test_mq_handler_helpers.py`（看 `register_kv_cache_handler` 如何处理收到的 `kv_cache`）。
2. 在 handler 里给每个 wrapper 调 `.to_tensor()` 后，打印 `t.data_ptr()` 与原始张量的关系（注意是不同进程，地址不同但物理显存相同）。
3. 跑 `python -m pytest tests/v1/multiprocess/test_mq.py::test_mq_register_kv_cache -x -s`。

需要观察的现象：server 进程里 `to_tensor()` 成功返回 CUDA 张量，且能读到 client 写入的值（可在 client 写入特定值、server 读出比对）。

预期结果：测试通过（标记 `skipif not torch.cuda.is_available()`，无 GPU 环境会被跳过）。**无 GPU 时此步为「待本地验证」。**

> 源码阅读型替代实践（无需 GPU、无需运行）：跟踪「IPC handle 在哪一步跨进程传递」——
> 1. [engine.py:L103](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/protocols/engine.py#L103) `REGISTER_KV_CACHE` 声明 payload 含 `KVCache`（wrapper 列表）；
> 2. [mq.py:L66-L79](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/mq.py#L66-L79) `_SPECIAL_ENCODER_DECODERS` 把 wrapper 用 pickle + ext code 1 编进 msgpack 帧；
> 3. [lmcache_driven_transfer.py:L816](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/modules/lmcache_driven_transfer.py#L816) server 端 `register_kv_cache` 收到 wrapper 列表；
> 4. [cuda/ipc_wrapper.py:L74](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/platform/cuda/ipc_wrapper.py#L74) `to_tensor()` 用 `_new_shared_cuda` 完成跨进程显存映射。
>
> 结论：**真正的 GPU 张量从不进 ZMQ；跨进程传递的始终是 IPC handle（一串字节），张量是在 server 进程里被「重新映射」出来的。**

#### 4.3.5 小练习与答案

**练习 1**：既然 `STORE` 高频发生，为什么不在每次 `STORE` 里重发一次 KV 张量的 IPC handle？

> **参考答案**：每次发 handle 等于每次序列化/反序列化大对象且要重复建立 IPC 映射，开销巨大。把「发布 handle」一次性做在 `REGISTER_KV_CACHE`，之后 `STORE`/`RETRIEVE` 只发轻量的 block id，把高频路径压到最低成本。

**练习 2**：为什么 `CudaIPCWrapper` 用 `pickle` 而不是 msgspec 来序列化？

> **参考答案**：msgspec 不支持「自定义 ext 编码类型的并集」。让 CUDA/RawCUDA/CPU-SHM/MUSA 四种 wrapper 共享同一个 ext code 1、以 `list[DeviceIPCWrapper]` 一种线类型传输，靠 pickle 保留具体子类身份，server 端 `to_tensor()` 才能按真实子类自动分派。

---

### 4.4 CPU 零拷贝：POSIX 共享内存 posix_shm.py

#### 4.4.1 概念说明

当 worker 是**非 GPU** 路径（engine-driven transfer，例如某些 CPU 侧 KV 布局）时，没有 CUDA IPC 可用，就走 POSIX 共享内存（`/dev/shm`）。`posix_shm.py` 是一层非常薄的封装，把 `shm_open` + `mmap` 包成四个易用函数：

- `shm_create_readwrite(name, nbytes)`：创建一段共享内存并映射，返回基地址。
- `shm_map_readwrite(name, nbytes)`：打开**已存在**的同名段并映射（另一进程）。
- `shm_munmap(addr)`：解除映射（best-effort）。
- `shm_unlink(name)`：删除段（幂等）。
- `shm_open_pool_as_mmap(name, nbytes)`：以独立 `mmap` 对象打开，供 `torch.frombuffer` 消费。

两个进程用**同一个名字**打开同一段共享内存，就得到了同一块物理内存的两个虚拟地址，写一边另一边立刻可见——这就是 CPU 版的零拷贝。

#### 4.4.2 核心流程

```text
进程 A (创建者)                          进程 B (打开者)
─────────────                           ─────────────
addr = shm_create_readwrite("/foo", N)   addr2 = shm_map_readwrite("/foo", N)
  ↓ 写入数据到 [addr, addr+N)              ↓ 可读到同样的数据（同一物理内存）
shm_munmap(addr)                        shm_munmap(addr2)
shm_unlink("/foo")                      # 创建者负责 unlink
```

关键实现取舍（见 [posix_shm.py 模块文档](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/posix_shm.py#L1-L29)）：

- 故意绕过 `multiprocessing.shared_memory.SharedMemory` 高层封装，直接用 CPython 内置的 `_posixshmem` C 扩展——因为高层封装在 `__del__` 时 `close()` 一个「还有导出缓冲」的 mmap 会抛 `BufferError`。
- 每个 addr 都在进程内注册表 `_ADDR_TO_MMAP` 里记回它对应的 mmap 对象，保证 `shm_munmap` 恰好 `close()` 一次，不泄漏页。
- `atexit` 钩子在进程退出时把所有「自己创建」的段 munmap + unlink，避免 `/dev/shm` 残留。

#### 4.4.3 源码精读

**创建/打开共享内存段。**

[`_open_and_mmap`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/posix_shm.py#L73-L103) 是统一入口：用 `_posixshmem.shm_open` 打开（`create=True` 时带 `O_CREAT|O_EXCL` 防覆盖、`ftruncate` 设大小），再 `mmap` 映射，**fd 在返回前就 close**（映射靠内核保留，不依赖 fd）。失败时如果是创建路径，会 `shm_unlink` 清理，避免半成品残留。

[`shm_create_readwrite`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/posix_shm.py#L122-L145) 和 [`shm_map_readwrite`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/posix_shm.py#L148-L168) 分别对应 create=True/False，都把 `(addr → mmap)` 记进注册表；前者还会把名字记进 `_OWNED_NAMES`（表示「这段是我建的，退出时要负责 unlink」）。

**用独立 mmap 打开（供 torch 消费）。**

[`shm_open_pool_as_mmap`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/posix_shm.py#L250-L273) 返回一个独立的 `mmap.mmap` 对象，调用方（非 GPU 的 SHM transport）用 `torch.frombuffer(mmap_obj, ...)` 直接在共享内存上造张量，零拷贝。

**释放与退出清理。**

[`shm_munmap`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/posix_shm.py#L171-L199) 从注册表 `pop` 出 mmap 并 `close()`，对「还有导出视图」（比如张量还占着这块内存）的 `BufferError` 当 best-effort 忽略。[`_atexit_cleanup`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/posix_shm.py#L228-L244) 在进程退出时统一 munmap 所有映射、unlink 所有自己创建的段。

#### 4.4.4 代码实践

> **目标**：在同一进程内用两个独立的 mmap 视图，验证共享内存的「写一边、另一边可见」。不需要 GPU。

照搬 [`test_open_pool_as_mmap_zero_copy_view`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/tests/v1/multiprocess/test_posix_shm.py#L56-L73) 即可：

```python
# 示例代码：POSIX SHM 零拷贝验证（仿 test_posix_shm.py，非项目原文件）
from lmcache.v1.multiprocess.posix_shm import (
    shm_create_readwrite, shm_open_pool_as_mmap, shm_munmap, shm_unlink,
)
name = f"/lmc_demo_{__import__('os').getpid()}"
nbytes = 4096
addr = shm_create_readwrite(name, nbytes)
try:
    mm = shm_open_pool_as_mmap(name, nbytes)
    mm2 = shm_open_pool_as_mmap(name, nbytes)
    mm[0:4] = b"\x01\x02\x03\x04"
    print("另一个视图读到:", bytes(mm2[0:4]))   # 期望 b'\x01\x02\x03\x04'
    mm.close(); mm2.close()
finally:
    shm_munmap(addr, nbytes)
    shm_unlink(name)
```

操作步骤：直接 `python demo.py`（仅 Linux/macOS，依赖 `_posixshmem`，标准库自带）。

需要观察的现象：往 `mm` 写入 4 字节后，**没做任何拷贝**，`mm2` 立刻能读到同样的字节——证明它们映射的是同一块物理内存。

预期结果：打印 `另一个视图读到: b'\x01\x02\x03\x04'`。`shm_munmap(0)` 这种空地址调用应安全 no-op（见 [`test_munmap_no_op_on_zero_addr`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/tests/v1/multiprocess/test_posix_shm.py#L76-L78)）。

> 也可以直接跑 `python -m pytest tests/v1/multiprocess/test_posix_shm.py -s` 验证全部 roundtrip。

#### 4.4.5 小练习与答案

**练习 1**：`shm_create_readwrite` 第二次用同名调用会发生什么？为什么？

> **参考答案**：抛 `OSError`（`FileExistsError`）。因为它带 `O_CREAT|O_EXCL`，专门用来防止覆盖别人已建的段。[`test_create_excl_collision`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/tests/v1/multiprocess/test_posix_shm.py#L45-L53) 正好验证这点。

**练习 2**：为什么 posix_shm.py 不直接用 `multiprocessing.shared_memory.SharedMemory`？

> **参考答案**：高层封装内部对自己的 mmap 持有 memoryview，一旦调用方又导出了缓冲（如 `torch.frombuffer`），`SharedMemory.close()` 在 `__del__` 时会抛 `BufferError: cannot close exported pointers exist`。自己持有 mmap 并配对显式 `shm_munmap`，才能让退出干净。

---

### 4.5 综合时序：把四者串起来（含 IPC handle 的传递点）

把上面四个模块合在一起，一条 GPU 路径的 RETRIEVE 完整时序如下（**★** 标出 IPC handle 跨进程传递的关键步骤）：

```text
[1] 一次性注册（worker 启动时）
    worker: kv_tensors → CudaIPCWrapper(...)                # 发布显存成 handle
    worker: submit_request(REGISTER_KV_CACHE, [id, wrappers, ...])
★   → ZMQ 把 wrappers 用 pickle+ext1 编码过网（handle 字节跨进程）
    daemon: register_kv_cache → 对每个 wrapper.to_tensor()  # 映射同一显存
            存进 _cache_contexts[id]

[2] 每次 RETRIEVE
    worker: submit_request(RETRIEVE, [key, id, gpu_block_ids, event_handle])
      → 仅 block id + 一个 event IPC handle 过网，张量不过网 ★
    worker: 立即拿到 MessagingFuture，可继续干别的
    daemon(亲和线程): 用 id 取已映射张量，按 block ids 在 load_stream 拷回
            record 一个新 CUDA Event → 回传 (event_ipc_handle, True) ★
    daemon: 结果入 output_queue → 主循环 send_multipart 回写
    worker: ClientPollingLoop 收到 → future.set_result((event_bytes, True))
    worker: future.to_cuda_future().result()
            → Event.from_ipc_handle 重建跨进程 Event → synchronize ★
            → 此时 GPU 数据已就绪，读取本地 block
```

核心结论一句话：**控制流（uid/type/key/block id）走 ZMQ 消息队列，结果用 Future 异步回收；GPU 张量始终零拷贝——CUDA 路径靠 IPC handle 在 [1] 注册时一次性跨进程映射，CPU 路径靠 POSIX 共享内存按名字映射。**

## 5. 综合实践

> **任务**：用一张时序图把「IPC handle 在哪几步跨进程传递」讲清楚，并标注每条 ZMQ 帧里**到底装了什么**。

具体步骤：

1. 打开 [`mq.py`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/mq.py) 与 [`protocols/engine.py`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/protocols/engine.py)，对 `REGISTER_KV_CACHE`、`STORE`、`RETRIEVE` 三种请求，分别列出：
   - multipart 帧序列（`[identity, uid, type, *payloads]`）；
   - 每个 payload 的类型（特别注意哪些是 `DeviceIPCWrapper`、哪些是 `list[list[int]]`、哪些是 `bytes` 即 event handle）；
   - handler 是 SYNC 还是 BLOCKING，跑在哪种线程池。
2. 在图上标出三个 ★ 点：(a) `REGISTER_KV_CACHE` 时 wrapper 过网；(b) `STORE/RETRIEVE` 时 event IPC handle 过网；(c) server 端 `to_tensor()` 与 client 端 `Event.from_ipc_handle` 的重建位置。
3. 写一段话回答：**为什么把 wrapper 的传输放在注册阶段、而不是每次存取？如果改成每次 RETRIEVE 都重传 wrapper，会有什么后果？**

预期产出：一张含 worker/daemon 两条泳道的时序图 + 上面三问的书面回答。这道题若能在不看答案的情况下讲清楚，说明你已经掌握了本讲的传输管道全貌。

## 6. 本讲小结

- **mq.py** 用 ZMQ `ROUTER`(server)/`DEALER`(client) 搭出一条「multipart 帧 + `request_uid` 配对」的 RPC 通道；收发全在后台轮询线程，调用方只拿 Future。
- handler 分 **SYNC**（主循环直接跑）与 **BLOCKING**（丢线程池）；BLOCKING 又分**普通池**（CPU 簿记）和**亲和池**（GPU 搬运，同实例落同线程免加锁）。
- **futures.py** 的 `MessagingFuture` 是「事件+结果」的纯 CPU 等待；`CUDAMessagingFuture` 额外 synchronize 一个**跨进程 CUDA Event**，把「字节到了」升级为「GPU 搬完了」。
- **KV 张量从不过网**：`REGISTER_KV_CACHE` 一次性把 `DeviceIPCWrapper`（IPC handle）发过去，server 用 `to_tensor()` 映射同一显存；此后 `STORE/RETRIEVE` 只发 block id + event handle。
- 所有设备后端（CUDA/RawCUDA/CPU-SHM/MUSA）共享**一个 msgspec ext code、一种 `list[DeviceIPCWrapper]` 线类型**，靠 pickle 保留子类身份，server 端零分支自动分派。
- **posix_shm.py** 提供 CPU 端的零拷贝：两个进程按同名段 `shm_open`+`mmap` 同一块物理内存；自管 mmap 生命周期以避开 `SharedMemory` 的 `BufferError`。

## 7. 下一步学习建议

- 下一篇 **u3-l3 MP Coordinator：跨实例协调** 会从「单实例 daemon」上升到「舰队级协调」，讲 [`mp_coordinator/`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/app.py) 的 peer 发现与 blend lookup，注意它用的是 **HTTP/REST** 而非本讲的 ZMQ。
- 想深挖 GPU 搬运细节，可读 [`modules/lmcache_driven_transfer.py`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/modules/lmcache_driven_transfer.py) 里 `STORE/RETRIEVE` handler 如何用 `store_stream/load_stream` 与 block id 完成实际拷贝。
- 想了解协议如何扩展新命令，预习 **u3-l4 HTTP API 与通信协议**，对照本讲的 `RequestType`/`ProtocolDefinition` 理解 SYNC/BLOCKING 的注册机制。
- 对共享内存的更多用法（CPU KV wrapper）感兴趣，可读 [`platform/cpu/shm.py`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/platform/cpu/shm.py) 与设计文档 [`device_ipc_wrapper_design.md`](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/docs/design/v1/multiprocess/device_ipc_wrapper_design.md)。
