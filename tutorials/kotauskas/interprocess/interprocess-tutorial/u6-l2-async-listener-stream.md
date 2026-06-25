# 异步 Listener 与 Stream

## 1. 本讲目标

上一讲（u6-l1）我们站在全局视角，看清了 interprocess 如何把同步 IPC 原语「镜像」成能在 Tokio 运行时里并发使用的异步对象：`tokio` feature 隐含 `async`，整块解锁 `local_socket::tokio` / `traits::tokio` / `unnamed_pipe::tokio`，会把「会等待的操作」改成返回 `impl Future`，并复用 `tokio::net` 原生类型。

本讲则钻进 **local socket 的异步 Listener 与 Stream**，从接口契约一路看到平台后端的实现细节。读完本讲，你应当能够：

1. 说出 `traits::tokio::Listener` 和 `traits::tokio::Stream` 两个 trait 各自定义了哪些方法、哪些是同步的、哪些是异步的，并解释原因。
2. 理解为什么把 `Stream` 放进 `Arc`（或直接用 `&Stream`）就能在两个并发任务里分别读写，而**不必调用 `split`**——这是异步层的推荐用法。
3. 看懂异步 `Stream`/`Listener` 的 enum 派发如何为枚举及它的引用实现 `AsyncRead`/`AsyncWrite`。
4. 剖析 Windows 异步 named pipe 后端：它如何**直接复用 `tokio::net::windows::named_pipe` 的原生类型**，`PipeListener` 如何用 `tokio::sync::Mutex` 保护「预装膛」的实例，`PipeStream` 又如何用 `MaybeArc` 实现按值拆分与重聚。

## 2. 前置知识

本讲默认你已读过 u6-l1，并了解 u3-l3（同步 Stream 的读写/拆分/重聚）、u4-l2/u4-l3（Windows named pipe 后端）的核心结论。为照顾从零开始的读者，先把几个关键术语用通俗语言过一遍：

- **Future 与 `async fn`**：Rust 里 `async fn foo()` 的返回值是一个实现了 `Future` 的匿名类型；调用它本身不执行函数体，只有 `.await`（或交给运行时轮询）才会驱动它前进。本讲里凡是返回 `impl Future<Output = T>` 的方法，都是在说「这是一个异步操作」。
- **Tokio 运行时与 reactor**：Tokio 在后台维护一个「事件循环（reactor）」，负责监听操作系统发来的「某个 fd/handle 现在可读/可写了」通知。interprocess 的异步对象把真正阻塞的系统调用下沉到后端，而这些后端恰恰复用了 Tokio 自己的类型，于是天然接入 reactor。
- **`AsyncRead` / `AsyncWrite`**：Tokio 定义的异步读写 trait，对应标准库同步的 `Read` / `Write`。它们不是用 `read()`/`write()` 方法，而是用 `poll_read` / `poll_write` 这种「被运行时反复轮询」的底层方法。
- **GAT（generic associated types，泛型关联类型）**：Rust 2021 引入的特性，允许在 trait 里写 `type T<'a>` 这样带生命周期的关联类型。`bound_util` 正是用它把「`&Self` 实现了 `AsyncRead`」编码进类型系统——u3-l3 已为同步版本讲过原理，本讲是它的异步翻版。
- **enum dispatch（枚举派发）**：interprocess 不用 `dyn Trait`（trait object）做平台派发，而是为每个抽象定义一个单变体枚举，再用宏把方法调用转发给当前平台的后端。u2-l2 已详述其零开销原理。
- **named pipe 的「预装膛实例」机制**：Windows named pipe 没有真正的「监听器」对象。服务端必须**预先**创建一个处于「待连接」状态的管道实例（叫 instance），客户端的连接直接落在这个实例上；服务端 `accept` 一次，就要再补建一个新实例装回去，否则下一位客户端连不上。u4-l3 已详述，本讲是它的异步版。
- **`Arc` vs `split`**：把一个 `Stream` 放进 `Arc` 让多个所有者共享同一个连接，是「按引用」共享；`split` 则是把流拆成收/发两个独立的所有权对象（按值拆分）。异步层**推荐用 `Arc`**，原因本讲会讲清楚。

## 3. 本讲源码地图

本讲涉及的源码可分四层，由外向内分别是「公共 trait 契约 → 枚举派发层 → 派发入口 → 平台后端」：

| 层次 | 文件 | 作用 |
|------|------|------|
| 公共 trait 契约 | [src/local_socket/tokio/listener/trait.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/tokio/listener/trait.rs) | 定义 `traits::tokio::Listener` trait |
| 公共 trait 契约 | [src/local_socket/tokio/stream/trait.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/tokio/stream/trait.rs) | 定义 `traits::tokio::Stream`、`RecvHalf`、`SendHalf` trait |
| 引用约束工具 | [src/bound_util.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/bound_util.rs) | 生成 `RefTokioAsyncRead`/`RefTokioAsyncWrite`，把「`&T: AsyncRead`」编码进类型系统 |
| 枚举派发层 | [src/local_socket/tokio/stream/enum.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/tokio/stream/enum.rs) | 异步 `Stream`/`RecvHalf`/`SendHalf` 枚举本体与 `AsyncRead`/`AsyncWrite` 派发 |
| 枚举派发层 | [src/local_socket/tokio/listener/enum.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/tokio/listener/enum.rs) | 异步 `Listener` 枚举本体与派发 |
| 派发入口 | [src/os/windows/local_socket/dispatch_tokio.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/local_socket/dispatch_tokio.rs) | Windows 端 `listen`/`connect` 路由 |
| 派发入口 | [src/os/unix/local_socket/dispatch_tokio.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/local_socket/dispatch_tokio.rs) | Unix 端 `listen`/`connect` 路由 |
| Windows 后端（原生 named pipe） | [src/os/windows/named_pipe/tokio/listener.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/tokio/listener.rs) | 异步 `PipeListener`，复用 `tokio::net::windows::named_pipe::NamedPipeServer` |
| Windows 后端 | [src/os/windows/named_pipe/tokio/stream.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/tokio/stream.rs) | 异步 `PipeStream` 结构与 `Drop`→`linger_pool` |
| Windows 后端 | [src/os/windows/named_pipe/tokio/stream/impl.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/tokio/stream/impl.rs) | `split`/`reunite`/进程 ID 查询 |
| Windows 后端 | [src/os/windows/named_pipe/tokio/stream/impl/ctor.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/tokio/stream/impl/ctor.rs) | `connect_by_path` 异步连接 |
| Windows 后端 | [src/os/windows/named_pipe/tokio/stream/impl/recv_bytes.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/tokio/stream/impl/recv_bytes.rs) | `poll_read` 就绪循环 |
| Windows 后端 | [src/os/windows/named_pipe/tokio/stream/impl/send.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/tokio/stream/impl/send.rs) | `poll_write`/`flush` |
| local_socket 包装桥 | [src/os/windows/named_pipe/local_socket/tokio/listener.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/local_socket/tokio/listener.rs) | 把公共 `ListenerOptions` 翻译成 `PipeListenerOptions` |
| local_socket 包装桥 | [src/os/windows/named_pipe/local_socket/tokio/stream.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/local_socket/tokio/stream.rs) | 把 public `Stream` trait 桥接到 `DuplexPipeStream` |
| 示例 | [examples/local_socket/tokio/listener.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/examples/local_socket/tokio/listener.rs) | 异步并发服务端示例 |
| 示例 | [examples/local_socket/tokio/stream.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/examples/local_socket/tokio/stream.rs) | 异步客户端示例 |

> 小贴士：阅读时抓住一条主线——**「壳 / 芯」分层**。`traits::tokio::*` 和顶层枚举是「壳」，只定义接口与派发；真正调用系统调用的是平台后端这个「芯」。这套结构在 u2-l3 已建立，本讲是它在异步场景下的实例。

## 4. 核心概念与源码讲解

### 4.1 traits::tokio::Listener：异步监听器接口契约

#### 4.1.1 概念说明

回忆同步版本（u3-l1）：`traits::Listener` 既定义了 `from_options`（创建监听器），又通过 `accept` 配合 `Incoming` 迭代器驱动服务主循环。异步版要做的事情一模一样，区别只在于——**创建监听器是同步的（只是绑定一个名字、建好底层对象，不需要等客户端），而 `accept` 是异步的（必须等一个客户端连上来才返回）**。

这条「只有会等待的操作才变成 `async`」的判断标准，正是 u6-l1 给出的镜像规则在方法级别的体现。

#### 4.1.2 核心流程

异步 `Listener` 的生命周期：

```
ListenerOptions::new().name(...).create_tokio()
        │  (同步)
        ▼
Listener::from_options(options)   ── 立即返回 io::Result<Self>
        │
        ▼
loop { listener.accept().await }  ── 每轮返回一个 io::Result<Stream>
        │  (异步：等客户端)
        ▼
得到连接后 tokio::spawn(...) 处理该连接
```

注意第三步：`accept` 之间没有「实例补建」的细节会藏在后端里（见 4.4），公共 trait 只暴露「等一个连接，返回一个流」。

#### 4.1.3 源码精读

`traits::tokio::Listener` 的定义非常精炼，只声明了三件事：关联的流类型、创建方法、accept 方法：

```rust
// src/local_socket/tokio/listener/trait.rs:16-35
pub trait Listener: Send + Sync + Sized + Sealed {
    type Stream: Stream;                                             // 关联的异步流类型

    fn from_options(options: ListenerOptions<'_>) -> io::Result<Self>; // 同步：创建监听器

    fn accept(&self) -> impl Future<Output = io::Result<Self::Stream>> + Send + Sync; // 异步：等连接

    fn do_not_reclaim_name_on_drop(&mut self);                       // 关闭「名称回收」
}
```

逐行解读：

- `pub trait Listener: Send + Sync + Sized + Sealed`：要求实现者可跨线程发送与共享（`Send + Sync`）、有具体大小（`Sized`），并被 `Sealed` 封印（即外部代码无法自己实现该 trait，只能在库内实现，保证枚举变体集合封闭）。
- `type Stream: Stream;`：监听器产出的连接类型必须是另一个异步 trait `Stream`（见 4.2）。这两个 trait 通过关联类型绑在一起。
- `from_options` **不是 `async fn`**，返回 `io::Result<Self>` 而非 `impl Future<...>`——因为创建监听器只是绑定名字、建立底层对象，不会等客户端。
- `accept(&self)` 返回 `impl Future<Output = io::Result<Self::Stream>> + Send + Sync`：这是异步等待，返回的 Future 完成时给出一个连接。注意它取 `&self`（不可变借用），于是**多个任务可以同时 `accept` 同一个监听器**——这正是并发服务端的基础。
- 方法上方的文档警告（原文见 [listener/trait.rs:27-30](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/tokio/listener/trait.rs#L27-L30)）特别指出：**在 Windows 上，如果长时间不调用 `accept`，新客户端可能连不上**。这与 4.4 讲的「实例补建」机制直接相关。

#### 4.1.4 代码实践

**实践目标**：对照上面三条方法，确认「哪些同步、哪些异步」的判断。

**操作步骤**：

1. 打开 [src/local_socket/tokio/listener/trait.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/tokio/listener/trait.rs)。
2. 对每个方法，看它的签名里有没有 `impl Future<...>` 或 `async`。
3. 再打开同步版 [src/local_socket/listener/trait.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/listener/trait.rs)，逐方法对照。

**需要观察的现象**：

- `from_options` 和 `do_not_reclaim_name_on_drop` 在同步/异步两版里**签名完全一样**（都是同步）。
- 唯一不同的是 `accept`：同步版返回 `io::Result<Self::Stream>`，异步版返回 `impl Future<Output = io::Result<Self::Stream>>`。

**预期结果**：你会得出 u6-l1 那条规则的实证——「只有会阻塞等待的操作才升级为异步」，创建监听器和配置名称回收都不阻塞，所以保持同步。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `accept` 取 `&self` 而不是 `&mut self`？如果取 `&mut self`，对并发服务端会有什么影响？

> **参考答案**：`&self` 意味着不可变借用，允许同一时刻存在多个借用者（或多个 `accept` 并发进行、或一边 `accept` 一边在别处读监听器状态）。若改成 `&mut self`，则同一时刻只能有一个 `accept` 在进行，必须用 `Mutex` 串行化，违背异步并发设计的初衷。后端内部若确需可变状态（如 Windows 要补建实例），会用 `Mutex`/原子量在**内部**自洽，对外仍是 `&self`——见 4.4。

**练习 2**：`from_options` 为什么不需要返回 Future？

> **参考答案**：创建监听器的过程只是「选好名字 → 建好底层 socket/pipe 实例 → 绑定监听」，这些系统调用要么立即完成、要么立即失败，不需要等待远端客户端响应，因此没有「会被挂起」的步骤，自然不必异步化。

---

### 4.2 traits::tokio::Stream：异步流接口契约与拆分重聚

#### 4.2.1 概念说明

异步 `Stream` 是 local socket 通信的主角。它要同时满足「能被异步读、能被异步写、能查错误、能取对端凭据」，还要支持「拆成收/发两半」和「重聚」。和同步版（u3-l3）相比，差别就是把 `Read`/`Write` 换成 `AsyncRead`/`AsyncWrite`，把 `RefRead`/`RefWrite` 换成异步的 `RefTokioAsyncRead`/`RefTokioAsyncWrite`。

#### 4.2.2 核心流程

一个异步流的典型用法（来自官方示例）：

```
Stream::connect(name).await           // 异步连接，得到 Stream
   │
   ▼
&conn 同时作为读端和写端               // 不 split，用引用共享（见 4.3）
   │
   ├─ BufReader::new(&conn).read_line(...)   // 读
   └─ (&conn).write_all(...)                // 写
   │
   ▼
try_join!(读, 写)                      // 两个操作并发推进
```

关键点：异步层**默认不调用 `split`**。`Stream` 的 `split` 方法仍然存在（按值拆成 `RecvHalf`/`SendHalf`），但 trait 文档明确建议你优先用 `Arc` 或引用来共享，原因见 4.2.3 的源码注释。

#### 4.2.3 源码精读

`traits::tokio::Stream` 的定义：

```rust
// src/local_socket/tokio/stream/trait.rs:19-21
pub trait Stream:
    AsyncRead + RefTokioAsyncRead + AsyncWrite + RefTokioAsyncWrite + StreamCommon
{
```

这条 supertrait 链是理解整条流能力的钥匙：

- `AsyncRead`：流本体可被异步读（`poll_read`）。
- `RefTokioAsyncRead`：「`&Self` 也能被异步读」被编码进类型系统——这是 4.3 的重点。
- `AsyncWrite`：流本体可被异步写。
- `RefTokioAsyncWrite`：「`&Self` 也能被异步写」。
- `StreamCommon`：跨同步/异步复用的通用能力（`take_error`、`peer_creds`），与 u3-l3/u3-l4 一致。

接着看它的方法：

```rust
// src/local_socket/tokio/stream/trait.rs:30-32  —— connect 是带默认实现的异步方法
fn connect(name: Name<'_>) -> impl Future<Output = io::Result<Self>> + Send + Sync {
    async { ConnectOptions::new().name(name).connect_tokio_as::<Self>().await }
}
```

`connect` 有默认实现，等价于「新建选项 → 设名字 → 调 `connect_tokio_as::<Self>()`」。注意它返回 `impl Future`，所以 `Stream::connect(name).await` 才是真正发起连接。

```rust
// src/local_socket/tokio/stream/trait.rs:34-45
/// Splits a stream into a receive half and a send half.
///
/// You probably want to avoid this mechanism for the following reasons:
/// - Placing a stream in an `Rc` or `Arc` produces identical behavior,
///   since `&Stream` implements `Read` and `Write`
/// - Dropping a half does not shut it down like it does with sockets,
///   which may be counterintuitive
fn split(self) -> (Self::RecvHalf, Self::SendHalf);

fn reunite(rh: Self::RecvHalf, sh: Self::SendHalf) -> ReuniteResult<Self>;

fn from_options(options: &ConnectOptions<'_>) -> impl Future<Output = io::Result<Self>> + Send + Sync;
```

两件值得记住的事：

1. **`split` 的文档直接劝退**：作者建议你优先用 `Arc`/`Rc` 共享，而不是 `split`。理由一是「`&Stream` 本身就实现了读写，放 `Arc` 行为完全等价」，理由二是「丢掉一个 half 并不会像真正的 socket 那样触发 shutdown，可能反直觉」。这正是本讲综合实践要用 `Arc` 而非 `split` 的依据。
2. `from_options` **是抽象方法（无默认实现）**，由各后端落地真正的异步连接逻辑（见 4.4）。

最后看两个「半边」trait：

```rust
// src/local_socket/tokio/stream/trait.rs:62-67
pub trait RecvHalf:
    AsyncRead + RefTokioAsyncRead + Send + Sync + Sized + Sealed + 'static
{
    type Stream: Stream;
}
// src/local_socket/tokio/stream/trait.rs:74-79
pub trait SendHalf:
    AsyncWrite + RefTokioAsyncWrite + Send + Sync + Sized + Sealed + 'static
{
    type Stream: Stream;
}
```

`RecvHalf` 只要求异步读能力（`AsyncRead + RefTokioAsyncRead`），`SendHalf` 只要求异步写能力（`AsyncWrite + RefTokioAsyncWrite`）。二者都带 `type Stream: Stream;` 反向指回来源流，这是 `reunite` 做类型配对用的。

#### 4.2.4 代码实践

**实践目标**：确认 `connect` 默认实现的等价关系。

**操作步骤**：

1. 阅读 [src/local_socket/tokio/stream/trait.rs:27-32](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/tokio/stream/trait.rs#L27-L32) 中 `connect` 的默认实现。
2. 再阅读 [src/local_socket/stream/options.rs:158-167](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/stream/options.rs#L158-L167) 中 `connect_tokio_as` 的定义，它只是 `S::from_options(self)`。
3. 用文字串起调用链：`Stream::connect(name).await` ⟶ `ConnectOptions::new().name(name).connect_tokio_as::<Self>().await` ⟶ `Self::from_options(&opts).await`。

**需要观察的现象 / 预期结果**：你会看到 `connect` 没有自己写连接逻辑，而是把活儿全转交给 `ConnectOptions` 和后端的 `from_options`，自身只负责「拼好选项」。这与同步版 `traits::Stream::connect` 完全对称（见 u3-l2）。

> 待本地验证：若你想亲手确认，可在自己机器上启用 `tokio` feature 编写一段调用 `Stream::connect` 的代码，断点或日志观察它最终确实走到了后端的 `from_options`。

#### 4.2.5 小练习与答案

**练习 1**：`RecvHalf` 的 supertrait 里为什么有 `AsyncWrite` 吗？`SendHalf` 里有 `AsyncRead` 吗？

> **参考答案**：都没有。`RecvHalf` 只要求 `AsyncRead + RefTokioAsyncRead`（只能读），`SendHalf` 只要求 `AsyncWrite + RefTokioAsyncWrite`（只能写）。拆分的目的就是把一个全双工流的能力收窄成单向，类型上正好反映这一点。

**练习 2**：`split` 的文档给出两条「避免使用」的理由，请用自己的话复述。

> **参考答案**：① 因为 `&Stream` 已经实现了读写，把流放进 `Arc`/`Rc` 就能得到等价的「多端共享」效果，不必拆分；② 丢掉 half 不会像 socket 的 `shutdown` 那样通知对端，容易和直觉不符。

---

### 4.3 引用读写：RefTokioAsyncRead/RefTokioAsyncWrite 与 `&Stream` 共享

#### 4.3.1 概念说明

4.2 反复强调「`&Stream` 实现了读写」，这看似矛盾——`AsyncRead`/`AsyncWrite` 通常要求 `self: Pin<&mut Self>`，怎么用一个不可变引用 `&Stream` 就能写？答案在 `bound_util` 模块：它用 GAT 把「`&T` 实现了 `AsyncRead`」这件事编码进类型系统，于是只要某个类型 `T` 满足「`&T: AsyncRead`」，它就自动实现了 `RefTokioAsyncRead`。

这个机制是异步层能用 `Arc` 共享、用 `try_join!` 并发收发的**类型论根基**。

#### 4.3.2 核心流程

「按引用共享读写」的本质：

```
对任意类型 T，若 for<'a> &'a T: AsyncRead  成立
                ─────────────────────────
                        ▼  bound_util 自动 impl
            T: RefTokioAsyncRead  （关联类型 RefRead<'a> = &'a T）

于是 Stream 既可 Pin<&mut Stream>   读写（按值消费视角）
        也可 Pin<&mut &Stream>  读写（按引用共享视角）
```

这意味着把同一个 `Stream` 放进 `Arc`，再在两个任务里各自拿一个 `&Stream`，就能一个读、一个写，二者**指向同一底层连接**，无需 `split`、无需拷贝。

#### 4.3.3 源码精读

`bound_util` 的核心是一个宏，它为每个「目标 trait」生成一个对应的「按引用」trait：

```rust
// src/bound_util.rs:10-37（节选宏骨架）
macro_rules! bound_util {
    (#[doc = $doc:literal] $trtname:ident of $otrt:ident with $aty:ident mtd $mtd:ident) => {
        pub trait $trtname {
            type $aty<'a>: $otrt + Is<&'a Self>  // GAT：关联类型带生命周期
            where Self: 'a;
            fn $mtd(&self) -> Self::$aty<'_>;     // 返回「保证 &Self 实现目标 trait」的引用
        }
        impl<T: ?Sized> $trtname for T            // 自动 blanket impl
        where for<'a> &'a T: $otrt,               // 条件：&T 实现目标 trait
        {
            type $aty<'a> = &'a Self where Self: 'a;
            fn $mtd(&self) -> Self::$aty<'_> { self }
        }
    };
    ...
}
```

宏的展开规则可以总结成一句话：**「只要 `&T` 实现了 `$otrt`（比如 `AsyncRead`），`T` 就自动实现 `$trtname`（比如 `RefTokioAsyncRead`）」**。其中 `type $aty<'a>` 是一个 GAT（泛型关联类型），用来表达「存在一个引用类型，它实现了目标 trait 且就是 `&'a Self`」。

异步版的两个具体实例：

```rust
// src/bound_util.rs:50-56
#[cfg(feature = "tokio")]
bound_util! {
    /// [Tokio's `AsyncRead`](TokioAsyncRead) by reference.
    RefTokioAsyncRead  of TokioAsyncRead  with Read  mtd as_tokio_async_read
    /// [Tokio's `AsyncWrite`](TokioAsyncWrite) by reference.
    RefTokioAsyncWrite of TokioAsyncWrite with Write mtd as_tokio_async_write
}
```

也就是说，只要某个类型 `T` 满足 `&T: AsyncRead`，它就自动是 `RefTokioAsyncRead`。而 `traits::tokio::Stream` 的 supertrait 链里同时要求 `AsyncRead + RefTokioAsyncRead`——这两个约束合起来，正是要保证「流本体可读、流的引用也可读」。

这套约束在客户端示例里被「兑现」：

```rust
// examples/local_socket/tokio/stream.rs:33-43（客户端，用引用同时读写）
let conn = Stream::connect(name).await?;

let mut recver = BufReader::new(&conn);   // 用 &conn 包成读端
let mut sender = &conn;                    // 用 &conn 当写端

let send = sender.write_all(b"Hello from client!\n");
let recv = recver.read_line(&mut buffer);

try_join!(send, recv)?;                    // 收发并发推进
```

注意 `recver` 和 `sender` 都是 `&conn` 的再借用，它们**共享同一个连接**。`try_join!` 让两个 future 并发轮询：写端先把数据塞进内核缓冲，读端等对端回包。这正是 u1-l3 提到的「异步版用 `try_join!` 并发收发，不必精心安排收发次序」的落点——而这一切能通过类型检查，靠的就是 4.3 这套「按引用读写」约束。

> 补充：同步版（u3-l3）用的是同名机制 `RefRead`/`RefWrite`，原理完全相同，只是把 `AsyncRead` 换成 `Read`。本讲只讲异步翻版。

#### 4.3.4 代码实践

**实践目标**：验证「`Arc<Stream>` 能在两个并发任务里分别读写」。

**操作步骤**：

1. 阅读 [src/bound_util.rs:50-56](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/bound_util.rs#L50-L56)，确认 `RefTokioAsyncRead`/`RefTokioAsyncWrite` 的生成条件。
2. 阅读 [examples/local_socket/tokio/stream.rs:33-43](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/examples/local_socket/tokio/stream.rs#L33-L43)，确认示例确实用 `&conn` 同时读写。
3. 在脑中（或纸上）推演：如果把示例改成「把 `conn` 装进 `Arc`，再 `tokio::spawn` 两个任务分别持有 `Arc` 的克隆、一个只读一个只写」，类型上为何合法。

**需要观察的现象**：示例里 `recver` 和 `sender` 都来自 `&conn`，不存在「移动所有权」——也就是说**同一连接被多个只读借用共享**。

**预期结果**：你会理解为什么 `split`（按值拆分、移动所有权）在异步层是「可选而非首选」：引用共享已经覆盖了绝大多数「同时收发」的场景。

#### 4.3.5 小练习与答案

**练习 1**：`bound_util` 宏的 blanket impl 条件是 `where for<'a> &'a T: $otrt`。如果某个类型只有 `T: AsyncRead` 而 `&T: AsyncRead` 不成立，它还能实现 `RefTokioAsyncRead` 吗？

> **参考答案**：不能。`RefTokioAsyncRead` 的自动实现**完全依赖**「`&T: AsyncRead`」这一前提。若只有 `T: AsyncRead`（按值可读但引用不可读），则不满足 blanket impl 的 where 子句，也就得不到 `RefTokioAsyncRead`，进而无法满足 `Stream` 的 supertrait 链。这就是为什么后端在实现时，必须**同时**为 `Stream` 本体和 `&Stream` 实现 `AsyncRead`/`AsyncWrite`——见 4.4 的 `recv_bytes.rs`/`send.rs`。

**练习 2**：为什么把流放进 `Arc` 比 `split` 更省心？

> **参考答案**：① `Arc` 只是增加引用计数，流的所有权结构不变，不需要额外的 `reunite` 配对逻辑；② `&Stream` 直接满足读写约束，不需要为「收半边/发半边」单独定义类型；③ 丢弃一个 `Arc` 克隆不会触发任何 shutdown 副作用，行为更可预期。代价是 `Arc` 本身有一次堆分配与原子引用计数开销，但对长连接而言通常可忽略。

---

### 4.4 Windows tokio named pipe 后端：复用 tokio 原生类型

#### 4.4.1 概念说明

前三个模块讲的都是「壳」：接口契约、引用约束。本模块钻进 Windows 平台的「芯」，回答三个问题：

1. **复用**：Windows 异步后端并不自己实现事件循环，而是直接用 Tokio 官方的 `tokio::net::windows::named_pipe::{NamedPipeServer, NamedPipeClient}`。interprocess 在其上叠加「预装膛实例」「消息模式泛型化」「limbo 延迟刷新」等能力。
2. **监听器**：`PipeListener` 不对应任何 Tokio 对象，是 interprocess 的发明；它持有一个 `Mutex<TokioNPServer>` 当作「膛里的待命实例」，每次 `accept` 用掉它再补一个。
3. **流**：`PipeStream` 用 `MaybeArc`（而非 `Arc`）持有底层 raw 流，做到「未拆分时零开销、拆分后才升级成 Arc」——这是 u8-l2 将详述的优化，本模块先看清它的接口用法。

#### 4.4.2 核心流程

**监听器 accept 的异步流程**（复用 u4-l3 的实例机制，加异步与互斥）：

```
listener.accept().await
   │
   ▼ 锁住 stored_instance (tokio::sync::Mutex)
stored_instance.connect().await   ← 在「膛里」那个实例上等客户端连上来
   │
   ▼ create_instance() 新建一个待命实例
replace(膛里, 新实例)              ← 把新实例装回膛里
   │
   ▼ 把刚才被连上的旧实例交给上层
返回 PipeStream（包装该实例）
```

关键：互斥锁 `Mutex` 只在「取出实例 + 补建实例」这一小段持有，`connect().await` 等待期间也持锁——所以同一时刻只有一个 `accept` 在推进。多个并发 `accept` 会排队，这与 4.1 练习里「`accept` 取 `&self` 仍可在内部用 `Mutex` 串行化」的答案呼应。

**流的连接与读写**：

```
connect_by_path(path).await
   │ 调 CreateFile 打开管道（失败则按 wait_mode 重试/spawn_blocking）
   ▼
TokioNPClient::from_raw_handle(...)   ← 把裸句柄交给 tokio 原生客户端类型
   │
   ▼
RawPipeStream { inner: Client(client), needs_flush }
   │
   ▼ poll_read / poll_write（就绪循环）
try_read_buf / try_write  ──遇 WouldBlock──▶ poll_read_ready / poll_write_ready（注册到 reactor）
```

读写走的是经典的 **mio/Tokio 就绪模型**：先试着 `try_read`/`try_write`，遇到 `WouldBlock` 就 `poll_*_ready` 把自己挂起、等 reactor 通知「可读/可写了」再继续。

#### 4.4.3 源码精读

**(a) 复用 tokio 原生类型**

Windows 异步后端在文件顶部就直接 `use` 了 Tokio 官方的 named pipe 类型：

```rust
// src/os/windows/named_pipe/tokio/stream/impl.rs:33-36
use tokio::net::windows::named_pipe::{
    NamedPipeClient as TokioNPClient, NamedPipeServer as TokioNPServer,
};
```

`TokioNPServer`/`TokioNPClient` 就是 `tokio::net::windows::named_pipe` 模块提供的、已经接入 reactor 的原生类型。interprocess 把它们包进自己的 `InnerTokio` 枚举：

```rust
// src/os/windows/named_pipe/tokio/stream.rs:66-69
enum InnerTokio {
    Server(TokioNPServer),
    Client(TokioNPClient),
}
```

这行代码是「复用」的铁证：流的内核就是 Tokio 的原生 server/client，interprocess 没有重造 reactor。

**(b) 异步监听器与「预装膛」实例**

```rust
// src/os/windows/named_pipe/tokio/listener.rs:38-42
pub struct PipeListener<Rm: PipeModeTag, Sm: PipeModeTag> {
    config: PipeListenerOptions<'static>,     // 创建新实例所需的配置
    stored_instance: Mutex<TokioNPServer>,    // 「膛里」那个待命实例
    _phantom: PhantomData<(Rm, Sm)>,
}
```

注意 `stored_instance` 用的是 `tokio::sync::Mutex`（异步互斥锁），不是 `std::sync::Mutex`——因为 `accept` 期间要在持锁状态下 `.await`，标准库的 `Mutex` 守卫不是 `Send` 的话会卡住运行时。

`accept` 的实现完美对应 4.4.2 的流程图：

```rust
// src/os/windows/named_pipe/tokio/listener.rs:48-58
pub async fn accept(&self) -> io::Result<PipeStream<Rm, Sm>> {
    let instance_to_hand_out = {
        let mut stored_instance = self.stored_instance.lock().await; // 锁住膛
        stored_instance.connect().await?;                            // 等客户端连上膛里实例
        let new_instance = self.create_instance()?;                  // 补建新实例
        replace(&mut *stored_instance, new_instance)                 // 新实例装回膛，旧实例取出
    };
    let raw = RawPipeStream::new_server(instance_to_hand_out);       // 旧实例包装成流
    Ok(PipeStream::new(raw))
}
```

逐行：锁膛 → 在膛里实例上 `connect().await` 等客户端 → 建新实例 → `replace` 把新实例装回膛、取出被连上的旧实例 → 把旧实例包成 `PipeStream` 返回。**这正是 u4-l3 同步版的实例机制在异步场景下的复刻**，只是把阻塞 `ConnectNamedPipe` 换成了 reactor 驱动的 `connect().await`，并用 `Mutex` 保护共享的膛。

监听器自身的创建（`create_tokio`）则会**强制非阻塞**（注释明说 Tokio 理应已设置，这里「保险起见」再设一次）：

```rust
// src/os/windows/named_pipe/tokio/listener.rs:109-120
pub fn create_tokio<Rm: PipeModeTag, Sm: PipeModeTag>(
    &self,
) -> io::Result<PipeListener<Rm, Sm>> {
    let mut config = self.to_owned()?;
    config.nonblocking = false; // 见下注
    let instance = config
        .create_instance(true, false, PipeListener::<Rm, Sm>::STREAM_ROLE, Rm::MODE)
        .and_then(npserver_from_handle)?;
    Ok(PipeListener::from_tokio_and_options(instance, config))
}
```

> 小贴士：这里的 `config.nonblocking = false` 看似与「强制非阻塞」矛盾，实际是 interprocess 内部的语义约定——在该后端里 `nonblocking=false` 恰好对应「交给 Tokio 的 OVERLAPPED/就绪模式」。细节涉及 `create_instance` 的标志位映射，属于实现内部，本讲不展开；读者只需记住结论：**Tokio 监听器始终以非阻塞/就绪方式工作**。

**(c) 流的连接**

```rust
// src/os/windows/named_pipe/tokio/stream/impl/ctor.rs:73-83
pub async fn connect_by_path<'s>(path: impl ToWtf16<'s>) -> io::Result<Self> {
    RawPipeStream::connect(
        path.to_wtf_16().map_err(to_io_error)?,
        Rm::MODE,
        Sm::MODE,
        ConnectWaitMode::Unbounded,    // local socket 包装层恒用无界等待
    )
    .await
    .map(Self::new)
}
```

`RawPipeStream::connect`（[ctor.rs:26-67](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/tokio/stream/impl/ctor.rs#L26-L67)）会先尝试 `connect_without_waiting`（非阻塞地试着连）；若返回「需要等待」，则按 `wait_mode` 决定：`Deferred` 报 `Unsupported`、`Timeout` 用 `spawn_blocking` 跑自旋重试、`Unbounded` 无限重试。最终把得到的句柄交给 Tokio 原生客户端类型：

```rust
// src/os/windows/named_pipe/tokio/stream/impl/ctor.rs:57
let client = unsafe { TokioNPClient::from_raw_handle(client.into_raw_handle())? };
```

这又是一处「复用」：连接成功后，裸句柄直接被 Tokio 的 `NamedPipeClient` 接管，接入 reactor。

**(d) 异步读写的就绪循环**

读：先 `try_read_buf`，遇 `WouldBlock` 就 `poll_read_ready` 挂起等通知，再循环：

```rust
// src/os/windows/named_pipe/tokio/stream/impl/recv_bytes.rs:8-21
fn poll_read_readbuf(&self, cx: &mut Context<'_>, buf: &mut ReadBuf<'_>) -> Poll<io::Result<()>> {
    loop {
        match downgrade_eof(same_clsrv!(x in self.inner => x.try_read_buf(buf))) {
            Ok(..) => return Poll::Ready(Ok(())),
            Err(e) if e.kind() == io::ErrorKind::WouldBlock => {}
            Err(e) => return Poll::Ready(Err(e)),
        }
        ready!(same_clsrv!(x in self.inner => x.poll_read_ready(cx)))?;
    }
}
```

`same_clsrv!` 宏（[impl.rs:3-10](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/tokio/stream/impl.rs#L3-L10)）是对 `InnerTokio` 的 `Server`/`Client` 两个变体做同样操作的简写——无论这条流是服务端还是客户端创建的，读写逻辑一致。

写：同样的就绪循环，写成功后**标记 needs_flush**（dirty）：

```rust
// src/os/windows/named_pipe/tokio/stream/impl/send.rs:8-19
fn poll_write(&self, cx: &mut Context<'_>, buf: &[u8]) -> Poll<io::Result<usize>> {
    loop {
        ready!(same_clsrv!(x in self.inner => x.poll_write_ready(cx)))?;
        match same_clsrv!(x in self.inner => x.try_write(buf)) {
            Err(e) if e.kind() == io::ErrorKind::WouldBlock => continue,
            els => {
                self.needs_flush.mark_dirty();   // 写过就要刷新
                return Poll::Ready(els);
            }
        }
    }
}
```

注意 `&PipeStream` 和 `PipeStream` **都实现了 `AsyncRead`/`AsyncWrite`**（见 [recv_bytes.rs:24-43](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/tokio/stream/impl/recv_bytes.rs#L24-L43) 与 [send.rs:75-110](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/tokio/stream/impl/send.rs#L75-L110)）——这正是 4.3 练习 1 要求的「`&T: AsyncRead`」前提，让 `RefTokioAsyncRead` 的 blanket impl 得以成立。

**(e) 按值拆分与重聚：MaybeArc**

最后看 `split`/`reunite`。Windows 后端用的是 `MaybeArc`（一个「内联 or 共享」的二选一容器），而不是简单的 `Arc`：

```rust
// src/os/windows/named_pipe/tokio/stream/impl.rs:43-49
pub fn split(mut self) -> (RecvPipeStream<Rm>, SendPipeStream<Sm>) {
    let (raw_ac, raw_a) = (self.raw.refclone(), self.raw);   // 引用克隆：未拆分→共享
    (
        RecvPipeStream { raw: raw_a.into(), flusher: (), _phantom: PhantomData },
        SendPipeStream { raw: raw_ac, flusher: self.flusher, _phantom: PhantomData },
    )
}
```

`refclone` 把底层 raw 流从「内联单所有者」升级成「共享（Arc）双所有者」，于是两个半边各持一份指向同一连接的共享指针。重聚时则反向降级：

```rust
// src/os/windows/named_pipe/tokio/stream/impl.rs:53-61
pub fn reunite(rh: RecvPipeStream<Rm>, sh: SendPipeStream<Sm>) -> ReuniteResult<Rm, Sm> {
    if !MaybeArc::ptr_eq(&rh.raw, &sh.raw) {     // 先判定两半是否同源
        return Err(ReuniteError { rh, sh });      // 不同源：归还两半所有权
    }
    let PipeStream { mut raw, flusher, .. } = sh;
    drop(rh);
    raw.try_make_owned();                         // 尝试降级回单所有者（若此时只剩这一份）
    Ok(PipeStream { raw, flusher, _phantom: PhantomData })
}
```

`ptr_eq` 用「两个共享指针是否指向同一块内存」判定同源（与 u3-l3 同步版 Unix 用 `Arc::ptr_eq` 的思路一致）；`try_make_owned` 在引用计数为 1 时把共享指针**降级**回内联单所有者，恢复「零开销」状态。这种「未拆分零开销、拆分才升级、重聚再降级」的设计是 `MaybeArc` 的精髓，u8-l2 会专门剖析其内部结构。

**(f) Drop 与 limbo（预览）**

流被 drop 时，若 `needs_flush` 为真，不会立即关闭句柄，而是送进 `linger_pool` 后台延迟刷新（u8-l1 主题）：

```rust
// src/os/windows/named_pipe/tokio/stream.rs:70-80
impl Drop for RawPipeStream {
    fn drop(&mut self) {
        let i = unsafe { ManuallyDrop::take(&mut self.inner) };
        if self.needs_flush.get_mut() {
            match i {
                InnerTokio::Server(p) => linger_pool::linger_boxed(p),
                InnerTokio::Client(p) => linger_pool::linger_boxed(p),
            }
        }
    }
}
```

这是 Windows named pipe 的固有特性：直接 drop 可能让对端读到不完整数据。interprocess 用后台线程先 `FlushFileBuffers` 再关闭。本讲只点到为止。

#### 4.4.4 代码实践

**实践目标**：画出 Windows 异步后端「连接 → 读写 → drop」的完整数据通路，并指出每一处「复用 Tokio 原生类型」的点。

**操作步骤**：

1. 读 [ctor.rs:26-67](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/tokio/stream/impl/ctor.rs#L26-L67)，标出 `TokioNPClient::from_raw_handle` 这一步。
2. 读 [recv_bytes.rs:8-43](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/tokio/stream/impl/recv_bytes.rs#L8-L43)，标出 `try_read_buf` / `poll_read_ready` 这两个调用——它们都是 `TokioNPClient`/`TokioNPServer` 的方法。
3. 读 [listener.rs:48-58](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/tokio/listener.rs#L48-L58)，标出 `stored_instance.connect().await`——`connect` 也是 `TokioNPServer` 的方法。
4. 读 [stream.rs:70-80](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/tokio/stream.rs#L70-L80)，标出 drop 时把 `TokioNPServer`/`TokioNPClient` 送进 `linger_pool`。

**需要观察的现象**：你会发现 `TokioNPServer`/`TokioNPClient` 这两个 Tokio 原生类型贯穿「连接 / accept / 读 / 写 / drop」全流程，interprocess 没有自己实现任何 reactor 逻辑。

**预期结果**：得到一张清晰的「数据通路图」，图上每一步都标注了「复用 tokio::net::windows」。

> 待本地验证：在 Windows 机器上启用 `tokio` feature，运行 [examples/local_socket/tokio/listener.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/examples/local_socket/tokio/listener.rs) 与 [stream.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/examples/local_socket/tokio/stream.rs)，确认服务端能并发处理多个客户端（多开几个客户端进程同时连）。

#### 4.4.5 小练习与答案

**练习 1**：`PipeListener::accept` 里用的是 `tokio::sync::Mutex` 而非 `std::sync::Mutex`，为什么？

> **参考答案**：`accept` 需要在持锁状态下 `.await`（等 `stored_instance.connect().await`）。`std::sync::Mutex` 的守卫在跨 `.await` 点时若不是 `Send`，会无法在多线程运行时里安全持有，且会阻塞当前工作线程；而 `tokio::sync::Mutex` 是为异步场景设计的，其守卫可跨 `.await`，且 `lock().await` 在锁被占用时会让出当前任务而不是阻塞线程。注意：Tokio 官方通常建议优先用 `std::sync::Mutex`（短临界区）以减少唤醒开销，这里因为临界区包含一个可能长时间的 `connect().await`，用 `tokio::sync::Mutex` 更合适。

**练习 2**：`PipeStream` 的 `split` 用 `refclone`、`reunite` 用 `try_make_owned`。请解释「升级」与「降级」分别指什么。

> **参考答案**：「升级」指 `refclone` 把底层 raw 流从「内联单所有者」变成「`Arc` 共享多所有者」，从而两个半边各持一份共享指针；「降级」指 `reunite` 在确认两半同源（`ptr_eq`）后，若此时引用计数为 1，用 `try_make_owned` 把共享指针退回内联单所有者，恢复未拆分时的零开销布局。这套升降级机制由 `MaybeArc` 提供，让流在「从不 split」时完全不产生堆分配。

**练习 3**：为什么 `&PipeStream` 也实现了 `AsyncRead`/`AsyncWrite`？这和 4.3 有什么关系？

> **参考答案**：因为后端为 `&PipeStream`（和 `PipeStream`）分别实现了 `AsyncRead`/`AsyncWrite`（见 recv_bytes.rs / send.rs），于是 `for<'a> &'a PipeStream: AsyncRead` 成立，满足 `bound_util` 的 blanket impl 条件，`PipeStream` 自动得到 `RefTokioAsyncRead`/`RefTokioAsyncWrite`，进而能放进 `Arc` 被多任务按引用共享读写。这是 4.3 的类型约束在后端的「兑现」。

---

## 5. 综合实践

**任务**：把第一讲（u1-l4）的同步回显服务改造成**异步并发版**，要求做到以下三点，把本讲知识串起来：

1. **用 `ListenerOptions` + `create_tokio()` 建监听器**（4.1），主循环 `listener.accept().await`，每来一条连接就 `tokio::spawn` 一个独立任务处理（与官方示例 [examples/local_socket/tokio/listener.rs:70-90](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/examples/local_socket/tokio/listener.rs#L70-L90) 同构）。
2. **故意用 `Arc<Stream>` 共享连接，而不是 `split`**（4.2、4.3）：在每条连接的任务里，把 `conn` 装进 `Arc`，再 `tokio::spawn` **两个**子任务——一个循环 `read`、一个循环 `write`（例如把读到的行原样写回，即回显）。两个子任务各自持有 `Arc` 的克隆，通过 `&*arc` 借用读写。
3. **并发收发**：用 `tokio::select!` 或 `tokio::join!` 让读写两个子任务同时推进（对比 u1-l4 同步版必须「一端先发一端先收」的限制）。

参考骨架（**示例代码**，非仓库原有，需你补全并启用 `tokio` feature 编译）：

```rust
// 示例代码：异步并发回显服务（用 Arc 共享，不用 split）
use interprocess::local_socket::{
    tokio::{prelude::*, Stream},
    ListenerOptions, ToNsName as _, GenericNamespaced,
};
use std::{io, sync::Arc};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};

#[tokio::main]
async fn main() -> io::Result<()> {
    let name = "example.sock".to_ns_name::<GenericNamespaced>()?;
    let listener = ListenerOptions::new().name(name).create_tokio()?;

    loop {
        let conn = listener.accept().await?;          // 4.1：异步 accept
        tokio::spawn(async move {                     // 每连接一个任务
            let conn = Arc::new(conn);                // 4.2/4.3：Arc 共享，不 split
            let reader = Arc::clone(&conn);
            let writer = Arc::clone(&conn);

            // 读任务：按行读，写回对端（回显）
            let read_task = tokio::spawn(async move {
                let mut buf = String::new();
                let mut br = BufReader::new(&*reader);
                loop {
                    buf.clear();
                    if br.read_line(&mut buf).await? == 0 { break; } // EOF
                    (&*writer).write_all(buf.as_bytes()).await?;
                }
                io::Result::Ok(())
            });
            let _ = read_task.await;
        });
    }
}
```

**验证要点**：

- 启动服务端后，**同时**开多个客户端（可用官方 [examples/local_socket/tokio/stream.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/examples/local_socket/tokio/stream.rs)），确认服务端能并发处理而不互相阻塞——这验证了 4.1「`accept` 取 `&self`、可并发」与 4.4「Tokio reactor 驱动」。
- 确认整个实现**没有出现 `split`/`reunite`**，全靠 `Arc` + 引用共享——这验证了 4.2 的「避免 split」建议与 4.3 的「`&Stream` 可读写」约束。
- 在 Windows 上运行时，留意 4.4 提到的 limbo 行为：即使客户端先 drop，服务端仍应能收到此前写入的完整数据（后台 `linger_pool` 刷新）。

> 待本地验证：上述骨架在不同平台（Linux/Windows）上的具体行为，尤其是 `read_line` 在 local socket（字节流、无消息边界）上的分帧表现。

## 6. 本讲小结

- 异步 `Listener`/`Stream` 是同步版的「镜像」：**只有会阻塞等待的操作（`accept`、`connect`、`from_options`）才升级为返回 `impl Future`**，创建监听器、配置名称回收保持同步。
- `traits::tokio::Stream` 的 supertrait 链 `AsyncRead + RefTokioAsyncRead + AsyncWrite + RefTokioAsyncWrite + StreamCommon` 同时保证「按值」与「按引用」都可异步读写，这是 `Arc` 共享、`try_join!` 并发收发的类型论根基。
- `bound_util` 用 GAT 把「`&T: AsyncRead`」编码成 `RefTokioAsyncRead` trait：只要后端为 `&PipeStream` 也实现了 `AsyncRead`/`AsyncWrite`，blanket impl 自动生效。
- 异步层的**首选用法是用 `Arc`（或 `&Stream`）共享，而非 `split`**——`split` 的文档明确劝退，理由是引用共享已等价、且 half 丢弃不会触发 shutdown。
- Windows 异步 named pipe 后端**直接复用 `tokio::net::windows::named_pipe::{NamedPipeServer, NamedPipeClient}`**，不重造 reactor；`PipeListener` 用 `tokio::sync::Mutex` 保护「预装膛实例」，`accept` 即「用掉膛里实例 + 补建新实例」。
- Windows 流的 `split`/`reunite` 走 `MaybeArc` 的「升级（`refclone`）/降级（`try_make_owned`）」机制，未拆分时零开销；drop 时若有未刷新数据则送入 `linger_pool` 后台处理。
- 读写遵循 Tokio 就绪模型：`try_read`/`try_write` 遇 `WouldBlock` 即 `poll_*_ready` 注册到 reactor 挂起等待。

## 7. 下一步学习建议

- **u6-l3（bound_util 与引用约束）**：本讲只用到了 `RefTokioAsyncRead`/`RefTokioAsyncWrite` 的「表面」，下一讲会拆开 `bound_util!` 宏、讲清 GAT 如何把 `&T: Read` 编码进类型系统，并带你手写一个最小复刻。
- **u8-l1（linger_pool）**：本讲 4.4 提到的「drop 后台刷新」只是点到为止，下一阶段会剖析高低水位线程池与低比特标记指针。
- **u8-l2（maybe_arc）**：本讲 4.4 的 `MaybeArc`「升级/降级」机制将在专家层完整展开，包括 `OptArc`/`OptArcIRC` trait 与 `Inline`/`Shared` 双态布局。
- **延伸阅读**：对照 [src/os/unix/uds_local_socket/tokio/listener.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/uds_local_socket/tokio/listener.rs) 与 [stream.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/uds_local_socket/tokio/stream.rs)，比较 Unix 后端如何复用 `tokio::net::UnixListener`/`UnixStream`，以及它的 `split` 如何直接复用 Tokio 的 `into_split`（内部 `Arc`）——体会两个平台后端「同样的壳/芯结构、不同的芯实现」。
