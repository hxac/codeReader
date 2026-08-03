# 阶段进程与运行时：StageEngineCoreProc 与 StageRuntime

## 1. 本讲目标

在 [u3-l1](u3-l1-async-omni-architecture.md) 我们画出了五层架构，在 [u3-l2](u3-l2-orchestrator.md) 我们看清了 Orchestrator 如何在阶段之间前推请求。但有个问题一直被「悬置」：**那些被编排的 stage，到底是什么？它们住在哪里？请求是怎么从主进程送进去的？**

本讲就把这层「悬置」拆开。读完本讲，你应当能够：

1. 说清楚一个 **stage（阶段）** 为什么是一个**独立的 EngineCore 子进程**，以及这个子进程是怎么被拉起来的（`StageEngineCoreProc.run_stage_core`）。
2. 理解主进程一侧的「工厂」：`StageRuntime`（单机）与 `DistStageRuntime`（分布式）如何根据配置，为每个 stage 拉起进程、握手、并把客户端装进 `StagePool`。
3. 掌握 `StageEngineCoreClient` 如何用 ZMQ + msgpack 把一条请求送进子进程、再从子进程把输出捞回来（`add_request_async` / `get_output_async`）。
4. 能够独立梳理「进程从启动到收到第一条请求」的完整初始化链路，并标注每一步发生在哪个类里。

本讲是 [u3-l4（OmniConnector）](u3-l4-omni-connectors.md) 的前置：阶段之间能解耦传输，前提是每个阶段先把「自己是一个可寻址的进程」这件事建立起来。

## 2. 前置知识

阅读本讲前，建议你已经掌握以下概念（若不熟悉，可先回看 u1-l3、u3-l1、u3-l2）：

- **stage（阶段）**：vLLM-Omni 把一个全模态请求拆成若干顺序子任务，每个子任务就是一个 stage。例如 Qwen3-Omni 的 Thinker → Talker → Code2wav 三段，就是三个 stage。
- **副本（replica）**：同一个 stage 可以有多个副本并行服务，用来横向扩容。副本之间互不共享权重，各自是独立进程。
- **EngineCore**：vLLM v1 架构里「真正干推理」的核心对象，跑在子进程里，含 Scheduler、Executor、Worker。vLLM-Omni 不重写它，而是**继承**它。
- **ZMQ + msgpack**：进程间通信的组合拳。ZMQ 负责传消息（ROUTER/PULL 等 socket 模式），msgpack 负责把 Python 对象序列化成字节流。
- **janus.Queue**：一个「同步端 + 异步端」的队列，用来在主线程（同步）和 Orchestrator 后台线程（异步）之间桥接消息。u3-l2 已讲过。
- **monkey-patch（猴子补丁）**：在运行时替换某个模块里的对象。本讲会看到 vLLM-Omni 用它把 vLLM 的 `EngineCoreRequest` 换成自家的 `OmniEngineCoreRequest`。

一句话定位本讲：u3-l2 讲的是「**横向**」——一个请求如何在多个 stage 之间流转；本讲讲的是「**纵向**」——主进程如何把某一个 stage 的进程实体建立起来，并把请求垂直地送进去。

## 3. 本讲源码地图

本讲涉及四个核心文件，全部位于 `vllm_omni/engine/` 下：

| 文件 | 作用 | 本讲角色 |
| --- | --- | --- |
| `stage_engine_core_proc.py` | 定义 **子进程入口** `StageEngineCoreProc` 及其静态方法 `run_stage_core` | 「住在子进程里」的那一端 |
| `stage_engine_core_proc_manager.py` | 定义 `StageEngineCoreProcManager`，负责**派生**（spawn）上述子进程 | 「负责拉起进程」的工厂 |
| `stage_runtime.py` | 定义 `StageRuntime` / `DistStageRuntime` 及两个数据类 `StageRuntimeInfo` / `StageRemoteFactoryContext`，外加工厂函数 `create_stage_runtime` | 「主进程一侧的编排者」，串联启动全过程 |
| `stage_client.py` | 定义 `StageClient` 等 **Protocol（协议接口）**，规定 stage 客户端长什么样 | 「客户端的契约」 |
| `stage_engine_core_client.py`（辅助） | 定义 `StageEngineCoreClientBase` 与具体实现 `StageEngineCoreClient` / `DPLBStageEngineCoreClient` | 「真正发请求、收输出」的客户端实现 |

> 说明：`stage_engine_core_client.py` 虽然没列在本讲规格的 `source_files` 里，但它是「请求投递」最小模块的真正实现所在，必须一起讲。`stage_client.py` 只是它的接口契约。

一个全景比喻：`StageRuntime` 是「包工头」，它读图纸（stage 配置），决定要建几个 stage、每个几份副本；`StageEngineCoreProcManager` 是「施工队」，负责把每个副本的进程真正 `fork`/`spawn` 出来；`StageEngineCoreProc.run_stage_core` 是「房子建成后住户的开门第一天」，进程在这里完成自我初始化并进入忙循环；`StageEngineCoreClient` 是「主进程对这间房子的对讲机」，请求通过对讲机送进去、结果通过对讲机传回来。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1** 子进程那一端：`StageEngineCoreProc` 的 `run_stage_core` 与忙循环。
- **4.2** 主进程那一端：`StageRuntime`、`StageRuntimeInfo`、`StageRemoteFactoryContext` 与单机/分布式工厂。
- **4.3** 通信对讲机：`StageClient` 契约与 `StageEngineCoreClient` 的 `add_request_async` / `get_output_async`。

### 4.1 子进程入口：StageEngineCoreProc 的 run_stage_core 与忙循环

#### 4.1.1 概念说明

先建立直觉：为什么 stage 要是「独立进程」？

设想 Qwen3-Omni 的三个阶段 Thinker / Talker / Code2wav。如果把它们塞进同一个进程，会遇到三个麻烦：

1. **显存隔离**：三个模型都想占 GPU，混在一起难以精确分配显存、也容易互相抢占。
2. **故障隔离**：一个阶段崩溃不应拖垮其它阶段。进程隔离让「谁崩谁死」。
3. **横向扩容**：希望某个慢的阶段（比如 Code2wav）多开几份副本，这天然是「多进程」语义。

vLLM v1 的 `EngineCoreProc` 已经是「一个 EngineCore 跑在子进程里」的设计。vLLM-Omni 的策略很轻：**继承它，但不调用它的入口**。文件开头就把这层意图说清楚了：

> `StageEngineCoreProc` inherits from vLLM's `EngineCoreProc` and runs the engine core busy loop in a subprocess, communicating with `StageEngineCoreClient` via ZMQ. … Does **not** delegate to `EngineCoreProc.run_engine_core()`.

也就是说，vLLM 自带的启动入口叫 `run_engine_core`，而 omni 不用它，而是自己写了一个 `run_stage_core`，这样可以在子进程启动的最早时机，注入 omni 专属的东西（stage_id、replica_id、协调器地址、以及一个能携带多模态载荷的请求类型）。

#### 4.1.2 核心流程

`run_stage_core` 是一个**静态方法**，被当作 multiprocessing 的 `target` 来执行——它就是子进程的 `main`。它的执行流程可以概括为「**准备环境 → 构造 EngineCore → 装上信号处理 → 进入忙循环**」四步：

```text
run_stage_core()                         # 子进程入口（静态方法）
│
├─ 1. 准备环境
│    ├─ 注册 reasoning parser（fork 出的子进程 ReasoningParserManager 是空的）
│    ├─ set_death_signal(SIGTERM)        # 父进程死时，子进程自动收到 SIGTERM
│    ├─ set_process_title(...)           # 方便在 ps/top 里认出是哪个 stage 哪个副本
│    └─ 设置 FLASHINFER_DISABLE_VERSION_CHECK 等 env
│
├─ 2. 关键补丁（必须在 __init__ 之前！）
│    └─ EngineCoreRequest := OmniEngineCoreRequest   # 让 IO 线程能解码 omni 载荷
│
├─ 3. 构造 EngineCore
│    ├─ maybe_apply_audex_cfg_patches()  # 在 Scheduler 构造前注入 CFG 补丁
│    └─ engine_core = StageEngineCoreProc(...)
│         └─ （继承自 vLLM）握手 → 起 IO 线程 → 建 Scheduler/Executor/Worker
│
├─ 4. （可选）挂上协调器心跳客户端
│    └─ coord_client = create_stage_coord_client(...)
│
├─ 5. 装信号处理（SIGTERM/SIGINT → 标记 shutdown 并唤醒）
│
└─ 6. engine_core.run_busy_loop()        # 进入「拉取输入 → 执行 → 推送输出」的死循环
```

其中最关键的两个工程要点：

- **「先补丁，后构造」**：第 2 步把请求类型替换掉，**必须发生在第 3 步构造 EngineCore 之前**。原因是 vLLM 的 `EngineCoreProc.__init__` 会在内部创建一个 `MsgpackDecoder(EngineCoreRequest)` 的 IO 线程；如果到那时 `EngineCoreRequest` 还是 vLLM 原版（不含 omni 的 `additional_information` 等字段），子进程就解码不了主进程发来的 omni 请求。
- **忙循环不是 omni 写的**：第 6 步的 `run_busy_loop()` 继承自上游 vLLM 的 `EngineCoreProc`，它才是真正「不停从输入 socket 拉请求、喂给 Scheduler/Executor 执行、把输出推到输出 socket」的循环。omni 只是**调用**它，不重写它。

#### 4.1.3 源码精读

先看类本身的声明与它对上游入口的态度。`StageEngineCoreProc` 继承 `EngineCoreProc`，并明确强调「不委托给 `run_engine_core`」：

[stage_engine_core_proc.py:49-55](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/stage_engine_core_proc.py#L49-L55) — 类声明，说明它继承 vLLM 的 `EngineCoreProc`，并提供自己的 `run_stage_core` 入口，**不**走上游 `run_engine_core`。

这个类只重写了一个业务方法 `preprocess_add_request`，作用是「在 vLLM 把请求交给 Scheduler 之前，把 omni 的载荷粘回去」：

[stage_engine_core_proc.py:57-62](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/stage_engine_core_proc.py#L57-L62) — `preprocess_add_request` 调用父类构造 scheduler 请求后，把 `additional_information` 与 `external_req_id` 补到请求上。没有它，跨阶段的多模态载荷会在子进程入口被丢掉。

下面是 `run_stage_core` 的签名与文档，它额外接收 4 个 omni 专属参数：

[stage_engine_core_proc.py:64-88](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/stage_engine_core_proc.py#L64-L88) — 静态方法签名与文档，参数含义：`dp_rank`/`local_dp_rank` 是 vLLM 数据并行 rank；`omni_coordinator_address` 是分布式协调器的 ROUTER 地址；`omni_stage_id` 是本副本所属的逻辑阶段号；`omni_replica_id` 是副本号。

接着是「准备环境」一段，注意它做了几件**健壮性**的事：

[stage_engine_core_proc.py:114-123](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/stage_engine_core_proc.py#L114-L123) — 设置进程标题、`set_death_signal`、以及若干环境变量。其中 `VLLM_OMNI_REPLICA_ID` 写进环境，方便后续组件（如 KV 连接器）读取副本号。

然后是**最关键的补丁时序**，注释把「为什么必须现在」讲得很直白：

[stage_engine_core_proc.py:125-135](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/stage_engine_core_proc.py#L125-L135) — 在 `__init__` **之前**把模块级 `EngineCoreRequest` 替换为 `OmniEngineCoreRequest`，否则 `__init__` 里启动的 IO 线程会用原版类型解码，导致 omni 字段丢失。

随后构造 EngineCore 并（可选地）挂上协调器心跳客户端：

[stage_engine_core_proc.py:141-145](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/stage_engine_core_proc.py#L141-L145) — 真正构造 `StageEngineCoreProc` 实例，`engine_index=dp_rank` 让它知道自己属于第几个数据并行 rank。

[stage_engine_core_proc.py:150-168](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/stage_engine_core_proc.py#L150-L168) — 若提供了协调器地址，则创建 `OmniCoordClientForStage`，心跳里携带 `queue_length`（取自 `scheduler.get_num_unfinished_requests`），供协调器做负载均衡（详见 [u3-l5](u3-l5-omni-coordinator.md)）。

信号处理与忙循环是子进程「正式上班」的标志：

[stage_engine_core_proc.py:170-183](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/stage_engine_core_proc.py#L170-L183) — 定义 `wakeup_engine`（往输入队列塞一条 WAKEUP 信号）与 `signal_handler`（收到终止信号则标记 shutdown、唤醒、抛 `SystemExit`），注册到 SIGTERM/SIGINT，最后进入 `run_busy_loop()`。这个忙循环继承自上游 vLLM 的 `EngineCoreProc`，是子进程真正干活的死循环（精确行号属上游 vLLM，此处待确认）。

最后，无论正常退出还是异常，`finally` 都要把信号还原、停掉心跳客户端、关闭 EngineCore：

[stage_engine_core_proc.py:195-204](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/stage_engine_core_proc.py#L195-L204) — 清理逻辑。异常时若 EngineCore 已建好，会调 `_send_engine_dead()` 让主进程的客户端感知到「这个 stage 死了」（对应 4.3 的 `check_health`）。

#### 4.1.4 代码实践

**实践目标**：理解「先补丁后构造」这条铁律的重要性。

**操作步骤**（源码阅读型实践）：

1. 打开 `vllm_omni/engine/stage_engine_core_proc.py`，定位 4.1.3 引用的第 125–135 行。
2. 回答：如果把第 131 行 `_vllm_engine_core_module.EngineCoreRequest = OmniEngineCoreRequest` 移到第 141 行 `engine_core = StageEngineCoreProc(...)` **之后**，会发生什么？
3. 提示：`EngineCoreProc.__init__`（上游 vLLM）在构造时会启动一个 IO 线程，该线程用「构造那一刻」的 `EngineCoreRequest` 创建 `MsgpackDecoder`。

**需要观察的现象**：子进程能正常启动（不会立刻崩溃），但一旦主进程发来携带 `additional_information` 的 omni 请求，IO 线程解码时会因为字段对不上而报错或丢弃字段。

**预期结果**：你应当得出结论——这条赋值必须在 `__init__` 之前执行，否则 omni 的跨阶段载荷在子进程入口处就被「洗掉」了。这是典型的「**时序耦合**」：补丁的对象是模块全局变量，而它的消费者（IO 线程）在 `__init__` 里就被冻结了。

> 本实践为源码阅读型，无需运行；若想验证，可在 4.5 综合实践里复现。

#### 4.1.5 小练习与答案

**练习 1**：`run_stage_core` 为什么是 `@staticmethod` 而不是普通方法或类方法？

> **参考答案**：因为它要作为 multiprocessing 的 `target` 被调用。multiprocessing 在子进程里通过 `target(*args, **kwargs)` 调用它，调用时还没有任何「实例」存在（实例是在方法体内才 `StageEngineCoreProc(...)` 构造出来的）。`@staticmethod` 让它无需 `self`/`cls` 即可被直接引用为 `StageEngineCoreProc.run_stage_core`，正好满足 `target=` 的签名要求。

**练习 2**：`set_death_signal(signal.SIGTERM)` 解决了什么问题？

> **参考答案**：它让子进程在父进程（主进程）意外死亡时，自动收到 `SIGTERM` 而退出，避免「主进程挂了、stage 子进程还残留占着 GPU」的孤儿进程问题。它用 Linux 的 `prctl(PR_SET_PDEATHSIG)` 实现，仅 Linux 有效。

---

### 4.2 主进程一侧的编排者：StageRuntime 与两个数据类

#### 4.2.1 概念说明

4.1 讲的是「**被拉起的**子进程内部」长什么样。但子进程不会自己凭空出现——需要有谁来决定「拉起几个、每个用哪些 GPU、用什么配置」。这就是 `StageRuntime` 的职责。

`StageRuntime` 有两个实现，对应两种部署形态：

- **`StageRuntime`（单机模式）**：所有 stage 都在同一台机器上，主进程直接 `spawn` 子进程，并创建「静态」客户端。没有协调器、没有 master server、没有 hub。
- **`DistStageRuntime`（分布式模式 / single_stage_mode）**：跨节点部署。它额外引入 `OmniCoordinatorRuntime`（独立进程）、`OmniMasterServer`（副本注册）、支持**远程副本**，以及通过 `MembershipController` 实现**动态成员**（运行时新增/摘除副本）。

两者由工厂函数 `create_stage_runtime` 根据 `single_stage_mode` 开关选择。

本模块还要讲两个**小而关键的数据类**，它们是「主进程对某个 stage 的认知摘要」：

- **`StageRuntimeInfo`**：一个 `frozen`（不可变）dataclass，描述某个 stage 的**输出角色**——它是不是最终阶段、最终输出什么模态、stage 类型。它被 `StagePool` 等运行时组件用来判断「这个 stage 的输出要不要交给用户」。
- **`StageRemoteFactoryContext`**：分布式专属，是「为远程副本创建主进程侧客户端」所需的**上下文打包**——把 stage_id、stage_type、配置、vllm_config、executor_class、batch_size 等打成一个对象，这样动态注册进来的远程副本，能用同一份模板生成客户端。

#### 4.2.2 核心流程

`StageRuntime.initialize()` 是主进程一侧的启动总入口，它分三个阶段把所有副本建好：

```text
StageRuntime.initialize()
│
├─ _prepare_stage_plans()
│    ├─ compute_replica_layout()        # 每个 stage 几个副本、各用哪些设备
│    ├─ prepare_engine_environment()    # 加载插件、设 spawn method
│    └─ _build_logical_stage_init_plans()  # 产出 LogicalStageInitPlan 列表
│         └─ 每个 stage → 若干 ReplicaInitPlan（含 vllm_config / executor_class）
│
├─ _initialize_stage_replicas()         # 并行/串行地把每个副本拉起来
│    ├─ 按 _replica_init_group_key() 分组：
│    │    ├─ "inline:diffusion"          # 本地 diffusion：必须在 orchestrator 线程串行
│    │    ├─ "remote:<stage>:<replica>"  # 远程副本：各自一组
│    │    └─ "device:<devices>"          # 本地 LLM：按设备分组，同组串行、跨组并行
│    └─ 每个副本走三条分支之一：
│         ├─ _initialize_remote_replica()       # DistStageRuntime 才实现
│         ├─ _initialize_local_diffusion_replica()
│         └─ _initialize_local_llm_replica()
│              └─ launch_stage_replica()         # 内部用 StageEngineCoreProcManager spawn 进程
│              └─ StageEngineCoreClientBase.make_async_mp_client()  # 创建客户端
│
└─ _finalize_initialized_stages()
     └─ _assemble_stage_pools()          # 把每组的客户端装进 StagePool
```

几个**调度细节**值得记住：

- **同 GPU 串行、跨 GPU 并行**：共享同一组 GPU 的副本必须**串行**初始化，否则会因为显存探测互相干扰；不同 GPU 上的副本可以**并行**初始化。这是用 `_replica_init_group_key` 按 `device:<设备>` 分组实现的。
- **本地 diffusion 必须串行**：`"inline:diffusion"` 组把所有本地 diffusion 副本放在一个串行组里，因为它们的 spawn 必须留在 orchestrator 线程。
- **`_replica_launch_lock`**：所有 LLM 副本的「spawn + 握手」还被一把全局锁串起来，防止多个子进程同时分配 ZMQ 端口、同时初始化 CUDA 上下文造成冲突。
- **失败清理很讲究**：如果某个副本初始化失败，`initialize()` 会收集所有**已经建好的**客户端，逐一 `shutdown()`，避免泄漏进程/显存。

#### 4.2.3 源码精读

先看两个数据类。`StageRuntimeInfo` 极简，只描述输出角色：

[stage_runtime.py:71-77](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/stage_runtime.py#L71-L77) — `StageRuntimeInfo`，`frozen=True, slots=True` 的不可变数据类。字段含义：`final_output`（是否最终阶段）、`final_output_type`（最终输出模态，如 text/image/audio）、`stage_type`（llm 或 diffusion）、可选 `model_stage`（如 thinker/talker）。

再看远程工厂上下文，它比 Info「重」得多，因为它要支撑动态注册：

[stage_runtime.py:79-91](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/stage_runtime.py#L79-L91) — `StageRemoteFactoryContext`，把创建一个远程副本客户端所需的全部材料打包：`stage_id`、`stage_type`、`stage_cfg`、`base_metadata`、`vllm_config`、`executor_class`、`diffusion_batch_size`。

接着是 `StageRuntime` 类的定位——它明确「单机、无协调器、直接拉进程」：

[stage_runtime.py:112-117](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/stage_runtime.py#L112-L117) — `StageRuntime` 类文档：单机模式，无 coordinator、无 master server、无 hub，直接拉起 stage 进程并用静态客户端构造 `StagePool`。

启动总入口 `initialize()` 采用「准备 → 初始化 → 收尾」三段式，并带失败清理：

[stage_runtime.py:232-259](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/stage_runtime.py#L232-L259) — `initialize()`：先 `_prepare_stage_plans`，再 `_initialize_stage_replicas`，最后 `_finalize_initialized_stages`。异常路径会把已初始化的客户端收集起来逐一关闭。

分组并行的调度逻辑是本模块的「调度心脏」：

[stage_runtime.py:434-506](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/stage_runtime.py#L434-L506) — `_initialize_stage_replicas`：按 `_replica_init_group_key` 把副本分进若干调度组；`inline:` 组先在当前线程串行跑；剩余组若只有 1 个就内联执行，否则用 `ThreadPoolExecutor` 并行跑；首个异常会通过 `primary_exc` 短路其余组。

[stage_runtime.py:508-519](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/stage_runtime.py#L508-L519) — `_replica_init_group_key`：本地 diffusion → `"inline:diffusion"`；远程副本 → `"remote:<stage>:<replica>"`；其余 → `"device:<设备串>"`。这就是「同 GPU 串行、跨 GPU 并行」的判据。

副本的三条分支分发：

[stage_runtime.py:521-530](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/stage_runtime.py#L521-L530) — `_initialize_replica` 按 `launch_mode` 与 `stage_type` 分流到 remote / local diffusion / local llm 三条路径。

本地 LLM 副本的初始化（本讲最重要的「拉起 + 握手 + 建客户端」链路）：

[stage_runtime.py:540-627](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/stage_runtime.py#L540-L627) — `_initialize_local_llm_replica`：解析物理设备 → `acquire_device_locks`（设备级文件锁，防同时初始化）→ 在 `_replica_launch_lock` 保护下用 `launch_stage_replica`（内部用 `StageEngineCoreProcManager` spawn 进程）→ 用返回的 ZMQ 地址经 `StageEngineCoreClientBase.make_async_mp_client` 创建客户端；失败时根据客户端是否已建分别走 client.shutdown / 资源清理。

> 注意第 596–604 行：客户端的地址来自 `resources.addresses`（子进程握手后回报的 ZMQ 端口），这正是 4.1 里 `run_stage_core` 完成握手后填进 `engine_core.addresses` 的同一份数据。

本地 diffusion 副本走另一条路径（diffusion 不用 `StageEngineCoreProcManager`，而是用 `launch_diffusion_stage_replica`）：

[stage_runtime.py:636-688](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/stage_runtime.py#L636-L688) — `_initialize_local_diffusion_replica`：注入 KV connector 配置后，调用 `launch_diffusion_stage_replica`；当「全流水线只有 1 个 stage 且只有 1 个副本」时可走 `use_inline` 内联模式（不开子进程）。diffusion stage 的运行时由 U5 专讲。

现在看分布式分支。`DistStageRuntime` 的关键在于「本地 vs 远程」的判定：

[stage_runtime.py:852-855](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/stage_runtime.py#L852-L855) — `DistStageRuntime._get_launch_mode`：若设置了 `single_stage_id_filter` 且当前 stage 不等于它，则该 stage 视为「远程」（不本地拉起，而是等待远端注册）；否则为「本地」。

远程副本靠 master server 的握手拿地址，再用 `StageRemoteFactoryContext` 建客户端：

[stage_runtime.py:865-898](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/stage_runtime.py#L865-L898) — `_initialize_remote_replica`：从 `OmniMasterServer` 取回远端注册时上报的 stage 配置，构造 `StageRemoteFactoryContext`，再交给 `_create_remote_replica_client` 建客户端。

[stage_runtime.py:981-1076](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/stage_runtime.py#L981-L1076) — `_create_remote_replica_client`：diffusion 与 LLM 两条子路径。LLM 远程会 `connect_remote_engine_cores` 握手、注入 `replica_host`（KV 连接器真正绑定的远端 IP），再 `make_async_mp_client`。**这个方法同时服务「初始远程槽位」和「运行时动态注册的副本」**——这就是 `StageRemoteFactoryContext` 存在的意义。

最后是工厂函数，用 `single_stage_mode` 一眼分流：

[stage_runtime.py:1084-1134](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/stage_runtime.py#L1084-L1134) — `create_stage_runtime`：`single_stage_mode=True` 且提供了 master 地址/端口则返回 `DistStageRuntime`，否则返回 `StageRuntime`。这是「主进程一侧」选择单机/分布式运行时的唯一入口。

#### 4.2.4 代码实践

**实践目标**：理解「同 GPU 串行、跨 GPU 并行」这条调度规则。

**操作步骤**（源码阅读型实践）：

1. 打开 `vllm_omni/engine/stage_runtime.py`，阅读 `_replica_init_group_key`（第 508–519 行）与 `_initialize_stage_replicas`（第 434–506 行）。
2. 假设有如下部署：stage 0（Thinker，1 副本，GPU 0）、stage 1（Talker，1 副本，GPU 0）、stage 2（Code2wav，1 副本，GPU 1）。请写出每个副本的 `group key`，并判断哪些副本会串行、哪些会并行。
3. 再假设 stage 1 改成 2 副本（GPU 1、GPU 2），重新判断。

**需要观察的现象**：你会看到「共享同一设备串的副本」被分进同一个 `device:` 组，从而串行初始化；不同设备串的副本进入不同组，从而并行初始化。

**预期结果**：

- 场景一：stage 0 与 stage 1 都在 GPU 0 → 同属 `device:0`（假设设备串为 `"0"`），**串行**；stage 2 在 GPU 1 → `device:1`，与前者**并行**。
- 场景二：stage 1 的两个副本分别在 GPU 1、GPU 2 → 形成 `device:1` 与 `device:2` 两个组，彼此**并行**。

> 结论：分组键的本质是「会不会抢同一块 GPU」。这正是为了规避显存探测冲突。

#### 4.2.5 小练习与答案

**练习 1**：`StageRuntimeInfo` 为什么用 `frozen=True`？

> **参考答案**：因为它是对 stage 输出角色的「**摘要快照**」，初始化后不应被运行时随意修改（否则 `StagePool` 等组件看到的 `final_output`/`final_output_type` 会与实际不符）。`frozen=True` 让它不可变、可哈希，避免被意外篡改；`slots=True` 进一步省内存。

**练习 2**：`StageRemoteFactoryContext` 与 `StageRuntimeInfo` 的定位差别是什么？

> **参考答案**：`StageRuntimeInfo` 是「**轻量的运行时摘要**」，描述一个 stage 的输出角色，几乎所有组件都用它；`StageRemoteFactoryContext` 是「**重量级的建客户端材料包**」，**仅分布式场景**用，专门为了让「运行时新注册的远程副本」能复用同一份模板生成客户端。前者描述「是什么」，后者描述「怎么造一个客户端」。

---

### 4.3 通信对讲机：StageClient 契约与 add_request_async / get_output_async

#### 4.3.1 概念说明

4.1 建好了子进程，4.2 把它装进了 `StagePool`。现在主进程要往子进程送请求、从子进程收结果——这件事由**客户端**完成。

vLLM-Omni 在这里也走「**不重写、只继承**」的路线：它直接继承 vLLM 的 `AsyncMPClient`（异步多进程客户端），复用其 ZMQ 连接、`outputs_queue`、输出后台任务等全部基建，只在外层包一层 stage 感知逻辑。架构文档把这一层叫 **Communication Layer**：

> `StageEngineCoreClient` • ZMQ ROUTER / PULL • Msgpack codec

注意三个层次的概念关系，别混淆：

- **`StageClient`（Protocol）**：一个**接口契约**（Python `Protocol`），规定「任何 stage 客户端必须暴露哪些属性和方法」。它是给静态类型检查和文档用的，本身不干活。
- **`StageEngineCoreClientBase`**：实现 stage 感知逻辑的**基类**（存元数据、改输出解码类型、处理上游输入等），但不包含传输实现。
- **`StageEngineCoreClient` / `DPLBStageEngineCoreClient`**：用**多重继承**把「stage 感知基类」和「vLLM 传输客户端」拼在一起：`class StageEngineCoreClient(StageEngineCoreClientBase, AsyncMPClient)`。这样 `add_request_async` / `get_output_async` 这些方法直接来自 `AsyncMPClient`，无需 omni 重写。

#### 4.3.2 核心流程

一次「主进程 → stage 子进程 → 主进程」的往返，数据流如下：

```text
主进程                                          stage 子进程
──────                                          ───────────
StageEngineCoreClient.add_request_async(req)
   │  （继承自 AsyncMPClient）
   ├─ msgpack 序列化 OmniEngineCoreRequest
   ├─ 经 ZMQ PULL socket 发送 ────────────────▶  IO 线程收到字节
   │                                                ├─ MsgpackDecoder(OmniEngineCoreRequest) 解码
   │                                                └─ 塞进 engine_core.input_queue
   │                                                     │
   │                                                     ▼
   │                                              run_busy_loop() 拉取
   │                                                ├─ preprocess_add_request（粘回 omni 载荷）
   │                                                ├─ scheduler.schedule()
   │                                                └─ executor.execute_model()  ← 真正推理
   │                                                     │
   │  ◀────────────  ZMQ PUSH socket 推送输出 ───────┘  output handler 序列化输出
   │
get_output_async()  （继承自 AsyncMPClient）
   └─ 从 outputs_queue 取 EngineCoreOutputs
        （已用 OmniEngineCoreOutputs 解码，含 multimodal_output）
```

两个关键工程细节：

- **输出类型也要打补丁**：和 4.1 替换请求类型对称，客户端这边要把 `EngineCoreOutputs` 换成 `OmniEngineCoreOutputs`（携带每个输出的多模态字段），且同样**必须在 `super().__init__()` 创建解码器之前**完成。
- **`check_health` 桥接 HTTP `/health`**：客户端把「子进程死活」暴露成 `check_health()`，向上被 `OmniBase.check_health()` 调用，最终服务 `/health` 端点。子进程死亡时（4.1 的 `_send_engine_dead`）会置位 `engine_dead`，这里就抛 `EngineDeadError`。

#### 4.3.3 源码精读

先看接口契约。`StageClient` 规定了所有 stage 客户端必须具备的公共属性与生命周期方法：

[stage_client.py:23-46](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/stage_client.py#L23-L46) — `StageClient` Protocol：暴露 `stage_id`、`replica_id`、`stage_type`、`model_stage`、`final_output`、`final_output_type`、`default_sampling_params`、`engine_input_source` 等属性，以及 `check_health()` / `shutdown()` 方法。这是 Orchestrator 与 entrypoint 代码消费的「公共表面」。

[stage_client.py:73-94](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/stage_client.py#L73-L94) — `StagePoolLLMClient` 在 `StageClient` 之上，规定了 LLM 类 stage 的池化 API：`add_request_async(EngineCoreRequest)`、`get_output_async() -> EngineCoreOutputs`、`set_engine_outputs`、`process_engine_inputs`、`get_kv_sender_info`。**注意 `add_request_async` / `get_output_async` 在这里只是「签名声明」，真正实现来自 `AsyncMPClient`。**

再看客户端基类与工厂方法。`make_async_mp_client` 根据 DP 模式选具体类：

[stage_engine_core_client.py:79-108](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/stage_engine_core_client.py#L79-L108) — `make_async_mp_client`：当 `data_parallel_size > 1` 且非外部 LB 时返回 `DPLBStageEngineCoreClient`（带 DP 负载均衡），否则返回 `StageEngineCoreClient`。这是 4.2 里 `_initialize_local_llm_replica` 创建客户端时调用的入口。

构造函数里同样有「先补丁后构造」的对称时序（这次是输出类型）：

[stage_engine_core_client.py:164-186](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/stage_engine_core_client.py#L164-L186) — 在 `super().__init__()` **之前**把 `EngineCoreOutput(s)` 替换为 `OmniEngineCoreOutput(s)`，否则客户端后台解码器会用原版类型，丢失多模态输出。随后才调用父类 `AsyncMPClient.__init__` 完成 ZMQ 连接等重活。

健康检查把子进程死活向上暴露：

[stage_engine_core_client.py:220-227](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/stage_engine_core_client.py#L220-L227) — `check_health`：若 `self.resources.engine_dead` 为真，抛 `EngineDeadError`。它被 `OmniBase.check_health()` 及 `/health` HTTP 端点间接调用。这与 4.1 子进程异常时调 `_send_engine_dead()` 一一对应。

最后是请求投递本身——注意它只是「打个日志再委托父类」：

[stage_engine_core_client.py:231-240](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/stage_engine_core_client.py#L231-L240) — `add_request_async`：记录调试日志后直接 `await super().add_request_async(request)`，真正的「msgpack 序列化 + ZMQ 发送」来自 `AsyncMPClient`。这正是「不重写、只继承」的体现。

`get_output_async` 在本文件里**没有出现**，因为它完全继承自 `AsyncMPClient`，无需 omni 改写——这本身就是一条重要信息。

另外，`process_engine_inputs` 是「把上游 stage 的输出转成本 stage 的输入」的关键方法，服务于跨阶段前推：

[stage_engine_core_client.py:385-413](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/stage_engine_core_client.py#L385-L413) — `process_engine_inputs`：若有自定义处理函数则调用之，否则走 `_default_process_engine_inputs`，从上游输出的 `token_ids` 构造 `OmniTokensPrompt`。这是 u3-l2 里 `_forward_to_next_stage` 最终落到的「下一阶段吃上一阶段输出」的实现点。

最底下是两个具体类的多重继承组合：

[stage_engine_core_client.py:436-441](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/stage_engine_core_client.py#L436-L441) — `StageEngineCoreClient(StageEngineCoreClientBase, AsyncMPClient)` 与 `DPLBStageEngineCoreClient(StageEngineCoreClientBase, DPLBAsyncMPClient)`：用多重继承把「stage 感知」与「vLLM 传输」拼到一起。`add_request_async`/`get_output_async` 等方法解析（MRO）到 `AsyncMPClient`/`DPLBAsyncMPClient`，省去重写。

#### 4.3.4 代码实践

**实践目标**：验证「输出类型补丁」与请求/输出往返的对称性。

**操作步骤**（源码阅读型实践）：

1. 在 `stage_engine_core_client.py` 中搜索 `get_output_async`，确认它**没有**被定义（完全继承自 `AsyncMPClient`）。
2. 对照 4.1 的「请求类型补丁」（`stage_engine_core_proc.py` 第 131 行）与 4.3 的「输出类型补丁」（`stage_engine_core_client.py` 第 170–172 行），用一句话写出两者的对称关系。
3. 思考：为什么请求类型补丁在**子进程**那一端，而输出类型补丁在**主进程客户端**这一端？

**需要观察的现象**：请求由主进程客户端发出、子进程解码；输出由子进程发出、主进程客户端解码。两端各自只管自己要解码的那一种类型。

**预期结果**：你应得出——「**谁解码，谁打补丁**」。子进程要解码请求 → 子进程补请求类型；客户端要解码输出 → 客户端补输出类型。两处都必须在各自 `super().__init__()` 创建解码器/IO 线程之前完成。

> 本实践为源码阅读型；若本地装好 vLLM-Omni 并能起一个单 stage diffusion 服务，可在 4.5 综合实践里结合日志观察。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `StageEngineCoreClient` 要用多重继承（`StageEngineCoreClientBase` + `AsyncMPClient`），而不是把 ZMQ 代码复制一份？

> **参考答案**：复用 vLLM 的传输基建（ZMQ socket、outputs_queue、后台 output task、健康监测等）是本架构「与 vLLM 核心兼容」的核心承诺。多重继承让 stage 感知逻辑（基类）与传输实现（`AsyncMPClient`）正交解耦：omni 只维护前者，后者随上游 vLLM 升级而自动获得改进。

**练习 2**：`check_health()` 是如何与子进程的死亡联动的？

> **参考答案**：子进程在 `run_stage_core` 的异常路径调用 `engine_core._send_engine_dead()`（4.1 第 193 行），这会把客户端共享的 `resources.engine_dead` 置位；客户端侧 `check_health()` 检测到该标志后抛 `EngineDeadError`，从而让上层（`OmniBase.check_health` → `/health` 端点）感知到 stage 已死。

## 5. 综合实践

**任务**：梳理「stage 进程从启动到收到第一条请求」的完整初始化链路，并标注每一步发生在哪个类、哪个文件。

这是本讲规格指定的核心实践任务，它把 4.1、4.2、4.3 三个模块串起来。

**实践目标**：产出一张「初始化时序表」，覆盖 `init_device → load_model → busy_loop → execute` 四个里程碑。

**操作步骤**：

1. 阅读以下文件，按时间顺序补全下表的「发生类」与「文件」两列：
   - `vllm_omni/engine/stage_runtime.py`（`initialize` / `_initialize_local_llm_replica`）
   - `vllm_omni/engine/stage_engine_core_proc_manager.py`（`__init__` / `proc.start()`）
   - `vllm_omni/engine/stage_engine_core_proc.py`（`run_stage_core`）
   - `vllm_omni/engine/stage_engine_core_client.py`（`make_async_mp_client` / `add_request_async`）

2. 参考时序表（请自行补全带 ▢ 的两列）：

   | 顺序 | 动作 | 发生类 | 文件 |
   | --- | --- | --- | --- |
   | 1 | 读配置、算副本布局、准备环境 | `StageRuntime._prepare_stage_plans` | ▢ |
   | 2 | 设备锁、spawn 子进程 | ▢（提示：`launch_stage_replica` 内部用 `StageEngineCoreProcManager`） | `stage_runtime.py` / `stage_engine_core_proc_manager.py` |
   | 3 | 子进程入口：设信号、设标题、补请求类型 | `StageEngineCoreProc.run_stage_core` | ▢ |
   | 4 | 构造 EngineCore（握手、起 IO 线程、建 Scheduler/Executor/Worker） | `StageEngineCoreProc.__init__`（继承自上游 vLLM `EngineCoreProc`） | 上游 vLLM（待确认行号） |
   | 5 | Worker 设备初始化、加载模型权重 | `init_device` / `load_model`（上游 vLLM Executor/Worker） | 上游 vLLM（待确认行号） |
   | 6 | 挂协调器心跳、装信号处理 | ▢ | `stage_engine_core_proc.py` |
   | 7 | 进入忙循环 | `engine_core.run_busy_loop()`（继承自上游 vLLM） | 上游 vLLM（待确认行号） |
   | 8 | 主进程用握手地址创建客户端 | `StageEngineCoreClientBase.make_async_mp_client` | ▢ |
   | 9 | 客户端补输出类型、连 ZMQ | `StageEngineCoreClientBase.__init__` → `AsyncMPClient.__init__` | `stage_engine_core_client.py` |
   | 10 | 客户端把所有副本装进 StagePool | `StageRuntime._assemble_stage_pools` | ▢ |
   | 11 | 第一条请求：`add_request_async` 发出 | ▢ | `stage_engine_core_client.py` |
   | 12 | 子进程 busy loop 拉取 → `preprocess_add_request` → schedule → execute_model | `StageEngineCoreProc.preprocess_add_request` + 上游 Scheduler/Executor | `stage_engine_core_proc.py` + 上游 vLLM |

   参考答案（带 ▢ 的列）：1→`stage_runtime.py`；2→`StageEngineCoreProcManager`；3→`stage_engine_core_proc.py`；6→`StageEngineCoreProc.run_stage_core`；8→`stage_engine_core_client.py`；10→`stage_runtime.py`；11→`StageEngineCoreClient.add_request_async`（继承自 `AsyncMPClient`）。

3. **进阶**：在上表里圈出所有「**上游 vLLM**」负责的步骤（4、5、7、12 的后半段），体会 vLLM-Omni「**不重写核心、只在外层包壳**」的扩展哲学。真正由 omni 写的，只有 1、2、3、6、8、10、11 这几层「壳」。

**需要观察的现象**：你会发现整条链路里，omni 自有代码集中在「**两端**」——子进程的入口准备（`run_stage_core`）和主进程的客户端/运行时装配（`StageRuntime` / `StageEngineCoreClientBase`），中间真正干推理的 `init_device`/`load_model`/`busy_loop`/`execute_model` 全部复用上游 vLLM。

**预期结果**：得到一张清晰的「谁在哪一步做了什么」表。这张表是后续阅读 u3-l4（OmniConnector 跨阶段传输）的基础——因为只有先确认「每个 stage 是一个可寻址、可通信的进程实体」，跨阶段的 KV / payload 传输才有意义。

> 若本地具备 GPU 且已按 [u1-l2](u1-l2-installation.md) 安装好 vLLM-Omni，可启动一个单 stage 的 diffusion 服务（如 Z-Image-Turbo），在另一终端 `ps aux | grep StageEngineCoreProc` 观察 `set_process_title` 设置的进程名，验证步骤 3；并在日志里搜索 `EngineCore running` / `stage initialized` 验证步骤 7、8。否则标注「待本地验证」即可。

## 6. 本讲小结

- **stage = 独立 EngineCore 子进程**：vLLM-Omni 继承 vLLM 的 `EngineCoreProc`，但用自己的 `run_stage_core` 作入口，以便在最早时机注入 stage_id / replica_id / omni 请求类型。
- **两处「先补丁后构造」铁律**：子进程入口替换 `EngineCoreRequest → OmniEngineCoreRequest`，主进程客户端替换 `EngineCoreOutputs → OmniEngineCoreOutputs`；都必须在各自的 `super().__init__()` 创建解码器之前完成。口诀是「**谁解码，谁打补丁**」。
- **忙循环继承不自造**：`run_busy_loop()`、`init_device`、`load_model`、`execute_model` 全部来自上游 vLLM，omni 只在外层包壳。
- **`StageRuntime` 是主进程的编排者**：单机用 `StageRuntime`、分布式用 `DistStageRuntime`，由 `create_stage_runtime` 工厂分流；初始化按「同 GPU 串行、跨 GPU 并行」调度副本。
- **两个数据类定位不同**：`StageRuntimeInfo` 是轻量的「输出角色摘要」，`StageRemoteFactoryContext` 是重量的「远程客户端建客户端材料包」，后者支撑分布式动态注册。
- **客户端走多重继承**：`StageEngineCoreClient(StageEngineCoreClientBase, AsyncMPClient)` 把 stage 感知与 vLLM 传输正交拼合，`add_request_async` / `get_output_async` 直接来自 `AsyncMPClient`，无需重写。

## 7. 下一步学习建议

- **承接本讲的纵向视角**，下一步读 [u3-l4：全解耦通信 OmniConnector 体系](u3-l4-omni-connectors.md)。本讲建立了「每个 stage 是可寻址进程」，u3-l4 讲「stage 之间如何用 OmniConnector 解耦传输 KV / payload」。
- 若你对**分布式副本与负载均衡**更感兴趣，可直接跳到 [u3-l5：OmniCoordinator](u3-l5-omni-coordinator.md)，那里讲本讲提到的协调器心跳、`LEAST_QUEUE_LENGTH` 等策略。
- 若你想看 **diffusion stage 的子进程内部**（本讲的 `_initialize_local_diffusion_replica` 分支对应的运行时），转入 [U5：Diffusion 模块](u5-l1-diffusion-engine.md)，那里讲 `DiffusionEngine` / `DiffusionWorker` 的进程模型。
- 想验证本讲链路，可在 `examples/offline_inference/qwen3_omni/end2end.py` 跑一次三阶段推理，对照 4.5 的时序表观察日志。
