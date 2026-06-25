# unsafe FFI 封装层

## 1. 本讲目标

学完本讲，你应当能够：

- 理解 interprocess 为什么、以及如何把裸的 `libc` / `windows-sys` 系统调用包成统一的 `io::Result`。
- 掌握 `OrErrno` / `FdOrErrno` / `HandleOrErrno` 这套「先把返回值归一化成布尔、再用 errno 翻译」的错误转换范式，并说清 errno 抓取时机的安全不变式。
- 掌握 `RawOsErrorExt::eeq` 在跨整数类型错误码比对（`Option<i32>` 对 `u32`）里的作用，以及它如何支撑「把某个 OS 错误码改写成更合适的 `ErrorKind`」。
- 能逐行解读一个 `unsafe` 封装：说清前置条件、错误转换路径、以及它为什么满足 `forbid` 级别的 lint。
- 理解 crate 的 unsafe lint 策略（`unsafe_op_in_unsafe_fn = forbid`、`ptr_as_ptr = forbid` 等）如何反向塑造代码形态。

## 2. 前置知识

本讲是专家层内容，默认你已经读过 u4-1（Unix 后端）与 u4-2（Windows 后端）。下面补充几个本讲用到的概念。

- **FFI（Foreign Function Interface）**：Rust 调用 C ABI 函数的机制。`extern "C"` 是跨平台 C 调用约定，`extern "system"` 在 Windows 上对应 Win32 调用约定（MSVC 下等同 `"C"`）。`libc` 和 `windows-sys` crate 几乎全是这种 `extern "C"/"system"` 函数。
- **系统调用返回值约定**：Unix 上的 C 接口风格不一，但失败信号高度集中——多数返回 `-1` 表示失败、其余值表示成功，并把详细原因写进**线程局部**的 `errno`；Windows 的 Win32 则常用 `BOOL`（`0`/非 `0`）或 `HANDLE`（失败返回 `INVALID_HANDLE_VALUE`/`NULL`），原因写进线程局部的 `GetLastError()`。
- **`unsafe fn` 与 `unsafe {}` 的区别**：`unsafe fn` 声明「调用我需要调用方满足某个契约」，但它**并不会**让函数体内的 unsafe 操作自动变安全。Rust 2021 后，函数体内的 unsafe 操作仍须包在显式的 `unsafe { ... }` 块里（见 4.5）。
- **`io::Result` / `io::Error`**：Rust 标准库的错误类型。`io::Error::last_os_error()` 抓取当前线程的 errno / last error；`raw_os_error()` 返回 `Option<i32>`（原始错误码）。
- **`MaybeUninit<T>`**：声明「这块内存可能未初始化」，常用于「先给 OS 一块缓冲区，由 OS 写入」的输出参数，避免无意义的初始化。
- **借用句柄（Rust 1.63 I/O safety）**：`BorrowedFd<'_>` / `BorrowedHandle<'_>` 表示「借用一个 fd/句柄，不负责关闭」；`OwnedFd` / `OwnedHandle` 表示「拥有所有权，drop 时关闭」。它们让 fd/句柄的生命周期进类型系统。
- **承接 u4-1 / u4-2 的「壳/芯」分层**：公共 API 是「壳」，平台后端 `os::unix` / `os::windows` 是「芯」。而 `c_wrappers` 是「芯」里**最贴近 OS 的一层**——它直接调系统调用，再往上才是 `uds_local_socket` / `named_pipe::local_socket` 等后端逻辑。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/misc.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/misc.rs) | 定义全库共用的错误转换工具族：`OrErrno`、`ToBool`、`FdOrErrno`、`HandleOrErrno`、`RawOsErrorExt`。 |
| [src/os/unix/c_wrappers.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/c_wrappers.rs) | Unix 后端的 `libc` 系统调用封装（fcntl、getsockopt、bind、connect、poll、set_nonblocking 等）。 |
| [src/os/windows/named_pipe/c_wrappers.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/c_wrappers.rs) | Windows named pipe 的 Win32 封装（GetNamedPipeInfo、PeekNamedPipe、CreateFileW、ReOpenFile、WaitNamedPipeW 等）。 |
| [src/os/windows/named_pipe/listener.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/listener.rs) | 监听器：`block_on_connect` 封装 `ConnectNamedPipe`，`thunk_accept_error` 改写错误码。 |
| [src/os/windows/named_pipe/listener/create_instance.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/listener/create_instance.rs) | `create_instance` 封装 `CreateNamedPipeW`。 |
| [src/os/unix/fifo_file.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/fifo_file.rs) | `create_fifo` 封装 `mkfifo`。 |
| [Cargo.toml](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/Cargo.toml) / [src/lib.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/lib.rs) | unsafe / clippy lint 策略。 |

## 4. 核心概念与源码讲解

### 4.1 封装的总体范式：把「裸系统调用」变成 `io::Result`

#### 4.1.1 概念说明

C 系统调用的返回值风格极不统一：有的返回 `0` 表示成功、`-1` 表示失败（如 `unlink`、`mkfifo`、`bind`）；有的返回非负数表示成功、`-1` 失败（如 `socket`、`fcntl`、`connect`）；有的返回写入了多少字节；Windows 的 Win32 则常用 `BOOL` 或 `HANDLE`。如果每一处调用都手写 `if ret == -1 { return Err(...) }`，代码会被样板淹没，而且容易漏掉「errno 必须在调用后立即抓取」这一关键约束。

interprocess 的统一解法是**两步走**：

1. **归一化**：不管原始返回值长什么样，先用一个比较式把它变成「成功时为真」的布尔。例如 `libc::unlink(...) != -1`、`ConnectNamedPipe(...) != 0`、`libc::socket(...).fd_or_errno()`。
2. **翻译**：把这个布尔喂给 `OrErrno`，由它在「假」时调用 `io::Error::last_os_error()` 抓取 errno，组装成 `Err`。

这套范式把「错误码从哪来、何时抓」这件事**集中到 `OrErrno` 一个地方**，调用点只需写一行比较 + 一个链式方法。

#### 4.1.2 核心流程

把 `OrErrno` 看成这样一个数学函数，其中 `success(r)` 是从原始返回值 `r` 算出的「成功为真」布尔，`T` 是成功时要返回的值：

\[
\mathrm{OrErrno}(\text{success}(r),\, T) =
\begin{cases}
\mathrm{Ok}(T) & \text{当 } \text{success}(r) = 1 \\
\mathrm{Err}\big(\text{last\_os\_error}()\big) & \text{当 } \text{success}(r) = 0
\end{cases}
\]

调用点的固定三段式伪代码：

```
unsafe { 系统调用(...) 〈比较式〉 }       // ① unsafe 块调 syscall，产出 success 布尔
    .true_val_or_errno(〈成功值〉)        // ② 翻译成 io::Result（失败时抓 errno）
    .map(|h| 〈把裸值包成拥有型对象〉)     // ③（可选）把裸 fd/handle 包成 OwnedFd/OwnedHandle
```

**errno 抓取时机不变式**：`last_os_error()` 必须在系统调用之后、任何其它可能改写 errno 的库/系统调用之前执行。三段式把翻译紧贴在 syscall 表达式后链式调用，中间只发生一次布尔比较和分支判断，不触碰 errno，因此抓到的 errno 一定是该次调用写入的。

#### 4.1.3 源码精读

`OrErrno` 定义在 `src/misc.rs`：

[src/misc.rs:29-53](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/misc.rs#L29-L53) — 定义 trait `OrErrno<T>` 并为所有实现 `ToBool` 的类型提供 blanket impl。`true_or_errno` 是核心：布尔为真返回 `Ok(f())`，为假返回 `Err(io::Error::last_os_error())`。`true_val_or_errno(v)` 是它的快捷版，把成功值直接写死成 `v`。

```rust
pub(crate) trait OrErrno<T>: Sized {
    fn true_or_errno(self, f: impl FnOnce() -> T) -> io::Result<T>;
    #[inline(always)]
    fn true_val_or_errno(self, value: T) -> io::Result<T> { self.true_or_errno(|| value) }
    fn false_or_errno(self, f: impl FnOnce() -> T) -> io::Result<T>;
    // …（false_* 为相反约定的对应版本，当前封装实际只用 true_* 族）
}
impl<B: ToBool, T> OrErrno<T> for B {
    fn true_or_errno(self, f: impl FnOnce() -> T) -> io::Result<T> {
        if self.to_bool() { Ok(f()) } else { Err(io::Error::last_os_error()) }
    }
    // …
}
```

注意 `OrErrno` 的泛型 `B: ToBool`，因此它对 `bool` 与 `i32` 都成立。[src/misc.rs:139-149](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/misc.rs#L139-L149) 定义 `ToBool`：`bool` 直接是自己，`i32` 用 `self != 0`。这让 `ConnectNamedPipe(...) != 0`（已经是 `bool`）和「直接传 `i32`」都能用。

针对两种最常见约定，`misc.rs` 还提供了特化入口：

[src/misc.rs:55-75](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/misc.rs#L55-L75) — `FdOrErrno`（Unix）把「返回 `RawFd`，`-1` 为失败」封装成一行；`HandleOrErrno`（Windows）把「返回 `HANDLE`，`INVALID_HANDLE_VALUE` 为失败」封装成一行。二者本质都是「比较出 true-on-success 布尔，再调 `true_val_or_errno`」。

```rust
impl FdOrErrno for RawFd {
    fn fd_or_errno(self) -> io::Result<Self> { (self != -1).true_val_or_errno(self) }
}
impl HandleOrErrno for HANDLE {
    fn handle_or_errno(self) -> io::Result<Self> {
        (self != INVALID_HANDLE_VALUE).true_val_or_errno(self)
    }
}
```

于是 `libc::socket(...)` 的返回值可以直接 `.fd_or_errno()`，`CreateNamedPipeW(...)` 的返回值可以直接 `.handle_or_errno()`——成功时把 fd/handle 原样带出，失败时自动抓 errno。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：体会三段式如何把样板压成一行。
2. **步骤**：打开 [src/os/unix/c_wrappers.rs:128-130](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/c_wrappers.rs#L128-L130)，看 `unlink` 的封装；再对照「不使用 OrErrno」的等价手写版本。
3. **现象**：原代码是 `unsafe { libc::unlink(path.as_ptr()) != -1 }.true_val_or_errno(())`，只有一行。等价手写需要：`let r = unsafe { libc::unlink(...) }; if r == -1 { return Err(io::Error::last_os_error()); } Ok(())`。
4. **预期结果**：你能说清两件事——（a）`!= -1` 这个比较式就是 `success(r)`；（b）`true_val_or_errno(())` 在假分支里替你调了 `last_os_error()`。
5. 待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `OrErrno` 的 blanket impl 里抓错误用的是 `io::Error::last_os_error()`，而不是 `io::Error::from_raw_os_error(errno)`？

**参考答案**：`last_os_error()` 内部就是去读线程局部的 errno（Unix）或 `GetLastError()`（Windows），它等价于「抓取当前最近一次系统调用写入的错误码」。用它比手动读 errno 更简洁，且与「系统调用刚执行完、errno 尚未被污染」的时机正好契合。

**练习 2**：`OrErrno` 是 `pub(crate)` 的。如果某个下游用户想在 `std::os::unix::net` 之外复用它，会发生什么？

**参考答案**：编译报错——它是 crate 私有的，外部无法命名或实现。这说明 interprocess 把这套错误转换工具定位为**内部基础设施**，不作为公共 API 承诺稳定性。

---

### 4.2 RawOsErrorExt：跨类型的错误码比对

#### 4.2.1 概念说明

`io::Error::raw_os_error()` 返回 `Option<i32>`——Unix 上 errno 是 `i32`，Windows 上 Rust 也会把 Win32 错误码规整成 `i32`（可能为负）。但 `windows-sys` 导出的错误码常量（如 `ERROR_PIPE_CONNECTED`）是 `u32`。当你要问「这个错误是不是某个特定码」时，类型对不上。

`RawOsErrorExt::eeq`（"errno equals"）就是为了弥合这个缝隙：它接收一个 `u32` 常量，把 `Option<i32>` 的内部值按位转成 `u32` 再比较。这样 `e.raw_os_error().eeq(ERROR_PIPE_CONNECTED)` 就是一句话的事。

#### 4.2.2 核心流程

`eeq` 的典型用法不是单独判断，而是配合 `or_else` / `match` 做**错误码改写**——把某个底层错误码翻译成更符合 Rust 习惯的 `ErrorKind`，或把它「降级」成成功。例如：

- `ERROR_PIPE_NOT_CONNECTED`（对端已关闭）→ `ErrorKind::BrokenPipe`。
- `ERROR_PIPE_LISTENING`（实例尚无连接）→ `ErrorKind::WouldBlock`（非阻塞语境）。
- `ERROR_PIPE_CONNECTED`（连接在 `ConnectNamedPipe` 之前就到了，见 4.4）→ 当作成功 `Ok(())`。

#### 4.2.3 源码精读

[src/misc.rs:239-251](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/misc.rs#L239-L251) — `RawOsErrorExt` 的定义与对 `Option<i32>` 的实现。`#[allow(clippy::cast_sign_loss)]` 配注释「bitwise comparison」说明：这是按位模式比较，不是数值比较，因此 `i32 → u32` 的符号位丢失无关紧要。

```rust
pub(crate) trait RawOsErrorExt {
    fn eeq(self, other: u32) -> bool;
}
impl RawOsErrorExt for Option<i32> {
    #[allow(clippy::cast_sign_loss)] // bitwise comparison
    fn eeq(self, other: u32) -> bool {
        match self { Some(n) => n as u32 == other, None => false }
    }
}
```

一个干净的应用例子在 [src/os/windows/misc.rs:15-23](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/misc.rs#L15-L23)：`decode_eof` 把 `ERROR_PIPE_NOT_CONNECTED` 改写成 `BrokenPipe`，让上层读到的错误更「Rust 化」。

`eeq` 真正发挥作用的地方是 named pipe 监听器的错误改写，见 [src/os/windows/named_pipe/listener.rs:169-177](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/listener.rs#L169-L177)（`thunk_accept_error`）：

```rust
fn thunk_accept_error(e: io::Error) -> io::Result<()> {
    if e.raw_os_error().eeq(ERROR_PIPE_CONNECTED) {
        Ok(())
    } else if e.raw_os_error().eeq(ERROR_PIPE_LISTENING) {
        Err(io::Error::from(io::ErrorKind::WouldBlock))
    } else {
        Err(e)
    }
}
```

#### 4.2.4 代码实践（源码阅读型）

1. **目标**：看清 `eeq` 如何把多个 Win32 错误码各归其位。
2. **步骤**：阅读 `thunk_accept_error`（上文），列出它处理的三种情况。
3. **现象**：`ERROR_PIPE_CONNECTED` 被吞成 `Ok(())`；`ERROR_PIPE_LISTENING` 被改写成 `WouldBlock`；其余原样上抛。
4. **预期结果**：你能解释为什么 `ERROR_PIPE_CONNECTED` 要算成功（它表示「客户端在服务端调用 `ConnectNamedPipe` 之前就已连上」，对 `accept` 而言正是想要的结果）。
5. 待本地验证。

#### 4.2.5 小练习与答案

**练习**：`eeq` 实现里为何是 `n as u32 == other`，而不是 `n == other as i32`？

**参考答案**：`other` 是 `u32`，最高位可能为 1（如 `ERROR_PIPE_LISTENING = 536870386`，高位是 1）。若写成 `other as i32`，它会被解释成一个很大的负数；而 errno 经 Rust 规整后存为 `i32` 时也正是这个负值的位模式。两种写法在「按位相等」意义上其实等价，但 `n as u32` 把比较有符号无符号的方向统一起来，配合 `cast_sign_loss` 的 allow 注释，明确表达了「这里只比位模式」，读起来意图更清楚。

---

### 4.3 Unix `c_wrappers` 精读

#### 4.3.1 概念说明

Unix 封装层 [src/os/unix/c_wrappers.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/c_wrappers.rs) 直接调用 `libc`。它的句柄参数一律用 `BorrowedFd<'_>`（借用，不关闭），需要产生新 fd 的函数返回 `OwnedFd`（拥有，drop 时关闭）。这是 Rust I/O safety 的体现：fd 的生命周期进类型系统，编译器保证「每个 fd 恰好被关闭一次」。

本模块几乎每个函数都是「unsafe 调系统调用 + `OrErrno` 翻译」的同款三段式，区别只在返回值约定（`!= -1` 还是 `fd_or_errno`）和是否需要输出缓冲区。

#### 4.3.2 核心流程

以三种典型形态为例：

- **简单 `0/-1` 约定**：`mkfifo`、`unlink`——`!= -1` 产布尔，`.true_val_or_errno(())`。
- **返回新 fd**：`socket`、`fcntl(F_DUPFD_CLOEXEC)`——`.fd_or_errno()` 成功时带出 fd，再 `OwnedFd::from_raw_fd` 包成拥有型。
- **带输出缓冲区**：`getsockopt`——用 `MaybeUninit<T>` 给内核写，校验写入长度，再 `assume_init`。

#### 4.3.3 源码精读

最薄的例子是 `mkfifo` 封装，位于 [src/os/unix/fifo_file.rs:38-45](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/fifo_file.rs#L38-L45)：

```rust
fn _create_fifo(path: &Path, mode: mode_t) -> io::Result<()> {
    let path = CString::new(path.as_os_str().as_bytes())?;
    unsafe { libc::mkfifo(path.as_bytes_with_nul().as_ptr().cast(), mode) != -1 }
        .true_val_or_errno(())
}
```

要点三处：（a）`CString::new(...)?` 保证路径不含内嵌 NUL，且 `as_bytes_with_nul` 给出 NUL 终止的字节序列——这是 `mkfifo` 的前置条件；（b）`.as_ptr().cast()` 把 `*const u8` 显式转成 `mkfifo` 要的 `*const c_char`（用 `.cast()` 而非 `as`，见 4.5）；（c）`!= -1` 即 `success(r)`，`true_val_or_errno(())` 翻译。

返回新 fd 的例子：[src/os/unix/c_wrappers.rs:56-58](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/c_wrappers.rs#L56-L58)（`fcntl_int`）。注意它本身是 `pub(super) unsafe fn`，体内还套了一层 `unsafe {}`——这正是为满足 `unsafe_op_in_unsafe_fn = forbid`（见 4.5）。

```rust
pub(super) unsafe fn fcntl_int(fd: BorrowedFd<'_>, cmd: c_int, val: c_int) -> io::Result<c_int> {
    unsafe { libc::fcntl(fd.as_raw_fd(), cmd, val) }.fd_or_errno()
}
```

带输出缓冲区的例子最值得细读：[src/os/unix/c_wrappers.rs:94-110](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/c_wrappers.rs#L94-L110)（`getsockopt`）。

```rust
pub(super) unsafe fn getsockopt<T>(fd, level, optname) -> io::Result<T> {
    let mut rslt = MaybeUninit::<T>::uninit();
    #[allow(clippy::cast_possible_truncation)] // safety contract
    let orig_len = size_of::<T>() as socklen_t;
    let mut len = orig_len;
    let success = unsafe {
        libc::getsockopt(fd.as_raw_fd(), level, optname, rslt.as_mut_ptr().cast(), &mut len) >= 0
    };
    if len < orig_len {
        return Err(io::Error::from(io::ErrorKind::InvalidData));
    }
    success.true_or_errno(|| unsafe { rslt.assume_init() })
}
```

这里有一个精妙之处：`true_or_errno` 接收的是一个**闭包** `|| unsafe { rslt.assume_init() }`。`assume_init()` 的安全前提是「该内存确实已被初始化」，而闭包只在 `success` 为真（即 `getsockopt` 成功）时才会被执行。于是 `assume_init` 的调用与它的安全前提被绑在了一起——失败路径根本不会触碰未初始化内存。同时闭包内的 `assume_init` 仍被显式 `unsafe {}` 包住，满足 forbid lint。

`len < orig_len` 的提前返回，是另一层防御：内核写入的字节数少于 `size_of::<T>()` 时直接报 `InvalidData`，避免读到残缺数据。

#### 4.3.4 代码实践（源码阅读型）

1. **目标**：把 `getsockopt` 的错误处理路径完整跑通。
2. **步骤**：跟踪 [src/os/unix/c_wrappers.rs:281-284](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/c_wrappers.rs#L281-L284) 的 `take_error`，看它如何用 `getsockopt(fd, SOL_SOCKET, SO_ERROR)` 取出非阻塞 connect 的异步错误。
3. **现象**：`take_error` 调 `getsockopt` 得到一个 `i32` errno，再用 `(errno != 0).then(...)` 决定返回 `Some` 还是 `None`。
4. **预期结果**：你能解释「为何取 `SO_ERROR` 不会因为上一次错误而误报」——因为 `getsockopt` 自己成功（`>= 0`），`success` 为真，闭包执行 `assume_init` 读出内核写入的 errno 值。
5. 待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`getsockopt` 为什么要先判断 `len < orig_len`，再判断 `success`？

**参考答案**：即使 `getsockopt` 返回成功，内核可能写入不足 `size_of::<T>()` 字节（例如对某些选项返回更短的结构）。此时直接 `assume_init` 会读到部分未初始化的字节，是 UB。先校验长度能挡住这类「成功但不完整」的情况，把它转成 `InvalidData`，是防御性编程。

**练习 2**：把 `getsockopt` 改成「先 `assume_init` 再判断成功」会有什么后果？

**参考答案**：若 `getsockopt` 失败，`rslt` 根本没被内核写入，此时 `assume_init` 会把未初始化内存当作合法 `T` 读出——典型未定义行为。当前写法用闭包把 `assume_init` 推迟到成功分支，从结构上杜绝了这一点。

---

### 4.4 Windows `c_wrappers` 与 named pipe 监听器

#### 4.4.1 概念说明

Windows 封装层的句柄参数用 `BorrowedHandle<'_>`，拥有型返回用 `OwnedHandle`。Win32 函数返回值有两种主流形态：返回 `BOOL`（`i32`，非 0 为真）或返回 `HANDLE`（失败为 `INVALID_HANDLE_VALUE`）。前者用 `.true_val_or_errno(...)`，后者用 `.handle_or_errno()`，与 Unix 端完全对称。

一个 Windows 特有的小工具是「可选输出指针」：很多 Win32 查询函数的每个输出参数都可传 `NULL` 表示「我不关心这个值」。interprocess 用 `Option<&mut T>` 表达「关心/不关心」，再统一翻译成裸指针。

#### 4.4.2 核心流程

named pipe 服务端的一次创建-接客流程是：

1. `CreateNamedPipeW` 造一个「待命实例」（[create_instance.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/listener/create_instance.rs)）。
2. `ConnectNamedPipe` 在该实例上等客户端连接（`block_on_connect`）。
3. 若 `ConnectNamedPipe` 报特殊码，用 `thunk_accept_error` 改写（4.2 已讲）。

#### 4.4.3 源码精读

可选输出指针的小工具在 [src/os/windows/named_pipe/c_wrappers.rs:26-37](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/c_wrappers.rs#L26-L37)：

```rust
fn optional_out_ptr<T>(outref: Option<&mut T>) -> *mut T {
    outref.map(mut2ptr).unwrap_or(ptr::null_mut())
}
pub(crate) unsafe fn hget(handle, f) -> io::Result<u32> {
    let mut x: u32 = 0;
    unsafe { f(handle.as_raw_handle(), mut2ptr(&mut x)) }.true_val_or_errno(x)
}
```

`hget` 把「接收一个句柄、一个 `*mut u32`、返回 `BOOL`」的整族查询函数（如某些 `Get*`）抽象成一个泛型调用：内部就地开一个 `u32` 缓冲，调函数，`true_val_or_errno(x)` 翻译——失败时虽然返回 `Err`，但 `x` 作为闭包值无副作用。

`CreateNamedPipeW` 的封装在 [src/os/windows/named_pipe/listener/create_instance.rs:65-79](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/listener/create_instance.rs#L65-L79)：

```rust
unsafe {
    CreateNamedPipeW((*self.path).as_ptr(), open_mode, pipe_mode, max_instances, /* … */)
        .handle_or_errno()
        // SAFETY: we just made it and received ownership
        .map(|h| OwnedHandle::from_raw_handle(h))
}
```

这是 4.1 三段式的完整实例：① `unsafe {}` 调 `CreateNamedPipeW`；② `.handle_or_errno()` 把 `HANDLE` 翻译成 `io::Result<RawHandle>`（失败即 `INVALID_HANDLE_VALUE` 时抓 `GetLastError`）；③ `.map(|h| OwnedHandle::from_raw_handle(h))` 把裸句柄包成拥有型。注释 `// SAFETY: we just made it and received ownership` 说清了 `from_raw_handle` 的安全前提——这个句柄是我们刚创建的、且 OS 把所有权交给了我们。

`ConnectNamedPipe` 的封装在 [src/os/windows/named_pipe/listener.rs:163-167](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/listener.rs#L163-L167)（`block_on_connect`）：

```rust
fn block_on_connect(handle: BorrowedHandle<'_>) -> io::Result<()> {
    unsafe { ConnectNamedPipe(handle.as_raw_handle(), ptr::null_mut()) != 0 }
        .true_val_or_errno(())
        .or_else(thunk_accept_error)
}
```

注意第三步换成了 `.or_else(thunk_accept_error)`：`ConnectNamedPipe` 即便返回「失败」（`0`），也可能只是 `ERROR_PIPE_CONNECTED`（客户端已抢先连上）这种「其实算成功」的情况，所以要把错误交给 `thunk_accept_error` 二次裁定（见 4.2.3）。这是 Windows named pipe 特有的、必须后处理错误码的典型场景。

#### 4.4.4 代码实践（源码阅读型，对应综合实践预备）

1. **目标**：把 `block_on_connect` 与其调用方串起来，看清一次 `accept` 的完整错误流。
2. **步骤**：从 [src/os/windows/named_pipe/listener.rs:63-79](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/listener.rs#L63-L79) 的 `accept` 出发，跟踪它调用 `block_on_connect_clearing_empty_conns` → `block_on_connect` → `thunk_accept_error` 的链路。
3. **现象**：`accept` 在持锁状态下调 `block_on_connect`；若返回 `ERROR_NO_DATA`（dead-on-arrival 连接），外层 `block_on_connect_clearing_empty_conns`（[listener.rs:154-161](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/listener.rs#L154-L161)）会先 `disconnect` 再重试。
4. **预期结果**：你能画出「`ConnectNamedPipe` 返回 0 → `true_val_or_errno` 报错 → `thunk_accept_error` 改写 → 外层按改写后的 `ErrorKind` 决定重试或上抛」的完整路径。
5. 待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `block_on_connect` 用 `!= 0` 而不是 `fd_or_errno` / `handle_or_errno`？

**参考答案**：`ConnectNamedPipe` 返回的是 `BOOL`（成功非 0，失败 0），不是 fd 也不是需要带出的 `HANDLE`（句柄早已由 `CreateNamedPipeW` 创建并保存在监听器里）。因此用 `!= 0` 产布尔、`true_val_or_errno(())` 丢弃成功值即可，没有「带出 fd/handle」的需求。

**练习 2**：`create_instance` 里 `.map(|h| OwnedHandle::from_raw_handle(h))` 的 `from_raw_handle` 是 unsafe 的，但它没有外层 `unsafe fn` 包裹（`create_instance` 本身是安全函数）。它是怎么满足 unsafe 规则的？

**参考答案**：`from_raw_handle` 的调用被包在那个 `unsafe { ... }` 块内部（块同时覆盖了 `CreateNamedPipeW` 调用和 `.map` 里的 `from_raw_handle`）。块前有 `// SAFETY:` 注释说明契约。安全函数体内出现显式 `unsafe` 块是完全合规的——「安全函数」只意味着「调用方无需满足特殊契约」，并不禁止函数体内部使用 `unsafe` 块来隔离真正不安全的操作。

---

### 4.5 unsafe lint 策略与边界

#### 4.5.1 概念说明

interprocess 作为一个大量使用 FFI 的库，对 unsafe 的纪律非常严格。它的 lint 策略分两处（承接 u1-3 的结论）：[Cargo.toml](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/Cargo.toml) 的 `[lints]` 对**整个 crate（含 examples/tests）**生效；[src/lib.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/lib.rs) 的 `#![warn(...)]` 只对**库本体**生效。

与 unsafe 最相关的是三条 `forbid`（`forbid` 比 `deny` 更强，连 `#[allow]` 也无法局部放开）：

- `unsafe_op_in_unsafe_fn = "forbid"`：`unsafe fn` 体内的 unsafe 操作**必须**再包一层显式 `unsafe { }`。
- `ptr_as_ptr = "forbid"`：禁止 `x as *const T` 式的指针 `as` 转换，必须用 `.cast()` / `.cast_mut()`。
- 此外还有 `deny` 级的整数 cast 警告（`cast_precision_loss`、`cast_possible_truncation`、`cast_possible_wrap`、`cast_sign_loss`），需要的地方用 `#[allow(...)]` + 注释说明理由。

#### 4.5.2 核心流程

这套 lint 反向塑造了代码形态：

1. **双层 unsafe**：所有 `unsafe fn` 的函数体里，真正的系统调用都再套一个 `unsafe { }`。如 `fcntl_int`、`getsockopt`。
2. **`.cast()` 取代 `as`**：所有指针类型转换走方法，如 `path.as_ptr().cast()`、`ref2ptr(&sa).cast_mut().cast()`。
3. **cast 加注释**：不可避免的整数截断/符号 cast 旁必有 `#[allow(clippy::cast_possible_truncation)] // safety contract` 或 `// bitwise comparison` 这类说明。

#### 4.5.3 源码精读

lint 策略定义在 [Cargo.toml:81-98](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/Cargo.toml#L81-L98)：

```toml
[lints.rust]
unsafe_op_in_unsafe_fn = "forbid"
# …
[lints.clippy]
ptr_as_ptr               = "forbid" # use .cast()
cast_possible_truncation = "warn"
cast_possible_wrap       = "warn"
cast_sign_loss           = "warn"
```

库本体的 `#![warn(...)]` 在 [src/lib.rs:4-10](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/lib.rs#L4-L10)，覆盖 `missing_docs`、`panic_in_result_fn`、`arithmetic_side_effects` 等。

对照看一个「双层 unsafe」实例——[src/os/unix/c_wrappers.rs:56-58](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/c_wrappers.rs#L56-L58) 的 `fcntl_int`：外层 `pub(super) unsafe fn` 声明「调用我需要 `fd` 是合法的借用 fd」，内层 `unsafe { libc::fcntl(...) }` 才是真正执行 FFI 调用的地方。如果没有内层那层 `unsafe`，`unsafe_op_in_unsafe_fn` 会直接 forbid 编译失败。

`.cast()` 的实例遍布全模块，例如 [src/os/windows/named_pipe/listener/create_instance.rs:74](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/listener/create_instance.rs#L74) 的 `ref2ptr(&sa).cast_mut().cast()`：先取 `&SECURITY_ATTRIBUTES` 的 `*const`，再 `.cast_mut()` 转可变、`.cast()` 抹平成 `CreateNamedPipeW` 要的 `*const VOID`。全程方法链，没有一个 `as`。

整数 cast 加注释的实例见 [src/os/unix/c_wrappers.rs:100-101](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/c_wrappers.rs#L100-L101)：`let orig_len = size_of::<T>() as socklen_t;` 旁有 `#[allow(clippy::cast_possible_truncation)] // safety contract`——因为 `size_of::<T>()` 是 `usize`，转 `socklen_t`（`u32`）在 64 位平台理论可能截断，作者用注释表明此处由调用方契约保证不溢出。

#### 4.5.4 代码实践（源码阅读型）

1. **目标**：验证「去掉内层 unsafe 会触发 forbid」。
2. **步骤**：在本地把 [src/os/unix/c_wrappers.rs:56-58](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/c_wrappers.rs#L56-L58) 的内层 `unsafe { }` 临时去掉，运行 `cargo clippy`（**注意：这只是观察实验，验证后务必还原，不要提交**）。
3. **现象**：clippy 会报 `unsafe_op_in_unsafe_fn` 的 forbid 错误，编译失败。
4. **预期结果**：你亲眼确认 `forbid` 比 `deny` 更严格——它不能用 `#[allow]` 在局部关闭。
5. 待本地验证（且务必还原改动，本讲禁止修改源码）。

#### 4.5.5 小练习与答案

**练习 1**：`forbid` 与 `deny` 的区别是什么？为什么 interprocess 对 `unsafe_op_in_unsafe_fn` 选 `forbid` 而非 `deny`？

**参考答案**：`deny` 可以被 `#[allow(...)]` 在更内层作用域局部关闭；`forbid` 一旦设定，任何 `#[allow]` 都无法关闭它（再 allow 反而会报错）。对一个 FFI 重度库而言，「unsafe fn 体内必须显式 unsafe 块」是希望**绝不破例**的硬纪律，选 `forbid` 能防止某处为了省事偷偷 allow 掉，从而保证每处 unsafe 操作都带着显式的、可审计的 `unsafe {}` 边界。

**练习 2**：`ptr_as_ptr = "forbid"` 想阻止什么样的写法？替代方案是什么？

**参考答案**：它阻止 `raw_ptr as *const U` / `as *mut U` 这种用 `as` 做指针类型转换的写法（`as` 转换静默、易错，且不链式）。替代方案是 `.cast::<U>()` / `.cast_mut()` / `.cast_const()`，它们是方法，可读性好、可链式，且类型推断更可控。本模块所有指针转换都走这条路。

---

## 5. 综合实践

> 选取 `ConnectNamedPipe` 或 `mkfifo` 的封装，逐行说明 `unsafe` 块的**前置条件**、**错误转换**、以及**如何满足 `forbid` 级别的 lint**。

下面给出一个「逐行解读模板」，先用它把 `block_on_connect`（`ConnectNamedPipe`）完整跑一遍，再请你用同一模板独立分析 `mkfifo` 封装。

### 模板（四列）

| 代码片段 | unsafe 前置条件 | 错误转换 | lint 合规 |

### 参考答案：`block_on_connect`

源码 [src/os/windows/named_pipe/listener.rs:163-167](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/listener.rs#L163-L167)：

| 代码片段 | unsafe 前置条件 | 错误转换 | lint 合规 |
|----------|-----------------|----------|-----------|
| `handle: BorrowedHandle<'_>` | 参数是借用句柄，调用方保证它指向一个有效的、由 `CreateNamedPipeW` 创建的服务端命名管道实例，且在调用期间存活。 | — | 借用句柄本身是安全类型，无需 unsafe。 |
| `unsafe { ConnectNamedPipe(handle.as_raw_handle(), ptr::null_mut()) }` | `ConnectNamedPipe` 要求句柄是「服务端命名管道实例、尚未连接」；`ptr::null_mut()` 表示同步（非 overlapped）等待。这些由 `accept` 调用链保证。 | — | `ConnectNamedPipe` 是 FFI 调用，包在显式 `unsafe {}` 内；`block_on_connect` 是**安全函数**，体内出现 unsafe 块合规。 |
| `!= 0` | — | `ConnectNamedPipe` 返回 `BOOL`，非 0 为成功、0 为失败。`!= 0` 即 4.1 的 `success(r)`。 | 布尔比较，无 cast。 |
| `.true_val_or_errno(())` | — | 成功返回 `Ok(())`；失败调 `io::Error::last_os_error()` 抓 `GetLastError()`。因紧贴 syscall、中间无其它 Win32 调用，抓到的码就是本次 `ConnectNamedPipe` 写入的。 | 调用安全库函数。 |
| `.or_else(thunk_accept_error)` | — | 把 `ERROR_PIPE_CONNECTED` 当成功、`ERROR_PIPE_LISTENING` 改写成 `WouldBlock`、其余原样上抛（见 4.2）。 | `thunk_accept_error` 用 `eeq` 比对错误码，无 unsafe。 |

**关键 lint 结论**：本函数没有 `as` 指针转换（`ptr::null_mut()` 是字面量，不是 cast），唯一 unsafe 操作 `ConnectNamedPipe` 被显式 `unsafe {}` 包裹，整数比较无截断——因此同时满足 `unsafe_op_in_unsafe_fn = forbid`、`ptr_as_ptr = forbid` 和 cast 警告族。

### 你的任务

用同一模板分析 [src/os/unix/fifo_file.rs:41-45](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/fifo_file.rs#L41-L45) 的 `_create_fifo`。需要说清：

1. **前置条件**：`CString::new(...)?` 保证了什么？`mkfifo` 对 `path` 与 `mode` 有何要求？
2. **错误转换**：`!= -1` 与 `true_val_or_errno(())` 分别扮演什么角色？errno 在何处被抓取？
3. **lint 合规**：`.as_ptr().cast()` 为什么不是 `as`？为何没有「双层 unsafe」？（提示：`_create_fifo` 不是 `unsafe fn`。）

完成后再挑战 `getsockopt`（4.3.3）：额外解释「闭包内 `assume_init` 为何安全」以及 `#[allow(clippy::cast_possible_truncation)]` 的必要性。

## 6. 本讲小结

- interprocess 用一套统一范式封装所有 FFI 系统调用：**先把任意返回值归一化成「成功为真」的布尔，再用 `OrErrno` 翻译成 `io::Result`**，把「errno 从哪来、何时抓」集中到一处。
- `OrErrno`（配 `ToBool`）是核心 trait；`FdOrErrno`/`HandleOrErrno` 是针对「返回 fd/handle、特殊值表失败」两种约定的特化入口，三者都最终落到 `io::Error::last_os_error()`。
- `RawOsErrorExt::eeq` 用按位比较弥合 `Option<i32>` errno 与 `u32` Win32 错误码常量的类型差，配合 `or_else`/`match` 做**错误码改写**（如 `ERROR_PIPE_CONNECTED→成功`、`ERROR_PIPE_LISTENING→WouldBlock`）。
- Unix 端用 `BorrowedFd`/`OwnedFd`，Windows 端用 `BorrowedHandle`/`OwnedHandle`，把 fd/句柄生命周期进类型系统；`getsockopt` 用 `MaybeUninit` + 闭包延迟 `assume_init`，从结构上保证只在成功路径读已初始化内存。
- `forbid` 级 lint 反向塑造代码：`unsafe_op_in_unsafe_fn = forbid` 强制「双层 unsafe」，`ptr_as_ptr = forbid` 强制 `.cast()`，整数 cast 必须配 `#[allow]` + 注释。
- `c_wrappers` 是「芯」里最贴近 OS 的一层，之上才是各平台 local socket / named pipe 后端逻辑；它**不暴露为公共 API**（`pub(crate)`/`pub(super)`），只服务于内部。

## 7. 下一步学习建议

- **横向对照错误体系**：本讲的 `OrErrno`/`RawOsErrorExt` 只解决「系统调用层」的错误。更上层的、消费型转换错误 `ConversionError`（归还所有权）在 u7-3「错误处理体系」讲解，建议对照阅读，看清「OS 错误 → `io::Error` → `ConversionError`」的层级。
- **深入句柄所有权**：`OwnedHandle::from_raw_handle`、`OwnedFd::from_raw_fd` 的安全前提、以及 `try_clone` / Windows `ShareHandle` 的克隆语义，见 u7-2「句柄/FD 抽象与所有权管理」。
- **看 unsafe 在更复杂场景的运用**：`linger_pool` 的低比特标记指针、`maybe_arc` 的 `ptr::read`/`ptr::write`（u8-1、u8-2）是 unsafe 在数据结构里的高阶用法，可作为本讲「unsafe 纪律」的进阶练习。
- **建议继续阅读的源码**：[src/os/unix/c_wrappers.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/c_wrappers.rs) 的 `poll`/`poll_loop`（含 `extern "C"` 声明 `ppoll`、NetBSD 的 `pollts` 别名）、[src/os/windows/named_pipe/c_wrappers.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/c_wrappers.rs) 的 `connect_without_waiting`（`CreateFileW` + `ERROR_PIPE_BUSY` 的 eeq 用法），都是巩固本讲范式的好材料。
