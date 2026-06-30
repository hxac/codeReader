# Scouting：节点发现

## 1. 本讲目标

本讲讲清楚 Zenoh 的「节点发现」机制——scouting。学完本讲后，你应当能够：

- 说清楚 scouting 到底解决什么问题、以及它**为什么不是必须的**（即使不做任何发现也能 pub/sub/query）。
- 用 `zenoh::scout(...)` 写出一个能在网络上发现 Zenoh 节点的小程序，并正确取出每个 `Hello` 里的 `zid` 与 `whatami`。
- 看懂协议层的 `Scout` / `HelloProto` 消息，理解「scout 多播出去 → 被发现的节点单播回 hello」这一来一回在网络层是如何用 UDP + 退避重发实现的。

本讲是「支撑特性」单元的第一讲，承接《u2-l1 打开一个 Session》里关于 `Config`、`WhatAmI` 与 `ZenohId` 的认知，并为《u7 内部架构》里 Runtime 的 orchestrator 建连流程埋下伏笔。

## 2. 前置知识

在进入本讲前，你需要先理解以下几个概念（均来自前置讲义）：

- **WhatAmI 三种角色**：Zenoh 节点有 router / peer / client 三种角色，默认 peer，由 `Config` 的顶层 `mode` 决定（见《u2-l3 配置系统与 WhatAmI 三种角色》）。本端角色只能靠配置观察。
- **ZenohId（zid）**：每个 Zenoh 节点的唯一标识，配置里不写死则在启动时随机生成（见《u2-l1》）。
- **Config**：`zenoh::Config` 字段私有，用 `insert_json5` / `get_json5` 以「斜杠键 + JSON5 值」读写（见《u2-l3》）。
- **builder 模式与 Resolvable/Resolve/Wait**：几乎所有「创建实体」的调用返回的都是 builder，必须 `.await` 或 `.wait()` 才真正执行（见《u1-l4》《u2-l1》）。
- **多播（multicast）与单播（unicast）**：本讲涉及 UDP 多播。简单说，多播是一次发送、同一组里所有成员都能收到；单播是点对点发给某一个地址。scouting 用多播把「有人在吗」喊出去，被发现的节点用单播把「我在这里」单独回给提问者。

一句话直觉：scouting 就是 Zenoh 在局域网里「喊一嗓子找同伴」的机制，它让一个新启动的节点不必事先知道对端的 IP:端口，而是通过网络自己发现可连接的 Zenoh 节点。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| `zenoh/src/api/scouting.rs` | 公开 API 层：定义 `zenoh::scout` 函数、`Scout` 句柄、`ScoutInner` 及其后台任务。 |
| `zenoh/src/api/builders/scouting.rs` | `ScoutBuilder`：scout 的 builder，提供 `.callback()` / `.with()` 取数方式，并实现 `Resolvable`/`Wait`/`IntoFuture`。 |
| `commons/zenoh-config/src/wrappers.rs` | 公开类型 `Hello`：对内部 `HelloProto` 的稳定封装，暴露 `zid()` / `whatami()` / `locators()`。 |
| `commons/zenoh-protocol/src/scouting/mod.rs` | 协议层：`ScoutingMessage` / `ScoutingBody` 枚举与消息 id 常量。 |
| `commons/zenoh-protocol/src/scouting/scout.rs` | 协议层 `Scout` 消息结构（提问方发出）。 |
| `commons/zenoh-protocol/src/scouting/hello.rs` | 协议层 `HelloProto` 消息结构（被发现方回应）。 |
| `zenoh/src/net/runtime/orchestrator.rs` | 网络层 `Runtime::scout`：真正用 UDP 多播发 Scout、收 Hello 的异步循环（带退避）。 |
| `commons/zenoh-protocol/src/core/whatami.rs` | `WhatAmI` 枚举与 `WhatAmIMatcher` 位掩码匹配器。 |
| `examples/examples/z_scout.rs` | 官方示例：scout 1 秒并打印收到的 Hello。 |

整体分层是：**公开 API（`zenoh::scout`）→ builder（`ScoutBuilder`）→ 内部实现（`_scout`，起后台任务）→ 网络层（`Runtime::scout`，UDP 收发）→ 协议消息（`Scout`/`HelloProto`）**。本讲按这条链路自顶向下讲。

## 4. 核心概念与源码讲解

### 4.1 scout：发起一次节点发现

#### 4.1.1 概念说明

`scout` 是 Zenoh 公开 API 里发起「节点发现」的入口。它的工作方式是：在后台 spawn 一个任务，**周期性地往一个 UDP 多播地址发送 Scout 消息**，同时监听该地址上回来的 Hello 应答，把每个应答通过回调或通道交给用户。

最关键的一点（也是初学者常误解的）：**scouting 不是使用 Zenoh 的前提**。Zenoh 的 crate 文档里明确写着——它「不是必须为了 publish、subscribe 或 query 数据而显式去发现其他节点」：

> [zenoh/src/lib.rs:L67-L69](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/lib.rs#L67-L69) —— `- [scouting] to discover Zenoh nodes in the network. Note that it's not necessary to explicitly discover other nodes just to publish, subscribe, or query data.`

也就是说，只要你在 `Config` 里写死了 `connect/endpoints`（要连的对端地址），节点之间就能直接建连收发数据，scouting 只是一个「我不想写死地址、让网络自己告诉我有谁」的便利机制。它典型用于 peer 模式的局域网自发现。

scouting 接收一个**角色过滤器** `WhatAmIMatcher`：你告诉它「我只想发现 router 和 peer」，它就只把符合角色的 Hello 交给你。

#### 4.1.2 核心流程

一次 `zenoh::scout` 的生命周期如下：

1. 用户调用 `zenoh::scout(what, config)`，得到一个 `ScoutBuilder`。
2. 在 builder 上选一种取数姿势（`.callback(f)` 或 `.with(channel)`），再 `.await` / `.wait()` resolve。
3. resolve 时进入内部 `_scout`：从 `config` 读出多播地址、TTL、网卡；为每块可用网卡绑定一个 UDP socket。
4. spawn 一个后台任务，调用 `Runtime::scout`：它**并发地**做两件事——
   - **发送循环**：周期性地向多播地址 `send_to` 一帧编码好的 Scout 消息，周期按指数退避增长（1s→2s→4s→8s 封顶）。
   - **接收循环**：在每个 socket 上 `recv_from`，解码出 `ScoutingMessage`，若是 Hello 且角色匹配，就通过回调上交给用户。
5. 用户拿到 `Scout` 句柄后，用 `recv_async()` 取 Hello（默认 handler 是 FifoChannel）。
6. `Scout` 被 drop（或显式 `.stop()`）时，`ScoutInner::drop` 用 `terminate` 取消后台任务，发现结束。

发送与接收是并行的，这解释了为什么 scout 是「边喊边听」而不是「喊完再听」。

#### 4.1.3 源码精读

公开入口 `zenoh::scout` 定义在 API 层，签名很轻：接受一个 `what`（可由 `WhatAmI` 经 `|` 运算得到 `WhatAmIMatcher`）和一个 `config`，返回 `ScoutBuilder<DefaultHandler>`：

[zenoh/src/api/scouting.rs:L231-L245](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/scouting.rs#L231-L245) —— `pub fn scout(...)` 把 `what` 转成 `WhatAmIMatcher`、把 `config` `try_into` 成 `Config`，连同默认 handler 装进 `ScoutBuilder`。

它被 re-export 到 crate 根，所以你能直接写 `zenoh::scout(...)`：

[zenoh/src/lib.rs:L283-L288](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/lib.rs#L283-L288) —— 根级 `pub use crate::{config::Config, scouting::scout, session::{open, Session}};`。

`ScoutBuilder` 的取数方式与《u3-l2 Handlers》完全同构：`.callback(f)` 等价于 `.with(Callback::from(f))`，不指定则用默认的 `DefaultHandler`（即 FifoChannel）。resolve 由 `Wait::wait` 与 `IntoFuture` 两条路径实现，最终都汇聚到内部 `_scout`：

[zenoh/src/api/builders/scouting.rs:L144-L153](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/builders/scouting.rs#L144-L153) —— `wait()` 把 handler 拆成 `(callback, receiver)`，调用 `_scout(what, config, callback)`，再把返回的 `ScoutInner` 与 `receiver` 组装成 `Scout`。

内部 `_scout` 负责把 config 翻译成网络参数并起后台任务：

[zenoh/src/api/scouting.rs:L148-L204](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/scouting.rs#L148-L204) —— 读多播地址/TTL/网卡（缺省用 `zenoh_config::defaults::scouting::multicast::*`），为每块网卡 `bind_ucast_port` 绑一个 `UdpSocket`，再用 `TerminatableTask::spawn` 在 `ZRuntime::Acceptor` 上跑 `Runtime::scout`，回调里把 `HelloProto` 转成公开 `Hello` 后 `callback.call(...)`。

注意：`_scout` 不会 panic。若没有可用网卡或绑定全部失败，它返回一个 `scout_task: None` 的 `ScoutInner`——即「静默地什么也不发现」，这也是为什么文档把它定位成「尽力而为」的便利机制。

用户侧的句柄 `Scout<Receiver>` 通过 `Deref` 透传 receiver 的 `recv_async` 等方法，并能在 drop 时停止后台任务：

[zenoh/src/api/scouting.rs:L111-L146](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/scouting.rs#L111-L146) —— `Scout` 与《u3-l1》的 `Subscriber` 对称：`Deref` 到 handler、`.stop()` 显式停止。

[zenoh/src/api/scouting.rs:L79-L86](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/scouting.rs#L79-L86) —— `Drop for ScoutInner` 取出任务并 `terminate(Duration::from_secs(10))`，保证即便忘了 `.stop()`，句柄离开作用域后后台任务也会被回收。

#### 4.1.4 代码实践

运行官方示例 `z_scout`，体验「喊一嗓子」：

[examples/examples/z_scout.rs:L16-L35](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/examples/examples/z_scout.rs#L16-L35) —— 用 `scout(WhatAmI::Peer | WhatAmI::Router, Config::default())` 发现节点，`tokio::time::timeout` 包裹 1 秒的 `recv_async` 循环，最后 `.stop()`。

1. **实践目标**：观察到 scouting 能发现本机其他 Zenoh 节点。
2. **操作步骤**：
   - 终端 A：`cargo run --example z_sub`（或先 `cargo build --release --all-targets` 再跑 `target/release/examples/z_sub`），让它一直运行。
   - 终端 B：`cargo run --example z_scout`。
3. **需要观察的现象**：终端 B 在 1 秒内打印出形如 `Hello { zid: "...", whatami: Peer, locators: [...] }` 的行。
4. **预期结果**：因为 `z_sub` 默认是 peer 角色、且默认开启多播 scouting，所以 `z_scout` 能收到它的 Hello。
5. **注意**：多播能否工作依赖运行环境。某些 CI / 容器环境禁用多播，此时 `z_scout` 可能收不到任何 Hello——这是环境问题而非代码问题。若收不到，可改用「显式 connect」方式验证数据通路（见本讲小结后建议）。

#### 4.1.5 小练习与答案

**练习 1**：如果在一个禁用多播的环境里调用 `zenoh::scout`，会发生什么？会 panic 吗？

> **答案**：不会 panic。`_scout` 在没有可用网卡或 UDP socket 绑定失败时，会返回 `ScoutInner { scout_task: None }`，后台任务根本不启动，相当于「静默地什么也发现不到」。调用方拿到一个永远收不到数据的 `Scout`，`recv_async` 会一直阻塞到超时或 stop。

**练习 2**：`Scout` 句柄为什么既能 `.await` 取数据，又能靠离开作用域自动停止？

> **答案**：`Scout` 内部持有一个 `ScoutInner`，后者实现了 `Drop`——drop 时调用后台任务的 `terminate`。所以无论你显式 `.stop()` 还是让 `Scout` 离开作用域，后台发送/接收任务都会被取消，UDP socket 随之释放。这和 `Subscriber`/`Publisher` 的「Drop 自动 undeclare」是同一套设计。

### 4.2 Hello：被发现节点的「名片」

#### 4.2.1 概念说明

每收到一个应答，scout 回调拿到的就是一个 `Hello`。可以把 `Hello` 理解成被发现节点递过来的「名片」，上面写着三件事：

- **zid**：我是谁（这个节点的 ZenohId）。
- **whatami**：我的角色（router / peer / client）。
- **locators**：你能从哪些地址连到我（一组 locator 字符串，如 `tcp/192.168.1.1:7447`）。

公开 API 暴露的 `Hello` 是个稳定封装，它**不直接等于**协议层的 `HelloProto`。这点很重要：协议层的 `HelloProto` 属于内部 crate（`zenoh-protocol`，不保证稳定），而 `zenoh::scouting::Hello` 是 `zenoh-config` 里对它的 `#[repr(transparent)]` 薄封装，只暴露安全的三个只读访问器，把不稳定的内部表示挡在门外。这正体现了《u1-l4》讲过的「稳定边界」思想。

#### 4.2.2 核心流程

Hello 的产生与消费路径：

1. 被发现节点收到 Scout 后，组装一个 `HelloProto`（带自己的 zid/whatami/locators），单播回提问方。
2. 提问方的 `Runtime::scout` 接收循环解码出 `HelloProto`，经角色匹配后回调。
3. API 层的 `_scout` 把 `HelloProto` 用 `From` 转成公开 `Hello`，再 `callback.call(hello.into())`。
4. 用户从 `Hello` 上读 `zid()` / `whatami()` / `locators()`。

注意第 3 步的 `.into()`：协议层的 `HelloProto` 到公开 `Hello` 的转换就发生在这一刻，这也是用户永远碰不到 `HelloProto` 字段、只能用三个访问器的原因。

#### 4.2.3 源码精读

公开 `Hello` 的定义与访问器：

[commons/zenoh-config/src/wrappers.rs:L102-L133](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-config/src/wrappers.rs#L102-L133) —— `pub struct Hello(HelloProto)` 是 `#[repr(transparent)]` 封装；`zid()` / `whatami()` / `locators()` 三个方法分别委托到内部字段；`empty()` 构造空 Hello（受 `internal` feature 门控，给插件/binding 用）。

协议层 `HelloProto` 的字段结构（内部表示）：

[commons/zenoh-protocol/src/scouting/hello.rs:L101-L107](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/scouting/hello.rs#L101-L107) —— `HelloProto { version, whatami, zid, locators }`。协议文档在同文件里还画出了 Scout→Hello 的一问一答时序，并给出 locator 的示例（`udp/192.168.1.1:7447`、`tcp/192.168.1.1:7447` 等）。

转换发生在网络层回调上交时：

[zenoh/src/api/scouting.rs:L184-L190](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/scouting.rs#L184-L190) —— `Runtime::scout` 的回调里 `callback.call(hello.into())`，这里的 `hello` 是 `HelloProto`，`.into()` 借助 `From<HelloProto> for Hello`（[commons/zenoh-config/src/wrappers.rs:L135-L139](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-config/src/wrappers.rs#L135-L139)）完成内部→公开的转换。

#### 4.2.4 代码实践

阅读型实践：确认公开 `Hello` 与协议 `HelloProto` 的边界。

1. **实践目标**：理解用户只能用三个只读访问器，碰不到内部字段。
2. **操作步骤**：在 `examples/examples/z_scout.rs` 收到 hello 后，尝试改成打印 `hello.zid()`、`hello.whatami()`、`hello.locators()` 三个字段分别输出，而不是整体 `{hello}`。
3. **需要观察的现象**：编译通过；输出分别是一个 ZenohId、一个角色字符串（`peer`/`router`/`client`）、一个 locator 切片。
4. **预期结果**：你**无法**访问 `hello.version` 或 `hello.zid`（裸字段），因为公开 `Hello` 没有暴露它们——这就是稳定封装的意义。若强行访问，编译器会报「no field」错误。
5. **待本地验证**：具体 zid 的字符串形态取决于运行时节点的 id（随机或配置指定）。

#### 4.2.5 小练习与答案

**练习 1**：为什么公开 `Hello` 要包一层，而不是直接把 `HelloProto` 暴露给用户？

> **答案**：因为 `HelloProto` 属于 `zenoh-protocol` 这个**内部 crate**，其字段布局和表示随版本可能变化、不保证稳定。公开 `Hello` 用 `#[repr(transparent)]` 封装后只暴露 `zid()`/`whatami()`/`locators()` 三个稳定方法，既零成本又把不稳定细节挡在稳定边界之内，符合《u1-l4》的 API 门面设计原则。

**练习 2**：`Hello` 的 `locators()` 返回什么？拿到它之后能做什么？

> **答案**：返回 `&[Locator]`，即这个节点声称自己可被连接到的地址列表（如 `tcp/...`、`udp/...`）。拿到 locators 后，提问方可以把它们填进 `Config` 的 `connect/endpoints`，主动向该节点发起连接——这正是 orchestrator 在 scout 到节点后自动建连的依据（见 4.3）。

### 4.3 scouting 协议：Scout 与 Hello 的消息模型与收发

#### 4.3.1 概念说明

前两节讲了「怎么用」和「拿到什么」。本节下钻到协议层与网络层，讲清楚这一问一答在网络上**到底是什么消息、怎么收发**。

协议层把 scouting 定义成一种独立于 transport/network 的轻量消息族，只有两种消息体：

- **Scout**：提问方发出，「网络里有哪些 Zenoh 节点？」它带一个 `what`（`WhatAmIMatcher`，即想发现哪些角色），可选带自己的 zid。
- **Hello（HelloProto）**：被发现方回应，「我在这里」，带自己的 zid、whatami 和 locators。

两者被统一装在 `ScoutingMessage { body: ScoutingBody }` 里，`ScoutingBody` 是个二选一的枚举。消息 id 用单字节标识（`SCOUT = 0x01`、`HELLO = 0x02`），编解码时靠这个 id 分派到具体消息类型。

注意区分两个 WhatAmI 编码层面（避免混淆）：

- **内存里的 `WhatAmI` 枚举**：`Router = 0b001`、`Peer = 0b010`、`Client = 0b100`，是位标志。
- **`WhatAmIMatcher`**：用位掩码表达「一组角色」，靠 `|` 运算组合，靠 `matches()` 判断某角色是否在集合里。

`WhatAmIMatcher` 内部用 `NonZeroU8` 存储并借用第 7 位作「非零哨兵」（保证 `NonZeroU8` 永不为零），匹配逻辑就是按位与：

[commons/zenoh-protocol/src/core/whatami.rs:L177-L179](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/core/whatami.rs#L177-L179) —— `matches` 即 `(self.0.get() & w as u8) != 0`。

`WhatAmI::Peer | WhatAmI::Router` 能生成 matcher，是因为 `WhatAmI` 实现了 `BitOr`：

[commons/zenoh-protocol/src/core/whatami.rs:L292-L300](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/core/whatami.rs#L292-L300) —— 两个 `WhatAmI` 做 `|` 得到 `WhatAmIMatcher`。

至于这两个消息在**线**上的字节级编码（header 字节、zint 变长整数等），属于 codec 范畴，留到《u10-l2 Zenoh080 线编码》细讲；本讲只关注「消息有哪些字段、谁发给谁」。

#### 4.3.2 核心流程

网络层的 `Runtime::scout` 是真正收发 UDP 数据报的地方。它并发跑两个 future：

- **send（发送循环）**：把一个 `Scout { version, what: matcher, zid: None }` 用 `Zenoh080` 编码进一个 `Vec<u8>`，然后**周期性**地对每个 socket `send_to` 到多播地址。周期采用指数退避：初始 1 秒，每轮 ×2，封顶 8 秒。退避公式为：

  \[
  \text{delay}_{n+1} = \min(\text{delay}_n \times 2,\; 8\text{s}),\quad \text{delay}_0 = 1\text{s}
  \]

  退避的意义是：刚启动时密集地问几次尽快发现节点，之后逐渐变慢以减少多播噪声。

- **recv（接收循环）**：对每个 socket 跑一个 `recv_from` 循环，收到的字节用 `Zenoh080` 解码成 `ScoutingMessage`；若 body 是 `Hello` 且 `matcher.matches(hello.whatami)`，就回调上交（回调返回 `Loop::Continue` 表示继续听）；不匹配的 Hello 打一条 warn 日志。

两个循环用 `tokio::select!` 同时驱动，任一结束（实际靠外层 cancellation token 取消）则整体结束。注意：提问方**只处理 Hello**，自己发出的 Scout 不会被自己当作应答处理。

#### 4.3.3 源码精读

协议消息族与 id 常量：

[commons/zenoh-protocol/src/scouting/mod.rs:L20-L36](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/scouting/mod.rs#L20-L36) —— `SCOUT = 0x01`、`HELLO = 0x02`；`ScoutingBody` 是 `Scout(Scout) | Hello(HelloProto)` 的枚举，`ScoutingMessage` 只包一个 `body`。编解码按 id 分派到这两个变体。

Scout 消息结构与官方时序图：

[commons/zenoh-protocol/src/scouting/scout.rs:L74-L79](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/scouting/scout.rs#L74-L79) —— `Scout { version, what: WhatAmIMatcher, zid: Option<ZenohIdProto> }`。同文件 L16–L67 的协议注释画出了「A 发 SCOUT 给 B、C；B、C 各自单播回 HELLO 给 A」的标准时序。

发送循环（编码 + 多播 + 退避）：

[zenoh/src/net/runtime/orchestrator.rs:L902-L946](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/runtime/orchestrator.rs#L902-L946) —— 构造 `Scout` 消息、用 `Zenoh080::new()` 编码进 `wbuf`，循环里对每个 socket `send_to(mcast_addr)`，`sleep(delay)` 后按 `SCOUT_PERIOD_INCREASE_FACTOR` 增长 delay 直到 `SCOUT_MAX_PERIOD`。

接收循环（解码 + 角色匹配 + 回调）：

[zenoh/src/net/runtime/orchestrator.rs:L947-L981](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/runtime/orchestrator.rs#L947-L981) —— 每个 socket 一个 `recv_from` 循环，解码 `ScoutingMessage`，仅对 `ScoutingBody::Hello` 且 `matcher.matches(hello.whatami)` 的调用回调 `f(hello.clone()).await`。

退避常量：

[zenoh/src/net/runtime/orchestrator.rs:L51-L54](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/runtime/orchestrator.rs#L51-L54) —— `RCV_BUF_SIZE = u16::MAX`、`SCOUT_INITIAL_PERIOD = 1s`、`SCOUT_MAX_PERIOD = 8s`、`SCOUT_PERIOD_INCREASE_FACTOR = 2`。

`Runtime::scout` 的签名（接受一组 socket、一个 matcher、多播地址、一个返回 `Loop` 的异步回调）：

[zenoh/src/net/runtime/orchestrator.rs:L892-L901](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/runtime/orchestrator.rs#L892-L901) —— 注意 `f: Fn(HelloProto) -> Fut`，回调收的是协议层 `HelloProto`，由上层 `_scout` 再 `.into()` 成公开 `Hello`。

#### 4.3.4 代码实践

按讲义规格，写一个 5 秒的发现器，打印每个 Hello 的 zid 与 whatami。

1. **实践目标**：用 `zenoh::scout(WhatAmI::Peer | WhatAmI::Router, config)` 自己写一个发现器，区分打印每个节点的 zid 与 whatami。
2. **操作步骤**：
   - 在 `examples` crate 里仿照 `z_scout.rs` 新建一个 example（或直接改 `z_scout.rs` 调试），核心代码如下（**示例代码**，非项目原有）：

     ```rust
     use std::time::Duration;
     use zenoh::{config::WhatAmI, scout, Config};

     #[tokio::main]
     async fn main() {
         zenoh::init_log_from_env_or("info");
         println!("Scouting for 5s...");
         // 只发现 peer 和 router；Config::default() 默认开启多播 scouting
         let receiver = scout(WhatAmI::Peer | WhatAmI::Router, Config::default())
             .await
             .unwrap();

         // 用 timeout 限制 5 秒
         let _ = tokio::time::timeout(Duration::from_secs(5), async {
             while let Ok(hello) = receiver.recv_async().await {
                 println!(
                     "discovered: zid={}, whatami={}",
                     hello.zid(),
                     hello.whatami()
                 );
             }
         })
         .await;

         receiver.stop();
     }
     ```
   - 终端 A 保持一个 `z_sub` 或 `zenohd` 运行；终端 B 运行上面的发现器。
3. **需要观察的现象**：5 秒内逐行打印 `discovered: zid=..., whatami=peer`（或 `router`）；5 秒到后程序退出。
4. **预期结果**：每个被发现的节点至少出现一次（因退避，前几秒更密集）。若把过滤器改成 `WhatAmI::Client`，而本机只有 peer/router 节点，则 5 秒内一行也不会打印——验证了 `matcher.matches` 的过滤作用。
5. **待本地验证**：多播在受限环境可能不可用；若 5 秒无输出，请先确认环境支持 UDP 多播，或改用显式 connect 验证数据通路。

#### 4.3.5 小练习与答案

**练习 1**：为什么发送循环要「指数退避」（1s→2s→4s→8s）而不是固定周期？

> **答案**：节点刚启动时希望尽快发现同伴，所以初期密集发送；但多播是「广播式」流量，长期高频发送会持续占用局域网带宽并打扰所有节点。指数退避让「冷启动快速发现」与「稳态低噪声」兼得——这是分布式发现协议（如 Zenoh scouting）的常见手法。

**练习 2**：接收循环里收到一个 `whatami` 不在 matcher 集合里的 Hello 会怎样？

> **答案**：不会上交给用户回调，而是打一条 `warn` 日志（`Received unexpected Hello`）。只有 `matcher.matches(hello.whatami)` 为真的 Hello 才会触发回调。这保证了「我只发现 router 和 peer」的过滤语义在网络层就生效，client 的 Hello（若你没收 client）不会送到你的应用。

**练习 3**：`ScoutingBody` 为什么设计成 `Scout | Hello` 的枚举，而不是两个独立的消息类型？

> **答案**：因为两者共用同一条 UDP 多播通道、同一种 `ScoutingMessage` 帧格式。用枚举统一表示后，编解码只需一个入口：按消息 id 字节（0x01/0x02）分派到 `Scout` 或 `HelloProto` 变体。接收方在同一个 `recv_from` 循环里解码，遇到 Scout 忽略（提问方不处理自己的提问）、遇到 Hello 上交，逻辑统一。

## 5. 综合实践

把本讲三个模块串起来，完成一个「带过滤与去重展示」的发现器：

1. **任务**：写一个 10 秒的 scout，过滤器为 `WhatAmI::Peer | WhatAmI::Router`；用 `flume::bounded(32)` 作为取数通道（即 `.with(flume::bounded(32))`，复习《u3-l2》）；收到 Hello 后按 `zid` 去重，最终用一个 `HashSet<ZenohId>` 统计 10 秒内共发现了多少个**不同**的节点，并打印每个节点的 `zid / whatami / locators`。
2. **提示**：
   - `.with(flume::bounded(32))` 返回的 `Scout` 经 `Deref` 暴露 `recv_async`（见 4.1.3 的 builder 文档示例）。
   - `hello.zid()` 返回的 `ZenohId` 实现了 `Hash`/`Eq`，可直接入 `HashSet`。
   - 用 `tokio::time::timeout(Duration::from_secs(10), ...)` 包裹接收循环。
3. **预期**：输出形如 `discovered 3 unique node(s)` 加每个节点的名片信息。
4. **延伸思考**：如果不做去重，同一个节点会被打印多次——结合 4.3.2 的退避机制解释为什么（提问方周期性重发 Scout，被发现的节点每次都会回 Hello）。

> 注：本任务为「源码阅读 + 编写」型实践，运行结果依赖多播环境，若环境不支持多播，可作为代码阅读练习完成，并标注「待本地验证」。

## 6. 本讲小结

- **scouting 是什么**：Zenoh 在局域网里用 UDP 多播「喊一嗓子」发现其他 Zenoh 节点的机制；它返回被发现节点的「名片」`Hello`（zid + whatami + locators）。
- **scouting 不是必须的**：只要在 `Config` 里写死 `connect/endpoints`，节点间就能直接收发数据；scouting 只是省去手写对端地址的便利机制，且失败时静默（不 panic）。
- **公开入口**：`zenoh::scout(what, config)` 返回 `ScoutBuilder`，经 `.callback()` / `.with()` 选取数姿势、`.await`/`.wait()` resolve 后得到 `Scout` 句柄；`Scout` 靠 `Deref` 暴露 `recv_async`，靠 `Drop` 自动停止后台任务。
- **Hello 的稳定边界**：公开 `Hello` 是对内部 `HelloProto` 的 `#[repr(transparent)]` 封装，只暴露 `zid()`/`whatami()`/`locators()`，把不稳定的协议表示挡在门外。
- **协议消息族**：scouting 只有 `Scout`（提问）和 `HelloProto`（应答）两种消息，装在 `ScoutingMessage` 里、靠单字节 id（0x01/0x02）分派；`WhatAmIMatcher` 用位掩码过滤角色。
- **网络层实现**：`Runtime::scout` 并发跑「发送循环（编码 Scout→多播→指数退避 1s→8s）」和「接收循环（解码→`matcher.matches`→回调上交 Hello）」，提问方只处理 Hello。

## 7. 下一步学习建议

- **下一步讲义**：《u6-l2 Liveliness：存活检测》讲另一种「感知节点/资源是否还在」的机制——与 scouting 的「发现」不同，liveliness 关注的是「在线/离线」的持续通知。
- **深入 Runtime 建连**：scouting 发现到 locators 之后，orchestrator 会据此自动建连。这部分在《u7-l3 Runtime 编排器：scouting 与建连》详讲，建议读完本讲后接着读 `zenoh/src/net/runtime/orchestrator.rs` 里 `Runtime::scout` 之后的 `connect` 与 `PeerConnector` 逻辑。
- **协议编码细节**：本讲刻意回避了 Scout/Hello 的字节级编码（header 字节、zint）。若想看「一条 Scout 消息如何变成字节」，去读《u10-l2 Zenoh080 线编码》与 `commons/zenoh-codec/` 下对 scouting 消息的 codec 实现。
- **配置项**：scouting 的多播地址/TTL/网卡/开关都可在 `Config` 的 `scouting/multicast/*` 下调整（见《u2-l3》），建议动手改 `scouting/multicast/enabled=false` 观察 scout 静默无输出的行为。
