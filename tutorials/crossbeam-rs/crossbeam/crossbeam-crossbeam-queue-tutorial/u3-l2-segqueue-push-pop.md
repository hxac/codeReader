# SegQueue 的 push 与 pop 主链路：块分配、安装与空判定

## 1. 本讲目标

学完本讲后，你应当能够：

- 逐行讲清楚 `SegQueue::push` 的无锁主循环：它如何用 CAS 推进 `tail.index`、如何懒分配第一个块、如何在块末尾「同时安装下一个块」。
- 讲清楚 `SegQueue::pop` 的对称逻辑：它如何用 CAS 推进 `head.index`、如何用 `HAS_NEXT` 位缓存「是否存在下一个块」的判定、如何判定空队列。
- 理解全局 `index` 如何被解码为「块内偏移」，以及为什么每块只有 31 个可用槽、第 32 个偏移是「缝合位」。
- 理解 `WRITE` 状态位的「发布协议」：为什么消费者弹出某个槽时还要 `wait_write` 自旋等待。

本讲只讲 `push`/`pop` 的**主链路**。块的内存回收（`DESTROY` 位、`destroy`、`wait_next`）属于 [u3-l3](u3-l3-segqueue-block-destroy.md)，`len`/`Drop`/迭代器属于 [u3-l4](u3-l4-segqueue-len-drop-iter.md)，内存序的精确论证属于 [u4-l1](u4-l1-atomic-orderings-fence.md)。

## 2. 前置知识

在进入源码前，先建立四个直觉。

### 2.1 全局 index 的位编码（承接 u3-l1）

`SegQueue` 用**一个 `usize`** 同时编码「块号」「块内偏移」「一个元数据位」。回顾关键常量：

[src/seg_queue.rs:26-32](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L26-L32) 定义了 `LAP=32`、`BLOCK_CAP=31`、`SHIFT=1`、`HAS_NEXT=1`。

给定一个 `index`，解码方式是：

- 块内偏移：\(\text{offset} = (\text{index} \gg \text{SHIFT}) \bmod \text{LAP}\)
- 块号（第几块）：\(\text{lap} = (\text{index} \gg \text{SHIFT}) \,/\, \text{LAP}\)
- 是否还有下一块：\(\text{index} \,\&\, \text{HAS\_NEXT}\)（即 bit0）

因为 `SHIFT=1`，bit0 被预留为 `HAS_NEXT`，所以「推进一格」不是 `index += 1`，而是 `index += (1 << SHIFT)`，即 `index += 2`。

> 一个关键推论：`offset` 的取值范围是 `0..32`，但只有 `0..=30`（共 31 个）是真正存数据的槽，`offset == 31`（即 `BLOCK_CAP`）是**块的缝合位**——它不代表任何槽，而是「这块已经写到头了，该换下一块」的信号。这就是 `BLOCK_CAP = LAP - 1 = 31` 的由来。

### 2.2 乐观 CAS 循环

无锁队列的入队/出队本质是「抢占式占座」：先读当前指针 `tail`，算出目标位置，然后用 `compare_exchange_weak` 尝试把指针从 `tail` 改成 `new_tail`。如果中间有别的线程抢先改了指针，CAS 失败，带上返回的最新值重试。整个循环直到某次 CAS 成功为止。

### 2.3 WRITE 位与「发布协议」

`Slot<T>` 内有一个值字段（`UnsafeCell<MaybeUninit<T>>`）和一个状态字段（`AtomicUsize`），状态字段用 `WRITE=1`、`READ=2`、`DESTROY=4` 三个正交比特位表达，参见 [src/seg_queue.rs:21-23](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L21-L23)。

生产者写值分两步：先 `write` 裸值，再 `fetch_or(WRITE, Release)` 把 `WRITE` 位置 1。消费者弹出时先确认 `WRITE` 已置位，再 `read` 值。`WRITE` 位因此充当「值已就绪」的发布标志——这就是「发布协议」。

### 2.4 为什么消费者要等生产者写完？

在 `ArrayQueue` 里，生产者是「先 CAS 占位，再写值，再放行 stamp」；消费者看到放行的 stamp 时，值一定已写好。`SegQueue` 的结构更扁平：`head` 和 `tail` 是**两个独立推进**的指针。消费者推进 `head` 抢占一个槽时，对应的生产者可能刚通过 `tail` 的 CAS **占住**这个槽，但还**没来得及写值**。所以消费者抢到槽后，必须先 `wait_write` 自旋，等 `WRITE` 位被置上，才能安全读取。这是 SegQueue 区别于 ArrayQueue 的一个核心并发细节。

## 3. 本讲源码地图

本讲只涉及一个源码文件，但会反复在不同函数之间跳转：

| 代码位置 | 作用 |
|---|---|
| [src/seg_queue.rs:188-200](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L188-L200) `SegQueue::new` | 构造空队列，`head.block`/`tail.block` 都是 null（首块懒分配）。 |
| [src/seg_queue.rs:214-292](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L214-L292) `push` | 入队主链路：CAS 推进 tail、首块分配、块末尾安装下一块、写值并置 WRITE。 |
| [src/seg_queue.rs:364-449](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L364-L449) `pop` | 出队主链路：HAS_NEXT 缓存判定、空队列判定、CAS 推进 head、wait_write 读值。 |
| [src/seg_queue.rs:45-50](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L45-L50) `Slot::wait_write` | 自旋等待 `WRITE` 位置位，配合 2.4 的并发细节。 |
| [src/seg_queue.rs:93-102](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L93-L102) `Block::wait_next` | 块末尾自旋等待 `next` 指针被安装（pop 跨块时用到）。 |
| [tests/seg_queue.rs:1-162](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/tests/seg_queue.rs#L1-L162) | smoke / spsc / mpmc 测试，是本讲实践的依据。 |

## 4. 核心概念与源码讲解

### 4.1 push 的 CAS 主循环与首个块分配

#### 4.1.1 概念说明

`push` 是一个「乐观 CAS 循环」。每轮做三件事：

1. 把当前 `tail.index` 解码成块内偏移 `offset`；
2. 若 `block` 还是 null（队列为空、从未写入过），就**懒分配第一个块**，并通过 CAS 把它安装到 `tail.block`（同时镜像到 `head.block`，让消费者能找到它）；
3. 用 `compare_exchange_weak(tail, new_tail)` 抢占入队位置，成功则写值返回，失败则带上返回的最新 `tail` 重试。

注意 `new()` 出来的队列 `head.block` 和 `tail.block` 都是 null（见 [src/seg_queue.rs:188-200](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L188-L200)）。第一个真正执行 `push` 的线程负责分配 block 0。这是一种「延迟到首次使用才分配」的策略，避免空队列就占一块堆内存。

#### 4.1.2 核心流程

push 主循环的伪代码：

```
load tail.index, tail.block
loop:
    offset = (tail >> SHIFT) % LAP
    if offset == BLOCK_CAP:          # 落在缝合位，说明别的线程正在安装下一块
        snooze; reload tail/block; continue

    if offset + 1 == BLOCK_CAP:      # 即将写满本块，提前把下一块 new 出来
        预分配 next_block（仅一次）

    if block == null:                # 首次 push：分配 block 0
        CAS tail.block: null -> block0
        成功 -> head.block = block0
        失败 -> 回收自己 new 的块，reload，continue

    new_tail = tail + (1 << SHIFT)   # +2，跨过 HAS_NEXT 位

    CAS tail.index: tail -> new_tail
        Ok  -> 写值 + 置 WRITE（+ 若写满本块则安装 next_block）-> return
        Err -> tail = 返回值; reload block; spin; 重试
```

#### 4.1.3 源码精读

入口处先用 `Acquire` 读 `tail.index` 与 `tail.block`，并准备一个 `next_block: Option` 用于「提前 new 下一块」：

[src/seg_queue.rs:215-218](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L215-L218) — 读 tail 指针，准备进入 CAS 循环。

**首个块分配**（最值得精读的分支）：

[src/seg_queue.rs:239-256](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L239-L256) — 当 `block.is_null()` 时分配 block 0。要点：

- 用 `Box::into_raw(Block::new())` 在堆上分配一个零初始化的块，拿到裸指针 `new`。
- 用 `compare_exchange(block=ptr::null(), new, Release, Relaxed)` 抢着安装。**只有赢的那一个线程**会继续；它还要 `self.head.block.store(new, Release)` 把同一指针写到 head，这样消费者才能从 head 找到第一块。
- **输的线程**不能泄漏内存：它把自己的 `new` 用 `Box::from_raw` 收回来（放进 `next_block`，留作本线程后续可能用得上），然后重新读 `tail.block`（此时已被赢家写成 block0）、`continue` 进入正常写入路径。

为什么用 CAS 而不是直接写？因为可能有多个生产者线程同时发现 `block` 是 null 并各自 `new` 了一块，必须保证全局只有一个块被采纳，其余的块必须被释放——CAS + 失败回收就是这个「选举」机制。

**CAS 推进 tail** 的主推进点：

[src/seg_queue.rs:261-266](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L261-L266) — `compare_exchange_weak(tail, new_tail, SeqCst, Acquire)`。成功序用 `SeqCst`、失败序用 `Acquire`（失败时拿回的 `t` 会被当作下一轮的 `tail`，所以需要 `Acquire` 来看见最新的内存状态）。内存序的精确论证见 [u4-l1](u4-l1-atomic-orderings-fence.md)。

CAS 成功后进入 `Ok` 分支，那里会做「写满本块则安装下一块」和「写值并置 WRITE」两件事，分别在 4.2 和 4.3 详述。

CAS 失败的 `Err` 分支：

[src/seg_queue.rs:285-289](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L285-L289) — 把返回的最新值 `t` 赋给 `tail`，重新读 `tail.block`（因为失败可能意味着别的线程换块了），然后 `backoff.spin()` 短暂自旋退避后再进入下一轮。

#### 4.1.4 代码实践

**实践目标**：亲眼看到「首块懒分配 + 多生产者选举」的行为。

**操作步骤**：

1. 阅读 [tests/seg_queue.rs:129-162](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/tests/seg_queue.rs#L129-L162) 的 `mpmc` 测试，注意它用 4 个生产者线程并发 `push`。
2. 在 `src/seg_queue.rs:240`（`let new = Box::into_raw(Block::<T>::new());` 这一行后面）临时加一行 `eprintln!("allocated first block {:p}", new);`，再在 248 行（`self.head.block.store` 之后）加一行 `eprintln!("won the first-block race");`。
3. 运行 `cargo test -p crossbeam-queue mpmc -- --nocapture`（注意：这是临时改源码做观察，验证后请还原）。

**需要观察的现象**：尽管有 4 个生产者线程，`won the first-block race` 通常只会打印**一次**（赢家），而 `allocated first block` 可能打印多次（每个发现 `block` 为 null 的线程都会先 new 一块）。这印证了「多线程并发首次 push 时，CAS 选举出唯一的 block 0，其余线程 new 出来的块被回收」。

**预期结果**：`won the first-block race` 恰好出现一次，测试仍然通过。本实践需要修改源码做观察，**完成后务必用 `git checkout -- src/seg_queue.rs` 还原**。

#### 4.1.5 小练习与答案

**练习 1**：为什么首个块分配的 CAS 成功后，还要单独 `store` 一次 `head.block`？只更新 `tail.block` 不够吗？

**参考答案**：消费者从 `head.block` 出发定位队头。`new()` 时 `head.block` 是 null，若首个生产者只更新 `tail.block`，消费者会一直看到 null 而在 [pop 的 `block.is_null()` 分支](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L400-L405) 里空转。所以必须把同一个 block0 指针同时写到 `head.block`，把队列「接通」。

**练习 2**：CAS 失败的那个线程，为什么要把自己 `Box::into_raw` 出来的指针用 `Box::from_raw` 收回去？

**参考答案**：它 new 出来的块没有被全局采纳（赢家用了自己的块）。裸指针指向的堆内存不会再被任何人引用，若不回收就会泄漏。`Box::from_raw` 重新接管所有权，让它在作用域结束时被释放（这里还被放进 `next_block` 复用，避免白白浪费一次分配）。

---

### 4.2 块末尾 next 块的预分配与安装

#### 4.2.1 概念说明

当本块只剩最后一个可用槽（`offset == BLOCK_CAP - 1 == 30`）时，写完这一格就意味着本块写满，必须**立刻**把下一块挂上来，否则后续的 `push` 无处可写。`SegQueue` 的做法分两步，且都做了优化：

- **提前预分配**：在 CAS 之前，一旦发现 `offset + 1 == BLOCK_CAP`，就先 `Block::new()` 把下一块 new 出来备好。这样赢家线程在 CAS 成功后可以「零等待」地安装它，缩短其他线程看到 `offset == BLOCK_CAP` 而空转的时间窗。
- **安装**：CAS 成功后，把备好的 `next_block` 同时挂到三个地方——`tail.block`、`tail.index`（跳过缝合位到下一块的 offset 0）、以及当前块的 `next` 指针。

#### 4.2.2 核心流程

设当前 `tail = T`，`offset = 30`（本块最后一格）：

```
预分配阶段（CAS 之前）:
    if offset + 1 == BLOCK_CAP and next_block 还没备好:
        next_block = Some(Block::new())     # 提前 new，省一次「安装时才分配」的延迟

安装阶段（CAS tail.index: T -> T+2 成功之后）:
    next_block_ptr = Box::into_raw(next_block)
    next_index = (T+2) + (1<<SHIFT)          # = T+4，即下一块的 offset 0
    tail.block.store(next_block_ptr)          # tail 指向新块
    tail.index.store(next_index)              # 跳过缝合位 (T+2 对应 offset 31)
    current_block.next.store(next_block_ptr)  # 旧块的 next 指向新块
    # 然后才写本块最后一格的值
```

注意 `tail.index` 在安装时被一次性推进 **两格**：CAS 先把它从 `T` 推到 `T+2`（对应 `offset=31`，即缝合位），紧接着 `store` 把它再推到 `T+4`（对应下一块 `offset=0`）。也就是说**缝合位只是 CAS 与 store 之间的一个瞬态值**——任何在此期间读到 `offset==31` 的线程会走 4.1 的等待分支重读。

#### 4.2.3 源码精读

**预分配**：

[src/seg_queue.rs:232-236](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L232-L236) — 注释写得很清楚：「如果即将要安装下一块，就提前分配，让别的线程等待时间尽量短」。`next_block.is_none()` 守卫保证整个 `push` 调用里最多预分配一次（避免循环重试时反复 new）。

**安装**（在 CAS 成功的 `Ok` 分支里）：

[src/seg_queue.rs:269-276](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L269-L276) — 三次 `store`（都用 `Release`）把下一块「接上」。`next_index = new_tail.wrapping_add(1 << SHIFT)` 正是上面说的「再推一格跳过缝合位」。三次 store 的顺序也有讲究：先更新 `tail.block`/`tail.index` 让后续生产者能立刻往新块写，再更新 `block.next` 让消费者能跨块。`block.next` 是消费者跨块时的依据（见 [pop 的 `wait_next`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L417)）。

> 为什么预分配放进 `push` 而不是在 `new()` 时就建好两块？因为 `SegQueue` 是无界队列，块要「按需增长」。预分配的优化只针对「马上就要用的下一块」，既不让生产者在安装时卡在分配器里，也不提前占用过多内存。

#### 4.2.4 代码实践

**实践目标**：在源码层面确认「写满本块的这一次 push，同时完成了三件事：写最后一格、安装下一块、推进 tail 越过缝合位」。

**操作步骤**（源码阅读型）：

1. 打开 [src/seg_queue.rs:267-283](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L267-L283)（`Ok` 分支）。
2. 回答：当 `offset + 1 == BLOCK_CAP` 时，先执行的是「安装下一块」（269-276 行）还是「写值」（279-281 行）？
3. 思考：如果把这两段顺序对调（先写值、再安装下一块），单线程功能上是否还正确？多线程下会出现什么问题？

**需要观察的现象 / 预期结果**：源码中**安装先于写值**。单线程下顺序对调功能仍正确；但多线程下，若先写值再安装，那么在写值与安装之间，`tail.index` 停留在 `T+2`（缝合位），其他生产者会持续空转等待，拉高竞争延迟——这正是预分配 + 先安装想要避免的窗口。（这属于推理题，无需运行。）

#### 4.2.5 小练习与答案

**练习 1**：预分配的 `next_block` 是 `Option<Box<Block<T>>>`。如果本次 `push` 最终 CAS 一直失败、循环退出前都没用上它，这块内存会怎样？

**参考答案**：`push` 正常返回路径只有 CAS 成功这一条，成功且 `offset+1==BLOCK_CAP` 时会用掉它（`Box::into_raw`）；成功但 `offset+1 != BLOCK_CAP` 时不会用到 `next_block`，此时它作为 `push` 的局部变量在函数返回时被 `drop`，自动释放。所以无论如何不会泄漏。注意：失败重试时不会重新 new，因为 `next_block.is_none()` 守卫只在「还没备好」时才分配一次。

**练习 2**：`next_index = new_tail.wrapping_add(1 << SHIFT)`。假设 `new_tail` 对应本块最后一格（offset 30），算一算 `next_index` 对应的 offset 是多少？

**参考答案**：`new_tail` 满足 `(new_tail >> 1) % 32 == 31`（缝合位），`next_index = new_tail + 2`，所以 `(next_index >> 1) % 32 == (31 + 1) % 32 == 0`，即下一块的 offset 0。这正是「跳过缝合位落到下一块起点」。

---

### 4.3 offset 计算与 WRITE 置位（发布协议）

#### 4.3.1 概念说明

`push` 抢到位置后，要把值写进对应的 `Slot`。这里有两个要点：

1. **从 `tail` 解码出 `offset`，定位槽**：`offset = (tail >> SHIFT) % LAP`。注意 `get_unchecked(offset)` 是 unsafe 的越界检查省略——因为 `offset` 在 `0..BLOCK_CAP` 内（缝合位已在循环开头被 `continue` 排除），数学上保证不越界。
2. **先写值，再用 `fetch_or(WRITE, Release)` 发布**：值的写入是裸 `write`（plain store），没有同步语义；同步靠随后那次 `fetch_or(WRITE, Release)`。`Release` 序保证：消费者用 `Acquire` 读到 `WRITE` 位时，一定也能看到之前的值写入。这就是 2.3 说的「发布协议」。

消费者侧由 `Slot::wait_write` 配合：

[src/seg_queue.rs:45-50](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L45-L50) — `while state.load(Acquire) & WRITE == 0 { snooze }`。`Acquire` load 与生产者的 `Release` fetch_or 配对，构成 happens-before 关系。

#### 4.3.2 核心流程

```
# 生产者（push 的 Ok 分支尾部）:
slot = block.slots[offset]
slot.value.write(MaybeUninit::new(value))   # 裸写，无同步
slot.state.fetch_or(WRITE, Release)          # 发布：从此刻起消费者可见

# 消费者（pop 的 Ok 分支，见 4.4）:
slot = block.slots[offset]
slot.wait_write()                            # 自旋直到 WRITE 被置（Acquire）
value = slot.value.read().assume_init()      # 安全读取
```

#### 4.3.3 源码精读

**offset 计算**（循环每一轮开头）：

[src/seg_queue.rs:222](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L222) — 一行解码，把全局 `tail` 折算成块内偏移。

**写值 + 发布**：

[src/seg_queue.rs:278-281](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L278-L281) — 注意顺序：`write` 在前，`fetch_or(WRITE, Release)` 在后。`MaybeUninit::new(value)` 把 `value` 包成 `MaybeUninit`，再 `write` 进 `UnsafeCell`。`fetch_or` 是原子「读-改-写」，即使多个生产者也不会同时写同一个 slot（slot 的占用由 `tail` 的 CAS 唯一决定），所以这里 `fetch_or` 实际上是「置位并发布」。

**消费者侧的等待**：

[src/seg_queue.rs:427-430](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L427-L430) — `slot.wait_write()` 之后才 `read().assume_init()`。这就是 2.4 提到的关键：消费者可能抢在生产者写值之前就 CAS 占住了 head，所以必须先等 `WRITE`。

#### 4.3.4 代码实践

**实践目标**：验证「先写值后置 WRITE」的发布协议在 spsc 下保证消费者永远读到已写入的值。

**操作步骤**：

1. 阅读 [tests/seg_queue.rs:102-126](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/tests/seg_queue.rs#L102-L126) 的 `spsc` 测试。消费者用 `loop { if let Some(x) = q.pop() { assert_eq!(x, i); break; } }` 严格断言收到的值等于期望序号。
2. 思考：如果没有 `wait_write`，即消费者 `read().assume_init()` 不等 `WRITE` 直接读，这个断言在什么竞态下会失败（读到未初始化内存 / 读到旧值）？
3. （可选运行）`cargo test -p crossbeam-queue spsc --release`，观察在 100_000 次往返下测试稳定通过。

**需要观察的现象**：当前实现下测试稳定通过。若去掉 `wait_write`（不要真的改源码，仅推理），当生产者刚 CAS 完 `tail` 但还没 `write` 时，消费者可能 CAS 完 `head` 并直接 `read`，读到 `MaybeUninit` 的未初始化内存——这是 UB，断言可能给出任意值或崩溃。

**预期结果**：当前源码下 spsc 测试通过；推理说明 `wait_write` 是必需的。

#### 4.3.5 小练习与答案

**练习 1**：`fetch_or(WRITE, Release)` 改成 `fetch_or(WRITE, Relaxed)` 会出什么问题？

**参考答案**：`Relaxed` 不建立 happens-before。消费者 `wait_write` 里的 `Acquire` load 虽然能看见 `WRITE` 被置位（因为原子位本身的可见性），但**不保证能看见之前的 `slot.value.write`**——处理器或编译器可能把值写入重排到置位之后。于是消费者可能看到 `WRITE=1` 但读到未写入完成的槽。必须用 `Release` 把「值写入」发布出去。

**练习 2**：`slot.value.get().write(...)` 为什么不会和其他生产者写同一个槽冲突？

**参考答案**：每个槽由唯一的 `tail` 值占用，而 `tail` 的占用通过 `compare_exchange_weak` 互斥地分配——两个生产者不可能 CAS 成功同一个 `tail`。所以任一槽在同一「圈」内只会有一个生产者写入，`write` 之间无数据竞争（从单线程语义看）。

---

### 4.4 pop 的 HAS_NEXT 缓存与空队列判定

#### 4.4.1 概念说明

`pop` 与 `push` 对称：它推进 `head.index`，从队头取值。但它有一个 `push` 没有的难题——**如何判定队列空了？** 空判定要比较 `head` 和 `tail`，而每次 pop 都去读 `tail` 比较昂贵。`SegQueue` 用一个**缓存位 `HAS_NEXT`**（index 的 bit0）来规避：

- 一旦某次 pop 确认「head 和 tail 已不在同一个块里」（说明队列很长、肯定有下一块），就把 `HAS_NEXT` 位置进 `head.index`。
- 此后从这个 head 往后的所有 pop，因为 `new_head & HAS_NEXT != 0`，就**跳过空判定**，直接 CAS 取值——它们「知道」后面一定还有块。
- 只有当 `new_head & HAS_NEXT == 0`（即还没确认有下一块，通常是在第一块内、且队列较短）时，才需要读 `tail` 做权威的空判定。

这是一种「乐观缓存 + 惰性求证」的模式：越靠近队尾（块数多）越乐观，越靠近队头第一块越谨慎。

#### 4.4.2 核心流程

pop 主循环伪代码：

```
load head.index, head.block
loop:
    offset = (head >> SHIFT) % LAP
    if offset == BLOCK_CAP:           # 缝合位：别的线程正在换块
        snooze; reload head/block; continue

    new_head = head + (1 << SHIFT)    # +2

    if new_head & HAS_NEXT == 0:      # 还没确认「有下一块」，需权威空判定
        fence(SeqCst)
        tail = tail.index.load(Relaxed)
        if (head>>SHIFT) == (tail>>SHIFT):   # 同位置 -> 空
            return None
        if (head>>SHIFT)/LAP != (tail>>SHIFT)/LAP:  # 不同块 -> 一定有下一块
            new_head |= HAS_NEXT

    if block == null:                 # 首个 push 还没完成
        snooze; reload; continue

    CAS head.index: head -> new_head
        Ok  -> 若到块尾则跨块(wait_next) + 更新 head.block/index
               wait_write + read 值
               置 READ / 按需 destroy（见 u3-l3）
               return Some(value)
        Err -> head = 返回值; reload block; spin; 重试
```

**为什么 `HAS_NEXT` 位能一直保留？** 因为 `new_head = head + 2`，加 2（二进制 `10`）不改变 bit0。所以一旦某次把 `HAS_NEXT`（bit0）置 1，后续每次 `+2` 都保留它——这个位是「粘性的」，永久标记「已经跨过至少一个块边界」。

**空判定的两层比较**：

- \((\text{head} \gg \text{SHIFT}) == (\text{tail} \gg \text{SHIFT})\)：head 和 tail 的「逻辑元素序号」相同 → 队列空。
- \((\text{head} \gg \text{SHIFT})/\text{LAP} \neq (\text{tail} \gg \text{SHIFT})/\text{LAP}\)：head 和 tail 在**不同的块号**里 → 队列至少跨了一块 → 一定有下一块 → 置 `HAS_NEXT`。

`fence(SeqCst)` 的作用是给「读 head」和「读 tail」这对操作建立一个全局排序点，保证空判定不会因为乱序而误判（精确论证见 [u4-l1](u4-l1-atomic-orderings-fence.md)）。

#### 4.4.3 源码精读

**offset 与缝合位处理**（与 push 对称）：

[src/seg_queue.rs:371-379](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L371-L379) — 消费者侧也会遇到 `offset == BLOCK_CAP`（别的消费者/生产者正在换块），此时 snooze 等待并重读。

**HAS_NEXT 缓存与空判定**（本模块核心）：

[src/seg_queue.rs:381-396](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L381-L396) — 逐行对应上面的伪代码。注意空判定 `return None` 直接退出函数；而「不同块」只把 `HAS_NEXT` 或进 `new_head`，**不返回**，继续往下走 CAS。

**首个 push 未完成时的等待**：

[src/seg_queue.rs:400-405](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L400-L405) — 若 `head.block` 还是 null，说明还没有任何 push 完成（第一个生产者正在分配 block 0）。消费者 snooze 等待，直到 `head.block` 被首个 push 镜像写入（见 4.1 的首块分配）。

**CAS 推进 head + 跨块 + 读值**：

[src/seg_queue.rs:408-440](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L408-L440) — CAS 成功后：
- 若 `offset+1 == BLOCK_CAP`（读到本块最后一格），通过 [`wait_next`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L417) 等待并取到下一块指针，把 `head.block`/`head.index` 推进到下一块，并据下一块的 `next` 是否已安装决定是否给新的 `next_index` 带上 `HAS_NEXT`。
- 然后 `wait_write` + `read` 取值（4.3 的发布协议）。
- 最后处理块回收（置 `READ`、必要时 `destroy`），这部分留给 [u3-l3](u3-l3-segqueue-block-destroy.md)。

#### 4.4.4 代码实践

**实践目标**：通过阅读 `smoke` 测试，理解 pop 的「空返回 None」与「有值返回 Some」两条路径。

**操作步骤**：

1. 阅读 [tests/seg_queue.rs:6-15](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L6-L15) 的 `smoke` 测试。
2. 对照源码跟踪 `q.pop()` 在「队列有一个元素」时走哪条分支（`new_head & HAS_NEXT == 0` → 读 tail → `head>>SHIFT != tail>>SHIFT` 且同块 → 不置 HAS_NEXT → CAS 成功 → 读值返回 `Some`）。
3. 跟踪第二次 `q.pop()`（队列已空）走哪条分支（`head>>SHIFT == tail>>SHIFT` → `return None`）。
4. 运行 `cargo test -p crossbeam-queue smoke -- --nocapture` 确认通过。

**需要观察的现象**：测试通过。两次 pop 分别命中 4.4.3 里的「取值」路径和「空判定 return None」路径。

**预期结果**：`smoke` 测试通过；你能用自己的话描述两条路径的差异。

#### 4.4.5 小练习与答案

**练习 1**：为什么空判定用 `head >> SHIFT == tail >> SHIFT`，而不是直接 `head == tail`？

**参考答案**：`head`/`tail` 的 bit0 是 `HAS_NEXT` 元数据位，可能与「逻辑元素序号」不一致（比如 head 带了 `HAS_NEXT` 而 tail 没带）。右移 `SHIFT` 位把 bit0 抹掉，比较的是纯粹的「逻辑元素序号」，避免 `HAS_NEXT` 位干扰空判定。

**练习 2**：假设队列很长（head 和 tail 已经隔了很多块），某个消费者的 `head.index` 里 `HAS_NEXT` 早已置位。它每次 pop 还会去读 `tail.index` 吗？

**参考答案**：不会。因为 `new_head & HAS_NEXT != 0`，整个 `if new_head & HAS_NEXT == 0 { ... }` 块被跳过，直接进入 CAS。这正是 `HAS_NEXT` 缓存的意义——长队列场景下 pop 完全不必碰 `tail`，减少跨核缓存行争用。

**练习 3**：`fence(SeqCst)` 出现在 `new_head & HAS_NEXT == 0` 分支里、读 `tail` 之前。粗略说说它的必要性。

**参考答案**：空判定要比较 head 与 tail 两个独立原子量。没有 fence 时，处理器可能把「读 head」和「读 tail」重排，导致看到一个「未来的 tail」却配「过时的 head」，误判为非空（或反之）。`SeqCst` fence 在两者之间建立顺序，使读到的 (head, tail) 是一个相对一致的组合，让「返回空」这一结论更可靠。完整论证见 [u4-l1](u4-l1-atomic-orderings-fence.md)。

## 5. 综合实践

**任务**：手算并画出「连续 `push` 31 个元素」的过程中 `tail.index`、`tail.block`、当前 block 的 `next` 指针的变化，标出**第 31 个 push 触发新块安装**的时刻。

设 block 0 的指针为 `B0`，block 1 为 `B1`。初始 `tail.index=0`、`tail.block=null`、`B0.next=null`。

关键不变量：`new_tail = tail + 2`；`offset = (tail >> 1) % 32`；当 `offset == 30`（即本块最后一格）且 CAS 成功时，安装下一块并把 `tail.index` 跳到下一块的 offset 0。

| push # | 进入时 tail.index | offset | 写入的槽 | CAS 后 tail.index | 安装动作 | tail.block | 当前块.next | head.block |
|---|---|---|---|---|---|---|---|---|
| 1  | 0  | 0  | B0[0]  | 2  | **首块分配**：CAS tail.block null→B0，并 head.block=B0 | B0 | null | **B0**（从 null 变 B0） |
| 2  | 2  | 1  | B0[1]  | 4  | 无 | B0 | null | B0 |
| 3  | 4  | 2  | B0[2]  | 6  | 无 | B0 | null | B0 |
| …  | …  | …  | …      | …  | …  | …  | …  | …  |
| 30 | 58 | 29 | B0[29] | 60 | 无（offset+1=30≠31） | B0 | null | B0 |
| **31** | **60** | **30** | **B0[30]** | **64** | **预分配 B1；CAS 60→62 后立即安装：tail.block=B1、tail.index=64、B0.next=B1** | **B1** | **B0.next = B1** | B0 |
| 32 | 64 | 0  | B1[0]  | 66 | 无 | B1 | B1.next=null | B0 |

**关键观察点**：

1. **第 1 个 push**：唯一一次触发「首块分配」。`tail.block` 与 `head.block` 同时从 null 变为 B0。
2. **第 31 个 push**：唯一一次触发「安装下一块」。`offset` 从 0 一路涨到 30，写满 B0 的最后一格。此 push 内 `tail.index` 先被 CAS 到 62（缝合位，瞬态），随即被 store 到 64（B1 的 offset 0）；`tail.block` 从 B0 切到 B1；`B0.next` 从 null 变为 B1。从此生产者开始往 B1 写。
3. **缝合位 62（offset 31）从未被任何元素占用**：它只在 CAS 与 store 之间瞬态存在，是「块写满、正换块」的信号。这就是 `BLOCK_CAP = LAP - 1 = 31` 的物理含义——每块在 index 空间占 32 个 offset 槽位，但只有 31 个存数据。
4. **`head.block` 在 push #1 之后到第 31 次 push 之间始终是 B0**：除首块分配那一次（push #1 通过 [src/seg_queue.rs:248](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L248) 把 `head.block` 从 null 镜像成 B0）外，push 不再改 `head.block`，只有 pop 跨块时才推进它。所以即便 tail 已经进了 B1，只要消费者还没消费，head 还停在 B0。

**如何验证你的手算**：写一个小测试，在第 31 次 push 前后用 `dbg!` 打印不可行（这些字段是私有的）。可行的方式是观察**可观测的副作用**：连续 push 31 个值后再 push 第 32 个，然后 pop 32 次，断言顺序为 `0..32`（参考 [tests/seg_queue.rs:36-52](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/tests/seg_queue.rs#L36-L52) 的 `len` 测试思路）。若跨块的 index 推进算错，FIFO 顺序或 `len()` 就会对不上。

```rust
// 示例代码：验证 31 这个「换块边界」不会破坏 FIFO
use crossbeam_queue::SegQueue;

let q = SegQueue::new();
for i in 0..32usize {
    q.push(i);
    // 第 31 次 (i==30) push 时内部安装了第二块；这是不可见的，但可通过正确性间接验证
}
for i in 0..32usize {
    assert_eq!(q.pop(), Some(i), "FIFO 破坏在 {}", i);
}
assert!(q.pop().is_none());
```

运行 `cargo test -p crossbeam-queue` 应全绿。这间接确认了「第 31 个 push 安装下一块」的 index 算术与 FIFO 语义一致。

## 6. 本讲小结

- `push` 是乐观 CAS 循环：解码 `offset` →（必要时）懒分配首块 → CAS 推进 `tail` → 写值并置 `WRITE`；失败则带最新值重试。
- **首块懒分配**：`new()` 时 `head.block`/`tail.block` 都是 null，第一个 push 用 CAS 选举出唯一的 block 0，赢家同时镜像写入 `head.block`，输家回收自己 new 的块。
- **块末尾安装**：在 `offset+1 == BLOCK_CAP` 时提前预分配下一块，CAS 成功后一次性把它挂到 `tail.block`、`tail.index`（跳过缝合位）和当前块 `.next`，让后续生产者「零等待」续写。
- **缝合位**：`offset == 31` 不存数据，是「块写满、正换块」的瞬态信号；这就是 `BLOCK_CAP = LAP - 1 = 31` 的由来。
- **WRITE 发布协议**：生产者先裸写值、再 `fetch_or(WRITE, Release)`；消费者用 `wait_write`（`Acquire`）自旋等到 `WRITE` 再 `read`，解决「消费者抢到槽但生产者还没写完」的竞态。
- **pop 的空判定与 HAS_NEXT 缓存**：用 index 的 bit0 缓存「是否存在下一块」，长队列下 pop 完全不必读 `tail`；只有 `HAS_NEXT==0` 时才用 `head>>SHIFT == tail>>SHIFT` 做权威空判定，并用 `fence(SeqCst)` 保证两次读的相对一致。

## 7. 下一步学习建议

本讲把 `push`/`pop` 的**主链路**讲完了，但刻意留下了三块「未展开」的内容，正好对应后续讲义：

1. **块的内存回收**：pop 里出现的 `Block::destroy`、`DESTROY` 位、`wait_next`、`wait_write` 背后的协调逻辑，请接着读 [u3-l3：Block 的内存回收](u3-l3-segqueue-block-destroy.md)。
2. **`len` / `Drop` / 迭代器**：跨块的 index 算术如何在 `len()` 里算出正确元素数、`Drop` 如何遍历回收整条块链表，请读 [u3-l4](u3-l4-segqueue-len-drop-iter.md)。
3. **内存序的精确论证**：本讲反复出现的 `SeqCst`/`Acquire`/`Release`/`fence` 为何不能用更弱的序，请读 [u4-l1：原子内存序与 fence](u4-l1-atomic-orderings-fence.md)。

建议在进入 u3-l3 前，先把本讲「综合实践」的手算表自己推一遍——它能让你在阅读块回收代码时，对「哪个线程在哪个块上」有清晰的空间感。
