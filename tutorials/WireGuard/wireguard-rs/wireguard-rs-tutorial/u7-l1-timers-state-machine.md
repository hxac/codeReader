# 定时器状态机与 Callbacks

## 1. 本讲目标

WireGuard 的会话密钥不是「一次握手，永久有效」，而是一套**有生命周期的密钥**：它会在固定时间后过期、在被破解足够多报文后主动轮换、在长时间无流量时用 keepalive 保活、在握手迟迟完不成时放弃并清零。驱动这一切的是一组**定时器（timer）**，而把这些定时器「挂」到真实数据收发动作上的，是一组**回调（Callbacks）**。

本讲学完后，你应该能够：

1. 说清 wireguard-rs 里 `Timers` 结构的五个定时器各自管什么协议事件、何时被启动与停止。
2. 理解 hjul 时间轮 `Runner` 的 `tick / slots / capacity` 三参数如何由 `TIMERS_*` 常量推导出来。
3. 读懂 `Timers::new` 工厂里 `fetch_peer!` / `fetch_timers!` 两个宏的「按公钥延迟查找」设计。
4. 看懂为 `PeerInner` 实现的 `Callbacks`（`send / recv / need_key / key_confirmed`）如何在路由器收发时驱动密钥刷新与握手重试。
5. 准确列出**发送侧 `keep_key_fresh` 触发重新握手的两个条件**，并解释**接收侧 `sent_lastminute_handshake` 标志防止重复握手**的作用。

## 2. 前置知识

阅读本讲前，你应当已经建立以下认知（来自前置讲义）：

- **握手与数据面分离**：握手模块用 Noise IK 协商出对称会话密钥 `KeyPair`，路由器（packet protector）用该密钥做 ChaCha20-Poly1305 加解密。
- **KeyWheel 三密钥轮转**（u5-l6）：每个 peer 维护 `current`（正在加密用）/ `previous`（解密旧报文）/ `next`（响应方待确认的新密钥）三个槽位。
- **密钥确认的不对称性**（u4-l6、u5-l3）：发起方 `consume_response` 产出的密钥 `initiator=true`、立即可加密；响应方的密钥 `initiator=false`，要靠「首个数据报文成功解密」才经 `confirm_key` 转正。
- **Callbacks trait**（u5-l1）：路由器在完成收发后，会回调上层 opaque 类型上的 `send / recv / need_key / key_confirmed` 四个方法；本工程里这个 opaque 类型就是 `PeerInner`。

几个本讲会反复用到的术语：

| 术语 | 含义 |
|------|------|
| 时间轮（timer wheel） | 一种用固定周期「tick」扫描、把到期定时器丢进对应「槽位」执行的数据结构，适合海量短期定时器。 |
| rekey（密钥轮换） | 在旧密钥失效前，主动发起一次新握手，换上全新会话密钥。 |
| keepalive（保活） | 无业务数据时也发一个空载荷的加密报文，让对端维持 NAT 映射、并证明「这条隧道还活着」。 |
| 事件钩子（event hook） | WireGuard 规范定义的一组「在某协议事件发生后应调用」的函数（如 `timers_data_sent`），由数据面/握手面触发。 |

## 3. 本讲源码地图

本讲聚焦四个文件：

| 文件 | 作用 |
|------|------|
| [src/wireguard/timers.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/timers.rs) | 全部主角：`Timers` 结构、五个定时器闭包、`timers_*` 事件方法、`Callbacks` 实现。 |
| [src/wireguard/constants.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/constants.rs) | 所有 `REKEY_* / REJECT_* / KEEPALIVE_*` 与 `TIMERS_*` 常量。 |
| [src/wireguard/router/types.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/types.rs) | `Callbacks` trait 的契约定义（路由器侧）。 |
| [src/wireguard/peer.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/peer.rs) | `PeerInner` 结构体（定时器的宿主）与 `packet_send_handshake_initiation`。 |

辅助引用（说明调用上下文）：`src/wireguard/wireguard.rs`（创建 `Runner`）、`src/wireguard/workers.rs`（握手成功后触发 `timers_handshake_complete` / `timers_session_derived`）、`src/wireguard/router/send.rs` 与 `receive.rs`（`C::send` / `C::recv` 调用点）。

## 4. 核心概念与源码讲解

### 4.1 Timers 状态字段与五个定时器

#### 4.1.1 概念说明

WireGuard 规范为每个 peer 定义了一组「定时器状态机」：每个协议事件（发了一个数据包、收了一个握手响应、密钥派生出来……）都要去拨弄几个定时器。wireguard-rs 把这套状态集中放进一个 `Timers` 结构，它**不是密码学对象**，而是一个「事件 → 定时器动作」的调度器。

`Timers` 持有两类东西：**配置/状态字段**（随配置改、随事件变的标量）和**五个定时器句柄**（由时间轮驱动、到期才执行的闭包）。

#### 4.1.2 核心流程

```
协议事件(收/发报文、握手完成、密钥派生)
        │
        ▼
  timers_*  事件方法   ──start/stop/reset──▶  五个 Timer
        │                                            │
        │                              到期时由 hjul 时间轮触发
        ▼                                            ▼
  更新原子状态(计数/标志)                    执行闭包(重握手/keepalive/清零)
```

五个定时器对应 WireGuard 规范里的五类到期动作：

| 定时器 | 到期后做什么 |
|--------|--------------|
| `retransmit_handshake` | 握手发出去没回音，重传一次 Initiation；超过次数则放弃并清零。 |
| `send_keepalive` | 一段时间没收到对端数据，发一个 keepalive。 |
| `send_persistent_keepalive` | 用户配置了固定间隔的 keepalive，按间隔周期发送。 |
| `new_handshake` | 长时间静默后，主动发起一次新握手（非重传）。 |
| `zero_key_material` | 彻底清零该 peer 的全部密钥材料（密钥已无价值）。 |

#### 4.1.3 源码精读

`Timers` 结构定义：

[src/wireguard/timers.rs:18-32](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/timers.rs#L18-L32) —— 定义了配置字段（`enabled`、`keepalive_interval`）、三个原子状态（`handshake_attempts`、`sent_lastminute_handshake`、`need_another_keepalive`）以及五个 `hjul::Timer`。

需要特别注意几个字段的语义：

- `enabled: bool`：注释写明「只在配置期间更新」。它是一切定时器动作的总开关——所有事件方法都会先判 `if timers.enabled`。`stop_timers` 会把它设为 `false`，从而让此后到期的定时器闭包成为空操作。
- `handshake_attempts: AtomicUsize`：已经重传过的握手次数，由 `retransmit_handshake` 闭包自增，超过 `MAX_TIMER_HANDSHAKES` 就放弃。
- `sent_lastminute_handshake: AtomicBool`：接收侧「最后一刻保命握手」的去重标志（详见 4.5）。
- `need_another_keepalive: AtomicBool`：当想启动 keepalive 定时器却发现它已在运行时，用这个标志记住「还需要再补一个」（见 4.4 的 `timers_data_received`）。

`stop_timers` 展示了「总开关 + 停表 + 复位」三步走：

[src/wireguard/timers.rs:46-69](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/timers.rs#L46-L69) —— 取写锁（阻止并发事件与 `start_timers`），置 `enabled=false`，逐个 `.stop()` 五个定时器，再把三个原子状态归零。对应 `WireGuard::down` 时对每个 peer 的 `peer.stop_timers()` 调用。

`start_timers` 是镜像操作，只重新点起 `send_persistent_keepalive`（若配置了间隔）：

[src/wireguard/timers.rs:71-87](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/timers.rs#L71-L87) —— 注意它不主动发起握手；握手是数据面/keepalive 按需触发的，`up` 只负责「让定时器开始工作」。

#### 4.1.4 代码实践

**实践目标**：确认 `enabled` 是所有定时器动作的唯一闸门。

**操作步骤**：
1. 打开 `src/wireguard/timers.rs`，在 `stop_timers` 把 `enabled` 设为 `false` 之后、`stop()` 五个定时器之前，加一行日志：`log::debug!("timers disabled for peer {}", self);`。
2. 对比阅读 `start_timers`，理解它为何用 `if timers.enabled { return; }` 做幂等保护。

**需要观察的现象**：所有 `timers_*` 事件方法体都以 `if timers.enabled { ... }` 守卫，说明 `enabled=false` 后即使时间轮到期、闭包也只是被 `fetch_timers!` 宏提前 `return`（见 4.3）。

**预期结果**：设备 down 后，再多的收发事件都不会改写定时器状态。待本地验证。

#### 4.1.5 小练习与答案

**Q1**：为什么 `enabled` 用普通 `bool` 而不是 `AtomicBool`，却能被多个定时器闭包安全读取？

**答**：因为 `enabled` 只在配置期间（`start_timers` / `stop_timers`）被写，而这两个方法都持有 `timers` 的**写锁**（`timers_mut()`）；事件方法读 `enabled` 时持有**读锁**（`timers()`）。读写锁已经保证了可见性与互斥，不必再叠加原子操作。定时器闭包内部则是通过 `fetch_timers!` 宏取读锁后再判 `enabled`。

**Q2**：`stop_timers` 里把三个原子状态归零，漏掉任一会怎样？

**答**：若不把 `handshake_attempts` 归零，下次 up 后第一次重传就可能直接超过阈值而误判「握手失败」；若不把 `sent_lastminute_handshake` 归零，接收侧 `keep_key_fresh` 在新会话里会错误地认为「已经发过保命握手」而不再触发；若不把 `need_another_keepalive` 归零，会多补一次无意义的 keepalive。

---

### 4.2 hjul 时间轮与 TIMERS_* 常量

#### 4.2.1 概念说明

五个定时器不是各自为政的操作系统定时器，而是统一挂在**一个 hjul 时间轮 `Runner`** 上。时间轮的思路是：把时间切成等长的「tick」，把整个轮盘分成若干「槽位（slots）」，定时器按其到期时间落到某个槽；每过一个 tick 扫描一个槽，把到期的闭包执行掉。这种结构对「大量、短期、可丢弃」的定时器非常高效——正好匹配 WireGuard 每个 peer 五个定时器、peer 数量可线性增长的场景。

#### 4.2.2 核心流程

时间轮由三个参数刻画：

- **tick**：扫描分辨率（多久转一格）。
- **slots**：轮盘格数，决定「最长能定多久的时」。
- **capacity**：内部存储的初始容量，会按需增长。

wireguard-rs 用 `constants.rs` 里的 `TIMERS_*` 常量把它们确定下来：

\[ \text{TIMERS\_SLOTS} = \frac{\text{TIMER\_MAX\_DURATION}}{\text{TIMERS\_TICK}} = \frac{200\,\text{s}}{100\,\text{ms}} = 2000 \]

也就是说，时间轮最长能调度 200 秒以内的定时器，分辨率 100 毫秒。

#### 4.2.3 源码精读

常量定义：

[src/wireguard/constants.rs:37-49](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/constants.rs#L37-L49) —— `TIMER_MAX_DURATION=200s`、`TIMERS_TICK=100ms`、`TIMERS_SLOTS=2000`、`TIMERS_CAPACITY=16`。

`Runner` 在设备创建时构造一次，放进 `WireguardInner.runner`：

[src/wireguard/wireguard.rs:37](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/wireguard.rs#L37) —— `runner: Mutex<Runner>` 字段。

[src/wireguard/wireguard.rs:290](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/wireguard.rs#L290) —— `Runner::new(TIMERS_TICK, TIMERS_SLOTS, TIMERS_CAPACITY)`。这是全设备**唯一**的时间轮，所有 peer 的所有定时器共享它。

`Runner` 的核心 API（从 timers.rs 用法归纳）：

| 方法 | 行为 |
|------|------|
| `runner.timer(closure)` | 在该时间轮上注册一个定时器，返回 `Timer` 句柄；到期执行 `closure`。 |
| `Timer::start(dur) -> bool` | 启动定时器。返回 `true` 表示「此前未运行、本次成功启动」；返回 `false` 表示「此前已在运行、本次未重启」。 |
| `Timer::reset(dur)` | 重置为新的时长（停表后重开）。 |
| `Timer::stop()` | 取消定时器。 |

> 说明：`start` 的返回值语义是从 [src/wireguard/timers.rs:100-105](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/timers.rs#L100-L105) 的 `!timers.send_keepalive.start(KEEPALIVE_TIMEOUT)` 用法反推得到的——返回 false 时设置 `need_another_keepalive`，意为「想重启但没成功」。

时间轮的「最大时长」必须能容纳 WireGuard 最长的定时器。最长的定时器是 `zero_key_material`，它被设为 `REJECT_AFTER_TIME * 3`：

[src/wireguard/timers.rs:166](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/timers.rs#L166) —— `timers.zero_key_material.reset(REJECT_AFTER_TIME * 3)`，即 \( 180\,\text{s} \times 3 = 540\,\text{s} \)。

注意：540s 超过了时间轮的 200s 上限。这正是 `constants.rs` 注释里 `TIME_HORIZON` 旁那句「this duration need not fit inside the timer wheel」的体现——hjul 对超长定时器有自身的处理（通常会在时间轮里多次转圈重插入），不必由调用方操心。但这也提醒我们：`TIMER_MAX_DURATION` 并不是「所有定时器时长」的硬上限，而是「时间轮一次扫描的周期」的设计参考。

#### 4.2.4 代码实践

**实践目标**：把时间轮参数与协议常量用断言钉死，避免日后改一处忘改另一处。

**操作步骤**：在 `src/wireguard/constants.rs` 末尾追加一个编译期断言（示例代码）：

```rust
// 示例代码：验证时间轮参数与最短/最长协议常量的关系
const _: () = assert!(TIMERS_TICK.as_micros() > 0);
const _: () = assert!(
    TIMER_MAX_DURATION.as_secs() >= REKEY_TIMEOUT.as_secs()
        + KEEPALIVE_TIMEOUT.as_secs(),
    "时间轮周期必须能容纳 new_handshake 的最长启动间隔"
);
```

**需要观察的现象**：`new_handshake` 的启动间隔是 `KEEPALIVE_TIMEOUT + REKEY_TIMEOUT = 15s`（见 4.4），远小于 200s 周期，断言成立。

**预期结果**：编译通过；若有人把 `TIMER_MAX_DURATION` 改小到 15s 以下，编译期就会失败。待本地验证。

#### 4.2.5 小练习与答案

**Q1**：为什么 tick 选 100ms 而不是 1ms 或 1s？

**答**：WireGuard 的最短定时器是 `REKEY_TIMEOUT = 5s` 与 `KEEPALIVE_TIMEOUT = 10s`，100ms 的分辨率（即最多 50 分之一个最短定时器）对定时精度绰绰有余，同时 100ms 一次扫描的 CPU 开销极低。1ms 会浪费 CPU、1s 会让 keepalive/重传抖动过大。

**Q2**：`TIMERS_CAPACITY = 16` 是什么的初始容量？

**答**：是时间轮内部存储结构（用于登记待触发定时器）的初始容量，会随 peer/定时器数量增长而扩容。设小一点减少空闲内存，设大一点减少扩容拷贝。

---

### 4.3 Timers::new：工厂、宏与五个定时器闭包

#### 4.3.1 概念说明

`Timers::new` 是一个**工厂函数**：给定设备句柄 `wg`、peer 公钥 `pk`、初始运行状态 `running`，它造出五个定时器。难点在于——定时器闭包要在「未来某个时刻」执行，那时 peer 可能已经被删除、设备可能已经 down。所以闭包**不能**在创建时直接捕获 peer 引用，而要在到期时「按公钥重新查一遍」。

#### 4.3.2 核心流程

```
Timers::new(wg, pk, running)
  │
  ├── 定义 fetch_peer! 宏: 按 pk 在 wg.peers 里查 peer，查不到就 return
  ├── 定义 fetch_timers! 宏: 取 peer 的 timers 读锁，若 !enabled 就 return
  │
  └── 对五个定时器，各用 runner.timer(|| { fetch_peer!; fetch_timers!; 做事 }) 构造
        （闭包只捕获 wg 的克隆 与 pk —— 都是 'static 的）
```

这套「延迟按公钥查找 + enabled 闸门」设计，是定时器能在 peer 频繁增删、设备 up/down 下安全运行的关键。

#### 4.3.3 源码精读

两个宏：

[src/wireguard/timers.rs:239-258](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/timers.rs#L239-L258) —— `fetch_peer!` 取 `wg.peers.read()` 后按 `pk` 查；查不到直接 `return`（peer 已删，定时器自然作废）。`fetch_timers!` 取该 peer 的 timers 读锁，若 `!enabled` 也 `return`（设备 down 了，不动作）。

构造体开头：

[src/wireguard/timers.rs:260-268](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/timers.rs#L260-L268) —— `let runner = wg.runner.lock();` 取时间轮锁；`enabled: running` 用传入的设备运行态初始化总开关。

**`retransmit_handshake` 闭包**（握手重传，最复杂的一个）：

[src/wireguard/timers.rs:269-299](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/timers.rs#L269-L299) —— 先 `fetch_peer!` + `fetch_timers!`；然后 `handshake_attempts.fetch_add(1)` 取出**旧值** `attempts`：

- 若 `attempts > MAX_TIMER_HANDSHAKES`（即已重传 18 次，见 4.2 推导），判定握手彻底失败：停 keepalive、启动 `zero_key_material`（`REJECT_AFTER_TIME*3`）、`purge_staged_packets()` 丢掉暂存包。
- 否则：重置自身 `retransmit_handshake.reset(REKEY_TIMEOUT)`（再等 5s）、`clear_src()` 清掉 sticky 源、`packet_send_queued_handshake_initiation(true)` 重排一次握手（`true` 表示是重试，不清零计数）。

**`send_keepalive` 闭包**：

[src/wireguard/timers.rs:300-313](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/timers.rs#L300-L313) —— 发 keepalive；若 `need_another_keepalive()` 返回 true（即期间又收到了数据、需要再保活一次），就再 `start(KEEPALIVE_TIMEOUT)` 排一个。

**`new_handshake` 闭包**（静默后主动新握手）：

[src/wireguard/timers.rs:314-330](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/timers.rs#L314-L330) —— 长时间没动静后，`clear_src()` + `packet_send_queued_handshake_initiation(false)` 发起一次**全新**握手（注意传 `false`，会清零 `handshake_attempts`，因为这不算重传链）。

**`zero_key_material` 闭包**：

[src/wireguard/timers.rs:331-341](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/timers.rs#L331-L341) —— 只 `fetch_peer!`（注意**没有** `fetch_timers!`，因为清零即便设备 down 也应执行），调用 `peer.zero_keys()` 把 KeyWheel 三槽全清。

**`send_persistent_keepalive` 闭包**：

[src/wireguard/timers.rs:342-360](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/timers.rs#L342-L360) —— 若 `keepalive_interval > 0`：停掉 `send_keepalive`、发一个 keepalive、再把自己 `start(keepalive_interval)` 周期重排，实现用户配置的固定间隔保活。

> 补充：`packet_send_queued_handshake_initiation` 定义在 [src/wireguard/peer.rs:47-74](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/peer.rs#L47-L74)，内部有「距上次发送不足 `REKEY_TIMEOUT` 则限速」与 `handshake_queued` 去重，最终把 `HandshakeJob::New(pk)` 投进握手队列（由 u4-l6 的 `handshake_worker` 消费）。

#### 4.3.4 代码实践

**实践目标**：把「五个定时器闭包 → 各自到期动作」整理成可核对的事实表。

**操作步骤**：在 `Timers::new` 上方加一段注释（示例代码），按下表填空：

| 定时器字段 | 是否调用 `fetch_timers!` | 到期动作 | 重排自身？ |
|-----------|:---:|------|:---:|
| retransmit_handshake | 是 | 计数++；超限放弃+清零，否则重传握手 | 超限时 reset(REKEY_TIMEOUT) |
| send_keepalive | 是 | 发 keepalive | need_another 时 start |
| new_handshake | 是 | clear_src + 全新握手 | 否 |
| zero_key_material | 否 | zero_keys | 否 |
| send_persistent_keepalive | 是 | 发 keepalive | start(keepalive_interval) |

**需要观察的现象**：只有 `zero_key_material` 不经过 `fetch_timers!`，因为它应当在任何状态下都执行清零。

**预期结果**：注释表格与源码五个闭包逐一对应无误。待本地验证。

#### 4.3.5 小练习与答案

**Q1**：闭包里为什么 `fetch_peer!` 用 `wg.peers.read()`（读锁）而不用写锁？

**答**：定时器闭包只读地查 peer、调用 peer 上的方法（peer 内部状态各自有锁），不需要持有 peer 映射的写锁；用读锁能允许多个定时器/握手 worker 并发查 peer，降低争用。peer 的增删（`add_peer` / `remove_peer`）才取写锁。

**Q2**：`retransmit_handshake` 里 `attempts > MAX_TIMER_HANDSHAKES` 用的是 `fetch_add` 的**返回值**（旧值），如果改成先 `load` 再 `add` 会有什么并发问题？

**答**：会引入 TOCTOU 竞态：两个并发到期可能都 `load` 到 17，于是都走「重传」分支而把计数顶到 19，绕过放弃判断。`fetch_add` 是原子的「读改写」，保证每个到期拿到的旧值唯一递增，判断与计数一体完成。

---

### 4.4 timers_* 事件方法：协议事件如何驱动定时器

#### 4.4.1 概念说明

`timers_*` 是一组**事件钩子方法**，名字直接对应 WireGuard 规范里的定时器事件。它们是「上层语义动作」（如「刚发了一个数据包」「握手完成了」）与「底层定时器 start/stop」之间的翻译层。注意它们本身不含时间轮逻辑，只负责在正确时机拨正确的那几个表。

#### 4.4.2 核心流程

下表把主要事件方法映射到「被谁调用 → 对定时器做了什么」：

| 事件方法 | 典型调用者 | 动作 |
|----------|-----------|------|
| `timers_data_sent` | `Callbacks::send`（发了含数据的报文） | `new_handshake.start(KEEPALIVE_TIMEOUT+REKEY_TIMEOUT)` |
| `timers_data_received` | `Callbacks::recv` | `send_keepalive.start(KEEPALIVE_TIMEOUT)`，启动失败则置 `need_another_keepalive` |
| `timers_any_authenticated_packet_sent` | 多处（发任何认证包后） | `send_keepalive.stop()`（既然刚发包，就不必再 keepalive） |
| `timers_any_authenticated_packet_received` | `Callbacks::recv` | `new_handshake.stop()`（既然刚收到包，不必因静默重握手） |
| `timers_handshake_initiated` | `sent_handshake_initiation` | `send_keepalive.stop()` + `retransmit_handshake.reset(REKEY_TIMEOUT)` |
| `timers_handshake_complete` | handshake_worker / `key_confirmed` | `retransmit.stop()` + 计数归零 + 复位 lastminute 标志 + 记录握手墙钟时间 |
| `timers_session_derived` | handshake_worker（产出新 keypair） | `zero_key_material.reset(REJECT_AFTER_TIME*3)` |
| `timers_any_authenticated_packet_traversal` | `Callbacks::send/recv` | 若配了 keepalive，`send_persistent_keepalive.reset(keepalive_interval)`（把周期保活往后推） |

#### 4.4.3 源码精读

`timers_data_sent` / `timers_data_received` 这对体现了「发数据→预备重握手、收数据→预备保活」的对称设计：

[src/wireguard/timers.rs:90-105](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/timers.rs#L90-L105) —— 发数据后，给 `new_handshake` 上一个 15s 的「静默就重握手」闹钟；收数据后，给 `send_keepalive` 上一个 10s 的「没流量就保活」闹钟，若该闹钟已在跑（`start` 返回 false），就标记「还需要再补一个」。

`timers_handshake_complete` 是握手的收尾，做四件事：

[src/wireguard/timers.rs:146-157](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/timers.rs#L146-L157) —— 停重传表、`handshake_attempts` 归零、`sent_lastminute_handshake` 复位为 false、把当前墙钟时间写进 `walltime_last_handshake`（供 UAPI `get` 报告 `last_handshake_time`，见 u6-l4）。

`timers_session_derived` 把「密钥清零」闹钟挂到新会话上：

[src/wireguard/timers.rs:162-168](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/timers.rs#L162-L168) —— 每次派生出新 keypair，都把 `zero_key_material` 重置为 540s；即「这把新密钥最多留 540s 就强制清零」。

`timers_any_authenticated_packet_traversal` 推迟周期保活：

[src/wireguard/timers.rs:173-182](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/timers.rs#L173-L182) —— 只要还有真实流量穿过隧道，就把「固定间隔 keepalive」往后推一个间隔，避免和正常数据重复发送。

最后是两个高层封装（直接被握手 worker 调用）：

[src/wireguard/timers.rs:194-206](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/timers.rs#L194-L206) —— `sent_handshake_initiation` 把「更新 `last_handshake_sent` + 触发 `timers_handshake_initiated` + 重传 + traversal + sent」打包，对应发起方发出 Initiation 后的全部定时器副作用；`sent_handshake_response` 是响应方发出 Response 后的简化版。

#### 4.4.4 代码实践

**实践目标**：验证「刚收到对端报文后，`new_handshake` 被停止」这一保活/重握手机制的正确性。

**操作步骤**：
1. 在 `timers_any_authenticated_packet_received` 的 `timers.new_handshake.stop();` 前后各加一行 `log::trace!("new_handshake stopped on recv");`。
2. 结合 u7-l4 的 `test_pure_wireguard`（双实例端到端测试）跑 `cargo test`，用 `RUST_LOG=trace` 观察日志。

**需要观察的现象**：每当一端收到对端的加密报文，日志里就出现一次 `new_handshake stopped`，说明「静默重握手」闹钟被不断推后。

**预期结果**：在有持续双向流量的测试里，`new_handshake` 不会真正到期；只有当流量真正停止 15s 后才会触发新握手。待本地验证。

#### 4.4.5 小练习与答案

**Q1**：为什么 `timers_any_authenticated_packet_sent` 要 `send_keepalive.stop()`？

**答**：keepalive 的目的是「告诉对端我还活着」；既然刚刚已经发了一个认证报文（数据或握手），对端已经知道本端活着，再等 10s 发 keepalive 就多余了。停掉它可以减少无谓流量。

**Q2**：`timers_handshake_complete` 里复位 `sent_lastminute_handshake = false` 的意义是什么？

**答**：`sent_lastminute_handshake` 是接收侧「保命握手」的去重标志（见 4.5）。每次新握手完成，密钥已经更新，旧的「保命握手」语义失效，必须把标志复位，让新密钥在接近过期时能再触发一次保命握手。

---

### 4.5 Callbacks 实现：把数据面收发接入定时器（含 keep_key_fresh）

#### 4.5.1 概念说明

`Callbacks` trait（[src/wireguard/router/types.rs:29-35](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/types.rs#L29-L35)）定义了四个回调点：`send / recv / need_key / key_confirmed`。路由器在数据面完成收发后调用它们，本工程为 `PeerInner` 实现了这套 trait——实现体正是「更新定时器 + 更新字节统计 + 判定是否该刷新密钥」。这一层是**定时器与真实数据流的接合部**。

四个回调的触发上下文：

| 回调 | 路由器侧调用点 | 含义 |
|------|---------------|------|
| `send` | [src/wireguard/router/send.rs:134](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/send.rs#L134)（`SendJob::sequential_work`，发出加密包后） | 刚向该 peer 发了一个传输报文 |
| `recv` | [src/wireguard/router/receive.rs:183](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/receive.rs#L183)（`ReceiveJob::sequential_work`，解密并处理完入站包后） | 刚从该 peer 收到一个传输报文 |
| `need_key` | [src/wireguard/router/peer.rs:291](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L291)（`Peer::send` 发现没有可用加密密钥时） | 要发包但没有密钥，催促上层去握手 |
| `key_confirmed` | [src/wireguard/router/peer.rs:341](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L341)（`confirm_key` 把 `next` 转正后） | 响应方的新密钥被首个数据报文确认 |

#### 4.5.2 核心流程

`send` / `recv` 的结构高度对称：

```
send/recv 回调被路由器调用
  ├── timers_any_authenticated_packet_traversal()   // 推迟周期 keepalive
  ├── timers_any_authenticated_packet_sent/received()
  ├── fetch_add 到 tx_bytes / rx_bytes              // 字节统计
  ├── 若是真实数据(size>阈值) → timers_data_sent / timers_data_received
  └── keep_key_fresh(...) → 满足条件则 packet_send_queued_handshake_initiation
```

`need_key` / `key_confirmed` 则极简：前者直接催一次握手，后者调 `timers_handshake_complete`。

`keep_key_fresh` 是「主动 rekey」的核心——在密钥还没失效、但**接近**失效时，主动发起一次新握手换密钥，避免密钥硬过期导致丢包。它在发送侧和接收侧有两套**不同的**触发条件，这正是本讲实践任务的重点。

#### 4.5.3 源码精读

**`send` 回调与发送侧 `keep_key_fresh`**：

[src/wireguard/timers.rs:371-394](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/timers.rs#L371-L394) —— 先 traversal + sent + `tx_bytes` 累加；若 `size > message_data_len(0) && sent`（即真实含数据且确实发出去了）才 `timers_data_sent`。`message_data_len(0)` = `sizeof(TransportHeader) + SIZE_TAG` = 16 + 16 = 32 字节，即「纯 keepalive」的报文长度，用来区分 keepalive 与数据。

发送侧 `keep_key_fresh`（[timers.rs:386-389](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/timers.rs#L386-L389)）有两个条件，**满足任一**就触发重新握手：

\[ \text{keep\_key\_fresh}^{\text{send}} = \bigl(\,\text{counter} > \underbrace{\text{REKEY\_AFTER\_MESSAGES}}_{2^{60}}\,\bigr) \;\lor\; \bigl(\,\text{keypair.initiator} \;\land\; \text{now} - \text{birth} > \underbrace{\text{REKEY\_AFTER\_TIME}}_{120\,\text{s}}\,\bigr) \]

- 条件一（报文计数）：当前密钥已加密超过 \( 2^{60} \) 个报文。这个条件**不限 initiator**——任一方只要发够了量就该轮换。
- 条件二（时间）：**且**自己是 initiator（即这把密钥是自己发起握手换来的、可立即加密），且密钥年龄超过 120s。`initiator` 限制保证了「时间到期的 rekey 由能立即加密的一方发起」，避免双方同时各自 rekey 造成握手碰撞。

常量见 [src/wireguard/constants.rs:4-8](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/constants.rs#L4-L8)：`REKEY_AFTER_MESSAGES = 1<<60`、`REKEY_AFTER_TIME = 120s`、`REJECT_AFTER_TIME = 180s`。注意 120s（rekey）< 180s（reject），留出 60s 的「rekey 余量」让新握手在硬过期前完成。

**`recv` 回调与接收侧 `keep_key_fresh`**：

[src/wireguard/timers.rs:403-431](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/timers.rs#L403-L431) —— 结构同 send，但收数据用 `size > 0 && sent` 判定（keepalive 解密后 `inner_length` 为空，但报文本身仍有长度，故此处用 `size > 0`）。接收侧 `keep_key_fresh`（[timers.rs:418-430](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/timers.rs#L418-L430)）只有一个时间条件，且多了一道 `sent_lastminute_handshake` 去重：

\[ \text{keep\_key\_fresh}^{\text{recv}} = \bigl(\,\text{now} - \text{birth} > \underbrace{\text{REJECT\_AFTER\_TIME} - \text{KEEPALIVE\_TIMEOUT} - \text{REKEY\_TIMEOUT}}_{180-10-5=165\,\text{s}}\,\bigr) \;\land\; \neg\,\text{sent\_lastminute\_handshake} \]

**`sent_lastminute_handshake` 的作用**——这是本讲实践任务的另一半。当密钥年龄超过 165s（即距离 180s 硬过期只剩 15s）时，接收侧会发起一次「最后一刻保命握手」。但接收侧每收到一个报文都会进 `recv`，如果不加去重，在这最后 15s 里每收一个包就排队一次握手，会瞬间打爆握手队列。

[src/wireguard/timers.rs:423-430](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/timers.rs#L423-L430) —— `swap(true, Ordering::Acquire)` 是原子的「置真并取旧值」：旧值为 false（第一次）时条件 `!swap(...)` 为真，触发一次握手；之后旧值变 true，条件恒假，不再触发。该标志**只在 `timers_handshake_complete` 里被复位为 false**（见 4.4），即「新握手完成后才允许下一次保命握手」。这就把最后 15s 内的成千上万次 `recv` 收敛成**恰好一次**保命握手。15s 这个余量也经过精心设计：它 \( \ge \) `KEEPALIVE_TIMEOUT(10s) + REKEY_TIMEOUT(5s)`，足以让一次握手（含一次重传）在硬过期前完成。

**`need_key` 与 `key_confirmed`**：

[src/wireguard/timers.rs:439-449](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/timers.rs#L439-L449) —— `need_key` 直接催一次握手（`packet_send_queued_handshake_initiation(false)`，自带速率限制与去重）；`key_confirmed` 调 `timers_handshake_complete` 收尾。注意 `key_confirmed` 只在响应方被触发（首个数据报文经 `confirm_key` 路径，[receive.rs:164-167](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/receive.rs#L164-L167)），发起方的握手完成则由 handshake_worker 直接调 `timers_handshake_complete`（[workers.rs:230](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/workers.rs#L230)）。

> 补充：`KeyPair` 的 `birth` / `initiator` 字段见 [src/wireguard/types.rs:31-37](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/types.rs#L31-L37)；密钥材料由 `Key::Drop` 自动清零（[types.rs:12-16](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/types.rs#L12-L16)），这是 u7-l2 的主题。

#### 4.5.4 代码实践（本讲核心任务）

**实践目标**：把发送侧 `keep_key_fresh` 的两个触发条件、以及接收侧 `sent_lastminute_handshake` 的去重作用，落到可观察的代码上。

**操作步骤**：

1. **列出发送侧两个条件**。打开 [src/wireguard/timers.rs:386-389](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/timers.rs#L386-L389)，在 `keep_key_fresh` 函数体上方写注释（示例代码）：

   ```rust
   // 发送侧 keep_key_fresh 触发重新握手的两个条件（满足其一）：
   //  (A) 计数条件：counter > REKEY_AFTER_MESSAGES (1<<60)，不限 initiator；
   //  (B) 时间条件：本端是 initiator 且密钥年龄 > REKEY_AFTER_TIME (120s)。
   // 条件 B 用 initiator 限定，确保「时间到点的 rekey」由能立即加密的一方发起。
   fn keep_key_fresh(keypair: &Arc<KeyPair>, counter: u64) -> bool {
       counter > REKEY_AFTER_MESSAGES
           || (keypair.initiator && Instant::now() - keypair.birth > REKEY_AFTER_TIME)
   }
   ```

2. **解释 `sent_lastminute_handshake` 的作用**。在 [timers.rs:423-430](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/timers.rs#L423-L430) 的 `swap` 调用旁加注释（示例代码）：

   ```rust
   // 接收侧：密钥年龄 > 165s(=180-10-5) 时发起「最后一刻保命握手」。
   // sent_lastminute_handshake 用 swap 原子去重：第一次 swap 返回 false → 触发一次握手；
   // 此后恒为 true → 同一密钥生命周期内不再重复握手。
   // 该标志只在 timers_handshake_complete 里复位，保证「新握手完成后才允许下一次保命握手」。
   if keep_key_fresh(keypair)
       && !peer.timers().sent_lastminute_handshake.swap(true, Ordering::Acquire)
   {
       peer.packet_send_queued_handshake_initiation(false);
   }
   ```

3. （可选，验证用）在两个 `keep_key_fresh` 返回 true 的分支里加 `log::debug!("keep_key_fresh triggered rekey")`，结合 `test_pure_wireguard` 跑 `RUST_LOG=debug cargo test`。

**需要观察的现象**：
- 发送侧：当一端持续发包超过 120s（且自己是 initiator）时，日志出现一次 rekey 触发。
- 接收侧：在密钥接近过期（>165s）的窗口内，即便对端连续涌入大量报文，`keep_key_fresh triggered rekey` 也**只打印一次**。

**预期结果**：发送侧两个条件与源码一致；接收侧去重生效、整个密钥生命周期内最多触发一次保命握手。待本地验证（时间类现象依赖真实时钟，建议在长流量测试中观察）。

#### 4.5.5 小练习与答案

**Q1**：发送侧 `keep_key_fresh` 的条件 B 为什么要加 `keypair.initiator`，而条件 A（计数）不加？

**答**：时间触发的 rekey 应由「能立即用新密钥加密」的一方发起——即上次握手的 initiator（其密钥 `initiator=true`、已确认、立即可加密）。如果双方都在 120s 时各自发起，会同时产生两条 Initiation 造成碰撞与浪费。而报文计数条件 A 是硬上限（任一方发够 \( 2^{60} \) 都该换），且双方计数不同步，故不限定 initiator，谁先到谁发起。

**Q2**：若把接收侧 `keep_key_fresh` 的阈值从 `REJECT_AFTER_TIME - KEEPALIVE_TIMEOUT - REKEY_TIMEOUT`（165s）改成 `REJECT_AFTER_TIME`（180s），会有什么后果？

**答**：保命握手会被推迟到密钥**已经硬过期**时才发起。此时旧密钥已无法加解密，而新握手尚未完成，会出现一段「既不能用旧密钥、又没有新密钥」的真空期，导致丢包。当前的 165s 阈值留出恰好 `KEEPALIVE_TIMEOUT + REKEY_TIMEOUT = 15s` 的余量，让一次握手（含一次 5s 重传）能在 180s 硬过期前完成。

**Q3**：`need_key` 回调每收一个待加密包都会被调用（注释明说 "called continuously"），为什么不会打爆握手队列？

**答**：因为 `need_key` → `packet_send_queued_handshake_initiation` 内部有两道闸门（[peer.rs:47-74](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/peer.rs#L47-L74)）：一是「距上次发送不足 `REKEY_TIMEOUT` 则限速直接 return」，二是 `handshake_queued` 原子去重（已在队列里就不再投）。所以连续的 `need_key` 只会真正排队一次握手。

---

## 5. 综合实践

把本讲全部知识串起来：**画出一条「密钥从诞生到清零」的定时器时序图**。

请基于本讲源码，绘制一张时序图（文字版即可），覆盖以下阶段，并在每个箭头上标注「触发了哪个 `timers_*` 方法 / 哪个 Callback / 启停了哪个定时器」：

1. **密钥派生**：handshake_worker 产出新 keypair → `timers_session_derived`（挂上 540s 的 `zero_key_material`）。
2. **正常收发**：双方持续收发数据 → 每次触发 `Callbacks::send/recv` → traversal / data_sent / data_received / stop keepalive / stop new_handshake。
3. **接近过期（发送侧）**：发包时密钥年龄 >120s（且 initiator）→ 发送侧 `keep_key_fresh` 触发 rekey → `need_key`/新握手。
4. **接近过期（接收侧）**：收包时密钥年龄 >165s → 接收侧 `keep_key_fresh` + `sent_lastminute_handshake` 去重 → 恰好一次保命握手。
5. **新握手完成**：`timers_handshake_complete`（复位 attempts、复位 lastminute 标志、记录墙钟时间）→ 回到阶段 1（新密钥重新挂 540s 清零表）。
6. **极端失败**：若握手连续重传超过 `MAX_TIMER_HANDSHAKES=18` 次 → `retransmit_handshake` 闭包放弃 → `zero_key_material`（540s 后）+ `purge_staged_packets`。

**交付物**：一张标注完整的时序图，并附一段文字说明「为何 120s(rekey) < 165s(recv 保命) < 180s(reject) < 540s(zero)」这组时间常量的层层递进关系（提示：每层都为下一层留出安全余量）。

## 6. 本讲小结

- `Timers` 是「事件 → 定时器」调度器，持有 3 个配置/状态字段与 5 个 hjul 定时器，全部受 `enabled` 总开关与读写锁保护。
- 五个定时器挂在**全设备唯一**的 hjul 时间轮上，参数由 `TIMERS_TICK=100ms / TIMERS_SLOTS=2000 / TIMERS_CAPACITY=16` 决定。
- `Timers::new` 用 `fetch_peer!`/`fetch_timers!` 宏实现「按公钥延迟查找 + enabled 闸门」，使定时器能在 peer 增删与 up/down 下安全触发。
- `timers_*` 事件方法是协议语义与定时器 start/stop/reset 之间的翻译层；`send`/`recv` 回调把数据面收发接入这套机制并负责字节统计。
- **发送侧 `keep_key_fresh` 两个条件**：`counter > 2^60`，或 `initiator 且年龄>120s`。
- **接收侧 `sent_lastminute_handshake`**：用原子 `swap` 把最后 15s（>165s）内的海量 `recv` 收敛成**恰好一次**保命握手，仅在 `timers_handshake_complete` 复位。

## 7. 下一步学习建议

- **u7-l2 密钥材料的清零与安全**：本讲多次提到 `zero_key_material` 定时器与 `Key::Drop`，下一讲深入 `Key::Drop` 清零、`clear_stack_on_return_fnone`、`ct_eq` 常时间比较，讲清「密钥材料的三层清零」。
- **u7-l3 零拷贝报文解析**：本讲用到的 `message_data_len`、`TransportHeader` 都基于 zerocopy，下一讲系统梳理 `LayoutVerified` 在握手与传输报文中的贯穿用法。
- **动手延伸**：若想验证时间常量的层层余量，可在 `constants.rs` 临时把 `REKEY_AFTER_TIME` 调小、`KEEPALIVE_TIMEOUT` 调大，观察 u7-l4 的 `test_pure_wireguard` 是否仍能稳定握手——这会直观暴露「保命握手余量不足」导致的丢包。
