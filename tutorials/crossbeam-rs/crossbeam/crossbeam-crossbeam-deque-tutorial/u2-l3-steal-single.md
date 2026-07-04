# 单任务偷取：Stealer::steal 与 CAS 竞争

## 1. 本讲目标

上一讲（u2-l2）我们读完了 owner 线程私有的 `Worker::push` 与 `Worker::pop`：它们只动 `back`（push / LIFO pop）和 `front`（FIFO pop / LIFO 最后元素 CAS）。本讲转到「对端」——**远端偷取者 `Stealer`** 的核心操作 `Stealer::steal`，把 `resize` 仍然当黑盒（留到 u2-l5），把 epoch GC 的深层机制留到 u4-l2，本讲只用到「`epoch::pin()` 会顺带发一道 `SeqCst` fence、并把当前线程钉在一个 epoch 上」这两点事实。

读完本讲，你应该能够：

1. 说出 `Steal<T>` 为什么是三态（`Empty` / `Success` / `Retry`）而不是 `Option<T>`，并解释 `Retry` 的语义以及它在 `or_else` / `FromIterator` 组合子里是如何被「放大」的。
2. 逐行看懂 `Stealer::steal` 的七个步骤：**Acquire 读 `front` → 可重入地补 `SeqCst` fence → `epoch::pin()` → Acquire 读 `back` 判空 → Acquire 读 `buffer` 并读槽位 → CAS 推进 `front` → 二次校验 `buffer` 是否被换**。
3. 解释为什么 `is_pinned()` 为真时要**手动补 fence**，而 `is_pinned()` 为假时却**不用补**——也就是 `epoch::pin()` 自带的 fence 折中。
4. 画出「读任务 → CAS `front` → 再校验 `buffer`」的两步偷取时序，并说明竞争失败（被另一个 stealer、或 owner 的 FIFO/LIFO pop 抢先）时为何返回 `Retry` 而非 `Empty`。
5. 理解 `Stealer::is_empty` / `Stealer::len` 为什么用 **「Acquire 读 `front` → `SeqCst` fence → Acquire 读 `back`」** 这个固定顺序。

本讲**不**展开 `epoch::pin()` 的全局 epoch 推进与延迟回收细节（留到 u4-l2），也**不**展开批量偷取（留到 u2-l4）。

## 2. 前置知识

### 2.1 复习：双游标模型与「谁能动什么」

Chase-Lev 队列用两个 `AtomicIsize` 游标表达队列内容：

\[ \text{len} = \text{back} - \text{front} \]

- `front`：队头。**owner 的 FIFO `pop` 与所有 stealer 的 `steal` 都会推进它**。
- `back`：队尾。**只有 owner 写**（`push` 加 1；LIFO `pop` 减 1）。
- 任务实体存在环形 `Buffer` 的槽位 `[front, back)` 区间内。

这是本讲理解所有内存序的钥匙，再用一张表强化「谁能动什么」：

| 字段 | 谁会写 | 谁会读 |
|------|--------|--------|
| `back` | **只有 owner** | owner 与所有 stealer |
| `front` | owner（FIFO pop / LIFO 最后元素 CAS）与 stealer（steal） | owner 与所有 stealer |
| `buffer`（`Inner` 里） | owner（`resize` 时 swap） | stealer（偷取时 load） |

`Stealer` 和 `Worker` 共享同一份 `Arc<CachePadded<Inner<T>>>`（见 [src/deque.rs:574-583](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L574-L583)），区别在于：`Worker` 是 `Send + !Sync`（单线程私有），`Stealer` 是 `Send + Sync + Clone`（可跨线程共享）。

### 2.2 复习：push 的发布顺序

`Stealer::steal` 读的「任务」是由 owner 的 `push` 写进去的。`push` 的发布顺序是（[src/deque.rs:399-433](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L399-L433)）：

```
write_volatile 写槽位 task  →  Release fence  →  Relaxed store back = b+1
```

steal 这边必须「看到 `back` 的推进」时**也一定看到了对应槽位里写好的 task**，这正是靠 owner 的 `Release fence` 与 stealer 的 `Acquire load back` 配对建立的 happens-before。本讲会反复用到这条对应关系。

### 2.3 两个并发原语速查

- **`compare_exchange`（CAS）**：原子地「若当前值等于期望值 `f`，则替换为 `f+1`」，成功返回 `Ok`，失败返回 `Err`。`Stealer::steal` 用它做「乐观抢占 `front`」。
- **`epoch::pin()`**：进入一个 epoch 临界区，返回一个守卫 `Guard`。它有两个本讲要用到的副作用：① **顺带发一道 `SeqCst` 内存屏障**；② 保证在此守卫存活期间，被「退休」的对象（如旧 `Buffer`）不会被真正释放——这正是 steal 里「放心读 `buffer` 槽位」的内存安全前提。

> 注：`epoch::pin()` 自带 `SeqCst` fence 这一事实，来自 `crossbeam-epoch` 的实现约定。本讲只使用该事实；它在 GC 中的完整作用留到 u4-l2。

## 3. 本讲源码地图

本讲内容集中在一个文件：

| 文件 | 本讲涉及的部分 | 作用 |
|------|----------------|------|
| [src/deque.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs) | `Stealer::steal` / `is_empty` / `len`、`Steal` 枚举与组合子 | 单任务偷取的全部实现 |
| [tests/lifo.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/tests/lifo.rs) | `spsc` 测试 | 本讲综合实践的参考写法 |

关键代码定位（行号基于当前 HEAD `6195355`）：

- `Steal<T>` 枚举与三态语义：[src/deque.rs:2085-2094](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L2085-L2094)
- `Steal::or_else` 组合子：[src/deque.rs:2185-2200](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L2185-L2200)
- `Stealer::is_empty` / `len`：[src/deque.rs:598-624](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L598-L624)
- `Stealer::steal` 主体：[src/deque.rs:641-683](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L641-L683)
- `Buffer::read`（volatile 读槽位）：[src/deque.rs:82-90](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L82-L90)

## 4. 核心概念与源码讲解

### 4.1 Steal 三态结果与 Retry 语义

#### 4.1.1 概念说明

普通队列的出队操作返回 `Option<T>`（`Some` / `None`）。但**并发 work-stealing 的偷取操作必须返回三态**，这是本讲最需要先建立的直觉。原因在于偷取失败有两种**本质不同**的原因：

- **`Empty`**：此刻队列**真的没有任务**（`back - front <= 0`）。调用方可以据此进入休眠、去偷别的队列，或者宣告「没活干了」。
- **`Retry`**：队列**其实有任务**，但这次偷取因为竞争（别的线程抢先拿走了、或 owner 正在 `resize`）而**没拿到**。这是一种「伪失败」——调用方应当**立刻重试**，而不是去休眠。

如果只有 `None`，调用方就无法区分「队列为空，可以休息」与「我只是手慢了，得再抢一次」，从而要么忙等到底（吞吐受损），要么误睡（任务被饿死）。所以 `Steal<T>` 是：

```rust
pub enum Steal<T> {
    Empty,        // 队列当时为空
    Success(T),   // 偷到一个任务
    Retry,        // 需要重试
}
```

定义见 [src/deque.rs:2085-2094](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L2085-L2094)。

#### 4.1.2 核心流程

`Retry` 在组合子里的「放大规则」是 `find_task` 回退链能正确工作的关键。两条规则：

1. **`Steal::or_else(f)`**（[src/deque.rs:2185-2200](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L2185-L2200)）：当前是 `Success` 就直接返回；是 `Empty` 就执行 `f` 拿备选；是 `Retry` 时执行 `f`，**只要 `f` 成功就返回成功，否则一律返回 `Retry`**（即使 `f` 返回 `Empty`，也透传出 `Retry`——因为「曾经需要重试」的信号不能被 `Empty` 吞掉）。

2. **`FromIterator`（collect 成 `Steal<T>`）**（[src/deque.rs:2213-2233](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L2213-L2233)）：遍历一组偷取结果，遇到第一个 `Success` 就返回；否则**只要有一个 `Retry`，整体就是 `Retry`**；全是 `Empty` 才是 `Empty`。

这两条规则共同保证：**只要回退链上任何一环报告过 `Retry`，最终结果就不会被误判为 `Empty`**，从而调用方总能正确地「再转一圈」。

#### 4.1.3 源码精读

`or_else` 的分支（注意 `Retry` 分支如何处理 `f()` 的 `Empty`）：

```rust
// src/deque.rs:2185-2200
pub fn or_else<F>(self, f: F) -> Self where F: FnOnce() -> Self {
    match self {
        Self::Empty => f(),
        Self::Success(_) => self,
        Self::Retry => {
            if let Self::Success(res) = f() {
                Self::Success(res)
            } else {
                Self::Retry   // 关键：f() 返回 Empty 时，仍然上报 Retry
            }
        }
    }
}
```

`FromIterator` 的「只要有一个 Retry 就是 Retry」：

```rust
// src/deque.rs:2218-2232
let mut retry = false;
for s in iter {
    match &s {
        Self::Empty => {}
        Self::Success(_) => return s,
        Self::Retry => retry = true,   // 记下「见过 Retry」
    }
}
if retry { Self::Retry } else { Self::Empty }
```

辅助判断 `is_retry()` / `is_empty()` / `success()` 提供了 `if !x.is_retry()` 这样的惯用法（见 [src/deque.rs:2141-2162](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L2141-L2162)），这是 `lib.rs` 里 `repeat_with(...).find(|s| !s.is_retry())` 自动重试模式的基础（详见 u1-l4）。

#### 4.1.4 代码实践

**目标**：用断言体会 `Retry` 在组合子里的放大规则。

把下面这段「示例代码」放进一个 `fn main()`（依赖 `crossbeam-deque = "0.8"`）：

```rust
// 示例代码
use crossbeam_deque::Steal::{self, Empty, Retry, Success};

fn main() {
    // Success 直接短路
    assert_eq!(Success(1).or_else(|| Success(2)), Success(1));
    // Empty 透传到备选
    assert_eq!(Empty.or_else(|| Success(2)), Success(2));
    // 关键：Retry + Empty => Retry（Empty 被吞）
    assert_eq!(Retry::<i32>.or_else(|| Empty), Retry);
    // Retry + Success => Success
    assert_eq!(Retry.or_else(|| Success(2)), Success(2));

    // collect：有一个 Retry 就是 Retry
    let v: Steal<i32> = vec![Empty, Retry, Empty].into_iter().collect();
    assert_eq!(v, Retry);
    // 全 Empty 才是 Empty
    let v: Steal<i32> = vec![Empty, Empty].into_iter().collect();
    assert_eq!(v, Empty);
}
```

**观察**：`Retry.or_else(|| Empty)` 结果是 `Retry` 而非 `Empty`——这正是「伪失败信号不能丢」的体现。
**预期**：全部断言通过，程序无输出正常结束。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `or_else` 的 `Retry` 分支改成「`f()` 返回 `Empty` 就返回 `Empty`」，会在 `find_task` 场景里造成什么后果？

> **答案**：一个 stealer 报 `Retry`（它手慢了，任务还在），但回退链后面的备选恰好返回 `Empty`，整体就被误判成 `Empty`，调用方可能据此休眠，导致**尚在队列里的任务被延迟处理甚至饿死**。所以必须保留 `Retry` 信号。

**练习 2**：`Success::<i32>.is_retry()` 和 `Empty::<i32>.is_success()` 分别返回什么？

> **答案**：都是 `false`。`is_retry()` 仅对 `Retry` 为真；`is_success()` 仅对 `Success(_)` 为真。

---

### 4.2 Stealer::steal 全流程精读

#### 4.2.1 概念说明

`Stealer::steal` 是本讲的主角：从队列的 `front` 端「偷」走一个任务。它和 owner 的 FIFO `pop` 抢的是**同一个 `front` 端**，所以两者天然存在竞争。整个偷取是**乐观两步**：先「假定 `front` 还没变，把槽位里的任务读出来」，再用 CAS「真正占有这个 `front` 槽位」；只有 CAS 成功才算偷到，否则就是一次 `Retry`。

#### 4.2.2 核心流程

`Stealer::steal`（[src/deque.rs:641-683](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L641-L683)）可以拆成 7 步：

```
1. f  = front.load(Acquire)                       // 读队头快照
2. 若 is_pinned()：fence(SeqCst)                  // 可重入补屏障（见 4.3）
3. guard = epoch::pin()                           // 进入 epoch 临界区（顺带一道 fence）
4. b  = back.load(Acquire)                        // 读队尾
     若 b - f <= 0：return Empty                  //    真空，直接判空
5. buffer = inner.buffer.load(Acquire, guard)     // 读 buffer 快照
   task    = buffer.read(f)                       // 乐观读出 front 槽位的任务
6. 若 buffer 被换(load != 快照) 或 front.CAS(f, f+1, SeqCst) 失败：
       return Retry                               // 快照失效或被抢，伪失败
7. return Success(task.assume_init())             // 偷到
```

三个要点先记住，下面逐一展开：

- **第 2 步的 `is_pinned()` 判断**：避免重复发 fence 的折中，单独成节在 4.3 讲。
- **第 5 步的「先读 task 再 CAS」**：是乐观并发的关键，单独成节在 4.4 讲。
- **第 6 步的「`buffer` 是否被换」**：因为 owner 的 `resize` 会 swap `buffer`，二次校验保证我们不会基于失效快照提交，详见 4.4。

#### 4.2.3 源码精读

逐段对应到源码。先是 **第 1～3 步**——读 `front`、按需补 fence、`epoch::pin()`：

```rust
// src/deque.rs:641-654
pub fn steal(&self) -> Steal<T> {
    // Load the front index.
    let f = self.inner.front.load(Ordering::Acquire);

    // A SeqCst fence is needed here.
    // If the current thread is already pinned (reentrantly), we must manually issue the
    // fence. Otherwise, the following pinning will issue the fence anyway, so we don't have to.
    if epoch::is_pinned() {
        atomic::fence(Ordering::SeqCst);
    }

    let guard = &epoch::pin();
```

注释点明了「fence 无论如何都要有一道」的硬性要求。`front` 用 `Acquire` 读，是为了同步到别的消费者（owner FIFO pop 或别的 stealer）对 `front` 的 Release/SeqCst 写。

接着 **第 4 步**——读 `back` 并判空：

```rust
// src/deque.rs:656-662
    // Load the back index.
    let b = self.inner.back.load(Ordering::Acquire);

    // Is the queue empty?
    if b.wrapping_sub(f) <= 0 {
        return Steal::Empty;
    }
}
```

`back` 用 `Acquire` 读，配合 owner `push` 末尾的 `Release fence`，保证「看到 `back` 推进」就一定「看到对应槽位写好的 task」（见 2.2）。`b - f <= 0` 判空，注意用 `wrapping_sub` 处理 `isize` 回绕。

然后 **第 5 步**——读 `buffer` 快照、乐观读出 `front` 槽位的任务：

```rust
// src/deque.rs:664-666
    // Load the buffer and read the task at the front.
    let buffer = self.inner.buffer.load(Ordering::Acquire, guard);
    let task = unsafe { buffer.deref().read(f) };
```

`buffer` 是 `crossbeam_epoch::Atomic<Buffer<T>>`，`load(Acquire, guard)` 在 epoch 守卫下取到一个 `Shared<Buffer<T>>` 并 `deref()`。`Buffer::read` 内部用 `ptr::read_volatile` 读槽位（[src/deque.rs:82-90](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L82-L90)），返回 `MaybeUninit<T>`——因为这一刻我们**还没真正占有**该槽位，不能急着 `assume_init`。

> 关于 `read_volatile`：`Buffer::write` / `read` 可能与 owner 的写发生在同一个槽位，严格说是 data race（UB），用 `volatile` 而非原子操作是为通用类型 `T` 换取性能的已知折中（见 [src/deque.rs:82-90](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L82-L90) 的注释）。其内存序层面的正当性将在 u4-l1 横向串讲。

最后 **第 6～7 步**——CAS 推进 `front`、二次校验 `buffer`、返回结果：

```rust
// src/deque.rs:668-683
    // Try incrementing the front index to steal the task.
    // If the buffer has been swapped or the increment fails, we retry.
    if self.inner.buffer.load(Ordering::Acquire, guard) != buffer
        || self
            .inner
            .front
            .compare_exchange(f, f.wrapping_add(1), Ordering::SeqCst, Ordering::Relaxed)
            .is_err()
    {
        // We didn't steal this task, forget it.
        return Steal::Retry;
    }

    // Return the stolen task.
    Steal::Success(unsafe { task.assume_init() })
}
```

两个失败条件用短路 `||` 串联：

1. **重新 `load` 的 `buffer` 与快照 `buffer` 不相等**——说明 owner 中途 `resize` 把 `buffer` 换掉了，我们的快照已过期。
2. **`front` 的 CAS 失败**——说明别的消费者（stealer 或 owner 的 FIFO/LIFO pop）已经把 `front` 推过去了。

只要任一条件成立，就 `return Retry`：注意这时**不调用** `task.assume_init()`，让这个没真正占有的 `MaybeUninit<T>` 直接被丢弃（注释 `forget it`），从而**绝不重复消费**同一个任务。只有 CAS 成功，才 `assume_init` 取出真正属于自己的任务。

#### 4.2.4 代码实践

**目标**：用最小单线程例子观察 `steal` 的三态返回与「和 owner FIFO `pop` 抢同一端」的关系。

```rust
// 示例代码
use crossbeam_deque::{Steal, Worker};

fn main() {
    let w = Worker::new_fifo();
    let s = w.stealer();

    assert_eq!(s.steal(), Steal::Empty); // 空队列 => Empty

    w.push(1);
    w.push(2);
    // FIFO：front 端是 1。steal 与 owner pop 都从 front 取。
    assert_eq!(s.steal(), Steal::Success(1)); // stealer 抢到队头
    assert_eq!(w.pop(), Some(2));             // owner 再 pop，只剩 2
    assert_eq!(s.steal(), Steal::Empty);      // 已空
}
```

**操作步骤**：把上面代码放入一个依赖了 `crossbeam-deque = "0.8"` 的二进制 crate，`cargo run`。
**观察**：`steal` 返回的是 `Success(1)` 而非 `Success(2)`——证明 steal 从 `front` 端取（和 FIFO pop 同端），而不是从 `back` 端。
**预期**：全部断言通过，正常结束。

#### 4.2.5 小练习与答案

**练习 1**：为什么第 5 步读 `task` 时用 `Buffer::read`（返回 `MaybeUninit<T>`），而最后才 `assume_init`？为什么不能在读到 `task` 时就 `assume_init`？

> **答案**：因为「读出 task」与「真正占有该槽位」是分离的两步。在 CAS 成功之前，这个槽位可能被别人抢走，此时我们**无权**拥有这个 `T`（否则会和真正的占有者 double-consume / double-free）。所以先以 `MaybeUninit` 持有「候选值」，CAS 成功才 `assume_init`，CAS 失败就静默丢弃（`MaybeUninit<T>` 丢弃不会运行 `T::drop`），保证一个任务恰好被消费一次。

**练习 2**：把 `Stealer::steal` 第 1 步读 `front` 的 `Ordering` 从 `Acquire` 改成 `Relaxed`，会破坏什么不变量？

> **答案**：会读不到其他消费者对 `front` 的最新推进，导致 `b - f` 算出的 `len` 偏大、读到已被别人消费的陈旧槽位，并让 CAS 期望值 `f` 经常失配。`Acquire` 才能正确同步到 owner/stealer 对 `front` 的 Release/SeqCst 写。

---

### 4.3 可重入 pin 与 SeqCst fence 的微妙处理

#### 4.3.1 概念说明

`steal` 第 2 步有一段容易看懂却容易看漏的代码：**只有当 `epoch::is_pinned()` 为真时，才手动发一道 `SeqCst` fence**。要理解它，得先接受两个事实：

1. `Stealer::steal` 在「读 `front`」和「读 `back`」之间**必须有一道 `SeqCst` 屏障**（这是 Le 等人弱内存模型论文对 Chase-Lev 的修正要求；详见 [src/deque.rs:101-113](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L101-L113) 引用的三篇论文）。没有它，在弱内存模型下可能出现「看到 `front` 已推进、却读到对应槽位未初始化内容」的灾难。

2. `epoch::pin()` 在**真正 pin（即当前线程未持有 pin）**时，其内部会**顺带发一道 `SeqCst` fence**。所以第 3 步的 `epoch::pin()` 通常已经把这道屏障发了。

于是问题变成：**如果当前线程已经 pin 过**（比如 steal 是在某个外层已经 `epoch::pin()` 的临界区里被调用，即「可重入」），那么第 3 步的 `epoch::pin()` 会退化成一个只增加引用计数的廉价空操作，**不再发 fence**——这时算法就缺了一道屏障。所以代码用 `is_pinned()` 检测这种情况，**手动补上**那道 fence。

#### 4.3.2 核心流程

把「fence 谁来发」画成一张决策图：

```
读 front（Acquire）
   │
   ├── is_pinned() == true（已 pin，pin() 会是空操作）
   │        └── 手动 fence(SeqCst)   ← 自己补
   │
   └── is_pinned() == false（未 pin）
            └── epoch::pin() 内部自带 fence(SeqCst)   ← pin() 补
   │
读 back（Acquire）
```

无论走哪条分支，`front` 与 `back` 之间都恰好有一道 `SeqCst` fence——既不少（保证正确性），也不多（避免未 pin 时的冗余屏障）。

#### 4.3.3 源码精读

核心就是这三行（[src/deque.rs:645-654](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L645-L654)）：

```rust
// A SeqCst fence is needed here.
//
// If the current thread is already pinned (reentrantly), we must manually issue the
// fence. Otherwise, the following pinning will issue the fence anyway, so we don't have to.
if epoch::is_pinned() {
    atomic::fence(Ordering::SeqCst);
}

let guard = &epoch::pin();
```

注释的第一句「A SeqCst fence is needed here」是**硬性需求**；后两句解释「为什么写成条件 fence」——这是对 `crossbeam-epoch` 内部「`pin()` 自带 fence」这一约定的精巧复用。

> 同样的「读 `front` → `SeqCst` fence → 读 `back`」结构，也出现在 `Stealer::is_empty` / `len`（见 4.4）和 `steal_batch_with_limit`（u2-l4）里，是本 crate 的固定范式。

#### 4.3.4 代码实践

**目标（源码阅读型）**：确认「`pin()` 在未 pin 时自带 fence」这一事实在 crate 内是被多处依赖的统一约定。

1. 打开 [src/deque.rs:641-683](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L641-L683)，记下 `steal` 里 `is_pinned()` 与 `epoch::pin()` 的相对位置。
2. 跳到 [src/deque.rs:746](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L746) 的 `steal_batch_with_limit`，观察它是否在「读 `front` 之后、`epoch::pin()` 之前」有同样的 `if epoch::is_pinned() { atomic::fence(SeqCst) }`。
3. 再看 `Stealer::is_empty`（[src/deque.rs:598-603](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L598-L603)）：它**没有** `epoch::pin()`，所以它**无条件**地 `atomic::fence(SeqCst)`。

**观察**：`is_empty` 因为不进 epoch 临界区，必须自己无条件发 fence；`steal` 因为要 `pin()`，所以能省一道（仅可重入时补）。这正是「fence 来源」的一致性体现。
**预期**：三个位置的 `SeqCst` fence 语义统一为「夹在 front 与 back 的读取之间」，只是来源不同。

#### 4.3.5 小练习与答案

**练习 1**：假设 `epoch::pin()` 从不带 fence，本讲的 `if epoch::is_pinned() { fence }` 写法还能保证正确吗？应如何修改？

> **答案**：不能。那样未 pin 的分支就缺了 `front` 与 `back` 之间的屏障。应改成无条件 `atomic::fence(Ordering::SeqCst)`，无论是否 pin 都自己发一道——代价是未 pin 时多发一次冗余屏障（性能略降）。

**练习 2**：为什么 `is_empty` 里是**无条件** `fence(SeqCst)`，而 `steal` 里是**条件** `fence`？

> **答案**：`is_empty` 不调用 `epoch::pin()`（不进 epoch 临界区），所以没有「`pin()` 自带 fence」可借，必须无条件自己发；`steal` 要 `pin()`，未 pin 时 `pin()` 会顺带发，所以只需在已 pin（`pin()` 退化为空操作）时补发。

---

### 4.4 两步偷取的并发语义：CAS 竞争与 steal/pop 关系

#### 4.4.1 概念说明

`steal` 的「读 task → CAS `front` → 再校验 `buffer`」是典型的**乐观并发**：先假设没竞争、把活儿干一半（读出候选 task），最后用一次 CAS「提交」。这样省掉了加锁，但必须处理两类竞争：

- **抢同一个 `front` 槽位**：另一个 stealer，或 owner 的 FIFO `pop`（也在 `front` 端），或 owner 的 LIFO `pop` 在「最后一个任务」时（其 CAS 目标也是 `front`）。这些都被 `front` 的 CAS 统一仲裁——只有一个赢家。
- **owner 中途 `resize` 换了 `buffer`**：我们的 `buffer` 快照过期，需要二次校验。

注意 `Retry` 的语义在这里体现得淋漓尽致：上述竞争失败时，**队列里通常还有任务**（只是被别人抢走了，或正在被 resize），所以返回 `Retry`（「请立刻重试」）而不是 `Empty`（「没任务了」）。

#### 4.4.2 核心流程

**两 stealer 抢同一槽位**（`front` CAS 仲裁）：

```
时间轴 ─────────────────────────────────────────────►
A: load front=f  ──────────  CAS(f, f+1) 成功 ── assume_init(taskA) => Success
B: load front=f  ──────────  CAS(f, f+1) 失败(front 已是 f+1) => Retry
```

**steal 与 owner FIFO pop 抢同一槽位**（owner 用 `fetch_add`，stealer 用 CAS）：

```
owner(FIFO pop): front.fetch_add(1, SeqCst) => 原值 f  → 取槽位 f
stealer:        front.CAS(f, f+1) 失败（fetch_add 已把它变 f+1） => Retry
或反之，stealer CAS 先成 => owner fetch_add 拿到 f+1，再据 b-new_f<0 回退（见 u2-l2）
```

**steal 与 owner LIFO「最后元素」pop**：这是最微妙的一处。当队列只剩一个任务时，owner 的 LIFO `pop` 减完 `back` 后 `len==0`，此时 owner 也要 CAS 推进 `front`（[src/deque.rs:513-528](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L513-L528)）。这个唯一的任务同时是 `front` 端（被 steal）和 `back-1` 端（被 LIFO pop）——两端重合，所以必须靠 `front` 的 CAS 决定唯一赢家：

```
队列只剩 1 个任务，front == back-1
owner(LIFO pop): back -= 1 => len==0 => front.CAS(f, f+1)
stealer:                                  front.CAS(f, f+1)
        ↑ 两个 CAS 同一个 (f, f+1)，只有一个 Ok
```

赢家拿走任务，输家：owner 一侧 `task.take()` 后还原 `back`（[src/deque.rs:526-531](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L526-L531)），stealer 一侧静默丢弃 `MaybeUninit` 并返回 `Retry`（[src/deque.rs:677-679](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L677-L679)）。两边都以「CAS 失败即放弃」保证**恰好消费一次**。

#### 4.4.3 源码精读

二次校验与 CAS 的短路 `||`（[src/deque.rs:668-679](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L668-L679)）：

```rust
if self.inner.buffer.load(Ordering::Acquire, guard) != buffer      // (a) buffer 被换？
    || self.inner.front
        .compare_exchange(f, f.wrapping_add(1), Ordering::SeqCst, Ordering::Relaxed)
        .is_err()                                                   // (b) front CAS 失败？
{
    return Steal::Retry;   // 任一成立 => 伪失败
}
```

- 条件 (a) 重新加载 `buffer`，与第 5 步的快照比较。`resize`（u2-l5）是唯一会 `swap` `buffer` 的地方；快照不一致意味着 owner 在我们眼皮底下换了 `buffer`，本次读出的 `task` 与「当前 `buffer` 上的 `front` 槽位」之间不再有可靠对应关系，于是保守地 `Retry`。
  - **内存安全**上，读 `task` 之所以不会 use-after-free，是因为 `guard`（epoch pin）保证被退休的旧 `buffer` 在本临界区内不会被释放——这个机制留到 u4-l2 详讲。
- 条件 (b) 是标准的乐观抢占：CAS 把 `front` 从 `f` 推到 `f+1`，成功者独占该槽位。CAS 用 `SeqCst` 成功序、`Relaxed` 失败序——成功路径需要全局顺序保证（与 owner 端的 SeqCst 写配对），失败路径只关心「没抢到」，无需额外同步。

对照 owner 的 FIFO `pop`，它用的是 `fetch_add` 而非 CAS（[src/deque.rs:467-473](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L467-L473)）：

```rust
let f = self.inner.front.fetch_add(1, Ordering::SeqCst);
let new_f = f.wrapping_add(1);
if b.wrapping_sub(new_f) < 0 {            // 抢过头了？回退
    self.inner.front.store(f, Ordering::Relaxed);
    return None;
}
```

owner 之所以能用更简单的 `fetch_add`（抢完再判越界回退），是因为它是 `front` 端的**唯一** owner——owner 和 stealer 抢同一端，但 owner 不必担心「抢过头」的代价（最多回退一次）。而 stealer 用 CAS 是为了「抢失败时立刻放弃、不污染 `front`」。

#### 4.4.4 代码实践

**目标**：体会 `Steal::Retry` 在真实并发下的「自旋重试」用法，并理解「失败不等于空」。

```rust
// 示例代码
use std::sync::atomic::{AtomicUsize, Ordering};
use crossbeam_deque::Worker;
use crossbeam_utils::thread::scope;

fn main() {
    let w = Worker::new_lifo();
    let s = w.stealer();

    // 预先放 3 个任务
    for i in 0..3 { w.push(i); }

    // 统计Retry/Empty/Success 各出现几次（单线程下不会Retry，仅演示循环写法）
    let retries = AtomicUsize::new(0);
    let mut got = vec![];
    while got.len() < 3 {
        match s.steal() {
            crossbeam_deque::Steal::Success(v) => got.push(v),
            crossbeam_deque::Steal::Retry => { retries.fetch_add(1, Ordering::Relaxed); }
            crossbeam_deque::Steal::Empty => break,
        }
    }
    got.sort();
    println!("got = {:?}, retries = {}", got, retries.load(Ordering::Relaxed));
    assert_eq!(got, vec![0, 1, 2]);
}
```

**操作步骤**：放入依赖了 `crossbeam-deque = "0.8"` 与 `crossbeam-utils = "0.2"` 的二进制 crate，`cargo run`。
**观察**：单线程下 `retries` 为 0；但循环写法 `match` 里**显式区分 `Retry`（继续）与 `Empty`（退出）**，这正是多线程下避免「误判空而休眠」的正确模式。
**预期**：打印 `got = [0, 1, 2], retries = 0`。

#### 4.4.5 小练习与答案

**练习 1**：两个 stealer 同时偷只有一个任务的队列，最终会偷到几个？另一个得到什么结果？为什么不会 double-free？

> **答案**：恰好偷到一个。赢家的 `front.CAS(f, f+1)` 成功，返回 `Success`；输家的 CAS 失败，走到 `return Retry`，其 `task`（`MaybeUninit<T>`）被丢弃但**不调用 `T::drop`**（`MaybeUninit` 的语义），所以不会 double-free。

**练习 2**：把第 6 步的 `||` 两个条件交换顺序（先 CAS 再比较 buffer），会有什么问题？

> **答案**：语义上仍正确（任一失败都 `Retry`），但会**更频繁地做无谓的 CAS**：在 owner 正在 `resize`（buffer 已变）但 `front` 还没人抢的情况下，原写法先用 `load != buffer` 短路判定 `Retry`，避免了徒劳的 CAS；交换后则会先做一次注定可能成功、却基于陈旧快照的 CAS，提交后才靠 buffer 校验回滚，增加了无谓的原子写竞争。所以现行写法的短路顺序也是一处性能考量。

---

### 4.5 is_empty 与 len 的读取顺序

#### 4.5.1 概念说明

`Stealer::is_empty` 与 `Stealer::len` 看似平淡，但它们和 `steal` 共用同一种内存序范式：**先 `Acquire` 读 `front`，再 `SeqCst` fence，再 `Acquire` 读 `back`**。理解它们能帮你巩固 4.3 的「`SeqCst` fence 夹在两次读取之间」这一固定结构。

注意：这两个方法是**观察值**，不是精确点查询。并发下 `len()` 只是一个「大致」快照——读到 `front` 与读到 `back` 之间，两个游标都可能被改写。

#### 4.5.2 核心流程

```
is_empty / len 的固定三步：
1. f = front.load(Acquire)
2.    fence(SeqCst)        ← 无条件（这里不进 epoch 临界区，不调用 pin）
3. b = back.load(Acquire)
   len = b - f
   is_empty := len <= 0
   len()    := max(len, 0) as usize
```

注意两点：

- **fence 无条件**：与 `steal` 的条件 fence 不同，因为这里没有 `epoch::pin()` 可借（见 4.3 练习 2）。
- **`len <= 0` 才算空**：用 `<= 0` 而非 `== 0`，是为了吸收「`front` 被读到比 `back` 更新的推进值」时算出的负值（`wrapping_sub` 下）——此时按「空」处理是安全的保守判定。

#### 4.5.3 源码精读

`is_empty`（[src/deque.rs:598-603](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L598-L603)）：

```rust
pub fn is_empty(&self) -> bool {
    let f = self.inner.front.load(Ordering::Acquire);
    atomic::fence(Ordering::SeqCst);
    let b = self.inner.back.load(Ordering::Acquire);
    b.wrapping_sub(f) <= 0
}
```

`len`（[src/deque.rs:619-624](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L619-L624)）：

```rust
pub fn len(&self) -> usize {
    let f = self.inner.front.load(Ordering::Acquire);
    atomic::fence(Ordering::SeqCst);
    let b = self.inner.back.load(Ordering::Acquire);
    b.wrapping_sub(f).max(0) as usize
}
```

两者结构完全对称，差别只在最后的归一：`is_empty` 用 `<= 0` 判定、`len` 用 `.max(0)` 把负值截到 0。

#### 4.5.4 代码实践

**目标**：观察 `len()` 在并发下的「大致快照」性质（非精确值）。

```rust
// 示例代码
use std::sync::atomic::{AtomicBool, Ordering};
use std::thread;
use crossbeam_deque::Worker;

fn main() {
    let w = Worker::new_fifo();
    let s = w.stealer();
    let stop = AtomicBool::new(false);

    let h = thread::spawn(move || {
        // 不断 push，制造 back 持续增长
        let mut i = 0;
        while !stop.load(Ordering::Relaxed) { w.push(i); i += 1; }
        i
    });

    // 主线程频繁采样 len()，它可以是任意非负值，且两次采样可能「乱序」增长
    for _ in 0..5 {
        println!("len snapshot = {}", s.len());
    }
    stop.store(true, Ordering::Relaxed);
    let pushed = h.join().unwrap();
    println!("total pushed = {}", pushed);
}
```

**操作步骤**：`cargo run` 多跑几次。
**观察**：`len()` 采到的值会随时间增长，但**不是严格递增**——两次读取之间 `front`/`back` 可能各自变化，得到的只是某个瞬时的大致值。
**预期**：程序正常结束；不同运行 `len` 序列不同。**待本地验证**具体数值。

#### 4.5.5 小练习与答案

**练习 1**：`is_empty` 返回 `false` 之后，立刻调 `steal()` 一定返回 `Success` 吗？

> **答案**：不一定。`is_empty` 返回 `false` 只说明采样的那一刻队列非空；在调用 `steal` 之前，别的线程可能已把任务偷光，于是 `steal` 返回 `Empty`；或者任务还在但被抢，返回 `Retry`。`is_empty` 只是「大致」观察值。

**练习 2**：为什么 `len` 最后要 `.max(0)`？

> **答案**：并发下可能读到「`front` 较新、`back` 较旧」的组合，使 `b - f` 在 `wrapping_sub` 下为负。`.max(0)` 把这种不可能为真负长度的情形截断为 0，对外始终返回非负 `usize`，符合 `len()` 的契约。

---

## 5. 综合实践

**目标**：把本讲三态语义、CAS 竞争、`Retry` 自旋串成一个真实并发测试——实现一个 **spsc（单生产者单消费者）** 测试，参考 [tests/lifo.rs:75-102](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/tests/lifo.rs#L75-L102) 的 `spsc`。

**任务**：在一个 LIFO `Worker` 上，生产者线程连续 `push 0..N`；消费者线程持 `Stealer` 循环 `steal`，遇 `Retry` 自旋重试、遇 `Empty` 也继续等（因为生产者还没 push 完），直到拿到 `0..N` 全部 N 个任务，并断言「收到的第 i 个值正好是 i、且 N 个任务每个恰好被取走一次」。最后再 `steal` 一次断言 `Empty`。

**参考实现**（缩小规模以便本地快速跑，`crossbeam-utils = "0.2"` 提供 `scope`）：

```rust
// 示例代码（整合测试形态，可放入 tests 目录或 main）
use crossbeam_deque::{Steal::Success, Worker};
use crossbeam_utils::thread::scope;

fn main() {
    const STEPS: usize = 10_000; // 真实测试里非 miri 用 50_000，这里缩小便于观察

    let w = Worker::new_lifo();
    let s = w.stealer();

    scope(|scope| {
        // 消费者：用 steal 取走 0..STEPS，遇 Retry/Empty 自旋重试
        scope.spawn(|_| {
            for i in 0..STEPS {
                loop {
                    if let Success(v) = s.steal() {
                        assert_eq!(i, v); // LIFO 但因每次 push 后最终都会被取，断言「值域无丢失」
                        break;
                    }
                    std::hint::spin_loop(); // Retry / Empty 时让出 CPU 片刻
                }
            }
            assert_eq!(s.steal(), crossbeam_deque::Steal::Empty);
        });

        // 生产者：连续 push 0..STEPS
        for i in 0..STEPS {
            w.push(i);
        }
    })
    .unwrap();

    println!("all {} tasks stolen exactly once", STEPS);
}
```

**操作步骤**：

1. 新建一个二进制（或集成测试），在 `Cargo.toml` 加：
   ```toml
   [dependencies]
   crossbeam-deque = "0.8"
   crossbeam-utils = "0.2"
   ```
2. `cargo run`（若放入 `tests/`，则 `cargo test --release -- --nocapture`）。
3. 把 `STEPS` 调到 `100_000` 再跑一次，观察耗时与正确性。

**需要观察的现象**：

- 消费者的 `loop` 里，`s.steal()` 会在高并发交替时频繁返回 `Retry`（被生产者的写节奏或内部 CAS 竞争影响）——这正是本讲强调的「伪失败，自旋重试」。
- 无论中途多少次 `Retry`，最终每个 `i` 都恰好被消费一次，无丢失、无重复、无 double-free。

**预期结果**：

- 程序无 panic，打印 `all N tasks stolen exactly once`。
- `s.steal()` 在循环结束后返回 `Empty`。

> **对照官方测试**：本实践是 [tests/lifo.rs:75-102](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/tests/lifo.rs#L75-L102) 的简化版。官方版本把 `STEPS` 设为 `if cfg!(miri) { 500 } else { 50_000 }`，并在 miri 下用 `std::hint::spin_loop`——这是降低 Miri 运行规模的常见技巧，将在 u4-l4 详讲。

**思考延伸（可选）**：把消费者从「单线程 steal」改成「多个 stealer 线程同时 steal，用 `AtomicUsize` 计数」，验证多偷取者下任务仍被恰好消费一次（即官方 `stampede` 测试的雏形，见 [tests/lifo.rs:104](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/tests/lifo.rs#L104) 起）。

## 6. 本讲小结

- 偷取返回 **三态 `Steal<T>`**：`Empty`（真空）、`Success(T)`、`Retry`（伪失败，需立刻重试）；`Retry` 信号在 `or_else` 与 `FromIterator` 里被「放大」，不会被 `Empty` 吞掉（[src/deque.rs:2085-2233](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L2085-L2233)）。
- `Stealer::steal` 是**乐观两步**：先 `Acquire` 读 `front` → `epoch::pin()` → `Acquire` 读 `back` 判空 → 读 `buffer` 快照并读槽位 → `SeqCst` CAS 推进 `front` 并二次校验 `buffer`（[src/deque.rs:641-683](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L641-L683)）。
- `front` 与 `back` 之间**必须有一道 `SeqCst` fence**；`steal` 利用 `epoch::pin()` 自带的 fence，仅在 `is_pinned()`（可重入）时手动补发（[src/deque.rs:645-654](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L645-L654)）。
- CAS 失败（被别的 stealer、owner FIFO `pop` 的 `fetch_add`、或 LIFO「最后元素」pop 抢先）或 `buffer` 被换（owner `resize`）时返回 `Retry`；赢家用 `assume_init` 取任务，输家丢弃 `MaybeUninit`（不触发 `drop`），保证**恰好消费一次**。
- `Stealer::is_empty` / `len` 用「`Acquire` 读 `front` → 无条件 `SeqCst` fence → `Acquire` 读 `back`」的同一范式，是并发下的**大致快照**而非精确点查询（[src/deque.rs:598-624](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L598-L624)）。

## 7. 下一步学习建议

- **u2-l4（批量偷取）**：本讲的 `steal` 是「偷一个」，下一讲进入 `steal_batch` / `steal_batch_and_pop`，会复用本讲的「读 `front` → `SeqCst` fence → `pin` → 读 `back`」骨架，并新增「偷约一半、上限 `MAX_BATCH=32`」与 FIFO/LIFO 源/目的组合的拷贝顺序逻辑。
- **u2-l5（resize 与生命周期）**：本讲把 `buffer` 被 `resize` 换掉当成「返回 `Retry`」的触发条件之一黑盒处理；下一讲拆开 `resize` 如何分配新 `buffer`、`copy`、`swap` 并延迟回收旧 `buffer`。
- **u4-l2（epoch GC）**：本讲承诺的「`epoch::pin()` 为什么能保证读旧 `buffer` 不 use-after-free」在那里得到完整解释——`pin()` / `defer_unchecked` / `flush` / `unprotected` 的延迟回收模型。
- **建议阅读源码**：带着本讲的结论重读 [src/deque.rs:641-683](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L641-L683) 一遍，并对照 [src/deque.rs:463-545](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L463-L545) 的 owner `pop`，体会「steal 与 pop 抢 `front`」的对称设计。
