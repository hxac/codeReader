# 项目定位与整体架构：Rust 原生 gRPC 服务扩展

## 1. 本讲目标

本讲是 sglang-grpc 学习手册的第一篇。读完本讲，你应当能够：

1. 说清楚 **sglang-grpc 是什么**：一个用 Rust + PyO3 编写、最终被编译成 Python 扩展模块 `sglang.srt.grpc._core` 的**进程内原生 gRPC 服务**。
2. 理解 **PyO3 扩展模型**：为什么一个 Rust crate（`crate-type = ["cdylib"]`、`name = "_core"`）能变成 Python 里可以 `import` 的模块。
3. 理解 **构建接入方式**：`python/pyproject.toml` 里的 `[[tool.setuptools-rust.ext-modules]]` 是如何把本 crate 钉进 sglang 的 Python wheel 的。
4. 理解 **职责边界**：Rust 服务、Python 的 `RuntimeHandle`、以及外部 `smg-grpc-servicer` 包三者各自负责什么、谁调用谁。

本讲**不**深入 gRPC 的流式分发、背压、错误映射等细节——那是后续进阶/专家篇的内容。本讲只帮你建立“整体地图”。

## 2. 前置知识

如果你对以下概念完全陌生，建议先花几分钟建立一个直觉，再继续往下读。

- **gRPC**：一种基于 HTTP/2 + Protocol Buffers（protobuf）的远程调用框架。客户端调用一个“方法名”，服务端处理并把结果返回。gRPC 的方法分两类：**一元（unary）**——一问一答；**流式（streaming）**——服务端持续吐数据（非常适合 LLM 逐 token 生成）。
- **protobuf / proto**：用一种与语言无关的 `.proto` 文件描述“有哪些消息、有哪些服务方法”，再用代码生成器（如 Rust 的 `tonic-build`）生成各语言的类型。
- **Rust crate 与 `cdylib`**：Rust 的“包”叫 crate。`cdylib`（C dynamic library）是一种产物类型，表示“编译成一个符合 C ABI 的动态库（`.so` / `.dylib` / `.dll`）”，这样别的语言（如 Python）就能通过 C FFI 加载它。
- **PyO3**：一个让 Rust 和 Python 互操作的库。它能把 Rust 函数/类暴露成 Python 对象，也能让 Rust 拿到 Python 的 `GIL`（全局解释器锁）去调用 Python 代码。
- **GIL（全局解释器锁）**：CPython 在同一时刻只允许一个线程执行 Python 字节码。Rust 想读/写 Python 对象时，必须先“持有 GIL”。
- **Tokio**：Rust 生态里最主流的异步运行时，gRPC 服务端（tonic）就跑在它上面。
- **setuptools-rust**：Python 打包工具 setuptools 的一个插件，能在 `pip install` / 构建 wheel 时自动调用 `cargo build` 把 Rust crate 编译进 Python 包。

> 如果上面某些名词暂时一知半解也没关系，本讲会在出现时结合源码再解释一遍。

## 3. 本讲源码地图

本讲涉及的关键文件如下（后面每个文件都会给出永久链接）：

| 文件（相对仓库根） | 作用 |
| --- | --- |
| `rust/sglang-grpc/Cargo.toml` | crate 清单：声明产物类型 `cdylib`、模块名 `_core`、依赖（pyo3/tonic/tokio 等）。 |
| `rust/sglang-grpc/src/lib.rs` | crate 根：声明子模块、定义 `#[pymodule] _core`、`#[pyfunction] start_server`、`#[pyclass] GrpcServerHandle`。 |
| `rust/sglang-grpc/build.rs` | 构建脚本：用 `tonic-build` 把 `.proto` 编译成 Rust 代码。 |
| `python/pyproject.toml` | Python 包清单：用 `[[tool.setuptools-rust.ext-modules]]` 把本 crate 接进 wheel。 |
| `python/sglang/srt/entrypoints/http_server.py` | Python 端**原生路径**入口：构造 `RuntimeHandle` 并调用 `sglang.srt.grpc._core.start_server`。 |
| `python/sglang/srt/entrypoints/grpc_server.py` | Python 端**外部包路径**：`serve_grpc` 委托给外部 `smg-grpc-servicer`。 |
| `python/sglang/srt/entrypoints/grpc_bridge.py` | 定义 `RuntimeHandle`：Rust 服务回调 Python 的“瘦句柄”。 |

crate 内部目录（来自 `git ls-files src proto`）：

```
src/lib.rs                # crate 根 + PyO3 入口
src/server.rs             # gRPC 服务实现（SglangServiceImpl）
src/server/tests.rs       # server 单元测试
src/bridge.rs             # Python↔Rust 桥接（PyBridge、回调、通道）
src/bridge/tests.rs       # bridge 单元测试
src/tokenizers.rs         # Rust 原生分词器
src/utils/mod.rs          # 工具模块 re-export（crate 私有）
src/utils/py_utils.rs     # Python dict / JSON 转换工具
src/utils/py_utils/tests.rs
src/utils/request_utils.rs# proto 请求 → Python dict 构建
```

## 4. 核心概念与源码讲解

### 4.1 crate 清单与 `[lib]` 段：从 Rust crate 到 `_core`

#### 4.1.1 概念说明

一个 Rust crate 想被 Python 当作“扩展模块”加载，需要满足两个硬条件：

1. **产物是动态库**：Python 加载扩展模块时，期望的是一个共享库文件。Rust 用 `crate-type = ["cdylib"]` 来声明“请编译成 C ABI 动态库”。
2. **有一个“模块初始化入口”**：Python 在 `import` 一个 C 扩展时，会调用库里一个约定好名字的初始化函数（PyO3 会用 `#[pymodule]` 自动生成它）。

此外，为了让生成的 `.so` 能直接被当作 Python 包的一部分，crate 的**库名**会和 Python 模块名对应起来。这里 crate 的 lib 名被设为 `_core`，对应 Python 端的最终模块名 `_core`（完整路径 `sglang.srt.grpc._core` 由打包工具拼出来）。

#### 4.1.2 核心流程

整体链路（本讲只关注“清单”这一段，后续 4.2/4.3 再补全）：

```
Cargo.toml: [lib] name="_core", crate-type=["cdylib"]
        │
        ▼
cargo build  →  生成 _core.so（Linux）/ _core.dylib / _core.pyd
        │
        ▼
（由 setuptools-rust 放到 Python 包路径 sglang/srt/grpc/_core.*）
        │
        ▼
Python: from sglang.srt.grpc import _core
```

#### 4.1.3 源码精读

清单里最关键的就是 `[lib]` 段这两行：

[rust/sglang-grpc/Cargo.toml:8-10](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/Cargo.toml#L8-L10) —— 这两行决定产物形态：`name = "_core"` 设定库名（对应 Python 模块名），`crate-type = ["cdylib"]` 声明编译成动态库而非默认的 Rust 静态库 `rlib`。

依赖段说明了本 crate 横跨的几个领域（pyo3=Python 互操作、tonic=gRPC、tokio=异步运行时、prost=protobuf、tokenizers=分词）：

[rust/sglang-grpc/Cargo.toml:12-23](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/Cargo.toml#L12-L23) —— 注意 pyo3 打开了 `extension-module` feature：

[rust/sglang-grpc/Cargo.toml:13](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/Cargo.toml#L13) —— 这个 feature 告诉 PyO3“我是要被嵌入到 Python 解释器里的扩展模块”，于是 PyO3 **不会**再去链接一份 Python 库，而是依赖宿主 Python 进程已加载的符号。这是避免“链接冲突”的关键开关。

[rust/sglang-grpc/Cargo.toml:28-29](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/Cargo.toml#L28-L29) —— `[features] default = ["pyo3/extension-module"]` 把该 feature 设为默认开启，保证无论谁构建这个 crate，都按“Python 扩展”的方式编译。

构建脚本 `build.rs` 负责把 proto 编译成 Rust 代码（proto 契约细节留到进阶篇 u2-l1，这里只点出它的存在）：

[rust/sglang-grpc/build.rs:4-12](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/build.rs#L4-L12) —— `tonic_build::configure()` 只生成 server 端代码（`build_server(true)`）、不生成 client（`build_client(false)`），因为本 crate 是服务端，不需要客户端桩代码。

#### 4.1.4 代码实践

1. **目标**：确认 crate 产物形态与 Python 模块名的对应关系。
2. **步骤**：打开 `rust/sglang-grpc/Cargo.toml`，定位 `[lib]` 段。
3. **观察**：记录 `name` 与 `crate-type` 两个值；再到 `[dependencies]` 找到 `pyo3`，确认它开启了哪个 feature。
4. **预期结果**：`name = "_core"`、`crate-type = ["cdylib"]`、`pyo3` 开启 `extension-module`。
5. **思考题（待本地验证）**：如果把 `crate-type` 改回默认（去掉该行），`cargo build` 仍能成功，但你猜 Python 端 `import sglang.srt.grpc._core` 会发生什么？（提示：产物文件类型不对。）注意：不要真的去改源码，本讲只读不写。

#### 4.1.5 小练习与答案

**Q1**：`crate-type = ["cdylib"]` 和默认的 `rlib` 有什么本质区别？

> **答**：`cdylib` 产出符合 C ABI 的共享库（`.so`/`.dylib`/`.pyd`），对外暴露 C 风格符号，能被 Python 通过 C FFI 当扩展加载；`rlib` 是 Rust 专用的静态库，只能给别的 Rust crate 链接用，Python 无法直接加载。

**Q2**：为什么 pyo3 要打开 `extension-module` feature？

> **答**：扩展模块运行时是被“嵌入”到已经存在的 Python 解释器进程里的。开启该 feature 后，PyO3 不再链接 Python 库本身，而是复用宿主进程已加载的 Python 符号，避免重复链接导致的符号冲突或加载失败。

### 4.2 PyO3 扩展模块 `_core`：`start_server` 与 `GrpcServerHandle`

#### 4.2.1 概念说明

光有一个 `.so` 还不够，Python 还要知道“这个模块里有哪些函数和类”。PyO3 用三个宏来表达：

- `#[pymodule]`：标记模块初始化函数，函数名就是 Python 端的模块名。这里函数叫 `_core`，所以 Python 模块就是 `_core`。
- `#[pyfunction]`：把一个 Rust 函数导出成 Python 可调用的函数。
- `#[pyclass]`：把一个 Rust 结构体导出成 Python 可实例化/可持有句柄的类。

本 crate 导出三个东西：`start_server`（启动服务的函数）、`GrpcServerHandle`（控制服务生命周期的句柄）、`ChunkSendStatus`（背压相关枚举，本讲不展开）。

#### 4.2.2 核心流程

```
Python: from sglang.srt.grpc import _core
        │
        ▼ _core.start_server(host, port, runtime_handle, ...)
        │  （Rust 侧：解析参数 → 绑定端口 → 建 Tokio 运行时 → 起 gRPC 线程）
        ▼
        返回 GrpcServerHandle
        │
        ▼ Python 持有 handle
        │  需要关停时：handle.shutdown() / handle.is_alive()
```

`start_server` 把“建运行时、起线程、跑 tonic server”这一串都封装好了，对 Python 而言就是一个普通函数调用。

#### 4.2.3 源码精读

模块入口把导出项注册进模块对象：

[rust/sglang-grpc/src/lib.rs:259-265](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/lib.rs#L259-L265) —— `#[pymodule] fn _core(...)` 就是 Python 看到的模块初始化入口；`add_function(start_server)` 和两个 `add_class` 把函数/类挂到模块上。Python 里 `dir(_core)` 就能看到 `start_server`、`GrpcServerHandle`、`ChunkSendStatus`。

启动函数及其签名（注意带默认值的形参）：

[rust/sglang-grpc/src/lib.rs:150-159](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/lib.rs#L150-L159) —— `#[pyfunction]` 把它导出；`#[pyo3(signature = (...))]` 定义了 Python 端可省略的默认值：`worker_threads=4`、`response_channel_capacity=64`、`response_timeout_secs=300`。其中 `runtime_handle: PyObject` 是一个**任意 Python 对象**——这正是 Rust 与 Python 运行时之间的边界（见 4.4）。

控制句柄的结构：

[rust/sglang-grpc/src/lib.rs:21-25](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/lib.rs#L21-L25) —— `GrpcServerHandle` 持有一个关停信号 `Arc<Notify>` 和一个 OS 线程的 `JoinHandle`。

[rust/sglang-grpc/src/lib.rs:30-35](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/lib.rs#L30-L35) —— `shutdown()` 发出关停信号并 `join` 等待线程结束；`is_alive()`（见下行）判断线程是否仍在跑。Python 只需要拿到这个 handle 就能优雅关停服务，完全感知不到背后的 Tokio / tonic。

> 启动函数内部还做了端口绑定、Tokio 运行时构建、`extract_tokenizer_info` 等事，这些是 u1-l4 的主题，本讲先不展开。

#### 4.2.4 代码实践

1. **目标**：列出 `_core` 模块对外暴露的全部符号，并核对 `start_server` 的参数与默认值。
2. **步骤**：阅读 `rust/sglang-grpc/src/lib.rs` 中 `#[pymodule] fn _core` 与 `#[pyfunction] start_server` 两处。
3. **观察**：在 `add_function` / `add_class` 三行里数出导出项；在 `#[pyo3(signature = (...))]` 里数出带默认值的参数。
4. **预期结果**：导出 1 个函数 + 2 个类；`start_server` 共 6 个形参，其中后 3 个有默认值。
5. **待本地验证**：构建出扩展后，在 Python 里执行 `import sglang.srt.grpc._core as g; print(dir(g)); help(g.start_server)`，对照你数出来的符号与签名是否一致。

#### 4.2.5 小练习与答案

**Q1**：`start_server` 的第三个参数 `runtime_handle` 类型是 `PyObject`，为什么不用某个具体的 Rust 结构体？

> **答**：因为 `runtime_handle` 是一个 **Python 对象**（SGLang 的 `RuntimeHandle`），Rust 只需要把它当不透明句柄持有，在需要时通过 GIL 调用它的 `submit_*` / `abort` 等方法即可。用 `PyObject` 表达“任意 Python 对象”正是这个边界的自然写法。

**Q2**：`GrpcServerHandle` 为什么既存 `Notify` 又存 `JoinHandle`？

> **答**：`Notify` 负责“通知服务该停了”（优雅关停的信号），`JoinHandle` 负责“等待那个跑服务的 OS 线程真正结束”。前者触发关停，后者确认关停完成，二者配合才能做到“调用 `shutdown()` 返回时服务已停止”。

### 4.3 Python 包构建接入：`[[tool.setuptools-rust.ext-modules]]`

#### 4.3.1 概念说明

光有 Rust crate 还不能让用户 `pip install sglang` 就用上它——必须把“编译这个 crate”这一步接进 Python 的构建流程。setuptools-rust 就是这个粘合剂：你在 `pyproject.toml` 里声明一个 ext-module 条目，告诉它“请编译这个 Rust crate，并把产物作为某个 Python 模块提供”。构建时 setuptools-rust 会自动调用 `cargo build`，再把生成的 `.so` 放到正确的包路径下。

#### 4.3.2 核心流程

```
pyproject.toml:
[[tool.setuptools-rust.ext-modules]]
target = "sglang.srt.grpc._core"     # 拼出来的 Python 模块全名
path   = "../rust/sglang-grpc/Cargo.toml"  # 指向 crate
binding = "PyO3"                      # 用 PyO3 绑定风格
        │
        ▼  (pip install / 构建 wheel)
setuptools-rust → cargo build → _core.so
        │
        ▼  按target 放置
sglang/srt/grpc/_core.cpython-*.so
        │
        ▼
from sglang.srt.grpc import _core   # 可导入
```

#### 4.3.3 源码精读

Python 清单里的声明（本 crate 对应第一条；第二条是另一个 crate `sglang-mm`，可作对照）：

[python/pyproject.toml:228-231](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/pyproject.toml#L228-L231) —— 注意，这里的永久链接是 Python 文件在仓库中的实际位置。三个字段的含义：

- `target = "sglang.srt.grpc._core"`：产物要伪装成的 **Python 模块全名**。setuptools-rust 会把 `.so` 放到 `sglang/srt/grpc/` 目录下并命名为 `_core.*.so`，于是 `from sglang.srt.grpc import _core` 成立。注意它与 crate 的 lib 名 `_core`（4.1）一致——模块名的最后一段来自 crate 名，前面的 `sglang.srt.grpc` 来自打包路径。
- `path = "../rust/sglang-grpc/Cargo.toml"`：指向本 crate 的清单。路径相对于 `python/` 目录（所以是 `../rust/...`）。
- `binding = "PyO3"`：告诉 setuptools-rust 使用 PyO3 的绑定约定（生成对应的入口符号、模块初始化方式），与 crate 里 `pyo3/extension-module` feature 呼应。

对照第二条（多模态 crate）：

[python/pyproject.toml:233-237](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/pyproject.toml#L233-L237) —— 它把 `rust/sglang-mm` 编译成 `sglang.srt.multimodal._core`，同样是 PyO3 绑定。二者是同一套机制的两个实例，说明这种“Rust crate → Python 扩展”的接入方式在 SGLang 里是通用的。

> 构建时还有 `SGLANG_BUILD_RUST_EXTS` 环境变量可以选择性构建部分扩展（比如只构建 grpc、跳过 mm），细节在 u1-l2 讲。

#### 4.3.4 代码实践

1. **目标**：弄懂 `target / path / binding` 三字段如何把本 crate 接进 Python 包。
2. **步骤**：打开 `python/pyproject.toml`，定位 `[[tool.setuptools-rust.ext-modules]]` 的第一条（`sglang.srt.grpc._core`）。
3. **观察与回答**：分别用一句话写下三个字段各自的作用；再与第二条 `sglang-mm` 条目对比，指出它们的 `target` 和 `path` 有何不同、`binding` 是否相同。
4. **预期结果**：能说清“target=模块全名、path=crate 清单、binding=PyO3”；两条记录只有 target/path 不同、binding 相同。
5. **待本地验证**：构建 wheel 后解压，确认里面有形如 `sglang/srt/grpc/_core.*.so` 的文件，且路径与 `target` 一致。

#### 4.3.5 小练习与答案

**Q1**：如果要把 crate 名从 `_core` 改成 `native`，至少需要同步改哪些地方？

> **答**：至少要改 `Cargo.toml` 的 `[lib] name`、`lib.rs` 里 `#[pymodule] fn _core` 的函数名、以及 `pyproject.toml` 里 `target = "sglang.srt.grpc._core"` 的最后一段，三处的模块名必须一致，否则 Python 端会找不到初始化符号或导入路径对不上。

**Q2**：`path = "../rust/sglang-grpc/Cargo.toml"` 为什么是 `../` 开头？

> **答**：因为 `pyproject.toml` 在仓库的 `python/` 子目录下，而 crate 在仓库的 `rust/sglang-grpc/` 下，两者是平级的兄弟目录，所以要从 `python/` 先回到仓库根（`../`）再进入 `rust/`。

### 4.4 进程内原生服务与 `RuntimeHandle`、`smg-grpc-servicer` 的边界

#### 4.4.1 概念说明

这是本讲最容易混淆、也最关键的一点：**SGLang 里其实存在两条 gRPC 路径**，本 crate 只是其中一条。

1. **原生 Rust 路径（本 crate）**：`rust/sglang-grpc` 编译出 `sglang.srt.grpc._core`，在 **sglang server 进程内** 直接启动一个 Rust tonic 服务，并通过 Python 的 `RuntimeHandle` 复用进程内已有的调度器。它的卖点是“零额外进程、Rust 原生、可与 Python 运行时同进程高效互通”。
2. **外部包路径**：`python/sglang/srt/entrypoints/grpc_server.py` 的 `serve_grpc` **委托**给外部 PyPI 包 `smg-grpc-servicer`。这是一条独立的、由外部包实现的 gRPC server 路径。

理解这条边界，能避免你把两个路径的职责搞混。本讲义的后续所有进阶/专家篇，**只**针对路径 1（本 Rust crate）。

#### 4.4.2 核心流程

**路径 1（原生，本 crate）—— 双向调用**：

```
Python (http_server.py)
   │  构造 RuntimeHandle(tokenizer_manager, ...)
   │  调用 _core.start_server(host, port, runtime_handle)
   ▼
Rust (_core)
   │  持有 runtime_handle（PyObject）
   │  每来一个 gRPC 请求：
   ▼
   Rust 通过 GIL 调用 runtime_handle.submit_xxx(request_dict, chunk_callback)
   ▼
Python (RuntimeHandle)
   │  把请求交给 TokenizerManager，每产出一个 chunk
   │  就回调 chunk_callback(payload, finished, error)
   ▼
Rust 把 chunk 写进响应通道，最终经 tonic 流式返回给 gRPC 客户端
```

关键：**Rust 调 Python（经 `RuntimeHandle`），Python 再调回 Rust（经 `chunk_callback`）**。`chunk_callback` 本身就是 4.2 里提到的 PyO3 对象。

**路径 2（外部包，仅做对照）**：

```
Python (grpc_server.py: serve_grpc)
   │  from smg_grpc_servicer.sglang.server import serve_grpc as _serve_grpc
   ▼
   await _serve_grpc(server_args, model_info, ...)
   （gRPC server 的实现完全在外部包 smg-grpc-servicer 里）
```

#### 4.4.3 源码精读

**路径 1 的 Python 端调用点**（位于 http_server.py）：

[python/sglang/srt/entrypoints/http_server.py:2602-2635](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/entrypoints/http_server.py#L2602-L2635) —— 它先 `from sglang.srt.grpc import _core as grpc_native`（这正是本 crate 的产物），构造 `RuntimeHandle(...)`，然后调用 `grpc_native.start_server(host=..., port=..., runtime_handle=runtime_handle, worker_threads=...)`，拿回 `grpc_handle`。注意它直接复用了进程内已有的 `tokenizer_manager` 等对象——这就是“进程内（in-process）”的含义。

`RuntimeHandle` 是 Rust 服务回调 Python 的“瘦句柄”，其文档字符串把边界说得很清楚：

[python/sglang/srt/entrypoints/grpc_bridge.py:56-63](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/entrypoints/grpc_bridge.py#L56-L63) —— “Thin Python handle that the Rust gRPC server calls into.” 它提供同步的 `submit_*` / `abort` / 信息查询方法；每个 submit 方法会收到一个 `chunk_callback`（Rust 侧 PyO3 对象），并用 `(chunk_dict, finished, error)` 反向把 TokenizerManager 产出的 chunk 推回 Rust。

**路径 2 的委托点**（与路径 1 是两码事）：

[python/sglang/srt/entrypoints/grpc_server.py:156-166](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/entrypoints/grpc_server.py#L156-L166) —— `serve_grpc` 把实际工作交给 `from smg_grpc_servicer.sglang.server import serve_grpc as _serve_grpc`；若该外部包未安装，则抛出提示“gRPC mode requires the smg-grpc-servicer package”。**这条路径与 `sglang.srt.grpc._core`（本 Rust crate）无关**——它依赖的是外部 PyPI 包。该文件顶部还顺带起一个 aiohttp HTTP sidecar（暴露 `/metrics`、`/start_profile` 等），那是另一类职责，不在本 crate 范围内。

> 一句话边界总结：本 Rust crate 负责“在 sglang 进程内跑一个原生 tonic server 并经 `RuntimeHandle` 复用进程内调度器”；`grpc_server.py: serve_grpc` 负责的是另一条“把 gRPC server 交给外部 `smg-grpc-servicer` 实现”的路径。两者并行存在、用途不同。

#### 4.4.4 代码实践

1. **目标**：在源码层面区分两条 gRPC 路径，并验证 `RuntimeHandle` 的回调方向。
2. **步骤**：
   - 打开 `python/sglang/srt/entrypoints/http_server.py` 第 2602 行起，找到 `_start_native_grpc_server_for_runtime`，确认它 `import` 的是 `sglang.srt.grpc._core` 并调用 `start_server`。
   - 打开 `python/sglang/srt/entrypoints/grpc_server.py` 第 156 行起，确认 `serve_grpc` `import` 的是 `smg_grpc_servicer.*`。
   - 打开 `python/sglang/srt/entrypoints/grpc_bridge.py` 第 56 行起，阅读 `RuntimeHandle` 文档字符串。
3. **观察**：两条路径 import 的模块完全不同；`RuntimeHandle` 文档里提到的 `chunk_callback` 是“Rust 给 Python、Python 再调回 Rust”的对象。
4. **预期结果**：能口述“路径1用本 crate 的 `_core.start_server` + `RuntimeHandle`；路径2委托外部包 `smg-grpc-servicer`”。
5. **待本地验证**：若本机已装好扩展，可在 Python 中尝试 `from sglang.srt.grpc import _core` 不报错；至于 `smg_grpc_servicer` 是否存在取决于是否单独安装该外部包。

#### 4.4.5 小练习与答案

**Q1**：为什么说本 crate 是“进程内（in-process）”的 gRPC 服务？

> **答**：因为它和 sglang 的主 server 跑在**同一个进程**里，通过 `RuntimeHandle` 直接复用进程内已经存在的 `tokenizer_manager` 等对象，不需要再开一个独立进程或跨进程通信；gRPC 在这里是“对外暴露的协议”，而不是“跨进程的边界”。

**Q2**：`grpc_server.py` 里的 `serve_grpc` 和本 Rust crate 是什么关系？

> **答**：它们是**两条不同的 gRPC 路径**。`serve_grpc` 委托给外部 PyPI 包 `smg-grpc-servicer`；而本 Rust crate 是 `sglang.srt.grpc._core`，由 `http_server.py` 里的 `_start_native_grpc_server_for_runtime` 通过 `start_server` 启动。本讲义后续只研究后者。

**Q3**：`RuntimeHandle` 里的 `chunk_callback` 是谁创建、谁调用？

> **答**：由 **Rust 侧**创建（一个 PyO3 对象），作为参数随 `submit_*` 传给 Python；然后由 **Python 侧**（`RuntimeHandle`/TokenizerManager）在每个响应 chunk 产出时回调它，把 `(chunk_dict, finished, error)` 推回 Rust。这构成 Rust→Python→Rust 的闭环。

## 5. 综合实践

把本讲的四个最小模块串起来，完成一次“**从 Rust crate 到 Python 导入的完整连线说明**”。

**任务**：写一段（约 200~400 字）中文说明，讲清楚下面这条链路上的每一环，并指出每一环对应的源码位置：

```
rust/sglang-grpc/Cargo.toml   ──►   src/lib.rs (_core PyO3 模块)
        │                                  │
        │  (setuptools-rust 接入)           │  (导出 start_server / GrpcServerHandle)
        ▼                                  ▼
python/pyproject.toml (ext-modules) ──►  sglang.srt.grpc._core（可被 Python import）
                                               │
                                               ▼  被 http_server.py 调用
                                  _core.start_server(..., runtime_handle=RuntimeHandle(...))
```

具体要求：

1. 解释 `Cargo.toml` 的 `[lib]` 段（`name="_core"`、`crate-type=["cdylib"]`）如何决定了产物形态与库名。
2. 解释 `src/lib.rs` 的 `#[pymodule] _core` 导出了哪些符号。
3. 解释 `pyproject.toml` 的 `[[tool.setuptools-rust.ext-modules]]` 三字段（`target` / `path` / `binding`）如何把 crate 编译并安放到 `sglang.srt.grpc._core` 这个 Python 模块路径。
4. 指出 Python 端在 `http_server.py` 中通过哪两行 `import` 拿到 `RuntimeHandle` 和 `_core`，以及最终调用 `start_server` 时传入了什么。

> 完成后，你应该能用一句话回答：“为什么 `from sglang.srt.grpc import _core` 能用？”——因为它背后是 setuptools-rust 把 `cdylib` crate `sglang-grpc` 编译成动态库、按 `target` 路径放置、并由 PyO3 提供模块初始化入口。

**待本地验证**（可选）：若环境允许构建，安装后执行 `python -c "from sglang.srt.grpc import _core; print(_core.start_server)"`，确认打印出一个内置函数对象。

## 6. 本讲小结

- sglang-grpc 是一个 **Rust + PyO3** 编写的 crate，产物是 Python 扩展模块 **`sglang.srt.grpc._core`**，本质是一个**进程内原生 gRPC 服务**。
- `Cargo.toml` 的 `[lib]` 段（`name="_core"` + `crate-type=["cdylib"]`）和 pyo3 的 `extension-module` feature，决定了它能被 Python 当扩展加载。
- `src/lib.rs` 的 `#[pymodule] _core` 导出 `start_server` 函数、`GrpcServerHandle` 句柄类等，对 Python 隐藏了 Tokio/tonic 细节。
- `pyproject.toml` 的 `[[tool.setuptools-rust.ext-modules]]` 通过 `target`/`path`/`binding` 三字段把本 crate 接进 sglang 的 wheel 构建。
- 存在**两条 gRPC 路径**：本 crate（经 `http_server.py` 的 `_core.start_server` + `RuntimeHandle`，进程内）与外部包 `smg-grpc-servicer`（经 `grpc_server.py` 的 `serve_grpc` 委托）。本讲义只研究前者。
- Rust↔Python 的边界是双向的：Rust 经 `RuntimeHandle` 调 Python 的 `submit_*`，Python 经 `chunk_callback` 把响应 chunk 推回 Rust。

## 7. 下一步学习建议

- 想搞清楚“扩展是怎么被构建出来的、能否只构建 grpc 而跳过 mm” → 下一讲 **u1-l2《构建链与工具链：Cargo、build.rs 与 setuptools-rust》**。
- 想先建立 crate 内部模块地图（bridge/server/tokenizers/utils 各管什么） → **u1-l3《目录结构与模块地图》**。
- 想逐段精读 `start_server` 的启动顺序与 `GrpcServerHandle` 生命周期 → **u1-l4《启动入口全景》**。
- 建议按 u1-l2 → u1-l3 → u1-l4 的顺序读完入门层，再进入 u2 进阶层接触 proto 契约与服务实现。
