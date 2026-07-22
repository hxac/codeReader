# proto 契约与 tonic 代码生成

## 1. 本讲目标

本讲是进阶层（u2）的第一篇。在入门层里我们已经知道：`sglang-grpc` 是一个用 Rust 写的、被编译成 Python 扩展 `sglang.srt.grpc._core` 的进程内 gRPC 服务。但「gRPC 服务」到底是什么形状、Rust 代码从哪里来？本讲就来回答这个最根本的问题。

读完本讲，你应当能够：

- 读懂 `proto/sglang/runtime/v1/sglang.proto` 里定义的服务与消息，并能区分 SGLang 原生 RPC、OpenAI 透传 RPC、Admin/Ops RPC 这三类。
- 看懂 `build.rs` 如何用 `tonic_build::configure()` 把这份 proto 编译成 Rust 代码，并理解 `build_server(true)` 与 `build_client(false)` 各自控制了什么。
- 理解 `src/lib.rs` 里 `tonic::include_proto!("sglang.runtime.v1")` 为什么参数必须和 proto 的 `package` 声明逐字一致，以及生成出来的类型在 Rust 端长什么样。

本讲只讲「契约（proto）」和「代码生成（tonic-build）」这两件事，不涉及服务方法的具体实现逻辑——那是 u2-l2 之后讲义的主题。

## 2. 前置知识

在进入源码前，先用通俗语言把几个概念讲清楚。

### 2.1 什么是 protobuf / proto

**Protocol Buffers（protobuf）** 是 Google 提出的一种「接口描述语言 + 二进制序列化格式」。它有两个产物：

1. 一份 `.proto` 文本文件，用人类可读的语法描述「有哪些消息（message）、有哪些远程调用（rpc）」。这就是**契约**。
2. 一套二进制编码规则，把上述消息压成紧凑的字节流在网络上传输。

对一个 gRPC 服务来说，`.proto` 就是它的「宪法」：客户端和服务端都依据它来生成各自的代码，双方因此能跨语言、跨进程地对话。

### 2.2 什么是 gRPC

gRPC 建立在 protobuf 之上。你在 `.proto` 里用 `service` 关键字声明一组方法（rpc），每个方法有「请求消息」和「响应消息」。gRPC 会把这些方法暴露成可被远程调用的接口。rpc 的返回值有两种重要写法：

- `returns (FooResponse)`：**一元（unary）调用**，发一个请求、收一个响应。
- `returns (stream FooResponse)`：**服务端流式（server streaming）调用**，发一个请求、服务端可以分多次把多个响应「流」回来。

这两种写法在本讲的 proto 里都会大量出现，是后面区分 RPC 类型的关键。

### 2.3 什么是 tonic 与 tonic-build

- **tonic** 是 Rust 生态的 gRPC 实现（基于 hyper + tokio）。运行时它负责真正的网络收发。
- **tonic-build** 是 tonic 的「编译期工具」。它在 `build.rs`（Rust 的构建脚本）里被调用，读取 `.proto`，调用底层的 `protoc`/`prost`，生成出 Rust 的消息结构体和服务 trait，写到一个由 Cargo 指定的输出目录 `OUT_DIR`。

简单说：**你手写 `.proto`，tonic-build 替你生成 Rust 代码**。本讲的核心就是看清楚这条「`.proto` → Rust」的流水线。

### 2.4 名词速查

| 名词 | 含义 |
| --- | --- |
| `message` | proto 里描述的一条数据结构，生成后对应一个 Rust 结构体。 |
| `service` | proto 里的一组 rpc，生成后对应一个 Rust trait。 |
| `optional` | proto3 字段关键字，表示「该字段可以显式标记为未设置」，需要 `--experimental_allow_proto3_optional` 才能稳定使用。 |
| `repeated` | 该字段可以出现 0 到多次，生成后对应 Rust 的 `Vec`。 |
| `OUT_DIR` | Cargo 在构建脚本运行时注入的环境变量，指向「放生成代码」的目录。 |

## 3. 本讲源码地图

本讲涉及三个关键文件，它们刚好构成「契约 → 生成 → 引入」的完整链路：

| 文件 | 作用 |
| --- | --- |
| [`proto/sglang/runtime/v1/sglang.proto`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/proto/sglang/runtime/v1/sglang.proto#L1-L314) | 契约本体：声明 `package`、`service SglangService`、以及所有 `message`。 |
| [`rust/sglang-grpc/build.rs`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/build.rs#L1-L16) | 构建脚本：用 `tonic_build::configure()` 把上面的 proto 编译成 Rust 代码。 |
| [`rust/sglang-grpc/src/lib.rs`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/lib.rs#L6-L8) | crate 入口：用 `tonic::include_proto!(...)` 把生成代码引入为 `pub mod proto`。 |

辅助参考（用来验证「生成出来的类型长什么样」）：

| 文件 | 作用 |
| --- | --- |
| [`rust/sglang-grpc/src/server.rs`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L216-L225) | 服务实现：`impl proto::sglang_service_server::SglangService`，直接展示了生成类型的实际用法。 |

> 说明：proto 文件位于仓库根的 `proto/` 目录，**不在** `rust/sglang-grpc/` crate 目录内。因此它的永久链接使用仓库根路径，而 build.rs / lib.rs 的链接使用 crate 内路径。这点和 `build.rs` 里写 `let proto_path = "../../proto/sglang/runtime/v1/sglang.proto";` 的相对路径是一致的。

## 4. 核心概念与源码讲解

本讲按三个最小模块拆分，正好对应链路的三个环节。

### 4.1 proto 契约：SglangService 服务块

#### 4.1.1 概念说明

第一块要解决的问题是：**这个 gRPC 服务对外究竟提供了哪些方法？数据结构是什么形状？**

答案全部写在一份 `.proto` 文件里。这份文件就是「契约」——服务端按它生成实现骨架，客户端按它生成调用桩（stub）。任何人想给这个服务加方法、改字段，都得先动这份契约。

在 `sglang-grpc` 里，契约把所有 rpc 分成三组：

1. **SGLang 原生 RPC**：使用强类型的 proto 消息（如 `TextGenerateRequest` → `TextGenerateResponse`），是 SGLang 自己定义的接口风格。
2. **OpenAI 兼容 RPC**：请求/响应体本身是一段原始 JSON 字节（`bytes json_body`），服务端只做「透传」，方便复用 OpenAI 客户端生态。
3. **Admin/Ops RPC**：管理类操作，如开启/停止 profiling、从磁盘更新权重。

这种分类不是 proto 语法强制的，而是用注释分块，方便阅读和维护。

#### 4.1.2 核心流程

一份 proto 文件自上而下的结构是固定的：

```
syntax = "proto3";          # 用哪个版本的语法
package <名字>;             # 这个 proto 属于哪个「命名空间」
service <服务名> {           # 一组 rpc
  rpc <方法>(<请求>) returns (<响应>);        # 一元
  rpc <方法>(<请求>) returns (stream <响应>); # 流式
}
message <消息名> { <字段> ... }   # 数据结构定义
```

其中两点对本讲尤其重要：

- `package` 决定了生成代码的「包路径」，必须和后面 `include_proto!` 的参数**逐字相同**。
- 一个 rpc 是否带 `stream` 关键字，决定了它生成出来是「返回单个 `Response`」还是「返回一个流」。

#### 4.1.3 源码精读

先看文件头部，确定语法版本与包名（这两行是整条生成链路的「身份标识」）：

[syntax 与 package](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/proto/sglang/runtime/v1/sglang.proto#L1-L2) 声明了 `syntax = "proto3"` 与 `package sglang.runtime.v1;`。后者就是稍后 `include_proto!` 必须照抄的字符串。

接着是核心的服务块：

[service SglangService](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/proto/sglang/runtime/v1/sglang.proto#L4-L35) 把全部 rpc 集中声明在一个 `service` 内。三组之间用注释分隔。

第一组，**SGLang 原生 RPC**（16 个）：

[原生 rpc 列表](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/proto/sglang/runtime/v1/sglang.proto#L5-L21)。注意 `TextGenerate` 和 `Generate` 用了 `returns (stream ...)`，是流式；其余如 `TextEmbed`、`Embed`、`Classify`、`Tokenize`、`HealthCheck`、`ListModels`、`Abort` 等都是一元。

第二组，**OpenAI 透传 RPC**（6 个）：

[OpenAI rpc 列表](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/build.rs) ← 这里给 build.rs 占位；正确链接见下一节。OpenAI 这一组请直接看 proto：[OpenAI rpc](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/proto/sglang/runtime/v1/sglang.proto#L23-L29)。其中 `ChatComplete`、`Complete` 是流式（`returns (stream OpenAIStreamChunk)`），`OpenAIEmbed`、`OpenAIClassify`、`Score`、`Rerank` 是一元。

第三组，**Admin/Ops RPC**（3 个）：

[Admin rpc 列表](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/proto/sglang/runtime/v1/sglang.proto#L31-L34)：`StartProfile`、`StopProfile`、`UpdateWeightsFromDisk`，全部一元。

合计：16 + 6 + 3 = **25 个 rpc**。其中流式 rpc 共 4 个（`TextGenerate`、`Generate`、`ChatComplete`、`Complete`），一元 rpc 共 21 个。

再看消息定义的两个典型样板，理解「类型化」与「透传」的区别。

类型化消息的代表是 [TextGenerateRequest / TextGenerateResponse](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/proto/sglang/runtime/v1/sglang.proto#L72-L93)：字段有明确的类型（`string text`、`bool finished`、`map<string,string> meta_info`、以及 `optional SamplingParams sampling_params` 这种嵌套消息）。生成后就是字段明确的 Rust 结构体，编译期就能查到拼写错误。

透传消息的代表是 [OpenAIRequest / OpenAIStreamChunk / OpenAIResponse](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/proto/sglang/runtime/v1/sglang.proto#L272-L285)：主体是 `bytes json_body`，即「把一整段 OpenAI 格式的 JSON 当作不透明字节」塞进去。服务端不解析它的字段结构，只搬运，所以叫「透传」。

最后注意 `optional` 关键字。例如 [SamplingParams](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/proto/sglang/runtime/v1/sglang.proto#L52-L68) 里几乎每个标量字段都标了 `optional float temperature = 1;`。proto3 的 `optional` 能区分「未设置」和「零值」，但这需要 protoc 开启实验特性——这正是 `build.rs` 里那条 `--experimental_allow_proto3_optional` 参数存在的原因（详见 4.2.3）。

#### 4.1.4 代码实践

**实践目标**：用肉眼在 proto 里完成一次 rpc 清点，建立对契约规模的直觉。

**操作步骤**：

1. 打开 [sglang.proto 的 service 块](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/proto/sglang/runtime/v1/sglang.proto#L4-L35)。
2. 逐行数 `rpc` 关键字出现的次数。
3. 按三段注释（`SGLang-native` / `OpenAI-compatible` / `Admin/Ops`）分别计数。
4. 在每一类内部，再区分哪些带了 `stream`、哪些没有。

**需要观察的现象**：`service` 块由上到下恰好被三行注释切成三段；流式 rpc 都集中在「生成类」与「OpenAI 聊天类」里。

**预期结果**：原生 16、OpenAI 6、Admin 3，共 25；其中流式 4 个。

> 待本地验证：如果你在本地用 `grep -c 'rpc ' sglang.proto` 计数，应得到 25。

#### 4.1.5 小练习与答案

**练习 1**：`returns (stream TextGenerateResponse)` 里的 `stream` 关键字如果删掉，会有什么后果？

> **参考答案**：该 rpc 会从「服务端流式」降级为「一元」。服务端将只能一次性返回单个 `TextGenerateResponse`，无法逐 token 边算边吐；客户端也只能等整段生成完才拿到结果，失去流式体验。生成出来的 Rust trait 方法签名也会从「返回一个流」变成「返回单个 `Response`」。

**练习 2**：`OpenAIRequest.json_body` 为什么用 `bytes` 而不是定义一堆具体字段？

> **参考答案**：因为 OpenAI 的请求/响应 JSON 字段集合会随版本和端点变化，且服务端只想把它原样转交给 Python 端处理。用 `bytes` 让 proto 不绑定 OpenAI 的具体 schema，proto 契约保持稳定，字段演化的责任留给 Python 侧——这就是「透传（pass-through）」的含义。

### 4.2 tonic-build 代码生成：build_server(true) / build_client(false)

#### 4.2.1 概念说明

proto 写好了，但它只是文本。Rust 编译器并不认识 `.proto`。需要一个「翻译官」把 proto 翻译成 Rust 代码——这就是 `build.rs` 里调用的 `tonic-build`。

`tonic-build` 提供了一个链式 API `tonic_build::configure()`，让你用一组开关精确控制「生成什么」：

- 要不要生成**服务端**骨架（trait + Server 类型）？
- 要不要生成**客户端**桩（Client 类型）？
- 要不要给 protoc 传额外参数？
- 要不要把 proto 的元信息（FileDescriptorSet）落盘？

`sglang-grpc` 这个 crate 只做服务端，不做客户端（客户端在别的语言/进程里），所以它显式地「开服务端、关客户端」。这是本节最关键的设计取舍。

#### 4.2.2 核心流程

构建脚本 `build.rs` 的执行时机是「Cargo 编译本 crate 之前」，它的工作流是：

```
Cargo 设置 OUT_DIR 等环境变量
   ↓
build.rs 调用 tonic_build::configure()
   ↓  链式配置开关：build_server / build_client / protoc_arg / file_descriptor_set_path
   ↓
.compile_protos([proto 文件], [include 根目录])
   ↓  内部调用 protoc + prost 生成 Rust 源码，写到 OUT_DIR
   ↓
println!("cargo:rerun-if-changed=...")  告诉 Cargo 何时该重跑本脚本
```

两个细节值得记住：

- `compile_protos` 的第二个参数是 **include 路径**（`&["../../proto"]`），它决定 protoc 去「哪里找被 import 的 proto」。本 proto 没有 import 别的文件，但这个路径仍然要指向 proto 所在的根，这样 `package` 路径才解析得对。
- `cargo:rerun-if-changed` 是 Cargo 的指令：**只有**当列出的文件变化时，才重跑 build.rs。这里只列了 proto 文件，不列 `.rs`，因为 `.rs` 改动只需走普通 Rust 编译，不需要重跑 protoc。

#### 4.2.3 源码精读

整个 `build.rs` 只有十几行，但每一行都对应生成链路的一环。

[build.rs 全文](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/build.rs#L1-L16) 是一份极简的构建脚本。

逐段看：

[proto_path 与 configure](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/build.rs#L2-L6) —— `let proto_path = "../../proto/sglang/runtime/v1/sglang.proto";` 用相对 crate 根的路径指向契约文件；随后 `tonic_build::configure().build_server(true).build_client(false)` 开服务端、关客户端。如果这里误写成 `build_client(true)`，就会额外生成一份本 crate 用不到的 `SglangServiceClient`，徒增编译产物体积。

[protoc_arg 与 file_descriptor_set_path](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/build.rs#L7-L11) —— `.protoc_arg("--experimental_allow_proto3_optional")` 把这个开关透传给底层 protoc，使 proto 里的 `optional` 字段（如 `SamplingParams.temperature`）能正确生成 `Option<f32>`；`.file_descriptor_set_path(...)` 让 tonic-build 把 proto 的二进制描述符（`FileDescriptorSet`）写到 `OUT_DIR/sglang_descriptor.bin`。这是一个构建产物：目前本 crate 的 `src/` 里没有引用它（即未接入运行期 gRPC reflection），保留它是为日后按需启用反射或自省留的口子。

[compile_protos 与 rerun-if-changed](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/build.rs#L12-L14) —— `.compile_protos(&[proto_path], &["../../proto"])?` 触发真正的生成；`println!("cargo:rerun-if-changed={}", proto_path)` 声明「只有 proto 变了才重跑」，避免改一个 `.rs` 就触发一次 protoc，加快增量构建。

> 注意 `?`：`build.rs` 的 `main()` 返回 `Result`，生成失败会把错误直接冒泡成构建失败，让 proto 语法错误在编译期就暴露。

#### 4.2.4 代码实践

**实践目标**：搞清楚「生成出来的东西到底落在哪里、长什么样」，建立「proto 改动如何传导到 Rust」的直觉。

**操作步骤**：

1. 在 `rust/sglang-grpc/` 下执行一次构建（例如 `cargo build`，或在 Python 端走 `SGLANG_BUILD_RUST_EXTS=grpc` 打包流程，见 u1-l2）。
2. 到构建输出目录寻找生成文件。它通常在 `target/<profile>/build/<crate-hash>/out/` 下（即 Cargo 注入的 `OUT_DIR`），文件名与 package 相关。
3. 在生成代码里搜 `struct TextGenerateRequest` 与 `trait SglangService`，确认它们确实是机器生成的。
4. 对比 `build_client(false)`：确认生成的代码里**没有** `SglangServiceClient`，只有服务端那一套。

**需要观察的现象**：改 `.proto`（比如给某个 message 加一个字段）再构建，生成文件会更新；只改 `server.rs`，则不会触发 build.rs 重跑（这正是 `rerun-if-changed` 的效果）。

**预期结果**：生成代码里能看到与 proto 一一对应的消息结构体，以及一个 `sglang_service_server` 子模块（见 4.3）。

> 待本地验证：`OUT_DIR` 的确切哈希路径取决于机器，需到 `target/` 下实际查找。

#### 4.2.5 小练习与答案

**练习 1**：如果本 crate 以后需要既当服务端又当客户端（例如内部还要调别的 gRPC 服务），`build.rs` 要怎么改？

> **参考答案**：把 `.build_client(false)` 改成 `.build_client(true)`。其余不变。这样会多生成一个 `sglang_service_client` 子模块（含 `SglangServiceClient` 类型），供本 crate 发起出站 gRPC 调用。

**练习 2**：`cargo:rerun-if-changed` 为什么只声明 proto 文件，而不声明 `src/` 下的 `.rs`？

> **参考答案**：`rerun-if-changed` 控制「何时重跑 build 脚本」；build 脚本唯一的产物（生成代码）只依赖 proto。`.rs` 文件的编译由 Cargo 常规流程负责，与 build 脚本无关。若把 `.rs` 也列进去，每次改源码都会无谓地重跑 protoc，拖慢增量构建。

### 4.3 proto 模块导入：tonic::include_proto!

#### 4.3.1 概念说明

tonic-build 把生成代码写进了 `OUT_DIR`——那只是一个磁盘上的临时目录。Rust 源码要怎么「看见」这些生成的类型？桥梁就是 `tonic::include_proto!` 宏。

这个宏的名字虽然叫 `include`，但它不是 `include_str!` 那种「把文件内容当字符串塞进来」。它的真正职责是：**用给定的 package 名，把 tonic-build 生成的那份 Rust 代码 `mod` 进当前文件**。所以它只接受一个参数——proto 的 `package` 名——而且必须和 proto 里写的**一模一样**（包括点号）。

#### 4.3.2 核心流程

导入流程极简，但有个值得强调的「包名一致性」约束：

```
proto 声明:    package sglang.runtime.v1;
                                  │
                                  │ 必须逐字相同
                                  ▼
Rust 引入:  tonic::include_proto!("sglang.runtime.v1")
                                  │
                                  │ 展开为：把 OUT_DIR 里对应生成代码纳入当前模块
                                  ▼
            外层 pub mod proto { ... }  → 生成类型暴露在 proto::* 下
```

导入之后，生成代码里有两类东西会被 `server.rs` 使用：

- 各 `message` 对应的结构体：直接出现在 `proto` 模块下，如 `proto::TextGenerateRequest`。
- `service` 对应的 trait：被放进一个名为「服务名蛇形小写 + `_server`」的子模块里，如服务 `SglangService` → `proto::sglang_service_server::SglangService`。

#### 4.3.3 源码精读

导入点只有三行，位于 crate 入口：

[proto 模块声明](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/lib.rs#L6-L8) —— 用 `pub mod proto { tonic::include_proto!("sglang.runtime.v1"); }` 把生成代码包进一个公开模块 `proto`。注意：

1. 宏参数 `"sglang.runtime.v1"` 与 proto 第 2 行的 `package sglang.runtime.v1;` **逐字相同**。若写成 `"sglang"` 或漏掉某一段，宏会找不到生成文件，编译报错。
2. 外层是 `pub mod proto`，所以整个 crate 都能用 `crate::proto::...` 或 `proto::...` 访问生成类型。

生成的 trait 长什么样？直接看 `server.rs` 怎么用最清楚：

[impl proto::sglang_service_server::SglangService](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L216-L225) —— 服务实现 `impl proto::sglang_service_server::SglangService for SglangServiceImpl`。从这里可以反推 tonic-build 的命名规则：proto 里的 `service SglangService` 生成出子模块 `sglang_service_server`，其内部定义了同名 trait `SglangService`（以及一个可直接挂到 tonic Server 的 `SglangServiceServer` 类型）。

这条路径里的几个细节：

- `sglang_service_server` 是「服务名转蛇形 + `_server` 后缀」。因为 `build_client(false)`，所以**没有**对应的 `sglang_service_client` 子模块。
- 流式 rpc 在 trait 里体现为一个**关联类型**：[type TextGenerateStream](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L220-L220) `type TextGenerateStream = StreamResult<proto::TextGenerateResponse>;`，方法 `text_generate` 则返回 `Result<Response<Self::TextGenerateStream>, Status>`。这条规则就是把 proto 里 `returns (stream ...)` 翻译成 Rust 的方式：用关联类型声明「我这个流吐的是什么」。
- 一元 rpc 则没有关联类型，直接 [返回单个 Response](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L559-L572)，如 `health_check` 返回 `Result<Response<proto::HealthCheckResponse>, Status>`。

`server.rs` 顶部的 [use crate::proto;](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L15-L15) 以及 [type StreamResult](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L26-L26) 共同把生成类型组织起来：`StreamResult<T>` 就是 `Pin<Box<dyn Stream<Item = Result<T, Status>> + Send + 'static>>` 的别名，专门用来填上面那些流式 rpc 的关联类型。

> 一句话总结命名映射（可在脑中默背）：
> - proto `message Foo` → Rust `proto::Foo`（结构体）。
> - proto `service Bar` → Rust `proto::bar_server::Bar`（trait）+ `proto::bar_server::BarServer`（可注册的服务类型）。
> - proto `rpc M(Req) returns (Resp)` → Rust `async fn m(&self, req: Request<proto::Req>) -> Result<Response<proto::Resp>, Status>`。
> - proto `rpc M(Req) returns (stream Resp)` → 多一个 `type MStream = ...;`，且 `m` 返回 `Result<Response<Self::MStream>, Status>`。

#### 4.3.4 代码实践

**实践目标**：通过「故意改错」来验证 `include_proto!` 的参数必须与 package 一致。

**操作步骤**：

1. 打开 [src/lib.rs 的 proto 模块](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/lib.rs#L6-L8)。
2. **仅做思想实验**（不要真的改源码并提交）：假设把宏参数改成 `tonic::include_proto!("sglang")`，预测编译结果。
3. 对照 proto 第 2 行的 `package sglang.runtime.v1;`，确认参数少了后缀。
4. 阅读生成代码（参见 4.2.4 找到 `OUT_DIR` 文件），注意文件名与完整 package 的对应关系。

**需要观察的现象**：宏会去 `OUT_DIR` 找与参数对应的生成文件；参数不匹配时找不到该文件。

**预期结果**：构建失败，错误形如「找不到 package `sglang` 对应的生成代码」（具体措辞待本地验证）。改回 `"sglang.runtime.v1"` 即恢复。

> 这是一个纯阅读型实践，不需要真正修改源码；它用来固化「proto package 名 ⇄ include_proto! 参数」必须一致这条硬约束。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `include_proto!` 要放在 `pub mod proto { ... }` 里面，而不是直接写在 `lib.rs` 顶层？

> **参考答案**：包进 `pub mod proto` 可以把所有生成类型收拢到一个命名空间下，避免它们和 crate 自身的类型（如 `bridge`、`server`）撞名，也便于在别处用 `proto::TextGenerateRequest` 这种清晰前缀引用。如果直接展开在顶层，几十个生成结构体会污染 crate 根命名空间。

**练习 2**：proto 里没有 `service`，只有 `message` 时，`include_proto!` 还能正常工作吗？`sglang_service_server` 子模块还会出现吗？

> **参考答案**：只要 `package` 名一致，`include_proto!` 仍能正常导入消息结构体。但若 proto 里没有 `service SglangService`，tonic-build 就不会生成 `sglang_service_server` 子模块——它只对存在且 `build_server(true)` 的 service 才生成服务端 trait。本 crate 同时有 service 和 `build_server(true)`，所以两者都在。

## 5. 综合实践

本任务把三个最小模块串起来：先读懂契约全貌，再规划一次「最小新增」需要动哪些文件。

### 5.1 第一步：清点契约

打开 [sglang.proto 的 service 块](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/proto/sglang/runtime/v1/sglang.proto#L4-L35)，完成下面这张表（答案已给出，请先自己数再核对）：

| 分类 | rpc 数 | 其中流式 | 其中一元 |
| --- | --- | --- | --- |
| SGLang 原生（第 5–21 行） | 16 | 2（TextGenerate、Generate） | 14 |
| OpenAI 透传（第 23–29 行） | 6 | 2（ChatComplete、Complete） | 4 |
| Admin/Ops（第 31–34 行） | 3 | 0 | 3 |
| **合计** | **25** | **4** | **21** |

### 5.2 第二步：规划「新增一个最简单的一元 RPC」

假设要新增 `rpc Ping(PingRequest) returns (PingResponse);`——一个只回声的探针方法。请回答三处分别要改什么：

**（1）proto**：必须改。
- 在 [service 块](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/proto/sglang/runtime/v1/sglang.proto#L4-L35) 里加一行 `rpc Ping(PingRequest) returns (PingResponse);`。
- 在文件中新增两个 `message`：`PingRequest` 和 `PingResponse`（例如后者含一个 `string message = 1;`）。
- 由于 `package` 不变，`include_proto!` 的参数**不需要**改。

**（2）build.rs**：**不需要手改**。
- 因为 [build.rs](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/build.rs#L12-L14) 的 `compile_protos` 是「整文件」编译，并不硬编码 rpc 列表；且 `rerun-if-changed` 已盯住 proto 文件，proto 一改就会自动重新生成 Rust 代码。所以新 rpc 会自动出现在生成的 trait 里。

**（3）server.rs**：必须改。
- tonic-build 会在 `proto::sglang_service_server::SglangService` 这个 trait 上**新增一个必须实现的方法** `ping`。如果不实现，[impl ... for SglangServiceImpl](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L216-L217) 就会因为「trait 方法未实现」而编译失败。
- 你需要补一个 `async fn ping(&self, req: Request<proto::PingRequest>) -> Result<Response<proto::PingResponse>, Status>`，返回值里填上想要的回声内容。
- （若新方法需要走推理路径，还要顺带在 `utils/request_utils.rs` 加一个 `build_ping_dict`、并在 bridge 侧处理；但「最简单的探针」往往不需要。）

**预期结论**：一次最小新增 = **改 proto + 改 server.rs**，build.rs 由于代码生成的「全文件、自动重跑」设计而**免改**。这正是 tonic-build 把「契约演化」自动化掉的价值。

> 待本地验证：可在本地分支上真正加一次 `Ping`，用 `cargo build` 观察到「未实现 trait 方法」的编译错误，再补上 `ping` 实现使其通过。

## 6. 本讲小结

- `proto/sglang/runtime/v1/sglang.proto` 是整套服务的**契约**：`package sglang.runtime.v1` + `service SglangService`，共 **25 个 rpc**，分原生（16）、OpenAI 透传（6）、Admin（3）三类，其中 4 个是流式（`returns (stream ...)`）。
- 契约演化靠 `build.rs` 的 [tonic_build::configure()](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/build.rs#L4-L12) 自动完成：`.build_server(true).build_client(false)` 只生成服务端骨架；`--experimental_allow_proto3_optional` 让 proto3 的 `optional` 字段生效；`rerun-if-changed` 只盯 proto，增量构建更快。
- 生成代码通过 [tonic::include_proto!("sglang.runtime.v1")](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/lib.rs#L6-L8) 引入为 `pub mod proto`；参数必须与 `package` 逐字一致。
- 命名映射要记牢：`message Foo` → `proto::Foo`；`service Bar` → `proto::bar_server::Bar`（本 crate 无 `bar_client`，因为 `build_client(false)`）；流式 rpc 在 trait 里多一个 `type MStream` 关联类型。
- 新增一个最简一元 rpc 只需改 **proto + server.rs** 两处，`build.rs` 因为「全文件编译 + 自动重跑」而无需手改。

## 7. 下一步学习建议

本讲只解决了「契约与代码生成」，即 trait 和消息结构是**怎么来的**。接下来的讲义应当回答「**怎么实现**这些 trait 方法」：

- **u2-l2 服务实现总览**：精读 [server.rs](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L216-L217) 里 `impl proto::sglang_service_server::SglangService for SglangServiceImpl` 的整体布局，把本讲看到的「trait 方法签名」逐一对应到真实实现。
- **u2-l3 流式 RPC**：深入 `text_generate`/`generate`/OpenAI 流式方法，看 `type XxxStream` 关联类型如何被填成一个 `async_stream` 流。
- **u2-l4 一元 RPC 与 JSON 解析**：看一元方法如何收单个终止 chunk 并把 Python 返回的 JSON 解析回 proto 响应。

建议在进入 u2-l2 前，再回看一遍本讲的「命名映射」小结——它会让你在阅读 `server.rs` 时一眼认出哪些符号是 tonic-build 生成的。
