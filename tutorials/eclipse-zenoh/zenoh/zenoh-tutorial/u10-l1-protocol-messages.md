# 协议消息模型（network/transport/scouting/zenoh）

> 本讲进入 Zenoh 的「内部 crate」`commons/zenoh-protocol`。它是《u7-l2 Primitives 与 Mux/DeMux》里 `Primitives`/`EPrimitives` 接口背后那一整套消息定义：网络上传的每一个字节、路由器转发的每一条声明、订阅端收到的每一条数据，最终都被还原成这里定义的某种消息结构。本讲不读 `zenoh-codec`（那是《u10-l2》的事），只把「有哪些消息、它们如何分层嵌套」这张地图建立起来。

## 1. 本讲目标

学完本讲，你应当能够：

1. 说出 Zenoh 协议消息的**四层划分**——`zenoh`（数据体）/ `network`（路由信封）/ `transport`（线路帧）/ `scouting`（发现），以及它们之间的**嵌套关系**：一个 `Put` 被包进 `Push`，再被包进 `Frame` 才上线。
2. 掌握 `core` 模块里的基础「词法」：`ZenohId`、`WhatAmI`、`WireExpr`、`Timestamp`、`Parameters`、`Encoding`，以及 `Priority`/`Reliability`/`CongestionControl` 这三个 QoS 枚举。
3. 看懂 `network` 层的七种消息体（`Declare`/`Push`/`Request`/`Response`/`ResponseFinal`/`Interest`/`OAM`）与 `NetworkMessage` 信封，并能复述 `Declare`/`Interest` 如何驱动「声明式路由」。
4. 给定任意一个消息名（如 `Put`、`Frame`），能指出它属于哪一层、做什么用、是否携带 `WireExpr`。

## 2. 前置知识

本讲假设你已经读过：

- **《u7-l2 Primitives 与 Mux/DeMux》**：知道 `Primitives`（消息进路由织网）与 `EPrimitives`（消息送出某张 face）这一对接口；本讲讲的正是这两个接口里来回传递的「消息长什么样」。
- **《u3-l1 Pub/Sub 基础》《u4-l1 Queryable》《u4-l3 Selector》**：知道 `Put`/`Delete`、`Query`/`Reply`、`Selector = Key Expression + Parameters` 这些**公开 API** 概念。本讲会看到它们在协议层对应的**内部消息体**。

几个铺垫性的术语：

- **crate 边界**：`zenoh-protocol` 是内部 crate（`lib.rs` 顶部就写着「intended for Zenoh's internal use」），不保证稳定，写应用不应直接依赖；但读源码理解原理时它是主角。
- **零拷贝缓冲 `ZBuf`/`ZSlice`**：协议里凡是「负载」字段（如 `Put.payload`）几乎都用 `ZBuf` 而非 `Vec<u8>`，这是为了零拷贝（详见《u10-l3》）。
- **扩展（extension / ext）**：Zenoh 的消息采用「固定 header + 可选扩展」的 TLV 风格设计，几乎每条消息都带一组 `ext_qos`/`ext_tstamp`/`ext_nodeid` 等可选字段。本讲会点到为止，编解码细节留给《u10-l2》。

## 3. 本讲源码地图

本讲围绕 `commons/zenoh-protocol/` 下的 6 个顶层模块文件展开，外加若干子模块：

| 文件 | 作用 |
|------|------|
| [commons/zenoh-protocol/src/lib.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/lib.rs) | crate 入口，声明 6 个子模块（`common/core/network/scouting/transport/zenoh`），定义协议版本号 `VERSION`。 |
| [commons/zenoh-protocol/src/core/mod.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/core/mod.rs) | **core 基础类型**：`ZenohIdProto`、`Priority`、`Reliability`、`CongestionControl`，并 re-export 子模块的 `WhatAmI`/`WireExpr`/`Encoding`/`Parameters`/`Timestamp`。 |
| [commons/zenoh-protocol/src/network/mod.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/network/mod.rs) | **network 层**：`NetworkBody` 七种消息体、`NetworkMessage` 信封、`Mapping`、消息 id 常量、以及统一抽取 QoS/WireExpr 的 `NetworkMessageExt` trait。 |
| [commons/zenoh-protocol/src/transport/mod.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/transport/mod.rs) | **transport 层**：`TransportBody` 十种消息体、`TransportMessage`、批大小 `BatchSize`、低延迟变体。 |
| [commons/zenoh-protocol/src/zenoh/mod.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/zenoh/mod.rs) | **zenoh 层数据体**：`PushBody`/`RequestBody`/`ResponseBody` 三个枚举，把 `Put`/`Del`/`Query`/`Reply`/`Err` 装进 network 层。 |
| [commons/zenoh-protocol/src/scouting/mod.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/scouting/mod.rs) | **scouting 层**：`ScoutingBody`（`Scout`/`HelloProto`），独立的发现协议，不走 transport 帧。 |

辅助子模块（按需精读）：`core/whatami.rs`、`core/wire_expr.rs`、`core/parameters.rs`、`core/encoding.rs`、`network/declare.rs`、`network/interest.rs`、`transport/frame.rs`、`transport/open.rs`、`scouting/scout.rs`、`scouting/hello.rs`、`zenoh/put.rs`、`zenoh/query.rs`、`zenoh/reply.rs`。

## 4. 核心概念与源码讲解

### 4.1 core 基础类型：协议的「词法表」

#### 4.1.1 概念说明

`core` 模块不定义任何「消息」，它定义的是**所有消息都会用到的原子类型**——可以理解成一本协议词典里的「字母表」。无论哪一层消息，只要涉及「谁发的」「发的是什么角色」「地址是什么」「数据怎么解释」，都会回过头来引用 `core` 里的类型：

- **身份**：`ZenohIdProto`（节点唯一 id）、`WhatAmI`（节点角色）。
- **地址**：`WireExpr`（线上的 key expression 表示）。
- **时间**：`Timestamp`（来自外部 `uhlc` crate 的混合逻辑时钟时间戳）。
- **查询参数**：`Parameters`（`a=b;c=d|e` 格式的键值视图）。
- **数据解读**：`Encoding`（负载的 MIME 风格标签）。
- **QoS**：`Priority`、`Reliability`、`CongestionControl`（这三个已经在《u3-l3》从公开 API 角度讲过，这里看它们的协议层定义）。

#### 4.1.2 核心流程：类型一览

下表把本节要讲的 core 类型收拢在一起（行末给出定义所在文件）：

| 类型 | 一句话 | 定义位置 |
|------|--------|----------|
| `ZenohIdProto(uhlc::ID)` | 节点全局唯一 id，1–16 字节 | `core/mod.rs` |
| `WhatAmI` | Router/Peer/Client 三角色，比特位编码 | `core/whatami.rs` |
| `WireExpr<'a>` | 线上 key expression：`scope` id + `suffix` + `mapping` | `core/wire_expr.rs` |
| `Timestamp` | 时间值(NTP64) + 逻辑计数 + 时钟 id，外部 `uhlc` | `core/mod.rs` re-export |
| `Parameters` | `;`/`=`/`|` 三分隔符的键值视图 | `core/parameters.rs` |
| `Encoding` | `id: u16` + 可选 `schema`，MIME 风格 | `core/encoding.rs` |
| `Priority` | 8 档优先级，`Control=0`…`Background=7` | `core/mod.rs` |
| `Reliability` | `BestEffort=0` / `Reliable=1` | `core/mod.rs` |
| `CongestionControl` | `Drop=0` / `Block=1`（+ unstable `BlockFirst=2`） | `core/mod.rs` |

#### 4.1.3 源码精读

**ZenohIdProto——可变长度的节点 id。** 它是对外部 `uhlc::ID` 的透明包装，最大 16 字节、最小 1 字节，所以线编码时实际长度可变（省字节）：

[commons/zenoh-protocol/src/core/mod.rs:65-67](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/core/mod.rs#L65-L67) 定义了 `ZenohIdProto(uhlc::ID)` 这一 `#[repr(transparent)]` newtype；`MAX_SIZE = 16` 在 [commons/zenoh-protocol/src/core/mod.rs:69-70](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/core/mod.rs#L69-L70)。它还能 `into_keyexpr()`（变成 hex 字符串的 `OwnedKeyExpr`）和作为 `Timestamp` 的时钟 id（[commons/zenoh-protocol/src/core/mod.rs:228-238](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/core/mod.rs#L228-L238)）——这就是 adminspace 里 `@/<zid>/...` 这类 key 的来源。

> 注意：公开 API 里叫 `ZenohId`，内部协议层叫 `ZenohIdProto`。`Timestamp` 与 `TimestampId` 同理来自 `uhlc`（[commons/zenoh-protocol/src/core/mod.rs:29](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/core/mod.rs#L29) re-export `Timestamp, NTP64`，[commons/zenoh-protocol/src/core/mod.rs:34](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/core/mod.rs#L34) 定义 `TimestampId`）。详见《u6-4 Timestamp》。

**WhatAmI——三种角色的比特位编码。** 三个变体分别占一位：

[commons/zenoh-protocol/src/core/whatami.rs:38-45](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/core/whatami.rs#L38-L45)：

```rust
pub enum WhatAmI {
    Router = 0b001,
    #[default]
    Peer = 0b010,
    Client = 0b100,
}
```

注意这不是 `0/1/2` 顺序值，而是三个**独立的比特位**。这样 `Router | Peer` 就能用按位或得到「路由器或对端」的集合，这正是 `WhatAmIMatcher`（[commons/zenoh-protocol/src/core/whatami.rs:136-138](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/core/whatami.rs#L136-L138)）的原理——它内部就是一个 `NonZeroU8` 位掩码，`matches(w)` 即 `(mask & w) != 0`（[commons/zenoh-protocol/src/core/whatami.rs:177-179](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/core/whatami.rs#L177-L179)）。这正是《u6-1 Scouting》里 `scout(WhatAmI::Peer | WhatAmI::Router, config)` 能写成位或表达式的底层原因。

**WireExpr——线上 key expression 的「前缀 id + 后缀」结构。** 这是本讲最重要的 core 类型之一，几乎所有 network 消息都带它：

[commons/zenoh-protocol/src/core/wire_expr.rs:59-64](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/core/wire_expr.rs#L59-L64)：

```rust
pub struct WireExpr<'a> {
    pub scope: ExprId, // 0 marks global scope
    pub suffix: Cow<'a, str>,
    pub mapping: Mapping,
}
```

理解要点：

1. **`scope` 是数字 id**（`ExprId = u16`，[commons/zenoh-protocol/src/core/wire_expr.rs:28](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/core/wire_expr.rs#L28)）。Zenoh 会把常用的长 key expression 映射成小整数（通过 `DeclareKeyExpr` 声明映射），之后线上只传 id，省带宽。`scope == 0` 表示「全局作用域」，此时整个 key 就是 `suffix` 字符串本身。
2. **`suffix` 是相对后缀**。当 `scope != 0` 时，真正的 key = 「`scope` id 对应的前缀」+ `suffix`；`scope == 0` 时 `suffix` 就是完整 key（见 `as_str()`，[commons/zenoh-protocol/src/core/wire_expr.rs:79-85](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/core/wire_expr.rs#L79-L85)）。
3. **`mapping: Mapping`** 表示这个前缀 id 是「发送方编号空间」还是「接收方编号空间」（[commons/zenoh-protocol/src/network/mod.rs:49-55](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/network/mod.rs#L49-L55)）。因为 keyexpr 的 id 编号是**每条连接本地**的，跨节点引用时必须声明用的是谁的编号。

> 与公开 API 的关系：用户写的 `KeyExpr`/`OwnedKeyExpr`（见《u2-l2》）是完整的 key expression 字符串；`WireExpr` 是它在**线上**的形态，多了 `scope` id 优化与 `mapping`。从 `&OwnedKeyExpr` 转 `WireExpr` 就是 `scope=0` + 借用字符串（[commons/zenoh-protocol/src/core/wire_expr.rs:161-169](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/core/wire_expr.rs#L161-L169)）。

**Parameters——零拷贝键值视图。** 与《u4-3 Selector》讲过的公开 `Parameters` 是同一个类型（公开 API 只是 re-export 它）。三个分隔符定义在 [commons/zenoh-protocol/src/core/parameters.rs:32-34](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/core/parameters.rs#L32-L34)：`;` 分隔键值对、`=` 分隔键与值、`|` 拆多值。它架在一段 `Cow<str>` 上做只读迭代（[commons/zenoh-protocol/src/core/parameters.rs:47-51](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/core/parameters.rs#L47-L51)），不分配内存。协议层它就是 `Query.parameters: String`（[commons/zenoh-protocol/src/zenoh/query.rs:86](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/zenoh/query.rs#L86)）那一列查询参数。

**Encoding——MIME 风格解读标签。** [commons/zenoh-protocol/src/core/encoding.rs:27-32](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/core/encoding.rs#L27-L32)：

```rust
pub struct Encoding {
    pub id: EncodingId,          // u16
    pub schema: Option<ZSlice>,
}
```

关键性质（与《u5-l1》一致）：协议**只搬运不解释**它，`id` 是小整数前缀省带宽，自定义编码用大 id 并把具体类型塞进 `schema`。它出现在 `Put.encoding`、`Reply` 内 `Put.encoding` 等处。

**三个 QoS 枚举。** [commons/zenoh-protocol/src/core/mod.rs:330-342](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/core/mod.rs#L330-L342) 的 `Priority`（8 档，`Control=0` 最高、`Background=7` 最低，默认 `Data=5`）；[commons/zenoh-protocol/src/core/mod.rs:506-514](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/core/mod.rs#L506-L514) 的 `Reliability`；[commons/zenoh-protocol/src/core/mod.rs:615-629](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/core/mod.rs#L615-L629) 的 `CongestionControl`。这三个值不会单独上线，而是被打包进每条 network 消息的 `ext_qos` 扩展里（见 4.2.3）。

#### 4.1.4 代码实践

**实践目标**：用源码回答几个关于 core 类型的具体问题，建立「类型→定义位置→关键约束」的肌肉记忆。

**操作步骤**：

1. 打开 `commons/zenoh-protocol/src/core/mod.rs`，找到 `ZenohIdProto`，确认 `MAX_SIZE` 的值与「最小 1 字节」的依据（提示：看 `FromStr` 实现里对 `uhlc::ID` 的解析）。
2. 打开 `core/whatami.rs`，对照 `WhatAmI` 的三个比特位，手算 `WhatAmI::Router | WhatAmI::Client` 的 `u8` 值，再去 `WhatAmIMatcher::to_str()`（[commons/zenoh-protocol/src/core/whatami.rs:183-196](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/core/whatami.rs#L183-L196)）核对你算出的常量对应哪个字符串。
3. 打开 `core/wire_expr.rs`，阅读 `as_str()`（L79）与 `try_as_str()`（L87）：当 `scope != 0` 时，`as_str()` 返回什么字面量？为什么 `try_as_str()` 此时会报错？

**需要观察的现象 / 预期结果**：

- `MAX_SIZE = 16`；`FromStr` 要求小写 hex（[commons/zenoh-protocol/src/core/mod.rs:198-213](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/core/mod.rs#L198-L213) 会拒绝大写）。
- `Router(0b001) | Client(0b100) = 0b101 = 5`，对应 `WhatAmIMatcher::U8_R_C`，字符串是 `"router|client"`。
- `scope != 0` 时 `as_str()` 返回占位符 `"<encoded_expr>"`，因为此时 key 是「id 编号」，没有可打印的字符串形式；`try_as_str()` 因此 `bail!("Scoped key expression")`。这解释了为什么带 scope 的 WireExpr 不能直接当字符串用。

#### 4.1.5 小练习与答案

**练习 1**：`ZenohIdProto` 为什么用 `uhlc::ID` 而不是固定 16 字节数组作为内部表示？

> **答案**：为了线上可变长度编码。`uhlc::ID` 支持只占实际所需的字节数（1–16），协议在 Scout/Init/Hello/Join 等消息里用 `zid_len` 字段标明真实长度（如 [commons/zenoh-protocol/src/scouting/scout.rs:57-60](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/scouting/scout.rs#L57-L60) 注释 `real_zid_len := 1 + zid_len`），短 id 省 15 字节。

**练习 2**：`WireExpr { scope: 5, suffix: "temp", mapping: Sender }` 表示什么？

> **答案**：它引用「发送方编号空间里 id=5 的那个前缀」，并在其后拼接后缀 `temp`。完整 key 必须由接收方先查到 id=5 对应的前缀字符串（通过此前面交换的 `DeclareKeyExpr`），再拼上 `temp` 才能得到。`mapping: Sender` 说明这个 5 是发送方的编号。

---

### 4.2 network 层消息：声明、推送、请求/应答、兴趣

#### 4.2.1 概念说明

`network` 层是协议的**「路由信封」层**。如果说 `zenoh` 层（4.3 节）描述的是「数据本身」（一个 Put、一次 Query），那么 `network` 层描述的是「这条数据要怎么被路由」——它给数据体套上 `WireExpr`（地址）、`ext_qos`（QoS）、`ext_nodeid`（来源节点），并定义了**声明类消息**（`Declare`/`Interest`），这两类消息不携带用户数据，只携带「谁对什么感兴趣」的元信息，正是它们驱动了《u8-3 Dispatcher》里的声明式路由。

network 层一共有 **7 种消息体**，统一封装在 `NetworkBody` 枚举里，再套一层 `NetworkMessage` 信封（带上 `reliability`）。

#### 4.2.2 核心流程：七种消息体 + 信封

[commons/zenoh-protocol/src/network/mod.rs:75-84](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/network/mod.rs#L75-L84) 定义了 `NetworkBody` 的七个变体：

| 变体 | 携带的 zenoh 体 | 一句话用途 |
|------|----------------|-----------|
| `Push(Push)` | `PushBody`（Put/Del） | 把发布的数据推送给匹配的订阅者 |
| `Request(Request)` | `RequestBody`（Query） | 发起一次查询请求 |
| `Response(Response)` | `ResponseBody`（Reply/Err） | 回复一条应答 |
| `ResponseFinal(ResponseFinal)` | 无 | 告知「该请求的所有应答已发完」 |
| `Interest(Interest)` | 无 | 声明对某类声明（sub/qabl/token）的兴趣 |
| `Declare(Declare)` | 无 | 声明/注销 subscriber/queryable/keyexpr/token |
| `OAM(Oam)` | 任意 | 带外管理/路由协议数据（链路状态等，见《u8-4》） |

每种消息体有自己的 **id 常量**，用于线编码时区分（[commons/zenoh-protocol/src/network/mod.rs:37-47](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/network/mod.rs#L37-L47)）：`DECLARE=0x1e`、`PUSH=0x1d`、`REQUEST=0x1c`、`RESPONSE=0x1b`、`RESPONSE_FINAL=0x1a`、`INTEREST=0x19`、`OAM=0x1f`。注释特意强调这些 id **绝不能与 transport 层 id 冲突**（因为两者最终在同一个 batch 里编码）。

信封 `NetworkMessage` 只是在 `NetworkBody` 旁加上一个 `reliability` 字段（[commons/zenoh-protocol/src/network/mod.rs:108-112](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/network/mod.rs#L108-L112)）：

```rust
pub struct NetworkMessage {
    pub body: NetworkBody,
    pub reliability: Reliability,
}
```

**「声明式路由」是怎么被这两类消息驱动的？** 概括为：

```
A: declare_subscriber(robot/*)
   ──> 发 Declare(DeclareSubscriber{ wire_expr: robot/* }) 给路由器 B
B: 路由器在 Tables 里登记「face A 对 robot/* 感兴趣」
   ──> (可选)发 Interest 让上游也知道自己关心这类声明
之后 C: put(robot/temp, 42)
   ──> 发 Push(Put) 给 B
B: 查表发现 A 感兴趣，转发 Push 给 A
A: 收到 Push，回调 subscriber
```

也就是说，`Declare`/`Interest` 在**控制面**塑造订阅表，`Push`/`Request`/`Response` 在**数据面**按表转发。这正是《u8-1 / u8-3》讲的 Gateway/Tables/Resource 机制的协议层落点。

#### 4.2.3 源码精读

**Declare——声明类消息的总入口。** [commons/zenoh-protocol/src/network/declare.rs:51-58](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/network/declare.rs#L51-L58)：

```rust
pub struct Declare {
    pub interest_id: Option<super::interest::InterestId>,
    pub ext_qos: ext::QoSType,
    pub ext_tstamp: Option<ext::TimestampType>,
    pub ext_nodeid: ext::NodeIdType,
    pub body: DeclareBody,
}
```

`body` 是个九变体枚举（[commons/zenoh-protocol/src/network/declare.rs:89-100](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/network/declare.rs#L89-L100)），覆盖四类实体的声明/注销：`DeclareKeyExpr`/`UndeclareKeyExpr`（keyexpr→id 映射）、`DeclareSubscriber`/`UndeclareSubscriber`、`DeclareQueryable`/`UndeclareQueryable`、`DeclareToken`/`UndeclareToken`，外加一个 `DeclareFinal`（标记某 Interest 触发的批量声明结束）。各变体的 id 在 [commons/zenoh-protocol/src/network/declare.rs:73-87](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/network/declare.rs#L73-L87)（如 `D_SUBSCRIBER=0x02`、`U_SUBSCRIBER=0x03`）。

关键观察：`DeclareSubscriber` 带 `wire_expr`（[commons/zenoh-protocol/src/network/declare.rs:342-346](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/network/declare.rs#L342-L346)），即「我订阅这个 KE」；而 `UndeclareSubscriber` 不带 `wire_expr`（注销只需 id，见 [commons/zenoh-protocol/src/network/declare.rs:377-381](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/network/declare.rs#L377-L381)）。`DeclareQueryable` 额外带 `ext_info: QueryableInfoType`（complete/distance，[commons/zenoh-protocol/src/network/declare.rs:434-439](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/network/declare.rs#L434-L439)）——这对应《u4-1》里 complete queryable 的概念。

**Interest——声明「对声明的兴趣」。** [commons/zenoh-protocol/src/network/interest.rs:142-151](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/network/interest.rs#L142-L151)：

```rust
pub struct Interest {
    pub id: InterestId,
    pub mode: InterestMode,
    pub options: InterestOptions,
    pub wire_expr: Option<WireExpr<'static>>,
    ...
}
```

读它的文档图（[commons/zenoh-protocol/src/network/interest.rs:29-103](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/network/interest.rs#L29-L103)）就能理解四种 `InterestMode`（[commons/zenoh-protocol/src/network/interest.rs:157-163](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/network/interest.rs#L157-L163)）：

- `Current`：只请求「当前已存在的」匹配声明，收到一批 `Declare` + 一个 `DeclareFinal` 结束。
- `Future`：只订阅「将来的」声明/注销。
- `CurrentFuture`：两者都要（这是《u6-2 Liveliness》里 `history(true)` 和持久订阅用的模式）。
- `Final`：停止上述订阅。

`InterestOptions`（[commons/zenoh-protocol/src/network/interest.rs:263-266](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/network/interest.rs#L263-L266)）用位标志指明关心哪类实体：`KEYEXPRS`/`SUBSCRIBERS`/`QUERYABLES`/`TOKENS`，外加 `AGGREGATE`（应答是否聚合）。这就是路由器之间「我只转发我下游关心的那类声明」的剪枝依据——对应《u8-3》里 interest 驱动的剪枝逻辑。

**Push——数据推送。** [commons/zenoh-protocol/src/network/push.rs:45-52](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/network/push.rs#L45-L52)：

```rust
pub struct Push {
    pub wire_expr: WireExpr<'static>,   // 数据的 key
    pub ext_qos: ext::QoSType,
    pub ext_tstamp: Option<ext::TimestampType>,
    pub ext_nodeid: ext::NodeIdType,
    pub payload: PushBody,              // Put 或 Del
}
```

注意：**`Put`/`Del`（zenoh 体）本身不带 key**，key 在外层 `Push.wire_expr`。这是 Zenoh 的设计——同一条 `Put` 数据可以被转发到不同 face，每条转发都复用同一个 `Put`，只在 `Push` 层换不同的 `wire_expr`（或带 scope 的 id）。

**Request / Response——查询与应答。** `Request`（[commons/zenoh-protocol/src/network/request.rs:56-67](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/network/request.rs#L56-L67)）带 `id: RequestId`（用于把应答回配到请求）、`wire_expr`、以及 `ext_target: QueryTarget`。`QueryTarget`（[commons/zenoh-protocol/src/network/request.rs:94-104](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/network/request.rs#L94-L104)）正是《u4-2》讲的 `BestMatching`/`All`/`AllComplete` 三种查询目标。`Response`（[commons/zenoh-protocol/src/network/response.rs:51-59](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/network/response.rs#L51-L59)）用同样的 `rid` 回配，`ResponseFinal`（[commons/zenoh-protocol/src/network/response.rs:129-134](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/network/response.rs#L129-L134)）标记「答完了」——对应《u4-1》里 `QueryInner::Drop` 自动发的最终帧。

**统一抽取：NetworkMessageExt trait。** 这是一个很实用的 trait，它给所有 network 消息提供统一的「取 QoS / 取 priority / 取 WireExpr / 是否可丢」方法，用一个大 `match` 分派到各消息体的 `ext_qos` 字段。例如判定「可丢弃」的公式（与《u3-l3》一致）：

[commons/zenoh-protocol/src/network/mod.rs:189-192](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/network/mod.rs#L189-L192)：

```rust
fn is_droppable(&self) -> bool {
    !self.is_reliable() || self.congestion_control() == CongestionControl::Drop
}
```

而 `wire_expr()` 方法（[commons/zenoh-protocol/src/network/mod.rs:207-228](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/network/mod.rs#L207-L228)）精确列出了**哪些消息携带 WireExpr**——这是本讲综合实践的直接依据。`Push`/`Request`/`Response` 返回 `Some`；`ResponseFinal`/`OAM` 返回 `None`；`Interest` 视 `wire_expr` 是否存在（restricted 与否）；`Declare` 则因 `DeclareBody` 变体而异（声明类带、注销类多数不带）。`ext_qos` 本身被打包成单个字节（`QoSType`，[commons/zenoh-protocol/src/network/mod.rs:444-462](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/network/mod.rs#L444-L462)），把 priority / congestion / express 三个 QoS 维度塞进 8 位。

#### 4.2.4 代码实践

**实践目标**：阅读 `NetworkMessageExt::wire_expr()` 这一个方法，自己归纳「network 层哪些消息携带 WireExpr、哪些不带」，而不是死记。

**操作步骤**：

1. 打开 [commons/zenoh-protocol/src/network/mod.rs:207-228](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/network/mod.rs#L207-L228)，逐个 `match` 分支记录返回值。
2. 对 `Declare` 分支，进一步打开 `DeclareBody` 枚举（[commons/zenoh-protocol/src/network/declare.rs:89-100](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/network/declare.rs#L89-L100)），对照该方法的 `DeclareBody::DeclareKeyExpr(m) => Some(&m.wire_expr)` 等分支，确认「声明类带、纯注销类带的是 `ext_wire_expr.wire_expr`」。

**需要观察的现象 / 预期结果**：你会得到一张「消息 → 是否带 WireExpr」表。关键结论：`Push`/`Request`/`Response` 一定带；`Interest` 视 restricted；`ResponseFinal`/`OAM` 不带；`Declare` 视 body。理解为什么 `Put`（zenoh 体）本身不带 key——因为 key 在外层 network 消息上。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Put`（zenoh 层）不带 `wire_expr`，而 `Push`（network 层）带？

> **答案**：解耦数据与地址。同一条 `Put`（一段 payload + encoding）可能被路由器转发给多个 face，每个 face 的转发是一个 `Push`，各自携带对应连接上的 `wire_expr`（可能是带 scope 的短 id）。把 key 放外层，`Put` 体就可以原样复用、零拷贝地多次发送。

**练习 2**：`Interest` 的 `mode: CurrentFuture` 与《u6-2》里 liveliness 的 `history(true)` 订阅是什么关系？

> **答案**：`history(true)` 的 liveliness 订阅底层就发一个 `Interest { mode: CurrentFuture, options: TOKENS }`：`Current` 让对端先补发当前已存在的存活 token（Put），`Future` 让对端继续推送将来的上线/下线（Put/Delete）。`Final` 模式则用于注销该 interest。

---

### 4.3 transport / scouting / zenoh 三层消息：线路帧、发现、数据体

#### 4.3.1 概念说明

现在把剩下的三层一次讲清：

- **transport 层**：管「消息怎么在一条链路上跑」。它定义了建连握手（`Init`/`Open`）、保活（`KeepAlive`）、断连（`Close`）、组播上线（`Join`），以及最重要的**帧**（`Frame`/`Fragment`）——network 层消息必须先被装进 `Frame` 才能上线。多播场景用 `Join` 替代握手。
- **scouting 层**：管「节点发现」，只有 `Scout`（提问）和 `Hello`（应答）两条消息。它**不走 transport 帧**，而是直接用 UDP 多播/单播裸发（见《u6-1》《u7-3》），所以它是独立的一层。
- **zenoh 层**：管「数据体本身」，即 `Put`/`Del`/`Query`/`Reply`/`Err`。它们不能独立上线，必须被装进 network 层的 `Push`/`Request`/`Response`。

**三层嵌套关系**（本讲最核心的图）：

```
TransportMessage
└─ Frame { sn, reliability, payload: Vec<NetworkMessage> }
   └─ NetworkMessage { reliability, body: NetworkBody::Push(Push) }
      ├─ wire_expr: WireExpr            ← 数据的 key
      └─ payload: PushBody::Put(Put)    ← zenoh 层数据体
         └─ payload: ZBuf               ← 真正的字节
```

即：**`Put`（zenoh）⊂ `Push`（network）⊂ `Frame`（transport）**。一条发布数据上线时要经过这三层包装。

#### 4.3.2 核心流程：三种消息体的变体

**transport 层** `TransportBody` 有 10 个变体（[commons/zenoh-protocol/src/transport/mod.rs:127-139](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/transport/mod.rs#L127-L139)），分四组：

| 组 | 变体 | 用途 |
|----|------|------|
| 握手（仅 unicast） | `InitSyn`/`InitAck`/`OpenSyn`/`OpenAck` | 建连四步握手（见《u9-3》） |
| 维护 | `Close`/`KeepAlive`/`Join` | 断连 / 心跳保活 / 多播上线 |
| 数据 | `Frame`/`Fragment` | 整帧 / 分片（见《u9-4》） |
| 带外 | `OAM` | 管理/路由协议（链路状态等） |

各变体 id 在 [commons/zenoh-protocol/src/transport/mod.rs:51-62](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/transport/mod.rs#L51-L62)（`INIT=0x01`、`OPEN=0x02`、`CLOSE=0x03`、`KEEP_ALIVE=0x04`、`FRAME=0x05`、`FRAGMENT=0x06`、`JOIN=0x07`），与 network 层 id 刻意错开。`TransportMessage` 只是 `TransportBody` 的薄封装（[commons/zenoh-protocol/src/transport/mod.rs:141-144](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/transport/mod.rs#L141-L144)）。

> **批大小与边界**：transport 层定义 `BatchSize = u16`（[commons/zenoh-protocol/src/transport/mod.rs:41](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/transport/mod.rs#L41)），单批最大 65535 字节；流式协议（TCP）会在前面补 2 字节小端长度前缀来界定边界（[commons/zenoh-protocol/src/transport/mod.rs:36-40](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/transport/mod.rs#L36-L40) 注释）。这呼应《u9-4》讲的批处理。

**scouting 层** 只有两条（[commons/zenoh-protocol/src/scouting/mod.rs:27-31](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/scouting/mod.rs#L27-L31)）：`Scout(Scout)` 与 `Hello(HelloProto)`，id 分别 `0x01`/`0x02`（[commons/zenoh-protocol/src/scouting/mod.rs:20-24](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/scouting/mod.rs#L20-L24)）。

**zenoh 层** 用三个枚举把数据体装进 network 层（[commons/zenoh-protocol/src/zenoh/mod.rs:44-48](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/zenoh/mod.rs#L44-L48)、[L79-82](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/zenoh/mod.rs#L79-L82)、[L106-110](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/zenoh/mod.rs#L106-L110)）：

| zenoh 枚举 | 变体 | 被谁装载 |
|-----------|------|---------|
| `PushBody` | `Put` / `Del` | `Push.payload` |
| `RequestBody` | `Query` | `Request.payload` |
| `ResponseBody` | `Reply` / `Err` | `Response.payload` |

各消息 id：`PUT=0x01`、`DEL=0x02`、`QUERY=0x03`、`REPLY=0x04`、`ERR=0x05`（[commons/zenoh-protocol/src/zenoh/mod.rs:28-35](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/zenoh/mod.rs#L28-L35)）。

#### 4.3.3 源码精读

**Frame——多条 network 消息打包成一个线路帧。** [commons/zenoh-protocol/src/transport/frame.rs:70-76](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/transport/frame.rs#L70-L76)：

```rust
pub struct Frame {
    pub reliability: Reliability,
    pub sn: TransportSn,              // 序列号，用于去重/乱序保护
    pub ext_qos: ext::QoSType,
    pub payload: Vec<NetworkMessage>, // 一或多条 network 消息
}
```

`Frame` 的文档说得很清楚（[commons/zenoh-protocol/src/transport/frame.rs:24-33](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/transport/frame.rs#L24-L33)）：它是「把多条 `NetworkMessage` 聚合成一个原子上线消息」的手段，多条小消息可以共享同一个序列号 `sn`。这里的 `sn` 正是《u9-4》SeqNum 机制的对象。`Frame` 不带 `WireExpr`——它只是个容器。

**Init / Open——建连握手（仅 unicast）。** `InitSyn`/`InitAck`（[commons/zenoh-protocol/src/transport/init.rs:36-63](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/transport/init.rs#L36-L63)）交换 `version`/`whatami`/`zid`，并协商 SN/ID 分辨率与 batch size；`InitAck` 还带回一个加密的 **Cookie**。随后 `OpenSyn`（[commons/zenoh-protocol/src/transport/open.rs:85-98](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/transport/open.rs#L85-L98)）回传 Cookie、给出 `lease`/`initial_sn`，并附带一组可选扩展协商：`ext_qos`/`ext_shm`/`ext_auth`/`ext_mlink`/`ext_lowlatency`/`ext_compression`（[commons/zenoh-protocol/src/transport/open.rs:100-132](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/transport/open.rs#L100-L132)）——握手时一次性敲定 QoS、共享内存、认证、多链路、低延迟、压缩等能力。`OpenAck` 是对端确认。完整握手流程见《u9-3》。

**KeepAlive——心跳；Close——断连；Join——多播上线。** `KeepAlive` 是个空结构体（[commons/zenoh-protocol/src/transport/keepalive.rs:84-85](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/transport/keepalive.rs#L84-L85)），文档引用 ITU-T G.8013 规定心跳间隔取租约的 1/4、连续 3.5 个间隔没收到就判死链（这正对应《u9-2》的 lease/keep_alive 关系）。`Join`（[commons/zenoh-protocol/src/transport/join.rs:38-67](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/transport/join.rs#L38-L67)）用于多播：周期性 advertise 自己的 `version`/`whatami`/`zid`/SN 分辨率/batch size/`lease`/`next_sn`，无需握手即可加入组（对应《u9-2》《u9-3》多播传输）。

**Scout / Hello——发现协议。** `Scout`（[commons/zenoh-protocol/src/scouting/scout.rs:74-79](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/scouting/scout.rs#L74-L79)）携带 `version`、`what: WhatAmIMatcher`（想找哪类节点）、可选 `zid`；文档要求它用多播/广播发出。被发现的节点核对匹配条件后，用单播回 `HelloProto`（[commons/zenoh-protocol/src/scouting/hello.rs:101-107](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/scouting/hello.rs#L101-L107)），里面是 `version`/`whatami`/`zid`/`locators`——即可达地址列表（如 `tcp/192.168.1.1:7447`）。这俩消息就是《u6-1》《u7-3》scouting 机制在线上的形态。注意它们携带的是 `WhatAmI`/`WhatAmIMatcher` 和 `ZenohId`，**不带 WireExpr**。

**Put——发布数据体。** [commons/zenoh-protocol/src/zenoh/put.rs:48-58](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/zenoh/put.rs#L48-L58)：

```rust
pub struct Put {
    pub timestamp: Option<Timestamp>,
    pub encoding: Encoding,
    pub ext_sinfo: Option<ext::SourceInfoType>,
    pub ext_attachment: Option<ext::AttachmentType>,
    pub ext_shm: Option<ext::ShmType>,   // shared-memory feature
    pub ext_unknown: Vec<ZExtUnknown>,
    pub payload: ZBuf,
}
```

它有 `encoding`、`timestamp`、`payload`（零拷贝 `ZBuf`），以及来源信息/用户附件/共享内存等扩展——但没有 key，key 在外层 `Push.wire_expr`。这正对应《u3-l1》公开 `Sample` 的 `key_expr`/`payload`/`kind`/`encoding`/`timestamp`/`attachment` 字段（只是协议层把 key 拆到了信封上）。

**Query / Reply——查询与应答体。** `Query`（[commons/zenoh-protocol/src/zenoh/query.rs:83-91](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/zenoh/query.rs#L83-L91)）带 `consolidation: ConsolidationMode`（[commons/zenoh-protocol/src/zenoh/query.rs:21-41](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/zenoh/query.rs#L21-L41)，即 `Auto`/`None`/`Monotonic`/`Latest`，对应《u4-2》的合并模式）和 `parameters: String`（即《u4-3》的查询参数）。`Reply`（[commons/zenoh-protocol/src/zenoh/reply.rs:46-51](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/zenoh/reply.rs#L46-L51)）的 `payload` 就是 `ReplyBody = PushBody`（即应答里装的是一个 `Put` 或 `Del`）。

> **扩展（ext）无处不在**：你会注意到每条消息都带 `ext_qos`/`ext_tstamp`/`ext_nodeid` 等字段。它们由 `common` 模块的 `zextz64!`/`zextzbuf!`/`zextunit!` 宏生成（如 [commons/zenoh-protocol/src/network/declare.rs:60-71](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/network/declare.rs#L60-L71)），这是一种带 id 的 TLV 扩展机制：固定 header 之外的可选信息都走扩展，这让协议能在不破坏旧版本的前提下演进。具体编解码留给《u10-l2》。

#### 4.3.4 代码实践

**实践目标**：亲手验证「三层嵌套」——追踪一条 `Put` 是如何被层层包装上线的。

**操作步骤**（源码阅读型）：

1. 从 [commons/zenoh-protocol/src/zenoh/put.rs:48](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/zenoh/put.rs#L48) 的 `Put` 出发，确认它没有 key 字段。
2. 看 `PushBody::Put(Put)`（[commons/zenoh-protocol/src/zenoh/mod.rs:44-48](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/zenoh/mod.rs#L44-L48)）把 `Put` 装进 `PushBody`。
3. 看 `Push.payload: PushBody`（[commons/zenoh-protocol/src/network/push.rs:45-52](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/network/push.rs#L45-L52)）把 `PushBody` 装进 network 层 `Push`，并在此层补上 `wire_expr`。
4. 看 `Frame.payload: Vec<NetworkMessage>`（[commons/zenoh-protocol/src/transport/frame.rs:70-76](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/transport/frame.rs#L70-L76)）把 `NetworkMessage`（内含 `Push`）装进 transport 帧。

**需要观察的现象 / 预期结果**：你能画出 `Put ⊂ PushBody ⊂ Push(NetworkBody) ⊂ NetworkMessage ⊂ Frame.payload[Vec] ⊂ TransportMessage` 这条完整的包含链。关键直觉：**每往上一层，就加上一类路由/传输所需的元信息**（key 在 Push 层加、序列号与 reliability 在 Frame 层加）。

#### 4.3.5 小练习与答案

**练习 1**：`Frame` 自己带 `reliability`，`NetworkMessage` 也带 `reliability`，二者什么关系？

> **答案**：transport 层一条 `Frame` 属于某个 reliability 通道（reliable 或 best_effort，对应《u9-4》的两套 SN 管道），帧内所有 `NetworkMessage` 通常沿用同一个 reliability（见 `Frame::rand()` 里 `m.reliability = reliability`，[commons/zenoh-protocol/src/transport/frame.rs:97-101](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/transport/frame.rs#L97-L101)）。`NetworkMessage.reliability` 是消息粒度的标记，供上层 `is_droppable()` 判断用。

**练习 2**：为什么 `Scout`/`Hello` 不被装进 `Frame`？

> **答案**：因为 scouting 发生在建连**之前**——节点正是靠 scouting 才找到对方地址、才能建立 transport。此时还没有 transport 通道，自然没有 Frame 可装。所以 scouting 消息直接用 UDP 裸发（多播 Scout、单播 Hello），是独立的协议层。

---

## 5. 综合实践

**任务**：整理一份「协议消息对照表」，把本讲涉及的代表性消息按层归类、写出一句话用途，并标注是否携带 `WireExpr`。这是本讲的总复习，也是后续读 `zenoh-codec` 与路由层源码时的速查表。

**操作步骤**：

1. 逐个打开下列源码点，确认每个消息的结构定义与所属枚举：
   - `Declare`：[commons/zenoh-protocol/src/network/declare.rs:51-58](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/network/declare.rs#L51-L58)
   - `Push`：[commons/zenoh-protocol/src/network/push.rs:45-52](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/network/push.rs#L45-L52)
   - `Request`：[commons/zenoh-protocol/src/network/request.rs:56-67](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/network/request.rs#L56-L67)
   - `Response`：[commons/zenoh-protocol/src/network/response.rs:51-59](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/network/response.rs#L51-L59)
   - `Interest`：[commons/zenoh-protocol/src/network/interest.rs:142-151](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/network/interest.rs#L142-L151)
   - `Frame`：[commons/zenoh-protocol/src/transport/frame.rs:70-76](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/transport/frame.rs#L70-L76)
   - `Put`：[commons/zenoh-protocol/src/zenoh/put.rs:48-58](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/zenoh/put.rs#L48-L58)
   - `Query`：[commons/zenoh-protocol/src/zenoh/query.rs:83-91](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/zenoh/query.rs#L83-L91)
   - `Reply`：[commons/zenoh-protocol/src/zenoh/reply.rs:46-51](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/zenoh/reply.rs#L46-L51)
2. 用 [commons/zenoh-protocol/src/network/mod.rs:207-228](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/network/mod.rs#L207-L228) 的 `wire_expr()` 方法作为「是否携带 WireExpr」的判定依据（注意 transport 层的 `Frame` 与 zenoh 层的 `Put`/`Query`/`Reply` 不在此方法覆盖范围内，需要你直接看结构体有没有 `wire_expr` 字段来判断）。
3. 把结果填进下表的「你的答案」列，再与「参考答案」列对照。

**参考答案表**（先自己填，再对照）：

| 消息 | 所属层 | 一句话用途 | 携带 WireExpr？ |
|------|--------|-----------|-----------------|
| `Declare` | network | 声明/注销 subscriber/queryable/keyexpr/token 等实体 | 部分带：声明类（`DeclareSubscriber`/`DeclareQueryable`/`DeclareToken`/`DeclareKeyExpr`）带；纯 id 注销类通过 `ext_wire_expr`；`DeclareFinal` 不带 |
| `Push` | network | 把 `Put`/`Del` 数据体推送给匹配的订阅者 | 是（`Push.wire_expr` 即数据的 key） |
| `Request` | network | 发起一次查询请求 | 是 |
| `Response` | network | 回复一条 `Reply`/`Err` 应答 | 是 |
| `Interest` | network | 声明对某类声明（sub/qabl/token）的兴趣 | 可选（restricted 到某 KE 时带，否则不带） |
| `Frame` | transport | 把一或多条 `NetworkMessage` 打包成带序列号的线路帧 | 否（容器，本身无 key） |
| `Put` | zenoh | 描述一次发布的数据体（payload + encoding + timestamp） | 否（key 在外层 `Push`） |
| `Query` | zenoh | 描述一次查询的参数（consolidation + parameters） | 否（key 在外层 `Request`） |
| `Reply` | zenoh | 描述一次应答的数据体（内含 `Put`/`Del`） | 否（key 在外层 `Response`） |

**预期结果**：你应当能总结出一条规律——**只有 network 层的消息会直接携带 `WireExpr`**；transport 层消息是线路容器不带 key，zenoh 层数据体的 key 由装载它的 network 消息提供。这条规律是理解 Zenoh「数据与地址解耦」设计的关键。

> 若想进一步验证（可选，待本地验证）：开启 `RUST_LOG=zenoh::net::primitives=trace` 跑一次 `z_pub`/`z_sub`，在日志里观察 `send_push` / `handle_push` 调用，你会看到一条 `Push` 携带 `wire_expr` 而其 `payload` 是 `Put`，与本讲的三层嵌套完全吻合。

## 6. 本讲小结

- Zenoh 协议消息分**四层**：`zenoh`（数据体 Put/Del/Query/Reply/Err）、`network`（路由信封 Declare/Push/Request/Response/ResponseFinal/Interest/OAM）、`transport`（线路帧 Init/Open/Close/KeepAlive/Frame/Fragment/Join）、`scouting`（发现 Scout/Hello）。
- 三层**嵌套**关系：`Put`(zenoh) ⊂ `Push`(network) ⊂ `Frame`(transport)；每往上一层补一类元信息（key 在 network 层加、序列号与 reliability 在 transport 层加）。
- `core` 模块是「词法表」：`ZenohId`（可变长 1–16 字节）、`WhatAmI`（三位比特编码 + `WhatAmIMatcher` 掩码）、`WireExpr`（scope id + suffix + mapping，是 key expression 的线上形态）、`Timestamp`（来自 uhlc）、`Parameters`（`;`/`=`/`|` 零拷贝视图）、`Encoding`（id + schema）。
- **声明式路由**由 `Declare`（声明实体）与 `Interest`（声明对声明的兴趣，四种 mode）在控制面塑造订阅表，`Push`/`Request`/`Response` 在数据面按表转发。
- **只有 network 层消息直接携带 `WireExpr`**；`Put`/`Query`/`Reply` 等 zenoh 数据体不带 key，实现了「数据与地址解耦」，便于同一条数据零拷贝转发到多个 face。
- 所有消息都用「固定 header + 可选 ext 扩展（`ext_qos`/`ext_tstamp`/`ext_nodeid`…）」的 TLV 风格，使协议可演进；具体线编码（Zenoh080、zint）留给《u10-l2》。

## 7. 下一步学习建议

- **《u10-l2 Zenoh080 线编码与 codec》**：本讲只讲了消息「长什么样」，下一讲讲它们「怎么变成字节」——`WCodec`/`RCodec`/`Zenoh080`、zint 变长整数、header 字节机制、batch/frame 编码。两讲合起来就是完整的协议层。
- **《u10-l3 Buffers：ZBuf/ZSlice》**：本讲反复出现的 `Put.payload: ZBuf` 与扩展里的 `ZSlice`，其零拷贝设计在那里展开。
- **回看《u7-l2》与《u8-3》**：带着本讲的 message 字典重读 `Primitives`/`EPrimitives` 接口和 dispatcher 路由，你会看到 `send_push`/`handle_push` 等方法正是对 `Push`/`Declare`/`Interest` 这些消息的搬运，协议层与路由层的对应关系会豁然开朗。
- **《u8-4 路由协议》**：本讲提到的 `OAM` 消息变体，在那里会看到它如何装载链路状态（`LinkStateList`）在 router 间传播拓扑。
