# busrtd：独立服务端二进制

## 1. 本讲目标

学完本讲，你应该能够：

- 说清 `busrtd` 这个独立二进制和嵌入式 `Broker` 的关系：它只是把库里的 `Broker` 包了一层「命令行参数 + 日志 + 信号 + 多传输监听」的外壳。
- 掌握 `busrtd` 的全部命令行参数（`-B/-P/-D/-w/-t` 等）以及它们如何映射到底层 `Broker` 与 `ServerConfig`。
- 理解 `busrtd` 的启动顺序：日志初始化 → daemonize → 建 tokio 运行时 → 写 pid 文件 → 挂信号处理 → 建 Broker 并初始化核心 RPC → 按路径类型分发监听。
- 理解信号处理与「优雅终止」：收到 SIGINT/SIGTERM 后如何广播 `BrokerEvent::shutdown()`、清理 pid/socket 文件、退出主循环。
- 理解多个 `-B` 绑定如何根据路径写法自动分发到 Unix socket / TCP / FIFO 三种通道。

本讲建立在 **u3-l1（创建 Broker 与注册内部客户端）** 与 **u6-l2（多传输层服务端）** 之上：u3-l1 讲了 `Broker::create` 与 `register_client`，u6-l2 讲了 `spawn_unix_server`/`spawn_tcp_server`/`spawn_fifo` 的传输细节。本讲则把这些「积木」组装成一个真实可运行的服务端进程。

## 2. 前置知识

- **`busrtd` 与 `busrt` 是两个不同的二进制**。`busrtd` 是服务端（daemon），常驻运行；`busrt` 是命令行客户端（CLI），用于调试。两者分别由 `Cargo.toml` 的 `[[bin]]` 段用 `required-features = ["server"]` 与 `["cli"]` 门控（见 u1-l2）。
- **`server` feature 是个「大礼包」**。它在 `Cargo.toml` 里定义为 `["dep:log", "dep:syslog", "dep:chrono", "dep:colored", "dep:clap", "dep:mimalloc", "dep:fork", "broker-rpc"]`，其中 `broker-rpc` 又传递带出 `broker` 与 `rpc`。所以启用 `server` 就自动拥有完整的代理核心与 RPC 能力，`src/server.rs` 才能同时用 `clap`、`fork`、`syslog`、`mimalloc` 并调用 `broker.init_default_core_rpc()`。
- **核心 RPC 客户端**：代理内部注册一个名为 `.broker` 的内部客户端，把「调用代理」退化为「向 `.broker` 发点对点 RPC」。`busrtd` 默认通过 `init_default_core_rpc()` 挂载内置处理器（`test/info/stats/client.list`），CLI 的 `broker info/stats` 本质就是调用这些方法（见 u5-l3）。
- **`announce` 事件**：代理通过向 `.broker/info`、`.broker/warn` 主题发布消息来通知客户端「有客户端上下线」「服务即将关停」。优雅终止依赖它。
- **FIFO（命名管道）通道**：一种特殊的「无连接」入口，让 shell 脚本直接用 `echo` 向总线发命令，依赖核心 RPC 客户端才能工作。

> 不熟悉的概念都会在后文结合源码展开。术语 `SECONDARY_SEP`、`QoS`、`Frame` 等在前置讲义中已建立，这里直接使用。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/server.rs](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/server.rs) | `busrtd` 二进制的全部代码：全局分配器、clap 参数、日志、daemonize、信号处理、main 启动流程、多传输绑定循环、优雅终止。 |
| [src/broker.rs](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs) | `busrtd` 调用的库 API：`Broker::create`、`init_default_core_rpc`、`set_queue_size`、`announce`、`spawn_unix_server`/`spawn_tcp_server`/`spawn_fifo`、`Options`、`ServerConfig`、`BrokerEvent`。 |
| [Cargo.toml](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/Cargo.toml) | `server` feature 定义与 `[[bin]] busrtd` 门控。 |
| [test.sh](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/test.sh) | 一键启动脚本，演示了一次绑定 unix+tcp+fifo 三种通道。 |

---

## 4. 核心概念与源码讲解

### 4.1 命令行参数与全局分配器：Opts（clap derive）

#### 4.1.1 概念说明

`busrtd` 是一个命令行程序，所有可调参数都来自命令行。它用 [`clap`](https://docs.rs/clap) 的 derive 风格定义参数：写一个普通 struct，加 `#[derive(Parser)]` 与 `#[clap(...)]` 属性，`clap` 就自动生成解析、`--help` 与错误提示。

在讲参数之前，先注意 `server.rs` 最顶部的两行——它们定义了**全局内存分配器**：

```rust
#[cfg(not(feature = "std-alloc"))]
#[global_allocator]
static ALLOC: mimalloc::MiMalloc = mimalloc::MiMalloc;
```

这是 [src/server.rs:1-3](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/server.rs#L1-L3) 的内容。`#[global_allocator]` 把整个进程的默认分配器换成 [mimalloc](https://github.com/microsoft/mimalloc)，它对多线程、小对象密集的 IPC 负载通常比系统默认分配器更快、更省内存。`#[cfg(not(feature = "std-alloc"))]` 表示：如果你显式启用 `std-alloc` feature，就回退到标准库分配器（用于需要对照基准或某些平台的特殊需求）。

#### 4.1.2 核心流程

`Opts` 结构体的字段就是 `busrtd` 的全部参数。`clap` 解析流程可概括为：

1. `Opts::parse()` 读取 `argv`。
2. 对每个字段，按 `#[clap(...)]` 里的 short/long 名匹配。
3. 缺少 `required` 参数或类型不匹配时，`clap` 自动打印错误并退出。
4. 解析成功后得到一个填好值的 `Opts`。

#### 4.1.3 源码精读

整个参数表定义在 [src/server.rs:65-110](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/server.rs#L65-L110)。下面是关键字段的含义对照：

| 参数 | 字段 | 默认值 | 作用 |
| --- | --- | --- | --- |
| `-B` / `--bind` | `path: Vec<String>` | （必填） | 监听地址，**可重复指定**；按写法自动选 unix/tcp/fifo |
| `-P` / `--pid-file` | `pid_file: Option<String>` | 无 | 把进程 PID 写入该文件，供运维/监控使用 |
| `--verbose` | `verbose: bool` | false | 开启 Trace 级别日志 |
| `-D` | `daemonize: bool` | false | 后台化（detach 进终端） |
| `--log-syslog` | `log_syslog: bool` | false | 强制把日志写到 syslog |
| `--force-register` | `force_register: bool` | false | 同名客户端冲突时顶替旧实例 |
| `-w` | `workers: usize` | 4 | tokio 工作线程数 |
| `-t` | `timeout: f64` | 5（秒） | 连接超时 |
| `--buf-size` | `buf_size: usize` | 16384 | 每客户端 I/O 缓冲大小 |
| `--buf-ttl` | `buf_ttl: u64` | 10（微秒） | 写缓冲刷新 TTL |
| `--queue-size` | `queue_size: usize` | 8192 | 每客户端帧队列大小 |

其中最关键的是 `-B`，它是 `required = true` 的 `Vec<String>`：

```rust
#[clap(
    short = 'B',
    long = "bind",
    required = true,
    help = "Unix socket path, IP:PORT or fifo:path, can be specified multiple times"
)]
path: Vec<String>,
```

这是 [src/server.rs:68-74](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/server.rs#L68-L74)。`Vec<String>` 让同一个参数可以出现多次，`clap` 会把所有值收集进一个数组。这就是 `busrtd` 能同时监听多个端点的根本机制。

`--force-register` 会被透传到 `Broker` 的 `Options::force_register(true)`，名字冲突时踢掉旧连接（详见 u3-l2）。

#### 4.1.4 代码实践

**实践目标**：不运行程序，仅通过 `--help` 验证 `clap` 生成的参数表与源码一致。

**操作步骤**：

1. 在项目根目录执行（待本地验证，需要已安装 Rust 工具链）：

   ```sh
   cargo run --release --features server --bin busrtd -- --help
   ```

2. 把 `--help` 输出里的每个选项，与上面的参数表逐条对照。

**需要观察的现象**：

- `-B, --bind <PATH>` 标注为「required」，且 help 文本里有「can be specified multiple times」。
- `-D` 没有 long 名（它是 `#[clap(short = 'D')]` 单字符开关）。
- `--buf-ttl` 的默认值显示为 `10`。

**预期结果**：`clap` 自动生成的 help 与源码里 `#[clap(...)]` 属性完全对应，说明参数定义没有「隐藏参数」。

> 说明：`--help` 是 `clap` 自动提供的，源码里并没有手写 help 文本生成逻辑。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `path` 字段是 `Vec<String>` 而不是 `String`？如果改成 `String` 会怎样？

> **答案**：因为 `busrtd` 要支持同时绑定多个端点（unix+tcp+fifo）。`Vec<String>` 配合 `clap` 让 `-B` 可重复出现。改成 `String` 后，`-B` 只能出现一次，无法多绑定，且 `clap` 会在第二次出现时报错。

**练习 2**：如果不想用 mimalloc，怎样让 `busrtd` 回退到系统分配器？

> **答案**：编译时启用 `std-alloc` feature（`--features server,std-alloc`）。此时 `#[cfg(not(feature = "std-alloc"))]` 为假，全局分配器那两行不编译，进程使用标准库默认分配器。

---

### 4.2 启动阶段：日志、daemonize 与 tokio 运行时

#### 4.2.1 概念说明

`busrtd` 的 `main` 函数不是异步的（Rust 的 `main` 不能直接 `async`），但它需要驱动大量异步任务。解决方法是：`main` 先做同步初始化（解析参数、配日志、可选后台化），然后手动构建一个 tokio 多线程运行时，用 `rt.block_on(async { ... })` 进入异步世界。

日志有三条路径：
- **控制台彩色日志**（`SimpleLogger`）：默认前台运行时使用，用 `colored` 给不同级别上色。
- **syslog**：后台化（`-D`）且未禁用时，日志走系统日志服务，不占终端。
- **Trace 全量日志**：`--verbose` 时开启。

「daemonize」是把进程从控制终端脱离、转入后台运行（类似 `nohup` 但更彻底，经 `fork` 实现真正的守护进程）。

#### 4.2.2 核心流程

`main` 的启动阶段流程（伪代码）：

```
parse Opts
if verbose                  → 控制台 Trace 日志
elif (前台 or DISABLE_SYSLOG=1) and not log_syslog
                            → 控制台 Info 日志
else                        → syslog（失败则回退控制台 Info）
打印启动参数
if daemonize                → fork::daemon(true, false)，父进程 exit(0)
构建多线程 tokio 运行时（worker_threads = opts.workers）
block_on:
    写 pid 文件（若有 -P）
    挂载 SIGINT/SIGTERM 处理
    创建 Broker + 初始化核心 RPC + 设置队列大小
    循环绑定每个 -B 路径
    进入主循环，定期检查 SERVER_ACTIVE 标志
```

#### 4.2.3 源码精读

**自定义控制台日志器** `SimpleLogger` 实现了 `log::Log` trait，把每条日志格式化为「RFC3339 时间 + 内容」，并按级别上色：

[src/server.rs:29-55](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/server.rs#L29-L55) — `SimpleLogger::log` 用 `chrono` 的 `Local::now().to_rfc3339_opts(...)` 生成时间戳，用 `colored` 的 `.yellow().bold()` / `.red()` 等给级别染色。

`set_verbose_logger` 注册它：

[src/server.rs:59-63](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/server.rs#L59-L63) — `log::set_logger(&LOGGER).map(|()| log::set_max_level(filter))` 一步完成「装日志器」和「设最大级别」。

**三选一日志策略**在 `main` 开头：

[src/server.rs:168-193](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/server.rs#L168-L193) — 这段是关键：`--verbose` 走 Trace；否则若「非后台化 **或** 环境变量 `DISABLE_SYSLOG=1`」且未强制 syslog，则走控制台 Info；最后才走 syslog。syslog 分支用 `syslog::Formatter3164` 构造标准 syslog 报文，进程名写死为 `"busrtd"`；若 `syslog::unix(...)` 失败（比如无 syslog socket），回退到控制台 Info，保证不会因为日志初始化失败而崩。

**daemonize**：

```rust
if opts.daemonize {
    if let Ok(fork::Fork::Child) = fork::daemon(true, false) {
        std::process::exit(0);
    }
}
```

这是 [src/server.rs:202-206](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/server.rs#L202-L206)。`fork::daemon(true, false)` 第一个 `true` 表示「保留 stdout/stderr」（让早期日志仍能输出），返回 `Child` 时说明当前是 fork 出的子进程；这里让**父进程** `exit(0)`，子进程继续往后跑，从而脱离终端。

**构建 tokio 运行时**：

```rust
let rt = tokio::runtime::Builder::new_multi_thread()
    .worker_threads(opts.workers)
    .enable_all()
    .build()
    .unwrap();
rt.block_on(async move { /* 异步主体 */ });
```

这是 [src/server.rs:207-211](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/server.rs#L207-L211)。`new_multi_thread()` 创建多线程运行时，`.worker_threads(opts.workers)` 让 `-w` 直接控制 worker 数量，`.enable_all()` 打开 IO/时间等全部驱动。之后所有异步逻辑都在 `block_on` 的闭包里。

#### 4.2.4 代码实践

**实践目标**：观察三种日志模式的实际差异。

**操作步骤**：

1. 前台 + 默认日志（控制台 Info）：

   ```sh
   cargo run --release --features server --bin busrtd -- -B /tmp/busrt.sock
   ```

2. 前台 + verbose（控制台 Trace）：

   ```sh
   cargo run --release --features server --bin busrtd -- -B /tmp/busrt.sock --verbose
   ```

3. 前台 + 强制 syslog（应回退到控制台，因为多数环境无 syslog 或会落到系统日志）：

   ```sh
   DISABLE_SYSLOG=0 cargo run --release --features server --bin busrtd -- -B /tmp/busrt.sock -D --log-syslog
   ```

**需要观察的现象**：

- 模式 1：看到彩色的 `starting BUS/RT server`、`binding at /tmp/busrt.sock`、`BUS/RT broker started`（Info 级别）。
- 模式 2：额外出现 Trace 级别（暗色）的细节，如信号处理启动、握手过程等。
- 模式 3：若环境无 syslog，行为回退到控制台 Info。

**预期结果**：Trace 模式信息最全，普通模式只打印关键生命周期事件。**待本地验证**：syslog 是否真的写入 `/var/log/syslog` 取决于目标机器的 syslog 服务配置。

#### 4.2.5 小练习与答案

**练习 1**：为什么日志初始化要放在 `daemonize` **之前**？

> **答案**：daemonize 会让父进程 `exit(0)`、子进程脱离终端继续运行。若先 daemonize 再配日志，父进程退出前没有任何日志，启动失败也无声无息；且子进程需要日志器已就绪才能记录后续步骤。先配日志保证「fork 之后子进程立刻能记录」。

**练习 2**：`fork::daemon(true, false)` 第二个参数 `false` 是什么意思？

> **答案**：第二个参数控制是否 `chdir("/")`（改变工作目录到根）。`false` 表示**不**改变工作目录，进程保持当前目录；第一个 `true` 表示保留标准输出。这样相对路径（如 pid 文件、socket 路径）仍按启动时的目录解析。

---

### 4.3 多传输绑定：按路径类型自动分发 unix/tcp/fifo

#### 4.3.1 概念说明

`busrtd` 最有特色的设计是：**一个 `-B` 参数，三种通道**。它不让你显式声明「我要 unix socket」还是「我要 tcp」，而是根据路径字符串的写法自动判断：

- `fifo:` 前缀 → FIFO 命名管道
- 以 `/` 开头，或以 `.sock`/`.socket`/`.ipc` 结尾 → Unix socket
- 其它（`host:port` 形式）→ TCP

这套判断逻辑全部在 `main` 的绑定循环里。对应的三个库方法是 `spawn_fifo`、`spawn_unix_server`、`spawn_tcp_server`（WebSocket 服务端只能嵌入使用，`busrtd` 不暴露，见 u6-l2）。

#### 4.3.2 核心流程

绑定循环（伪代码）：

```
创建 ServerConfig（buf_size / buf_ttl / timeout）
for path in opts.path:
    if path 以 "fifo:" 开头:
        broker.spawn_fifo(fifo_path, buf_size)   # 依赖核心 RPC 已初始化
    elif path 以 .sock/.socket/.ipc 结尾 或 以 / 开头:
        broker.spawn_unix_server(path, server_config)
    else:
        broker.spawn_tcp_server(path, server_config)
```

注意 FIFO 分支只传 `buf_size`（FIFO 是按行读的，不需要 `buf_ttl`/`timeout`）；而 unix/tcp 共享同一个 `ServerConfig`。

#### 4.3.3 源码精读

先看创建 Broker 与初始化核心 RPC 的几行，它们在绑定循环之前，是 FIFO 能工作的前提：

[src/server.rs:223-226](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/server.rs#L223-L226) — `Broker::create(&Options::default().force_register(opts.force_register))` 用命令行 `--force-register` 构造代理；紧接着 `broker.init_default_core_rpc().await.unwrap()` 挂载 `.broker` 内置 RPC 处理器；`broker.set_queue_size(opts.queue_size)` 把 `--queue-size` 透传进去。

`init_default_core_rpc` 的实现：

[src/broker.rs:1366-1374](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1366-L1374) — 注册名为 `.broker`（`BROKER_NAME`）的内部客户端，挂上内置 `BrokerRpcHandlers`，包成 `RpcClient`（构造即自动 spawn processor），再 `set_core_rpc_client` 存进 `BrokerDb`。

**绑定循环本体**在 [src/server.rs:228-262](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/server.rs#L228-L262)，核心是三段判断：

FIFO 分支：

```rust
if let Some(_fifo) = path.strip_prefix("fifo:") {
    broker.spawn_fifo(_fifo, opts.buf_size).await
        .expect("unable to start fifo server");
    sock_files.push(_fifo.to_owned());
}
```

这是 [src/server.rs:231-239](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/server.rs#L231-L239)。注意它整体在 `#[cfg(feature = "rpc")]` 守卫内——`spawn_fifo` 实现依赖核心 RPC 客户端。

Unix/TCP 分支共用 `ServerConfig`：

```rust
let server_config = ServerConfig::new()
    .buf_size(opts.buf_size)
    .buf_ttl(buf_ttl)
    .timeout(timeout);
if path.ends_with(".sock") || path.ends_with(".socket")
    || path.ends_with(".ipc") || path.starts_with('/')
{
    broker.spawn_unix_server(&path, server_config).await...
    sock_files.push(path);
} else {
    broker.spawn_tcp_server(&path, server_config).await...
}
```

这是 [src/server.rs:241-260](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/server.rs#L241-L260)。`ServerConfig` 用建造者模式链式设置三项（`ServerConfig::new` 默认值见 [src/broker.rs:855-865](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L855-L865)）。Unix socket 路径会被记录进 `sock_files`（退出时清理），TCP 则不记录（端口不存在「文件」需要删）。

**底层的三个 spawn 方法**（详见 u6-l2）：
- `spawn_unix_server` [src/broker.rs:1445-1462](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1445-L1462)：先删旧 socket 文件，`UnixListener::bind`，再用 `spawn_server!` 宏接 accept 循环，客户端类型标记为 `BusRtClientKind::LocalIpc`。
- `spawn_tcp_server` [src/broker.rs:1463-1479](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1463-L1479)：`TcpListener::bind`，客户端类型 `BusRtClientKind::Tcp`。
- `spawn_fifo` [src/broker.rs:1625-1660](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1625-L1660)：**先检查核心 RPC 客户端是否已初始化**，否则返回 `not_supported`；然后用 `unix_named_pipe::create` 建管道、设权限 `0o622`，开一个后台任务用 `BufReader::lines()` 逐行读命令，每行调 `send_fifo_cmd` 经核心 RPC 客户端发出。

**FIFO 命令语法**由 `send_fifo_cmd` 解析（[src/broker.rs:1662-1731](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1662-L1731)）：

| 写法 | 含义 |
| --- | --- |
| `=topic payload` | 向主题 `topic` 发布消息（`publish`） |
| `target .method` | 向 `target` 发 RPC 通知（notification） |
| `target :method k=v k=v` | 向 `target` 发 RPC 调用（call0，参数为 msgpack map） |
| `target payload`（target 含 `*`/`?`） | 广播（`send_broadcast`） |
| `target payload`（普通） | 点对点消息（`send`） |

> 注意：普通消息的 payload 只取一个空白分隔的 token（解析时只 `sp.next()` 一次），所以 payload 不能含空格。

#### 4.3.4 代码实践

**实践目标**：用 `test.sh` 的现成命令一次绑定三种通道，验证路径自动分发。

**操作步骤**：

1. 用项目自带的 `test.sh server`（它已经预置了三绑定）启动：

   ```sh
   ./test.sh server
   ```

   等价于：

   ```sh
   cargo run --release --features server,rpc --bin busrtd -- \
     -B /tmp/busrt.sock -B 0.0.0.0:9924 -B fifo:/tmp/busrt.fifo
   ```

   参见 [test.sh:7-11](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/test.sh#L7-L11)。

2. 观察启动日志，应出现三行 `binding at ...`：
   - `/tmp/busrt.sock` → 走 unix 分支（以 `/` 开头）。
   - `0.0.0.0:9924` → 走 tcp 分支。
   - `fifo:/tmp/busrt.fifo` → 走 fifo 分支。

**需要观察的现象**：三条绑定都成功，无 `unable to start ...` 报错；`/tmp/busrt.sock` 与 `/tmp/busrt.fifo` 两个文件被创建。

**预期结果**：`busrtd` 同时监听三种通道。**待本地验证**：若 `/tmp/busrt.sock` 已存在且被占用，`spawn_unix_server` 会先 `remove_file` 再 bind（见源码 L1450），所以重复启动一般不会因残留 socket 失败。

#### 4.3.5 小练习与答案

**练习 1**：路径 `/var/run/busrt.ipc` 会被判为哪种通道？依据是什么？

> **答案**：Unix socket。因为它以 `.ipc` 结尾，命中判断条件 `path.ends_with(".ipc")`（即便它也以 `/` 开头，两个条件只要满足一个即可）。

**练习 2**：为什么 `spawn_fifo` 在核心 RPC 未初始化时直接返回错误，而 `spawn_unix_server`/`spawn_tcp_server` 不需要这个检查？

> **答案**：FIFO 命令是经核心 RPC 客户端发出的（`send_fifo_cmd` 调 `rpc.notify`/`rpc.call0`/`rpc.client().publish` 等），没有核心 RPC 客户端就无法解析执行任何命令，所以前置检查。Unix/TCP 是真正的传输层监听，连接进来后走完整的握手与帧处理，不依赖核心 RPC 客户端。`busrtd` 因为总在绑定前调 `init_default_core_rpc()`，FIFO 检查必然通过。

---

### 4.4 信号处理与优雅终止：terminate 与 handle_term_signal

#### 4.4.1 概念说明

服务端进程必须能「干净地退出」：收到终止信号时，先通知所有客户端「我要关停了」，再删除自己创建的 pid/socket 文件，最后退出。否则客户端不知道总线已消失，残留的 socket 文件会阻塞下次启动。

`busrtd` 用一个全局原子标志 `SERVER_ACTIVE` 协调各部分：信号处理任务收到信号后调用 `terminate()`，`terminate()` 把标志置 `false`，主循环检测到就 `break` 退出 `block_on`，进程结束。

优雅终止还调用 `broker.announce(BrokerEvent::shutdown())`，向 `.broker/warn` 主题发布一条「shutdown」消息——所有客户端注册时都自动订阅了 `.broker/warn`（见 u3-l2），所以**无需任何额外配置**，客户端都能收到关停通知。

#### 4.4.2 核心流程

```
全局变量:
  SERVER_ACTIVE : AtomicBool = true   # 主循环的存活标志
  PID_FILE      : Mutex<Option<String>>
  SOCK_FILES    : Mutex<Vec<String>>
  BROKER        : Mutex<Option<Broker>>

启动时:
  spawn 两个信号任务: SIGINT (Ctrl-C) / SIGTERM
  主循环每 100ms 检查 SERVER_ACTIVE，false 则 break

收到信号:
  handle_term_signal 任务 → 调 terminate(allow_log)
  terminate:
    删 PID_FILE（若有）
    删 SOCK_FILES 中每个文件
    broker.announce(BrokerEvent::shutdown())  # 通知 .broker/warn
    SERVER_ACTIVE = false
    sleep(1s)  # 给 announce 一点传播时间
```

#### 4.4.3 源码精读

**全局状态**定义在文件上部：

[src/server.rs:21-25](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/server.rs#L21-L25) — 四个 `LazyLock` 全局：`SERVER_ACTIVE` 是 `AtomicBool`（无锁，主循环高频读），其余三个是 `tokio::sync::Mutex` 包裹的可变状态。`BROKER` 存代理实例供 `terminate` 调 `announce`。

**`terminate` 函数**是优雅终止的核心：

[src/server.rs:112-138](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/server.rs#L112-L138) — 顺序为：① 删 pid 文件；② 循环删所有 socket 文件；③ `info!("terminating")`；④ **若启用了 rpc feature**，从 `BROKER` 取出代理，调 `broker.announce(BrokerEvent::shutdown())`，失败只 `error!` 不阻断；⑤ `SERVER_ACTIVE.store(false, ...)`；⑥ 再 `sleep(1s)` 给 announce 消息一点在网络上传播的时间。

`BrokerEvent::shutdown()` 的构造：

[src/broker.rs:623-630](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L623-L630) — 固定 `s = "shutdown"`、`topic = BROKER_WARN_TOPIC`（即 `.broker/warn`）。

`announce` 最终落到 `BrokerDb::announce`：

[src/broker.rs:709-724](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L709-L724) — 给事件补上纳秒时间戳 `event.t = now_ns()`，把事件 msgpack 序列化后 `publish` 到 `event.topic`（`.broker/warn`），用 `QoS::No`（关停通知不需要 ACK，尽快发出）。若核心 RPC 客户端为空则静默返回。

**`handle_term_signal!` 宏**为每种信号 spawn 一个独立的监听任务：

[src/server.rs:140-162](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/server.rs#L140-L162) — 用 `tokio::signal::unix::signal(SignalKind)` 绑定信号，`v.recv().await` 阻塞等待，收到后调 `terminate($allow_log)`。宏的 `$allow_log` 参数控制是否打印 trace 日志——见下文。

**信号挂载点**在 `block_on` 内：

```rust
handle_term_signal!(SignalKind::interrupt(), false);   // SIGINT / Ctrl-C，静默
handle_term_signal!(SignalKind::terminate(), true);    // SIGTERM，打印日志
```

这是 [src/server.rs:221-222](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/server.rs#L221-L222)。注意两个 `allow_log` 值不同：**Ctrl-C（SIGINT）静默**（避免用户狂按 Ctrl-C 时刷屏），**SIGTERM 打印日志**（这是 systemd/kill 默认发的信号，留下审计痕迹）。

**主循环**在最末：

[src/server.rs:264-272](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/server.rs#L264-L272) — 先把 `broker` 存进全局 `BROKER`（供 `terminate` 使用），然后 `loop` 每 100ms 检查 `SERVER_ACTIVE`，一旦为 `false` 就 `break`，`block_on` 返回，运行时 drop，进程退出。

#### 4.4.4 代码实践

**实践目标**：亲手触发优雅终止，观察文件清理与 shutdown 通知。

**操作步骤**：

1. 启动带 pid 文件与三绑定的服务（新开终端 A）：

   ```sh
   ./test.sh server -P /tmp/busrt.pid
   ```

2. 终端 B 连一个监听客户端，订阅 `.broker/warn`（它本就自动订阅，这里显式 listen 便于看到关停通知）：

   ```sh
   cargo run --release --bin busrt --features cli -- /tmp/busrt.sock listen '.broker/#'
   ```

3. 终端 C 检查文件存在：

   ```sh
   ls -l /tmp/busrt.pid /tmp/busrt.sock /tmp/busrt.fifo
   ```

4. 回到终端 A 按 `Ctrl-C`（SIGINT）或另开终端 `kill $(cat /tmp/busrt.pid)`（SIGTERM）。

**需要观察的现象**：

- 终端 B 的监听客户端收到一条来自 `.broker/warn` 的 shutdown 消息（msgpack 序列化，含 `s="shutdown"` 与时间戳 `t`）。
- 终端 A 退出（SIGTERM 会打印 `terminating`；Ctrl-C 静默）。
- 终端 C 再 `ls`，`/tmp/busrt.pid`、`/tmp/busrt.sock` 都已被删除。

**预期结果**：优雅终止流程完整生效——通知 → 清理文件 → 退出。**待本地验证**：终端 B 能否在 1 秒 `sleep` 窗口内收到 shutdown，取决于网络与本机时序；若没收到，可把 `terminate` 里的 `sleep(Duration::from_secs(1))` 视为「尽力而为」的传播窗口。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `SERVER_ACTIVE` 用 `AtomicBool`，而 `PID_FILE`/`SOCK_FILES`/`BROKER` 用 `tokio::Mutex`？

> **答案**：`SERVER_ACTIVE` 只被主循环高频读、被 `terminate` 偶尔写，用原子量无锁、零等待，适合「热路径标志位」。另外三个是复合状态（Option/Vec/Broker），读写需要跨多个操作保持一致，必须用互斥锁串行化访问，故用 `Mutex`。

**练习 2**：如果删掉 `terminate` 里的 `broker.announce(BrokerEvent::shutdown())`，客户端会怎样？

> **答案**：客户端不会收到任何关停通知，只有等它自己下次读 socket 时发现连接断开（EOF）才「事后」感知。对于长连接空闲客户端，可能很久都不知道总线已关停。announce 让客户端能「主动、即时」地收到关停事件并做收尾。

**练习 3**：`terminate` 里最后的 `sleep(Duration::from_secs(1))` 是必须的吗？去掉会怎样？

> **答案**：不是严格必须，但去掉有风险。`announce` 是异步 publish，消息从发出到真正写到所有客户端 socket 需要一点时间；若 `terminate` 立即返回、主循环立刻 `break`、进程退出，部分客户端可能还没读到 shutdown 帧连接就断了。这 1 秒是一个「传播保险窗口」，牺牲 1 秒退出时间换取通知可达性。

---

## 5. 综合实践

把本讲四个模块串起来，完成一次「完整的服务端生命周期」演练。

**任务**：启动一个同时监听三种通道的 `busrtd`，用 FIFO 触发一条消息，再用 CLI 观察客户端列表与统计，最后优雅关停。

**步骤**：

1. **启动服务端**（终端 A，带 pid 文件、队列大小、三绑定）：

   ```sh
   cargo run --release --features server,rpc --bin busrtd -- \
     -B /tmp/busrt.sock -B 0.0.0.0:9924 -B fifo:/tmp/busrt.fifo \
     -P /tmp/busrt.pid --queue-size 4096
   ```

   观察日志确认三个 `binding at ...` 都成功。

2. **起一个监听客户端**（终端 B，名为 `listener`，订阅所有主题）：

   ```sh
   cargo run --release --bin busrt --features cli -- /tmp/busrt.sock -n listener listen '#'
   ```

3. **用 FIFO 触发一条消息**（终端 C，向 fifo 发布主题消息）：

   ```sh
   echo '=news/hello world' > /tmp/busrt.fifo
   ```

   预期：终端 B 的 listener 收到一条 topic 为 `news/hello`、payload 为 `world` 的消息（验证 FIFO 的 `=topic payload` 语法经核心 RPC publish 生效）。

4. **用 CLI 观察客户端列表与统计**（终端 C）：

   ```sh
   cargo run --release --bin busrt --features cli -- /tmp/busrt.sock broker client.list
   cargo run --release --bin busrt --features cli -- /tmp/busrt.sock broker stats
   ```

   预期：`client.list` 列出至少 `.broker` 与 `listener` 两个主客户端；`stats` 显示已收发的帧/字节计数（这两个命令本质是对 `.broker` 的 `client.list`/`stats` RPC 调用，见 [src/cli.rs:626](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/cli.rs#L626) 与 [src/cli.rs:655](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/cli.rs#L655)）。

5. **优雅关停**（终端 C）：

   ```sh
   kill -TERM $(cat /tmp/busrt.pid)
   ```

   预期：终端 B 收到 `.broker/warn` 的 shutdown 通知；终端 A 打印 `terminating` 后退出；`/tmp/busrt.pid`、`/tmp/busrt.sock` 被清理。

**验收标准**：能解释每一步对应的源码位置——绑定分发（4.3）、FIFO 命令解析（`send_fifo_cmd`）、`broker client.list/stats` 的 RPC 本质（4.1 + u5-l3）、shutdown 通知与文件清理（4.4）。

> 本实践依赖 `busrt` CLI 的具体子命令语法，详见下一讲 u8-l2。若 `listen '#'` 的引号转义在你的 shell 有问题，可改用 `listen ".broker/#"` 先验证订阅机制。

---

## 6. 本讲小结

- `busrtd` 是一个薄外壳：把库里的 `Broker` 配上 clap 参数、日志、daemonize、信号处理与多传输监听，编译为独立服务端二进制，由 `server` feature 门控。
- 参数全部集中在 `Opts`（[src/server.rs:65-110](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/server.rs#L65-L110)），其中 `-B` 可重复，是「多绑定」的根；`--force-register`、`--queue-size` 等透传给底层 `Broker`/`ServerConfig`。
- 启动顺序固定：日志（控制台/syslog/Trace 三选一）→ 可选 daemonize → 建多线程 tokio 运行时 → 写 pid 文件 → 挂 SIGINT/SIGTERM → `Broker::create` + `init_default_core_rpc` + `set_queue_size` → 绑定循环 → 主循环。
- **路径自动分发**是核心特色：`fifo:` 前缀走 `spawn_fifo`，`/` 开头或 `.sock`/`.socket`/`.ipc` 结尾走 `spawn_unix_server`，其余走 `spawn_tcp_server`；FIFO 依赖核心 RPC 客户端已初始化。
- **优雅终止**靠全局 `SERVER_ACTIVE` 标志协调：信号任务调 `terminate()`，它删 pid/socket 文件、`announce(BrokerEvent::shutdown())` 通知 `.broker/warn`、置标志为 false；主循环检测到后退出；SIGINT 静默、SIGTERM 留日志。
- mimalloc 全局分配器（可被 `std-alloc` feature 关闭）为高频小对象 IPC 负载优化内存分配性能。

## 7. 下一步学习建议

- **u8-l2（busrt CLI）**：本讲综合实践大量使用了 `busrt` CLI 的 `listen`/`broker client.list`/`broker stats` 子命令，下一讲会完整精读 `cli.rs`，讲清每个子命令的参数与载荷编解码。
- **u8-l3（FIFO 与 announce）**：本讲只触及 FIFO 命令语法与 shutdown 通知，u8-l3 会深入 `send_fifo_cmd` 的全部命令分支、`spawn_fifo` 的权限与读写模型，以及 `BrokerEvent` 的 `reg`/`unreg`/`shutdown` 三类事件在 `.broker/info`、`.broker/warn` 上的完整发布机制。
- **延伸阅读**：若想理解「为什么 FIFO 必须先 `init_default_core_rpc`」，可重读 [src/broker.rs:1366-1374](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1366-L1374) 与 `BrokerRpcHandlers`（u5-l3）；若对传输层握手细节感兴趣，可结合 u6-l1（连接生命周期）回看 `spawn_unix_server`/`spawn_tcp_server` 背后的 `handle_peer`。
