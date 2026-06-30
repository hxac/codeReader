# Liveliness：存活检测

## 1. 本讲目标

学完本讲，你应当能够：

- 理解 Zenoh **liveliness（存活检测）** 解决什么问题，以及它和普通 Pub/Sub 的本质区别。
- 用 `session.liveliness().declare_token(...)` 声明一个与 Session 生命周期绑定的**存活令牌**。
- 用 `session.liveliness().declare_subscriber(...)` 订阅 token 的**上线（Put）/下线（Delete）**变化，并理解 `history` 选项的含义。
- 用 `session.liveliness().get(...)` **拉取**当前网络里仍然存活的 token 列表。
- 把这三个原语组合成一个最小但完整的「服务发现 / 心跳感知」场景。

本讲是「支撑特性」单元的一讲，依赖《u3-l1 Pub/Sub 基础》。你只需会用 `Session`、`KeyExpr`、`Subscriber`、`Sample`，并能区分 `Put`/`Delete` 即可。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**什么是「存活」？** 分布式系统里，一个进程常常需要知道「另一个进程还在不在」。常见做法是周期性心跳（每隔 N 秒发一次消息），但心跳要应用自己写：发什么、发多频繁、断线重连、漏包怎么办……都很繁琐。Zenoh 把这件事做成了一等公民：你声明一个 `LivelinessToken`，只要声明它的 Session 还活着、网络还通，这个 token 就「存活」；一旦进程退出、崩溃或网络分区，token 会自动消失。**整个生命周期由 Zenoh 协议托管，应用不需要自己写任何心跳。**

**它和 Pub/Sub 的区别。** 《u3-l1》里的 `Publisher`/`Subscriber` 是搬运「数据」的——你 `put` 一条 payload，订阅端收到这条 payload。而 liveliness 搬运的是「状态」：token 的存在与否。token 本身**不携带业务数据**（payload 恒为空），它只表达「我在线」。所以严格来说，liveliness 是一个用 key expression 编址、由协议层维护真值的「分布式存在感」服务。

**三种用法。** Zenoh 的 liveliness 复用了你已熟悉的两条主链路：

| 动作 | 类比 | 模式 | 对应示例 |
|------|------|------|---------|
| `declare_token` | 声明「我在线」 | 供给侧 | `z_liveliness` |
| `declare_subscriber` | 监听别人上/下线 | 推送（push） | `z_sub_liveliness` |
| `get` | 查询当前谁在线 | 拉取（pull） | `z_get_liveliness` |

可以这样记：liveliness 把《u4 查询/应答》和《u3 订阅》两套机制，套用在「token 存在」这件特殊的事上。下面三个最小模块分别对应这三行。

> 术语约定：本讲里「**下线**」= token 不再存活（收到 `Delete`），「**上线**」= token 新出现（收到 `Put`）。这与《u3-l1》里 `SampleKind::Put` / `SampleKind::Delete` 的含义完全一致。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [zenoh/src/lib.rs:878-933](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/lib.rs#L878-L933) | 公开门面里 liveliness 模块的总览文档，是本讲概念的权威出处。 |
| [zenoh/src/api/liveliness.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/liveliness.rs) | `Liveliness` 入口结构、`declare_token` / `declare_subscriber` / `get` 三个方法、`LivelinessToken` 及其 `Drop`。 |
| [zenoh/src/api/builders/liveliness.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/builders/liveliness.rs) | 三个 builder：`LivelinessTokenBuilder`、`LivelinessSubscriberBuilder`（含 `history`）、`LivelinessGetBuilder`（含 `timeout`）。 |
| [zenoh/src/api/session.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs) | `liveliness()` 访问器，以及真正发协议消息的 `_inner` 实现。 |
| [examples/examples/z_liveliness.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/examples/examples/z_liveliness.rs) | 声明 token 的最小示例。 |
| [examples/examples/z_sub_liveliness.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/examples/examples/z_sub_liveliness.rs) | 订阅 token 变化的最小示例。 |
| [examples/examples/z_get_liveliness.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/examples/examples/z_get_liveliness.rs) | 查询当前存活 token 的最小示例。 |

> 公开稳定 API 全部在 `zenoh::liveliness` 模块下；`session.rs` 里带 `_inner` 后缀的函数是 `pub(crate)` 内部实现，仅供源码阅读，不应直接依赖。

## 4. 核心概念与源码讲解

本讲对应三个最小模块：**LivelinessToken（声明令牌）**、**存活订阅**、**存活查询**。注意，三个动作都从同一个入口 `session.liveliness()` 出发——它只是返回一个借用 Session 的轻量结构：

```rust
// zenoh/src/api/session.rs
pub fn liveliness(&self) -> Liveliness<'_> {
    Liveliness { session: self }
}
```

见 [zenoh/src/api/session.rs:1260-1262](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L1260-L1262)。`Liveliness` 本身不持有状态，只是把三个方法「挂在」当前 Session 上，符合 Zenoh 一贯的 builder 风格。

### 4.1 LivelinessToken：声明一个存活令牌

#### 4.1.1 概念说明

`LivelinessToken` 是「存活」这件事的供给侧：你声明它，表示「以这条 key expression 为名，我在线」。它的权威定义在公开文档里——这也是本讲最重要的核心命题：

> A token whose liveliness is tied to the Zenoh Session. A declared liveliness token will be seen as alive … **while the liveliness token is not undeclared or dropped, while the Zenoh application that declared it is alive (hasn't stopped or crashed) and while … has Zenoh connectivity with the [monitor]**.

翻译过来，一个 token「存活」要同时满足三个条件，缺一不可：

1. token 没有被 `undeclare()` 或 drop；
2. 声明它的进程还活着（没退出、没崩溃）；
3. 声明方与监控方之间网络可达（没有分区）。

这三条由 Zenoh 协议在路由层自动维护。一旦其中任何一条不满足，token 在全网范围内「消失」，监控方会收到 `Delete`。**这正是 liveliness 比手写心跳省心的地方：进程 `Ctrl-C` 杀掉、机器掉电、网线拔掉，都会自动被识别为下线。**

见 [zenoh/src/api/liveliness.rs:213-222](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/liveliness.rs#L213-L222)。

#### 4.1.2 核心流程

声明一个 token 的流程（从公开 API 到协议消息）：

```
session.liveliness().declare_token(ke).await
        │
        ▼
LivelinessTokenBuilder.wait()           # resolve
        │  把 ke 转成 OwnedKeyExpr
        ▼
Session::declare_liveliness_inner(ke)   # 分配 id
        │
        ▼
primitives.send_declare( Declare{ body: DeclareToken{ id, wire_expr } } )
        │  发出一条 network 层 DeclareToken 声明
        ▼
返回 LivelinessToken { session: WeakSession, id, undeclare_on_drop: true }
```

token 被 drop 时，`Drop` 实现会发出一条 `UndeclareToken`，自动从全网注销。也可以主动调用 `undeclare()` 提前注销。

#### 4.1.3 源码精读

公开入口 `declare_token` 接受任何能转成 `KeyExpr` 的参数，构造一个 builder：

```rust
// zenoh/src/api/liveliness.rs
pub fn declare_token<'b, TryIntoKeyExpr>(
    &self, key_expr: TryIntoKeyExpr,
) -> LivelinessTokenBuilder<'a, 'b>
where TryIntoKeyExpr: TryInto<KeyExpr<'b>>, ...
{
    LivelinessTokenBuilder {
        session: self.session,
        key_expr: TryIntoKeyExpr::try_into(key_expr).map_err(Into::into),
    }
}
```

见 [zenoh/src/api/liveliness.rs:121-133](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/liveliness.rs#L121-L133)。注意它**只构造 builder，不发任何消息**——必须 `.await`（或 `.wait()`）才 resolve，否则只会触发 `#[must_use]` 警告，token 不会真正注册。

resolve 的真正动作在 builder 的 `Wait` 实现里：

```rust
// zenoh/src/api/builders/liveliness.rs
fn wait(self) -> ZResult<LivelinessToken> {
    let session = self.session;
    let key_expr = self.key_expr?.into_owned();
    session.declare_liveliness_inner(&key_expr)
        .map(|id| LivelinessToken {
            session: self.session.downgrade(),
            id,
            undeclare_on_drop: true,
        })
}
```

见 [zenoh/src/api/builders/liveliness.rs:47-60](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/builders/liveliness.rs#L47-L60)。这里有两点值得记：返回的 `LivelinessToken` 用 `WeakSession`（弱引用）持有 Session，且 `undeclare_on_drop: true`——这两个字段是自动下线机制的关键。

`declare_liveliness_inner` 做的事很轻：分配一个 `id`，然后发出一条 network 层 `Declare` 消息，其 body 是 `DeclareToken`：

```rust
// zenoh/src/api/session.rs
pub(crate) fn declare_liveliness_inner(&self, key_expr: &KeyExpr) -> ZResult<Id> {
    let id = self.0.runtime.next_id();
    let primitives = zread!(self.0.state).primitives()?;
    primitives.send_declare(&mut Declare {
        // ... ext 字段略 ...
        body: DeclareBody::DeclareToken(DeclareToken {
            id,
            wire_expr: key_expr.to_wire(self).to_owned(),
        }),
    });
    Ok(id)
}
```

见 [zenoh/src/api/session.rs:1972-1987](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L1972-L1987)。可见 token 在协议层就是一个「带 id 的 key expression 声明」，和声明 `Subscriber` 用的是同一类 `Declare` 消息（只是 body 不同）。

自动注销靠 `Drop`：

```rust
// zenoh/src/api/liveliness.rs
impl Drop for LivelinessToken {
    fn drop(&mut self) {
        if self.undeclare_on_drop {
            if let Err(error) = self.undeclare_impl() {
                error!(error);
            }
        }
    }
}
```

见 [zenoh/src/api/liveliness.rs:323-331](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/liveliness.rs#L323-L331)。`undeclare_impl` 会发出 `UndeclareToken`（见 [zenoh/src/api/session.rs:2092-2108](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L2092-L2108)）。注意 `undeclare_impl` 第一行先把 `undeclare_on_drop` 置为 `false`——这是为了避免「函数中途 panic → Drop 再次触发 → 二次 panic」的防御性写法。

> ⚠️ 类型上的 `#[must_use]` 提醒很关键：`#[must_use = "Liveliness tokens will be immediately dropped and undeclared if not bound to a variable"]`（[zenoh/src/api/liveliness.rs:237](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/liveliness.rs#L237)）。**如果不把 `.await.unwrap()` 的结果绑定到变量，token 会立刻被 drop、立刻注销。**

#### 4.1.4 代码实践

本实践以官方示例 `z_liveliness` 为蓝本。

**实践目标**：声明一个 token，确认它被注册，并体会「token 绑定变量」的必要性。

**操作步骤**：

1. 编译并运行官方示例（需要在仓库根目录）：

   ```bash
   cargo build --release --example z_liveliness
   ./target/release/examples/z_liveliness --key app/instance-1
   ```

   也可以用 `cargo run --release --example z_liveliness -- --key app/instance-1`。

2. 阅读示例主流程 [examples/examples/z_liveliness.rs:18-37](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/examples/examples/z_liveliness.rs#L18-L37)，它只做三件事：`open` → `declare_token` → `std::thread::park()` 阻塞。注意 `token` 变量被保留到 `park()` 之后，正是因为上面那条 `#[must_use]` 约束。

3. **故意写错**：在自己的小程序里把 `let token = ...` 改成 `session.liveliness().declare_token("app/instance-1").await.unwrap();`（不绑定变量），编译观察编译器告警。

**需要观察的现象**：

- 步骤 1 中，进程打印 `Declaring LivelinessToken on 'app/instance-1'...` 后阻塞，不退出。
- 步骤 3 中，编译器应给出 `unused must_use: LivelinessToken` 之类的告警，且即使忽略告警运行，token 也会在声明后立刻注销。

**预期结果**：你会直观理解「token 必须存活在某个变量里」这一语义。运行中进程的精确存活证据，留到 4.2 的订阅端观察（因为单看声明方看不到自己）。

> 若本地尚未配置 Rust 1.75+ 或 `cargo build` 失败，记录「待本地验证」即可，源码阅读部分本身已完整。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `LivelinessToken` 用 `WeakSession` 而不是 `&Session` 或 `Arc<Session>`？

**参考答案**：`Session` 的所有权和生命周期由用户掌控，`LivelinessToken` 不应「绑架」Session（否则 token 不 drop，Session 可能无法按预期关闭）。用弱引用 `WeakSession`，token 在 drop 时尝试通知 Session 注销即可，Session 先于 token 释放也安全（`undeclare_impl` 对会话已关闭的情况做了处理）。

**练习 2**：进程被 `kill -9` 强杀时，token 为什么还能被监控方感知为「下线」？源码里哪段代码负责发注销消息？

**参考答案**：强杀进程时本地 `Drop` 不会执行，但 Zenoh 路由层维护着「哪些 Session 还在」的真值——声明方与路由器之间的传输断开（lease 超时）后，路由器会替这个 Session 注销它声明的所有 token，监控方因此收到 `Delete`。本地正常退出时，负责发注销消息的是 `Drop for LivelinessToken`（[zenoh/src/api/liveliness.rs:323-331](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/liveliness.rs#L323-L331)）→ `undeclare_impl` → `UndeclareToken`。

### 4.2 存活订阅：感知 token 的上线/下线

#### 4.2.1 概念说明

声明 token 是「我在场」，订阅 liveliness 则是「我想知道谁上线下线」。它返回的是一个**普通 `Subscriber`**（《u3-l1》讲过的同一类型），区别只在于：收到的 `Sample` 不是业务数据，而是 token 的状态变化——

- `SampleKind::Put` → 某个匹配 key 的 token **上线**；
- `SampleKind::Delete` → 某个匹配 key 的 token **下线**；
- `sample.key_expr()` → 是哪个 token；
- `sample.payload()` → **恒为空**（token 不携带数据）。

匹配规则与普通订阅完全一致：两端 key expression **相交**则触发（见《u2-l2》的 `intersects`）。比如 token 声明在 `app/instance-1`，订阅 `app/**`，二者相交，所以能收到通知。

#### 4.2.2 核心流程

订阅 liveliness 的特别之处在于一个 `history`（历史）选项，它决定「订阅瞬间，要不要把当前已经存活的 token 也补发给我」：

```
declare_subscriber(ke).history(true/false).await
        │
        ▼
Session::declare_liveliness_subscriber_inner(ke, history, cb)
        │  注册回调到本地资源表
        ▼
若 history == true：先从本地已知的 remote_tokens 里，
        按 intersects(ke) 挑出当前存活 token，立即补发 Put 给回调
        │
        ▼
primitives.send_interest( Interest{
        mode:  history? CurrentFuture : Future,
        options: KEYEXPRS + TOKENS,
        wire_expr: ke } )
```

关键是发出的 `Interest` 消息的 `mode` 字段：

- `history=true` → `InterestMode::CurrentFuture`：**要现在（Current）也要将来（Future）**，即补发当前存活 token + 监听后续变化；
- `history=false`（默认）→ `InterestMode::Future`：**只要将来**，不主动补发；但文档注明「当前存活的 token 仍有可能被投递」——因为路由器在转发时可能顺带带上。

这三档 mode 的语义见 [commons/zenoh-protocol/src/network/interest.rs:158-162](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/network/interest.rs#L158-L162)，比特编码是 `Current=0b01`、`Future=0b10`、`CurrentFuture=0b11`。

#### 4.2.3 源码精读

公开入口返回一个带默认 handler 的 builder，并把 `history` 初值设为 `false`：

```rust
// zenoh/src/api/liveliness.rs
pub fn declare_subscriber<'b, TryIntoKeyExpr>(
    &self, key_expr: TryIntoKeyExpr,
) -> LivelinessSubscriberBuilder<'a, 'b, DefaultHandler>
where ... {
    LivelinessSubscriberBuilder {
        session: self.session,
        key_expr: TryIntoKeyExpr::try_into(key_expr).map_err(Into::into),
        handler: DefaultHandler::default(),
        history: false,
    }
}
```

见 [zenoh/src/api/liveliness.rs:157-171](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/liveliness.rs#L157-L171)。这里也能用 `.callback(...)` / `.with(channel)` 换取数方式，和《u3-l2》完全一致。

`history()` 选项的文档说得最清楚，建议直接读注释（[zenoh/src/api/builders/liveliness.rs:224-238](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/builders/liveliness.rs#L224-L238)）：

> When set to `true`, Zenoh queries the network for *currently live tokens* upon declaring the subscriber.
> When set to `false`, Zenoh does not query the network for currently live tokens. In this mode, currently live tokens may still be delivered to the subscriber.

resolve 时（[zenoh/src/api/builders/liveliness.rs:248-278](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/builders/liveliness.rs#L248-L278)）调用内部 `declare_liveliness_subscriber_inner`，注意它构造的 `Subscriber` 的 `kind` 是 `SubscriberKind::LivelinessSubscriber`——这把 liveliness 订阅和普通数据订阅在内部状态表里区分开，避免二者互相干扰。

内部实现里有两段最值得读。**第一段**：当 `history=true`，从本地已知的 `remote_tokens`（远端存活 token 缓存）里按 `intersects` 挑出当前存活 token，立即补发 `Put`：

```rust
// zenoh/src/api/session.rs
let known_tokens = if history {
    state.remote_tokens.values()
        .filter(|token| key_expr.intersects(token))
        .cloned().collect::<Vec<KeyExpr<'static>>>()
} else { vec![] };
// ... 异步补发 Put（payload 为空）...
```

见 [zenoh/src/api/session.rs:2040-2073](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L2040-L2073)。补发出的 Sample 是手搓的 `Put`，`payload: ZBytes::new()`（空）、`timestamp: None`——再次印证 token 无业务数据。

**第二段**：向网络发 `Interest` 消息，mode 随 `history` 取值：

```rust
// zenoh/src/api/session.rs
primitives.send_interest(&mut Interest {
    id,
    mode: if history { InterestMode::CurrentFuture } else { InterestMode::Future },
    options: InterestOptions::KEYEXPRS + InterestOptions::TOKENS,
    wire_expr: Some(key_expr.to_wire(self).to_owned()),
    // ... ext 字段略 ...
});
```

见 [zenoh/src/api/session.rs:2075-2087](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L2075-L2087)。`InterestOptions::TOKENS`（[commons/zenoh-protocol/src/network/interest.rs:266](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/network/interest.rs#L266)）这一位告诉路由器「我对 token 感兴趣」——这是 liveliness 复用 `Interest` 协议、又区别于普通订阅的关键标志位。

**接收侧**（监控方如何把 token 变化变成 `Put`/`Delete` 投递给回调）：当路由器把别人的 `DeclareToken` 转发过来时，会进入处理分支。若该消息带 `interest_id`（说明是对某次查询 `Interest::Current` 的应答），则回复给查询；否则插入本地 `remote_tokens` 并通知订阅者一个 `Put`：

```rust
// zenoh/src/api/session.rs
zenoh_protocol::network::DeclareBody::DeclareToken(m) => {
    // ... 解析 key_expr ...
    if let Entry::Vacant(e) = state.remote_tokens.entry(m.id) {
        e.insert(key_expr.clone());
        drop(state);
        self.execute_subscriber_callbacks(/*...*/ &mut Put::default().into(), /*...*/);
    }
}
```

见 [zenoh/src/api/session.rs:3097-3155](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L3097-L3155)。对应的 `UndeclareToken` 分支则从 `remote_tokens` 移除并投递 `Del`（[zenoh/src/api/session.rs:3156-3179](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L3156-L3179)）。**这就是「上线=Put、下线=Delete」在源码里的落点。**

#### 4.2.4 代码实践

**实践目标**：用官方示例 `z_sub_liveliness` 实时观察 token 的上线与下线。

**操作步骤**：

1. 终端 A：先启动订阅端，监听 `app/**`：

   ```bash
   cargo run --release --example z_sub_liveliness -- --key 'app/**'
   ```

2. 终端 B：启动 4.1 的 `z_liveliness` 声明 token `app/instance-1`：

   ```bash
   cargo run --release --example z_liveliness -- --key app/instance-1
   ```

3. 在终端 B 按 `Ctrl-C`（或直接关掉）让声明方退出。

**需要观察的现象**（订阅端输出）：

```
>> [LivelinessSubscriber] New alive token ('app/instance-1')      # 步骤2 后：上线（Put）
>> [LivelinessSubscriber] Dropped token ('app/instance-1')        # 步骤3 后：下线（Delete）
```

**预期结果**：分别对应 `SampleKind::Put` 与 `SampleKind::Delete` 分支，正是示例 [examples/examples/z_sub_liveliness.rs:38-49](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/examples/examples/z_sub_liveliness.rs#L38-L49) 的 `match sample.kind()`。

**进阶**：重新打开订阅端，但这次先用终端 B 声明 token，**再**启动订阅端。默认 `--history` 未开启时，订阅端可能看不到已存活的 `app/instance-1`（因为 `InterestMode::Future` 不补发）；加上 `--history` 再试：

```bash
cargo run --release --example z_sub_liveliness -- --key 'app/**' --history
```

应能看到 `New alive token ('app/instance-1')` 被补发。对照源码 [zenoh/src/api/session.rs:2040-2048](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L2040-L2048) 理解补发来源。

#### 4.2.5 小练习与答案

**练习 1**：为什么默认 `history=false`，但文档又说「当前存活 token 仍可能被投递」？二者矛盾吗？

**参考答案**：不矛盾。`history=false` 只意味着订阅端**本进程**不主动从自己的 `remote_tokens` 缓存补发，且发出的 `Interest` mode 是 `Future`。但路由器在传播 token 时，对一个新的 `Future` 订阅者仍可能把「当前存活」的 token 当作新事件转发一次（见源码注释「interest_id is set if the Token is an Interest::Current … used to decide if subs with history=false should be called or not」，[zenoh/src/api/session.rs:3143-3146](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L3143-L3146)）。所以「不保证补发」≠「一定不补发」。若你的逻辑强依赖「订阅瞬间拿到全部存活 token」，请显式用 `history(true)`。

**练习 2**：liveliness 订阅返回的 `Sample`，其 `payload` 为什么是空的？

**参考答案**：因为 liveliness 表达的是「存在状态」而非「数据内容」。token 只有存活/不存活两种状态，由 `SampleKind`（Put/Delete）表达已足够，不需要 payload。源码里补发和应答构造 Sample 时都用 `payload: ZBytes::new()`（[zenoh/src/api/session.rs:2058-2070](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L2058-L2070)）。

### 4.3 存活查询：拉取当前存活的 token

#### 4.3.1 概念说明

订阅是「持续监听」，查询是「拉取快照」：`session.liveliness().get(ke)` 会向网络发出一次请求，收集**当前**所有匹配 `ke` 且仍存活的 token，应答以 `Reply` 形式返回（和《u4》查询/应答的 `Reply` 是同一类型）。它适合「一次性盘点现在有谁在线」，例如启动时发现服务、或周期性做存活体检。

注意它与订阅的两个区别：

1. 查询用的是 `InterestMode::Current`（**只要现在**），订阅 `history=true` 用的是 `CurrentFuture`（现在 + 将来）。
2. 查询有**超时**：到点未应答完，会收到一个 `ReplyError`（payload 文本为 `"Timeout"`）作为结束标志。

#### 4.3.2 核心流程

```
session.liveliness().get(ke).timeout(d).await   # resolve 得到一个 handler
        │
        ▼
Session::liveliness_query(ke, timeout, cb)
        │  分配 id，注册 liveliness_queries[id] = { cb }
        ▼
spawn 一个计时任务：tokio::select! { 超时 → cb.call(Reply{Err: "Timeout"}); token.cancelled → 不做事 }
        │
        ▼
primitives.send_interest( Interest{
        mode: Current,           # 只要当前存活 token
        options: KEYEXPRS + TOKENS,
        wire_expr: ke } )
        │
        ▼  网络里匹配的 token 持有方，逐个用 DeclareToken(带 interest_id) 回应
        │  接收侧把每条带 interest_id 的 DeclareToken 包装成 Reply{Ok(Sample{Put})} 投给 cb
        ▼
调用方在 handler 上 recv_async() 收 Reply，直到超时/取消
```

#### 4.3.3 源码精读

公开入口与 `declare_subscriber` 对称，额外预填一个默认超时（取自 `queries_default_timeout()`）：

```rust
// zenoh/src/api/liveliness.rs
pub fn get<'b, TryIntoKeyExpr>(&self, key_expr: TryIntoKeyExpr)
    -> LivelinessGetBuilder<'a, 'b, DefaultHandler>
where ... {
    let key_expr = key_expr.try_into().map_err(Into::into);
    LivelinessGetBuilder {
        session: self.session,
        key_expr,
        timeout: self.session.queries_default_timeout(),
        handler: DefaultHandler::default(),
        #[cfg(feature = "unstable")]
        cancellation_token: None,
    }
}
```

见 [zenoh/src/api/liveliness.rs:193-210](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/liveliness.rs#L193-L210)。`timeout()` 选项见 [zenoh/src/api/builders/liveliness.rs:452-459](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/builders/liveliness.rs#L452-L459)。

resolve 后调用 `liveliness_query`，它做三件事（[zenoh/src/api/session.rs:2782-2856](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L2782-L2856)）：

1. **分配 id 并注册回调**。这里有一段重要注释：

   > Queries must use the same id generator as liveliness subscribers. This is because both query's id and subscriber's id are used as interest id, so both must not overlap.

   见 [zenoh/src/api/session.rs:2791-2794](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L2791-L2794)。查询和订阅共用一套 interest id 空间，所以共用 `next_id()`。

2. **起一个超时计时任务**，用 `tokio::select!` 在「到点」和「被取消」之间二选一；超时则向回调投一个 `ReplyError("Timeout")`：

   ```rust
   // zenoh/src/api/session.rs
   tokio::select! {
       _ = tokio::time::sleep(timeout) => {
           // ... 从 liveliness_queries 移除，cb.call(Reply{ Err: ReplyError::new("Timeout", ...) })
       }
       _ = token.cancelled() => {}
   }
   ```

   见 [zenoh/src/api/session.rs:2811-2831](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L2811-L2831)。**这就是查询「到点自然结束」的机制**——调用方在 `recv_async` 循环里收到这条错误后，再 recv 就会因 handler 关闭而返回 `Err`，循环自然退出（和《u4-l2》里 `nb_final` 归零退出的思路一致）。

3. **发出 `Interest`（mode = `Current`）**：

   ```rust
   // zenoh/src/api/session.rs
   primitives.send_interest(&mut Interest {
       id,
       mode: InterestMode::Current,
       options: InterestOptions::KEYEXPRS + InterestOptions::TOKENS,
       wire_expr: Some(wexpr.clone()),
       // ... ext 字段略 ...
   });
   ```

   见 [zenoh/src/api/session.rs:2845-2853](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L2845-L2853)。注意与订阅的区别：这里是纯 `Current`，不关心将来。

   > 一个实现细节（注释见 [zenoh/src/api/session.rs:2833-2836](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L2833-L2836)）：查询**没有**像 `history=true` 订阅那样从本地 `remote_tokens` 预先补发，而是依赖路由器在每次查询时重发当前 token。这是因为 gateway 在响应查询时会重发当前 token，是协议设计的有意为之。

**应答侧**：网络上存活 token 的持有方，会把 `DeclareToken` 带 `interest_id` 回应；接收侧命中 `liveliness_queries` 里的查询时，构造一条 `Reply{ Ok(Sample{Put, 空payload}) }` 投给查询回调（见 [zenoh/src/api/session.rs:3109-3131](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L3109-L3131)）。于是调用方每收到一个 `Ok` 应答，就代表一个当前存活的 token。

#### 4.3.4 代码实践

**实践目标**：用官方示例 `z_get_liveliness` 拉取当前存活 token，并验证「声明方退出后，查询结果随之变空」。

**操作步骤**：

1. 终端 B（声明方）保持运行 `z_liveliness --key app/instance-1`（沿用 4.2 启动的进程）。
2. 终端 C 执行查询：

   ```bash
   cargo run --release --example z_get_liveliness -- --key-expr 'app/**' --timeout 3000
   ```

3. 关掉终端 B（让 `app/instance-1` 下线），在终端 C **再次**执行步骤 2 的查询。

**需要观察的现象**：

- 步骤 2 输出：`>> Alive token ('app/instance-1')`（成功查询到存活 token）。
- 步骤 3 输出：只有查询提示，**没有** `Alive token` 行；等待约 `--timeout` 毫秒后查询结束。

**预期结果**：`app/instance-1` 下线后，`get` 不再返回它，证明 token 的存活与声明进程绑定。循环写法见 [examples/examples/z_get_liveliness.rs:31-48](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/examples/examples/z_get_liveliness.rs#L31-L48)，注意它用 `reply.result()` 区分 `Ok(Sample)`（存活 token）与 `Err`（含超时错误）。

> 若本地暂不便启动多进程，可只阅读源码 [examples/examples/z_get_liveliness.rs:20-49](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/examples/examples/z_get_liveliness.rs#L20-L49) 并标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：查询超时后调用方收到的 `Reply` 是什么形态？为什么这样设计能让 `recv_async` 循环干净退出？

**参考答案**：超时后回调收到 `Reply { result: Err(ReplyError::new("Timeout", Encoding::ZENOH_STRING)) }`（[zenoh/src/api/session.rs:2821-2825](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L2821-L2825)）。投递完这条错误后查询结束、handler 被关闭，此后 `recv_async().await` 返回 `Err`，于是 `while let Ok(reply) = ...recv_async().await` 循环自然跳出，无需手写 `break`。

**练习 2**：把查询的 key expression 从 `app/**` 改成 `app/instance-1`，结果会一样吗？再改成 `other/**` 呢？

**参考答案**：`app/instance-1` 与 token 的 key 精确相等，相交，仍能查到该 token，结果一样。改成 `other/**` 则与 `app/instance-1` 不相交，查不到任何 token，超时后只收到 Timeout 错误。这印证了 liveliness 的匹配本质仍是 key expression 的集合关系（《u2-l2》的 `intersects`）。

## 5. 综合实践

把三个最小模块串成一个完整的「实例存活看板」小任务。

**场景**：你有若干个 worker 实例，每个实例在启动时声明一个 liveliness token `app/instance-<n>`；一个监控进程既要**实时**看到实例上线下线（订阅），又要能**按需**拉取当前在线名单（查询）。

**任务**：基于三个官方示例，组织一次三终端演练并解释现象。

1. **终端 A（监控·订阅）**：

   ```bash
   cargo run --release --example z_sub_liveliness -- --key 'app/**'
   ```

   期望：任何 `app/instance-*` 的上线/下线都会被实时打印（Put / Delete）。

2. **终端 B、C（worker·声明）**：分别启动两个实例：

   ```bash
   cargo run --release --example z_liveliness -- --key app/instance-1
   cargo run --release --example z_liveliness -- --key app/instance-2
   ```

   期望：终端 A 依次打印两条 `New alive token`。

3. **终端 D（监控·查询快照）**：

   ```bash
   cargo run --release --example z_get_liveliness -- --key-expr 'app/**' --timeout 3000
   ```

   期望：打印两条 `Alive token`，与终端 A 累计的「在线」集合一致。

4. **制造下线**：关掉终端 B（instance-1）。观察：
   - 终端 A（订阅）立刻打印 `Dropped token ('app/instance-1')`；
   - 在终端 D 再查一次，只剩 `app/instance-2` 一条 `Alive token`。

**要回答的问题**（写在你的学习笔记里）：

- 订阅（push）和查询（pull）分别适合什么场景？如果只想要「当前快照」该用哪个？如果想第一时间感知下线又该用哪个？
- 如果在终端 A 启动时 instance-1 已经在线，默认配置下终端 A 能否立刻看到它？如何保证一定能看到？（提示：`--history` 与 `InterestMode::CurrentFuture`。）
- 进程被 `kill -9`（而非正常退出）时，终端 A 是否仍能收到 `Dropped`？为什么？（提示：回顾 4.1.5 练习 2——协议层 lease 超时。）

完成上述演练后，你就把本讲的三个原语在「声明—订阅—查询—下线」闭环里全部跑通了。

## 6. 本讲小结

- **Liveliness 是协议托管的存在感服务**：声明 `LivelinessToken` 后，它的存活与 Session 绑定——未 drop、进程在世、网络可达三条件满足即存活；任一不满足，token 自动从全网消失。
- **声明靠 `declare_token`**：resolve 后发出 `DeclareToken`；`Drop` 时自动发 `UndeclareToken`。token 必须绑定到变量，否则立即注销（`#[must_use]`）。
- **订阅靠 `liveliness().declare_subscriber`**：返回普通 `Subscriber`，`Put`=上线、`Delete`=下线、payload 恒空；`history(true)` 走 `InterestMode::CurrentFuture` 补发当前存活 token，默认 `Future` 只听将来。
- **查询靠 `liveliness().get`**：走 `InterestMode::Current` 拉取当前快照，有超时机制，到点投 `ReplyError("Timeout")` 使接收循环自然退出。
- **底层复用 `Interest` + `Declare` 协议**：liveliness 不是独立协议族，而是用 `InterestOptions::TOKENS` 标志位复用了声明式兴趣机制，这是它能跨路由器自动维护真值的根本原因。
- **匹配本质仍是 key expression 相交**：与普通 Pub/Sub / Queryable 一致，token 的 key 与订阅/查询的 key `intersects` 才会触发。

## 7. 下一步学习建议

- 阅读《u6-l3 Matching》：liveliness 告诉你「谁在场」，matching 告诉你「有没有人在听」，二者常配合实现按需发布。
- 阅读《u6-l1 Scouting》：scouting 发现「节点」，liveliness 监控「逻辑实体」，注意区分二者粒度。
- 进阶可读《u8 路由》单元中 `dispatcher/token.rs` 与 `interests.rs`，看 token 如何在路由表里被注册与按需转发；以及 `commons/zenoh-protocol/src/network/interest.rs` 里 `InterestMode`/`InterestOptions` 的完整定义。
- 若关心「断线多久后才判定下线」，可顺带阅读传输层的 lease/keep_alive 配置（见《u9-l2》）——它决定了强杀进程后被感知为下线的延迟。
