# CachePadded 与 Backoff：伪共享与自旋退避优化

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清楚「伪共享（false sharing）」是什么，以及它为什么会拖慢一个看似正确的无锁队列；
- 解释 `CachePadded` 用「对齐 + 填充」把 `head` 与 `tail` 钉到不同缓存行的原理，并能读懂它的 `repr(align(N))` 实现；
- 解释 `Backoff` 的指数退避算法，区分 `spin()`（忙等）与 `snooze()`（让出）两种策略；
- 在两个队列的源码中，准确指出每一个 `backoff.spin()` 与 `backoff.snooze()` 调用点，并说明「为什么这里是 spin、那里是 snooze」；
- 自己写一个粗略的吞吐 benchmark，感受这两类优化在高并发 MPMC 场景下的作用。

本讲是「专家层」的工程优化篇，承接你已经掌握的 `ArrayQueue`（u2-l1～u2-l4）与 `SegQueue`（u3-l1～u3-l4）主链路，以及 u4-l1 的原子内存序知识。本讲不再重复算法本身，只聚焦**为什么这套无锁算法在真实 CPU 上跑得快**。

## 2. 前置知识

### 2.1 缓存行（cache line）

CPU 不按字节读写内存，而是按「缓存行」整块搬运。主流 x86-64 / aarch64 的一行通常是 **64 字节**（部分架构更大）。当核心修改某个字节时，硬件会让**整行**进入「已修改」状态（MESI 协议中的 Modified），其他核心里缓存了同一行的副本会被作废（invalidate）。

> 直觉比喻：缓存行就像一节 64 个座位的地铁车厢。你只动了 1 号座位，整节车厢对别人来说都「过期」了，必须重新去总站（内存 / 别的核心）拉一份新的。

### 2.2 伪共享（false sharing）

两个线程各自只写**自己的**变量，逻辑上互不干扰，但这两个变量恰好落在**同一个缓存行**里。于是每次写都会作废对方核心里的那一行，导致这节车厢在两个核心之间来回搬运——**逻辑上没有数据竞争，物理上却像在抢同一把锁**。这就是伪共享。在 `head`/`tail` 这种「消费者狂写 head、生产者狂写 tail」的场景下，伪共享能让吞吐掉一个数量级。

### 2.3 CAS 自旋与总线流量

无锁队列靠 `compare_exchange_weak`（CAS）抢指针。CAS 是一条 read-modify-write 指令，它**独占整条缓存行**。N 个线程在同一颗原子变量上拼命重试时，缓存行会在核心间疯狂弹跳（cache line ping-pong），就算算法是「正确无锁」的，吞吐也会被互连带宽压垮。`Backoff` 就是用来给这股竞争「降温」的。

### 2.4 PAUSE / YIELD 提示指令

`core::hint::spin_loop()` 会被编译成 CPU 的 `PAUSE`（x86）或 `YIELD`（ARM）指令。它**不让出时间片**，只是告诉 CPU「我在自旋」，从而：降低这一小段流水线的功耗、避免分支预测惩罚、改善超线程 sibling 的执行机会。它是「忙等」的礼貌版。

---

## 3. 本讲源码地图

本讲涉及的真实源码文件（跨 `crossbeam-queue` 与其依赖 `crossbeam-utils`）：

| 文件 | 作用 |
| --- | --- |
| `crossbeam-queue/src/array_queue.rs` | 在 `head`/`tail` 字段上使用 `CachePadded`；在 push/pop 主循环中使用 `Backoff` 的 spin/snooze |
| `crossbeam-queue/src/seg_queue.rs` | 在 `head`/`tail` 的 `Position` 上使用 `CachePadded`；在 push/pop/wait_write/wait_next 中使用 `Backoff` |
| `crossbeam-utils/src/cache_padded.rs` | `CachePadded<T>` 的定义：`repr(align(N))` + 填充 + `Deref`/`DerefMut` |
| `crossbeam-utils/src/backoff.rs` | `Backoff` 的定义：`new`/`spin`/`snooze`/`is_completed` 的指数退避实现 |

两个队列文件通过 `use crossbeam_utils::{Backoff, CachePadded};` 引入这两个原语，本仓库不重复实现它们。

---

## 4. 核心概念与源码讲解

### 4.1 CachePadded：用对齐与填充消除伪共享

#### 4.1.1 概念说明

`ArrayQueue` 有两个被高频写入的原子游标：`head`（消费者推进）和 `tail`（生产者推进）。它们各只是一个 `usize`（8 字节）。如果直接挨在一起：

```
| head (8B) | tail (8B) | ......... 其余 48 字节 ......... |   ← 同一个 64B 缓存行
```

那么生产者写 `tail` 时，会把消费者核心里的 `head` 一起作废；消费者写 `head` 时，又把生产者核心里的 `tail` 作废。两个本不相关的变量被绑在了同一节「车厢」上——典型伪共享。

`CachePadded<T>` 的解法是：用 `repr(align(N))` 强制 `T` 的起始地址按 N 字节对齐，并把整个结构体大小补齐到 N 的整数倍。于是 `CachePadded<AtomicUsize>` 自己就独占一整条缓存行，`head` 和 `tail` 自然落在不同行，互不干扰。

#### 4.1.2 核心流程

`CachePadded` 的「魔法」完全来自 Rust 的 `repr(align(N))`：

1. **对齐**：结构体起始地址必须是 N 的倍数（N 随架构变化，x86-64 取 128）。
2. **填充**：编译器自动在 `value` 后面补零，直到 `size_of::<CachePadded<T>>()` 是 N 的倍数。
3. **透明访问**：实现 `Deref` / `DerefMut`，让 `self.tail.load(...)` 这种写法能穿透包装，直接调用内部 `AtomicUsize` 的方法。

各架构假定的缓存行长度 N：

| 架构 | N |
| --- | --- |
| x86-64 / aarch64 / arm64ec / powerpc64 | 128 |
| s390x | 256 |
| arm / mips / sparc / hexagon | 32 |
| m68k | 16 |
| 其它（含 x86、wasm、riscv） | 64 |

> 为什么 x86-64 用 128 而不是 64？源码注释里有说明：从 Intel Sandy Bridge 起，空间预取器（spatial prefetcher）会**成对**地拉取两行 64 字节，所以保守按 128 对齐。这部分注释见 `cache_padded.rs` 的对齐配置区。

注意：N 只是「合理猜测」，不保证等于运行机器的真实缓存行长度——但只要 N ≥ 真实行长，填充就一定有效。

#### 4.1.3 源码精读

`CachePadded` 的对齐通过一组 `#[cfg_attr(...)]` 实现，以 x86-64 的 128 字节对齐为例：

[cache_padded.rs:L86-L90](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/cache_padded.rs#L86-L90) — 给 x86-64 / aarch64 / powerpc64 设 `repr(align(128))`；其余架构各自有对应的 `cfg_attr` 分支。

结构体本身只有一个字段，对齐保证由外层 `repr(align)` 提供：

[cache_padded.rs:L150-L151](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/cache_padded.rs#L150-L151) — `pub struct CachePadded<T> { value: T }`，大小被编译器补齐到 N 的整数倍。

之所以外部代码写 `self.tail.load(...)` 不会报错，是因为这两个 trait：

[cache_padded.rs:L187-L197](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/cache_padded.rs#L187-L197) — `Deref`/`DerefMut` 把 `&CachePadded<T>` 透明地转成 `&T` / `&mut T`，这也是 u2-l4 提到的 `get_mut()` 能穿透 `CachePadded` 的根因。

在 `ArrayQueue` 里，两个字段被各自包了一层：

[array_queue.rs:L59-L67](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L59-L67) — `head: CachePadded<AtomicUsize>` 与 `tail: CachePadded<AtomicUsize>`，二者各占独立缓存行。

构造时用 `CachePadded::new` 包裹初始原子值：

[array_queue.rs:L122-L123](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L122-L123) — `head`/`tail` 都初始化为 0（`{ lap:0, index:0 }`）。

`SegQueue` 的做法完全一致，只是内部类型换成更复杂的 `Position<T>`：

[seg_queue.rs:L163-L166](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L163-L166) — `head`/`tail` 都是 `CachePadded<Position<T>>`，`Position` 里含 `index` 与 `block` 两个原子字段，整块被钉在一行。

#### 4.1.4 代码实践

**目标**：用一个**纯独立**的微型 benchmark，亲眼看到伪共享对两个原子计数器的拖累。这不需要改队列源码，只需 `crossbeam_utils::CachePadded`。

1. 实践目标：量化「两个线程各写各的原子变量」时，共享缓存行 vs 分离缓存行的吞吐差距。
2. 操作步骤：新建一个临时 binary，定义两个对照结构体，分别起两个线程各 `fetch_add` 5 千万次，用 `std::time::Instant` 计时。

   ```rust
   // 示例代码：伪共享微基准（不是 crossbeam-queue 的源码）
   use std::sync::atomic::{AtomicUsize, Ordering};
   use std::time::Instant;
   use crossbeam_utils::CachePadded;

   // 对照组 A：两个原子紧挨着 —— 大概率落在同一缓存行（伪共享）
   struct Shared { a: AtomicUsize, b: AtomicUsize }
   // 对照组 B：各自 CachePadded —— 强制分到不同缓存行
   struct Padded { a: CachePadded<AtomicUsize>, b: CachePadded<AtomicUsize> }

   fn main() {
       for (name, run) in [
           ("shared (false sharing)", run_shared as fn() -> u128),
           ("padded (CachePadded)",   run_padded as fn() -> u128),
       ] {
           let t = Instant::now();
           let ops = run();
           println!("{name}: {ops} ops in {:?}", t.elapsed());
       }
   }
   // run_shared / run_padded：各 spawn 两个线程，
   // 一个循环 a.fetch_add(1, Relaxed)，一个循环 b.fetch_add(1, Relaxed)，
   // 每个线程做 50_000_000 次，join 后返回总 ops。
   ```

3. 需要观察的现象：在多核机器上，「shared」版本的耗时应明显大于「padded」版本。
4. 预期结果：**待本地验证**。典型多核 x86-64 上，padded 版常快数倍；但单核或超线程 sibling 上差距可能很小。
5. 若差距不明显，检查：是否真的跑在多核物理核上、`std::time::Instant` 精度、编译是否 `--release`。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `CachePadded` 从 `ArrayQueue` 的 `head`/`tail` 上去掉，功能会出错吗？为什么？

**答案**：功能**不会**出错。`CachePadded` 只影响内存布局（对齐与填充），不改变任何读写语义；`Deref` 让所有 `.load()/.store()/.compare_exchange_weak()` 调用照常工作。去掉它只是让 `head`/`tail` 可能共享缓存行，损害的是**性能**，不是正确性。

**练习 2**：`size_of::<CachePadded<AtomicUsize>>()` 在 x86-64 上大约是多少？为什么不是 8？

**答案**：约 128 字节。`AtomicUsize` 本身 8 字节，但 `repr(align(128))` 要求大小是 128 的整数倍，所以编译器在后面填充了约 120 字节——这正是「用空间换缓存行隔离」的代价。

---

### 4.2 Backoff::spin：CAS 失败后的指数忙等退避

#### 4.2.1 概念说明

无锁主循环里，CAS 抢指针失败是常态（别人抢先了一步）。最朴素的写法是「失败立刻重试」，但在高并发下，N 个线程立刻重试会让缓存行更加频繁地弹跳，反而**降低**所有人成功的概率——这叫「惊群」。

`Backoff::spin()` 给出温和的对策：失败一次后，先空转一小会儿再重试；再失败，空转两倍时间……每次翻倍。这样并发的重试会被自然地「摊开」，减少正面碰撞。关键性质：`spin()` **只发 CPU 提示指令（PAUSE/YIELD），绝不让出时间片**——因为 CAS 失败意味着别人刚刚成功，资源很可能「立刻」可用，应该保持热度等。

#### 4.2.2 核心流程

`Backoff` 内部只有一个计数器 `step: Cell<u32>`，初值 0。`spin()` 的行为：

```
本次空转次数 = 2 ^ min(step, SPIN_LIMIT)        # SPIN_LIMIT = 6
执行该次数的 hint::spin_loop()
若 step ≤ SPIN_LIMIT：step += 1                  # 封顶后不再增长
```

退避进度表（`spin()` 连续调用）：

| 调用次数（step） | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 第 8 次起 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 空转 spin_loop 次数 | 1 | 2 | 4 | 8 | 16 | 32 | 64 | 64（封顶） |

也就是说，前几次几乎「立刻重试」（保持低延迟），失败多次后才退化成每次 64 次 PAUSE。指数增长公式：

\[
\text{iters}(s) = 2^{\min(s,\,\text{SPIN\_LIMIT})}
\]

`SPIN_LIMIT = 6` 与 `YIELD_LIMIT = 10` 这两个常量定义在文件顶部：

[backoff.rs:L5-L6](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/backoff.rs#L5-L6) — `SPIN_LIMIT=6` 控制 spin 的指数上限；`YIELD_LIMIT=10` 控制 snooze 何时升级为让出（见 4.3）。

#### 4.2.3 源码精读

`spin()` 的实现非常短：

[backoff.rs:L146-L154](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/backoff.rs#L146-L154) — 用 `1 << step` 算出空转次数、循环调 `hint::spin_loop()`，随后给 `step` 自增（封顶于 `SPIN_LIMIT`）。注意全程没有 `thread::yield_now()`。

`Backoff` 结构与构造：

[backoff.rs:L80-L95](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/backoff.rs#L80-L95) — `Backoff { step: Cell<u32> }`，`new()` 把 step 置 0。用 `Cell` 是因为 `spin`/`snooze` 通过 `&self` 自增计数器。

在 `ArrayQueue::push_or_else` 的 CAS 失败分支，正是经典 spin 用法：

[array_queue.rs:L171-L175](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L171-L175) — `compare_exchange_weak` 失败（`Err(t)`）时，用拿回的新 tail 重载、然后 `backoff.spin()` 再进下一轮。语义即「别人刚赢了这一格，我退一步马上重试」。

`SegQueue::push` 的 CAS 失败分支同样用 spin：

[seg_queue.rs:L285-L289](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L285-L289) — `tail.index` 的 CAS 失败后，重载 `block` 并 `backoff.spin()`。

注意：`push_or_else` 还有一处 `backoff.spin()` 在「疑似满」分支（[array_queue.rs:L179](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L179)），那里读完权威 `head` 后重试，也属于「可能很快就有进展」的快速重试场景，故用 spin 而非 snooze。

#### 4.2.4 代码实践

**目标**：把队列里所有 `backoff.spin()` 调用点枚举出来，并理解它们的共同特征。

1. 实践目标：建立「spin = CAS 失败/快速重试」的判断直觉。
2. 操作步骤：在 `array_queue.rs` 与 `seg_queue.rs` 中搜索 `backoff.spin()`，逐个记录其所在分支条件。
3. 需要观察的现象：每个 `spin()` 调用点的上方，是否都是一次「CAS 失败」或「读到可能过期的值、即将重读权威值」？
4. 预期结果：应能列出至少 5 个 spin 调用点（`array_queue` 的 push_or_else 两处、pop 两处；`seg_queue` 的 push、pop 各一处 CAS 失败处），且全部属于「竞争失败型」重试。
5. 思考题（不必改码）：把某处 `spin()` 改成 `snooze()` 会让「竞争失败但很快可重试」的路径多一次 OS 让出，预期会**增加**单次失败的延迟——这就是为什么这里要选 spin。

#### 4.2.5 小练习与答案

**练习 1**：`spin()` 为什么不让出时间片（不调 `thread::yield_now`）？

**答案**：因为 CAS 失败意味着另一个线程**刚刚成功**推进了游标，目标资源大概率立即可用。让出时间片会触发一次内核态调度（微秒级），远比空转几十纳秒昂贵。保持忙等、只发 PAUSE 提示，是「等几十纳秒就能拿到的资源」的最优解。

**练习 2**：若把 `SPIN_LIMIT` 调到 20，`spin()` 的行为会如何变化？

**答案**：指数退避会一直翻倍到 `2^20`（约 100 万次）PAUSE 才封顶，单次失败后的等待时间暴涨。对于「稍等即可」的 CAS 重试场景，这会显著拖慢低竞争下的延迟——`SPIN_LIMIT=6`（封顶 64 次）是经验上的低延迟折中。

---

### 4.3 Backoff::snooze：等待他人推进时的让出退避

#### 4.3.1 概念说明

另一类等待不是「我抢输了」，而是「**别人还没干完，我在等他**」。例如消费者抢到了一个槽位，但生产者还没把值写进去（`SegQueue` 的 `wait_write`）；或队列尾部到达块末，等别人安装下一个块（`wait_next`）。这种等待可能跨越一次完整的内存写甚至一次堆分配，对方线程甚至可能被操作系统调度走了。此时再用纯忙等（`spin`）会白白烧一个时间片的 CPU，还可能抢走对方的时间片、延长等待。

`snooze()` 的策略是「先礼后兵」：前几步和 `spin` 一样用 PAUSE 忙等（也许对方马上就好）；超过 `SPIN_LIMIT` 后，**调用 `thread::yield_now()` 主动让出时间片**，把 CPU 让给可能正在干活的对方线程。

#### 4.3.2 核心流程

```
若 step ≤ SPIN_LIMIT(6)：空转 2^step 次 spin_loop（忙等阶段）
否则（step > 6）：
    在 std 下：调用 thread::yield_now() 让出时间片
    在 no_std 下：仍只能继续 spin_loop（没有 OS 可让）
若 step ≤ YIELD_LIMIT(10)：step += 1
```

`snooze` 的退避进度表：

| 调用次数（step） | 1～6 | 7 | 8 | 9 | 10 | 第 11 次起 |
| --- | --- | --- | --- | --- | --- | --- |
| 行为 | 2^step 次 PAUSE | yield_now | yield_now | yield_now | yield_now | yield_now（封顶） |

也就是说，`snooze` 会在「短暂的忙等」之后升级为「持续让出」。配合 `is_completed()`（`step > YIELD_LIMIT`），调用方还能进一步选择阻塞（`thread::park`）——不过两个队列都没有用到 `is_completed`，而是停留在 `snooze` 层。

#### 4.3.3 源码精读

`snooze()` 的实现，注意 `else` 分支里的 `yield_now`：

[backoff.rs:L207-L225](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/backoff.rs#L207-L225) — `step ≤ SPIN_LIMIT` 时只 `spin_loop`；超过后，`#[cfg(feature = "std")]` 调 `::std::thread::yield_now()`，`#[cfg(not(feature = "std"))` 退化为继续 `spin_loop`。`step` 封顶于 `YIELD_LIMIT`。

`snooze` 在 `SegQueue` 的两个等待函数里是核心：

[seg_queue.rs:L45-L50](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L45-L50) — `Slot::wait_write`：消费者抢到槽但生产者尚未写值，循环 `Acquire` 读 `state`，等 `WRITE` 位置位，期间 `backoff.snooze()`。这是「等别人完成多步操作（写值 + fetch_or WRITE）」的典型场景。

[seg_queue.rs:L93-L102](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L93-L102) — `Block::wait_next`：队尾已到块末，等安装 `next` 块（一次堆分配 + 多个 Release store），同样 `snooze`。

在两个主循环里，「到达块末等待下一块」「首块尚未分配」都用 snooze：

[seg_queue.rs:L224-L230](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L224-L230) — `push` 中 `offset == BLOCK_CAP` 时 `backoff.snooze()`，等下一块安装；

[seg_queue.rs:L399-L405](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L399-L405) — `pop` 中 `block.is_null()` 时 `backoff.snooze()`，等首次 push 把首块建出来。

`ArrayQueue` 里也有对称的 snooze——「等 stamp 被更新」的分支：

[array_queue.rs:L181-L185](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L181-L185) — 注释直接写明 `// Snooze because we need to wait for the stamp to get updated.` 此时 stamp 既不等于 tail 也不等于 head+1，说明别的线程正在该槽上进退两难，必须等它更新 stamp。

#### 4.3.4 代码实践

**目标**：理解 `wait_write` 为什么必须用 snooze 而不是 spin。

1. 实践目标：把「等待型」场景与「竞争失败型」场景区分清楚。
2. 操作步骤：阅读 [seg_queue.rs:L428-L430](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L428-L430) 处 `pop` 抢到槽后调用 `slot.wait_write()` 的逻辑，画一条时间线：生产者执行 `write(value)` 与 `fetch_or(WRITE, Release)` 之间，消费者已经在 `wait_write` 里自旋。
3. 需要观察的现象：如果生产者在此期间被 OS 调度走（比如时间片耗尽），消费者若用纯 `spin` 会持续占用一颗核心空转；而 `snooze` 在 6 次后让出，把核心还给可能正等着运行的生产者。
4. 预期结果：在「生产者被抢占」的极端情况下，snooze 能让整体推进更快；在「生产者立刻写完」的常见情况下，snooze 的前 6 次忙等又保证了低延迟。这是它相对纯 spin / 纯 yield 的优势。
5. 「待本地验证」：可用一个故意在生产者 `write` 前插入 `thread::sleep` 的实验，对比 spin-only 与 snooze 的消费者 CPU 占用与完成时间。

#### 4.3.5 小练习与答案

**练习 1**：`snooze` 在 `no_std` 下退化为只 `spin_loop`，这会带来什么后果？

**答案**：在没有 OS 调度器的裸环境（no_std）里没有「让出时间片」可言，唯一能做的就是 CPU 提示指令。因此 `snooze` 退化为持续 PAUSE。后果是：当对方线程若以中断 / 另一个核的形式存在，仍可推进；但若系统是单核协作式调度，纯忙等可能饿死对方——这正是 no_std 嵌入式使用无锁原语时需要特别小心的地方。

**练习 2**：为什么两个队列都不用 `is_completed()` + `thread::park()`，而停在 `snooze`？

**答案**：`park` 需要一个配对的 `unpark` 通知机制，而无锁队列的「对方」是不固定的多个生产者/消费者，无法可靠地知道该由谁来 `unpark`。引入 park/unpark 会让队列从「无锁」退化成「可能阻塞」，违背设计目标。`snooze` 是「尽量不自旋，但不真的睡」的折中——保留无锁活性（live-lock 自由），又避免纯忙等的浪费。

---

### 4.4 何时 spin、何时 snooze：两种退避的工程取舍

#### 4.4.1 概念说明

`spin` 与 `snooze` 不是随便选的。两者对应**两类本质不同的「失败」**：

- **竞争失败（contention miss）**：我抢同一格输了，但**别人已经成功**，资源马上会再可用 → 用 `spin`，保持热度立刻重试。
- **等待依赖（waiting for a producer/peer）**：我需要的数据**别人还没产出**（值没写、块没装、stamp 没更新），对方可能正被调度走 → 用 `snooze`，先短暂忙等，不行就让出 CPU 促其推进。

一句话总结：**「抢输了」spin，「等别人」snooze。**

#### 4.4.2 核心流程

两个队列里 spin/snooze 的选择决策表：

| 场景 | 出现位置 | 选用 | 理由 |
| --- | --- | --- | --- |
| CAS 抢游标失败 | `push_or_else`、`pop`、`SegQueue::push/pop` 的 CAS `Err` 分支 | `spin` | 别人刚赢，资源立刻可重抢 |
| 疑似满/空，重读权威游标 | `push_or_else` 疑似满分支、`pop` 疑似空分支 | `spin` | 读到的可能是过期值，重读很快有进展 |
| 等 stamp 更新 | `ArrayQueue` push/pop 的 else 分支 | `snooze` | 别的线程在该槽上进退两难，需等它写完 stamp |
| 等 WRITE 位（值未写） | `SegQueue::Slot::wait_write` | `snooze` | 等生产者写值 + 置 WRITE |
| 等 next 块安装 | `Block::wait_next`、push/pop 的 `offset==BLOCK_CAP` | `snooze` | 等一次堆分配 + 多个 store |
| 等首块分配 | `SegQueue::pop` 的 `block.is_null()` | `snooze` | 等首次 push 建出首块 |

可以看到：凡是「立刻重试有希望」的，一律 spin；凡是「依赖别人完成一段未完成工作」的，一律 snooze。规则高度一致，跨两个队列通用。

#### 4.4.3 源码精读

最能体现「同一次循环里同时用 spin 和 snooze」的，是 `ArrayQueue::push_or_else` 的三分支结构。对照看 CAS 失败（spin）与等 stamp（snooze）：

[array_queue.rs:L154-L186](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L154-L186) — 三个分支：`tail == stamp`（可写，CAS 失败时 spin）、疑似满（spin 后重读 tail）、else（snooze 等 stamp 更新）。同一函数内同时出现两种退避，正说明它们面向不同情形。

`SegQueue::push` 同样在一段循环里混用：块末等待用 snooze，CAS 失败用 spin：

[seg_queue.rs:L258-L290](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L258-L290) — `Ok(_)` 写值成功；`Err(t)` 是 CAS 失败，重载 block 后 `backoff.spin()`；而前面的块末分支是 `snooze`。

一个值得注意的细节：`spin` 与 `snooze` 都按「每次循环一轮」自增 `step`。因此在长时间等待（如 `wait_next`）里，`snooze` 会逐步升级到 `yield_now`；而在高竞争的 CAS 路径上，`spin` 会封顶在 64 次 PAUSE，避免无限增长。

#### 4.4.4 代码实践（本讲主任务）

**目标**：写一个 MPMC 吞吐 benchmark，量化「CachePadded + Backoff」这套组合在真实竞争下的作用，并定位 Backoff 在 CAS 失败路径上的贡献。

1. 实践目标：用 `std::time::Instant` 粗测 `ArrayQueue` 在 4 生产者 + 4 消费者下的吞吐（ops/s）。
2. 操作步骤：
   - 新建临时 binary，依赖 `crossbeam-queue = "0.3"` 与 `crossbeam-utils = "0.8"`。
   - 参照 `tests/array_queue.rs` 里 `mpmc` 测试的 `scope` 写法（[array_queue.rs:L215-L249](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/tests/array_queue.rs#L215-L249)），4 个线程各 push `COUNT` 次、4 个线程各 pop 到收齐。
   - 在 `scope` 外用 `Instant::now()`…`elapsed()` 计时，吞吐 = `8 * COUNT / 耗时秒数`。
   - 把 `COUNT` 设大（如 200_000），`--release` 跑。
3. 需要观察的现象：记录 baseline 吞吐；然后**本地 fork** 一份 `array_queue.rs`，把所有 `backoff.spin()` 与 `backoff.snooze()` 注释掉（直接重试/重读），再测一次；再把 `head`/`tail` 的 `CachePadded<...>` 改成裸 `AtomicUsize`，再测一次。
4. 预期结果：**待本地验证**。典型多核机器上，去掉 Backoff 后高竞争下吞吐应明显下降（CAS 风暴）；去掉 CachePadded 后吞吐也会下降（伪共享）。注意：这是 fork 实验，不要提交对源码的修改。
5. 进阶：用 `perf stat -e cache-misses,mem_load_l3_miss_retired` 之类的工具观察「去掉 CachePadded」前后 cache-miss 的变化，能更直接看到伪共享的物理表现（待本地具备 perf 环境）。

#### 4.4.5 小练习与答案

**练习 1**：在 `ArrayQueue::pop` 中，「疑似空」分支用的是 `spin` 而非 `snooze`（[array_queue.rs:L363-L373](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L363-L373)）。为什么？

**答案**：疑似空分支里先 `fence(SeqCst)` 再重读权威 `tail`：若 `tail == head` 才真正返回 `None`；否则说明「刚才只是 stamp 还没跟上」，立刻重读 `head` 大概率就能 pop。这属于「读到可能过期的值、重读很快有进展」的快速重试，与 CAS 失败同属竞争型，故用 spin。真正的「等别人产出」才用 snooze。

**练习 2**：假设你把 `ArrayQueue::push_or_else` 里 CAS 失败处的 `backoff.spin()` 误改成 `backoff.snooze()`，单线程下会有可观察影响吗？多线程呢？

**答案**：单线程下 CAS 不会失败（无竞争），该分支根本不执行，故无影响。多线程下，CAS 失败后会过早升级到 `yield_now()`，每次小竞争都付出一次 OS 调度开销，**低~中竞争下的延迟会显著上升**——这正是 spin/snooze 必须区分清楚的原因。

---

## 5. 综合实践

把本讲四块知识串起来，完成一个**「优化项归因」小研究**：

1. **基线测量**：按 4.4.4 写出 MPMC 吞吐 benchmark，记录 `ArrayQueue::new(1024)` 在 4+4 线程下的 ops/s（多次取中位数）。
2. **逐项剥离**：本地 fork 出三个变体，逐一测量并填表：
   - 变体 A：仅去掉所有 `Backoff`（spin/snooze 全删）。
   - 变体 B：仅把 `CachePadded<AtomicUsize>` 换成裸 `AtomicUsize`。
   - 变体 C：同时去掉 Backoff 与 CachePadded。
3. **归因分析**：用 baseline − A 估算 Backoff 的贡献，baseline − B 估算 CachePadded 的贡献，再观察 C 是否约为两者之和（说明二者独立）还是更糟（说明有交互）。
4. **CAS 路径定位**：在变体 A 上，特别关注「CAS 失败次数」——可在 fork 里给 `compare_exchange_weak` 的 `Err` 分支加一个 `AtomicUsize` 计数器，对比有无 Backoff 时失败次数与每次失败耗时的变化，解释 Backoff 是通过「减少失败次数」还是「降低每次失败成本」在起作用（或两者兼有）。
5. **结论**：写一段 3～5 句的总结，说明在**你的机器**上哪项优化收益更大，以及线程数从 2 增到 16 时两项收益如何变化。

> 所有数值结论标注「待本地验证」。仓库不带 benches 目录，本实践需你自行创建临时工程，不要把 fork 改动写回 `crossbeam-queue` 源码。

---

## 6. 本讲小结

- **伪共享**是无锁队列的隐形性能杀手：`head`（消费者写）与 `tail`（生产者写）若同处一条缓存行，每次写入都会互相作废对方核心的缓存。
- **`CachePadded`** 用 `repr(align(N))` + 自动填充，让每个被包裹的原子值独占缓存行；它纯属内存布局优化，不改语义，靠 `Deref`/`DerefMut` 透明访问。
- **`Backoff::spin`** 是 CAS 失败后的指数忙等：空转 `2^min(step,6)` 次 PAUSE，**不让出时间片**，适合「抢输了、资源马上会再可用」。
- **`Backoff::snooze`** 是等待他人推进时的退避：先忙等，超过 `SPIN_LIMIT` 后 `thread::yield_now()` 主动让出，适合「等别人写值/装块/更新 stamp」。
- **选择口诀**：「抢输了 spin，等别人 snooze」——两个队列在所有调用点上严格遵守这条规则，且常在同一函数内混用两者。
- 两类优化都是「用空间/延迟换吞吐」的折中：`CachePadded` 付内存，`Backoff`（snooze）付偶尔的调度开销，换来的都是高并发下数倍的吞吐。

## 7. 下一步学习建议

- **继续专家层**：下一讲 u4-l3「unsafe 的安全性论证与 MaybeUninit」会剖析 `unsafe impl Send/Sync`、`MaybeUninit` 的 write/read/assume_init 与 `needs_drop` 守卫，与本讲的缓存行/退避配合，构成「正确且快」的完整图景。
- **横向阅读**：直接精读 [`crossbeam-utils/src/cache_padded.rs`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/cache_padded.rs) 与 [`backoff.rs`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/backoff.rs) 全文，对照本讲的行号定位加深理解。
- **工具实践**：学习 `perf`（Linux）/Instruments（macOS）观察 cache-miss 与调度事件，把本讲的「待本地验证」数值真正测出来。
- **延展阅读**：了解 MESI 缓存一致性协议与 Intel 的 spatial prefetcher，理解「为什么 x86-64 要按 128 而非 64 对齐」的硬件根因。
