# 多进程编排启动流程

> 承接上一讲：在 u1-l4 中我们看到，`api_server.py` 只有十几行，它把 `set_start_method("spawn")` 设好后，就按 `args.run_mode` 把控制权交给 `api_start.py` 里的某个启动函数。本讲就深入这个「真正的启动大脑」——`api_start.py`，看它如何把一个空的命令行参数对象，编排成一整套互相协作的推理服务进程。

## 1. 本讲目标

学完本讲，你应当能够：

1. 说清楚 `normal_or_p_d_start` 在 `normal` 模式下**依次拉起了哪些子进程**，以及为什么是这个顺序。
2. 理解「子进程拉起机制」：`process_manager` 如何用 `multiprocessing.Process` 启动子进程，并用一根管道做 `init ok` 握手来保证子进程已就绪。
3. 看懂**端口分配**三件套：`PortLocker` 锁端口、`alloc_can_use_network_port` 批量申请端口、再把端口名解包到 `args` 的各个字段；并理解「外部 HTTP 端口」和「内部 zmq 端口」的区别。
4. 理解**信号处理**如何让一整组多进程在 Ctrl+C 或 kill 时优雅退出而不留孤儿进程。
5. 建立一张「进程 → 职责 → 通信端口」的脑内地图，为第二单元（u2）讲请求链路打好基础。

## 2. 前置知识

本讲需要你已经具备（u1-l1 ~ u1-l4 已建立）的认知：

- **LightLLM 是一个多进程架构**：HttpServer / Router / ModelBackend / Detokenization 等是各自独立的进程，靠 zmq、rpyc、共享内存通信。本讲就是讲「这些进程是怎么被一个个拉起来的」。
- **`run_mode` 的分发**：`normal` / `prefill` / `decode` 都进入 `normal_or_p_d_start`；`pd_master` / `config_server` / `visual_only` 各有专门函数。本讲聚焦最常见的 `normal_or_p_d_start`。
- **spawn 启动方式**：`api_server.py` 调用了 `torch.multiprocessing.set_start_method("spawn")`。spawn 会让子进程作为一个**全新的 Python 解释器**启动，而不是 fork 复制父进程内存（这对 CUDA 是安全的）。它带来的一个关键后果是：**父进程在启动子进程之前写入的环境变量，会被子进程继承**——这一点在本讲的「参数传递」环节会用到。

如果你对下面几个 Python 标准库概念不熟，先建立一个最简印象：

- **`multiprocessing.Process(target=func, args=...)`**：开一个新进程，让它去执行 `func`。
- **`multiprocessing.Pipe`**：一对管道，一端写、一端读，用于父子进程间传消息。
- **`subprocess.Popen`**：从 Python 里启动一个外部命令（这里是 `hypercorn` 这个 ASGI 服务器）。
- **`signal.signal`**：给进程注册「收到某信号（如 Ctrl+C 触发的 SIGINT）时该执行什么函数」。
- **zmq**：一个高性能消息队列库，LightLLM 用它的 PULL/PUSH/PUB/SUB 模式在进程间传 token。rpyc 则是一个「让 Python 进程之间像本地函数调用一样互相调用」的库。

## 3. 本讲源码地图

本讲几乎全部围绕一个文件展开，辅以它依赖的工具函数：

| 文件 | 作用 |
| --- | --- |
| [lightllm/server/api_start.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_start.py) | **本讲主角**。包含 `normal_or_p_d_start` 等启动函数，负责参数自动配置、端口分配、按顺序拉起子进程、启动 hypercorn、注册信号处理。 |
| [lightllm/utils/start_utils.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/utils/start_utils.py) | 提供 `process_manager`（`SubmoduleManager`），封装「开子进程 + init ok 握手 + 统一终止」的逻辑。 |
| [lightllm/utils/net_utils.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/utils/net_utils.py) | 提供 `alloc_can_use_network_port`（找空闲端口）与 `PortLocker`（锁住端口防冲突）。 |
| [lightllm/utils/envs_utils.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/utils/envs_utils.py) | 提供 `set_unique_server_name`（生成唯一服务名，用于命名空间隔离）、`set_env_start_args`（把参数序列化进环境变量）。 |
| `lightllm/server/router/manager.py`、`detokenization/manager.py`、`metrics/manager.py`、`embed_cache/manager.py`、`visualserver/manager.py` | 这些文件各自定义一个 `start_xxx_process(args, pipe_writer)` 函数，就是被 `api_start.py` 拉起的子进程入口。 |

## 4. 核心概念与源码讲解

本讲把 `api_start.py` 拆成四个最小模块来讲：**启动编排总流程**、**子进程拉起机制**、**端口分配**、**信号处理**。前两个共同构成「启动编排」这一主题，后两个分别是「端口分配」和「信号处理」。

### 4.1 启动编排总流程：normal_or_p_d_start 做了什么

#### 4.1.1 概念说明

`normal_or_p_d_start` 是 `api_start.py` 里最重要的函数，也是 `normal` 模式（最常见、单进程组既能 prefill 又能 decode 的部署形态）的总指挥。可以把它想象成一个「剧组导演」：

- 它手里有一份「剧本」——命令行解析出来的 `args`（可能很多字段还是 `None`，要靠它补全）。
- 它要决定「今天这场戏需要哪些演员（子进程）上场」——是不是多模态模型？要不要视觉进程？要不要音频进程？要不要 CPU 缓存？
- 它要给每个演员分配「对讲机频道（端口）」和「更衣室钥匙（共享内存 id）」。
- 它要让演员按顺序上场，每个演员就位（`init ok`）后才让下一个上场。
- 最后它自己退到后台，盯着前台主角（hypercorn HTTP 服务），等演出结束。

需要强调：`normal_or_p_d_start` 这个函数同时服务三种 `run_mode`：`normal`、`prefill`、`decode`。函数开头有一道「关卡」——只允许这三种（外加 `visual_only`）继续往下走，其余直接 `return`。

#### 4.1.2 核心流程

把 `normal_or_p_d_start` 的执行过程抽象成下面五个阶段：

```text
┌─────────────────────────────────────────────────────────────────┐
│ 阶段 0：模式关卡                                                 │
│   run_mode ∈ {normal, prefill, decode, visual_only} 才继续        │
└─────────────────────────────────────────────────────────────────┘
                              │
┌─────────────────────────────▼───────────────────────────────────┐
│ 阶段 1：参数自动补全与校验                                       │
│   - 多模态探测 (has_vision_module / has_audio_module)            │
│   - 共享内存 id 生成 (cpu_kv_cache_shm_id 等)                    │
│   - 调度/性能/量化/MTP 等几十项参数的默认值与互斥断言             │
│   - 从 config.json 读 eos_id / tool_call_parser / data_type      │
└─────────────────────────────────────────────────────────────────┘
                              │
┌─────────────────────────────▼───────────────────────────────────┐
│ 阶段 2：端口分配（详见 4.3）                                     │
│   - PortLocker 锁住已知端口                                      │
│   - alloc_can_use_network_port 批量申请空闲端口                  │
│   - 解包成 router_port / detokenization_port / metric_port ...   │
│   - set_env_start_args 把参数写进环境变量（供子进程继承）        │
└─────────────────────────────────────────────────────────────────┘
                              │
┌─────────────────────────────▼───────────────────────────────────┐
│ 阶段 3：按顺序拉起子进程（每个都要 init ok 才继续）              │
│   embed_cache → visual → audio → multi_level_kv_cache            │
│   → metric → [router + detokenization]  （条件触发，见 4.1.3）   │
└─────────────────────────────────────────────────────────────────┘
                              │
┌─────────────────────────────▼───────────────────────────────────┐
│ 阶段 4：启动 hypercorn（HTTP 服务）+ 可选 health_monitor         │
│         注册信号处理，http_server_process.wait() 阻塞收尾        │
└─────────────────────────────────────────────────────────────────┘
```

阶段 1 里有大量「如果某参数是 None，就给它一个合理默认值；如果两个参数互斥，就断言报错」的代码，这些是防御性编程，逻辑直白，这里不逐条展开，重点放在阶段 2~4。

#### 4.1.3 源码精读

**关卡与多模态探测。** 函数一开始先自动设置几个全局量，再过模式关卡，然后探测模型是不是多模态：

[lightllm/server/api_start.py:88-111](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_start.py#L88-L111) —— 第 88~89 行是模式关卡（只放行四种 run_mode）；第 92~101 行通过 `has_vision_module` / `has_audio_module` 检测 `model_dir` 里的模型配置，决定 `disable_vision` / `disable_audio` 的取值；第 108~111 行据此推导 `enable_multimodal`。这段决定了「阶段 3 要不要拉起 visual / audio / embed_cache 进程」。

**生成共享内存 id。** 多级缓存和多模态嵌入都需要一块跨进程共享的内存，LightLLM 用一个随机整数作为这块共享内存的「门牌号」：

[lightllm/server/api_start.py:113-118](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_start.py#L113-L118) —— `uuid.uuid1().int % 123456789` 生成一个有界的随机整数，赋给 `cpu_kv_cache_shm_id` / `multi_modal_cache_shm_id`。同一台机器上启动多个 LightLLM 实例时，各自拿到不同的 id，共享内存就不会串台。

**阶段 3：按顺序拉起子进程。** 这是本模块的核心。注意所有条件分支：

[lightllm/server/api_start.py:423-491](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_start.py#L423-L491) —— 依次为：
- L423~429：`enable_multimodal` 时拉起 `start_cache_manager`（embed_cache，多模态嵌入缓存）；
- L431~454：未禁用视觉时拉起 `start_visual_process`（有 `visual_use_proxy_mode` 两种实现可选）；
- L456~466：未禁用音频时拉起 `start_audio_process`；
- L468~476：`enable_cpu_cache` 时拉起 `start_multi_level_kv_cache_manager`；
- L478~483：**无条件**拉起 `start_metric_manager`（指标进程）；
- L485~491：**无条件**同时拉起 `start_router_process` 与 `start_detokenization_process`（这两个写在同一个 `start_submodule_processes` 调用里）。

这个顺序不是随意的：

1. **metric 必须早于 router**：因为 router 初始化时会连上 `metric_port`（见 [router/manager.py:96](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/manager.py#L96) 的 `MetricClient(args.metric_port)`），如果 metric 还没起来，router 连不上。
2. **router 和 detokenization 一起起**：router 推理出新 token 后要 PUSH 给 detokenization，两者协作紧密，放同一批起。
3. **embed_cache / visual / audio 必须早于 router**：多模态请求要先算图像/音频嵌入，再交给 router 推理，所以这些「上游」进程要先就位。

每个 `start_submodule_processes` 调用都是**阻塞的**——必须等这批里所有子进程都发回 `init ok`，才会继续下一批。这意味着子进程必须真的能成功初始化（加载模型权重、绑定端口等），主进程才往下走；任何一个失败，整个启动就会中止。这个握手细节在 4.2 详述。

**阶段 4：启动 hypercorn。** 所有协作进程就绪后，主角——HTTP 服务——登场：

[lightllm/server/api_start.py:494-512](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_start.py#L494-L512) —— 用 `subprocess.Popen` 启动 `hypercorn`（一个 ASGI 服务器），加载 `lightllm.server.api_http:app` 这个 app，绑定到 `{args.host}:{args.port}`（默认 127.0.0.1:8000），worker 数由 `args.httpserver_workers` 决定。注意它与之前拉起的子进程不同：hypercorn 是一个**外部命令进程**，不是用 `mp.Process` 拉起的 Python 函数。

**收尾阻塞。** 函数最后一行不是 `return`，而是：

[lightllm/server/api_start.py:523-525](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_start.py#L523-L525) —— 先 `setup_signal_handlers` 注册信号处理（见 4.4），再 `http_server_process.wait()` **阻塞**主进程，直到 hypercorn 退出。也就是说，主进程的「`normal_or_p_d_start` 调用」会一直卡在这里，充当整个进程组的「看护者」，直到服务被关闭。

> 小结一句：`normal_or_p_d_start` 把启动过程做成了一条**顺序、阻塞、握手驱动**的流水线，每个环节失败都会及时暴露。

#### 4.1.4 代码实践

**实践目标**：亲手把 `normal` 模式下「被拉起的进程」整理成一张对照表，把本讲的抽象流程落到具体进程上。

**操作步骤**：

1. 打开 [lightllm/server/api_start.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_start.py)，定位 `normal_or_p_d_start` 函数（L74 起）。
2. 找到阶段 3（L423~491），逐个列出每个 `process_manager.start_submodule_processes(...)` 调用，记录：
   - 它调用的 `start_*` 函数名（去对应 `manager.py` 里搜这个函数名，看它 `setproctitle.setproctitle(...)` 设的进程角色名）。
   - 它的触发条件（是无条件，还是 `if args.enable_multimodal` 之类）。
3. 对每个进程，进它的 `start_xxx_process` 函数体，找出它**绑定/连接**的端口字段（搜 `args.xxx_port`、`bind(`、`connect(`）。

**需要观察的现象 / 预期结果**：你应该能得到类似下面这张表的雏形（端口含义在 4.3 会补全）：

| 进程角色（proctitle） | start 函数 | 触发条件 | 关键通信端口 |
| --- | --- | --- | --- |
| `metric_manager` | `start_metric_manager` | 无条件 | rpyc 服务于 `metric_port` |
| `router_server` | `start_router_process` | 无条件 | zmq PULL 绑 `router_port` |
| `detokenization_server` | `start_detokenization_process` | 无条件 | zmq PULL 绑 `detokenization_port`，PUB 绑 `http_server_port` |
| `cache_manager` | `start_cache_manager` | `enable_multimodal` | rpyc 服务于 `cache_port` |
| `visual_server` | `start_visual_process` | 未禁用视觉 | `visual_port` |
| `audio_server` | `start_audio_process` | 未禁用音频 | `audio_port` |
| `multi_level_kv_cache` | `start_multi_level_kv_cache_manager` | `enable_cpu_cache` | `multi_level_kv_cache_port` |

> 注：完整可运行需要 GPU 与模型权重，本实践是**源码阅读型实践**，重点在「能从源码读出进程清单与触发条件」，不要求真正启动服务。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `start_metric_manager` 必须在 `start_router_process` 之前调用？如果调换顺序会怎样？

> **参考答案**：因为 `RouterManager.__init__` 里直接 `MetricClient(args.metric_port)` 去连接 metric 进程（[router/manager.py:96](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/manager.py#L96)）。而 `start_submodule_processes` 是阻塞握手——router 启动时会连 metric。若 metric 尚未起来，router 的初始化连接会失败，发回的不是 `init ok`，主进程会 kill 掉所有子进程并 `sys.exit(1)`，启动失败。

**练习 2**：`run_mode == "decode"` 时，多模态相关的进程还会启动吗？

> **参考答案**：不会。L104~106 显式把 `disable_audio` / `disable_vision` 都置为 `True`（PD 分离的 decode 节点不做多模态编码），于是 L431 / L456 的条件都不满足，visual 与 audio 进程都不会被拉起。

---

### 4.2 子进程拉起机制：mp.Process 与 init ok 握手

#### 4.2.1 概念说明

4.1 里反复提到「每个 `start_submodule_processes` 调用会阻塞，直到子进程发回 `init ok`」。这套机制实现在 `lightllm/utils/start_utils.py` 的 `SubmoduleManager` 类里，全局单例 `process_manager` 就是它的实例。

它解决两个问题：

1. **怎么开子进程**：用标准库 `multiprocessing.Process`（在 spawn 模式下），让子进程去执行我们给的 `start_func`。
2. **怎么知道子进程准备好了**：开子进程时同时开一根 `Pipe`，子进程初始化完成后往管道里写 `"init ok"`，父进程从管道里 `recv()`，收到了才认为这个子进程就绪。如果子进程初始化抛异常，它会把异常信息写进管道，父进程收到非 `"init ok"` 的内容就 kill 掉所有子进程并退出。

这是一种非常实用的「**启动期同步握手**」模式：它比「无脑 fork 然后祈祷」可靠得多——能在启动阶段就暴露子进程的初始化错误（端口被占、权重找不到、CUDA OOM 等），而不是让一个半残的服务静默上线。

#### 4.2.2 核心流程

```text
父进程 normal_or_p_d_start              子进程 start_xxx_process(args, pipe_writer)
─────────────────────────────           ─────────────────────────────────────────
for 每个 start_func:
    pipe_reader, pipe_writer = Pipe()        （继承 pipe_writer）
    p = Process(target=start_func,
                args=start_arg + (pipe_writer,))
    p.start()  ───────────────────────►  开始执行 start_func:
                                                setproctitle(...)
                                                初始化 Manager(args)   # 加载模型/绑端口
                                                pipe_writer.send("init ok")  ◄──── 握手
    state = pipe_reader.recv()  ◄──────                              （或 send 异常字符串）
    if state != "init ok":
        kill 所有子进程; sys.exit(1)
    记录 p 到 self.processes
```

关键点：`start_arg + (pipe_writer,)` —— 子进程函数的真实签名是 `start_xxx_process(args, pipe_writer)`，多出来的最后一个参数 `pipe_writer` 就是 `process_manager` 自动追加的，业务侧的 `start_*` 函数不需要自己管管道。

#### 4.2.3 源码精读

[lightllm/utils/start_utils.py:13-41](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/utils/start_utils.py#L13-L41) —— 这是 `SubmoduleManager.start_submodule_processes` 的核心：
- L18~26：对每个 `(start_func, start_arg)`，建一对管道，用 `mp.Process` 开子进程，把 `start_arg + (pipe_writer,)` 作为参数传入；
- L29~37：依次对每根管道 `recv()`，只有收到字符串 `"init ok"` 才算成功；否则打印错误、kill 掉本批所有子进程、`sys.exit(1)`；
- L39~40：断言所有子进程还活着，并把它们累加到 `self.processes` 列表里，供后续 `terminate_all_processes` 统一管理。

来看看「子进程那一侧」是如何配合的。以 detokenization 为例：

[lightllm/server/detokenization/manager.py:169-183](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/detokenization/manager.py#L169-L183) —— 签名正是 `(args, pipe_writer)`；先注册 graceful 退出、设进程名；接着 `DeTokenizationManager(args)` 完成绑定 zmq 端口、加载 tokenizer 等初始化；初始化一旦抛异常（L178~180）就 `pipe_writer.send(str(e))` 让父进程捕获；成功则 `pipe_writer.send("init ok")`（L181），然后才进入 `handle_loop()` 主循环。

router 进程如出一辙，只是初始化更重（要加载模型、起 NCCL 通信域等）：

[lightllm/server/router/manager.py:537-573](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/manager.py#L537-L573) —— L551 建 `RouterManager`、L555 `wait_to_model_ready()` 把所有 GPU 上的模型进程拉起来；任何异常都拼成完整 traceback 字符串经管道回传（L567），成功则 L571 发 `init ok`，L572 才进入调度主循环 `loop_for_fwd()`。

**参数怎么传到子进程**：除了通过 `mp.Process` 的 `args=` 直接把 `args` 对象 pickle 传过去之外，`normal_or_p_d_start` 还会在拉起子进程**之前**调用 `set_env_start_args(args)`：

[lightllm/server/api_start.py:418](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_start.py#L418) 与 [lightllm/utils/envs_utils.py:35-40](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/utils/envs_utils.py#L35-L40) —— 把整个 `args` 序列化成 JSON 存进环境变量 `LIGHTLLM_START_ARGS`。因为 spawn 模式下子进程会继承父进程的环境变量，所以子进程里任何地方都可以用 `get_env_start_args()` 读回完整的启动参数（而且被 `lru_cache` 缓存）。这两套机制并存，让参数在进程树里无处不在。

#### 4.2.4 代码实践

**实践目标**：亲手验证「init ok 握手」的阻塞与失败传播行为。

**操作步骤**：

1. 阅读 [start_utils.py:29-37](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/utils/start_utils.py#L29-L37)，确认：父进程是**逐个** `recv()`、并且**整批**一起启动一起校验的。
2. 做一个思想实验（不必真跑）：假设你把 `start_detokenization_process` 里的 `pipe_writer.send("init ok")`（[detokenization/manager.py:181](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/detokenization/manager.py#L181)）注释掉，问自己：
   - 父进程在 `pipe_reader.recv()` 处会怎样？（答：一直阻塞，因为没有数据可读。）
   - 如果让初始化阶段抛一个异常（模拟端口被占用），父进程会收到什么？（答：收到异常字符串，触发 kill + sys.exit(1)。）

**需要观察的现象 / 预期结果**：

- 正常情况下，日志里会逐条打印 `init func start_xxx_process : init ok`。
- 任何子进程初始化失败，你会看到 `init func start_xxx_process : <错误信息>`，随后整个服务启动中止。

> 待本地验证：以上是依据源码逻辑的推断；若要亲见，需要在本机或容器中真实启动一次服务（可参考 u1-l2 的快速启动），观察终端日志中的 `init func ... : init ok` 行。

#### 4.2.5 小练习与答案

**练习 1**：`start_submodule_processes` 同时传了 `start_router_process` 和 `start_detokenization_process` 两个函数（[api_start.py:485-491](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_start.py#L485-L491)）。它们是同时启动、再一起校验，还是串行启动串行校验？

> **参考答案**：**先同时启动，再依次校验**。看 [start_utils.py:18-26](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/utils/start_utils.py#L18-L26)：循环里先对所有 func 都 `process.start()`（即两个子进程并发开始初始化），把 `pipe_reader` 收集起来；然后 L29 才开始逐个 `recv()`。所以两个进程是并行初始化、串行等待握手的。

**练习 2**：为什么 router 初始化失败时，传回管道的是「完整 traceback 字符串」而不是直接 `raise`？

> **参考答案**：因为子进程是独立进程，它直接 `raise` 只会让**自己**崩溃，父进程通过 `Process` 对象看不到详细异常。把 traceback 序列化成字符串经管道发回（[router/manager.py:560-569](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/manager.py#L560-L569)），父进程的 `recv()` 拿到的就不是 `"init ok"`，于是能 `logger.error(...)` 把真正原因打印出来，方便排查。

---

### 4.3 端口分配：锁定、批量申请与命名空间隔离

#### 4.3.1 概念说明

LightLLM 一启动就要用掉**十几个端口**：router 一个、detokenization 一个、metric 一个、visual / audio / cache 各一个、hypercorn 一个，PD 分离模式还要额外给每个节点一组 rpyc 端口……手动一个个指定既繁琐又容易冲突。

LightLLM 的做法是「**让程序自己找空闲端口**」：只要求用户指定最关键的 `--port`（hypercorn 的对外 HTTP 端口），其余内部端口全部由 `alloc_can_use_network_port` 在 10000 号以上的区间自动探测空闲端口来分配。同时用 `PortLocker` 在分配期间把已知端口「占住」，防止同一台机器上同时启动两个 LightLLM 实例时撞端口。

这里有一个**新手极易混淆**的点，务必区分：

- **外部 HTTP 端口** = `args.port`（默认 8000）：hypercorn 绑定，是**用户用 curl 请求的端口**。
- **内部 zmq 端口** = `http_server_port`（自动分配的 10 个端口之一）：detokenization 进程往这里 PUB 生成结果，hypercorn 工作进程从这里 SUB 订阅。它和 `args.port` **不是同一个端口**！只是名字里都有 "http" 容易看混。

#### 4.3.2 核心流程

端口分配三步走：

```text
步骤 A：收集「已知会占用」的端口
    already_used = [args.port] + 可选的 nccl_port + visual/audio nccl_ports

步骤 B：PortLocker.lock_port()  —— 为每个已知端口开 socket bind+listen 临时占住

步骤 C：alloc_can_use_network_port(num, used_ports=already_used)
    从 10000 起逐个 connect_ex 探测，挑出 num 个「连不上(=空闲)且不在 used 列表」的端口
    随机打乱后返回前 num 个

步骤 D：把返回的前 10 个端口「名解包」到 args 的各字段
    nccl_port, router_port, ..., metric_port, multi_level_kv_cache_port = ports[0:10]

步骤 E：set_env_start_args(args) 之后再 PortLocker.release_port()  —— 释放占位
```

为什么需要步骤 B「先锁住再申请」？因为申请到的端口要在「稍后真正被各子进程 bind」之前都是「逻辑占用、物理空闲」状态——如果不锁住，在这段空窗期另一个并发的 LightLLM 实例可能申请到同一个端口，等到真正 bind 时才报「端口已被占用」，错误出现得很晚且难定位。`PortLocker` 把这种「晚到的冲突」提前到启动瞬间暴露。

申请的端口数量也不是写死的 10，而是一个公式（覆盖固定 10 个 + 多机/PD/多模态需要的额外端口）：

\[ \text{num} = 10 + \text{node\_world\_size} + \text{visual\_dp}\times\text{visual\_tp} + \text{visual\_dp} + \text{audio\_dp} \]

其中前 10 个是固定槽位（见步骤 D），后面的是 visual/audio 的 nccl 端口和 PD 节点的 rpyc 端口。

#### 4.3.3 源码精读

**步骤 A+B：锁住已知端口。**

[lightllm/server/api_start.py:342-353](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_start.py#L342-L353) —— L342 起把 `args.port`、`nccl_port`、visual/audio nccl 端口收集进 `already_uesd_ports`；L352~353 用 `PortLocker` 全部 bind 锁住。

`PortLocker` 的实现很直白：

[lightllm/utils/net_utils.py:66-84](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/utils/net_utils.py#L66-L84) —— 每个端口开一个 socket，`bind` + `listen(1)` 把端口占住（`SO_REUSEADDR` 避免 TIME_WAIT 干扰）；`release_port` 时 `close` 所有 socket 即释放。

**步骤 C：批量探测空闲端口。**

[lightllm/server/api_start.py:355-360](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_start.py#L355-L360) —— 按 4.3.2 的公式申请端口，`used_ports` 传入刚才锁住的列表，确保不会重复分配。

`alloc_can_use_network_port` 的探测逻辑：

[lightllm/utils/net_utils.py:10-24](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/utils/net_utils.py#L10-L24) —— 从 10000 号端口开始，用 `socket.connect_ex(("localhost", port))` 逐个试：返回非 0 表示「连不上」= 该端口当前空闲，加入候选；候选攒到 `num*30` 个就停止（多采一些再筛）；最后 `random.shuffle` 打乱并取前 `num` 个。打乱是为了避免所有实例都挤在最前面的端口。

**步骤 D：名解包到 args 字段。**

[lightllm/server/api_start.py:361-398](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_start.py#L361-L398) —— L361~372 用 Python 的元组解包，把返回数组的前 10 个端口一次性赋给 10 个语义化变量；L388~398 再把这些值写回 `args.nccl_port` / `args.router_port` / … / `args.metric_port` 等字段。这样后续每个子进程拿到的 `args` 里就已经带好了它该用的端口。注意 L373 之后剩余的端口还用来分配 visual_nccl_ports、audio_nccl_ports、pd_node_infer_rpyc_ports。

**命名空间隔离：唯一服务名。** 同机多实例时，除了端口，还有「共享内存 key」「zmq ipc 前缀」也会冲突，LightLLM 用一个全局唯一服务名来隔离：

[lightllm/utils/envs_utils.py:13-20](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/utils/envs_utils.py#L13-L20) —— `set_unique_server_name` 生成一个 16 位随机十六进制串加后缀，写入环境变量 `LIGHTLLM_UNIQUE_SERVICE_NAME_ID`。router 里诸如 `SharedInt(f"{get_unique_server_name()}_shm_max_total_token_num")`（[router/manager.py:65](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/manager.py#L65)）和 `args.zmq_mode` 的 ipc 前缀（[api_start.py:149-153](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_start.py#L149-L153)）都用它做前缀，保证两个实例互不串扰。

#### 4.3.4 代码实践

**实践目标**：分清「对外 HTTP 端口」与「内部 zmq 端口」，并理解端口的去向。

**操作步骤**：

1. 在 [api_start.py:361-372](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_start.py#L361-L372) 找到那 10 个端口变量名，记下 `http_server_port`。
2. 进 [detokenization/manager.py:30-36](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/detokenization/manager.py#L30-L36)，确认 detokenization 是 `bind` 在 `args.http_server_port` 上做 PUB。
3. 再回到 [api_start.py:494-499](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_start.py#L494-L499)，确认 hypercorn 绑的是 `args.port`（默认 8000）。

**需要观察的现象 / 预期结果**：你应该清楚地认识到——`args.port`（如 8000）是给用户 curl 的；而 `http_server_port` 是一个自动分配的内部端口，仅供 detokenization 与 hypercorn 工作进程之间用 zmq PUB/SUB 传生成结果。两者完全不同。

#### 4.3.5 小练习与答案

**练习 1**：`alloc_can_use_network_port` 是怎么判断一个端口「空闲」的？为什么不直接用 `bind` 来判断？

> **参考答案**：它用 `connect_ex(("localhost", port))` 去尝试**连接**该端口，返回非 0（连接失败）就认为端口当前没有服务在监听，即空闲（[net_utils.py:12-16](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/utils/net_utils.py#L12-L16)）。这种「反向探测」比直接 `bind` 更轻量，不会真的占用端口资源（bind+listen 会真正占住），适合一次性探测大量端口。

**练习 2**：如果不调用 `PortLocker.lock_port()`，最坏会发生什么？

> **参考答案**：从「探测到空闲」到「子进程真正 bind 该端口」之间有一段空窗。若此期间另一进程占用了同一端口，子进程 bind 时才报「Address already in use」，错误出现得晚、且堆栈指向子进程深处，难以定位是端口冲突。`PortLocker` 把端口在分配期间提前占住，让冲突在启动瞬间立刻暴露。

---

### 4.4 信号处理与优雅退出

#### 4.4.1 概念说明

启动起来一组协作进程（metric、router、detokenization、若干 model 子进程、hypercorn……）后，怎么**干净地停掉**它们是个真实问题。如果只是 `kill -9`，会留下一堆孤儿进程（尤其 model 进程占了大量 GPU 显存不释放），下次启动就 OOM。

LightLLM 在主进程注册了两个信号处理器，分别应对两种关闭意图：

- **SIGINT**（按 Ctrl+C 触发）：用户「我现在就要停」，处理策略是**强制**——立刻 kill http server，然后 `terminate_all_processes()` 杀掉所有子进程，立即退出。
- **SIGTERM**（`kill <pid>` 默认发的信号）：通常用于运维「请优雅关闭」，处理策略是**温和**——先给 hypercorn 发 SIGTERM 让它处理完在途请求，最多等 60 秒，再统一终止所有子进程。

注意：子进程的「自我了断」其实有两层保障——除了主进程这里的 `terminate_all_processes`，每个子进程入口还调了 `graceful_registry(...)`（你在前面 `start_router_process` 等里都见过）注册了自己的优雅退出逻辑。本讲聚焦主进程这一层。

#### 4.4.2 核心流程

```text
收到信号 ──┬── SIGINT（强制）
           │     kill_recursive(http_server_process)   # 立即杀 hypercorn
           │     process_manager.terminate_all_processes()  # 递归杀所有子进程
           │     sys.exit(0)
           │
           └── SIGTERM（优雅）
                 http_server_process.send_signal(SIGTERM)   # 请 hypercorn 自己退
                 while (未超 60s 且 http_server 仍活): sleep(1)  # 等它收尾
                 若超时: kill_recursive(http_server_process)
                 process_manager.terminate_all_processes()
                 sys.exit(0)
```

`terminate_all_processes` 内部对每个被托管的子进程做 `kill_recursive`：用 `psutil` 找出它的**所有子孙进程**（router 还会派生 model 进程，所以是树形），从叶子到根逐个 kill，确保不遗漏、不留孤儿。

#### 4.4.3 源码精读

[lightllm/server/api_start.py:33-71](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_start.py#L33-L71) —— 这是 `setup_signal_handlers`：
- L35~42：SIGINT 分支，`kill_recursive(http_server_process)` 后立即 `terminate_all_processes()` 并 `sys.exit(0)`；
- L43~63：SIGTERM 分支，先给 hypercorn 转发 SIGTERM（L46），然后用一个最多 60 秒的轮询循环（L48~53）等它退出；超时则强杀（L58~59）；最后同样 `terminate_all_processes()`；
- L65~66：用 `signal.signal` 把这个 handler 同时绑到 SIGTERM 和 SIGINT。

L69~70 还会把 http server 的 pid 打到日志里，方便排查。

再看 `terminate_all_processes` 怎么递归清理：

[lightllm/utils/start_utils.py:43-69](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/utils/start_utils.py#L43-L69) —— L46~56 定义内联的 `kill_recursive`：用 `psutil.Process(pid).children(recursive=True)` 拿到整棵进程树，**先 kill 子进程再 kill 父进程**；L58~61 对 `self.processes` 里每个还活着的子进程执行该清理并 `join`；L64~68 额外处理 MPS（若启用过则恢复 GPU 计算模式）。这种「先叶子后根」的清理顺序正是为了避免孤儿。

需要特别说明：hypercorn（`http_server_process`）是用 `subprocess.Popen` 起的**外部命令进程**，不在 `process_manager` 托管列表里，所以它由信号处理器单独 `kill_recursive` / `send_signal` 处理；而 metric/router/detokenization 等是 `mp.Process` 起的、被 `process_manager` 托管的，统一由 `terminate_all_processes()` 清理。两套清理路径分工明确。

#### 4.4.4 代码实践

**实践目标**：在脑中演练一次「优雅退出 vs 强制退出」的差异。

**操作步骤**：

1. 阅读 [api_start.py:35-63](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_start.py#L35-L63)，对比 SIGINT 与 SIGTERM 两个分支的差异。
2. 想象你在终端对一个运行中的 LightLLM 服务分别执行 `Ctrl+C` 与 `kill <主进程pid>`，预测两种情况下：
   - hypercorn 收到的信号分别是什么？
   - 是否会等待 in-flight 请求处理完？
   - 子进程被清理的时机。

**需要观察的现象 / 预期结果**：

- `Ctrl+C`（SIGINT）：hypercorn 被立即强杀，可能打断正在处理的请求；随后所有子进程被立即终止。
- `kill <pid>`（SIGTERM）：hypercorn 先收到 SIGTERM，有机会优雅收尾（最多等 60s）；之后再清理子进程。

> 待本地验证：真实行为可在本机启动服务后，分别用 `Ctrl+C` 和 `kill` 触发，观察日志中 `Received SIGINT ... forcing immediate exit` 与 `Received SIGTERM, shutting down gracefully...` 两类不同的日志行（[api_start.py:36,44](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_start.py#L36)）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `terminate_all_processes` 要用 `children(recursive=True)` 递归地找子进程，而不是只 kill 直接子进程？

> **参考答案**：因为进程是树形的——主进程拉起 router，router 又会拉起每个 GPU 上的 model 进程（见 [router/manager.py 的 start_model_process 调用](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/manager.py#L127)）。只 kill 直接子进程（router）会让 model 进程变成孤儿，继续占用 GPU 显存。递归清理（[start_utils.py:48-56](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/utils/start_utils.py#L48-L56)）才能把整棵树连根拔起。

**练习 2**：hypercorn 进程为什么不在 `process_manager.terminate_all_processes()` 里被清理？

> **参考答案**：因为 hypercorn 是用 `subprocess.Popen` 启动的外部命令进程（[api_start.py:512](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_start.py#L512)），没有被加入 `SubmoduleManager.processes` 列表，所以 `terminate_all_processes` 不管它。它由信号处理器单独通过 `kill_recursive` / `send_signal(SIGTERM)` 处理。两套机制各管一类进程。

---

## 5. 综合实践

**任务：绘制 normal 模式的「进程全景图」并把本讲四个模块串起来。**

请综合本讲内容，完成一张完整的「启动全景图」（可以画在纸上或用 Markdown 列表），要求体现以下要素：

1. **进程清单与触发条件**：列出 `normal_or_p_d_start` 拉起的所有进程（含 hypercorn），每个标注：proctitle 角色名、触发条件（无条件 / `enable_multimodal` / `enable_cpu_cache` 等）、启动顺序。
2. **端口连线**：把每个进程用到的端口连起来，重点画出 zmq 的 PULL/PUSH/PUB/SUB 关系：
   - 谁是 HTTP 入口（`args.port`，hypercorn）？
   - 谁绑 `router_port`、`detokenization_port`、`http_server_port`（内部 zmq 链路）？
   - metric 与 embed_cache 用的是 rpyc（不同于 zmq）。
3. **启动与退出时序**：
   - 标出「init ok 握手」发生在哪一步，以及它如何把启动过程变成顺序阻塞的。
   - 标出 `PortLocker` 占端口、`alloc_can_use_network_port` 探测端口、`release_port` 释放的时机。
   - 标出收到 SIGINT / SIGTERM 时，hypercorn 与各子进程分别走哪条清理路径。

**进阶（选做）**：阅读 `pd_master_start`（[api_start.py:528-590](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_start.py#L528-L590)），对比它和 `normal_or_p_d_start` 的差异——PD master 模式下还拉起哪些进程？不拉起哪些进程？为什么？（提示：PD master 是 PD 分离部署里的「调度大脑」，本身不做推理。）

> 说明：本实践为**源码阅读型综合实践**，目标是让你把「启动编排 / 子进程握手 / 端口分配 / 信号处理」四条线索在一张图里贯通；若要对照真实日志，可按 u1-l2 启动一次小模型服务，结合本讲引用的源码行号核对。

## 6. 本讲小结

- `normal_or_p_d_start` 是 `normal` / `prefill` / `decode` 三种模式的「总指挥」，把启动做成五阶段流水线：模式关卡 → 参数补全校验 → 端口分配 → 顺序拉起子进程 → 启动 hypercorn 并阻塞收尾。
- 子进程的拉起与就绪由 `process_manager`（`SubmoduleManager`）统一管理：用 `mp.Process` 开进程，用 `Pipe` + `"init ok"` 字符串做**启动期握手**，任一子进程初始化失败就整体中止。
- 启动顺序有依赖：metric 必须早于 router（router 初始化要连 metric）；router 与 detokenization 同批起；多模态/CPU 缓存等上游进程要先于 router 就位。
- 端口分配是「锁住已知端口 → 探测空闲端口 → 名解包到 args」三步；要严格区分**对外 HTTP 端口**（`args.port`，hypercorn）与**内部 zmq 端口**（`http_server_port` 等，自动分配）。
- 同机多实例通过 `set_unique_server_name` 生成的唯一服务名，给共享内存 key、zmq ipc 前缀做**命名空间隔离**。
- 信号处理区分 SIGINT（强制立即退出）与 SIGTERM（优雅等待 hypercorn 收尾最多 60s 再退出）；`terminate_all_processes` 用 psutil 递归清理整棵进程树，不留孤儿；hypercorn 作为外部命令进程走单独的清理路径。

## 7. 下一步学习建议

到这里，你已经知道 LightLLM **启动时发生了什么**——哪些进程被拉起、它们各自绑了什么端口、怎么握手、怎么退出。但每个进程**内部**是怎么运转的、一次推理请求是怎么在这些进程之间流转的，我们还没展开。建议接下来：

1. **第二单元 u2-l1（多进程架构总览）**：用官方架构图把本讲拉起的这些进程串成一张「请求数据流图」，看清 zmq/rpyc/共享内存分别承担哪段通信。
2. **u2-l2（HTTP API 服务与请求分发）**：从 hypercorn 加载的 `api_http.py` 切入，看一个请求进来后怎么被分发给 router 或 visual。
3. 想提前感受各进程内部细节的，可以直接读：
   - [router/manager.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/manager.py) 的 `loop_for_fwd`（router 调度主循环，u2-l5 详讲）；
   - [detokenization/manager.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/detokenization/manager.py) 的 `handle_loop`（反 token 化主循环，u2-l7 详讲）。
