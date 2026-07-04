# push 与 pop 的实现：FIFO vs LIFO 分支

## 1. 本讲目标

上一讲（u2-l1）我们搭好了 Chase-Lev 队列的「骨架」：环形 `Buffer<T>`、共享的 `Inner<T>`（`front`/`back`/`buffer`）以及 `CachePadded` 防伪共享。本讲进入「主人线程」的私有操作——`Worker::push` 与 `Worker::pop` 的真实实现。

读完本讲，你应该能够：

1. 看懂 `Worker::push` 的固定四步：**算长度 → 满则扩容 → 写槽位 → Release fence + 推进 back**，并理解为什么 `push` 在 FIFO 和 LIFO 下代码完全相同。
2. 看懂 `Worker::pop` 的 FIFO 分支：用 `fetch_add` 抢占 `front`、检测越界回退、按需缩容。
3. 看懂 `Worker::pop` 的 LIFO 分支：**先把 `back` 减 1、SeqCst fence、再读 `front`**，并理解「只剩一个任务」时为什么需要一次 `compare_exchange` 仲裁。
4. 能够画出 LIFO「最后一个任务」在 owner-pop 与远端 steal 之间竞争的时序图。

本讲**不**展开 `resize` 内部的 epoch GC 细节（留到 u2-l5 与 u4-l2），也**不**展开 `Stealer::steal`（留到 u2-l3）。本讲只聚焦 owner 这一端的 push/pop。

## 2. 前置知识

### 2.1 复习：双游标模型

Chase-Lev 队列用两个只增不减（LIFO 的 `back` 会减，详见后文）的 `AtomicIsize` 游标来表达队列内容：

\[ \text{len} = \text{back} - \text{front} \]

- `front`：队列「队头」，**被偷取者（Stealer）推进**，也被 owner 的 FIFO `pop` 推进。
- `back`：队列「队尾」，**只有 owner 写**（`push` 加 1；LIFO `pop` 减 1）。
- 实际任务存在环形 `Buffer` 的槽位 `[front, back)` 区间内。

### 2.2 关键并发事实：谁能动什么

这是理解本讲所有内存序选择的钥匙：

| 字段 | 谁会写 | 谁会读 |
|------|--------|--------|
| `back` | **只有 owner**（push / LIFO pop） | owner 与所有 stealer |
| `front` | owner（pop）与 stealer（steal） | owner 与所有 stealer |
| `buffer`（`Inner` 里） | owner（`resize` 时 swap） | stealer（偷取时 load） |

正因为 `back` 只有 owner 写，owner 自己读 `back` 时用 `Relaxed` 就够了（看到的一定是自己最新的写）。而 `front` 会被别的线程推进，owner 读 `front` 时必须用 `Acquire`，才能「同步到」别的线程对 `front` 的 Release/SeqCst 写，从而算出正确的 `len`。

### 2.3 push 为什么不分 FIFO/LIFO

这是上一讲（u1-l3）已经给出的结论，本讲给出源码依据：`push` **永远**把任务写到 `back` 槽位、再把 `back` 加 1。FIFO 与 LIFO 的差别**只在 `pop` 从哪一端取**：

- **FIFO**：从 `front` 取（先进先出）。
- **LIFO**：从 `back-1` 取（后进先出）。

所以同一序列 `push 1,2,3`，FIFO 出队是 `1,2,3`，LIFO 出队是 `3,2,1`。

### 2.4 术语速查

- **`fetch_add`**：原子地「读取旧值并加上 delta」，返回旧值。用来抢占一个唯一的槽位号。
- **`compare_exchange`（CAS）**：原子地「若当前值等于期望值，则替换为新值」，成功返回 Ok，失败返回 Err 并带回当前值。用来做「乐观抢占」。
- **`fence`**：内存屏障，给屏障前后的（非原子或 Relaxed）内存操作建立顺序约束，本身不读写变量。
- **`MIN_CAP = 64`**：缓冲区最小容量（[src/deque.rs:17-18](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L17-L18)），缩容不会低于它。

## 3. 本讲源码地图

本讲全部内容集中在一个文件里：

| 文件 | 本讲涉及的部分 | 作用 |
|------|---------------|------|
| [src/deque.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs) | `Worker::push`（L399-L433）、`Worker::pop`（L450-L545） | 本讲主角 |
| 同上 | `Worker::resize`（L289-L322，本讲只当黑盒用） | push 满时翻倍、pop 到 1/4 时缩半 |
| 同上 | `Buffer::write` / `Buffer::read`（L72-L90） | 槽位的 volatile 写/读（u2-l1 已讲） |
| 同上 | `Flavor` 枚举（L147-L155） | 区分 Fifo / Lifo |
| tests/lifo.rs、tests/fifo.rs | smoke 测试 | 验证出队顺序 |

## 4. 核心概念与源码讲解

### 4.1 push：写槽位 + fence + 推进 back（FIFO/LIFO 共用）

#### 4.1.1 概念说明

`push` 是「生产」操作，由 owner 线程独占调用。它的职责很朴素：在 `back` 指向的槽位写下任务，再把 `back` 往前推一格。难点不在「写」，而在两件事：

1. **容量不够时要先扩容**，否则会覆盖尚未被消费的旧任务。
2. **发布顺序**：必须保证「先写好槽位内容，再让别的线程看到 `back` 前进」。否则一个 stealer 看到 `back` 增大了，去读那个槽位时却读到垃圾值。

因为 `push` 总是操作 `back` 这一端，与 flavor 无关，所以 FIFO 和 LIFO 共用同一段 `push` 代码。

#### 4.1.2 核心流程

```text
push(task):
    b = back        # Relaxed 读（owner 专用字段）
    f = front       # Acquire 读（别的线程会推进 front，要同步）
    len = b - f
    buffer = 本地 Cell 缓存的 Buffer

    if len >= buffer.cap:        # 满了
        resize(2 * buffer.cap)   # 翻倍扩容
        buffer = 重新读本地缓存

    buffer.write(b, task)        # 写槽位（volatile 写）

    fence(Release)               # 关键屏障：保证上面的写对下文可见
    back.store(b + 1)            # 发布：back 前进一格
```

扩容判据是 \(\text{len} \ge \text{cap}\)，即「现有任务数已经填满整个环形缓冲区」。注意因为 `back`、`front` 都是带符号整数且会持续增长（不会取模回绕到真实下标），`len` 的计算用的是 `wrapping_sub`，真实槽位由 `Buffer::at` 的 `index & (cap-1)` 负责取模（见 u2-l1）。

发布顺序的内存序配对是这样的：

- **写侧（owner，push）**：`buffer.write(...)`（volatile 非原子写）→ `fence(Release)` → `back.store(..., Relaxed)`。
- **读侧（stealer，steal）**：`back.load(Acquire)` → 若观察到新 `back`，则 `buffer.read(...)` 能读到任务。

`fence(Release)` 把「写槽位」和「Relaxed store back」粘在一起，使得任何通过 `Acquire load back` 观察到新 `back` 的线程，都必然观察到槽位里已写好的任务。这正是 Chase-Lev 算法的核心同步点。

#### 4.1.3 源码精读

先看完整函数：[src/deque.rs:399-433](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L399-L433)。下面逐段拆解。

**第 1 步：读取游标与本地 buffer 缓存** —— [src/deque.rs:400-406](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L400-L406)

```rust
let b = self.inner.back.load(Ordering::Relaxed);   // owner 专用，Relaxed 足够
let f = self.inner.front.load(Ordering::Acquire);  // 要同步别的线程对 front 的推进
let mut buffer = self.buffer.get();                 // Cell 里的本地快照
let len = b.wrapping_sub(f);
```

`self.buffer` 是 `Cell<Buffer<T>>`（[src/deque.rs:202](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L202)）。owner 维护一份 buffer 指针的「本地副本」做快速访问，避免每次 push 都去 `Inner.buffer` 上做一次原子 load（那是 stealer 才必须走的慢路径）。

**第 2 步：满了就翻倍扩容** —— [src/deque.rs:408-415](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L408-L415)

```rust
if len >= buffer.cap as isize {
    unsafe { self.resize(2 * buffer.cap); }
    buffer = self.buffer.get();   // resize 换了 buffer，必须重新读
}
```

注意 `resize` 之后**重新读取** `self.buffer.get()`——因为 `resize` 内部会 `self.buffer.replace(new)`（[src/deque.rs:308](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L308)），本地副本已经失效。忘记刷新的话会写进已废弃的旧 buffer。

**第 3 步：写槽位** —— [src/deque.rs:417-420](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L417-L420)

```rust
unsafe {
    buffer.write(b, MaybeUninit::new(task));
}
```

`Buffer::write` 内部是 `ptr::write_volatile`（[src/deque.rs:78-80](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L78-L80)）。这里**不**用原子 store，是一种「为了对任意类型 `T` 都能工作而做的已知折中」（详见 u2-l1 与 u4-l1）。

**第 4 步：Release fence + 推进 back（含 tsan 双路径）** —— [src/deque.rs:422-432](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L422-L432)

```rust
#[cfg(not(crossbeam_sanitize_thread))]
atomic::fence(Ordering::Release);
let store_order = if cfg!(crossbeam_sanitize_thread) {
    Ordering::Release
} else {
    Ordering::Relaxed
};
self.inner.back.store(b.wrapping_add(1), store_order);
```

正常编译时：先 `fence(Release)`，再用 `Relaxed` store `back`。在 ThreadSanitizer 模式下（`crossbeam_sanitize_thread` cfg 点亮，见 u1-l2 的 `build.rs`），因为 tsan **不理解 fence**，代码改为省略 fence、直接用 `Release` 语义 store `back`，让 tsan 仍能正确建立同步关系。这是「生产性能 vs 检测工具友好」的工程折中，完整讨论留到 u4-l3。

> **小结**：`push` 的内存序骨架是「`Acquire` 读 front → 写槽位 → `Release` fence → `Relaxed` 写 back」，与 stealer 的「`Acquire` 读 back → 读槽位」严格配对。

#### 4.1.4 代码实践

**实践目标**：亲眼看到 `push` 在超过 `MIN_CAP=64` 时触发翻倍扩容，且数据不丢失。

**操作步骤**：

1. 在 `crossbeam-deque` 目录下新建一个临时 example（或直接写一个 `#[test]`），创建一个 FIFO `Worker`。
2. 连续 `push` 0..100（超过 64，必然扩容）。
3. 连续 `pop` 直到返回 `None`，把每次的值收集起来。
4. 断言收集到的序列正好是 `0..100`。

参考示例代码（非项目原有，需自建文件 `examples/push_resize.rs`）：

```rust
// 示例代码：验证 push 触发扩容后数据完整
use crossbeam_deque::Worker;

fn main() {
    let w = Worker::new_fifo();
    for i in 0..100usize {
        w.push(i); // 第 65 次 push 时 len>=64，触发 resize(128)
    }
    let mut got = Vec::new();
    while let Some(v) = w.pop() {
        got.push(v);
    }
    assert_eq!(got, (0..100).collect::<Vec<_>>());
    println!("OK: 扩容后 {} 个任务全部正确取回", got.len());
}
```

**需要观察的现象**：程序正常退出并打印 `OK`，说明从 64→128 的扩容过程中，已经写入的 0..64 被正确搬到了新 buffer（这正是 `resize` 里 `copy_nonoverlapping` 做的事，见 [src/deque.rs:299-303](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L299-L303)）。

**预期结果**：`OK: 扩容后 100 个任务全部正确取回`。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `push` 里读 `back` 用 `Relaxed`，读 `front` 却用 `Acquire`？

**答案**：`back` 只有 owner 自己写，owner 读自己的写不需要跨线程同步，`Relaxed` 足够；`front` 会被 stealer（或 owner 的 FIFO/LIFO pop）推进，owner 必须用 `Acquire` 才能同步到这些 Release/SeqCst 写，从而算出正确的 `len` 并据此决定是否扩容。

**练习 2**：如果把第 4 步的 `fence(Release)` 删掉（普通编译模式下），最坏会发生什么？

**答案**：在弱内存模型 CPU 上，处理器或编译器可能把「写槽位」重排到「store back」之后。于是 stealer 先通过 `Acquire load back` 观察到 `back` 前进，再去读槽位时却读到未初始化的旧值——产生读到垃圾/未定义行为。`fence(Release)` 就是用来禁止这种重排的。

**练习 3**：`resize` 之后为什么必须 `buffer = self.buffer.get()` 再赋值一次？

**答案**：`resize` 内部用 `self.buffer.replace(new)` 把本地 buffer 指针换成了新 buffer（[src/deque.rs:308](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L308)）。若不刷新本地变量，后续 `buffer.write(b, ...)` 会写进已经被 swap 出去的旧 buffer，造成任务丢失。

---

### 4.2 pop 的 FIFO 分支：fetch_add 抢占与回退缩容

#### 4.2.1 概念说明

`pop` 是 owner 的「消费」操作。它先做一个共用的「快速判空」，然后按 flavor 分叉。FIFO 分支从 `front` 端取任务（先进先出）。

FIFO 分支的核心技巧是用 **`fetch_add`** 来抢占槽位：`front.fetch_add(1)` 原子地取出当前 `front` 作为「我认领的槽位号」，同时把 `front` 加 1。这保证了即使有 stealer 也在抢 `front`，每个人拿到的槽位号互不相同——没有两个线程会认领同一个槽位。

但 `fetch_add` 是「先抢占、后判断」的乐观策略：万一抢到的槽位其实已经超过 `back`（队列被别人掏空了），就要**回退**并把这次 `pop` 当作空。

#### 4.2.2 核心流程

`pop` 的共用前置部分（[src/deque.rs:451-461](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L451-L461)）：

```text
b = back (Relaxed), f = front (Relaxed)
len = b - f
if len <= 0: return None     # 看起来是空的（可能陈旧，下面再校验）
```

FIFO 分支（[src/deque.rs:465-487](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L465-L487)）：

```text
Flavor::Fifo:
    f = front.fetch_add(1, SeqCst)   # 抢占槽位号 f，front → f+1
    new_f = f + 1
    if b - new_f < 0:                # 越界：认领的槽位 >= back，队列其实空了
        front.store(f, Relaxed)      # 回退：把 front 拨回 f
        return None
    task = buffer.read(f).assume_init()
    if cap > MIN_CAP and len <= cap/4:   # 用得很少，缩容一半
        resize(cap / 2)
    return Some(task)
```

两个要点：

1. **越界回退判据** `b - new_f < 0`，即 `new_f > b`，等价于「我认领的槽位 `f` 落在了 `[front, back)` 之外（≥ back）」。这只可能发生在「判空检查之后、`fetch_add` 之前，有 stealer 把最后一个任务偷走了」的竞争窗口里。
2. **缩容判据**用的是**这次 pop 之前**的 `len`（在函数顶部算好的那个），条件是 `cap > MIN_CAP && len <= cap/4`。注意它和 LIFO 分支的判据略有不同（见 4.3）。

> **为什么 FIFO pop 不需要 epoch::pin 或 buffer 二次校验？**
> 因为 `pop` 与 `resize` 都只由 owner 单线程调用，一次 `pop` 执行期间不可能发生 `resize`（owner 不会和自己并发）。所以 owner 读 `self.buffer.get()`（本地 Cell 副本）总是当前有效的，buffer 指针在单次 pop 内稳定不变。这与 stealer 必须用 epoch::pin + 重新 load buffer 校验（u2-l3）形成鲜明对比。

#### 4.2.3 源码精读

**FIFO 分支主体** —— [src/deque.rs:465-487](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L465-L487)

```rust
Flavor::Fifo => {
    // Try incrementing the front index to pop the task.
    let f = self.inner.front.fetch_add(1, Ordering::SeqCst);
    let new_f = f.wrapping_add(1);

    if b.wrapping_sub(new_f) < 0 {
        self.inner.front.store(f, Ordering::Relaxed);  // 回退
        return None;
    }

    unsafe {
        let buffer = self.buffer.get();
        let task = buffer.read(f).assume_init();       // 直接 assume_init：槽位一定有任务

        // Shrink the buffer if `len - 1` is less than one fourth of the capacity.
        if buffer.cap > MIN_CAP && len <= buffer.cap as isize / 4 {
            self.resize(buffer.cap / 2);
        }
        Some(task)
    }
}
```

逐行注释：

- `fetch_add(1, SeqCst)`：原子拿到旧 `front` 记为 `f`，并把 `front` 置为 `f+1`。用 `SeqCst` 是为了和 push/steal 的全局顺序协调。由于这是整个文件里**唯一**对 `front` 做 `fetch_add` 的地方（steal/batch steal/LIFO pop 都用 CAS），且 `pop` 是 owner 专属，所以这里的抢占逻辑在 owner 单线程视角下是确定的。
- 越界检测 `b.wrapping_sub(new_f) < 0`：`b` 是函数顶部读的快照。若 `new_f > b`，说明认领的槽位不在有效区间内，回退 `front` 到 `f` 并返回 `None`。回退是安全的：stealer 只会把 `front` 推进到至多 `back`（偷空即停），不会越过 `back`，所以把 `front` 拨回 `f`（此时 `f` 恰为 `back` 附近）不会与 stealer 的合法推进冲突。
- `buffer.read(f).assume_init()`：因为 `fetch_add` 已唯一认领了 `f`，且 `b - new_f >= 0` 保证了 `f < b`（槽位里确实有任务），所以可以**直接** `assume_init`，不需要像 LIFO 分支那样包成 `Option<MaybeUninit>` 再判断。
- 缩容：`len <= cap/4` 时 `resize(cap/2)`，且不低于 `MIN_CAP`（`resize` 内部以 `cap/2` 调用，实际下界由后续 push/pop 与 `reserve` 保证不破 `MIN_CAP`，见 u2-l5）。

#### 4.2.4 代码实践

**实践目标**：用 FIFO worker 验证「先进先出」顺序，并构造一个 owner 与 stealer 竞争导致 FIFO pop 越界回退、返回 `None` 的场景。

**操作步骤**：

1. 创建 FIFO `Worker` 及其 `Stealer`。
2. `push 1, 2, 3`。
3. 连续 `pop` 三次，打印结果，验证得到 `Some(1), Some(2), Some(3)`。
4. 再 `pop` 一次，验证得到 `None`。
5. （越界回退观察）`push 10`；在 owner `pop` 之前，先用 `Stealer` 把它偷走（`s.steal() == Success(10)`），再让 owner `pop()`，观察返回 `None`——这正是「判空时 len=1，但 fetch_add 前任务被偷走」触发回退的路径。

参考示例代码：

```rust
// 示例代码：FIFO 顺序 + 越界回退
use crossbeam_deque::{Steal, Worker};

fn main() {
    let w = Worker::new_fifo();
    let s = w.stealer();
    w.push(1); w.push(2); w.push(3);
    assert_eq!(w.pop(), Some(1));
    assert_eq!(w.pop(), Some(2));
    assert_eq!(w.pop(), Some(3));
    assert_eq!(w.pop(), None);

    // 越界回退场景
    w.push(10);
    assert_eq!(s.steal(), Steal::Success(10)); // 任务被偷走
    assert_eq!(w.pop(), None);                 // owner 的 pop 越界，回退后返回 None
    println!("OK");
}
```

**需要观察的现象**：第 5 步的 `w.pop()` 返回 `None` 而非 panic，也不会读到 `10`（因为 `10` 已被 stealer 取走，owner 的 `fetch_add` 越界后被回退）。

**预期结果**：打印 `OK`。

> 注：步骤 5 是单线程内手动模拟竞争窗口，确定性可观察。真实多线程下越界回退是「偶发」的，但路径完全相同。

#### 4.2.5 小练习与答案

**练习 1**：FIFO 分支里 `fetch_add` 之后，为什么可以直接 `buffer.read(f).assume_init()`，而不像 LIFO 分支那样要包成 `Option` 再决定是否 `take()`？

**答案**：因为 `fetch_add` 已让 owner 唯一认领了槽位 `f`，而随后的越界检测 `b - new_f < 0` 已经排除了「`f` 落在有效区间之外」的情况——只要走到 `read` 这一行，就保证 `f < b`，槽位里一定有任务，所以可以直接 `assume_init`。

**练习 2**：越界回退时为什么用 `front.store(f, Relaxed)` 而不是 CAS？

**答案**：回退发生在 owner 自己的 pop 内，目的是撤销「我自己刚做的 `fetch_add`」，把 `front` 拨回抢之前的值 `f`。stealer 不会把 `front` 推过 `back`，所以这里写回 `f` 不会破坏 stealer 的合法推进；用简单的 `store` 比 CAS 更廉价，足以恢复一致性。

**练习 3**：FIFO 缩容条件是 `len <= cap/4`，这里的 `len` 是「pop 之前」还是「pop 之后」的长度？

**答案**：是 **pop 之前**的 `len`（函数顶部 `let len = b.wrapping_sub(f)` 算好的那个，[src/deque.rs:456](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L456)）。注释里写的 `len - 1`（[src/deque.rs:480](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L480)）正是「pop 之后」的语义——即 pop 后队列长度若不足容量的 1/4 就缩容。

---

### 4.3 pop 的 LIFO 分支：先减 back、fence、最后元素 CAS

#### 4.3.1 概念说明

LIFO 分支从 `back` 端取任务（后进先出）。它的实现比 FIFO 微妙得多，原因是 **`back` 现在要被 owner「减」**，而 `back` 同时是 stealer 判断「队列还有没有任务」的依据。owner 不能直接 `back -= 1` 就算了，必须处理「当队列只剩一个任务时，owner 从 back 端取、stealer 从 front 端取，二者瞄准的是同一个槽位」的竞争。

为此 LIFO 分支设计了「乐观减 back + SeqCst fence + 读 front 复核 + 必要时 CAS 仲裁」的四段式流程。其中的 CAS 仅在「只剩最后一个任务」时触发，用来在 owner 和 stealer 之间决出唯一赢家，避免同一个任务被双方各取一次（double-free）。

#### 4.3.2 核心流程

LIFO 分支（[src/deque.rs:490-543](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L490-L543)）：

```text
Flavor::Lifo:
    b = b - 1                       # 乐观地把 back 减 1（声明「最末槽位归我」）
    store back = b (Relaxed)
    fence(SeqCst)                   # 关键屏障：让下面的 front 读到一致的视图
    f = front (Relaxed)
    len = b - f                     # 减 1 之后的长度

    if len < 0:                     # 减过头了：队列本来就空（多个 owner pop? 不会；这里 len<0
        store back = b + 1          # 其实对应「pop 前队列就空」的边界）→ 恢复 back，返回 None
        return None

    task = read(slot b)             # 先把最末槽位读出来（包成 Option<MaybeUninit>）

    if len == 0:                    # ★ 只剩最后一个任务，可能与 stealer 竞争 ★
        if CAS(front: f → f+1) 失败:   # stealer 已经抢先推进了 front
            task.take()             #   我没抢到，丢弃读出的任务（不 assume_init）
        store back = b + 1          # 恢复 back（无论成败都要恢复）
        return task（成功则 Some，失败则 None）
    else:                           # 还有 >=1 个任务，无竞争
        if cap > MIN_CAP and len < cap/4:
            resize(cap / 2)
        return Some(task.assume_init())
```

几个关键设计：

1. **为什么先「乐观减 back」再 fence 再读 front？**
   owner 先把 `back` 减 1，等于「占住」最末槽位；接着 `fence(SeqCst)` 确保后续读到的 `front` 是在此写之后的一致快照。这样，若读到的 `front` 已经被 stealer 推进（说明 stealer 也在抢这个槽位），`len` 的计算就能反映竞争结果。`SeqCst` fence 是为了与 stealer 侧的 SeqCst 操作建立全序，避免「owner 减了 back 但 stealer 没看到 / stealer 推进了 front 但 owner 没看到」的危险交错。

2. **`len < 0` 分支**：`back` 减 1 后若 `len = b - f < 0`，说明队列在 pop 之前其实就已经空了（`back == front`），这次 pop 应当返回 `None`，于是把 `back` 恢复到 `b+1`。

3. **`len == 0` 分支（最后元素 CAS）**：这是本讲的重点，单独在 4.3.4 用时序图讲透。

4. **缩容判据**：LIFO 分支用的是**减 1 之后**的 `len`，且条件是 `len < cap/4`（严格小于），与 FIFO 的 `len <= cap/4`（针对 pop 前 len）略有差别——这是源码里两个分支的一处细微不对称。

#### 4.3.3 源码精读

**LIFO 分支主体** —— [src/deque.rs:490-543](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L490-L543)

```rust
Flavor::Lifo => {
    // Decrement the back index.
    let b = b.wrapping_sub(1);
    self.inner.back.store(b, Ordering::Relaxed);

    atomic::fence(Ordering::SeqCst);

    // Load the front index.
    let f = self.inner.front.load(Ordering::Relaxed);

    // Compute the length after the back index was decremented.
    let len = b.wrapping_sub(f);

    if len < 0 {
        // The queue is empty. Restore the back index to the original task.
        self.inner.back.store(b.wrapping_add(1), Ordering::Relaxed);
        None
    } else {
        let buffer = self.buffer.get();
        let mut task = unsafe { Some(buffer.read(b)) };

        // Are we popping the last task from the queue?
        if len == 0 {
            // Try incrementing the front index.
            if self.inner.front.compare_exchange(
                f, f.wrapping_add(1), Ordering::SeqCst, Ordering::Relaxed,
            ).is_err() {
                // Failed. We didn't pop anything. Reset to `None`.
                task.take();
            }
            // Restore the back index to the original task.
            self.inner.back.store(b.wrapping_add(1), Ordering::Relaxed);
        } else {
            // Shrink the buffer if `len` is less than one fourth of the capacity.
            if buffer.cap > MIN_CAP && len < buffer.cap as isize / 4 {
                unsafe { self.resize(buffer.cap / 2); }
            }
        }

        task.map(|t| unsafe { t.assume_init() })
    }
}
```

逐段说明：

- **乐观减 back**（L492-L493）：`b` 减 1 后用 `Relaxed` store。因为 `back` 只有 owner 写，这里不需要额外同步；同步交给紧接着的 `fence(SeqCst)`。
- **SeqCst fence**（L495）：建立与 stealer SeqCst 操作的全序，保证随后读到的 `front` 反映「减 back 之后」的世界状态。注意它没有 `#[cfg]` 包裹——LIFO pop 不像 push/steal 那样提供 tsan 双路径（因为这里的 `back`/`front` 写都是 Relaxed，fence 之外没有可替代的 Acquire/Release store 落点；该 fence 在 tsan 下也保留）。
- **读 front 复核 len**（L498-L501）：用减 1 之后的 `b` 与新读到的 `f` 算 `len`。
- **`len == 0` 仲裁**（L513-L531）：读出最末槽位任务为 `Option<MaybeUninit<T>>`；尝试 CAS 把 `front` 从 `f` 推进到 `f+1`。CAS 成功=owner 赢，`task` 保持 `Some`；CAS 失败=stealer 赢（已把 `front` 推走），`task.take()` 丢弃。**无论成败都把 `back` 恢复到 `b+1`**（因为这次 pop「消费」了最末槽位，而最末槽位同时也是 `front` 端，需要让 `back` 与被推进的 `front` 重新对齐到「空」状态）。
- **返回**（L541）：`task.map(|t| t.assume_init())`。只有 CAS 成功时 `task` 才是 `Some`，才会调用一次 `assume_init`——这保证了被竞争的任务**恰好被消费一次**，杜绝 double-free。

> **为什么最后元素要走 CAS，而普通元素不用？**
> 当 `len > 0`（减 1 后仍 ≥1，即 pop 前至少 2 个任务）时，最末槽位 `[back-1]` 与 stealer 瞄准的队头槽位 `[front]` 不是同一个，owner 取尾、stealer 取头，井水不犯河水，无需仲裁。只有 `len == 0`（pop 前**恰好** 1 个任务）时，`back-1 == front`，双方瞄准同一个槽位，必须用 CAS 决出唯一赢家。

#### 4.3.4 代码实践（本讲指定实践：最后元素 CAS 时序图）

**实践目标**：画时序图说明「队列只剩一个任务时，owner 的 LIFO pop 与远端 steal 如何通过 `compare_exchange` 仲裁，保证任务恰好被消费一次」。

> **重要前提（忠实于源码）**：`Worker` 是 `!Sync`（[src/deque.rs:208](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L208) 的 `PhantomData<*mut ()>` 加上只 `unsafe impl Send`），所以**两个线程不能同时对同一个 `Worker` 调用 `pop`**。「最后一个任务」的真实竞争，发生在 **owner 的 LIFO `pop`**（从 `back` 端）与 **另一线程通过 `Stealer::steal`**（从 `front` 端）之间。下面的时序图按这个真实场景画。

**初始状态**：队列恰有 1 个任务，`front = 10`，`back = 11`，任务在槽位 `[10]`。

```text
owner 线程（LIFO pop）                 stealer 线程（s.steal()）
────────────────────────────           ────────────────────────────
                                       f = front.load(Acquire) = 10
                                       guard = epoch::pin()
                                       b = back.load(Acquire) = 11   （读到减 1 之前的旧值）
                                       len = 11 - 10 = 1 > 0
                                       task = read(slot 10)
b_back = 11, f_front = 10, len = 1
b = 11 - 1 = 10
store back = 10 (Relaxed)
fence(SeqCst)
f = front.load = 10
len = 10 - 10 = 0  → 最后一个任务!
task = read(slot 10)
                                       ┌─ 两个线程都瞄准 front: 10→11 ─┐
                                       │                               │
   CAS(front 10→11)  ─────► 情况 A: stealer 先赢                       │
                            stealer CAS 成功 → Steal::Success(slot10) │
                            owner CAS 失败(front 已是 11)              │
                              → task.take() = None                    │
                            store back = 11 (恢复)                    │
                            return None                               │
                                                                          │
   CAS(front 10→11)  ─────► 情况 B: owner 先赢 ◄──────────────────────┘
                            owner CAS 成功 → task 保持 Some
                            store back = 11 (恢复)
                            return Some(slot10 的值)
                            stealer CAS 失败 → Steal::Retry
```

**两种情况的终态完全一致**：`front = 11`，`back = 11`，队列为空；区别只在于任务被谁拿走。

**关键安全保证**：

- 槽位 `[10]` 被 owner 和 stealer **各读了一次**（两次 `read`），但只有 CAS 赢家会调用 `assume_init`（owner 通过 `task.map(... assume_init)`，且只有未 `take()` 才进入；stealer 只有 CAS 成功才 `task.assume_init()`）。所以任务**恰好被消费一次**，不会 double-free。
- CAS 输家丢弃自己读出的副本（owner 用 `task.take()` 丢弃 `Option<MaybeUninit>` 但**不**调用 `assume_init`/`Drop`；stealer 直接 `return Steal::Retry` 忘掉读出的 `MaybeUninit`）。由于 `read` 是 `read_volatile` 的按字节拷贝，丢弃副本不会触发 `T` 的析构，安全。

**需要观察的现象（可选的本地实验）**：在 `tests/lifo.rs` 的 `smoke` 测试里能看到这种「同一槽位两端取」的简化版：`push 6,7,8,9` 后 `w.pop() == Some(9)`（owner 从 back 取走 9），紧接着 `s.steal() == Success(6)`（stealer 从 front 取走 6），再 `w.pop() == Some(8)`、`Some(7)`（[tests/lifo.rs:37-45](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/tests/lifo.rs#L37-L45)）。当队列被取到只剩 1 个时，owner pop 与 stealer steal 就进入了上面时序图的仲裁分支。

**预期结果**：理解时序图后，能清楚回答「CAS 赢家拿任务、输家 `take()` 后返回 `None`/`Retry`，且无 double-free」。真实多线程下两个分支都会随机出现，行为均正确——若想本地验证，可参考 `tests/lifo.rs` 的 `stampede`/`stress` 风格写一个多线程压测（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：LIFO 分支里，为什么读完 `front` 之后要把 `task` 包成 `Option<MaybeUninit<T>>`，而不是像 FIFO 那样直接 `assume_init`？

**答案**：因为在 `len == 0`（最后元素）分支里，owner 可能 CAS 失败而拿不到这个任务。若提前 `assume_init`，就等于「认定槽位归我」，但 CAS 失败说明槽位其实被 stealer 拿走了——此时再 `assume_init` 就会和 stealer 各消费一次，造成 double-free。包成 `Option` 后，只有 CAS 成功才 `map(assume_init)`，CAS 失败则 `take()` 丢弃，保证了「恰好消费一次」。

**练习 2**：最后元素分支里，无论 CAS 成功还是失败，为什么都要 `store back = b + 1`（恢复 back）？

**答案**：owner 一开始「乐观」地把 `back` 减了 1（`store back = b`）。但这次 pop 消费的最末槽位同时也是 `front` 端的那个槽位，而 `front` 已经被（owner 自己或 stealer 的）推进加过 1 了。为了让 `back` 与新的 `front` 重新对齐到「空」状态（`back == front`），必须把 `back` 恢复到 `b + 1`。不恢复的话 `back` 会比 `front` 小 1，`len` 变成 -1，队列状态就错了。

**练习 3**：FIFO 缩容条件是 `len <= cap/4`（len 为 pop 前），LIFO 缩容条件是 `len < cap/4`（len 为减 1 后）。这两处不对称是有意为之吗？

**答案**：这是源码中确实存在的细微差别（FIFO：[src/deque.rs:481](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L481) 用 `<=` 且 `len` 是顶部快照；LIFO：[src/deque.rs:534](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L534) 用 `<` 且 `len` 是减 1 后的值）。两者语义上都近似「pop 之后队列长度不足容量 1/4 就缩一半」，只是各自用本分支已有的 `len` 变量表达，边界恰好差一个元素的精度。对正确性无影响（缩容只是性能优化，且都有 `cap > MIN_CAP` 兜底）。

## 5. 综合实践

把本讲三个模块串起来，完成一个小练习：**用一段单线程代码同时演示 push 的扩容、FIFO 与 LIFO 的出队差异，以及 owner-pop 与 stealer-steal 对最末任务的竞争**。

**任务**：

1. 创建一个 LIFO `Worker` `w` 及其 `Stealer` `s`。
2. 连续 `push 0..70`（超过 `MIN_CAP=64`，触发 64→128 扩容）。
3. 此时 `w.len()` 应为 70。先 `w.pop()` 一次，预期得到 `Some(69)`（LIFO：最新者）。
4. 用 `s.steal()` 偷一次，预期得到 `Steal::Success(0)`（steal 从 front 端：最旧者）。
5. 把队列 `pop` 到只剩 1 个任务（不断 `w.pop()`，记录取出的值），观察取出的顺序应为 `69, 68, 67, ...`（严格 LIFO 递减，除已被偷走的 0 之外）。
6. 当 `w.len() == 1` 时，先调用 `s.steal()` 把最后一个任务偷走（应得 `Success`），再调用 `w.pop()`，预期得到 `None`（这正是 4.3.4 时序图「情况 A：stealer 先赢」的单线程模拟）。
7. 重新 `push 100`，这次让 owner 先 `w.pop()` 再让 stealer `s.steal()`，验证 owner 拿到 `Some(100)`、stealer 拿到 `Empty`。

**验收标准**：

- 步骤 3-4 证明 LIFO owner 从 back 取、stealer 从 front 取，两端不冲突。
- 步骤 2 的扩容没有丢失任何任务（最终能完整取回除被偷者外的全部 69 个）。
- 步骤 6 证明最末任务的 CAS 仲裁：被 stealer 先取走后，owner 的 `pop()` 干净地返回 `None`，既不 panic 也不重复给出任务。

参考骨架（示例代码，自行补全断言）：

```rust
use crossbeam_deque::{Steal, Worker};

fn main() {
    let w = Worker::new_lifo();
    let s = w.stealer();
    for i in 0..70usize { w.push(i); }            // 触发扩容
    assert_eq!(w.len(), 70);
    assert_eq!(w.pop(), Some(69));                 // LIFO 取最新
    assert_eq!(s.steal(), Steal::Success(0));      // steal 取最旧

    while w.len() > 1 { w.pop(); }                 // 剩 1 个
    assert_eq!(w.len(), 1);
    assert_eq!(s.steal(), Steal::Success(_));      // stealer 抢走最后任务（情况 A）
    assert_eq!(w.pop(), None);                     // owner pop 越界/被抢 → None

    w.push(100);
    assert_eq!(w.pop(), Some(100));                // owner 先取（情况 B 的 owner 侧）
    assert_eq!(s.steal(), Steal::Empty);
    println!("综合实践通过");
}
```

> 提示：步骤 6 里 `Steal::Success(_)` 的占位需要用实际值匹配；可先 `let got = s.steal();` 再断言 `matches!(got, Steal::Success(_))`。

## 6. 本讲小结

- **`push` 与 flavor 无关**：永远「读 back/front 算 len → 满则 `resize(2*cap)` → `buffer.write(b, task)` → `fence(Release)` + `back.store(b+1)`」。内存序骨架是「Acquire 读 front ↔ 写槽位 ↔ Release fence ↔ Relaxed 写 back」，与 stealer 的 Acquire 读 back 配对。
- **tsan 双路径**：push（以及 steal_batch）在 `crossbeam_sanitize_thread` 下省略 fence、改用 Release store，以适配 ThreadSanitizer「不理解 fence」的限制。
- **FIFO pop 用 `fetch_add` 抢占**：原子认领槽位 `f`，再用 `b - new_f < 0` 检测越界并回退 `front`、返回 `None`；正常路径直接 `assume_init` 并按 `len <= cap/4` 缩容。
- **LIFO pop 是「乐观减 back + SeqCst fence + 读 front 复核」**：`back` 减 1 后若 `len < 0` 表示本来空，恢复并返回 `None`。
- **最后元素 CAS 仲裁**：当 `len == 0` 时 owner 的最末槽位与 stealer 的队头槽位重合，用 `compare_exchange(front)` 决出唯一赢家，赢家 `assume_init` 消费、输家 `take()`/`Retry` 丢弃，保证恰好消费一次、无 double-free。
- **owner 操作的特权**：`pop`/`resize`/`push` 都是 owner 专属，单次 pop 内 buffer 稳定，所以 owner 不需要 epoch::pin 或 buffer 二次校验——这与 stealer 路径（下一讲）形成对比。

## 7. 下一步学习建议

- **u2-l3 单任务偷取：Stealer::steal 与 CAS 竞争**：从 owner 的对端看 `front` 端，你会看到 stealer 如何用 `epoch::pin` + 「读任务 → CAS front → 再校验 buffer 未被换」的两步偷取，与本讲 LIFO 最后元素 CAS 遥相呼应。
- **u2-l5 缓冲区扩缩容与生命周期：resize 与 reserve**：本讲把 `resize` 当黑盒，下一阶段会拆开它如何用 epoch 延迟回收旧 buffer，以及 `reserve` 的倍增算法。
- **u4-l1 内存序与 volatile 读写 hack 深入**：本讲提到的 `fence(Release)`/`fence(SeqCst)` 配对、`write_volatile` 折中，会在专家层用 Chase-Lev 与弱内存模型论文系统性地论证。
- 继续阅读建议：对照 `tests/fifo.rs` 与 `tests/lifo.rs` 的 `smoke` 测试逐行验证本讲的出队顺序结论；用 `cargo test --package crossbeam-deque` 跑一遍，观察 stampede/stress 测试如何压测本讲的竞争分支。
