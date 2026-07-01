# array flavor：有界环形缓冲

## 1. 本讲目标

本讲深入 `crossbeam-channel` 的**有界通道**（`bounded(cap)`，`cap > 0`）底层实现——`flavors/array.rs`。学完后你应当能够：

- 说清「stamp（戳记）+ lap（圈数）+ mark（断开位）」三者如何打包进一个 `usize`，以及它们如何**在没有锁、没有计数器**的前提下同时表达「这个槽可写吗 / 可读吗 / 通道断开了吗」。
- 画出一次成功 `send` + `recv` 的槽位 stamp 变化时序，理解 **start（占位）→ commit（提交）两阶段协议**，以及 `Token` 为何要把这两个阶段解耦。
- 解释当通道满 / 空时，`send` / `recv` 如何从「自旋退避」升级到「注册到 `SyncWaker` 并真正阻塞」，又被对端如何唤醒。

本讲只读两个文件：`flavors/array.rs`（环形缓冲本体）与 `waker.rs`（阻塞线程队列）。引用计数、flavor 派发外壳、错误类型已在 u3-l2 / u3-l3 讲过，本讲直接承接。

## 2. 前置知识

- **环形缓冲（ring buffer）**：一块固定大小的数组，用 `head`（读位置）和 `tail`（写位置）两个游标循环复用槽位，避免频繁分配。单生产者单消费者（SPSC）时只需两个游标即可；但**多生产者多消费者（MPMC）**时，多个线程会同时争抢推进 `head`/`tail`，必须有并发安全手段。
- **CAS（compare-and-swap）**：`compare_exchange` 是原子「比较相等则写入」指令，是构建无锁算法的基石。失败说明有人抢先一步，需要重读重试。
- **stamp / lap 防 ABA**：在 MPMC 环形队列里，光比较槽位下标不足以判断状态——同一个下标在不同「圈」代表不同消息。给每次循环计数（lap）并把它编进「戳记」里，就能区分「这一圈的槽」与「上一圈遗留的槽」。这正是 Vyukov 算法的核心技巧。
- **Backoff 三段式退避**：CAS 失败时先 `spin`（纯自旋），再 `snooze`（让出时间片），`is_completed()` 后转入阻塞（见 u2-l1）。
- **两阶段提交（reservation + commit）**：`start_send` 先用 CAS **预定**一个槽位并把信息记进 `Token`，`write` 再把消息**写入**该槽。这种拆分让 select 机制可以「先选中某条操作、再统一提交」（见 u3-l3 的 `Token` 设计）。

> 阅读提示：本讲出现的 `usize` 经常不是普通数字，而是「打包字」。我们会反复用 `{ lap, mark, index }` 这种三元组记法来读它，请先习惯这个视角。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [crossbeam-channel/src/flavors/array.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs) | 有界 MPMC 环形缓冲本体：`Slot` / `Channel` / `ArrayToken`，以及 `start_send`/`write`/`start_recv`/`read` 与阻塞版 `send`/`recv`。 |
| [crossbeam-channel/src/waker.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/waker.rs) | 阻塞线程队列：`Waker`（selectors / observers 两条 `Vec`）与 `SyncWaker`（`Mutex<Waker>` + `is_empty` 快速路径）。array 用它管理「满了等的发送者」与「空了等的接收者」。 |
| [crossbeam-channel/src/select.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs) | `Token` 联合体与 `SelectHandle` trait 定义（u3-l3 已讲，本讲只引用其 `array` 字段）。 |
| [crossbeam-channel/src/channel.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs) | 公共门面：`bounded(cap)` 构造函数与 `SenderFlavor`/`ReceiverFlavor` 的 `match` 派发（u3-l3 已讲）。 |

## 4. 核心概念与源码讲解

### 4.1 槽位布局：stamp 打包与 ArrayToken

#### 4.1.1 概念说明

有界通道的本质是一块预分配的 `cap` 个槽位（slot）数组，被 `head`（读端）和 `tail`（写端）循环复用。难点在于：**在没有互斥锁的前提下，如何让多个发送者 / 接收者安全地判断「这个槽现在能不能写、能不能读」？**

Vyukov 算法的巧妙之处是：**不给通道维护单独的元素计数，而是让每个槽自己「盖章」记录状态**。每个槽持有一个原子戳记 `stamp`，它和 `head`/`tail` 一样，都是同一个「打包字」格式：

\[
\text{stamp} = \{\,\text{lap（圈数）},\ \text{mark（断开位）},\ \text{index（槽下标）}\,\}
\]

三位打包进一个 `usize`：低位放 `index`，中间单独一位放 `mark`，高位放 `lap`。判定规则极其简洁：

- 一个槽**可写** ⟺ `slot.stamp == tail`（戳记对上了当前写游标）；
- 一个槽**可读** ⟺ `slot.stamp == head + 1`（戳记领先读游标一格）。

写完之后把戳记改成 `tail + 1`（于是它变成「可读」）；读完之后把戳记改成 `head + one_lap`（跨一整圈，于是它下一圈才重新「可写」）。`lap` 的存在避免了「同一 index、不同圈」造成的 ABA 误判——这就是 u4-l1 即将讲到的同类防 ABA 思想在通道里的运用。

#### 4.1.2 核心流程

设容量为 `cap`，构造时算出两个常量：

\[
\text{mark\_bit} = (\text{cap}+1)\ \text{.next\_power\_of\_two}(), \qquad \text{one\_lap} = \text{mark\_bit} \times 2
\]

- `mark_bit` 是「严格大于 `cap` 的最小 2 的幂」，既用作断开标志位，其低若干位又刚好能装下所有合法 `index`（`0..cap`）。
- `one_lap = mark_bit * 2` 在 `index` 位与 `mark` 位之上再空出一格，保证跨圈时 `lap` 部分恰好多 1。

从一个打包字 `v` 里拆分三段：

```
index = v & (mark_bit - 1)      // 低位的槽下标
mark  = v & mark_bit            // 单个断开位
lap   = v & !(one_lap - 1)      // 高位的圈数
```

初始化时：`head = tail = 0`，第 `i` 个槽的 `stamp = i`（即 `{lap:0, mark:0, index:i}`），表示「第 0 圈、可写」。

`Token` 在两阶段协议里承担「随身携带所选槽位」的职责。array flavor 对应的字段是 `ArrayToken`：

```
ArrayToken { slot: *const u8, stamp: usize }
//  slot : start 阶段记下预定到的槽地址，commit 阶段用它找回槽
//  stamp: start 阶段算出「提交时应写入的戳记」，commit 阶段照此 store
```

#### 4.1.3 源码精读

每个槽由一个原子戳记 + 一个存放消息的 `UnsafeCell<MaybeUninit<T>>` 组成（[array.rs:29-37](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L29-L37)）。`MaybeUninit` 是因为同一槽位在「可写」状态下并不持有合法 `T`，不能让编译器假定它总是初始化好的。

`Channel` 持有全部状态（[array.rs:59-93](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L59-L93)）：`head` / `tail` 都用 `CachePadded<AtomicUsize>` 包装以消除伪共享（见 u2-l2）；`senders` / `receivers` 是两个 `SyncWaker`，分别登记「满了等的发送者」和「空了等的接收者」。

构造函数算常量并给每个槽盖初始戳记（[array.rs:97-130](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L97-L130)）：

```rust
let mark_bit = (cap + 1).next_power_of_two();
let one_lap = mark_bit * 2;
// ...
let buffer: Box<[Slot<T>]> = (0..cap).map(|i| Slot {
    stamp: AtomicUsize::new(i),                 // {lap:0, mark:0, index:i}
    msg: UnsafeCell::new(MaybeUninit::uninit()),
}).collect();
```

`ArrayToken` 仅两个裸字段（[array.rs:39-57](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L39-L57)），它是 `Token` 联合体里的一个分支（[select.rs:23-32](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L23-L32)）。`Token` 把六种 flavor 各自需要的临时数据并列放在一起，每次操作只填命中的那一个字段——这是 u3-l3 讲过的「每 flavor 一字段」胖结构体。

#### 4.1.4 代码实践

> **实践目标**：手算 `cap = 2` 与 `cap = 5` 时的打包常量，建立对 `{lap, mark, index}` 布局的直觉。

1. 阅读上面引用的 [array.rs:100-119](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L100-L119)。
2. 填表（待本地验证：用一个小 Rust 程序打印 `(cap+1).next_power_of_two()` 与 `mark_bit*2` 对照）：

   | `cap` | `mark_bit` | `one_lap` | index 占哪些位 | mark 在第几位 |
   | --- | --- | --- | --- | --- |
   | 2 | 4 | 8 | bit 0（仅 0,1 合法） | bit 2 |
   | 5 | 8 | 16 | bit 0–2（0..5） | bit 3 |

3. **观察**：`mark_bit` 总是严格大于 `cap` 的 2 的幂，所以 `index` 的低若干位永远不会与 `mark` 位重叠。
4. **预期结果**：你能用 `{lap, mark, index}` 三元组口述「`stamp = 9` 在 `cap=2` 时代表什么」——答：`9 = 0b1001` → `lap=1, mark=0, index=1`，即「第 1 圈的下标 1 槽」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `mark_bit` 取「严格大于 `cap` 的 2 的幂」，而不能直接取 `cap` 本身？

> **答案**：`index = v & (mark_bit - 1)` 需要一个连续的低位掩码来装下所有合法下标 `0..cap`。若 `mark_bit` 不是 2 的幂，`mark_bit - 1` 就不是「低 N 位全 1」的掩码，无法干净地切出 `index`；若 `mark_bit <= cap`，则某些合法 `index` 会爬到 `mark` 位之上，破坏三段布局。取「严格大于」既保证是 2 的幂，又保证 `cap < mark_bit`（所有 index 落在 mark 之下）。

**练习 2**：`head` 的注释说「head 里的 mark 位永远是 0」，但 `tail` 的 mark 位却可能被置 1，为什么断开信息只挂在 `tail` 上？

> **答案**：断开（disconnect）是由「最后一个 sender」或「最后一个 receiver」离开时触发的（见 u3-l2 的 `disconnect_senders` / `disconnect_receivers`），它需要对所有正在 `start_send` 的发送者可见。发送者循环里每次都重新 `load(tail)`，因此把断开位挂在 `tail` 上能让发送路径第一时间发现（[array.rs:149](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L149)）；接收路径则在判空后顺带检查 `tail` 的 mark 位（[array.rs:284](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L284)）。head 不承载断开信息，保持纯粹。

---

### 4.2 两阶段协议：start 占位 + write/read 提交

#### 4.2.1 概念说明

发送 / 接收被刻意拆成**两个阶段**：

- **start 阶段**（`start_send` / `start_recv`）：在一个 `Backoff` 循环里用 CAS 把 `tail` / `head` 推进一步，从而**独家预定**某个槽位；把槽地址和「应写入的戳记」记进 `token.array`，然后返回。
- **commit 阶段**（`write` / `read`）：根据 `token.array.slot` 找回那个槽，真正写入 / 读出消息，并把戳记 `store` 成 `token.array.stamp`。

为什么非要拆开？因为 select 机制需要「先确认这条操作能成功（选中），再去执行可能有副作用的提交」。拆分之后，`start_*` 可以作为 `SelectHandle::try_select` 的实现去参与多路选择；只有被选中的那条操作才会调用 `write` / `read` 完成提交（见 u3-l3「Token 把选中与完成解耦」）。普通（非 select）的 `send`/`recv` 只是把这两步紧挨着调用而已。

#### 4.2.2 核心流程

**发送 `start_send`**（无锁抢占一个可写槽）：

```
loop:
    tail = load(tail, Relaxed)
    if tail 标了 mark（已断开）: token 置空, return true（让 write 报 Disconnected）
    index, lap = 拆分 tail
    stamp = 槽[index].stamp.load(Acquire)
    if tail == stamp:                      # 该槽可写！
        new_tail = 索引+1 未越界 ? tail+1 : lap+one_lap   # 同圈+1 或 跨圈回 0
        if CAS(tail -> new_tail, SeqCst) 成功:
            token.slot = &槽[index]
            token.stamp = tail + 1         # 写完后戳记 = tail+1（变成可读）
            return true
        else: tail = 失败返回的现值; spin 重试
    elif stamp + one_lap == tail + 1:      # 戳记落后一整圈 → 可能已满
        fence(SeqCst); head = load(head)
        if head + one_lap == tail: return false   # 确认满
        spin; 重读 tail
    else:                                  # 戳记还在更新中
        snooze; 重读 tail
```

**提交 `write`**（[array.rs:215-230](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L215-L230)）：把消息写进 `token.slot` 指向的槽，`stamp.store(token.stamp, Release)` 把它标记为「可读」（`Release` 保证消息内容在戳记变更前对读者可见），再 `receivers.notify()` 叫醒一个等消息的接收者。

**接收 `start_recv`**（无锁抢占一个可读槽，[array.rs:233-303](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L233-L303)）：对称地，当 `head + 1 == stamp` 时该槽可读，CAS 推进 `head`，记下 `token.stamp = head + one_lap`（读完后戳记跨一圈，变回「下一圈可写」）。若 `stamp == head` 说明还没人写进来，再查 `tail`：`tail == head` 即为空，若同时带 mark 位则返回断开。

**提交 `read`**（[array.rs:306-321](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L306-L321)）：读出消息，`stamp.store(token.stamp, Release)`，再 `senders.notify()` 叫醒一个因「满」而阻塞的发送者。

一次成功 `send` + `recv` 的 stamp 时序（`cap = 2`，`mark_bit = 4`，`one_lap = 8`，初值 `slot[0].stamp = 0`、`slot[1].stamp = 1`、`head = tail = 0`）：

| 时刻 | 动作 | `tail` | `head` | `slot[0].stamp` | `token` 内容 |
| --- | --- | --- | --- | --- | --- |
| t0 | 初值 | 0 | 0 | 0（可写） | — |
| t1 | `start_send` | 1 | 0 | 0 | slot→[0], stamp=0+1=**1** |
| t2 | `write` | 1 | 0 | **1**（可读，因 head+1=1） | — |
| t3 | `start_recv` | 1 | 1 | 1 | slot→[0], stamp=0+8=**8** |
| t4 | `read` | 1 | 1 | **8**（下一圈可写） | — |

注意 t4 之后 `slot[0].stamp = 8`。当 `tail` 绕完一圈走到 `8` 时，`tail == stamp` 重新成立，该槽再次可写——这就是 lap 防止 ABA 的体现：如果只比较 index（都是 0），我们会误以为「还是第 0 圈那个旧消息」。

#### 4.2.3 源码精读

`start_send` 的 CAS 抢占与 token 填充（[array.rs:143-212](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L143-L212)），关键几行：

```rust
let stamp = slot.stamp.load(Ordering::Acquire);
if tail == stamp {                                    // 可写判定
    let new_tail = if index + 1 < self.cap() { tail + 1 }
                   else { lap.wrapping_add(self.one_lap) };
    match self.tail.compare_exchange_weak(tail, new_tail, SeqCst, Relaxed) {
        Ok(_) => {
            token.array.slot = slot as *const Slot<T> as *const u8;
            token.array.stamp = tail + 1;             // 提交时写入的戳记
            return true;
        }
        Err(t) => { tail = t; backoff.spin(); }
    }
}
```

`write` 的提交（[array.rs:223-229](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L223-L229)）：

```rust
unsafe { slot.msg.get().write(MaybeUninit::new(msg)) }
slot.stamp.store(token.array.stamp, Ordering::Release);  // 戳记 = tail+1
self.receivers.notify();                                 // 叫醒等待的接收者
```

注意 `write` 是 `unsafe fn`：安全性建立在「`start_send` 已用 CAS 独家预定该槽」这一不变量之上，调用者（`channel.rs`）保证只在 `start_send` 返回 `true` 后、且未被其他线程复用 token 时调用。`channel.rs` 的派发把这一步接上（[channel.rs:1542](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L1542)）。`read` 的派发同理（[channel.rs:1553](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L1553)）。

「断开」走另一条路：`start_send` 发现 `tail` 带了 mark 位，就把 `token.slot` 置空（[array.rs:149-153](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L149-L153)）；`write` 见到空 slot 就 `return Err(msg)` 把消息原样退回（[array.rs:217-219](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L217-L219)）——这就是 u3-l1 讲过的「发送失败必带回原消息」。

#### 4.2.4 代码实践

> **实践目标**：用日志跟踪一个 `cap = 2` 的通道在「2 个发送者各发 5 条、1 个接收者消费」下的 stamp 状态机。

1. 在本地写一个临时二进制（依赖 `crossbeam-channel`），伪代码：
   ```rust
   // 示例代码：仅用于观察行为，不修改 crossbeam 源码
   let (s, r) = crossbeam_channel::bounded::<i32>(2);
   let s2 = s.clone();
   std::thread::spawn(move || for v in 1..=5 { s.send(v).unwrap(); });
   std::thread::spawn(move || for v in 6..=10 { s2.send(v).unwrap(); });
   let mut got = Vec::new();
   while got.len() < 10 { got.push(r.recv().unwrap()); }
   got.sort();
   assert_eq!(got, (1..=10).collect::<Vec<_>>());
   ```
2. 操作：先运行确认 10 条消息无丢失、无重复。**注意**：公共 API 不暴露 `stamp`，你无法直接打印它；要观察 stamp，需对照本讲 4.2.2 的时序表，在纸上为每条 `send`/`recv` 推演 `tail`/`head`/`slot[i].stamp` 的变化。
3. 现象：因容量只有 2，发送者会频繁撞上「满」而阻塞或退避，直到接收者消费腾出空位。
4. 预期结果：最终收到 1..=10 全部 10 个值；并能在纸上演算出 `slot[0]` 与 `slot[1]` 的 stamp 在 `0/1 ↔ 8/9 ↔ 16/17 …` 之间交替递增（每圈 `+one_lap=8`）。
5. 若想真正看到内部状态：阅读 `crossbeam-channel` 源码后，可 fork 一份在 `start_send`/`write`/`start_recv`/`read` 各加一行 `eprintln!`，再跑上述示例——**待本地验证**（本讲不修改官方源码）。

#### 4.2.5 小练习与答案

**练习 1**：`write` 里写消息用 `Release` 序，读戳记（`start_send`/`start_recv` 里 `slot.stamp.load`）用 `Acquire` 序。这一配对保证了什么？

> **答案**：保证「消息内容先于戳记变更对其他线程可见」。发送者 `Release` 写戳记后，任何在接收侧 `Acquire` 读到新戳记的线程，也一定能看到该槽里已被写入的完整消息（happens-before 关系）。若改用 `Relaxed`，接收者可能读到「戳记说可读、但消息字节还没刷出来」的撕裂状态。

**练习 2**：`start_send` 里 CAS 推进 `tail` 成功后，为什么还要 `backoff.spin()` 在失败分支？这里用 `spin` 而非 `snooze` 合理吗？

> **答案**：CAS 失败意味着「别的发送者刚刚抢占了同一位置并已前进」，这正是 u2-l1 里「别人已前进、低延迟重试」的场景，适合纯 `spin` 而不让出时间片。`snooze` 只用在「需要等别人更新戳记」的第三分支（[array.rs:206-209](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L206-L209)），那是「等对方干活」的条件等待，适合让出时间片。

**练习 3**：`start_recv` 判空时为何先 `fence(SeqCst)` 再 `load(tail)`？

> **答案**：`stamp == head` 这个读取与随后对 `tail` 的读取必须构成一个一致的快照。`fence(SeqCst)` 配合前面的 `Acquire` 读戳记，给「判断空 / 满」这个多变量决策建立强同步，避免因重排而把「恰好在变动的中间态」误判为空或满（见 [array.rs:277-296](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L277-L296) 与 `start_send` 中对称的 [array.rs:194-205](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L194-L205)）。

---

### 4.3 与 Waker 协作：阻塞 send / recv 与唤醒

#### 4.3.1 概念说明

`start_send` 返回 `false` 表示「通道满」，`start_recv` 返回 `false` 表示「通道空」。这时调用方有两种选择：

- `try_send` / `try_recv`：直接把 `false` 翻译成 `Full` / `Empty` 错误返回（[array.rs:324-331](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L324-L331)、[array.rs:387-395](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L387-L395)）。
- 阻塞版 `send` / `recv`：先自旋退避一会儿赌它变好，仍不行就**把自己登记到 `SyncWaker` 并真正阻塞**，等对端来唤醒。

阻塞的难点是经典的「丢失唤醒」：如果先登记、后检查状态，可能错过登记瞬间发生的事件；如果先检查、后登记，又可能在两者之间被唤醒而无人知晓。array 的解法是 u2-l5 Parker 用过的同一招——**先登记，再复查，复查发现已就绪就自我中止（abort）**，从而在登记与复查之间发生的事件不会丢。

#### 4.3.2 核心流程

阻塞版 `send` 的结构（[array.rs:334-384](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L334-L384)）：

```
loop:                                     # 被伪唤醒/超时会回到这里重试
    # ① 快速重试阶段：自旋+snooze 赌一把
    backoff = Backoff::new()
    loop:
        if start_send(token): return write(token, msg)   # 成功即走
        if backoff.is_completed(): break
        backoff.snooze()
    # ② 超时检查
    if 有 deadline 且已过: return Timeout(msg)
    # ③ 登记并阻塞（防丢失唤醒）
    Context::with(|cx| {
        oper = Operation::hook(token)
        senders.register(oper, cx)                  # 先登记
        if not is_full() or is_disconnected():      # 再复查
            cx.try_select(Aborted)                   # 已就绪 → 自我中止
        sel = cx.wait_until(deadline)                # 阻塞等待
        match sel:
            Aborted | Disconnected: senders.unregister(oper)   # 撤销登记后重试
            Operation(_): {}                        # 被对端选中，外层重跑 start_send
    })
```

`recv`（[array.rs:398-446](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L398-L446)）结构完全对称，只是把 `senders` 换成 `receivers`、`is_full` 换成 `is_empty`，并在 `Disconnected` 后仍要继续循环以抽干剩余消息（注释 [array.rs:439-441](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L439-L441)）。

唤醒由对端在 commit 时触发：`write` 调 `receivers.notify()`、`read` 调 `senders.notify()`（见 4.2.3）。`SyncWaker::notify` 会从登记队列里挑一个**别的线程**的操作，用 CAS 把它的 `Selected` 从 `Waiting` 改成 `Operation(oper)`，再 `unpark` 它（[waker.rs:225-237](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/waker.rs#L225-L237) → [waker.rs:84-111](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/waker.rs#L84-L111)）。

#### 4.3.3 源码精读

`SyncWaker` 是「`Mutex<Waker>` + 一个 `is_empty` 快速路径位」的组合（[waker.rs:182-188](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/waker.rs#L182-L188)）。`notify` 先读 `is_empty`，若为空就直接返回，**避免无等待者时也去抢锁**（[waker.rs:225-228](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/waker.rs#L225-L228)）：

```rust
pub(crate) fn notify(&self) {
    if !self.is_empty.load(Ordering::SeqCst) {        // 快速路径：无等待者则不锁
        let mut inner = self.inner.lock();
        if !self.is_empty.load(Ordering::SeqCst) {    // 双检，防拿到锁前状态变化
            inner.try_select();
            inner.notify();
            self.is_empty.store(/* ... */, SeqCst);
        }
    }
}
```

`Waker::try_select` 遍历 selectors，跳过属于当前线程的项，对每项尝试 `cx.try_select(Operation(oper))`——只有状态仍是 `Waiting` 才能 CAS 成功（[waker.rs:84-111](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/waker.rs#L84-L111)）。成功后存包、`unpark` 唤醒，并把该项移出队列保持干净。

`disconnect` 则把所有等待者标记为 `Selected::Disconnected` 并唤醒（[waker.rs:155-168](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/waker.rs#L155-L168)）。通道断开由 `disconnect_senders` / `disconnect_receivers` 触发，它们用 `tail.fetch_or(mark_bit)` 点亮断开位（[array.rs:487-496](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L487-L496)、[array.rs:506-517](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L506-L517)）。注意最后一个 receiver 离开时还会调 `discard_all_messages` 把残留消息逐个 `assume_init_drop` 掉（[array.rs:531-575](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L531-L575)），这是 u3-l1「最后 receiver 离开即清理」在有界通道里的落实。

最后看 `Sender`/`Receiver` 如何把上面一切接到 `SelectHandle` trait（[array.rs:613-683](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L613-L683)）。例如 `Sender::register` 就是「登记到 senders 后查 is_ready」（[array.rs:658-661](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L658-L661)），与阻塞 `send` 里的「登记 + 复查」如出一辙——`SelectHandle` 不过是把阻塞版的那套登记/唤醒协议抽象成了 u3-l3 描述的统一接口。

#### 4.3.4 代码实践

> **实践目标**：制造一次真实的阻塞 `recv`，画出「接收者 park → 发送者唤醒接收者」的完整调用链。

1. 写一个示例（**示例代码**）：
   ```rust
   use std::thread;
   use std::time::Duration;
   use crossbeam_channel::bounded;

   let (s, r) = bounded(1);
   thread::spawn(move || {
       thread::sleep(Duration::from_secs(1));
       s.send("hello").unwrap();          // 此刻接收者正阻塞在 recv
   });
   assert_eq!(r.recv(), Ok("hello"));     // 主线程会阻塞约 1 秒
   ```
2. 操作：对照本讲源码，在纸上逐步标注调用链。预期路径如下：
   - 接收者：`recv` → `start_recv` 返回 `false`（空）→ 退避后 `receivers.register` → `is_empty()` 仍真 → `cx.wait_until` 阻塞（park）。
   - 1 秒后发送者：`send` → `start_send` 返回 `true` → `write` 写入槽并 `receivers.notify()` → `SyncWaker::notify` → `Waker::try_select` 选中接收者的 `oper` 并 `cx.unpark()`。
   - 接收者被唤醒：`wait_until` 返回 `Operation(_)` → 外层循环重跑 `start_recv` → 这次 `head+1 == stamp` 成立 → `read` 取走消息。
3. 现象：主线程约 1 秒后打印 `hello`；若把 `bounded(1)` 换成 `unbounded()`，行为不变但底层走的是 list flavor（下一讲 u3-l5）。
4. 预期结果：能口述「为什么发送者 `write` 之后必须 `receivers.notify()`」——若不通知，接收者会一直 park 到超时（无超时则永久阻塞），这正是 u2-l1 强调的「谁置位谁唤醒」配对原则。
5. 若想验证「先登记再复查」防丢失唤醒：理论推演——若发送者在接收者「register 之后、复查 is_empty 之前」恰好 `write`，则复查时 `is_empty()` 返回 `false`，接收者 `try_select(Aborted)` 自我中止并重跑 `start_recv` 立即拿到消息；若发送者在「复查之后」才 `write`，则接收者已进入 `wait_until`，发送者的 `notify` 会 CAS 命中并将其唤醒。两种时序都不丢——**待本地验证**（可用 loom 做状态空间枚举，见 u7-l3）。

#### 4.3.5 小练习与答案

**练习 1**：`SyncWaker::notify` 为什么要「双检 `is_empty`」（拿到锁前后各读一次）？

> **答案**：第一次读 `is_empty` 是**乐观快速路径**，若没有等待者就直接返回、避免抢锁；但「读 is_empty=false」与「拿到锁」之间，可能恰好所有等待者都撤销了登记（比如它们超时/被对端选中而 `unregister`）。第二次在锁内再读，拿到的是与持锁状态一致的快照，避免「以为有人、其实已空」的无效 `try_select`。这是「快速路径 + 锁内复查」的经典模式。

**练习 2**：阻塞 `send` 的最外层为什么是 `loop`，而被唤醒后不直接 `write`，而是重跑 `start_send`？

> **答案**：被对端 `notify` 唤醒只表示「**可能**有空位了」（比如某个接收者消费了一条），但具体是哪个槽、戳记是多少，必须重新由 `start_send` 用 CAS 去抢占才能确定；而且唤醒也可能是伪唤醒或超时返回。所以唤醒后回到循环顶端重跑 `start_send`：成功则 `write`，失败则继续退避/登记。把「选中（哪条操作就绪）」与「提交（具体占哪个槽）」分离，正是两阶段协议在阻塞路径上的延续。

**练习 3**：`disconnect_receivers` 上标注了 `# Safety`，要求「只能在最后一个 receiver drop 时调用、且此前其它 receiver 的析构已被 acquire 或更强的序观察到」。为什么需要这个前提？

> **答案**：`discard_all_messages` 会逐槽 `assume_init_drop` 消息，它依赖「此刻没有别的 receiver 还在并发读槽」这一事实。该事实只能由「所有 receiver 的 `release`（drop） happens-before 这最后一个 receiver 的 `disconnect_receivers`」来保证（见 [array.rs:500-517](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L500-L517)）。这个安全性由更上层的 `counter.rs` 引用计数协议（u3-l2）在 `release` 时用 `AcqRel` 兜底，所以 flavor 内部用 `unsafe fn` 把责任上交。

## 5. 综合实践

把本讲三个模块串起来：**「画时序图 + 跑并发示例 + 解释防丢失唤醒」三合一**。

1. **画图**：取 `cap = 3`（算出 `mark_bit = 4`？请先纠正：`cap=3` 时 `(3+1).next_power_of_two() = 4`，`one_lap = 8`）。在纸上画出连续 3 次 `send`（塞满）+ 1 次「第 4 次 `send` 阻塞」+ 1 次 `recv`（腾位并唤醒阻塞的发送者）的全过程，标注每一步的 `tail`、`head`、三个槽的 `stamp`、以及 `SyncWaker` 里登记/唤醒的发生点。
   - 提示：第 3 次 `send` 会让某个槽的 stamp 落到「`stamp + one_lap == tail + 1`」分支并被判满；阻塞的发送者走 4.3.2 的 ①→③ 流程；`recv` 的 `read` 调 `senders.notify()` 把它救醒。
2. **跑示例**：用 4.2.4 的「2 发送者各发 5 条 + 1 接收者」程序，把 `cap` 从 2 改成 1、4、8，观察吞吐与阻塞频率的变化（**待本地验证**：可加 `Instant::now()` 计时）。
3. **解释**：用一句话向同事说明「为什么 array 通道既不需要互斥锁、也不需要单独的元素计数，就能正确实现 MPMC 有界传输」——参考答案：*每个槽的 stamp 同时编码了下标、圈数与可读/可写状态，发送/接收只靠对 head/tail 的 CAS 与对 stamp 的 Acquire/Release 配对，就在无锁前提下完成了「占位—提交—阻塞—唤醒」全流程。*

## 6. 本讲小结

- array flavor 是基于 **Vyukov 有界 MPMC 队列**的环形缓冲，用 `head`/`tail` 两个 `CachePadded` 原子游标 + 每槽一个 `stamp` 协调并发。
- stamp 把 `{lap, mark, index}` 打包进一个 `usize`：`mark_bit = (cap+1).next_power_of_two()`、`one_lap = mark_bit*2`；`lap` 防止跨圈 ABA，`mark` 表达断开。
- 状态判定只需两个等式：**可写 ⟺ `stamp == tail`**（写后变 `tail+1`），**可读 ⟺ `stamp == head+1`**（读后变 `head+one_lap`）。
- 发送/接收是 **start（CAS 占位，填 `ArrayToken`）→ commit（`write`/`read` 写读消息 + 改戳记）** 两阶段协议，让 select 能把「选中」与「提交」解耦。
- 满了/空了时，阻塞版 `send`/`recv` 走「自旋退避 → 登记 `SyncWaker` → 复查就绪 → `wait_until` 阻塞」，对端在 commit 时 `notify` 唤醒，靠「先登记再复查」杜绝丢失唤醒。
- 斷開由 `tail.fetch_or(mark_bit)` 点亮，触发 `Waker::disconnect` 唤醒所有等待者；最后一个 receiver 离开时 `discard_all_messages` 清理残留消息。

## 7. 下一步学习建议

- **下一讲 u3-l5（list flavor）**：对比无界链表实现，观察它如何用「每块 31 条消息的分块链表」摆脱容量上限、以及为何不需要 array 这套「满」判定。
- **u3-l6（zero flavor）**：看 `bounded(0)` 如何把「没有缓冲」做到极致——send 与 recv 必须配对，是 array 退掉缓冲后的极端形态。
- **u3-l7（Context 与 Waker）**：本讲多次出现 `Context::with`、`cx.try_select`、`cx.wait_until`，下一讲会拆开线程局部阻塞上下文的实现，把 4.3 的唤醒链补全。
- **延伸阅读**：Vyukov 原文（源码注释 [array.rs:5-9](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L5-L9) 给出的两个链接）讲了原始的 bounded MPMC queue，对照阅读能加深对 stamp/lap 设计动机的理解。同样的 lap 防 ABA 思想将在 u4-l1（`ArrayQueue`）再现。
