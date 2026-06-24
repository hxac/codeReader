# FIFO 通道与 announce 事件

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 BUS/RT 的 **FIFO 通道** 是什么：它是一个被代理打开供读取的操作系统命名管道（named pipe），让纯 shell 脚本也能驱动代理发消息、发主题、做 RPC。
- 掌握 `send_fifo_cmd` 解析的 **四种命令语法**（普通消息 / `=主题` 发布 / `.通知` / `:方法` 调用），并理解它们最终都被「核心 RPC 客户端」代为发出。
- 理解 `spawn_fifo` **强依赖核心 RPC 客户端** 这一前置条件，未初始化时返回 `not_supported`。
- 理解 `BrokerEvent` 的三种语义（`reg` / `unreg` / `shutdown`）以及 `announce` 如何把它们发布到 `.broker/info` 与 `.broker/warn` 两个主题上。
- 能够参照 `examples/broker_custom_rpc.rs`，亲手用 `echo` 命令驱动一个嵌入 Broker 的 fifo，并观察 announce 事件。

## 2. 前置知识

本讲建立在两篇讲义之上，请先确认你已经理解：

- **u3-l1（创建 Broker 与注册内部客户端）**：你知道 `Broker` 是一个可嵌入的普通 Rust 值，`register_client` 注册内部客户端，内部客户端永不断线。
- **u5-l3（自定义 Broker RPC 与核心 RPC 接口）**：你知道代理会注册一个名为 `.broker` 的内部客户端，并把它连同 `RpcClient` 一起称为「核心 RPC 客户端」，存放在 `BrokerDb.rpc_client` 中；`init_default_core_rpc()` 一键挂内置处理器，`set_core_rpc_client()` 挂你自己的处理器。

本讲要做的事，本质是回答两个问题：

1. 「外部 shell 脚本怎么往这个 Rust 代理里塞命令？」→ **FIFO 通道**。
2. 「代理自身的状态变化（有客户端上线/下线/代理要关停）怎么通知所有客户端？」→ **announce 事件**。

二者看似无关，却共享同一个前提：**核心 RPC 客户端必须已初始化**。本讲会反复回到这一点。

几个背景术语回顾：

- **命名管道（FIFO / named pipe）**：Unix 的一种特殊文件，写入端写入的字节可被读取端按行读取，常用作 shell 与常驻进程之间的「命令入口」。
- **`.broker`**：代理进程自己注册的内部客户端名，既是一个点对点投递目标，也是一个可被订阅的普通客户端。
- **msgpack**：BUS/RT RPC 层默认的二进制序列化格式（见 u5-l1）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/broker.rs](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs) | 本讲主战场：`BrokerEvent`、`BrokerDb::announce`、`Broker::spawn_fifo`、`Broker::send_fifo_cmd` 全部在此。 |
| [src/common.rs](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/common.rs) | 提供 `now_ns()`，announce 用它给事件盖时间戳。 |
| [examples/broker_custom_rpc.rs](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/examples/broker_custom_rpc.rs) | 本讲实践任务的蓝本：挂自定义 RPC + 订阅 `#` + `spawn_fifo`。 |

> 说明：`BrokerEvent`、`spawn_fifo`、`send_fifo_cmd`、`BrokerDb::announce` 都带 `#[cfg(feature = "broker-rpc")]` 守卫，即它们需要 `broker-rpc` feature（含 `broker` + `rpc`）。`busrtd` 默认经 `server → broker-rpc` 自动具备。

---

## 4. 核心概念与源码讲解

### 4.1 BrokerEvent：announce 事件的三种语义

#### 4.1.1 概念说明

代理在运行中会产生一些「值得让客户端知道」的状态事件，例如：

- 某个客户端注册上线了（`reg`）；
- 某个客户端注销下线了（`unreg`）；
- 代理自身即将关停（`shutdown`）。

`BrokerEvent` 就是这些事件的**值对象**（value object）。它本身不发送任何东西，只是一个「装着事件描述的结构体」；真正把它发出去的是下一节的 `announce`。这样设计的好处是：事件描述与投递方式解耦——你可以用同一个 `BrokerEvent` 走代理内置的 publish 投递，也可以自定义处理。

#### 4.1.2 核心流程

`BrokerEvent` 的字段设计：

| 字段 | 含义 | 是否上线 |
| --- | --- | --- |
| `s` (subject) | 事件主题词，如 `"reg"` / `"unreg"` / `"shutdown"` | 是（msgpack 序列化） |
| `d` (data) | 附加数据，如客户端名；`shutdown` 时为 `None` | 是（`None` 时省略） |
| `t` (time) | 纳秒时间戳，发布时由 `announce` 填入 | 是 |
| `topic` | 该事件应发布到哪个主题，仅用于内部路由 | **否**（`#[serde(skip)]`） |

三个预设构造器分别把 `topic` 钉死：

- `reg(name)` / `unreg(name)` → 发布到 `.broker/info`
- `shutdown()` → 发布到 `.broker/warn`

关键点：`topic` 字段带有 `#[serde(skip)]`，所以它**不会出现在线上载荷里**，只是 `announce` 内部用来决定「往哪个主题 publish」。订阅者收到的 msgpack 载荷只有 `{"s","d","t"}`。

#### 4.1.3 源码精读

先看三个主题常量与代理自身客户端名，它们定义在 broker.rs 顶部、不受 feature 门控：

```rust
pub const BROKER_INFO_TOPIC: &str = ".broker/info";
pub const BROKER_WARN_TOPIC: &str = ".broker/warn";
pub const BROKER_NAME: &str = ".broker";
```

> [src/broker.rs:54-56](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L54-L56) — `.broker/info` 发布上下线，`.broker/warn` 发布关停告警。

`BrokerEvent` 结构体与三个构造器（注意 `topic` 的 `serde(skip)`）：

```rust
#[cfg_attr(feature = "broker-rpc", derive(Serialize, Deserialize))]
pub struct BrokerEvent<'a> {
    s: &'a str,
    #[cfg_attr(feature = "broker-rpc", serde(skip_serializing_if = "Option::is_none"))]
    d: Option<&'a str>,
    t: u64,
    #[cfg_attr(feature = "broker-rpc", serde(skip))]
    topic: &'a str,
}

impl<'a> BrokerEvent<'a> {
    pub fn shutdown() -> Self {
        Self { s: "shutdown", d: None, t: 0, topic: BROKER_WARN_TOPIC }
    }
    pub fn reg(name: &'a str) -> Self {
        Self { s: "reg", d: Some(name), t: 0, topic: BROKER_INFO_TOPIC }
    }
    pub fn unreg(name: &'a str) -> Self {
        Self { s: "unreg", d: Some(name), t: 0, topic: BROKER_INFO_TOPIC }
    }
    // new(s, d, topic) 可自定义事件
}
```

> [src/broker.rs:607-656](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L607-L656) — `BrokerEvent` 定义与构造器：`s/d/t` 上线，`topic` 仅作内部路由。

#### 4.1.4 代码实践

**目标**：用源码阅读理解 `BrokerEvent` 上线字段的边界。

1. 打开 [src/broker.rs:607-617](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L607-L617)。
2. 对比 `shutdown()` 与 `reg(name)`：`shutdown` 的 `d` 是 `None`，配合 `d` 字段上的 `serde(skip_serializing_if = "Option::is_none")`，思考 `shutdown` 事件序列化后的 msgpack 里是否还有 `d` 键。
3. **预期结果**：`shutdown` 载荷为 `{"s":"shutdown","t":...}`（无 `d` 键）；`reg` 载荷为 `{"s":"reg","d":"<name>","t":...}`。
4. 这一现象的精确输出**待本地验证**（可参考 4.2 实践订阅后打印）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `topic` 字段要标 `#[serde(skip)]`？

> **答**：`topic` 只是 `announce` 内部决定「发布到哪个主题」的路由标签，不属于事件内容本身。若它也被序列化，订阅者会收到一个冗余的、且容易与系统主题命名混淆的字段。

**练习 2**：如果你要新增一种「代理配置变更」的事件，应该发布到 `.broker/info` 还是 `.broker/warn`？参照现有约定说明理由。

> **答**：应发到 `.broker/info`。现有约定是：常规可观测状态（上下线）走 `info`，需要立即知晓的告警（关停）走 `warn`。配置变更属于常规可观测状态，故走 `info`；只有影响可用性的紧急变更才考虑 `warn`。

---

### 4.2 BrokerDb::announce 与 .broker 主题约定

#### 4.2.1 概念说明

`announce` 是把 `BrokerEvent` 真正投递出去的动作：它把事件用 msgpack 序列化，然后**通过核心 RPC 客户端** `publish` 到 `event.topic`。

这里有一个最重要的设计抉择：announce 不自己造一条发送链路，而是**复用核心 RPC 客户端**。也就是说，announce 发出的每一帧，**都是 `.broker` 这个客户端在发布**。这同时决定了 announce 的前置条件——如果核心 RPC 客户端还没初始化（`rpc_client` 为 `None`），announce 就是一个**静默的空操作**（直接返回 `Ok(())`），既不报错也不缓存。

#### 4.2.2 核心流程

`BrokerDb::announce` 的执行步骤：

```text
announce(event):
    加锁取 rpc_client
    若 rpc_client 为 None:
        直接返回 Ok(())        # 静默空操作
    否则:
        event.t = now_ns()      # 盖纳秒时间戳
        core_client.publish(event.topic, msgpack(event), QoS::No)
        返回 publish 的结果
```

触发 announce 的三个调用点：

| 触发点 | 事件 | 条件 | 源码位置 |
| --- | --- | --- | --- |
| `register_client` | `reg(name)` | 仅 primary 客户端（不含 `%%` 二级） | broker.rs:749-754 |
| `unregister_client` | `unreg(name)` | primary 且曾成功注册 | broker.rs:810-815 |
| 用户 / 服务端终止 | `shutdown()` | 显式调 `Broker::announce` | broker.rs:1389-1393 |

**主题约定与可达性**（这是实战中最关键的一点）：

- `.broker/info`（reg/unreg）：客户端**必须主动 `subscribe(".broker/info")`** 才能收到。
- `.broker/warn`（shutdown）：`insert_client` 在注册任何客户端时都会**自动** `subscribe(BROKER_WARN_TOPIC)`，所以**所有客户端零配置即可收到关停通知**。

#### 4.2.3 源码精读

`BrokerDb::announce`，核心 RPC 未设置时静默返回：

```rust
#[cfg(feature = "broker-rpc")]
async fn announce(&self, mut event: BrokerEvent<'_>) -> Result<(), Error> {
    if let Some(rpc_client) = self.rpc_client.lock().await.as_ref() {
        event.t = now_ns();
        rpc_client.client().lock().await.publish(
            event.topic,
            rmp_serde::to_vec_named(&event).map_err(Error::data)?.into(),
            QoS::No,
        ).await?;
    }
    Ok(())
}
```

> [src/broker.rs:707-724](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L707-L724) — announce 复用核心 RPC 客户端 publish；未初始化则空操作。

`now_ns` 用 `CLOCK_REALTIME` 取纳秒时间戳：

```rust
pub fn now_ns() -> u64 {
    let t = nix::time::clock_gettime(nix::time::ClockId::CLOCK_REALTIME).unwrap();
    t.tv_sec() as u64 * 1_000_000_000 + t.tv_nsec() as u64
}
```

> [src/common.rs:93-96](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/common.rs#L93-L96) — announce 用它给 `t` 字段填值。

触发点之一：`register_client` 在插入成功后，仅对 primary 客户端发 reg：

```rust
#[cfg(feature = "broker-rpc")]
if primary {
    if let Err(e) = self.announce(BrokerEvent::reg(&name)).await {
        error!("{}", e);
    }
}
```

> [src/broker.rs:749-754](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L749-L754) — 上线 reg，只针对 primary。对称的下线 unreg 在 [src/broker.rs:810-815](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L810-L815)。

每个客户端注册时自动订阅 `.broker/warn`，保证关停通知零配置可达：

```rust
sdb.subscribe(BROKER_WARN_TOPIC, &client);
```

> [src/broker.rs:780](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L780) — `insert_client` 内，所有客户端自动订阅 warn 主题。

对外暴露的薄封装 `Broker::announce`（用户/服务端用它发 shutdown）：

```rust
#[cfg(feature = "broker-rpc")]
pub async fn announce(&self, event: BrokerEvent<'_>) -> Result<(), Error> {
    self.db.announce(event).await
}
```

> [src/broker.rs:1389-1393](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1389-L1393) — 转发到 `BrokerDb::announce`。

#### 4.2.4 代码实践

**目标**：亲眼看到 announce 事件被发布。

1. 基于 [examples/broker_custom_rpc.rs](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/examples/broker_custom_rpc.rs)，把核心客户端的订阅从 `"#"` 改为显式订阅 `.broker/info`：

   ```rust
   core_client.subscribe(".broker/info", QoS::No).await?;
   ```

2. 启动该示例后，另开一个终端用 CLI 再连一个客户端（会触发 `reg`），例如 `./busrt -p /tmp/busrt.sock listen`（任意会注册一个客户端的命令即可）。
3. **需要观察的现象**：示例的 `handle_frame` 打印出一条来自 `.broker`、topic 为 `.broker/info` 的发布帧，载荷是 msgpack，解出来形如 `{"s":"reg","d":"<新客户端名>","t":<纳秒>}`。
4. **预期结果**：能稳定看到新客户端上线时的 reg 事件。精确客户端名与时间戳**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 announce 在核心 RPC 未初始化时选择「静默返回 Ok」而不是返回错误？

> **答**：announce 是注册/注销流程的副产物（见 `register_client` 里 `if let Err(e) = self.announce(...)` 只打 `error!` 不影响主流程）。若它返回错误并向上传播，会导致「没挂核心 RPC 的代理」连客户端注册都失败，破坏了「核心 RPC 可选」的设计。静默空操作让 announce 成为纯增益功能。

**练习 2**：一个只订阅了 `.broker/info` 的客户端，能否收到代理的 shutdown 通知？为什么？

> **答**：能。`insert_client` 会为每个客户端自动订阅 `.broker/warn`，与用户是否额外订阅 `.broker/info` 无关。shutdown 发布在 `.broker/warn`，故一定可达。

---

### 4.3 spawn_fifo：shell 可写入的命名管道入口

#### 4.3.1 概念说明

FIFO 通道是 BUS/RT 给 shell 脚本准备的「后门」：代理在文件系统里创建一个命名管道并打开读端，任何 shell 进程都能 `echo "命令" > /path/to/fifo` 把一行文本塞进去；代理逐行读取，把每行解析成一次操作（消息/发布/通知/RPC）并执行。

关键认知：**这些操作不是「写入方」在发，而是核心 RPC 客户端（`.broker`）在发**。换句话说，fifo 让外部 shell 脚本能「以代理自己的身份」发出消息与 RPC。这也直接决定了它的前置条件——必须有核心 RPC 客户端，否则 `spawn_fifo` 立刻返回 `Err(not_supported)`。

#### 4.3.2 核心流程

`spawn_fifo` 的启动流程：

```text
spawn_fifo(path, buf_size):
    若核心 RPC 客户端为 None:
        返回 Err(not_supported("broker core RPC client not initialized"))
    删除可能残留的同名旧文件
    创建命名管道 path（权限 0o622）
    以读端打开 path
    spawn 一个常驻任务：
        loop:
            逐行读取，每行调 send_fifo_cmd(rpc_client, line)
            读到 EOF（所有写端关闭）后 sleep 100ms 再循环重试
    把任务 JoinHandle 存入 self.services
```

一个重要的实现细节：命名管道在所有写端关闭后，读端会收到 EOF（`next_line` 返回 `None`），但这不代表管道作废——新的写端再次打开时同一读端能继续读到新数据。所以这里的 `sleep(100ms)` 循环不是「重连」，而是为了避免在连续 EOF 时忙等空转。

#### 4.3.3 源码精读

`spawn_fifo` 的文档注释直接列出了四种语法，这是本讲最重要的速查表：

```rust
/// Broker fifo channel is useful for shell scripts and allows to send:
///
/// echo TARGET MESSAGE > /path/to/fifo              # a one-to-one or broadcast message
/// echo '=TOPIC' MESSAGE                            # publish to a topic
/// echo TARGET .MESSAGE                             # RPC notification
/// echo TARGET :method param=value param=value      # RPC call, the payload will be sent as msgpack
///
/// Requires rpc feature + broker core rpc client to be set
```

> [src/broker.rs:1615-1623](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1615-L1623) — 四种命令语法的官方说明。

前置条件检查与管道创建：

```rust
let rpc_client = self.db.rpc_client.clone();
if rpc_client.lock().await.is_none() {
    return Err(Error::not_supported(BROKER_RPC_NOT_INIT_ERR));
}
let _r = tokio::fs::remove_file(path).await;
unix_named_pipe::create(path, Some(0o622))?;
// chown fifo as it's usually created with 644
tokio::fs::set_permissions(path, std::fs::Permissions::from_mode(0o622)).await?;
let fd = unix_named_pipe::open_read(path)?;
```

> [src/broker.rs:1626-1636](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1626-L1636) — 未初始化核心 RPC 直接报错；建管道权限 0o622，再以**读端** `open_read` 打开（注意会先 `remove_file` 清理残留文件，故 path 不能指向你不想被删的文件）。

常驻读取循环（EOF 后 100ms 重试）：

```rust
let service = tokio::spawn(async move {
    let reader = BufReader::with_capacity(buf_size, f);
    let mut lines = reader.lines();
    let sleep_step = Duration::from_millis(100);
    loop {
        while let Some(line) = match lines.next_line().await { Ok(v) => v, Err(_) => None } {
            if let Err(e) = Self::send_fifo_cmd(&rpc_client, line).await {
                error!("{}: {}", socket_path, e);
            }
        }
        tokio::time::sleep(sleep_step).await;
    }
});
self.services.push(service);
```

> [src/broker.rs:1638-1659](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1638-L1659) — 逐行分发到 `send_fifo_cmd`；任务句柄存入 `services`。

未初始化时的错误文案常量：

```rust
const BROKER_RPC_NOT_INIT_ERR: &str = "broker core RPC client not initialized";
```

> [src/broker.rs:59](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L59) — `spawn_fifo` 与 `send_fifo_cmd` 共用的报错文案。

#### 4.3.4 代码实践

**目标**：验证「未初始化核心 RPC 时 spawn_fifo 会失败」。

1. 写一个最小嵌入程序：`Broker::new()` 后**不**调用 `init_default_core_rpc()` / `set_core_rpc_client()`，直接 `broker.spawn_fifo("/tmp/x.fifo", 8192).await`。
2. **需要观察的现象**：`spawn_fifo` 返回错误，错误文案为 `broker core RPC client not initialized`，且 `/tmp/x.fifo` 未被创建。
3. **预期结果**：与源码 1627-1629 行的 `Error::not_supported(BROKER_RPC_NOT_INIT_ERR)` 一致。精确的 `Error` 显示形式**待本地验证**。
4. 对比：加上 `broker.init_default_core_rpc().await?;` 后再 `spawn_fifo`，应成功。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `spawn_fifo` 在创建管道前要先 `tokio::fs::remove_file(path)`？

> **答**：命名管道的路径若已存在（上次运行的残留，或被误创建为普通文件），`unix_named_pipe::create` 会因路径已占用而失败。先删后建保证幂等启动。副作用是：**不要把 path 指向有价值的现存文件**，它会被无条件删除。

**练习 2**：读取循环里的 `sleep(100ms)` 起什么作用？去掉会怎样？

> **答**：命名管道在所有写端关闭后读端收到 EOF，去掉 sleep 会让 `loop` 在没有写端时进入忙等空转，浪费 CPU。100ms 的间隔让代理在等待新写端时近乎空闲。

---

### 4.4 send_fifo_cmd：四种命令语法解析

#### 4.4.1 概念说明

`send_fifo_cmd` 是 fifo 的「大脑」：它把一行文本解析成一次操作。全部操作都通过传入的核心 RPC 客户端发出——普通消息/广播/主题发布用其底层 `AsyncClient`（`.client()`），通知/调用用其 `Rpc` 能力（`notify`/`call0`）。

理解四种语法的关键，是抓住「第一个 token 是什么、第二个 token 以什么开头」这两个判据。

#### 4.4.2 核心流程

决策树（按源码顺序）：

```text
line = 一行文本
cmd  = line.trim()

if cmd 以 '=' 开头:                    # ① 主题发布
    tokens = cmd 去掉 '=' 后 split(' ')
    topic   = tokens[0]
    payload = tokens[1]
    core.publish(topic, payload, QoS::No)

else:                                  # 其余三种：target + payload
    tokens = line split(' ')           # 注意：用的是原始 line，不是 cmd
    target  = tokens[0]
    payload = tokens[1]

    if payload 以 '.' 开头:             # ② RPC 通知
        method = payload 去掉 '.'
        core.notify(target, method, QoS::No)

    elif payload 以 ':' 开头:           # ③ RPC 调用(call0)
        method = payload 去掉 ':'
        params = str_to_params_map(tokens[2..])   # key=value 对
        core.call0(target, method, msgpack(params), QoS::No)

    else:                               # ④ 普通消息
        if target 含 '*' 或 '?':        #     广播
            core.send_broadcast(target, payload, QoS::No)
        else:                           #     点对点
            core.send(target, payload, QoS::No)
```

两个容易踩的细节：

1. **trim 的不对称**：`=` 分支用的是 `cmd`（已 trim），而普通消息分支用的是原始 `line`（未 trim）。这意味着行首若有空格，`=` 分支能正确解析，但普通消息分支会把空串当成 `target` 报错。
2. **载荷是单 token**：除 `:method` 外，`payload` 只取第二个空格分隔后的**一个** token。例如 `echo .broker hello world`，`world` 会被丢弃，载荷只是 `hello`。多词载荷需自行约定编码。

关于 `:method` 的 call0：它在 RPC 协议里 `id=0`，表示「不需要回复」（见 u5-l1/u5-l2）。处理器仍会执行 `handle_call`（副作用照常发生），但**代理不会回送任何结果帧**。所以 fifo 的 `:method` 是「触发即忘」的调用。

#### 4.4.3 源码精读

`=` 前缀 → publish：

```rust
if let Some(s) = cmd.strip_prefix('=') {
    let mut sp = s.split(' ');
    let topic = sp.next().ok_or_else(|| Error::data("topic not specified"))?;
    let payload = sp.next().ok_or_else(|| Error::data("payload not specified"))?;
    rpc.client().lock().await.publish(topic, payload.as_bytes().into(), QoS::No).await?;
}
```

> [src/broker.rs:1672-1685](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1672-L1685) — `=topic payload` 发布主题。

普通消息分支（含 `.`/`:` 前缀判定与广播判定）：

```rust
let mut sp = line.split(' ');
let target = sp.next().ok_or_else(|| Error::data("target not specified"))?;
let payload = sp.next().ok_or_else(|| Error::data("payload not specified"))?;

if let Some(s) = payload.strip_prefix('.') {                 // 通知
    rpc.notify(target, s.as_bytes().into(), QoS::No).await?;
} else if let Some(method) = payload.strip_prefix(':') {     // call0
    let s = sp.collect::<Vec<&str>>();
    let params = crate::common::str_to_params_map(&s)?;
    rpc.call0(target, method, rmp_serde::to_vec_named(&params).map_err(Error::data)?.into(), QoS::No).await?;
} else if target.contains(&['*', '?'][..]) {                 // 广播
    rpc.client().lock().await.send_broadcast(target, payload.as_bytes().into(), QoS::No).await?;
} else {                                                     // 点对点
    rpc.client().lock().await.send(target, payload.as_bytes().into(), QoS::No).await?;
}
```

> [src/broker.rs:1687-1729](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1687-L1729) — 四语法中后三种的判定与分发：`.` 通知、`:` call0（参数经 `str_to_params_map` 转 msgpack）、含通配符则广播否则点对点。

注意 `:method` 的参数解析复用了 CLI 也用的 `str_to_params_map`（见 u8-l2），把 `key=value` 对解析成带类型的 map 后再 msgpack 序列化，与 RPC 层的载荷契约一致。

#### 4.4.4 代码实践

**目标**：用 `echo` 驱动 fifo 的四种语法，并在示例的回调里观察到效果。

以 [examples/broker_custom_rpc.rs](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/examples/broker_custom_rpc.rs) 为蓝本（它已 `set_core_rpc_client` + `core_client.subscribe("#")` + `spawn_fifo("/tmp/busrt.fifo", 8192)`，且三个回调都会 `println!`）。启动它后，在另一终端执行：

```bash
# ④ 点对点消息（target=.broker，无通配符 → send）
echo '.broker hello' > /tmp/busrt.fifo

# ① 主题发布（= 前缀 → publish）
echo '=news/tech hi' > /tmp/busrt.fifo

# ② RPC 通知（payload 以 . 开头 → notify）
echo '.broker .greet' > /tmp/busrt.fifo

# ③ RPC 调用（payload 以 : 开头 → call0，msgpack 参数）
echo '.broker :ping message=world' > /tmp/busrt.fifo
```

**需要观察的现象**（对照示例回调）：

| 命令 | 命中回调 | 预期打印要点 |
| --- | --- | --- |
| `.broker hello` | `handle_frame` | 来自 `.broker` 的 `Message` 帧，topic 为 `None`，载荷 `hello` |
| `=news/tech hi` | `handle_frame` | topic 为 `news/tech`，载荷 `hi` |
| `.broker .greet` | `handle_notification` | 来自 `.broker` 的通知，方法/载荷 `greet` |
| `.broker :ping message=world` | （call0，`handle_call` 执行但**无回复**） | ping 处理器返回 `world`，但调用方收不到回包；无新打印（除非你在 handler 内加日志） |

**预期结果**：前三条都能在示例输出里看到对应打印；第四条因 call0 不回包而「静默成功」。精确输出格式**待本地验证**。

> 对比：若想看到 RPC 的**回复**，不要用 fifo（它只能 call0），改用 `busrt` CLI 的 `rpc call`（见 u8-l2），它用带 `id>0` 的 `call` 并打印回复，例如 `busrt rpc call .broker ping message=world` 应打印 `world`。

#### 4.4.5 小练习与答案

**练习 1**：命令 `echo 'worker.* hello' > /tmp/busrt.fifo` 走的是哪条分支？为什么不需要 `=` 或 `.`/`:` 前缀？

> **答**：走「普通消息」分支里的**广播**子分支。因为 target `worker.*` 不以 `=` 开头，payload `hello` 也不以 `.`/`:` 开头，但 target 含通配符 `*`，命中 `target.contains(&['*', '?'][..])`，故调 `send_broadcast`。这正是文档注释里「a one-to-one **or broadcast** message」的广播情形。

**练习 2**：为什么 `echo '.broker :ping message=world'` 之后，写入方拿不到 ping 的返回值？如何才能拿到？

> **答**：fifo 的 `:method` 用的是 `call0`，其 RPC `id=0` 表示「不需要回复」，处理器执行了 `handle_call` 但不回送结果帧，所以写入方（一个管道写端）无法接收返回值。要拿到返回值，必须用带 `id>0` 的 `call`，即通过 `busrt` CLI 的 `rpc call` 或编程式 `RpcClient` 的 `call()`（见 u5-l2）。

---

## 5. 综合实践

把本讲四个模块串起来：搭建一个「带核心 RPC + fifo + announce 观测」的嵌入代理，并用 shell 全程驱动。

1. 复制 [examples/broker_custom_rpc.rs](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/examples/broker_custom_rpc.rs) 为新文件，做两处改造：
   - 让核心客户端**同时**订阅 `#`（接收发布帧）与 `.broker/info`（接收 reg/unreg announce）。可分两次 `subscribe` 调用。
   - 保留 `set_core_rpc_client` 与 `spawn_fifo("/tmp/busrt.fifo", 8192)`。
2. 启动该程序，确认输出 `Waiting for frames to .broker`。
3. 在另一终端，依次执行并观察：

   ```bash
   # (a) 经 fifo 让 .broker 发点对点消息
   echo '.broker from-fifo' > /tmp/busrt.fifo

   # (b) 经 fifo 发布主题
   echo '=sensors/temp 23.5' > /tmp/busrt.fifo

   # (c) 用 CLI 连入一个新客户端，触发 announce(reg)
   ./busrt -p /tmp/busrt.sock listen &

   # (d) 经 fifo 做一次 RPC 调用（call0，注意拿不到回复）
   echo '.broker :ping message=hey' > /tmp/busrt.fifo
   ```

4. **串联验证点**：
   - (a)(b) 验证 4.4 的四语法解析（消息走 `handle_frame`）。
   - (c) 验证 4.2 的 announce：你应在 `.broker/info` 订阅端看到一条 msgpack 的 `reg` 事件（解出 `s="reg"`, `d=<cli 客户端名>`）。
   - (d) 验证 4.4 的 call0：处理器执行但无回复；对比 `./busrt rpc call .broker ping message=hey` 能拿到 `hey`。
5. 优雅终止本程序（Ctrl+C 或其自身循环退出），观察它若调用了 `Broker::announce(BrokerEvent::shutdown())`，所有连入的客户端（如 (c) 的 listen）都会经 `.broker/warn` 收到关停通知。

**预期结果**：你能用纯 shell 命令完成「发消息、发主题、触发上下线事件、做 RPC」，并清晰区分 fifo 的 call0（无回复）与编程式 call（有回复）。所有具体输出**待本地验证**。

## 6. 本讲小结

- **FIFO 通道**是代理创建的命名管道读端，供 shell 脚本 `echo` 一行命令驱动代理；所有命令最终都由**核心 RPC 客户端 `.broker`** 代为发出。
- `spawn_fifo` **强依赖核心 RPC 客户端**：未初始化时立刻返回 `Err(not_supported("broker core RPC client not initialized"))`，不会创建管道。
- `send_fifo_cmd` 用四种语法解析一行：`=` 前缀→publish、`.` 前缀→notify、`:` 前缀→call0（参数 `key=value` 经 `str_to_params_map` 转 msgpack）、其余→普通消息（target 含 `*`/`?` 则广播，否则点对点）。
- fifo 的 `:method` 是 **call0（`id=0`）**，处理器执行但不回包；要拿返回值得用 CLI 的 `rpc call` 或编程式 `call`。
- `BrokerEvent` 三语义：`reg`/`unreg`→`.broker/info`，`shutdown`→`.broker/warn`；`topic` 字段 `serde(skip)`，仅作内部路由，载荷只含 `{"s","d","t"}`。
- `announce` 复用核心 RPC 客户端 publish；核心 RPC 未设置时**静默空操作**；`.broker/warn` 被所有客户端自动订阅（关停零配置可达），`.broker/info` 需主动订阅。

## 7. 下一步学习建议

- **多语言绑定（u8-l4）**：fifo 是 Rust/shell 侧的入口，Python/JS 绑定则是另一条「跨语言驱动代理」的路径，可对比两者如何复用同一套二进制协议与 `.broker` 主题。
- **回看 u5-l2 / u5-l3**：本讲多次提到 call0 不回包、`.broker` 既是投递目标又是 RPC 服务端，这些机制都在 RPC 层讲清了；带着 fifo 的实践体验重读会有更深理解。
- **动手扩展**：尝试在自定义核心 RPC 处理器里加一个 `shell.exec` 方法，再经 fifo 的 `:shell.exec cmd=...` 触发，体会「fifo + 核心 RPC」组合成轻量运维通道的玩法（注意安全：fifo 权限 0o622，任何本机用户可写）。
