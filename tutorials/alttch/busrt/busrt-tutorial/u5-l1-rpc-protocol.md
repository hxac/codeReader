# RPC 协议与 RpcEvent 解析

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清 BUS/RT 的 RPC 层在「字节层面」是如何编码的：四种帧类型（通知 / 请求 / 回复 / 错误）各自的字节布局。
- 理解一个 `Frame` 是如何被解析成 `RpcEvent` 的，包括 `id()`、`method()`、`payload()`、`code()` 各自从哪几个字节切片得到。
- 弄懂 RPC 协议里最关键的两个设计：`id == 0` 表示「不需要回复」，以及「零拷贝头」（`use_header`）如何让线程内通信免去字节拼接。
- 看懂 `prepare_call_payload` 如何拼出一次调用的请求头，以及 `RpcError` 的错误码体系。

本讲只讲 **协议与解析**（`src/rpc/mod.rs`）。至于「客户端怎么发起调用、处理器怎么注册」属于下一讲 u5-l2 的内容，本讲只在必要处点到。

## 2. 前置知识

本讲承接 u2-l1（核心类型 `Frame`/`FrameData`）与 u4-l2（`ipc::Client` 帧收发）。在进入正文前，请先回忆两件事：

1. **`Frame` 是什么。** 在 [src/lib.rs:71](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L71) 有 `pub type Frame = Arc<FrameData>;`，即一帧就是一个引用计数的 `FrameData`。`FrameData` 内部有三个本讲会反复用到的字段（见 [src/lib.rs:410-419](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L410-L419)）：

   - `buf: Vec<u8>` —— 完整的入站缓冲区。
   - `payload_pos: usize` —— 真正业务载荷在 `buf` 中的起始偏移；`payload()` 返回 `&buf[payload_pos..]`（[src/lib.rs:484-487](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L484-L487)）。
   - `header: Option<Vec<u8>>` —— 「零拷贝载荷前缀」。它的文档说得很清楚（[src/lib.rs:488-495](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L488-L495)）：**对 IPC（socket）通信 `header` 恒为 `None`；对线程内（inter-thread）通信 `header` 会被填充**。RPC 层正是利用这个字段来避免线程内通信的字节拷贝。

2. **一次调用有去有回。** RPC（Remote Procedure Call，远程过程调用）的本质是「我调用你的方法，你把结果还给我」。但 BUS/RT 还支持两种不需要回复的形式：纯通知（notification）和「发出去就完事」的 `call0`。本讲要回答的核心问题之一就是：**协议用什么机制区分「要回复」和「不要回复」？** 答案就是 `id` 字段。

> 小贴士：本讲里「小端序」（little-endian）会多次出现，意思是多字节整数在内存中「低位字节在前」。例如 `u32` 值 `1` 的小端字节序列是 `01 00 00 00`。BUS/RT 的整个线上协议都用小端序（见 u2-l3）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/rpc/mod.rs](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/mod.rs) | RPC 协议的核心：帧类型常量、错误码、`RpcEvent` 及其从 `Frame` 的解析、`RpcError`、`prepare_call_payload`。本讲几乎全部内容都在这里。 |
| [src/lib.rs](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs) | 提供 `Frame = Arc<FrameData>`、`FrameData`（含 `header`/`payload`/`payload_pos`）以及 `Error::data`。 |
| [src/rpc/async_client.rs](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/async_client.rs) | 仅作「印证」用：展示 `prepare_call_payload` 在 `call`/`call0` 中如何被调用，以及处理器收到请求后如何拼回复。细节留给 u5-l2。 |

## 4. 核心概念与源码讲解

### 4.1 RPC 帧类型常量与 RpcEventKind

#### 4.1.1 概念说明

RPC 层并不发明新的传输方式，而是**复用** u4 学到的 `Frame`（点对点 `Message` 帧）。它只是约定：一条 `Message` 帧的载荷（或零拷贝头）的第一个字节，用来标明「这是一次 RPC 的哪一种动作」。BUS/RT 一共定义了四种动作：

| 常量 | 值 | 含义 | 典型方向 |
| --- | --- | --- | --- |
| `RPC_NOTIFICATION` | `0x00` | 通知：单向，不需要回复 | 调用方 → 被调用方 |
| `RPC_REQUEST` | `0x01` | 请求：调用某方法，需要回复 | 调用方 → 被调用方 |
| `RPC_REPLY` | `0x11` | 回复：请求的成功结果 | 被调用方 → 调用方 |
| `RPC_ERROR` | `0x12` | 错误回复：请求失败 | 被调用方 → 调用方 |

这四个常量定义在 [src/rpc/mod.rs:10-13](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/mod.rs#L10-L13)。

它们被收进一个枚举 `RpcEventKind`，且 `#[repr(u8)]` 保证枚举判别值与上面的字节完全一致（[src/rpc/mod.rs:22-30](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/mod.rs#L22-L30)）：

```rust
#[repr(u8)]
pub enum RpcEventKind {
    Notification = RPC_NOTIFICATION, // 0x00
    Request = RPC_REQUEST,           // 0x01
    Reply = RPC_REPLY,               // 0x11
    ErrorReply = RPC_ERROR,          // 0x12
}
```

> 一个值得注意的细节：`RpcEventKind` 的 `Display` 实现里，`Notification` 分支被拼写成了 `"notifcation"`（少了字母 `i`，见 [src/rpc/mod.rs:38-51](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/mod.rs#L38-L51)）。这是源码里的真实拼写，如果你在日志里看到 `notifcation` 不要以为是日志库写错了——它就是从这里来的。

#### 4.1.2 核心流程

协议判定的流程非常简单：

1. 拿到一帧的「正文」（body，下文 4.3 详述 body 到底是 header 还是 payload）。
2. 读 `body[0]`，即第一个字节。
3. `0x00` → 通知；`0x01` → 请求；`0x11` → 回复；`0x12` → 错误回复；其它值 → 报 `Unsupported RPC frame code` 错误。

其余字节怎么切分，取决于帧类型，这正是 4.3 要讲的内容。

#### 4.1.3 源码精读

四个常量与错误码集中定义在文件开头（[src/rpc/mod.rs:10-20](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/mod.rs#L10-L20)）。其中 `RPC_ERROR_CODE_*` 是一组「约定俗成」的 `i16` 错误码，值的选取借鉴了 JSON-RPC 的错误码段（负数、`-32xxx`），用来在 `RPC_ERROR` 帧里携带机器可读的失败原因，例如方法未找到（`-32601`）、参数非法（`-32602`）、解析失败（`-32700`）等。

```rust
pub const RPC_NOTIFICATION: u8 = 0x00;
pub const RPC_REQUEST:       u8 = 0x01;
pub const RPC_REPLY:         u8 = 0x11;
pub const RPC_ERROR:         u8 = 0x12;

pub const RPC_ERROR_CODE_NOT_FOUND:             i16 = -32001;
pub const RPC_ERROR_CODE_PARSE:                 i16 = -32700;
pub const RPC_ERROR_CODE_INVALID_REQUEST:       i16 = -32600;
pub const RPC_ERROR_CODE_METHOD_NOT_FOUND:      i16 = -32601;
pub const RPC_ERROR_CODE_INVALID_METHOD_PARAMS: i16 = -32602;
pub const RPC_ERROR_CODE_INTERNAL:              i16 = -32603;
```

> 小练习（答案见 4.1.5）：为什么 `RPC_REPLY`/`RPC_ERROR` 被设计成 `0x11`/`0x12`，而不是紧挨着 `0x01` 的 `0x02`/`0x03`？（提示：观察低 4 位与高 4 位。）

#### 4.1.4 代码实践

**目标**：用 Rust 把四个类型常量打印出来，建立「常量名 ↔ 字节值 ↔ 语义」的直觉。

**步骤**（这是「源码阅读型 + 极小可运行示例」实践）：

1. 在一个启用了 `rpc` feature 的项目里（例如直接在 busrt 仓库里写一个 example，或 `cargo add busrt --features rpc`）写几行：
   ```rust
   // 示例代码：仅演示常量值，非项目原有代码
   use busrt::rpc::{RpcEventKind, RPC_NOTIFICATION, RPC_REQUEST, RPC_REPLY, RPC_ERROR};
   fn main() {
       println!("NOTIFICATION = 0x{:02x}", RPC_NOTIFICATION);
       println!("REQUEST      = 0x{:02x}", RPC_REQUEST);
       println!("REPLY        = 0x{:02x}", RPC_REPLY);
       println!("ERROR        = 0x{:02x}", RPC_ERROR);
       println!("{RpcEventKind::Request:?} as u8 = {}", RpcEventKind::Request as u8);
   }
   ```
   > 注意：`busrt::rpc` 下的这些常量与 `RpcEventKind` 是否以该路径公开，取决于版本与 feature；若路径不通，可直接对照 [src/rpc/mod.rs:10-30](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/mod.rs#L10-L30) 阅读取值。本步骤的输出**待本地验证**。

2. 对照 [src/rpc/mod.rs:22-30](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/mod.rs#L22-L30)，确认 `RpcEventKind::Request as u8` 确实等于 `0x01`。

**需要观察的现象**：四个常量是 `0x00 / 0x01 / 0x11 / 0x12`，且枚举的 `as u8` 与常量逐一对齐。

**预期结果**：打印输出与上表完全一致。

#### 4.1.5 小练习与答案

**练习 1**：`RPC_ERROR_CODE_METHOD_NOT_FOUND` 的值是多少？它在什么场景下被发送？
**答案**：`-32601`（[src/rpc/mod.rs:18](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/mod.rs#L18)）。当被调用方收到一个它没有实现的 RPC 方法名时，会用这个码构造 `RPC_ERROR` 帧回复（`RpcError::method`，见 4.4）。

**练习 2（承接 4.1.3 的小练习）**：为什么 reply/error 用 `0x11`/`0x12`？
**答案**：观察二进制：`0x01 = 0000_0001`，`0x11 = 0001_0001`，`0x12 = 0001_0010`。它们的**低 4 位**分别与 `request(1)` / 自身保持区别，而**高 4 位为 1** 暗示「这是一个响应类帧」。这是一种非强制的助记编码约定，便于人眼/调试器快速区分「请求方向」与「响应方向」。注意这并非协议强制语义——解析时仍是逐字节精确匹配（见 4.3）。

---

### 4.2 RpcEvent：从一帧到一次 RPC 事件

#### 4.2.1 概念说明

`Frame` 是「传输层」的概念（一串字节 + 元数据），而 `RpcEvent` 是「RPC 语义层」的概念（这次事件是通知还是请求？调用的方法名是什么？参数在哪？要不要回复？）。两者之间靠 `TryFrom<Frame> for RpcEvent` 这座桥转换（[src/rpc/mod.rs:139-207](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/mod.rs#L139-L207)）。

`RpcEvent` 本身非常薄（[src/rpc/mod.rs:53-60](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/mod.rs#L53-L60)）：它**不拷贝任何字节**，只是把原始 `Frame` 借在手里，外加两个解析时算出的偏移量。

```rust
pub struct RpcEvent {
    kind: RpcEventKind,   // 4.1 讲过的帧类型
    frame: Frame,         // 原始帧（Arc 引用计数，廉价持有）
    payload_pos: usize,   // RPC 参数在 frame.payload() 中的起始偏移
    use_header: bool,     // 走零拷贝头路径，还是走 IPC 单缓冲路径
}
```

四个字段配合各 getter 方法，实现「按需切片、零拷贝」。下表把每个 getter 与它读取的字节对应起来（以「正文 = body」统称，body 的来源见 4.3）：

| getter | 读取的字节 | 类型 |
| --- | --- | --- |
| `kind()` | 解析时已确定，直接返回字段 | `RpcEventKind` |
| `frame()` | 返回整个原始帧的引用 | `&Frame` |
| `sender()` / `primary_sender()` | 来自 `FrameData.sender`（传输层填的发送方名） | `&str` |
| `id()` | `body[1..5]`，按小端 `u32` 解 | `u32` |
| `method()` | 见下文，请求帧专属 | `&[u8]` |
| `code()` | `body[5..7]`，按小端 `i16` 解，仅错误帧 | `i16` |
| `payload()` | `frame.payload()[self.payload_pos..]` | `&[u8]` |

#### 4.2.2 核心流程

一次「收帧 → 解析 → 取值」的流程：

```
入站 Frame
   │
   ▼
RpcEvent::try_from(frame)          ← 4.3 的解析逻辑
   │   确定 kind、payload_pos、use_header
   ▼
RpcEvent
   │  .kind()      → 判类型，决定处理方式
   │  .id()        → 是否需要回复（id==0 即否）
   │  .method()    → 请求帧：要调用的方法名
   │  .payload()   → 方法参数（msgpack 字节）
   │  .code()      → 错误帧：失败码
   ▼
交给处理器（u5-l2 的 RpcHandlers）
```

#### 4.2.3 源码精读

**`id()`**（[src/rpc/mod.rs:86-97](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/mod.rs#L86-L97)）：读取 body 第 1~4 字节（跳过第 0 字节的类型码），按小端转 `u32`。它根据 `use_header` 决定从 `header` 还是 `payload` 取这 4 字节：

```rust
pub fn id(&self) -> u32 {
    u32::from_le_bytes(
        if self.use_header {
            &self.frame.header().unwrap()[1..5]
        } else {
            &self.frame.payload()[1..5]
        }
        .try_into()
        .unwrap(),
    )
}
```

**`is_response_required()`**（[src/rpc/mod.rs:98-101](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/mod.rs#L98-L101)）就是 `id != 0` 的语义化封装——这是本讲最重要的一个判断：

```rust
pub fn is_response_required(&self) -> bool {
    self.id() != 0
}
```

**`method()`**（[src/rpc/mod.rs:105-113](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/mod.rs#L105-L113)）只在请求帧上有意义。它的两条分支正好对应「零拷贝头」与「IPC 单缓冲」两种布局：

```rust
pub fn method(&self) -> &[u8] {
    if self.use_header {
        let header = self.frame.header.as_ref().unwrap();
        &header[5..header.len() - 1]   // 跳过前 5 字节(type+id)，去掉末尾的 0x00
    } else {
        &self.frame().payload()[5..self.payload_pos - 1] // 跳过 type+id，到 0x00 之前
    }
}
```

- 零拷贝头路径：方法名在 `header[5 .. len-1]`（开头 5 字节是 `type+id`，末尾 1 字节是 `0x00` 分隔符）。
- IPC 路径：方法名在 `payload[5 .. payload_pos-1]`（`payload_pos` 在解析时已被算成「`0x00` 之后第一个字节」的位置，所以 `payload_pos-1` 正好是那个 `0x00`）。

**`payload()`**（[src/rpc/mod.rs:79-82](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/mod.rs#L79-L82)）：注意它读的是 `frame.payload()`（传输层的载荷切片），再按 `self.payload_pos` 二次切片：

```rust
pub fn payload(&self) -> &[u8] {
    &self.frame().payload()[self.payload_pos..]
}
```

**`code()`**（[src/rpc/mod.rs:118-136](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/mod.rs#L118-L136)）：仅对错误帧返回 `body[5..7]` 的小端 `i16`，其它类型恒返回 `0`。

#### 4.2.4 代码实践

**目标**：在不启动网络的情况下，验证「`id==0` ⇒ 不需要回复」这条规则在协议层与处理器层的双重体现。

**步骤**：

1. 阅读 [src/rpc/mod.rs:98-101](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/mod.rs#L98-L101)，确认 `is_response_required()` 仅依赖 `id() != 0`。
2. 打开 [src/rpc/async_client.rs:182-186](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/async_client.rs#L182-L186)，看处理器收到 `Request` 时如何决定要不要准备回复通道：
   ```rust
   let ev = if id > 0 {
       Some((event.frame().sender().to_owned(), processor_client.clone()))
   } else {
       None
   };
   ```
3. 继续往下看 [src/rpc/async_client.rs:195-232](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/async_client.rs#L195-L232)，回复动作被包在 `if let Some((target, cl)) = ev` 里——即 `id==0` 时根本不会进入发送回复的分支。

**需要观察的现象**：协议层（`is_response_required`）与处理器层（`id > 0` 才建回复通道）用的是**同一个 `id` 判据**，两处语义一致。

**预期结果**：你能用一句话说清「`id==0` 为何不需要回复」——因为无论是协议层还是处理器，都把 `id != 0` 作为「有人等着拿回复」的唯一信号；`id==0` 表示调用方根本没登记等待，回包也无人接收。

#### 4.2.5 小练习与答案

**练习 1**：`RpcEvent` 持有 `frame: Frame`（即 `Arc<FrameData>`）。为什么不直接存 `FrameData`，而要存 `Arc`？
**答案**：为了让 `RpcEvent` 可以廉价地被克隆/移动到后台任务里处理（u5-l2 会看到处理器在 `tokio::spawn` 里运行），`Arc` 只增加一次引用计数，不复制缓冲字节；若直接存 `FrameData` 则每次传递都要拷贝整个 `buf`。

**练习 2**：`code()` 对非错误帧返回什么？为什么安全？
**答案**：返回 `0`（[src/rpc/mod.rs:122-135](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/mod.rs#L122-L135)）。它先用 `self.kind == RpcEventKind::ErrorReply` 守卫，只有错误帧才会去读 `body[5..7]`，因此不会对长度不足的请求/回复帧越界读 2 字节。

---

### 4.3 TryFrom\<Frame\>：四种帧的字节布局与解析

> 这是本讲的重头戏，也是综合实践（第 5 节）的前置。请重点掌握「IPC 路径（`use_header=false`）请求帧」的字节切分。

#### 4.3.1 概念说明

解析的第一步，是确定「正文 body」到底是 `header` 还是 `payload`。这个分叉是 BUS/RT 零拷贝模型的核心（[src/rpc/mod.rs:142-144](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/mod.rs#L142-L144)）：

```rust
let (body, use_header) = frame
    .header()
    .map_or_else(|| (frame.payload(), false), |h| (h, true));
```

- **线程内通信**：发送方（broker 的内部客户端 `zc_send`）把 RPC 控制字节（type+id+method+0x00）放进 `FrameData.header`，真正的参数放进 `payload`（见 [src/broker.rs:370-390](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L370-L390)，`header = Some(header.to_vec())`、`payload_pos = 0`）。于是 `frame.header()` 为 `Some`，body = header，`use_header = true`，**参数天然就在 `payload` 里，无需任何切片与拷贝**。
- **IPC 通信**：socket 上只能传一条字节流，所以 RPC 控制字节和参数被拼在同一个缓冲里，`FrameData.header` 为 `None`（见 [src/lib.rs:488-495](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L488-L495) 的文档）。于是 body = payload，`use_header = false`，**解析时必须在 payload 里用 `0x00` 作分隔符找出 method 与 params 的边界**。

一句话总结：`use_header` 是「能不能不拷贝」的开关。线程内可以（控制字节和参数本来就分开放），IPC 不行（全挤在一条流里）。

#### 4.3.2 核心流程：四种帧的字节布局

下面统一用「IPC 路径（`use_header=false`）」来画字节图，因为这是综合实践要你手绘的版本。所有多字节整数均为小端。

**① 通知帧 `RPC_NOTIFICATION`（至少 1 字节）**

```
偏移: 0        1...........N
     [0x00]   [ params... ]
      type    参数(可为空)
```
- 解析（[src/rpc/mod.rs:156-161](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/mod.rs#L156-L161)）：`payload_pos = usize::from(!use_header)`。IPC 路径下 `!false = true` → `payload_pos = 1`，即参数从 `payload[1..]` 开始（跳过 type 字节）。没有 id、没有 method、不需要回复。

**② 请求帧 `RPC_REQUEST`（至少 6 字节：type + id(4) + 至少 1 字节 method 或 0x00）**

```
偏移: 0       1..4        5.........5+m-1   5+m       6+m..........N
     [0x01]  [id u32 LE]  [ method 字节 ]   [0x00]    [ params... ]
      type    调用id         方法名         分隔符      方法参数
```
- 解析（[src/rpc/mod.rs:162-184](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/mod.rs#L162-L184)）：先 `check_len!(6)` 保证至少有 type+id；然后对 `body[5..]` 用 `splitn(2, |c| *c == 0)` 按**第一个 `0x00`**切成两段——第一段是 method，第二段是 params。设 method 长度为 `m`，则 `payload_pos = 6 + m`（即 `0x00` 之后第一字节的位置）。
  - `id()` → `body[1..5]`
  - `method()` → `body[5 .. 5+m]`（即 `payload[5 .. payload_pos-1]`）
  - `payload()` → `body[6+m ..]`（即 `payload[payload_pos..]`）

**③ 回复帧 `RPC_REPLY`（至少 5 字节：type + id(4)）**

```
偏移: 0       1..4         5..........N
     [0x11]  [id u32 LE]   [ result... ]
      type    对应请求id     结果参数
```
- 解析（[src/rpc/mod.rs:185-193](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/mod.rs#L185-L193)）：`check_len!(5)`；IPC 路径 `payload_pos = 5`，跳过 type+id，结果就是 `payload[5..]`。

**④ 错误帧 `RPC_ERROR`（至少 7 字节：type + id(4) + code(2)）**

```
偏移: 0       1..4         5..6          7..........N
     [0x12]  [id u32 LE]  [code i16 LE]  [ error data... ]
      type    对应请求id     错误码          错误详情
```
- 解析（[src/rpc/mod.rs:194-202](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/mod.rs#L194-L202)）：`check_len!(7)`；IPC 路径 `payload_pos = 7`，跳过 type+id+code，错误详情是 `payload[7..]`；`code()` 读 `body[5..7]` 的小端 `i16`。

#### 4.3.3 源码精读

请求帧在 `use_header=false`（IPC）分支用 `splitn` 找分隔符，是全篇最需要看懂的几行（[src/rpc/mod.rs:171-183](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/mod.rs#L171-L183)）：

```rust
} else {
    let mut sp = body[5..].splitn(2, |c| *c == 0);   // 从第5字节起，按第一个0x00切两段
    let method = sp.next().ok_or_else(|| Error::data("No RPC method"))?;
    let payload_pos = 6 + method.len();                // 0x00 之后第一字节的位置
    sp.next()
        .ok_or_else(|| Error::data("No RPC params block"))?; // 必须存在(可为空)params段
    Ok(RpcEvent {
        kind: RpcEventKind::Request,
        frame,
        payload_pos,
        use_header: false,
    })
}
```

要点：

- `splitn(2, ...)` 只切**一次**，所以即使 params 里含有 `0x00`，也不会被误判为分隔符——分隔符是 method 之后**第一个** `0x00`。
- 第二段（params）用 `sp.next()` 校验「必须存在」，但它**允许为空**（空 params 是合法的），校验的只是「那个 `0x00` 之后还有内容（哪怕 0 字节）」这一事实，保证帧结构完整。
- `check_len!(6)` 宏（[src/rpc/mod.rs:148-154](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/mod.rs#L148-L154)）在进入分支前先保证长度够 type+id，避免 `body[5..]` 越界。

而 `use_header=true`（线程内）分支要轻得多（[src/rpc/mod.rs:164-170](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/mod.rs#L164-L170)）：因为 method 已经在 header 里、params 已经在 payload 里，`payload_pos` 直接给 `0`，不用找分隔符。

最后，若 `body[0]` 不匹配任何已知类型，返回 `Unsupported RPC frame code`（[src/rpc/mod.rs:203](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/mod.rs#L203)）；若 body 为空，更早就返回 `Empty RPC frame`（[src/rpc/mod.rs:145-147](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/mod.rs#L145-L147)）。两处都用 `Error::data`（[src/lib.rs:176-181](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L176-L181)）构造一个 `ErrorKind::Data` 错误。

#### 4.3.4 代码实践

见第 5 节「综合实践」——它专门让你手绘 `use_header=false` 请求帧的字节切分，并解释 `id==0` 不需要回复的原因。

#### 4.3.5 小练习与答案

**练习 1**：一条 IPC 请求帧的字节为 `01 01 00 00 00 61 64 64 00 05`（十六进制）。请解出 type、id、method、params。
**答案**：
- type = `0x01`（请求）。
- id = `body[1..5]` = `01 00 00 00` → 小端 `u32` = `1`（非 0，需要回复）。
- 从 `body[5..]` = `61 64 64 00 05` 按 `0x00` 切：method = `61 64 64` = `"add"`，分隔符 `00`，params = `05`。
- `payload_pos = 6 + 3 = 9`。

**练习 2**：为什么请求帧的 `check_len!(6)` 要求至少 6 字节，而不是 5？
**答案**：type(1) + id(4) = 5 字节只是控制头；第 6 字节用来保证 `body[5..]` 非空，使 `splitn` 至少能产出 method 段（即便 method 为空，也必须有那个 `0x00` 分隔符存在）。少于 6 字节就无从判断 method/params 边界。

---

### 4.4 RpcError：错误码体系与构造

#### 4.4.1 概念说明

RPC 的失败有两类：一是「被调用方主动返回错误」（用 `RPC_ERROR` 帧，携带 4.1 的 `RPC_ERROR_CODE_*`）；二是「调用方本地就把各种 `Error`/IO/msgpack 错误归一化成 `RpcError`」以便统一处理。`RpcError`（[src/rpc/mod.rs:209-214](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/mod.rs#L209-L214)）就是这两类失败的统一容器，含 `code: i16` 与可选 `data: Option<Vec<u8>>`（错误详情，常是字符串或 msgpack）。

#### 4.4.2 核心流程

```
            ┌─ RpcError::method/not_found/params/parse/invalid/internal  (约定码，构造RPC_ERROR帧)
RpcError ←──┤
            └─ From<Error> / From<io::Error> / From<rmp_serde::*> / From<regex::Error>  (本地错误归一化)
```

`RpcResult = Result<Option<Vec<u8>>, RpcError>`（[src/rpc/mod.rs:351-352](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/mod.rs#L351-L352)）是处理器 `handle_call` 的返回类型：`Ok(Some(bytes))` 表示成功且有结果，`Ok(None)` 表示成功但无结果，`Err(RpcError)` 表示失败。

#### 4.4.3 源码精读

**便捷构造器**（[src/rpc/mod.rs:228-287](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/mod.rs#L228-L287)）：每个都绑定一个约定码，例如 `method()` → `-32601`、`params()` → `-32602`、`parse()` → `-32700`、`internal()` → `-32603`。它们让处理器一行就能造出语义化错误：

```rust
pub fn method(err: Option<Vec<u8>>) -> Self {
    Self { code: RPC_ERROR_CODE_METHOD_NOT_FOUND, data: err }
}
```

**从 `RpcEvent` 反解**（[src/rpc/mod.rs:216-226](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/mod.rs#L216-L226)）：当调用方收到一帧 `RPC_ERROR`，用 `RpcError::try_from(&event)` 取出 `code` 与 `data`：

```rust
fn try_from(event: &RpcEvent) -> Result<Self, Self::Error> {
    if event.kind() == RpcEventKind::ErrorReply {
        Ok(RpcError::new(event.code(), Some(event.payload().to_vec())))
    } else {
        Err(Error::data("not a RPC error"))
    }
}
```

**本地错误归一化**：最巧妙的是 `From<Error> for RpcError`（[src/rpc/mod.rs:290-298](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/mod.rs#L290-L298)），把 BUS/RT 自身的 `ErrorKind`（u8）映射到 `i16` 错误码段：

```rust
fn from(e: Error) -> RpcError {
    RpcError {
        code: -32000 - e.kind() as i16,   // ErrorKind(0..=N) → -32000..=(-32000-N)
        data: None,
    }
}
```

即所有 BUS/RT 内部错误都会落到 `-32000` 及更负的码段，**不与 JSON-RPC 约定的 `-32xxx` 码（如 `-32601`）正面冲突**，留出可辨识区间。其余 `From`（`io::Error`→internal、`rmp_serde::decode`→parse、`rmp_serde::encode`→internal、`regex::Error`→parse）都在 [src/rpc/mod.rs:300-340](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/mod.rs#L300-L340)，逻辑相同：把外部错误归到最贴近的约定码，并把错误信息字符串塞进 `data`。

#### 4.4.4 代码实践

**目标**：验证「`From<Error> for RpcError`」的码段不会和约定码撞车。

**步骤**：

1. 假设 `ErrorKind` 的若干判别值为 `0,1,2,...`（见 u2-l1，`ErrorKind` 是 `#[repr(u8)]`）。
2. 用公式 `code = -32000 - kind` 心算：`kind=0 → -32000`，`kind=1 → -32001`……
3. 对照 [src/rpc/mod.rs:15-20](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/mod.rs#L15-L20)，注意到 `RPC_ERROR_CODE_NOT_FOUND = -32001` 与 `kind=1` 的映射结果**数值相同**。

**需要观察的现象**：这里确实存在数值上的重叠（`-32001` 既是「not found」约定码，也可能是某个 `ErrorKind` 的映射结果）。

**预期结果**：你能指出这并非 bug 而是可接受的——两套码出现在**不同上下文**（一个是被调用方主动 `RpcError::not_found()` 构造，一个是本地 `Error` 自动归一化），且 `data` 字段（约定码常带描述，归一化恒为 `None`）可作辅助区分。若要绝对避免歧义，建议处理器优先使用语义化构造器（`method()`/`params()` 等）而非把 `Error` 直接 `?` 进 `RpcResult`。

#### 4.4.5 小练习与答案

**练习 1**：`RpcResult` 的 `Ok(None)` 表示什么？
**答案**：调用成功但无返回值（[src/rpc/mod.rs:351-352](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/mod.rs#L351-L352)）。处理器会回一个**没有 params 的 `RPC_REPLY` 帧**（见 [src/rpc/async_client.rs:199-207](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/async_client.rs#L199-L207) 的 `(&[][..]).into()` 分支）。

**练习 2**：`rpc_err_str`（[src/rpc/mod.rs:32-36](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/mod.rs#L32-L36)）的作用是什么？
**答案**：把任何 `Display` 类型（如字符串、数字）转成 `Some(Vec<u8>)`，方便塞进 `RpcError` 的 `data` 字段——构造错误时一行写 `RpcError::method(rpc_err_str("no such user"))`。

---

### 4.5 prepare_call_payload：构造请求头

#### 4.5.1 概念说明

调用方要发出一个 `RPC_REQUEST`，需要把「type + id + method + 0x00 分隔符」拼成一段前缀。`prepare_call_payload`（[src/rpc/mod.rs:354-363](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/mod.rs#L354-L363)）就是干这件事的。它产出的这段字节，在线程内通信里会成为 `FrameData.header`，在 IPC 通信里会与 params 拼成一条流——但无论哪种，**这段前缀的字节布局都严格对应 4.3 的请求帧定义**，所以接收端能原样解析回来。

#### 4.5.2 核心流程

```
method: "add"   id_bytes: [01,00,00,00]    （call）或 [00,00,00,00] （call0）
            │
            ▼  prepare_call_payload
        ┌────┬──────────────┬───────┬──────┐
        │0x01│ id(4, LE)    │"add"  │ 0x00 │   ← 这就是 RPC_REQUEST 的前缀
        └────┴──────────────┴───────┴──────┘
            │
            ▼  作为 zc_send 的 header 参数发出
        frame.header = Some(上述字节)   （线程内）
        或 与 params 拼接进 payload      （IPC）
```

#### 4.5.3 源码精读

函数本体极其简短（[src/rpc/mod.rs:354-363](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/mod.rs#L354-L363)）：

```rust
pub(crate) fn prepare_call_payload(method: &str, id_bytes: &[u8]) -> Vec<u8> {
    let m = method.as_bytes();
    let mut payload = Vec::with_capacity(m.len() + 6); // type(1)+id(4)+0x00(1)+method
    payload.push(RPC_REQUEST);     // 0x01
    payload.extend(id_bytes);      // 4 字节 id（调用方负责传小端）
    payload.extend(m);             // 方法名
    payload.push(0x00);            // 分隔符
    payload
}
```

注意 `id_bytes` 是**外部传入**的，函数本身不生成 id。两种调用方式决定了要不要回复：

- **`call0`**（不需要回复）：传 `[0, 0, 0, 0]`（[src/rpc/async_client.rs:360](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/async_client.rs#L360)）。于是 `id()` 在接收端解出 `0`，`is_response_required()` 为假，处理器不发回复。
- **`call`**（需要回复）：传递增 `call_id` 的小端字节（[src/rpc/async_client.rs:388](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/async_client.rs#L388)，`call_id` 从 1 起、到 `u32::MAX` 回绕到 1，见 [src/rpc/async_client.rs:377-387](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/async_client.rs#L377-L387)）。调用方同时把这个 `call_id` 与一个 `oneshot` 通道登记进 `calls` 表，等对方回 `RPC_REPLY`/`RPC_ERROR` 时凭 id 兑现（详见 u5-l2）。

`with_capacity(m.len() + 6)` 是精确预估：`1(type) + 4(id) + method + 1(0x00)`，避免 `Vec` 扩容时的多余拷贝——又一个微小的零拷贝/低开销细节。

#### 4.5.4 代码实践

**目标**：对照 `prepare_call_payload` 与 4.3 的请求帧布局，验证「发出去的字节」与「解析的字节」是对称的。

**步骤**：

1. 阅读 [src/rpc/async_client.rs:353-366](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/async_client.rs#L353-L366) 的 `call0`，确认它把 `prepare_call_payload(method, &[0,0,0,0])` 的结果当作 `zc_send` 的 header，而 params 作为 payload。
2. 阅读 [src/rpc/async_client.rs:370-420](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/async_client.rs#L370-L420) 的 `call`，确认它在 `prepare_call_payload` 前先分配并递增 `call_id`，并登记等待通道。
3. 心算一次 `call("svc", "add", params, QoS::Processed)`（设 `call_id=1`）发出的 header 字节：`01 01 00 00 00 61 64 64 00`。

**需要观察的现象**：第 3 步算出的字节序列，与 4.3.5 练习 1 给出的字节完全一致（去掉 params）。

**预期结果**：你能确认「构造端 `prepare_call_payload`」与「解析端 `TryFrom<Frame>` 的 Request 分支」读写的字节位置严格对应——这正是协议自洽的体现。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `call_id` 从 1 开始，而不是 0？
**答案**：因为 `id == 0` 被 `call0` 占用，语义是「不需要回复」（[src/rpc/mod.rs:98-101](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/mod.rs#L98-L101)）。若 `call` 也用 0，接收端会误以为不需要回复而不回包，调用方就永远等不到结果。回绕时也跳回 1（[src/rpc/async_client.rs:380-385](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/async_client.rs#L380-L385)）而非 0。

**练习 2**：`prepare_call_payload` 为什么把 `0x00` 放在最后？
**答案**：它是 method 与 params 之间的分隔符（见 4.3.2 请求帧布局）。接收端从 `body[5..]` 用 `splitn(2, 0x00)` 切出 method——分隔符必须在 method 之后、params 之前，所以放在 method 之后。

---

## 5. 综合实践

本任务把 4.1–4.5 串起来，正好对应本讲的代码实践要求。

### 任务：手绘 IPC 请求帧字节切分，并解释 id==0 不需回复

**实践目标**：用一张字节布局图，把「一条 `use_header=false` 的 `RPC_REQUEST` 帧」从构造（`prepare_call_payload`）到解析（`TryFrom<Frame>`）完整说清楚，并解释 `id==0` 为何不需要回复。

**操作步骤**：

1. **构造场景**。假设调用方执行：
   ```rust
   // 示例代码：说明用，非项目原有代码
   rpc.call0("svc.add", "echo", b"hi".as_ref().into(), QoS::Processed).await;
   ```
   即方法名 `echo`、params = `hi`（2 字节）、`call0`（id 为 0）。

2. **算出 header 字节**。对照 [src/rpc/mod.rs:354-363](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/mod.rs#L354-L363) 的 `prepare_call_payload(method="echo", id_bytes=[0,0,0,0])`：
   - `0x01`（type）+ `00 00 00 00`（id）+ `65 63 68 6f`（"echo"）+ `00`（分隔符）
   - 即 header = `01 00 00 00 00 65 63 68 6f 00`。

3. **画出 IPC 帧字节布局**。因为走 IPC（socket），header 与 params 拼进同一条流（`FrameData.header = None`），整条 payload 为：
   ```
   偏移: 0     1 2 3 4   5 6 7 8 9   10      11 12
        [01] [00 00 00 00] [65 63 68 6f] [00]  [68 69]
         type   id=0(LE)     "echo"      分隔   params="hi"
   ```
   在图上标注：`body[0]` type、`body[1..5]` id、`body[5..10]` method、`body[10]` `0x00`、`body[11..]` params。

4. **回放解析**。对照 [src/rpc/mod.rs:162-184](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/mod.rs#L162-L184)：
   - `body[0]=0x01` → `Request`；
   - `check_len!(6)` 通过（共 13 字节）；
   - `use_header=false` → 对 `body[5..]` = `65 63 68 6f 00 68 69` 做 `splitn(2, 0x00)` → method=`65 63 68 6f`（"echo"）、params=`68 69`（"hi"）；
   - `payload_pos = 6 + 4 = 10`。
   - 验证 getter：`id()`=`body[1..5]`=0；`method()`=`body[5..9]`="echo"；`payload()`=`body[10..]`="hi"。

5. **解释 id==0 不需回复**。结合两处源码：
   - 协议层 [src/rpc/mod.rs:98-101](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/mod.rs#L98-L101)：`is_response_required()` = `id() != 0`。本例 id=0 ⇒ false。
   - 处理器层 [src/rpc/async_client.rs:182-186](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/async_client.rs#L182-L186)：`if id > 0` 才构造 `(target, client)` 回复上下文；id=0 时 `ev = None`，后续 [src/rpc/async_client.rs:195-232](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/async_client.rs#L195-L232) 的 `if let Some((target, cl)) = ev` 整段被跳过，**不会发出任何 `RPC_REPLY`/`RPC_ERROR` 帧**。

**需要观察的现象**：构造端（`prepare_call_payload` 写入的字节）与解析端（`TryFrom<Frame>` 读出的切片）在偏移上完全对齐；`id==0` 在协议层与处理器层双重短路了回复路径。

**预期结果**：

- 你交出一张标注了每个字段偏移与含义的字节布局图（如第 3 步）。
- 你能用一句话回答「id==0 为何不需要回复」：**因为 `id` 既是回复的「寻址键」（调用方用它登记等待通道），又是「要不要回复」的开关；id=0 意味着没有调用方在等，回包无人接收，所以协议层与处理器层都据 `id != 0` 决定是否走回复分支。**

> 进阶（可选）：把第 2 步的 `call0` 换成 `call`（`call_id=1`），重算 header = `01 01 00 00 00 65 63 68 6f 00`，再画出「被调用方回 `RPC_REPLY`」的字节布局（type=`0x11`、id=`01 00 00 00`、result 自定），对照 [src/rpc/async_client.rs:210-217](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/async_client.rs#L210-L217) 验证回复帧的构造。此步结果**待本地运行验证**。

## 6. 本讲小结

- RPC 层**复用** `Message` 帧，用载荷（或零拷贝头）**首字节**区分四种动作：通知 `0x00`、请求 `0x01`、回复 `0x11`、错误 `0x12`（[src/rpc/mod.rs:10-30](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/mod.rs#L10-L30)）。
- `RpcEvent` 是不拷贝字节的「语义视图」，靠 `payload_pos` 与 `use_header` 两个偏移把 `id`/`method`/`payload`/`code` 从原始帧里切出来（[src/rpc/mod.rs:53-136](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/mod.rs#L53-L136)）。
- `use_header` 是零拷贝开关：线程内通信把 RPC 控制字节放进 `FrameData.header`、参数放进 `payload`，免去拼接；IPC 只能把一切挤进一条流，靠 `0x00` 分隔符找 method/params 边界（[src/rpc/mod.rs:142-184](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/mod.rs#L142-L184)）。
- `id == 0` 是「不需要回复」的唯一信号：协议层 `is_response_required()` 与处理器层 `id > 0` 用同一判据，决定是否构造并发送回复帧（[src/rpc/mod.rs:98-101](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/mod.rs#L98-L101)、[src/rpc/async_client.rs:182-232](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/async_client.rs#L182-L232)）。
- `RpcError` 统一两类失败：约定码（`-32xxx`）用于主动 `RPC_ERROR` 帧，`From<Error>` 等把本地错误归一到 `-32000 - kind` 段（[src/rpc/mod.rs:228-298](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/mod.rs#L228-L298)）。
- `prepare_call_payload` 拼出请求前缀 `[0x01][id:4][method][0x00]`，`call0` 传全零 id、`call` 传递增 id，由此决定回复与否（[src/rpc/mod.rs:354-363](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/mod.rs#L354-L363)、[src/rpc/async_client.rs:353-420](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/async_client.rs#L353-L420)）。

## 7. 下一步学习建议

本讲只讲了「协议与解析」——字节怎么编、怎么解。接下来该看「谁来发、谁来收」：

- **u5-l2 RpcClient 与 RpcHandlers 处理器**：精读 [src/rpc/async_client.rs](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/async_client.rs) 全文，弄懂 `RpcClient::call` 如何用 `call_id` + oneshot 通道兑现回复、`processor` 事件循环如何把 `RpcEvent` 分发给 `handle_call`/`handle_notification`/`handle_frame`，以及 blocking/task_pool 选项的取舍。
- **u5-l3 自定义 Broker RPC 与核心 RPC 接口**：看 `broker.rs` 如何挂载自定义 RPC、内置的 `.broker` 方法（info/stats/client.list）如何用同一套协议实现。
- 阅读建议：在进入 u5-l2 前，先回头把本讲 4.3 的四种帧字节布局和综合实践的图记牢——u5-l2 里 `processor` 构造 `RPC_REPLY`/`RPC_ERROR` 的代码，正是对这些布局的「写」端印证。
