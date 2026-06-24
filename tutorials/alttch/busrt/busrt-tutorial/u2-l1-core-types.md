# 核心类型：Error、QoS、FrameOp 与 FrameData

## 1. 本讲目标

本讲是进入 BUS/RT 源码内部的第一步。学完后你应当能够：

- 说清 `ErrorKind` / `Error` 错误体系是如何与线上的 `ERR_*` 字节常量一一对应的，并能解释「编码准确、解码有损」这个细节。
- 写出 `QoS` 四个等级的位语义，会用位运算判断 `needs_ack()` 与 `is_realtime()`。
- 区分 `FrameOp`（客户端请求代理执行的操作）与 `FrameKind`（帧本身的类型标签）这两个看似相似、实则职责不同的枚举。
- 理解 `FrameData` 为什么用 `buf` + `payload_pos` 实现零拷贝，以及 `Frame = Arc<FrameData>` 为什么是广播/发布订阅高性能分发的基础。

这些类型全部定义在 [`src/lib.rs`](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs) 中，且都不依赖任何 Cargo feature——也就是说，即便用 `default-features = false` 编译，它们依然存在。它们是整个库的「公共契约」，后续所有模块（broker、ipc、rpc）都建立在这些类型之上。

## 2. 前置知识

阅读本讲前，你需要大致了解（对应前置讲义 u1-l3）：

- **协议常量**：`lib.rs` 顶部有一批 `OP_*`（操作码）、`ERR_*`（错误码）常量，它们是线上字节的具体数值。
- **feature 门控**：BUS/RT 用 Cargo feature 控制模块编译，但本讲涉及的核心类型**不受 feature 门控**，始终编译。
- **进程间通信（IPC）**：BUS/RT 是一个消息代理，客户端把「帧（Frame）」发给代理，代理再分发给其他客户端。

此外，需要一点 Rust 基础：

- `#[repr(u8)]`：让枚举的内存表示就是一个 `u8`，于是 `variant as u8` 能拿到它在内存里的字节值。
- `Arc<T>`：原子引用计数指针，`.clone()` 只增加计数、不复制内部数据。
- 位运算 `&`：按位与，常用来「掩码」出某几个比特位。

> 名词解释：
> - **帧（Frame）**：BUS/RT 中传输的基本单位，包含类型、发送者、主题、负载等。
> - **线上字节（wire byte）**：真正写到 socket 里的原始字节，区别于内存里的 Rust 对象。
> - **零拷贝（zero-copy）**：尽量复用同一块内存，而不是反复复制 `Vec<u8>`。

## 3. 本讲源码地图

本讲只涉及一个源码文件，但它信息密度很高：

| 文件 | 作用 |
|------|------|
| [`src/lib.rs`](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs) | 库入口：协议常量、核心类型（`Error`/`ErrorKind`/`QoS`/`FrameOp`/`FrameKind`/`FrameData`/`Frame`）、类型别名、`empty_payload!` 宏、模块声明。 |

辅助阅读（用于看到类型被真实使用）：

| 文件 | 作用 |
|------|------|
| [`examples/client_listener.rs`](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/examples/client_listener.rs) | 演示 `frame.sender()` / `kind()` / `topic()` / `payload()` 的实际调用。 |
| [`examples/inter_thread.rs`](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/examples/inter_thread.rs) | 演示 `QoS::No` 与 `QoS::Processed` 在 `send` / `subscribe` 中的用法。 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：错误体系、服务质量 QoS、帧操作码与帧类型、帧数据与帧句柄。

### 4.1 错误体系：ErrorKind 与 Error

#### 4.1.1 概念说明

任何一个网络协议都需要一套错误语言。BUS/RT 的设计很直接：**每一个错误种类就是一个线上字节**。代理处理失败时，回送的不是一个复杂的错误对象，而是一个 `u8` 错误码。客户端拿到这个字节后，再还原成有意义的错误类型。

这套机制由三层构成：

1. **协议常量 `ERR_*`**：定义在 [`src/lib.rs:27-35`](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L27-L35)，是「错误码的数值表」。
2. **`ErrorKind` 枚举**：把这些数值包成 Rust 枚举，方便在代码里 `match`。关键在于它带 `#[repr(u8)]`，所以 `variant as u8` 直接就是线上字节。
3. **`Error` 结构体**：在 `ErrorKind` 之外再附加一段可选的人类可读 `message`，用于日志和调试。

#### 4.1.2 核心流程

错误的「编码 → 传输 → 解码」流程：

```text
代理内部产生错误
   │  ErrorKind::Timeout
   ▼  kind as u8  ──► 0x78（#[repr(u8)] 直接拿到字节）
写入线上 ACK/状态字节
   │
   ▼
客户端收到字节 0x78
   │  ErrorKind::from(0x78)  ──► 见下方「有损解码」
   ▼
Error { kind, message }
```

注意一个反直觉的点：**编码是准确的，解码却是有损的**。`kind as u8` 永远精确；但 `ErrorKind::from(byte)` 这一侧并不一定能还原出原来的种类（详见 4.1.3）。

状态字节还有一个「成功」约定：`RESPONSE_OK = 0x01` 表示成功，其它值都视为错误码。这由 [`IntoBusRtResult for u8`](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L230-L246) 统一处理。

#### 4.1.3 源码精读

**协议常量**——错误码的数值表：

```rust
pub const ERR_CLIENT_NOT_REGISTERED: u8 = 0x71;
pub const ERR_DATA: u8 = 0x72;
pub const ERR_IO: u8 = 0x73;
pub const ERR_OTHER: u8 = 0x74;
pub const ERR_NOT_SUPPORTED: u8 = 0x75;
pub const ERR_BUSY: u8 = 0x76;
pub const ERR_NOT_DELIVERED: u8 = 0x77;
pub const ERR_TIMEOUT: u8 = 0x78;
pub const ERR_ACCESS: u8 = 0x79;
```

见 [`src/lib.rs:27-35`](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L27-L35)。注意它们集中在 `0x71`–`0x79`，留出空间给操作码（`0x00`–`0x13`、`0xFE`）使用。

**`ErrorKind` 枚举**——每个变体的判别值就是错误码：

```rust
#[derive(Debug, Eq, PartialEq, Copy, Clone)]
#[repr(u8)]
pub enum ErrorKind {
    NotRegistered = ERR_CLIENT_NOT_REGISTERED, // 0x71
    NotSupported = ERR_NOT_SUPPORTED,         // 0x75
    Io = ERR_IO,                               // 0x73
    Timeout = ERR_TIMEOUT,                     // 0x78
    Data = ERR_DATA,                           // 0x72
    Busy = ERR_BUSY,                           // 0x76
    NotDelivered = ERR_NOT_DELIVERED,          // 0x77
    Access = ERR_ACCESS,                       // 0x79
    Other = ERR_OTHER,                         // 0x74
    Eof = 0xff,                                // 仅本地，无 ERR_ 常量
}
```

见 [`src/lib.rs:91-104`](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L91-L104)。`#[repr(u8)]` 是整套机制的支点：`ErrorKind::Timeout as u8` 等于 `0x78`，可以直接塞进线上字节。

**有损解码**——`From<u8> for ErrorKind`：

```rust
impl From<u8> for ErrorKind {
    fn from(code: u8) -> Self {
        match code {
            ERR_CLIENT_NOT_REGISTERED => ErrorKind::NotRegistered,
            ERR_NOT_SUPPORTED => ErrorKind::NotSupported,
            ERR_IO => ErrorKind::Io,
            ERR_DATA => ErrorKind::Data,
            ERR_BUSY => ErrorKind::Busy,
            ERR_NOT_DELIVERED => ErrorKind::NotDelivered,
            ERR_ACCESS => ErrorKind::Access,
            _ => ErrorKind::Other,    // 注意：ERR_TIMEOUT 没有单独分支！
        }
    }
}
```

见 [`src/lib.rs:106-119`](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L106-L119)。这里有一个**值得记住的细节**：`ERR_TIMEOUT`（`0x78`）并没有出现在 `match` 分支里，因此 `ErrorKind::from(0x78)` 会落到 `_ => ErrorKind::Other`。也就是说，把一个 `Timeout` 编码成字节再解码回来，得到的是 `Other` 而不是 `Timeout`。同理 `Eof`（`0xff`）也没有对应常量，解码同样变成 `Other`。

> 为什么会这样？`Timeout` 主要是**客户端本地**的概念（等待 ACK 超时），通常不由代理作为错误字节回送；`Eof` 则是连接结束的本地信号。因此解码侧用 `Other` 兜底。理解这个「不对称」对阅读后续 rpc/ipc 代码很有帮助。

**`Error` 结构体与构造方法**：

```rust
#[derive(Debug)]
pub struct Error {
    kind: ErrorKind,
    message: Option<String>,
}
```

见 [`src/lib.rs:142-146`](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L142-L146)。它提供了一批便捷构造器，例如 [`Error::timeout()`](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L210-L216)、[`Error::data(e)`](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L176-L181)、[`Error::access(e)`](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L182-L188) 等（完整列表见 [`src/lib.rs:160-228`](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L160-L228)），以及读取种类的 [`kind()`](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L224-L227)。

**两类本地错误的产生**：

- 系统 IO 错误：[`From<std::io::Error>`](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L255-L269) 检测到 `UnexpectedEof` / `BrokenPipe` / `ConnectionReset` 时，归类为本地 `Eof`，其余归为 `Io`。这正是连接断开时的判定逻辑。
- `async_channel::SendError`、`oneshot::RecvError`：当事件通道关闭时（见 [`src/lib.rs:299-306`](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L299-L306) 与 [`src/lib.rs:308-316`](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L308-L316)），同样产生 `Eof`。

**状态字节 → Result**：

```rust
impl IntoBusRtResult for u8 {
    fn to_busrt_result(self) -> Result<(), Error> {
        if self == RESPONSE_OK {        // 0x01
            Ok(())
        } else {
            Err(Error { kind: self.into(), message: None })
        }
    }
}
```

见 [`src/lib.rs:234-246`](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L234-L246)。这是「线上一个字节」与「Rust 的 `Result`」之间的桥梁，`RESPONSE_OK` 定义在 [`src/lib.rs:23`](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L23)。

#### 4.1.4 代码实践（源码阅读型）

**实践目标**：亲手验证「编码准确、解码有损」这个结论。

**操作步骤**：

1. 打开 [`src/lib.rs:106-119`](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L106-L119) 的 `From<u8> for ErrorKind`。
2. 对每一个 `ErrorKind` 变体，先查 [`src/lib.rs:91-104`](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L91-L104) 得到它的 `as u8` 值。
3. 再回到 `From<u8>`，判断这个字节能否被还原回**同一个**变体。

**需要观察的现象**：填一张表，标出哪些变体「编码→解码」会失真。

**预期结果**（基于源码分析）：

| 变体 | `as u8` | `from(u8)` 还原为 | 是否一致 |
|------|---------|-------------------|----------|
| `Timeout` | `0x78` | `Other` | ❌ |
| `Eof` | `0xff` | `Other` | ❌ |
| 其余 8 个 | `0x71`–`0x75`,`0x76`,`0x77`,`0x79` | 自身 | ✅ |

> 说明：`Other`（`0x74`）恰好也落到 `_ => Other`，所以它「看起来一致」其实是兜底的结果，并不是因为有专门分支。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ErrorKind::Timeout as u8` 是 `0x78`，但 `ErrorKind::from(0x78)` 得到的不是 `Timeout`？

> **答案**：`ErrorKind` 带 `#[repr(u8)]`，`Timeout = ERR_TIMEOUT = 0x78`，所以 `as u8` 精确得到 `0x78`；但反向的 `From<u8>` 在 `match` 里没有 `ERR_TIMEOUT` 分支，落入 `_ => ErrorKind::Other`，因此还原不出 `Timeout`。

**练习 2**：`Eof`（`0xff`）这种错误通常在什么情形下产生？

> **答案**：它是一个**本地**错误码（没有 `ERR_EOF` 线上常量）。当读到 socket 的 `UnexpectedEof` / `BrokenPipe` / `ConnectionReset`，或事件通道（`async_channel::SendError`、`oneshot::RecvError`）关闭时，本地代码会构造 `ErrorKind::Eof`，表示「对端连接结束」。

---

### 4.2 服务质量 QoS

#### 4.2.1 概念说明

「服务质量（Quality of Service）」描述发送一条消息时，你愿意付出多少代价换取多少可靠性/实时性。BUS/RT 把它设计成极简的 4 档，背后是**两个相互独立的二进制标志位**：

- **processed / ack 位**：是否要求代理「确认已经处理」这条消息。开启后，发送方会等待代理回送 `OP_ACK`，可靠但慢。
- **realtime 位**：是否按实时优先级处理。开启后，消息会被立即刷新出站（而不是攒在缓冲里），延迟更低、对实时运行时更友好。

README 的基准测试直观体现了 ack 位的代价（[`README.md:74-77`](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/README.md#L74-L77)）：`send.qos.no` 约 274 万次/秒，而 `send.qos.processed` 只有约 18 万次/秒——差了一个数量级，原因就是要等 ACK。

#### 4.2.2 核心流程

`QoS` 的四个值就是把两个位组合起来。设 `qos_byte` 为枚举的 `u8` 值，\( b_0 \) 为最低位（ack），\( b_1 \) 为次低位（realtime）：

\[
\text{qos\_byte} = b_1 \cdot 2 + b_0
\]

判断方法完全是位掩码：

\[
\text{needs\_ack} \iff b_0 = 1 \iff \text{qos\_byte} \mathbin{\&} 0\mathrm{b}01 \neq 0
\]

\[
\text{is\_realtime} \iff b_1 = 1 \iff \text{qos\_byte} \mathbin{\&} 0\mathrm{b}10 \neq 0
\]

于是得到真值表：

| QoS | `as u8` | 二进制 | `needs_ack()` | `is_realtime()` | 含义 |
|-----|---------|--------|---------------|-----------------|------|
| `No` | 0 | `0b00` | `false` | `false` | 发出去就完事，最快 |
| `Processed` | 1 | `0b01` | `true`  | `false` | 要等代理确认处理 |
| `Realtime` | 2 | `0b10` | `false` | `true`  | 实时优先，不等确认 |
| `RealtimeProcessed` | 3 | `0b11` | `true`  | `true`  | 实时 + 要确认 |

两个位相互正交，所以 `RealtimeProcessed = Realtime + Processed`，这正是位运算带来的简洁。

> QoS 如何影响后续行为（在后续讲义展开）：`needs_ack` 为真时，发送方法会返回一个 `OpConfirm`（一次性确认通道），代理处理完后回送 `OP_ACK`；`is_realtime` 为真时，出站写入走「立即刷新」而非「定时批量刷新」（见 u4-l3 的 `TtlBufWriter`）。

#### 4.2.3 源码精读

**QoS 枚举**：

```rust
#[derive(Debug, Copy, Clone)]
#[repr(u8)]
pub enum QoS {
    No = 0,
    Processed = 1,
    Realtime = 2,
    RealtimeProcessed = 3,
}
```

见 [`src/lib.rs:352-359`](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L352-L359)。注意它只派生了 `Debug, Copy, Clone`，**没有**派生 `Eq/PartialEq`——这是它和 `ErrorKind`/`FrameOp`/`FrameKind` 的一个小差异（后三者都派生了 `Eq, PartialEq`），意味着你不能直接对两个 `QoS` 做 `==` 比较。

**位运算判断**：

```rust
impl QoS {
    pub fn is_realtime(self) -> bool {
        self as u8 & 0b10 != 0
    }
    pub fn needs_ack(self) -> bool {
        self as u8 & 0b1 != 0
    }
}
```

见 [`src/lib.rs:361-370`](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L361-L370)。`self as u8` 把枚举转成字节，再与掩码做按位与——一次运算即可判定，无需 `match`。

**反向解析**：

```rust
impl TryFrom<u8> for QoS {
    type Error = Error;
    fn try_from(q: u8) -> Result<Self, Error> {
        match q {
            0 => Ok(QoS::No),
            1 => Ok(QoS::Processed),
            2 => Ok(QoS::Realtime),
            3 => Ok(QoS::RealtimeProcessed),
            _ => Err(Error::data(format!("Invalid QoS: {}", q))),
        }
    }
}
```

见 [`src/lib.rs:372-383`](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L372-L383)。只有 `0..=3` 合法，其它值返回 `ErrorKind::Data` 错误。

**真实用法**——订阅时指定 QoS（来自示例）：

```rust
let opc = client.subscribe("#", QoS::Processed).await?.expect("no op");
opc.await??;   // 因为 needs_ack，这里等待代理确认订阅成功
```

见 [`examples/client_listener.rs:13-14`](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/examples/client_listener.rs#L13-L14)。点对点发送用 `QoS::No` 的例子见 [`examples/inter_thread.rs:31-34`](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/examples/inter_thread.rs#L31-L34)。

#### 4.2.4 代码实践（可运行）

**实践目标**：亲手运行程序，打印 `QoS` 的真值表，并对照 `ErrorKind` 的 `u8` 表示验证错误码与协议常量的对应关系（含 4.1 提到的有损解码）。

由于这些类型不受 feature 门控，我们可以用一个**几乎零依赖**的小 crate 来验证。

**操作步骤**：

1. 新建一个独立 crate（不修改 busrt 仓库本身）：

   ```bash
   cargo new qos-demo
   cd qos-demo
   cargo add busrt --no-default-features
   ```

2. 把下面这段**示例代码**写入 `src/main.rs`：

   ```rust
   // 示例代码：演示 QoS 位语义与 ErrorKind 错误码映射
   use busrt::{ErrorKind, QoS};

   fn main() {
       // 1) QoS 真值表：bit0 = processed(ack)，bit1 = realtime
       println!("== QoS 真值表 ==");
       for qos in [
           QoS::No,
           QoS::Processed,
           QoS::Realtime,
           QoS::RealtimeProcessed,
       ] {
           println!(
               "  {:<18} as u8 = {} (0b{:04b}) | needs_ack={:<5} is_realtime={}",
               format!("{:?}", qos),
               qos as u8,
               qos as u8,
               qos.needs_ack(),
               qos.is_realtime(),
           );
       }

       // 2) ErrorKind <-> 线上错误码（ERR_* 常量）的对应
       println!("\n== ErrorKind 错误码映射 ==");
       for kind in [
           ErrorKind::NotRegistered,
           ErrorKind::NotSupported,
           ErrorKind::Io,
           ErrorKind::Timeout,
           ErrorKind::Data,
           ErrorKind::Busy,
           ErrorKind::NotDelivered,
           ErrorKind::Access,
           ErrorKind::Other,
           ErrorKind::Eof,
       ] {
           let code = kind as u8;
           let decoded = ErrorKind::from(code); // 模拟从线上字节反解
           let mark = if decoded != kind { "  <-- 编解码不一致！" } else { "" };
           println!(
               "  {:<14} code=0x{:02X}  线上反解->{:<13}{}",
               format!("{:?}", kind),
               code,
               format!("{:?}", decoded),
               mark,
           );
       }
   }
   ```

3. 运行：`cargo run`（首次会从 crates.io 拉取 `busrt` 及其少量非可选依赖）。

**需要观察的现象**：QoS 四行的 `needs_ack`/`is_realtime` 是否符合 4.2.2 的真值表；ErrorKind 表里哪几行出现「编解码不一致」。

**预期结果**（基于源码分析，待本地验证）：

```text
== QoS 真值表 ==
  No                 as u8 = 0 (0b0000) | needs_ack=false is_realtime=false
  Processed          as u8 = 1 (0b0001) | needs_ack=true  is_realtime=false
  Realtime           as u8 = 2 (0b0010) | needs_ack=false is_realtime=true
  RealtimeProcessed  as u8 = 3 (0b0011) | needs_ack=true  is_realtime=true

== ErrorKind 错误码映射 ==
  NotRegistered   code=0x71  线上反解->NotRegistered
  NotSupported    code=0x75  线上反解->NotSupported
  Io              code=0x73  线上反解->Io
  Timeout         code=0x78  线上反解->Other          <-- 编解码不一致！
  Data            code=0x72  线上反解->Data
  Busy            code=0x76  线上反解->Busy
  NotDelivered    code=0x77  线上反解->NotDelivered
  Access          code=0x79  线上反解->Access
  Other           code=0x74  线上反解->Other
  Eof             code=0xFF  线上反解->Other          <-- 编解码不一致！
```

如果本地输出与此不符，请优先以你本地的实际输出为准，并回头核对 `src/lib.rs` 的 `From<u8>` 分支。

#### 4.2.5 小练习与答案

**练习 1**：要发送一条「既需要代理确认处理、又是实时优先」的消息，应该用哪个 `QoS`？它的 `as u8` 是多少？

> **答案**：`QoS::RealtimeProcessed`，`as u8 = 3`（`0b11`），`needs_ack()` 与 `is_realtime()` 都为 `true`。

**练习 2**：为什么 `needs_ack` / `is_realtime` 用位运算判断，而不是用 `match` 罗列四种情况？

> **答案**：因为 ack 与 realtime 是两个**正交独立**的标志位，用位掩码（`& 0b1`、`& 0b10`）一次运算即可判定某一维，逻辑清晰且天然支持组合；用 `match` 反而要把四种组合都列一遍，且新增维度时改动更大。

---

### 4.3 帧操作码与帧类型：FrameOp 与 FrameKind

#### 4.3.1 概念说明

读 BUS/RT 源码时，你会遇到两个长得很像的枚举：`FrameOp` 和 `FrameKind`。它们都复用同一批 `OP_*` 常量，但**职责不同**：

- **`FrameOp`**：客户端**请求代理执行的操作**（动词）。比如「订阅主题」「点对点发消息」「发布」。它包含一批**控制类操作**（订阅/取消订阅/排除），这些操作本身不会变成发给其他客户端的数据帧。
- **`FrameKind`**：一个帧**本身是什么类型**（名词/标签）。它是 `FrameData` 的 `kind` 字段，描述「这一坨数据是消息、广播、发布、还是 ACK」。它额外包含两个 `FrameOp` 没有的特殊值：`Prepared`（本地构造、尚未上线的帧）和 `Acknowledge`（代理回送的 ACK）。

一句话区分：`FrameOp` 回答「客户端想让代理做什么」，`FrameKind` 回答「这个帧是什么」。

#### 4.3.2 核心流程

同一个字节在协议里有两种解读视角：

```text
                  共享的 OP_* 常量
                         │
        ┌────────────────┼────────────────┐
        ▼                                 ▼
   FrameOp 视角                     FrameKind 视角
  （客户端→代理 的操作）            （帧本身的类型标签）
  含控制操作：subscribe/...         含 Prepared(0xff) / Acknowledge(0xFE)
```

- 代理**读取**来自客户端的字节时，关心的是「客户端请求了什么操作」→ 偏 `FrameOp` 语义。
- 代理**构造**要分发给订阅者的 `FrameData` 时，要给它打上类型标签 → 用 `FrameKind`。

> 说明：两种视角的精确解析发生在 `broker.rs` / `ipc.rs`（见 u2-l3 线上协议、u6-l1 连接生命周期）。本讲只需建立「同字节、两视角」的概念。

#### 4.3.3 源码精读

**`FrameOp` 枚举**——客户端可请求的全部操作：

```rust
#[repr(u8)]
pub enum FrameOp {
    Nop = OP_NOP,                       // 0x00 空操作
    Message = OP_MESSAGE,               // 0x12 点对点消息
    Broadcast = OP_BROADCAST,           // 0x13 广播
    PublishTopic = OP_PUBLISH,          // 0x01 发布到主题
    PublishTopicFor = OP_PUBLISH_FOR,   // 0x06 发布变体（由 broker 处理）
    SubscribeTopic = OP_SUBSCRIBE,      // 0x02 订阅
    UnsubscribeTopic = OP_UNSUBSCRIBE,  // 0x03 取消订阅
    ExcludeTopic = OP_EXCLUDE,          // 0x04 排除主题
    UnexcludeTopic = OP_UNEXCLUDE,      // 0x05 取消排除
}
```

见 [`src/lib.rs:318-332`](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L318-L332)。注意源码里有一段注释（[`src/lib.rs:329-331`](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L329-L331)）解释为什么「取消排除」叫 `Unexclude` 而不是 `Include`：因为 `include` 容易和 `subscribe` 混淆，而 `unexclude` 明确表示「排除」的逆操作。

`PublishTopicFor`（`OP_PUBLISH_FOR = 0x06`）是发布的一种变体，其精确分发语义在 broker 内部处理，将在 u3/u6 讲义展开，本讲不展开猜测。

**`FrameKind` 枚举**——帧的类型标签：

```rust
#[repr(u8)]
pub enum FrameKind {
    Prepared = 0xff,        // 本地构造的帧（尚未上线），sender 为 None
    Message = OP_MESSAGE,   // 0x12
    Broadcast = OP_BROADCAST, // 0x13
    Publish = OP_PUBLISH,   // 0x01
    Acknowledge = OP_ACK,   // 0xFE 代理回送的 ACK
    Nop = OP_NOP,           // 0x00
}
```

见 [`src/lib.rs:385-394`](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L385-L394)。两个独有值：
- `Prepared = 0xff`：一个哨兵值，标记「这帧是本地代码构造出来的，不是从线上收到的」。它的 `sender` 字段为 `None`。
- `Acknowledge = OP_ACK = 0xFE`：代理对 `needs_ack` 消息的确认回送（呼应 4.2 的 `Processed`）。

**对照关系**（`OP_*` 常量见 [`src/lib.rs:10-19`](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L10-L19)）：

| 字节 | `FrameOp` | `FrameKind` | 说明 |
|------|-----------|-------------|------|
| `0x00` | `Nop` | `Nop` | 共有 |
| `0x01` | `PublishTopic` | `Publish` | 共有（名字不同） |
| `0x12` | `Message` | `Message` | 共有 |
| `0x13` | `Broadcast` | `Broadcast` | 共有 |
| `0x02`–`0x06`（除 0x01） | subscribe/exclude/for 等 | — | 仅 `FrameOp`（控制操作） |
| `0xFE` | — | `Acknowledge` | 仅 `FrameKind`（ACK 回送） |
| `0xff` | — | `Prepared` | 仅 `FrameKind`（本地哨兵） |

两个枚举的反向解析 [`TryFrom<u8> for FrameOp`](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L334-L350) 与 [`TryFrom<u8> for FrameKind`](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L396-L408) 都对未知字节返回 `ErrorKind::Data` 错误。

#### 4.3.4 代码实践（源码阅读型）

**实践目标**：亲手把 `FrameOp` 与 `FrameKind` 的差异整理成清单。

**操作步骤**：

1. 打开 [`src/lib.rs:318-332`](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L318-L332)（FrameOp）与 [`src/lib.rs:385-394`](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L385-L394)（FrameKind）。
2. 列出：哪些字节值两个枚举都有；哪些只有 `FrameOp`；哪些只有 `FrameKind`。

**需要观察的现象**：控制类操作（subscribe 等）只出现在 `FrameOp`；`Prepared` 与 `Acknowledge` 只出现在 `FrameKind`。

**预期结果**：与 4.3.3 的对照表一致。

#### 4.3.5 小练习与答案

**练习 1**：`FrameKind` 有而 `FrameOp` 没有的两个变体是什么？分别有什么用途？

> **答案**：`Prepared`（`0xff`）标记本地构造、尚未上线的帧（`sender` 为 `None`）；`Acknowledge`（`OP_ACK = 0xFE`）是代理对需要确认的消息（`QoS::Processed`）回送的 ACK 帧。

**练习 2**：客户端「订阅主题」用哪个 `FrameOp`？它在 `FrameKind` 里有对应变体吗？为什么？

> **答案**：用 `FrameOp::SubscribeTopic`（`OP_SUBSCRIBE = 0x02`）。`FrameKind` 里**没有**对应变体，因为「订阅」是客户端对代理的**控制操作**，不会变成一坨分发给别人的数据帧，所以它不属于「帧类型」。

---

### 4.4 帧数据与帧句柄：FrameData 与 Frame

#### 4.4.1 概念说明

前面三个模块讲的是「分类标签」（错误种类、服务质量、操作/帧类型），本模块讲真正承载内容的容器——`FrameData`，以及它的轻量句柄 `Frame`。

`FrameData` 的设计围绕两个性能目标：

1. **零拷贝**：从线上读进来的一整块缓冲 `buf`，可以直接复用，不把「头部」和「负载」拆成两个 `Vec` 再拼接。它用一个 `payload_pos` 偏移量标记「真实负载从 `buf` 的哪里开始」。
2. **廉价扇出**：广播/发布订阅时，同一条消息要发给 N 个订阅者。如果每次都复制整个 `buf`，代价巨大。于是定义 `Frame = Arc<FrameData>`，克隆 `Frame` 只是引用计数 +1，所有订阅者共享同一块内存。

#### 4.4.2 核心流程

`FrameData` 的关键字段协作方式：

```text
              buf:  [ H E A D E R   P A Y L O A D ]
                                 ^
                                 payload_pos
header (Option) ──┐              │
                  │              │
                  ▼              ▼
        payload() = &buf[payload_pos..]   ← 零拷贝切片，不新建 Vec

  sender / topic / kind / realtime       ← 元数据
```

分发时的引用计数：

```text
   broker 收到一帧
        │  构造 FrameData，包成 Arc<FrameData>  (= Frame)
        ▼
   对每个订阅者：frame.clone()  ──► 只增加 Arc 引用计数
        │                              （不复制 buf）
        ▼
   各订阅者拿到同一份 FrameData 的共享引用
```

#### 4.4.3 源码精读

**`FrameData` 结构体**：

```rust
#[derive(Debug)]
pub struct FrameData {
    kind: FrameKind,
    sender: Option<String>,        // 发送者名；Prepared/Nop 帧为 None
    topic: Option<String>,         // pub/sub 主题
    header: Option<Vec<u8>>,       // 零拷贝负载前缀（IPC 为 None，线程内为 Some）
    buf: Vec<u8>,                  // 完整入站缓冲
    payload_pos: usize,            // 真实负载在 buf 中的起点
    realtime: bool,                // 由发送方 QoS 传递而来
}
```

见 [`src/lib.rs:410-419`](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L410-L419)。各字段含义：

- `kind`：帧类型标签（4.3 的 `FrameKind`）。
- `sender`：原始发送者名字；**本地构造的帧（`Prepared`/`new_nop`）为 `None`**。
- `topic`：仅 pub/sub 通信有值。
- `header`：零拷贝的「负载前缀」。IPC 通信时为 `None`，线程内通信时为 `Some`——自定义协议层（如 RPC）用它携带元数据，避免拷贝负载（详见 [`src/lib.rs:488-495`](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L488-L495) 的注释）。
- `buf` + `payload_pos`：零拷贝核心，`payload()` 直接切片。

**关键方法**：

```rust
pub fn payload(&self) -> &[u8] {
    &self.buf[self.payload_pos..]   // 零拷贝：从偏移量切片
}

/// # Panics：sender 为 None（Prepared/Nop 帧）时 panic
pub fn sender(&self) -> &str {
    self.sender.as_ref().unwrap()
}

pub fn primary_sender(&self) -> &str {
    let primary_sender = self.sender.as_ref().unwrap();
    if let Some(pos) = primary_sender.find(SECONDARY_SEP) {  // "%%"
        &primary_sender[..pos]
    } else {
        primary_sender
    }
}

pub fn is_realtime(&self) -> bool { self.realtime }
```

分别见 [`payload()`(src/lib.rs:484-487)](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L484-L487)、[`sender()`(src/lib.rs:461-464)](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L461-L464)、[`primary_sender()`(src/lib.rs:468-476)](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L468-L476)、[`is_realtime()`(src/lib.rs:496-499)](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L496-L499)。

两个要点：

1. `sender()` 在 `sender` 为 `None` 时会 **panic**。文档说「prepared 帧会 panic」，但严格来说只要 `sender` 是 `None`（包括 `new_nop()`）就会 panic（见 [`src/lib.rs:458-464`](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L458-L464)）。
2. `primary_sender()` 会剥掉 [`SECONDARY_SEP`](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L49)（`"%%"`），例如 `"worker.1%%0"` → `"worker.1"`。这与「二级客户端」机制有关（u3-l2 详讲）。

**两个构造器**：完整的 [`FrameData::new(...)`](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L422-L441) 和便捷的 [`FrameData::new_nop()`](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L442-L453)（构造一个空 Nop 帧）。

**轻量句柄 `Frame`**：

```rust
pub type Frame = Arc<FrameData>;
pub type EventChannel = async_channel::Receiver<Frame>;
```

见 [`src/lib.rs:71-72`](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L71-L72)。`Frame` 只是 `Arc<FrameData>` 的别名；`EventChannel` 是客户端接收帧的异步通道，传的就是 `Frame`。这就是「广播/发布订阅时克隆句柄而非拷贝缓冲」的实现基础。

**真实用法**——示例里通过 `Frame` 调用各方法：

```rust
while let Ok(frame) = rx.recv().await {
    println!(
        "Frame from {}: {:?} {:?} {}",
        frame.sender(),                                  // &str
        frame.kind(),                                    // FrameKind
        frame.topic(),                                   // Option<&str>
        std::str::from_utf8(frame.payload()).unwrap_or("..."), // &[u8]
    );
}
```

见 [`examples/client_listener.rs:17-25`](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/examples/client_listener.rs#L17-L25)。注意 `frame` 是 `Frame`（即 `Arc<FrameData>`），但调用的是 `FrameData` 的方法（`Arc` 自动解引用）。

#### 4.4.4 代码实践（源码阅读型）

**实践目标**：理解 `payload_pos` 的零拷贝含义与 `Arc` 扇出的好处。

**操作步骤**：

1. 读 [`payload()`(src/lib.rs:484-487)](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L484-L487)：确认它只是 `&self.buf[self.payload_pos..]`，没有新建 `Vec`。
2. 读 [`Frame` 类型别名(src/lib.rs:71)](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L71)：确认它是 `Arc<FrameData>`。
3. 思考：若广播给 100 个订阅者，用 `Frame`（`Arc`）相比「每次复制 `FrameData`」节省了什么。

**需要观察的现象**：`payload()` 返回的是对内部 `buf` 的切片引用；`Frame` 是 `Arc`。

**预期结果**：`payload()` 零拷贝；100 个订阅者共享同一块 `buf`，只增加 100 次引用计数，而非复制 100 份负载。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `Frame` 定义为 `Arc<FrameData>`，而不是直接用 `FrameData`？

> **答案**：广播/发布订阅要把同一条消息分发给多个订阅者。`Arc::clone` 只增加引用计数，不复制内部的 `buf`；若直接传 `FrameData`（非 `Clone` 的廉价复制），每个订阅者都要拷贝整块缓冲，开销随订阅者数线性增长。

**练习 2**：`payload()` 返回 `&self.buf[self.payload_pos..]`，这为什么体现了零拷贝？

> **答案**：`buf` 保存的是**完整入站缓冲**，`payload_pos` 标记真实负载起点。`payload()` 只是从同一块内存切出一个子切片引用，不分配新的 `Vec`，因此「头部 + 负载」可以共处一个 `buf` 而无需拼接拷贝。

---

## 5. 综合实践

把本讲的四个模块串起来：写一个程序，既构造并检查 `FrameData`（验证零拷贝切片、`primary_sender` 剥离 `%%`、`sender()` 在空帧上 panic 的行为、`Arc` 共享），又打印 `QoS` 真值表与 `ErrorKind` 映射。

继续在 4.2.4 建好的 `qos-demo` crate 里，把 `src/main.rs` 替换为下面这段**示例代码**：

```rust
// 示例代码：综合实践 —— FrameData 构造 + QoS 真值表 + ErrorKind 映射
use busrt::{ErrorKind, FrameData, FrameKind, QoS};
use std::sync::Arc;

fn main() {
    // ---- A. FrameData 的零拷贝与元数据 ----
    // buf = "HEADER-PAYLOAD"，payload_pos = 6 → payload = "PAYLOAD"
    let frame_data = FrameData::new(
        FrameKind::Message,
        Some("worker.1%%0".into()), // 二级客户端名
        Some("news/tech".into()),
        None,
        b"HEADER-PAYLOAD".to_vec(),
        6,
        true,
    );
    assert_eq!(frame_data.kind(), FrameKind::Message);
    assert_eq!(frame_data.payload(), b"PAYLOAD");      // 零拷贝切片
    assert_eq!(frame_data.topic(), Some("news/tech"));
    assert_eq!(frame_data.primary_sender(), "worker.1"); // 剥掉 %%0
    assert!(frame_data.is_realtime());
    println!("FrameData.payload  = {:?}", std::str::from_utf8(frame_data.payload()).unwrap());
    println!("FrameData.primary_sender = {}", frame_data.primary_sender());

    // ---- B. Arc 句柄共享 ----
    let frame: Arc<FrameData> = Arc::new(frame_data);
    let cloned = frame.clone();
    assert!(Arc::ptr_eq(&frame, &cloned)); // 同一块内存，仅引用计数增加
    println!("Arc ptr_eq (共享同一帧) = {}", Arc::ptr_eq(&frame, &cloned));

    // ---- C. new_nop 与 sender() panic 行为 ----
    let nop = FrameData::new_nop();
    assert_eq!(nop.kind(), FrameKind::Nop);
    assert!(nop.payload().is_empty());
    // nop.sender(); // ⚠️ 取消注释会 panic：sender 为 None
    println!("new_nop().kind = {:?}, payload 为空 = {}", nop.kind(), nop.payload().is_empty());

    // ---- D. QoS 真值表 ----
    println!("\n== QoS 真值表 ==");
    for qos in [QoS::No, QoS::Processed, QoS::Realtime, QoS::RealtimeProcessed] {
        println!(
            "  {:<18} u8={} needs_ack={} is_realtime={}",
            format!("{:?}", qos), qos as u8, qos.needs_ack(), qos.is_realtime(),
        );
    }

    // ---- E. ErrorKind 映射（含编解码不一致） ----
    println!("\n== ErrorKind 不一致的变体 ==");
    for kind in [ErrorKind::Timeout, ErrorKind::Eof] {
        println!("  {:?}: as u8=0x{:02X}, from(u8)={:?}", kind, kind as u8, ErrorKind::from(kind as u8));
    }
}
```

**操作步骤**：

1. 确保 `busrt` 仍以 `--no-default-features` 引入（本程序只用到不受 feature 门控的类型）。
2. `cargo run`。
3. 把第 C 部分的 `nop.sender();` 取消注释，再 `cargo run`，观察 panic。

**需要观察的现象**：

- `payload()` 输出 `PAYLOAD`（验证 `payload_pos` 切片）。
- `primary_sender()` 输出 `worker.1`（验证 `%%` 剥离）。
- `Arc::ptr_eq` 为 `true`（验证共享）。
- 取消注释后程序因 `sender()` 在 `None` 上 `unwrap` 而 panic。

**预期结果**（基于源码分析，待本地验证）：前述断言全部通过、程序正常打印；取消注释 `nop.sender()` 后 panic，报错信息类似于 `called Option::unwrap() on a None value`。

## 6. 本讲小结

- `ErrorKind` 带 `#[repr(u8)]`，每个变体的判别值就是线上 `ERR_*` 字节；`kind as u8` 编码永远精确，但 `From<u8>` 解码**有损**：`Timeout`(0x78) 与 `Eof`(0xff) 都会还原成 `Other`。
- `QoS` 是两个正交位的组合：低位 `0b01`=需要 ACK（`Processed`），高位 `0b10`=实时（`Realtime`）；`needs_ack()`/`is_realtime()` 用位掩码判断。ACK 会让吞吐下降约一个数量级。
- `FrameOp`（客户端请求的操作，含 subscribe/exclude 等控制操作）与 `FrameKind`（帧的类型标签，含独有的 `Prepared` 与 `Acknowledge`）复用同一批 `OP_*` 字节，但视角不同。
- `FrameData` 用 `buf` + `payload_pos` 实现零拷贝负载切片；`header` 字段让线程内/RPC 层携带元数据而免拷贝。
- `Frame = Arc<FrameData>` 让广播/发布订阅通过「克隆引用计数」而非「复制缓冲」完成廉价扇出。
- 这四类核心类型都不受 Cargo feature 门控，是整个库始终存在的公共契约。

## 7. 下一步学习建议

本讲建立了「类型与标签」的基础，接下来的两讲会把这些类型放进真实的字节流和内存模型里：

- **u2-l2 零拷贝载荷模型：`borrow::Cow`**：本讲的 `payload()` 返回 `&[u8]`，而发送方传入的负载用的是 `Cow`（`"hello".as_bytes().into()` 就用到了它）。下一讲讲清 `Borrowed`/`Owned`/`Referenced` 三种变体如何在不同来源间实现零拷贝。
- **u2-l3 线上协议与帧格式**：本讲的 `FrameOp`/`FrameKind`/`ERR_*`/`RESPONSE_OK`/`GREETINGS` 都会出现在真实的握手与帧头字节布局里。下一讲会画出「客户端发送帧」与「代理回送帧」的字节级结构，把本讲的常量落到具体偏移上。

建议在进入下一讲前，先完成 4.2.4 的可运行实践，确认你亲手见过 `QoS` 真值表与 `ErrorKind` 的编解码不一致——这会让后续读 `broker.rs` / `ipc.rs` 的握手与分发代码时事半功倍。
