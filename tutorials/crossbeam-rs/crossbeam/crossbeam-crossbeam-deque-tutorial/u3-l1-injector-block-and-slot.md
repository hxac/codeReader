# Injector 设计与 Block/Slot 数据结构

## 1. 本讲目标

学完本讲，你应该能够：

- 说清楚 `Injector` 与 `Worker/Stealer` 在**底层实现上为什么完全不同**：前者是「block 链表 + 双 Position」，后者是「单一环形 Buffer」。
- 解释 `LAP`、`BLOCK_CAP`、`SHIFT`、`HAS_NEXT` 这一组常量如何把「逻辑队列索引」和「块内偏移」「是否有下一个块」全部编码进一个 `usize` 里。
- 理解 `Slot` 用 `UnsafeCell<MaybeUninit<T>>` 存任务、用 `AtomicUsize` 的 `WRITE/READ/DESTROY` 三位标记状态的设计，以及 `wait_write` 为什么需要自旋。
- 读懂 `Block` 作为链表节点的结构，以及 `Block::new / wait_next / destroy` 三个方法各自负责什么。
- 画出 `head`/`tail` 两个 `Position`、`block.next` 指针在跨块时的链接关系。

本讲**只讲数据结构与索引编码**，不深入 `Injector::push` 的完整并发算法（那是 u3-l2）、也不深入 `Injector::steal` 的 `Block::destroy` 协作回收（那是 u3-l3）。我们把这两个流程当成黑盒，只看它们用到的「积木」长什么样。

## 2. 前置知识

在进入本讲前，请确认你已经掌握（这些在 u1-l4、u2-l1 已建立）：

- **三种队列角色**：`Worker`（单线程私有本地队列）、`Stealer`（偷取视图）、`Injector`（全局共享 FIFO 入口）。`Injector` 是任务调度器里「外部把新任务投递进系统」的总入口，通常一个调度器只有一个。
- **Chase-Lev 环形缓冲区**：`Worker/Stealer` 共享一个容量为 2 的幂的环形 `Buffer<T>`，用 `front`/`back` 两个 `AtomicIsize` 游标 + 掩码 `index & (cap-1)` 做 O(1) 寻址。
- **`MaybeUninit<T>`**：一种「内存已分配但值可能未初始化」的容器，读写需要 `unsafe`，常用于无锁数据结构里「先占槽位、后填值」的场景。
- **`CachePadded<T>`**：把数据填充到缓存行对齐，避免多核之间的「伪共享」（false sharing）。
- **`AtomicUsize` 的位运算**：用 `fetch_or` 置位、`& mask` 取位是无锁状态机的常用手段。

一个关键对比直觉：环形 `Buffer` 容量固定（运行期靠 `resize` 扩缩），适合「单 owner + 多 stealer」的本地队列；而 `Injector` 是**全局、多生产者多消费者、理论上无限增长**的入口，固定容量的环形缓冲区不合适——它需要一个能随任务数增长、且无需整体搬移的结构，这就是**链表式分块（linked list of blocks）**。

## 3. 本讲源码地图

本讲涉及的代码**全部集中在一个文件**里：

| 文件 | 作用 |
| --- | --- |
| [src/deque.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs) | 整个 crate 的唯一实现文件。本讲关注其中的 `Injector` 一节（约 L1196–L1361），包括状态位常量、`Slot`、`Block`、`Position`、`Injector` 五个类型。 |

具体地，你会依次读到：

1. 一组 `const`：`WRITE/READ/DESTROY`（槽位状态位）与 `LAP/BLOCK_CAP/SHIFT/HAS_NEXT`（索引编码常量）。
2. `Slot<T>`：单个任务槽 + `wait_write`。
3. `Block<T>`：含 `BLOCK_CAP` 个 `Slot` 的链表节点 + `new/wait_next/destroy`。
4. `Position<T>`：一个 `(index, block)` 二元组，表示队列里的某个位置。
5. `Injector<T>`：持有 `head`/`tail` 两个 `Position`，外加构造函数。

## 4. 核心概念与源码讲解

### 4.1 索引编码与状态位常量

#### 4.1.1 概念说明

`Injector` 要在一个 `usize` 大小的「索引」里同时携带三类信息：

1. **逻辑队列位置**：这是队列里的第几个任务（全局递增，不随块切换而重置）。
2. **块内偏移**：在当前 block 的 63 个槽位里，落在第几个。
3. **元数据位**：例如「这个索引之后还有下一个 block 吗」。

为此源码定义了一组常量，把一个 `usize` 当成「低位保留给元数据、其余位才是真正的递增索引」的位打包结构。同时，每个 `Slot` 也用一个 `AtomicUsize` 的三个独立位来标记自己的状态。这两组常量是理解后续所有 `>>`、`%`、`fetch_or` 操作的钥匙。

#### 4.1.2 核心流程

**槽位状态位**（每个 `Slot` 的 `state` 字段里互不冲突的三位）：

| 常量 | 值 | 含义 |
| --- | --- | --- |
| `WRITE` | `1` | 任务已被写入这个槽位（生产者发布完成）。 |
| `READ` | `2` | 任务已被读走（消费者消费完成）。 |
| `DESTROY` | `4` | 这个 block 正在被销毁。 |

**索引编码常量**（决定一个 `usize` 索引如何被解读）：

| 常量 | 值 | 含义 |
| --- | --- | --- |
| `LAP` | `64` | 「一圈」覆盖 64 个逻辑索引；一个 block 对应一圈。 |
| `BLOCK_CAP` | `LAP - 1 = 63` | 一个 block 最多放 63 个任务（第 64 个索引是「块边界」哨兵）。 |
| `SHIFT` | `1` | 索引的最低 1 位保留给元数据，真正的逻辑索引是 `index >> 1`。 |
| `HAS_NEXT` | `1` | 占用索引的最低位；为 1 表示「当前 block 之后还有下一个 block」。 |

给定一个原始索引 `index`，解读方式是：

- 逻辑索引：\( \text{logical} = \text{index} \gg \text{SHIFT} \)
- 块内偏移：\( \text{offset} = \text{logical} \bmod \text{LAP} \)
- 第几圈（即第几个 block）：\( \text{lap} = \text{logical} \,/\, \text{LAP} \)
- 是否有后继块：`index & HAS_NEXT != 0`

为什么 `BLOCK_CAP = LAP - 1` 而不是等于 `LAP`？因为每一圈 64 个逻辑索引里，必须**留出一个索引值（offset == 63）作为「块边界」哨兵**——它不存任务，只用来标记「当前 block 写满了，该切换到 `block.next`」。所以每个 block 实际可用槽位是 63。

#### 4.1.3 源码精读

槽位状态位定义在 [src/deque.rs:1196-1202](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1196-L1202)，注释清楚说明了三个位的语义：

```rust
// Bits indicating the state of a slot:
// * If a task has been written into the slot, `WRITE` is set.
// * If a task has been read from the slot, `READ` is set.
// * If the block is being destroyed, `DESTROY` is set.
const WRITE: usize = 1;
const READ: usize = 2;
const DESTROY: usize = 4;
```

索引编码常量紧接着在 [src/deque.rs:1204-1211](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1204-L1211)：

```rust
// Each block covers one "lap" of indices.
const LAP: usize = 64;
// The maximum number of values a block can hold.
const BLOCK_CAP: usize = LAP - 1;
// How many lower bits are reserved for metadata.
const SHIFT: usize = 1;
// Indicates that the block is not the last one.
const HAS_NEXT: usize = 1;
```

注意 `SHIFT = 1` 与 `HAS_NEXT = 1` 的关系：`HAS_NEXT` 正好占用 `SHIFT` 所保留的那 1 个低位。也就是说，「保留给元数据的低位」目前唯一的用途就是存 `HAS_NEXT` 这一位。

这两组常量在后续 `push`/`steal` 里被反复使用，例如 [src/deque.rs:1396](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1396) 计算 `let offset = (tail >> SHIFT) % LAP;`——本讲你只需记住这个解码公式即可。

#### 4.1.4 代码实践

**实践目标**：亲手把索引编码公式跑一遍，验证「块边界哨兵」确实出现在 offset == 63。

**操作步骤**：

1. 打开一个 Rust playground 或本地 `cargo new decode && cd decode`。
2. 写一段纯算术（不需要任何依赖）：

```rust
// 示例代码：复现 Injector 的索引解码逻辑
const LAP: usize = 64;
const BLOCK_CAP: usize = LAP - 1;
const SHIFT: usize = 1;
const HAS_NEXT: usize = 1;

fn decode(index: usize) {
    let logical = index >> SHIFT;
    let offset = logical % LAP;
    let lap = logical / LAP;
    let has_next = index & HAS_NEXT != 0;
    println!(
        "raw={:#x} logical={} lap={} offset={} has_next={}",
        index, logical, lap, offset, has_next
    );
}

fn main() {
    // 枚举若干逻辑连续的索引（每次 += 1 << SHIFT）
    let mut idx = 0usize;
    for _ in 0..70 {
        decode(idx);
        idx += 1 << SHIFT;
    }
}
```

**需要观察的现象**：

- `offset` 从 `0` 递增到 `62`，然后下一步**跳回 `0`**（因为新的一圈/新的 block），`offset` 永远不会停在 `63` 写任务——但 `63` 这个值在 `push` 的中间窗口里会短暂出现。
- `lap` 在跨块时 `+1`：`offset` 跳回 0 的同时 `lap` 从 0 变 1。

**预期结果**：第 63 次迭代时 `offset=62`（block 0 的最后一个可用槽），第 64 次迭代时 `offset=0, lap=1`（进入 block 1）。

> 待本地验证：上面的循环是「逻辑连续」的理想序列；真实 `push` 里 `tail` 会被 CAS 原子推进，单线程下序列与此一致。

#### 4.1.5 小练习与答案

**练习 1**：给定 `tail = 130`（十进制），求 `offset`、`lap`、`logical`。

答案：\( \text{logical} = 130 \gg 1 = 65 \)；\( \text{offset} = 65 \bmod 64 = 1 \)；\( \text{lap} = 65 / 64 = 1 \)。即「第 2 个 block（lap=1）的第 1 个槽位」。

**练习 2**：为什么 `HAS_NEXT` 用最低位、而 `logical` 用 `index >> 1`，两者不会冲突？

答案：因为 `SHIFT = 1` 明确规定了「最低 1 位是元数据区，不参与逻辑索引」。`HAS_NEXT` 占用的正是这 1 位；`index >> 1` 把这 1 位右移出去，所以 `logical` 的值与 `HAS_NEXT` 是否为 1 互相独立。

---

### 4.2 Slot\<T\>：任务槽与 wait_write 自旋等待

#### 4.2.1 概念说明

`Slot` 是 `Injector` 里**最小的工作单元**——存放一个任务。它必须解决一个无锁队列的经典难题：消费者可能在生产者「刚刚认领了槽位、但还没把任务写进去」的窗口里读到这个槽位。于是 `Slot` 把「数据」和「是否就绪」分成两个字段：

- `task`：真正存任务的地方，用 `UnsafeCell<MaybeUninit<T>>`，意味着「内部可变 + 可能未初始化」。
- `state`：一个 `AtomicUsize`，用上一节的 `WRITE/READ/DESTROY` 位标记这个槽处于生命周期的哪一阶段。

`UnsafeCell` 是 Rust 里「告诉编译器这里可能有共享可变」的合法出口；`MaybeUninit` 进一步声明「值可能还没填」。两者叠加，让生产者可以「先占位、后写入」而不触发 UB。

#### 4.2.2 核心流程

一个槽位的生命周期：

1. **诞生**：`Block::new` 把整块内存零初始化，此时 `state == 0`（无 `WRITE`、无 `READ`、无 `DESTROY`），`task` 未初始化。
2. **写入**：生产者 CAS 认领该槽位后，调用 `slot.task.write(...)` 填值，再用 `slot.state.fetch_or(WRITE, Release)` 发布——`Release` 序保证「写值」对其他线程可见早于 `WRITE` 位置位。
3. **等待**：若消费者先到了（`WRITE` 位还没置上），就调用 `wait_write()` 自旋等待 `WRITE` 出现。
4. **读走**：消费者读出任务后，用 `fetch_or(READ)` 标记已读，配合后续 `Block::destroy` 决定能否回收。

`wait_write` 是「乐观 + 自旋退避」策略：它假设 `WRITE` 位马上就会被置上（生产者只是在另一个核上、几条指令之后就会发布），所以用 `Backoff::snooze()` 做「先忙等、再逐步退避」的轻量等待，而不是直接 `yield` 或睡眠。

#### 4.2.3 源码精读

`Slot` 结构定义在 [src/deque.rs:1213-1220](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1213-L1220)：

```rust
/// A slot in a block.
struct Slot<T> {
    /// The task.
    task: UnsafeCell<MaybeUninit<T>>,

    /// The state of the slot.
    state: AtomicUsize,
}
```

`wait_write` 在 [src/deque.rs:1222-1230](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1222-L1230)：

```rust
impl<T> Slot<T> {
    /// Waits until a task is written into the slot.
    fn wait_write(&self) {
        let backoff = Backoff::new();
        while self.state.load(Ordering::Acquire) & WRITE == 0 {
            backoff.snooze();
        }
    }
}
```

要点解读：

- `load(Ordering::Acquire)` 与生产者的 `fetch_or(WRITE, Release)` 配对，构成 happens-before：一旦这里读到 `WRITE` 位，此前生产者对 `task` 的写入对本线程可见。
- `& WRITE == 0` 表示「还没写入」，就继续 `snooze()`。
- `Backoff` 来自 `crossbeam_utils`（见 [src/deque.rs:13](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L13) 的 `use crossbeam_utils::{Backoff, CachePadded};`），`snooze()` 会在前期忙等、后期插入 `cpu_relax`/`yield` 以降低总线争用。

#### 4.2.4 代码实践

**实践目标**：理解 `Acquire`/`Release` 配对在 `wait_write` 里的必要性。

**操作步骤**：

1. 阅读生产者侧 [src/deque.rs:1432-1435](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1432-L1435) 的发布顺序：先 `slot.task.write(...)`，再 `slot.state.fetch_or(WRITE, Release)`。
2. 在注释里把这两步与 `wait_write` 的 `load(Acquire)` 连成一条同步链。

**需要观察的现象**：发布是「写数据 → Release 置 WRITE 位」；读取是「Acquire 读 WRITE 位 → 读数据」。两边的序不能反。

**预期结果**：你能用一句话说明——如果生产者把 `fetch_or` 改成 `Relaxed`，消费者可能看到 `WRITE` 已置位却读到未初始化的 `task`，从而触发 UB。这正是 `Acquire/Release` 不可省的原因。

> 待本地验证：这里无需运行，只需在源码旁加注释说明同步关系即可。

#### 4.2.5 小练习与答案

**练习 1**：`Slot` 为什么用 `UnsafeCell<MaybeUninit<T>>` 而不是直接 `T`？

答案：直接放 `T` 要求槽位「始终已初始化」，但无锁队列里槽位在被认领到被写入之间确实是未初始化的。`MaybeUninit<T>` 显式表达「可能未初始化」，`UnsafeCell<T>` 则告诉编译器「这个值可能被别名共享可变地访问」，二者组合才能合法地表达「先占位、后填值」。

**练习 2**：`wait_write` 用的是 `snooze()` 而不是 `spin()`，这两者（在 crossbeam `Backoff` 语义里）有什么区别？

答案：`Backoff::spin()` 前期是纯忙等（`cpu_relax`），适合「几乎立刻就能成功」的场景；`Backoff::snooze()` 在忙等若干轮后会逐步退避到 `yield_now`，适合「可能要等一小会儿」的场景。`wait_write` 预期等待时间略长于一次 CAS，所以选 `snooze()` 更温和。

---

### 4.3 Block\<T\>：链表节点与 new / wait_next / destroy

#### 4.3.1 概念说明

`Block` 是链表里的一个节点，内部固定含 `BLOCK_CAP = 63` 个 `Slot`。整个 `Injector` 就是一串 `Block` 串成的单向链表：`head` 和 `tail` 各持有一个 `Position`（指向某个 block + 某个 index），生产者从 tail 端往后追加，消费者从 head 端往前取走；当一个 block 的所有槽都被消费完，就切换到 `block.next`，并按需销毁旧 block。

`Block` 提供三个方法，分别对应链表节点的三种操作：

- `new()`：分配并零初始化一个新块（所有 `Slot::state == 0`）。
- `wait_next()`：自旋等待「下一个 block 被安装」，返回 `next` 指针。
- `destroy()`：协作式销毁——只有确认没有其他线程还在用这个 block 的任何槽时，才真正释放内存。

#### 4.3.2 核心流程

**块与块的链接**：`Block` 只有一个 `next: AtomicPtr<Block<T>>` 指向下一个块。生产者在某个 block 写满（offset 到达 62）时，预分配下一个 block，CAS 成功后通过 `(*block).next.store(next_block, Release)` 把它挂上去。消费者跨块时用 `wait_next` 读取这个指针。

**协作销毁**：`destroy` 不能「一看 block 用完就 `free`」，因为别的线程可能还持有这个 block 的指针、正在读某个槽。它采用「标记 + 检查」：从后往前遍历槽位，对每个尚未 `READ` 的槽 `fetch_or(DESTROY)`；如果发现某个槽既没 `READ` 又被自己标上了 `DESTROY`，说明有线程正卡在那个槽上（很可能在 `wait_write`），那就**提前返回，把销毁责任交给那个线程**；否则全部检查通过，才 `drop(Box::from_raw(this))` 真正释放。这个机制的完整并发分析留到 u3-l3，本讲只看它的数据结构职责。

#### 4.3.3 源码精读

`Block` 结构定义在 [src/deque.rs:1232-1241](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1232-L1241)：

```rust
/// A block in a linked list.
///
/// Each block in the list can hold up to `BLOCK_CAP` values.
struct Block<T> {
    /// The next block in the linked list.
    next: AtomicPtr<Block<T>>,

    /// Slots for values.
    slots: [Slot<T>; BLOCK_CAP],
}
```

`Block::new` 在 [src/deque.rs:1254-1269](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1254-L1269)，用 `Global.allocate_zeroed` 一次性零分配整块内存（含 63 个 `Slot`），并附带了详尽的 SAFETY 注释说明为什么零初始化是安全的：

```rust
fn new() -> Box<Self> {
    // unsafe { Box::new_zeroed().assume_init() } requires Rust 1.92
    match Global.allocate_zeroed(Self::LAYOUT) {
        Some(ptr) => {
            // SAFETY: ...
            //  [3] `Slot::task` (UnsafeCell) may be safely zero initialized because it
            //       holds a MaybeUninit.
            //  [4] `Slot::state` (AtomicUsize) may be safely zero initialized.
            unsafe { Box::from_raw(ptr.as_ptr().cast()) }
        }
        None => handle_alloc_error(Self::LAYOUT),
    }
}
```

`wait_next` 在 [src/deque.rs:1271-1281](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1271-L1281)，自旋等待 `next` 指针被生产者写入：

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

`destroy` 在 [src/deque.rs:1283-1301](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1283-L1301)，从后往前扫描槽位，做协作销毁判定：

```rust
unsafe fn destroy(this: *mut Self, count: usize) {
    for i in (0..count).rev() {
        let slot = unsafe { (*this).slots.get_unchecked(i) };
        // Mark the `DESTROY` bit if a thread is still using the slot.
        if slot.state.load(Ordering::Acquire) & READ == 0
            && slot.state.fetch_or(DESTROY, Ordering::AcqRel) & READ == 0
        {
            return; // 有线程仍在用这个槽，交由它继续销毁
        }
    }
    drop(unsafe { Box::from_raw(this) }); // 无人占用，真正释放
}
```

> 这段只是「数据结构层面」的展示；`count` 取什么值、谁在什么时候调用 `destroy`、为什么不会 double-free，属于 `steal` 的并发流程，留到 u3-l3 精读。

#### 4.3.4 代码实践

**实践目标**：在注释里推演 `destroy` 的「协作移交」语义。

**操作步骤**：

1. 假设一个 block 的 63 个槽里，第 10 号槽正被线程 B 在 `wait_write` 里等待（`state` 既无 `WRITE` 也无 `READ`），其余槽都已被 `READ`。
2. 线程 A 调用 `destroy(this, 63)`，从 `i = 62` 往前扫到 `i = 10`。
3. 在 `i = 10` 处执行 `load(Acquire) & READ == 0`（为真）且 `fetch_or(DESTROY) & READ == 0`（为真），于是 `return`。

**需要观察的现象**：A 提前返回，没有真正 `drop` 这个 block；B 此后从 `wait_write` 醒来时，会检测到 `DESTROY` 位并接管销毁。

**预期结果**：你能解释清楚——`destroy` 的设计保证「同一时刻只有一个线程会真正执行 `Box::from_raw`」，从而避免 double-free，同时让正在用槽的线程不会被抢走内存。

> 待本地验证：该流程的真实调度见 u3-l3 的 `steal` 精读；本讲只需在源码注释里把上述故事写清楚。

#### 4.3.5 小练习与答案

**练习 1**：`Block::new` 为什么用 `allocate_zeroed` 而不是 `Box::new(Block{ next: AtomicPtr::new(null), slots: [???] })`？

答案：`slots` 是 63 个 `Slot` 的数组，逐个构造需要为每个 `Slot::task`（`MaybeUninit`）写初始化代码、且 `MaybeUninit` 并不能直接用 `Default` 批量构造。零分配一次性把 `next`（null 指针）和所有 `state`（0）置成合法初值，而 `task` 本就允许未初始化（`MaybeUninit`），所以零初始化既快又安全。

**练习 2**：`wait_next` 和 `Slot::wait_write` 都用 `Backoff::snooze()`，它们等待的事件分别是什么？

答案：`wait_write` 等的是「本槽位的 `WRITE` 位被置上」（生产者写完值）；`wait_next` 等的是「本 block 的 `next` 指针被填上」（生产者安装了下一个 block）。两者都是「生产者还没来得及发布」的短暂窗口，所以都用 `snooze` 轻量退避。

---

### 4.4 Position\<T\> 与 Injector\<T\>：双游标 + 块指针的链表块队列骨架

#### 4.4.1 概念说明

有了 `Slot` 和 `Block`，最后需要一个「全局视图」把它们组织成队列。`Injector` 采用和 Chase-Lev 队列**同样的双游标思想**（`head`/`tail`），但每个游标不再只是一个整数，而是一个 `Position`——同时持有「逻辑 `index`」和「当前所在 `block` 的指针」。

为什么要给游标配一个 `block` 指针？因为索引虽然能编码出 `offset` 和 `lap`，但要从某个 `index` 找到它对应的 `Block` 节点，必须从链表头走 `lap` 步——太慢。所以 `Position` 直接缓存「当前关注的那个 block 指针」，跨块时再切换。

`Injector` 因此是：

```text
Injector { head: Position, tail: Position }
Position { index: AtomicUsize, block: AtomicPtr<Block> }
```

- `tail`：生产者写入位置（FIFO 末尾）。
- `head`：消费者读取位置（FIFO 头部）。
- 两个 `Position` 都用 `CachePadded` 包裹，避免 head/tail 落在同一缓存行上引起伪共享（生产者频繁写 tail、消费者频繁写 head）。

#### 4.4.2 核心流程

**初始化**：`Injector::new` 会**预先分配第一个 block**，并让 `head` 和 `tail` 都指向它、`index` 都为 0。所以一个新建的空 `Injector` 已经有一个（空的）block 挂在上面。

**生产者大致流程**（u3-l2 精读，这里只看数据结构如何被使用）：

1. 读 `tail.index` 与 `tail.block`，算 `offset`。
2. 若 `offset == BLOCK_CAP`，说明当前 block 已满、需要等下一个 block 安装好（`snooze`）。
3. 若 `offset + 1 == BLOCK_CAP`（即将写满），提前 `Block::new()` 预分配下一个块。
4. CAS 推进 `tail.index`；若写满，就把预分配的块挂到 `block.next`、并把 `tail.block` 切到新块。
5. 写槽位、置 `WRITE` 位。

**消费者大致流程**（u3-l3 精读）：读 `head`，算 `offset`，按 `HAS_NEXT` 判空或切换 `head.block` 到 `next`，读槽位、置 `READ` 位，并在块尾调用 `Block::destroy`。

#### 4.4.3 源码精读

`Position` 定义在 [src/deque.rs:1304-1311](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1304-L1311)：

```rust
/// A position in a queue.
struct Position<T> {
    /// The index in the queue.
    index: AtomicUsize,

    /// The block in the linked list.
    block: AtomicPtr<Block<T>>,
}
```

`Injector` 结构与文档在 [src/deque.rs:1313-1341](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1313-L1341)：

```rust
/// An injector queue.
///
/// This is a FIFO queue that can be shared among multiple threads. Task schedulers typically have
/// a single injector queue, which is the entry point for new tasks.
pub struct Injector<T> {
    /// The head of the queue.
    head: CachePadded<Position<T>>,

    /// The tail of the queue.
    tail: CachePadded<Position<T>>,

    /// Indicates that dropping a `Injector<T>` may drop values of type `T`.
    _marker: PhantomData<T>,
}
```

注意 `_marker: PhantomData<T>`（与 `Worker` 的 `PhantomData<*mut ()>` 不同！）。`Worker` 用 `*mut ()` 把自己拉成 `!Send + !Sync`（单线程私有）；而 `Injector` 用 `PhantomData<T>`，并在 [src/deque.rs:1343-1344](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1343-L1344) 显式实现 `Send + Sync`：

```rust
unsafe impl<T: Send> Send for Injector<T> {}
unsafe impl<T: Send> Sync for Injector<T> {}
```

也就是说 `Injector<T>` 在 `T: Send` 时是 `Send + Sync`，可以跨线程共享 `&Injector`——这正是「全局共享入口」所需。

`Default` 实现预分配第一个 block，在 [src/deque.rs:1346-1361](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1346-L1361)：

```rust
impl<T> Default for Injector<T> {
    fn default() -> Self {
        let block = Box::into_raw(Block::<T>::new());
        Self {
            head: CachePadded::new(Position {
                block: AtomicPtr::new(block),
                index: AtomicUsize::new(0),
            }),
            tail: CachePadded::new(Position {
                block: AtomicPtr::new(block),
                index: AtomicUsize::new(0),
            }),
            _marker: PhantomData,
        }
    }
}
```

`new()` 只是转发给 `default()`，见 [src/deque.rs:1373-1375](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1373-L1375)。

#### 4.4.4 代码实践

**实践目标**：解释一个 `tail` 索引在跨 block 时为何会短暂出现 `offset == BLOCK_CAP`，并画出 `block.next` 的链接关系。

**操作步骤**：

1. 设 `SHIFT = 1`，`LAP = 64`，`BLOCK_CAP = 63`。假设初始 `tail = 2 << SHIFT = 4`（即 `logical = 2, offset = 2`），当前 block 记为 `B0`。
2. 每次成功 push，`tail` 增加 `1 << SHIFT = 2`，`offset` 随之 `+1`。
3. 当某次 push 读到 `offset = 62` 时（block `B0` 的最后一个可写槽，即「第 63 个槽位」），命中 [src/deque.rs:1408](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1408) 的 `offset + 1 == BLOCK_CAP`，于是**预分配** `B1 = Block::new()`。
4. CAS 把 `tail.index` 推进到 `new_tail`（其 `offset = 63 == BLOCK_CAP`）——**这就是「offset 等于 BLOCK_CAP」的瞬间**。
5. CAS 成功后进入 [src/deque.rs:1421-1430](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1421-L1430) 的安装分支：把 `B1` 挂为 `B0.next`，把 `tail.block` 切到 `B1`，并把 `tail.index` 重写为 `next_index`（其 `offset = 0, lap = 1`）。
6. 与此同时，若有另一个生产者线程在步骤 4 与步骤 5 之间读到 `tail.index`，它会算出 `offset == BLOCK_CAP`，从而命中 [src/deque.rs:1399-1404](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1399-L1404) 的等待分支 `backoff.snooze()`，**直到步骤 5 把 `tail.index` 更新为新块**才继续。

**block.next 链接关系图**：

```text
        tail.block                    head.block
            |                              |
            v                              v
   +----+ next   +----+ next   +----+ next   +----+
   | B0 |----->  | B1 |----->  | B2 |----->  | B3 |-----> null
   +----+        +----+        +----+        +----+
   (已满/         (tail         (中间)        (head
    部分消费)      所在)                        所在)
```

要点：

- 每个 block 写满 63 个槽后，生产者**预分配**下一个 block 并用 `(*block).next.store(next_block, Release)` 挂上，链表只增不减。
- `tail.block` 总是指向「当前正在写入」的块；`head.block` 总是指向「当前正在消费」的块。
- 消费者跨块时用 `Block::wait_next` 读取 `next`，切到新块后，旧块通过 `Block::destroy` 协作回收（u3-l3）。

**需要观察的现象**：`offset == BLOCK_CAP` 是一个**瞬态**——它只在「CAS 推进 tail」与「安装下一个 block 并更新 tail.index」之间存在。任何线程在这个窗口里读到它，都会进入 `snooze` 自旋，而不是去写一个根本不存在的槽位。

**预期结果**：你能解释清楚「为什么 offset 会等于 BLOCK_CAP」——它不是某个真实槽位的编号，而是「这个 block 已经写满、正在切换到下一个 block」的边界哨兵；并且能画出 `B0 → B1 → B2 → ...` 的单向链表。

> 待本地验证：真实多线程下这个瞬态窗口极短，难以用断言直接捕获；建议结合 u3-l2 的 `push` 精读在源码注释里走一遍单线程序列。

#### 4.4.5 小练习与答案

**练习 1**：`Injector` 的 `head`/`tail` 为什么都用 `CachePadded<Position<T>>` 包裹？

答案：生产者频繁写 `tail.index`/`tail.block`，消费者频繁写 `head.index`/`head.block`。若 head 和 tail 落在同一缓存行，两个核会反复互相失效对方的缓存行（伪共享），拖慢性能。`CachePadded` 把各自填充到独立缓存行，避免这种争用。

**练习 2**：`Injector` 用 `PhantomData<T>` 而 `Worker` 用 `PhantomData<*mut ()>`，这两个选择各自的目的？

答案：`Worker` 想「禁止跨线程共享」，于是用 `*mut ()` 把 auto trait 默认拉成 `!Send + !Sync`，再只手动开 `Send`，实现「单线程私有」。`Injector` 想「允许跨线程共享」，于是用 `PhantomData<T>`（不污染 auto trait），再显式 `unsafe impl<T: Send> Send/Sync`，只要任务 `T: Send` 就能在多线程间共享 `&Injector`。

**练习 3**：新建一个 `Injector::new()` 时，`head` 和 `tail` 是否指向同一个 block？`index` 分别是多少？

答案：是，都指向同一个预分配的 `B0`，`index` 都是 `0`。此时队列为空，第一个 `push` 会在 `B0` 的 offset 0 写入。

## 5. 综合实践

把本讲的四个积木串起来，做一次「数据结构自检」：

1. **画一张完整的 Injector 结构图**：包含 `Injector` →（`head`, `tail`）两个 `CachePadded<Position>` → 每个 `Position` 含 `index: AtomicUsize` 与 `block: AtomicPtr<Block>` → 至少画 3 个 `Block` 组成的链表 → 每个 `Block` 含 `next: AtomicPtr` 与 `slots: [Slot; 63]` → 每个 `Slot` 含 `task: UnsafeCell<MaybeUninit<T>>` 与 `state: AtomicUsize`。在图上标出 `WRITE/READ/DESTROY` 三位、`LAP=64`/`BLOCK_CAP=63`/`SHIFT=1`/`HAS_NEXT=1` 四常量。

2. **手动模拟一次跨块**：假设 `B0` 已写入 62 个任务（offset 0..61 已消费、offset 62 待写），现在连续 push 两个任务。写出：
   - 第 1 个 push 后 `tail.index` 的原始值、`offset`、`lap`，以及 `B0.next` 是否被设置、指向谁。
   - 第 2 个 push 时读到的 `tail.index` 解码出的 `offset` 和 `lap`，确认它落在新 block。

3. **对照源码核验**：把你手算的结果与 [src/deque.rs:1388-1446](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1388-L1446) 的 `push` 实现逐行对照，确认你的心智模型与代码一致。

这个练习把「索引编码 → Slot 状态 → Block 链表 → Position/Injector」四件事一次性串起来，为 u3-l2（`push` 完整算法）和 u3-l3（`steal` + `destroy` 协作）打好地基。

## 6. 本讲小结

- `Injector` 与 `Worker/Stealer` 的实现**完全不同**：前者是 `Block` 单向链表 + `head`/`tail` 两个 `Position`，后者是单一环形 `Buffer`。
- 索引在一个 `usize` 里打包了「逻辑位置（`>> SHIFT`）」「块内偏移（`% LAP`）」「第几圈（`/ LAP`）」「是否有后继块（`& HAS_NEXT`）」四类信息。
- `LAP = 64`、`BLOCK_CAP = 63`：每圈 64 个逻辑索引留出 1 个（offset == 63）当块边界哨兵，故每块实际可写 63 个任务。
- `Slot` 用 `UnsafeCell<MaybeUninit<T>>` 存任务、用 `AtomicUsize` 的 `WRITE/READ/DESTROY` 三位标记状态；`wait_write` 用 `Acquire` 加载 + `Backoff::snooze` 自旋等待 `WRITE` 位。
- `Block` 提供 `new`（零分配整块）、`wait_next`（等 `next` 指针）、`destroy`（协作销毁，保证不 double-free）三个方法。
- `Injector` 用 `PhantomData<T>` + 显式 `unsafe impl Send/Sync`，在 `T: Send` 时可跨线程共享，与单线程私有的 `Worker` 形成鲜明对比。

## 7. 下一步学习建议

- **u3-l2 Injector::push**：本讲把 `push` 当黑盒，下一讲会逐行拆解它的 CAS 循环、`backoff.spin` 重试、预分配优化与跨块安装的内存序。
- **u3-l3 Injector::steal 与 Block::destroy**：本讲只介绍了 `destroy` 的「职责」，下一讲会从 `steal` 视角完整分析 `HAS_NEXT` 判空、`wait_next` 切换 head.block，以及协作销毁为何不会 double-free。
- **横向对比**：回头重读 u2-l1 的 `Buffer`/`Inner`，对照本讲的 `Slot`/`Block`/`Position`/`Injector`，体会「固定容量环形缓冲区」与「无限增长链表块」两种无锁队列设计在数据结构层面的取舍。
