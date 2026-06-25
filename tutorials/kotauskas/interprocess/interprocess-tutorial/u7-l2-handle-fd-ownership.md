# 句柄/FD 抽象与所有权管理

## 1. 本讲目标

本讲承接 u2-l3 建立的「壳/芯」分层与 u5-l1、u5-l2 的句柄传递主题，钻进一个贯穿全库却常被忽略的基础设施：**句柄（Handle）/ 文件描述符（FD）的所有权与克隆**。

interprocess 的几乎所有 I/O 类型都包裹着一个 OS 对象——Windows 上是 `HANDLE`，Unix 上是 fd。这些对象有**所有权**（谁负责关闭它）、有**可克隆性**（能否复制出第二个指向同一内核对象的引用）、还有**进程隶属**（一个句柄值只在创建它的进程里有意义）。本讲就是把这三件事在源码层面讲清楚。

学完本讲，你应当能够：

1. 说出 `TryClone` trait 为什么是「可失败的」，它与 OS 的 `DuplicateHandle`/`dup` 系统调用是什么关系，以及它为何能作为跨平台的统一克隆抽象。
2. 区分 `OwnedHandle`/`OwnedFd`（拥有，drop 时关闭）与 `BorrowedHandle`/`BorrowedFd`（借用，不关闭）两种所有权形态，并理解 newtype 如何经 `forward_handle` 宏在它们之间安全转换。
3. 理解 Windows 专有的 `ShareHandle` trait：为何「把句柄数值发给另一个进程」行不通，`DuplicateHandle` 如何把句柄复制进目标进程的句柄表。
4. 读懂 Windows 内部的 `AdvOwnedHandle`：它如何用「低比特标记 + 零位 niche」在 `OwnedHandle` 之上叠加额外状态而不增加内存开销。
5. 认识一个贯穿全库的约定：**克隆/共享出来的句柄一律是不可继承的**，这与原始管道句柄「默认可继承」形成对照。

---

## 2. 前置知识

### 2.1 句柄与文件描述符是什么

在 Windows 上，内核对象（文件、管道、事件、进程……）由一个不透明值 `HANDLE`（本质是指针大小的整数）代表；在 Unix 上，同类资源由一个小整数 **文件描述符（fd）** 代表。二者都是**进程局部**的：同一个内核对象，在进程 A 里可能是句柄值 `0x120`，在进程 B 里可能是句柄值 `0x88`，互不通用。这一点是本讲 `ShareHandle` 存在的根本原因。

### 2.2 Rust 的 I/O 安全（Rust 1.63+）

标准库为句柄/FD 提供了一套所有权类型，对应关系如下：

| Windows | Unix | 语义 |
|---|---|---|
| `OwnedHandle` | `OwnedFd` | **拥有**该句柄/fd，`Drop` 时调用 `CloseHandle`/`close` |
| `BorrowedHandle<'a>` | `BorrowedFd<'a>` | **借用**该句柄/fd，生命周期绑定到某个 owner，不负责关闭 |
| `RawHandle`（`*mut c_void`） | `RawFd`（`c_int`） | 裸数值，无所有权、无生命周期，操作它需 `unsafe` |

围绕它们还有四个 trait：`AsHandle`/`AsFd`（借出 `Borrowed`）、`IntoRawHandle`/`IntoRawFd`（交出裸值、放弃所有权）、`FromRawHandle`/`FromRawFd`（用裸值构造，`unsafe`）、`AsRawHandle`/`AsRawFd`（取裸值但不交出所有权，`unsafe` 在于调用者要保证用法正确）。

> 关键直觉：`Owned*` 拥有、`Borrowed*` 借用、`Raw*` 是无类型的裸数值。interprocess 的 newtype 壳内部字段几乎总是 `Owned*`（或其变体），把「关闭」的责任封装在类型里。

### 2.3 克隆一个 OS 对象意味着什么

复制一个 `OwnedHandle` **不是**复制那个整数——那只会造出两个都认为自己「拥有」同一句柄的值，drop 时双重关闭。真正的「克隆」是请求内核**再分配一个独立的句柄/fd，指向同一个底层内核对象**：Windows 用 `DuplicateHandle`，Unix 用 `dup`/`fcntl(F_DUPFD_CLOEXEC)`。这两个调用都可能失败（资源耗尽等），这正是 `TryClone` 要「Try」的原因。

### 2.4 与已学讲义的衔接

- u2-l3 讲过 `impmod!` 把后端类型注入公共 newtype，本讲的 `Sender`/`Recver` 仍是这套壳/芯结构。
- u5-l1、u5-l2 讲过管道句柄**默认可继承**、可经数值传给子进程；本讲会指出 `try_clone()` 出来的副本恰恰**不可继承**，二者对照。
- u7-l1 讲过 `forward_*` 与 `derive_*` 两类宏；本讲的 `forward_try_clone`、`forward_handle` 正是其中的转发宏实例。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [src/try_clone.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/try_clone.rs) | 定义全库唯一的可失败克隆 trait `TryClone`，并为所有 `Clone` 类型提供 blanket impl。 |
| [src/os/windows/share_handle.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/share_handle.rs) | Windows 专有 trait `ShareHandle`：把句柄复制进**另一个进程**的句柄表，返回可在对端使用的裸值。 |
| [src/os/windows/adv_handle.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/adv_handle.rs) | `AdvOwnedHandle`：在 `OwnedHandle` 之上叠加「低比特标记 + 零 niche」的增强句柄类型。 |
| [src/os/windows/c_wrappers.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/c_wrappers.rs) | Windows 底层封装：`duplicate_handle`（本进程内复制）、`duplicate_handle_to_foreign`（复制到指定进程）。 |
| [src/os/unix/c_wrappers.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/c_wrappers.rs) | Unix 底层封装：`duplicate_fd`，用 `F_DUPFD_CLOEXEC`/`dup`+`FD_CLOEXEC` 复制 fd。 |
| [src/macros/forward_try_clone.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros/forward_try_clone.rs) | 为 newtype 自动转发 `TryClone` 的声明式宏。 |

辅助佐证文件（用于看清 trait 如何落地到具体类型）：`src/os/unix/fdops.rs`、`src/os/unix/unnamed_pipe.rs`、`src/os/windows/unnamed_pipe.rs`、`src/os/windows/named_pipe/stream/impl/handle.rs`、`src/os/unix/uds_local_socket/stream.rs`、`src/local_socket/stream/enum.rs`。

---

## 4. 核心概念与源码讲解

### 4.1 TryClone：可失败的句柄克隆

#### 4.1.1 概念说明

`Clone` 是 Rust 标准库里「不可失败的克隆」：`fn clone(&self) -> Self`。它假设克隆永远不会出错——这对纯内存数据结构成立，但对 OS 对象不成立。复制一个句柄/fd 要向内核申请一个新表项，而内核可能因为**句柄表已满、内存不足、权限不够**等原因拒绝。

如果把这种操作塞进 `Clone`，就只能用 `panic` 或 `abort` 处理失败，这显然不合适。于是 interprocess 自定义了一个可失败版本——`TryClone`，返回 `io::Result<Self>`。它的文档注释把动机说得很直白：

> The `DuplicateHandle`/`dup` system calls can fail for a variety of reasons, most of them being related to system resource exhaustion.

`TryClone` 是 interprocess 的**跨平台统一克隆抽象**：用户代码写 `stream.try_clone()`，不必关心底层是 `DuplicateHandle` 还是 `dup`。这正呼应了 u2-l1 的「trait 定义接口、各平台后端落地」哲学。

#### 4.1.2 核心流程

`TryClone` 的使用流程是一条从公共类型到内核的下行链路：

1. 用户对 `Stream`（公共枚举）或 `Sender`/`Recver`（公共 newtype）调用 `try_clone()`。
2. 公共壳把调用转发给内部字段（芯）——枚举走 `dispatch!`，newtype 走 `forward_try_clone!` 宏。
3. 芯（`FdOps`/`AdvOwnedHandle`/`UnixStream`）调用平台 `c_wrappers` 的复制函数。
4. `c_wrappers` 执行真正的系统调用（`DuplicateHandle`/`dup`/`F_DUPFD_CLOEXEC`），失败则经 `OrErrno` 转 `io::Error`。
5. 成功则把新句柄/fd 包回成同类型返回。

注意一个关键约定：**无论哪个平台，复制出来的副本都是不可继承的**——Unix 走 `F_DUPFD_CLOEXEC`（或 `dup` 后补 `FD_CLOEXEC`），Windows 的 `DuplicateHandle` 传 `bInheritHandle = 0`。这与 u5-l1 讲的「原始管道句柄默认可继承」恰好相反，是本讲要强调的所有权差异之一。

#### 4.1.3 源码精读

trait 本体极简，外加一个 blanket impl：

[src/try_clone.rs:7-13](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/try_clone.rs#L7-L13) 定义 `TryClone`，并对所有 `T: Clone` 提供「直接 `Ok(self.clone())`」的实现。这个 blanket impl 很重要：它让任何已经实现 `Clone` 的类型自动获得 `TryClone`，于是需要 `T: TryClone` 约束的泛型代码也能接受普通的 `Clone` 类型。它在 [src/lib.rs:68-69](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/lib.rs#L68-L69) 以 `pub use try_clone::*;` 导出为 crate 顶层公共项。

newtype 如何获得 `TryClone`？靠转发宏 `forward_try_clone!`：

[src/macros/forward_try_clone.rs:3-12](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros/forward_try_clone.rs#L3-L12) 生成形如 `impl TryClone for $ty { fn try_clone(&self) -> io::Result<Self> { Ok(Self(self.0.try_clone()?)) } }` 的实现——把克隆委托给内部字段 `.0`，再把结果重新包回壳。这和 u7-l1 讲的 `forward_*` 转发宏同源：前提是**芯已实现 `TryClone`**。

Unix 后端的芯 `FdOps`（包着 `OwnedFd`）手写了 `TryClone`，落到 `duplicate_fd`：

[src/os/unix/fdops.rs:55-60](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/fdops.rs#L55-L60) 调用 `c_wrappers::duplicate_fd`。而 [src/os/unix/c_wrappers.rs:112-126](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/c_wrappers.rs#L112-L126) 就是真正的系统调用封装：在有原子 `cloexec` 的平台上用 `F_DUPFD_CLOEXEC`（一步完成「复制 + 设不可继承」），否则先 `dup` 再 `fcntl(F_SETFD, FD_CLOEXEC)` 两步完成——这就是「副本不可继承」的 Unix 落点。

公共 newtype `Recver`/`Sender` 则经 `multimacro!` 套上转发宏，以 Unix 版为例：

[src/os/unix/unnamed_pipe.rs:98-105](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/unnamed_pipe.rs#L98-L105) 对 `Recver` 批量应用宏，其中 `forward_try_clone` 即负责把 `FdOps` 的 `TryClone` 搬到 `Recver` 上；紧接着的 `Sender`（L115-122）同理。

最上层，公共 `local_socket::Stream` 枚举用 `dispatch!` 把 `try_clone` 转给当前平台后端：

[src/local_socket/stream/enum.rs:140-144](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/stream/enum.rs#L140-L144) 对枚举 `Stream` 实现 `TryClone`，单分支 `match`（另一平台变体被 `#[cfg]` 排除，编译期消解）把调用转发给后端 `Stream`，再用 `From` 把后端类型包回公共枚举。后端 UDS `Stream` 的实现见 [src/os/unix/uds_local_socket/stream.rs:138-141](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/uds_local_socket/stream.rs#L138-L141)，它直接复用标准库 `UnixStream::try_clone()`（标准库内部也是 `dup`）。

#### 4.1.4 代码实践

**实践目标**：验证 `try_clone()` 返回的是指向同一底层连接的**独立句柄**，且克隆可失败。

**操作步骤**（示例代码，非项目原有文件）：

```rust
// 示例代码：演示 TryClone 行为
use interprocess::local_socket::{prelude::*, ListenerOptions, Stream};
use std::io::{Read, Write};

let name = "try-clone-demo".to_ns_name()?;
// 起一个本地服务端（同步）
let listener = ListenerOptions::new().name(name.clone()).create_sync()?;

let handle = std::thread::spawn(move || {
    let mut conn = listener.incoming().next().unwrap()?;
    let mut buf = [0u8; 5];
    conn.read_exact(&mut buf)?;          // 读取客户端写来的 5 字节
    println!("server read: {:?}", buf);
    conn.write_all(b"pong")?;            // 回写
    let _ = conn;                         // 服务端连接在此 drop
    Ok::<_, std::io::Error>(())
});

let mut a = Stream::connect(name.clone())?;
let mut b = a.try_clone()?;               // ← 本讲主角：克隆出第二个句柄
assert!(b.try_clone().is_ok());           // 副本还能继续克隆

a.write_all(b"hello")?;                   // 用 a 写
drop(a);                                  // 关掉 a，但 b 仍指向同一连接
let mut resp = String::new();
b.read_to_string(&mut resp)?;             // 用克隆出的 b 读回响应
println!("client got: {resp}");
handle.join().unwrap()?;
```

**需要观察的现象**：`a` 被 drop 后，`b` 仍然能读到服务端的 `pong`——这说明 `b` 与 `a` 指向**同一底层连接**，关闭其一不影响另一个（直到所有副本都关闭，连接才真正关闭）。

**预期结果**：打印 `server read: [104, 101, 108, 108, 111]`（`"hello"`）与 `client got: pong`。

**待本地验证**：以上为示例代码，未在本环境编译运行；请在本机启用相应 feature 后用 `cargo` 验证。若把 `try_clone` 换成「直接赋值整数」会因双重 close 或对端无响应而失败——这正是 `TryClone` 存在的意义。

#### 4.1.5 小练习与答案

**练习 1**：`TryClone` 为什么不直接复用标准库的 `Clone`？

> **答**：`Clone::clone` 返回 `Self`（不可失败），而复制 OS 句柄/fd 的系统调用（`DuplicateHandle`/`dup`）可能因资源耗尽等失败。把可失败操作塞进不可失败签名只能 `panic`，故另立返回 `io::Result<Self>` 的 `TryClone`。

**练习 2**：blanket impl `impl<T: Clone> TryClone for T` 有什么用？

> **答**：它让任何 `Clone` 类型自动满足 `TryClone`，于是写 `T: TryClone` 约束的泛型函数既能接受真正的句柄类型（手写的 `TryClone`），也能接受普通 `Clone` 数据，无需为后者再写一份实现。

---

### 4.2 ShareHandle：跨进程共享句柄（Windows）

#### 4.2.1 概念说明

u5-l2 讲过把句柄数值传给子进程：因为句柄**可继承**，父子两侧的数值恰好相同，子进程可直接用。但这套机制有两个限制——它**只适用于父子关系**，且要求句柄在创建时就标记为可继承。

如果两个进程**没有**父子关系（比如两个独立启动的程序），或者句柄不是可继承的，还能共享吗？能，但要走另一条路：`DuplicateHandle`。这个 Win32 API 的特别之处在于，它把句柄复制进**指定进程**的句柄表——接收方用「它自己进程的一个句柄」来指代。复制完成后，目标进程拿到一个**对它自己有效的**新句柄值，原进程则拿到这个新值的**裸数值**，可以经任意 IPC（典型如 named pipe）发过去。

`ShareHandle` trait 就是把这套流程封装成安全接口。注意它**仅 Windows 存在**——Unix 的 fd 在进程间没有等价的「复制进目标进程表」的通用 syscall（Unix 用 `sendmsg` 的辅助消息传递 fd，是另一套机制，interprocess 未封装）。

#### 4.2.2 核心流程

`share(receiver)` 的工作流：

1. 调用方持有一个 `AsHandle` 的对象（如 `Sender`/`Recver`），并持有「目标进程」的一个句柄（通常是对端进程由 `OpenProcess` 拿到的 `BorrowedHandle`）。
2. `share` 内部调用 `DuplicateHandle`，参数：源进程=当前进程、源句柄=自己的句柄、**目标进程=receiver**、`bInheritHandle=0`、`DUPLICATE_SAME_ACCESS`。
3. 内核在 receiver 的句柄表里新建一个表项，把新句柄值写回调用方提供的指针。
4. `share` 返回这个新句柄的**裸数值** `RawHandle`。
5. 调用方把这个数值通过任意通道发给 receiver 进程；receiver 用 `FromRawHandle`（`unsafe`）重建 I/O 对象。

约定（见 trait 文档）：产物**不应是可继承**的——它是一个逻辑错误但不是未定义行为（UB）。这与 4.1 节「副本不可继承」一脉相承。

#### 4.2.3 源码精读

trait 定义与默认方法：

[src/os/windows/share_handle.rs:21-34](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/share_handle.rs#L21-L34) 声明 `ShareHandle: AsHandle`，其 `share` 方法体（L31-33）只有一行——委托给 `c_wrappers::duplicate_handle_to_foreign`。文档点明这是「非父子关系下共享句柄的唯一方式」，且「不需要 unsafe，因为 `DuplicateHandle` 在 `lpTargetHandle` 是合法指针时不会导致 UB，只会产生错误」。

为谁实现？匿名管道的两端：

[src/os/windows/share_handle.rs:35-36](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/share_handle.rs#L35-L36) 为公共 `Recver`、`Sender` 各实现一次 `ShareHandle`——因为这两个类型是「设计上要在进程间共享」的。注意实现体为空：`share` 已是 trait 的默认方法，这里只需声明「它实现了 `ShareHandle`」（隐含要求 `Recver: AsHandle`，由 `forward_handle` 宏提供）。

底层系统调用封装：

[src/os/windows/c_wrappers.rs:52-77](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/c_wrappers.rs#L52-L77) `duplicate_handle_to_foreign`（L52-57）接收源句柄与目标进程句柄，交给 `duplicate_handle_inner`（L59-77）。后者是 `duplicate_handle`（本进程内复制）与 `duplicate_handle_to_foreign`（复制到指定进程）的共用核心：当 `other_process` 为 `None` 时目标进程取当前进程（即 4.1 节的「同进程克隆」），为 `Some(h)` 时复制进 `h` 指代的进程。第 71 行传 `0` 即 `bInheritHandle = FALSE`，确保产物不可继承。

#### 4.2.4 代码实践

**实践目标**：阅读理解 `share` 如何产出「对端进程有效」的句柄值（源码阅读型实践，因跨进程 demo 需 Windows 双进程编排）。

**操作步骤**：

1. 打开 [src/os/windows/share_handle.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/share_handle.rs)，对照 `duplicate_handle_inner` 的 `DuplicateHandle` 参数顺序：源进程、源句柄、目标进程、`&mut new_handle`、`dwDesiredAccess=0`、`bInheritHandle=0`、`DUPLICATE_SAME_ACCESS`。
2. 追问自己三个问题：(a) 为何 `other_process` 是 `BorrowedHandle` 而非进程 PID？(b) 返回的 `RawHandle` 能否在**本进程**直接使用？(c) 它与 4.1 的 `try_clone` 在「目标进程」上有何不同？

**需要观察的现象/预期结果**：

- (a) `DuplicateHandle` 的 API 就是以「目标进程的句柄」来指代目标进程，而非 PID——故参数是 `BorrowedHandle`。
- (b) 返回值是**在 receiver 进程里才有效**的句柄，在本进程里是悬空的，不能直接用。
- (c) `try_clone` 的目标进程恒为「当前进程」（`None` 分支），`share` 的目标进程是任意指定进程。

**待本地验证**：完整的双进程 demo 需要在 Windows 上编写两个独立程序并用 named pipe 传回 `share` 的数值，再以 `FromRawHandle` 重建。本讲不展开，留作进阶练习。

#### 4.2.5 小练习与答案

**练习 1**：为什么不能像 u5-l2 那样，直接把 `Sender` 的句柄数值发给一个**非子进程**让它使用？

> **答**：句柄是**进程局部**的。可继承句柄只在「创建时标注 + 父子关系」下保证两进程数值相同；非子进程的句柄表完全独立，直接发数值过去，对端拿到的整数大概率指向无关对象甚至无效。必须用 `DuplicateHandle` 在对端句柄表里**新建**一个指向同一内核对象的表项。

**练习 2**：`share` 返回 `RawHandle`（裸值），而 `try_clone` 返回 `Self`（安全类型）。为什么有这个差异？

> **答**：`try_clone` 的新句柄留在**本进程**，可以安全地包回 `OwnedHandle`/`Self`；`share` 的新句柄诞生在**对端进程**，本进程无法用安全类型持有它（它在本进程的句柄表里不存在），只能交出裸数值让对端去重建。

---

### 4.3 AdvOwnedHandle：低比特标记的零额外开销句柄（Windows）

#### 4.3.1 概念说明

`OwnedHandle` 是标准库类型，足够好，但 interprocess 在 Windows 后端常需要**随句柄附带一点额外布尔状态**。例如匿名管道的 `Sender` 要记录「是否有未刷新的缓冲」（`needs_flush`，见 u5-l1 的 limbo 机制）；named pipe 的 `RawPipeStream` 也要类似标记。

最朴素的写法是加一个字段：`struct Sender { io: OwnedHandle, needs_flush: bool }`。但这会让结构体从 1 个字（pointer-sized）膨胀，且 `Option<OwnedHandle>` 仍要占额外空间（因为 `OwnedHandle` 的 niche 是其 raw 值为 `null`/`0`）。

`AdvOwnedHandle` 的思路是经典的**低比特标记（low-bit tagging）**：把句柄值存进一个 `NonZeroUsize`，并「偷用」最低的 1～2 个比特来存布尔标记。为什么能偷？因为 Windows 内核句柄值在低位是留空的（实际句柄值不占用最低若干比特），可以安全地用 `|` 写入标记、用 `& !mask` 读出真实句柄。同时 `NonZeroUsize` 自带「永不为 0」的 niche，使 `Option<AdvOwnedHandle>` 与 `AdvOwnedHandle` 等大——零额外开销。

两个 `const TAG0: bool`、`const TAG1: bool` 泛型参数控制「哪个标记位是活跃的」：`AdvOwnedHandle<false, false>` 表示不带任何标记（等价于纯 `OwnedHandle`），`<true, false>` 带一位、`<true, true>` 带两位。用 const generic 而非运行时标志，是为了让「是否提供 `tag0()`/`set_tag0()` 方法」在**编译期**决定——未启用的位没有访问方法，调用者无法误写。

#### 4.3.2 核心流程

**标记的编码与解码**。设句柄数值为 \(h\)，两位标记为 \(t_0, t_1 \in \{0,1\}\)，则存储值为：

\[
\text{stored} = h \;\big|\; (t_1 \ll 1) \;\big|\; t_0
\]

读回真实句柄只需清除最低两位（掩码 `TAG_UNMASK = !0b11`）：

\[
h = \text{stored} \;\&\; \text{TAG\_UNMASK}
\]

读回标记：

\[
t_0 = (\text{stored} \;\&\; 1) \ne 0, \qquad t_1 = (\text{stored} \;\&\; 2) \ne 0
\]

`mk_tag` 还会把「该位是否被启用」（const 泛型 `TAG0`/`TAG1`）与「写入值」做逻辑与，确保未启用的位恒为 0。

**生命周期与所有权**。`AdvOwnedHandle` 是「拥有」类型：它的 `Drop` 把掩码后的裸句柄交还给 `OwnedHandle::from_raw_handle`，由后者负责 `CloseHandle`——所以标记位绝不泄漏给 `CloseHandle`（否则会拿一个错位的数值去关句柄）。`From<OwnedHandle>`、`IntoRawHandle`、`From<AdvOwnedHandle> for OwnedHandle` 等转换则保证标记位在跨类型时被正确剥离或保留。

#### 4.3.3 源码精读

类型定义与 Drop：

[src/os/windows/adv_handle.rs:12-21](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/adv_handle.rs#L12-L21) `AdvOwnedHandle` 是 `#[repr(transparent)]` 包着 `NonZeroUsize`（L16），`repr(transparent)` 保证它与 `usize`/`NonZeroUsize` 二进制布局一致。`Drop`（L18-21）调 `as_raw_handle()`（已掩码）重建 `OwnedHandle` 再丢弃——标记位被丢弃前就已剥离。

标记位运算核心：

[src/os/windows/adv_handle.rs:24-48](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/adv_handle.rs#L24-L48) 提供全部私有工具：`mk_tag`（L26-28）即上面的编码公式（`TAG1`/`TAG0` 是 const 泛型，与运行时 `tag1`/`tag0` 相与）；`TAG_MASK = 0b11`、`TAG_UNMASK = !0b11`（L33-34）；`new`（L38-43）把句柄转 `usize`、`|` 上标记、塞进 `NonZeroUsize`（注释「valid handles are never zero」即零 niche 的安全性来源）；`tag0_or_false`/`tag1_or_false`（L45-47）读最低两位。注意它们对**未启用**的位也安全返回 `false`。

句柄访问（掩码后借出）：

[src/os/windows/adv_handle.rs:100-106](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/adv_handle.rs#L100-L106) `AsRawHandle::as_raw_handle` 永远先 `& Self::TAG_UNMASK` 清掉标记位，再返回——下游的所有系统调用（`ReadFile`/`WriteFile`/`DuplicateHandle`）拿到的都是干净句柄。注释里的 `FUTURE use Strict Provenance API` 暗示当前用整数运算处理句柄值，未来或改用严格 provenance。

`TryClone` 实现——克隆时保留标记：

[src/os/windows/adv_handle.rs:183-189](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/adv_handle.rs#L183-L189) 复制底层句柄（走 `duplicate_handle`，本进程内、不可继承），再用 `new(h, self.tag0_or_false(), self.tag1_or_false())` 把**当前标记值**抄到新句柄上。这很关键：复制一个「带 `needs_flush=true` 标记」的句柄，副本也带 true（因为副本确实指向同一未刷新缓冲）。

它在真实类型里如何被使用？以匿名管道 `Sender` 为例：

[src/os/windows/unnamed_pipe.rs:124-163](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/unnamed_pipe.rs#L124-L163) `Sender` 内部是 `ManuallyDrop<AdvOwnedHandle>` 加一个独立的 `needs_flush: bool`（L125-128）。注意这里**没有**用 `AdvOwnedHandle` 的标记位来存 `needs_flush`，而是另设字段——因为 `Sender` 的 `Drop`（L151-158）需要在「确实需要 linger」时把句柄**转移**给 `linger_pool` 而非关闭，故用 `ManuallyDrop` 接管析构。`AdvOwnedHandle` 在此扮演「拥有句柄、提供 `AsHandle`/`TryClone`/`From` 一整套能力」的基座，`Sender`/`Recver`（L104，直接 `Recver(AdvOwnedHandle)`）在其上叠业务逻辑。named pipe 的 `RawPipeStream` 同样以 `ManuallyDrop<AdvOwnedHandle>` 为字段（[src/os/windows/named_pipe/stream.rs:75](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/stream.rs#L75)）。

#### 4.3.4 代码实践

**实践目标**：源码阅读型实践——追踪 `AdvOwnedHandle` 如何同时满足「零额外开销」「正确关闭句柄」「克隆时保留标记」三个约束。

**操作步骤**：

1. 在 [adv_handle.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/adv_handle.rs) 中定位三处：`Drop`（L18-21）、`as_raw_handle`（L100-106）、`TryClone`（L183-189）。
2. 回答：若 `Drop` 忘了掩码（直接用 `self.0.get()` 当句柄关闭），会发生什么？若 `TryClone` 忘了抄标记，副本的行为会如何偏离？

**需要观察的现象/预期结果**：

- `Drop` 不掩码：会把一个「末两位被污染」的数值交给 `CloseHandle`，要么关错对象、要么报无效句柄错误。
- `TryClone` 不抄标记：副本的标记位全为 0，若上层用标记表示「未刷新」，则副本会误以为已刷新、跳过 linger，造成数据丢失。

**预期结果**：理解「掩码」与「抄标记」是 `AdvOwnedHandle` 正确性的两个守门员。**待本地验证**：标记位的实际行为可在 Windows 上写小测验证（本环境为 Linux，无法编译 Windows 后端）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `AdvOwnedHandle` 用 `NonZeroUsize` 而非 `usize` 存储句柄？

> **答**：`NonZeroUsize` 自带「永不为 0」的 niche，使 `Option<AdvOwnedHandle>` 能用 0 这个非法值表示 `None`，与 `AdvOwnedHandle` 占同样大小——零额外开销。`usize` 没有这个 niche，`Option` 会多占空间。文档注释「valid handles are never zero」正是这个 niche 成立的前提。

**练习 2**：const 泛型 `TAG0`/`TAG1` 相比「运行时两个 `bool` 字段」有什么好处？

> **答**：const 泛型让「标记位是否启用」成为类型的一部分。只有 `TAG0 = true` 的实例才有 `tag0()`/`set_tag0()` 方法（见 L50-66 的条件 impl），编译期就禁止了对未启用位的误操作；且 `repr(transparent)` + 标记压进句柄低位，使整个类型仍是一个 `usize` 大小，不因多两个 `bool` 而膨胀。

**练习 3**：`AdvOwnedHandle` 与 `ShareHandle` 一个管「带标记的拥有」，一个管「跨进程共享」，二者在 `TryClone` 上如何统一？

> **答**：`AdvOwnedHandle` 直接实现 `TryClone`（本进程内 `duplicate_handle`），而 `ShareHandle` 是另一条「复制到指定进程」的路径。二者底层都走 `c_wrappers::duplicate_handle_inner` 的同一个 `DuplicateHandle`，差别仅在目标进程是「当前」还是「指定」——同源殊途。

---

## 5. 综合实践

把本讲三块内容串成一个任务：**写一个用 `try_clone()` 把同一个 local socket 连接交给两个作用域分别使用的程序，并验证它们指向同一底层连接**。

要求：

1. 用 `ListenerOptions` 起同步服务端，用 `Stream::connect` 起客户端。
2. 客户端拿到 `Stream` 后调用 `try_clone()` 得到第二份 `Stream`，在**不同作用域**（两个线程，或先后两段代码）分别读写。
3. 验证「指向同一连接」：用 `a` 写、drop `a`，再用克隆出的 `b` 读——若 `b` 仍能读到服务端响应，说明二者共享底层连接（参考 4.1.4 的示例）。
4. 进阶（Windows）：若你在 Windows 上，进一步尝试对 unnamed pipe 的 `Sender` 调用 `share(receiver)`，把得到的 `RawHandle` 经 named pipe 发给另一个独立进程，对端用 `FromRawHandle` 重建 `Recver` 并读取——这把 4.2 的跨进程共享与 4.1 的同进程克隆对照起来。
5. 对照思考：`try_clone()` 出来的副本是否可继承？结合 4.1 讲的「副本不可继承」与 u5-l1/u5-l2 的「原始句柄可继承」，写一两句话总结二者的所有权差异。

**预期结果**：克隆出的句柄与原句柄共享同一 OS 对象（关闭其一不影响另一个），且副本不可继承（无法直接用于 u5-l2 式的父子继承，需重新走 `share` 或创建可继承句柄）。本任务为示例级实践，**待本地验证**。

---

## 6. 本讲小结

- `TryClone`（[src/try_clone.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/try_clone.rs)）是 interprocess 的**跨平台可失败克隆**抽象：`fn try_clone(&self) -> io::Result<Self>`，因 `DuplicateHandle`/`dup` 可失败而存在；blanket impl 让所有 `Clone` 类型自动满足它。
- newtype 经 `forward_try_clone!` 宏、枚举经 `dispatch!` 把 `try_clone` 转发给芯，最终落到平台 `c_wrappers`（Unix `duplicate_fd` 用 `F_DUPFD_CLOEXEC`，Windows `duplicate_handle` 用 `DuplicateHandle`）。
- **副本一律不可继承**：Unix 设 `FD_CLOEXEC`、Windows 传 `bInheritHandle=0`，与原始管道句柄「默认可继承」相反——这是本讲的核心所有权差异。
- `OwnedHandle`/`OwnedFd`（拥有，drop 关闭）对 `BorrowedHandle`/`BorrowedFd`（借用，不关闭）的区分是全库句柄所有权的基础，`forward_handle` 宏为 newtype 生成它们之间的安全转换。
- Windows 的 `ShareHandle`（[share_handle.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/share_handle.rs)）解决「**非父子**进程间共享句柄」：经 `DuplicateHandle` 把句柄复制进目标进程表，返回对端才有效的裸值——句柄是进程局部的，不能直接发数值。
- Windows 的 `AdvOwnedHandle`（[adv_handle.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/adv_handle.rs)）用**低比特标记 + `NonZeroUsize` 零 niche**，在不增内存的前提下为句柄叠加布尔状态，`Drop`/`as_raw_handle`/`TryClone` 三处分别保证正确关闭、干净借出、克隆时保留标记。

---

## 7. 下一步学习建议

- **u8-l1（linger_pool）**：本讲多次提到 `Sender` 的 `needs_flush` 与 limbo，下一篇会讲清「drop 后缓冲为何不立即清空、后台线程如何延迟 `FlushFileBuffers`」，是 `AdvOwnedHandle` + `ManuallyDrop` 组合所服务的真实场景。
- **u9-l1（unsafe FFI 封装层）**：本讲的 `duplicate_handle_inner`、`duplicate_fd` 都是 `unsafe` 系统调用封装，下一篇系统讲解 `OrErrno`/`RawOsErrorExt` 等错误转换工具与 crate 的 `unsafe_op_in_unsafe_fn = forbid` 安全策略。
- **重读 u5-l2（句柄传递与子进程）**：学完本讲后回头比较「可继承句柄的父子传递」与「`ShareHandle` 的跨进程复制」两条路线，能更清楚地看出 interprocess 在所有权设计上的取舍。
- **延伸阅读**：标准库 [`std::os::windows::io`](https://doc.rust-lang.org/std/os/windows/io/index.html) 与 [`std::os::unix::io`](https://doc.rust-lang.org/std/os/unix/io/index.html) 模块文档，以及 Win32 [`DuplicateHandle`](https://learn.microsoft.com/windows/win32/api/handleapi/nf-handleapi-duplicatehandle) 文档，可补全本讲跳过的底层细节。
