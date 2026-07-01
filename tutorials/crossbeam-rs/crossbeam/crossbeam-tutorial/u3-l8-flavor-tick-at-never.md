# tick / at / never：时间与特殊 flavor

## 1. 本讲目标

crossbeam-channel 不只有「装消息」的通道（`array`/`list`/`zero`），还有三种**只读**的特殊 flavor：

- `at` / `after`：在某个绝对时刻**投递一次**消息；
- `tick`：以固定周期**反复投递**消息；
- `never`：**永不投递**，是 select 的「兜底空通道」。

学完本讲，你应当能够：

1. 说出这三种 flavor 各自的语义、构造方式与「消息是什么」；
2. 读懂 `flavors/at.rs`、`flavors/tick.rs`、`flavors/never.rs` 三个文件的实现，理解 `try_recv`/`recv`/`deadline` 的差异；
3. 解释 `at` 的「恰好一次」与 `tick` 的「周期推进」背后的原子操作差异；
4. 说明 `never` 在 `select!` 中的作用——尤其是「可选超时」这个经典模式；
5. 理解三种 flavor 如何通过 `SelectHandle::deadline()` 与 select 算法协作，做到「到点即醒」而无需轮询。

## 2. 前置知识

本讲建立在你已经学完以下讲义的基础上（不会再重复其结论）：

- **u3-l3 flavors 架构与 SelectHandle**：你已经知道 `ReceiverFlavor` 是一个枚举，`recv`/`try_recv` 等公共方法靠 `match flavor` 把调用派发到具体实现；所有 flavor 都实现统一的 `SelectHandle` trait；操作结果暂存在 `Token` 联合体里。本讲正是在这个派发框架里**补充三种新的 receiver flavor**。
- **u2-l3 AtomicCell**：`tick` flavor 用 `AtomicCell<Instant>` 存「下次投递时刻」。你会看到 `AtomicCell` 如何处理一个 16 字节的非平凡类型。
- **u3-l1 channel 总览**：你已经熟悉 `bounded`/`unbounded`/`bounded(0)` 三类普通通道，以及错误类型家族（`TryRecvError::Empty`、`RecvTimeoutError::Timeout` 等）。

补充两个本讲用到的标准库概念：

- **`std::time::Instant`**：一个单调递增的时间点。本讲里它有「双重身份」——既是「消息该何时投递」的判定依据，**也是被投递出去的消息本身**（你 `recv()` 到的值就是一个 `Instant`）。
- **`std::time::Duration`**：一段时间间隔。`tick(dur)` 与 `after(dur)` 都以它为输入。

> 一个贯穿全讲的关键认知：这三种 flavor **没有发送端**。文件开头都写着同一句话——「Messages cannot be sent into this kind of channel; they are materialized on demand.」（消息不能被发送进来，而是按需「物化」出来）。也就是说，通道内部没有任何缓冲区去装别人发来的数据，消息是在 `recv` 触发时由 flavor 自己「算」出来的。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [crossbeam-channel/src/flavors/at.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/at.rs) | 「在指定时刻投递一次」的 flavor，由 `at(Instant)` / `after(Duration)` 创建。 |
| [crossbeam-channel/src/flavors/tick.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/tick.rs) | 「周期性投递」的 flavor，由 `tick(Duration)` 创建。 |
| [crossbeam-channel/src/flavors/never.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/never.rs) | 「永不投递」的 flavor，由 `never()` 创建。 |
| [crossbeam-channel/src/channel.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs) | 公共构造函数 `at`/`after`/`tick`/`never`，以及 `ReceiverFlavor` 枚举与派发外壳。 |
| [crossbeam-channel/src/select.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs) | `Token` 联合体定义，以及 select 算法里消费 `deadline()` 的逻辑。 |
| [crossbeam-channel/src/utils.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/utils.rs) | `sleep_until(Option<Instant>)`：阻塞到某时刻、或近似「永久阻塞」的工具。 |
| [crossbeam-channel/examples/stopwatch.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/examples/stopwatch.rs) | 真实示例：用 `tick` 每秒打印耗时，配合 `select!` 监听 Ctrl-C。 |

## 4. 核心概念与源码讲解

本讲按三个 flavor 拆成三个最小模块，再加一个「它们如何融入 select」的整合模块。

### 4.1 at：单次定时投递 Instant

#### 4.1.1 概念说明

`at` flavor 解决的问题是：**我想要一个通道，它在某个确定时刻 `when` 准时「收到」一条消息，而且只收一条，之后这条消息就消失了。**

它对外由两个构造函数暴露（见 4.1.3）：

- `at(when: Instant)`：直接给一个**绝对时刻**；
- `after(duration: Duration)`：给一个**相对时长**，内部换算成 `Instant::now() + duration`。

二者本质相同，最终都落到同一个 `flavors::at::Channel`。它的语义是「容量为 1、永不断开、恰好投递一条消息」，消息内容就是那个投递时刻 `when` 本身。

为什么消息是 `Instant` 而不是 `()`？因为 `after` 的典型用途是**超时**：你不仅想知道「到点了」，还想知道「约定的是哪个点」（例如做时间对齐、统计延迟）。返回 `Instant` 让这个信息无损传递。

#### 4.1.2 核心流程

`at` 的状态极简，只有两个字段：一个固定的投递时刻 `delivery_time`，和一个标记「消息是否已被取走」的 `received`。它的核心是一个**「先到先得、唯一赢家」**协议：

```
try_recv（非阻塞）:
  1. 若 received 已为 true            → Empty（消息早被别人取走了）
  2. 若 now < delivery_time           → Empty（还没到点）
  3. received.swap(true, SeqCst):
       返回 false（我是第一个）        → Ok(delivery_time)   ← 唯一赢家
       返回 true （被人抢先）          → Empty

recv（阻塞）:
  1. 若 received 已为 true            → 直接 Timeout（不会再有第二条消息）
  2. 循环 sleep，直到 now >= delivery_time 或外部 deadline 到
  3. received.swap(true, SeqCst) 抢消息:
       第一个                          → Ok(delivery_time)
       输掉竞争                        → 永久阻塞（sleep_until(None)）
```

关键点：`received.swap(true, SeqCst)` 是一个**原子 test-and-set**，保证全局只有一个 `recv` 能拿到这条消息。这与 `tick`（每个周期都有一条新消息）形成鲜明对比——`at` 的消息是**不可再生的稀缺资源**。

注意第 3 步里输掉竞争的分支会**永久阻塞**：因为 `at` 通道「永不断开」，一个来迟的 `recv` 在逻辑上是在「等下一条消息」，而下一条永远不会来。所以它选择死等（`unreachable!()` 只是给类型检查器一个交代，表示这里事实上不会返回）。

#### 4.1.3 源码精读

先看公共构造函数。`after` 把相对时长换算成绝对时刻，若加法溢出（`checked_add` 返回 `None`）则退化为 `never()`——一个很务实的降级：

[after 的实现](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L181-L188) 把 `deadline` 包成 `At` flavor。`at` 函数与之几乎相同，只是跳过了加法：

[at 的实现](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L232-L236) 直接用传入的 `Instant` 构造。

再看 flavor 内部。状态定义与 token 类型在文件顶部：

[at.rs:16 AtToken 类型别名](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/at.rs#L16) —— `AtToken = Option<Instant>`，select 成功时把投递时刻塞进 `Token.at`。

[at.rs:19-25 Channel 结构](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/at.rs#L19-L25) —— `delivery_time: Instant`（不可变）+ `received: AtomicBool`。

非阻塞读取是上面流程图的直接翻译：

[at.rs:39-59 try_recv](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/at.rs#L39-L59) —— 注意三道关：Relaxed 快速判重 → 时间未到判 Empty → `swap(true, SeqCst)` 抢夺。注释特意说明前两步用 `Relaxed` 只是个「乐观可选检查」，真正的同步由第 52 行的 `SeqCst` swap 承担。

阻塞读取稍微复杂，因为它还要支持外部 deadline：

[at.rs:63-98 recv](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/at.rs#L63-L98) —— 第 90 行的 `swap` 决定赢家；第 95 行的 `utils::sleep_until(None)` + 第 96 行 `unreachable!()` 就是「输掉竞争后永久阻塞」的实现。

最后是一个对 select 至关重要的方法：

[at.rs:160-167 deadline()](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/at.rs#L160-L167) —— 只要消息还没被取走，就告诉 select「我最早在 `delivery_time` 可能有效」；一旦消息已被取走，返回 `None`（表示「别再为我醒来了」）。这个方法在 4.4 节会再次出现。

#### 4.1.4 代码实践

**实践目标**：验证 `after` 投递的消息就是「构造时刻 + duration」，且只投递一次。

**操作步骤**（示例代码，可在任意依赖 `crossbeam-channel` 的 crate 里运行）：

```rust
use std::time::{Duration, Instant};
use crossbeam_channel::after;

let start = Instant::now();
let r = after(Duration::from_millis(100));

// 第一次 recv：会阻塞约 100ms 后返回，消息就是约定的时刻
let msg = r.recv().unwrap();
assert!(msg >= start + Duration::from_millis(99));

// 第二次 recv：消息已被取走，at 通道永不断开 → 永久阻塞！
// 所以这里只能用 try_recv 观察到 Empty：
assert!(r.try_recv().is_err()); // Err(Empty)
```

**需要观察的现象**：第一次 `recv` 约在 100ms 后返回；`try_recv` 返回 `Err(TryRecvError::Empty)`。

**预期结果**：断言通过。注意**千万不要**在第二次调用阻塞版 `recv()`，否则线程会永久挂起（这正是源码第 95 行 `sleep_until(None)` 的行为）。

> 待本地验证：不同平台的调度精度不同，`msg` 与 `start + 100ms` 的偏差可能从几微秒到几毫秒不等。仓库自带的 `after` doctest 用了一个 60ms 容差的 `eq` 函数来容忍这种偏差，你可参考。

#### 4.1.5 小练习与答案

**练习 1**：如果把一个 `at(when)` 通道的 `Receiver` `clone()` 出两份，分别在两个线程里同时 `recv()`，会发生什么？两个线程都能拿到消息吗？

> **答案**：不能。`clone()` 共享同一份 `Arc<flavors::at::Channel>`（见 [channel.rs:1205 的 Clone 实现](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L1199-L1212) 中 `At(chan) => At(chan.clone())`），即共享同一个 `received: AtomicBool`。`swap(true, SeqCst)` 是原子的 test-and-set，只有先到的线程拿到 `false`（成功），另一个拿到 `true` 后进入永久阻塞分支。所以**恰好一个线程拿到消息**。

**练习 2**：`at.rs` 里 `recv` 的最后为什么是 `sleep_until(None)` 紧跟 `unreachable!()`，而不是直接返回一个错误？

> **答案**：`at` 通道的契约是「容量 1、永不断开」。输掉竞争的 `recv` 在逻辑上是在等「下一条消息」，但下一条永不会来，且通道也永不会断开（没有 `Disconnected` 可返回）。所以语义上它应当一直阻塞。`sleep_until(None)` 实现了「近似永久阻塞」（每轮睡 1000s，见 4.4 节的 `utils::sleep_until`），`unreachable!()` 仅用于安抚类型系统，标记此处事实上无返回值。

### 4.2 tick：周期定时与 AtomicCell 时间戳

#### 4.2.1 概念说明

`tick` flavor 解决的是**周期性触发**：每隔 `duration` 就「来一条消息」，可以一直收下去。典型用途是心跳、轮询节奏、定时刷新。

由 `tick(duration: Duration)` 创建，返回 `Receiver<Instant>`。每次 `recv()` 拿到的消息是「这次 tick 约定的投递时刻」。

它和 `at` 最大的区别在于：**消息是可再生的**。每被取走一条，flavor 就把「下次投递时刻」往后推一个 `duration`，于是源源不断。这要求「下次投递时刻」必须**可变且可被多线程原子修改**——这就是 `AtomicCell<Instant>` 登场的理由。

#### 4.2.2 核心流程

```
try_recv:
  loop:
    delivery_time = load()              // 读「下次投递时刻」
    if now < delivery_time              → Empty（还没到点）
    next = now + duration
    if CAS(delivery_time → next) 成功   → Ok(旧 delivery_time)   ← 推进成功
    否则（被人抢先推进了）              → 回到 loop 重试

recv:
  loop:
    delivery_time = load()
    若有外部 deadline 且更早            → 睡到它，返回 Timeout
    next = max(delivery_time, now) + duration
    if CAS(delivery_time → next) 成功:
       if now < delivery_time           → sleep 到 delivery_time（这条还没到点）
       return Ok(旧 delivery_time)
    否则                                → 重试
```

这里有一个**非常重要的设计决策**：下次投递时刻不是简单地「旧值 + duration」，而是 `delivery_time.0.max(now) + duration`（源码第 120 行）。它的含义是：

- 如果接收者很准时（`now ≈ delivery_time`），下一拍就是 `delivery_time + duration`，节奏稳定；
- 如果接收者**迟到了**（`now >> delivery_time`，比如某次处理很慢），下一拍取 `now + duration`，**直接跳过被错过的那几拍**，而不是疯狂补发。

也就是说，`tick` 的策略是「**宁可丢拍，也不积压**」。这避免了「一次慢、之后被排山倒海的补发消息淹没」。

CAS 失败（`compare_exchange` 返回 `Err`）只意味着「别的接收者刚把它推进了」，于是 `loop` 重新读取最新的 `delivery_time` 再试。注意：和 `at` 不同，这里**没有永久阻塞分支**——因为 tick 永远有下一拍，竞争失败重试即可。

#### 4.2.3 源码精读

状态定义与一个看似古怪的包装类型：

[tick.rs:61-67 Channel 结构](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/tick.rs#L61-L67) —— `delivery_time: AtomicCell<Align<Instant>>` + `duration: Duration`。

那个 `Align<T>` 是本讲的「彩蛋」，它直接呼应了 u2-l3 的 AtomicCell：

[tick.rs:19-36 Align 包装类型](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/tick.rs#L19-L36) —— 在支持 128 位原子的架构（x86_64/aarch64/riscv64/powerpc64/s390x/loongarch64 等）上强制 `repr(align(16))`。

为什么？`Instant` 在 64 位平台上通常是 16 字节（两个 64 位字段），放不进单个 64 位原子。按 u2-l3 的规则，`AtomicCell<Instant>` 本会退化为**全局序列锁**的慢路径。但在支持 128 位原子指令（如 x86_64 的 `CMPXCHG16B`）的架构上，只要数据 **16 字节对齐**，`AtomicCell` 就能走 lock-free 的 128 位原子快路径。`Align` 就是为了补足这个对齐要求。文件里的测试 [is_lock_free](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/tick.rs#L38-L58) 正是用来钉住「在支持的架构上确实是 lock-free」这一性质（miri/loom/sanitize 环境下除外）。

非阻塞读取实现上面流程图：

[tick.rs:81-98 try_recv](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/tick.rs#L81-L98) —— 关键是第 90-94 行：CAS 把 `delivery_time` 推进到 `now + duration`，成功后返回**旧值**（这一拍约定的时刻）。

阻塞读取多了一个外部 deadline 的处理，并体现了「丢拍」策略：

[tick.rs:102-130 recv](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/tick.rs#L102-L130) —— 第 118-121 行的 `delivery_time.0.max(now) + self.duration` 就是「迟到则跳拍」的公式，可写成：

\[
\text{next} = \max(\text{delivery\_time},\ \text{now}) + \text{duration}
\]

select 协作入口：

[tick.rs:180-182 deadline()](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/tick.rs#L180-L182) —— 永远返回 `Some(下次投递时刻)`，因为 tick 永远有下一拍。

#### 4.2.4 代码实践

**实践目标**：观察 `tick` 的「丢拍」行为——当接收者睡过头时，错过的拍不会被补发。

**操作步骤**（示例代码）：

```rust
use std::time::{Duration, Instant};
use crossbeam_channel::tick;

let ms = |ms| Duration::from_millis(ms);
let start = Instant::now();
let r = tick(ms(100));

let t1 = r.recv().unwrap();            // 约在 100ms
assert!(t1 >= start + ms(99) && t1 < start + ms(160));

std::thread::sleep(ms(500));           // 睡过头，错过 200/300/400/500ms 共 4 拍

let t2 = r.recv().unwrap();            // 紧接着返回（不补发）
// t2 应当 ≈ 600ms 这条（因为 next = max(200ms, now≈600ms) + 100ms 之后那拍）
println!("t2 - start = {:?}", t2.duration_since(start));

let t3 = r.recv().unwrap();            // 之后恢复正常节奏
println!("t3 - start = {:?}", t3.duration_since(start));
```

**需要观察的现象**：`t2` 在「睡醒后立刻」返回（因为投递时刻已过，`try_recv` 路径立即成功），而 `t3` 与 `t2` 之间恢复为约 100ms 间隔。你**不会**看到 200/300/400/500ms 这几条被连续补发出来。

**预期结果**：打印出的 `t2` 大致在 600ms 附近，`t3 - t2` 约 100ms。具体数值与调度有关。

> 待本地验证：由于 `max(delivery_time, now)` 的取值依赖线程被唤醒的精确时刻，`t2` 的确切读数会波动；重点观察「没有补发积压」这一性质，而非某个绝对数值。仓库的 `tick` doctest 同样用容差比较，可作参照。

#### 4.2.5 小练习与答案

**练习 1**：`tick` 的 `try_recv` 用的是 `compare_exchange`，而 `at` 的 `try_recv` 用的是 `swap`。为什么 `tick` 必须用 CAS 而不能用 `swap`？

> **答案**：`tick` 推进的是**「下次投递时刻」**，推进必须基于「我读到的旧时刻」来计算新值（`now + duration`，且要判断 `now < delivery_time`）。如果用 `swap` 无脑覆盖，就可能用陈旧的旧值算出一个**倒退**或错乱的时刻（例如把已经被别人推进到很远的时刻又改回较小的值）。CAS 保证「只有当 `delivery_time` 仍然是我读到的那值时，我才推进」，从而维护时刻的单调推进。`at` 则只是把一个布尔从 false 翻成 true，任何竞争者翻的结果都一样，所以 `swap` 足矣。

**练习 2**：为什么 `tick` 的 `Channel` 用 `Arc` 共享，而 `never` 的 `Channel` 在 `clone` 时却是 `Channel::new()` 全新一个（见 [channel.rs:1207](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L1199-L1212)）？

> **答案**：`tick` 的多个 `Receiver` 克隆必须共享同一份「下次投递时刻」与节拍，否则就不是一个通道了，所以用 `Arc`。而 `never` 通道**没有任何状态**（只有一个 `PhantomData`，见 4.3.3），所有 `never` 通道行为完全相同——都是永不投递。所以克隆时直接新建一个零成本的空壳即可，无需共享。这也解释了为什么 `same_channel` 对两个 `never` 通道永远返回 `true`（[channel.rs:1167](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L1160-L1170)）。

### 4.3 never：始终 Empty 的兜底通道

#### 4.3.1 概念说明

`never` 是一个「什么都不做」的通道：它永远不会有消息，也永远不会断开。单看它没什么用，它的价值**完全体现在 `select!` 里**——作为一个可以临时占位的「空分支」。

最经典的场景是**「可选超时」**：你想给某个操作加超时，但超时时长是运行期决定的，可能是 `Some(dur)` 也可能是 `None`（表示「这次不要超时」）。`select!` 要求所有分支在编译期写死，你不能在运行期动态增删分支。这时用 `never()` 兜底：

```rust
let timeout = duration.map(after).unwrap_or_else(never);
//              Some(dur) → after(dur)      None → never()
select! {
    recv(r) -> msg => { /* 处理消息 */ }
    recv(timeout) -> _ => { /* 超时；若 timeout 是 never()，这一分支永不触发 */ }
}
```

`never()` 让「无超时」也能塞进同一个 `select!` 结构里，而不会改变代码骨架。这是它在仓库文档里被强调的用法（见 [channel.rs:238-274 never 的文档与示例](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L238-L279)）。

#### 4.3.2 核心流程

`never` 的实现是三个 flavor 里最短的，因为每个方法都是常量：

```
try_recv   → Err(Empty)            // 永远空
recv       → sleep_until(deadline) // 阻塞到外部 deadline（或永久）
             Err(Timeout)
read       → Err(())               // select 取值时失败
is_empty   → true
is_full    → true                  // 容量 0，既「满」又「空」
len        → 0
capacity   → Some(0)

SelectHandle:
  try_select → false               // 永不就绪
  deadline   → None                // 别为我唤醒
  is_ready   → false
```

`is_empty` 与 `is_full` 同时为 `true` 看似矛盾，其实和零容量 rendezvous 通道的口径一致（u3-l1 讲过：零容量通道「总是空、也总是满」）。区别在于 rendezvous 通道在 send/recv 配对时能成交，而 `never` 没有发送端，永远不会成交。

#### 4.3.3 源码精读

[never.rs:19-30 Channel 定义与构造](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/never.rs#L19-L30) —— `Channel<T>` 只有一个 `PhantomData<T>`，`new()` 是 `const fn`。注意它是**泛型** `Channel<T>`（不像 `at`/`tick` 写死 `Instant`），因为 `never<T>()` 可以是任意 `T` 的接收端——反正永远不会真的产出 `T`。

[never.rs:34-43 try_recv / recv](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/never.rs#L34-L43) —— `recv` 把阻塞完全委托给 `utils::sleep_until(deadline)`：传入 `None` 则近似永久阻塞，传入某时刻则睡到那时返回 `Timeout`。

状态查询都是常量：

[never.rs:51-73 is_empty/is_full/len/capacity](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/never.rs#L51-L73) —— `capacity` 返回 `Some(0)`，与「零容量」口径一致。

SelectHandle 实现里最有意义的是 `deadline()` 永远返回 `None`：

[never.rs:82-103 SelectHandle 关键方法](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/never.rs#L76-L112) —— `try_select` 恒 `false`、`deadline` 恒 `None`、`is_ready` 恒 `false`。它对 select 算法「完全透明」：既不参与抢占，也不提供唤醒时机。

公共构造函数是 `const fn`，因此可在常量上下文里使用：

[never 的实现](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L275-L279) —— 注意它**不经过 `counter`**（不像 array/list/zero），因为没有任何共享状态需要引用计数。

#### 4.3.4 代码实践

**实践目标**：用 `never()` 实现「可选超时」模式，验证 `None` 时 select 不会误触发超时分支。

**操作步骤**（示例代码）：

```rust
use std::time::Duration;
use crossbeam_channel::{after, never, select, unbounded};

fn work(timeout: Option<Duration>) {
    let (s, r) = unbounded::<i32>();

    std::thread::spawn(move || {
        std::thread::sleep(Duration::from_millis(50));
        let _ = s.send(42);
    });

    // 关键：Some → after，None → never
    let t = timeout.map(after).unwrap_or_else(never);

    select! {
        recv(r) -> msg => println!("收到: {:?}", msg),
        recv(t) -> _ => println!("超时"),
    }
}

fn main() {
    work(None);                         // 不会超时，必定收到 42
    work(Some(Duration::from_millis(1)));// 几乎必定超时
}
```

**需要观察的现象**：第一次 `work(None)` 打印「收到: Ok(42)」，超时分支**从不**触发；第二次 `work(Some(1ms))` 多半打印「超时」。

**预期结果**：如上。这个例子完整复现了仓库 [never 文档示例](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L248-L274) 的意图。

#### 4.3.5 小练习与答案

**练习 1**：在 `select!` 里同时放一个 `never()` 分支和一个真实通道分支，select 算法会不会因为 `never` 而「卡住」或变慢？

> **答案**：不会。select 的第一阶段会遍历所有分支调用 `try_select`（[select.rs:207-211](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L206-L211)）。`never` 的 `try_select` 恒返回 `false`，对遍历毫无影响。在计算最早唤醒时刻时（4.4 节），`never` 的 `deadline()` 返回 `None`，被 `Option::min` 逻辑忽略。所以 `never` 对 select 而言是一个**零开销的占位符**。

**练习 2**：`never` 的 `Channel<T>` 为何要带泛型 `T`？明明它永远不产出值。

> **答案**：因为 `Receiver<T>` 整体是泛型的，`ReceiverFlavor::Never(flavors::never::Channel<T>)`（[channel.rs:746](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L729-L747)）必须能装下任意 `T`，才能让 `never::<T>()` 返回 `Receiver<T>` 与其他分支在同一 `select!` 里对齐类型（例如和 `Receiver<MyMsg>` 一起 select）。`PhantomData<T>` 让类型跟上而不占用任何运行期空间。

### 4.4 三种 flavor 如何融入 select

#### 4.4.1 概念说明

前面三个模块都出现了 `deadline()` 方法。它是 `SelectHandle` trait 的一个方法，作用是告诉 select 算法：「**我这个操作最早在哪个时刻可能就绪**」。

这一点是 `at`/`tick` 区别于普通通道的核心。普通通道（array/list/zero）不知道自己何时会有消息，`deadline()` 返回 `None`——select 只能用条件变量被动等待别人唤醒。而 `at`/`tick` 是**时间驱动**的，它们明确知道下一次就绪时刻，于是 select 可以**主动算出一个睡眠上限**，到点醒来重新尝试。这让「带超时的 select」既高效（不轮询）又精确。

#### 4.4.2 核心流程

select 算法（u3-l9 会完整讲解，这里只看与时间有关的部分）在注册完所有操作、发现没有立刻就绪的之后，会：

```
deadline = 外部 timeout（Now→直接返回；Never→None；At(when)→Some(when)）
for 每个操作 handle:
    if let Some(x) = handle.deadline():       // 问每个 flavor：你最早何时就绪？
        deadline = min(deadline, x)            // 取最早的
cx.wait_until(deadline)                        // 睡到那个时刻，期间仍可被别的线程唤醒
```

也就是 select 把「外部用户给的超时」和「每个 flavor 自己声明的就绪时刻」**合并取最小值**，作为本次阻塞的睡眠上限。

#### 4.4.3 源码精读

`Token` 联合体为每种 flavor 预留了一个字段，时间 flavor 占了 `at` 与 `tick`：

[select.rs:24-32 Token 结构](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L24-L32) —— `at: AtToken`、`tick: TickToken`、`never: NeverToken`（后者带 `#[allow(dead_code)]`，因为 `never` 永不产出值，这个字段确实不会被读，但保留以维持 `Token` 的统一布局）。

select 算法里消费 `deadline()` 的关键片段：

[select.rs:248-263 计算最早 deadline 并 wait_until](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L248-L263) —— 第 256-260 行遍历所有操作取最早时刻；第 263 行 `cx.wait_until(deadline)` 阻塞到该时刻或被唤醒。

派发外壳把 `Receiver::deadline()` 路由到具体 flavor：

[channel.rs:1460-1469 deadline() 派发](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L1460-L1469) —— `At`/`Tick`/`Never` 各自调自己的 `deadline()`，普通通道返回 `None`。

`utils::sleep_until` 是「阻塞到时刻」的底层工具，被多个 flavor 直接复用：

[utils.rs:43-56 sleep_until](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/utils.rs#L43-L56) —— `None` 时每轮睡 1000s（近似永久，但会循环重检，方便 loom 等模型检查终止），`Some(d)` 时睡到该时刻。

最后，`ReceiverFlavor` 把三种新 flavor 和三种普通 flavor 并列在一个枚举里，这是 u3-l3「flavor 派发架构」的完整面貌：

[channel.rs:729-747 ReceiverFlavor 枚举](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L729-L747) —— 注意 `At`/`Tick` 用 `Arc<...>`（可共享、有状态），`Never` 直接内联 `flavors::never::Channel<T>`（零状态）。

#### 4.4.4 代码实践：源码阅读型——追踪 select 如何为 `tick` 设定醒来时刻

**实践目标**：把本模块的「合并取最早 deadline」逻辑在脑中跑一遍，并用真实示例佐证。

**操作步骤**：

1. 打开仓库自带的 [examples/stopwatch.rs:39-55](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/examples/stopwatch.rs#L39-L55)。它做了 `tick(1s)` + 一个 Ctrl-C 通知通道的 `select!`。
2. 推理：当既没到 1 秒、也没人按 Ctrl-C 时，select 进入阻塞。此时它收集到的 deadline 来自谁？
   - `tick` 的 `deadline()` 返回 `Some(下次整秒)`；
   - `ctrl_c` 通道（`bounded(100)` 的 receiver）`deadline()` 返回 `None`；
   - 外部没有 timeout（`select!` 无 `default`）。
   - 合并结果 = `Some(下次整秒)`。
3. 于是 select 会精确地在 1 秒后醒来重新尝试 `tick`，**不需要每毫秒轮询**。

**需要观察的现象**：若在本机运行（仅非 Windows）`cargo run --example stopwatch`（需要 `signal_hook` 依赖，见 [crossbeam-channel/examples 目录](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/examples/stopwatch.rs)），每秒稳定打印一行耗时，且 CPU 占用接近 0（说明在睡眠而非自旋）。

**预期结果**：每秒一行 `Elapsed: N.NNN sec`，按 Ctrl-C 退出。若运行环境缺依赖，则此项**待本地验证**。

> 提示：本实践是「源码阅读型」，即便不运行，也应当能回答「select 靠什么知道何时为 tick 醒来」——答案是 `SelectHandle::deadline()` 与 `cx.wait_until`。

#### 4.4.5 小练习与答案

**练习**：如果一个 `select!` 同时含有 `recv(after(5s))` 和 `recv(tick(1s))`，且没有其他分支就绪，select 第一次会在大约多久后醒来？醒来后通常会发生什么？

> **答案**：约 1 秒。select 合并所有 `deadline()` 取最小值：`tick` 声明 1s、`after` 声明 5s，最小是 1s，所以 `wait_until(1s)`。醒来后重新遍历 `try_select`，`tick` 已到点且 CAS 推进成功 → 被选中。此后每秒 tick 触发一次；直到第 5 秒 `after` 也到点，那一轮 tick 与 after 都就绪，select（在非 biased 模式下）会随机选一个（公平性由 [select.rs:196-199 的 shuffle](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L196-L199) 保证）。

## 5. 综合实践

把三个 flavor 串起来，做一个「带心跳与总超时的工作循环」——这是 `at`/`tick`/`never` 在真实系统里最常见的组合形态。

**任务**：模拟一个事件处理循环。

- 主工作通道 `jobs`：由另一个线程往里塞「任务」（用 `unbounded`）；
- 心跳 `tick(200ms)`：每隔 200ms 打印一次「还在跑」，证明循环未卡死；
- 总超时 `after(1s)`：1 秒后整体退出；
- 用 `select!` 同时监听三者。

参考实现（示例代码）：

```rust
use std::time::Duration;
use crossbeam_channel::{after, select, tick, unbounded};

fn main() {
    let (jobs_s, jobs) = unbounded::<&'static str>();
    let heartbeat = tick(Duration::from_millis(200));
    let deadline = after(Duration::from_secs(1));

    std::thread::spawn(move || {
        // 模拟零星到来的任务
        for task in ["a", "b", "c"] {
            let _ = jobs_s.send(task);
            std::thread::sleep(Duration::from_millis(150));
        }
    });

    loop {
        select! {
            recv(jobs) -> msg => println!("处理任务: {:?}", msg),
            recv(heartbeat) -> _ => println!("  (心跳: 仍在运行)"),
            recv(deadline) -> _ => {
                println!("总超时，退出");
                break;
            }
        }
    }
}
```

**验证清单**：

1. 你应当看到若干「处理任务」与「心跳」交替出现，最后是「总超时，退出」。
2. 试着把 `deadline` 换成 `never()`（即把超时去掉）：循环将一直跑，直到你手动中断——这复用了 4.3.4 的「可选超时」思想。
3. 对照 4.4.3 的 `deadline()` 派发，解释 select 为何能在「任务」未来时仍按 200ms 节奏醒来打印心跳：因为 `tick` 的 `deadline()` 给了它一个 200ms 的唤醒上限。

> 待本地验证：输出顺序与任务线程的睡眠交错有关，不保证逐行一致；重点关注「心跳按 200ms 节奏出现、1 秒后准时超时退出」这两个时序性质。

## 6. 本讲小结

- `at`/`after` 是「在指定时刻投递一次」的 flavor，靠 `received: AtomicBool` 的 `swap(true, SeqCst)` 保证**全局恰好一个赢家**；输掉竞争的 `recv` 会永久阻塞，因为「永不断开」的通道不会有第二条消息。
- `tick` 是「周期投递」的 flavor，用 `AtomicCell<Align<Instant>>` 存「下次投递时刻」，靠 `compare_exchange` 推进节拍；采用 `max(delivery_time, now) + duration` 实现「**宁可丢拍、也不积压**」。`Align` 的 16 字节对齐让 `Instant` 在支持 128 位原子的架构上走 AtomicCell 的 lock-free 快路径（呼应 u2-l3）。
- `never` 是「永不投递」的零状态 flavor（仅一个 `PhantomData`），`try_select` 恒 `false`、`deadline` 恒 `None`；它纯粹是 `select!` 的占位分支，经典用法是 `duration.map(after).unwrap_or_else(never)` 实现「可选超时」。
- 三者都是**只读、无发送端**的 flavor，消息（`Instant`）由 flavor 按需「物化」；`at`/`tick` 的消息是 `Instant`，经 `mem::transmute_copy` 重解释为 `Receiver<T>` 的 `T`（因为公共构造函数只产出 `Receiver<Instant>`，该转换是安全的）。
- 时间 flavor 通过 `SelectHandle::deadline()` 与 select 算法协作：select 合并「外部 timeout」与「各 flavor 声明的就绪时刻」取最小值，作为 `wait_until` 的睡眠上限，做到**到点即醒、无需轮询**。

## 7. 下一步学习建议

本讲把 receiver 端的六种 flavor 补齐了。接下来：

- **u3-l9 select 动态选择算法**：本讲多次引用了 `run_select`、`try_select → register → wait_until → unregister → accept` 的五阶段流程，但没有展开。下一讲会完整剖析 select 如何保证公平性与正确性，建议重点对照本讲 4.4 节阅读。
- **u3-l10 select! 宏**：本讲的实践里频繁用了 `select!` 宏，它是 `Select` 的声明式语法糖。学完宏的实现后，回看本讲的 `select! { recv(tick) -> ... }` 会更清楚它如何展开成对 `deadline()` 的调用。
- 若想再看一个时间 flavor 的真实组合范例，可直接研读仓库的 [examples/stopwatch.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/examples/stopwatch.rs) 与 [examples/matching.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/examples/matching.rs)，它们是 select 与 at/tick 配合的最佳样本。
