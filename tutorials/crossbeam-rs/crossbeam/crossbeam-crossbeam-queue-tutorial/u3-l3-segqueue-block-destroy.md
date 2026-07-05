# Block 的内存回收：destroy、DESTROY 位与零初始化分配

> 本讲属于「进阶：SegQueue 无界分段队列核心机制」单元，承接 [u3-l2](./u3-l2-segqueue-push-pop.md) 的 push/pop 主链路，专门解决一个被前几讲刻意推迟的问题：**当一个 Block 被消费殆尽后，谁来、何时、如何安全地把它归还给堆？**

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 SegQueue 在多消费者并发读取一个块的末尾槽时，**如何用单个 `DESTROY` 比特位**在线程之间安全地「交接」销毁职责，做到不重复释放、也不泄漏。
- 解释 `Block::new` 为什么能用一次 `allocate_zeroed` 就把整块（含 `AtomicPtr`、`AtomicUsize`、`MaybeUninit`）安全初始化。
- 区分 `wait_write` / `wait_next` 两种自旋等待各自等待的是什么、为什么都用 `snooze` 而非 `spin`。
- 说明独占引用下的 `destroy_mut` 为什么可以省掉全部 DESTROY 协调。

## 2. 前置知识

本讲默认你已经掌握：

- **Slot 状态位**：每个槽有一个 `AtomicUsize` 状态字，三个正交比特 `WRITE=1`、`READ=2`、`DESTROY=4`，可叠加（见 [u3-l1](./u3-l1-segqueue-block-structure.md)）。
- **块结构**：`Block<T>` 含 `next: AtomicPtr` 与 `slots: [Slot<T>; 31]`，每块恰好 31 个数据槽（`BLOCK_CAP=31`），索引 30 是最后一块数据槽。
- **push/pop 主链路**：消费者通过 CAS 抢到 head 指针后才能读某个槽，读前要 `wait_write` 等 WRITE 位（见 [u3-l2](./u3-l2-segqueue-push-pop.md)）。
- **原子读改写（RMW）**：`fetch_or` 是原子的「读-或-写」，返回旧值。本讲大量依赖它做无锁协调。
- **`MaybeUninit<T>`**：表示「可能未初始化」的内存，全零字节也是一种合法的未初始化状态。

一个核心直觉先放在这里：**释放一块被多线程共享的内存，最难的不是「释放」本身，而是确认「此刻没有任何线程还在读这块内存里的某个槽」。** 本讲 90% 的篇幅都在解决这一个问题。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/seg_queue.rs` | SegQueue 全部实现。本讲聚焦其中 `Block` 的四个关联函数：`new` / `wait_next` / `destroy` / `destroy_mut`，以及 `pop` / `pop_mut` 中调用它们的两处。 |
| `src/alloc_helper.rs` | crate 私有的分配器封装 `Global`，提供 `allocate` / `allocate_zeroed` / `deallocate`。`Block::new` 通过它做零初始化堆分配。 |
| `src/lib.rs` | 在 `#[cfg(all(feature = "alloc", target_has_atomic = "ptr"))]` 守卫下声明 `mod alloc_helper;`（[src/lib.rs:27](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/lib.rs#L27)）。 |
| `tests/seg_queue.rs` | 含 `drops`（析构计数）与 `stack_overflow`（大元素）测试，是验证回收正确性的活样本。 |

> 说明：`src/alloc_helper.rs` 中的 `use crate::alloc_helper::Global;`（[src/seg_queue.rs:15](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L15)）解析到的是 **crossbeam-queue crate 自带的** `alloc_helper` 模块。`crossbeam-utils` 里有一份逐字相同的副本（`crossbeam-utils/src/alloc_helper.rs`），那是给 utils 自己用的；本讲只读 queue 这一份。

---

## 4. 核心概念与源码讲解

### 4.1 块回收的难题与 DESTROY 协调协议

#### 4.1.1 概念说明

SegQueue 是一条块链表。消费者从 head 块逐槽弹出，当 head 块的 31 个数据槽全部被消费后，这块就「没用了」，应当归还给堆。难点在于：

- **谁释放？** 可能有多个消费者正在并发读同一块的不同槽。如果让「读到第 30 个槽的那个人」直接 `drop` 整块，而此时另一个线程还捏着第 5 个槽的引用正在 `read`，就会 **释放后使用（use-after-free）**。
- **只释放一次？** 如果两个线程都以为自己该释放，就会 **双重释放（double-free）**。
- **不漏掉？** 如果「该释放的人」提前走了，没人释放，就会 **内存泄漏**。

crossbeam 的解法叫 **DESTROY 位交接协议**：用一个 `DESTROY=4` 比特位，配合两次原子 `fetch_or`，在销毁者与消费者之间做一次确定性的「交接（handoff）」——**保证恰好有一个线程来完成最后的释放**。

#### 4.1.2 核心流程

一个块的销毁由「读到第 30 个槽（最后一个数据槽）的消费者」发起。它扫描块内尚未确认读完的槽，逐个协调：

```text
destroy(block, start):
    for i in start .. 30:                      # 注意：不含第 30 个槽
        slot = block.slots[i]
        if 该槽尚未 READ:
            原子地 fetch_or(DESTROY)
            若置 DESTROY 那一刻 READ 仍为 0:
                return                          # 把接力棒交给正在读这个槽的消费者
            # 否则：消费者已读完，继续下一个槽
    # 走到这里：所有槽都已读完，没人再用这块了
    drop(Box::from_raw(block))                  # 真正释放
```

**交接的关键**在于「销毁者置 DESTROY」与「消费者置 READ」这两个 `fetch_or` 是原子的，硬件会序列化它们。无论谁先，结果都唯一确定：

| 实际执行顺序 | 销毁者 `fetch_or(DESTROY)` 的返回值 | 消费者 `fetch_or(READ)` 的返回值 | 谁继续扫？ |
| --- | --- | --- | --- |
| 消费者先置 READ | `old & READ != 0` | — | 销毁者继续下一个槽 |
| 销毁者先置 DESTROY | `old & READ == 0`（销毁者 return） | `old & DESTROY != 0` | 消费者读完值后接管，从 `offset+1` 继续 |

无论哪种顺序，**恰好一方**会推进扫描：要么销毁者自己一路扫到底并释放，要么它在中途把接力棒交给某个正在读的消费者，由那位消费者读完值后继续。不会重复释放，也不会泄漏。

#### 4.1.3 源码精读

先看状态位常量定义（[src/seg_queue.rs:17-23](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L17-L23)）：

```rust
const WRITE: usize = 1;
const READ: usize = 2;
const DESTROY: usize = 4;
```

三个比特互相正交（`1 | 2 | 4 = 7`），可以同时存在于同一个 `state` 字里。

核心函数 `Block::destroy`（[src/seg_queue.rs:104-121](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L104-L121)）：

```rust
unsafe fn destroy(this: *mut Self, start: usize) {
    // 最后一个槽不需要置 DESTROY，因为它正是发起销毁的那个槽。
    for i in start..BLOCK_CAP - 1 {                 // start..30，跳过槽 30
        let slot = unsafe { (*this).slots.get_unchecked(i) };

        if slot.state.load(Ordering::Acquire) & READ == 0            // 廉价提示：可能还在用
            && slot.state.fetch_or(DESTROY, Ordering::AcqRel) & READ == 0
        {
            return;                                 // 接力棒交出，等消费者读完后续接
        }
    }
    drop(unsafe { Box::from_raw(this) });           // 全部读完，安全释放
}
```

逐行解读：

1. `for i in start..BLOCK_CAP - 1` 即 `start..30`。`BLOCK_CAP=31`，所以槽下标是 `0..=30`。**循环刻意不包含槽 30**——这是本讲实践任务要你解释的关键点（见 4.1.4）。
2. 第一处 `load(Acquire) & READ == 0` 是**廉价提示**：先普通读一眼，若 READ 已置位，就直接跳过，省掉一次 RMW。
3. `fetch_or(DESTROY, AcqRel)` 是**权威判定**：原子地把 DESTROY 或进去，并拿到旧值。若旧值里 READ 仍为 0，说明在「我置 DESTROY」这一瞬间，那个槽的消费者还没读完——于是 `return`，把扫到一半的工作交给它。
4. 走出循环才 `Box::from_raw` + `drop`，真正释放整块。

那么「接力棒」是怎么被接住的？看 `pop` 里的调用点（[src/seg_queue.rs:432-438](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L432-L438)）：

```rust
// 读出值之后：
if offset + 1 == BLOCK_CAP {            // 我读的是最后一个槽（offset==30）
    Block::destroy(block, 0);           // 我是发起者，从槽 0 开始扫
} else if slot.state.fetch_or(READ, Ordering::AcqRel) & DESTROY != 0 {
    Block::destroy(block, offset + 1);  // 发现 DESTROY 已被置 → 接力，从下一个槽继续
}
```

这正是上表第二行的「消费者接管」分支：消费者读完值后 `fetch_or(READ)`，若发现旧值里 DESTROY 已被置（说明有销毁者正在等它），就接过来从 `offset+1` 继续扫。

> **不变量**：销毁的**首次**触发永远来自「读到槽 30 的那个消费者」（`offset+1 == BLOCK_CAP` 分支，调 `destroy(block, 0)`）。第二个分支只会「续上」一个已经在进行中的销毁。因此当你看到任何 `destroy` 调用时，槽 30 必然已经被那个唯一的消费者读过了——这就是循环可以跳过槽 30 的根本依据。

#### 4.1.4 代码实践

**实践目标**：亲手讲清楚「为什么最后一个槽不需要置 DESTROY 位」，并用文字推演一次并发回收。

**操作步骤**：

1. 打开 [src/seg_queue.rs:104-121](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L104-L121)，定位 `for i in start..BLOCK_CAP - 1`。
2. 回答下面两个问题（写下来）：
   - 槽 30（`BLOCK_CAP - 1`）是被谁、在什么时刻读的？为什么它「不需要」与别的线程协调？
   - 假设有 3 个消费者 C1/C2/C3 几乎同时分别读完槽 28、29、30，最终是谁调用 `Box::from_raw` 把这块真正释放掉？画出可能的时序。
3. 用 `cargo test -p crossbeam-queue drops` 运行析构计数测试（[tests/seg_queue.rs:164-213](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/tests/seg_queue.rs#L164-L213)）观察它如何断言「push 进去的 `DropCounter` 最终全部被 drop 恰好一次」。

**需要观察的现象**：`drops` 测试在并发 pop/push 之后断言 `DROPS == steps`，再 `drop(q)` 之后断言 `DROPS == steps + additional`。这正是 DESTROY 协议正确性的直接证据——既不漏 drop（不泄漏），也不多 drop（不重复释放）。

**预期结果**：测试通过；你写出的时序应能说明「读到槽 30 的 C3 发起 `destroy(block, 0)`，扫到某个仍在读的槽时 return；那位读者读完值后接管并最终 `drop`」。**待本地验证**：在不同 CPU 核数下反复跑 `drops`，确认稳定通过。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `destroy` 里的 `fetch_or(DESTROY, AcqRel)` 换成「先 `load` 再 `store`」两步，会发生什么？

> **答案**：会引入 **TOCTOU 竞态**——在 `load` 与 `store` 之间，消费者可能刚好置了 READ 并离开，于是销毁者错以为「还有人占用」而 return，却再没有人来接力，最终**泄漏**这块内存。`fetch_or` 的原子性正是为了避免这个窗口。

**练习 2**：销毁者扫到槽 `i` 时发现 READ 已置位，于是跳过继续扫槽 `i+1`。这能保证槽 `i` 的消费者「此后绝不再访问这块内存」吗？

> **答案**：能。READ 由消费者在 `read().assume_init()` **之后**才 `fetch_or(READ)`（见 [src/seg_queue.rs:436](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L436)）。一旦销毁者通过 `fetch_or` 观察到 READ，依 Acquire/Release 配合，该消费者对槽内存的所有读操作都已发生在 READ 写入之前，故不会再访问。

---

### 4.2 Block::new 与零初始化堆分配

#### 4.2.1 概念说明

`Block::new` 要一次造出一个含 31 个 `Slot` 的大结构。常规写法 `Box::new(Block{...})` 会**先在栈上构造整个 Block 再移动到堆**——当 `T` 很大时（比如 32KB 的数组 ×31），这会撑爆线程栈。crossbeam 的做法是**直接在堆上分配一块全零内存**，然后把它「当成」已初始化的 `Block` 用。

这之所以合法，是因为 `Block` 的**每一个字段都允许全零字节**作为合法初值。注意源码注释里那段编号 SAFETY 论证（[src/seg_queue.rs:79-84](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L79-L84)），它是整个 unsafe 的核心依据。

#### 4.2.2 核心流程

```text
Block::new():
    layout = Layout::new::<Block>()          # 编译期算出大小与对齐
    断言 layout.size != 0                     # Block 必非零大小
    ptr = Global.allocate_zeroed(layout)     # 一次堆分配 + 清零
    match ptr:
        Some(p) -> Box::from_raw(p)          # 把裸指针「包」成 Box，接管所有权
        None   -> handle_alloc_error(layout) # 分配失败 → 终止进程（不 unwind）
```

`allocate_zeroed` 内部对非零大小的 layout 调用标准库的 `alloc::alloc::alloc_zeroed`，对零大小 layout 返回一个悬垂指针（但 Block 断言了非零，所以走不到那条分支）。

#### 4.2.3 源码精读

`Block::new`（[src/seg_queue.rs:74-90](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L74-L90)）：

```rust
const LAYOUT: Layout = {
    let layout = Layout::new::<Self>();
    assert!(layout.size() != 0, "Block should never be zero-sized, ...");
    layout
};

fn new() -> Box<Self> {
    match Global.allocate_zeroed(Self::LAYOUT) {
        Some(ptr) => unsafe { Box::from_raw(ptr.as_ptr().cast()) },
        None => handle_alloc_error(Self::LAYOUT),
    }
}
```

SAFETY 论证（节选自源码注释）逐条对应字段：

| 字段 | 类型 | 为什么全零合法 |
| --- | --- | --- |
| `next` | `AtomicPtr<Block<T>>` | 全零 = 空指针，是 `AtomicPtr` 的合法值（「还没有下一个块」）。 |
| `slots[i].value` | `UnsafeCell<MaybeUninit<T>>` | `MaybeUninit` **没有任何有效性约束**，全零就是一种合法的「未初始化」状态。 |
| `slots[i].state` | `AtomicUsize` | 全零 = 三个状态位（WRITE/READ/DESTROY）都为 0，即「未写、未读、未销毁」，合法初值。 |

> 注意第 76 行注释 `// unsafe { Box::new_zeroed().assume_init() } requires Rust 1.92`——这说明作者刻意没用不稳定/高版本的 API，而是手写 `allocate_zeroed` 来兼容较低 MSRV（1.60）。

再看 `Global::allocate_zeroed`（[src/alloc_helper.rs:44-46](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/alloc_helper.rs#L44-L46)），它委托给 `alloc_impl`（[src/alloc_helper.rs:12-34](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/alloc_helper.rs#L12-L34)）：

```rust
fn alloc_impl(&self, layout: Layout, zeroed: bool) -> Option<NonNull<u8>> {
    match layout.size() {
        0 => Some(dangling(layout)),                    // 零大小：返回对齐的悬垂指针
        _size => unsafe {
            let raw_ptr = if zeroed { alloc::alloc::alloc_zeroed(layout) }
                          else { alloc::alloc::alloc(layout) };
            NonNull::new(raw_ptr)                        // 分配失败 → None
        },
    }
}
```

- **零大小布局**特殊处理：返回一个对齐合法但不可解引用的「悬垂」指针（`dangling`，[src/alloc_helper.rs:16-19](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/alloc_helper.rs#L16-L19)）。`Block` 已断言非零，所以这条分支对 `Block::new` 不会命中，但 `allocate_zeroed` 作为通用原语必须正确处理它。
- 分配失败时返回 `None`，由 `Block::new` 调 `handle_alloc_error` 终止进程——这是 Rust 生态对「无法恢复的内存不足」的标准处理，**不会 unwind**（避免 unwind 越过 unsafe 边界）。

#### 4.2.4 代码实践

**实践目标**：亲眼看到「直接堆分配」如何避免大元素导致的栈溢出。

**操作步骤**：

1. 阅读 `stack_overflow` 测试（[tests/seg_queue.rs:239-250](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/tests/seg_queue.rs#L239-L250)）：它 push 一个含 `[_data: [u8; 32_768]]`（32KB）的元素。
2. 算一笔账：若用 `Box::new(Block{...})`，栈上临时量约 \( 31 \times 32768 \approx 1014 \text{ KB} \)，远超默认线程栈（通常 2MB 但实际可用更少，且 31 个大元素叠加极易触发保护页）。而 `allocate_zeroed` 直接在堆上落位，栈上只占一个裸指针。
3. 运行：`cargo test -p crossbeam-queue stack_overflow`。

**需要观察的现象**：测试通过、无栈溢出；元素被正确 push 并由 `into_iter` 消费。

**预期结果**：通过。这正是 0.3.12 版本修复的回归（把 Block 直接分配在堆上）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Block::LAYOUT` 用 `const { ... }` 而不是 `static` 或运行期计算？

> **答案**：`Layout::new::<Self>()` 是 `const fn`，在 `const` 块里求值可在编译期算出大小与对齐，零运行期开销；同时编译期 `assert!(size != 0)` 把「Block 绝非零大小」这一不变量固化在类型层面，越早失败越好。

**练习 2**：如果 `T` 是一个「全零字节不是合法值」的类型（例如含有裸 `&T` 引用），`allocate_zeroed` 还安全吗？

> **答案**：仍然安全——因为 `T` 被包在 `MaybeUninit<T>` 里，而 `MaybeUninit` **不要求**其内部 `T` 是合法值。在写入真正数据前，那块内存只是「未初始化的 `MaybeUninit`」，零字节与否都不违反任何不变量。生产者写入时用 `write` 重新覆盖，消费者读时用 `read().assume_init()` 并由 WRITE 位保证已初始化。

---

### 4.3 自旋等待：wait_write 与 wait_next

#### 4.3.1 概念说明

无锁队列里有两种「必须等别的线程把活干完」的场景：

- **等值写好**：消费者抢到了某个槽的 head 指针，但生产者可能还没来得及把值写进去、置 WRITE 位。消费者必须等 WRITE 出现才能 `read`。这就是 `Slot::wait_write`。
- **等下一块装好**：消费者读到块末尾（offset==30），需要跳到 `block.next` 指向的下一块；但生产者可能还在安装 next。消费者必须等 `next` 非空。这就是 `Block::wait_next`。

两者都**不是忙等死循环**，而是带退让的自旋——用 `Backoff::snooze()`，在等待的过程中逐步「让出」CPU。

#### 4.3.2 核心流程

```text
wait_write(self):          # Slot 方法
    loop load(state):
        if WRITE 已置位 -> return
        snooze()            # 退让，避免独占核心

wait_next(self) -> *mut Block:
    loop load(next):
        if next 非空 -> return next
        snooze()
```

为什么用 `snooze` 而非 `spin`？回顾 [u2-l2](./u2-l2-arrayqueue-push-pop.md) 的退避策略：`spin` 是纯忙等（适合「马上就好」的极短等待，如 CAS 失败重试）；`snooze` 在自旋若干轮后调用 `yield`，把时间片让给那个真正该干活的线程。等 WRITE/等 next 都属于「要等另一个线程完成一段稍重的工作」，所以用 `snooze`。

#### 4.3.3 源码精读

`Slot::wait_write`（[src/seg_queue.rs:43-51](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L43-L51)）：

```rust
fn wait_write(&self) {
    let backoff = Backoff::new();
    while self.state.load(Ordering::Acquire) & WRITE == 0 {
        backoff.snooze();
    }
}
```

- 用 `Acquire` 载入：与生产者 `fetch_or(WRITE, Release)`（[src/seg_queue.rs:281](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L281)）配对，确保消费者看到 WRITE 为 1 时，**也能看到此前 `write(value)` 写入的值**（发布-订阅关系）。
- 调用点在 `pop` 里 `read().assume_init()` 之前（[src/seg_queue.rs:429-430](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L429-L430)），是「抢到槽但值还没写好」竞态的唯一正确出口。

`Block::wait_next`（[src/seg_queue.rs:92-102](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L92-L102)）：

```rust
fn wait_next(&self) -> *mut Self {
    let backoff = Backoff::new();
    loop {
        let next = self.next.load(Ordering::Acquire);
        if !next.is_null() {
            return next;
        }
        backoff.snooze();
    }
}
```

- 调用点在 `pop` 处理块末尾时（[src/seg_queue.rs:417](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L417)）：消费者已经 CAS 推进了 head、确认要换块，但 `next` 可能尚未被生产者 `store`。
- `Acquire` 与生产者 `(*block).next.store(next_block, Release)`（[src/seg_queue.rs:275](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L275)）配对，保证看到 next 指针时也能看到新块里已写入的数据。

#### 4.3.4 代码实践

**实践目标**：体会 `wait_write` 在 SPSC 高速场景下的命中率。

**操作步骤**：

1. 阅读 `spsc` 测试（[tests/seg_queue.rs:101-126](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/tests/seg_queue.rs#L101-L126)）：单生产者狂 push、单消费者狂 pop。
2. 在本地 fork 一份 `seg_queue.rs`（仅本地实验，勿提交），在 `wait_write` 循环里加一个 `AtomicUsize` 计数，统计「自旋次数」。
3. 跑 `cargo test -p crossbeam-queue spsc -- --release`，读出计数。

**需要观察的现象**：在 release 优化下，`wait_write` 的自旋次数应**非常少**——因为生产者写值与置 WRITE 之间只隔几条指令，消费者几乎总是「来晚了」直接看到 WRITE。这佐证了 WRITE 协议的开销很低。

**预期结果**：每数万个元素只有极少数自旋。**待本地验证**：debug 构建下自旋可能明显增多，可对比 release。

#### 4.3.5 小练习与答案

**练习 1**：`wait_write` 里把 `Acquire` 换成 `Relaxed` 会出什么问题？

> **答案**：虽然能「看到 WRITE=1」，但失去了与生产者 `Release` 写值的 happens-before 关系，消费者可能读到**未初始化或半初始化**的值（数据竞争 + UB）。Acquire 是必需的。

**练习 2**：`wait_next` 是无限循环，会不会死锁？

> **答案**：在正常运行下不会。生产者在装好 next 后总会 `store`；只要队列还在被使用，next 最终非空。唯一的「永久阻塞」风险是生产者线程意外死亡且恰好卡在装 next 之前——这是无锁设计的固有取舍，调用方需保证生产者不会「半途永驻」。

---

### 4.4 destroy_mut：独占引用下的快速释放

#### 4.4.1 概念说明

回顾 [u2-l4](./u2-l4-arrayqueue-exclusive-mut.md) 的思想：当函数签名是 `&mut self` 时，Rust 借用检查器在编译期就排除了其他线程，于是所有原子协调都可以省掉。块回收也一样——`destroy_mut` 是 `destroy` 的「无并发」特化版：**不扫槽、不置 DESTROY、不交接，直接 `Box::from_raw` 释放**。

#### 4.4.2 核心流程

```text
destroy_mut(this):           # 仅在 &mut 下调用
    drop(Box::from_raw(this))   # 直接释放，无需任何协调
```

前提（源码契约）：**调用方必须保证此刻没有其他线程在用这块**。在 `pop_mut` 的单线程上下文里这天然成立。

#### 4.4.3 源码精读

`Block::destroy_mut`（[src/seg_queue.rs:123-126](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L123-L126)）：

```rust
/// Destroys the block. Only safe to call with exclusive access, when no other thread is using it.
unsafe fn destroy_mut(this: *mut Self) {
    drop(unsafe { Box::from_raw(this) });
}
```

调用点在 `pop_mut`（[src/seg_queue.rs:513-522](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L513-L522)）：

```rust
if offset + 1 == BLOCK_CAP {
    Block::destroy_mut(block);                       // 块末尾：直接释放
} else {
    let state = *(*block).slots.get_unchecked_mut(offset).state.get_mut();
    *(*block).slots.get_unchecked_mut(offset).state.get_mut() = state | READ;
    if state & DESTROY != 0 {                        // 兼容：若曾被并发置过 DESTROY
        Block::destroy(block, offset + 1);           // 仍走协调版收尾
    }
}
```

注意第二个分支里保留了 `if state & DESTROY != 0` 的检查——这是为了**与共享版混用时的安全兜底**（见 `exclusive_reference` 测试里 `push`/`pop` 与 `push_mut`/`pop_mut` 交替的写法，[tests/seg_queue.rs:54-99](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/tests/seg_queue.rs#L54-L99)）：只要遵守「`&mut` 与 `&` 借用不重叠」的铁律，纯独占路径下 DESTROY 永远是 0，走 `destroy_mut` 快路径。

#### 4.4.4 代码实践

**实践目标**：确认独占路径下 `destroy_mut` 的语义与共享版 `destroy` 等价。

**操作步骤**：

1. 阅读 `exclusive_reference` 测试（[tests/seg_queue.rs:54-99](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/tests/seg_queue.rs#L54-L99)）：它交替使用 `push_mut`/`pop_mut` 与 `push`/`pop`，并不断断言 `len()`。
2. 运行：`cargo test -p crossbeam-queue exclusive_reference`。

**需要观察的现象**：尽管 push/pop 总数远超 31（单块容量），队列会经历多次「换块 + 释放旧块」，`len()` 始终正确，无泄漏、无崩溃。

**预期结果**：测试通过，证明 `destroy_mut` 在独占换块时正确回收了每一块。

#### 4.4.5 小练习与答案

**练习 1**：`destroy_mut` 为什么连 `READ` 位都不用管，直接释放整块就安全？

> **答案**：因为 `&mut self` 保证了**全局唯一访问**——既没有别的消费者在读任何槽，`Drop`（`Box::from_raw` 触发）会自动 drop 掉 `Block` 内所有 `Slot` 的字段。注意 `Slot::value` 是 `MaybeUninit`，其 `Drop` 是 no-op，所以未读的槽不会误 drop 值；而 `pop_mut` 读出的值已由调用方持有，不会泄漏。

**练习 2**：`pop_mut` 的 else 分支里，为什么读完值后还要检查 `DESTROY` 并可能走共享版 `destroy`？

> **答案**：为了支持「`pop_mut` 之前曾被并发 `pop` 置过 DESTROY」的混用场景。虽然正确用法下不应出现，但保留这一兜底能让「不重叠借用」的约束即便在边界情况下也更稳健——若发现 DESTROY 已置，说明有个未完成的协调流程，应由当前持 `&mut` 的线程顺手收尾。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面这个「**回收全链路推演**」任务。

**场景**：一个 SegQueue，块容量 `BLOCK_CAP=31`。生产者已 push 了 62 个元素（即块 0 与块 1 各满），现在停止生产。有 4 个消费者并发 `pop` 把队列清空。

**任务**：

1. **画出块链表**：标出 block0、block1 的 `next` 指针、各 31 个槽的初始状态（全部 `WRITE=1, READ=0, DESTROY=0`）。
2. **标注发起者**：哪个消费者最先消费到 block0 的槽 30？它调用哪个函数、`start` 是几？为什么跳过槽 30？
3. **推演一次交接**：假设发起者扫到 block0 的槽 12 时，C2 还没读完槽 12。写出两个 `fetch_or` 的返回值，指出谁 `return`、谁后续接管、接管者从哪个 `start` 继续扫。
4. **确认终止**：block0 最终由谁调 `Box::from_raw` 释放？block1 呢（注意此时生产者已停，block1 不会有下一块）？
5. **验证**：用 `drops` 测试（[tests/seg_queue.rs:164-213](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/tests/seg_queue.rs#L164-L213)）的思路写一个最小测试：push 62 个 `DropCounter`，4 线程并发 pop 清空，最后断言 `DROPS == 62` 且队列已空。**待本地验证**：在 `cfg!(miri)` 下把数量缩小到 10 以内再跑。

**参考要点**：

- 步骤 2：发起者是「CAS 抢到 head 对应槽 30 的那个消费者」，调用 `Block::destroy(block0, 0)`；跳过槽 30 因为槽 30 正由它自己读过了，无需与他人协调（见 4.1 不变量）。
- 步骤 3：发起者 `fetch_or(DESTROY)` 返回 `old & READ == 0` → 它 `return`；C2 读完值后 `fetch_or(READ)` 返回 `old & DESTROY != 0` → C2 接管，调 `destroy(block0, 13)`。
- 步骤 4：block0 由「最后一个扫完的线程」（可能是 C2 或继续接力的某线程）`Box::from_raw` 释放；block1 同理，因无 block2，其 `next` 保持 null，被释放时无需等下一块。

---

## 6. 本讲小结

- SegQueue 的块回收用 **`DESTROY=4` 比特位 + 两次原子 `fetch_or`** 实现「销毁者↔消费者」的确定性交接，保证恰好一个线程完成释放，**不重复释放、不泄漏**。
- 销毁的首次发起者永远是「读到块内最后数据槽（槽 30）的那个消费者」，因此**槽 30 永远不需要置 DESTROY**——循环 `start..BLOCK_CAP-1` 刻意排除它。
- `Block::new` 用 `Global.allocate_zeroed` **直接在堆上落一块全零内存**，合法依据是 `AtomicPtr`(null)、`AtomicUsize`(0)、`MaybeUninit`(全零) 三类字段都允许全零初值；这同时避免了大元素导致的栈溢出。
- `wait_write` / `wait_next` 是两种带 `snooze` 退让的自旋，分别等「值写好」和「下一块装好」，都用 `Acquire` 与生产者的 `Release` 配对。
- `destroy_mut` 是 `destroy` 的独占特化版，省掉全部协调直接释放；`pop_mut` 仍保留 DESTROY 兜底以应对与共享版混用的边界。
- 分配失败走 `handle_alloc_error` 终止进程（不 unwind），符合 Rust 对不可恢复 OOM 的惯例。

## 7. 下一步学习建议

- **内存序的精确论证**：本讲多次用到 `Acquire`/`Release`/`AcqRel`，但刻意没展开「为什么不能用更弱的序」。这正是下一讲 [u4-l1 原子内存序与 fence 的运用](./u4-l1-atomic-orderings-fence.md) 的主题。
- **unsafe 安全性总论**：本讲的 `Box::from_raw`、`assume_init`、`get_unchecked` 都依赖不变量维持，[u4-l3 unsafe 的安全性论证与 MaybeUninit](./u4-l3-unsafe-maybeuninit-safety.md) 会系统论证 `Send/Sync` 与各处 unsafe。
- **并发测试方法**：本讲引用了 `drops` 测试，[u4-l4 并发测试方法与可线性化验证](./u4-l4-concurrency-testing.md) 会讲解 `linearizable` 等更严格的无锁正确性验证手段。
- **后续数据**：若你想看块回收在 `len` / `Drop` / `IntoIter` 中的另一面，可先读 [u3-l4 SegQueue 的 len、Drop 与迭代器](./u3-l4-segqueue-len-drop-iter.md)。
