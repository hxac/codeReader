# AAA 访问控制：ClientAaa 与权限掩码

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 BUS/RT 的 AAA（鉴权 Authorization）解决了什么问题、在什么场景下才需要开启。
- 读懂 `ClientAaa` 这个结构体里四类权限（p2p / publish / subscribe / broadcast）加 `hosts_allow` 的含义，并能用建造者方法配置出一条策略。
- 区分两套掩码语法：对端名掩码（`.` 分隔、`?`/`*`）与主题掩码（`/` 分隔、`+`/`#`）。
- 说清 AAA 的两个鉴权时机——连接时（`connect_allowed`）与每条入站帧时（`handle_reader` 里的分支），以及「Unix socket 绕过 IP 检查」「二级客户端复用主名策略」「已连接客户端的策略被缓存」这三个关键细节。

本讲是 u6（Broker 内部）的最后一讲，建立在 u6-l1（连接生命周期 `handle_peer`/`handle_reader`）之上——AAA 的鉴权点就嵌在那条生命周期里。

## 2. 前置知识

在进入源码前，先用大白话建立三个直觉。

**为什么需要 AAA。** 当 Broker 只通过线程内通道（嵌入模式）或 Unix socket 在同一台机器上提供服务时，能连上来的进程基本可信。但一旦你用 `spawn_tcp_server` 或 `spawn_websocket_server`（u6-l2）把总线暴露到网络，任何能连上端口的人都能以任意名字注册客户端、订阅所有主题、向任意对端发消息。BUS/RT 本身不带密码/令牌之外的更强身份（注意：`ipc::Config` 的 `token` 只是客户端名字的一部分，见 u4-l2），所以「谁能做什么」需要一个独立的权限层——这就是 AAA。它本质上是一张「客户端名 → 策略」的白名单映射。

**白名单模型。** `ClientAaa::new()` 出厂即「全部放行」（连任意主机、可任意 p2p/publish/subscribe/broadcast）。你的工作是**收窄**：用 `allow_*_to`（只允许某些掩码）或 `deny_*`（全部禁止）把它锁紧。这是一种「默认放行、显式禁止」的写法，所以配置时一定要逐项想清楚，漏配就等于放行。

**掩码与 AclMap。** 四类权限里，p2p 和 broadcast 针对的是**对端名字**（用 `.` 分层，和广播表 `BroadcastMap` 一致，见 u3-l2），publish 和 subscribe 针对的是**主题**（用 `/` 分层，和订阅表 `SubMap` 一致，见 u3-l3）。这两套用同一套底层结构 `AclMap`，只是配置了不同的分隔符与通配符。这正是为什么你会看到 `?`/`*` 与 `+`/`#` 两套符号——它们不是两套引擎，而是同一引擎的两种参数化。

> 名词解释：本讲里「鉴权（authorization）」指「判断某动作是否被允许」，不含身份认证（authentication）。BUS/RT 的 AAA 只做前者。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/broker.rs` | AAA 的全部实现：`ClientAaa` 结构与建造者、`AaaMap` 类型别名、`ServerConfig::aaa_map`、连接时鉴权 `connect_allowed`、逐帧鉴权 `handle_reader` 分支。 |
| `src/lib.rs` | 鉴权失败用的错误码 `ERR_ACCESS` 与 `ErrorKind::Access`。 |
| `examples/broker_aaa.rs` | 唯一一份可运行的 AAA 示例，是本讲实践的蓝本。 |

整条链路可以这样理解：`ServerConfig.aaa_map`（你给的策略表）→ 存进每条连接的 `PeerHandlerParams.aaa_map` → 握手时按客户端名取出对应的 `ClientAaa`（一份快照）→ `connect_allowed` 卡连接、`handle_reader` 卡每一帧。

## 4. 核心概念与源码讲解

### 4.1 AAA 的整体结构：AaaMap、ServerConfig 与 ClientAaa

#### 4.1.1 概念说明

AAA 由三个角色组成：

- **`AaaMap`**：一张「客户端名 → 策略」的总表，是 `Arc<SyncMutex<HashMap<String, ClientAaa>>>`，可以在运行时被改写。
- **`ServerConfig`**：每条监听器（unix/tcp/websocket）的配置，通过 `.aaa_map(map)` 把总表挂上去。
- **`ClientAaa`**：单个客户端名的具体策略，含四类操作权限和一份主机白名单。

为什么 `AaaMap` 是 `Arc<Mutex<...>>` 而不是普通 `HashMap`？因为示例注释明确写道「这张表之后随时可以改」（见 4.4.4 的实践），而多个 accept 循环可能同时读取它，所以它必须可在多任务间共享、可在线程安全地修改。

#### 4.1.2 核心流程

```
启动时：
  AaaMap::default()  →  往里 insert 若干 (name, ClientAaa)
  ServerConfig::new().aaa_map(aaa_map)  →  把表挂到某监听器的 config 上
  broker.spawn_tcp_server(addr, config)

每来一个连接（handle_peer）：
  从 PeerHandlerParams 取出 aaa_map
  按客户端「主名」取出对应的 ClientAaa（取出后是 Clone 的快照）
  传给 handle_reader 逐帧使用
```

#### 4.1.3 源码精读

`AaaMap` 就是一个被 `Arc` 包裹、用 `parking_lot` 同步互斥锁保护的「名字→策略」映射（互斥锁类型 `SyncMutex` 在 `rt` feature 下会切换为无自旋锁实现，见 u7-l1）：

[src/broker.rs:844](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L844) — `AaaMap` 类型别名，把「客户端名」映射到它的 `ClientAaa` 策略。

`ServerConfig` 是监听器级配置，`aaa_map` 是其中的一个**可选**字段（默认 `None` = 不启用 AAA，全部放行）：

[src/broker.rs:846-L853](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L846-L853) — `ServerConfig` 结构，`aaa_map: Option<AaaMap>` 默认为 `None`，意味着「没挂 AAA 表 = 不做任何限制」。

[src/broker.rs:888-L891](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L888-L891) — `ServerConfig::aaa_map()` 建造者方法，把总表装进 config。

`ClientAaa` 把四类操作拆成「精确掩码表 + 是否任意放行」的组合（每种操作都是一个 `AclMap` 加一个 `_any` 布尔），外加一份主机白名单 `hosts_allow`：

[src/broker.rs:900-L911](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L900-L911) — `ClientAaa` 结构体：`hosts_allow`（主机白名单）+ 四对 `allow_xxx_to: AclMap` / `allow_xxx_any: bool`。

注意四种 `AclMap` 的初始化参数是**有规律地分裂成两套**的（见 `Default` 实现）：

[src/broker.rs:913-L929](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L913-L929) — `ClientAaa::default()`：`hosts_allow` 默认放行 `0.0.0.0/0`（任意 IPv4），四类权限的 `_any` 全为 `true`；p2p/broadcast 用 `.separator('.').wildcard("*").match_any("?")`，publish/subscribe 用 `.separator('/').wildcard("#").match_any("+")`。

这一行极其重要：它把「对端名掩码」与「主题掩码」两套符号的根源固定下来，与广播表/订阅表的约定（u3-l2、u3-l3）完全一致。`AclMap` 来自 `submap` crate（`use submap::{AclMap, BroadcastMap, SubMap};` 见 [src/broker.rs:34](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L34)），`.matches(具体值)` 用于判定某个具体名字/主题是否命中已注册的掩码。

#### 4.1.4 代码实践

**目标**：把「三角色」串起来，对照官方示例确认你理解了数据流。

1. 打开 `examples/broker_aaa.rs`，定位三行：
   - `let aaa_map = AaaMap::default();`（建总表）
   - `map.insert("test".to_owned(), ClientAaa::new()...);`（往表里塞策略）
   - `let config = ServerConfig::new().aaa_map(aaa_map);`（把表挂到 config）
2. 对照 4.1.2 的流程图，在纸上画出：`AaaMap` → `ServerConfig.aaa_map` → `spawn_tcp_server` →（每连接）`PeerHandlerParams.aaa_map` →（握手后）`ClientAaa` 快照 → `handle_reader`。
3. **需要观察的现象**：三行代码之外，没有任何「注册回调」或「每帧查表」的显式代码——AAA 的检查全在 `handle_peer`/`handle_reader` 内部自动发生。
4. **预期结果**：你能说清「总表是共享的、快照是每连接私有的一份」这一关键事实，为 4.4 讲的「缓存」埋下伏笔。
5. 运行结果待本地验证（本步骤是阅读型实践，无需运行）。

#### 4.1.5 小练习与答案

**练习 1**：如果不调用 `ServerConfig::aaa_map(...)`，AAA 是否生效？为什么？
**答案**：不生效。`ServerConfig::aaa_map` 默认是 `None`，`handle_peer` 里 `if let Some(aaa_map) = params.aaa_map` 整段被跳过，`handle_reader` 收到 `aaa = None`，所有分支都走 `else { true }`（全放行）。

**练习 2**：`AaaMap` 为什么用 `Arc<SyncMutex<...>>` 而不是 `RwLock`？
**答案**：因为示例允许「之后随时改表」，需要在多个 accept 任务间共享同一份表，`Arc` 提供共享、`SyncMutex` 提供并发安全的读写。读多写少理论上可用 `RwLock`，但这里读的频率（仅连接握手时读一次）并不高，且鉴权快照在连接时就 Clone 走了，锁竞争极轻，用普通互斥锁更简单。

---

### 4.2 ClientAaa 建造者：四类权限掩码与 hosts_allow

#### 4.2.1 概念说明

`ClientAaa` 用建造者模式（builder）配置。每一类操作都有一对互补的方法：

- `allow_xxx_to(masks)`：只允许匹配这些掩码的目标（**会先把 `_any` 关掉**）。
- `deny_xxx()`：彻底禁止这类操作（`_any = false` 且清空掩码表）。

主机白名单单独用 `hosts_allow(networks)` 配置，里面是 `IpNetwork`（CIDR 网段，如 `127.0.0.0/8`）。

#### 4.2.2 核心流程

判定「某个具体动作是否允许」用统一公式（设当前动作类别为 X）：

\[
\text{allowed} = \text{allow\_X\_any} \;\lor\; \text{allow\_X\_to.matches(target)}
\]

即「要么任意放行，要么命中掩码表」。`_any` 是一条快路径：为 `true` 时连掩码表都不用查。

建造者方法对 `_any` 的处理有个容易踩坑的细节：`allow_xxx_to` 一进来**无条件**把 `_any` 置为 `false`，然后遍历掩码时，若遇到「全放行哨兵」再把 `_any` 拨回 `true`。p2p/broadcast 的哨兵是 `"*"`，publish/subscribe 的哨兵是 `"#"`（与各自的「多层通配符」字符一致）。

```
allow_p2p_to(&["test"]):
  allow_p2p_any = false
  插入 "test"
  → 允许且仅允许 p2p 到 "test"

allow_p2p_to(&["*"]):
  allow_p2p_any = false
  遇到 "*" → allow_p2p_any = true（又恢复任意）
  → 等价于没限制（写法上多余，但不会出错）

deny_p2p():
  allow_p2p_any = false, allow_p2p_to 清空
  → 彻底禁止 p2p
```

#### 4.2.3 源码精读

`hosts_allow` 用 `IpNetwork` 集合，`connect_allowed` 逐个判断 `addr` 是否落在某个允许网段内：

[src/broker.rs:936-L940](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L936-L940) — `hosts_allow`：替换主机白名单（默认 `0.0.0.0/0`）。

以 p2p 为代表看一对方法（publish/subscribe/broadcast 形式完全对称，只是掩码符号与哨兵不同）：

[src/broker.rs:947-L957](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L947-L957) — `allow_p2p_to`：先把 `allow_p2p_any` 关掉，逐个插入掩码；遇到 `"*"` 再把 `allow_p2p_any` 打开。文档注释列出了对端名掩码的写法（`group.?.*`、`group.subgroup.client` 等，`.` 分层、`?` 单层、`*` 多层）。

[src/broker.rs:958-L963](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L958-L963) — `deny_p2p`：`_any = false` 并清空掩码表，即完全禁止 p2p。

主题类（publish/subscribe）的方法注释列出了主题掩码写法（`topic/+#`、`topic/#`，`/` 分层、`+` 单层、`#` 多层），哨兵是 `"#"`：

[src/broker.rs:970-L980](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L970-L980) — `allow_publish_to`：关 `_any`、插入掩码、遇 `"#"` 重开 `_any`。

[src/broker.rs:1004-L1009](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1004-L1009) — `deny_subscribe`：完全禁止订阅（这正是 `broker_aaa.rs` 里 `test2` 的配置之一）。

> 两套符号速查表：

| 动作 | 针对对象 | 分隔符 | 单层通配 | 多层通配 | 全放行哨兵 |
| --- | --- | --- | --- | --- | --- |
| p2p / broadcast | 对端名 | `.` | `?` | `*` | `"*"` |
| publish / subscribe | 主题 | `/` | `+` | `#` | `"#"` |

`connect_allowed` 是 `ClientAaa` 上**唯一**一个非建造者方法（私有，`fn` 而非 `pub fn`），返回「该 IP 是否被允许连接」：

[src/broker.rs:1033-L1041](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1033-L1041) — `connect_allowed(addr)`：遍历 `hosts_allow`，只要 `addr` 落在任一允许网段即放行。

#### 4.2.4 代码实践

**目标**：亲手写出「客户端 B 仅允许向 `news/#` 发布、禁止订阅、禁止广播、仅能 p2p 给 `test`」这条策略。

1. 在 `examples/broker_aaa.rs` 里找到 `test2` 的那段链式调用，见 [examples/broker_aaa.rs:31-L37](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/examples/broker_aaa.rs#L31-L37)：

```rust
// 示例代码（来自 examples/broker_aaa.rs）
ClientAaa::new()
    .allow_publish_to(&["news/#"])
    .deny_subscribe()
    .deny_broadcast()
    .allow_p2p_to(&["test"]),
```

2. 逐行推断每步之后 `_any` 与掩码表的状态（按下表填空）：

| 调用后 | `allow_publish_any` | `allow_publish_to` | `allow_subscribe_any` | `allow_p2p_any` | `allow_p2p_to` |
| --- | --- | --- | --- | --- | --- |
| `ClientAaa::new()` | true | 空 | true | true | 空 |
| `.allow_publish_to(&["news/#"])` | false | {news/#} | true | true | 空 |
| `.deny_subscribe()` | false | {news/#} | false(表清空) | true | 空 |
| `.deny_broadcast()` | ? | ? | ? | ? | ? |
| `.allow_p2p_to(&["test"])` | ? | ? | ? | false | {test} |

3. **预期结果**：你能填出剩余两行（broadcast 与 publish 列在最后两步不变），并解释为什么 `allow_p2p_to(&["test"])` 之后 `allow_p2p_any` 变 `false`。
4. 运行结果待本地验证（填表型实践）。

#### 4.2.5 小练习与答案

**练习 1**：`ClientAaa::new().allow_publish_to(&["news/#", "#"])` 最终的 publish 权限是什么？
**答案**：进入方法时 `allow_publish_any` 被置 `false`；插入 `news/#` 后 `any` 仍为 `false`；遇到 `"#"` 时 `any` 被拨回 `true`。所以最终 `allow_publish_any == true`，即「任意发布」——`news/#` 这条掩码反而没起限制作用。这是一个易错点：把 `"#"` 和 `news/#` 混在一起传会意外放开全部权限。

**练习 2**：为什么 p2p 的哨兵是 `"*"` 而 publish 的哨兵是 `"#"`？
**答案**：哨兵与该类别「多层通配符」的字符保持一致。p2p/broadcast 的多层通配是 `*`（对端名，`.` 分层），publish/subscribe 的多层通配是 `#`（主题，`/` 分层）。这样「全放行」与「匹配所有」用同一个符号，语义统一。

---

### 4.3 连接时鉴权：connect_allowed、hosts_allow 与 ClientIp

#### 4.3.1 概念说明

第一道关卡发生在**握手阶段**（`handle_peer`，见 u6-l1）：客户端报上自己的名字后，Broker 做两件事——

1. **名字必须在 AAA 表里**：按客户端「主名」查 `AaaMap`，查不到直接拒绝（`ERR_ACCESS`）。
2. **来源 IP 必须被该策略的 `hosts_allow` 放行**：查到了还要看 `connect_allowed(addr)`。

这里有个关键细节：**Unix socket 连接会绕过 IP 检查**。因为 Unix socket 没有 IP 地址（`ClientIp::No`），而「能连上 Unix socket」本身就已经说明是本机进程，没必要再用网段卡。

另一个关键细节：**按「主名」查表**。二级客户端名（如 `svc%%0`，`%%` 是 `SECONDARY_SEP`，见 u3-l2）会被截断成主名 `svc` 后再查表。也就是说，一个主名下所有二级客户端**共用同一份 AAA 策略**。

#### 4.3.2 核心流程

```
handle_peer 握手：读到 client_name（u16 长度 + 名字字节）
  client_primary_name = client_name 中第一个 %% 之前的部分

  if 存在 aaa_map:
      aaa = aaa_map.lock().get(client_primary_name).cloned()
      if aaa 不存在:
          回 ERR_ACCESS，断开（"Client not in AAA map"）
      else if params.ip 是 ClientIp::Addr(addr):
          if !aaa.connect_allowed(addr):
              回 ERR_ACCESS，断开（"not allowed to connect from {addr}"）
      # ClientIp::No（unix）跳过 IP 检查
  else:
      aaa = None  # 没挂 AAA 表，不做连接级鉴权

  继续注册客户端、把 aaa 快照传给 handle_reader
```

注意「`.cloned()`」：取出来的是一份 `ClientAaa` 副本，之后该连接每帧都用这份快照判定，**不再回查总表**。这就是「已连接客户端的策略被缓存」的来源。

#### 4.3.3 源码精读

`ClientIp` 是个内部枚举，区分「有 IP（TCP/WebSocket）」与「无 IP（Unix）」：

[src/broker.rs:1278-L1281](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1278-L1281) — `enum ClientIp { No, Addr(IpAddr) }`。

两个 `From` 实现决定了不同传输的 IP 取值——Unix 永远是 `No`，TCP/WebSocket 取对端 IP：

[src/broker.rs:1283-L1293](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1283-L1293) — `From<unix::SocketAddr>` 产出 `ClientIp::No`；`From<std::net::SocketAddr>` 产出 `ClientIp::Addr(addr.ip())`。

`spawn_server!` 宏在 accept 到连接后用 `addr.into()` 把地址转成 `ClientIp` 传进 `handle_connection`：

[src/broker.rs:1241](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1241) — `addr.into()`：Unix accept 的地址转成 `ClientIp::No`，TCP accept 的转成 `ClientIp::Addr(..)`。

`handle_peer` 里的连接级鉴权（这是本模块最核心的一段）：

[src/broker.rs:1772-L1797](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1772-L1797) — 先取主名（`%%` 前），再在 `aaa_map` 里查；查不到回 `ERR_ACCESS`；查到且 `params.ip` 是 `Addr(addr)` 时调 `connect_allowed(addr)`，不过则回 `ERR_ACCESS`。`ClientIp::No`（Unix）会跳过 IP 判定。最后 `aaa`（一份 `Clone`）被传给 `handle_reader`。

`ERR_ACCESS` 与 `ErrorKind::Access` 定义在 `lib.rs`（错误码 `0x79`，见 u2-l1 的错误体系）：

[src/lib.rs:35](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L35) — `pub const ERR_ACCESS: u8 = 0x79;`。

[src/lib.rs:101](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L101) — `Access = ERR_ACCESS`（`ErrorKind` 变体，`#[repr(u8)]`，判别值即线上字节）。

#### 4.3.4 代码实践

**目标**：验证「Unix socket 绕过 IP 检查」与「主名查表」两个结论。

1. 阅读上面 1772–1797 这段，确认两件事：
   - `hosts_allow` 检查被包在 `if let ClientIp::Addr(addr) = params.ip { ... }` 里；
   - 查表用的是 `client_primary_name`（`%%` 前的主名），不是完整 `client_name`。
2. **思考题（无需运行）**：假设你给 `test` 配了 `hosts_allow(["127.0.0.0/8"])`，然后用 `busrt` CLI 通过 **Unix socket** 以 `test%%3` 连接。请推断：
   - (a) 这次连接能成功吗？
   - (b) `%%3` 这个二级名会不会因为「不在 AAA 表」而被拒？
3. **预期结果**：(a) 能成功——Unix socket 的 `params.ip` 是 `ClientIp::No`，IP 检查整段跳过，`127.0.0.0/8` 不生效；(b) 不会——查表用主名 `test`，`test%%3` 截断后是 `test`，命中策略。
4. 运行结果待本地验证（若想实测，可改 `broker_aaa.rs` 增加 `spawn_unix_server`，用 CLI 经 unix 连接观察）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `connect_allowed` 只在 `ClientIp::Addr` 分支里被调用，而不是对所有连接都调？
**答案**：Unix socket 没有对端 IP（`ClientIp::No`），无从做网段判断；而且能连上 Unix socket 文件的进程天然在本机，再用 IP 白名单卡没有意义。所以只有 TCP/WebSocket 这类带 IP 的连接才走 `connect_allowed`。

**练习 2**：如果一个客户端在总表里被删掉了，但它的连接还活着，会被立即踢下线吗？
**答案**：不会。`handle_peer` 只在握手时查一次表并 `Clone` 走快照，后续每帧都用快照判定，不再回查总表。要让它重新鉴权，必须先 `force_disconnect` 让它断开、重连时才会重新查表（这正是 `broker_aaa.rs` 里每 5 秒 `force_disconnect("test2")` 的用意）。

---

### 4.4 逐帧鉴权：handle_reader 中的 AAA 分支

#### 4.4.1 概念说明

第二道关卡发生在**每一条入站业务帧**上（`handle_reader`，见 u6-l1）。握手时拿到的 `ClientAaa` 快照随帧传入，对四类操作分别套用 4.2 的统一公式：

\[
\text{allowed} = \text{allow\_X\_any} \;\lor\; \text{allow\_X\_to.matches(target)}
\]

- `FrameOp::SubscribeTopic` → 查 `allow_subscribe_*`；
- `FrameOp::Message`（点对点）→ 查 `allow_p2p_*`；
- `FrameOp::Broadcast` → 查 `allow_broadcast_*`；
- `FrameOp::PublishTopic` / `PublishTopicFor` → 查 `allow_publish_*`。

被拒后的反馈方式有个**贯穿全库的关键设计**：拒绝只通过 ACK 帧回报，且**仅当该帧的 QoS 要求确认（`qos.needs_ack()`）时**才回 `ERR_ACCESS` 的 ACK。如果客户端用 `QoS::No` 发送，被拒的操作会被**静默丢弃**——客户端收不到任何错误。

#### 4.4.2 核心流程

```
handle_reader 收到一帧，拆出 op、target、qos：
  switch op:
    SubscribeTopic:  for 每个主题 t:
        allowed = allow_subscribe_any || allow_subscribe_to.matches(t)
        if allowed: 真正订阅
        else if needs_ack: send_ack!(ERR_ACCESS, realtime)
    Message(p2p):
        allowed = allow_p2p_any || allow_p2p_to.matches(target)
        if allowed: send!()
        else if needs_ack: send_ack!(ERR_ACCESS)
    Broadcast:
        allowed = allow_broadcast_any || allow_broadcast_to.matches(target)
        ... 同上 ...
    PublishTopic / PublishTopicFor:
        allowed = allow_publish_any || allow_publish_to.matches(target)
        ... 同上 ...
```

注意 `SubscribeTopic` 是按「主题列表」逐个判定的（一帧可含多个用 `0x00` 分隔的主题），被拒的主题只是不进订阅表，其它主题照常订阅——而不是整帧失败。

#### 4.4.3 源码精读

订阅鉴权（注意是逐主题判定，且 `else if qos.needs_ack()` 才回 ACK）：

[src/broker.rs:1986-L1995](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1986-L1995) — `allow_subscribe_any || allow_subscribe_to.matches(topic)` 决定该主题能否订阅；不允许时仅 `qos.needs_ack()` 才 `send_ack!(ERR_ACCESS, qos.is_realtime())`。

点对点鉴权（`FrameOp::Message`），允许才进 `send!` 宏（见 u3-l3）：

[src/broker.rs:2092-L2117](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L2092-L2117) — `allow_p2p_any || allow_p2p_to.matches(target)`；通过则 `send!`，否则（且需要确认时）回 `ERR_ACCESS`。

广播鉴权（`FrameOp::Broadcast`）：

[src/broker.rs:2120-L2144](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L2120-L2144) — `allow_broadcast_any || allow_broadcast_to.matches(target)`；通过则 `send_broadcast!`。

发布鉴权（`FrameOp::PublishTopic`，`PublishTopicFor` 形式相同）：

[src/broker.rs:2146-L2171](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L2146-L2171) — `allow_publish_any || allow_publish_to.matches(target)`；通过则 `publish!`。

`send_ack!` 宏拼出 6 字节的 ACK 帧（`OP_ACK | op_id(4) | code(1)`，结构见 u2-l3），`code` 处填 `ERR_ACCESS`：

[src/broker.rs:1946-L1951](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1946-L1951) — `send_ack!` 宏：`buf[0]=OP_ACK`，`buf[1..5]=op_id`，`buf[5]=code`（鉴权失败时为 `ERR_ACCESS`）。

`ERR_ACCESS` 在客户端侧还原成 `ErrorKind::Access`（u2-l1 已讲过 `From<u8>` 解码），所以客户端会拿到一个 `ErrorKind::Access` 错误。

#### 4.4.4 代码实践（本讲主实践）

**目标**：运行官方 `broker_aaa` 示例，用两个客户端验证 `test2` 的越权操作被拒绝。

`broker_aaa.rs` 启动一个 TCP Broker（`0.0.0.0:7777`），配置：`test` 可任意操作（仅限 `127.0.0.0/8` 主机），`test2` 仅能向 `news/#` 发布、禁止订阅、禁止广播、仅能 p2p 给 `test`，且每 5 秒被强制断开一次（演示「改表后需重连才重新鉴权」）。

**操作步骤**：

1. 终端 A：启动示例 broker（需 `broker-rpc` feature 提供核心 RPC）。
   ```bash
   cargo run --example broker_aaa --features broker-rpc
   ```
2. 终端 B：用 `busrt` CLI 以 `test2` 身份连接，尝试订阅 `#`（应被拒）。
   ```bash
   cargo run --features cli --bin busrt -- \
       -n test2 -C tcp://127.0.0.1:7777 listen '#'
   ```
3. 终端 C：先以 `test` 身份订阅 `news/#`（接收方），再以 `test2` 身份向 `news/tech` 发布（应成功），并尝试 p2p 发给 `test`（应成功）。
   ```bash
   # 接收方
   cargo run --features cli --bin busrt -- \
       -n test -C tcp://127.0.0.1:7777 listen 'news/#'
   # 发布（合法）
   cargo run --features cli --bin busrt -- \
       -n test2 -C tcp://127.0.0.1:7777 publish 'news/tech' hi
   ```
4. 终端 D：让 `test2` 尝试越权操作——p2p 发给一个非 `test` 的客户端、或向 `weather/#` 发布、或广播。这些应当失败。

**需要观察的现象**：

- 步骤 2 的 `listen '#'`：`test2` 被 `deny_subscribe()`，订阅应被拒。**能否看到明确的 `Access` 错误取决于 CLI 是否对该操作请求 ACK**——若 CLI 用 `QoS::No`，则表现为「订阅不上、收不到消息」而无报错；若用 `QoS::Processed`，则会看到 `ErrorKind::Access`（待本地验证 CLI 默认 QoS）。
- 步骤 3：`test` 能收到 `news/tech` 的 `hi`。
- 步骤 4：越权操作全部失败，终端 A 的 broker 日志应打印含 `test2` 的 access 错误（因为 `handle_reader` 返回的错误经 `format_result!` 拼上了 `[test2]` 前缀，见 u6-l1）。
- 终端 A 每 5 秒打印一次 `forcing test2 disconnect`，随后 `test2` 的连接被踢。

**预期结果**：`test2` 的合法操作（向 `news/#` 发布、p2p 给 `test`）成功，越权操作（订阅任意、广播、p2p 给他人、向 `weather/#` 发布）被拒。具体 CLI 报错文案与默认 QoS 待本地验证。

> 提示：本实践依赖 `cli` 与 `broker-rpc` 两个 feature，且需要能编译两个二进制；若本地编译受限，可退化为「源码阅读型实践」——只跟踪 4.4.3 的四个分支，说明每种 `FrameOp` 走哪条 `allow_*` 判定。

#### 4.4.5 小练习与答案

**练习 1**：`test2` 用 `QoS::No` 发一条被禁止的 p2p 消息，客户端会得到什么反馈？为什么？
**答案**：得不到任何反馈（操作被静默丢弃）。因为 `handle_reader` 里拒绝分支是 `else if qos.needs_ack() { send_ack!(...) }`，`QoS::No` 的 `needs_ack()` 为 `false`，既不回 `RESPONSE_OK` 也不回 `ERR_ACCESS`。这是「确认可选」设计（u4-l1）在鉴权上的体现：**想观测到鉴权失败，必须用要求确认的 QoS**。

**练习 2**：把 `broker_aaa.rs` 里 `test2` 的 `deny_subscribe()` 改成 `allow_subscribe_to(&["news/#"])`，然后 `test2` 订阅 `news/tech` 会怎样？订阅 `news/x/y` 呢？
**答案**：`news/#` 的 `#` 是多层通配，`news/tech`（一层）和 `news/x/y`（两层）都能匹配，所以两者都允许订阅。注意 `allow_subscribe_to` 会把 `allow_subscribe_any` 置 `false`，因此除 `news/` 子树外的主题（如 `weather/x`）仍被拒。

**练习 3**：为什么所有 AAA 拒绝都用 `ERR_ACCESS`（`0x79`）而不是各自单独的错误码？
**答案**：统一用 `ErrorKind::Access` 让客户端无需区分「被哪种权限拒」，只需知道「这次操作因权限不足失败」。错误体里带的 message（如 `not allowed to connect from {addr}`）仅在 Broker 侧日志可见，线上回给客户端的只是单字节 `0x79`——既省字节，又不向客户端泄露内部策略细节（只说「不行」，不说「哪条规则挡了你」）。

## 5. 综合实践

把本讲四个模块串成一个完整任务：**为一个暴露在 TCP 上的 Broker 设计一套三角色 AAA，并验证权限矩阵**。

设想场景：一个 IoT 总线有三种客户端——

- `controller`：管理员，可任意操作，但只能从内网 `10.0.0.0/8` 连接。
- `sensor`：传感器，**只能发布**到 `sensors/#`，不能订阅、不能 p2p、不能广播。
- `dashboard`：看板，只能订阅 `sensors/#` 和 `status/#`，不能发布、不能 p2p。

任务：

1. 仿照 `examples/broker_aaa.rs`，新建一份示例（或直接改它），用 `ClientAaa` 建造者配置这三个角色，挂到 `ServerConfig::aaa_map`，并用 `spawn_tcp_server` 监听。
2. 写出每个角色的 builder 链，并自行推断每条链执行后四对 `_any`/掩码表的状态（用 4.2.4 的表格）。
3. 用三个 `busrt` CLI 客户端（或三份 `ipc::Client` 程序）分别以这三个名字连接，验证：
   - `sensor` 向 `sensors/temp` 发布成功，向 `status/x` 发布失败；
   - `sensor` 订阅任意主题失败（被 `deny_subscribe`）；
   - `dashboard` 订阅 `sensors/#` 成功并能收到 `sensor` 的发布；
   - `controller` 从 `127.0.0.1`（不在 `10.0.0.0/8`）连接应被 `connect_allowed` 拒绝（若你把 `controller` 的 `hosts_allow` 设为 `10.0.0.0/8`）。
4. 验证「缓存」效应：在程序运行中往 `aaa_map` 里改 `sensor` 的策略（比如加一条允许发布 `status/#`），观察**已连接的 `sensor` 不受影响**；只有 `force_disconnect("sensor")` 后重连，新策略才生效。

这个任务覆盖了：建造者配置（4.2）、连接时 IP 鉴权（4.3）、逐帧四类鉴权（4.4）、以及「快照缓存 + force_disconnect 重鉴权」这条贯穿全讲的暗线。运行结果待本地验证。

## 6. 本讲小结

- AAA 是一张「客户端主名 → `ClientAaa` 策略」的白名单总表 `AaaMap`，经 `ServerConfig::aaa_map` 挂到监听器；默认不挂表即「全部放行」。
- `ClientAaa` 把四类操作（p2p/publish/subscribe/broadcast）各拆成「`AclMap` 掩码表 + `_any` 布尔」，外加主机白名单 `hosts_allow`；判定公式统一为 `allow_X_any || allow_X_to.matches(target)`。
- 两套掩码符号同源不同参：对端名用 `.`/`?`/`*`（p2p、broadcast），主题用 `/`/`+`/`#`（publish、subscribe）；全放行哨兵分别是 `"*"` 与 `"#"`，混进普通掩码会意外放开权限。
- 鉴权分两道：连接时按主名查表 + `connect_allowed(IP)`，逐帧时在 `handle_reader` 按 `FrameOp` 查对应权限；被拒仅当 `qos.needs_ack()` 时回 `ERR_ACCESS`（`0x79`）的 ACK，`QoS::No` 的越权操作被静默丢弃。
- 两个易被忽略的事实：**Unix socket 绕过 IP 检查**（`ClientIp::No`）；**握手时 `Clone` 走快照**，已连接客户端的策略被缓存，改表后需 `force_disconnect` 重连才重新鉴权。

## 7. 下一步学习建议

- 本讲把 u6（Broker 内部）的连接生命周期、多传输、AAA 三块讲完。接下来可进入 **u7-l1 实时特性**：`QoS::Realtime` 如何影响刷新与队列，以及 `parking_lot_rt` 在 `rt` feature 下对 `SyncMutex`（本讲 `AaaMap` 用的就是它）的实时安全替换——你会看到 AAA 的锁在实时场景下如何被重新选型。
- 若对「策略可热更新」感兴趣，可继续读 `Broker::force_disconnect`（[src/broker.rs:1441-L1443](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1441-L1443)）与它调用的 `BrokerDb::trigger_disconnect`，理解「重连即重鉴权」的实现。
- 想从客户端视角体验鉴权失败，可读 **u8-l2 busrt CLI** 里 `send`/`publish`/`rpc call` 如何把 `ErrorKind::Access` 展示给用户，把本讲的「静默丢弃 vs ACK 报错」在工具层闭环。
