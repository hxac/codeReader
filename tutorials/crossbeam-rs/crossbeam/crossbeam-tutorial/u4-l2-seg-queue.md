# SegQueue：无界分段队列

## 1. 本讲目标

本讲精读 `crossbeam-queue` 的 `SegQueue`——一个**无界**的多生产者多消费者（MPMC）并发队列。学完后你应当能够：

- 说清 SegQueue「链表式分段」的整体形状：为什么用一串小 `Block` 拼起来，而不是像 `ArrayQueue` 那样一块固定的大缓冲。
- 读懂 `index` 的位编码：`SHIFT`、`LAP`、`HAS_NEXT` 三个常量如何把「在第几段、第几槽、后面还有没有段」打包进一个 `usize`。
- 跟踪一次 `push` 和一次 `pop` 的完整路径，理解「先 CAS 索引占位、再写值/读值、用槽位状态位 `WRITE`/`READ` 同步」的两阶段协议。
- 解释最精巧的「段回收接力协议」：`Block::destroy` 如何在没有全局锁、且多个消费者可能正在读同一块的情况下，保证**恰好一个线程**释放整段内存，且绝不释放「还有线程在用」的段。

本讲是 u4-l1（ArrayQueue）的姊妹篇：ArrayQueue 用一块固定缓冲 + lap/stamp 防 ABA；SegQueue 用动态分配的链表块彻底回避了缓冲复用带来的 ABA，但换来了「按需分配 + 安全回收段」的工程复杂度。

## 2. 前置知识

阅读本讲前，建议先具备以下认知（均在前序讲义建立）：

- **u4-l1 ArrayQueue**：已经见过「环形缓冲 + lap/index 编码 + 槽位 stamp 状态机」这套无锁队列的基本套路。SegQueue 复用了同样的「`head`/`tail` 两个原子索引 + 槽位状态位 + CAS 推进」骨架，但把「单一固定缓冲」换成了「链表块」。
- **u2-l1 Backoff**：队列在「等别人写值」「等下一段就位」时用 `Backoff::snooze()` 退避，在 CAS 失败时用 `Backoff::spin()` 重试。这两条曲线在本讲会反复出现。
- **u2-l2 CachePadded**：`head`/`tail` 是被多核高频原子改写的热点字段，分别用 `CachePadded` 包裹以隔离缓存行、消除伪共享。
- 基本的 Rust `unsafe`、`AtomicUsize`、`compare_exchange_weak`、`MaybeUninit` 概念。

关键术语速览：

- **段（segment）/ 块（Block）**：一个能容纳 31 个元素的小数组节点，多个 Block 用 `next` 指针串成链表。
- **槽（Slot）**：Block 里的一个存储位，含一个值和一个状态字。
- **lap（圈）**：索引计数走完一个 Block 的 32 个位置称为「一圈」。`LAP = 32`。
- **HAS_NEXT**：索引最低位，标记「当前块后面还有下一个块」。

## 3. 本讲源码地图

本讲只涉及 `crossbeam-queue` 的两个源码文件，辅以测试文件验证行为：

| 文件 | 作用 |
| --- | --- |
| [crossbeam-queue/src/lib.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/lib.rs) | crate 门面，导出 `ArrayQueue` 与 `SegQueue`，并用 `#[cfg(feature = "alloc")]` + `target_has_atomic = "ptr"` 门控 |
| [crossbeam-queue/src/seg_queue.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs) | SegQueue 全部实现：`Block`/`Slot`/`Position` 数据结构、`push`/`pop`/`push_mut`/`pop_mut`、`len`/`is_empty`/`Drop` |
| crossbeam-queue/src/alloc_helper.rs | 一层极薄的全局分配器封装（`allocate_zeroed` 等），给 `Block::new` 提供零初始化分配 |
| crossbeam-queue/tests/seg_queue.rs | 行为测试：`smoke`、`spsc`、`mpmc`、`drops`、`stack_overflow`，是本讲实践的运行依据 |

> crate 文档把两者定位得很清楚：ArrayQueue 是「构造时一次性分配定长缓冲」的有界队列；SegQueue 是「按需分配小段」的无界队列，没有容量上限，但每个元素都要走动态分配，因而比 ArrayQueue 略慢。见 [crossbeam-queue/src/lib.rs:1-6](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/lib.rs#L1-L6)。

## 4. 核心概念与源码讲解

### 4.1 数据布局与 index 编码：Block 链表、状态位、SHIFT/HAS_NEXT/LAP

#### 4.1.1 概念说明

SegQueue 的形状是**一条单向链表**，链表上每个节点 `Block` 是一个能装下若干元素的小数组。生产者从链表尾部追加元素；当当前块写满时，分配一个新块挂到链表尾；消费者从链表头部取元素；当一个块里所有元素都被取走，就回收这个块。

这就把「无界」和「并发」分开了：容量无界靠「按需挂新块」实现；并发安全靠每块内部槽位上的原子状态位 + `head`/`tail` 索引的 CAS 推进实现。

一个核心设计取舍是：**每块只装 31 个元素**（`BLOCK_CAP = 31`），而不是装很多。块小意味着：

- 回收频率高、单次回收代价低；
- 消费者「追上」生产者、跨越块边界的窗口短；
- 块本身的内存可以被快速归还。

但块小也意味着一次大批量 `push` 会分配很多块（这点正是综合实践要量化观察的）。

#### 4.1.2 核心流程

先看四组常量，它们是整个实现的「坐标系」：

[crossbeam-queue/src/seg_queue.rs:17-32](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L17-L32) 定义了槽位状态位与索引编码常量。中文说明如下：

- `WRITE = 1`：该槽已被写入值（生产者就绪信号）。
- `READ = 2`：该槽的值已被读出（消费者完成信号）。
- `DESTROY = 4`：该块正在被销毁，回收接力标记。
- `LAP = 32`：每块覆盖「一圈」共 32 个索引位置。
- `BLOCK_CAP = LAP - 1 = 31`：每块真正能存 31 个值。
- `SHIFT = 1`：索引最低 1 位保留给元数据。
- `HAS_NEXT = 1`：最低位取值，表示「当前块之后还有下一个块」。

**index 编码三件套**。`head`/`tail` 各持一个 `usize` 索引，它的位含义是：

\[
\text{index} = \underbrace{(\text{位置计数器} \ll 1)}_{\text{bits 1\dots}} \;\big|\; \underbrace{\text{HAS\_NEXT}}_{\text{bit 0}}
\]

由此可从 `index` 反推出三件事：

\[
\text{offset（块内槽号）}= (\text{index} \gg \text{SHIFT}) \bmod \text{LAP}
\]

\[
\text{lap（块序号）}= (\text{index} \gg \text{SHIFT}) \;/\; \text{LAP}
\]

\[
\text{是否有后继块}= (\text{index}\ \&\ \text{HAS\_NEXT}) \ne 0
\]

- `offset` 取值 `0..32`，其中 `0..31`（即 `0..BLOCK_CAP`）是 31 个真实槽，`offset == 31 == BLOCK_CAP` 是**哨兵位置**——遇到它就表示「当前块已用尽，该跨到下一块了」。这就是 `BLOCK_CAP = LAP - 1` 的由来：32 个位置里留 1 个当哨兵。
- `SHIFT = 1` 加上每次推进 `index += 1 << SHIFT = 2`，是为了把最低位腾出来存 `HAS_NEXT` 而又不干扰位置计数器的连续递增（`+2` 不会进位到 bit 0，所以 `HAS_NEXT` 在块内随推进被保留）。

**为什么 SegQueue 不像 ArrayQueue 那样需要复杂的 stamp 防 ABA？** 因为 ArrayQueue 的缓冲是**一块固定内存反复绕圈使用**，同一个槽在不同圈（lap）下会被复用，必须用 stamp 标记「这是第几圈」才能区分。而 SegQueue 每一圈都对应一个**全新堆分配的 Block**，指针各不相同，天然不存在「同一块内存被重复使用导致身份混淆」的 ABA。索引里的 lap 计数只用于算「在第几块、第几槽」，不需要担起防 ABA 的职责。

#### 4.1.3 源码精读

`Slot` 与 `Block` 的定义见 [crossbeam-queue/src/seg_queue.rs:34-62](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L34-L62)，中文说明：

```rust
struct Slot<T> {
    value: UnsafeCell<MaybeUninit<T>>, // 值；MaybeUninit 表示可能尚未初始化
    state: AtomicUsize,                // WRITE/READ/DESTROY 状态位
}

struct Block<T> {
    next: AtomicPtr<Block<T>>,         // 链表下一块（null = 末块）
    slots: [Slot<T>; BLOCK_CAP],       // 31 个槽
}
```

`Block::new` 用**零初始化分配**构造一个块，见 [crossbeam-queue/src/seg_queue.rs:74-90](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L74-L90)。零是合法初值，因为：

- `next = 0` 即 `null`，表示「暂时没有下一块」；
- `state = 0` 即三个状态位都未置，表示「既没写也没读」；
- `value` 是 `MaybeUninit`，本就不要求初始化。

`SegQueue` 主体只有两个字段：`head` 与 `tail`，各自是一个 `CachePadded<Position<T>>`，见 [crossbeam-queue/src/seg_queue.rs:161-170](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L161-L170)。`Position` 把「索引」和「当前块指针」绑在一起（[seg_queue.rs:129-136](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L129-L136)）：

```rust
struct Position<T> {
    index: AtomicUsize,        // 编码后的索引（含 offset/lap/HAS_NEXT）
    block: AtomicPtr<Block<T>> // 指向 head/tail 当前所在块
}
```

`CachePadded`（u2-l2）保证 `head` 和 `tail` 落在不同缓存行——这是关键，因为生产者线程密集写 `tail`、消费者线程密集写 `head`，若它们共享缓存行会造成严重的伪共享。

> 一个易被忽略的工程细节：`Block` 是**直接在堆上**用 `Box` 创建的，从不先在栈上构造。测试 [crossbeam-queue/tests/seg_queue.rs:237-250](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/tests/seg_queue.rs#L237-L250)（`stack_overflow`）专门守护这一点：当 `T` 是 32 KiB 的大结构时，`[Slot<T>; 31]` 会让一个 `Block` 高达约 1 MiB，若在栈上构造会立即爆栈。

#### 4.1.4 代码实践

**实践目标**：用纸笔（或一个小程序）验证你对 index 编码的理解。

**操作步骤**：

1. 取一个 `index = 0b1000`（十进制 8）。计算 `offset = (8 >> 1) % 32` 与 `lap = (8 >> 1) / 32`。
2. 取 `index = 0b111110`（十进制 62，即 `(31 << 1)`）。计算 `offset`，确认它等于 `BLOCK_CAP`（哨兵位置）。
3. 取 `index = 0b111111`（十进制 63）。它的最低位是 1，说明 `HAS_NEXT` 被置位；再算 `offset`，应当与第 2 步相同。

**预期结果**：

- 第 1 步：`offset = 4`，`lap = 0`，即「第 0 块的第 4 槽」。
- 第 2 步：`offset = 31 = BLOCK_CAP`，即哨兵位置，说明当前块已写满。
- 第 3 步：`offset` 仍为 31，但 `HAS_NEXT=1`，说明「当前块后面还有一块」。

**需要观察的现象**：`+2` 推进会让 `offset` 每次加 1 而 `HAS_NEXT` 位保持不变——这解释了为什么块内连续推进不需要每次重新查 tail。

> 待本地验证：若你愿意，可在 `push` 临时加一行 `eprintln!("offset={}", offset)` 跑 `cargo test -p crossbeam-queue seg_queue::smoke -- --nocapture` 观察输出（**注意：测试结束后务必撤销改动，不要提交对源码的修改**）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `BLOCK_CAP` 恰好是 `LAP - 1` 而不是等于 `LAP`？

**答案**：因为一个 lap 的 32 个索引位置里，最后一个（`offset == 31`）被当作「块已用尽」的哨兵，不对应真实槽。所以 32 个位置里只有 31 个能存值，`BLOCK_CAP = 31`。

**练习 2**：`head` 和 `tail` 为什么要分别用 `CachePadded` 包裹？

**答案**：生产者高频写 `tail`、消费者高频写 `head`。若二者落在同一缓存行，MESI 协议会让该行在核间反复失效（伪共享），吞吐崩塌。`CachePadded` 把它们撑到各自独占一条缓存行（u2-l2）。

### 4.2 push：按需预分配下一段 + CAS tail + 写值置 WRITE

#### 4.2.1 概念说明

`push(value)` 把一个元素追加到队尾。它要同时完成三件事：**占位**（CAS 推进 tail 索引，抢到一个槽的独占写入权）、**写值**（把值写进槽并置 `WRITE`）、**扩容**（当写满当前块时，挂上下一块）。

难点在于多个生产者并发竞争同一个 tail。这里采用经典的「CAS 推进索引」：谁能把 tail 从旧值 CAS 到新值，谁就赢得了这个槽；失败者读取返回的新 tail 重试。索引推进成功后，再慢慢写值——值是否就绪由槽的 `WRITE` 位单独通知消费者。

一个关键的优化是**预分配**：当一个生产者即将写满当前块（`offset + 1 == BLOCK_CAP`）时，它**提前**把下一块分配好。这样真正写满、需要挂接时，直接用现成的指针，把其它等待线程的等待时间压到最短。

#### 4.2.2 核心流程

`push` 的主循环伪代码：

```
load tail.index, tail.block
loop:
    offset = (tail >> SHIFT) % LAP
    if offset == BLOCK_CAP:            # 当前块已满，但下一块还没就位
        snooze 退避; reload tail; continue

    if offset + 1 == BLOCK_CAP 且 未预分配:   # 即将写满，提前分配下一块
        next_block = Block::new()

    if block == null:                   # 第一次 push，分配首块
        CAS tail.block: null -> new
            成功: head.block = new; block = new
            失败: 把 new 回收到 next_block 复用; reload; continue

    new_tail = tail + (1 << SHIFT)
    CAS_weak tail.index: tail -> new_tail
        成功:
            if offset + 1 == BLOCK_CAP:        # 写入的是本块最后一槽 → 挂接下一块
                安装 next_block 到 tail.block / tail.index / block.next
            写值到 slot[offset]; 置 WRITE 位
            return
        失败:
            tail = 返回的新值; reload block; spin 退避
```

注意三个细节：

1. **先 CAS 索引，后写值**。索引 CAS 是「门票」，赢得门票才能往这个槽写；值就绪与否由 `WRITE` 位另行通知（消费者会等它）。
2. **首块的特殊处理**。队列为空时 `tail.block == null`，第一个成功的生产者负责分配首块，并且**同时**把 `head.block` 也指向它，这样消费者才能开始工作。
3. **预分配的复用**。若一个生产者分配了首块却在 CAS 中输给别人，它不会丢弃这块内存，而是把它塞进自己的 `next_block` 局部变量，留作「即将写满时」的下一块，避免一次无谓的分配-释放。

#### 4.2.3 源码精读

主循环开头：算 offset、处理「块已满但下一块没就位」的等待，见 [crossbeam-queue/src/seg_queue.rs:220-230](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L220-L230)。

预分配下一块（即将写满时提前 `Block::new()`）：见 [crossbeam-queue/src/seg_queue.rs:232-236](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L232-L236)。

首块分配 + CAS + 同时初始化 head.block：见 [crossbeam-queue/src/seg_queue.rs:238-256](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L238-L256)。CAS 失败时把 `new` 回收到 `next_block`（`Box::from_raw`），避免内存泄漏。

CAS tail 索引成功后的处理：挂接下一块、写值、置 `WRITE`，见 [crossbeam-queue/src/seg_queue.rs:261-284](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L261-L284)。其中挂接下一块的关键三行：

```rust
self.tail.block.store(next_block, Ordering::Release);   // tail 指向新块
self.tail.index.store(next_index, Ordering::Release);   // 索引跨入新 lap
(*block).next.store(next_block, Ordering::Release);     // 链表链接
```

这三行共同构成「下一块就位」的发布；`wait_next`（见 4.4）会读取 `block.next` 来等待它。

CAS 失败的退避路径：见 [crossbeam-queue/src/seg_queue.rs:285-289](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L285-L289)，用 `backoff.spin()`（u2-l1）做纯自旋重试。

> 顺带一提，本文件还提供了 `push_mut`（[seg_queue.rs:309-345](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L309-L345)），它在持有 `&mut self` 独占访问时省掉所有原子操作与内存屏障，逻辑与 `push` 同构，是单线程批量的快路径。

#### 4.2.4 代码实践

**实践目标**：跟踪「首块分配」的竞态，理解 CAS 输家如何避免泄漏。

**操作步骤**：

1. 阅读 [seg_queue.rs:238-256](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L238-L256)。
2. 设想两个生产者线程同时 `push` 第一条消息：两者都看到 `block == null`，都执行了 `Box::into_raw(Block::new())` 分配了各自的 `new`。
3. 回答：CAS 赢家做什么？CAS 输家拿到的那块 `new` 去哪了？如果不做那个回收，会发生什么？

**预期结果**：CAS 赢家把 `tail.block` 从 null 改成自己的块，并设 `head.block`；输家的 `compare_exchange` 返回 `Err`，于是它执行 `next_block = Some(Box::from_raw(new))`，把刚分配的块「接住」放进局部变量，留作后续写满时的下一块。**如果不回收**，输家分配的块就永远无人引用，造成内存泄漏——这正是该分支存在的原因。

**需要观察的现象**：这是「源码阅读型实践」，无需运行；若要验证，可用 miri 跑 `drops` 测试确认无泄漏（见 4.4 实践）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `push` 要「先 CAS 索引、再写值」，而不是反过来？

**答案**：索引 CAS 是抢占槽位写入权的「门票」，只有抢到票的线程才有权写这个槽。若先写值再抢票，多个生产者会写进同一个槽造成数据破坏。值是否就绪由独立的 `WRITE` 位通知消费者，因此「占位」与「数据就绪」可以解耦。

**练习 2**：预分配（`offset + 1 == BLOCK_CAP` 时提前 `Block::new()`）带来了什么好处？

**答案**：当真正写满、需要挂接下一块时，指针已经现成，能立刻 `store` 发布，从而把其它正在 `wait_next` / `snooze` 等待的线程的等待时间压到最短，减少自旋开销。

### 4.3 pop：CAS head 推进 + 等待 WRITE + 读值置 READ

#### 4.3.1 概念说明

`pop()` 从队头取一个元素，返回 `Option<T>`（空时返回 `None`）。它与 `push` 对称：CAS 推进 head 索引抢到一个槽的读取权，然后**等待**该槽的 `WRITE` 位被置（生产者可能刚抢到索引还没写完值），读到值后置 `READ` 位。

这里有个微妙点：消费者抢到 head 索引后，对应的生产者**未必已经把值写好**——因为生产者是「先 CAS 索引、后写值」。所以消费者必须 `wait_write`：自旋（用 `Backoff::snooze`）直到 `WRITE` 位出现。这种「先占座、后上菜」的解耦让生产者和消费者的关键路径都只卡在一次 CAS 上。

判断「队列是否为空」也值得注意。直觉上比较 head 与 tail 即可，但 tail 是生产者高频写入的争用字段，每次 pop 都加载它会加剧争用。SegQueue 的做法是用 `HAS_NEXT` 位做**缓存提示**：如果当前块已知有后继块（`HAS_NEXT=1`），那队列必然非空，根本不用读 tail；只有当处于「最后已知块」时才读 tail 做精确判空。

#### 4.3.2 核心流程

`pop` 主循环伪代码：

```
load head.index, head.block
loop:
    offset = (head >> SHIFT) % LAP
    if offset == BLOCK_CAP:                # 当前块消费完，下一块还没就位
        snooze; reload; continue

    new_head = head + (1 << SHIFT)
    if new_head & HAS_NEXT == 0:           # 处于「最后已知块」，需要判空
        fence(SeqCst); load tail
        if head>>SHIFT == tail>>SHIFT:     # head 与 tail 同位 → 空
            return None
        if head 所在 lap != tail 所在 lap:  # 它们在不同块 → 确实有后继
            new_head |= HAS_NEXT           # 记下这个提示，后续 pop 免读 tail

    if block == null:                      # 第一次 push 还在进行
        snooze; reload; continue

    CAS_weak head.index: head -> new_head
        成功:
            if offset + 1 == BLOCK_CAP:    # 读的是本块最后一槽 → 跨到下一块
                next = wait_next(); 更新 head.block / head.index
            wait_write();                  # 等生产者置 WRITE
            value = slot[offset].read()
            if offset + 1 == BLOCK_CAP:
                Block::destroy(block, 0)   # 整块读完，启动回收（见 4.4）
            elif 该槽已被别人标记 DESTROY:
                Block::destroy(block, offset+1)  # 接力回收
            else:
                置 READ 位
            return Some(value)
        失败:
            head = 返回的新值; reload block; spin 退避
```

#### 4.3.3 源码精读

判空与 `HAS_NEXT` 提示逻辑：见 [crossbeam-queue/src/seg_queue.rs:381-396](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L381-L396)。其中 `head >> SHIFT == tail >> SHIFT` 是「逻辑位置相同即空」，`/ LAP` 比较的是「块序号」。

等待生产者写值：`wait_write` 用 `snooze` 自旋到 `WRITE` 位置，见 [crossbeam-queue/src/seg_queue.rs:43-51](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L43-L51)：

```rust
fn wait_write(&self) {
    let backoff = Backoff::new();
    while self.state.load(Ordering::Acquire) & WRITE == 0 {
        backoff.snooze();
    }
}
```

读取值并决定回收/置位：见 [crossbeam-queue/src/seg_queue.rs:427-440](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L427-L440)。读值用 `slot.value.get().read().assume_init()`——这里 `assume_init` 是安全的，因为 `wait_write` 已经保证 `WRITE` 被置、值已就绪。读完后的三分支决定了谁来回收这块（详见 4.4）。

跨块移动（读的是本块最后一槽）：见 [crossbeam-queue/src/seg_queue.rs:416-425](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L416-L425)，其中调用 `wait_next` 等待 `block.next` 就位。

> `is_empty`（[seg_queue.rs:541-545](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L541-L545)）只比较 `head>>SHIFT == tail>>SHIFT`，是 `pop` 判空逻辑的简化版（不更新提示位）。

#### 4.3.4 代码实践

**实践目标**：运行真实测试，验证 SPSC（单生产者单消费者）下消息严格有序、且最后 `pop` 返回 `None`。

**操作步骤**：

1. 阅读 [crossbeam-queue/tests/seg_queue.rs:101-126](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/tests/seg_queue.rs#L101-L126)（`spsc` 测试）。
2. 执行：
   ```bash
   cargo test -p crossbeam-queue seg_queue::spsc -- --nocapture
   ```
3. 观察：消费者用 `loop { if let Some(x) = q.pop() { assert_eq!(x, i); break; } }` 不断重试 `pop`，直到拿到第 `i` 条。

**预期结果**：测试通过，断言 `q.pop().is_none()` 成立（生产者发完 COUNT 条后队列被抽干）。在 miri 下 COUNT 会自动降为 100（`cfg!(miri)`），便于穷尽检查。

**需要观察的现象**：消费者 `pop` 返回 `None` 时并不意味着「永远没有」——它只代表「此刻 head 追上了 tail」。所以测试里消费者收到 `None` 后会立刻重试，而不是放弃。这正是无锁队列「`None` 是瞬时快照」的典型用法。

#### 4.3.5 小练习与答案

**练习 1**：消费者 CAS 抢到 head 索引后，为什么还要 `wait_write`？

**答案**：因为生产者是「先 CAS tail 索引占位、后写值并置 `WRITE`」。消费者抢到 head 后，对应生产者可能刚占完位、还没来得及写值。`wait_write` 自旋等待 `WRITE` 位置，保证读到的是已就绪的值，而不是未初始化内存。

**练习 2**：`pop` 里 `new_head & HAS_NEXT == 0` 这个分支什么时候进入？它为什么能省掉对 tail 的读取？

**答案**：当当前 head 的 `HAS_NEXT` 位为 0，即「处于最后已知块」时进入，此时必须读 tail 才能判空。反之，若 `HAS_NEXT == 1`（已知有后继块），队列必然还有数据（至少后继块里有），所以跳过整个判空分支、免去一次 contended 的 tail 加载。

### 4.4 段的推进与回收协议：wait_next 与 Block::destroy 的接力

#### 4.4.1 概念说明

这是 SegQueue 最精巧、也是本讲实践重点的部分。问题陈述：当一个 Block 的 31 个槽都被消费完，我们要释放这块内存。但难点在于——

**多个消费者可能正同时读同一块的不同槽**。无锁队列里，消费者 A 读槽 30（本块最后一槽）并 CAS 推进 head 跨块时，消费者 B 可能才刚读完槽 5 的值、还没置 `READ` 位。如果 A 这时直接释放整块，B 手里就握着悬垂指针。

所以释放必须满足：**所有槽都确认已被读（`READ` 位都置上）之后，才能释放；且恰好由一个线程执行释放。** 但又没有全局锁来串行化这件事。

SegQueue 的解法是一个优雅的**接力（handoff）协议**：

- 当某线程（通常是读最后一槽的消费者）判定「这块可以开始回收了」，它调用 `Block::destroy(block, 0)`。
- `destroy` 从 `start` 槽往后扫描：对每个槽，检查 `READ` 是否已置。
  - 若已置 `READ`（该槽无人再用）→ 继续扫下一个。
  - 若未置 `READ`（仍有线程在用这个槽）→ 用 `fetch_or` 给该槽打上 `DESTROY` 位，然后**提前返回**，把回收责任「挂起」在这个未完成的槽上。
- 后来，那个正在用该槽的消费者读完后置 `READ` 时，会发现 `DESTROY` 位已经被别人打上了——这说明「别人想把整块回收，但被我挡住了」。于是**它**接手，从自己的下一个槽开始继续 `destroy` 扫描。
- 这样一路接力，最终总有一个线程扫到末尾、发现所有槽都已 `READ`，由它执行真正的 `Box::from_raw`（释放内存）。

效果：**恰好一个线程释放内存**，且释放时**所有槽都已被读**，没有悬垂引用。这正是本讲综合实践要解释的「最后一个完成读取的线程负责释放段」协议——更精确地说，是「接力链上第一个扫到全 `READ` 的线程负责释放」。

#### 4.4.2 核心流程

`destroy` 的扫描逻辑伪代码：

```
fn destroy(this, start):
    for i in start .. BLOCK_CAP-1:        # 扫描 start..30（不含触发槽本身）
        slot = this.slots[i]
        if slot.state & READ == 0:        # 还有人在用这个槽
            old = slot.state.fetch_or(DESTROY)
            if old & READ == 0:           # 原子确认仍无人读 → 把责任挂这里
                return                    # 等那个读者完成后接力
            # 否则 fetch_or 与 load 之间恰好被置了 READ，继续扫
    # 扫完都没被挡 → 所有槽已读，安全释放
    drop(Box::from_raw(this))
```

`pop` 中触发回收的三种情形（[seg_queue.rs:432-438](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L432-L438)）：

1. 读的是本块最后一槽（`offset+1 == BLOCK_CAP`）→ `Block::destroy(block, 0)`，从槽 0 开始扫。
2. 不是最后一槽，但读完后发现槽上已有 `DESTROY` 位（别人想回收被自己挡住）→ `Block::destroy(block, offset+1)`，从自己的下一槽接棒。
3. 否则 → 只置 `READ` 位即可，不触发回收。

跨块推进时等待下一块就位：`wait_next` 自旋到 `block.next` 非 null，见 [seg_queue.rs:92-102](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L92-L102)。它存在的原因：生产者写满一块后，先 CAS 索引跨块、再 `store block.next`；消费者若在两者之间跨块，就会看到 `next` 还是 null，需要等。

#### 4.4.3 源码精读

`Block::destroy` 全文：见 [crossbeam-queue/src/seg_queue.rs:104-121](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L104-L121)。关键两点：

- 循环范围 `start..BLOCK_CAP - 1`，即 `start..30`，**不含第 30 槽**（触发 `destroy` 的那一槽正在被当前线程读，无需检查）。
- `load` 后再 `fetch_or` 是两次原子操作，但 `fetch_or` 的返回值（旧值）才是权威判定：若 `fetch_or` 返回的旧值里 `READ` 仍为 0，才确认「确有线程在用」并返回；否则说明 `load` 与 `fetch_or` 之间正好有人置了 `READ`，继续往下扫。这种「load 预览 + fetch_or 确认」的双检避免了在已经 `READ` 的槽上误打 `DESTROY` 而白跑一趟。

`Block::destroy_mut`（[seg_queue.rs:123-126](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L123-L126)）是独占访问版本（`pop_mut` 用），无需扫描与接力，直接释放。

`Drop for SegQueue`（[seg_queue.rs:599-634](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L599-L634)）在队列被整体丢弃时，遍历 head→tail 之间所有槽 `assume_init_drop` 释放剩余值，并逐块 `Box::from_raw` 释放链表节点——因为 `drop` 拿的是 `&mut self`，独占访问，无需并发接力协议。

> 行为测试 `drops`（[tests/seg_queue.rs:164-213](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/tests/seg_queue.rs#L164-L213)）用一个带 `Drop` 计数的类型，断言并发 push/pop 后所有被 pop 的值都恰好析构一次、队列 drop 时剩余值也全部析构——这正是对「回收协议不泄漏、不重复释放」的端到端验证。

#### 4.4.4 代码实践（本讲核心实践）

**实践目标**：量化「按需分段」的分配次数，并用自己的话讲清接力回收协议。

**操作步骤（A：估算段分配次数）**：

1. 假设持续 `push` \(10^6\) 条消息且暂不 `pop`。
2. 每块容量 `BLOCK_CAP = 31`。计算需要的块数：
   \[
   \lceil 1\,000\,000 / 31 \rceil = 32\,259
   \]
   （32 258 块各装 31 条 = 999 998 条，剩 2 条进第 32 259 块。）
3. 写一个最小程序验证（在自己的测试 crate 里，**不要改 crossbeam 源码**）：
   ```rust
   use crossbeam_queue::SegQueue;
   let q = SegQueue::new();
   for i in 0..1_000_000u64 { q.push(i); }
   assert_eq!(q.len(), 1_000_000);
   // drop 时会走 Drop，释放约 32259 个块
   ```

**操作步骤（B：解释接力回收协议）**：

阅读 [seg_queue.rs:104-121](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L104-L121) 与 [seg_queue.rs:432-438](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L432-L438)，回答以下问题：

1. 为什么 `destroy` 的循环是 `start .. BLOCK_CAP - 1`，而不是 `start .. BLOCK_CAP`？
2. 假设消费者 A 刚读完槽 30 并调用 `destroy(block, 0)`，而消费者 B 还卡在槽 7（已 CAS 占位、尚未置 `READ`）。请描述 A 和 B 各自的执行路径，以及最终是谁释放了这块内存。

**预期结果**：

- A：从槽 0 扫到槽 7，发现槽 7 未 `READ`，用 `fetch_or` 打上 `DESTROY` 位后 `return`，**不释放**。
- B：稍后读完槽 7 的值，执行 `slot.state.fetch_or(READ)`，发现返回的旧值带 `DESTROY`（别人想回收），于是调用 `destroy(block, 8)` 从槽 8 接棒扫描。
- 若 B 之后再无别的消费者卡在槽里，B 一路扫到槽 29 全是 `READ`，由 **B** 执行 `Box::from_raw` 释放这块。若有第三人也卡住，则继续接力。

**需要观察的现象**：释放者既不是「读最后一槽的 A」，也不一定是「最后一个开始读的线程」，而是**接力链上第一个扫到「全 `READ`」的线程**——这正是「恰好一个线程释放」且「释放时无人在用」的双重保证。

> 待本地验证：步骤 A 的块数无法从外部 API 直接观测（SegQueue 不暴露块计数）。若要实测，可用 miri/valgrind 观察分配次数，或临时在 `Block::new` 加日志统计（**测试后撤销**）。协议解释部分（步骤 B）属于源码阅读型实践，可直接完成。

#### 4.4.5 小练习与答案

**练习 1**：`destroy` 里为什么用「先 `load` 预览、再 `fetch_or` 确认」两步，而不是直接 `fetch_or(DESTROY)` 然后看返回值？

**答案**：两步双检是一个性能优化。若某槽其实已经 `READ`，直接 `fetch_or` 会无谓地给它打上 `DESTROY` 位（虽然无害，但会让后续判断多一次不必要的位运算）。先 `load` 预览能在大多数槽已 `READ` 的情况下快速跳过，只对「疑似未读」的槽付出 `fetch_or` 的代价；而 `fetch_or` 的返回值仍是权威判定，保证正确性。

**练习 2**：`wait_next` 为什么需要存在？生产者挂接下一块时不是已经 `store` 了 `block.next` 吗？

**答案**：生产者写满一块时，**先 CAS tail 索引跨块、后才 `store block.next`**（见 [seg_queue.rs:267-275](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L267-L275)）。消费者若在这两步之间跨块，就会看到 `next` 仍为 null，必须用 `wait_next` 自旋等待生产者完成 `store`。这是「索引先于链接发布」带来的短暂窗口。

## 5. 综合实践

把本讲知识串起来，完成一个「观察 MPMC 下分段分配与安全回收」的小任务。

**任务**：实现一个 4 生产者 / 4 消费者的 SegQueue 压力测试，验证无丢失、无重复、无泄漏，并量化块分配。

**操作步骤**：

1. 阅读 `mpmc` 测试 [tests/seg_queue.rs:128-162](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/tests/seg_queue.rs#L128-L162)：它用 4 个生产者各发 `COUNT` 条 `0..COUNT`，4 个消费者各收 `COUNT` 条，每收到 `n` 就给 `v[n]` 计数 +1，最后断言每个值恰好被收到 `THREADS` 次。
2. 在自己的测试 crate（依赖 `crossbeam-queue`、`crossbeam-utils`）里复刻它，但把 `COUNT` 调大（如 `50_000`）。
3. 执行：
   ```bash
   cargo test -p crossbeam-queue seg_queue::mpmc --release
   cargo test -p crossbeam-queue seg_queue::drops --release
   ```
4. 在心里（或日志中）回答：
   - 4 生产者共发 `4 × 50_000 = 200_000` 条，会分配多少块？（答：\(\lceil 200\,000 / 31 \rceil = 6\,452\) 块，跨生产者共享。）
   - 消费完毕后这些块是否被回收？依据是哪个协议？（答：接力回收协议，由每个块的「接棒扫到全 `READ`」的线程释放。）
   - `drops` 测试如何证明无泄漏？（答：它统计 `Drop` 计数，断言 pop 出的值与剩余值析构次数之和恰等于 push 次数。）

**预期结果**：测试全部通过；你能用自己的话讲清「索引 CAS 占位 → WRITE/READ 同步 → 跨块 wait_next → 接力 destroy 回收」这一完整链路。

**需要观察的现象**：高并发下消费者会频繁拿到 `pop() == None`（瞬时追平 head/tail）并立刻重试——这不是 bug，而是无锁队列的正常行为；`Backoff` 在此期间把空转控制在合理水平。

> 待本地验证：具体块数无法从公开 API 读出；如需精确观测，建议在 fork 中临时给 `Block::new`/`Block::destroy` 加计数日志后运行，验证完即撤销。

## 6. 本讲小结

- SegQueue 是一条**单向链表**，每块 `Block` 装 31 个元素（`BLOCK_CAP = LAP - 1 = 31`，剩 1 个位置当哨兵）；用「按需挂新块」实现无界，回避了 ArrayQueue 的缓冲复用 ABA 问题。
- 索引 `usize` 用 `SHIFT=1` 把最低位腾给 `HAS_NEXT`（标记「当前块有后继」），位置计数藏在高位；`offset = (index>>1) % 32`、`lap = (index>>1) / 32`。
- 槽状态位 `WRITE`/`READ`/`DESTROY` 解耦了「占位」与「数据就绪」：生产者先 CAS tail 占位、再写值置 `WRITE`；消费者先 CAS head 占位、再 `wait_write` 读值、置 `READ`。
- `push` 在即将写满时**预分配**下一块以缩短他人等待；首块分配竞态的输家会回收自己的块避免泄漏。
- `pop` 用 `HAS_NEXT` 提示跳过对 contended 字段 `tail` 的读取来判空；跨块时用 `wait_next` 等待生产者发布 `block.next`。
- 段回收靠 `Block::destroy` 的**接力协议**：扫描各槽 `READ` 位，遇到未读完的槽就打 `DESTROY` 并挂起，由那个读者完成后接棒，最终由扫到「全 `READ`」的线程释放——保证「恰好一个线程释放、且释放时无人在用」。

## 7. 下一步学习建议

- **进入 epoch 单元（u5）**：SegQueue 的回收是「精确计数 + 接力」的精巧协议，只适用于「节点何时可释放」能被精确推导的场景。下一单元的 `crossbeam-epoch` 解决的是更一般的「无锁数据结构中已删除节点的安全回收」难题，是 deque（u6）和 skiplist（u7）的安全基石。
- **对比阅读 ArrayQueue（u4-l1）**：重新对照两种队列的 lap/index 编码与回收策略，体会「固定缓冲 + stamp 防 ABA」与「动态链表块 + 接力回收」的取舍。
- **用工具验证**：在 `crossbeam-queue` 上尝试 `cargo +nightly miri test -p crossbeam-queue`（miri 会把测试 COUNT 自动调小以穷尽检查），确认本讲的 `unsafe` 块没有未定义行为；这会自然过渡到 u7-l3 的并发正确性验证主题。
