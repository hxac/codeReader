# 非阻塞模式、超时与等待模式

> 本讲属于专家层（advanced），承接 u3-l1（ListenerOptions 构建器）与 u3-l3（Stream 接口与拆分）。前置讲义已经讲过这些 API 的「接口契约」与「构建器位标志压缩」；本讲的任务是钻进**实现**：四个非阻塞状态如何在两个平台上落地、收发超时为何在 Windows 上恒失败、连接等待三态在 Unix 与 Windows 上的实现为何截然不同。读完本讲，你将能精确预测任意「非阻塞 + 超时」组合在各平台上的行为。

## 1. 本讲目标

学完后你应当能够：

- 用「accept 维度 / stream 维度」两把尺子精确解释 `ListenerNonblockingMode` 的四态，并说清它们如何被压进一个字节。
- 区分三种「时间控制旋钮」——运行期非阻塞（`set_nonblocking`）、收发超时（`set_recv_timeout`/`set_send_timeout`）、连接等待模式（`ConnectWaitMode`）——并说清 `WouldBlock` 与 `TimedOut` 的关系。
- 描述 `ConnectWaitMode::Deferred / Timeout / Unbounded` 在 Unix（非阻塞 connect + `poll` 轮询）与 Windows（named pipe「连接即就绪」）上的实现差异。
- 看懂 Windows 监听器为何用 `AtomicEnum<ListenerNonblockingMode>` 而非 `AtomicBool`，以及 `accept` 之后为何要「事后校正」流的状态。

## 2. 前置知识

本讲默认你已经理解以下概念（在 u1/u2/u3 单元已建立）：

- **local socket 的壳/芯分层**：公共 `Listener`/`Stream` 枚举是「壳」，经 `dispatch_sync` 把系统调用下沉到 `os::unix`/`os::windows` 后端「芯」。
- **阻塞 I/O 与非阻塞 I/O**：阻塞调用在「无数据可读 / 缓冲已满 / 无连接到来」时会挂起当前线程直到条件满足；非阻塞调用在同样情况下立即返回 `Err(io::ErrorKind::WouldBlock)`，把「何时再试」的决定权交还调用者。
- **超时 I/O**：介于二者之间——在规定时长内阻塞等待，超时则返回 `Err(io::ErrorKind::TimedOut)`。
- **`io::ErrorKind`**：标准库对 I/O 错误的分类枚举，`WouldBlock`、`TimedOut`、`Unsupported` 是本讲的主角。

一个贯穿全讲的直觉：

> **三把旋钮控制的是同一件事——「当底层操作暂时无法完成时，调用方希望发生什么」。** 非阻塞 = 立即返回 `WouldBlock`；超时 = 等待有限时间后返回 `TimedOut`；阻塞（默认）= 无限等待。`Unsupported` 则表示「这个平台根本没提供这把旋钮」。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/local_socket/listener/trait.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/listener/trait.rs) | 定义 `Listener` trait、`set_nonblocking`、`ListenerNonblockingMode` 四态枚举及其拆解方法 |
| [src/local_socket/listener/options.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/listener/options.rs) | `ListenerOptions` 构建器，把四态压进位标志 |
| [src/local_socket/stream/trait.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/stream/trait.rs) | `Stream` trait 的 `set_nonblocking`/`set_recv_timeout`/`set_send_timeout` 签名 |
| [src/lib.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/lib.rs) | 顶层 `ConnectWaitMode` 三态枚举与 `timeout_or_unsupported` 折叠 |
| [src/local_socket/stream/options.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/stream/options.rs) | `ConnectOptions`，把 `wait_mode` 压进位标志 |
| [src/atomic_enum.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/atomic_enum.rs) | `AtomicEnum`/`ReprU8`：用原子 `u8` 存枚举的底层设施 |
| [src/misc.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/misc.rs) | `timeout_expiry`：把 `Duration` 换算成绝对截止时刻 |
| [src/os/unix/uds_local_socket/listener.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/uds_local_socket/listener.rs) | Unix 后端监听器：四态拆成「本体 + `AtomicBool`」两路 |
| [src/os/unix/uds_local_socket/stream.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/uds_local_socket/stream.rs) | Unix 后端流：超时委托标准库 `UnixStream`，连接走非阻塞 connect |
| [src/os/unix/c_wrappers.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/c_wrappers.rs) | Unix FFI 封装：`set_nonblocking`、`create_client`、`wait_for_connect`、`poll` |
| [src/os/windows/named_pipe/local_socket/listener.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/local_socket/listener.rs) | Windows 后端监听器：用 `AtomicEnum` 记住全态、`accept` 后校正 |
| [src/os/windows/named_pipe/local_socket/stream.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/local_socket/stream.rs) | Windows 后端流：收发超时恒返回 `Unsupported`、连接忽略 `wait_mode` |
| [src/os/windows/local_socket/dispatch_sync.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/local_socket/dispatch_sync.rs) | Windows 极薄派发入口 |

## 4. 核心概念与源码讲解

### 4.1 ListenerNonblockingMode：监听器的非阻塞四态

#### 4.1.1 概念说明

一个「监听器」对象身上其实有**两个相互独立的「是否非阻塞」维度**：

1. **accept 维度**——`accept()` 本身是否非阻塞。非阻塞时，没有客户端敲门就立即返回 `WouldBlock`，而不是挂起等待。
2. **stream 维度**——`accept()` 产出的**新连接流**是否默认非阻塞。注意这影响的是「将来从 `accept` 拿到的流」，而不是监听器自身。

两个维度各有「是/否」，组合出四种状态，这正是 `ListenerNonblockingMode` 的四个变体。为什么不用一个 `bool`？因为「我想让 accept 阻塞、但新连接非阻塞」（即 `Stream` 态）是一个真实需求——例如服务端主循环用阻塞 `accept` 简化逻辑，却希望每个客户端连接能被改成非阻塞以配合事件循环。单一 `bool` 表达不了这四种组合。

#### 4.1.2 核心流程

四态与两维度的对应关系如下表（`accept_nonblocking()` / `stream_nonblocking()` 是两个查询方法）：

| 变体 | accept 维度 | stream 维度 | 典型用途 |
|------|:---:|:---:|------|
| `Neither` | 阻塞 | 阻塞 | 默认值，最简单的阻塞服务器 |
| `Accept` | 非阻塞 | 阻塞 | 主循环要边 accept 边干别的事，但连接本身阻塞处理 |
| `Stream` | 阻塞 | 非阻塞 | 阻塞 accept、非阻塞连接（配合事件循环） |
| `Both` | 非阻塞 | 非阻塞 | 全非阻塞服务器 |

枚举还提供 `from_bool(accept, stream)` 构造器，把两个 `bool` 直接映射成变体——这正是位标志压缩的钥匙（见 4.1.3）。

运行期可以通过 `Listener::set_nonblocking(&self, mode)` **随时切换**四态（注意签名是 `&self`，不要求可变借用），这对「先阻塞启动、之后再切非阻塞」的场景很有用。

#### 4.1.3 源码精读

枚举本体定义在监听器 trait 文件中，四个变体按声明顺序获得 `#[repr(u8)]` 判别值 `0/1/2/3`：

[src/local_socket/listener/trait.rs:64-76](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/listener/trait.rs#L64-L76) —— `ListenerNonblockingMode` 枚举：`Neither(0)`、`Accept(1)`、`Stream(2)`、`Both(3)`，标记 `#[repr(u8)]`。

两个查询方法把变体拆回 `bool`，用 `matches!` 列举「含某维度」的变体：

[src/local_socket/listener/trait.rs:90-95](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/listener/trait.rs#L90-L95) —— `accept_nonblocking` 命中 `Accept | Both`，`stream_nonblocking` 命中 `Stream | Both`。

关键巧思在于判别值与位标志的**天然对齐**。`Neither=0b00`、`Accept=0b01`、`Stream=0b10`、`Both=0b11`；而 `ListenerOptions` 把这两个维度分别放在 bit0（accept）和 bit1（stream）：

[src/local_socket/listener/options.rs:29-37](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/listener/options.rs#L29-L37) —— 位偏移常量：`SHFT_NONBLOCKING_ACCEPT=0`、`SHFT_NONBLOCKING_STREAM=1`，`NONBLOCKING_BITS = bit0 | bit1`。

因为判别值的二进制正好是「bit0=accept、bit1=stream」，构建器的 `nonblocking()` setter 能**一次写两个位**，无需拆解：

[src/local_socket/listener/options.rs:86-94](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/listener/options.rs#L86-L94) —— `self.flags = (self.flags & (ALL_BITS ^ NONBLOCKING_BITS)) | nonblocking as u8;`：先清掉两个非阻塞位，再「或」上枚举判别值，等价于同时设置两维。

换算关系可以写成：

\[
\text{flags} \,\&\, \text{NONBLOCKING\_BITS} \;=\; \text{ListenerNonblockingMode as u8}
\]

反方向的 getter 则逐位读回，供后端按维度独立取用：

[src/local_socket/listener/options.rs:174-179](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/listener/options.rs#L174-L179) —— `get_nonblocking_accept` 读 bit0、`get_nonblocking_stream` 读 bit1。

最后，枚举实现了 `unsafe impl ReprU8`：

[src/local_socket/listener/trait.rs:97](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/listener/trait.rs#L97) —— `unsafe impl crate::ReprU8 for ListenerNonblockingMode {}`。这是把整个枚举塞进**单个原子字节**的前提，Windows 后端的 `AtomicEnum` 直接依赖它（见 4.1.4 的延伸与 4.3.3）。

`ReprU8` 的机制：要求实现者确实是 `#[repr(u8)]`，然后用 `transmute_copy` 在 `u8` 与枚举之间无损转换：

[src/atomic_enum.rs:51-61](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/atomic_enum.rs#L51-L61) —— `ReprU8` trait 的 `to_u8` / `from_u8`。

#### 4.1.4 代码实践

**实践目标**：观察 `Accept` 非阻塞模式下，无连接时 `accept()` 立即返回 `WouldBlock`，并据此实现一个「边 accept 边打心跳」的主循环。

**操作步骤**（示例代码，需放在启用 interprocess 的二进制 crate 中）：

```rust
// 示例代码：非阻塞 accept + 心跳主循环
use interprocess::local_socket::{ListenerOptions, ListenerNonblockingMode, prelude::*};
use std::time::{Duration, Instant};

fn main() -> std::io::Result<()> {
    let name = "u9-l2-nonblocking-accept".to_ns_name()?;
    let listener = ListenerOptions::new()
        .name(name)
        .nonblocking(ListenerNonblockingMode::Accept) // 仅 accept 非阻塞
        .create_sync()?;

    let mut last_beat = Instant::now();
    loop {
        match listener.accept() {
            Ok(stream) => { /* 处理连接，此处省略 */ let _ = stream; }
            Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                // 没有新连接：做点别的，例如限频心跳
                if last_beat.elapsed() >= Duration::from_secs(1) {
                    println!("heartbeat: still waiting for clients...");
                    last_beat = Instant::now();
                }
                std::thread::sleep(Duration::from_millis(50)); // 避免 CPU 空转
            }
            Err(e) => return Err(e),
        }
    }
}
```

**需要观察的现象**：
- 即使没有客户端，程序也不会卡在 `accept()`，而是不断打印心跳。
- `accept()` 在 `Accept`/`Both` 模式下返回 `WouldBlock`；在 `Neither`/`Stream` 模式下则会阻塞（这就是「仅 accept 维度」的精确含义）。

**预期结果**：心跳按秒打印；启动一个客户端后心跳期间穿插出现连接处理。

**待本地验证**：上述睡眠轮询的 CPU 占用与心跳频率取决于本机调度，具体数值需本地测量。**Windows 提醒**：named pipe 有「连接即就绪」语义，客户端连上后若不及时 `accept` 会产生 dead-on-arrival 连接阻塞后续连接（详见 u4-l3），故 Windows 上长时间不 accept 需谨慎。

#### 4.1.5 小练习与答案

**练习 1**：若希望「`accept` 阻塞、但每个新连接默认非阻塞」，应选哪个变体？它压进 `flags` 后 bit0/bit1 各是多少？

> **答案**：选 `Stream`。它只置 stream 维度，故 bit0(accept)=0、bit1(stream)=1，判别值 `0b10 = 2`，与 `NONBLOCKING_BITS` 中 bit1 单独为 1 一致。

**练习 2**：为什么 `nonblocking()` setter 用 `| nonblocking as u8` 一行就能写两个位，而不必分别 `set_bit` 两次？

> **答案**：因为枚举判别值的二进制位布局（bit0=accept、bit1=stream）与位标志的布局完全一致，`nonblocking as u8` 本身就已编码好两个维度，先清位再「或」即可。

---

### 4.2 Stream 的非阻塞与收发超时

#### 4.2.1 概念说明

一旦连接建立，流的「时间控制」由三把旋钮决定，本小节聚焦后两把（第一把 `set_nonblocking` 在 4.1 已以监听器形式登场，流的版本语义更简单——单个 `bool`）：

- `set_nonblocking(bool)`：切换**运行期**非阻塞。非阻塞时，读无数据 / 写缓冲满 → 立即 `WouldBlock`。
- `set_recv_timeout(Option<Duration>)`：收超时。设为 `Some(t)` 时，读在 `t` 内无数据 → `TimedOut`；`None`（默认）则无限等待。
- `set_send_timeout(Option<Duration>)`：发超时，语义对称。

`WouldBlock` 与 `TimedOut` 的关系值得记牢：

> 非阻塞模式（`set_nonblocking(true)`）下，操作**永远**立即返回 `WouldBlock`，超时设置**不生效**；超时只在阻塞模式下才有意义，届时它在「立即返回」与「无限等待」之间插入一条「等 t 秒」的中间道路。两者是互斥的时间策略，不是叠加。

#### 4.2.2 核心流程

三把旋钮在 `Stream` trait 上的签名一致地返回 `io::Result<()>`——返回 `Err` 通常意味着「此平台不支持该旋钮」：

```
set_nonblocking(&self, bool)         -> io::Result<()>
set_recv_timeout(&self, Option<Dur>) -> io::Result<()>
set_send_timeout(&self, Option<Dur>) -> io::Result<()>
```

平台分歧是本节的重点：

| 旋钮 | Unix 后端 | Windows 后端（named pipe） |
|------|-----------|--------------------------|
| `set_nonblocking` | 支持，底层 `fcntl(F_SETFL, O_NONBLOCK)` 或 `ioctl(FIONBIO)` | 支持 |
| `set_recv_timeout` | 支持，委托标准库 `UnixStream::set_read_timeout` | **恒返回 `Err(Unsupported)`** |
| `set_send_timeout` | 支持，委托 `set_write_timeout` | **恒返回 `Err(Unsupported)`** |

为什么 Windows named pipe 不支持收发超时？因为 Win32 named pipe 没有与 BSD socket `SO_RCVTIMEO`/`SO_SNDTIMEO` 对应的「单次读写超时」原语。interprocess 选择诚实地返回 `Unsupported`，而不是假装支持，这正体现了「同接口、异实现」的设计纪律（见 u3-l3）。

#### 4.2.3 源码精读

trait 契约定义了三个方法的签名与文档（注意 `set_nonblocking` 接 `bool`，比监听器版简单）：

[src/local_socket/stream/trait.rs:36-51](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/stream/trait.rs#L36-L51) —— 三个旋钮的声明，文档明确「非阻塞下立即返回 `WouldBlock`」。

**Unix 后端**把超时直接转发给标准库的 `UnixStream`（UDS 的收发超时由内核 `SO_RCVTIMEO`/`SO_SNDTIMEO` 实现），非阻塞则走自己的 FFI 封装：

[src/os/unix/uds_local_socket/stream.rs:54-66](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/uds_local_socket/stream.rs#L54-L66) —— `set_nonblocking` 调 `c_wrappers::set_nonblocking`；`set_recv_timeout` 调 `self.0.set_read_timeout`；`set_send_timeout` 调 `self.0.set_write_timeout`。

`set_nonblocking` 的 FFI 实现按平台分两路——Linux/Android 用 `ioctl(FIONBIO)`，其它 Unix 用 `fcntl(F_SETFL)` 翻转 `O_NONBLOCK`：

[src/os/unix/c_wrappers.rs:67-79](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/c_wrappers.rs#L67-L79) —— `set_nonblocking`：非 Linux/Android 走 `get_flflags` 读旧标志、清掉 `O_NONBLOCK` 再按需置位；Linux/Android 走 `ioctl(FIONBIO)`。

> 旁注：还存在一个 `fast_set_nonblocking`（[c_wrappers.rs:83-92](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/c_wrappers.rs#L83-L92)），它假设该 fd「从未被外部用 `fcntl(F_SETFL)` 改过」，因此可省掉一次「读旧标志」直接写新标志——监听器 `accept` 后给新连接置非阻塞时就用它（见 4.3.3）。

**Windows 后端**则用一个本地函数 `no_timeouts()` 统一拒绝超时：

[src/os/windows/named_pipe/local_socket/stream.rs:25-27](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/local_socket/stream.rs#L25-L27) —— `no_timeouts()` 返回 `Err(Unsupported)`，消息点名「named pipes do not support I/O timeouts」。

[src/os/windows/named_pipe/local_socket/stream.rs:52-55](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/local_socket/stream.rs#L52-L55) —— `set_recv_timeout` / `set_send_timeout` 直接返回 `no_timeouts()`，参数都用 `_: Option<Duration>` 忽略掉。

（流自身的 `set_nonblocking` 在 Windows 上是支持的，见同文件 48-50 行转发到底层 `DuplexPipeStream`。）

#### 4.2.4 代码实践

**实践目标**：验证「阻塞 + 超时」返回 `TimedOut`，并对比 Windows 上超时旋钮返回 `Unsupported`。

**操作步骤**（示例代码）：

```rust
// 示例代码：收超时行为验证（Unix 有意义，Windows 会得到 Unsupported）
use interprocess::local_socket::{prelude::*, ListenerOptions, ConnectOptions};
use std::time::Duration;

fn main() -> std::io::Result<()> {
    let name = "u9-l2-timeout".to_ns_name()?;
    let listener = ListenerOptions::new().name(name.clone()).create_sync()?;
    let handle = std::thread::spawn(move || {
        let _client = ConnectOptions::new().name(name).connect_sync().unwrap();
        // 故意不发送任何数据，让服务端读阻塞
        std::thread::sleep(Duration::from_secs(5));
    });
    let server = listener.accept()?;
    server.set_recv_timeout(Some(Duration::from_millis(500)))?;
    let mut buf = [0u8; 16];
    match std::io::Read::read(&mut &server, &mut buf) {
        Err(e) => println!("read returned error kind: {:?}", e.kind()),
        Ok(n) => println!("read returned {} bytes", n),
    }
    handle.join().unwrap();
    Ok(())
}
```

**需要观察的现象**：
- **Unix**：`set_recv_timeout` 返回 `Ok(())`，约 0.5 秒后 `read` 返回 `Err`，`kind()` 为 `TimedOut`。
- **Windows**：`set_recv_timeout` 本身返回 `Err`，`kind()` 为 `Unsupported`，`read` 根本到不了。

**预期结果**：Unix 上打印 `TimedOut`；Windows 上在 `set_recv_timeout` 处即报 `Unsupported`。

**待本地验证**：Unix 下「约 0.5 秒」的实测耗时受调度影响，需本地计时确认。

#### 4.2.5 小练习与答案

**练习 1**：若先 `set_nonblocking(true)` 再 `set_recv_timeout(Some(1s))`，读无数据时返回什么？

> **答案**：返回 `WouldBlock`。非阻塞模式下超时不生效，操作总是立即返回 `WouldBlock`；超时只在阻塞模式（`set_nonblocking(false)`）下才作为「限时等待」介入。

**练习 2**：为什么 Windows 后端把超时参数写成 `_: Option<Duration>` 而不是 `_timeout`？

> **答案**：函数体根本不使用该值（直接返回 `Unsupported`），用无名 `_` 表达「彻底忽略」。这与 4.3 将看到的「Windows 连接忽略 `wait_mode`」是同一类「同接口、不支持、诚实报错」的处理。

---

### 4.3 ConnectWaitMode：连接等待三态

#### 4.3.1 概念说明

`ConnectWaitMode` 控制**连接操作本身**如何等待服务端 accept，这是与「连接后的收发超时」（4.2）完全不同的阶段。三态：

- `Deferred`：连接调用**立即返回**一个尚未真正建立的对象；真正的握手在后台进行，连接错误会推迟到「下一次 I/O」才暴露。
- `Timeout(Duration)`：进入等待状态，最多等指定时长；超时则返回 `Err(TimedOut)`。
- `Unbounded`（默认）：进入等待状态，无限期等待直到连接建立。

务必区分两个阶段，这是初学者最易混淆的点：

| 阶段 | 控制旋钮 | 关心的事 |
|------|---------|---------|
| 连接建立阶段 | `ConnectWaitMode` | 「连接握手」要等多久 |
| 连接后收发阶段 | `set_recv/send_timeout` | 「读写」要等多久 |

`set_recv_timeout` 管**不了**连接握手，`wait_mode` 也管**不了**读写——二者正交。

#### 4.3.2 核心流程

公共层把 `ConnectWaitMode` 用一个折叠函数 `timeout_or_unsupported` 归一成后端能消化的 `Option<Duration>`：

```
Deferred  -> Err(Unsupported)        // 后端不支持「立即返回、后台握手」
Timeout(t) -> Ok(Some(t))            // 等待 t 秒
Unbounded  -> Ok(None)               // 无限等待
```

三态在不同平台的实现差异极大：

| 平台 | `Unbounded` | `Timeout` | `Deferred` |
|------|------------|-----------|-----------|
| **Unix** | 阻塞 connect 直接等 | 非阻塞 connect → `EINPROGRESS` → `poll` 限时轮询 | 非阻塞 connect 后立即返回，握手留待后续 I/O |
| **Windows（local socket 包装层）** | 阻塞等待 | **等同 Unbounded**（忽略超时） | **不支持**，但在 local socket 包装层会被忽略而非报错 |

Unix 的 `Timeout`/`Deferred` 都依赖「非阻塞 connect」这一底层机制：发起连接时 socket 设为非阻塞，`connect` 立即返回 `EINPROGRESS`（表示「正在连接」），随后用 `poll` 等待 socket 可写或超时。Windows 的 named pipe 则是「连接即就绪」——客户端一 `CreateFile` 成功就立刻处于已连接态，没有 Unix 那种「握手在后台进行」的阶段，故 `wait_mode` 在 local socket 包装层被整体忽略（原生 named pipe 层对 `Deferred` 报错，详见 u3-l2）。

#### 4.3.3 源码精读

顶层枚举与折叠函数：

[src/lib.rs:42-66](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/lib.rs#L42-L66) —— `ConnectWaitMode`（`Deferred`/`Timeout(Duration)`/`Unbounded`，`Unbounded` 为 `#[default]`），以及 `timeout_or_unsupported`：`Deferred→Err(Unsupported)`、`Timeout(t)→Ok(Some(t))`、`Unbounded→Ok(None)`。

`ConnectOptions::wait_mode` 把三态压进位标志（`SHFT_DEFERRED`、`SHFT_TIMEOUT`），并用独立字段存 `timeout` 时长；其文档注释里有一张「额外 `fcntl` 次数」的对照表，揭示 Unix 后端如何尽量减少系统调用：

[src/local_socket/stream/options.rs:57-98](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/stream/options.rs#L57-L98) —— `wait_mode` setter 与平台差异说明（Windows：`Timeout` 实际无效、`Deferred` 在原生层不支持）。

**Unix 后端**的连接实现是本节的技术核心。`from_options` 先判定「是否需要非阻塞 connect」（`Timeout` 或 `Deferred` 都需要），用 `create_client` 发起连接，仅在 `Timeout` 且确属进行中（`inprog`）时调用 `wait_for_connect` 限时等待；最后还要「校正」流的非阻塞状态——因为非阻塞 connect 临时把 socket 设成了非阻塞，得按用户真正想要的 `nonblocking_stream` 改回去：

[src/os/unix/uds_local_socket/stream.rs:31-52](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/uds_local_socket/stream.rs#L31-L52) —— `nonblocking_connect` 由 `Timeout | Deferred` 触发；`create_client(..., nonblocking_connect)`；仅 `Timeout` 且 `inprog` 时 `wait_for_connect`；最后若 `nonblocking_stream != nonblocking_connect` 则 `fast_set_nonblocking` 校正。

非阻塞 connect 的判定发生在 FFI 封装层——`connect` 返回 `EINPROGRESS` 或 `EAGAIN` 即视为「进行中」：

[src/os/unix/c_wrappers.rs:263-279](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/c_wrappers.rs#L263-L279) —— `create_client`：`connect` 成功则 `inprog=false`；`EINPROGRESS`/`EAGAIN` 则 `inprog=true`；其它错误直接返回。

限时等待用 `poll` 轮询 `POLLOUT`，超时返回 `TimedOut`；若就绪则用 `SO_ERROR` 取出真正的连接结果（连接失败也会让 socket 变可写，须查错误）：

[src/os/unix/c_wrappers.rs:286-303](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/c_wrappers.rs#L286-L303) —— `wait_for_connect`：`poll_loop(POLLOUT)`；命中 `POLLHUP/POLLERR` 或 VxWorks 时查 `take_error`；无 `POLLOUT` 则 `TimedOut`。

`poll_loop` 把 `Option<Duration>` 折算成绝对截止时刻 `end`，每次轮询后用剩余时间继续，被信号打断（`EINTR`）时返回 0 触发重试而非报错：

[src/os/unix/c_wrappers.rs:306-326](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/c_wrappers.rs#L306-L326) —— `poll_loop`：`timeout.map(timeout_expiry)` 算截止点；命中事件或 hangup/error 即 `break`；到期则返回 0；否则 `spin_loop()` 让出 CPU 时间片。

[src/os/unix/c_wrappers.rs:328-400](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/c_wrappers.rs#L328-L400) 中的 `poll` 在支持的系统用 `ppoll`（精度到纳秒），其余退化到毫秒级 `poll`，并把 `EINTR` 归一为 `Ok(0)` 让上层重试。

绝对截止时刻由 `timeout_expiry` 计算，它还能捕获「`Duration` 太大导致 `Instant` 溢出」的错误：

[src/misc.rs:403-408](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/misc.rs#L403-L408) —— `timeout_expiry`：`Instant::now().checked_add(timeout)`，溢出时报 `InvalidInput`。

**Windows 后端**的连接实现则极其简短——它完全无视 `wait_mode`，直接 `connect_by_path` 后按 `nonblocking_stream` 调一次 `set_nonblocking`：

[src/os/windows/named_pipe/local_socket/stream.rs:38-45](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/local_socket/stream.rs#L38-L45) —— `from_options`：`connect_by_path` 后仅按 `nonblocking_stream` 调整；`wait_mode` 在此层根本不读（与 u3-l2 的结论一致：Windows local socket 包装层恒为 `Unbounded` 语义）。

派发入口只是把公共 `ConnectOptions` 透传给后端：

[src/os/windows/local_socket/dispatch_sync.rs:7-15](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/local_socket/dispatch_sync.rs#L7-L15) —— `connect` 委托 `np_impl::Stream::from_options`。

#### 4.3.4 代码实践

**实践目标**：体会 `Deferred` 与 `Timeout` 在 Unix 上的差异，以及 Windows 上 `wait_mode` 被忽略的事实。

**操作步骤**（示例代码）：

```rust
// 示例代码：连接等待模式对比（需 Unix 才能看出 Timeout 的 poll 行为）
use interprocess::{
    local_socket::{prelude::*, ListenerOptions, ConnectOptions},
    ConnectWaitMode,
};
use std::time::Duration;

fn main() -> std::io::Result<()> {
    let name = "u9-l2-waitmode".to_ns_name()?;
    // 注意：这里故意不启动服务端
    let res = ConnectOptions::new()
        .name(name.clone())
        .wait_mode(ConnectWaitMode::Timeout(Duration::from_millis(300)))
        .connect_sync();
    match res {
        Ok(_) => println!("connected (unexpected without a server)"),
        Err(e) => println!("connect failed, kind = {:?}", e.kind()),
    }
    Ok(())
}
```

**需要观察的现象**：
- **对端不存在时**（如上，未起服务端）：根据 u3-l2 的结论，`connect` 会**立即硬失败**（`NotFound`/`ConnectionRefused`），`wait_mode` 此时**不产生差异**——因为根本没有「进行中的握手」可等。
- **要让 `Timeout` 真正发挥作用**，需要一个「存在但迟迟不 accept」的服务端：在 Unix 上会观察到约 0.3 秒后返回 `TimedOut`（非阻塞 connect → `EINPROGRESS` → `poll` 超时）。

**预期结果**：对端不存在 → 立即 `ConnectionRefused`（Unix）/ 相应错误（Windows）；对端存在但不 accept → Unix 上 `Timeout` 到点返回 `TimedOut`。

**待本地验证**：「对端存在但不 accept」的场景需要特殊构造（例如服务端 `accept` 前长期睡眠），其返回时机与错误类型需本地实测确认。Windows 上由于包装层忽略 `wait_mode`，`Timeout` 与 `Unbounded` 行为相同，这一点也需本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 Unix 的 `Timeout` 连接要先发非阻塞 connect 再 `poll`，而不是直接阻塞 connect？

> **答案**：阻塞 connect 一旦发出就无法被「限时」——它会一直挂到成功或失败。要实现超时，必须先用非阻塞 connect 拿到「进行中（`EINPROGRESS`）」的立即返回，再用带超时的 `poll` 等待 socket 可写，超时即可主动放弃。这正是 `create_client` + `wait_for_connect` 的协作模式。

**练习 2**：`wait_mode(Timeout)` 设置的时长，与之后 `set_recv_timeout` 设置的时长，会相互影响吗？

> **答案**：不会。二者作用于不同阶段：前者管「连接握手等多久」，后者管「连接后单次读等多久」。`Timeout` 连接完成后，socket 的非阻塞状态会被校正回用户指定的 `nonblocking_stream`，收发超时则另由 `set_recv_timeout` 独立设置。

---

## 5. 综合实践

把三把旋钮串起来，实现一个「自适应」local socket 服务端：用 `Accept` 非阻塞模式跑主循环，无连接时打印心跳；一旦收到连接（**Unix** 上）切到收发超时模式处理请求，超时则断开。

**任务要求**：

1. 用 `ListenerOptions::nonblocking(ListenerNonblockingMode::Accept)` 创建监听器。
2. 主循环中 `accept()` 返回 `WouldBlock` 时打印限频心跳（如每秒一次），并 `sleep` 一小段避免空转。
3. 收到连接后，调用 `set_recv_timeout(Some(2s))` 给读操作限时；若读超时则打印日志并继续主循环。
4. 在代码里用 `#[cfg]` 或运行期判断处理 Windows 差异：Windows 上 `set_recv_timeout` 会返回 `Unsupported`，应优雅处理（如回退到无超时的阻塞读，或直接报错退出）。
5. 同时实现一个配套客户端：连接后先睡 3 秒再发数据，用来触发服务端的读超时。

**验收要点**：

- 服务端在无客户端时持续打印心跳，CPU 不打满。
- Unix 上客户端延迟发送时，服务端约 2 秒后观测到 `TimedOut` 并打印日志。
- Windows 上服务端能检测到 `set_recv_timeout` 返回 `Unsupported` 并走你设计的回退分支。

**待本地验证**：心跳频率、CPU 占用、超时实测耗时均依赖本机环境，需本地测量并记录。

> **提示**：本实践刻意暴露「同一份代码在两个平台上行为不同」的现实。这正是本讲的核心教训——非阻塞与超时 API 在 interprocess 里**接口统一、实现分化**，写跨平台代码时必须为 `Unsupported` 这类「平台不支持」的返回值预留处理路径。

## 6. 本讲小结

- `ListenerNonblockingMode` 是「accept 维度 × stream 维度」的四态枚举，其 `#[repr(u8)]` 判别值与 `ListenerOptions` 的 bit0/bit1 天然对齐，故构建器能一次写两位。
- 流的时间控制有三把旋钮：运行期 `set_nonblocking(bool)`、收发超时 `set_recv/set_send_timeout`；`WouldBlock`（非阻塞立即返回）与 `TimedOut`（阻塞限时）是互斥策略，不叠加。
- Windows named pipe 后端对收发超时**恒返回 `Unsupported`**，因为 Win32 无对应原语；Unix 则委托标准库 `UnixStream` 的 `SO_RCVTIMEO`/`SO_SNDTIMEO`。
- `ConnectWaitMode`（`Deferred`/`Timeout`/`Unbounded`）管的是**连接握手**阶段，与收发超时正交；`timeout_or_unsupported` 把它折叠成 `Option<Duration>`。
- Unix 的 `Timeout`/`Deferred` 依赖「非阻塞 connect → `EINPROGRESS` → `poll` 限时轮询」；Windows named pipe「连接即就绪」，local socket 包装层**整体忽略 `wait_mode`**。
- Windows 监听器用 `AtomicEnum<ListenerNonblockingMode>`（依赖 `ReprU8`）记住全态，并在 `accept` 后按需「事后校正」新连接的非阻塞状态——这是 Windows 不同于 Unix（用 `AtomicBool` + `fast_set_nonblocking`）的关键实现差异。

## 7. 下一步学习建议

- **衔接 u9-l1（FFI 封装层）**：本讲反复出现的 `poll`/`ppoll`、`fcntl`、`ioctl(FIONBIO)`、`OrErrno`、`EINTR`→`Ok(0)` 等模式，正是 u9-l1 详讲的 `c_wrappers` 范式。建议回头精读 [src/os/unix/c_wrappers.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/c_wrappers.rs) 的 `poll` 与 `OrErrno`，把「系统调用 → `io::Result`」的转换彻底打通。
- **深入 Windows named pipe 内部**：若想理解 Windows 监听器 `accept` 的「预装膛实例」「dead-on-arrival」机制，以及非阻塞 accept 为何牵涉后台线程，请重读 u4-l3 与 u8-l1（linger_pool）。
- **看测试如何覆盖超时边界**：参考 u9-l3，阅读 `tests/` 中 `timeout`、`no_server`、`no_client` 等用例，理解 interprocess 如何在不稳定时序下测试这些「时间相关」行为。
- **动手扩展**：尝试为本讲的「综合实践」补一个跨平台集成测试（仿照 `tests/util` 的抽象），分别断言 Unix 的 `TimedOut` 与 Windows 的 `Unsupported`，体会「同接口、异实现」如何被测试守护。
