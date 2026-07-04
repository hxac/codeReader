# Injector::push：tail 推进与跨 block 安装

## 1. 本讲目标

在上一讲（u3-l1）我们建立了 Injector 的「骨架」：它是一个由 `Block` 单向链表组成、用 `head`/`tail` 两个 `Position` 索引的全局 MPMC FIFO 队列，每个 `Block` 含 `BLOCK_CAP=63` 个 `Slot`。本讲把 **`Injector::push` 的并发算法**从黑盒里打开，逐行精读。

学完本讲你应该能够：

1. 说出 `push` 主循环里「计算 offset → 判块尾 → 预分配 → CAS 推进 tail → 写槽 → 发布」每一步的作用与先后顺序。
2. 解释为什么到块尾（`offset == BLOCK_CAP`）时要 `Backoff::snooze` 自旋等待，而 CAS 失败时却用更轻的 `Backoff::spin`。
3. 理解「写任务体 + `slot.state.fetch_or(WRITE, Release)`」这对发布顺序是如何让偷取方在 `Slot::wait_write` 里安全看到任务的。
4. 看懂跨块时 `tail.block` / `tail.index` / `block.next` 三处 `Release` store 如何把一个全新的 `Block` 安装进链表。

## 2. 前置知识

本讲默认你已掌握 u3-l1 的内容，即：索引编码常量、`Slot`/`Block`/`Position`/`Injector` 的数据结构。这里只把本讲会反复用到的事实简要复述，作为热身。

**索引编码回顾。** `tail.index` 是一个 `usize`，它把多层信息打包在一起：

- 最低 `SHIFT=1` 位是元数据位（`HAS_NEXT` 用），`push` 侧始终让这一位为 0，因此 push 侧的 index 永远是偶数。
- 逻辑位置（已经「预约」了多少个槽）是 `index >> SHIFT`。
- 块内偏移是 `(index >> SHIFT) % LAP`，其中 `LAP=64`。
- 第几圈（第几个 block）是 `(index >> SHIFT) / LAP`。

\[ \text{offset} = \left(\text{index} \gg \text{SHIFT}\right) \bmod \text{LAP} \]

每个 `LAP=64` 留出 `offset == 63`（即 `BLOCK_CAP`）这一格当**哨兵**，标记「本 block 已写满」，它本身不存任务，所以每个 block 实际只承载 `BLOCK_CAP = LAP - 1 = 63` 个任务。

**Backoff 回顾。** `crossbeam_utils::Backoff` 提供两档退避：

- `spin()`：极短的自旋（本质是 `spin_loop_hint`），用于「马上就会好」的轻微竞争。
- `snooze()`：较长的自旋，且在若干轮后会调用 `thread::yield_now()` 让出 CPU，用于「要等别的线程做完一段活」的较久等待。

**compare_exchange_weak 回顾。** 它是「乐观锁」原语：若 `tail.index` 当前的值仍等于我读到的 `tail`，就把它更新为 `new_tail` 并返回 `Ok`；否则把当前的真实值塞进 `Err(t)` 返回。`_weak` 版本允许「伪失败」（值其实相等也偶尔返回 `Err`），因此总是放在 `loop` 里重试。这正是 push 主循环的骨架。

## 3. 本讲源码地图

本讲只读一个文件，但会聚焦其中三段代码：

| 代码区域 | 行号 | 作用 |
| --- | --- | --- |
| `Injector::push` 主循环 | src/deque.rs:1388-1446 | 本讲主角：把一个任务安全写进队列 |
| 索引编码常量 | src/deque.rs:1200-1211 | `WRITE/READ/DESTROY` 状态位与 `LAP/BLOCK_CAP/SHIFT/HAS_NEXT` 索引位 |
| `Slot::wait_write` | src/deque.rs:1222-1230 | 偷取方如何等待 push 方置上 `WRITE` 位 |
| `Injector` 构造 `Default` | src/deque.rs:1346-1361 | 初始时 head/tail 都指向第一个预分配的 block |
| `tests/injector.rs::busy_retry` | tests/injector.rs:13-20 | 把可能 `Retry` 的 steal 包成确定性断言的测试辅助 |

## 4. 核心概念与源码讲解

### 4.1 push 的主循环骨架：offset 计算 + CAS 推进 tail.index

#### 4.1.1 概念说明

`Injector` 是 MPMC 队列，任意数量的线程都可能**同时**调用 `push`。多个 push 并发时，必须保证：

1. 每个任务被写进一个**唯一**的槽位，绝不重写、绝不跳号。
2. 槽位分配顺序就是 FIFO 顺序（`head` 端按 `index` 升序偷取）。

实现思路是经典的「乐观锁预约槽位」：每个 push 线程先读 `tail.index`，算出自己想写的 `offset`，再用一次 CAS 把 `tail.index` 往前推一格。**CAS 成功的那一刻，这个槽位就被该线程独占了**；CAS 失败说明有人抢先，重读最新值再试。这样无需互斥锁就保证了「一槽一线程」。

注意：CAS 成功只代表「拿到了槽位号」，**并不代表任务已经写好**。任务体的写入发生在 CAS 成功之后（见 4.3）。这二者之间的窗口正是「为什么需要 `WRITE` 位」的根源。

#### 4.1.2 核心流程

`push` 主循环的伪代码（去掉块尾处理细节后）：

```text
读 tail.index (Acquire), tail.block (Acquire)
loop {
    offset = (tail >> SHIFT) % LAP            # 我想写第几格
    若 offset == BLOCK_CAP: 见 4.2（块尾等待）
    若 offset+1 == BLOCK_CAP: 见 4.2（预分配）
    new_tail = tail + (1 << SHIFT)            # 推进一格（2 个 index 单位）
    match CAS(tail.index: tail -> new_tail):
        Ok  => { （可选）安装 next block 见 4.2；写槽位+发布见 4.3；return }
        Err(t) => { tail = t; 重读 block; backoff.spin(); 继续循环 }
}
```

关键不变式：

- `tail.index` 单调递增（`wrapping_add` 意义下），相邻两次成功 CAS 的差恒为 `1 << SHIFT`（即 2）。
- CAS 成功 ⟺ 该线程获得 `offset` 槽位的独占写权。

#### 4.1.3 源码精读

先看 `push` 的签名与循环开头的读取。注意两个原子读都用 `Ordering::Acquire`，是为了与别的线程安装新 block 时的 `Release` store 配对（见 4.2）。

[Injector::push 入口与初始加载 — src/deque.rs:1388-1393](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1388-L1393)：函数开头创建 `Backoff`，并 `Acquire` 读取 `tail.index` 与 `tail.block`；`next_block` 先初始化为 `None`（预分配块缓存，4.2 详述）。

`offset` 的计算只有一行，但浓缩了整个索引编码方案：

[计算 offset — src/deque.rs:1395-1396](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1395-L1396)：`offset = (tail >> SHIFT) % LAP`，即「先右移 1 位去掉元数据，再对 64 取模得到块内偏移」。

接下来是 CAS 推进 tail 的核心。`new_tail = tail + (1 << SHIFT)` 把 index 加 2（推进一个逻辑槽）。CAS 的成功序用 `SeqCst`、失败序用 `Acquire`：

[CAS 推进 tail.index — src/deque.rs:1412-1444](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1412-L1444)：

- 成功分支（`Ok(_)`）进入 `unsafe` 块，先（在块尾情况下）安装 next block，再写槽位、置 `WRITE` 位，最后 `return`。
- 失败分支把返回的当前值 `t` 赋给 `tail`，重新 `Acquire` 读取 `tail.block`（因为对方可能已经换了 block），然后 `backoff.spin()` 做一次短自旋再回到循环顶部。

`compare_exchange_weak` 而非 `compare_exchange`：因为外层是 `loop`，`weak` 版本偶尔的伪失败会被循环自然吸收，换来的（在部分架构上）是更廉价的指令序列。

#### 4.1.4 代码实践

**实践目标：** 通过手工演算，建立对 `tail.index` 编码与 CAS 推进的直觉，确认「相邻两次成功 push，`tail.index` 恰好差 2」。

**操作步骤：**

1. 假设一个全新的 `Injector`，初始 `tail.index = 0`。
2. 模拟连续 4 次 `push`，按下表填入每次循环里 `offset` 与成功后的 `tail.index`：

   | 第几次 push | 进入时 tail.index | offset | 成功后 tail.index |
   | --- | --- | --- | --- |
   | 1 | 0 | 0 | 2 |
   | 2 | 2 | ? | ? |
   | 3 | 4 | ? | ? |
   | 4 | 6 | ? | ? |

3. 写出 `(65 >> 1) % 64` 与 `65 % 64` 的结果，验证它们相等，从而理解「右移 1 位再取模」并不会破坏取模语义（因为本侧 index 都是偶数）。

**需要观察的现象 / 预期结果：** offset 序列应是 `0,1,2,3`；成功后 `tail.index` 序列应是 `2,4,6,8`，每步差 2（即 `1 << SHIFT`）。

**待本地验证：** 上述演算可手动完成；若想程序化确认，可写一个小例子在 `push` 前后用 `unsafe` 读 `tail.index`（仅作学习用途，不要用于生产），但更稳妥的是直接对照下面的练习答案。

#### 4.1.5 小练习与答案

**练习 1.** 为什么 `new_tail = tail + (1 << SHIFT)` 而不是 `tail + 1`？

**参考答案：** 因为最低位（`SHIFT=1` 位）被保留为 `HAS_NEXT` 元数据位，push 侧必须保持它为 0。每次推进一个逻辑槽，index 要加 `1 << SHIFT = 2`，这样「逻辑槽号 = index >> 1」始终成立，而 bit 0 永远干净。

**练习 2.** CAS 失败时为什么要重新 `Acquire` 加载 `tail.block`，而不能复用旧值？

**参考答案：** 失败说明别的线程已经推进过 `tail.index`。如果那次推进发生在块尾（`offset+1 == BLOCK_CAP`），对方会同时安装 next block 并把 `tail.block` 换成新块。若复用旧的 `block` 指针，可能写进已经写满的旧块，造成数据错乱。

### 4.2 块尾处理：snooze 自旋等待 + 预分配 next block

#### 4.2.1 概念说明

一个 block 只能装 63 个任务。当队列里的任务越来越多，必然要跨到下一个 block。跨块带来的两个难题：

1. **谁负责创建下一个 block？** 答案：写满当前 block 的最后一个槽位（`offset == BLOCK_CAP - 1 == 62`）的那次 push。
2. **别的 push 线程怎么知道「下一个 block 还没建好」？** 答案：它们会看到一个特殊的 `offset == BLOCK_CAP`（63）——这是哨兵位置，永远不会被写入任务，它存在就是为了标记「本 block 已满，下一个 block 正在/已经被安装」。

理解这一点至关重要：`tail.index` 在块尾会**短暂地**停在哨兵位置 `offset == 63`（因为写最后任务的 push 先 CAS 到了那里，紧接着才把 `tail.index` 跳到下一个 block 的起点，见 4.2.3）。在这段窗口里，其他并发 push 读到的 `offset == BLOCK_CAP`，于是明白要等。

#### 4.2.2 核心流程

块尾相关逻辑分散在主循环的三个位置，串起来是：

```text
loop {
    offset = (tail >> SHIFT) % LAP
    # ① 哨兵等待
    if offset == BLOCK_CAP:
        backoff.snooze()           # 较长退避：等别人安装 block
        重读 tail.index, tail.block
        continue
    # ② 预分配优化（要写本 block 最后一格之前）
    if offset + 1 == BLOCK_CAP and next_block is None:
        next_block = Some(Block::new())   # 提前分配，缩短 ③ 的窗口
    # ③ CAS 推进 tail ...
    # 成功后，若本次写的是最后一格（offset+1==BLOCK_CAP）：
    #    把 next_block 安装进链表（见 4.2.3）
}
```

为什么 ② 要在 CAS **之前**就分配好 `next_block`？因为 `Block::new()` 涉及堆分配，相对较慢。如果等 CAS 成功（即已经抢到最后一格、并发 push 已经开始排队等哨兵）才分配，那么「哨兵窗口」会被拉长，所有等待者都要多 snooze 久。提前分配好，CAS 一成功就能立刻安装，窗口最短。

#### 4.2.3 源码精读

**① 哨兵等待分支：** 到块尾时 `snooze`（长退避）后重读并 `continue`：

[offset == BLOCK_CAP 自旋等待 — src/deque.rs:1398-1404](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1398-L1404)：这里用 `backoff.snooze()` 而非 `spin()`，因为「安装一个 block」包含一次堆分配，可能耗时较长，纯 CPU 自旋会浪费资源，`snooze` 在等待若干轮后会 `yield_now()` 让出 CPU。

**② 预分配分支：** 仅在即将写本 block 最后一格、且尚未分配时触发：

[offset+1 == BLOCK_CAP 预分配 — src/deque.rs:1406-1410](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1406-L1410)：`next_block` 被缓存在循环外的局部变量里（见 [src/deque.rs:1392](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1392)），因此即便本次 CAS 失败、循环重试，预分配的 block 也能在下一轮被复用，不会泄漏或重复分配。

**③ CAS 成功后的安装：** 只有当本次写的就是最后一格（`offset + 1 == BLOCK_CAP`）时才执行：

[安装 next block — src/deque.rs:1421-1430](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1421-L1430)：

- `Box::into_raw(next_block.unwrap())` 把预分配的 block 从 `Box` 转成裸指针，所有权转移给链表。
- `next_index = new_tail.wrapping_add(1 << SHIFT)`：从当前 CAS 写入的哨兵 index 再加一格，**跳过哨兵偏移 63**，落到下一个 block 的 offset 0（其 `>> SHIFT` 是 64 的倍数，`% LAP == 0`）。
- `tail.block.store(next_block, Release)` 与 `tail.index.store(next_index, Release)`：用 `Release` 发布新 block 指针与新 index，让正在 ① 里 `snooze` 等待的线程一旦 `Acquire` 重读就能立刻看到。
- `(*block).next.store(next_block, Release)`：把当前 block 的 `next` 指针也接上，这样偷取侧走到块尾时能用 [`Block::wait_next`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1272-L1281) 取到下一个 block。

**注意 `tail.index` 的两次写入：** CAS 先把 `tail.index` 推到哨兵（offset 63），紧接着安装分支又用普通 `store` 把它覆盖到下一个 block 的起点。CAS 的成功序 `SeqCst` 与后续 `Release` store 共同保证了「写满本 block」这件事对其它线程可见的顺序。

#### 4.2.4 代码实践

**实践目标：** 通过运行一个真实测试，验证跨 block 的链表与索引维护正确（这是本讲的核心实践任务，对应大纲要求的 130 任务用例）。

**操作步骤：**

1. 在 `tests/` 目录新增一个文件（例如 `tests/cross_block.rs`），键入以下测试（参考 `tests/injector.rs` 中 [`busy_retry`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/tests/injector.rs#L13-L20) 与 `smoke` 的写法）：

   ```rust
   use crossbeam_deque::{Injector, Steal};

   // 把可能 Retry 的 steal 包成确定结果，参考 tests/injector.rs::busy_retry
   fn busy_retry<T>(mut f: impl FnMut() -> Steal<T>) -> Steal<T> {
       loop {
           let s = f();
           if !s.is_retry() {
               return s;
           }
       }
   }

   #[test]
   fn injector_cross_block_fifo() {
       let q = Injector::new();
       // 130 = 63 + 63 + 4：会跨越 3 个 block
       for i in 0..130u32 {
           q.push(i);
       }
       // 按 FIFO 顺序全部偷回
       for i in 0..130u32 {
           assert_eq!(busy_retry(|| q.steal()), Steal::Success(i));
       }
       assert_eq!(busy_retry(|| q.steal()), Steal::Empty);
   }
   ```

2. 在 `crossbeam-deque` 目录运行：

   ```bash
   cargo test --test cross_block
   ```

**需要观察的现象 / 预期结果：**

- 测试通过，说明前 63 个任务落在 block 0（offset 0..62）、第 64..126 个落在 block 1、最后 4 个落在 block 2，且 `steal` 按严格升序 `0..130` 取回，无丢失、无重复、无乱序。
- 若把上限改成 `63`、`126`（恰好填满 1 个、2 个 block 的边界），同样应通过——这能专门验证「写满 block 的最后一次 push 触发安装 next block」这条路径。

**待本地验证：** 上述断言基于源码逻辑推演；请在本地实际运行 `cargo test` 确认（如处于 Miri 环境，注意 `steal` 的 doctest 会因 `MIRI_FALLIBLE_WEAK_CAS` 跳过，但本测试不涉及该 cfg，正常可跑）。

#### 4.2.5 小练习与答案

**练习 1.** 为什么 ① 用 `snooze` 而 CAS 失败分支用 `spin`？

**参考答案：** ① 等待的是「另一个线程完成堆分配并安装 block」，可能耗时较长，用 `snooze`（最终会 `yield_now`）避免空耗 CPU；CAS 失败只是轻微竞争，对方马上就会让出槽位，用轻量 `spin` 即可。

**练习 2.** 假设把 ② 的预分配去掉，改成 CAS 成功后再 `Block::new()`，会对并发性能有什么影响？

**参考答案：** CAS 成功意味着本线程已抢到 block 的最后一格，此时其它 push 线程已经开始在 ① 里 snooze 等待安装。若此时才分配，分配期间所有等待者都在空转/让出，延长了「哨兵窗口」，吞吐下降。预分配把耗时操作挪到竞争形成之前，是最小化窗口的关键优化。

### 4.3 写槽位与发布：write + fetch_or(WRITE, Release)

#### 4.3.1 概念说明

CAS 成功只「预约」了槽位号，任务体还没写。这里要解决一个经典的发布（publication）问题：

- 写入侧：先写任务数据，再告诉别人「数据就绪」。
- 读取侧：先确认「数据就绪」，再读数据。

如果只写数据而不发信号，读取侧无从知道槽位是否已填；如果先发信号再写数据，读取侧可能读到半初始化的内存。`Injector` 用 `Slot.state` 的 `WRITE` 位充当这个信号：

- `WRITE` 位初值为 0；写入侧写完任务后用 `fetch_or(WRITE, Release)` 把它置 1。
- 读取侧在 [`Slot::wait_write`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1222-L1230) 里循环 `state.load(Acquire)`，直到看到 `WRITE` 位才继续读任务。

`Release`（写）与 `Acquire`（读）这对内存序建立了 **happens-before** 关系：读取侧一旦看到 `WRITE`，就必定能看到写入侧在 `fetch_or` **之前**对 `task` 写入的全部字节。这样任务体就不会被「撕裂」地读到。

#### 4.3.2 核心流程

CAS 成功后的发布顺序（无论是否跨块，最后两步都一样）：

```text
（可选）安装 next block：Release store tail.block / tail.index / block.next
slot = block.slots[offset]
slot.task.write(MaybeUninit::new(task))          # 写任务体（普通写，非原子）
slot.state.fetch_or(WRITE, Release)              # 发布：置 WRITE 位
return
```

注意任务体写入用的是 `UnsafeCell` 内部的裸指针写（非原子），它之所以安全，正是靠紧接着的 `fetch_or(WRITE, Release)` 来「兜底」可见性。

#### 4.3.3 源码精读

CAS 成功分支里的写槽与发布：

[写任务并发布 WRITE 位 — src/deque.rs:1432-1437](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1432-L1437)：

- `(*block).slots.get_unchecked(offset)`：拿到目标槽位（`get_unchecked` 越界检查被省略，因为 `offset < BLOCK_CAP` 由前面的块尾分支保证）。
- `slot.task.get().write(MaybeUninit::new(task))`：把任务包进 `MaybeUninit` 写入槽位的 `UnsafeCell`。这是非原子写。
- `slot.state.fetch_or(WRITE, Ordering::Release)`：原子地置 `WRITE` 位，`Release` 序确保上一行的写对任何 `Acquire` 读到 `WRITE` 的线程可见。
- `return`：完成。

偷取侧的配对读取在 [`Slot::wait_write`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1222-L1230) 与 `Injector::steal` 里：

[steal 读任务前先 wait_write — src/deque.rs:1525-1528](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1525-L1528)：`slot.wait_write()` 自旋直到 `state.load(Acquire) & WRITE != 0`，随后 `slot.task.get().read().assume_init()` 取出任务。这条 `Acquire` 加载与 push 侧的 `Release` 发布严格配对，构成完整的 happens-before。

状态位常量定义在：

[WRITE/READ/DESTROY 状态位 — src/deque.rs:1200-1202](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1200-L1202)：`WRITE=1`、`READ=2`、`DESTROY=4`，互不重叠的位，可用 `fetch_or` / 位与独立读写。本讲只用 `WRITE`；`READ` 与 `DESTROY` 留给 u3-l3 的 `steal` 与 `Block::destroy`。

> 小贴士：`fetch_or` 而非「先 `load` 再 `store`」是为了让它成为**单条**原子读改写指令，避免与并发 push（理论上不同槽位不会撞，但与 `READ`/`DESTROY` 的设置者可能并发）产生丢位。

#### 4.3.4 代码实践

**实践目标：** 体会「先写数据再发 Release 信号」的必要性——若颠倒顺序会读到未初始化内存。

**操作步骤（源码阅读型，无需改源码）：**

1. 重读 [src/deque.rs:1432-1437](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1432-L1437) 与 [src/deque.rs:1222-1230](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1222-L1230)。
2. 在注释里画出两条线程的时序：

   ```
   push 线程:   write(task) ──► fetch_or(WRITE, Release)
   steal 线程:                          │
                                        ▼ (happens-before)
                  wait_write: load(state, Acquire) 看到 WRITE ──► read(task)
   ```

3. 假设把 push 改成「先 `fetch_or(WRITE)` 再 `write(task)`」，回答下面的练习 2。

**需要观察的现象 / 预期结果：** 你应当能解释：颠倒后，steal 可能在 `write(task)` 完成前就看到 `WRITE`，从而 `read` 到 `MaybeUninit` 的随机字节（对非 `Copy` 类型甚至触发未定义行为）。这正是 `Release/Acquire` 配对不可省略的原因。

#### 4.3.5 小练习与答案

**练习 1.** 任务体写入 `slot.task.write(...)` 是非原子的，为什么不会构成数据竞争（data race）？

**参考答案：** 因为对同一槽位的「写任务体」与「读任务体」永远由 happens-before 串行化：写入侧先于其 `fetch_or(WRITE, Release)`，读取侧后于其 `load(Acquire)` 看到 `WRITE`，而 `Release`/`Acquire` 建立了先后关系。Rust 内存模型下，被 happens-before 串行化的非原子访问不构成数据竞争。此外，每个槽位一旦被某次 push 写入并被对应 steal 读出，就再不会被复用（block 用完即销毁），不存在交错重写。

**练习 2.** 若把发布顺序颠倒（先置 `WRITE` 再写任务体），`Slot::wait_write` 会怎样？

**参考答案：** `wait_write` 可能在任务体尚未写完时就观察到 `WRITE`，随即 `read().assume_init()` 读到未初始化或半初始化的内存，产生未定义行为。因此 `write` 在前、`fetch_or(WRITE)` 在后的顺序是正确性所必需的，不是性能优化。

## 5. 综合实践

把本讲三个模块串起来：构造一个会**连续跨越多个 block**、并在**多线程并发 push** 下运行的场景，验证 `push` 在竞争与跨块双重压力下仍保持 FIFO 与无丢失。

**任务：** 完成下面的多线程测试骨架（可与 4.2.4 的单线程测试对照）。目标线程数与每个线程的 push 数请保证总任务数远超两个 block 的容量（例如 4 线程 × 64 任务 = 256，跨 5 个 block）。

```rust
use std::sync::Arc;
use std::sync::atomic::{AtomicUsize, Ordering::SeqCst};

use crossbeam_deque::{Injector, Steal};
use crossbeam_utils::thread::scope;

fn busy_retry<T>(mut f: impl FnMut() -> Steal<T>) -> Steal<T> {
    loop {
        let s = f();
        if !s.is_retry() { return s; }
    }
}

#[test]
fn injector_concurrent_push_fifo() {
    let q = Arc::new(Injector::new());
    const THREADS: usize = 4;
    const PER_THREAD: usize = 64;
    const TOTAL: usize = THREADS * PER_THREAD;

    // 每个线程注入自己区段的编号，便于事后校验「恰好消费一次」
    scope(|s| {
        for t in 0..THREADS {
            let q = q.clone();
            s.spawn(move |_| {
                for i in 0..PER_THREAD {
                    q.push(t * PER_THREAD + i);
                }
            });
        }
    }).unwrap();

    // 全部偷回，断言总数正确且无重复
    let seen = Arc::new(vec![AtomicUsize::new(0); TOTAL]);
    let mut got = 0usize;
    loop {
        match busy_retry(|| q.steal()) {
            Steal::Success(v) => {
                assert_eq!(seen[v].fetch_add(1, SeqCst), 0, "任务 {} 被重复消费", v);
                got += 1;
            }
            Steal::Empty => break,
            _ => unreachable!(),
        }
    }
    assert_eq!(got, TOTAL, "应有 {} 个任务，实际 {}", TOTAL, got);
}
```

**运行：** `cargo test --test injector_concurrent -- --test-threads=1`（也可直接放进 `tests/injector.rs` 一起跑）。

**验收点：**

1. 测试通过 ⟹ 并发 push 下跨 block 的链表安装（4.2）与 `WRITE` 发布（4.3）都正确。
2. 多跑几次（并发 bug 常偶发）仍稳定通过。
3. 尝试把 `PER_THREAD` 调到正好让某个线程的最后一次 push 落在块尾（例如调整使得某线程写满 block），重点观察是否仍稳定。

## 6. 本讲小结

- `push` 用「乐观锁预约槽位」：`Acquire` 读 `tail.index` → 算 `offset` → `compare_exchange_weak(SeqCst/Acquire)` 推进一格，CAS 成功即独占该槽位。
- `offset == BLOCK_CAP`（哨兵 63）表示本 block 已满、下一个 block 正在/已被安装，此时用 `Backoff::snooze`（长退避）等待并重读；CAS 失败则用更轻的 `Backoff::spin`。
- 写本 block 最后一格之前会**预分配** `next_block`，使 CAS 成功后能立刻安装，最小化「哨兵窗口」。
- 跨块安装用三处 `Release` store（`tail.block` / `tail.index` / `block.next`），把新 block 同时暴露给等待的 push 与偷取侧的 `Block::wait_next`。
- 任务体写入用非原子 `write`，靠紧随其后的 `slot.state.fetch_or(WRITE, Release)` 发布；偷取侧的 `Slot::wait_write`（`Acquire`）与之配对，建立 happens-before，杜绝读到未初始化内存。
- `tail.index` 单调推进，相邻成功 push 差值恒为 `1 << SHIFT = 2`，跨块时跳过哨兵偏移 63。

## 7. 下一步学习建议

本讲只覆盖了「生产」端（`push`）。要理解 `head` 端如何消费、block 如何被协作销毁，请进入：

- **u3-l3 Injector::steal 与 Block 销毁机制**：精读 `steal` 的 head 推进、`HAS_NEXT` 判空、`wait_next` 跨块切换，以及 `Block::destroy` 如何用 `READ`/`DESTROY` 位实现「最后一个读者负责释放 block」的惰性回收。
- 之后 **u4-l2（epoch GC）** 会回到「跨线程延迟回收内存」这一主题，把 Injector 的 block 销毁与 Worker 的 buffer 回收放在统一的内存安全框架下对照。
- 若想横向对比「单端环形缓冲区」的实现，可回顾 u2-l1 / u2-l2，体会 Injector 的「链表块队列」与 Worker 的「固定环形 buffer」在扩容、回收上的本质差异。
