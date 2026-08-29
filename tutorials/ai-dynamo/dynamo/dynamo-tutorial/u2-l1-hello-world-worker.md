# Hello World：用 dynamo.runtime 写最小 worker

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `dynamo.runtime` 模块里三个核心对象 `DistributedRuntime`、`Endpoint`、`Client` 各自的职责，以及 `namespace.component.endpoint` 三段式路径的含义。
2. 独立读懂并用 `@dynamo_endpoint` + `@dynamo_worker` 两个装饰器写出一个最小的流式服务端（worker）。
3. 用 `endpoint.client()` → `wait_for_instances()` → `client.generate()` 写出消费流式响应的客户端。
4. 分清 `dynamo.runtime`（分布式运行时原语）与 `dynamo.llm`（LLM 领域高层 API）两个 Python 模块的分工，知道什么时候用哪个。
5. 把一个本地跑通的 worker 映射到 Kubernetes 上的 `DynamoGraphDeployment` YAML。

本讲是所有后续源码阅读的地基：无论后面讲 KV 路由、P/D 分离还是 KVBM，"注册 endpoint、被 client 发现、流式收发"这套骨架都来自本讲。

## 2. 前置知识

本讲只需要 Python 基础，不需要 GPU、不需要懂 Rust。但以下几个概念请先过一遍：

- **异步生成器（async generator）**：一个 `async def` 函数里出现 `yield`，它就不再是普通协程，而是异步生成器——调用它不会立刻执行函数体，而是返回一个可以 `async for` 迭代的对象。Dynamo 的流式响应就是靠它实现的：每 `yield` 一次，客户端就收到一帧。
- **装饰器（decorator）**：`@dynamo_worker()` 这种带括号的装饰器是"装饰器工厂"——先调用 `dynamo_worker()` 返回真正的装饰器，再用它包装你的函数。Dynamo 用这两个装饰器把"创建运行时、解析请求"等样板代码从你面前藏起来。
- **Pydantic BaseModel**：一个用类声明字段、自动做数据校验的库。Dynamo 的 `@dynamo_endpoint` 可选地把请求负载解析成 Pydantic 模型；本例传 `str` 跳过解析。
- **uvloop**：asyncio 事件循环的高性能替代品，`uvloop.install()` 一行替换，行为不变、速度更快。Dynamo 的示例入口都装了它。
- **三段式 endpoint 路径**：Dynamo 里每个可调用的服务地址长成 `hello_world.backend.generate`——`namespace（命名空间）.component（组件）.endpoint（端点）`。可以类比为 DNS：namespace 是域名，component 是主机，endpoint 是端口上的一个具体服务。
- **承接上一讲（u1-l2）**：本地开发不需要 etcd/NATS——`DYN_DISCOVERY_BACKEND=file` 用写文件的方式做服务发现，`DYN_EVENT_PLANE=zmq` 用 ZMQ 替代 NATS 做事件面传输。本讲的运行命令会再次用到这两个环境变量。

一个容易混淆的点先说清楚：在 Dynamo 的语境里，**client 进程本身也是一个 "worker"**——`client.py` 用的同样是 `@dynamo_worker()` 装饰器。区别只在于：服务端 worker 用 `runtime` 去**注册** endpoint，客户端 worker 用 `runtime` 去**查找并调用** endpoint。"worker" 指的是"接入分布式运行时的进程"，而不是"干活的服务端"。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲视角 |
|------|------|----------|
| [examples/custom_backend/hello_world/hello_world.py](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/examples/custom_backend/hello_world/hello_world.py) | 服务端：注册 `generate` endpoint 并流式返回问候语 | 主角，38 行的完整 worker |
| [examples/custom_backend/hello_world/client.py](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/examples/custom_backend/hello_world/client.py) | 客户端：连接 endpoint、循环发请求、指数退避重试 | 主角，Client API 的用法大全 |
| [examples/custom_backend/hello_world/README.md](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/examples/custom_backend/hello_world/README.md) | 示例说明与运行命令 | 运行依据 |
| [lib/bindings/python/src/dynamo/runtime/\_\_init\_\_.py](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/bindings/python/src/dynamo/runtime/__init__.py) | `dynamo.runtime` 包：再导出 Rust 类 + 两个装饰器 | 主角，Python 侧唯一逻辑文件 |
| [lib/bindings/python/src/dynamo/_core.pyi](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/bindings/python/src/dynamo/_core.pyi) | PyO3 扩展 `dynamo._core` 的类型存根（API 说明书） | 查 API 签名用 |
| [lib/bindings/python/rust/lib.rs](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/bindings/python/rust/lib.rs) | PyO3 扩展的 Rust 实现 | 只看三处：构造函数、响应流、Annotated |
| [examples/custom_backend/hello_world/deploy/hello_world.yaml](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/examples/custom_backend/hello_world/deploy/hello_world.yaml) | 把示例部署成 K8s `DynamoGraphDeployment` | 从本地走向集群 |
| [lib/bindings/python/src/dynamo/llm/\_\_init\_\_.py](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/bindings/python/src/dynamo/llm/__init__.py) | `dynamo.llm` 包的再导出 | 对照：高层模块长什么样 |

## 4. 核心概念与源码讲解

### 4.1 dynamo.runtime：一层薄封装与三个核心对象

#### 4.1.1 概念说明

回顾 u1-l4 讲过的构建链：Rust 核心通过 PyO3 编译成 Python 扩展模块 `dynamo._core`，而 `dynamo.runtime` 只是包在这个扩展外面的一层**薄薄的 Python 包装**。它做两件事：

1. **再导出** Rust 类：`DistributedRuntime`、`Endpoint`、`Client`、`Context`、`PyAsyncRequestStream`——你在 Python 里 `import` 到的这些类，实际都是 Rust 结构体。
2. **提供两个纯 Python 装饰器**：`@dynamo_worker()` 和 `@dynamo_endpoint`，把最常见的样板代码封装起来。

三个核心对象的职责：

| 对象 | 职责 | 类比 |
|------|------|------|
| `DistributedRuntime` | 进程级运行时：持有事件循环、服务发现后端、传输配置；是创建一切的根 | 数据库连接池 |
| `Endpoint` | 一个可服务的端点地址；既能"注册处理函数"（服务端视角），又能"生成客户端"（客户端视角） | 一个具名队列的声明 |
| `Client` | 连到某 endpoint 的全部实例，负责路由选择与请求收发 | 操作队列的句柄 |

#### 4.1.2 核心流程

从 `import` 到可用对象的链路：

```text
import dynamo.runtime
   │
   ├─ from dynamo._core import DistributedRuntime, Endpoint, Client, Context, ...   (Rust 编译产物)
   │
   └─ 定义 dynamo_worker() / dynamo_endpoint() 两个装饰器                        (纯 Python)
   │
调用 @dynamo_worker() 包装的函数时：
   │
   ├─ 1. asyncio.get_running_loop() 取当前事件循环
   ├─ 2. 读环境变量 DYN_REQUEST_PLANE（默认 "tcp"）
   ├─ 3. 读环境变量 DYN_DISCOVERY_BACKEND（默认 "etcd"）
   ├─ 4. DistributedRuntime(loop, discovery_backend, request_plane)  ← 进入 Rust
   └─ 5. await func(runtime, ...)  把运行时注入你的函数体
```

注意第 2、3 步：**Python 侧只显式读这两个环境变量**。README 里要求的 `DYN_EVENT_PLANE=zmq` 不是在这里读的，而是由 Rust 侧的运行时配置模块读取（定义在 `lib/runtime/src/config/environment_names.rs`），这解释了为什么两个进程必须设置相同的环境变量才能互相发现。

#### 4.1.3 源码精读

先看再导出清单——`dynamo.runtime` 的全部"家当"就是这五个 Rust 类加两个装饰器：

[lib/bindings/python/src/dynamo/runtime/\_\_init\_\_.py:14-18](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/bindings/python/src/dynamo/runtime/__init__.py#L14-L18)

这段代码从 `dynamo._core`（PyO3 扩展）逐个导入 `Client`、`Context`、`DistributedRuntime`、`Endpoint`、`PyAsyncRequestStream` 并以 `X as X` 形式再导出——注释说明这样做是为了避免 `import *` 导致的"无法检测未定义名称"问题。

再看 `dynamo_worker` 装饰器的核心实现：

[lib/bindings/python/src/dynamo/runtime/\_\_init\_\_.py:41-49](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/bindings/python/src/dynamo/runtime/__init__.py#L41-L49)

包装函数做了三件事：取事件循环、从环境变量解析 `request_plane` 与 `discovery_backend` 两个配置（[L43-L44](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/bindings/python/src/dynamo/runtime/__init__.py#L43-L44) 给出默认值 `tcp`/`etcd`）、构造 `DistributedRuntime` 后把它作为第一个参数注入你的函数。所以被装饰的函数签名必须是 `async def worker(runtime: DistributedRuntime, ...)`——`runtime` 是装饰器塞进来的，不是你创建的。

跨过 PyO3 边界看 Rust 构造函数（不求看懂全部，只看分支结构）：

[lib/bindings/python/rust/lib.rs:1179-1189](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/bindings/python/rust/lib.rs#L1179-L1189)

`DistributedRuntime::new` 把 Python 传来的字符串翻译成 Rust 枚举：`discovery_backend` 为 `"kubernetes"` 时走 K8s 原生发现，否则当作 `kv::Selector` 解析（也就是 `etcd`/`file`/`mem` 这几种 KV 存储后端，与 u1-l2 结论一致）；`request_plane` 解析成 `RequestPlaneMode`；事件面传输类型由 `resolve_event_transport_kind` 结合显式参数与环境变量决定。随后（[L1193-L1208](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/bindings/python/rust/lib.rs#L1193-L1208)）优先复用进程内已有的 tokio 运行时，没有才新建——这意味着**一个进程里创建多个 `DistributedRuntime` 也会共享同一个 tokio 运行时**。

#### 4.1.4 代码实践

**实践目标**：亲手验证"dynamo.runtime 只是 Rust 扩展的再导出层"这一论断。

**操作步骤**（需已按 u1-l4 完成安装或构建）：

```bash
python3 -c "
from dynamo.runtime import DistributedRuntime, Endpoint, Client
print(DistributedRuntime.__module__)
print(Endpoint.__module__)
print(Client.__module__)
import inspect
print(inspect.isbuiltin(DistributedRuntime.endpoint) or 'not-builtin')
"
```

**需要观察的现象**：三个类的 `__module__` 都应打印 `dynamo._core`，而不是 `dynamo.runtime`。

**预期结果**：确认这些类来自编译好的 Rust 扩展模块；`dynamo.runtime/__init__.py` 里对它们没有任何加工。

**待本地验证**：以上命令需要在装有 `ai-dynamo-runtime`（或源码构建出的 `_core` 扩展）的环境里执行，本讲义编写环境未运行它。

#### 4.1.5 小练习与答案

**练习 1**：如果不使用 `@dynamo_worker()` 装饰器，你如何手动构造等价的 `DistributedRuntime`？

**参考答案**：照抄装饰器内部即可——`loop = asyncio.get_running_loop()`，然后 `runtime = DistributedRuntime(loop, os.environ.get("DYN_DISCOVERY_BACKEND", "etcd"), os.environ.get("DYN_REQUEST_PLANE", "tcp"))`。装饰器没有魔法，只是把这三行标准化了（见 [\_\_init\_\_.py:41-49](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/bindings/python/src/dynamo/runtime/__init__.py#L41-L49)）。

**练习 2**：为什么 README 要求 backend 和 client 两个进程都设置 `DYN_DISCOVERY_BACKEND=file`？

**参考答案**：因为服务发现后端决定"服务端把 endpoint 注册到哪里、客户端从哪里查找 endpoint"。一个用 `file`（写本地文件）、一个用默认 `etcd`（连 etcd 集群）时，两边看的是不同的"电话簿"，客户端永远找不到服务端。

**练习 3**：`DistributedRuntime(loop, "file", "tcp")` 中第二个参数还可以传哪些值？

**参考答案**：`"kubernetes"`、`"etcd"`、`"file"`、`"mem"` 四种。Rust 侧逻辑（[lib.rs:1179-1185](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/bindings/python/rust/lib.rs#L1179-L1185)）是 `"kubernetes"` 特判，其余字符串按 KV selector 解析。

### 4.2 服务端骨架：@dynamo_endpoint + @dynamo_worker

#### 4.2.1 概念说明

一个最小的 Dynamo 服务端只需要两块积木：

- **处理函数**（`content_generator`）：一个异步生成器，吃一个请求、流式吐多个响应。`@dynamo_endpoint(str, str)` 声明请求/响应的类型提示。
- **worker 函数**：拿到 `runtime`，把三段式路径拼出来，调用 `endpoint.serve_endpoint(处理函数)` 完成注册。注册完成后，处理函数就成为一个可被任何客户端按名字调用的网络服务——**你不需要写任何 socket 代码**。

`@dynamo_endpoint(请求模型, 响应模型)` 的两个参数：当请求模型是 `BaseModel` 子类时，装饰器会把收到的 `str`/`dict` 负载自动解析成该模型实例再做校验；传 `str` 这种非 Pydantic 类型则完全跳过解析（本例的做法）。响应模型目前只收下不校验。

#### 4.2.2 核心流程

服务端从启动到可服务的完整流程：

```text
python hello_world.py
   │
   ├─ uvloop.install() + asyncio.run(worker())          # 入口
   ├─ @dynamo_worker() 拦截：构造 DistributedRuntime    # 见 4.1
   ├─ runtime.endpoint("hello_world.backend.generate")
   │     └─ 按三段式路径创建/获取 namespace → component → endpoint
   ├─ await endpoint.serve_endpoint(content_generator)
   │     └─ 注册到服务发现（file 模式 = 写文件 + keep-alive）
   └─ 事件循环保持运行，等待请求
收到一条请求 "world,sun,moon,star"：
   ├─ 请求负载原样传给 content_generator（str 跳过解析）
   └─ 按逗号切分，每个词 sleep(1) 后 yield "Hello {word}!"   # 4 帧，间隔约 1 秒
```

关键理解：`serve_endpoint` 之后进程就"挂"在那里 await，永不主动返回（除非取消或关闭）。请求来一次，`content_generator` 就被调用一次；**多个请求是并发调用的**，每个请求拿到自己独立的生成器实例。

#### 4.2.3 源码精读

服务端全文只有 38 行，先看处理函数：

[examples/custom_backend/hello_world/hello_world.py:16-21](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/examples/custom_backend/hello_world/hello_world.py#L16-L21)

`@dynamo_endpoint(str, str)` 声明请求和响应都是 `str`（非 Pydantic 类型，跳过解析）。函数体先用日志记录收到的请求，然后按逗号切分输入，对每个词 `sleep(1)` 再 `yield` 一句问候——`await asyncio.sleep(1)` 既模拟了"生成一个 token 的耗时"，也让你能在客户端肉眼观察到流式效果。

再看 worker 注册部分：

[examples/custom_backend/hello_world/hello_world.py:24-33](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/examples/custom_backend/hello_world/hello_world.py#L24-L33)

三个名字拼出三段式路径（L26-L28），`runtime.endpoint("hello_world.backend.generate")` 一步到位拿到 `Endpoint` 对象（Rust 侧会按需创建对应的 namespace 与 component），最后 `await endpoint.serve_endpoint(content_generator)` 把处理函数挂上去。`serve_endpoint` 的完整签名（[\_core.pyi:153-165](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/bindings/python/src/dynamo/_core.pyi#L153-L165)）还接受 `graceful_shutdown`（默认 `True`，关闭前等在途请求跑完）、`metrics_labels`（指标打标）和 `health_check_payload`（健康探活负载）三个可选参数，注册后该端点可被所有客户端在 `{{namespace}}/components/{{component}}/endpoints/{{endpoint}}` 路径下发现。

最后是进程入口：

[examples/custom_backend/hello_world/hello_world.py:36-38](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/examples/custom_backend/hello_world/hello_world.py#L36-L38)

`uvloop.install()` 必须在 `asyncio.run` 之前调用，替换默认事件循环实现。

顺带看一眼 `@dynamo_endpoint` 的解析逻辑，理解"传 `str` 为什么能跳过"：

[lib/bindings/python/src/dynamo/runtime/\_\_init\_\_.py:99-114](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/bindings/python/src/dynamo/runtime/__init__.py#L99-L114)

包装器只在 `request_model` 是 `BaseModel` 子类、且参数个数为一或二（支持 `(self, request)` 的方法形式）时才做 `parse_raw`/`parse_obj`；否则请求原样透传给被装饰函数。随后 `async for item in func(...)` 把你的生成器逐帧转发出去（响应校验目前是 TODO）。

#### 4.2.4 代码实践

**实践目标**：跑通原始 hello_world 服务端，观察注册日志与流式节奏。

**操作步骤**：

1. 终端一，按 [README.md:57-67](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/examples/custom_backend/hello_world/README.md#L57-L67) 启动服务端：

   ```bash
   cd examples/custom_backend/hello_world
   DYN_DISCOVERY_BACKEND=file DYN_EVENT_PLANE=zmq python hello_world.py
   ```

2. 终端二启动客户端（命令同上，只是把文件换成 `client.py`，详见 4.3）。
3. 在服务端日志里找 `Received request:` 与 `Serving endpoint hello_world/backend/generate` 两类输出。

**需要观察的现象**：客户端不是一次性打印四行，而是**每隔约 1 秒多出一行**——因为 `content_generator` 每个词前都 `sleep(1)`。服务端日志能看到收到的原始请求字符串。

**预期结果**：客户端最终输出 `Hello world!` / `Hello sun!` / `Hello moon!` / `Hello star!` 四行（见 [README.md:75-83](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/examples/custom_backend/hello_world/README.md#L75-L83) 的预期输出）。

**待本地验证**：具体日志格式可能随版本变化，以实际运行为准。

#### 4.2.5 小练习与答案

**练习 1**：把 `@dynamo_endpoint(str, str)` 改成 `@dynamo_endpoint(dict, str)`，`content_generator` 里 `request` 会变成什么类型？

**参考答案**：仍然是原始对象、不做解析——因为 `dict` 不是 `BaseModel` 子类，不满足 [\_\_init\_\_.py:103](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/bindings/python/src/dynamo/runtime/__init__.py#L103) 的 `issubclass(request_model, BaseModel)` 条件，负载原样透传。要触发解析必须传一个 Pydantic 模型类。

**练习 2**：如果想把端点路径改成 `my_ns.my_comp.my_ep`，需要改哪几处？

**参考答案**：只改服务端的三个名字变量（[hello_world.py:26-28](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/examples/custom_backend/hello_world/hello_world.py#L26-L28)）和客户端的路径字符串（[client.py:26](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/examples/custom_backend/hello_world/client.py#L26)）。namespace/component/endpoint 不需要预先在任何地方声明——`runtime.endpoint()` 按路径惰性创建。

**练习 3**：为什么处理函数是"每请求调用一次"而不是"全局单实例循环读请求"？

**参考答案**：`serve_endpoint` 注册的是一个异步生成器**工厂**：每条请求到达时运行时会调用一次该函数、拿到一个专属生成器，请求之间互不干扰，天然支持并发。这也是 Dynamo 用"异步生成器"而非"回调函数"建模流式响应的原因。

### 4.3 客户端：Client 连接与流式消费

#### 4.3.1 概念说明

客户端要回答三个问题：**怎么连**（`endpoint.client()`）、**什么时候能连**（`wait_for_instances()`）、**怎么收发**（`client.generate()` + `async for`）。

- `endpoint.client()` 返回一个 `Client`，它背后是一个**实例观察者**：持续监听服务发现里这个 endpoint 的所有在线实例（一个 endpoint 可以被多个 worker 进程同时服务，client 会在实例间做路由）。默认策略是轮询（round-robin）。
- `wait_for_instances()` 是关键的"等对方上线"调用：服务端可能还没启动、正在启动、或刚刚崩溃重启。这个 await 会挂起直到至少有一个实例可用，返回实例 ID 列表。
- `generate(request)` 发出请求并返回一个响应流；`async for response in stream` 逐帧消费，每帧是一个 `Annotated` 包装对象，用 `response.data()` 取出真正的负载。

#### 4.3.2 核心流程

```text
python client.py
   │
   ├─ @dynamo_worker() 构造 runtime（同服务端）
   ├─ runtime.endpoint("hello_world.backend.generate")   # 同一个路径，这次是"找"
   ├─ client = await endpoint.client()                   # 创建客户端（默认 round-robin）
   ├─ await client.wait_for_instances()                  # 阻塞直到有实例上线
   │
   └─ 死循环：
        ├─ stream = await client.generate("world,sun,moon,star")
        ├─ async for response in stream: print(response.data())   # 逐帧打印
        ├─ 成功 → 退避重置为 0.1s，sleep(1) 后进入下一轮
        └─ 失败 → 打印异常，按 current_delay 退避，且 current_delay = min(×2, 5.0s)
```

退避参数：初始 0.1 秒，每次失败翻倍，上限 5 秒（[client.py:33-35](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/examples/custom_backend/hello_world/client.py#L33-L35)）。注意 `except asyncio.CancelledError: raise`（[L48-L50](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/examples/custom_backend/hello_world/client.py#L48-L50)）——取消信号必须重新抛出，否则 Ctrl-C 后进程无法优雅退出，这是写任何 Dynamo worker 都要遵守的纪律（u2-l3 取消传播一讲会展开）。

#### 4.3.3 源码精读

连接三步曲：

[examples/custom_backend/hello_world/client.py:23-30](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/examples/custom_backend/hello_world/client.py#L23-L30)

与 hello_world.py 对比：同样是 `@dynamo_worker()`、同样用 `runtime.endpoint(...)` 拿 endpoint——**服务端与客户端共用同一套 API 入口**，区别只在下一步调用什么：服务端调 `serve_endpoint`（注册），客户端调 `endpoint.client()`（连接）。[\_core.pyi:196-202](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/bindings/python/src/dynamo/_core.pyi#L196-L202) 说明 `client()` 默认使用 round-robin 路由，可选传 `router_mode` 改变。

请求与流式消费：

[examples/custom_backend/hello_world/client.py:41-43](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/examples/custom_backend/hello_world/client.py#L41-L43)

`await client.generate(...)` 返回的不是数据而是一个流对象；`async for` 逐帧取出，`response.data()` 剥掉传输层包装拿到业务负载。这个 `.data()` 定义在 Rust 侧的 `Annotated` 类上：

[lib/bindings/python/rust/lib.rs:2110-2139](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/bindings/python/rust/lib.rs#L2110-L2139)

流中的每一帧默认被包成 `Annotated`，提供 `data()`（负载）、`is_error()`（是否错误帧）、`event()`、`comments()` 四个访问器。而流本身的迭代协议（`__aiter__`/`__anext__`）由 `AsyncResponseStream` 实现（[lib.rs:2019-2061](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/bindings/python/rust/lib.rs#L2019-L2061)）：它把一个 tokio mpsc channel 适配成 Python 异步迭代器，channel 关闭时抛出 `StopAsyncIteration` 结束循环。还有一个实用细节：若调用 `generate(request, annotated=False)`，[\_core.pyi:374-379](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/bindings/python/src/dynamo/_core.pyi#L374-L379) 签名中的 `annotated` 参数会让 Rust 直接返回裸负载（[lib.rs:2049-2054](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/bindings/python/rust/lib.rs#L2049-L2054)），连 `.data()` 都不用调。

除了 `generate`，`Client` 还有三种显式路由方法（[\_core.pyi:340-372](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/bindings/python/src/dynamo/_core.pyi#L340-L372)）：`random()`（随机实例）、`round_robin()`（轮询）、`direct(request, instance_id)`（指定实例）。`generate()` 本质上是"按默认路由模式发请求"的便捷入口。实例信息可随时用 `instance_ids()`（[\_core.pyi:297-304](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/bindings/python/src/dynamo/_core.pyi#L297-L304)）查询。

#### 4.3.4 代码实践

**实践目标**：体会 `wait_for_instances()` 的"等上线"语义和 `Annotated` 包装的存在。

**操作步骤**：

1. **先启动 client、后启动 backend**（故意颠倒 README 的顺序），观察 client 在 `wait_for_instances()` 处安静等待；随后启动 backend，确认 client 自动继续往下走。
2. 在 client 的循环体里加两行调试打印（示例代码，修改你本地副本即可）：

   ```python
   # 示例代码：加在 while True 循环开头
   print("instances:", client.instance_ids())
   stream = await client.generate("world,sun,moon,star")
   async for response in stream:
       print(type(response).__name__, "->", response.data())
   ```

3. （可选）把 `client.generate(...)` 改成 `await client.generate("world", annotated=False)`，打印语句相应改成直接 `print(response)`。

**需要观察的现象**：步骤 2 应打印出 `Annotated -> Hello world!` 这样的行，证明每帧是 `Annotated` 对象、`.data()` 取负载；步骤 3 中 `response` 直接就是字符串。步骤 1 中颠倒启动顺序时 client 不报错、只是等待。

**预期结果**：`wait_for_instances()` 返回后 `instance_ids()` 至少含一个实例 ID；三种路由方法行为一致（只有一个实例时看不出差别）。

**待本地验证**：`instance_ids()` 返回的具体数值格式（是否为小整数列表）请以实际输出为准。

#### 4.3.5 小练习与答案

**练习 1**：如果删掉 `await client.wait_for_instances()` 这一行直接 `generate`，会发生什么？

**参考答案**：在没有任何在线实例时，请求无处投递，会走异常路径被 `except Exception` 捕获，打印错误并按指数退避重试；等 backend 上线后仍能恢复。`wait_for_instances()` 的价值是把"等实例上线"和"发请求"分开，避免无意义的失败请求——对读多写少的控制逻辑尤其重要。

**练习 2**：想让请求总是打到同一个指定实例，用哪个方法？

**参考答案**：`await client.direct(request, instance_id)`（[\_core.pyi:362-372](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/bindings/python/src/dynamo/_core.pyi#L362-L372)），`instance_id` 可来自 `instance_ids()` 的返回值。

**练习 3**：`response.data()` 与 `response.event()` 分别返回什么？什么时候你会关心 `event()`？

**参考答案**：`data()` 返回业务负载本体；`event()` 返回帧携带的事件名（如错误/控制类事件），`comments()` 返回附加注释（见 [lib.rs:2124-2139](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/bindings/python/rust/lib.rs#L2124-L2139)）。写通用消费者（比如要把错误帧与数据帧区别对待、或做请求取消联动）时需要检查 `is_error()`/`event()`；只关心生成内容时用 `data()` 即可。

### 4.4 从 dynamo.runtime 到 dynamo.llm，再到 Kubernetes

#### 4.4.1 概念说明

**两个模块的分工**。`dynamo.runtime` 提供的是"分布式运行时原语"：endpoint 注册、实例发现、流式收发——与业务无关，任何 Python 服务都能用（本讲的 hello_world 就是纯 `dynamo.runtime` 应用，连"模型"的概念都没有）。`dynamo.llm` 则是 LLM 领域的高层封装：

[lib/bindings/python/src/dynamo/llm/\_\_init\_\_.py:10-56](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/bindings/python/src/dynamo/llm/__init__.py#L10-L56)

从这里再导出的是 `HttpService`（OpenAI 兼容 HTTP 服务）、`KvRouter`（KV 感知路由）、`make_engine`（引擎装配）、`ModelInput`、`register_model`、`WorkerType` 等 LLM 语义的对象。回顾 u1-l2 的 sample 后端：它走的是更高一层的 `Worker.run` 封装——[components/src/dynamo/common/backend/worker.py:217-229](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/components/src/dynamo/common/backend/worker.py#L217-L229) 的 `Worker` 只是个薄垫片，生命周期状态机、信号处理、优雅下线全在 Rust（该文件 [L4-L16](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/components/src/dynamo/common/backend/worker.py#L4-L16) 的模块文档写明了这一点）。层次关系可以记成：

```text
dynamo.runtime  （本讲：endpoint/client 原语，业务无关）
      ▲ 被
dynamo.llm + common/backend Worker  （LLM 语义：HTTP 服务、路由、引擎生命周期）
      ▲ 被
dynamo.frontend / dynamo.vllm / ... （具体产品形态，u5、u8 讲）
```

**从本地走向 K8s**。本地两个终端跑的两个进程，到集群上就是 `DynamoGraphDeployment`（DGD）里的两个 component。hello_world 的 YAML 是最小示范。

#### 4.4.2 核心流程

DGD 把"进程拓扑"翻译成"工作负载拓扑"：

```text
DynamoGraphDeployment (hello-world)
   ├─ components[0]: Frontend        (type: frontend)
   │    └─ podTemplate: /bin/sh -c "python3 client.py"     ← 就是 client.py 进程
   └─ components[1]: HelloWorldWorker (type: worker)
        └─ podTemplate: /bin/sh -c "python3 hello_world.py" ← 就是服务端进程
```

每个 component 声明副本数、容器镜像、启动命令、探针与资源；Operator（u10 讲）负责把它们变成实际的 Pod。两个容器的工作目录都指向示例目录（`workingDir: /workspace/examples/custom_backend/hello_world/`），说明镜像是"整个仓库打进去"的开发镜像。

#### 4.4.3 源码精读

看 DGD 的骨架与两个 component 的关键差异：

[examples/custom_backend/hello_world/deploy/hello_world.yaml:4-11](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/examples/custom_backend/hello_world/deploy/hello_world.yaml#L4-L11)

`apiVersion: nvidia.com/v1beta1` + `kind: DynamoGraphDeployment` 是 DGD 的 CRD 标识；`spec.backendFramework: vllm` 声明后端框架类型，`spec.components` 是组件列表。

[examples/custom_backend/hello_world/deploy/hello_world.yaml:52-57](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/examples/custom_backend/hello_world/deploy/hello_world.yaml#L52-L57)

`HelloWorldWorker` component 的名字与 `type: worker`（L91）——Operator 用 `type` 区分 frontend/worker 并施加不同的网络与 Etcd 配置。L56-L57 显示容器命令就是 `/bin/sh -c` 包着的 `python3 hello_world.py`，与本地手动运行完全一致。

README 对这个 YAML 有一段重要的"祛魅"说明：

[examples/custom_backend/hello_world/README.md:98-98](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/examples/custom_backend/hello_world/README.md#L98)

它明确指出这是一个**退化示例**：client 不是 web 服务器，而是一次性发送固定文本的脚本，跑完就退出。所以集群里真正持续运行的只有 `HelloWorldWorker` 一个 Pod；这个例子要展示的是 worker 的写法，不是标准的 Frontend-Backend 部署形态（标准形态在 u1-l2 已见过：frontend 进程 + engine worker 进程）。

#### 4.4.4 代码实践

**实践目标**：不部署集群，纯靠阅读把 YAML 字段映射到你已经跑过的本地行为。

**操作步骤**：

1. 打开 [deploy/hello_world.yaml](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/examples/custom_backend/hello_world/deploy/hello_world.yaml)，逐个 component 回答下表中的问题。
2. 填写这张映射表（示例答案已给出一部分）：

   | YAML 字段 | 对应本地概念 | 你的答案 |
   |-----------|-------------|----------|
   | `components[1].name = HelloWorldWorker` | 终端一里的 `python hello_world.py` 进程 | （已给） |
   | `components[1].replicas = 1` | 起几个服务端进程 | ？ |
   | `components[0].type = frontend` 与 `components[1].type = worker` | 本地两个终端的角色差异 | ？ |
   | `readinessProbe`（[L77-L82](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/examples/custom_backend/hello_world/deploy/hello_world.yaml#L77-L82) 的 `exit 0`） | 本地靠什么判断对方"好了" | 提示：对照 `wait_for_instances()` |
   | `workingDir` | 你 `cd` 到的目录 | ？ |

3. 思考题（写进笔记）：如果 `replicas` 从 1 改成 3，客户端的 round-robin 路由会发生什么？

**需要观察的现象**：本实践为源码阅读型，无运行现象；重点是能不查资料说出每个字段对应本地实验的哪个动作。

**预期结果**：映射表填完后，你应当能说出——`replicas=3` 时 DGD 会起 3 个服务端 Pod，客户端 `Client` 观察到 3 个实例并把请求轮询分摊（这正是 4.3 说的"client 背后是实例观察者"）。

#### 4.4.5 小练习与答案

**练习 1**：用一句话概括 `dynamo.runtime` 与 `dynamo.llm` 的边界。

**参考答案**：`dynamo.runtime` 暴露与领域无关的分布式原语（`DistributedRuntime`/`Endpoint`/`Client` + 两个装饰器），`dynamo.llm` 在其上再导出 LLM 领域对象（`HttpService`、`KvRouter`、`make_engine`、`WorkerType` 等）——前者是"网络与发现的语法"，后者是"推理服务的词汇"。

**练习 2**：hello_world 的 K8s 部署里为什么说"只会看到 HelloWorldWorker Pod 在运行"？

**参考答案**：因为 client 组件（`Frontend`）执行的 `client.py` 是一次性脚本：发一轮固定请求、打印完就退出，Pod 随之结束；README [L98](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/examples/custom_backend/hello_world/README.md#L98) 明确称其为退化示例，它演示的是 worker 而非标准部署形态。

**练习 3**：`type: frontend` 与 `type: worker` 这两个字段最终被谁消费？

**参考答案**：被 Dynamo Operator 的 reconciler 消费——它根据 component 类型决定生成什么样的工作负载与网络/Etcd 配置。这属于 u10 的内容，本讲只需要知道这个字段不是摆设。

## 5. 综合实践

**任务**：把 `hello_world.py` 改造成一个 `echo_upper` worker——收到的请求按逗号切分后，把每个词**转成大写**再流式返回，并用配套客户端验证。这个任务贯穿本讲全部内容：装饰器、三段式路径、serve_endpoint、client 连接、流式消费。

**操作步骤**：

1. 复制并改写服务端（以下为**示例代码**，基于 [hello_world.py](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/examples/custom_backend/hello_world/hello_world.py) 修改，保存为 `echo_upper.py`，放在同目录）：

   ```python
   # 示例代码：echo_upper.py —— hello_world.py 的大写回显改版
   import asyncio
   import logging

   import uvloop

   from dynamo.runtime import DistributedRuntime, dynamo_endpoint, dynamo_worker
   from dynamo.runtime.logging import configure_dynamo_logging

   logger = logging.getLogger(__name__)
   configure_dynamo_logging(service_name="backend")


   @dynamo_endpoint(str, str)
   async def upper_generator(request: str):
       logger.info(f"Received request: {request}")
       for word in request.split(","):
           await asyncio.sleep(0.3)
           yield f"ECHO {word.upper()}!"


   @dynamo_worker()
   async def worker(runtime: DistributedRuntime):
       # 关键改动：换一个 namespace，避免和原示例撞名
       endpoint = runtime.endpoint("echo_upper.backend.generate")
       logger.info("Serving endpoint echo_upper/backend/generate")
       await endpoint.serve_endpoint(upper_generator)


   if __name__ == "__main__":
       uvloop.install()
       asyncio.run(worker())
   ```

2. 复制并改写客户端（**示例代码**，保存为 `echo_client.py`；相对 [client.py](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/examples/custom_backend/hello_world/client.py) 只改了路径字符串与输入文本）：

   ```python
   # 示例代码：echo_client.py —— client.py 的最小改版
   import asyncio

   import uvloop

   from dynamo.runtime import DistributedRuntime, dynamo_worker


   @dynamo_worker()
   async def worker(runtime: DistributedRuntime):
       endpoint = runtime.endpoint("echo_upper.backend.generate")
       client = await endpoint.client()
       await client.wait_for_instances()

       stream = await client.generate("hello,dynamo,worker")
       async for response in stream:
           print(response.data())


   if __name__ == "__main__":
       uvloop.install()
       asyncio.run(worker())
   ```

3. 两个终端分别运行（环境变量与 README 一致）：

   ```bash
   cd examples/custom_backend/hello_world
   DYN_DISCOVERY_BACKEND=file DYN_EVENT_PLANE=zmq python echo_upper.py   # 终端一
   DYN_DISCOVERY_BACKEND=file DYN_EVENT_PLANE=zmq python echo_client.py  # 终端二
   ```

4. 验证通过后，回到第 5 步做行为实验：把客户端改成 `annotated=False`，确认输出不变、但每帧直接是字符串。

**需要观察的现象**：

- 客户端每隔约 0.3 秒打印一行 `ECHO HELLO!`、`ECHO DYNAMO!`、`ECHO WORKER!`。
- 故意先跑客户端再跑服务端：客户端停在 `wait_for_instances()`，服务端一起来就继续。
- 把服务端的 namespace 改回 `hello_world` 而客户端仍用 `echo_upper`：客户端永远等待（两个"电话簿"条目对不上）。

**预期结果**：大写回显按帧到达；你已经独立写出了"Dynamo worker + client"最小闭环，且能解释其中每一步在源码里的落点（装饰器在 [\_\_init\_\_.py:21-51](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/bindings/python/src/dynamo/runtime/__init__.py#L21-L51)，serve/client 在 [\_core.pyi:144-202](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/bindings/python/src/dynamo/_core.pyi#L144-L202)，`.data()` 在 [lib.rs:2128-2130](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/bindings/python/rust/lib.rs#L2128-L2130)）。

**待本地验证**：完整运行效果需在装好 `ai-dynamo-runtime` 的环境执行；若 `DYN_EVENT_PLANE=zmq` 报错，说明当前版本默认事件面已切换，可尝试只保留 `DYN_DISCOVERY_BACKEND=file`。

## 6. 本讲小结

- `dynamo.runtime` 是 PyO3 扩展 `dynamo._core` 外的一层薄封装：再导出 `DistributedRuntime`/`Endpoint`/`Client`/`Context`/`PyAsyncRequestStream` 五个 Rust 类，外加 `@dynamo_worker()` 与 `@dynamo_endpoint` 两个纯 Python 装饰器。
- `@dynamo_worker()` 的全部工作：取事件循环、读 `DYN_REQUEST_PLANE`/`DYN_DISCOVERY_BACKEND` 环境变量（默认 `tcp`/`etcd`）、构造 `DistributedRuntime` 注入你的函数；client 进程与服务端进程用的是同一个装饰器。
- 服务端三步：`runtime.endpoint("namespace.component.endpoint")` 拿端点 → `await endpoint.serve_endpoint(异步生成器)` 注册 → 处理函数每请求一个生成器实例、`yield` 一帧客户端收一帧。
- 客户端三步：`await endpoint.client()` 建客户端（默认 round-robin）→ `await client.wait_for_instances()` 等实例上线 → `await client.generate(req)` 拿流，逐帧 `response.data()` 取负载；`Annotated` 还提供 `is_error()`/`event()`。
- `dynamo.runtime`（领域无关原语）与 `dynamo.llm`（`HttpService`/`KvRouter`/`make_engine` 等 LLM 高层对象）是上下两层；hello_world 只用了下层。
- 本地的两个进程对应 DGD 里的两个 component（`type: frontend` / `type: worker`），容器命令就是你在终端里敲的那条 `python3 xxx.py`。

## 7. 下一步学习建议

- **下一讲（u2-l2）**将钻进 PyO3 绑定本身：`lib/bindings/python/rust/lib.rs` 里 `#[pyclass]`/`#[pymethods]` 如何把 Rust 结构体变成你在本讲里用到的 Python 类，`make_engine` 等函数又从哪些 Rust 模块导出。读完那一讲，本讲里所有"Rust 侧"的引用点都会落到实处。
- **u2-l3（cancellation 示例）**把本讲的单跳链路拉长成 client → middle_server → server 三跳，重点观察取消信号如何沿链路传播——你在本讲已经见过 `except asyncio.CancelledError: raise` 这条纪律。
- 想提前看"高层封装"的读者可以对比阅读 [components/src/dynamo/common/backend/worker.py](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/components/src/dynamo/common/backend/worker.py)（`Worker.run` 垫片）的模块文档字符串，体会"生命周期在 Rust、引擎语义在 Python"的分层。
- 进阶路线预告：u3-l2 将在 Rust 源码里追一遍 endpoint 注册如何写进 etcd、客户端如何按 tag 订阅实例变化——即本讲 `serve_endpoint`/`wait_for_instances` 背后的完整机制。
