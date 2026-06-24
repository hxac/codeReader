# 实时特性：rt feature、QoS Realtime 与异步分配器

## 1. 本讲目标

BUS/RT 同时为「高负载」和「超低延迟实时」两类场景做了优化。本讲讲清楚让它在实时运行时（real-time runtime）下也能安全工作的三根支柱：

1. **QoS 的实时位**——用一个布尔位告诉系统「这条消息请立即刷新，不要缓冲」。
2. **rt feature 的锁切换**——把内部互斥锁从会自旋的 `parking_lot` 换成不自旋的 `parking_lot_rt`，消除实时系统里的延迟毛刺。
3. **AsyncAllocator + direct_alloc_limit**——把大块内存分配挪出实时运行时，让「大消息慢发送方」但不拖累「小消息的实时客户端」。

学完后你应当能够：

- 说出 `QoS::Realtime` 在位掩码上的位置，以及它如何端到端透传到出站刷新策略。
- 解释 `rt` feature 到底改了哪些锁、为什么自旋锁对实时不安全。
- 读懂 `handle_reader` 里 `direct_alloc_limit` 的分支，并能实现一个自定义 `AsyncAllocator`。

本讲承接 u6-l1（连接生命周期，知道 `handle_reader`/`handle_writer` 各自的职责）和 u2-l1（核心类型，知道 `QoS` 的两个位）。

## 2. 前置知识

### 2.1 什么是「实时安全」

在通用异步运行时（如 tokio）里，一个任务在 worker 线程上跑，遇到 `.await` 才可能让出。如果一个操作**不经过 `.await` 就长时间占用 CPU**（比如分配并清零一大块内存 `vec![0; huge]`，或一把会自旋的锁），它就会霸占 worker 线程，导致**同一 worker 上其它任务**（包括别人的实时小消息）被拖延。

实时安全的核心要求是：**关键路径上的操作必须有界、可预测、不忙等**。

### 2.2 自旋锁为什么对实时不友好

`parking_lot::Mutex` 在抢锁失败时会先**自旋**（busy-wait，空转 CPU 一会儿）再让出。这在吞吐上很高效，但在实时系统里有两个坏处：

- 自旋本身就是一次不可预测的 CPU 占用，造成延迟毛刺。
- 可能引发**优先级反转**：高优先级实时线程在自旋等一把锁，而持锁的是低优先级线程，由于高优先级线程占着 CPU 空转，低优先级线程反而拿不到 CPU 去释放锁，延迟被放大。

`parking_lot_rt` 是 `parking_lot` 的一个分支，**移除了自旋**，只靠操作系统的 futex/condvar 把线程挂起（park）。挂起会让出 CPU，延迟变得有界、可预测，因此是实时安全的。

> 小贴士：本讲里「实时（realtime）」指**延迟可预测**，不一定等于「快」。目标是「最坏情况也很快」，而不是「平均很快」。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `src/lib.rs` | `QoS` 枚举与 `is_realtime()`/`needs_ack()`；`rt` feature 下的 `RawMutex`/`Condvar` 别名 |
| `src/broker.rs` | `AsyncAllocator` trait、`Options::with_async_allocator`、`handle_reader` 的 `direct_alloc_limit` 分支、`rt` 下 `SyncMutex` 的导入切换、帧的 `realtime` 字段透传 |
| `src/comm.rs` | `Flush` 枚举与 `From<bool> for Flush`：把 realtime 布尔位翻译成「立即刷新/延迟合并」 |
| `Cargo.toml` | `rt` feature 定义（`rt = ["dep:parking_lot_rt"]`） |
| `examples/inter_thread.rs` | 嵌入式 Broker 的最小蓝本，本讲实践在其基础上扩展 |

永久链接 base：`https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/`

## 4. 核心概念与源码讲解

### 4.1 实时特性的三根支柱（总览）

在钻进每一处源码之前，先用一张图把三根支柱串起来。一条从客户端发出的实时消息，要经过这三道关卡：

```
发送方                                代理(Broker)                          接收方
  │  QoS::Realtime                        │                                    │
  │  ──① realtime 位──>                   │                                    │
  │  send/publish(...,QoS::Realtime)      │                                    │
  │                              ② rt feature 锁                            │
  │                              clients/broadcasts/subscriptions           │
  │                              这些表用的 SyncMutex                        │
  │                              在 rt 下是 parking_lot_rt(不自旋)           │
  │                                   handle_reader                          │
  │                                   ③ direct_alloc_limit 分支              │
  │                                   小消息: vec![0;len] 直接分(快)          │
  │                                   大消息: async_allocator.allocate(挪走)   │
  │                                   把 realtime 透传进 FrameData            │
  │                                                                       │
  │                              handle_writer                              │
  │                              frame.realtime.into() => Flush::Instant     │
  │                              ──① 立即 flush ─────────────────────────>  │
```

- **① QoS Realtime 位**：决定「这条消息是否立即刷新」。
- **② rt feature**：决定「代理内部路由表的锁是否自旋」。
- **③ AsyncAllocator**：决定「大消息的内存分配是否霸占运行时线程」。

三者正交、各管一段。下面逐一精读。

### 4.2 QoS 的实时位：is_realtime 与 needs_ack

#### 4.2.1 概念说明

`QoS`（Quality of Service，服务质量）是 BUS/RT 给每条消息贴的「两个开关」。它用一个 `u8` 的低两位编码，**两个位是正交的、各自独立的语义**：

| 变量 | 值 | `needs_ack()` (位 0) | `is_realtime()` (位 1) |
|---|---|---|---|
| `No` | 0 | 否 | 否 |
| `Processed` | 1 | 是 | 否 |
| `Realtime` | 2 | 否 | 是 |
| `RealtimeProcessed` | 3 | 是 | 是 |

- 位 0（`needs_ack`）= 是否要等代理回 `OP_ACK` 才算发送成功。
- 位 1（`is_realtime`）= 是否要求立即刷新出站缓冲（本讲主角）。

注意：`Processed`（要 ACK）和 `Realtime`（要立即刷新）是两件事。`RealtimeProcessed = 3` 两个都想要。

#### 4.2.2 核心流程

`QoS` 用位掩码一次判定即可，无需 match：

\[
\text{is\_realtime}(q) = (q\ \&\ \texttt{0b10}) \neq 0,\qquad
\text{needs\_ack}(q) = (q\ \&\ \texttt{0b01}) \neq 0
\]

因为 `#[repr(u8)]`，`self as u8` 直接拿到判别值，位运算是 `#[inline]` 的零开销操作。

#### 4.2.3 源码精读

枚举定义（[src/lib.rs:352-359](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L352-L359)）——四个变体的判别值就是上面表格里的 0/1/2/3：

```rust
#[derive(Debug, Copy, Clone)]
#[repr(u8)]
pub enum QoS {
    No = 0,
    Processed = 1,
    Realtime = 2,
    RealtimeProcessed = 3,
}
```

两个判定方法（[src/lib.rs:361-370](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L361-L370)）——纯粹的位与运算：

```rust
impl QoS {
    pub fn is_realtime(self) -> bool { self as u8 & 0b10 != 0 }
    pub fn needs_ack(self) -> bool  { self as u8 & 0b1  != 0 }
}
```

这个 `realtime` 布尔值最终会被存进 `FrameData` 的字段（[src/lib.rs:418](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L418)），随帧一起在网络/线程间流转，并由 `FrameData::is_realtime()`（[src/lib.rs:496-499](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L496-L499)）读出。也就是说：**发送方设的 QoS 实时位，会被打包进帧、随帧送达接收方**——这是端到端实时透传的基础。

#### 4.2.4 代码实践

**目标**：亲手验证四个 QoS 变体的两个位。

**操作步骤**（源码阅读型，无需启动服务）：

1. 对照 [src/lib.rs:352-359](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L352-L359)，在纸上写出每个变体的 `as u8` 值。
2. 套用 `is_realtime = q & 0b10 != 0` 与 `needs_ack = q & 0b1 != 0` 手算。

**需要观察的现象 / 预期结果**：得到与 4.2.1 表格完全一致的结果。特别注意 `Realtime = 2`（二进制 `10`）：`is_realtime` 为真、`needs_ack` 为假。

#### 4.2.5 小练习与答案

**练习 1**：如果有一个第 5 种 QoS 值 `4`（二进制 `100`），它的 `is_realtime()` 和 `needs_ack()` 各是什么？

**答案**：`4 & 0b10 = 0`，`4 & 0b01 = 0`，所以两者都为假。但注意 `QoS::try_from(4)` 会返回 `Err`（[src/lib.rs:372-383](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L372-L383) 只接受 0..=3），所以协议层不会出现值 4。

**练习 2**：为什么把 ACK 和 realtime 设计成两个正交的位，而不是「Realtime 就隐含 Processed」？

**答案**：因为它们优化的是两个独立维度——`needs_ack` 优化「可靠性」（要不要确认），`is_realtime` 优化「延迟」（要不要立即刷新）。实时场景常常想要最低延迟但不需要逐条确认（`QoS::Realtime = 2`），把两者绑死会损失这种组合的灵活性。

### 4.3 从 QoS 到刷新：端到端实时透传

#### 4.3.1 概念说明

`is_realtime()` 只是个布尔位。它怎么变成「立即把字节刷到 socket」？靠的是出站缓冲层 `TtlBufWriter` 的 `Flush` 策略。BUS/RT 的设计是：**发送方在 QoS 里声明实时性，代理把它透传进帧，接收方的写出端据此决定刷新时机**。

`Flush` 有三态（[src/comm.rs:8-13](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/comm.rs#L8-L13)）：

| `Flush` | 含义 | 谁会走到 |
|---|---|---|
| `No` | 只入缓冲，不刷新 | 帧头、中段（合并用） |
| `Scheduled` | 入缓冲 + 通知后台 flusher 延迟刷新（吞吐优先） | 非 realtime 的 payload |
| `Instant` | 同步立即 flush（延迟优先） | realtime 的 payload |

#### 4.3.2 核心流程

`From<bool> for Flush`（[src/comm.rs:15-24](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/comm.rs#L15-L24)）是连接两层的「翻译器」：realtime 布尔位 → `Instant`/`Scheduled`：

```
frame.realtime == true  => Flush::Instant   (立即 flush)
frame.realtime == false => Flush::Scheduled (延迟合并 flush)
```

透传链路（内部客户端场景）：

1. 用户调用 `client.send(target, payload, QoS::Realtime)`。
2. `broker::Client` 的 `send` 实现把 `qos.is_realtime()` 作为参数传给 `send!` 宏（[src/broker.rs:349-368](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L349-L368)，关键在 [第 364 行](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L364)）。
3. `send!` 宏把这个布尔值写进 `FrameData.realtime`（[src/broker.rs:129-137](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L129-L137)）。
4. 帧经接收方的 `tx` 通道被其 `handle_writer` 取出，用 `frame.realtime.into()` 决定 payload 的刷新策略（[src/broker.rs:2259](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L2259)）。

#### 4.3.3 源码精读

翻译器（[src/comm.rs:15-24](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/comm.rs#L15-L24)）：

```rust
impl From<bool> for Flush {
    fn from(realtime: bool) -> Self {
        if realtime { Flush::Instant } else { Flush::Scheduled }
    }
}
```

写出端如何用 `Flush`（[src/comm.rs:66-75](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/comm.rs#L66-L75)）：`Instant` 在持锁状态下同步 `flush()`，`Scheduled` 只往容量为 1 的信号通道发个通知、让后台 flusher 延迟（默认 10µs）刷新以合并多条消息：

```rust
pub async fn write(&mut self, buf: &[u8], flush: Flush) -> std::io::Result<()> {
    let mut writer = self.writer.lock().await;
    let result = writer.write_all(buf).await;
    if flush == Flush::Instant {
        writer.flush().await?;                       // 实时：立刻刷
    } else if flush == Flush::Scheduled && self.tx.is_empty() {
        let _ = self.tx.send(()).await;              // 非实时：通知 flusher 稍后刷
    }
    result
}
```

代理的 `handle_writer` 把帧拆成「头（`Flush::No` 合并）+ payload（按 realtime 决定）」两次写：头部永远合并（[src/broker.rs:2255](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L2255)），只有 payload 由 `frame.realtime.into()` 决定（[src/broker.rs:2259](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L2259)）。对 `Prepared` 帧（如 ACK）同样用 `frame.realtime.into()`（[src/broker.rs:2229](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L2229)）——所以 ACK 也继承实时紧迫性。

而 ACK 的 `realtime` 来自 `send_ack!` 宏调用处传入的 `qos.is_realtime()`（例如 [src/broker.rs:1994](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1994) 的 `send_ack!(ERR_ACCESS, qos.is_realtime())`）。

#### 4.3.4 代码实践

**目标**：追踪一条 `QoS::Realtime` 消息从发送到对端写出的完整刷新路径。

**操作步骤**（源码追踪型）：

1. 从 [src/broker.rs:349-368](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L349-L368) 出发，确认 `qos.is_realtime()` 被传入 `send!` 宏。
2. 跳到 [src/broker.rs:129-137](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L129-L137)，确认它写入 `FrameData.realtime`。
3. 跳到接收方写出端 [src/broker.rs:2259](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L2259)，确认 `frame.realtime.into()` 被用作 payload 的 `Flush`。
4. 跳到 [src/comm.rs:15-24](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/comm.rs#L15-L24) 与 [src/comm.rs:66-75](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/comm.rs#L66-L75)，确认 `Instant` 触发同步 `flush()`。

**预期结果**：你能画出「QoS 位 → FrameData.realtime → Flush::Instant → writer.flush()」这条不间断的链，没有任何环节丢掉实时性。

#### 4.3.5 小练习与答案

**练习**：为什么头部用 `Flush::No` 而只有 payload 看 realtime？

**答案**：头部（6 字节 + sender/topic）总是紧跟着 payload 一起发出，把头部和 payload 合并到同一次系统调用更高效。头部先入缓冲（`Flush::No`），随后 payload 的 `Flush` 策略一次性决定整批数据的刷新时机——这样实时消息只需一次 flush 就把「头+体」都送出去，非实时消息则借机把多帧合并成一次 flush 提升吞吐。

### 4.4 rt feature：互斥锁的实时安全切换

#### 4.4.1 概念说明

`rt` 是一个**编译期开关**（[Cargo.toml:88](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/Cargo.toml#L88)，`rt = ["dep:parking_lot_rt"]`）。启用它，BUS/RT 内部用的同步互斥锁就从 `parking_lot`（会自旋）换成 `parking_lot_rt`（不自旋、实时安全）。README 把它列在 [「Real-time safety」](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/README.md#L50) 一节。

注意区分两类锁：

- **同步锁**（`SyncMutex`，parking_lot / parking_lot_rt）：保护 `BrokerDb` 的路由表、`BusRtClient` 的排除列表等。`rt` feature 切换的就是这类锁。
- **异步锁**（`tokio::sync::Mutex`）：如 `rpc_client: Arc<Mutex<Option<RpcClient>>>`（[src/broker.rs:663](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L663)），由 tokio 管理，`.await` 让出，不受 `rt` 影响。

#### 4.4.2 核心流程

`rt` 的切换发生在**每个模块顶部的 import**，用 `#[cfg(feature = "rt")]` 把 `SyncMutex` 这个别名指向不同的具体类型。所有用 `SyncMutex<T>` 的字段因此整体换实现，业务代码一行不用改。

以 `broker.rs` 为例，影响面覆盖三张路由表与客户端内部状态：

```
BrokerDb.clients       : SyncMutex<HashMap<..>>   ┐
BrokerDb.broadcasts    : SyncMutex<BroadcastMap>  ├ rt 下 = parking_lot_rt::Mutex
BrokerDb.subscriptions : SyncMutex<SubMap>        ┘   (不自旋)
BusRtClient.secondaries: SyncMutex<HashSet>       ┐
BusRtClient.exclusions : SyncMutex<AclMap>        ┘
AaaMap                 : Arc<SyncMutex<HashMap>>  ─┘
```

#### 4.4.3 源码精读

切换点在 `broker.rs` 顶部（[src/broker.rs:19-22](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L19-L22)）：

```rust
#[cfg(not(feature = "rt"))]
use parking_lot::Mutex as SyncMutex;      // 默认：会自旋
#[cfg(feature = "rt")]
use parking_lot_rt::Mutex as SyncMutex;   // rt：不自旋，实时安全
```

同样的 `cfg` 切换还出现在 `rpc/async_client.rs`、`ipc.rs`、`cursors.rs`、`sync/rpc.rs` 等模块（如 [src/rpc/async_client.rs:14](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/async_client.rs#L14)、[src/ipc.rs:19](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs#L19)、[src/cursors.rs:6](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/cursors.rs#L6)），保证全链路的同步锁一致切换。

除此之外，`lib.rs` 里还有一组**类型别名**专门服务于**同步客户端**（`ipc-sync`/`rpc-sync` feature 下的 `SyncEventChannel`），在 `rt` 下指向 `rtsc::pi` 的实时原语（[src/lib.rs:74-86](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L74-L86)）：

```rust
#[cfg(all(any(feature = "ipc-sync", feature = "rpc-sync"), feature = "rt"))]
type RawMutex = rtsc::pi::RawMutex;
#[cfg(all(any(feature = "ipc-sync", feature = "rpc-sync"), feature = "rt"))]
type Condvar = rtsc::pi::Condvar;
#[cfg(all(any(feature = "ipc-sync", feature = "rpc-sync"), not(feature = "rt")))]
type RawMutex = parking_lot::RawMutex;
#[cfg(all(any(feature = "ipc-sync", feature = "rpc-sync"), not(feature = "rt")))]
type Condvar = parking_lot::Condvar;
```

这组别名喂给同步事件通道（[src/lib.rs:84](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L84)）：

```rust
pub type SyncEventChannel = rtsc::channel::Receiver<Frame, RawMutex, Condvar>;
```

即：同步客户端的事件通道底层锁，也随 `rt` 一起切换。`rtsc` 依赖见 [Cargo.toml:56](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/Cargo.toml#L56)，`parking_lot_rt` 依赖由 `rt` feature 拉入（[Cargo.toml:54](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/Cargo.toml#L54)）。

> 小结：`rt` feature 通过两处 `cfg` 共同起作用——(a) 各模块 import 的 `SyncMutex` 别名切换（覆盖 broker/ipc/rpc/cursors/sync 的内部锁）；(b) `lib.rs` 里 `RawMutex`/`Condvar` 别名切换（覆盖同步客户端的事件通道原语）。两者都在 `rt` 下改用不含自旋的实时安全实现。

#### 4.4.4 代码实践

**目标**：观察 `rt` feature 如何改变编译产物里的锁类型。

**操作步骤**（编译对比型）：

1. 不开 `rt`：`cargo build --features broker`（或 `cargo build --features full`）。
2. 开 `rt`：`cargo build --features "broker,rt"`。
3. 两次都加 `--emit mir` 或用 `cargo expand` 看 `broker.rs`，确认 `SyncMutex` 一处解析为 `parking_lot::Mutex`、另一处为 `parking_lot_rt::Mutex`。

**需要观察的现象 / 预期结果**：开 `rt` 后，`parking_lot_rt` 出现在依赖图里（`Cargo.lock`），`broker.rs` 顶部第 22 行的 import 生效、第 20 行被跳过。**待本地验证**具体 MIR 差异。

#### 4.4.5 小练习与答案

**练习 1**：`BrokerDb.rpc_client` 用的 `tokio::sync::Mutex` 会受 `rt` feature 影响吗？

**答案**：不会。`rt` 只切换同步锁 `SyncMutex`（parking_lot ↔ parking_lot_rt）。`rpc_client` 用的是 tokio 的异步 `Mutex`（[src/broker.rs:663](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L663)），它通过 `.await` 让出、本身不忙等，不参与这次切换。

**练习 2**：为什么「不自旋」对实时更安全，即便可能稍微降低吞吐？

**答案**：自旋在持锁者很快释放时省了一次内核挂起，吞吐高；但自旋时间不可预测，且会引发优先级反转。实时系统追求「最坏情况延迟有界」，宁可稍微损失平均吞吐，也要把 CPU 让出去（park）使延迟可预测。这正是 `parking_lot_rt` 的取舍。

### 4.5 AsyncAllocator 与 direct_alloc_limit：混合实时/高负载策略

#### 4.5.1 概念说明

这是本讲最精巧的设计，解决一个矛盾：**实时客户端要的是「小消息、快进快出」，但同一个代理上可能有人发几 MB 的大消息**。

`vec![0; len]` 对大 `len` 是一次「分配 + 清零」的同步重活——它**没有 `.await` 点**，会霸占当前 tokio worker 线程，直到分配完成。如果这个 worker 上还跑着别人的实时小消息任务，那些小消息就被卡住，实时性被破坏。

`AsyncAllocator` + `direct_alloc_limit` 的解法是一条**阈值分流**：

- 消息长度 ≤ `direct_alloc_limit`：直接 `vec![0; len]`，小而快，实时安全。
- 消息长度 > `direct_alloc_limit`：调用用户提供的 `AsyncAllocator::allocate(...).await`，把分配**挪到别处**（独立线程、阻塞线程池、缓冲池、非实时 CPU……）。由于 `handle_reader` 在这里 `.await`，当前 worker 可以去轮询其它任务——**大消息只拖慢它自己的发送方，不拖累别人的实时小消息**。

`Options::with_async_allocator` 的文档（[src/broker.rs:1320-1325](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1320-L1325)）说得很直白：大消息（超过阈值）只减慢发送方，对代理（及其它实时客户端）没有影响。

#### 4.5.2 核心流程

```
handle_reader 读到一帧, len = 载荷长度
        │
        ├── direct_alloc_limit 未设置?  ──是──> vec![0; len]  (默认, 全部直接分配)
        │
        └── direct_alloc_limit = L 已设置
                │
                ├── len <= L ──> vec![0; len]            (小消息: 直接分, 实时安全)
                │
                └── len  > L ──> async_allocator.allocate(name, len).await
                                            │
                                            ├── Some(buf) ─> 用返回的 buf 继续
                                            └── None      ─> Error::io("Refused to allocate")
        │
        v
   reader.read_exact(&mut buf)  把载荷字节填进 buf
```

关键点：`allocate(...).await` 是一个**真正的挂起点**。在它等待期间，tokio 调度器可以运行同一 worker 上的其它任务（别的客户端的小消息）。而同步 `vec![0; huge]` 没有挂起点，整段阻塞 worker。这一处 `.await` 就是「大消息不影响实时客户端」的实现机制。

#### 4.5.3 源码精读

trait 定义（[src/broker.rs:1044-1047](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1044-L1047)）——异步、可跨线程、带 `client_name` 便于实现按客户端的配额策略：

```rust
#[async_trait]
pub trait AsyncAllocator {
    async fn allocate(&self, client_name: &str, size: usize) -> Option<Vec<u8>>;
}
```

`Options` 建造者方法（[src/broker.rs:1320-1335](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1320-L1335)）——`direct_alloc_limit` 与 `async_allocator` **成对**设置：

```rust
pub fn with_async_allocator(
    mut self,
    direct_alloc_limit: usize,
    async_allocator: Arc<dyn AsyncAllocator + Send + Sync + 'static>,
) -> Self {
    self.direct_alloc_limit = Some(direct_alloc_limit);
    self.async_allocator.replace(async_allocator);
    self
}
```

`Broker` 通过 `Broker::create(&opts)` 读入这两个字段（[src/broker.rs:1341-1353](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1341-L1353)，[src/broker.rs:1350-1351](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1350-L1351)），并最终透传进 `handle_reader`（[src/broker.rs:1895-1896](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1895-L1896)）。

`handle_reader` 里的分流分支（[src/broker.rs:1932-1945](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1932-L1945)）——本模块心脏：

```rust
let mut buf: Vec<u8> = if let Some(limit) = direct_alloc_limit {
    if len as usize > limit {
        async_allocator                              // 大消息：挪走分配
            .unwrap()
            .allocate(client_name, len as usize)
            .await                                   // <-- 关键挂起点
            .ok_or_else(|| Error::io(format!("Refused to allocate {} bytes", len)))?
    } else {
        vec![0; len as usize]                        // 小消息：直接分
    }
} else {
    vec![0; len as usize]                            // 未配置：全部直接分
};
time::timeout(timeout, reader.read_exact(&mut buf)).await??;   // 填充载荷
```

> 注意：`async_allocator.unwrap()` 看似危险，但安全——因为 `direct_alloc_limit` 与 `async_allocator` 只能通过 `with_async_allocator` **同时**设置（两者字段私有、`Default` 时同为 `None`），不可能出现「设了 limit 没设 allocator」。

#### 4.5.4 代码实践（核心实践）

**目标**：实现一个把大块分配挪到 `spawn_blocking` 线程的 `AsyncAllocator`，配置 `direct_alloc_limit`，并说明大消息为何不阻塞实时小消息。

**操作步骤**：

1. 新建一个 example（示例代码，非项目原有），结构参照 `examples/inter_thread.rs`。
2. 如下实现 `OffThreadAllocator` 并用 `Options::with_async_allocator` 装配 `Broker`。

示例代码：

```rust
// 示例代码：自定义 AsyncAllocator + direct_alloc_limit
// required-features = ["broker"]；并需在 Cargo.toml 加 async-trait 依赖
use async_trait::async_trait;
use busrt::broker::{AsyncAllocator, Broker, Options};
use busrt::client::AsyncClient;
use busrt::QoS;
use std::sync::Arc;
use std::time::Duration;
use tokio::time::sleep;

// 把 vec![0; size] 放到阻塞线程池里跑，不占用 tokio 异步 worker
struct OffThreadAllocator;

#[async_trait]
impl AsyncAllocator for OffThreadAllocator {
    async fn allocate(&self, _client_name: &str, size: usize) -> Option<Vec<u8>> {
        tokio::task::spawn_blocking(move || vec![0u8; size]).await.ok()
    }
}

const SMALL: usize = 100;              // 小于阈值：broker 直接 vec![0; len]
const BIG: usize = 2 * 1024 * 1024;    // 2 MB，大于阈值：broker 走 allocator

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let allocator: Arc<dyn AsyncAllocator + Send + Sync> = Arc::new(OffThreadAllocator);
    // 阈值 64 KB：<= 它直接分配(实时安全)，> 它交给分配器(挪出运行时)
    let opts = Options::default().with_async_allocator(64 * 1024, allocator);
    let mut broker = Broker::create(&opts);

    let mut sender = broker.register_client("sender").await?;
    let mut receiver = broker.register_client("receiver").await?;
    let rx = receiver.take_event_channel().unwrap();

    tokio::spawn(async move {
        let big = vec![0u8; BIG];
        let small = vec![0u8; SMALL];
        loop {
            // 大消息：handle_reader 走 allocate().await 分支
            sender.send("receiver", big.clone().into(), QoS::No).await.unwrap();
            // 小消息：handle_reader 走 vec![0; len] 分支
            sender.send("receiver", small.clone().into(), QoS::No).await.unwrap();
            sleep(Duration::from_millis(100)).await;
        }
    });

    let mut n = 0;
    while let Ok(frame) = rx.recv().await {
        n += 1;
        println!("recv #{} payload_len={}", n, frame.payload().len());
        if n >= 10 { break; }
    }
    Ok(())
}
```

**需要观察的现象 / 预期结果**：

- 程序正常收发，`payload_len` 在 `100` 与 `2097152` 之间交替。
- 在 [src/broker.rs:1932-1945](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1932-L1945) 加日志或断点可确认：2 MB 消息命中 `async_allocator.allocate(...).await` 分支，100 字节消息命中 `vec![0; len]` 分支。
- **实时性论证（无需测量即可理解）**：因为大消息走 `allocate().await`，handle_reader 在此处挂起，tokio 能在同一 worker 上继续推进小消息任务；若不开 allocator，2 MB 的 `vec![0; 2_097_152]` 会同步阻塞 worker，小消息被迫等待。若要量化延迟差异，可在发送循环中给小消息打时间戳统计最坏情况延迟——**待本地验证**。

> 编译提示：`AsyncAllocator` 的 `#[async_trait]` 来自 `async-trait` crate；用户侧实现时需自行引入 `async-trait`（busrt 仅在 `rpc` feature 下重导出它，见 [src/lib.rs:7-8](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L7-L8)）。

#### 4.5.5 小练习与答案

**练习 1**：把上面 `OffThreadAllocator` 的 `spawn_blocking` 换成直接 `Some(vec![0u8; size])`（仍走 `async fn` 但不真正挪线程），实时性还有效吗？

**答案**：基本无效。`vec![0; size]` 即便包在 `async fn` 里，只要它本身不含 `.await`，执行时仍会同步阻塞当前 worker 线程直到分配完成。`async fn` 不会自动把 CPU 密集/同步阻塞操作变成非阻塞——必须显式交给 `spawn_blocking`（或真正的独立线程/池）。这正是 `AsyncAllocator` trait 让你**自己决定**如何挪走分配的原因。

**练习 2**：`allocate` 返回 `None` 会怎样？为什么传 `client_name`？

**答案**：返回 `None` 时，[src/broker.rs:1938](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1938) 把它转成 `Error::io("Refused to allocate N bytes")`，`handle_reader` 返回错误、该连接被终止。传 `client_name` 让分配器可以实施**按客户端的配额/限流策略**（比如「客户端 A 最多同时占 16MB」），从而防止单个大消息客户端耗尽内存。

## 5. 综合实践

把三根支柱串起来，设计一个「混合负载」嵌入 Broker：

1. 用 `Options::default().with_async_allocator(limit, allocator)` 创建 `Broker`（支柱③）。
2. 注册一个**实时**客户端 `rt_client`：它只收发小消息（< `limit`），并用 `QoS::Realtime` 发送（支柱①）。
3. 注册一个**大消息**客户端 `big_client`：周期性发送远超 `limit` 的几 MB 载荷。
4. （可选）分别用 `cargo build --features broker` 与 `--features "broker,rt"` 编译对比，体会支柱②。

**验收要点**：

- 大消息确实走 `AsyncAllocator` 分支（在 [src/broker.rs:1934](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1934) 加 trace 日志确认）。
- 实时小消息用 `QoS::Realtime`，在接收方写出端命中 `Flush::Instant`（[src/broker.rs:2259](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L2259)）。
- 能用自己的话回答：即便大消息分配正在进行，为什么 `rt_client` 的小消息仍能被及时处理？（答：大分配在 `allocate().await` 挂起，worker 得以轮询小消息任务；且 `rt` 下路由表锁不自旋、小消息刷新走 `Instant`。）

## 6. 本讲小结

- **QoS 用两个正交位**编码：位 0 `needs_ack`（要否 ACK）、位 1 `is_realtime`（要否立即刷新）；`is_realtime = q & 0b10`（[src/lib.rs:361-370](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L361-L370)）。
- **实时位端到端透传**：发送方设的 `realtime` 打包进 `FrameData`，接收方 `handle_writer` 经 `From<bool> for Flush` 翻译成 `Flush::Instant` 触发同步刷新（[src/comm.rs:15-24](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/comm.rs#L15-L24)、[src/broker.rs:2259](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L2259)）。
- **rt feature 切换同步锁**：经各模块 `cfg` import 把 `SyncMutex` 从 `parking_lot` 换成不自旋的 `parking_lot_rt`；`lib.rs` 的 `RawMutex`/`Condvar` 别名则服务同步客户端的事件通道（[src/broker.rs:19-22](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L19-L22)、[src/lib.rs:74-86](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L74-L86)）。自旋锁对实时不安全（延迟毛刺、优先级反转）。
- **AsyncAllocator + direct_alloc_limit 阈值分流**：小消息直接 `vec![0; len]`（快、实时安全），大消息走 `allocate().await`（关键挂起点）挪出运行时，于是大消息只拖慢发送方、不拖累别人的实时小消息（[src/broker.rs:1932-1945](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L1932-L1945)）。
- 三者**正交**：QoS Realtime 管「刷新时机」、rt feature 管「锁是否自旋」、AsyncAllocator 管「大分配是否霸占线程」，可独立启用、组合使用。

## 7. 下一步学习建议

- 想看「流式分块传大块数据」的更上层封装，进入 **u7-l2 游标 cursors**：`cursors::Cursor` 把「产生一大段数据」拆成多次小块 `next`/`next_bulk`，与本章的「单条大消息」思路互补。
- 想理解同步客户端如何把实时原语（`rtsc::pi`、`SyncEventChannel`）用在真实代码里，进入 **u7-l4 同步客户端**，对照本章 4.4 的 `lib.rs` 别名。
- 想再钻一层「写出端的延迟 vs 吞吐」权衡，回顾 **u4-l3 TtlBufWriter**，结合本章 `Flush` 三态加深理解。
