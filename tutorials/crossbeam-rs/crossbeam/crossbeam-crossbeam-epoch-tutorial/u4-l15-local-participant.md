# Local：参与者结构与注册/计数

## 1. 本讲目标

在前两讲里，我们已经认识了用户视角的 `Collector`（`Arc<Global>` 的薄包装）和 `LocalHandle`（一张「会员卡」，内部只有一根 `*const Local` 指针）。本讲要顺着这根指针走进去，拆开 `Local` 这个**参与者（participant）**结构本身。

学完本讲，你应当能够：

- 说清 `Local` 的七个字段各自承担什么职责，以及为什么它必须用 `#[repr(C)]` 把 `entry` 放在首字段。
- 画出 `Local` 在堆上的字段布局，标注 `CachePadded` 与「跨线程字段 / 线程私有字段」的分界。
- 跟踪 `register` 的完整分配与插入链表流程：`Owned::new → into_shared → List::insert → LocalHandle`。
- 用一张状态转移表说清 `guard_count` 与 `handle_count` 这两个计数器分别由谁增减，以及在哪两种情况下会触发 `finalize`。
- 解释 `IsElement<Local>` 这个侵入式链表适配器的三个方法（`entry_of` / `element_of` / `finalize`）如何让 `Local` 既能挂进无锁链表、又能被延迟回收。

本讲只聚焦 `Local` 自身；`Global` 的整体结构（locals 链表 / queue / epoch）留给 u4-l16，epoch 推进与回收主链路留给 u5 单元。

## 2. 前置知识

阅读本讲前，你需要先理解以下概念（在前序讲义中已建立）：

- **EBR 与 pin/unpin**：线程访问共享数据前先 pin（钉住并快照当前全局 epoch），访问完 unpin。详见 u1-l1、u3-l9。
- **Guard 是 pin 的凭证**：`Guard` 内部只有一根 `*const Local` 指针，pin 可重入，`Guard::drop` 调用 `unpin`。详见 u3-l9。
- **Collector 与 LocalHandle 的引用关系**：`register()` 新建的 `Local` 会把传入 `Collector` 克隆一份存起来，所以 `drop(collector)` 后 `handle` 仍可用。详见 u4-l13。
- **`Owned<T>` 是堆上独占指针**：`Owned::new` 在堆上分配且对象不可移动，地址稳定。详见 u2-l6。
- **侵入式链表（intrusive list）的直觉**：节点自身内嵌一个「链表指针」字段，而不是把整个节点包进链表节点。这是无锁链表的常见写法，本讲会用到它的 `IsElement` 抽象，完整实现细节见 u6-l20。

本讲用到的两个 Rust 语言点，对不熟悉的者作一句解释：

- **`#[repr(C)]`**：强制结构体字段按声明顺序、无重排地布局。本讲依赖它做「`entry` 必须在偏移 0」的指针投射。
- **`UnsafeCell<T>` / `Cell<T>`**：`UnsafeCell` 是「内部可变性的原始砖块」，告诉编译器「这个值可能被别名修改」；`Cell` 是建立在它之上的、单线程安全可变的包装。`Local` 里跨线程读写的 `epoch` 用原子，而**只有拥有者线程才会动**的计数器用 `Cell`（见 4.1）。
- **`ManuallyDrop<T>`**：包裹后 `drop` 不会被自动调用，需要手动 `ptr::read` 取出再释放。本讲里它配合 `finalize` 的「按需释放 `Collector` 引用」。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `src/internal.rs` | 定义 `Local`（参与者）、`Global`（全局数据）、`Bag`/`SealedBag`（延迟闭包包），以及 `register`、`pin`、`unpin`、`acquire_handle`、`release_handle`、`finalize` 的实现。本讲的主战场。 |
| `src/sync/list.rs` | Michael 风格的无锁侵入式链表：`Entry`、`List`、`IsElement` trait、`insert`、`delete`、`Iter`。`Local` 正是借由它挂进 `Global` 的参与者链表。 |
| `src/collector.rs` | `Collector::register` 转发到 `Local::register`；`LocalHandle::drop` 调 `release_handle`。提供调用链的上游入口。 |
| `src/epoch.rs` | `Epoch` 与 `AtomicEpoch`，即 `Local::epoch` 字段的类型。本讲只用到「它是一个原子整数」这一层。 |

## 4. 核心概念与源码讲解

### 4.1 Local 的字段布局与 CachePadded

#### 4.1.1 概念说明

`Local` 是一个线程在一个 `Collector` 中的**参与者记录**。可以这样理解它的角色：每当某线程对某 `Collector` 调一次 `register()`，就在堆上诞生一个 `Local`，它要回答四个问题——

1. 我在链表里的位置是什么？（`entry`）
2. 我归哪个 `Collector` 管？（`collector`）
3. 我手头攒着哪些待回收的垃圾？（`bag`）
4. 我现在 pin 了几层？还有几张会员卡？当前 epoch 是多少？（计数器与 `epoch`）

关键设计约束：`Local` 必须**不可移动（immovable）**且**地址稳定**，因为它要作为节点挂进无锁链表，别的线程会握着指向它的指针去读它的 `epoch`。所以 `Local` 一经 `Owned::new` 在堆上分配，就永远待在那块内存里，直到 `finalize` 流程把它从链表摘除并最终回收。

#### 4.1.2 核心流程

`Local` 的字段可分为三类：

- **链表节点身份**：`entry`——它是 `Local` 挂进 `Global::locals` 链表的「钩子」。
- **线程私有字段**：`collector`、`bag`、`guard_count`、`handle_count`、`pin_count`——只有**拥有该 `Local` 的那一个线程**会读写它们（计数器因此可以用非原子的 `Cell`）。
- **跨线程热字段**：`epoch`——别的线程在 `try_advance` 中会遍历链表读取每个 `Local` 的 `epoch`，所以它必须用原子，并且用 `CachePadded` 隔离到独立缓存行，避免「拥有者线程频繁写计数器」与「别的线程频繁读 epoch」之间发生**伪共享（false sharing）**。

把 `entry` 放在**首字段**是一个刻意的布局决定，配合 `#[repr(C)]`，使得 `*const Local` 与 `*const Entry` 可以直接互转（见 4.4）。`list.rs` 的注释也点出了这一分层意图：

```rust
/// An Entry is accessed from multiple threads, so it would be beneficial to put it in a different
/// cache-line than thread-local data in terms of performance.
```

即：`entry`（多线程访问）与中间那批线程私有字段，最好别挤在同一缓存行上互相干扰。

#### 4.1.3 源码精读

`Local` 的定义在 [src/internal.rs:291-318](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L291-L318)：

```rust
#[repr(C)] // Note: `entry` must be the first field
pub(crate) struct Local {
    entry: Entry,
    collector: UnsafeCell<ManuallyDrop<Collector>>,
    pub(crate) bag: UnsafeCell<Bag>,
    guard_count: Cell<usize>,
    handle_count: Cell<usize>,
    pin_count: Cell<Wrapping<usize>>,
    epoch: CachePadded<AtomicEpoch>,
}
```

逐字段说明（对照注释）：

- `entry: Entry`——链表节点钩子。`Entry` 本身只含 `next: Atomic<Entry>`（见 [src/sync/list.rs:18-23](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/list.rs#L18-L23)），所以占一个指针字。
- `collector: UnsafeCell<ManuallyDrop<Collector>>`——指向所属 `Collector` 的引用。`Collector` 就是 `Arc<Global>`，所以这里实质是一根 `Arc` 指针。`ManuallyDrop` 表示这根引用**不在 `Local` 被 drop 时自动释放**，而是由 `finalize` 手动 `ptr::read` 取出再 drop（见 4.3）。`UnsafeCell` 给内部可变性。
- `bag: UnsafeCell<Bag>`——本线程的延迟闭包包，承接 u3-l10/u3-l11 的 `defer`。`Bag` 是 `[Deferred; MAX_OBJECTS] + len`，是 `Local` 中**体积最大**的字段。
- `guard_count: Cell<usize>`——当前活跃的 `Guard` 数量，即 pin 的「重入深度」。
- `handle_count: Cell<usize>`——当前活跃的 `LocalHandle` 数量，即「会员卡张数」。
- `pin_count: Cell<Wrapping<usize>>`——累计 pin 次数，仅用于「每 pin 128 次顺手 collect 一下」的节拍器（见 u5-l18）。
- `epoch: CachePadded<AtomicEpoch>`——本线程当前的本地 epoch（含 pinned 位）。这是唯一被其他线程读的字段，用 `CachePadded` 隔离缓存行。

关于体积：`Bag` 在非 sanitizer/miri 配置下是 64 个 `Deferred`，单个 `Deferred` 是「函数指针 + 3 个字的数据缓冲」（见 u3-l11），所以 `Local` 整体偏大。仓库里有一段**当前被注释掉**的大小检查 [src/internal.rs:320-330](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L320-L330)，原本想断言 `size_of::<Local>() <= 2048`，但因 `Bag` 体积问题被挂起（代码里留了 issue 链接 `#869`）。这是「字段布局是已知工程关切」的直接证据。

`Global` 作为参照，它的三个核心字段也在 [src/internal.rs:164-174](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L164-L174)：`locals: List<Local>`（参与者链表）、`queue: Queue<SealedBag>`（全局垃圾队列）、`epoch: CachePadded<AtomicEpoch>`（全局 epoch）。每个 `Local` 通过 `collector.global.locals` 挂在这条链表上。

#### 4.1.4 代码实践

**实践目标**：建立 `Local` 在堆上的布局直觉，区分「跨线程字段」与「线程私有字段」。

**操作步骤**：

1. 阅读上面的字段定义，画出一张布局草图（不必精确到字节）。建议格式如下（→ 表示按 `#[repr(C)]` 声明顺序排布，`‖` 表示 `CachePadded` 强制的缓存行边界）：

   ```
   堆上的 Local（repr(C)，地址稳定、不可移动）
   ┌─────────────────────────────┐  偏移 0（IsElement 投射点）
   │ entry      : Entry          │  ← 跨线程：别的线程通过它遍历/摘除节点
   ├─────────────────────────────┤
   │ collector  : Arc<Global> ptr│  ← 线程私有
   │ bag        : [Deferred;64]+ │  ← 线程私有（体积最大）
   │ guard_count: Cell<usize>    │  ← 线程私有
   │ handle_count: Cell<usize>   │  ← 线程私有
   │ pin_count  : Cell<usize>    │  ← 线程私有
   ├═════════════════════════════┤  ‖ CachePadded 边界（隔离缓存行）
   │ epoch      : AtomicEpoch    │  ← 跨线程热字段：try_advance 会读它
   └─────────────────────────────┘
   ```

2. 在草图上用两种颜色分别标出「跨线程访问」与「只有拥有者线程访问」的字段，体会 `CachePadded` 为什么只包住 `epoch`。

**需要观察的现象 / 预期结果**：你会清楚地看到，除 `entry` 与 `epoch` 外，其余五个字段都只被拥有者线程触碰——这正是它们能用 `Cell`（非原子）而非 `AtomicUsize` 的前提。若把 `guard_count` 改成跨线程读写却不加原子，就会构成数据竞争。

> 说明：上图中 `bag` 的具体字节数取决于 `MAX_OBJECTS`（64 或 sanitizer 下的 4）与 `Deferred` 的大小，本实践只需定性画出层次，**精确字节数待本地验证**（可用一个实验性 helper 打印 `size_of::<Bag>()`，但 `Local` 是 `pub(crate)` 私有类型，需在同 crate 内测量）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `guard_count` 用 `Cell<usize>` 而不是 `AtomicUsize`？

> **答**：因为 `guard_count` 只会被拥有该 `Local` 的那一个线程增减（pin/unpin 都在本线程发生），不存在跨线程并发访问，所以无需原子的同步开销。`Cell` 的「`!Sync`」正好表达「只能单线程使用」这一约束——编译器会阻止把 `Guard`（从而间接 `Local` 的计数器）跨线程共享。这与 u3-l9 中「`Guard` 因含裸指针而 `!Send + !Sync`」一脉相承。

**练习 2**：如果把 `#[repr(C)]` 去掉会发生什么？

> **答**：编译器可能重排字段，`entry` 不一定仍在偏移 0。这样 `IsElement::entry_of` 里「`local.cast::<Entry>()`」就会指向错误地址，链表彻底错乱。`#[repr(C)]` 是这段指针投射能成立的前提。

---

### 4.2 register：从 Owned 到链表节点再到 LocalHandle

#### 4.2.1 概念说明

`register` 是「让一个线程加入某个 `Collector`」的核心动作。它要完成三件事：在堆上造一个 `Local`、把它挂进 `Global::locals` 链表、返回一张指向它的 `LocalHandle`。

注意一个微妙的点：整个 `register` 过程用的是 `unprotected()` 假守卫（u3-l9 讲过的、`local` 为 null 的「不 pin」守卫）。原因正如源码注释所说——这段代码不解引用任何需要 epoch 保护的共享指针：新造的 `Local` 此刻还**未被其他线程可见**，对它的操作不需要 pin。

#### 4.2.2 核心流程

```
Collector::register(&self)               // collector.rs:47
        │
        ▼
Local::register(collector)               // internal.rs:338
        │
        │  1. Owned::new(Self { ... })   // 堆分配一个不可移动的 Local
        │       entry      = Entry::default()       // next = null
        │       collector  = collector.clone()      // Arc<Global> 引用 +1
        │       bag        = Bag::new()
        │       guard_count= 0
        │       handle_count = 1                     // 返回的 handle 预占一张
        │       pin_count   = 0
        │       epoch       = Epoch::starting()
        │
        │  2. .into_shared(unprotected())            // Owned → Shared<'static>
        │
        │  3. collector.global.locals.insert(local)  // CAS 挂到链表头部
        │
        ▼
LocalHandle { local: local.as_raw() }    // 裸指针，不持有 Arc
```

`handle_count` 一开始就设为 `1`，因为返回的这张 `LocalHandle` 就是第一张会员卡——计数器必须如实反映「当前有几张卡」。

#### 4.2.3 源码精读

上游入口在 [src/collector.rs:47-49](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/collector.rs#L47-L49)，只是简单转发：

```rust
pub fn register(&self) -> LocalHandle {
    Local::register(self)
}
```

真正的实现在 [src/internal.rs:337-357](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L337-L357)：

```rust
pub(crate) fn register(collector: &Collector) -> LocalHandle {
    unsafe {
        // Since we dereference no pointers in this block, it is safe to use `unprotected`.
        let local = Owned::new(Self {
            entry: Entry::default(),
            collector: UnsafeCell::new(ManuallyDrop::new(collector.clone())),
            bag: UnsafeCell::new(Bag::new()),
            guard_count: Cell::new(0),
            handle_count: Cell::new(1),
            pin_count: Cell::new(Wrapping(0)),
            epoch: CachePadded::new(AtomicEpoch::new(Epoch::starting())),
        })
        .into_shared(unprotected());
        collector.global.locals.insert(local, unprotected());
        LocalHandle { local: local.as_raw() }
    }
}
```

四个要点：

1. **`Owned::new` 堆分配**：`Local` 经 `Owned` 落在堆上，地址从此固定（满足 `insert` 要求「container is immovable, e.g. inside an `Owned`」的契约）。
2. **`collector.clone()`**：把传入 `Collector`（即 `Arc<Global>`）克隆一份存进 `Local`。这一步是 u4-l13 讲过的「`drop(collector)` 后 `handle` 仍可用」的根源——`Local` 自己也握着一根 `Arc`，`Global` 至少要等到所有 `Local` 销毁才会真正释放。
3. **`into_shared(unprotected())`**：把 `Owned`（独占）转成 `Shared<'static, Local>`。`unprotected()` 提供 `'static` 生命周期，让 `Shared` 不与任何真实 guard 绑定（这里本就没并发）。
4. **`locals.insert`**：把节点 CAS 挂到链表头部，详见下面链表侧的 [src/sync/list.rs:165-196](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/list.rs#L165-L196)。

链表的 `insert` 是一个标准的「读后缀 → CAS 改头指针」循环（Michael 风格）：

```rust
pub(crate) unsafe fn insert<'g>(&'g self, container: Shared<'g, T>, guard: &'g Guard) {
    let to = &self.head;
    let entry: *const Entry = C::entry_of(unsafe { container.as_ptr() }); // ← 取出内嵌 Entry
    let entry_ptr = Shared::from(entry);
    let mut next = to.load(Relaxed, guard);
    loop {
        unsafe { (*entry).next.store(next, Relaxed) }                 // 新节点后继 = 旧头
        match to.compare_exchange_weak(next, entry_ptr, Release, Relaxed, guard) {
            Ok(_) => break,
            Err(err) => next = err.current,                            // 输了重试
        }
    }
}
```

其中 `C::entry_of(container.as_ptr())` 这一行，就是把 `*const Local` 投射成 `*const Entry`——正是 4.4 要讲的 `IsElement` 在发挥作用。注意 `insert` 只把新节点接到链表里，**不分配、不回收任何节点**，所以它配合 `unprotected()` 是安全的。

最后，`LocalHandle { local: local.as_raw() }` 只存一根裸指针 [src/collector.rs:76-78](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/collector.rs#L76-L78)。`LocalHandle` 不增加 `Arc` 引用计数——它信任 `Local` 的 `handle_count` 已替它记账。

#### 4.2.4 代码实践

**实践目标**：确认「同一 `Collector` 在两个线程 register 出的两个 `Local` 挂在同一条 `locals` 链表上，且 `handle.collector()` 指向同一个 `Collector`」。

**操作步骤**（源码阅读 + 可选运行）：

1. 阅读 `src/collector.rs` 的测试 `pin_reentrant`（[src/collector.rs:134-151](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/collector.rs#L134-L151)），它演示了「先 `register`，再 `drop(collector)`，handle 仍能 pin」。
2. 在你自己的实验项目里写一段（**示例代码**，非项目原有）：

   ```rust
   use crossbeam_epoch::Collector;
   use crossbeam_utils::thread;

   let collector = Collector::new();
   thread::scope(|s| {
       for _ in 0..2 {
           s.spawn(|_| {
               let h1 = collector.register();
               let h2 = collector.register(); // 同一线程再注册一张卡
               // 两个 LocalHandle 指向不同 Local，但 collector() 相同
               assert!(h1.collector() == h2.collector());
               assert_eq!(h1.collector() == h2.collector(), true);
           });
       }
   }).unwrap();
   ```

3. （可选）`cargo test` 跑通后，把第 2 步里的断言改成一张表，记录「线程数 × 每线程 handle 数」与「collector 是否同一」的关系。

**需要观察的现象 / 预期结果**：不同线程、同一线程多次 `register` 产生的 `LocalHandle` 互不相同（指向不同 `Local`），但 `.collector()` 用 `PartialEq`（`Arc::ptr_eq`）比较都相等。这印证了「`Global` 只有一份，`Local` 每注册一次多一份」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `register` 用 `unprotected()` 而不是 `pin()`？

> **答**：因为这段代码不解引用任何「可能正被其他线程释放」的共享指针。新 `Local` 尚未挂进链表，对它的写入不存在与回收线程的竞争；`insert` 只对链表头做 CAS，也不需要 pin 保护。用 `unprotected()` 省去了一次真实的 pin/unpin 开销。源码注释「Since we dereference no pointers in this block, it is safe to use `unprotected`」正是此意。

**练习 2**：`handle_count` 为什么初始化为 `1` 而不是 `0`？

> **答**：因为 `register` 返回的这张 `LocalHandle` 本身就是第一张「会员卡」，计数器必须立即如实反映它的存在。若初始化为 `0`，则这张刚返回的 handle 一旦 drop，`release_handle` 会以为「没有卡了」而错误地触发 `finalize`。

---

### 4.3 双计数器：guard_count / handle_count 与 finalize 的两个触发点

#### 4.3.1 概念说明

`Local` 用**两个独立计数器**刻画自己的「是否还有人用」：

- `guard_count`——当前有几层 pin（几个活跃 `Guard`）。由 `pin` 增、`unpin` 减。
- `handle_count`——当前有几张 `LocalHandle`（几张会员卡）。由 `acquire_handle` 增、`release_handle` 减。

只有当**两个都归零**时，`Local` 才真正「无人使用」，此时调用 `finalize` 把它从链表摘除、释放残留垃圾、归还 `Arc<Global>` 引用。之所以要拆成两个计数器、且 `finalize` 有**两个触发点**，是因为「最后一个 guard 离开」与「最后一张会员卡离开」可能以任意顺序发生。

#### 4.3.2 核心流程

把两个计数器的所有变化点摊在一张表上（标 ★ 的会触发 `finalize`）：

| 操作（调用点） | guard_count | handle_count | 是否触发 finalize |
|---|---|---|---|
| `register` 返回 | 0 | 1 | 否 |
| `pin`（首个 guard）`internal.rs:401-462` | 0→1 | 不变 | 否 |
| `pin`（重入） | n→n+1 | 不变 | 否 |
| `unpin`（非末个 guard）`internal.rs:464-479` | n→n-1（≥1） | 不变 | 否 |
| `unpin`（末个 guard，且仍有卡） | 1→0 | >0 | 否 |
| `unpin`（末个 guard，且已无卡） | 1→0 | 0 | ★ 情况 A |
| `acquire_handle` `internal.rs:504-510` | 不变 | n→n+1 | 否 |
| `release_handle`（仍有 guard）`internal.rs:512-527` | >0 | n→n-1 | 否 |
| `release_handle`（无 guard，但卡>1） | 0 | n→n-1（≥1） | 否 |
| `release_handle`（无 guard，且末张卡） | 0 | 1→0 | ★ 情况 B |

`finalize` 的统一触发条件是：

\[
\text{guard\_count} == 0 \ \land\ \text{handle\_count} == 0
\]

两个触发点分别对应「谁先到零」：

- **情况 A**（在 `unpin` 中）：会员卡早已全部 drop，但仍有 guard 存活；当最后一个 guard 也 drop 时，由 `unpin` 触发 `finalize`。这发生于「`let g = handle.pin(); drop(handle); /* … */ drop(g);`」这类 handle 比 guard 先死的写法。
- **情况 B**（在 `release_handle` 中）：当前没有活跃 guard，最后一张会员卡 drop 时触发。这是最常见的「线程退出、TLS 里的 `LocalHandle` 自动 drop」路径（见 u4-l14 的默认收集器）。

无论哪条路径，最终都汇聚到同一个 `Local::finalize`，它做四件事：临时把 `handle_count` 抬到 1（防止内部的 `pin` 再次触发 `finalize`）→ pin 并把残留 `bag` 推入全局队列 → 把 `Arc<Global>` 引用 `ptr::read` 出来 → 在链表上标记删除并 `drop` 引用。

#### 4.3.3 源码精读

先看两个查询/访问方法。`is_pinned` 就是判 `guard_count > 0`（[src/internal.rs:371-375](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L371-L375)）：

```rust
pub(crate) fn is_pinned(&self) -> bool {
    self.guard_count.get() > 0
}
```

`pin` 只改 `guard_count`，并在「首个 guard」时才真正写 epoch + 屏障（[src/internal.rs:401-462](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L401-L462)），其计数关键行是：

```rust
let guard_count = self.guard_count.get();
self.guard_count.set(guard_count.checked_add(1).unwrap());
if guard_count == 0 { /* 真正的 pin：写 epoch、屏障、周期 collect */ }
```

`unpin` 则在「末个 guard」时清 epoch，并检查是否该 `finalize`（[src/internal.rs:464-479](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L464-L479)）：

```rust
pub(crate) fn unpin(&self) {
    let guard_count = self.guard_count.get();
    self.guard_count.set(guard_count - 1);
    if guard_count == 1 {
        self.epoch.store(Epoch::starting(), Ordering::Release);
        if self.handle_count.get() == 0 {
            unsafe { Self::finalize(self); }   // ← 情况 A
        }
    }
}
```

再看 handle 侧。`acquire_handle` / `release_handle` 分别在 `guard.rs` 的 `repin_after`（[src/guard.rs:383-388](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/guard.rs#L383-L388)）和 `LocalHandle::drop`（[src/collector.rs:100-107](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/collector.rs#L100-L107)）被调用。`release_handle` 的定义在 [src/internal.rs:512-527](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L512-L527)：

```rust
pub(crate) unsafe fn release_handle(this: *const Self) {
    let guard_count = unsafe { (*this).guard_count.get() };
    let handle_count = unsafe { (*this).handle_count.get() };
    unsafe { (*this).handle_count.set(handle_count - 1); }
    if guard_count == 0 && handle_count == 1 {
        unsafe { Self::finalize(this); }       // ← 情况 B
    }
}
```

注意它接收的是 `*const Self` 裸指针而非 `&self`——因为调用方 `LocalHandle::drop` 只握着 `*const Local`，且此时 `Local` 可能正在被销毁，借引用不安全。

最后是 `finalize` 本体 [src/internal.rs:529-569](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L529-L569)，核心步骤（精简）：

```rust
unsafe fn finalize(this: *const Self) {
    // 0. 断言两个计数都为 0
    // 1. 临时把 handle_count 设为 1，防止下面 pin() 里的 unpin 再次触发 finalize
    unsafe { (*this).handle_count.set(1); }
    {
        // 2. pin 并把残留 bag 推入全局队列（冲走本线程还没回收的垃圾）
        let guard = &(*this).pin();
        (*this).global().push_bag((*this).bag.with_mut(|b| &mut *b), guard);
    }
    unsafe { (*this).handle_count.set(0); }   // 3. 还原为 0
    unsafe {
        // 4. 取出 Arc<Global> 引用（必须在标记删除之前读，否则并发读者会读到悬垂指针）
        let collector: Collector = ptr::read((*this).collector.with(|c| &*(*c)));
        (*this).entry.delete(unprotected());  // 5. 链表上标记删除（next 的 tag=1）
        drop(collector);                       // 6. 归还引用，可能是最后一根 Arc → Global 真正销毁
    }
}
```

这里有两点尤其值得注意：

- **「临时抬到 1」**：第 1 步把 `handle_count` 设为 1，是为了让第 2 步 `pin()` 产生的 guard 在 drop 时走 `unpin`，看到 `handle_count > 0` 而**不再递归调用 `finalize`**。这是用计数器巧妙地防递归。
- **「先读引用再标记删除」**：第 4 步必须在第 5 步 `entry.delete` **之前**用 `ptr::read` 把 `Arc<Global>` 取走。注释明确说：「Since we're not protected by a guard at this time, it's crucial that the reference is read before marking the `Local` as deleted.」一旦标记删除，别的遍历线程可能随时把这块 `Local` 内存回收掉，届时再读就悬垂了。

> 关于第 5 步 `entry.delete`：它只是把节点的 `next` 指针 `fetch_or(1)` 标记为「逻辑删除」（[src/sync/list.rs:143-154](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/list.rs#L143-L154)），真正的「物理摘除 + 释放内存」要等到后续某次链表遍历（`Iter::next` 或 `List::drop`）调用 `IsElement::finalize`——这就是 4.4 的内容。

#### 4.3.4 代码实践

**实践目标**：用一段「让 guard 比 handle 先死」的程序，亲眼看一次「情况 A」的 finalize 路径。

**操作步骤**（**示例代码**，非项目原有；可用 `cargo --test` 或临时 example 验证）：

```rust
use crossbeam_epoch::{self as epoch, Collector};

let collector = Collector::new();
let handle = collector.register();
let guard = handle.pin();      // guard_count: 0→1
drop(handle);                  // release_handle：guard_count>0，不 finalize
// 此时 guard 仍存活，is_pinned() 仍为 true
assert!(epoch::is_pinned() ^ true || true); // 说明：is_pinned 走默认收集器，与自建 collector 无关
drop(guard);                   // unpin：guard_count 1→0 且 handle_count==0 → ★ 情况 A，触发 finalize
```

> 上面 `assert!` 那行只是占位说明——`epoch::is_pinned()` 查的是**默认收集器**的 TLS 状态（u4-l14），而自建 `collector` 与默认收集器是两个独立实例。要观察自建实例的 finalize，更稳妥的方式是阅读测试 `pin_reentrant`（[src/collector.rs:134-151](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/collector.rs#L134-L151)）与 `flush_local_bag`（[src/collector.rs:153-172](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/collector.rs#L153-L172)），它们正好演示了「drop(handle) 后 guard 仍可 flush」。

**需要观察的现象 / 预期结果**：程序正常退出、无 double-free、无泄漏。`drop(guard)` 之后再访问该 `Local` 就会悬垂——但因为我们不再访问，所以安全。finalize 会在内部把残留 `bag` 冲进全局队列，并最终归还 `Arc<Global>`，使 `Global` 在合适时机被销毁。

**若无法本地运行**：仅完成「源码阅读型实践」即可——逐行对照 4.3.2 的状态表，标注 `pin_reentrant` 测试每一步的两个计数器值。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `release_handle` 接收 `*const Self` 而不是 `&self`？

> **答**：因为调用方 `LocalHandle::drop`（[src/collector.rs:100-107](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/collector.rs#L100-L107)）只有 `*const Local` 裸指针；而且一旦进入 `finalize`，这块 `Local` 内存可能随后被释放，再以引用形式持有它不安全。用裸指针把「可能正在销毁」的语义显式表达出来。

**练习 2**：假设把 `finalize` 第 1 步「临时把 `handle_count` 设为 1」去掉，会发生什么？

> **答**：第 2 步 `let guard = &(*this).pin();` 产生的 guard 在语句块结束 drop 时会走 `unpin`，此时 `guard_count` 由 1 变 0、而 `handle_count` 仍是 0，于是 `unpin` 又会调用 `finalize`——形成无限递归（最终栈溢出）。临时抬到 1 正是为了打破这个递归。

---

### 4.4 IsElement<Local>：让 Local 成为链表元素

#### 4.4.1 概念说明

`sync::list::List<T, C: IsElement<T>>` 是一个泛型的无锁侵入式链表。它不规定「元素长什么样、Entry 藏在元素里的哪个位置」，而是把这个知识外包给一个 trait——`IsElement<T>`。任何想挂进 `List<T>` 的类型 `T`，只要实现 `IsElement`，告诉链表三件事：

1. `entry_of`：给定元素指针，它的 `Entry` 在哪？
2. `element_of`：给定 `Entry` 指针，反推出宿主元素指针。
3. `finalize`：当这个节点被链表物理摘除时，如何回收宿主元素的内存？

`Local` 实现了 `IsElement<Self>`，于是 `List<Local>` 才能成立（`Global.locals` 的类型正是 `List<Local>`，[src/internal.rs:167](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L167)）。

#### 4.4.2 核心流程

```
              挂入链表时
Local*  ──entry_of──►  Entry*   （正投射：偏移 0，repr(C)）

              遍历/摘除时
Entry*  ──element_of──► Local*  （反投射：偏移 0 的逆）

              物理摘除节点时
Entry*  ──IsElement::finalize──► guard.defer_destroy( Shared<Local> )
                                 （延迟回收整个 Local 的内存）
```

对 `Local` 而言，`entry` 是首字段且 `#[repr(C)]`，所以「偏移为 0」，`entry_of` 与 `element_of` 都只是 `pointer::cast`，零开销。

注意区分**两个同名的 `finalize`**：

- `Local::finalize(this: *const Self)`（4.3 讲的）——参与者主动「退会」：冲垃圾、标记删除、归还 `Arc`。
- `<Local as IsElement>::finalize(entry, guard)`（本节）——链表把节点**物理摘除**时的回调：`defer_destroy` 整个 `Local` 的内存。

前者只标记「逻辑删除」，后者才真正释放 `Local` 的堆内存，而且要等到宽限期之后。

#### 4.4.3 源码精读

`IsElement` trait 定义在 [src/sync/list.rs:70-95](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/list.rs#L70-L95)：

```rust
pub(crate) trait IsElement<T> {
    fn entry_of(_: *const T) -> *const Entry;
    unsafe fn element_of(_: *const Entry) -> *const T;
    unsafe fn finalize(_: *const Entry, _: &Guard);
}
```

`Local` 的实现 [src/internal.rs:572-586](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L572-L586)：

```rust
impl IsElement<Self> for Local {
    fn entry_of(local: *const Self) -> *const Entry {
        // SAFETY: `Local` is `repr(C)` and `entry` is the first field of it.
        local.cast::<Entry>()
    }

    unsafe fn element_of(entry: *const Entry) -> *const Self {
        // SAFETY: `Local` is `repr(C)` and `entry` is the first field of it.
        entry.cast::<Self>()
    }

    unsafe fn finalize(entry: *const Entry, guard: &Guard) {
        unsafe { guard.defer_destroy(Shared::from(Self::element_of(entry))) }
    }
}
```

三个方法各自的角色：

- `entry_of`：`insert` 用它从 `*const Local` 取出 `*const Entry`，才能把节点接到链表里。
- `element_of`：`Iter::next` 用它从当前 `Entry` 反推回 `*const Local`，把 `&Local` 交给上层（`try_advance` 就靠这个遍历每个参与者的 epoch）。
- `finalize`：当 `Iter::next` 物理摘除一个被标记删除的节点、或 `List::drop` 销毁整条链时调用（[src/sync/list.rs:231](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/list.rs#L231) 与 [src/sync/list.rs:263](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/list.rs#L263)），它 `defer_destroy` 整个 `Local`——即把「释放 `Local` 堆内存」这件事**推迟到宽限期之后**，避免误伤还在读它的并发遍历者。

`List::drop` 是理解这一点的另一面 [src/sync/list.rs:221-236](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/list.rs#L221-L236)：它断言「每个仍在链表里的节点的后继 tag 必为 1（已逻辑删除）」，然后对每个节点调 `C::finalize`。也就是说，`Global` 销毁时，所有未及时退会的 `Local` 内存也会经此路径被 `defer_destroy`。

#### 4.4.4 代码实践

**实践目标**：通过阅读链表测试，建立「`Entry` 内嵌 + `IsElement` 投射 + 延迟回收」三位一体的直觉。

**操作步骤**（源码阅读型）：

1. 读 `src/sync/list.rs` 中为 `Entry` 自己实现的 `IsElement<Self>`（[src/sync/list.rs:315-327](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/list.rs#L315-L327)）——因为 `Entry` 本身就是「自己即元素」，它的 `entry_of`/`element_of` 都是恒等投射。这是 `Local` 实现的「退化版镜像」，对照阅读能更好理解。
2. 读 `insert` 测试 [src/sync/list.rs:331-366](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/list.rs#L331-L366)：先 `register().pin()` 得到 guard，再 `Owned::new(Entry).into_shared(&guard)`，最后 `l.insert(...)`。这正是 `Local::register` 在「元素类型换成 `Local`」后的同构流程。
3. 在纸上把 `Local` 版本的流程画一遍：`Owned::new(Local{...}).into_shared(unprotected())` → `C::entry_of` 取 `Entry` → `insert` 的 CAS 循环 → `LocalHandle{ local: as_raw() }`。

**需要观察的现象 / 预期结果**：你会看到 `register` 与 `list::tests::insert` 在结构上几乎一一对应，唯一区别是 `Local` 版本多了「初始化七个字段」「返回 `LocalHandle`」「把 `Entry` 放在首字段以零开销投射」。

**若想动手**：可仿照 `list.rs` 测试里 `IsElement<Self> for Entry` 的写法，自己写一个含 `Entry` + 业务字段的结构（**示例代码**，非项目原有）：

```rust
struct Participant { entry: Entry, id: usize }
impl IsElement<Self> for Participant {
    fn entry_of(p: *const Self) -> *const Entry { p.cast() }
    unsafe fn element_of(e: *const Entry) -> *const Self { e.cast() }
    unsafe fn finalize(e: *const Entry, g: &Guard) {
        unsafe { g.defer_destroy(Shared::from(Self::element_of(e))) }
    }
}
```

（注意：`IsElement`、`Entry` 等均为 `pub(crate)` 私有，本例仅作示意，需在同 crate 内才能编译。）

#### 4.4.5 小练习与答案

**练习 1**：`entry_of` 与 `element_of` 为什么都是「零开销的 `cast`」？换个布局还能这样吗？

> **答**：因为 `Local` 是 `#[repr(C)]` 且 `entry` 是首字段，`Entry` 的地址与 `Local` 的地址完全相同，偏移为 0，所以两个方向都只是 `pointer::cast`，没有加减偏移的成本。若 `entry` 不是首字段（或去掉 `repr(C)` 导致重排），就必须用 `offset_of` 计算偏移来投射，既慢又易错。

**练习 2**：`<Local as IsElement>::finalize` 与 `Local::finalize` 各自负责什么？为什么缺一不可？

> **答**：`Local::finalize`（内部方法）是「退会」：冲走残留垃圾、在链表上**逻辑标记**删除、归还 `Arc<Global>` 引用——但它**不释放 `Local` 自身的堆内存**，因为可能还有并发遍历者在读它。`<Local as IsElement>::finalize` 是链表在**物理摘除**节点时调的回调：用 `defer_destroy` 把「释放 `Local` 内存」推迟到宽限期之后。前者管「逻辑生命」，后者管「物理生命」，二者配合才能既安全又无泄漏。

## 5. 综合实践

把本讲四个最小模块串起来的综合任务如下（即本讲指定的代码实践任务）：

**任务**：画出 `Local` 在内存中的字段布局图，并解释 `handle_count` 与 `guard_count` 的增减归属，以及 `finalize` 的两个触发点。

**步骤**：

1. **画布局图**（承接 4.1.4）：在一张图上画出 `Local` 的七个字段，标注：
   - `#[repr(C)]` 的作用范围（整个结构体按声明顺序布局）；
   - `entry` 位于偏移 0（`IsElement` 投射点）；
   - `CachePadded` 只包住 `epoch`，用于把它与线程私有字段隔离到不同缓存行；
   - 用两种颜色区分「跨线程字段（`entry`、`epoch`）」与「线程私有字段（其余五个）」。
2. **写计数器归属表**（承接 4.3.2）：列一张表，左列是 `guard_count` / `handle_count`，右列填「由谁增、由谁减」，并标出 `register` 时 `handle_count` 的初值 1。
3. **标 finalize 两个触发点**：在 4.3.2 的状态转移表上，用箭头分别标出「情况 A（在 `unpin` 中，末个 guard 且无卡）」与「情况 B（在 `release_handle` 中，无 guard 且末张卡）」，并说明它们最终都汇聚到同一个 `Local::finalize`。
4. **对照源码核验**：把你画的图与表，逐条对照 [src/internal.rs:291-318](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L291-L318)（字段）、[src/internal.rs:337-357](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L337-L357)（register）、[src/internal.rs:464-479](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L464-L479)（unpin/情况 A）、[src/internal.rs:512-527](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L512-L527)（release_handle/情况 B）核验一遍，确认没有遗漏。

**预期结果**：一张能独立讲清「`Local` 长什么样、谁在动它的两个计数器、什么时候退会」的图加表。这张图表将是你阅读 u4-l16（`Global` 与 Bag 队列）和 u5 单元（epoch 推进与回收主链路）时的「参与者侧」速查表。

## 6. 本讲小结

- `Local` 是「一个线程在一个 `Collector` 中的参与者记录」，堆分配、地址不可移动；七个字段分为「链表钩子 `entry`、线程私有五字段、跨线程热字段 `epoch`」三类。
- `#[repr(C)]` + `entry` 首字段，使得 `*const Local` 与 `*const Entry` 可零开销互转，这是侵入式链表 `IsElement` 投射的基石。
- 只有 `epoch` 用 `CachePadded` 包裹，把它与线程私有字段隔离到不同缓存行，避免伪共享；其余计数器因为是单线程访问而用 `Cell`。
- `register` 流程：`Owned::new(Local{…}) → into_shared(unprotected) → List::insert(CAS 挂头) → LocalHandle{ local: as_raw() }`，全程 `unprotected`，`handle_count` 初值为 1。
- 两个计数器各司其职：`guard_count` 由 `pin`/`unpin` 增减（pin 重入深度），`handle_count` 由 `acquire_handle`/`release_handle` 增减（会员卡张数）；二者皆归零才触发 `finalize`。
- `finalize` 有两个触发点——「末个 guard 且无卡」走 `unpin`、「无 guard 且末张卡」走 `release_handle`——最终汇聚到同一个 `finalize`，它冲残留垃圾、临时抬 `handle_count` 防 `pin` 递归、读出 `Arc<Global>` 后标记删除并归还引用。
- `<Local as IsElement>::finalize` 与内部 `Local::finalize` 是两件事：前者是链表物理摘除时的 `defer_destroy` 回收（延迟到宽限期后），后者是参与者主动退会的逻辑清理。

## 7. 下一步学习建议

- **u4-l16（Global 与 Bag 队列）**：本讲的 `Local` 通过 `collector.global.locals` 挂在 `Global` 上。下一讲会把 `Global` 的三件套（locals 链表 / queue 队列 / epoch）与 `SealedBag::is_expired` 的「两步宽限期」判据补齐，正好承接本讲 `finalize` 里 `push_bag` 入队的去向。
- **u5-l17（Epoch 表示）**：本讲反复提到的 `Epoch::starting()` / `pinned()` / `successor()` 的位编码细节下一讲会拆开讲。
- **u5-l18（pin/unpin 与内存屏障）**：本讲对 `pin` 只讲了「首个 guard 才真正写 epoch」，下一讲会展开 `SeqCst` 屏障与 x86 的 `compare_exchange` hack。
- **u6-l20（无锁侵入式链表）**：若你想彻底搞懂本讲 `List::insert` / `Iter::next` / 逻辑删除与物理摘除的两阶段，这是专家层的深入篇。
