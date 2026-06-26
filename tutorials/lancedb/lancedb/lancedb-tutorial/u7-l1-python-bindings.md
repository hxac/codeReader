# Python 绑定：PyO3 与同步/异步

## 1. 本讲目标

本讲是「多语言绑定」单元的第一篇。前面几讲（u2 连接与表、u3 查询）讲的都是 **Rust 核心**里的抽象：`Connection`、`Table`/`BaseTable`、`QueryBase`、`VectorQuery`。本讲要回答一个关键问题：

> 这些 Rust 类型，是怎么变成 Python 里能 `import lancedb` 直接用的 `AsyncTable`、`Table` 的？

学完本讲，你应该能够：

- 说清「Rust 核心 + PyO3 薄绑定 + Python 封装层」三层各自的职责。
- 看懂 `python/src/*.rs` 里的 `#[pyclass]`、`#[pymethods]`、`#[pyfunction]` 和 `future_into_py` 在做什么。
- 理解 Python 端为什么有 **两套** API：`AsyncTable`（异步）和 `LanceTable`（同步），以及它们如何通过一个后台事件循环 `LOOP` 互相桥接。
- 知道 `fork()` 安全这件事在绑定层是怎么被处理的。
- 看懂「命名空间（namespace）连接」这条进阶路径：为什么 `rest` 实现要在 Rust 里 **原生构建** 命名空间客户端、安装 read-freshness provider，从而让 `QueryTable` 下推正确路由到 Rust 表（透出 `x-lancedb-min-timestamp` 新鲜度头）。
- 写出同一个「连接 → 建表 → 向量搜索」任务的同步版与异步版脚本。

## 2. 前置知识

在进入源码前，先建立几个直觉。如果你已经很熟悉，可以跳过。

### 2.1 什么是 PyO3

PyO3 是一个让 **Rust 和 Python 互相调用** 的库。它做三件事：

1. 用 `#[pyclass]` 把一个 Rust `struct` 注册成一个 Python 类。
2. 用 `#[pymethods]` 把 `impl` 块里的 Rust 方法变成 Python 方法。
3. 用 `#[pyfunction]` 把一个 Rust 函数变成 Python 函数。

最终这些类和函数被一个 `#[pymodule]` 标注的初始化函数打包成一个 **原生扩展模块**（一个 `.so` / `.pyd` 文件），Python 用 `import` 就能加载它。LanceDB 的这个模块名字叫 `_lancedb`。

> 术语：**原生扩展（native extension）** 指用 C/Rust 编译成机器码、被 Python 解释器直接加载的模块，区别于纯 `.py` 文件。它运行速度接近原生，但必须为每个平台单独编译。

### 2.2 Rust 的 async 和 Python 的 async 不是一回事

- **Rust async**：`async fn` 返回一个 `Future`，必须由一个 **运行时（runtime）**，通常是 `tokio`，去轮询（poll）它才会真正执行。Rust 的 future 是「惰性」的——你不 `await`/`spawn` 它，它一行都不会跑。
- **Python async**：`async def` 返回一个 **协程（coroutine）**，必须放进一个 **事件循环（event loop）** 里 `await` 才会执行。

绑定层要解决的核心难题就是：**怎么让 Rust 的 Future 和 Python 的 coroutine 对接起来**。LanceDB 的答案是：Rust 侧用一个 fork-safe 的 tokio 运行时跑 Future，再把它包装成一个 Python 可 `await` 的对象。

### 2.3 为什么需要两套 API（同步 + 异步）

LanceDB 的所有 IO 操作（建表、查询、删除…）在 Rust 核心里都是 `async` 的，因为它们要并发地读对象存储、跑 ANN 搜索。但 Python 用户有两类需求：

- 写脚本、做实验时，希望用最简单的 **同步** 写法（`table.search(...)`，一行出结果）。
- 写 Web 服务时，希望用 **异步** 写法（`await table.vector_search(...)`），不阻塞事件循环。

LanceDB 的设计是：**异步是「正根」，同步是对异步的薄包装**。二者背后调用的是同一套 Rust 核心，行为完全一致。本讲会反复回到这一点。

### 2.4 命名空间连接与 read-freshness（本讲新增）

承接 u6-l2，LanceDB 除了最朴素的 `db://`（远程）和本地路径连接外，还有一条「命名空间（namespace）」连接：表的位置由命名空间服务端分配，支持多级命名空间。命名空间连接在 Python 里由 `lancedb.connect_namespace(...)` 发起。

这条路径有一个微妙的取舍：**把查询下推到服务端（`QueryTable` pushdown）能省带宽，但绕过了 Rust 的 read-freshness（读新鲜度）机制**——后者负责在请求头里写入 `x-lancedb-min-timestamp`，保证「读得到自己的写」。本讲 4.6 节会专门讲这个 bug 的修复：让 `rest` 实现的命名空间客户端在 Rust 里原生构建、装上 read-freshness provider，并把 `QueryTable` 下推路由回 Rust 表。

## 3. 本讲源码地图

本讲涉及的关键文件，分属两层：

| 文件 | 所属层 | 作用 |
|------|--------|------|
| `python/Cargo.toml` | Rust 绑定 | 声明 `_lancedb` 这个 cdylib crate 及其 feature |
| `python/src/lib.rs` | Rust 绑定 | `#[pymodule]` 注册所有导出的类与函数 |
| `python/src/connection.rs` | Rust 绑定 | `Connection` pyclass、`connect` / `connect_namespace_client` pyfunction |
| `python/src/table.rs` | Rust 绑定 | `Table` pyclass 及其大量 `#[pymethods]` |
| `python/src/query.rs` | Rust 绑定 | `Query` / `VectorQuery` / `FTSQuery` / `HybridQuery` pyclass |
| `python/src/runtime.rs` | Rust 绑定 | fork-safe 的 tokio 运行时、`future_into_py` 与 `block_on` |
| `python/python/lancedb/__init__.py` | Python 封装 | 顶层 `connect`（同步）与 `connect_async`（异步）入口 |
| `python/python/lancedb/db.py` | Python 封装 | `AsyncConnection` / `LanceDBConnection` |
| `python/python/lancedb/table.py` | Python 封装 | `AsyncTable` 与 `Table`/`LanceTable` |
| `python/python/lancedb/namespace.py` | Python 封装 | 命名空间连接 `LanceNamespaceDBConnection` / `AsyncLanceNamespaceDBConnection`（4.6 节主角） |
| `python/python/lancedb/background_loop.py` | Python 封装 | 后台事件循环 `LOOP`（同步包装异步的关键） |

记忆口诀：**左边是 Rust 的「骨架」，右边是 Python 的「血肉」**。骨架负责把 Rust 类型暴露出去，血肉负责给它们加上 Python 习惯的用法（docstring、类型提示、pandas/pyarrow 转换等）。

## 4. 核心概念与源码讲解

本讲把这两个最小模块拆成 6 节讲解：
- 4.1 ~ 4.3 属于最小模块 **python (src bindings)**（Rust 绑定层）。
- 4.4 ~ 4.5 属于最小模块 **python/lancedb (py api)**（Python 封装层）。
- 4.6 节横跨两层与 Rust 核心，讲命名空间连接的 read-freshness 与 `QueryTable` 下推路由（本次更新的重点）。

### 4.1 Rust→Python 的桥梁：PyO3 与 `_lancedb` 模块

#### 4.1.1 概念说明

要把 Rust 代码变成 Python 能 `import` 的东西，需要：

1. 一个编译成 **动态库** 的 crate。
2. 一个用 `#[pymodule]` 标注的入口函数，它负责把所有要导出的类、函数「登记」进模块对象。
3. 通过 PyO3 的宏，把 Rust 的 `struct` / `fn` 自动生成与 Python 对象互相转换的胶水代码。

LanceDB 的绑定 crate 叫 `lancedb-python`，但它编译产物的模块名是 `_lancedb`（带下划线，表示「内部实现模块」，用户不该直接 `import _lancedb`，而应该 `import lancedb`）。

#### 4.1.2 核心流程

```
maturin build / maturin develop
        │  (编译 Rust crate 为 cdylib)
        ▼
  _lancedb.cpython-3xx-*.so   ← 原生扩展
        │  (Python 执行 import _lancedb)
        ▼
  调用 #[pymodule] _lancedb()
        │  (注册 add_class / add_function)
        ▼
  Python 侧得到 Connection, Table, Query, connect 等名字
        │
        ▼
  python/lancedb/__init__.py 里  from ._lancedb import connect as lancedb_connect
```

#### 4.1.3 源码精读

首先看 crate 的产物配置。`[lib]` 段把产物名定为 `_lancedb`，类型定为 `cdylib`（C 兼容动态库，正是 Python 扩展模块需要的格式）：

[python/Cargo.toml:13-15](https://github.com/lancedb/lancedb/blob/8a5cd74e489194a698f99868f9fba753c23eb25a/python/Cargo.toml#L13-L15) —— 把 Rust crate 编译成名为 `_lancedb` 的原生 Python 扩展模块。

依赖里有两项是 PyO3 体系的关键：`pyo3`（带 `extension-module` feature 表示这是扩展模块，`abi3-py39` 表示兼容 Python 3.9+），以及 `pyo3-async-runtimes`（连接 PyO3 与 tokio 的桥）：

[python/Cargo.toml:29-34](https://github.com/lancedb/lancedb/blob/8a5cd74e489194a698f99868f9fba753c23eb25a/python/Cargo.toml#L29-L34) —— PyO3 0.28 + pyo3-async-runtimes，是本讲异步桥接的基础。

注意绑定 crate 默认开启了一大堆 feature，向核心库 `lancedb` 透传 `aws`/`gcs`/`azure`/`remote` 等，这样 `pip install lancedb` 的用户开箱即用云后端（这承接了 u1-l2 讲过的 feature 透传机制）：

[python/Cargo.toml:50-52](https://github.com/lancedb/lancedb/blob/8a5cd74e489194a698f99868f9fba753c23eb25a/python/Cargo.toml#L50-L52) —— Python 绑定默认开启云存储与远程后端，向核心库下推 feature。

再看模块入口。`_lancedb` 函数体里逐个调用 `m.add_class::<T>()?` / `m.add_function(...)?`，把 Rust 类型登记进模块。每个被登记的类型，就是 Python 侧能拿到的一个名字（`Connection`、`Table`、`Query`、`VectorQuery` 等）：

[python/src/lib.rs:36-73](https://github.com/lancedb/lancedb/blob/8a5cd74e489194a698f99868f9fba753c23eb25a/python/src/lib.rs#L36-L73) —— `#[pymodule]` 入口，把所有 `#[pyclass]` 与 `#[pyfunction]` 注册为 Python 可见的名字。

注意它还初始化了 `env_logger`（`LANCEDB_LOG` 环境变量控制日志级别），并把 `__version__` 设为编译期 crate 版本。lib.rs 顶部的 `pub mod` 声明则列出了绑定层的全部子模块：

[python/src/lib.rs:22-34](https://github.com/lancedb/lancedb/blob/8a5cd74e489194a698f99868f9fba753c23eb25a/python/src/lib.rs#L22-L34) —— 绑定层的模块清单（arrow / connection / error / expr / query / table / runtime 等）。

#### 4.1.4 代码实践

**目标**：确认 `_lancedb` 这个原生扩展确实存在，并看清它导出了哪些名字。

1. 用 CLAUDE.md 提供的命令引导开发环境（编译绑定）：

   ```bash
   cd python && uv run --extra tests --extra dev maturin develop --extras tests,dev
   ```

2. 进入 uv 环境后运行：

   ```bash
   cd python && uv run --extra tests python -c "import _lancedb; print(sorted(n for n in dir(_lancedb) if not n.startswith('__')))"
   ```

3. **观察**：输出里应能看到 `Connection`、`Table`、`Query`、`VectorQuery`、`FTSQuery`、`HybridQuery`、`connect`、`connect_namespace_client`、`Session` 等——它们正是 lib.rs 里 `add_class`/`add_function` 登记的那些名字。
4. **预期结果**：导出列表与 [python/src/lib.rs:36-73](https://github.com/lancedb/lancedb/blob/8a5cd74e489194a698f99868f9fba753c23eb25a/python/src/lib.rs#L36-L73) 里登记的类/函数一一对应。如果出现 `ModuleNotFoundError: No module named '_lancedb'`，说明还没用 `maturin develop` 编译绑定，按第 1 步重做即可（这呼应了 python/CLAUDE.md 的提示）。
5. 如果本地无法编译（缺 Rust 工具链等），可改为阅读 `_lancedb.pyi`（类型存根文件）观察导出符号，并标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：如果想在 Python 里新增一个名字 `Foo`（对应某个 Rust 类型），绑定层最少要改哪两处？

**参考答案**：① 在某个 Rust 文件里用 `#[pyclass] pub struct Foo {...}` 定义该类型；② 在 [python/src/lib.rs:36-73](https://github.com/lancedb/lancedb/blob/8a5cd74e489194a698f99868f9fba753c23eb25a/python/src/lib.rs#L36-L73) 的模块入口里加一行 `m.add_class::<Foo>()?;`。此外还要更新 `python/lancedb/_lancedb.pyi` 的类型存根（见 python/CLAUDE.md 的说明）。

**练习 2**：为什么模块名是 `_lancedb`（带下划线），而用户 `import` 的是 `lancedb`？

**参考答案**：下划线前缀是 Python 惯例，表示「内部实现模块」。`_lancedb` 只负责把 Rust 能力暴露成裸的 Python 对象；真正面向用户、带完整文档和类型提示的 API 在纯 Python 包 `lancedb` 里（`python/lancedb/`），它在 `__init__.py` 里 `from ._lancedb import ...` 复用底层能力。

---

### 4.2 暴露连接与表：`pyclass` / `pymethods` / `pyfunction`

#### 4.2.1 概念说明

登记好模块后，下一步是看 **具体类型怎么暴露**。LanceDB 在绑定层用三种 PyO3 宏：

- `#[pyclass]` 标在 `struct` 上 → 成为一个 Python 类。
- `#[pymethods] impl` 块 → 块里的方法成为 Python 方法。
- `#[pyfunction]` 标在自由函数上 → 成为模块级函数。

绑定层的 `Connection` 和 `Table` 基本就是 Rust 核心 `lancedb::Connection` / `lancedb::Table` 的 **包装壳**：内部持有核心类型，方法里把参数转成 Rust 类型、调用核心、再把返回值转回 Python 类型。

#### 4.2.2 核心流程

以 `connect` 为例，一次 Python → Rust 的完整调用：

```
Python: await lancedb_connect(uri, ...)   # 或 LOOP.run 包装的同步调用
        │
        ▼  (PyO3 把 Python 参数转成 Rust 参数)
Rust:   pub fn connect(...) -> future_into_py(py, async move {
            lancedb::connect(&uri)...execute().await   # 调用核心
        })
        │
        ▼  (future 跑在 fork-safe tokio 上)
Rust:   返回 Connection::new(builder.execute().await?)
        │
        ▼  (PyO3 把 Connection 转成 Python 对象)
Python: 得到一个 _lancedb.Connection 实例
```

关键点：**返回值用 `future_into_py` 包成协程**，所以 Rust 的 `async` 逻辑变成了 Python 可 `await` 的对象。`#[pyo3(signature = (...))]` 用来精确声明 Python 侧的默认参数与关键字参数。

#### 4.2.3 源码精读

先看 `Connection` 这个 pyclass：它只是把核心 `LanceConnection` 装在 `Option` 里（`Option` 是为了支持 `close()` 后置空，调用时再报错）：

[python/src/connection.rs:30-45](https://github.com/lancedb/lancedb/blob/8a5cd74e489194a698f99868f9fba753c23eb25a/python/src/connection.rs#L30-L45) —— `Connection` pyclass 是核心 `LanceConnection` 的薄壳，`get_inner` 在已关闭时报错。

`connect` 这个 pyfunction 是连接入口。注意 `#[pyo3(signature = (uri, api_key=None, ...))]` 声明了 Python 侧的参数形态，函数体把每个 `Option` 参数按需喂给核心的 `ConnectBuilder`，最后 `future_into_py` 把整段异步逻辑变成协程：

[python/src/connection.rs:541-590](https://github.com/lancedb/lancedb/blob/8a5cd74e489194a698f99868f9fba753c23eb25a/python/src/connection.rs#L541-L590) —— `connect` 把 Python 参数转成 builder 调用，再包成 Python 协程返回。

`table_names` 是「builder + future_into_py」的典型样例：先把可选项拼到 builder 上（注意核心的 builder 模式，承接 u2-l1），再 `op.execute().await` 跑在协程里：

[python/src/connection.rs:119-136](https://github.com/lancedb/lancedb/blob/8a5cd74e489194a698f99868f9fba753c23eb25a/python/src/connection.rs#L119-L136) —— `table_names` 体现「Python 参数 → 核心 builder → future_into_py 执行」的标准三段式。

再看 `Table` pyclass 和它的 `schema()` 方法。`schema()` 展示了 **返回值如何从 Rust 转回 Python**：核心返回 Arrow `Schema`，用 `Python::attach(|py| schema.to_pyarrow(py))` 把它转成 pyarrow 的 `Schema` 对象再交还给 Python（`Python::attach` 是 PyO3 0.28 用来在异步上下文里安全获取 GIL 的 API）：

[python/src/table.rs:466-472](https://github.com/lancedb/lancedb/blob/8a5cd74e489194a698f99868f9fba753c23eb25a/python/src/table.rs#L466-L472) —— `Table::schema` 把核心的 Arrow schema 经 `to_pyarrow` 转成 Python 对象。

`add` 方法则展示了 **入参转换**：它接收一个 `PyScannable`（绑定层自定义的、能把 pandas/pyarrow/list-of-dict 统一转成 Arrow reader 的类型），按 `"append"/"overwrite"` 设置模式后执行：

[python/src/table.rs:474-489](https://github.com/lancedb/lancedb/blob/8a5cd74e489194a698f99868f9fba753c23eb25a/python/src/table.rs#L474-L489) —— `Table::add` 接收 `PyScannable` 数据并解析写入模式（承接 u2-l3 的 Scannable 抽象）。

`query()` 方法返回一个新的 `Query` pyclass（注意它不是 async，只配置不执行，承接 u3-l1 的「只配置不执行」）：

[python/src/table.rs:866-868](https://github.com/lancedb/lancedb/blob/8a5cd74e489194a698f99868f9fba753c23eb25a/python/src/table.rs#L866-L868) —— `Table::query` 返回 `Query` 绑定对象，本身不触发 IO。

`Query` 上的 `nearest_to` 把普通查询升级为向量查询，`execute` 把结果包成 `RecordBatchStream`（绑定层重新导出的 Arrow 流类型）：

[python/src/query.rs:465-470](https://github.com/lancedb/lancedb/blob/8a5cd74e489194a698f99868f9fba753c23eb25a/python/src/query.rs#L465-L470) —— `Query::nearest_to` 把查询向量从 pyarrow 转成 Arrow array，调用核心升级为 `VectorQuery`。

[python/src/query.rs:544-562](https://github.com/lancedb/lancedb/blob/8a5cd74e489194a698f99868f9fba753c23eb25a/python/src/query.rs#L544-L562) —— `execute` 在 future 里跑核心查询，返回 `RecordBatchStream`（承接 u3-l2 的流式结果与 `_distance` 列）。

#### 4.2.4 代码实践

**目标**：亲手验证「Python 调用 → Rust 执行 → 返回 Python 对象」这一链路，并观察错误如何跨语言传播。

1. 编译绑定后，运行下面这段（**示例代码**，非项目原有）：

   ```python
   # 示例代码
   import _lancedb
   import asyncio

   async def main():
       conn = await _lancedb.connect("memory://")          # 走 Rust connect 协程
       print(type(conn), conn.uri())                       # <class '_lancedb.Connection'>
       print(await conn.table_names())                     # []

   asyncio.run(main())
   ```

2. **观察 1**：`conn` 是 `_lancedb.Connection` 实例，`conn.uri()` 是同步属性（`#[getter]`），`conn.table_names()` 是协程（需 `await`）。
3. **观察 2**：故意把 URI 改成非法值（如 `db://x` 但不带 `api_key`），看抛出的异常。它来自核心 `lancedb::Error`（承接 u2-l4），经 `infer_error()` 转成 Python 异常。
4. **预期结果**：正常时打印类型与空表名列表；非法连接时抛出 `ValueError` 或 `RuntimeError`（取决于错误变体）。
5. 若本地未编译绑定，标注「待本地验证」，改为在 [python/src/connection.rs:541-590](https://github.com/lancedb/lancedb/blob/8a5cd74e489194a698f99868f9fba753c23eb25a/python/src/connection.rs#L541-L590) 里跟踪 `connect` 如何分流到本地/远程后端。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Table::schema()` 里要用 `Python::attach(|py| ...)` 包裹 `to_pyarrow`，而不是直接调？

**参考答案**：因为这段代码运行在一个 Rust 的 `async move` future 里，**当前线程不一定持有 GIL**（Python 全局解释器锁）。`Python::attach` 会在安全的时机获取 GIL 再执行闭包，避免在没有 GIL 的情况下操作 Python 对象导致未定义行为。

**练习 2**：`connect` 的 `#[pyo3(signature = (...))]` 和函数实际的 Rust 参数列表是什么关系？

**参考答案**：Rust 参数列表决定「函数接受什么类型」，`signature` 决定「Python 侧看到的名字、默认值和是否关键字参数」。二者必须一一对应；`signature` 让 Python 用户能用 `connect("memory://")` 或 `connect("db://x", api_key="...")` 这种符合 Python 习惯的写法。

---

### 4.3 异步执行核心：`future_into_py` 与 fork-safe 运行时

#### 4.3.1 概念说明

上一节反复出现的 `future_into_py` 是本节主角。它把一个 Rust `Future<Output = PyResult<T>>` 变成一个 Python 可 `await` 的对象。但 Rust future 不会自己跑——它需要一个 tokio 运行时去 poll。

`pyo3-async-runtimes` 默认用一个全局 tokio 运行时。但 LanceDB 遇到一个 **致命的坑**：Python 的 `multiprocessing` 在 Linux 上默认用 `fork()`，而 tokio 的 worker 线程 **无法在 fork 后存活**——子进程继承到的 runtime 是「冻住」的，之后每个 `future_into_py` 都会永久挂起。

LanceDB 的解决办法（runtime.rs 顶部注释写得很清楚）：**不依赖那个全局 runtime，而是自己持有一个可重建的 runtime 指针**，并在 `fork()` 的子进程里通过 `pthread_atfork` 把指针置空，下一次用时再重建。

#### 4.3.2 核心流程

```
future_into_py(py, fut)
        │
        ▼  把 fut 交给 LanceRuntime::spawn
LanceRuntime::spawn(fut)
        │
        ▼  get_runtime() 取（必要时建）tokio 运行时
        │      ┌─ 若指针非空 → 直接用
        │      └─ 若为空（fork 后）→ create_runtime() 重建并装回 AtomicPtr
        ▼
tokio 运行时 poll fut，结果回传给 Python 协程

—— fork 时 ——
pthread_atfork child handler：RUNTIME.store(null)   # 标记需要重建

—— 同步 pyfunction 要驱动 async 时 ——
runtime::block_on(fut)  →  get_runtime().block_on(fut)   # 4.6 节会用
```

#### 4.3.3 源码精读

`future_into_py` 本身很短，它只是把 `pyo3_async_runtimes::generic::future_into_py` 的运行时换成自家的 `LanceRuntime`：

[python/src/runtime.rs:145-151](https://github.com/lancedb/lancedb/blob/8a5cd74e489194a698f99868f9fba753c23eb25a/python/src/runtime.rs#L145-L151) —— 用自定义 `LanceRuntime` 替代默认全局运行时的 `future_into_py`。

`LanceRuntime` 实现了 `pyo3_async_runtimes::generic::Runtime` trait，`spawn`/`spawn_blocking` 都委托给 `get_runtime()` 取到的 tokio 运行时：

[python/src/runtime.rs:101-120](https://github.com/lancedb/lancedb/blob/8a5cd74e489194a698f99868f9fba753c23eb25a/python/src/runtime.rs#L101-L120) —— `LanceRuntime` 把 future 投递到自己拥有的 fork-safe tokio 运行时。

fork 安全的关键在 `get_runtime` 与 atfork handler。`get_runtime` 用 `AtomicPtr` 惰性建运行时；`atfork_child` 在 fork 的子进程里把指针置空（注意它 **故意 leak 旧运行时**，因为 drop 一个 tokio Runtime 会去 join 已经死掉的 worker 线程而挂起）：

[python/src/runtime.rs:40-75](https://github.com/lancedb/lancedb/blob/8a5cd74e489194a698f99868f9fba753c23eb25a/python/src/runtime.rs#L40-L75) —— `get_runtime` 惰性建运行时，`atfork_child` 在 fork 后置空指针，保证子进程下一次调用时重建。

同文件里还有一个 4.6 节要用的工具：`block_on`。它复用同一个 fork-safe 运行时，**同步阻塞**当前线程跑完一个 future——专门给「本身是同步 `#[pyfunction]`、却需要驱动一段 async 逻辑」的场景（如原生构建命名空间客户端）：

[python/src/runtime.rs:59-66](https://github.com/lancedb/lancedb/blob/8a5cd74e489194a698f99868f9fba753c23eb25a/python/src/runtime.rs#L59-L66) —— `block_on` 在共享运行时上同步阻塞地跑完一个 future（注释强调不能在运行时自己的 worker 线程里调用，否则死锁）。

> 小贴士：Python 侧也有对称的 fork 处理（见 4.5 节的 `background_loop.py`）。Rust 侧管 tokio runtime，Python 侧管 asyncio 事件循环，两边的 fork 安全是配套设计的。

#### 4.3.4 代码实践

**目标**：感受「Rust async 必须有运行时才跑」这一事实，并理解 fork 重建逻辑（源码阅读型实践）。

1. 阅读上面的 `future_into_py` 与 `get_runtime`，回答：如果删掉 `atfork_child` 里的 `RUNTIME.store(null)`，在 `multiprocessing`（fork 模式）的子进程里第一次 `connect` 会发生什么？
2. **预期推断**：子进程继承到父进程已经建好的 tokio runtime，但其 worker 线程已在 fork 时死亡；`get_runtime` 看到指针非空就直接返回它，于是 `spawn(fut)` 投递到一个没有活线程的运行体上，future 永远不被 poll → **调用永久挂起**。这正是注释里描述的 bug。
3. **本地验证（可选）**：编译绑定后写一个用 `multiprocessing.Process` fork 出子进程、在子进程里 `lancedb.connect("memory://")` 的脚本（**示例代码**），观察在当前代码下能正常返回（因为有 atfork 重建）。
4. 若无法运行，明确标注「待本地验证」，并以源码注释为准理解机制。

#### 4.3.5 小练习与答案

**练习 1**：`future_into_py` 返回给 Python 的是一个什么对象？Python 侧该怎么对待它？

**参考答案**：返回一个 **可 await 的协程/awaitable**。Python 侧必须 `await` 它（在 async 函数里），或者用同步包装器（`LOOP.run`）驱动它，否则它不会执行——这和 Rust future 的惰性一致。

**练习 2**：为什么 `atfork_child` 里要「故意 leak 旧 runtime」而不是 `drop` 它？`block_on` 为什么「不能在运行时自己的 worker 线程里调用」？

**参考答案**：`drop` 一个 tokio `Runtime` 会尝试 `join` 它的 worker 线程；而 fork 后这些线程已不存在，join 会永久阻塞，导致子进程卡死。leak 内存换不挂死，是 fork 安全的常见取舍。`block_on` 在 worker 线程里调用会「在 runtime 内部再次阻塞 runtime 的线程」造成死锁，所以注释禁止；它只该被外层同步线程（如 Python 主线程经 PyO3 进来的调用）使用。

---

### 4.4 Python 层异步封装：`AsyncTable` 与 `AsyncConnection`

#### 4.4.1 概念说明

Rust 绑定层（`_lancedb`）暴露的 `Connection`/`Table` 已经是 Python 对象了，但它们用起来很「裸」：方法签名都是位置参数、返回的是底层 Arrow 流、没有 pandas 转换。Python 封装层 `lancedb`（纯 Python 包）的工作就是给它们 **穿上 Python 习惯的外衣**：

- 提供命名参数、类型提示、详尽 docstring。
- 支持 pandas / list-of-dict / pyarrow 等多种数据输入。
- 提供 `connect_async`（返回 `AsyncConnection`）这一异步入口。

这一节聚焦 **异步** 封装：`AsyncConnection` 与 `AsyncTable`。

#### 4.4.2 核心流程

```
await lancedb.connect_async(uri, ...)
        │  __init__.py: await lancedb_connect(...)        ← Rust connect 协程
        ▼
AsyncConnection(rust_connection)
        │
        ▼  await conn.create_table("t", data)
AsyncConnection.create_table:
        data → to_scannable() → Arrow reader
        await self._inner.create_table(...)               ← Rust Connection.create_table 协程
        return AsyncTable(rust_table)                     ← 包装成 Python 异步表
        │
        ▼  await table.vector_search(vec).to_pandas()
AsyncTable.query()/vector_search() → AsyncQuery → await execute → 结果转 pandas
```

注意：`AsyncConnection` / `AsyncTable` 的方法都是 `async def`，内部 `await self._inner.<rust 方法>()`。这里的 `self._inner` 就是绑定层的 `_lancedb.Connection` / `_lancedb.Table`。

#### 4.4.3 源码精读

`connect_async` 是异步入口。它把参数整理后 `await lancedb_connect(...)`（即 Rust 的 `connect` 协程），再用结果构造 `AsyncConnection`：

[python/python/lancedb/__init__.py:426-439](https://github.com/lancedb/lancedb/blob/8a5cd74e489194a698f99868f9fba753c23eb25a/python/python/lancedb/__init__.py#L426-L439) —— `connect_async` await Rust `connect` 协程，返回 `AsyncConnection`。

`AsyncConnection.create_table` 在做完数据预处理后调用 Rust 的 `create_table` 协程，并 `return AsyncTable(new_table)`：

[python/python/lancedb/db.py:1648-1658](https://github.com/lancedb/lancedb/blob/8a5cd74e489194a698f99868f9fba753c23eb25a/python/python/lancedb/db.py#L1648-L1658) —— 异步建表：await Rust 协程拿到原生 Table，包装成 `AsyncTable`。

`AsyncTable` 的 `__init__` 把绑定层 `LanceDBTable`（即 `_lancedb.Table`）存为 `self._inner`，并接收一组命名空间上下文（`namespace_path` / `namespace_client` / `pushdown_operations` / `route_pushdown_to_rust`，后两者是 4.6 节的主角）：

[python/python/lancedb/table.py:4272-4294](https://github.com/lancedb/lancedb/blob/8a5cd74e489194a698f99868f9fba753c23eb25a/python/python/lancedb/table.py#L4272-L4294) —— `AsyncTable` 持有原生 `_inner` 表与命名空间上下文（含新的 `_route_pushdown_to_rust` 开关）。

`AsyncTable.query()` 返回一个 `AsyncQuery`（承接 u3 的查询构建器），它包着原生 `Table.query()`：

[python/python/lancedb/table.py:4451-4460](https://github.com/lancedb/lancedb/blob/8a5cd74e489194a698f99868f9fba753c23eb25a/python/python/lancedb/table.py#L4451-L4460) —— `AsyncTable.query` 把原生查询对象包成 `AsyncQuery`。

`AsyncTable.add` 是「封装层加值」的典型：它先做 schema 推断、向量列清洗、`to_scannable` 转换，最后才 `await self._inner.add(...)`（即 Rust `Table::add`）。这一层把 pandas/list-of-dict 等异构输入统一成 Arrow，是绑定层不做的：

[python/python/lancedb/table.py:4815-4819](https://github.com/lancedb/lancedb/blob/8a5cd74e489194a698f99868f9fba753c23eb25a/python/python/lancedb/table.py#L4815-L4819) —— 预处理完成后 `await` 绑定层 `Table::add`。

> 一句话总结：**异步层 = 数据预处理（Python） + await Rust 协程**。封装层几乎不含业务逻辑，业务逻辑全在 Rust 核心。

#### 4.4.4 代码实践

**目标**：用异步 API 完成「连接 → 建表 → 向量搜索」，体会 `await` 串联协程的写法。

1. 编译绑定后运行下面这段（**示例代码**，参考了 `AsyncTable` docstring 里的可运行示例）：

   ```python
   # 示例代码
   import asyncio, lancedb

   async def main():
       db = await lancedb.connect_async("./.lancedb_async")
       data = [{"vector": [1.1, 1.2], "b": 2},
               {"vector": [0.5, 1.3], "b": 4}]
       tbl = await db.create_table("my_table", data=data)
       rs = await tbl.vector_search([0.4, 0.4]).to_pandas()
       print(rs)

   asyncio.run(main())
   ```

2. **观察**：每个跨 Rust 边界的调用（`connect_async`、`create_table`、`vector_search().to_pandas()`）都要 `await`，因为它们都是协程。
3. **预期结果**：打印一个按 `_distance` 升序排列的 pandas DataFrame，包含 `b`、`vector`、`_distance` 三列（承接 u3-l2）。
4. 若未编译绑定或缺少 pyarrow/pandas，标注「待本地验证」，并可对照 [python/python/lancedb/table.py:4256-4266](https://github.com/lancedb/lancedb/blob/8a5cd74e489194a698f99868f9fba753c23eb25a/python/python/lancedb/table.py#L4256-L4266) 的官方 docstring 示例核对预期输出。

#### 4.4.5 小练习与答案

**练习 1**：`AsyncTable.add` 里有一大段 `if mode == "overwrite"` / `_sanitize_data` 的预处理，为什么不直接把这些也放进 Rust 核心？

**参考答案**：这些都是 Python 生态特有的输入形态处理（pandas DataFrame、list-of-dict、LanceModel 等），依赖 Python 的类型系统与库。Rust 核心只接受 Arrow 数据（承接 u1-l4 的 `Scannable`/`IntoArrow`），所以「把任意 Python 对象变成 Arrow」的活留给 Python 封装层最合适。

**练习 2**：`AsyncTable` 方法返回的 `AsyncQuery`，和绑定层 `Query` 是什么关系？

**参考答案**：`AsyncQuery` 是纯 Python 的封装类，内部持有绑定层的 `_lancedb.Query`（由 `Table.query()` 返回）。它把链式配置方法（`select`/`where`/`limit`）和终端方法（`to_arrow`/`to_pandas`）用 Python 习惯重新组织，最终仍 `await` 绑定层的 `Query.execute()`。

---

### 4.5 Python 层同步封装：`Table`/`LanceTable` 与后台 `LOOP`

#### 4.5.1 概念说明

异步好用，但很多人就是想要 `table.search(...)` 这种一行出结果的同步写法。LanceDB 的设计原则是：**同步 API 不重复实现业务逻辑，而是把异步协程「堵着跑完」**。

实现这个「堵着跑完」的，是一个叫 `LOOP` 的 **后台事件循环**：一个在守护线程里永久运行 `asyncio` 循环的单例。`LOOP.run(coro)` 把协程提交到那个后台循环，然后阻塞当前线程等结果。于是 `LanceTable.add` 就是 `LOOP.run(self._table.add(...))`——把异步的 `AsyncTable.add` 同步化。

> 术语：**守护线程（daemon thread）** 是后台运行、不阻止进程退出的线程。`LOOP` 用守护线程跑 asyncio，确保它不会让 Python 解释器无法退出。

#### 4.5.2 核心流程

```
同步用户代码： db = lancedb.connect("./db"); tbl = db["my_table"]; tbl.add(data)
                        │
                        ▼   (全是同步调用)
LanceDBConnection.__init__:  self._conn = AsyncConnection(LOOP.run(do_connect()))
        │                          ▲
        │                          └─ LOOP.run 把协程提交到后台 asyncio 线程并阻塞等结果
        ▼
LanceTable.add:  return LOOP.run(self._table.add(data, ...))
                        │
                        ▼
                  AsyncTable.add (await self._inner.add)
                        │
                        ▼
                  Rust Table::add (future_into_py → tokio)
```

核心结论：**同步 API 是异步 API 的阻塞包装**，二者共享同一套 Rust 核心，行为完全一致。

#### 4.5.3 源码精读

先看 `LOOP` 的定义。`BackgroundEventLoop` 在 `__init__` 时就启动一个跑 `run_forever` 的守护线程；`run(future)` 用 `asyncio.run_coroutine_threadsafe` 把协程投递到那个循环，再 `.result()` 阻塞取结果：

[python/python/lancedb/background_loop.py:11-39](https://github.com/lancedb/lancedb/blob/8a5cd74e489194a698f99868f9fba753c23eb25a/python/python/lancedb/background_loop.py#L11-L39) —— `LOOP` 是一个常驻守护线程的 asyncio 事件循环单例。

`run` 方法本身只有几行，但它正是「同步包装异步」的全部秘密：

[python/python/lancedb/background_loop.py:30-36](https://github.com/lancedb/lancedb/blob/8a5cd74e489194a698f99868f9fba753c23eb25a/python/python/lancedb/background_loop.py#L30-L36) —— `run` 把协程丢到后台循环并阻塞等待，是同步 API 的基石。

> fork 安全：这个文件里还有 `os.register_at_fork(after_in_child=_reset_after_fork)`，在 fork 子进程里重建 `LOOP`（旧线程已死）。这和 4.3 节 Rust 侧的 `pthread_atfork` 是一对——Rust 管 tokio，Python 管 asyncio，缺一不可。

同步入口 `lancedb.connect` 最终构造 `LanceDBConnection`，而它的 `__init__` 就是用 `LOOP.run(do_connect())` 跑异步连接：

[python/python/lancedb/db.py:651-670](https://github.com/lancedb/lancedb/blob/8a5cd74e489194a698f99868f9fba753c23eb25a/python/python/lancedb/db.py#L651-L670) —— 同步 `LanceDBConnection` 本质是 `AsyncConnection(LOOP.run(do_connect()))`，证明「同步包异步」。

`LanceTable.__init__` 同样用 `LOOP.run` 跑 `open_table`（除非调用方已经传了一个 `_async` 表，省去重复打开）。注意它现在还接收一个 `route_pushdown_to_rust` 开关（4.6 节主角）：

[python/python/lancedb/table.py:2043-2056](https://github.com/lancedb/lancedb/blob/8a5cd74e489194a698f99868f9fba753c23eb25a/python/python/lancedb/table.py#L2043-L2056) —— `LanceTable` 打开表时用 `LOOP.run` 驱动异步的 `open_table`。

每个同步方法都是同一套模式。`add`：

[python/python/lancedb/table.py:3110-3120](https://github.com/lancedb/lancedb/blob/8a5cd74e489194a698f99868f9fba753c23eb25a/python/python/lancedb/table.py#L3110-L3120) —— `LanceTable.add` = `LOOP.run(self._table.add(...))`。

`delete` 同理（注意它还把 `Expr` 解包成底层谓词，承接 u3-l4）：

[python/python/lancedb/table.py:3466-3468](https://github.com/lancedb/lancedb/blob/8a5cd74e489194a698f99868f9fba753c23eb25a/python/python/lancedb/table.py#L3466-L3468) —— `LanceTable.delete` 一行 `LOOP.run` 完成同步删除。

最精妙的是 `_execute_query`：异步查询返回的是 **异步迭代器**（一段段 RecordBatch），同步层要把它变成 **同步迭代器**。做法是用一个生成器，每次 `yield LOOP.run(async_iter.__anext__())`——把「取下一段」这个异步动作也同步化，再喂给 `pa.RecordBatchReader.from_batches`。注意开头的 `if` 还多了一个 `and not self._route_pushdown_to_rust` 的守卫（4.6 节细讲）：

[python/python/lancedb/table.py:3523-3556](https://github.com/lancedb/lancedb/blob/8a5cd74e489194a698f99868f9fba753c23eb25a/python/python/lancedb/table.py#L3523-L3556) —— `_execute_query` 把异步结果流逐段 `LOOP.run` 同步化，包成 pyarrow reader。

> 注意：[python/python/lancedb/__init__.py:64-238](https://github.com/lancedb/lancedb/blob/8a5cd74e489194a698f99868f9fba753c23eb25a/python/python/lancedb/__init__.py#L64-L238) 的 `connect`（同步）会按 URI 前缀分流：`db://` 走 `RemoteDBConnection`，其余走 `LanceDBConnection`。但无论哪条，最终都用 `LOOP.run` 驱动 Rust 核心，设计一致。

#### 4.5.4 代码实践

**目标**：用同步 API 完成与 4.4.4 相同的任务，对比两种写法。

1. 编译绑定后运行（**示例代码**）：

   ```python
   # 示例代码
   import lancedb

   db = lancedb.connect("./.lancedb_sync")
   data = [{"vector": [1.1, 1.2], "b": 2},
           {"vector": [0.5, 1.3], "b": 4}]
   tbl = db.create_table("my_table", data=data)
   rs = tbl.search([0.4, 0.4]).to_pandas()   # 同步，无 await
   print(rs)
   ```

2. **观察 1**：没有任何 `await` / `asyncio.run`，写法最简。
3. **观察 2**：在 `LanceTable.add` / `delete` 上打断点或加日志（**示例代码**：在 `python/python/lancedb/table.py` 的 `LOOP.run(...)` 调用处临时 `print`），可看到每次同步调用都真的走了后台 `LOOP`。
4. **预期结果**：输出与 4.4.4 的异步版**完全一致**（同样的 `_distance` 升序 DataFrame），印证「同步与异步共享同一套核心」。
5. **对比结论**：同步版更短、更适合脚本；异步版在 Web 服务里不会阻塞事件循环。若本地无法运行，标注「待本地验证」。

#### 4.5.5 小练习与答案

**练习 1**：如果用户在一个已经有 asyncio 事件循环的环境（比如 Jupyter、FastAPI 请求处理函数）里调用同步的 `lancedb.connect`，会不会出问题？

**参考答案**：不会卡死，因为 LanceDB 的同步 API 用的是 **自己专用的后台 `LOOP` 线程**（`asyncio.run_coroutine_threadsafe`），而不是当前线程的事件循环。不过最佳实践仍是：在异步环境里直接用 `connect_async`/`AsyncTable`，避免无谓的线程跳转；在纯同步脚本里用 `connect`/`Table`。

**练习 2**：`_execute_query` 为什么不能简单 `LOOP.run(整个查询)` 一次性拿全部结果，而要逐段 `LOOP.run(async_iter.__anext__())`？

**参考答案**：底层查询结果是 **流式** 的（`RecordBatchStream`，承接 u3-l2），可能很大。逐段同步化可以边读边产出，避免把整个结果集一次性物化进内存；同时也让 pyarrow 的 `RecordBatchReader` 能以真正的流方式被下游消费。

---

### 4.6 命名空间客户端：原生构建与 read-freshness 下推路由（本次更新重点）

#### 4.6.1 概念说明

前面几节都假设「Python 把参数交给 Rust 核心，Rust 跑完返回结果」。但 **命名空间（namespace）连接** 这条路径多了一层微妙：它有两条不同的「建连」实现，并且 **查询既可以在 Python 侧下推，也可以在 Rust 表里执行**。两条路径对「读新鲜度（read-freshness）」的支持不同，混用就会读到陈旧数据。本次提交（PR #3571）正是修这个隐患。

先把三个概念摆清楚：

- **read-freshness（读新鲜度）**：承接 u5-l3 / u6-l3。命名空间后端通过在请求头注入 `x-lancedb-min-timestamp = max(baseline, now - interval)` 来绕过服务端缓存，保证「读得到自己的写」。这个头由 Rust 里的 `ReadFreshnessContextProvider`（一个 `DynamicContextProvider`）在 **Rust 侧** 生成。
- **QueryTable 下推（pushdown）**：承接 u6-l2。把一个查询整段下推到命名空间服务端执行（`namespace_client.query_table`），省去把数据拉回客户端再过滤的带宽。
- **两种建连方式**：
  - **包装（wrap）**：Python 侧先用纯 Python 客户端构造好 namespace 客户端对象，再交给 Rust「包」一层（`from_namespace_client`）。这条路 **没有装 read-freshness provider**。
  - **原生构建（native build）**：把实现名 + 属性（如 `("rest", {"uri": ...})`）交给 Rust，由 Rust 直接 `LanceNamespaceDatabase::connect(...)` 建连，这一步会装上 read-freshness provider。

**Bug 的根源**：旧的 `connect_namespace_client` 永远走「包装」路径，于是即便 `rest` 实现也拿不到 read-freshness provider；而 Python 的 `_execute_query` 看到「该下推 QueryTable」时就直接调用纯 Python 的 `namespace_client.query_table`（urllib3 路径），它 **不会带 `x-lancedb-min-timestamp` 头** → 读操作静默绕过新鲜度 → 读到陈旧结果。

**修复思路**：① 当实现是 `rest` 且属性非空时，改走 **原生构建**（装上 provider）；② Python 侧记一个开关 `_route_pushdown_to_rust`：若为真，则 **不再走 Python 的 query_table 下推**，而是让查询回到 Rust 表执行（Rust 表自带 provider，会带新鲜度头）。

#### 4.6.2 核心流程

```
lancedb.connect_namespace("rest", {"uri": ...}, pushdown_operations=["QueryTable"])
        │
        ▼  __init__.py / namespace.py
LanceNamespaceDBConnection(... namespace_client_impl="rest", properties={"uri":...})
        │   计算 _route_pushdown_to_rust = _builds_namespace_natively(...)  # rest+属性非空 → True
        ▼
_connect_namespace_client(...)  ← Rust pyfunction（注意：它是同步的 #[pyfunction]）
        │
        ▼  build_namespace_natively(impl, properties)?
        │      ┌─ True（rest + 非空）→ runtime::block_on(LanceNamespaceDatabase::connect(...))
        │      │                        └─ connect() 内部 builder.context_provider(ReadFreshnessContextProvider)
        │      └─ False → from_namespace_client(预构建的 Python 客户端)   # 无 provider
        ▼
表上查询 _execute_query / to_arrow：
        if _should_push_down_query_table(...) and not _route_pushdown_to_rust:
            → Python 的 namespace_client.query_table（urllib3，不带新鲜度头）   # 旧路径
        else:
            → 回到 Rust 表执行（带 read-freshness 头）                          # 新路径
```

#### 4.6.3 源码精读

**Rust 绑定层：决定走哪条路。** `connect_namespace_client` 是一个 **同步** `#[pyfunction]`，所以它需要在新加的 `runtime::block_on` 里驱动 `LanceNamespaceDatabase::connect` 这段 async 逻辑。`build_namespace_natively` 就是「分诊台」——只有 `rest` 且属性非空才原生构建：

[python/src/connection.rs:603-650](https://github.com/lancedb/lancedb/blob/8a5cd74e489194a698f99868f9fba753c23eb25a/python/src/connection.rs#L603-L650) —— `connect_namespace_client` 按 `build_namespace_natively` 二选一：原生构建（带 `block_on` + `connect`）或包装预构建客户端。

[python/src/connection.rs:652-660](https://github.com/lancedb/lancedb/blob/8a5cd74e489194a698f99868f9fba753c23eb25a/python/src/connection.rs#L652-L660) —— `build_namespace_natively`：`matches!(impl, Some("rest")) && !properties.is_empty()`。

`block_on` 复用 4.3 节那个 fork-safe 运行时，把这段 async 建连在同步函数里「堵着跑完」：

[python/src/runtime.rs:59-66](https://github.com/lancedb/lancedb/blob/8a5cd74e489194a698f99868f9fba753c23eb25a/python/src/runtime.rs#L59-L66) —— `block_on` 给同步 `#[pyfunction]` 驱动 async 操作用。

**Rust 核心：原生构建为何能拿到新鲜度。** `LanceNamespaceDatabase::connect` 在 `ConnectBuilder` 上挂了一个 `ReadFreshnessContextProvider`——这就是「原生构建」相对「包装」的关键区别：

[rust/lancedb/src/database/namespace.rs:127-145](https://github.com/lancedb/lancedb/blob/8a5cd74e489194a698f99868f9fba753c23eb25a/rust/lancedb/src/database/namespace.rs#L127-L145) —— `LanceNamespaceDatabase::connect` 的签名（4.6.1 的「原生构建」入口）。

[rust/lancedb/src/database/namespace.rs:164-169](https://github.com/lancedb/lancedb/blob/8a5cd74e489194a698f99868f9fba753c23eb25a/rust/lancedb/src/database/namespace.rs#L164-L169) —— 装上 `ReadFreshnessContextProvider` 后才 `.connect()`。

而 provider 本身在做的事就是「读操作时算出 min-timestamp 并塞进上下文」（最终成为 HTTP 头）：

[rust/lancedb/src/database/read_freshness.rs:105-119](https://github.com/lancedb/lancedb/blob/8a5cd74e489194a698f99868f9fba753c23eb25a/rust/lancedb/src/database/read_freshness.rs#L105-L119) —— `provide_context` 只在读操作时返回 `MIN_TIMESTAMP_CONTEXT_KEY`（即 `x-lancedb-min-timestamp`）。

**Python 封装层：镜像分诊 + 记下开关。** Python 侧必须和 Rust 的 `build_namespace_natively` 保持一致（注释明确写了 "Must mirror"），否则两边对「要不要路由到 Rust」的判断会不一致：

[python/python/lancedb/namespace.py:376-386](https://github.com/lancedb/lancedb/blob/8a5cd74e489194a698f99868f9fba753c23eb25a/python/python/lancedb/namespace.py#L376-L386) —— `_builds_namespace_natively` 是 Rust `build_namespace_natively` 的 Python 镜像。

`LanceNamespaceDBConnection.__init__` 在建连时就算好 `_route_pushdown_to_rust`（异步版 `AsyncLanceNamespaceDBConnection` 同样如此）：

[python/python/lancedb/namespace.py:448-454](https://github.com/lancedb/lancedb/blob/8a5cd74e489194a698f99868f9fba753c23eb25a/python/python/lancedb/namespace.py#L448-L454) —— 连接记下 `_route_pushdown_to_rust`，并随建表/开表透传给表对象。

**Python 封装层：用开关让下推回到 Rust。** 关键就是每个「可能下推 QueryTable」的入口都加了 `and not self._route_pushdown_to_rust` 守卫。`LanceTable._execute_query` 与 `AsyncTable._execute_query`/`to_arrow` 三处一致：

[python/python/lancedb/table.py:3533-3543](https://github.com/lancedb/lancedb/blob/8a5cd74e489194a698f99868f9fba753c23eb25a/python/python/lancedb/table.py#L3533-L3543) —— `LanceTable._execute_query`：`_route_pushdown_to_rust` 为真时跳过 Python 端的服务端下推，回到 Rust 表执行。

[python/python/lancedb/table.py:4517-4523](https://github.com/lancedb/lancedb/blob/8a5cd74e489194a698f99868f9fba753c23eb25a/python/python/lancedb/table.py#L4517-L4523) —— `AsyncTable.to_arrow` 同样的守卫（`AsyncTable._execute_query` 处亦然）。

> 顺带一提：当查询确实路由回 Rust 表（而非 Python 下推）时，服务端返回的 Arrow 数据由 Rust 的 `parse_arrow_ipc_response` 解析。本次提交顺带让它同时认得 Arrow IPC **文件** 格式（`ARROW1` 魔数开头，REST/phalanx 返回的正是这种）与 **流** 格式，否则会用错 reader 报 "failed to fill whole buffer"：
> [rust/lancedb/src/table/query.rs:582-583](https://github.com/lancedb/lancedb/blob/8a5cd74e489194a698f99868f9fba753c23eb25a/rust/lancedb/src/table/query.rs#L582-L583) 与 [rust/lancedb/src/table/query.rs:591-628](https://github.com/lancedb/lancedb/blob/8a5cd74e489194a698f99868f9fba753c23eb25a/rust/lancedb/src/table/query.rs#L591-L628) —— 按魔数分流 `FileReader`/`StreamReader`，两种格式都能解析。

#### 4.6.4 代码实践

**目标**：验证「`rest` 原生构建 → `_route_pushdown_to_rust = True`」，对比本地 `dir` 实现（非原生）保持旧路径。这一步复刻了本次提交新增的回归测试。

1. 编译绑定后运行（**示例代码**，复刻 `python/python/tests/test_namespace.py` 的回归测试断言）：

   ```python
   # 示例代码
   import lancedb

   # rest + 非空属性 → 原生构建 → 必须把下推路由回 Rust（带新鲜度头）
   db_rest = lancedb.connect_namespace(
       "rest",
       {"uri": "http://localhost:12345"},
       namespace_client_pushdown_operations=["QueryTable"],
   )
   print("rest _route_pushdown_to_rust =", db_rest._route_pushdown_to_rust)   # 预期 True

   # 本地 dir 实现 → 不原生构建 → 走旧的 Python 下推路径
   db_dir = lancedb.connect_namespace("dir", {"root": "/tmp/ns_dir"})
   print("dir  _route_pushdown_to_rust =", db_dir._route_pushdown_to_rust)    # 预期 False
   ```

2. **需要观察的现象**：
   - `rest` 连接的 `_route_pushdown_to_rust` 为 `True`；`dir` 连接为 `False`。
   - 这正好对应 Rust `build_namespace_natively` 的判定（见 [python/src/connection.rs:652-660](https://github.com/lancedb/lancedb/blob/8a5cd74e489194a698f99868f9fba753c23eb25a/python/src/connection.rs#L652-L660)）。
3. **预期结果**：两行输出分别是 `True` / `False`。`connect_namespace` 本身不会真的发 HTTP（只建客户端、算开关），所以无需真实服务端即可验证。
4. **进阶（需真实/ mock 的 rest 服务端，可能「待本地验证」）**：在 `rest` 连接上对一个含数据的表做查询，用抓包或服务端日志确认请求头里带了 `x-lancedb-min-timestamp`。若手头没有服务端，则阅读 [python/python/tests/test_namespace.py](https://github.com/lancedb/lancedb/blob/8a5cd74e489194a698f99868f9fba753c23eb25a/python/python/tests/test_namespace.py) 中新增的 `test_route_pushdown_to_rust_*` / `test_async_route_pushdown_to_rust_*` 四个测试，理解断言意图。
5. 若本地无法编译绑定，标注「待本地验证」，并以 4.6.3 的源码链为准理解行为。

#### 4.6.5 小练习与答案

**练习 1**：为什么 Python 的 `_builds_namespace_natively` 必须「严格镜像」Rust 的 `build_namespace_natively`？两边不一致会怎样？

**参考答案**：Rust 决定「建连时是否装 provider」，Python 决定「查询时是否走 Rust 表」。如果两边判定不一致——例如 Rust 没装 provider（判定为 False）但 Python 判定为 True 而把下推路由回 Rust——虽然语义上 Rust 表仍会（尝试）带新鲜度头，但 baseline 没被正确初始化，头会缺省或失真；反过来更糟：Rust 装了 provider（True）但 Python 判定 False、走了 Python 的 urllib3 下推，则 **完全绕过新鲜度**，静默读到陈旧数据。所以两边必须一致，且源码注释用 "Must mirror" 明确标注。

**练习 2**：`connect_namespace_client` 是 `#[pyfunction]`（同步），却要调一个 async 的 `LanceNamespaceDatabase::connect`。它怎么做到不阻塞 tokio 运行时自身？

**参考答案**：它用 `runtime::block_on(fut)`，而 `block_on` 是在「运行时外部」的线程（这里是 Python 主线程经 PyO3 进来的调用）上调用 `get_runtime().block_on(fut)`——即由调用方线程阻塞等待、运行时的 worker 线程池去跑 future。它不会在运行时的 worker 线程内部再次 block（那会死锁），这正是 `block_on` 注释里「Must not be called from within the runtime's own worker threads」的含义。

**练习 3**：本次修复为什么要同时改 `LanceTable`（同步）和 `AsyncTable`（异步）两处？

**参考答案**：因为同步与异步两套 API 各自有 `_execute_query`/`to_arrow` 的下推判断（见 4.4/4.5 节的分层）。`_route_pushdown_to_rust` 这个开关必须同时加到两条路径上，否则用户用 `AsyncTable` 查询时会绕过修复、仍读陈旧数据——提交里的 `test_async_route_pushdown_to_rust_for_native_rest` 就专门防这个回归。

---

## 5. 综合实践

把本讲的全部知识串起来，做一个「同库、两套写法」的对照实验，并延伸到命名空间连接的新鲜度开关。

**任务**：在一个临时目录里，分别用同步和异步两种 API 完成完整的 CRUD + 向量搜索闭环，并验证它们读写的是同一份数据；最后用 `connect_namespace` 验证 read-freshness 路由开关。

**步骤**：

1. 用 `maturin develop` 编译绑定（命令见 4.1.4）。

2. 写一个脚本（**示例代码**），分三段：

   ```python
   # 示例代码
   import asyncio, lancedb

   # —— 段 A：同步写 ——
   db = lancedb.connect("./.lancedb_lab")
   db.create_table("vecs", data=[
       {"vector": [1.0, 0.0], "id": 1},
       {"vector": [0.0, 1.0], "id": 2},
       {"vector": [1.0, 1.0], "id": 3},
   ], mode="overwrite")

   # —— 段 B：异步读 + 搜索 ——
   async def search_async():
       adb = await lancedb.connect_async("./.lancedb_lab")
       tbl = await adb.open_table("vecs")          # 读到段 A 同步写入的表
       return await tbl.vector_search([1.0, 0.0]).limit(2).to_pandas()

   async_result = asyncio.run(search_async())

   # —— 段 C：同步再搜一次对比 ——
   sync_result = db.open_table("vecs").search([1.0, 0.0]).limit(2).to_pandas()

   print("async:\n", async_result)
   print("sync :\n", sync_result)

   # —— 段 D：命名空间连接的新鲜度开关（不需真实服务端）——
   db_rest = lancedb.connect_namespace(
       "rest", {"uri": "http://localhost:12345"},
       namespace_client_pushdown_operations=["QueryTable"],
   )
   db_dir = lancedb.connect_namespace("dir", {"root": "./.lancedb_lab"})
   print("rest route_pushdown_to_rust:", db_rest._route_pushdown_to_rust)  # True
   print("dir  route_pushdown_to_rust:", db_dir._route_pushdown_to_rust)   # False
   ```

3. **需要观察的现象**：
   - 段 B 能成功打开段 A 写的表 → 说明同步写、异步读操作的是**同一个底层 Lance 数据集**。
   - `async_result` 与 `sync_result` 的 `id` 顺序一致、`_distance` 一致 → 说明两套 API 行为完全等价。
   - 段 D：`rest` 连接开关为 `True`、`dir` 为 `False`，印证 4.6 节的原生构建判定。

4. **预期结果**：两次搜索的 top-2 都是 `id=1`（向量 `[1,0]`，距离最近）和 `id=3`（向量 `[1,1]`），且距离值相同；段 D 两行分别为 `True` / `False`。

5. **进阶（源码阅读型）**：在 `LanceTable.add` 的 `LOOP.run(...)` 处与 `AsyncTable.add` 的 `await self._inner.add` 处各加一条日志（**示例代码**），运行段 A 和段 B，分别确认：同步调用确实经过了 `LOOP`，异步调用确实直接 `await` 了 Rust 协程。结合 [python/python/lancedb/background_loop.py:30-36](https://github.com/lancedb/lancedb/blob/8a5cd74e489194a698f99868f9fba753c23eb25a/python/python/lancedb/background_loop.py#L30-L36) 与 [python/src/runtime.rs:145-151](https://github.com/lancedb/lancedb/blob/8a5cd74e489194a698f99868f9fba753c23eb25a/python/src/runtime.rs#L145-L151)，画出从 Python 调用到 Rust tokio 的完整调用栈。再对照 [python/src/connection.rs:603-660](https://github.com/lancedb/lancedb/blob/8a5cd74e489194a698f99868f9fba753c23eb25a/python/src/connection.rs#L603-L660) 理解段 D 的开关是如何在「原生构建 vs 包装」之间分诊的。

6. 若本地无法编译/运行，至少完成「进阶」的源码阅读部分，并对其余步骤标注「待本地验证」。

## 6. 本讲小结

- LanceDB Python 绑定是「**Rust 绑定层 `_lancedb`**（PyO3 把核心类型暴露成裸 Python 对象）+ **纯 Python 封装层 `lancedb`**（加 docstring、类型提示、pandas/pyarrow 转换）」两层结构。
- 绑定层用 `#[pyclass]`/`#[pymethods]`/`#[pyfunction]` 暴露 `Connection`/`Table`/`Query` 等，用 `future_into_py` 把 Rust `async` 逻辑包成 Python 协程；Arrow 类型经 `FromPyArrow`/`to_pyarrow` + `Python::attach` 跨语言传递。
- 异步执行靠一个 **fork-safe 的 tokio 运行时**（`LanceRuntime` + `AtomicPtr` + `pthread_atfork`），避免 `multiprocessing` fork 后 runtime 冻死；同文件新增的 `block_on` 让同步 `#[pyfunction]` 也能驱动一段 async 逻辑。
- Python 层提供 **两套等价 API**：`AsyncConnection`/`AsyncTable`（异步，`await self._inner.<rust 方法>()`）和 `LanceDBConnection`/`LanceTable`（同步）。
- 同步 API 是异步 API 的阻塞包装：靠常驻守护线程的后台事件循环 `LOOP`（`asyncio.run_coroutine_threadsafe` + `.result()`）把协程「堵着跑完」；连流式查询都逐段 `LOOP.run(__anext__())` 同步化。
- **命名空间连接的 read-freshness（本次更新）**：`rest` 实现走 Rust 原生构建（`LanceNamespaceDatabase::connect` 装 `ReadFreshnessContextProvider`），并用镜像判定 `_route_pushdown_to_rust` 让 `QueryTable` 下推回到自带新鲜度头的 Rust 表，避免纯 Python 下推路径静默读到陈旧数据。
- 因此同步与异步行为完全一致、读写同一份 Lance 数据；选哪套只取决于使用场景（脚本 vs Web 服务）。Python 侧的 `os.register_at_fork` 与 Rust 侧的 `pthread_atfork` 是配套的 fork 安全设计。

## 7. 下一步学习建议

- **横向对比另一种绑定**：本讲讲的是 PyO3（Python）。下一讲 u7-l2 会讲 Node.js/TypeScript 绑定（napi-rs），建议对照阅读，体会「同一套 Rust 核心 + 不同薄绑定」的复用价值。
- **深入一个绑定方法的全链路**：选一个方法（如 `Table::search`），从 `python/python/lancedb/table.py` 的 `LanceTable`/`AsyncTable` → `python/src/query.rs` 的 `Query`/`VectorQuery` → `rust/lancedb/src/query.rs` 的核心实现，完整走一遍，巩固 u2/u3 学到的查询知识。
- **追完 read-freshness 这条线**：从本讲 4.6 节出发，回到 `rust/lancedb/src/database/read_freshness.rs` 与 `namespace.rs`，再结合 u5-l3（读一致性/时间旅行）和 u6-l3（远程后端 `x-lancedb-min-version`/`x-lancedb-min-timestamp` 头），把「为什么命名空间连接需要原生构建」彻底看懂。
- **阅读类型存根**：浏览 `python/python/lancedb/_lancedb.pyi`，它是绑定层导出符号的「权威清单」，也是给 IDE/类型检查器用的契约。尝试理解为什么每新增一个 `#[pyclass]` 方法都要同步更新它（见 python/CLAUDE.md）。
- **贡献流程预演**：结合 u7-l3 与 AGENTS.md 里「添加一个 Table 新方法」的清单，试着在纸上列出「在 Rust 核心、Python 绑定、Python 封装三层分别要改哪些文件」，为后续真正动手改绑定打基础。
