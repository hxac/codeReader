# 无界链表：list flavor

## 1. 本讲目标

本讲深入 `src/flavors/list.rs`，讲解 `unbounded()` 通道（无界通道）的底层实现。读完本讲，你应当能够：

- 说出 list flavor 如何用「分块链表」组织无界队列，以及它和 array flavor（环形数组）的核心差异。
- 看懂 `index` 的位编码（`LAP` / `BLOCK_CAP` / `SHIFT` / `MARK_BIT`）和 `Slot` 的三个状态位（`WRITE` / `READ` / `DESTROY`）。
- 跟踪一次 `send` 如何用 CAS 推进 `tail`、如何在块满时增长新块，并理解为什么**发送方永不阻塞**。
- 跟踪一次 `recv` 如何用 CAS 推进 `head`、如何判定空与断开，以及 `write`/`read` 如何配合 `SyncWaker::notify` 形成生产消费闭环。
- 解释 `Block::destroy` 的惰性回收机制，以及 `DESTROY` 位如何把「已读空的块」的释放责任安全地交接给最后一个 reader。

## 2. 前置知识

本讲假设你已经读过：

- **u2-l1（架构总览）**：知道 `unbounded()` 走 list flavor，`Sender`/`Receiver` 只是一个壳，所有方法都 match flavor 转发。
- **u2-l4（阻塞与唤醒 context + waker）**：理解阻塞操作「登记 + park + 被 unpark」的三段式，以及 `SyncWaker`（`is_empty` 快速路径 + 锁协作）的语义。

几个需要先建立的直觉：

| 概念 | 直觉解释 |
|------|----------|
| 无界通道 | 容量没有上限，`capacity()` 返回 `None`，`is_full()` 永远返回 `false`。 |
| 分块链表 | 不是「每条消息一个堆节点」，而是「每块 `BLOCK_CAP` 条消息一个堆节点」，减少对分配器的压力、提升缓存命中。 |
| 无锁（lock-free） | 用 `compare_exchange` 推进游标，不用互斥锁保护队列；同一时刻只有一个线程能成功推进。 |
| 惰性回收 | 一个块只有当它里面**所有槽位都已被读出**时才释放；负责释放的可能是任意一个 reader。 |

如果你对 `UnsafeCell<MaybeUninit<T>>`、`Acquire`/`Release` 内存序还不熟，先记住一条主线：**发送方把消息写进槽位后用 `Release` 置 `WRITE` 位；接收方用 `Acquire` 看到 `WRITE` 位后才读槽位** —— 这条 happens-before 关系保证接收方一定能读到完整的消息。细节会在 4.4 节展开。

## 3. 本讲源码地图

本讲主要涉及两个文件：

| 文件 | 作用 |
|------|------|
| [src/flavors/list.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs) | list flavor 的全部实现：`Slot` / `Block` / `Position` / `Channel`，以及 `start_send` / `start_recv` / `write` / `read` / `Block::destroy` / 断开与丢弃逻辑。 |
| [src/waker.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/waker.rs) | `SyncWaker` 提供 `register` / `notify` / `disconnect`，是接收方阻塞与唤醒的载体。本讲复用 u2-l4 的结论，不重复展开。 |

此外会点到：

| 文件 | 作用 |
|------|------|
| [src/channel.rs:50-59](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L50-L59) | `unbounded()` 构造函数，经 `counter::new` 把 list `Channel` 计数化后塞进 `SenderFlavor::List` / `ReceiverFlavor::List`。 |
| [src/select.rs:24-31](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L24-L31) | `Token` 结构体，含一个 `list: ListToken` 字段，在「抢占游标」与「搬运数据」两步之间传递槽位定位信息。 |

## 4. 核心概念与源码讲解

### 4.1 数据结构总览：Slot / Block / Position / Channel

#### 4.1.1 概念说明

list flavor 的目标是一个**无界、多生产者多消费者（mpmc）**队列。最朴素的实现是「每条消息一个堆节点 + next 指针」的单链表，但这样每发一条消息就调用一次分配器，开销大、缓存不友好。

crossbeam-channel 的做法是**分块（chunking）**：把连续的多条消息打包到一个 `Block` 里，每个 `Block` 含 `BLOCK_CAP` 个 `Slot`；`Block` 之间用 `next` 指针串成链表。这样分配次数除以 `BLOCK_CAP`，对分配器的压力大幅降低。

队列整体由两个游标定位：

- `head`：接收方下一次要读的位置。
- `tail`：发送方下一次要写的位置。

两者各自记录「逻辑下标 `index` + 当前所在块指针 `block`」。

#### 4.1.2 核心流程

```text
                  Block 0                Block 1               Block 2
              ┌─────────────┐       ┌─────────────┐       ┌─────────────┐
head ──►      │ slot[0..30] │ next─►│ slot[0..30] │ next─►│ slot[0..30] │ next─► null
              │             │       │             │       │             │
tail ──►      └─────────────┘       └─────────────┘       └─────────────┘
```

- 生产者推进 `tail`，消费者推进 `head`。
- 当 `tail` 走到一个块的末尾，发送方分配新块并挂到 `next` 上。
- 当 `head` 走到一个块的末尾，接收方读取最后一个槽后，销毁旧块并把 `head` 推进到 `next`。

#### 4.1.3 源码精读

`Slot` 是最小存储单元，含消息本体与状态位：

```rust
struct Slot<T> {
    msg: UnsafeCell<MaybeUninit<T>>,
    state: AtomicUsize,
}
```

[src/flavors/list.rs:50-56](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L50-L56)：`msg` 用 `MaybeUninit` 因为槽位可能尚未写入；`UnsafeCell` 允许跨线程「看似可变」地访问，安全性靠 `state` 位 + 原子序手动维护（详见 4.4）。`wait_write` 在 `state` 上自旋直到出现 `WRITE` 位。

`Block` 是链表节点：

```rust
struct Block<T> {
    next: AtomicPtr<Block<T>>,
    slots: [Slot<T>; BLOCK_CAP],   // BLOCK_CAP = 31
}
```

[src/flavors/list.rs:71-77](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L71-L77)：注意 `slots` 长度是 `BLOCK_CAP = 31`，但一个块覆盖的「逻辑 lap」是 `LAP = 32`（见 4.2）。`next` 用 `AtomicPtr` 是因为发送方写、接收方读，需要原子访问。

`Position` 把「下标 + 块指针」打包：

```rust
struct Position<T> {
    index: AtomicUsize,
    block: AtomicPtr<Block<T>>,
}
```

[src/flavors/list.rs:141-148](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L141-L148)。注意 `index` 与 `block` 是**两个独立**的原子字段，它们的更新不是单步原子的，这是 list flavor 实现里最需要小心处理的并发细节（4.3 会看到发送方如何用「先 CAS index，再装 next 块」的顺序规避竞争）。

最后是 `Channel` 本体：

```rust
pub(crate) struct Channel<T> {
    head: CachePadded<Position<T>>,
    tail: CachePadded<Position<T>>,
    receivers: SyncWaker,
    _marker: PhantomData<T>,
}
```

[src/flavors/list.rs:177-189](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L177-L189)：注意三点：

1. `head` / `tail` 用 `CachePadded` 填充到缓存行大小，防止发送线程和接收线程在同一缓存行上各自更新 `head`/`tail` 时产生**伪共享（false sharing）**。
2. 只有 `receivers` 一个 `SyncWaker` —— 因为发送方永不阻塞，所以**没有 `senders` waker**。这是 list 与 array 的一个重要区别（array 满了发送方要阻塞）。
3. `PhantomData<T>` 提示 drop 时可能要 drop `T` 类型的消息。

`Channel::new` 把两个块的 `block` 指针都初始化为 `null`（懒分配：第一条消息发送时才分配第一个块），`index` 都为 0：

[src/flavors/list.rs:193-206](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L193-L206)。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：在源码层面确认 list 队列的存储层次。
2. **步骤**：打开 `src/flavors/list.rs`，分别找到 `Slot`、`Block`、`Position`、`Channel` 四个 `struct` 定义，数一下从外到内的层次。
3. **观察**：`Channel` 有两个 `Position`（head/tail），每个 `Position` 持有一个 `Block` 指针，每个 `Block` 持有 31 个 `Slot`，每个 `Slot` 持有一个 `MaybeUninit<T>`。
4. **预期结果**：你能在脑中画出 4.1.2 的分块链表示意图，并指出「堆分配的最小单位是 `Block`，不是单条消息」。

### 4.2 index 编码与 state 位标记

#### 4.2.1 概念说明

无锁队列必须解决两个经典问题：

1. **游标转圈歧义**：用 `usize` 表示下标，迟早会溢出回绕；回绕后「旧的下标」和「新的下标」数值相同，无法区分。array flavor 用 `stamp`（版本号）解决，list flavor 用「lap + 偏移」的位编码解决。
2. **槽位生命周期协调**：一个槽位可能处于「未写 / 已写未读 / 已读」等状态，多个线程要据此决定能否写、能否读、能否释放。list flavor 用三个状态位 `WRITE` / `READ` / `DESTROY` 标记。

#### 4.2.2 核心流程

**index 的位编码**。`index` 是一个 `usize`，最低位（bit 0）是元数据位 `MARK_BIT`，其余位右移 `SHIFT=1` 后才是「逻辑下标」：

```text
index  =  [    lap    :    offset    : MARK_BIT ]
            高若干位      低 5 位        bit 0

逻辑下标 = index >> SHIFT = index >> 1
块内偏移 = (index >> SHIFT) % LAP   // LAP = 32
块号 lap = (index >> SHIFT) / LAP
```

其中：

- `LAP = 32`：一个块覆盖 32 个逻辑位置（一个「圈」）。
- `BLOCK_CAP = LAP - 1 = 31`：但实际只有 31 个槽位存消息，第 32 个位置（offset == 31）是**边界哨兵**，触发块切换。
- `SHIFT = 1`：保留 1 个最低位给元数据。
- `MARK_BIT = 1`：在不同游标里含义不同——在 `tail` 里表示「通道已断开」；在 `head` 里表示「当前块后面还有更多块」。

为什么 `BLOCK_CAP = LAP - 1` 而不是 `= LAP`？因为要留一个 offset（31）作为「块结束」的信号：当发送方算出 `offset == BLOCK_CAP` 时，它知道要等下一个块被挂上；当 `offset + 1 == BLOCK_CAP` 时，它知道这次发送完就要预分配并挂上下一个块。

**state 的三个位**：

```text
WRITE   = 1   // 槽位已写入消息
READ    = 2   // 槽位已被读出
DESTROY = 4   // 所在块正在被销毁
```

- `WRITE`：发送方写完消息后置位（`Release`）；接收方等这个位（`Acquire`）才读。
- `READ`：接收方读完消息后置位；销毁逻辑用它判断「这个槽位已不再被使用」。
- `DESTROY`：销毁逻辑置位，把「继续销毁」的责任交接给仍在此块上的 reader（详见 4.5）。

#### 4.2.3 源码精读

常量定义：

[src/flavors/list.rs:30-47](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L30-L47)

```rust
const WRITE: usize = 1;
const READ: usize = 2;
const DESTROY: usize = 4;

const LAP: usize = 32;
const BLOCK_CAP: usize = LAP - 1;   // 31
const SHIFT: usize = 1;
const MARK_BIT: usize = 1;
```

`Slot::wait_write` 是 state 位配合内存序的典型用法：

[src/flavors/list.rs:59-65](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L59-L65)

```rust
fn wait_write(&self) {
    let backoff = Backoff::new();
    while self.state.load(Ordering::Acquire) & WRITE == 0 {
        backoff.snooze();
    }
}
```

`Acquire` 读 `WRITE` 位：一旦看到该位，就与发送方 `write` 里的 `Release` 写形成 happens-before，保证看到完整的消息内容。`Backoff::snooze` 在自旋时让出 CPU（不占用满核），是 u2-l4 提到的退避策略。

`MARK_BIT` 在断开时的用法（fetch_or）：

[src/flavors/list.rs:561-570](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L561-L570)

```rust
pub(crate) fn disconnect_senders(&self) -> bool {
    let tail = self.tail.index.fetch_or(MARK_BIT, Ordering::SeqCst);
    if tail & MARK_BIT == 0 {
        self.receivers.disconnect();
        true
    } else {
        false
    }
}
```

用 `fetch_or` 原子地「置位并取旧值」：只有第一个把 `MARK_BIT` 从 0 变 1 的调用才会真正执行 `receivers.disconnect()` 并返回 `true`，保证断开回调只触发一次（这与 u2-l2 讲的 `destroy` 标志同理）。

#### 4.2.4 代码实践（参数实验型）

1. **目标**：体会 `BLOCK_CAP = 31` 与 `LAP = 32` 的关系。
2. **步骤**：阅读 4.3 节 `start_send` 中两处对 `offset` 的判断：`offset == BLOCK_CAP`（等下一块）和 `offset + 1 == BLOCK_CAP`（预分配下一块）。
3. **思考**：如果把 `LAP` 设为 `32` 而 `BLOCK_CAP` 也设为 `32`（不留哨兵），发送方还怎么知道「该挂新块了」？
4. **预期结果**：理解「哨兵 offset」是块切换的触发点，所以 `slots` 数组长度（31）比 `LAP`（32）少 1。

### 4.3 发送：start_send 永不阻塞的 CAS 推进与分块增长

#### 4.3.1 概念说明

`unbounded()` 通道容量无限，因此**发送方永远不会因为「队列满」而阻塞**。`start_send` 的全部工作就是：抢占一个槽位（推进 `tail`），如果撞到块尾就增长新块。它只在「别的发送方正在挂新块」时短暂自旋等待，绝不在 waker 上登记 park。

`start_send` 永远返回 `true`（`send` 里用 `assert!(self.start_send(token))` 兜底），所以 `Sender` 的 `SelectHandle::is_ready` 直接返回 `true`，`register`/`unregister` 都是空操作——这是 list flavor「发送方永不阻塞」在 select 层面的体现。

#### 4.3.2 核心流程

`start_send` 的主循环（伪代码）：

```text
load tail.index, tail.block
loop:
    if tail 上有 MARK_BIT:           # 通道已断开
        token.block = null; return true   # write 会据此返回 Err
    offset = (tail >> SHIFT) % LAP
    if offset == BLOCK_CAP:          # 撞到边界哨兵，等别人挂好下一块
        snooze; reload; continue
    if offset + 1 == BLOCK_CAP and 未预分配:
        预分配 next_block             # 提前分配，缩短别人等待
    if block == null:                # 第一条消息，分配第一个块
        CAS tail.block: null -> new
        head.block = new             # 让接收方也能看到第一个块
        （失败则回收 new 并重试）
    new_tail = tail + (1 << SHIFT)
    if CAS tail.index: tail -> new_tail 成功:
        if offset + 1 == BLOCK_CAP:  # 这次发送后块满了，挂上 next_block
            tail.block = next_block
            tail.index += (1 << SHIFT)   # 额外跳过哨兵，进入新 lap
            block.next = next_block
        token.block = block; token.offset = offset
        return true
    else:
        reload; backoff.spin
```

关键设计：

1. **先 CAS `tail.index` 抢占下标**，抢占成功后才挂 `next` 块。`index` 是唯一的「先到先得」裁判，保证同一槽位只被一个发送方抢占。
2. **边界处的额外 `fetch_add`**：当 `offset + 1 == BLOCK_CAP`，发送方除了 CAS 推进的 `+2`，再 `fetch_add(1 << SHIFT)` 多跳一格，使下一个 offset 越过哨兵 31、回到新块的 0。
3. **懒分配第一个块**：`block == null` 时分配第一个块，并**同时**把它写进 `tail.block` 和 `head.block`——后者让等待的接收方立刻能看到块。

#### 4.3.3 源码精读

`start_send` 主体：

[src/flavors/list.rs:219-299](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L219-L299)

摘出最关键的几段。

断开快速路径（写 `null` 让 `write` 返回 `Err`）：

```rust
if tail & MARK_BIT != 0 {
    token.list.block = ptr::null();
    return true;
}
```

预分配下一块，缩短其他线程的等待：

```rust
if offset + 1 == BLOCK_CAP && next_block.is_none() {
    next_block = Some(Block::<T>::new());
}
```

懒分配第一个块，并同时初始化 `tail.block` 与 `head.block`：

```rust
if block.is_null() {
    let new = Box::into_raw(Block::<T>::new());
    if self.tail.block
        .compare_exchange(block, new, Ordering::Release, Ordering::Relaxed)
        .is_ok()
    {
        self.head.block.store(new, Ordering::Release);
        block = new;
    } else {
        next_block = unsafe { Some(Box::from_raw(new)) }; // 别人抢先了，回收
        ...
    }
}
```

CAS 推进 `tail.index`，成功后若处于块尾则挂新块：

```rust
match self.tail.index.compare_exchange_weak(
    tail, new_tail, Ordering::SeqCst, Ordering::Acquire,
) {
    Ok(_) => unsafe {
        if offset + 1 == BLOCK_CAP {
            let next_block = Box::into_raw(next_block.unwrap());
            self.tail.block.store(next_block, Ordering::Release);
            self.tail.index.fetch_add(1 << SHIFT, Ordering::Release); // 跳过哨兵
            (*block).next.store(next_block, Ordering::Release);
        }
        token.list.block = block as *const u8;
        token.list.offset = offset;
        return true;
    },
    Err(t) => { tail = t; ... backoff.spin(); }
}
```

`send`/`try_send` 用 `assert!` 依赖 `start_send` 必然成功：

[src/flavors/list.rs:441-452](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L441-L452)

```rust
pub(crate) fn send(&self, msg: T, _deadline: Option<Instant>)
    -> Result<(), SendTimeoutError<T>>
{
    let token = &mut Token::default();
    assert!(self.start_send(token));                 // 永不阻塞，必然 true
    unsafe { self.write(token, msg).map_err(SendTimeoutError::Disconnected) }
}
```

注意 `_deadline` 被忽略——这正是「无界通道的发送永远不会超时」的体现。

`Sender` 的 `SelectHandle` 实现也印证了「永不阻塞」：

[src/flavors/list.rs:752-780](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L752-L780) 里 `is_ready` 恒为 `true`，`register`/`unregister`/`unwatch` 都是空操作，`accept` 直接转发到 `try_select`。

#### 4.3.4 代码实践（跟踪型）

1. **目标**：跟踪连续 `send` 时块如何分配、`tail.index` 如何变化。
2. **步骤**：假设从空通道开始，逐条 `send`，按下表记录每次 `start_send` 进入时的 `tail` 与执行后的 `tail.index`（设 `SHIFT=1`，每条消息推进 `+2`）。

   | 第几条 send | 进入时 offset | 是否触发块切换 | 执行后 tail.index |
   |-------------|---------------|----------------|-------------------|
   | 1（首条）   | 0             | 否（分配首块） | 2 |
   | 2           | 1             | 否             | 4 |
   | …           | …             | 否             | … |
   | 31（末条）  | 30            | 是（挂 next）  | 60 → CAS→62 → fetch_add→64 |

3. **观察重点**：第 31 条发送时，`tail.index` 先被 CAS 从 60 推到 62，再被 `fetch_add(+2)` 推到 64，使下一 条的 `offset = (64>>1) % 32 = 0`（新块起点）。
4. **预期结果**：你能解释「为什么第 31 条消息会让 `tail.index` 多跳 2」——为了让 offset 越过哨兵 31 进入新块的 0。

> 说明：上表为根据源码逻辑推导的预期值，实际是否每条都精确对应，建议在本地用调试日志验证（见综合实践）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `start_send` 在 `block.is_null()` 分支里，CAS 成功后要同时 `head.block.store(new, Release)`？

**答案**：因为接收方也在等第一个块——若只初始化 `tail.block`，等待中的接收方会一直在 `block.is_null()` 上自旋；同时写 `head.block` 让它们立刻能看到块并开始接收。这是发送方与接收方在「通道首次初始化」这一刻的握手。

**练习 2**：多个发送方同时撞到块尾，会不会分配出多个 `next_block` 造成泄漏？

**答案**：不会。`tail.index` 的 CAS 是唯一裁判——只有一个发送方能成功 CAS 推进 `tail`，只有它会把预分配的 `next_block` 挂上；CAS 失败的发送方会在 `Err(t)` 分支重新加载，其预分配的块若来自 `block.is_null()` 失败分支会被 `Box::from_raw` 回收。注意 `next_block` 只在 `offset+1 == BLOCK_CAP` 时预分配且仅预分配一次（`is_none()` 守卫），不会重复。

### 4.4 接收与搬运：start_recv、空/断开判定、write/read

#### 4.4.1 概念说明

接收比发送复杂，因为接收方必须处理三种情况：

1. **有消息**：抢占一个槽位（推进 `head`），读出消息。
2. **空且未断开**：返回「未就绪」，由上层（`recv` 或 `select`）决定是否阻塞。
3. **空且已断开**：返回错误。

`start_recv` 负责前两步的判定与 `head` 的 CAS 推进；真正把消息搬出槽位的是 `read`；发送方对应的搬运是 `write`。`write` 末尾调用 `self.receivers.notify()` 唤醒因空而阻塞的接收方，形成「生产唤醒消费」的闭环。

#### 4.4.2 核心流程

**start_recv 主循环**（伪代码）：

```text
load head.index, head.block
loop:
    offset = (head >> SHIFT) % LAP
    if offset == BLOCK_CAP:          # 边界哨兵，等下一块挂上
        snooze; reload; continue
    new_head = head + (1 << SHIFT)
    if new_head 的 MARK_BIT == 0:    # 还不确定后面有没有更多块
        fence(SeqCst)
        tail = load tail.index (Relaxed)
        if head>>SHIFT == tail>>SHIFT:    # 头尾相同 → 空
            if tail 有 MARK_BIT:           #   且断开 → 返回错误
                token.block = null; return true
            else:                          #   未断开 → 未就绪
                return false
        if head 与 tail 不在同一块:          # 后面还有块
            new_head |= MARK_BIT
    if block == null:                # 首块尚未初始化，等
        snooze; reload; continue
    if CAS head.index: head -> new_head 成功:
        if offset + 1 == BLOCK_CAP:  # 读完了本块最后一个槽，切到 next
            next = block.wait_next()
            更新 head.block = next；重算 head.index（带 MARK_BIT）
        token.block = block; token.offset = offset
        return true
    else:
        reload; backoff.spin
```

**write（发送方搬运）**：把消息写进槽位，`fetch_or(WRITE, Release)`，再 `receivers.notify()`。

**read（接收方搬运）**：`wait_write()` 等消息落盘，`read` 出消息，然后处理块的销毁（见 4.5）。

#### 4.4.3 源码精读

空与断开的判定（这是 `start_recv` 最微妙的部分）：

[src/flavors/list.rs:340-361](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L340-L361)

```rust
if new_head & MARK_BIT == 0 {
    atomic::fence(Ordering::SeqCst);
    let tail = self.tail.index.load(Ordering::Relaxed);

    if head >> SHIFT == tail >> SHIFT {
        if tail & MARK_BIT != 0 {
            token.list.block = ptr::null();   // 空 + 断开 → 错误
            return true;
        } else {
            return false;                      // 空 + 未断开 → 未就绪
        }
    }

    if (head >> SHIFT) / LAP != (tail >> SHIFT) / LAP {
        new_head |= MARK_BIT;                  // 跨块，标记后面还有块
    }
}
```

要点：

- 「空」的判据是 `head>>SHIFT == tail>>SHIFT`（逻辑下标相等），而不是比较完整 `index`，因为要忽略 `MARK_BIT`。
- `fence(SeqCst)` 保证「读 tail」与发送方的写操作之间有合理的全局顺序，避免漏判空/非空。
- `MARK_BIT` 置于 `new_head`：当 head 与 tail 不在同一 lap，说明当前块后面至少还有一个块，置位让后续读到块尾时知道要去 `wait_next()`。

块尾切换（读最后一个槽后推进到 next）：

[src/flavors/list.rs:379-394](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L379-L394)

```rust
Ok(_) => unsafe {
    if offset + 1 == BLOCK_CAP {
        let next = (*block).wait_next();
        let mut next_index = (new_head & !MARK_BIT).wrapping_add(1 << SHIFT);
        if !(*next).next.load(Ordering::Relaxed).is_null() {
            next_index |= MARK_BIT;
        }
        self.head.block.store(next, Ordering::Release);
        self.head.index.store(next_index, Ordering::Release);
    }
    token.list.block = block as *const u8;
    token.list.offset = offset;
    return true;
},
```

注意：切到 `next` 块后，若 `next` 自己还有 `next`，就把 `MARK_BIT` 重新置上，保证「后面还有块」的信息一路传递。

`write` —— 发送方的搬运，以及生产唤醒消费：

[src/flavors/list.rs:302-318](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L302-L318)

```rust
pub(crate) unsafe fn write(&self, token: &mut Token, msg: T) -> Result<(), T> {
    if token.list.block.is_null() {
        return Err(msg);                       // 通道断开
    }
    let block = token.list.block.cast::<Block<T>>();
    let offset = token.list.offset;
    let slot = unsafe { (*block).slots.get_unchecked(offset) };
    unsafe { slot.msg.get().write(MaybeUninit::new(msg)) }   // 先写消息
    slot.state.fetch_or(WRITE, Ordering::Release);            // 再用 Release 置 WRITE
    self.receivers.notify();                                  // 唤醒等待的接收方
    Ok(())
}
```

内存序关键：**先 `write` 消息，再用 `Release` 置 `WRITE` 位**。接收方在 `read` 里用 `Acquire` 等 `WRITE` 位，于是「写消息」一定 happens-before「读消息」，保证可见性。

`read` —— 接收方的搬运：

[src/flavors/list.rs:406-430](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L406-L430)

```rust
pub(crate) unsafe fn read(&self, token: &mut Token) -> Result<T, ()> {
    if token.list.block.is_null() {
        return Err(());                          // 通道断开
    }
    let block = token.list.block as *mut Block<T>;
    let offset = token.list.offset;
    let slot = unsafe { (*block).slots.get_unchecked(offset) };
    slot.wait_write();                           // Acquire 等 WRITE
    let msg = unsafe { slot.msg.get().read().assume_init() };

    unsafe {
        if offset + 1 == BLOCK_CAP {
            Block::destroy(block, 0);            // 读到块尾，销毁本块
        } else if slot.state.fetch_or(READ, Ordering::AcqRel) & DESTROY != 0 {
            Block::destroy(block, offset + 1);   // 有人想销毁但本槽在用，接力销毁
        }
    }
    Ok(msg)
}
```

`read` 与 `write` 通过 `WRITE` 位 + `Acquire`/`Release` 配对，是 list flavor 并发正确性的核心（u3-l4 会专门讨论）。

最后看 `recv` 的阻塞形态，它复用了 u2-l4 的三段式：

[src/flavors/list.rs:466-515](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L466-L515)：先 `start_recv` 自旋几次；不行就 `Context::with` 里 `register` 到 `receivers`、复查就绪防丢失唤醒、`wait_until` 阻塞、被唤醒后按 `Selected` 决定 `unregister` 或完成。这与 array flavor 的 `recv` 结构一致，差别只在 `start_recv`/`read` 的实现。

#### 4.4.4 代码实践（可运行示例）

1. **目标**：直观验证 list 通道的发送不阻塞、接收在空时阻塞、断开后立即返回。
2. **步骤**：新建一个二进制 crate，加入 `crossbeam-channel` 依赖，运行下面的**示例代码**：

   ```rust
   // 示例代码：演示 list flavor 的基本收发与断开
   use std::thread;
   use crossbeam_channel::{unbounded, RecvTimeoutError};
   use std::time::Duration;

   fn main() {
       let (s, r) = unbounded();              // list flavor
       // 发送方永不阻塞：连发 100 万条也不会卡住
       for i in 0..1_000_000 {
           s.send(i).unwrap();
       }

       // 接收方在空时阻塞 —— 这里开个线程先等再发
       let s2 = s.clone();
       thread::spawn(move || {
           thread::sleep(Duration::from_millis(100));
           s2.send(42).unwrap();
       });
       assert_eq!(r.recv(), Ok(42));          // 阻塞到对方发送

       // 超时接收：空且未断开 → Timeout
       assert_eq!(r.recv_timeout(Duration::from_millis(50)),
                  Err(RecvTimeoutError::Timeout));

       drop(s);                               // 发送端全部断开
       // 排空剩余 100 万条后，recv 立刻返回错误，不再阻塞
       let mut n = 0;
       while r.recv().is_ok() { n += 1; }
       println!("drained {n} items, then recv returned disconnected");
   }
   ```

3. **观察**：连发 100 万条不阻塞（印证「无界 + 发送永不阻塞」）；`recv_timeout` 在空时返回 `Timeout`；`drop(s)` 后排空即返回断开错误。
4. **预期结果**：程序打印类似 `drained 1000000 items, then recv returned disconnected`。

#### 4.4.5 小练习与答案

**练习 1**：`write` 为什么必须**先写消息，再 `fetch_or(WRITE, Release)`**，顺序能反过来吗？

**答案**：不能。`Release` 写 `WRITE` 位的作用是「把之前的所有写（消息内容）发布给用 `Acquire` 读的线程」。若先置 `WRITE` 再写消息，接收方可能看到 `WRITE` 位却读到未初始化的数据。先写后置位，再靠 `Release`/`Acquire` 建立同步，才能保证接收方读到完整消息。

**练习 2**：`start_recv` 里「空」的判据是 `head>>SHIFT == tail>>SHIFT`，为什么不是 `head == tail`？

**答案**：因为 `head`/`tail` 的最低位（`MARK_BIT`）携带元数据（tail 上表示断开，head 上表示「后面还有块」），比较完整 `index` 会因这些元数据位不同而误判。比较 `>>SHIFT` 后的逻辑下标，才能正确反映「生产到哪、消费到哪」。

### 4.5 惰性回收与断开：Block::destroy、DESTROY 责任交接、discard_all_messages

#### 4.5.1 概念说明

链表队列必须回收已读空的块，否则内存泄漏。难点在于**并发**：当一个 reader 读完某块的最后一个槽、想销毁它时，可能另一个 reader 还在**更早的槽位**上 `wait_write`（消息尚未落盘）。此时不能直接 `Box::from_raw` 释放，否则那个 reader 会访问已释放内存。

list flavor 的解法是 **`DESTROY` 位 + 责任交接**：

- 想销毁块的线程逐个检查槽位：若某槽的 `READ` 位为 0（还没被读出/还在用），就给它打上 `DESTROY` 位然后**离开**，把销毁责任交给那个仍在此槽的 reader。
- reader 读完自己的槽后，发现 `DESTROY` 位被置，就**接管**销毁，从自己的下一个槽继续检查。

这样保证「一个块恰好被释放一次，且释放时没有任何线程还在用它」。

断开（disconnect）则分两种：

- **发送端断开**（所有 Sender drop）：置 `MARK_BIT`，唤醒接收方；已缓冲的消息仍可被接收，排空后 `recv` 返回错误。
- **接收端断开**（所有 Receiver drop）：置 `MARK_BIT`，并调用 `discard_all_messages` **急切丢弃**所有残留消息、释放所有块。

#### 4.5.2 核心流程

**Block::destroy(this, start)**（伪代码）：

```text
for i in start .. BLOCK_CAP-1:        # 注意不含最后一个槽（它在块切换时由调用方负责）
    slot = this.slots[i]
    if slot.state 的 READ 位 == 0      # 这个槽还在用 / 还没读
        且 fetch_or(DESTROY) 后 READ 位仍 == 0:
            return                      # 把销毁责任留给该 reader
# 所有槽都已被读出 → 安全释放
drop(Box::from_raw(this))
```

**reader 接力销毁**（在 `read` 末尾）：

```text
if offset+1 == BLOCK_CAP:              # 读了本块最后一个槽
    Block::destroy(block, 0)           # 从头尝试销毁整个块
elif fetch_or(READ) 看到 DESTROY:       # 我读这槽时有人想销毁，但本槽还在用
    Block::destroy(block, offset+1)    # 我读完了，从我下一个槽接力销毁
```

**接收端断开 → discard_all_messages**：把 `head.block` 取走（`swap` 成 null，避免与正在初始化首块的发送方冲突），沿链表逐槽 `wait_write` + `assume_init_drop` 丢弃消息，并 `Box::from_raw` 释放每个块。

#### 4.5.3 源码精读

`Block::destroy` —— 惰性回收的核心：

[src/flavors/list.rs:119-137](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L119-L137)

```rust
unsafe fn destroy(this: *mut Self, start: usize) {
    for i in start..BLOCK_CAP - 1 {
        let slot = unsafe { (*this).slots.get_unchecked(i) };
        if slot.state.load(Ordering::Acquire) & READ == 0
            && slot.state.fetch_or(DESTROY, Ordering::AcqRel) & READ == 0
        {
            return;                  // 该槽还在用，留给它读完后接力
        }
    }
    drop(unsafe { Box::from_raw(this) });  // 所有槽都已读出，安全释放
}
```

注释里特别说明：循环只到 `BLOCK_CAP - 1`，因为**最后一个槽**的销毁已经由「读到块尾切换块」的调用路径（`offset+1 == BLOCK_CAP`）开启，这里不必重复处理。`fetch_or(DESTROY, AcqRel)` 的返回值（旧状态）若不含 `READ`，说明此刻此槽尚未被读出，可能正有 reader 在 `wait_write`，于是 `return` 让该 reader 接力。

`read` 末尾的两条销毁路径：

[src/flavors/list.rs:421-427](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L421-L427)

```rust
unsafe {
    if offset + 1 == BLOCK_CAP {
        Block::destroy(block, 0);                // 读到块尾：尝试销毁整块
    } else if slot.state.fetch_or(READ, Ordering::AcqRel) & DESTROY != 0 {
        Block::destroy(block, offset + 1);       // 接力销毁
    }
}
```

第二条分支的含义：我在读这个槽时，有人调过 `destroy` 并给本槽打了 `DESTROY`（因为当时 `READ` 还没置位）；现在我读完了、置上 `READ`，发现 `DESTROY` 在，于是**接管**销毁，从 `offset+1` 继续检查。

`disconnect_receivers` + `discard_all_messages` —— 接收端断开时急切回收：

[src/flavors/list.rs:575-586](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L575-L586)：置 `MARK_BIT` 后，若由本次调用触发断开，则调用 `discard_all_messages`。

[src/flavors/list.rs:591-653](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L591-L653)：核心是沿链表遍历，对每个消息槽 `wait_write` + `assume_init_drop`，对每个块尾走 `wait_next` + `Box::from_raw`。其中 `head.block.swap(null)` 很关键——用 `swap` 而非 `load`，避免与「正在初始化首块的发送方」互相覆盖（注释明确解释了这个并发场景），迟到的首块分配由发送方在 `Drop` 里回收。

`Channel::drop`（所有句柄释放后）也做类似遍历，把剩余消息和块清掉：

[src/flavors/list.rs:673-708](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L673-L708)。

#### 4.5.4 代码实践（源码阅读型）

1. **目标**：理解 `DESTROY` 位如何把销毁责任安全交接。
2. **步骤**：阅读 `Block::destroy`（L120-L137）和 `read` 末尾（L421-L427），按下面场景填空。

   **场景**：一个块有 31 个槽（下标 0..30）。reader A 正在读下标 5（消息尚未落盘，在 `wait_write`），reader B 读完下标 30（块尾），调用 `Block::destroy(block, 0)`。

   - `destroy` 从 i=0 开始扫描。i=0..4 的 `READ` 位都为 1（已读），跳过；i=5 的 `READ` 位为 0 → 给 slot[5] 打 `DESTROY` 位，`return`。
   - reader A 的 `wait_write` 返回，读出消息，执行 `fetch_or(READ)`，发现返回值含 `DESTROY` → 调用 `Block::destroy(block, 6)` 接力。
   - i=6..29 都已读，最终 `Box::from_raw` 释放块。

3. **观察**：销毁不是由「想销毁的 B」完成，而是由「最后一个离开的 reader」完成；任何时刻都没有线程在访问已释放内存。
4. **预期结果**：你能向别人讲清「为什么 `destroy` 遇到 `READ==0` 的槽要 `return`，以及 `read` 里 `fetch_or(READ) & DESTROY` 这条分支为什么是接力的触发点」。

#### 4.5.5 小练习与答案

**练习 1**：`Block::destroy` 的循环上界为什么是 `BLOCK_CAP - 1` 而不是 `BLOCK_CAP`？

**答案**：因为最后一个槽（下标 `BLOCK_CAP-1 = 30`）的销毁已经由「读到块尾」的路径触发：`read` 在 `offset + 1 == BLOCK_CAP` 时直接调用 `Block::destroy(block, 0)`，它本身就已经把整块纳入销毁流程。`destroy` 内部若再扫描到最后一个槽并处理，会与该路径重复。注释也写明「不必给最后一个槽设 `DESTROY`，因为它已经开始了块的销毁」。

**练习 2**：`discard_all_messages` 里为什么用 `head.block.swap(null)` 而不是 `load`？

**答案**：因为发送方可能正在「首条消息」路径上 CAS 初始化第一个块（`tail.block` 与 `head.block`）。若接收方只 `load` 再覆盖写，可能与发送方的 `store` 竞争而丢失其中一个写入。`swap` 原子地「取走并置 null」，发送方若稍后 CAS 会看到 null 而重新分配，其迟到的块由发送方在 `Drop` 中回收，从而避免内存泄漏与竞争。

## 5. 综合实践

把本讲知识串起来，做一个「加日志跟踪 list 内部状态」的小任务。

**任务**：编写一个多生产者程序，向一个 `unbounded` 通道并发发送恰好 `N * 31`（例如 `4 * 31 = 124`）条消息，用一个接收方全部收走。在收发过程中，结合源码回答：

1. 通道总共分配了几个 `Block`？（提示：`\lceil 消息数 / 31 \rceil`，但要看首块懒分配与块切换的时机。）
2. 收完最后一条消息后，哪些块被 `Block::destroy` 释放？由谁触发？
3. 若在收完前 `drop` 所有 Sender，接收方还能不能收完缓冲的消息？为什么？

**步骤建议**：

1. 用 `crossbeam_utils::thread::scope` 起若干个生产者线程，每个发一部分消息；主线程用一个接收方 `r.iter().collect()` 收集。
2. 对照源码的 `BLOCK_CAP = 31`、`start_send` 的块切换、`read` 的销毁路径，逐题推导。
3. （进阶）若想直接观察，可临时在 `src/flavors/list.rs` 的 `Block::new` 和 `Block::destroy` 里各加一行 `eprintln!`（**仅用于本地学习，勿提交**），重新 `cargo build` 后运行你的程序，数日志行数验证推导。注意这会修改源码，学习后请还原。

**预期结论**：

1. 块数 ≈ \(\lceil \text{消息数} / 31 \rceil\)。例如 124 条消息对应 4 个块（124 / 31 = 4）。
2. 收完最后一条时，前 3 个块在读到各自块尾时被销毁；第 4 个块（最后一个）若没有后续块，其销毁发生在通道 `Drop` 时（见 [src/flavors/list.rs:673-708](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L673-L708)）。
3. 能收完。发送端断开只置 `MARK_BIT` 并唤醒接收方（[src/flavors/list.rs:561-570](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L561-L570)），缓冲的消息仍在链表里；接收方在 `start_recv` 里走「非空」分支继续读，直到 `head>>SHIFT == tail>>SHIFT` 且 `tail` 带 `MARK_BIT`，才返回断开错误（[src/flavors/list.rs:345-355](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L345-L355)）。

## 6. 本讲小结

- list flavor 用**分块链表**实现无界 mpmc 队列：堆分配的最小单位是含 31 个槽的 `Block`，而非单条消息。
- `index` 用位编码同时携带「逻辑下标」与元数据：最低位 `MARK_BIT` 在 tail 表示断开、在 head 表示「后面还有块」；offset 31 是块切换哨兵，故 `BLOCK_CAP = LAP - 1 = 31`。
- 发送方**永不阻塞**：`start_send` 只用 CAS 推进 `tail`、按需增长新块，永远返回 `true`；`Sender` 的 `SelectHandle` 也因此恒就绪。
- 接收方用 `start_recv` 的 CAS 推进 `head`，靠 `head>>SHIFT == tail>>SHIFT` 判空、靠 `tail` 的 `MARK_BIT` 判断开；`write` 先写消息再 `Release` 置 `WRITE`，`read` 用 `Acquire` 等 `WRITE`，建立消息可见性。
- 生产唤醒消费：`write` 末尾 `receivers.notify()`；接收方阻塞复用 u2-l4 的「register + wait_until + unregister」三段式，但只有一个 `receivers` waker（无 senders waker）。
- 内存回收用 **`DESTROY` 位 + 责任交接**：想销毁块的线程给「仍在用的槽」打 `DESTROY` 后离开，最后一个离开该槽的 reader 接力完成 `Box::from_raw`，保证「恰好释放一次且无人正在使用」。

## 7. 下一步学习建议

- **u3-l1（select 核心算法）**：本讲的 `Sender`/`Receiver` 的 `SelectHandle` 实现是 select 算法的对接点，读完 u3-l1 你会明白 `try_select`/`register`/`accept`/`is_ready` 如何被 `run_select` 调度。
- **u3-l4（内存序与 unsafe 正确性）**：本讲多次出现 `Release`/`Acquire`/`SeqCst`/`AcqRel`，那篇讲义会系统解释每种选择的理由，并把 `list.rs` 的所有 `unsafe` 块按「指针解引用 / `MaybeUninit` / `Box::from_raw`」分类。
- **对比 array flavor（u2-l5）**：list 与 array 是两种风格迥异的有界/无界实现——array 用 `stamp` 版本号的环形数组、发送方会因满而阻塞；list 用 lap 编码的链表、发送方永不阻塞。对照阅读能加深对「无锁队列设计空间」的理解。
- 继续阅读源码时，建议带着这条主线：**`index` 的 CAS 是唯一裁判，`state` 位 + 内存序协调槽位生命周期，`DESTROY` 位负责安全回收**。
