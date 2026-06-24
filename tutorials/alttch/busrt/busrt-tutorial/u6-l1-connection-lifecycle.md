# 连接生命周期：handle_peer / handle_reader / handle_writer / handle_pinger

## 1. 本讲目标

本讲深入 `src/broker.rs`，拆解**一个外部客户端连接从建立到断开的完整生命周期**。学完后你应该能够：

- 说清 `handle_peer` 如何完成握手、注册客户端，并编排三个并发任务（reader / writer / pinger）。
- 看懂 `handle_reader` 如何逐帧解析入站字节、按 `FrameOp` 分发到「订阅控制」或「三种通信模式」，以及在 `QoS::needs_ack()` 时回送 ACK。
- 理解 `handle_writer` 如何把内存里的 `FrameData` 序列化成线上 6 字节头 + 体的字节流。
- 理解 `handle_pinger` 这个「心跳 + 背压守护」循环的作用。
- 把一条 `publish` 消息从「发送方 socket → handle_reader → publish! 宏 → 订阅者通道 → 订阅者 handle_writer → 订阅者 socket」的完整数据流串起来。

本讲是整个 broker 源码里最核心、也最长的一条调用链，建议配合前两讲（[u3-l2 BrokerDb](u3-l2-broker-db.md) 的三张路由表、[u2-l3 线上协议](u2-l3-wire-protocol.md) 的帧字节布局）一起阅读。

## 2. 前置知识

阅读本讲前，请确认你已理解以下概念（在前序讲义中已建立）：

- **三张路由表**：`BrokerDb` 持有 `clients`（点对点，精确全名）、`broadcasts`（广播，`.` 分层 + `?`/`*`）、`subscriptions`（发布订阅，`/` 分层 + `+`/`#`）。
- **客户端对象**：一个 `BusRtClient` 持有入站有界通道 `tx`（`async_channel::Sender<Frame>`，默认容量 8192）、收发统计原子计数器、排除列表等。
- **入站帧 vs 出站帧头长度不同**：客户端→代理是 **9 字节头** `op_id(4)|flags(1)|len(4)`；代理→客户端是 **6 字节头** `kind(1)|len(4)|realtime(1)`。
- **flags 字节**：低 6 位是操作码 `op`，高 2 位是 `qos`，即 `flags = op | (qos << 6)`。
- **QoS 两个正交位**：低位 `needs_ack()`（要不要 ACK），高位 `is_realtime()`（要不要立刻刷新出站）。
- **零拷贝扇出**：分发时用 `Arc::clone` 复用 `Frame = Arc<FrameData>`，不复制字节。

此外，请记住一个贯穿全讲的关键事实：**一个外部客户端连接对应四个并发任务**——本讲的四个 `handle_*` 函数里，`handle_peer` 是「总指挥」，其余三个是它 spawn 出来并发跑的「工人」，三者中任何一个结束都会终结整个连接。

## 3. 本讲源码地图

本讲几乎全部内容集中在 `src/broker.rs`，只引用少量 `src/lib.rs` 中的协议常量与类型定义：

| 文件 | 作用 |
|------|------|
| [src/broker.rs](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs) | 四个 `handle_*` 函数、分发宏、`BusRtClient`、`handle_connection` 调用入口 |
| [src/lib.rs](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs) | `FrameOp` / `FrameKind` / `QoS` / `FrameData` 与协议常量 |
| [src/comm.rs](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/comm.rs) | `TtlBufWriter` 与 `Flush` 三态枚举（出站缓冲，详见 [u4-l3](u4-l3-ttl-buf-writer.md)） |
| examples/client_listener.rs | 实践用：订阅 `#` 并打印收到的每一帧 |
| examples/client_sender.rs | 实践用：发布主题、点对点、广播 |

## 4. 核心概念与源码讲解

### 4.1 handle_peer：握手与三个并发任务的编排

#### 4.1.1 概念说明

`handle_peer` 是「一个连接」的入口函数。每当 `spawn_server!` 宏 `accept` 到一个新连接，它就通过 `handle_connection` 把读写两个半部分（`reader` / `writer`）打包进 `PeerHandlerParams`，然后 `tokio::spawn` 一个任务跑 `handle_peer`。

换句话说：**一个 `handle_peer` 调用 = 一个客户端连接的整个生命周期**。它做三件事：

1. 完成协议握手（魔数 + 版本号）。
2. 读取并注册客户端名，做 AAA 连接级鉴权。
3. spawn 出 reader / writer / pinger 三个并发任务，用 `tokio::select!` 等其中任意一个结束，然后注销客户端。

#### 4.1.2 核心流程

```
handle_connection (accept 后调用)
        │  打包 PeerHandlerParams，spawn
        ▼
handle_peer
  ├─ ① 握手：发送 GREETINGS(0xEB)+version → 读回客户端 3 字节 → 校验 → 回 RESPONSE_OK
  ├─ ② 读客户端名（u16 长度 + 名字），校验非空且不以 '.' 开头
  ├─ ③ 提取 primary_name（按 SECONDARY_SEP "%%" 切分）
  ├─ ④ AAA 连接级鉴权（connect_allowed）
  ├─ ⑤ BusRtClient::new → db.register_client → 回 RESPONSE_OK
  ├─ ⑥ 构造三个 future：handle_pinger / handle_reader / handle_writer
  └─ ⑦ tokio::select!：谁先结束谁终结连接，统一 finish_peer!（unregister_client）
```

握手字节布局（代理侧）：

- 代理先发 3 字节：`GREETINGS[0]`(0xEB) + `PROTOCOL_VERSION.to_le_bytes()`(2 字节小端)。
- 客户端回 3 字节：同样 `0xEB` + 版本号，代理校验魔数与版本。
- 代理回 1 字节 `RESPONSE_OK`(0x01)。
- 客户端发 2 字节 `u16` 名字长度 + 名字字节。

#### 4.1.3 源码精读

函数签名与泛型约束（`R` 是读半部、`W` 是写半部，都要求 `Unpin + Send + Sync + 'static` 以便跨任务移动）：

[src/broker.rs:1733-1737](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1733-L1737) — 函数入口，从 `PeerHandlerParams` 解出 timeout / reader / writer / queue_size / db。

握手的前半段——代理主动发送问候并校验客户端回送的魔数与版本：

[src/broker.rs:1748-1761](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1748-L1761) — 发送 `GREETINGS + PROTOCOL_VERSION`；读 3 字节；魔数不符或版本不符则回 `ERR_NOT_SUPPORTED`(0x75) 并报错；通过则回 `RESPONSE_OK`。

注意这里的 `write_and_flush!` 宏（[src/broker.rs:1743-1747](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1743-L1747)）用 `Flush::Instant` 立即刷新，保证握手这一小段控制流量立刻送达、不被合并缓冲。

读取客户端名并校验（不能为空、不能以 `.` 开头——`.` 前缀是代理保留名，如 `.broker`）：

[src/broker.rs:1762-1774](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1762-L1774) — 读 `u16` 长度 + 名字；非法名回 `ERR_DATA`(0x72)；用 `SECONDARY_SEP` 提取主名 `client_primary_name`。

AAA 连接级鉴权——注意只在有 IP（TCP/WebSocket，`ClientIp::Addr`）时才做主机白名单校验，Unix socket（`ClientIp::No`）跳过：

[src/broker.rs:1775-1797](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1775-L1797) — 查 `aaa_map`；客户端不在映射里或 `connect_allowed(addr)` 返回 `false`，则回 `ERR_ACCESS`(0x79) 拒绝。

构造客户端对象并注册（注册失败会把错误码写回客户端再返回）：

[src/broker.rs:1798-1815](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1798-L1815) — `BusRtClient::new` 创建客户端 + 入站通道 `rx` + 断线监听 `disconnect_listener`；`db.register_client` 写入三张路由表（详见 [u3-l2](u3-l2-broker-db.md)）；成功后回 `RESPONSE_OK`。

构造并 select 三个 future：

[src/broker.rs:1816-1827](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1816-L1827) — 注意 `handle_reader` 和 `handle_writer` 共享同一个 `client` 与 `writer`/`reader`，三者**并发**跑（并不是顺序执行）。

[src/broker.rs:1847-1868](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1847-L1868) — `tokio::select!` 四选一：reader 结束、writer 结束、pinger 结束，或 `disconnect_listener` 触发（被代理主动踢掉，如 `force_register` 顶替）。无论哪条分支都先 `finish_peer!`（`db.unregister_client`）再返回。

调用入口 `handle_connection` 把 accept 到的流拆成读写半部、各自包上 `BufReader` / `TtlBufWriter`，再 spawn `handle_peer`：

[src/broker.rs:1174-1215](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1174-L1215) — `handle_connection`；错误经 `pretty_error!` 打印。`PeerHandlerParams` 结构定义见 [src/broker.rs:1258-1276](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1258-L1276)。

#### 4.1.4 代码实践

**实践目标**：观察握手失败与成功两种情况，验证 `handle_peer` 的握手机制。

**操作步骤**（源码阅读型，无需改源码）：

1. 阅读 [src/broker.rs:1748-1774](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1748-L1774)，确认握手字节顺序。
2. 用 `nc -U /tmp/busrt.sock`（或任意能连 Unix socket 的工具）连接一个由 `test.sh server` 启动的 `busrtd`，手动发送十六进制 `eb 01 00`（魔数 + 版本号 1 小端），观察代理回送的 `01`（RESPONSE_OK）。
3. 再故意发一个错误魔数（如 `00 01 00`），观察代理回送 `75`（ERR_NOT_SUPPORTED）后断开。

**需要观察的现象**：魔数/版本正确时握手通过、连接保持；错误时代理回送错误码并立即关闭连接。

**预期结果**：握手是「代理先发、客户端回应、代理再确认」的 3 次往返。如果本地没有 `nc` 或 busrtd，标注「待本地验证」即可，重点是读懂字节顺序。

#### 4.1.5 小练习与答案

**练习 1**：为什么握手控制帧要用 `Flush::Instant`，而业务帧出站时却常常用 `Flush::Scheduled` 或 `Flush::No`？

**答案**：握手是连接能否继续的前置条件，必须立刻送达并尽快完成（任何延迟都拖慢建链）；业务帧则希望合并多个小帧为一次系统调用以提高吞吐，因此用 `Scheduled`（等待 ~10µs ttl 合并）或 `No`（仅入缓冲）。这是「控制平面优先」与「数据平面吞吐」的取舍。

**练习 2**：`client_primary_name` 是如何从 `client_name` 得到的？为什么需要它？

**答案**：用 `client_name.find(SECONDARY_SEP)`（即 `"%%"`）定位分隔符，取其前半段；没有分隔符则整串即主名。主名用于 AAA 鉴权（`aaa_map.get(client_primary_name)`），因为二级客户端（如 `worker.1%%0`）应继承其主客户端的权限配置。

---

### 4.2 handle_reader：入站帧解析与 FrameOp 分发

#### 4.2.1 概念说明

`handle_reader` 是连接生命周期里**最重**的循环：它不断从 socket 读入客户端发来的帧，解析 9 字节头，按 `FrameOp` 把帧路由到「订阅控制」或「三种通信模式」，并在 `QoS::needs_ack()` 时回送 ACK。

它把前面讲过的三张路由表（[u3-l2](u3-l2-broker-db.md)）和三个分发宏（[u3-l3](u3-l3-communication-patterns.md)）真正接到了「外部客户端发来的字节」上。

#### 4.2.2 核心流程

```
loop {
  读 9 字节头 header_buf[0..9]
  ├─ flags==0 → OP_NOP（客户端心跳），trace 后 continue
  ├─ 解析 op  = flags & 0x3F（低 6 位）
  ├─ 解析 qos = (flags >> 6) & 0x3F（高 2 位）
  ├─ op_id = header_buf[0..4]（ACK 关联用）
  ├─ len   = u32::LE(header_buf[5..9])
  ├─ payload_size_limit 校验
  ├─ 分配 payload 缓冲（direct_alloc_limit / async_allocator 分支）
  └─ 读 len 字节 payload 到 buf

  match op {
    SubscribeTopic    → AAA 校验 → db.subscriptions.subscribe → 可选 ACK
    ExcludeTopic      → client.exclusions.insert → 置 has_exclusions → 可选 ACK
    UnexcludeTopic    → client.exclusions.remove → 可选清 has_exclusions → 可选 ACK
    UnsubscribeTopic  → db.subscriptions.unsubscribe → 可选 ACK
    _ (Message/Broadcast/Publish/PublishFor) →
        按 0x00 切出 target 与 payload_pos
        ├─ Message     → AAA → send!        → 可选 ACK(OK 或 error.kind)
        ├─ Broadcast   → AAA → send_broadcast! → 可选 ACK
        ├─ PublishTopic→ AAA → publish!     → 可选 ACK
        └─ PublishTopicFor → 再切 receiver → publish!(for) → 可选 ACK
  }
}
```

flags 字节的位划分：

\[
\text{flags} = \text{op} \;|\; (\text{qos} \ll 6)
\]

解码时 `op = flags & 0b0011\_1111`（低 6 位），`qos = (flags >> 6)`（高 2 位，取值 0–3）。

#### 4.2.3 源码精读

循环头与首包超时（`first_packet_timeout = timeout * 10 / 8`，给老客户端多 20% 余量）：

[src/broker.rs:1901-1919](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1901-L1919) — 读 9 字节头；读到 `UnexpectedEof`（对端正常关闭）时返回 `Ok(())`，这是「客户端优雅断开」的正常退出路径；`flags == 0` 即客户端 ping（OP_NOP），trace 后 `continue`。

解析 flags 与长度、payload 大小限制校验：

[src/broker.rs:1920-1931](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1920-L1931) — 拆出 `op` / `qos` / `op_id` / `len`；`payload_size_limit` 超限直接报错断连。

payload 缓冲分配——`direct_alloc_limit` 触发时，超大 payload 不在实时运行时线程上 `vec![0; len]`，而是交给 `async_allocator` 异步分配（实时特性，详见 [u7-l1](u7-l1-realtime.md)）：

[src/broker.rs:1932-1945](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1932-L1945) — 三分支：超过 limit 走 `async_allocator.allocate`、否则 `vec![0; len]`、未配置 limit 时直接 `vec![0; len]`。

`send_ack!` 宏——构造定长 6 字节 ACK 帧 `OP_ACK | op_id(4) | code(1)`，包成 `FrameKind::Prepared` 的 `FrameData` 推进**本客户端自己的** `client.tx`（也就是说 ACK 是代理写给「正在发送的那一方」的，走的是发送方自己的 writer）：

[src/broker.rs:1946-1972](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1946-L1972) — 注意 `realtime` 由 `qos.is_realtime()` 决定，ACK 会镜像请求的实时性；统计 `w_frames` / `w_bytes`（本客户端 + db 两份）。

`SubscribeTopic` 分支——按 `0x00` 切出多个主题，逐个做 AAA 订阅校验，通过者写入 `db.subscriptions`，再按 `needs_ack` 回 ACK：

[src/broker.rs:1974-2007](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1974-L2007) — 被拒绝的主题（`allow_subscribe_*` 不通过）在 `needs_ack` 时回 `ERR_ACCESS`，否则静默丢弃。

通用消息分发分支——先用 `splitn(2, |c| *c == 0)` 把 `target` 与 payload 分开，算出 `payload_pos = tgt.len() + 1`：

[src/broker.rs:2081-2088](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L2081-L2088) — 切出 target 与 payload 边界；`broken frame` 表示缺少 `0x00` 分隔符。

`FrameOp::Message`（点对点）——AAA 校验后调用 `send!` 宏，按返回结果回 ACK（成功 OK、失败回 `e.kind` 错误码）：

[src/broker.rs:2089-2118](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L2089-L2118) — `send!` 未命中目标返回 `not_registered`，会回送 `ERR_CLIENT_NOT_REGISTERED`(0x71)。

`FrameOp::Broadcast` 与 `FrameOp::PublishTopic` 分支：

[src/broker.rs:2119-2172](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L2119-L2172) — 广播与发布都「无人匹配也静默成功」（只回 OK），区别在查 `broadcasts` 表还是 `subscriptions` 表。

`FrameOp::PublishTopicFor`——在已有 `payload_pos` 基础上再切一个 `receiver`（定向发布，叠加主名筛选）：

[src/broker.rs:2173-2205](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L2173-L2205) — 二次 `splitn` 取 receiver，调整 `payload_pos`，调用 `publish!` 的「带 receiver」重载。

#### 4.2.4 代码实践

**实践目标**：在源码中标注「一次 `publish` 调用」经过 `handle_reader` 的每一步行号。

**操作步骤**：

1. 打开 `src/broker.rs`，定位 `FrameOp::PublishTopic` 分支 [src/broker.rs:2146-2172](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L2146-L2172)。
2. 向上回溯，确认 `op`/`qos`/`len` 的解析点 [src/broker.rs:1920-1923](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1920-L1923)。
3. 跟进 `publish!` 宏 [src/broker.rs:178-213](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L178-L213)，记下它查表（`db.subscriptions`）、retain 排除、构造 `Arc<FrameData>`、对每个订阅者 `safe_send_frame!` 的顺序。

**需要观察的现象**：一帧 `publish` 在 `handle_reader` 内只「读取 + 查表 + 投递到订阅者通道」，**不直接写 socket**——真正的 socket 写发生在订阅者的 `handle_writer` 里（见 4.3）。

**预期结果**：你能用一句话说清——`handle_reader` 负责「解析 + 路由 + 推通道」，`handle_writer` 负责「序列化 + 写 socket」，两者经 `client.tx` 通道解耦。

#### 4.2.5 小练习与答案

**练习 1**：客户端发来一帧，`flags = 0b0101_0010`。请算出 `op` 和 `qos` 分别是什么。

**答案**：`op = flags & 0x3F = 0b01_0010 = 0x12 = OP_MESSAGE`（点对点 Message）；`qos = flags >> 6 = 0b01 = 1 = QoS::Processed`（需要 ACK、非实时）。

**练习 2**：为什么 `send_ack!` 把帧推进 `client.tx`（发送方自己的入站通道），而不是直接写 socket？

**答案**：因为一个连接的所有出站字节都必须由唯一的 `handle_writer` 序列化（保证字节顺序、统一经过 `TtlBufWriter` 合并缓冲）。把 ACK 也当成一帧推进 `client.tx`，让 writer 统一处理，避免了「reader 直写」与「writer 写」交错导致字节流错乱。

---

### 4.3 handle_writer：出站帧序列化

#### 4.3.1 概念说明

`handle_writer` 是连接的「出口」。它从 `client.tx` 的接收端 `rx`（即 `EventChannel`）不断取出 `Frame`，把内存中的 `FrameData` 序列化成线上 6 字节头 + 体的字节流，经 `TtlBufWriter` 写出去。

它与 `handle_reader` 是**严格解耦**的：reader 只管往 `tx` 推帧，writer 只管从 `rx` 取帧并写 socket。代理要「转发」一条消息，本质就是 reader 收到后推进**接收方**的 `tx`，由接收方的 writer 写出。

#### 4.3.2 核心流程

```
loop {
  rx.recv().await → 取得一帧 Frame (Arc<FrameData>)
  ├─ FrameKind::Prepared（ACK / NOP 等已序列化帧）
  │     → 直接 write(frame.buf, frame.realtime.into())
  └─ 其它（Message / Broadcast / Publish）
        → 拼装 6 字节头 + 体：
           byte 0:   kind（OP_MESSAGE=0x12 / OP_BROADCAST=0x13 / OP_PUBLISH=0x01）
           byte 1-4: frame_len（u32 小端）
           byte 5:   realtime（0/1）
           体:        sender + 0x00 [+ topic + 0x00] [+ header] + payload
        → write(头, Flush::No)   // 仅入缓冲
        → write(header, Flush::No)
        → write(payload, frame.realtime.into())  // 实时则 Instant，否则 Scheduled
}
```

代理→客户端帧的体长度计算：

\[
\text{frame\_len} = \underbrace{(\text{sender.len}+1)}_{\text{sender + 分隔符}} + \underbrace{[\text{topic.len}+1]}_{\text{可选}} + \underbrace{[\text{header.len}]}_{\text{零拷贝前缀}} + \underbrace{(\text{buf.len} - \text{payload\_pos})}_{\text{payload}}
\]

#### 4.3.3 源码精读

`handle_writer` 主体——`while let Ok(frame) = rx.recv().await`，通道关闭（`rx` 返回 `Err`）时退出循环返回 `Ok(())`：

[src/broker.rs:2213-2263](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L2213-L2263) — 整个序列化逻辑。

**Prepared 分支**（[src/broker.rs:2228-2229](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L2228-L2229)）：ACK 与 NOP 这类帧在 `handle_reader` 里已经拼好了完整字节（见 `send_ack!` 的 `buf.to_vec()`），这里直接把 `frame.buf` 整块写出，按 `frame.realtime` 决定刷新策略。

**普通帧拼装**——计算 `extra_len`、清空复用缓冲 `buf`、依次推入 6 字节头与体：

[src/broker.rs:2231-2255](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L2231-L2255) — `buf` 预分配 `6 + MAX_SENDER_NAME_LEN`（256，见 [src/broker.rs:52](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L52)）避免频繁扩容；头与 sender/topic 用 `Flush::No` 仅入缓冲，**只有最后的 payload** 用 `frame.realtime.into()` 决定是否立即刷新。

这里体现了 [u4-l3](u4-l3-ttl-buf-writer.md) 讲过的 `From<bool> for Flush` 映射：`realtime=true → Flush::Instant`、`realtime=false → Flush::Scheduled`（[src/comm.rs:15-23](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/comm.rs#L15-L23)）。于是头与中段始终合并缓冲，只由 payload 的实时位决定整帧是否跳过 ~10µs 合并窗口、立即落网——这正是端到端低延迟的关键。

#### 4.3.4 代码实践

**实践目标**：手算一条点对点 `Message` 帧被 `handle_writer` 序列化后的完整字节。

**操作步骤**：

1. 假设发送方 `sender = "a"`，payload = `"hi"`（2 字节），`realtime = false`。
2. 对照 [src/broker.rs:2241-2255](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L2241-L2255) 逐步拼字节。
3. 写出：`kind`(1) + `frame_len`(4 小端) + `realtime`(1) + `"a"` + `0x00` + `"hi"`。

**需要观察的现象 / 预期结果**：

- `kind = OP_MESSAGE = 0x12`
- 体 = `a` + `\0` + `hi` = 5 字节 → `frame_len = 5 = 0x05 0x00 0x00 0x00`
- `realtime = 0x00`
- 完整线上字节（共 11 字节）：
  `12 05 00 00 00 00 61 00 68 69`
  （即 `0x12` + len5 + `0x00` + `'a'=0x61` + `\0=0x00` + `'h'=0x68` + `'i'=0x69`）

注意点对点 Message 帧**不带 topic**，所以体里只有 `sender\0 payload`；而 `Publish` 帧会在 sender 之后多一段 `topic\0`。

#### 4.3.5 小练习与答案

**练习 1**：为什么头部和 sender/topic 用 `Flush::No`，而 payload 用 `frame.realtime.into()`？

**答案**：一帧是一个逻辑单元，要整体决定「是否立即发送」。把头与中段先入缓冲（`No`），最后写 payload 时根据 realtime 位统一决定——若是实时帧，payload 这一步用 `Instant` 会触发对**整段已缓冲内容**的立即 flush（因为 `Instant` 在持锁状态下同步刷新整个 BufWriter）；若非实时则用 `Scheduled` 让 flusher 合并窗口内的多帧一起发。这样既保证实时帧低延迟，又让普通帧能合并。

**练习 2**：`handle_writer` 在什么情况下正常退出（返回 `Ok(())`）？

**答案**：当 `rx.recv().await` 返回 `Err`，即发送端 `client.tx` 被关闭时。这通常发生在 `handle_peer` 的 `tokio::select!` 中其它分支（reader / pinger / disconnect）先结束、或 `safe_send_frame!` 对外部客户端满队列时调用 `tx.close()` 之后。通道关闭 → writer 循环自然结束。

---

### 4.4 handle_pinger：心跳与背压守护

#### 4.4.1 概念说明

`handle_pinger` 是一个**周期性后台循环**，做两件事：

1. **心跳保活**：每隔 `timeout / 2` 往客户端的 `tx` 推一个 NOP 帧，`handle_writer` 会把它序列化成 6 字节的 NOP 发出去，证明「代理还活着」。
2. **背压守护**：每次发送前检查 `tx.is_full()`——如果客户端的入站队列满了，说明该客户端消费太慢，代理**主动**给它发一个 `io` 错误从而终结连接（在 `select!` 里触发 `finish_peer!`）。

它和 [u4-l2](u4-l2-ipc-client.md) 讲的客户端侧心跳是对称的：客户端每隔 `timeout/2` 发 9 字节 PING（`handle_reader` 里 `flags==0` 那条路径），代理这边发 6 字节 NOP。两端互探，谁在 `timeout` 内收不到对方的心跳或数据就认为连接已死。

#### 4.4.2 核心流程

```
loop {
  sleep(timeout / 2)
  if tx.is_full():
      warn("queue is full, force unregistering")
      return Err(io)            // → 在 select! 中终结连接
  tx.send(FrameData::new_nop()) // 推一帧 NOP 给 writer 序列化发出
}
```

#### 4.4.3 源码精读

[src/broker.rs:1871-1884](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1871-L1884) — 整个函数。注意三个细节：

- 间隔是 `timeout / 2`（不是 `timeout`），保证即便丢一次心跳，在 `timeout` 内仍有第二次机会，降低误判。
- `tx.is_full()` 检查发生在 `send` **之前**：若已满就不再尝试入队，直接报错踢掉，避免 NOP 本身把队列彻底撑满后无法恢复。
- NOP 帧 `FrameData::new_nop()` 在 `handle_writer` 里走的是**普通帧分支**（`FrameKind::Nop`，不是 `Prepared`），会被序列化成 `[OP_NOP=0x00][len=0...][realtime]` 的 6 字节帧（sender/topic/payload 全空）。

配合客户端侧的心跳路径——`handle_reader` 收到 `flags == 0` 时只是 `trace!("{} ping")` 然后 `continue`，即客户端心跳对代理而言是「无副作用的心跳包」。

#### 4.4.4 代码实践

**实践目标**：观察「客户端不消费 → 队列堆满 → 被 pinger 踢掉」的背压行为。

**操作步骤**（源码阅读型 + 可选运行）：

1. 阅读 [src/broker.rs:1876-1883](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1876-L1883)，确认满队列返回 `Error::io("client queue overflow")`。
2. 设想一个「只订阅 `#` 但从不调用 `rx.recv()`」的客户端（即注册后不取事件通道、也不消费），让大量发布者持续 `publish`。
3. 追踪该客户端的 `client.tx` 会被 `publish!` 宏里的 `safe_send_frame!` 不断推帧（见 [src/broker.rs:206-210](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L206-L210)），直到 8192 容量填满。

**需要观察的现象 / 预期结果**：队列填满后，下一次 pinger 周期到来时 `tx.is_full()` 为真，代理日志出现 `client ... queue is full, force unregistering`，该客户端被强制注销。**对比**：若被填满的是**内部客户端**（`BusRtClientKind::Internal`），`safe_send_frame!` 走的是「阻塞发送方」而非踢掉（[src/broker.rs:85-98](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L85-L98)）——这是内部/外部客户端最关键的行为差异（详见 [u3-l1](u3-l1-broker-and-internal-client.md)）。运行结果待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：pinger 发的是 NOP 帧，客户端也发 PING 帧。这两者为什么不冲突、也不会被对方当成业务消息？

**答案**：NOP/ PING 的 `op` 都是 `OP_NOP = 0x00`。代理侧 `handle_reader` 看到 `flags == 0` 直接 `continue`；客户端侧也会识别 NOP 帧并丢弃。它们是「带外」的保活信号，不进入任何路由表，也不触发 ACK。

**练习 2**：把 pinger 的间隔从 `timeout / 2` 改成 `timeout` 会有什么风险？

**答案**：丢一次心跳后，下一次探测要等满 `timeout`，可能已经超过对端的超时阈值，导致对端先一步判定连接死亡并断开，出现「双方都在、却互相踢掉」的假死误判。`timeout / 2` 给了一次重试余量。

---

## 5. 综合实践：追踪一条 publish 帧的完整数据流

**实践目标**：把本讲四个函数串起来，画出一条 `publish` 消息从发送方到订阅者的端到端数据流图，并在源码中标注每一步的行号。

**操作步骤**：

1. **准备环境**：参照 `test.sh server` 启动一个监听 `/tmp/busrt.sock` 的 `busrtd`（它会 `init_default_core_rpc`）。
2. **启动订阅者**：运行 [examples/client_listener.rs](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/examples/client_listener.rs)——它 `subscribe("#", QoS::Processed)` 订阅全部主题，然后循环 `rx.recv()` 打印 `sender / kind / topic / payload`。
3. **启动发布者**：运行 [examples/client_sender.rs](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/examples/client_sender.rs)——它对 `some/topic` 执行 `publish("hello", QoS::Processed)`。
4. **观察订阅者输出**：应打印类似 `Frame from test.client.sender: Publish Some("some/topic") hello`。
5. **对照源码画数据流图**（下图标注了关键行号）：

```
[发送方进程]  examples/client_sender.rs
   │  client.publish("some/topic", "hello", QoS::Processed)
   │  → ipc::Client 经 socket 发出 9 字节头帧: op_id|flags(op=0x01,qos=1)|len + "some/topic\0hello"
   ▼
[代理进程]  src/broker.rs
   ① handle_reader 循环读到该帧
      • 解析 flags → op=PublishTopic, qos=Processed  (L1920-1923)
      • 按 0x00 切出 target="some/topic", payload_pos (L2082-2086)
   ② AAA 校验通过 → 调用 publish! 宏  (L2146-2165)
   ③ publish! 宏内部  (L178-213)
      • db.subscriptions.get_subscribers("some/topic")  → 找到订阅者集合
      • retain 排除 has_exclusions 的客户端            (L188-191)
      • 构造 Arc<FrameData>{ kind=Publish, sender=Some("test.client.sender"),
                              topic=Some("some/topic"), buf, payload_pos, realtime }
      • 对每个订阅者: safe_send_frame!(订阅者.tx, frame.clone())  (L206-210)
        ※ 关键：这里推进的是【订阅者】的 tx，不是发送方的！
   ④ ACK：因 qos.needs_ack() → send_ack!(RESPONSE_OK) 推进【发送方】自己的 tx (L2166-2168)
   ▼
[订阅者进程内、代理侧]  订阅者的 handle_writer 循环
   ⑤ rx.recv() 取到上面推进的 Publish 帧              (L2222)
   ⑥ 序列化为 6 字节头 + 体:                          (L2241-2259)
      kind=0x01 | len(4 LE) | realtime | "test.client.sender\0" "some/topic\0" "hello"
   ⑦ 经 TtlBufWriter 写入【订阅者】的 socket
   ▼
[订阅方进程]  examples/client_listener.rs
   ⑧ 客户端 ipc::Client 的 handle_read 收到 6 字节头帧
      按 0x00 切出 sender / topic / payload，包成 Frame 投递事件通道
   ⑨ rx.recv() 打印: Frame from test.client.sender: Publish some/topic hello
```

**需要观察的现象**：

- 订阅者收到了发布者的消息，`kind` 为 `Publish`，`topic` 为 `some/topic`。
- 发送方因 `QoS::Processed` 收到了 ACK（`opc.await??` 不报错）。
- 发送方与订阅者是**两个不同的连接**，各自有独立的 `handle_reader` / `handle_writer` / `handle_pinger` 三人组；它们之间唯一的耦合点是代理内部的 `client.tx` 通道与三张路由表。

**关键理解点**（请务必想清楚）：

1. **转发 = 跨连接推通道**。`handle_reader`（连接 A）解析完一帧后，把结果推进的是连接 B（订阅者）的 `tx`。代理本身不「写 A 的 socket 转发到 B 的 socket」，而是「A 的 reader → B 的 tx → B 的 writer」。
2. **零拷贝**。`publish!` 构造的是 `Arc<FrameData>`，多个订阅者通过 `frame.clone()`（仅增 `Arc` 引用计数）共享同一块缓冲，不复制 payload 字节（[src/broker.rs:193-201](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L193-L201)）。
3. **ACK 走发送方自己的通道**。`send_ack!` 推进的是发送方 `client.tx`（[src/broker.rs:1959-1970](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1959-L1970)），由发送方自己的 `handle_writer` 序列化成 6 字节 ACK 帧发回。
4. **实时性端到端透传**。发送方在 flags 高位设的 `is_realtime()`，被 `handle_reader` 读出后赋给 `FrameData.realtime`，一路传到订阅者的 `handle_writer`，最终决定 payload 用 `Flush::Instant` 立即落网。

如果本地无法运行，标注「待本地验证」；数据流图与行号对照可通过纯阅读源码完成。

## 6. 本讲小结

- `handle_peer` 是「一个连接 = 一个函数调用」的总指挥：握手 → 注册（含 AAA 连接级鉴权）→ `select!` 编排 reader/writer/pinger 三任务，任一结束即 `unregister_client`。
- `handle_reader` 是入站主循环：读 9 字节头 → 拆 `flags` 为 `op`(低 6 位)/`qos`(高 2 位) → 按 `FrameOp` 分发到订阅控制或 `send!`/`send_broadcast!`/`publish!` 三宏 → 按 `needs_ack` 回 6 字节 ACK。
- `handle_writer` 是出站主循环：从 `client.tx` 取帧，序列化成 6 字节头 + 体的线上字节；头与中段用 `Flush::No` 合并缓冲，仅 payload 由 `realtime` 位决定 `Instant`/`Scheduled`。
- `handle_pinger` 是心跳 + 背压守护：每 `timeout/2` 推一帧 NOP 保活，并在队列满时主动踢掉慢消费者。
- **转发的本质是跨连接推通道**：reader 解析后推进**接收方**的 `tx`，由接收方的 writer 写出；发送方与接收方是两个独立连接。
- ACK、NOP 也是帧，统一走 `handle_writer` 序列化，保证每连接单写出站、字节流不交错。

## 7. 下一步学习建议

- **[u6-l2 多传输层](u6-l2-multi-transport.md)**：本讲的 `handle_peer` 只关心「读写半部」，不关心字节来自 Unix / TCP / WebSocket 哪种 socket。下一讲看 `spawn_server!` 宏与 `spawn_unix_server` / `spawn_tcp_server` / `spawn_websocket_server` 如何把不同监听器抽象成统一的 `(reader, writer)` 喂给 `handle_connection`。
- **[u6-l3 AAA 访问控制](u6-l3-aaa-access-control.md)**：本讲只接触了连接级的 `connect_allowed` 与每帧的 `allow_*_any / allow_*_to.matches()`。下一讲系统讲 `ClientAaa` 的四类权限与主题/对端掩码语法。
- **重读 [u3-l3 三种通信模式](u3-l3-communication-patterns.md)**：现在你已看到 `send!`/`send_broadcast!`/`publish!` 三宏的真实调用点（`handle_reader` 的 `_ =>` 分支），可回去印证「查找→统计→扇出」三段式与 exclude 机制。
- **进阶阅读**：想理解实时性如何在这条链路上生效，可提前读 [u7-l1 实时特性](u7-l1-realtime.md)，重点关注 `handle_reader` 里 `direct_alloc_limit` 分支（[src/broker.rs:1932-1944](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1932-L1944)）与 `handle_writer` 的 `realtime` 刷新策略如何协同。
