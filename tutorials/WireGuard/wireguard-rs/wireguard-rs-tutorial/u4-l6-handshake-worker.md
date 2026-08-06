# 握手工作线程：驱动状态机

## 1. 本讲目标

本讲精读 `src/wireguard/workers.rs` 中的 `handshake_worker`——它是握手状态机的「驱动引擎」：从握手队列里取出任务，决定是否进入抗 DoS 的 under-load 模式，调用握手 `Device` 的 `process`/`begin` 完成密码学计算，再把派生出的会话密钥交给路由器 peer、回收旧的 receiver id，并触发对应的定时器回调。

学完后你应该能够：

- 说清 `HandshakeJob::Message` 与 `HandshakeJob::New` 两种任务分别由谁产生、如何被消费。
- 掌握 under-load 的两段判定（阈值 + 1 秒迟滞）以及它如何影响 cookie/限速校验。
- 画出一次完整握手（initiation → response → 首个数据包确认密钥）在 `handshake_worker` 各分支中的触发顺序。
- 准确指出「已确认（confirmed）密钥」与「未确认（unconfirmed）密钥」分别在哪一步、由哪一端产生，以及它们如何进入路由器的 KeyWheel。

## 2. 前置知识

在进入本讲前，你需要已经建立以下认知（来自前置讲义）：

- **握手与数据面分离**：`handshake::Device` 负责用 Noise IK 协商对称会话密钥，`router::Device` 负责用该密钥做传输报文的加解密与 cryptokey 路由。`handshake_worker` 是把这两者连起来的桥梁之一。
- **HandshakeJob 队列**：`WireGuard::new` 按 CPU 核数创建一个 `ParallelQueue<HandshakeJob>`（单发送端、多接收端、容量 128），并 spawn 等量的 `handshake_worker` 作为消费者。`udp_worker` 把入站握手报文投递进来（见 u3-l3）。
- **receiver id**：握手阶段每端会分配一个 4 字节的临时标识，放在握手报文的 `f_sender`/`f_receiver` 字段里，用于在 `id_map` 中回溯到 peer。它生命周期短、可回收（见 u4-l2）。
- **KeyPair 的 `initiator` 字段**：`KeyPair.initiator` 并非「是否发起了握手」，而是「该密钥是否已被确认」。这个布尔位决定了密钥进入路由器 KeyWheel 的 `current`（立即可加密）还是 `next`（等待首个报文确认）。

本讲不重新讲解 Noise 密码学细节（见 u4-l3）和 MAC/Cookie/限速的内部实现（见 u4-l4），只关注 `handshake_worker` 如何调度它们。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/wireguard/workers.rs` | 三类工作线程的所在地；本讲的主角 `handshake_worker` 与 `HandshakeJob` 枚举都在这里 |
| `src/wireguard/handshake/device.rs` | `Device::process`（处理入站握手）与 `Device::begin`（发起本地握手）、`Device::release`（回收 id） |
| `src/wireguard/handshake/types.rs` | `Output<'a, O>` 三元组与 `HandshakeError` 定义 |
| `src/wireguard/handshake/noise.rs` | `create_response`/`consume_response` 派生 `KeyPair`，决定 `initiator` 标志 |
| `src/wireguard/peer.rs` | 上层 `PeerInner`，提供 `packet_send_handshake_initiation`（投递 `New` 任务）与定时器回调方法 |
| `src/wireguard/router/peer.rs` | 路由器 peer 的 `add_keypair`（KeyWheel 轮转）、`send_raw`、`set_endpoint`、`confirm_key` |
| `src/wireguard/timers.rs` | `sent_handshake_initiation`/`sent_handshake_response`/`timers_handshake_complete`/`timers_session_derived` 等回调钩子 |
| `src/wireguard/constants.rs` | `THRESHOLD_UNDER_LOAD`、`DURATION_UNDER_LOAD`、`MAX_QUEUED_INCOMING_HANDSHAKES` 等常量 |
| `src/wireguard/types.rs` | `KeyPair` 结构定义 |

## 4. 核心概念与源码讲解

### 4.1 HandshakeJob 枚举：握手队列里的两种任务

#### 4.1.1 概念说明

`handshake_worker` 是一个「消费者」：它在一个 `crossbeam` 通道的接收端上阻塞，有任务就处理，没有就等。那么「生产者」是谁、产出的又是什么？答案就是 `HandshakeJob`——握手队列里流转的任务只有两种：

- `HandshakeJob::Message(Vec<u8>, E)`：一**条入站握手报文**，附带发送方端点 `E`（平台的 `Endpoint` 类型）。它由 `udp_worker` 在收到 `TYPE_INITIATION/RESPONSE/COOKIE_REPLY` 报文时投递。
- `HandshakeJob::New(PublicKey)`：一个**本地主动发起握手**的请求，只携带目标 peer 的公钥。它在本端没有可用密钥、需要主动发起握手时投递。

把这两种任务放进同一个队列，是因为它们都要占用稀缺的「握手计算」资源，需要串行化/并行化地交给同一个 worker 池处理。

#### 4.1.2 核心流程

```text
生产者                                  消费者
─────────────────────────              ─────────────────────────
udp_worker                              handshake_worker × N
  收到 TYPE_INITIATION/RESPONSE/           for job in rx {
  COOKIE_REPLY 报文                          处理 Message 或 New
  → queue.send(Message(msg, src))          }
  → pending.fetch_add(1)

PeerInner::packet_send_handshake_initiation
  （由 need_key / 定时器触发，带速率限制）
  → queue.send(New(pk))
  → pending.fetch_add(1)
```

两个生产者都在投递前对 `wg.pending`（一个 `AtomicUsize`）做 `fetch_add(1)`，这是 under-load 判定的输入（见 4.2）。

#### 4.1.3 源码精读

枚举定义本身非常朴素：

[src/wireguard/workers.rs:30-33](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/workers.rs#L30-L33) 定义了 `Message` 与 `New` 两个变体，`Message` 持有报文字节和对端端点，`New` 只持有公钥。

`udp_worker` 作为 `Message` 的生产者，在分用出握手报文后投递：

[src/wireguard/workers.rs:129-134](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/workers.rs#L129-L134) 先 `pending.fetch_add(1)` 再 `queue.send(HandshakeJob::Message(msg, src))`。注意是「先计数后投递」，这样 worker 取出任务时 `pending` 一定已经反映了这条报文。

`New` 的生产者在 `PeerInner::packet_send_handshake_initiation`：

[src/wireguard/peer.rs:47-74](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/peer.rs#L47-L74)。这段代码有三个值得注意的设计：

1. **速率限制**（L51-58）：用 `last_handshake_sent` 锁住，若距上次发起不足 `REKEY_TIMEOUT`（5 秒）就直接返回，避免疯狂重握手。
2. **去重**（L61）：`handshake_queued.swap(true, SeqCst)` 是一个「test-and-set」——只有当原来是 `false` 时才返回 `false`（表示「之前没在排队，这次由我来投递」），从而保证**同一 peer 同时最多只有一个 `New` 任务在队列里**。
3. **计数**（L62-63）：与 `udp_worker` 一样，先 `pending.fetch_add(1)` 再 `send`。

> 这个 `handshake_queued` 标志在 worker 处理完 `New` 任务后会被复位为 `false`（见 4.4.3），形成一个完整的「置位→处理→复位」循环。

#### 4.1.4 代码实践

**实践目标**：追踪 `HandshakeJob` 的全部生产点与消费点，确认「每个 job 都恰好配对一次 `pending` 加减」。

**操作步骤**：

1. 在 [src/wireguard/workers.rs:30-33](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/workers.rs#L30-L33) 处确认枚举的两个变体。
2. 用编辑器全局搜索 `queue.send` 与 `HandshakeJob::`，定位两个生产点：`udp_worker`（workers.rs:133）与 `packet_send_handshake_initiation`（peer.rs:63）。
3. 用全局搜索 `for job in rx`，定位唯一消费点 `handshake_worker`（workers.rs:155）。

**需要观察的现象**：两个生产点都成对出现 `pending.fetch_add(1, ...)` 与 `queue.send(...)`；消费点 `handshake_worker` 开头有对应的 `pending.fetch_sub(1, ...)`（见 4.2.3）。

**预期结果**：你能列出一张表，证明 `pending` 的增减是对称的——每投递一个 job 加 1，每消费一个 job 减 1。**待本地验证**：可在每个加减处临时加一行 `eprintln!("pending {:?}", wg.pending.load(Ordering::Relaxed))` 观察计数变化。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `packet_send_handshake_initiation` 要用 `swap(true, ...)` 而不是先 `load` 再 `store`？

**参考答案**：先 `load` 后 `store` 是「检查再设置」的两步操作，两个线程可能同时读到 `false`、然后都投递一个 `New`，导致同一 peer 在队列里出现重复任务。`swap` 是原子的 test-and-set，保证「置位」与「判定之前是否已置位」不可分割，从而做到去重。

**练习 2**：`handshake_queued` 标志如果忘了在 worker 里复位，会出现什么后果？

**参考答案**：该 peer 在第一次握手后将永远无法再发起任何新握手——`swap(true,...)` 永远返回 `true`，`packet_send_handshake_initiation` 永远走 `else` 分支直接返回，密钥过期后无法重协商。

---

### 4.2 under-load 判定：何时启动 cookie 抗 DoS

#### 4.2.1 概念说明

WireGuard 在昂贵的 Noise 握手之前有一套廉价的两层防御：mac1（始终校验）和 mac2+限速（仅过载时校验）。这套机制何时启用，由 `handshake_worker` 在每条任务开头判定——即 **under-load（过载）状态**。判定的唯一输入是 `wg.pending`：当前队列里还没被消费的握手任务数量。

under-load 的核心思想是：正常情况下握手任务很少、几乎一进队就被消费；一旦堆积超过阈值，说明对端可能在用伪造源地址洪水攻击，于是启动更严格的 cookie 校验与限速。

#### 4.2.2 核心流程

`handshake_worker` 取出任务后，先 `fetch_sub(1)` 把自己这条从计数里扣掉（拿到的是扣减**前**的旧值），然后用**两段判定**决定是否过载：

1. **阈值判定**：若旧值 `pending > THRESHOLD_UNDER_LOAD`，立即判定过载，并刷新 `last_under_load` 时间戳为「现在」。
2. **迟滞判定**（hysteresis）：若上一条没触发，再看「距上次进入过载是否还未超过 `DURATION_UNDER_LOAD`（1 秒）」。若是，则保持过载状态——这就是「一旦进入过载，至少维持 1 秒」的迟滞，避免负载在阈值附近抖动导致抗 DoS 逻辑频繁开关。

判定结果 `under_load` 是一个布尔值，决定了传给 `device.process` 的第三个参数 `src`：过载时传 `Some(源地址)`，触发 mac2/cookie/限速；不过载时传 `None`，只做 mac1。

#### 4.2.3 源码精读

常量定义在 `constants.rs`：

[src/wireguard/constants.rs:19-29](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/constants.rs#L19-L29)。其中 `MAX_QUEUED_INCOMING_HANDSHAKES = 4096` 是队列总容量上限；`THRESHOLD_UNDER_LOAD = 4096/8 = 512`，即堆积到 512 条就算过载；`DURATION_UNDER_LOAD = 1s` 是迟滞窗口。

worker 里的两段判定：

[src/wireguard/workers.rs:156-176](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/workers.rs#L156-L176)。逐行解读：

- L159：`fetch_sub(1, SeqCst)` 返回扣减前的旧值 `pending`。
- L160：`debug_assert!(pending < MAX_QUEUED_INCOMING_HANDSHAKES + (1<<16))`——一个宽松的合理性检查，确保计数没有因为 bug 飙到离谱的值。
- L163-167：**第一段**，阈值判定。注意触发后会 `*wg.last_under_load.lock() = Instant::now()`，这同时为第二段的迟滞「种下」时间起点。
- L170-176：**第二段**，迟滞判定。`DURATION_UNDER_LOAD >= elapsed` 用「>=」表示「只要还没满 1 秒，就继续算过载」。

> 为什么把 `last_under_load` 初始化为「很久以前」？在 [src/wireguard/wireguard.rs:286](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/wireguard.rs#L286) 里它被设成 `Instant::now() - TIME_HORIZON`，`elapsed()` 会得到一个极大值，远大于 1 秒，因此设备刚启动、从未过载时第二段判定自然为假——避免了用 `Option<Instant>` 的繁琐。

最终 `under_load` 被翻译成 `src` 参数：

[src/wireguard/workers.rs:183-191](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/workers.rs#L183-L191) 在调用 `device.process` 时，`if under_load { Some(src.into_address()) } else { None }`。`device.process` 内部只有当 `src` 为 `Some` 时才校验 mac2、在 mac2 失败时回 CookieReply、并查询令牌桶限速器（详见 u4-l4）。

#### 4.2.4 代码实践

**实践目标**：理解阈值与迟滞窗口的数值含义，并验证「迟滞」对抖动的影响。

**操作步骤**：

1. 在 [src/wireguard/constants.rs:19-29](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/constants.rs#L19-L29) 中确认 `THRESHOLD_UNDER_LOAD = 512`、`DURATION_UNDER_LOAD = 1s`。
2. 阅读源码回答：假设 `pending` 在第 0 ms 为 600、第 100 ms 降到 100、第 500 ms 又升到 600，问第 300 ms 处理任务时 `under_load` 是什么？
3. （可选，**待本地验证**）把 `THRESHOLD_UNDER_LOAD` 临时改成 `1`，重新编译跑握手测试，观察是否会因为阈值过低而频繁触发 cookie 回复。

**预期结果**：第 2 步——第 0 ms 因 600>512 进入过载并刷新 `last_under_load`；第 300 ms 时 `elapsed=300ms < 1s`，即便此刻 `pending` 已降到 100，第二段迟滞仍判 `under_load=true`。这正说明迟滞让过载状态「粘住」了 1 秒。

#### 4.2.5 小练习与答案

**练习 1**：为什么用 `fetch_sub` 返回的「旧值」而不是 `fetch_sub` 之后再 `load` 来做阈值比较？

**参考答案**：`fetch_sub` 后再 `load` 读到的是「扣减后的值」，少了当前这条任务，且与投递时的计数语义不一致。用旧值（扣减前）才准确反映「这条任务入队时队列的拥挤程度」——这正是 under-load 判定想衡量的量。

**练习 2**：迟滞窗口 `DURATION_UNDER_LOAD` 设得过大（比如 60 秒）会有什么副作用？

**参考答案**：设备一旦遭遇短时洪峰，会在随后整整 60 秒内对所有握手（包括合法对端）都强制 mac2+限速校验；若合法对端尚未拿到 cookie，其握手会被持续拒绝或回 CookieReply，导致正常连接长时间建不起来。所以 1 秒是一个在「平滑抖动」与「快速恢复」之间的折中。

---

### 4.3 Message 分支：device.process 处理入站握手

#### 4.3.1 概念说明

当 worker 取出的是 `HandshakeJob::Message`，意味着收到了一条对端发来的握手报文。worker 自己不做任何密码学，而是把报文连同（可能的）源地址交给 `handshake::Device::process`。`process` 会按报文首 4 字节的类型字段把任务分用到对应的 Noise 函数，并把结果打包成一个三元组 `Output` 返回。

#### 4.3.2 核心流程

`Output<'a, O>` 的定义在 [src/wireguard/handshake/types.rs:82-86](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/types.rs#L82-L86)，三个分量分别是：

1. `Option<&'a O>`——认证成功的 peer 的不透明引用（`O` 就是路由器的 `PeerHandle`）；认证失败或只是 cookie 回复时为 `None`。
2. `Option<Vec<u8>>`——需要回发的报文（可能是握手 Response、CookieReply，也可能没有）。
3. `Option<KeyPair>`——握手成功派生出的新会话密钥。

`process` 对三类报文的产出可以总结成下表：

| 报文类型 | 处理者 | peer | resp | keypair | 说明 |
| --- | --- | --- | --- | --- | --- |
| Initiation | `consume_initiation`+`create_response` | Some | Some(Response) | Some(未确认) | 响应方收到发起，回 Response 并得到未确认密钥 |
| Response | `consume_response` | Some | None | Some(已确认) | 发起方收到响应，得到已确认密钥，无需回报文 |
| CookieReply | `lookup_id`+`process cookie` | None | None | None | 只更新本端 cookie，不产生密钥、不认证对端 |
| mac2 失败/限速 | （提前返回） | None | Some(CookieReply) | None | 索要 cookie，不做 DH |

#### 4.3.3 源码精读

worker 的 `Message` 分支：

[src/wireguard/workers.rs:180-249](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/workers.rs#L180-L249)。它先取一次 `wg.peers.read()` 拿到 `Device` 的读锁，然后用 `device.process(...)` 做密码学处理，最后用返回的三元组更新 peer 状态。先看 process 调用与回发：

[src/wireguard/workers.rs:183-204](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/workers.rs#L183-L204)。注意 L198 的 `wg.router.send_raw(&msg[..], &mut src)`——回复报文不经过路由器的加密管道，而是用路由器持有的 UDP writer 直接「裸发」。它取 `&mut src` 是因为 Linux 的 sticky socket 在源粘连失效时可能需要 `clear_src` 重发（见 u2-l3）。

`device.process` 的内部分用：

[src/wireguard/handshake/device.rs:308-431](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/device.rs#L308-L431)。它按 `LittleEndian::read_u32(msg)` 把报文分到 `TYPE_INITIATION`（L330）、`TYPE_RESPONSE`（L386）、`TYPE_COOKIE_REPLY`（L416）三个分支。每个分支在 `src` 为 `Some` 时先做 mac2/限速校验（L338-356 等），这正是 4.2 中 under-load 判定的下游消费者。以 Initiation 分支为例：

[src/wireguard/handshake/device.rs:330-384](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/device.rs#L330-L384)：先 `check_mac1`，过载时 `check_mac2`（失败则回 CookieReply 提前返回）、查限速；然后 `consume_initiation` 解密、`allocate` 分配新 id、`create_response` 生成响应与密钥，最终返回 `(Some(&peer.opaque), Some(resp), Some(keys))`——注意这里的 `keys` 来自 `create_response`，是**未确认**密钥。

> peer 引用的生命周期：`process` 返回的 `peer: Option<&'a O>` 借用了 `device`（即 `wg.peers` 的读锁），所以 worker 必须在同一个作用域内、读锁释放之前完成对 `peer` 的所有使用（更新字节、设端点、加 keypair）。

#### 4.3.4 代码实践

**实践目标**：理解 `Output` 三元组在不同报文下的取值，并能预测 worker 后续行为。

**操作步骤**：

1. 阅读 [src/wireguard/handshake/types.rs:82-86](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/types.rs#L82-L86) 中 `Output` 三个分量的注释。
2. 对照 [src/wireguard/handshake/device.rs:308-431](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/device.rs#L308-L431)，逐一确认三类报文各自的 `return` 语句，填出本讲 4.3.2 的表格。
3. 重点观察 CookieReply 分支 [src/wireguard/handshake/device.rs:416-428](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/device.rs#L416-L428) 返回 `(None, None, None)`——解释这为什么不会触发 worker 里的任何「握手成功」收尾逻辑。

**预期结果**：你能准确说出「只有 Initiation 与 Response 两条路径会让 worker 走进 `if let Some(peer) = peer` 块」，因为只有它们返回了 `Some(peer)`。

#### 4.3.5 小练习与答案

**练习 1**：为什么 CookieReply 处理后 `peer` 是 `None`，即便它确实通过 `lookup_id` 找到了 peer？

**参考答案**：因为 CookieReply **不做密码学认证**——任何人只要猜对一个 receiver id 都能让设备去 `process` cookie。源码注释明确写「DOES NOT cryptographically verify the peer」（device.rs:426-427）。如果在这种情况下返回 `Some(peer)`，worker 就会更新该 peer 的字节计数与端点，等于让未认证方影响了 peer 状态，因此故意返回 `None`。

**练习 2**：worker 为什么用 `wg.router.send_raw` 而不是某个 `peer.send_*` 来回发握手响应？

**参考答案**：握手响应要在「尚不知道/尚未设置该 peer 的路由器端点」时就被发回去——它是用报文到达的源地址 `src` 直接回发的。`router.send_raw(msg, &mut src)` 走的是设备级 UDP writer、用显式目的地址发送，不依赖 peer 的 endpoint 字段；而 `peer.send_raw`（见 4.4）依赖 peer 已设置的 endpoint，更适合在 endpoint 已知后发送 Initiation。

---

### 4.4 New 分支：device.begin 发起本地握手

#### 4.4.1 概念说明

`HandshakeJob::New(pk)` 表示「本端要主动向公钥为 `pk` 的 peer 发起一次握手」。这与 `Message`（被动收到的报文）相对。它由 `need_key` 回调、定时器重试、`keep_key_fresh` 等路径触发——本质都是「路由器想发数据却没有密钥，或现有密钥即将过期，于是请求握手模块重新协商」。

#### 4.4.2 核心流程

```text
worker 取出 New(pk)
  ├─ wg.peers.read().get(&pk)  → 若 peer 不存在则什么都不做（可能刚被删除）
  ├─ device.begin(&mut OsRng, &pk)
  │     ├─ allocate 一个新 local id
  │     ├─ noise::create_initiation 构造 Initiation 字节
  │     └─ macs.generate 填充 mac1/mac2
  │     → 返回 Initiation 报文
  ├─ peer.send_raw(msg)  → 通过 peer 的 endpoint 把 Initiation 发出去
  ├─ peer.opaque().sent_handshake_initiation()  → 设置重传定时器等
  └─ peer.opaque().handshake_queued.store(false)  → 复位去重标志
```

#### 4.4.3 源码精读

worker 的 `New` 分支：

[src/wireguard/workers.rs:250-267](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/workers.rs#L250-L267)。这里有一个容易忽略的细节：代码对 `wg.peers` **取了两次读锁**。第一次是 `wg.peers.read().get(&pk)`（L251），返回的 `peer` 借自这个临时读守卫，该守卫会存活到整个 `if let` 块结束；第二次是 `let device = wg.peers.read()`（L256），仅用于 `device.begin`。由于 `spin::RwLock` 允许同一时刻多个读者，两把读锁并存不会死锁。`begin` 只用到 `device`：

[src/wireguard/handshake/device.rs:278-301](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/device.rs#L278-L301)：`allocate` 一个 fresh 的 receiver id（L287），调用 `noise::create_initiation`（L291）填充 Noise 载荷，再用 `peer.macs.generate`（L294-296）算出 mac1/mac2，最后把整个 `Initiation` 序列化成 `Vec<u8>` 返回。

发报文与回调：

[src/wireguard/workers.rs:257-265](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/workers.rs#L257-L265)。`peer.send_raw(&msg[..])` 用 peer 当前的 endpoint 发出 Initiation（注意：若 peer 还没有 endpoint，`send_raw` 会返回 `NoEndpoint` 错误并被忽略——Initiation 就发不出去，要等对端先联系过来）。发送成功后调用 `peer.opaque().sent_handshake_initiation()` 设置定时器。最后 L263-265 把 `handshake_queued` 复位为 `false`，与 4.1.3 的去重逻辑闭合。

> 这里用 `peer.send_raw`（依赖 peer endpoint）而不是 `router.send_raw`（带显式地址），是因为**主动发起握手**时本端已经知道（或学习过）对端地址；而 4.3 的回复是「收到哪来回哪」，用到达源地址 `src` 显式回发更合适。

#### 4.4.4 代码实践

**实践目标**：理解 `begin` 与「peer 不存在」的边界条件。

**操作步骤**：

1. 阅读 [src/wireguard/workers.rs:250-267](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/workers.rs#L250-L267)，注意最外层 `if let Some(peer) = ...get(&pk)`——思考：如果一个 `New(pk)` 任务在队列里排队期间，该 peer 被 `remove_peer` 删除了，会发生什么？
2. 阅读 [src/wireguard/handshake/device.rs:278-301](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/device.rs#L278-L301) 的 `begin`，确认它会因为 `(None, _)` 或 `(_, None)` 返回 `UnknownPublicKey` 错误。
3. **待本地验证**：在 `peer.send_raw` 失败的 `.map_err(...)` 分支里加一行日志，观察「peer 无 endpoint 时主动握手失败」是否真的只记录日志、不影响 `handshake_queued` 的复位。

**预期结果**：第 1 步——若 peer 已被删除，`get(&pk)` 返回 `None`，整个 `if let` 块跳过，`handshake_queued` **不会被复位**（但此时该 peer 已不存在，标志也随 `PeerInner` 一起被释放，无副作用）。第 2 步——`begin` 对未知公钥返回错误，但 worker 用 `let _ =` 忽略了它。

#### 4.4.5 小练习与答案

**练习 1**：为什么 worker 在 `begin` 之后、`send_raw` 失败时仍然要执行 `sent_handshake_initiation()` 和复位 `handshake_queued`？

**参考答案**：从代码看，`sent_handshake_initiation` 在 `map` 闭包内（只在 `begin` 成功时执行），而复位 `handshake_queued` 在闭包外（`begin` 无论成功失败都执行）。复位是为了让该 peer 后续能再次发起握手；即便本次 `send_raw` 失败（比如瞬时网络错误），也应当允许下一次重试，否则 `handshake_queued` 永远卡在 `true`。`sent_handshake_initiation` 设置重传定时器，保证即便这次发送看似成功但对端没收到，也会在 `REKEY_TIMEOUT` 后重试。

**练习 2**：`New` 分支没有调用 `device.process`，因此不会经过 under-load 判定。这意味着什么？

**参考答案**：under-load（cookie/限速）只针对**入站**报文——它防御的是「别人发给我们的洪水」。而我们**主动发起**的握手是出站行为，不涉及被攻击，因此 `begin`/`create_initiation` 路径完全不需要 mac2/限速校验。`New` 分支也确实没有读取 `under_load` 变量。

---

### 4.5 握手成功后的收尾：keypair 交付、id 释放与定时器回调

#### 4.5.1 概念说明

`device.process` 成功返回后（即 `peer` 为 `Some` 的两条路径），worker 要做四件收尾工作：统计收发字节、学习/更新对端端点、触发定时器回调、把新密钥交给路由器 peer。其中最关键、也最容易混淆的是「新密钥的确认状态」。

WireGuard 的密钥确认遵循非对称设计：

- **发起方**（发出 Initiation、收到 Response 的一方）收到 Response 后，密钥立即被确认（`initiator=true`）。因为能收到一个被正确 MAC 的 Response，本身就证明响应方算出了正确密钥。
- **响应方**（收到 Initiation、发出 Response 的一方）发出 Response 后，密钥是**未确认**的（`initiator=false`）。它的密钥要等到「收到第一个用该密钥加密的传输报文」时才被确认。

这一非对称性完全由 `KeyPair.initiator` 这个布尔位表达，并被路由器的 KeyWheel 解释为放进 `current`（立即用于加密）还是 `next`（等待确认）。

#### 4.5.2 核心流程

收尾逻辑的触发条件与动作：

```text
if let Some(peer) = peer {              // 仅 Initiation/Response 路径进入
    // ① 字节统计
    peer.opaque().rx_bytes += msg.len()
    peer.opaque().tx_bytes += resp_len

    // ② 学习端点（支持漫游）
    peer.set_endpoint(src)

    // ③ 定时器回调（二选一）
    if resp_len > 0 { sent_handshake_response() }      // 响应方
    else            { timers_handshake_complete() }     // 发起方

    // ④ 交付新密钥（若有）
    if let Some(kp) = keypair {
        timers_session_derived()                        // 启动密钥过期定时器
        for id in peer.add_keypair(kp) {                // 进入 KeyWheel，返回待释放 id
            device.release(id)                          // 在 id_map 里回收
        }
    }
}
```

`add_keypair` 的内部根据 `kp.initiator` 分流：

| `kp.initiator` | 谁会产生 | KeyWheel 动作 | 是否立即可加密 |
| --- | --- | --- | --- |
| `true`（已确认） | 发起方 `consume_response` | 写入 `enc_key`，`current = new`，旧 `current → previous` | 是，并立即发 keepalive/暂存包来「通知」对端 |
| `false`（未确认） | 响应方 `create_response` | `next = new`，旧 `next → previous` | 否，等首个传输报文到达后由 `confirm_key` 确认 |

#### 4.5.3 源码精读

worker 里的收尾主块：

[src/wireguard/workers.rs:206-244](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/workers.rs#L206-L244)。逐段对应：

- L211-215：`req_len = msg.len()`（入站报文长度）累加到 `rx_bytes`；`resp_len`（我们回发的响应长度）累加到 `tx_bytes`。注意 `resp_len` 在 L194-196 由是否 `resp.is_some()` 决定。
- L218：`peer.set_endpoint(src)`——把报文到达的源地址学习为 peer 的新端点。这是 WireGuard **无缝漫游**的实现：对端换了 IP/端口，下一个握手报文到达时端点就被更新。
- L220-231：定时器二选一。`resp_len > 0` 表示「我们发出了一个响应」——这只发生在响应方处理 Initiation 时（Response 长度 92 字节），故调 `sent_handshake_response`；`resp_len == 0` 表示「我们没有回发，只是收到了对方的 Response」——这是发起方，调 `timers_handshake_complete`。
- L234-244：密钥交付。先 `timers_session_derived()`（启动 `REJECT_AFTER_TIME*3` 的密钥清零倒计时），再 `peer.add_keypair(kp)`，把返回的待释放 id 逐个 `device.release(id)`。

`initiator` 标志由 Noise 层决定。响应方的 `create_response`：

[src/wireguard/handshake/noise.rs:473-486](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/noise.rs#L473-L486) 注释明确写「return unconfirmed key-pair」，`initiator: false`。发起方的 `consume_response`：

[src/wireguard/handshake/noise.rs:569-585](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/noise.rs#L569-L585) 注释写「return confirmed key-pair」，`initiator: true`。

`KeyPair` 结构本身：

[src/wireguard/types.rs:31-37](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/types.rs#L31-L37)，`initiator` 字段的注释是「has the key-pair been confirmed?」——再次印证它表达的是「确认状态」而非「是否发起了握手」。

KeyWheel 的分流在路由器 peer 里：

[src/wireguard/router/peer.rs:436-498](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L436-L498)。`add_keypair` 在 L446-457 分两路：`new.initiator` 为真时（L448-452）写入 `enc_key` 并把 `current` 移入 `previous`、新密钥入 `current`，随后在 L481-491 立即尝试 `send_staged` 或发 keepalive 来确认；为假时（L453-457）把 `next` 移入 `previous`、新密钥入 `next`，**等待**首个传输报文触发 `confirm_key`（[src/wireguard/router/peer.rs:316-349](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L316-L349)）。`add_keypair` 返回的 `release` 向量里收集了被挤出 KeyWheel 的旧密钥的 id（最多 3 个），交回 worker 由 `device.release` 在 `id_map` 中删除。

`device.release` 本身：

[src/wireguard/handshake/device.rs:268-271](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/device.rs#L268-L271)，从 `id_map` 删掉该 id 并断言它确实存在——这维持了「id 借出必归还」的不变量。

> 这里形成一条跨模块的所有权链：握手 `Device` 的 `id_map` 借出 id → 报文带 id 上路 → 路由器 KeyWheel 持有含 id 的密钥 → 密钥被轮转挤出 → worker 调 `release` 归还 id。任何一个环节漏掉归还，`id_map` 就会泄漏。

定时器回调钩子都定义在 `timers.rs`：

[src/wireguard/timers.rs:194-206](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/timers.rs#L194-L206) 的 `sent_handshake_initiation`/`sent_handshake_response` 更新 `last_handshake_sent` 并联动若干定时器；[src/wireguard/timers.rs:146-157](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/timers.rs#L146-L157) 的 `timers_handshake_complete` 停止重传定时器、记录 `walltime_last_handshake`（UAPI `last_handshake_time` 的来源）；[src/wireguard/timers.rs:162-168](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/timers.rs#L162-L168) 的 `timers_session_derived` 启动密钥清零倒计时。注意这些钩子都先检查 `timers.enabled`，设备 down 时是空操作。

#### 4.5.4 代码实践

**实践目标**：把「initiator 标志 → KeyWheel 分流 → confirmed/unconfirmed」这条链路在源码里走通。

**操作步骤**：

1. 从 [src/wireguard/handshake/noise.rs:473-486](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/noise.rs#L473-L486)（`initiator:false`）和 [src/wireguard/handshake/noise.rs:569-585](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/noise.rs#L569-L585)（`initiator:true`）出发，确认两个产生点。
2. 跟到 [src/wireguard/router/peer.rs:446-457](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L446-L457) 看 `if new.initiator { ... } else { ... }` 的两路。
3. 对比 [src/wireguard/router/peer.rs:316-349](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L316-L349) 的 `confirm_key`，确认响应方的 `next` 密钥是如何在首个传输报文到达时被「转正」到 `current` 的（这条路径在 router 的 receive 管道，不在 handshake_worker）。

**预期结果**：你能解释清楚「响应方的未确认密钥并不由 handshake_worker 确认，而是由 router 的 receive 管道在收到第一个 MAC 正确的传输报文时调 `confirm_key` 确认」。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `resp_len > 0` 用作区分「响应方」与「发起方」的判据？它有没有可能误判？

**参考答案**：因为 `consume_response`（发起方）返回的 resp 是 `None`，而 `create_response`（响应方）返回的 resp 是 `Some(Response)`。cookie 回复路径虽然 resp 也为 `Some`，但它返回的 peer 是 `None`，根本进不了 `if let Some(peer)` 块。所以在能进入该块的两种情况里，`resp_len > 0` 恰好等于「我是响应方」，不会误判。

**练习 2**：`add_keypair` 返回的 id 列表为什么最多 3 个？

**参考答案**：KeyWheel 最多持有 `next/current/previous` 三把密钥（见 router/peer.rs 的注释 L432-435）。每次 `add_keypair` 会把被挤出的 `previous`（或回收的 `retired`）对应的 id 收集起来归还。由于一个 peer 同时最多有 3 把密钥，单次轮转释放的 id 也就不超过 3 个。

---

## 5. 综合实践

**任务**：画一张完整的握手时序图，标注从 initiation 到 response、再到首个数据包确认密钥的过程中，**双方 `handshake_worker` 各分支的触发顺序**，并指出在哪一步产生了 confirmed 与 unconfirmed keypair。

设 A 为发起方（initiator）、B 为响应方（responder）。

```text
A (initiator)                         B (responder)                   注释
─────────────                         ─────────────                   ────
[need_key 触发]
packet_send_handshake_initiation
  → pending+1
  → queue.send(New(B_pk)) ──────────────┐
                                        │
handshake_worker: New 分支               │
  pending-1                              │
  device.begin → 构造 Initiation         │
  peer.send_raw ────── Initiation ───────┐
  sent_handshake_initiation             │ │
  handshake_queued=false                │ │
                                          │ │
                       [udp_worker] 收到 Initiation, TYPE_INITIATION
                         → pending+1 → queue.send(Message) ──┐
                                                              │
                       handshake_worker: Message 分支          │
                         pending-1                             │
                         device.process(Initiation)            │
                           → consume_initiation + create_response
                           → (peer=Some, resp=Response, keypair=未确认)
                         router.send_raw ── Response ──────────┐
                         rx_bytes+=148, tx_bytes+=92            │
                         set_endpoint(A_src)                    │
                         resp_len>0 → sent_handshake_response   │
                         timers_session_derived                 │
                         add_keypair(未确认) → next=B_new       │  ← ❶ unconfirmed 产生在 B
                           → device.release(旧id)               │
                                                              │
[udp_worker] 收到 Response, TYPE_RESPONSE                     │
  → pending+1 → queue.send(Message) ──┐                       │
                                      │                       │
handshake_worker: Message 分支         │                       │
  pending-1                            │                       │
  device.process(Response)             │                       │
    → consume_response                 │                       │
    → (peer=Some, resp=None, keypair=已确认)
  resp=None → resp_len=0               │                       │
  rx_bytes+=92, tx_bytes+=0            │                       │
  set_endpoint(B_src)                  │                       │
  resp_len==0 → timers_handshake_complete
  timers_session_derived               │                       │
  add_keypair(已确认) → current=A_new, 立即可加密              │  ← ❷ confirmed 产生在 A
    → 立即发 keepalive/staged ─────────┐  → device.release     │
                                      │                       │
                                      │  首个 transport 报文 (TYPE_TRANSPORT)
                       [udp_worker] ───┘ → router.recv (不经 handshake_worker!)
                       receive 管道 sequential_work:
                         confirm_key → B 的 next→current      │  ← ❸ B 的密钥此时才确认
                         key_confirmed → timers_handshake_complete
```

**关键结论（请在图中标注并写进你的笔记）**：

1. **unconfirmed keypair** 在第 ❶ 步产生，位于**响应方 B**，由 `create_response`（`initiator=false`）产出，进入 B 的 `next` 槽，等待确认。
2. **confirmed keypair** 在第 ❷ 步产生，位于**发起方 A**，由 `consume_response`（`initiator=true`）产出，直接进入 A 的 `current` 槽并立即可用于加密。
3. 响应方 B 的未确认密钥**不是由 `handshake_worker` 确认的**，而是在第 ❸ 步——A 发来的首个传输报文到达后，由**路由器 receive 管道**调用 `confirm_key` 把 `next` 转正为 `current`。这一步走的是 `udp_worker → router.recv`，完全不经过握手队列与 `handshake_worker`。
4. 整个过程中 `handshake_worker` 在 A 侧触发 `New`、`Message` 各一次；在 B 侧触发 `Message` 一次。`pending` 计数经历 3 次加、3 次减，最终归零。

**进阶思考（待本地验证）**：若 A 在收到 Response 前，因为 `REKEY_TIMEOUT`（5 秒）超时重发了 Initiation，会在 A 侧再次走 `New` 分支。请结合 [src/wireguard/handshake/peer.rs:104-120](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/peer.rs#L104-L120) 的 `check_replay_flood`（`TIME_BETWEEN_INITIATIONS=20ms`）思考：B 会如何处置这次重复的 Initiation？

## 6. 本讲小结

- `HandshakeJob` 只有 `Message`（入站握手报文，由 `udp_worker` 投递）与 `New`（本地主动发起，由 `packet_send_handshake_initiation` 投递）两种；两个生产者都先 `pending+1` 再 `send`，worker 消费时 `pending-1`，计数严格对称。
- under-load 用「阈值（512）+ 1 秒迟滞」两段判定，结果只影响传给 `device.process` 的 `src` 参数——过载才启用 mac2/cookie/限速，且只针对**入站**报文。
- `Message` 分支把报文交给 `device.process`，得到三元组 `Output(peer, resp, keypair)`；只有 Initiation/Response 两条认证路径会返回 `Some(peer)` 从而触发收尾逻辑。
- `New` 分支用 `device.begin` 构造并发送 Initiation，随后复位 `handshake_queued`，与生产端的去重标志形成闭环。
- 密钥确认是非对称的：发起方的 `consume_response` 产出 `initiator=true` 的**已确认**密钥（入 `current`），响应方的 `create_response` 产出 `initiator=false` 的**未确认**密钥（入 `next`，等首个传输报文经 `confirm_key` 转正）。
- 收尾四件事——字节统计、端点学习（漫游）、定时器回调、密钥交付与 id 回收——共同维护了 `id_map`「借出必归还」的不变量。

## 7. 下一步学习建议

- **路由器数据面**：本讲多次提到「首个传输报文触发 `confirm_key`」。建议接着学习 u5-l1（路由器总览）与 u5-l3（接收管道），看 `confirm_key` 如何在 `ReceiveJob::sequential_work` 中被调用。
- **KeyWheel 状态机**：u5-l6 会系统讲解 `next/current/previous` 三密钥轮转，与本讲的 confirmed/unconfirmed 概念无缝衔接。
- **定时器状态机**：本讲涉及的 `sent_handshake_initiation`/`timers_handshake_complete` 等钩子的完整语义在 u7-l1 展开，那里的 `retransmit_handshake` 定时器解释了「5 秒重传」的来源。
- **抗 DoS 全貌**：若你想深入了解 under-load 触发后的 cookie/限速细节，回到 u4-l4 阅读 `macs.rs` 与 `ratelimiter.rs`。
