# 同步客户端：sync 模块与阻塞式 RPC

## 1. 本讲目标

学完本讲，你应当能够：

- 说出 BUS/RT 为什么在「全套异步客户端」之外，额外提供一套**同步（阻塞）**客户端，以及它适合什么场景。
- 掌握 `SyncClient` trait 与异步版 `AsyncClient` trait 在方法签名上的对应关系，以及两者确认机制的本质差异。
- 理解 `sync::ipc::Client` 是如何用 **rtsc 阻塞通道 + 原生 `std::thread`** 取代 tokio 异步运行时来完成「连接 / 握手 / 收帧 / 收发」的。
- 理解 `SyncRpc` / `SyncRpcHandlers` / `Processor::run` 三者如何拼出一个**阻塞式 RPC 事件循环**，并能把它和异步版 `RpcClient`（u5-l2）的线程模型做对比。
- 动手写出一个最小的同步 RPC 服务端，并解释它为什么需要你**手动** spawn 两个线程。

---

## 2. 前置知识

本讲建立在你已经读完下面两篇讲义的基础上，相关概念不会重复展开：

- **u4-l2（ipc::Client 异步客户端）**：你已经知道握手的三次往返（`GREETINGS` 魔数 + `PROTOCOL_VERSION` + 客户端名注册）、客户端→代理的 **9 字节帧头** `op_id(4)|flags(1)|len(4)`、代理→客户端的 **6 字节帧头** `kind(1)|len(4)|realtime(1)`，以及 `send_frame_and_confirm!` 宏如何按 `QoS.needs_ack()` 决定是否登记一个确认通道。本讲的同步客户端走的是**完全相同的二进制协议**，只是把「谁来读写这个 socket」从 tokio 任务换成了普通线程。
- **u5-l2（RpcClient 与 RpcHandlers 异步版）**：你已经知道 RPC 帧靠载荷首字节区分四种动作（通知 `0x00` / 请求 `0x01` / 回复 `0x11` / 错误 `0x12`），`id==0` 表示「不需要回复」，以及异步 `RpcClient::new` 会在构造时**自动 spawn 一个 processor 任务**。本讲的关键反差就在这里：同步版**不会自动 spawn**，所有线程都得你亲手拉起。

两个需要快速回顾的底层事实：

1. **QoS 是两个正交位**（`src/lib.rs`）。低位的 `needs_ack()` 决定「要不要等代理回 ACK」，高位的 `is_realtime()` 决定「要不要立刻刷新出站」：

   \[
   \text{QoS} \in \{0,1,2,3\},\quad
   \text{needs\_ack} = q\ \&\ 1,\quad
   \text{is\_realtime} = q\ \&\ 0b10
   \]

   这两个位在同步与异步客户端里语义完全一致。

2. **同步客户端没有 tokio**。它依赖一个叫 [rtsc](https://crates.io/crates/rtsc) 的库提供「同步/阻塞版的通道与互斥锁」，并直接用标准库 `std::net::{TcpStream, UnixStream}` 和 `std::thread`。这套实现可以脱离整个 tokio 运行时独立工作。

> 一个一句话定位：**同步客户端是给「不想引入 tokio、只想在普通线程里收发消息和做 RPC」的程序准备的**——比如嵌入式脚本、同步业务主循环、或必须运行在实时调度策略下的守护进程。

---

## 3. 本讲源码地图

本讲涉及的关键源码文件：

| 文件 | 作用 |
| --- | --- |
| `src/sync/mod.rs` | sync 模块入口，声明三个子模块，`rpc` 子模块受 `rpc-sync` feature 门控 |
| `src/sync/client.rs` | 定义 `SyncClient` trait——同步客户端的统一契约（与异步 `AsyncClient` 对应） |
| `src/sync/ipc.rs` | 同步 IPC 客户端实现：`Config`、`Client`、`Reader`、帧编解码宏、`chat` 握手、`handle_read` 收帧循环 |
| `src/sync/rpc.rs` | 同步 RPC 层：`SyncRpc` trait、`RpcClient`、`SyncRpcHandlers` trait、`Processor` 事件循环 |
| `src/lib.rs` | `SyncEventChannel` / `SyncOpConfirm` / `SyncEventSender` 类型别名（rt 下的锁别名切换）、`QoS`、`IntoBusRtResult` |
| `Cargo.toml` | `ipc-sync` / `rpc-sync` / `full` feature 与 `rtsc`、`oneshot` 依赖 |

先建立 feature 认知（详见 u1-l3）：

- [`Cargo.toml` L77](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/Cargo.toml#L77)：`ipc-sync` 引入 `rtsc`、`parking_lot`、`tungstenite`，启用 `sync::{client, ipc}`。
- [`Cargo.toml` L80](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/Cargo.toml#L80)：`rpc-sync` 引入 `rtsc`、`parking_lot`、`regex`，启用 `sync::rpc`。
- [`Cargo.toml` L84](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/Cargo.toml#L84)：`full` **包含 `ipc-sync`**，但**不含 `rpc-sync`**，也不构建两个二进制。要用同步 RPC，需显式开 `rpc-sync`。

模块声明把 `rpc` 子模块单独门控：

[`src/sync/mod.rs` L1-L4](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/sync/mod.rs#L1-L4) 声明 `client` / `ipc` 总是编译，`rpc` 仅在 `rpc-sync` 下编译。这意味着：**只用 `ipc-sync` 能收发消息，但拿不到 RPC 层**——这和异步侧 `ipc` 与 `rpc` 互相独立的设计是对称的。

---

## 4. 核心概念与源码讲解

### 4.1 SyncClient：同步客户端的统一契约

#### 4.1.1 概念说明

回顾 u4-l1：异步侧有一个 `AsyncClient` trait，它把「嵌入式内部客户端 `broker::Client`」和「外部 IPC 客户端 `ipc::Client`」统一成一个接口，让上层 RPC 与业务代码与传输无关。

同步侧做了**完全对称**的设计，叫 [`SyncClient`](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/sync/client.rs#L9-L10)。它存在的意义有三：

1. **去掉 `async`/`await`**：所有方法都是普通同步函数，调用即阻塞、返回即完成，不需要运行时。
2. **把 `&self` 改成 `&mut self`**：因为没有运行时调度，发送动作需要可变借用内部的帧号自增器和 socket 写句柄。注意：**同步 trait 要求 `&mut self`，这直接决定了 RPC 层必须用 `Arc<Mutex<_>>` 把客户端包起来**（见 4.3）。
3. **确认类型换成同步版**：`Result<SyncOpConfirm, Error>`，其中 `SyncOpConfirm` 用的是 `oneshot` crate 的**阻塞** receiver，而不是 tokio 的。

#### 4.1.2 核心流程

`SyncClient` 的方法与 `AsyncClient` 一一对应，分三类：

```
消息投递    send / zc_send / send_broadcast / publish / publish_for
订阅控制    subscribe / unsubscribe / (+ _bulk) / exclude / unexclude (+ _bulk)
连接/生命周期  take_event_channel / ping / is_connected / get_connected_beacon / get_timeout / get_name
```

确认机制的关键类型定义在 lib.rs：

[`src/lib.rs` L88-L89](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L88-L89)：

```rust
pub type SyncOpConfirm = Option<oneshot::Receiver<Result<(), Error>>>;
```

对比异步版 [`src/lib.rs` L70](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L70) `OpConfirm = Option<tokio::sync::oneshot::Receiver<...>>`——结构完全一样，区别只在 receiver 来自哪个 crate。`oneshot`（[`Cargo.toml` L57](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/Cargo.toml#L57)）提供的是**可阻塞等待**的 `recv()` / `recv_timeout()`，这正是 `SyncRpc::call` 能在普通线程里「发请求后阻塞等回复」的基础。

#### 4.1.3 源码精读

trait 全貌在 [`src/sync/client.rs` L9-L56](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/sync/client.rs#L9-L56)。两个值得专门看的点：

第一，`publish_for` 有默认实现且返回「不支持」：

[`src/sync/client.rs` L27-L36](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/sync/client.rs#L27-L36) 默认 `Err(Error::not_supported("publish_for"))`。这与异步版相反——异步 `AsyncClient` 里 `publish_for` 是唯一有默认实现的投递方法，但语义是「真正发送」。同步侧把它做成「默认不支持」，由具体实现（`sync::ipc::Client`）覆写。

第二，连接相关接口（`ping` / `is_connected` / `get_connected_beacon` / `get_timeout` / `get_name`）签名与异步版几乎一致，但实现语义不同（见 4.2.3）。

> 小结：`SyncClient` 就是 `AsyncClient` 的「去异步镜像」。把 `.await` 拿掉、`&self` 改 `&mut self`、确认通道换成阻塞版——剩下完全一样。

---

### 4.2 sync::ipc::Client：基于 rtsc 通道与原生线程的连接

#### 4.2.1 概念说明

`sync::ipc::Client` 是 `SyncClient` 唯一的实现（异步侧有内部客户端和 IPC 客户端两个实现，同步侧只有 IPC 一个——因为没有「嵌入式 broker」的同步版）。

它要解决的核心矛盾是：**一个 socket 有读、写两面，写是主线程发起的同步调用，读是持续不断的后台行为，二者要共享同一个帧号空间和 ACK 对账表，却不能用 tokio 来并发**。BUS/RT 的解法是经典的「**主线程写、后台线程读，用通道和共享 map 桥接**」模型：

- **写**：业务线程调用 `client.send(...)` → 宏拼帧 → 直接 `writer.write_all(...)` 写 socket。
- **读**：一个独立线程跑 `Reader::run()`，循环 `read_exact`，把收到的业务帧推进一条 rtsc 通道，把 ACK 在共享 `ResponseMap` 里对账。

#### 4.2.2 核心流程

连接建立流程（`connect` → `connect_broker` → 按 path 选传输 → `chat` 握手 → 构造 `Client` + `Reader`）：

```
connect(config)
  └─ connect_broker(config, None)
       ├─ 按 config.path 选传输：
       │    ws:// / wss://  → panic!("not implemented yet")  ← 同步版暂不支持 WS
       │    .sock/.socket/.ipc/以/开头 → UnixStream
       │    其余 host:port            → TcpStream
       ├─ set_write_timeout / try_clone 拆出读半部 BufReader
       ├─ connect_broker! 宏：
       │    chat(name, &mut socket)    ← 0xEB+版本握手 + 名字注册
       │    rtsc::channel::bounded(queue_size) ← 建一条阻塞有界通道
       │    组装 Reader{inner, tx, responses, rconn}
       └─ 返回 (Client{writer, frame_id, responses, rx, connected, ...}, Reader)
```

注意 `connect` 返回的是 **`(Client, Reader)` 元组**，并且 `Reader` **不会被自动启动**——文档明确写了「must be started manually by calling `Reader::run()` (e.g. in a separate thread)」。这是同步客户端最常踩的坑：忘了 spawn `reader.run()`，于是能发但永远收不到任何帧/ACK。

收帧循环 `handle_read` 对 6 字节帧头做分发（与异步版 u4-l2 一致）：

- `Nop`：丢弃（保活心跳）。
- `Acknowledge`：用 `buf[5]`（realtime/ACK 码字节）做 `to_busrt_result()` 对账，在 `responses` 里按 `op_id` 取出 oneshot sender 兑现。
- 其余（业务帧）：按 `0x00` 切出 sender / topic / payload，包成 `FrameData` 投递事件通道。

#### 4.2.3 源码精读

**Config（建造者）** [`src/sync/ipc.rs` L35-L75](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/sync/ipc.rs#L35-L75)：字段与异步 `ipc::Config` 对应——`path`（决定传输类型）、`name`、`buf_size`、`queue_size`、`timeout`、`token`（Bearer 鉴权头）。默认值复用全局常量 `DEFAULT_BUF_SIZE`/`DEFAULT_QUEUE_SIZE`/`DEFAULT_TIMEOUT`（均为 8192/8192/1s，见 [`src/lib.rs` L43-L47](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L43-L47)）。

**按 path 自动选传输** [`src/sync/ipc.rs` L232-L289](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/sync/ipc.rs#L232-L289)：规则与异步版对称，但有两处差异要记住：

- [`L233-L234`](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/sync/ipc.rs#L233-L234)：`ws://`/`wss://` 走 `unimplemented!(...)`，会直接 panic——**同步客户端目前不支持 WebSocket**。
- TCP 分支额外调用 `set_nodelay(true)`（[`L272`](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/sync/ipc.rs#L272)）关闭 Nagle，降低小帧延迟；Unix 分支无此操作。

**Client 结构** [`src/sync/ipc.rs` L94-L104](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/sync/ipc.rs#L94-L104)：持有 `writer: Box<dyn Socket + Send>`（被类型擦除的 Tcp/Unix 流）、自增 `frame_id`、ACK 对账表 `responses: ResponseMap`、事件通道接收端 `rx`、连接标志 `connected`、超时与配置。其中：

[`src/sync/ipc.rs` L33](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/sync/ipc.rs#L33) 定义对账表：

```rust
type ResponseMap = Arc<Mutex<BTreeMap<u32, oneshot::Sender<Result<(), Error>>>>>;
```

它被 `Client`（写侧）和 `Reader`（读侧）**共享**：写侧在 [`send_frame_and_confirm!`](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/sync/ipc.rs#L128-L143) 里按 `frame_id` 插入 sender，读侧在 `handle_read` 的 ACK 分支里按 `ack_id` 取出兑现。注意 `Mutex` 的具体类型随 `rt` feature 切换：非 rt 用 `parking_lot::Mutex`，rt 下用 `rtsc::pi::Mutex`（见文件顶部 [`L11-L14`](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/sync/ipc.rs#L11-L14)）。

**发送宏族** 与异步版同名同形，只是把异步刷新换成立刻写：

- [`prepare_frame_buf!`](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/sync/ipc.rs#L108-L116) 拼帧头前 5 字节 `op_id(4) | flags(1)`，`flags = op as u8 | (qos as u8) << 6`——操作码占低 6 位、QoS 占高 2 位，和 u2-l3 讲的协议一致。
- [`send_data_or_mark_disconnected!`](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/sync/ipc.rs#L118-L126)：写失败立刻把 `connected` 置 false 并 shutdown socket。**这是同步客户端感知断线的唯一途径**——它没有心跳监控任务，写失败即「连通性判定」。
- [`send_frame_and_confirm!`](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/sync/ipc.rs#L128-L143)：若 `qos.needs_ack()` 则登记一个 `oneshot::channel()` 进 `responses`，返回 `Some(rx)`；否则返回 `None`。与异步版逻辑同构。

**SyncClient 实现** [`src/sync/ipc.rs` L334-L463](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/sync/ipc.rs#L334-L463)：每个方法都是「调一个宏」。例如 [`send`（L343-L345）](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/sync/ipc.rs#L343-L345) 直接展开成 `send_frame!(self, target, payload.as_slice(), FrameOp::Message, qos)`；`ping`（[`L447-L450`](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/sync/ipc.rs#L447-L450)）就是往 socket 写一个 `PING_FRAME`。注意 `publish_for` 在这里被**真正实现**（[`L378-L393`](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/sync/ipc.rs#L378-L393)），覆盖了 trait 的「不支持」默认值。

**chat 握手** [`src/sync/ipc.rs` L529-L566](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/sync/ipc.rs#L529-L566)：与异步版 `chat` 字节级一致——读 3 字节问候校验魔数与版本、回写同样 3 字节、读 1 字节 `RESPONSE_OK`、发送 `u16` 名字长度 + 名字、再读 1 字节确认。唯一区别是用了同步的 `read_exact`/`write_all`。

**Reader 与 handle_read** [`src/sync/ipc.rs` L568-L582](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/sync/ipc.rs#L568-L582)：`Reader::run` 是个消费 `self` 的阻塞函数，内部 [`handle_read`（L471-L527）](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/sync/ipc.rs#L471-L527) `loop { read_exact 6 字节头; 按帧类型分发 }`。出错时（连接断开）记录日志并把 `rconn` 置 false。

ACK 对账这一行值得单独看（[`L486`](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/sync/ipc.rs#L486)）：

```rust
let _r = tx.send(buf[5].to_busrt_result());
```

`buf[5]` 是 6 字节帧头里的 realtime/状态字节。`to_busrt_result()`（[`src/lib.rs` L234-L246](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L234-L246)）把 `0x01`(`RESPONSE_OK`) 映射成 `Ok(())`、其余字节经 `ErrorKind: From<u8>` 还原成错误——这就是 `QoS::Processed` 的 ACK 兑现路径。

#### 4.2.4 代码实践

**实践目标**：亲手感受「写同步、读后台线程、ACK 跨线程对账」的三段结构，并验证「忘记 spawn Reader 会收不到 ACK」。

**操作步骤**（这是一个**源码阅读 + 最小调用**型实践，因为项目没有提供同步示例，以下为示例代码）：

1. 在项目根新建一个临时二进制 crate（或用 `cargo run --example`），在 `Cargo.toml` 里对它启用 `ipc-sync` feature。
2. 先启动一个异步 broker（用 `test.sh server` 或 `examples`），监听 `/tmp/busrt.sock`。
3. 写下面这段「示例代码」（**非项目原有代码**）：

```rust
// 示例代码：同步客户端发送并等待 ACK
use busrt::borrow::Cow;
use busrt::ipc::sync::Client; // 实际路径以你启用的 feature 为准：sync::ipc::Client
use busrt::ipc::sync::Config;
use busrt::QoS;
use std::thread;
use std::time::Duration;

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let cfg = Config::new("/tmp/busrt.sock", "test.sync.sender");
    let (mut client, reader) = Client::connect(&cfg)?;
    // 关键：必须手动启动读线程，否则下面的 op_confirm 永远等不到 ACK
    thread::spawn(move || reader.run());

    // 向某个目标发送，QoS::Processed 要求代理回 ACK
    let op_confirm = client
        .send("test.sync.target", Cow::Owned(b"hello".to_vec()), QoS::Processed)?
        .expect("needs ack");
    // 阻塞等待 ACK（oneshot 的同步 recv）
    op_confirm.recv_timeout(Duration::from_secs(2))??;
    println!("ACK received");
    Ok(())
}
```

**需要观察的现象 / 预期结果**：

- **保留** `thread::spawn(move || reader.run())` 这一行：预期打印 `ACK received`。
- **注释掉**这一行后重跑：预期 `recv_timeout` 在约 2 秒后超时返回错误——因为没有人读 socket、也就没有线程去把 ACK 投递进 oneshot。这直接印证了「Reader 必须手动启动」。
- 若写一个不存在的目标名（如 `no.such.target`），`QoS::Processed` 下应收到 `NotRegistered` 错误（经 ACK 字节还原）。

> 若无法本地验证运行结果，请标注「待本地验证」并改为阅读型任务：对照 [`handle_read`](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/sync/ipc.rs#L471-L527) 与 [`send_frame_and_confirm!`](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/sync/ipc.rs#L128-L143)，画出 ACK 字节从「代理回帧 → Reader 读到 → responses 取 sender → oneshot 兑现 → 业务线程 recv 返回」的完整链路。

#### 4.2.5 小练习与答案

**练习 1**：同步 `Config` 没有 `buf_size`/`queue_size` 会怎样？`queue_size` 控制的是哪条通道的容量？

**答案**：默认取 `DEFAULT_BUF_SIZE=8192` / `DEFAULT_QUEUE_SIZE=8192`。`queue_size` 控制的是 `connect_broker!` 宏里那条 `rtsc::channel::bounded(queue_size)`（即 Reader → 业务的事件通道 `rx`），它给订阅/点对点入站帧提供天然背压；写侧 socket 不受它约束。

**练习 2**：为什么 `sync::ipc::Client` 在 `path` 为 `ws://...` 时会 panic 而不是返回 `Err`？这是不是缺陷？

**答案**：因为代码用了 `unimplemented!` 宏（panic）。从语义上看它更适合返回 `Error::not_supported`，但当前实现选择 panic——使用同步客户端时应避免 `ws://`/`wss://` 路径，改用 Unix/TCP。

---

### 4.3 SyncRpc / SyncRpcHandlers：同步 RPC 客户端与处理器

#### 4.3.1 概念说明

有了 `SyncClient`，就能在它之上叠一层 RPC（复用 u5-l1 讲的 RPC 帧协议）。同步 RPC 层有三个角色：

- **`SyncRpc` trait**：对应异步侧的 `Rpc` trait，提供 `notify` / `call0` / `call`。
- **`SyncRpcHandlers` trait**：对应异步侧的 `RpcHandlers`，提供 `handle_call` / `handle_notification` / `handle_frame` 三个回调。
- **`RpcClient`**（注意：同步与异步都叫这个名字，但在不同模块 `sync::rpc` vs `rpc::async_client`）：把一个 `SyncClient` 包成 RPC 客户端，并产出一个 `Processor`。

#### 4.3.2 核心流程

一个完整的同步 RPC 程序需要**你自己拉起两个线程**：

```
主线程:
  (client, reader) = Client::connect(cfg)
  thread::spawn(reader.run())          ← ① 收帧线程：socket → 事件通道
  (rpc, processor) = RpcClient::new(client, handlers)
  thread::spawn(processor.run())       ← ② 事件循环线程：事件通道 → 分发到 handlers
  // 之后主线程可任意调用 rpc.call(...) / rpc.notify(...)
```

调用 `rpc.call(target, method, params, qos)` 时发生的事（见 4.4 精读）：

1. 自增 `call_id`（回绕到 1，避开「id=0 不需回复」），用 `prepare_call_payload` 拼出请求前缀。
2. 在 `CallMap` 里登记 `id → oneshot::Sender`。
3. **加锁** `client`，`zc_send` 把请求帧发出去。
4. **阻塞** `rx.recv_timeout(timeout)` 等回复（无 timeout 则 `rx.recv()`）。
5. 回复由 ② 号线程的 `Processor::run` 收到后，在 `CallMap` 里按 id 取出 sender 兑现 → 唤醒第 4 步。
6. 把回复解析成 `Ok(RpcEvent)` 或 `Err(RpcError)`。

#### 4.3.3 源码精读

**SyncRpcHandlers** [`src/sync/rpc.rs` L20-L30](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/sync/rpc.rs#L20-L30)：三个方法都有默认实现——`handle_call` 默认返回 `Err(RpcError::method(None))`（方法未找到），`handle_notification`/`handle_frame` 默认空操作。这让你可以只实现关心的那一个。不需要处理器的「只调用」客户端用 [`DummyHandlers`（L32-L41）](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/sync/rpc.rs#L32-L41)，由 `RpcClient::new0` 使用。

**SyncRpc** trait [`src/sync/rpc.rs` L45-L71](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/sync/rpc.rs#L45-L71)。注意一个同步特有的方法 [`client()`（L52）](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/sync/rpc.rs#L52)：

```rust
fn client(&self) -> Arc<Mutex<(dyn SyncClient + Send + 'static)>>;
```

它把内部被 `Arc<Mutex<_>>` 包裹的客户端还给你，这样处理器线程里也能 `lock()` 后调用 `subscribe`/`send_broadcast`。这是同步侧特有的需求——因为 `SyncClient` 的方法是 `&mut self`，必须靠互斥锁才能在多线程间共享。

**notify / call0** [`src/sync/rpc.rs` L247-L263](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/sync/rpc.rs#L247-L263)：`notify` 用 `zc_send` 把载荷首字节设为 `RPC_NOTIFICATION`（`0x00`）；`call0` 用 `prepare_call_payload(method, &[0,0,0,0])` 拼出 id 全零的请求帧——**id=0 表示不需要回复**，与 u5-l1/u5-l2 的约定一致。两者都返回 `SyncOpConfirm`（不阻塞等结果）。

**call** [`src/sync/rpc.rs` L267-L316](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/sync/rpc.rs#L267-L316)：这是同步 RPC 最核心的方法，几个关键点：

- [`L274-L284`](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/sync/rpc.rs#L274-L284)：`call_id` 自增，到 `u32::MAX` 回绕到 **1**（绝不回 0，否则对端不回复）。
- [`L286-L289`](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/sync/rpc.rs#L286-L289)：建 `oneshot`、登记进 `CallMap`、`client.lock().zc_send(...)` 发送。
- [`L290-L310`](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/sync/rpc.rs#L290-L310)：**阻塞等待**——有 `timeout` 用 `rx.recv_timeout(timeout)`，否则 `rx.recv()`。超时/错误都先从 `CallMap` 移除该 id 再返回。
- [`L311-L315`](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/sync/rpc.rs#L311-L315)：拿到 `RpcEvent` 后尝试 `RpcError::try_from(&result)`，是错误帧就返回 `Err`，否则返回 `Ok(result)`。

对比异步 `Rpc::call`：异步版把 oneshot 等待交给 `await`，运行时在等回复期间会让出线程；同步版则是**当前线程整个阻塞**在 `recv_timeout` 上。所以在同步侧，**发起 `call` 的线程和跑 `Processor::run` 的线程必须不同**——否则你在处理器线程里 `call` 自己的客户端，就会永远等不到那个本该由同一线程处理的回复帧，造成死锁。

#### 4.3.4 代码实践

**实践目标**：体会「`call` 阻塞当前线程」这一约束，确认「发起调用的线程 ≠ Processor 线程」。

**操作步骤**（示例代码，**非项目原有代码**）：

1. 启动一个异步 broker。
2. 跑一个「服务端」程序：连接后 `RpcClient::new0` 拿到 dummy RPC（只收不处理），其实它扮演被调用方需要真实 handler——为简单起见，本步先确认 `call0`（不等回复）能发出。
3. 写一个客户端，在**主线程**直接 `rpc.call(target, "ping", ...)`，并把 `Processor::run` 放在**另一个线程**。预期收到回复。
4. 把 `Processor::run` 改成也在**主线程**（即 `processor.run()` 不 spawn、直接在 `call` 之前不可能——这里改为：不 spawn processor，而是在主线程先 `processor.run()` 会永远阻塞，无法再 `call`）。观察：只要 processor 与 call 在同一线程，就无法完成一次完整 RPC。

**预期结果**：

- processor 在独立线程 → `call` 正常返回 `RpcEvent`。
- processor 与 call 同线程 → 要么 `call` 永久阻塞（超时），要么根本没机会 `call`（被 `processor.run()` 阻塞）。

> 这一对比正是同步 API 最大的取舍：**它要求你显式管理线程拓扑**，而异步版靠运行时调度隐藏了这一点。

#### 4.3.5 小练习与答案

**练习 1**：`SyncRpc::call` 在没有设置 `timeout`（`get_timeout()` 返回 `None`）时会怎样？

**答案**：走 `rx.recv()`（[`L303`](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/sync/rpc.rs#L303)），**永久阻塞**直到收到回复或 sender 被丢弃。`sync::ipc::Client::get_timeout()` 恒返回 `Some(self.timeout)`（默认 1s），所以实际用 IPC 客户端时一定有超时保护；但如果你给 `RpcClient` 喂了一个 `get_timeout()` 返回 `None` 的自定义 `SyncClient`，`call` 就会无限等。

**练习 2**：`call_id` 为什么回绕到 1 而不是 0？

**答案**：协议规定 `id==0` 表示「不需要回复」（见 u5-l1）。若回绕到 0，对端处理器会认为这是一个 `call0`、不会回任何帧，本次 `call` 登记的 oneshot 就永远等不到兑现，只能等超时。

---

### 4.4 Processor::run：阻塞式事件循环与线程模型

#### 4.4.1 概念说明

`Processor::run` 是同步 RPC 的「心脏」，对应异步侧 `processor()` 事件循环（u5-l2）。它的职责是从事件通道 `rx` 取帧、解析成 `RpcEvent`、按种类分发：

- **Notification**（`0x00`）：调 `handle_notification`，直接在本线程同步执行。
- **Request**（`0x01`）：**`thread::spawn` 一个新线程**跑 `handle_call`，处理完手工拼回复帧回送。
- **Reply / ErrorReply**（`0x11`/`0x12`）：在 `CallMap` 里按 id 兑现某次 `call` 的 oneshot。
- 非 `Message` 帧：调 `handle_frame`。

#### 4.4.2 核心流程

```
Processor::run(self):
  while let Ok(frame) = rx.recv():        // 阻塞取帧，通道关闭则退出循环
    if frame.kind() == Message:
      RpcEvent::try_from(frame):
        Notification → handlers.handle_notification(ev)        // 同步、顺序
        Request(id):
          id==0 → 不需要回复，spawn 线程只跑 handle_call
          id>0  → spawn 线程：跑 handle_call 后，按结果拼
                    Ok(v)  → [0x11][id:4] + v          (RPC_REPLY)
                    Err(e) → [0x12][id:4][code:2] + e.data (RPC_ERROR)
                  通过 client.lock().zc_send(target, header, payload, qos) 回送
        Reply/ErrorReply → calls.lock().remove(&id) → tx.send(event)  // 唤醒 call
    else:
      handlers.handle_frame(frame)
```

#### 4.4.3 源码精读

`Processor::run` 全文 [`src/sync/rpc.rs` L89-L197](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/sync/rpc.rs#L89-L197)。三个关键点：

**① 请求用 `thread::spawn` 处理**（[`L120`](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/sync/rpc.rs#L120)）。每个入站请求都开一个**新原生线程**跑 `handle_call`，这是同步版与异步版最显著的线程模型差异：

- 异步版（u5-l2）默认对每个请求 `tokio::spawn` 一个**轻量任务**，可选 `task_pool` 限流；handler 是 `async fn`。
- 同步版对每个请求 `thread::spawn` 一个**操作系统线程**；handler 是普通同步函数。

这意味着同步服务端在高 QPS 下要承受**每请求一线程**的开销（线程创建/调度/栈内存），不适合极高并发；但好处是 handler 可以做任意**阻塞 IO**（同步查数据库、调阻塞库）而不会像异步 handler 那样「一个阻塞卡住整个运行时」。这正是同步客户端的核心价值场景。

**② 回复的 QoS 镜像请求的 realtime 位**（[`L121-L125`](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/sync/rpc.rs#L121-L125)）：请求帧 `is_realtime()` 时回复用 `QoS::RealtimeProcessed`，否则 `QoS::Processed`——与异步版行为一致，保证实时请求得到实时回复。

**③ 回复帧手工拼接**（[`L148-L169`](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/sync/rpc.rs#L148-L169)）：成功时拼 `[RPC_REPLY(0x11)] [id:4 小端]` 作 header、结果作 payload；失败时拼 `[RPC_ERROR(0x12)] [id:4] [code:2 小端]` 作 header、错误数据作 payload，再用 `client.lock().zc_send(target, header, payload, qos)` 回送。注意它通过 `send_reply!` 宏（[`L128-L147`](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/sync/rpc.rs#L128-L147)）**加锁**共享的 `client` 来发送——因为 `SyncClient` 是 `&mut self`，回复发生在 spawn 出来的线程里，必须经过那把 `Arc<Mutex<_>>`。

**④ Reply 兑现 oneshot**（[`L173-L186`](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/sync/rpc.rs#L173-L186)）：收到回复帧时从 `CallMap` 移出 sender 发送 `event`，唤醒另一线程里阻塞在 `recv` 的 `call`；找不到则记 `orphaned RPC response`。

**RpcClient 构造不 spawn**（[`src/sync/rpc.rs` L213-L238](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/sync/rpc.rs#L213-L238)）：`init` 只是把 `client` 包进 `Arc<Mutex<...>>`、取出事件通道、组装 `Processor` 并返回 `(RpcClient, Processor)`，**完全没有 `thread::spawn`**。对比异步 `RpcClient::new` 会在构造时自动 `tokio::spawn(processor)`（u5-l2），这是两者最容易被忽略、却最关键的差异——**同步版你要是忘了 spawn `processor.run()`，RPC 服务端不会响应任何调用，客户端的 `call` 会一直超时。**

#### 4.4.4 代码实践（贯穿本讲的综合实践见第 5 节）

这里给一个「源码阅读型」小实践：对照 [`Processor::run`](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/sync/rpc.rs#L89-L197) 与异步 `processor`（`src/rpc/async_client.rs`，u5-l2），填写下表（部分已给出）：

| 维度 | 异步 `RpcClient` | 同步 `RpcClient` |
| --- | --- | --- |
| 处理器启动 | 构造时自动 `tokio::spawn` | **手动** `thread::spawn(processor.run())` |
| 请求处理并发单位 | tokio 任务（可选 task_pool 限流） | 原生 OS 线程（每请求一个） |
| handler 形态 | `async fn` | 同步 `fn` |
| handler 内能否做阻塞 IO | 会卡住整个运行时（危险） | 安全（独立线程） |
| 客户端共享方式 | `Arc<dyn AsyncClient>`（`&self` 方法） | `Arc<Mutex<dyn SyncClient>>`（`&mut self` 方法） |
| `call` 等待回复 | `await`（让出线程） | `recv_timeout`（阻塞线程） |

**预期结果**：你能用自己的话解释「为什么同步处理器敢让 handler 做阻塞 IO，而异步处理器不行」。答案：同步处理器把每个请求丢进独立 OS 线程，该线程阻塞只影响它自己；异步处理器跑在共享的运行时 worker 上，一个 handler 阻塞会霸占 worker、拖慢同 worker 上的其它任务。

#### 4.4.5 小练习与答案

**练习 1**：`Processor::run` 的 `while let Ok(frame) = rx.recv()` 在什么情况下退出循环？

**答案**：当 `rx`（事件通道 `SyncEventChannel`）的所有 sender 都被丢弃、通道关闭时，`recv()` 返回 `Err`，`while let Ok(...)` 终止。实际触发场景是 `Reader` 线程因连接断开而结束（它持有 sender 的副本）。

**练习 2**：为什么回复帧要用 `zc_send`（带 header）而不是普通 `send`？

**答案**：RPC 回复需要把控制字节（`0x11`/`0x12` + id/code）放进 header、把业务结果放进 payload，二者分离才能让对端按 u5-l1 的解析逻辑从 payload 偏移正确切出 method/params。`zc_send` 正好提供「header + payload」两段载荷的零拷贝发送。

**练习 3**：若 `handle_call` 内部又对**同一个** RpcClient 调用了 `rpc.client().lock().call(...)`，会发生什么？

**答案**：会死锁/超时。`call` 需要等回复，而回复要由 `Processor::run` 处理；但 `Processor::run` 此刻正卡在为当前请求 spawn 出的线程里等待 `handle_call` 返回（更准确说：processor 主循环未被阻塞，但该 `call` 的回复需要发回，而 call 持有 client 锁去发送请求……实际是 call 阻塞等回复线程、与处理器不在同一线程时未必死锁，但若 handler 在处理器线程内同步调用则会卡住）。结论：**同步处理器里不要对自身发起需要回复的 `call`**，改用 `call0`/`notify`，或把客户端拆成「调用客户端」与「处理客户端」两个连接。

---

## 5. 综合实践

**任务**：实现一个最小的**同步 RPC 服务端**，处理 `test` 方法并回复，然后对比它与异步 `client_rpc_handler`（u5-l2）的线程模型。

**准备**：先用 `test.sh server` 启动一个异步 broker 监听 `/tmp/busrt.sock`。

**第 1 步：写同步服务端**（示例代码，**非项目原有代码**；需在 `Cargo.toml` 启用 `rpc-sync` feature）

```rust
// 示例代码：同步 RPC 服务端
use busrt::rpc::RpcEvent;
use busrt::rpc::sync::{RpcClient, SyncRpcHandlers}; // 实际路径：busrt::sync::rpc::{RpcClient, SyncRpcHandlers}
use busrt::sync::ipc::{Client, Config};
use busrt::sync::client::SyncClient; // 为 subscribe
use busrt::QoS;
use std::thread;
use std::time::Duration;

struct MyHandlers;
impl SyncRpcHandlers for MyHandlers {
    fn handle_call(&self, event: RpcEvent) -> busrt::rpc::RpcResult {
        match event.parse_method()? {
            "test" => Ok(Some(b"\x81\xa2ok\xc3".to_vec())), // msgpack {"ok": true}
            _ => Err(busrt::rpc::RpcError::method(None)),
        }
    }
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let cfg = Config::new("/tmp/busrt.sock", "test.sync.rpc");
    let (mut client, reader) = Client::connect(&cfg)?;
    thread::spawn(move || reader.run());           // ① 收帧线程
    client.subscribe("#", QoS::Processed)?;         // 订阅（可选）
    let (rpc, processor) = RpcClient::new(client, MyHandlers);
    let beacon = rpc.is_connected();
    thread::spawn(move || processor.run());         // ② 事件循环线程
    while beacon {
        thread::sleep(Duration::from_secs(1));
    }
    Ok(())
}
```

> 注意：上面注释里写的模块路径仅为示意。`sync::rpc::RpcClient` 与异步 `rpc::RpcClient` 同名但处在不同模块。请以你启用的 feature 下实际 `pub use` 的路径为准；若不确定，**待本地验证**后修正导入。

**第 2 步：调用它**。用异步 CLI 或异步 `client_rpc.rs` 风格的客户端对 `test.sync.rpc` 调用 `test` 方法（两者走同一个二进制协议，可互通）。

**第 3 步：做线程模型对比**。填出下表并据此写一段说明：

| 对比项 | 异步 `client_rpc_handler.rs` | 本同步服务端 |
| --- | --- | --- |
| 运行时 | tokio | 无（原生线程） |
| 收帧 | tokio 任务 `handle_read` | 独立线程 `Reader::run` |
| 事件分发 | 构造时自动 spawn 的 `processor` 任务 | 手动 `thread::spawn(processor.run())` |
| 单个请求处理 | `tokio::spawn` 一个 async 任务 | `thread::spawn` 一个 OS 线程 |
| handler 共享状态 | 需 `Atomic`/`Mutex`（多任务并发） | 同样需 `Atomic`/`Mutex`（多线程并发） |

**预期结果**：服务端能正确响应 `test` 返回 `{"ok":true}`；并能在对比表里清楚说出「同步版把异步运行时隐藏的线程拓扑暴露给了开发者」。

---

## 6. 本讲小结

- `SyncClient`（`src/sync/client.rs`）是 `AsyncClient` 的「去异步镜像」：方法一一对应，差别在 `&mut self`、无 `.await`、确认类型换成阻塞的 `SyncOpConfirm = Option<oneshot::Receiver<Result<(),Error>>>`。
- `sync::ipc::Client` 用「主线程同步写 socket + 独立线程 `Reader::run` 同步读」的经典模型，读写两侧通过 `Arc<Mutex<ResponseMap>>` 对账 ACK、通过 rtsc 阻塞通道投递业务帧；**不支持 WebSocket**，按 path 自动选 Unix/TCP。
- `connect` 返回 `(Client, Reader)`，且 `Reader` **必须手动 `thread::spawn(reader.run())`** 启动——忘了就收不到任何帧与 ACK。
- `SyncRpc` 的 `call` 在**当前线程**阻塞 `recv_timeout`/`recv` 等回复，因此「发起 `call` 的线程」必须不同于「跑 `Processor::run` 的线程」，否则会卡死或超时。
- `Processor::run` 对每个入站请求 `thread::spawn` 一个**原生 OS 线程**处理——与异步版每请求一个 tokio 任务形成对照；这让同步 handler 可以安全地做阻塞 IO，但承受每请求一线程的并发成本。
- 与异步 `RpcClient::new` 不同，同步 `RpcClient::new`/`init` **不会自动 spawn** processor，处理器线程需手动拉起；这是从异步迁到同步时最容易踩的坑。

## 7. 下一步学习建议

- **回到异步侧做横向巩固**：重读 u5-l2 的 `RpcClient` 与 `processor()`，对照本讲的同步版，确保你能解释「自动 spawn vs 手动 spawn」「任务 vs 线程」「await 让出 vs recv 阻塞」三组差异。
- **实时场景**：若你的同步客户端要跑在实时调度策略下，配合 u7-l1 阅读 `src/lib.rs` 的 `RawMutex`/`Condvar` 别名（`#[cfg(feature = "rt")]` 下切到 `rtsc::pi`/`parking_lot_rt`），理解同步通道在 rt feature 下如何避免自旋锁。
- **跨语言对照**：BUS/RT 的 Python 绑定（u8-l4）有同步与异步两套，其同步实现与本讲的 `sync` 模块在「主线程写 + 后台线程读 + 通道桥接」的架构上高度同构，可作为不同语言里同一思路的参照。
- **继续手册**：u8 单元（运维、工具与生态）会进入 `busrtd` 服务端、`busrt` CLI、fifo 通道与多语言绑定，把「同步/异步客户端如何与独立服务端互通」放到真实运维语境中。
