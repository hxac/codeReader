# 源码地图：目录结构与 feature→模块映射

## 1. 本讲目标

学完本讲后，你应该能够：

- 说出 `src/` 目录下每一个源码文件的职责。
- 看懂 `src/lib.rs` 中用 `#[cfg(feature = "...")]` 守卫的模块声明，知道每个文件在什么 feature 组合下才会被编译。
- 读懂 `Cargo.toml` 的 `[features]` 表，理解 feature 之间的传递依赖（例如 `broker-rpc` 自动带出 `broker` + `rpc`）。
- 在拿到任何一个功能需求时，能快速定位「应该去读哪个文件」。

本讲只建立**导航地图**，不深入任何单个模块的实现细节——那是后续讲义的任务。前置讲义 [u1-l1](u1-l1-project-overview.md) 已经介绍了 BUS/RT 的定位与 feature 的基本概念，[u1-l2](u1-l2-build-and-run.md) 讲了如何编译运行；本讲把「feature」和「磁盘上的源码文件」一一对应起来。

## 2. 前置知识

### 2.1 什么是 Cargo feature

Cargo 的 feature 是一种**条件编译开关**。在 `Cargo.toml` 里声明一个 feature，就可以在代码里用：

```rust
#[cfg(feature = "broker")]
pub mod broker;
```

表示「只有当用户启用了 `broker` 这个 feature 时，`broker` 模块才会被编译进最终产物」。这样做的好处是：只用客户端的人不需要拖入服务端那一大堆依赖（TLS、WebSocket 等），编译更快、产物更小。

### 2.2 条件编译的几种常见写法

在 `lib.rs` 里你会反复看到下面这些写法，理解它们是本讲的核心：

| 写法 | 含义 |
|------|------|
| `#[cfg(feature = "x")]` | 仅当启用 `x` 时编译 |
| `#[cfg(any(feature = "a", feature = "b"))]` | 启用 `a` **或** `b` 任一个即编译 |
| `#[cfg(all(feature = "a", not(feature = "b")))]` | 同时满足「启用 a」且「未启用 b」 |

> 术语提示：`any(...)` 相当于逻辑「或」，`all(...)` 相当于逻辑「与」，`not(...)` 相当于「非」。

### 2.3 传递依赖

一个 feature 可以「包含」其他 feature。例如 `broker-rpc = ["broker", "rpc", "dep:rmp-serde"]` 表示：一旦启用 `broker-rpc`，就自动启用 `broker` 和 `rpc` 两个 feature，并引入 `rmp-serde` 这个依赖。因此你只要写 `--features broker-rpc`，就能一次性拿到服务端 + RPC 全套能力。这个机制是理解「为什么有些模块看起来没有直接对应的 feature 却能编译」的关键。

---

## 3. 本讲源码地图

本讲只依赖两个文件，但它们是整个项目的「目录索引」：

| 文件 | 作用 |
|------|------|
| [src/lib.rs](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs) | 库的入口。前半部分放协议常量与核心类型（始终编译），后半部分用 `#[cfg(feature)]` 声明各子模块（按 feature 条件编译）。 |
| [Cargo.toml](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/Cargo.toml) | 定义所有 feature、它们的传递依赖，以及 `busrtd` / `busrt` 两个二进制的门控条件。 |

为方便对照，下面是 `src/` 下全部源码文件的清单（本讲会逐一解释它们归属哪个 feature）：

```
src/
├── lib.rs              # 入口：常量 + 核心类型 + 模块声明
├── borrow.rs           # 零拷贝 Cow
├── common.rs           # 公共辅助
├── broker.rs           # 服务端核心
├── ipc.rs              # 异步 IPC 客户端
├── client.rs           # AsyncClient trait
├── comm.rs             # TtlBufWriter 缓冲写入
├── cursors.rs          # 游标流式传输
├── server.rs           # busrtd 二进制入口
├── cli.rs              # busrt CLI 二进制入口
├── rpc/
│   ├── mod.rs          # RPC 协议层（常量 + RpcEvent）
│   └── async_client.rs # 异步 RpcClient
├── sync/
│   ├── mod.rs          # 同步模块入口
│   ├── client.rs       # SyncClient trait
│   ├── ipc.rs          # 同步 IPC 客户端
│   └── rpc.rs          # 同步 RPC
└── tools/
    └── pubsub.rs       # TopicBroker 辅助工具
```

---

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：先看「始终编译」的协议常量与重导出，再看「按 feature 条件编译」的模块声明，最后看 `Cargo.toml` 里 feature 是如何定义和互相依赖的。

### 4.1 协议常量与重导出：永远在场的地基

#### 4.1.1 概念说明

无论你启用哪些 feature，BUS/RT 都有一部分代码**一定会被编译**：这就是 `lib.rs` 顶部的协议常量、核心类型和一些类型别名。它们是整个库的「公共契约」——例如客户端和服务端必须对「发布操作码是 0x01」「错误码 0x78 表示超时」达成一致，这些常量因此不能依赖任何 feature。

理解这一点很重要：即使你只用 `busrt` 作为最轻量的客户端，这些常量和 `Frame`、`Error`、`QoS` 等类型也始终可用。

#### 4.1.2 核心流程

`lib.rs` 顶部的内容大致分四组：

1. **操作码常量** `OP_*`：定义帧的操作类型（发布、订阅、消息、广播、ACK 等），是线上协议的「动词」。
2. **协议元常量**：`PROTOCOL_VERSION`（协议版本号）、`GREETINGS`（握手魔数 `0xEB`）、`PING_FRAME`（9 字节心跳帧）。
3. **错误码常量** `ERR_*`：与 `ErrorKind` 枚举一一对应，用于把 `u8` 错误码翻译成可读错误。
4. **默认值常量**：`DEFAULT_TIMEOUT`、`DEFAULT_BUF_SIZE`、`DEFAULT_QUEUE_SIZE` 等，给客户端和服务端提供统一默认值。

紧随其后的是一批**类型别名**（re-export），其中一部分被 feature 守卫——它们是「连接各模块的桥梁」，留到 4.2 再细讲。

#### 4.1.3 源码精读

操作码常量，定义了线上帧的所有「动作」——注意 `OP_ACK = 0xFE` 与错误码区段刻意拉开距离，便于协议解析时区分：

[src/lib.rs:10-19](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L10-L19) — 这段定义了 `OP_NOP`/`OP_PUBLISH`/`OP_SUBSCRIBE`/`OP_MESSAGE`/`OP_BROADCAST`/`OP_ACK` 等操作码，是后续 `FrameOp`、`FrameKind` 枚举的底层取值。

协议元常量，握握与心跳都依赖它们：

[src/lib.rs:21-25](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L21-L25) — `PROTOCOL_VERSION`、`RESPONSE_OK`、`PING_FRAME`。客户端与服务端建立连接时先交换 `GREETINGS` 与版本号，再用 `PING_FRAME` 做心跳。

错误码常量，与 `ErrorKind` 一一映射：

[src/lib.rs:27-35](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L27-L35) — `ERR_CLIENT_NOT_REGISTERED` 到 `ERR_ACCESS`，这些 `u8` 值就是线上传输的错误码，`ErrorKind::from(u8)` 会把它们翻译回枚举（见 [src/lib.rs:106-119](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L106-L119)）。

默认值常量，给客户端与服务端共享：

[src/lib.rs:43-49](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L43-L49) — `DEFAULT_TIMEOUT`、`DEFAULT_BUF_TTL`、`DEFAULT_BUF_SIZE`、`DEFAULT_QUEUE_SIZE`，以及二级客户端分隔符 `SECONDARY_SEP = "%%"`。

始终编译的核心类型别名（无 feature 守卫）：

[src/lib.rs:71-72](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L71-L72) — `Frame = Arc<FrameData>` 和 `EventChannel = async_channel::Receiver<Frame>`。无论用哪个 feature，帧都以 `Arc<FrameData>` 的形式在系统里流动。

> 顺带一提：文件最顶部 [src/lib.rs:1](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L1) 用 `#![doc = include_str!(... "README.md")]` 把 README 当作 crate 文档，所以 `cargo doc` 生成的说明就是 README 内容。

#### 4.1.4 代码实践

**目标**：验证「常量层」始终编译、不依赖任何 feature。

**操作步骤**：

1. 用最少的依赖创建一个临时 crate（或在 examples 里加一个小文件）。
2. 在 `Cargo.toml` 里引入 busrt，**不开启任何 feature**：

   ```toml
   [dependencies]
   busrt = { path = "..", default-features = false }
   ```

3. 写一段只引用常量的代码：

   ```rust
   // 示例代码：不启用任何 feature，仅引用始终编译的常量
   use busrt::{OP_PUBLISH, PROTOCOL_VERSION, GREETINGS, ERR_TIMEOUT};
   fn main() {
       println!("PUBLISH op = 0x{:02x}", OP_PUBLISH);
       println!("protocol version = 0x{:04x}", PROTOCOL_VERSION);
       println!("greetings = {:02x?}", GREETINGS);
       println!("timeout err = 0x{:02x}", ERR_TIMEOUT);
   }
   ```

4. 执行 `cargo build`（不传 `--features`）。

**需要观察的现象**：即使没有任何 feature，这行代码也能编译通过，证明这些常量不在任何 `#[cfg(feature)]` 守卫之内。

**预期结果**：编译成功，打印出 `PUBLISH op = 0x01`、`protocol version = 0x0001` 等。若尝试在同一程序里 `use busrt::broker::Broker;`，则**会编译失败**（因为 `broker` 模块被 feature 守卫）——这正是 4.2 要讲的内容。

> 待本地验证：不同 Rust 版本下 `default-features = false` 的行为一致；若你机器上 busrt 已被其他 feature 拉入，可在一个全新的空 crate 里验证以排除干扰。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `OP_ACK = 0xFE` 和错误码（`0x71`~`0x79`）要分在不同的数值区段？
**参考答案**：协议解析时可以用数值范围快速区分「这是一个确认帧」还是「这是一个错误响应」，避免歧义；也便于将来扩展新的操作码而不会和错误码撞车。

**练习 2**：`ErrorKind::Eof = 0xff`（见 [src/lib.rs:103](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L103)）为什么没有对应的 `ERR_*` 常量？
**参考答案**：`Eof` 表示连接结束（如对端关闭），是**本地**状态，不通过线上协议传输，所以不需要在线上错误码区段里占一个值。

---

### 4.2 模块的条件声明：feature→文件映射的核心

#### 4.2.1 概念说明

`lib.rs` 文件后半部分（约第 502 行起）是整个项目的「模块调度台」。每写一行 `pub mod xxx;`，就在告诉编译器「把 `src/xxx.rs`（或 `src/xxx/` 目录）纳入编译」。关键在于：**绝大多数模块前面都带有 `#[cfg(feature = "...")]` 守卫**，于是「启用哪个 feature」就等于「编译哪些文件」。

掌握这张映射表，你就能在任何功能需求下迅速定位源码：想看服务端就去 `broker.rs`（feature `broker`）；想看异步客户端就去 `ipc.rs`（feature `ipc`）；想看 RPC 就去 `rpc/`（feature `rpc` 或 `rpc-sync`）。

#### 4.2.2 核心流程

模块声明的判定逻辑可以用下面这段伪代码概括（对应 `lib.rs` 第 502–523 行）：

```text
始终编译:
    borrow        # 零拷贝 Cow，无依赖
    common        # 公共辅助

if any(rpc, broker, ipc):
    tools/pubsub  # TopicBroker 工具
    client        # AsyncClient trait（统一客户端接口）

if any(broker, ipc):
    comm          # TtlBufWriter 缓冲写入

if broker:    broker   # 服务端核心
if cursors:   cursors  # 游标流式传输
if ipc:       ipc      # 异步 IPC 客户端
if any(rpc, rpc-sync):  rpc   # RPC 协议层
if any(ipc-sync, rpc-sync): sync  # 同步客户端
```

注意几个「需要两个 feature 之一」的模块（`any(...)`），它们的存在说明：**有的能力被异步和同步两条线共用**。例如 `rpc` 模块在「异步 RPC（`rpc`）」和「同步 RPC（`rpc-sync`）」任一启用时都要编译，因为两者共用同一套 RPC 协议常量与 `RpcEvent` 解析逻辑。

#### 4.2.3 源码精读

始终编译的两个模块，没有任何 feature 守卫：

[src/lib.rs:502-503](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L502-L503) — `pub mod borrow;` 与 `pub mod common;`，它们是所有 feature 组合下都在场的基础设施。

`tools` 是一个内联模块，里面的 `pubsub` 子模块被 `any(rpc, broker, ipc)` 守卫：

[src/lib.rs:504-507](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L504-L507) — `tools::pubsub`（即 `src/tools/pubsub.rs` 里的 `TopicBroker`）只在启用了 rpc/broker/ipc 之一时才编译。

四个「单一 feature」守卫的核心模块：

[src/lib.rs:509-514](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L509-L514) — `broker`（需要 feature `broker`）、`cursors`（需要 `cursors`）、`ipc`（需要 `ipc`）。这是最直观的「文件名 ≈ feature 名」映射。

「异步/同步共用」的 RPC 与 sync 模块，用 `any(...)` 守卫：

[src/lib.rs:515-518](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L515-L518) — `rpc` 模块在 `rpc` 或 `rpc-sync` 任一启用时编译；`sync` 模块在 `ipc-sync` 或 `rpc-sync` 任一启用时编译。

被「通信类 feature 共用」的 `client` 与 `comm`：

[src/lib.rs:520-523](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L520-L523) — `client`（`AsyncClient` trait）在 rpc/broker/ipc 任一启用时编译；`comm`（`TtlBufWriter`）在 broker 或 ipc 启用时编译。

> **二级守卫的陷阱**：模块声明只是「进门门票」，模块**内部**还可能对子模块再套一层 feature 守卫。最典型的两处：
> - `rpc/mod.rs` 内部对异步客户端再守卫：[src/rpc/mod.rs:4-5](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/mod.rs#L4-L5) 中 `mod async_client;` 仅在 `feature = "rpc"` 时编译。所以**只开 `rpc-sync` 时**，`rpc/` 模块会编译（拿到协议常量与 `RpcEvent`），但 `rpc/async_client.rs`（异步 `RpcClient`）**不会**编译。
> - `sync/mod.rs` 内部对同步 RPC 再守卫：[src/sync/mod.rs:3-4](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/sync/mod.rs#L3-L4) 中 `pub mod rpc;` 仅在 `feature = "rpc-sync"` 时编译。所以**只开 `ipc-sync` 时**，`sync/client.rs` 与 `sync/ipc.rs` 会编译，但 `sync/rpc.rs` **不会**编译。
>
> 这个细节解释了为什么 `rpc` 模块的守卫是 `any(rpc, rpc-sync)` 而不是单独的 `rpc`：同步 RPC 复用了 `rpc/mod.rs` 里的协议层，却不引入异步客户端那一整套 tokio 依赖。

文件末尾还有一个始终导出的宏：

[src/lib.rs:525-530](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L525-L530) — `empty_payload!` 宏展开为 `borrow::Cow::Borrowed(&[])`，是构造「空载荷」的快捷方式，后续讲义会反复用到。

#### 4.2.4 代码实践

**目标**：用编译器的报错信息反向验证「模块↔feature」映射。

**操作步骤**：

1. 准备一个依赖 busrt 的 crate，**只开启 `ipc` feature**：

   ```toml
   [dependencies]
   busrt = { path = "..", default-features = false, features = ["ipc"] }
   ```

2. 依次尝试在代码里 `use` 下列路径，每试一个就 `cargo check` 一次：

   ```rust
   use busrt::ipc::Client;        // 预期：成功（ipc 已启用）
   use busrt::client::AsyncClient;// 预期：成功（client 守卫 any(rpc,broker,ipc)，ipc 命中）
   use busrt::comm::Flush;        // 预期：成功（comm 守卫 any(broker,ipc)，ipc 命中）
   use busrt::broker::Broker;     // 预期：失败（broker 未启用）
   use busrt::rpc::RpcClient;     // 预期：失败（rpc 未启用）
   use busrt::cursors::Cursor;    // 预期：失败（cursors 未启用）
   ```

3. 把 `features` 换成 `["broker-rpc"]`，重复上面的 `use`，观察哪些从「失败」变成「成功」。

**需要观察的现象**：`broker-rpc` 会经传递依赖（见 4.3）同时拉入 `broker` 和 `rpc`，于是 `broker::Broker`、`rpc::RpcClient` 都能编译通过；但 `cursors::Cursor` 仍然失败——因为 `cursors` 不在 `broker-rpc` 的依赖链里。

**预期结果**：你得到一张「以编译器为裁判」的实测映射表，与本讲 4.2.3 的源码结论完全一致。若与预期不符，先检查 `default-features = false` 是否生效。

> 待本地验证：`cargo check` 的实际错误消息措辞因版本而异，但「找不到该项」的结论稳定。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `client` 模块的守卫是 `any(rpc, broker, ipc)` 而不是 `all(rpc, broker, ipc)`？
**参考答案**：`AsyncClient` trait 是统一接口，**任何一种**客户端场景（纯 RPC、嵌入式 broker、IPC 客户端）都需要它，所以用「或」；若用「与」则必须同时开三个 feature 才能用，违背了模块化设计的初衷。

**练习 2**：如果只想用**同步**客户端（不开任何异步 feature），最少需要开哪些 feature 才能让 `sync::ipc::Client` 可用？`sync::rpc` 呢？
**参考答案**：`sync::ipc::Client` 只需 `ipc-sync`（`sync` 模块守卫 `any(ipc-sync, rpc-sync)` 命中）。而 `sync::rpc` 需要额外开 `rpc-sync`（`sync/mod.rs` 内部对 `rpc` 子模块单独守卫 `rpc-sync`）。

---

### 4.3 Cargo.toml 的 `[features]`：feature 定义与传递依赖

#### 4.3.1 概念说明

4.2 讲了「模块被哪些 feature 守卫」，但 feature 本身是怎么定义的？哪些 feature 会自动带出别的 feature？这些答案都在 `Cargo.toml` 的 `[features]` 表里。这一节解决一个常见困惑：**为什么有时候我没显式开某个 feature，相关模块却也能编译？**——答案是「传递依赖」。

同时，`Cargo.toml` 里还定义了两个二进制 `busrtd` 和 `busrt` 的门控条件，这决定了「构建服务端/CLI 需要开什么 feature」。

#### 4.3.2 核心流程

`[features]` 表的阅读规则：

1. 每一行形如 `name = ["dep:xxx", "other-feature", "xxx/some-cfg"]`。
2. 列表里的 `"other-feature"` 表示「启用本 feature 时，自动也启用 `other-feature`」（传递依赖）。
3. `"dep:xxx"` 表示引入名为 `xxx` 的可选依赖（注意 `dep:` 前缀，它只引入依赖、不隐含开启同名 feature）。
4. `"submap/digest"` 这种写法表示「启用 `submap` 依赖的 `digest` feature」。

把 4.2 的「模块↔feature」和本节的「feature↔feature」两张图叠在一起，就能从「我开了 X」一路推导到「哪些源码文件会编译」。

#### 4.3.3 源码精读

核心通信 feature 的定义——注意它们各自引入的依赖差异：

[src/Cargo.toml:69-80](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/Cargo.toml#L69-L80) — 这里定义了 `broker`、`broker-rpc`、`ipc`、`ipc-sync`、`rpc`、`rpc-sync`。重点看 `broker-rpc = ["broker", "rpc", "dep:rmp-serde"]`：它把 `broker` 和 `rpc` **都**拉了进来，所以开 `broker-rpc` 等于「服务端 + RPC 全家桶」。

`full` 与几个「扩展」feature：

[src/Cargo.toml:84-89](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/Cargo.toml#L84-L89) — `full = ["rpc", "ipc", "broker", "broker-rpc", "ipc-sync"]`。**关键陷阱**：`full` 并不包含 `cursors`、`rpc-sync`、`rt`、`tracing`，也不含 `server`/`cli`。也就是说，即使开 `full`，`src/cursors.rs` 和 `src/sync/rpc.rs` 仍然不会被编译——它们需要额外开 `cursors` 或 `rpc-sync`。

两个二进制的门控——和库 feature 分属不同维度：

[src/Cargo.toml:96-104](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/Cargo.toml#L96-L104) — `busrtd`（`src/server.rs`）需要 feature `server`；`busrt`（`src/cli.rs`）需要 feature `cli`。`required-features` 决定了「不开这个 feature，对应二进制根本不会被构建」。

docs.rs 用来生成文档的 feature 集合——它和 `full` **不一样**：

[src/Cargo.toml:13-15](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/Cargo.toml#L13-L15) — 文档站点用 `["broker", "ipc", "rpc", "ipc-sync", "rpc-sync", "cursors"]` 构建。注意它**包含** `rpc-sync` 和 `cursors`（所以在线文档能看到 `sync/rpc` 与 `cursors`），而 `full` 不包含——这是为什么 docs.rs 上的 API 比 `--features full` 编译出的更全。

`server` 与 `cli` 的依赖链，体现传递依赖的威力：

[src/Cargo.toml:67-68](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/Cargo.toml#L67-L68) — `server` 末尾包含 `broker-rpc`，于是 `busrtd` 自动具备 `broker` + `rpc` + `rmp-serde` 能力（与 [u1-l2](u1-l2-build-and-run.md) 讲到的「核心 RPC」呼应）。

[src/Cargo.toml:81-83](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/Cargo.toml#L81-L83) — `cli` 包含 `ipc` 和 `rpc`，加上 prettytable、bma-benchmark 等只为 CLI 用的依赖。

#### 4.3.4 代码实践

**目标**：用 `cargo tree` 直观看到 feature 的传递依赖链。

**操作步骤**：

1. 在 busrt 仓库根目录执行：

   ```bash
   # 只看启用了哪些 feature（传递展开后）
   cargo tree -e features --features server -i busrt:broker
   ```

2. 再执行另一组对比：

   ```bash
   cargo tree -e features --features full    -i busrt:rpc
   cargo tree -e features --features cursors -i busrt:rpc
   ```

3. 用 `--no-default-features --features broker-rpc` 构建一次，再用 `--features full` 构建一次，比较两者的编译产物中是否包含 `cursors` 相关符号（可选：`cargo build` 后观察是否报 `cursors` 未启用相关警告）。

**需要观察的现象**：

- `--features server` 会展开出 `broker-rpc → broker + rpc`，再展开 `broker → rustls / tungstenite / ...`。
- `--features cursors` 会自动带出 `rpc`（因为 `cursors = ["rpc", "dep:uuid"]`），所以即使你只写了 `cursors`，`rpc/` 模块也会编译。
- `--features full` **不会**带出 `cursors` 和 `rpc-sync`，所以 `src/cursors.rs`、`src/sync/rpc.rs` 不参与编译。

**预期结果**：你能在 `cargo tree` 的输出里清晰看到 `cursors → rpc`、`broker-rpc → broker, rpc`、`server → broker-rpc` 这几条传递边，与本节源码结论一致。

> 待本地验证：`cargo tree -e features` 的具体输出格式随 Cargo 版本变化；若旧版不支持 `-e features`，可用 `cargo tree --features server` 观察依赖列表是否出现 `rustls`、`tungstenite` 等（出现即证明 `broker` 被传递启用）。

#### 4.3.5 小练习与答案

**练习 1**：用户执行 `cargo build --features full`，下列文件哪些**不会**被编译？为什么？
`broker.rs`、`ipc.rs`、`cursors.rs`、`rpc/async_client.rs`、`sync/rpc.rs`、`cli.rs`。
**参考答案**：不会被编译的是 `cursors.rs`（`full` 不含 `cursors`）、`sync/rpc.rs`（`full` 含 `ipc-sync` 但不含 `rpc-sync`）、`cli.rs`（`full` 不含 `cli`，且 `cli.rs` 是 `[[bin]]` 门控）。其余三个会被编译。

**练习 2**：为什么 `cursors = ["rpc", "dep:uuid"]` 里要带 `rpc`？
**参考答案**：游标机制建立在 RPC 之上（服务端通过 RPC 方法 `next`/`next_bulk` 把数据分块推给客户端），所以 `cursors` 模块必须和 RPC 协议层一起编译；带上 `rpc` 保证用户只写 `--features cursors` 也能拿到完整能力，不必自己再显式加 `rpc`。

---

## 5. 综合实践：手绘「源码文件 → feature」映射表

把本讲三节内容串起来，完成下面这张总表（这是本讲的核心交付物）。

**任务**：对照 [src/lib.rs:502-523](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L502-L523) 的模块声明与 [src/Cargo.toml:66-104](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/Cargo.toml#L66-L104) 的 feature 定义，自己画出「每个源码文件需要哪个 feature 才编译」的对应关系表，并标注「`full` 是否能让它编译」。

下面是一份参考答案（建议你先自己填，再对照）：

| 源码文件 | lib.rs 守卫条件 | 最直接启用它的 feature | `full` 是否覆盖 |
|----------|----------------|----------------------|----------------|
| `borrow.rs` | 无（始终） | 任何组合都编译 | ✅ |
| `common.rs` | 无（始终） | 任何组合都编译 | ✅ |
| `lib.rs`（常量+核心类型） | 无（始终） | 任何组合都编译 | ✅ |
| `tools/pubsub.rs` | `any(rpc,broker,ipc)` | rpc / broker / ipc | ✅ |
| `client.rs` | `any(rpc,broker,ipc)` | rpc / broker / ipc | ✅ |
| `comm.rs` | `any(broker,ipc)` | broker / ipc | ✅ |
| `broker.rs` | `broker` | broker | ✅（经 `broker-rpc`） |
| `ipc.rs` | `ipc` | ipc | ✅ |
| `rpc/mod.rs` | `any(rpc,rpc-sync)` | rpc / rpc-sync | ✅（经 `rpc`） |
| `rpc/async_client.rs` | 模块内额外 `rpc` | rpc | ✅（经 `rpc`） |
| `sync/mod.rs`、`sync/client.rs`、`sync/ipc.rs` | `any(ipc-sync,rpc-sync)` | ipc-sync / rpc-sync | ✅（经 `ipc-sync`） |
| `sync/rpc.rs` | 模块内额外 `rpc-sync` | rpc-sync | ❌（`full` 不含 `rpc-sync`） |
| `cursors.rs` | `cursors` | cursors | ❌（`full` 不含 `cursors`） |
| `server.rs`（`busrtd` 二进制） | `[[bin]]` required `server` | server | ❌（`full` 不含 `server`） |
| `cli.rs`（`busrt` 二进制） | `[[bin]]` required `cli` | cli | ❌（`full` 不含 `cli`） |

**关键结论（务必记住）**：

1. `full` 覆盖了**库的核心**（borrow/common/lib、tools/pubsub、client、comm、broker、ipc、rpc 全套、sync 的 ipc-sync 部分），但**不覆盖** `cursors`、`sync/rpc`、`rt` 变体，也**不构建**两个二进制。
2. 想要「和 docs.rs 文档一样全」，应该用 `["broker","ipc","rpc","ipc-sync","rpc-sync","cursors"]`（见 [Cargo.toml:14](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/Cargo.toml#L14)），而不是 `full`。
3. 「文件名 ≈ feature 名」在 `broker`/`ipc`/`cursors` 上成立，但 `rpc` 与 `sync` 因为「异步/同步共用」而使用 `any(...)` 守卫，且内部还有二级守卫——这是最容易被忽略的陷阱。

**延伸练习**：试着把上表「最直接启用它的 feature」一列，再推导出「启用 `server` 后哪些文件会编译」。提示：`server → broker-rpc → broker + rpc`，再加上 `server` 自己引入的 `src/server.rs`。

## 6. 本讲小结

- `src/lib.rs` 顶部的协议常量（`OP_*`/`ERR_*`/`GREETINGS`/`PROTOCOL_VERSION` 等）与核心类型（`Frame = Arc<FrameData>`）**始终编译**，是整个库不依赖任何 feature 的公共契约。
- `lib.rs` 后半部分的模块声明（[src/lib.rs:502-523](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L502-L523)）用 `#[cfg(feature)]` 把每个源码文件挂到对应 feature 上，构成了「feature→模块」导航图。
- 多数模块是「单 feature 守卫」（如 `broker.rs→broker`），但 `rpc`/`sync` 因异步/同步共用而用 `any(...)`，且模块内部还有二级守卫（`rpc/async_client.rs` 需 `rpc`，`sync/rpc.rs` 需 `rpc-sync`）。
- `Cargo.toml` 的 `[features]` 定义了传递依赖：`broker-rpc = broker + rpc + rmp-serde`、`cursors = rpc + uuid`、`server → broker-rpc`、`cli → ipc + rpc`。
- **重要陷阱**：`full` 不含 `cursors`/`rpc-sync`/`rt`/`server`/`cli`，所以开 `full` 仍编译不到 `cursors.rs`、`sync/rpc.rs` 和两个二进制。
- 两个二进制 `busrtd`（`server`）与 `busrt`（`cli`）由 `[[bin]] required-features` 门控，与库 feature 是两个不同维度。

## 7. 下一步学习建议

有了这张源码地图，接下来的学习顺序建议如下：

1. **先打协议与类型基础**：进入第 2 单元，精读 [u2-l1 核心类型](u2-l1-core-types.md)（`Error`/`QoS`/`FrameOp`/`FrameData`）与 [u2-l2 零拷贝 Cow](u2-l2-zero-copy-cow.md)（`src/borrow.rs`），这两讲本讲只点了名，还没展开。
2. **再看线上帧格式**：[u2-l3 线上协议](u2-l3-wire-protocol.md) 会用到本讲的 `OP_*`/`GREETINGS`/`PROTOCOL_VERSION` 常量，解释它们如何在握手与帧编解码中被拼装。
3. **阅读源码时**：随时回到本讲的映射表定位文件。例如学到「RPC」就直奔 `src/rpc/`（feature `rpc`/`rpc-sync`），学到「服务端」就直奔 `src/broker.rs`（feature `broker`）。
4. **想一次性看到全部 API**：构建或阅读文档时用 `["broker","ipc","rpc","ipc-sync","rpc-sync","cursors"]`（即 docs.rs 的 feature 集），比 `full` 更完整。
