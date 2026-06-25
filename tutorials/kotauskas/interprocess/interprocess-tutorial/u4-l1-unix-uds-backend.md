# Unix 后端：UDS local socket 实现

## 1. 本讲目标

在前面的单元里，我们一直把 local socket 当作一个「跨平台的抽象外壳」在使用：`ListenerOptions`、`ConnectOptions`、`Stream`、`prelude` 都属于公共层。本讲要**把壳撬开**，看清 Unix 平台上这层抽象背后真正的「芯」——`os::unix::uds_local_socket` 模块。

学完本讲，你应当能够：

1. 说清 `dispatch_sync`（以及 `dispatch_tokio`）这一层极薄的「派发入口」如何把 `ListenerOptions` / `ConnectOptions` 路由到 Unix 后端，并理解「同一个 `create_sync_as` / `connect_sync_as` 在链上被调用两次」这一关键设计。
2. 读懂 Unix 后端的 `Listener` 如何包装标准库的 `UnixListener`、`Stream` 如何包装 `UnixStream`，以及 `accept` / `set_nonblocking` / `split` / `reunite` 等方法分别落地到哪些系统调用。
3. 理解地址解释函数 `dispatch_name` 如何把一个 `Name`（路径 / 抽象命名空间 / 伪命名空间）翻译成 `sockaddr_un`，从而对接 `name_type` 系统里的四个标记类型。

## 2. 前置知识

- **Unix domain socket（UDS / AF_UNIX）**：同一台机器上两个进程通过一个「socket 文件路径」或「抽象命名空间名」建立的双向字节流连接。它是 Unix local socket 的底层操作系统原语。
- **`sockaddr_un` 与 `sun_path`**：内核用来描述一个 UDS 地址的 C 结构体，其中 `sun_path` 是一段定长缓冲区（容量记为 `SUN_LEN`），既可放文件路径（NUL 结尾），也可放 Linux 抽象命名空间名（首字节为 0）。
- **公共层壳 / 平台后端芯**（见 u2-l3）：公共类型只是 newtype 壳，真正执行系统调用的是 `os::unix` / `os::windows` 后端；二者经 `impmod!` 注入。
- **enum dispatch**（见 u2-l2）：公共 `Listener` / `Stream` 是单变体枚举，`dispatch!` 宏把方法调用转发给后端。
- **`ReclaimGuard`、`AtomicBool`**：Rust 的 RAII 守卫与原子布尔，本讲会用到。
- **`connect()` 的 `EINPROGRESS`**：非阻塞 `connect()` 在连接未立即完成时返回的错误码，表示「连接正在进行中」，需要稍后用 `poll` 等待其就绪。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `src/os/unix/local_socket/dispatch_sync.rs` | 同步派发入口：`listen()` / `connect()`，把公共构建器路由到 Unix 后端。 |
| `src/os/unix/uds_local_socket.rs` | 后端顶层：共享辅助函数（`ReclaimGuard`、`dispatch_name`、`listen_and_maybe_overwrite`、`write_run_user` 等）。 |
| `src/os/unix/uds_local_socket/listener.rs` | 后端 `Listener`：包装 `UnixListener`，实现 `traits::Listener`。 |
| `src/os/unix/uds_local_socket/stream.rs` | 后端 `Stream`：包装 `UnixStream`，实现 `traits::Stream` / `StreamCommon`，以及 `RecvHalf` / `SendHalf`。 |
| `src/os/unix/c_wrappers.rs` | UDS 底层系统调用封装：`create_listener`、`create_client`、`set_nonblocking`、`take_error`、`wait_for_connect`。 |
| `src/os/unix/ud_addr.rs` | `UdAddr` / `TerminatedUdAddr`：对 `sockaddr_un` 的安全包装与 NUL 终止见证（witness）。 |
| `src/os/unix/local_socket/name_type.rs` | 四个标记类型（`FilesystemUdSocket` 等）如何把名字映射为 `NameInner` 变体。 |

> 本讲聚焦的「最小模块」是 `os::unix::uds_local_socket` 与 `os::unix::local_socket::dispatch_sync`；其余文件是理解它们所必需的支撑。

## 4. 核心概念与源码讲解

### 4.1 派发入口：dispatch_sync 与「两次调用」之谜

#### 4.1.1 概念说明

在 u2-l2 我们看到公共 `Listener` / `Stream` 枚举用 `dispatch!` 转发方法。但「创建」这一步（`from_options`）有点特殊：它发生在对象还不存在、必须由构建器构造的阶段。interprocess 的做法是——公共枚举的 `from_options` 不直接 new 出后端，而是**调用一个极薄的派发函数**，由这个函数再去构造后端、再 `From` 回公共枚举。

这个派发函数就住在 `dispatch_sync.rs` 里，整个文件只有十几行，是「壳」与「芯」之间唯一的桥梁。

#### 4.1.2 核心流程

服务端创建的完整派发链：

```
ListenerOptions::create_sync()                       // 公共入口
  └─ create_sync_as::<公共枚举 Listener>()           // 第 1 次调用 create_sync_as
       └─ 公共枚举 Listener::from_options(opts)      // enum.rs
            └─ dispatch::listen(opts)                // = dispatch_sync::listen（经 impmod! 注入）
                 └─ opts.create_sync_as::<后端 uds_impl::Listener>()  // 第 2 次调用 create_sync_as
                      └─ 后端 Listener::from_options(opts)            // 真正的系统调用
                 └─ .map(Listener::from)             // 后端结构体变回公共枚举
```

客户端连接的链路完全对称（`connect_sync` / `connect_sync_as` / `dispatch_sync::connect`）。

注意一个关键设计：`create_sync_as` / `connect_sync_as` **在一条链上被调用两次**。第一次的类型参数是公共枚举（负责「认出自己」并跳到派发函数）；第二次类型参数是具体后端（负责真正落地系统调用）。这两次调用走的是**同一个泛型函数**，只是实例化的 `S` 不同。

#### 4.1.3 源码精读

派发入口本体——`listen` 与 `connect` 各自只有一行有效代码：

[src/os/unix/local_socket/dispatch_sync.rs:L7-L14](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/local_socket/dispatch_sync.rs#L7-L14)

```rust
pub fn listen(options: ListenerOptions<'_>) -> io::Result<Listener> {
    options.create_sync_as::<uds_impl::Listener>().map(Listener::from)
}
pub fn connect(options: &ConnectOptions<'_>) -> io::Result<Stream> {
    options.connect_sync_as::<uds_impl::Stream>().map(Stream::from)
}
```

- `uds_impl` 是 `super::super::uds_local_socket`，即 Unix 后端模块。
- `create_sync_as::<uds_impl::Listener>()` 触发后端 `Listener::from_options`（见 4.2）。
- `.map(Listener::from)` 把后端结构体包回公共枚举——这里用的是 u2-l2 提到、`mkenum!` 自动生成的 `From` 实现。

公共枚举层是怎么把 `from_options` 接到这个 `dispatch_sync` 的？靠 `impmod!` 注入别名：

[src/local_socket/listener/enum.rs:L11-L13](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/listener/enum.rs#L11-L13)

```rust
impmod! {local_socket::dispatch_sync as dispatch}
```

[src/local_socket/listener/enum.rs:L61-L63](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/listener/enum.rs#L61-L63)

```rust
fn from_options(options: ListenerOptions<'_>) -> io::Result<Self> {
    dispatch::listen(options)
}
```

`impmod!` 按 `cfg(unix)` / `cfg(windows)` 把 `dispatch_sync` 模块以 `dispatch` 别名引入（Windows 上则引入 named pipe 的派发模块）。`Stream` 侧对称地用 `dispatch_sync::connect`（见 `stream/enum.rs` 第 85–86 行）。

两次调用的「扳机」是 `ListenerOptions` 上的泛型方法：

[src/local_socket/listener/options.rs:L206-L212](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/listener/options.rs#L206-L212)

```rust
pub fn create_sync(self) -> io::Result<Listener> { self.create_sync_as::<Listener>() }
/// ...
pub fn create_sync_as<L: traits::Listener>(self) -> io::Result<L> { L::from_options(self) }
```

`create_sync_as<L>` 的全部职责就是 `L::from_options(self)`。当 `L = 公共枚举 Listener` 时，进入派发函数；当 `L = 后端 uds_impl::Listener` 时，进入真正的后端构造。这正是「同一个函数、两次实例化」的来源。

#### 4.1.4 代码实践

**实践目标**：亲手在源码里走一遍「两次 `create_sync_as`」的调用，确认你对派发链的理解。

**操作步骤**：

1. 打开 `src/os/unix/local_socket/dispatch_sync.rs`，定位 `listen` 函数。
2. 在 `listen` 内的 `create_sync_as::<uds_impl::Listener>()` 处，意识到这一次 `L` 是**后端**类型。
3. 跳到 `src/local_socket/listener/enum.rs` 的 `from_options`，看到它调用 `dispatch::listen`——而这又是被 `ListenerOptions::create_sync()` → `create_sync_as::<公共 Listener>()` → `公共 Listener::from_options` 触发的，那一次 `L` 是**公共枚举**。
4. 用 `grep` 验证 `create_sync_as` 只有一处定义：

   ```text
   src/local_socket/listener/options.rs:211: pub fn create_sync_as<L: traits::Listener>(self) -> io::Result<L> { L::from_options(self) }
   ```

**需要观察的现象 / 预期结果**：你会确认 `create_sync_as` 确实只有一个泛型定义，却被两种 `L` 实例化调用，从而完成「公共 → 派发 → 后端」的三段式。

#### 4.1.5 小练习与答案

**练习 1**：如果未来 interprocess 在 Unix 上新增第二个 UDS 后端（比如基于 io-uring），`dispatch_sync::listen` 需要怎么改？

> **参考答案**：在 `listen` 内根据某种判据（如 `ConnectOptions` 里的开关或 name 类型）选择 `create_sync_as::<后端A>()` 或 `create_sync_as::<后端B>()`，再把结果 `.map(Listener::from)`。这正是 `create_sync_as` 设计成泛型的远期意义——派发函数有权决定实例化哪个后端。

**练习 2**：`connect` 的第二个参数为什么是 `&ConnectOptions`（引用），而 `listen` 是 `ListenerOptions`（按值）？

> **参考答案**：监听器创建会**消费**构建器（它需要拿走 `name` 等字段去 bind），故按值；而连接可能要重试（`dispatch_name` 会为伪命名空间尝试多个目录），构建器需要被反复读取，故传引用。

---

### 4.2 Listener 后端：UnixListener 封装、accept 与非阻塞

#### 4.2.1 概念说明

Unix 后端的 `Listener` 是一个**三字段结构体**，核心是标准库的 `std::os::unix::net::UnixListener`。它在上面挂了两个额外能力：**名称回收**（drop 时 unlink 僵尸 socket 文件，见 u3-l1）和**「新建连接默认非阻塞」开关**（解决 Windows named pipe 监听器非阻塞语义怪异而引入的跨平台统一接口）。

它实现了 `traits::Listener`——也就是公共层契约里那几个方法：`from_options`、`accept`、`set_nonblocking`、`do_not_reclaim_name_on_drop`，外加作为迭代器的 `next`。

#### 4.2.2 核心流程

**创建监听器（`from_options`）**：

```
from_options(opts)
  └─ listen_and_maybe_overwrite(opts, |addr, opts| {
        create_listener(SOCK_STREAM, addr, nonblocking_accept, mode)  // socket + fchmod + bind + listen
        reclaim = ReclaimGuard::new(reclaim_name, addr)               // 记下要回收的路径
     })
  └─ .map(UnixListener::from)   // 把 OwnedFd 包成标准库 UnixListener
  └─ 组装 Listener { listener, reclaim, nonblocking_streams }
```

**接受连接（`accept`）**：

```
listener.accept()                // 标准库阻塞/非阻塞 accept
  └─ 若 nonblocking_streams == true：对新 stream 调 fast_set_nonblocking(true)
```

**切换非阻塞模式（`set_nonblocking`）**：把 `ListenerNonblockingMode` 四态拆成两个独立动作——

| 模式 | `UnixListener` 本体非阻塞（影响 accept） | 新建 stream 非阻塞 |
|------|:---:|:---:|
| `Neither` | 否 | 否 |
| `Accept` | 是 | 否 |
| `Stream` | 否 | 是 |
| `Both` | 是 | 是 |

#### 4.2.3 源码精读

结构体本体——三个字段，`pub(super)` 暴露给同模块的 `stream.rs` 等使用：

[src/os/unix/uds_local_socket/listener.rs:L22-L28](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/uds_local_socket/listener.rs#L22-L28)

```rust
#[derive(Debug)]
pub struct Listener {
    pub(super) listener: UnixListener,
    pub(super) reclaim: ReclaimGuard,
    pub(super) nonblocking_streams: AtomicBool,
}
impl crate::Sealed for Listener {}
```

`impl crate::Sealed for Listener {}` 是对象安全封印（见 u2-l1）：只有后端类型才能实现 `traits::Listener`。

`from_options` 把构建器交给 `listen_and_maybe_overwrite`，闭包里调用 `c_wrappers::create_listener` 完成真正的 socket/bind/listen：

[src/os/unix/uds_local_socket/listener.rs:L32-L50](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/uds_local_socket/listener.rs#L32-L50)

```rust
fn from_options(opts: ListenerOptions<'_>) -> io::Result<Self> {
    let mut reclaim = ReclaimGuard::default();
    let nonblocking_streams = AtomicBool::new(opts.get_nonblocking_stream());
    Ok(Self {
        listener: listen_and_maybe_overwrite(opts, |addr, opts| {
            let rslt = c_wrappers::create_listener(
                libc::SOCK_STREAM,
                addr,
                opts.get_nonblocking_accept(),
                opts.get_mode(),
            )?;
            reclaim = ReclaimGuard::new(opts.get_reclaim_name(), addr);
            Ok(rslt)
        })
        .map(UnixListener::from)?,
        reclaim,
        nonblocking_streams,
    })
}
```

注意 `reclaim` 是 `let mut` 后在闭包里被赋值——闭包捕获的是可变引用，`listen_and_maybe_overwrite` 返回后才把最终地址写进 `reclaim`。

`accept` 直接用标准库 `UnixListener::accept`，并在需要时把新连接设为非阻塞：

[src/os/unix/uds_local_socket/listener.rs:L51-L59](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/uds_local_socket/listener.rs#L51-L59)

```rust
fn accept(&self) -> io::Result<Stream> {
    // TODO do our own accept4 and pass SOCK_NONBLOCK on supported platforms
    let stream = self.listener.accept().map(|(s, _)| Stream::from(s))?;
    if self.nonblocking_streams.load(Acquire) {
        c_wrappers::fast_set_nonblocking(stream.as_fd(), true)?;
    }
    Ok(stream)
}
```

- `accept()` 丢弃了返回的对端地址 `(_, _)`，因为 local socket 不使用它（`accept()` 抛弃 peer 地址是 UDS 的常见简化）。
- TODO 注释表明作者希望未来用 `accept4(SOCK_NONBLOCK)` 原子地一步完成「accept + 非阻塞」，省掉一次系统调用。

`set_nonblocking` 把四态拆成「本体」与「新连接」两路：

[src/os/unix/uds_local_socket/listener.rs:L60-L66](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/uds_local_socket/listener.rs#L60-L66)

```rust
fn set_nonblocking(&self, nonblocking: ListenerNonblockingMode) -> io::Result<()> {
    use ListenerNonblockingMode::*;
    self.listener.set_nonblocking(matches!(nonblocking, Accept | Both))?;
    self.nonblocking_streams.store(matches!(nonblocking, Stream | Both), Release);
    Ok(())
}
```

- 本体的非阻塞（影响 `accept` 是否立即返回 `WouldBlock`）委托给标准库 `UnixListener::set_nonblocking`。
- 新建连接的非阻塞只存进原子布尔，留待 `accept` 时应用——这正是 u3-l1 提到的「`Stream` 位与 `Accept` 位分别管理」的落地。

名称回收的 RAII 守卫：drop 时 `unlink` socket 文件路径。

[src/os/unix/uds_local_socket.rs:L68-L74](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/uds_local_socket.rs#L68-L74)

```rust
impl Drop for ReclaimGuard {
    fn drop(&mut self) {
        if let Some(s) = self.as_c_str() {
            let _ = c_wrappers::unlink(s);
        }
    }
}
```

`do_not_reclaim_name_on_drop` 就是调用 `reclaim.forget()` 把守卫「 disarm」（清空内部 buffer），drop 时便不再 unlink：

[src/os/unix/uds_local_socket/listener.rs:L67-L67](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/uds_local_socket/listener.rs#L67)

最后，`Listener` 同时实现了 `Iterator`（`next` 就是 `accept`），这样它自身就能直接用在 `for` 循环里——和公共层 `Incoming` 迭代器一致：

[src/os/unix/uds_local_socket/listener.rs:L69-L74](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/uds_local_socket/listener.rs#L69-L74)

#### 4.2.4 代码实践

**实践目标**：把 `traits::Listener::accept` 与 `set_nonblocking` 两个方法在后端的落地逐一对应到系统调用。

**操作步骤**：

1. 打开 `src/os/unix/uds_local_socket/listener.rs`，找到 `accept`（L51-L59）与 `set_nonblocking`（L60-L66）。
2. 对 `accept`，确认它只调用了两处：`self.listener.accept()`（标准库，底层是 `accept4`/`accept` 系统调用）和 `c_wrappers::fast_set_nonblocking`。
3. 跳到 `src/os/unix/c_wrappers.rs` 的 `set_nonblocking`（L67-L79）与 `fast_set_nonblocking`（L83-L92），看清：
   - Linux/Android 用 `ioctl(FIONBIO)`；
   - 其他 Unix 用 `fcntl(F_SETFL, O_NONBLOCK)`。
4. 对 `set_nonblocking`，确认 `Accept|Both` → `UnixListener::set_nonblocking`（影响 accept 本身），`Stream|Both` → 写原子布尔（影响后续 accept 出来的连接）。

**需要观察的现象 / 预期结果**：你能画出一张表，把四个 `ListenerNonblockingMode` 各自触发的「本体 fcntl」与「新连接 fcntl」对应清楚。

**待本地验证**：在一个 Linux 环境写一个最小监听器，先 `set_nonblocking(Accept)`，在无客户端时调用 `accept`，预期立即收到 `io::ErrorKind::WouldBlock`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `accept` 要在拿到连接后**再**单独调一次 `fast_set_nonblocking`，而不是在 `accept` 时就一步到位？

> **参考答案**：标准库的 `UnixListener::accept` 不接受「请顺便把新 fd 设为非阻塞」的参数（虽然内核的 `accept4(SOCK_NONBLOCK)` 支持）。当前实现走「先 accept、再 set_nonblocking」两步，存在一个极短的阻塞窗口。源码里的 TODO 正是要用自写的 `accept4` 消除这个窗口。

**练习 2**：`reclaim` 用 `AtomicBool` 存储「是否回收」，还是用 `ReclaimGuard` 直接持有路径？两种方案的区别是什么？

> **参考答案**：源码选后者——`ReclaimGuard` 直接持有要 unlink 的路径字节，drop 时凭路径 unlink。好处是无需在 drop 时再去 `Listener` 别处查「当初绑定的地址是什么」，信息自包含；`forget()` 清空 buffer 即可 disarm，零额外状态。

---

### 4.3 Stream 后端：UnixStream 封装、连接与半流

#### 4.3.1 概念说明

后端 `Stream` 是最朴素的 newtype：`pub struct Stream(pub(super) UnixStream)`。它把标准库 `UnixStream` 包了一层，实现 `traits::Stream`（连接、读写、拆分）与 `StreamCommon`（取错误、对端凭据）。

后端 `Stream` 的几个特点：

- 读写通过**引用**实现 `Read`/`Write`（`impl Read for &Stream`），这呼应了 u3-l3 的 `RefRead`/`RefWrite`——正是为了支持「按引用读写」才把 `Read`/`Write` 建在 `&Stream` 上。
- `split` / `reunite` 用 `Arc<Stream>` 实现（见 u3-l3）：`RecvHalf(Arc<Stream>)` 与 `SendHalf(Arc<Stream>)`。
- 连接阶段对 `ConnectWaitMode` 三态（`Deferred` / `Timeout` / `Unbounded`，见 u3-l2）有专门处理。

#### 4.3.2 核心流程

**连接（`from_options`）**：

```
from_options(opts)
  ├─ nonblocking_connect = (wait_mode == Timeout | Deferred)
  ├─ dispatch_name(opts, ..., |addr,_| create_client(addr, nonblocking_connect))
  │      └─ 返回 (OwnedFd, inprog)   // inprog 表示 connect 返回了 EINPROGRESS
  ├─ 若 wait_mode == Timeout(t) 且 inprog：wait_for_connect(fd, Some(t))  // poll POLLOUT
  └─ 若最终非阻塞状态 != nonblocking_connect：fast_set_nonblocking 校正
```

**读写**：`&Stream` 的 `read`/`write` 直接转发给 `&self.0`（即 `&UnixStream`）。

**拆分 / 重聚**：

```
split(self):  arc = Arc::new(self); (RecvHalf(clone), SendHalf(arc))
reunite(rh, sh): 若 !Arc::ptr_eq → Err(ReuniteError{rh,sh})
                  否则 drop(rh); Arc::into_inner(sh.0) 取回 Stream
```

#### 4.3.3 源码精读

连接入口 `from_options`，注意它如何把 `ConnectWaitMode` 折叠成「是否非阻塞连接」、以及如何用 `wait_for_connect` 兜底超时：

[src/os/unix/uds_local_socket/stream.rs:L31-L52](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/uds_local_socket/stream.rs#L31-L52)

```rust
fn from_options(mut opts: &ConnectOptions<'_>) -> io::Result<Self> {
    let nonblocking_connect = matches!(
        opts.get_wait_mode(),
        ConnectWaitMode::Timeout(..) | ConnectWaitMode::Deferred
    );
    let (stream, inprog) = dispatch_name(
        &mut opts, false,
        |&mut opts| opts.name.borrow(),
        |_| None,
        |addr, _| c_wrappers::create_client(addr, nonblocking_connect),
    )?;
    if let ConnectWaitMode::Timeout(timeout) = opts.get_wait_mode() {
        if inprog {
            c_wrappers::wait_for_connect(stream.as_fd(), Some(timeout), CONN_TIMEOUT_MSG)?;
        }
    }
    if opts.get_nonblocking_stream() != nonblocking_connect {
        c_wrappers::fast_set_nonblocking(stream.as_fd(), opts.get_nonblocking_stream())?;
    }
    Ok(stream.into())
}
```

要点：

- `Timeout` 与 `Deferred` 都要求「非阻塞地发起 connect」（这样才不会卡死），区别仅在后续：`Timeout` 用 `poll` 等到就绪或超时；`Deferred` 则把「尚未就绪」的连接直接交还给用户（不再等待）。这与 u3-l2 描述的三态语义一致。
- `inprog` 来自 `create_client` 对 `EINPROGRESS`/`EAGAIN` 的识别。
- 最后一步 `fast_set_nonblocking` 是「校正」：如果用户期望的最终非阻塞状态与「为连接而临时设置的非阻塞」不一致，就调整一次。

`create_client` 的底层——发起非阻塞连接并识别「进行中」：

[src/os/unix/c_wrappers.rs:L263-L279](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/c_wrappers.rs#L263-L279)

```rust
pub(super) fn create_client(
    dst: TerminatedUdAddr<'_>,
    nonblocking: bool,
) -> io::Result<(OwnedFd, bool)> {
    let sock = create_socket(libc::SOCK_STREAM, nonblocking)?;
    if !CAN_CREATE_NONBLOCKING && nonblocking {
        set_nonblocking(sock.as_fd(), true)?;
    }
    let inprog = match connect(sock.as_fd(), dst) {
        Ok(()) => false,
        Err(e) if matches!(e.raw_os_error(), Some(libc::EINPROGRESS) | Some(libc::EAGAIN)) => true,
        Err(e) => return Err(e),
    };
    Ok((sock, inprog))
}
```

`Timeout` 模式下用 `poll` 等待连接就绪，并在出错时通过 `SO_ERROR` 取真实错误：

[src/os/unix/c_wrappers.rs:L286-L303](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/c_wrappers.rs#L286-L303)

读写转发——`&Stream` 实现 `Read`/`Write`，内层就是 `&self.0`（`&UnixStream`）：

[src/os/unix/uds_local_socket/stream.rs:L93-L112](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/uds_local_socket/stream.rs#L93-L112)

拆分与重聚（见 u3-l3 详述），此处给出后端落地以印证「用 `Arc` + `ptr_eq`、全程无 `unsafe`」：

[src/os/unix/uds_local_socket/stream.rs:L68-L82](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/uds_local_socket/stream.rs#L68-L82)

```rust
fn split(self) -> (RecvHalf, SendHalf) {
    let arc = Arc::new(self);
    (RecvHalf(Arc::clone(&arc)), SendHalf(arc))
}
fn reunite(rh: RecvHalf, sh: SendHalf) -> ReuniteResult<Self> {
    if !Arc::ptr_eq(&rh.0, &sh.0) {
        return Err(ReuniteError { rh, sh });
    }
    drop(rh);
    let inner = Arc::into_inner(sh.0).expect("stream half inexplicably copied");
    Ok(inner)
}
```

`StreamCommon` 的两个方法：`take_error` 读 `SO_ERROR`、`peer_creds` 取对端凭据（u3-l4 详讲）。

[src/os/unix/uds_local_socket/stream.rs:L84-L91](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/uds_local_socket/stream.rs#L84-L91)

```rust
impl traits::StreamCommon for Stream {
    fn take_error(&self) -> io::Result<Option<io::Error>> { c_wrappers::take_error(self.as_fd()) }
    fn peer_creds(&self) -> io::Result<PeerCreds> {
        PeerCredsInner::for_socket(self.as_fd()).map(From::from)
    }
}
```

`take_error` 的底层就是一次 `getsockopt(SO_ERROR)`：

[src/os/unix/c_wrappers.rs:L281-L284](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/c_wrappers.rs#L281-L284)

半流类型只是 `Arc<Stream>` 的 newtype，靠 `multimacro!` 批量转发读写 trait：

[src/os/unix/uds_local_socket/stream.rs:L167-L207](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/uds_local_socket/stream.rs#L167-L207)

#### 4.3.4 代码实践

**实践目标**：追踪一次 `Timeout` 模式的连接，理解非阻塞 connect + poll 的两段式。

**操作步骤**：

1. 从 `stream.rs` 的 `from_options`（L31）出发，确认 `ConnectWaitMode::Timeout(t)` 会让 `nonblocking_connect = true`。
2. 跟到 `create_client`（c_wrappers.rs L263），确认非阻塞 `connect` 在未完成时返回 `EINPROGRESS`，于是 `inprog = true`。
3. 回到 `from_options`，确认 `inprog` 为真时调用 `wait_for_connect(fd, Some(timeout))`。
4. 读 `wait_for_connect`（c_wrappers.rs L286-L303）：它用 `poll` 等 `POLLOUT`，就绪后用 `take_error` 取连接是否真的成功。

**需要观察的现象 / 预期结果**：你能讲清「为什么非阻塞 connect 之后还必须再 `getsockopt(SO_ERROR)` 一次」——因为非阻塞 connect 的「可写」只表示连接结束（成功或失败），真正失败的原因藏在 `SO_ERROR` 里。

#### 4.3.5 小练习与答案

**练习 1**：`Deferred` 模式下，`from_options` 会调用 `wait_for_connect` 吗？连接尚未就绪时会怎样？

> **参考答案**：不会。`Deferred` 只把 `nonblocking_connect` 置真（让 connect 不阻塞），但 `if let Timeout(...)` 分支不匹配，所以不等待。连接尚未就绪时，`Stream` 仍被构造并返回，其 fd 处于「连接进行中」状态；用户后续读写会收到 `WouldBlock`，需自行 `poll` 或 `take_error`。

**练习 2**：`reunite` 里 `Arc::into_inner(sh.0).expect(...)` 为什么是安全的（不会 panic）？

> **参考答案**：前面已经用 `Arc::ptr_eq` 确认两半来自同一个 `Arc`；随后 `drop(rh)` 释放了 `RecvHalf` 持有的那一个强引用，此时该 `Arc` 的强引用计数应为 1（只剩 `sh.0`），`into_inner` 必然成功取回内部 `Stream`。`expect` 的文案「inexplicably copied」正说明这是不该发生的不变式违反。

---

### 4.4 地址解释：dispatch_name 从 Name 到 sockaddr_un

#### 4.4.1 概念说明

u2-l4 讲过 `Name<'s>` 内部用 `NameInner` 枚举存了四个平台变体。在 Unix 上，真正要把名字塞进内核 `sockaddr_un` 时，需要根据变体类型走不同路线。这一步由后端顶层的 `dispatch_name` 函数完成，它把 `NameInner` 翻译成具体的 `sockaddr_un` 字节布局。

Unix 上的三种地址形态（对应 name_type.rs 的标记类型）：

| `NameInner` 变体 | 形态 | name_type 标记类型 | 内核地址 |
|---|---|---|---|
| `UdSocketPath` | 文件系统路径 | `FilesystemUdSocket` | `sun_path = "/path/to/sock"` |
| `UdSocketNs` | Linux 抽象命名空间 | `AbstractNsUdSocket`（仅 Linux/Android） | `sun_path[0]=0`，其后为名字 |
| `UdSocketPseudoNs` | 伪命名空间兜底 | `SpecialDirUdSocket`（非 Linux） | `/run/user/<uid>/名字`，失败回退 `/tmp/` |

#### 4.4.2 核心流程

`dispatch_name` 是一个高阶函数：它接收一个「如何取名字」「如何取 spin 时间」「如何创建」的闭包三元组，然后按 `NameInner` 变体分派：

```
dispatch_name(o, create_dirs, get_name, max_spin_time, create):
  addr = UdAddr::new()           // 清零的 sockaddr_un 缓冲
  match get_name(o).0:
    UdSocketPath(path)   → addr.init(path); create(addr.write_terminator())
    UdSocketNs(name)     → addr.init_namespaced(name); create(...)        // Linux only
    UdSocketPseudoNs(n)  → write_run_user(addr, n)                        // /run/user/<uid>/
                         → with_missing_dir_creat(...create...)           // 顺带建目录
                         → 若 benign 失败：write_prefixed(addr, tmpdir(), n) 再试   // 回退 /tmp/
```

- `write_terminator()` 返回一个 `TerminatedUdAddr` 见证类型，**在类型系统里**保证「NUL 终止已写入」，之后才能把地址指针交给 `bind`/`connect`。这是把「运行期不变式」编码进类型的典型手法。
- 伪命名空间会先试 `/run/user/<uid>/`，目录不存在时还会 `create_dir_all`，再不行回退到 `/tmp/`。

#### 4.4.3 源码精读

`dispatch_name` 的主体——按变体分派：

[src/os/unix/uds_local_socket.rs:L138-L184](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/uds_local_socket.rs#L138-L184)

注意 `UdSocketNs`（抽象命名空间）整段被 `#[cfg(any(target_os = "linux", target_os = "android"))]` 门控——只有 Linux 内核支持抽象命名空间，其他 Unix 连这一变体都不编译。

`UdAddr` 的容量 `SUN_LEN` 与结构布局：

[src/os/unix/ud_addr.rs:L24-L27](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/ud_addr.rs#L24-L27)

[src/os/unix/ud_addr.rs:L42-L50](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/ud_addr.rs#L42-L50)

```rust
pub(super) const SUN_LEN: usize = { /* sun.sun_path.len() */ };

#[derive(Copy, Clone)]
#[repr(C)]
pub(super) struct UdAddr {
    len: socklen_t,
    sun: MaybeUninit<sockaddr_un>,
    terminator: MaybeUninit<c_char>, // 紧跟在 sun_path 之后，处理「路径恰好填满」的边界
}
```

`init`（普通路径）与 `init_namespaced`（抽象命名空间，首字节写 0）：

[src/os/unix/ud_addr.rs:L177-L192](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/ud_addr.rs#L177-L192)

```rust
pub fn init(&mut self, path: &[NonZeroU8]) -> io::Result<()> {
    Self::check_path_length(path.len())?;
    unsafe { self.push_slice(path) };
    Ok(())
}
pub fn init_namespaced(&mut self, nsname: &[NonZeroU8]) -> io::Result<()> {
    Self::check_path_length(nsname.len() + 1)?;
    unsafe { self.path_ptr_mut().write(0) };  // 抽象命名空间：首字节为 0
    self.len = 1;
    unsafe { self.push_slice(nsname) };
    Ok(())
}
```

`write_terminator` 写入 NUL 并返回见证类型：

[src/os/unix/ud_addr.rs:L159-L167](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/ud_addr.rs#L159-L167)

伪命名空间的路径拼装——`/run/user/<uid>/名字`，并把 UID 数字逐位写入（不依赖 UID 大小假设）：

[src/os/unix/uds_local_socket.rs:L244-L281](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/uds_local_socket.rs#L244-L281)

回退到临时目录：

[src/os/unix/uds_local_socket.rs:L286-L297](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/uds_local_socket.rs#L286-L297)

最后，标记类型如何产生这些 `NameInner` 变体（这就是 `dispatch_name` 的上游）：

[src/os/unix/local_socket/name_type.rs:L18-L45](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/local_socket/name_type.rs#L18-L45)

```rust
tag_enum!(FilesystemUdSocket);
impl PathNameType<OsStr> for FilesystemUdSocket {
    fn map(path: Cow<'_, OsStr>) -> io::Result<Name<'_>> {
        // 禁止 interior nul
        Ok(Name(NameInner::UdSocketPath(path)))
    }
}
```

`AbstractNsUdSocket` 产生 `UdSocketNs`、`SpecialDirUdSocket` 产生 `UdSocketPseudoNs`，逻辑同理（name_type.rs L82-L109 / L47-L80）。`GenericNamespaced` 的泛型映射在 Linux 上转发给 `AbstractNsUdSocket`、在其他 Unix 上转发给（已 deprecated 的）`SpecialDirUdSocket`（见 name_type.rs 的 `map_generic!` 宏 L111-L140）。

#### 4.4.4 代码实践

**实践目标**：验证「同一个 `GenericNamespaced` 名字，在 Linux 与其他 Unix 上落地为不同的 `sockaddr_un`」。

**操作步骤**：

1. 打开 `src/os/unix/local_socket/name_type.rs` 的 `map_generic!`（L135-L140），看清 `namespaced` 分支：Linux/Android 调 `AbstractNsUdSocket::map`，其余调 `SpecialDirUdSocket::map`。
2. 打开 `src/os/unix/uds_local_socket.rs` 的 `dispatch_name`（L138），对照：`UdSocketNs` 走 `init_namespaced`（首字节 0），`UdSocketPseudoNs` 走 `write_run_user`（拼成 `/run/user/<uid>/...`）。
3. 用 `grep` 确认 `UdSocketNs` 分支被 `#[cfg(linux/android)]` 包裹，`UdSocketPseudoNs` 没有。

**需要观察的现象 / 预期结果**：你能解释为什么在 macOS/BSD 上用 `GenericNamespaced` 名字会在文件系统里出现一个 `/run/user/<uid>/xxx` 的 socket 文件，而在 Linux 上则不产生任何文件（抽象命名空间）。

**待本地验证**：在 Linux 上用 `GenericNamespaced` 名字跑一个 server，用 `ss -x -a` 或 `lsof -U` 观察是否出现 `@名字` 形式的抽象命名空间条目（`@` 前缀是 `ss` 对抽象命名空间的显示约定）。

#### 4.4.5 小练习与答案

**练习 1**：`write_terminator` 为什么要返回一个 `TerminatedUdAddr` 见证类型，而不是直接返回 `&UdAddr`？

> **参考答案**：把「NUL 终止已写入」这一运行期事实编码进类型系统。`bind`/`connect` 只接受 `TerminatedUdAddr`，从而在编译期保证：传给内核的指针指向的必定是 NUL 结尾的合法地址，无法忘记写终止符。这是「状态机编码」的典型应用。

**练习 2**：`escape_nuls`（把名字里的 NUL 换成下划线）的存在意义是什么？注释说「仅为了保留 2.2 及更早的行为」。

> **参考答案**：早期版本允许名字里含 NUL（通过转义保留），但这与「文件系统路径禁止 interior nul」的检查并存会造成行为不一致。`escape_nuls` 是向后兼容的兜底：对伪命名空间名字里的 NUL 不报错而是替换，避免破坏旧程序。新代码应直接用不含 NUL 的名字。

---

## 5. 综合实践

**任务**：从公共 API 出发，端到端追踪一条「服务端 bind+listen+accept」与「客户端 connect」的完整调用链，并画出一张包含下列节点的流程图。

要求在图上标注：

1. 公共 `ListenerOptions::create_sync()` 在哪两次「实例化」`create_sync_as`；
2. `dispatch_sync::listen` / `dispatch_sync::connect` 各自的位置；
3. 后端 `Listener::from_options` 调用的 `c_wrappers::create_listener`（socket→fchmod→bind→listen）四步；
4. 后端 `Stream::from_options` 调用的 `dispatch_name` → `create_client`（socket→connect）两步，以及 `Timeout` 模式下的 `wait_for_connect`；
5. `ReclaimGuard` 在何时被构造、何时（drop）执行 `unlink`。

**进阶（可选，待本地验证）**：在一个 Linux 环境里，分别用 `GenericFilePath`（文件路径）和 `GenericNamespaced`（抽象命名空间）两种名字跑同一个回显 server，用 `strace -e trace=socket,bind,listen,connect,unlink` 观察两次的系统调用差异——预期前者能看到 `bind("/path")` 与退出时的 `unlink`，后者看到 `bind` 的地址首字节为 `\0` 且无 `unlink`。

## 6. 本讲小结

- `dispatch_sync.rs` 是公共枚举与 Unix 后端之间唯一的桥梁：`listen` / `connect` 各一行，靠 `create_sync_as::<后端>()` 触发后端、再 `.map(Listener::from)` 包回公共枚举。
- `create_sync_as` / `connect_sync_as` 在一条派发链上被**调用两次**：第一次 `S = 公共枚举`（跳进派发函数），第二次 `S = 后端类型`（真正落地）——这是理解整个创建流程的钥匙。
- 后端 `Listener` 是 `UnixListener` + `ReclaimGuard` + `AtomicBool` 的三字段包装；`accept` 用标准库 accept 再按需设非阻塞，`set_nonblocking` 把四态拆成「本体」与「新连接」两路。
- 后端 `Stream` 是 `UnixStream` 的 newtype；连接阶段把 `ConnectWaitMode` 折叠为非阻塞 connect，`Timeout` 用 `poll`+`SO_ERROR` 兜底；`split`/`reunite` 用 `Arc`+`ptr_eq`，无 `unsafe`。
- `dispatch_name` 把 `NameInner` 的三种 Unix 变体翻译成 `sockaddr_un`：文件路径、Linux 抽象命名空间（首字节 0）、伪命名空间（`/run/user/<uid>/` 回退 `/tmp/`）；`TerminatedUdAddr` 见证类型在编译期守住 NUL 终止不变式。

## 7. 下一步学习建议

- **u4-l2 Windows named pipe local socket 后端**：对照本讲，看 Windows 后端如何用完全不同的原语（named pipe 实例 + `ConnectNamedPipe`）实现同一份 `traits::Listener` / `traits::Stream` 契约，体会「同一接口、两套实现」的抽象威力。
- **u6-l1 / u6-l2 Tokio 异步集成**：本讲的 `dispatch_tokio.rs` 与 `uds_local_socket/tokio/` 是同步版本的异步镜像，底层换成 `AsyncFd`。学完异步后再回看本讲，会发现「壳/芯」分层在异步下完全复用。
- **u9-l1 unsafe FFI 封装层**：若想深入 `c_wrappers.rs` 里每一个 `unsafe { libc::... }` 的前置条件与错误转换（`OrErrno` / `fd_or_errno`），那是专家层的内容。
- 继续阅读：`src/os/unix/uds_local_socket/tokio/listener.rs` 与 `stream.rs`，以及 `src/os/unix/local_socket/peer_creds.rs`（对端凭据的后端实现，承接 u3-l4）。
