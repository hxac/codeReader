# Pub/Sub 基础：put / Publisher / Subscriber / Sample

## 1. 本讲目标

学完本讲后，你应该能够：

- 用 `Session::declare_publisher` 创建 `Publisher`，并用 `Publisher::put` / `Publisher::delete` 发送数据；
- 区分「长生命周期 Publisher」与「一次性快捷方法 `Session::put` / `Session::delete`」两种发布方式；
- 用 `Session::declare_subscriber` 创建 `Subscriber`，并写出基于 `recv_async()` 的接收循环；
- 说清楚 `Sample` 这个数据单元里有哪些字段，特别是 `kind`（`Put` / `Delete`）、`payload`、`key_expr`；
- 对照 `z_pub.rs` / `z_sub.rs` 两个官方示例，把上面的 API 串成可运行的发布/订阅程序。

本讲只讲 Pub/Sub（data in motion）这一条主链路，Query/Reply 留到《u4》。

## 2. 前置知识

本讲承接《u2-l1 打开一个 Session》和《u2-l2 Key Expression》。开始前请确认你已经理解以下两件事：

- **Session 是 Arc 句柄**：`zenoh::open(config)` 返回的 `Session` 可以被克隆、跨线程共享，所有发布/订阅操作都在它上面发起。
- **匹配靠 Key Expression 的集合关系，而不是地址**：一条消息从谁发到谁，不写「对方 IP」，完全由两端的 key expression 是否「相交」决定。本讲会反复用到这个直觉。

补充一个贯穿全文的小公式：若发布端用 key expression \(k_p\) 发布，订阅端订阅 \(k_s\)，则该样本会被投递，当且仅当

\[
\mathrm{intersects}(k_p,\, k_s) = \mathrm{true}
\]

其中 `intersects` 就是《u2-l2》讲过的「两集合有交集」判断。这也是为什么 `z_pub` 默认发 `demo/example/zenoh-rs-pub`、`z_sub` 默认订 `demo/example/**`（`**` 匹配多层）天然能匹配上。

再补充一个术语：**Sample（样本）**。它是 Zenoh 中 Pub/Sub 的数据单元——「一条带元数据的消息」。发布端 `put` 出去的、订阅端 `recv` 到来的，都是 `Sample`，本讲第 4.3 节会拆开它。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [zenoh/src/api/publisher.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/publisher.rs#L1) | 定义 `Publisher` 结构体及其 `put` / `delete` / `undeclare` 方法、`Priority` 枚举。 |
| [zenoh/src/api/subscriber.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/subscriber.rs#L1) | 定义 `Subscriber` 结构体及其 `undeclare`、`Deref` 到 handler 的实现。 |
| [zenoh/src/api/sample.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/sample.rs#L1) | 定义 `Sample`、`SampleKind`、`SampleFields`，是 Pub/Sub 的数据单元。 |
| [zenoh/src/api/session.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L1) | `Session` 的 `declare_publisher` / `declare_subscriber` / `put` / `delete` 入口。 |
| [examples/examples/z_pub.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/examples/examples/z_pub.rs#L1) | 官方发布示例，演示 `declare_publisher` + `put` 主流程。 |
| [examples/examples/z_sub.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/examples/examples/z_sub.rs#L1) | 官方订阅示例，演示 `declare_subscriber` + `recv_async` 主流程。 |

## 4. 核心概念与源码讲解

### 4.1 Publisher 与 put / delete

#### 4.1.1 概念说明

发布端要做两件事：

1. **声明一个 Publisher**，绑定到一个 key expression。声明的目的是告诉 Zenoh 网络「我以后会往这个 key 发数据」，网络据此做路由优化。
2. **发送数据**，有两种动作：`put`（写入/更新一个值，对应 `SampleKind::Put`）和 `delete`（声明一个值被删除，对应 `SampleKind::Delete`）。

为什么要有 `delete`？因为 Zenoh 的 Pub/Sub 不只是「发个消息」，它还和存储/查询语义挂钩——`delete` 表示「这个 key 上的数据没了」，存储后端和订阅者都能据此清理状态。

此外，Zenoh 提供了**两种发布姿势**：

- **长生命周期 `Publisher`**：适合反复往同一 key 发数据的场景（如周期上报）。声明一次、`put` 多次，`Publisher` 的 QoS 配置（优先级、拥塞控制、编码）只写一次。
- **一次性快捷方法**：`Session::put(key, value)` / `Session::delete(key)`。从源码看，它们其实是「临时声明一个 Publisher 并立刻发一次」的语法糖（见 4.1.3），适合临时、零散的发布。

#### 4.1.2 核心流程

发布一条 `put` 的流程可以概括为：

```
Session::declare_publisher(key)   ──►  返回 PublisherBuilder（带默认 QoS）
        .await                    ──►  resolve 成 Publisher（已向网络声明）
Publisher::put(payload)           ──►  返回 PublisherPutBuilder（可改 encoding/timestamp/attachment）
        .await                    ──►  resolve：调用 session.resolve_put(...) 真正发出去
```

关键点：`declare_publisher` 和 `put` 返回的都是 **builder**，必须 `.await`（或同步 `.wait()`）才会真正执行。这跟《u1-l4》讲的 `Resolvable` / `Resolve` / `Wait` 三大 trait 一致——builder「不 resolve 就什么都不做」，编译器会用 `#[must_use]` 警告你。

`Publisher` 还实现了 `Drop`：当它被释放时，若 `undeclare_on_drop` 为真，会自动向网络「注销」这个发布者。所以正常情况下你不必手动 `undeclare`，让 `Publisher` 离开作用域即可。

#### 4.1.3 源码精读

先看 `Publisher` 这个结构体本身，它持有发布所需的一切上下文：

[zenoh/src/api/publisher.rs:110-125](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/publisher.rs#L110-L125) —— `Publisher` 字段：`key_expr`、`encoding`、`congestion_control`、`priority`、`destination`（本地/远端）等。注意它只持有一个 `WeakSession`（弱引用），所以 `Publisher` 不会阻止 Session 被关闭。

`put` 方法返回一个 builder，并把 Publisher 自身的 `encoding` 预填进去（可被覆盖）：

[zenoh/src/api/publisher.rs:257-273](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/publisher.rs#L257-L273) —— `Publisher::put`，构造一个 `PublicationBuilder { kind: PublicationBuilderPut { payload, encoding } }`。文档明确说明：匹配该 key expression 的订阅者会收到 `SampleKind::Put`。

`delete` 方法结构相同，只是 `kind` 换成 `PublicationBuilderDelete`：

[zenoh/src/api/publisher.rs:291-300](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/publisher.rs#L291-L300) —— `Publisher::delete`，订阅者会收到 `SampleKind::Delete`。

builder 的 `wait()`（也就是 `.await` 背后真正执行的同步逻辑）最终调用 `session.resolve_put(...)`：

[zenoh/src/api/builders/publisher.rs:492-498](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/builders/publisher.rs#L492-L498) —— `PublicationBuilder<&Publisher, PublicationBuilderPut>` 的 `wait`，调用 `self.publisher.session.resolve_put(key_expr, payload, kind, encoding, ...)` 把样本送进网络层。`delete` 的版本（同文件 513 行附近）只是把 payload 换成空的 `ZBytes::new()`。

再看 `Publisher` 的自动注销：

[zenoh/src/api/publisher.rs:467-475](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/publisher.rs#L467-L475) —— `Drop for Publisher`，若 `undeclare_on_drop` 为真则调用 `undeclare_impl()`。

最后看「快捷方法」`Session::put` 是如何实现的——它确实就是「临时声明 Publisher + put」的糖：

[zenoh/src/api/session.rs:1320-1341](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L1320-L1341) —— `Session::put`，内部 `publisher: self.declare_publisher(key_expr)`，`kind: PublicationBuilderPut { payload, ... }`。`Session::delete`（1360 行起）同理，`kind` 为 `PublicationBuilderDelete`。

`declare_publisher` 本身只是搭好一个带默认 QoS 的 builder：

[zenoh/src/api/session.rs:1185-1204](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L1185-L1204) —— `Session::declare_publisher`，默认 `congestion_control: CongestionControl::DEFAULT`、`priority: Priority::DEFAULT`、`encoding: Encoding::default()`。

> 顺带一提 `Priority` 枚举（[publisher.rs:530-541](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/publisher.rs#L530-L541)），共 7 档，默认 `Data`，数值越小优先级越高。它和 `CongestionControl` 都属于 QoS，本讲只需知道「默认值在哪设」，深入对比留到《u3-l3 服务质量》。

#### 4.1.4 代码实践

**实践目标**：对照官方 `z_pub.rs`，看懂「打开 Session → 声明 Publisher → 循环 put」三步。

**操作步骤**：

1. 阅读官方示例主流程：

   [examples/examples/z_pub.rs:27-60](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/examples/examples/z_pub.rs#L27-L60) —— 注意三个动作：`zenoh::open(config)`、`session.declare_publisher(&key_expr)`、循环里 `publisher.put(buf).encoding(...).attachment(...).await`。

2. 在仓库根目录编译示例（需要 `--all-targets` 才会编出 example，参见《u1-l2》）：

   ```bash
   cargo build --release --example z_pub
   ```

3. 单独跑发布端（此时没有订阅端，消息不会有人收，但能验证程序正常）：

   ```bash
   cargo run --release --example z_pub -- --key demo/hello
   ```

**需要观察的现象**：终端会每秒打印一行 `Putting Data ('demo/hello': '[   0] Pub from Rust!')...`。

**预期结果**：程序持续运行、逐秒打印，按 Ctrl-C 退出。此时还没有订阅者，所以这只是「发出去」。

**待本地验证**：若编译报缺少 feature，请参考《u1-l2》确认 Rust 版本与 workspace 编译方式。

#### 4.1.5 小练习与答案

**练习 1**：`Session::put(key, value)` 和「先 `declare_publisher(key)` 再 `publisher.put(value)`」相比，在「连续发送 100 次」的场景下哪个更优？为什么？

> **参考答案**：后者更优。`Session::put` 每次调用内部都会 `declare_publisher`，相当于每次都向网络重新声明一次发布者；而长生命周期的 `Publisher` 只声明一次，之后 100 次 `put` 复用同一个声明，开销更小、网络优化更充分。

**练习 2**：下面这段代码能成功发布吗？为什么？
```rust
let publisher = session.declare_publisher("a/b").await?;
publisher.put("hello");
```

> **参考答案**：不能（会有 `#[must_use]` 警告，且消息不会真正发出）。`put(...)` 返回的是一个 builder，必须 `.await`（或 `.wait()`）才会 resolve 并真正发送；不 resolve 它就什么都不做。

---

### 4.2 Subscriber

#### 4.2.1 概念说明

订阅端的任务是用一个 key expression「表达兴趣」，然后持续接收所有与该 key expression 相交的 `Sample`。

与发布端对称：

- 用 `Session::declare_subscriber(key)` 声明，返回 `SubscriberBuilder`，`.await` 后得到 `Subscriber<Handler>`。
- `Subscriber` 同样在 `Drop` 时自动注销，无需手动 `undeclare`。

一个关键设计：**`Subscriber` 把「数据怎么取」交给了 Handler**。`Subscriber<Handler>` 实现了 `Deref<Target = Handler>`，所以你可以直接在 `Subscriber` 上调用 handler 的方法——最常见的就是默认 handler 提供的 `recv_async()` / `recv()`。Handler 还可以是回调函数或自定义通道，这是《u3-l2 回调与通道》的主题，本讲用默认 handler 即可。

默认 handler 是 `DefaultHandler`（本质上是一个 `FifoChannel`，先进先出、不丢消息）：

[zenoh/src/api/handlers/mod.rs:48-49](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/handlers/mod.rs#L48-L49) —— `pub struct DefaultHandler(FifoChannel);`

#### 4.2.2 核心流程

订阅接收的标准循环：

```
Session::declare_subscriber(key)  ──► SubscriberBuilder（默认 DefaultHandler）
        .await                    ──► resolve 成 Subscriber<DefaultHandler>
loop {
    subscriber.recv_async().await ──► 返回一个 Sample（经 Deref 到 FifoChannel）
}
```

`recv_async()` 在所有 sender 被丢弃时返回 `Err`，因此 `while let Ok(sample) = sub.recv_async().await { ... }` 是惯用写法——Session 关闭、Subscriber 被注销时循环自然结束。

#### 4.2.3 源码精读

`Subscriber` 结构体（注意 `#[non_exhaustive]`，不能直接用结构体字面量构造）：

[zenoh/src/api/subscriber.rs:161-166](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/subscriber.rs#L161-L166) —— `Subscriber<Handler>` 字段：`inner: SubscriberInner`、`handler: Handler`、`callback_sync_group`。

`Deref` 把 `Subscriber` 透明地变成它的 handler，这就是为什么 `subscriber.recv_async()` 能用：

[zenoh/src/api/subscriber.rs:287-298](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/subscriber.rs#L287-L298) —— `Deref` / `DerefMut` 都指向 `Handler`。

`recv` / `recv_async` 等方法其实定义在 handler（`FifoChannel` 的接收端）上：

[zenoh/src/api/handlers/fifo.rs:86-90](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/handlers/fifo.rs#L86-L90) —— `FifoReceiver::recv`，阻塞等待一个值。

`Subscriber` 的自动注销（与 `Publisher` 对称）：

[zenoh/src/api/subscriber.rs:266-274](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/subscriber.rs#L266-L274) —— `Drop for Subscriber`，`undeclare_on_drop` 为真时调用 `undeclare_impl()`。

`declare_subscriber` 入口：

[zenoh/src/api/session.rs:1108-1122](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L1108-L1122) —— 返回 `SubscriberBuilder<..., DefaultHandler>`，默认 origin 为 `Locality::Any`。

#### 4.2.4 代码实践

**实践目标**：跑通官方 `z_sub.rs`，与《4.1.4》的 `z_pub` 配合，亲眼看到匹配与投递。

**操作步骤**：

1. 阅读订阅主流程：

   [examples/examples/z_sub.rs:26-50](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/examples/examples/z_sub.rs#L26-L50) —— `zenoh::open(config)`、`session.declare_subscriber(&key_expr)`、`while let Ok(sample) = subscriber.recv_async().await { ... }`，循环里用 `sample.kind()`、`sample.key_expr()`、`sample.payload()` 解析样本。

2. 开两个终端，**先启订阅端**：

   ```bash
   # 终端 A
   cargo run --release --example z_sub
   ```

   ```bash
   # 终端 B
   cargo run --release --example z_pub
   ```

3. 把发布端的 key 改到订阅范围之外，观察匹配失败：

   ```bash
   cargo run --release --example z_pub -- --key other/place
   ```

**需要观察的现象**：
- 第 2 步：订阅端每秒打印 `>> [Subscriber] Received PUT ('demo/example/zenoh-rs-pub': '[   0] Pub from Rust!')`。
- 第 3 步：订阅端**不再打印**任何东西，因为 `other/place` 与订阅的 `demo/example/**` 不相交。

**预期结果**：验证了「投递与否由 key expression 相交关系决定」这一核心结论。

**待本地验证**：若两端进程互不可见（如不在同一主机），需通过 `-e tcp/<对端地址>:7447` 显式 connect，参见《u1-l2》的 `CommonArgs`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `subscriber.recv_async()` 能直接调用，而 `recv_async` 明明定义在 handler 上？

> **参考答案**：因为 `Subscriber<Handler>` 实现了 `Deref<Target = Handler>`（subscriber.rs:287），方法调用会自动解引用到 handler。默认 handler `DefaultHandler(FifoChannel)` 的接收端正好提供 `recv_async`。

**练习 2**：`while let Ok(sample) = sub.recv_async().await { ... }` 这个循环在什么情况下会退出？

> **参考答案**：当所有往该通道发送数据的 sender 都被丢弃时，`recv_async` 返回 `Err`，`Ok(sample)` 匹配失败，循环结束。典型情形是 Session 被 close 或 `Subscriber` 被注销/drop。

---

### 4.3 Sample：Pub/Sub 的数据单元

#### 4.3.1 概念说明

无论发布还是订阅，流动的都是 `Sample`。可以把 `Sample` 想成「一条带信封的消息」：

- `key_expr`：这条消息「贴在」哪个 key 上（订阅端收到的 key 可能是发布 key 的具体值，也可能是被路由后的最终 key）。
- `payload`：消息正文，类型是 `ZBytes`（零拷贝字节容器，《u5-l1》详讲）。本讲只需知道它能 `try_to_string()` 还原成字符串。
- `kind`：`SampleKind::Put`（写入）或 `SampleKind::Delete`（删除）。这就是《4.1》说的「put/delete 对应两种 kind」在订阅端的体现。
- `encoding`：负载的编码类型（如 `TEXT_PLAIN`），是给应用层用的提示。
- `timestamp`：可选的时间戳，用于乱序合并去重（《u6-l4》详讲）。
- `attachment`：可选的附带数据（如 `z_pub` 里 `--attach` 的内容），可以塞额外元信息。

其中最该记住的是 `kind`：**订阅端必须区分 Put 与 Delete**——Put 表示「有个新值」，Delete 表示「这个值被删了」。在带存储/状态同步的应用里，收到 Delete 通常意味着要清理本地对应的状态。

#### 4.3.2 核心流程

`Sample` 在网络层由 `PushBody::Put` / `PushBody::Del` 两种消息体承载，到达订阅端后被 `Sample::from_push` 转换成公开的 `Sample`：

```
网络 PushBody::Put  ──►  Sample { kind: Put,  payload: put.payload,  encoding: put.encoding, ... }
网络 PushBody::Del  ──►  Sample { kind: Delete, payload: 空,         encoding: 默认,        ... }
```

即：**Delete 的 payload 是空的**，它只携带「这个 key 上的数据没了」这一信号（外加可选的 timestamp/attachment）。

#### 4.3.3 源码精读

`SampleKind` 枚举，只有两种取值：

[zenoh/src/api/sample.rs:144-153](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/sample.rs#L144-L153) —— `SampleKind { Put = 0, Delete = 1 }`，默认 `Put`。

`Sample` 结构体（`#[non_exhaustive]`，部分字段受 `unstable` 门控）：

[zenoh/src/api/sample.rs:239-253](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/sample.rs#L239-L253) —— 字段：`key_expr`、`payload`、`kind`、`encoding`、`timestamp`、`qos`，以及 unstable 的 `reliability` / `source_info`、`attachment`。

读取这些字段的 getter：

[zenoh/src/api/sample.rs:255-289](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/sample.rs#L255-L289) —— `key_expr()` / `payload()` / `kind()` / `encoding()` / `timestamp()`。

`from_push` 展示了 Put 与 Del 两种消息体如何被分别构造，是理解「Delete 无 payload」的关键：

[zenoh/src/api/sample.rs:350-384](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/sample.rs#L350-L384) —— `PushBody::Put` 分支：`payload: mem::take(&mut put.payload)`、`kind: SampleKind::Put`；`PushBody::Del` 分支：`payload: Default::default()`（空）、`kind: SampleKind::Delete`。

如果你想把 `Sample` 解构成字段（避免反复 clone getter），可以用 `SampleFields`：

[zenoh/src/api/sample.rs:198-213](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/sample.rs#L198-L213) —— `SampleFields` 的公开字段（`key_expr`、`payload`、`kind`、`encoding`、`timestamp`、`attachment` 等），可通过 `let SampleFields { key_expr, payload, kind, .. } = sample.into();` 一次性拆出。

仓库里也有现成的单元测试，用最精简的方式展示了 Put/Delete 的端到端一致性，值得对照阅读：

[zenoh/src/api/publisher.rs:679-705](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/publisher.rs#L679-L705) —— `sample_kind_integrity_in_publication` 测试：声明 sub + pub，分别 `put(VALUE)` / `delete()`，断言收到的 `sample.kind` 与发出的一致，且 Put 时 `payload.try_to_string()` 等于原值。

#### 4.3.4 代码实践

**实践目标**：亲手验证「put 发出 Put、delete 发出 Delete，且 Delete 的 payload 为空」。

**操作步骤**：

1. 阅读上面的测试 [publisher.rs:679-705](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/publisher.rs#L679-L705)。
2. 运行它：

   ```bash
   cargo test -p zenoh --lib sample_kind_integrity_in_publication -- --nocapture
   ```

**需要观察的现象**：测试通过。

**预期结果**：说明在同一个 Session 里，`publisher.put("zenoh")` 让订阅端收到 `SampleKind::Put` 且 payload 还原为 `"zenoh"`；`publisher.delete()` 让订阅端收到 `SampleKind::Delete`。

**待本地验证**：单测在进程内本地完成投递（SessionLocal），不需要起额外进程。

#### 4.3.5 小练习与答案

**练习 1**：订阅端收到一个 `Sample`，如何判断它是「写入一个值」还是「删除一个值」？Delete 时 payload 里有内容吗？

> **参考答案**：看 `sample.kind()`，返回 `SampleKind::Put` 表示写入、`SampleKind::Delete` 表示删除。根据 `from_push`（sample.rs:370-382），Delete 的 `payload` 为默认空值——Delete 只表达「这个 key 上的数据没了」，不携带正文。

**练习 2**：`Sample` 被标记了 `#[non_exhaustive]`，这对使用者意味着什么？

> **参考答案**：意味着不能在 crate 外用结构体字面量 `Sample { ... }` 直接构造它，只能通过 API（如 builder、`from_push`）创建，也无法匹配它的全部字段做穷尽解构。这样 Zenoh 团队后续可以给 `Sample` 增加字段而不破坏已有用户代码。

## 5. 综合实践

把本讲三块内容（Publisher/put、Subscriber、Sample）串起来，完成下面这个「温度上报 + 状态清理」小程序。它综合体现：长生命周期 Publisher 的复用、`recv_async` 接收循环、`SampleKind` 的 Put/Delete 区分，以及 key expression 相交匹配。

**任务**：写一个进程，它同时是发布者也是订阅者：

- 声明 `Publisher` 在 `sensor/temp` 上；
- 声明 `Subscriber` 订阅 `sensor/*`（与发布 key 相交，自己也能收到自己的消息）；
- 每 0.5 秒 `put` 一个随机温度；
- 订阅端收到样本时，**区分 Put 与 Delete**，把 `key_expr`、`kind`、`payload` 打印出来；
- 发到第 5 条后，发一次 `delete`，观察订阅端的输出。

下面是示例代码（**非项目原有代码，仅供练习参考**，依赖 `zenoh` 与 `tokio`，feature 按需）：

```rust
// 示例代码：温度上报 + 状态清理，综合演示 put / delete / Subscriber / Sample
use std::time::Duration;
use zenoh::Config;

#[tokio::main]
async fn main() {
    let session = zenoh::open(Config::default()).await.unwrap();

    // 1) 订阅 sensor/*（与下面的 sensor/temp 相交，本进程也能收到）
    let subscriber = session.declare_subscriber("sensor/*").await.unwrap();
    // 把接收循环放到独立任务里，边发边收
    let handle = tokio::spawn(async move {
        while let Ok(sample) = subscriber.recv_async().await {
            match sample.kind() {
                zenoh::sample::SampleKind::Put => {
                    let v = sample.payload().try_to_string().unwrap_or_default();
                    println!("[收] Put   {} = {}", sample.key_expr().as_str(), v);
                }
                zenoh::sample::SampleKind::Delete => {
                    println!("[收] Delete {}（数据已清理）", sample.key_expr().as_str());
                }
            }
        }
    });

    // 2) 长生命周期 Publisher，复用声明
    let publisher = session.declare_publisher("sensor/temp").await.unwrap();
    for i in 0..6u32 {
        if i == 5 {
            publisher.delete().await.unwrap(); // 发 Delete
        } else {
            let temp = 20 + (i as f64) * 0.5; // 模拟随机温度
            publisher.put(format!("{temp:.1}")).await.unwrap();
        }
        tokio::time::sleep(Duration::from_millis(500)).await;
    }

    // 等接收循环把最后一条处理完
    tokio::time::sleep(Duration::from_secs(1)).await;
    handle.abort();
    session.close().await.unwrap();
}
```

**需要观察的现象**：前 5 条为 `[收] Put sensor/temp = 20.0 / 20.5 / ...`，第 6 条为 `[收] Delete sensor/temp（数据已清理）`。

**预期结果**：证明 (a) `Publisher` 被复用、(b) 订阅者通过 key expression 相交收到自己发布的样本、(c) `put` 与 `delete` 在订阅端分别表现为 `SampleKind::Put` 与 `SampleKind::Delete`。

**待本地验证**：具体随机温度值取决于你用的随机方式；上面用确定递增值代替随机，便于核对。若想用真随机，可引入 `rand` 并改 `temp` 计算。

**延伸思考**：把订阅 key 改成 `sensor/humidity`（不相交），重新运行，订阅端应**收不到任何东西**——再次印证「投递由 key expression 相交决定」。

## 6. 本讲小结

- 发布侧有两种姿势：长生命周期的 `Publisher`（`declare_publisher` + 反复 `put`/`delete`，适合周期发送）和一次性快捷方法 `Session::put` / `Session::delete`（源码里就是「临时声明 Publisher 再发一次」）。
- `put` / `delete` / `declare_*` 返回的都是 builder，必须 `.await`（或 `.wait()`）才真正执行；`Publisher` 和 `Subscriber` 都在 `Drop` 时自动注销。
- 订阅侧用 `declare_subscriber` 声明，`Subscriber<Handler>` 通过 `Deref` 到 handler，默认 handler（`DefaultHandler = FifoChannel`）提供 `recv_async()`，用 `while let Ok(sample) = sub.recv_async().await` 是惯用接收循环。
- `Sample` 是 Pub/Sub 的数据单元，核心字段是 `key_expr`、`payload`、`kind`；`SampleKind` 只有 `Put` / `Delete` 两种，且 `Delete` 的 payload 为空。
- 消息投递与否完全由两端 key expression 的相交关系决定：\(\mathrm{intersects}(k_p, k_s)\) 为真才送达。
- 端到端一致性可直接看仓库内单元测试 `sample_kind_integrity_in_publication`（publisher.rs:679）。

## 7. 下一步学习建议

- 想了解「数据怎么取」的更多姿势（回调 vs 通道、背压 vs 丢帧），进入《u3-l2 回调与通道：Handlers / Channels》。
- 想掌握可靠性、拥塞控制、优先级对网络行为的影响，进入《u3-l3 服务质量（QoS）》。
- 想从「发-收」转向「问-答」，进入《u4 Query/Reply》单元，先读《u4-l1 Queryable 与 Query》。
- 想深入理解 `Sample.payload` 的 `ZBytes` 为何能零拷贝，进入《u5-l1 ZBytes 与 Encoding》。
