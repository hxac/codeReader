# SegQueue 的分块链表结构：Block、Slot 状态位与 Position

## 1. 本讲目标

本讲是 SegQueue 系列的第一篇，**只讲数据结构，不讲 push/pop 主链路**。读完本讲你应该能够：

- 说清 `SegQueue` 为什么用「块链表（linked list of segments）」来实现无界队列，整体长什么样。
- 画出 `Slot<T>` 与 `Block<T>` 的字段构成，解释 `next` 指针和 `slots` 数组的作用。
- 准确说出 `WRITE` / `READ` / `DESTROY` 三个状态位的数值与含义。
- 推导 `LAP` / `BLOCK_CAP` / `SHIFT` / `HAS_NEXT` 四个常量为何这样取值，并能拆解一个 `index` 的每一比特代表什么。
- 解释 `Position<T>` 与 `SegQueue<T>` 的字段，以及 `SegQueue::new()` 创建出的初始空队列长什么样。

push / pop / 块的销毁协调等内容分别放在 u3-l2 与 u3-l3，本讲不展开。

## 2. 前置知识

本讲假设你已经读过 u2-l1（ArrayQueue 的 stamp/lap/Slot 模型）。我们会复用其中几个概念：

- **链表（linked list）**：节点之间用指针串起来，可以随用随长。SegQueue 就是用链表来实现「无界」的。
- **原子类型**：`AtomicUsize`、`AtomicPtr<T>`。多个线程并发读写同一个字段时，必须用原子操作才不会撕裂。
- **`MaybeUninit<T>` 与 `UnsafeCell`**：让一块内存「可能尚未初始化」，由代码自己负责写/读的时序。ArrayQueue 的 `Slot<T>` 已经用过同样的套路。
- **`CachePadded`**：把一个字段填充到一整个缓存行，避免两个线程高频改动的字段落在同一缓存行上产生「伪共享」。ArrayQueue 用它包了 `head`/`tail`，SegQueue 也一样。
- **位运算**：`<<`（左移）、`>>`（右移）、`&`（按位与）、`|`（按位或）。本讲会大量用到「对 2 的幂取模等价于按位与」这一技巧：当 \(n\) 是 2 的幂时，\(x \bmod n = x \mathbin{\&} (n-1)\)。

如果你对 ArrayQueue 里「把 index 和 lap 编码进同一个 `usize`」的做法还有印象，那么本讲你会看到非常相似的设计——只是 ArrayQueue 在一个固定数组里转圈，SegQueue 在一条链表上往前走。

## 3. 本讲源码地图

本讲只精读一个文件：

| 文件 | 作用 |
| --- | --- |
| `src/seg_queue.rs` | SegQueue 的全部实现：常量、`Slot<T>`、`Block<T>`、`Position<T>`、`SegQueue<T>` 及其方法。 |

该文件内本讲关注的区域：

- 常量定义（`WRITE/READ/DESTROY`、`LAP/BLOCK_CAP/SHIFT/HAS_NEXT`）
- `Slot<T>` 结构与 `wait_write`
- `Block<T>` 结构、`LAYOUT`、`new`、`wait_next`（`destroy` / `destroy_mut` 留到 u3-l3）
- `Position<T>` 结构
- `SegQueue<T>` 结构、`new`、`Send/Sync`、`UnwindSafe`

此外会顺带提到 `Block::new` 依赖的 `src/alloc_helper.rs`（零初始化堆分配原语），其深讲放在 u4-l5。

## 4. 核心概念与源码讲解

在进入字段之前，先用一句话建立整体直觉：**`SegQueue` 是一条单向链表，链表上的每个节点叫一个 block（块/段），每个 block 是一个能装 31 个元素的小数组。生产者在链表尾追加元素、装满了就新挂一个 block；消费者从链表头取元素、取空了的旧 block 就被回收。** 因为链表可以无限长，所以队列「无界」；因为 block 要现用现分配，所以它比一次性预分配好的 `ArrayQueue` 略慢。

下面按「槽 → 块 → 常量与位编码 → 队列外壳」的顺序自底向上拆。

### 4.1 Slot<T> 与 WRITE/READ/DESTROY 状态位

#### 4.1.1 概念说明

`Slot<T>` 是最小的存储单元，装「一个元素 + 它当前的状态」。和 ArrayQueue 的 `Slot` 用一个 `stamp` 当状态机不同，SegQueue 的 `Slot` 用一组**布尔状态位**来描述自己：

- **WRITE**：这个槽里**已经写入了**一个值（生产者可见）。
- **READ**：这个槽里的值**已经被读走**了（消费者可见）。
- **DESTROY**：这个 block **正在被销毁**（用于块的内存回收协调，u3-l3 详讲）。

为什么要把「是否写了」「是否读了」做成位？因为同一个 `state` 字段（一个 `AtomicUsize`）要同时承载这几件事，用不同的比特位就能用一次 `fetch_or` 同时读旧值、写新值，既省内存又省原子操作。

#### 4.1.2 核心流程

一个槽的生命周期（仅数据视角，不含并发协调细节）：

```
state = 0                 # 新建/零初始化：空槽，尚无值
   | 生产者写入 value，再 fetch_or(WRITE)
   v
state = WRITE             # 已写入，等待消费者来读
   | 消费者读出 value，再 fetch_or(READ)
   v
state = WRITE | READ      # 已读：该槽的使命完成，等块回收
```

`DESTROY` 位由销毁块的逻辑按需 OR 进去，与上面两条主线正交，本讲先记住「它是一个可以叠加的标志位」即可。

#### 4.1.3 源码精读

状态位常量定义在文件开头：

[seg_queue.rs:17-23](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L17-L23) —— 定义 `WRITE=1`、`READ=2`、`DESTROY=4`，分别是 bit0、bit1、bit2。注释说明了每个位的语义。

```rust
const WRITE: usize = 1;
const READ: usize = 2;
const DESTROY: usize = 4;
```

`Slot<T>` 的结构非常精简：

[seg_queue.rs:34-41](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L34-L41) —— `value` 用 `UnsafeCell<MaybeUninit<T>>` 装「可能未初始化」的值，`state` 是一个原子状态字。

```rust
struct Slot<T> {
    value: UnsafeCell<MaybeUninit<T>>,
    state: AtomicUsize,
}
```

> 注意与 ArrayQueue 的区别：ArrayQueue 的 `Slot` 用 `stamp`（一个不断累加的戳）当状态机；SegQueue 的 `Slot` 没有 stamp，只用三个布尔位。这是因为 SegQueue 的「第几圈」信息编码在全局 `index` 里（见 4.3），不需要每个槽自己记。

`wait_write` 是消费者会用到的一个辅助方法，作用是**自旋等待直到生产者把 WRITE 位写上**：

[seg_queue.rs:43-51](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L43-L51) —— 循环 `load(state)`，只要 `& WRITE == 0`（还没写入）就用 `Backoff::snooze()` 让出 CPU 等一等；一旦看到 WRITE 位，函数返回，调用方就可以安全读值了。

```rust
fn wait_write(&self) {
    let backoff = Backoff::new();
    while self.state.load(Ordering::Acquire) & WRITE == 0 {
        backoff.snooze();
    }
}
```

#### 4.1.4 代码实践

**源码阅读型实践：手算状态值。**

1. 实践目标：通过状态位的位运算，验证你对三个位的理解。
2. 操作步骤：
   - 设想一个槽经历了「写入 → 读出」两步。写下每步之后 `state` 的十进制值。
   - 再设想在「已写入但未读」时，销毁逻辑对它 `fetch_or(DESTROY)`。写下此时 `state` 的值。
3. 需要观察的现象：状态值就是被置位的那些常数之和。
4. 预期结果：
   - 初始：`0`。
   - 写入后：`WRITE = 1`。
   - 读出后：`WRITE | READ = 1 | 2 = 3`。
   - 写入未读 + DESTROY：`WRITE | DESTROY = 1 | 4 = 5`。
5. 这个小练习确认了：三个位互不冲突，可以任意叠加。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `state` 要用 `AtomicUsize` 而不是普通 `usize`？

**参考答案**：因为同一个槽会被多个线程并发访问——生产者写值后要 `fetch_or(WRITE)`，消费者读完要 `fetch_or(READ)`，销毁逻辑又要 `fetch_or(DESTROY)`。普通 `usize` 的读改写在并发下会撕裂丢失更新，必须用原子操作把「读旧值 → 改位 → 写回」做成不可分割的一步。

**练习 2**：`wait_write` 用的是 `Ordering::Acquire`，能用 `Relaxed` 吗？

**参考答案**：不能随意换。生产者是先 `write(value)` 再用 `Release` 序置 WRITE 位（见 u3-l2）；消费者用 `Acquire` 读 state，才能与生产者的 `Release` 配对，保证「看到 WRITE 位 = 一定看得到之前写入的 value」。换成 `Relaxed` 会丢失这层同步，可能读到未初始化的内存。完整论证放在 u4-l1。

---

### 4.2 Block<T>：链表节点与 slots 数组

#### 4.2.1 概念说明

`Block<T>` 是链表上的一个节点，承担两件事：

1. **装最多 31 个元素**（`slots: [Slot<T>; BLOCK_CAP]`，`BLOCK_CAP = 31`）。
2. **指向下一个 block**（`next: AtomicPtr<Block<T>>`），把所有 block 串成单向链表。

为什么是「小块 + 链表」而不是「一个超大数组」？因为队列无界——你无法预先知道要装多少元素，只能随用随长。每挂一个新 block 就 `Box` 分配一次（31 个槽），分配粒度小、开销可控；旧 block 整块读完后整块回收，内存占用随实际积压元素数线性增长，不会无限膨胀。

#### 4.2.2 核心流程

block 在链表中的角色（仅结构，不含分配时序）：

```
head.block                       tail.block
    │                                 │
    v                                 v
 [Block 0] --next--> [Block 1] --next--> [Block 2] --next--> null
  31 slots            31 slots           31 slots
 (正在被消费)         (积压中)            (正在被生产)
```

- `head.block` 指向消费者当前在读的块。
- `tail.block` 指向生产者当前在写的块。
- `next` 在块写满、需要新块时由生产者安装；安装前 `next == null`，消费者若跑到块尾会自旋等待（`wait_next`）。

#### 4.2.3 源码精读

`Block<T>` 的字段：

[seg_queue.rs:53-62](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L53-L62) —— `next` 是指向下一个块的原子指针；`slots` 是固定长度 `BLOCK_CAP`（=31）的槽位数组。

```rust
struct Block<T> {
    next: AtomicPtr<Block<T>>,
    slots: [Slot<T>; BLOCK_CAP],
}
```

`Block::new` 用「零初始化堆分配」一次性建好整个块：

[seg_queue.rs:64-90](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L64-L90) —— `LAYOUT` 在 `const` 上下文里断言块大小非零；`new` 调 `Global::allocate_zeroed` 拿到一块全 0 的堆内存，再 `Box::from_raw` 包成 `Box<Self>`。注释 `[1]~[4]` 逐字段论证了「全 0」为何对每个字段都是合法初值。

```rust
fn new() -> Box<Self> {
    match Global.allocate_zeroed(Self::LAYOUT) {
        Some(ptr) => unsafe { Box::from_raw(ptr.as_ptr().cast()) },
        None => handle_alloc_error(Self::LAYOUT),
    }
}
```

为什么全 0 安全？对照字段看（注释 [1]–[4]）：

- `next = null`：合法，表示「还没有下一个块」。
- 每个 `Slot` 的 `state = 0`：合法，表示「空槽、未写未读」。
- 每个 `Slot` 的 `value` 是 `MaybeUninit<T>`：`MaybeUninit` 的内存可以是任意比特（包括全 0），因为它本来就代表「尚未初始化」。

`allocate_zeroed` 来自 `src/alloc_helper.rs`：

[alloc_helper.rs:44-46](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/alloc_helper.rs#L44-L46) —— `Global::allocate_zeroed` 在 `alloc_impl` 里转发到 `alloc::alloc::alloc_zeroed(layout)`，返回一块清零内存；分配失败返回 `None`，由调用方 `handle_alloc_error` 兜底（abort）。

`wait_next` 是消费者跑到块尾时的等待辅助：

[seg_queue.rs:93-102](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L93-L102) —— 循环 `load(next)`，只要还是 `null` 就 `snooze()` 让出，直到生产者把下一个块的指针装上。

> 本讲只看结构。`destroy` / `destroy_mut`（块销毁与 DESTROY 位协调）放在 u3-l3。

#### 4.2.4 代码实践

**源码阅读型实践：核对块的容量与零初始化安全性。**

1. 实践目标：确认每个 block 装 31 个槽，并验证 `allocate_zeroed` 对所有字段都安全。
2. 操作步骤：
   - 在源码里找到 `BLOCK_CAP` 的定义（4.3 会精读），确认 `slots` 数组长度为 31。
   - 对照 `Block::new` 的 SAFETY 注释 `[1]~[4]`，把每个字段（`next`、`slots`、`Slot.value`、`Slot.state`）对应到一条「全 0 合法」的理由。
3. 需要观察的现象：四个字段都能在「全 0」下处于合法初始态，没有一个是必须非零才能用的。
4. 预期结果：你会得出「正因为每个字段都容忍全 0，才能用一次 `allocate_zeroed` 代替逐个 `Default` 初始化，省去 31 个槽的循环初始化」这一结论。
5. 思考延伸：如果 `T` 是 `Box<u32>` 这样的「指针型」类型，`MaybeUninit` 全 0 会不会被误当成「已初始化的空指针」？答案是不会——代码靠 `state` 位的 WRITE 来判断是否已初始化，从不靠 `value` 本身的比特模式，所以 `T` 是什么类型都不影响安全性。

#### 4.2.5 小练习与答案

**练习 1**：`next` 为什么是 `AtomicPtr` 而不是 `Option<Box<Block<T>>>`？

**参考答案**：因为「安装下一个块」是并发操作——某个生产者负责 CAS/写入 `next`，其他线程（生产者和消费者）会并发地 `load(next)` 来读它。`Box` 的普通字段无法被多线程并发读写；用 `AtomicPtr` 才能安全地「一个线程装指针、多个线程读指针」。另外 `next` 初始必须是 `null`（而非 `Some`），原子指针的零初始化天然就是 `null`。

**练习 2**：`LAYOUT` 里的 `assert!(layout.size() != 0, ...)` 是在防什么？

**参考答案**：零大小类型的布局在 Rust 分配器里有特殊语义（`alloc` 对 ZST 返回的指针可以是 dangling）。这个断言确保 `Block<T>` 永远不是 ZST——由于它含一个 `AtomicPtr` 字段和一个非空数组，大小必然非零，断言只是把这一不变量显式化，让后续 `allocate_zeroed` / `Box::from_raw` 的安全性论证更清晰。

---

### 4.3 LAP / BLOCK_CAP / SHIFT / HAS_NEXT 常量与 index 的位编码

这是本讲的核心，也是本讲的实践任务所在。四个常量共同决定了一个全局 `index` 如何被拆成「块号 + 块内偏移 + 元数据位」。

#### 4.3.1 概念说明

SegQueue 没有 ArrayQueue 那种「每个槽自己记 stamp」的做法，而是把**整个队列的进度编码在两个全局 `index` 里**（`head.index` 和 `tail.index`）。一个 `index` 是个 `usize`，它的不同比特位承担不同职责：

- 最低位（bit 0）是一个**元数据位** `HAS_NEXT`：标记「当前块是否还有后继块」。
- 中间几比特编码**块内偏移**：当前在 block 内的第几个槽。
- 高位比特编码**块号（lap）**：当前在第几个 block。

为此定义了四个常量：

| 常量 | 值 | 作用 |
| --- | --- | --- |
| `LAP` | 32 | 一个 block 覆盖的一「圈」逻辑位置数；必须是 2 的幂。 |
| `BLOCK_CAP` | `LAP - 1 = 31` | 一个 block 实际能装的元素数。 |
| `SHIFT` | 1 | 为元数据保留的低位比特数。 |
| `HAS_NEXT` | 1 | 标记「当前 block 不是最后一个」的位掩码。 |

#### 4.3.2 核心流程：index 的位布局

给定一个 `index`，代码用两个公式拆解（来自 push/pop，u3-l2 详讲）：

\[
\text{offset} = (\text{index} \gg \text{SHIFT}) \bmod \text{LAP}
\]

\[
\text{lap} = (\text{index} \gg \text{SHIFT}) \,/\, \text{LAP}
\]

因为 `LAP = 32 = 2^5`、`SHIFT = 1`，这两个公式等价于位运算：

\[
\text{offset} = (\text{index} \gg 1) \mathbin{\&} 31
\]

\[
\text{lap} = \text{index} \gg 6
\]

所以一个 `index` 的比特布局是：

| 比特位 | 含义 | 取值 / 说明 |
| --- | --- | --- |
| **bit 0** | `HAS_NEXT`（被 `SHIFT` 保留的元数据位） | `0`：当前块无后继或尚未确认；`1`：当前块有后继块。 |
| **bit 1 – bit 5**（5 个比特） | **块内偏移 offset** | `(index >> 1) & 31`，范围 \(0..31\)。其中 `0..30` 对应 `slots[0..30]` 共 31 个真实槽；`31` 是块边界缝（见下文）。 |
| **bit 6 及以上** | **块号 lap** | `index >> 6`，标识元素属于链表里的第几个 block。 |

注意每次 push/pop 让 `index` 前进的不是 `1`，而是 `1 << SHIFT = 2`——这样最低位（`HAS_NEXT`）在正常前进时保持不变，只有需要标记「换块」时才被显式置位。

#### 4.3.3 源码精读

四个常量的定义：

[seg_queue.rs:25-32](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L25-L32) —— 注释点明：每个 block 覆盖一「lap」；`BLOCK_CAP` 是块能装的最大元素数；`SHIFT` 是为元数据保留的低位数；`HAS_NEXT` 标记「块不是最后一个」。

```rust
const LAP: usize = 32;
const BLOCK_CAP: usize = LAP - 1;
const SHIFT: usize = 1;
const HAS_NEXT: usize = 1;
```

这些常量在 push/pop 里被这样使用（本讲只看「怎么拆 index」，主链路留到 u3-l2）：

[seg_queue.rs:221-230](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L221-L230) —— `push` 里计算 `offset = (tail >> SHIFT) % LAP`，并在 `offset == BLOCK_CAP`（即 31）时进入「块边界」处理分支。

```rust
let offset = (tail >> SHIFT) % LAP;
if offset == BLOCK_CAP {
    // 到达块边界，等待/安装下一个块
    ...
}
```

[seg_queue.rs:381-396](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L381-L396) —— `pop` 里检查 `new_head & HAS_NEXT == 0` 时，用 `(head >> SHIFT) / LAP != (tail >> SHIFT) / LAP` 判断 head 与 tail 是否已落在不同块，若是则把 `HAS_NEXT` 位置 1。

```rust
let mut new_head = head + (1 << SHIFT);
if new_head & HAS_NEXT == 0 {
    ...
    if (head >> SHIFT) / LAP != (tail >> SHIFT) / LAP {
        new_head |= HAS_NEXT;
    }
}
```

#### 4.3.4 代码实践（本讲指定实践任务）

**纸笔推导型实践：推算 `BLOCK_CAP = LAP - 1 = 31`，并拆解 `index` 各比特。**

1. 实践目标：亲手推出「为何每块 31 个槽」以及「一个 index 的每一比特代表什么」。
2. 操作步骤与问题：
   - **(a) 为什么 `LAP` 取 32 而不是 31？** 提示：`offset` 要用 `% LAP` 和 `/ LAP` 提取。当 `LAP` 是 2 的幂时，`% LAP` 编译成 `& (LAP-1)`、`/ LAP` 编译成右移，都是单条快速指令。所以 `LAP` 必须是 2 的幂，取 32。
   - **(b) 为什么 `BLOCK_CAP = LAP - 1 = 31`，而不是直接 32？** 提示：`offset` 取值范围是 \(0..32\)（共 32 个值）。其中一个值（`offset == 31`，即 `BLOCK_CAP`）被用作**块边界缝**——它是 tail 在「安装下一个块」这个多步操作期间的瞬态位置，其他线程看到 `offset == BLOCK_CAP` 就知道要等一等。既然 32 个 offset 里要留 1 个当缝，真实槽就只剩 31 个。
   - **(c) `index` 的比特如何划分？** 按 4.3.2 的表逐位写出。
3. 需要观察的现象 / 预期结果：
   - `LAP = 32 = 2^5`。
   - 真实槽数 = `LAP - 1 = 31 = BLOCK_CAP`，对应 `slots[0..30]`；`offset == 31` 是边界缝，无对应槽。
   - `index` 比特布局：**bit 0 = `HAS_NEXT`**（`SHIFT` 保留的元数据位）；**bit 1–5 = 块内偏移**（`(index >> 1) & 31`）；**bit 6 及以上 = 块号 lap**（`index >> 6`）。
4. 验证用例（手算）：
   - `index = 0`：bit0=0、offset=0、lap=0 → block 0 的第 0 槽，无后继。✓
   - `index = 60`（二进制 `111100`）：bit0=0、`60>>1=30`、offset=30、lap=0 → block 0 的最后一个真实槽。✓
   - `index = 64`（二进制 `1000000`）：bit0=0、`64>>1=32`、`32&31=0`、`64>>6=1` → block 1 的第 0 槽。✓（注意 60→64 跳过了 62，那个缝就是 `offset==31`。）
   - `index = 3`（二进制 `11`）：bit0=1（`HAS_NEXT` 已置位）、`3>>1=1`、offset=1 → block 0 第 1 槽，且当前块有后继。
5. 这个推导确认了四个常量是「为快速位运算 + 一个边界缝」而精心选定的，不是随意取值。

#### 4.3.5 小练习与答案

**练习 1**：把 `LAP` 改成 64、`SHIFT` 仍为 1，那么 `BLOCK_CAP`、offset 比特数、lap 的起始比特分别是多少？

**参考答案**：`BLOCK_CAP = LAP - 1 = 63`；offset 需要 \(\lceil \log_2 64 \rceil = 6\) 个比特，占据 bit 1–6；lap 从 bit 7 起（`index >> 7`）。块变大了（63 槽），单块分配更重但块间切换更少。

**练习 2**：为什么 `HAS_NEXT` 要单独占一个比特，而不是用「lap 不同」来推断？

**参考答案**：因为判断「当前块有没有后继」需要在**还站在当前块**时就快速得到答案，并且这个信息要随 `index` 一起被原子地推进（CAS head 时一并带上）。把 `HAS_NEXT` 塞进 `index` 的最低位，让「推进 head」和「更新后继标志」合并成一次原子 CAS；否则需要额外字段和额外的同步，徒增竞态。

---

### 4.4 Position<T> 与 SegQueue<T> 字段

#### 4.4.1 概念说明

有了 `Block` 和 `index` 编码，还差一层「外壳」把它们组织成队列。这层外壳就是两个结构：

- **`Position<T>`**：一个「位置」，由「一个 `index` + 一个指向 `Block` 的指针」组成。它回答「我现在该在哪个块、哪个偏移上操作」。
- **`SegQueue<T>`**：队列本体，持有**两个位置**——`head`（出队端）和 `tail`（入队端），外加一个只用于 `Drop` 语义的标记。

之所以 `index` 和 `block` 要**配对**放在一起，是因为单看 `index` 只知道逻辑位置（块号 + 偏移），但要从该位置真正读写，还需要一个指向那个 `Block` 的**裸指针**缓存，免得每次都从头沿着链表走一遍。`head.block` / `tail.block` 就是这两个端点各自的「当前块缓存」。

#### 4.4.2 核心流程：队列的初始空状态

`SegQueue::new()` 创建的空队列结构如下：

```
head: Position { index: 0, block: null }
tail: Position { index: 0, block: null }
_marker: PhantomData<T>
```

注意：**初始时 `head.block` 与 `tail.block` 都是 `null`**——一个块都还没分配。第一个 `push` 才会现分配第一个块，并把它同时安装到 `head.block` 和 `tail.block`（因为只有一个块时，头尾都指向它）。这个分配时序在 u3-l2 精讲。

#### 4.4.3 源码精读

`Position<T>`：

[seg_queue.rs:129-136](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L129-L136) —— `index` 是逻辑位置（块号+偏移+HAS_NEXT 编码在里头），`block` 是指向当前块的指针。

```rust
struct Position<T> {
    index: AtomicUsize,
    block: AtomicPtr<Block<T>>,
}
```

`SegQueue<T>` 本体：

[seg_queue.rs:138-170](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L138-L170) —— 文档注释把 SegQueue 描述为「a linked list of segments」；结构体只有三个字段：`head`、`tail`（都用 `CachePadded` 包裹避免伪共享）和 `_marker`。

```rust
pub struct SegQueue<T> {
    head: CachePadded<Position<T>>,
    tail: CachePadded<Position<T>>,
    _marker: PhantomData<T>,
}
```

几个要点：

- `head` / `tail` 用 `CachePadded` 包裹，和 ArrayQueue 一样，是为了让「生产者高频改 tail」与「消费者高频改 head」落到不同缓存行，避免伪共享（u4-l2 详讲）。
- `_marker: PhantomData<T>` 本身不占空间，只是告诉编译器「drop 这个队列可能会 drop `T` 类型的值」，让 `Drop` 检查与生命周期推断正确。

`new()` 的初始化：

[seg_queue.rs:188-200](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L188-L200) —— 两个 `Position` 的 `index` 都为 0、`block` 都为 `null`。`const fn` 使得可以在常量上下文里构造。

```rust
pub const fn new() -> Self {
    Self {
        head: CachePadded::new(Position {
            block: AtomicPtr::new(ptr::null_mut()),
            index: AtomicUsize::new(0),
        }),
        tail: CachePadded::new(Position {
            block: AtomicPtr::new(ptr::null_mut()),
            index: AtomicUsize::new(0),
        }),
        _marker: PhantomData,
    }
}
```

并发的安全性声明（unsafe impl 的论证放在 u4-l3，本讲只指出其存在）：

[seg_queue.rs:172-176](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L172-L176) —— 当 `T: Send` 时，`SegQueue<T>` 才 `Send + Sync`（元素能跨线程传递，队列才能跨线程共享）；并声明 `UnwindSafe` / `RefUnwindSafe`（可跨 panic 边界使用）。

```rust
unsafe impl<T: Send> Send for SegQueue<T> {}
unsafe impl<T: Send> Sync for SegQueue<T> {}
impl<T> UnwindSafe for SegQueue<T> {}
impl<T> RefUnwindSafe for SegQueue<T> {}
```

#### 4.4.4 代码实践

**源码阅读型实践：画出空队列并预测第一次 push 要做什么。**

1. 实践目标：把 `Position` / `SegQueue` 的字段与初始状态对上号，为 u3-l2 的 push 主链路做铺垫。
2. 操作步骤：
   - 读 `new()`，确认 `head` 和 `tail` 的 `index`/`block` 初值。
   - 在纸上画出空队列的字段示意图（两个 `Position`、都 `block=null`、`index=0`）。
   - 不看 u3-l2，只凭本讲的结构知识，推测：第一次 `push(x)` 时，代码发现 `tail.block` 是 `null`，必须先做哪两件事？
3. 需要观察的现象：空队列里没有任何 block，head 和 tail 都「悬空」。
4. 预期结果：第一次 push 必须 (1) `Block::new()` 分配第一个块；(2) 把这个块指针同时装到 `tail.block` 和 `head.block`（因为此刻头尾是同一个块），然后才能往 `slots[0]` 写值。你可以在 [seg_queue.rs:238-256](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L238-L256) 核对这一推测（这正是 u3-l2 的入口）。
5. 如果只想本地验证初始化本身：在一个临时 binary 里 `let q = SegQueue::<i32>::new();` 然后 `dbg!(q.is_empty());`，应得到 `true`（`is_empty` 比较的是 `head.index == tail.index`，二者都是 0）。运行结果待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：`head` 和 `tail` 为什么各自单独缓存一个 `block` 指针，而不是只存一个 `index` 再沿链表找？

**参考答案**：性能。如果只存 `index`，每次操作都要从链表头/尾遍历到目标块，O(块数) 的开销。把「当前块指针」缓存进 `Position`，端点操作就变成 O(1) 直接寻址。块切换（写满/读空时跳到 `next`）才更新这个缓存。

**练习 2**：`_marker: PhantomData<T>` 如果删掉，会发生什么？

**参考答案**：结构体字段里没有真正持有 `T`（值都藏在 `UnsafeCell<MaybeUninit<T>>` 的裸内存里），编译器的 drop 检查会认为 `SegQueue<T>` 的 drop 与 `T` 无关，从而拒绝在 `Drop` 里释放 `T` 类型值的某些写法，并可能影响自动 trait 推断。`PhantomData<T>` 显式声明「drop 本类型可能 drop `T`」，让 `Drop` 实现和 `Send/Sync` 约束都正确。

---

## 5. 综合实践

**把本讲所有结构串起来：画出 `SegQueue::new()` 之后、第一次 `push('a')` 之前的完整内存示意图，并标注每个常量的角色。**

要求在一张图上体现：

1. `SegQueue` 外壳：`head` 与 `tail` 两个 `CachePadded<Position>`，以及 `_marker`。
2. 每个 `Position` 的 `index`（=0）和 `block`（=`null`）。
3. 此刻链表上**还没有任何 block**（解释为什么 `head.block`/`tail.block` 是 null）。
4. 在图旁用一张表写出四个常量 `LAP=32`、`BLOCK_CAP=31`、`SHIFT=1`、`HAS_NEXT=1` 的含义，并标出 `index` 的比特布局（bit0=HAS_NEXT，bit1–5=offset，bit6+=lap）。
5. 再画出「已经 push 了 32 个元素」之后的预测结构：应该有 **2 个 block**（block 0 的 31 个槽全满，block 1 装了第 32 个元素），`tail.block` 指向 block 1、`tail.index` 的 lap 部分应为 1。用 `(index >> 1) / LAP` 和 `(index >> 1) % LAP` 验证你的 `tail.index` 取值是否自洽。

完成后，对照 [seg_queue.rs:214-292](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L214-L292) 的 `push` 实现检查你的预测——这正是 u3-l2 要逐行走读的代码。

## 6. 本讲小结

- `SegQueue` 是一条单向**块链表**：每个 `Block<T>` 装 31 个元素并通过 `next` 指针相连，无界、按需分配。
- `Slot<T>` = `UnsafeCell<MaybeUninit<T>>`（值）+ `AtomicUsize`（状态）；状态用 `WRITE=1`、`READ=2`、`DESTROY=4` 三个互不冲突的比特位表达。
- `Block::new` 用 `allocate_zeroed` 一次性零初始化整个块，因为 `next`、`state`、`MaybeUninit` 都容忍全 0。
- 四个常量 `LAP=32`、`BLOCK_CAP=LAP-1=31`、`SHIFT=1`、`HAS_NEXT=1` 共同把全局 `index` 编码为：**bit0=HAS_NEXT 元数据位、bit1–5=块内偏移、bit6+=块号**；其中 `offset==31` 是块边界缝，所以每块真实槽正好 31 个。
- 队列外壳 `SegQueue<T>` 持 `head`/`tail` 两个 `CachePadded<Position<T>>`（位置 = `index` + 当前块指针）和一个 `PhantomData<T>` 标记；`new()` 出来的空队列两个 `block` 都是 `null`。
- 本讲只看结构与编码；push/pop 主链路在 u3-l2，块的销毁协调在 u3-l3，原子序与 unsafe 论证在 u4 系列。

## 7. 下一步学习建议

- **下一步必读 u3-l2**：`SegQueue` 的 push 与 pop 主链路——块分配、`tail.index` 的 CAS 推进、块末尾安装 `next`、`HAS_NEXT` 与空队列判定。本讲的常量与字段是它的直接前置。
- 读 u3-l2 时，建议把本讲的「index 比特布局表」放在手边对照，每一步 CAS 都对照看 `offset` 和 `lap` 是怎么变的。
- 之后 u3-l3 讲 `destroy` / DESTROY 位如何安全回收块，把本讲提到的「块销毁」补全。
- 如果对 `CachePadded`、`Backoff`、原子内存序的细节感兴趣，可跳读 u4-l1（原子序与 fence）与 u4-l2（CachePadded 与 Backoff），但建议先完成 u3-l2/l3 再回头。
