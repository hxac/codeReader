# ipc::Client：连接代理与帧收发

## 1. 本讲目标

本讲精读 `src/ipc.rs`，搞清楚一个**外部客户端**是如何工作的：它怎么从一行 `path` 字符串自动选择 Unix / TCP / WebSocket 传输、怎么完成与代理的握手、怎么把 `AsyncClient` trait 的方法（send / publish / subscribe 等）翻译成线上的二进制帧，又怎么在后台解析代理回送的帧并把 ACK 兑现成你 `await` 的结果。

学完后你应该能够：

- 说出 `Config` 七个参数各自的作用与默认值，并能解释 `path` 如何决定传输类型。
- 复述 `connect → connect_broker → chat → handle_read` 这条启动链每一步做了什么。
- 看懂 `handle_read` 如何用 6 字节头解析入站帧、如何区分 ACK 与业务帧、如何把 ACK 兑现成 `OpConfirm`。
- 理解 `prepare_frame_buf!` / `send_frame!` / `send_frame_and_confirm!` 这组宏如何拼出 9 字节头的发送帧，并解释 QoS 如何同时影响「是否要确认」与「是否立即刷新」。

## 2. 前置知识

在读本讲前，你应该已经掌握下面几个概念（来自前置讲义）：

- **三种通信模式与通道的区别**（u1-l1）：send 点对点、broadcast 广播、publish 发布订阅；线程内 / Unix / TCP / WebSocket 是四种通道。
- **线上协议**（u2-l3）：客户端→代理的业务帧是 **9 字节头** `op_id(4) | flags(1) | len(4)`，其中 `flags = op | (qos << 6)`；代理→客户端是 **6 字节头** `kind(1) | len(4) | realtime(1)`；握手用 `GREETINGS(0xEB)` + 版本号；ACK 是定长 6 字节。
- **QoS 的两个正交位**（u2-l1）：低位 `needs_ack()`（是否等代理回 ACK），高位 `is_realtime()`（是否立即刷新出站缓冲）。
- **零拷贝载荷 `Cow`**（u2-l2）：发送方法的载荷签名是 `Cow<'async_trait>`，socket 路径只需只读视图，走 `as_slice()`。
- **`AsyncClient` trait**（u4-l1）：投递与订阅方法统一返回 `Result<OpConfirm, Error>`，`OpConfirm` 是否为 `Some` 取决于 `QoS::needs_ack()`；本讲的 `ipc::Client` 就是该 trait 的「外部客户端」实现，与内部客户端 `broker::Client` 形成对照。

> 关键对照：内部客户端（u3-l1）没有连接概念——`is_connected` 恒真、确认即时兑现；而本讲的 `ipc::Client` 是**真实的网络往返**：会断线、ACK 要等代理回帧、ping 是真发心跳。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/ipc.rs](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs) | 本讲主文件。定义 `Config`、`Client`、`Writer` 枚举、一组发送/连接宏、`chat` 握手与 `handle_read` 解析循环。 |
| [src/comm.rs](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/comm.rs) | `Flush` 枚举与 `TtlBufWriter`，是发送路径的底层缓冲器（本讲引用，细节在 u4-l3 详讲）。 |
| [src/lib.rs](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs) | 协议常量（`GREETINGS`/`PING_FRAME` 等）、默认值（`DEFAULT_*`）、`FrameData::new` 与 `Frame` 别名。 |
| [examples/client_sender.rs](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/examples/client_sender.rs) | 最小发送端示例：publish + send + send_broadcast。 |
| [examples/client_listener.rs](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/examples/client_listener.rs) | 最小接收端示例：subscribe `#` + 事件循环。 |

---

## 4. 核心概念与源码讲解

### 4.1 Config：连接一个客户端需要的全部参数

#### 4.1.1 概念说明

要连一个 BUS/RT 代理，客户端至少要知道两件事：**连到哪**（path）和**自己叫什么**（name）。但除此之外还有一组「调参旋钮」控制缓冲、队列与超时。`ipc::Config` 用**建造者模式（builder pattern）**把这些都收进一个结构体：先用 `Config::new(path, name)` 建一个带默认值的实例，再链式调用 `.buf_size(..)`、`.timeout(..)` 等方法覆盖感兴趣的参数，其余保持默认。

#### 4.1.2 核心流程

`Config` 的七字段与取值来源：

| 字段 | 含义 | 默认值（来自 `lib.rs`） |
| --- | --- | --- |
| `path` | 连接地址，**同时决定传输类型** | 无，必填 |
| `name` | 客户端唯一名（握手时注册给代理） | 无，必填 |
| `buf_size` | socket 读写缓冲区容量 | `DEFAULT_BUF_SIZE = 8192` |
| `buf_ttl` | 出站缓冲的定时刷新间隔 | `DEFAULT_BUF_TTL = 10µs` |
| `queue_size` | 入站事件通道容量（背压） | `DEFAULT_QUEUE_SIZE = 8192` |
| `timeout` | 连接/单次写/单次读的超时 | `DEFAULT_TIMEOUT = 1s` |
| `token` | WebSocket 鉴权的 Bearer token | `None` |

**path 如何决定传输类型**（这是 `Config` 最关键的隐含语义）：

```
ws://... 或 wss://...   → WebSocket（可走 TLS，可附 token）
以 '/' 开头，或
  以 .sock/.socket/.ipc 结尾 → Unix socket
其余（如 host:port）     → TCP
```

#### 4.1.3 源码精读

`Config` 结构体本身与建造者方法都在这里：

[src/ipc.rs:73-82](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs#L73-L82) —— 七个私有字段。

[src/ipc.rs:84-99](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs#L84-L99) —— `Config::new` 填入必填的 `path`/`name`，其余从 `crate::DEFAULT_*` 常量取默认值：

```rust
pub fn new(path: &str, name: &str) -> Self {
    Self {
        path: path.to_owned(),
        name: name.to_owned(),
        buf_size: crate::DEFAULT_BUF_SIZE,
        buf_ttl: crate::DEFAULT_BUF_TTL,
        queue_size: crate::DEFAULT_QUEUE_SIZE,
        timeout: crate::DEFAULT_TIMEOUT,
        token: None,
    }
}
```

这些默认值定义在 `lib.rs` 顶部，**不受任何 feature 门控**：[src/lib.rs:43-47](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L43-L47)。注意 `buf_ttl` 只有 **10 微秒**——这意味着默认情况下出站缓冲几乎一填就尽快刷新，延迟极低。

[src/ipc.rs:100-120](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs#L100-L120) —— 链式建造者方法，每个都 `mut self` 后返回 `Self`，并附带说明：`token` 方法的注释明确它只用于 WebSocket 的 `Authorization: Bearer <token>` 头。

> 提醒：`Config` 的文档注释说 Unix socket path「must end with .sock/.socket/.ipc」，但实际分发逻辑（见 4.2.3）还额外接受「以 `/` 开头」的路径，比注释更宽松。以源码为准。

#### 4.1.4 代码实践

**目标**：理解 path 的传输分发，并感受 builder 的用法。

1. 打开 `src/ipc.rs`，定位 `Config::new` 与 `connect_broker`。
2. 对下面四个 path，判断它们各自会走哪种传输（先把答案写下，再去 `connect_broker` 对应分支核对）：
   - `/tmp/busrt.sock`
   - `/var/run/busrt.ipc`
   - `127.0.0.1:8811`
   - `wss://broker.example.com/ws`
3. 写一小段（示例代码，非项目原有）配置：`Config::new("/tmp/busrt.sock", "my.client").timeout(Duration::from_millis(500)).buf_size(4096)`，说明它相对默认值改了哪两个参数。

**预期结果**：前两个走 Unix，第三个走 TCP，第四个走 WebSocket（TLS）。改动了 `timeout` 与 `buf_size`。

#### 4.1.5 小练习与答案

**Q1**：如果把 `queue_size` 设得很小（比如 4），接收端会发生什么？

> **答**：`queue_size` 是入站事件通道（`async_channel::bounded`）的容量。它满了之后，`handle_read` 里 `tx.send(frame).await` 会阻塞读取循环，对代理形成背压；极端情况下代理侧写入会被卡住，进而可能触发超时或断连。默认 8192 给了较大余量。

**Q2**：`token` 字段对 Unix / TCP 客户端有意义吗？

> **答**：没有。它只在 WebSocket 分支里被读出并写进 HTTP `Authorization` 头（见 4.2.3）。Unix/TCP 不做这层鉴权，设了也被忽略。

---

### 4.2 connect / connect_broker / chat：从 socket 到就绪的启动流程

#### 4.2.1 概念说明

`Client::connect` 是对外入口，但它本身很薄：真正干活的是私有的 `connect_broker`。整个启动流程可以理解成三段：

1. **建链 + 选传输**：根据 `path` 建立对应类型的 socket 连接，拆分成「读半部 / 写半部」。
2. **握手**：调用 `chat`，和代理交换问候字节、校验协议版本、把客户端名注册给代理。
3. **启动后台读取**：把读半部丢进一个 `tokio::spawn` 的 `handle_read` 任务里常驻运行，把入站帧通过有界通道送给业务层。

握手阶段还有一个容易忽略的细节：握手字节是**直接 `write_all` 到原始写半部**（立即落网），而握手之后的业务写入则交给 `TtlBufWriter`（带 TTL 的缓冲写入）。所以连接建立瞬间握手必然先于任何业务帧到达。

#### 4.2.2 核心流程

```
Client::connect(config)                          对外入口，套一层 timeout
   └─ connect_broker(config, None)               真正建链 + 选传输
        ├─ 按 path 选 WebSocket / Unix / TCP
        ├─ 建立 stream → into_split() → (reader, writer)
        └─ connect_broker! 宏
             ├─ chat(name, reader, writer)       握手（直接写 writer）
             ├─ bounded(queue_size) → (tx, rx)   入站事件通道
             └─ tokio::spawn(handle_read(...))   后台读循环 → reader_fut
        返回 (Writer, reader_fut, rx) → 组装成 Client
```

握手 `chat` 的字节对话（与 u2-l3 的协议描述一致）：

```
客户端                                    代理
        ←  GREETINGS[0]=0xEB + version(LE u16)   （代理先发 3 字节）
   校验魔数与版本
        →  原样回送这 3 字节                       （客户端回 echo）
        ←  RESPONSE_OK(0x01)                      （代理确认握手）
        →  name_len(LE u16) + name bytes          （客户端发名字）
        ←  RESPONSE_OK(0x01)                      （代理确认注册，注册成功）
```

#### 4.2.3 源码精读

**对外入口**——`connect` 只是用 `tokio::time::timeout` 把整个建链+握手包起来，超时即报错：[src/ipc.rs:253-258](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs#L253-L258)。另有一个 `connect_stream` 允许传入**已存在**的 `UnixStream`（用于外部已接管 socket 建立的场景）。

**传输分发主体**——`connect_broker` 用一个大的 `if/else if/else` 按 `path` 选传输：[src/ipc.rs:260-372](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs#L260-L372)。三个分支结构高度一致：「建链 → `into_split` → 构造读缓冲 → 调 `connect_broker!` 宏 → 用写半部包一个 `TtlBufWriter` 存进 `Writer` 枚举」。差异只在建链方式：

- **WebSocket 分支** [src/ipc.rs:264-309](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs#L264-L309)：解析 URI、按 `buf_size` 配置 `WebSocketConfig`、若有 `token` 则加 `Authorization` 头、`connect_async` 后用 `WsStream` + `compat*` 把 futures 流适配成 tokio `AsyncRead/Write`。
- **Unix 分支** [src/ipc.rs:310-347](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs#L310-L347)：`UnixStream::connect`，读半部套 `BufReader`（Windows 下直接返回 `not_supported`）。
- **TCP 分支** [src/ipc.rs:348-372](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs#L348-L372)：`TcpStream::connect` + `set_nodelay(true)`（关闭 Nagle，降低小帧延迟）。

`Writer` 枚举把三种写半部统一成一个类型：[src/ipc.rs:55-71](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs#L55-L71)，其 `write` 方法按变体转发给内部的 `TtlBufWriter`。

**核心启动宏**——`connect_broker!` 把「握手 + 建通道 + spawn 读循环」三步打包：[src/ipc.rs:234-250](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs#L234-L250)。注意它把 `connected` 原子布尔的克隆搬进 `handle_read` 任务闭包，**读循环一旦结束（出错或 EOF）就把 `connected` 置为 `false`**——这就是外部客户端「会断线」的来源。

**握手函数**——`chat` 严格实现上面的字节对话：[src/ipc.rs:618-656](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs#L618-L656)。两个关键校验：[src/ipc.rs:628-633](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs#L628-L633) 校验 `GREETINGS[0]==0xEB` 与 `PROTOCOL_VERSION`（常量见 [src/lib.rs:21](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L21) 与 [src/lib.rs:37](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L37)）；随后两次等待代理回 `RESPONSE_OK`(0x01)，分别对应「握手确认」和「注册确认」，任一不符都把代理回送的字节经 `buf[0].into()` 转成 `ErrorKind` 上报：[src/ipc.rs:635-654](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs#L635-L654)。名字长度上限是 `u16::MAX`：[src/ipc.rs:623-625](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs#L623-L625)。

#### 4.2.4 代码实践

**目标**：追踪启动链，并实测连接与断线感知。

1. 启动一个 `busrtd` 监听 `/tmp/busrt.sock`（参照 u1-l2）。
2. 写一个最小客户端（示例代码，基于 `client_listener.rs` 改写）：`Client::connect(&Config::new("/tmp/busrt.sock", "probe"))` 后立刻打印 `client.is_connected()`。
3. 观察 `is_connected()` 返回 `true`。
4. 把 `busrtd` 杀掉，让连接对端的 reader 收到 EOF——理论上后台 `handle_read` 任务会退出并把 `connected` 置 `false`。如果你持有 `get_connected_beacon()` 返回的原子布尔，过一会儿应观测到它变 `false`。

**预期结果**：连接成功时 beacon 为 true；代理消失后，读取循环结束，beacon 翻转为 false。（精确翻转时机取决于 TCP/Unix 的 EOF 检测速度，可标注「待本地验证」。）

#### 4.2.5 小练习与答案

**Q1**：为什么 TCP 分支要调 `set_nodelay(true)`，而 Unix 分支不需要？

> **答**：`set_nodelay` 关闭的是 TCP 的 Nagle 算法。Nagle 会攒小包合并发送以提升吞吐，但会增加小帧延迟——这对一个 IPC 总线不可取，故显式关闭。Unix socket 是本机内存拷贝，没有 Nagle 这层，无此参数。

**Q2**：握手用原始 `write_all`，业务用 `TtlBufWriter`。如果颠倒（让握手也走缓冲）会怎样？

> **答**：握手响应必须尽快落网才能让对端推进状态机。若握手字节滞留在 `TtlBufWriter` 里等 TTL 刷新（虽然默认 10µs），会无谓增加建链延迟，且握手尚未完成时缓冲器尚未构造，逻辑上也说不通。

---

### 4.3 handle_read：入站帧解析循环与 ACK 兑现

#### 4.3.1 概念说明

`handle_read` 是连接建立后被 `tokio::spawn` 起来的后台任务，它是个**无限循环**：不断从读半部精确读取「6 字节头 + 主体」，解析成 `Frame`，再通过入站通道送给业务层。它还承担一个关键职责——**把代理回送的 ACK 帧兑现成发送方正在 `await` 的 `OpConfirm`**。

回忆 u4-l1：投递方法返回的 `OpConfirm` 是 `Option<oneshot::Receiver<Result<(),Error>>>`。对内部客户端它是「即时兑现的假确认」；而对外部客户端，发送方在发帧时把一个 oneshot 发送端存进 `responses` 表（见 4.4），**真正兑现它的就是这里的 `handle_read`**——收到代理回的 ACK 帧，查表、把结果发进对应的 oneshot 通道。

#### 4.3.2 核心流程

```
loop {
    read_exact(6 字节头)
    kind = buf[0]            // FrameKind
    realtime = buf[5] != 0
    match kind:
      Nop        → 跳过（心跳/占位）
      Acknowledge→ 解析 ack_id=u32(buf[1..5])
                    从 responses 表取出 oneshot::Sender
                    把 buf[5]（code）转成 Result 发进去 → 兑现 OpConfirm
      其他       → read_exact(len 字节主体)
                    解析 sender / 可选 topic / payload（用 0x00 切分）
                    包成 FrameData → tx.send(frame) → 送进事件通道
}
```

主体字段的切分规则（取决于 `kind`）：

- `FrameKind::Publish`：主体用 `0x00` 切成 3 段 → `sender 0x00 topic 0x00 payload`。
- 其他业务帧（如 `Message`/`Broadcast`）：切成 2 段 → `sender 0x00 payload`，topic 为 `None`。

这与 u2-l3 描述的代理→客户端帧格式（`kind | len | realtime` 头 + `0x00` 分隔的主体）完全对应。

#### 4.3.3 源码精读

整个函数：[src/ipc.rs:555-616](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs#L555-L616)。

**6 字节头读取与 kind 解析**：[src/ipc.rs:564-568](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs#L564-L568)。`buf[0].try_into()` 把 kind 字节转成 `FrameKind`（非法字节会在这里报错）。

**ACK 兑现**——这是外部客户端确认机制的核心：[src/ipc.rs:571-579](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs#L571-L579)。从 `buf[1..5]` 解出 `ack_id`（小端 u32），在 `responses` 表里 `remove` 出对应的 oneshot 发送端，把 `buf[5]`（code 字节）经 `to_busrt_result()` 转成 `Result<(), Error>` 发进去。如果查不到（比如对应帧没要求 ACK、或 ACK 来晚了），打印一条 orphaned 警告——**孤儿 ACK 不会让连接中断**，只是丢弃。

**业务帧主体解析**：[src/ipc.rs:580-613](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs#L580-L613)。注意主体读取套了一层 `tokio::time::timeout(timeout, ...)`（[src/ipc.rs:583](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs#L583)），防止恶意/异常的大 len 把读取挂死。解析出的 `sender`/`topic` 是 UTF-8 字符串，`payload_pos` 记录 payload 在 buf 中的起点，从而实现零拷贝载荷切片（与 u2-l1 的 `FrameData` 设计呼应）。最终 `tx.send(frame).await` 把帧送进事件通道：[src/ipc.rs:603-612](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs#L603-L612)，`FrameData::new` 的签名见 [src/lib.rs:423-441](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L423-L441)。

#### 4.3.4 代码实践

**目标**：理解 ACK 兑现与事件投递，并看懂接收端示例。

1. 打开 [examples/client_listener.rs](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/examples/client_listener.rs)。它先 `subscribe("#", QoS::Processed)`（[第 13-14 行](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/examples/client_listener.rs#L13-L14)），再 `take_event_channel()` 拿走 `handle_read` 喂数据的那个通道（[第 16 行](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/examples/client_listener.rs#L16)），然后在 `while let Ok(frame) = rx.recv().await` 循环里打印 `sender()/kind()/topic()/payload()`（[第 17-25 行](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/examples/client_listener.rs#L17-L25)）。
2. 对照本节源码，说明 listener 收到的每一帧 `frame` 是由 `handle_read` 的哪几行构造出来的（提示：`Arc::new(FrameData::new(...))`）。
3. 说明为什么 listener 在 `subscribe` 之后必须调用 `take_event_channel()`：因为通道接收端只能被取走一次，业务层需要持有它才能进入事件循环。

**预期结果**：帧由 `handle_read` 的 [src/ipc.rs:603-611](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs#L603-L611) 构造，经 `tx.send` 投递，listener 的 `rx.recv().await` 收到。

#### 4.3.5 小练习与答案

**Q1**：如果代理回了一个 ACK，但 `responses` 表里查不到对应 `ack_id`，会发生什么？

> **答**：打印 `orphaned busrt op ack <id>` 警告并丢弃（[src/ipc.rs:576-578](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs#L576-L578)）。连接不受影响。常见诱因：该帧用 `QoS::No`（不需要 ACK，没登记），或 ACK 迟到于超时。

**Q2**：`handle_read` 在解析 `Publish` 帧时用 `splitn(3, ...)`，而其他帧用 `splitn(2, ...)`。为什么？

> **答**：发布订阅帧携带 topic，主体是 `sender 0x00 topic 0x00 payload` 三段；点对点/广播帧不带 topic，主体是 `sender 0x00 payload` 两段。`splitn` 的次数对应段数，从而正确取出 topic 与 payload 起点。

---

### 4.4 发送帧宏族：从 prepare_frame_buf! 到 send_frame_and_confirm!

#### 4.4.1 概念说明

`AsyncClient` 的每个方法（send / publish / subscribe 等）最终都要变成线上字节。`ipc.rs` 没有为每个方法各写一套序列化代码，而是用一组**宏**把「拼帧头 + 决定确认 + 写出」的逻辑复用起来。为什么用宏而不是函数？源码注释直接说明了（[src/ipc.rs:136](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs#L136)）：为了保证内联、避免多余 future 状态机开销。

这一组宏从下到上分三层：

- `prepare_frame_buf!`：**生成帧 id + flags**，返回一个只含 `op_id(4) | flags(1)` 的缓冲。
- `send_frame!` / `send_zc_frame!`：**补全 9 字节头并拼上 target/receiver**，得到「头缓冲」。
- `send_frame_and_confirm!`：**按 QoS 决定是否登记确认通道**，然后分两次写出（头缓冲 + payload），返回 `OpConfirm`。

其中最巧妙的一点是**头缓冲与 payload 分两次写**：头部是有自己的 `Vec<u8>`（拥有），而 payload 直接用 `Cow::as_slice()` 的只读视图（零拷贝）。这样既不把 payload 拷进头部缓冲，又能让 payload 单独按实时性决定刷新策略。

#### 4.4.2 核心流程

一条 `send(target, payload, QoS)` 的完整编码链：

```
send()                                  AsyncClient 方法
 └─ send_frame!(target, payload, Message, qos)
      ├─ prepare_frame_buf!(op, qos, header_len)
      │     frame_id++（跳过 0，到 u32::MAX 回绕到 1）
      │     buf = frame_id.to_le_bytes()          // 4
      │         ++ (op as u8 | (qos as u8) << 6)  // 1 = flags
      ├─ buf += len(LE u32)                        // 4  → 至此 9 字节头完成
      ├─ buf += target + 0x00                      // 目标 + 分隔
      └─ send_frame_and_confirm!(&buf, &payload, qos)
           ├─ if qos.needs_ack():
           │     (tx, rx) = oneshot::channel()
           │     responses[frame_id] = tx          // 登记，等 ACK 兑现
           │     confirm = Some(rx)
           ├─ send_data_or_mark_disconnected!(&buf,   Flush::No)        // 写头
           ├─ send_data_or_mark_disconnected!(&payload, qos.is_realtime().into())  // 写载荷
           └─ Ok(confirm)
```

flags 字节的位划分（与 u2-l3 一致）：

\[ \text{flags} = \text{op} \;\big|\; (\text{qos} \ll 6) \]

即 op 占低 6 位、qos 占高 2 位。QoS 的两个位又分别决定两件事：

\[ \text{needs\_ack} = \text{qos} \,\&\, 1 \qquad \text{is\_realtime} = (\text{qos} \,\&\, 2) \ne 0 \]

于是 QoS 与本组宏的对应关系：

| QoS | needs_ack | is_realtime | 是否登记确认 | payload 刷新 |
| --- | --- | --- | --- | --- |
| No (0) | 否 | 否 | 否 | `Scheduled`（缓冲 + TTL） |
| Processed (1) | 是 | 否 | 是 | `Scheduled` |
| Realtime (2) | 否 | 是 | 否 | `Instant`（立即刷新） |
| ProcessedRealtime (3) | 是 | 是 | 是 | `Instant` |

`Flush::from(bool)` 把 `is_realtime` 映射成 `Instant`/`Scheduled`：[src/comm.rs:15-24](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/comm.rs#L15-L24)。

#### 4.4.3 源码精读

**帧 id 自增**：[src/ipc.rs:399-406](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs#L399-L406)。到 `u32::MAX` 回绕到 **1**（不是 0），保证 frame_id 恒 ≥1。

**`prepare_frame_buf!`**——生成 `op_id + flags`：[src/ipc.rs:138-146](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs#L138-L146)。`Vec::with_capacity` 的容量预算 `expected_header_len + 4 + 1` 已为后续追加 len 与 flags 预留，避免多次扩容。

**`send_frame!`**——补全 9 字节头并拼 target，有三种重载：[src/ipc.rs:199-232](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs#L199-L232)。

- 第一重载（target + payload）：用于 `send`/`publish`/`send_broadcast`。len = `t.len() + payload.len() + 1`。
- 第二重载（target + receiver + payload）：用于 `publish_for` 定向发布，多一段 `receiver 0x00`。
- 第三重载（仅 payload，无 target）：用于 `subscribe`/`unsubscribe`/`exclude` 等控制操作——此时「主题」就放在 payload 位置，len = `payload.len()`。

**`send_zc_frame!`**——零拷贝变体（带 header），用于 `zc_send`：[src/ipc.rs:182-197](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs#L182-L197)，比 `send_frame!` 多拼一段 header（线程内/RPC 的零拷贝元数据前缀）。

**`send_frame_and_confirm!`**——确认登记与分两次写出：[src/ipc.rs:165-180](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs#L165-L180)。关键三行：

```rust
let rx = if $qos.needs_ack() {
    let (tx, rx) = oneshot::channel();
    $self.responses.lock().insert($self.frame_id, tx);  // 登记确认
    Some(rx)
} else { None };
send_data_or_mark_disconnected!($self, $buf, Flush::No);              // 头：不刷新
send_data_or_mark_disconnected!($self, $payload, $qos.is_realtime().into()); // 载荷：按实时性
```

注意 `responses` 的键是**当前 frame_id**，与 ACK 里的 `ack_id` 严格对应——这就是发送端登记、`handle_read` 兑现的闭环。

**`send_data_or_mark_disconnected!`**——带超时的写出与断线标记：[src/ipc.rs:148-163](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs#L148-L163)。写入包在 `tokio::time::timeout(timeout, ...)` 里；一旦写失败，立刻 `reader_fut.abort()` + `connected.store(false)`，把客户端标记为已断线（与 4.2 的「读循环结束置 false」形成对称：读出错或写出错都会断线）。

**AsyncClient 实现把这些宏接到 trait 方法上**：[src/ipc.rs:412-547](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs#L412-L547)。例如 `send` 一行就委托给 `send_frame!`：[src/ipc.rs:422-429](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs#L422-L429)；`publish`：[src/ipc.rs:454-461](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs#L454-L461)；`subscribe` 用第三重载：[src/ipc.rs:478-480](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs#L478-L480)。`ping` 直接写 `PING_FRAME`（9 字节全零，见 [src/lib.rs:25](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L25)）并用 `Flush::Instant` 立即刷新：[src/ipc.rs:530-534](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs#L530-L534)。

**Drop 实现**：客户端析构时中止后台读任务，避免泄漏：[src/ipc.rs:549-553](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs#L549-L553)。

#### 4.4.4 代码实践

**目标**：把发送端示例走通，并对照宏理解每条调用。

1. 打开 [examples/client_sender.rs](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/examples/client_sender.rs)，它依次做了三件事：
   - `publish("some/topic", ..., QoS::Processed)`（[第 13-17 行](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/examples/client_sender.rs#L13-L17)）
   - `send("test.client.listener", ..., QoS::Processed)`（[第 19-27 行](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/examples/client_sender.rs#L19-L27)）
   - `send_broadcast("test.*", ..., QoS::Processed)`（[第 29-33 行](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/examples/client_sender.rs#L29-L33)）
2. 对这三条调用，分别指出它们落到 `send_frame!` 的哪个重载、各自用了哪个 `FrameOp`（Message / PublishTopic / Broadcast）。
3. 注意每条调用都 `.await?.expect("no op")` 后再 `opc.await??`：第一个 `?` 是发送是否成功（写入了 socket），`expect` 解开 `OpConfirm`（因为 `Processed` 的 `needs_ack` 为真，确认通道必为 `Some`），第二个 `opc.await??` 才是等代理回 ACK 并检查 code。
4. 想象把三条的 QoS 都改成 `QoS::No`：此时 `OpConfirm` 会变成 `None`，`.expect("no op")` 会 panic——说明运行时不能用 `Processed` 的写法去等一个 `No` 的确认。

**预期结果**：publish→`send_frame!` 第一重载 + `PublishTopic`；send→第一重载 + `Message`；send_broadcast→第一重载 + `Broadcast`。把 QoS 改 No 会因 `expect` 解 `None` 而 panic（待本地验证）。

#### 4.4.5 小练习与答案

**Q1**：为什么头缓冲用 `Flush::No`，而 payload 才按实时性决定 `Flush`？

> **答**：头部与载荷是同一条逻辑帧的两段，应作为一个整体决定何时落网。`Flush::No` 只写入 `BufWriter` 不触发刷新；payload 是第二段，它的 `Flush` 决定整帧何时真正发出——实时帧 `Instant` 立即刷新，普通帧 `Scheduled` 安排 TTL 后刷新。这样把刷新决策收口在最后一段，避免头先单独发出去。

**Q2**：发送端登记 `responses[frame_id]` 与 `handle_read` 取出之间，如果代理迟迟不回 ACK，会怎样？

> **答**：发送方 `opc.await`（即 oneshot Receiver 的 await）不会被兑现，会一直挂起。业务层通常配合 `tokio::time::timeout` 包裹这个 `opc.await`（或依赖发送时的 `timeout`）来避免永久阻塞；超时后该表项成为孤儿，后续 ACK 到达时走 4.3.5 Q1 的 orphaned 路径。

**Q3**：`subscribe` 为什么用 `send_frame!` 的「仅 payload」重载，而不是「target + payload」重载？

> **答**：订阅的「主题」对代理而言是操作参数（放在主体里），不是投递目标。9 字节头后面直接跟 `len + topic`，没有 target 段也没有 `0x00` 分隔——这对应协议里 `OP_SUBSCRIBE` 的帧格式。用第三重载正好生成这种无 target 的帧。

---

## 5. 综合实践

把本讲四个模块串起来，写一对能互通的程序。**目标**：sender 向某主题 publish，并向 listener 点对点 send；listener 订阅 `#` 后打印收到的每一帧的 `kind / topic / payload`。

### 操作步骤

1. **启动代理**（参照 u1-l2）：

   ```bash
   ./test.sh server   # 默认会绑 /tmp/busrt.sock 等多个 -B
   ```

2. **编写 listener**（以 `client_listener.rs` 为蓝本，示例代码）：

   ```rust
   use busrt::client::AsyncClient;
   use busrt::ipc::{Client, Config};
   use busrt::QoS;

   #[tokio::main]
   async fn main() -> Result<(), Box<dyn std::error::Error>> {
       let config = Config::new("/tmp/busrt.sock", "test.client.listener");
       let mut client = Client::connect(&config).await?;
       client.subscribe("#", QoS::Processed).await?.expect("no op").await??;
       let rx = client.take_event_channel().unwrap();
       while let Ok(frame) = rx.recv().await {
           println!(
               "kind={:?} topic={:?} payload={}",
               frame.kind(),
               frame.topic(),
               std::str::from_utf8(frame.payload()).unwrap_or("<?>"),
           );
       }
       Ok(())
   }
   ```

3. **编写 sender**（以 `client_sender.rs` 为蓝本，示例代码，去掉 broadcast，专注 publish + send）：

   ```rust
   use busrt::client::AsyncClient;
   use busrt::ipc::{Client, Config};
   use busrt::QoS;

   #[tokio::main]
   async fn main() -> Result<(), Box<dyn std::error::Error>> {
       let config = Config::new("/tmp/busrt.sock", "test.client.sender");
       let mut client = Client::connect(&config).await?;
       // 发布订阅
       client
           .publish("demo/topic", b"hello-topic".as_ref().into(), QoS::Processed)
           .await?.expect("no op").await??;
       // 点对点
       client
           .send("test.client.listener", b"hello-direct".as_ref().into(), QoS::Processed)
           .await?.expect("no op").await??;
       Ok(())
   }
   ```

4. **先起 listener，再起 sender**（顺序很重要：listener 要先完成 `subscribe("#")` 才能收到 publish 帧）。如果顺序反了，sender 的 publish 会因为「当时还没有订阅者」而无人接收。

### 需要观察的现象

- listener 应打印出两条帧：
  - 一条 `kind=Publish`、`topic=Some("demo/topic")`、payload=`hello-topic`（来自 publish）。
  - 一条 `kind=Message`、`topic=None`、payload=`hello-direct`（来自点对点 send，不带 topic）。
- 注意两类帧的 `topic` 字段差异——正好对应 4.3 里 `splitn(3)` 与 `splitn(2)` 的区别。
- 把 sender 的 QoS 改成 `QoS::No`，并把 `.expect("no op").await??` 改成只 `.await?`（不再等确认），观察 sender 是否立刻返回、listener 是否仍能收到——以此体会 `needs_ack` 对发送路径的影响。

### 预期结果

listener 收到 publish 与 send 各一条；publish 带 topic、send 不带 topic。改用 `QoS::No` 后 sender 不再阻塞等待 ACK，但消息照常送达（待本地验证：取决于你的 OS 与 socket 缓冲）。

---

## 6. 本讲小结

- **`Config`** 用建造者模式集中管理 7 个参数，`path` 字符串同时决定传输类型（`ws://`→WebSocket、`/`或特定后缀→Unix、其余→TCP），默认值强调低延迟（`buf_ttl=10µs`、`timeout=1s`）。
- **启动链** `connect → connect_broker → chat → handle_read`：`connect` 套超时；`connect_broker` 按 path 建链并拆读写半部；`chat` 完成 `0xEB`+版本号握手与名字注册；`handle_read` 被 spawn 成后台读循环。
- **`handle_read`** 解析代理→客户端的 6 字节头帧：`Nop` 跳过、`Acknowledge` 查 `responses` 表兑现 `OpConfirm`、业务帧按 `splitn` 切出 sender/topic/payload 包成 `FrameData` 投递给事件通道。
- **发送宏族** 三层复用：`prepare_frame_buf!` 生成 `op_id+flags`，`send_frame!` 补全 9 字节头并拼 target，`send_frame_and_confirm!` 按 `QoS.needs_ack()` 决定登记确认、按 `QoS.is_realtime()` 决定 payload 刷新策略。
- **QoS 两个正交位**同时驱动「是否登记确认」与「payload 是否立即刷新」，是连接发送路径与底层 `TtlBufWriter`（u4-l3）的枢纽。
- **外部客户端 vs 内部客户端**：前者有真实网络往返（ACK 要等、ping 真发、会断线），后者确认即时兑现、永不断线——二者通过同一个 `AsyncClient` trait 对上层透明。

## 7. 下一步学习建议

- **接着读 u4-l3（TtlBufWriter）**：本讲反复出现的 `Flush::No/Scheduled/Instant` 与 `TtlBufWriter` 的 TTL 刷新机制，是理解「普通帧为何能批量、实时帧为何能低延迟」的关键，下一讲会把缓冲器本身拆开讲。
- **进入 RPC 层（u5-l1）**：本讲的帧收发是 RPC 的传输底座。`RpcEvent` 正是 `TryFrom<Frame>`——建立在本讲 `handle_read` 投递出的 `Frame` 之上。理解了本讲的帧结构，再去读 RPC 的 method/payload 切分会非常自然。
- **回头对照 u3-l3 的分发宏**：本讲讲的是「客户端如何发帧」，u3-l3 讲的是「代理如何收帧并分发」。把发送端 `send_frame!` 的字节布局与代理端 `handle_reader` 的解析对应起来，就能完整看到一条消息从发出到送达的全过程。
- **建议继续阅读的源码**：[src/comm.rs](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/comm.rs)（缓冲器）、[src/broker.rs](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs) 中 `handle_reader`/`handle_peer`（对端视角的收发）。
