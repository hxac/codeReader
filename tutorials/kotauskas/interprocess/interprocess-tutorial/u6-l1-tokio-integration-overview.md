# Tokio 集成与异步 trait 概览

## 1. 本讲目标

本讲是「Tokio 异步集成」单元的第一讲，目标是让你从全局看清 interprocess 是**如何**把同步 IPC 原语变成「能在 Tokio 运行时里高效并发」的异步对象的。

学完本讲，你应当能够：

1. 说出 `tokio` feature 一旦启用，会额外解锁哪些类型集合（`local_socket::tokio`、`traits::tokio`、`unnamed_pipe::tokio`），以及它和 `async` feature 的依赖关系。
2. 在源码层面辨认出「同步 trait」与「异步 trait」的**并行结构**——同一套接口契约，把标准库 `Read`/`Write` 换成 Tokio 的 `AsyncRead`/`AsyncWrite`。
3. 理解为什么 interprocess 的 Tokio 类型**只能在 Tokio 运行时上下文里使用**，脱离运行时调用会发生什么（panic），以及背后的原因。

本讲**承接** u2-l3（`impmod!` 与平台后端注入的「壳/芯」分层）和 u3-l3（同步 `Stream` 的读写、拆分与重聚）。我们会反复用到这两个讲义建立的概念，不再重复展开。

---

## 2. 前置知识

在进入异步之前，请确保你理解下面几个概念（本讲不会从头讲）：

- **同步 I/O 与阻塞**：同步调用一个 `read()`，若数据没来，线程会一直卡住，直到有数据或出错。多个并发连接就需要多个线程各自阻塞。
- **异步 I/O 与事件循环**：异步把「等待」这件事交给运行时的**事件循环（reactor）**托管。程序向运行时登记「这个句柄可读了请叫我」，然后去做别的事；可读时运行时唤醒对应的任务。这样**一个线程可以并发处理成千上万个连接**。
- **Future 与 `.await`**：Rust 的异步基于 `Future` trait。`.await` 表示「在这里挂起，把控制权交还给运行时，等这个 Future 就绪后再恢复」。
- **Tokio**：Rust 生态中最主流的异步运行时。它提供 reactor、线程池调度、定时器，以及一套异步 I/O trait（`AsyncRead`/`AsyncWrite`）。
- **interprocess 的「壳/芯」分层**（来自 u2-l3）：公共模块是「壳」（newtype 或 enum），真正的系统调用在 `os::unix`/`os::windows` 的后端「芯」里，由 `impmod!` 宏按平台注入。
- **同步 `Stream` 的接口契约**（来自 u3-l3）：`traits::Stream` 用 `Read + RefRead + Write + RefWrite + StreamCommon` 作为 supertrait，既支持按值、也支持按引用读写。

如果你对这些还比较陌生，建议先回到 u2-l3 和 u3-l3 复习。本讲的核心动作是：**把上面这套同步契约「翻译」成异步版本**，其余机制几乎原样复用。

> 为什么 interprocess 要单独做异步？库自己的文档说得很直白：异步版本能让 local socket 流/监听器「由 OS 内核在可收可发时主动通知」，从而不必为了把对象放进等待状态而专门开线程。这句话出自模块级文档，见 [src/local_socket.rs:124-128](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket.rs#L124-L128)。

---

## 3. 本讲源码地图

本讲涉及的文件不多，但跨越「公共门控 → trait 定义 → newtype 壳 → 平台后端芯 → 构建器入口 → 示例」整条链路。

| 文件 | 作用 |
|------|------|
| `Cargo.toml` | 定义 `async`、`tokio` 两个 feature 及其依赖链。 |
| `src/lib.rs` | crate 根，组织公共模块与 `os` 平台模块。 |
| `src/local_socket.rs` | local socket 模块根；声明 `traits::tokio` 子模块与 `pub mod tokio` 子模块，并给出「只能在 Tokio 运行时使用」的关键文档。 |
| `src/local_socket/tokio/stream/trait.rs` | 异步 `Stream`/`RecvHalf`/`SendHalf` trait 定义（本讲的核心）。 |
| `src/local_socket/tokio/listener/trait.rs` | 异步 `Listener` trait 定义。 |
| `src/local_socket/tokio/stream/enum.rs` | 异步 `Stream` 枚举本体，用 `mkenum!`/`dispatch!` 派发到后端。 |
| `src/unnamed_pipe.rs` | unnamed pipe 模块根；声明 `pub mod tokio` 子模块。 |
| `src/unnamed_pipe/tokio.rs` | 匿名管道的 Tokio 变体：`pipe()`、`Recver`、`Sender`。 |
| `src/bound_util.rs` | `RefTokioAsyncRead`/`RefTokioAsyncWrite`，把「`&Self: AsyncRead`」编码进类型系统。 |
| `src/os/unix/uds_local_socket/tokio/stream.rs` | Unix 后端的异步 `Stream`，封装 `tokio::net::UnixStream`（运行时依赖的来源）。 |
| `src/local_socket/stream/options.rs` | `ConnectOptions::connect_tokio`/`connect_tokio_as`。 |
| `src/local_socket/listener/options.rs` | `ListenerOptions::create_tokio`/`create_tokio_as`。 |
| `examples/local_socket/tokio/stream.rs` | 异步客户端示例（实践任务的参考）。 |
| `examples/local_socket/tokio/listener.rs` | 异步服务端示例（展示 `tokio::spawn`）。 |

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：feature 门控、同步/异步的并行 trait 结构、unnamed_pipe 的 Tokio 变体、运行时上下文与 panic。

### 4.1 tokio feature 门控：启用了什么

#### 4.1.1 概念说明

interprocess 默认是**纯同步**库——不开任何 feature，它只暴露阻塞式的 `Read`/`Write` 接口。异步能力全部藏在 `tokio` feature 后面。

关键事实（来自 [Cargo.toml:25-29](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/Cargo.toml#L25-L29)）：

```toml
[features]
default = []
async = ["futures-core"]
tokio = ["dep:tokio", "async"]
doc_cfg = []
```

有两点必须记住：

1. **三个 feature 默认全关**，`default = []`。要用异步，必须显式 `--features tokio`。
2. **`tokio` 隐含 `async`**。依赖链是 `tokio ⇒ async ⇒ futures-core`。也就是说，启用 `tokio` 会顺带把 `async` 和 `futures-core` 一起拉进来。`async` 本身只引入 `futures-core`（提供 `Future` 等核心 trait），并没有引入 Tokio 运行时——真正的运行时依赖是 `tokio` feature 带来的 `dep:tokio`（`dep:` 前缀表示「引用 optional 依赖 `tokio` 而非创建同名 feature」，这是 u1-l3 讲过的细节）。

> 为什么要把 `async` 和 `tokio` 分开？这是一个**面向未来**的设计。理论上 interprocess 未来可能支持别的运行时（比如 `async-std`/`smol`）。把「异步共用的核心 trait」放在 `async` 下、把「Tokio 专属实现」放在 `tokio` 下，就能在不破坏 `async` 用户的前提下增删运行时后端。目前只有 Tokio 一个实现，所以两者在实践中等价。

#### 4.1.2 核心流程

`tokio` feature 在源码里的门控是「**编译期整块裁剪**」式的：

- 一整块异步类型集合（`local_socket::tokio`、`unnamed_pipe::tokio`、`traits::tokio`、`bound_util` 里的异步版 trait）全部用 `#[cfg(feature = "tokio")]` 包裹。
- 不开 feature 时，这些模块**根本不存在于编译产物中**，不是「存在但不可用」。

这与同步类型「无条件存在」形成对照。你可以把 feature gate 想象成一个开关：

```
开启 tokio feature
   │
   ├──> Cargo.toml: tokio = ["dep:tokio", "async"]
   │        拉入 tokio crate + futures-core
   │
   ├──> src/local_socket.rs:#cfg  ──> 解锁 local_socket::tokio 模块
   │                                 解锁 traits::tokio 子模块
   │
   └──> src/unnamed_pipe.rs:#cfg ──> 解锁 unnamed_pipe::tokio 子模块
                                     解锁 bound_util 的异步版 trait
```

文档展示则由 `doc_cfg` feature 单独控制（它只影响 docs.rs 上是否显示「需要 tokio feature」的徽章），与功能本身无关。

#### 4.1.3 源码精读

feature 的定义见 [Cargo.toml:27-28](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/Cargo.toml#L27-L28)：`async` 行声明对 `futures-core` 的依赖，`tokio` 行声明对 optional 依赖 `tokio` 的引用并隐含 `async`。

在模块层面，local socket 的异步模块由两道 `#[cfg]` 把守，见 [src/local_socket.rs:137-139](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket.rs#L137-L139)：

```rust
#[cfg(feature = "tokio")]
#[cfg_attr(feature = "doc_cfg", doc(cfg(feature = "tokio")))]
pub mod tokio {
```

第一行决定模块**是否编译**，第二行决定文档里**是否打 feature 徽章**。匿名管道的异步子模块是同样的两件套，见 [src/unnamed_pipe.rs:22-24](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/unnamed_pipe.rs#L22-L24)：

```rust
#[cfg(feature = "tokio")]
#[cfg_attr(feature = "doc_cfg", doc(cfg(feature = "tokio")))]
pub mod tokio;
```

注意这里是 `pub mod tokio;`（分号结尾，引用同名文件 `unnamed_pipe/tokio.rs`），而 local socket 那边是 `pub mod tokio { ... }`（内联，包含 `listener`/`stream` 子模块）。形式不同，但都受同一个 feature 门控。

#### 4.1.4 代码实践

1. **实践目标**：直观感受 feature 门控的存在——不开 `tokio` 时异步类型根本不存在。
2. **操作步骤**：
   - 在仓库根目录执行 `cargo build`（不带 feature）。编译成功。
   - 再写一个最小文件 `scratch.rs`，内容为 `use interprocess::local_socket::tokio::Stream;`，用 `rustc --edition 2021 --extern interprocess=<path-to-rlib> scratch.rs` 之类的方式编译（或放进一个临时小 crate 的 `src/main.rs` 并以 `interprocess = { path = ".." }` 依赖）。**预期编译失败**，提示找不到 `tokio` 模块。
   - 改用 `cargo build --features tokio`，再编译同一文件。**预期编译成功**。
3. **需要观察的现象**：feature 关闭时，错误信息指向的是「模块不存在」，而非「类型私有」或「方法未实现」。这说明是**整块裁剪**，不是访问限制。
4. **预期结果**：关闭 `tokio` 编译失败、开启后成功，验证「异步能力全由 `tokio` feature 门控」。
5. 如果你不确定本地怎么挂临时 crate，可只做第 1、2 步的「开/关 feature 分别 `cargo build`」，并阅读 [Cargo.toml:25-29](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/Cargo.toml#L25-L29) 确认依赖链。

#### 4.1.5 小练习与答案

**练习 1**：假设你只启用 `async`（不启用 `tokio`），能用 `local_socket::tokio::Stream` 吗？为什么？

> **答案**：不能。`local_socket::tokio` 模块的门控是 `#[cfg(feature = "tokio")]`，而 `tokio` 并未被 `async` 隐含（依赖方向是 `tokio ⇒ async`，单向）。单独开 `async` 只拉进 `futures-core`，不会解锁任何 Tokio 类型。

**练习 2**：`dep:tokio` 里的 `dep:` 前缀去掉会怎样？

> **答案**：会创建一个与 optional 依赖同名的 feature。这在某些 Rust 版本下会导致歧义/告警，且语义上「feature 名」与「依赖名」会混在一起。`dep:` 前缀正是为了显式表达「这里引用的是 optional 依赖本身」，避免与 feature 命名空间冲突（u1-l3 已介绍）。

---

### 4.2 同步与异步的并行结构：traits::tokio 与 AsyncRead/AsyncWrite

#### 4.2.1 概念说明

理解 interprocess 异步层**最省力的方式**，是把它看作同步层的「镜像」。几乎每一个同步概念，都有一个异步对应物：

| 同步（u3-l3） | 异步（本讲） |
|---------------|--------------|
| `traits::Stream` | `traits::tokio::Stream` |
| `traits::Listener` | `traits::tokio::Listener` |
| `local_socket::Stream`（枚举） | `local_socket::tokio::Stream`（枚举） |
| supertrait 含 `Read`/`Write` | supertrait 含 `AsyncRead`/`AsyncWrite` |
| `RefRead`/`RefWrite` | `RefTokioAsyncRead`/`RefTokioAsyncWrite` |
| `Stream::connect(name) -> io::Result<Self>` | `Stream::connect(name) -> impl Future<...>` |
| `Listener::accept() -> io::Result<Stream>` | `Listener::accept() -> impl Future<...>` |
| `Read`/`Write` 方法 `fn read(...)` | `AsyncRead` 的 `fn poll_read(...)` |

「并行结构」这个词的含义就在这里：**接口契约（能 connect、能 accept、能 split/reunite、能 peer_creds）几乎完全一致，只是把「阻塞返回结果」改成「返回一个 Future，要 `.await` 才能拿到结果」**。

唯一实质性的替换，是读写能力的来源：同步用标准库 `std::io::Read`/`Write`，异步用 `tokio::io::AsyncRead`/`AsyncWrite`。这两个 trait 不是「await 版的 Read」那么简单——它们基于 `Pin` + `Poll`，是协作式异步（由运行时在就绪时唤醒），而不是阻塞。

#### 4.2.2 核心流程

异步 `Stream` trait 的 supertrait 链可以和 u3-l3 的同步版逐字对照。同步版是：

```
Read + RefRead + Write + RefWrite + StreamCommon
```

异步版把前四个换成 Tokio 的等价物（见 [src/local_socket/tokio/stream/trait.rs:19-21](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/tokio/stream/trait.rs#L19-L21)）：

```
AsyncRead + RefTokioAsyncRead + AsyncWrite + RefTokioAsyncWrite + StreamCommon
```

注意 `StreamCommon` 是**原样复用**的——它定义的是 `take_error`/`peer_creds` 这类与读写模型无关的方法，所以同步异步共用同一份定义。这是「并行结构」里少有的「完全共享」的部分。

方法的「异步化」套路也很统一。以 `connect` 为例：

- 同步：`fn connect(name) -> io::Result<Self>`（直接返回结果，阻塞到连接完成）。
- 异步：`fn connect(name) -> impl Future<Output = io::Result<Self>> + Send + Sync`（立刻返回一个 Future，`.await` 时才真正等待连接）。

`accept` 同理：异步版的返回类型是 `impl Future<Output = io::Result<Self::Stream>>`。这种「把返回值 `T` 换成 `impl Future<Output = T>`」的翻译，就是异步化的全部表面差异。

#### 4.2.3 源码精读

**异步 `Stream` trait 本体**，见 [src/local_socket/tokio/stream/trait.rs:19-55](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/tokio/stream/trait.rs#L19-L55)。其中最关键的两段：

supertrait 链（第 19-21 行）——把同步的 `Read`/`Write` 替换为 Tokio 的 `AsyncRead`/`AsyncWrite`，并把 `RefRead`/`RefWrite` 替换为它们的异步版：

```rust
pub trait Stream:
    AsyncRead + RefTokioAsyncRead + AsyncWrite + RefTokioAsyncWrite + StreamCommon
{
```

`connect` 的默认实现（第 30-32 行）——返回 `impl Future`，内部委托给 `ConnectOptions::connect_tokio_as::<Self>()` 并 `.await`：

```rust
fn connect(name: Name<'_>) -> impl Future<Output = io::Result<Self>> + Send + Sync {
    async { ConnectOptions::new().name(name).connect_tokio_as::<Self>().await }
}
```

对比 u3-l3 的同步版：同步 `Stream::connect` 是 `ConnectOptions::new().name(name).connect_sync_as::<Self>()`（无 `.await`、无 `async` 块）。这就是「返回 Future」与「直接返回」的全部差别。

`from_options` 同样是返回 Future，见 [src/local_socket/tokio/stream/trait.rs:52-54](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/tokio/stream/trait.rs#L52-L54)——它是各后端真正落地的「无默认实现」方法。

**异步 `Listener` trait**，见 [src/local_socket/tokio/listener/trait.rs:16-35](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/tokio/listener/trait.rs#L16-L35)。注意它的 `from_options` 是**同步**的（`fn from_options(options) -> io::Result<Self>`，创建监听器本身不异步），但 `accept` 是异步的（第 31 行返回 `impl Future`）：

```rust
fn accept(&self) -> impl Future<Output = io::Result<Self::Stream>> + Send + Sync;
```

这点和同步 `Listener` 一致——`accept` 才是「可能要等」的操作。

**`traits::tokio` 子模块**本身只是个再导出聚合点，见 [src/local_socket.rs:95-101](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket.rs#L95-L101)：它把 `tokio::listener::trait::*` 和 `tokio::stream::trait::*` 收拢到 `traits::tokio` 名下，与同步 `traits` 的组织方式完全对称。

**`RefTokioAsyncRead`/`RefTokioAsyncWrite`** 来自 `bound_util`，见 [src/bound_util.rs:50-56](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/bound_util.rs#L50-L56)。它们和同步的 `RefRead`/`RefWrite` 是同一个 `bound_util!` 宏的两个实例，唯一区别是底层 trait 换成了 `TokioAsyncRead`/`TokioAsyncWrite`。这正是「并行结构」在宏层面的体现——u6-l3 会专门讲 GAT 机制，本讲只需知道：它把「`&Self: AsyncRead`」编码进了类型系统，使得异步流也能像同步流那样**用 `&Stream` 同时读写**（异步服务端正是靠这一点用共享引用并发收发，而不必 `split`）。

#### 4.2.4 代码实践

1. **实践目标**：亲手验证「同步与异步 trait 的并行结构」——同一份接口契约，只差读写 trait 的来源。
2. **操作步骤**：
   - 打开 [src/local_socket/tokio/stream/trait.rs:19-55](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/tokio/stream/trait.rs#L19-L55)（异步 `Stream`）。
   - 对照 u3-l3 引用过的同步 `traits::Stream`（在 `src/local_socket/stream/trait.rs`）。
   - 列一张表，逐方法比对：`connect`、`from_options`、`split`、`reunite` 的签名差异。
3. **需要观察的现象**：`split`/`reunite` 在异步版里签名**几乎不变**（`split` 仍按值消费返回两半、`reunite` 仍归还所有权）；唯一系统性变化是 `connect`/`from_options`/`accept` 的返回值从 `T` 变成 `impl Future<Output = T>`。
4. **预期结果**：你会得出结论——「异步化只动了『会等待的操作』的返回类型，其余契约原样照搬」。这是 interprocess 异步层设计上最省心的地方。
5. 若想确认 `split`/`reunite` 在异步枚举层也是手写 `match`（不走 `dispatch!`），可参看 [src/local_socket/tokio/stream/enum.rs:83-110](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/tokio/stream/enum.rs#L83-L110)，与同步版结构一致（u3-l3、u2-l2 已解释为何按值消费的方法必须手写）。

#### 4.2.5 小练习与答案

**练习 1**：异步 `Stream` 的 supertrait 链里有 `StreamCommon`，为什么它不需要一个「异步版」？

> **答案**：`StreamCommon` 只定义 `take_error`/`peer_creds`，这两个操作都是「取一个内核已填好的快照值」，不涉及「等待数据到来」，本身就不会长时间阻塞，因此同步异步共用同一份定义。需要被异步化的只有「读写」和「连接/接受」这类会等待的操作。

**练习 2**：异步 `Listener::from_options` 为什么是同步函数，而 `accept` 是异步的？

> **答案**：创建监听器（绑定名字、建 socket）是一次性的本地操作，能立刻完成，故同步；而 `accept` 要「等一个客户端来连接」，这个等待正是异步要优化的对象，故返回 Future。这与同步 `Listener` 的分工完全一致。

**练习 3**：异步 `Stream::connect` 的默认实现里用了 `async { ... }` 块。这个块返回的类型实现了 `Send + Sync`（见返回 trait bound），这对使用者意味着什么？

> **答案**：意味着 `.connect(...)` 产生的 Future 可以安全地跨线程移动（比如在多线程 Tokio runtime 的不同 worker 间调度），也能在多个任务间共享引用。`Send + Sync` 是 Tokio 多线程运行时下 `tokio::spawn` 对 Future 的硬性要求。

---

### 4.3 unnamed_pipe::tokio：壳/芯模板的第二个实例

#### 4.3.1 概念说明

u2-l3 已经讲过：同步的 `unnamed_pipe` 是「壳/芯」分层的典范——公共 `Sender`/`Recver` 是 `pub(crate)` 字段的 newtype 壳，`impmod!` 把后端实现注入，`multimacro!` 批量缝 trait。

本模块的关键结论是：**`unnamed_pipe::tokio` 就是同一个模板的第二个实例**。差别只有三处：

1. 路径多一层 `::tokio`（公共壳住在 `unnamed_pipe::tokio::{Sender, Recver}`）。
2. 宏从同步版（`forward_sync_read`/`forward_sync_write`/`derive_raw`）换成异步版（`forward_tokio_read`/`forward_tokio_write`/`pinproj_for_unpin`/`derive_asraw`）。
3. 「芯」从同步的 `FdOps`/平台 pipe 实现换成异步的就绪通知实现。

读写能力的来源也随之改变：同步 `Recver` 靠 `Read` trait，异步 `Recver` 靠 `AsyncRead` trait；`Sender` 同理。但「newtype 壳 + `impmod!` 注入 + `multimacro!` 缝 trait」的骨架**一模一样**。

#### 4.3.2 核心流程

`unnamed_pipe::tokio::pipe()` 的派发链，可以和同步版逐行对照：

```
pipe()                                    // 公共入口（壳）
  └──> pipe_impl()                        // impmod! 注入的后端别名
         └── os::unix / os::windows 的异步 pipe 实现（芯）
                返回 (SenderImpl, RecverImpl)
                ── 后端直接构造并返回公共壳类型
```

注意一个 u2-l3 强调过的细节：**后端 `pipe_impl` 直接返回公共 `Sender`/`Recver`**，而不是返回后端私有类型再由公共层包装。所以公共 `pipe()` 的函数体只有一行——转发。

#### 4.3.3 源码精读

**`impmod!` 注入**，见 [src/unnamed_pipe/tokio.rs:8-12](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/unnamed_pipe/tokio.rs#L8-L12)：

```rust
impmod! {unnamed_pipe::tokio,
    Recver as RecverImpl,
    Sender as SenderImpl,
    pipe_impl,
}
```

把它和同步版 [src/unnamed_pipe.rs:26-30](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/unnamed_pipe.rs#L26-L30) 放在一起看，你会发现**逐字相同**，只是路径从 `unnamed_pipe` 变成 `unnamed_pipe::tokio`。`impmod!` 宏按 `cfg(unix)`/`cfg(windows)` 把 `crate::os::unix::unnamed_pipe::tokio::{...}` 或 `crate::os::windows::unnamed_pipe::tokio::{...}` 以统一别名注入。

**`pipe()` 入口**，见 [src/unnamed_pipe/tokio.rs:31-32](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/unnamed_pipe/tokio.rs#L31-L32)——一行转发到 `pipe_impl()`，与同步版 `pipe()` 结构一致。

**`Recver` 壳**，见 [src/unnamed_pipe/tokio.rs:44-53](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/unnamed_pipe/tokio.rs#L44-L53)：

```rust
pub struct Recver(pub(crate) RecverImpl);
multimacro! {
    Recver,
    pinproj_for_unpin(RecverImpl),
    forward_tokio_read,
    forward_as_handle,
    forward_try_handle(io::Error),
    forward_debug,
    derive_asraw,
}
```

对比同步 `Recver`（[src/unnamed_pipe.rs:62-70](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/unnamed_pipe.rs#L62-L70)），差异正是「宏换成异步版」：`forward_sync_read` → `forward_tokio_read`，多了 `pinproj_for_unpin`（异步 trait 需要 `Pin` 投影），`derive_raw` → `derive_asraw`（异步用安全 `AsHandle`/`AsFd` 而非裸 `AsRaw*`）。`Sender`（[src/unnamed_pipe/tokio.rs:64-74](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/unnamed_pipe/tokio.rs#L64-L74)）同理，用 `forward_tokio_write`。

`forward_tokio_read`/`forward_tokio_write` 宏的定义见 [src/macros/forward_iorw.rs:115-190](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros/forward_iorw.rs#L115-L190)：它们为 newtype 生成 `AsyncRead`/`AsyncWrite` 的 `poll_read`/`poll_write`，内部用 `pinproj` 把 `Pin<&mut Self>` 投影到内部字段，再调用内部类型的同名 `poll_*`。这正是「newtype 零手写」的来源。

#### 4.3.4 代码实践

1. **实践目标**：追踪异步 `unnamed_pipe::tokio::pipe()` 从公共壳到平台芯的注入路径，亲手确认「同一模板的第二个实例」。
2. **操作步骤**：
   - 从 [src/unnamed_pipe/tokio.rs:31-32](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/unnamed_pipe/tokio.rs#L31-L32) 的 `pipe()` 出发。
   - 它调用 `pipe_impl()`，这个名字来自第 8-12 行 `impmod!` 的注入。
   - `impmod!` 展开后（见 [src/macros.rs:4-14](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros.rs#L4-L14)），在 Unix 上等价于 `use crate::os::unix::unnamed_pipe::tokio::pipe_impl;`。
   - 画出这条 `pipe() → pipe_impl() → os::{unix,windows}::unnamed_pipe::tokio` 的链路，并标注 `impmod!` 的作用点。
3. **需要观察的现象**：公共 `pipe()` 函数体里**没有任何平台判断、没有任何系统调用**，全部下沉到芯。
4. **预期结果**：你会得到一张与 u2-l3 同步版几乎相同的图，唯一差别是路径多 `::tokio`、芯换成异步实现。
5. 待本地验证：实际展开 `impmod!` 可用 `cargo expand`（若已安装）观察 `unnamed_pipe::tokio` 模块，确认两条 `#[cfg(unix)]`/`#[cfg(windows)]` 的 `use`。

#### 4.3.5 小练习与答案

**练习 1**：为什么异步 `Recver` 用 `derive_asraw` 的「安全版」`derive_asraw`/`forward_as_handle`，而同步 `Recver` 用 `derive_raw`？

> **答案**：`derive_raw` 生成 `AsRawFd`/`AsRawHandle`（返回裸数值，不安全、Rust 1.63 前的老接口）；`forward_as_handle`/`derive_asraw` 生成 `AsFd`/`AsHandle`（返回借用句柄 `BorrowedFd`/`BorrowedHandle`，是 Rust I/O safety 的新接口）。异步层选择更现代、更安全的句柄抽象。注意这是命名/风格差异，不影响「壳/芯」骨架本身。

**练习 2**：`multimacro!` 里出现了 `pinproj_for_unpin(RecverImpl)`，同步版却没有。为什么异步 newtype 需要它？

> **答案**：`AsyncRead`/`AsyncWrite` 的方法签名是 `self: Pin<&mut Self>`，要求 `Self` 被 `Pin` 住。newtype 要把 `Pin<&mut Self>` 转发到内部字段，就需要一个 `pinproj` 方法把 `&mut self.0` 包成 `Pin`。`pinproj_for_unpin!` 正是生成这个投影方法（见 [src/macros.rs:17-26](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros.rs#L17-L26)）。同步 `Read`/`Write` 用的是 `&mut self`，不需要 `Pin`，所以没有这一步。

---

### 4.4 运行时上下文要求与 panic 行为

#### 4.4.1 概念说明

这是本讲最容易踩坑、也最需要讲清楚的一点：**interprocess 的 Tokio 类型，只能在 Tokio 运行时上下文里使用**。脱离运行时调用它们的方法，会**直接 panic**。

库文档把话说得很明确（见 [src/local_socket.rs:134-136](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket.rs#L134-L136)）：

> Types from this module will *not* work with other async runtimes, such as `async-std` or `smol`, since the Tokio types' methods will panic whenever they're called outside of a Tokio runtime context.

为什么会这样？因为 interprocess 的异步后端**直接复用 Tokio 原生类型**。以 Unix 为例，异步 `Stream` 就是 `tokio::net::UnixStream` 的一层 newtype（见 [src/os/unix/uds_local_socket/tokio/stream.rs:30-33](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/uds_local_socket/tokio/stream.rs#L30-L33)）。而 `tokio::net::UnixStream` 的所有 I/O 操作都依赖 Tokio 的 **reactor（事件循环）** 来注册就绪通知——reactor 是由 Tokio 运行时在启动时建立并注册到线程本地的。一旦当前线程没有运行中的 Tokio 运行时（也就没有 reactor），调用这些方法就会 panic，典型错误信息形如：

```
there is no reactor running, must be called from the context of a Tokio 1.x runtime
```

这**不是 interprocess 的限制，而是 Tokio 的限制**——interprocess 只是忠实地继承了它所复用的 Tokio 类型的前提条件。

#### 4.4.2 核心流程

「运行时上下文」的生命周期可以这么理解：

```
#[tokio::main]          // 宏在 main 里建立 Tokio 运行时，注册 reactor 到当前线程
async fn main() {       // 进入运行时上下文 ── 这里 reactor 已就绪
    let conn = Stream::connect(name).await?;   // ✅ 合法：reactor 在
    conn.write_all(...).await?;                // ✅ 合法

    // 若在此处用 std::thread::spawn 开一个裸线程，
    // 并在那个线程里直接 .await conn 的操作：
    //   ❌ panic：新线程没有 reactor
}

// main 返回后，运行时销毁，reactor 也随之消失。
// 之后任何对 Tokio 类型的调用都会 panic。
```

`#[tokio::main]` 宏做的事，等价于：

```rust
fn main() {
    tokio::runtime::Builder::new_multi_thread()  // 或 new_current_thread
        .enable_all()
        .build()
        .unwrap()
        .block_on(async { /* 你的 async main 体 */ });
}
```

`block_on` 期间，当前线程持有 reactor；`async` 体里的 `.await` 才有 reactor 可用。示例代码里那个 `#[cfg(not(feature = "tokio"))]` 的兜底 `main`（[examples/local_socket/tokio/stream.rs:2-5](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/examples/local_socket/tokio/stream.rs#L2-L5)）就是为了「未启用 feature 时也能编译」——它和 u1-l3 讲过的「双 main 门控」是同一套手法。

#### 4.4.3 源码精读

**模块文档对 panic 行为的明确声明**，见 [src/local_socket.rs:134-136](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket.rs#L134-L136)，以及前面那句对异步动机的说明（第 124-128 行）。这是理解「为什么不能换运行时」的权威出处。

**panic 的技术根源：后端复用 Tokio 原生类型**。Unix 后端的异步 `Stream` 定义见 [src/os/unix/uds_local_socket/tokio/stream.rs:30-33](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/uds_local_socket/tokio/stream.rs#L30-L33)：

```rust
/// Wrapper around [`UnixStream`] that implements [`Stream`](traits::Stream).
#[derive(Debug)]
pub struct Stream(pub(super) UnixStream);
```

这里的 `UnixStream` 是 `tokio::net::UnixStream`（见该文件第 24-26 行的 import）。连接建立时它还做了 `UnixStream::from_std(...)`（[第 47 行](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/uds_local_socket/tokio/stream.rs#L45-L48)）——把一个标准库的 `UnixStream` **注册到 Tokio reactor** 上变成异步版本。注册动作本身就要求 reactor 存在。

读写时用的 `ioloop` 闭包（[src/os/unix/uds_local_socket/tokio/stream.rs:95-105](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/uds_local_socket/tokio/stream.rs#L95-L105)）调用了 `self.0.try_read_buf(...)` 和 `self.0.poll_read_ready(cx)`——这两个都是 `tokio::net::UnixStream` 的方法，依赖 reactor。在 reactor 缺席时，它们就是 panic 的直接触发点。

Windows 后端同理，异步类型位于 `os::windows::named_pipe::local_socket::tokio`（由 [src/local_socket/tokio/stream/enum.rs:3-4](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/tokio/stream/enum.rs#L3-L4) 的 `np_impl` 注入），同样建立在 Tokio 的 named pipe 事件机制之上，同样要求运行时上下文。

> 补充：库文档在 [src/local_socket.rs:136](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket.rs#L136) 还留了一句「Open an issue if you'd like to see other runtimes supported as well.」——说明多运行时支持是「未做」而非「不可能」，但这需要为每个运行时单独写一套后端，目前并未实现。

#### 4.4.4 代码实践

1. **实践目标**：亲眼看到「脱离 Tokio 运行时调用异步类型会 panic」。
2. **操作步骤**（写一个故意失败的最小程序，**标注为示例代码**）：
   ```rust
   // 示例代码：故意在运行时之外调用，用于观察 panic
   use interprocess::local_socket::{tokio::prelude::*, GenericNamespaced, ListenerOptions};
   use tokio::io::AsyncWriteExt;

   fn main() {
       // 注意：这里没有 #[tokio::main]，也没有手动建立运行时
       let name = "example.sock".to_ns_name::<GenericNamespaced>().unwrap();
       // 下一行会 panic：there is no reactor running ...
       let conn = tokio::runtime::Runtime::new().unwrap()
           .block_on(async { /* 这里反而合法 */ });
       // 真正会 panic 的写法是把 Stream::connect(name).await 放在没有运行时的地方：
       //   let conn = futures::executor::block_on(Stream::connect(name));
       // 因为 futures::executor 不是 Tokio 运行时，没有 reactor。
   }
   ```
   更干净的可复现方式：在一个用 `futures::executor::block_on`（而非 `#[tokio::main]`）驱动的 `async` 块里调用 `Stream::connect(name).await`，运行后会 panic。
3. **需要观察的现象**：程序崩溃，错误信息提到「no reactor running」或「must be called from the context of a Tokio 1.x runtime」。
4. **预期结果**：确认 interprocess 的异步类型**强依赖 Tokio 运行时**；换成任何非 Tokio 的执行器都会 panic。
5. 待本地验证：上述可复现方式需要额外依赖 `futures` 执行器；若不想引入，可改为「在 `std::thread::spawn` 的裸线程里直接 `.await`」来观察同样的 panic。

#### 4.4.5 小练习与答案

**练习 1**：为什么 interprocess 不把「检测运行时是否存在、不存在就返回 `Err`」做成优雅降级，而是让它 panic？

> **答案**：根本原因在于 interprocess 复用的是 `tokio::net::*` 这类**原生 Tokio 类型**，它们内部访问线程本地的 reactor 句柄，缺 reactor 时由 Tokio 自身决定 panic。interprocess 没有在这层之上包一层「软检查」，因为这会给每一次 I/O 都增加额外开销，且 reactor 缺失本质上是「用法错误」（程序结构问题），用 panic 暴露比静默返回错误更符合 Rust 的惯例。

**练习 2**：`#[tokio::main]` 和手动 `Runtime::new().block_on(...)` 在「是否提供 reactor」上有区别吗？

> **答案**：没有本质区别——两者都会建立一个 Tokio 运行时并注册 reactor，在 `block_on`/`async main` 体内部调用异步类型都是合法的。区别只在运行时的种类（单线程 vs 多线程）和配置，不在「有没有 reactor」。

---

## 5. 综合实践

本任务把本讲四个模块串起来：启用 feature → 写异步客户端 → 与同步客户端逐点对比 → 体会运行时上下文。

**任务**：参考仓库自带的异步客户端示例，写一个最小的异步 local socket 客户端，并写一份「同步 vs 异步」对照笔记。

参考实现就在 [examples/local_socket/tokio/stream.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/examples/local_socket/tokio/stream.rs)，它的核心片段（第 6-43 行）值得逐行读懂：

```rust
#[cfg(feature = "tokio")]
#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    use interprocess::local_socket::{
        tokio::{prelude::*, Stream},
        GenericFilePath, GenericNamespaced,
    };
    use tokio::{io::{AsyncBufReadExt, AsyncWriteExt, BufReader}, try_join};

    let name = if GenericNamespaced::is_supported() {
        "example.sock".to_ns_name::<GenericNamespaced>()?
    } else {
        "/tmp/example.sock".to_fs_name::<GenericFilePath>()?
    };

    let conn = Stream::connect(name).await?;       // 注意 .await

    let mut recver = BufReader::new(&conn);        // tokio::io::BufReader
    let mut sender = &conn;                        // 共享引用即可写

    let send = sender.write_all(b"Hello from client!\n");
    let recv = recver.read_line(&mut buffer);
    try_join!(send, recv)?;                        // 并发收发
}
```

**操作步骤**：

1. 先在一个终端启动异步服务端（它会在 accept 后 `tokio::spawn` 处理每条连接，见 [examples/local_socket/tokio/listener.rs:82-89](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/examples/local_socket/tokio/listener.rs#L82-L89)）：
   ```
   cargo run --example local_socket_tokio_server --features tokio
   ```
2. 在另一个终端启动异步客户端：
   ```
   cargo run --example local_socket_tokio_client --features tokio
   ```
3. 观察两端各打印一行对方的问候语。
4. 写一份对照笔记，至少包含以下五点差异（对照同步示例 `examples/local_socket/sync/stream.rs`）：

   | 维度 | 同步客户端 | 异步客户端 |
   |------|-----------|-----------|
   | 入口 | `fn main()` | `#[tokio::main] async fn main()` |
   | 连接 | `Stream::connect(name)`（直接返回） | `Stream::connect(name).await`（等待） |
   | 收发顺序 | 必须「一端先发、一端先收」，否则单线程死锁（u1-l4、u3-l3） | 用 `try_join!(send, recv)` **并发**收发，无需精心排序 |
   | BufReader | `std::io::BufReader` | `tokio::io::BufReader` + `AsyncBufReadExt` |
   | 写回方式 | `get_mut` 取内部流再写 | 直接用 `&conn` 共享引用写（异步 `&Stream: AsyncWrite`） |

5. **体会运行时上下文**：试着把客户端 `main` 的 `#[tokio::main]` 去掉、改成普通 `fn main()`，编译能过吗？运行会怎样？（预期：编译可能因 `.await` 不在 `async` 上下文而报错；若改成 `futures::executor::block_on` 驱动，则运行时 panic，验证 4.4 的结论。）

**预期结果**：你不仅能跑通异步回显，还能用一句话讲清「异步化到底改了什么」——入口加运行时、会等待的操作加 `.await`、收发从「串行避死锁」变成「`try_join` 并发」。

> 待本地验证：上述 `cargo run --example ... --features tokio` 命令的实际行为以你本机为准；若环境无 Tokio runtime 的多线程特性，可参考 [Cargo.toml:70-78](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/Cargo.toml#L70-L78) 的 `[dev-dependencies]`（示例已带 `rt-multi-thread`、`macros` 等 feature）。

---

## 6. 本讲小结

- `tokio` feature 默认关闭，启用它会隐含 `async`（依赖链 `tokio ⇒ async ⇒ futures-core`），并整块解锁 `local_socket::tokio`、`traits::tokio`、`unnamed_pipe::tokio` 等异步类型集合——不开 feature 时这些模块**根本不编译**。
- 异步层是同步层的「镜像」：接口契约几乎一致，只把 `Read`/`Write` 换成 `AsyncRead`/`AsyncWrite`、把 `RefRead`/`RefWrite` 换成 `RefTokioAsyncRead`/`RefTokioAsyncWrite`，把「会等待的操作」（`connect`/`accept`/`from_options`）的返回值从 `T` 换成 `impl Future<Output = T>`；`StreamCommon` 原样复用。
- `unnamed_pipe::tokio` 是「壳/芯 + `impmod!` + `multimacro!`」模板的第二个实例，与同步版逐字同构，差别仅在路径多 `::tokio`、宏换成异步版（`forward_tokio_read/write`、`pinproj_for_unpin`）、芯换成异步实现。
- interprocess 的 Tokio 类型**只能在 Tokio 运行时上下文里使用**，原因是后端直接复用 `tokio::net::UnixStream` 等原生类型，它们依赖运行时的 reactor；脱离运行时调用会 panic（「there is no reactor running」），这是 Tokio 的限制被 interprocess 忠实继承。
- `#[tokio::main]` / `Runtime::block_on` 的作用就是建立运行时、注册 reactor，使 `async` 体里的 `.await` 合法；异步示例用 `try_join!` 并发收发，从而摆脱了同步版「必须一端先发一端先收」的死锁约束。

---

## 7. 下一步学习建议

本讲只看了异步层的「门控、trait 结构、运行时要求」这一全局图景，还没深入两个方向，建议按顺序继续：

1. **u6-l2 异步 Listener 与 Stream**：精读异步 `Listener`/`Stream` 的实际用法，重点看 `tokio::spawn` 每连接一个任务的并发模型、异步 `split`/`connect`，以及 Windows 下 Tokio 后端如何复用 `tokio::net::windows` 的原生 named pipe 类型。
2. **u6-l3 bound_util 与引用约束**：本讲反复提到的 `RefTokioAsyncRead`/`RefTokioAsyncWrite` 到底是怎么用 GAT 把「`&Self: AsyncRead`」编码进类型系统的，为什么这让异步流能用 `&Stream` 共享读写而不必 `split`——这是理解异步并发收发的类型论钥匙。

此外，若你想了解后端如何把同步系统调用接到 Tokio 的就绪通知上，可先翻 [src/os/unix/uds_local_socket/tokio/stream.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/uds_local_socket/tokio/stream.rs) 里 `ioloop` 的 `try_*` + `poll_*_ready` 轮询模式，那是「阻塞原语异步化」的典型写法。
