# Buffer 与 Inner：环形缓冲区数据结构

## 1. 本讲目标

本讲是进入「Chase-Lev work-stealing 队列实现」的第一讲。前面你已经会使用 `Worker` 的 `push`/`pop`、`Stealer` 的 `steal`、以及 `Injector`（见 u1-l3、u1-l4），但那时我们刻意把「底层是怎么存的」当成了黑盒。

学完本讲，你应当能够：

- 说清 `Buffer<T>` 是什么、为什么容量必须是 **2 的幂**，以及 `at(index)` 如何用 `index & (cap-1)` 做 O(1) 取模实现环形寻址；
- 理解 `Buffer<T>` 为什么被标记为 `Copy`、为什么它的 `Drop` **不会释放内存**，以及 `write`/`read` 为什么用 `ptr::write_volatile`/`read_volatile`；
- 掌握 `Inner<T>` 的三个字段 `front`/`back`（`AtomicIsize`）与 `buffer`（`CachePadded<Atomic<Buffer<T>>>`）各自的职责，以及 `Inner::drop` 如何善后；
- 知道 `CachePadded` 在这里的作用：**防止伪共享（false sharing）**；
- 记住三个常量 `MIN_CAP=64`、`MAX_BATCH=32`、`FLUSH_THRESHOLD_BYTES=1<<10` 的含义与出处。

本讲**只讲数据结构本身**，不深入 `push`/`pop`/`steal` 的并发控制与内存序（那是 u2-l2、u2-l3 的内容），也不讲扩缩容的完整流程（u2-l5）和 epoch 回收（u4-l2）。

## 2. 前置知识

- **环形缓冲区（ring buffer / circular buffer）**：用一块固定大小的连续内存，配合两个游标（头、尾）来模拟一个队列。当下标走到数组末尾时，折回到开头继续用，整体像首尾相接的环。它的最大优点是不搬移元素、入队出队都是 O(1)。
- **`front` / `back` 双游标模型**：`back` 是下一个**写入**位置，`front` 是下一个**读出（被偷）**位置，队列长度恒为 `len = back - front`。这两个游标只增不减（用 `wrapping_add`），所以即使取模后绕回，它们的差值仍然代表元素个数。
- **2 的幂取模技巧**：当 \( n \) 是 2 的幂时，\( x \bmod n = x \ \& \ (n-1) \)。位与运算比取模快，这是环形缓冲区常把容量设成 2 的幂的根本原因。
- **原子类型与内存序（仅需要概念层面）**：`AtomicIsize` 是可以被多线程安全读写的整数；`Ordering::Relaxed`/`Acquire`/`Release`/`SeqCst` 描述「这条原子操作对其他线程可见性的严格程度」。本讲只需建立直觉，具体配对留到后续讲。
- **`MaybeUninit<T>`**：一块「可能还没初始化」的内存。环形缓冲区里很多槽位在某一时刻是空的，用 `MaybeUninit` 包裹可以避免「必须立即初始化」的开销。
- **CachePadded / 伪共享（false sharing）**：CPU 缓存以「缓存行（通常 64 字节）」为单位加载。如果两个线程各自频繁改写的数据恰好落在同一缓存行上，即使它们逻辑上互不相关，硬件也会不断让该缓存行在两个核心之间来回失效、重新同步，造成性能损失。`CachePadded` 把数据补齐到一整个缓存行，把可能被不同线程热改的字段隔开。

## 3. 本讲源码地图

本讲的全部内容集中在单一文件 `src/deque.rs` 中（共 2233 行）。我们要精读的是它最顶部的「数据结构定义区」。

| 代码位置 | 作用 |
| --- | --- |
| [src/deque.rs:L17-L23](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L17-L23) | 三个关键常量 `MIN_CAP`、`MAX_BATCH`、`FLUSH_THRESHOLD_BYTES`。 |
| [src/deque.rs:L29-L99](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L29-L99) | `Buffer<T>` 结构体及其 `alloc`/`dealloc`/`at`/`write`/`read`，以及 `Clone`/`Copy` 实现。 |
| [src/deque.rs:L114-L145](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L114-L145) | `Inner<T>` 结构体（Worker 与 Stealer 共享的核心状态）与它的 `Drop` 实现。 |
| [src/deque.rs:L197-L209](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L197-L209) | `Worker<T>` 字段，可看到它如何同时持有 `Arc<CachePadded<Inner<T>>>` 与一份私有的 `Cell<Buffer<T>>` 缓存。 |
| [src/deque.rs:L574-L583](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L574-L583) | `Stealer<T>` 字段，与 Worker 共享同一个 `Inner`。 |

> 提示：`CachePadded` 与 `Backoff` 来自 `crossbeam_utils`，`Atomic`/`Owned` 与 `epoch` 来自 `crossbeam_epoch`，见 [src/deque.rs:L12-L13](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L12-L13)。

---

## 4. 核心概念与源码讲解

### 4.1 Buffer\<T>：幂次容量的环形缓冲区

#### 4.1.1 概念说明

`Buffer<T>` 是真正存放任务的「一块连续内存」。它只有两个字段：

- `ptr: *mut T` —— 指向分配出来的内存；
- `cap: usize` —— 这块内存能装多少个 `T`，**永远是 2 的幂**。

要注意一个反直觉的设计：`Buffer<T>` 自身**只是一个「指针 + 长度」的薄壳**，销毁这个薄壳（让它离开作用域）**并不会释放底层内存**。真正释放内存必须显式调用 `dealloc`。这一点和普通 `Vec`/`Box` 不同，是「延迟回收」机制（epoch GC，见 u4-l2）所要求的：旧的 buffer 指针会被到处复制、暂时保留，所以它的生命周期由作者用 `unsafe` 手动管理。

#### 4.1.2 核心流程

环形寻址的关键在 `at(index)`：

\[ \text{物理下标} = \text{index} \ \& \ (\text{cap} - 1) \]

因为 `cap` 是 2 的幂，设 \( \text{cap} = 2^k \)，则 \( \text{cap}-1 \) 的二进制是 \( k \) 个 1，按位与恰好保留 `index` 的低 \( k \) 位，等价于 \( \text{index} \bmod 2^k \)。

- `alloc(cap)`：用 `Box<[MaybeUninit<T>]>` 分配 `cap` 个未初始化槽位，转成裸指针 `*mut T`，返回 `Buffer { ptr, cap }`。
- `dealloc(self)`：把裸指针还原回 `Box`，`drop` 掉它，真正归还内存。
- `at(index)`：返回第 `index` 个槽位的指针，用掩码实现取模。
- `write(index, task)` / `read(index)`：在槽位上写 / 读一个 `MaybeUninit<T>`，用 **volatile** 语义。

`front`/`back` 游标一直累加（`wrapping_add(1)`），永不回卷；真正的「回卷」发生在 `at` 内部的掩码里。所以同一个物理槽位会被 `index`、`index+cap`、`index+2*cap` … 反复复用。

#### 4.1.3 源码精读

**结构体定义与 `Send`**（注意：销毁它不会释放内存）：

[src/deque.rs:L29-L37](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L29-L37) —— `Buffer<T>` 只有 `ptr` 和 `cap` 两个字段，注释明确说明「dropping an instance of this struct will *not* deallocate the buffer」；并手动 `unsafe impl Send`，让裸指针能在多线程间传递。

**分配**：用 `MaybeUninit` 保证「不强制初始化每个槽位」：

[src/deque.rs:L41-L52](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L41-L52) —— `debug_assert_eq!(cap, cap.next_power_of_two())` 强制容量为 2 的幂；`(0..cap).map(|_| MaybeUninit::<T>::uninit()).collect::<Box[_]>()` 分配 `cap` 个未初始化槽位，再 `Box::into_raw(...).cast::<T>()` 拿到裸指针。

**释放**：把裸指针还原成 `Box` 再 drop：

[src/deque.rs:L55-L62](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L55-L62) —— 通过 `Box::from_raw(slice_from_raw_parts_mut(...))` 重建切片 Box 后 drop，这是唯一真正归还内存的路径。

**O(1) 环形寻址**：

[src/deque.rs:L65-L70](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L65-L70) —— `self.ptr.offset(index & (self.cap - 1) as isize)`，用位与代替取模。注释说明之所以全程按 `MaybeUninit` 处理，是因为「读到一半可能才发现自己其实没有访问这块内存的权利」（并发偷取场景，见 u2-l3）。

**volatile 读写**：

[src/deque.rs:L72-L90](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L72-L90) —— `write` 用 `ptr::write_volatile`，`read` 用 `ptr::read_volatile`。注释坦言：这里可能与另一个线程对**同一槽位**并发读写，严格来说是 **data race / UB**；理论上应该用原子 store/load，但对任意类型 `T` 既昂贵又难通用实现，于是「作为一种 hack」用 volatile 读写替代。这个折中的正确性论证与论文引用在 u4-l1 详述，本讲只需记住「槽位读写用 volatile，不用原子」。

**Copy + Clone**：

[src/deque.rs:L93-L99](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L93-L99) —— `Buffer<T>` 是 `Copy`，`clone` 就是 `*self`（复制这两个字段）。因此「复制一个 Buffer」只是复制指针，极其廉价，这正是 Worker 能拥有一份私有缓存（见 4.3）的前提。但也因此，**`Buffer` 离开作用域不会触发 `dealloc`**——`Copy` 类型不能有释放资源的 `Drop`，否则会重复释放。内存回收完全交给 `dealloc` + epoch GC。

#### 4.1.4 代码实践

**实践目标**：亲手验证环形缓冲区的「游标只增、掩码回卷」模型与 `at` 公式。

**操作步骤**：

1. 假设 `cap = MIN_CAP = 64`，初始 `front = 0, back = 0`。
2. 连续 `push` 5 个任务 `A, B, C, D, E`，每次 `push` 先在 `back` 处写，再 `back += 1`。记录结果。
3. 计算若干 `index` 经过 `at` 后的物理下标。

**需要观察的现象 / 预期结果**（这是一个「纸上推演」型实践，无需编译运行）：

- push 5 个后：`front = 0`，`back = 5`，物理槽位 `at(0)..at(4)` 分别是 `A, B, C, D, E`，`len = back - front = 5`。
- `at(65) = 65 & (64 - 1) = 65 & 63`。\( 65 = \text{0b1000001} \)，\( 63 = \text{0b0111111} \)，按位与得 `0b0000001 = 1`；而 \( 65 \bmod 64 = 1 \)。两者一致 ✓。
- `at(128) = 128 & 63 = 0`，说明绕了一圈回到槽 0（此时若旧任务已被取走，槽 0 被复用，不会冲突）。

> 在纸上画一个 64 格的圆环，把 `A..E` 填进 0..4 号格，标出 `front`（指向 0）、`back`（指向 5）。再标出 `index=65` 落在 1 号格、`index=128` 落在 0 号格，就能直观看到「游标前进、槽位复用」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Buffer<T>` 必须是 2 的幂？如果不是 2 的幂，`at` 还能正确工作吗？

**答案**：因为 `at` 用 `index & (cap-1)` 代替取模，这只在 `cap` 是 2 的幂时才等价于 `index % cap`。若 `cap` 不是 2 的幂，`cap-1` 的二进制不再全是 1，掩码会给出错误的物理下标，导致越界或错位。`alloc` 里的 `debug_assert_eq!(cap, cap.next_power_of_two())` 正是用来在调试期守住这个不变量。

**练习 2**：`Buffer<T>` 是 `Copy`，但它管理的内存很「重」。这会不会导致内存泄漏或重复释放？

**答案**：不会重复释放，但要求作者格外小心。`Copy` 意味着复制它只是拷贝两个标量字段（指针 + 容量），且 `Copy` 类型不允许自定义 `Drop`，所以它离开作用域时**什么都不会发生**（这正是设计意图：薄壳不持有所有权）。真正释放内存的唯一途径是显式调用 `dealloc`。如果忘了调用就会泄漏——因此释放责任被严格地交给了 `Inner::drop` 与 epoch GC（4.3、u4-l2）。

---

### 4.2 关键常量：MIN_CAP、MAX_BATCH、FLUSH_THRESHOLD_BYTES

#### 4.2.1 概念说明

[src/deque.rs:L17-L23](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L17-L23) 定义了三个贯穿全文件的常量：

- `MIN_CAP: usize = 64` —— 队列的**最小容量**，也就是 `new_fifo`/`new_lifo` 一开始就分配的槽位数。
- `MAX_BATCH: usize = 32` —— `steal_batch()` / `steal_batch_and_pop()` **一次最多偷取的任务数**。
- `FLUSH_THRESHOLD_BYTES: usize = 1 << 10`（即 1024 字节）—— 当一个**退役（retired）的 buffer** 字节数达到这个阈值时，会立刻 `flush` 线程本地垃圾，让它尽快被回收。

#### 4.2.2 核心流程

| 常量 | 值 | 在哪里被用 | 作用 |
| --- | --- | --- | --- |
| `MIN_CAP` | 64 | `new_fifo`/`new_lifo` 的 `Buffer::alloc(MIN_CAP)`；`resize` 缩容下限 | 给新队列一个不大不小的起步容量，避免一上来就频繁扩容；缩容时不会低于它 |
| `MAX_BATCH` | 32 | `steal_batch` 系列方法的 `limit` 上限 | 限制单次批量偷取规模，防止一次偷太多导致负载不均 |
| `FLUSH_THRESHOLD_BYTES` | 1024 | `resize` 中 `if mem::size_of::<T>() * new_cap >= FLUSH_THRESHOLD_BYTES { guard.flush() }` | 大 buffer 被替换后，尽快触发回收，避免大块内存长期滞留 |

具体地：

- `MIN_CAP` 的使用见 [src/deque.rs:L226](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L226) 与 [src/deque.rs:L254](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L254)。
- `FLUSH_THRESHOLD_BYTES` 的判定见 [src/deque.rs:L319-L321](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L319-L321)。
- `MAX_BATCH` 出现在 `steal_batch` 系列方法中，本讲只需记住它的含义，具体计算在 u2-l4。

> 公式：退役 buffer 的字节数 = \( \text{sizeof}(T) \times \text{cap} \)。例如 `T = u64`（8 字节）、`cap = 64` 时为 512 字节，**不触发** flush；当 `cap` 扩到 256 时为 2048 字节，**触发** flush。

#### 4.2.3 源码精读

三个常量的定义集中在一处，注释也解释了用途：

[src/deque.rs:L17-L23](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L17-L23) —— 注释里写明：`FLUSH_THRESHOLD_BYTES` 是「If a buffer of at least this size is retired, thread-local garbage is flushed so that it gets deallocated as soon as possible」。

它如何决定是否 flush（在 `resize` 末尾）：

[src/deque.rs:L317-L321](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L317-L321) —— 用 `mem::size_of::<T>() * new_cap` 与阈值比较，达标就 `guard.flush()`。这里 `new_cap` 是**新** buffer 的容量，而它之所以用来判断「旧 buffer 有多大」，是因为扩容/缩容时新旧容量在数量级上一致（缩容是 `cap/2`、扩容是 `2*cap`）。

#### 4.2.4 代码实践

**实践目标**：用 `FLUSH_THRESHOLD_BYTES` 推算「什么类型的 T、多大 cap 会触发 flush」。

**操作步骤**：

1. 固定阈值 \( 1024 \) 字节。
2. 对若干 `T`（`u8`=1B、`u64`=8B、假设某结构体 128B）计算「不触发 flush 的最大 cap」。

**预期结果**（纸上计算）：

- \( \text{sizeof}(T) \times \text{cap} \geq 1024 \) 即触发。
- `T = u8`：`cap >= 1024` 才触发（对应约 5 次倍增：64→128→256→512→1024）。
- `T = u64`（8B）：`cap >= 128` 触发（64→128）。
- `T = 128B`：`cap >= 8` 就触发，但 `MIN_CAP=64` 远大于 8，所以**初始分配就触发**。

**待本地验证**：以上是纸面计算；若想实测，可在 `resize` 前后加调试打印观察 `guard.flush()` 是否被调用（修改源码仅用于本地学习，不要提交）。

#### 4.2.5 小练习与答案

**练习 1**：`MIN_CAP` 为什么取 64 而不是 4 或 8？

**答案**：64 恰好与一条典型 CPU 缓存行（64 字节，对齐到 `T` 时）量级接近，且作为 2 的幂满足掩码要求；取太小会频繁扩容，取太大会浪费内存。它是一个经验性的「起步容量」。

**练习 2**：如果把 `FLUSH_THRESHOLD_BYTES` 调得非常大（比如 `1 << 30`），会有什么后果？

**答案**：大 buffer 退役后不会立即 `flush`，旧内存会在线程本地垃圾队列里堆积，直到 epoch 自然推进才被回收，瞬时内存占用升高；反之调得太小会增加 `flush` 频率、增加开销。当前 1024 是个偏「尽快回收大块」的保守选择。

---

### 4.3 Inner\<T>：共享的核心状态与 Drop 实现

#### 4.3.1 概念说明

`Inner<T>` 是 **Worker 与 Stealer 真正共享的底层状态**。回顾 u1-l3：`Worker` 调用 `stealer()` 时，是 `Arc::clone` 出一份指向**同一个** `Inner` 的引用。也就是说，本地 push/pop 的线程和远程偷取的线程，看的是同一份 `front`/`back`/`buffer`。

它只有三个字段：

- `front: AtomicIsize` —— 队头游标（下一个被偷/弹出的位置），主要由偷取者推进；
- `back: AtomicIsize` —— 队尾游标（下一个写入位置），主要由 Worker 自己推进；
- `buffer: CachePadded<Atomic<Buffer<T>>>` —— 当前的缓冲区指针（原子指针，由 epoch 管理）。

注释里引用了三篇奠基性论文（Chase-Lev 原始论文、弱内存模型下的正确 work-stealing、CDSchecker 模型检测器），说明这个 `Inner` 的并发正确性是有学术背书的。

#### 4.3.2 核心流程

- **构造**（见 `new_fifo`）：`front = 0`，`back = 0`，`buffer` 装一个 `Buffer::alloc(MIN_CAP)`，整体包进 `Arc<CachePadded<Inner<T>>>`。
- **运行期**：Worker 写任务时推进 `back`；偷取者读任务时推进 `front`；扩缩容时把 `buffer` 原子地 `swap` 成新指针。
- **销毁**（`Inner::drop`）：此时已经没有其他线程在用（`Arc` 计数归零），用 `epoch::unprotected()` 在「无并发」前提下，遍历 `front..back` 把残留任务 `drop_in_place`，再 `dealloc` 掉 buffer。

#### 4.3.3 源码精读

**字段定义与论文引用**：

[src/deque.rs:L101-L123](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L101-L123) —— 注释列出三篇论文链接；`buffer` 字段类型是 `CachePadded<Atomic<Buffer<T>>>`：`Atomic`（来自 crossbeam-epoch）是带 epoch 标签的原子指针，`CachePadded` 把它单独占一条缓存行（原因见 4.4）。

**Drop 实现**（单线程、无并发场景下的善后）：

[src/deque.rs:L125-L145](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L125-L145) —— 关键点：

1. 用 `self.back.get_mut()` / `self.front.get_mut()` 拿到 `&mut` 引用（Drop 期间独占，可直接改写原子内部值）；
2. 用 `epoch::unprotected()` 加载 buffer 指针——这是 epoch 提供的「我现在确实没有别的线程竞争」的逃生舱，**只能在确定单线程时使用**；
3. `while i != b { buffer.deref().at(i).drop_in_place(); i = i.wrapping_add(1); }` 逐个 drop 残留任务；
4. `buffer.into_owned().into_box().dealloc()` 真正释放 buffer 内存。

**Worker 如何同时持有共享与私有副本**：

[src/deque.rs:L197-L209](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L197-L209) —— `Worker` 有两个跟 buffer 有关的字段：

- `inner: Arc<CachePadded<Inner<T>>>` —— 共享的权威状态（stealer 也看这里）；
- `buffer: Cell<Buffer<T>>` —— **Worker 私有的 buffer 指针缓存**。

因为 `Buffer` 是 `Copy`（4.1），`Cell<Buffer<T>>` 让 Worker 能在**不碰原子、不做 epoch pin**的情况下快速拿到当前 buffer 指针。看 `push` 里这两行就很清楚：

[src/deque.rs:L403](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L403) 与 [src/deque.rs:L414](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L414) —— `let mut buffer = self.buffer.get();` 直接取私有副本；扩容后 `buffer = self.buffer.get();` 再取一次更新后的副本。而 `resize` 中 `self.buffer.replace(new)`（[src/deque.rs:L308](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L308)）负责把私有副本同步成新 buffer。

> 这就是 Worker 「快路径」的秘密：自己读写时用 `Cell` 缓存的裸指针，省掉原子加载和 epoch 开销；只有偷取者（`Stealer`）才需要走 `inner.buffer` 的原子加载 + `epoch::pin`（见 u2-l3）。

#### 4.3.4 代码实践

**实践目标**：通过阅读 `Inner::drop`，理解「为什么 drop 里敢用 `unprotected()`」。

**操作步骤**：

1. 读 [src/deque.rs:L125-L145](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L125-L145)。
2. 回答：`Inner::drop` 被调用的前提是什么？为什么此时不可能有别的线程在读写这个 `Inner`？

**预期结果（推理）**：

- `Inner` 只能通过 `Arc` 持有。`Inner::drop` 触发当且仅当**最后一个** `Arc<Inner>`（即最后一个 Worker 与所有 Stealer）被销毁。
- 既然 `Arc` 计数归零，就不再有任何线程能拿到这个 `Inner`，自然没有并发，于是可以安全地用 `unprotected()` 和 `get_mut()`。

**待本地验证**：可以用一个最小程序 `drop` 掉 Worker 和它的 Stealer，观察（加打印后）`Inner::drop` 是否在所有引用消失后才执行。

#### 4.3.5 小练习与答案

**练习 1**：`front`/`back` 为什么用 `AtomicIsize`（有符号）而不是 `AtomicUsize`？

**答案**：因为队列长度用 `back.wrapping_sub(front)` 计算，结果可能是负数（表示「空」或并发读取时偷取者看到的瞬态）。用 `isize` 可以自然地表达 `len <= 0` 这类判断（见 `is_empty`：[src/deque.rs:L363-L367](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L363-L367)）。`wrapping_*` 运算保证游标在 `isize` 范围内「永远递增」也不会溢出 UB（2^63 次操作量级远超实际）。

**练习 2**：`Worker` 已经有了 `inner.buffer`（共享的原子指针），为什么还要再存一份 `Cell<Buffer<T>>`？

**答案**：为了快路径性能。Worker 自己 push/pop 非常频繁，每次都从 `inner.buffer` 做原子加载并 `epoch::pin` 太贵。由于 `Buffer` 是 `Copy`，存一份私有副本（`Cell` 提供内部可变性）后，Worker 直接 `get()` 拿裸指针即可；只有在 `resize` 时才用 `replace` 同步这份副本。这是一个典型的「快慢路径分离」设计。

---

### 4.4 CachePadded：防止伪共享

#### 4.4.1 概念说明

`CachePadded` 来自 `crossbeam_utils`（[src/deque.rs:L13](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L13)），它的作用是把被包裹的数据**补齐（pad）到一整条 CPU 缓存行**（通常 64 字节），从而避免**伪共享（false sharing）**。

伪共享是这样一种性能陷阱：两个 CPU 核心各自频繁改写**不同**的变量，但这两个变量恰好落在**同一条缓存行**上。缓存行是缓存同步的最小单位，于是哪怕逻辑上互不相关，这条缓存行也会在两个核心之间被反复「失效—重读」，极大拖慢性能。解决办法就是把会被不同核心热改的数据用 `CachePadded` 隔开到各自的缓存行。

#### 4.4.2 核心流程

在本 crate 中，`CachePadded` 出现在两个关键位置：

1. **整个 `Inner` 被 `Arc<CachePadded<Inner<T>>>` 包裹**（Worker/Stealer 字段）：因为不同线程会频繁读写 `Inner` 内部的 `front`/`back`。
2. **`Inner.buffer` 字段本身是 `CachePadded<Atomic<Buffer<T>>>`**：把 buffer 指针与 `front`/`back` 隔到不同缓存行，避免 Worker 频繁改 `back` 时把 buffer 指针所在缓存行也「震」给偷取者。

效果上：

\[ \text{同一缓存行上的热字段} \Rightarrow \text{不断互相失效} \xrightarrow{\text{CachePadded}} \text{各占独立缓存行} \Rightarrow \text{互不干扰} \]

#### 4.4.3 源码精读

**`Inner.buffer` 用 `CachePadded` 单独隔离**：

[src/deque.rs:L114-L123](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L114-L123) —— `front`、`back` 是两个 `AtomicIsize`（各 8 字节），`buffer` 是 `CachePadded<Atomic<Buffer<T>>>`。如果不加 `CachePadded`，三者可能挤在同一缓存行里：Worker 每次推进 `back`（push）、偷取者每次推进 `front`（steal），都会让彼此的缓存行失效。`CachePadded` 强制把 buffer 指针独占一条缓存行，把冲突隔开。

**整个 `Inner` 再被 `CachePadded<Inner<T>>` 包一层**：

[src/deque.rs:L199](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L199)（Worker）与 [src/deque.rs:L576](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L576)（Stealer）—— 两者都是 `Arc<CachePadded<Inner<T>>>`。`new_fifo`/`new_lifo` 构造时用 `CachePadded::new(Inner { ... })` 把整个内部状态补齐，见 [src/deque.rs:L228-L232](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L228-L232)。

> 这是一个「双层防护」：内层 `CachePadded` 隔开 `Inner` 自己的字段之间；外层 `CachePadded` 隔开「这个队列的 Inner」与「堆上相邻的其他对象」。代价是每个 `Inner` 多占一两条缓存行的内存（通常几十到上百字节），对于并发吞吐的提升完全值得。

#### 4.4.4 代码实践

**实践目标**：建立「伪共享」与「缓存行」的直觉。

**操作步骤**：

1. 假设缓存行 = 64 字节，`AtomicIsize` = 8 字节。
2. 想象一个**没有** `CachePadded` 的 `Inner { front, back, buffer }`，估算它们在内存中的排布。
3. 分析：Worker 线程疯狂 push（改 `back`），偷取线程疯狂 steal（改 `front`），会发生什么？

**预期结果（推理）**：

- 没有 padding 时，`front`（偏移 0）、`back`（偏移 8）、`buffer` 指针（偏移 16）都在同一条 64 字节缓存行内。
- 两个核心分别改 `back` 和 `front`，硬件为保证一致性，会让这条缓存行在两个核心间反复失效并重传，吞吐骤降——这就是伪共享。
- 加上 `CachePadded` 后，关键字段被推到各自独立缓存行，互不打扰。

**待本地验证**：真正的伪共享需要 benchmark 才能量化（用 `criterion` 对比有/无 `CachePadded` 的吞吐）。本讲只做推理，benchmark 实践留作进阶练习。

#### 4.4.5 小练习与答案

**练习 1**：`CachePadded` 会增加内存开销。在本场景下，这个开销值得吗？

**答案**：值得。`Inner` 是被多核高频读写的热数据；不隔离伪共享的话，每次 push/steal 都会引发缓存行失效，性能损失远大于多占几十字节内存。`CachePadded` 是并发数据结构里非常标准的取舍。

**练习 2**：为什么是 `Inner.buffer` 单独被 `CachePadded`，而 `front`/`back` 没有各自被 `CachePadded`？

**答案**：这是基于访问模式的权衡。`front` 和 `back` 虽然也常被改，但它们的字段较小、相互作用模式已被论文验证；而 buffer 指针在扩缩容时才被 `swap`，把它单独隔离能避免「扩容抖动」干扰 front/back 的常规读写。如果对极端吞吐有要求，也可以把 front/back 各自 pad（crossbeam 的很多数据结构确实这么做），本 crate 选择了当前这层折中。

---

## 5. 综合实践

把本讲的四个模块串起来，做一次「完整的结构解读 + 推演」：

1. **画结构图**：画出 `Worker<T>`、`Stealer<T>`、`Inner<T>`、`Buffer<T>` 之间的引用关系。应当体现：
   - Worker 和 Stealer 各持有一个 `Arc<CachePadded<Inner<T>>>`，指向**同一个** `Inner`；
   - `Inner` 含 `front`/`back`/`buffer`，其中 `buffer` 是 `CachePadded<Atomic<Buffer<T>>>`；
   - Worker 额外有一份 `Cell<Buffer<T>>` 私有副本；
   - `Buffer` 是 `{ ptr, cap }`，`Copy`，销毁不释放内存。
2. **推演一次 push + 一次 steal 的数据结构变化**（只看数据结构，不看并发控制）：
   - 初始 `front=0, back=0, cap=64`。
   - Worker `push(A)`：在 `at(0)=0` 写 A，`back` 变 1。
   - Stealer `steal()`：从 `at(0)=0` 读 A，`front` 变 1。
   - 此刻 `len = back - front = 0`，队列空；槽 0 物理上仍留着 A 的比特，但逻辑上已被取走，下次 `push` 到 `at(64)=0` 时会被覆盖。
3. **回答综合问题**：如果此时 Worker 连续 push 到第 65 个任务，`at(64)` 落在哪个物理槽？会和谁冲突吗？为什么不会丢数据？
   - 提示：`at(64) = 64 & 63 = 0`；此时最早的 A 早已被偷走（`front` 已推进），槽 0 被安全复用；游标差值 `back - front` 始终准确反映存活任务数。

> 这个综合实践不写代码，重在让你把「薄壳 Buffer + 双游标 Inner + CachePadded 隔离 + Worker 私有缓存」这四件事在脑中连成一张图。它也是后续 u2-l2（push/pop 实现）的直接前置。

## 6. 本讲小结

- `Buffer<T>` 是「指针 + 容量」的薄壳，容量恒为 2 的幂，`at(index)` 用 `index & (cap-1)` 做 O(1) 环形取模。
- `Buffer<T>` 是 `Copy`，**销毁它不会释放内存**；唯一释放途径是显式 `dealloc`，回收责任交给 `Inner::drop` 与 epoch GC。
- 槽位读写用 `ptr::write_volatile`/`read_volatile` 而非原子操作，是「为通用类型 T 牺牲严格无 UB 换取性能」的已知折中（详见 u4-l1）。
- `Inner<T>` 是 Worker 与 Stealer 共享的核心状态：`front`/`back`（`AtomicIsize`）+ `buffer`（`CachePadded<Atomic<Buffer<T>>>`），正确性有三篇论文背书。
- `Inner::drop` 在 `Arc` 计数归零的单线程场景下，用 `epoch::unprotected()` 安全善后：逐个 drop 残留任务再 `dealloc`。
- `CachePadded` 通过把热字段补齐到独立缓存行来**防止伪共享**；Worker 还用一份 `Cell<Buffer<T>>` 私有副本做「快路径」，省掉每次 push/pop 的原子加载与 epoch pin。

## 7. 下一步学习建议

本讲只搭好了「数据结构舞台」。接下来：

- **u2-l2（push 与 pop 的实现）**：在 `Buffer`/`Inner` 之上，逐行看 `Worker::push`（满则扩容、写槽、fence、推进 back）与 `Worker::pop`（FIFO 用 `fetch_add` 推进 front、LIFO 先减 back 再读槽）。
- **u2-l3（单任务偷取）**：看 `Stealer::steal` 如何用 `epoch::pin` + CAS 在 `Inner` 上安全偷取，并理解 `at` 注释里「读到一半才发现没权限」的真正含义。
- **u2-l5（扩缩容与生命周期）**：看 `resize` 如何 `swap` 旧 buffer 并用 epoch 延迟回收，呼应本讲「Buffer 销毁不释放内存」的设计动机。
- **u4-l2（epoch GC）**：系统理解「为什么旧 buffer 不能立即释放、要靠 epoch 延迟回收」，把本讲埋下的伏笔彻底讲清。

建议阅读顺序：先把本讲的「结构图」画熟，再带着它去读 u2-l2，你会发现自己能很快看懂 push/pop 在「改哪个游标、写哪个槽」。
