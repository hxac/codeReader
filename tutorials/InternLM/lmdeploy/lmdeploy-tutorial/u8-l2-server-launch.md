# u8-l2 服务启动 launch_server

## 1. 本讲目标

在 u8-l1 里，我们已经看清了 `serve/openai/` 这个目录「装了哪些路由、用什么契约、怎么校验请求」。但有一个问题被刻意绕开了：**这些路由是怎么被跑起来的？敲下 `lmdeploy serve api_server ...` 之后，到底发生了什么？**

本讲就来补上这一段。学完后你应该能够：

1. 说清「单进程服务」与「多进程 DP 服务」两条启动路径的分叉点在哪里，各自调用 `serve()` 还是 `launch_server()`。
2. 看懂 `launch_server()` 如何按 `dp / tp / ep` 把 GPU 切给若干子进程、为每个子进程分配连续端口、再用进程轮询保证「一个挂全挂」。
3. 看懂 `serve()` 如何把异步引擎、路由、中间件、生命周期装配成一个 FastAPI 应用，并交给 uvicorn 跑起来。
4. 用 `APIClient`（或 curl）向已经起好的服务发一次 chat 请求，并理解它背后的 SSE 流式协议。
5. 认识 `server_port`、`dp`、`server_name` 等启动参数的真实含义与约束。

## 2. 前置知识

阅读本讲前，建议你已经掌握：

- **u8-l1**：`api_server.py` 的路由分四组（OpenAI 兼容 / lmdeploy 扩展 / 运维 / PD 分离）、`VariableInterface` 是进程级单例接口面、服务层强制 `stream_response=True`。
- **u2-l3 / u3-l2**：`PytorchEngineConfig` / `TurbomindEngineConfig` 里的 `tp`（张量并行）、`dp`（数据并行）、`ep`（专家并行）三个并行度字段。
- **u1-l5**：CLI 的「类体即注册 + `set_defaults(run=...)` 派发」模式——`SubCliServe.api_server` 就是 `serve api_server` 子命令的 `run` 回调。

两个本讲会用到的工程概念：

- **进程级单例（process-wide singleton）**：FastAPI 的每个路由是一个函数，函数签名里拿不到「引擎」这种重对象。lmdeploy 的做法是把引擎塞进一个类的类属性（`VariableInterface.async_engine`），所有路由通过类名访问它。这意味着「一个进程 = 一个引擎 = 一份服务」。
- **spawn 子进程**：Python 的 `multiprocessing` 用 `spawn` 方式启动子进程时，子进程会重新 import 父模块、拥有独立的 CUDA 上下文。这正是 DP（数据并行）想要的——每个 DP rank 独占一组 GPU、各跑一份完整模型、各开一个 HTTP 端口。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [lmdeploy/cli/serve.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/cli/serve.py) | `serve api_server` 命令的真实入口；在这里分叉到 `serve()` 或 `launch_server()` |
| [lmdeploy/serve/openai/launch_server.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/openai/launch_server.py) | 多进程多卡编排器；DP 模式下为每个 rank 起一个子进程 |
| [lmdeploy/serve/openai/api_server.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/openai/api_server.py) | 单进程服务装配；`serve()` 构建 FastAPI app 并交给 uvicorn |
| [lmdeploy/serve/openai/api_client.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/openai/api_client.py) | 纯 `requests` 实现的 OpenAI 兼容客户端，用于验证服务 |

## 4. 核心概念与源码讲解

### 4.1 launch_server：多进程多卡编排

#### 4.1.1 概念说明

当只有一个 DP 副本（`dp=1`）时，服务就是「一个进程、一份引擎、一个端口」，直接调 `serve()` 即可。但当 `dp>1`（数据并行，常用于把多张卡切成几组、每组各跑一份模型以提升吞吐），就需要在**一个命令**里同时拉起多个独立的服务进程。

`launch_server()` 就是这个「拉起多个进程」的编排器。它本身**不碰模型、不碰张量**，只做三件事：

1. 按 `dp / tp / ep` 算出「要起几个进程、每个进程分到哪几张卡」。
2. 给每个进程分配一个 HTTP 端口，用 `multiprocessing.Process`（spawn）把它们逐个启动，每个子进程内部最终还是调用 `serve()`。
3. 轮询所有子进程的退出码——任何一个挂掉，就杀掉全部、抛错退出（避免「一个副本静默死了，另一个还活着」的假健康状态）。

#### 4.1.2 核心流程

```
launch_server(backend_config, server_port, ...)
 │
 ├─ 断言：dp>1 且 (tp>1 或 ep>1)        # DP 多进程模式的前提
 ├─ num_devices  = max(dp, tp, ep)       # 至少需要的 GPU 数
 ├─ dp_per_node  = dp // num_nodes       # 本节点要起的进程数
 ├─ tp_per_dp    = num_devices // dp     # 每个进程分到的 GPU 数
 │
 ├─ 分配端口：
 │     proxy 模式 → find_available_ports() 随机找空端口
 │     否则       → base_port, base_port+1, ... 连续占段（默认 23333 起）
 │
 ├─ for idx in range(dp_per_node):       # 每个进程一份
 │     ├─ deepcopy backend_config，写上 dp_rank
 │     ├─ gpu_ids = [idx*tp_per_dp, (idx+1)*tp_per_dp)   # GPU 切片
 │     ├─ 子进程端口 = server_port_li[idx]
 │     └─ mp.Process(target=_run_server, ...).start()
 │           └─ _run_server 内部设 CUDA_VISIBLE_DEVICES → 调 serve()
 │
 ├─ 绑定 SIGINT/SIGTERM/SIGQUIT → cleanup_processes（杀全部子进程组）
 │
 └─ 轮询 while alive_processes:
         每个子进程 join(timeout=1)
         exitcode != 0 → 终止其余、抛 RuntimeError
```

这里的关键设计是「父进程只编排，子进程才真正干活」。父进程是个轻量的「看门狗」，它持有所有子进程的句柄，负责分配资源与回收。

#### 4.1.3 源码精读

先看入口的两个断言，它们划定了 DP 多进程模式的适用边界：

[launch_server.py:119-124](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/openai/launch_server.py#L119-L124) —— 断言 `dp>1` 且 `tp>1 or ep>1`，并据此推导三个派生量。`num_devices=max(dp,tp,ep)` 保证 GPU 数够切；`dp_per_node` 是本节点进程数，`tp_per_dp` 是每个进程分到的卡数。注意 `dp` 要能被 `num_nodes` 整除、`num_devices` 要能被 `dp` 整除，否则切片会出错。

端口分配有两条分支：

[launch_server.py:133-141](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/openai/launch_server.py#L133-L141) —— 若是 proxy 模式（`proxy_url` 非空且未显式指定端口）则随机找空端口；否则用 `server_port`（默认 23333）为起点，连续占 `dp_per_node` 个端口，并逐个用 `is_port_available` 校验可用、用 `_validate_server_port_range` 校验不越界。

```python
def _validate_server_port_range(base_port: int, dp_per_node: int) -> None:
    if not 1 <= base_port <= 65535:
        raise ValueError(...)
    end_port = base_port + dp_per_node - 1
    if end_port > 65535:
        raise ValueError(...)
```

[launch_server.py:41-48](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/openai/launch_server.py#L41-L48) —— 这就是 `server_port_range` 约束的来源：DP 模式下端口必须**连续**，`server_port` 是起始端口，占用区间为 `[base_port, base_port + dp_per_node - 1]`，且不能超过 65535。这也是「为什么 DP 模式的端口不能随便挑」的根本原因。

核心的多卡进程分配就在这个循环里：

[launch_server.py:143-162](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/openai/launch_server.py#L143-L162) —— 这是本模块最关键的一段。每个 `idx` 对应一个 DP 进程：

- `gpu_ids_per_dp = [idx*tp_per_dp, (idx+1)*tp_per_dp)` —— 把 GPU 按 `tp_per_dp` 切成连续段，第 `idx` 段分给第 `idx` 个进程。例如 8 卡 `dp=2, tp=4`：进程 0 占 GPU 0-3，进程 1 占 GPU 4-7。
- `backend_config_dp.dp_rank = dp_rank` —— 给每份深拷贝的配置写上全局 DP rank（多节点时还要叠加 `node_rank * dp_per_node`），引擎据此初始化分布式通信。
- `mp.Process(target=_run_server, args=(gpu_ids_per_dp, model_path), kwargs=cur_server_kwargs)` —— spawn 一个子进程。

子进程的真正入口是 `_run_server`：

[launch_server.py:59-72](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/openai/launch_server.py#L59-L72) —— 它先 `os.setpgrp()` 让自己成为新进程组组长（便于后续 `os.killpg` 整组杀），再把分到的 GPU 写进 `CUDA_VISIBLE_DEVICES`（这是「GPU 切片」落地的唯一手段——子进程只看得见这几张卡），最后调 `serve(model_path, **kwargs)`。注意异常分支用 `os._exit(1)` 而不是 `sys.exit()`：为了避免引擎/Ray 等非守护子进程在 atexit 清理时永久阻塞，直接以非零码结束进程组，让父进程的轮询捕获。

最后是「看门狗」轮询：

[launch_server.py:171-182](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/openai/launch_server.py#L171-L182) —— 父进程不能用简单的 `proc.join()` 等任意一个子进程（那样某个子进程崩溃会被另一个长期健康运行的进程挡住，看不到）。它用 `join(timeout=1)` 轮询所有存活进程，一旦发现某个 `exitcode != 0`，立即 `terminate_processes` 其余进程并抛 `RuntimeError`。这是「一个挂全挂」语义的实现。

#### 4.1.4 代码实践

**实践目标**：不开真实多卡、不起服务，仅靠阅读源码 + 计算，验证端口分配与 GPU 切片逻辑。

**操作步骤**：

1. 假设你有 8 张 GPU，想跑 `dp=2, tp=4`（单节点 `num_nodes=1`），`server_port=23333`。
2. 在源码里手工推演 `launch_server` 的三个派生量：
   - `num_devices = max(2, 4, ep=1) = ?`
   - `dp_per_node = 2 // 1 = ?`
   - `tp_per_dp = num_devices // dp = ?`
3. 推演循环 `for idx in range(dp_per_node)` 两次迭代里 `gpu_ids_per_dp` 分别是哪几张卡。
4. 推演 `server_port_li` 是哪两个端口。
5. 把 `_validate_server_port_range(23333, 2)` 代入，确认不会抛错；再试 `_validate_server_port_range(65535, 2)`，确认会抛错。

**需要观察的现象**：你应当得到「进程 0 → GPU 0-3 → 端口 23333；进程 1 → GPU 4-7 → 端口 23334」的结论，与源码 `gpu_ids_per_dp`、`server_port_li[idx]` 的语义一致。

**预期结果**：`num_devices=4`、`dp_per_node=2`、`tp_per_dp=2`。GPU 切片为 `[0,1]` 与 `[2,3,4,5,6,7]` ❌——注意这里要小心：`tp_per_dp = num_devices // dp = 4 // 2 = 2`，所以正确切片是进程 0 → GPU `[0,1]`、进程 1 → GPU `[2,3]`，共 4 张卡（不是 8 张）。这正是 `num_devices = max(dp, tp, ep)` 而非「总卡数」的含义——**待本地验证**：在你的真实硬件上用 `--tp 4 --dp 2` 跑一次 `lmdeploy serve api_server`，观察日志里 `gpus=` 与端口打印是否与推演一致。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `launch_server` 要用 `mp.set_start_method('spawn', force=True)`，而不能用默认的 `fork`？

**参考答案**：`fork` 会把父进程的内存与 CUDA 上下文原样复制给子进程，多份引擎会共享同一份 CUDA 状态导致混乱；而 `spawn` 让子进程从零开始 import、各自建立独立的 CUDA 上下文与 `CUDA_VISIBLE_DEVICES` 视图，是 DP 多副本隔离的前提。

**练习 2**：若 `server_port=23333` 且 `dp_per_node=4`，会占用哪些端口？如果其中 23334 已被占用，会怎样？

**参考答案**：占用 23333、23334、23335、23336。若 23334 被占用，[launch_server.py:139-141](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/openai/launch_server.py#L139-L141) 的 `is_port_available(port)` 返回 False，直接抛 `ValueError('Port 23334 is not available')`，服务起不来。

---

### 4.2 api_server：FastAPI 服务装配

#### 4.2.1 概念说明

`serve()` 是「单进程服务」的装配函数——无论是直接被 CLI 调用（`dp=1` 或 TurboMind），还是被 `launch_server` 的子进程调用（DP 模式），最终都会汇聚到它。它像一个「工厂」：吃进模型路径与一堆配置，吐出一个跑在 uvicorn 上的 FastAPI 应用。

`serve()` 的职责可以归纳为四件：

1. **造引擎**：根据 `backend` 选 `AsyncEngine` 或 `VLAsyncEngine`（经 `get_task`），实例化后挂到 `VariableInterface.async_engine`——这就是 u8-l1 提到的「进程级单例」。
2. **建应用**：新建 FastAPI app，挂上路由（OpenAI 兼容、Anthropic、Responses 三套）、异常处理、可选的 metrics 挂载。
3. **加中间件**：CORS、API Key 鉴权、引擎睡眠拦截、并发上限。
4. **交运行**：调 `uvicorn.run(app=app, host=..., port=...)`，进入阻塞式服务循环。

注意 `serve()` 里**没有**任何路由定义——路由是模块级用 `@router.post(...)` 装饰器在 import 时就注册好的（见 u8-l1），`serve()` 只负责把它们 `include_router` 进 app。

#### 4.2.2 核心流程

```
serve(model_path, backend, backend_config, server_name, server_port, ...)
 │
 ├─ 设置日志级别、解析 reasoning/tool parser、解析默认 generation_config
 ├─ get_task(backend, model_path, ...) → (task_type, pipeline_class)
 ├─ 若 PytorchEngineConfig：enable_mp_engine=True（启用多进程引擎）
 ├─ VariableInterface.async_engine = pipeline_class(...)
 │
 ├─ lifespan = create_lifespan_handler(...)   # 引擎健康监控 + metrics 定时上报
 ├─ app = FastAPI(docs_url=..., lifespan=lifespan)
 ├─ app.include_router(router)                # OpenAI 路由
 ├─ app.include_router(create_anthropic_router(...))
 ├─ app.include_router(create_responses_router(...))
 ├─ app.add_exception_handler(RequestValidationError, ...)
 ├─ mount_metrics(app, backend_config)        # 挂 /metrics（可选）
 ├─ app.add_middleware(CORSMiddleware, ...)
 ├─ app.add_middleware(AuthenticationMiddleware, ...)   # 若有 api_keys
 ├─ app.add_middleware(EngineSleepingMiddleware, ...)
 ├─ app.add_middleware(ConcurrencyLimitMiddleware, ...) # 若有 max_concurrent_requests
 │
 └─ uvicorn.run(app, host=server_name, port=server_port, ...)
       │
       ├─ lifespan 启动：health_monitor.start()、metrics 后台任务
       ├─ startup_event：async_engine.start_loop(...)    # 真正启动推理循环
       └─ （阻塞，处理请求）
       └─ shutdown_event：async_engine.close()
```

#### 4.2.3 源码精读

先看引擎的选择与构造：

[api_server.py:1603-1621](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/openai/api_server.py#L1603-L1621) —— `get_task(backend, model_path, ...)` 根据 `backend` 与模型是否多模态返回 `pipeline_class`（`AsyncEngine` 或 `VLAsyncEngine`，承接 u2-l5）。随后对 PyTorch 后端打开 `enable_mp_engine=True`（让引擎以多进程模式运行，`distributed_executor_backend` 控制具体后端），最后实例化引擎并挂到 `VariableInterface.async_engine`。从这一行起，所有路由就能通过 `VariableInterface.async_engine` 访问到引擎。

接下来是 FastAPI app 的组装：

[api_server.py:1625-1636](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/openai/api_server.py#L1625-L1636) —— `create_lifespan_handler` 造一个 lifespan 上下文管理器（下面细讲），`FastAPI(docs_url='/', lifespan=lifespan)` 建应用，然后 `include_router` 把三套路由（OpenAI 主路由、Anthropic 兼容、Responses API）全部挂上。`disable_fastapi_docs=True` 时会把 docs/redoc/openapi 三个文档端点全关掉（生产环境常用，避免暴露接口清单）。

lifespan 是「应用启动前 / 关闭后」的钩子，比 `@router.on_event` 更现代：

[api_server.py:1467-1502](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/openai/api_server.py#L1467-L1502) —— `lifespan_handler` 在 `yield` 之前启动 `EngineHealthMonitor`（健康监控）、可选地启动 metrics 后台协程（每 10 秒拉一次调度指标并强制写日志）；`yield` 之后（应用关闭时）停止监控、取消任务、停 metrics。注意 `VariableInterface.health_monitor` 的赋值/摘除都在这里，保证它与 app 生命周期一致。

但「真正启动推理循环」的动作其实发生在更老的 `startup_event` 里：

[api_server.py:1381-1384](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/openai/api_server.py#L1381-L1384) —— `async_engine.start_loop(asyncio.get_running_loop(), use_async_api=True)` 把引擎的内部推理循环绑定到 uvicorn 跑的那个 asyncio 事件循环上。承接 u4-l1/u4-2：引擎的 `EngineLoop`（preprocess/main/send_response 等协程）从这一刻起开始运转，否则路由收到请求也无人处理。proxy 模式下，启动事件还会向 `proxy_url/nodes/add` 注册自己（见 u8-l4）。

中间件按「越后添加越外层」的顺序包裹 app：

[api_server.py:1638-1657](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/openai/api_server.py#L1638-L1657) —— CORS（默认放行所有来源）、可选的 API Key 鉴权（`api_keys` 非空时启用 `AuthenticationMiddleware`）、`EngineSleepingMiddleware`（引擎在 sleep 状态时拒绝请求，配合 `/sleep`、`/wakeup` 路由）、可选的 `ConcurrencyLimitMiddleware`。并发上限中间件用一个 `asyncio.Semaphore` 实现：

[api_server.py:1424-1433](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/openai/api_server.py#L1424-L1433) —— 每个 HTTP 请求进入时先 `async with semaphore`，超过 `max_concurrent_requests` 的请求会排队等待。这是「过载保护」——避免瞬时海量请求把引擎队列堆爆。

最后是 uvicorn 阻塞式运行：

[api_server.py:1666-1672](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/openai/api_server.py#L1666-L1672) —— `serve()` 函数的最后一行，也是「服务真正开始监听」的那一刻。`host`/`port` 来自 `server_name`/`server_port`，`log_level` 默认从环境变量 `UVICORN_LOG_LEVEL`（默认 `info`）取，`timeout_keep_alive` 默认 5 秒。SSL 时还会传 `ssl_keyfile`/`ssl_certfile`。**这行返回之前，整个进程都在服务请求**——这也是为什么 `launch_server` 的子进程会在 `_run_server` 里调它并长期阻塞。

#### 4.2.4 代码实践

**实践目标**：在本地起一个最小服务，并确认「lifespan → startup_event → uvicorn 监听」这条启动链真的发生了。

**操作步骤**：

1. 选一个小模型（例如 `Qwen/Qwen2.5-0.5B-Instruct`，显存够小可在 CPU/单卡跑），执行：
   ```bash
   lmdeploy serve api_server Qwen/Qwen2.5-0.5B-Instruct \
       --backend pytorch --model-name qwen-test --server-port 23333
   ```
2. 观察启动日志，找到三处关键输出：
   - `HINT: Please open http://0.0.0.0:23333 in a browser ...`（[api_server.py:1662-1665](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/openai/api_server.py#L1662-L1665) 打印三次）。
   - 引擎加载完成、`start_loop` 被调用的痕迹（可加 `--log-level INFO`）。
   - uvicorn 的 `Uvicorn running on http://0.0.0.0:23333`。
3. 在另一个终端 `curl http://localhost:23333/v1/models`，应当返回 `qwen-test`。
4. `Ctrl-C` 停止，观察 `shutdown_event` 是否打印引擎关闭日志。

**需要观察的现象**：服务启动顺序为「加载模型 → start_loop → uvicorn 开始监听」；`/v1/models` 在监听后即可访问；停止时引擎被 `close()`。

**预期结果**：若环境无 GPU 或模型下载失败，可能起不来——**待本地验证**。即使起不来，也请把 `--log-level INFO` 下看到的最后几行日志与上述三个代码点对应起来，确认启动链路。

#### 4.2.5 小练习与答案

**练习 1**：`serve()` 里为什么把 `async_engine` 挂到 `VariableInterface` 的类属性上，而不是用 FastAPI 的依赖注入（`Depends`）传给每个路由？

**参考答案**：引擎是个重对象，全局只需一份；用类属性做进程级单例，路由函数零参数即可访问，签名简洁。FastAPI 的 `Depends` 更适合「每个请求独立、需要清理」的轻量资源，把全局唯一对象塞进 `Depends` 反而徒增样板代码。

**练习 2**：`max_concurrent_requests` 与引擎自身的 `max_batch_size` 是一回事吗？

**参考答案**：不是。`max_concurrent_requests`（`ConcurrencyLimitMiddleware`）限制的是「HTTP 层同时进入的请求数」，超过则排队；`max_batch_size`（引擎调度层）限制的是「同一个 forward batch 里能塞多少序列」。前者是过载保护闸门，后者是调度吞吐旋钮，两者层级不同。

---

### 4.3 api_client：OpenAI 兼容客户端

#### 4.3.1 概念说明

服务起好了，怎么验证它工作正常？lmdeploy 提供了一个极简客户端 `api_client.py`。它**只依赖 `requests`**（不依赖 openai SDK、不依赖 lmdeploy 引擎），用最直白的 HTTP 调用演示了「如何与一个 OpenAI 兼容服务对话」。

`APIClient` 不是给生产用的完整 SDK，而是一个**教学/调试用的样例客户端**：它把每个 OpenAI 端点（`/v1/chat/completions`、`/v1/completions`、`/v1/models`、`/v1/encode`）封装成一个方法，并亲手解析 SSE（Server-Sent Events）流式响应。读懂它，你就能用任何语言的任何 HTTP 库与 lmdeploy 服务对话。

#### 4.3.2 核心流程

```
APIClient(api_server_url, api_key=None)
 │  拼出四个端点 URL，准备 headers（含可选 Authorization）
 │
 ├─ available_models    → GET  /v1/models         → ['model-name', ...]
 ├─ encode(input, ...)  → POST /v1/encode         → (input_ids, length)
 ├─ chat_completions_v1(model, messages, ..., stream=False)
 │     POST /v1/chat/completions, json=pload, stream=stream
 │     for chunk in response.iter_lines(delimiter=b'\n'):
 │         stream=True  → 剥 'data: ' 前缀，跳过 '[DONE]'，json_loads 后 yield
 │         stream=False → 整体 json_loads 后 yield
 └─ completions_v1(...) → POST /v1/completions（同上，但是 prompt 而非 messages）
```

`pload` 的构造用了一个 Python 小技巧：`{k: v for k, v in locals().copy().items() if ...}`——把方法的所有局部变量（也就是所有参数）原样打包成请求体，省去逐个手写。

#### 4.3.3 源码精读

`APIClient` 的构造：

[api_client.py:48-58](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/openai/api_client.py#L48-L58) —— 拼出四个端点 URL，准备 `headers`。若传了 `api_key`，加上 `Authorization: Bearer <key>`——这与 `serve()` 里的 `AuthenticationMiddleware` 对应：服务端启用了 api_keys 时，客户端必须带这个头。

`chat_completions_v1` 是核心，尤其要读懂它的流式解析：

[api_client.py:158-173](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/openai/api_client.py#L158-L173) —— 先用 `locals().copy()` 把所有参数打包成 `pload`，`requests.post(..., stream=stream)` 发出请求。然后 `response.iter_lines(chunk_size=8192, delimiter=b'\n')` 按行迭代响应体。流式分支会：

- 跳过 `data: [DONE]`（SSE 结束标记）；
- 剥掉每行开头的 `data: ` 前缀（SSE 协议规定）；
- `json_loads` 解析成 dict 后 `yield`。

非流式分支直接把整行 `json_loads` 后 yield（服务端虽强制内部流式，但非流式请求会被服务层消费 generator 后整体返回，见 u8-l1）。

`available_models` 属性演示了「懒加载」：

[api_client.py:60-66](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/openai/api_client.py#L60-L66) —— 首次访问时才发 `GET /v1/models`，结果缓存到 `self._available_models`，后续访问直接返回。对应的 `get_model_list` 函数：

[api_client.py:10-24](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/openai/api_client.py#L10-L24) —— 发 GET、取 `.json()['data']`、抽出每个 `item['id']`，返回模型名列表。这就是 OpenAI `/v1/models` 协议的标准结构（`{"data": [{"id": ...}, ...]}`）。

#### 4.3.4 代码实践

**实践目标**：用 `APIClient` 向本地服务发一次 chat 请求，并对照源码理解每一段。

**操作步骤**：

1. 先按 4.2.4 起好服务（`--server-port 23333`）。
2. 写一个 `client_demo.py`：
   ```python
   from lmdeploy.serve.openai.api_client import APIClient

   api_client = APIClient('http://0.0.0.0:23333')
   print('models:', api_client.available_models)        # 触发 GET /v1/models

   model_name = api_client.available_models[0]
   # 非流式
   for output in api_client.chat_completions_v1(
           model=model_name,
           messages=[{'role': 'user', 'content': '你好，请用一句话介绍自己'}],
           temperature=0.7):
       print('non-stream:', output['choices'][0]['message']['content'])

   # 流式
   for output in api_client.chat_completions_v1(
           model=model_name,
           messages=[{'role': 'user', 'content': '写两个 Python 的好处'}],
           stream=True):
       delta = output['choices'][0]['delta'].get('content', '')
       print(delta, end='', flush=True)
   print()
   ```
3. 运行 `python client_demo.py`。

**需要观察的现象**：非流式调用会一次性打印完整回复；流式调用会逐字打印（看到 token 一个个蹦出来）。在 `chat_completions_v1` 的 for 循环里打断点或加 `print(output)`，可以看到每个 SSE chunk 的原始结构。

**预期结果**：若服务正常，应看到模型回复。若连接被拒，先确认 `curl http://0.0.0.0:23333/v1/models` 能通——**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `chat_completions_v1` 用 `yield` 而不是 `return`？即使 `stream=False` 也是生成器。

**参考答案**：因为方法体里有 `yield`，整个函数就是生成器函数，无论 `stream` 取值都返回生成器。非流式时服务端只回一段，生成器只 yield 一次；流式时 yield 多次。用生成器统一两种语义，调用方用 `for ... in ...` 消费即可。

**练习 2**：用 `curl` 模拟 `APIClient.chat_completions_v1` 发一个流式 chat 请求，应该怎么写？

**参考答案**：
```bash
curl -N http://localhost:23333/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{"model":"qwen-test","messages":[{"role":"user","content":"hi"}],"stream":true}'
```
`-N` 禁用缓冲以实时看到流式输出；响应里每个事件都是 `data: {...}\n` 形式，结尾是 `data: [DONE]`，与 [api_client.py:162-168](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/openai/api_client.py#L162-L168) 的解析逻辑一一对应。

---

## 5. 综合实践

把本讲三个最小模块串起来，完成一次「从命令行到客户端」的完整链路追踪。

**任务**：

1. **选择路径**：阅读 [cli/serve.py:297-348](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/cli/serve.py#L297-L348)，回答：在什么条件下 CLI 走 `serve()`，什么条件下走 `launch_server()`？把判定条件写在笔记里。
2. **启动服务（单进程路径）**：用 `--backend pytorch --tp 1 --dp 1` 起一个 0.5B 模型服务，端口 23333。在启动日志里标出三处：① 引擎实例化（`VariableInterface.async_engine` 赋值后）、② `start_loop`（`startup_event`）、③ uvicorn 监听。
3. **多进程推演（不必真跑）**：假设把命令改成 `--tp 2 --dp 2`，在 [launch_server.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/openai/launch_server.py) 里推演会起几个子进程、各占哪些 GPU、各监听哪个端口，把结果画成表格。
4. **客户端验证**：服务起好后，分别用 `APIClient` 和 `curl -N` 各发一次流式 chat 请求，对照 [api_client.py:160-169](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/openai/api_client.py#L160-L169) 确认你收到的每个 `data: {...}` 都能被正确解析。
5. **关闭观察**：`Ctrl-C` 后，对照 [api_server.py:1406-1410](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/openai/api_server.py#L1406-L1410) 确认 `shutdown_event` 调用了 `async_engine.close()`。

**验收标准**：能画出「CLI `serve api_server` →（分叉）→ `serve()` 或 `launch_server()` → `serve()`（子进程内）→ FastAPI 装配 → uvicorn → lifespan/startup → 路由处理 → APIClient/curl」这条完整时序图，并标注每个环节对应的源码行号。

## 6. 本讲小结

- **两条启动路径的分叉点在 CLI**：`cli/serve.py` 的 `api_server()` 里，`dp==1` 或 TurboMind 后端走 `serve()`（单进程），否则走 `launch_server()`（多进程 DP 编排）。
- **`launch_server()` 只编排不干活**：它按 `dp/tp/ep` 推导 GPU 切片与端口分配，用 `mp.Process(spawn)` 起子进程，每个子进程内部仍调 `serve()`；父进程用轮询保证「一个挂全挂」。
- **`server_port_range` 约束来自连续端口占用**：DP 模式下端口必须从 `server_port` 起连续占 `dp_per_node` 个，且不能超 65535。
- **`serve()` 是 FastAPI 装配工厂**：造引擎 → 挂到 `VariableInterface` → 建 app 并 `include_router` 三套路由 → 加四类中间件 → `uvicorn.run`。
- **引擎的真正启动在 `startup_event`**：`async_engine.start_loop(...)` 把推理循环绑到 uvicorn 的事件循环上，路由才开始有人响应。
- **`APIClient` 是纯 `requests` 的教学客户端**：演示了 OpenAI 兼容端点的调用与 SSE 流式解析（剥 `data: ` 前缀、跳 `[DONE]`），是理解服务协议的最短路径。

## 7. 下一步学习建议

- **u8-l3 异步引擎封装 async_engine**：本讲里 `VariableInterface.async_engine` 是个「黑盒」，下一讲拆开 `AsyncEngine` 看它如何把底层 `Engine` 包装成可被 FastAPI 路由调用的异步推理引擎，以及 `start_loop`、`generate`、`is_sleeping` 等方法的实现。
- **u8-l4 代理与多机部署 proxy**：本讲的 `launch_server` 是「单节点多进程」，`proxy_url` 分支只点到为止；下一讲讲清 proxy 如何把多个 api_server 节点注册、路由、流式转发。
- **延伸阅读**：若你对「DP 多进程引擎」内部如何通信感兴趣，可跳读 `lmdeploy/pytorch/config.py` 的 `DistConfig` 与 `distributed_executor_backend` 相关代码（承接 u9-l4）。
