# Michael-Scott 无锁队列 sync/queue

## 1. 本讲目标

本讲继续深入 crossbeam-epoch 的「基础设施层」，讲解 `src/sync/queue.rs` 实现的 **Michael-Scott 无锁队列**。它和上一讲的无锁链表（u6-l20）同属 `src/sync/`，但套路完全不同：链表是「head 单指针 + 逻辑删除 + 遍历者清扫」，而队列是「head/tail 双指针 + 哨兵节点 + helping 协作」。这个队列不是摆设——收集器的全局垃圾袋就存在它里面（`Global.queue: Queue<SealedBag>`），`Global::collect` 用它的「条件弹出」`try_pop_if` 只取出已过宽限期的袋。所以读懂它，等于读懂 EBR 回收链路的「最后一棒」。

学完本讲，你应当能够：

- 说清楚**哨兵节点（sentinel）**的作用，以及为什么队列永远至少有一个节点。
- 解释 `tail` 指针**可以滞后**于真实尾节点这一设计，以及生产者之间如何 **helping（互助推进 tail）** 来保证进度。
- 读懂 `push_internal` 的两步 CAS（先链接新节点、再推进 tail）和 `pop_internal` 的 head 前移 + `defer_destroy`。
- 回答两个关键问题：① 生产者 A 推进 tail 失败时，生产者 B 如何 helping？② `pop` 在 `head == tail` 时为什么还要尝试推进 tail？
- 把 `try_pop_if` 与 `Global::collect` 的 `is_expired` 条件对应起来，理解「条件弹出」在真实代码里的用途。

## 2. 前置知识

本讲假设你已掌握前置讲义，这里只做最小回顾：

- **CAS（compare_exchange）**（u2-l8）：`compare_exchange(current, new, succ_order, fail_order, guard)` 期望原子值仍是 `current`，是则换成 `new` 并返回成功，否则失败并把「真实当前值」带回来。本讲的「链接新节点」「推进 head/tail」全靠它。失败是常态——并发下会输掉竞态，调用者负责重试。
- **`Atomic` / `Shared` / `Owned` 与 `'g` 生命周期**（u2-l5～u2-l7）：`Atomic<T>` 是共享原子指针；`load(&Guard)` 借出 `Shared<'g, T>`；`Owned::into_shared` 把独占指针交成借用的 `Shared`。本讲里 `head`/`tail` 都是 `Atomic<Node<T>>`，节点在 `Shared` 之间流转。
- **`Guard`、`defer_destroy` 与宽限期**（u3-l9/u3-l10）：`guard.defer_destroy(ptr)` 把一个 `Shared` 升级为 `Owned` 后 drop，但真正执行要等到全局 epoch 前进满 2 步（宽限期）。本讲 `pop` 摘下旧哨兵后正是用它延迟回收——别的消费者可能还握着指向旧哨兵的 `Shared`，立刻 free 会 use-after-free。
- **`unprotected()`**（u3-l9）：返回不真正 pin 的假守卫，`defer` 在它下面立即执行。本讲在 `Queue::new` 与 `Queue::drop` 这种「单线程、无并发」场景里用到。
- **宽限期判据 `is_expired`**（u4-l16）：`global_epoch.wrapping_sub(sealed_epoch) >= 2`。这是 4.4 里 `try_pop_if` 的弹出条件。

一句话提示：本讲的 `Node.data` 是 `MaybeUninit<T>`——哨兵节点和「已被 pop 的节点」都不持有效值。`assume_init_read` 用「按位读出」把值搬走，节点随即变成新的空哨兵。这个「哨兵接力」是整篇的机关。

## 3. 本讲源码地图

本讲主要涉及两个文件：

| 文件 | 作用 |
| --- | --- |
| [src/sync/queue.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/queue.rs) | Michael-Scott 无锁队列的完整实现：`Queue`/`Node`、`push`/`push_internal`、`pop_internal`/`pop_if_internal`、`try_pop`/`try_pop_if`、`Drop`，以及单元测试（含 MPMC 压测）。 |
| [src/internal.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs) | 收集器内部实现。`Global.queue` 就是 `Queue<SealedBag>`；`push_bag` 往里塞封箱垃圾袋，`collect` 用 `try_pop_if(is_expired)` 取出已过宽限期的袋并 drop。 |

队列被使用的三个关键点：

- [src/internal.rs:165-174](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L165-L174)：`Global` 持有 `queue: Queue<SealedBag>`，注释写明这是「延迟函数袋的全局队列」。
- [src/internal.rs:191-198](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L191-L198)：`push_bag` 把本地袋封箱（盖当前 epoch 戳）后 `queue.push`。
- [src/internal.rs:217-225](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L217-L225)：`collect` 用 `queue.try_pop_if(|sb| sb.is_expired(global_epoch), guard)` 条件弹出至多 8 个过保袋并 drop。

## 4. 核心概念与源码讲解

### 4.1 Queue/Node 结构与哨兵节点初始化

#### 4.1.1 概念说明

朴素的队列用「head 指向队首、tail 指向队尾」两个指针。无锁化时会立刻撞上两个麻烦：

1. **空队列的特判**：队列空时 `head == tail == null`，push 要同时建首节点、改 head、改 tail；pop 要判空。这些「同时改两个指针」的操作无法用单次 CAS 完成，是并发地狱。
2. **pop 之后的回收**：摘下的节点可能还被别的消费者引用，不能立刻 free。

Michael-Scott 的解法对这两点各给一招：

- **哨兵节点（sentinel/dummy node）**：队列初始化时就放一个「永远在队首、不存有效值」的空节点。这样队列**任何时候都至少有一个节点**，`head` 永远指向哨兵，`tail` 永远指向「真实尾或接近真实尾的节点」。push 永远是「往某个节点的 next 上挂新节点」，pop 永远是「把 head 前移到下一个节点」——没有任何空特判。
- **`defer_destroy`**：pop 摘下的旧哨兵交给 EBR 延迟回收（4.3 详述）。

第二个关键设计写在文件顶部注释里：**`tail` 可以滞后于真实尾**。即 `tail` 指向的节点，其 `next` 可能已经非空（已经被别人挂了新节点）。这不是 bug，而是为了把「链接新节点」与「推进 tail」解耦成两次独立 CAS，从而允许**别的生产者帮忙推进 tail（helping）**。4.2 会专讲。

#### 4.1.2 核心流程

队列的物理形态（单链表 + 哑节点）：

```
head -> [S 哨兵] -> [D1] -> [D2] -> ... -> [Dk] -> null
  ^                                       ^
  |                                       |
head 始终指向哨兵                       tail 指向真实尾或其前一个节点
（哨兵的 data 槽空）                     （D1..Dk 的 data 槽都有值）
```

不变量：

- **`head` 永远指向当前哨兵**（一个 `data` 为空的节点）；`head.next` 为 `null` 当且仅当队列为空（只有哨兵）。
- **`tail` 指向真实尾或比真实尾滞后一个节点**（瞬时态）。`tail.next == null` 时 `tail` 确为真实尾；`tail.next != null` 时 `tail` 滞后，需被推进。
- **非哨兵节点的 `data` 在入队时初始化、被 pop 后再次变空**（成为新哨兵）。故 `data` 用 `MaybeUninit<T>`。

`Queue::new` 的初始化只有一件事：分配哨兵，让 `head = tail = 哨兵`。

#### 4.1.3 源码精读

**`Queue<T>` 结构**——只有两个原子指针：

[src/sync/queue.rs:23-27](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/queue.rs#L23-L27)：`head` 与 `tail` 都是 `CachePadded<Atomic<Node<T>>>`。`CachePadded` 把它们各自填充到独立缓存行——因为 head 是消费者的争用热点、tail 是生产者的争用热点，若它们（或与相邻字段）共享缓存行，会触发伪共享（false sharing）拖慢并发。这与 `Local`/`Global` 里热字段用 `CachePadded` 是同一手法（u4-l15/u4-l16）。

[src/sync/queue.rs:20-22](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/queue.rs#L20-L22)：文件顶部的表示说明注释，明确三点：① 单链表 + 前置哨兵；② `tail` 可能滞后于真实尾；③ 非哨兵节点要么全是 `Data`、要么全是 `Blocked`（本讲只讲 `Data` 路径，`Blocked` 是给「阻塞 pop 的线程」留的请求槽，crossbeam 当前未在收集器里用到）。

**`Node<T>` 结构**——一个数据槽 + 一个 next 指针：

[src/sync/queue.rs:29-39](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/queue.rs#L29-L39)：`data: MaybeUninit<T>` 与 `next: Atomic<Node<T>>`。注释解释为何用 `MaybeUninit`：哨兵节点永远没值；其它节点「从 push 开始持值，直到被 pop 出去」，pop 后空节点会被交给收集器销毁。

[src/sync/queue.rs:41-43](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/queue.rs#L41-L43)：`unsafe impl<T: Send> Sync/Send for Queue<T>`。注释点明：「任意单个 `T` 不会被并发访问，所以无需 `T: Sync`」。为什么？因为每个 `T` 从 push 进队列到被 pop 出去，**只被一个线程（那个赢了的消费者）读出**（`assume_init_read`），不存在两个线程同时读同一个值；只要 `T` 能在线程间转移（`Send`）即可。注意 4.3 的 `pop_if_internal` 因为要在线程间共享 `&T` 给条件函数判读，额外要求 `T: Sync`。

**`Queue::new`——哨兵初始化**：

[src/sync/queue.rs:47-63](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/queue.rs#L47-L63)：先建空的 `head`/`tail`（都为 `null()`），再分配一个 `data` 为 `uninit()`、`next` 为 `null()` 的哨兵节点；在 `unsafe` 块里用 `unprotected()`（构造期无并发）把哨兵 `into_shared`，再让 `head`/`tail` 都 `store` 成它。`store` 用 `Relaxed`——此时队列尚未发布给别的线程，无需同步。返回的 `Queue` 一出场就满足「head=tail=哨兵」不变量。

#### 4.1.4 代码实践（源码阅读型）

**实践目标**：确认「队列永远至少有一个节点」与哨兵的「空 data」两个不变量。

**操作步骤**：

1. 阅读 [src/sync/queue.rs:47-63](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/queue.rs#L47-L63)，确认 `new` 出来的队列里 `head` 与 `tail` 指向**同一个**哨兵，且哨兵的 `data` 是 `MaybeUninit::uninit()`。
2. 阅读 [src/sync/queue.rs:205-217](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/queue.rs#L205-L217) 的 `Drop for Queue`：它先 `while try_pop().is_some() {}` 把所有数据节点 pop 空，**最后**还要单独 `drop(sentinel.into_owned())` 销毁残留的哨兵——反证「即便队列已空，哨兵仍在」。
3. 思考：如果 `new` 不建哨兵、让 `head = tail = null()`，那么 `pop_internal` 里 `head.deref()`（[L122](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/queue.rs#L122)）在空队列时会发生什么？

**需要观察的现象 / 预期结果**：去掉哨兵后，`head` 为 `null()`，`head.deref()` 解引用空指针是未定义行为（UB）。哨兵的存在让 `head` 永远非空、`head.deref()` 永远合法，从而把「判空」从「head 是不是 null」简化成「head.next 是不是 null」（[L124/L141](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/queue.rs#L124)）。本步为推理验证，无需运行。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Queue::new` 里用 `unprotected()` 而不是正常的 `pin()`？

**答案**：构造期队列尚未发布给任何其它线程，不存在并发访问，自然不需要 pin 的保护；且 `into_shared` / `store` 都不依赖 Guard 提供的生命周期保护（节点立即被 `head`/`tail` 持有）。`unprotected()` 在这里既正确又零开销。`Queue::drop`（[L207-208](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/queue.rs#L207-L208)）同理——析构时队列也已被独占。

**练习 2**：`head` 和 `tail` 各自被谁频繁写？

**答案**：`tail` 几乎只被生产者写（push 链接新节点 + 推进 tail）；`head` 几乎只被消费者写（pop 前移 head）。这正是它们各自用 `CachePadded` 独占缓存行的理由——生产者群和消费者群互不干扰。注意 `pop` 里有一个「推进 tail」的写（4.3），属于消费者偶发的 helping，不改变「tail 主写者是生产者」的整体格局。

### 4.2 push_internal / push：tail 滞后与 helping

#### 4.2.1 概念说明

入队的难点在于：**「链接新节点」和「推进 tail」是两件事，必须分成两次 CAS**。一次 CAS 只能动一个字，没法「把新节点挂到 tail.next 的同时把 tail 前移」。MS 队列的策略是：

1. 先 CAS 把新节点挂到「当前 tail 节点的 `next`」上（`tail.next: null -> new`）。这一步成功，新节点**逻辑上已入队**。
2. 再 CAS 把 `tail` 指针前移到新节点（`tail: old -> new`）。这一步只是「书架整理」，失败也无妨。

把第 2 步独立出来有个巨大好处：**如果原生产者在第 1 步成功后、第 2 步前被打断（停滞），别的生产者可以从第 1 步留下的痕迹（`tail.next != null`）看出 tail 滞后了，主动帮它把 tail 推进。** 这就是 **helping（互助）**。它保证了「即便某个生产者卡住，队列仍能继续前进」——这是无锁算法「系统级前进保证（lock-free）」的关键：至少有一个线程在推进。

> 小心区分两种「失败」：第 1 步的 CAS 失败 = 输掉竞态（有人先挂了节点），需重试；第 2 步的 CAS 失败 = tail 已被别人推进，皆大欢喜，忽略即可。

#### 4.2.2 核心流程

`push_internal(onto, new, guard)` 的判定树（`onto` 是调用者快照的 tail）：

```
读 onto.next
├─ onto.next 非空  => onto 不是真实尾（tail 滞后了）
│      「helping」：CAS(tail: onto -> onto.next)   # 帮忙推进 tail，结果忽略
│      return false                                # 本次没挂上 new，调用者重试
│
└─ onto.next 为空  => onto 像是真实尾
       CAS(onto.next: null -> new)                 # 链接新节点（入队的线性化点）
       ├─ 成功：CAS(tail: onto -> new)             # 推进 tail，结果忽略
       │          return true
       └─ 失败：输掉竞态，return false             # 调用者重试
```

`push` 外层只是 `loop { 读 tail 快照; if push_internal(tail, new, guard) { break; } }`——失败就重新读 tail 再来。

**线性化点**：`onto.next` 的 CAS 成功那一刻，新节点即对全体线程可见、可被 pop。之后的 tail 推进只是优化（让后续 push 能挂到更靠后的位置），不影响正确性。

**helping 为何保证进度**：假设生产者 A 在「挂了节点 X、还没推进 tail」时被抢占。此时 `tail` 仍指向旧节点 S，但 `S.next = X`（非空）。任何后续生产者 B 读到 `tail = S`，进 `push_internal` 会走「onto.next 非空」分支，用一次 CAS 把 `tail` 从 S 推进到 X，然后返回 false 让自己重试。于是 tail 被 B 推进了，队列恢复了「tail 指向真实尾」的健康态。A 即便永远不醒，队列也不受影响。

**内存序**：读 `tail` 与 `onto.next` 用 `Acquire`（要看到节点内容与链接）；链接新节点的 CAS 用 `Release`（成功时发布新节点，让消费者的 `Acquire` 读到完整数据）/ `Relaxed`（失败）；推进 tail 的 CAS 用 `Release`/`Relaxed`。

#### 4.2.3 源码精读

**`push_internal`——判断真实尾 + helping + 链接 + 推进**：

[src/sync/queue.rs:67-97](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/queue.rs#L67-L97)：函数被标 `#[inline(always)]`，因为它处在 push 的热路径上。逐段对应：

- [L75-77](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/queue.rs#L75-L77)：`o = onto.deref()`（unsafe，契约是 `onto` 来自合法的 `tail` 快照），读 `o.next`；若 `next.as_ref().is_some()`（非空）→ 走 helping 分支。
- [L79-82](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/queue.rs#L79-L82)：**helping**——`tail.compare_exchange(onto, next, Release, Relaxed, guard)`，把 tail 从滞后的 `onto` 推进到 `next`。`let _ =` 说明结果无所谓（成功是帮忙、失败是别人已推进，都行）。返回 `false` 让外层重试。
- [L85-88](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/queue.rs#L85-L88)：`onto.next` 为空，onto 像真实尾。用 CAS 把 `onto.next` 从 `Shared::null()` 换成 `new`——**这是入队的线性化点**。`is_ok()` 决定是否成功挂上。
- [L89-94](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/queue.rs#L89-L94)：挂成功后，CAS 推进 `tail: onto -> new`，结果同样忽略。

注意两次推进 tail 的 CAS 都用 `Release`（成功）：发布「tail 已移动」给读 tail 的线程，配合它们读 tail 的 `Acquire`。

**`push`——外层循环**：

[src/sync/queue.rs:99-116](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/queue.rs#L99-L116)：先用 `Owned::new` 建节点（`data` 用 `MaybeUninit::new(t)` 初始化为入队值），`into_shared(guard)` 转成 `Shared`（所有权让渡，详见 u2-l6）。然后 `loop`：每次 `Acquire` 读 tail 快照，调 `push_internal`，成功就 `break`。注释 [L108](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/queue.rs#L108) 「optimistically start at tail」——乐观地从 tail 找挂载点，失败（含 helping 后）重读再来。

#### 4.2.4 代码实践（必做：画 helping 时序）

**实践目标**：亲手画出「生产者 A 推进 tail 失败时，生产者 B 如何 helping」的时序。这是本讲的核心练习。

**初始状态**：队列空，`head = tail = S`（哨兵），`S.next = null`。

**操作步骤**：在纸上按下表逐行推演（S=哨兵，X、Y 是两个待入队值）：

| 时刻 | 线程 | 操作 | 队列形态（head→…，标注 tail） |
| --- | --- | --- | --- |
| t0 | — | 初始 | `head=tail=S`, `S.next=null` |
| t1 | A | `push(X)`：读 `tail=S`；`push_internal(S,X)`：`S.next` 为空 → CAS(`S.next`: null→X) **成功** | `head=S → X`, `tail=S`（X 已逻辑入队，但 tail 还在 S） |
| t2 | A | **被抢占**，尚未执行推进 tail 的 CAS | （同上）|
| t3 | B | `push(Y)`：读 `tail=S`；`push_internal(S,Y)`：`S.next=X` 非空 → **helping**：CAS(`tail`: S→X) | `head=S → X`, `tail=X`；返回 false |
| t4 | B | `push` 循环重读 `tail=X`；`push_internal(X,Y)`：`X.next` 为空 → CAS(`X.next`: null→Y) **成功** | `head=S → X → Y`, `tail=X` |
| t5 | B | 推进 tail：CAS(`tail`: X→Y) | `head=S → X → Y`, `tail=Y`；B 完成 |
| t6 | A | 醒来，执行 CAS(`tail`: S→X) → **失败**（tail 已是 Y） | （不变；`let _` 忽略失败）|

**需要观察的现象**：

1. **t1 之后、t5 之前，`tail` 一度滞后**（指向 S，而真实尾是 X 或 Y）。这期间任何新生产者都会像 B 一样被引到 helping 分支。
2. **B 在 t3 帮 A 推进了 tail**（S→X），否则 B 自己的 Y 没法挂——因为 B 只能把新节点挂到「真实尾的 next」上，而 S 的 next 已被 A 占了。helping 让 B 不必等 A。
3. **A 在 t6 的 CAS 必然失败**（tail 已是 Y，不再等于 A 期望的 S），但 `let _` 把失败吞掉，毫无问题。

**预期结果**：你能解释「为什么 helping 是 lock-free 的关键」——A 卡住不阻止 B 入队。这正是 [src/sync/queue.rs:78-82](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/queue.rs#L78-L82) 那几行「帮助推进 tail」存在的意义。本步为源码阅读 + 推理，无需运行。

#### 4.2.5 小练习与答案

**练习 1**：`push_internal` 里两个推进 tail 的 CAS（helping 分支 [L79-81](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/queue.rs#L79-L81) 与链接成功后 [L90-93](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/queue.rs#L90-L93)），为什么都对结果用 `let _ =`？

**答案**：因为它们都是「尽力推进」。helping 分支：若 CAS 失败，说明别的线程已把 tail 推得更靠后，目的已达；链接成功后推进 tail：若失败，说明别的生产者或某个 helping 的消费者已推进，新节点照样逻辑入队了。两种情况下「tail 没被我推进」都不影响正确性，只影响后续 push 的效率，故忽略结果。

**练习 2**：为什么入队的线性化点是「`onto.next` 的 CAS 成功」，而不是「tail 推进成功」？

**答案**：因为 `onto.next: null -> new` 成功的那一刻起，新节点已挂在链表里，**任何消费者的 pop 都可能读到它**（pop 读的是 `head.next`，沿链走）。而 tail 推进只是让「下一次 push 从更靠后的位置开始找挂载点」，纯属性能优化。把线性化点定在 tail 推进会让「节点已可被 pop、却还没线性化」出现矛盾。

### 4.3 pop_internal / pop_if_internal：head 前移、推进 tail、defer_destroy

#### 4.3.1 概念说明

出队用同样的「哨兵接力」思想：**pop 不是把队首数据抠出来，而是把 head 前移一格，让原来的第二节点变成新哨兵，并销毁旧哨兵。**

具体说，队列形态是 `head -> [S 哨兵] -> [D 队首值] -> ...`。pop 做三件事：

1. 把 `head` 从哨兵 S 前移到 D（CAS `head: S -> D`）。现在 D 成了新哨兵。
2. 把 S（旧哨兵）`defer_destroy`——它马上要被回收，但别的消费者可能还指着它，得等宽限期。
3. 读出并返回 D 里的值（`D.data.assume_init_read()`）。

关键细节：**返回的值来自 `head.next`（即 D），而不是 `head`（S）。** 因为 S 是哨兵，它的 data 槽是空的。这一点常被初学者看反。

**为什么 pop 里也要推进 tail？** 这正是本讲实践任务要回答的第二个问题，先给结论：当 `head == tail` 时，若不推进 tail，tail 会指向一个「即将被 defer_destroy 的节点」，后续 push 读到这个 tail 并 `deref` 它就是 use-after-free。详见 4.3.4。

`pop_if_internal` 是 `pop_internal` 的「条件版」：多了一个 `condition(&T) -> bool`，只有在队首值满足条件时才真正 pop。它要求 `T: Sync`——因为多个消费者可能**同时**对同一个节点求值条件函数（共享 `&T`）。这个版本不是为普通队列准备的，而是为收集器的 `collect` 准备的：用 `is_expired` 当条件，只弹出已过宽限期的垃圾袋（4.4）。

#### 4.3.2 核心流程

`pop_internal(guard)` 的判定树：

```
读 head（Acquire），h = head.deref()
读 h.next（Acquire）
├─ next 为空  => 队列空（只有哨兵）  => Ok(None)
└─ next = n 非空：
     CAS(head: head -> n, Release/Relaxed)        # head 前移，线性化点
     ├─ 失败 => Err(())                            # 输掉竞态，调用者重试
     └─ 成功：
          读 tail（Relaxed）
          if head == tail:                          # ★ 旧 head 还被 tail 指着
              CAS(tail: tail -> n)                  #   推进 tail，避免悬挂
          guard.defer_destroy(head)                 # 延迟回收旧哨兵
          return Ok(Some(n.data.assume_init_read()))# 读出值（n 现在是新哨兵）
```

`pop_if_internal` 几乎相同，只是把「`next` 非空」分支再套一层 `condition(&n.data)` 判断：满足才走 CAS；不满足或队空都返回 `Ok(None)`。

**线性化点**：head 的 CAS 成功那一刻，该消费者「赢得」了这个队首值。失败的线程拿到 `Err(())`，由外层 `try_pop`/`try_pop_if` 的 `loop` 重试。

**两个易错点**：

- 返回值取自 `n`（= `head.next`，新哨兵），不是 `head`（旧哨兵）。
- `defer_destroy` 的是 `head`（旧哨兵），不是 `n`。

#### 4.3.3 源码精读

**`pop_internal`**：

[src/sync/queue.rs:119-143](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/queue.rs#L119-L143)：返回 `Result<Option<T>, ()>`——`Ok(None)` 表空、`Ok(Some(v))` 表成功、`Err(())` 表输掉竞态。逐行：

- [L121-123](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/queue.rs#L121-L123)：`Acquire` 读 head（与 push 链接新节点的 `Release` 配对，确保看到新节点的完整内容），`h = head.deref()`（unsafe），再 `Acquire` 读 `h.next`。
- [L124-141](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/queue.rs#L124-L141)：`match next.as_ref()`——`Some(n)` 走 pop，`None` 返回 `Ok(None)`（队空）。
- [L126-127](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/queue.rs#L126-L127)：CAS `head: head -> next`，`Release`（成功，发布 head 前移）/ `Relaxed`（失败）。
- [L128-135](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/queue.rs#L128-L135)：CAS 成功后的清理。`Relaxed` 读 tail（仅用于比较，不依赖同步）；`if head == tail` 则 CAS 推进 `tail: tail -> next`（**这就是 4.3.4 要详述的关键一步**）。
- [L136](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/queue.rs#L136)：`guard.defer_destroy(head)`——把旧哨兵延迟回收。
- [L137](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/queue.rs#L137)：`Some(n.data.assume_init_read())`——**按位读出 n 的值**返回。`assume_init_read` 是 `ptr::read` 语义，把值搬走，n 的 data 槽从此「空」，n 名正言顺地成为新哨兵。

**`pop_if_internal`——条件版**：

[src/sync/queue.rs:147-175](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/queue.rs#L147-L175)：签名 `fn pop_if_internal<F>(&self, condition: F, guard) where T: Sync, F: Fn(&T) -> bool`。结构同 `pop_internal`，唯一区别在 [L156-157](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/queue.rs#L156-L157)：匹配守卫 `Some(n) if condition(unsafe { &*n.data.as_ptr() })`——先对 `n.data` 求值条件，**通过才进 CAS**；`None | Some(_) => Ok(None)` 把「队空」与「条件不满足」合并返回（[L173](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/queue.rs#L173)）。注意 `T: Sync` 的必要性：多个消费者可能同时对**同一个** `n` 求条件，相当于共享 `&T`，故需 `Sync`。

**`try_pop` / `try_pop_if`——公开入口**：

[src/sync/queue.rs:180-202](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/queue.rs#L180-L202)：两者都是 `loop { if let Ok(head) = self.pop_internal(...) { return head; } }`——把「输掉竞态的 `Err(())`」自动重试掉，对外只暴露 `Option<T>`。

**`Drop for Queue`**：

[src/sync/queue.rs:205-217](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/queue.rs#L205-L217)：用 `unprotected()`（析构期无并发），先 `while try_pop().is_some() {}` 把数据节点全 pop 干净（pop 会 `defer_destroy` 旧哨兵，但在 `unprotected` 下 `defer` 立即执行），最后单独 `drop(sentinel.into_owned())` 销毁最终残留的那个哨兵。

#### 4.3.4 代码实践（必做：解释 head==tail 时为何推进 tail）

**实践目标**：回答本讲第二个核心问题——`pop` 在 `head == tail` 时为什么还要尝试推进 tail？这是 [src/sync/queue.rs:128-135](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/queue.rs#L128-L135) 那段代码存在的理由。

**场景推演**：考虑队列里只有一个真实数据节点 D 的瞬时态。由于「链接节点」与「推进 tail」是两次 CAS，存在一个窗口期：D 已经被挂上（`S.next = D`），但 `tail` 还没从 S 推进到 D。即：

```
head = S,  tail = S,  S.next = D,  D.next = null
```

此时一个消费者 pop：

1. 读 `head = S`，读 `head.next = D`（非空）。
2. CAS `head: S -> D` 成功。**现在 head = D**。
3. 读 `tail = S`。判断 `head（旧值 S）== tail（S）`？**是**。
4. 若**没有** [L131-135](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/queue.rs#L131-L135) 的推进：直接 `defer_destroy(S)`。于是 `tail` 仍指向 S——一个**已排定回收**的节点。
5. 下一个生产者 `push`：读 `tail = S`（[L109](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/queue.rs#L109)），进 `push_internal` 立刻 `onto.deref()`（[L75](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/queue.rs#L75)）解引用 S → **use-after-free**（S 可能已被宽限期后的回收线程释放）。

**正解**：[L131-135](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/queue.rs#L131-L135) 在 `defer_destroy(head)` **之前**先把 `tail` 从 S 推进到 D（`CAS(tail: S -> D)`）。推进后 `tail` 指向 D（新哨兵、存活），再回收 S 就安全了。注释 [L130](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/queue.rs#L130) 一句话点题：「Advance the tail so that we don't retire a pointer to a reachable node」——别让 `tail` 留在即将回收的节点上。

**为什么用 `if head == tail` 守卫、而不是无条件推进？** 因为只有「旧 head 恰好等于当前 tail」时，tail 才指向我们要回收的那个节点。若 `head != tail`（队列有 ≥2 个数据节点，或 tail 已被别人推进到更后），tail 本就不指向旧 head，无需也没意义去推进。这是一次精确的「只在必要时 helping」。

**预期结果**：你能向同事讲清「pop 里的 tail 推进是一次定向 helping——防止自己 defer_destroy 的旧哨兵被 tail 引用」。本步为源码阅读 + 推理；可选地，跑一下 MPMC 测试（见综合实践）观察无 crash。

#### 4.3.5 小练习与答案

**练习 1**：`pop_internal` 返回的值是从哪个节点读出来的？`defer_destroy` 的又是哪个节点？两者为什么不同？

**答案**：值从 `n = head.next`（新哨兵，原队首）的 `data` 槽 `assume_init_read` 读出；`defer_destroy` 的是 `head`（旧哨兵）。不同，因为旧哨兵的 `data` 槽本来就是空的（哨兵不持值），真正持有队首值的是它的后继。pop 的本质是「哨兵接力 + 销毁旧哨兵」，所以被回收的与被取值的是两个不同节点。

**练习 2**：`pop_if_internal` 为什么要求 `T: Sync`，而 `pop_internal` / `push` 只要求 `T: Send`？

**答案**：`pop_if_internal` 的 `condition: Fn(&T) -> bool` 会对节点的 `data` 求值，而并发下多个消费者可能**同时对同一个** `n` 求条件（都看到它、都判一下），相当于多线程共享 `&T`，故需 `T: Sync`。普通 `push`/`pop` 里每个 `T` 从入队到被某个赢家消费者 `assume_init_read` 读出，全程只被一个线程接触，只需 `T: Send`（能跨线程转移所有权）。这与 [src/sync/queue.rs:41-43](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/queue.rs#L41-L43) 的 `unsafe impl<T: Send> Sync` 注释一致。

**练习 3**：`pop_internal` 里读 `tail` 用 `Relaxed`（[L129](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/queue.rs#L129)），为什么不必用 `Acquire`？

**答案**：因为这里只是把 tail 的瞬时值拿来和 `head` 比一下、决定要不要做一次「尽力推进」的 CAS。即使读到的是稍旧的 tail 值，最坏后果只是「本该推进却没推进」或「CAS 失败」，都不影响正确性（后续 push/pop 还会再来）。`Relaxed` 足够，无需 `Acquire` 的同步保证。

### 4.4 作为 Global 垃圾队列：try_pop_if 与 is_expired

#### 4.4.1 概念说明

讲到这里，你可能会问：crossbeam 内部用这个队列装什么？答案是**全局垃圾袋**。回顾 u4-l16：每个线程把延迟闭包攒进本地 `Bag`，满了就 `seal(epoch)`（盖当前全局 epoch 戳）成 `SealedBag`，塞进全局队列；`collect` 再从队列里把「已过宽限期」的袋取出 drop，执行里面的闭包。

这个「塞进 / 取出」用的就是本讲的 `Queue<SealedBag>`：

- `Global::push_bag` → `queue.push(sealed_bag, guard)`：生产者（任意线程的 `defer` 溢出时）入队。
- `Global::collect` → `queue.try_pop_if(|sb| sb.is_expired(global_epoch), guard)`：消费者（任意线程的周期性 / 手动 collect）**只弹出**已过宽限期的袋。

`try_pop_if` 的「条件弹出」在这里大显身手：队列是 FIFO，但回收**不能**严格 FIFO——一个袋能否回收取决于「它的 epoch 戳是否已过宽限期」（`global_epoch.wrapping_sub(sealed) >= 2`），与入队先后无严格关系（虽然实践中 epoch 单调推进，靠前的袋通常先过期）。`try_pop_if` 让 collect 从队首开始找第一个未过期的袋，一旦遇到就停（`try_pop_if` 只从 head 弹）。

这是一个绝佳的「无锁数据结构 + EBR」自举例子：**队列本身用 EBR 回收自己 pop 下来的旧哨兵节点（`defer_destroy`），而队列里装的内容又是 EBR 待回收的垃圾袋。** 两层延迟回收嵌套。

#### 4.4.2 核心流程

```
线程 A 的本地 Bag 满：
   push_bag(bag, guard):
       bag = mem::replace(bag, Bag::new())     # 取出满袋，换空袋
       fence(SeqCst)                            # 保证袋内写入对回收线程可见
       epoch = global.epoch.load(Relaxed)       # 盖戳
       queue.push(bag.seal(epoch), guard)       # ← 本讲 push

任意线程的 collect：
   global_epoch = try_advance(guard)            # 尝试推进 epoch
   for _ in 0..8:
       queue.try_pop_if(|sb| sb.is_expired(global_epoch), guard)
           None    => break                     # 队首袋未过期（后面的也不会更老），停
           Some(sb)=> drop(sb)                  # 弹出并 drop → 执行袋内所有延迟闭包
```

注意 `try_pop_if` 的「队首即最新」语义与 `is_expired` 的单调性配合：因为 epoch 大致单调递增、袋按入队顺序排，**队首袋是最老的**。若队首都未过期，后面的袋更不会过期，所以 `try_pop_if` 返回 `None` 时 `collect` 直接 `break`（[src/internal.rs:222](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L222)）。这是 `try_pop_if` 相比「逐个 pop 再判断」的效率优势：未过期的袋不会被无谓弹出再塞回（队列也没有塞回操作）。

#### 4.4.3 源码精读

**`Global.queue` 字段**：

[src/internal.rs:165-174](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L165-L174)：`Global` 三字段之一 `queue: Queue<SealedBag>`，注释「全局的延迟函数袋队列」。

**`SealedBag::is_expired`——宽限期判据**（u4-l16 已讲，这里复用）：

[src/internal.rs:155-162](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L155-L162)：`global_epoch.wrapping_sub(self.epoch) >= 2`。注释点明依据：「一个 pinned 的参与者最多见证一次 epoch 推进，因此与当前 epoch 相差不足 1 的袋还不能销毁」——即必须差满 2（宽限期）。

**`push_bag`——入队**：

[src/internal.rs:191-198](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L191-L198)：`mem::replace` 取出满袋换空袋 → `SeqCst fence`（关键：保证「产生袋内垃圾的那些写入」在盖戳前对回收线程全局可见）→ `Relaxed` 读全局 epoch 盖戳 → `self.queue.push(bag.seal(epoch), guard)`。这一步调用的就是 4.2 的 `push`。

**`collect`——条件弹出并销毁**：

[src/internal.rs:207-226](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L207-L226)：先 `try_advance` 拿当前 `global_epoch`，然后循环至多 `COLLECT_STEPS = 8` 次（[L178](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L178)）调 `queue.try_pop_if(|sb| sb.is_expired(global_epoch), guard)`：返回 `None` 就 `break`（队首袋未过期），返回 `Some(sb)` 就 `drop(sb)`——`drop` 触发 `Bag::drop`（[src/internal.rs:125-134](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L125-L134)）执行袋内全部延迟闭包。`#[cold]` 标注把 `collect` 放到冷路径，保护 `pin` 热路径。这一步调用的就是 4.3 的 `try_pop_if`。

#### 4.4.4 代码实践（源码阅读型：跟踪一条垃圾袋的生命）

**实践目标**：把一条 `SealedBag` 从「入队」到「被 drop」的完整生命链路串起来，验证「两层延迟回收」。

**操作步骤**：

1. 入队侧：从 [src/internal.rs:191-198](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L191-L198) 的 `push_bag` → [src/sync/queue.rs:100-116](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/queue.rs#L100-L116) 的 `push`，确认袋被包成 `SealedBag` 节点挂到链表尾。
2. 出队侧：从 [src/internal.rs:217-225](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L217-L225) 的 `try_pop_if(is_expired)` → [src/sync/queue.rs:147-175](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/queue.rs#L147-L175) 的 `pop_if_internal`，确认只有 `is_expired` 为真（`wrapping_sub >= 2`）的袋才会被 CAS 弹出。
3. 销毁侧：弹出的 `SealedBag` 被 `drop`（[src/internal.rs:223](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L223)）→ `Bag::drop`（[src/internal.rs:125-134](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L125-L134)）逐个执行延迟闭包。
4. 第二层回收：`pop_if_internal` 弹出袋的同时，对队列自己的旧哨兵节点调了 `defer_destroy`（[src/sync/queue.rs:168](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/queue.rs#L168)）——这个旧哨兵的回收又要再走一遍 EBR 宽限期。

**预期结果**：你能画出「袋的闭包」与「队列旧哨兵节点」两条独立的延迟回收路径，理解为何说这是「EBR 自举」。本步为源码阅读，无需运行。

#### 4.4.5 小练习与答案

**练习 1**：`collect` 用 `try_pop_if` 而不是「`try_pop` 后自己判断再塞回」，有什么好处？

**答案**：队列没有「塞回」原语，pop 出来就只能消费或丢弃。若用普通 `try_pop`，遇到未过期的袋要么强行 drop（错误！袋内闭包尚未安全可执行）、要么无法放回（丢失袋）。`try_pop_if` 在 CAS 前先用条件函数判定，**未满足条件的袋根本不被弹出**，留在队首；一旦队首袋未过期，`try_pop_if` 返回 `None`，`collect` 据此 `break`——既安全又高效。

**练习 2**：`collect` 每次最多 drop 8 个袋（`COLLECT_STEPS`），为什么要限量？

**答案**：这是「摊销式增量回收」。一次 `collect`（由 `pin` 每 128 次或用户 `flush` 触发）只回收一小批，避免在热路径上做大量析构（袋内闭包可能很重、甚至产生新垃圾）。剩余袋等下一次 `collect` 再处理。回收进度由 epoch 推进驱动，与「一次清空」无关。详见 u5-l19。

## 5. 综合实践

**任务**：把本讲两个核心练习（helping 时序、head==tail 推进 tail）与真实 MPMC 测试串起来验证你的理解。

**操作步骤**：

1. 阅读多生产者多消费者压测 [src/sync/queue.rs:399-443](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/queue.rs#L399-L443) 的 `push_try_pop_many_mpmc`：2 组生产者分别推 `Left`/`Right`、2 组消费者各弹 `CONC_COUNT` 次（miri 下 1000，否则一百万，见 [L271](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/queue.rs#L271)），最后对消费者收到的值排序后断言「无丢失、无重复」。
2. 在脑中把 4.2.4 的 helping 时序叠加到这个测试上：当两个生产者线程并发 push 时，必有一方频繁走 helping 分支（[L79-82](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/queue.rs#L79-L82)）；当生产者与消费者并发时，pop 的 head==tail 推进（[L131-135](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/queue.rs#L131-L135)）会被反复触发。测试能通过，正说明这两处「看似多余的 CAS」不可或缺。
3. （可选，本地运行）在 `crossbeam-epoch` 目录下跑：

   ```bash
   cargo test --lib sync::queue::test::push_try_pop_many_mpmc
   ```

   再用 miri 跑小规模版本（`CONC_COUNT=1000`）以验证无数据竞争：

   ```bash
   MIRIFLAGS="-Zmiri-many-seeds" cargo +nightly miri test --lib sync::queue::test::push_try_pop_many_mpmc
   ```

4. （可选，加深理解）在 `pop_internal` 的 `if head == tail` 分支前后各加一句 `eprintln!("pop advancing tail")`（**示例修改，仅供本地观察，勿提交**），重跑测试，观察该分支在 MPMC 下被触发的频率。

**需要观察的现象 / 预期结果**：测试通过；若加了日志，能看到 helping 与 tail 推进在高并发下高频发生。若本环境不便运行，标注「待本地验证」——理解时序本身才是本实践的重点。

**反思题**：若把 [src/sync/queue.rs:131-135](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/queue.rs#L131-L135) 的 tail 推进删掉，MPMC 测试还会过吗？

> 提示：单线程下大概率能过（tail 滞后窗口小、不易撞上），但在 miri/ThreadSanitizer 的高并发下会暴露 use-after-free。这正是该 CAS「防悬挂」的价值——它修的是一个小概率但致命的竞态。

## 6. 本讲小结

- **哨兵节点**让队列永远至少有一个节点：`head` 永远指向哨兵（`data` 空），`tail` 指向真实尾或其前一个节点；由此 push/pop 都不必特判空队列。
- **入队是两次 CAS**：先 CAS 把新节点挂到「tail 节点的 next」（线性化点），再 CAS 推进 tail。两次都允许失败——前者失败重试，后者失败说明别人已推进。
- **helping 保证 lock-free**：当 `tail.next` 非空（tail 滞后），后来的生产者主动 CAS 推进 tail，使某个生产者卡住也不会阻塞全队。
- **pop 是「哨兵接力」**：head 前移一格，旧哨兵 `defer_destroy` 延迟回收，值从新哨兵（原队首）的 `data` 读出；任何 pop/pop_if 都靠 head 的 CAS 决出唯一赢家。
- **pop 在 `head == tail` 时推进 tail** 是定向 helping：防止自己即将 `defer_destroy` 的旧哨兵被 `tail` 引用，避免后续 push 解引用已回收节点。
- **`try_pop_if` 服务于 `Global::collect`**：用 `is_expired`（`wrapping_sub >= 2` 宽限期）当条件，只弹出可安全销毁的垃圾袋，队首未过期即停；这是 EBR 「条件回收」的真实调用点，也是「无锁队列 + EBR」的自举设计。

## 7. 下一步学习建议

本讲是 u6 专家层基础设施的收尾之一。建议接下来：

1. **u6-l22 no_std / loom / 可移植性抽象层**：`src/sync/queue.rs` 里 `Atomic`、`Guard`、`unprotected` 都跑在 `lib.rs` 顶部的 `primitive` 抽象层之上；那篇讲义会讲清这套抽象如何在 loom 与真实环境间切换、`UnsafeCell` wrapper 与 `const_fn!` 宏的作用。
2. **u6-l23 测试、基准与示例**：本讲引用的 `push_try_pop_many_mpmc` 属于常规单元测试；那篇会进一步讲 `tests/loom.rs` 如何用 loom 对 EBR + 无锁数据结构做有界状态模型检验，以及 `benches/` 如何测 `pin`/`defer`/`flush`。
3. **回头巩固回收链路**：若你对 `collect`→`try_pop_if`→`is_expired` 这条链还想看全景，建议重读 u5-l19（try_advance 与 collect）——本讲的队列是那条链的「存储后端」，二者互为表里。
