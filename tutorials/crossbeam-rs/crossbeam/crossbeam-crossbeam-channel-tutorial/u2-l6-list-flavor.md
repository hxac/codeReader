# 无界链表：list flavor

## 1. 本讲目标

本讲深入 `src/flavors/list.rs`，讲解 `unbounded()` 通道（无界通道）的底层实现。读完本讲，你应当能够：

- 说出 list flavor 如何用「分块链表（block 链表）」组织无界队列，以及它和 array flavor（环形数组）的核心差异。
- 看懂 `index` 的位编码（`LAP` / `BLOCK_CAP` / `SHIFT` / `MARK_BIT`）和 `Slot` 的三个状态位（`WRITE` / `READ` / `DESTROY`）。
- 跟踪一次 `send` 如何用 CAS 推进 `tail`、如何在块满时分配并链接新块，并理解为什么**发送方永不阻塞**。
- 跟踪一次 `recv` 如何用 CAS 推进 `head`、如何判定「空」与「断开」、如何在跨块时跳转，以及 `write`/`read` 如何配合 `SyncWaker::notify` 形成生产消费闭环。
- 解释 `Block::destroy` 的惰性回收机制：`DESTROY` 位如何把「已读空的块」的释放责任安全地交接给仍在使用该块的最后一个 reader。

## 2. 前置知识

本讲假设你已经读过：

- **u1-l2**（`unbounded` / `bounded` 构造函数按容量分流：`unbounded → list`）。
- **u2-l1**（六种 flavor 总览与「公共类型壳 + 按 flavor 分发」的架构母题）。
- **u2-l4**（`Context` 的 park/unpark、`Selected` 状态机、`SyncWaker` 的 register/notify/disconnect）。

先建立两点直觉，再进入源码：

1. **无界队列不能预分配固定大小的数组。** array flavor 用固定大小的环形数组（`bounded(cap)`），但 `unbounded()` 的容量是无限的，消息数量事先未知，所以必须能动态增长——list flavor 的解法是把队列拆成一条「固定大小的 block 链表」，需要时再追加新 block。
2. **「分块」是为了少压榨分配器、提升缓存命中。** 如果每条消息都 `Box` 一次，分配开销巨大；如果一个队列就是一个无限大的数组，又无法动态增长。折中是：每个 block 装固定 31 条消息（`BLOCK_CAP`），装满就链上下一个 block。

关键术语（本讲会反复用到）：

| 术语 | 含义 |
| --- | --- |
| block | 链表中的一个节点，固定容纳 `BLOCK_CAP=31` 条消息 |
| slot | block 内的一个槽位，存放单条消息 + 状态位 |
| lap | 「一圈」，指 index 在一个 block 内走完一轮（共 32 个位置，其中 31 个装消息） |
| `head` | 接收游标（下一个要读的位置） |
| `tail` | 发送游标（下一个要写的位置） |
| `WRITE` / `READ` / `DESTROY` | slot 的三个状态位 |
| `MARK_BIT` | index 的最低位，在 head/tail 中含义不同 |

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/flavors/list.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs) | 本讲主角：无界链表通道的完整实现 |
| [src/waker.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/waker.rs) | `SyncWaker`：接收者阻塞队列，`write` 末尾调 `notify` 唤醒接收者 |
| [src/channel.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs) | `unbounded()` 把 `list::Channel` 包进 `counter` 再塞进 `SenderFlavor::List` / `ReceiverFlavor::List` |
| [src/select.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs) | `Token`、`SelectHandle` trait，list 通过 `ListToken` 字段与 select 对接 |
| [tests/list.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/tests/list.rs) | list flavor 的语义测试，本讲实践会用到 |

---

## 4. 核心概念与源码讲解

本讲拆成 5 个最小模块：

1. **数据结构与索引编码**：`Block` / `Slot` / `Position` / `Channel` 与位编码、状态位。
2. **发送**：`start_send` + `write`（CAS 推进 tail、分块增长、写入与唤醒）。
3. **接收**：`start_recv` + `read`（CAS 推进 head、跨块跳转、读取消息）。
4. **惰性回收**：`Block::destroy` 与 `DESTROY` 位交接。
5. **阻塞、断开与销毁**：`recv` 阻塞循环、senders 永不阻塞、`disconnect_*` / `discard_all_messages` / `Drop`。

---

### 4.1 数据结构与索引编码：Block / Slot / Position / Channel

#### 4.1.1 概念说明

list flavor 的核心数据结构是一条「单向链表」，链表节点是 `Block`，每个 `Block` 内部是一个固定大小的 `Slot` 数组。整条通道只需要两个游标：

- `tail`：发送方写入位置（生产者往前推）。
- `head`：接收方读取位置（消费者往前推）。

新消息总是写到 `tail`，旧消息总是从 `head` 读出。当 `head` 追上 `tail`（同一圈同一偏移），队列就空了。由于队列无界，`tail` 可以无限往前走，走过的旧 block 由接收者读完最后一个 slot 后负责释放。

#### 4.1.2 核心流程

先看位编码常量，它定义了 index 如何同时承载「第几圈 / 块内第几个 / 标志位」三层信息：

- `LAP = 32`：一圈有 32 个位置。
- `BLOCK_CAP = LAP - 1 = 31`：但只有 31 个位置装消息，第 32 个位置（offset == 31）是「块边界」哨兵，触发安装下一个 block。
- `SHIFT = 1`：最低 1 位保留给元数据。
- `MARK_BIT = 1`：即 bit0。它在 head 和 tail 里含义不同：
  - 在 **tail** 里置位 → 通道已断开（disconnected）。
  - 在 **head** 里置位 → 当前 head block **不是最后一个**（后面还有 block），读完可以释放它。

index 的布局（每条消息步长为 `1 << SHIFT = 2`，保护 bit0）：

\[
\text{index} = \underbrace{\text{lap}}_{\text{高若干位}} \times 64 \;+\; \underbrace{\text{offset}\,(0..31)}_{\text{中间 5 位，但占 6 个 bit 步长}} \times 2 \;+\; \underbrace{\text{MARK\_BIT}}_{\text{bit0}}
\]

代码里取偏移量的方式是：

\[
\text{offset} = (\text{index} \gg \text{SHIFT}) \bmod \text{LAP} = (\text{index} \gg 1) \bmod 32
\]

每个 slot 有三个状态位（也是 `usize` 打包进 `AtomicUsize`）：

- `WRITE = 1`：消息已写入该 slot。
- `READ = 2`：消息已从该 slot 读出。
- `DESTROY = 4`：有人想销毁该 block，但当前 slot 正被某个 reader 使用，请它读完后接力销毁。

#### 4.1.3 源码精读

常量定义：

[list.rs:30-47](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L30-L47) — 定义 `WRITE/READ/DESTROY` 三个状态位，以及 `LAP/BLOCK_CAP/SHIFT/MARK_BIT` 四个布局常量，并注释说明 `MARK_BIT` 在 head/tail 中的双重含义。

`Slot` 结构：

[list.rs:49-66](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L49-L66) — `Slot<T>` 用 `UnsafeCell<MaybeUninit<T>>` 存消息（多线程可写、未初始化时合法），用 `AtomicUsize` 存状态位；`wait_write` 用 `Acquire` 自旋等待 `WRITE` 位置位（保证读到对方已写入的消息）。

`Block` 结构：

[list.rs:68-105](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L68-L105) — `Block<T>` 持 `next: AtomicPtr<Block<T>>`（链表下一节点）和 `slots: [Slot<T>; BLOCK_CAP]`（31 个槽）。`Block::new` 用 `Global.allocate_zeroed` 零分配一块内存再 `Box::from_raw` 包装，因为 `AtomicPtr/AtomicUsize/MaybeUninit` 都允许零初始化（注释 [1]-[4] 逐字段论证了安全性）。

`Position` 与 `Channel`：

[list.rs:140-206](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L140-L206) — `Position<T>` 把 `index: AtomicUsize` 与 `block: AtomicPtr<Block<T>>` 打包；`Channel<T>` 持 `head` / `tail` 两个 `CachePadded<Position<T>>`（防伪共享）、一个 `receivers: SyncWaker`（阻塞接收者队列），以及仅用于 drop 语义的 `_marker: PhantomData<T>`。新建时 head/tail 的 block 都初始化为空指针 `null_mut()`，index 为 0——第一个 sender 才真正分配首块。

#### 4.1.4 代码实践

**实践目标**：搞清楚「一个 block 到底装几条消息、index 步长是多少」。

1. 打开 `src/flavors/list.rs` 顶部常量区。
2. 用计算器或纸笔验证：
   - `BLOCK_CAP = LAP - 1 = 31`，所以一个 block 装 31 条消息。
   - 每条消息让 `tail` 步进 `1 << SHIFT = 2`。
   - 走完一个 block（31 条消息）后，index 还要额外步进 2 跨过「边界哨兵」位置（offset 31），即一圈总步进 `LAP << SHIFT = 64`。
3. **需要观察的现象**：当你向一个 `unbounded()` 通道连续发送 31 条消息时，仍处在第一个 block；发送第 32 条时，会触发分配并链接第二个 block（详见 4.3）。

**预期结果**：能口算出「发送第 N 条消息时位于第 `⌊(N-1)/31⌋` 个 block 的第 `(N-1) mod 31` 个 slot」。（运行结果可由下方 4.7 综合实践验证。）

#### 4.1.5 小练习与答案

**练习 1**：为什么 `BLOCK_CAP = LAP - 1`，而不是直接让一个 block 装满 32 条？

**答案**：需要一个额外的「边界哨兵」位置（offset == `LAP-1 == 31`）来表示「块结束、该安装下一个 block 了」。发送方看到 offset 到达 `BLOCK_CAP` 就知道要等/装下一块，接收方同理用它来跨块跳转。这避免了「偏移量刚好回绕到 0 时无法区分是新块第一格还是旧块第一格」的歧义。

**练习 2**：`MARK_BIT` 在 head 和 tail 中各代表什么？

**答案**：在 tail 中置位代表「通道已断开」（disconnected）；在 head 中置位代表「当前 head block 不是最后一个，后面还有 block」。两者复用同一个 bit0，但语义按游标区分。

---

### 4.2 发送：start_send + write（CAS 推进 tail、分块增长）

#### 4.2.1 概念说明

list flavor 是**无界**通道，意味着发送方永远不会因为「队列满」而阻塞——`start_send` 总能成功（要么抢占一个 slot，要么发现通道已断开）。这与 array flavor 的「满了要阻塞等待 receiver」截然不同。发送分两步：

1. `start_send`：用 CAS 把 `tail` 往前推一格，**预留**一个 slot（把 block 指针和 offset 写进 `token`）。这一步只占位，还没写消息。
2. `write`：把消息真正写进预留的 slot，置 `WRITE` 位，并唤醒一个阻塞的接收者。

为什么要拆两步？因为 `select!` 机制要求「先抢占资源、再决定是否完成」（见 u3-l1）。普通 `send` 也复用这条路径：`start_send` 抢占后必然 `write`。

#### 4.2.2 核心流程

`start_send` 的主循环（伪代码）：

```
load tail.index, tail.block
loop:
    if tail 的 MARK_BIT 置位 → 通道断开：token.block = null，返回 true
    offset = (tail >> 1) % 32

    if offset == 31 (BLOCK_CAP):  # 块边界，下一块还没装好
        snooze; reload; continue

    if offset == 30 且 没预分配下一块:  # 快到块尾，提前分配下一块
        next_block = Block::new()

    if block 是 null:  # 通道首个消息，分配首块
        CAS tail.block: null → new
        成功则 head.block 也存 new（让 receiver 看见）
        失败则回收 new，reload，continue

    new_tail = tail + 2
    CAS tail.index: tail → new_tail
        成功:
            if offset == 30:  # 写的是本块最后一格，安装下一块
                tail.block = next_block
                tail.index += 2  # 跨过边界哨兵
                block.next = next_block  # 链接！
            token 记录 block + offset
            返回 true
        失败:
            reload tail；spin 退避；重试
```

关键设计点：

- **预分配下一块**：在 CAS 之前就把下一块 `Block::new()` 分配好，这样抢到尾格的线程能立刻把它链上，让其他 sender 等待时间最短。
- **首块安装要同时更新 head.block**：因为 receiver 一开始看到的 `head.block` 是 null，必须让 sender 在装好首块后通知 receiver（`self.head.block.store(new, Release)`）。
- **CAS 失败要回收预分配的块**：如果首块 CAS 失败（别的线程抢先装了），要把刚 `Box::into_raw` 的块用 `Box::from_raw` 收回来，避免内存泄漏。

`write` 非常短：把消息写进 slot，`fetch_or(WRITE, Release)` 置位，然后 `receivers.notify()` 唤醒一个阻塞接收者。

#### 4.2.3 源码精读

`start_send` 全文：

[list.rs:218-299](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L218-L299) — 发送预留循环。重点看三段：

- [list.rs:226-230](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L226-L230)：检测 `tail & MARK_BIT`（断开），返回 `token.block = null`。
- [list.rs:243-268](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L243-L268)：预分配下一块、首块安装（含 CAS 失败时 `Box::from_raw` 回收）。
- [list.rs:270-298](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L270-L298)：CAS 推进 `tail.index`，成功后若写的是本块最后一格，则安装并链接下一块（`(*block).next.store(next_block, Release)`）。

`write`：

[list.rs:301-318](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L301-L318) — 写消息、`fetch_or(WRITE, Release)`、`self.receivers.notify()`。`token.block.is_null()` 表示 `start_send` 检测到断开，直接 `Err(msg)` 把消息还回去。

上层 `send` / `try_send`：

[list.rs:432-452](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L432-L452) — `send` 用 `assert!(self.start_send(token))`（发送永不失败抢占，故可断言），再 `write`；注意 `send` 的 `_deadline` 参数被忽略——这正体现了「发送方永不阻塞」，超时毫无意义。`try_send` 把 `SendTimeoutError` 归一化为 `TrySendError`，其中 `Timeout` 分支用 `unreachable!()` 消除（同因）。

唤醒链路（承接 u2-l4）：

[waker.rs:224-237](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/waker.rs#L224-L237) — `SyncWaker::notify` 先用 `is_empty` 原子快速路径判断有没有阻塞者，没有就直接返回（无锁）；有才加锁调 `try_select` 选中一个「别的线程」的接收者并 unpark。

#### 4.2.4 代码实践

**实践目标**：跟踪「连续发送」时新 block 如何分配与链接、`tail.index` 如何用 CAS 推进。

1. 在 [src/flavors/list.rs:252](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L252)（首块分配）和 [list.rs:281-285](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L281-L285)（安装并链接下一块）两处各加一行临时日志，例如：

   ```rust
   // 示例代码：仅用于观察，验证后请删除
   eprintln!("[send] 分配首块");
   // 以及
   eprintln!("[send] 链接新 block，offset={}", offset);
   ```

   （标注为「示例代码」，非项目原有内容；验证完务必还原，不要提交。）

2. 写一个小程序连续 `send` 70 条消息（`70 = 2*31 + 8`，会经历首块分配 + 两次块增长）。

3. **需要观察的现象**：
   - 首块分配日志应只打印 1 次（第 1 条消息时）。
   - 链接新 block 日志应打印 2 次（第 31 条写完、第 62 条写完时，即 offset==30）。

**预期结果**：70 条消息产生 1 次「分配首块」+ 2 次「链接新 block」，共 3 个 block（第三个 block 用了 8 格）。**待本地验证**：日志次数与并发情况有关，单线程发送时如上；多线程并发时首块 CAS 可能竞争失败，回收日志也会出现。

#### 4.2.5 小练习与答案

**练习 1**：`start_send` 在 offset == 30（`BLOCK_CAP-1`）时为什么要「提前」分配 `next_block`，而不是等 CAS 成功后再分配？

**答案**：为了让抢到尾格的线程能立刻把下一块链接上去（`(*block).next.store(next_block, ...)`），缩短其他 sender 在 `wait_next` 上的等待。如果 CAS 后才分配，期间所有想写入下一块的线程都得空转。预分配把耗时操作挪到 CAS 之前，是一种降低临界区的优化。

**练习 2**：为什么 `send` 的签名里有 `_deadline: Option<Instant>` 却完全不用？

**答案**：为了和 array/zero flavor 共享同一套上层 API（`chan.send(msg, deadline)`）。list 是无界通道，发送永不阻塞，所以超时没有意义——`start_send` 必然立即返回 true（抢占成功或检测到断开），不存在「等到超时」的情况，故参数被忽略、`try_send` 里 `Timeout` 分支用 `unreachable!()`。

---

### 4.3 接收：start_recv + read（CAS 推进 head、跨块跳转）

#### 4.3.1 概念说明

接收和发送对称：`start_recv` 用 CAS 推进 `head`，`read` 取出消息并置 `READ` 位。但接收比发送多了三件事：

1. **要判定「空」**：当 `head` 追上 `tail`（同圈同偏移），队列空，返回 `false`（表示「现在没准备好」），由上层决定是否阻塞。
2. **要判定「断开」**：如果空且 `tail` 的 `MARK_BIT` 置位，说明发送方全部断开且没残留消息，返回 `token.block = null`（上层据此返回 `Disconnected`）。
3. **要跨块跳转**：当读到本块最后一格（offset == 30），要把 `head.block` 推进到下一个 block，并决定是否给新 head 置 `MARK_BIT`。

#### 4.3.2 核心流程

`start_recv` 主循环（伪代码）：

```
load head.index, head.block
loop:
    offset = (head >> 1) % 32

    if offset == 31:  # 块边界，等下一块装好
        snooze; reload; continue

    new_head = head + 2
    if new_head 的 MARK_BIT == 0:   # 当前 head 块「可能是最后一个」
        fence(SeqCst)
        tail = tail.index.load(Relaxed)
        if head>>1 == tail>>1:      # head==tail：空
            if tail 的 MARK_BIT:     # 空 且 断开
                token.block = null; 返回 true
            else:
                返回 false           # 空但没断开：未就绪
        if head 与 tail 不在同一 block:
            new_head |= MARK_BIT    # 标记：head 块不是最后一个

    if block == null:  # 首条消息还没被 sender 初始化
        snooze; reload; continue

    CAS head.index: head → new_head
        成功:
            if offset == 30:  # 读的是本块最后一格，跨到下一块
                next = block.wait_next()
                next_index = (new_head & !MARK_BIT) + 2
                if next.next != null: next_index |= MARK_BIT
                head.block = next
                head.index = next_index
            token 记录 block + offset
            返回 true
        失败:
            reload; spin; 重试
```

关键点：

- **`MARK_BIT` 在 head 的设置时机**：当 head 与 tail 不在同一个 block（说明 tail 已经走到后面的 block，head 块已经「过时」），就给 head 置 `MARK_BIT`，表示「当前 head 块不是最后一个」——读完可以安全销毁。
- **跨块后是否保留 `MARK_BIT`**：跨到 `next` 块后，看 `next.next` 是否非空来决定（[list.rs:384-386](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L384-L386)）。
- **`SeqCst` fence**：在比较 head/tail 前插一道全序栅栏，配合 `tail.index` 的 `SeqCst` 写入，保证断开与可见性的正确同步。

`read` 也很短：`wait_write` 自旋等消息写入，读出消息，然后判断是否触发销毁（见 4.4）。

#### 4.3.3 源码精读

`start_recv` 全文：

[list.rs:320-403](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L320-L403) — 接收预留循环。重点：

- [list.rs:340-361](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L340-L361)：空/断开判定 + 跨块 `MARK_BIT` 设置。注意空判定用 `head >> SHIFT == tail >> SHIFT`（比较去掉标志位的逻辑索引）。
- [list.rs:372-401](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L372-L401)：CAS 推进 `head.index`，成功后若读到本块最后一格则跨块跳转。

`read`：

[list.rs:405-430](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L405-L430) — `wait_write` 等待消息写入（`Acquire`），`slot.msg.get().read().assume_init()` 读出消息，随后进入销毁判定（见 4.4）。

上层 `try_recv` / `recv`：

[list.rs:454-515](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L454-L515) — `try_recv` 调一次 `start_recv`：返回 false 即 `Empty`，返回 true 且 `read` 失败即 `Disconnected`。`recv` 是「自旋重试 → 超时检查 → register 到 `receivers` → `wait_until` 阻塞 → 唤醒后 unregister 并重试」的完整阻塞循环（详见 4.5）。

#### 4.3.4 代码实践

**实践目标**：跟踪一次跨块接收，看 `head` 如何从 block A 跳到 block B。

1. 在 [list.rs:382](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L382)（`let next = (*block).wait_next();`）处加临时日志：

   ```rust
   // 示例代码：仅用于观察，验证后请删除
   eprintln!("[recv] 跨块：head.block 从旧块跳到下一块");
   ```

2. 先 `send` 35 条消息（跨越第 1 个 block 的 31 格 + 第 2 个 block 的 4 格），再连续 `recv` 35 次。

3. **需要观察的现象**：跨块日志应恰好打印 1 次——发生在接收第 31 条消息（读完第 1 个 block 的最后一格 offset==30）时。

**预期结果**：35 条消息接收过程中，跨块日志打印 1 次（第 31 次 recv 时）。这印证「读到本块最后一格才跨块」。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`start_recv` 在什么条件下返回 `false`？返回 `false` 后上层 `try_recv` 和 `recv` 分别怎么处理？

**答案**：当 `head == tail`（逻辑索引相等，即队列空）且 `tail` 没有置 `MARK_BIT`（未断开）时返回 `false`。`try_recv` 据此返回 `Err(TryRecvError::Empty)`；`recv` 则进入「自旋重试 → 超时检查 → 注册阻塞」的完整流程（见 4.5）。

**练习 2**：为什么跨块时新 `head.index` 要用 `(new_head & !MARK_BIT).wrapping_add(1 << SHIFT)` 再按 `next.next` 是否非空决定是否置 `MARK_BIT`？

**答案**：跨块要进入下一个 block 的 offset 0。先清掉旧 head 的 `MARK_BIT`（那是针对旧 block 的语义），步进到下一格，再根据「新 block 后面是否还有 block」重新决定是否置位——因为 `MARK_BIT` 在 head 表示「当前块不是最后一个」，跨块后这个判断要针对新块重新计算。

---

### 4.4 惰性回收：Block::destroy 与 DESTROY 位交接

#### 4.4.1 概念说明

链表通道必须回收已读空的 block，否则会无限泄漏内存。但回收有个难点：**当一个 reader 读完某 block 的最后一格、想销毁整个 block 时，可能还有别的 reader 正在读取这个 block 更靠前的 slot**（mpmc！多个接收者并发）。直接释放会释放正在被读的内存。

list flavor 的解法是**惰性回收 + DESTROY 位交接**：

- 谁读到本块最后一格（offset == `BLOCK_CAP-1` == 30），谁就成为「销毁负责人」，调用 `Block::destroy(block, 0)`。
- `destroy` 从 `start` 往后扫描每个 slot：如果某个 slot 的 `READ` 位没置（说明还有 reader 在用），就在它上面置 `DESTROY` 位，然后**自己先撤**——把销毁责任交给那个正在用的 reader。
- 那个 reader 读完自己的 slot 后，在 `read` 里发现 `DESTROY` 位被置了，就接棒继续调用 `Block::destroy(block, offset+1)`，从自己下一格往后扫。
- 如果 `destroy` 一路扫完都没遇到「在用」的 slot，说明没人再用这个块了，就 `Box::from_raw` 安全释放。

这样保证：**block 恰好被释放一次，且释放时绝无 reader 在读它**。

#### 4.4.2 核心流程

`read` 末尾的销毁判定（伪代码）：

```
读出消息后:
    if offset + 1 == BLOCK_CAP:         # 读的是最后一格 → 我是销毁负责人
        Block::destroy(block, 0)
    else if fetch_or(READ) & DESTROY:    # 自己置 READ 时发现别人留了 DESTROY → 接棒
        Block::destroy(block, offset + 1)
```

`Block::destroy(this, start)`（伪代码）：

```
for i in start..BLOCK_CAP-1:           # 不含最后一格（它已启动销毁）
    slot = this.slots[i]
    if slot.state & READ == 0:          # 有 reader 在用？
        old = slot.state.fetch_or(DESTROY)
        if old & READ == 0:             # 确实还在用 → 留 DESTROY，交棒，return
            return
# 全程没人用 → 安全释放
drop(Box::from_raw(this))
```

注意双重检查：先用 `load` 看 `READ`，再用 `fetch_or(DESTROY)` 的返回值（旧值）再看一次 `READ`。这是因为 `load` 和 `fetch_or` 之间，reader 可能刚好读完置了 `READ`。两次检查 + `fetch_or` 的原子性保证了「要么我看到 READ 了（reader 已完成，可跳过），要么我成功置上 DESTROY 且 reader 之后能看到它（接棒）」。

#### 4.4.3 源码精读

`Block::destroy`：

[list.rs:119-137](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L119-L137) — 扫描 `start..BLOCK_CAP-1`，对「仍在使用」（`READ` 未置）的 slot 置 `DESTROY` 并提前返回；全空才 `Box::from_raw` 释放。注释明确：「不必给最后一格置 DESTROY，因为那一格已经启动了整个块的销毁」。

`read` 中的销毁触发：

[list.rs:419-427](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L419-L427) — 两个分支：读到最后一格 → `Block::destroy(block, 0)`；否则若发现自己置 `READ` 时旧值含 `DESTROY` → `Block::destroy(block, offset+1)` 接棒。

`wait_next`：

[list.rs:107-117](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L107-L117) — 接收者跨块时自旋等待 `next` 指针就绪（发送方可能刚 CAS 完 index 还没来得及 `store(next)`）。

#### 4.4.4 代码实践

**实践目标**：理解 DESTROY 位交接的时序，画出「读最后一格触发销毁、遇到在用 slot 则交棒」的流程。

1. 阅读以上三段源码，按下面场景手动推演（纸笔即可）：
   - 场景 A（单接收者）：接收者按顺序读 slot 0..30。读 slot 30 时触发 `Block::destroy(block, 0)`。`destroy` 扫描 slot 0..29，发现全都已 `READ`（自己刚读过），于是直接 `Box::from_raw` 释放。
   - 场景 B（两接收者并发）：接收者 X 读 slot 0，接收者 Y 读 slot 30。Y 先到 30，触发 `destroy(block, 0)`，扫到 slot 0 发现 `READ` 未置（X 还没读完），在 slot 0 置 `DESTROY` 后返回。X 读完 slot 0，发现自己置 `READ` 时旧值含 `DESTROY`，于是接棒调 `destroy(block, 1)`，继续往后扫并最终释放。
2. **需要观察的现象**：无论哪种场景，block 都恰好释放一次，且释放时所有 slot 都已读完。

**预期结果**：能讲清「DESTROY 位是销毁责任的『接力棒』」，以及双重 `READ` 检查为何能避免漏交接。这是源码阅读型实践，不涉及运行。

#### 4.4.5 小练习与答案

**练习 1**：`Block::destroy` 里为什么是双重检查 `READ`（先 `load` 再看 `fetch_or` 返回值），而不是只检查一次？

**答案**：`load` 与 `fetch_or` 之间存在时间窗口，reader 可能刚好在两者之间读完并置了 `READ`。如果只 `load` 一次就决定交棒，可能给一个已经读完的 slot 白留 `DESTROY`，而那个 reader 永远不会再回来接棒，导致 block 永不释放。双重检查：`load` 看到 `READ` 就跳过（reader 已完成）；否则 `fetch_or(DESTROY)` 的旧值若仍无 `READ`，说明在原子操作那一刻 reader 还没完成，它随后置 `READ` 时一定会看到 `DESTROY` 并接棒。

**练习 2**：为什么 `destroy` 的循环是 `start..BLOCK_CAP-1`（不含最后一格）？

**答案**：因为最后一格（offset == `BLOCK_CAP-1` == 30）的 reader 正是「触发本次 `destroy` 的人」——它已经读完并在执行销毁，不可能「还在用」。注释也写明「最后一格已经启动了块的销毁，无需再给它置 DESTROY」。

---

### 4.5 阻塞、断开与销毁：recv 循环、senders 永不阻塞、disconnect / discard / Drop

#### 4.5.1 概念说明

最后把三个收尾机制串起来：

1. **`recv` 的阻塞循环**：接收者发现队列空时，先自旋重试（`Backoff`），再检查超时，最后注册到 `receivers: SyncWaker` 并 `wait_until` 阻塞，被 `write` 里的 `notify` 或 `disconnect` 唤醒后重试。这套机制直接复用 u2-l4 讲过的 `Context` / `SyncWaker`。
2. **senders 永不阻塞**：因为无界，`Sender` 的 `SelectHandle` 实现里 `is_ready` 恒为 `true`、`register`/`unregister`/`watch`/`unwatch` 全是空操作——发送方从不需要排队等待。
3. **断开**：分「发送方断开」和「接收方断开」两种，都用 `tail.index.fetch_or(MARK_BIT)` 抢一次性置位权。
   - 发送方断开：唤醒所有阻塞接收者，让它们排空剩余消息后收到 `Disconnected`。
   - 接收方断开：**立即丢弃所有剩余消息**（急切释放内存），因为没人会再来读了。
4. **`Drop`**：整个通道被销毁时（counter.rs 释放堆内存的最后一步，见 u2-l2），`Channel::drop` 顺序排空并释放所有剩余 block。

#### 4.5.2 核心流程

`disconnect_senders` / `disconnect_receivers`（伪代码）：

```
old = tail.index.fetch_or(MARK_BIT, SeqCst)   # 抽奖：谁先把 MARK_BIT 从 0 变 1
if old & MARK_BIT == 0:                        # 我中奖，我是第一个断开者
    [senders 断开]: receivers.disconnect()      # 唤醒所有阻塞接收者
    [receivers 断开]: discard_all_messages()    # 急切丢弃剩余消息
    返回 true
else:
    返回 false                                  # 别人已经断开过了
```

`discard_all_messages`：用 `head.block.swap(null)` 原子「摘下」第一个 block（防止与正在初始化首块的 sender 冲突），然后沿链表逐 slot `wait_write` + `assume_init_drop` 丢消息，每读完一个 block 就 `Box::from_raw` 释放，直到 head 追上 tail。

#### 4.5.3 源码精读

`recv` 阻塞循环：

[list.rs:465-515](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L465-L515) — 外层 `loop` 重试，内层 `Backoff` 自旋；超时检查后 `Context::with` 注册到 `self.receivers`，并在注册后复查 `is_empty`/`is_disconnected`（防丢失唤醒：若注册瞬间恰好有消息进来，主动 `try_select(Aborted)` 取消阻塞重试），最后 `cx.wait_until(deadline)` 阻塞。

`disconnect_senders` / `disconnect_receivers`：

[list.rs:558-586](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L558-L586) — 都用 `fetch_or(MARK_BIT)` 抢断开权。`disconnect_receivers` 多调一句 `discard_all_messages()`。

`discard_all_messages`：

[list.rs:588-653](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L588-L653) — 注意 [list.rs:600-605](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L600-L605) 先等 tail 落到块边界外（避免与并发 sender 的 index 推进冲突导致泄漏）；[list.rs:611](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L611) 用 `swap(null)` 摘首块；随后沿链逐 slot 丢消息并释放 block。

`Channel::drop`：

[list.rs:673-708](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L673-L708) — 用 `get_mut` 拿到 `&mut` 引用（此时已无并发），清掉标志位，沿链逐 slot `assume_init_drop` 并 `Box::from_raw` 释放每个 block，最后释放尾块。

senders 永不阻塞（`Sender` 的 `SelectHandle`）：

[list.rs:752-780](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L752-L780) — `is_ready` 恒返回 `true`，`register`/`unregister`/`watch`/`unwatch` 全是空实现，印证「发送方从不需要阻塞排队」。

对照接收者（`Receiver` 的 `SelectHandle`）：

[list.rs:716-750](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L716-L750) — `register` 真正注册到 `self.0.receivers`，`is_ready` 看 `!is_empty() || is_disconnected()`。

#### 4.5.4 代码实践

**实践目标**：验证「发送方断开后剩余消息仍可接收」「接收方先断开则消息被急切丢弃」。

1. 参考 [tests/list.rs:20-31](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/tests/list.rs#L20-L31) 的 `smoke` 测试，写两段小程序（示例代码）：

   ```rust
   // 示例代码：发送方断开，剩余消息可收
   let (s, r) = crossbeam_channel::unbounded();
   for i in 0..50 { s.send(i).unwrap(); }
   drop(s);                                  // 发送方断开
   let got: Vec<i32> = r.iter().collect();   // 仍能收完 50 条
   assert_eq!(got.len(), 50);
   assert_eq!(r.recv(), Err(crossbeam_channel::RecvError)); // 排空后报 Disconnected
   ```

   ```rust
   // 示例代码：接收方先断开，消息被丢弃
   let (s, r) = crossbeam_channel::unbounded::<i32>();
   for i in 0..50 { s.send(i).unwrap(); }
   drop(r);                                  // 接收方先断开 → discard_all_messages
   // 此时 50 条消息已被急切丢弃，block 也已释放
   ```

2. 运行 `cargo test --test list` 确认现有测试通过。

3. **需要观察的现象**：
   - 第一段：`iter().collect()` 拿到全部 50 条；之后再 `recv` 立即返回 `RecvError`。
   - 第二段：`drop(r)` 后无内存泄漏（无法直接观察，但可结合 4.4 理解 `discard_all_messages` 已释放所有 block）。

**预期结果**：第一段断言通过；第二段程序正常结束、无 panic。**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `disconnect_senders` 不像 `disconnect_receivers` 那样调用 `discard_all_messages`？

**答案**：发送方断开后，已经缓冲的消息**仍应被接收者取走**（这是 channel 的语义承诺：已 send 的消息不应丢失）。所以只唤醒阻塞接收者让它们继续排空，排空后自然收到 `Disconnected`。而接收方断开后，再也没有人会来读这些消息，留着只会泄漏内存，所以要立即 `discard_all_messages` 急切释放。

**练习 2**：`recv` 在注册到 `receivers` 之后，为什么还要再检查一次 `is_empty() || is_disconnected()` 并可能 `try_select(Aborted)`？

**答案**：防止「丢失唤醒」。在 `register` 之前的瞬间，可能恰好有 sender 写入消息（或通道断开），而那次 `notify` 看不到刚注册的自己。注册后复查一次，若发现已就绪就主动 `try_select(Aborted)` 把自己标记为「取消阻塞」，从而跳出 `wait_until` 重试——这是 u2-l4 讲过的「注册后再复查」标准模式。

---

## 5. 综合实践

把本讲知识串起来：**跟踪一条消息从「跨块发送」到「跨块接收并被回收」的完整生命周期**。

任务：

1. 写一个程序：用 `unbounded()` 创建通道，**单线程**连续 `send` 65 条消息（`65 = 2*31 + 3`，会创建 3 个 block）。
2. 在以下四处加临时日志（示例代码，验证后删除）：
   - [list.rs:252](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L252)：首块分配。
   - [list.rs:282](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L282)：链接新 block（记录 offset）。
   - [list.rs:382](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L382)：接收跨块。
   - [list.rs:136](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L136)：block 被 `Box::from_raw` 真正释放。
3. 接着连续 `recv` 65 次。
4. **预期日志（单线程）**：
   - 首块分配：1 次。
   - 链接新 block：2 次（写完第 31、62 条时）。
   - 接收跨块：2 次（读完第 31、62 条时）。
   - block 释放：2 次（读完第 31、62 条时，`destroy` 直接成功）；最后一个 block 在 `Channel::drop` 时释放（如果 drop 前已排空，则 head==tail，drop 里不再有 block 可释放）。
5. 用一句话解释：为什么「链接新 block 2 次」与「接收跨块 2 次」一一对应，而 block 释放却由接收侧负责。

**思考题**：如果把发送改成多线程并发，日志次数会变吗？为什么？（提示：并发 CAS 竞争会导致首块分配的 CAS 失败回收，但「成功的链接」次数仍由消息总量决定。）

> ⚠️ 本实践需要修改源码加日志，属于「学习用临时改动」。**验证完毕务必用 `git checkout src/flavors/list.rs` 还原**，不要提交这些日志。

---

## 6. 本讲小结

- list flavor 是 `unbounded()` 通道的底层实现，用一条**固定大小 block 的单向链表**组织无界队列，每个 block 装满 `BLOCK_CAP=31` 条消息，块满就链接下一个 block。
- `index` 用位编码同时承载「圈数 lap / 块内偏移 / 标志位」：最低位 `MARK_BIT` 在 tail 表示「断开」、在 head 表示「当前块非最后」；`Slot` 用 `WRITE/READ/DESTROY` 三位协调生产消费与销毁。
- **发送方永不阻塞**：`start_send` 用 CAS 推进 `tail`，必然成功抢占一个 slot 或检测到断开；`write` 写消息并 `notify` 唤醒一个阻塞接收者。
- `start_recv` 用 CAS 推进 `head`，能区分「空」「断开」「有数据」，并在读到本块最后一格时跨块跳转；`read` 用 `wait_write` 等消息可见后读出。
- **惰性回收 + DESTROY 接力**：读到最后一格的 reader 负责销毁整个 block，若发现别的 slot 仍被占用就置 `DESTROY` 交棒，那个 reader 读完接棒继续，保证 block 恰好释放一次且释放时无人再用。
- 断开分两种：发送方断开只唤醒接收者（剩余消息仍可排空），接收方断开则 `discard_all_messages` 急切丢弃；`Channel::drop` 在无并发前提下顺序释放所有 block。

## 7. 下一步学习建议

- **对比 array flavor（u2-l5）**：array 是固定环形数组、靠 stamp 版本号防回绕歧义、发送方会因满而阻塞；list 是动态链表、靠 LAP/BLOCK_CAP 边界哨兵防歧义、发送方永不阻塞。两者都是「start + write/read 两步走」配合 select，建议对照阅读。
- **学习 zero flavor（u2-l7）**：零容量会合通道，发送与接收必须配对，是第三种「真实」flavor。
- **进入 select 内核（u3-l1）**：本讲反复出现的 `Token` / `start_send`/`start_recv` 拆两步、`SelectHandle` 的 register/accept，正是 select 算法的直接构件，u3-l1 会把它们串成完整的 `run_select` 流程。
- **深入内存序（u3-l4）**：本讲多处 `Acquire`/`Release`/`SeqCst` 的选择（如 `write` 用 Release、`read` 用 Acquire 建立 happens-before，`fetch_or(DESTROY, AcqRel)` 保证接棒可见）将在 u3-l4 系统论证。
