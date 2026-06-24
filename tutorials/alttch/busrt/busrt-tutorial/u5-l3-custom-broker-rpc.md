# 自定义 Broker RPC 与核心 RPC 接口

## 1. 本讲目标

学完本讲，你应当能够：

- 理解「Broker 自己也是一个客户端」这个设计，并知道这个特殊的客户端名字是 `.broker`。
- 掌握两种给 Broker 挂载 RPC 的方式：`init_default_core_rpc()`（一键挂内置处理器）与 `set_core_rpc_client()`（挂你自己的处理器）。
- 说清代理内置的四个核心 RPC 方法 `test` / `info` / `stats` / `client.list`（外加一个 `benchmark.test`）分别返回什么。
- 理解 `BrokerEvent` announce 机制：客户端注册/注销/关停时，代理会向 `.broker/info`、`.broker/warn` 主题广播事件，以及为什么所有客户端默认就能收到 `shutdown`。
- 能够参照 `broker_custom_rpc.rs` 写出一个挂载自定义 RPC 的嵌入式 Broker，并用 `busrt` CLI 调用验证。

本讲是 u5（RPC 层）的收尾：u5-l1 讲了 RPC 帧协议，u5-l2 讲了通用 `RpcClient` / `RpcHandlers`，本讲把它们落到一个特殊角色上——**代理进程自己**。

## 2. 前置知识

阅读本讲前，请确认你已经理解以下内容（来自前置讲义）：

- **u3-l1 嵌入式 Broker 与内部客户端**：`Broker::new()` 创建一个普通 Rust 值，`register_client(name)` 注册一个内部客户端并返回 `broker::Client` 句柄；内部客户端实现 `AsyncClient` trait、永不断线、确认即时兑现。
- **u4-l1 AsyncClient trait**：统一的异步客户端接口，`send`/`publish`/`subscribe` 等方法返回 `Result<OpConfirm, Error>`；`take_event_channel()` 拿走入站帧的接收端。
- **u5-1 RPC 协议**：RPC 复用 Message 帧，载荷首字节区分通知 `0x00` / 请求 `0x01` / 回复 `0x11` / 错误 `0x12`；`RpcEvent` 是对帧的零拷贝语义视图。
- **u5-2 RpcClient 与 RpcHandlers**：`RpcClient::new(client, handlers)` 在底层客户端之上套一层 RPC；`RpcHandlers` trait 的三个回调 `handle_call` / `handle_notification` / `handle_frame` 分别处理 RPC 请求、RPC 通知、非 RPC 帧。

一个关键事实会在本讲反复用到：**`RpcClient::new` 会在构造时自动启动 RPC 事件循环**（`processor` 任务），所以你不需要手动 spawn 一个循环——只要 `RpcClient` 被创建，它就开始消费底层客户端的事件通道并把帧分发给 handler。这一点是理解「为什么 `init_default_core_rpc` 之后代理就能立刻响应 RPC」的钥匙。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/broker.rs` | 定义 `.broker` 相关常量、`BrokerEvent`、`BrokerRpcHandlers`（内置处理器）、`init_default_core_rpc` / `set_core_rpc_client` / `core_rpc_client` / `announce`、`spawn_fifo`。本讲的主战场。 |
| `src/rpc/async_client.rs` | `RpcClient::new` / `init`，展示构造时自动 spawn `processor`。 |
| `src/common.rs` | `BrokerInfo` / `BrokerStats` / `ClientInfo` / `ClientList` 四个可序列化结构，是内置 RPC 方法的返回值类型。 |
| `examples/broker_custom_rpc.rs` | 官方示例：挂载自定义 handler，同时开 unix、websocket、fifo 三种入口。 |
| `src/server.rs` | 独立服务端 `busrtd` 调用 `init_default_core_rpc()` 的位置，证明内置方法是默认开启的。 |

## 4. 核心概念与源码讲解

### 4.1 把 RPC 挂到 Broker 上：核心 RPC 客户端

#### 4.1.1 概念说明

到目前为止，你见过的 RPC 都是「客户端 A 调用客户端 B」：A 发请求，B 的 `RpcHandlers` 处理并回复。本讲要回答一个新问题：**能不能直接调用代理本身？** 比如问代理「你现在连了几个客户端？」「你收发了多少字节？」

BUS/RT 的答案很优雅：**代理进程自己注册一个名为 `.broker` 的内部客户端**。于是「调用代理」就退化成「向 `.broker` 这个客户端发起点对点 RPC」。这带来三个好处：

1. 代理复用了和普通客户端完全一样的消息投递路径（u3-l3 的 `send!` 宏、u4 的 `AsyncClient`），无需为管理接口单独开一套机制。
2. 任何能连上代理的客户端，都能用标准 RPC 调用 `.broker`，`busrt` CLI 的 `broker info` / `stats` 本质就是这种调用（见 u1-l2）。
3. 这个 `.broker` 客户端同时承担「广播系统事件」的职责（4.3 节的 announce），一举两得。

这个 `.broker` 客户端连同它身上挂的 `RpcClient`，合称**核心 RPC 客户端（core RPC client）**。它被存放在 `BrokerDb` 里，是代理的全局共享资源。

#### 4.1.2 核心流程

挂载核心 RPC 有两条路径，对应两种 API：

```
路径 A：init_default_core_rpc()  —— 一键挂「内置处理器」
  1. register_client(".broker")           注册内部客户端，拿到 Client 句柄
  2. BrokerRpcHandlers{ db }              构造内置处理器（4.2 节详解）
  3. RpcClient::new(client, handlers)     构造 RpcClient —— 此时 processor 自动启动
  4. set_core_rpc_client(rpc_client)      把它存进 BrokerDb.rpc_client

路径 B：set_core_rpc_client(your_rpc)  —— 挂「你自己的处理器」
  1. register_client(".broker")           你自己先注册（示例里就是这么做的）
  2. （可选）core_client.subscribe("#")    订阅全部主题，让 handle_frame 能看到发布帧
  3. RpcClient::new(core_client, MyHandlers)  构造你自己的 RpcClient
  4. set_core_rpc_client(rpc_client)      替换核心 RPC 客户端
```

> ⚠️ 关键取舍：路径 A 和路径 B 是**互斥**的。`set_core_rpc_client` 是「整体替换」——一旦你挂上自己的处理器，内置的 `info` / `stats` / `client.list` 就不再被处理（除非你在自己的 handler 里重新实现它们）。如果你既要自定义方法、又想保留内置自省能力，就得在自定义 handler 里手动转发这些方法，或干脆只用 `init_default_core_rpc` 而不改写。

无论哪条路径，第 3 步的 `RpcClient::new` 都会**立即**在后台 spawn `processor` 事件循环，所以挂载完成后代理马上就能响应 RPC。

#### 4.1.3 源码精读

先看 `.broker` 这个名字和它关联的两个主题常量，它们定义在 broker.rs 顶部、不受 feature 门控：

[src/broker.rs:54-56](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L54-L56) —— 定义 `.broker`（客户端名）、`.broker/info`（注册/注销事件）、`.broker/warn`（关停事件）三个常量。

路径 A 的全部实现只有 9 行，它把「注册客户端 + 构造处理器 + 构造 RpcClient + 存入数据库」串成一步：

[src/broker.rs:1366-1374](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1366-L1374) —— `init_default_core_rpc`：注册 `.broker` 客户端，用 `BrokerRpcHandlers`（持有 `db` 的 `Arc`）构造 `RpcClient`，再调 `set_core_rpc_client` 存起来。注意它需要 `broker-rpc` feature。

路径 B 的落点是这个写方法：

[src/broker.rs:1379-1383](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1379-L1383) —— `set_core_rpc_client`：用 `Mutex::lock().await.replace(client)` 把核心 RPC 客户端写入 `BrokerDb.rpc_client`。`replace` 意味着旧的会被丢弃（整体替换）。

配套的读取方法用来给 fifo、announce 等机制取回这个客户端：

[src/broker.rs:1384-1388](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1384-L1388) —— `core_rpc_client`：返回 `Arc<Mutex<Option<RpcClient>>>`，调用方加锁后取出。示例里就用它判断 `.broker` 是否还连着。

那「自动启动事件循环」发生在哪？在 `RpcClient` 的构造函数里：

[src/rpc/async_client.rs:292-307](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/async_client.rs#L292-L307) —— `RpcClient::init`：先 `take_event_channel()` 拿走入站帧接收端 `rx`，紧接着 `tokio::spawn(processor(rx, client, calls, handlers, opts))` 把处理器任务丢到后台。这就是为什么挂载后无需手动启动循环。

最后，独立服务端 `busrtd` 默认走的就是路径 A，证明内置方法对 `busrtd` 用户开箱即用：

[src/server.rs:225](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/server.rs#L225) —— `busrtd` 启动时调用 `broker.init_default_core_rpc()`。

#### 4.1.4 代码实践

**实践目标**：亲手验证「`init_default_core_rpc` 之后，`.broker` 立刻能响应内置 RPC」。

**操作步骤**：

1. 在 `examples/` 下新建一个最小示例（示例代码，非项目原有）：

   ```rust
   // examples/mini_core_rpc.rs （示例代码）
   use busrt::broker::{Broker, ServerConfig};
   #[tokio::main]
   async fn main() -> Result<(), Box<dyn std::error::Error>> {
       let mut broker = Broker::new();
       broker.spawn_unix_server("/tmp/busrt.sock", ServerConfig::default()).await?;
       broker.init_default_core_rpc().await?;   // 挂载内置处理器
       // 阻塞住，保持代理运行
       tokio::signal::ctrl_c().await?;
       Ok(())
   }
   ```

2. 编译运行（需要 `broker` + `broker-rpc`）：

   ```bash
   cargo run --example mini_core_rpc --features "broker broker-rpc"
   ```

3. 另开一个终端，用 `busrt` CLI 调用内置 `test` 方法：

   ```bash
   cargo run --features cli -- /tmp/busrt.sock rpc call .broker test
   ```

**需要观察的现象**：CLI 立刻返回一个结果，而不是超时或报 method not found。

**预期结果**：返回 `{"ok": true}`（msgpack 解码后的形式）。这证明 `init_default_core_rpc` 已经把内置 `BrokerRpcHandlers` 挂好，且处理器循环已自动运行。如果返回错误，多半是忘记加 `broker-rpc` feature。

> 待本地验证：不同版本 CLI 对 msgpack 布尔的打印格式可能略有差异（如 `true` / `(bool) true`）。

#### 4.1.5 小练习与答案

**练习 1**：如果只调用 `set_core_rpc_client(my_rpc)` 而不先 `register_client(".broker")`，会发生什么？

**答案**：`.broker` 这个客户端根本不存在，发往 `.broker` 的点对点消息会在 `send!` 宏里查 `clients` 表未命中，返回 `not_registered` 错误；你的 handler 永远收不到调用。所以无论路径 A 还是 B，注册 `.broker` 客户端都是前提——路径 A 把这一步包进了 `init_default_core_rpc`，路径 B 必须手动做（示例第 64 行就是这么做的）。

**练习 2**：为什么 `set_core_rpc_client` 之后不用再手动 spawn 一个 processor 循环？

**答案**：因为 `RpcClient::new`（即 `init`）在构造时已经 `tokio::spawn(processor(...))` 把事件循环丢到后台了。`set_core_rpc_client` 只是把已经「活着」的 `RpcClient` 存进数据库，循环早已在消费 `.broker` 客户端的事件通道。

### 4.2 代理内置的核心 RPC 接口：test / info / stats / client.list

#### 4.2.1 概念说明

路径 A 挂上去的处理器 `BrokerRpcHandlers` 提供了一组「管理自省」方法，它们是 `busrt` CLI 的 `broker info` / `stats` / `client.list` 背后的真正实现。这些方法让你不必停机就能观察代理的运行状态：

- `test`：健康检查，固定返回 `{"ok": true}`，不接受参数。
- `info`：返回代理的作者与版本号（`BrokerInfo`）。
- `stats`：返回代理启动以来的收发统计（`BrokerStats`）。
- `client.list`：返回当前所有（主）客户端的列表与各自的收发计数（`ClientList`），支持正则过滤。
- `benchmark.test`：基准测试专用，原样回显载荷，供 `busrt benchmark` 测吞吐。

这些返回值都用 **msgpack**（`rmp_serde`）序列化——这也是为什么 `broker-rpc` feature 要依赖 `rmp-serde`（见 u1-l3）。

#### 4.2.2 核心流程

`BrokerRpcHandlers::handle_call` 收到一个 RPC 请求后：

```
1. parse_method()           取出方法名字符串
2. 若是 "benchmark.test"     → 原样回显 payload（基准测试）
3. 否则再 parse_method()，match 方法名：
   "test"        → 校验无参数 → 返回 RPC_OK（即 msgpack {"ok": true}）
   "info"        → 校验无参数 → rmp_serde 序列化 Broker::info()
   "stats"       → 校验无参数 → rmp_serde 序列化 self.db.stats()
   "client.list" → 委托 self.client_list(payload)（可带 filter 参数）
   其他          → RpcError::method(None)（方法不存在）
```

注意一个小细节：每个方法都用 `if !payload.is_empty() { return Err(RpcError::params(None)) }` 拒绝多余参数，这是一种轻量的参数校验。`client.list` 是唯一真正解析参数的方法（可选的 `filter` 正则）。

#### 4.2.3 源码精读

内置处理器的主体：

[src/broker.rs:1066-1099](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1066-L1099) —— `impl RpcHandlers for BrokerRpcHandlers`：`handle_call` 按 `parse_method()` 分发到四个方法；`handle_notification` 和 `handle_frame` 都是空实现（代理自身不处理通知与非 RPC 帧）。

`test` 方法返回的 `RPC_OK` 是一段预先算好的 msgpack 字节常量，省去每次调用都序列化：

[src/broker.rs:1063](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1063) —— `RPC_OK = [129, 162, 111, 107, 195]`。这 5 个字节就是 msgpack 编码的 `{"ok": true}`：`0x81`（1 项的 fixmap）、`0xa2`（长度 2 的 fixstr）、`ok`、`0xc3`（true）。

`info` 与 `stats` 的数据来源：

[src/broker.rs:1354-1364](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1354-L1364) —— `stats()` 从 `BrokerDb` 的四个 `AtomicU64`（r/w frames/bytes）与 `startup_time` 汇总；`info()` 返回编译期常量 `AUTHOR` / `VERSION`。

这两个方法的返回类型定义在 `common.rs`：

[src/common.rs:43-56](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/common.rs#L43-L56) —— `BrokerStats`（uptime + 收发帧/字节）与 `BrokerInfo`（author/version），都派生了 `Serialize`/`Deserialize`。

`client.list` 是最复杂的一个，它遍历 `BrokerDb.clients` 并支持正则过滤：

[src/broker.rs:1102-1148](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1102-L1148) —— `client_list`：可选解析 `{ filter: "正则" }` 参数，加锁 `clients` 表，**只保留 primary 客户端**（`c.primary`，过滤掉 `%%` 二级客户端），按正则筛名字，收集每个客户端的 kind/source/port/收发计数/当前队列长度/实例数，最后按名字排序返回 `ClientList`。

返回的单条客户端信息结构：

[src/common.rs:12-23](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/common.rs#L12-L23) —— `ClientInfo`：单客户端的快照（含 `queue` 当前队列深度、`instances` 含二级在内的实例数）。

#### 4.2.4 代码实践

**实践目标**：用 `busrt` CLI 触发四个内置方法，对照源码确认返回内容。

**操作步骤**（接 4.1.4 跑起来的 `mini_core_rpc`）：

```bash
# 健康检查
cargo run --features cli -- /tmp/busrt.sock rpc call .broker test
# 版本信息
cargo run --features cli -- /tmp/busrt.sock rpc call .broker info
# 收发统计
cargo run --features cli -- /tmp/busrt.sock rpc call .broker stats
# 客户端列表（带正则过滤，只看名字以 . 开头的）
cargo run --features cli -- /tmp/busrt.sock rpc call .broker client.list filter=^\\.
```

**需要观察的现象**：四条命令分别返回布尔、版本对象、统计对象、客户端数组。

**预期结果**：`test` 返回 `{"ok": true}`；`info` 返回类似 `{author=..., version=...}`；`stats` 返回含 `uptime/r_frames/...` 的对象；`client.list` 返回一个数组，里面至少有 `.broker` 自己（此时通常只有它一个客户端，外加你这条 CLI 连接临时注册的客户端）。

> 待本地验证：`client.list` 在你的 CLI 连接期间会多出一条客户端记录；CLI 退出后该记录消失（这正好引出 4.3 的 reg/unreg 事件）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `test` 用预计算的 `RPC_OK` 字节常量，而 `info` / `stats` 每次都调 `rmp_serde::to_vec_named`？

**答案**：`test` 的返回值是固定的 `{"ok": true}`，预先编码成 5 字节常量可避免每次调用都走序列化，是热路径上的微优化；而 `info` 尤其 `stats` 的内容随时间变化（uptime、收发计数持续增长），无法预计算，必须实时序列化。

**练习 2**：`client.list` 为什么过滤掉非 primary 客户端？

**答案**：二级客户端（名字含 `%%`，如 `worker%%0`，见 u3-l2）是某个主客户端的附属实例，把它们全列出来会让列表膨胀且信息冗余。`client_list` 用 `c.primary` 只保留主客户端，并通过 `instances = secondaries.len() + 1` 在主客户端上汇总其实例总数，既精简又保留了「这个主客户端开了几个实例」的信息。

### 4.3 BrokerEvent announce 与 .broker 主题约定

#### 4.3.1 概念说明

除了「被调用」，`.broker` 客户端还有一项主动职责：**广播系统事件**，称为 announce。当有客户端注册、注销，或代理即将关停时，代理会向特定主题发布一条 msgpack 编码的事件，让所有关心的客户端感知拓扑变化。

这里有一个对运维很重要的约定（两个主题分工不同）：

- `.broker/info`：发布 `reg`（注册）和 `unreg`（注销）事件。客户端想感知拓扑变化，需**主动订阅**这个主题。
- `.broker/warn`：发布 `shutdown`（关停）事件。**所有客户端在注册时会被自动订阅**这个主题（见 u3-l2 的 `insert_client`），所以无需额外操作，每个客户端默认都能收到代理关停通知，从而及时重连或退出。

#### 4.3.2 核心流程

announce 的触发点贯穿客户端生命周期：

```
register_client 成功 → 若是 primary → announce(reg, .broker/info)
drop_client          → 若是 primary → announce(unreg, .broker/info)
代理关停             → 广播         → announce(shutdown, .broker/warn)
```

`BrokerDb::announce` 的执行：

```
1. 锁 db.rpc_client，取出核心 RPC 客户端（若未初始化则静默跳过）
2. 给 event 打上当前纳秒时间戳 t
3. 用 rmp_serde::to_vec_named 序列化 event
4. 通过核心 RPC 客户端的底层 client，publish 到 event.topic，QoS::No
```

关键点：announce **依赖核心 RPC 客户端已初始化**。如果没调用 `init_default_core_rpc` / `set_core_rpc_client`，`rpc_client` 为 `None`，announce 直接是空操作（`if let Some(...)` 不命中），不会报错也不会发布。

#### 4.3.3 源码精读

`BrokerEvent` 是一个带生命周期的结构，字段名刻意用单字母以缩短线上体积，serde 时 `topic` 字段被跳过（它是路由用的，不该进载荷）：

[src/broker.rs:610-656](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L610-L656) —— `BrokerEvent` 结构与构造器：`new` / `shutdown`（topic=`.broker/warn`）/ `reg`（topic=`.broker/info`）/ `unreg`（topic=`.broker/info`），以及 getter `subject()` / `data()` / `time()`。注意 `#[cfg_attr(feature = "broker-rpc", serde(skip))]` 让 `topic` 不参与序列化。

`BrokerDb::announce` 的实现：

[src/broker.rs:707-724](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L707-L724) —— 取核心 RPC 客户端、打时间戳 `now_ns()`、msgpack 序列化、`publish(event.topic, ..., QoS::No)`。`QoS::No` 意味着 announce 不要求 ACK，走最高吞吐路径。

注册成功时触发 reg：

[src/broker.rs:749-754](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L749-L754) —— `register_client` 末尾，若是 primary 客户端就 `announce(BrokerEvent::reg(&name))`。

注销时触发 unreg：

[src/broker.rs:812](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L812) —— `drop_client` 中 `announce(BrokerEvent::unreg(&client.name))`。

所有客户端默认订阅 `.broker/warn` 的位置（这是 shutdown 能被全员收到的根本原因）：

[src/broker.rs:780](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L780) —— `insert_client` 里 `sdb.subscribe(BROKER_WARN_TOPIC, &client)`，给每个新客户端自动订阅关停主题。

对外暴露的 `Broker::announce`（供你手动发布自定义事件）：

[src/broker.rs:1389-1393](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1389-L1393) —— `Broker::announce` 委托给 `BrokerDb::announce`。

#### 4.3.4 代码实践

**实践目标**：观察 reg/unreg 事件，并理解「shutdown 全员可达」。

**操作步骤**（接 4.1.4 的 `mini_core_rpc`）：

1. 终端 A 跑代理（已挂 `init_default_core_rpc`）。
2. 终端 B 订阅 `.broker/info`，保持监听：

   ```bash
   cargo run --features cli -- /tmp/busrt.sock rpc listen -t .broker/info
   ```

3. 终端 C 启动一个临时客户端连接（比如再发一次 `broker info`），然后退出。
4. 在终端 B 观察输出。
5. 最后在终端 A 按 Ctrl+C 关停代理，同时在另一个已订阅 `.broker/warn` 的终端观察 shutdown。

**需要观察的现象**：终端 C 的客户端连接时，终端 B 收到一条 `reg` 事件；终端 C 退出时，收到一条 `unreg` 事件。

**预期结果**：事件载荷是 msgpack map，含 `s`（"reg"/"unreg"）、`d`（客户端名）、`t`（纳秒时间戳）。shutdown 事件发布在 `.broker/warn`，因为全员默认订阅该主题，所以不必专门订阅也能收到。

> 待本地验证：若你在终端 B 用 `rpc listen` 而非 `listen`，需确认 CLI 是否会把订阅到的 publish 帧打印出来；不同子命令对非 RPC 帧的处理见 u8-l2。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `reg`/`unreg` 发到 `.broker/info` 而 `shutdown` 发到 `.broker/warn`，且只有后者被自动订阅？

**答案**：拓扑变化（reg/unreg）频率较高、信息量大，并非每个客户端都关心，强制推送会制造噪声，所以放在 `.broker/info` 让需要的客户端按需订阅；而关停是必须让所有人知道的关键事件（否则客户端会对着一个已死的代理空转），所以放在 `.broker/warn` 并在注册时自动订阅，保证「零配置即可收到关停通知」。

**练习 2**：如果代理没有调用 `init_default_core_rpc` 也没 `set_core_rpc_client`，客户端注册时还会发布 reg 事件吗？

**答案**：不会。`BrokerDb::announce` 开头是 `if let Some(rpc_client) = self.rpc_client.lock().await.as_ref()`，核心 RPC 客户端未初始化时这个分支不命中，函数直接返回 `Ok(())`，什么都不发布。announce 强依赖核心 RPC 客户端。

### 4.4 自定义 Broker RPC 完整示例

#### 4.4.1 概念说明

路径 B（`set_core_rpc_client`）的完整范例就是官方示例 `broker_custom_rpc.rs`。它演示了一个真实场景：你想给代理挂上自己的业务方法（如 `ping`），同时又想用 fifo 让 shell 脚本能往代理灌消息。该示例同时开启了三种入口（unix / websocket / fifo），是把 u5-l1、u5-l2、4.1～4.3 串起来的「集成样板」。

需要特别留意示例里 `set_core_rpc_client` 的副作用：示例挂的是 `MyHandlers`（只认 `test`/`ping`），**完全替换**了内置处理器。因此跑这个示例时，`rpc call .broker info` 会返回 method not found（`RpcError::method`），而 `rpc call .broker test` 返回的是 `"passed"`（字符串），与 4.2 里 `busrtd` 返回的 `{"ok": true}` 不同——这正是「替换 vs 内置」的活教材。

#### 4.4.2 核心流程

`broker_custom_rpc.rs` 的 `main` 流程：

```
1. Broker::new()                                  建空代理
2. spawn_unix_server("/tmp/busrt.sock")           开 unix 入口
3. spawn_websocket_server("127.0.0.1:3001")       开 websocket 入口
4. register_client(".broker")                     手动注册核心客户端（路径 B 必须自己做）
5. core_client.subscribe("#")                     订阅全部主题 → handle_frame 能看到所有发布帧
6. RpcClient::new(core_client, MyHandlers)        构造自定义 RPC（processor 自动启动）
7. set_core_rpc_client(crpc)                      整体替换核心 RPC 客户端
8. spawn_fifo("/tmp/busrt.fifo")                  开 fifo 入口（依赖第 7 步已完成）
9. 循环 publish("test", "broker alive")           演示代理主动发布
```

handler 部分：`handle_call` 用 `parse_method()` 分发——`test` 返回 `"passed"`，`ping` 用 `rmp_serde::from_slice` 把载荷反序列化成 `PingParams{ message }` 并把 `message` 字段原样回送；其它方法返回 `RpcError::method(None)`。

#### 4.4.3 源码精读

先看 handler 的请求处理（这是本讲实践任务的核心）：

[examples/broker_custom_rpc.rs:18-31](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/examples/broker_custom_rpc.rs#L18-L31) —— `impl RpcHandlers for MyHandlers::handle_call`：`ping` 分支把 `event.payload()` 反序列化成 `PingParams`，返回 `params.message` 的字节。这正是「返回 msgpack 解出的 message 字段」。

`PingParams` 用 `Option<&str>` 以兼容「不带 message 字段」的调用：

[examples/broker_custom_rpc.rs:13-16](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/examples/broker_custom_rpc.rs#L13-L16) —— `PingParams<'a>` 借用式反序列化，`message` 可选。

`main` 的挂载关键几步：

[examples/broker_custom_rpc.rs:64-76](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/examples/broker_custom_rpc.rs#L64-L76) —— 注册 `.broker`、订阅 `#`、`RpcClient::new`、`set_core_rpc_client`、`spawn_fifo`。注意注释说明 `set_core_rpc_client` 后才能 spawn fifo（fifo 依赖核心 RPC 客户端）。

fifo 依赖核心 RPC 客户端这一前置条件在源码里有显式检查：

[src/broker.rs:1625-1629](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1625-L1629) —— `spawn_fifo` 开头：若 `rpc_client` 为 `None`，直接返回 `Error::not_supported(BROKER_RPC_NOT_INIT_ERR)`。这就是为什么必须先 `set_core_rpc_client`。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：运行 `broker_custom_rpc`，用 `busrt` CLI 调用自定义 `ping` 方法，验证它返回 msgpack 解出的 `message` 字段。

**操作步骤**：

1. 启动示例（需要 `broker` + `broker-rpc`）：

   ```bash
   cargo run --example broker_custom_rpc --features "broker broker-rpc"
   ```

   等到看到 `Waiting for frames to .broker`。

2. 另开终端，调用 `ping`，传入 `message=hello`（CLI 会把 `key=value` 解析成 msgpack map，见 `common.rs` 的 `str_to_params_map`）：

   ```bash
   cargo run --features cli -- /tmp/busrt.sock rpc call .broker ping message=hello
   ```

3. 对照调用 `test`，确认走的是自定义 handler 而非内置：

   ```bash
   cargo run --features cli -- /tmp/busrt.sock rpc call .broker test
   ```

**需要观察的现象**：第 2 步返回 `hello`；第 3 步返回 `passed`（而不是 4.2 里 `busrtd` 的 `{"ok": true}`）。

**预期结果**：

- `ping message=hello` → handler 把载荷反序列化得 `message="hello"`，回送其字节，CLI 解码后打印 `hello`。
- `test` → `MyHandlers` 返回 `"passed"`，CLI 打印 `passed`。
- 若调用 `rpc call .broker info` → 返回 method not found 错误，证明内置处理器已被整体替换。

> 待本地验证：CLI 对返回字符串的打印格式（是否带引号、是否标注类型）依版本而异；若 `message=hello` 的键名解析方式与预期不符，可改用客户端代码直接构造 msgpack 载荷调用。

#### 4.4.5 小练习与答案

**练习 1**：示例里 `core_client.subscribe("#")` 这一步去掉会怎样？

**答案**：`handle_call` / `handle_notification` 不受影响（它们由 RPC 请求/通知触发，与主题订阅无关），但 `handle_frame` 将再也收不到 `publish` 广播的帧——因为代理发布到主题后，只有订阅了匹配主题的客户端才会收到。示例主循环每 100ms 向 `test` 主题 publish `"broker alive"`，正是靠 `subscribe("#")` 让 `MyHandlers::handle_frame` 能把这些发布帧打印出来。去掉订阅后，这些帧就没人接收了。

**练习 2**：如何在保留内置 `info`/`stats` 的同时增加自定义 `ping` 方法？

**答案**：不能同时挂两个处理器。可选方案：(a) 在你自己的 handler 里手动实现 `info`/`stats`/`client.list`（参考 4.2 的源码，需要拿到 `db` 句柄）；(b) 只用 `init_default_core_rpc`，放弃自定义方法，把业务逻辑放到另一个普通客户端上（注册一个非 `.broker` 的客户端挂 RPC），这样 `.broker` 保留内置自省，业务 RPC 走独立客户端。方案 (b) 通常是更干净的架构。

## 5. 综合实践

**任务**：搭建一个「可观测的嵌入式 Broker」，把本讲四个模块串起来。

要求：

1. 用 `Broker::new()` 建代理，开 unix 服务 `/tmp/busrt.sock`。
2. 调用 `init_default_core_rpc()` 挂载内置处理器（路径 A）。
3. 用 `busrt` CLI 完成三件事并记录输出：
   - `rpc call .broker info` —— 确认版本信息（验证 4.1 + 4.2）。
   - `rpc call .broker client.list` —— 记录此时有哪些客户端（验证 4.2）。
   - `rpc listen -t .broker/info` 后，另开一个客户端连接再断开，观察 reg/unreg 事件（验证 4.3）。
4. 思考题（不用实现）：如果你想把上面的 `info` 换成自定义的 `ping`，需要改用哪条路径？会丢失什么能力？（验证 4.4 的取舍）

**验收标准**：能解释「为什么 `client.list` 里会出现又消失客户端记录」「为什么 reg 事件只有订阅了 `.broker/info` 才看得到，而 shutdown 不用订阅也能收到」。

> 待本地验证：第 3 步中 `rpc listen` 是否能稳定打印 publish 帧、以及事件载荷的具体字段，建议在本地实际跑一遍对照。

## 6. 本讲小结

- **代理自身是一个名为 `.broker` 的内部客户端**：调用代理 = 向 `.broker` 发点对点 RPC，复用了全部普通消息投递机制。
- **两条挂载路径互斥**：`init_default_core_rpc()` 一键挂内置处理器（`busrtd` 默认走这条）；`set_core_rpc_client(your_rpc)` 整体替换为你自己的处理器。
- **`RpcClient::new` 自动启动事件循环**：构造时即 `tokio::spawn(processor(...))`，挂载完成后代理立刻可响应 RPC，无需手动 spawn。
- **内置四方法**：`test`→`{"ok":true}`（预编码常量）、`info`→版本、`stats`→收发计数、`client.list`→主客户端列表（可正则过滤），全部 msgpack 序列化。
- **announce 事件分两个主题**：`.broker/info`（reg/unreg，需主动订阅）与 `.broker/warn`（shutdown，全员注册时自动订阅，故零配置可收关停）。
- **announce 与 fifo 都强依赖核心 RPC 客户端**：未初始化时 announce 静默空操作、`spawn_fifo` 直接返回 `not_supported`。

## 7. 下一步学习建议

本讲完成了 RPC 层（u5）的全部内容。接下来：

- **u6-l1 连接生命周期**：深入 `handle_peer` / `handle_reader`，看一条外部客户端的 RPC 请求帧是如何被解析、鉴权、经 `send!` 宏投递到 `.broker` 核心客户端的事件通道，最终被本讲的 `processor` 取出处理的——这是把 u5 与传输层缝合的关键一讲。
- **u8-l1 busrtd 独立服务端**：看 `init_default_core_rpc` 在真实服务里的完整调用上下文，以及信号处理如何触发 `BrokerEvent::shutdown()` 广播。
- **u8-l3 FIFO 通道与 announce 事件**：详读 `spawn_fifo` / `send_fifo_cmd` 的命令语法（`=topic` / `.通知` / `:rpc`），把本讲 4.4 提到的 fifo 入口彻底讲透。

建议复习时回到 `examples/broker_custom_rpc.rs`，它是检验你是否真正理解「核心 RPC 客户端 = `.broker` 客户端 + RpcClient + 自动 processor」三件套的最佳标尺。
