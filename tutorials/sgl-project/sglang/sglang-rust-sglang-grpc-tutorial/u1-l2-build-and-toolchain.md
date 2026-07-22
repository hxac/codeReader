# 构建链与工具链：Cargo、build.rs 与 setuptools-rust

## 1. 本讲目标

本讲承接 [u1-l1](u1-l1-project-overview.md)（你已经知道 sglang-grpc 是一个被编译成 `sglang.srt.grpc._core` 的 Rust+PyO3 进程内 gRPC 服务）。本讲只回答一个问题：**这个 Rust crate 到底是怎么被“建”出来的、又是怎么被打进 Python 包里的？**

读完本讲，你应当能够：

1. 说清楚 `build.rs` 在做什么：它用 `tonic_build::configure()` 把 `.proto` 编译成 Rust 代码，并解释为什么 `cargo:rerun-if-changed` 只盯住 proto 文件。
2. 读懂 `Cargo.toml` 里每一类依赖（pyo3 / tonic / prost / tokio / tokenizers …）在运行时扮演的角色，以及 `[profile.release]` 的优化取舍。
3. 理解 `rust-toolchain.toml` 如何把整个项目的 Rust 编译器版本钉死。
4. 说清楚 `python/setup.py` 的 `BuildRust` 与 `_selected_rust_extensions` 是如何用环境变量 `SGLANG_BUILD_RUST_EXTS=grpc` 实现“只构建本扩展、跳过 sglang-mm”的。

本讲**不**涉及 gRPC 业务逻辑、流式分发或桥接通道——那是 u2/u3 的内容。本讲只看“构建链”。

## 2. 前置知识

如果你对以下概念完全陌生，建议先建立一个直觉再往下读。

- **构建脚本（build.rs）**：Rust crate 可以附带一个 `build.rs`（也叫 build script），它在**编译 crate 之前**由 cargo 自动运行，通常用来“生成代码”“探测环境”“告诉 cargo 一些指令”。本讲的 `build.rs` 就属于“生成代码”这一类。
- **代码生成（codegen）**：从一种描述（这里是一个 `.proto` 文件）自动产出另一种语言的源代码（这里是 Rust 的服务 trait 和消息结构体），然后再把这些生成的代码和手写代码一起编译。
- **protoc 与 prost**：`protoc` 是 protobuf 官方的编译器；`prost` 是 Rust 社区流行的 protobuf 库；`tonic` 是 Rust 的 gRPC 框架，它的 `tonic-build` 包在 `prost` 基础上额外生成 gRPC 服务端/客户端代码。
- **`OUT_DIR`**：cargo 在运行 build script 时注入的环境变量，指向一个“构建产物临时目录”，生成的代码通常写在这里，之后用 `include!` 或 `tonic::include_proto!` 引入。
- **profile（编译档案）**：cargo 的 `[profile.xxx]` 段，用来控制优化级别、是否开启 LTO、是否 strip 符号等。常见的有 `dev`（调试）和 `release`（发布）。
- **setuptools-rust**：setuptools 的插件，让 `pip install` / `python -m build` 在打包 Python wheel 时，自动调用 `cargo build` 把 Rust crate 编译成扩展模块并放进包里。
- **cmdclass**：setuptools 的扩展点，允许你用自己的子类替换某个构建命令（如 `build_rust`），从而在原生流程前后插入自定义逻辑。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件（相对仓库根） | 作用 |
| --- | --- |
| `rust/sglang-grpc/Cargo.toml` | crate 清单：依赖、产物类型、feature、编译 profile。 |
| `rust/sglang-grpc/build.rs` | 构建脚本：用 `tonic-build` 编译 `.proto`，产物写入 `OUT_DIR`。 |
| `rust/sglang-grpc/rust-toolchain.toml` | 固定本目录使用的 Rust 工具链（channel + profile）。 |
| `python/setup.py` | Python 端构建钩子：`BuildRust` 子类 + `_selected_rust_extensions` 过滤逻辑。 |
| `python/pyproject.toml` | Python 包清单：`[[tool.setuptools-rust.ext-modules]]` 声明两个 Rust 扩展。 |

一句话串起整条链：

```
pyproject.toml 声明 Rust 扩展  ──►  setuptools-rust 触发 build_rust 命令
        │
        ▼
setup.py 的 BuildRust.run() 用 SGLANG_BUILD_RUST_EXTS 过滤扩展
        │
        ▼
cargo build 调用本 crate
        │
        ├── build.rs 先跑：tonic-build 编译 proto → OUT_DIR 下的 Rust 代码
        │
        └── 编译 src/*.rs + 生成的 proto 代码 → _core.so（cdylib）
        │
        ▼
.so 放进 Python 包路径 sglang/srt/grpc/_core.*
```

## 4. 核心概念与源码讲解

### 4.1 Cargo.toml：依赖、产物类型与编译 profile

#### 4.1.1 概念说明

`Cargo.toml` 是这个 crate 的“身份证”。它向 cargo 声明四件事：

1. 这个包叫什么、版本多少、用什么 Rust edition（`[package]`）。
2. 编译产物是什么形态、叫什么名字（`[lib]`）。u1-l1 已讲过 `name = "_core"` + `crate-type = ["cdylib"]`，本讲不再重复，只点一句：它决定了最终产出 `_core.so`。
3. 依赖了哪些别的 crate（`[dependencies]`）以及编译期依赖（`[build-dependencies]`）。
4. 在不同编译档案下怎么优化（`[profile.release]` / `[profile.dev]`）。

#### 4.1.2 核心流程

构建时，cargo 先读 `[build-dependencies]` 把 `tonic-build` 编译好（给 build.rs 用），再读 `[dependencies]` 编译业务依赖，最后把所有产物按 `[profile]` 的设置链接成 `_core.so`。

#### 4.1.3 源码精读

[package] 段说明这是一个独立包，edition 2024（需要较新的 Rust，见 4.3）：

[rust/sglang-grpc/Cargo.toml:1-6](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/Cargo.toml#L1-L6) —— 包名 `sglang-grpc`、edition `2024`、一句话描述“In-process Rust gRPC server for SGLang”。

依赖列表是理解“运行时靠什么”的钥匙：

[rust/sglang-grpc/Cargo.toml:12-23](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/Cargo.toml#L12-L23) —— 业务依赖一览。下表逐项说明作用：

| 依赖 | 在本项目里负责什么 |
| --- | --- |
| `pyo3 0.23`（`extension-module`） | Rust↔Python 互操作；把 `start_server` 等暴露成 Python 可调用对象。 |
| `tokio 1`（`full`） | 异步运行时；tonic gRPC 服务就跑在它上面（u3 会细讲线程模型）。 |
| `tonic 0.12`（`gzip`, `transport`） | gRPC 框架本体；`transport` 提供 server 启停，`gzip` 启用压缩。 |
| `prost 0.13` | protobuf 消息的序列化/反序列化（tonic 底层用它）。 |
| `uuid 1`（`v4`） | 为每个请求生成随机 id（rid），用于在桥接通道里关联请求。 |
| `tracing` / `tracing-subscriber`（`env-filter`） | 结构化日志，`env-filter` 支持用环境变量过滤日志级别。 |
| `serde_json 1` | 解析 Python 回调返回的 JSON 字符串（一元 RPC 的响应解析用到它）。 |
| `tokenizers 0.21`（`onig`） | 原生 HuggingFace 分词器，`onig` 提供正则后端（u2-l8 详讲）。 |
| `tokio-stream 0.1`（`net`） | 异步流工具，配合 tonic 构造响应流。 |
| `async-stream 0.3` | 用 `stream!` 宏把 `yield` 风格的代码变成异步流（u2-l3 详讲）。 |

注意 `[features] default = ["pyo3/extension-module"]`：

[rust/sglang-grpc/Cargo.toml:28-29](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/Cargo.toml#L28-L29) —— 默认就开启 PyO3 的“扩展模块”feature，保证无论谁来构建都按 Python 扩展的方式编译（u1-l1 已解释其含义）。

编译期依赖只有 `tonic-build`：

[rust/sglang-grpc/Cargo.toml:25-26](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/Cargo.toml#L25-L26) —— `tonic-build` 只在 build.rs 里用，所以放在 `[build-dependencies]`，不会进入最终 `_core.so`。

release profile 的优化取舍（发布 wheel 时走的就是它）：

[rust/sglang-grpc/Cargo.toml:31-34](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/Cargo.toml#L31-L34) —— `opt-level = 2`（中等优化，兼顾速度与体积）、`lto = "thin"`（跨 crate 链接期优化，但比 fat LTO 更快）、`strip = true`（去掉调试符号，缩小 `.so` 体积）。这是面向“既要性能又不想让构建太慢”的常见组合。

[rust/sglang-grpc/Cargo.toml:36-38](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/Cargo.toml#L36-L38) —— `dev` 档案关掉优化、保留少量调试信息，方便本地开发调试。

#### 4.1.4 代码实践

**目标**：建立“依赖列表 ↔ 运行时职责”的直觉。

**步骤**：

1. 打开上面的依赖表与本讲源码地图，对照 `Cargo.toml:12-23`。
2. 做一个连线练习：把每个依赖名与“它在运行时负责什么”连起来（不查表，凭印象）。

**需要观察的现象**：你会发现自己能大致分出三类——Python 互操作（pyo3）、异步/gRPC 运行时（tokio/tonic/prost/tokio-stream/async-stream）、业务工具（uuid/serde_json/tokenizers/tracing）。

**预期结果**：能口述“为什么这个 crate 同时需要 tokio 和 pyo3——因为 tonic 跑在 tokio 上，而桥接又必须经 pyo3 调 Python”。（本练习为源码阅读型，不产生运行输出。）

#### 4.1.5 小练习与答案

**练习**：为什么 `tonic-build` 要放在 `[build-dependencies]` 而不是 `[dependencies]`？

**答案**：`tonic-build` 只在**编译前**由 build.rs 调用来生成代码，运行时（`_core.so` 被加载后）根本用不到它。放进 `[dependencies]` 会让它（及其依赖）被链接进最终动态库，无谓增大体积；`[build-dependencies]` 让它只在构建期存在。

---

### 4.2 build.rs 与 tonic_build::configure：把 proto 编译成 Rust 代码

#### 4.2.1 概念说明

`build.rs` 是 cargo 在编译本 crate **之前**自动运行的脚本。它的职责很单一：读 `proto/sglang/runtime/v1/sglang.proto`，用 `tonic-build` 生成 Rust 代码（服务 trait、消息结构体），写到 cargo 提供的 `OUT_DIR` 里；之后 `src/lib.rs` 再用 `tonic::include_proto!("sglang.runtime.v1")` 把这些生成代码拉进来一起编译。

这个 crate 没有 Rust 工作区级别的 `Cargo.toml`（`rust/` 下并没有 workspace 文件），构建完全由 Python 端的 setuptools-rust 指向 `rust/sglang-grpc/Cargo.toml` 单独驱动（见 4.4）。因此 build.rs 里用的是相对于 crate 根的相对路径 `../../proto/...`（即仓库根的 `proto/sglang/runtime/v1/sglang.proto`，已确认存在）。

#### 4.2.2 核心流程

build.rs 的执行过程：

```
1. 定位 proto 文件：proto_path = "../../proto/sglang/runtime/v1/sglang.proto"
2. tonic_build::configure()
       .build_server(true)          # 只要服务端代码
       .build_client(false)         # 不要客户端桩（本 crate 是服务端）
       .protoc_arg(--experimental_allow_proto3_optional)  # 允许 proto3 的 optional 字段
       .file_descriptor_set_path($OUT_DIR/sglang_descriptor.bin)  # 保存描述符
       .compile_protos([proto_path], ["../../proto"])   # 编译；第二个参数是 include 路径
3. println!("cargo:rerun-if-changed={proto_path}")   # 告诉 cargo 何时该重跑本脚本
```

关键点：

- `compile_protos` 的第二个参数 `["../../proto"]` 是 include 路径——proto 文件内部的 `import "..."` 会相对它解析。
- 生成的代码会落在 `OUT_DIR` 下；`src/lib.rs:6-8` 的 `tonic::include_proto!("sglang.runtime.v1")` 把它展开。注意字符串参数 `"sglang.runtime.v1"` 必须与 proto 里的 `package sglang.runtime.v1;` 严格一致。

#### 4.2.3 源码精读

整个 build.rs 只有 16 行，逻辑集中在这里：

[rust/sglang-grpc/build.rs:1-16](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/build.rs#L1-L16) —— main 函数先定位 proto，配置 tonic-build，编译，最后声明重跑条件。

逐行拆解配置链：

[rust/sglang-grpc/build.rs:4-12](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/build.rs#L4-L12) —— `.build_server(true)` / `.build_client(false)`：本 crate 是 gRPC **服务端**，所以只生成服务 trait 和 server 端桩，不生成客户端代码，减少无用产物。

[rust/sglang-grpc/build.rs:7](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/build.rs#L7) —— `--experimental_allow_proto3_optional`：proto3 默认没有显式 `optional`，proto 文件里用了 `optional` 字段时需要把这个开关传给 protoc，否则编译会报错。

[rust/sglang-grpc/build.rs:8-11](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/build.rs#L8-L11) —— `file_descriptor_set_path` 把 FileDescriptorSet（一份二进制的 proto 自描述）写到 `$OUT_DIR/sglang_descriptor.bin`。`OUT_DIR` 由 cargo 注入，是构建临时目录；这份描述符可被用于反射 / gRPC 服务注册。

[rust/sglang-grpc/build.rs:12](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/build.rs#L12) —— `compile_protos(&[proto_path], &["../../proto"])`：第一个参数是要编译的 proto 文件列表，第二个是 import 的 include 根。

最后这行是本讲实践任务的主角：

[rust/sglang-grpc/build.rs:14](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/build.rs#L14) —— `cargo:rerun-if-changed` 只声明了 `proto_path` 一个文件。原因见 4.2.4 与第 5 节的综合实践。

#### 4.2.4 代码实践

**目标**：理解 `cargo:rerun-if-changed` 为何只盯 proto。

**步骤**：

1. 阅读 [build.rs:14](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/build.rs#L14)。
2. 回顾 cargo 的语义：默认情况下，**包内任意文件变化**都会触发重跑 build script；一旦脚本输出了至少一条 `rerun-if-changed`，默认规则就被**替换**成“只在被声明的路径变化时重跑”（`build.rs` 自身始终被隐式监视）。
3. 想一想：build.rs 的输出（生成的 proto 代码）只取决于什么输入？

**需要观察的现象 / 推理**：

- build.rs 不读任何 `.rs` 源文件，它的唯一外部输入就是 `.proto`。
- `.rs` 源文件由 cargo 正常的 Rust 编译流程处理，与 build script 无关。
- 如果**不**声明 `rerun-if-changed`，那么你每改一行 `src/server.rs`，cargo 的粗粒度默认规则都可能重跑 build script → 重新调用 protoc，浪费构建时间。
- 声明 `rerun-if-changed=proto_path` 后，cargo 只在 proto 变了（或 build.rs 自身变了）才重跑，编辑 Rust 代码不再触发 protoc。

**预期结果**：能口述——“build.rs 唯一会改变输出的输入是 proto 文件，所以只需盯它；build.rs 自身被 cargo 隐式监视，build 依赖（tonic-build）的版本变化由 Cargo.lock / `[build-dependencies]` 追踪，都不用手写 rerun-if-changed。”

> 待本地验证：若你本地装好 Rust 工具链，可以在 `rust/sglang-grpc` 下运行 `cargo build`，然后只 `touch src/server.rs` 再 `cargo build`，观察第二次构建**不**重新跑 protoc（终端不会出现 build script 处理 proto 的输出）；而 `touch ../../proto/sglang/runtime/v1/sglang.proto` 后再构建则会重跑。

#### 4.2.5 小练习与答案

**练习 1**：`compile_protos` 的第二个参数 `&["../../proto"]` 如果写错（比如漏掉），会出什么问题？

**答案**：proto 文件里的 `import "..."` 语句将无法解析，protoc 会报“找不到导入文件”的编译错误，build.rs 失败，整个 crate 编译中止。

**练习 2**：如果未来要新增一个 gRPC 方法，需要改 build.rs 吗？

**答案**：通常**不需要**。新方法加在 `.proto` 文件里即可，proto 一变，`rerun-if-changed` 自动触发 build.rs 重新生成代码；`tonic-build` 的配置（只要/不要 server/client）不变。你只需在 `src/server.rs` 里实现新 trait 方法（u2-l1 / u3-l6 会讲完整清单）。

---

### 4.3 rust-toolchain.toml：固定工具链

#### 4.3.1 概念说明

`rust-toolchain.toml` 是 rustup 识别的特殊文件。把它放在某个目录下，凡是“在该目录或子目录里”调用的 `cargo` / `rustc`，rustup 都会**自动切换到**文件里指定的工具链版本，而不必全局 `rustup default`。它的意义是：保证任何人、CI、setuptools-rust 在构建本 crate 时用的是同一个编译器版本，避免“我这能编、你那报错”。

#### 4.3.2 核心流程

```
rustup 在执行 cargo 前，读取 rust-toolchain.toml
   ├── channel = "1.90"  → 使用 Rust 1.90 工具链（未安装则自动下载）
   └── profile = "minimal" → 只装 rustc + cargo（跳过 rust-docs/clippy 等，省空间省时间）
```

#### 4.3.3 源码精读

整个文件只有三行：

[rust/sglang-grpc/rust-toolchain.toml:1-3](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/rust-toolchain.toml#L1-L3) —— `channel = "1.90"`、`profile = "minimal"`。

为什么需要这么新？因为 `Cargo.toml` 用了 `edition = "2024"`，而 Rust 2024 edition 在 1.85 才稳定，`1.90` 远高于门槛，能稳定支持。

`profile = "minimal"` 的取舍：构建扩展只需编译器本体，安装时跳过文档和额外组件，CI 镜像和开发机的工具链下载更快、更小。

#### 4.3.4 代码实践

**目标**：确认工具链文件生效。

**步骤**：

1. 阅读上面的三行配置。
2. （可选）若本地装了 rustup，在 `rust/sglang-grpc` 下运行 `rustc --version`，应显示 `1.90.x`。

**预期结果**：在该目录里执行 cargo 命令时，rustup 自动使用 1.90，与全局默认工具链无关。

> 待本地验证：取决于你本机是否安装 rustup 以及是否联网下载该工具链；未装 rustup 时该文件被忽略，使用系统 `cargo`。

#### 4.3.5 小练习与答案

**练习**：如果有人把 `Cargo.toml` 的 edition 改成 `2024`，但环境里的 rustc 只有 1.80，会怎样？

**答案**：rustc 1.80 不认识 2024 edition（需 ≥1.85），编译会直接报错。这正是 `rust-toolchain.toml` 把 channel 钉到 `1.90` 的意义——它在前置就保证工具链足够新。

---

### 4.4 setuptools-rust：BuildRust 与 _selected_rust_extensions

#### 4.4.1 概念说明

前面三节讲的都是“Rust 这一侧怎么编”。本节回答“怎么把它接进 Python 包，并且能**选择性**只编某几个扩展”。

sglang 的 Python 包声明了**两个** Rust 扩展（在 `pyproject.toml`）：

- `sglang.srt.grpc._core`（本讲主角，路径 `../rust/sglang-grpc/Cargo.toml`）
- `sglang.srt.multimodal._core`（多模态扩展，路径 `../rust/sglang-mm/Cargo.toml`）

默认情况下两个都会被构建。但很多时候你只想改动并验证 gRPC 这一块，不想等 sglang-mm 也编一遍。`python/setup.py` 提供了环境变量 `SGLANG_BUILD_RUST_EXTS`，通过子类化 setuptools-rust 的 `build_rust` 命令、在真正构建前**过滤**扩展列表，实现“只构建想要的”。

注意：这是**构建期**环境变量，所以 setup.py 直接读 `os.environ`，而不是读 sglang 运行时的 `sglang.srt.environ`——后者要等包构建完才存在（[setup.py:1-13](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/setup.py#L1-L13) 的模块文档串已说明这一点）。

#### 4.4.2 核心流程

```
pyproject.toml 声明 2 个 [[tool.setuptools-rust.ext-modules]]  (grpc + multimodal)
        │
        ▼
setuptools-rust 注册 build_rust 构建命令；setup.py 用 BuildRust 子类替换它（cmdclass）
        │
        ▼
pip / python -m build 触发 build_rust → BuildRust.run()
        │
        ▼
run() 调 _selected_rust_extensions(self.extensions)  根据 SGLANG_BUILD_RUST_EXTS 过滤
        │
        ├── 未设置 / "all" / 空  → 全部 2 个
        ├── "none"               → 0 个（直接 return）
        └── "grpc,xxx"           → 只保留名字（大小写不敏感）包含某 token 的扩展
        │
        ▼
self.extensions = 过滤结果；非空则 super().run() 真正调 cargo 构建
```

`_selected_rust_extensions` 的匹配规则是**大小写不敏感的子串匹配**：把 token 与扩展的**全限定名**（如 `sglang.srt.grpc._core`）做 `token in ext.name.lower()` 判断。所以 `grpc` 能命中 `sglang.srt.grpc._core`，却命中不了 `sglang.srt.multimodal._core`。

#### 4.4.3 源码精读

先看 pyproject.toml 里两个扩展的声明：

[python/pyproject.toml:228-237](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/pyproject.toml#L228-L237) —— 两段 `[[tool.setuptools-rust.ext-modules]]`：第一段 `target = "sglang.srt.grpc._core"` 指向本 crate；第二段 `target = "sglang.srt.multimodal._core"` 指向 `sglang-mm`。`binding = "PyO3"` 说明用 PyO3 绑定。

再看 setup.py 的过滤函数。它先决定“要哪些”：

[python/setup.py:38-47](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/setup.py#L38-L47) —— 读 `SGLANG_BUILD_RUST_EXTS`：未设置返回全部；`"all"` 或纯空白也返回全部；`"none"` 返回空列表。

接着按逗号切分 token，并拒绝空条目：

[python/setup.py:49-54](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/setup.py#L49-L54) —— 例如 `"grpc,,mm"` 里有空条目，会直接 `raise ValueError`，避免用户误写。

然后做子串匹配，并检查是否有 token 啥也没命中：

[python/setup.py:56-71](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/setup.py#L56-L71) —— 对每个 token，`hits = {ext.name for ext in declared if token in ext.name.lower()}`。命中则并入 `matched`；一个都没命中则记入 `unmatched`，最终抛出带“已声明扩展清单”的错误，提示用户拼错了。

`BuildRust` 子类把这个过滤接到真正的构建命令前：

[python/setup.py:74-89](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/setup.py#L74-L89) —— 子类化 setuptools-rust 的 `build_rust`，并把它注册进 `cmdclass`。注意开头的 `try/except`：在“不声明 Rust 扩展的备用平台 pyproject”上可能没装 `setuptools_rust`，此时降级为 `_cmdclass = {}`，让构建不致于因缺少插件而失败。

run() 的核心四行：

[python/setup.py:79-85](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/setup.py#L79-L85) —— 先用 `_selected_rust_extensions` 过滤得到要构建的扩展，同时回写到 `self.extensions` 和 `self.distribution.rust_extensions`（后者保证整个 distribution 视图一致）；如果过滤后为空，直接 `return`（一个都不编）；否则 `super().run()` 交给 setuptools-rust 原生流程，对剩下的扩展逐个调用 cargo。

最后把命令挂上去：

[python/setup.py:92](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/setup.py#L92) —— `setup(cmdclass=_cmdclass)`：把 `build_rust` 命令替换成带过滤逻辑的 `BuildRust`（或在不支持时为空，沿用默认）。

#### 4.4.4 代码实践

**目标**：手动推演 `SGLANG_BUILD_RUST_EXTS=grpc` 时的过滤结果。

**步骤**：

1. 假设 `declared` = [`sglang.srt.grpc._core`, `sglang.srt.multimodal._core`]。
2. `raw = "grpc"`，`spec = "grpc"`（已 lower）。
3. 不是 `"all"` / `"none"`，进入 token 匹配：`tokens = ["grpc"]`。
4. 对 `"grpc"`：检查它是否是每个 ext.name 的大小写不敏感子串。

**需要观察的现象**：

- `"grpc" in "sglang.srt.grpc._core".lower()` → `True`（`...srt.grpc._core` 含子串 `grpc`）→ 命中。
- `"grpc" in "sglang.srt.multimodal._core".lower()` → `False`（`multimodal._core` 不含 `grpc`）→ 不命中。
- `matched = {"sglang.srt.grpc._core"}`，`unmatched = []`（无报错）。

**预期结果**：`_selected_rust_extensions` 返回 `[sglang.srt.grpc._core]` 这一个扩展。于是 `BuildRust.run` 只对本 crate 调 cargo，`sglang-mm`（多模态扩展）不在 `self.extensions` 里，被**跳过**。这就是 `SGLANG_BUILD_RUST_EXTS=grpc` 能“只构建本扩展、跳过 sglang-mm”的原因。

> 待本地验证：在 `python/` 下执行 `SGLANG_BUILD_RUST_EXTS=grpc pip install -e .`（或 `python -m build --wheel`），观察日志里只出现对 `sglang-grpc` 的 cargo 构建步骤，不出现 `sglang-mm`。具体日志格式取决于 setuptools-rust 版本，故标注待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：如果设置 `SGLANG_BUILD_RUST_EXTS=mm`，结果是什么？

**答案**：**会报错**（`ValueError`）。这里有个子串匹配的陷阱：`"mm"` 并**不是** `"sglang.srt.multimodal._core"` 的子串——单词 `multimodal` 拼作 `m-u-l-t-i-m-o-d-a-l`，两个 `m` 之间隔着 `ulti`，不相邻，所以 `"mm" in "multimodal"` 为 `False`。它也不是 `grpc` 扩展名的子串，于是 `unmatched = ["mm"]`，函数抛错并列出已声明扩展。想选多模态扩展，应使用真正是其子串的 token，如 `multimodal`、`multi` 或 `modal`（它们都是 `"multimodal"` 的连续子串）。这个小坑正说明：用**完整词**当 token 最稳妥。

**练习 2**：设置 `SGLANG_BUILD_RUST_EXTS=nonexistent` 会发生什么？

**答案**：`"nonexistent"` 不是任何 ext.name 的子串，`unmatched = ["nonexistent"]`，函数抛出 `ValueError`，并列出已声明的扩展名清单，构建在过滤阶段就失败，不会进入 cargo。

---

## 5. 综合实践

把本讲的两条主线（build.rs 的 proto 编译 + setup.py 的扩展选择）串起来。

**实践目标**：从“源码阅读”角度，完整复述 sglang-grpc 从 `.proto` 到 `sglang.srt.grpc._core` 的构建链，并解释两个关键开关。

**操作步骤**：

1. 打开 `rust/sglang-grpc/build.rs`，定位 [build.rs:14](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/build.rs#L14) 的 `cargo:rerun-if-changed`。
2. 写一段话回答：**为什么它只声明了 proto 文件这一个路径？**（提示：build.rs 的唯一外部输入是什么；不声明的话 cargo 的默认重跑规则会怎样；build.rs 自身与 build 依赖由谁负责。）
3. 打开 `python/setup.py`，定位 [setup.py:56-71](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/setup.py#L56-L71) 的匹配循环。
4. 假设当前声明的两个扩展是 `sglang.srt.grpc._core` 与 `sglang.srt.multimodal._core`。写出 `SGLANG_BUILD_RUST_EXTS=grpc` 时，`matched` 集合的最终内容，并说明为何 sglang-mm 被排除。
5. 结合 [setup.py:79-85](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/setup.py#L79-L85)，解释被排除的扩展**为什么真的不会被构建**（而不是只是不显示）。

**需要观察的现象 / 推理要点**：

- build.rs 侧：`rerun-if-changed` 把重跑条件收紧到“只在 proto 变化时”，避免每次改 `.rs` 都重跑 protoc。
- setup.py 侧：过滤发生在 `super().run()` **之前**，被排除的扩展根本不会进入 cargo 调用列表。

**预期结果**（参考答案）：

- 第 2 步：build.rs 只读 proto，其输出（生成的 Rust proto 代码）唯一取决于 proto 文件；`.rs` 源码由 cargo 的正常编译流程处理，与 build script 无关。不声明 `rerun-if-changed` 时，cargo 会用“包内任意文件变化就重跑”的粗粒度默认规则，导致编辑 Rust 代码也触发 protoc；声明后只在 proto（及隐式监视的 build.rs 自身）变化时重跑。build 依赖 `tonic-build` 的版本变化由 `Cargo.lock` + `[build-dependencies]` 追踪，无需手写。
- 第 4 步：`matched = {"sglang.srt.grpc._core"}`。`"grpc"` 是 `sglang.srt.grpc._core` 的子串，但不是 `sglang.srt.multimodal._core` 的子串，故多模态扩展被排除。
- 第 5 步：`BuildRust.run` 先把 `self.extensions` 和 `self.distribution.rust_extensions` 都替换为过滤后的列表，再 `super().run()`。setuptools-rust 原生流程只会遍历 `self.extensions` 调 cargo，被过滤掉的扩展不在其中，因此**确实不会被构建**——不只是“隐藏”。

> 待本地验证：完整的端到端构建（`SGLANG_BUILD_RUST_EXTS=grpc pip install -e .`）依赖本机 Rust 工具链、setuptools-rust 版本与网络，无法在此保证运行结果；以上为基于源码的静态推演，逻辑可由阅读 `build.rs` 与 `setup.py` 直接验证。

## 6. 本讲小结

- `Cargo.toml` 用 `[lib] name="_core" crate-type=["cdylib"]` 决定产物形态，依赖按“Python 互操作 / 异步 gRPC 运行时 / 业务工具”三类各司其职；`[profile.release]` 选 `opt-level=2 + thin LTO + strip` 兼顾性能与体积。
- `build.rs` 唯一职责是用 `tonic_build::configure()` 把 `.proto` 编成 Rust 代码写入 `OUT_DIR`；只生成 server 端、传 `--experimental_allow_proto3_optional`、保存 descriptor，最后 `compile_protos([proto], [include_root])`。
- `cargo:rerun-if-changed` 只盯 proto：因为它是 build.rs 唯一会改变输出的外部输入；声明后可避免改 `.rs` 时无谓重跑 protoc。
- `rust-toolchain.toml` 把工具链钉到 `1.90`（`profile=minimal`），与 `edition=2024` 配套，保证所有人构建一致。
- `pyproject.toml` 用两段 `[[tool.setuptools-rust.ext-modules]]` 声明 grpc 与 multimodal 两个扩展。
- `setup.py` 的 `BuildRust.run` 在构建前用 `_selected_rust_extensions` 按 `SGLANG_BUILD_RUST_EXTS` 做**大小写不敏感子串匹配**过滤；`=grpc` 只保留 `sglang.srt.grpc._core`，从而跳过 sglang-mm。

## 7. 下一步学习建议

到这里你已经掌握 sglang-grpc 的“怎么构建”。下一步建议进入 u1 单元的最后两篇：

- **u1-l3 目录结构与模块地图**：阅读 `src/lib.rs` 顶部的模块声明，建立 `bridge / server / tokenizers / utils / proto` 之间的依赖关系，为精读各模块定位。
- **u1-l4 启动入口全景**：从 `start_server` 出发，看运行时如何在独立线程里拉起 Tokio 运行时与 tonic server，把本讲“编译产物 `_core.so`”真正“跑起来”。

随后进入 u2，从 `proto` 契约（u2-l1）开始，沿着“服务实现 → 流式/一元分发 → Python 桥接”的真实调用链逐层深入。
