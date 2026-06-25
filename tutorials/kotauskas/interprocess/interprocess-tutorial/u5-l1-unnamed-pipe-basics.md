# 匿名管道基础：pipe() 与 Sender/Recver

## 1. 本讲目标

本讲聚焦 interprocess 中**最贴近操作系统原语**的一个模块——`unnamed_pipe`（匿名管道）。学完本讲，你应当能够：

- 说出**匿名管道**（unnamed pipe）区别于 local socket / FIFO 文件 / Windows named pipe 的本质特征。
- 用 `pipe()` 一行代码创建一对 `(Sender, Recver)`，并通过标准库的 `Read` / `Write` trait 收发字节。
- 看懂「公共 `Sender` / `Recver` 是壳、平台后端是芯」的分层：公共类型只是 newtype，真正的系统调用下沉到 Unix 的 `FdOps` 或 Windows 的 `CreatePipe`。
- 说清楚 `multimacro!` 如何为公共类型批量「缝」上 `Read` / `Write` / `Debug` / 句柄转换等一整套 trait，从而免去手写样板。
- 理解句柄/FD **默认可继承**这一关键设计：它正是匿名管道能用于与**子进程**通信的根基。

本讲承接 u2-l3（`impmod!` 与平台后端注入），那里建立的「壳/芯」视角将在这里被原样复用。

## 2. 前置知识

在进入源码前，先用大白话把几个概念讲清楚。

**进程（process）** 是操作系统中一个正在运行的程序实例，每个进程有独立的内存空间。两个进程的内存互不可见，于是需要**进程间通信（IPC）** 来交换数据。

**管道（pipe）** 是 IPC 里最朴素的形态：它是一段**内核缓冲区**配上**两个端点**。一端只写、一端只读，数据像流水一样从写端流向读端。你可以把它想象成一根水管：

- 写端（water inlet）≈ 本讲的 `Sender`
- 读端（water outlet）≈ 本讲的 `Recver`
- 水管本身 = 内核里那段缓冲区，你摸不到它的「地址」，只能握着两端的把手

**匿名 vs 命名**：

- **匿名管道（unnamed / anonymous pipe）**：没有名字、没有文件系统路径，唯一的存在方式就是那两个端点句柄。**端点一旦全部关闭，管道就彻底消失**。它没有「重新连接」的概念。
- **命名管道**：在文件系统里有个名字（Unix 的 FIFO 文件、Windows 的 named pipe），任意进程都能凭名字打开它。

> 术语提醒：Windows 的 "named pipe" 和 Unix 的 "named pipe(FIFO)" 是**完全不同的两样东西**。interprocess 为避免混淆，把 Unix 版特称为 **FIFO 文件**（见 u4-l4），把 Windows 版称为 **named pipe**（见 u4-l3）。本讲只讲**匿名管道**，它两端都跨平台可用。

**句柄 / 文件描述符（handle / FD）**：操作系统用一个整数（Unix 叫 file descriptor / FD，Windows 叫 handle）来代表一个打开的「I/O 对象」。`Sender` / `Recver` 本质上就是对这个整数的**有所有权**的包装。

**句柄继承（handle inheritance）**：当父进程创建子进程时，可以让子进程**继承**父进程的一部分句柄。被继承的句柄在子进程里指向**同一个**内核对象（同一段管道缓冲区），数值还相同。这就是「同一根管道被两个进程同时握着」的实现原理——本讲要反复回到这一点。

**Rust newtype 模式**：`pub struct Sender(pub(crate) Inner);` 这样的结构体只有一个字段，作用是给内部类型换一个有意义的名字、控制其构造入口。interprocess 的公共 `Sender` / `Recver` 都是 newtype。

**`impmod!` / `multimacro!`**：这两个 crate 私有宏已在 u2-l3 讲透——`impmod!` 把平台后端类型按 `cfg(unix)` / `cfg(windows)` 以统一别名注入公共模块；`multimacro!` 把一串转发宏批量套到某个类型上。本讲只看它们在 `unnamed_pipe` 上的具体落点。

## 3. 本讲源码地图

| 文件 | 角色 |
|------|------|
| `src/lib.rs` | crate 根，声明 `pub mod unnamed_pipe;`（[src/lib.rs:22](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/lib.rs#L22)） |
| `src/unnamed_pipe.rs` | **公共层（壳）**：模块文档、`pipe()` 函数、公共 `Sender` / `Recver` 定义与 `multimacro!` |
| `src/unnamed_pipe/tokio.rs` | 公共层的 **Tokio 异步版**，结构与同步版几乎一致（本讲略讲，u6 详讲） |
| `src/os/unix/unnamed_pipe.rs` | **Unix 后端（芯）**：`libc::pipe2` / `libc::pipe` 调用、后端 `Sender` / `Recver`、`UnnamedPipeExt` 扩展 |
| `src/os/unix/fdops.rs` | Unix 上**真正发起系统调用**的 `FdOps`（`libc::read` / `write` / `readv` / `writev`） |
| `src/os/windows/unnamed_pipe.rs` | **Windows 后端（芯）**：`CreatePipe` 调用、`CreationOptions` 构建器、带 limbo 逻辑的后端 `Sender` |
| `src/macros.rs`、`src/macros/*.rs` | `impmod!` / `multimacro!` 及各转发宏的定义 |
| `examples/unnamed_pipe/sync/*.rs` | 同步示例：跨「线程」传递句柄、一端写一端读 |

阅读建议：先把公共层 `src/unnamed_pipe.rs`（壳）通读一遍，再去 Unix 后端（最直观）看系统调用，最后扫一眼 Windows 后端的差异。

## 4. 核心概念与源码讲解

### 4.1 unnamed_pipe 模块：跨平台管道抽象

#### 4.1.1 概念说明

`unnamed_pipe` 是 interprocess 提供的**匿名管道**跨平台封装。模块开头的文档用三句话点明了它的定位：

- 匿名管道**只能通过句柄访问**——一旦某个端点关闭，它那一头就再也够不着了。
- 它**最适合用来和子进程通信**（因为句柄可被继承给子进程）。
- 默认创建出来的句柄是**可继承**的。

这套定位决定了它的 API 极简：只有一个创建函数 `pipe()`，外加两个句柄类型 `Sender`（写）和 `Recver`（读）。没有监听器、没有名字、没有连接握手——这些复杂能力属于 local socket，不属于匿名管道。

> **何时该用匿名管道，何时该用别的？** 模块文档特意提醒：如果只是想让子进程通过 `stdin` / `stdout` / `stderr` 与父进程通信，标准库的 [`std::process::Stdio`](https://doc.rust-lang.org/std/process/struct.Stdio.html) 通常更简单，建议优先考虑。匿名管道的价值在于「需要第 3、4 条独立的数据通道」或「想把句柄跨进程显式传递」这类场景（见 u5-l2）。

#### 4.1.2 核心流程

`unnamed_pipe` 模块的结构是 u2-l3 讲过的「壳/芯」分层的一个干净实例：

```text
公共层（src/unnamed_pipe.rs）        平台后端（src/os/{unix,windows}/unnamed_pipe.rs）
┌──────────────────────────┐        ┌────────────────────────────────────┐
│ pub fn pipe()            │        │ pub fn pipe_impl() -> Result       │
│   └─ pipe_impl()  ──────────────────▶  Unix: libc::pipe2 / libc::pipe  │
│                          │        │  Windows: Win32 CreatePipe         │
│ pub struct Sender(壳)    │        │ struct Sender(芯: FdOps/Handle)    │
│ pub struct Recver(壳)    │        │ struct Recver(芯: FdOps/Handle)    │
│   multimacro! 缝 trait   │        │   真正的系统调用                    │
└──────────────────────────┘        └────────────────────────────────────┘
          ▲                                     ▲
          └──── impmod! 把「芯」以别名注入「壳」 ────┘
```

关键点（全部来自 u2-l3 的结论，这里只是落到具体模块）：

1. 公共 `pipe()` **不直接**发起系统调用，它只是转发给 `pipe_impl()`，而 `pipe_impl` 是 `impmod!` 注入的后端函数。
2. 公共 `Sender` / `Recver` 是字段 `pub(crate)` 的 newtype 壳；它们的 `Read` / `Write` / `Debug` / 句柄转换 trait 全部由 `multimacro!` 转发给内部的「芯」。
3. 因为 `cfg(unix)` 与 `cfg(windows)` 互斥，**同一次编译里只有一个后端存在**，派发零开销。
4. 后端函数**直接返回公共类型**（`PubSender` / `PubRecver`），所以「芯造好 → 包成壳 → 返回」是闭环的。

#### 4.1.3 源码精读

先看模块文档与顶层结构（[src/unnamed_pipe.rs:1-31](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/unnamed_pipe.rs#L1-L31)）：

```rust
//! Unlike named pipes, unnamed pipes are only accessible through their handles – once an endpoint
//! is closed, its corresponding end of the pipe is no longer accessible. Unnamed pipes typically
//! work best when communicating with child processes.
//!
//! The handles and file descriptors are inheritable by default. The `AsRawHandle` and `AsRawFd`
//! traits can be used to get a numeric handle value which can then be communicated to a child
//! process ...
```

这段文档（[src/unnamed_pipe.rs:3-11](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/unnamed_pipe.rs#L3-L11)）浓缩了本讲的三大要点：句柄-only 访问、适合子进程通信、默认可继承。

紧接着是 Tokio 异步子模块的 feature 门控（[src/unnamed_pipe.rs:22-24](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/unnamed_pipe.rs#L22-L24)）和**全讲最关键的一行宏**——后端注入：

```rust
impmod! {unnamed_pipe,
    Recver as RecverImpl,
    Sender as SenderImpl,
    pipe_impl,
}
```

[src/unnamed_pipe.rs:26-30](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/unnamed_pipe.rs#L26-L30)

对照 u2-l3，这段 `impmod!` 会展开成两条互斥的 `use`：

- 在 Unix 编译时：`use crate::os::unix::unnamed_pipe::{Recver as RecverImpl, Sender as SenderImpl, pipe_impl};`
- 在 Windows 编译时：`use crate::os::windows::unnamed_pipe::{Recver as RecverImpl, Sender as SenderImpl, pipe_impl};`

于是公共层拿到了三个统一别名，后续代码完全平台无关。`pipe_impl` 是后端的创建函数；`RecverImpl` / `SenderImpl` 是后端的「芯」类型。

`impmod!` 本体见 [src/macros.rs:4-14](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros.rs#L4-L14)。

#### 4.1.4 代码实践（源码阅读型）

**目标**：亲手验证「壳/芯」注入确实存在，而不只是听我一面之词。

**步骤**：

1. 打开 [src/unnamed_pipe.rs:26-30](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/unnamed_pipe.rs#L26-L30)，确认 `impmod!` 注入了哪三个名字。
2. 分别打开两个后端文件 [src/os/unix/unnamed_pipe.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/unnamed_pipe.rs) 与 [src/os/windows/unnamed_pipe.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/unnamed_pipe.rs)，在其中各找到名为 `pipe_impl` 的 `pub(crate)` 函数。
3. 确认它们都返回 `io::Result<(PubSender, PubRecver)>`——即**直接返回公共类型**。

**预期观察**：两个后端的 `pipe_impl` 返回类型一致（都是公共壳类型），函数签名也一致，这就是 `impmod!` 能把它们当同一个别名使用的根据。

#### 4.1.5 小练习与答案

**Q1**：模块文档说「unnamed pipes typically work best when communicating with child processes」，为什么？  
**A1**：因为匿名管道没有名字、没有路径，无法被任意进程凭名字打开；唯一的复用方式是靠**句柄继承**把端点传给子进程。所以它的天然适用场景就是父子进程通信。

**Q2**：如果只是想让子进程读父进程的 `stdout`，模块文档建议用什么替代匿名管道？  
**A2**：标准库的 [`std::process::Stdio`](https://doc.rust-lang.org/std/process/struct.Stdio.html)，它能在 `Command` 上直接把子进程的 `stdout` 等接到管道上，省去手动管理句柄。

### 4.2 pipe()：创建管道的统一入口

#### 4.2.1 概念说明

`pipe()` 是匿名管道唯一的创建入口。它的签名极度简洁：

```rust
pub fn pipe() -> io::Result<(Sender, Recver)>
```

注意返回元组的顺序是 **`(Sender, Recver)`**——写端在前、读端在后（[src/unnamed_pipe.rs:50](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/unnamed_pipe.rs#L50)）。返回 `io::Result` 是因为底层系统调用可能失败（例如资源耗尽）。

`pipe()` 本身只做一件事：转发给后端 `pipe_impl()`。文档里那句「The platform-specific builders in the `os` module of the crate might be more helpful if extra configuration for the pipe is needed.」点明了：`pipe()` 是「默认设置」快捷方式，需要更多选项（非阻塞、安全描述符、缓冲区提示）时，要去 `os` 模块找平台专有构建器。

#### 4.2.2 核心流程

```text
用户调用 pipe()
      │
      ▼  公共层 (src/unnamed_pipe.rs)
   pipe_impl()   ← impmod! 注入的别名
      │
      ▼  Unix 后端 (src/os/unix/unnamed_pipe.rs)
   pub(crate) fn pipe_impl() { pipe(false) }
      │
      ▼
   libc::pipe2  (Linux/Android, 可一步带上 O_NONBLOCK)
   libc::pipe   (其它 Unix)
      │  得到两个原始 fd：fds[0]=读, fds[1]=写
      ▼
   OwnedFd::from_raw_fd 包成拥有所有权的 fd
      │
      ▼
   FdOps(OwnedFd) → 后端 Sender / Recver
      │
      ▼
   PubSender / PubRecver (包成公共壳) → 返回给用户
```

Windows 路径与之对称：`pipe_impl()` → `CreationOptions::default().build()` → Win32 `CreatePipe`，差别只在「芯」换成 `AdvOwnedHandle` 而非 `FdOps`，并且 `Sender` 多了一段 limbo 刷新逻辑（见 4.3）。

#### 4.2.3 源码精读

公共 `pipe()` 的全部实现就一行（[src/unnamed_pipe.rs:49-50](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/unnamed_pipe.rs#L49-L50)）：

```rust
#[inline]
pub fn pipe() -> io::Result<(Sender, Recver)> { pipe_impl() }
```

`pipe_impl` 是 4.1 里 `impmod!` 注入的别名。真正干活的是后端。

**Unix 后端**（[src/os/unix/unnamed_pipe.rs:42-89](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/unnamed_pipe.rs#L42-L89)）提供了一个比公共 `pipe()` 更强的平台函数——可以一步创建**非阻塞**管道：

```rust
/// ## System calls
/// - `pipe2` (Linux)
/// - `pipe` (not Linux)
/// - `fcntl` (not Linux, only if `nonblocking` is `true`)
pub fn pipe(nonblocking: bool) -> io::Result<(PubSender, PubRecver)> {
    let (success, fds) = unsafe {
        let mut fds: [c_int; 2] = [0; 2];
        #[cfg(any(target_os = "linux", target_os = "android"))]
        { result = libc::pipe2(fds.as_mut_ptr(), if nonblocking { libc::O_NONBLOCK } else { 0 }); }
        #[cfg(not(any(target_os = "linux", target_os = "android")))]
        { result = libc::pipe(fds.as_mut_ptr()); }
        (result == 0, fds)
    };
    if success {
        let (w, r) = unsafe {
            let w = OwnedFd::from_raw_fd(fds[1]);  // 写端 = fds[1]
            let r = OwnedFd::from_raw_fd(fds[0]);  // 读端 = fds[0]
            (w, r)
        };
        let w = PubSender(Sender(FdOps(w)));
        let r = PubRecver(Recver(FdOps(r)));
        // ...
        Ok((w, r))
    } else {
        Err(io::Error::last_os_error())
    }
}
#[inline]
pub(crate) fn pipe_impl() -> io::Result<(PubSender, PubRecver)> { pipe(false) }
```

要点解读：

- `libc::pipe` / `pipe2` 返回两个 fd：`fds[0]` 恒为**读端**、`fds[1]` 恒为**写端**（这是 POSIX 的约定）。
- Linux 用 `pipe2` 可以在一次系统调用里设置 `O_NONBLOCK`，省掉一次 `fcntl`；其它 Unix 没有 `pipe2`，要非阻塞就得创建后再 `fcntl`（代码里那段 `#[cfg(not(...))]` 的 `set_nonblocking` 兜底就是干这个的）。
- 原始 fd 经 `OwnedFd::from_raw_fd` 变成**拥有所有权**的 Rust 对象（drop 时自动关闭 fd），再层层包成 `FdOps` → 后端 `Sender`/`Recver` → 公共 `PubSender`/`PubRecver`。**后端直接返回公共类型**，印证了 4.1.2 的闭环。

真正发起 `read` / `write` 系统调用的地方在 `FdOps`（[src/os/unix/fdops.rs:11-53](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/fdops.rs#L11-L53)）：

```rust
#[repr(transparent)]
pub(super) struct FdOps(pub(super) OwnedFd);
impl FdOps {
    pub(super) unsafe fn read_ptr(fd, ptr, len) -> io::Result<usize> {
        let bytes_read = unsafe { libc::read(fd.as_raw_fd(), ptr.cast(), len) };
        (bytes_read >= 0).true_val_or_errno(i2u(bytes_read))
    }
    pub(super) fn write(fd, buf: &[u8]) -> io::Result<usize> {
        let bytes_written = unsafe { libc::write(fd.as_raw_fd(), buf.as_ptr().cast(), length_to_write) };
        (bytes_written >= 0).true_val_or_errno(i2u(bytes_written))
    }
}
```

`FdOps` 就是「`OwnedFd` + 一组把 libc 调用结果翻译成 `io::Result` 的方法」。`true_val_or_errno` 是 u4-l4 提过的错误翻译工具：返回值非负视为成功，否则取 `errno`。

**Windows 后端**（[src/os/windows/unnamed_pipe.rs:100-102](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/unnamed_pipe.rs#L100-L102)）的 `pipe_impl` 走构建器：

```rust
pub(crate) fn pipe_impl() -> io::Result<(PubSender, PubRecver)> {
    CreationOptions::default().build()
}
```

而 `build()` / `create()`（[src/os/windows/unnamed_pipe.rs:64-90](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/unnamed_pipe.rs#L64-L90)）调用 Win32 的 `CreatePipe`，把句柄包成 `OwnedHandle`，再装进后端 `Sender` / `Recver`：

```rust
let success = unsafe { CreatePipe(&mut r, &mut w, ref2ptr(&sd).cast_mut().cast(), hint_raw) } != 0;
if success {
    let (w, r) = unsafe {
        let w = OwnedHandle::from_raw_handle(w);
        let r = OwnedHandle::from_raw_handle(r);
        (w, r)
    };
    let w = PubSender(Sender { io: ManuallyDrop::new(w.into()), needs_flush: false });
    let r = PubRecver(Recver(r.into()));
    Ok((w, r))
}
```

`CreatePipe` 的第三个参数是 `SECURITY_ATTRIBUTES`，由 `create_security_attributes(...)` 根据 `inheritable` 与 `security_descriptor` 构造（[src/os/windows/unnamed_pipe.rs:72](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/unnamed_pipe.rs#L72)）——这正是「默认可继承」在 Windows 上的落点（`inheritable` 默认 `true`，见 4.3.3）。

#### 4.2.4 代码实践（调用链追踪型）

**目标**：把 4.2.2 的流程图与真实代码一一对应。

**步骤**：

1. 从 [src/unnamed_pipe.rs:50](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/unnamed_pipe.rs#L50) 的 `pipe()` 出发，确认它调用 `pipe_impl`。
2. 在 [src/os/unix/unnamed_pipe.rs:88-89](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/unnamed_pipe.rs#L88-L89) 找到 `pipe_impl`，确认它调用 `pipe(false)`。
3. 在 `pipe(false)` 内部追踪：`libc::pipe` → `from_raw_fd` → `FdOps` → 后端 `Sender`/`Recver` → `PubSender`/`PubRecver`。

**预期观察**：你会看到原始的 `c_int` fd 是如何被层层「穿衣服」最终变成用户拿到的 `Sender` / `Recver`，且没有任何一层在「壳」里发起系统调用——系统调用全部在「芯」里。

#### 4.2.5 小练习与答案

**Q1**：为什么 Unix 后端要区分 `pipe2`（Linux/Android）和 `pipe`（其它）？  
**A1**：`pipe2` 能在一次调用里原子地设置 `O_NONBLOCK` 等标志，避免「创建后再 `fcntl`」之间的窗口；非 Linux 平台没有 `pipe2`，只能用 `pipe` + 可选的 `fcntl` 兜底。

**Q2**：公共 `pipe()` 返回 `(Sender, Recver)`，而 POSIX `pipe()` 返回 `(read_fd, write_fd)`。两者顺序一致吗？  
**A2**：不一致。POSIX 是读端在前、写端在后（`fds[0]` 读、`fds[1]` 写）；interprocess 的公共 `pipe()` 把顺序调成了**写端 `Sender` 在前、读端 `Recver` 在后**。后端代码里正是把 `fds[1]` 包成 `Sender`、`fds[0]` 包成 `Recver` 来对齐这个对外约定。

### 4.3 Sender：发送端与 multimacro! 转发

#### 4.3.1 概念说明

`Sender` 是匿名管道的**写端**。它的「核心功能」全部通过标准库的 [`Write`](https://doc.rust-lang.org/std/io/trait.Write.html) trait 暴露——`write_all`、`flush` 等方法都来自 `Write`。除此之外，`Sender` 还能：

- 与 `OwnedHandle` / `OwnedFd` **互转**（`From` / `Into`），从而能跨进程传递；
- 暴露原始句柄值（`AsRawHandle` / `AsRawFd`）；
- 打印 `Debug`。

这些 trait **没有一个是手写的**——它们全部由 `multimacro!` 批量缝上去。

`Sender` 的定义本身只有两行（[src/unnamed_pipe.rs:93-94](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/unnamed_pipe.rs#L93-L94)）：

```rust
pub struct Sender(pub(crate) SenderImpl);
impl Sealed for Sender {}
```

字段 `pub(crate)` 意味着**外部无法构造** `Sender`（守住了「只能由 `pipe()` 创建」的不变式），但 crate 内的后端可以自由构造它。

#### 4.3.2 核心流程

`multimacro!` 在 `Sender` 上挂了 4 个转发宏（[src/unnamed_pipe.rs:95-101](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/unnamed_pipe.rs#L95-L101)）：

```rust
multimacro! {
    Sender,
    forward_sync_write,
    forward_handle,
    forward_debug,
    derive_raw,
}
```

每个宏各贡献一组 trait，对应关系如下：

| 宏 | 贡献的 trait | 效果 |
|----|------------|------|
| `forward_sync_write` | `impl Write for Sender` | `write`/`flush`/`write_vectored` 全部转发给 `self.0`（`SenderImpl`） |
| `forward_handle` | `AsHandle`/`AsFd` + `From<OwnedHandle>`/`From<OwnedFd>` + `From<Sender> for OwnedHandle/OwnedFd` | 安全的句柄借用与双向转换（六种） |
| `forward_debug` | `impl Debug` | `Debug::fmt` 转发给 `self.0` |
| `derive_raw` | `AsRawHandle`/`AsRawFd` + `IntoRaw*` + `FromRawHandle`/`FromRawFd` | **派生**（不是转发）：在安全句柄 trait 之上拼出原始 trait |

`multimacro!` 本体（[src/macros.rs:33-46](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros.rs#L33-L46)）做的事很朴素：把同一个类型名依次喂给列表里的每个宏。

注意 `forward_*` 与 `derive_*` 是两类不同性质的工具（u7-l1 会系统讲）：

- `forward_sync_write` / `forward_handle` / `forward_debug` 是**转发宏**：直接调用 `self.0` 上已有的同名实现。
- `derive_raw` 是**派生宏**：它不转发，而是**基于安全句柄 trait 拼装**出原始 trait。例如 `as_raw_handle` 的实现是「先 `as_handle()` 拿到借用句柄，再对它调 `as_raw_handle()`」（见 [src/macros/derive_raw.rs:9-17](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros/derive_raw.rs#L9-L17)）。

#### 4.3.3 源码精读

**转发宏 `forward_sync_write`** 生成（[src/macros/forward_iorw.rs:24-46](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros/forward_iorw.rs#L24-L46)）：

```rust
impl ::std::io::Write for $ty {
    fn write(&mut self, buf: &[u8]) -> ::std::io::Result<usize> { self.0.write(buf) }
    fn flush(&mut self) -> ::std::io::Result<()> { self.0.flush() }
    fn write_vectored(...) { self.0.write_vectored(bufs) }
}
```

于是 `tx.write_all(b"...")` 最终走到 `self.0.write(...)`，即后端 `SenderImpl` 的 `write`。

**转发宏 `forward_handle`** 是一组嵌套宏的根（[src/macros/forward_handle_and_fd.rs:85-98](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros/forward_handle_and_fd.rs#L85-L98)），它等于 `forward_asinto_handle` + `forward_from_handle`，最终给出（在对应 `cfg` 下）：

```rust
impl AsHandle for Sender { fn as_handle(&self) -> BorrowedHandle<'_> { AsHandle::as_handle(&self.0) } }
impl From<OwnedHandle> for Sender { fn from(x: OwnedHandle) -> Self { Self(From::from(x)) } }
impl From<Sender> for OwnedHandle { fn from(x: Sender) -> Self { From::from(x.0) } }
// Unix 上对称地给出 AsFd / From<OwnedFd> ...
```

这就解释了示例里 `let txh: OwnedHandle = tx.into();` 为何能编译——它用的正是 `impl From<Sender> for OwnedHandle`。

**派生宏 `derive_raw`**（[src/macros/derive_raw.rs:124-137](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros/derive_raw.rs#L124-L137)）展开为 `derive_asintoraw` + `derive_fromraw`，拼出 `AsRawHandle` 等。其 `as_raw_handle` 的实现（[src/macros/derive_raw.rs:9-17](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros/derive_raw.rs#L9-L17)）值得一看：

```rust
fn $mtd(&self) -> RawHandle {
    let h = AsHandle::as_handle(self);   // 先拿安全借用句柄
    AsRawHandle::as_raw_handle(&h)        // 再取原始值
}
```

也就是说 `Sender` 的「原始句柄」能力是**搭在安全句柄能力之上**派生出来的，没有重复实现。

**Windows 后端 Sender 的特殊之处——limbo**。公共 `Sender` 的文档（[src/unnamed_pipe.rs:82-85](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/unnamed_pipe.rs#L82-L85)）提到：

> On Windows, much like named pipes, unnamed pipes are subject to limbo, meaning that dropping an unnamed pipe does not immediately discard the contents of the send buffer.

这体现在后端 `Sender` 的 `Drop`（[src/os/windows/unnamed_pipe.rs:124-158](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/unnamed_pipe.rs#L124-L158)）：写端持有 `needs_flush: bool` 标记，`write` 成功后置为 `true`，`drop` 时若仍需刷新就交给 `linger_pool`（u8-l1 详讲）在后台 `FlushFileBuffers` 后再关闭句柄，避免缓冲里尚未被读端取走的字节丢失。Unix 后端没有这个问题（内核管道缓冲在关闭写端后仍可被读端读出，且读端会得到 EOF）。

#### 4.3.4 代码实践（源码阅读型）

**目标**：把「`Sender` 上的 trait 全是宏缝上去的」这件事验证到底。

**步骤**：

1. 在 [src/unnamed_pipe.rs:95-101](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/unnamed_pipe.rs#L95-L101) 找到 `Sender` 的 `multimacro!` 列表。
2. 逐个打开宏定义，确认每个宏给 `Sender` 加了什么：
   - `forward_sync_write` → [forward_iorw.rs:24](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros/forward_iorw.rs#L24)
   - `forward_handle` → [forward_handle_and_fd.rs:85](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros/forward_handle_and_fd.rs#L85)
   - `forward_debug` → [forward_fmt.rs:13](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros/forward_fmt.rs#L13)
   - `derive_raw` → [derive_raw.rs:124](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros/derive_raw.rs#L124)
3. 体会：`Sender` 结构体本体**零手写 trait**，但用户拿到的是一个 `Write` + 全套句柄转换 + `Debug` 的完整类型。

**预期观察**：你会确认 interprocess 用「转发宏 + 派生宏」把 newtype 的样板代码压缩到了极致——这正是 `multimacro!` 的价值。

#### 4.3.5 小练习与答案

**Q1**：`forward_handle` 给出的 `impl From<Sender> for OwnedHandle`，和 `derive_raw` 给出的 `IntoRawHandle`，都能「拿走」`Sender` 的句柄，二者有何区别？  
**A1**：`From<Sender> for OwnedHandle` 是**安全**的，它把 `Sender` 消费掉并交出一个**会自动关闭**的 `OwnedHandle`（所有权干净转移）。`IntoRawHandle` 是 **unsafe** 的，它吐出一个原始整数、不再关闭句柄，调用方要自己负责——一般只在跨进程传递句柄数值时用（见 u5-l2）。

**Q2**：为什么 Windows 的后端 `Sender` 字段用 `ManuallyDrop` 并自带 `Drop`，而 Unix 后端不用？  
**A2**：Windows 写端 drop 时需要按需触发后台 `FlushFileBuffers`（limbo 机制）才能安全关闭，故要自定义 `Drop`，并在其中 `ManuallyDrop::take` 取出句柄交给 `linger_pool`；Unix 管道缓冲由内核托管，写端关闭即可（读端随后读到 EOF），`OwnedFd` 的默认 drop 足够，无需自定义。

### 4.4 Recver：接收端与句柄可继承性

#### 4.4.1 概念说明

`Recver` 是匿名管道的**读端**，核心功能通过 [`Read`](https://doc.rust-lang.org/std/io/trait.Read.html) trait 暴露。它和 `Sender` 是镜像关系：同样是无手写 trait 的 newtype 壳，同样由 `multimacro!` 缝上一套 trait，区别只在「芯」是读实现而非写实现。

`Recver` 的定义（[src/unnamed_pipe.rs:62-63](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/unnamed_pipe.rs#L62-L63)）：

```rust
pub struct Recver(pub(crate) RecverImpl);
impl Sealed for Recver {}
multimacro! {
    Recver,
    forward_sync_read,
    forward_handle,
    forward_debug,
    derive_raw,
}
```

与 `Sender` 相比，唯一的差别是 `forward_sync_write` 换成了 `forward_sync_read`（[src/unnamed_pipe.rs:64-70](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/unnamed_pipe.rs#L64-L70)）。这就是「读端 vs 写端」在公共层的全部体现。

#### 4.4.2 核心流程

`Recver` 的 trait 来源与 `Sender` 完全对称（把「写」换成「读」）：

```text
forward_sync_read  → impl Read for Recver          (read / read_vectored)
forward_handle     → AsHandle/AsFd + From<OwnedHandle>/From<OwnedFd> + 反向
forward_debug      → impl Debug
derive_raw         → AsRawHandle/AsRawFd + IntoRaw* + FromRaw*
```

因为 `Recver: Read`，你可以直接用标准库的 `BufReader::new(rx)` 把它包成带缓冲的读端，再用 `read_line` / `read_until` 等便利方法——示例 [side_a.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/examples/unnamed_pipe/sync/side_a.rs) 正是这么做的。

读端的关键语义（与 u4-l4 的 FIFO 一致）：

- 若缓冲区暂时无数据，`read` 会**阻塞**，直到有数据或写端全部关闭。
- **所有写端都关闭后**，`read` 返回 `Ok(0)`，即 EOF——这是「对方写完了」的信号。
- 在 Unix 上可用 `UnnamedPipeExt::set_nonblocking` 切到非阻塞模式，此时无数据则立即返回 `WouldBlock`。

#### 4.4.3 源码精读

`forward_sync_read` 生成的 `impl Read`（[src/macros/forward_iorw.rs:4-22](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros/forward_iorw.rs#L4-L22)）：

```rust
impl ::std::io::Read for $ty {
    fn read(&mut self, buf: &mut [u8]) -> ::std::io::Result<usize> { self.0.read(buf) }
    fn read_vectored(&mut self, bufs) { self.0.read_vectored(bufs) }
}
```

注意它**没有**生成 `read_to_end`（宏注释写明：这个宏不打算用在 `Chain` 这类适配器上）。`read_to_end` 是 `Read` 的默认方法，会反复调用 `read`，所以照样可用。

**Unix 后端 `Recver`**（[src/os/unix/unnamed_pipe.rs:91-105](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/unnamed_pipe.rs#L91-L105)）是 `FdOps` 的 newtype，`Read` 实现最终落到 [fdops.rs:31-40](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/fdops.rs#L31-L40) 的 `libc::read` / `libc::readv`。

**Unix 专有扩展 `UnnamedPipeExt`**（[src/os/unix/unnamed_pipe.rs:22-40](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/unnamed_pipe.rs#L22-L40)）提供 `set_nonblocking`：

```rust
pub trait UnnamedPipeExt: AsFd + Sealed {
    fn set_nonblocking(&self, nonblocking: bool) -> io::Result<()> {
        c_wrappers::set_nonblocking(self.as_fd(), nonblocking)
    }
}
impl UnnamedPipeExt for PubRecver {}
impl UnnamedPipeExt for PubSender {}
```

它同时对 `Recver` 和 `Sender` 实现，底层调 `fcntl(F_SETFL, O_NONBLOCK)`。Windows 没有这个 trait（命名管道/匿名管道在 Windows 上有不同的非阻塞模型）。

**Windows 后端 `Recver`**（[src/os/windows/unnamed_pipe.rs:104-122](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/unnamed_pipe.rs#L104-L122)）是 `AdvOwnedHandle` 的 newtype，`Read` 手写为 `c_wrappers::read(self.as_handle(), buf)`，比 Unix 版少了 vectored 与 `needs_flush` 那套逻辑（读端无需刷新）。

**句柄可继承性**是本讲的另一条主线。模块文档（[src/unnamed_pipe.rs:7-11](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/unnamed_pipe.rs#L7-L11)）说默认可继承，Windows 后端用 `CreationOptions::inheritable`（默认 `true`，[src/os/windows/unnamed_pipe.rs:36](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/unnamed_pipe.rs#L36) 与 [:45](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/unnamed_pipe.rs#L45)）控制。继承性使得 4.4.4 / 综合实践里「把 `Sender` 转成 `OwnedHandle`/`OwnedFd` 再传给另一个执行流」成为可能——这正是示例 [side_a.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/examples/unnamed_pipe/sync/side_a.rs) 的核心手法（见第 16-25 行）。

#### 4.4.4 代码实践（编写型）

**目标**：写一个最小的「单进程内用 `pipe()` 收发字符串」程序，验证 `Recver: Read`。

> 以下为**示例代码**，非仓库原有代码；建议在依赖 `interprocess` 的测试 crate 中运行。

```rust
// 示例代码：用 pipe() 在单进程内读一行
use interprocess::unnamed_pipe::pipe;
use std::io::{prelude::*, BufReader};

fn main() -> std::io::Result<()> {
    let (tx, rx) = pipe()?;                 // (Sender, Recver)
    let mut rx = BufReader::new(rx);        // Recver: Read，可被 BufReader 包装
    std::thread::spawn(move || {
        let mut tx = tx;                    // 捕获写端到子线程
        tx.write_all(b"hello, recver\n").unwrap();
        // tx 在此 drop，写端关闭；后续读端若再读会得到 EOF
    });
    let mut line = String::new();
    rx.read_line(&mut line)?;               // 读到换行即返回
    println!("got: {}", line.trim());
    Ok(())
}
```

**步骤**：把上面的代码放进一个 `fn main`，`Cargo.toml` 里加 `interprocess = "<版本>"`，然后 `cargo run`。

**需要观察的现象**：

1. 程序正常退出，打印 `got: hello, recver`。
2. 若把子线程里的 `\n` 去掉，`read_line` 会**一直阻塞**直到写端关闭（子线程结束时 `tx` drop，读端读到 EOF，`read_line` 以当前已读内容返回）——这正好印证 4.4.2 的 EOF 语义。

**预期结果**：能稳定复现「带换行立即返回、不带换行等到写端关闭」两种行为。若你所在平台/版本的缓冲行为有差异，相关部分标注**待本地验证**。

#### 4.4.5 小练习与答案

**Q1**：为什么 `forward_sync_read` 没有生成 `read_to_end`，但示例里仍能用 `read_to_end` 读出全部内容？  
**A1**：`read_to_end` 是 `Read` trait 提供的**默认方法**，内部循环调用 `read`。只要 `read` 被正确转发（`forward_sync_read` 已做），`read_to_end` 自然可用，无需宏重复生成。

**Q2**：Unix 上想把读端设成非阻塞，该用哪个 API？为什么 Windows 没有对应物？  
**A2**：用 `UnnamedPipeExt::set_nonblocking(true)`（[src/os/unix/unnamed_pipe.rs:33](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/unnamed_pipe.rs#L33)），底层 `fcntl`。Windows 的匿名/命名管道采用基于 overlapped I/O 的并发模型而非「fd 标志位非阻塞」，故没有同名扩展（非阻塞/超时在 Windows 上走另一套机制）。

## 5. 综合实践

把本讲四块内容串起来：用 `pipe()` 建立管道、主线程写、另一线程读，完成一次字符串传递，并顺带验证句柄可转换性。

> 以下为**示例代码**，借鉴了仓库示例 [examples/unnamed_pipe/sync/main.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/examples/unnamed_pipe/sync/main.rs) 的结构（那是「跨线程传递句柄」的版本，可对照阅读）。

```rust
// 示例代码：综合实践——主线程写、子线程读、并验证句柄转换
use interprocess::unnamed_pipe::pipe;
use std::io::{prelude::*, BufReader};

#[cfg(unix)]
use std::os::unix::io::{AsRawFd, FromRawFd, OwnedFd};
#[cfg(windows)]
use std::os::windows::io::{AsRawHandle, FromRawHandle, OwnedHandle};

fn main() -> std::io::Result<()> {
    let (mut tx, rx) = pipe()?;             // 4.2：创建管道，写端在前

    // 4.4：把读端交给子线程，用 BufReader 按行读
    let reader = std::thread::spawn(move || {
        let mut rx = BufReader::new(rx);
        let mut buf = String::new();
        rx.read_line(&mut buf).unwrap();
        buf
    });

    // 4.3：写端实现 Write，write_all 直接可用
    tx.write_all(b"Hello across the pipe!\n")?;

    // 验证 Sender 能拿到原始句柄值（来自 derive_raw / AsRaw*）
    #[cfg(unix)]
    println!("sender raw fd = {}", tx.as_raw_fd());
    #[cfg(windows)]
    println!("sender raw handle = {:?}", tx.as_raw_handle());

    drop(tx);                                // 关闭写端，确保任何等待 EOF 的读取能返回
    let received = reader.join().unwrap();
    println!("received: {}", received.trim());
    assert_eq!(received.trim(), "Hello across the pipe!");
    Ok(())
}
```

**实践要点**（结合各模块）：

1. **创建**（4.2）：`pipe()?` 一步拿到 `(Sender, Recver)`，注意是写端在前。
2. **读端**（4.4）：`Recver: Read`，可直接 `BufReader::new`；子线程里 `read_line` 遇换行即返回。
3. **写端**（4.3）：`Sender: Write`，`write_all` 发送一行；`as_raw_fd` / `as_raw_handle` 来自 `derive_raw` 派生的 `AsRaw*`。
4. **EOF 语义**：`drop(tx)` 关闭写端，演示读端在写端关闭后获得 EOF——若把消息里的 `\n` 去掉，`read_line` 会一直阻塞到 `drop(tx)` 才返回（**待本地验证**在不同平台的行为是否一致）。
5. **对照真实示例**：仓库的 [side_a.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/examples/unnamed_pipe/sync/side_a.rs) + [side_b.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/examples/unnamed_pipe/sync/side_b.rs) 进一步演示了「把 `Sender` 转成 `OwnedHandle`/`OwnedFd`、经通道传到另一个执行流、再用 `Sender::from(handle)` 重建」的完整句柄传递链——这正是 u5-l2 的主题，本实践只在其基础上做了简化。

**运行方式**：在依赖 `interprocess`（同步，不开 `tokio`）的 crate 中 `cargo run`。

## 6. 本讲小结

- `unnamed_pipe` 是 interprocess 最贴近 OS 原语的模块：一个 `pipe()` + 两个句柄类型 `Sender`（写）/ `Recver`（读），核心能力经标准库 `Write` / `Read` trait 暴露。
- 模块沿用 u2-l3 的「壳/芯」分层：公共 `Sender`/`Recver` 是 `pub(crate)` 字段的 newtype 壳，`impmod!` 把后端 `SenderImpl`/`RecverImpl`/`pipe_impl` 注入公共层；后端**直接返回公共类型**。
- `pipe()` 本身只转发给后端 `pipe_impl()`：Unix 用 `libc::pipe2`/`libc::pipe`（真正 I/O 在 `FdOps` 的 `libc::read`/`write`），Windows 用 `CreatePipe`（经 `CreationOptions` 构建器）。
- `multimacro!` 为公共类型批量缝上 trait：`forward_sync_read`/`forward_sync_write`（`Read`/`Write`）、`forward_handle`（安全句柄六转换）、`forward_debug`、`derive_raw`（派生 `AsRaw*`/`FromRaw*`/`IntoRaw*`）。newtype 本体零手写 trait。
- 句柄/FD **默认可继承**（Windows `CreationOptions::inheritable` 默认 `true`），这是匿名管道适合**子进程通信**的根基；`From<Sender/Recver> for OwnedHandle/OwnedFd` 与 `From<...>` 的反向转换让句柄能跨执行流/跨进程传递。
- 平台差异：Windows 写端有 **limbo**（drop 时经 `linger_pool` 后台刷新），Unix 有 `UnnamedPipeExt::set_nonblocking` 扩展。

## 7. 下一步学习建议

- **u5-l2（句柄/FD 传递与子进程通信）**：本讲的直接后续。它把「句柄可继承 + `From<Sender> for OwnedHandle`」升级为真正的**跨进程**传递——父进程 spawn 子进程、用命令行/环境变量把句柄数值传过去、子进程用 `FromRawHandle`/`FromRawFd` 重建 `Sender`/`Recver`。建议先精读仓库示例 [side_a.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/examples/unnamed_pipe/sync/side_a.rs) 与 [side_b.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/examples/unnamed_pipe/sync/side_b.rs)。
- **u7-l1（宏系统全景）**：若你想彻底搞懂 `forward_*` 与 `derive_*` 两类宏的区别、以及 `multimacro!` 如何批量调度它们，这是专门讲宏的一篇。
- **u6-l1（Tokio 集成）**：本讲的 [src/unnamed_pipe/tokio.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/unnamed_pipe/tokio.rs) 与同步版结构同构（u2-l3 已点明），异步版把 `Write`/`Read` 换成 `AsyncWrite`/`AsyncRead`、芯换成 `AsyncFd<FdOps>`，可在学完同步管道后无缝迁移。
