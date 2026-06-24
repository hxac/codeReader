# 从零构建与运行：busrtd 服务端与 busrt CLI

## 1. 本讲目标

上一篇（u1-l1）我们已经知道了 BUS/RT 是一个 Rust/Tokio 编写的 IPC 消息代理，可以用 feature 控制编译出不同能力的库或可执行程序。本讲的目标是把项目「真正跑起来」：

- 学会用 Cargo 命令编译出 BUS/RT 的**两个二进制**：常驻服务端 `busrtd` 和命令行客户端 `busrt`。
- 理解 `Cargo.toml` 里的 `[[bin]]` 段与 `required-features` 是如何做到「只有打开某个 feature 才会编译某个二进制」的。
- 会用仓库自带的 `test.sh` 和 `justfile` 一键启动服务端与客户端，而不必手敲一长串参数。
- 用 `busrt` CLI 连上运行中的 `busrtd`，执行 `broker info` 和 `broker stats`，看清楚客户端与代理之间是如何对话的。

学完本讲，你就能在自己的机器上把 BUS/RT 跑起来，并用官方 CLI 调试它，为后续阅读协议、Broker 内部等更深的源码打下基础。

## 2. 前置知识

在动手之前，先通俗地解释几个关键概念。

### 2.1 二进制（binary）与库（library）

一个 Rust 项目可以同时产出两种东西：

- **库（lib）**：供别的 Rust 程序 `use` 调用，本身不能直接运行。BUS/RT 的库定义在 [Cargo.toml:92-94](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/Cargo.toml#L92-L94)，名为 `busrt`。
- **二进制（bin）**：可以直接执行的程序。BUS/RT 提供了两个二进制：`busrtd`（服务端）和 `busrt`（CLI 客户端）。

### 2.2 Cargo feature（特性开关）

Cargo 的 feature 是「可选功能开关」。打开不同 feature，就会编译进不同的模块和依赖。上一篇已经讲过 `rpc`、`broker`、`ipc` 等 feature 的作用。本讲要特别关注两点：

- feature 之间可以**传递依赖**，比如 `server` 这个 feature 会自动把 `broker-rpc` 带进来，而 `broker-rpc` 又会带进 `rpc`。
- 二进制可以用 `required-features` 声明「只有这些 feature 打开时，才编译我」。

### 2.3 服务端与客户端的关系

`busrtd` 是常驻后台的「代理（broker）」，负责接收所有客户端连接并转发消息；`busrt` CLI 是一个临时连接的客户端，连上代理、发一条命令、拿到结果就退出。本讲你要做的，就是「先把代理 `busrtd` 跑起来，再用 `busrt` CLI 去问它几个问题」。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [Cargo.toml](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/Cargo.toml) | 声明两个 `[[bin]]`、feature 开关与依赖，是理解「编译什么」的总开关。 |
| [test.sh](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/test.sh) | 仓库自带的一键启动脚本，分 `server` 和 `cli` 两个分支。 |
| [justfile](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/justfile) | 用 [just](https://github.com/casey/just) 命令器定义的 `test` 目标，做静态检查（clippy）。 |
| [src/server.rs](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/server.rs) | `busrtd` 服务端的全部源码：解析命令行、建代理、按路径类型监听。 |
| [src/cli.rs](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/cli.rs) | `busrt` CLI 客户端的全部源码：连接代理、派发子命令、打印结果。 |

## 4. 核心概念与源码讲解

### 4.1 两个二进制：busrtd 与 busrt 的 feature 门控

#### 4.1.1 概念说明

BUS/RT 并不是一个二进制「包打天下」，而是把服务端和客户端拆成两个独立的可执行程序。这样做的好处是：

- 你可以只在一台机器上部署轻量的 `busrtd` 服务端，不需要把 CLI、基准测试等代码编译进去。
- `busrt` CLI 是纯客户端，体积更小、依赖更少，适合当作调试工具随身携带。

Cargo 用 `[[bin]]` 段来声明一个二进制，每段指明：二进制名字、入口源码路径、以及「必须打开哪些 feature 才编译它」（`required-features`）。当 `required-features` 里写的 feature 没有被启用时，Cargo 根本不会编译这个二进制——这就是所谓的「feature 门控（gating）」。

#### 4.1.2 核心流程

编译并运行某个二进制的判流程可以概括为：

1. 你在命令行用 `--features <X>` 启用某些 feature。
2. Cargo 检查每个 `[[bin]]` 的 `required-features` 是否都被满足。
3. 只有被满足的二进制才会被编译；`--bin <名字>` 指定运行哪一个。

对 BUS/RT 而言：

- 要跑 `busrtd`，必须启用 `server` feature。
- 要跑 `busrt`，必须启用 `cli` feature。

#### 4.1.3 源码精读

两个二进制的声明在 [Cargo.toml:96-104](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/Cargo.toml#L96-L104)：

```toml
[[bin]]
name = "busrtd"
path = "src/server.rs"
required-features = ["server"]

[[bin]]
name = "busrt"
path = "src/cli.rs"
required-features = ["cli"]
```

可以看到：

- `busrtd` 的入口是 `src/server.rs`，没有 `server` feature 就编译不出来。
- `busrt` 的入口是 `src/cli.rs`，没有 `cli` feature 就编译不出来。

这两个 feature 各自又拉入了哪些依赖？见 [Cargo.toml:66-83](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/Cargo.toml#L66-L83)：

```toml
server = ["dep:log", "dep:syslog", "dep:chrono", "dep:colored",
  "dep:clap", "dep:mimalloc", "dep:fork", "broker-rpc"]
# ...
broker-rpc = ["broker", "rpc", "dep:rmp-serde"]
# ...
cli = ["ipc", "rpc", "dep:colored", "dep:clap", "dep:env_logger",
  "dep:bma-benchmark", "dep:prettytable-rs", "dep:hostname", "dep:hex",
  "dep:num-format", "dep:mimalloc", "dep:serde_json", "dep:is-terminal", "dep:rmp-serde"]
```

理解要点：

- `server` 通过 `broker-rpc` **传递依赖**了 `broker` 和 `rpc`。这意味着只要启用 `server`，代理的 RPC 能力（包括本讲后面会用到的 `.broker` 内置方法）和 fifo 通道都会被一并编译进来。这也是 `src/server.rs` 里大量 `#[cfg(feature = "rpc")]` 代码块在 `server` 下都会生效的原因。
- `cli` 通过 `ipc` 和 `rpc` 拉入客户端连接与 RPC 调用所需的栈，再加 `serde_json`/`rmp-serde` 用来解码返回的载荷、`prettytable-rs`/`colored` 用来美化输出。

#### 4.1.4 代码实践

1. **实践目标**：直观感受 feature 门控——不启用对应 feature 时，二进制根本不存在。
2. **操作步骤**：
   - 在项目根目录运行 `cargo build --release`（不带任何 feature）。注意 [Cargo.toml:90](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/Cargo.toml#L90) 的 `#default = ["full"]` 是被注释掉的，所以默认没有任何 feature。
   - 再运行 `cargo build --release --features server --bin busrtd`。
3. **需要观察的现象**：第一次只编译出库；第二次才会编译出 `target/release/busrtd`。
4. **预期结果**：`target/release/` 目录下第一次没有 `busrtd`/`busrt` 可执行文件，第二次出现 `busrtd`。具体产物列表「待本地验证」。
5. 这个实践不涉及运行，只是验证「门控」生效。

#### 4.1.5 小练习与答案

**练习 1**：如果不加 `--features server`，直接 `cargo run --bin busrtd` 会发生什么？

**答案**：Cargo 会报错，提示 `busrtd` 的 `required-features = ["server"]` 没有被满足，因为默认没有任何 feature 被启用。

**练习 2**：为什么 `server` feature 没有显式列出 `rpc`，但 `busrtd` 依然能用 RPC？

**答案**：因为 `server` 传递依赖了 `broker-rpc`，而 `broker-rpc = ["broker", "rpc", ...]` 又传递依赖了 `rpc`。所以启用 `server` 时 `rpc` 自动可用。

### 4.2 编译与一键启动：test.sh 与 justfile

#### 4.2.1 概念说明

虽然直接敲 `cargo run --release --features ... --bin ... -- <参数>` 也能跑，但参数太长、容易出错。仓库提供了两个辅助脚本帮你省事：

- **`test.sh`**：一个朴素的 POSIX shell 脚本，把「启动服务端」和「启动客户端」封装成 `server` 和 `cli` 两个子命令，并预置了一组常用的监听地址。
- **`justfile`**：配合 [just](https://github.com/casey/just) 命令器使用，主要定义了一个 `test` 目标，对各种 feature 组合跑 `clippy` 静态检查（CI 里用的就是它）。

> 名字叫 `test.sh`，但它**并不运行单元测试**，而是「一键拉起服务端/客户端做手工联调」。这一点初学者容易误解。

#### 4.2.2 核心流程

`test.sh` 的逻辑非常简单：取第一个参数作为子命令，剩下的参数原样透传。

```text
test.sh <server|cli> [额外参数...]
  ├─ server → cargo run --bin busrtd --features server,rpc -- <预置 -B 绑定> [额外参数]
  └─ cli    → cargo run --bin busrt  --features cli         -- /tmp/busrt.sock [额外参数]
```

`justfile` 的 `test` 目标则是循环对若干 feature 组合执行 `clippy`，确保每种组合都能通过静态检查。

#### 4.2.3 源码精读

`test.sh` 的核心就是 [test.sh:7-18](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/test.sh#L7-L18) 的 `case` 分支：

```sh
case ${CMD} in
  server)
    cargo run --release --features server,rpc --bin busrtd -- -B /tmp/busrt.sock \
      -B 0.0.0.0:9924 -B fifo:/tmp/busrt.fifo $*
    ;;
  cli)
    cargo run --release --bin busrt --features cli -- /tmp/busrt.sock $*
    ;;
  *)
    echo "command unknown: ${CMD}"
    ;;
esac
```

要点：

- `server` 分支预置了**三个**监听地址，分别对应三种通道（下一篇 u1-l3 与 u6 会详讲）：`/tmp/busrt.sock`（Unix socket）、`0.0.0.0:9924`（TCP）、`fifo:/tmp/busrt.fifo`（命名管道）。`$*` 把你在命令行追加的参数（比如更多 `-B` 或 `-v`）继续透传给 `busrtd`。
- 这里写的是 `--features server,rpc`。结合 4.1 可知，`server` 已经传递包含了 `rpc`，所以这里的 `,rpc` 其实是**冗余但无害**的，相当于一种「显式声明意图」的写法。
- `cli` 分支固定连接 `/tmp/busrt.sock`，后面 `$*` 透传子命令（如 `broker info`）。

`justfile` 的 `test` 目标见 [justfile:6-15](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/justfile#L6-L15)，它依次对 `server`、`broker`、`ipc`、`rpc`、`cli`、`server,rpc`、`ipc-sync`、`ipc-sync,rpc-sync`、`rt` 等组合跑 `clippy`，用来保证这些 feature 组合都能编译并过静态检查。

#### 4.2.4 代码实践

1. **实践目标**：用最少的命令，借助 `test.sh` 启动服务端。
2. **操作步骤**：阅读 `test.sh` 后，执行 `sh test.sh server`（首次会触发 release 编译，可能耗时数分钟）。
3. **需要观察的现象**：编译结束后，`busrtd` 开始监听，日志里会逐条打印 `binding at ...`。
4. **预期结果**：看到类似 `binding at /tmp/busrt.sock`、`binding at 0.0.0.0:9924`、`binding at fifo:/tmp/busrt.fifo`、`BUS/RT broker started` 的日志。具体时间戳与顺序「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`sh test.sh foo` 会输出什么？为什么？

**答案**：会输出 `command unknown: foo`。因为 `case` 里只有 `server` 和 `cli` 两个分支，其余都落入 `*)` 默认分支。

**练习 2**：如果你想用 `test.sh` 给 `busrtd` 追加「详细日志」开关，该怎么做？

**答案**：执行 `sh test.sh server --verbose`（或 `-v` 的长写法）。因为 `$*` 会把 `--verbose` 透传给 `busrtd`，而 `busrtd` 的 `Opts` 里有 `--verbose` 选项（见 4.3）。

### 4.3 busrtd 服务端启动：命令行参数与传输自动选择

#### 4.3.1 概念说明

`busrtd` 启动后要做三件事：

1. 解析命令行参数（用 [clap](https://docs.rs/clap) 这个库）。
2. 创建一个 `Broker`（代理）对象，并初始化它内置的核心 RPC（这样 `.broker` 方法才能用）。
3. 对命令行里每一个 `-B`（绑定地址），根据地址的**写法**自动判断该用哪种传输（Unix socket / TCP / fifo），然后开始监听。

理解「根据写法自动选传输」是本模块的重点：你不必告诉 `busrtd`「这是 unix」「这是 tcp」，它看路径格式就能推断。

#### 4.3.2 核心流程

`busrtd` 主流程（`main` 函数）的关键步骤：

```text
解析 Opts（clap）
  ↓
按 verbose/daemonize/syslog 配置日志
  ↓ （若 -D 则 fork 到后台）
构建多线程 Tokio 运行时（worker 数 = -w）
  ↓
Broker::create(...)
  ↓
init_default_core_rpc()      # 启用 .broker 内置 RPC（需 rpc feature）
  ↓
for 每个 -B 绑定路径:
    ├─ "fifo:..." 前缀       → spawn_fifo()
    ├─ 以 / 开头或 .sock/.socket/.ipc 结尾 → spawn_unix_server()
    └─ 其余                   → spawn_tcp_server()
  ↓
进入空闲循环，等待终止信号
  ↓
收到 SIGINT/SIGTERM → terminate() → announce(shutdown) → 退出
```

#### 4.3.3 源码精读

命令行参数定义在 [src/server.rs:66-110](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/server.rs#L66-L110)，其中最关键的是 `-B`：

```rust
#[clap(
    short = 'B',
    long = "bind",
    required = true,
    help = "Unix socket path, IP:PORT or fifo:path, can be specified multiple times"
)]
path: Vec<String>,
```

注意 `path` 是 `Vec<String>`，且 `required = true`——也就是说**至少要给一个 `-B`**，可以给多个。其他常用参数包括 `-w`（worker 线程数，默认 4）、`-t`（超时秒数，默认 5）、`--buf-size`、`--queue-size`、`-D`（daemonize 后台化）、`--force-register`（允许重名客户端强制注册）等。

绑定与传输选择的核心循环在 [src/server.rs:228-262](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/server.rs#L228-L262)，关键判断如下（简化）：

```rust
for path in opts.path {
    info!("binding at {}", path);
    if let Some(_fifo) = path.strip_prefix("fifo:") {
        // ① fifo 前缀 → 命名管道（需要 rpc feature）
        broker.spawn_fifo(_fifo, opts.buf_size).await...
    } else {
        // ② 以 / 开头或 .sock/.socket/.ipc 结尾 → Unix socket
        if path.ends_with(".sock") || path.ends_with(".socket")
            || path.ends_with(".ipc") || path.starts_with('/')
        {
            broker.spawn_unix_server(&path, server_config).await...
        } else {
            // ③ 其余（如 0.0.0.0:9924）→ TCP
            broker.spawn_tcp_server(&path, server_config).await...
        }
    }
}
```

把这段和 `test.sh` 的三个 `-B` 对上号：

- `/tmp/busrt.sock` 以 `/` 开头 → 走 `spawn_unix_server`。
- `0.0.0.0:9924` 不匹配 unix 规则 → 走 `spawn_tcp_server`。
- `fifo:/tmp/busrt.fifo` 有 `fifo:` 前缀 → 走 `spawn_fifo`。

还要注意：在绑定前，[src/server.rs:224-225](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/server.rs#L224-L225) 会调用 `broker.init_default_core_rpc()`（在 `#[cfg(feature = "rpc")]` 守卫下）。这一步注册了 `.broker` 目标的内置方法（`info`/`stats`/`client.list`/`test`），本讲后面 CLI 要用的 `broker info`、`broker stats` 就依赖它。也正因为 `server` feature 传递包含了 `rpc`，这段代码才会被编译进来。

优雅退出方面，[src/server.rs:221-222](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/server.rs#L221-L222) 注册了对 `SIGINT`（Ctrl-C）和 `SIGTERM` 的处理，触发 [src/server.rs:129-134](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/server.rs#L129-L134) 的 `terminate()`，后者会通过 `broker.announce(BrokerEvent::shutdown())` 广播一条 shutdown 公告，再删除 pid 文件和 socket 文件。

#### 4.3.4 代码实践

1. **实践目标**：让 `busrtd` **只**监听一个 Unix socket `/tmp/busrt.sock`（不用 `test.sh` 预置的三绑定）。
2. **操作步骤**：因为 `test.sh server` 把三个 `-B` 写死了，要只绑定一个 socket，应直接调用 cargo：
   `cargo run --release --features server --bin busrtd -- -B /tmp/busrt.sock -v`
3. **需要观察的现象**：日志只出现一行 `binding at /tmp/busrt.sock`，随后 `BUS/RT broker started`。
4. **预期结果**：进程在前台运行，`ls -l /tmp/busrt.sock` 能看到一个 socket 文件；按 Ctrl-C 后进程退出并自动删除该 socket 文件。是否自动删除「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`-B foo.sock` 和 `-B foo` 分别会走哪条传输分支？

**答案**：`foo.sock` 以 `.sock` 结尾 → Unix socket 分支；`foo` 既没有 `fifo:` 前缀、也不以 `/` 开头或特定后缀结尾 → TCP 分支（会被当成 `host:port` 解析）。

**练习 2**：为什么省略所有 `-B` 会让 `busrtd` 启动失败？

**答案**：因为 `Opts.path` 标注了 `required = true`，clap 在解析阶段就会报错退出，根本进不到绑定循环。

### 4.4 busrt CLI：连接代理与 broker 子命令

#### 4.4.1 概念说明

`busrt` CLI 是一个「一次性客户端」：它连接代理、执行一条子命令、打印结果、然后退出。它的子命令分成几类（见 [src/cli.rs:114-124](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/cli.rs#L114-L124)）：

- `broker`：向代理的 `.broker` 目标发起 RPC，查询代理自身信息（`info`/`stats`/`client.list`/`test`）。
- `listen`：订阅主题，持续监听收到的消息。
- `send`：点对点（或带通配符时广播）发消息。
- `publish`：向主题发布消息。
- `rpc`：RPC 相关（`listen`/`notify`/`call0`/`call`）。
- `benchmark`：内置基准测试。

本模块聚焦 `broker` 子命令，因为它是验证「服务端是否真的跑起来了」最直接的方式。

#### 4.4.2 核心流程

`broker info` / `broker stats` 的执行流程：

```text
解析 Opts（clap），得到代理地址 /tmp/busrt.sock 与子命令
  ↓
create_client(): 用 ipc::Config 建一个 ipc::Client 并 connect()
  ↓
包成 RpcClient（用 DummyHandlers，因为只是发起调用不需要处理）
  ↓
rpc.call(".broker", "info"/"stats", empty_payload, QoS::Processed)
  ↓ （代理把请求路由给自己内置的核心 RPC 处理器）
用 rmp-serde 把返回的 msgpack 载荷反序列化为 BrokerInfo / BrokerStats
  ↓
用 prettytable 打印成表格
```

也就是说，`broker info` 本质上是一次普通的 RPC 调用，目标 `.broker` 是代理「自己」——这就是为什么它依赖服务端启用了核心 RPC（4.3 提到的 `init_default_core_rpc()`）。

#### 4.4.3 源码精读

CLI 的顶层选项见 [src/cli.rs:126-147](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/cli.rs#L126-L147)。第一个位置参数 `path` 就是代理地址（`test.sh` 里固定传 `/tmp/busrt.sock`），此外有 `-n`（客户端名）、`--token`（鉴权 bearer）、`--timeout`、`-v`/`-s` 等。

`broker` 子命令的枚举见 [src/cli.rs:44-54](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/cli.rs#L44-L54)：

```rust
enum BrokerCommand {
    #[clap(name = "client.list")] ClientList,
    #[clap(name = "info")] Info,
    #[clap(name = "stats")] Stats,
    #[clap(name = "test")] Test,
}
```

注意 `client.list` 这种带点的名字用 `#[clap(name = ...)]` 显式声明，这样在命令行里写 `broker client.list` 才能正确匹配。

`broker info` 的处理见 [src/cli.rs:666-676](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/cli.rs#L666-L676)：

```rust
BrokerCommand::Info => {
    let rpc = RpcClient::new(client, DummyHandlers {});
    let result =
        wto!(rpc.call(".broker", "info", empty_payload!(), QoS::Processed)).unwrap();
    let info: BrokerInfo = rmp_serde::from_slice(result.payload()).unwrap();
    let mut table = ctable(vec!["field", "value"]);
    table.add_row(row!["author", info.author]);
    table.add_row(row!["version", info.version]);
    table.printstd();
}
```

它调用 `.broker` 的 `info` 方法，把返回的 msgpack 反序列化成 `BrokerInfo`，打印 `author` 和 `version` 两行。这两个字段的来源在代理侧 [src/broker.rs:1358-1364](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1358-L1364)：`author` 取自 `crate::AUTHOR`（`"(c) 2022 Bohemia Automation / Altertech"`，见 [src/lib.rs:39-41](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L39-L41)），`version` 取自 `crate::VERSION`（即 `CARGO_PKG_VERSION`，当前为 `0.5.5`）。

`broker stats` 的处理见 [src/cli.rs:652-665](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/cli.rs#L652-L665)，调用 `.broker` 的 `stats` 方法，反序列化成 `BrokerStats`，打印 `r_frames`/`r_bytes`/`w_frames`/`w_bytes`/`uptime`（结构定义见 [src/common.rs:43-55](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/common.rs#L43-L55)）。

连接代理的通用函数是 [src/cli.rs:368-379](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/cli.rs#L368-L379) 的 `create_client`，它根据 `Opts` 构建 `ipc::Config` 并 `Client::connect`。如果连不上会直接 `expect` 报错退出——所以一定要先确保 `busrtd` 在跑、且 `/tmp/busrt.sock` 存在。

#### 4.4.4 代码实践

1. **实践目标**：用 CLI 查询运行中代理的版本与统计。
2. **操作步骤**：在 4.3 已经启动 `busrtd` 的前提下，另开一个终端执行：
   - `sh test.sh cli broker info`
   - `sh test.sh cli broker stats`
3. **需要观察的现象**：两次命令分别打印一张两列表格。
4. **预期结果**：
   - `broker info` 打印 `author` 为 `(c) 2022 Bohemia Automation / Altertech`、`version` 为 `0.5.5`。
   - `broker stats` 打印 `r_frames`/`r_bytes`/`w_frames`/`w_bytes`（随交互次数增长）和 `uptime`（秒，随运行时间增长）。
   - 具体数值「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：如果不先启动 `busrtd`，直接 `sh test.sh cli broker info` 会怎样？

**答案**：`create_client` 里的 `Client::connect` 会失败，触发 `.expect("Unable to connect to the busrt broker")`，进程 panic 退出。

**练习 2**：`broker info` 返回的 `version` 字段在源码里是怎么得到的？

**答案**：代理侧 `BrokerInfo::info()` 把 `version` 设为 `crate::VERSION`，而 `VERSION = env!("CARGO_PKG_VERSION")`，即 Cargo.toml 里 `[package] version`（当前 0.5.5）。

## 5. 综合实践

把本讲四个模块串起来，完成下面的端到端联调任务（对应本讲规格里的代码实践任务）：

**任务**：参照 `test.sh`，用 `server` 命令启动一个监听 `/tmp/busrt.sock` 的 `busrtd`，再用 `cli` 命令执行 `broker info` 与 `broker stats`，记录输出。

**操作步骤**：

1. **清理旧 socket**：`rm -f /tmp/busrt.sock /tmp/busrt.fifo`（避免上一次残留干扰）。
2. **终端 A——启动服务端**：`sh test.sh server`。
   - 首次会触发 release 编译；完成后日志应出现三行 `binding at ...` 和 `BUS/RT broker started`。
   - 想只绑定 unix socket，可改用 `cargo run --release --features server --bin busrtd -- -B /tmp/busrt.sock -v`（见 4.3.4）。
3. **终端 B——查询信息**：`sh test.sh cli broker info`，记录 `author` 与 `version`。
4. **终端 C——查询统计**：`sh test.sh cli broker stats`，记录各计数字段与 `uptime`。
5. **停止服务端**：在终端 A 按 Ctrl-C，观察日志打印 `terminating`，并确认 `/tmp/busrt.sock` 被清理。

**需要观察的现象**：

- 终端 B/C 能成功连上代理并打印表格，说明「代理进程 ↔ CLI 客户端」链路打通。
- 多跑几次 `broker stats`，`uptime` 单调递增、计数字段非零，说明代理在正常工作。

**预期结果**：

- `broker info`：`version` = `0.5.5`，`author` = `(c) 2022 Bohemia Automation / Altertech`。
- `broker stats`：五项字段齐全且 `uptime` 随时间增长。
- 各字段具体数值「待本地验证」。

**思考延伸**：如果你在启动服务端时去掉 `,rpc`（即手动执行 `cargo run --release --features server --bin busrtd -- -B /tmp/busrt.sock`），`broker info` 还能成功吗？结合 4.1 关于「`server` 已传递包含 `rpc`」的结论想一想，再实际验证你的判断。

## 6. 本讲小结

- BUS/RT 产出两个二进制：服务端 `busrtd`（入口 `src/server.rs`）和 CLI 客户端 `busrt`（入口 `src/cli.rs`），分别由 `server` 和 `cli` 两个 feature 通过 `required-features` 门控。
- `server` feature 传递依赖 `broker-rpc`→`rpc`，所以 `busrtd` 天然带核心 RPC 能力；`test.sh` 里 `,rpc` 是冗余但无害的写法。
- `test.sh` 用 `case` 把命令封装成 `server`/`cli` 两个分支，预置了三种 `-B` 绑定；`justfile` 的 `test` 目标则对多种 feature 组合跑 clippy。
- `busrtd` 根据 `-B` 路径的写法自动选择传输：`fifo:` 前缀→命名管道，以 `/` 开头或 `.sock`/`.socket`/`.ipc` 结尾→Unix socket，其余→TCP。
- `broker info`/`stats` 本质是向代理自身 `.broker` 目标发起的 RPC 调用，依赖服务端 `init_default_core_rpc()`，返回值用 rmp-serde 反序列化后由 prettytable 打印。
- 优雅退出通过 SIGINT/SIGTERM 触发，会广播 `BrokerEvent::shutdown()` 并清理 pid/socket 文件。

## 7. 下一步学习建议

本讲让你能「把 BUS/RT 跑起来并用 CLI 调试」。接下来建议：

- **u1-l3（源码地图）**：系统梳理 `src/` 目录与「feature→模块」的对应关系，建立全局导航，为阅读协议与 Broker 内部做准备。
- **在跑通本讲后再尝试**：用 `sh test.sh cli listen -t '#'` 订阅全部主题，另开终端 `sh test.sh cli publish test/topic hello`，亲手体验发布订阅；这会自然衔接 u3（通信模式）的内容。
- 进阶阅读：想理解 `broker info` 那条 RPC 在代理内部是如何被处理和路由的，可先跳读 [src/broker.rs:1358-1364](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1358-L1364) 附近的 `BrokerRpcHandlers`，但这部分建议留到 u5（RPC 层）和 u6（Broker 内部）再深入。
