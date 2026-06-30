# 路由协议：链路状态、网络与 gossip

## 1. 本讲目标

本讲是「内部架构（二）：路由（HAT 拓扑）」单元的第四讲，承接《u8-l1 路由骨架》《u8-l2 HAT 四种拓扑》《u8-3 dispatcher》。前三讲回答了「消息到了一张 Face 之后，HAT 如何决定转发给哪张下游 Face」；本讲要回答一个更底层的问题：**router / peer 一开始是怎么知道全网有哪些节点、它们之间是怎么连的？**

读完本讲，你应该能够：

- 说清 Zenoh 的「链路状态（link-state）」数据模型 `LinkState` 的字段含义，尤其是 `psid`、`sn`、边权重与可选字段的取舍。
- 读懂 `Zenoh080Routing` 编解码，能描述一条链路状态如何被序列化成字节、又被 `OAM_LINKSTATE` 消息搬运。
- 理解 router HAT 使用的 `Network` 引擎如何把各方链路状态拼成一张全网拓扑图、并用 Bellman-Ford 算最短路径树得到「下一跳」。
- 理解 peer HAT 使用的 `Gossip` 引擎如何用**同一套线协议**做发现与传播，以及它与 `Network` 的异同。
- 能部署 3 个 router 互联，用日志观察链路状态的传播过程。

## 2. 前置知识

- **链路状态路由的直觉**。想象每个节点只负责广播「我和谁直接相连」这一条事实，配上一个单调递增的版本号；所有节点各自收齐这些事实后，就能在本地拼出同一张全网连接图，再各自算出最短路径。这就是 OSPF / IS-IS 的核心思想，也是 Zenoh router 之间同步拓扑的方式。
- **OAM（Operations, Administration and Maintenance）消息**。Zenoh 的网络层消息里有一类通用的 `OAM` 消息，它带一个 `id` 和一段任意字节 `body`，相当于网络层的一个「扩展插槽」。Zenoh 把链路状态塞进 `id = OAM_LINKSTATE` 的 OAM 消息里搭车传输，而不是另开一个协议端口。
- **`psid`（peer-specific id）技巧**。不同邻居给同一个节点起的本地编号不一样，所以线协议上节点用「发送方本地编号」`psid` 标识自己，真正的全局标识 `zid` 只在需要时才附带；接收方维护一张 `psid ↔ zid` 映射表来还原。这个技巧会在 4.1、4.3 反复出现。
- 本讲默认你已经掌握《u8-l2》中 router HAT 用「最短路径树（spanning tree）」决定唯一下一跳、以及 `TreesComputationWorker` 把树计算搬到后台的设计。

## 3. 本讲源码地图

本讲涉及的关键文件及其职责：

| 文件 | 职责 |
| --- | --- |
| `zenoh/src/net/protocol/mod.rs` | 协议子模块入口，声明三个子模块并定义路由器网络名常量。 |
| `zenoh/src/net/protocol/linkstate.rs` | 链路状态**数据模型**：`LinkState` / `LocalLinkState` / `LinkStateList` / `LinkEdgeWeight` / `LinkInfo`。 |
| `zenoh/src/net/codec/mod.rs` | 定义 `Zenoh080Routing` 编码标记（codec 的「钥匙」）。 |
| `zenoh/src/net/codec/linkstate.rs` | `Zenoh080Routing` 对 `LinkState` / `LinkStateList` 的读写实现（WCodec/RCodec）。 |
| `zenoh/src/net/protocol/network.rs` | router HAT 使用的 `Network` 引擎：维护拓扑图、处理入站链路状态、算最短路径树。 |
| `zenoh/src/net/protocol/gossip.rs` | peer HAT 使用的 `Gossip` 引擎：用同一套线协议做发现与单跳/多跳传播。 |

此外，理解「消息怎么进出」还会用到几个集成点文件（仅引用、不展开）：`commons/zenoh-protocol/src/network/oam.rs`（定义 `OAM_LINKSTATE`）、`zenoh/src/net/primitives/demux.rs`（入站分用 OAM）、`zenoh/src/net/routing/hat/router/mod.rs` 与 `hat/peer/mod.rs`（两个 HAT 的 `handle_oam` 入口）。

## 4. 核心概念与源码讲解

### 4.1 链路状态协议：LinkState 数据模型

#### 4.1.1 概念说明

链路状态（link-state）路由的本质是：**每个节点只描述自己的邻居关系，附带一个单调递增的序号，然后全网洪泛；每个节点在本地把这些片段拼成同一张图。**

Zenoh 把「一条节点的自我描述」抽象成 `LinkState` 结构。可以把它想象成一张名片，上面写着：「我是谁（可选）、我当前版本号是多少、我和哪些节点直接相连（一组邻居编号）、这些边的权重各是多少」。注意它**不是路由表**——它不告诉别人「数据该怎么走」，只告诉别人「拓扑长什么样」。路由表（最短路径树）是每个节点拿到这些名片后在本地自己算出来的（见 4.3）。

#### 4.1.2 核心流程

一条 `LinkState` 在节点间的生命周期：

1. **本地构造**：节点把自己的邻居集合（在本地图里的索引）和版本号 `sn` 填进 `LinkState`，按需附带 `zid`、`whatami`、`locators`、边权重。
2. **洪泛传播**：把一条或多条 `LinkState` 打包成 `LinkStateList`，编码进 `OAM_LINKSTATE` 消息，沿已建立的传输链路发给邻居；邻居再继续向它的邻居传播（router 的 `Network` 全洪泛，peer 的 `Gossip` 单跳或多跳）。
3. **本地还原**：接收方用 `psid ↔ zid` 映射把名片里的本地编号还原成全局 `zid`，更新本地图里对应节点的连接关系。
4. **新鲜度判定**：用 `sn` 判定新旧——`sn` 不大于本地已存值的名片直接丢弃，避免旧消息覆盖新状态。

#### 4.1.3 源码精读

**协议子模块入口**只做声明，并给出路由器网络的固定名字，router HAT 初始化时用它命名自己的拓扑图：

- [zenoh/src/net/protocol/mod.rs:14-18](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/protocol/mod.rs#L14-L18) 声明 `gossip` / `linkstate` / `network` 三个子模块，并定义 `ROUTERS_NET_NAME = "[Routers Network]"`。

**线协议选项位**用一个字节的各个 bit 控制哪些可选字段出现，这是 Zenoh 省带宽的常见手法：

- [zenoh/src/net/protocol/linkstate.rs:20-24](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/protocol/linkstate.rs#L20-L24) 定义 `PID`/`WAI`/`LOC`/`WGT`/`GWY` 五个位，分别对应「是否带 zid / whatami / locators / 边权重 / 是否是网关」。

**`LinkState` 结构**就是那张「名片」：

- [zenoh/src/net/protocol/linkstate.rs:44-54](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/protocol/linkstate.rs#L44-L54) 字段含义：
  - `psid: u64` —— **发送方本地**给该节点分配的编号（不是全局 zid！）。
  - `sn: u64` —— 单调递增的版本号，用于丢弃过期状态。
  - `zid / whatami / locators` —— 都 `Option`，只在选项位为 1 时才发送。
  - `links: Vec<u64>` —— 邻居列表，存的是**每个邻居的 `psid`**（同样是发送方本地编号）。
  - `link_weights` —— 与 `links` 一一对应的边权重，可选。
  - `is_gateway` —— 该节点是否是跨 region 的网关。

源码文件顶部还画了线格式示意（`options | psid | sn | zid? | whatami? | locators? | links | weights?`），可对照阅读：[zenoh/src/net/protocol/linkstate.rs:26-43](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/protocol/linkstate.rs#L26-L43)。

**边权重**用 `NonZeroU16` 表达「0 表示未设置」的约定，未设置时用默认值 100：

- [zenoh/src/net/protocol/linkstate.rs:68-84](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/protocol/linkstate.rs#L68-L84) `DEFAULT_LINK_WEIGHT = 100`；`value()` 在未设置时返回 100，`as_raw()` 在未设置时返回 0（线协议上不发送权重时填 0）。

**`LocalLinkState`** 是接收方把 `psid` 还原成 `zid` 之后的「本地化」形态——这时 `links` 不再是本地编号数组，而是 `zid -> 权重` 的映射：

- [zenoh/src/net/protocol/linkstate.rs:98-106](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/protocol/linkstate.rs#L98-L106) `links: HashMap<ZenohIdProto, LinkEdgeWeight>`。线协议上跑的是 `LinkState`，引擎内部处理的是 `LocalLinkState`，二者由 4.3 的 `convert_to_local_link_states` 转换。

**`LinkStateList`** 就是多条名片的打包外壳：

- [zenoh/src/net/protocol/linkstate.rs:163-172](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/protocol/linkstate.rs#L163-L172) `link_states: Vec<LinkState>`。一次 OAM 消息可以同时携带多个节点的状态，便于批量同步。

**`LinkInfo` 不是线协议类型**——这是本讲一个容易踩坑的点。`LinkInfo`（`src_weight`/`dst_weight`/`actual_weight`）是节点在本地把图算出来之后，用来**汇报/观察**某条边最终权重的派生结构，由 4.3 的 `Network::links_info()` 产生：

- [zenoh/src/net/protocol/linkstate.rs:221-226](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/protocol/linkstate.rs#L221-L226) 只有 `LinkState` / `LinkStateList` 会被编进 OAM 字节流；`LinkInfo` 从不上线。

边权重也可以从配置加载，重复的 `dst_zid` 会被拒绝：

- [zenoh/src/net/protocol/linkstate.rs:195-213](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/protocol/linkstate.rs#L195-L213) `link_weights_from_config` 把配置里的 `transport_weights` 列表转成 `zid -> LinkEdgeWeight` 映射。

#### 4.1.4 代码实践

**实践目标**：通过阅读源码，能用中文说清「一条 `LinkState` 携带哪些字段、哪些是可选的、为什么 `psid` 不等于 `zid`」。

**操作步骤**：

1. 打开 [zenoh/src/net/protocol/linkstate.rs:44-54](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/protocol/linkstate.rs#L44-L54)，对照 [选项位定义 linkstate.rs:20-24](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/protocol/linkstate.rs#L20-L24)，列出「必发字段」与「可选字段」两张清单。
2. 阅读 `LinkEdgeWeight` 的 [默认值与 as_raw 逻辑 linkstate.rs:68-91](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/protocol/linkstate.rs#L68-L91)，回答：为什么「未设置」在线协议上要用 0、在本地计算时却用 100？
3. 写一段话解释：为什么 `links` 字段存的是 `psid` 而不是 `zid`？（提示：节点编号的局部性 + 带宽。）

**需要观察的现象 / 预期结果**：你应该得出——必发的是 `psid`、`sn`、`links`；可选的是 `zid`、`whatami`、`locators`、`link_weights`、`is_gateway`；`psid≠zid` 是为了让 `zid`（较长）只在邻居首次需要建立映射时才发送。本步骤为纯源码阅读，无运行输出。

#### 4.1.5 小练习与答案

**练习 1**：如果两个节点互发的 `LinkState` 里都没有带 `link_weights`，它们之间这条边的权重是多少？
**答案**：100。`LinkEdgeWeight` 未设置时 `value()` 返回 `DEFAULT_LINK_WEIGHT = 100`。

**练习 2**：`LinkState` 的 `sn` 字段有什么作用？收到一条 `sn` 比本地已有值更小的名片会怎样？
**答案**：`sn` 是单调递增的版本号，用于新鲜度判定；`sn` 不大于本地已存值的名片会被当作过期状态丢弃（见 4.3 的 `link_states` 实现），避免旧拓扑覆盖新拓扑。

---

### 4.2 Zenoh080Routing 线编码：LinkState 如何变成字节

#### 4.2.1 概念说明

《u10-l2》（后续会专门讲）会系统讲 Zenoh 的 `Zenoh080` 线编码与 `WCodec`/`RCodec` trait。这里只需知道：`Zenoh080Routing` 是一个**编码标记类型（codec marker）**——它本身不带数据，只是一个「钥匙」，告诉编译器「请用路由协议这一套编解码规则」。为 `Zenoh080Routing` 实现 `WCodec<&LinkState, &mut W>` 就等于声明「LinkState 的写规则」，实现 `RCodec<LinkState, &mut R>` 就等于声明「读规则」。

这种「用空类型当编码版本标签」的设计，让 Zenoh 能同时支持多版线协议（如 `Zenoh080` 与 `Zenoh080Routing`）而互不干扰。

#### 4.2.2 核心流程

`LinkState` 的编码顺序（写）：

```
options(u64)  ── 各 bit 表示可选字段是否出现
psid(u64)     ── 本地编号
sn(u64)       ── 版本号
[zid]?        ── 仅当 options 的 PID 位为 1
[whatami(u8)]?── 仅当 WAI 位为 1
[locators]?   ── 仅当 LOC 位为 1
links_len(u64) + links[psid; links_len]
[weights; links_len]?  ── 仅当 WGT 位为 1（数量复用 links_len，不单独写）
```

解码是严格镜像：先读 `options`，再按位决定后续读不读哪些字段。`LinkStateList` 则是「长度前缀 + 逐个 `LinkState`」。

#### 4.2.3 源码精读

**编码标记本身**定义在 codec 模块入口：

- [zenoh/src/net/codec/mod.rs:14-23](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/codec/mod.rs#L14-L23) `pub struct Zenoh080Routing;`（一个空结构体）加 `const fn new()`。

**写 `LinkState`** 的关键在于「先按字段是否为 `Some` 置 options 位，再按固定顺序写」：

- [zenoh/src/net/codec/linkstate.rs:33-86](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/codec/linkstate.rs#L33-L86) 写 options（[L42-58](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/codec/linkstate.rs#L42-L58)）→ 写 `psid`/`sn` → 按位写 `zid`/`whatami`/`locators` → 写 `links.len()` 再逐个写 link → 若有权重，**复用 `links.len()` 逐个写权重，不再单独写长度**（[L77-82](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/codec/linkstate.rs#L77-L82)）。

**读 `LinkState`** 完全对称，用 `imsg::has_option(options, FLAG)` 判断每个可选字段：

- [zenoh/src/net/codec/linkstate.rs:88-149](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/codec/linkstate.rs#L88-L149) 注意 [L124-134](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/codec/linkstate.rs#L124-L134) 读取权重时同样用 `links_len` 作循环上界，不另读长度。

**`LinkStateList`** 的编解码就是「长度 + 逐个递归调用」：

- [zenoh/src/net/codec/linkstate.rs:152-188](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/codec/linkstate.rs#L152-L188) 写时先写 `link_states.len()` 再逐个 `self.write`；读时先读 `len` 再逐个 `self.read`。

**这套编码产物最终装进 OAM 消息**。`Network` 和 `Gossip` 都用同一段代码把 `LinkStateList` 编进 `ZBuf`，再包成 `OAM_LINKSTATE`：

- [zenoh/src/net/protocol/network.rs:355-370](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/protocol/network.rs#L355-L370) `make_msg`：`Zenoh080Routing::new().write(buf.writer(), &LinkStateList{...})` → 包成 `NetworkBody::OAM(Oam { id: OAM_LINKSTATE, body: ZExtBody::ZBuf(buf), ... })`。
- `OAM_LINKSTATE` 的值定义在协议层：[commons/zenoh-protocol/src/network/oam.rs:24-28](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/network/oam.rs#L24-L28) `OAM_LINKSTATE: OamId = 0x0001`，`Oam` 结构见 [oam.rs:53-59](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/network/oam.rs#L53-L59)。

#### 4.2.4 代码实践

**实践目标**：手工「模拟」一次 `LinkState` 编码，理解 options 位与字段顺序。

**操作步骤**：

1. 假设一条 `LinkState { psid: 3, sn: 7, zid: Some(...), whatami: Some(Router), locators: None, links: vec![1, 2], link_weights: None, is_gateway: false }`。
2. 对照 [WCodec 实现 codec/linkstate.rs:39-85](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/codec/linkstate.rs#L39-L85)，写出它的 options 值（哪些位置 1）。
3. 写出字段的发送顺序。

**预期结果**：options 应置 `PID`（有 zid）和 `WAI`（有 whatami）两位，即 `PID | WAI`；`LOC`/`WGT`/`GWY` 均为 0。发送顺序为 `options → psid(3) → sn(7) → zid → whatami → links_len(2) → 1 → 2`（无 locators、无 weights）。本步骤为纸面推演，无需运行。

#### 4.2.5 小练习与答案

**练习 1**：为什么写 `link_weights` 时不单独写它的长度？
**答案**：因为权重与 `links` 严格一一对应，数量等于 `links_len`，读取端已知道这个长度，复用即可省字节。源码注释也点明了这一点。

**练习 2**：`Zenoh080Routing` 这个类型存了什么状态？为什么它是个空结构体？
**答案**：不存任何状态。它只是一个编译期「编码版本标签」，用来在 trait 实现里区分「用哪一套线协议规则」；真正的读写逻辑在 `WCodec`/`RCodec` 的 `impl` 里。

---

### 4.3 Network：router 的链路状态引擎与最短路径树

#### 4.3.1 概念说明

`Network` 是 **router HAT 专用的链路状态引擎**。它的核心是一张用 [`petgraph`](https://docs.rs/petgraph) 维护的无向图 `StableUnGraph<Node, f64>`：节点是路由器，边是路由器之间的传输链路，边权是该链路的代价。router 之间通过 4.1/4.2 的线协议互相交换 `LinkState`，各自在这张图上「增删改」节点和边，最终每个 router 都收敛到**同一张全网拓扑图**。

但「知道拓扑」还不够，router 还要知道「数据该往哪走」。《u8-l2》讲过 router HAT 用最短路径树决定**唯一下一跳**以避免环路。本节就看这个下一跳是怎么算出来的：`Network` 为每个节点作为根各跑一次 Bellman-Ford，得到「以任意节点为源、任意节点为目的」时的下一跳表 `trees[src].directions[dst]`。

`Network` 同时支持几种工作模式，由构造参数控制：`full_linkstate`（是否做完整链路状态洪泛与建图）、`gossip` / `gossip_multihop`（是否兼作 gossip 发现，以及是否多跳传播）。router HAT 初始化时 `full_linkstate = true`。

#### 4.3.2 核心流程

`Network` 处理入站链路状态的 `link_states()` 主流程：

```
收到的 LinkStateList（psid 编码）
   │
   ▼ convert_to_local_link_states   ── 用来源链路的 psid↔zid 映射还原成 LocalLinkState
   │
   ├── 若 !full_linkstate && !gossip_multihop ──→ process_singlehop_gossip_linkstate（轻量单跳路径）
   │
   └── 否则（完整链路状态）：
         ① 逐条按 sn 判新鲜度，新增/更新节点，增/改/删边（update_edge / remove_edge）
         ② remove_detached_nodes：从本节点 DFS，删掉不再可达的节点
         ③ autoconnect：对刚发现且应连的节点，spawn connect_peer（带随机退避）
         ④ propagate_link_states：把新/更新状态洪泛给除来源外的其它链路
         ⑤ 返回 removed_nodes（供 HAT 清理订阅/查询/token 路由）
```

树计算则异步发生在后台：`link_states` 改完图后，router HAT 通过 `TreesComputationWorker` 把「重算」请求投到一个容量为 1 的通道里；worker 每 100ms 醒来一次消费请求，调用 `compute_trees()` 重算所有最短路径树。这样把 O(V·(V·E)) 的 Bellman-Ford 计算与数据快路径解耦，代价是约 100ms 的最终一致延迟。

#### 4.3.3 源码精读

**`Network` 结构体**——注意它的图、树、距离表都按 `NodeIndex` 索引：

- [zenoh/src/net/protocol/network.rs:134-149](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/protocol/network.rs#L134-L149) 关键字段：`graph: StableUnGraph<Node, f64>`（拓扑图）、`trees: Vec<Tree>`（每个根节点一棵最短路径树）、`distances: Vec<f64>`（从自己出发的距离）、`links: VecMap<Link>`（每条传输链路及其 `psid↔zid` 映射）、`full_linkstate`/`gossip`/`gossip_multihop` 三个模式开关。
- 图的节点 `Node` 把 `links` 存成 `HashMap<ZenohIdProto, LinkEdgeWeight>`（局部化形态）：[network.rs:61-72](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/protocol/network.rs#L61-L72)。
- 每条传输链路 `Link` 持有 `mappings`（psid→zid）和 `local_mappings`（psid→本地 NodeIndex），这是 4.1 提到的 `psid` 还原表：[network.rs:80-118](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/protocol/network.rs#L80-L118)。

**构造**：`Network::new` 把自己加进图、初始化空树与零距离：

- [zenoh/src/net/protocol/network.rs:153-195](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/protocol/network.rs#L153-L195) `is_gateway` 取自 `bound.is_south()`（region 朝向）。

**psid → zid 还原**：`convert_to_local_link_states` 用来源链路的映射表，把每条 `LinkState`（含其 `links` 数组里的 psid）全部翻译成 `zid`，得到 `LocalLinkState`：

- [zenoh/src/net/protocol/network.rs:462-564](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/protocol/network.rs#L462-L564) 重点是 [L487-491](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/protocol/network.rs#L487-L491)：遇到带 `zid` 的名片就 `set_zid_mapping` 登记，遇到不带 `zid` 的就查已有映射还原；查不到则报错丢弃。

**主入口 `link_states`**：判新鲜度 → 增删节点/边 → 删孤立节点 → autoconnect → 洪泛：

- [zenoh/src/net/protocol/network.rs:696-828](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/protocol/network.rs#L696-L828) 新鲜度判定在 [L726-742](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/protocol/network.rs#L726-L742)（`oldsn < ls.sn` 才接受，否则 `continue` 丢弃过期状态）；边的增/改在 [L760-770](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/protocol/network.rs#L760-L770)，删在 [L786-801](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/protocol/network.rs#L786-L801)；删孤立节点在 [L804](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/protocol/network.rs#L804)；洪泛在 [L822](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/protocol/network.rs#L822)。

**单跳 gossip 快路径**：当既不做完整链路状态也不做多跳 gossip 时，走更轻量的 `process_singlehop_gossip_linkstate`，只更新直接邻居信息并按需 autoconnect：

- [zenoh/src/net/protocol/network.rs:566-631](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/protocol/network.rs#L566-L631) 注意 [L597-599](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/protocol/network.rs#L597-L599) 的注释：gossip 可能发来 `links` 为空的名片，这种情况会被忽略，因为 `Network` 认为两节点只有互相对彼此都有连接声明时才算真正连通。

**删孤立节点**：从自己出发做 DFS，凡不可达的节点都删掉，防止断网后残留幽灵节点：

- [zenoh/src/net/protocol/network.rs:990-1013](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/protocol/network.rs#L990-L1013) `remove_detached_nodes`。

**边权重的确定**：`update_edge` 取两端各自声明权重的较大值（与 `DEFAULT_CONFIG.json5` 注释「两端都设则取较大」一致），再加一点点基于 zid 哈希的抖动来打破等价路径，保证全网 deterministic：

- [zenoh/src/net/protocol/network.rs:428-459](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/protocol/network.rs#L428-L459) 最终边权为

  \[
  w_{\text{edge}} = w_{\max}\cdot\bigl(1 + 0.01\cdot r\bigr),\quad r=\frac{\text{hash}(z_1,z_2)\bmod 2^{32}}{2^{32}-1}\in[0,1]
  \]

  其中 \(w_{\max}\) 是两端声明权重的较大者（都未声明则取默认 100）。

**最短路径树 `compute_trees`** ——本节的高潮。对每个节点作根跑 Bellman-Ford，得到 `predecessors`（从根到各点的最短路径上，每个点的「父亲」），再据此填三张表：

- [zenoh/src/net/protocol/network.rs:1015-1116](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/protocol/network.rs#L1015-L1116)
  - `trees[root].parent`：根到「自己」路径上自己的父亲（即自己朝根方向的上一跳）。
  - `trees[root].children`：父亲恰好是「自己」的那些节点。
  - `trees[root].directions[dst]`：**从自己出发去 dst 的下一跳**。算法在 [L1079-1095](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/protocol/network.rs#L1079-L1095)：从 dst 沿 `predecessors` 一直回溯到「父亲是自己」的那个节点，它就是下一跳。
  - 返回值 `new_children`（[L1099-1115](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/protocol/network.rs#L1099-L1115)）只含「相比上一次新出现的 children」，供 HAT 决定哪些路由需要新增声明。

`Tree` 结构本身：[network.rs:126-132](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/protocol/network.rs#L126-L132)。

**HAT 怎么取下一跳**：`route_successor(src, dst)` 先把 `src`/`dst` 的 zid 映射到 `NodeIndex`，再查 `trees[src].directions[dst]`：

- [zenoh/src/net/protocol/network.rs:1149-1179](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/protocol/network.rs#L1149-L1179) `successor_entry` / `SuccessorEntry{source, destination, successor}`（[L1190-1194](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/protocol/network.rs#L1190-L1194)）。这就是《u8-l2》里 router HAT「查最短路径树得到唯一下一跳 face」的真正数据来源。

**`links_info`** 产出 4.1 提到的派生 `LinkInfo`，供观察每条边最终的 `src_weight`/`dst_weight`/`actual_weight`：

- [zenoh/src/net/protocol/network.rs:1118-1147](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/protocol/network.rs#L1118-L1147)。

**集成：消息怎么进到 `Network`**。入站网络消息由 `DeMux` 分用，OAM 分支锁住路由表后交给所属 HAT 的 `handle_oam`：

- [zenoh/src/net/primitives/demux.rs:187-221](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/primitives/demux.rs#L187-L221) `NetworkBodyMut::OAM(m)` → `owner_hat.handle_oam(...)`。
- router HAT 的 `handle_oam` 用 `Zenoh080Routing` 解码出 `LinkStateList`，调用 `net_mut().link_states(...)`：[zenoh/src/net/routing/hat/router/mod.rs:321-347](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/hat/router/mod.rs#L321-L347)。返回的 `removed_nodes` 会被用来清理这些节点上挂着的订阅/查询/token 路由（[L349-362](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/hat/router/mod.rs#L349-L362)）。
- router HAT 在 `init` 时用 `full_linkstate = true` 创建 `Network`：[hat/router/mod.rs:262-273](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/hat/router/mod.rs#L262-L273)。

**后台树计算的节流**：`TreesComputationWorker` 是个 `TerminatableTask`，跑在 `ZRuntime::Net` 上，醒来先睡 `TREES_COMPUTATION_DELAY_MS`（=100ms）再消费通道里的重算请求；通道容量为 1，`try_send` 满了就丢弃，天然合并多次请求：

- [zenoh/src/net/routing/hat/router/mod.rs:69-98](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/hat/router/mod.rs#L69-L98) worker 定义；常量 `TREES_COMPUTATION_DELAY_MS = 100` 在 [zenoh/src/net/routing/hat/mod.rs:65](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/hat/mod.rs#L65)。

#### 4.3.4 代码实践

**实践目标**：读懂 `compute_trees` 的 `directions` 是如何从 Bellman-Ford 的 `predecessors` 推出来的。

**操作步骤**：

1. 阅读 [compute_trees network.rs:1015-1097](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/protocol/network.rs#L1015-L1097)，特别盯住 [L1079-1095](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/protocol/network.rs#L1079-L1095) 的回溯循环。
2. 假设一条 3 节点链 `R1 — R2 — R3`（R1 和 R3 不直连），自己是 R1。以 R3 为根时，Bellman-Ford 给出的 `predecessors` 链大致是 `R3 ← R2 ← R1`。手算：R1 要把数据送给 R3，`directions[R3]` 应该指向谁？

**预期结果**：从 dst=R3 沿 `predecessors` 回溯：R3 的父亲是 R2（≠自己），继续；R2 的父亲是 R1（==自己），所以下一跳是 R2。即 `trees[R3].directions[R3] = R2`。这正是 router HAT 在 R1 上把发往 R3 方向的数据交给「通向 R2 的那张 face」的依据。本步骤为纸面推演，无需运行。

#### 4.3.5 小练习与答案

**练习 1**：`update_edge` 为什么要在真实权重上乘一个 `1 + 0.01*r` 的微小抖动？
**答案**：当多条路径权重完全相等时，不同节点可能各自选不同的下一跳，导致行为不确定甚至环路。加入基于两端 zid 哈希的确定性抖动，能保证等价路径在全网的「优劣排序」一致，从而每个节点都选同一条路径。

**练习 2**：`Network::link_states` 返回的 `removed_nodes` 会被 router HAT 用来做什么？
**答案**：用来清理这些已消失节点上挂着的远端订阅（subscribers）、查询（queryables）和 token，调用 `disable_data_routes` / `unpropagate_subscriber` 等，避免向不存在的节点继续转发（见 [hat/router/mod.rs:349-362](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/hat/router/mod.rs#L349-L362)）。

---

### 4.4 Gossip：peer 的发现与传播

#### 4.4.1 概念说明

`Gossip` 是 **peer HAT 使用的引擎**，与 `Network` 并列。关键事实是：**`Gossip` 与 `Network` 共用同一套链路状态线协议**——同样的 `LinkState`/`LinkStateList`、同样的 `Zenoh080Routing` 编解码、同样的 `OAM_LINKSTATE` 消息。源码注释明确写了这一点：

> `Gossip` interoperates w/ `Network`: both implementations use the same linkstate underlying protocol.
> —— [zenoh/src/net/routing/hat/peer/mod.rs:85-88](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/hat/peer/mod.rs#L85-L88)

那它们的区别是什么？`Network`（router）做**完整链路状态**：建全网图、算最短路径树、按唯一下一跳转发。`Gossip`（peer）更轻：默认只做**单跳传播**（`gossip_multihop=false`），主要用于**发现**和**自动建连（autoconnect）**，外加在多网关场景下做去重。它**不算最短路径树**，peer HAT 的转发走的是另一套（兴趣剪枝的洪泛，见《u8-l2》《u8-3》）。

`gossip_multihop=true` 时，gossip 信息会多跳传播到全网，适合「linkstate 模式下并非所有节点都两两直连」的场景，代价是更多发现流量、更低可扩展性（见 `DEFAULT_CONFIG.json5` 注释）。

#### 4.4.2 核心流程

`Gossip::link_states` 的处理逻辑：

```
收到的 LinkStateList
   │ 用来源链路的 psid↔zid 映射还原（带 zid 的名片登记映射，不带则查表）
   ▼
   逐条处理：
     ├── 自己已知该 zid 且 sn 更新 → 更新 locators 等，必要时向其它链路转发（单跳）
     └── should_autoconnect(zid, whatami) && 有 locators
           └── spawn 任务：connect_peer(zid, locators)   ── 真正发起传输建连
   ▼
   若 (!wait_declares) || src 不是 Peer → terminate_peer_connector_zid(src)
        （表示来源已连上、声明已就绪，可停止对该 zid 的建连尝试）
```

注意 `Gossip` 的「转发」远没有 `Network` 那么彻底——它主要把「自己直接邻居」的信息告诉别人，并且只在必要时附带 locators（由 `propagate_locators` 判断）。

#### 4.4.3 源码精读

**`Gossip` 结构**与 `Network` 形似但更简，图同样是 `StableUnGraph<Node, f64>`：

- [zenoh/src/net/protocol/gossip.rs:105-114](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/protocol/gossip.rs#L105-L114) 持有 `gossip_target`（只对哪些角色发）、`autoconnect`、`wait_declares`、图、链路表。
- 它的 `Node`（[gossip.rs:50-60](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/protocol/gossip.rs#L50-L60)）**没有** `Network::Node` 那种 `links: HashMap`——gossip 节点不把每条边都存下来，连接信息只在「构造自己的 LinkState」时临时从 `self.links` 取（见下）。

**构造自己的 LinkState**：gossip 只为「自己」这一节点填 `links`（即自己的直接邻居），其它节点的名片 `links` 留空：

- [zenoh/src/net/protocol/gossip.rs:169-211](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/protocol/gossip.rs#L169-L211) `make_link_state`，注意 `if details.links && idx == self.idx` 才填邻居列表。

**编码进 OAM**——与 `Network` 完全相同的一段代码：

- [zenoh/src/net/protocol/gossip.rs:213-228](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/protocol/gossip.rs#L213-L228) `make_msg` 用 `Zenoh080Routing` 写 `LinkStateList` 进 `OAM_LINKSTATE`。

**`Gossip::link_states`** 主处理：还原映射 → 更新图/locators → 单跳转发 → autoconnect：

- [zenoh/src/net/protocol/gossip.rs:269-418](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/protocol/gossip.rs#L269-L418)
  - 映射还原与未知映射报错：[L294-331](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/protocol/gossip.rs#L294-L331)。
  - autoconnect 建连：[L385-409](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/protocol/gossip.rs#L385-L409)，对已连的 zid 跳过，否则登记 `peer_connector_zid` 并 `connect_peer`。
  - 建连就绪后停止重试：[L411-417](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/protocol/gossip.rs#L411-L417)。

**新链路建立时的扩散**：`add_link` 会把自己更新后的状态发给其它已有链路，并把「所有已知节点」的状态发给新链路，让对端快速建立全局视图：

- [zenoh/src/net/protocol/gossip.rs:420-519](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/protocol/gossip.rs#L420-L519) 注意 [L506-513](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/protocol/gossip.rs#L506-L513) 的注释：peer 网关 HAT 需要 `links` 信息来在多网关场景下去重数据/查询，非网关的 peer 对端会忽略 links 信息。

**集成**：peer HAT 的 `handle_oam` 同样解码 `LinkStateList` 后分派给 `Gossip::link_states`：

- [zenoh/src/net/routing/hat/peer/mod.rs:357-381](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/hat/peer/mod.rs#L357-L381) peer HAT 的 `Net` 枚举可装 `Gossip`（[hat/peer/mod.rs:81](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/hat/peer/mod.rs#L81)），`link_states` 按 gossip/linkstate 分派（[hat/peer/mod.rs:564-575](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/hat/peer/mod.rs#L564-L575)）。

**相关配置**（`DEFAULT_CONFIG.json5`）：`scouting/gossip/enabled`（默认 true）、`scouting/gossip/multihop`（默认 false）、`scouting/gossip/target`、`scouting/gossip/autoconnect`：

- [DEFAULT_CONFIG.json5:176-208](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/DEFAULT_CONFIG.json5#L176-L208)。

#### 4.4.4 代码实践

**实践目标**：对比 `Gossip::make_link_state` 与 `Network::make_link_state`，理解「为什么 gossip 节点不存边」。

**操作步骤**：

1. 读 [Gossip::make_link_state gossip.rs:169-211](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/protocol/gossip.rs#L169-L211)，注意 `links` 只在 `idx == self.idx` 时才填。
2. 对比 [Network::make_link_state network.rs:308-353](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/protocol/network.rs#L308-L353)，后者对任意节点都从 `self.graph[idx].links` 取边。
3. 写一段话：peer 用 gossip 的主要目的是什么？为什么它不需要像 router 那样为每个节点维护完整边集？

**预期结果**：peer 的 gossip 主要服务于**发现 + autoconnect**（找到对端 locators 并建连）和**多网关去重**，转发本身靠兴趣洪泛而非最短路径树，因此不必维护全网完整边集，省内存与带宽。本步骤为源码阅读型实践，无运行输出。

#### 4.4.5 小练习与答案

**练习 1**：`Gossip` 和 `Network` 在线协议层是否兼容？一个 router 和一个 peer 交换链路状态消息时会发生什么？
**答案**：兼容——两者用相同的 `LinkState`/`LinkStateList`/`Zenoh080Routing`/`OAM_LINKSTATE`。router 收到 peer 的消息走 `Network::link_states`（且 router 的 `gossip` 默认开启，能处理单跳/多跳 gossip），peer 收到 router 的消息走 `Gossip::link_states`。它们各自按自己的策略利用这些信息。

**练习 2**：把 `scouting/gossip/multihop` 从 `false` 改成 `true` 会带来什么好处和代价？
**答案**：好处是 gossip 信息多跳传播到全网，在 linkstate 模式下并非所有节点两两直连时也能完成发现；代价是更多发现流量、更低可扩展性（见 [DEFAULT_CONFIG.json5:179-184](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/DEFAULT_CONFIG.json5#L179-L184) 注释）。

---

## 5. 综合实践

本实践把本讲三个最小模块串起来：**部署 3 个 router 组成一条链，用日志观察链路状态的传播，并验证全网拓扑视图的收敛**。

> 下方命令的具体日志输出**待本地验证**（取决于编译版本与运行环境），但现象是确定的。

**拓扑**：`R1 — R2 — R3`，其中 R3 只连 R2、R2 只连 R1，R1 与 R3 不直连。关闭多播 scouting，强制拓扑完全由显式 connect + 链路状态协议建立。这样能清楚看到：R1 通过链路状态「学到」R3 的存在，并算出经过 R2 的路径。

**操作步骤**：

1. 先编译（首次较慢）：

   ```bash
   cargo build --release -p zenohd
   ```

2. 开三个终端，分别用提升的日志级别启动三个 router（`--no-multicast-scouting` 关闭多播发现，`-e` 指定要连的对端）：

   ```bash
   # 终端 1 —— R1（监听 7447）
   RUST_LOG="zenoh::net::protocol=debug,zenoh::net::routing::hat=debug,info" \
     cargo run --release -p zenohd -- --listen tcp/127.0.0.1:7447 --no-multicast-scouting

   # 终端 2 —— R2（监听 7448，连 R1）
   RUST_LOG="zenoh::net::protocol=debug,zenoh::net::routing::hat=debug,info" \
     cargo run --release -p zenohd -- --listen tcp/127.0.0.1:7448 -e tcp/127.0.0.1:7447 --no-multicast-scouting

   # 终端 3 —— R3（监听 7449，连 R2）
   RUST_LOG="zenoh::net::protocol=debug,zenoh::net::routing::hat=debug,info" \
     cargo run --release -p zenohd -- --listen tcp/127.0.0.1:7449 -e tcp/127.0.0.1:7448 --no-multicast-scouting
   ```

3. **观察链路状态传播**：在 R1 的日志里应能看到类似 `Add node (state) <R3-zid>` 的 debug 行（来自 [network.rs:753](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/protocol/network.rs#L753)），表示 R1 通过 R2 转发来的链路状态学到了 R3；把级别调到 `trace` 还能看到 `Received from <R2-zid> raw: [...]` / `Send to ...` 这类收发链路状态列表的日志（[network.rs:701](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/protocol/network.rs#L701)、[network.rs:377](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/protocol/network.rs#L377)）。

4. **观察最短路径树**：在 R1 上把级别设到 `debug`，应能看到形如 `Tree <R3-zid> ["R2 <- R3", "R1 <- R2", ...]` 的日志（来自 [compute_trees 的 debug 分支 network.rs:1037-1053](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/protocol/network.rs#L1037-L1053)），印证 R1 算出的「去 R3 的下一跳是 R2」。

5. **验证转发**：在 R3 后面挂一个 subscriber（订阅 `demo/**`），在 R1 后面用 `z_pub` 发布 `demo/test`。由于 R1 已学到 R3 并算出经 R2 的下一跳，消息应能跨两跳送达 R3 的订阅端。（若未送达，优先检查三个 router 是否都已打印出彼此的 `Add node` 日志，即拓扑是否已收敛。）

**需要观察的现象 / 预期结果**：

- 每个 router 最终都在日志里出现另外两个 zid 的 `Add node`，说明全网拓扑已在各节点收敛。
- R1 的 `Tree` 日志显示去 R3 的路径经过 R2。
- 跨两跳的 pub/sub 能正常收发。

**源码阅读小结**（对应任务要求）：写一段话说明——线协议上被编码并交换的是 `LinkState`/`LinkStateList`（4.1/4.2），由 `OAM_LINKSTATE` 搬运；接收方用 `psid↔zid` 映射还原后更新本地图（4.3 的 `convert_to_local_link_states`）；router 在图上用 Bellman-Ford 算最短路径树得到下一跳（4.3 的 `compute_trees`）。`LinkInfo` **不上线**，它只是 `Network::links_info()` 产出的本地派生视图，用来观察每条边最终合并出的 `src_weight`/`dst_weight`/`actual_weight`。

> 若编译或运行受阻，可退化为「源码阅读型实践」：只做上面第 3、4 步对应的源码阅读与日志字符串比对，不实际启动进程。

## 6. 本讲小结

- 链路状态路由的核心是「每个节点只描述自己的邻居 + 一个单调 `sn`，全网洪泛，各自在本地拼出同一张拓扑图」；Zenoh 的名片是 `LinkState`，打包成 `LinkStateList`。
- `LinkState` 的 `psid` 是**发送方本地编号**，不等于全局 `zid`；`zid`/`whatami`/`locators`/`link_weights` 都是按选项位按需出现的可选字段，目的是省带宽。
- 线编码由 `Zenoh080Routing` codec marker 驱动：`options → psid → sn → 可选字段 → links(长度前缀) → 可选权重(复用 links 长度)`，最终装进 `OAM_LINKSTATE`（`0x0001`）的 `ZBuf` body 搭车传输。
- `Network`（router HAT）做完整链路状态：建 `petgraph` 图、按 `sn` 判新鲜度增删节点/边、删孤立节点、autoconnect、洪泛；并用 Bellman-Ford 对每个根算最短路径树，`trees[src].directions[dst]` 即「去 dst 的下一跳」，树计算被 `TreesComputationWorker` 异步化到后台、每 100ms 合并重算。
- `Gossip`（peer HAT）**共用同一套线协议**，但更轻：默认单跳传播，主要用于发现与 autoconnect 及多网关去重，不算最短路径树。
- 边权取两端声明权重的较大者（都未声明则 100），并加基于 zid 哈希的确定性抖动打破等价路径；`LinkInfo` 是本地派生观察结构，不参与线协议。

## 7. 下一步学习建议

- 想系统理解 `Zenoh080Routing` 之外的整体线编码（`WCodec`/`RCodec`/`zint` 变长整数/header 字节机制），请阅读《u10-l2 Zenoh080 线编码与 codec》。
- 想了解 router 拿到 `Network` 算出的下一跳后，如何在 dispatcher/HAT 里真正完成一次数据转发，回顾《u8-3 dispatcher》与《u8-l2 HAT》，重点是 `compute_data_route` 如何消费 `SuccessorEntry`。
- 想看「边权重如何影响路由选择」，可结合本讲的 `update_edge` 抖动公式，阅读 `zenoh/src/tests/link_weights.rs` 的测试用例，观察不同权重下最短路径树的变化。
- 若对 gossip/autoconnect 在建连编排里的角色感兴趣，可回到《u7-l3 Runtime 编排器》，把本讲的 `connect_discovered_peer`/`autoconnect` 与 `orchestrator.rs` 的 `scout`/`responder`/`AutoConnect` 串成完整的「发现→建连」链路。
