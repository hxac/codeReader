# 进程拓扑：三大管理器与 IPC 概览

## 1. 本讲目标

本讲是第 3 单元（服务端架构与请求生命周期）的第二篇，承接 [u3-l1 服务启动全流程](u3-l1-server-launch-flow.md)。上一讲我们追完了 `sglang serve` 从命令行到 HTTP 就绪的启动链，并提到「默认形态由 `Engine._launch_subprocesses` 按 Scheduler 子进程 → Detokenizer 子进程 → 主进程 TokenizerManager 顺序组装引擎」。本讲就把这句话彻底拆开。

学完后你应当能够：

- 准确说出 SGLang 运行时由哪几个进程组成、每个进程里运行的是哪个类、它们之间是什么父子关系。
- 画出一条请求的 token 在三大管理器之间的完整流转路径（Tokenizer → Scheduler → GPU → Detokenizer → Tokenizer）。
- 看懂进程之间是用哪些 ZMQ socket、哪几条 IPC 通道、用什么序列化格式通信的。
- 理解 `Engine._launch_subprocesses` 是如何「编排」这几个进程、并用 watchdog 守护它们的。

## 2. 前置知识

在进入源码前，先用三个比喻建立直觉。

**多进程而不是多线程。** LLM 推理有两类截然不同的工作：一类是「CPU 上的请求接入、token 化、调度、解码字符串」——这类工作是 I/O 密集、用 Python `asyncio` 就能高效处理；另一类是「GPU 上的模型前向计算」——这类工作需要死死霸占 GPU、用 CUDA stream 控制时序。如果把它们塞进同一个进程的同一个事件循环里，CPU 调度逻辑会被 GPU kernel 阻塞，GPU 又会被 Python 解释器的 GIL 拖慢。SGLang 的解法是**把它们放进不同进程**：CPU 侧的管理器各自跑自己的事件循环，GPU 侧的调度器独占一个进程专心做推理。进程之间用 ZMQ（ZeroMQ）传消息，彼此不共享内存、不抢 GIL。

**三大管理器各司其职。**

- `TokenizerManager`：主进程里。它是「前台接待」——接 HTTP/Engine 请求，把文本切成 token id，给每个请求发一个号牌（request id），然后把请求往后厨（Scheduler）送；同时它也是「传菜员」——把后厨做好的结果端回给等候的调用者。
- `Scheduler`：子进程里。它是「后厨主厨」——决定哪些请求凑成一个 batch、什么时候送进 GPU（前向计算）、GPU 算完怎么回收结果。它持有 `TpModelWorker` / `ModelRunner`，是真正碰 GPU 的角色。
- `DetokenizerManager`：子进程里。它是「摆盘员」——把 GPU 产出的 token id 序列翻译回人类可读的文字（detokenize），并且只翻译新增的部分（增量解码），再把文本结果送回前台。

**进程间通信（IPC）。** 三个进程不共享内存，靠 ZMQ 的 socket 传消息。ZMQ 在 socket 之上提供了「PUSH/PULL」这种模式：PUSH 端发送、PULL 端接收，天然适合单向的生产者-消费者管道。SGLang 给每对进程分配了一条命名的 IPC 通道（一个 `ipc://` 或 `tcp://` 地址），消息体用 `msgspec` 序列化成 msgpack 二进制（高效且跨进程类型安全）。

> 术语：**GIL**（全局解释器锁）——CPython 同一进程里同一时刻只有一个线程执行 Python 字节码，所以多线程无法真正并行 Python 代码；多进程才能绕开。**ZMQ**——一个高性能异步消息库，socket 模式包括 PUSH/PULL（单向管道）、PUB/SUB、REQ/REP、DEALER 等。**msgpack**——一种二进制序列化格式，比 JSON 更小更快。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [srt/entrypoints/engine.py](../srt/entrypoints/engine.py) | `Engine` 类；`_launch_subprocesses` 是编排三大进程的总入口。 |
| [srt/managers/tokenizer_manager.py](../srt/managers/tokenizer_manager.py) | 主进程的 `TokenizerManager`：接入请求、token 化、收发消息。 |
| [srt/managers/scheduler.py](../srt/managers/scheduler.py) | 子进程的 `Scheduler`：事件循环、组 batch、调度 GPU 前向。 |
| [srt/managers/detokenizer_manager.py](../srt/managers/detokenizer_manager.py) | 子进程的 `DetokenizerManager`：增量解码、回传文本。 |
| [srt/managers/scheduler_components/ipc_channels.py](../srt/managers/scheduler_components/ipc_channels.py) | `SchedulerIpcChannels`：Scheduler 侧的 ZMQ socket 集合。 |
| [srt/managers/io_struct.py](../srt/managers/io_struct.py) | 跨进程消息的定义（`BaseReq`/`BaseBatchReq` 等）与 `sock_send/sock_recv`。 |
| [srt/server_args.py](../srt/server_args.py) | `PortArgs`：所有 IPC 通道名字的集中存放点。 |

## 4. 核心概念与源码讲解

### 4.1 进程拓扑与 IPC 主干

#### 4.1.1 概念说明

先看一张「物理视图」——SGLang 默认（单机、`dp_size=1`、`tokenizer_worker_num=1`、`detokenizer_worker_num=1`）运行时，到底有几个进程：

```
        主进程 (node_rank 0)
   ┌───────────────────────────────────┐
   │  HTTP / FastAPI   或   Engine API │
   │  TokenizerManager (前台接待)       │   ← 接请求 / 发请求 / 收结果
   └───────────────┬───────────────────┘
                   │ fork (multiprocessing.Process)
        ┌──────────┴──────────┐
        ▼                     ▼
 ┌──────────────┐      ┌───────────────────┐
 │ Scheduler    │      │ DetokenizerManager│
 │ sglang::     │      │  sglang::         │
 │ scheduler    │      │  detokenizer      │
 │ (持有 GPU)   │      │  (持有 tokenizer) │
 └──────────────┘      └───────────────────┘
   子进程 1                子进程 2
```

要点：

1. **TokenizerManager 在主进程内**——它不是一个单独 fork 的进程，而是直接在拉起 HTTP/Engine 的那个主进程里被实例化的「对象」。这就是为什么它既能拿到 FastAPI 的请求对象，又能直接把结果异步返回给调用者。
2. **Scheduler 与 DetokenizerManager 各自是 `multiprocessing.Process` 子进程**——通过 `mp.Process(...)` 启动，进程名（`setproctitle`）分别被设成 `sglang::scheduler` 和 `sglang::detokenizer`，方便你用 `ps` 一眼分辨。
3. **三个进程靠 ZMQ 通信**，形成一条**单向环**：Tokenizer → Scheduler → Detokenizer → Tokenizer。

通信主干（谁 bind、谁 connect 见 4.1.2）：

```
        TokenizerManager (主进程)
   PUSH ──────────────────►  scheduler_input_ipc_name  ──────────► PULL  Scheduler
   PULL ◄──────────────────  tokenizer_ipc_name         ◄────────── PUSH  Detokenizer

        Scheduler
   PUSH ──────────────────►  detokenizer_ipc_name       ──────────► PULL  Detokenizer
```

也就是说，三个进程之间其实只有三条逻辑通道：**Tokenizer→Scheduler（送请求）**、**Scheduler→Detokenizer（送 token 结果）**、**Detokenizer→Tokenizer（送文本结果）**。这三条通道的名字全部集中定义在一个数据类里。

#### 4.1.2 核心流程

这三条通道的名字存放在 [`PortArgs`](../srt/server_args.py) 里，由主进程在启动时一次性生成（`ipc://` 是 Unix domain socket 文件路径，`tcp://` 是 TCP 地址）。关键三行：

- `tokenizer_ipc_name`：TokenizerManager **接收**（PULL）来自 Detokenizer 的结果。
- `scheduler_input_ipc_name`：Scheduler（rank 0）**接收**（PULL）来自 Tokenizer 的请求。
- `detokenizer_ipc_name`：DetokenizerManager **接收**（PULL）来自 Scheduler 的 token 结果。

ZMQ 里 PUSH/PULL 的 `bind`（绑定端，相当于服务端）与 `connect`（连接端，相当于客户端）配对规则：**一方 bind，另一方 connect**。SGLang 的约定是——**主进程和 Detokenizer 进程各自 bind 自己「接收」的那条 socket，而 Scheduler 子进程统一 connect 全部 socket**。原因很自然：`PortArgs.init_new` 在主进程里先生成好通道名字，Scheduler 作为后启动的子进程只需 connect 到既定地址即可，不必关心谁先启动。

用伪代码表示 ZMQ 拓扑：

```
# 通道 A：scheduler_input_ipc_name
TokenizerManager:  PUSH, bind=True   ──►  Scheduler:  PULL, connect

# 通道 B：detokenizer_ipc_name
Scheduler:  PUSH, connect   ──►  DetokenizerManager:  PULL, bind=True

# 通道 C：tokenizer_ipc_name
DetokenizerManager:  PUSH, connect   ──►  TokenizerManager:  PULL, bind=True
（skip_tokenizer_init 时，Scheduler 也 connect 到这条通道直接把结果送回前台）
```

进程之间传的不是任意 Python 对象，而是定义在 [`io_struct.py`](../srt/managers/io_struct.py) 里的消息结构。所有消息都继承自两个基类：

- `BaseReq`：单个请求的载荷（带 `rid` 请求号），如 `TokenizedGenerateReqInput`。
- `BaseBatchReq`：批量的载荷（带 `rids` 列表），如 `BatchStrOutput`（一批文本结果）。

它们都是 `msgspec.Struct`（带 `tag=True`，解码时能自动识别子类型），并通过 `sock_send/sock_recv` 用 msgpack 序列化后塞进 ZMQ 帧。

#### 4.1.3 源码精读

通道名字集中定义在 [`PortArgs`](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/server_args.py#L8726-L8732)：

[server_args.py:L8726-L8732](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/server_args.py#L8726-L8732) —— 三条核心 IPC 通道的字段定义，注释写清了每条通道的「发送方 → 接收方」方向。

单机模式下，[`PortArgs.init_new`](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/server_args.py#L8798-L8800) 用临时文件名生成三条 `ipc://` 地址（多机时改用 `tcp://`）：

[server_args.py:L8798-L8800](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/server_args.py#L8798-L8800) —— 三条通道地址的具体生成。

消息基类定义在 [`io_struct.py`](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/managers/io_struct.py#L74-L96)：

[io_struct.py:L74-L82](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/managers/io_struct.py#L74-L82) —— `BaseReq`：单请求 IPC 载荷基类，带 `rid`（请求唯一号牌）和 `http_worker_ipc`（多 worker 路由用）。

[io_struct.py:L85-L96](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/managers/io_struct.py#L85-L96) —— `BaseBatchReq`：批量 IPC 载荷基类，带 `rids` 列表。

序列化与收发逻辑在 [`sock_send/sock_recv`](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/managers/io_struct.py#L2253-L2266)：

[io_struct.py:L2253-L2266](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/managers/io_struct.py#L2253-L2266) —— `sock_send` 用 `msgpack_encode` 把消息编成二进制再 `socket.send`；`sock_recv` 反过来。注意有个 `_USE_PICKLE_IPC` 兜底分支（调试/兼容时回退到 pickle）。

> 顺带一提：对于那些「没法用 msgspec 类型描述」的字段（如多模态输入、计时对象），`io_struct.py` 提供了 `PickleWrapper` 把它们先 pickle 成字节再塞进 msgpack——这就是 `wrap_pickle_fields` / `unwrap_from_pickle` 的由来（在 4.3 会看到它被调用）。

#### 4.1.4 代码实践

**目标**：在源码层面确认三条 IPC 通道的「bind/connect」归属，填出一张拓扑表。

**操作步骤**：

1. 打开 `tokenizer_manager.py` 的 `init_ipc_channels`，看 `recv_from_detokenizer` 和 `send_to_scheduler` 的第三个参数（`bind`）。
2. 打开 `detokenizer_manager.py` 的 `init_ipc_channels`，看 `recv_from_scheduler` 和 `send_to_tokenizer` 的 `bind`。
3. 打开 `scheduler_components/ipc_channels.py` 的 `SchedulerIpcChannels.create`，看 Scheduler 侧四个 socket 的 `bind`。

**需要观察的现象**：三个文件里 `get_zmq_socket(..., True/False)` 的第三个布尔值。

**预期结果**（这张表你应当能自己填出来）：

| 通道 | TokenizerManager | Scheduler | DetokenizerManager |
| --- | --- | --- | --- |
| `scheduler_input_ipc_name` | PUSH **bind** | PULL connect | — |
| `detokenizer_ipc_name` | — | PUSH connect | PULL **bind** |
| `tokenizer_ipc_name` | PULL **bind** | PUSH connect（仅 skip_tokenizer_init） | PUSH connect |

#### 4.1.5 小练习与答案

**练习 1**：为什么 Scheduler 侧的 socket 几乎全是 `connect` 而不是 `bind`？

**答案**：因为 `PortArgs` 在主进程里先生成通道地址，主进程（TokenizerManager）和 Detokenizer 进程分别 bind 自己「接收」的 socket；Scheduler 是后启动的子进程，只需 connect 到已确定的地址即可，且 connect 一侧在 bind 一侧未就绪时也能重试，避免启动时序竞争。

**练习 2**：`BaseReq` 和 `BaseBatchReq` 的区别是什么？为什么需要两个？

**答案**：`BaseReq` 描述单个请求（带 `rid`），`BaseBatchReq` 描述一批结果（带 `rids` 列表）。请求是「一条一条」从前台送进来的，所以入站用单请求；但 GPU 每次前向产出的是「一整批」token 结果，所以出站用批量结构，减少消息条数。

---

### 4.2 Engine._launch_subprocesses：进程编排

#### 4.2.1 概念说明

`_launch_subprocesses` 是一个 `@classmethod`，它是「总装配线」：给定 `server_args`，它负责把 TokenizerManager（主进程对象）、Scheduler（子进程）、DetokenizerManager（子进程）按正确顺序拉起来，等模型加载完毕，再装一个 `SubprocessWatchdog`（子进程存活看门狗）盯着它们。它的 docstring 一句话点明了分工：

> Launch the TokenizerManager in the main process, the Scheduler in a subprocess, and the DetokenizerManager in another subprocess.

理解它的关键，是看它「先拉谁、后拉谁、等谁就绪」。

#### 4.2.2 核心流程

装配顺序（伪代码）：

```
_launch_subprocesses(server_args, ...):
    1. 配置环境/日志/插件/GC，生成 PortArgs（三条 IPC 通道地址）
    2. _launch_scheduler_processes(...)   # fork 出 1 个或多个 Scheduler 子进程
    3. _launch_detokenizer_subprocesses(...)  # fork 出 Detokenizer 子进程
    4. init_tokenizer_manager_func(...)   # 在【主进程内】直接构造 TokenizerManager 对象
    5. scheduler_init_result.wait_for_ready()  # 阻塞，等 Scheduler 把模型加载完
    6. 把 scheduler 的 max_req_input_len 回填给 tokenizer_manager
    7. 启动 SubprocessWatchdog 盯住所有子进程
    return (tokenizer_manager, ...)
```

为什么是「Scheduler 先、Tokenizer 后」？因为 Scheduler 要加载模型权重、建 KV 缓存池，这步最慢；尽早 fork 它、让它后台加载，期间主进程正好可以并行去拉 Detokenizer 和构造 TokenizerManager。最后一步 `wait_for_ready()` 才会真正阻塞，等 Scheduler 通过 pipe 报告「我准备好了」。

Scheduler 子进程数量取决于并行度：默认 `use_dp_controller=False` 时，按 `pp_rank × tp_rank` 笛卡尔积，**每个 (pp_rank, tp_rank) 组合 fork 一个 Scheduler 进程**（张量并行 / 流水线并行的 worker 就这么来的）；当 `dp_size > 1` 时，则只 fork 一个 `DataParallelController` 进程，由它再去管下面的 worker（这部分留到第 7 单元展开）。

#### 4.2.3 源码精读

总入口 [`_launch_subprocesses`](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/entrypoints/engine.py#L765-L783)，docstring 点明三方归属：

[engine.py:L765-L783](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/entrypoints/engine.py#L765-L783) —— 总装配线的方法签名与说明。

装配主体的四步，[engine.py:L823-L901](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/entrypoints/engine.py#L823-L901)：

- [engine.py:L823-L829](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/entrypoints/engine.py#L823-L829) —— 步骤 2：拉起 Scheduler 子进程（`_launch_scheduler_processes`）。
- [engine.py:L864-L872](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/entrypoints/engine.py#L864-L872) —— 步骤 3：拉起 Detokenizer 子进程，并把它的 pid 收集进 `all_child_pids`。
- [engine.py:L874-L882](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/entrypoints/engine.py#L874-L882) —— 步骤 4：**在主进程内**构造 `TokenizerManager`（注意它不是 fork 出来的进程，而是直接 `init_tokenizer_manager_func(...)`）。
- [engine.py:L884-L890](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/entrypoints/engine.py#L884-L890) —— 步骤 5：阻塞等待 Scheduler 加载完模型，并把 `max_req_input_len` 回填给 TokenizerManager。

Scheduler 子进程是怎么 fork 的？看 [`_launch_scheduler_processes`](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/entrypoints/engine.py#L626-L661)：

[engine.py:L639-L661](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/entrypoints/engine.py#L639-L661) —— 在 `pp_rank × tp_rank` 双层循环里，对每个 GPU 位置 `mp.Process(target=run_scheduler_process_func, ...)` 启动一个 Scheduler 进程，并通过 `mp.Pipe` 把它「准备好」的消息传回主进程。

Detokenizer 子进程的 fork 在 [`_launch_detokenizer_subprocesses`](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/entrypoints/engine.py#L707-L735)：

[engine.py:L727-L735](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/entrypoints/engine.py#L727-L735) —— 默认 `detokenizer_worker_num <= 1` 时，启动**单个** `run_detokenizer_process` 子进程。

最后，看门狗把所有子进程登记起来，[engine.py:L892-L901](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/entrypoints/engine.py#L892-L901) —— 把 Scheduler 进程（命名 `scheduler_i`）和 Detokenizer 进程（命名 `detokenizer`）一起交给 `SubprocessWatchdog`，任一子进程意外退出时它会触发关闭。

`Engine.__init__` 是这个 classmethod 的唯一调用方，[engine.py:L234-L252](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/entrypoints/engine.py#L234-L252) —— 把返回的 `tokenizer_manager` 挂到 `self.tokenizer_manager`，并把 watchdog 反向挂回 tokenizer_manager，形成「TokenizerManager 能感知子进程死活」的回路。

#### 4.2.4 代码实践

**目标**：跟踪「谁被 fork、谁被 new」的时序，画出启动甘特图。

**操作步骤**：

1. 在 `engine.py` 的 `_launch_subprocesses` 里，给第 2 步（`_launch_scheduler_processes`）、第 3 步（`_launch_detokenizer_subprocesses`）、第 4 步（`init_tokenizer_manager_func`）、第 5 步（`wait_for_ready`）各加一行 `logger.info(f"[TOPO] step N ...")`。
2. 用任意可用小模型启动一次服务（命令见本讲综合实践）。
3. 观察启动日志里四条 `[TOPO]` 的先后顺序与时间间隔。

**需要观察的现象**：第 2 步发出后，到第 5 步 `wait_for_ready` 返回之间，会有一段较长的等待（模型加载）；期间第 3、4 步会很快打印出来。

**预期结果**：你能得到一条「Scheduler fork（早）→ 模型加载（慢，后台）→ Detokenizer fork + TokenizerManager 构造（快）→ wait_for_ready 阻塞返回（最晚）」的时序。注意第 4 步是在**主进程**执行的——它的存在证明了 TokenizerManager 不是子进程。

> 这是「源码阅读 + 日志增强型实践」，命令的实际运行结果依赖你的本地 GPU 环境，如未运行请标注为「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：如果 `wait_for_ready()` 永远不返回，最可能是哪个进程出了问题？

**答案**：Scheduler 子进程。`wait_for_ready` 等的是 Scheduler 通过 `mp.Pipe` 回报的「模型加载完成」消息（见 `_launch_scheduler_processes` 里的 `wait_for_ready` 与 `scheduler_pipe_readers`）。Scheduler 没准备好通常意味着模型加载失败或卡死。

**练习 2**：为什么 TokenizerManager 不像另外两个那样用 `mp.Process` 启动？

**答案**：因为它要在主进程里直接对接 HTTP/FastAPI 和 Engine 的调用者（把异步结果 `future` 解析回调用方），它和 HTTP server 共享同一个事件循环；若另起进程，还得再加一层 IPC 把结果传回主进程，纯属浪费。

---

### 4.3 TokenizerManager：主进程的接入与收发

#### 4.3.1 概念说明

`TokenizerManager` 是「前台接待 + 传菜员」二合一。它的职责可以拆成两段：

- **入站（接待）**：拿到一个请求（HTTP 请求体或 `Engine.generate` 的参数）→ token 化（多模态还要预处理图像/视频）→ 分配 `rid` → 把请求「投递」给 Scheduler。
- **出站（传菜）**：从一个 ZMQ socket 上**持续接收** Detokenizer 回传的批量文本结果 → 按 `rid` 找到正在等待的那个请求的 `ReqState` → 把增量结果写进去，唤醒等待它的异步调用者。

它继承了两个 mixin（`TokenizerControlMixin`、`TokenizerManagerScoreMixin`），本身只关注「请求/结果」的主干流程。

#### 4.3.2 核心流程

入站（伪代码）：

```
generate_request(req_input):
    rid = 生成唯一号牌
    state = ReqState(rid, ...)              # 记录这个请求的等待状态
    rid_to_state[rid] = state
    tokenized = 把文本/多模态切成 token id    # TokenizedGenerateReqInput
    tokenized.wrap_pickle_fields()           # 多模态等字段先 pickle
    _dispatch_to_scheduler(tokenized)        # PUSH 到 scheduler_input 通道
    async for ... in state:                  # 异步等结果，可流式
        yield 增量结果
```

出站（独立的事件循环 `handle_loop`）：

```
handle_loop():                              # 后台 asyncio 任务，常驻
    while True:
        recv_obj = await async_sock_recv(recv_from_detokenizer)  # 从 tokenizer 通道收
        if 是 Batch*Output:
            await _handle_batch_output(recv_obj)   # 按 rids 分发到各 ReqState
        else:
            _result_dispatcher(recv_obj)           # 控制类消息（abort/会话等）
```

关键点：**入站是「被动触发」的**（来一个请求处理一个），**出站是「常驻事件循环」**（`handle_loop` 一直在 `await recv`，结果一到就分发）。两个方向共用同一个 `rid_to_state` 字典作为「号牌 → 等待者」的桥梁。

#### 4.3.3 源码精读

类定义与 docstring，[tokenizer_manager.py:L265-L266](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/managers/tokenizer_manager.py#L265-L266) —— `TokenizerManager`，注释明确「a process that tokenizes the text」。

`__init__` 是一个典型的「编排式」初始化，把各子系统分派给具名方法，[tokenizer_manager.py:L278-L328](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/managers/tokenizer_manager.py#L278-L328) —— 注意 `init_ipc_channels(port_args)` 这一行，它就是建立 ZMQ 收发 socket 的入口。

IPC socket 的建立，[tokenizer_manager.py:L412-L427](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/managers/tokenizer_manager.py#L412-L427)：

- `recv_from_detokenizer`：`PULL` + `bind=True`，挂在 `tokenizer_ipc_name`——出站收结果的 socket。
- `send_to_scheduler`：`PUSH` + `bind=True`，挂在 `scheduler_input_ipc_name`——入站发请求的 socket。

投递请求的薄封装，[tokenizer_manager.py:L435-L443](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/managers/tokenizer_manager.py#L435-L443) —— `_dispatch_to_scheduler` 就是 `sock_send(self.send_to_scheduler, obj)`（多 tokenizer 模式下额外盖个 `http_worker_ipc` 戳）。

请求在投递前的最后处理，[tokenizer_manager.py:L1367-L1377](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/managers/tokenizer_manager.py#L1367-L1377) —— `_send_one_request`：记录计时戳 → 包装 shm 特性 → `wrap_pickle_fields`（把多模态等非 msgspec 字段 pickle）→ 投递。这正是 4.1 里提到的 `PickleWrapper` 的使用点。

出站事件循环，[tokenizer_manager.py:L1884-L1897](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/managers/tokenizer_manager.py#L1884-L1897) —— `handle_loop`：死循环 `await async_sock_recv(self.recv_from_detokenizer)`，收到批量输出就走 `_handle_batch_output`，否则交给 `_result_dispatcher`。

按 `rid` 分发结果的入口，[tokenizer_manager.py:L1899-L1914](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/managers/tokenizer_manager.py#L1899-L1914) —— `_handle_batch_output`：遍历 `recv_obj.rids`，从 `self.rid_to_state` 找到对应 `ReqState`，把增量文本/token 写回，从而唤醒正在 `async for` 等结果的调用者。

#### 4.3.4 代码实践

**目标**：定位「接收 → token 化 → 发往 Scheduler」的主路径，并解释它为何在主进程。

**操作步骤**：

1. 从 `Engine.generate`（或 HTTP 的 `/generate` 路由）出发，跟随调用直到 `tokenizer_manager.generate_request`。
2. 在 `generate_request` 内找到「token 化」与「`_send_one_request`/`_send_batch_request`」两处。
3. 在 `handle_loop` 与 `_handle_batch_output` 处确认「结果如何回到调用者」。

**需要观察的现象**：请求路径是「HTTP/Engine → `generate_request` → token 化 → `_dispatch_to_scheduler` → ZMQ」；结果路径是「ZMQ → `handle_loop` → `_handle_batch_output` → `ReqState` → 调用者的 `async for`」。

**预期结果**：你能用一句话总结——TokenizerManager 在主进程，是因为它必须直接持有 HTTP/Engine 的异步句柄，把结果原地交给调用方，省掉一层回程 IPC。

#### 4.3.5 小练习与答案

**练习 1**：`rid_to_state` 字典为什么是「入站」和「出站」两个方向共用的关键数据结构？

**答案**：入站时按 `rid` 注册 `ReqState`（建立「号牌→等待者」），出站时 `_handle_batch_output` 按 `rids` 查回 `ReqState` 并写入增量结果。它把「发出去的请求」和「收回来的结果」用同一个号牌对上号。

**练习 2**：`handle_loop` 收到 `BatchStrOutput` 和收到 `AbortReq` 走的是同一条分支吗？

**答案**：不是。`handle_loop` 先判断类型：`BatchStrOutput / BatchEmbeddingOutput / BatchTokenIDOutput` 走 `_handle_batch_output`，其余（如 `AbortReq`、会话控制回执等）走 `_result_dispatcher`。

---

### 4.4 Scheduler：调度与 GPU 执行子进程

#### 4.4.1 概念说明

`Scheduler` 是「后厨主厨」，跑在自己的子进程里（进程名 `sglang::scheduler`）。它做三件事，循环往复：① 把从 Tokenizer 收来的新请求接进自己的批次；② 决定下一个要送进 GPU 的 batch（连续批处理、prefill/decode 拆分）；③ 让 `TpModelWorker`/`ModelRunner` 执行一次前向，处理结果，再把产出的 token 推给 Detokenizer。**它是唯一真正操作 GPU 的进程**（KV 缓存池、模型权重都在它手里）。

根据配置，`Scheduler` 有多种事件循环变体：`event_loop_normal`（普通）、`event_loop_overlap`（CPU/GPU 重叠）、`event_loop_pp`（流水线并行）、各种 PD 分离变体等。本讲只关注最基础的 `event_loop_normal`，其余留到第 4 单元后续讲义（u4-l3、u4-l7）和第 7 单元。

#### 4.4.2 核心流程

`event_loop_normal` 的一次循环（伪代码）：

```
while True:
    recv_reqs = request_receiver.recv_requests()          # 从 scheduler_input 通道 PULL 新请求
    process_input_requests(recv_reqs)                      # 并入 waiting 队列
    plan = get_next_batch_to_run(running_batch, last_batch) # 调度：挑哪些请求、组多大 batch
    batch = plan.batch_to_run
    if batch:
        result = run_batch(batch)                          # 送进 GPU 前向（model_worker）
        process_batch_result(batch, result)                # 采样、更新状态、把 token 推给 Detokenizer
    else:
        on_idle()
    last_batch = batch
```

`process_batch_result` 的最后一步会通过 `ipc_channels.send_to_detokenizer` 把这一步产出的 token id（`BatchTokenIDOutput` 或 `BatchStrOutput`）PUSH 到 Detokenizer 通道。注意「把结果送出去」和「等 GPU 算完」在同一循环里——这就是为什么需要 `overlap` 变体来掩盖这部分 CPU 开销（见 u4-l7）。

#### 4.4.3 源码精读

`Scheduler` 类定义，[scheduler.py:L303](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/managers/scheduler.py#L303) —— 一个继承多个 mixin 的大型类（修改它的 `__init__` 需要先读 `large-class-style` 技能）。`__init__` 里有大量配置读取，[scheduler.py:L313-L368](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/managers/scheduler.py#L313-L368) —— 注意 `self.enable_overlap`（决定走哪个事件循环）在这里确定。

请求接收器的初始化，[scheduler.py:L1756-L1758](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/managers/scheduler.py#L1756-L1758) —— `init_request_receiver` 用 `ipc_channels.recv_from_tokenizer`（PULL socket）构造 `SchedulerRequestReceiver`，循环里靠它 `recv_requests()`。

事件循环的统一入口与分发，[`dispatch_event_loop`](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/managers/scheduler.py#L4502-L4516)：

[scheduler.py:L4502-L4516](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/managers/scheduler.py#L4502-L4516) —— 默认（非 PD 分离、非 PP）时，`enable_overlap` 决定走 `event_loop_overlap` 还是 `event_loop_normal`。这就是「多种循环变体」的分发枢纽。

普通循环的循环体，[`event_loop_normal`](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/managers/scheduler.py#L1522-L1554)：

[scheduler.py:L1522-L1554](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/managers/scheduler.py#L1522-L1554) —— 收请求 → `process_input_requests` → `get_next_batch_to_run` → `run_batch` → `process_batch_result`，正是 4.4.2 的五步。

循环前置的 stream/`run_event_loop` 设置，[scheduler.py:L1472-L1503](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/managers/scheduler.py#L1472-L1503) —— `run_event_loop` 建立 `schedule_stream`（CPU 侧 CUDA stream，用于和 GPU forward stream 重叠），再 `dispatch_event_loop`。

子进程入口函数 [`run_scheduler_process`](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/managers/scheduler.py#L4598-L4663)：

[scheduler.py:L4647-L4663](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/managers/scheduler.py#L4647-L4663) —— 在子进程里 `Scheduler(...)` 构造、`pipe_writer.send(...)` 回报就绪信息、然后 `scheduler.run_event_loop()` 进入死循环（直到 `ShutdownReq` 置 `gracefully_exit`）。这就是 4.2 里 `_launch_scheduler_processes` 等待的那条「就绪」消息的发送端。

Scheduler 侧的 socket 集合，[`SchedulerIpcChannels`](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/managers/scheduler_components/ipc_channels.py#L16-L22)：

[ipc_channels.py:L36-L68](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/managers/scheduler_components/ipc_channels.py#L36-L68) —— rank 0 的 Scheduler 创建四个 socket：`recv_from_tokenizer`（PULL，connect 到 `scheduler_input_ipc_name`）、`send_to_tokenizer`（PUSH）、`send_to_detokenizer`（PUSH，connect 到 `detokenizer_ipc_name`）、`recv_from_rpc`（DEALER）。非 rank 0 的 worker 这些都置空（只做本 rank 的 GPU 计算，不做 IPC 路由）。

#### 4.4.4 代码实践

**目标**：标注 `event_loop_normal` 一次循环调用的函数链，并定位「结果推给 Detokenizer」的代码点。

**操作步骤**：

1. 在 `event_loop_normal`（scheduler.py:1522 起）逐行标注 5 个步骤对应的函数调用。
2. 全局搜索 `send_to_detokenizer.send_output`（如 scheduler.py:4460），确认 `process_batch_result` 把 token 结果 PUSH 到 Detokenizer 通道。

**需要观察的现象**：循环体的顶部 `recv_requests` 来自 `recv_from_tokenizer`，底部的结果发送走向 `send_to_detokenizer`。

**预期结果**：你能画出「PULL(tokenizer) → process → GPU forward → process_batch_result → PUSH(detokenizer)」的单循环数据流，并指出 `last_batch` 如何在两次循环之间传递状态。

#### 4.4.5 小练习与答案

**练习 1**：`dispatch_event_loop` 为什么不在 `run_event_loop` 里直接 `if/else`，而是单独抽成函数？

**答案**：因为 PD 分离模式下 prefill/decode 节点各自要走完全不同的事件循环（`event_loop_*_disagg_*`），分支很多；单独抽成 `dispatch_event_loop` 让 `run_event_loop` 只负责「建 stream + 调度循环」这两件事，保持顶层流程像伪代码一样清晰。

**练习 2**：非 rank 0 的 Scheduler worker 为什么把 `recv_from_tokenizer` 置为 `None`？

**答案**：只有 rank 0 负责和 TokenizerManager 对接（接请求、回结果），其余 TP/PP worker 只做本 rank 的 GPU 前向并通过 NCCL 等集合通信与 rank 0 协作，不直接参与 ZMQ IPC，所以这些 socket 对它们没有意义。

---

### 4.5 DetokenizerManager：增量解码回传子进程

#### 4.5.1 概念说明

`DetokenizerManager`（进程名 `sglang::detokenizer`）是「摆盘员」。GPU 每一步产出的只是**新增的 token id**，但调用者要的是**文本**。把 id 翻译成文本叫 detokenize；为了省算力，每一步只翻译「相比上次多出来的那几个 token」，这叫**增量解码（incremental decode）**。它解码完，把文本结果 PUSH 回 TokenizerManager。

为什么要把 detokenize 单独拎成一个进程？因为 `tokenizer.decode` 是纯 CPU 操作，且对某些 tokenizer（如带 BPE merge 的）会比较吃 CPU；让它和 Scheduler 的 GPU 调度隔离，避免拖慢推理主循环。

> 一个细节：当 `skip_tokenizer_init=True`（不输出文本，只要 token id）时，Scheduler 会**绕过** Detokenizer，直接把结果 PUSH 到 `tokenizer_ipc_name` 通道（见 4.1 拓扑表里的备注）。此时 Detokenizer 不参与数据回路。

#### 4.5.2 核心流程

`event_loop`（伪代码）：

```
while True:
    recv_obj = sock_recv(recv_from_scheduler)   # 从 detokenizer 通道 PULL（BatchTokenIDOutput 等）
    output = _request_dispatcher(recv_obj)       # 分发：token_id_out → 增量解码 → BatchStrOutput
    if output is not None:
        sock_send(send_to_tokenizer, output)     # PUSH 回 tokenizer 通道
```

增量解码的关键是「skip 指针」：每个请求记录「已经解码到第几个 token」，下次只解码 `[skip, 新长度)` 这一段，再和上次的末尾做拼接修正（因为某些 BPE token 的边界会跨步，需要用 surrogate/read 双缓冲修正）。

#### 4.5.3 源码精读

类定义，[detokenizer_manager.py:L91-L94](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/managers/detokenizer_manager.py#L91-L94) —— `DetokenizerManager`，同样继承一个多 worker mixin。

`__init__` 同样是编排式，[detokenizer_manager.py:L94-L109](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/managers/detokenizer_manager.py#L94-L109) —— 先建 IPC socket，再加载 tokenizer，再初始化运行状态（含 `decode_status` 这个「skip 指针」字典）与分发器。

IPC socket 建立，[detokenizer_manager.py:L111-L122](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/managers/detokenizer_manager.py#L111-L122)：

- `recv_from_scheduler`：`PULL` + `bind=True`，挂在 `detokenizer_ipc_name`——接收 Scheduler 的 token 结果。
- `send_to_tokenizer`：`PUSH` + `bind=False`（connect），挂在 `tokenizer_ipc_name`——回传文本结果给 TokenizerManager。

事件循环，[`event_loop`](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/managers/detokenizer_manager.py#L166-L174)：

[detokenizer_manager.py:L166-L174](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/managers/detokenizer_manager.py#L166-L174) —— 经典的「收 → 分发 → 发」三步：`sock_recv(recv_from_scheduler)` → `_request_dispatcher(recv_obj)` → 若有输出则 `sock_send(send_to_tokenizer, output)`。`BatchEmbeddingOutput`（嵌入模型，无需解码）会原样透传（见 `handle_batch_embedding_out`，L208-L210）。

子进程入口 [`run_detokenizer_process`](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/managers/detokenizer_manager.py#L512-L534)：

[detokenizer_manager.py:L512-L534](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/managers/detokenizer_manager.py#L512-L534) —— `setproctitle("sglang::detokenizer")` 给进程改名（这就是 `ps` 里能看到的标签），构造 `DetokenizerManager`，然后 `manager.event_loop()` 进死循环；崩溃时给父进程发 `SIGQUIT` 触发整体关闭。

#### 4.5.4 代码实践

**目标**：确认 Detokenizer 走的是「增量解码」而非每步全量重算。

**操作步骤**：

1. 在 `event_loop` 的 `sock_send(self.send_to_tokenizer, output)` 前加一行日志，打印 `output.output_ids[i]` 的长度（本次新增的 token 数）和该请求累计的 token 数。
2. 发起一个流式请求，让它生成较长的文本。

**需要观察的现象**：每次回传的「新增 token 数」是 1（或固定小步长），而不是「已生成总长度」。

**预期结果**：你会看到 Detokenizer 每次只解码并回传**新增**的 token，验证了增量解码设计。若无法运行，可改为阅读 `decode_status` / skip 指针相关逻辑，标注为「待本地验证」。

#### 4.5.5 小练习与答案

**练习 1**：嵌入模型（embedding）的请求经过 Detokenizer 时会发生什么？

**答案**：几乎不发生任何事。`handle_batch_embedding_out` 直接把 `BatchEmbeddingOutput` 原样返回（embedding 不需要解码文本），随后被 `event_loop` PUSH 回 TokenizerManager。

**练习 2**：如果 `skip_tokenizer_init=True`，Detokenizer 还会被创建吗？

**答案**：进程仍按 `_launch_detokenizer_subprocesses` 的逻辑启动，但 Scheduler 在 `SchedulerIpcChannels.create` 里会把 `send_to_detokenizer` 也指向 `tokenizer_ipc_name`（直接送回前台），所以数据回路会绕过 Detokenizer 的解码逻辑（详见 4.1 拓扑表备注）。

---

## 5. 综合实践

把本讲全部最小模块串起来：**启动服务 → 确认进程拓扑 → 画出一次请求的完整流转图**。

### 步骤一：启动服务

用一个小模型启动（请替换为你本地可用的模型路径）：

```bash
python -m sglang.launch_server \
  --model-path Qwen/Qwen2.5-0.5B \
  --tp 1 --host 127.0.0.1 --port 30000
```

> 若无 GPU，可只做源码阅读部分；命令的实际输出依赖本地环境，标注为「待本地验证」。

### 步骤二：确认进程拓扑

在另一个终端，服务就绪后执行：

```bash
ps -ef | grep -E "sglang::(scheduler|detokenizer)" | grep -v grep
```

**预期看到**：

- 1 个主进程（你执行 `launch_server` 的那个，跑着 TokenizerManager + HTTP/uvicorn）。
- `--tp 1` 时有 **1 个** `sglang::scheduler` 子进程；若用 `--tp 2`，会有 2 个。
- 1 个 `sglang::detokenizer` 子进程。

把这些进程和本讲讲的类一一对应：主进程 = `TokenizerManager`（+ FastAPI），`sglang::scheduler` = `Scheduler`，`sglang::detokenizer` = `DetokenizerManager`。

### 步骤三：画出请求流转图

基于步骤二的进程，画一条 `/generate` 请求的完整流转。下面是参考答案（应当与你从源码读出的一致）：

```
调用者(Engine/HTTP)
   │  ① 文本请求
   ▼
┌─────────────────────┐
│ TokenizerManager    │  (主进程)
│  - token 化、分 rid │
│  ② PUSH(scheduler_  │
│       input_ipc)    │
└────────┬────────────┘
         │ TokenizedGenerateReqInput
         ▼
┌─────────────────────┐
│ Scheduler           │  (子进程 sglang::scheduler, 持有 GPU)
│  - 组 batch、前向   │
│  - 采样产出 token id│
│  ③ PUSH(detokenizer │
│       _ipc)         │
└────────┬────────────┘
         │ BatchTokenIDOutput
         ▼
┌─────────────────────┐
│ DetokenizerManager  │  (子进程 sglang::detokenizer)
│  - 增量解码成文本   │
│  ④ PUSH(tokenizer_  │
│       ipc)          │
└────────┬────────────┘
         │ BatchStrOutput
         ▼
┌─────────────────────┐
│ TokenizerManager    │  handle_loop 收到，按 rid 写回 ReqState
│  ⑤ 异步唤醒调用者   │
└────────┬────────────┘
         │
         ▼
调用者拿到生成文本
```

把这张图和你在 4.1 画的 IPC 拓扑表对照：①②走 `scheduler_input_ipc_name`，③走 `detokenizer_ipc_name`，④⑤走 `tokenizer_ipc_name`——三条通道、一个闭环。

### 步骤四（进阶，可选）

把 `--tp` 改成 2，重复步骤二，观察 `sglang::scheduler` 进程数变成 2，并结合 4.4 解释「为什么 rank 0 之外的 scheduler 不建 IPC socket」（提示：只有 rank 0 接请求、回结果，其余只做本 rank GPU 计算并通过 NCCL 协作）。

## 6. 本讲小结

- SGLang 运行时默认由**主进程的 `TokenizerManager`** + **子进程 `Scheduler`** + **子进程 `DetokenizerManager`** 三方组成；只有 Scheduler 真正操作 GPU。
- `Engine._launch_subprocesses` 是总装配线：先 fork Scheduler（让它后台加载模型），再 fork Detokenizer，再在主进程内构造 TokenizerManager，最后 `wait_for_ready` 等模型加载完成，并装上 `SubprocessWatchdog`。
- 三方靠三条 ZMQ PUSH/PULL 通道形成闭环：`scheduler_input_ipc_name`（Tokenizer→Scheduler）、`detokenizer_ipc_name`（Scheduler→Detokenizer）、`tokenizer_ipc_name`（Detokenizer→Tokenizer）；通道名集中在 `PortArgs`。
- 通道两端的 bind/connect 约定是「主进程与 Detokenizer 各自 bind 自己的接收 socket，Scheduler 统一 connect」；消息用 `msgspec`/msgpack 序列化，非 msgspec 字段经 `PickleWrapper` 包装。
- 一条请求的 token 流是：Tokenizer(化、发) → Scheduler(调度、GPU 前向、发 token) → Detokenizer(增量解码、发文本) → Tokenizer(按 rid 唤醒调用者)。
- Scheduler 有多种事件循环变体（normal/overlap/pp/disagg），由 `dispatch_event_loop` 按配置分发；本讲只展开 `event_loop_normal`。

## 7. 下一步学习建议

- 想深入「Scheduler 内部怎么组 batch、怎么连续批处理」→ 第 4 单元调度核心系列，尤其 u4-l3（Scheduler 事件循环细节）、u4-l4（ScheduleBatch 生命周期）。
- 想搞清「消息结构到底有哪些字段、跨进程怎么编解码」→ u4-l1（io_struct 与 IPC）会专门展开 `BaseReq`/`BatchStrOutput` 与 msgpack 细节。
- 想理解「为什么需要 overlap 调度」→ u4-l7（Overlap 调度器），它正是为了让 Scheduler 的 CPU 准备工作与 GPU 前向重叠。
- 想看分布式拓扑（多个 Scheduler、DP 控制器）→ 第 7 单元 u7-l1（张量并行与 parallel_state）、u7-l4（数据并行控制器）。
