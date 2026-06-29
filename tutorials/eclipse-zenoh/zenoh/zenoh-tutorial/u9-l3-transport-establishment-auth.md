# 传输建连、认证与内部状态机

## 1. 本讲目标

本讲承接《u9-l2 Transport 层：unicast 与 multicast 管理》。上一讲只回答了「谁建连、谁保活、参数从哪来」，并把 Open/Close 握手、认证、内部状态机留给了本讲。学完之后，你应该能够：

- 说清两条 Zenoh 节点之间一条 **unicast 传输**是如何从一条裸 Link「升级」出来的：InitSyn → InitAck → OpenSyn → OpenAck 这四次握手的每一步交换了什么、协商出了什么。
- 解释 **Cookie 机制**为什么能让服务端在 Init 阶段保持「无状态」，以及它如何防资源耗尽。
- 掌握两种内置认证 **auth_usrpwd（用户名密码）** 与 **auth_pubkey（公钥）** 的挑战—应答流程，以及它们的配置键名与接入点。
- 读懂 `transport_unicast_inner` 的 `TransportStatus` 状态机（`Uninitialized → Alive → Closed`）、`add_link` 返回的「两个闭包 + 一个 OpenAck」设计，以及 `schedule` 如何把消息送上网。
- 区分 **unicast 的握手建连** 与 **multicast 的 Join 收发**：为什么组播没有 Init/Open 握手、没有认证、没有 Cookie，而是靠周期性 Join + lease 维持成员关系。

本讲全部位于 `io/zenoh-transport/` 这个**内部 crate**（不保证稳定，应用不应直接依赖），是理解 Zenoh「通电时刻」的关键一讲。

## 2. 前置知识

阅读本讲前，请确认你理解以下概念（均来自前置讲义）：

- **Transport 层管理**（u9-l2）：`TransportManager` 用统一 builder 管理 unicast/multicast；unicast 按**对端 zid** 索引传输表，主动建连走 `open_transport_unicast`、被动接入走 `handle_new_link_unicast`；KeepAlive 间隔 \(=\) `lease`/`keep_alive`。本讲正是上一讲刻意回避的「握手细节」。
- **Link 层**（u9-l1）：`Link` / `LinkManager` 把 endpoint 变成可读写字节连接；握手就跑在这样的裸 Link 之上。
- **WhatAmI 与 ZenohId**（u2-l3、u2-l1）：握手要交换的正是双方的 `whatami`（router/peer/client）与 `zid`（节点唯一标识）。
- **QoS 与 Priority**（u3-l3）：握手要协商是否启用 QoS 分道；传输内部为每个优先级各维护一套收发结构。
- **协议消息模型**（u10-l1，可后置）：Init/Open/Close/Join 都属于 transport 层消息。

一句话定位：**Transport 管理层决定「连接的参数与生命周期」，本讲决定「连接如何被真正建立出来、如何被认证、建立后内部状态如何流转」**。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [io/zenoh-transport/src/unicast/establishment/mod.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/establishment/mod.rs) | 定义握手两大 FSM trait：`OpenFsm`（发起方）与 `AcceptFsm`（应答方）；以及确定性初始序列号函数 `compute_sn`。 |
| [io/zenoh-transport/src/unicast/establishment/open.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/establishment/open.rs) | **发起方主角**。`OpenLink` 实现 `OpenFsm` 的四步；入口函数 `open_link` 编排完整握手。 |
| [io/zenoh-transport/src/unicast/establishment/accept.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/establishment/accept.rs) | **应答方主角**。`AcceptLink` 实现 `AcceptFsm` 的四步；入口函数 `accept_link`。含 Cookie 生成与校验。 |
| [io/zenoh-transport/src/unicast/establishment/cookie.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/establishment/cookie.rs) | `Cookie` 结构与加解密 codec `Zenoh080Cookie`（用 `BlockCipher` 加密协商状态）。 |
| [io/zenoh-transport/src/unicast/establishment/ext/auth/mod.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/establishment/ext/auth/mod.rs) | 认证总入口：`Auth` 聚合 usrpwd/pubkey 两种机制，自身实现 `OpenFsm`/`AcceptFsm`，作为 Init/Open 的一个扩展挂载。 |
| [io/zenoh-transport/src/unicast/establishment/ext/auth/usrpwd.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/establishment/ext/auth/usrpwd.rs) | 用户名密码认证：HMAC 挑战—应答、字典文件加载。 |
| [io/zenoh-transport/src/unicast/establishment/ext/auth/pubkey.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/establishment/ext/auth/pubkey.rs) | 公钥认证：RSA 双向挑战（互发公钥 + 加密 nonce 互验）。 |
| [io/zenoh-transport/src/unicast/authentication.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/authentication.rs) | `TransportAuthId`：把认证结果（用户名、zid、链路认证 id）汇总给上层。 |
| [io/zenoh-transport/src/unicast/transport_unicast_inner.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/transport_unicast_inner.rs) | `TransportUnicastTrait` 与 `TransportStatus` 状态机的定义。 |
| [io/zenoh-transport/src/unicast/universal/transport.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/universal/transport.rs) | 通用传输实现：`add_link`（返回启动闭包 + OpenAck）、`sync`（状态机迁移）、`schedule`、`get_auth_ids`。 |
| [io/zenoh-transport/src/unicast/link.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/link.rs) | `TransportLinkUnicast`（握手期收发）与 `LinkUnicastWithOpenAck` / `MaybeOpenAck`（延迟发送 OpenAck 的载体）。 |
| [io/zenoh-transport/src/unicast/manager.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/manager.rs) | `init_transport_unicast`（注册进表）、`init_new/existing_transport_unicast`、`open_transport_unicast_inner`、`handle_new_link_unicast`。把握手与状态机串起来。 |
| [io/zenoh-transport/src/multicast/transport.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/multicast/transport.rs) | 组播传输内部：`new_peer`（收到 Join 注册成员 + lease 看门狗）、`del_peer`、TX/RX 启动。 |
| [commons/zenoh-protocol/src/transport/init.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/transport/init.rs) | `InitSyn` / `InitAck` 协议消息结构（含各扩展定义）。 |
| [commons/zenoh-protocol/src/transport/open.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/transport/open.rs) | `OpenSyn` / `OpenAck` 协议消息结构。 |
| [commons/zenoh-protocol/src/transport/close.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/transport/close.rs) | `Close` 消息与全部 `reason` 码（INVALID / MAX_LINKS / MAX_SESSIONS / EXPIRED …）。 |

## 4. 核心概念与源码讲解

### 4.1 establishment 握手：四次消息把裸 Link 升级成传输

#### 4.1.1 概念说明

上一讲我们看到 `open_transport_unicast` 会「发起握手」，但没说握手长什么样。Zenoh 的 unicast 建连采用**两个阶段、四次消息**的有限状态机（FSM）：

```text
    发起方 (Open)                        应答方 (Accept)
    ─────────────                        ──────────────
1.  ──── InitSyn ────>                   ① 接收，校验版本/协商参数
2.                  <──── InitAck ────   ② 回带 Cookie（加密的协商状态）
3.  ──── OpenSyn ────>                   ③ 用 Cookie 恢复状态，校验 lease/sn
4.                  <──── OpenAck ────   ④ 延迟到 add_link 成功后才发送
```

- **第一阶段 Init（InitSyn/InitAck）**：交换 `version` / `whatami` / `zid`，协商 `resolution`（序列号/请求 id 的位宽）与 `batch_size`（取两端最小值），并探测各项扩展能力（QoS、SHM、Auth、MultiLink、LowLatency、Compression…）。应答方在此阶段**把所有协商结果打包进一个加密 Cookie 回传**，自己不留状态。
- **第二阶段 Open（OpenSyn/OpenAck）**：发起方把 Cookie 原样回传（`OpenSyn.cookie == InitAck.cookie`），并正式提交 `lease` 与 `initial_sn`；应答方解密 Cookie 恢复状态、校验 nonce，最后回 `OpenAck` 携带己方的 `lease` 与 `initial_sn`。到此传输才算「通电」。

两个 FSM 的接口由一对 trait 定义：发起方 `OpenFsm`、应答方 `AcceptFsm`，各四个异步步骤，命名严格对称。

> 术语：下文把「发起方」也叫 Open 侧或 client 侧行为，「应答方」叫 Accept 侧或 server 侧行为——但注意这仅指握手角色，与节点的 WhatAmI（router/peer/client）无关：任何节点都可能同时既是发起方（主动 `open`）又是应答方（被动 `accept`）。

#### 4.1.2 核心流程

**发起方**入口是 `open_link`（被 `open_transport_unicast_inner` 调用），用 `step!` 宏串联四步，任一步失败就带原因关闭 Link：

```text
open_link(endpoint, link, manager, expected_zid)
→ 构造 OpenLink FSM（含各扩展子 FSM）+ 初始 State
→ step!(send_init_syn)   // 发 InitSyn：version/whatami/zid + 扩展
→ step!(recv_init_ack)   // 收 InitAck：拿到 other_zid/whatami/cookie，协商 resolution/batch_size
→ 若给了 expected_zid 且不匹配 → close(INVALID) 拒绝
→ step!(send_open_syn)   // 发 OpenSyn：lease + compute_sn 初始序号 + 回传 cookie
→ step!(recv_open_ack)   // 收 OpenAck：拿到 other_bound/lease/initial_sn
→ 用协商结果构造 TransportConfigUnicast
→ manager.init_transport_unicast(config, link, other_initial_sn, other_lease)
```

**应答方**入口是 `accept_link`（被 `handle_new_link_unicast` 在 `ZRuntime::Acceptor` 上 spawn），同样四步：

```text
accept_link(link, manager)
→ 构造 AcceptLink FSM（持 prng + cipher）
→ step!(recv_init_syn)   // 收 InitSyn：校验 version，协商 resolution/batch_size（取 min）
→ step!(send_init_ack)   // 发 InitAck：生成随机 nonce，把 State 加密成 Cookie 回传
                        //    ★ 此后本地 State 释放，靠 Cookie 恢复
→ step!(recv_open_syn)   // 收 OpenSyn：解密 Cookie、校验 nonce、恢复 State、读 lease/initial_sn
→ step!(send_open_ack)   // 构造 OpenAck 但【暂不发送】（怕 MAX_LINKS）
→ 用协商结果构造 TransportConfigUnicast
→ manager.init_transport_unicast(...)   // 内部 add_link 成功后才真正发送 OpenAck
```

**确定性初始序列号**：双方的 `initial_sn` 不是各自随机，而是由 `compute_sn` 从**两个 zid + resolution** 经 Shake128 哈希确定性算出。这样即便 multilink 场景下发起多次连接尝试，每次算出的初始序号都一致，避免状态错乱。

握手期任何一步出错，都会构造一条 `Close` 消息回送对端并关闭 Link。`Close` 的 `reason` 取自一个固定码表，常用的有 `INVALID`（参数非法）、`MAX_LINKS`（链路数超限）、`MAX_SESSIONS`（会话数超限）、`GENERIC`（通用错误）。

#### 4.1.3 源码精读

**两大 FSM trait**——四个方法的命名完全对称，是理解握手「四拍」的钥匙：

[io/zenoh-transport/src/unicast/establishment/mod.rs:35-99](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/establishment/mod.rs#L35-L99) —— `OpenFsm` 有 `send_init_syn / recv_init_ack / send_open_syn / recv_open_ack`；`AcceptFsm` 有 `recv_init_syn / send_init_ack / recv_open_syn / send_open_ack`。每个方法都用关联类型声明了输入/输出，把「每一步交换什么」写成契约。

**确定性初始序列号 `compute_sn`**——为什么不用随机数：

[io/zenoh-transport/src/unicast/establishment/mod.rs:104-118](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/establishment/mod.rs#L104-L118) —— 用 `Shake128` 把 `zid1`、`zid2` 喂进去，输出再 `& seq_num::get_mask(...)` 截到协商出的位宽。注释明说：multilink 时多次连接尝试必须用同一个 `initial_sn`，与其到处存状态，不如「随时重算必然相同」。

**InitSyn 消息结构**——握手第一拍真正发出去的内容：

[commons/zenoh-protocol/src/transport/init.rs:119-136](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/transport/init.rs#L119-L136) —— `InitSyn` 字段：`version`、`whatami`、`zid`、`resolution`、`batch_size`，外加一排可选扩展 `ext_qos / ext_shm / ext_auth / ext_mlink / ext_lowlatency / ext_compression / ext_patch / ext_region_name`。扩展用 `Option<...>`，存在即表示「我支持/我请求」。

**发起方发送 InitSyn**——把各扩展子 FSM 的输出汇总成消息：

[io/zenoh-transport/src/unicast/establishment/open.rs:217-244](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/establishment/open.rs#L217-L244) —— 先逐个调用 `ext_qos.send_init_syn(...)`、`ext_auth.send_init_syn(...)` 等收齐扩展载荷，再组装成 `InitSyn` 并 `link.send(&msg)`。注意大量扩展用 `zcondfeat!("transport_auth", ...)` 宏做**编译期 feature 门控**：未启用该 feature 时直接取 `None`。

**发起方收 InitAck——协商 resolution 与 batch_size**：

[io/zenoh-transport/src/unicast/establishment/open.rs:287-326](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/establishment/open.rs#L287-L326) —— 关键规则：对 `FrameSN` 与 `RequestID` 两种 resolution，**若对端声明的位宽比自己大则判非法**（`INVALID`），否则取对端值；`batch_size` 直接取两端 `min`。这保证双方最终用同一套（更细的）参数。`recv_init_ack` 的输出 `RecvInitAckOut`（[L389-L396](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/establishment/open.rs#L389-L396)）抽出 `other_zid / other_whatami / other_cookie`。

**expected_zid 校验**——防止连错节点（与 orchestrator 的 scouting 建连配合）：

[io/zenoh-transport/src/unicast/establishment/open.rs:729-740](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/establishment/open.rs#L729-L740) —— 若调用方传了期望 zid（`open_transport_unicast_with_zid`），且与 `InitAck` 里对端自报的 zid 不符，立即 `close(INVALID)` 并报错。这就是近期提交「Check for expected ZID when opening scouted links」的落点。

**发起方发 OpenSyn——提交 lease 与初始序号、回传 Cookie**：

[io/zenoh-transport/src/unicast/establishment/open.rs:492-514](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/establishment/open.rs#L492-L514) —— `mine_initial_sn = compute_sn(input.mine_zid, input.other_zid, ...)`，`cookie: input.other_cookie`（原样回传），`lease: input.mine_lease`。`OpenSyn` 的协议结构见 [transport/open.rs:85-98](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/transport/open.rs#L85-L98)（`lease / initial_sn / cookie` + 扩展）。

**发起方收 OpenAck——拿到对端的 lease / 初始序号 / bound**：

[io/zenoh-transport/src/unicast/establishment/open.rs:605-616](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/establishment/open.rs#L605-L616) —— 输出 `other_bound / other_initial_sn / other_lease`，这三项（连同前面协商的 resolution 等）随后填进 `TransportConfigUnicast`。

**应答方收 InitSyn——校验版本**：

[io/zenoh-transport/src/unicast/establishment/accept.rs:192-202](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/establishment/accept.rs#L192-L202) —— `init_syn.version != input.mine_version` 即以 `INVALID` 拒绝（协议版本不匹配是建连失败的最常见原因之一）。

**应答方发 InitAck——生成 Cookie**（本模块最关键的一段）：

[io/zenoh-transport/src/unicast/establishment/accept.rs:381-419](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/establishment/accept.rs#L381-L419) —— 生成随机 `nonce: u64`，把整个协商 `State`（含所有扩展状态）塞进 `Cookie`，用 `Zenoh080Cookie { cipher, prng, codec }` **加密**后作为 `InitAck.cookie` 回传。返回的 `cookie_nonce` 被应答方自己记下，留给第三步校验。

**Cookie 结构与加解密 codec**：

[io/zenoh-transport/src/unicast/establishment/cookie.rs:30-49](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/establishment/cookie.rs#L30-L49) —— `Cookie` 字段就是「协商状态快照」：`zid / whatami / resolution / batch_size / nonce` + 各扩展 `StateAccept`。

[io/zenoh-transport/src/unicast/establishment/cookie.rs:140-174](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/establishment/cookie.rs#L140-L174) —— `Zenoh080Cookie` 的 `write` 先用内层 `Zenoh080` 序列化 Cookie，再 `cipher.encrypt(buff, prng)` 加密；`read` 反之先 `cipher.decrypt` 再反序列化。`BlockCipher` 是对称分组密码（密钥来自 `TransportManager` 启动时生成，进程级）。

**应答方收 OpenSyn——解密 Cookie、校验 nonce、恢复状态**：

[io/zenoh-transport/src/unicast/establishment/accept.rs:504-550](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/establishment/accept.rs#L504-L550) —— `open_syn.cookie` 解密成 `Cookie`；若 `input.cookie_nonce != cookie.nonce` 则判「Unknown cookie」拒绝（[L524-L527](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/establishment/accept.rs#L524-L527)）；随后从 Cookie 字段**重建** `State`（[L530-L550](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/establishment/accept.rs#L530-L550)）。这正是「Init 阶段无状态」的实现：第二阶段的状态完全来自对端回传的 Cookie，而非本地内存。

**应答方构造 OpenAck 但暂不发送**：

[io/zenoh-transport/src/unicast/establishment/accept.rs:706-730](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/establishment/accept.rs#L706-L730) —— 注释直说：「Do not send the OpenAck right now since we might still incur in MAX_LINKS error」。OpenAck 被装进 `LinkUnicastWithOpenAck`（[accept.rs:928](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/establishment/accept.rs#L928)），等 `init_transport_unicast → add_link` 通过了 `MAX_LINKS` / `MAX_SESSIONS` 检查后才由 `MaybeOpenAck::send_open_ack` 真正发出。

**延迟发送 OpenAck 的载体**：

[io/zenoh-transport/src/unicast/link.rs:282-319](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/link.rs#L282-L319) —— `MaybeOpenAck` 持有一个 `Option<OpenAck>`；`send_open_ack` 只在 `Some` 时发送（发起方这边是 `None`，因为它已在 `recv_open_ack` 收到了对端的 OpenAck）。注意它还专门处理了 compression 的小 workaround：发 OpenAck 时临时关掉压缩（OpenAck 不应被压缩）。

**Close reason 码表**——握手失败时携带的原因：

[commons/zenoh-protocol/src/transport/close.rs:22-31](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/transport/close.rs#L22-L31) —— `GENERIC=0 / UNSUPPORTED=1 / INVALID=2 / MAX_SESSIONS=3 / MAX_LINKS=4 / EXPIRED=5 / UNRESPONSIVE=6 / CONNECTION_TO_SELF=7`。握手代码里到处用 `close::reason::INVALID`、`close::reason::MAX_LINKS` 等常量。

#### 4.1.4 代码实践（源码阅读型：追踪一条 unicast 连接的握手时序）

**实践目标**：把「四次消息各自交换了什么、协商出了什么」落到具体源码行，建立可复现的握手心智模型。

**操作步骤**：

1. 打开 [open.rs 的 `open_link`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/establishment/open.rs#L620-L846)，找到 `step!` 宏（[L705-L715](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/establishment/open.rs#L705-L715)）和它包住的四次调用，确认顺序是 `send_init_syn → recv_init_ack → send_open_syn → recv_open_ack`。
2. 对每一次消息，分别去协议结构定义里核对其字段：
   - InitSyn：[transport/init.rs:119-136](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/transport/init.rs#L119-L136)
   - InitAck：[transport/init.rs:237-255](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/transport/init.rs#L237-L255)（注意多了 `cookie` 字段）
   - OpenSyn / OpenAck：[transport/open.rs:85-98](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/transport/open.rs#L85-L98) 与 [L189-L201](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/transport/open.rs#L189-L201)
3. 整理成一张「消息 → 携带的关键字段 → 协商动作」对照表（示例前两行）：

| 消息 | 方向 | 关键字段 | 协商/校验动作 |
| --- | --- | --- | --- |
| InitSyn | 发起→应答 | version/whatami/zid/resolution/batch_size + 扩展 | 应答方校验 version；取 resolution/batch_size 的 min |
| InitAck | 应答→发起 | whatami/zid/resolution/batch_size/**cookie** + 扩展 | 发起方校验对端 resolution 不得超过本地；记下 cookie |

**需要观察的现象**：你会看到 InitAck 与 OpenSyn 都携带 `cookie`，且 OpenSyn 的 cookie 就是 InitAck 里那个；而 OpenAck 不再带 cookie——状态已在应答方恢复完毕。

**预期结果**：产出一张完整的四行对照表，并写出一句结论：「Init 阶段协商参数并以 Cookie 暂存，Open 阶段用 Cookie 恢复状态并提交 lease/initial_sn，四次消息后双方参数完全对齐」。**待本地验证**：若想看真实报文，可在握手代码已有的 `tracing::trace!`（如 [open.rs:236](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/establishment/open.rs#L236)）处开 `RUST_LOG=zenoh_transport=trace` 抓取。

#### 4.1.5 小练习与答案

**练习 1**：为什么应答方在 Init 阶段不保留协商状态、非要绕一圈用加密 Cookie 回传？

**参考答案**：这是一种**抗资源耗尽 / 抗放大攻击**的设计。如果应答方为每个「只发了 InitSyn 还没完成 Open」的半连接都在内存里建状态，攻击者可以用海量伪造源地址的 InitSyn 把应答方内存打爆（类似 TCP SYN flood）。Cookie 把状态序列化、加密后交给发起方保管，应答方在 Init 阶段几乎无状态；只有当发起方用正确 Cookie 回来 Open（证明它真的能收发包，且密钥正确）时，应答方才从 Cookie 恢复状态并真正分配资源。`nonce` 校验（[accept.rs:524-527](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/establishment/accept.rs#L524-L527)）确保 Cookie 未被篡改/重放。

**练习 2**：`compute_sn` 为什么用 `Shake128(zid1, zid2)` 确定性计算，而不是各自随机生成一个初始序号？

**参考答案**：见 [mod.rs:109-111](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/establishment/mod.rs#L109-L111) 注释：multilink（同一对 zid 之间多条物理链路）场景下，发起方可能并发发起多次连接尝试，**每次尝试必须用同一个 `initial_sn`** 才能把多条链路并入同一条传输。与其在到处存状态来保证一致，不如让初始序号成为「两个 zid 的确定函数」——重算必然相同，天然一致。

---

### 4.2 authentication：用户名密码与公钥认证

#### 4.2.1 概念说明

握手里的扩展 `ext_auth`（协议扩展 id `0x3`，类型 `ZExtZBuf`）承载认证。Zenoh 内置两种可选认证机制，各自受独立 feature 门控：

- **auth_usrpwd**（id `0x2`）：用户名 + 密码，用 **HMAC** 做挑战—应答，密码本身不上线。
- **auth_pubkey**（id `0x1`）：RSA 公钥，双方互发公钥并各自用对方公钥加密一个随机 nonce、再用自己私钥解密互验。

二者都遵循同一个「扩展即子 FSM」的设计：`Auth` 聚合器自身实现 `OpenFsm` / `AcceptFsm`，在 Init/Open 的每一步被主 FSM 调用，内部再分派到 usrpwd / pubkey 子 FSM。两种机制可以**同时启用**（互不影响），任一失败则握手失败。

**配置入口**（确认自 [DEFAULT_CONFIG.json5](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/DEFAULT_CONFIG.json5#L764-L780)）：

```json5
transport: {
  auth: {
    usrpwd: { user, password, dictionary_file },   // dictionary_file: 每行 "user:password"
    pubkey: { public_key_pem, private_key_pem, public_key_file, private_key_file, ... },
  },
}
```

认证通过后，usrpwd 模式下被验证的**用户名**会被写进 `TransportConfigUnicast.auth_id`，最终经 `get_auth_ids()` 暴露给上层（路由层可据此做基于用户名的访问控制）。

#### 4.2.2 核心流程

**auth_usrpwd 的挑战—应答**（HMAC）：

```text
发起方                                  应答方
  ─ InitSyn: 标记「我有凭证」(ZExtUnit) ─→   记录对方要用 usrpwd
  ← InitAck: 下发 nonce 挑战 (ZExtZ64) ──     nonce = random u64
  ─ OpenSyn: user + HMAC(nonce, pwd) ───→    查字典取该 user 的 pwd，
                                            重算 HMAC(nonce, pwd) 比对
  ← OpenAck: 确认 (ZExtUnit) ───────────     通过则返回 username 作为 auth_id
```

关键点：密码从不直接上线，上线的是 `HMAC(key=nonce, msg=password)`；nonce 是一次性的，故抓包也无法重放。

**auth_pubkey 的双向挑战**（RSA）：

```text
发起方(Alice)                           应答方(Bob)
  ─ InitSyn: alice 公钥 ───────────────→   查白名单是否允许 alice
  ← InitAck: bob 公钥 + enc_alice(nonce_B) ─ 生成挑战 nonce_B，用 alice 公钥加密
  (alice 用自己私钥解出 nonce_B，再用 bob 公钥加密)
  ─ OpenSyn: enc_bob(nonce_B) ──────────→   bob 用私钥解出，比对 == nonce_B ?
  ← OpenAck: 确认 ─────────────────────     通过
```

#### 4.2.3 源码精读

**认证聚合器 `Auth` 与配置加载**：

[io/zenoh-transport/src/unicast/establishment/ext/auth/mod.rs:51-71](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/establishment/ext/auth/mod.rs#L51-L71) —— `Auth` 持 `Option<RwLock<AuthPubKey>>` 与 `Option<RwLock<AuthUsrPwd>>`；`from_config` 从 `config.transport().auth()` 分别读 pubkey / usrpwd，仅在确有配置时才 `Some`。`Auth::open / accept / fsm` 三个方法（[L73-L111](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/establishment/ext/auth/mod.rs#L73-L111)）按「是否配置了该机制」决定是否实例化对应子 FSM。

**两种机制的 id 常量**：

[io/zenoh-transport/src/unicast/establishment/ext/auth/mod.rs:44-49](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/establishment/ext/auth/mod.rs#L44-L49) —— `PUBKEY = 0x1`、`USRPWD = 0x2`。认证扩展内部是一个 `Vec<ZExtUnknown>`，按这两个 id 分派到子 FSM（`ztake!` / `ztryinto!` 宏，[L264-L283](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/establishment/ext/auth/mod.rs#L264-L283)）。

**usrpwd：发起方发 OpenSyn（HMAC 挑战应答）**：

[io/zenoh-transport/src/unicast/establishment/ext/auth/usrpwd.rs:312-344](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/establishment/ext/auth/usrpwd.rs#L312-L344) —— `key = state.nonce.to_le_bytes()`，`hmac = hmac::sign(&key, password)`，组装 `{ user, hmac }` 发出。注意若 `credentials` 未配置则直接返回 `None`（不发该扩展）。

**usrpwd：应答方下发 nonce 挑战**：

[io/zenoh-transport/src/unicast/establishment/ext/auth/usrpwd.rs:386-393](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/establishment/ext/auth/usrpwd.rs#L386-L393) —— `send_init_ack` 直接 `Ok(Some(ZExtZ64::new(state.nonce)))`，把应答方的随机 nonce 作为挑战下发给发起方。

**usrpwd：应答方校验（核心安全判定）**：

[io/zenoh-transport/src/unicast/establishment/ext/auth/usrpwd.rs:397-428](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/establishment/ext/auth/usrpwd.rs#L397-L428) —— 用 `open_syn.user` 在本地字典 `lookup` 查到对应密码（查不到 → `Invalid user`），用同一个 `nonce` 重算 `HMAC(nonce, pwd)`，与对端送来的 `open_syn.hmac` 比对，不等则 `Invalid password`。通过则返回 `username`。

**usrpwd：字典文件加载格式**：

[io/zenoh-transport/src/unicast/establishment/ext/auth/usrpwd.rs:69-122](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/establishment/ext/auth/usrpwd.rs#L69-L122) —— `from_config` 读 `dictionary_file`，按行解析 `user:password`（以第一个 `:` 切分；空用户名或空密码都判错）。这就是配置项 `transport/auth/usrpwd/dictionary_file` 指向文件的格式约定。

**pubkey：发起方 InitSyn 送出公钥**：

[io/zenoh-transport/src/unicast/establishment/ext/auth/pubkey.rs:371-390](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/establishment/ext/auth/pubkey.rs#L371-L390) —— `InitSyn { alice_pubkey: 本节点公钥 }`。

**pubkey：发起方收 InitAck（解挑战、再加密）**：

[io/zenoh-transport/src/unicast/establishment/ext/auth/pubkey.rs:394-436](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/establishment/ext/auth/pubkey.rs#L394-L436) —— 先查白名单是否允许对端公钥（[L414-L418](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/establishment/ext/auth/pubkey.rs#L414-L418)）；用自己私钥 `decrypt_blinded` 解出应答方的挑战 nonce；再用对方公钥加密同一个 nonce 存入 `state.nonce`，留待 OpenSyn 回送。

**pubkey：应答方收 OpenSyn 校验**：

[io/zenoh-transport/src/unicast/establishment/ext/auth/pubkey.rs:607-644](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/establishment/ext/auth/pubkey.rs#L607-L644) —— 用自己私钥解出对端回送的 nonce，与当初发的 `state.challenge` 比对（[L638-L641](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/establishment/ext/auth/pubkey.rs#L638-L641)），不等则 `Invalid nonce`。能正确回送等于证明发起方持有对应私钥。

**认证结果汇总 `TransportAuthId`**：

[io/zenoh-transport/src/unicast/authentication.rs:20-67](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/authentication.rs#L20-L67) —— 汇总 `username`（usrpwd 验出的用户名）、`zid`、`link_auth_ids`（链路层如 TLS 客户端证书的 CN）。`set_username`（[L36-L50](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/authentication.rs#L36-L50)）从 `UsrPwdId` 提取用户名。最终由 `TransportUnicastUniversal::get_auth_ids` 聚合（见 4.3.3）。

#### 4.2.4 代码实践（可运行型：启用 auth_usrpwd 观察握手变化）

**实践目标**：让一条 unicast 连接强制走用户名密码认证，观察「凭证正确则建连、错误则被拒」。

**操作步骤**：

1. 准备一个字典文件 `users.txt`，内容为（每行 `user:password`）：
   ```text
   alice:secret123
   ```
2. 终端 A 启动 router（应答方），加载字典：
   ```bash
   RUST_LOG=zenoh_transport=debug cargo run -p zenohd -- \
     --cfg 'transport/auth/usrpwd/dictionary_file:"users.txt"'
   ```
   （配置键名以 [DEFAULT_CONFIG.json5](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/DEFAULT_CONFIG.json5#L766-L771) 为准。`auth_usrpwd` 确属 `zenoh` 的默认 feature——见 [zenoh/Cargo.toml:34-36](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/Cargo.toml#L34-L36) 的 `default = [..., "auth_pubkey", "auth_usrpwd", ...]`，而 zenohd 又启用 `zenoh/default`，故无需额外 feature 开关即可使用。）
3. 终端 B 以 client 连接，**提供正确凭证**。示例的 `CommonArgs` 原生支持 `--cfg 'KEY:VALUE'` 透传（[examples/src/lib.rs:19-21](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/examples/src/lib.rs#L19-L21) 处定义，`VALUE` 按 JSON5 解析）：
   ```bash
   cargo run --example z_sub -- -e tcp/127.0.0.1:7447 -m client \
     --cfg 'transport/auth/usrpwd/user:"alice"' \
     --cfg 'transport/auth/usrpwd/password:"secret123"'
   ```
4. 再开终端 C，**提供错误密码**（如把上面 `password` 改成 `"wrong"`），重复步骤 3。

**需要观察的现象**：

- 步骤 3（正确凭证）：连接建立成功，A 的 debug 日志可见正常握手完成；可正常收发数据。
- 步骤 4（错误密码）：B 端应在握手阶段被 A 拒绝，A 端日志出现 `Invalid password`（对应 [usrpwd.rs:423-425](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/establishment/ext/auth/usrpwd.rs#L423-L425)），B 收到 `Close` 后建连失败。

**预期结果**：验证「密码不上线、靠 HMAC(nonce, password) 比对」——改字典里 alice 的密码，原凭证立刻失效。**待本地验证**：确切的日志字符串（如 `Invalid password`、`Invalid user`）与拒绝时携带的 `Close reason` 可能因版本略有差异，请以实际 `RUST_LOG=zenoh_transport=debug` 输出为准。

#### 4.2.5 小练习与答案

**练习 1**：usrpwd 认证里，密码是否会以明文出现在网络上？为什么抓到一次 `HMAC(nonce, password)` 不能用来重放？

**参考答案**：不会明文上线；上线的是 `HMAC(key=nonce, msg=password)`（[usrpwd.rs:325-327](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/establishment/ext/auth/usrpwd.rs#L325-L327)）。`nonce` 是应答方每次握手随机生成的 `u64`（[usrpwd.rs:386-393](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/establishment/ext/auth/usrpwd.rs#L386-L393)），故每次握手 HMAC 值都不同；抓到的旧值换一次握手（nonce 变了）就比对不上，无法重放。

**练习 2**：auth_usrpwd 与 auth_pubkey 能否在同一节点同时启用？握手时它们如何区分各自的扩展载荷？

**参考答案**：可以同时启用——`Auth` 聚合器把两者都放进同一个 `ext_auth` 扩展内部的 `Vec<ZExtUnknown>`，用 `id::PUBKEY(0x1)` / `id::USRPWD(0x2)` 区分（[ext/auth/mod.rs:44-49](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/establishment/ext/auth/mod.rs#L44-L49)）；收发时按 id 分派到对应子 FSM。任一启用的机制校验失败，握手即失败。

---

### 4.3 transport_unicast_inner：握手之后的状态机与收发骨架

#### 4.3.1 概念说明

握手跑完，协商结果被装进 `TransportConfigUnicast`，交给 `TransportManager::init_transport_unicast` 注册进传输表，并据此创建真正的传输实现对象。这个对象实现一个内部 trait `TransportUnicastTrait`，并持有一个三态状态机 `TransportStatus`。

`TransportStatus` 只有三态，非常简单：

```text
   Uninitialized ──add_link(sync)──→  Alive
        │                               │
        │                          close / delete
        └───────────────────────────────┴──→ Closed（终态，不可恢复）
```

- `Uninitialized`：传输刚建好，还没收到第一条链路的对端初始序号。
- `Alive`：至少一条链路已 `sync` 成功，可正常 `schedule` 收发。
- `Closed`：终态。任何后续操作（再 add_link、schedule）都会返回错误。

`add_link` 是握手到运行期的「合龙」动作：它把握手产出的 `LinkUnicastWithOpenAck` 拆成「真正的收发链路 + 延迟 OpenAck + 两个启动闭包」。两个闭包（`start_tx` / `start_rx`）故意做成**闭包**而非立即执行——因为它们的启动顺序很讲究：必须先 `send_open_ack`（完成协议握手）、再 `start_tx`、再通知上层 `new_link`、最后 `start_rx`，并且整个过程要持有 `status` 锁以避免与并发 close 竞争。

`schedule(msg)` 则是出站终点：上层 `Mux`（见 u7-l2）把网络消息投到这里，由传输内部按优先级送上网（批处理/管道细节留待 u9-l4）。

#### 4.3.2 核心流程

**从握手到运行的合龙**（`init_transport_unicast`，分新建 / 已存在两种）：

```text
init_transport_unicast(config, link_with_ack, other_initial_sn, other_lease)
→ 锁 transports 表
→ 若该 zid 已存在：init_existing_transport_unicast
    → 校验 config 与已有传输一致（否则 INVALID）
    → transport.add_link(...) → 得到 (start_tx, start_rx, ack, guard)
    → ack.send_open_ack()       // ★ 此时才真正发出 OpenAck
    → start_tx()  → notify_new_link → start_rx() → drop(guard)
→ 否则：init_new_transport_unicast
    → 校验 config.zid != self.zid()（禁止连自己，CONNECTION_TO_SELF）
    → 校验 len() < max_sessions（否则 INVALID）
    → 选实现：is_lowlatency ? Lowlatency : Universal
    → impl.add_link(...) → ack.send_open_ack() → 插入表
    → notify_new_transport(handler.new_unicast) → set_callback
    → notify_new_link → start_tx()/start_rx()
```

**add_link 内部**（以 Universal 实现为例）：

```text
add_link(link_with_ack, other_initial_sn, other_lease)
→ sync(other_initial_sn)：Uninitialized→Alive，把 RX 优先级队列的初始序号同步成对端值
→ 检查 inbound 链路数 < max_links（否则 MAX_LINKS）
→ unpack：拆出主链路 + MaybeOpenAck + (可选)associated_link
→ 包装成 TransportLinkUnicastUniversal（含 TransmissionPipelineProducer）
→ push 进 TransportLinks
→ 构造 start_tx 闭包：算 keep_alive = lease/keep_alive，link.start_tx(...)
→ 构造 start_rx 闭包：link.start_rx(..., other_lease)
→ 返回 (start_tx, start_rx, ack, status_guard)
```

注意 `add_link` 返回的 `status_guard`（一把 `AsyncMutex` 锁）会被调用方一直持有到 `start_rx()` 之后才 `drop`——这保证「合龙」期间不会有并发 `close` 把状态改成 `Closed`。

#### 4.3.3 源码精读

**TransportStatus 三态枚举**：

[io/zenoh-transport/src/unicast/transport_unicast_inner.rs:56-60](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/transport_unicast_inner.rs#L56-L60) —— `Uninitialized / Alive / Closed`。

**TransportUnicastTrait 契约**——传输实现必须提供的能力：

[io/zenoh-transport/src/unicast/transport_unicast_inner.rs:65-114](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/transport_unicast_inner.rs#L65-L114) —— 分三组：访问器（`get_zid / get_whatami / get_auth_ids / is_qos / region_name / get_bound / get_config / stats`）、链路（`add_link`，返回 `AddLinkResult`）、TX（`schedule`，返回 `ZResult<bool>` 表示是否真发出）、终止（`close`）。`AddLinkResult` 类型（[L45-L53](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/transport_unicast_inner.rs#L45-L53)）正是「两个闭包 + MaybeOpenAck + status 锁」。

**sync：状态机迁移 + RX 序号同步**：

[io/zenoh-transport/src/unicast/universal/transport.rs:226-253](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/universal/transport.rs#L226-L253) —— `Uninitialized` 时把所有 `priority_rx` 的初始序号同步为 `initial_sn_rx` 并迁到 `Alive`；`Alive` 直接放行；`Closed` 返回 `Err`。

**add_link：合龙的全过程**：

[io/zenoh-transport/src/unicast/universal/transport.rs:261-347](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/universal/transport.rs#L261-L347) —— 先 `sync`；再查 inbound 链路数上限（multilink 关时为 1，[L284-L306](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/universal/transport.rs#L284-L306)）；`unpack` 拆链路；构造 `start_tx` 闭包时计算 `keep_alive = lease / keep_alive as u32`（[L329-L331](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/universal/transport.rs#L329-L331)，呼应 u9-l2 的保活公式）；最后返回 `(start_tx, start_rx, ack, status_guard)`。

**TransportUnicastUniversal::make：构造收发骨架**：

[io/zenoh-transport/src/unicast/universal/transport.rs:104-149](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/universal/transport.rs#L104-L149) —— 按 `is_qos` 决定 TX 优先级队列数（`Priority::NUM` 或 1），RX 固定建 `Priority::NUM` 条；TX 各队列初始序号同步为 `config.tx_initial_sn`（即握手算出的 `compute_sn`）。

**init_transport_unicast：分派新建/已存在**：

[io/zenoh-transport/src/unicast/manager.rs:779-829](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/manager.rs#L779-L829) —— 锁表后按 `transports.get(&config.zid)` 是否命中分派；失败时区分 `InitTransportError::Link`（关链路）与 `::Transport`（关整个传输）做清理。

**init_new_transport_unicast：三项校验 + 选实现 + 合龙**：

[io/zenoh-transport/src/unicast/manager.rs:606-634](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/manager.rs#L606-L634) —— 禁止连自己（`CONNECTION_TO_SELF`）与会话上限（`INVALID`）两项校验。

[io/zenoh-transport/src/unicast/manager.rs:672-743](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/manager.rs#L672-L743) —— 按 `is_lowlatency` 选 `TransportUnicastLowlatency` 或 `TransportUnicastUniversal`；`add_link` → `send_open_ack`（[L721](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/manager.rs#L721)）→ 插表 → `start_tx()` → `notify_new_transport` → `notify_new_link` → `start_rx()` → `drop(add_link_guard)`。这个顺序就是「协议握手完成 → 上线发送 → 通知上层 → 上线接收」的最优排列。

**init_existing_transport_unicast：multilink 加链路**：

[io/zenoh-transport/src/unicast/manager.rs:487-541](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/manager.rs#L487-L541) —— 先校验新链路协商出的 `config` 与已有传输**完全一致**（否则 `INVALID`，[L498-L513](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/manager.rs#L498-L513)），再把链路并入同一传输。这就是「同一对 zid 之间多条物理链路共享一条传输」的落点。

**主动建连与被动接入的握手触发点**（把本模块与 u9-l2 串起来）：

[io/zenoh-transport/src/unicast/manager.rs:844-890](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/manager.rs#L844-L890) —— `open_transport_unicast_inner` 把 `new_link` + `open_link` 整个握手包在 `tokio::time::timeout(open_timeout, ...)` 里（[L880-L889](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/manager.rs#L880-L889)）。

[io/zenoh-transport/src/unicast/manager.rs:926-961](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/manager.rs#L926-L961) —— `handle_new_link_unicast` 在 `ZRuntime::Acceptor` 上 spawn，用 `accept_timeout` 包住 `accept_link`（[L947-L958](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/manager.rs#L947-L958)）。

**get_auth_ids：把认证结果交给上层**：

[io/zenoh-transport/src/unicast/universal/transport.rs:434-446](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/universal/transport.rs#L434-L446) —— 汇总链路层 auth id 与 usrpwd 用户名（`set_username(&self.config.auth_id)`），正是 4.2 认证结果的出口。

#### 4.3.4 代码实践（源码阅读型：追踪 OpenAck 延迟发送时序）

**实践目标**：理解「OpenAck 为什么不能在 `send_open_ack` 这一步立刻发出」，看清 `add_link` 返回值与启动闭包的精巧顺序。

**操作步骤**：

1. 阅读 [accept.rs 的 `send_open_ack`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/establishment/accept.rs#L616-L731)，确认它**只构造 `OpenAck` 放进 `SendOpenAckOut`，没有任何 `link.send`**（注释在 [L722-L727](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/establishment/accept.rs#L722-L727)）。
2. 跟到 [accept.rs:928](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/establishment/accept.rs#L928)，看 OpenAck 被装进 `LinkUnicastWithOpenAck::new(a_link, Some(oack), ...)`。
3. 跟到 [manager.rs 的 `init_new_transport_unicast`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/manager.rs#L698-L743)，看 `add_link(...)` 返回 `ack` 后，第 [721 行](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/manager.rs#L721) 才 `ack.send_open_ack().await`——此刻 `add_link` 内的 `MAX_LINKS` / `MAX_SESSIONS` 校验已通过。
4. 对照 [link.rs 的 `MaybeOpenAck::send_open_ack`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/link.rs#L295-L314)，确认它只在 `Some(msg)` 时发（发起方一侧是 `None`）。

**需要观察的现象**：OpenAck 的「构造」与「发送」被刻意拆在两个时间点：构造在 `AcceptFsm::send_open_ack`（握手 FSM 内），发送在 `init_transport_unicast`（合龙阶段，校验通过后）。

**预期结果**：写出一句话结论——「延迟发送 OpenAck 是为了避免在已超 `MAX_LINKS`/`MAX_SESSIONS` 时仍向对端确认建连，从而保持『只有真正分配资源的连接才回 OpenAck』的不变式」。

#### 4.3.5 小练习与答案

**练习 1**：`add_link` 为什么要返回两个「启动闭包」`start_tx` / `start_rx`，而不是在 `add_link` 内部直接启动 TX/RX 任务？

**参考答案**：因为启动顺序涉及协议正确性与并发安全。`init_new_transport_unicast` 必须按 `add_link → send_open_ack → start_tx → notify_new_transport/new_link → start_rx → drop(guard)` 的固定顺序执行（[manager.rs:698-743](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/manager.rs#L698-L743)）：必须先发 OpenAck 让对端确认建连、再开始发数据；必须先注册回调（`set_callback`）再 `start_rx`，否则入站消息到达时还没有回调可投递。把这些时机交给 `add_link` 内部就丧失了编排灵活性，故做成闭包交由上层在正确时机调用。

**练习 2**：`init_existing_transport_unicast` 为什么要求新链路的 `config` 与已有传输**完全相等**（`*existing_config != config` 即拒）？

**参考答案**：multilink 把同一对 zid 的多条物理链路并入同一条逻辑传输，它们**共享同一套序列号空间、同一套优先级队列**。若两条链路协商出不同的 `sn_resolution` / `is_qos` / `initial_sn`，就会破坏序列号单调性与分道一致性。因此 [manager.rs:498-513](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/manager.rs#L498-L513) 强制要求配置一致；而 `compute_sn` 的确定性（4.1）正是为了让多次连接尝试天然算出相同的 `initial_sn`，使这一校验能通过。

---

### 4.4 multicast transport 的收发：没有握手，靠 Join + lease

#### 4.4.1 概念说明

与 unicast 的「四次握手 + Cookie + 认证 + 状态机」截然不同，**multicast 传输没有 Init/Open 握手**。组播是「加入一个多播组即同时收发」的无连接模型（见 u9-l2），成员关系靠周期性广播的 **Join** 消息维持。

具体差异：

| 维度 | unicast | multicast |
| --- | --- | --- |
| 建连 | InitSyn/InitAck/OpenSyn/OpenAck 四次握手 | 无握手，发 Join 即「上线」 |
| 状态恢复 | Cookie 加密暂存 | 无（无状态可恢复） |
| 认证 | auth_usrpwd / auth_pubkey | 无 |
| 成员发现 | 传输表按 zid，建连时注册 | 每收到一个新 zid 的 Join 即 `new_peer` |
| 心跳 | KeepAlive，间隔 lease/keep_alive | Join，周期 `join_interval` |
| 失联判据 | lease 内无任何入站 → 传输 `EXPIRED` | 某成员 lease 内无 Join → `del_peer(EXPIRED)` |

`TransportMulticastInner` 维护一个 `HashMap<Locator, TransportMulticastPeer>`（按多播组 Locator 索引，组内再按来源 Locator 区分各 peer）。每个 peer 各有一个基于 `lease` 的「看门狗」任务：每过一个 lease 周期检查一次 `is_active` 标志——该标志在收到该 peer 的 Join 时被置真、检查时被换回假；若检查时仍为假（说明整个 lease 内没收到该 peer 的 Join），就 `del_peer` 并以 `EXPIRED` 通知上层。

#### 4.4.2 核心流程

```text
加入组（open_transport_multicast / add_listener_multicast，二者等同）
→ TransportMulticastInner::make：建 priority_tx、空 peers 表
→ start_tx：周期性（join_interval）向组里发 Join（带本节点 zid/whatami/lease/initial_sn）
→ start_rx：循环收数据报，每收到一个 Join：
    → 若来源 Locator 不在 peers 表 → new_peer：注册 + spawn lease 看门狗
    → 否则：置该 peer 的 is_active = true（续命）

peer 看门狗（每 lease 周期一次）：
→ if !is_active.swap(false) {   // 上个周期没收到 Join
     del_peer(locator, EXPIRED)  // 判定该 peer 已离组
   }
```

#### 4.4.3 源码精读

**TransportMulticastInner::make：构造组播传输**：

[io/zenoh-transport/src/multicast/transport.rs:104-151](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/multicast/transport.rs#L104-L151) —— 建 `priority_tx`（按 `config.initial_sns.len()` 决定队列数，并校验「长度为 1 或 Priority::NUM」否则 `Invalid QoS configuration`，[L112-L120](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/multicast/transport.rs#L112-L120)）；`peers` 初始化为空 `HashMap`；保存组 Locator。**没有** prng/cipher/authenticator——组播不需要握手与认证。

**new_peer：收到新 Join 注册成员 + 起 lease 看门狗**：

[io/zenoh-transport/src/multicast/transport.rs:336-435](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/multicast/transport.rs#L336-L435) —— 从 `Join` 消息构造 `TransportPeer` 并回调 `callback.new_peer` 拿到入站 handler；按 `join.ext_qos` 或 `join.next_sn` 建 `priority_rx` 并 `sync` 初始序号（[L357-L372](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/multicast/transport.rs#L357-L372)）；然后 spawn 看门狗任务（[L394-L411](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/multicast/transport.rs#L394-L411)）。注意 region_name 固定为 `None`（[L348](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/multicast/transport.rs#L348)，组播暂不支持 region）。

**lease 看门狗：失联即剔除**：

[io/zenoh-transport/src/multicast/transport.rs:394-408](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/multicast/transport.rs#L394-L408) —— 一个 `tokio::time::interval_at(now + lease, lease)`，每次 tick 做 `c_is_active.swap(false, AcqRel)`：若返回假（意味着上个 lease 周期内没人把它置真，即没收到该 peer 的 Join），就 `break` 并 `del_peer(..., close::reason::EXPIRED)`；同时 `select!` 了 cancellation token，传输关闭时能干净退出。这正是组播版的「lease 死链判据」。

**del_peer：移除成员并通知上层**：

[io/zenoh-transport/src/multicast/transport.rs:437-455](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/multicast/transport.rs#L437-L455) —— 从 `peers` 移除该 Locator 对应的 peer，`cancel` 其 token，并回调 `handler.closed()` 通知路由层该 peer 已离线。

**TX/RX 启动**：

[io/zenoh-transport/src/multicast/transport.rs:240-331](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/multicast/transport.rs#L240-L331) —— `start_tx` 把 `version/zid/whatami/lease/join_interval/sn_resolution/batch_size` 传给链路的 TX（TX 内部按 `join_interval` 周期发 Join）；`start_rx` 启动接收循环。对比 unicast 的 `add_link` 返回闭包、择机启动，multicast 这里是直接 `start_tx`/`start_rx`——因为没有「握手完成」这一时刻需要对齐。

**TransportMulticastPeer 的 active 标志与 qos 判定**：

[io/zenoh-transport/src/multicast/transport.rs:51-76](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/multicast/transport.rs#L51-L76) —— `is_active: Arc<AtomicBool>` 是看门狗的续命标志；`set_active` 在收到该 peer 的 Join 时调用；`is_qos` 按 `priority_rx.len() == Priority::NUM` 判定。

#### 4.4.4 代码实践（源码阅读型：对比 unicast 与 multicast 的「成员加入」路径）

**实践目标**：用源码印证「组播无握手、靠 Join 续命」，并理解看门狗的 lease 数学。

**操作步骤**：

1. 阅读 [new_peer](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/multicast/transport.rs#L336-L435)，确认它**不涉及任何 Init/Open/Cookie**，而是直接从一条 `Join` 消息建出 peer。
2. 阅读 [L394-L408](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/multicast/transport.rs#L394-L408) 的看门狗，写出 lease 与「最长容忍的 Join 间隔」的关系：若 `join_interval > lease`，看门狗会怎样？
3. 对比 unicast 的握手路径（[open.rs `open_link`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/establishment/open.rs#L620-L846)），列出 multicast 省掉了哪些组件。

**需要观察的现象**：multicast 的 peer 加入是「被动收到 Join 才注册」，而 unicast 是「主动发起四次握手才建立」；multicast 完全没有认证、Cookie、`TransportStatus` 状态机。

**预期结果**：写出结论——「multicast 以 `Join` 既是上线宣告又是续命心跳，用 `lease` 看门狗做软成员管理；若 `join_interval > lease`，成员会在收到下一次 Join 前就被误判离线并 `del_peer`，故部署时须保证 `join_interval < lease`（默认 2500ms < 10000ms，安全）。」

#### 4.4.5 小练习与答案

**练习 1**：为什么 multicast 不实现 auth_usrpwd / auth_pubkey？

**参考答案**：组播是一对多的无连接模型，一条 Join 发给整个组、由所有成员接收；既没有明确的「点对点应答方」来发挑战 nonce，也不适合在组里明文传凭证或为每个成员维护挑战状态。组播通常部署在受信局域网内用于发现与轻量广播（如 scouting），安全边界由网络本身（隔离的组播域）承担，故协议层未内置认证。

**练习 2**：multicast 的看门狗用 `is_active.swap(false, AcqRel)`，这个「换回假」的语义是什么？为什么用 `swap` 而不是先 `load` 再 `store`？

**参考答案**：`is_active` 在收到该 peer 的 Join 时被 `set_active()` 置真（[transport.rs:69-71](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/multicast/transport.rs#L69-L71)）。看门狗每个 lease 周期做一次「读旧值并清零」的原子操作：若旧值为真（说明本周期内收到过 Join，已续命），就继续；若旧值为假（本周期内没收到 Join），就判定失联。`swap` 是原子的「读改写」，避免了 `load` 与 `store` 之间的竞态——否则在 `load`（看到真）和 `store(false)` 之间到达的 Join 可能被清零丢失，造成误判。

---

## 5. 综合实践

**任务**：画出一条 unicast 连接从「`open_transport_unicast` 调用」到「`schedule` 能发数据」的完整时序，标注每一步经过的关键函数、交换的消息、状态机迁移，并附一份 multicast 的对照时序。

**操作步骤**：

1. **unicast 时序**。按以下顺序在源码里定位并连成一条链：
   - 入口：[manager.rs `open_transport_unicast_inner`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/manager.rs#L844-L890)（`open_timeout` 包裹）。
   - 握手四拍：[open.rs `open_link`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/establishment/open.rs#L620-L846) 内的 `send_init_syn / recv_init_ack / send_open_syn / recv_open_ack`；应答方对应 [accept.rs `accept_link`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/establishment/accept.rs#L734-L951)。
   - Cookie 流转：生成（[accept.rs:381-419](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/establishment/accept.rs#L381-L419)）→ 回传 → 解密恢复（[accept.rs:504-550](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/establishment/accept.rs#L504-L550)）。
   - 合龙：[manager.rs `init_new_transport_unicast`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/manager.rs#L586-L777) 的 `add_link → send_open_ack → start_tx → notify → start_rx`。
   - 状态机：[transport.rs `sync`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/universal/transport.rs#L226-L253)（`Uninitialized → Alive`）。
   - 出站：[transport.rs `schedule`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/universal/transport.rs#L451-L453)。
2. **（可选）启用认证**。在时序里插一层：若配置了 usrpwd，握手扩展 `ext_auth` 在 InitSyn 标记、InitAck 下发 nonce、OpenSyn 送 HMAC、OpenAck 确认（[usrpwd.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/establishment/ext/auth/usrpwd.rs#L272-L438)）；认证结果经 [get_auth_ids](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/universal/transport.rs#L434-L446) 出口。
3. **multicast 对照时序**。用 [transport.rs `new_peer`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/multicast/transport.rs#L336-L435) 与看门狗（[L394-L408](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/multicast/transport.rs#L394-L408)）画出「发 Join → 被对端 new_peer → 周期续命 → 失联 EXPIRED」的简化链。
4. **填对比表**：

| 维度 | unicast | multicast |
| --- | --- | --- |
| 建连消息 | InitSyn/InitAck/OpenSyn/OpenAck | Join（周期性） |
| 状态暂存 | 加密 Cookie | 无 |
| 认证 | usrpwd(HMAC) / pubkey(RSA) | 无 |
| 内部状态机 | `TransportStatus`（Uninitialized→Alive→Closed） | peer 级 `is_active` 看门狗 |
| 初始序号 | `compute_sn(zid1,zid2)` 确定性 | Join 携带 `next_sn` / `ext_qos` |
| OpenAck 时序 | `add_link` 通过校验后延迟发送 | 无 OpenAck |
| 失联判定 | lease 内无入站 → 传输 EXPIRED | lease 内无 Join → del_peer(EXPIRED) |

**预期结果**：产出一份「unicast 全生命周期时序图（文字版）+ multicast 对照表」。完成后回到 4.2.4 跑一次 usrpwd 实验，把图里的「OpenSyn 送 HMAC」与实测日志对上，即算贯通本讲。

## 6. 本讲小结

- unicast 建连是**两阶段四次消息** FSM：`OpenFsm`（`send_init_syn/recv_init_ack/send_open_syn/recv_open_ack`）与 `AcceptFsm`（对称四步）由 [establishment/mod.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/establishment/mod.rs#L35-L99) 定义；Init 阶段协商 `version/whatami/zid/resolution/batch_size` 并探测扩展，Open 阶段提交 `lease/initial_sn`。
- **Cookie 机制**让应答方在 Init 阶段无状态：协商结果用进程级 `BlockCipher` 加密成 Cookie 交发起方保管，Open 阶段回传、解密、用 `nonce` 校验后恢复状态——这是抗资源耗尽/抗放大的关键。
- 初始序列号由 `compute_sn` 从双方 zid 经 Shake128 **确定性**算出，保证 multilink 多次连接尝试天然一致。
- 认证以扩展 `ext_auth`(id 0x3) 挂载：**auth_usrpwd** 用 `HMAC(nonce, password)` 挑战应答（密码不上线），**auth_pubkey** 用 RSA 双向互验公钥；二者由 `Auth` 聚合、可同开，结果（用户名等）经 `TransportAuthId` / `get_auth_ids` 出口。
- 握手产物装进 `TransportConfigUnicast`，由 `init_transport_unicast` 注册进表并创建传输实现（`Universal` 或 `Lowlatency`）；`add_link` 合龙时 `TransportStatus` 从 `Uninitialized` 迁到 `Alive`，并返回 `start_tx/start_rx` 闭包与延迟的 OpenAck。
- **OpenAck 延迟发送**：应答方在 `send_open_ack` 只构造不发送，待 `add_link` 通过 `MAX_LINKS`/`MAX_SESSIONS` 校验后才由 `MaybeOpenAck::send_open_ack` 真正发出，保证「只有真正分配资源的连接才回 OpenAck」。
- **multicast 完全不同**：无握手、无 Cookie、无认证、无 `TransportStatus`；成员靠周期性 `Join`（`join_interval`）宣告与续命，每个 peer 一个 `lease` 看门狗，超 lease 未收到 Join 即 `del_peer(EXPIRED)`。

## 7. 下一步学习建议

- **u9-l4 批处理、分片与优先级管道**：本讲的 `schedule` 只说「消息投到传输」，但消息如何被 `BatchConfig` 打包成帧、按 `Priority` 分道进入 `TransmissionPipeline`、大消息如何分片并在对端 `defragmentation` 按 `TransportSn` 重组，是下一讲的专题。
- **u10-l1 / u10-l2 协议消息与 Zenoh080 线编码**：本讲频繁引用 `InitSyn/OpenSyn/Cookie` 的字段，下一阶段会讲这些消息如何被 `Zenoh080` codec 序列化成字节（header + zint + body），读完会更理解 Cookie 加密前后的形态。
- **回到 u7-l2 / u7-l3**：带着本讲认识重读 `Mux` 的 `route_data → TransportUnicast::schedule`，以及 orchestrator 的 `connect_peer → open_transport_unicast`（含 `expected_zid` 校验），你会看清「Runtime 建连 → 传输握手 → 路由层挂载 Face」的完整通电链路。
- **配置与安全实战**：结合 [DEFAULT_CONFIG.json5 的 auth 段](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/DEFAULT_CONFIG.json5#L764-L780)，尝试为两个 zenohd 节点配置互信的 usrpwd 字典或 pubkey，用 `RUST_LOG=zenoh_transport=debug` 观察握手扩展的交换，验证「凭证不符则建连被拒」。
