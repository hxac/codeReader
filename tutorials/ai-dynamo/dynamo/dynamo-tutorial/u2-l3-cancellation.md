# 请求取消与生命周期：cancellation 示例

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释 Dynamo 中 `Context` 的生命周期与三态状态机（Live / Stopped / Killed），以及 `stop_generating()`、`is_stopped()`、`is_killed()` 各自的语义。
2. 跑通 `examples/custom_backend/cancellation` 示例的两种拓扑（直连、带中间层），并读懂三个进程（client / middle_server / server）的代码。
3. 说清取消信号在多跳链路上的传播路径：客户端 `context.stop_generating()` → 父 `Controller` 置为 Stopped → 递归传播给所有 `link_child` 链接的子 context → 下游 worker 的 `context.is_stopped()` 变为真。
4. 在自己的 worker 里正确实现「取消感知」：在每个 yield 前检查取消状态、在收到取消后统计已生成的 token 数并优雅收尾。

本讲承接 u2-l1 的 hello_world：那里你已经会用 `DistributedRuntime` + `serve_endpoint` 写一个最小 worker。本讲把视角从「怎么收发」转向「怎么取消与清理」——这是生产环境（前端断连、客户端停止生成、worker 逐级释放资源）的基础能力。

## 2. 前置知识

- **流式生成器**：Dynamo 的 endpoint handler 是一个异步生成器（`async def generate(self, request, context): ... yield x`），每 `yield` 一次，客户端就收到一帧。取消问题的本质是：**生产端（server）如何知道消费端（client）不再需要后续帧了**。
- **Context 是什么**：每个请求在进入 worker 时都附带一个 `Context` 对象（Python 类，由 Rust 的 `AsyncEngineContext` trait 对象包装而成）。它承载请求的取消控制、trace 信息与元数据。本讲只关注取消部分。
- **优雅取消 vs 硬终止**：
  - `stop_generating()`：**优雅取消**。告知引擎「别再产出新结果了」，已在流水线中的结果不失效，幂等。
  - `kill()`：**硬终止**。通常意味着客户端与服务端的网络连接已被切断，偏好「不排空剩余流直接终止」，是否支持取决于引擎实现。
- **多跳链路（proxy 模式）**：真实部署里请求往往不止一跳——frontend → 路由器 → prefill worker → decode worker。中间节点如果只做转发（proxy），就必须把上游传来的 context **透传**给下游，否则取消信号到不了真正干活的地方。
- **需要的前置组件**：本示例 `DistributedRuntime(loop, "file", "nats")` 的三个参数分别是事件循环、服务发现后端（`file`，无需 etcd）、请求面模式（`nats`）。因此按原样运行需要本地有一个 NATS server；事件面未显式指定时默认 ZMQ（无需额外组件）。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [examples/custom_backend/cancellation/README.md](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/examples/custom_backend/cancellation/README.md) | 示例说明：两种拓扑的启动方式与预期现象 |
| [examples/custom_backend/cancellation/server.py](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/examples/custom_backend/cancellation/server.py) | 后端 worker：循环生成 0-999，每次 yield 前检查 `context.is_stopped()` |
| [examples/custom_backend/cancellation/middle_server.py](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/examples/custom_backend/cancellation/middle_server.py) | 中间层（代理）：把请求转发给后端，并把 context 透传下去 |
| [examples/custom_backend/cancellation/client.py](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/examples/custom_backend/cancellation/client.py) | 客户端：收到 3 帧后调用 `context.stop_generating()` 取消 |
| [lib/bindings/python/rust/context.rs](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/bindings/python/rust/context.rs) | Python `Context` 类的 PyO3 实现（`is_stopped` / `stop_generating` / `async_killed_or_stopped` 等方法在此定义） |
| [lib/runtime/src/pipeline/context.rs](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/runtime/src/pipeline/context.rs) | Rust 侧取消状态机：`State` 枚举与 `Controller`（取消传播的真正发生地） |
| [lib/bindings/python/rust/lib.rs](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/bindings/python/rust/lib.rs) | `create_request_context`：出站请求带 context 时如何建立父子链接 |
| [lib/runtime/src/distributed.rs](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/runtime/src/distributed.rs) | `RequestPlaneMode` 定义（解释示例构造函数第三个参数 `"nats"` 的含义） |
| [docs/fern/pages/developer-guide/knowledge-base/concepts/fault-tolerance/request-cancellation-architecture.md](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/docs/fern/pages/developer-guide/knowledge-base/concepts/fault-tolerance/request-cancellation-architecture.md) | 官方取消架构文档（示例 README 底部引用的就是它） |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**4.1 Context 生命周期**、**4.2 cancellation 示例三进程**、**4.3 取消传播的完整闭环**。

### 4.1 Context 生命周期：一个请求的取消状态机

#### 4.1.1 概念说明

一次请求从客户端发出到 worker 处理，Dynamo 会为它维护一个「取消状态」。这个状态只有三个值：**Live（活着）→ Stopped（被优雅取消）→ Killed（被硬终止）**。状态只能单向流转，一旦离开 Live 就回不去。

挂在这个状态机上的对象就是 `Context`。你在 Python 里 `from dynamo._core import Context` 拿到的类，是一个薄壳：真正存状态的是 Rust 侧的 `Controller`（一个用 `tokio::sync::watch` 通道实现的状态广播器）。`Context` 同时还承载 trace 信息、请求元数据等，本讲只看取消相关的那部分。

为什么需要它？因为流式生成是**跨进程、跨机器**的：客户端进程消费 token，worker 进程生产 token，两者之间隔着网络。Python 原生的 `asyncio.CancelledError` 只能在单个进程内传播，跨进程必须有一个显式的、可序列化关联的「取消信号载体」——这就是 Context 存在的意义。

#### 4.1.2 核心流程

一个 Context 的生命周期大致是：

```text
创建（Live）
  │
  ├── client 端：Context() 显式创建，随请求发出
  ├── worker 端：框架为每个进入的请求自动创建，注入到 handler 的 context 参数
  │
  │  请求期间任何一方都可查询：
  │    context.is_stopped()  → 状态 != Live 即 True
  │    context.is_killed()   → 状态 == Killed 才 True
  │
  ├── client 调 context.stop_generating()
  │      → Controller 递归对所有子 context 调 stop_generating()
  │      → 状态置为 Stopped
  │
  └── 连接断开等硬终止路径 → 状态置为 Killed
```

两个常用 API 的选择标准：

| 场景 | 用什么 |
|------|--------|
| 客户端主动「停止生成」（如用户点了停止按钮） | `context.stop_generating()` |
| worker 在循环里做昂贵计算前检查 | `context.is_stopped()`（同步、廉价） |
| worker 想在长等待中被异步唤醒 | `await context.async_killed_or_stopped()` |
| 区分「软取消」与「连接已断」 | `is_killed()` 为 True 表示硬终止 |

#### 4.1.3 源码精读

**① Python `Context` 类的真身**。[lib/bindings/python/rust/context.rs:L59-L70](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/bindings/python/rust/context.rs#L59-L70)：`Context` 是 PyO3 的 `#[pyclass]`，字段 `inner: Arc<dyn AsyncEngineContext>` 持有真正的 Rust 实现（通常就是 `Controller`）。

**② Python 侧的取消三件套**。[lib/bindings/python/rust/context.rs:L296-L306](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/bindings/python/rust/context.rs#L296-L306) 定义了 `is_stopped()`、`is_killed()`、`stop_generating()` 三个方法——每个都只是把调用转发给 `self.inner`（Rust trait 对象），没有任何 Python 侧逻辑。这印证了 u2-l2 讲过的「wrapper + inner」模式。

**③ 异步等待取消**。[lib/bindings/python/rust/context.rs:L312-L325](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/bindings/python/rust/context.rs#L312-L325) 的 `async_killed_or_stopped()` 用 `tokio::select!` 同时监听 `killed()` 和 `stopped()` 两个 future，任一完成即返回 `True`。适合「worker 正在长等待、希望取消一到就立刻中断」的场景。

**④ Rust 状态机**。[lib/runtime/src/pipeline/context.rs:L354-L369](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/runtime/src/pipeline/context.rs#L354-L369)：`State` 枚举只有 `Live / Stopped / Killed` 三个值；`Controller` 持有一个 `watch` 通道的发送端与接收端（`tx` / `rx`），外加 `child_context: Mutex<Vec<Arc<dyn AsyncEngineContext>>>` ——这个 Vec 就是「子 context 列表」，取消传播的依据。

**⑤ 状态查询的实现**。[lib/runtime/src/pipeline/context.rs:L412-L418](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/runtime/src/pipeline/context.rs#L412-L418)：

```rust
fn is_stopped(&self) -> bool {
    *self.rx.borrow() != State::Live
}
fn is_killed(&self) -> bool {
    *self.rx.borrow() == State::Killed
}
```

注意 `is_stopped()` 的判定是「不是 Live」——也就是说 **Killed 蕴含 Stopped**：连接被硬终止时，`is_stopped()` 同样返回 True。这就是为什么示例的 server 只检查 `is_stopped()` 就够用。

**⑥ 取消传播的核心 8 行**。[lib/runtime/src/pipeline/context.rs:L438-L452](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/runtime/src/pipeline/context.rs#L438-L452)：`stop_generating()` 先把子 context 列表克隆一份（避免父被意外链接在子之下时死锁），对每个子 context **递归调用** `stop_generating()`，最后才把自己的状态 `send(State::Stopped)`。取消就这样沿父子链一层层传下去。`kill()`（[L470-L484](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/runtime/src/pipeline/context.rs#L470-L484)）结构完全相同，只是置为 `Killed`。`link_child()`（[L486-L491](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/runtime/src/pipeline/context.rs#L486-L491)）就是把子 context push 进那个 Vec。

**⑦ 官方架构文档**对传播机制的总结在 [request-cancellation-architecture.md:L32-L34](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/docs/fern/pages/developer-guide/knowledge-base/concepts/fault-tolerance/request-cancellation-architecture.md#L32-L34)：父 context 被取消时，所有链接的子 context 自动被取消，从而让整条请求管线（frontend → 各级 worker）一起停下来。

#### 4.1.4 代码实践

**实践目标**：不改任何服务代码，直接在 Python 里验证 Context 的取消语义——确认 `stop_generating()` 之后 `is_stopped()` 立即为真、`async_killed_or_stopped()` 立即完成。

**操作步骤**（以下为示例代码，保存为任意 `.py` 文件运行）：

```python
# ctx_probe.py —— 验证 Context 取消语义（示例代码）
import asyncio
from dynamo._core import Context

async def main():
    ctx = Context()
    print("初始 id:", ctx.id())
    print("初始 is_stopped:", ctx.is_stopped())        # 预期 False
    print("初始 is_killed:", ctx.is_killed())          # 预期 False

    ctx.stop_generating()
    print("取消后 is_stopped:", ctx.is_stopped())      # 预期 True
    print("取消后 is_killed:", ctx.is_killed())        # 预期 False（优雅取消不是 kill）

    # 已停止的 context 上再调一次 stop_generating，验证幂等、不抛异常
    ctx.stop_generating()
    print("重复取消后 is_stopped:", ctx.is_stopped())  # 预期仍为 True

    done = await ctx.async_killed_or_stopped()
    print("async_killed_or_stopped 返回:", done)        # 预期 True，且立即返回

asyncio.run(main())
```

运行方式：在装好 `ai-dynamo-runtime`（或本地 maturin 构建产物）的环境里执行 `python3 ctx_probe.py`。

**需要观察的现象**：六行输出与注释里的预期完全一致；特别是 `await ctx.async_killed_or_stopped()` 没有等待就返回了。

**预期结果**：验证了三件事——`stop_generating()` 同步生效、幂等、能让异步等待立即解除。这三点正是 4.2 中 server 端「每帧检查一次」模式能够工作的前提（检查是廉价的同步调用）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `is_stopped()` 的实现是 `!= State::Live` 而不是 `== State::Stopped`？

**答案**：因为状态机里 Killed 也表示「请求不应继续」。若实现为 `== Stopped`，硬终止（连接断开）时 worker 检查 `is_stopped()` 会得到 False，继续白算。用 `!= Live` 让 Killed 蕴含 Stopped，worker 只需检查一个标志就能覆盖两种取消路径（见 [lib/runtime/src/pipeline/context.rs:L412-L418](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/runtime/src/pipeline/context.rs#L412-L418)）。

**练习 2**：`Controller` 为什么用 `tokio::sync::watch` 通道而不是 `Mutex<bool>` 来存状态？

**答案**：`Mutex<bool>` 只能**轮询**（同步查询），无法异步等待；`watch` 通道同时支持两种用法——`rx.borrow()` 提供廉价同步读（`is_stopped`），`rx.changed().await` 提供异步唤醒（`stopped()` / `killed()`，进而支撑 Python 的 `async_killed_or_stopped()`）。一个原语同时满足「每帧廉价检查」和「长等待立即中断」两类需求。

**练习 3**：如果同一个 Context 对象被 `stop_generating()` 调用两次，第二次会发生什么？

**答案**：什么坏事都没有。`stop_generating()` 会再次遍历子 context（对已停止的子 context 而言也是幂等的），随后 `tx.send(State::Stopped)` 只是重复发送同一个值。官方文档也明确说明该方法是幂等的（[request-cancellation-architecture.md:L59-L62](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/docs/fern/pages/developer-guide/knowledge-base/concepts/fault-tolerance/request-cancellation-architecture.md#L59-L62)）。

### 4.2 cancellation 示例：三个进程、两种拓扑

#### 4.2.1 概念说明

`examples/custom_backend/cancellation` 用三个小脚本演示取消的两种拓扑：

- **直连（默认）**：`client → server`。client 直接调用后端 endpoint `demo.server.generate`。
- **代理（--middle）**：`client → middle_server → server`。client 调 `demo.middle.generate`，middle_server 再转发给 `demo.server.generate`。

后端 server 是一个「永动机」：循环 1000 次，每次 sleep 0.1 秒后 yield 一个数字——模拟一个需要长时间流式生成的模型。client 只想要前 3 帧，拿到后主动取消。如果没有取消机制，server 会傻算满 1000 次（浪费 100 秒「GPU 时间」）；有了取消传播，server 在收到信号的那次迭代就停下来。

middle_server 的存在不是为了演示转发本身，而是为了演示**多跳链路上取消能不能穿透中间层**——答案是能，前提是中间层把 context 透传给下游。

#### 4.2.2 核心流程

带中间层拓扑下的一次完整取消时序（对照 README 的 [L68-L75](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/examples/custom_backend/cancellation/README.md#L68-L75)）：

```text
client                          middle_server                     server
  │  generate(req, context=ctx)   │                                │
  ├──────────────────────────────▶ 创建子 context（link 到 ctx）      │
  │                               ├─ generate(req, context=ctx') ──▶ 创建子 context（link 到 ctx'）
  │                               │                                ├── yield 0 ──┐
  │ ◀── 0 ────────────────────────┼── 转发 0 ◀──────────────────────┘   │
  │ ◀── 1, 2 （同上路径）           │                                │
  │                                │                                │
  │ ctx.stop_generating()          │                                │
  │   → ctx 状态 = Stopped          │                                │
  │   → 递归：ctx' = Stopped        │                                │
  │   → 递归：server 端 context = Stopped                            │
  │                                │                                │
  │ break（停止消费）                │                                │
  │                                ◀── 流结束                        │
  │                                ├── 打印 "Backend stream ended"   │
  │                                │                                ├── 下一轮迭代检查 is_stopped() == True
  │                                │                                ├── raise asyncio.CancelledError
  │                                │                                └── 生成器终止，资源回收
```

关键点：取消信号**不走数据流**，而是走 context 的父子链接（控制信号），因此即使 middle_server 还在 `async for` 里等下一帧，server 端的状态也已经被翻转，下一次迭代自查时就会退出。

#### 4.2.3 源码精读

**① server：取消感知的生产者**。[server.py:L16-L28](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/examples/custom_backend/cancellation/server.py#L16-L28)：

```python
async def generate(self, request, context):
    for i in range(1000):
        print(f"Server: Processing iteration {i}")
        if context.is_stopped():
            print(f"Server: Cancelled at iteration {i}")
            raise asyncio.CancelledError
        await asyncio.sleep(0.1)
        print(f"Server: Sending iteration {i}")
        yield i
```

这段代码是 Dynamo worker 取消处理的**标准范式**，值得逐行拆：

- handler 签名带 `context` 参数——按 u2-l1 讲过的机制，框架按参数名探测注入；不写这个参数就得不到取消通知。
- 检查放在**每次迭代开头、昂贵工作（这里是 sleep）之前**——「先查再做」。
- 检测到取消后 `raise asyncio.CancelledError`——用标准的 Python 取消异常终止生成器，让框架的清理逻辑接管。

**② server：runtime 与 endpoint 注册**。[server.py:L33-L46](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/examples/custom_backend/cancellation/server.py#L33-L46)：`DistributedRuntime(loop, "file", "nats")` 创建运行时，`runtime.endpoint("demo.server.generate")` 取端点（`namespace.component.endpoint` 三段式路径），`serve_endpoint(handler.generate)` 阻塞服务。注意第三个参数 `"nats"` 是**请求面模式**——[lib/bindings/python/rust/lib.rs:L1157-L1164](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/bindings/python/rust/lib.rs#L1157-L1164) 的签名 `(event_loop, discovery_backend, request_plane, ...)` 说明了各参数位置；[lib/runtime/src/distributed.rs:L795-L805](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/runtime/src/distributed.rs#L795-L805) 定义了 `RequestPlaneMode`：`Nats` 是 legacy 模式，`Tcp` 是默认。所以**按原样运行本示例需要本地 NATS server**（详见 4.2.4）。

**③ middle_server：透传 context 的代理**。[middle_server.py:L29-L45](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/examples/custom_backend/cancellation/middle_server.py#L29-L45)：

```python
async def generate(self, request, context):
    ...
    # Forward request to backend using round_robin with the same context
    # This passes the cancellation context through to the backend
    stream = await self.backend_client.generate(request, context=context)
    async for response in stream:
        data = response.data()
        print(f"Middle server: Forwarding response {data}")
        yield data
    print("Middle server: Backend stream ended")
```

整段的核心就是 `context=context` 这一个关键字参数：把**上游注入的 context 原样传给下游请求**。这一行做了两件事（机制在 4.3 详述）：建立「middle 的 context → server 端新 context」的父子链接；让 server 端的 handler 拿到的 context 与 client 持有的是同一条取消链。假如漏写这个参数，client 的取消最多传到 middle_server，server 会继续生成满 1000 次。

**④ middle_server：初始化与自身注册**。[middle_server.py:L21-L27](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/examples/custom_backend/cancellation/middle_server.py#L21-L27) 中 `initialize()` 先连接后端 endpoint 并 `wait_for_instances()` 等后端上线；[middle_server.py:L58-L64](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/examples/custom_backend/cancellation/middle_server.py#L58-L64) 把自己注册成 `demo.middle.generate`。middle_server 同时是后端的 client 和前端的 server——这正是 frontend、router 等组件在 Dynamo 中的真实角色。

**⑤ client：谁发起取消**。[client.py:L14-L36](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/examples/custom_backend/cancellation/client.py#L14-L36)：

```python
context = Context()
stream = await client.generate("dummy_request", context=context)
iteration_count = 0
async for response in stream:
    number = response.data()
    print(f"Client: Received {number}")
    if iteration_count >= 2:
        print("Client: Cancelling after 3 responses...")
        context.stop_generating()
        break
    iteration_count += 1
```

注意：**取消的发起方持有自己的 Context 对象**，先创建、再随请求发出（`context=context`）、最后在本地调用 `stop_generating()`。这模拟的正是「用户点了停止生成按钮」：前端不需要断开连接，一个控制信号就够了。

**⑥ client：拓扑选择**。[client.py:L52-L64](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/examples/custom_backend/cancellation/client.py#L52-L64)：默认直连 `demo.server.generate`，加 `--middle` 参数则连 `demo.middle.generate`。README 的 [L30-L57](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/examples/custom_backend/cancellation/README.md#L30-L57) 给出了两种模式的启动顺序：先 server，再（可选）middle_server，最后 client。

#### 4.2.4 代码实践

**实践目标**：跑通示例的两种拓扑，亲眼看到「取消在第几帧生效、三个进程各自打印了什么」。

**操作步骤**：

1. 准备依赖。示例构造函数用了 `request_plane="nats"`，需要一个本地 NATS server：

   ```bash
   # 任选其一获得 nats-server
   nats-server &            # 已安装的话，默认监听 4222
   # 或用 docker
   docker run -d -p 4222:4222 nats:latest
   ```

   替代方案：把三个脚本里的 `DistributedRuntime(loop, "file", "nats")` 都改成 `DistributedRuntime(loop, "file", "tcp")`（`tcp` 是 `RequestPlaneMode` 的默认值），即可完全复用 u2-l1 的零依赖环境（file 发现 + ZMQ 事件面 + TCP 请求面）。此改法的行为待本地验证，但参数含义有源码依据。

2. 直连模式，开两个终端：

   ```bash
   cd examples/custom_backend/cancellation
   python3 server.py          # 终端 1
   python3 client.py          # 终端 2
   ```

3. 代理模式，开三个终端：

   ```bash
   python3 server.py          # 终端 1
   python3 middle_server.py   # 终端 2
   python3 client.py --middle # 终端 3
   ```

**需要观察的现象**：

- client 终端：依次打出 `Received 0 / 1 / 2`，然后 `Cancelling after 3 responses...` 和 `Stream stopped`，进程正常退出（总耗时约 0.3-0.5 秒，而非 100 秒）。
- server 终端：`Processing iteration 0/1/2...` 若干条，随后在某次迭代打出 `Cancelled at iteration N` 并停止输出——注意 N 通常略大于 2，因为取消信号到达时 server 可能已在处理第 3、4 次迭代。
- middle_server 终端（代理模式）：`Forwarding response 0/1/2` 之后打出 `Backend stream ended`，进程不退出、继续等下一个请求。

**预期结果**：两种拓扑下取消都生效；代理模式下 client 收到的帧比 server 实际生成的帧略少（middle 与 server 之间在途的帧被丢弃）。具体迭代序号的精确值待本地验证（取决于网络与调度时序）。

#### 4.2.5 小练习与答案

**练习 1**：如果把 middle_server.py 第 37 行的 `context=context` 改成不传 context，会发生什么？

**答案**：下游请求会创建一个独立的、无父链接的 context（见 4.3 的 `create_request_context`：`None` 分支直接 `request.into()`，不 link）。client 调 `stop_generating()` 后，取消只能传播到 middle_server 持有的那条链，server 端的 context 永远是 Live，server 会继续生成满 1000 次（100 秒）。这正是中间层必须透传 context 的原因。

**练习 2**：为什么 server 的检查点放在 `for` 循环的开头、`await asyncio.sleep(0.1)` 之前，而不是放在 `yield` 之后？

**答案**：放在昂贵工作之前才能省掉被浪费的计算——检测到取消后连这次的 sleep/计算都跳过，立刻抛 `CancelledError`。放在 yield 之后意味着每次都要先做完一轮工作再检查，被取消的那一轮计算就被浪费了。对于真实的 LLM worker，「昂贵工作」是一次前向推理，先查再做的收益大得多。

**练习 3**：client 里 `break` 和 `context.stop_generating()` 分别去掉一个，行为有什么不同？

**答案**：去掉 `break` 只调 `stop_generating()`：server 端会停止生成，流自然结束，client 的 `async for` 正常走完——这是更「优雅」的写法。去掉 `stop_generating()` 只 `break`：client 停止消费并退出进程，此时依赖**断连传播**路径（client 进程退出导致连接关闭，框架把 context 置为 Killed/Stopped，server 通过 `is_stopped()` 察觉）。后者是否以及多快生效取决于传输层实现，待本地验证——4.3 的实践会专门做这个实验。

### 4.3 取消传播的完整闭环与边界情况

#### 4.3.1 概念说明

4.2 里那个「取消沿链路传播」的箭头，落到源码上就是一次 `link_child`。本模块把闭环补全，并看一个容易被忽略的**竞态边界**：如果取消发生在「client 刚发出下游请求、但请求还没送达」的窗口期怎么办？

这不是学术问题。真实场景：frontend 收到用户断连，要把取消传给 prefill worker——可此时发给 prefill 的子请求可能还在路上。如果链接建立时只做 `link_child`，而父 context 在链接前一瞬间已置为 Stopped，子 context 就会被遗漏，worker 白干。

#### 4.3.2 核心流程

出站请求携带 context 时的完整处理流程（[lib/bindings/python/rust/lib.rs:L148-L173](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/bindings/python/rust/lib.rs#L148-L173)）：

```text
client.generate(request, context=ctx)
  │
  ├─ create_request_context(request, parent_ctx=Some(ctx))
  │    ├─ 创建子 context，继承父的 id 与 metadata 快照
  │    ├─ parent.link_child(child)          ← 建立父子链接
  │    └─ 若 parent.is_stopped() || parent.is_killed():
  │         child.stop_generating()          ← 竞态兜底：父已取消则立刻取消子
  │
  ├─ 请求发往下游 worker
  └─ 下游框架为该请求创建 server 端 context（挂进 handler 签名）
       └─ worker 每次迭代 context.is_stopped() 自查
```

于是整条取消链闭环为：

\[ \text{client } \texttt{stop\_generating()} \;\rightarrow\; \text{Controller.send(Stopped)} \;\rightarrow\; \forall \text{child}: \text{递归 stop\_generating} \;\rightarrow\; \text{server 端 } \texttt{is\_stopped()} = \text{True} \]

每一跳都是同一个 `Controller::stop_generating` 的递归调用（4.1 的第⑥点），没有额外的协议消息——**取消传播是进程内状态机的级联，跨进程的部分由请求面在建链时预先搭好**。

#### 4.3.3 源码精读

**① 建链与竞态兜底**。[lib/bindings/python/rust/lib.rs:L148-L173](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/bindings/python/rust/lib.rs#L148-L173)：

```rust
Some(parent_ctx) => {
    let child_ctx = RsContext::with_id_and_metadata(
        request,
        parent_ctx.inner().id().to_string(),
        parent_ctx.metadata_snapshot(),
    );
    parent_ctx.inner().link_child(child_ctx.context());
    if parent_ctx.inner().is_stopped() || parent_ctx.inner().is_killed() {
        // Let the server handle the cancellation for now since not all backends are
        // properly handling request exceptions
        // TODO: (DIS-830) Return an error if context is cancelled
        child_ctx.context().stop_generating();
    }
    child_ctx
}
```

三个细节：子 context **继承父的 id**（跨多跳仍是同一个请求标识，便于追踪与关联）；`link_child` 之后**立刻复查**父状态，已取消就把子也取消——这就是竞态兜底；注释里的 TODO 表明当前选择「让 server 端自己处理取消」，未来可能改为直接向调用方报错。

**② `Context()` 无参构造**。[lib/bindings/python/rust/context.rs:L263-L277](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/bindings/python/rust/context.rs#L263-L277)：`py_new` 在 id 为 `None` 时用 `Controller::default()`（即随机 UUID + Live 状态）。这就是 client.py 里 `Context()` 那一行背后发生的事——你在客户端创建的，是一个崭新的、独立根部的取消控制器。

**③ 两个相关但不同的场景**。官方文档 [request-cancellation-architecture.md:L14-L30](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/docs/fern/pages/developer-guide/knowledge-base/concepts/fault-tolerance/request-cancellation-architecture.md#L14-L30) 区分了 frontend 的两类检测（连接意外关闭、SSE 流意外关闭）与 worker 的两类检测（收到显式取消控制消息、TCP 连接掉线）。worker 收到信号后**框架只负责置位状态，是否真正停止由引擎实现自行决定**（文档 [L38-L40](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/docs/fern/pages/developer-guide/knowledge-base/concepts/fault-tolerance/request-cancellation-architecture.md#L38-L40) 特别强调：runtime 的取消计数指标只记「收到了信号」，不保证引擎真的中止）——这就是本示例 server.py 存在的全部理由：它演示引擎侧「愿意配合」的标准写法。

**④ 可观测性挂钩**。取消在 Prometheus 指标上有对应计数：frontend 侧 `dynamo_frontend_model_cancellation_total`、runtime 侧 `dynamo_component_cancellation_total`（文档 [L36-L38](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/docs/fern/pages/developer-guide/knowledge-base/concepts/fault-tolerance/request-cancellation-architecture.md#L36-L38)）。生产上可用它们确认「取消信号发了」与「worker 收到」是否对得上。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：按讲义规格完成两件事——(1) 修改 server.py，收到取消信号时打印日志并统计「已被取消的 token 数」（即取消生效前已生成的帧数）；(2) 让 client 提前断开（不调用 `stop_generating()`），验证 middle_server 与 server 都能正确收尾。

**操作步骤**：

1. **改造 server.py**（示例代码，基于原文件修改；建议复制为 `server_stats.py` 以免覆盖原示例）：

   ```python
   class DemoServer:
       """Simple server that generates numbers and respects cancellation"""

       async def generate(self, request, context):
           sent = 0                                  # 新增：已生成 token 计数
           try:
               for i in range(1000):
                   print(f"Server: Processing iteration {i}")
                   if context.is_stopped():
                       # 新增：取消日志 + 被取消的 token 数统计
                       print(
                           f"[CANCELLED] request={context.id()} "
                           f"sent_tokens={sent} at_iteration={i}"
                       )
                       raise asyncio.CancelledError
                   await asyncio.sleep(0.1)
                   print(f"Server: Sending iteration {i}")
                   sent += 1
                   yield i
           except asyncio.CancelledError:
               # 兜底：无论取消从哪条路径触发，都能在这里统一收尾
               print(f"[CLEANUP] generator exiting, total sent = {sent}")
               raise
   ```

   改动点：`sent` 计数；`context.id()` 打进日志（利用「子 context 继承父 id」的特性，可以在三个进程的日志里用同一个 id 串起一次请求）；`try/except asyncio.CancelledError` 统一收尾（记得 re-raise，不要吞掉取消）。

2. **改 client 观察断连路径**。复制 `client.py` 为 `client_abort.py`，把 `context.stop_generating()` 一行注释掉、只保留 `break`，模拟「客户端直接停止消费并退出」：

   ```python
   if iteration_count >= 2:
       print("Client: aborting WITHOUT stop_generating...")
       # context.stop_generating()   # 注释掉：观察断连传播
       break
   ```

3. 三终端启动（同 4.2.4 的代理模式）：`server_stats.py`、`middle_server.py`、`client.py --middle`。确认正常取消路径下 `[CANCELLED]` 和 `[CLEANUP]` 都打印、`sent_tokens` 约为 2-4。

4. 换 `client_abort.py --middle` 再跑一轮，观察断连路径。

**需要观察的现象**：

| 实验 | server 日志 | middle_server 日志 | 结论依据 |
|------|-------------|--------------------|----------|
| 正常取消 | `[CANCELLED] sent_tokens≈2-4` 后 `[CLEANUP]` | `Backend stream ended` | 优雅取消链路闭环 |
| 提前断开 | 是否出现 `[CANCELLED]`？出现在第几帧？ | 是否出现 `Backend stream ended`？ | 断连传播的时序 |

**预期结果**：正常取消路径的行为可以确定（与 4.2.4 一致，外加两条新日志）。提前断开路径的具体表现——server 是靠 `is_stopped()` 察觉还是靠流的下游断开被框架取消、middle_server 的 `Backend stream ended` 是否打印、各自在第几帧——**待本地验证**；验证时把观察到的帧序号与 `sent_tokens` 记录下来，对照 4.3.1 的两类 worker 检测场景（控制消息 vs 连接掉线）归类。

**思考题（不必写代码）**：如果把 `except asyncio.CancelledError` 里的 `raise` 去掉，会发生什么？

**参考答案**：生成器会吞掉取消异常、看似「正常完成」。上游收到的效果相当于流被提前正常关闭而非取消，框架的错误处理与指标统计可能失真；这也是处理 `asyncio.CancelledError` 的通用规范——捕获后必须重新抛出。

#### 4.3.5 小练习与答案

**练习 1**：middle_server 收到取消后，它对 server 的子请求是靠什么机制被取消的？middle_server 自己需要写任何取消处理代码吗？

**答案**：靠建链时 `create_request_context` 里的 `link_child`——middle 的 context 是 client context 的子，server 端 context 是 middle context 的子，`stop_generating()` 沿链递归。middle_server **不需要**写显式取消代码：它 `async for` 的流会因 server 停止生成而自然结束，生成器走到 `print("Backend stream ended")` 后正常返回。框架把取消变成了「普通的流结束」，这正是该设计的优雅之处。

**练习 2**：一个请求经过 client → middle → server 三跳，三个进程日志里 `context.id()` 会是同一个值吗？为什么？

**答案**：会。`create_request_context` 用 `parent_ctx.inner().id().to_string()` 创建子 context（[lib/bindings/python/rust/lib.rs:L156-L160](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/bindings/python/rust/lib.rs#L156-L160)），id 逐跳继承。这也是官方文档「`id()` 由用户设置、子请求可用同一 id 关联原始请求」的设计意图（[request-cancellation-architecture.md:L47-L49](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/docs/fern/pages/developer-guide/knowledge-base/concepts/fault-tolerance/request-cancellation-architecture.md#L47-L49)）——实践中可用它把一条请求在多个进程的日志串起来。

**练习 3**：为什么 `create_request_context` 在 `link_child` 之后还要检查一次父状态并补发 `stop_generating()`？

**答案**：处理竞态窗口。`link_child` 只保证「此后」的取消会传播；若父 context 在 `link_child` 调用前的一瞬间已被取消（例如 frontend 刚收到用户断连、而转发给 worker 的请求正在构造），没有任何机制会再触发这条新链。复查 + 立即补发把窗口关死（[lib/bindings/python/rust/lib.rs:L162-L167](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/bindings/python/rust/lib.rs#L162-L167)）。

## 5. 综合实践

把本讲内容串成一个完整任务：**给你的 echo_upper worker（u2-l1 综合实践的产物）补上完整的取消处理，并构建一条两跳链路验证它**。

1. **改造 worker**：参考 server.py 的范式，在你的 worker 生成循环里加入「先查再做」——每次迭代开头 `if context.is_stopped(): raise asyncio.CancelledError`，并用 `try/except ... raise` 在收尾处统计与打印已发送 token 数（不要吞异常）。
2. **构建两跳链路**：参考 middle_server.py 写一个代理 worker，把请求转发给你的 echo_upper worker，**必须透传 `context=context`**。
3. **写取消客户端**：参考 client.py，收到第 N 帧后 `stop_generating()`；再写一个注释掉 `stop_generating()` 的「断连版」。
4. **验收清单**（三种情况各跑一遍，整理成表格）：
   - 正常取消：三个进程日志里同一个 `context.id()` 都出现；worker 打印取消统计；代理打印流结束。
   - 断连版：记录 worker 靠哪条路径停下、第几帧停（待本地验证的结论如实记录）。
   - 反例验证：把代理里的 `context=context` 去掉，确认 worker 不再响应取消（生成满全部帧），用日志证明取消链确实断在中间层。

完成这个任务后，你就掌握了 Dynamo worker「生命周期感知」的全部基本功：状态机语义、传播机制、标准写法与反例。

## 6. 本讲小结

- `Context` 是跨进程的取消信号载体：Python 类是薄壳，状态由 Rust `Controller` 用 `tokio::sync::watch` 通道维护，三态状态机 **Live → Stopped / Killed** 单向流转，`is_stopped()` 的判定是 `!= Live`（Killed 蕴含 Stopped）。
- 取消传播 = **父子链接上的递归调用**：`stop_generating()` 先遍历 `child_context` 列表对每个子 context 递归调用，再置自身为 Stopped；中间层代理只要透传 `context=context`，框架就会通过 `link_child` 自动搭好链。
- worker 侧标准范式是「先查再做 + 抛 `asyncio.CancelledError`」：检查放在每次迭代开头、昂贵工作之前；`Context` 按参数名注入，不写 `context` 参数就收不到取消。
- `create_request_context` 有竞态兜底：建链后复查父状态，父已取消则立刻取消子，杜绝「取消发生在建链窗口」的遗漏；子 context 继承父 id，可跨进程串联一次请求的日志。
- 框架只负责「置位」，引擎负责「配合」：runtime 的取消指标只记信号到达，真正停止生成依赖 worker 自查——这也是本示例存在的意义。
- 运行提示：示例构造函数 `DistributedRuntime(loop, "file", "nats")` 的第三个参数是请求面模式（legacy NATS），按原样跑需要本地 nats-server；改为 `"tcp"`（默认值）可复用 u2-l1 的零依赖环境。

## 7. 下一步学习建议

- **下一讲 u3-l1（Runtime 与 DistributedRuntime）**：本讲把 `DistributedRuntime(loop, "file", "nats")` 当黑盒用过了，下一讲进入 Rust 侧拆解它的职责边界、`DiscoveryBackend` 与 `RequestPlaneMode` 的完整选项。
- **顺延阅读 u2-l2（PyO3 绑定）**：如果对 `Context` 薄壳如何映射 Rust 结构体意犹未尽，可回看该讲的「wrapper + inner」模式与三步定位法。
- **继续阅读源码**：`lib/runtime/src/pipeline/context.rs` 里 `StreamContext` 的实现（本讲跳过的部分）展示框架如何在 ingress 侧为每个进入的请求创建 Controller；以及 [request-cancellation-architecture.md](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/docs/fern/pages/developer-guide/knowledge-base/concepts/fault-tolerance/request-cancellation-architecture.md) 同目录下的 `graceful-shutdown-architecture.md` 与 `request-migration-architecture.md`，它们把「取消」放进更大的容错图景（优雅停机、请求迁移），与 u12-l4 的故障容忍讲义遥相呼应。
