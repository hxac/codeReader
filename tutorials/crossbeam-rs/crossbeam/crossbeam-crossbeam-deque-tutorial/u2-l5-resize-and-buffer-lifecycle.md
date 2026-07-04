# 缓冲区扩缩容与生命周期：resize 与 reserve

## 1. 本讲目标

前几讲里我们一直把 `resize` 当作一个「黑盒」：`push` 满了就调它翻倍，`pop` 到很少就调它缩半，至于它内部到底做了什么、旧缓冲区怎么处理、为什么不会和正在偷取的线程打架——全部跳过。本讲就专门打开这个黑盒。

读完本讲，你应该能够：

1. 看懂 `Worker::resize` 的固定五步：**算窗口 → 分配新 buffer → 逐槽拷贝 → swap 共享指针 + 换本地缓存 → 用 epoch 延迟释放旧 buffer**，并理解为什么拷贝用的是 `copy_nonoverlapping`（位拷贝）而不会 double-drop。
2. 理解 `swap` 旧 buffer 之后**不能立刻释放**的根本原因（别的 stealer 可能正拿着旧指针读槽位），以及 `epoch::pin` + `guard.defer_unchecked` 如何把释放推迟到「所有线程都离开临界区」之后。
3. 看懂 `Worker::reserve` 的容量倍增算法，以及它为什么是「批量偷取」专用的预扩容入口（和 `push` 的逐个扩容不同）。
4. 串起整个动态容量管理策略：`push` 触发 **2× 扩容**、`pop` 触发 **cap/2 缩容**、且容量永远不低于 `MIN_CAP = 64`、永远保持 2 的幂。

本讲**不**深入 crossbeam-epoch 的内部机制（全局 epoch 轮转、垃圾袋结构等，留到 u4-l2），也**不**重复 push/pop 主流程（已在 u2-l2 讲过）。本讲只聚焦「容量如何变化、旧内存如何安全退役」。

## 2. 前置知识

### 2.1 复习：环形 buffer 与绝对索引

u2-l1 讲过，`Buffer<T>` 是一个容量恒为 **2 的幂** 的环形数组，寻址靠掩码：

\[
\text{slot}(i) = \text{ptr}\big[\,i \,\&\, (\text{cap}-1)\,\big]
\]

`front`/`back` 是**绝对索引**（只增不减，LIFO 的 `back` 会减但不会回绕到 0），队列内容就是 `[front, back)` 这段窗口落到环形槽位上。当 `cap` 改变时，同一个绝对索引 `i` 会落到**不同的物理槽位**（因为掩码变了），这是「扩容后必须重新落位」的关键。

### 2.2 谁能改 buffer？

这张表是本讲正确性的钥匙（u2-l2 已给出，这里只补 buffer 这一列）：

| 字段 | 谁会写 | 谁会读 |
|------|--------|--------|
| `back` | 只有 owner | owner 与所有 stealer |
| `front` | owner（pop）与 stealer（steal） | owner 与所有 stealer |
| `inner.buffer`（共享指针） | **只有 owner**（`resize` 时 `swap`） | stealer（偷取前 `load`） |
| `Worker.buffer`（owner 的 `Cell` 本地缓存） | 只有 owner（`resize` 时 `replace`） | 只有 owner |

关键事实：**只有 owner 线程会换 buffer**。stealer 永远只是 `load` 读出当前 buffer 指针去用，从不换。这就把「换 buffer」这件事限制在单写者，避免了多线程同时换 buffer 的噩梦。

### 2.3 为什么换掉旧 buffer 后不能立刻 `free`？

设想这个时间线：

1. stealer 线程 S 执行 `steal`，`load` 出当前共享 buffer 指针 `B_old`（[src/deque.rs:665](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L665)）。
2. owner 线程此刻调用 `resize`，把共享 buffer `swap` 成 `B_new`，并「想」释放 `B_old`。
3. S 继续用手里那份 `B_old` 指针去 `read(f)` 读槽位（[src/deque.rs:666](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L666)）。

如果第 2 步立刻 `dealloc(B_old)`，第 3 步就是 **use-after-free**。所以旧 buffer 必须「等所有正在用它的人都放手了」才能真释放——这正是 epoch-based memory reclamation（基于 epoch 的内存回收）要解决的问题。本讲只用到它的 API，机制细节留到 u4-l2。

### 2.4 术语速查

- **`#[cold]`**：给函数的优化提示，告诉编译器「这个分支很少执行」，从而把它的代码移出热路径，让 `push`/`pop` 的常规路径更紧凑。
- **`copy_nonoverlapping`**：`ptr` 模块提供的**按字节位拷贝**（bitwise copy），相当于 `memcpy`。它不调用任何构造/析构，只是搬运原始字节。
- **`epoch::pin()`**：进入一个临界区，返回一个 `Guard`；在 guard 存活期间，本线程「被登记为活跃」，他人推迟的回收不会真正执行。
- **`defer_unchecked`**：往当前线程的「垃圾袋」里塞一个闭包，约定「等所有线程都离开临界区后」再执行它（这里是释放旧 buffer）。
- **`guard.flush()`**：主动把本线程垃圾袋里的回收任务尽快结算一批，用于大块内存的及时回收。

## 3. 本讲源码地图

本讲全部内容仍集中在这一个文件：

| 文件 | 本讲涉及的部分 | 作用 |
|------|---------------|------|
| [src/deque.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs) | `Worker::resize`（L289-L322） | 本讲第一主角 |
| 同上 | `Worker::reserve`（L324-L350） | 批量偷取的预扩容 |
| 同上 | `push` 的扩容触发点（L408-L415）、`pop` 的缩容触发点（L480-L483、L533-L539） | 容量策略的三个落点 |
| 同上 | `Buffer::alloc`/`dealloc`（L40-L62）、常量（L17-L23） | 分配/释放与阈值常量 |
| 同上 | `Inner::drop`（L125-L145） | 队列销毁时如何收尾 |
| 同上 | `Stealer::steal_batch_with_limit` 中对 `dest.reserve` 的调用（L781） | reserve 的典型调用方 |
| tests/lifo.rs | `stampede` / `destructors` | 压力测试与析构测试（实践参考） |

## 4. 核心概念与源码讲解

### 4.1 容量策略全景：何时扩、何时缩

#### 4.1.1 概念说明

在进入 `resize` 内部之前，先看清「容量什么时候会变」这张全局图。Chase-Lev 队列的容量是**动态**的：忙的时候自动变大，闲下来自动缩回去，但永远满足两条铁律：

1. **恒为 2 的幂**——这是 `at()` 用 `index & (cap-1)` 做环形寻址的前提（[src/deque.rs:65-70](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L65-L70)）。
2. **下限是 `MIN_CAP = 64`**（[src/deque.rs:17-18](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L17-L18)）——再怎么缩也不会低于它。

容量的变化只有两种方向、三个触发点：

| 触发点 | 方向 | 条件 | 新容量 |
|--------|------|------|--------|
| `push` 写满 | 扩容 | `len >= cap` | `2 * cap` |
| `pop`（FIFO） | 缩容 | `cap > MIN_CAP && len <= cap/4` | `cap / 2` |
| `pop`（LIFO） | 缩容 | `cap > MIN_CAP && len < cap/4` | `cap / 2` |
| 批量偷取写入前 | 扩容 | `cap - len < reserve_cap` | 倍增至刚好够 |

注意缩容的「四分之一」阈值：只有当队列长度掉到容量的 1/4 以下时才缩一半。这是为了避免**反复抖动（thrashing）**——如果一缩就立刻被填满、一满就立刻扩，容量会不停来回跳。1/4 这个滞后量保证了一次缩容后，至少还有 cap/2 的余量可以继续 push，不会马上又触发扩容。

#### 4.1.2 核心流程

```text
容量生命周期：

  创建：new_fifo/new_lifo → Buffer::alloc(MIN_CAP=64)

  push 路径：
    len = back - front
    若 len >= cap:           # 写满了
        resize(2 * cap)      # 翻倍
        重读本地 buffer 缓存

  pop 路径（取走一个任务后）：
    若 cap > MIN_CAP 且 len <= cap/4（FIFO）/ len < cap/4（LIFO）:
        resize(cap / 2)      # 缩半，但不低于 MIN_CAP

  批量偷取路径（stealer 把任务倒进 dest 之前）：
    dest.reserve(batch_size)
      → 若 cap - len < batch_size: 倍增 cap 直到够，再 resize
```

#### 4.1.3 源码精读：三个触发点

**`push` 中的扩容**——满了就翻倍，然后**必须重读本地的 `buffer` 缓存**，因为 `resize` 已经把 `Cell` 里的旧 buffer 换成了新的：

[src/deque.rs:408-L415](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L408-L415)（队列满时翻倍扩容，并刷新本地 buffer 缓存）

```rust
// Is the queue full?
if len >= buffer.cap as isize {
    // Yes. Grow the underlying buffer.
    unsafe {
        self.resize(2 * buffer.cap);
    }
    buffer = self.buffer.get();
}
```

> 这一行 `buffer = self.buffer.get();` 极容易看漏。如果漏掉它，后面 `buffer.write(b, ...)` 会写到**已经退役的旧 buffer** 上，而别的线程看到的是新 buffer——数据就「丢」在了旧 buffer 里。

**`pop` 的 FIFO 缩容**——`fetch_add` 抢占成功并读到任务之后，检查是否该缩：

[src/deque.rs:480-L483](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L480-L483)（FIFO pop 后，长度降到容量 1/4 时缩半）

```rust
// Shrink the buffer if `len - 1` is less than one fourth of the capacity.
if buffer.cap > MIN_CAP && len <= buffer.cap as isize / 4 {
    self.resize(buffer.cap / 2);
}
```

注意这里的 `len` 是 `pop` 函数**开头**就算好的「pop 前长度」（[src/deque.rs:456](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L456)），所以注释说「`len - 1`（pop 后长度）小于容量 1/4」。条件 `len <= cap/4` 近似等价于「pop 后长度 < cap/4」。

**`pop` 的 LIFO 缩容**——在 `else` 分支里（即不是最后一个任务时），用**减 1 后**的 `len` 判断：

[src/deque.rs:533-L539](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L533-L539)（LIFO pop 后，长度降到容量 1/4 时缩半）

```rust
// Shrink the buffer if `len` is less than one fourth of the capacity.
if buffer.cap > MIN_CAP && len < buffer.cap as isize / 4 {
    unsafe {
        self.resize(buffer.cap / 2);
    }
}
```

两个细节：① LIFO 这里用的是 `<` 而 FIFO 用的是 `<=`，属于实现上的微调，不影响「1/4 阈值」的整体语义；② 两处都有 `buffer.cap > MIN_CAP` 这个守卫——只有当前容量严格大于 64 才会缩，因此 `cap/2` 的最小结果是 `128/2 = 64 = MIN_CAP`，**容量永远不会跌破 64**。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**，目标是确认「2 的幂」与「下限 64」两条铁律在代码里处处成立。

1. **实践目标**：验证所有调用 `resize` 的入口都传入 2 的幂，且缩容不会跌破 `MIN_CAP`。
2. **操作步骤**：
   - 打开 [src/deque.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs)，搜索三处 `self.resize(`（分别在 push L412、pop FIFO L482、pop LIFO L536）和一处 `self.resize(new_cap)`（reserve L346）。
   - 逐个确认入参：`2 * buffer.cap`、`buffer.cap / 2`、`new_cap`（由 `cap * 2` 反复倍增得到）。
   - 再看 `Buffer::alloc` 开头的断言 [src/deque.rs:42](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L42)：`debug_assert_eq!(cap, cap.next_power_of_two())`。
3. **需要观察的现象**：四个入口的入参在数学上都保持 2 的幂；缩容守卫 `cap > MIN_CAP` 保证最小结果为 64。
4. **预期结果**：你能用自己的话回答「为什么 `at()` 可以用 `index & (cap-1)` 取模」——因为 cap 恒为 2 的幂，`cap-1` 是一串全 1 的低位掩码。
5. 待本地验证：若想看运行时容量变化，可在 `resize` 第一行临时加一句 `eprintln!("resize cap={new_cap} len={}", b.wrapping_sub(f));`（仅用于本地观察，**不要提交**），再跑 4.4 的实践测试。

#### 4.1.5 小练习与答案

**练习 1**：假设当前 `cap = 64`，队列里有 64 个任务（满了）。此时连续 `push` 2 个任务，容量会经历怎样的变化？

**答案**：第 1 次 `push` 时 `len(64) >= cap(64)` 成立，触发 `resize(128)`，容量变 128，写入第 65 个任务；第 2 次 `push` 时 `len(65) < cap(128)`，不再扩容，直接写入。最终容量为 128。

**练习 2**：为什么缩容阈值用「1/4」而不是「1/2」？

**答案**：为了引入滞后（hysteresis），避免抖动。若缩到 1/2 就立刻缩半，缩完之后队列几乎又满了，下一次 `push` 马上触发扩容，容量会在两个值之间反复跳。用 1/4 阈值缩半后，剩余容量约为当前长度的 2 倍，留出缓冲空间，减少扩缩容频率。

---

### 4.2 Worker::resize：分配、拷贝、swap、延迟回收

#### 4.2.1 概念说明

`resize` 是本讲的绝对主角。它接收一个**新容量**（不是增量），负责把整个活窗口 `[front, back)` 从旧 buffer 搬到新 buffer，然后把共享指针原子地换过去，最后把旧 buffer「挂号」等以后再释放。整个函数被标了 `#[cold]`（[src/deque.rs:290](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L290)），提示编译器这是冷路径，别让它干扰 `push`/`pop` 的热路径优化。

`resize` 只能由 owner 调用（`push`、`pop`、`reserve` 都是 owner 端操作），这一点保证了「换 buffer」永远是单写者。

#### 4.2.2 核心流程

```text
resize(new_cap):                       # #[cold]，仅 owner 调用
  1. b = back.load(Relaxed)            # owner 专用字段，Relaxed 即可
     f = front.load(Relaxed)           # 见 4.2.3 的「为何 Relaxed 安全」
     buffer = 本地 Cell 缓存的旧 buffer

  2. new = Buffer::alloc(new_cap)      # 分配新 buffer（2 的幂）
     for i in [f, b):                  # 逐槽拷贝活窗口
       copy_nonoverlapping(buffer.at(i) → new.at(i), 1)

  3. guard = epoch::pin()              # 进入临界区，登记本线程活跃

  4. self.buffer.replace(new)          # 换 owner 的本地缓存
     old = inner.buffer.swap(new, Release, guard)   # 原子换共享指针，拿到旧值

  5. guard.defer_unchecked(|| old.dealloc())        # 旧 buffer 延迟释放
     if size_of::<T>() * new_cap >= 1024:
        guard.flush()                 # 大块内存：主动尽早回收
```

#### 4.2.3 源码精读

完整函数如下（[src/deque.rs:289-L322](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L289-L322)，分配新 buffer、拷贝活窗口、原子换指针、延迟释放旧 buffer）：

```rust
#[cold]
unsafe fn resize(&self, new_cap: usize) {
    // Load the back index, front index, and buffer.
    let b = self.inner.back.load(Ordering::Relaxed);
    let f = self.inner.front.load(Ordering::Relaxed);
    let buffer = self.buffer.get();

    // Allocate a new buffer and copy data from the old buffer to the new one.
    let new = Buffer::alloc(new_cap);
    let mut i = f;
    while i != b {
        unsafe { ptr::copy_nonoverlapping(buffer.at(i), new.at(i), 1) }
        i = i.wrapping_add(1);
    }

    let guard = &epoch::pin();

    // Replace the old buffer with the new one.
    self.buffer.replace(new);
    let old =
        self.inner
            .buffer
            .swap(Owned::new(new).into_shared(guard), Ordering::Release, guard);

    // Destroy the old buffer later.
    unsafe { guard.defer_unchecked(move || old.into_owned().into_box().dealloc()) }

    // If the buffer is very large, then flush the thread-local garbage in order to deallocate
    // it as soon as possible.
    if mem::size_of::<T>() * new_cap >= FLUSH_THRESHOLD_BYTES {
        guard.flush();
    }
}
```

逐段拆解：

**① 读取窗口边界（[L293-L295](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L293-L295)）**。`back` 是 owner 专用字段，`Relaxed` 读到自己最新的写没问题；`front` 会被 stealer 推进，这里也用 `Relaxed` 看似「冒险」，但其实是**故意取一个偏旧的快照**——见下面的安全性说明。

**② 分配 + 拷贝（[L298-L303](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L298-L303)）**。`Buffer::alloc(new_cap)` 分配新 buffer（内部用 `Box<[MaybeUninit<T>]>`，[src/deque.rs:40-L52](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L40-L52)）。然后从 `f` 到 `b` 逐个 `copy_nonoverlapping`。

> **为什么 `front` 读 `Relaxed` 也安全？** 因为 `front` 单调递增。读到偏旧（偏小）的 `f`，意味着拷贝区间 `[f, b)` 比「真实活窗口」**多覆盖了几个早已被消费的低位槽位**。这些多拷贝的字节会落进新 buffer 的「死槽位」里——而 `Inner.front` 仍是真实的较大值，后续 nobody 会再去读那些低于 `front` 的索引，所以多拷贝无害。读到偏旧的 `f` 只会**多拷**不会**漏拷**，安全性成立。

> **为什么 `copy_nonoverlapping` 不会 double-drop？** 这是本讲最微妙的一点。拷贝后，旧 buffer 和新 buffer 的对应槽位里**存着相同的字节**，看起来像「同一个 `T` 有两份」。但关键在于旧 buffer 的释放路径：`dealloc`（[src/deque.rs:54-L62](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L54-L62)）只是 `Box::from_raw` 把整块内存当 `Box<[MaybeUninit<T>]>` 释放——**`MaybeUninit<T>` 的 drop 是 no-op，不会调用 `T::drop`**。所以旧 buffer 释放时不会跑 `T` 的析构，`T` 在语义上只「活」在新 buffer 里，是一次语义 move（用位拷贝实现）。真正会调用 `T::drop` 的地方是 `Inner::drop`（见 4.2.4）和正常 `pop`/`steal` 的 `assume_init`。

**③ `epoch::pin()`（[L305](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L305)）**。进入临界区。这一步同时附带了一个 `SeqCst` fence（epoch 内部行为），把后续的 `swap` 与 stealer 的 `load` 正确同步。

**④ 换指针 + 换缓存（[L308-L312](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L308-L312)）**。先 `self.buffer.replace(new)` 更新 owner 本地 `Cell` 缓存；再用 `inner.buffer.swap(..., Release, guard)` 原子地把**共享**指针换成 `new`，返回值 `old` 就是退役的旧 buffer。`Release` 序保证：stealer 之后用 `Acquire` 读到新 buffer 时，新 buffer 里拷贝好的数据对它全部可见。

**⑤ 延迟释放 + 按需 flush（[L315-L321](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L315-L321)）**。`guard.defer_unchecked(move || old...dealloc())` 把「释放旧 buffer」这个动作塞进本线程的垃圾袋，约定等所有线程都离开临界区后再执行——这就解决了 2.3 节的 use-after-free 风险。

  最后一段 `if mem::size_of::<T>() * new_cap >= FLUSH_THRESHOLD_BYTES` 是个内存占地优化。`FLUSH_THRESHOLD_BYTES = 1 << 10 = 1024`（[src/deque.rs:21-L23](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L21-L23)）。当退役的 buffer「很大」（按 `T 的大小 × 容量` 估算）时，立刻 `guard.flush()` 触发一次本线程垃圾结算，让这块大内存尽快真正归还给分配器，而不是在垃圾袋里攒着占用内存峰值。对小 buffer 不 flush，是为了避免频繁结算拖慢热路径。

#### 4.2.4 队列销毁时的收尾：Inner::drop

`resize` 负责运行期的扩缩容，而队列彻底销毁时（`Arc<Inner>` 引用计数归零），还有最后一个 `buffer` 要释放，外加丢弃残留任务。这在 `Inner::drop` 里（[src/deque.rs:125-L145](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L125-L145)，单线程场景下逐个 drop 残留任务并释放当前 buffer）：

```rust
impl<T> Drop for Inner<T> {
    fn drop(&mut self) {
        let b = *self.back.get_mut();
        let f = *self.front.get_mut();
        unsafe {
            let buffer = self.buffer.load(Ordering::Relaxed, epoch::unprotected());
            let mut i = f;
            while i != b {
                buffer.deref().at(i).drop_in_place();   // 丢弃残留任务
                i = i.wrapping_add(1);
            }
            buffer.into_owned().into_box().dealloc();   // 释放当前 buffer
        }
    }
}
```

两个要点：
- `drop` 发生在 `Arc` 计数归零时，必然是**单线程**场景，所以用 `epoch::unprotected()`（不进入临界区，直接操作）是安全的。
- 它只释放「当前那一个」buffer——历史上被 `resize` 换掉的旧 buffer 早已在各自的 `defer_unchecked` 里被 epoch 回收掉了，`Inner::drop` 不必关心它们。这就和 `resize` 形成了清晰的分工：**运行期退役的 buffer 归 epoch 管，最终存活的那一个归 `Drop` 管**。

#### 4.2.5 代码实践

**源码阅读 + 时序分析实践**：在注释里讲清「为什么 swap 之后旧 buffer 不能立刻释放」。

1. **实践目标**：把 `resize` 的第 ④⑤ 步与 `Stealer::steal` 的读槽位路径对应起来，画出同步关系。
2. **操作步骤**：
   - 重读 `resize` 的 [L309-L315](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L309-L315)：`swap`（Release）拿旧 buffer、`defer_unchecked` 推迟释放。
   - 重读 `Stealer::steal` 的 [L665-L670](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L665-L670)：stealer 用 `guard`（即 `epoch::pin`）`load` 出 buffer 指针并 `read` 槽位。
   - 画出时序：stealer 在自己的 `guard` 存活期间持着旧 buffer 指针；owner `swap` 后 `defer`；epoch 机制保证只要还有任何线程的 `guard` 指向那个旧 buffer 所属的 epoch，`defer` 的闭包就不执行。
3. **需要观察的现象**：stealer 的 `read` 完成并 `drop(guard)` 之后，旧 buffer 才有可能被真正 `dealloc`。
4. **预期结果**：你能解释「owner 的 `defer_unchecked`」与「stealer 的 `epoch::pin`」如何通过 epoch 编号配对，避免 stealer 读到已释放内存。机制细节（epoch 轮转、GC 触发条件）留到 u4-l2。
5. 待本地验证：完整运行机制建议用 Miri 跑 `tests/lifo.rs` 的 `stampede` 验证无 use-after-free（见 4.4）。

#### 4.2.6 小练习与答案

**练习 1**：`resize` 里 `front` 用 `Ordering::Relaxed` 读取，万一读到一个非常旧的 `front`（比真实值小很多），会发生什么？会丢任务吗？

**答案**：不会丢任务。`front` 单调递增，读到偏小的值只是让拷贝区间 `[f, b)` 多覆盖几个已消费的低位槽位；这些多拷贝的字节落进新 buffer 中低于真实 `front` 的死槽位，之后无人读取。真正决定活窗口的是 `Inner.front`（仍是真实值），它没有被 `resize` 修改。所以最坏情况只是多拷几个字节，不会漏拷任何活任务。

**练习 2**：`copy_nonoverlapping` 之后，同一个 `T` 的字节同时存在于新旧两个 buffer。为什么这不算「双重所有权」、不会 double-drop？

**答案**：因为旧 buffer 的释放路径 `Buffer::dealloc` 是把内存当作 `Box<[MaybeUninit<T>]>` 整块释放的，而 `MaybeUninit<T>` 的析构是 no-op，**不会调用 `T::drop`**。所以旧 buffer 释放时只归还内存、不跑 `T` 的析构；`T` 在语义上只活在新 buffer 里，由后续 `pop`/`steal` 的 `assume_init` 或 `Inner::drop` 的 `drop_in_place` 来负责析构。这是一次用位拷贝实现的语义 move。

**练习 3**：`resize` 末尾的 `if mem::size_of::<T>() * new_cap >= FLUSH_THRESHOLD_BYTES { guard.flush(); }`，为什么对「大 buffer」才 flush？

**答案**：`flush` 会立刻结算本线程垃圾袋，能尽快回收大块内存、压低内存峰值；但结算本身有开销。小 buffer 占用不大，攒在垃圾袋里等下次自然回收即可，不值得为它付 flush 的代价。所以用一个字节阈值（1024）区分「值得立刻回收的大块」和「可以等等的小块」。

---

### 4.3 Worker::reserve：批量偷取的预扩容

#### 4.3.1 概念说明

`push` 的扩容是「写一个、检查一次、满了翻倍」——因为一次只写一个任务。但**批量偷取**不一样：它一次要把最多 `MAX_BATCH = 32`（[src/deque.rs:19-L20](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L19-L20)）个任务连续写进目的 worker 的 `back` 端，**然后才统一发布 `back`**（见 u2-l4）。如果目的 worker 的 buffer 装不下这一整批，写入就会越界、覆盖尚未消费的任务。

所以批量偷取在写之前需要一次性预留 `batch_size` 个位置——这就是 `reserve` 的职责。它和 `push` 的区别是：**按需倍增到「刚好够」**，而不是无脑翻一倍。

注意 `reserve` 是个**私有方法**（没有 `pub`，[src/deque.rs:326](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L326)），只在模块内被 `Stealer::steal_batch_with_limit`、`Injector::steal_batch_with_limit` 等调用，调用形式是 `dest.reserve(batch_size)`——而 `dest` 永远是**调用线程自己拥有的 worker**（因为 `Worker: !Sync`，别的线程拿不到 `&Worker`）。所以 `reserve` 仍然是 owner 端操作，符合「只有 owner 改 buffer」的不变式。

#### 4.3.2 核心流程

```text
reserve(reserve_cap):                 # 仅 owner 调用（dest 的 owner）
  if reserve_cap == 0: return         # 无需预留
  b = back.load(Relaxed)
  f = front.load(SeqCst)              # 取较新快照做容量决策
  len = b - f
  cap = 本地 buffer.cap
  if cap - len < reserve_cap:         # 剩余空间不够
      new_cap = cap * 2
      while new_cap - len < reserve_cap:
          new_cap *= 2                # 反复倍增，直到 new_cap - len >= reserve_cap
      resize(new_cap)
```

容量倍增的数学表达：求最小的 `k ≥ 1` 使得

\[
\text{cap}\cdot 2^{k} - \text{len} \;\geq\; \text{reserve\_cap}
\]

然后 `resize(cap · 2^k)`。由于 `cap` 和 `2^k` 都是 2 的幂，乘积仍是 2 的幂，满足环形寻址前提。

#### 4.3.3 源码精读

[src/deque.rs:324-L350](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L324-L350)（按需倍增容量，确保能再装下 `reserve_cap` 个任务而不触发中途扩容）：

```rust
fn reserve(&self, reserve_cap: usize) {
    if reserve_cap > 0 {
        // Compute the current length.
        let b = self.inner.back.load(Ordering::Relaxed);
        let f = self.inner.front.load(Ordering::SeqCst);
        let len = b.wrapping_sub(f) as usize;

        // The current capacity.
        let cap = self.buffer.get().cap;

        // Is there enough capacity to push `reserve_cap` tasks?
        if cap - len < reserve_cap {
            // Keep doubling the capacity as much as is needed.
            let mut new_cap = cap * 2;
            while new_cap - len < reserve_cap {
                new_cap *= 2;
            }

            // Resize the buffer.
            unsafe {
                self.resize(new_cap);
            }
        }
    }
}
```

逐点说明：

- **`reserve_cap == 0` 直接返回**：批量大小可能算出来是 0（极少见），此时不必动 buffer。
- **`back` 用 `Relaxed`、`front` 用 `SeqCst`**：`back` 是 owner 专用，`Relaxed` 够；`front` 用 `SeqCst` 取一个**较新**快照，以便准确判断剩余空间 `cap - len`。即便读到偏旧的 `front`（偏小→`len` 偏大→`cap-len` 偏小），也只是**保守地多扩一点**，不会出错。
- **倍增循环**：先 `new_cap = cap * 2`（至少翻一倍），若仍不够就继续 `* 2`。例如 `cap=64`、`len=60`、要预留 `reserve_cap=32`：第一次 `new_cap=128`，`128-60=68 ≥ 32`，停止，`resize(128)`。
- **复用 `resize`**：`reserve` 自己不碰指针，只算出目标容量，把脏活全交给 `resize`。这保证了「换 buffer + 延迟回收」的逻辑只有一处实现。

**调用方示例**：`Stealer::steal_batch_with_limit` 在算出 `batch_size` 后、写槽位之前调用它（[src/deque.rs:780-L781](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L780-L781)）：

```rust
let batch_size = cmp::min((len as usize).div_ceil(2), limit);
dest.reserve(batch_size);
```

Injector 的批量偷取也以同样方式调用 `dest.reserve(batch_size)`（[src/deque.rs:1665-L1666](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1665-L1666) 与 [L1868-L1869](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1868-L1869)）。

#### 4.3.4 代码实践

**跟踪调用链实践**：确认 `reserve` 在批量偷取里被调用的时机。

1. **实践目标**：验证 `reserve` 总是在「写目的 buffer 之前」被调用，且 `dest` 是调用线程自己的 worker。
2. **操作步骤**：
   - 在 [src/deque.rs:746](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L746) `steal_batch_with_limit` 里，顺着 `batch_size` 的计算（L780）→ `dest.reserve`（L781）→ `dest_buffer = dest.buffer.get()`（L785）→ 写循环（L797 起）的顺序读一遍。
   - 同样在 Injector 的 `steal_batch_with_limit`（[L1601](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1601)）里找到对应的 `dest.reserve(batch_size)`（L1666）。
3. **需要观察的现象**：`reserve` 一定先于任何 `dest_buffer.write(...)` 执行。
4. **预期结果**：你能回答「为什么批量偷取不能像 `push` 那样边写边检查满」——因为批量写入期间 `back` 还没发布，中间若触发扩容会破坏「先写槽位再推 back」的发布顺序；必须一次性预留好。
5. 待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`cap = 64`、`len = 50`，要 `reserve(20)`。`new_cap` 最终是多少？

**答案**：`cap - len = 14 < 20`，需要扩。`new_cap` 从 `128` 开始：`128 - 50 = 78 ≥ 20`，停止。最终 `resize(128)`。

**练习 2**：为什么 `reserve` 不像 `push` 那样「满了才扩一倍」，而是要算到「刚好够」？

**答案**：批量偷取一次写入多达 `MAX_BATCH=32` 个任务，若用 push 的「一次扩一倍」语义，可能在写入中途仍然不够（例如 cap=64、len=60、要写 32 个，翻一倍到 128 后 128-60=68≥32 够了；但若 cap=64、len=40、要写 32 个，不扩的话 64-40=24<32 会越界）。`reserve` 用倍增循环一次性算出「至少够装下整批」的容量，保证后续连续写入绝不会中途撞到容量墙，从而保持「先写完所有槽位、最后统一 Release store back」的发布顺序不被破坏。

---

### 4.4 综合跑通：扩容 → 缩容 → 全量校验

#### 4.4.1 概念说明

前面三个模块分别讲了「触发点」「resize 内部」「reserve」。这个小节把它们串起来：构造一个会**同时触发扩容和缩容**的场景，验证整条链路下队列仍然正确——顺序对、无丢失、无重复。

这是 `resize` 正确性最直接的端到端检验：如果在扩缩容过程中丢了任务、重复了任务、或顺序乱了，这个测试会立刻失败。

#### 4.4.2 核心流程

```text
1. LIFO Worker，初始 cap = MIN_CAP = 64
2. push 0..128        # 第 64 次 push 时 cap=64 满了 → resize(128)；之后继续 push 到 128
3. 连续 pop 直到 None  # 长度掉到 cap/4=32 以下时 → resize(64) 缩回去
4. 收集所有 pop 结果，断言：
   - 数量 = 128（无丢失）
   - 去重后仍 = 128（无重复）
   - 顺序是 LIFO（128-1=127, 126, ..., 0）
```

#### 4.4.3 源码精读（测试依据）

tests 目录里没有专门针对 resize 的单测，但 `stampede` 和 `destructors`（[tests/lifo.rs:104-L143](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/tests/lifo.rs#L104-L143) 等）在多线程下大规模 push/pop，**隐式地反复触发扩缩容**并校验无丢失无重复。下面的示例代码是单线程版本，专门把扩缩容做成显式、可控的回归测试。

#### 4.4.4 代码实践

**实践目标**：写一个单线程测试，显式触发扩容（push 超过 64）和缩容（pop 到很少），校验队列在容量变化下仍然正确。

把下面这段**示例代码**放进一个独立的 cargo 测试项目（依赖 `crossbeam-deque = "0.8"`），或临时加到 `tests/lifo.rs` 末尾本地运行（**不要提交到源码**）：

```rust
// 示例代码：验证 resize 扩缩容下的正确性（单线程）
use crossbeam_deque::Worker;

#[test]
fn resize_grow_and_shrink_lifo() {
    let w = Worker::new_lifo();          // 初始 cap = MIN_CAP = 64
    let n = 128usize;                    // 超过 64，必然触发扩容

    // 1. 连续 push 0..128：第 64 次时 len>=cap 触发 resize(128)
    for i in 0..n {
        w.push(i);
    }

    // 2. 连续 pop 直到空：长度跌破 cap/4=32 时触发 resize(64) 缩回
    let mut popped = Vec::new();
    while let Some(v) = w.pop() {
        popped.push(v);
    }

    // 3. 断言：数量正确（无丢失）
    assert_eq!(popped.len(), n, "丢失了任务");

    // 4. 断言：无重复（去重后数量不变）
    let mut sorted = popped.clone();
    sorted.sort_unstable();
    sorted.dedup();
    assert_eq!(sorted.len(), n, "出现了重复任务");

    // 5. 断言：LIFO 顺序（后进先出：127, 126, ..., 0）
    let expected: Vec<usize> = (0..n).rev().collect();
    assert_eq!(popped, expected, "LIFO 顺序错误");

    // 6. 队列已空
    assert!(w.is_empty());
    assert_eq!(w.len(), 0);
}
```

1. **操作步骤**：在仓库根目录新建一个临时 crate，或在 `tests/lifo.rs` 末尾粘贴该测试，运行 `cargo test resize_grow_and_shrink_lifo`（用 Miri 验证内存安全：`cargo +nightly miri test resize_grow_and_shrink_lifo`）。
2. **需要观察的现象**：测试通过；若想观察容量变化，可在 `src/deque.rs` 的 `resize` 第一行临时加 `eprintln!("resize -> {new_cap}")`（仅本地，不提交），会看到形如 `resize -> 128`（扩容）随后 `resize -> 64`（缩容）的输出。
3. **预期结果**：扩容与缩容各至少发生一次，且所有断言通过——证明在容量动态变化下任务既不丢失也不重复、LIFO 顺序保持正确。
4. **如果用 Miri**：还能额外验证 `resize` 的 `copy_nonoverlapping` 与 epoch 延迟释放没有内存错误（无 use-after-free、无 double-free）。待本地验证（Miri 较慢，可先把 `n` 调小到 80）。

#### 4.4.5 小练习与答案

**练习 1**：把上面测试里的 `n` 从 128 改成 65（刚好只多一个），扩容还会发生吗？缩容呢？

**答案**：扩容会发生——第 64 次 push 时 `len(64) >= cap(64)` 触发 `resize(128)`，写入第 65 个。缩容也会发生——连续 pop 时，当 `len` 掉到 `<= 128/4 = 32` 时触发 `resize(64)` 缩回。所以即便只多一个，扩缩容各至少一次。

**练习 2**：如果把 `Worker::new_lifo()` 换成 `Worker::new_fifo()`，上面第 5 步的顺序断言要怎么改？

**答案**：FIFO 从 `front` 端出队，顺序是先进先出，即 `0, 1, 2, ..., 127`。应改为 `let expected: Vec<usize> = (0..n).collect();`（去掉 `.rev()`）。其余断言（数量、去重、空）不变。

---

## 5. 综合实践

把本讲三个最小模块（触发点、`resize`、`reserve`）串成一个**端到端的小任务**：模拟「worker 被人批量灌入任务、自己再慢慢消费」的完整生命周期，观察容量随之起伏。

任务描述：

1. 创建一个 LIFO `Worker` `w` 和它的 `Stealer` `s`。
2. 用 `w` 连续 `push` 100 个 `i32`（触发一次扩容：64→128）。
3. 用 `s.steal_batch_with_limit(&w, 32)` 从 `w` 偷一批（不超过 32 个）**到 `w` 自己**——等等，这会触发 `Arc::ptr_eq` 短路（[src/deque.rs:748](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L748)）。所以正确做法是**另建一个目的 worker** `w2`，用 `s.steal_batch_with_limit(&w2, 32)`，观察 `w2.reserve` 被调用后 `w2` 是否扩容。
4. 然后分别把 `w` 和 `w2` 全部 `pop` 空（触发缩容），统计两边取出的任务总数，断言 = 100（无丢失）。

参考实现（**示例代码**）：

```rust
// 示例代码：综合实践——扩容、批量偷取预扩容、缩容的完整生命周期
use crossbeam_deque::{Steal, Worker};

fn main() {
    let w = Worker::new_lifo();          // cap = 64
    let s = w.stealer();

    // 1. push 100 个 → 在第 64 次触发 resize(128)
    for i in 0..100i32 {
        w.push(i);
    }

    // 2. 偷一批进 w2（演示 reserve 被调用）
    let w2 = Worker::new_fifo();
    match s.steal_batch_with_limit(&w2, 32) {
        Steal::Success(()) => println!("批量偷取成功，w2.len() = {}", w2.len()),
        other => println!("批量偷取结果: {:?}", other),
    }

    // 3. 各自消费干净
    let mut total = 0;
    while w.pop().is_some() { total += 1; }
    while w2.pop().is_some() { total += 1; }

    println!("共取回 {total} 个任务");
    assert_eq!(total, 100, "任务总数必须等于 push 的 100 个");
    println!("OK: 无丢失、无重复");
}
```

跑通后，你可以回答这几个综合问题：

- `w` 的容量经历了 `64 → 128 →（消费后）64` 的怎样的起伏？
- `w2` 作为全新 worker（cap=64），被灌入一批 ≤32 个任务时，为什么 `reserve` **不会**触发它的扩容？（提示：`cap - len = 64 - 0 = 64 ≥ 32`，本就够装。）
- 如果把第 2 步的 `limit` 改成 `usize::MAX`，`w2` 一次会被灌入多少个？会触发 `w2` 的扩容吗？（提示：批量大小受「偷一半」约束，`min(len.div_ceil(2), limit)`；当 `w` 还剩很多时，`w2` 可能被灌入 50 个，仍 ≤ 64，不扩容。）

> 待本地验证：以上输出与容量变化建议在本地实际运行确认；如需观察 `reserve` 真正触发扩容，可先把 `w2` 预先填满（`for i in 0..60 { w2.push(i); }`）再偷，使 `64 - 60 = 4 < batch_size` 成立。

## 6. 本讲小结

- **三个触发点**：`push` 满时 `resize(2*cap)` 翻倍；`pop` 后长度掉到 `cap/4` 以下时 `resize(cap/2)` 缩半（FIFO 用 `<=`、LIFO 用 `<`）；批量偷取写目的 worker 前用 `reserve` 按需倍增。容量恒为 2 的幂、永不低于 `MIN_CAP=64`。
- **`resize` 五步**：读窗口（`front` 用 `Relaxed` 取偏旧快照只多拷不漏拷）→ `Buffer::alloc` → `copy_nonoverlapping` 逐槽拷贝（位拷贝，靠旧 buffer 的 `dealloc` 不跑 `T::drop` 避免 double-drop）→ `epoch::pin` 后 `swap` 共享指针（Release）+ 换本地缓存 → `defer_unchecked` 延迟释放旧 buffer，大块时 `guard.flush()` 尽早回收。
- **核心安全保证**：`swap` 旧 buffer 后**不立即释放**，因为 stealer 可能正持旧指针读槽位；epoch 机制把释放推迟到所有线程离开临界区之后，杜绝 use-after-free。
- **`reserve` 的角色**：批量偷取专用的「一次性预留」入口，用倍增循环算出「刚好够装整批」的容量，保证连续写入不会中途撞墙、不破坏「先写槽位再统一发布 back」的顺序；它复用 `resize` 干脏活。
- **分工清晰**：运行期退役的旧 buffer 归 epoch 管（`defer_unchecked`），队列销毁时存活的最后一个 buffer 归 `Inner::drop` 管（`epoch::unprotected` + `drop_in_place` + `dealloc`）。
- **`#[cold]`** 把 `resize` 移出热路径，让 `push`/`pop` 的常规路径保持紧凑。

## 7. 下一步学习建议

本讲把 `resize` 的「做了什么」讲透了，但刻意把 epoch 机制当黑盒。接下来推荐：

1. **u4-l2（无锁内存回收：crossbeam-epoch 与 Buffer 生命周期）**：深入 `epoch::pin` / `defer_unchecked` / `guard.flush` 背后的全局 epoch 轮转、垃圾袋结算与 GC 触发条件，回答「旧 buffer 到底什么时候、由谁真正释放」。
2. **u4-l1（内存序与 volatile hack 深入）**：横向串讲全文件的 `Acquire/Release/SeqCst` 配对，包括 `resize` 里 `swap(Release)` 与 stealer `load(Acquire)` 如何建立 happens-before。
3. **回顾 u2-l3 / u2-l4**：带着本讲对 `resize` 的理解，重读 `Stealer::steal` 与 `steal_batch` 里「load buffer → 读槽 → CAS front → 再校验 buffer 未被换」的两步偷取，理解它如何兼容 owner 随时可能发生的 `resize`。
4. **动手延伸**：在 4.4 的测试基础上，加一个多线程版本（参考 `tests/lifo.rs` 的 `stampede`），用 `AtomicUsize` 计数所有被取走的任务，验证高并发扩缩容下仍然无丢失无重复。
