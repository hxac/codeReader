# 打开一个 Session

## 1. 本讲目标

学完本讲，你应当能够：

- 用 `zenoh::open(config)` 打开一个 Zenoh 会话（`Session`），并理解它返回的是一个「构造器（builder）」。
- 理解 `Session` 在底层是一个 `Arc` 句柄：它很便宜、可以克隆、可以跨线程共享，并且会在所有克隆都被释放时自动关闭。
- 区分「会话启动前在 `Config` 上改配置」和「会话启动后通过运行时 `Notifier` 改配置」两种方式的关键差异。
- 会用 `session.zid()` / `session.info()` 读取本节点的 ZenohId，以及当前连接到的 router / peer 列表。
- 搞清楚 Zenoh 节点的「角色（WhatAmI）」是什么，以及为什么公开 API 里没有一个直接的「读取本端角色」的方法。

本讲是第二单元《核心概念：Session 与 Key Expression》的第一讲，承接 [u1-l2 构建、运行与第一个示例](u1-l2-build-run-examples.md)——你已经能把 `z_pub`/`z_sub` 跑起来，本讲我们正式拆解「打开会话」这一步在源码里到底发生了什么。

## 2. 前置知识

在继续之前，请确保你已经了解（来自第一单元）：

- **Zenoh 的基本使用范式**：`zenoh::open(config)` 得到一个 `Session`，在它上面声明 `Publisher`/`Subscriber` 等实体（见 [u1-l2](u1-l2-build-run-examples.md)）。
- **builder 模式与 `Resolvable`/`Resolve`/`Wait` 三大 trait**：Zenoh 里几乎所有「创建实体」的调用返回的都不是实体本身，而是一个 builder；你必须用 `.await`（异步）或 `.wait()`（同步）把它「解析（resolve）」成真正的实体（见 [u1-l4 公开 API 地图](u1-l4-public-api-map.md)）。本讲的 `zenoh::open(...)` 正是这样一个 builder。
- **稳定 vs 内部 crate 的边界**：`zenoh` 是对外稳定 API，而 `commons/*`、`io/*` 是内部实现（见 [u1-l3](u1-l3-repo-crate-layout.md)）。本讲会偶尔点一下 `Session` 内部委托给了哪个 net 层结构，但不会深入。

另外补充两个本讲会用到的 Rust 概念（不熟的读者看一眼即可）：

- **`Arc<T>`**：线程安全的引用计数指针。多个 `Arc` 指向同一份堆上的 `T`，克隆一个 `Arc` 只是增加引用计数（非常便宜），只有当最后一个 `Arc` 被释放时，`T` 才会被真正销毁。理解这一点，你就能理解为什么 `Session` 可以随便克隆共享。
- **`Option<T>`**：Rust 里表示「可能没有值」的类型，`Some(x)` 表示有值，`None` 表示没有。Zenoh 的配置里很多字段都是 `Option`，比如节点角色 `mode` 不写就是 `None`。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [zenoh/src/api/session.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs) | `Session` 结构体定义、`Clone`/`Drop` 实现、`close()`/`zid()`/`info()` 等方法，以及根级 `open()` 函数的所在地。 |
| [zenoh/src/api/builders/session.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/builders/session.rs) | `open()` 返回的 `OpenBuilder`，以及它如何 resolve 成 `Session`。 |
| [zenoh/src/api/config.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/config.rs) | 公开的 `Config` 包装类型，提供 `from_file`/`from_json5`/`insert_json5`/`get_json` 等方法。 |
| [commons/zenoh-config/src/lib.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-config/src/lib.rs) | `Config` 的真正实现（内部 crate），定义了 `mode` 字段、`set_mode` 等访问器。 |
| [commons/zenoh-protocol/src/core/whatami.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/core/whatami.rs) | `WhatAmI` 枚举（Router/Peer/Client）的定义。 |
| [zenoh/src/api/info.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/info.rs) | `SessionInfo` 及其 `zid()`/`routers_zid()`/`peers_zid()`/`transports()` 方法。 |
| [examples/examples/z_info.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/examples/examples/z_info.rs) | 官方示例：打开会话并打印 zid / routers / peers 等信息。 |

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 open / Session**：打开会话、Session 的 Arc 句柄语义与生命周期。
- **4.2 Config**：配置对象，以及「启动前改」与「启动后改」的差异。
- **4.3 会话信息查询**：读取 zid、router/peer 列表与节点角色。

### 4.1 open / Session

#### 4.1.1 概念说明

`Session` 是 Zenoh 的「主组件」。它持有 Zenoh 的 **runtime**（运行时），而 runtime 维护着本节点到整个 Zenoh 网络的连接状态。你在 Session 上声明的所有实体（`Publisher`、`Subscriber`、`Querier`、`Queryable`……）都靠它来运转。你可以把它粗略理解成「一条到 Zenoh 网络的逻辑连接 + 一张本地实体表」。

创建 Session 的唯一公开入口是根级函数 `zenoh::open(config)`。注意：它**不直接返回 `Session`**，而是返回一个 `OpenBuilder`。这是 Zenoh 一贯的 builder 风格——你必须把 builder「resolve」一下，才会真正得到 `Session`。resolve 有两种等价方式：

- 异步：`let session = zenoh::open(config).await.unwrap();`
- 同步：`let session = zenoh::open(config).wait().unwrap();`（`wait` 来自 `Wait` trait）

无论哪种，最终拿到的都是 `ZResult<Session>`（即 `Result<Session, zenoh::Error>`）。

#### 4.1.2 核心流程

打开一个 Session 的过程可以概括为：

1. 调用 `zenoh::open(config)`，得到 `OpenBuilder`。
2. `.await`（或 `.wait()`）触发 `OpenBuilder` 的 resolve：先把 `config` 收敛成 `zenoh::Config`，再调用 `Session::new(...)`。
3. `Session::new` 内部启动 runtime（建链、scouting、路由等——这些是后续单元的内容），并用一个 `Arc<SessionInner>` 包裹出最终的 `Session`。
4. 之后你就可以在 `&Session` 上 `declare_publisher` / `declare_subscriber` 等等。
5. 关闭会话：要么主动 `session.close().await`，要么干脆把所有 `Session` 克隆都释放掉（`Drop` 会自动关闭）。

用伪代码表示：

```
zenoh::open(config)            // -> OpenBuilder
    .await                     // OpenBuilder::wait(): config -> Config, 然后 Session::new(config)
    .unwrap()                  // -> Session (内部 = Arc<SessionInner>)

session.declare_subscriber(..) // 在 Session 上声明实体
session.close().await          // 主动关闭（可选；全部克隆 drop 时也会自动关闭）
```

#### 4.1.3 源码精读

**根级 `open` 函数**：它只是一个薄壳，构造并返回 `OpenBuilder`。注意它对参数的要求是 `TryInto<Config>`——也就是说你不一定要先造好一个 `Config`，任何能转换成 `Config` 的东西（比如 `Config` 本身、甚至某些内部类型）都可以传。

[zenoh/src/api/session.rs:3543-3549](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L3543-L3549) —— 根级 `open` 函数定义，`config` 参数约束为 `TryInto<crate::config::Config>`。

```rust
pub fn open<TryIntoConfig>(config: TryIntoConfig) -> OpenBuilder<TryIntoConfig>
where
    TryIntoConfig: std::convert::TryInto<crate::config::Config> + Send + 'static,
    ...
{
    OpenBuilder::new(config)
}
```

**`OpenBuilder` 如何 resolve 成 `Session`**：`Resolvable::To = ZResult<Session>` 说明 await/wait 的产物是「可能出错的 `Session`」。`Wait::wait` 里先把 config 收敛，再调 `Session::new(config)`；`IntoFuture` 让 `.await` 走通。

[zenoh/src/api/builders/session.rs:94-100](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/builders/session.rs#L94-L100) —— `OpenBuilder` 的 resolve 目标类型是 `ZResult<Session>`。

[zenoh/src/api/builders/session.rs:102-119](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/builders/session.rs#L102-L119) —— 同步 `wait()` 的实现：`self.config.try_into()` 收敛配置，再交给 `Session::new(config)`。

```rust
fn wait(self) -> <Self as Resolvable>::To {
    let config: crate::config::Config = self
        .config
        .try_into()
        .map_err(|e| zerror!("Invalid Zenoh configuration {:?}", &e))?;
    Session::new(config, /* ... */).wait()
}
```

> 这段也顺带回答了一个常见疑问：`#[must_use]` 加在 `OpenBuilder` 上，意思是「如果你只写 `zenoh::open(config)` 却不 `.await`/`.wait()`，它什么都不会做」——和第一单元讲过的 `Resolvable` trait 的 `#[must_use]` 行为一致。

**`Session` 本质是一个 `Arc`**：看它的结构定义——`#[repr(transparent)] pub struct Session(Arc<SessionInner>);`。也就是说 `Session` 在内存里就是一个指向 `SessionInner` 的 Arc 指针。

[zenoh/src/api/session.rs:744-746](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L744-L746) —— `Session` 定义，内部就是 `Arc<SessionInner>`。

`SessionInner` 才是真正持有状态的地方：runtime（连接状态）、`state: RwLock<SessionState>`（你声明的实体表）、一个 `strong_counter`（用来判断「我是不是最后一个克隆」）等等。

[zenoh/src/api/session.rs:679-688](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L679-L688) —— `SessionInner` 字段，包含 runtime、`SessionState`、`strong_counter` 等。

**克隆很便宜**：`Clone` 实现只是 `Arc::clone`（加一个原子计数），所以你可以放心地把 `Session` 克隆多份，分别交给不同的 tokio 任务/线程。

[zenoh/src/api/session.rs:770-775](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L770-L775) —— `impl Clone for Session`，本质是克隆内部 `Arc` 并递增 `strong_counter`。

**自动关闭**：更巧妙的是 `Drop`——当 `strong_counter` 归零（即最后一个 `Session` 克隆被释放），会自动调用 `self.close().wait()`。这意味着你**即使忘了手动 close，只要让 `Session` 离开作用域，它也会被正确关闭**。

[zenoh/src/api/session.rs:777-785](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L777-L785) —— `impl Drop for Session`，最后一个克隆释放时自动 close。

**手动关闭 `close()`**：返回的是一个 `CloseBuilder`（又是 builder 模式），所以要 `.await`。关闭后，该会话上声明的所有 subscriber/queryable 都会停止收数据，再发布/查询会报错。

[zenoh/src/api/session.rs:945-947](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L945-L947) —— `Session::close` 返回 `CloseBuilder`，配合 `.await` 完成（同步）关闭。

**`zid()` 快捷方式**：直接返回本会话的 `ZenohId`（同步、不返回 builder）。`ZenohId` 是 Zenoh 网络里每个节点的唯一标识（最多 16 字节）。

[zenoh/src/api/session.rs:893-897](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L893-L897) —— `Session::zid` 是 `session.info().zid()` 的便捷快捷方式。

官方文档对 `Session` 的「可克隆 Arc + 实体生命周期」有一段很清晰的说明，建议直接读：

[zenoh/src/api/session.rs:700-713](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L700-L713) —— `Session` 的文档注释：可克隆、克隆是 Arc、关闭会话会停掉所有实体。

#### 4.1.4 代码实践

**实践目标**：亲手打开一个会话，打印它的 ZenohId，睡 3 秒后关闭，验证 Session 能正常创建与销毁。

**操作步骤**：

1. 在 `examples` crate 里仿照 `z_info.rs` 新建一个 example（或在一个独立的 cargo 项目里依赖 `zenoh`）。
2. 写入下面的「示例代码」（**注意：这是为本讲编写的示例，不是仓库原有代码**）：

```rust
// 示例代码：z_session_open.rs
#[tokio::main]
async fn main() {
    // 1. 用默认配置打开会话
    let session = zenoh::open(zenoh::Config::default()).await.unwrap();

    // 2. 打印本节点的 ZenohId
    println!("我的 zid = {}", session.zid());

    // 3. 睡眠 3 秒（模拟「做点什么」）
    tokio::time::sleep(std::time::Duration::from_secs(3)).await;

    // 4. 主动关闭会话
    session.close().await.unwrap();
    println!("会话已关闭，is_closed = {}", session.is_closed());
}
```

若把它作为 examples crate 的新 example，编译运行（参考 [u1-l2](u1-l2-build-run-examples.md) 的命令格式）：

```bash
cargo run --example z_session_open
```

若是独立 cargo 项目，`Cargo.toml` 至少需要（版本按你本地 zenoh 对齐）：

```toml
[dependencies]
zenoh = "1"              # 与仓库版本一致
tokio = { version = "1", features = ["full"] }
```

**需要观察的现象**：

- 第 2 步会打印一串形如 `e0...` 的 hex 串，那就是本会话的 `ZenohId`。**每次重新运行，这个 id 都会不同**——因为默认配置不固定 `id`，Zenoh 会随机生成一个 u128（见后文 4.2 节 `id` 字段说明）。
- 程序会停顿约 3 秒，然后打印 `会话已关闭，is_closed = true`。

**预期结果**：程序无报错地打开、打印 zid、停顿、关闭。

> 待本地验证：如果你用的是较旧的 tokio，`sleep` 需确认开启了 `time` feature（用 `features = ["full"]` 最省事）。具体能否编译取决于你本地的 zenoh 版本号。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `session.close().await.unwrap();` 这一行删掉，会发生什么？会话还会被关闭吗？

**参考答案**：会。因为 `Session` 的 `Drop` 实现会在最后一个克隆被释放时自动调用 `close().wait()`。`main` 结束时 `session` 离开作用域被 drop，会话照样会被关闭。差别只是：手动 `close()` 是异步且可拿到关闭错误的；自动 drop 走的是同步 `wait()`，关闭错误只会被 `tracing::error!` 记录而不会返回给你。

**练习 2**：下面两段代码，`s1` 和 `s2` 指向的是同一个底层会话吗？

```rust
let s1 = zenoh::open(zenoh::Config::default()).await.unwrap();
let s2 = s1.clone();
```

**参考答案**：是的。`Session` 内部是 `Arc<SessionInner>`，`clone()` 只是把 Arc 计数 +1，`s1` 和 `s2` 共享同一份 runtime 和实体表。在 `s1` 上声明的 subscriber，`s2` 也能看到。`Session` 甚至实现了 `PartialEq`（`Arc::ptr_eq`），你可以用 `s1 == s2` 验证它们是同一个会话。

---

### 4.2 Config

#### 4.2.1 概念说明

`open(config)` 里的 `config` 决定了这个会话「以什么角色、监听/连接哪些端点、开启哪些特性」运行。Zenoh 的配置**故意不暴露内部字段**，只能通过几种方式整体或增量地读写它，以保持配置结构在版本间可以自由演进。这是 Zenoh 维持稳定边界的又一个体现（见 [u1-l4](u1-l4-public-api-map.md)）。

公开的 `zenoh::Config` 是对内部 `zenoh_config::Config` 的一层包装：

```rust
pub struct Config(pub(crate) zenoh_config::Config);
```

它提供的主要方法有：

| 方法 | 作用 |
| --- | --- |
| `Config::default()` | 得到一份默认配置（`#[derive(Default)]`）。 |
| `Config::from_file(path)` | 从 JSON5/YAML 文件加载。 |
| `Config::from_json5(s)` | 从 JSON5 字符串加载。 |
| `Config::from_env()` | 从 `ZENOH_CONFIG` 环境变量指向的文件加载。 |
| `Config::insert_json5(key, value)` | 在配置树的某个 `key` 上写入一个 JSON5 值（**会话启动前**用）。 |
| `Config::get_json(key)` | 读取某个 `key` 的 JSON 表示。 |
| `Config::remove(key)` | 删除某个 `key`。 |

其中节点角色由顶层的 `mode` 字段控制，取值是 `"router"` / `"peer"` / `"client"` 三选一，对应 `WhatAmI` 枚举。

#### 4.2.2 核心流程

读写配置有两种典型场景，**它们用的方法不一样，务必分清**：

- **场景 A：会话启动前**——你手里有一个 `Config` 值，可以随便用 `insert_json5` / `get_json` 改任意 key（包括 `mode`、`listen`、`connect` 等），然后再 `open(config)`。这是最常见的用法。
- **场景 B：会话启动后**——你已经 `open` 出 `Session`，想运行时改配置。这时要通过 `session.config()`（`unstable`）拿到一个 `Notifier`，而**它的 `insert_json5` 只允许改以 `plugins/` 开头的 key**（因为运行时改网络相关配置是不安全的）。

这一点是初学者最容易踩的坑：在运行时 `Notifier` 上 `insert_json5("mode", "client")` 会直接报错。所以「改 mode」必须、也只能在**启动前**的 `Config` 上做。

```
启动前：Config::default() --insert_json5--> open(config)   ✓ 可改任意 key
启动后：session.config() (Notifier) --insert_json5-->       ✗ 仅允许 plugins/* key
```

#### 4.2.3 源码精读

**`Config` 是个包装类型，字段私有**：注释明确说「配置是不稳定的，所以不直接暴露字段，只能通过文件/JSON5/`insert_json5` 读写」。

[zenoh/src/api/config.rs:43-44](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/config.rs#L43-L44) —— `Config` 定义：`pub struct Config(pub(crate) zenoh_config::Config);`，带 `#[derive(Default)]`。

**`insert_json5` / `get_json`（启动前，任意 key）**：直接委托给内部 `zenoh_config::Config`。

[zenoh/src/api/config.rs:82-99](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/config.rs#L82-L99) —— `Config::insert_json5` 与 `Config::get_json`，无 key 前缀限制。

```rust
pub fn insert_json5(&mut self, key: &str, value: &str) -> ZResult<()> {
    self.0.insert_json5(key, value).map_err(|err| zerror!("{err}").into())
}
pub fn get_json(&self, key: &str) -> ZResult<String> {
    self.0.get_json(key).map_err(|err| zerror!("{err}").into())
}
```

**运行时 `Notifier` 的 `insert_json5`（启动后，仅 `plugins/`）**：注意开头那句 `ensure_config_key_is_dynamically_writable(key)?`——它就是那个「只允许 `plugins/`」的关卡。

[zenoh/src/api/config.rs:179-188](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/config.rs#L179-L188) —— `ensure_config_key_is_dynamically_writable`：非 `plugins/` 开头的 key 直接 `bail!` 报错。

[zenoh/src/api/config.rs:258-263](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/config.rs#L258-L263) —— 运行时 `Notifier::insert_json5`，先做上面的 key 检查再写入并 `notify`。

**`mode` 字段与角色**：在内部 `zenoh_config::Config` 里，`mode` 是个 `Option<WhatAmI>`——不写就是 `None`，运行时 runtime 会把它补成默认值（`Peer`）。

[commons/zenoh-config/src/lib.rs:522-523](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-config/src/lib.rs#L522-L523) —— `mode` 字段：`mode: Option<whatami::WhatAmI>`，注释说明 `"router"` 是 `zenohd` 的默认、`"peer"`/`"client"` 另两种。

**`WhatAmI` 枚举**：三种节点角色。注意它们是用位掩码表示的（`0b001`/`0b010`/`0b100`），这样可以用按位或 `|` 组合成 `WhatAmIMatcher`（在 scouting 那一讲会用到）。`Peer` 是 `#[default]`。

[commons/zenoh-protocol/src/core/whatami.rs:38-45](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/core/whatami.rs#L38-L45) —— `WhatAmI` 枚举：`Router=0b001`、`Peer=0b010`(默认)、`Client=0b100`。

三种角色的语义（直接摘自源码注释）：

- **peer（默认）**：主动发现其他节点并建立直连（可用组播发现 + gossip）。
- **client**：只连一个接入点，由它充当通往网络的网关；适合算力受限的设备。
- **router**：运行 zenoh 路由器，维护预定义拓扑，不主动发现节点而依赖静态配置。

[commons/zenoh-protocol/src/core/whatami.rs:21-37](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/core/whatami.rs#L21-L37) —— `WhatAmI` 文档：三种 mode 的行为说明，并指明 peer 是默认。

> 补充：`mode` 不写时，默认配置文件 [DEFAULT_CONFIG.json5](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/DEFAULT_CONFIG.json5) 里写的是 `mode: "peer"`；而 `zenohd` 路由器二进制会把默认抬成 `"router"`。`id` 字段同理：不写则会在创建会话时**随机生成一个 u128**，所以你才会看到每次运行 zid 都不同。

#### 4.2.4 代码实践

**实践目标**：用「会话启动前」的方式读写配置，验证 `insert_json5` / `get_json` 的行为，并搞清楚 `mode` 的取值与默认。

**操作步骤**（这是一段「源码阅读 + 小程序」结合的实践）：

1. 写一个不联网的小程序，只操作 `Config`（不 `open`）：

```rust
// 示例代码：z_config_probe.rs
fn main() {
    let mut config = zenoh::Config::default();

    // 读取默认 mode（注意：未显式设置时这里是 null）
    println!("默认 mode = {}", config.get_json("mode").unwrap());

    // 启动前改 mode 为 client
    config.insert_json5("mode", "client").unwrap();
    println!("改后 mode = {}", config.get_json("mode").unwrap());
}
```

2. 阅读仓库测试，确认「`mode: "client"` 写进 JSON5 后能被解析成 `WhatAmI::Client`」：

[commons/zenoh-config/src/lib.rs:1246-1257](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-config/src/lib.rs#L1246-L1257) —— 测试断言：JSON5 `mode: "client"` 解析后 `config.mode() == Some(WhatAmI::Client)`。

**需要观察的现象**：

- 第 1 行打印 `默认 mode = null`（因为 `Config::default()` 没显式设 mode，字段是 `None`；真正固定成 `peer` 是 runtime 启动时 `expanded()` 干的）。
- 第 2 行打印 `改后 mode = "client"`。注意 `get_json` 返回的是 **JSON 文本**，字符串值会带引号，所以你看到的是 `"client"`（含双引号），而不是 `client`。

**预期结果**：两次打印分别是 `null` 与 `"client"`，从而印证「启动前可在 `Config` 上自由读写任意 key」。

> 待本地验证：`Config::default()` 上 `get_json("mode")` 是否确切返回 `null` 取决于内部 serde 默认；若你本地表现为 `"peer"`，说明该版本在 default 阶段就已填默认，这也合理——关键是理解「mode 决定角色」这一点。

#### 4.2.5 小练习与答案

**练习 1**：在会话 `open` 之后，执行 `session.config().insert_json5("mode", "router")` 会成功吗？为什么？

**参考答案**：不会成功，会报错。`session.config()` 返回的是运行时 `Notifier`，它的 `insert_json5` 只允许 `plugins/` 开头的 key（见 `ensure_config_key_is_dynamically_writable`）。`mode` 是网络拓扑核心配置，不允许运行时改动；要改 `mode`，只能在 `open` 之前的 `Config` 上改。

**练习 2**：`Config::from_json5(r#"{ mode: "peer" }"#)` 和 `Config::default()` 然后 `insert_json5("mode","peer")`，两者效果一样吗？

**参考答案**：在「最终生效的 mode」上是一样的，都是 peer。差别在于入口：前者从一段 JSON5 整体反序列化出一个 `Config`（适合一次性加载整个配置文件）；后者先拿默认配置再增量改一个字段（适合「默认配置 + 少量覆盖」，也是 `z_info` 等示例里 CLI 参数 `From<CommonArgs> for Config` 的典型做法）。

---

### 4.3 会话信息查询

#### 4.3.1 概念说明

会话打开后，你可能想知道几件事：

- 我自己的 ZenohId 是多少？→ `session.zid()` 或 `session.info().zid()`
- 我当前连到了哪些 router？→ `session.info().routers_zid()`
- 我当前连到了哪些 peer？→ `session.info().peers_zid()`
- 我开了哪些传输/链路？→ `session.info().transports()` / `links()`（`unstable`）

这些查询都通过 `session.info()` 返回的 `SessionInfo` 对象进行。这里有一个**重要的事实需要先讲清楚**：公开 API 里**没有一个直接读取「本端角色（whatami）」的方法**。`SessionInfo` 能告诉你 zid、连到的 router/peer 列表、传输/链路信息，但本端角色本身是配置（`mode`）决定的，只能通过 4.2 节的 `config.get_json("mode")` 来观察（或在 `unstable` 下通过 `transports()` 里**对端**的 `whatami()` 间接看到邻居角色）。

> 为什么这么设计？因为「本端角色」在会话存活期间不会变（不能运行时改 mode），它就是个配置属性；而 `SessionInfo` 提供的都是「会运行时变化」的动态信息（连了谁、开了哪些链路）。所以两者分开。

#### 4.3.2 核心流程

查询会话信息的标准套路：

1. `let info = session.info();` 拿到 `SessionInfo`（很轻，内部只持有一个 `WeakSession`）。
2. 调用具体方法，它们又都返回 builder，需要 `.await`：
   - `info.zid().await` → `ZenohId`
   - `info.routers_zid().await` → 一个产出 `ZenohId` 的迭代器（可 `.collect::<Vec<ZenohId>>()`）
   - `info.peers_zid().await` → 同上，peer 列表
3. 如果你启用了 `unstable` feature，还能：
   - `info.transports().await` → `Vec<Transport>`，每个 `Transport` 有 `.zid()` 和 `.whatami()`（**对端**的）。
   - `info.links().await` → `Vec<Link>`。

官方示例 `z_info.rs` 把这套用法完整演示了一遍，是本模块最好的参照。

#### 4.3.3 源码精读

**`SessionInfo` 结构**：内部只有一个 `WeakSession`（弱引用），所以持有它不会阻止会话被关闭。

[zenoh/src/api/info.rs:37-55](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/info.rs#L37-L55) —— `SessionInfo` 定义与文档：用于访问本会话的 zid 及连接到的 router/peer。

**`zid()` 返回的是 builder，要 `.await`**：注意返回类型是 `ZenohIdBuilder`，不是直接 `ZenohId`。

[zenoh/src/api/info.rs:63-76](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/info.rs#L63-L76) —— `SessionInfo::zid` 返回 `ZenohIdBuilder`，`.await` 后得到 `ZenohId`。

`ZenohIdBuilder` 的 resolve 目标就是 `ZenohId`，所以 `info.zid().await` 的类型是 `ZenohId`：

[zenoh/src/api/builders/info.rs:38-55](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/builders/info.rs#L38-L55) —— `ZenohIdBuilder`，`type To = ZenohId;`。

**`routers_zid()` / `peers_zid()` 返回的是「迭代器 builder」**：resolve 后得到 `Box<dyn Iterator<Item = ZenohId>>`，所以可以 `.collect()`。

[zenoh/src/api/info.rs:78-108](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/info.rs#L78-L108) —— `routers_zid` 与 `peers_zid`，均返回对应的 builder。

**`transports()`（`unstable`）里的 `Transport::whatami()` 是对端的角色**：这是公开 API 里唯一能见到 `whatami()` 的地方，但它反映的是**你连到的那台远端节点**的角色，不是你自己的。

[zenoh/src/api/info.rs:127-143](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/info.rs#L127-L143) —— `SessionInfo::transports`（`unstable`），返回各传输会话信息。

[zenoh/src/api/info.rs:292-296](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/info.rs#L292-L296) —— `Transport::whatami`（`unstable`）：返回**远端**节点类型。

**官方示例 `z_info.rs` 的核心**：先 `open`，再 `session.info()`，依次打印 zid、routers、peers；`unstable` 下还打印 transports/links 并监听连接事件。

[examples/examples/z_info.rs:27-39](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/examples/examples/z_info.rs#L27-L39) —— `z_info` 主体：`open` → `session.info()` → 打印 `zid()`/`routers_zid()`/`peers_zid()`。

```rust
let session = zenoh::open(config).await.unwrap();

let info = session.info();
println!("zid: {}", info.zid().await);
println!(
    "routers zid: {:?}",
    info.routers_zid().await.collect::<Vec<ZenohId>>()
);
println!(
    "peers zid: {:?}",
    info.peers_zid().await.collect::<Vec<ZenohId>>()
);
```

#### 4.3.4 代码实践

**实践目标**：跑通官方 `z_info` 示例，亲眼看到 zid / routers / peers 的输出，并理解为什么「单机独跑」时 routers/peers 列表是空的。

**操作步骤**：

1. 单独跑 `z_info`（参考 [u1-l2](u1-l2-build-run-examples.md) 的运行方式）：

   ```bash
   cargo run --example z_info
   ```

2. 阅读示例开头那行 `zenoh::init_log_from_env_or("error");`——它来自 `zenoh_util`，按环境变量（`RUST_LOG`）初始化日志，缺省等级 `error`。这是 Zenoh 应用的标准日志初始化姿势。

   [zenoh/src/lib.rs:281](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/lib.rs#L281) —— 根级 re-export `init_log_from_env_or`。

**需要观察的现象**：

- 会打印一行 `zid: <某 hex 串>`。
- `routers zid: []` 和 `peers zid: []` 都是空数组——因为你这台节点没连任何 router/peer（默认 peer 模式下，没有邻居自然就空）。
- 如果带 `unstable` feature 编译，还会打印 `transports:` / `links:` 段（同样基本为空）。

**预期结果**：看到自己的 zid，且 routers/peers 列表为空。这正好印证 4.3.1 的结论——在「没有对端」时，`SessionInfo` 只能告诉你自己的 zid，角色信息要回 `Config` 去看。

> 进阶：再开一个终端跑 `zenohd`（或另一个 `z_info`/`z_pub`），让两者通过默认 scouting 互相发现，重跑 `z_info`，你会看到 `peers zid` 不再为空——这就把「发现到的邻居」可视化了出来。scouting 机制本身是 [u6-l1](u6-l1-scouting.md) 的内容，本讲只需观察现象。

#### 4.3.5 小练习与答案

**练习 1**：`session.zid()` 和 `session.info().zid().await` 有什么区别？

**参考答案**：结果一样（都是本会话的 `ZenohId`），区别在调用形态。`session.zid()` 是个同步快捷方式，直接返回 `ZenohId`；`session.info().zid()` 返回 `ZenohIdBuilder`，需要 `.await` 才得到 `ZenohId`。源码注释也把前者称为「`zid()` is a convenient shortcut」（见 [session.rs:893-894](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L893-L894)）。日常用 `session.zid()` 更省事。

**练习 2**：为什么说「在公开 API 里看不到本端的 whatami」？我想确认自己跑的是 client 模式，该怎么办？

**参考答案**：因为 `SessionInfo` 没有 `whatami()` 方法——它提供的是动态信息（连了谁、开了哪些链路），而本端角色是配置属性、运行期间不变，所以没放进 `SessionInfo`。`unstable` 下的 `Transport::whatami()` 反映的是**对端**角色，不是本端。确认本端模式最稳的做法是查配置：在 `open` 之前 `config.get_json("mode")`，或在 `unstable` 下用 `session.config()` 读 mode。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面的综合任务（这是本讲的核心实践）。

**任务**：写一个程序，分两次运行——

1. 第一次：用 `Config::default()`（即 peer 模式）打开会话，打印 zid 与 mode，睡 3 秒后 `close`。
2. 第二次：用 `insert_json5("mode", "client")` 改成 client 模式后再打开，打印 zid 与 mode，睡 3 秒后 `close`。

对比两次输出的 zid 与「whatami（即 mode）」，体会「角色由配置决定、zid 随机生成」。

**示例代码**（**本讲编写，非仓库原有代码**；用一个布尔常量切换两次运行）：

```rust
// 示例代码：z_session_role_cmp.rs
const AS_CLIENT: bool = false; // 改成 true 再跑一次

#[tokio::main]
async fn main() {
    zenoh::init_log_from_env_or("error");

    let mut config = zenoh::Config::default();
    if AS_CLIENT {
        config.insert_json5("mode", "client").unwrap();
    }
    // whatami 由 mode 决定；用 get_json 观察它
    let mode = config.get_json("mode").unwrap();
    println!("本次配置的 mode(whatami) = {}", mode);

    let session = zenoh::open(config).await.unwrap();
    println!("我的 zid = {}", session.zid());

    tokio::time::sleep(std::time::Duration::from_secs(3)).await;
    session.close().await.unwrap();
}
```

**操作步骤**：

1. 先以 `AS_CLIENT = false` 编译运行一次，记录 `mode` 与 `zid`。
2. 把 `AS_CLIENT` 改成 `true`，再编译运行一次，记录新的 `mode` 与 `zid`。

**需要观察的现象与预期结果**：

| 运行 | `mode` 输出 | `zid` 输出 |
| --- | --- | --- |
| 第一次（default） | `null`（或 `"peer"`，取决于版本默认是否已填） | 某个 hex 串 **A** |
| 第二次（client） | `"client"` | 另一个 hex 串 **B**（与 A 不同） |

关键结论：

- **whatami 不同**：第二次明确是 `"client"`，第一次是默认（peer）。这就是题目所说的「对比 zid 之外的 whatami 输出」——通过 `config.get_json("mode")` 观察到角色差异（因为公开 API 没有直接读本端 whatami 的方法）。
- **zid 也不同**：因为默认不固定 `id`，每次 `open` 都随机生成一个 u128。如果你想两次 zid 一致，可以在两次运行里都 `config.insert_json5("id", "\"<某个固定 hex>\"")`（注意 JSON5 字符串要带引号）。
- 两次都能正常打开、停顿、关闭，说明 Session 的生命周期管理是可靠的。

> 待本地验证：`Config::default()` 时 `get_json("mode")` 的确切返回（`null` vs `"peer"`）以你本地版本为准；无论哪种，第二次的 `"client"` 都应清晰可见。

---

## 6. 本讲小结

- `zenoh::open(config)` 返回的是 `OpenBuilder`，必须 `.await`（或 `.wait()`）才会真正创建 `Session`；resolve 结果是 `ZResult<Session>`。
- `Session` 本质是 `Arc<SessionInner>`：克隆便宜、可跨线程共享；最后一个克隆被 drop 时会**自动关闭**，也可手动 `session.close().await`。
- `Config` 字段私有，只能用 `from_file`/`from_json5`/`insert_json5`/`get_json` 读写；**会话启动前**可改任意 key，**启动后**通过 `session.config()`（`Notifier`）只能改 `plugins/` 开头的 key。
- 节点角色由顶层 `mode` 决定，取值 `"router"`/`"peer"`/`"client"`，对应 `WhatAmI`；默认是 peer（`zenohd` 默认 router）。`id` 不写则随机生成，所以每次运行 zid 不同。
- 会话信息查询走 `session.info()`：`zid()`（也可直接 `session.zid()`）、`routers_zid()`、`peers_zid()`；`transports()`/`links()` 需要 `unstable` feature。
- 公开 API **没有**直接读「本端 whatami」的方法；本端角色靠配置 `mode` 观察，`Transport::whatami()`（unstable）反映的是**对端**角色。

## 7. 下一步学习建议

本讲你掌握了「打开会话」这一步。接下来建议：

- **下一讲 [u2-l2 Key Expression：Zenoh 的地址空间](u2-l2-key-expressions.md)**：会话有了，下一步要学 Zenoh 的「地址」——key expression 的斜杠路径与 `*`/`**` 通配符，理解了它，你才知道 pub/sub 的消息是怎么匹配送达的。
- **再下一讲 [u2-l3 配置系统与 WhatAmI 三种角色](u2-l3-config-whatami.md)**：会深入 `Config` 的结构、`DEFAULT_CONFIG.json5`、以及 router/peer/client 三种拓扑角色的更多细节，把本讲 4.2 节的内容展开。
- **想提前看内部实现**：可以浏览 [zenoh/src/api/session.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs) 中 `Session::init`（[session.rs:850-891](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L850-L891)）如何构造 `Arc<SessionInner>` 并注册 primitives——这是第 7 单元「从 Session 到 net 层」的伏笔，现在看不懂没关系，留个印象即可。
