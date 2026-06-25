# 句柄/FD 传递与子进程通信

## 1. 本讲目标

上一讲（u5-l1）我们学会了用 `pipe()` 在**同一个进程内**创建匿名管道、用 `Sender`/`Recver` 读写。本讲要解决一个更关键的问题：**如何把这个管道的写端交给另一个进程**，从而让父子两个独立进程通过同一根管道对话。

具体地，学完本讲你应该能够：

1. 说清楚「句柄继承」是什么，以及为什么 interprocess 的匿名管道**默认就具备**跨进程传递能力。
2. 在代码层面完成 `Sender`/`Recver` 与标准库 `OwnedHandle`/`OwnedFd` 的**双向安全转换**，并知道是哪段源码（哪个宏）赋予了这种能力。
3. 用 `FromRawHandle`/`FromRawFd` 在子进程里**重建**一个 `Sender`/`Recver`，并准确说出这条路径为何是 `unsafe`、需要满足哪些不变式。
4. 设计一条「父进程创建管道 → 把句柄数值通过命令行参数/环境变量传给子进程 → 子进程重建并读写」的完整链路。

## 2. 前置知识

- **进程隔离**：现代操作系统里，每个进程有独立的内存空间。进程 A 不能直接读写进程 B 的变量。进程之间要共享数据，必须通过操作系统提供的 IPC 机制（管道、socket、共享内存等）。
- **句柄（Handle）/ 文件描述符（FD）**：操作系统对「一个打开的 I/O 资源」（文件、管道、socket）的不透明引用。在 Windows 上叫 handle，是一个不透明指针值；在 Unix 上叫 file descriptor（fd），是一个小整数。同一个底层资源，在每个进程里可能对应不同的数值。
- **句柄继承**：当一个进程启动子进程时，操作系统允许子进程「继承」父进程的一部分句柄。继承下来的句柄在父子两侧指向**同一个内核对象**，于是两端就能通过它通信。这正是匿名管道与子进程通信的根基。
- **Rust 的 I/O safety（1.63 起）**：标准库引入了 `OwnedHandle`/`OwnedFd`（拥有所有权的句柄）、`AsHandle`/`AsFd`（借出句柄）、`From<OwnedHandle>`/`From<OwnedFd>`（安全转换）等 trait，把原本裸露的句柄管理包进类型系统。而 `AsRawHandle`/`AsRawFd`（取出数值）、`FromRawHandle`/`FromRawFd`（由数值重建）则仍是 `unsafe`，因为它们绕过了所有权检查。

> 本讲会频繁出现「句柄」一词统称 Windows handle 与 Unix fd；涉及平台差异时会明确区分 `OwnedHandle`（Windows）与 `OwnedFd`（Unix）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [examples/unnamed_pipe/sync/side_a.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/examples/unnamed_pipe/sync/side_a.rs) | 「写端持有者」侧：创建管道、把 `Sender` 转成 `OwnedHandle`/`OwnedFd`、读回应答。 |
| [examples/unnamed_pipe/sync/side_b.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/examples/unnamed_pipe/sync/side_b.rs) | 「写端使用者」侧：接收一个 `OwnedHandle`/`OwnedFd`，重建 `Sender` 并写入。 |
| [examples/unnamed_pipe/sync/main.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/examples/unnamed_pipe/sync/main.rs) | 示例入口：用线程 + `mpsc` 通道把句柄从 side_a 传给 side_b（单进程内的简化演示）。 |
| [src/unnamed_pipe.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/unnamed_pipe.rs) | 公共 `Sender`/`Recver` 类型与 `pipe()` 函数。 |
| [src/macros/forward_handle_and_fd.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros/forward_handle_and_fd.rs) | 生成 `From<OwnedHandle/Fd>` 等安全句柄转换的宏。 |
| [src/macros/derive_raw.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros/derive_raw.rs) | 生成 `FromRawHandle`/`FromRawFd` 等 unsafe 裸句柄转换的宏。 |
| [src/os/windows/unnamed_pipe.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/unnamed_pipe.rs) | Windows 后端：`CreatePipe` 与默认 `inheritable = true`。 |
| [src/os/unix/unnamed_pipe.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/unnamed_pipe.rs) | Unix 后端：`pipe2`/`pipe` 系统调用，默认不带 `O_CLOEXEC`。 |

---

## 4. 核心概念与源码讲解

### 4.1 匿名管道为什么「天然」能跨进程：句柄继承

#### 4.1.1 概念说明

匿名管道（unnamed pipe）最大的特点是：**它没有名字**，唯一的访问途径就是持有它的句柄/FD。一旦某一端的句柄被关闭，那一端就再也找不回来。这听起来像个限制，但换个角度，它恰好是「与子进程通信」的最佳载体——因为子进程通信几乎总是从 `fork`/`exec` 或 `CreateProcess` 派生而来，而这类派生天然支持**句柄继承**。

于是形成一条干净的链路：

```
父进程 pipe() 得到 (Sender, Recver)
        │  把 Sender 的句柄标记为「可继承」并交给子进程
        ▼
子进程继承到同一个内核管道对象的写端
        │  父进程用 Recver 读，子进程用 Sender 写
        ▼
两端通过同一根管道通信
```

关键在于：interprocess **默认就把匿名管道的句柄设成可继承**。这不是使用者的责任，而是库在创建管道时就做好的。下一节我们直接去源码里找证据。

#### 4.1.2 核心流程

- 父进程调用 `pipe()`，得到一对可继承的句柄。
- 父进程启动子进程；操作系统把可继承的句柄复制进子进程的句柄表。
- 子进程拿到（可能数值不同的）句柄，但它指向**同一个内核管道**。
- 之后父子双方各自读写，互不可见对方的内存，却能交换字节。

注意：句柄**继承**只在进程**创建**时发生；已经运行的进程之间不能事后「赠送」句柄（那需要 Windows 的 `WSADuplicateSocket`/Unix 的 `SCM_RIGHTS` 等更复杂的机制，不属于本讲范畴）。

#### 4.1.3 源码精读

公共模块的文档把这件事说得很直白——句柄「默认可继承」，并提示用 `AsRawHandle`/`AsRawFd` 取数值、用 `FromRawHandle`/`FromRawFd` 重建：

[examples/unnamed_pipe/sync/side_a.rs:16-18](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/examples/unnamed_pipe/sync/side_a.rs#L16-L18) 旁边的注释解释了「可继承」这一前提：子进程之所以能拿到同一个句柄，正是因为句柄被标记为可继承。

证据一：Windows 后端。`CreationOptions` 构建器有一个 `inheritable` 字段，默认值就是 `true`：

[src/os/windows/unnamed_pipe.rs:28-40](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/unnamed_pipe.rs#L28-L40) —— 注意 `inheritable: bool` 字段及其文档「Specifies whether the resulting pipe can be inherited by child processes. The default value is `true`.」。

这个默认值落在 `new()` 里：

[src/os/windows/unnamed_pipe.rs:44-46](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/unnamed_pipe.rs#L44-L46) —— `Self { inheritable: true, ... }`。

`create()` 最终调用 Win32 的 `CreatePipe`，并把可继承性写进 `SECURITY_ATTRIBUTES.bInheritHandle`：

[src/os/windows/security_descriptor.rs:37-48](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/security_descriptor.rs#L37-L48) —— 第 46 行 `attrs.bInheritHandle = inheritable.to_i32();`，即 `bInheritHandle = 1`。这就是「子进程可继承」的 Windows 落脚点。

证据二：Unix 后端。`pipe2`/`pipe` 创建的 fd 默认**不**带 `FD_CLOEXEC`（关闭即执行）标志，因此默认可被子进程继承：

[src/os/unix/unnamed_pipe.rs:49-85](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/unnamed_pipe.rs#L49-L85) —— 第 56 行 `libc::pipe2(..., if nonblocking { libc::O_NONBLOCK } else { 0 })`，非阻塞时只传 `O_NONBLOCK`，没有 `O_CLOEXEC`，意味着 fd 在 `exec` 后仍然存在。注意 interprocess **不提供**关闭继承的开关（Unix 版 `CreationOptions` 无此字段），所以同步匿名管道的 fd 总是可继承的。

#### 4.1.4 代码实践

1. **实践目标**：确认两个平台后端「默认可继承」的事实。
2. **操作步骤**：
   - 阅读 [src/os/windows/unnamed_pipe.rs:44-46](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/unnamed_pipe.rs#L44-L46)，找到 `inheritable: true`。
   - 阅读 [src/os/unix/unnamed_pipe.rs:53-57](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/unnamed_pipe.rs#L53-L57)，确认 `pipe2` 的 flags 里没有 `O_CLOEXEC`。
3. **需要观察的现象**：Windows 靠 `bInheritHandle`、Unix 靠「不设 close-on-exec」，两者用不同机制达到同一目的。
4. **预期结果**：能用自己的话解释「为什么不需要任何额外参数，子进程就能继承匿名管道」。

#### 4.1.5 小练习与答案

**练习 1**：如果在 Unix 上希望阻止子进程继承某个管道 fd，应该怎么做？interprocess 提供了这个能力吗？

> **答案**：应给该 fd 设置 `FD_CLOEXEC`（关闭即执行）标志。interprocess 的同步匿名管道 API **不直接提供**关闭继承的选项；如确需关闭，可拿到 fd 后自行 `fcntl(F_SETFD, FD_CLOEXEC)`（标准库 `OsFdExt` 等途径）。

**练习 2**：Windows 的 `bInheritHandle` 和 Unix 的「不带 `O_CLOEXEC`」对应的是同一种语义吗？

> **答案**：是的。两者都是「允许句柄在子进程创建/执行时被继承」的正向开关，只是平台用各自的机制表达。interprocess 把这个共同默认值（可继承）藏在后端里，对使用者透明。

---

### 4.2 把 Sender/Recver 转成可传递的句柄：安全互转

#### 4.2.1 概念说明

要把管道端点交给子进程，第一步是**从 `Sender`/`Recver` 里取出底层句柄**。直接用 `unsafe` 的 `as_raw_handle()` 拿裸数值虽然可行，但更优雅、更安全的做法是：把 `Sender` **转换成**标准库的 `OwnedHandle`（Windows）/ `OwnedFd`（Unix）。

`OwnedHandle`/`OwnedFd` 是「拥有一个句柄」的类型——它和 `Sender` 一样会在 drop 时关闭句柄，但它是**平台无关的通用载体**，可以放心地在线程/进程边界间移动，也方便和标准库的其它 I/O 类型（`File`、`Child` 的 stdio 等）互转。

interprocess 为 `Sender`/`Recver` 实现了**双向**的安全转换：
- `From<Sender> for OwnedHandle`（把 `Sender` 变成裸句柄载体）
- `From<OwnedHandle> for Sender`（把裸句柄载体变回 `Sender`）

这套转换由声明式宏自动生成，使用者无需手写。

#### 4.2.2 核心流程

```
Sender  ──(From/Sender::into)──►  OwnedHandle / OwnedFd  ──(From/Sender::from)──►  Sender
   │                                                                        │
   └─ drop 时关闭句柄                                          drop 时同样关闭句柄 ─┘
```

转换是**按值移动**的，全程不复制底层句柄，也没有 `unsafe`。从 `Sender` 转成 `OwnedHandle` 后，原 `Sender` 不复存在（所有权移交）；反之亦然。

#### 4.2.3 源码精读

示例 side_a 正是用 `.into()` 把写端 `tx` 转成句柄载体的：

[examples/unnamed_pipe/sync/side_a.rs:14-18](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/examples/unnamed_pipe/sync/side_a.rs#L14-L18) —— 第 14 行 `let (tx, rx) = pipe()?;` 创建管道；第 18 行 `let txh = tx.into();` 把写端 `Sender` 转成 `Handle`（即 `OwnedHandle`/`OwnedFd`）。注释里专门指出：「`OwnedHandle` 和 `OwnedFd` 都实现了 `From<unnamed_pipe::Sender>`」。

`Handle` 类型别名是按平台条件定义的：

[examples/unnamed_pipe/sync/side_a.rs:3-6](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/examples/unnamed_pipe/sync/side_a.rs#L3-L6) —— `#[cfg(windows)] type Handle = os::windows::io::OwnedHandle;` 与 `#[cfg(unix)] type Handle = os::unix::io::OwnedFd;`。这正是本讲两个标准库 io 模块（`std::os::windows::io`、`std::os::unix::io`）的登场方式。

那这套 `From` 转换是哪里来的？答案在公共 `Sender`/`Recver` 的 `multimacro!` 调用里：

[src/unnamed_pipe.rs:93-101](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/unnamed_pipe.rs#L93-L101) —— `Sender` 上挂了 `forward_handle` 宏（外加 `forward_sync_write`、`forward_debug`、`derive_raw`）。`Recver` 同理见 [src/unnamed_pipe.rs:62-70](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/unnamed_pipe.rs#L62-L70)。

`forward_handle` 展开后会生成 `From<Sender> for OwnedHandle`（写方向）和 `From<OwnedHandle> for Sender`（读方向）：

[src/macros/forward_handle_and_fd.rs:26-46](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros/forward_handle_and_fd.rs#L26-L46) —— `forward_into_handle` 生成 `From<$ty> for OwnedHandle/OwnedFd`，内部 `From::from(x.0)`，即把转换委托给内部后端字段（`.0`），后者才是真正持有句柄的 `SenderImpl`/`RecverImpl`。

[src/macros/forward_handle_and_fd.rs:48-68](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros/forward_handle_and_fd.rs#L48-L68) —— `forward_from_handle` 生成反方向的 `From<OwnedHandle/OwnedFd> for $ty`，用 `Self(From::from(x))` 包回公共 newtype。这两条合起来就是「壳/芯」分层在句柄转换上的体现：公共 newtype 只转发，真正持句柄的是芯。

Windows 后端手写了这两条 impl 作为最终落地点（因为 Windows 的 `Sender` 还涉及 `ManuallyDrop` 与 limbo，不能纯靠宏）：

[src/os/windows/unnamed_pipe.rs:168-179](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/unnamed_pipe.rs#L168-L179) —— `From<OwnedHandle> for Sender` 把 `OwnedHandle` 装进 `ManuallyDrop` 并把 `needs_flush` 置为 `true`；`From<Sender> for OwnedHandle` 用 `ManuallyDrop::take` 取出内部句柄并转移所有权（注意这里**绕过了** Drop，避免触发 linger_pool，因为所有权已移交）。

#### 4.2.4 代码实践

1. **实践目标**：亲手走一遍「`Sender` → `OwnedHandle/Fd` → `Sender`」的安全往返。
2. **操作步骤**（示例代码，非项目原有代码）：

   ```rust
   use interprocess::unnamed_pipe::pipe;
   use std::io::Write;

   let (tx, _rx) = pipe()?;
   // 把 Sender 转成标准库句柄载体（平台条件）
   #[cfg(unix)]
   let handle: std::os::unix::io::OwnedFd = tx.into();
   #[cfg(windows)]
   let handle: std::os::windows::io::OwnedHandle = tx.into();
   // 再转回 Sender
   let mut tx = interprocess::unnamed_pipe::Sender::from(handle);
   tx.write_all(b"ping")?;
   ```

3. **需要观察的现象**：整个往返没有 `unsafe`，能正常编译运行。
4. **预期结果**：写出后能成功写入字节，证明所有权正确转移、句柄未被关闭。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `let txh = tx.into();` 之后，原变量 `tx` 不能再被使用？

> **答案**：`.into()` 消费（move）了 `tx`，把句柄所有权移交给了 `OwnedHandle`/`OwnedFd`。句柄只有一份，不可能同时被 `Sender` 和 `OwnedHandle` 拥有，否则会在两边各 drop 一次造成「双重关闭」。

**练习 2**：`forward_handle` 宏对 `Sender` 同时生成了哪两个方向的 `From`？

> **答案**：`From<Sender> for OwnedHandle/Fd`（取出句柄载体）与 `From<OwnedHandle/Fd> for Sender`（装回 newtype），分别对应 `forward_into_handle` 与 `forward_from_handle`。

---

### 4.3 在子进程重建 I/O 对象：FromRawHandle / FromRawFd

#### 4.3.1 概念说明

上一节的安全互转要求我们手里**已经有一个 `OwnedHandle`/`OwnedFd` 值**。但在真正的跨进程场景里，子进程拿到的不是这个 Rust 值，而是**一个数值**——父进程通过命令行参数或环境变量传过来的、句柄的编号。子进程必须用这个数值，凭空「重建」出一个 `Sender`。

这就是 `FromRawHandle`（Windows）/ `FromRawFd`（Unix）的用途：给定一个裸数值，构造出拥有该句柄的对象。**它们之所以是 `unsafe`，是因为编译器无法替你核对三件事**：

1. 这个数值确实是一个**有效的、打开的**句柄。
2. 调用者**拥有**这个句柄（有权关闭它），且没有别处也认为自己是所有者（避免双重关闭）。
3. 这个句柄的**类型/语义**与你要构造的对象匹配（比如别把一个 socket fd 当成管道 fd）。

只要这三点满足，重建就是安全的；违反任何一条，就是未定义行为。在「父进程显式传递 + 子进程独占使用」的场景下，这三点通常都成立，因此是合理的 `unsafe` 使用。

#### 4.3.2 核心流程

```
父进程: Sender ──as_raw_handle()──► 数值(如 0x3c) ──命令行/env──► 子进程
                                                                     │
子进程: unsafe { Sender::from_raw_handle(数值) } ◄──────────────────┘
```

关键事实：**同一句柄在父子两侧的数值相等**（这正是句柄继承的特性），所以父进程取出的数值，子进程可以直接用。interprocess 的注释明确强调了这一点。

> 在示例的单进程简化演示里，传递的是 `OwnedHandle` 值本身（走安全的 `From`），不是裸数值；裸数值 + `FromRaw` 这条真正跨进程的路径由注释文档说明，留给读者在「综合实践」里实现。

#### 4.3.3 源码精读

side_b 收到一个 `Handle`（`OwnedHandle`/`OwnedFd`），用 `from(handle)` 重建 `Sender`——这是**安全路径**：

[examples/unnamed_pipe/sync/side_b.rs:7-18](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/examples/unnamed_pipe/sync/side_b.rs#L7-L18) —— 第 16 行 `let mut tx = unnamed_pipe::Sender::from(handle);`。注释指出：在真实子进程里，`handle` 会是一个 `OwnedHandle`/`OwnedFd`，它由 `FromRawHandle`/`FromRawFd` 从数值构造而来；而这个数值「thanks to handle inheritance」与父进程那边相等。

[examples/unnamed_pipe/sync/side_b.rs:11-16](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/examples/unnamed_pipe/sync/side_b.rs#L11-L16) 旁边的注释把不变式说得很清楚：「数值可通过命令行参数传递，因为它在数值上等于父进程通过 `Owned{Handle,Fd}::from()` 得到的值——这得益于句柄继承。」

那 `FromRawHandle`/`FromRawFd` 对 `Sender` 的实现是哪来的？还是 `multimacro!` 里的 `derive_raw`：

[src/macros/derive_raw.rs:89-122](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros/derive_raw.rs#L89-L122) —— `derive_fromraw` 生成 `unsafe impl FromRawHandle for $ty`（与 Unix 的 `FromRawFd`）。注意第 98-99 行的实现：先 `unsafe { FromRawHandle::from_raw_handle(fd) }` 造一个 `OwnedHandle`，再 `From::from(h)` 装回 newtype。也就是说，**裸路径 = 先用 unsafe 造 `OwnedHandle`，再走上一节的安全 `From`**——两条路径在这里汇合。

[src/macros/derive_raw.rs:124-137](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros/derive_raw.rs#L124-L137) —— `derive_raw` 把 `derive_asintoraw`（`AsRawHandle`/`IntoRawHandle`）与 `derive_fromraw`（`FromRawHandle`）打包，于是 `Sender`/`Recver` 同时具备取数值、按数值重建两套能力。

取数值（`AsRawHandle`/`AsRawFd`）的生成见 [src/macros/derive_raw.rs:4-37](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros/derive_raw.rs#L4-L37)：它先 `as_handle()` 借出安全句柄，再取其 raw 值——可见 interprocess 的 raw 能力建立在安全 I/O 之上。

#### 4.3.4 代码实践

1. **实践目标**：在同进程内手动走一遍 unsafe 裸路径，理解它等价于安全路径。
2. **操作步骤**（示例代码，非项目原有代码）：

   ```rust
   use std::os::unix::io::{AsRawFd, FromRawFd, OwnedFd};
   use interprocess::unnamed_pipe::{pipe, Sender};
   use std::io::Write;

   let (tx, _rx) = pipe()?;
   // 取裸数值
   let raw = tx.as_raw_fd();
   // 用 ManuallyDrop 阻止 tx 的 drop 关闭句柄，模拟「所有权已移交别处」
   let tx = std::mem::ManuallyDrop::new(tx);
   // unsafe 重建
   let owned: OwnedFd = unsafe { OwnedFd::from_raw_fd(raw) };
   let mut tx2: Sender = owned.into();
   tx2.write_all(b"raw path ok")?;
   ```

3. **需要观察的现象**：若不使用 `ManuallyDrop`，`tx` 和 `tx2` 会在各自 drop 时各关闭一次同一个 fd，造成双重关闭；用 `ManuallyDrop` 才能模拟「所有权唯一」。
4. **预期结果**：写入成功，且程序正常退出无「Bad file descriptor」类错误。**待本地验证**：不同平台/版本的 stdout 缓冲可能影响观察。
5. **注意**：这是为理解机制而写的最小演示；真实场景请优先用 4.2 的安全 `.into()`，只有在跨越进程边界拿到裸数值时才用 `FromRawFd`。

#### 4.3.5 小练习与答案

**练习 1**：`derive_fromraw` 生成的 `FromRawHandle` 实现里，为什么不直接构造 newtype，而是先造 `OwnedHandle` 再 `From::from`？

> **答案**：复用安全路径。这样 unsafe 的范围被压缩到「由数值造 `OwnedHandle`」这一步，之后转回 newtype 用的是已验证的安全 `From<OwnedHandle> for Sender`。既减少重复代码，也把 unsafe 边界收窄。

**练习 2**：下列代码有何错误？`let raw = tx.as_raw_fd(); let tx2 = unsafe { Sender::from_raw_fd(raw) };`（`tx` 仍存活）。

> **答案**：`tx` 没有被消耗，它仍认为自己是 fd 的所有者；现在 `tx` 和 `tx2` 同时「拥有」同一个 fd，二者 drop 时会双重关闭，属于未定义行为。正确做法是先转移所有权（`tx.into()` 成 `OwnedFd`，或用 `ManuallyDrop` 阻止原 `tx` 关闭），再重建。

---

### 4.4 把句柄数值真正送达子进程：传递机制

#### 4.4.1 概念说明

到目前为止，我们解决了「取句柄」「重建句柄」两端，中间还差一步：**怎么把数值从父进程送到子进程**。interprocess 的设计哲学是——**它不关心你怎么传**。库只保证句柄可继承、并提供取值/重建的 trait；传输通道由使用者自行选择，常见的有：

- **命令行参数**：把数值转成字符串塞进 `argv`。
- **环境变量**：同理塞进子进程环境。
- **stdin**：若子进程 stdio 已被接管，可借道。
- **平台特定机制**：如 Unix `SCM_RIGHTS`、Windows `WSADuplicateSocket`（用于已运行进程，超出本讲）。

仓库示例为了**平台无关**，没有真正 `spawn` 子进程，而是用「线程 + `mpsc` 通道」在**同一个进程内**模拟这条链路：`mpsc` 通道扮演「传递机制」的角色，把 `OwnedHandle` 值从 side_a 送到 side_b。理解了这个简化，你就能把 `mpsc` 替换成「`Command` + 命令行参数」，做出真正的跨进程版本。

#### 4.4.2 核心流程

示例的实际数据流（单进程内）：

```
main 线程
  │ 1) spawn 线程跑 side_a
  ▼
side_a 线程: pipe() → (tx, rx); tx.into() → txh(OwnedHandle/Fd)
  │ 2) mpsc.send(txh)            ◄── mpsc 通道 = 传递机制（真实场景换成命令行/env）
  ▼
main 线程: hrx.recv() → handle; 3) side_b::emain(handle)
  │
  ▼
side_b: Sender::from(handle) → tx; tx.write_all("Hello from side B!")
  │ 4) 字节经管道回流
  ▼
side_a: rx.read_line() → "Hello from side B!"  ✓ assert 通过
```

注意角色：side_a 创建管道、**保留读端 `rx`**、把**写端**交给 side_b；side_b 拿写端发消息，side_a 读回。命名上「a/b」不等于「读/写」，要按代码看。

#### 4.4.3 源码精读

入口组装三段：

[examples/unnamed_pipe/sync/main.rs:6-13](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/examples/unnamed_pipe/sync/main.rs#L6-L13) —— 第 7 行创建容量 1 的 `mpsc::sync_channel`；第 8 行 spawn 线程跑 side_a 并把发送端 `htx` 交给它；第 9 行 main 线程 `recv()` 拿到句柄；第 11 行调用 side_b；第 12 行 `join` 等待 side_a（它要读回应答）。

side_a 把写端转换后送出：

[examples/unnamed_pipe/sync/side_a.rs:19-30](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/examples/unnamed_pipe/sync/side_a.rs#L19-L30) —— 第 19-23 行注释说明真实场景应「通过命令行参数或 stdin 传给子进程」，并点明「这之所以行得通，是因为子进程继承句柄」；第 25 行 `handle_sender.send(txh)` 是示例用的简化传递；第 29-30 行用 `BufReader` 按行读回应答。

[examples/unnamed_pipe/sync/side_a.rs:28-32](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/examples/unnamed_pipe/sync/side_a.rs#L28-L32) —— 第 32 行 `assert_eq!(buf.trim(), "Hello from side B!");` 验证通信成功。

side_b 重建并写入：

[examples/unnamed_pipe/sync/side_b.rs:11-21](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/examples/unnamed_pipe/sync/side_b.rs#L11-L21) —— 第 16 行 `Sender::from(handle)` 重建写端；第 18 行 `tx.write_all(b"Hello from side B!\n")` 发送。

运行方式（示例用 `[[example]]` 显式声明，见 [Cargo.toml:114-119](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/Cargo.toml#L114-L119)）：

```bash
cargo run --example unnamed_pipe_sync
```

预期输出：无报错、断言通过（程序静默成功退出）。

#### 4.4.4 代码实践

1. **实践目标**：跑通仓库自带的简化示例，确认「转换 + 传递 + 重建 + 回读」整条链路可用。
2. **操作步骤**：
   - 在仓库根目录执行 `cargo run --example unnamed_pipe_sync`。
3. **需要观察的现象**：程序正常退出（exit code 0），无 panic、无 `assert` 失败。
4. **预期结果**：因为示例只在单进程内用线程传递 `OwnedHandle`，应总是成功。若失败，多半是环境（如 tokio feature 误开）问题。
5. **待本地验证**：在不同平台上的退出码一致。

#### 4.4.5 小练习与答案

**练习 1**：示例为什么用 `mpsc::sync_channel(1)` 而不是真正的子进程？

> **答案**：为了让示例**平台无关**且自包含。真正的子进程需要 `Command`、命令行参数序列化、平台特定的句柄继承配置（Windows 还要处理 stdio），这会让示例失去通用性。`mpsc` 通道忠实扮演了「传递机制」这一抽象角色，把跨进程问题降维成跨线程问题来演示。

**练习 2**：如果把 side_a 改成「保留写端 `tx`、把读端 `rx` 传给 side_b」，side_b 该如何改？

> **答案**：side_b 需要把 `Sender::from(handle)` 换成 `Recver::from(handle)`，并把 `write_all` 换成 `read`（用 `BufReader` 更顺）。`Recver` 与 `Sender` 一样由 `forward_handle` + `derive_raw` 提供句柄互转，所以重建方式完全对称。

---

## 5. 综合实践

**任务**：把仓库的单进程示例改造成**真正的「父进程 → 子进程」demo**，用命令行参数（或环境变量）传递句柄数值。

要求：

1. 写两个二进制（或在同一程序里按子命令分支）：
   - **父模式**（`--parent`）：调用 `pipe()` 得到 `(tx, rx)`；用 `as_raw_handle()`/`as_raw_fd()` 取写端数值；用 `std::process::Command` 启动自身的子模式，把数值通过命令行参数传过去（并确保写端不被父进程 drop，可用 `ManuallyDrop` 或先把 `tx` 转成 `OwnedHandle` 后用 `mem::forget`/转交）；父进程用 `rx` 读取一行并打印。
   - **子模式**（`--child <数值>`）：解析数值，用 `unsafe { Sender::from_raw_handle(数值) }`（或 `from_raw_fd`）重建写端，写入 `b"Hello from child!\n"`。
2. 关键检查点：
   - 子进程能继承到同一个句柄（数值相等）。
   - 父进程在 spawn 后**不要关闭**写端，否则管道写端全关、读端立即收到 EOF（参见 u5-l1 提到的「写端全部关闭 → read 返回 `Ok(0)`」）。
   - Windows 下注意：若写端 drop 会进入 limbo（后台刷新），可能影响时序，详见 u8-l1。
3. 思考题：为什么必须保证「至少一个写端存活」父进程才能读到数据？如果父进程把唯一的写端也 `forget` 掉、只让子进程持有写端，读端会在什么时刻收到 EOF？

**参考实现骨架**（示例代码，非项目原有代码）：

```rust
use interprocess::unnamed_pipe::{pipe, Sender};
use std::io::{Read, Write};

fn main() -> std::io::Result<()> {
    let args: Vec<String> = std::env::args().collect();
    if let Some(raw) = args.get(2) {
        // —— 子模式 ——
        #[cfg(unix)]
        let tx: Sender = unsafe {
            use std::os::unix::io::FromRawFd;
            Sender::from_raw_fd(raw.parse().unwrap())
        };
        #[cfg(windows)]
        let tx: Sender = unsafe {
            use std::os::windows::io::FromRawHandle;
            Sender::from_raw_handle(raw.parse().unwrap())
        };
        let mut tx = tx;
        tx.write_all(b"Hello from child!\n")?;
        return Ok(());
    }

    // —— 父模式 ——
    let (tx, mut rx) = pipe()?;
    #[cfg(unix)]
    let raw = std::os::unix::io::AsRawFd::as_raw_fd(&tx);
    #[cfg(windows)]
    let raw = std::os::windows::io::AsRawHandle::as_raw_handle(&tx);
    // 阻止父进程 drop 写端（所有权概念上已交给子进程）
    let tx = std::mem::ManuallyDrop::new(tx);

    std::process::Command::new(&args[0])
        .arg("--child")
        .arg(raw.to_string())
        .status()?;

    let mut buf = String::new();
    rx.read_to_string(&mut buf)?;
    print!("{buf}");
    Ok(())
}
```

> 说明：上面骨架用 `ManuallyDrop` 模拟「写端所有权移交给子进程」。在生产代码里更稳妥的做法是先把 `tx` 转成 `OwnedHandle`/`OwnedFd` 并保证子进程真正继承它（Windows 上 `CreateProcess` 的 `bInheritHandles`、Unix 上 fd 不带 `O_CLOEXEC`），再在父侧释放。完整正确性**待本地验证**，重点在于理解链路而非一键跑通。

## 6. 本讲小结

- 匿名管道「无名、只能凭句柄访问」的特性，使它与「子进程继承句柄」机制天然契合；interprocess 默认就把同步匿名管道的句柄设为**可继承**（Windows `bInheritHandle = 1`，Unix 不设 `O_CLOEXEC`）。
- `Sender`/`Recver` 与标准库 `OwnedHandle`/`OwnedFd` 之间有**双向安全转换**，由 `forward_handle` 宏生成的 `From` impl 提供，按值移动、无 `unsafe`。
- 在子进程一侧拿到的是**句柄数值**，需用 `FromRawHandle`/`FromRawFd`（`derive_raw` 宏生成，`unsafe`）重建；其不变式是「数值有效、所有权唯一、语义匹配」。由于句柄继承，父子两侧数值相等。
- interprocess **不规定**如何把数值送达子进程；命令行参数、环境变量、stdin 均可。仓库示例为保持平台无关，用「线程 + `mpsc` 通道」单进程内模拟了这条链路。
- 真正的跨进程版本只需把示例里的 `mpsc` 通道替换为 `Command` + 数值传递，并注意「写端存活」以避免读端过早 EOF。

## 7. 下一步学习建议

- **u6（Tokio 异步集成）**：本讲聚焦同步 unnamed pipe；若你要在异步运行时里与子进程通信，应接着学 `unnamed_pipe::tokio` 与 `traits::tokio`，注意异步版的句柄/FD 同样可继承、可重建，只是读写改走 `AsyncRead`/`AsyncWrite`。
- **u7-l2（句柄/FD 抽象与所有权管理）**：想深入 `OwnedHandle`/`OwnedFd`/`BorrowedHandle` 的所有权模型、`TryClone` 与 Windows 的 `ShareHandle`/`AsHandle` 体系，可进入专家层。
- **u8-l1（linger_pool）**：本讲多次提到「Windows 写端 drop 不立即清空缓冲（limbo）」，其内部清理机制（持久线程 + 水位队列）在专家层详述；做跨进程 demo 遇到「子进程写完退出、父进程读到一半」的时序问题时值得参考。
- **u9-l1（unsafe FFI 封装层）**：若你想彻底搞清 `FromRawHandle` 这类 unsafe 边界如何与 crate 的 `unsafe_op_in_unsafe_fn = forbid` lint 策略相容，可阅读 c_wrappers 与安全策略讲义。
