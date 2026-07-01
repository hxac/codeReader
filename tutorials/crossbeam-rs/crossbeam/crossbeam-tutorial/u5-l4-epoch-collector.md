# Collector 与参与者注册

## 1. 本讲目标

上一讲（u5-l3）我们通过全局的 `epoch::pin()` 第一次拿到了 `Guard`，但刻意绕开了一个问题：`pin()` 背后到底“连着谁”？本讲就回答这个问题，拆解 crossbeam-epoch 的**句柄层**。

学完本讲你应该能够：

1. 说清 `Collector`、`LocalHandle`、`Global`、`Local` 这四个类型各自的职责与相互关系。
2. 描述一次 `register()` 是如何把一个参与者节点无锁地“挂”进全局链表的。
3. 读懂 `LocalHandle::pin()` 的三件事：可重入保护（`guard_count`）、记录局部 epoch 并执行 `SeqCst` fence、以及周期性触发垃圾回收。
4. 论证“为什么 pin 时必须有一条 `SeqCst` fence 才能保证回收安全”。

---

## 2. 前置知识

本讲假设你已读过 u5-l1～u5-l3，至少熟悉以下概念：

- **epoch 内存回收**：无锁数据结构删除节点时不能立即释放，要先标记当前 epoch、延迟两个 epoch 再销毁。安全判据是

  \[ \text{global\_epoch} - \text{bag\_epoch} \geq 2 \]

- **`Guard` 与 `pin`**：`Guard` 是“当前线程处于 pin 状态”的 RAII 凭证，`Drop` 时 unpin。pin 是可重入的，只有第一个 Guard 真正发布 epoch。

- **`Shared<'g, T>` 的生命周期 `'g`**：绑定到 `Guard`，从类型上保证读出的指针不逃出本次 pin。

- **侵入式链表（intrusive linked list）**：节点自身内嵌一个链表“挂钩”（`Entry`），而不是把节点整体塞进链表。crossbeam-epoch 用它来登记所有参与者。

如果你对“为什么要延迟两个 epoch 才安全”还存疑，请先回到 u5-l1 复习，本讲不再重复论证。

---

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| `crossbeam-epoch/src/collector.rs` | 句柄层公共 API：`Collector` 与 `LocalHandle`。 |
| `crossbeam-epoch/src/internal.rs` | 内部实现：全局状态 `Global` 与参与者 `Local`，含 `register`/`pin`/`unpin`/`finalize`。 |
| `crossbeam-epoch/src/epoch.rs` | `Epoch` 类型：`starting`/`pinned`/`successor` 等 epoch 编码。 |
| `crossbeam-epoch/src/sync/list.rs` | 无锁侵入式链表 `List`：`insert`/`delete`/`iter`。 |
| `crossbeam-epoch/src/default.rs` | 全局默认收集器与 `epoch::pin()` 入口（上一讲已读，本讲仅承接）。 |

一句话定位：`collector.rs` 是“对外卖的门面”，`internal.rs` 是“真正干活的核心”，两者通过一个裸指针 `*const Local` 串起来。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **4.1** 句柄层全景：`Collector` 与 `LocalHandle`
2. **4.2** `register`：把参与者插入全局链表
3. **4.3** `LocalHandle::pin`：可重入保护、记录 epoch、fence、周期 collect

### 4.1 句柄层全景：Collector 与 LocalHandle

#### 4.1.1 概念说明

crossbeam-epoch 要同时服务“所有线程共享”与“每个线程私有”两类状态，于是把它们拆成两层：

- **全局状态 `Global`**（共享）：参与者链表、全局垃圾队列、全局 epoch。所有线程读写同一份。
- **参与者状态 `Local`**（私有）：本线程的垃圾袋 `Bag`、局部 epoch、pin 计数等。每个参与者（通常是每个线程）一份。

但用户不该直接碰 `Global`/`Local`（它们是 `pub(crate)`），于是有了两个公共句柄类型：

- `Collector` —— 持有全局状态，本质就是一个引用计数的 `Global`。
- `LocalHandle` —— 持有一个参与者的引用，本质就是一个指向 `Local` 的裸指针。

这正好对应上一讲提到的“句柄层（`Collector`/`LocalHandle`）”。你可以把关系画成：

```
Collector ──Arc──▶ Global ──locals 链表──▶ [Local, Local, Local, ...]
                                                  ▲
LocalHandle ──*const Local────────────────────────┘
```

一个关键直觉：**`Collector` 是“共享”那一侧，`LocalHandle` 是“私有”那一侧**。`register()` 就是从共享侧“领”出一份私有侧状态。

#### 4.1.2 核心流程

```text
Collector::new()       → Arc::new(Global::new())              // 建一份全局状态
collector.register()   → Local::register(collector)           // 领一份私有状态
                       → 返回 LocalHandle { local: *const Local }

collector.clone()      → 克隆 Arc，指向同一个 Global
LocalHandle::pin()     → (*local).pin() → Guard               // 下一节细讲
LocalHandle.drop()     → Local::release_handle()              // 退订参与者
```

注意一个反直觉点（也是 `collector.rs` 顶部文档示例强调的）：**`drop(collector)` 之后 `handle` 依然能用**。原因在 4.2 节揭晓——`register` 时 `Local` 会持有一份 `Collector` 的克隆，把 Arc 引用计数顶住。

#### 4.1.3 源码精读

`Collector` 的定义极简，整个结构体只有一个字段：

[crossbeam-epoch/src/collector.rs:23-29](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/collector.rs#L23-L29) —— `Collector` 就是 `Arc<Global>` 的包装；`Global` 内部含裸指针与原子操作，Rust 无法自动推导 `Send`/`Sync`，故手动 `unsafe impl` 声明它可以跨线程共享。

构造走 `Default`，克隆就是克隆 Arc：

[crossbeam-epoch/src/collector.rs:31-44](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/collector.rs#L31-L44) —— `new()`/`default()` 调 `Arc::new(Global::new())`。

[crossbeam-epoch/src/collector.rs:52-59](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/collector.rs#L52-L59) —— `Clone` 只是再增加一个 Arc 强引用，两个 `Collector` 指向**同一个**全局状态。

两个 `Collector` 是否“同源”用 `Arc::ptr_eq` 判定：

[crossbeam-epoch/src/collector.rs:67-73](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/collector.rs#L67-L73) —— 这在并发数据结构里有用：可以校验“参与同一结构的所有 Guard 是否来自同一个 Collector”。

`LocalHandle` 同样只有一个字段——一个裸指针：

[crossbeam-epoch/src/collector.rs:76-98](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/collector.rs#L76-L98) —— `LocalHandle` 把所有操作代理给 `*const Local`；`pin()`/`is_pinned()`/`collector()` 都是对 `(*self.local)` 的转发。

再看 `Global` 与 `Local` 的字段布局，建立全景：

[crossbeam-epoch/src/internal.rs:164-174](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L164-L174) —— `Global` 三件套：侵入式参与者链表 `locals`、全局垃圾队列 `queue`、全局 epoch（用 `CachePadded` 隔离避免伪共享，呼应 u2-l2）。

[crossbeam-epoch/src/internal.rs:291-318](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L291-L318) —— `Local` 字段一览：链表挂钩 `entry`、回指的 `Collector`、本线程垃圾袋 `bag`、两个计数器 `guard_count`/`handle_count`、辅助计数 `pin_count`、局部 `epoch`。`#[repr(C)]` 保证 `entry` 是第 0 个字段（4.2 节用到）。

#### 4.1.4 代码实践

**实践目标**：验证 `Collector::clone` 共享同一份全局状态，以及“drop 原 Collector 后 handle 仍可用”。

**操作步骤**（新建一个临时 crate 依赖 `crossbeam-epoch`，或直接对照仓库内的 doctest）：

```rust
// 示例代码：非项目原有，用于理解
use crossbeam_epoch::Collector;

let c1 = Collector::new();
let c2 = c1.clone();
assert!(c1 == c2, "克隆出的两个 Collector 指向同一个 Global");

let handle = c1.register();
drop(c1);                  // 释放 c1 这个 Arc 引用
let guard = handle.pin();  // 依然能 pin —— 因为 Local 还持有一份 Collector 克隆
drop(guard);
drop(handle);
```

**预期结果**：`assert` 通过，`drop(c1)` 之后 `handle.pin()` 不 panic、不空指针。

**待本地验证**：若你用 `cargo test -p crossbeam-epoch` 跑 `pin_reentrant` 这个用例，它演示的正是“drop collector 后 handle 仍工作”这一约定（见下一节 4.3.4 引用的测试）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Collector` 要手写 `unsafe impl Send + Sync`，而不是让编译器自动推导？

**答案**：因为 `Global` 内部含 `List`（裸指针、`Atomic`）等无法自动证明线程安全的字段；语义上它确实是可跨线程共享的（参与者链表是无锁的、epoch 是原子），所以由实现者手动声明并承担安全责任。

**练习 2**：`LocalHandle` 里只存一个 `*const Local` 裸指针，它如何保证 `Local` 在自己存活期间不被回收？

**答案**：`Local` 内部有一个 `handle_count` 计数器（见 4.2），`register` 时设为 1，`LocalHandle::drop` 时减 1；归零且无 Guard 时才会从链表摘除并销毁。只要还有 `LocalHandle` 或 `Guard` 持有它，`Local` 就不会被 finalize。

---

### 4.2 register：把参与者插入全局链表

#### 4.2.1 概念说明

`register()` 解决的问题是：**全局状态需要一个“在册参与者名单”**。回收垃圾前，`try_advance` 必须遍历所有参与者，确认“当前 pin 着的人都在同一个 epoch”才能推进 epoch（u5-l5 详述）。这个名单就是 `Global::locals`——一条**无锁的侵入式单向链表**。

“侵入式”的含义：参与者节点 `Local` 自身内嵌一个链表挂钩 `entry: Entry`，链表通过这个挂钩把节点串起来，而不是把整个 `Local` 塞进链表容器。好处是：节点只需一次堆分配，且无需移动。

为了让链表能把“任意类型”串起来，crossbeam 用了一个 `IsElement<T>` trait，提供“从 `T` 取 `Entry` 指针”和“从 `Entry` 指针还原 `T`”两个换算。对 `Local` 的实现利用了 `#[repr(C)]` + “entry 是首字段”这一布局技巧：`*const Local` 与 `*const Entry` 在数值上相等，直接 `cast` 即可。

#### 4.2.2 核心流程

```text
Collector::register(&self)
  └─ Local::register(self):
       1. Owned::new(Local{ entry, collector: clone(self), bag, counts, epoch })
       2. .into_shared(unprotected())        // 转成 Shared 指针，用“假守卫”
       3. collector.global.locals.insert(local, unprotected())
            └─ CAS 循环：把新节点插到链表 head 之后（无锁）
       4. 返回 LocalHandle { local: local.as_raw() }
```

注意第 2、3 步用的是 `unprotected()`——“假守卫”。这看起来违反了“操作 `Atomic` 必须先 pin”的规矩，但这里安全：`register` 全程没有解引用任何可能被并发释放的指针，新分配的节点此刻没有别的线程看得到。

另一个细节：`Local` 的 `collector` 字段存的是 `collector.clone()`，这正是 4.1 里“drop 原 Collector 后 handle 仍可用”的原因——节点自己顶住了一份 Arc 引用。

#### 4.2.3 源码精读

公共入口只是一行转发：

[crossbeam-epoch/src/collector.rs:46-49](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/collector.rs#L46-L49) —— `Collector::register` 调 `Local::register(self)`。

`Local::register` 是本节主角：

[crossbeam-epoch/src/internal.rs:337-357](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L337-L357) —— 用 `Owned::new(...)` 在堆上造一个 `Local`（初始 `guard_count=0`、`handle_count=1`、`pin_count=0`、epoch 为 `starting()`），`collector` 字段克隆了一份 `Collector`（顶住 Arc 引用），然后 `collector.global.locals.insert(...)` 把它挂进链表，最后返回裸指针形式的 `LocalHandle`。注释说明全程不解引用指针，故可用 `unprotected()`。

链表插入的无锁 CAS 循环：

[crossbeam-epoch/src/sync/list.rs:165-196](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/sync/list.rs#L165-L196) —— `List::insert` 把新节点的 `next` 指向当前 head 的后继，再用 `compare_exchange_weak` 把 head 改成指向新节点；CAS 失败就更新后继重试。典型的“插到链表头部”无锁算法。

`IsElement<Local> for Local` 的换算：

[crossbeam-epoch/src/internal.rs:572-586](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L572-L586) —— 因为 `Local` 是 `#[repr(C)]` 且 `entry` 是首字段，`entry_of` 与 `element_of` 只是 `cast` 指针类型；`finalize` 给链表用，在节点被摘除后通过 Guard 延迟回收 `Local` 本身。

辅助理解 epoch 编码（`register` 里初始值为 `Epoch::starting()`）：

[crossbeam-epoch/src/epoch.rs:39-43](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/epoch.rs#L39-L43) —— `starting()` 返回 unpinned 的初始 epoch。

#### 4.2.4 代码实践

**实践目标**：通过源码阅读理解“侵入式链表如何用首字段布局省去偏移计算”，并估算多线程并发 register 时链表的形态。

**操作步骤**：

1. 打开 [crossbeam-epoch/src/internal.rs:572-586](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L572-L586)，确认 `entry_of` 只是 `local.cast::<Entry>()`——这依赖 `entry` 必须是 `Local` 的第 0 个字段，正是 `#[repr(C)]`（[internal.rs:292](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L292)）的用意。
2. 跟踪一次多线程 `register`：起 4 个线程，每个 `collector.register()` 一次。由于 `List::insert` 总是插到 head，最终链表顺序与注册顺序**相反**（后注册的在前）。这是无锁链表的常见特性，对正确性无影响（`try_advance` 只需遍历到所有节点，不关心顺序）。

**需要观察的现象**：注册是 O(1) 的 CAS；线程数增多不会让 `register` 退化成加锁等待。

**待本地验证**：可写一个微基准，比较 1 线程与 8 线程各 register 1000 次的耗时，预期增长平缓。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `register` 可以用 `unprotected()` 假守卫，而读取 `Atomic` 数据结构时必须真 pin？

**答案**：`register` 只操作“刚分配、尚未对其他线程可见”的新节点，不存在“被并发释放”的风险；而读取数据结构时，目标节点可能正被别的线程删除并延迟回收，必须 pin 才能保证读期间不被释放。

**练习 2**：`register` 返回的 `LocalHandle` 持有一个裸指针 `*const Local`。这个指针在什么情况下会失效？

**答案**：当 `handle_count` 与 `guard_count` 都归零（最后一个 `LocalHandle` drop 且无 Guard）时，`finalize` 会把节点从链表标记删除并最终回收。只要还存在存活的 `LocalHandle` 或 `Guard`，指针就有效。

---

### 4.3 LocalHandle::pin：可重入保护、记录 epoch、fence、周期 collect

#### 4.3.1 概念说明

`LocalHandle::pin()` 是本讲最核心、也是整个 epoch 机制“发布参与者身份”的地方。它一次完成四件事：

1. **可重入保护**：用 `guard_count` 计数当前线程叠了多少层 Guard。只有 0→1 的第一次 pin 才真正“发布 epoch”，1→0 的最后一次 unpin 才真正“撤回”，中间的嵌套 pin/unpin 只是改计数。
2. **记录局部 epoch**：pin 时把“全局当前 epoch + pinned 位”写进自己的 `Local::epoch`，等价于对外广播“我此刻在这个 epoch 读数据”。
3. **`SeqCst` fence**：在写完局部 epoch 之后插一条全屏障，保证“pin 之后对数据结构的所有读”不会重排到“发布 epoch”之前。**这条 fence 是回收安全的命门**。
4. **周期性回收**：用一个 `pin_count` 计数器，每 pin 满一定次数（128 次）顺带调一次 `global.collect()`，让垃圾回收搭便车。

为什么 pin 要“发布 epoch”？回忆 u5-l1 的安全判据 \(\text{global\_epoch} - \text{bag\_epoch} \geq 2\)：回收方只有确认“没有任何参与者还停在我要回收的那个旧 epoch”，才能销毁旧垃圾。参与者通过把自己写进 `Local::epoch`（pinned 位 + 全局 epoch 值）来宣告自己的存在，回收方在 `try_advance` 里遍历链表读取这个值。pin，就是“把自己登记进可见性系统”的动作。

#### 4.3.2 核心流程

`Local::pin()` 的逻辑（`guard_count == 0` 分支是关键）：

```text
guard_count = self.guard_count.get()
self.guard_count = guard_count + 1              # 可重入：计数 +1
if guard_count == 0:                             # 仅第一次 pin 做实质工作
    global_epoch = global.epoch.load(Relaxed)
    new_epoch    = global_epoch.pinned()         # 打上 pinned 位
    self.epoch.store(new_epoch, Relaxed)         # 发布“我在此 epoch”
    fence(SeqCst)                                # ← 安全命门（见 4.3.5）
    count = self.pin_count.get(); pin_count += 1
    if count % 128 == 0:
        global.collect(&guard)                   # 周期回收
return Guard { local: self }
```

对应的 unpin（`Guard::drop` 时触发）是对称的：

```text
guard_count = self.guard_count.get()
self.guard_count = guard_count - 1
if guard_count == 1:                             # 仅最后一次 unpin 做实质工作
    self.epoch.store(starting(), Release)        # 撤回：写回 unpinned
    if handle_count == 0: finalize(self)         # 连 handle 也没了，摘除节点
```

可重入的直观含义：连续两次 `handle.pin()` 只有第一次真正发布 epoch，两次 `drop(guard)` 只有第二次真正撤回——这就是 u5-l3 里“guard_count 驱动可重入 pin”的具体落点。

#### 4.3.3 源码精读

`LocalHandle::pin` 一行转发：

[crossbeam-epoch/src/collector.rs:81-85](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/collector.rs#L81-L85) —— 调 `(*self.local).pin()`。

`Local::pin` 全貌：

[crossbeam-epoch/src/internal.rs:401-462](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L401-L462) —— 先构造 `Guard` 并把 `guard_count` 加 1；`guard_count == 0`（即原本未 pin）时进入实质分支：读全局 epoch、算出 `new_epoch`、写入 `self.epoch`、执行 fence、累加 `pin_count`，并在每 128 次 pin 时调 `collect`。

`guard_count` 的可重入语义可见于 `is_pinned`：

[crossbeam-epoch/src/internal.rs:371-375](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L371-L375) —— `is_pinned` 就是判 `guard_count > 0`。这是从外部观察 `guard_count` 的唯一窗口（`guard_count` 本身是 `pub(crate)`，crate 外访问不到）。

epoch 的 `pinned` 编码：

[crossbeam-epoch/src/epoch.rs:62-68](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/epoch.rs#L62-L68) —— `pinned()` 把最低位置 1，表示“已 pin”；`Local::pin` 用它把全局 epoch 转成“本参与者的 pinned epoch”。

**x86 fence 优化**值得单独一看：

[crossbeam-epoch/src/internal.rs:416-448](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L416-L448) —— 在 x86/x86_64 上，代码不用 `atomic::fence(SeqCst)`（编译成 `mfence`），而是用 `compare_exchange(SeqCst, SeqCst)`（编译成 `lock cmpxchg`），同时顺手把 `new_epoch` 写进去。注释坦言这偏离了 C++ 内存模型对 SC fence 的严格定义，但基准测试表明它更快，并补一条 `compiler_fence(SeqCst)` 降低 LLVM 误优化风险。这是“先正确（语义等价于全屏障）、再榨性能”的工程取舍。

周期回收的触发点：

[crossbeam-epoch/src/internal.rs:450-458](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L450-L458) —— `PINNINGS_BETWEEN_COLLECT = 128`（定义在 [internal.rs:333-335](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L333-L335)）；注意判据用的是自增**前**的 `count`，所以第一次 pin（count=0）就会触发一次 `collect`，之后每 128 次再触发。

`collect` 与 `try_advance`（下一讲 u5-l5 的主角，这里先建立衔接）：

[crossbeam-epoch/src/internal.rs:207-226](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L207-L226) —— `collect` 标了 `#[cold]`（pin 很少真的走到它，编译器把它放冷路径）；它先 `try_advance` 推进 epoch，再从全局队列弹出并销毁已过期（`is_expired`）的垃圾袋，每次最多销毁 `COLLECT_STEPS = 8` 个。

[crossbeam-epoch/src/internal.rs:237-289](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L237-L289) —— `try_advance` 遍历 `self.locals` 链表，逐个读参与者的 `local_epoch`；只要存在“pin 着且 epoch 与全局不同”的参与者，就拒绝推进并返回当前 epoch。这正是 pin 时“发布 epoch”要被这里观察到的原因。

#### 4.3.4 代码实践

**实践目标**：用唯一的公开窗口 `is_pinned()` 观察 `guard_count` 的可重入变化。

`guard_count` 是 `pub(crate)`，crate 外无法直接读取，但 `LocalHandle::is_pinned()` 返回的就是 `guard_count > 0`，可以作为它的“一比特观测口”。仓库内已有现成用例 `pin_reentrant`：

[crossbeam-epoch/src/collector.rs:134-151](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/collector.rs#L134-L151) —— 该测试先 `assert!(!handle.is_pinned())`，进入第一层 `pin()` 后 `is_pinned()` 为真，进入第二层嵌套 `pin()` 仍为真，内层 Guard drop 后仍为真，外层 Guard drop 后才回到假。

**操作步骤**：

1. 运行 `cargo test -p crossbeam-epoch pin_reentrant`，确认它通过。
2. 仿照它写一段（示例代码，非项目原有）：

   ```rust
   let collector = crossbeam_epoch::Collector::new();
   let handle = collector.register();
   assert!(!handle.is_pinned());            // guard_count == 0
   let g1 = handle.pin();
   assert!(handle.is_pinned());             // guard_count == 1
   let g2 = handle.pin();
   assert!(handle.is_pinned());             // guard_count == 2，但 epoch 只发布过一次
   drop(g2);
   assert!(handle.is_pinned());             // guard_count == 1，仍未撤回
   drop(g1);
   assert!(!handle.is_pinned());            // guard_count == 0，真正 unpin
   ```

**需要观察的现象**：嵌套 pin 不会重复“发布 epoch”，第二次 `pin()` 不触发 `guard_count == 0` 分支；只有最外层 Guard drop 时才真正把 `self.epoch` 写回 `starting()`。

**预期结果**：上面每条 `assert` 都成立。

#### 4.3.5 小练习与答案

**练习 1（本讲核心论证）**：为什么 `Local::pin` 在写完 `self.epoch` 之后必须有一条 `SeqCst` fence？去掉它会怎样？

**答案**：pin 的语义是“从这一刻起，我读数据结构；删除方在回收我可能正在读的节点前，必须先观察到我 pin 在该 epoch”。fence 阻止 CPU/编译器把“pin 之后对数据结构的读”重排到“写 `self.epoch`（发布 pin）”之前。若去掉：回收方（`try_advance`）可能先看到“没人 pin 在旧 epoch”，于是推进 epoch、销毁旧节点；而本线程的读却被重排到发布 pin 之前，实际读到了已释放内存 → use-after-free。这条 fence 把“发布可见性”和“后续读取”钉成全局可见的先后顺序，是回收安全的命门。

**练习 2**：`pin()` 里每 128 次才调一次 `collect()`，会不会导致垃圾迟迟不被回收？

**答案**：不会成为正确性问题，只影响回收及时性。`collect` 是“搭便车”式的渐进回收：它每次最多销毁 8 个垃圾袋，靠多次 pin 累积推进；最坏情况下垃圾延迟回收，但安全性判据 \(\geq 2\) 个 epoch 仍成立。若需要立即回收，可手动调 `Guard::flush()` 触发 `collect`。

**练习 3**：`unpin` 在 `guard_count == 1`（最后一个 Guard）时把 `self.epoch` 写回 `starting()`。这一步用 `Release` 序而不是 `SeqCst`，够吗？

**答案**：够。撤回 pin 只需保证“本线程此前的读”不漏到撤回之后（由 `Release` 保证），不需要像 pin 那样再拦住“之后的读”——撤回之后本线程不再持有受保护引用。`try_advance` 用 `Relaxed` 读各参与者 epoch（配合其自身的 `SeqCst`/`Acquire` fence），所以 `Release` 已足够建立同步。

---

## 5. 综合实践

本任务把本讲三个模块串起来，对应规格要求的实践。

**任务**：创建一个自定义 `Collector`，注册两个参与者并各自 pin，用公开接口观察 `guard_count` 的可重入变化，并论证 fence 的安全作用。

**操作步骤**（示例代码，非项目原有；可放进一个依赖 `crossbeam-epoch` 的临时 crate 的 `tests/` 下）：

```rust
use crossbeam_epoch::Collector;

let collector = Collector::new();          // 4.1：建一份 Global

// 4.2：注册两个参与者（两个 LocalHandle 指向各自的 Local，共享同一个 Global）
let h1 = collector.register();
let h2 = collector.register();
assert!(h1.collector() == h2.collector()); // 同源校验（Arc::ptr_eq）

// 4.3：各自 pin，通过 is_pinned() 观察 guard_count
let g1 = h1.pin();
let g1b = h1.pin();            // 可重入：h1 的 guard_count == 2
assert!(h1.is_pinned() && h2.is_pinned() == false);
let g2 = h2.pin();
assert!(h2.is_pinned());

drop(g1b); drop(g1); drop(g2); // 逐层 unpin
assert!(!h1.is_pinned() && !h2.is_pinned());
```

**需要观察/回答的问题**：

1. `h1.collector() == h2.collector()` 是否成立？（成立——两个 handle 共享同一 `Global`，见 4.1。）
2. `h1` 嵌套 pin 时，`is_pinned()` 从第一次 pin 起就为真，与 `guard_count` 的具体值无关（只能观察到“是否 > 0”）。请对照 [internal.rs:401-462](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L401-L462) 解释：第二次 `pin()` 走的是 `guard_count != 0` 分支，不会再次发布 epoch。
3. **fence 安全论证**：参照 4.3.5 练习 1，写一段话说明——若 `g1` pin 之后没有 `SeqCst` fence，另一个线程的 `try_advance`（[internal.rs:237-289](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L237-L289)）可能误判“无人在旧 epoch”而推进 epoch 并销毁 `g1` 正在读的节点。

**预期结果**：所有 `assert` 通过；能用自己的话复述 fence 的安全作用。

**待本地验证**：把上面代码写成 `#[test]` 用 `cargo test` 运行；fence 的安全性无法用普通测试“观测”，需通过阅读源码与 4.3.5 的论证理解（仓库另用 miri/tsan/loom 验证，见 u7-l3）。

---

## 6. 本讲小结

- `Collector` 本质是 `Arc<Global>`，`LocalHandle` 本质是指向 `Local` 的裸指针；二者构成 epoch 的“句柄层”，把共享的 `Global` 与私有的 `Local` 隔离开。
- `register()` 用 `Owned::new(Local)` 在堆上造一个参与者节点，借 `#[repr(C)]`+首字段布局把它无锁 CAS 插入 `Global::locals` 侵入式链表；节点自带一份 `Collector` 克隆，故 drop 原 Collector 后 handle 仍可用。
- `LocalHandle::pin()` 做四件事：`guard_count++` 可重入保护、仅 0→1 时记录 `pinned` epoch、插 `SeqCst` fence（x86 上用 `lock cmpxchg` 优化）、每 128 次 pin 周期性 `collect`。
- `is_pinned()` 是 crate 外观察 `guard_count` 的唯一窗口（`guard_count` 本身 `pub(crate)`）。
- pin 后的 `SeqCst` fence 是回收安全的命门：它保证“发布 pin”对回收方的 `try_advance` 全局可见于“后续对数据结构的读”，杜绝 use-after-free。

---

## 7. 下一步学习建议

本讲把“参与者如何登记与发布 epoch”讲完了，但故意把 `try_advance` 与 `collect` 的内部细节留到了下一讲。

- **下一讲 u5-l5《internal：全局状态、epoch 推进与垃圾回收》**：精读 `internal.rs` 的 `Global`/`Local` 字段协作、`try_advance` 如何遍历链表判定“可否推进”、以及 `Bag`/`SealedBag` 如何落实“延迟两个 epoch 才销毁”。本讲已多次引用这些函数，下一讲会把它们彻底讲透。
- **横向衔接**：本讲的 `Collector`/`LocalHandle` 是 u6（crossbeam-deque）与 u7（crossbeam-skiplist）创建自定义收集器的入口——当数据结构想拥有“独立、随结构销毁而清空”的垃圾队列时，就会 `Collector::new()` 而非用全局默认收集器。
- **建议阅读的源码**：在进入 u5-l5 前，可先通读 [internal.rs:164-289](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/internal.rs#L164-L289)（`Global` 的全部方法），带着“本讲里 pin 发布的 epoch，是如何被 try_advance 读到的”这个问题去读，会顺畅很多。
