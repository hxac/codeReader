# 微服务与路由器

## 1. 本讲目标

学完本讲，你应当能够：

- 说清「微服务（Microserving）模式」要解决什么问题，以及它和单一 `mlc_llm serve` 在部署形态上的本质区别。
- 把握路由器（Router）在系统中的定位：它本身不是推理引擎，而是一个**编排/分发器**——对内拉起多个引擎子进程，对外只暴露 OpenAI 兼容入口。
- 读懂 `cli/router.py`（命令行层）→ `interface/router.py`（FastAPI 编排层）→ `router/router.py`（Router 类实现）→ `microserving_entrypoints.py`（子请求级端点）这一整条调用链。
- 理解两种内置调度策略：`round-robin`（请求级负载均衡）与 `disagg`（prefill-decode 分离式）。
- 掌握自定义路由的扩展点 `translate_request`，并会读懂 `examples/python/microserving/custom_router.py` 如何把一个 OpenAI 请求翻译成 `prep_recv → remote_send → start_generate` 三段微服务调用。

## 2. 前置知识

本讲是 **advanced** 级别，承接以下已建立的认知（请确认你已掌握）：

- **u11-l2（REST 服务器）**：`mlc_llm serve` 用 `AsyncMLCEngine` + `ServerContext` + FastAPI + uvicorn 暴露 OpenAI 端点；端点靠 `Depends` 取上下文、`current()` 取全局单例。本讲的「微服务」就是在这套 REST 服务器之上再加一层路由。
- **u9（C++ 引擎）**：每个引擎子进程背后都是同一个 C++ `ThreadedEngine`，`Step()` 是心跳。
- **u10-1（分页 KV 缓存）与 u10-2（前缀缓存）**：分离式推理要跨引擎搬运 KV cache，分页与前缀缓存是它的物理基础。
- **u12-1（多 GPU / 张量并行 / disco）**：本讲会用到 `disco` 分布式会话与 **nvshmem**（GPU 间 KV 传输的底层传输层）。

两个需要先建立的直觉：

1. **什么是「分离式推理（disaggregation）」？** 一次 LLM 推理分两段：**prefill**（吃整条 prompt，算 KV，是 compute-bound、可并行的大块计算）与 **decode**（逐 token 自回归生成，是 memory-bound）。把这两段拆给**不同**的引擎实例去做——专门的 prefill 实例算完 KV，再把 KV 传给 decode 实例继续生成——就是分离式推理。它能分别针对两段调优资源、提高吞吐。
2. **什么是「RISC 风格的子请求 API」？** 普通的 OpenAI `/v1/completions` 是一个「黑盒大请求」：给 prompt，拿回答，引擎内部如何 prefill/decode 不可控。Microserving 把这个黑盒拆成几个**子请求原语**（准备接收 KV、远程发送 KV、开始生成），让你像搭积木一样在 Python 里编排跨引擎的协作流程。这正是本讲主角「路由器」要做的事。

## 3. 本讲源码地图

| 文件 | 角色 | 作用 |
|------|------|------|
| [python/mlc_llm/cli/router.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/cli/router.py) | 命令行层（CLI） | 解析 `mlc_llm router` 的 argv，转调接口层。 |
| [python/mlc_llm/interface/router.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/router.py) | 接口层（FastAPI 编排） | `serve()` 函数：实例化 Router、组装 FastAPI app、注册 `/v1/completions`、跑 uvicorn。 |
| [python/mlc_llm/router/router.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/router/router.py) | Router 核心实现 | 用 `PopenServer` 拉起多个引擎子进程，实现两种调度与抢占回退。 |
| [python/mlc_llm/serve/entrypoints/microserving_entrypoints.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/entrypoints/microserving_entrypoints.py) | 子请求级端点 | `prep_recv`/`remote_send`/`start_generate` 三个 RISC 原语端点。 |
| [python/mlc_llm/protocol/microserving_protocol.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/protocol/microserving_protocol.py) | 协议模型 | 三种子请求的 Pydantic 请求/响应体。 |
| [examples/python/microserving/custom_router.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/examples/python/microserving/custom_router.py) | 自定义路由示例 | 继承 `Router`、覆写 `translate_request`，手写分离式三段流程。 |
| [python/mlc_llm/serve/server/popen_server.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/server/popen_server.py) | 子进程服务器封装 | 用 `subprocess.Popen` 跑 `mlc_llm serve` 子进程，轮询直到就绪。 |
| [docs/microserving/tutorial.rst](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/docs/microserving/tutorial.rst) | 教程文档 | 官方对微服务理念与自定义 Router 用法的逐段讲解。 |

整条链路的层次关系一句话概括：**`cli` 解析参数 → `interface.serve` 起 FastAPI 并暴露 `/v1/completions` → 每个请求交由 `Router.handle_completion` → `translate_request` 决定调度策略 → 经 `send_*` HTTP 调用打到各引擎子进程的 `/microserving/*` 端点**。

---

## 4. 核心概念与源码讲解

### 4.1 微服务部署模式：为什么需要路由器

#### 4.1.1 概念说明

回忆 u11-l2：单一 `mlc_llm serve` 是「一个进程、一个引擎、一套 OpenAI 端点」。它把 prefill 与 decode 都放在同一个 `ThreadedEngine` 里，靠连续批处理（continuous batching）与 Action 循环调度。这套设计在**单实例**下已经很好，但在**生产部署**时有两类需求它直接搞不定：

1. **横向扩展**：一张卡装不下全部流量，要跑多份模型副本做负载均衡。
2. **分离式推理**：把 prefill 和 decode 分别交给不同实例，让 prefill 实例吃满算力、decode 实例吃满显存带宽，二者各自独立批处理，整体吞吐更高。

这两类需求的共同点是：**客户端只该看到一个入口，但背后有多个引擎在协作。** 于是需要一个位于客户端与引擎之间的「中间层」来分发与编排——这就是**路由器（Router）**。

关键认知（必须牢记，下面所有源码都围绕它）：

> **路由器本身不做推理。** 它对内用 `subprocess.Popen` 拉起 N 个真正的 `mlc_llm serve` 子进程（每个子进程各有一个完整的 `AsyncMLCEngine` + C++ `ThreadedEngine`），对外只暴露一个 OpenAI 兼容入口 `/v1/completions`，再根据调度策略把请求分发到这些子进程。

这与单一 `serve` 的本质区别可归纳为：

| 维度 | 单一 `mlc_llm serve` | 微服务 `mlc_llm router` |
|------|----------------------|--------------------------|
| 进程数 | 1 个引擎进程 | 1 个路由进程 + N 个引擎子进程 |
| 引擎 | 直接持有 `AsyncMLCEngine` | 路由不持有引擎，靠 HTTP 调子进程 |
| 对外端点 | `/v1/chat/completions`、`/v1/completions`、`/v1/embeddings`、`/v1/models` | **仅 `/v1/completions`**（见 4.2.3） |
| 内部端点 | 无 | 每个引擎子进程额外暴露 `/microserving/*` |
| 典型用途 | 单机单模型对话/服务 | 多副本负载均衡、prefill-decode 分离 |

#### 4.1.2 核心流程

一次微服务部署的启动流程（伪代码）：

```
Router.__init__:
    1. 把 hosts/ports/num_gpus 拼成 server_urls = ["http://h0:p0", "http://h1:p1", ...]
    2. 计算 device_id_starts（每个端点占哪些 GPU 的起始编号）
    3. 调 tvm 全局函数拿到 nvshmem uid（构建跨 GPU 通信世界）
    4. 用多线程并发启动 N 个 PopenServer 子进程（必须并发，见 4.3.2）
    5. 等所有子进程就绪，加载 tokenizer

interface.serve:
    6. router = Router(...)
    7. app = FastAPI(); app 注册 POST /v1/completions
    8. uvicorn.run(app)  # 阻塞，对外服务
```

一次请求的处理流程（`disagg` 模式，详见 4.3）：

```
客户端 POST /v1/completions
  → Router.handle_completion（必要时装 prompt、加 DebugConfig、preempt 重试循环）
    → Router.translate_request（调度策略入口）
      → _handle_completion_disagg:
          ① POST decode引擎 /microserving/prep_recv   → 拿 KV 接收地址 + 前缀命中长度
          ② POST prefill引擎 /microserving/remote_send → prefill 并把 KV 传给 decode 引擎
          ③ POST decode引擎 /microserving/start_generate → prefill 末 token 并开始 decode，流式回吐
```

#### 4.1.3 源码精读

先看路由器拉起引擎子进程的关键代码。`Router.__init__` 会用 `PopenServer` 启动 N 个子进程，而 `PopenServer.start` 的核心就是拼出一条 `mlc_llm serve` 命令并 `subprocess.Popen` 执行：

[python/mlc_llm/serve/server/popen_server.py:64-91](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/server/popen_server.py#L64-L91) — 拼接 `python -m mlc_llm serve <model> --device ... --mode server ...` 命令。注意它把 `engine_config`（如 `prefix_cache_mode`、`gpu_memory_utilization`）翻译成 `--overrides` 透传给子进程。

[python/mlc_llm/serve/server/popen_server.py:123-145](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/server/popen_server.py#L123-L145) — 真正用 `subprocess.Popen` 拉起子进程，然后**轮询 `/v1/models` 直到子进程就绪**（最多 120 秒）。这段解释了「为什么路由器是子进程编排器」：每个端点都是一个独立、完整的 `serve` 进程。

再看 `Router.__init__` 如何调度多个 `PopenServer`：

[python/mlc_llm/router/router.py:45-51](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/router/router.py#L45-L51) — 把 `hosts[i]:ports[i]` 拼成 `server_urls`，这是后续所有 HTTP 转发的目标地址。

[python/mlc_llm/router/router.py:65-68](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/router/router.py#L65-L68) — 计算 `device_id_starts`。例如 `num_gpus=[2,2]`，则 `device_id_starts=[0,2,4]`：端点 0 用 GPU 0–1，端点 1 用 GPU 2–3，最后一个元素 `4` 是 GPU 总数。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：从源码确认「路由器只暴露 `/v1/completions`、背后是 N 个子进程」这一结论。
2. **操作步骤**：
   - 打开 `python/mlc_llm/interface/router.py`，统计 `@router_app.post(...)` 注册了哪些端点。
   - 打开 `python/mlc_llm/router/router.py`，在 `__init__` 里找到 `self.servers: List[PopenServer]` 与 `start_server`，确认每个端点都是一个 `PopenServer`。
3. **需要观察的现象**：接口层只注册了 `/v1/completions` 一个端点；`Router` 持有的 `self.servers` 长度等于 `len(hosts)`。
4. **预期结果**：能口头复述「微服务 = 1 个路由 FastAPI + N 个 PopenServer 子进程，对外只有 completions 端点」。
5. 运行结果：**待本地验证**（微服务部署需要多 GPU 与已编译模型，本实践为源码阅读型）。

#### 4.1.5 小练习与答案

**练习 1**：为什么路由器对外只暴露 `/v1/completions`，而不像单一 `serve` 那样也暴露 `/v1/chat/completions`？

> **参考答案**：路由器的核心职责是「编排 prefill/decode 分离或负载均衡」，这种编排发生在 **completion（裸 prompt + 生成）层面**。chat completion 多出来的「对话模板拼装」与多轮上下文管理属于应用层语义，与跨引擎调度无关；让上游先把对话渲染成 prompt（completion）再进来，路由逻辑更纯粹。这也是 `Router.__init__` 末尾要 `self.tokenizer = Tokenizer(model)` 的原因——它只在需要把字符串 prompt 编码成 token 时用，不做对话拼装。

**练习 2**：`Router` 类里没有任何 `import` 形如 `MLCEngine` 的引擎对象，它怎么完成推理？

> **参考答案**：它不自己推理，而是用 `PopenServer` 拉起 N 个 `mlc_llm serve` 子进程，再用 `aiohttp` 通过 HTTP（`/v1/completions` 或 `/microserving/*`）把请求转发过去。引擎逻辑全在子进程里。

---

### 4.2 router CLI 与 serve 接口的分层编排

#### 4.2.1 概念说明

和 u2-l1 讲过的「cli/ 入口层 + interface/ 接口层」两层结构完全一致，`router` 子命令也遵循这套分层：

- **`cli/router.py`**：命令行入口层。只做 argv 解析（`ArgumentParser`），把命令行字符串翻译成结构化参数，然后转调接口层。
- **`interface/router.py`**：Python 接口层。含真正实现 `serve()`——实例化 Router、组装 FastAPI、跑 uvicorn。

这种分层的好处（和 compile/serve 命令同理）：同一套 `serve()` 实现既能被 `mlc_llm router` 命令行调用，也能被 `examples/python/microserving/custom_router.py` 这样的 Python 脚本直接 `from mlc_llm.interface.router import serve` 调用——这正是自定义路由能「以 Python 代码扩展」的前提。

接口层还预留了一个关键扩展点：`serve()` 的参数 `router_type: Type[Router] = Router`。默认用内置 `Router`，但允许你**传入自定义子类**（如 `CustomRouter`），从而在不改一行框架代码的前提下替换调度逻辑。

#### 4.2.2 核心流程

```
mlc_llm router <model> --router-mode disagg --endpoint-ports 9124 9125 ...
   │
   ▼  sys.argv[1:2] = ["router"]
__main__.py 分发到 cli/router.py:main
   │
   ▼  解析 argv
cli/router.py 构造 ArgumentParser，parse_args
   │
   ▼  转调
interface/router.py:serve(model, model_lib, router_host, ..., router_mode, pd_balance_factor, router_type=Router)
   │
   ▼
Router(...) → FastAPI app → uvicorn.run
```

CLI 层的几个关键参数含义：

| 参数 | 含义 |
|------|------|
| `model` | 模型目录（位置参数，必填） |
| `--model-lib` | 编译好的模型库路径 |
| `--router-mode` | `disagg`（分离式，默认）或 `round-robin`（轮询多副本） |
| `--router-host/--router-port` | 路由器对外监听地址 |
| `--endpoint-hosts` | 各引擎端点主机，逗号分隔 |
| `--endpoint-ports` | 各引擎端点端口，空格分隔 |
| `--endpoint-num-gpus` | 各端点占用的 GPU 数，空格分隔 |
| `--enable-prefix-cache` | 是否开启前缀缓存（radix） |
| `--pd-balance-factor` | 把多少比例的 prefill 挪给 decode 引擎做（见 4.3.4） |

#### 4.2.3 源码精读

先确认 `router` 是 `__main__.py` 八个子命令之一：

[python/mlc_llm/__main__.py:25](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/__main__.py#L25) 与 [python/mlc_llm/__main__.py:58-61](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/__main__.py#L58-L61) — `router` 在子命令 choices 列表里，匹配后懒加载 `mlc_llm.cli.router` 并调用 `cli.main(sys.argv[2:])`。

CLI 层解析参数并转调接口层：

[python/mlc_llm/cli/router.py:8-15](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/cli/router.py#L8-L15) — `main(argv)` 入口，定义把逗号字符串切成列表的 `list_of_strings` 类型，构造 `ArgumentParser`。

[python/mlc_llm/cli/router.py:27-45](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/cli/router.py#L27-L45) — 关键的「路由器参数」：`--router-mode`（choices 限定为 `disagg`/`round-robin`）、`--router-host/--router-port`（路由自身地址）、`--endpoint-hosts/--endpoint-ports`（子引擎地址）。注意端点主机用逗号、端口用空格——两种分隔符不一致，是历史遗留。

[python/mlc_llm/cli/router.py:79-90](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/cli/router.py#L79-L90) — 把解析结果原样转给 `interface.router.serve(...)`。CLI 层完全不做业务，只是参数搬运工。

接口层 `serve()` 的签名与编排：

[python/mlc_llm/interface/router.py:17-29](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/router.py#L17-L29) — `serve()` 的参数列表。最后一行 `router_type: Type[Router] = Router` 就是**自定义路由扩展点**：默认内置 `Router`，但 `custom_router.py` 会传 `router_type=CustomRouter`。

[python/mlc_llm/interface/router.py:31-41](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/router.py#L31-L41) — 实例化路由器。`router_type(...)` 用传入的类（默认 `Router`）构造对象，把端点地址、GPU 数、模式都传进去。注意实例化发生在 `serve()` 内部，所以自定义子类的 `__init__`/`translate_request` 都会自然生效。

[python/mlc_llm/interface/router.py:45-77](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/router.py#L45-L77) — 唯一对外端点 `POST /v1/completions`。流式分支采用与 u11-l2 同款的「手动 `await anext` 拉第一块以在本作用域捕获早期异常」两段式 async generator（注释行 57–60 解释了原因），再把每块包成 SSE `data: ...\n\n`。**注意**：源码 79 行有 FIXME「Non-streaming response not fully implemented」，说明微服务当前以**流式**为主路径。

[python/mlc_llm/interface/router.py:118-125](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/router.py#L118-L125) — 组装 FastAPI app：挂 CORS 中间件、`include_router` 注册端点、注册 `BadRequestError` 异常处理器，最后 `uvicorn.run` 阻塞服务。这套横切关注点与单一 `serve` 一致（参见 u11-l2）。

#### 4.2.4 代码实践（源码阅读型 + 命令探索）

1. **实践目标**：验证「CLI 层薄壳 + 接口层 serve + router_type 扩展点」的分层，并对照命令行帮助。
2. **操作步骤**：
   - 运行 `mlc_llm router --help`，对照 [cli/router.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/cli/router.py) 确认每个参数的来源。
   - 在 `interface/router.py` 里定位 `router_type` 参数，并在 `custom_router.py` 末尾的 `serve(...)` 调用里找到 `router_type=CustomRouter`，确认扩展点是同一处。
3. **需要观察的现象**：`--help` 输出的参数顺序与 `cli/router.py` 里 `add_argument` 顺序一致；`router_type` 在 CLI 层并不出现（CLI 永远用默认 `Router`），只有 Python 脚本才能注入自定义类。
4. **预期结果**：能说清「命令行走默认 Router；要走自定义 Router 必须写 Python 脚本直接调 `interface.serve`」。
5. `mlc_llm router --help` 的实际输出：**待本地验证**（需要安装好的 `mlc_llm`；参数定义本身以源码为准）。

#### 4.2.5 小练习与答案

**练习 1**：如果我想用命令行 `mlc_llm router` 跑自定义 `CustomRouter`，能做到吗？为什么？

> **参考答案**：不能直接做到。`cli/router.py` 的 `serve(...)` 调用没有传 `router_type`，恒为默认 `Router`。`router_type` 是 `interface.serve` 的关键字参数，**只对直接调 Python API 的人开放**。要用自定义 Router，必须像 `custom_router.py` 那样写脚本 `from mlc_llm.interface.router import serve; serve(..., router_type=CustomRouter)`。这是有意为之：自定义路由本质是写 Python 编排逻辑，不适合用命令行参数表达。

**练习 2**：`--endpoint-hosts` 用逗号分隔、`--endpoint-ports` 用空格分隔，这两种不同分隔方式分别对应 argparse 的什么机制？

> **参考答案**：`--endpoint-hosts` 用自定义 `type=list_of_strings`（逗号 `split`），是「单参数内部切分」；`--endpoint-ports` 用 `nargs="*"`，是「多个位置参数收集成列表」。前者把 `a,b,c` 切成 3 项，后者把 `9124 9125` 收成 `[9124, 9125]`。

---

### 4.3 Router 类：端点生命周期与两种内置调度

#### 4.3.1 概念说明

`Router` 类（`router/router.py`）是微服务的核心，承担三件事：

1. **生命周期管理**：构造时拉起 N 个引擎子进程，析构/`terminate` 时关停它们。
2. **请求分发**：`handle_completion` 是对外入口，`translate_request` 是策略分派器。
3. **两种内置调度**：
   - `round-robin`：把每个请求**整条**送到「最空闲」的那个副本，副本之间互不相干——纯负载均衡。
   - `disagg`：把请求拆成 prefill（送端点 0）+ decode（送端点 1..N）两段协作——分离式推理。

还有两个贯穿两种模式的关键设计：

- **nvshmem 世界**：分离式要在引擎实例间传 KV，底层用 GPU 间高速通信库 nvshmem。所有 GPU 必须加入同一个 nvshmem world，所以子进程**必须并发启动**（任何一个独占等下去都会因 world 不完整而卡死）。
- **抢占回退（preempt）**：decode 引擎可能因显存不足把请求抢占（u9-3 讲过的机制），此时它返回 `finish_reason == "preempt"`。路由器检测到后 `yield None`，由外层 `handle_completion` 的 `while not completed` 循环**重跑整个 `translate_request`**——这正是「preempt 后丢草稿与 KV、保留已生成内容、重新调度」在路由层的体现。

#### 4.3.2 核心流程

**构造期（拉起子进程）**：

```
1. hosts/ports/num_gpus → server_urls
2. device_id_starts 累加计算（每个端点的 GPU 起始号）
3. tvm.get_global_func("runtime.disco.nvshmem.init_nvshmem_uid")() → uid
4. 多线程并发：每个线程 start_server(i)
     - 算该端点的 nvshmem 配置（uid, npes=总GPU数, pe_start=该端点起始号）
     - PopenServer(...).start(extra_env={"MLC_NVSHMEM_INIT_CONFIG_JSON_STR": ...})
5. join 所有线程；加载 tokenizer
```

**请求期（disagg 三段）**：见 4.1.2 的伪代码。其中 `kv_window_end` 的计算是分离式调度的数学核心：

当 `pd_balance_factor` 接近 0 时，`kv_window_end = -1`（哨兵），表示「让 prefill 引擎算除最后一个 token 外的所有 token」；否则

\[
\text{kv\_window\_end} = \lfloor (1 - \text{pd\_balance\_factor}) \times \text{len(prompt)} \rfloor
\]

即「把末尾 `pd_balance_factor` 比例的 token 留给 decode 引擎自己 prefill」。例如 `pd_balance_factor=0.1` 表示最后 10% 的 prompt 由 decode 引擎 prefill，减轻 prefill 引擎负担、平衡两端（PD balance 由此得名）。

#### 4.3.3 源码精读

**nvshmem uid 与并发启动**——这是分离式跨引擎传 KV 的前提：

[python/mlc_llm/router/router.py:58-59](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/router/router.py#L58-L59) — 调 `tvm.get_global_func("runtime.disco.nvshmem.init_nvshmem_uid")` 拿到一个全局唯一的 nvshmem uid。这个 uid 会分发给所有子进程，让它们加入同一个通信世界。

[python/mlc_llm/router/router.py:70-91](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/router/router.py#L70-L91) — `start_server(i)`：为第 i 个端点算 nvshmem 配置（`npes`=总 GPU 数、`pe_start`=该端点起始 GPU 号），构造 `PopenServer`，并用环境变量 `MLC_NVSHMEM_INIT_CONFIG_JSON_STR` 把配置传给子进程。注意 `PopenServer` 的 `device=f"cuda:{device_id_starts[i]}"` 指定该端点用哪张卡，`engine_config` 里 `prefix_cache_mode` 受 `--enable-prefix-cache` 控制。

[python/mlc_llm/router/router.py:93-104](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/router/router.py#L93-L104) — 用多线程**并发**启动所有子进程并 `join`。注释 61–62 行点明原因：nvshmem world 的初始化需要所有 GPU 同时在场，串行启动某个端点会因 world 不完整而永久阻塞。

**请求入口与策略分派**：

[python/mlc_llm/router/router.py:112-132](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/router/router.py#L112-L132) — `handle_completion`：若 prompt 是字符串则用 tokenizer 编码；补一个空 `DebugConfig`；关键是 `while not completed` 循环——只要 `translate_request` `yield None`（表示被抢占），就把 `completed` 置回 `False`，**整段重跑**。这是抢占重试的源头。

[python/mlc_llm/router/router.py:134-149](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/router/router.py#L134-L149) — `translate_request`：纯策略分派，按 `self.router_mode` 转给 `_handle_completion_disagg` 或 `_handle_completion_round_robin`。**这就是自定义路由要覆写的方法**。

**round-robin 调度**：

[python/mlc_llm/router/router.py:151-160](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/router/router.py#L151-L160) — `_pick_endpoint`：在候选端点里挑 `num_running_requests` 最小的那个（实际是最少连接策略，名字叫 round-robin 但实现是 least-loaded）。

[python/mlc_llm/router/router.py:162-213](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/router/router.py#L162-L213) — `_handle_completion_round_robin`：选最空闲端点 → 计数 +1 → 直接 `POST {server_url}/v1/completions`（注意这里是普通 OpenAI 端点，不是 microserving 端点）→ 把 SSE 流逐块解析回 `CompletionResponse` → 计数 -1。同样在 `finish_reason == "preempt"` 时 `yield None` 触发外层重跑。

**disagg 调度（分离式三段）**：

[python/mlc_llm/router/router.py:218-244](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/router/router.py#L218-L244) — `_handle_completion_disagg` 开头：约定**端点 0 永远是 prefill（P），其余是 decode（D）**；从 D 里挑最空闲的；用上面的公式算 `kv_window_end`。注释 226–230 把三段流程讲得很清楚。

[python/mlc_llm/router/router.py:248-307](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/router/router.py#L248-L307) — 三段本体：① `send_prepare_receive` 问 D 准备接收（返回 KV 地址 + 前缀命中长度）；② 若有未命中的前缀段，`send_remote_send` 让 P prefill 并把 KV 传给 D；③ `send_start_generate` 让 D 从 `begin=kv_window_end` 开始 prefill 末尾 token 并 decode，流式回吐。注意 263–267 行对 `kv_window_end < 0` 的修正：`-1` 会被还原成 `len(prompt)-1`（最后一个 token 的下标），供 start_generate 用。

**三个 send_* HTTP 包装方法**：

[python/mlc_llm/router/router.py:309-336](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/router/router.py#L309-L336) — `send_prepare_receive`：`POST /microserving/prep_recv`，返回 `(kv_append_metadata_base64, prefix_matched_length)`。

[python/mlc_llm/router/router.py:338-355](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/router/router.py#L338-L355) — `send_remote_send`：`POST /microserving/remote_send`，P 返回空 ack 表示 KV 已传完。

[python/mlc_llm/router/router.py:357-390](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/router/router.py#L357-L390) — `send_start_generate`：`POST /microserving/start_generate`，把 SSE 流解析回 `CompletionResponse` 并 yield。

#### 4.3.4 代码实践（参数实验型）

1. **实践目标**：理解 `pd_balance_factor` 如何切分 prefill/decode 的工作量。
2. **操作步骤**：阅读 [router.py:240-267](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/router/router.py#L240-L267)，对一条 `len(prompt)=1000` 的请求，手算以下三种情况下 `kv_window_end`（prep_recv 阶段、remote_send 的 end、start_generate 的 begin）：
   - `pd_balance_factor=0.0`
   - `pd_balance_factor=0.1`
   - `pd_balance_factor=0.5`
3. **需要观察的现象**：因子越大，留给 decode 引擎自己 prefill 的末尾 token 越多；为 0 时 prefill 引擎几乎算完整个 prompt（只留最后一个 token 给 decode）。
4. **预期结果**：
   - `0.0`：kv_window_end 初值 -1 → prep_recv end=-1（D 准备接收 prompt[0:-1]）；修正后 = 999；remote_send end=999；start_generate begin=999（D 只 prefill 最后一个 token）。
   - `0.1`：kv_window_end = int(0.9×1000) = 900 → prep_recv end=900；不 <0 不修正；remote_send end=900；start_generate begin=900（D 自己 prefill prompt[900:1000] 共 100 个 token）。
   - `0.5`：kv_window_end = 500；remote_send end=500；start_generate begin=500（D 自己 prefill 后 500 个 token）。
5. 运行结果：**待本地验证**（本实践为源码阅读 + 手算，无需真实部署）。

#### 4.3.5 小练习与答案

**练习 1**：`_pick_endpoint` 名字暗示 round-robin（轮询），但实现是「挑 `num_running_requests` 最小的」。这两种策略有何区别？为什么这里选后者？

> **参考答案**：纯 round-robin 按固定顺序轮流发请求，不管各端点当前忙闲；least-loaded（最少连接）则动态看谁手上活最少。分离式/多副本场景下各请求耗时差异大（prompt 长短、生成长度不同），固定轮询会导致某些端点堆积、另一些空闲，所以用 least-loaded 更均衡。

**练习 2**：为什么子进程必须**并发**启动，不能 `for i in range(N): start_server(i)` 串行？

> **参考答案**：所有端点的 GPU 要加入同一个 nvshmem world 才能跨引擎传 KV。nvshmem world 的初始化是**集合操作**——所有参与者必须同时在场。如果串行启动，先启动的端点会在初始化时等待后启动的端点，而后者还没被拉起，于是死锁。并发启动让所有端点同时进入初始化，world 才能成型。代码注释（61–62 行）明确写了这一点。

**练习 3**：`handle_completion` 里 `yield None` 之后会发生什么？

> **参考答案**：`while not completed` 循环里，`completed` 被重置为 `False`，于是 `translate_request` 被整体重跑——重新选端点、重新 prep_recv/remote_send/start_generate。这对应 decode 引擎把请求抢占后，路由层把整条分离式流程重来的语义（与 u9-3 抢占后「丢草稿与 KV、保留 committed token、插回队列」一脉相承，只不过这里是在路由层重新发起）。

---

### 4.4 自定义路由扩展：translate_request 与微服务三段 API

#### 4.4.1 概念说明

微服务最强大的地方在于**可编程**：内置的 `round-robin`/`disagg` 只是两个例子，真正的扩展点是 `translate_request` 这个方法。它接收一个 OpenAI `CompletionRequest` + `request_id`，返回一个 `AsyncGenerator[CompletionResponse]`——你想怎么编排跨引擎协作，就在这里写。

[docs/microserving/tutorial.rst](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/docs/microserving/tutorial.rst) 把这套理念称为 **RISC 风格的子请求级 API**：与其给一个臃肿的「大请求」接口，不如给三个简单、正交的「原语」，让用户像写汇编一样组合出任意跨引擎模式。三个原语对应三个 `/microserving/*` 端点：

| 原语 | 端点 | 干什么 |
|------|------|--------|
| **prep_recv** | `POST /microserving/prep_recv` | 让 **decode 引擎**准备接收 KV：匹配前缀缓存、分配 KV 槽位，返回「KV 接收地址元数据 + 命中的前缀长度」。 |
| **remote_send** | `POST /microserving/remote_send` | 让 **prefill 引擎**算指定 KV 窗口，并把算好的 KV 跨机发给 decode 引擎；返回空 ack。 |
| **start_generate** | `POST /microserving/start_generate` | 让 **decode 引擎**从指定位置 prefill 末尾 token 并开始 decode，流式返回生成结果。 |

这三个原语背后都复用了普通的 `/v1/completions` 实现——它们只是往请求里塞一个 `debug_config.disagg_config`（`DisaggConfig`），告诉引擎这次只做 prefill 的某一段、或只做 KV 收发、不做采样。也就是说，**微服务端点是「带特殊调试指令的 completion 请求」**（DisaggConfig 属于 u6-3 讲过的 `DebugConfig` 后门字段）。

#### 4.4.2 核心流程

`custom_router.py` 的分离式流程（与内置 `_handle_completion_disagg` 等价但更易读）：

```
translate_request(request, request_id):
    request.user = request_id              # 让微服务 API 能用 request_id 关联
    decode_start = len(request.prompt) - 1 # 末 token 下标
    ① PrepRecvRequest(**request, end=decode_start)
       → send_prepare_receive(server=decode引擎)   # D 准备接收 prompt[0:decode_start]
       → 得 kv_addr_info
    ② RemoteSendRequest(**request, begin=0, end=decode_start,
                        kv_addr_info=..., recv_rank=decode引擎的rank)
       → send_remote_send(server=prefill引擎)       # P 算并送 KV
    ③ StartGenerateRequest(**request, begin=decode_start)
       → send_start_generate(server=decode引擎)     # D prefill 末 token 并 decode
       → 流式 yield response
       （若 finish_reason=="preempt" → yield None 触发外层重跑）
```

三个请求体都是 `CompletionRequest` 的子类，只多加一个区间字段（`end`/`begin`+`end`/`begin`），体现「子请求 = completion 请求 + 一个 KV 窗口」的正交设计。

#### 4.4.3 源码精读

**协议模型**——三个子请求体：

[python/mlc_llm/protocol/microserving_protocol.py:8-19](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/protocol/microserving_protocol.py#L8-L19) — `PrepRecvRequest(CompletionRequest)` 仅加 `end: int`，文档说明 `[0, end]` 是要在 prefill 实例算、并在 decode 实例分配槽位的 KV 区间。

[python/mlc_llm/protocol/microserving_protocol.py:22-36](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/protocol/microserving_protocol.py#L22-L36) — `PrepRecvResponse` 返回 `kv_append_metadata`（目标 decode 实例上的 KV 元数据，base64 字符串）与 `prefix_matched_length`（启用前缀缓存时命中的公共前缀长度，否则 0）。

[python/mlc_llm/protocol/microserving_protocol.py:39-60](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/protocol/microserving_protocol.py#L39-L60) — `RemoteSendRequest` 加 `begin`/`end`（要 prefill 的 KV 区间）、`kv_addr_info`（来自 prep_recv 的元数据）、`recv_rank`（目标 decode 实例的 group 偏移）。

[python/mlc_llm/protocol/microserving_protocol.py:63-72](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/protocol/microserving_protocol.py#L63-L72) — `StartGenerateRequest` 只加 `begin`（decode 实例从哪开始 prefill）。

**微服务端点——把子请求翻译成带 DisaggConfig 的 completion**：

[python/mlc_llm/serve/entrypoints/microserving_entrypoints.py:22-45](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/entrypoints/microserving_entrypoints.py#L22-L45) — `prep_recv`：往 `request.debug_config.disagg_config` 塞 `DisaggConfig(kind="prepare_receive", kv_window_begin=0, kv_window_end=request.end)`，强制 `stream=False`，然后**复用** `openai_entrypoints.request_completion`；最终从 `response.usage.extra` 里抠出 `prefix_matched_length` 与 `kv_append_metadata` 返回。注意 KV 接收地址是借 OpenAI 协议的 `usage.extra`（u6-3 / u1-l3 讲过的 `extra` 字段）回传的——巧妙复用而非另造协议。

[python/mlc_llm/serve/entrypoints/microserving_entrypoints.py:48-63](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/entrypoints/microserving_entrypoints.py#L48-L63) — `remote_send`：塞 `DisaggConfig(kind="remote_send", kv_window_begin/end, kv_append_metadata, dst_group_offset=recv_rank)`，复用 `request_completion` 让 prefill 引擎算这段 KV 并发出去，返回空 `{}`。

[python/mlc_llm/serve/entrypoints/microserving_entrypoints.py:66-73](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/entrypoints/microserving_entrypoints.py#L66-L73) — `start_generate`：塞 `DisaggConfig(kind="start_generation", kv_window_begin=request.begin)`，直接返回 `request_completion` 的结果（流式或非流式均可）。

> 三个端点的 `kind` 取值 `prepare_receive`/`remote_send`/`start_generation` 正是 u12-l3 将要讲的 C++ 端 `disagg_prepare_recv`/`disagg_remote_send` 动作的触发开关——本讲停在 Python 协议层，C++ 侧的 KV 收发实现在 u12-l3。

**自定义路由示例**：

[examples/python/microserving/custom_router.py:14-19](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/examples/python/microserving/custom_router.py#L14-L19) — `class CustomRouter(Router)`，只覆写 `translate_request`。这就是全部扩展点：继承 + 一个方法。

[examples/python/microserving/custom_router.py:26-40](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/examples/python/microserving/custom_router.py#L26-L40) — 第 ① 段：`decode_start = len(prompt)-1`，构造 `PrepRecvRequest(**request, end=decode_start)`，`send_prepare_receive` 发给 `server_urls[1]`（decode 引擎），拿回 `kv_addr_info`。

[examples/python/microserving/custom_router.py:42-53](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/examples/python/microserving/custom_router.py#L42-L53) — 第 ② 段：构造 `RemoteSendRequest(begin=0, end=decode_start, kv_addr_info=..., recv_rank=self.device_id_starts[1])`，`send_remote_send` 发给 `server_urls[0]`（prefill 引擎）。`recv_rank` 用 `device_id_starts[1]`——即 decode 引擎在全局 GPU 世界里的起始 rank，这样 prefill 引擎才知道把 KV 发给谁。

[examples/python/microserving/custom_router.py:55-68](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/examples/python/microserving/custom_router.py#L55-L68) — 第 ③ 段：`StartGenerateRequest(begin=decode_start)` 发给 decode 引擎，流式 yield；若 `finish_reason=="preempt"` 先 `yield None`（触发 4.3 讲的外层重跑），再 `yield response`。

[examples/python/microserving/custom_router.py:71-81](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/examples/python/microserving/custom_router.py#L71-L81) — 启动：`serve(..., router_type=CustomRouter)`。这里两个端点各占 2 GPU（`endpoint_num_gpus=[2,2]`），分别监听 9124/9125，路由监听 9123。`model`/`model_lib` 是占位路径，需替换成真实编译产物。

#### 4.4.4 代码实践（源码阅读型——本讲的核心实践）

1. **实践目标**：读懂 `custom_router.py` 如何把一个 OpenAI 请求分派到 prefill 与 decode 两个后端模型，并说清微服务模式与单一 `serve` 的区别。
2. **操作步骤**：
   - 打开 [custom_router.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/examples/python/microserving/custom_router.py) 与 [docs/microserving/tutorial.rst](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/docs/microserving/tutorial.rst) 对照阅读。
   - 用三种颜色高亮三段：① prep_recv（发 decode 引擎）、② remote_send（发 prefill 引擎）、③ start_generate（发 decode 引擎）。
   - 在每个 `send_*` 调用旁标注它打到的 `server_url` 与 HTTP 路径（`/microserving/prep_recv` 等）。
3. **需要观察的现象**：请求并不是「发到某一个模型」，而是被**拆成三段**、在 prefill 引擎（`server_urls[0]`）与 decode 引擎（`server_urls[1]`）之间接力：decode 先占好 KV 槽位 → prefill 算好 KV 跨机送过去 → decode 接着生成。
4. **预期结果**：能写出如下分派说明——

   > `custom_router` 把一次 completion 请求翻译成跨 prefill/decode 两引擎的三步接力：① 给 decode 引擎（`server_urls[1]`）发 `prep_recv`，让它为 `prompt[0:len-1]` 分配 KV 接收槽位并返回地址元数据；② 把该元数据连同区间 `[0, len-1]` 发给 prefill 引擎（`server_urls[0]`）的 `remote_send`，由它 prefill 这段 prompt 并经 nvshmem 把 KV 跨机送到 decode 引擎；③ 给 decode 引擎发 `start_generate`，让它 prefill 最后一个 token 并开始自回归 decode，流式回吐结果。若 decode 引擎返回 `preempt`，则 `yield None`，由 `Router.handle_completion` 重跑整段流程。

   并指出与单一 `serve` 的区别：单一 `serve` 是**单进程内** prefill+decode 一体调度（连续批处理），客户端直接打到引擎；微服务是**多进程**，prefill 与 decode 分属不同引擎实例，靠路由器在二者间搬运 KV，可分别扩容、提升吞吐。

5. 运行结果：**待本地验证**（实跑需要 4 张 GPU、已编译模型与 nvshmem 环境；本实践以源码阅读 + 撰写分派说明为产出）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `custom_router` 里 `recv_rank` 要用 `self.device_id_starts[1]` 而不是直接写 `1`？

> **参考答案**：`recv_rank` 是 decode 引擎在**全局 nvshmem/disco 世界**里的 group 偏移，不是它的端点下标。若 prefill 引擎占 GPU 0–1（`device_id_starts=[0,...]`），decode 引擎占 GPU 2–3，则 decode 的起始 rank 是 2（=`device_id_starts[1]`），而非 1。直接写 `1` 会把 KV 发到错误的 worker。`device_id_starts` 把「端点下标」翻译成「全局 rank」。

**练习 2**：三个微服务端点 `prep_recv`/`remote_send`/`start_generate` 内部都没有自己写推理逻辑，它们是怎么完成 KV 收发的？

> **参考答案**：它们都复用 `openai_entrypoints.request_completion`，只是往请求的 `debug_config.disagg_config` 塞一个 `kind` 不同的 `DisaggConfig`（`prepare_receive`/`remote_send`/`start_generation`）加上 KV 窗口区间。引擎（C++ ThreadedEngine）看到这个 debug 指令就走对应的分离式动作分支（u12-3 的 `disagg_prepare_recv`/`disagg_remote_send`）。KV 地址元数据则借 OpenAI 响应的 `usage.extra` 字段回传，避免另造协议。

**练习 3**：如果我想在 `custom_router` 基础上加一个「第三个引擎做推测解码起草」的玩法，应该改哪里？

> **参考答案**：在 `translate_request` 里，于 ③ `start_generate` 之前或之中，新增对第三个引擎（`server_urls[2]`）的调用（用 microserving 原语让它先起草 draft token），再把 draft 信息随 `start_generate` 送给 decode 引擎做校验。这正是微服务「可编程」的价值：不动框架，只在 `translate_request` 里写新的编排。`Router` 已把 `server_urls`/`device_id_starts`/`send_*` 包装好供子类直接用。

---

## 5. 综合实践

把本讲四块知识（微服务部署形态、CLI/接口分层、Router 调度、自定义扩展）串起来，完成下面这个**源码阅读 + 设计型**任务（实跑仍待本地多卡环境验证）：

**任务：绘制一次 disagg 请求的完整时序图，并设计一个「带日志的自定义 Router」。**

1. **画时序图**：以一条 `len(prompt)=1000`、`pd_balance_factor=0.1`、前缀命中 100 token 的请求为例，画出从客户端到 prefill 引擎、decode 引擎的时序，至少包含：
   - 客户端 → 路由器：`POST /v1/completions`（stream=true）
   - 路由器 → decode 引擎：`POST /microserving/prep_recv`，body 含 `end=900`；返回 `prefix_matched_length=100`、`kv_append_metadata=...`
   - 路由器 → prefill 引擎：`POST /microserving/remote_send`，body 含 `begin=100, end=900, recv_rank=2`；返回 `{}`
   - 路由器 → decode 引擎：`POST /microserving/start_generate`，body 含 `begin=900`；流式回吐
   - 在每个箭头旁标注对应的 `DisaggConfig.kind`（`prepare_receive`/`remote_send`/`start_generation`）。
2. **写最小改动**：复制 `custom_router.py` 为 `custom_router_logged.py`，在 ①②③ 三段的 `send_*` 调用前后各加一行 `print`（如 `print(f"[router] step1 prep_recv end={decode_start}")`），并说明：当 decode 引擎返回 `preempt` 时，你会看到三段日志**重复打印**——这就是 `handle_completion` 的 `while not completed` 重跑在起作用。
3. **回答**：这个带日志的自定义 Router，用命令行 `mlc_llm router` 能跑吗？为什么？（答：不能，CLI 不传 `router_type`；必须 `python custom_router_logged.py` 直接调 `interface.serve(..., router_type=CustomRouterLogged)`。）

**交付物**：一张时序图（手绘或文本均可）+ 一份带日志的 `translate_request` 片段 + 对「日志重复」现象的解释。运行结果：**待本地验证**。

## 6. 本讲小结

- **微服务 = 路由器 + N 个引擎子进程**：路由器是编排层而非引擎，用 `PopenServer` 拉起 N 个完整 `mlc_llm serve` 子进程，对外只暴露 `/v1/completions`。
- **两层分明的代码结构**：`cli/router.py`（薄壳 argv 解析）→ `interface/router.py`（`serve()` 起 FastAPI + uvicorn）→ `router/router.py`（`Router` 类实现）。与 compile/serve 命令同一套 cli/interface 范式。
- **扩展点是 `router_type` 与 `translate_request`**：`interface.serve(router_type=...)` 允许注入 `Router` 子类，覆写 `translate_request` 即可自定义跨引擎编排；CLI 不暴露此参数，故自定义路由只能写 Python 脚本。
- **两种内置调度**：`round-robin`（最少连接负载均衡，整请求转发到 `/v1/completions`）与 `disagg`（prefill-decode 分离式，端点 0 恒为 prefill、其余为 decode）。
- **分离式三段原语**：`prep_recv`（decode 占 KV 槽）→ `remote_send`（prefill 算 KV 跨机送）→ `start_generate`（decode prefill 末 token 并生成），三者复用 completion 请求 + `DisaggConfig`，KV 地址借 `usage.extra` 回传。
- **两个工程细节**：子进程必须**并发启动**（nvshmem world 需所有 GPU 同时在场）；decode 返回 `preempt` 时 `yield None`，触发 `handle_completion` 的 `while` 循环**整体重跑** `translate_request`。

## 7. 下一步学习建议

- **u12-l3（分离式推理 Disaggregation）**：本讲停在 Python 协议与路由层；`DisaggConfig.kind` 如何触发 C++ 引擎里真正的 KV 跨机收发（`disagg_remote_send.cc`/`disagg_prepare_recv.cc`）、nvshmem 与 disco 远程会话如何协作，是自然的下一站。
- **u12-l1（多 GPU / 张量并行 / disco）**：本讲的 `device_id_starts`、nvshmem uid、`recv_rank` 都建立在 disco 分布式会话之上；想彻底搞懂「KV 怎么跨 GPU 传」需回看 disco 与 `multi_gpu_loader`。
- **继续阅读**：`python/mlc_llm/serve/entrypoints/openai_entrypoints.py` 的 `request_completion`（看微服务端点如何复用它）、`python/mlc_llm/protocol/debug_protocol.py` 的 `DisaggConfig` 定义，以及官方博客 https://blog.mlc.ai/2025/01/07/microserving-llm-engines 了解 MicroServing 的设计动机。
