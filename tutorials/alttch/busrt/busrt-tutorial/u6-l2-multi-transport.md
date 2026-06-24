# 多传输层：Unix、TCP 与 WebSocket 服务端

## 1. 本讲目标

学完本讲，你应当能够：

- 理解 BUS/RT 服务端为何能在 **Unix socket、TCP、WebSocket（含 TLS）** 三种完全不同的传输上运行同一套业务逻辑，而核心代码几乎不重复。
- 看懂 `spawn_server!` 宏如何把「监听 → 接受连接 → 预处理 → 切分读写半部 → 交给 `handle_connection`」这条公共流水线抽象出来。
- 区分 `spawn_unix_server` / `spawn_tcp_server` / `spawn_websocket_server` 三者的实现差异，特别是 WebSocket 为何不使用通用宏。
- 理解 `handle_connection` 与 `prepare_*` 系列函数如何把任意传输「拍平」成一对通用的 `AsyncRead`/`AsyncWrite`，从而与上一讲（u6-l1）的连接生命周期无缝衔接。
- 掌握两处「按路径自动选传输」的分发点：服务端 `busrtd` 的 `-B` 绑定、客户端 `ipc::Client::connect` 的 `path`，以及与它们并列的 FIFO 特殊通道。

## 2. 前置知识

本讲承接 u6-l1（连接生命周期）。回顾几个关键概念：

- **传输（transport）**：物理层面消息走什么通道——Unix socket、TCP、WebSocket。它只决定「字节怎么搬」，不决定「消息怎么分发」。分发模式（点对点 / 广播 / 发布订阅）是正交的另一件事，已经在 u3-l3 讲过。
- **`handle_peer`**：上一讲讲的连接生命周期总指挥（握手 + reader/writer/pinger 三任务）。它接收的是一对已经准备好的读写流。
- **`AsyncRead`/`AsyncWrite`**：Rust 异步 IO 的通用 trait。tokio 的 `TcpStream`、`UnixStream` 都实现了它们；WebSocket 流通过兼容层（`compat()`）也能实现它们。**只要能变成这两个 trait 的对象，`handle_peer` 就能处理它**——这是本讲的灵魂。
- **`Frame = Arc<FrameData>`**：上一讲强调过，转发靠克隆引用计数而非复制字节，与具体传输无关。
- **`TtlBufWriter`**：u4-l3 讲过的出站缓冲层，`handle_connection` 会用它包裹每个连接的写半部。

如果上面这些名词你还不熟，建议先回看 u4-l3 和 u6-l1 再继续。

## 3. 本讲源码地图

| 文件 | 本讲涉及的内容 |
| --- | --- |
| `src/broker.rs` | 三种 `spawn_*_server`、`spawn_fifo`、`spawn_server!` 宏、`handle_connection`、`prepare_*` 辅助函数、`BusRtClientKind`、`ServerConfig`、`Broker` 结构 |
| `src/ipc.rs` | 客户端侧 `connect_broker` 如何按 `path` 选 Unix/TCP/WebSocket |
| `src/server.rs` | `busrtd` 二进制如何按 `-B` 绑定路径自动选择传输 |
| `examples/inter_thread.rs` | 实践蓝本：嵌入 Broker + Unix 服务端 |

永久链接基准为：

```
https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/
```

---

## 4. 核心概念与源码讲解

### 4.1 统一抽象层：`spawn_server!` 宏 + `handle_connection` + `prepare_*`

#### 4.1.1 概念说明

三种物理传输的「接受连接」过程其实高度相似：

1. 在一个地址上监听。
2. 循环 `accept()` 拿到一条新连接（一个流对象）。
3. 对流做一些**传输相关的预处理**（比如 TCP 要关掉 Nagle 算法）。
4. 把流切成「读半部 + 写半部」。
5. 把读写半部连同配置交给同一个**连接处理器**。

第 1 步「监听」的 API 不同（`UnixListener` vs `TcpListener`），第 4 步「切分」返回的类型也不同（`unix::OwnedWriteHalf` vs `tcp::OwnedWriteHalf`）。但第 2、3、5 步逻辑完全一致。

BUS/RT 用一个 **`spawn_server!` 宏**把这条公共流水线抽出来，再用几个极小的 `prepare_*` 函数把「传输相关的小差异」隔离掉。结果是：`spawn_unix_server` 和 `spawn_tcp_server` 各自只有十几行，真正的连接处理逻辑一行都不用重复。

#### 4.1.2 核心流程

通用服务循环的伪代码：

```
spawn 异步任务:
    loop:
        (stream, addr) = listener.accept().await   # 不同 listener，同一写法
        if prepare(stream) 出错: continue           # 传输相关预处理
        (reader, writer) = stream.into_split()      # 切读写半部
        client_source = prepare_source(addr)        # 提取对端来源（IP / 无）
        handle_connection(... reader, writer ...)    # 进入统一生命周期
```

预处理函数只有两个职责：

- `prepare_*_stream(stream)`：在握手前对流做设置（TCP 关 Nagle；Unix 无需操作）。
- `prepare_*_source(addr)`：把对端地址转成一个可选字符串，用于 AAA 鉴权与日志。

`handle_connection` 则是「传输无关」与「传输相关」的分界线：它一收到读写半部，就立刻用 `BufReader` 和 `TtlBufWriter` 包裹，然后 spawn `handle_peer`（u6-l1 的生命周期总指挥）。从这一刻起，代码再也不知道、也不需要知道这条连接来自 Unix 还是 TCP。

#### 4.1.3 源码精读

先看四个极小的辅助函数。`prepare_unix_stream` 是空操作（Unix socket 不需要关 Nagle），`prepare_tcp_stream` 调 `set_nodelay(true)` 关闭 Nagle 算法以降低小消息延迟：

[prepare_unix_stream / prepare_tcp_stream — src/broker.rs:1151-1161](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1151-L1161)：`prepare_unix_stream` 直接返回 `Ok(())`，`prepare_tcp_stream` 把 `set_nodelay` 的 IO 错误映射成 `Error`。

`prepare_tcp_source` 返回对端地址字符串（如 `127.0.0.1:54321`），供 AAA 主机白名单与日志使用；`prepare_unix_source` 返回 `None`，因为 Unix socket 对端没有网络地址：

[prepare_tcp_source / prepare_unix_source — src/broker.rs:1163-1171](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1163-L1171)

接着是分界线函数 `handle_connection`。注意它的两个入参类型用 `impl AsyncReadExt` / `impl AsyncWriteExt`，是**泛型 + 静态分发**，意味着任何能读写、满足约束的类型都能传进来：

[handle_connection — src/broker.rs:1173-1215](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1173-L1215)：它把 reader 包成 `BufReader`、writer 包成 `TtlBufWriter`（参见 u4-l3），把 `ServerConfig` 里的 `timeout`、`payload_size_limit`、`aaa_map` 与传入的 `kind`、`source` 组装成 `PeerHandlerParams`，然后 `tokio::spawn` 启动 `Broker::handle_peer`。错误时用 `pretty_error!` 打印。

`handle_connection` 的签名里有一个 `kind: BusRtClientKind` 参数。这是连接在协议层之外**唯一**保留下来的「传输身份」记录，标识这条连接是内部 / 本地 IPC / TCP / WebSocket 哪一种。它的取值在各自的 `spawn_*_server` 里硬编码传入：

[BusRtClientKind — src/broker.rs:499-516](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L499-L516)：四个变体 `Internal` / `LocalIpc` / `Tcp` / `WebSocket`，并提供 `as_str()` 给日志/统计用。

最后看核心的 `spawn_server!` 宏。它接受 7 个参数：`$self`、`$path`、`$listener`、`$config`、`$kind`、`$prepare`（流预处理函数名）、`$prepare_source`（来源提取函数名），展开成上面伪代码描述的循环：

[spawn_server! 宏 — src/broker.rs:1217-1256](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1217-L1256)：从 `$self` 克隆 `db`、`queue_size`、`direct_alloc_limit`、`async_allocator`，spawn 一个 `loop`：`accept()` → `$prepare(&stream)` → `into_split()` → `$prepare_source(&addr)` → `handle_connection(...)`。任务句柄被 push 进 `$self.services`（`Vec<JoinHandle<()>>`，见 [Broker 结构 src/broker.rs:1049-1055](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1049-L1055) 的 `services` 字段）。

注意宏里 `addr.into()` 这一行：`addr` 是 listener 返回的地址类型，转换成 `ClientIp`：

```rust
enum ClientIp { No, Addr(IpAddr) }
impl From<tokio::net::unix::SocketAddr> for ClientIp { ... => Self::No }
impl From<std::net::SocketAddr>       for ClientIp { ... => Self::Addr(addr.ip()) }
```

TCP 对端有 IP（用于 AAA 主机白名单），Unix 对端没有（记为 `No`）。这就是为什么 `prepare_unix_source` 返回 `None`、`prepare_tcp_source` 返回地址字符串——传输差异被收敛到这几个小函数里。

#### 4.1.4 代码实践（源码阅读型）

**目标**：亲手验证「传输差异只活在 `prepare_*` 和 `ClientIp` 里」。

1. 打开 [src/broker.rs:1217-1256](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1217-L1256) 的 `spawn_server!` 宏。
2. 找到 `handle_connection` 的调用点，数一下它一共接收多少个参数。
3. 在宏体里搜索任何「`TcpStream` / `UnixStream`」字样——你会发现**一个都没有**。宏体里出现的只有泛化的 `$listener`、`$stream`、`$prepare`。
4. 预期结果：你确认宏体对传输类型完全无知，全部差异由调用方传进来的 `$listener`、`$prepare`、`$prepare_source` 决定。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `prepare_tcp_stream` 里的 `set_nodelay(true)` 删掉，会对什么场景产生可观察的影响？

> 参考答案：TCP 默认开启 Nagle 算法，会把小包合并以提升吞吐，代价是增加延迟。BUS/RT 大量传输小消息帧，关掉 Nagle（`set_nodelay(true)`）能显著降低小消息的端到端延迟。删掉后，在「大量小消息」场景下你会观察到延迟上升；而 Unix socket 不经过 TCP 协议栈，没有 Nagle，所以 `prepare_unix_stream` 本来就是空操作。

**练习 2**：为什么 `ClientIp` 对 Unix 对端返回 `No`、对 TCP 对端返回 `Addr(ip)`？

> 参考答案：TCP 连接携带真实的远端 IP，可供 AAA 的 `hosts_allow` 主机白名单鉴权（见 u6-l3）。Unix socket 是本地文件系统路径通信，对端没有网络 IP 概念，只能记为 `No`——这也意味着 AAA 的主机白名单对 Unix 客户端没有意义。

---

### 4.2 Unix 与 TCP 服务端：`spawn_unix_server` / `spawn_tcp_server`

#### 4.2.1 概念说明

有了 4.1 的抽象层，启动一个具体传输的服务端就成了「选 listener + 填两个 prepare 函数名」的填空题。这两个方法就是把填空题填好：

- `spawn_unix_server`：用 `UnixListener`，传输标记 `LocalIpc`，配套 `prepare_unix_stream` / `prepare_unix_source`。
- `spawn_tcp_server`：用 `TcpListener`，传输标记 `Tcp`，配套 `prepare_tcp_stream` / `prepare_tcp_source`。

二者都接收一份 `ServerConfig`（缓冲大小、TTL、超时、AAA、载荷上限），并都把任务句柄存进 `broker.services`。

需要特别说明：**`spawn_unix_server` 和 `spawn_server_connection` 在 Windows 上不可用**（`#[cfg(not(target_os = "windows"))]`），因为 Windows 没有标准 Unix domain socket；TCP 与 WebSocket 则是跨平台的。

#### 4.2.2 核心流程

```
spawn_unix_server(path, config):
    remove_file(path)          # 清理可能残留的旧 socket 文件
    listener = UnixListener::bind(path)
    spawn_server!(..., listener, LocalIpc, prepare_unix_stream, prepare_unix_source)

spawn_tcp_server(path, config):
    listener = TcpListener::bind(path).await   # path 形如 "127.0.0.1:8891"
    spawn_server!(..., listener, Tcp, prepare_tcp_stream, prepare_tcp_source)
```

注意一个细节差异：`spawn_unix_server` 在 bind 前会先 `remove_file(path)` 删除可能残留的旧 socket 文件——否则重复启动会因「文件已存在」而 bind 失败。`spawn_tcp_server` 不需要这步（端口释放由内核管理）。

#### 4.2.3 源码精读

`ServerConfig` 是每种传输共享的「每服务端配置」，用建造者模式组装：

[ServerConfig — src/broker.rs:847-897](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L847-L897)：字段 `buf_size` / `buf_ttl` / `timeout` / `payload_size_limit` / `aaa_map`；`Default` 取 `DEFAULT_BUF_SIZE`、`DEFAULT_BUF_TTL`、`DEFAULT_TIMEOUT`；建造者方法 `buf_size` / `buf_ttl` / `timeout` / `aaa_map` / `payload_size_limit` 链式设置。

`spawn_unix_server`（带 `#[cfg(not(target_os = "windows"))]`，先删旧文件再 bind 再调宏）：

[spawn_unix_server — src/broker.rs:1444-1462](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1444-L1462)

`spawn_tcp_server`（直接 bind 再调宏）：

[spawn_tcp_server — src/broker.rs:1463-1479](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1463-L1479)

这两个方法的宏调用参数，正好是上一节「填空题」的标准答案——你能一眼看出，二者只有 `$listener`、`$kind`、`$prepare`、`$prepare_source` 四个参数不同。

此外还有一个 `spawn_server_connection`，它不走「监听 → accept」流程，而是让你直接塞进一条**已经存在的** `UnixStream`（例如由 systemd socket activation 传入）。它内部同样调用 `handle_connection`：

[spawn_server_connection — src/broker.rs:1591-1614](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1591-L1614)

#### 4.2.4 代码实践

**目标**：用一个嵌入 Broker 同时监听 Unix socket 和 TCP 端口，证明两种传输可共存于同一个 `Broker` 实例。

1. 复制 `examples/inter_thread.rs`（它已有一个 Unix 服务端）作为蓝本。
2. 在 `spawn_unix_server` 之后再加一行 `spawn_tcp_server`，绑定 `127.0.0.1:8891`。
3. 注册一个内部监听客户端 `echo`，取出它的 event channel，在 `tokio::spawn` 里循环打印收到的帧的 `sender()` 与 `payload()`。
4. 分别用两个**外部** `ipc::Client` 连接——一个 `Config::new("/tmp/busrt-dual.sock", "unix.client")`、一个 `Config::new("127.0.0.1:8891", "tcp.client")`——各向 `echo` 发一条点对点消息。
5. 编译运行（需要 `broker` + `ipc` feature）：
   ```bash
   cargo run --example <你的示例名> --features "broker ipc"
   ```
   > 注：自定义示例需在 `Cargo.toml` 的 `[[example]]` 段登记，或直接改写 `examples/inter_thread.rs` 本体（本讲不要求改源码，建议新建一个示例文件并补登记）。
6. 观察预期结果：`echo` 客户端会打印出两行，`sender()` 分别为 `unix.client` 与 `tcp.client`，证明两条不同传输的连接都被同一个 `Broker` 路由到同一个内部客户端。这就是「传输无关」的直接证据。

> 如果你不方便登记新示例，最小验证方式是分两个进程：进程 A 跑改写后的双服务端 Broker；进程 B 跑两份 `examples/client_sender.rs`（分别把 `Config::new` 的 path 改成 unix 路径与 tcp 地址），观察进程 A 的 `echo` 是否都收到。

#### 4.2.5 小练习与答案

**练习 1**：同一个 `Broker` 可以同时绑定多个 TCP 端口吗？多个 Unix socket 呢？

> 参考答案：都可以。`spawn_tcp_server` / `spawn_unix_server` 每次调用都把一个新的 accept 循环任务 push 进 `broker.services`，`Broker` 对服务端数量没有上限。`busrtd` 正是靠 `-B` 多次重复来同时绑定多个端点的（见 4.4）。

**练习 2**：为什么 `spawn_unix_server` 要在 `bind` 前调用 `remove_file`，而 `spawn_tcp_server` 不用？

> 参考答案：Unix socket 在文件系统里是一个真实文件。进程异常退出后旧 socket 文件会残留，再次 `bind` 同一路径会因「文件已存在」而失败，所以要先删。TCP 端口由内核管理，进程退出后端口会被释放（除非处于 TIME_WAIT），不需要手动清理。

---

### 4.3 WebSocket 服务端（含 TLS）：`spawn_websocket_server`

#### 4.3.1 概念说明

WebSocket 与前两种传输有一个本质区别：**它要在一条 TCP 连接之上再做一次应用层握手**（HTTP Upgrade）。这个握手本身是一个带超时、可能失败的异步协商，无法塞进 `spawn_server!` 宏那个简单的「accept → split → handle」流水线里。

因此 `spawn_websocket_server` **没有使用通用宏**，而是内联了一段结构类似的循环，并为每条连接额外 spawn 一个独立任务来做：

1. （可选）TLS 握手——如果传入了 `tls_config`。
2. WebSocket 握手（`accept_async_with_config`）。
3. 把 WS 流通过兼容层转成 `AsyncRead`/`AsyncWrite`，再交给 `handle_connection`。

启用 TLS 时协议串记为 `wss://`，否则为 `ws://`。注意一个重要事实：**`busrtd` 命令行二进制并不暴露 WebSocket 绑定**（见 4.4），WebSocket 服务端是**嵌入场景专用**的——需要 TLS 的 WebSocket 接入只能通过在自己代码里调用 `spawn_websocket_server` 实现。

#### 4.3.2 核心流程

```
spawn_websocket_server(path, config, tls_config):
    listener = TcpListener::bind(path)              # path 形如 "0.0.0.0:8443"
    tls_acceptor = tls_config.map(TlsAcceptor::from)
    ws_proto = if tls { "wss" } else { "ws" }
    spawn loop:
        (stream, addr) = listener.accept()
        prepare_tcp_stream(stream)                  # WS 底层也是 TCP，照样关 Nagle
        spawn 独立任务(每连接一个):
            if 有 tls_acceptor:
                tls_stream = tls_acceptor.accept(stream)   # 可能超时/失败
                prepare_and_handle(tls_stream)
            else:
                prepare_and_handle(stream)

prepare_and_handle(stream):
    ws_config = 设置读/写缓冲、消息上限为 u32::MAX
    ws = async_tungstenite::accept_async_with_config(stream, ws_config)  # WS 握手，带超时
    (r, w) = WsStream::new(ws).split()
    handle_connection(..., r.compat(), w.compat_write(), ..., WebSocket, ...)
```

为什么消息上限设成 `u32::MAX`？因为 BUS/RT 自己在 `ServerConfig.payload_size_limit` 里做应用层载荷上限校验（在 `handle_peer` 里），没必要让 WebSocket 层再做一次更严的限制，这里把 WS 层的额度开到最大，把真正的限制权留给应用层。

#### 4.3.3 源码精读

签名：第三个参数 `tls_config: Option<Arc<rustls::ServerConfig>>`，`Some` 表示启用 TLS（`wss`），`None` 表示明文（`ws`）：

[spawn_websocket_server — src/broker.rs:1480-1590](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1480-L1590)：底层用 `TcpListener`，外层 accept 循环与 `spawn_server!` 几乎一样，但每条连接再 `tokio::spawn` 一个任务，先按 `tls_acceptor` 是否存在决定是否做 TLS 握手，再调内层宏 `prepare_and_handle!`。

内层宏 `prepare_and_handle!`（定义在方法体内、[src/broker.rs:1517-1564](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1517-L1564)）负责 WS 握手与兼容层转换。关键三步：

```rust
// 1. 构造 WS 配置：缓冲用 config.buf_size，消息/帧上限放开到 u32::MAX
let mut ws_config = tungstenite::protocol::WebSocketConfig::default();
ws_config.read_buffer_size = config.buf_size;
ws_config.write_buffer_size = config.buf_size;
ws_config.max_message_size = Some(usize::try_from(u32::MAX).unwrap());
// 2. WS 握手（带超时），失败/超时则记日志并 return
let (r, w) = tokio::time::timeout(timeout,
        async_tungstenite::tokio::accept_async_with_config($stream, Some(ws_config)))
    .await ... ;
let (r, w) = ws_stream_tungstenite::WsStream::new(ws).split();
// 3. 用 compat() / compat_write() 把 futures 的 AsyncRead/Write 适配成 tokio 的
handle_connection(db, r.compat(), w.compat_write(), ..., BusRtClientKind::WebSocket, ...);
```

TLS 分支在外层 accept 循环里：[src/broker.rs:1565-1581](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1565-L1581)。有 `tls_acceptor` 时先 `tls_acceptor.accept(stream)`（同样带超时），成功拿到 `tls_stream` 再喂给 `prepare_and_handle!`；否则直接喂原始 `stream`。

这里出现的 `tokio_util::compat`（[broker.rs 导入 src/broker.rs:43](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L43)）是关键的「桥接器」：WebSocket 库 `async-tungstenite` 基于 `futures` 的 `AsyncRead/AsyncWrite`，而 tokio 的 `BufReader`/`TtlBufWriter` 基于 tokio 自己的 `AsyncReadExt/AsyncWriteExt`。`.compat()` / `.compat_write()` 在两套 trait 之间做零成本适配，让 WS 流也能进入 `handle_connection` 这个统一入口。

依赖关系（见 `Cargo.toml` 的 `[features]`）：`broker` feature 会一次性引入 `rustls`、`tokio-rustls`、`async-tungstenite`、`ws_stream_tungstenite`、`tungstenite`、`futures-util`、`tokio-util` 这一整套 WebSocket/TLS 栈（[Cargo.toml:69-72](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/Cargo.toml#L69-L72)）。

#### 4.3.4 代码实践（源码阅读型）

**目标**：理解 WebSocket 为何必须为每条连接单独 spawn 任务。

1. 对比 [spawn_server! 宏 src/broker.rs:1217-1256](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1217-L1256) 与 [spawn_websocket_server src/broker.rs:1480-1590](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1480-L1590)。
2. 在 WS 版本里找到 `tokio::spawn(async move { ... })` 这层「每连接独立任务」。
3. 思考：如果像 `spawn_server!` 那样在 accept 循环里**直接**做 WS 握手（不 spawn 独立任务），会发生什么？
4. 预期结论：WS 握手（尤其叠加 TLS 握手）耗时不确定且带超时。若在 accept 循环里同步等待，一个慢握手会**阻塞整个 accept 循环**，导致后续新连接排队无法被 accept。所以必须为每条连接单独 spawn，让 accept 循环立刻回到 `listener.accept()` 继续接客。

#### 4.3.5 小练习与答案

**练习 1**：一个明文 `ws://` 客户端能否连上启用了 `tls_config` 的 WebSocket 服务端？

> 参考答案：不能。启用了 `tls_config` 后，服务端会对每条新连接先做 TLS 握手（`tls_acceptor.accept`）。明文 `ws://` 客户端不会说 TLS，TLS 握手会失败/超时被记录后丢弃。客户端必须用 `wss://` 并信任服务端证书（rustls 用 native certs）。

**练习 2**：为什么 WS 层的 `max_message_size` 要设成 `u32::MAX`，而不是一个小值来防 DoS？

> 参考答案：因为载荷上限的真正把关者是应用层的 `ServerConfig.payload_size_limit`，它会在 `handle_peer` 里按业务语义统一校验（对所有传输生效）。如果在 WS 层再设一个小限制，会出现「WS 客户端被卡、而 Unix/TCP 客户端不受限」的不一致行为。把 WS 层额度开到最大、把限制权统一交给应用层，能保证三种传输的载荷策略一致。

---

### 4.4 传输选择：两处自动分发点与 FIFO 特殊通道

#### 4.4.1 概念说明

4.1–4.3 讲的是「服务端方法怎么实现」。但用户怎么**触发**它们？BUS/RT 有两处「按路径字符串自动选传输」的分发点，规则高度对称：

| 分发点 | 位置 | 触发方式 |
| --- | --- | --- |
| 服务端 | `busrtd` 二进制 | 命令行 `-B <path>`（可多次） |
| 客户端 | `ipc::Client` | `Config::new(path, name)` 的 `path` |

两者的路径判定规则几乎一致（见 4.4.3 表格）。此外还有一条**与 socket 完全不同**的特殊通道——**FIFO**，它没有连接、没有握手，是给 shell 脚本用的命名管道入口。

#### 4.4.2 核心流程

**服务端 `busrtd` 分发**（每个 `-B` 走一遍）：

```
for path in opts.path:
    if path 以 "fifo:" 开头:    spawn_fifo(去掉前缀)
    elif path 以 '/' 开头 或以 .sock/.socket/.ipc 结尾:  spawn_unix_server(path)
    else:                       spawn_tcp_server(path)
```

**客户端 `ipc::Client::connect_broker` 分发**（单个 path）：

```
if path 以 "ws://" / "wss://" 开头:    WebSocket 分支
elif path 以 '/' 开头 或以 .sock/.socket/.ipc 结尾:  Unix 分支
else:                                  TCP 分支
```

注意两处不对称：客户端能识别 `ws://` 选 WebSocket，而 `busrtd` 的 `-B` **不识别** WebSocket（没有对应分支）——再次印证 WebSocket 服务端只能嵌入使用。

**FIFO 通道**则完全另起一路：它不走 `handle_connection`，而是循环从命名管道读行，按行首字符解析成不同命令，再通过**核心 RPC 客户端**（u5-l3 的 `.broker`）发送：

```
spawn_fifo(path, buf_size):
    要求核心 RPC 客户端已初始化，否则返回 not_supported
    创建命名管道(path) 并设置权限 0o622
    spawn loop:
        逐行读取
        send_fifo_cmd(line)

send_fifo_cmd(line):
    若以 '=' 开头      → publish 主题
    否则拆出 target:
        target 含 * 或 ? → send_broadcast
        否则 payload:
            以 '.' 开头 → rpc.notify
            以 ':' 开头 → rpc.call0（参数按 param=value 转 msgpack）
            否则       → 普通 send
```

#### 4.4.3 源码精读

**服务端分发**在 `src/server.rs`，对每个 `-B` 路径判定（fifo 前缀优先，其次按后缀/前缀判 Unix，否则 TCP）：

[server.rs 绑定分发 — src/server.rs:228-262](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/server.rs#L228-L262)：注意 `fifo:` 分支带 `#[cfg(feature = "rpc")]` 守卫——FIFO 强依赖 RPC 与核心 RPC 客户端。

判定规则速查表：

| `path` 写法 | 服务端 `busrtd` | 客户端 `ipc::Client` |
| --- | --- | --- |
| `fifo:/path` | FIFO 通道 | （不支持） |
| `/tmp/x.sock`、`x.socket`、`x.ipc`、`/...` | Unix 服务端 | Unix 客户端 |
| `host:port` | TCP 服务端 | TCP 客户端 |
| `ws://host:port` / `wss://...` | （不支持） | WebSocket 客户端 |

**客户端分发**在 `src/ipc.rs` 的 `connect_broker`。三个分支结构完全对称，都是「建连 → `into_split` → `connect_broker!` 宏握手 → 包成 `Writer` 变体」：

[ipc::connect_broker 路径分发 — src/ipc.rs:260-385](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs#L260-L385)。其中三个分支：

- WebSocket 分支 [src/ipc.rs:265-309](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs#L265-L309)：解析 URL，可选带 `Authorization: Bearer <token>` 头，握手后包成 `Writer::WebSocket`。
- Unix 分支 [src/ipc.rs:310-347](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs#L310-L347)：`UnixStream::connect`，包成 `Writer::Unix`（Windows 上返回 `not_supported("unix sockets")`）。
- TCP 分支 [src/ipc.rs:348-372](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs#L348-L372)：`TcpStream::connect` 并 `set_nodelay`，包成 `Writer::Tcp`。

三种传输共用同一个握手宏 `connect_broker!`（在 u4-l2 讲过），所以握手后的 reader 循环、ACK 机制对三种传输完全一致。客户端用一个 `Writer` 枚举把三种写半部收拢，调用 `write` 时按变体分发：

[ipc::Writer 枚举与 write — src/ipc.rs:55-71](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs#L55-L71)：`Unix` / `Tcp` / `WebSocket` 三个变体都内含一个 `TtlBufWriter<...>`，`write` 方法只是 match 分发到内层的 `TtlBufWriter::write`。

**FIFO 特殊通道**：`spawn_fifo`（带 `#[cfg(feature = "broker-rpc")]`）创建命名管道并 spawn 读行循环：

[spawn_fifo — src/broker.rs:1615-1660](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1615-L1660)：开头检查 `rpc_client` 是否已设置，未设置则返回 `Error::not_supported(BROKER_RPC_NOT_INIT_ERR)`；创建管道后设权限 `0o622`（所有者读写、其他用户只写，供 shell `echo > fifo`）。

命令解析 `send_fifo_cmd`，按行首/前缀分流四种语法：

[send_fifo_cmd — src/broker.rs:1661-1731](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1661-L1731)。四种命令语法（文档见方法 doc 注释 [src/broker.rs:1616-1623](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1616-L1623)）：

```
echo TARGET MESSAGE > /path/to/fifo        # 点对点；TARGET 含 * ? 则广播
echo '=TOPIC' MESSAGE                      # 发布到主题
echo TARGET .MESSAGE                       # RPC 通知
echo TARGET :method param=value param=...  # RPC 调用（参数转 msgpack）
```

#### 4.4.4 代码实践

**目标**：用 `busrtd` 一个进程同时绑定 Unix socket、TCP 端口和 FIFO，验证三者在同一代理里共存，且 FIFO 能触发消息。

1. 用 `test.sh server` 启动 `busrtd`，传三个 `-B`（覆盖三种传输）：
   ```bash
   ./test.sh server -B /tmp/busrt.sock -B 127.0.0.1:8891 -B fifo:/tmp/busrt.fifo
   ```
   > 若 `test.sh` 不便用，可直接：
   > ```bash
   > cargo run --bin busrtd --features server -- -B /tmp/busrt.sock -B 127.0.0.1:8891 -B fifo:/tmp/busrt.fifo
   > ```
   > `busrtd` 默认 `init_default_core_rpc()`，所以 FIFO 所依赖的核心 RPC 客户端已就绪。
2. 另开终端，用 `busrt` CLI 连 Unix socket 监听广播主题：
   ```bash
   cargo run --bin busrt --features cli -- -B /tmp/busrt.sock listen '#'
   ```
3. 向 FIFO 写一条发布命令，触发一条主题消息：
   ```bash
   echo '=news/tech hello-from-fifo' > /tmp/busrt.fifo
   ```
4. 观察预期结果：第 2 步的 `listen '#'` 终端应打印出 `news/tech` 主题下的 `hello-from-fifo` 消息。再用 `busrt` CLI 跑 `broker stats`（向 `.broker` 发 RPC）确认 Unix 与 TCP 两个端点都已注册客户端。
5. 进一步：把 CLI 的 `-B` 换成 `127.0.0.1:8891`，重复第 3 步，确认 TCP 客户端同样能收到 FIFO 触发的消息——说明三种传输在同一 `Broker` 内完全互通。

> 若无法运行，标注「待本地验证」：核心要观察的是「FIFO 写入 → 经核心 RPC 客户端 → 经 publish! 宏分发 → 被 Unix/TCP 两种传输上的订阅者都收到」这条链路成立。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `spawn_fifo` 在核心 RPC 客户端未初始化时返回 `not_supported`，而 `spawn_unix_server` 不需要这个前置条件？

> 参考答案：FIFO 命令的执行（点对点 / 广播 / 发布 / RPC 调用）**全部**通过核心 RPC 客户端（u5-l3 的 `.broker`）来发送，没有它就无处发命令。而 Unix/TCP 服务端走的是 `handle_connection` → `handle_peer` 的标准连接生命周期，每条连接自带握手与帧解析，不依赖核心 RPC 客户端。

**练习 2**：客户端 `ipc::Client` 的路径 `10.0.0.1:8891` 会被判定为哪种传输？为什么不会被误判成 Unix？

> 参考答案：判定为 TCP。因为它既不以 `ws://`/`wss://` 开头，也不以 `/` 开头，也不以 `.sock`/`.socket`/`.ipc` 结尾（判断顺序见 [src/ipc.rs:310-314](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs#L310-L314)），于是落入最后的 TCP 分支 `TcpStream::connect`。

**练习 3**：为什么 `busrtd -B ws://0.0.0.0:8443` 不会启动一个 WebSocket 服务端？

> 参考答案：`server.rs` 的绑定分发只有 `fifo:` / Unix / TCP 三个分支，根本没有识别 `ws://`。WebSocket 服务端（`spawn_websocket_server`）只能在嵌入代码里调用，`busrtd` 不暴露它。

---

## 5. 综合实践

把本讲四个模块串起来，设计一个「四传输同框」的小任务：

**场景**：写一个嵌入 `Broker`，同时开放 **Unix + TCP + WebSocket** 三种 socket 服务端，并用 **FIFO** 通道接收 shell 命令。所有传输上的客户端都能互相通信。

**步骤**：

1. 创建嵌入 `Broker`，调用 `init_default_core_rpc().await`（为 FIFO 与 announce 准备核心 RPC 客户端）。
2. 依次 `spawn_unix_server("/tmp/multi.sock", ...)`、`spawn_tcp_server("127.0.0.1:8891", ...)`、`spawn_websocket_server("127.0.0.1:8892", None, ...)`（先用明文 `ws`，`tls_config` 传 `None`）。
3. 调用 `spawn_fifo("/tmp/multi.fifo", 4096).await`。
4. 注册一个内部客户端 `aggregator`，取出 event channel，循环打印每条收到的消息的 `sender()` 与 `topic()`（用 `frame.topic()` 区分是否为发布订阅消息）。
5. 用三种**外部**客户端分别连上三种 socket（unix path、`127.0.0.1:8891`、`ws://127.0.0.1:8892`），名字分别取 `c.unix` / `c.tcp` / `c.ws`，各向 `aggregator` 发一条点对点消息。
6. 用 shell `echo '=topic/x hi' > /tmp/multi.fifo` 触发一条发布消息，让 `aggregator` 也订阅 `#` 接收。
7. 预期：`aggregator` 打印出 4 条来源不同的消息（3 条点对点 + 1 条 FIFO 触发的发布），分别来自 `c.unix` / `c.tcp` / `c.ws` 与 `topic/x`。
8. 加分项：给 WebSocket 换上 `tls_config`，用一个自签证书让 `wss://` 客户端连入，验证 TLS 路径。

**观察重点**：

- 四种「入口」（三种 socket accept 循环 + FIFO 读行循环）都被 push 进同一个 `broker.services`，背后是同一个 `BrokerDb` 路由表。
- 无论消息从哪种传输进来，最终都汇入 `aggregator` 的 event channel——这正是「传输无关」的终极证明。
- WebSocket 那条连接在 `handle_connection` 之前多经历了「WS 握手（+ 可选 TLS 握手）」，但之后与 Unix/TCP 走完全相同的生命周期。

> 如果本地不便编译运行（尤其 TLS），至少完成「阅读源码画出四传输 → `handle_connection` → `handle_peer` → 路由表」的数据流图，并标注每种传输在 `handle_connection` 之前的差异化预处理步骤。

## 6. 本讲小结

- BUS/RT 服务端用 **`spawn_server!` 宏**把「监听 → accept → 预处理 → 切分 → `handle_connection`」这条公共流水线抽象出来，Unix 与 TCP 两种服务端因此各只需十几行。
- **`handle_connection`** 是「传输相关」与「传输无关」的分界线：它把任意读写半部用 `BufReader`/`TtlBufWriter` 包裹后 spawn `handle_peer`，从此代码不再关心传输类型；唯一保留的传输身份是 `BusRtClientKind`。
- 传输差异被收敛到几个极小的 `prepare_*` 函数：`prepare_tcp_stream` 关 Nagle、`prepare_tcp_source` 取对端 IP，而 Unix 版本分别是空操作和 `None`。
- **WebSocket 服务端**不使用通用宏，因为它在 TCP 之上还要做一次带超时的 WS（及可选 TLS）握手，必须为每条连接单独 spawn 任务；它通过 `tokio_util::compat` 把 futures 流适配成 tokio 流再进 `handle_connection`。
- 有两处「按路径自动选传输」的分发点（`busrtd` 的 `-B` 与 `ipc::Client` 的 `path`），规则高度对称；但 **WebSocket 服务端只能嵌入使用**，`busrtd` 不暴露它。
- **FIFO** 是与 socket 完全不同的特殊通道：无连接、无握手，按行解析命令并通过核心 RPC 客户端发送，强依赖 `broker-rpc` feature 与已初始化的核心 RPC 客户端。

## 7. 下一步学习建议

- **u6-l3（AAA 访问控制）**：本讲提到 `prepare_tcp_source` 取对端 IP、`ClientIp::Addr` 供 AAA 使用。下一讲会讲清 `ClientAaa` 的四类权限与主机白名单 `hosts_allow` 如何在 `handle_peer` 与 `handle_reader` 里拦截帧——你会看到本讲的「对端 IP」如何被真正用于鉴权。
- **u8-l1（busrtd 独立服务端）**：如果想看 `-B` 多绑定、信号处理、daemonize 的完整工程实现，直接精读 `src/server.rs`。
- **u8-l3（FIFO 与 announce）**：本讲只讲了 FIFO 的命令解析骨架，更完整的命令语义、`.broker/info` 与 `.broker/warn` 的 announce 机制将在 u8-l3 展开。
- 建议继续阅读的源码：[src/broker.rs:1217-1256](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1217-L1256)（宏）、[src/broker.rs:1480-1590](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1480-L1590)（WebSocket）、[src/ipc.rs:260-385](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs#L260-L385)（客户端分发）。
