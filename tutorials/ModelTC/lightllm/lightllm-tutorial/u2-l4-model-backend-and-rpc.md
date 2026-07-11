# Model Backend 推理后端与 RPC

## 1. 本讲目标

本讲聚焦 LightLLM 多进程架构中的「模型推理后端」这一层——也就是每个 GPU 上跑的那个独立进程。学完本讲，你应当能够：

- 说清 **`ModelRpcServer` / `ModelRpcClient`** 的 rpyc 服务模型：为什么每个 GPU 要单独起一个进程，它对外暴露哪些方法，客户端又是如何把远程调用「异步化」包装的。
- 掌握 **`ModeBackend`** 这个推理后端基类：它的 `init_model` 里按什么顺序组装了模型、KV 内存管理器、RadixCache，以及它为什么要在末尾启动两个 `infer_loop` 线程。
- 理解 **批推理接口**：`infer_loop` 主循环做了哪些事，`prefill` / `decode` 在「四阶段 overlap」里如何组织一次前向计算。
- 认清 **router 与 model backend 之间的协作方式**：rpyc 负责「一次性控制」，共享内存 `ShmObjsIOBuffer` 负责「每一步的数据下发」，二者分工互补。

> 名词提示：本讲的标题里写作 `init_model/prefill_batch/decode_batch`，这是早期版本的接口名。当前代码里，`ModeBackend` 的对外接口是 `init_model`，而 `prefill_batch/decode_batch` 已经演化为由 `infer_loop` 自驱轮询共享内存、再调用 `prefill` / `decode` 的结构（见 4.3）。本讲以当前真实源码为准。

## 2. 前置知识

阅读本讲前，建议你已经建立以下认知（对应 u2-l1、u2-l3）：

- **多进程架构**：LightLLM 把一次推理拆成多个进程——HttpServer 收请求并分词、Router 调度、ModelBackend（每 GPU 一个）做 GPU 推理、Detokenization 把 token 解码回文本。
- **三类 IPC**：zmq 传轻量通知，rpyc 传需要应答的远程调用，**共享内存**承载 `Req` 等大块状态、做到零拷贝。
- **「对象放共享内存、线上只传索引」**：跨进程传递的往往只是 `index_in_shm_mem` 这种小整数，真正的 `Req` 与 prompt 数组常驻共享内存原地读写。
- **`ShmObjsIOBuffer`**（u2-l3）：一个单生产者多消费者的命令管道，靠 `set_ready` / `sub_state` / `is_empty` 协议让节点内多个 rank 各读一次。本讲你会看到它如何被 router 写、被 backend 读。

如果上面这几条还陌生，先回到 u2-l1 和 u2-l3 补课。本讲不再重复这些基础概念。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [lightllm/server/router/model_infer/model_rpc.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/model_rpc.py) | 定义 rpyc 服务端 `ModelRpcServer`、客户端 `ModelRpcClient`，以及拉起每个 GPU 进程的 `start_model_process`。 |
| [lightllm/server/router/model_infer/mode_backend/base_backend.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/base_backend.py) | 推理后端基类 `ModeBackend`：`init_model` 组装全部组件，并声明 `infer_loop/prefill/decode` 抽象接口；还包含大量可复用的请求分类、前后处理函数。 |
| [lightllm/server/router/model_infer/mode_backend/chunked_prefill/impl.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/chunked_prefill/impl.py) | `ModeBackend` 的主力子类 `ChunkedPrefillBackend`：给出 `infer_loop` 与 `prefill_normal`/`decode_normal` 的真实实现。 |
| [lightllm/server/router/model_infer/infer_batch.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/infer_batch.py) | 定义推理侧的全局上下文 `InferenceContext`（`g_infer_context`）、GPU 上的请求对象 `InferReq`，以及用于 overlap 的 `InferReqUpdatePack`。 |
| [lightllm/server/router/manager.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/manager.py) | `RouterManager`：用它来说明 router 一侧如何「用 rpyc 做控制、用共享内存下数据」。 |

阅读建议：先看 4.1 建立进程边界，再看 4.2 理解后端初始化，最后看 4.3 把「主循环」串起来。

## 4. 核心概念与源码讲解

### 4.1 RPC 服务：每 GPU 一个 ModelRpcServer 进程

#### 4.1.1 概念说明

LightLLM 用张量并行（TP）时，每个 GPU（每个 rank）都是一个**独立的操作系统进程**，各自持有自己那份模型权重和 KV Cache。Router 进程需要能「指挥」这些 GPU 进程，但又不能让 GPU 推理阻塞 Router 的调度循环。

这里的解法是 **rpyc + Unix socket**：

- 每个 GPU 进程里跑一个 `ModelRpcServer`（一个 rpyc `ThreadedServer`），监听一个临时的 Unix domain socket。
- Router 一侧持有一组 `ModelRpcClient`，通过这个 socket 远程调用 GPU 进程的方法。

但要注意一个关键点：**rpyc 在 LightLLM 里几乎只承担「一次性控制」**——比如初始化模型、查询最大 token 容量。真正每一步要推理哪些请求，**不走 rpyc**，而是走共享内存（见 4.1.4 和 4.3）。这样设计是因为每步推理都是高频且数据量大的操作，rpyc 的序列化开销会成为瓶颈，而共享内存零拷贝且能让 backend 自驱轮询。

#### 4.1.2 核心流程

一次「拉起 GPU 进程并建立 RPC 通道」的流程：

1. Router 调用 `start_model_process(...)`，为一个 rank `mp.Process` 起一个子进程，目标函数是 `_init_env`。
2. 子进程里创建 `ModelRpcServer`，用 `ThreadedServer` 绑定一个随机 Unix socket 路径，`success_event.set()` 表示「我准备好了」，然后 `t.start()` 阻塞提供服务。
3. 父进程（Router）等待 `success_event`（带 40 秒超时），确认子进程活着后，用 `unix_connect` 连上这个 socket，得到一个 `ModelRpcClient`。
4. Router 拿到所有 rank 的 `ModelRpcClient` 后，用 rpyc 调用 `init_model` 把初始化参数下发，再调用 `get_max_total_token_num` 查询容量。

#### 4.1.3 源码精读

**服务端：`ModelRpcServer` 是一个 rpyc Service。**

[model_rpc.py:40-50](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/model_rpc.py#L40-L50) 定义了服务端类，它在 `__init__` 里只是记录 rank 信息，真正的活都在 `exposed_*` 方法里（rpyc 约定：只有 `exposed_` 前缀的方法才能被远程调用）。

[model_rpc.py:52-109](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/model_rpc.py#L52-L109) 是 `exposed_init_model`，它做两件事：**选 backend 类型** 和 **初始化模型**。选型逻辑是一棵优先级树：

```python
if is_prefill_node:        # PD 分离的 prefill 节点
    self.backend = PDDPChunkedForPrefillNode(...) or PDChunkedPrefillForPrefillNode(...)
elif is_decode_node:       # PD 分离的 decode 节点
    self.backend = PDDPForDecodeNode(...) or PDDecodeNode(...)
elif self.args.dp > 1:     # 数据并行
    self.backend = DPChunkedPrefillBackend()
elif use_reward_model:     # 奖励模型
    ...
else:                      # 默认的 chunked prefill（本讲主角）
    self.backend = ChunkedPrefillBackend()
```

选完之后一句 `self.backend.init_model(kvargs)`（[model_rpc.py:101](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/model_rpc.py#L101)）把后续全部交给 backend。

[model_rpc.py:111-112](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/model_rpc.py#L111-L112) 是另一个对外方法 `exposed_get_max_total_token_num`，直接委托给 `self.backend.get_max_total_token_num()`——这就是 Router 用来询问「这块 GPU 一共能装多少 token」的入口。

**进程拉起：`start_model_process`。**

[model_rpc.py:174-210](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/model_rpc.py#L174-L210) 是 Router 异步调用的入口。关键几步：

- [model_rpc.py:183-185](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/model_rpc.py#L183-L185)：生成一个随机 Unix socket 路径，形如 `/tmp/lightllm_model_infer_<8位hex>.sock`（见 [model_rpc.py:213-216](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/model_rpc.py#L213-L216)）。
- [model_rpc.py:187-200](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/model_rpc.py#L187-L200)：用 `mp.Process(target=_init_env, ...)` 起子进程，并把 `success_event` 传进去做握手。
- [model_rpc.py:203](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/model_rpc.py#L203)：`await asyncio.to_thread(success_event.wait, timeout=40)`——把阻塞的 `wait` 扔到线程池，让 Router 的异步循环不被卡住。
- [model_rpc.py:208](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/model_rpc.py#L208)：用带重试的 `unix_connect` 连上 socket，返回 `ModelRpcClient`。

子进程一侧的 `_init_env`（[model_rpc.py:149-171](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/model_rpc.py#L149-L171)）做了三件事：注册优雅退出、设置进程名（`lightllm::<server>::model_infer:RANK<n>`）、起 `ThreadedServer`。其中 [model_rpc.py:167](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/model_rpc.py#L167) 创建服务时带上了 `protocol_config={"allow_pickle": True}`——因为 `kvargs` 里含有需要 pickle 的对象（如 StartArgs）。

#### 4.1.4 客户端：`ModelRpcClient` 如何把调用异步化

Router 的调度循环是 `asyncio` 写的，但 rpyc 的远程调用本质上是同步阻塞的。`ModelRpcClient` 用一个精巧的 `async_wrap` 把两者桥接起来。

[model_rpc.py:115-146](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/model_rpc.py#L115-L146) 是核心。`async_wrap` 的内部逻辑：

```python
def async_wrap(f):
    f = rpyc.async_(f)          # 把 rpyc 方法变成「立即返回 NetREF+wait」的形式

    async def _func(*args, **kwargs):
        try:
            ans = f(*args, **kwargs)     # 发起调用，立刻返回一个 async 结果对象
        except BaseException as e:
            logger.exception(str(e)); os._exit(-1)   # 远端初始化失败→直接退出整个进程

        await asyncio.to_thread(ans.wait)  # 把阻塞的 wait 扔到线程池
        return ans.value                   # 取出真实返回值（出错会在此抛出）

    return _func
```

要点拆解：

- `rpyc.async_(f)` 让调用「发出去就返回」，不等结果，返回一个带 `.wait()` / `.value` 的对象。
- `await asyncio.to_thread(ans.wait)` 是关键：把「等远端算完」这个阻塞动作丢到独立线程，**当前 async 任务让出执行权**，Router 的 event loop 可以继续跑别的 rank 的初始化，从而实现多个 GPU 进程并行初始化。
- 出错处理很激进：[model_rpc.py:125-127](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/model_rpc.py#L125-L127) 里一旦远端抛异常，直接 `os._exit(-1)`——因为模型初始化失败意味着这个 rank 无法服务，留着只会让其它 rank 干等，不如整体退出。

> 注意：`ModelRpcClient` 只包装了 `init_model` 和 `get_max_total_token_num` 两个方法（[model_rpc.py:135-137](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/model_rpc.py#L135-L137)）。这正印证了「rpyc 只做控制」——一旦 `init_model` 完成，backend 就靠自己的 `infer_loop` 轮询共享内存自驱运行，Router 不再通过 rpyc 触发每一步推理。

#### 4.1.5 代码实践

**实践目标**：亲手追踪 `ModelRpcClient` 的异步包装链路，并对比「同步 rpyc」与「async_wrap」的区别。

**操作步骤**：

1. 打开 [model_rpc.py:119-146](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/model_rpc.py#L119-L146)，在 `async_wrap` 内的 `ans = f(*args, **kwargs)` 之后加一行（**示例代码**，仅供阅读理解，不真正修改源码）：
   ```python
   print(f"[debug] rpyc call dispatched, now awaiting in thread pool")
   ```
2. 追问自己三个问题：
   - 如果把 `await asyncio.to_thread(ans.wait)` 改成直接 `ans.wait()`，会发生什么？（提示：阻塞整个 event loop，多 rank 无法并行初始化。）
   - 为什么出错要 `os._exit(-1)` 而不是 `raise`？（提示：避免其它 rank 在 `asyncio.gather` 里空等。）
   - `get_max_total_token_num` 里为什么对返回值还要套一层 `obtain`？（见 [model_rpc.py:144-146](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/model_rpc.py#L144-L146)；`obtain` 会把 NetREF 解引用成本地真实值。）

**需要观察的现象 / 预期结果**：你能口头复述「Router 发起 `init_model` → rpyc 立即返回 → 线程池里 `wait` → 取 `.value`」这条链路，并解释为什么多 GPU 初始化是并行的。

> 待本地验证：实际并行初始化的耗时对比，需要在多卡环境启动服务并观察日志时间戳才能确认。

#### 4.1.6 小练习与答案

**练习 1**：`ModelRpcServer` 上为什么只暴露 `init_model` 和 `get_max_total_token_num`，而不暴露 `prefill` / `decode`？

**答案**：因为每一步推理是高频且数据量大的操作，走 rpyc 会带来序列化与跨进程往返开销；改用共享内存 `ShmObjsIOBuffer` 下发请求索引、由 backend 的 `infer_loop` 自驱轮询，既零拷贝又解耦了 Router 与 GPU 的节奏。

**练习 2**：`async_wrap` 里 `await asyncio.to_thread(ans.wait)` 如果换成 `ans.wait()`，多卡并行初始化会怎样？

**答案**：`ans.wait()` 是阻塞调用，会卡住 Router 的 event loop，导致 rank 之间变成串行初始化（一个卡完才轮到下一个），失去并行加速。

---

### 4.2 推理后端：ModeBackend 的初始化与组件组装

#### 4.2.1 概念说明

`exposed_init_model` 选出来的那个 `self.backend`（默认是 `ChunkedPrefillBackend`）才是 GPU 进程里真正干活的对象。它继承自基类 `ModeBackend`，职责可以概括为：

- **组装推理所需的全部组件**：分布式环境、模型本体（`TpPartBaseModel`）、KV 内存管理器、RadixCache、共享内存请求管理器、采样参数管理器等。
- **维护推理侧的全局上下文** `g_infer_context`（一个单例），让请求对象、KV 索引、radix cache 都能被各处访问。
- **提供「主循环 + 抽象钩子」的骨架**：基类把 `infer_loop` / `prefill` / `decode` 声明为抽象方法（抛 `NotImplementedError`），具体行为交给子类实现；同时把请求分类、前后处理等可复用逻辑沉到基类。

`ModeBackend` 是一个典型的**模板方法模式**：基类定流程骨架与公共工具，子类填具体算法。所有「模式」（普通、PD 分离、DP、约束解码、MTP……）都通过继承它来定制。

#### 4.2.2 核心流程

`init_model` 是一个长长的初始化流水线，按顺序：

1. 解析基本参数（`run_mode`、`world_size`、`dp_size`、`weight_dir` 等）。
2. `init_distributed_env` + `init_rank_infos`：建立 NCCL 通信域，确定自己在哪个 rank、哪个节点、是不是 master。
3. `get_model(...)`：根据模型 config 拿到具体的 `TpPartBaseModel` 实例（模型本体，含权重、KV 内存管理器、req_manager）。
4. 建立 `RadixCache`（若启用动态 prompt cache）。
5. `g_infer_context.register(...)`：把 backend、req_manager、radix_cache、shm_req_manager 都注册进全局上下文。
6. 建立 DP / 多节点 TP 协同用的通信 tensor 与通信组。
7. **启动两个 `infer_loop` 线程**（这是性能关键，见下文）。

#### 4.2.3 源码精读

**基类骨架**：[base_backend.py:55-84](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/base_backend.py#L55-L84) 是 `ModeBackend.__init__`，预先准备好共享内存管理器、overlap 事件管理器，以及几个「钩子函数」字段（`prefill_mask_func` / `decode_mask_func` / `extra_post_req_handle_func`）——这些钩子是约束解码、状态机等子类定制的扩展点。

**初始化主流程**：[base_backend.py:86-254](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/base_backend.py#L86-L254)。几处关键点：

- [base_backend.py:118-123](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/base_backend.py#L118-L123)：初始化分布式环境并创建默认通信组。注意 `group_size` 在开启 microbatch overlap 时为 2（用于双流 all-reduce 分组），否则为 1。
- [base_backend.py:152](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/base_backend.py#L152)：`get_model(model_cfg, model_kvargs)` 拿到模型本体，`model_kvargs` 里打包了 `weight_dir`、`max_total_token_num`、`mem_fraction`、`data_type`、`disable_cudagraph` 等几乎所有推理配置（[base_backend.py:132-151](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/base_backend.py#L132-L151)）。模型注册与匹配的细节在 u5-l1 讲。
- [base_backend.py:165-184](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/base_backend.py#L165-L184)：按是否启用动态 prompt cache、是否线性注意力混合模型，选择 `RadixCache` 或 `LinearAttPagedRadixCache`。RadixCache 是 lightllm 的招牌设计，u4-l2 专题讲解。
- [base_backend.py:192-198](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/base_backend.py#L192-L198)：`g_infer_context.register(...)` 把所有组件挂到全局上下文，自此 backend 各处都能通过 `g_infer_context` 拿到 req_manager / radix_cache / shm_req_manager。
- [base_backend.py:233-235](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/base_backend.py#L233-L235)：建立两个 `ShmObjsIOBuffer`——`shm_reqs_io_buffer` 收 router 下发的请求命令，`shm_pd_trans_io_buffer` 收 PD 分离的分块传输进度。

**最关键的两行——启动两个推理线程**：

```python
# base_backend.py:250-253
self.infer_loop_thread  = threading.Thread(target=self.infer_loop, daemon=True)
self.infer_loop_thread.start()
self.infer_loop_thread1 = threading.Thread(target=self.infer_loop, daemon=True)
self.infer_loop_thread1.start()
```

为什么要起**两个** `infer_loop` 线程？注释（[base_backend.py:248-249](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/base_backend.py#L248-L249)）写得很清楚：「启动两个线程进行推理，对于具备双 batch 推理折叠的场景，可以降低 cpu overhead，大幅提升 gpu 的使用率」。也就是说，两个线程交替跑两个 batch 的前后处理，让 GPU 在一个 batch 的前向计算时，CPU 同时做另一个 batch 的采样/后处理——这就是 overlap。两个线程通过 `OverlapEventPack` 互相同步（见 4.3.3）。

**抽象接口**：[base_backend.py:283-290](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/base_backend.py#L283-L290) 把 `infer_loop` / `prefill` / `decode` 都声明为 `raise NotImplementedError()`，强制子类实现。

**容量查询**：[base_backend.py:280-281](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/base_backend.py#L280-L281) 的 `get_max_total_token_num` 直接返回 `self.model.mem_manager.size`，即 KV 内存管理器能容纳的 token 总数——这就是 `exposed_get_max_total_token_num` 最终返回的值。

#### 4.2.4 代码实践

**实践目标**：理清 `init_model` 的初始化顺序，搞懂每一步组装了什么。

**操作步骤**：

1. 打开 [base_backend.py:86-254](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/base_backend.py#L86-L254)。
2. 列一张表，把 `init_model` 分成 7 段，每段写「调用了什么 / 产出什么对象」：
   - 参数解析（86-116）
   - 分布式环境（118-123）
   - 模型本体（130-154）
   - RadixCache（165-188）
   - 全局上下文注册（192-198）
   - 通信 tensor / 通信组（200-231）
   - 启动 infer_loop 线程（250-253）
3. 回答：如果 `use_dynamic_prompt_cache` 为 `False`（即命令行加了 `--disable_dynamic_prompt_cache`），`self.radix_cache` 会是什么？（看 [base_backend.py:165-166](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/base_backend.py#L165-L166)）

**预期结果**：你能画出 `ModeBackend` 持有的关键对象关系图：`model`(含 `mem_manager`、`req_manager`)、`radix_cache`、`shm_req_manager`、`shm_reqs_io_buffer`、`overlap_event_manager`，并说清 `g_infer_context` 是把它们串起来的「总线」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `init_model` 末尾要起两个 `infer_loop` 线程而不是一个？

**答案**：为了双 batch overlap——一个线程在等 GPU 前向算完时，另一个线程可以同时在 CPU 上做另一个 batch 的采样与后处理，从而压低 CPU overhead、提高 GPU 利用率。两个线程通过 `OverlapEventPack` 协调，避免对同一批共享状态的竞争。

**练习 2**：`ModeBackend` 把 `prefill` / `decode` 写成 `raise NotImplementedError()`，这是哪种设计模式？好处是什么？

**答案**：模板方法模式。基类锁定主循环骨架（`infer_loop`）与公共工具（分类、前后处理），子类只需覆写 `prefill` / `decode` 的具体算法，就能支持普通、PD 分离、约束解码、MTP 等多种模式，复用大量公共逻辑。

---

### 4.3 批推理接口：infer_loop 主循环与 prefill/decode

#### 4.3.1 概念说明

backend 不是一个「被 Router 远程调用 `prefill()` / `decode()`」的被动对象——恰恰相反，它在 `init_model` 末尾就启动了自驱的 `infer_loop` 线程，**主动**轮询共享内存，自己决定这一步该 prefill 还是 decode。这是 LightLLM 区别于「Router 同步驱动 GPU」架构的关键。

`ChunkedPrefillBackend` 是默认实现，它的三件套是：

- **`infer_loop`**：主循环。每一轮：读新请求 → 给请求分类 → 由 `control_state_machine` 决定走 prefill / decode / pass → 调对应方法。
- **`prefill_normal` / `decode_normal`**：单步前向的「四阶段 overlap」实现。
- **`g_infer_context` + `InferReq`**：推理侧的状态承载——`InferReq` 是 GPU 上一个请求的运行时对象（`cur_kv_len`、`cur_output_len`、`finish_status`…）。

#### 4.3.2 核心流程

`infer_loop` 单轮逻辑（伪代码）：

```
while True:
    event_pack = overlap_event_manager.get_overlap_event_pack()  # 取一个同步包(协调两个线程)
    event_pack.wait_to_forward()                                 # 等到轮到自己推进
    _try_read_new_reqs()                                         # 从共享内存读 router 下发的新请求/命令
    prefill_reqs, decode_reqs = _get_classed_reqs(...)           # 把请求分成 prefill/decode/暂停/完成
    run_way = control_state_machine.select_run_way(...)          # 决定这一步干什么
    if run_way.is_prefill():  prefill(...);  continue
    elif run_way.is_decode(): decode(...);   continue
    elif run_way.is_pass():   # 没活干，做一次空转的 overlap 同步，sleep 20ms
        ...
```

`prefill_normal` 的「四阶段」（decode 同理）：

1. **阶段一（GPU 流）**：`model.forward` + 采样 + scatter token，在 overlap stream 上执行，最后 `record` 一个 event。
2. **阶段二（CPU）**：通知对方线程、做 `_pre_post_handle`（更新 `cur_kv_len` / `cur_output_len`，生成 `InferReqUpdatePack`）。
3. **阶段三**：等阶段一的 event 同步完成（GPU 算完了），再做 `_post_handle`（把 token 写回 `shm_req`，更新 finish 状态）。
4. **阶段四**：通知对方线程可以推进。

这四个阶段配合 `event_pack` 的 `notify_*` / `wait_*`，让两个线程交错使用 CPU 与 GPU。

#### 4.3.3 源码精读

**子类绑定方法**：[chunked_prefill/impl.py:32-51](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/chunked_prefill/impl.py#L32-L51)。`ChunkedPrefillBackend.__init__` 里做了一个动态绑定：如果开了 `mtp_mode`，就把 `self.prefill` / `self.decode` 指向 `prefill_mtp` / `decode_mtp`（推测解码，u7-l5 讲），否则指向 `prefill_normal` / `decode_normal`。所以 `infer_loop` 里调用的 `self.prefill(...)` 实际指向哪个实现，取决于启动参数。

**主循环**：[chunked_prefill/impl.py:53-101](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/chunked_prefill/impl.py#L53-L101)。逐段看：

- [impl.py:57-60](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/chunked_prefill/impl.py#L57-L60)：取 `event_pack`，若不支持 overlap（如 xgrammar/outlines 模式）就关闭它。
- [impl.py:62-64](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/chunked_prefill/impl.py#L62-L64)：`wait_to_forward()` + `_try_read_new_reqs()`——这一步是从共享内存把 Router 写进来的请求命令取出来。
- [impl.py:66-70](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/chunked_prefill/impl.py#L66-L70)：`_get_classed_reqs(...)` 给请求分类，返回 `(prefill_reqs, decode_reqs)`。
- [impl.py:72-97](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/chunked_prefill/impl.py#L72-L97)：`select_run_way` 决定走向——prefill / decode / pass。注意 prefill 和 decode 分支前都有一句 `g_infer_context.get_overlap_stream().wait_stream(torch.cuda.current_stream())`，注释解释是为了保证 `_try_read_new_reqs` 里的一些算子操作（如往显存写 KV 索引）已完成，避免读到脏数据。`pass` 分支代表「没活干」，做一次空同步后 `sleep(0.02)`。

**`_try_read_new_reqs`：backend 怎么读 Router 的下发**：[base_backend.py:375-384](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/base_backend.py#L375-L384) 是入口，按是否多节点 TP 分流。普通模式 [base_backend.py:386-412](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/base_backend.py#L386-L412) 的核心是：

- 由节点内 master rank 检查 `shm_reqs_io_buffer.is_ready()`，把结果（0/1）写进一个 GPU tensor。
- 用 NCCL `broadcast` 把这个 0/1 同步给节点内所有 rank——保证「要么大家都读，要么大家都不读」，避免不同 rank 步调不一致。
- 若 ready，调 `_read_reqs_buffer_and_init_reqs()`（[base_backend.py:437-456](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/base_backend.py#L437-L456）真正取出命令并初始化请求。命令分三类：新建请求的 tuple、`AbortedReqCmd`/`StopStrMatchedReqCmd`（中止/命中停止串）、`ProfilerCmd`（性能采样命令）。

**Router 一侧怎么写**：在 [manager.py:301-309](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/manager.py#L301-L309) 的 `_add_batch` 里，Router 把新 batch 的请求转成 rpc 对象，`write_obj` 进 `shm_reqs_io_buffer`，再 `set_ready()`——这正是上面 `_try_read_new_reqs` 读的对端。中止（[manager.py:322-328](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/manager.py#L322-L328)）和命中停止串（[manager.py:330-336](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/base_backend.py#L330-L336)）走的也是同一个 buffer。这就把「rpyc 做控制、共享内存做数据」的分工看得很清楚了。

> 串起来：Router 的 `_step`（[manager.py:280-299](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/manager.py#L280-L299)）每轮决定要不要加新 batch，要加就 `_add_batch` 写共享内存；backend 的 `infer_loop` 每 20ms 量级轮询一次，读到就走 prefill/decode。二者完全解耦，靠 `ShmObjsIOBuffer` 的 ready/empty 协议握手。

**单步前向 `prefill_normal`**：[impl.py:103-145](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/chunked_prefill/impl.py#L103-L145) 对应上面说的四阶段。注意几个细节：

- `prepare_prefill_inputs(prefill_reqs, is_chuncked_mode=...)`（[impl.py:109](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/chunked_prefill/impl.py#L109)）把 `InferReq` 列表打包成 `ModelInput`（批的张量布局），这是模型前向的输入。
- 整个 GPU 计算包在 `with torch.cuda.stream(g_infer_context.get_overlap_stream()):` 里，跑在专门的 overlap 流上，最后 `sync_event.record()`（[impl.py:125-126](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/chunked_prefill/impl.py#L125-L126)）。
- [impl.py:134](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/chunked_prefill/impl.py#L134) 的 `sync_event.synchronize()` 才真正阻塞等 GPU 算完，然后 `_post_handle` 把结果写回 `shm_req`。这种「先 record 后 synchronize」正是 overlap 的实现手段——CPU 在两个时间点之间可以做别的活。
- `decode_normal`（[impl.py:147-183](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/chunked_prefill/impl.py#L147-L183)）结构几乎一样，区别在于输入用 `prepare_decode_inputs`，且 `is_chuncked_mode=False`。

**推理侧状态 `InferReq`**：[infer_batch.py:491-582](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/infer_batch.py#L491-L582) 是 GPU 上一个请求的运行时对象。它和 u2-l3 讲的共享内存里的 `Req` 是一对：`Req` 是跨进程的 ctypes 结构，`InferReq` 是 GPU 进程内部的 Python 对象，通过 `self.shm_req`（[infer_batch.py:585](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/infer_batch.py#L585)）指向共享内存里那个 `Req`。最关键的几个字段（[infer_batch.py:594-595](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/infer_batch.py#L594-L595)）：`cur_kv_len`（已计算 KV 的长度）、`cur_output_len`（已输出 token 数），调度和分类逻辑全靠它们。

**`InferReqUpdatePack`：为 overlap 解耦的延迟更新**：[infer_batch.py:888-949](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/infer_batch.py#L888-L949)。它的注释（[infer_batch.py:889-893](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/infer_batch.py#L889-L893)）说明：把 `_post_handle` 里「需要等输出确认」和「不需要确认」的两部分解耦，绑定参数后延迟处理，方便 overlap。`handle()` 里（[infer_batch.py:938-948](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/infer_batch.py#L938-L948)）只有 `is_master_in_dp` 的请求才会把 `shm_cur_output_len` / `finish_token_index` / `finish_status` 写回共享内存——这些正是 Router 和 Detokenization 进程要读的字段。

#### 4.3.4 代码实践

**实践目标**：完整复述 `infer_loop` 一轮做了什么，并理解 backend 与 Router 通过共享内存的握手。

**操作步骤**：

1. 打开 [chunked_prefill/impl.py:53-101](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/chunked_prefill/impl.py#L53-L101)，在每一行旁标注它属于「取同步包 / 读新请求 / 分类 / 选走向 / 执行」中的哪一类。
2. 打开 [base_backend.py:386-412](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/base_backend.py#L386-L412)，找到那条 NCCL `broadcast`，解释：为什么读共享内存这件「纯 CPU」的事，还要做一次 GPU 上的集合通信？（提示：为了让节点内所有 rank 对「这一轮有没有新请求」达成一致，保证 TP 下各 rank 处理的请求集合完全相同。）
3. 对照 [manager.py:301-309](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/manager.py#L301-L309) 的 `_add_batch` 和 [base_backend.py:437-456](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/base_backend.py#L437-L456) 的 `_read_reqs_buffer_and_init_reqs`，画一张「Router 写 → buffer → backend 读」的时序图。

**需要观察的现象 / 预期结果**：你能解释清楚——Router 完全不知道 backend 什么时候真正推理某条请求，它只负责把请求写进 buffer；backend 的两个 `infer_loop` 线程自驱轮询，谁先轮到谁就推进。这种解耦让 Router 的调度循环（30ms 一次）和 GPU 推理各自跑在最优节奏上。

> 待本地验证：两个线程实际交错执行的时序，需要开启性能日志或用 nsight 观察流的时间线才能看到。

#### 4.3.5 小练习与答案

**练习 1**：`infer_loop` 里 `run_way.is_pass()` 分支为什么会 `time.sleep(0.02)`？

**答案**：`pass` 表示当前既没有 prefill 也没有 decode 可做（比如刚启动、或所有请求都在等 KV），此时没必要空转占满 CPU，于是做一次空的 overlap 同步后睡 20ms，降低无效轮询的 CPU 消耗。

**练习 2**：为什么 `_post_handle` 写回共享内存前要先 `sync_event.synchronize()`？

**答案**：因为采样的 token 是在 overlap 流上异步算出来的，`sync_event.synchronize()` 确保 GPU 上的 `next_token_ids` 真正就绪，之后才能把它们写回 `shm_req` 供 Router/Detokenization 读取，否则会读到未初始化的脏数据。

**练习 3**：`InferReq` 和共享内存里的 `Req` 是什么关系？

**答案**：`Req` 是跨进程的 ctypes 结构（u2-l3），常驻共享内存；`InferReq` 是 GPU 进程内部的 Python 运行时对象，通过 `self.shm_req` 指向那个 `Req`。backend 计算 `cur_kv_len` / `cur_output_len` / `finish_status` 后，由 master rank 写回 `Req` 的对应字段，Router 和 Detokenization 再从共享内存读到这些结果。

---

## 5. 综合实践

把本讲三块知识串成一个端到端的理解任务：**追踪「一条请求从被 Router 选中，到在某个 GPU 上被推理一步」的完整控制流与数据流**。

要求你产出一份说明文档，覆盖：

1. **控制流（rpyc）**：Router 在启动期如何用 `ModelRpcClient.init_model` 把 backend 拉起来（参考 [manager.py:173-177](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/manager.py#L173-L177) 与 [model_rpc.py:139-142](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/model_rpc.py#L139-L142)）。说清为什么这是一次性的、之后不再用 rpyc 触发推理。
2. **数据流（共享内存）**：Router 的 `_add_batch`（[manager.py:301-309](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/manager.py#L301-L309)）如何把请求写进 `shm_reqs_io_buffer`，backend 的 `_try_read_new_reqs`（[base_backend.py:375-412](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/base_backend.py#L375-L412)）如何用 NCCL broadcast 协调各 rank 读取。
3. **推理一步**：`infer_loop`（[impl.py:53-101](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/chunked_prefill/impl.py#L53-L101)）如何分类、选走向，`prefill_normal`（[impl.py:103-145](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/chunked_prefill/impl.py#L103-L145)）如何用「四阶段 + overlap stream + event」把前向、采样、后处理交错起来。
4. **结果回流**：`_post_handle` 写回 `shm_req` 的哪些字段（参考 [infer_batch.py:938-948](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/infer_batch.py#L938-L948)），以及为什么只有 `is_master_in_dp` 的请求才写。

完成后，你应当能用一张图把「Router 进程」和「Model backend 进程」画在两边，中间标出 rpyc（启动期）和 `ShmObjsIOBuffer`（运行期）两条通道，并标注两个 `infer_loop` 线程的位置。

## 6. 本讲小结

- **每 GPU 一个进程**：`start_model_process` 为每个 rank 起独立进程，`ModelRpcServer` 以 rpyc `ThreadedServer` 监听 Unix socket，`ModelRpcClient` 在 Router 一侧持有连接。
- **rpyc 只做控制**：对外只暴露 `init_model`（选 backend + 初始化）和 `get_max_total_token_num`；`async_wrap` 用 `rpyc.async_` + `asyncio.to_thread` 把同步调用桥进 async 循环，实现多 rank 并行初始化。
- **`ModeBackend` 是模板基类**：`init_model` 顺序组装分布式环境、模型本体、RadixCache、全局上下文，最后启动**两个 `infer_loop` 线程**做双 batch overlap。
- **backend 是自驱的**：`infer_loop` 主动轮询共享内存，自己决定 prefill/decode/pass，Router 不通过 rpyc 触发每一步推理。
- **数据走共享内存**：Router 把请求写进 `shm_reqs_io_buffer` 并 `set_ready`，backend 的 `_try_read_new_reqs` 用 NCCL broadcast 协调各 rank 读取——这就是「rpyc 做控制、共享内存做数据」的分工。
- **单步前向是四阶段 overlap**：GPU 流上算 forward+采样并 record event，CPU 同时做前后处理，最后 synchronize 等 GPU 就绪再写回 `shm_req`，由 `InferReqUpdatePack` 解耦延迟更新。

## 7. 下一步学习建议

本讲建立了「Router → 共享内存 → backend → GPU 推理」这条链路的 backend 这一环。接下来建议：

- **u2-l5 Router 调度循环**：去看 Router 一侧的 `_step` 主循环如何决定「这一轮加不加新 batch」，与本讲的 `_add_batch` / `shm_reqs_io_buffer` 对接。
- **u3-l1 TpPartBaseModel 推理框架**：本讲里 `self.model.forward(model_input)` 是一个黑盒，下一单元会拆开模型基类，看 `forward` 内部如何组织 prefill/decode 两阶段。
- **u4-l1 / u4-l2 KV Cache 与 RadixCache**：本讲多次提到 `mem_manager` 和 `radix_cache`，第四单元专题讲解 token 级 KV 管理与前缀复用。
- **u6-l2 microbatch overlap**：本讲的「两个 infer_loop 线程 + overlap stream」是 overlap 的入门形态，第六单元会讲更激进的 microbatch/TPSP 重叠。
