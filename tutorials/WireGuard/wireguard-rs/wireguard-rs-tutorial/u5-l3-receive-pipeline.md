# 接收管道：解密（ReceiveJob）

## 1. 本讲目标

本讲是路由器数据面讲解的**入站对称篇**。在 [u5-l2](./u5-l2-send-pipeline.md) 我们已经看完了出站方向（明文 IP 包 → 加密 → UDP 发出），本讲反过来看入站方向：从 UDP 收到一条密文传输报文开始，到把解密后的明文 IP 包写回 TUN 网卡为止。

读完本讲，你应该能够：

1. 说出 `ReceiveJob` 把入站处理拆成 **并行（`parallel_work`）** 与 **串行（`sequential_work`）** 两个阶段的根本原因，以及每个阶段各承担什么职责。
2. 解释为什么 **AEAD 解密** 与 **cryptokey 路由校验（`check_route`）** 放在并行阶段，而 **防回放（`AntiReplay::update`）** 必须放在串行阶段。
3. 看懂「认证失败 / 路由校验失败时把缓冲区截断到 0 字节」这一安全设计，是如何防止未认证数据被误用的。
4. 描述响应方如何借由 **首个成功解密的传输报文** 把 `next` 密钥「转正」为 `current`（密钥的静默确认机制）。
5. 识别 **keepalive 报文**，并说明它在写入 TUN 之前是如何被丢弃的。

---

## 2. 前置知识

本讲假设你已经掌握以下内容（若不熟请先回看依赖讲义）：

- **WireGuard 传输报文格式**：由 16 字节明文 `TransportHeader`（`f_type` / `f_receiver` / `f_counter`）+ 密文载荷 + 16 字节 Poly1305 认证标签（`SIZE_TAG`）组成。详见 [u5-l1](./u5-l1-router-overview.md) 与 [u5-l2](./u5-l2-send-pipeline.md)。
- **AEAD（ChaCha20-Poly1305）**：一种「加密 + 认证」一体的算法。`open_in_place`（解密）只有在密钥、nonce、密文都正确时才返回明文，否则报错；这一步既解密又校验了完整性。
- **receiver id**：报文头里的 4 字节 `f_receiver`，是一个短期的临时标识，接收方用它查表定位「该用哪个解密状态（`DecryptionState`）来解这条报文」。见 [u4-l2](./u4-l2-handshake-device.md)。
- **KeyWheel 三密钥轮转**：`next`（未确认）/ `current`（当前加密用）/ `previous`（旧解密用）。见 [u5-l1](./u5-l1-router-overview.md) 与 [u5-l6](./u5-l6-keywheel-peer.md)。
- **保序队列 `Queue`**：靠 `contenders` 原子计数实现「多线程并行做 `parallel_work`、单线程按入队顺序做 `sequential_work`」的机制。见 [u5-l1](./u5-l1-router-overview.md) 与 [u5-l4](./u5-l4-ordered-queue.md)。

> 关键直觉：**解密是「重活」、防回放是「需要排队的活」**。本讲的核心就是把这两件事分别塞进并行阶段和串行阶段，从而既榨干多核 CPU，又保证协议正确性。

---

## 3. 本讲源码地图

本讲涉及的关键文件及其职责：

| 文件 | 作用 |
| --- | --- |
| [src/wireguard/router/receive.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/receive.rs) | **本讲主角**。定义 `ReceiveJob`，实现 `ParallelJob`（解密 + 路由校验）与 `SequentialJob`（防回放 + 密钥确认 + 写 TUN + 回调）。 |
| [src/wireguard/router/device.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/device.rs) | 定义 `DecryptionState`（每个接收密钥的状态：密钥、防回放器、所属 peer、确认标志）与 `Device::recv` 入口（按 receiver id 查表、入队）。 |
| [src/wireguard/router/peer.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs) | `Peer::confirm_key`（首个报文转正密钥）、`DecryptionState::new`（确认标志初值）。 |
| [src/wireguard/router/ip.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/ip.rs) | `inner_length`：从解密后的明文判定真实 IP 包长度，顺带识别 keepalive。 |
| [src/wireguard/router/route.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/route.rs) | `RoutingTable::check_route`：按**源地址**反查，校验报文归属的 peer，防伪造。 |
| [src/wireguard/router/anti_replay.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/anti_replay.rs) | RFC 6479 滑动位图防回放，`update` 同时检查并标记序号。 |
| [src/wireguard/router/messages.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/messages.rs) | `TransportHeader` 的 zerocopy 布局。 |

---

## 4. 核心概念与源码讲解

### 4.1 接收管道全景：从 `recv` 到两阶段处理

#### 4.1.1 概念说明

入站方向的第一个处理者是 `udp_worker`（见 [u3-l3](./u3-l3-udp-worker.md)）。当它读到一条 `TYPE_TRANSPORT(4)` 报文时，会调用 `Device::recv(src, msg)`，这就进入了路由器的接收管道。

接收管道同样遵循路由器的「**两层队列模型**」（见 [u5-l1](./u5-l1-router-overview.md)）：

- **并行阶段（`parallel_work`）**：多个 worker 线程同时干活，做重活、无副作用的计算。
- **串行阶段（`sequential_work`）**：靠 per-peer 的 `Queue` + `contenders` 互斥，保证**同一个 peer** 的报文按入队顺序逐个处理副作用。

`ReceiveJob` 是这条管道上的任务对象，它同时实现了这两个 trait。一个报文从 UDP 进来，会被包装成一个 `ReceiveJob`，先丢进该 peer 的保序 `Queue`，再丢进全局并行派发队列；worker 线程取出后先调 `parallel_work()`，再调 `queue().consume()` 触发串行消费。

#### 4.1.2 核心流程

入站一条传输报文的完整流程（伪代码）：

```
udp_worker 读到 TYPE_TRANSPORT 报文
        │
        ▼
Device::recv(src, msg):
   1. 解析 TransportHeader，读出 f_receiver（receiver id）
   2. 在 recv 表（receiver id → DecryptionState）里查到对应的解密状态 dec
      （查不到 → RouterError::UnknownReceiverId，丢弃）
   3. 用 (msg, dec, src) 构造 ReceiveJob
   4. 先 dec.peer.inbound.push(job)   ── 进 per-peer 保序队列（满则丢）
      再 work.send(JobUnion::Inbound(job)) ── 进全局并行派发队列
        │
        ▼
某个 worker 线程取出 job:
   job.parallel_work();    ── 并行：解密 + cryptokey 路由校验（失败→截断）
   job.queue().consume();  ── 串行：防回放 + 密钥确认 + 更新端点 + 写 TUN + 回调
```

关键点：`Device::recv` 本身**不做任何密码学运算**，它只负责「按 receiver id 找到解密状态」并把任务投递出去。真正的解密在 worker 线程里异步发生。

#### 4.1.3 源码精读

**`Device::recv` 入口**——按 receiver id 查 `recv` 表并构造 `ReceiveJob`：

[device.rs:211-250](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/device.rs#L211-L250)：解析 `TransportHeader`，用 `header.f_receiver.get()` 在 `recv` 表里查 `DecryptionState`，构造 `ReceiveJob` 后**先入保序队列、再入并行队列**。

核心两句：

```rust
// 先入 per-peer 保序队列（满了就丢，返回 false 则不派发）
if dec.peer.inbound.push(job.clone()) {
    self.state.work.send(JobUnion::Inbound(job));
}
```

注释 L244-L245 明确说明了这个顺序的意图：「先加进保序队列（满则丢），再加进并行工作队列（满则等）」。保序队列的 `push` 决定了串行处理的先后；并行队列只负责把活派给空闲 worker。

**worker 线程的对称处理**——入站出站一视同仁，都是「先 parallel_work 再 consume」：

[worker.rs:25-28](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/worker.rs#L25-L28)：

```rust
Ok(JobUnion::Inbound(job)) => {
    job.parallel_work();
    job.queue().consume();
}
```

`consume` 内部会取 per-peer 队列里**队首且已 ready** 的任务执行 `sequential_work`（详见 [u5-l4](./u5-l4-ordered-queue.md)）。这就是从「并行」过渡到「有序」的唯一桥梁。

#### 4.1.4 代码实践

**实践目标**：理清「receiver id → 解密状态」这条查表链路在源码里的具体位置。

**操作步骤**：

1. 打开 `src/wireguard/router/device.rs`，定位 `pub fn recv`（L211）。
2. 找到 `self.state.recv.read()` 与 `.get(&header.f_receiver.get())`（L236-L239），确认 `recv` 表的类型（见 `DeviceInner::recv` 字段，[device.rs:34](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/device.rs#L34)）。
3. 回忆 [peer.rs 的 `add_keypair`](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L436)：握手成功后，新密钥的 `recv.id` 被插入这张 `recv` 表——这就是 receiver id 与解密状态建立联系的源头。

**需要观察的现象**：`recv` 表的 key 是 `u32`（receiver id），value 是 `Arc<DecryptionState>`，即每个接收密钥对应一份独立的解密状态（含独立的防回放位图）。

**预期结果**：你能在脑中画出 `add_keypair 写入 recv 表` → `Device::recv 读 recv 表` → `ReceiveJob 携带 DecryptionState` 的闭环。

#### 4.1.5 小练习与答案

**练习 1**：`Device::recv` 找不到 receiver id 时返回什么错误？为什么不直接 panic？

**参考答案**：返回 `RouterError::UnknownReceiverId`（[device.rs:239](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/device.rs#L239)）。receiver id 是短期的、会被回收复用的，网络上行随时可能收到属于已过期密钥的迟到报文或攻击者伪造的报文，属于「正常的异常」，必须优雅丢弃而非崩溃。

**练习 2**：为什么 `push`（保序队列）在 `work.send`（并行队列）之前调用？

**参考答案**：保序队列的入队顺序就是后续串行处理的顺序。若先入并行队列、worker 极快地取走并 `consume`，而此时该 job 还没进保序队列，`consume` 在队首就看不到它，串行处理会被推迟甚至乱序。先入保序队列，保证了「job 一定在队列里」这一不变量在它被 worker 处理前成立。

---

### 4.2 `parallel_work`：并行解密与 cryptokey 路由校验

#### 4.2.1 概念说明

`parallel_work` 是接收管道里最重的一步，承担两件事：

1. **AEAD 解密（`open_in_place`）**：用 `DecryptionState` 里持有的接收密钥 + 报文头里的 counter 构造 nonce，对载荷做 ChaCha20-Poly1305 解密并认证。这是 CPU 密集型操作，正是要靠多核并行来摊薄的「重活」。
2. **cryptokey 路由校验（`check_route`）**：解密成功后，检查明文 IP 包的**源地址**是否确实属于当前 peer 的 allowed-ips 范围。这是一道**防伪造**关卡——防止某个 peer（或拿到某 peer 密钥的攻击者）冒充别的来源发包。

这两件事共同的特点是：**纯函数性质、无共享可变状态、无副作用**。解密只读自己的密钥和报文缓冲；`check_route` 只读路由表（`spin::RwLock` 读锁）。因此可以安全地被多个 worker 线程并发执行，互不干扰。

#### 4.2.2 核心流程

`parallel_work` 内部用一个立即调用的闭包把整段校验收敛成一个 `ok: bool`：

```
parallel_work():
  锁定 job.buffer
  ok = (|| {
     1. LayoutVerified 把缓冲区切成 (TransportHeader, 载荷)
        失败 → return false
     2. 构造 12 字节 nonce：前 4 字节为 0，后 8 字节 = header.f_counter
     3. LessSafeKey::open_in_place(nonce, Aad::empty(), 载荷)
        失败（认证失败）→ return false
     4. if header.f_counter >= REJECT_AFTER_MESSAGES → return false   （计数耗尽）
     5. return 载荷长度 == SIZE_TAG   ← keepalive：空载荷只剩一个标签
            || peer.device.table.check_route(&peer, &packet)          ← 源地址归属校验
  })();
  if !ok { buffer.truncate(0) }    ← 失败就清空，杜绝未认证数据被误用
  ready.store(true, Release)        ← 通知串行阶段「我好了」
```

注意第 5 步的两个分支：
- `packet.len() == SIZE_TAG`：载荷在 `open_in_place` 后若**只剩 16 字节标签**，说明明文为空，这是一条 **keepalive**（见 4.4），直接放行，不做路由校验。
- 否则才调用 `check_route`，按源地址查表。

#### 4.2.3 源码精读

**`ReceiveJob` 的内部结构**——一个被 `Arc` 包裹的 `Inner`，持有就绪标志、缓冲区（端点 + 密文）与解密状态：

[receive.rs:16-20](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/receive.rs#L16-L20)：

```rust
struct Inner<E, C, T, B> {
    ready: AtomicBool,                   // job 状态：parallel_work 完成后置 true
    buffer: Mutex<(Option<E>, Vec<u8>)>, // 端点 & 密文缓冲区
    state: Arc<DecryptionState<E, C, T, B>>, // 解密状态（密钥 + 防回放器）
}
```

`buffer` 用 `Mutex` 包裹，是因为 `parallel_work`（写：解密、截断）与 `sequential_work`（读：写 TUN）在不同线程访问它；`ready` 用原子变量配 `Release`/`Acquire` 序做跨线程的「完成通知」。

**`parallel_work` 的关键注释**——把「为什么这么分阶段」讲得很清楚：

[receive.rs:55-65](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/receive.rs#L55-L65)：

> 并行阶段做：解密 + cryptokey 路由查找。
> 注意：认证失败或路由校验失败（疑似冒充）时，会把消息缓冲区截断到 0 字节。
> 注意：**不能在并行阶段做防回放**，否则会因为线程调度导致报文被错误地丢出（滑出）窗口外。

这段注释是本讲最核心的设计陈述，4.3 节会专门展开「为什么不能在并行阶段防回放」。

**解密本体**——构造 nonce、`open_in_place`、counter 上限检查：

[receive.rs:90-109](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/receive.rs#L90-L109)：

```rust
// 构造 12 字节 nonce：高 4 字节为 0，低 8 字节为 counter
let mut nonce = [0u8; 12];
nonce[4..].copy_from_slice(header.f_counter.as_bytes());
let nonce = Nonce::assume_unique_for_key(nonce);
let key = LessSafeKey::new(
    UnboundKey::new(&CHACHA20_POLY1305, &job.state.keypair.recv.key[..]).unwrap(),
);
// 尝试解密（并认证）载荷
match key.open_in_place(nonce, Aad::empty(), packet) {
    Ok(_) => (),
    Err(_) => return false,        // 认证失败 → ok = false
}
// counter 不能达到 REJECT_AFTER_MESSAGES
if header.f_counter.get() >= REJECT_AFTER_MESSAGES {
    return false;
}
```

> 术语解释：`Aad::empty()` 表示「附加认证数据为空」。WireGuard 的传输报文头**不参与** AEAD 认证（与握手报文不同），因为 `f_receiver` 与 `f_counter` 的完整性已由「接收方用它定位密钥、密钥本身正确才能解密成功」这一事实隐式保证。

**cryptokey 路由校验**——`ok` 闭包的最后一行：

[receive.rs:111-113](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/receive.rs#L111-L113)：

```rust
// 检查 cryptokey 路由：keepalive（空载荷）放行；否则校验源地址归属
packet.len() == SIZE_TAG || peer.device.table.check_route(&peer, &packet)
```

`check_route` 的实现见 [route.rs:116-138](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/route.rs#L116-L138)：它解析明文 IP 头，取**源地址**做最长前缀匹配（`longest_match`），并要求匹配到的 peer **就是** 解密这条报文的那个 peer（`p == peer`）。这与出站 `get_route`（按**目的地址**选 peer）恰好对称：**出站按目的、入站按源**。

**失败截断**——`parallel_work` 末尾的安全闸：

[receive.rs:115-123](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/receive.rs#L115-L123)：

```rust
// 失败时清空消息：标记失败，并避免后续误用未认证数据
if !ok {
    msg.1.truncate(0);
}
// 标记就绪
self.0.ready.store(true, Ordering::Release);
```

截断到 0 后，串行阶段的 `LayoutVerified::new_from_prefix` 会因缓冲区不足 16 字节而解析 `TransportHeader` 失败，从而提前 `return`（见 4.3.3）。这样即便 job 仍进入串行阶段，也不会有任何未认证字节流向 TUN 或回调。

#### 4.2.4 代码实践

**实践目标**：动手验证「cryptokey 路由校验放在并行阶段、且只读路由表」这一事实。

**操作步骤**：

1. 打开 [route.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/route.rs)，阅读 `check_route`（L116-L138），确认它只调用 `self.ipv4.read()` / `self.ipv6.read()`（**读锁**），不修改任何状态。
2. 对比 `get_route`（L74-L114，出站用），两者都调 `longest_match`，但 `check_route` 取的是 `header.f_source`，`get_route` 取的是 `header.f_destination`。
3. 在 `check_route` 末尾临时加一行 `log::trace!("check_route: src belongs to peer = {}", p == peer);`（仅用于阅读理解，**不要提交**），重新 `cargo build`。

**需要观察的现象**：编译能通过，说明 `check_route` 在 `parallel_work` 的并发环境下被调用没有任何线程安全问题（只持读锁）。

**预期结果**：你能口头复述——「`check_route` 只读、无副作用，所以放进并行阶段安全；它防的是『拿到 peer A 密钥的人冒充 peer B 的源地址』这种伪造」。

> 待本地验证：是否真的加日志、运行观察由你决定；若不运行，理解代码即可。

#### 4.2.5 小练习与答案

**练习 1**：`open_in_place` 返回 `Err` 时，函数返回 `false`。这个 `false` 最终会导致什么副作用？

**参考答案**：`ok` 为 `false`，触发 `msg.1.truncate(0)`（[receive.rs:118](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/receive.rs#L118)），缓冲区被清空。随后串行阶段解析 `TransportHeader` 失败而提前返回，报文不会写进 TUN，也不会触发密钥确认。

**练习 2**：为什么 keepalive（`packet.len() == SIZE_TAG`）要**跳过** `check_route`？

**参考答案**：keepalive 的明文载荷为空，根本没有 IP 头，`check_route` 无法解析源地址。keepalive 的唯一作用是「保活」（刷新端点、确认密钥、驱动定时器），不携带任何用户数据，没有伪造源地址的意义，因此直接放行。

---

### 4.3 `sequential_work`：防回放、密钥确认与写入 TUN

#### 4.3.1 概念说明

`sequential_work` 在 per-peer 保序队列里**逐个串行执行**，承担所有「需要修改共享状态、或对顺序敏感」的工作：

1. **防回放（`AntiReplay::update`）**：检查并标记 `f_counter`，拒绝重复或过旧的序号。
2. **密钥确认（`confirm_key`）**：首个成功报文把响应方的 `next` 密钥转正为 `current`。
3. **更新端点**：学习/刷新对端真实地址（支撑漫游）。
4. **写入 TUN**：把解密后的明文 IP 包交还内核网络栈。
5. **触发回调 `C::recv`**：通知上层（定时器层）字节计数与密钥使用情况。

为什么这些必须串行？因为它们要么修改共享可变状态（防回放位图、KeyWheel、端点），要么对处理顺序敏感。`Queue::consume` 的 `contenders` 互斥保证同一 peer 的这些副作用按入队顺序逐个执行，不会并发踩踏。

#### 4.3.2 核心流程

```
sequential_work(self):    // self 被消费（take 出 endpoint、用完即弃）
  锁 buffer，取出 endpoint
  解析 TransportHeader（失败→return；也覆盖了 parallel 阶段的认证失败）

  ① 防回放：if !protector.update(f_counter) → return（重放/过旧，丢弃）

  ② 密钥确认：if !confirmed.swap(true, SeqCst) {   // 旧值 false → 首次
                    peer.confirm_key(&keypair)       // 把 next 转正
                }

  ③ 更新端点：*peer.endpoint.lock() = endpoint

  ④ 写 TUN：if let Some(inner) = inner_length(packet) {
                  if inner + SIZE_TAG <= packet.len() {
                      peer.device.inbound.write(&packet[..inner])   // 写真实 IP 包
                  }
              }   // keepalive / 畸形：inner_length 返回 None → 不写

  ⑤ 回调：C::recv(&peer.opaque, msg.1.len(), true, &keypair)
```

#### 4.3.3 源码精读

**串行入口与缓冲区解析**——`is_ready` 用 `Acquire` 序与 `parallel_work` 的 `Release` 配对：

[receive.rs:127-155](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/receive.rs#L127-L155)：

```rust
fn is_ready(&self) -> bool {
    self.0.ready.load(Ordering::Acquire)
}
fn sequential_work(self) {
    debug_assert_eq!(self.is_ready(), true, "doing sequential work on an incomplete job");
    let job = &self.0;
    let peer = &job.state.peer;
    let mut msg = job.buffer.lock();
    let endpoint = msg.0.take();
    // 解析传输头；失败直接返回（也覆盖了认证失败：缓冲区被截断到 0，解析不出 16 字节头）
    let (header, packet) = match LayoutVerified::new_from_prefix(&msg.1[..]) {
        Some(v) => v,
        None => return,
    };
    ...
}
```

> 注意注释 L152：`// also covers authentication failure (will fail to parse header)`。这正是 4.2 失败截断的下半句——截断到 0 字节后，这里 `new_from_prefix` 拿不到 16 字节的 `TransportHeader`，返回 `None`，整条报文被安静丢弃，未认证字节无路可走。

**① 防回放**——`AntiReplay::update`，必须串行：

[receive.rs:157-161](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/receive.rs#L157-L161)：

```rust
if !job.state.protector.lock().update(header.f_counter.get()) {
    log::debug!("inbound worker: replay detected");
    return;
}
```

`protector` 是 `Mutex<AntiReplay>`，`update` 是「检查 + 标记」的复合读改写操作（[anti_replay.rs:103-110](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/anti_replay.rs#L103-L110)）。即便有 `Mutex` 保护，它仍**必须在串行阶段**调用——原因见下文「为什么防回放必须在串行阶段」。

**② 密钥确认**——`swap` 是一次性触发器，只有首个报文转正密钥：

[receive.rs:163-167](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/receive.rs#L163-L167)：

```rust
if !job.state.confirmed.swap(true, Ordering::SeqCst) {
    log::debug!("inbound worker: message confirms key");
    peer.confirm_key(&job.state.keypair);
}
```

`confirmed` 的初值是 `keypair.initiator`（见 [peer.rs:136](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L136)，`DecryptionState::new`）：

- **响应方（non-initiator）**的密钥 `initiator=false`，故 `confirmed` 初值 `false`。首个报文 `swap(true)` 返回旧值 `false`，进入 `if`，调 `confirm_key`，把 `next` 转正为 `current`。后续报文 `swap` 返回 `true`，不再重复确认。
- **发起方（initiator）**的密钥 `initiator=true`，`confirmed` 初值 `true`，`swap(true)` 直接返回 `true`，永不进 `if`——发起方的密钥在它收到响应报文（握手阶段）时就被视为已确认。

`confirm_key` 的实现在 [peer.rs:316-349](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L316-L349)：校验 `keypair == keys.next` 后，用三轮 `mem::swap` 旋转 KeyWheel（`next→current→previous`），新建 `EncryptionState` 写入 `enc_key`，回调 `C::key_confirmed`，并补发暂存包 `send_staged`。

> 这就是 WireGuard 的「**静默确认**」：响应方并不靠额外的握手报文确认密钥，而是**直到收到对方用该密钥加密的第一个数据报文**，才认定密钥已安全送达，进而开始用它加密发送。详见 [u5-l6](./u5-l6-keywheel-peer.md)。

**③④ 写入 TUN**——用 `inner_length` 取真实 IP 长度，截掉标签与填充：

[receive.rs:169-180](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/receive.rs#L169-L180)：

```rust
// 更新端点
*peer.endpoint.lock() = endpoint;
// 判断是否应写入 TUN（keepalive 与畸形包没有 inner length）
if let Some(inner) = inner_length(packet) {
    if inner + SIZE_TAG <= packet.len() {
        let _ = peer.device.inbound.write(&packet[..inner]).map_err(|e| {
            log::debug!("failed to write inbound packet to TUN: {:?}", e);
        });
    }
}
```

`peer.device.inbound` 就是 TUN 的 `Writer`（见 [device.rs:27](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/device.rs#L27)），写进去的 `&packet[..inner]` 是剥掉认证标签和发送端填充后的**真实 IP 包**，内核网络栈随后就能像处理普通网卡收到的包一样处理它。

> 端点更新 `*peer.endpoint.lock() = endpoint`（L170）是 WireGuard **无缝漫游**的基础：对端换了 IP（比如手机切到 4G），下一个报文从新地址到达，这里就把 peer 的端点刷新为新地址，后续出站报文自动发往新地址。

**⑤ 触发回调**——`C::recv`，即使 keepalive 也会触发：

[receive.rs:182-184](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/receive.rs#L182-L184)：

```rust
C::recv(&peer.opaque, msg.1.len(), true, &job.state.keypair);
```

回调签名见 [types.rs:29-35](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/types.rs#L29-L35)：`recv(opaque, size, sent, keypair)`。上层（[timers.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/timers.rs)，见 [u7-l1](./u7-l1-timers-state-machine.md)）用它做入站字节统计（`msg.1.len()` 含头与标签）并驱动 keepalive / 重握手定时器。注意这一行在 `if let Some(inner)` 之外，所以 **keepalive 也会触发回调**——这正是 keepalive 能「保活」的机制：它虽不写 TUN，却刷新了端点、确认了密钥、喂了字节计数与定时器。

#### 4.3.4 代码实践

**实践目标**：动手解释「为什么 `check_route` 放并行、`AntiReplay::update` 放串行」这一本讲核心问题。

**操作步骤**：

1. 阅读并对照以下两段代码：
   - `check_route`（[route.rs:116-138](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/route.rs#L116-L138)）：只取 `self.ipv4.read()` / `self.ipv6.read()`，**只读**。
   - `AntiReplay::update` → `update_store`（[anti_replay.rs:67-91](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/anti_replay.rs#L67-L91)）：会改写 `self.last` 与 `self.bitmap`，是**读改写**。
2. 把你的解释写成一段注释，贴在 `parallel_work` 上方注释（[receive.rs:55-65](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/receive.rs#L55-L65)）旁边作为学习笔记（**不要提交到源码，仅本地理解**）。

**需要观察的现象 / 预期结果**：你的解释应包含以下三点（参考答案见 4.3.5）：

- `check_route` 无副作用、只读路由表，并发安全，且能在昂贵的串行槽之前就剔除伪造报文，故放并行。
- `update` 修改共享位图，存在数据竞争风险。
- 更关键的是：`update` 对**处理顺序**敏感，并行阶段线程调度会让序号「乱序到达」，可能导致合法报文被错误地滑出窗口而误丢（这正是源码注释 L63-L65 的原话）。

> 待本地验证：若想直观验证「顺序敏感」，可阅读 [anti_replay.rs 自带测试](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/anti_replay.rs#L117-L156)，观察 `update(65536)` 后窗口如何整体前移——若这一步与对旧序号的 `update` 并发执行，旧序号就会被「误伤」。

#### 4.3.5 小练习与答案

**练习 1（本讲核心问题）**：用一句话说清「为什么 cryptokey 路由校验在并行阶段、防回放在串行阶段」。

**参考答案**：`check_route` 是**无副作用的只读**查询（只持路由表读锁），可安全并行，还能在占用串行槽之前提前剔除伪造报文；而 `AntiReplay::update` 是**修改共享位图、且对处理顺序敏感**的读改写操作，若并行执行，线程调度造成的乱序会让本应落在窗口内的合法报文被错误地「滑出窗口」而丢弃（见源码注释 [receive.rs:63-65](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/receive.rs#L63-L65)），故必须放到 per-peer 串行阶段。

**练习 2**：响应方收到第 1 个传输报文时，`confirmed.swap(true, SeqCst)` 的返回值是什么？收到第 2 个时呢？

**参考答案**：响应方密钥 `initiator=false`，`confirmed` 初值 `false`。第 1 个报文：`swap(true)` 返回旧值 `false`，进入 `if` 执行 `confirm_key`（密钥转正）。第 2 个报文：`swap(true)` 返回旧值 `true`（已被第 1 个置位），不进 `if`，不再重复确认——`swap` 充当了一次性触发器。

**练习 3**：为什么 `C::recv` 回调放在 `if let Some(inner)` 之外（L183），而不是之内？

**参考答案**：因为 keepalive 的 `inner_length` 为 `None`（见 4.4），不会进 `if`、不写 TUN；但 keepalive 仍需触发回调，好让上层刷新字节计数与 keepalive / 重握手定时器（即「保活」语义）。把回调放在 `if` 之外，保证**所有**通过认证与防回放的报文（含 keepalive）都通知上层。

---

### 4.4 keepalive 识别与缓冲区截断：两条安全边界

#### 4.4.1 概念说明

本节聚焦两个容易混淆但很重要的细节：

- **keepalive 报文如何被识别并在写 TUN 前被丢弃**：keepalive 是个「空载荷」报文，目的是让对端确信链路活着。它在接收管道里要走完全流程（解密、防回放、密钥确认、回调），但**唯独不写进 TUN**——因为它的明文载荷是空的，根本没有 IP 包。
- **认证失败时截断缓冲区到 0 字节**：这是一道纵深防御，确保未认证数据在任何情况下都不会被误用。

#### 4.4.2 核心流程

**keepalive 的产生**：`send_keepalive` 发送的就是 `vec![0u8; SIZE_MESSAGE_PREFIX]`，即 16 字节，刚好等于传输头大小，载荷为空（见 [peer.rs:500-503](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L500-L503)）。加密后变成「16 头 + 16 标签」，明文为 0 字节。

**keepalive 的识别**：在 `sequential_work` 里，`inner_length(packet)` 会尝试读明文首字节的 IP 版本号。keepalive 的明文为空，`packet.get(0)` 为 `None`，`inner_length` 直接返回 `None`，于是 `if let Some(inner)` 不命中，**不写 TUN**。

#### 4.4.3 源码精读

**`inner_length`——从明文判定真实 IP 长度，keepalive/畸形返回 `None`**：

[ip.rs:31-49](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/ip.rs#L31-L49)：

```rust
pub fn inner_length(packet: &[u8]) -> Option<usize> {
    match packet.get(0)? >> 4 {           // 取首字节高 4 位 = IP 版本；空包 → None
        VERSION_IP4 => {
            let (header, _): (LayoutVerified<&[u8], IPv4Header>, _) =
                LayoutVerified::new_from_prefix(packet)?;
            Some(header.f_total_len.get() as usize)   // IPv4：total_len 字段
        }
        VERSION_IP6 => {
            let (header, _): (LayoutVerified<&[u8], IPv6Header>, _) =
                LayoutVerified::new_from_prefix(packet)?;
            Some(header.f_len.get() as usize + mem::size_of::<IPv6Header>()) // IPv6：载荷长+固定头
        }
        _ => None,                         // 非 4/6 → None（畸形包）
    }
}
```

> 关键点：`packet.get(0)?` 的 `?`——当 `packet` 为空（keepalive 的明文就是空），`get(0)` 返回 `None`，`?` 提前返回 `None`。所以 keepalive 在这里被精确识别。`f_total_len`（IPv4）/ `f_len`（IPv6）是发送方在 IP 头里声明的真实长度，与发送端的 padding 对齐（见 [u3-l2](./u3-l2-tun-worker.md) 的 `padding`）配合：解密后用它把「对齐填充」和「认证标签」一起剥掉，只写 `&packet[..inner]` 这段真实 IP 包。

**双层长度校验**：在 `sequential_work` 里还有一道 `inner + SIZE_TAG <= packet.len()`（[receive.rs:175](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/receive.rs#L175)）。即使首字节碰巧被认成 IP 版本，只要声明长度 `inner` 加上标签超过实际缓冲，也不会越界写入——这是一道防「畸形/截断报文声称超大 total_len」的护栏。

**失败截断的纵深防御**：回到 [receive.rs:115-119](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/receive.rs#L115-L119)，注释写得很明白：

```rust
// remove message in case of failure:
// to indicate failure and avoid later accidental use of unauthenticated data.
if !ok {
    msg.1.truncate(0);
}
```

「截断以标记失败，并避免后续误用未认证数据」。它与串行阶段的解析失败提前返回（[receive.rs:151-154](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/receive.rs#L151-L154)）形成**双层保险**：即使有人将来改动代码，只要 `parallel_work` 仍截断，未认证字节就无路可走。

#### 4.4.4 代码实践

**实践目标**：把「keepalive 在写 TUN 前被丢弃」这一行为在源码里走通。

**操作步骤**：

1. 先看 keepalive 的发送端：[peer.rs:500-503](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L500-L503)，确认 `send_keepalive` 发的是 `vec![0u8; SIZE_MESSAGE_PREFIX]`，明文载荷为空。
2. 追踪它在接收端的两处落点：
   - `parallel_work` 里 `packet.len() == SIZE_TAG` 放行（[receive.rs:112](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/receive.rs#L112)）——keepalive 解密后明文为 0，`packet` 恰为 16 字节标签，匹配此条件，跳过 `check_route`。
   - `sequential_work` 里 `inner_length(packet)` 返回 `None`（[ip.rs:33](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/ip.rs#L33) 的 `packet.get(0)?`）——`if let Some(inner)` 不命中，**不写 TUN**。
3. 写一句话总结：keepalive **走完认证与防回放、触发回调与密钥确认，但不进 TUN**。

**需要观察的现象**：keepalive 在两个阶段的「身份」不同——在并行阶段靠「长度恰为 SIZE_TAG」被识别并放行；在串行阶段靠「`inner_length` 为 None」被拦在 TUN 门外。

**预期结果**：你能清楚说出 keepalive 为何「保活但不打扰内核网络栈」。

> 待本地验证：可参考 [u7-l4](./u7-l4-testing-strategy.md) 提到的 `test_pure_wireguard`，用 dummy 平台构造两个互连实例，让一方发 keepalive，观察对方 TUN 写入计数是否为 0 而 rx 字节计数是否增加。

#### 4.4.5 小练习与答案

**练习 1**：keepalive 在 `parallel_work` 中靠什么被识别？在 `sequential_work` 中又靠什么被拦在 TUN 之外？

**参考答案**：`parallel_work` 中靠 `packet.len() == SIZE_TAG`（解密后明文为空、只剩 16 字节标签，[receive.rs:112](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/receive.rs#L112)）识别并跳过路由校验；`sequential_work` 中靠 `inner_length(packet)` 对空明文返回 `None`（[ip.rs:33](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/ip.rs#L33)），使 `if let Some(inner)` 不命中而不写 TUN。

**练习 2**：假如某天有人把 `parallel_work` 末尾的 `truncate(0)` 删掉，串行阶段还会写出未认证数据吗？为什么仍说截断是「双层保险」？

**参考答案**：单看这条路径，串行阶段的 `AntiReplay::update` 在认证失败的报文上行为未定义（因为 `open_in_place` 失败时明文不可信），且 `inner_length` / `check_route` 都依赖可信明文。截断保证了「认证失败的报文进串行阶段时连 16 字节头都解析不出来，必然提前 return」。删掉截断后，串行阶段虽仍可能因别处检查拦截，但失去了「明确标记失败」的语义，未认证字节存在被后续逻辑误用的风险。所以截断是**主动、确定**的第一道闸，串行的解析失败是**被动**的第二道闸，合称双层保险。

---

## 5. 综合实践

**综合任务**：画一张「入站报文生命周期」流程图，并把本讲全部知识点串到这张图上。

要求在图中至少标注以下要素，并配上对应的源码行号链接：

1. **入口**：`udp_worker` → `Device::recv`，按 `f_receiver` 查 `recv` 表（[device.rs:236-239](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/device.rs#L236-L239)）。
2. **两阶段分界**：`parallel_work` 与 `sequential_work`，中间隔着 `ready` 的 `Release`/`Acquire` 与 `Queue::consume` 的 `contenders` 互斥。
3. **并行阶段的 4 道关卡**：`LayoutVerified` 解析头 → `open_in_place` 解密 → counter 上限 → `check_route`（或 keepalive 放行），失败统一 `truncate(0)`。
4. **串行阶段的 5 个动作**：防回放 `update` → 密钥确认 `confirm_key`（仅首次）→ 更新端点 → `inner_length` 判定后写 TUN → `C::recv` 回调。
5. 用**不同颜色/标记**标出两条特殊路径：
   - **keepalive**：并行阶段命中 `packet.len() == SIZE_TAG`、串行阶段 `inner_length=None`，最终**不写 TUN 但触发回调**。
   - **认证失败 / 路由失败**：并行阶段 `truncate(0)`、串行阶段解析头失败提前 `return`，**全程无副作用**。

完成后，对照本讲 4.3.5 练习 1 的参考答案，在你的图旁写一段话，回答本讲的核心问题：「为什么 `check_route` 在并行、`AntiReplay::update` 在串行？」

> 提示：可以用 Mermaid 的 `flowchart` 或手绘。重点是**讲清「无副作用只读 → 并行；有副作用且顺序敏感 → 串行」**这条判断准则，而不是图的精美程度。

---

## 6. 本讲小结

- **两阶段分工**：`ReceiveJob` 把入站处理拆成并行 `parallel_work`（解密 + cryptokey 路由校验）与串行 `sequential_work`（防回放 + 密钥确认 + 更新端点 + 写 TUN + 回调）。
- **并行的判据是无副作用**：AEAD 解密虽重，但是纯计算；`check_route` 只持路由表读锁。二者并发安全，故放并行，还能提前剔除伪造报文、不占串行槽。
- **串行的判据是顺序敏感**：`AntiReplay::update` 修改共享位图且对处理顺序敏感，并行会让合法报文因调度乱序被错误地滑出窗口（源码注释原话），故必须靠 `Queue` 的 per-peer 串行保证。
- **失败截断是纵深防御**：认证或路由失败时 `truncate(0)`，使串行阶段解析头失败而提前返回，杜绝未认证字节流向 TUN 或回调。
- **首个报文静默确认密钥**：响应方密钥 `confirmed` 初值 `false`，首个成功报文经 `swap` 触发一次 `confirm_key`，把 `next` 转正为 `current`——这就是 WireGuard 的密钥静默确认。
- **keepalive 保活但不入 TUN**：并行阶段靠 `packet.len() == SIZE_TAG` 放行，串行阶段靠 `inner_length` 返回 `None` 拦在 TUN 门外，但仍触发回调驱动定时器。

---

## 7. 下一步学习建议

- **[u5-l4 有序队列 Queue](./u5-l4-ordered-queue.md)**：本讲反复提到的 `Queue::consume` 与 `contenders` 互斥在那里有完整拆解，建议紧接着读，彻底搞懂「并行加密/解密、有序副作用」的实现细节。
- **[u5-l6 密钥轮转 KeyWheel 与 Peer 生命周期](./u5-l6-keywheel-peer.md)**：本讲的 `confirm_key` 只是 KeyWheel 转正的入口，完整的 `next/current/previous` 三密钥轮转、`staged_packets` 暂存、`add_keypair` 的 initiator 与非 initiator 分支都在这一讲。
- **[u5-l7 防回放窗口（RFC 6479）](./u5-l7-anti-replay.md)**：本讲把 `AntiReplay::update` 当黑盒，它的 2048 位滑动位图、窗口大小推导、位运算细节在那里专门讲解。
- **[u7-l1 定时器状态机与 Callbacks](./u7-l1-timers-state-machine.md)**：本讲的 `C::recv` 回调最终驱动哪些定时器（keepalive、重握手、密钥清零），在那里闭环。
