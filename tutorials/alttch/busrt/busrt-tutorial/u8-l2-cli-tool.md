# busrt CLI：调试与基准工具

## 1. 本讲目标

`busrt`（注意不是 `busrtd`）是 BUS/RT 项目自带的命令行客户端。它本身**不是**服务端，而是一个连接到代理的**调试、观测与基准测试工具**。学完本讲，你应当能够：

- 说出 `busrt` CLI 的全部子命令（`broker` / `listen` / `send` / `publish` / `rpc` / `benchmark`）的用途与参数；
- 解释全局参数 `Opts`（`path` / `name` / `token` / `timeout` / `buf-size` / `queue-size` 等）如何映射到底层 `ipc::Config`；
- 理解 `broker info/stats/test/client.list` 这些「看似内置」的命令，本质上是向代理自身的 `.broker` 目标发起的 RPC 调用；
- 看懂 `print_payload` 对收到的载荷做 JSON → MessagePack → HEX 的三级自动解码逻辑；
- 掌握内置基准测试（基于 `bma-benchmark`）的工作方式，并能用它测出吞吐量。

本讲建立在 **u4-l2（ipc::Client 连接与帧收发）** 与 **u5-l2（RpcClient 与 RpcHandlers 处理器）** 之上：CLI 复用的正是这两个组件，本讲只讲「CLI 这层壳」如何把它们组织起来。

## 2. 前置知识

阅读本讲前，建议你已经了解：

- **ipc::Client 与 Config**（u4-l2）：CLI 的每一次操作都先调用 `ipc::Client::connect` 建立到代理的连接，`path` 字段决定走 Unix socket / TCP / WebSocket。
- **AsyncClient trait**（u4-l1）：`send` / `send_broadcast` / `publish` / `subscribe` 返回 `Result<OpConfirm, Error>`，`OpConfirm` 是否为 `Some` 由 `QoS::needs_ack()` 决定。
- **RpcClient 与 Rpc trait**（u5-l2）：`notify` / `call0` / `call` 三种 RPC 调用，以及 `RpcClient::new` 会在构造时自动 `spawn(processor)`。
- **核心 RPC 客户端**（u5-l3）：代理注册了名为 `.broker` 的内部客户端，调用代理就等价于向 `.broker` 发点对点 RPC，内置方法有 `test` / `info` / `stats` / `client.list`。
- **clap**：Rust 生态最常用的命令行参数解析库，用 derive 宏把 `struct`/`enum` 直接变成命令行接口。

> 小术语：**子命令（subcommand）** 指 `busrt <path> send ...` 里 `send` 这一层；**全局参数** 指 `<path>` 与 `-n/--name`、`--timeout` 等紧跟在 `busrt` 之后、对所有子命令生效的选项。

## 3. 本讲源码地图

本讲几乎全部聚焦在单个文件，少量类型来自公共模块：

| 文件 | 作用 |
| --- | --- |
| [src/cli.rs](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/cli.rs) | CLI 的全部逻辑：clap 定义、子命令分发、载荷解码、基准测试、`main`。 |
| [src/common.rs](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/common.rs) | `BrokerInfo` / `BrokerStats` / `ClientList` 等结构，以及 `str_to_params_map`（把 `key=value` 字符串解析成 RPC 参数 map）。 |
| [src/rpc/async_client.rs](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/async_client.rs) | `Rpc` trait（`notify`/`call0`/`call`）与 `RpcClient`，CLI 直接调用这些方法。 |
| [Cargo.toml](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/Cargo.toml) | `cli` feature 与 `[[bin]] name = "busrt"` 的门控定义。 |
| [test.sh](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/test.sh) | 一键启动脚本，`cli` 分支展示了 CLI 的标准调用方式。 |

先记住一句话定位：**`busrt` CLI = `ipc::Client`（建连）+ `RpcClient`（RPC）+ clap 参数壳 + 载荷解码与基准测试的胶水代码。**

---

## 4. 核心概念与源码讲解

### 4.1 子命令体系与全局参数 Opts

#### 4.1.1 概念说明

CLI 的命令行接口由 clap 的 derive 宏描述。整体是一个两层结构：

- **顶层 `Command` 枚举**：决定「这次要干什么」——`broker` / `listen` / `send` / `publish` / `rpc` / `benchmark`。
- **全局 `Opts` 结构**：承载对所有命令都生效的公共配置（连哪个代理、用什么名字、超时多久）。

其中 `broker` 与 `rpc` 是「嵌套子命令」，它们各自又带一层子枚举（`BrokerCommand`、`RpcCommand`）；其余（`listen`/`send`/`publish`/`benchmark`）是「叶子命令」，直接带自己的参数结构。

#### 4.1.2 核心流程

```text
命令行 argv
   │
   ▼
Opts::parse()        ← clap 解析：拆出全局参数 + 子命令
   │
   ▼
match opts.command {  ← 根据 Command 分发到不同处理分支
   Broker(...)  → 调 .broker RPC（info/stats/test/client.list）
   Listen(...)  → 订阅主题，循环打印收到的帧
   Send(...)    → 点对点 send 或（名字含通配符时）广播
   Publish(...) → 发布主题
   Rpc(...)     → RPC listen / notify / call0 / call
   Benchmark(..)→ 跑基准测试
}
```

每一个分支在做真正的业务前，都会先调用 `create_client(&opts, &client_name)` 建立一个 `ipc::Client`。

#### 4.1.3 源码精读

顶层命令枚举 [src/cli.rs:114-124](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/cli.rs#L114-L124)：注意 `Broker` 和 `Rpc` 上方的 `#[clap(subcommand)]` 标注，说明它们各自再展开一层子命令。

```rust
#[derive(Clone, Subcommand)]
enum Command {
    #[clap(subcommand)]
    Broker(BrokerCommand),
    Listen(ListenCommand),
    r#Send(TargetPayload),
    Publish(PublishCommand),
    #[clap(subcommand)]
    Rpc(RpcCommand),
    Benchmark(BenchmarkCommand),
}
```

> 细节：变体名是 `r#Send`（raw identifier），clap 会把变体名转换为小写，所以命令行里输入的是 `send`。同理 `Call0` → `call0`。

`broker` 子命令的下一层 [src/cli.rs:44-54](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/cli.rs#L44-L54)，用 `#[clap(name = ...)]` 显式指定了带点号的名字（`client.list`）：

```rust
enum BrokerCommand {
    #[clap(name = "client.list")] ClientList,
    #[clap(name = "info")]        Info,
    #[clap(name = "stats")]       Stats,
    #[clap(name = "test")]        Test,
}
```

`rpc` 子命令的下一层 [src/cli.rs:90-96](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/cli.rs#L90-L96)：

```rust
enum RpcCommand {
    Listen(RpcListenCommand),
    Notify(TargetPayload),
    Call0(RpcCall),
    Call(RpcCall),
}
```

全局参数 `Opts` [src/cli.rs:126-147](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/cli.rs#L126-L147)，每个字段都对应 `ipc::Config` 的一个旋钮：

```rust
struct Opts {
    #[clap(name = "socket path or host:port")] path: String,   // 代理地址，决定传输类型
    #[clap(short = 'n', long = "name")]      name: Option<String>,
    #[clap(long = "buf-size",   default_value = "8192")] buf_size: usize,
    #[clap(long = "queue-size", default_value = "8192")] queue_size: usize,
    #[clap(long = "timeout",    default_value = "5")]    timeout: f32,
    #[clap(long, help = "Bearer token for authentication")] token: Option<String>,
    #[clap(short = 'v', long = "verbose")] verbose: bool,
    #[clap(short = 's', long = "silent")]  silent: bool,
    #[clap(subcommand)] command: Command,
}
```

这些参数如何变成一个 `ipc::Client`，看 `create_client` [src/cli.rs:368-379](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/cli.rs#L368-L379)：建造者模式逐项设置后 `connect`，几乎是一比一地把 `Opts` 翻译成 `Config`：

```rust
async fn create_client(opts: &Opts, name: &str) -> Client {
    let mut config = Config::new(&opts.path, name)
        .buf_size(opts.buf_size)
        .queue_size(opts.queue_size)
        .timeout(Duration::from_secs_f32(opts.timeout));
    if let Some(token) = &opts.token {
        config = config.token(token);
    }
    Client::connect(&config).await.expect("Unable to connect to the busrt broker")
}
```

客户端名未指定时由 `main` 自动生成 `cli.<hostname>.<pid>` [src/cli.rs:570-582](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/cli.rs#L570-L582)，避免多个 CLI 实例重名冲突。

> 值得注意：`main` 标注了 `#[tokio::main(worker_threads = 1)]`（[src/cli.rs:567](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/cli.rs#L567)），即整个 CLI 跑在单线程的 current-thread 运行时上。对调试工具足够；基准测试的并发靠在该运行时上 `tokio::spawn` 多个异步任务实现（异步 I/O 多路复用，而非多线程）。

#### 4.1.4 代码实践

1. **目标**：用 clap 自带的帮助功能，从外部确认本节描述的子命令树。
2. **操作步骤**：构建并查看帮助（构建命令取自 [test.sh:12-13](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/test.sh#L12-L13)）：
   ```sh
   cargo run --release --bin busrt --features cli -- --help
   cargo run --release --bin busrt --features cli -- broker --help
   cargo run --release --bin busrt --features cli -- rpc --help
   ```
3. **需要观察的现象**：顶层应列出 `broker / listen / send / publish / rpc / benchmark` 六个子命令；`broker` 下应出现 `info / stats / test / client.list`；`rpc` 下应出现 `listen / notify / call0 / call`。
4. **预期结果**：帮助文本与 `Command` / `BrokerCommand` / `RpcCommand` 三个枚举一一对应。
5. 若本机尚未编译过该 feature 组合，第一次 `cargo run` 会触发编译，耗时尚不确定——**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Command` 枚举里 `Broker` 和 `Rpc` 需要加 `#[clap(subcommand)]`，而 `Listen` / `Send` 不需要？

> **答案**：`Broker` / `Rpc` 各自还嵌套了一个子枚举（`BrokerCommand` / `RpcCommand`），`#[clap(subcommand)]` 告诉 clap「这里再展开一层子命令」。`Listen` / `Send` 是叶子命令，参数直接由其携带的 struct（`ListenCommand` / `TargetPayload`）描述，无需再分层。

**练习 2**：`Opts::timeout` 默认值是 `5`，它的单位是什么？它最终被传给了哪个底层字段？

> **答案**：单位是秒（`f32`），在 `create_client` 中经 `Duration::from_secs_f32(opts.timeout)` 转成 `Duration`，再交给 `Config::timeout`，成为 `ipc::Client` 的操作超时。

---

### 4.2 broker 子命令：核心 RPC 客户端的封装

#### 4.2.1 概念说明

`busrt broker info/stats/test/client.list` 看起来像是「CLI 直接查询代理」，但本质上是 **CLI 作为普通客户端，向代理自身的 `.broker` 目标发起一次点对点 RPC 调用**，再把返回的 MessagePack 字节反序列化成结构体、用 `prettytable` 打印成表格。这正是 u5-l3 讲过的「核心 RPC 客户端」机制：代理内置了 `test` / `info` / `stats` / `client.list` 等方法。

#### 4.2.2 核心流程

四个分支的套路完全一致：

```text
create_client(.)
   │
   ▼
RpcClient::new(client, DummyHandlers{})   ← 不需要处理入站 RPC，用 DummyHandlers
   │
   ▼
rpc.call(".broker", "<method>", empty_payload!(), QoS::Processed)   ← 等待回复
   │
   ▼
rmp_serde::from_slice(result.payload())   ← 把 MessagePack 还原成 Rust 结构
   │
   ▼
ctable(...).printstd()                     ← 打印表格
```

`client.list` 额外做了一步 `clients.clients.sort()` 与「过滤掉自己」的处理。

#### 4.2.3 源码精读

以 `stats` 为例 [src/cli.rs:652-665](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/cli.rs#L652-L665)：

```rust
BrokerCommand::Stats => {
    let rpc = RpcClient::new(client, DummyHandlers {});
    let result = wto!(rpc.call(".broker", "stats", empty_payload!(), QoS::Processed)).unwrap();
    let stats: BrokerStats = rmp_serde::from_slice(result.payload()).unwrap();
    let mut table = ctable(vec!["field", "value"]);
    table.add_row(row!["r_frames", stats.r_frames]);
    table.add_row(row!["r_bytes", stats.r_bytes]);
    // ... w_frames / w_bytes / uptime
    table.printstd();
}
```

`BrokerStats` 结构定义在 [src/common.rs:41-49](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/common.rs#L41-L49)，字段就是表格里打印的那些：

```rust
pub struct BrokerStats {
    pub uptime: u64,
    pub r_frames: u64, pub r_bytes: u64,
    pub w_frames: u64, pub w_bytes: u64,
}
```

`BrokerInfo` 类似 [src/common.rs:51-56](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/common.rs#L51-L56)，只有 `author` 与 `version`。

`client.list` 稍复杂 [src/cli.rs:623-651](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/cli.rs#L623-L651)：反序列化成 `ClientList` 后排序、跳过自己（`if c.name != client_name`），再逐行打印每个客户端的名字、类型、来源 IP、端口、读写帧/字节、队列与实例数。对应的 `ClientInfo` 字段见 [src/common.rs:12-23](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/common.rs#L12-L23)。

> `wto!` 宏（「with timeout」）定义在 [src/cli.rs:612-618](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/cli.rs#L612-L618)，它把任意 future 包进 `tokio::time::timeout(timeout, ..)` 并在超时时 `expect("timed out")` 直接 panic 退出。`rpc.call` 内部已经把「发送请求 + 等待回复」整个往返做完，所以这里只需一层 `wto!`。

#### 4.2.4 代码实践

1. **目标**：亲眼看到 `broker` 子命令返回的表格，并理解它是一次 RPC。
2. **操作步骤**（需要先有一个运行中的代理，参考 u1-l2 的 `test.sh server` 分支）：
   ```sh
   # 终端 A：启动代理（监听 /tmp/busrt.sock）
   sh test.sh server

   # 终端 B：查询
   sh test.sh cli broker info
   sh test.sh cli broker stats
   sh test.sh cli broker client.list
   sh test.sh cli broker test
   ```
3. **需要观察的现象**：`info` 输出 author/version 两行；`stats` 输出收发帧/字节与 uptime；`client.list` 列出当前连到代理的客户端（至少包含这个 CLI 自己，但会被过滤掉，所以你会看到代理的 `.broker` 等其它客户端）；`test` 返回一段载荷。
4. **预期结果**：每个命令都打印一张 `ctable` 生成的对齐表格。
5. 表格的确切行数取决于代理上当前挂了多少客户端——**待本地验证**具体内容。

#### 4.2.5 小练习与答案

**练习 1**：`broker stats` 用的是 `QoS::Processed`，如果改成 `QoS::No` 会发生什么？

> **答案**：`QoS::No` 的 `needs_ack()` 为假，`rpc.call` 仍会发送请求帧并等待回复（`call` 内部按 `call_id` 登记 oneshot 通道，与 QoS 是否 ACK 无关），但代理不会再回 `OP_ACK` 确认帧。对 `call` 而言关键的是「方法返回的 RPC 回复帧」而非 ACK，所以通常仍能拿到结果；但语义上 `Processed` 更准确地表达了「我要确认这条请求已被代理处理」。

**练习 2**：为什么这些命令用 `DummyHandlers` 而不是自定义 handler？

> **答案**：CLI 在这里只**发起** RPC 调用、不**接收**任何入站 RPC，因此 `processor` 不会触发 `handle_call`。`RpcClient::new(client, DummyHandlers {})`（参见 [src/rpc/async_client.rs:268-272](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/async_client.rs#L268-L272)）正是为「只调用不响应」准备的便捷构造。

---

### 4.3 消息收发子命令：listen / send / publish

#### 4.3.1 概念说明

这三个子命令对应 BUS/RT 的三种「消息」玩法：

- `listen`：订阅主题，然后阻塞循环，把收到的每一帧打印出来——调试 pub/sub 的「接收端」。
- `send`：向某个对端**点对点**发消息；但若目标名里含通配符 `*` / `?`，CLI 自动改走**广播** `send_broadcast`。
- `publish`：向某个**主题**发布消息，所有订阅该主题（含 `+`/`#` 通配）的客户端都会收到。

三者发送时统一用 `QoS::Processed`，即发送后还要等待代理的 ACK 确认。

#### 4.3.2 核心流程

```text
send 分支：
  payload = 命令行参数 或 读 stdin
  if target 含 '*' 或 '?':
      send_broadcast(target, payload, Processed)   ← 广播
  else:
      send(target, payload, Processed)              ← 点对点
  wto!( 确认通道 ).unwrap()  →  打印 OK

listen 分支：
  exclude_topics(.)  →  subscribe_topics(.)   ← 先排除再订阅
  take_event_channel()
  spawn 打印循环：while rx.recv() { print_frame(frame) }
  while is_connected(): ping(); sleep 500ms   ← 主循环保活，断线即退出
```

#### 4.3.3 源码精读

`send` 的广播自动判定 [src/cli.rs:710-720](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/cli.rs#L710-L720)：

```rust
Command::r#Send(ref cmd) => {
    let mut client = create_client(&opts, &client_name).await;
    let payload = get_payload(cmd.payload.as_deref()).await;
    let fut = if cmd.target.contains(&['*', '?'][..]) {
        client.send_broadcast(&cmd.target, payload.into(), QoS::Processed)
    } else {
        client.send(&cmd.target, payload.into(), QoS::Processed)
    };
    wto!(wto!(fut).unwrap().unwrap()).unwrap().unwrap();
    ok!();
}
```

> 这里的「双层 `wto!`」值得拆开看（回顾 u4-l1 的 `OpConfirm = Option<oneshot::Receiver<Result<(), Error>>>`）：内层 `wto!(fut).unwrap().unwrap()` 先在超时内拿到发送结果、再拆出 `OpConfirm` 里的确认 `Receiver`（`QoS::Processed` 保证是 `Some`）；外层 `wto!(receiver).unwrap().unwrap()` 再在超时内等待这个确认通道兑现（拆掉 `RecvError` 与 `Error` 两层）。也就是说「发送」与「确认」各有一个独立超时。

`publish` 几乎一样，只是固定调 `publish` [src/cli.rs:721-732](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/cli.rs#L721-L732)。

`listen` 的接收循环 [src/cli.rs:686-709](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/cli.rs#L686-L709)：先 `exclude_topics` 再 `subscribe_topics`（顺序对应 u4-l1 注释里「先 exclude 再 subscribe」的建议），然后取走事件通道，spawn 一个 `while rx.recv()` 循环调用 `print_frame`。主线程每 500ms `ping` 一次保活，一旦 `is_connected()` 为假就退出并 `abort` 打印任务。

载荷来源 `get_payload` [src/cli.rs:360-366](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/cli.rs#L360-L366)：命令行给了字符串就用字符串字节，否则 `read_stdin` 把标准输入整段读进来（终端模式下会提示 `Ctrl-D to finish`，见 [src/cli.rs:350-358](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/cli.rs#L350-L358)）。

#### 4.3.4 代码实践

1. **目标**：用两个 CLI 实例验证 pub/sub 与点对点投递。
2. **操作步骤**：
   ```sh
   # 终端 A：监听所有主题
   sh test.sh cli listen -t '#'

   # 终端 B：发布 + 点对点
   sh test.sh cli publish news/tech "hello topic"
   sh test.sh cli send "<A终端打印的client_name>" "hello p2p"
   ```
3. **需要观察的现象**：终端 A 在 `publish` 后立即打印出一条 `Publish` 帧，`topic:` 显示 `news/tech`、载荷显示 `hello topic`；`send` 后再打印一条 `Message` 帧，不带 topic。
4. **预期结果**：`print_frame` 会先打印帧类型（黄色）、`from <sender> (<primary_sender>)`、可选 topic，再打印载荷（见 [src/cli.rs:290-303](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/cli.rs#L290-L303)）。
5. 若不确定终端 A 的客户端名，可先在终端 C 跑 `sh test.sh cli broker client.list` 查看——**待本地验证**实际名字。

#### 4.3.5 小练习与答案

**练习 1**：执行 `sh test.sh cli send "worker.*" hi` 会走点对点还是广播？为什么？

> **答案**：走广播。目标串 `worker.*` 含 `*`，`cmd.target.contains(&['*', '?'][..])` 为真，因此调用 `send_broadcast`（对应 u3-l3 的广播掩码分发）。

**练习 2**：`listen` 子命令为什么要在主循环里反复 `ping`？

> **答案**：`ipc::Client` 是外部客户端，连接可能因网络或代理主动踢出而断开。主循环用 `is_connected()` 判断存活、用 `ping` 维持心跳；一旦断开就跳出循环并 `abort` 打印任务，避免 CLI 在连接已死时还空转。

---

### 4.4 RPC 子命令与载荷编解码

#### 4.4.1 概念说明

`rpc` 子命令有四个动作，正好对应 u5-l2 的三种 RPC 调用再加一个监听：

| 子命令 | 调用的 RPC 方法 | 是否需要回复 |
| --- | --- | --- |
| `rpc listen` | 订阅主题后用真实 `Handlers` 跑 processor，接收并打印入站 RPC | — |
| `rpc notify` | `Rpc::notify`（通知帧 `0x00`，无 method） | 否 |
| `rpc call0` | `Rpc::call0`（请求帧但 id 填全零） | 否 |
| `rpc call` | `Rpc::call`（带自增 id，登记 oneshot 等回复） | 是 |

而**载荷编解码**是本模块的另一重点：CLI 收到任意载荷时，`print_payload` 会按 **JSON → MessagePack → HEX** 的顺序尝试自动识别并漂亮打印；发送 RPC 参数时，`key=value` 字符串经 `str_to_params_map` 转成 map 再用 MessagePack 序列化。

#### 4.4.2 核心流程

RPC 调用的载荷准备（`prepare_rpc_call!` 宏 [src/cli.rs:597-610](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/cli.rs#L597-L610)）：

```text
params == ["-"]      → 读 stdin 作为原始 payload
params 为空          → 空 payload
否则                 → str_to_params_map(["k=v", ...]) → rmp_serde::to_vec_named(..)  (MessagePack)
```

收到的载荷解码（`print_payload` [src/cli.rs:179-227](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/cli.rs#L179-L227)）：

```text
逐字节检查：是否「像文本」（无 < 9 的控制字节）？
 ├─ 像文本 且是合法 UTF-8
 │   ├─ 能解成 JSON → 打印 "JSON:" + pretty
 │   └─ 否则       → 打印 "STR: <原文>"
 └─ 否则（二进制）
     ├─ 能解成 MessagePack → 打印 "MSGPACK:" + 转 JSON pretty（失败则 hex）
     ├─ silent 模式        → 原始字节直接写 stdout
     └─ 否则               → print_hex（超 256 字节截断 + "..."）
```

#### 4.4.3 源码精读

`rpc call` 分支 [src/cli.rs:772-784](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/cli.rs#L772-L784)，成功则把返回载荷交给 `print_payload`，失败则打印 RPC 错误码并以退出码 1 退出：

```rust
RpcCommand::Call(cmd) => {
    let (rpc, payload) = prepare_rpc_call!(cmd, client);
    match wto!(rpc.call(&cmd.target, &cmd.method, payload.into(), QoS::Processed)) {
        Ok(result) => print_payload(result.payload(), opts.silent).await,
        Err(e) => {
            let message = e.data().map_or("", |data| std::str::from_utf8(data).unwrap_or(""));
            error!("RPC Error {}: {}", e.code(), message);
            std::process::exit(1);
        }
    }
}
```

> `rpc.call` 返回 `Result<RpcEvent, RpcError>`（见 [src/rpc/async_client.rs:115-121](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/async_client.rs#L115-L121) 与 [src/rpc/async_client.rs:370-388](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/async_client.rs#L370-L388)），内部已用自增 `call_id`（回绕到 1 以避开「id=0 不需回复」）登记 oneshot 通道并等满整个往返，所以 CLI 只需一层 `wto!`。错误对象的 `code()` / `data()` 定义在 [src/rpc/mod.rs:234-238](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/mod.rs#L234-L238)。

`rpc notify` 与 `rpc call0` 分支 [src/cli.rs:749-771](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/cli.rs#L749-L771)：二者都返回 `Result<OpConfirm, Error>`，因此和 4.3 的 `send` 一样用「双层 `wto!`」分别处理发送与确认。

`rpc listen` 分支 [src/cli.rs:736-748](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/cli.rs#L736-L748)：用真实 `Handlers`（而非 `DummyHandlers`）构造 `RpcClient`，于是 processor 收到入站 RPC 时会触发 `Handlers` 的 `handle_call` / `handle_notification` / `handle_frame`，三者都把内容打印出来（实现见 [src/cli.rs:307-348](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/cli.rs#L307-L348)）。

两个解码函数很薄 [src/cli.rs:169-177](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/cli.rs#L169-L177)：

```rust
fn decode_msgpack(payload: &[u8]) -> Result<Value, rmp_serde::decode::Error> {
    rmp_serde::from_slice(payload)
}
fn decode_json(payload: &str) -> Result<BTreeMap<Value, Value>, serde_json::Error> {
    serde_json::from_str(payload)
}
```

参数解析 `str_to_params_map` 在 [src/common.rs:60-86](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/common.rs#L60-L86)：把每个 `key=value` 切开，值按 `true/false` → Bool、整数 → I64、浮点 → F64、其余 → String 自动推断类型，组装成 `HashMap<&str, Value>`。

#### 4.4.4 代码实践

1. **目标**：观察 `print_payload` 的三级解码，以及 `rpc call` 对 `.broker` 内置方法的调用。
2. **操作步骤**（代理已在运行）：
   ```sh
   # 1) 调用内置 test，返回 {"ok":true}，应被识别为 JSON
   sh test.sh cli rpc call .broker test

   # 2) 调用内置 client.list，返回 MessagePack，应打印 "MSGPACK:" + JSON 视图
   sh test.sh cli rpc call .broker client.list

   # 3) 带参数调用（演示 key=value 解析；找一个接受参数的方法，或对 .broker info 传空参）
   sh test.sh cli rpc call .broker info
   ```
3. **需要观察的现象**：第 1 步打印 `JSON:` 前缀加格式化对象；第 2 步打印 `MSGPACK:` 前缀，内容被转成 JSON 数组/对象；若返回值既不是合法文本也不是合法 MessagePack，会退化为 `HEX: ...`。
4. **预期结果**：与 `print_payload` 的分支逻辑完全吻合。
5. 不同方法返回的精确字段以代理内置 RPC 为准——**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：假设某方法返回字节 `[0x82, 0xa3, 'f','o','o', 0x01, 0xa3, 'b','a','r', 0x02]`（MessagePack 的 `{"foo":1,"bar":2}`），`print_payload` 会走哪个分支？为什么不会先走 JSON？

> **答案**：走 MessagePack 分支。第一个字节 `0x82` < 9 为假（`0x82 = 130`），但判定「像文本」时需所有字节都 ≥ 9——这里多数可打印，关键是没有字节 < 9，所以**会先尝试文本/JSON**。然而这些字节不是合法 UTF-8/JSON，于是 `decode_json` 失败，最终落到 `decode_msgpack` 成功，打印 `MSGPACK:` 加 `{"bar":2,"foo":1}`。（这题提醒：判定顺序是 JSON 优先，只有文本路径全失败才试 MessagePack。）

**练习 2**：`rpc call foo.bar k=10 name=abc` 中，`k=10` 和 `name=abc` 最终以什么形式到达服务端？

> **答案**：`str_to_params_map` 把 `k=10` 解析成 `I64(10)`、`name=abc` 解析成 `String("abc")`，得到 `{"k": I64(10), "name": String("abc")}`，再由 `rmp_serde::to_vec_named` 序列化成 MessagePack map 字节，作为 RPC 请求的 params 载荷发送。

---

### 4.5 benchmark 子命令：内置基准测试

#### 4.5.1 概念说明

`benchmark` 子命令用 [`bma-benchmark`](https://crates.io/crates/bma-benchmark) crate 做吞吐量压测。它一次运行会跑两组场景：

- **RPC 场景**（`benchmark_rpc`）：`rpc.call`（往返）、`rpc.call + handle`（本地 handler 回环）、`rpc.call0`（不等回复）。
- **消息场景**（`benchmark_client`）：`send.qos.no`、`send.qos.processed`、`send+recv.qos.no`、`send+recv.qos.processed`。

每组都用多个 worker（独立客户端）并发发送，最后 `staged_benchmark_print!` 汇总打印每秒操作数。

#### 4.5.2 核心流程

```text
BenchmarkCommand { workers, payload_size, iters }
   │
   ▼
benchmark_rpc:    每 worker 一个 RpcClient，循环 call/call0
                 ├─ rpc.call       → 目标 .broker / 方法 benchmark.test（代理内置）
                 ├─ rpc.call+handle→ 目标自己 / 方法 benchmark.selftest（本机 handler 回环）
                 └─ rpc.call0      → 目标 <name>-null / 方法 test
   │
   ▼
benchmark_client: 每 worker 一个 ipc::Client + 独立接收任务
                 ├─ send.qos.no / send.qos.processed
                 └─ send+recv.qos.no / send+recv.qos.processed
   │
   ▼
staged_benchmark_print!()   ← 打印各场景 ops/s
```

负载是固定字节 `vec![0xee; payload_size]`（[src/cli.rs:398](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/cli.rs#L398) 与 [src/cli.rs:496](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/cli.rs#L496)）。`iters` 在 worker 间均分：`iters_worker = iters / workers`。

#### 4.5.3 源码精读

`BenchmarkCommand` 的三个旋钮 [src/cli.rs:104-112](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/cli.rs#L104-L112)：

```rust
struct BenchmarkCommand {
    #[clap(short = 'w', long = "workers", default_value = "1")] workers: u32,
    #[clap(long = "payload-size", default_value = "100")] payload_size: usize,
    #[clap(short = 'i', long = "iters", default_value = "1000000")] iters: u32,
}
```

主分发 [src/cli.rs:787-812](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/cli.rs#L787-L812)：先打印参数摘要，依次跑 `benchmark_rpc` 与 `benchmark_client`，最后 `staged_benchmark_print!()` 汇总。

`benchmark_rpc` 中关键的 `rpc.call + handle` 回环 [src/cli.rs:548-555](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/cli.rs#L548-L555)：目标设为「自己」，方法 `benchmark.selftest`，由本进程的 `BenchmarkHandlers` 处理并原样回送载荷（[src/cli.rs:473-486](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/cli.rs#L473-L486)）：

```rust
async fn handle_call(&self, event: RpcEvent) -> RpcResult {
    if event.parse_method()? == "benchmark.selftest" {
        Ok(Some(event.payload().to_vec()))
    } else {
        Err(RpcError::method(None))
    }
}
```

而 `rpc.call` 场景 [src/cli.rs:541-547](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/cli.rs#L541-L547) 打的是代理内置的 `.broker` / `benchmark.test` 方法，因此**测的是「经代理往返」的吞吐**；`rpc.call + handle` 测的是「本机 RPC 处理器回环」（不经代理业务逻辑，但仍过完整 RPC 编解码）。两组对比能区分「代理转发开销」与「RPC 框架开销」。

`benchmark_client` 的发送宏 `spawn_sender!` [src/cli.rs:422-437](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/cli.rs#L422-L437)：每个 worker 发到一个「空目标」（`<name>-null`，没有接收方，仅测发送/代理侧开销），按 QoS 决定是否等待 ACK；`send+recv` 场景额外 spawn 一个接收任务数到 `iters_worker` 帧（[src/cli.rs:460-467](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/cli.rs#L460-L467)）。

> 注意基准默认跑 100 万次，量很大。`bma_benchmark` crate 由 [src/cli.rs:20-21](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/cli.rs#L20-L21) 的 `#[macro_use] extern crate bma_benchmark;` 引入，提供 `staged_benchmark_start!` / `staged_benchmark_finish_current!` / `staged_benchmark_print!`。

#### 4.5.4 代码实践

1. **目标**：跑一次基准，记录各场景吞吐，并体会参数对结果的影响。
2. **操作步骤**（代理已运行；先用小规模避免跑太久）：
   ```sh
   # 单 worker、1000 次、100 字节负载（快速冒烟）
   sh test.sh cli benchmark -w 1 -i 1000 --payload-size 100

   # 再加大 worker 与 payload 对比
   sh test.sh cli benchmark -w 4 -i 100000 --payload-size 1024
   ```
3. **需要观察的现象**：终端先打印参数摘要，随后逐个场景输出 ops/s（每秒操作数）；`rpc.call` 通常低于 `rpc.call0`（一个要等回复、一个不等）；`send.qos.no` 通常高于 `send.qos.processed`（后者每个 ACK 多一次往返）。
4. **预期结果**：`staged_benchmark_print!` 汇总一张表，列出各场景名与吞吐。
5. 具体数值取决于机器、传输（Unix socket vs TCP）与负载大小——**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `send.qos.processed` 的吞吐通常明显低于 `send.qos.no`？

> **答案**：`QoS::Processed` 的 `needs_ack()` 为真，每条消息发送后都要等代理回一个 `OP_ACK` 帧才能继续（回顾 u2-l3 的 ACK 机制与 u4-l3 的刷新策略），相当于每次发送多一个往返；`QoS::No` 不等 ACK，可全速连发。代价是 `QoS::No` 不保证送达。

**练习 2**：`rpc.call + handle` 场景里，被调用的方法由谁处理？为什么它和 `rpc.call`（打 `.broker`）的结果会有差异？

> **答案**：由 CLI 进程自己挂的 `BenchmarkHandlers::handle_call` 处理（方法名 `benchmark.selftest`，原样回送载荷）。它和打 `.broker benchmark.test` 的差异在于：前者是「本机 RPC 处理器回环」，主要测 RPC 框架（编解码、processor 分发、oneshot 兑现）的开销；后者要经代理转发给 `.broker` 内置处理器再返回，多了一段代理内部路由。两者对比可分离出「代理转发」这一段的成本。

---

## 5. 综合实践

把本讲四个模块串起来，完成一次端到端的调试流程。**前提**：已用 `sh test.sh server` 启动代理（监听 `/tmp/busrt.sock`），见 [test.sh:8-11](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/test.sh#L8-L11)。

1. **开三个终端**：A（监听）、B（发送）、C（查询/压测）。
2. **终端 A**——订阅所有主题并常驻：
   ```sh
   sh test.sh cli listen -t '#'
   ```
   记下启动时打印的本机 `client_name`（形如 `cli.<host>.<pid>`）。
3. **终端 B**——发点对点 + 发布主题：
   ```sh
   sh test.sh cli send  "<A的client_name>"  "p2p hello"
   sh test.sh cli publish  demo/topic       '{"v":1}'
   ```
   观察 A 是否分别收到一条 `Message`（无 topic）和一条 `Publish`（`topic: demo/topic`，载荷被识别为 `JSON:`）。
4. **终端 C**——RPC 调用代理内置方法：
   ```sh
   sh test.sh cli rpc call .broker test
   sh test.sh cli rpc call .broker stats
   ```
   验证 `test` 返回 JSON、`stats` 返回 MessagePack（被 `print_payload` 转成 JSON 视图）。
5. **终端 C**——跑一次小规模基准并记录吞吐：
   ```sh
   sh test.sh cli benchmark -w 1 -i 5000 --payload-size 64
   ```
   记下 `send.qos.no`、`send.qos.processed`、`rpc.call` 三项的 ops/s。

**完成判定**：
- 你能说出 `send`、`publish`、`rpc call` 分别对应 `AsyncClient` / `Rpc` 的哪个方法；
- 你能解释 `broker stats` 其实是一次 `.broker` RPC；
- 你拿到了一组本机吞吐基线（**待本地验证**具体数值）。

> 进阶：把代理绑定换成 TCP（`-B 0.0.0.0:9924`），用 `busrt 127.0.0.1:9924 benchmark ...` 重跑基准，对比 Unix socket 与 TCP 的吞吐差距，体会传输层（u6-l2）对性能的影响。

## 6. 本讲小结

- `busrt` CLI 由 `cli` feature 门控（[Cargo.toml:81-83](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/Cargo.toml#L81-L83)），其 `[[bin]]` 入口为 `src/cli.rs`（[Cargo.toml:101-104](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/Cargo.toml#L101-L104)），本身不是服务端，而是连接代理的调试工具。
- 顶层 `Command` 枚举分六个子命令，`broker` / `rpc` 各自带一层嵌套子命令；全局 `Opts` 一比一映射到 `ipc::Config`（`create_client`）。
- `broker info/stats/test/client.list` 本质是向 `.broker` 发起的点对点 RPC，结果用 `rmp_serde` 反序列化后以 `prettytable` 打印。
- `send` 在目标含 `*`/`?` 时自动改走广播；`listen` 用「先 exclude 再 subscribe + ping 保活」维持接收循环；统一用 `QoS::Processed`。
- `print_payload` 按 **JSON → MessagePack → HEX** 三级自动解码载荷；RPC 参数用 `str_to_params_map` 把 `key=value` 解析成带类型的 map 再 MessagePack 序列化。
- `benchmark` 基于 `bma-benchmark`，覆盖 RPC（call/call0/call+handle）与消息（send/send+recv × no/processed）多场景，靠多 worker 并发压测吞吐。

## 7. 下一步学习建议

- **u8-l1（busrtd 服务端）**：本讲多次 `broker info/stats` 调用的那些内置方法，正是在 `server.rs` 经 `init_default_core_rpc()` 挂上去的；结合那一讲可看全「服务端如何提供、客户端如何消费」核心 RPC。
- **u8-l3（FIFO 与 announce）**：`busrtd` 默认还会 `spawn_fifo`，你可用 `echo` 往命名管道写命令触发消息——配合本讲的 `listen` 验证端到端通路。
- **u7-l2（cursors）**：本讲的 `rpc call` 只演示了「一次性」RPC；若需要流式拉取大数据，继续学游标层。
- **动手扩展**：仿照 `Handlers`（[src/cli.rs:307-348](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/cli.rs#L307-L348)）写一个自己的 `RpcHandlers`，挂到 `RpcClient::new` 上，把 CLI 变成一个临时 RPC 服务端，体会 u5-l2 的处理器分发。
