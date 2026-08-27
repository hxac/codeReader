# PyO3 绑定：Rust 如何暴露给 Python

## 1. 本讲目标

上一讲(u2-l1)你已经用 `DistributedRuntime`、`Endpoint`、`Client` 三个 Python 对象写出了 hello world worker。但你有没有想过：这些「Python 类」的构造函数、方法到底定义在哪里？答案是——**它们根本不是 Python 代码，而是 Rust 结构体**，通过 PyO3 桥接成了一个名为 `dynamo._core` 的 C 扩展模块。

学完本讲，你应该能够：

1. 说出 `dynamo._core` 扩展模块的注册入口在哪里，`#[pymodule]`、`#[pyclass]`、`#[pymethods]` 各自做什么。
2. 追踪一个 Python 类型(如 `Context`)背后对应的 Rust 结构体，理解「wrapper 结构体 + inner 字段」这一通用模式。
3. 解释 `AsyncEngine` 在 Python 与 Rust 之间的桥接方式：一个 Python 异步生成器如何被包装成 Rust 的 `AsyncEngine` trait 实现。
4. 看懂 `lib/bindings/python/src/dynamo/` 下 Python 包装层的再导出逻辑，回答「`from dynamo.llm import make_engine` 里的 `make_engine` 到底从哪来」。

## 2. 前置知识

### 2.1 PyO3 是什么

PyO3 是一个 Rust 库，让 Rust 代码可以被 Python 解释器加载和调用。三个最核心的宏：

| 宏 | 作用 | 类比 Python |
|---|---|---|
| `#[pyclass]` | 把 Rust 结构体/枚举变成 Python 中的类 | `class Foo:` |
| `#[pymethods]` | 把 `impl` 块里的方法暴露为 Python 方法 | 类体内定义 `def` |
| `#[pymodule]` | 定义扩展模块的初始化入口(注册哪些类和函数) | 模块的 `__init__.py` |

编译产物是一个 `.so` 共享库(Python 称为扩展模块)。Python 侧 `import` 它时，解释器调用这个初始化函数，模块里的类和函数就诞生了。

### 2.2 GIL(全局解释器锁)

CPython 任意时刻只允许一个线程执行 Python 字节码，这把锁叫 GIL。Rust 代码持有 GIL 才能触碰 Python 对象。所以你会看到源码里大量 `Python::with_gil(|py| ...)`——申请锁、操作 Python 对象、释放锁。长时间持锁会卡住整个 Python 进程，这是后面 `spawn_blocking` 设计的动机。

### 2.3 async 桥接：future_into_py

Rust 用 tokio 跑异步任务，Python 用 asyncio。`pyo3_async_runtimes::tokio::future_into_py` 把一个 Rust future 转成 Python 的 awaitable,让 Python 代码可以 `await` 一个「实际在 Rust/tokio 里执行」的异步操作。你在 u2-l1 里 `await client.generate(...)` 时，底层就是它。

### 2.4 序列化三件套

Python 对象不能直接进 Rust 类型系统，需要在边界上转换：

- `pythonize::depythonize`:Python 对象 → Rust `serde` 类型(进方向)。
- `pythonize::pythonize`:Rust serde 类型 → Python 对象(出方向)。
- `rmpv::Value`:MessagePack 动态值，是 Dynamo 请求在网络上传输的负载格式(请求面编码)，Python dict 进出 Rust 时都会经过它。

### 2.5 命名空间包与 .pyi 存根

回顾 u1-l3/u1-l4:`lib/bindings/python/src/dynamo/` 目录下**没有顶层 `__init__.py`**,`components/src/dynamo/` 同样没有。这是 Python 的隐式命名空间包机制——两个 wheel(`ai-dynamo-runtime` 和 `ai-dynamo`)各自携带 `dynamo/` 下的子目录，安装后合并成同一个 `dynamo` 包。另外 `src/dynamo/_core.pyi` 是类型存根文件，它不含任何可执行代码，只为 IDE/类型检查器描述 `_core` 里类的签名(因为真实实现是编译后的 `.so`,IDE 读不到)。

## 3. 本讲源码地图

| 文件 | 角色 |
|---|---|
| `lib/bindings/python/rust/lib.rs` | 扩展模块总入口:`#[pymodule] fn _core` 注册所有类;`DistributedRuntime`/`Endpoint`/`Client`/`PyAsyncRequestStream` 的定义也在这里 |
| `lib/bindings/python/rust/context.rs` | `Context` 与 `ContextMetadata` 两个 pyclass:每请求的取消信号、trace、元数据句柄 |
| `lib/bindings/python/rust/engine.rs` | `PythonAsyncEngine`:把 Python 异步生成器桥接为 Rust `AsyncEngine` trait 实现 |
| `lib/bindings/python/rust/llm/entrypoint.rs` | `make_engine` 函数与 `EntrypointArgs`、`EngineConfig`、`EngineType` 等 pyclass 的定义处 |
| `lib/bindings/python/src/dynamo/runtime/__init__.py` | `dynamo.runtime` 包装层：再导出核心类 + `dynamo_worker`/`dynamo_endpoint` 装饰器 |
| `lib/bindings/python/src/dynamo/llm/__init__.py` | `dynamo.llm` 包装层：再导出 `make_engine`、`EntrypointArgs` 等几十个符号 |
| `lib/bindings/python/Cargo.toml` | crate `dynamo-py3` 的定义：产物名 `_core`、`cdylib`、排除出 workspace |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：**M1 `_core` 模块注册入口**、**M2 五个核心 pyclass 与 wrapper 模式**、**M3 AsyncEngine 异步桥接**、**M4 Python 包装层再导出**。

### 4.1 模块一:`_core` 扩展模块的注册入口

#### 4.1.1 概念说明

Cargo.toml 里的 `[lib]` 节决定了编译产物的身份。整个绑定 crate 叫 `dynamo-py3`,但它编译出的共享库名叫 `_core`;又因为 `src/dynamo/` 是命名空间包的一部分，最终 Python 里 `import dynamo._core` 加载的就是它。`#[pymodule]` 标注的函数 `fn _core(...)` 就是 Python import 时触发的初始化入口——名字必须与 `[lib] name` 一致。

#### 4.1.2 核心流程

一次 `import dynamo._core` 的完整流程:

```text
Python: import dynamo._core
  └─ 解释器在 dynamo 包目录找到 _core.abi3.so,调用其初始化函数
      └─ Rust: #[pymodule] fn _core(m)          (lib.rs L359-364)
          └─ register_core(m)                    (lib.rs L175)
              ├─ 初始化 Rust 日志/tracing(跳过条件:环境变量 DYNAMO_SKIP_PYTHON_LOG_INIT)
              ├─ m.add_function(...)  注册顶层函数(make_engine、register_model 等)
              ├─ m.add_class(...)     注册类(DistributedRuntime、Endpoint、...)
              ├─ engine::add_to_module(m) 等子模块各自补充注册
              └─ m.add("__version__", env!("CARGO_PKG_VERSION"))
```

注意有**两个** `#[pymodule] fn _core`,由编译 feature 二选一:`custom-policy` feature 启用时先注册自定义路由策略目录再走 `register_core`;默认(stock)构建直接 `register_core`。这解释了为什么某些私有可能力在开源 wheel 里不存在。

#### 4.1.3 源码精读

crate 身份与产物名的定义——空 `[workspace]` 节把它排除出顶层 workspace(u1-l4 讲过原因)，`crate-type` 同时要 `cdylib`(给 Python 的共享库)和 `rlib`(支持 Rust 侧 doctest):

- [lib/bindings/python/Cargo.toml:L13-L18](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/bindings/python/Cargo.toml#L13-L18) — `[lib]` 节声明 `path = "rust/lib.rs"`、`name = "_core"`、`crate-type = ["cdylib", "rlib"]`,这就是 Python 侧 `dynamo._core` 的物理来源。

所有 Rust 侧子模块在这里声明，其中 `context`、`engine`、`llm` 是本讲三个主角:

- [lib/bindings/python/rust/lib.rs:L80-L91](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/bindings/python/rust/lib.rs#L80-L91) — 声明 `mod context;`、`mod engine;`、`mod llm;` 等子模块，PyO3 类分散在这些模块中定义，再集中到 `lib.rs` 注册。

两个初始化入口，按 feature 切换:

- [lib/bindings/python/rust/lib.rs:L359-L364](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/bindings/python/rust/lib.rs#L359-L364) — 默认构建的 `#[pymodule] fn _core`,函数体只有一行 `register_core(m)`,这是 Python `import dynamo._core` 时最先执行的 Rust 代码。

`register_core` 是注册总表，节选关键几行:

- [lib/bindings/python/rust/lib.rs:L193](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/bindings/python/rust/lib.rs#L193) — 把 `llm::entrypoint::make_engine` 注册为模块级函数。
- [lib/bindings/python/rust/lib.rs:L200-L210](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/bindings/python/rust/lib.rs#L200-L210) — 依次注册 `DistributedRuntime`、`Endpoint`、`Client`、`PyAsyncRequestStream`、`EntrypointArgs` 等类；每一行 `m.add_class::<T>()` 对应 Python 里一个可以直接 `isinstance` 的类型。
- [lib/bindings/python/rust/lib.rs:L243](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/bindings/python/rust/lib.rs#L243) — 注册 `context::Context`(定义在 context.rs,4.2 节精读)。

`engine.rs` 有自己独立的补充注册函数，在 `register_core` 末尾被调用:

- [lib/bindings/python/rust/lib.rs:L261](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/bindings/python/rust/lib.rs#L261) — `engine::add_to_module(m)` 把 `PythonAsyncEngine` 类挂进模块(实现见 4.3.3)。

#### 4.1.4 代码实践

**实践目标**：用 Python 内省验证「注册表」与源码一致，建立「源码 ↔ 运行时」的对应感。

**操作步骤**(需按 u1-l4 构建过本地环境;`maturin develop` 产物在 venv 中):

```bash
source .venv/bin/activate
python3 - <<'EOF'
import dynamo._core as c
names = [n for n in dir(c) if not n.startswith("_") or n == "__version__"]
print("version:", c.__version__)
for target in ["DistributedRuntime", "Endpoint", "Client",
               "Context", "PyAsyncRequestStream", "EntrypointArgs"]:
    print(f"{target:24s}", "OK" if target in dir(c) else "MISSING")
print(type(c.DistributedRuntime))   # <class 'type'> —— 它就是一个真正的 Python 类
EOF
```

**需要观察的现象**：所有目标类都打印 OK;`type(...)` 是 `<class 'type'>`,说明这些是原生 Python 层面的类型对象(由 Rust 构造)，而不是某种代理。

**预期结果**:`__version__` 打印 `1.5.0` 之类的版本号(对应 Cargo.toml 中 `dynamo-py3` 的 `version`,由 `env!("CARGO_PKG_VERSION")` 编译期注入)。若尚未本地构建，可改用 `pip show ai-dynamo-runtime` 确认已安装版本后 `python3 -c "import dynamo._core; print(dynamo._core.__version__)"` 验证 PyPI 版本，其余结论同样成立。

#### 4.1.5 小练习与答案

**练习 1**:`dynamo._core` 里注册的 `__version__` 来自哪里？为什么它总是和 `ai-dynamo-runtime` 的 wheel 版本对齐？

**答案**：来自 lib.rs 中 `m.add("__version__", env!("CARGO_PKG_VERSION"))`,`env!` 在编译期读取 crate `dynamo-py3` 的 `Cargo.toml` 版本；而 u1-l3 讲过，这个绑定 crate 打包进 `ai-dynamo-runtime` wheel,且仓库中四处版本号(hatch_build.py、pyproject、Cargo)刻意对齐，所以两者一致。

**练习 2**：为什么存在两个 `#[pymodule] fn _core` 定义却不会冲突？

**答案**：它们分别被 `#[cfg(feature = "custom-policy")]` 和 `#[cfg(not(feature = "custom-policy"))]` 条件编译门控(见 lib.rs L352-364 附近的 cfg 属性)，同一次构建只有一个版本参与编译。

### 4.2 模块二：核心 pyclass 与「wrapper + inner」模式

#### 4.2.1 概念说明

观察 `DistributedRuntime`、`Endpoint`、`Client` 的定义会发现一个统一模式:PyO3 类只是一个**薄壳(wrapper)**,真正逻辑在 `inner` 字段里对 Rust 运行时 crate 的原生结构体的持有。这样做的原因:运行时类型(如 `rs::DistributedRuntime`)定义在 `dynamo-runtime` crate 中，不能也不需要改成 pyclass;绑定层只负责「暴露」，不负责「实现」。另有一个高频伴随字段 `event_loop: PyObject`——保存 Python 的 asyncio 事件循环引用，以便 Rust 侧在任何线程重新进入该循环执行 Python 回调。

#### 4.2.2 核心流程

以 `DistributedRuntime` 为例，Python 侧 `DistributedRuntime(loop, "file", "tcp")` 的构造链:

```text
Python: DistributedRuntime(event_loop, discovery_backend, request_plane)
  └─ #[new] (lib.rs L1156-1164)
      ├─ 解析 discovery_backend 字符串 → DiscoveryBackend(Kubernetes 或 KvStore(etcd/file/mem...))
      ├─ 解析 request_plane → RequestPlaneMode
      ├─ 复用进程内已有 tokio Runtime,没有则 Worker::from_settings() 新建
      │    └─ 首次初始化 pyo3_async_runtimes 的静态 runtime(每进程一次,OnceCell)
      ├─ 组装 DistributedConfig → rs::DistributedRuntime::new(...)
      └─ 返回 wrapper: DistributedRuntime { inner, event_loop }
```

后续任何方法调用(如 `runtime.namespace(...)`)都是：Python 调用 → PyO3 进入 `#[pymethods]` 块 → 操作 `self.inner`(纯 Rust 世界)→ 结果再包装成 pyclass 返回给 Python。

#### 4.2.3 源码精读

三个核心类的 wrapper 定义:

- [lib/bindings/python/rust/lib.rs:L854-L859](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/bindings/python/rust/lib.rs#L854-L859) — `DistributedRuntime` 结构体:两个字段 `inner: rs::DistributedRuntime` 与 `event_loop: PyObject`。`rs` 是 `dynamo_runtime` 的别名(见 lib.rs L31-39 的 use)。
- [lib/bindings/python/rust/lib.rs:L874-L879](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/bindings/python/rust/lib.rs#L874-L879) — `Endpoint` 同样是 `inner + event_loop` 的薄壳，inner 指向运行时的 `rs::component::Endpoint`。
- [lib/bindings/python/rust/lib.rs:L901-L906](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/bindings/python/rust/lib.rs#L901-L906) — `Client` 的 inner 是一个 `PushRouter<rmpv::Value, RsAnnotated<rmpv::Value>>`,印证 u2-l1 所见:客户端请求经 MessagePack 编码、按路由模式分发。

`#[new]` 构造函数——注意它同时演示了「同步签名 + 返回 Self」与异步初始化的混合:tokio 运行时复用逻辑保证一个进程多个 `DistributedRuntime` 实例共享同一个 tokio runtime:

- [lib/bindings/python/rust/lib.rs:L1156-L1235](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/bindings/python/rust/lib.rs#L1156-L1235) — `#[new]` 解析两个字符串参数为 Rust 枚举，然后 `runtime_from_existing().or_else(...)` 复用或创建 tokio Runtime,最后 `block_on(rs::DistributedRuntime::new(...))` 同步完成初始化并返回 wrapper。

`Context` 是「每请求句柄」，也遵循同一模式，但 inner 是 trait 对象 `Arc<dyn AsyncEngineContext>`——因为请求可能来自网络或进程内，具体实现不同，共同点是都能取消、报 id、携带元数据:

- [lib/bindings/python/rust/context.rs:L59-L70](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/bindings/python/rust/context.rs#L59-L70) — `Context` 持有 `inner: Arc<dyn AsyncEngineContext>`(取消与 id)、`trace_context`(分布式追踪)、`first_token`(分离式服务首 token 信号)、`metadata`(键值元数据)与 `span`(observability 用 tracing span)。

`PyAsyncRequestStream` 是双向流端点的入站迭代器，结构上就是「一个 tokio mpsc 接收器的壳」:

- [lib/bindings/python/rust/lib.rs:L2074-L2085](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/bindings/python/rust/lib.rs#L2074-L2085) — 字段只有一个 `rx: Arc<Mutex<mpsc::Receiver<PyObject>>>`;它的 `__anext__`(L2098-2107)每次 `recv().await` 一帧，通道关闭时抛 `StopAsyncIteration`——用 Rust channel 完整模拟了 Python 异步迭代器协议。

#### 4.2.4 代码实践

**实践目标**：亲手验证「Python 对象 = Rust 结构体」，并学会用 `help()` 读 PyO3 生成的签名。

**操作步骤**:

```bash
python3 - <<'EOF'
from dynamo._core import DistributedRuntime, Context
import inspect
# PyO3 会为每个 pyclass 生成 docstring 与 text_signature
print(DistributedRuntime.__doc__)
print(Context.__doc__)
# 尝试错误构造,观察 Rust 侧抛回的 Python 异常
try:
    DistributedRuntime(None, "不存在的后端", "tcp")
except Exception as e:
    print(type(e).__name__, ":", e)
EOF
```

**需要观察的现象**：docstring 能打印出构造参数说明；传入非法 discovery_backend 时得到 `ValueError`(或解析失败对应的其他异常)，错误消息由 Rust 的 `to_pyerr` 包装而来。

**预期结果**：Rust 侧任何 `Err` 都经由 `to_pyerr`(lib.rs L366-371,把 `Display` 错误格式化成 `PyException`)变成 Python 异常——这是所有绑定方法的统一错误出口。具体报错文案「待本地验证」(取决于 `kv::Selector` 解析逻辑的报错格式)。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Endpoint` wrapper 要保存 `event_loop` 而 `Client` 不用？

**答案**:`Endpoint.serve_endpoint()` 之后，Rust 网络线程收到请求时需要回调 Python 生成器，必须知道「回到哪个事件循环」执行;`Client` 的调用方向是 Python 主动发起，`future_into_py` 已经把 Rust future 挂到了当前循环上，无需额外保存。

**练习 2**:`Client.generate()` 返回的 `AsyncResponseStream` 是怎么把 Rust 流变成 Python 可 `async for` 的对象的？

**答案**：Rust 侧先 `tokio::spawn(process_stream(...))` 把响应流灌进一个 mpsc 通道(lib.rs L1972-1999);`AsyncResponseStream.__anext__`(lib.rs L2027-2060)每次 await 通道取一帧，通道耗尽时抛 `PyStopAsyncIteration`,于是 Python 的 `async for` 协议自然成立。

### 4.3 模块三：AsyncEngine 桥接——Python 生成器如何变成 Rust 引擎

#### 4.3.1 概念说明

Dynamo 的 Rust 运行时里，「引擎」是一个 trait:`AsyncEngine<Req, Resp>`,核心方法是 `async fn generate(&self, request) -> Result<Resp>`。网络 ingress 只认这个 trait。但用户(以及 vLLM/SGLang 后端)写的引擎是 **Python 异步生成器函数**。`engine.rs` 的职责就是造一座桥:`PythonAsyncEngine`——一个 pyclass,同时(作为 Rust 类型)实现 `AsyncEngine` trait;它收到 Rust 请求后，调用手中的 Python 生成器，再把 Python 逐帧 yield 的结果转回 Rust 流。

桥上要解决四个问题：

1. **值转换**：Rust 请求 → Python 对象(pythonize),Python 响应 → Rust 类型(depythonize)。
2. **GIL 安全**：Python 调用可能长时间阻塞，不能卡住 tokio reactor → 放进 `spawn_blocking`。
3. **迭代协议**：Python 异步生成器靠 `__anext__` 驱动 → 包装成 Rust `Stream`,且**按需拉取**(消费者要一帧才 poll 一次，防止生成器复用可变对象造成数据竞争)。
4. **上下文注入**：Rust 侧的取消信号要传给 Python → 构造 `Context` pyclass 作为关键字参数注入，前提是 Python 函数签名声明了 `context` 参数(启动时探测一次)。

#### 4.3.2 核心流程

一条请求穿过 `PythonAsyncEngine` 的完整路径(对应 u2-l1 的 hello world):

```text
Rust ingress 收到请求(SingleIn<Req>)
  └─ PythonAsyncEngine::generate                     (engine.rs L231)
      └─ generate_python_stream                       (engine.rs L291)
          ├─ request.transfer(()) → 拆出 (请求体, 上下文 ctx)
          ├─ invoke_generator(...)                    (engine.rs L74)
          │    ├─ spawn_blocking + with_gil:
          │    │    ├─ pythonize(请求) → Python 对象
          │    │    ├─ 若生成器签名有 context:Py::new(Context::new(ctx, trace, None, metadata))
          │    │    ├─ generator.call(py, (input,), kwargs={"context": ...})
          │    │    └─ demand_driven_python_stream → Stream<Item=PyResult<PyObject>>
          │    └─ (返回流,每帧按需 poll __anext__)
          └─ forward_responses(stream, ctx, id)
               └─ 每帧 depythonize → Annotated<Resp> → ResponseStream 返回 Rust 世界
Python 侧看到的样子: async def generate(request, context): ... yield token
```

#### 4.3.3 源码精读

`PythonAsyncEngine` 的定义与构造——注意它保存的是「生成器函数对象 + 事件循环」，并**在构造时探测一次** Python 函数是否接受 `context` 关键字:

- [lib/bindings/python/rust/engine.rs:L168-L193](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/bindings/python/rust/engine.rs#L168-L193) — `#[pyclass] struct PythonAsyncEngine(PythonServerStreamingEngine)`;`#[new]` 接收 Python 生成器与事件循环，内部创建 `CancellationToken` 并委托给 `PythonServerStreamingEngine::new`(后者在 L244-259 调用 `detect_has_context` 完成签名探测)。

trait 实现——泛型参数 `Req: Serialize, Resp: Deserialize` 表明任何可序列化类型都能过桥:

- [lib/bindings/python/rust/engine.rs:L225-L234](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/bindings/python/rust/engine.rs#L225-L234) — `impl AsyncEngine<SingleIn<Req>, ManyOut<Annotated<Resp>>> for PythonAsyncEngine`,`generate` 一行委托给内部的 `PythonServerStreamingEngine`。

桥的核心 `generate_python_stream`——请求拆包、Context 构造、生成器调用、响应流组装都在这:

- [lib/bindings/python/rust/engine.rs:L291-L327](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/bindings/python/rust/engine.rs#L291-L327) — 关键在 L304-322:`request.transfer(())` 把请求与上下文分离;L315-322 的 `engine.has_context.then_some(...)` 只有当签名探测通过时才构造 `Context::new(ctx, current_trace_context, None, metadata)` 并以 `context=` 关键字传入——这正是你在 u2-l1 中「声明 `context` 参数即可拿到取消句柄」的机制出处。

GIL 安全的调用入口:

- [lib/bindings/python/rust/engine.rs:L74-L113](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/bindings/python/rust/engine.rs#L74-L113) — `invoke_generator` 用 `tokio::task::spawn_blocking` + `Python::with_gil` 执行 Python 调用，注释明确说明：GIL 争用时可能无限阻塞，不能在 tokio reactor 线程上直接拿锁。

Python 异步迭代器 → Rust Stream:

- [lib/bindings/python/rust/engine.rs:L115-L139](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/bindings/python/rust/engine.rs#L115-L139) — `demand_driven_python_stream` 用 `futures::stream::unfold` 反复调用 `__anext__`,拿到 `PyStopAsyncIteration` 时返回 `None` 结束流——这是「按需拉取」语义的实现：无人 poll 就不会推进生成器。

它在 `_core` 模块的注册(回到 4.1 的注册总表):

- [lib/bindings/python/rust/engine.rs:L36-L40](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/bindings/python/rust/engine.rs#L36-L40) — `add_to_module` 只注册 `PythonAsyncEngine` 一个类，由 lib.rs L261 调用。

补充：Python 侧也能直接构造它(文档注释 L149-167 给出了用法示例)，而 u2-l1 里你走的是更高层的 `endpoint.serve_endpoint(generator)`——后者在 lib.rs L1431-1552 内部就是 `engine::PythonAsyncEngine::new(generator, event_loop)` 再包上 ingress,殊途同归。

#### 4.3.4 代码实践

**实践目标**：通过「去掉/加上 `context` 参数」观察签名探测的实际效果，验证桥的注入逻辑。

**操作步骤**(基于 u2-l1 的 hello world 目录，拷贝两份 worker 做对照):

```bash
cd examples/custom_backend/hello_world
# 1) 对照版:把 hello_world.py 复制为 hello_nc.py,
#    将端点函数签名从 async def generate(request, context) 改成 async def generate(request)
#    (函数体内删除对 context 的使用)
# 2) 分别启动原版与对照版(参照该目录 README / u2-l1 的环境变量)
# 3) 两个版本都用 client.py 发送请求,并中途 Ctrl-C 断开 client
```

**需要观察的现象**：两版都能正常收到全部 token(桥对有无 context 都兼容)；差异在取消路径——原版 worker 日志里 context 相关的停止日志会触发，对照版没有该信号可用。

**预期结果**：证实 `detect_has_context`(engine.rs L49-54)在引擎构造期就决定了调用形态:有 `context` 参数走 `generator.call(py, (input,), kwargs)`,没有则退回 `generator.call1(py, (input,))`(engine.rs L92-103 的 match 分支)。日志具体文案「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么响应流要「按需拉取」而不是把生成器一口气跑完缓冲起来？

**答案**：engine.rs L561-564 的注释给了直接原因——Python 生成器可能复用并改写同一个 dict/list 对象，缓冲裸 `PyObject` 句柄会让早先的帧观察到后续帧的修改；同时按需拉取天然形成背压(下游编码完一帧才取下一帧)。

**练习 2**：Python 生成器抛出的异常如何回到 Rust 并最终到达客户端？

**答案**：经 `map_python_exception`(engine.rs L381-440)把 `PyErr` 映射为带 `ErrorType::Backend(...)` 的 `DynamoError`——`ValueError/TypeError` 归为 `InvalidArgument`、`CancelledError` 归为 `Cancelled` 等；随后由响应转发任务(engine.rs L458-541)包成 `Annotated` 错误帧发给客户端，而不是让流悄悄截断。

### 4.4 模块四:`dynamo.runtime` / `dynamo.llm` 包装层的再导出

#### 4.4.1 概念说明

`dynamo._core` 是「机器友好」的一锅端模块：几十个类与函数平铺。Python 包装层 `src/dynamo/runtime/__init__.py` 与 `src/dynamo/llm/__init__.py` 做两件事：**再导出**(按领域分组给稳定入口)与**增值**(加上纯 Python 才方便实现的装饰器、Protocol、别名)。你在 u2-l1 用的 `dynamo_worker` 装饰器就是包装层新增的，Rust 里根本没有对应物。

#### 4.4.2 核心流程

一次 `from dynamo.llm import make_engine` 的符号解析链:

```text
from dynamo.llm import make_engine
  └─ dynamo.llm/__init__.py L49: from dynamo._core import make_engine
      └─ dynamo._core = cdylib _core(.abi3).so
          └─ 注册自 lib.rs L193: wrap_pyfunction!(llm::entrypoint::make_engine)
              └─ 定义于 rust/llm/entrypoint.rs L727: #[pyfunction] pub fn make_engine
```

`EntrypointArgs` 同理:`llm/__init__.py` L12 再导出 ← lib.rs L210 `add_class` ← 定义于 `rust/llm/entrypoint.rs` L560-562。**结论：给 `dynamo.llm` 的 LLM 域符号，其 Rust 定义不在 `rust/llm/__init__.py`(不存在这个文件)，而在 `rust/llm/` 下的各专题模块，`entrypoint.rs` 是其中承载引擎装配的一支。**

#### 4.4.3 源码精读

`dynamo.llm` 的再导出全表(节选关键行):

- [lib/bindings/python/src/dynamo/llm/__init__.py:L10-L56](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/bindings/python/src/dynamo/llm/__init__.py#L10-L56) — 每行一个 `from dynamo._core import X as X` 的再导出，覆盖 `HttpService`、`KvRouter`、`RouterConfig`、`PythonAsyncEngine` 等几十个符号;`as X` 的冗余写法是为了让 lint 识别重导出。
- [lib/bindings/python/src/dynamo/llm/__init__.py:L49](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/bindings/python/src/dynamo/llm/__init__.py#L49) — `from dynamo._core import make_engine`:frontend 的 main.py 最终 `await make_engine(distributed_runtime, args)`(u5-l1 会用到)就是从这个入口拿到的函数。
- [lib/bindings/python/src/dynamo/llm/__init__.py:L73-L75](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/bindings/python/src/dynamo/llm/__init__.py#L73-L75) — 向后兼容别名 `fetch_llm = fetch_model` 等：历史上这些函数叫 `register_llm`,旧后端代码仍可运行——包装层承担 API 演进的缓冲职责。

`dynamo.runtime` 的再导出与增值装饰器:

- [lib/bindings/python/src/dynamo/runtime/__init__.py:L14-L18](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/bindings/python/src/dynamo/runtime/__init__.py#L14-L18) — 只再导出五个领域无关原语:`Client`、`Context`、`DistributedRuntime`、`Endpoint`、`PyAsyncRequestStream`,与 `dynamo.llm` 的几十个 LLM 域符号形成分层对照。
- [lib/bindings/python/src/dynamo/runtime/__init__.py:L21-L51](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/bindings/python/src/dynamo/runtime/__init__.py#L21-L51) — `dynamo_worker` 装饰器:读 `DYN_REQUEST_PLANE`(默认 tcp)与 `DYN_DISCOVERY_BACKEND`(默认 etcd)两个环境变量,构造 `DistributedRuntime` 后注入给被装饰函数——u2-l1 里「worker 函数第一个参数凭空出现 runtime」的魔法全在这 30 行纯 Python。

`make_engine` / `EntrypointArgs` 的 Rust 定义处(本讲实践的靶心):

- [lib/bindings/python/rust/llm/entrypoint.rs:L725-L731](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/bindings/python/rust/llm/entrypoint.rs#L725-L731) — `#[pyfunction] pub fn make_engine(py, distributed_runtime, args: EntrypointArgs)`:用 `LocalModelBuilder` 收集模型名/端口/路由配置等，在 `future_into_py` 中异步构建 `LocalModel`,再经 `select_engine`(L850)装配出 `EngineConfig`。
- [lib/bindings/python/rust/llm/entrypoint.rs:L560-L562](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/bindings/python/rust/llm/entrypoint.rs#L560-L562) — `#[pyclass] pub(crate) struct EntrypointArgs`:frontend 侧 CLI 参数的 Rust 容器，字段含 model_path、engine_type、router_config、chat_engine_factory 等(其 `#[new]` 构造器在 L591 起)。
- [lib/bindings/python/rust/llm/entrypoint.rs:L66-L73](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/bindings/python/rust/llm/entrypoint.rs#L66-L73) — `#[pyclass] enum EngineType { Echo, Dynamic, Mocker }`:`Dynamic` 即 u4-l1 将讲的「Python 侧注入引擎」模式。

#### 4.4.4 代码实践(本讲主实践)

**实践目标**：亲手完成「Python 符号 → Rust 定义」的反向定位，产出一份导出路径笔记(这是阅读任何 `_core` 符号的通用方法)。

**操作步骤**:

1. 在 `lib/bindings/python/rust/lib.rs` 中搜索 `#[pyclass]`,确认:
   - `DistributedRuntime` 定义于 [L854](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/bindings/python/rust/lib.rs#L854)(注册于 L200);
   - `Endpoint` 定义于 [L874](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/bindings/python/rust/lib.rs#L874)(注册于 L202)。
2. 在 `lib.rs` 的 `register_core` 中找到 `make_engine` 与 `EntrypointArgs` 的注册行(L193、L210),记下注册时引用的 Rust 路径 `llm::entrypoint::make_engine` 与 `llm::entrypoint::EntrypointArgs`。
3. 打开 `rust/llm/entrypoint.rs`,用编辑器跳转确认 `pub fn make_engine`(L727)与 `pub(crate) struct EntrypointArgs`(L562)。
4. 把以下链路写成笔记(可直接抄改):

```markdown
## make_engine / EntrypointArgs 导出路径笔记

Python 入口:  dynamo.llm.__init__ L49  from dynamo._core import make_engine
                                              L12  from dynamo._core import EntrypointArgs
扩展模块:      dynamo._core  (= dynamo-py3 crate 的 cdylib, Cargo.toml [lib] name = "_core")
注册行:        rust/lib.rs L193  add_function(llm::entrypoint::make_engine)
               rust/lib.rs L210  add_class(llm::entrypoint::EntrypointArgs)
Rust 定义:     rust/llm/entrypoint.rs L727  #[pyfunction] pub fn make_engine
               rust/llm/entrypoint.rs L560-562  #[pyclass] struct EntrypointArgs
结论: 它们从绑定 crate 的 llm::entrypoint 子模块导出, 经 register_core 挂进 _core,
      再由 Python 侧 dynamo/llm/__init__.py 再导出给用户。
```

5. 用同样的三步法(包装层 import 行 → lib.rs 注册行 → 定义文件)再追踪一个自选符号，建议选 `HttpService`(答案:`http.rs`,注册于 lib.rs L241)。

**需要观察的现象**：每个符号都能在三层各找到一行「落点」，三层文件路径与行号一一对应，没有断链。

**预期结果**：得到一张可复用的「三层定位表」。以后在 Python 代码里遇到任何不认识的 `dynamo._core` 符号，`grep "add_class::<\|add_function(" rust/lib.rs` 即可反查定义文件。

#### 4.4.5 小练习与答案

**练习 1**:`dynamo.runtime` 和 `dynamo.llm` 两个包装层再导出的符号数量差异巨大，这个分层边界在哪里？

**答案**:`dynamo.runtime` 只导出五个领域无关的分布式原语(Client/Context/DistributedRuntime/Endpoint/PyAsyncRequestStream)加两个装饰器;`dynamo.llm` 导出 LLM 域的几十个符号(HttpService、KvRouter、make_engine 等)。对应 u2-l1 的结论：runtime 是通用原语层，llm 是其上的 LLM 高层封装。

**练习 2**:`register_model` 在 `dynamo.llm` 里的别名是 `register_llm`,这个别名是 Rust 侧实现的还是 Python 侧实现的？为什么这么设计？

**答案**：Python 侧——llm/__init__.py L74-75 直接赋值 `register_llm = register_model`。放在包装层做兼容，Rust 侧无需为旧名字重复注册，也让后续只改 Python 文件就能演进 API,不必重编 `.so`。

**练习 3**：如果你要给 `_core` 新增一个 pyclass 并让用户从 `dynamo.llm` 用到它，至少要改几处？

**答案**：三处——① 在 `rust/` 下某模块定义 `#[pyclass]` 结构体与 `#[pymethods]`;② 在 `rust/lib.rs` 的 `register_core` 里 `m.add_class::<T>()`;③ 在 `src/dynamo/llm/__init__.py` 加 `from dynamo._core import T as T`。(可选第四处：更新 `src/dynamo/_core.pyi` 存根，否则 IDE 无类型提示。)

## 5. 综合实践

**任务：制作你自己的 `_core` 导出地图(Export Map)。**

目标是把本讲三个模块(注册入口、wrapper 模式、包装层再导出)串成一个可复用的查阅工具：

1. **收集**：写一个小脚本，`import dynamo._core` 后打印 `dir(dynamo._core)` 中全部公共类与函数名，保存为 `core_symbols.txt`。
2. **反查**：对其中 10 个符号(必含 `DistributedRuntime`、`Endpoint`、`Client`、`Context`、`PyAsyncRequestStream`、`PythonAsyncEngine`、`make_engine`、`EntrypointArgs`、`HttpService`、`KvRouter`),用 4.4.4 的三步法定位到 Rust 定义文件与行号，整理成三列表格：Python 符号 | lib.rs 注册行 | Rust 定义位置。
3. **验证**：对照 `src/dynamo/_core.pyi`,确认这 10 个符号都有存根声明；任选 2 个比较 `.pyi` 中的签名与 Rust `#[pyo3(signature = ...)]` 是否一致。
4. **标注分层**：给表格加第四列，标注该符号由 `dynamo.runtime` 还是 `dynamo.llm` 再导出(或两者都未导出、仅 `_core` 内部使用)。

产出物：一张表 + 一段 200 字总结，说明「Python 看到的 Dynamo API 面积」与「Rust 实现面积」的映射关系。这张表在你后续阅读 u3(Rust 运行时)、u4(make_engine 装配)时都是现成的索引入口。

## 6. 本讲小结

- `dynamo._core` 是 PyO3 扩展模块，crate 名 `dynamo-py3`,`#[pymodule] fn _core` + `register_core`(rust/lib.rs L175)是全部符号的唯一注册总表。
- 所有暴露给 Python 的类都遵循「wrapper + inner」模式:`inner` 持有 Rust 运行时原生结构体(或 trait 对象如 `Arc<dyn AsyncEngineContext>`),需要回调 Python 的类额外保存 `event_loop`。
- `PythonAsyncEngine`(engine.rs)是异步之桥：Rust `AsyncEngine` trait 的实现，内部经 `spawn_blocking + with_gil` 调用 Python 生成器，`__anext__` 被包装成按需拉取的 Rust `Stream`,Context 以关键字参数按签名探测结果注入。
- `dynamo.runtime` / `dynamo.llm` 是纯 Python 再导出层：前者管五个分布式原语加 `dynamo_worker` 装饰器，后者管几十个 LLM 域符号;`make_engine`、`EntrypointArgs` 的 Rust 定义在 `rust/llm/entrypoint.rs`,经 lib.rs L193/L210 注册进 `_core`。
- 定位任何 `_core` 符号的三步法：包装层 import 行 → lib.rs 注册行 → Rust 定义文件；错误统一经 `to_pyerr` 变成 Python 异常。

## 7. 下一步学习建议

本讲你已看清 Python↔Rust 的边界。接下来两条路：

1. **向 Rust 深处(推荐先走)**：u3-l1「Runtime 与 DistributedRuntime」——穿过 `inner: rs::DistributedRuntime` 这扇门，看 `lib/runtime/src/runtime.rs` 与 `distributed.rs` 里 tokio Runtime、服务发现后端的真实实现；建议先读 `lib/runtime/src/distributed.rs` 中 `DistributedRuntime::new`,它正是本讲 L1229-1232 `block_on` 调用的目标。
2. **向装配层走**：u4-l1「entrypoint 与 EngineConfig」——本讲只追到 `make_engine` 的定义处，下一讲拆开它内部的 `select_engine`(entrypoint.rs L850)与 `EngineType::Dynamic` 如何把 Python 引擎工厂注入 Rust 拓扑。

若想巩固本讲，可再读 `lib/bindings/python/rust/push_egress.rs`(推送式响应的另一条桥)与 `src/dynamo/_core.pyi`(完整 API 存根)，对比两者与 `engine.rs` 拉取式路径的差异。
