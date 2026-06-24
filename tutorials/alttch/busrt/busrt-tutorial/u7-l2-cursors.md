# 游标 cursors：流式数据传输

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚「为什么需要游标」——把一个**有状态的服务端数据流**（数据库结果集、HTTP 流、生成器等）通过 BUS/RT 的 RPC 一块一块地拉到客户端，而不是一次性塞进一个 RPC 响应里。
- 实现 `cursors::Cursor` trait，编写自己的服务端游标（逐条 `next` 与分块 `next_bulk`）。
- 理解 `cursors::Meta` 如何用「完成标志 + 过期时刻」管理游标生命周期，以及 `touch()` 为什么能让「慢但持续活跃」的游标不被清理。
- 理解 `cursors::Map` 如何用一个 `BTreeMap<Uuid, Box<dyn Cursor>>` 把任意多种游标统一调度，并用后台清理任务回收资源。
- 用 `cursors::Payload` 这个 serde 句柄在客户端与服务端之间传递游标身份（UUID + 批量大小），把 RPC 和游标拼成一条完整的流式管道。

本讲依赖 u5-l2（RpcClient / RpcHandlers / processor）。游标本身不是独立的传输机制，而是**构建在 RPC 之上的一层应用协议**：客户端发起一次普通 RPC 拿到游标句柄，之后反复用普通 RPC 拉取数据块。理解这一点，本讲的所有代码就都顺理成章了。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**「拉模型」与「游标」**。假设数据库里有一百万行客户记录，客户端想把它们全部读出来。最朴素的 RPC 写法是服务端一次性 `select *`、序列化成一个大字节串、塞进一个 RPC 响应回传。问题很明显：内存暴涨、单条响应过大、一旦中途断线全部重来。游标（cursor）是一种经典的「拉模型」方案——服务端先**打开**一个流（返回一个不透明句柄，通常是 UUID），客户端再**反复**用这个句柄「取下一块」，直到取完。游标就是服务端那一份「当前读到第几行了」的有状态对象。

**游标与 BUS/RT 的关系**。BUS/RT 不关心游标里装的是什么数据、用什么格式序列化——它只搬运字节。游标被实现成一个可选的 feature（`cursors`），在 `lib.rs` 里被条件编译守卫：

[cursors 模块声明 - src/lib.rs:511-512](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L511-L512) 说明：只有启用 `cursors` feature 时，`cursors` 模块才被编译进库。

而该 feature 的定义是 `cursors = ["rpc", "dep:uuid"]`（见 [Cargo.toml:85](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/Cargo.toml#L85)），即它**强制带出 `rpc`**——这正好印证「游标建在 RPC 之上」。

**四个角色**。本讲的全部源码可以归纳成四个角色，记住它们，后面的源码就是它们之间的对话：

| 角色 | 职责 | 谁实现 |
| --- | --- | --- |
| `Cursor` trait | 单个数据流「取下一块」的抽象 | 你的业务代码（如 `CustomerCursor`） |
| `Meta` | 单个游标的生命周期（完成？过期？） | 库提供，你的游标结构体里**内嵌**一个 |
| `Map` | 管理多个游标：登记、按 UUID 派发、定时清理 | 库提供，放在你的 `RpcHandlers` 里 |
| `Payload` | 线上句柄：UUID + 批量大小，可 serde | 库提供，客户端/服务端共用 |

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/cursors.rs](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/cursors.rs) | 游标层的全部实现：`Cursor` trait、`Meta`、`Map`、`Payload`，约 170 行，是本讲核心。 |
| [examples/server_cursor.rs](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/examples/server_cursor.rs) | 服务端示例：把 PostgreSQL 结果集包成 `CustomerCursor`，挂在 `RpcHandlers` 上。 |
| [examples/client_cursor.rs](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/examples/client_cursor.rs) | 客户端示例：发起 RPC 拿游标，再用 `N`/`NB` 两种方式拉取并打印。 |
| [src/rpc/mod.rs](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/mod.rs) | RPC 协议基础（`RpcEvent::payload`、`parse_method`、`RpcError` 工厂方法），游标错误经它返回。 |
| [src/rpc/async_client.rs](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/async_client.rs) | `RpcClient::call`——客户端每次「取一块」就是一次 `call`。 |

## 4. 核心概念与源码讲解

### 4.1 Cursor trait：单个数据流的抽象

#### 4.1.1 概念说明

`Cursor` 是游标层最底层的抽象。任何「能逐块产出字节」的东西都可以实现它：一个数据库查询流、一个 HTTP 分块下载、一个内存里的 `Vec` 迭代器，甚至一个生成器。trait 本身只规定三个方法，对数据格式和来源**零假设**——序列化成 msgpack、JSON、原始字节都行，只要客户端能解。

为什么是「字节」而不是泛型 `T`？因为游标数据最终要经 BUS/RT 的 RPC 帧传输，而 RPC 帧的载荷就是 `&[u8]`（见 u5-l1）。把序列化责任留给游标实现，可以让 BUS/RT 保持传输无关，也让同一个 `Map` 能同时装下「客户表游标」「日志游标」等完全不同类型的游标（它们都满足 `Cursor`，被擦除成 `Box<dyn Cursor>`）。

#### 4.1.2 核心流程

一个游标被消费的典型流程：

```
客户端                       服务端 Cursor
  │  call("N", cursor_uuid)   │
  │ ─────────────────────────>│  next()  → 取下一条 → 序列化为 Vec<u8>
  │<───────────────────────── │  返回 Some(bytes)
  │  （收到空载荷 → 结束）      │
  │  call("N", cursor_uuid)   │  next()  → 没有了 → mark_finished() → 返回 None
  │ ─────────────────────────>│
  │<───────────────────────── │  返回 None（空载荷）
  │  break                    │  （此后 cleaner 任务会回收该游标）
```

关键约定（与示例代码一致）：

- `next` 返回 `Option<Vec<u8>>`：`Some` 是一条数据，`None` 表示流已耗尽。
- `next_bulk` 返回 `Vec<u8>`（**一个数组**的序列化字节）：空数组表示流已耗尽。
- 两种结束方式都要求游标**自己**调用 `meta().mark_finished()` 标记完成——清理任务据此回收它。

#### 4.1.3 源码精读

trait 定义非常精简：

[cursor trait - src/cursors.rs:127-132](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/cursors.rs#L127-L132) 说明：`#[async_trait]` 要求实现者 `Send + Sync`（trait 对象才能跨 `Map` 共享）；三个方法分别是逐条取、分块取、返回内嵌的 `Meta`。

服务端示例 `CustomerCursor` 的实现把一个 `futures::Stream`（`sqlx` 的查询流）包成游标：

[CustomerCursor 结构与 next - examples/server_cursor.rs:38-73](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/examples/server_cursor.rs#L38-L73) 说明：注意两点——(1) `stream` 被 `tokio::sync::Mutex` 包裹，因为 `futures::Stream` 默认不是 `Sync`，而 RPC 服务端要求 handler（进而要求游标）`Send + Sync`；(2) `next` 在 `try_next` 取不到行时调用 `self.meta().mark_finished()` 并返回 `Ok(None)`。

[next_bulk 实现 - examples/server_cursor.rs:76-98](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/examples/server_cursor.rs#L76-L98) 说明：循环最多取 `count` 条，凑齐即 `break`；若实际取到不足 `count`（流提前耗尽），调用 `mark_finished()`，最后把整个 `Vec<Customer>` 序列化返回。注意 `count == 0` 时直接返回空数组。

> 一个非常重要的工程价值：因为 `Map` 内部把游标存为 `Box<dyn Cursor>`、按 UUID 派发（见 4.4），所以**所有**实现了 `Cursor` 的游标可以共用同一对 RPC 方法 `N`/`NB`。无论你有多少种游标（客户、订单、日志……），客户端代码完全不用改。

#### 4.1.4 代码实践

**目标**：理解 `next` 与 `next_bulk` 的返回语义差异，以及 `mark_finished` 的触发时机。

**步骤**：

1. 打开 [examples/server_cursor.rs:56-98](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/examples/server_cursor.rs#L56-L98)，对照 `next` 与 `next_bulk`。
2. 在 `next` 的 `Ok(None)` 分支前、`next_bulk` 的 `if result.len() < count` 分支里，各加一行 `eprintln!("cursor finished");`（仅作阅读标记，不实际修改源码——你也可以只在脑中标注）。
3. 推演：假设数据库有 0 行，客户端先调一次 `N`，再调一次 `NB(100)`。`next` 会走到哪个分支？`next_bulk` 会返回什么？`mark_finished` 各被调用几次？

**预期结果**（待本地验证）：0 行时，`next` 第一次就取不到行 → `mark_finished()` + `Ok(None)`；`next_bulk(100)` 的 `while` 循环一次都不进 → `result.len()(0) < count(100)` → `mark_finished()` + 返回空数组的序列化字节。两者都标记完成。

#### 4.1.5 小练习与答案

**练习 1**：`next` 返回 `Ok(None)` 和 `next_bulk` 返回空 `Vec<u8>`，客户端分别如何判断「流结束了」？

**答案**：`next` 的 `None` 经 `Map::next` 原样透传成 RPC 的空载荷（`Option` 为 `None`），客户端检查 `result.payload().is_empty()` 即结束（见 [client_cursor.rs:41](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/examples/client_cursor.rs#L41)）。`next_bulk` 永远返回 `Some(数组字节)`，客户端反序列化成 `Vec<T>` 后，判断「这一块长度 < bulk_size」即认为到达最后一块（见 [client_cursor.rs:69](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/examples/client_cursor.rs#L69)）。

**练习 2**：为什么 `CustomerCursor` 要用 `tokio::sync::Mutex` 包裹 `stream`，而不能直接持有裸 `Stream`？

**答案**：`futures::Stream` 只要求 `Send`，不要求 `Sync`；而游标要存进 `Map` 的 `BTreeMap<_, Box<dyn Cursor + Send + Sync>>`，必须 `Send + Sync`。用 `Mutex` 包裹后，外部拿 `&CustomerCursor`（共享引用）也能 `.lock()` 拿到独占访问，从而满足 `Sync`。示例注释里也写明了这一点。

---

### 4.2 Meta：游标的生命周期（完成标志 + 过期时刻）

#### 4.2.1 概念说明

游标是**有状态的服务端资源**：它持有数据库连接/句柄、缓冲区等。如果客户端拉到一半就崩溃、网络断了、或者干脆忘了，这个游标会一直占着资源。所以每个游标都需要两套「死亡条件」：

1. **正常结束**：数据读完了（`mark_finished`）。
2. **超时回收**：很久没人来取（TTL 到期）。

`Meta` 就是把这两套条件封装成一个独立的小对象，你的游标结构体只需**内嵌一个 `Meta` 字段**并实现 `Cursor::meta()` 返回它的引用，`Map` 的清理任务就能据此判断该游标是否还该活着。

注意 `Meta` 不是「额外负担」——如果你的游标直接放进 `Map`，就**必须**有它（示例注释明确写了「must exist in all cursor structures if `busrt::cursors::Map` helper object is used」）。

#### 4.2.2 核心流程

游标存活判定的核心是一个简单逻辑式：

\[
\text{alive} = \neg\,\text{finished} \;\wedge\; (\text{expires} > \text{now})
\]

其中 `expires` 在每次被访问时被刷新（`touch`）：

\[
\text{expires}_{\text{new}} = \text{now} + \text{ttl}
\]

这意味着：

- TTL 衡量的是「空闲时长」，不是「总寿命」。一个慢悠悠但持续在拉的游标，每次 `next`/`next_bulk` 都会 `touch()`，永远不会过期。
- 只有**既没读完、又长时间没人取**的游标才会被 TTL 回收——这正是我们想要的：回收的是「被遗忘」的游标，不是「正在工作」的游标。

清理任务周期性地对整张表做一次过滤，丢弃所有 `!is_alive()` 的游标：

```
每 cleaner_interval：
    for (uuid, cursor) in map:
        keep if cursor.meta().is_alive()
```

#### 4.2.3 源码精读

`Meta` 用一个原子布尔记「完成」、一把同步互斥锁记「过期时刻」：

[Meta 结构 - src/cursors.rs:135-139](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/cursors.rs#L135-L139) 说明：`finished` 用 `AtomicBool`（无锁、可在 `&self` 上标记完成）；`expires` 是 `Instant`，因为要被 `touch` 修改所以放在锁里。注意 `SyncMutex` 在普通构建下是 `parking_lot::Mutex`，在 `rt` feature 下换成不自旋的 `parking_lot_rt::Mutex`（实时安全，见 u7-l1）。

判定与刷新方法：

[is_finished / is_expired / mark_finished / is_alive / touch - src/cursors.rs:151-170](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/cursors.rs#L151-L170) 说明：`is_alive` 就是上面那个逻辑式的直接翻译——「未完成 且 未过期」；`touch` 把 `expires` 整体覆盖为 `now + ttl`，是 TTL 刷新的唯一入口。

示例中游标如何嵌入 `Meta` 并在结束时标记：

[CustomerCursor::new 与 meta() - examples/server_cursor.rs:102-113](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/examples/server_cursor.rs#L102-L113) 说明：构造时 `Meta::new(CURSOR_TTL)` 初始化 `expires = now + 30s`；`meta()` 只是把内嵌字段借出去给 `Map`。

#### 4.2.4 代码实践

**目标**：体会 `touch()` 对 TTL 的「续命」效果。

**步骤**：

1. 阅读 [src/cursors.rs:163-170](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/cursors.rs#L163-L170)，确认 `is_alive` 与 `touch` 的关系。
2. 思想实验：设 `ttl = 10s`，`cleaner_interval = 1s`。某游标在第 0 秒创建，客户端在第 8 秒、第 18 秒、第 28 秒各拉一次，然后不再拉。问：该游标会在第几秒左右被清理？

**预期结果**：每次 `next`/`next_bulk` 在 [cursors.rs:87 与 cursors.rs:100](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/cursors.rs#L87-L100) 都会先 `touch()`，把 `expires` 续到 `调用时刻 + 10s`。最后一次拉取在第 28 秒，`expires = 38s`，于是游标在第 38 秒之后的第一次清理 tick（约第 39 秒）被判定过期并移除。（待本地验证具体 tick 时刻。）

#### 4.2.5 小练习与答案

**练习**：如果把 `ttl` 设得非常短（比如 100ms），而客户端拉取间隔是 200ms，会发生什么？该如何避免？

**答案**：游标会在两次拉取之间的某个清理 tick 上因 `is_expired` 被回收；之后客户端再 `call("N", uuid)` 时，`Map` 找不到该 UUID，返回 `RpcError::not_found`（见 4.4）。避免方法是让 `ttl` 显著大于客户端的最大拉取间隔，或让客户端拉取更频繁。

---

### 4.3 Payload：线上的游标句柄

#### 4.3.1 概念说明

游标在服务端是 `Box<dyn Cursor>`，一个有状态的对象；但客户端拿不到、也不应该拿到这个对象——客户端只需要一个**不透明的身份标识**，用来在后续 RPC 里指明「我要取这个游标的下一块」。这个标识就是 `Payload`。

`Payload` 本质上是一个能被 serde 序列化的小结构体，内含：

- `u`：游标的 `Uuid`（`Map::add` 时生成）。
- `n`：可选的「批量大小」，仅分块拉取 `NB` 时用得上。

它被设计成客户端和服务端**共用**的类型：服务端 `Ccustomers` 方法把新建游标的 UUID 包成 `Payload` 回传；客户端解析出 `Payload`，之后每次调用 `N`/`NB` 又把它原样（可能改了 `n`）序列化回传。两边用同一套 serde 编解码，省去自己定义协议。

#### 4.3.2 核心流程

句柄在两端之间流转的完整路径：

```
服务端                            客户端
  Ccustomers →
    u = map.add(cursor)             call("Ccustomers") →
    return Payload{u}                parse → Payload{u, n=None}
                                  ─ pack 成 Cow 复用 ─
  N  →                             call("N", payload{u})    （逐条）
    map.next(u)                    
  NB →                             call("NB", payload{u, n=100})  （分块，设 n）
    map.next_bulk(u, payload.n)
```

注意客户端示例用了一个小优化：把 `Payload` 预先序列化成 `Vec<u8>`，再包成 `borrow::Cow::Borrowed`（零拷贝借用切片，见 u2-l2），每次循环 `clone` 的只是一个 `&[u8]` 视图，避免每轮重复序列化。

#### 4.3.3 源码精读

`Payload` 的定义刻意小巧，且 `n` 在为 `None` 时不参与序列化：

[Payload 结构与 serde - src/cursors.rs:19-24](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/cursors.rs#L19-L24) 说明：`#[serde(skip_serializing_if = "Option::is_none")]` 意味着逐条拉取（不设 `n`）时，线上的 `Payload` 就是裸 UUID，体积最小。

便捷方法与默认值：

[Payload 方法 - src/cursors.rs:33-50](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/cursors.rs#L33-L50) 说明：`bulk_number()` 在 `n` 为 `None` 时返回默认 `1`；`set_bulk_number` / `clear_bulk_number` 用于在分块与逐条之间切换。`From<Uuid>` 让你写 `Payload::from(u)` 即可创建一个逐条句柄。

客户端拿到句柄、复用句柄的代码：

[客户端解析与复用 Payload - examples/client_cursor.rs:24-46](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/examples/client_cursor.rs#L24-L46) 说明：先反序列化出 `Payload`，再 `to_vec_named` 序列化后包成 `Cow::Borrowed` 复用；循环里反复 `call("N", b_cursor.clone())`。

分块路径里客户端设置批量大小：

[分块客户端 - examples/client_cursor.rs:47-72](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/examples/client_cursor.rs#L47-L72) 说明：`cursor.set_bulk_number(bulk_size)` 后重新打包，服务端 `NB` 分支用 `p.bulk_number()` 取出该值传给 `Map::next_bulk`。

#### 4.3.4 代码实践

**目标**：看清 `Payload` 在两种拉取方式下「线上长什么样」。

**步骤**：

1. 阅读 [src/cursors.rs:19-31](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/cursors.rs#L19-L31) 与 [client_cursor.rs:30-57](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/examples/client_cursor.rs#L30-L57)。
2. 推演：逐条路径 `b_cursor`（未设 `n`）与分块路径 `b_cursor`（`set_bulk_number(100)`）序列化后的字节数，哪个体积更大？大几个字节？

**预期结果**：逐条路径的 `Payload` 因 `skip_serializing_if`，`n` 不出现，只序列化 UUID（16 字节 + msgpack 头）；分块路径多了 `n` 字段（一个 msgpack 正整数）。多出的字节数取决于 msgpack 编码 `100` 的长度（待本地验证，可用 `rmp_serde::to_vec_named` 打印实际字节数确认）。

#### 4.3.5 小练习与答案

**练习**：为什么 `bulk_number()` 在 `n` 为 `None` 时返回 `1` 而不是 `0`？

**答案**：因为 `Payload` 也可能被 `NB`（分块）方法复用；若客户端忘了 `set_bulk_number`，默认 `1` 至少能取到一条数据，而 `0` 会让 `next_bulk(0)` 直接返回空数组（见 [server_cursor.rs:78](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/examples/server_cursor.rs#L78)），误判为「流已结束」。默认 `1` 是更安全的退化行为。

---

### 4.4 Map：多游标管理与后台清理

#### 4.4.1 概念说明

`Map` 是游标层的「调度中心」。一个服务端进程往往同时挂着多个客户端、每种业务又有自己的游标类型，全部混在一起。`Map` 用一张 `BTreeMap<Uuid, Box<dyn Cursor + Send + Sync>>` 把它们统一收纳：

- **登记**：`add(cursor)` 生成 UUID、装箱、插入，返回 UUID 给调用方。
- **派发**：`next(uuid)` / `next_bulk(uuid, count)` 按 UUID 查表，转调对应游标的方法。
- **清理**：一个后台 `tokio` 任务周期性地丢弃完成或过期的游标。

因为存的是 trait 对象，`Map` 对游标的**具体类型一无所知**——这正是「一个 `N`/`NB` 方法服务所有游标类型」能成立的根本原因。

`Map` 通常作为字段放进你的 `RpcHandlers` 结构体（示例里就是 `MyHandlers { pool, cursors }`），随 handler 的生命周期存在。

#### 4.4.2 核心流程

`Map` 的派发流程带三个细节，理解了就掌握了全部：

```
next(uuid):
    读锁查 uuid
    ├─ 找到: cursor.meta().touch()  ← 刷新 TTL（关键！）
    │         return cursor.next().await
    └─ 未找到: Err(RpcError::not_found)   ← 客户端用了一个已过期/不存在的游标

next_bulk(uuid, count):
    读锁查 uuid
    ├─ 找到: cursor.meta().touch()
    │         return Some(cursor.next_bulk(count).await)
    └─ 未找到: Err(RpcError::not_found)

后台 cleaner（每 cleaner_interval）:
    写锁 retain: 保留 cursor.meta().is_alive() 的项
```

三个关键点：

1. **`touch` 在派发时调用**：所以「活跃」的游标永远不过期。
2. **找不到游标返回 `not_found`**：对应客户端拿到一个过期/非法 UUID 的情形。
3. **`Drop` 时中止清理任务**：`Map` 被销毁时，后台 `JoinHandle` 被 `abort`，不会泄漏任务。

#### 4.4.3 源码精读

`Map` 持有一张带 `RwLock` 的表和一个清理任务句柄：

[Map 结构与 new - src/cursors.rs:53-68](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/cursors.rs#L53-L68) 说明：`new(cleaner_interval)` 构造时**立即 spawn** 清理任务（注释写明「cleaner task is automatically spawned」），并把 `JoinHandle` 存起来。

登记与派发：

[add / next / next_bulk - src/cursors.rs:70-105](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/cursors.rs#L70-L105) 说明：`add` 用 `Uuid::new_v4()` 生成身份、`Box::new` 擦除类型；`next`/`next_bulk` 都先 `cursor.meta().touch()` 再转调；注意 `next_bulk` 永远把结果包成 `Some(...)`（数组字节，可能为空），而 `next` 透传 `Option`（可能为 `None`）。两者在 UUID 缺失时都返回 `RpcError::not_found`。

清理任务与析构：

[spawn_cleaner 与 Drop - src/cursors.rs:106-124](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/cursors.rs#L106-L124) 说明：cleaner 用 `tokio::time::interval` 周期 tick，每次拿写锁 `retain(|_, v| v.meta().is_alive())`；`Drop::drop` 取出并 `abort` 任务，保证 `Map` 销毁后不再有空转的清理任务。

服务端把 `Map` 挂进 handler、在 `handle_call` 里派发：

[MyHandlers 与 handle_call - examples/server_cursor.rs:116-151](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/examples/server_cursor.rs#L116-L151) 说明：`Ccustomers` 创建游标并 `cursors.add(...)`，把返回的 UUID 包成 `Payload` 回传；`N`/`NB` 反序列化 `Payload`，转调 `cursors.next` / `cursors.next_bulk`。三个方法加起来不到 30 行，就是一整套流式服务端。

#### 4.4.4 代码实践

**目标**：跟踪一次「客户端用一个不存在的 UUID 拉取」的错误路径。

**步骤**：

1. 阅读 [cursors.rs:85-105](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/cursors.rs#L85-L105) 的 `next`/`next_bulk` 与 [server_cursor.rs:134-147](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/examples/server_cursor.rs#L134-L147) 的 `N`/`NB` 分支。
2. 推演：若客户端伪造一个随机 UUID 直接 `call("N", payload{随机uuid})`，`Map::next` 走哪个分支？返回什么？这个错误最终如何回到客户端？

**预期结果**：`Map::next` 在 `BTreeMap::get` 返回 `None`，返回 `Err(RpcError::not_found(None))`。`RpcResult` 是 `Result<Option<Vec<u8>>, RpcError>`，该 `Err` 被 u5-l2 的 processor 拼成一帧错误回复（`0x12`，code = `RPC_ERROR_CODE_NOT_FOUND`）回送客户端，客户端的 `rpc.call(...).await` 以 `Err` 形式返回。（待本地验证具体 error code 数值。）

#### 4.4.5 小练习与答案

**练习 1**：`add` 用的是 `write().await`，`next`/`next_bulk` 用的是 `read().await`，为什么这样分工？

**答案**：`add` 要修改表（插入），必须写锁；`next`/`next_bulk` 只查表（`get`）后转调游标方法，用读锁即可，允许多个客户端并发拉取不同游标而不互斥。注意 `touch()` 修改的是 `Meta` 内部的原子/锁，与 `Map` 的 `RwLock` 无关，所以读锁下也能刷新 TTL。

**练习 2**：`remove` 方法的文档说「通常不应调用，除非没有清理任务」，为什么？

**答案**：因为 cleaner 任务会自动回收完成/过期的游标，手动 `remove` 容易和 cleaner 竞争、也容易在客户端还在用时提前删掉游标。只有在「没有 spawn cleaner」（比如自己管理生命周期）时才需要手动 `remove`。

---

### 4.5 端到端：用 RPC 把数据库流式传给客户端

#### 4.5.1 概念说明

前四节讲了游标层的零件，本节把它们和 RPC 层（u5-l2）拼起来，看一条完整的数据流如何从服务端的数据库流到客户端的 `println!`。这节的目的是让你看到：**游标层没有引入任何新的传输机制**——「打开游标」和「拉取一块」都只是普通的 `rpc.call`，游标完全是 RPC 之上的一层约定。

#### 4.5.2 核心流程

完整交互（结合两个示例）：

```
[服务端 main]                          [客户端 main]
Broker::new + spawn_unix_server
register_client("db")
RpcClient::new(client, MyHandlers{     Client::connect(/tmp/busrt.sock)
   pool, cursors: Map::new(30s) })     RpcClient::new0(client)
                                       │
                                       │ call("db","Ccustomers")  ← 普通 RPC，开游标
                                       │   → sqlx 查询 → CustomerCursor → cursors.add → UUID
                                       │   ← Payload{u}
                                       │
                                       │ 循环 call("db","N", Payload{u})  ← 逐条
                                       │   ← Some(bytes) / None
                                       │   反序列化 Customer，打印；空则 break
                                       │
                                       │ call("db","Ccustomers")  ← 再开一个新游标
                                       │ set_bulk_number(100)
                                       │ 循环 call("db","NB", Payload{u,100})  ← 分块
                                       │   ← Vec<Customer>；len<100 则 break
```

两个细节值得强调：

- **`RpcClient::new0`**：客户端只调用不响应，所以用 `new0`（`DummyHandlers`，见 u5-l2），不必实现 `RpcHandlers`。
- **`QoS::Processed`**：每一步拉取都带 ACK，保证可靠；游标是「取一条少一条」的有状态消费，丢帧会导致跳号，所以逐块拉取必须可靠。

#### 4.5.3 源码精读

服务端 main 把各零件装配起来：

[服务端 main - examples/server_cursor.rs:153-170](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/examples/server_cursor.rs#L153-L170) 说明：建 broker、监听 unix socket、注册客户端 `db`、建连接池、构造带 `cursors: Map` 的 handler，最后 `RpcClient::new(client, handlers)`——注意 `RpcClient::new` 构造时即自动 spawn processor（见 u5-l2），所以挂载完成代理立刻能响应 `Ccustomers`/`N`/`NB`。

客户端两段式拉取：

[客户端逐条 + 分块 - examples/client_cursor.rs:15-74](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/examples/client_cursor.rs#L15-L74) 说明：先逐条（`N`）拉到空为止，再用新游标分块（`NB`，`bulk_size=100`）再拉一遍，两种方式打印同一份数据。两段都把 `Payload` 预序列化成 `Cow::Borrowed` 复用，避免循环内重复序列化。

拉取所依赖的 RPC 契约（来自 u5-l1/u5-l2，此处仅作引用）：

- 客户端每次「取一块」是一次 [`RpcClient::call` - src/rpc/async_client.rs:370](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/async_client.rs#L370)（trait 声明在 [async_client.rs:115](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/async_client.rs#L115)），它登记 `call_id` 等待回复。
- 服务端 handler 用 [`event.parse_method()` - src/rpc/mod.rs:115](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/mod.rs#L115) 分发方法名、用 [`event.payload()` - src/rpc/mod.rs:80](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/mod.rs#L80) 取参数。

#### 4.5.4 代码实践

**目标**：不依赖数据库，跑通「服务端内存数据 → 游标 → 客户端拉取」的完整链路（这也是综合实践的一部分，此处先做最小版）。

**步骤**：

1. 仿照 `CustomerCursor`，写一个 `MemCursor`：内部持有 `Arc<Mutex<std::sync::Mutex<Vec<Item>>>>` 或 `tokio::sync::Mutex<Vec<Item>>` 加一个下标，`next` 取一条并序列化、取完 `mark_finished` 返回 `None`；`next_bulk(count)` 取最多 `count` 条，不足则 `mark_finished`。
2. handler 里 `Ccustomers` 改成 `self.cursors.add(MemCursor::new(vec![...]))`，`N`/`NB` 不用改（这正是 `Map` 多态派发的好处）。
3. 用 `client_cursor.rs` 不做任何修改直接连上去拉取（数据格式与 `Customer` 一致即可）。

**预期结果**（待本地验证）：客户端应能逐条、再分块打印出你塞进 `MemCursor` 的全部条目，且最后一轮因长度不足而 `break`。

#### 4.5.5 小练习与答案

**练习**：为什么客户端在「逐条」和「分块」两段之间要重新 `call("Ccustomers")` 拿一个**新**游标，而不是复用第一个？

**答案**：因为游标是**单向消耗**的有状态对象——逐条拉取已经把第一个游标读到末尾并 `mark_finished`，cleaner 也迟早会回收它。要重新从头读，必须开一个新游标（新 UUID）。这也说明游标不支持「回退」，只能前进。

---

## 5. 综合实践

**任务**：实现一个**非数据库版本**的游标流式传输，覆盖本讲全部知识点。

**要求**：

1. **服务端**：
   - 定义一个内存数据源，比如 `struct Record { id: u32, value: String }`，预填 250 条。
   - 实现 `MemCursor`：内嵌 `cursors::Meta`，`next` 逐条返回（取完 `mark_finished` + `None`），`next_bulk(count)` 分块返回（不足 `count` 时 `mark_finished`）。注意用 `Mutex` 包裹可变状态以满足 `Send + Sync`。
   - 在 `RpcHandlers` 里放一个 `cursors: cursors::Map`（`Map::new(Duration::from_secs(30))`），实现 `records.open`（开游标）、`N`（逐条）、`NB`（分块）三个方法——`N`/`NB` 可直接复用 `cursors.next` / `cursors.next_bulk`。
   - 用嵌入式 `Broker` 监听 `/tmp/busrt-cursor.sock`，注册客户端（如 `store`），`RpcClient::new` 挂载 handler。
2. **客户端**（一个程序内两段，参照 [client_cursor.rs](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/examples/client_cursor.rs)）：
   - 第一段：`records.open` 拿游标，循环 `N` 逐条拉取并打印，直到空载荷。
   - 第二段：重新 `records.open` 拿新游标，`set_bulk_number(100)`，循环 `NB` 分块拉取并打印，直到某块长度 < 100。
3. **观察与思考**：
   - 两种方式打印的 `id` 序列应完全一致（0..250）。
   - 故意把客户端的 `bulk_size` 设成大于总条数（如 1000），观察 `NB` 第一轮就返回不足、立即 `break`，验证「不足即最后一块」的判定。
   - 故意在两轮 `N` 之间 `sleep` 超过 TTL（如 35s），观察第二次 `N` 是否返回 `not_found`，验证 TTL 回收。

**预期结果**（待本地验证）：逐条与分块均能完整输出 250 条；`bulk_size=1000` 时分块只一轮即止；超过 TTL 后再拉取返回 `not_found` 错误。

**提示**：

- 编译服务端示例需要 `--features broker,ipc,rpc,cursors,sqlx,futures`（见 [Cargo.toml:136](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/Cargo.toml#L136)）；你的内存版不需要 `sqlx`，只需 `cursors`（它自带 `rpc`）。
- 客户端用 `RpcClient::new0` 即可（无需 handler）。
- `Map::new` 会自动 spawn cleaner，`Drop` 时自动回收，无需手动管理。

## 6. 本讲小结

- 游标（`cursors`）是构建在 RPC **之上**的一层拉模型流式协议：一次 `call` 开游标拿 UUID 句柄，之后反复 `call` 拉取数据块；BUS/RT 本身仍是传输无关的「搬运字节」。
- `Cursor` trait 是单个数据流的抽象（`next` 逐条 / `next_bulk` 分块 / `meta` 返回内嵌 `Meta`），数据格式与来源完全由实现者决定。
- `Meta` 用「原子完成标志 + 带 TTL 的过期时刻」管理生命周期；`is_alive = !finished ∧ expires > now`，每次访问 `touch()` 续命——所以活跃游标不过期，只有被遗忘的才回收。
- `Map` 用 `BTreeMap<Uuid, Box<dyn Cursor>>` 多态收纳任意游标，按 UUID 派发，并以后台 `retain(is_alive)` 任务自动清理；这是「一对 `N`/`NB` 方法服务所有游标类型」的根本。
- `Payload` 是客户端/服务端共用的 serde 句柄（UUID + 可选批量大小），`n` 为 `None` 时不参与序列化；客户端常预先序列化成 `Cow::Borrowed` 复用。
- 结束判定有两种约定：`next` 的 `None`（空载荷）与 `next_bulk` 的「块长度 < bulk_size」；游标单向消耗，重读须开新游标。

## 7. 下一步学习建议

- **横向扩展**：尝试用游标封装一个非数据库的真实流——比如 HTTP 分块下载（`reqwest` 的 `bytes_stream`）、或本地大文件按行读取——体会 `Cursor` trait 对数据源的无关性。
- **结合实时性**：阅读 u7-l1，思考若客户端处于实时运行时，分块拉取大块数据时如何用 `direct_alloc_limit` / `AsyncAllocator` 避免阻塞 worker；游标 `next_bulk` 返回的大 `Vec<u8>` 正是「大消息」的典型场景。
- **同步客户端版本**：阅读 u7-l4 的 `sync::rpc`，尝试用同步客户端实现相同的游标拉取逻辑，对比异步 `RpcClient::call` 与同步 `SyncRpc` 的线程模型差异。
- **继续本单元**：本讲之后是 u7-l3（`TopicBroker` 阻塞式主题分发），它是另一种「按主题隔离处理」的工具，可与游标的「按 UUID 隔离状态」对照理解。
