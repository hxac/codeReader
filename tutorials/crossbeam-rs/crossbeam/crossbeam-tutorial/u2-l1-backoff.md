# Backoff 自旋退避

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚为什么在 CAS（compare-and-swap）失败循环里「傻转」会拖慢整台机器，以及「退避（backoff）」如何缓解它。
- 读懂 `crossbeam_utils::Backoff` 的全部源码，理解 `step` 字段如何驱动一条「自旋→让出时间片→建议阻塞」的指数升级曲线。
- 区分 `spin()` 与 `snooze()` 两种退避分别对应「别人已经前进了，我快速重试」和「我在等别人前进」两种场景，并知道何时该用 `is_completed()` 切换到真正的阻塞（`park` / 条件变量）。
- 自己动手实现一个用 `Backoff` 退避的自旋锁，并用微基准对比 `spin` 与 `snooze` 变体。

## 2. 前置知识

本讲是进入 crossbeam-utils 并发原语的第一站，你需要先有下面这些直觉（不要求精通）：

- **原子操作与 CAS**。`AtomicUsize`、`AtomicBool` 这类类型支持「原子地读—改—写」。其中 `compare_exchange(expected, new, ...)` 是最常用的：只有当当前值等于 `expected` 时才把它改成 `new` 并返回成功，否则返回失败（并把当前值还给你）。它是构建无锁数据结构的基石。
- **CAS 循环**。因为 CAS 可能被别的线程抢先而失败，常见写法是套一个 `loop`：读旧值 → 算新值 → CAS → 失败就重试。这种循环也常被称为「无锁循环（lock-free loop）」或「自旋循环（spin loop）」。
- **指令与调度**。CPU 有一条 `PAUSE`（x86）/`YIELD`（ARM）指令，能让流水线「歇一下」，降低争用与功耗；而操作系统调度器则能在 `yield_now()` 时把当前线程的时间片让给别人。
- **门面与导入路径**。前置讲义 u1-l3 已经讲过：主 crate `crossbeam` 把 `crossbeam-utils` 里的 `Backoff` 装进 `crossbeam::utils` 模块重导出；本讲里出现的 `crossbeam_utils::Backoff` 与 `crossbeam::utils::Backoff` 指向同一个类型。

## 3. 本讲源码地图

本讲聚焦一个文件，辅以两处真实使用点：

| 文件 | 作用 |
| --- | --- |
| [crossbeam-utils/src/backoff.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/backoff.rs) | `Backoff` 的全部实现，不到 300 行，是本讲主角。 |
| [crossbeam-utils/src/lib.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/lib.rs) | 在这里 `pub use crate::backoff::Backoff;` 把它公开为 `crossbeam_utils::Backoff`。 |
| [crossbeam-utils/src/lib.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/lib.rs)（`primitive` 模块） | 定义 `hint::spin_loop`：正常编译时映射到 `core::hint::spin_loop`，在 loom 模型检查时映射到 `loom::hint::spin_loop`。`Backoff::spin/snooze` 内部自旋就调用它。 |
| [crossbeam-queue/src/array_queue.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs) | 真实使用样例：CAS 失败时 `backoff.spin()`，等待槽位状态更新时 `backoff.snooze()`。 |
| [crossbeam-channel/src/context.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/context.rs) | 真实使用样例：阻塞等待一个 packet 出现时用 `backoff.snooze()`。 |

## 4. 核心概念与源码讲解

### 4.1 为什么需要退避：CAS 循环里的「惊群」

#### 4.1.1 概念说明

想象 4 个线程同时跑同一个 CAS 循环去抢同一个 `AtomicUsize`。每次只有一个线程赢，其余 3 个失败重试。如果失败的线程「立刻、不停」地再发起 CAS，会发生什么？

- CPU 之间通过缓存一致性协议（如 MESI）同步缓存行。争用越凶，缓存行在核之间来回「弹射」越频繁，内存总线被打满，赢家反而更难成功——这叫**争用（contention）**。
- 失败线程还占着 CPU，把赢家本可使用的执行资源挤掉。

解决办法是**退避（backoff）**：失败后先「等一小会」再重试，而且越失败等得越久，从而把争用摊平。`Backoff` 就是 crossbeam 提供的、经过调优的退避器：每一步大约比上一步慢一倍，并能在「等得够久」时提醒你改用更省 CPU 的阻塞机制。

#### 4.1.2 核心流程

`Backoff` 把退避建模成一条三段式升级曲线：

```text
step = 0
   │   指数自旋（PAUSE 指令，1, 2, 4, 8, 16, 32, 64 次）
   ▼
step = 6（SPIN_LIMIT）
   │   让出时间片（yield_now）给 OS 调度器
   ▼
step = 10（YIELD_LIMIT）→ is_completed() = true
   │   建议你改用 park / 条件变量 真正阻塞
   ▼
```

一个 `Backoff` 实例里只藏着一个计数器 `step`，所有方法都在读写它。

#### 4.1.3 源码精读

先看数据结构本身，它极其精简——只有一个 `Cell<u32>`：

```rust
// crossbeam-utils/src/backoff.rs
pub struct Backoff {
    step: Cell<u32>,
}
```

[backoff.rs:80-82](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/backoff.rs#L80-L82) 用 `Cell` 提供「内部可变性」。代价是 `Cell` 不是 `Sync`，所以 **`Backoff` 不能跨线程共享 `&Backoff`**——它天然就是「每个线程一个、放在函数局部」的设计。这一点对正确使用很关键（见 4.3.4）。

构造与重置都只是把 `step` 归零：

```rust
pub fn new() -> Self { Self { step: Cell::new(0) } }   // L94-97
pub fn reset(&self) { self.step.set(0); }              // L109-112
```

源码顶部的文档注释还给了三个范本例子：无锁 `fetch_mul` 用 `spin()`、等待 `AtomicBool` 用 `snooze()`、长时间等待用 `is_completed()` 配合 `thread::park()`，见 [backoff.rs:19-73](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/backoff.rs#L19-L73)。本讲后面会逐一拆开。

#### 4.1.4 代码实践

**实践目标**：先建立「没有退避时争用很痛」的直觉。

1. 写一个 4 线程并发对同一个 `AtomicUsize` 做 `fetch_add` 的小程序，每线程做 100 万次。
2. 用 `std::time::Instant` 测量总耗时。
3. 把每线程的工作量从「纯 `fetch_add`」改成「`fetch_add` 外面包一个空转的 `for _ in 0..0 {}`」做对照基线（只是为了体会测量方法）。

**需要观察的现象**：单线程跑 400 万次 `fetch_add` 与 4 线程各跑 100 万次相比，4 线程版本每条操作的均摊耗时会显著高于单线程——这就是争用的代价。

**预期结果**：4 线程版本明显更慢（具体倍数 **待本地验证**，取决于 CPU 与缓存架构）。

#### 4.1.5 小练习与答案

**练习 1**：为什么不能简单地用 `thread::sleep(Duration::from_nanos(x))` 来代替 `Backoff`？

> 参考答案：`sleep` 至少要陷入内核、交给调度器再唤醒，开销在微秒级以上，对「失败重试」这种纳秒级事件来说太重；而 `Backoff::spin` 只是在用户态执行 `PAUSE` 指令，根本不进内核。

**练习 2**：`Backoff` 字段用 `Cell<u32>` 而不是 `AtomicU32`，这意味着什么？

> 参考答案：`Cell` 不是 `Sync`，因此 `&Backoff` 不能在线程间共享。它强制了「每个线程拥有自己的 `Backoff`」这一用法，避免了多线程同时推进同一个 `step` 反而互相干扰退避节奏。

### 4.2 SPIN_LIMIT / YIELD_LIMIT 与指数增长

#### 4.2.1 概念说明

`Backoff` 的整条升级曲线由两个常量决定：

- `SPIN_LIMIT`：纯自旋阶段的上限。`step` 从 0 长到这个值之前，每调用一次就把自旋次数翻倍。
- `YIELD_LIMIT`：让出时间片阶段的上限。超过 `SPIN_LIMIT` 之后继续累加 `step`，直到它超过 `YIELD_LIMIT`，`is_completed()` 才返回 `true`。

它们的取值是经验调优的结果：太小退避不足，太大延迟过高。

#### 4.2.2 核心流程

第 \(n\) 次调用时的自旋次数服从指数增长：

\[ \text{spins}(n) = 2^{\min(n,\,\text{SPIN\_LIMIT})} \quad (n = 0, 1, 2, \dots) \]

也就是 \(1, 2, 4, 8, 16, 32, 64\)，然后封顶在 \(64\)。每一步恰好是上一步的两倍——这正是文档里那句「Each step of the back off procedure takes roughly twice as long as the previous step」的由来。

把 `step`、自旋次数、所属阶段、是否 `is_completed()` 列成一张表：

| `step`（进入时） | `spin()` 自旋次数 | `snooze()` 行为 | 阶段 | `is_completed()` |
| --- | --- | --- | --- | --- |
| 0 | 1 | 自旋 1 | 纯自旋 | 否 |
| 1 | 2 | 自旋 2 | 纯自旋 | 否 |
| 2 | 4 | 自旋 4 | 纯自旋 | 否 |
| 3 | 8 | 自旋 8 | 纯自旋 | 否 |
| 4 | 16 | 自旋 16 | 纯自旋 | 否 |
| 5 | 32 | 自旋 32 | 纯自旋 | 否 |
| 6 | 64 | 自旋 64 | 纯自旋（边界） | 否 |
| 7 | 64 | `yield_now` | 让出时间片 | 否 |
| 8 | 64 | `yield_now` | 让出时间片 | 否 |
| 9 | 64 | `yield_now` | 让出时间片 | 否 |
| 10 | 64 | `yield_now` | 让出时间片（边界） | 否 |
| 11 | 64 | `yield_now` | 封顶 | **是** |

#### 4.2.3 源码精读

两个常量定义在文件最顶部：

```rust
// crossbeam-utils/src/backoff.rs
const SPIN_LIMIT: u32 = 6;
const YIELD_LIMIT: u32 = 10;
```

见 [backoff.rs:5-6](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/backoff.rs#L5-L6)。注意 `SPIN_LIMIT < YIELD_LIMIT`，二者之间恰好留出了 4 个 `yield_now` 的台阶（step 7~10），让线程在「彻底放弃自旋、转入阻塞」之前先尝试几次「让出时间片」这种较轻的退让。

`step` 的增长由各方法末尾的一句控制（如 `spin()` 末尾）：

```rust
if self.step.get() <= SPIN_LIMIT {
    self.step.set(self.step.get() + 1);
}
```

也就是说 `step` 在 `spin()` 里最多涨到 `SPIN_LIMIT + 1 = 7` 就停；`snooze()` 里把上限换成 `YIELD_LIMIT`，最多涨到 11。这是「封顶」的来源。

#### 4.2.4 代码实践

**实践目标**：用代码验证表格里的指数序列。

1. 写一段程序：`let b = Backoff::new();` 然后**不实际退避**，而是用反射退避节奏的方式——连续调用 `b.spin()`，在每次调用前后打印……（注意 `step` 是私有字段，无法直接读）。
2. 改用更直接的办法：手写一个等价的退避器 `MyBackoff { step: u32 }`，复刻 `spin()` 的自旋次数公式 `1 << self.step.min(SPIN_LIMIT)`，每次调用打印 `1 << step`，连续调用 10 次。

**需要观察的现象**：打印序列应为 `1, 2, 4, 8, 16, 32, 64, 64, 64, 64`。

**预期结果**：前 7 次指数增长到 64，之后封顶。

#### 4.2.5 小练习与答案

**练习 1**：把 `SPIN_LIMIT` 调大到 10 会带来什么坏处？

> 参考答案：自旋次数会一直翻倍到 \(2^{10}=1024\)，单次失败重试的延迟与功耗显著上升；在高争用下反而让 CPU 在无意义的 `PAUSE` 上空耗更久。

**练习 2**：为什么 `SPIN_LIMIT` 要小于 `YIELD_LIMIT`，而不是相等？

> 参考答案：留出中间的若干个 `step`，让退避在「纯自旋」和「转入阻塞」之间有一个「让出时间片」的缓冲档位；这样短暂争用能快速自旋消化，较长的等待又能及时把 CPU 让出去，不必一上来就阻塞。

### 4.3 spin() vs snooze()：两种退避模式

#### 4.3.1 概念说明

这是本讲最重要的一组区分。crossbeam 把退避分成两种「语义」，对应两种等待情境：

- **`spin()`——「别人前进了，我快速重试」**。用于无锁循环里 CAS 失败：你知道**有人刚刚成功改了状态**，所以很可能下一次马上就能成功，应当低延迟地立即重试。它**只执行 `PAUSE`/`YIELD` 指令，绝不让出时间片**。
- **`snooze()`——「我在等别人前进」**。用于阻塞式等待：你在等一个由**别的线程在未来某个时刻**才会推进的条件（如某个槽位被写入、某个标志变 `true`）。它先自旋若干次，超过 `SPIN_LIMIT` 后开始调用 `thread::yield_now()` 把时间片让给调度器。

一句话记忆：**失败重试用 `spin`，条件等待用 `snooze`**。

#### 4.3.2 核心流程

```text
spin():
  自旋 1 << min(step, SPIN_LIMIT) 次（PAUSE 指令）
  step 在 <= SPIN_LIMIT 时 +1
  → 永不让出时间片；step 封顶于 7

snooze():
  if step <= SPIN_LIMIT:
      自旋 1 << step 次
  else:
      std 环境：thread::yield_now()    ← 让出整个时间片
      no_std 环境：继续自旋 1 << step 次（无 OS 可让）
  step 在 <= YIELD_LIMIT 时 +1
  → step 封顶于 11，之后 is_completed() = true
```

#### 4.3.3 源码精读

先看 `spin()`，它非常短：

```rust
// crossbeam-utils/src/backoff.rs:145-154
#[inline]
pub fn spin(&self) {
    for _ in 0..1 << self.step.get().min(SPIN_LIMIT) {
        hint::spin_loop();
    }
    if self.step.get() <= SPIN_LIMIT {
        self.step.set(self.step.get() + 1);
    }
}
```

- `1 << self.step.get().min(SPIN_LIMIT)`：自旋次数随 `step` 翻倍，但用 `.min(SPIN_LIMIT)` 封顶，因此最多 `1 << 6 = 64` 次。
- `hint::spin_loop()`：内部就是 CPU 的 `PAUSE`/`YIELD` 提示指令。它通过 [backoff.rs:3](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/backoff.rs#L3) 引入的 `crate::primitive::hint` 提供——正常编译映射到 `core::hint::spin_loop`，开启 `crossbeam_loom` 时映射到 `loom::hint::spin_loop` 以便模型检查（见 [lib.rs:49-83](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/lib.rs#L49-L83)）。
- 注意 `spin()` **永远不会让 `step` 超过 7**，所以单靠 `spin()`，`is_completed()` 永远不会变 `true`——它就是为「短促重试」设计的。

再看 `snooze()`，它比 `spin()` 多了一个「让出时间片」的分叉：

```rust
// crossbeam-utils/src/backoff.rs:206-225
#[inline]
pub fn snooze(&self) {
    if self.step.get() <= SPIN_LIMIT {
        for _ in 0..1 << self.step.get() {
            hint::spin_loop();
        }
    } else {
        #[cfg(not(feature = "std"))]
        for _ in 0..1 << self.step.get() {
            hint::spin_loop();
        }
        #[cfg(feature = "std")]
        ::std::thread::yield_now();
    }
    if self.step.get() <= YIELD_LIMIT {
        self.step.set(self.step.get() + 1);
    }
}
```

- 前 7 次（`step <= 6`）：与 `spin()` 一样指数自旋。
- 第 8 次起（`step > SPIN_LIMIT`）：在 `std` 环境调用 `thread::yield_now()` 让出时间片；在 `no_std` 环境因为没有调度器可让，只能继续自旋（文档注释 [backoff.rs:163](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/backoff.rs#L163) 因此说 `no_std` 下 `snooze` 等价于 `spin`）。
- `step` 上限是 `YIELD_LIMIT`，所以 `snooze()` 能把 `step` 推到 11，使 `is_completed()` 变 `true`。

真实项目里两种方法的分工，看 `ArrayQueue` 的 `pop` 就一目了然：CAS 抢 `head` 失败（别的线程抢到了，等于「别人前进了」）用 `spin`；而槽位的 `stamp` 还没更新到位（要等持有者写入，属于「等别人前进」）用 `snooze`：

```rust
// crossbeam-queue/src/array_queue.rs:358-377（pop 内部）
Err(h) => { head = h; backoff.spin(); }   // CAS 失败 → spin
...
backoff.spin();                            // 重读后重试 → spin
...
// Snooze because we need to wait for the stamp to get updated.
backoff.snooze();                          // 等 stamp 更新 → snooze
```

channel 那边，`Context::wait_packet` 在「等一个 packet 出现」这种纯条件等待里用的也是 `snooze`：[context.rs:130-137](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/context.rs#L130-L137)。

#### 4.3.4 代码实践

**实践目标**：亲手验证 `snooze()` 在「让出时间片」后会显著降速，并体会二者选型。

1. 写一个「忙等 `AtomicBool` 变 `true`」的循环，分别用 `spin()` 和 `snooze()` 退避，由另一个线程睡眠 50ms 后置位。
2. 在等待循环里同时累加「循环了多少次」，最后打印该计数。

**需要观察的现象**：用 `spin()` 时循环次数会非常多（它几乎不歇），CPU 占用接近 100%；用 `snooze()` 时循环次数明显少很多（因为开始 `yield_now`），CPU 占用下降。

**预期结果**：`spin()` 版本的循环计数比 `snooze()` 版本高一到数个数量级（具体数值 **待本地验证**）。

**正确使用提示**：因为 `Backoff` 不是 `Sync`，务必在每个线程、每次操作里新建一个局部 `Backoff`（就像 `array_queue.rs` 里 `let backoff = Backoff::new();` 那样），不要试图共享。

#### 4.3.5 小练习与答案

**练习 1**：实现自旋锁时，加锁失败应该用 `spin()` 还是 `snooze()`？为什么？

> 参考答案：两者都有道理，关键看「持有锁的时间」。锁被持有极短、且你预期马上会释放时用 `spin()`（低延迟）；如果临界区可能较长，用 `snooze()` 更早地让出时间片，避免空耗 CPU。本讲的综合实践会让你用微基准实测二者的吞吐差异。

**练习 2**：单靠反复调用 `spin()`，`is_completed()` 会变成 `true` 吗？

> 参考答案：不会。`spin()` 把 `step` 封顶在 `SPIN_LIMIT + 1 = 7`，而 `is_completed()` 要求 `step > YIELD_LIMIT(10)`。只有 `snooze()` 能把 `step` 推过 10。

### 4.4 is_completed() 与阻塞决策

#### 4.4.1 概念说明

自旋和让时间片都是「忙等」：线程仍然是可运行状态，仍在消耗 CPU。当一个条件迟迟不满足时，继续忙等就是浪费。更省资源的做法是**真正阻塞**这个线程——比如 `std::thread::park()` 或等待一个 `Condvar`——把 CPU 完全让出来，直到别人显式唤醒你（`unpark` 或 `notify`）。

`is_completed()` 就是 `Backoff` 给你的「切换信号」：当它返回 `true`，等于在说「我已经替你退避得够久了，别再忙等，去阻塞吧」。把它和 `snooze()` 组合，就得到经典的「先自旋、再让时间片、最后阻塞」三段式等待。

#### 4.4.2 核心流程

```text
while 条件未满足:
    if backoff.is_completed():
        thread::park()      # 或 condvar.wait()
    else:
        backoff.snooze()
```

注意一个关键点：**「谁把条件置位，谁负责唤醒」**。如果你用 `park` 阻塞了线程，那么把 `AtomicBool` 设为 `true` 的那个线程，必须紧接着调用被阻塞线程的 `unpark()`，否则被阻塞线程会一直睡下去。

#### 4.4.3 源码精读

`is_completed()` 的实现只有一行，但它和 `snooze()` 的封顶值是配套设计的：

```rust
// crossbeam-utils/src/backoff.rs:270-273
#[inline]
pub fn is_completed(&self) -> bool {
    self.step.get() > YIELD_LIMIT
}
```

即 `step > 10`，等价于 `step >= 11`——这正是 `snooze()` 把 `step` 推到的封顶值。所以「反复 `snooze()` 直到 `is_completed()`」恰好对应「自旋 7 档 + 让时间片 4 档 = 11 档」的完整升级曲线。

文档里给的最佳范本是 `blocking_wait`：

```rust
// crossbeam-utils/src/backoff.rs:63-73（文档示例）
fn blocking_wait(ready: &AtomicBool) {
    let backoff = Backoff::new();
    while !ready.load(SeqCst) {
        if backoff.is_completed() {
            thread::park();        // 退避完成 → 真正阻塞
        } else {
            backoff.snooze();      // 否则继续升级退避
        }
    }
}
```

而唤醒方（在另一个线程里）必须成对地「置位 + unpark」：

```rust
// crossbeam-utils/src/backoff.rs:257-261（文档示例）
thread::spawn(move || {
    thread::sleep(Duration::from_millis(100));
    ready2.store(true, SeqCst);
    waiter.unpark();               // 必须唤醒被 park 的线程
});
```

完整可运行版本见 [backoff.rs:241-266](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/backoff.rs#L241-L266)。

#### 4.4.4 代码实践

**实践目标**：用 `is_completed()` 把忙等改造成「短忙等 + 长阻塞」，并验证唤醒配对。

1. 复制文档里的 `blocking_wait` 范本，主线程先拿到 `thread::current()` 的句柄，传给一个子线程。
2. 子线程睡眠 200ms 后置位 `ready` 并调用主线程句柄的 `unpark()`。
3. 主线程调用 `blocking_wait`。

**需要观察的现象**：主线程在退避升级阶段（前 ~11 次 `snooze`）CPU 有占用，之后进入 `park` 几乎不占 CPU，直到被 `unpark` 唤醒。

**预期结果**：程序在大约 200ms 后正常退出，不会死锁；若**故意删掉**子线程里的 `unpark()`，则可能陷入长时间阻塞（体现「谁置位谁唤醒」的必要性）。

#### 4.4.5 小练习与答案

**练习 1**：如果把 `is_completed()` 换成「永远返回 `false`」会怎样？

> 参考答案：线程永远不会 `park`，会一直 `snooze()`（封顶后持续 `yield_now`）忙等到条件满足。功能上仍正确，但在条件长时间不满足时会持续占用一个 CPU 核做无意义的让出—重跑循环。

**练习 2**：为什么唤醒方在 `store(true)` 之后还要 `unpark()`，二者缺一不可？

> 参考答案：`store(true)` 只是改了条件，并不能唤醒任何正在 `park` 的线程；`unpark()` 才是真正的唤醒动作。反过来，只 `unpark` 不 `store`，被唤醒的线程重新检查条件仍为 `false`，会再次阻塞。所以「置位 + 唤醒」必须成对，并且要防范「检查条件」与「park」之间的竞态（这正是 crossbeam-utils 里 `Parker` 要用三态状态机解决的问题，见后续 u2-l5 讲义）。

## 5. 综合实践：实现一个 Backoff 自旋锁并做微基准

把本讲三个最小模块串起来，完成下面这个贯穿性任务。

**任务**：实现自旋锁 `SpinLock`，提供 `spin()` 与 `snooze()` 两个加锁变体，并在 4 线程高争用下对比吞吐。

下面是 **示例代码**（非项目原有代码），可直接放进一个依赖了 `crossbeam-utils` 的小 crate 里运行：

```rust
// 示例代码：Backoff 自旋锁 + 微基准
use crossbeam_utils::Backoff;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::Arc;
use std::thread;
use std::time::Instant;

pub struct SpinLock {
    locked: AtomicBool,
}

impl SpinLock {
    pub const fn new() -> Self {
        Self { locked: AtomicBool::new(false) }
    }

    // 变体 A：CAS 失败用 spin（低延迟，适合极短临界区）
    pub fn lock_spin(&self) {
        let backoff = Backoff::new(); // 每次加锁新建一个局部 Backoff
        while self
            .locked
            .compare_exchange_weak(false, true, Ordering::Acquire, Ordering::Relaxed)
            .is_err()
        {
            backoff.spin();
        }
    }

    // 变体 B：CAS 失败用 snooze（更早让出时间片，适合较长临界区）
    pub fn lock_snooze(&self) {
        let backoff = Backoff::new();
        while self
            .locked
            .compare_exchange_weak(false, true, Ordering::Acquire, Ordering::Relaxed)
            .is_err()
        {
            backoff.snooze();
        }
    }

    pub fn unlock(&self) {
        self.locked.store(false, Ordering::Release);
    }
}

fn bench(label: &str, use_snooze: bool) {
    const THREADS: usize = 4;
    const ITERS: usize = 100_000;
    let lock = Arc::new(SpinLock::new());
    let counter = Arc::new(AtomicUsize::new(0));
    let start = Instant::now();
    let handles: Vec<_> = (0..THREADS)
        .map(|_| {
            let (lock, counter) = (lock.clone(), counter.clone());
            thread::spawn(move || {
                for _ in 0..ITERS {
                    if use_snooze { lock.lock_snooze(); } else { lock.lock_spin(); }
                    counter.fetch_add(1, Ordering::Relaxed);
                    lock.unlock();
                }
            })
        })
        .collect();
    for h in handles { h.join().unwrap(); }
    println!(
        "{label}: 耗时 {:?}, 计数 {} (期望 {})",
        start.elapsed(),
        counter.load(Ordering::Relaxed),
        THREADS * ITERS,
    );
}

fn main() {
    bench("spin  变体", false);
    bench("snooze变体", true);
}
```

**操作步骤**：

1. 新建一个 crate，在 `Cargo.toml` 里加 `crossbeam-utils = "0.8"`。
2. 把上面的代码放进 `src/main.rs`，`cargo run --release`（务必用 release，否则退避的细微差异会被未优化代码淹没）。
3. 多跑几轮取稳定值，并尝试把临界区里的 `fetch_add` 换成更长的「假工作」（如一个忙循环），观察两个变体的相对优劣如何翻转。

**需要观察的现象**：

- 临界区极短时，`spin` 变体通常更快（少了 `yield_now` 的调度开销）。
- 把临界区调长、或线程数加多时，`snooze` 变体吞吐更稳，因为它更早让出时间片，减少了争用弹射。

**预期结果**：两个变体计数都应精确等于 `THREADS * ITERS`（验证锁正确）；耗时排序随临界区长度而翻转，**具体数值待本地验证**。

**延伸思考**：本任务的 `SpinLock` 永远忙等，没有用 `is_completed()`。如果想做一个「短忙等 + 长 park」的锁，应该怎么改？（提示：让 `lock_snooze` 在 `is_completed()` 后调用 `thread::park`，并在 `unlock` 里对可能阻塞的线程 `unpark`——但这会引出丢失唤醒问题，正是 u2-l5 `Parker` 要解决的。）

## 6. 本讲小结

- `Backoff` 用一个 `Cell<u32>` 的 `step` 字段，把退避建模成一条「指数自旋 → 让出时间片 → 建议阻塞」的升级曲线；因为 `Cell` 不是 `Sync`，它天然是「每线程/每操作一个局部变量」。
- 两个常量定调：`SPIN_LIMIT = 6` 决定纯自旋阶段（自旋次数 \(2^{\min(\text{step},6)}\)，封顶 64），`YIELD_LIMIT = 10` 决定 `snooze` 阶段封顶于 `step = 11`。
- `spin()` 只发 `PAUSE`/`YIELD` 指令、永不让时间片，适合「CAS 失败、别人已前进」的无锁重试；`snooze()` 先自旋、超过 `SPIN_LIMIT` 后 `yield_now()`，适合「等别人前进」的条件等待。
- `is_completed()`（`step > YIELD_LIMIT`）是「改用 `park`/条件变量真正阻塞」的切换信号；典型用法是 `blocking_wait`：`is_completed` 为真就 `park`，否则 `snooze`。
- 唤醒必须配对：谁把等待条件置位，谁就要负责 `unpark`/`notify`，否则阻塞线程会睡死。
- 真实工程里看得很清楚：`ArrayQueue` 的 CAS 失败用 `spin`、等 `stamp` 更新用 `snooze`，`channel::Context::wait_packet` 的条件等待用 `snooze`。

## 7. 下一步学习建议

- 接下来读 **u2-l2 CachePadded 与伪共享**：它会解释为什么争用字段除了用 `Backoff` 退避，还要靠缓存行对齐来减少争用弹射，二者是互补的手段。
- 之后进入 **u2-l3 AtomicCell**：你会再次看到 `primitive` 抽象和序列锁，并理解 crossbeam-utils 内部原语如何互相组合。
- 想直接看 `Backoff` 的「上层应用」？可以跳读 **u4-l1 ArrayQueue** 里 `push`/`pop` 的完整 CAS 循环，体会 `spin`/`snooze` 在真实无锁队列里如何分工。
- 对「阻塞唤醒」的工程化感兴趣的话，**u2-l5 Parker** 把本讲末尾提到的「短忙等 + 长 park + 防丢失唤醒」做成了一个完整的三态状态机，是本讲 `is_completed` 思路的正式落地。
