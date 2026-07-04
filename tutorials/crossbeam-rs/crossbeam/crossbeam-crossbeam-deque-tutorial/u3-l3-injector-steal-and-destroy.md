# Injector::steal 与 Block 销毁机制

## 1. 本讲目标

本讲承接 [u3-l1（Injector 的 Block/Slot 数据结构）](./u3-l1-injector-block-and-slot.md) 与 [u3-l2（Injector::push）](./u3-l2-injector-push.md)。在上一讲里，我们已经看清楚生产者 `push` 是如何用 `compare_exchange_weak` 推进 `tail`、写槽位、并在块尾安装下一个 block 的。本讲把视角切到**消费者一侧**，回答三个问题：

1. `Injector::steal` 是如何从 `head` 端取出一个任务、并安全地跨过 block 边界的？
2. 读完一个槽位后，`slot.state` 上的 `READ` 位与 `DESTROY` 位是如何协同工作的？
3. 一个可能同时被多个线程读取的 `Block`，如何做到**恰好被释放一次**（既不 use-after-free，也不 double-free）？

学完后你应当能够：

- 画出 `Injector::steal` 从「读 head」到「返回 `Steal::Success`」的完整控制流，并解释 `HAS_NEXT` 位、空队列判定、`SeqCst` fence 的作用。
- 说清楚到块尾时 `wait_next` 如何切换 `head.block`，以及为什么 `next_index` 要「剥掉 `HAS_NEXT` 再加、再按需补回」。
- 用「DESTROY 是一个接力令牌」的心智模型，逐步推演 `Block::destroy` 的协作销毁过程，论证唯一性。

## 2. 前置知识

在进入源码前，先用三段话把上一讲已建立、本讲会反复用到的概念对齐（不重复细节，只复习结论）：

- **索引编码**：`Injector` 把「全局逻辑位置」「块内偏移」「第几圈（第几个 block）」「是否有后继 block」打包进一个 `usize`。约定 `SHIFT = 1`、`LAP = 64`、`HAS_NEXT = 1`，于是对任意 `index`：
  - 逻辑位置 `pos = index >> SHIFT`
  - 块内偏移 `offset = pos % LAP`
  - 第几个 block `lap = pos / LAP`
  - 是否有后继 `index & HAS_NEXT`
  每圈 `LAP=64` 个位置里，`offset == 63` 被当作**哨兵**（块边界），真正可写槽位是 `BLOCK_CAP = 63` 个（offset 0..62）。详见 [src/deque.rs:1204-1211](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1204-L1211)。
- **Slot 三状态位**：`WRITE=1`（任务已写入）、`READ=2`（任务已读出）、`DESTROY=4`（block 正在被销毁）。三者位不重叠，可共存。详见 [src/deque.rs:1196-1202](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1196-L1202)。
- **生产者发布顺序**（u3-l2）：`push` 先非原子地把任务体写入 `slot.task`，再 `slot.state.fetch_or(WRITE, Release)` 发布。消费者的 `Slot::wait_write` 用 `Acquire` 加载配对，形成 happens-before，保证不会读到未初始化的内存。详见 [src/deque.rs:1432-1435](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1432-L1435) 与 [src/deque.rs:1224-1229](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1224-L1229)。

一个贯穿本讲的关键直觉：**`Injector` 的 `head` 和 `tail` 是两个独立的 `Position`，`steal` 只动 `head`、`push` 只动 `tail`**，二者靠 block 链表（`block.next`）和状态位间接同步。这与 Worker/Stealer 那种「共享同一份环形 buffer」的模型截然不同。

## 3. 本讲源码地图

本讲全部源码集中在 [src/deque.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs) 单文件中，涉及的关键代码点如下：

| 代码点 | 行号 | 作用 |
| --- | --- | --- |
| 常量 `WRITE/READ/DESTROY/LAP/BLOCK_CAP/SHIFT/HAS_NEXT` | 1196-1211 | 状态位与索引编码 |
| `Slot::wait_write` | 1224-1229 | 自旋等待任务被写入（消费者侧） |
| `Block::wait_next` | 1272-1281 | 自旋等待 `block.next` 被安装 |
| `Block::destroy` | 1284-1301 | 协作销毁 block（本讲重点之一） |
| `Position<T>` | 1305-1311 | `(index, block)` 二元组 |
| `Injector<T>` 结构体 | 1332-1341 | `head` / `tail` 两个 `Position` |
| `Injector::push`（回顾） | 1388-1446 | 生产者发布，与 `steal` 配对 |
| **`Injector::steal`** | **1464-1540** | **本讲核心** |

测试侧可参考 [tests/injector.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/tests/injector.rs) 的 `smoke` / `spsc` / `mpmc` / `stampede` 用例，理解 `steal` 在并发下的行为。

---

## 4. 核心概念与源码讲解

本讲把 `steal` 与销毁机制拆成 4 个最小模块：先读 `head` 与块尾等待（4.1），再判定空与推进 `head`（4.2），再跨块与读任务（4.3），最后是 `READ`/`DESTROY` 与 `Block::destroy` 的协作销毁（4.4）。

### 4.1 Injector::steal 骨架：读 head、算偏移、块尾等待

#### 4.1.1 概念说明

`Stealer`/`Injector` 的偷取返回的是三态枚举 `Steal<T>`，而非 `Option<T>`：

- `Empty` —— 队列真的为空（在这次调用的快照里）。
- `Success(T)` —— 成功偷到一个任务。
- `Retry` —— **伪失败**：操作被并发干扰打断，应当**立刻重试**，而不是当作空。

区分 `Empty` 与 `Retry` 是回退链（`find_task`）正确性的关键（见 [u1-l4](./u1-l4-stealer-injector-steal-workflow.md)）。`Injector::steal` 的入口是一段「自旋读 head」的循环，目的只有一个：拿到一个**尚未被哨兵占据**的 `head` 位置。

#### 4.1.2 核心流程

```
steal() 入口循环:
  1. head.index  ← load(Acquire)
  2. head.block  ← load(Acquire)
  3. offset = (head >> SHIFT) % LAP
  4. 若 offset == BLOCK_CAP（哨兵 63）:
       backoff.snooze()   // 下一 block 正被 push 安装，长退避
       继续循环重读        // 回到第 1 步
     否则:
       break              // 拿到一个有效 offset，退出循环
```

为什么会有「`offset == BLOCK_CAP`」的等待？回顾 u3-l2：当 `push` 写满一个 block（写到 offset 62）时，它会在 CAS 成功后安装下一个 block，并把 `tail.index` 推过哨兵 63。在这极短的窗口里，`head` 可能已经追到哨兵位置（上一个 block 的最后一个槽位被消费后，`head` 被推到 offset 63），而下一个 block 还没安装好。此时 `steal` 用 `snooze`（长退避，会让出 CPU）等待 `push` 完成安装，再重读 `head`。

注意这里用 `snooze()` 而不是更轻的 `spin()`：跨 block 安装涉及分配内存和多次 Release store，耗时相对较长，`snooze` 能减少无谓的总线竞争。

#### 4.1.3 源码精读

入口循环：[src/deque.rs:1464-1483](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1464-L1483)

```rust
pub fn steal(&self) -> Steal<T> {
    let mut head;
    let mut block;
    let mut offset;

    let backoff = Backoff::new();
    loop {
        head = self.head.index.load(Ordering::Acquire);
        block = self.head.block.load(Ordering::Acquire);

        // Calculate the offset of the index into the block.
        offset = (head >> SHIFT) % LAP;

        // If we reached the end of the block, wait until the next one is installed.
        if offset == BLOCK_CAP {
            backoff.snooze();
        } else {
            break;
        }
    }
    ...
```

这段代码做了三件事，用中文标注：

- `head = self.head.index.load(Acquire)`：Acquire 加载 `head.index`，与 `steal` 自身稍后对 `head.index` 的 Release store、以及 `push` 对 `tail` 的更新建立同步。
- `block = self.head.block.load(Acquire)`：取出当前 head 指向的 block 指针。注意 `index` 和 `block` 是**两次独立加载**，这正是后面 4.2 节需要 `SeqCst` fence 来「校正」的原因。
- `offset = (head >> SHIFT) % LAP`：按索引编码算出块内偏移；命中哨兵 63 就 `snooze` 重读。

#### 4.1.4 代码实践

**实践目标**：亲手触发一次跨 block 的 `steal`，观察 FIFO 顺序与跨块的正确性。

**操作步骤**（新建一个依赖 `crossbeam-deque = "0.8"` 的 cargo 二进制项目，把下面代码放进 `src/main.rs`）：

```rust
// 示例代码：验证 Injector 跨 block 的 FIFO 偷取
use crossbeam_deque::{Injector, Steal};

fn main() {
    let q = Injector::new();
    // BLOCK_CAP = 63，push 130 个任务会跨过 block 0、1、2
    for i in 0..130u32 {
        q.push(i);
    }

    let mut got = Vec::new();
    loop {
        match q.steal() {
            Steal::Success(v) => got.push(v),
            Steal::Empty => break,
            // 单线程下 Retry 极少出现，这里防御性地重试
            Steal::Retry => continue,
        }
    }

    assert_eq!(got, (0..130).collect::<Vec<_>>());
    println!("OK: 按序偷到 {} 个任务，跨块链接正确", got.len());
}
```

**需要观察的现象**：

1. 程序应正常打印 `OK: 按序偷到 130 个任务，跨块链接正确`。
2. 当 `steal` 消费到 offset 62（第 63 个任务，即值 62）时，会进入 4.3 节描述的 `wait_next` 跨块分支，把 `head.block` 切到下一个 block，从而能继续读到值 63、64……

**预期结果**：`got` 严格等于 `[0,1,2,...,129]`，说明 block 链表的 `next` 指针与 `head` 推进在跨块时维护正确。

> 待本地验证：如果你在 `steal` 内部对应行加调试打印（需修改源码，仅供观察），可以看到 offset 在每个 block 内从 0 涨到 62 后跳回 0。

#### 4.1.5 小练习与答案

**练习 1**：为什么入口循环对 `offset == BLOCK_CAP` 用 `backoff.snooze()` 而不是 `backoff.spin()`？

**参考答案**：因为命中哨兵意味着下一个 block **正在被 `push` 安装**——这涉及内存分配和三处 Release store（`tail.block`、`tail.index`、`block.next`），耗时较长。`snooze` 是长退避（可能让出 CPU），适合等待这类「相对慢」的事件；`spin` 是短退避，适合 CAS 竞争失败这种「很快会有进展」的场景。

**练习 2**：`head.index` 和 `head.block` 是分两次 `Acquire` 加载的。如果在两次加载之间，另一个线程把 `head` 推进了 block，会读到「旧 index + 新 block」或「新 index + 旧 block」吗？

**参考答案**：理论上可能读到不一致的组合，但这种不一致只会让 `offset` 与 `block` 暂时错位。后续的 CAS（见 4.2）以这次读到的 `head` 为期望值去推进，若 `head` 已被别人改动，CAS 必然失败并返回 `Retry`；若 CAS 成功，则证明 `head` 在此期间未被改动，`block` 与 `index` 是一致的。也就是说，CAS 充当了「一致性兜底」，无需在加载阶段付出更高代价。

---

### 4.2 HAS_NEXT 位、空队列判定与 CAS 推进 head

#### 4.2.1 概念说明

拿到有效 `head` 后，`steal` 要决定两件事：

1. **队列是不是空的？** 若空，返回 `Steal::Empty`。
2. **当前 block 后面还有没有 block？** 若有，要在 `head` 上记一个 `HAS_NEXT` 标记，避免后续每次 `steal` 都重复去读 `tail`。

这两个判定都依赖对 `tail` 的观察，而 `tail` 由 `push` 修改，于是存在「读 `head` 和读 `tail` 之间的一致性」问题。`HAS_NEXT` 位正是用来缓存「我已知 head 和 tail 跨块」这一结论，减少重复判定。

#### 4.2.2 核心流程

```
new_head = head + (1 << SHIFT)            // 即 head + 2

if new_head & HAS_NEXT == 0:              // 最低位为 0：尚不知是否跨块，需要判定
    fence(SeqCst)                          // 在读 head 之后、读 tail 之前立一道全局屏障
    tail = self.tail.index.load(Relaxed)

    if head >> SHIFT == tail >> SHIFT:     // 逻辑位置相等 → 队空
        return Steal::Empty

    if (head >> SHIFT) / LAP != (tail >> SHIFT) / LAP:   // 不同 block（不同圈）
        new_head |= HAS_NEXT               // 记下「后面还有 block」

// 用 CAS 把 head 从 head 推进到 new_head
if compare_exchange_weak(head, new_head, SeqCst, Acquire).is_err():
    return Steal::Retry                    // 被别人抢了，伪失败
```

几个要点：

- **`HAS_NEXT` 的语义**：它是 `index` 的最低位（因为 `SHIFT=1`，正常推进每次加 2，最低位恒为 0，正好腾出来当标志）。一旦某次 `steal` 判定「head 与 tail 跨块」，就把 `HAS_NEXT` 写进 `new_head`；此后 `head` 在本 block 内继续推进时，最低位始终保留为 1，于是 `new_head & HAS_NEXT != 0` 成立，**直接跳过整段 tail 判定**——这是一个减少竞争热点的优化。
- **`SeqCst` fence 的作用**：它保证「本线程先读到 `head`、后读到 `tail`」这一顺序在全局 SC 序里成立。配合 `push` 那边对 `tail.index` 的 `SeqCst` CAS，可以避免「steal 误判空」：确保不会出现「steal 看到 head、却漏看了紧接着的 push」这种不一致快照。这是经典的 Dekker 式「双变量判定」配 fence 写法。
- **CAS 失败即 `Retry`**：`head` 是多消费者竞争的热点，CAS 失败说明别的 stealer 抢先推进了 `head`，返回 `Retry` 让调用方重试，而不是错误地返回 `Empty`。

#### 4.2.3 源码精读

空判定与 `HAS_NEXT` 设置：[src/deque.rs:1485-1500](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1485-L1500)

```rust
let mut new_head = head + (1 << SHIFT);

if new_head & HAS_NEXT == 0 {
    atomic::fence(Ordering::SeqCst);
    let tail = self.tail.index.load(Ordering::Relaxed);

    // If the tail equals the head, that means the queue is empty.
    if head >> SHIFT == tail >> SHIFT {
        return Steal::Empty;
    }

    // If head and tail are not in the same block, set `HAS_NEXT` in head.
    if (head >> SHIFT) / LAP != (tail >> SHIFT) / LAP {
        new_head |= HAS_NEXT;
    }
}
```

注意 `head >> SHIFT` 会同时剥掉 `SHIFT` 位和 `HAS_NEXT` 位，得到的是「纯逻辑位置」，所以 `head >> SHIFT == tail >> SHIFT` 比较的是不带任何标志的逻辑位置，判定空准确。

CAS 推进 `head`：[src/deque.rs:1503-1510](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1503-L1510)

```rust
// Try moving the head index forward.
if self
    .head
    .index
    .compare_exchange_weak(head, new_head, Ordering::SeqCst, Ordering::Acquire)
    .is_err()
{
    return Steal::Retry;
}
```

成功序 `SeqCst`、失败序 `Acquire`。成功后，本线程**独占了 `offset` 这个槽位**（别的 stealer 的 CAS 会失败），接下来就可以安全地读取槽位了。

#### 4.2.4 代码实践

**实践目标**：用源码阅读 + 断言验证 `HAS_NEXT` 优化与空判定的正确性。

**操作步骤**：

1. 阅读 [src/deque.rs:1485-1500](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1485-L1500)，在注释里用你自己的话回答：「`new_head & HAS_NEXT == 0` 为真时进入判定块；那么 `new_head & HAS_NEXT != 0`（即 `head` 已带 `HAS_NEXT`）时为什么可以**安全地跳过**空判定？」
2. 运行现有的 `smoke` 测试验证空语义：

```bash
cargo test --test injector smoke
```

**需要观察的现象**：`smoke` 测试应通过，其中 `assert_eq!(busy_retry(|| q.steal()), Empty)`（[tests/injector.rs:25](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/tests/injector.rs#L25)）验证空队列返回 `Empty`。

**预期结果**：测试通过；你的注释应说明「`HAS_NEXT` 一旦置位，意味着此前已有一次 `steal` 确认过 head 与 tail 跨块，只要 head 还没推进到块尾（4.3 节会重算 `HAS_NEXT`），这个结论就持续有效，因此无需重复读 tail」。

#### 4.2.5 小练习与答案

**练习 1**：空判定里用的是 `head >> SHIFT == tail >> SHIFT`，而不是 `head == tail`。为什么不能直接比 `head == tail`？

**参考答案**：因为 `head` 的最低位可能携带着 `HAS_NEXT` 标志，而 `tail` 不带这个标志（`push` 推进 `tail` 时只加 `1 << SHIFT`，不碰最低位）。直接比 `head == tail` 会把「逻辑位置相同但 head 带 `HAS_NEXT`」误判为不等。`>> SHIFT` 把最低位（`HAS_NEXT`）一起移掉，得到纯逻辑位置再比较才正确。

**练习 2**：CAS 失败时为什么返回 `Retry` 而不是 `Empty`？

**参考答案**：CAS 失败只说明「别的消费者抢先推进了 `head`」，并不说明队列空。若错误返回 `Empty`，会让 `find_task` 回退链误以为 Injector 已空、转而去偷别的队列，造成任务饥饿。`Retry` 准确传达了「被并发干扰，请立刻重试」的语义。

---

### 4.3 跨块切换 wait_next 与读取任务

#### 4.3.1 概念说明

CAS 成功后，本线程独占了 `offset` 槽位。但若 `offset` 恰好是 block 的最后一个可用槽位（`offset + 1 == BLOCK_CAP`，即 offset == 62），说明本 block 的 63 个任务都将被消费完毕，`head` 需要切换到下一个 block。这就是 `steal` 里的「跨块切换」分支，它和 `push` 的「跨块安装」（u3-l2）是一对镜像操作。

切换完成后，再从槽位里把任务读出来。读取要等生产者发布完成——这正是 `Slot::wait_write` 的职责。

#### 4.3.2 核心流程

```
（CAS 已成功，独占 offset 槽位）

if offset + 1 == BLOCK_CAP:                // 刚偷了块尾（offset == 62）
    next = block.wait_next()                // 自旋等 block.next 非 null
    next_index = (new_head & !HAS_NEXT) + (1 << SHIFT)   // 跳过哨兵，落到下一 block 的 offset 0
    if next.next 非 null:
        next_index |= HAS_NEXT              // 新 block 后面还有 block，补回标志
    head.block.store(next, Release)
    head.index.store(next_index, Release)

slot = block.slots[offset]
slot.wait_write()                           // Acquire 等 WRITE 位（配对 push 的 fetch_or(WRITE, Release)）
task = slot.task.read().assume_init()       // 读出任务

... 接下来是 READ/DESTROY 处理（见 4.4）...

return Steal::Success(task)
```

`next_index` 的计算是这一段最绕的地方，单独说一下：

- 进入跨块分支时，`offset == 62`，`new_head` 的逻辑位置落在本 block 的**哨兵 63** 上。
- `(new_head & !HAS_NEXT)` 先剥掉 `HAS_NEXT` 标志，得到「干净的、指向哨兵 63 的 index」。
- `.wrapping_add(1 << SHIFT)` 再加 2，逻辑位置从 63 推到 64，即 `64 / LAP = 第 1 圈`、`64 % LAP = offset 0`——正好是下一个 block 的第一个槽位。
- 最后，如果新的 block 自身也有后继（`next.next` 非 null），就把 `HAS_NEXT` 重新置上，保持标志的「懒缓存」语义。

#### 4.3.3 源码精读

跨块切换：[src/deque.rs:1512-1523](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1512-L1523)

```rust
unsafe {
    // If we've reached the end of the block, move to the next one.
    if offset + 1 == BLOCK_CAP {
        let next = (*block).wait_next();
        let mut next_index = (new_head & !HAS_NEXT).wrapping_add(1 << SHIFT);
        if !(*next).next.load(Ordering::Relaxed).is_null() {
            next_index |= HAS_NEXT;
        }

        self.head.block.store(next, Ordering::Release);
        self.head.index.store(next_index, Ordering::Release);
    }
    ...
```

`Block::wait_next` 的实现：[src/deque.rs:1272-1281](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1272-L1281)

```rust
fn wait_next(&self) -> *mut Self {
    let backoff = Backoff::new();
    loop {
        let next = self.next.load(Ordering::Acquire);
        if !next.is_null() {
            return next;
        }
        backoff.snooze();
    }
}
```

它用 `Acquire` 加载 `block.next`，与 `push` 端 `(*block).next.store(next_block, Release)`（[src/deque.rs:1429](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1429)）配对，确保消费者看到 `next` 指针时，也能看到新 block 里所有字段的初始化。

读取任务：[src/deque.rs:1525-1528](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1525-L1528)

```rust
    // Read the task.
    let slot = (*block).slots.get_unchecked(offset);
    slot.wait_write();
    let task = slot.task.get().read().assume_init();
```

`slot.wait_write()`（[src/deque.rs:1224-1229](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1224-L1229)）用 `Acquire` 自旋等 `WRITE` 位，与 `push` 的 `slot.state.fetch_or(WRITE, Release)`（[src/deque.rs:1435](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1435)）配对。这条 Acquire/Release 边是「不会读到未初始化内存」的根本保证：消费者看到 `WRITE` 位，意味着生产者写入任务体的非原子 store 已经对其可见。

#### 4.3.4 代码实践

**实践目标**：在 4.1.4 的跨块示例基础上，定位并验证「跨块切换」确实发生在 offset 62。

**操作步骤**：

1. 在 4.1.4 的 `main.rs` 里，把循环改成「记录每次 `steal` 返回值在 `got` 中的索引」，并在取出值 `62`、`63` 前后各打印一行，例如：

```rust
match q.steal() {
    Steal::Success(v) => {
        if v == 62 || v == 63 {
            println!("跨块边界：刚取到 {v}");
        }
        got.push(v);
    }
    _ => {}
}
```

2. 运行 `cargo run`。

**需要观察的现象**：会看到连续打印 `跨块边界：刚取到 62` 与 `跨块边界：刚取到 63`。前者对应 `steal` 走到 `offset == 62`、触发 `wait_next` 把 `head.block` 切换到下一个 block；后者是从新 block 的 offset 0 读出。

**预期结果**：最终断言 `got == 0..130` 仍然成立，证明跨块切换没有丢任务、没有乱序。

> 待本地验证：`wait_next` 在单生产者提前 push 满所有任务的情况下几乎不会真正自旋（`block.next` 早已安装好），所以你不会看到卡顿；只有在「边 push 边 steal」且卡在块尾时才会触发它的 snooze 循环。

#### 4.3.5 小练习与答案

**练习 1**：跨块分支里更新 `head.block` 和 `head.index` 都用了 `Release`，而不是 `SeqCst`。为什么这里 `Release` 就够？

**参考答案**：这里只是**发布**本线程对 `head` 的推进结果，目的是让后续的 `steal`（用 `Acquire` 读 `head`）看到新的 `block`/`index` 以及新 block 中字段的初始化。这是一个单向的「发布」，不需要和其他变量建立全局 SC 序，所以 `Release` 足够，且比 `SeqCst` 便宜。前面 4.2 节判定空时用 `SeqCst` fence，是因为那里涉及 `head` 与 `tail` **两个变量**的协同观察，需要全局屏障。

**练习 2**：`wait_write` 用 `Acquire` 自旋等 `WRITE`。如果生产者用 `Relaxed` 而非 `Release` 来 `fetch_or(WRITE)`，会发生什么？

**参考答案**：会破坏 happens-before。消费者可能看到 `WRITE` 位已置，却读不到生产者写入槽位的任务体（读到未初始化内存），这是未定义行为。`Release` 发布保证了「写任务体」先于「置 WRITE 位」对消费者可见，`Acquire` 加载则让消费者在看到 WRITE 后也能看到任务体。

---

### 4.4 发布 READ 位、DESTROY 检测与 Block::destroy 协作销毁

这是本讲最精巧的部分。一个 block 有 63 个槽位，可能被多达 63 个线程**同时**读取不同的槽位（每个槽位因 CAS 只被一个线程独占）。那么谁来释放这个 block？怎么保证恰好释放一次？答案藏在一对状态位 `READ` / `DESTROY` 和一个「接力令牌」机制里。

#### 4.4.1 概念说明

先把销毁问题的来龙去脉讲清楚：

- 一个 block 一旦被消费完毕（所有 63 个槽位都已读出），它就成了垃圾，应当被释放。
- 但「最后一个读完的线程」并不能简单粗暴地 `drop` 整个 block，因为**别的线程可能正卡在某个槽位的 `wait_write` 或 `read` 上**——block 还在被使用。
- 反过来，也不能让多个线程都尝试释放，否则会 double-free。

`crossbeam-deque` 的解法是**协作销毁（cooperative destruction）**：

1. 每个读槽位的线程，读完都会用 `fetch_or(READ)` 把该槽位标记为「已读」。
2. 当某个线程判定「本 block 该考虑销毁了」（比如刚读完块尾 offset 62，或发现槽位上已有别人留下的 `DESTROY` 标记），它就调用 `Block::destroy(block, offset)`。
3. `Block::destroy` 从高到低扫描槽位，**找出第一个还没被标记 `READ` 的槽位**，给它打上 `DESTROY` 标记然后**返回**（把手里的销毁责任「交接」给将来读这个槽位的线程）。
4. 若一直扫描都没遇到未读槽位（说明所有前序槽位都已读完、没有线程在用），则真正 `drop` 这个 block。

这样，销毁责任像一根**接力棒**：每个拿到它的线程，要么找到下一个还在用槽位的线程把棒交出去，要么确认无人使用后自己终结（drop）。**接力棒同一时刻只在一处**，所以恰好被 drop 一次。

#### 4.4.2 核心流程

`steal` 末尾的销毁触发：[src/deque.rs:1530-1536](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1530-L1536)

```
// 已读出 task

if (offset + 1 == BLOCK_CAP)                               // 情况 A：刚读完块尾 offset 62
   || (slot.state.fetch_or(READ, AcqRel) & DESTROY != 0):  // 情况 B：本槽位被人留了 DESTROY 标记
{
    Block::destroy(block, offset)                          // 接过/发起销毁责任
}
```

两种触发情形：

- **情况 A（块尾）**：读完 offset 62 意味着本 block 的所有可用槽位都已被消费，由本线程发起销毁。
- **情况 B（接力）**：本线程读槽位时，发现 `DESTROY` 位已被置——这说明此前有别的线程扫描到了本槽位、把销毁责任留在了这里。本线程读完，接过接力棒，继续 `destroy`。

注意情况 B 的 `fetch_or(READ, AcqRel)` 是**一次操作两用**：既标记本槽位已读，又顺带取回旧状态判断是否带 `DESTROY`。

`Block::destroy` 内部：[src/deque.rs:1284-1301](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1284-L1301)

```
destroy(this, count):    // count == 触发者的 offset
    for i in (0..count).rev():        // 从 count-1 往下扫到 0
        slot = this.slots[i]
        // 双重检查 READ：先乐观 load，再权威 fetch_or
        if slot.state.load(Acquire) & READ == 0
           && slot.state.fetch_or(DESTROY, AcqRel) & READ == 0:
            return                      // 找到「未读」槽位，标记 DESTROY，交接责任，返回
    // 所有 [0, count) 槽位都已读 → 无人使用，安全销毁
    drop(Box::from_raw(this))
```

要点：

- **反向扫描**（`.rev()`）：从离 `count` 最近的槽位往下找，确保总是把接力棒交给「最高位的、还在用的槽位」，使销毁责任单调向低位推进，最终收敛。
- **双重检查 `READ`**：先 `load(Acquire)` 做快速乐观判断（命中已读就跳过，避免写脏缓存行），再用 `fetch_or(DESTROY, AcqRel)` 做**权威**判断——它的返回值是 `fetch_or` 之前的旧状态，能捕捉到 load 之后才被置上的 `READ`，消除「检查与标记之间」的竞态。
- **跳过触发槽位**：循环范围是 `0..count`，不含 `count` 自身。注释说明触发槽位（即调用者刚读的那个）已在销毁流程中，无需再标记。
- **唯一性**：每次 `destroy` 调用最多给一个槽位打 `DESTROY`（找到就 return），所以「接力棒」全局唯一；只有扫描到底（所有槽位都 `READ`）的那次调用才执行 `drop`。

#### 4.4.3 源码精读

`steal` 末尾触发销毁：[src/deque.rs:1530-1536](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1530-L1536)

```rust
            // Destroy the block if we've reached the end, or if another thread wanted to destroy
            // but couldn't because we were busy reading from the slot.
            if (offset + 1 == BLOCK_CAP)
                || (slot.state.fetch_or(READ, Ordering::AcqRel) & DESTROY != 0)
            {
                Block::destroy(block, offset);
            }

            Steal::Success(task)
```

`Block::destroy` 全文：[src/deque.rs:1284-1301](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1284-L1301)

```rust
    /// Sets the `DESTROY` bit in slots starting from `start` and destroys the block.
    unsafe fn destroy(this: *mut Self, count: usize) {
        // It is not necessary to set the `DESTROY` bit in the last slot because that slot has
        // begun destruction of the block.
        for i in (0..count).rev() {
            let slot = unsafe { (*this).slots.get_unchecked(i) };

            // Mark the `DESTROY` bit if a thread is still using the slot.
            if slot.state.load(Ordering::Acquire) & READ == 0
                && slot.state.fetch_or(DESTROY, Ordering::AcqRel) & READ == 0
            {
                // If a thread is still using the slot, it will continue destruction of the block.
                return;
            }
        }

        // No thread is using the block, now it is safe to destroy it.
        drop(unsafe { Box::from_raw(this) });
    }
```

把这段逐行翻译成「正在发生什么」：

- `for i in (0..count).rev()`：从 `count-1` 扫到 0。`count` 是触发者刚读的 `offset`，所以扫描范围是「所有比触发槽位更靠前的槽位」。
- `if slot.state.load(Acquire) & READ == 0`：若该槽位**尚未**被读，说明可能有线程正卡在它上面（已 CAS 占坑但还没 `fetch_or(READ)`）。
- `&& slot.state.fetch_or(DESTROY, AcqRel) & READ == 0`：原子地置 `DESTROY` 并复查旧值里有没有 `READ`。若仍无 `READ`，确认这个槽位「有人在用」，`return` 把责任交出去；若复查发现 `READ` 已被置（load 与 fetch_or 之间被读了），说明其实没人用了，继续循环。
- 循环走完仍没 return：`[0, count)` 全都已读，没有任何线程在使用本 block，`drop(Box::from_raw(this))` 真正释放。

#### 4.4.4 代码实践（本讲指定实践）

**实践目标**：用注释+逐步推演，论证「多线程并发读同一 block 的不同槽位、且某线程读到块尾 offset 62 时，`Block::destroy` 如何保证恰好释放一次、不会 double-free」。

**操作步骤**：在一个独立文档（或代码注释）里，按下面的剧本逐步推演。设定：block 0 有 63 个槽位（offset 0..62），8 个线程 T0..T7 并发 `steal`，分别抢到 offset 0..7（为简化，假设只有前 8 个槽位有任务，且 T7 抢到 offset 7……但为了让「块尾」触发，我们把场景改成：T_last 抢到 offset 62）。为兼顾「块尾触发」与「接力」，按下面两个阶段写：

**阶段一：块尾触发（情况 A）**

1. 假设 T62 通过 CAS 抢到了 offset 62。它读完任务，`offset + 1 == BLOCK_CAP` 成立，调用 `Block::destroy(block, 62)`。
2. `destroy(block, 62)` 反向扫描 `i = 61, 60, ..., 0`。假设此刻槽位 0..61 中，只有 T5 抢到了 offset 5 但**还没**执行到 `fetch_or(READ)`（即 slot 5 的 `READ == 0`，T5 正卡在 `wait_write` 或 `read`）。
3. 扫描到 `i = 61,60,...,6` 时这些槽位 `READ` 都已置（对应线程已读完），循环继续；扫描到 `i = 5` 时，`load() & READ == 0` 且 `fetch_or(DESTROY) & READ == 0` 同时成立 → **给 slot 5 打上 `DESTROY`，`return`**。T62 不释放 block，责任交接给 T5。
4. T5 随后完成读取，执行 `slot.state.fetch_or(READ) & DESTROY != 0`——发现 `DESTROY` 已置（情况 B），于是调用 `Block::destroy(block, 5)`。

**阶段二：接力收敛（情况 B → … → drop）**

5. `destroy(block, 5)` 扫描 `i = 4,3,2,1,0`。若此刻 slot 0..4 都已 `READ`，则循环走完，`drop(Box::from_raw(block))`——**T5 是最终终结者**，block 被释放恰好一次。
6. 若 slot 0..4 中还有某个 slot k 的 `READ == 0`，则把 `DESTROY` 打在最高的那个未读 slot 上并 return，责任继续下传，直到某次扫描发现 `[0, k)` 全部已读，由那个线程执行 drop。

**需要观察/验证的现象**（写成你的结论）：

- 全程**至多一个 slot** 携带 `DESTROY`（接力棒唯一）：因为每次 `destroy` 一旦给某个 slot 打 `DESTROY` 就立即 `return`，不会继续给更低 slot 打标记；而新的 `DESTROY` 只在新接力者再次调用 `destroy` 时产生，且范围更小。
- **恰好一次 drop**：`drop` 只在「扫描到底、所有前序槽位都 `READ`」时执行；而每次销毁责任传递都会**缩小**扫描范围（`count` 单调递减），所以最终必然有一次调用扫描到底并 drop，且只有那一次。
- **不会 double-free**：`drop(Box::from_raw(this))` 只出现在 `destroy` 的最后一行，且只有「扫描到底」的调用能到达；接力中的调用都在 `return` 提前退出，根本到不了 `drop`。

**预期结果**：你的注释能自洽地解释「接力棒全局唯一 → 范围单调收敛 → 恰好一次 drop」这条因果链。

**对照真实测试**：运行并发压测，观察无 double-free / 无任务丢失：

```bash
cargo test --test injector mpmc
cargo test --test injector stampede
```

> `mpmc`（[tests/injector.rs:89-125](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/tests/injector.rs#L89-L125)）让 4 个线程 push、4 个线程 steal 共 25000 轮，最终断言每个任务被消费恰好 `THREADS` 次——这正是「无丢失、无重复、无 double-free」的端到端验证。若 `Block::destroy` 的协作销毁有 bug（如 double-free），在多线程压测下几乎必然以崩溃或 ASan/MSan 报错暴露。

#### 4.4.5 小练习与答案

**练习 1**：`Block::destroy` 里的双重检查写成 `if load() & READ == 0 && fetch_or(DESTROY) & READ == 0`。如果去掉前面的 `load()`，只保留 `fetch_or`，正确性是否受影响？为什么仍要保留 `load()`？

**参考答案**：正确性不受影响——`fetch_or(DESTROY)` 的返回值（旧状态）才是权威判断，单独用它足以正确决定是否交接。保留前面的 `load(Acquire)` 是一个**性能优化**：对于已经 `READ` 的槽位（销毁时的常见情况，因为多数槽位早已读完），先做一次只读的 `load` 就能跳过，避免 `fetch_or` 把 `DESTROY` 写进去从而弄脏该槽位所在的缓存行（这会无谓地广播缓存失效）。这是无锁代码里常见的「乐观快路径 + 权威慢路径」写法。

**练习 2**：为什么 `destroy` 用反向扫描（`.rev()`），而不是正向 `0..count`？

**参考答案**：反向扫描保证接力棒总是交给「最高的、还在用的槽位」。这样销毁责任的下界（`count`）单调递减，每次接力的扫描范围严格缩小，保证有限步内收敛到 drop。若正向扫描，可能在低位 slot 打 `DESTROY`，而高位还有未读 slot，接力路径会反复跳动、范围不单调，虽然最终也能收敛，但推理更复杂且可能多走几轮。反向扫描让「未读槽位必然在已扫过的高位之外」这一不变式成立，逻辑更清晰。

**练习 3**：销毁触发条件里，`offset + 1 == BLOCK_CAP`（情况 A）为什么不也需要检查 `DESTROY`？换句话说，块尾触发的线程会不会和某个接力者重复 drop？

**参考答案**：不会。块尾触发（情况 A）是**首次**对本 block 发起销毁——在此之前没有任何线程对本 block 调用过 `destroy`，因此本 block 上还没有任何 `DESTROY` 标记。情况 A 的线程是接力链的**起点**，它调用 `destroy(block, 62)` 后，要么自己 drop（若所有前序槽位已读），要么把唯一的接力棒交出去。此后任何对本 block 的销毁都只能经由「读到带 `DESTROY` 的槽位」（情况 B）进入，而接力棒唯一、范围单调收敛，故不会与起点重复 drop。

---

## 5. 综合实践

把本讲四个模块串起来，做一个「边 push 边 steal、跨多 block、多消费者」的小测试，端到端验证 `Injector::steal` 与 `Block::destroy` 的正确性。

**任务**：参考 [tests/injector.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/tests/injector.rs) 的 `spsc` 与 `mpmc` 写法，写一个测试：单个 `Injector`，1 个生产者线程 push `0..N`（N 取 200，跨 4 个 block），4 个消费者线程并发 `steal`，用 `AtomicUsize` 数组记录每个值被消费的次数，最终断言每个值恰好被消费一次。

**参考骨架**（示例代码）：

```rust
// 示例代码：综合实践 —— 跨 block 多消费者 steal
use crossbeam_deque::{Injector, Steal};
use crossbeam_utils::thread::scope;
use std::sync::atomic::{AtomicUsize, Ordering::SeqCst};

const N: usize = 200;
const CONSUMERS: usize = 4;

let q = Injector::new();
let hits = (0..N).map(|_| AtomicUsize::new(0)).collect::<Vec<_>>();

scope(|s| {
    s.spawn(|_| {
        for i in 0..N {
            q.push(i);
        }
    });
    for _ in 0..CONSUMERS {
        s.spawn(|_| {
            let mut stolen = 0usize;
            while stolen < N {
                if let Steal::Success(v) = q.steal() {
                    // 每个值应只被消费一次；若被消费多次，下面会 panic
                    let prev = hits[v].fetch_add(1, SeqCst);
                    assert_eq!(prev, 0, "值 {v} 被重复消费，疑似销毁/接力 bug");
                    stolen = 0; // 重置空闲计数
                } else {
                    stolen += 1; // 连续多次空/重试后退出（简化处理）
                    if stolen > 1_000_000 { break; }
                    std::hint::spin_loop();
                }
            }
        });
    }
}).unwrap();

for (i, h) in hits.iter().enumerate() {
    assert_eq!(h.load(SeqCst), 1, "值 {i} 未被消费或被重复消费");
}
println!("综合实践通过：{N} 个任务跨多 block 被 {CONSUMERS} 个消费者各消费恰好一次");
```

**验收标准**：

1. 测试稳定通过（多次运行无 panic、无崩溃）——验证 `Block::destroy` 的协作销毁不会 double-free（否则会在某次运行崩在重复消费断言或释放后使用）。
2. 所有 `N` 个值各被消费恰好一次——验证 `steal` 的 CAS 推进、`HAS_NEXT`/空判定、跨块切换全程正确。
3. 你能用本讲 4.4 的「接力棒」模型，口头解释消费者线程在 block 边界（offset 62）附近的行为。

> 说明：上面骨架用一个简化的「连续若干次失败就退出」的策略防止消费者在所有任务已消费后死循环；生产环境请用 `busy_retry` 或更精确的「已知总量」计数。完整且稳健的写法见 [tests/injector.rs:60-86（spsc）](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/tests/injector.rs#L60-L86) 与 [tests/injector.rs:89-125（mpmc）](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/tests/injector.rs#L89-L125)。

## 6. 本讲小结

- `Injector::steal` 从 `head` 端消费：先 Acquire 读 `head.index`/`head.block` 算出 `offset`，命中哨兵 63（块边界、下一 block 尚未安装）就 `snooze` 重读。
- 用 `HAS_NEXT` 位懒缓存「head 与 tail 是否跨 block」的判定：`new_head & HAS_NEXT == 0` 时才在 `SeqCst` fence 后读 `tail` 做空判定与跨块判定；CAS 推进 `head` 失败则返回 `Steal::Retry`。
- 读完块尾（offset 62）触发跨块切换：`wait_next` 自旋等 `block.next`，计算 `next_index`（剥 `HAS_NEXT`、跳过哨兵、按需补 `HAS_NEXT`），用 Release 更新 `head.block`/`head.index`。
- 读任务靠 `Slot::wait_write`（Acquire）配对 `push` 的 `fetch_or(WRITE, Release)` 建立 happens-before，杜绝读到未初始化内存。
- 销毁采用**协作接力**：`READ` 位标记「已读」，`DESTROY` 位是「销毁责任令牌」；`Block::destroy` 反向扫描，遇到未读槽位就打 `DESTROY` 并交接、扫描到底则 `drop`。接力棒全局唯一、扫描范围单调收敛，保证 block 恰好被释放一次。

## 7. 下一步学习建议

- 下一讲 [u3-l4（Injector 批量 steal、len 与 Drop）](./u3-l4-injector-batch-len-drop.md) 会把同样的索引编码与 block 链表用到批量偷取（`steal_batch*`）、`len()` 的一致性重读循环，以及 `Injector::Drop` 遍历 `head..tail` 回收剩余任务与 block 上——届时你会看到 `destroy` 之外另一条 block 释放路径。
- 若想横向对照「另一种 block 释放策略」，可回顾 [u2-l5（resize 与 reserve）](./u2-l5-resize-and-buffer-lifecycle.md) 中 Worker/Stealer 用 `crossbeam-epoch` 延迟回收旧 buffer 的做法，体会「epoch GC」与「协作销毁」两种无锁回收思路的差异。
- 对内存序的配对关系（Acquire/Release/SeqCst fence）想系统梳理的读者，可在学完 u3-l4 后进入 [u4-l1（内存序与 volatile hack 深入）](./u4-l1-memory-ordering.md)，把本讲散落各处的 fence 选择统一串讲一遍。
