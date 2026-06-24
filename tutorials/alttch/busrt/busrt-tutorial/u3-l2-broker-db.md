# BrokerDb：客户端注册表与订阅映射

## 1. 本讲目标

学完本讲后，你应当能够：

- 说出 `BrokerDb` 里三张核心映射（`clients` / `broadcasts` / `subscriptions`）各自的作用与配置差异。
- 解释一个客户端从「注册 → 写入路由表 → 注销」的完整生命周期，以及 `force_register` 在名称冲突时如何踢掉旧连接。
- 理解主客户端（primary）与二级客户端（secondary）的关系，以及 `%%`（`SECONDARY_SEP`）分隔符的主名提取逻辑。

本讲是上一讲《u3-l1 创建 Broker 与注册内部客户端》的「向下钻取」：上一讲告诉你怎么用 `register_client` 拿到一个句柄，本讲拆开 `Broker` 的内核 `BrokerDb`，看清句柄背后到底维护了哪些数据结构。

## 2. 前置知识

- **Rust 所有权与 `Arc`**：`Arc<T>` 是线程安全的引用计数指针。`BrokerDb` 里的客户端都被包成 `Arc<BusRtClient>`，这样广播 / 发布订阅时只需增加引用计数（O(1)）就能把同一个客户端「扇出」给多个数据结构，而不必复制。
- **`Mutex` 与原子计数**：三张映射各自用一把 `SyncMutex`（`parking_lot::Mutex`，或在 `rt` feature 下换成无自旋锁的 `parking_lot_rt::Mutex`）保护；而收发字节 / 帧数等统计量用 `AtomicU64`，无需加锁即可累加。
- **通配符匹配**：BUS/RT 的主题 / 对端名匹配借鉴了 MQTT 风格——`+` / `?` 表示「匹配单层」，`#` / `*` 表示「匹配剩余所有层」。具体用哪一对，取决于映射的配置（本讲会讲）。
- **`submap` crate**：BUS/RT 把订阅树和广播树的实现外包给了 `submap`，提供 `SubMap`、`BroadcastMap`、`AclMap` 三种结构。本讲只把它们当成「带通配符的映射」来用，不深入其内部。

## 3. 本讲源码地图

本讲几乎全部内容集中在单个文件里：

| 文件 | 作用 |
| --- | --- |
| [src/broker.rs](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs) | `BrokerDb`、`BusRtClient`、注册 / 注销 / 二级客户端逻辑全部在此 |
| [src/lib.rs](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs) | 仅引用常量 `SECONDARY_SEP` |
| [examples/inter_thread.rs](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/examples/inter_thread.rs) | 嵌入式 Broker 的最小蓝本，代码实践以它为基础 |

## 4. 核心概念与源码讲解

### 4.1 BrokerDb：代理的内核数据结构

#### 4.1.1 概念说明

`Broker` 这个结构体本身很薄，真正干活的是它持有的 `Arc<BrokerDb>`。可以把 `BrokerDb` 理解成代理的「路由大脑」——它维护三张表：

1. **`clients`**：客户端注册表。`HashMap<String, Arc<BusRtClient>>`，按客户端**全名**（如 `worker.1`）精确查找，用于点对点 `send`。
2. **`broadcasts`**：广播映射（`BroadcastMap`）。按**对端名掩码**查找一组客户端，用于 `send_broadcast`（如 `worker.*`）。
3. **`subscriptions`**：订阅映射（`SubMap`）。按**主题**查找订阅者，用于 `publish`（如 `news/tech`）。

此外它还持有全局收发计数（`r_frames` / `r_bytes` / `w_frames` / `w_bytes`）、启动时间（用于 uptime 统计）、可选的核心 RPC 客户端，以及一个 `force_register` 开关。

> 关键区别：`clients` 是「精确名 → 客户端」，而 `broadcasts` / `subscriptions` 是「掩码 / 主题 → 一组客户端」。三种通信模式（点对点 / 广播 / 发布订阅）分别对应这三张表。

#### 4.1.2 核心流程

三张表各自独立加锁、互不嵌套，避免死锁。它们的「匹配语法」在 `Default` 实现里被配置成不同风格：

- `broadcasts`：分隔符 `.`，单层通配 `?`，多层通配 `*` —— 适合对端名（`group.sub.client`）。
- `subscriptions`：分隔符 `/`，单层通配 `+`，多层通配 `#` —— 适合主题（`news/tech/alerts`）。

这套差异贯穿整个库：**广播看名字（`.` 分层），订阅看主题（`/` 分层）**。

#### 4.1.3 源码精读

`BrokerDb` 的字段定义：

[src/broker.rs:658-670](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L658-L670) —— 三张映射各持一把 `SyncMutex`，统计量是 `AtomicU64`，`force_register` 是普通 `bool`。

注意 `SyncMutex` 是一个条件编译别名，普通模式用 `parking_lot::Mutex`，`rt` 模式换成实时安全的 `parking_lot_rt::Mutex`：

[src/broker.rs:19-22](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L19-L22) —— 实时特性的「锁切换」入口就在这里。

`Default` 实现配置了两张匹配表的不同通配风格，这是理解广播 / 订阅行为差异的根：

[src/broker.rs:672-695](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L672-L695) —— `broadcasts` 用 `.``?``*`（[L676-L681](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L676-L681)），`subscriptions` 用 `/``+``#`（[L682-L684](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L682-L684)）。

> 小贴士：`force_register` 默认 `false`（[L692](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L692)），只有用 `Broker::create(&Options::default().force_register(true))` 才会打开，见 [src/broker.rs:1341-1353](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1341-L1353)。

#### 4.1.4 代码实践

**实践目标**：建立「三张表 + 两种匹配语法」的直觉。

**操作步骤**：

1. 打开 [src/broker.rs:658-695](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L658-L695)。
2. 画一张三列表格，列出每张映射的：键、值类型、分隔符、单层通配、多层通配、对应通信模式。
3. 对照 `send!` / `send_broadcast!` / `publish!` 三个宏（[L111-L248](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L111-L248)），确认它们分别查的是 `clients` / `broadcasts` / `subscriptions`。

**预期结果**（参考答案）：

| 映射 | 键 | 值 | 分隔符 | 单层 | 多层 | 通信模式 |
| --- | --- | --- | --- | --- | --- | --- |
| `clients` | 精确全名 | `Arc<BusRtClient>` | — | — | — | 点对点 `send` |
| `broadcasts` | 对端名掩码 | `Arc<BusRtClient>` | `.` | `?` | `*` | 广播 `send_broadcast` |
| `subscriptions` | 主题 | `Arc<BusRtClient>` | `/` | `+` | `#` | 发布订阅 `publish` |

#### 4.1.5 小练习与答案

**练习 1**：为什么 `clients` 用 `HashMap` 而不是 `BroadcastMap`？
**答案**：点对点 `send` 是按精确全名查找单个目标，`HashMap` 的 O(1) 精确查找最合适；`BroadcastMap` 的价值在于通配符匹配一组目标，点对点用不上。

**练习 2**：如果要让广播也能按 `/` 分层匹配，需要改哪里？
**答案**：改 [src/broker.rs:676-681](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L676-L681) 里 `BroadcastMap::new().separator('.')` 为 `.separator('/')`，并把通配符也相应调整。但这会破坏「对端名用 `.`」的既有约定，通常不建议。

---

### 4.2 BusRtClient：一个客户端的全部状态

#### 4.2.1 概念说明

`BusRtClient` 是「一个连上代理的逻辑客户端」的完整表示。无论是嵌入式的内部客户端，还是通过 Unix/TCP/WebSocket 接入的外部客户端，在 `BrokerDb` 内部都是同一个结构。它持有：

- **身份**：`name`（全名）、`primary_name`（主名）、`digest`（名字的 sha256 摘要，用作去重 / 比较的键）、`kind`（来源类型）。
- **通信**：`tx`（入站消息的有界通道发送端，消费者从这个通道拿消息）、`disconnect_trig`（代理主动踢人时的触发器）。
- **统计**：`r_frames` / `r_bytes` / `w_frames` / `w_bytes` 四个原子计数器。
- **主 / 二级关系**：`primary: bool` 标志位，以及 `secondaries: HashSet<String>` 记录它名下的二级客户端全名。
- **排除列表**：`has_exclusions` 快速标志位 + `exclusions: AclMap`，用于 `exclude` 机制（见上一讲）。

`kind` 用一个枚举区分来源：

[src/broker.rs:498-504](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L498-L504) —— `Internal`（嵌入式）/ `LocalIpc`（Unix socket）/ `Tcp` / `WebSocket`。

#### 4.2.2 核心流程

构造一个 `BusRtClient`（`BusRtClient::new`）会同时产出三样东西：

1. 客户端结构体本身；
2. 入站通道的接收端 `rx`（`EventChannel`），交给消费循环；
3. 一个 `disconnect_listener`，在代理触发 `disconnect_trig` 时被唤醒（外部客户端的连接处理循环靠它感知「被踢」）。

身份判定有一个关键一行代码：`primary = name == primary_name`。如果二者相等，说明这个名字里没有 `%%` 二级分隔符，它自己就是主客户端。

#### 4.2.3 源码精读

结构体字段全集：

[src/broker.rs:518-538](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L518-L538) —— 注意 `secondaries`、`exclusions` 各自独立加锁，`tx` 是 `async_channel::Sender<Frame>`。

构造函数：

[src/broker.rs:546-585](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L546-L585) —— 三处要点：
- [L555](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L555)：`digest = submap::digest::sha256(name)`，摘要基于**全名**，所以每个二级客户端（`svc%%0`、`svc%%1`）都有不同摘要。
- [L556](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L556)：`async_channel::bounded(queue_size)`，默认 `DEFAULT_QUEUE_SIZE = 8192`（[L50](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L50)），天然背压。
- [L557](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L557)：`primary = name == primary_name`。

身份比较与排序都基于 `digest`，而不是字符串名字：

[src/broker.rs:587-605](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L587-L605) —— `PartialEq` / `Ord` / `Eq` 全部按摘要比较。这让 `submap` 能用固定大小的 32 字节摘要做去重和排序，避免长字符串名的开销。

#### 4.2.4 代码实践

**实践目标**：确认通道容量与 `primary` 判定逻辑。

**操作步骤**：

1. 阅读 [src/broker.rs:546-585](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L546-L585)。
2. 回答：当 `name = "worker.1"`、`primary_name = "worker.1"` 时 `primary` 是什么？当 `name = "worker.1%%0"`、`primary_name = "worker.1"` 时呢？
3. 查 [src/broker.rs:50](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L50) 默认队列大小，并思考：如果消费者来不及处理，发送端会发生什么？（提示：回顾上一讲「内部客户端阻塞、外部客户端被强制断连」）

**预期结果**：

- `"worker.1"`：`primary == true`（主客户端）。
- `"worker.1%%0"`：`primary == false`（二级客户端）。
- 默认队列 `8192`；满队列时，内部客户端的发送方阻塞（`safe_send_frame!` 宏里 `tx.send(...).await`），外部客户端则被 `unregister_client` + `tx.close()` 踢掉（见 [src/broker.rs:83-109](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L83-L109)）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `PartialEq` 用 `digest` 而不是 `name`？
**答案**：摘要比较是固定 32 字节、O(1)，且天然把 `BusRtClient` 变成可在 `submap` / `HashSet` 里高效去重的键；用 `name`（变长 `String`）比较更慢，也无法直接作为定长键。

**练习 2**：`r_frames` / `w_frames` 为什么用 `AtomicU64` 而不是放在 `Mutex` 里？
**答案**：统计量是高频写、低竞争场景，原子操作无锁、不会阻塞消息主链路；放进 `Mutex` 会让每条消息的收发都争锁，拖慢吞吐。

---

### 4.3 注册 / 注入 / 注销：客户端的生命周期

#### 4.3.1 概念说明

一个客户端在 `BrokerDb` 里要走完三步：

- **注册 `register_client`**：对外入口，负责「冲突处理」和「事件通知（announce）」，真正干活的是 `insert_client`。
- **注入 `insert_client`**：把客户端同时写入 `clients` / `broadcasts` / `subscriptions` 三张表，并自动给它订阅 `.broker/warn` 告警主题。
- **注销 `drop_client`**：从三张表移除；如果是主客户端，还要**级联**注销它名下的所有二级客户端。

`unregister_client` 是 `drop_client` 的「带 announce」包装：先记住「之前是否注册过」，调用 `drop_client`，再对主客户端发 `unreg` 事件。

#### 4.3.2 核心流程

**注册（含 force_register 冲突处理）**：

```
register_client(client):
    allow_force = client.primary AND db.force_register   # 仅主客户端可强制
    result = insert_client(client)
    if result == Busy AND allow_force:
        prev = clients.remove(name)                       # 1. 摘掉旧连接
        drop_client(prev)                                 # 2. 清理旧连接的路由表项
        prev.disconnect_trig.trigger()                    # 3. 通知旧连接的 handle_peer 退出
        insert_client(client)                             # 4. 重新注入新连接
    if client.primary:
        announce(reg)                                     # 发 "reg" 事件到 .broker/info
```

**注入**：

```
insert_client(client):
    if not client.primary:
        primary_client = clients.get(client.primary_name)  # 二级客户端必须先有主客户端
        if 不存在: return NotRegistered
    if clients 名字已存在: return Busy
    if 有主客户端: primary.secondaries.insert(client.name) # 登记到主的 secondaries 集合
    broadcasts.register_client(name, client)
    subscriptions.register_client(client)
    subscriptions.subscribe(".broker/warn", client)        # 自动订阅告警主题
    client.registered = true
    clients.insert(name, client)
```

**注销（级联）**：

```
drop_client(client):
    if not client.registered: return                       # 幂等
    client.registered = false
    subscriptions.unregister_client(client)
    broadcasts.unregister_client(name)
    clients.remove(name)
    if client.primary:
        for sec in client.secondaries:                     # 级联所有二级
            if sec 不是 Internal: sec.disconnect_trig.trigger()
            drop_client(sec)                               # 递归
        secondaries.clear()
    else:
        clients.get(primary_name).secondaries.remove(name) # 从主的集合里摘掉自己
```

#### 4.3.3 源码精读

`register_client`（含 force_register 分支）：

[src/broker.rs:725-756](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L725-L756) —— 注意 [L731-L735](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L731-L735) 的 `allow_force` 在 `broker-rpc` 下要求 `client.primary && self.force_register`（只有主客户端能被强制顶替）；冲突分支 [L738-L746](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L738-L746) 依次 `remove` 旧实例 → `drop_client` → `trigger` → 重新 `insert_client`。

`insert_client`（真正的三表写入）：

[src/broker.rs:757-791](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L757-L791) —— 要点：
- [L759-L768](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L759-L768)：二级客户端注入前，主客户端必须已存在，否则 `NotRegistered`。
- [L769](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L769)：用 `hash_map::Entry::Vacant` 原子地判定「名字是否被占用」，避免 TOCTOU。
- [L770-L772](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L770-L772)：把二级全名登记进主客户端的 `secondaries` 集合。
- [L777-L781](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L777-L781)：`register_client` 进订阅树后，**自动订阅** `BROKER_WARN_TOPIC`（`.broker/warn`，定义见 [src/broker.rs:55](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L55)）。
- [L784-L789](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L784-L789)：名字已占用时返回 `Error::busy(...)`。

`unregister_client`（带 announce 的注销）：

[src/broker.rs:804-816](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L804-L816) —— 先记 `was_registered`，再 `drop_client`，主客户端且曾注册过则发 `unreg` 事件。

`drop_client`（含级联）：

[src/broker.rs:817-841](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L817-L841) —— 要点：
- [L818-L819](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L818-L819)：`registered` 标志位让注销**幂等**（`Drop` 和显式 `unregister` 都可能触发）。
- [L825-L836](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L825-L836)：主客户端被注销时，级联断开并清理所有二级客户端；其中非 `Internal` 的二级还会被 `trigger`（让它们的 `handle_peer` 退出）。
- [L837-L839](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L837-L839)：二级客户端被注销时，从主客户端的 `secondaries` 集合里摘掉自己。

> 顺带一看：`broker::Client` 的 `Drop` 实现直接调 `drop_client`（[src/broker.rs:492-496](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L492-L496)），所以内部客户端句柄一离开作用域就会自动从路由表消失（但不会发 announce，注释建议提前手动 `unregister`）。

#### 4.3.4 代码实践

**实践目标**：讲清楚名称冲突时 `force_register` 如何踢掉旧连接（本讲规格指定的阅读型实践）。

**操作步骤**：

1. 读 [src/broker.rs:725-756](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L725-L756) 的 `register_client`，定位 `Err(e) if e.kind() == ErrorKind::Busy && allow_force` 分支。
2. 读 [src/broker.rs:757-791](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L757-L791) 的 `insert_client`，确认名字占用时返回 `Error::busy`（`ErrorKind::Busy`）。
3. 追踪 `prev.disconnect_trig.trigger()` 的效果：在 [src/broker.rs:1847-1868](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1847-L1868) 的 `handle_peer` 的 `tokio::select!` 里，`disconnect_listener` 被唤醒后打印「disconnected by the broker」并执行 `finish_peer!`（再次 `unregister_client`，幂等）。
4. 用下面的最小程序验证「未开启 force_register 时重名被拒绝」（示例代码）：

```rust
// 示例代码：放在 examples/no_force_dup.rs，用 cargo run --example no_force_dup --features broker 运行
use busrt::broker::Broker;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let broker = Broker::new();           // force_register 默认 false
    let _a = broker.register_client("svc").await?;
    match broker.register_client("svc").await {
        Ok(_) => println!("第二次注册成功（未预期）"),
        Err(e) => println!("第二次注册被拒绝: {:?}", e.kind()),
    }
    Ok(())
}
```

**需要观察的现象**：

- 程序打印 `第二次注册被拒绝: Busy`。
- 若把 `Broker::new()` 换成 `Broker::create(&busrt::broker::Options::default().force_register(true))`，第二次注册会成功——旧实例被 `drop_client` 清出三张表，旧外部连接的 `disconnect_listener` 被触发而退出。

**预期结果**：

- 未开 `force_register`：同名二次注册 → `ErrorKind::Busy`。
- 开 `force_register`：同名二次注册 → 旧主客户端被踢（`remove` + `drop_client` + `trigger`），新客户端注入成功。
- 二级客户端名（如 `worker.1%%0`）的主名提取：取第一个 `%%` 之前的子串，即 `worker.1`（详见 4.4）。

> 完整的「外部连接被顶替」场景需要起一个 `spawn_unix_server` 并用两个 IPC 客户端同名连接，建议本地搭建验证（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：`insert_client` 里为什么要先用 `Entry::Vacant` 判定，而不是直接 `clients.insert`？
**答案**：`insert` 会无条件覆盖旧值，无法区分「新建」和「重名冲突」。用 `Entry::Vacant` 能在名字未被占用时才执行后续的三表写入与 `secondaries` 登记，并在已占用时返回 `Busy`，保证注册语义正确。

**练习 2**：`drop_client` 为什么对 `Internal` 二级客户端不调用 `disconnect_trig.trigger()`？
**答案**：内部客户端没有 `handle_peer` 连接循环，也没有人在监听 `disconnect_listener`；触发它毫无意义。只有外部客户端（`LocalIpc`/`Tcp`/`WebSocket`）的 `handle_peer` 才靠 `disconnect_listener` 感知「被代理踢掉」。

---

### 4.4 二级客户端与 SECONDARY_SEP

#### 4.4.1 概念说明

「二级客户端（secondary）」让一个逻辑客户端可以拥有多个物理实例——比如同一个服务名下挂多个连接做负载分担或高可用。BUS/RT 用 `%%` 作为主名与二级编号的分隔符（常量 `SECONDARY_SEP`），二级客户端的全名形如 `worker.1%%0`、`worker.1%%1`。

关键设计：

- **主名提取**：全名里第一个 `%%` 之前的部分就是主名。`worker.1%%0` 的主名是 `worker.1`。
- **共享主名、独立身份**：二级客户端的 `primary_name` 指向主名，但 `digest` 基于完整全名计算，所以每个二级在 `submap` 里是独立条目。
- **生命周期绑定**：主客户端被注销时，级联注销所有二级（见 4.3）；`client.list` RPC 汇报的实例数是 `secondaries.len() + 1`。

#### 4.4.2 核心流程

注册二级客户端的入口是 `Broker::register_secondary_for`：

```
register_secondary_for(primary_client):
    if not primary_client.bus.primary: return NotSupported("not a primary client")
    id = primary_client.secondary_counter.fetch_add(1)    # 从 0 递增
    name = primary.name + "%%" + id                        # 例如 "svc%%0"
    return register_client(name)                           # 复用普通注册流程
```

主名提取在「外部连接握手」和「内部注册」两条路径上用**完全相同**的逻辑：

```
primary_name = name.find("%%").map_or(name, |pos| &name[..pos])
```

#### 4.4.3 源码精读

分隔符常量定义在 `lib.rs`，始终编译（不受 feature 门控）：

[src/lib.rs:49](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L49) —— `pub const SECONDARY_SEP: &str = "%%";`

内部注册路径的主名提取：

[src/broker.rs:1394-1415](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1394-L1415) —— `Broker::register_client`，提取逻辑在 [L1395-L1397](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1395-L1397)：`name.find(SECONDARY_SEP).map_or_else(|| name, |pos| &name[..pos])`。找不到 `%%` 时主名就是全名本身。

二级注册入口：

[src/broker.rs:1419-1429](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1419-L1429) —— 要点：
- [L1420](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1420)：只允许对主客户端注册二级，否则 `NotSupported`。
- [L1421-L1423](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1421-L1423)：用 `secondary_counter.fetch_add(1)` 取号，从 `0` 开始单调递增。
- [L1424](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1424)：`format!("{}{}{}", client.bus.name, SECONDARY_SEP, secondary_id)` → `svc%%0`。
- [L1425](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1425)：委托回 `register_client`，所以二级客户端会走完整的 `insert_client` 流程（包括被登记进主客户端的 `secondaries` 集合，见 [src/broker.rs:770-772](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L770-L772)）。

外部连接握手路径的主名提取（与内部完全一致）：

[src/broker.rs:1772-1774](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1772-L1774) —— `handle_peer` 里对客户端发来的名字做同样的 `find(SECONDARY_SEP)` 切分。这就是为什么外部客户端也可以用 `name%%N` 连上来充当二级实例。

实例数汇报（`client.list` RPC）：

[src/broker.rs:1143](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1143) —— `instances: v.secondaries.lock().len() + 1`，即主客户端自身加其名下二级数量。汇报时只统计主客户端（[L1127](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1127) 的 `c.primary` 过滤），避免重复计数。

实例数可写成：

\[ \text{instances} = |\text{secondaries}| + 1 \]

#### 4.4.4 代码实践

**实践目标**：亲眼看到二级客户端的命名规则与主名提取。

**操作步骤**：把下面的程序存为 `examples/secondary_demo.rs`，用 `cargo run --example secondary_demo --features broker` 运行（示例代码）：

```rust
use busrt::broker::Broker;
use busrt::client::AsyncClient; // 引入 get_name()

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let broker = Broker::new();
    let primary = broker.register_client("svc").await?;
    let sec0 = broker.register_secondary_for(&primary).await?;
    let sec1 = broker.register_secondary_for(&primary).await?;
    println!("primary: {}", primary.get_name());
    println!("sec0   : {}", sec0.get_name());
    println!("sec1   : {}", sec1.get_name());
    Ok(())
}
```

**需要观察的现象**：

- 三个名字分别是 `svc`、`svc%%0`、`svc%%1`。
- `svc%%0` / `svc%%1` 的主名都是 `svc`（即 `primary_name`），但它们是不同的 `BusRtClient`（digest 不同）。

**预期结果**：

```
primary: svc
sec0   : svc%%0
sec1   : svc%%1
```

> 进阶观察（待本地验证）：若给 `broker` 配上核心 RPC（`init_default_core_rpc`），再用 `busrt` CLI 执行 `busrt -N svc broker client.list`，应能看到 `svc` 的 `instances = 3`（自身 + 两个二级）。

#### 4.4.5 小练习与答案

**练习 1**：为什么二级客户端的 `digest` 基于完整全名（`svc%%0`）而不是主名（`svc`）？
**答案**：如果用主名算摘要，同一主名下的所有二级会拥有相同摘要，在 `submap` 的 `HashSet` / 订阅树里会被当成同一条目而互相覆盖，无法各自独立收发消息。基于全名算摘要保证每个物理实例都是独立条目。

**练习 2**：如果直接对 `sec0`（一个二级客户端）再调用 `register_secondary_for(&sec0)`，会发生什么？
**答案**：返回 `Err(NotSupported("not a primary client"))`。因为 [src/broker.rs:1420](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1420) 检查 `client.bus.primary`，二级客户端的 `primary == false`，只有主客户端才能派生二级。

**练习 3**：主客户端被 `drop_client` 时，它的二级客户端会怎样？
**答案**：全部被级联注销（[src/broker.rs:825-836](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L825-L836)）：先从三张表移除，非 `Internal` 的还会被 `disconnect_trig.trigger()` 触发其 `handle_peer` 退出。

---

## 5. 综合实践

把本讲的「三表注册 + 二级客户端 + 自动订阅 `.broker/warn`」串起来验证。

**任务**：创建一个嵌入式 Broker，注册主客户端 `svc` 和它的一个二级 `svc%%0`，再注册一个发布者 `pub`；让 `pub` 向主题 `.broker/warn` 发布一条消息，观察主客户端和二级客户端是否**都**收到。

**预期依据**：[src/broker.rs:777-781](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L777-L781) 里 `insert_client` 对**每个**注册的客户端（无论主 / 二级）都自动 `subscribe(BROKER_WARN_TOPIC, &client)`，所以两者都应收到。

**参考代码**（示例代码，存为 `examples/broker_db_demo.rs`，用 `cargo run --example broker_db_demo --features broker` 运行）：

```rust
use busrt::broker::Broker;
use busrt::client::AsyncClient;
use busrt::QoS;
use std::time::Duration;
use tokio::time::sleep;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let broker = Broker::new();

    let mut primary = broker.register_client("svc").await?;
    let mut sec0 = broker.register_secondary_for(&primary).await?;
    let mut pub0 = broker.register_client("pub").await?;

    let mut rx_p = primary.take_event_channel().unwrap();
    let mut rx_s = sec0.take_event_channel().unwrap();

    // 两个消费者：分别打印主客户端与二级客户端收到的帧
    tokio::spawn(async move {
        while let Ok(frame) = rx_p.recv().await {
            println!("primary 收到 topic={:?} payload={:?}",
                frame.topic(),
                std::str::from_utf8(frame.payload()).unwrap_or("?"));
        }
    });
    tokio::spawn(async move {
        while let Ok(frame) = rx_s.recv().await {
            println!("sec0 收到    topic={:?} payload={:?}",
                frame.topic(),
                std::str::from_utf8(frame.payload()).unwrap_or("?"));
        }
    });

    // 发布到 .broker/warn —— insert_client 已让 svc 与 svc%%0 都订阅了它
    pub0.publish(".broker/warn", "hi".as_bytes().into(), QoS::No).await?;
    sleep(Duration::from_millis(200)).await; // 给消费者一点时间打印
    Ok(())
}
```

**需要观察的现象**：`primary` 和 `sec0` 各打印一行，`topic` 都是 `Some(".broker/warn")`，`payload` 都是 `Ok("hi")`。

**预期结果**：

```
primary 收到 topic=Some(".broker/warn") payload=Ok("hi")
sec0 收到    topic=Some(".broker/warn") payload=Ok("hi")
```

若两者都收到，就同时验证了：三张表的注册、二级客户端的注册与独立身份、`insert_client` 的自动告警订阅、以及 `publish` 经 `subscriptions` 表的扇出。

> 注：本实践不需要 `rpc` feature；`publish` / `take_event_channel` 都来自 `broker` + `client` 模块。运行结果待本地验证。

## 6. 本讲小结

- `BrokerDb` 是代理的内核，维护 `clients`（精确名）、`broadcasts`（`.` 分层对端掩码）、`subscriptions`（`/` 分层主题）三张映射，统计量用原子计数、映射各自独立加锁。
- `BusRtClient` 是「一个逻辑客户端」的完整状态，身份按 `sha256(name)` 摘要比对；`primary = (name == primary_name)` 区分主 / 二级。
- 注册走 `register_client` → `insert_client`：后者用 `Entry::Vacant` 原子判定重名，写入三张表，并把每个客户端自动订阅到 `.broker/warn`。
- 名称冲突时，只有开启 `force_register` 的主客户端会被顶替：摘掉旧实例 → `drop_client` → 触发其 `disconnect_trig` → 重新注入。
- `drop_client` 是幂等的；主客户端注销会级联断开并清理其全部二级客户端。
- 二级客户端全名 = 主名 + `%%` + 递增编号（`SECONDARY_SEP = "%%"`），主名取第一个 `%%` 之前的子串；外部连接握手与内部注册用同一套提取逻辑。

## 7. 下一步学习建议

- 下一讲《u3-l3 三种通信模式：send、broadcast 与 publish》会展开 `send!` / `send_broadcast!` / `publish!` 三个宏，看它们如何分别查 `clients` / `broadcasts` / `subscriptions` 表，以及 `exclude` 排除机制如何与 `has_exclusions` 配合。
- 想了解外部客户端如何连上来触发 `register_client`，可先跳读 [src/broker.rs:1733-1869](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1733-L1869) 的 `handle_peer`（连接生命周期，第六单元 u6-l1 详讲）。
- 对订阅树 / 广播树的通配匹配算法本身感兴趣，可去读 `submap` crate 的 `SubMap` / `BroadcastMap` 文档。
