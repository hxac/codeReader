# unsafe 的安全性论证与 MaybeUninit

## 1. 本讲目标

本讲是专家层的「内存安全」专题。前面几讲我们已经讲清了 `ArrayQueue` 的 stamp 状态机、`SegQueue` 的块链表与 WRITE/READ/DESTROY 位，以及所有原子操作的内存序选择。但还有一个问题始终被刻意回避：**这两个队列内部到处是 `unsafe`，凭什么它们是健全的（sound）？**

学完本讲，你应当能够：

1. 为 `unsafe impl<T: Send> Send/Sync for ArrayQueue/SegQueue` 给出完整的正确性论证，尤其是「为什么只要 `T: Send`、而不需要 `T: Sync`」。
2. 理解 `MaybeUninit<T>` 的 `uninit` / `new` / `write` / `read` / `replace` / `assume_init` / `assume_init_drop` 各自的语义，以及它们如何与 stamp / state 位配合，保证「不读未初始化内存、不重复读、不重复释放」。
3. 解释 `Drop` 中 `mem::needs_drop::<T>()` 守卫的作用，以及按槽回收时的安全性。
4. 解释 `UnwindSafe` / `RefUnwindSafe` 为什么被无条件实现（`impl<T>`），以及它放弃了 auto trait 的哪些保守保护。

---

## 2. 前置知识

本讲需要你已经掌握前几讲的内容，这里只做最简短的术语回顾：

- **`UnsafeCell<T>`**：Rust 中「合法的内部可变性」原语。它告诉编译器「这块内存可能被别名 `&mut` 修改，不要做违反可变性的优化」。代价是：含 `UnsafeCell` 的类型不再是自动 `Sync`。
- **`MaybeUninit<T>`**：一个可能持有未初始化内存的包装类型。它本身是「零成本但需要程序员手动跟踪初始化状态」的。读取未初始化的 `MaybeUninit` 是未定义行为（UB）。
- **auto trait `Send` / `Sync`**：编译器根据字段自动推导。`Send` 表示所有权可跨线程转移；`Sync` 表示 `&T` 可被多线程共享。
- **`stamp` / `state` 状态机**（u2-l1、u3-l1）：`ArrayQueue` 用 `slot.stamp` 编码「这个槽现在该被写还是该被读」；`SegQueue` 用 `slot.state` 的 WRITE/READ/DESTROY 位编码槽状态。它们是本讲所有 `unsafe` 的「安全前提」。
- **Acquire / Release 配对**（u4-l1）：生产者「写值 → Release 标记」、消费者「Acquire 标记 → 读值」，把数据的可见性绑定在同一原子变量上。

一句话总括本讲的中心思想：

> crossbeam-queue 之所以敢用 `unsafe`，是因为它把「哪个线程此刻可以访问哪个槽的 T」完全锁死在一个**互斥的状态机**里——写之前先 CAS 抢占、读之前也先 CAS 抢占，再用 Acquire/Release 把生产者的写和消费者的读串成一条 happens-before 链。于是任意时刻，一个槽里的 T 至多被一个线程触碰，队列本质上是一个「所有权搬运管道」，而不是「共享只读数据」。

---

## 3. 本讲源码地图

本讲只精读两个文件，但聚焦于其中与「安全性」相关的行：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `src/array_queue.rs` | 有界 MPMC 队列 | `unsafe impl Send/Sync`、`MaybeUninit` 的写/读/替换、`Drop` 与 `needs_drop`、`UnwindSafe` |
| `src/seg_queue.rs` | 无界分段 MPMC 队列 | `unsafe impl Send/Sync`、WRITE 位发布协议、`Block::destroy` 协调、`Drop`、`UnwindSafe` |

链接基准（当前 HEAD）：

```
https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/
```

---

## 4. 核心概念与源码讲解

### 4.1 unsafe impl Send/Sync 的依据

#### 4.1.1 概念说明

`ArrayQueue<T>` 和 `SegQueue<T>` 的字段里都含 `UnsafeCell<MaybeUninit<T>>`（槽里的值），而 `UnsafeCell<T>` 被语言标记为 `!Sync`。因此，含它的复合类型**不会**自动获得 `Sync`，于是必须手写：

```rust
unsafe impl<T: Send> Sync for ArrayQueue<T> {}
unsafe impl<T: Send> Send for ArrayQueue<T> {}
```

注意这两个 `impl` 的 bound 都是 **`T: Send`**，而不是 `T: Sync`。这一点经常被初学者写错，需要重点论证为什么「只要能搬运、不需要能共享」就够了。

#### 4.1.2 核心流程

论证 `Sync` 的标准套路是回答一个问题：**多个线程同时持有 `&ArrayQueue<T>` 并发调用 `push`/`pop` 时，会不会发生数据竞争？**

把答案拆成三条互斥保证：

1. **生产者之间互斥**：所有生产者通过 `self.tail` 的 `compare_exchange_weak` 串行抢占「下一个可写槽」。赢得 CAS 的那个生产者才被允许写 `slot.value`，其余的重试。→ 同一槽不会有两个生产者同时写。
2. **消费者之间互斥**：所有消费者通过 `self.head` 的 CAS 串行抢占「下一个可读槽」。赢得 CAS 的那个消费者才被允许 `read()`。→ 同一槽不会有两个消费者同时读（不会「double-read」）。
3. **生产者 → 消费者的交接**：生产者写值后用 `Release` 发布 stamp；消费者用 `Acquire` 读 stamp，且只有当 `stamp == head + 1`（ArrayQueue）或看到 `WRITE` 位（SegQueue）时才会读值。这建立了 happens-before，消费者保证能看到生产者写的全部字节。

三条合起来意味着：**任意时刻，一个槽里的 T 至多被一个线程访问，且永远是「先写后读、写一次读一次」**。这正是「所有权从生产者线程搬运到消费者线程」的语义，等价于一次跨线程 `T` 的 move。所以约束只需 `T: Send`（T 可以跨线程搬运），而**不需要** `T: Sync`（那要求多个线程能同时共享同一个 T 的引用，而本队列永远不会让两个线程同时看同一个 T）。

`Send` 的论证则简单：把整个队列 `Box` 进新线程时，所有缓冲区里的 T 的所有权一并转移，所以需要 `T: Send`。

#### 4.1.3 源码精读

两个队列的 `unsafe impl` 写法一致（仅 bound 顺序不同）：

[src/array_queue.rs:76-80](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L76-L80) — `ArrayQueue` 的 `Send`/`Sync`/`UnwindSafe`/`RefUnwindSafe` 四个 trait impl，紧挨在一起：

```rust
unsafe impl<T: Send> Sync for ArrayQueue<T> {}
unsafe impl<T: Send> Send for ArrayQueue<T> {}
```

[src/seg_queue.rs:172-173](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L172-L173) — `SegQueue` 同样以 `T: Send` 为 bound。注意 `SegQueue` 还有一个 [`_marker: PhantomData<T>`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L168-L169) 字段——它参与 drop 检查，但手写的 `unsafe impl` 会覆盖 `PhantomData` 默认推导出的 `!Send`/`!Sync` 限制（`PhantomData<T>` 默认 `Send iff T: Send`、`Sync iff T: Sync`，这里恰好与 bound 一致，但 `UnsafeCell` 仍是主导因素）。

为什么必须手写、不能 `derive`？看槽的定义：

[src/array_queue.rs:17-27](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L17-L27) — `Slot<T>` 的 `value: UnsafeCell<MaybeUninit<T>>`。`UnsafeCell` 是 `!Sync`，传染给 `Slot`、`Box<[Slot]>`、再到 `ArrayQueue`，使自动推导失败，必须 `unsafe impl`。

#### 4.1.4 代码实践

**实践目标**：用编译器「反证」`T: Sync` 不是必需的，`T: Send` 才是。

**操作步骤**：

1. 写一个 `!Sync` 但 `Send` 的类型，例如含 `Cell<i32>` 的包装：

   ```rust
   use std::cell::Cell;
   #[derive(Debug)]
   struct NotSync(Cell<i32>);   // Cell<T> 是 Send 但 !Sync
   ```

2. 把它放进 `ArrayQueue`，在两个线程间一进一出：

   ```rust
   use crossbeam_queue::ArrayQueue;
   let q = ArrayQueue::new(16);
   q.push(NotSync(Cell::new(1))).unwrap();
   std::thread::scope(|s| {
       let q = &q;
       s.spawn(move || { q.push(NotSync(Cell::new(2))).unwrap(); });
       s.spawn(move || { let v = q.pop(); println!("{v:?}"); });
   });
   ```

3. 再写一个 `!Send` 的类型（如 `Rc<i32>`），尝试 `q.push(Rc::new(1))`。

**需要观察的现象**：第 2 步能编译通过并运行；第 3 步在编译期报错 `Rc<i32>` 不能跨线程发送。

**预期结果**：这从两个方向印证了 `T: Send` 是真约束、`T: Sync` 不是约束。

**待本地验证**：上述为示例代码，请在你本机的 crossbeam 仓库内新建临时 binary 运行确认。

#### 4.1.5 小练习与答案

**练习 1**：如果队列的实现有 bug，导致两个消费者可能同时读到同一个槽，那么 `T: Send` 是否还足以保证 `Sync` 的健全性？

**答案**：不够。若两个消费者并发 `read()` 同一个 `MaybeUninit<T>`，相当于两个线程同时获取该 T 的所有权——这是重复 move-out，对非 `Copy` 类型是 UB。此时要让 `Sync` 健全，必须收紧到 `T: Copy`（或更强的 `T: Sync`）。这正是为什么「消费者 CAS 互斥」是不可妥协的不变量。

**练习 2**：为什么 bound 是 `T: Send` 而不是 `T: Send + 'static`？

**答案**：队列本身没有限制元素的生命周期；只要借用可以在作用域内安全搬运，带生命周期的 `T` 也可以入队（`ArrayQueue<T>` 的 `T` 可以含引用）。`'static` 不是 `Send`/`Sync` 的内在要求。

---

### 4.2 MaybeUninit 的 write / read / assume_init

#### 4.2.1 概念说明

`MaybeUninit<T>` 是一块「可能是、也可能不是已初始化 T」的内存。它的危险在于：读一块**从未被初始化**的 `MaybeUninit` 是 UB（可能是任意位模式，对带不变量的类型致命）。它的常用操作：

| 操作 | 语义 | 在队列中对应 |
| --- | --- | --- |
| `MaybeUninit::uninit()` | 创建未初始化 | `new()` 中所有槽的初值 |
| `MaybeUninit::new(v)` | 创建已初始化（包住 v） | 写值前的参数 |
| `ptr.write(val)` | 按位写入（覆盖），pointee 视为已初始化 | push 写值 |
| `ptr.read()` | 按位移出（`ptr::read` 语义），原位变成「逻辑未初始化」 | pop 读值 |
| `ptr.replace(val)` | 写入新值、返回旧值 | `force_push` 覆盖最旧元素 |
| `m.assume_init()` | 断言已初始化，把 `MaybeUninit<T>` 变成 `T` | read 之后 |
| `m.assume_init_drop()` | 原地 drop 已初始化的 T | `Drop` 中回收 |

> **关于 `.read()` / `.write()` / `.replace()` 的解析**：代码里 `slot.value.get()` 返回 `*mut MaybeUninit<T>`，随后的 `.write()` / `.read()` / `.replace()` 是**裸指针的内建方法**（等价于自由函数 `core::ptr::write` / `ptr::read` / `ptr::replace`）。注意它们操作的是整个 `MaybeUninit<T>` 单元：`.read()` 移出一个 `MaybeUninit<T>`，再用 `.assume_init()` 断言其中的 T 已初始化。这要求较新版本的 Rust 才支持指针内建方法；`Cargo.toml` 里的 `rust-version` 字段标注为 `1.60`，与 HEAD 代码的实际依赖并不一致（属版本元信息滞后），本讲以 HEAD 实际代码为准。

健全性的总原则：**每一次 `read().assume_init()` 之前，都必须有一次与之配对的、由 Release→Acquire 串起来的 `write()`，且 CAS 已保证该槽的写/读各自独占。**

#### 4.2.2 核心流程

以 `ArrayQueue` 为例，画出一对 push/pop 的时序（`s` 表示 stamp）：

```
生产者 push(v)                          消费者 pop()
─────────────────                       ─────────────────
1. CAS 抢占 tail: tail→new_tail         (尚未参与)
2. slot.value.write(MaybeUninit::new(v)) ← 写值
3. slot.stamp.store(tail+1, Release)    ← 发布
                                        4. stamp = slot.stamp.load(Acquire)
                                        5. 若 stamp == head+1：CAS 抢占 head
                                        6. slot.value.read().assume_init() ← 读值
                                        7. slot.stamp.store(head+one_lap, Release)
```

关键点：

- 第 2 步的 `write` 与第 6 步的 `read` 之间没有显式同步，但它们被**同一个原子变量 `slot.stamp`** 的 Release（步骤 3）和 Acquire（步骤 4）串成 happens-before。消费者读到 `stamp == head+1` 的那一刻，必然已经看到生产者的 `write`。
- 第 1 步和第 5 步的 CAS 互斥保证「同一槽最多一个生产者、最多一个消费者」。
- 因此 `read()` 读到的一定是已初始化、且尚未被别人读过的值。`assume_init()` 的断言成立。

`SegQueue` 的逻辑同构，只是「发布标记」从 `slot.stamp` 换成了 `slot.state` 的 `WRITE` 位：生产者写值后 `fetch_or(WRITE, Release)`，消费者先 `wait_write()`（Acquire 自旋等 `WRITE`）再 `read()`。

#### 4.2.3 源码精读

**初始化为未初始化**——`ArrayQueue::new` 把每个槽的值都置为 `MaybeUninit::uninit()`，stamp 置为 `i`（只有 `slot[0]` 一开始可写）：

[src/array_queue.rs:106-114](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L106-L114) — 这就是「槽一开始合法地未初始化」的来源。所有后续 `read()` 都必须先经过一次配对的 `write()`。

**push 写值 + Release 发布**——`push_or_else` 在赢得 tail CAS 后的 `Ok(_)` 分支：

[src/array_queue.rs:163-170](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L163-L170) — 先 `write`，后 `store(..., Release)`。注意顺序不可颠倒：必须先写值再发布 stamp，否则消费者可能看到「stamp 已更新但值还没写」的窗口，`read()` 出乱码。这两行是「write→Release」发布协议的实体。

**pop 读值**——赢得 head CAS 后：

[src/array_queue.rs:351-356](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L351-L356) — 本行就是 `let msg = unsafe { slot.value.get().read().assume_init() };`。它的安全性正是本讲综合实践要你写 SAFETY 注释的地方（见第 5 节）。它之前在 [src/array_queue.rs:330](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L330) 以 `Acquire` 读过 stamp，并因 `head + 1 == stamp` 进入本分支。

**force_push 的 replace**——队列满时覆盖最旧元素，需要「写入新值、同时取出旧值」：

[src/array_queue.rs:290](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L290) — `slot.value.get().replace(MaybeUninit::new(v)).assume_init()`。`replace` 原子地（指这一条指令语义上的「先存后取」）把旧值交还，安全性来自上方的 `head.compare_exchange_weak`：CAS 成功意味着这个被覆盖的槽此刻被本线程独占，且其旧值必定是已初始化的（因为它在前一圈被 push 过、还没被 pop）。

**SegQueue 的同构版本**——生产者写值并 `fetch_or(WRITE, Release)`：

[src/seg_queue.rs:278-281](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L278-L281) — 与 ArrayQueue 的 `write`+`store(Release)` 完全对应，只是发布载体是 `WRITE` 位。

消费者先 `wait_write`（Acquire 自旋）再读：

[src/seg_queue.rs:428-430](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L428-L430) — `wait_write` 在 [src/seg_queue.rs:45-50](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L45-L50) 用 `Acquire` 自旋等 `WRITE` 位置位，确保读到的是生产者已写完的值，再 `read().assume_init()`。

#### 4.2.4 代码实践

**实践目标**：亲手验证「write→Release / Acquire→read」配对缺一不可。

**操作步骤**（源码阅读型）：

1. 打开 `src/array_queue.rs`，定位 `push_or_else` 第 163-170 行与 `pop` 第 351-356 行。
2. 思想实验：假设把第 168 行 `slot.stamp.store(tail + 1, Ordering::Release)` 改成 `Ordering::Relaxed`，推演在弱内存模型（如 ARM）上会发生什么。
3. 再假设把第 166 行的 `write` 和第 168 行的 `store` 顺序对调，推演后果。

**需要观察的现象 / 预期结果**：

- 改 `Release→Relaxed`：消费者可能在 `head+1 == stamp` 成立时，尚未观察到生产者的 `write`，`read()` 读到未初始化字节 → UB。x86 是强模型（store-store 不重排、且 Acquire/Release 近似全功能），常掩盖此 bug；须靠 `miri` 或 `loom` 多种子才能复现。
- 对调 write/store 顺序：消费者看到 stamp 已更新就立刻读，但值还没写完 → 同样 UB，且更容易在任何平台触发。

**待本地验证**：上述为思想实验，不要真的改坏源码后用于生产；可在副本里改动后跑 `cargo miri test` 观察是否报 UB。

#### 4.2.5 小练习与答案

**练习 1**：`read()` 之后槽里的字节并没有被清零，为什么不会出问题？

**答案**：`read()` 是 `ptr::read` 语义，把 T **按位搬走**并把槽标记为「逻辑上已移出」。此后唯一能再次访问该槽的合法路径是生产者在下一圈重新 `write`——`write` 会无条件覆盖这些字节，所以陈旧字节不会泄漏。stamp 状态机保证在「重新 write」之前没有任何消费者能读到它（stamp 不等于 `head+1`）。

**练习 2**：`force_push` 用 `replace` 而不是「先 `read` 再 `write`」，为什么？

**答案**：`read` 后 `write` 是两步，中间槽处于「逻辑未初始化」；若有别的消费者在此窗口抢到该槽就会 UB。`replace` 是单步「写入新值并取回旧值」，保证了槽始终处于「已初始化」状态，配合 head CAS 的独占，安全且无窗口。

---

### 4.3 assume_init_drop 与 needs_drop 守卫

#### 4.3.1 概念说明

当队列被 drop 时，缓冲区里「还在排队的」T 必须被正确析构——既不能漏 drop（内存/资源泄漏），也不能重复 drop（double-free，UB）。这里有两层问题：

1. **哪些槽是已初始化的？** 答：head 到 tail 之间环形区段内的槽。这个区间的计算和 `len()` 完全一样（见 u2-l3 / u3-l4）。
2. **每个已初始化槽如何安全 drop？** 用 `assume_init_drop()`——它断言槽已初始化并原地调用 T 的 drop。

此外有个优化：如果 `T` 根本没有 drop 胶水（`mem::needs_drop::<T>() == false`，例如纯 `Copy` 类型），那整个 drop 循环都是空操作，可以直接跳过。`ArrayQueue` 用 `needs_drop` 守卫包住循环；`SegQueue` 的 `Drop` 则无条件执行（对无 drop 胶水的类型，`assume_init_drop` 会被编译成空操作，结果等价，只是多了一次循环）。

#### 4.3.2 核心流程

`ArrayQueue::drop`（持有 `&mut self`，**无任何并发**）：

```
if needs_drop::<T>():
    hix, tix ← head, tail 的 index 部分
    len     ← 与 len() 相同的三分类环形算术
    for i in 0..len:
        index ← 环形映射 (hix + i) % capacity
        slot[index].value.assume_init_drop()
```

关键安全性：`&mut self` 由借用检查器在编译期保证「此刻没有别的线程」，所以这里完全不需要原子操作、不需要 CAS、不需要 stamp 协调。只要 `len` 算对了，`assume_init_drop` 的「已初始化」断言就成立——因为这些槽正是「被 push 过、还没被 pop 掉」的槽。

#### 4.3.3 源码精读

**`needs_drop` 守卫 + 按槽 drop**：

[src/array_queue.rs:535-572](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L535-L572) — 整个析构体被 [第 537 行 `if mem::needs_drop::<T>()`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L537) 包住。核心 drop 调用在 [第 564-568 行](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L564-L568)：

```rust
unsafe {
    debug_assert!(index < self.buffer.len());
    let slot = self.buffer.get_unchecked_mut(index);
    (*slot.value.get()).assume_init_drop();
}
```

注意 `(*slot.value.get()).assume_init_drop()` 这里 `get()` 返回 `*mut MaybeUninit<T>`，解引用后对 `MaybeUninit<T>` 调 `assume_init_drop`（`&mut self`）。

**为什么 `needs_drop` 既是优化也是安全的**：对 `needs_drop::<T>() == false` 的类型（如 `i32`、`u8`），就算不做守卫直接 `assume_init_drop`，编译器生成的 drop 也是空的，不会出 UB；守卫只是省掉一次无谓循环。换言之，守卫改变的是性能，不是健全性——健全性由「只 drop 已初始化槽」保证。

**SegQueue 的对照**：[src/seg_queue.rs:599-634](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L599-L634) 的 `Drop` 没有 `needs_drop` 守卫，直接在 [第 614-617 行](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L614-L617) 对数据槽 `assume_init_drop`、在块边界用 `Box::from_raw` 释放块。它在 `&mut self` 下同样无并发，安全性论证与 ArrayQueue 一致：head→tail 之间的数据槽都是已初始化的。

#### 4.3.4 代码实践

**实践目标**：用一个带析构计数的类型，验证「队列被 drop 时，剩余元素恰好被 drop 一次」。

**操作步骤**：

1. 写一个用 `Arc<AtomicUsize>` 在 `Drop` 时自增计数器的类型：

   ```rust
   use std::sync::{Arc, atomic::{AtomicUsize, Ordering}};
   struct Counted(Arc<AtomicUsize>);
   impl Drop for Counted { fn drop(&mut self) { self.0.fetch_add(1, Ordering::SeqCst); } }
   ```

2. 往 `ArrayQueue::new(8)` 里 push 5 个 `Counted`，消费 2 个，然后让队列离开作用域被 drop。

3. 断言计数器最终 == 5（被消费的 2 个 + 被 drop 的 3 个，各 drop 一次）。

**需要观察的现象**：计数器恰好等于入队总数 5，没有重复也没有遗漏。

**预期结果**：验证了 `Drop` 的 `len` 算术正确、`assume_init_drop` 对每个剩余槽恰好调用一次。

**待本地验证**：请在本机运行确认计数。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `ArrayQueue::drop` 里的 `if mem::needs_drop::<T>()` 守卫删掉，对 `Vec<i32>` 元素会怎样？

**答案**：仍然正确。`needs_drop::<Vec<i32>>()` 为真，守卫有无都进入循环；区别只在 `needs_drop::<i32>()` 这种为假的类型——删守卫会多跑一次空循环，结果不变。所以守卫纯属性能优化。

**练习 2**：为什么 `Drop` 里可以直接用 `*self.head.get_mut()` 而不用 `load(Ordering::...)`？

**答案**：`drop(&mut self)` 持有独占引用，借用检查器保证此刻无其他线程访问；`AtomicUsize::get_mut()` 返回底层 `&mut usize`，把原子读写退化为普通整数读，既快又显然安全（详见 u2-l4）。

---

### 4.4 UnwindSafe / RefUnwindSafe

#### 4.4.1 概念说明

`UnwindSafe` 和 `RefUnwindSafe` 是两个与 `catch_unwind`（捕获 panic）相关的 auto trait。它们的设计目的是拦截一类常见 bug：**一个 `&mut T`（或基于 `UnsafeCell` 的共享可变状态）在「修改到一半」时被 panic 打断，然后被 `catch_unwind` 之外的代码观察到半成品状态**。因此默认情况下，`&mut T` 和含 `UnsafeCell` 的类型都被判定为 `!UnwindSafe` / `!RefUnwindSafe`，阻止你把它们跨 `catch_unwind` 边界传递。

`ArrayQueue` / `SegQueue` 内部含 `UnsafeCell<MaybeUninit<T>>`，按默认规则它们会是 `!RefUnwindSafe`。但 crossbeam 选择**无条件**手写实现：

```rust
impl<T> UnwindSafe for ArrayQueue<T> {}
impl<T> RefUnwindSafe for ArrayQueue<T> {}
```

注意 bound 是 `impl<T>`（对任意 T），而不是 `impl<T: UnwindSafe>`。这是**主动放弃** auto trait 的保守保护，向用户承诺「跨 `catch_unwind` 共享/持有这个队列是安全的」。

#### 4.4.2 核心流程

这个承诺靠三点支撑：

1. **公共操作不触发用户代码、不会半途 panic**：`push`/`pop`/`force_push` 内部只有原子读写、`MaybeUninit` 的按位 `write`/`read`/`replace` 和整数运算。按位搬运 T **不会**调用 T 的任何方法（T 的析构只在 `Drop` 或 `pop` 移出后由调用方触发）。因此一次 `push`/`pop` 要么完整完成、要么不开始，不存在「写了一半」的可见中间态。
2. **内部不变量对 panic 鲁棒**：即使假设某次操作中途发生了不可能发生的 panic，队列的并发不变量（stamp/state 状态机）依然可恢复——别的线程仍能正常 push/pop，因为不变量不依赖「单次操作必须完成」。
3. **T 自身可能 `!UnwindSafe`，但这无关紧要**：队列从不把内部的 T 引用暴露出去，T 只在入队/出队时被 move。跨 `catch_unwind` 持有整个队列，相当于持有这些 T 的所有权，是否会观察到「坏的 T」由 T 自己的 `UnwindSafe` 决定，而非由队列决定。

综合这三点，crossbeam 认为把队列标为无条件 `UnwindSafe`/`RefUnwindSafe` 是健全的，且符合「并发原语应当对 panic 友好」的工程惯例（`Mutex` 等也采用类似立场，只是 `Mutex` 因 poisoning 走了另一条路）。

#### 4.4.3 源码精读

[src/array_queue.rs:79-80](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L79-L80) — `ArrayQueue` 的两个 unwind trait，紧接 `Send/Sync` 之后：

```rust
impl<T> UnwindSafe for ArrayQueue<T> {}
impl<T> RefUnwindSafe for ArrayQueue<T> {}
```

[src/seg_queue.rs:175-176](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L175-L176) — `SegQueue` 同样无条件实现。

对照：[`new()` 里的 `assert!(cap > 0)`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L96-L97) 是构造期的 panic，发生在任何共享之前，不影响并发安全性——这印证了「运行期操作不会 panic」。

#### 4.4.4 代码实践

**实践目标**：验证队列可以安全地跨 `catch_unwind` 使用。

**操作步骤**：

1. 在 `catch_unwind` 闭包内持有一个 `ArrayQueue`，往里 push 一些值，在闭包内主动 `panic!()`。

   ```rust
   use crossbeam_queue::ArrayQueue;
   use std::panic::{catch_unwind, AssertUnwindSafe};
   let q = ArrayQueue::new(4);
   q.push(1).unwrap();
   let _ = catch_unwind(AssertUnwindSafe(|| {
       let q = &q;            // 共享引用跨边界
       q.push(2).unwrap();
       panic!("boom");
   }));
   // 队列在这里仍然可用，且其不变量完好
   assert_eq!(q.pop(), Some(1));
   assert_eq!(q.pop(), Some(2));   // panic 之前的 push 仍然生效
   ```

2. 尝试不带 `AssertUnwindSafe` 直接捕获 `&q`，观察编译器是否抱怨。

**需要观察的现象**：闭包内的 `push(2)` 在 panic 前已完成，所以 panic 之后仍能 pop 出 2；队列状态完好。

**预期结果**：因为 `RefUnwindSafe` 已被无条件实现，第 1 步能编译；若去掉手写 impl，第 2 步那种直接共享会被编译器拒绝（这正是 auto trait 的保守保护，crossbeam 主动放开了它）。

**待本地验证**：请在本机运行确认 pop 结果。

#### 4.4.5 小练习与答案

**练习 1**：为什么 impl 的 bound 是 `impl<T>` 而不是 `impl<T: UnwindSafe>`？如果写成后者会带来什么不便？

**答案**：写成 `impl<T: UnwindSafe>` 会让含 `!UnwindSafe` 元素的队列（如元素内部含 `RefCell` 等）自身也 `!UnwindSafe`，无法跨 `catch_unwind` 使用。但队列从不暴露内部 T 的引用，T 是否 `UnwindSafe` 与队列自身的 panic 安全性无关，所以无条件 `impl<T>` 既健全又更通用。

**练习 2**：`push` 过程中是否可能 panic？若可能，会破坏安全性吗？

**答案**：在当前实现里 `push` 不会调用任何可能 panic 的用户代码（按位 write 不调用 T 的方法），唯一可能的 panic 是 `new` 里的容量断言，发生在共享之前。因此运行期不存在「push 中途 panic」的可见路径，这正是无条件 `UnwindSafe` 成立的现实依据。

---

## 5. 综合实践

把本讲四个模块串起来：为 `ArrayQueue::pop` 中最关键的那行 `unsafe` 写一段合格的 `SAFETY` 注释。这是 crossbeam 源码里这行**目前缺失**正式 SAFETY 注释的地方（注意 `seg_queue.rs` 的 `IntoIter` 里反而有现成范例可参照，见 [src/seg_queue.rs:676-683](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L676-L683)）。

**实践目标**：为 [src/array_queue.rs:353](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L353) 的

```rust
let msg = unsafe { slot.value.get().read().assume_init() };
```

写一段 SAFETY 注释，论证：(a) 读到的一定是已初始化的值；(b) 不会有另一个消费者并发重复读；(c) stamp 的 Release store 如何与这里的读配合。

**操作步骤**：

1. 重读 `pop` 全函数（[src/array_queue.rs:318-380](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L318-L380)）与 `push_or_else` 的写值+发布（[src/array_queue.rs:163-170](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L163-L170)）。
2. 在副本里给该行加上如下风格的 SAFETY 注释（参照 crossbeam 仓库内既有注释风格）。

**参考答案**（你可以据此对照自己的版本）：

```rust
// SAFETY: 读取此槽是安全的，依据如下三条同时成立：
//
// (a) 独占读取（无 double-read）：我们刚刚通过了上方 `self.head.compare_exchange_weak(
//     head, new, SeqCst, Relaxed)` 的 Ok(_) 分支。head 的 CAS 是所有消费者对「下一个
//     可读槽」的互斥抢占——只有赢得这次 CAS 的线程才会执行本行 read()，因此不会有
//     第二个消费者并发读取同一个槽，read() 的按位移出不会与另一个 read 竞争。
//
// (b) 槽已初始化：本函数顶部以 `slot.stamp.load(Ordering::Acquire)`（line 330）读取
//     stamp，且仅当 `head + 1 == stamp` 时才进入本分支。生产者在 push 中遵循
//     「先 `slot.value.get().write(...)`、后 `slot.stamp.store(tail+1, Release)`」的顺序
//     （line 166-168）。我们 Acquire 读到的 stamp 值 `tail+1` 正是生产者 Release 写入的，
//     这建立了「生产者 write → 本线程 read」的 happens-before，故 slot.value 的字节
//     已被完整写入，assume_init() 的「已初始化」断言成立。
//
// (c) Release store 的配合：本行 read() 之后，我们在 line 354-355 执行
//     `slot.stamp.store(head + one_lap, Release)`，把该槽的 stamp 推到「下一圈可写」
//     状态。这个 Release store 与将来某生产者对该槽的 stamp 的 Acquire load 配对，
//     保证生产者下一圈的 write 发生在本消费者 read 完成之后，避免「读旧值被新写覆盖」
//     的竞态。stamp 既是「读权限令牌」又是 happens-before 的载体。
let msg = unsafe { slot.value.get().read().assume_init() };
```

**需要观察的现象**：写完注释后，回头检查 (a)(b)(c) 三条是否各自能从源码里指出对应的行号与内存序依据；任何一条若找不到源码支撑，说明论证有漏洞。

**预期结果**：你能用 stamp 的 `write→Release / Acquire→read→Release` 这条链，完整解释为什么这行 `unsafe` 在并发下依然健全。这正是本讲四个模块（Send/Sync、MaybeUninit、needs_drop、UnwindSafe）共同服务的最终目标：**让每一个 `unsafe` 都有可指认的不变量背书**。

---

## 6. 本讲小结

- `ArrayQueue`/`SegQueue` 因含 `UnsafeCell<MaybeUninit<T>>` 而是 `!Sync`，必须手写 `unsafe impl`；bound 只要 `T: Send`，因为队列是「所有权搬运管道」，同一 T 永不跨线程共享，故不需要 `T: Sync`。
- `MaybeUninit` 的健全性建立在「write→Release / Acquire→read」配对 + CAS 互斥之上：stamp（ArrayQueue）或 WRITE 位（SegQueue）既是状态机令牌，又是 happens-before 的载体。
- `read()`/`write()`/`replace()` 在本仓库中是裸指针的内建方法（等价于 `ptr::read`/`ptr::write`/`ptr::replace`），操作整个 `MaybeUninit<T>` 单元，再用 `assume_init()` 断言初始化。
- `Drop` 在 `&mut self` 下无需任何原子操作；`assume_init_drop` 的安全性来自 head→tail 区间算术的精确性，`needs_drop` 守卫只是跳过无 drop 胶水类型的空循环优化。
- `UnwindSafe`/`RefUnwindSafe` 被无条件 `impl<T>`，因为公共操作不触发可 panic 的用户代码，队列不变量对 panic 鲁棒——这是主动放弃 auto trait 保守保护的审慎选择。
- 验证本讲所有内存安全论证，最终都要落到 `miri` 多种子 / `loom` 上，x86 强模型会掩盖弱内存 bug（承接 u4-l1）。

---

## 7. 下一步学习建议

- **u4-l4（并发测试方法与可线性化验证）**：本讲论证了「应当健全」，下一讲教你如何用 `drops` 析构计数测试、`linearizable` 可线性化测试、`cfg!(miri)` 规模缩放，**实际验证**这些 unsafe 真的不出问题——把「纸面论证」变成「跑得过 miri」。
- **动手加深**：用 `cargo +nightly miri test -p crossbeam-queue` 跑一遍测试套件，观察 miri 是否对本讲涉及的 `read().assume_init()`、`assume_init_drop()`、`replace().assume_init()` 报错；如果全绿，说明本讲的论证与工具检查一致。
- **横向阅读**：对比 `crossbeam-epoch` 的 `Atomic<T>` 与 `crossbeam-utils` 的 `CachePadded`/`Backoff` 的 `unsafe impl`，体会 crossbeam 全家桶在 Send/Sync/UnwindSafe 上的一致立场。
