# 阻塞式 API：超时、关闭与 io::Read/io::Write 集成

## 1. 本讲目标

本讲承接 [u7-l1](u7-l1-blockingrb-semaphore.md)（`BlockingRb` + `Semaphore` 地基）与 [u4-l3](u4-l3-frozen-wrapper.md)（`Caching` 按需同步包装器），讲解 `ringbuf-blocking` 暴露给用户的「阻塞式 API」。

读完本讲，你应当能够：

- 说清 `BlockingProd` / `BlockingCons` 的结构：它们 = `Caching` 基座 + 一个 `timeout` 字段，数据面方法靠委托复用、阻塞方法是新写的控制面。
- 掌握 `wait_vacant` / `wait_occupied` / `push` / `pop` 等方法的「等待—重试」循环与超时、关闭语义。
- 区分 `WaitError::TimedOut`（等不到、超时）与 `WaitError::Closed`（对端 drop）两种失败，并知道 `pop` 会先取尽积压再报 Closed。
- 在 `std` 下把 `BlockingProd` 当 `io::Write`、`BlockingCons` 当 `io::Read`，当作跨线程字节管道使用。
- 用 `set_timeout` 配置全局超时，并理解它与 `NO_WAIT` / `FOREVER` 三态的关系。

## 2. 前置知识

本讲假设你已掌握以下概念（来自前置讲义）：

- **SPSC 环形缓冲区与双索引**（u2-l1）：`read` / `write` 两个索引描述缓冲区状态，`vacant_len` / `occupied_len` 分别是空闲与占用槽位数。
- **Producer / Consumer / Observer trait**（u3）：`try_push`（满则 `Err(elem)` 不阻塞）、`try_pop`（空则 `None` 不阻塞）、`push_slice` / `pop_slice`（批量，要求 `Copy`）。
- **委托机制 Based + Delegate\***（u3-l5）：空标记 trait + blanket impl，让包装器零成本转发方法到内部 `base`。
- **Caching 包装器**（u4-l4）：`try_push` 在本地判满才 `fetch`、成功立即 `commit`；`SharedRb::split` 默认产出 `CachingProd` / `CachingCons`。
- **hold 标志与 SPSC 不变量**（u5-l2）：`read_is_held` / `write_is_held` 标记两端是否被占用；对端 drop 会复位对应 hold 标志。
- **BlockingRb + Semaphore**（u7-l1）：`BlockingRb` = `SharedRb` + 两个二值信号量 `read` / `write`；推进 write 索引或 `hold_write` 会 `write.give()`，推进 read 索引或 `hold_read` 会 `read.give()`。规律是「**信号量名 = 你等待的索引**」：生产者等空位（等 read 索引推进）→ 等 `read` 信号量；消费者等数据（等 write 索引推进）→ 等 `write` 信号量。
- **Semaphore trait**（u7-l1）：`give`（幂等置位）、`try_take`（非阻塞取旧值）、`take(Option<Duration>)`（阻塞取，超时返 false）、`take_iter(timeout)`（返回 `TakeIter`）、`NO_WAIT = Some(ZERO)`、`FOREVER = None`。

一个关键直觉：核心 crate 的 `try_*` 方法是「立即成功或失败」的非阻塞接口；本讲的阻塞 API 只是在它外面包了一层「**失败了就等一会儿再试**」的循环，等待靠的就是 u7-l1 的信号量。所以本讲没有新的数据结构，只有新的**控制流**。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `blocking/src/wrap/mod.rs` | 定义 `BlockingWrap` 结构体（三字段：钥匙 `rb` + `Caching` 基座 `base` + `timeout`），`Based` / `Wrap` 实现，以及 `WaitError` 枚举。 |
| `blocking/src/wrap/prod.rs` | `BlockingProd`（写端）：`wait_vacant` / `push` / `push_exact` / `push_all_iter` 与 `io::Write` 实现。 |
| `blocking/src/wrap/cons.rs` | `BlockingCons`（读端）：`wait_occupied` / `pop` / `pop_exact` / `pop_until_end` / `pop_all_iter` 与 `io::Read` 实现。 |
| `blocking/src/sync.rs` | `Semaphore` trait、`TakeIter`（带 `reset`）、`TimeoutIter`（剩余超时计算）——阻塞循环的同步引擎。 |
| `blocking/src/rb.rs` | `BlockingRb`：在 `set_write_index` / `set_read_index` / `hold_*` 处 `give` 信号量，是唤醒的注入点。 |
| `blocking/src/alias.rs` | `BlockingHeapRb` / `BlockingStaticRb` 类型别名。 |
| `blocking/src/tests.rs` | 真实用例：`wait` / `slice_all` / `vec_all` / `iter_all` / `write_read` 五个跨线程测试。 |

## 4. 核心概念与源码讲解

### 4.1 BlockingProd / BlockingCons 的结构：Caching 基座 + timeout 字段

#### 4.1.1 概念说明

`BlockingProd` 和 `BlockingCons` 不是从零实现的新缓冲区，而是对核心 `Caching` 包装器的**再包装**。它做两件事：

1. **继承数据面**：所有非阻塞方法（`try_push` / `try_pop` / `push_slice` / `pop_slice` / `vacant_len` / `occupied_len` …）原样复用，靠委托机制零成本转发到内部的 `Caching` 基座。
2. **新增控制面**：一组阻塞方法（`push` / `pop` / `wait_*` 等），它们的本质是「调用非阻塞方法 → 失败就等信号量 → 重试」，并多带一个**全局超时**字段 `timeout: Option<Duration>`。

这样设计的好处是：阻塞逻辑与无锁数据搬运彻底解耦。u7-l1 已经把「什么时候 `give` 信号量」埋进了 `BlockingRb` 的 `set_*_index` / `hold_*` 里，本讲只需在「等」这一侧消费这些信号量。

#### 4.1.2 核心流程

`BlockingWrap` 是一个带两个 const generic 布尔 `P`（写权）/ `C`（读权）的泛型结构，与 `Direct` / `Caching` 同构：

```
BlockingWrap<R, P, C> {
    rb:     R,                   // 钥匙（RbRef），用来拿到底层 BlockingRb 的信号量
    base:   Caching<R, P, C>,    // Caching 基座，真正干活的人
    timeout: Option<Duration>,   // 全局超时，None = FOREVER
}

type BlockingProd<R> = BlockingWrap<R, true, false>;   // 只写
type BlockingCons<R> = BlockingWrap<R, false, true>;   // 只读
```

- `new(rb)`：克隆一份钥匙给 `rb` 字段，再用同一钥匙构造 `Caching` 基座（`Caching::new` 会经 `Frozen::new` 断言 hold 标志，保证 SPSC 唯一性），`timeout` 默认 `None`（即 `FOREVER`，永久等待）。
- 阻塞方法统一通过 `self.base.xxx(...)` 调用 Caching 的非阻塞方法，通过 `self.rb.rb()` 拿到底层 `BlockingRb` 再取它的信号量。

#### 4.1.3 源码精读

结构体定义与构造器：

[blocking/src/wrap/mod.rs:12-16](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/wrap/mod.rs#L12-L16) — `BlockingWrap` 三字段结构，`base` 就是 `Caching` 基座。

[blocking/src/wrap/mod.rs:18-25](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/wrap/mod.rs#L18-L25) — `new`：钥匙克隆一份、构造 Caching、`timeout` 默认 `None`。

委托基座——`Based` 指向 `Caching`，于是 `DelegateObserver` / `DelegateProducer` / `DelegateConsumer` 的 blanket impl 会把所有核心 trait 方法转发过来：

[blocking/src/wrap/mod.rs:31-39](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/wrap/mod.rs#L31-L39) — `Based for BlockingWrap`，`type Base = Caching<R, P, C>`。

[blocking/src/wrap/prod.rs:13-16](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/wrap/prod.rs#L13-L16) — `BlockingProd` 别名 + 两个空标记 trait `DelegateObserver` / `DelegateProducer`（详见 u3-l5）。这两个空 impl 让 `BlockingProd` **免费获得** `try_push` / `push_slice` / `vacant_len` 等全部 Producer 方法。

[blocking/src/wrap/cons.rs:13-16](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/wrap/cons.rs#L13-L16) — `BlockingCons` 别名 + `DelegateObserver` / `DelegateConsumer`，对称地获得 `try_pop` / `pop_slice` / `occupied_len` 等。

超时配置与关闭探测：

[blocking/src/wrap/prod.rs:29-34](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/wrap/prod.rs#L29-L34) — `set_timeout` / `timeout` 读写全局超时字段。

[blocking/src/wrap/prod.rs:25-27](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/wrap/prod.rs#L25-L27) — `BlockingProd::is_closed`：`!self.read_is_held()`。生产者的对端是消费者，消费者持有 `read` 端；消费者 drop 会复位 `read_held`，于是 `read_is_held()` 变 false、`is_closed()` 变 true。（`read_is_held` 是 `Observer` 的只读方法，由委托转发到 Caching 再到 `BlockingRb`。）

#### 4.1.4 代码实践

**实践目标**：确认 `BlockingProd` 既有委托来的非阻塞方法，又有新写的阻塞方法。

**操作步骤**：

1. 在 `blocking` crate 下写一个临时二进制（或 `cargo test` 风格的小函数），创建 `BlockingHeapRb::<u32>::new(4)` 并 `split()`。
2. 对 `prod` 调用委托来的 `vacant_len()`（非阻塞、来自 Observer），再调用本讲新写的 `prod.push(1)`（阻塞）。
3. 用 `prod.timeout()` 确认默认是 `None`（FOREVER）。

**预期结果**：两套方法都能编译通过——前者证明委托生效，后者证明控制面已挂上；`timeout()` 返回 `None`。

> 说明：本实践为「源码阅读 + 编译验证」型，无需观察运行时输出。

#### 4.1.5 小练习与答案

**练习 1**：`BlockingProd` 只实现了 `DelegateObserver` 和 `DelegateProducer` 两个空 trait，为什么就能调用 `push_slice`？

**答案**：`push_slice` 是 `Producer` trait 的默认方法。`DelegateProducer` 是空标记 trait，配 u3-l5 的 blanket impl，把 `Producer` 的全部方法（含默认方法）转发到 `base`（即 `Caching`），`Caching` 再落到 `SharedRb`。所以 `BlockingProd` 无需自己写 `push_slice`。

**练习 2**：为什么 `BlockingProd::is_closed` 查的是 `read_is_held()` 而不是 `write_is_held()`？

**答案**：生产者的「对端」是消费者，消费者持有 `read` 端（`hold_read`）。消费者 drop 会复位 `read_held`，所以生产者用 `!read_is_held()` 判断对端是否还在。查 `write_is_held` 没意义——那是生产者自己持有的标志。

---

### 4.2 WaitError：TimedOut 与 Closed 两种失败

#### 4.2.1 概念说明

阻塞方法可能失败，失败原因只有两类，由 `WaitError` 枚举表达：

- `WaitError::TimedOut`：在超时预算内始终没等到条件满足（缓冲区一直满 / 一直空）。
- `WaitError::Closed`：对端已经 drop（消费者走了 / 生产者走了），继续等下去也不会有进展。

这是两种**性质不同**的失败：超时是「暂时不行，以后可能行」；关闭是「永久不行，别再等了」。调用方通常对二者反应不同——超时可以重试或降级，关闭应当收尾退出。

注意返回类型因方法而异：

| 方法 | 成功 | 失败 |
| --- | --- | --- |
| `wait_vacant` / `wait_occupied` | `Ok(())` | `Err(WaitError)` |
| `push` | `Ok(())` | `Err((WaitError, Item))` —— **把元素一起退还** |
| `pop` | `Ok(Item)` | `Err(WaitError)` |

`push` 失败时把元素塞回返回值，是为了**不丢数据**——和核心 `try_push` 返回 `Err(elem)` 的设计一脉相承。

#### 4.2.2 核心流程

`WaitError` 的判定并不复杂，关键在于每个阻塞方法都遵循同一个三出口循环（详见 4.3）：条件满足 → `Ok`；对端关闭 → `Err(Closed)`；等待迭代器耗尽（超时）→ `Err(TimedOut)`。

一个容易忽略的细节：`pop` 在返回 `Closed` 前会**先尝试取走缓冲区里残留的元素**。即「关闭」不等于「立即抛弃积压数据」——只要还有数据，`pop` 照样返回 `Ok(item)`，直到真正空了且对端已关闭，才返回 `Err(Closed)`。这与 [u6-l2](u6-l2-async-producer-consumer-futures.md) 异步版的语义完全一致。

#### 4.2.3 源码精读

[blocking/src/wrap/mod.rs:61-65](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/wrap/mod.rs#L61-L65) — `WaitError` 枚举，派生了 `Clone/Copy/PartialEq/Eq/Hash/Debug`，可直接 `match` 与比较。

`pop` 的「先取后判关」顺序（注意 `try_pop` 在 `is_closed` 之前）：

[blocking/src/wrap/cons.rs:49-59](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/wrap/cons.rs#L49-L59) — `pop`：每轮先 `try_pop`，取到就 `Ok(item)` 返回；只有取不到**且** `is_closed()` 才返回 `Err(Closed)`；循环耗尽则 `Err(TimedOut)`。这正是「取尽积压再报关闭」的来源。

对照 `push` 的元素退还：

[blocking/src/wrap/prod.rs:49-60](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/wrap/prod.rs#L49-L60) — `push`：返回 `Result<(), (WaitError, Item)>`，超时与关闭都把 `item` 带回。

#### 4.2.4 代码实践

**实践目标**：亲手制造两种 `WaitError`。

**操作步骤**（示例代码）：

```rust
// 示例代码：演示两种 WaitError
use ringbuf_blocking::{traits::*, wrap::WaitError, BlockingHeapRb};
use std::time::Duration;

// —— 制造 TimedOut：缓冲区满、消费者不取 ——
let rb = BlockingHeapRb::<u32>::new(2);
let (mut prod, cons) = rb.split(); // 注意：cons 留在作用域里不 drop，保持 read 端 held
prod.set_timeout(Some(Duration::from_millis(50)));
assert_eq!(prod.push(1), Ok(()));
assert_eq!(prod.push(2), Ok(()));
match prod.push(3) {                       // 满，且消费者没在取
    Err((WaitError::TimedOut, item)) => println!("超时，元素 {item} 被退还"),
    other => panic!("预期 TimedOut，实际 {other:?}"),
}
drop(cons); // 现在消费者走了

// —— 制造 Closed：对端已 drop ——
match prod.push(4) {
    Err((WaitError::Closed, _)) => println!("对端已关闭"),
    other => panic!("预期 Closed，实际 {other:?}"),
}
```

**需要观察的现象**：第 3 次 `push` 阻塞约 50ms 后打印「超时」；`drop(cons)` 后第 4 次 `push` 立即（或在下一轮等待循环里）返回 `Closed`。

**预期结果**：两条 `println` 都触发。`push` 失败时 `item` 始终被退还，没有数据丢失。

> 说明：超时是真实的线程阻塞，运行耗时可观察到约 50ms 的停顿。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `push` 的错误类型是 `(WaitError, Item)` 而 `pop` 只是 `WaitError`？

**答案**：`push` 拥有要写入的元素的所有权，失败时若不退还就会丢数据，所以把 `Item` 塞进返回值。`pop` 失败时根本没拿到元素（缓冲区空），没什么可退还的，所以只返回 `WaitError`。

**练习 2**：若生产者已经 drop，但缓冲区里还剩 3 个元素，连续调用 3 次 `pop` 会得到什么？

**答案**：前 3 次都返回 `Ok(元素)`（按 FIFO 顺序），第 4 次才返回 `Err(WaitError::Closed)`。因为 `pop` 每轮先 `try_pop`，取得到就返回，只有空且关闭才报 Closed。

---

### 4.3 阻塞同步循环：wait_iter! 宏与 wait_vacant / wait_occupied

#### 4.3.1 概念说明

所有阻塞方法共享同一个「等待—重试」引擎，由一个内部宏 `wait_iter!` 产出。理解了它，4.4 的 `push` / `pop` 等不过是「在这个循环里塞一段非阻塞操作」。

这个引擎解决三个问题：

1. **怎么等**：等 u7-l1 的信号量。生产者等 `read` 信号量（消费者读走数据腾出空位时 `give`），消费者等 `write` 信号量（生产者写入数据时 `give`）。
2. **怎么不丢唤醒**：首轮先做一次**非阻塞**的 `try_take` 排空已积累的通知，再复查条件；之后才真正阻塞等待。这处理了「对方在我开始等之前就已经 `give` 过」的竞态。
3. **怎么算超时**：用一个 `TimeoutIter` 把总超时预算逐次扣减已流逝时间，得到「本次 `take` 还能等多久」。

`wait_vacant(count)` 和 `wait_occupied(count)` 是最纯粹的形态——它们**只等待、不搬运数据**：等到有 `count` 个空位 / `count` 个元素就返回 `Ok(())`。配合委托来的非阻塞方法（`push_slice` / `pop_slice`），就能实现「先等够再批量搬」的手动模式。

#### 4.3.2 核心流程

统一的循环骨架（以 `wait_vacant` 为例）：

```
for _ in wait_iter!(self) {        // 每轮：先等信号量（首轮非阻塞排空）
    if vacant_len() >= count {     // 条件满足
        return Ok(());
    }
    if is_closed() {               // 对端走了
        return Err(WaitError::Closed);
    }
}
Err(WaitError::TimedOut)           // 循环耗尽 = 超时预算用完
```

`wait_iter!(self)` 在 `BlockingProd` 中展开为：

```
self.rb.rb().read.take_iter(self.timeout()).reset()
```

逐段拆解：

- `self.rb`：钥匙（`RbRef`）。
- `.rb()`：拿到底层 `BlockingRb`。
- `.read`：生产者等待的那个信号量（**注意是 `read` 不是 `write`**——生产者等空位，空位来自消费者推进 `read` 索引）。
- `.take_iter(timeout)`：返回一个 `TakeIter`（`sync.rs`）。
- `.reset()`：把 `TakeIter` 的 `reset` 标志置 true，使**首轮**走非阻塞的 `try_take` 路径。

`TakeIter::next()` 的两条路径：

- **首轮（reset 为真）**：清标志，执行 `semaphore.try_take()`（非阻塞取旧值，把已积累的 `give` 排空），返回 `Some(())`——**不消耗超时预算**。
- **后续轮**：调用 `semaphore.take(timeout_iter.next()?)`。`timeout_iter.next()` 返回本次还能等多久：
  - `FOREVER`（`None`）→ 返回 `Some(None)`，`take` 永久等待直到被 `give`；
  - `Some(dur)` → 返回 `Some(Some(剩余))`，`take` 最多等「剩余」时长；
  - 预算耗尽（`dur <= elapsed`）→ 返回 `None`，`?` 让 `next()` 提前返回 `None`，`for` 循环结束 → 走到 `Err(TimedOut)`。

剩余超时的计算（`TimeoutIter`）：

\[
\text{remaining} = \text{dur} - \text{elapsed}, \quad \text{elapsed} = \text{start.elapsed}()
\]

其中 `start` 在 `TimeoutIter::new` 时记录。若 `remaining > 0` 才继续等，否则判定超时。

> 三态对照：`set_timeout(None)` = `FOREVER`（永不超时，只能被 `give` 或关闭唤醒）；`set_timeout(Some(ZERO))` 等价于 `NO_WAIT`（首轮 `try_take` 后下一轮 `TimeoutIter` 立即返回 `None`，最多一轮，几乎退化为非阻塞）；`set_timeout(Some(d))` 为有限等待。

#### 4.3.3 源码精读

`wait_iter!` 宏——生产者等 `read`、消费者等 `write`：

[blocking/src/wrap/prod.rs:18-22](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/wrap/prod.rs#L18-L22) — 生产者版本：`.read.take_iter(...).reset()`。

[blocking/src/wrap/cons.rs:18-22](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/wrap/cons.rs#L18-L22) — 消费者版本：`.write.take_iter(...).reset()`，对称。

`wait_vacant` / `wait_occupied` 的循环本体：

[blocking/src/wrap/prod.rs:36-47](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/wrap/prod.rs#L36-L47) — `wait_vacant`：等够 `count` 个空位。注意 `debug_assert!(count <= capacity)`——不能要求超过容量的空位，否则永远等不到。

[blocking/src/wrap/cons.rs:36-47](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/wrap/cons.rs#L36-L47) — `wait_occupied`：对称地等够 `count` 个元素。

同步引擎 `TakeIter` 与 `reset`：

[blocking/src/sync.rs:132-151](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/sync.rs#L132-L151) — `TakeIter::next`：`reset` 为真时走 `try_take()` 非阻塞排空；否则 `take(timeout)`，超时（`take` 返回 false）则返回 `None` 终止循环。

[blocking/src/sync.rs:153-158](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/sync.rs#L153-L158) — `reset()`：置标志，链式返回 `Self`，故能 `.take_iter(t).reset()` 一气呵成。

剩余超时计算 `TimeoutIter`：

[blocking/src/sync.rs:109-130](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/sync.rs#L109-L130) — `TimeoutIter::next`：`Some(dur)` 时返回 `Some(Some(dur - elapsed))`（预算耗尽则 `None`）；`None`（FOREVER）时永远返回 `Some(None)`。

信号量的 `give` 注入点（唤醒从哪来）：

[blocking/src/rb.rs:72-83](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/rb.rs#L72-L83) — `BlockingRb` 的 `Producer::set_write_index` 在推进 write 索引后 `self.write.give()`（唤醒等数据的消费者）；`Consumer::set_read_index` 在推进 read 索引后 `self.read.give()`（唤醒等空位的生产者）。这正是 `wait_iter!` 所等通知的来源。

#### 4.3.4 代码实践

**实践目标**：用「手动模式」——`wait_vacant` + 委托来的 `push_slice`——完成批量写入，体会等待与搬运分离的控制力。

**操作步骤**：

1. 阅读真实测试 [blocking/src/tests.rs:25-68](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/tests.rs#L25-L68) 的 `wait` 用例：生产者线程 `prod.set_timeout(...)` 后循环 `wait_vacant(1)` + `push_slice(bytes)`，消费者线程循环 `wait_occupied(1)` + `pop_slice(&mut buffer)`。
2. 在本地 `cd blocking && cargo test wait -- --nocapture` 运行它。

**需要观察的现象**：测试通过；两个线程以容量仅 7 的小缓冲区搬运了一大段文本（`THE_BOOK_FOREWORD.repeat(10)`），靠 `wait_vacant` / `wait_occupied` 自动背压（满了生产者就阻塞等，空了消费者就阻塞等）。

**预期结果**：`assert_eq!(*smsg, rmsg)` 成立——收到的字节与原文完全一致。

> 说明：这是项目自带测试，已验证可运行。它演示了「等待」与「非阻塞搬运」解耦的典型用法：先 `wait_vacant(1)` 保证至少有 1 个空位，再用非阻塞的 `push_slice` 立刻写入（不会失败）。

#### 4.3.5 小练习与答案

**练习 1**：`wait_iter!` 末尾的 `.reset()` 如果去掉会怎样？

**答案**：首轮就不会做 `try_take` 排空。于是若对端在「我检查条件（还不满足）」与「我调用 `take`」之间已经 `give` 过信号量，我仍能在随后的 `take` 里立即取到（因为信号量处于 given 态），所以**正确性不破**；但会丢失「首轮非阻塞快查」的优化——每次进入阻塞方法都至少经历一次真正的阻塞 `take`（哪怕条件其实早已满足），增加无谓的线程挂起/唤醒开销。`reset` 是性能优化，也使 `NO_WAIT` 语义成立。

**练习 2**：生产者的 `wait_iter!` 等的是 `.read` 信号量，为什么不是 `.write`？

**答案**：生产者等的是「空位」，空位因消费者推进 `read` 索引而增加；而 `read` 索引推进时 `BlockingRb` 会 `read.give()`（见 rb.rs L82）。所以「等空位」=「等 `read` 信号量」。`.write` 信号量是消费者等数据时用的。这条「信号量名 = 你等待的索引」规律与 u7-l1 完全一致。

---

### 4.4 数据面阻塞方法：push / pop 与批量变体

#### 4.4.1 概念说明

4.3 的 `wait_*` 只等待不搬运。本小节的方法把「等待 + 搬运」合在一起，是一站式 API。它们分两类：

**单元素 / 单次**：

- `push(item)`：阻塞写入单个元素，满则等。失败退还元素。
- `pop()`：阻塞读出单个元素，空则等。失败返回 `WaitError`。

**批量**（更高效，减少循环与同步次数）：

- `push_exact(&slice)` / `pop_exact(&mut slice)`：要求 `Item: Copy`。把整个切片写完 / 读满才返回，返回实际搬运数。`pop_exact` 在「切片填满」或「关闭且缓冲区空」时停止。
- `push_all_iter(iter)`：从一个迭代器尽可能多地写入，不要求 `Copy`；返回写入数；遇到关闭则提前停。
- `pop_until_end(&mut Vec)`：要求 `alloc`。持续读入 Vec，直到「关闭且空」。内部用 `pop_slice_uninit` 直接写进 Vec 的 spare capacity，避免逐元素 push 的开销。
- `pop_all_iter()`：返回一个惰性迭代器 `PopAllIter`，每次 `next` 调用 `pop().ok()`，直到 `pop` 返回 `Err`（超时或关闭）才产出 `None`。

它们的循环结构与 4.3 完全同构：`for _ in wait_iter!(self) { 做一点搬运; 判完成/判关闭 }`。

#### 4.4.2 核心流程

以 `push` 为例（最典型）：

```
for _ in wait_iter!(self) {
    item = match base.try_push(item) {   // 非阻塞尝试
        Ok(()) => return Ok(()),         // 成功
        Err(e) => e,                     // 满了，拿回元素继续等
    };
    if is_closed() {
        return Err((Closed, item));      // 对端走了
    }
}
Err((TimedOut, item))                    // 超时
```

`push_exact` 的差异在于「循环直到切片写完」：

```
for _ in wait_iter!(self) {
    if is_closed() { break; }
    let n = base.push_slice(slice);      // 非阻塞写一段
    slice = &slice[n..];                 // 推进未写部分
    count += n;
    if slice.is_empty() { break; }       // 全写完
}
return count
```

注意 `push_exact` 在循环开头先判 `is_closed()` 再写——若对端已走，不再徒劳写入，直接返回已写数。`pop_exact` 对称，但停止条件多了「关闭且空」：`if slice.is_empty() || (is_closed() && is_empty()) { break; }`，即「读够了」或「对端关了且没存货了」都停。

#### 4.4.3 源码精读

单元素：

[blocking/src/wrap/prod.rs:49-60](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/wrap/prod.rs#L49-L60) — `push`：`try_push` 成功即返回，失败拿回 `item` 继续等；关闭退还 `(Closed, item)`，超时退还 `(TimedOut, item)`。

[blocking/src/wrap/cons.rs:49-59](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/wrap/cons.rs#L49-L59) — `pop`：`try_pop` 取到即返回，空且关闭返回 `Closed`，循环耗尽返回 `TimedOut`。

批量（`Item: Copy`）：

[blocking/src/wrap/prod.rs:82-107](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/wrap/prod.rs#L82-L107) — `push_exact`：循环 `push_slice` 推进切片，先判关闭、写完即停。

[blocking/src/wrap/cons.rs:66-85](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/wrap/cons.rs#L66-L85) — `pop_exact`：停止条件 `slice.is_empty() || (is_closed() && is_empty())`。

迭代器/Vec 变体：

[blocking/src/wrap/prod.rs:62-80](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/wrap/prod.rs#L62-L80) — `push_all_iter`：`peekable` 迭代器，每轮 `base.push_iter(&mut iter)`，迭代器耗尽或关闭即停。

[blocking/src/wrap/cons.rs:87-107](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/wrap/cons.rs#L87-L107) — `pop_until_end`（`#[cfg(feature = "alloc")]`）：内层 `loop` 用 `pop_slice_uninit(vec.spare_capacity_mut())` 直接写 Vec 备用容量，按需 `reserve`，`unsafe { vec.set_len(...) }` 推进长度；外层 `wait_iter` 循环直到 `is_closed() && is_empty()`。

[blocking/src/wrap/cons.rs:129-139](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/wrap/cons.rs#L129-L139) — `PopAllIter`：`next` 调 `owner.pop().ok()`，`pop` 返回 `Err`（超时/关闭）即产出 `None` 终止迭代。

真实用例：

[blocking/src/tests.rs:70-101](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/tests.rs#L70-L101) — `slice_all`：生产者 `push_exact(&bytes)`、消费者 `pop_exact(&mut bytes)`，各自带 `TIMEOUT`，验证全量搬运。

[blocking/src/tests.rs:103-135](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/tests.rs#L103-L135) — `vec_all`：`pop_until_end(&mut Vec)` 把对端写完关闭后的全部数据收进 Vec。

[blocking/src/tests.rs:137-163](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/tests.rs#L137-L163) — `iter_all`：`push_all_iter(iter)` + `pop_all_iter().collect()`。

#### 4.4.4 代码实践

**实践目标**：对比 `push`（逐个）与 `push_exact`（批量）的行为，并验证 `pop` 在关闭后取尽积压。

**操作步骤**（示例代码）：

```rust
// 示例代码：批量 vs 单个，以及关闭后取尽
use ringbuf_blocking::{traits::*, wrap::WaitError, BlockingHeapRb};
use std::{thread, time::Duration};

let rb = BlockingHeapRb::<u32>::new(4);
let (mut prod, mut cons) = rb.split();
prod.set_timeout(Some(Duration::from_secs(1)));
cons.set_timeout(Some(Duration::from_secs(1)));

let p = thread::spawn(move || {
    assert_eq!(prod.push_exact(&[10, 20, 30, 40, 50]), 5); // 5 个，超过容量也照搬不误
    // prod 在此 drop -> 复位 write_held + write.give() -> 通知消费者
});
let c = thread::spawn(move || {
    let mut got = Vec::new();
    loop {
        match cons.pop() {
            Ok(x) => got.push(x),                      // 先把积压取尽
            Err(WaitError::Closed) => break,           // 取尽且对端关闭
            Err(WaitError::TimedOut) => break,         // 安全网
        }
    }
    got
});
p.join().unwrap();
let got = c.join().unwrap();
assert_eq!(got, vec![10, 20, 30, 40, 50]); // FIFO，全部收到
```

**需要观察的现象**：`push_exact` 在容量仅 4 时仍写入 5 个元素（说明它内部循环等消费者腾位）；消费者 `pop` 循环把 5 个全取到，最后才因 `Closed` 退出。

**预期结果**：`assert_eq!(got, vec![10,20,30,40,50])` 成立。

> 说明：本实践含真实线程阻塞与跨线程同步；若机器极慢，可把超时调大。逻辑上 `push_exact` 会阻塞到全部写完，`pop` 会取尽积压再报 Closed。

#### 4.4.5 小练习与答案

**练习 1**：`push_exact` 在循环开头先 `if self.is_closed() { break; }` 再写，为什么？

**答案**：若消费者已 drop，继续写也没人读，且写满后会永久阻塞（直到超时）。先判关闭可在对端已走时立即停止、返回已写数，避免无谓等待。注意它**不返回错误**——`push_exact` 返回的是「已写数」，调用方据此判断是否写完。

**练习 2**：`pop_all_iter()` 什么时候产出 `None`（停止迭代）？

**答案**：当其 `next` 内部调用的 `pop()` 返回 `Err` 时——即超时（`TimedOut`）或对端关闭且缓冲区空（`Closed`）。`.ok()` 把 `Err` 转成 `None`。所以在 `FOREVER` 超时下，它会一直产出元素直到对端关闭；带超时则可能提前停。

---

### 4.5 io::Read / io::Write 字节管道集成

#### 4.5.1 概念说明

当 `Item = u8` 且开启 `std` feature 时，`BlockingProd` 额外实现 `std::io::Write`、`BlockingCons` 额外实现 `std::io::Read`。于是环形缓冲区两端可以直接当作**字节管道**接入任何 `io::Read` / `io::Write` 生态（文件、网络、`BufReader`、`read_to_end` / `write_all` …），无需手写等待循环。

它们的实现没有发明新机制，就是把 4.3 的等待循环套进 `io` trait 的方法签名：

- `BlockingProd::write(&[u8]) -> io::Result<usize>`：循环 `wait_iter` → `push_slice(buf)`，写出 `n>0` 即返回 `Ok(n)`；关闭返回 `Ok(0)`（对端读端没了，类似「管道破裂」前的 EOF）；超时返回 `Err(io::ErrorKind::TimedOut)`。`flush` 是空操作。
- `BlockingCons::read(&mut [u8]) -> io::Result<usize>`：循环 `wait_iter` → `pop_slice(buf)`，读到 `n>0` 即返回 `Ok(n)`；关闭且空返回 `Ok(0)`（EOF）；超时返回 `Err(io::ErrorKind::TimedOut)`。

两个关键映射：

1. **关闭 → EOF（`Ok(0)`）**：`io::Read` 约定 `Ok(0)` 表示流结束。这里「对端写端 drop」即 EOF。
2. **超时 → `Err(TimedOut)`**：把 `WaitError::TimedOut` 映射成 `io::ErrorKind::TimedOut`，调用方可用 `ErrorKind` 区分。

注意 `flush` 为何是空操作：核心 ringbuf 的写入是「推进 write 索引即发布」（Release store 立即对消费者可见），没有用户态缓冲区需要冲刷，和异步版（u6-l3）的 `poll_flush` 恒为空操作同理。

#### 4.5.2 核心流程

`write` 的循环：

```
for _ in wait_iter!(self) {
    if is_closed() { return Ok(0); }     // 读端没了 -> EOF
    let n = base.push_slice(buf);
    if n > 0 { return Ok(n); }           // 写了多少就返回多少
}
Err(io::ErrorKind::TimedOut.into())      // 超时
```

`read` 的循环：

```
for _ in wait_iter!(self) {
    let n = base.pop_slice(buf);
    if n > 0 { return Ok(n); }           // 读到就返回
    if is_closed() { return Ok(0); }     // 空且写端关了 -> EOF
}
Err(io::ErrorKind::TimedOut.into())      // 超时
```

配合 `std::io` 的扩展方法（`WriteExt::write_all` 会在 `Ok(0)` 时报 `UnexpectedEof`、循环直到写完；`ReadExt::read_to_end` 会循环直到 `Ok(0)`），就得到完整的字节管道语义。

#### 4.5.3 源码精读

[blocking/src/wrap/prod.rs:109-129](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/wrap/prod.rs#L109-L129) — `io::Write for BlockingProd`（`#[cfg(feature = "std")]`，约束 `<Self as Based>::Base: Producer<Item = u8>`）。`write` 用 `base.push_slice(buf)`；关闭 `Ok(0)`，超时 `Err(TimedOut)`；`flush` 直接 `Ok(())`。

[blocking/src/wrap/cons.rs:110-127](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/wrap/cons.rs#L110-L127) — `io::Read for BlockingCons`（同样 `#[cfg(feature = "std")]`，约束 `Base: Consumer<Item = u8>`）。`read` 用 `base.pop_slice(buf)`；读到 `Ok(n)`，空且关闭 `Ok(0)`（EOF），超时 `Err(TimedOut)`。

真实用例——把两端当字节管道：

[blocking/src/tests.rs:165-196](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/tests.rs#L165-L196) — `write_read`：生产者线程 `prod.write_all(&bytes)`，消费者线程 `cons.read_to_end(&mut bytes)`。生产者写完 drop → 复位 `write_held` 并 `write.give()`（见 [blocking/src/rb.rs:90-94](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/rb.rs#L90-L94)）→ 消费者 `read` 收到 EOF（`Ok(0)`）→ `read_to_end` 收尾。这是关闭信号如何经 hold 通道传到 `io::Read` 的完整链路。

#### 4.5.4 代码实践

**实践目标**：用 `io::Write` / `io::Read` 把 `BlockingHeapRb<u8>` 当作跨线程字节管道，传送一段文本。

**操作步骤**：

1. 阅读 [blocking/src/tests.rs:165-196](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/tests.rs#L165-L196) 的 `write_read` 测试。
2. 本地运行：`cd blocking && cargo test write_read -- --nocapture`。

**需要观察的现象**：测试通过；容量仅 7 的缓冲区搬运了大段文本，靠 `write_all` / `read_to_end` 内部的循环 + 阻塞背压自动完成分块传输；生产者线程结束后，消费者 `read_to_end` 因收到 `Ok(0)`（EOF）正常结束。

**预期结果**：`assert_eq!(*smsg, rmsg)` 成立——收到的字节流与原文逐字节一致。

> 说明：这是项目自带、已验证的测试。它演示了「写端 drop → EOF」的管道语义，无需任何额外关闭调用。

#### 4.5.5 小练习与答案

**练习 1**：`BlockingProd::flush` 为什么是空操作？如果它什么都不做，数据怎么到达消费者？

**答案**：核心 ringbuf 写入时，`push_slice` 内部推进 write 索引是一次 `Release` 原子 store，数据立刻对消费者可见（u5-l1、u4-l4），没有用户态写缓冲区需要冲刷。所以 `flush` 无事可做。这与异步版 `poll_flush` 恒为空操作同理（u6-l3）。

**练习 2**：若不设超时（`FOREVER`），`cons.read_to_end` 在生产者还活着但暂时没写数据时会怎样？

**答案**：`read` 会阻塞在 `wait_iter` 的 `take(None)` 上（永久等待），直到生产者再写（`write.give` 唤醒）或 drop（关闭 → `Ok(0)` → EOF → `read_to_end` 结束）。所以 `FOREVER` 下 `read_to_end` 只会在生产者关闭后返回；若要避免无限阻塞，应 `set_timeout(Some(d))`，超时则 `read` 返回 `Err(TimedOut)`、`read_to_end` 会把它当错误向上抛。

---

## 5. 综合实践

**任务**：用 `BlockingHeapRb` 在两个线程间传递数据，一次性演示本讲三大要点——阻塞与超时、关闭语义、`io` 字节管道。

请编写并运行下面的程序（示例代码），它分两幕：

```rust
// 示例代码：综合实践
use ringbuf_blocking::{traits::*, wrap::WaitError, BlockingHeapRb};
use std::{
    io::{Read, Write},
    thread,
    time::Duration,
};

fn main() {
    // —— 第一幕：阻塞、超时、关闭 ——
    let rb = BlockingHeapRb::<u32>::new(2);
    let (mut prod, cons) = rb.split(); // cons 留着不取，保持 read 端 held
    prod.set_timeout(Some(Duration::from_millis(50)));

    assert_eq!(prod.push(1), Ok(()));
    assert_eq!(prod.push(2), Ok(()));      // 缓冲区满
    // 第 3 次：消费者没在取 -> 阻塞约 50ms 后超时
    match prod.push(3) {
        Err((WaitError::TimedOut, item)) => println!("[1] 满了，超时，退还 {item}"),
        other => panic!("[1] 预期 TimedOut，实际 {other:?}"),
    }
    drop(cons);                            // 消费者走了
    match prod.push(4) {
        Err((WaitError::Closed, _)) => println!("[2] 对端关闭"),
        other => panic!("[2] 预期 Closed，实际 {other:?}"),
    }

    // —— 第二幕：io::Write / io::Read 字节管道 ——
    let rb = BlockingHeapRb::<u8>::new(8);
    let (mut prod, mut cons) = rb.split();
    prod.set_timeout(Some(Duration::from_secs(1)));
    cons.set_timeout(Some(Duration::from_secs(1)));

    let msg = b"hello, blocking ringbuf";
    let p = thread::spawn(move || {
        prod.write_all(msg).unwrap();      // 阻塞直到全部写完
                                          // prod drop -> write.give() + write_held=false
    });
    let c = thread::spawn(move || {
        let mut buf = Vec::new();
        cons.read_to_end(&mut buf).unwrap(); // 读到 EOF(Ok(0)) 为止
        buf
    });
    p.join().unwrap();
    let received = c.join().unwrap();
    assert_eq!(received, msg);
    println!("[3] io 管道收到 {} 字节: {:?}", received.len(), String::from_utf8_lossy(&received));
}
```

**操作步骤**：

1. 把上述程序放进 `blocking` crate 的某个 example（例如 `blocking/examples/pipe.rs`），注意 `Cargo.toml` 里 example 需要 `required-features = ["std"]`（参考根 crate examples 的门控习惯）。
2. 运行 `cargo run --example pipe`（在 `blocking` 目录下）。

**需要观察的现象**：

- `[1]`：第 3 次 `push` 打印前有明显停顿（约 50ms，是真实的阻塞超时）。
- `[2]`：`drop(cons)` 后 `push(4)` 立即返回 `Closed`。
- `[3]`：消费者收到完整字节串，与原文一致。

**预期结果**：三行 `[1]`/`[2]`/`[3]` 全部打印，`assert_eq!` 全部成立。

> 说明：本综合实践含线程阻塞与跨线程同步，运行耗时可观察。若环境极慢，把 50ms 调大或把第二幕超时调大即可；逻辑结论不受影响。若不便加 example，可直接参考 `blocking/src/tests.rs` 里 `wait` 与 `write_read` 两个已验证测试作为等价演示。

## 6. 本讲小结

- `BlockingProd` / `BlockingCons` = `Caching` 基座 + `timeout` 字段：数据面方法（`try_push`/`try_pop`/`push_slice`/`pop_slice` …）全靠 `Delegate*` 委托复用，阻塞方法是新写的控制面。
- 所有阻塞方法共享 `wait_iter!` 引擎：每轮先等信号量（首轮 `reset` 走非阻塞 `try_take` 排空），再复查条件 / 关闭；循环耗尽即超时。生产者等 `read` 信号量、消费者等 `write` 信号量（「信号量名 = 你等待的索引」）。
- 失败只有两类：`WaitError::TimedOut`（等不到、超时）与 `WaitError::Closed`（对端 drop）。`push` 失败会把元素退还（`Err((WaitError, Item))`），`pop` 会先取尽积压再报 `Closed`。
- 超时三态：`FOREVER = None`（永不超时）、`NO_WAIT = Some(ZERO)`（几乎退化为非阻塞）、`Some(d)`（有限等待）；`TimeoutIter` 用 `dur - elapsed` 逐次扣减预算。
- `set_timeout` 配置全局超时，作用于该端所有阻塞方法与 `io` 方法。
- `std` + `Item = u8` 下，`BlockingProd: io::Write`、`BlockingCons: io::Read`：关闭映射为 `Ok(0)`（EOF），超时映射为 `Err(io::ErrorKind::TimedOut)`，`flush` 为空操作（写入即发布）。写端 drop 经 hold 通道把 EOF 传给读端。

## 7. 下一步学习建议

- **回头看同步引擎细节**：若想彻底弄懂 `StdSemaphore::take` 如何用 `Condvar + Mutex` 配合 `TimeoutIter` 处理伪唤醒与超时边界，重读 [blocking/src/sync.rs:82-101](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/blocking/src/sync.rs#L82-L101)（u7-l1 已铺垫，可结合本讲的 `wait_iter!` 再看一遍）。
- **对照异步版**：本讲的阻塞 API 与 [u6-l2](u6-l2-async-producer-consumer-futures.md) 的 `AsyncProducer` / `AsyncConsumer` 几乎一一对应（`push`/`pop`/`wait_vacant`/`wait_occupied`/`*_exact`/关闭语义），只是把「信号量阻塞」换成了「Future + AtomicWaker」。对比阅读能加深对「核心 + 同步原语」派生模式的理解。
- **进入专家扩展层**：下一单元 u8 面向二次开发，建议先读 [u8-l1](u8-l1-features-portable-atomic-no-std.md)（feature flags / `no_std` / `portable-atomic`）与 [u8-l2](u8-l2-custom-storage-from-raw-parts.md)（自定义 `Storage`），其中会讲到如何把 `BlockingRb` 搬到 `no_std` 嵌入式平台——只需提供自己的 `Semaphore` 实现。
- **动手改超时策略**：尝试给本讲综合实践加一个「动态超时」——在传输过程中用 `set_timeout` 切换 `FOREVER` 与有限超时，观察 `io::Read` / `io::Write` 在两种模式下的行为差异。
