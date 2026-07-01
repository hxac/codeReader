# list flavor：无界链表

## 1. 本讲目标

本讲深入 crossbeam-channel 的 `unbounded()` 通道底层实现 `flavors/list.rs`，读完你应该能够：

- 说出 list flavor 的存储结构：分块链表（每个 `Block` 容纳 31 条消息）以及 `head`/`tail` 两个 `Position` 如何驱动它。
- 解释 `usize` 索引里的 `LAP`（圈）/ `offset`（块内偏移）/ `MARK_BIT`（标记位）三段编码，以及为什么每块恰好是 31 条。
- 跟踪一条消息从 `start_send` 占位、`write` 写入、`start_recv` 取号、`read` 读出的完整两阶段流程，理解 `ListToken{block, offset}` 的桥梁作用。
- 说清「块」是如何被**协作回收**的（`DESTROY` 协议），以及当最后一个 receiver 离开时 `discard_all_messages` 如何把整条链表连同残留消息一起清掉。
- 对比 array flavor，说明为什么 list **永远不需要处理「满」**。

本讲承接 [u3-l3](u3-l3-flavors-architecture.md)（flavor 派发与 `SelectHandle`/`Token` 契约）与 [u3-l4](u3-l4-flavor-array.md)（array 的两阶段 start→commit 与阻塞唤醒），不重复这些已建立的概念，而是聚焦 list 独有的「分块链表 + 按需扩容 + 协作回收」。

## 2. 前置知识

- **flavor 与两阶段协议**：crossbeam-channel 把一次 `send` 拆成 `start`（用 CAS 占住一个槽位，把定位信息写进 `Token`）与 `write`（真正写入消息）两步；`recv` 同理拆成 `start_recv`/`read`。这样「被 select 选中」与「真正提交」可以解耦（见 u3-l3、u3-l4）。本讲会频繁看到 `token.list.block` / `token.list.offset` 在两个阶段间传递。
- **`Token`**：一个「每种 flavor 一字段」的胖联合体（u3-l3）。list 用其中的 `list` 字段，类型就是本讲的 `ListToken`。
- **引用计数与销毁**：`Sender`/`Receiver` 共享一份 `Counter`，`release`（drop）时减计数，归零的一侧触发 `disconnect_*`，最后离开者负责物理释放（u3-l2）。本讲的 `disconnect_receivers` / `discard_all_messages` 就挂在这条回调链上。
- **CAS 与 ABA**：用 `compare_exchange` 推进 `head`/`tail`，失败则 `Backoff` 退避重试。list 用 `LAP`（圈数）让索引单调前进，天然规避跨圈 ABA。
- **`CachePadded`**：`head`/`tail` 分别被发送方、接收方高频原子改写，用 `CachePadded` 隔离到不同缓存行，避免伪共享（u3-l2）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [crossbeam-channel/src/flavors/list.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs) | list flavor 的全部实现：`Block`/`Slot`/`Position`/`Channel` 数据结构，`start_send`/`write`/`start_recv`/`read` 两阶段流程，`discard_all_messages`、`Drop`、`disconnect_*` 与 `SelectHandle` 实现。 |
| [crossbeam-channel/src/flavors/mod.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/mod.rs) | 六种 flavor 的模块声明，本讲关注其中的 `list`。 |
| [crossbeam-channel/src/channel.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs) | 公共门面：`unbounded()` 构造函数把 `list::Channel` 包进 `counter`，再装进 `SenderFlavor::List` / `ReceiverFlavor::List`。 |

## 4. 核心概念与源码讲解

### 4.1 存储结构：Block、Slot、Position 与 LAP 索引编码

#### 4.1.1 概念说明

`unbounded()` 通道没有容量上限，发送方永不需要因为「满了」而阻塞。那消息存在哪里？最朴素的想法是「每条消息 `Box` 一个节点串成链表」，但这会让分配器压力极大、缓存局部性极差。crossbeam 的做法是**分块链表（chunked linked list）**：把连续的若干条消息打包进一个堆上的 `Block`，`Block` 之间再用 `next` 指针串起来。这样既保留了「按需扩容、无上限」的灵活性，又把单次分配摊销到多条消息上。

每个 `Block` 容纳 **31** 条消息（不是 32，下面解释）。索引被编码进一个 `usize`，用「圈（lap）+ 块内偏移（offset）」来定位「第几块的第几个槽」。

#### 4.1.2 LAP 索引编码

索引 `usize` 被切成三段，由几个常量定义：

[list-flavors-constants]: https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L30-L47

[crossbeam-channel/src/flavors/list.rs:30-47][list-flavors-constants] 定义了状态位与索引编码常量。含义如下：

- `WRITE=1`、`READ=2`、`DESTROY=4` 是**槽位状态位**，写在每个 `Slot` 的 `state` 里（不是索引里）。
- `LAP=32`：一圈覆盖 32 个「位移后的位置」。
- `BLOCK_CAP=31`（`LAP-1`）：每个块真正能存 **31** 条消息，第 32 个位置是「块边界」哨兵。
- `SHIFT=1`：索引最低 1 位保留给 `MARK_BIT`，真正的位移值从第 1 位开始。
- `MARK_BIT=1`：最低位有**双重含义**——写在 `head.index` 里表示「当前块后面还有块」，写在 `tail.index` 里表示「通道已断开」。

给定一个索引 `index`，解码方式是：

\[
\text{offset} = \left\lfloor \frac{\text{index}}{2} \right\rfloor \bmod 32, \qquad
\text{lap} = \left\lfloor \frac{\lfloor \text{index}/2 \rfloor}{32} \right\rfloor
\]

因为 `LAP=32` 是 2 的幂，`% LAP` 与 `/ LAP` 在机器码里就是 `& 31` 和 `>> 5`，极其廉价。`offset` 取值 `0..=31`：`0..=30` 对应块内 31 个真实槽，`31`（等于 `BLOCK_CAP`）是「本块已满、该跳到下一块」的边界。

> **为什么每块是 31 条，而不是 30 或 63？** 这是对「2 的幂次取模最便宜」与「块不要太大」的折中：`LAP=32` 让取模变位与，`BLOCK_CAP=31` 则是「一个 2 的幂减一」能塞下的最大槽数。31 条/块在分配器开销与内存浪费之间取了平衡。

#### 4.1.3 槽、块、位置与通道

[list-slot-block]: https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L49-L77

[crossbeam-channel/src/flavors/list.rs:49-77][list-slot-block] 定义了 `Slot<T>` 与 `Block<T>`：

- `Slot<T>` 持有消息 `msg: UnsafeCell<MaybeUninit<T>>` 与状态 `state: AtomicUsize`。`MaybeUninit` 不是因为「值还没初始化」，而是阻止编译器假定槽里一定是合法 `T`（这与 [u2-l3](u2-l3-atomic-cell.md) 中 AtomicCell 用 `MaybeUninit` 屏蔽自动析构的理由一致），因此 `Channel` 必须手动管理析构。
- `Block<T>` 持有 `next: AtomicPtr<Block<T>>`（指向下一块）与 `slots: [Slot<T>; BLOCK_CAP]`（31 个槽）。
- `Slot::wait_write` 用 `Backoff::snooze()`（见 [u2-l1](u2-l1-backoff.md)）自旋等待 `WRITE` 位置位——这是接收方等待「发送方把消息写进来」的占位循环。

[list-position-channel]: https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L140-L206

[crossbeam-channel/src/flavors/list.rs:177-189][list-position-channel] 定义了核心结构 `Channel<T>`：

- `head: CachePadded<Position<T>>`：接收方推进的「读位置」。
- `tail: CachePadded<Position<T>>`：发送方推进的「写位置」。
- `receivers: SyncWaker`：登记因「通道空」而阻塞的接收线程（发送方永不阻塞，所以没有 `senders` 等待队列）。

每个 `Position` 含 `index: AtomicUsize`（上面那套编码的索引）与 `block: AtomicPtr<Block<T>>`（当前所在块的指针）。`Channel::new()`（[同文件 193-206 行][list-position-channel]）把 head/tail 的 index 初始化为 0、block 初始化为空指针——也就是说，**通道刚建好时一个块都没有**，第一块在第一条消息发送时才分配。

#### 4.1.4 代码实践

源码阅读型实践，目标是把「索引编码」内化为直觉。

1. **目标**：能用纸笔算出「第 60 条消息存哪」。
2. **步骤**：
   - 打开 [list.rs 的常量段][list-flavors-constants]，确认 `LAP=32`、`BLOCK_CAP=31`、`SHIFT=1`。
   - 假设第 60 条消息（0 起算即序号 60）写入时 `tail.index` 的位移值 `index>>1 = 60`。
   - 计算 `offset = 60 % 32 = 28`，`lap = 60 / 32 = 1`。
3. **观察**：序号 60 落在 lap 1（第 2 块）的 offset 28（第 29 个槽）。
4. **预期结果**：你能复现「序号 n → 块号 `n/31`、块内槽 `n%31`」（注意是按 31 而非 32 切块，因为每块只存 31 条真实消息）。这正是后面「发 1000 条要多少块」的算法基础。

#### 4.1.5 小练习与答案

**练习 1**：`MARK_BIT` 在 `head.index` 和 `tail.index` 里分别表示什么？为什么同一个位能有两种含义？

> **答案**：在 `head.index` 里表示「当前块之后还有下一块」（接收方据此知道读完后要跳块）；在 `tail.index` 里表示「通道已断开」。两者不会混淆，因为发送方只读写 `tail`、接收方只读写 `head`，解读 `MARK_BIT` 时总是结合自己所在的那一端。

**练习 2**：为什么 `BLOCK_CAP = LAP - 1 = 31`，而不是直接等于 `LAP = 32`？

> **答案**：`LAP=32` 的第 32 个位置（offset=31）被用作「块边界哨兵」——当 offset 推进到 31，意味着本块已满、需要跳到下一块。如果 32 个位置都用来存消息，就没有一个「无歧义的满」信号可用，而保留一个哨兵位让「满」可以被廉价检测（`offset == BLOCK_CAP`）。

---

### 4.2 发送路径：start_send 与 write（尾指针扩容 + ListToken）

#### 4.2.1 概念说明

发送走两阶段（u3-l3 已建立的模式）：`start_send` 用 CAS 在 `tail` 上占住一个槽位，把定位信息写进 `Token.list`；随后 `write` 真正把消息摆进那个槽并唤醒接收方。list 的特别之处在于 `start_send` 还要负责**按需分配新块**并把它们链起来——这是「无界」的关键。

#### 4.2.2 核心流程

`start_send` 的主循环（伪代码）：

```
load tail.index, tail.block
loop:
    若 tail.index 的 MARK_BIT=1 → 通道已断开：token.list.block=null，返回 true（write 会据此返回 Err）
    offset = (tail.index >> 1) % 32
    若 offset == 31（块边界）→ snooze 退避，等待别的发送方把下一块装好，重读 tail，重试
    若 offset == 30（即将写满本块）且未预分配 → 提前 new 一个 Block 备用（减少别人等待时间）
    若 tail.block == null（首条消息）→ 分配第一块，CAS 装进 tail.block 并发布到 head.block
    用 CAS 把 tail.index 从 tail 推进到 tail + 2
    成功：
        若刚写的是 offset 30（本块最后一个槽）→ 把预分配的块装上：
            tail.block = 下一块；tail.index += 2（越过 offset 31 边界）；本块.next = 下一块
        token.list.block = 当前块；token.list.offset = offset
        返回 true
    失败：backoff.spin() 重试
```

#### 4.2.3 源码精读

[list-start-send]: https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L218-L299

[crossbeam-channel/src/flavors/list.rs:219-299][list-start-send] 是 `start_send` 全文，关键点：

- **断开检测**（227-230 行）：`tail & MARK_BIT != 0` 即已断开，把 `token.list.block` 置空返回 `true`——注意这里返回的是 `true`（「选中成功」），真正的失败由 `write` 看到 `block.is_null()` 后返回 `Err(msg)` 表达，与 u3-l4 array 的处理一致。
- **块边界等待**（236-241 行）：`offset == BLOCK_CAP` 时 `snooze` 等待，说明别的发送方正在安装下一块，自己稍后重读。
- **预分配**（245-247 行）：当 `offset + 1 == BLOCK_CAP`（即正在写本块最后一个真实槽 offset 30）时，提前 `Block::new()` 一个备用块，目的是让「等待下一块」的线程尽快看到它。
- **首块分配**（251-268 行）：`block.is_null()` 时分配第一块，用 `compare_exchange` 装进 `tail.block`，并**同步发布到 `head.block`**（260 行）——因为第一块既是发送方的写起点、也是接收方的读起点。CAS 失败则把刚分配的块收回重试。
- **推进 tail**（270-298 行）：`new_tail = tail + (1<<SHIFT)`（即 `+2`，保留 MARK_BIT 位）。CAS 成功后，若刚写的是本块最后一槽，就把预分配块装上：`tail.block` 指向新块、`tail.index` 再 `fetch_add(2)` 越过边界、并把本块 `next` 指过去。最后把「当前块 + offset」写进 token。

[list-write]: https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L301-L318

[crossbeam-channel/src/flavors/list.rs:302-318][list-write] 是 `write`：若 `token.list.block` 为空则返回 `Err(msg)`（断开，把消息还给调用者）；否则用 `MaybeUninit::write` 把消息写进槽，再用 `fetch_or(WRITE, Release)` 置位（Release 保证消息内容先于 `WRITE` 对接收方可见），最后 `receivers.notify()` 唤醒一个睡眠的接收者。

`ListToken` 本身只是两个普通字段的载体：

[list-token]: https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L150-L168

[crossbeam-channel/src/flavors/list.rs:152-168][list-token] 定义 `ListToken{ block: *const u8, offset: usize }`。`block` 存的是 `*const u8`（类型擦除的裸指针，`write`/`read` 时再 `cast::<Block<T>>` 还原），`offset` 是块内槽号。它就是 `Token` 联合体里属于 list 的那个字段，在 start 与 commit 两阶段之间传递「你占住的是哪一块的第几槽」。

#### 4.2.4 代码实践

**目标**：验证「list 永远不会因为满而阻塞/失败」。

1. 写一段程序，用 `unbounded()` 创建通道，主线程**连续** `s.try_send(i)` 发送 `0..1000`，全程不接收。
2. 编译运行，确认 1000 次 `try_send` 全部返回 `Ok(())`。
3. 思考：如果是 `bounded(8)`，同样发 1000 条不接收，第 9 次 `try_send` 会怎样？
4. **预期结果**：list 版本 1000 次全成功；`bounded(8)` 版本第 9 次起返回 `TrySendError::Full(_)`。这直观说明 list 的 `is_full()` 恒为 `false`、`capacity()` 返回 `None`（见 [list.rs:554-556](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L554-L556) 与 [668-670 行](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L668-L670)）。

参考示例代码（**示例代码**，非项目原有）：

```rust
use crossbeam_channel::{unbounded, bounded, TrySendError};

fn main() {
    // list：无界，永不「满」
    let (s, _r) = unbounded();
    for i in 0..1000 {
        assert!(s.try_send(i).is_ok(), "list 不应在第 {i} 条失败");
    }

    // 对照：bounded(8) 会满
    let (s, _r) = bounded(8);
    for i in 0..1000 {
        match s.try_send(i) {
            Ok(()) => {}
            Err(TrySendError::Full(_)) => return, // 预期在此处停下
            Err(TrySendError::Disconnected(_)) => unreachable!(),
        }
    }
}
```

#### 4.2.5 小练习与答案

**练习 1**：`start_send` 在 `offset == BLOCK_CAP`（31）时为什么要 `snooze` 等待，而不是自己直接装下一块？

> **答案**：当 offset 推进到 31，说明**另一个发送方**刚把本块写满、正在安装下一块（它会做 `tail.block = next`、`tail.index += 2`）。此刻去抢着装块会与之竞争；正确做法是 `snooze` 退避、重读 `tail`，等对方把 `tail.block`/`tail.index` 推过边界后，自己自然进入新块的 offset 0。

**练习 2**：为什么第一块要在 `tail.block` 和 `head.block` **两处**都发布？

> **答案**：第一块之前 head 与 tail 的 block 都是 null。第一条消息发出后，发送方的 `tail.block` 要指向它（之后继续往后写），接收方的 `head.block` 也要指向它（之后从这里开始读）。所以 [list.rs:260](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L260) 在 CAS 成功装上 `tail.block` 后，紧接着把同一个指针 `store` 进 `head.block`。

---

### 4.3 接收路径：start_recv 与 read（DESTROY 协议的块回收）

#### 4.3.1 概念说明

接收同样两阶段：`start_recv` 用 CAS 推进 `head` 占住一个读槽，`read` 取出消息。难点不在读，而在**块如何被释放**。因为这是 MPMC 通道，多个接收者可能同时在同一块的不同槽上读，谁都不能在别人还没读完时就把整块 `free` 掉。crossbeam 的方案是 **`DESTROY` 协议**：读完本块最后一个槽的接收者发起销毁，若发现仍有同伴占用更早的槽，就留下 `DESTROY` 标记「拜托」，由最后离开的同伴接力完成回收。

#### 4.3.2 核心流程

`start_recv` 的关键判定（伪代码）：

```
loop:
    offset = (head.index >> 1) % 32
    若 offset == 31（块边界）→ snooze 等待下一块装好，重试
    new_head = head + 2
    若 new_head 还没带 MARK_BIT：
        fence(SeqCst)
        读 tail（Relaxed）
        若 head>>1 == tail>>1（空）：
            若 tail 已断开 → token.block=null 返回 true（read 返回 Disconnected）
            否则 → 返回 false（未就绪，select 会去阻塞）
        若 head 与 tail 不在同一块 → 给 new_head 置 MARK_BIT（提示「读完本块要跳块」）
    若 head.block==null（首条消息正在发送）→ snooze 重试
    CAS 推进 head.index 到 new_head
    成功：
        若 offset==30（本块最后一槽）→ wait_next 拿到下一块，把 head.block/ head.index 推进到新块
        token.list.block = 当前块；token.list.offset = offset；返回 true
    失败：backoff.spin() 重试
```

`read` 取出消息后决定块的命运：

```
wait_write（等发送方写进来）→ 读出 msg
若 offset==30（本块最后一槽）→ Block::destroy(block, 0)   # 本块已无未读槽，尝试整块回收
否则若本槽原本就带 DESTROY 位 → Block::destroy(block, offset+1)  # 别人拜托我接力回收
返回 Ok(msg)
```

#### 4.3.3 源码精读

[list-start-recv]: https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L320-L403

[crossbeam-channel/src/flavors/list.rs:321-403][list-start-recv] 是 `start_recv`，注意几处与 `start_send` 的对称与不对称：

- **空判定**（340-355 行）：只有 `new_head` 还没带 `MARK_BIT` 时才需要查 tail 判空。`head>>SHIFT == tail>>SHIFT` 即「读位追上写位」=空；空且断开则给 null token 返回 true（`read` 会翻译成 `Disconnected`），空且未断开则返回 false（让 select 去注册阻塞，详见 u3-l7）。
- **跨块标记**（357-360 行）：若 head 与 tail 已不在同一块（`/ LAP` 不同），给 `new_head` 置 `MARK_BIT`，提示接收方「读完本块记得跳到下一块」。这正是 4.1.5 里 `MARK_BIT` 在 head 端的含义。
- **跳块**（380-390 行）：读完本块最后一槽（offset 30）时，`wait_next` 等到下一块指针，把 `head.block` 推进到新块、并用 `store` 重写 `head.index`（含是否再带 `MARK_BIT` 的判断）。

[list-read]: https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L405-L430

[crossbeam-channel/src/flavors/list.rs:406-430][list-read] 是 `read`，重点是结尾的回收决策（419-427 行）。

[list-block-destroy]: https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L107-L137

[crossbeam-channel/src/flavors/list.rs:120-137][list-block-destroy] 是协作回收的核心 `Block::destroy(this, start)`：

- 从 `start` 起扫描到 `BLOCK_CAP-1`，对每个**仍被占用**（`READ` 位未置）的槽 `fetch_or(DESTROY)`，并立即 `return`——意思是「还有同伴在读这块，我留下 DESTROY 标记先走，等他读完接力」。
- 若一路扫完都没遇到占用者，说明无人再用本块，`drop(Box::from_raw(this))` 物理释放。

这样就形成了接力链：发起销毁的线程 A 发现槽 `k` 被线程 B 占用，留下 `DESTROY`；B 读完槽 `k` 后，在 `read` 里看到 `DESTROY` 位（[424 行][list-read]），从 `k+1` 起调用 `Block::destroy` 继续扫，如此传递，直到某个线程发现整块无人占用，完成真正的释放。**每个块恰好被释放一次，且不会在有人读时被提前释放。**

#### 4.3.4 代码实践

源码阅读型实践，跟踪一次回收接力。

1. **目标**：说清「块何时被真正 `free`」。
2. **步骤**：
   - 读 [read][list-read] 的 419-427 行：注意触发 `Block::destroy` 的两种条件。
   - 读 [Block::destroy][list-block-destroy]：注意它在遇到 `READ==0` 的槽时 `fetch_or(DESTROY)` 后**立即 return**。
   - 回到 `read` 第 424 行：`fetch_or(READ)` 同时若发现 `DESTROY` 已置位，就从 `offset+1` 接力 `destroy`。
3. **观察**：这是一条「谁最后离开谁释放」的协议，没有全局锁，完全靠 `state` 的位组合 + Acquire/Release 同步。
4. **预期结果**：你能向同事解释「为什么多个 receiver 并发读同一块时，不会 double-free，也不会泄漏」。

#### 4.3.5 小练习与答案

**练习 1**：`read` 中 `fetch_or(READ, AcqRel) & DESTROY != 0` 这个判断为什么用 `fetch_or` 而不是先 `load` 再 `store`？

> **答案**：必须把「置 READ」与「读取 DESTROY」合并成一个原子操作，否则存在窗口：先 load 看到 DESTROY=0，正要置 READ 时，另一线程恰好发起 destroy 看到 READ=0 而留下 DESTROY 并 return，于是两边都以为「对方会回收」，导致泄漏。`fetch_or` 让「置 READ」与「取回旧 DESTROY」原子完成，杜绝这个竞争。

**练习 2**：为什么 `start_recv` 在判定「空」时要先 `fence(SeqCst)` 再 `load` tail？

> **答案**：要在「head 已推进」与「tail 当前值」之间建立全局顺序，确保「看到 tail 没动」与「确实没有未读消息」一致。SeqCst fence 把本次 head 推进纳入全局全序，避免因重排而把「非空」误判为「空」（从而错误返回未就绪或错误阻塞）。

---

### 4.4 销毁语义：disconnect_receivers 与 discard_all_messages

#### 4.4.1 概念说明

无界通道有个隐患：如果所有 receiver 都被 drop 了，而 sender 还活着继续发，消息就会**无限堆积在链表里**，造成内存泄漏。为此，list 在「最后一个 receiver 离开」时立即调用 `discard_all_messages`，把整条链表连同里面尚未消费的消息**全部丢弃并释放**。这是 list 区别于 array 的一个重要销毁语义（array 在 receiver 先离开时同样会 discard，但 list 的链表结构让这件事更值得强调）。

与之对照：**最后一个 sender 离开**时，`disconnect_senders` 只置断开位并唤醒等待的 receiver，**不**丢弃消息——因为还可能有 receiver 想把剩余消息消费完，它们会在读空后收到 `Disconnected`。

#### 4.4.2 核心流程

```
某 receiver 被 drop
  → counter::Receiver::release 减计数
  → receivers 归零 → 调用回调 Channel::disconnect_receivers
       fetch_or(MARK_BIT) 于 tail.index
       若本调用是第一个置位者（旧值 MARK_BIT==0）→ discard_all_messages()
discard_all_messages:
  等 tail 越过块边界（若有发送方正卡在边界）
  swap 取走 head.block（把链表所有权夺过来）
  从 head 扫到 tail：逐槽 wait_write + assume_init_drop 丢弃消息；每越过一个块边界就 free 旧块
  free 最后剩下的块
Channel::drop（整体释放时）：
  同样从 head 扫到 tail 丢消息、释放块（此时已无并发，用 get_mut 取值）
```

#### 4.4.3 源码精读

[list-disconnect-recv]: https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L572-L586

[crossbeam-channel/src/flavors/list.rs:575-586][list-disconnect-recv] 是 `disconnect_receivers`：用 `fetch_or(MARK_BIT, SeqCst)` 抢着断开，旧值 `MARK_BIT==0` 的那个调用是赢家，负责 `discard_all_messages()`（保证只执行一次）。注意它由 `counter::Receiver::release` 经回调触发——这正是 [u3-l2](u3-l2-counter-and-errors.md) 讲的「最后一侧触发 disconnect、唯一销毁」协议在此处的落地。

[list-discard]: https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L588-L653

[crossbeam-channel/src/flavors/list.rs:591-653][list-discard] 是 `discard_all_messages`，要点：

- **等待边界**（593-605 行）：若 tail 正卡在块边界（`offset == BLOCK_CAP`），说明有发送方正在装下一块，必须等它落地，否则会漏掉正在分配的块（注释指出否则会内存泄漏）。
- **夺取链表**（611 行）：`head.block.swap(null, AcqRel)` 把整条链表的起点指针原子夺过来置 null——此后迟到的发送方若还想初始化首块，会看到 null 并自行在 `Drop` 里释放它（609-610 行注释解释了这种「半初始化」竞态的处理）。
- **逐块丢弃**（627-644 行）：从 head 扫到 tail，对真实槽 `wait_write`（等发送方写完）后 `assume_init_drop` 析构消息；遇到块边界则 `free` 当前块、走向 `next`。
- **收尾**（647-649 行）：释放最后一个块。

[list-drop]: https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L673-L708

[crossbeam-channel/src/flavors/list.rs:673-708][list-drop] 是 `Drop for Channel`：当 `Channel` 整体被释放（即 sender 与 receiver 都已离开、counter 触发物理释放）时，剩余的 head..tail 消息在这里被析构、块被释放。此时已无并发，所以直接用 `get_mut` 取值，不再需要原子操作。它和 `discard_all_messages` 形成互补：后者是「receiver 先走」的提前清理，前者是「彻底销毁」的最终清理。

最后看一眼公共门面如何把 list 装配起来：

[list-unbounded]: https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L50-L58

[crossbeam-channel/src/channel.rs:50-58][list-unbounded] 是 `unbounded()`：`counter::new(flavors::list::Channel::new())` 把 list 的 `Channel` 包进共享计数账本 `Counter`，再分别装进 `SenderFlavor::List` 与 `ReceiverFlavor::List`（枚举定义见 [channel.rs:371-380](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L371-L380)）。此后所有 `send`/`recv`/`SelectHandle` 调用都经 `SenderFlavor::List(chan) => chan.xxx()` 静态派发到本文件实现的方法（u3-l3 已讲过派发表）。

#### 4.4.4 代码实践

**目标**：观察「receiver 先离开时消息被立即丢弃」。

1. 创建 `unbounded()` 通道，发送 100_000 条 `String`（每条带较大内容，便于观察内存）。
2. **不接收**，直接 `drop(r)` 丢弃 receiver。
3. 在消息类型上实现带 `println!` 的 `Drop`，或在发送后用进程内存指标观察。
4. **预期结果**：`drop(r)` 之后，100_000 条消息的析构会被触发（若实现了 `Drop` 打印，会看到大量输出），内存回落——这正是 `discard_all_messages` 在最后一个 receiver 离开时即时清理的体现。
5. 若无法在本地运行，**待本地验证**：至少阅读 [discard_all_messages][list-discard] 确认它会 `assume_init_drop` 每一条 head..tail 之间的消息。

参考示例代码（**示例代码**，非项目原有）：

```rust
use crossbeam_channel::unbounded;

struct Traced(String);
impl Drop for Traced {
    fn drop(&mut self) {
        // 仅在大量析构时才容易观察到效果；此处省略打印以避免刷屏
    }
}

fn main() {
    let (s, r) = unbounded();
    for i in 0..100_000 {
        s.send(Traced(format!("msg-{i}"))).unwrap();
    }
    // 不接收，直接丢弃所有 receiver → 触发 discard_all_messages
    drop(r);
    // 此刻链表应已被清空
    drop(s);
}
```

#### 4.4.5 小练习与答案

**练习 1**：为什么最后一个 **sender** 离开时不调用 `discard_all_messages`，而最后一个 **receiver** 离开时要？

> **答案**：sender 全部离开后，仍可能有 receiver 想消费剩余消息，应让它们读完再收到 `Disconnected`，所以只置断开位 + 唤醒（`disconnect_senders`，[list.rs:561-570](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L561-L570)）。而 receiver 全部离开后，**没有任何人会再消费**这些消息，留着只会让继续发送的 sender 把通道撑爆成内存泄漏，所以必须立即丢弃。

**练习 2**：`discard_all_messages` 为什么要用 `head.block.swap(null)` 而不是 `load`？

> **答案**：`swap` 在读取的同时把 `head.block` 原子地置为 null，从而「夺取」整条链表的释放所有权，并阻止迟到的发送方误以为首块还没初始化而重复分配。若只用 `load`，夺取与置 null 就不是原子的，会与正在初始化首块的发送方产生竞争。

---

## 5. 综合实践

把本讲三个模块串起来，完成一个「估算 + 验证」的小任务。

**任务**：用 `unbounded()` 发送 `N=1000` 条消息且不接收，先**纸上估算**需要分配多少个 `Block`，再用源码逻辑复核。

**步骤**：

1. **估算**：每块存 `BLOCK_CAP=31` 条真实消息。`1000 / 31 = 32` 余 `8`，所以前 32 块装满（共 `32×31=992` 条，序号 0..991），第 33 块装剩余 8 条（序号 992..999，槽 0..7）。结论：**33 个块**。
2. **源码复核**：对照 [start_send][list-start-send] 的预分配逻辑——仅当 `offset+1 == BLOCK_CAP`（即写本块最后一槽 offset 30）时才预分配下一块。第 33 块只写到槽 7，`offset+1=8 ≠ 31`，所以**不会**预分配第 34 块。估算成立。
3. **观察点**：若你给 `Block::new` 临时加一行日志（仅本地学习用，勿提交），发 1000 条应恰好看到 33 次分配。
4. **回答第二个问题**：list 为什么不需要像 array 那样处理容量上限？因为它是**按需扩容的分块链表**——满了就 `Block::new()` 链一个新块，`capacity()` 返回 `None`、`is_full()` 恒 `false`，`start_send` 只在断开时返回失败，永不因「满」阻塞；而 array 是定长环形缓冲，满了就必须阻塞等接收方腾位（u3-l4）。

**预期结果**：你能不查代码就说出「N 条消息 → ⌈N/31⌉ 个块」，并能解释 list 与 array 在容量处理上的本质差别。若无法运行验证块分配次数，**待本地验证**（可借助临时日志或调试器观察 `tail.block` 链长度）。

## 6. 本讲小结

- list flavor 是 `unbounded()` 的实现：一条**分块链表**，每块 `Block` 容纳 `BLOCK_CAP=31` 条消息，块间用 `next` 指针链接，`head`/`tail` 两个 `CachePadded<Position>` 分别驱动收发。
- 索引被编码进 `usize`：`SHIFT=1` 位留给 `MARK_BIT`（head 端表「还有下一块」、tail 端表「已断开」），其余位按 `LAP=32` 切成「圈 + 偏移」，使取模/除法退化为位运算。
- 发送走两阶段：`start_send` 用 CAS 推进 `tail` 占槽，必要时**预分配并安装下一块**实现无界扩容；`write` 写消息、置 `WRITE` 位、唤醒接收者。`ListToken{block, offset}` 在两阶段间传递占位信息。
- 接收走对称的 `start_recv`/`read`，并通过 **`DESTROY` 协议**协作回收块：读完本块最后一槽者发起销毁，遇占用则留 `DESTROY` 拜托，最后离开者接力完成 `free`，保证每块恰好释放一次。
- 最后一个 receiver 离开时 `discard_all_messages` **即时丢弃全部消息并释放整条链表**，避免无界泄漏；最后一个 sender 离开时只置断开位唤醒接收者，不丢消息。
- 与 array 的根本区别：list 永不「满」（`is_full()` 恒 `false`、`capacity()` 返回 `None`），靠按需分配新块而非阻塞来吸收发送。

## 7. 下一步学习建议

- 接下来读 [u3-l6](u3-l6-flavor-zero.md) 零容量会合通道 `zero`，它是「无缓冲、send/recv 必须配对」的另一个极端，与 list 的「无限缓冲」形成对照。
- 若想搞清楚接收方在通道空时如何真正阻塞、发送方 `notify` 如何唤醒，继续读 [u3-l7](u3-l7-context-and-waker.md) 的 `Context` 与 `Waker`——本讲里 `receivers.register/notify` 与 `recv` 里的 `cx.wait_until` 都依赖它们。
- 想看动态选择如何把这些 flavor 的 `try_select`/`accept` 串起来，读 [u3-l9](u3-l9-select-algorithm.md) 的 select 算法。
