# Stream 的读写、拆分与重聚

## 1. 本讲目标

本讲聚焦 local socket 客户端拿到 `Stream` 之后「能用它做什么」。学完后你应当能够：

- 调用 `set_nonblocking` / `set_recv_timeout` / `set_send_timeout` 控制流的读写阻塞行为，并能解释为什么这些方法在 **Windows 的 local socket 上会直接失败**。
- 看懂 `StreamCommon` 提供的 `take_error` / `peer_creds` 两个「瞬时查询」型方法。
- 把一个 `Stream` 用 `split` 拆成 `RecvHalf` / `SendHalf` 两半，理解它的代价与官方推荐的 `Arc` 替代方案。
- 正确使用 `reunite` 把两半合回 `Stream`，并在两半「来自不同流」时，从 `ReuniteError` 里**完整取回两半的所有权**。

本讲承接 u3-l1（服务端 `Listener`）与 u3-l2（客户端 `ConnectOptions`），是同步 local socket 使用层的收尾。

## 2. 前置知识

阅读本讲前，你需要已经了解：

- **local socket 是抽象而非 OS 原语**（u2-l1）：底层在 Unix 是 Unix domain socket、在 Windows 是 named pipe。
- **trait 定义接口 + enum 做派发**（u2-l2）：公共 `Stream` 是一个枚举，`dispatch!` 宏把方法调用转发给当前平台的唯一后端；但因为 `dispatch!` 不支持「按值消费」，所以涉及所有权转移的方法要**手写 `match`**——这一点本讲会反复用到。
- **位标志构建器与 `from_options` 派发链**（u3-l1、u3-l2）：`Stream` 的配置方法最终都会下沉到平台后端的 `from_options`。

两个术语提醒：

- **全双工（full-duplex）**：UDS 和 named pipe 都是「同一时刻既能读又能写」的。这正是 `split` 后用两个线程分别收发的动机——可以打破 u1-l4 里那种「一端先发一端先收」的串行约束。
- **所有权归还（ownership return）**：当一个操作以「按值」拿走了你的对象却又失败时，把对象原样还给你，而不是直接丢弃。这是 `ReuniteError` 的核心设计。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/local_socket/stream/trait.rs` | 定义 `Stream` / `StreamCommon` / `RecvHalf` / `SendHalf` 四个 trait 与 `ReuniteResult` 别名。这是本讲的「接口契约」。 |
| `src/local_socket/stream/enum.rs` | 公共 `Stream` / `RecvHalf` / `SendHalf` 枚举本体：用 `dispatch!` 转发普通方法，**手写 `match`** 实现 `split` / `reunite`，并为枚举及引用实现标准库 `Read` / `Write`。 |
| `src/error.rs` | `ReuniteError` 结构（归还两半所有权）与 `ReuniteResult` 别名；还定义了与本讲相关的 `ConversionError`。 |
| `src/os/unix/uds_local_socket/stream.rs` | Unix 后端：`Stream` 包一个 `Arc`，`split` 用 `Arc::clone`，`reunite` 用 `Arc::ptr_eq` 判同源。 |
| `src/os/windows/named_pipe/local_socket/stream.rs` | Windows 后端：`split` / `reunite` 委托给 `DuplexPipeStream`；`set_recv/send_timeout` 恒返回 `Unsupported`。 |

> 提示：后端两个文件不是本讲的最小模块，但它们能让「平台差异」变得具体可见，精读时会引用其中的关键行。

## 4. 核心概念与源码讲解

### 4.1 traits::Stream：读写、非阻塞与超时

#### 4.1.1 概念说明

`Stream` 是「一条已连接的、可读可写的 local socket 字节流」。trait 本身只描述「能做什么」，不关心底层是 UDS 还是 named pipe。它的能力分四类：

1. **连接**：`connect`（默认实现）/ `from_options`（后端落地）。
2. **读写**：通过 supertrait `Read + Write`（按值 `&mut self`）以及 `RefRead + RefWrite`（按引用 `&self`）获得——见 4.1.3。
3. **阻塞控制**：`set_nonblocking`、`set_recv_timeout`、`set_send_timeout`。
4. **形态变换**：`split`（拆）、`reunite`（合）。

注意区分两个「超时」概念，这是初学者最常混淆的点：

- **收发超时**（本讲的 `set_recv/send_timeout`）：连接已经建立，控制「读 / 写单个操作最多阻塞多久」。
- **连接等待**（u3-l2 的 `ConnectWaitMode`）：控制「连接进行中」如何处理。

两者作用于连接生命周期的不同阶段，互不影响。

#### 4.1.2 核心流程

拿到 `Stream` 后，典型的读写控制流程：

```text
stream = Stream::connect(name)        # 已连接
        │
        ├── stream.set_nonblocking(true/false)   # 切非阻塞/阻塞
        ├── stream.set_recv_timeout(Some(1s))    # 读超时（Windows 不支持！）
        ├── stream.set_send_timeout(Some(1s))    # 写超时（Windows 不支持！）
        │
        └── 用 stream.read()/write() 收发
```

非阻塞模式下，两种情况会立即返回 `Err(io::ErrorKind::WouldBlock)`：

- 读时没有新数据；
- 写时发送缓冲区已满（对端还没把之前的数据读走）。

#### 4.1.3 源码精读

先看 trait 定义，注意它的 supertrait 链：[src/local_socket/stream/trait.rs:22-27](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/stream/trait.rs#L22-L27)

```rust
pub trait Stream: Read + RefRead + Write + RefWrite + StreamCommon {
    type RecvHalf: RecvHalf<Stream = Self>;
    type SendHalf: SendHalf<Stream = Self>;
```

这条 supertrait 链说明：一个 `Stream` 既能「按值」(`Read`/`Write`，即 `&mut self`) 又能「按引用」(`RefRead`/`RefWrite`，即 `&self`) 读写，并且必须实现 `StreamCommon`。`RefRead`/`RefWrite` 来自 `bound_util`（u6-l3 详解），它用 GAT 把「`&Self` 实现了 `Read`」这一事实编码进类型系统——这正是官方推荐「把 `Stream` 放进 `Arc` 共享读写、而不必 `split`」的类型论依据。

阻塞控制三个方法：[src/local_socket/stream/trait.rs:36-51](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/stream/trait.rs#L36-L51)

```rust
fn set_nonblocking(&self, nonblocking: bool) -> io::Result<()>;
fn set_recv_timeout(&self, timeout: Option<Duration>) -> io::Result<()>;
fn set_send_timeout(&self, timeout: Option<Duration>) -> io::Result<()>;
```

公共 `Stream` 枚举用 `dispatch!` 把它们转发给后端：[src/local_socket/stream/enum.rs:89-101](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/stream/enum.rs#L89-L101)

```rust
fn set_nonblocking(&self, nonblocking: bool) -> io::Result<()> {
    dispatch!(Self: x in self => x.set_nonblocking(nonblocking))
}
fn set_recv_timeout(&self, timeout: Option<Duration>) -> io::Result<()> {
    dispatch!(Self: x in self => x.set_recv_timeout(timeout))
}
```

真正的平台差异藏在后端。Unix 后端直接委托给标准库的 `UnixStream`：[src/os/unix/uds_local_socket/stream.rs:59-66](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/uds_local_socket/stream.rs#L59-L66)

```rust
fn set_recv_timeout(&self, timeout: Option<Duration>) -> io::Result<()> {
    self.0.set_read_timeout(timeout)
}
fn set_send_timeout(&self, timeout: Option<Duration>) -> io::Result<()> {
    self.0.set_write_timeout(timeout)
}
```

而 Windows 后端对收发超时**恒返回错误**：[src/os/windows/named_pipe/local_socket/stream.rs:25-27](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/local_socket/stream.rs#L25-L27) 与 [src/os/windows/named_pipe/local_socket/stream.rs:52-55](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/local_socket/stream.rs#L52-L55)

```rust
fn no_timeouts() -> io::Result<()> {
    Err(io::Error::new(io::ErrorKind::Unsupported, "named pipes do not support I/O timeouts"))
}
...
fn set_recv_timeout(&self, _: Option<Duration>) -> io::Result<()> { no_timeouts() }
fn set_send_timeout(&self, _: Option<Duration>) -> io::Result<()> { no_timeouts() }
```

> 关键结论：**同样的 `set_recv_timeout` 调用，在 Unix 上设置成功，在 Windows 上返回 `Err(Unsupported)`**。这是 local socket「统一接口、平台差异下沉到后端」设计的一个典型实例——公共 trait 签名一致，行为却不同。如果你要写跨平台代码，调用超时方法后必须检查返回值。

#### 4.1.4 代码实践

**实践目标**：亲眼看到「收发超时」在不同平台上的行为差异。

**操作步骤**：

1. 复用 u3-l1 的监听器（或 u1-l4 的回显 server），让它跑起来但不发送数据。
2. 写一个客户端，连接后立刻设置读超时，然后在没有任何数据到来的情况下 `read`：

```rust
// 示例代码：客户端
use interprocess::local_socket::{prelude::*, Stream, GenericNamespaced};
use std::{io::Read, time::Duration};

fn main() -> std::io::Result<()> {
    let name = "example-timeout.sock".to_ns_name::<GenericNamespaced>()?;
    let stream = Stream::connect(name)?;

    // 设置读超时
    match stream.set_recv_timeout(Some(Duration::from_millis(200))) {
        Ok(()) => println!("设置读超时成功（Unix 预期走到这里）"),
        Err(e) => println!("设置读超时失败：{e}（Windows named pipe 预期走到这里）"),
    }

    let mut buf = [0u8; 16];
    let res = stream.read(&mut buf); // 无数据可读
    println!("read 结果：{res:?}");
    Ok(())
}
```

**需要观察的现象**：

- 在 **Unix** 上：`set_recv_timeout` 返回 `Ok(())`，约 200ms 后 `read` 返回 `Err(WouldBlock)`（超时触发）。
- 在 **Windows** 上：`set_recv_timeout` 返回 `Err(Unsupported)`；若跳过超时设置，`read` 会一直阻塞（因为没有数据，也没有超时）。

**预期结果**：平台行为截然不同。Windows 部分的具体错误种类「待本地验证」（取决于运行环境），但 `Unsupported` 这一点由源码确定。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Stream` 的 supertrait 里要同时有 `Read` 和 `RefRead`，而不是只要 `Read`？

**参考答案**：`Read`（标准库）对应 `&mut self` 读写；而 `RefRead` 把「`&self` 也能 `Read`」编码进类型系统。后者让我们可以把 `Stream` 放进 `Arc` 后，仅靠共享引用 `&Stream` 就能读写，从而避免 `split` 的代价（见 4.3）。

**练习 2**：`set_nonblocking(true)` 之后，`read` 在无数据时返回什么？`set_recv_timeout` 的超时和它是什么关系？

**参考答案**：返回 `Err(io::ErrorKind::WouldBlock)`，且**不阻塞**。超时是「阻塞模式」下给「最长等多久」设的上限；一旦进入非阻塞模式，操作根本不等待，超时也就无从触发。两者是「是否等待」与「等待多久」的不同维度。

---

### 4.2 StreamCommon：错误取出与对端凭据

#### 4.2.1 概念说明

`StreamCommon` 收纳「与具体读写方式无关、但所有流（同步与异步）都该有的瞬时查询」。它的定义是：[src/local_socket/stream/trait.rs:79](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/stream/trait.rs#L79)

```rust
pub trait StreamCommon: Debug + Send + Sync + Sized + Sealed + 'static {
```

注意它的 supertrait 约束：要求 `Send + Sync + Sized + 'static`，并通过 `Sealed` 封印——即外部代码无法自己实现该 trait，只能用库提供的类型。两个方法：

- `take_error`：取出内核里「暂存的异步错误」（如 `SO_ERROR`），取走后清空，再次调用返回 `None`。
- `peer_creds`：查询对端凭据（pid、euid/egid 等）。这一项平台差异极大，本讲只点到，详细用法与安全注意事项在 **u3-l4** 专讲。

#### 4.2.2 核心流程

```text
stream.take_error()  ->  Ok(None)  无暂存错误
                    ->  Ok(Some(e)) 取出一个暂存错误并清空
                    ->  Err(e)      取操作本身失败（如系统调用出错）

stream.peer_creds()  ->  Ok(PeerCreds{ pid, ... })   见 u3-l4
```

#### 4.2.3 源码精读

trait 方法签名：[src/local_socket/stream/trait.rs:80-95](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/stream/trait.rs#L80-L95)

```rust
fn take_error(&self) -> io::Result<Option<io::Error>>;
/// ... using some of them for making security decisions may be subject to race conditions.
fn peer_creds(&self) -> io::Result<PeerCreds>;
```

枚举层转发：[src/local_socket/stream/enum.rs:132-139](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/stream/enum.rs#L132-L139)

```rust
impl r#trait::StreamCommon for Stream {
    fn take_error(&self) -> io::Result<Option<io::Error>> {
        dispatch!(Self: x in self => x.take_error())
    }
    fn peer_creds(&self) -> io::Result<PeerCreds> { dispatch!(Self: x in self => x.peer_creds()) }
}
```

平台差异同样在后端。Unix 后端用一个真正的系统调用包装取出错误：[src/os/unix/uds_local_socket/stream.rs:84-91](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/uds_local_socket/stream.rs#L84-L91)

```rust
fn take_error(&self) -> io::Result<Option<io::Error>> { c_wrappers::take_error(self.as_fd()) }
```

而 Windows 后端**恒返回 `Ok(None)`**：[src/os/windows/named_pipe/local_socket/stream.rs:69-76](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/local_socket/stream.rs#L69-L76)

```rust
impl traits::StreamCommon for Stream {
    fn take_error(&self) -> io::Result<Option<io::Error>> { Ok(None) }
    fn peer_creds(&self) -> io::Result<PeerCreds> {
        Ok(PeerCredsInner { pid: self.0.peer_process_id()? }.into())
    }
}
```

> 结论：`take_error` 在 Unix 上有意义，在 Windows local socket 上永远拿不到东西。这又是一个「公共签名一致、平台行为不同」的例子——别假设 `take_error` 跨平台都能返回真实错误。

#### 4.2.4 代码实践

**实践目标**：在连接刚建立时调用 `take_error` 与 `peer_creds`，观察初始状态。

**操作步骤**：在上面的客户端里，连接成功后立即加两行：

```rust
println!("take_error = {:?}", stream.take_error()?);   // 预期 None（刚连接，无错误）
println!("peer_creds = {:?}", stream.peer_creds());     // 打印对端 pid 等，详见 u3-l4
```

**需要观察的现象**：`take_error` 在两个平台上都应返回 `Ok(None)`（连接刚建立，无暂存错误）。`peer_creds` 在 Unix 上能看到 euid/egid，在 Windows 上至少能看到 `pid`。

**预期结果**：`take_error` 的初始 `None` 是确定的；`peer_creds` 的具体字段随平台与运行账号变化，「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`take_error` 为什么叫 `take`（取走）而不是 `get`（获取）？

**参考答案**：因为取出后内核里的暂存错误会被清空，连续两次调用，第二次返回 `None`。它是有副作用的「消费式」读取，不是幂等查询，故用 `take`。

**练习 2**：`StreamCommon` 的 supertrait 含 `Sealed`。这给库的扩展性带来什么影响？

**参考答案**：外部类型无法实现 `StreamCommon`（及其衍生的 `Stream`），只能使用库内置的后端。这锁死了「哪些类型能被塞进公共 `Stream` 枚举」，保证了 enum dispatch 的变体集合是封闭的。

---

### 4.3 split：拆成 RecvHalf / SendHalf

#### 4.3.1 概念说明

`split` 把一条 `Stream`（按值消费）拆成「接收半」`RecvHalf` 与「发送半」`SendHalf`。两半可以分别交给不同线程，实现**真正的并发全双工**：一个线程只管读、另一个只管写，互不阻塞。

但是——trait 的文档注释明确**劝退**这种用法，给出了两条理由：[src/local_socket/stream/trait.rs:53-60](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/stream/trait.rs#L53-L60)

```rust
/// Splits a stream into a receive half and a send half.
///
/// You probably want to avoid this mechanism for the following reasons:
/// - Placing a stream in an `Rc` or `Arc` produces identical behavior,
///   since `&Stream` implements `Read` and `Write`
/// - Dropping a half does not shut it down like it does with sockets,
///   which may be counterintuitive
fn split(self) -> (Self::RecvHalf, Self::SendHalf);
```

理解这两条：

1. **`Arc` 等价且更省事**：因为 `&Stream: Read + Write`（supertrait `RefRead`/`RefWrite`），把 `Stream` 放进 `Arc`、各线程拿 `&Stream` 即可同时读写，效果和 `split` 一样，还省去了 `split`/`reunite` 的形态管理。
2. **drop 一半不会 shutdown**：对普通 socket，drop 读半/写半会关闭对应方向（对端收到 EOF / 写失败）。但 local socket 无法跨平台 shutdown，所以**drop 一半只是丢弃这半的句柄，连接方向不会被关闭**——这与直觉相悖。

`RecvHalf` / `SendHalf` 各自是一个独立的 trait，约束同样含 `Sealed`、`Send + Sync`：[src/local_socket/stream/trait.rs:98-124](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/stream/trait.rs#L98-L124)。它们只暴露各自方向需要的接口：`RecvHalf` 只有 `Read`、`SendHalf` 只有 `Write`，外加各自的 `set_timeout`。

#### 4.3.2 核心流程

```text
stream.split()  ->  (RecvHalf, SendHalf)    # 按值消费 stream
                        │           │
        (可分别移交给线程)  读/recv      写/send
                        │           │
                        └────┬──────┘
                             ▼
                    Stream::reunite(rh, sh)  # 见 4.4
```

`RecvHalf` / `SendHalf` 都被标注 `#[derive(Clone, Debug)]`（后端，见 4.3.3），所以在 Unix 上它们其实是廉价的 `Arc` 克隆——但你**不应**依赖这一点写跨平台代码，因为 trait 层面并不保证 `Clone`。

#### 4.3.3 源码精读

公共枚举的 `split` 是**手写 `match`**，不是 `dispatch!`：[src/local_socket/stream/enum.rs:103-116](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/stream/enum.rs#L103-L116)

```rust
fn split(self) -> (RecvHalf, SendHalf) {
    match self {
        #[cfg(windows)]
        Stream::NamedPipe(s) => {
            let (rh, sh) = s.split();
            (RecvHalf::NamedPipe(rh), SendHalf::NamedPipe(sh))
        }
        #[cfg(unix)]
        Stream::UdSocket(s) => {
            let (rh, sh) = s.split();
            (RecvHalf::UdSocket(rh), SendHalf::UdSocket(sh))
        }
    }
}
```

为什么手写？因为 `split(self)` 按值消费 `self`，而 u2-l2 讲过 `dispatch!` 宏只支持 `&self`/`&mut self`，不支持按值消费。所以凡是「吃掉所有权」的方法（`split`、`reunite`）都得手写 `match`。注意这里先把 `self` 解构成后端流 `s`，调用后端 `s.split()`，再把返回的后端半边重新包回公共枚举 `RecvHalf::NamedPipe(...)`。

Unix 后端的 `split` 实现极其直白——**就是包一层 `Arc` 再各克隆一份**：[src/os/unix/uds_local_socket/stream.rs:68-72](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/uds_local_socket/stream.rs#L68-L72)

```rust
fn split(self) -> (RecvHalf, SendHalf) {
    let arc = Arc::new(self);
    (RecvHalf(Arc::clone(&arc)), SendHalf(arc))
}
```

两个半边其实是 `Arc<Stream>` 的两个强引用，强引用计数为 2。这正是 `RecvHalf`/`SendHalf` 标注 `#[derive(Clone, Debug)]` 的原因：[src/os/unix/uds_local_socket/stream.rs:167-169](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/uds_local_socket/stream.rs#L167-L169)

```rust
/// [`Stream`]'s receive half, implemented using [`Arc`].
#[derive(Clone, Debug)]
pub struct RecvHalf(pub(super) Arc<Stream>);
```

Windows 后端则委托给 `DuplexPipeStream::split`（那是 `maybe_arc` 优化层，u8-l2 专讲）：[src/os/windows/named_pipe/local_socket/stream.rs:57-61](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/local_socket/stream.rs#L57-L61)

```rust
fn split(self) -> (RecvHalf, SendHalf) {
    let (rh, sh) = self.0.split();
    (RecvHalf(rh), SendHalf(sh))
}
```

> 结论：**Unix 上 `split` 的代价就是一次堆分配（`Arc::new`）**；Windows 上则取决于 `maybe_arc` 是否已升级为 `Arc`（未拆分时内联、零分配，u8-l2）。这正是官方建议「用 `Arc<Stream>` 代替 `split`」的微妙之处——两条路在 Unix 上殊途同归，都落到 `Arc`。

#### 4.3.4 代码实践

**实践目标**：把一条流 `split` 后，用 `thread::scope` 让收发两半并发工作（全双工），再 `reunite` 合回。这是本讲的主干可运行实践。

**操作步骤**：

1. 启动 u3-l1（或 u1-l4）的回显 server：收到一行、原样写回。
2. 编写客户端（示例代码）：

```rust
// 示例代码：客户端——split + 并发收发 + reunite
use interprocess::local_socket::{prelude::*, Stream, GenericNamespaced};
use std::{
    io::{prelude::*, BufReader},
    thread,
};

fn main() -> std::io::Result<()> {
    let name = "example-split.sock".to_ns_name::<GenericNamespaced>()?;
    let conn = Stream::connect(name.clone())?; // 假设回显 server 已启动

    let (mut rh, mut sh) = conn.split(); // 按值消费 conn

    let echoed = thread::scope(|s| -> std::io::Result<Vec<u8>> {
        // 读线程：借用 &mut rh
        let reader = s.spawn(|| {
            let mut buf = Vec::new();
            BufReader::new(&mut rh).read_until(b'\n', &mut buf)?;
            Ok(buf)
        });
        // 主线程：用 sh 写
        sh.write_all(b"ping\n")?;
        reader.join().unwrap()
    })?;
    // 作用域结束，rh/sh 的可变借用释放，所有权回到当前栈
    println!("收到回显：{:?}", String::from_utf8_lossy(&echoed));

    // 合并回来（成功路径）
    let _conn = Stream::reunite(rh, sh)?;
    println!("reunite 成功");
    Ok(())
}
```

**需要观察的现象**：

- 发送 `ping\n` 后，读线程能立刻读到回显 `ping\n`，二者**并发进行、互不阻塞**——这就是全双工相对 u1-l4 串行模式的优势。
- 作用域结束后 `reunite` 成功，没有报错。

**预期结果**：成功打印回显与「reunite 成功」。本实践依赖一个能正确回显的 server；若你复用的是 u1-l4 那种「先收后发」的同步 server，单连接场景下仍可工作（server 先读完一行再写回）。「待本地验证」多客户端并发时 server 的处理顺序。

#### 4.3.5 小练习与答案

**练习 1**：既然官方劝退 `split`，那它在什么场景下仍值得用？

**参考答案**：当你需要让收发两半**类型不同、各自独立移动**（例如把读半交给一个长期消费者线程、写半留在主线程），并且不希望引入 `Arc` 的共享可变性管理时，`split` 更直接。但绝大多数「两个线程同时读写同一条流」的场景，用 `Arc<Stream>` + `&Stream` 更简单。

**练习 2**：为什么 `RecvHalf` 的 trait 只有 `Read` 而 `SendHalf` 只有 `Write`？

**参考答案**：因为它们代表半双工的单一方向。`RecvHalf` 只负责接收，故约束为 `Read + RefRead`；`SendHalf` 只负责发送，故约束为 `Write + RefWrite`。这样类型系统就能保证「拿到读半的人没法误写」。

---

### 4.4 reunite：重聚与 ReuniteError 的所有权归还

#### 4.4.1 概念说明

`reunite` 是 `split` 的逆运算：把一个 `RecvHalf` 和一个 `SendHalf` 合并回原来的 `Stream`。它的签名是**关联函数**（没有 `self`）：[src/local_socket/stream/trait.rs:62-65](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/stream/trait.rs#L62-L65)

```rust
fn reunite(rh: Self::RecvHalf, sh: Self::SendHalf) -> ReuniteResult<Self>;
```

所以调用形式是 `Stream::reunite(rh, sh)`（与测试里 `DuplexPipeStream::reunite(recver, sender)` 一致）。

关键问题：**如何判断两半属于同一条流？** 这取决于后端：

- Unix：两半各持一个 `Arc<Stream>`，用 `Arc::ptr_eq` 比较是否指向**同一个**堆对象。
- Windows：委托给 `DuplexPipeStream::reunite`，内部用 `maybe_arc` 的 `ptr_eq`（u8-l2）。

如果不属于同一条流，`reunite` 失败，返回 `ReuniteError`。而这个错误**不是丢弃两半，而是把两半的所有权原样还给你**——这是本讲最重要的设计。

#### 4.4.2 核心流程

Unix 后端的 `reunite` 逻辑（强引用计数视角）：

```text
split 之后：    Arc 强引用计数 = 2   (RecvHalf 持 1, SendHalf 持 1)

reunite(rh, sh):
  if !Arc::ptr_eq(rh.arc, sh.arc):     # 不是同一个 Arc → 不同流
      return Err(ReuniteError{ rh, sh })  # 原样归还两半
  drop(rh);                            # 计数 2 -> 1
  Arc::into_inner(sh.arc)              # 计数 1 -> 0，取出内部 Stream
      .expect("stream half inexplicably copied")
```

`Arc::into_inner` 只有在「这是最后一个强引用」时才返回 `Some`。因为 `reunite` 按值拿走了两半、又先 `drop(rh)`，此时 `sh` 是唯一引用，所以能安全取回内部的 `Stream`。整套机制没有任何 `unsafe`，完全建立在 `Arc` 的所有权语义上。

#### 4.4.3 源码精读

公共枚举的 `reunite` 同样是手写 `match`：[src/local_socket/stream/enum.rs:117-130](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/stream/enum.rs#L117-L130)

```rust
fn reunite(rh: RecvHalf, sh: SendHalf) -> ReuniteResult {
    match (rh, sh) {
        #[cfg(windows)]
        (RecvHalf::NamedPipe(rh), SendHalf::NamedPipe(sh)) => {
            np_impl::Stream::reunite(rh, sh).map(From::from).map_err(|e| e.convert_halves())
        }
        #[cfg(unix)]
        (RecvHalf::UdSocket(rh), SendHalf::UdSocket(sh)) => {
            uds_impl::Stream::reunite(rh, sh).map(From::from).map_err(|e| e.convert_halves())
        }
        #[allow(unreachable_patterns)]
        (rh, sh) => Err(ReuniteError { rh, sh }),
    }
}
```

读这段要分两层：

1. **变体匹配层**：先把两半解构成后端类型，委托给后端 `reunite`。由于同一构建只有一个后端编译进来，末尾的兜底 `(rh, sh) => Err(...)` 实际**不可达**（故 `#[allow(unreachable_patterns)]`）。真正的「不同流」判定发生在后端内部。
2. **错误类型转换**：后端返回的后端专属 `ReuniteError`，经 `e.convert_halves()` 转换回公共枚举层的 `ReuniteError`（见下文）。

Unix 后端的真正判定：[src/os/unix/uds_local_socket/stream.rs:73-82](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/uds_local_socket/stream.rs#L73-L82)

```rust
fn reunite(rh: RecvHalf, sh: SendHalf) -> ReuniteResult<Self> {
    if !Arc::ptr_eq(&rh.0, &sh.0) {
        return Err(ReuniteError { rh, sh });
    }
    drop(rh);
    let inner = Arc::into_inner(sh.0).expect("stream half inexplicably copied");
    Ok(inner)
}
```

`ReuniteError` 的结构——注意两个字段都是 `pub`，把两半的所有权交还调用者：[src/error.rs:155-163](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/error.rs#L155-L163)

```rust
#[derive(Debug)]
pub struct ReuniteError<R, S> {
    /// Ownership of the receive half.
    pub rh: R,
    /// Ownership of the send half.
    pub sh: S,
}
```

它的 `Display` 文案固定，且实现了 `Error`：[src/error.rs:183-188](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/error.rs#L183-L188)

```rust
impl<R, S> Display for ReuniteError<R, S> {
    fn fmt(&self, f: &mut Formatter<'_>) -> fmt::Result {
        f.write_str("attempt to reunite stream halves that come from different streams")
    }
}
impl<R: Debug, S: Debug> Error for ReuniteError<R, S> {}
```

最后看 `convert_halves`——它在「公共枚举 ↔ 后端」之间搬运错误里的两半类型：[src/error.rs:164-182](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/error.rs#L164-L182)

```rust
pub fn map_halves<NR: From<R>, NS: From<S>>(
    self, fr: impl FnOnce(R) -> NR, fs: impl FnOnce(S) -> NS,
) -> ReuniteError<NR, NS> { ... }
pub fn convert_halves<NR: From<R>, NS: From<S>>(self) -> ReuniteError<NR, NS> {
    self.map_halves(From::from, From::from)
}
```

> 设计要点：`ReuniteError` 泛型 `<R, S>` 让「两半」的具体类型可变。后端 `reunite` 失败时返回的是「后端半边」的 `ReuniteError`，枚举层用 `convert_halves()` 把它们 `From::from` 成「公共枚举半边」，对外只暴露公共类型。这正是 u2-l2/u2-l3「壳/芯」分层在错误类型上的延续。

#### 4.4.4 代码实践

**实践目标**：故意把「来自不同流」的两半送去 `reunite`，观察 `ReuniteError`，并证明所有权被完整归还。

**操作步骤**：接 4.3 的客户端，在成功 `reunite` 之后再追加一段「错误重组」实验（示例代码）：

```rust
// 示例代码：制造跨流 reunite 失败
use interprocess::local_socket::{Stream, ReuniteError};

// 假设我们又连了两条独立的流 conn_a、conn_b（需要 server 能接受多连接）
let conn_a = Stream::connect(name.clone())?;
let conn_b = Stream::connect(name.clone())?;
let (rh_a, _sh_a) = conn_a.split();
let (_rh_b, sh_b) = conn_b.split();

// 交叉重组：用 a 的收半 + b 的发半
match Stream::reunite(rh_a, sh_b) {
    Ok(_) => println!("不该走到这里：两半来自不同流"),
    Err(ReuniteError { rh, sh }) => {
        // 所有权被完整归还！rh 是 a 的收半，sh 是 b 的发半
        println!("预期中的失败：两半来自不同流");
        println!("我仍然持有 rh（来自 conn_a）和 sh（来自 conn_b）");
        // 可以分别 drop 或继续使用，不会丢失任何资源
    }
}
```

**需要观察的现象**：`reunite` 返回 `Err(ReuniteError { .. })`，且通过解构能拿到 `rh` 与 `sh`——证明两半没有在失败时被丢弃。

**预期结果**：在 Unix 上，`Arc::ptr_eq` 判定两半指向不同 `Arc`，必然返回 `Err`。在 Windows 上由 `DuplexPipeStream::reunite` 判定，结论一致。跨平台行为确定。**「待本地验证」** 的只有一点：要让 `conn_a` / `conn_b` 两次 `connect` 都成功，server 必须能并发接受连接（见 u4-l3 对 named pipe「连接即就绪」与 accept 循环的说明）；若用串行 server，第二次连接可能阻塞——此时可改在 server 端 `accept()` 两条流来构造实验。

#### 4.4.5 小练习与答案

**练习 1**：`reunite` 失败时为什么不直接 `panic` 或丢弃两半，而要包成 `ReuniteError` 归还？

**参考答案**：因为两半持有真实的 OS 句柄，丢弃会造成资源泄漏或意外关闭。把所有权原样归还，调用者可以选择重新配对、单独使用或显式 drop，资源管理始终显式且不丢失。这与 `error.rs` 中 `ConversionError` 的 `source` 字段「失败时归还输入所有权」是同一设计哲学。

**练习 2**：在公共枚举的 `reunite` 里，末尾的 `(rh, sh) => Err(ReuniteError { rh, sh })` 为什么标了 `#[allow(unreachable_patterns)]`？

**参考答案**：因为同一构建里只有一个后端（`cfg(unix)` 或 `cfg(windows)` 二选一），上面的两个匹配臂必有一个能匹配所有合法输入，兜底臂永远不会执行。但它必须在语法上存在以让 `match` 穷尽，故用 lint 属性消音。

**练习 3**：Unix 后端 `reunite` 里 `Arc::into_inner(sh.0).expect(...)` 为什么可以安全 `expect`，不会 panic？

**参考答案**：通过 `ptr_eq` 判定后，`rh` 与 `sh` 指向同一个 `Arc`，强引用计数为 2。`reunite` 按值拥有两半，先 `drop(rh)` 把计数降到 1，此时 `sh.0` 是唯一强引用，`Arc::into_inner` 必然返回 `Some`。panic 只在「两半虽同源但其中之一被额外克隆」时才会发生，而 trait 层面的使用方式不会产生这种克隆，故实际上不会触发。

---

## 5. 综合实践

把本讲的知识串成一个完整的「全双工回显客户端 + 错误恢复」小程序。

**任务**：基于 u1-l4 的回显 server，写一个客户端，完成以下全部步骤，并解释每一步对应的源码机制：

1. 用 `Stream::connect` 连接服务端。
2. 调用 `set_recv_timeout(Some(500ms))`，**检查返回值**，用文字记录你在当前平台是 `Ok` 还是 `Err(Unsupported)`（呼应 4.1.3 的平台差异）。
3. `split` 出收发两半，用 `thread::scope` 让读、写并发执行：发送 3 行 `hello N\n`，读回 3 行回显。
4. 用 `Stream::reunite` 合并两半，确认成功。
5. 再连一条流，故意「跨流 reunite」，捕获 `ReuniteError`，从其 `.rh` / `.sh` 字段取回两半，分别 `drop` 它们，观察程序正常退出、无资源泄漏。

**验收要点**：

- 步骤 2 能说清「为什么 Windows 会失败」。
- 步骤 3 能解释「为什么这里不会像 u1-l4 那样死锁」（全双工 + split 后并发收发）。
- 步骤 4/5 能画出 `split → Arc 计数=2 → reunite drop(rh) → Arc::into_inner` 的所有权流转图。
- 若步骤 5 因 server 不支持并发连接而无法构造两条流，改为「在 server 端 accept 两条流、跨流 reunite」并说明理由。

## 6. 本讲小结

- `Stream` trait 用一条 supertrait 链 `Read + RefRead + Write + RefWrite + StreamCommon` 同时支持按值与按引用读写，后者是用 GAT 把 `&Self: Read` 编码进类型系统的结果。
- 阻塞控制三件套 `set_nonblocking` / `set_recv_timeout` / `set_send_timeout` 在 **Unix 上正常工作，在 Windows local socket 上后两者恒返回 `Err(Unsupported)`**——同一签名、不同行为。
- `StreamCommon` 提供 `take_error`（Unix 取暂存错误、Windows 恒 `None`）与 `peer_creds`（详见 u3-l4），它是「同步/异步共用」的瞬时查询接口，且被 `Sealed` 封印。
- `split` 把流拆成 `RecvHalf`/`SendHalf`，但官方建议用 `Arc<Stream>` 代替；公共枚举里 `split`/`reunite` 因按值消费而是**手写 `match`**，不走 `dispatch!`。
- Unix 后端 `split` = 包一层 `Arc` 再克隆，`reunite` 用 `Arc::ptr_eq` 判同源、`Arc::into_inner` 取回，全程无 `unsafe`。
- `ReuniteError` 在失败时**原样归还两半所有权**（`pub rh`/`pub sh`），并通过 `convert_halves`/`map_halves` 在公共层与后端层之间搬运半边类型。

## 7. 下一步学习建议

- **u3-l4 对端凭据 PeerCreds**：本讲只点了 `peer_creds`，它的平台分层、字段可用性与「用 PID 鉴权的竞态风险」值得专讲。
- **u4-l1 / u4-l2 平台后端剖析**：本讲多次提到「行为差异下沉到后端」，下一单元会带你看 `dispatch_sync` 如何把 `ConnectOptions` 路由到 UDS / named pipe 后端的 `from_options`，把这里的「壳/芯」关系在连接路径上看完整。
- **u6-l3 bound_util 与引用约束**：想彻底弄懂 `RefRead`/`RefWrite` 如何用 GAT 表达 `&T: Read`，以及它为什么是「`Arc` 替代 `split`」的类型论基础，留到异步单元展开。
- **u7-l3 错误处理体系**：本讲的 `ReuniteError` 只是 `error.rs` 的一部分；`ConversionError` 的「details + cause + source」三段式与所有权归还语义将在专家层系统讲解。
- **u8-l2 maybe_arc 与流拆分优化**：本讲提到 Windows 后端 `split`/`reunite` 委托给 `DuplexPipeStream`，其内部的 `MaybeArc`（Inline vs Shared）如何在「未拆分时零开销、拆分时才升级为 `Arc`」，是 Windows 流的精华。
