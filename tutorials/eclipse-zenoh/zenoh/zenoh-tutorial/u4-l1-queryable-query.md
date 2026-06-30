# Queryable 与 Query：提供数据

## 1. 本讲目标

本讲进入 Zenoh 的**第二条主链路——查询/应答（Query/Reply）**。学完后你应当能够：

- 用 `Session::declare_queryable` 声明一个「可查询端」，并用回调或通道接收 `Query`。
- 看懂 `Query` 这个对象携带了哪些信息（key expression、参数、负载、附件），并能正确读取它们。
- 区分三种应答方法：`reply`（回一个 `Put` 样本）、`reply_del`（回一个 `Delete` 样本）、`reply_err`（回一个错误）。
- 理解 `Reply` / `ReplyError` 是怎么在请求侧被封装的，以及 Queryable 为什么是「提供历史/当前数据」的天然位置。

本讲只讲「**提供数据的一方（Queryable 侧）**」；发起查询的 `Session::get` / `Querier` 留到《u4-l2 Get 与 Querier》。

## 2. 前置知识

阅读本讲前，你需要已经掌握（这些在前置讲义里讲过）：

- **Session**：`zenoh::open(config).await` 得到的会话句柄，所有实体都在它上面声明（见《u2-l1》）。
- **Key Expression（KE）**：Zenoh 的「地址空间」。一条 KE 表示一批 key 的集合，两端的匹配由 KE 的**相交（intersects）关系**决定，而不是 IP 地址（见《u2-l2》）。
- **Pub/Sub 与 Sample**：`Publisher` 主动推送、`Subscriber` 被动接收，数据单元是 `Sample`，`SampleKind` 只有 `Put`/`Delete` 两值（见《u3-l1》）。
- **Builder 模式与三大 trait**：`declare_*` 返回的都是 builder，必须 `.await`（或同步 `.wait()`）才会真正执行；这套机制由 `Resolvable` / `Wait` / `Resolve` 三个 trait 支撑（见《u1-l4》《u3-l1》）。
- **Handlers 机制**：取数有「回调」和「通道」两种姿势，底层都是 `IntoHandler<T>`（见《u3-l2》）。

**直觉上的关键区别**：Pub/Sub 是「推（push）」——发布者一有新数据就推给所有匹配的订阅者；Query/Reply 是「拉（pull）」——请求方主动问一次，应答方把**自己当前持有的数据**回给它。所以：

- Subscriber 只能看到「声明之后」发生的新数据，**晚加入会错过历史**。
- Queryable 适合暴露「**当前状态 / 历史 / 持久化数据**」：谁问就给谁，问一次给一次。

这也是为什么 Zenoh 的存储后端（见《u11-l3》）天然就是一个 Queryable——它把落盘的数据通过查询接口暴露出来。

> 术语：Zenoh 文档里把 pub/sub 称为 *data in motion*（流动数据），把 query/reply 与存储称为 *data at rest*（静止数据）。本讲就是 *data at rest* 这条链路的「供给侧」。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [`zenoh/src/api/queryable.rs`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/queryable.rs) | 定义 `Query`（请求对象）、`QueryInner`、`Queryable`（可查询端句柄）、`QueryableState`，以及 `Query::reply` / `reply_del` / `reply_err` 三个应答方法的入口。 |
| [`zenoh/src/api/query.rs`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/query.rs) | 定义 `Reply`、`ReplyError`（应答的两种结果）、`ReplyKeyExpr`（是否允许「不相交」的应答）、`QueryConsolidation` 等。 |
| [`zenoh/src/api/builders/queryable.rs`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/builders/queryable.rs) | `QueryableBuilder`：声明 Queryable 用的 builder，提供 `callback` / `with` / `complete` / `allowed_origin` 等方法。 |
| [`zenoh/src/api/builders/reply.rs`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/builders/reply.rs) | `ReplyBuilder`（Put/Delete）、`ReplyErrBuilder`：三种应答的具体 builder，`wait()` 里把应答打包成协议消息发出。 |
| [`zenoh/src/api/session.rs`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs) | `Session::declare_queryable`（公开入口）、`declare_queryable_inner`（注册到会话并向网络发声明）、`handle_query`（把入站查询派发给匹配的 Queryable 回调）。 |
| [`examples/examples/z_queryable.rs`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/examples/examples/z_queryable.rs) | 官方示例：声明 Queryable、`recv_async` 循环、用 `reply` 回一个固定字符串。 |
| [`examples/examples/z_get.rs`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/examples/examples/z_get.rs) | 官方查询客户端示例，本讲用它来测试我们写的 Queryable。 |

公开 API 的导出位置在 [`zenoh/src/lib.rs`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/lib.rs) 的 `query` 模块里，`Query`、`Queryable`、`Reply`、`ReplyError`、`ReplyBuilder` 等都从这里 re-export（见下文 4.1.3）。

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：

1. **Queryable**：声明一个可查询端。
2. **Query**：进入回调的那个「请求对象」。
3. **三种应答**：`reply` / `reply_del` / `reply_err`。
4. **Reply / ReplyError**：应答在请求侧的两种结果封装。

### 4.1 Queryable：声明一个可查询端

#### 4.1.1 概念说明

`Queryable` 是「**愿意回答查询的实体**」。它绑定一条 key expression，表示「**凡落在我的 KE 范围内的查询，都可能派发给我**」。和 `Subscriber` 一样，它由 `Session` 声明、可被 `undeclare`，并在 `Drop` 时自动反声明。

`Queryable` 与 `Subscriber` 的对照：

| 维度 | Subscriber | Queryable |
| --- | --- | --- |
| 模式 | push（被动接收新数据） | pull（请求方问一次，才答一次） |
| 触发时机 | 发布者每次 `put` | 请求方每次 `get` |
| 适合的数据 | 实时事件流 | 当前状态 / 历史 / 持久化数据 |
| 数据单元 | `Sample` | 收到 `Query`，回 `Sample` 或错误 |

一个 Queryable 收到的请求对象叫 **`Query`**；它给请求方的回答可以是「数据（`reply`/`reply_del`）」或「错误（`reply_err`）」。

#### 4.1.2 核心流程

声明并使用一个 Queryable 的标准三步（和 Pub/Sub 几乎对称）：

1. `zenoh::open(config).await` 打开 Session。
2. `session.declare_queryable(ke)` 声明 Queryable（返回 builder）。
3. 选择取数姿势：`.callback(|query| {...})` 或 `.with(某通道)`，再 `.await` 拿到 `Queryable<Handler>`。
4. 用回调处理 `Query`，或用 `queryable.recv_async().await` 在循环里取 `Query`。
5. 在回调里调 `query.reply(...)` 等方法应答。

内部数据流（从声明到派发）大致是：

```
Session::declare_queryable(ke)        // 公开入口
        │
        ▼
QueryableBuilder.wait()               // resolve：把 callback 注册进会话
        │
        ▼
Session::declare_queryable_inner()    // 存入 state.queryables，
        │                             // 并向网络发 DeclareQueryable
        ▼
（远端 get 到来时）
Session::handle_query()               // 按 KE 相交 + complete + origin 过滤
        │                             // 命中的 Queryable 各克隆一份 callback
        ▼
callback.call(query)                  // 每个 Queryable 收到一个 Query
```

#### 4.1.3 源码精读

**公开入口** `Session::declare_queryable` 只做一件事：构造一个 `QueryableBuilder`（默认 `complete=false`、默认 handler、`Locality::Any`）。

[zenoh/src/api/session.rs:1150-1165](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L1150-L1165) ——构造 builder，本身不发任何网络消息。

**`QueryableBuilder`** 是泛型结构，类型参数 `Handler` 随你选的取数方式变化：

[zenoh/src/api/builders/queryable.rs:45-51](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/builders/queryable.rs#L45-L51) ——字段含义：`key_expr` 是「待解析的 KE（可能出错，所以是 `ZResult<KeyExpr>`）」、`complete` 是完整性标记、`origin` 是允许的来源（`Locality`）、`handler` 是取数方式。

它有两个关键方法：

- [`.callback(f)`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/builders/queryable.rs#L69-L75)：把 `Fn(Query)` 闭包包成 `Callback<Query>`，本质是 `.with(Callback::from(f))`。
- [`.with(handler)`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/builders/queryable.rs#L122-L141)：换任意 `IntoHandler<Query>`（如 `flume::bounded(32)`、`FifoChannel`、`RingChannel`）。不指定时默认是 `DefaultHandler`（即 `FifoChannel`）。

还有两个配置项：

- [`.complete(bool)`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/builders/queryable.rs#L199-L202)：把 Queryable 标记为「完整」。一个「完整」的 Queryable 承诺自己**独占**该 KE 的全部数据，请求方（默认 `QueryTarget::BestMatching`）拿到它的数据后就不必再问别人。
- [`.allowed_origin(Locality)`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/builders/queryable.rs#L207-L210)：限定只接收来自「本会话内（`SessionLocal`）」「远端（`Remote`）」或「两者（`Any`，默认）」的查询。

`Locality` 的定义见 [`zenoh/src/api/sample.rs:47-55`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/sample.rs#L47-L55)：`SessionLocal` / `Remote` / `Any`。

**真正执行声明的 `Wait::wait()`**：

[zenoh/src/api/builders/queryable.rs:226-251](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/builders/queryable.rs#L226-L251) ——这里做了四件事：

1. `handler.into_handler()` 把取数方式拆成 `(Callback<Query>, receiver)`——回调交给框架，receiver 存进 `Queryable`。
2. `declare_nonwild_prefix`：对 KE 做一次「非通配前缀」声明优化（与传输层资源编号有关，先不深究）。
3. `declare_queryable_inner(...)`：注册到会话并发网络声明。
4. 返回 `Queryable { inner, handler: receiver, callback_sync_group }`。

**`declare_queryable_inner` 的核心**：

[zenoh/src/api/session.rs:1875-1928](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L1875-L1928) ——要点：

- 把回调包成 `QueryableState`（含 `id`、`key_expr`、`complete`、`origin`、`callback`），存入 `state.queryables: HashMap<Id, Arc<QueryableState>>`（`state` 即 [`session.rs:171`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L171) 的 `queryables` 表）。
- 若 `origin != SessionLocal`，就向网络层 `primitives.send_declare(...)` 发一条 `DeclareBody::DeclareQueryable`，把「我这条 KE 上有 Queryable」广播出去，让路由把匹配的查询转发过来。
- 最后更新 matching status（与《u6-l3 Matching》相关）。

**反声明**：`close_queryable` 会从表里移除，并发 `UndeclareQueryable`：

[zenoh/src/api/session.rs:1930-1953](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L1930-L1953)。

**`Queryable<Handler>` 句柄**本身：

[zenoh/src/api/queryable.rs:743-748](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/queryable.rs#L743-L748) ——它持有 `inner`（含 session 弱引用、id、`undeclare_on_drop` 标志、KE）、`handler`、`callback_sync_group`。

它和 `Subscriber` 一样实现了两件贴心的事：

- [**`Deref` 到 `Handler`**](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/queryable.rs#L902-L908)：所以默认（`FifoChannel` handler）下你能直接写 `queryable.recv_async().await`。
- [**`Drop` 时自动 undeclare**](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/queryable.rs#L881-L889)：只要 `undeclare_on_drop` 为真（默认），句柄一释放就自动 `close_queryable`。

**派发侧** `Session::handle_query`：当一条查询到达本会话（无论来自本会话的 get 还是远端），框架用它过滤「哪些 Queryable 该被通知」：

[zenoh/src/api/session.rs:2869-2928](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L2869-L2928) ——过滤条件（第 2889-2894 行）是三个逻辑的「与」：

1. `origin` 匹配（`Any` 总通过；否则 `local == (origin==SessionLocal)`）；
2. `complete` 或 target 不是 `AllComplete`；
3. **`queryable.key_expr.intersects(key_expr)`**——Queryable 的 KE 与查询 KE **相交**才会被选中。

命中的 Queryable 各克隆一份 callback，然后 `cb.call(query.clone())`（第 2923-2926 行）。注意：**多个 Queryable 都会收到同一个查询**（每个拿到一份克隆的 `Query`），各自独立应答——这就是 `QueryTarget::All` 时「多个应答者」的来源。

> 这个 `intersects` 正是《u2-l2》讲过的「KE 集合相交判断」。Zenoh 的所有匹配，底层都是同一种集合运算。

#### 4.1.4 代码实践

**目标**：跑通官方 `z_queryable` 示例，建立「声明—接收—应答」的体感。

**操作步骤**（需要两个终端，先编译示例 `cargo build --release --examples` 或直接 `cargo run`）：

1. 终端 A 启动 Queryable（默认 KE 为 `demo/example/zenoh-rs-queryable`）：

   ```bash
   cargo run --release -p zenoh-examples --example z_queryable
   ```

2. 终端 B 用 `z_get` 查询它（注意 `-s` 要与 Queryable 的 KE 相交）：

   ```bash
   cargo run --release -p zenoh-examples --example z_get -- -s "demo/example/zenoh-rs-queryable"
   ```

**需要观察的现象**：

- 终端 A 打印 `>> [Queryable ] Received Query '...'`，随后 `Responding ('...': 'Queryable from Rust!')`。
- 终端 B 打印 `>> Received ('demo/example/zenoh-rs-queryable': 'Queryable from Rust!')`。

**预期结果**：查询命中 Queryable 并收到一条 `Put` 应答。试着把终端 B 的 `-s` 改成 `demo/example/nonexistent`（与 Queryable 的 KE 不相交），预期**收不到任何应答**（超时后 `z_get` 静默退出）——这验证了「匹配由 KE 相交决定」。

#### 4.1.5 小练习与答案

**练习 1**：`declare_queryable` 返回的 builder 如果忘记 `.await`（或 `.wait()`），会发生什么？

> **答案**：它只是一个 `QueryableBuilder`，标记了 `#[must_use]`，不会执行注册，也不会收到任何查询。编译器会给出「Resolvables do nothing unless you resolve them」警告。必须 `.await`（或 `.wait()`）才会真正声明。

**练习 2**：把示例里的 `key_expr` 从 `demo/example/zenoh-rs-queryable` 改成通配的 `demo/example/*`，再用 `-s "demo/example/foo"` 查询，还能命中吗？为什么？

> **答案**：能命中。因为 `demo/example/*` 与 `demo/example/foo` **相交**（`*` 匹配单层），`handle_query` 的 `intersects` 过滤通过。注意此时回 `reply` 时应使用**具体的查询 key**（`demo/example/foo`），而不是 Queryable 声明的 `demo/example/*`——见 4.2.1 的说明。

---

### 4.2 Query：进入回调的请求对象

#### 4.2.1 概念说明

回调（或 `recv_async`）收到的就是一个 `Query`。它携带请求方发来的全部信息：

- **`key_expr()`**：被查询的 key expression（**可能含通配符**，如 `demo/example/*`）。
- **`parameters()`**：查询参数（`?` 后面的 `name=value;...`，详见《u4-l3 Selectors》）。
- **`selector()`**：上面两者的组合（`key_expr?parameters`）。
- **`payload()` / `encoding()`**：查询**可以带负载**（不像 HTTP GET 那样一定无 body）。例如「带条件查询」「提交一段过滤脚本」。
- **`attachment()`**：可选的附件（带外元数据）。
- （unstable）`source_info()`：来源信息；（unstable）`priority()` / `congestion_control()` / `express()`：QoS。

> **最容易踩的坑**（源码注释里专门强调）：`Query::key_expr()` **不是**你回 `reply` 时该用的 key——因为它可能含通配符。你应当用「**与本次查询匹配的具体 key**」来应答。例如 Queryable 声明在 `foo/*`，先后收到 `foo/bar` 和 `foo/baz` 两个查询，就该分别回 `foo/bar`、`foo/baz`。这条规则的完整说明见 [queryable.rs 的 `Query` 文档注释（第 149-168 行）](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/queryable.rs#L149-L168)。

#### 4.2.2 核心流程

`Query` 在 `handle_query` 里被构造，然后 clone 给每个命中的 Queryable：

```
handle_query(...)
   ├── 用 key_expr/parameters/qos/... 构造 QueryInner（Arc 共享）
   ├── 构造 Query { inner, eid, value, attachment }
   └── for (eid, cb) in 命中的 queryables:
           query.eid = eid          // 每个 Queryable 对应不同 eid
           cb.call(query.clone())   // 各拿一份克隆
```

**应答的「结束信号」是自动的**：`QueryInner` 实现了 `Drop`，当所有 `Query` 克隆都被释放（即所有 Queryable 处理完毕、`reply` 调用结束）时，`Drop` 会自动向请求方发一条 `ResponseFinal`，表示「我这边的应答发完了」。所以你**不需要**手动「关闭」一个 Query——只要别把它一直持有不放即可。

#### 4.2.3 源码精读

**`Query` 结构**：

[zenoh/src/api/queryable.rs:169-175](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/queryable.rs#L169-L175) ——`inner: Arc<QueryInner>`（可廉价 clone）、`eid`（本 Queryable 的实体 id）、`value: Option<(ZBytes, Encoding)>`（查询负载）、`attachment`。

**`QueryInner` 与自动 `ResponseFinal`**：

[zenoh/src/api/queryable.rs:112-147](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/queryable.rs#L112-L147) ——`Drop for QueryInner`（第 139-147 行）调用 `primitives.send_response_final(...)`，这就是「应答结束」的来源。

**读取方法**（都是一行代理，零开销）：

- [`selector()`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/queryable.rs#L192-L195)：`Selector::borrowed(&key_expr, &parameters)`。
- [`key_expr()`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/queryable.rs#L211-L214)：返回 `&KeyExpr<'static>`。
- [`parameters()`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/queryable.rs#L230-L233)：返回 `&Parameters<'static>`。
- [`payload()`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/queryable.rs#L252-L255)：返回 `Option<&ZBytes>`。

**派发处**（已在 4.1.3 引用）：[session.rs:2869-2928](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L2869-L2928)。注意第 2916-2926 行：只有 `!queryables.is_empty()` 时才构造 `Query` 并派发；如果没有任何 Queryable 命中，连 `Query` 都不会构造，请求方直接拿不到应答。

#### 4.2.4 代码实践

**目标**：观察 `Query` 携带的信息。这是「源码阅读 + 改造」型实践。

**操作步骤**：

1. 打开 [`examples/examples/z_queryable.rs`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/examples/examples/z_queryable.rs)，定位到接收循环 [第 40-65 行](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/examples/examples/z_queryable.rs#L40-L65)。
2. 在循环开头加一行日志（**示例代码**，仅用于观察）：

   ```rust
   println!(">> key='{}', params='{}', has_payload={}",
            query.key_expr(),
            query.parameters(),
            query.payload().is_some());
   ```

3. 用 `z_get` 带参数和负载查询：

   ```bash
   cargo run --release -p zenoh-examples --example z_get -- \
     -s "demo/example/zenoh-rs-queryable?day=2024-01-01;limit=10" -p "hello"
   ```

**需要观察的现象**：Queryable 端应打印类似 `>> key='demo/example/zenoh-rs-queryable', params='day=2024-01-01;limit=10', has_payload=true`。

**预期结果**：证明查询的 key、参数、负载都能在 `Query` 上读到。若你的 `z_get` 版本不支持 `-p/--payload`，则 `has_payload` 为 `false`——这本身也验证了「查询负载是可选的」。

> 待本地验证：不同 `z_get` 版本的 `-p` 参数名是否一致，请以 `--help` 为准。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `handle_query` 在「没有任何 Queryable 命中」时不构造 `Query`？

> **答案**：见 [session.rs:2916](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L2916) 的 `if !queryables.is_empty()` 守卫。没有命中者时构造 `Query` 没有意义；而且若构造了 `Query` 又立刻 drop，`QueryInner::Drop` 会多发一条空的 `ResponseFinal`，可能干扰请求方的结束判定。所以框架先过滤，有命中才构造。

**练习 2**：如果一个 Queryable 收到查询后既不 `reply` 也不 `reply_err`，只是把 `Query` 丢掉，请求方会怎样？

> **答案**：`Query` 被 drop → `QueryInner::Drop` 发 `ResponseFinal` → 请求方收到「该应答者已结束、且没有任何数据」的信号，即该应答者对本次查询**零应答**。请求方不会一直等它（除非整体 `get` 超时）。

---

### 4.3 三种应答：reply / reply_del / reply_err

#### 4.3.1 概念说明

`Query` 提供三个应答方法，对应三种语义：

| 方法 | 应答类型 | 协议层 | 语义 |
| --- | --- | --- | --- |
| `query.reply(ke, payload)` | `Sample`（`Put`） | `ResponseBody::Reply` + `ReplyBody::Put` | 「这条 key 的数据是 ……」 |
| `query.reply_del(ke)` | `Sample`（`Delete`） | `ResponseBody::Reply` + `ReplyBody::Del` | 「这条 key 的数据已被删除」 |
| `query.reply_err(payload)` | `ReplyError` | `ResponseBody::Err` | 「我处理不了这个查询，原因是 ……」 |

`reply` / `reply_del` 与 Pub/Sub 里的 `SampleKind::Put`/`Delete` 完全对称——只不过这次是「被查询时才回」。`reply_err` 则是 Query/Reply 独有的「显式错误应答」。

它们都返回 builder，同样要 `.await`/`.wait()` 才真正发出，并带 `#[must_use]`。

> **默认的「相交校验」**：除非请求方显式允许「不相交应答」（见 4.3.3 的 `accepts_replies`），否则用 `reply`/`reply_del` 回一个**与查询 KE 不相交**的 key 会在发送端直接报错。这能防止「查 `foo/bar` 却答 `foo/baz`」这类意外。

#### 4.3.2 核心流程

三种应答最终都汇入「发一条协议 `Response` 消息」：

```
query.reply(ke, payload)        ─┐
query.reply_del(ke)             ─┼─▶ ReplyBuilder.wait()
                                  │     ├─ 组装 Sample（Put 或 Delete）
                                  │     └─ Query._reply_sample(sample)
                                  │           └─ primitives.send_response(Response{ Reply(Put|Del) })
query.reply_err(payload)        ──▶ ReplyErrBuilder.wait()
                                        └─ primitives.send_response(Response{ Err })
```

注意：

- 应答使用**查询的 QoS**（`query.inner.qos`），不是 Queryable 自己设的。所以 `ReplyBuilder` 上的 `congestion_control`/`priority` 已被标 `#[deprecated]`，调了也没用；只有 `express` 还能改。
- 每条 `Response` 都带 `ext_respid`（应答者 id：`zid` + `eid`），让请求方知道是谁答的。

#### 4.3.3 源码精读

**三个入口方法**（都在 `impl Query`）：

- [`reply`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/queryable.rs#L381-L393)：返回 `ReplyBuilder<'_, 'b, ReplyBuilderPut>`。
- [`reply_err`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/queryable.rs#L398-L404)：返回 `ReplyErrBuilder<'_>`。
- [`reply_del`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/queryable.rs#L414-L424)：返回 `ReplyBuilder<'_, 'b, ReplyBuilderDelete>`。

**`ReplyBuilder`**（typestate 风格，用 `ReplyBuilderPut`/`ReplyBuilderDelete` 区分两种应答）：

[zenoh/src/api/builders/reply.rs:55-64](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/builders/reply.rs#L55-L64)。

- `ReplyBuilderPut` 的 [`wait()`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/builders/reply.rs#L185-L197)：用 `SampleBuilder::put(...)` 组装一个 `Put` 样本，再调 `query._reply_sample(...)`。
- `ReplyBuilderDelete` 的 [`wait()`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/builders/reply.rs#L199-L210)：用 `SampleBuilder::delete(...)` 组装一个 `Delete` 样本，同样调 `query._reply_sample(...)`。

**统一的发送逻辑 `_reply_sample`**：

[zenoh/src/api/queryable.rs:561-604](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/queryable.rs#L561-L604) ——要点：

- 第 563-565 行：若**不允许不相交应答**且回的 key 与查询 KE 不相交，直接 `bail!` 报错（这就是 4.3.1 提到的校验）。
- 第 570-601 行：构造协议 `Response`，按 `sample.kind` 决定 `ReplyBody::Put`（带 payload/encoding/timestamp）还是 `ReplyBody::Del`（无 payload），然后 `primitives.send_response(...)`。

**`reply_err` 的发送逻辑**：

[zenoh/src/api/builders/reply.rs:268-294](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/builders/reply.rs#L268-L294) ——构造 `ResponseBody::Err`（带 `encoding` 和 `payload`），`wire_expr` 用的是**查询自己的 key_expr**（错误应答不涉及「回哪个 key」的问题），发 `Response`。注意错误应答**不做**相交校验。

**关于「是否允许不相交应答」**：请求方通过 selector 参数 `_anyke` 控制，Queryable 侧用 [`Query::accepts_replies()`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/queryable.rs#L445-L454) 读取，返回 [`ReplyKeyExpr`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/query.rs#L250-L257)（`MatchingQuery` 默认 / `Any`）。这在《u4-l2》《u4-l3》会更具体地用到，本讲只需知道「默认要求相交」。

#### 4.3.4 代码实践

**目标**：在同一个 Queryable 里同时演示 `reply` 与 `reply_err`。

**操作步骤**：在 `z_queryable.rs` 的接收循环里，把固定的 `reply` 改成「按查询 key 决定回正常值还是错误」（**示例代码**）：

```rust
use zenoh::Wait; // 同步闭包里用 .wait()

while let Ok(query) = queryable.recv_async().await {
    let key = query.key_expr().as_str();
    if key.ends_with("forbidden") {
        // 用 reply_err 显式报错
        query.reply_err("access denied").wait().ok();
    } else {
        // 正常回一个 Put 样本
        query.reply(key_expr.clone(), payload.clone()).await.ok();
    }
}
```

然后用 `z_get` 分别查询一个正常 key 和一个以 `forbidden` 结尾的 key：

```bash
cargo run --release -p zenoh-examples --example z_get -- -s "demo/example/zenoh-rs-queryable"
cargo run --release -p zenoh-examples --example z_get -- -s "demo/example/forbidden"
```

**需要观察的现象**：第一次查询在 `z_get` 端打印 `>> Received ('...': '...')`（命中 `Ok` 分支）；第二次打印 `>> Received (ERROR: 'access denied')`（命中 `Err` 分支，对应 [`z_get.rs:61-67`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/examples/examples/z_get.rs#L61-L67) 的错误处理）。

**预期结果**：`reply_err` 的 payload 在请求侧被当作 `ReplyError` 呈现。注意第二次查询的 key 必须与 Queryable 声明的 KE 相交才会被送达——你可能需要把 Queryable 声明在更宽的 KE（如 `demo/example/*`）上。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `ReplyBuilder::congestion_control(...)` 和 `priority(...)` 被标了 `#[deprecated]`？

> **答案**：见 [reply.rs:147-155](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/builders/reply.rs#L147-L155)。应答统一使用**查询自带的 QoS**（`ReplyBuilder::new` 里 `qos: query.inner.qos.into()`，[reply.rs:80](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/builders/reply.rs#L80)），Queryable 单方面改 cc/priority 没有意义，所以保留方法签名但标注「调了也没用」。

**练习 2**：`reply` 回一个与查询 KE 不相交的 key，会发生什么？怎样让它成功？

> **答案**：默认会在 `_reply_sample` 里 `bail!` 报错（[queryable.rs:563-565](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/queryable.rs#L563-L565)）。要让其成功，请求方必须在 `get` 时启用「不相交应答」（`accept_replies(ReplyKeyExpr::Any)`，底层加 `_anyke` 参数），此时 `query.accepts_replies()` 返回 `Any`，校验放行。

---

### 4.4 Reply / ReplyError：应答的两种结果封装

#### 4.4.1 概念说明

上面三种应答方法发出的内容，在**请求侧**被统一封装成 `Reply`：

- `reply` / `reply_del` → `Reply { result: Ok(Sample) }`。
- `reply_err` → `Reply { result: Err(ReplyError) }`。

也就是说：**`Reply` 是一个 `Result<Sample, ReplyError>` 的包装**。请求方拿到 `Reply` 后，用 `reply.result()`（借用）或 `reply.into_result()`（拿所有权）取出结果，按 `Ok`/`Err` 分别处理。

> 本讲重点在「供给侧」：理解你调的 `reply_err` 会变成对端的 `ReplyError`、你调的 `reply` 会变成对端的 `Ok(Sample)` 即可。`Reply` 的**消费**细节（多个应答的合并 consolidation 等）留到《u4-l2》。

#### 4.4.2 核心流程

```
Queryable 侧                       请求方（get）侧
─────────────                      ───────────────
query.reply(ke, payload)   ──┐
                              ├──▶ Reply { Ok(Sample{Put}) }
query.reply_del(ke)         ──┤
                              ├──▶ Reply { Ok(Sample{Delete}) }
query.reply_err(msg)        ──┘
                                 ▶ Reply { Err(ReplyError) }
```

`Reply` / `ReplyError` 都是普通数据结构，由 `zenoh::query` 模块导出（[`lib.rs:661-663`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/lib.rs#L661-L663) 把 `Reply`、`ReplyError`、`ReplyKeyExpr` 等从 `api::query` re-export）。

#### 4.4.3 源码精读

**`ReplyError`**：

[zenoh/src/api/query.rs:100-140](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/query.rs#L100-L140) ——字段 `payload: ZBytes` + `encoding: Encoding`，提供 `payload()` / `encoding()` 读取。它实现了 `Display + Error`（[第 142-153 行](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/query.rs#L142-L153)），所以可以直接当 `std::error::Error` 用。

**`Reply`**：

[zenoh/src/api/query.rs:162-201](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/query.rs#L162-L201) ——核心是 `result: Result<Sample, ReplyError>`。三个取值方法：

- [`result()`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/query.rs#L172-L174)：借用的 `Result<&Sample, &ReplyError>`。
- [`result_mut()`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/query.rs#L177-L179)：可变借用。
- [`into_result()`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/query.rs#L182-L184)：拿走所有权。

并且 `From<Reply> for Result<Sample, ReplyError>`（[第 211-215 行](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/query.rs#L211-L215)），所以可以直接 `let r: Result<Sample, ReplyError> = reply.into();`。

请求侧怎么消费它，看 `z_get` 的循环即可：

[examples/examples/z_get.rs:47-69](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/examples/examples/z_get.rs#L47-L69) ——`match reply.result()` 分 `Ok(sample)`（打印 key+payload）和 `Err(err)`（打印错误 payload）两支，正是上面数据流的落点。

#### 4.4.4 代码实践

**目标**：在请求侧观察 `Reply` 的 `Ok`/`Err` 两态（配合 4.3.4 改造过的 Queryable）。

**操作步骤**：

1. 启动 4.3.4 改造后的 Queryable（声明在 `demo/example/*`，`forbidden` 结尾的 key 报错）。
2. 用 `z_get` 查询正常 key 与 `forbidden` key，观察 `z_get.rs` 第 47-69 行两个分支分别命中。

**需要观察的现象**：

- 正常 key → `>> Received ('...': '...')`（`Ok` 分支）。
- `forbidden` key → `>> Received (ERROR: 'access denied')`（`Err` 分支）。

**预期结果**：证明 `reply` → `Reply{Ok}`、`reply_err` → `Reply{Err}` 的对应关系。

> 待本地验证：`z_get` 默认 target 为 `BestMatching`，单 Queryable 场景下行为符合预期；若部署多个 Queryable，需配合 `--target ALL` 才能收到全部应答（详见《u4-l2》）。

#### 4.4.5 小练习与答案

**练习 1**：`ReplyError` 既然实现了 `std::error::Error`，它打印出来长什么样？

> **答案**：见 [query.rs:142-151](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/query.rs#L142-L151) 的 `Display` 实现：形如 `query returned an error with a <N>-byte payload and encoding <encoding>`，包含字节数和编码，但不直接包含 payload 文本——payload 要自己用 `.payload()` 取。

**练习 2**：为什么 `Reply` 用 `Result<Sample, ReplyError>` 而不是「成功就不返回、失败才返回」？

> **答案**：因为一次查询可能有**多个**应答者（`QueryTarget::All`），每个应答者可能成功或失败。`Reply` 把「来自某个应答者的一次结果」统一封装，请求方逐条处理、按 `Ok`/`Err` 分类，才能正确汇总。`ResponseFinal`（`QueryInner::Drop`）才表示「**某个应答者**全部发完」，两者职责不同。

---

## 5. 综合实践

把本讲的 Queryable 声明、`Query` 读取、三种应答串起来，实现一个「**内存键值存储 Queryable**」。

**需求**：

- 进程内维护一个 `HashMap<String, String>` 作为「数据库」。
- 声明一个服务 `kv/*` 的 Queryable。
- 收到查询时：
  - 若该 key 存在 → `reply` 回对应值。
  - 若不存在 → `reply_err` 回错误信息。
- 用官方 `z_get` 作为客户端测试。

下面是一份完整可编译的**示例代码**（不是仓库原有代码；建议放进 `examples/examples/` 自行命名，或单独建一个 bin 试验）：

```rust
// 示例代码：内存键值存储 Queryable
use std::{collections::HashMap, sync::{Arc, Mutex}};
use zenoh::{key_expr::KeyExpr, Config, Wait};

#[tokio::main]
async fn main() {
    zenoh::init_log_from_env_or("error");

    // 1) 内置「数据库」
    let db = Arc::new(Mutex::new(HashMap::from([
        ("kv/name".to_string(), "zenoh".to_string()),
        ("kv/lang".to_string(), "rust".to_string()),
        ("kv/year".to_string(), "2024".to_string()),
    ])));

    // 2) 打开 Session
    let session = zenoh::open(Config::default()).await.unwrap();

    // 3) 声明服务 kv/* 的 Queryable，用回调处理每个 Query
    let queryable = session
        .declare_queryable("kv/*")
        .callback({
            let db = db.clone();
            move |query| {
                // query.key_expr() 是本次被查询的具体 key（如 kv/name），不含通配
                let key = query.key_expr().as_str().to_string();
                let value = db.lock().unwrap().get(&key).cloned();
                match value {
                    Some(v) => {
                        // 命中：回 Put 样本。注意用「具体 key」而非 kv/*
                        query.reply(key.clone(), v).wait().ok();
                    }
                    None => {
                        // 未命中：回错误应答
                        query.reply_err(format!("no value for key '{key}'"))
                            .wait().ok();
                    }
                }
            }
        })
        .await
        .unwrap();

    println!("Queryable ready on kv/*. Ctrl-C to quit.");
    tokio::signal::ctrl_c().await.ok();
    drop(queryable);   // 显式 undeclare（其实 Drop 也会自动做）
}
```

**测试**（另开终端，用官方 `z_get`）：

```bash
# 命中：应打印 >> Received ('kv/name': 'zenoh')
cargo run --release -p zenoh-examples --example z_get -- -s "kv/name"

# 未命中：应打印 >> Received (ERROR: 'no value for key \'kv/missing\'')
cargo run --release -p zenoh-examples --example z_get -- -s "kv/missing"
```

**验收要点**：

1. 命中时走 `reply` → 请求侧 `Reply{Ok(Sample)}` → 打印值。
2. 未命中时走 `reply_err` → 请求侧 `Reply{Err(ReplyError)}` → 打印错误。
3. 若查询一个与 `kv/*` 不相交的 key（如 `other/x`），Queryable 根本收不到查询（`handle_query` 的 `intersects` 过滤），请求侧超时无应答。

> 思考延伸：把 `HashMap` 换成一个真正落盘的存储，这个 Queryable 就成了 Zenoh 存储后端的雏形——这正是《u11-l3 存储与后端》要做的事。

## 6. 本讲小结

- `Session::declare_queryable(ke)` 返回 `QueryableBuilder`，`.await`/`.wait()` 后注册到会话：回调存入 `state.queryables`，并向网络发 `DeclareQueryable`（[session.rs:1875-1928](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L1875-L1928)）。
- `Queryable<Handler>` 与 `Subscriber` 对称：`Deref` 到 handler（默认 `FifoChannel`，可直接 `recv_async`）、`Drop` 时自动 undeclare（[queryable.rs:881-908](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/queryable.rs#L881-L908)）。
- 入站查询由 `handle_query` 按 `origin`/`complete`/`intersects` 三条件过滤后派发给命中的 Queryable，每个拿到一份克隆的 `Query`（[session.rs:2869-2928](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L2869-L2928)）。
- `Query` 携带 `key_expr`/`parameters`/`payload`/`attachment`；其 `key_expr` 可能含通配符，回 `reply` 时要用具体 key。
- 三种应答：`reply`（Put）、`reply_del`（Delete）、`reply_err`（错误）；前两者默认要求回的 key 与查询 KE 相交，应答统一沿用查询的 QoS。
- `QueryInner::Drop` 自动发 `ResponseFinal`，无需手动「关闭」查询；应答在请求侧被封装成 `Reply = Result<Sample, ReplyError>`。
- Queryable 是 pull 模型，天然适合暴露「当前状态/历史/持久化数据」，是存储后端的底层形态。

## 7. 下一步学习建议

- **《u4-l2 Get 与 Querier》**：站到请求侧，学 `Session::get`（一次性查询）与 `Querier`（长生命周期查询器），完整理解 `Reply` 的消费、多个应答的合并（consolidation）与 `QueryTarget`（All / AllComplete / BestMatching）。
- **《u4-l3 Selector 与查询参数》**：深入 `Selector` 的 `key?name=value;...` 语法，以及 Queryable 如何读取并利用 `parameters()` 做过滤。
- **《u6-l3 Matching》**：理解 `declare_queryable_inner` 里调用的 `update_matching_status`，以及如何感知「有没有人在查我」。
- **《u11-l3 存储与后端》**：看 `Volume`/`Storage` trait 如何把一个真实存储变成 Queryable，把本讲的 `HashMap` 升级为可持久化的后端。
- 若想了解 `Query`/`Response`/`ResponseFinal` 在协议层长什么样，可先翻 [`zenoh-protocol` 的 network/zenoh 消息定义](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src)，这会在《u10-l1 协议消息模型》系统讲解。
