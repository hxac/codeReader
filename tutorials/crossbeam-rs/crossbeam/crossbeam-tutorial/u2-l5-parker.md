# Parker 线程停放原语

## 1. 本讲目标

本讲精读 `crossbeam-utils/src/sync/parker.rs`，讲解 crossbeam 提供的「线程停放（park）」原语 `Parker` / `Unparker`。学完后你应当能够：

- 说清 `Parker` 的「令牌（token）」模型，理解为何「先 `unpark` 后 `park`」不会永久阻塞。
- 画出 `EMPTY` / `PARKED` / `NOTIFIED` 三态状态机的迁移规则。
- 解释 `park()` 如何用 `Mutex` + `Condvar` 阻塞、如何处理「伪唤醒（spurious wakeup）」与超时。
- 解释 `unpark()` 为何用 `swap` 而非 `compare_exchange`，以及它如何靠加锁来避免「丢失唤醒（lost wakeup）」。
- 动手用 `Parker` 写一个跨线程事件等待器。

本讲只依赖 u1-l4（作用域线程）的生命周期与线程概念，不涉及 epoch、channel 等更复杂机制。

## 2. 前置知识

### 2.1 什么是「停放一个线程」

一个线程在等待某个条件时，有两种极端做法：

- **忙等待（busy-wait / 自旋）**：循环反复检查条件，CPU 一直满载。u2-l1 的 `Backoff` 就是给自旋加退避，但本质上仍在线。
- **阻塞（block / park）**：把线程标记为「睡眠」，交出 CPU，由操作系统调度别的线程；等到条件满足时再被别人「唤醒」。

`Parker` 属于第二种：它让当前线程真正睡下去，直到另一个线程通过 `Unparker` 把它叫醒。在 u2-l1 讲过的「自旋 → 让时间片 → 阻塞」三段式里，`Parker` 正是最后那段「阻塞」的落地工具——`Backoff::is_completed()` 返回 true 时，就该改用 `Parker` 这类原语了。

### 2.2 Mutex 与 Condvar 的经典配合

`Parker` 内部用标准库的 `Mutex` 和 `Condvar`（条件变量）实现阻塞。你需要知道两个要点：

- `Condvar::wait(m)` 是一个**原子动作**：它会释放持有的锁 `m`，然后让线程睡眠；被唤醒时再重新获取锁 `m` 后返回。正是因为「释放锁」和「睡眠」是原子的，才不会出现「刚释放锁、还没睡下就被通知」的窗口。
- `Condvar` 允许**伪唤醒**：`wait` 可能在没有任何人 `notify` 的情况下自己返回。因此条件变量的标准用法是 `while` 循环：醒来后必须重新检查条件，不能假设「醒来 = 条件成立」。`park()` 的实现里你会看到这个循环。

### 2.3 丢失唤醒问题

如果「通知」发生在「等待方还没真正睡下」的那一小段时间里，通知就被白白丢掉了，等待方会永远睡死——这就是「丢失唤醒」。`Parker` 用一个原子状态字 + 一把锁，把这段危险窗口消除掉，这是本讲最值得品味的工程细节。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [crossbeam-utils/src/sync/parker.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/parker.rs) | 本讲主角。定义 `Parker` / `Unparker` / `UnparkReason`，以及内部的 `Inner` 状态机和 `park` / `unpark` 全部逻辑。 |
| [crossbeam-utils/src/sync/mod.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/mod.rs) | 把 `Parker` / `Unparker` / `UnparkReason` 公开导出；注意 `parker` 模块在 `crossbeam_loom` 下也参与编译，会被 loom 模型检查（见 u7-l3）。 |
| [crossbeam-utils/src/lib.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/lib.rs) | 定义 `primitive` 抽象层：在 loom 下把 `Arc`/`Mutex`/`Condvar`/原子类型替换成 loom 版本，使 `Parker` 能被模型检查。 |
| [crossbeam-utils/tests/parker.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/tests/parker.rs) | 三个集成测试，覆盖「先 unpark 后 park」「无人 unpark 超时」「另一线程 unpark」三种场景，是理解行为的最佳入口。 |

对外路径：用户既可写 `crossbeam_utils::sync::Parker`，也可通过主 crate 门面写 `crossbeam::sync::Parker`（u1-l3 讲过 `crossbeam` 把 `crossbeam_utils::sync` 重导出为 `crossbeam::sync`）。

## 4. 核心概念与源码讲解

### 4.1 令牌模型与对外 API

#### 4.1.1 概念说明

`Parker` 的全部行为可以用一个抽象概念概括：**令牌（token）**。每个 `Parker` 关联一个令牌，初始时令牌**不存在**：

- `park()`：阻塞当前线程，直到令牌**存在**；返回前**消费**掉令牌（令牌重新变为不存在）。
- `park_timeout(dur)` / `park_deadline(t)`：与 `park` 相同，但最多等指定时间；超时也会返回，并通过 `UnparkReason` 告诉你是被 `unpark` 唤醒还是超时。
- `unpark()`：如果令牌尚不存在，则**原子地**令其存在。因为令牌初始不存在，所以「先 `unpark` 后 `park`」时，`park` 会立刻发现令牌并消费、立即返回——**不会永久阻塞**。

这和标准库 `std::thread::park` / `unpark` 的语义一致，但 crossbeam 把它做成一个**独立、可克隆句柄、可自定义 Collector 之外随处可用**的对象，而不是绑死在线程句柄上。文档原文把它比作「一把用 `park`/`unpark` 上锁解锁的自旋锁」。

#### 4.1.2 核心流程

一个典型的使用结构是「一等待、一通知」：

```text
等待方线程                              通知方线程
-----------                             -----------
let p = Parker::new();                  拿到 u: Unparker（p.unparker().clone()）
let u = p.unparker().clone();
把 u move 给通知方 ────────────────────►
                                        (做某些事，条件成立)
p.park();   // 阻塞                     u.unpark();  // 投递令牌
// 被唤醒，继续                         ...
```

`Parker` 与 `Unparker` 共享同一份内部状态（`Arc<Inner>`）：`Parker` 归「会 park 自己的那个线程」所有，`Unparker` 可以 `.clone()` 出多份发给任意线程去 `unpark`。

#### 4.1.3 源码精读

先看两个公开结构体的定义。[parker.rs:56-59](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/parker.rs#L56-L59) 定义 `Parker`：它内部就持有一个 `Unparker`，外加一个 `PhantomData<*const ()>`。

```rust
pub struct Parker {
    unparker: Unparker,
    _marker: PhantomData<*const ()>,
}
```

这个 `_marker` 很关键：`*const ()` 既不是 `Send` 也不是 `Sync`，于是默认情况下 `Parker` 既不能跨线程发送、也不能跨线程共享。随后 [parker.rs:61](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/parker.rs#L61) 只手动实现了 `Send`：

```rust
unsafe impl Send for Parker {}
```

也就是说 **`Parker`: `Send` 但 `!Sync`**——你可以把它 move 到另一个线程，但**同一个 `Parker` 不能被多个线程同时调用 `park`**。这是有意的：`park` 的语义本就是「停放当前线程」，一个 `Parker` 只服务于一个线程。

对照看 [parker.rs:223-228](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/parker.rs#L223-L228) 的 `Unparker`：

```rust
pub struct Unparker {
    inner: Arc<Inner>,
}

unsafe impl Send for Unparker {}
unsafe impl Sync for Unparker {}
```

**`Unparker` 既是 `Send` 又是 `Sync`**——因为它只是一把「通知遥控器」，内部引用计数 `Arc<Inner>` 让它可以被任意线程持有、克隆、并发调用 `unpark`。这正符合「等待方持有 `Parker`、通知方持有 `Unparker`」的分工。`Clone` 实现见 [parker.rs:309-315](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/parker.rs#L309-L315)，只是克隆那份 `Arc`，所以开销很低。

`Parker::default()` 在 [parker.rs:63-76](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/parker.rs#L63-L76) 构造了那份共享的 `Inner`，初始状态是 `EMPTY`：

```rust
inner: Arc::new(Inner {
    state: AtomicUsize::new(EMPTY),
    lock: Mutex::new(()),
    cvar: Condvar::new(),
}),
```

三个字段构成了本讲的全部舞台：一个原子状态字、一把锁、一个条件变量。

对外方法都是薄封装。`park` 直接转调内部，见 [parker.rs:109-111](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/parker.rs#L109-L111)：

```rust
pub fn park(&self) {
    self.unparker.inner.park(None);   // None 表示不限时
}
```

限时版 [parker.rs:126-134](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/parker.rs#L126-L134) 把 `Duration` 换算成绝对时刻 `Instant`（deadline），溢出时退化为无限等待：

```rust
pub fn park_timeout(&self, timeout: Duration) -> UnparkReason {
    match Instant::now().checked_add(timeout) {
        Some(deadline) => self.park_deadline(deadline),
        None => { self.park(); UnparkReason::Unparked }
    }
}
```

返回值类型 `UnparkReason` 定义在 [parker.rs:320-327](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/parker.rs#L320-L327)，是个只有两个变体的枚举：`Unparked`（被通知唤醒）和 `Timeout`（超时返回）。这让调用方能区分「等到了」还是「没等到」。

#### 4.1.4 代码实践

**实践目标**：用 `Parker` 写一个事件等待器，并验证「先 `unpark` 后 `park`」不会永久阻塞。

下面是**示例代码**（可放进一个独立 crate 的 `examples/event_wait.rs`，该 crate 的 `Cargo.toml` 加上 `crossbeam = "0.8"`）：

```rust
// 示例代码
use crossbeam_utils::sync::Parker;
use std::thread;
use std::time::Duration;

fn main() {
    let p = Parker::new();
    let u = p.unparker().clone(); // 通知遥控器，可 move 到别的线程

    // 场景 1：先 unpark 后 park —— 令牌已就位，park 应立即返回
    u.unpark();
    p.park();
    println!("场景1：先 unpark 后 park，立即返回，没有永久阻塞");

    // 场景 2：跨线程事件等待
    let handle = thread::spawn(move || {
        thread::sleep(Duration::from_millis(200));
        println!("通知方：条件成立，调用 unpark");
        u.unpark();
    });

    println!("等待方：开始 park 阻塞……");
    p.park(); // 阻塞，直到对方 unpark
    println!("等待方：被唤醒，继续执行");

    handle.join().unwrap();
}
```

**操作步骤**：

1. 新建一个 binary crate，`Cargo.toml` 加 `crossbeam = "0.8"`。
2. 把上面代码放入 `examples/event_wait.rs`。
3. 运行 `cargo run --example event_wait`。

**需要观察的现象**：

- 场景 1 的打印**立即**出现（没有任何卡顿）。
- 场景 2 中「等待方：开始 park 阻塞」先打印，约 0.2 秒后「通知方：条件成立」与「等待方：被唤醒」相继打印。

**预期结果**：程序正常退出，主线程不会被场景 1 的 `park()` 卡死，证明令牌被提前投递后可被后续 `park` 消费。

> 说明：crossbeam 自己在 `crossbeam-utils/tests/parker.rs` 里用 `park_timeout` 做了完全相同性质的测试（见下文 4.3.4），可对照阅读。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Parker` 是 `!Sync`，而 `Unparker` 是 `Sync`？如果允许两个线程同时对同一个 `Parker` 调用 `park`，会出什么问题？

**参考答案**：`Parker` 表达的是「停放**当前**线程」的语义，其 `park` 内部会用当前线程去阻塞在 `Condvar` 上，天然是「每 Parker 归属一个线程」的模型；`PhantomData<*const ()>` + 仅 `impl Send` 把这一约束写进了类型系统。若两个线程对同一个 `Parker` 同时 `park`，两者会争抢同一把内部锁、阻塞在同一个 `Condvar` 上，语义混乱且必然互相干扰。`Unparker` 只是「投递令牌」的无状态遥控器，`Arc<Inner>` + `impl Send + Sync` 允许任意线程并发 `unpark`，完全安全。

**练习 2**：文档说「`unpark` 后再 `park` 会立即返回」。但如果在「`unpark` 之后、`park` 之前」又调用了一次 `unpark`，`park` 会被唤醒两次吗？

**参考答案**：不会。令牌是「二值」的（存在/不存在），多次 `unpark` 只是把令牌反复置为「存在」，仍是同一个令牌；随后的 `park` 消费一次令牌后立即返回。这就是为什么 `unpark` 是「幂等」的——它不会累积唤醒次数。

---

### 4.2 Inner 三态状态机与 Mutex+Condvar 组合

#### 4.2.1 概念说明

令牌模型只是对外抽象，内部需要一个状态来同时编码两件事：**令牌在不在**，以及**有没有线程正堵在 `park` 里等待**。`parker.rs` 用一个 `AtomicUsize` 和三个常量值完成这件事，见 [parker.rs:329-331](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/parker.rs#L329-L331)：

```rust
const EMPTY: usize = 0;    // 没令牌，也没人等着
const PARKED: usize = 1;   // 没令牌，但有线程正堵在 park 里
const NOTIFIED: usize = 2; // 有令牌（已被人 unpark，等待被消费）
```

可以这样理解三态：

- `EMPTY`：平静状态——没人等，也没令牌。
- `PARKED`：有线程正在 `park` 里沉睡，等待令牌到来。
- `NOTIFIED`：令牌已就位（`unpark` 投递的），尚未被 `park` 消费。它可能是在「没人等」时投递的（提前通知，留在那里等下一次 `park`），也可能是「有人等」时投递的（用来唤醒沉睡者）。

`Inner` 结构见 [parker.rs:333-337](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/parker.rs#L333-L337)：

```rust
struct Inner {
    state: AtomicUsize,
    lock: Mutex<()>,
    cvar: Condvar,
}
```

`state` 承载状态机，`lock` 与 `cvar` 配合完成「安全睡眠 + 唤醒」。注意这三个类型并非直接来自标准库，而是来自 `crate::primitive::sync`（见 [parker.rs:4-7](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/parker.rs#L4-L7)）。这是一个**抽象层**：在普通编译下它是 `alloc::sync::Arc` / `std::sync::{Mutex, Condvar}`；而在 `crossbeam_loom` cfg 下，[lib.rs:47-83](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/lib.rs#L47-L83) 会把它们替换成 `loom::sync::*`，使整个 `Parker` 能被 loom 做穷举式并发模型检查（详见 u7-l3）。所以你读到的「标准库类型」其实是一层可换皮的别名。

#### 4.2.2 核心流程

三态状态机的迁移规则可整理成下表（把 `park` / `unpark` 看作事件）：

| 当前状态 | 触发事件 | 动作 | 新状态 |
|---|---|---|---|
| `EMPTY` | `park` 决定要睡 | `CAS(EMPTY→PARKED)`，加锁后 `cvar.wait` | `PARKED` |
| `PARKED` | `unpark` | `swap(→NOTIFIED)`，加锁+`notify_one` 唤醒沉睡者 | `NOTIFIED` |
| `NOTIFIED` | `park` | `CAS(NOTIFIED→EMPTY)`，立即返回（消费令牌） | `EMPTY` |
| `NOTIFIED` | `unpark` | `swap(→NOTIFIED)`（幂等，仍是有令牌） | `NOTIFIED` |
| `EMPTY` | `unpark` | `swap(→NOTIFIED)`，没人等，直接返回（令牌留下） | `NOTIFIED` |

要点：`NOTIFIED` 是「令牌在」的统一表示，无论令牌是「提前投递、没人等」还是「有人等、刚被唤醒」；任何 `park` 进入时第一件事就是尝试消费 `NOTIFIED`。

#### 4.2.3 源码精读

全部状态机逻辑写在 `Inner` 的两个方法里。`Inner::park` 见 [parker.rs:340-410](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/parker.rs#L340-L410)，`Inner::unpark` 见 [parker.rs:412-434](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/parker.rs#L412-L434)。这两个方法的具体分支留到 4.3、4.4 拆开讲，这里只关注它们共同的一个工程选择：**所有原子操作都用 `SeqCst`（顺序一致）**，例如 [parker.rs:343-344](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/parker.rs#L343-L344) 与 [parker.rs:417](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/parker.rs#L417)。

`SeqCst` 是最强的内存序，开销也最大。`Parker` 在这里优先选择了**正确性简单可论证**而非榨取最后一点性能——对一个会被频繁用于「线程沉睡」的原语，阻塞本身的开销远大于内存序的开销，这个取舍是合理的。源码注释里也反复强调「为了能和对方的写同步」，把同步关系建立得非常明确（见 4.4.3 对 [parker.rs:413-416](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/parker.rs#L413-L416) 的分析）。

#### 4.2.4 代码实践

**实践目标**：通过阅读测试，用断言反推状态机行为。

阅读 [crossbeam-utils/tests/parker.rs:9-18](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/parker.rs) 的第一个测试 `park_timeout_unpark_before`：

```rust
let p = Parker::new();
for _ in 0..10 {
    p.unparker().unpark();                                   // 投递令牌 → NOTIFIED
    assert_eq!(
        p.park_timeout(Duration::from_millis(u32::MAX as u64)),
        UnparkReason::Unparked,                              // 立即消费令牌 → EMPTY
    );
}
```

**操作步骤**：

1. 在仓库根目录执行 `cargo test -p crossbeam-utils --test parker`。
2. 观察三个测试全部通过。

**需要观察的现象与预期结果**：循环 10 次都立刻返回 `Unparked`，且总耗时远小于 `u32::MAX` 毫秒。这验证了状态机在「`unpark` → `park`」反复来回时，`NOTIFIED ↔ EMPTY` 的消费链路是稳定闭合的——每一次 `park` 都恰好消费上一次 `unpark` 的令牌，不多不少。如果状态机有「累积唤醒」或「丢令牌」的 bug，10 次循环里必然出现一次卡到超时返回 `Timeout`。

> 待本地验证：若你的环境 `u32::MAX` 毫秒换算 `Instant` 时溢出，会走 `park_timeout` 的 `None` 分支退化为无限等待，但因为有 `unpark` 先行，仍会立即返回。

#### 4.2.5 小练习与答案

**练习 1**：`PARKED` 和 `NOTIFIED` 都「没有令牌可被消费」吗？它们的区别到底是什么？

**参考答案**：恰恰相反——`NOTIFIED` 是「令牌已就位、等待被消费」；`PARKED` 是「**没**有令牌、但有线程在等」。两者的区别在于「令牌在不在」：`NOTIFIED` 表示有令牌（任意一次 `park` 碰到它都会消费并返回），`PARKED` 表示没令牌（沉睡的线程在等一个未来的 `unpark`）。

**练习 2**：为什么 `Inner` 同时需要 `lock` 和 `cvar` 两个同步原语，只用 `state` 一个原子变量行不行？

**参考答案**：`state` 解决的是「令牌在不在 / 有没有人在等」的**逻辑判断**，但它本身无法让线程真正睡下或被唤醒——线程睡眠必须靠操作系统的阻塞原语（`Condvar::wait`），唤醒必须靠 `Condvar::notify_one`。`lock` 则是配合 `cvar` 的「保护等待/通知临界区」的必要机制（见 4.4）。只用一个原子变量，要么得退化成忙等自旋（浪费 CPU），要么缺少可靠唤醒通道。

---

### 4.3 park() 的阻塞逻辑与伪唤醒处理

#### 4.3.1 概念说明

`park` 要同时处理三类情况，且都不能卡死：

1. **令牌已就位**（`NOTIFIED`）：立即消费，不阻塞。
2. **已经过了 deadline**：立即返回 `Timeout`，不阻塞。
3. **需要真正睡眠**：进入 `PARKED` → `cvar.wait` → 醒来后还得提防**伪唤醒**与**超时竞态**。

第 3 点是最微妙的：`Condvar` 允许伪唤醒，所以「醒来」不等于「有令牌」，必须循环重检；同时超时与 `unpark` 可能「同时」到达，需要明确该报哪一个。

#### 4.3.2 核心流程

`Inner::park` 的伪代码（对应 [parker.rs:340-410](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/parker.rs#L340-L410)）：

```text
fn park(deadline):
    # ① 快速消费已就位令牌（无需加锁）
    if CAS(state: NOTIFIED → EMPTY) 成功:
        return Unparked

    # ② 已过截止时间，直接返回超时（不睡）
    if deadline 已过:
        return Timeout

    # ③ 准备真正睡眠：先加锁
    m = lock.lock()

    # ④ 把状态从 EMPTY 推进到 PARKED；期间若被 unpark，则消费令牌返回
    match CAS(state: EMPTY → PARKED):
        Ok            => 继续去睡
        Err(NOTIFIED) => swap(state → EMPTY); return Unparked   # 修正「①之后、④之前」的竞态
        Err(其它)     => panic（状态不一致）

    # ⑤ 睡眠循环（处理伪唤醒与超时）
    loop:
        m = cvar.wait(m) 或 cvar.wait_timeout(m, 剩余时间)
        if deadline 已过（wait_timeout 用尽）:
            return 据此时 swap(state→EMPTY) 的结果: NOTIFIED→Unparked, PARKED→Timeout
        if CAS(state: NOTIFIED → EMPTY) 成功:
            return Unparked
        # 否则是伪唤醒，回到 loop 继续睡
```

两次「消费令牌」检查（① 和 ④ 的 `Err(NOTIFIED)`）是为了覆盖一段竞态窗口：线程在 ① 看到状态不是 `NOTIFIED`，但还没来得及加锁并把状态推进到 `PARKED`，此时若 `unpark` 把状态改成了 `NOTIFIED`，必须在 ④ 的 `Err` 分支里捕获，否则会错误地睡下去。

#### 4.3.3 源码精读

**① 快速消费令牌**：[parker.rs:342-348](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/parker.rs#L342-L348)——不加锁先试一发 `CAS(NOTIFIED → EMPTY)`，成功就直接返回。这是「先 unpark 后 park」能立即返回的根因。

**② 零超时短路**：[parker.rs:351-355](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/parker.rs#L351-L355)——发现 deadline 已过，直接返回 `Timeout`，避免无意义的加锁。

**③④ 加锁并推进到 PARKED**：[parker.rs:358-374](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/parker.rs#L358-L374)：

```rust
let mut m = self.lock.lock().unwrap();

match self.state.compare_exchange(EMPTY, PARKED, SeqCst, SeqCst) {
    Ok(_) => {}                                   // 成功标记「我在这等」
    Err(NOTIFIED) => {
        // 关键：① 之后、加锁之前可能又来了 unpark，必须重新读 state 消费它
        let old = self.state.swap(EMPTY, SeqCst);
        assert_eq!(old, NOTIFIED, "park state changed unexpectedly");
        return UnparkReason::Unparked;
    }
    Err(n) => panic!("inconsistent park_timeout state: {}", n),
}
```

这里 [parker.rs:364-371](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/parker.rs#L364-L371) 的注释解释了为何 `Err(NOTIFIED)` 分支要**再读一次** `state`（用 `swap`）：因为「读到 `NOTIFIED`」到「执行 `swap`」之间，`unpark` 可能又来了一次（令牌本就是幂等的，但我们要确保 acquire 语义能同步到对方在 `unpark` 之前的所有写）。用 `swap(EMPTY)` 既消费了令牌，又是一次能与之同步的读。

**⑤ 睡眠循环**：[parker.rs:376-409](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/parker.rs#L376-L409)。核心是无限 `loop`，每次 `cvar.wait` 或 `wait_timeout` 返回后：

- 若 deadline 已到（[parker.rs:388-394](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/parker.rs#L388-L394)）：用 `swap(state → EMPTY)` 收尾——如果此刻状态是 `NOTIFIED`（`unpark` 与超时几乎同时到达），优先报 `Unparked`，否则报 `Timeout`。源码注释在 [parker.rs:384-386](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/parker.rs#L384-L386) 明确说：宁可报「等到了」也不报超时。
- 否则尝试 `CAS(NOTIFIED → EMPTY)`（[parker.rs:398-405](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/parker.rs#L398-L405)）：成功说明是真唤醒，返回 `Unparked`；失败说明是**伪唤醒**，落到循环末尾注释（[parker.rs:407-408](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/parker.rs#L407-L408)）回炉重睡。

这正是「条件变量必须在 `while` 里重检条件」的标准范式。

#### 4.3.4 代码实践

**实践目标**：复现「跨线程 unpark 唤醒阻塞中的 park」并用 `UnparkReason` 验证返回原因。

**示例代码**：

```rust
// 示例代码
use crossbeam_utils::sync::{Parker, UnparkReason};
use std::thread;
use std::time::Duration;

fn main() {
    // 场景 A：无人 unpark，超时返回 Timeout
    let p = Parker::new();
    let reason = p.park_timeout(Duration::from_millis(50));
    assert_eq!(reason, UnparkReason::Timeout);
    println!("场景A：超时返回 {:?}", reason);

    // 场景 B：另一线程在 50ms 后 unpark，应返回 Unparked
    let p = Parker::new();
    let u = p.unparker().clone();
    let h = thread::spawn(move || {
        thread::sleep(Duration::from_millis(50));
        u.unpark();
    });
    let reason = p.park_timeout(Duration::from_secs(5));
    assert_eq!(reason, UnparkReason::Unparked);
    println!("场景B：被 unpark 唤醒 {:?}", reason);
    h.join().unwrap();
}
```

**操作步骤**：放入 `examples/park_reason.rs`，运行 `cargo run --example park_reason`。

**需要观察的现象**：场景 A 在约 50ms 后打印 `Timeout`；场景 B 同样约 50ms 后打印 `Unparked`（远未到 5 秒上限）。

**预期结果**：两次断言均成立，程序正常退出。说明 `park` 既能在超时与唤醒竞态时正确区分二者，也能在伪唤醒时自我纠正、不会提前误返回。

#### 4.3.5 小练习与答案

**练习 1**：`park` 在循环里醒来后，为什么不直接 `return Unparked`，而非要先 `CAS(NOTIFIED → EMPTY)` 成功才返回？

**参考答案**：因为唤醒可能是**伪唤醒**——操作系统/条件变量允许在没有 `notify` 的情况下自行返回。此时 `state` 仍是 `PARKED`（没有真令牌），若直接返回，调用方会误以为「条件满足」。`CAS(NOTIFIED → EMPTY)` 失败恰好用来甄别「没有真令牌」，从而继续睡；只有 CAS 成功（确实有令牌）才返回。这是条件变量使用的铁律。

**练习 2**：若 `unpark` 与超时**同时**发生，`park_timeout` 会返回 `Unparked` 还是 `Timeout`？源码在哪里做了这个选择？

**参考答案**：返回 `Unparked`。选择落在 [parker.rs:388-394](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/parker.rs#L388-L394)：超时收尾时用 `swap(state → EMPTY)`，若结果是 `NOTIFIED` 则优先报 `Unparked`。源码注释（[parker.rs:384-386](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/parker.rs#L384-L386)）明确说明在「同时到达」时优先报告 `unpark`。

---

### 4.4 unpark() 的先发通知与防止丢失唤醒

#### 4.4.1 概念说明

`unpark` 看似简单——「投递令牌并叫醒对方」——但它必须回答两个棘手问题：

1. **如果状态已经是 `NOTIFIED`，还要不要再写一次？** 答案是**要**。因为 `park` 一侧需要一次「release 写」来建立同步关系（让被唤醒的线程能看到 `unpark` 之前的所有写）。所以 `unpark` 必须用 `swap`（无条件写），而不是「读到 `NOTIFIED` 就返回」的 `compare_exchange`。
2. **怎么避免丢失唤醒？** 「通知方发了 `notify_one`」与「等待方真正进入睡眠」之间存在时间差。如果通知发在等待方「还没 `wait`」的窗口里，通知就丢了，等待方永远睡死。`Parker` 的解法是：**让 `unpark` 去抢同一把 `lock`**，确保 `notify_one` 只在等待方已经（或必然）进入 `wait` 之后才发出。

#### 4.4.2 核心流程

`Inner::unpark` 的伪代码（对应 [parker.rs:412-434](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/parker.rs#L412-L434)）：

```text
fn unpark():
    # ① 无条件写成 NOTIFIED（用 swap，确保 release 同步）
    match swap(state → NOTIFIED):
        EMPTY    => return            # 没人等，令牌留下即可
        NOTIFIED => return            # 已有令牌，幂等
        PARKED   => 继续去唤醒         # 有人正堵着
        _        => panic

    # ② 抢锁再立刻释放，目的是「等到等待方进入 wait」
    drop(lock.lock())

    # ③ 这时等待方必然已睡在 cvar 上，发通知不会丢
    cvar.notify_one()
```

为什么第 ② 步「抢锁」能防丢失唤醒？回顾 `park` 一侧：它在 `cvar.wait(m)` **之前**一直持有 `lock`；`wait` 是「原子地释放 `lock` 并睡眠」。所以：

- 只要 `unpark` 能拿到 `lock`，就说明 `park` 一侧已经执行到 `wait`、把锁放了、人也睡下了——此时 `notify_one` 必然命中。
- 若 `park` 还没走到 `wait`（仍持锁），`unpark` 的 `lock.lock()` 会**阻塞等待**，直到 `park` 进入 `wait` 释放锁。这正是源码注释（[parker.rs:424-432](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/parker.rs#L424-L432)）所说：「等待方在把 `state` 置为 `PARKED`（或伪唤醒后上次检查 `state`）与真正 `wait` 之间有一段窗口；幸好这段窗口里它持着 `lock`，所以我们抢锁就能等到它准备好接收通知」。

这就是经典的「条件变量 + 互斥锁」防丢失唤醒范式：**通知方持锁发通知，等待方持锁进等待**，二者靠同一把锁把危险窗口排除掉。

#### 4.4.3 源码精读

**① 用 `swap` 而非 CAS**：[parker.rs:417-422](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/parker.rs#L417-L422)：

```rust
match self.state.swap(NOTIFIED, SeqCst) {
    EMPTY => return,    // no one was waiting
    NOTIFIED => return, // already unparked
    PARKED => {}        // gotta go wake someone up
    _ => panic!("inconsistent state in unpark"),
}
```

紧邻的注释 [parker.rs:413-416](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/parker.rs#L413-L416) 解释了为何必须是 `swap`：即便状态已是 `NOTIFIED`，也要再写一次 `NOTIFIED`，以产生一次能与 `park` 的 acquire 读「同步」的 release 写；如果改成「读到 `NOTIFIED` 就返回」的 CAS，第二次 `unpark` 就不会产生任何写，`park` 一侧的同步关系就建立不起来。

**②③ 抢锁 + 通知**：[parker.rs:432-433](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/parker.rs#L432-L433)：

```rust
drop(self.lock.lock().unwrap());
self.cvar.notify_one();
```

注意顺序是「先 `drop` 锁、再 `notify_one`」（注释 [parker.rs:430-431](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/parker.rs#L430-L431)）：如果先 `notify` 再 `drop`，被唤醒的 `park` 会立刻醒来、却撞在还没释放的 `lock` 上又得再睡一次（虽然正确，但多一次无谓的调度抖动）。先 `drop` 再 `notify`，让被唤醒者一觉醒来就能拿到锁返回，更顺滑。

#### 4.4.4 代码实践

**实践目标**：亲手复现「丢失唤醒」会怎样，再体会 `Parker` 是如何消除它的。我们用一个**故意脆弱的对照实现**来说明问题。

**示例代码**（对照实验，仅为理解概念，不是 crossbeam 的实现）：

```rust
// 示例代码：仅供理解「丢失唤醒」，不要用于生产
use std::sync::{Condvar, Mutex};
use std::thread;
use std::time::Duration;

struct NaiveParker {
    flag: Mutex<bool>,
    cvar: Condvar,
}

impl NaiveParker {
    // 注意：park 在 wait 之前并没有原子地「标记自己要睡 + 释放锁」，
    // 并且没有 unpark 抢锁的配合，存在丢失唤醒窗口。
    fn park_naive(&self) {
        let mut f = self.flag.lock().unwrap();
        while !*f {
            f = self.cvar.wait(f).unwrap();   // 危险窗口：notify 若在重检前到达会丢
        }
        *f = false;
    }
    fn notify(&self) {
        // 这里没有抢锁就 notify，可能丢通知
        self.cvar.notify_one();
    }
}

fn main() {
    let p = std::sync::Arc::new(NaiveParker { flag: Mutex::new(false), cvar: Condvar::new() });
    let q = p.clone();
    let h = thread::spawn(move || {
        // 故意：在线程还没进入 wait 前 notify（容易丢）
        q.notify();
    });
    thread::sleep(Duration::from_millis(10));
    p.park_naive();   // 多次运行下，有可能永久卡住
    h.join().unwrap();
}
```

**操作步骤**：

1. 把上面代码放入一个临时 binary 多次运行，观察是否偶发卡死（取决于调度，不一定每次复现）。
2. 然后回到 `crossbeam`，对照 [parker.rs:432-433](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/parker.rs#L432-L433) 与 `park` 一侧的加锁（[parker.rs:358](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/parker.rs#L358)），理解「通知方抢锁、等待方持锁进 `wait`」是如何把这个窗口消除的。

**需要观察的现象与预期结果**：脆弱版**可能**偶发卡死；而用 `crossbeam_utils::sync::Parker` 重写同样的「先 `unpark` 后 `park`」场景（见 4.1.4）**永远不会**卡死——因为 `unpark` 的 `lock.lock()` 会等到 `park` 进入 `wait` 才发 `notify_one`。这正是 crossbeam 测试 [tests/parker.rs:32-50](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/tests/parker.rs#L32-L50)（`park_timeout_unpark_called_other_thread`）反复断言 `Unparked` 的底气。

> 待本地验证：脆弱版卡死行为依赖线程调度，在单核或高负载下更易复现；若多次运行未卡死，可增大 `notify` 与 `park` 之间的间隔或运行次数。

#### 4.4.5 小练习与答案

**练习 1**：把 `unpark` 里的 `self.state.swap(NOTIFIED, SeqCst)` 改成 `compare_exchange(NOTIFIED, NOTIFIED, …)`（即「已是 NOTIFIED 就什么都不写、直接返回」），功能上会出什么问题？

**参考答案**：会破坏内存同步。`park` 一侧依赖 `unpark` 产生的 release 写来「同步看见 `unpark` 之前的其它写」。若 `unpark` 在读到 `NOTIFIED` 时不写就返回，连续两次 `unpark` 中的第二次就不会产生任何写，`park` 通过 acquire 读 `state` 时就缺少与之配对的 release，可能观测不到对方更早的写。用 `swap` 保证**每次** `unpark` 都写一次 `NOTIFIED`，同步关系才始终成立。注释 [parker.rs:413-416](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/parker.rs#L413-L416) 专门说明了这点。

**练习 2**：`unpark` 在 `PARKED` 分支里先 `drop(lock.lock())` 再 `notify_one`，能否改成先 `notify_one` 再 `drop`？为什么源码不这么做？

**参考答案**：功能上仍正确（不会丢唤醒，因为 `notify` 也在持锁期间发生），但会多一次「无效抖动」：被唤醒的 `park` 立即被调度运行，却发现 `lock` 还被 `unpark` 持有，只能又阻塞等锁，等 `unpark` 释放后才能继续。先 `drop` 再 `notify` 则让被唤醒者一醒来就能拿锁返回，调度更顺滑。注释 [parker.rs:430-431](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/parker.rs#L430-L431) 给出了同样解释。

---

## 5. 综合实践

把本讲知识串起来，做一个「**带超时的单次事件门闸（one-shot latch）**」：主线程创建一个 `Parker`，把 `Unparker` 交给后台线程；主线程 `park_deadline` 最多等 1 秒，根据返回的 `UnparkReason` 判断是「按时完成」还是「超时」。

**示例代码**：

```rust
// 示例代码
use crossbeam_utils::sync::{Parker, UnparkReason};
use std::thread;
use std::time::{Duration, Instant};

fn main() {
    let p = Parker::new();
    let u = p.unparker().clone();
    let deadline = Instant::now() + Duration::from_secs(1);

    let h = thread::spawn(move || {
        // 模拟一个可能很慢的任务
        thread::sleep(Duration::from_millis(300));
        u.unpark(); // 完成后开门
    });

    match p.park_deadline(deadline) {
        UnparkReason::Unparked => println!("任务在 1s 内完成"),
        UnparkReason::Timeout => println!("任务超时"),
    }
    h.join().unwrap();

    // 练习：把上面 sleep 改成 1500ms，重新运行，应看到「任务超时」。
}
```

**操作步骤**：

1. 把代码放入 `examples/latch.rs`，运行 `cargo run --example latch`，观察打印。
2. 把后台线程的 `sleep` 改成 `Duration::from_millis(1500)`，再次运行。
3. 对照源码说明：当任务在 300ms 完成、`u.unpark()` 把 `state` 从 `EMPTY` 置为 `NOTIFIED` 时，主线程的 `park_deadline` 走 [parker.rs:342-348](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/parker.rs#L342-L348) 的快速消费分支，立即返回 `Unparked`。当任务 1500ms 慢于 1s deadline 时，主线程在 [parker.rs:388-394](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/parker.rs#L388-L394) 的超时收尾里 `swap` 出 `PARKED`，返回 `Timeout`。

**需要观察的现象与预期结果**：300ms 版本打印「任务在 1s 内完成」；1500ms 版本在约 1 秒后打印「任务超时」。两者都不会卡死，也不会误报。

**思考延伸**：这个「带超时的门闸」就是许多异步运行时（如 Tokio 的调度器）停放 worker 线程的基本套路——`Parker` 正是为此类场景设计的公共原语。注意 `crossbeam-channel` 内部**并未**直接使用 `Parker`（它有自己基于 `Context`/`Waker` 的阻塞体系，见后续 u3-l7），`Parker` 是面向用户的、独立可复用的停放工具。

## 6. 本讲小结

- `Parker` 用「令牌」模型统一了行为：`park` 阻塞到令牌出现并消费它，`unpark` 投递令牌；令牌是二值幂等的，「先 `unpark` 后 `park`」会立即返回。
- 对外是 `Parker`（`Send` + `!Sync`，归单个等待线程所有）与 `Unparker`（`Send + Sync`，可克隆给任意通知线程）两类句柄，共享同一份 `Arc<Inner>`。
- 内部状态机只有三态 `EMPTY` / `PARKED` / `NOTIFIED`，编码了「令牌在不在」与「有没有人在等」两件事。
- `park` 的核心是「先快速消费 `NOTIFIED` → 加锁推进到 `PARKED` → `cvar` 循环睡眠，并妥善处理伪唤醒、超时与超时/唤醒竞态」。
- `unpark` 用 `swap`（而非 CAS）无条件写 `NOTIFIED` 以建立 release 同步；并在 `PARKED` 时「抢锁 + `notify_one`」来彻底消除丢失唤醒窗口。
- 所有原子操作统一用 `SeqCst`，并用 `crate::primitive` 抽象层让同一份代码可被 loom 模型检查，体现「先正确、再可验证」的工程取舍。

## 7. 下一步学习建议

- **横向对比**：阅读标准库 `std::thread::park` / `Thread::unpark` 的文档，对照理解 crossbeam 版本把「停放」从线程句柄上解耦成独立对象的好处。
- **继续 u2 同步原语**：下一讲 u2-l6 会讲 `WaitGroup`（同步一组任务完成）与 `ShardedLock`（分片读写锁），它们与 `Parker` 同属 `crossbeam_utils::sync`，可以连起来读 `sync/mod.rs` 这一整块。
- **回看 Backoff**：重温 u2-l1，把「自旋 → 让时间片 → 阻塞」三段式与 `Parker` 对应起来——你会理解 `Backoff::is_completed()` 为何是「该切换到 `Parker` 这类阻塞原语」的信号。
- **为 channel 做铺垫**：记住 `crossbeam-channel` 并不直接用 `Parker`，但有相似的「阻塞 + 唤醒」需求；等到 u3-l7 讲 `Context` 与 `Waker` 时，可以把两套机制对照，体会「专用阻塞体系」与「通用 `Parker`」的设计差异。
