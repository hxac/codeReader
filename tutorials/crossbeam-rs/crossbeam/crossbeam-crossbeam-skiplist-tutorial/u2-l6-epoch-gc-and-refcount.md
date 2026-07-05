# epoch 内存回收与引用计数

## 1. 本讲目标

上一讲（u2-l5）我们看清了一个跳表节点在内存里长什么样，并且埋下一个伏笔：当 `refs_and_height` 的高位（引用计数）归零时，节点会被「安排回收」——但没有展开「安排回收」到底意味着什么。本讲就来回答这个核心问题：**在无锁并发下，一个被摘除的节点究竟什么时候、由谁、用什么机制安全地释放掉？**

学完本讲，你应当能够：

- 解释为什么无锁数据结构**不能像普通 Rust 代码那样直接 `free`** 一个被摘除的节点，并说出两类「悬空访问」风险（use-after-free 与 double-free）。
- 说出 crossbeam-epoch 的四个核心类型 `Atomic` / `Shared` / `Guard` / `Collector` 各自扮演的角色，以及它们与「引用计数」如何分工协作。
- 精读 `Node::try_increment`（获取引用，拒绝已经归零的节点）、`Node::finalize`（析构键值 + 释放内存）、`NodeRef::decrement` / `decrement_with_pin`（递减并在归零时延迟回收）这四段代码，并能解释 `fetch_sub(Release)` + `fence(Acquire)` 这一经典引用计数回收序。
- 解释 `SkipList` 为什么要把一个 `Collector` 存成字段、`check_guard` 为什么强制要求传入的 `Guard` 来自同一个 `Collector`。
- 解释 `insert` / `remove` 等方法为什么带有 `K: 'static + V: 'static` 约束。

本讲**只讲内存回收协议本身**，不展开搜索/插入/删除算法的并发逻辑（那是第三单元），也不深入内存序的全部细节（留到 u5-l17）——但会讲清 `Release`/`Acquire` 栅栏在「安全回收」里的最小作用。

## 2. 前置知识

在进入源码前，先建立四个直觉。

### 2.1 无锁结构为什么不能直接 `free`

考虑 `lib.rs` 文档里举的经典场景（见 4.1 节源码）：

1. 线程 A 调用 `get`，拿到了指向某个节点的指针；
2. 线程 B 把这个 key 从跳表里**摘除**了；
3. 线程 A 仍然用手里那个指针去访问节点的 key/value。

如果「摘除」就立即释放内存，线程 A 第 3 步就踩到了**已释放内存**——这就是 **use-after-free**，属于未定义行为。

更隐蔽的是反过来：如果 A 和 B 同时都想释放同一个节点，就会**释放两次**——这就是 **double-free**，同样是未定义行为。

普通单线程 Rust 靠所有权和借用检查在编译期杜绝这两类问题。但无锁结构里，多个线程同时持有指向同一节点的**裸指针**，编译器帮不上忙，必须由运行时机制来保证「没有人还会访问它」之后才能释放。这个机制就是本讲的主角：**基于 epoch 的内存回收（epoch-based reclamation, EBR）+ 引用计数（reference counting）**。

### 2.2 两类「还可能访问它」的人，需要两套防护

一个被摘除的节点，可能被两类人继续访问：

| 谁 | 持续多久 | 用什么防护 |
|----|----------|-----------|
| **临时读者**：某个线程在「临界区」里从 `Atomic` 指针加载出了一个 `Shared`，正在顺着它读节点 | 只在临界区内（很短，通常一次函数调用） | **epoch（`Guard`）** |
| **长期句柄**：用户拿到的 `Entry` / `RefEntry`，可能跨过很多次临界区、甚至存到结构体字段里长期持有 | 任意长，直到用户主动释放 | **引用计数** |

关键认识：**epoch 和引用计数不是二选一，而是叠加的两道闸门**。一个节点要被真正销毁，必须同时满足：

- 它的**引用计数已经归零**（没有任何 `Entry`/`RefEntry` 句柄持有它）；
- 并且**全局 epoch 已经推进**到「所有当时正在临界区里的临时读者都已退出」之后（没有任何线程还捏着一个加载出来的 `Shared`）。

引用计数归零时，节点被**挂进垃圾袋**（`defer_unchecked`）；epoch 推进时，垃圾袋里的节点才被**真正析构**（`finalize`）。本讲的核心就是把这两步讲透。

### 2.3 引用计数：经典的「最后一个走的人关灯」

如果你用过 `Arc`，本讲的引用计数逻辑你会很亲切。`Arc::clone` 把计数加一，`Arc::drop` 把计数减一；当某次 `drop` 把计数从 1 减到 0 时，**只有这一次**会触发析构。判定方法是看 `fetch_sub` 返回的**旧值**：

\[ \text{旧值} == 1 \quad\Longleftrightarrow\quad \text{这次递减把它从 } 1 \text{ 减到 } 0 \]

用「旧值是否等于 1」而不是「新值是否等于 0」来判定，是为了在并发递减下保证**恰好一个线程**赢得析构权，避免 double-free。`crossbeam-skiplist` 的 `NodeRef::decrement` 用的正是同一个套路，只不过计数是塞在 `refs_and_height` 的高位里（回顾 u2-l5）。

### 2.4 crossbeam-epoch 速览

本讲会反复出现 `crossbeam-epoch` 的四个类型。先建立直觉即可，细节会在 4.2 节结合源码展开：

- **`Atomic<T>`**：一个可以被原子替换的指针「槽位」。跳表塔里的每个指针就是 `Atomic<Node>`。
- **`Shared<'g, T>`**：从 `Atomic` 加载出来的「非拥有指针」，生命周期绑定到 `Guard`（`'g`）。它还带 2 个 tag 位，本讲不展开（删除标记留到 u3-l9）。
- **`Guard`**：一次「把当前线程钉住（pin）」的凭证。持有 `Guard` 期间，本线程看到的 epoch 被冻结，**这一刻产生的垃圾不会被回收**。`Guard` 也是投递延迟任务的句柄（`defer_unchecked`）。
- **`Collector`**：一个全局的 epoch 时钟 + 垃圾袋。`SkipList` 自己持有一个。

## 3. 本讲源码地图

本讲几乎全部内容集中在一个文件里：

| 文件 | 作用 | 本讲关注点 |
|------|------|-----------|
| `src/base.rs` | 底层无锁跳表的全部实现 | `Node::try_increment` / `Node::finalize` / `NodeRef::decrement` / `NodeRef::decrement_with_pin` / `SkipList::collector` / `SkipList::check_guard` |
| `src/lib.rs` | crate 顶层文档 | 「Garbage collection」章节对回收机制的对外说明 |

`base.rs` 顶部的导入已经把 crossbeam-epoch 的四个类型一次性引入，这是我们整个回收机制的入口：

[base.rs:15](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L15) —— `use crossbeam_epoch::{self as epoch, Atomic, Collector, Guard, Shared};`，把 epoch 模块别名为 `epoch`，并直接引入 `Atomic / Collector / Guard / Shared` 四个类型。

## 4. 核心概念与源码讲解

### 4.1 问题动机：为什么需要专门的回收机制

#### 4.1.1 概念说明

本节不引入新源码，而是先把「为什么不能直接 free」这件事用 `lib.rs` 的官方叙述钉死，作为后续所有机制的动机。`crossbeam-skiplist` 在顶层文档里专门用一节「Garbage collection」解释了它采用 EBR 的原因——这正是本讲要深入的那套机制对用户的「对外承诺」。

#### 4.1.2 核心流程

文档把 use-after-free 的产生过程拆成三步：

1. 线程 A 调用 `get`，持有对某个 value 的引用；
2. 线程 B 删除该 key；
3. 线程 A 再去访问该 value。

如果删除即释放，第 3 步就是内存损坏。解决方案是：**被删除的值，要等到所有对它的引用都消失后才释放**——这套机制只作用于「map 内部的值」，类似 Java 的 GC，但完全自动、用户无需操心；唯一要注意的是：**持有 `Entry` 句柄会推迟对应内存的释放**。

#### 4.1.3 源码精读

[lib.rs:102-125](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/lib.rs#L102-L125) ——「Garbage collection」章节。开头点明并发数据结构的两大难题是 use-after-free 和 double-free（两者都是 UB）；中段给出上面三步场景；末段承诺采用 `crossbeam-epoch` 的 EBR，并提醒「持有 `Entry` 会阻止内存被释放」。这段文档是本讲所有源码机制的「需求来源」。

#### 4.1.4 代码实践

**实践目标**：把文档的三步场景变成你自己的语言，确认你真的理解了「为什么需要延迟回收」。

**操作步骤**：
1. 打开 [lib.rs:102-125](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/lib.rs#L102-L125)。
2. 用你自己的话写下：如果删除即释放，线程 A 在第 3 步具体会发生什么？为什么 Rust 的借用检查器拦不住？
3. 再写一句：为什么「持有 `Entry` 会阻止释放」这件事，恰好是解决上面问题的关键？

**预期结果**：你能清楚地表达「借用检查器看不到跨线程的裸指针别名，所以必须在运行时保证安全」。

#### 4.1.5 小练习与答案

**练习 1**：文档说这套机制「类似 Java 的 GC，但只作用于 map 内部的值」。它和真正的 GC 最关键的区别是什么？

**答案**：它只回收跳表内部的节点内存，且依赖 epoch 推进 + 引用计数两个明确触发点，而不是追踪整个堆的对象图；因此它没有「stop-the-world」，延迟也可预测（一次 `Guard` 的生命周期 + 一次 epoch 推进）。

---

### 4.2 crossbeam-epoch 的四大类型：Atomic / Shared / Guard / Collector

#### 4.2.1 概念说明

要读懂 `decrement` 里的 `guard.defer_unchecked(...)`，必须先明白 `Guard` 是什么、它和 `Collector` 是什么关系。本节把这四个类型在跳表里的具体用法对照清楚，为 4.3–4.6 节铺垫。

#### 4.2.2 核心流程

四个类型的关系可以这样串起来：

```
Collector（全局时钟 + 垃圾袋）
   │  .pin() 得到一个 ──┐
   ▼                    │
 （epoch 推进）         Guard（钉住当前线程的凭证）
                        │  持有它时：Atomic.load(guard) → Shared
                        │           guard.defer_unchecked(closure)
                        ▼
                      Shared（非拥有指针，绑定 Guard 生命周期）
```

- `Atomic<T>` 是**槽位**：跳表塔的每一层就是一个 `Atomic<Node>`，存着「这一层下一个节点是谁」。
- `Shared<'g, T>` 是**读出来的快照**：在 `Guard` 存活期间从 `Atomic` 加载得到，生命周期不能超过那个 `Guard`。
- `Guard` 是**读写的通行证 + 投递垃圾的句柄**：所有 `Atomic` 操作都要带一个 `&Guard`；`defer_unchecked` 也挂在它上面。
- `Collector` 是**时钟**：决定垃圾什么时候真正倒掉。

#### 4.2.3 源码精读

[base.rs:129-145](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L129-L145) —— `Node` 结构体，其末尾的 `tower: Tower<K, V>` 就是一组 `Atomic<Node<K, V>>`（见 [base.rs:32-39](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L32-L39)）。这是 `Atomic` 在跳表里的物理存在形式。

[base.rs:283-290](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L283-L290) —— `NodeRef::from_shared`，把一个 epoch 的 `Shared<'a, Node>` 转成 `NodeRef`。这正体现了 `Shared` 是「从 `Atomic` 加载出来的、绑定 `Guard` 的指针」。

[base.rs:472-473](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L472-L473) —— `SkipList` 把 `Collector` 存为字段：`collector: Collector`。底层 `base::SkipList` 要求**外部传入** `Collector`（见 [base.rs:487-505](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L487-L505) 的 `new` / `with_comparator`），这样它能在 `no_std + alloc` 下工作；高层 `SkipMap` 则传 `epoch::default_collector().clone()`（详见 u4-l14）。

#### 4.2.4 代码实践

**实践目标**：建立对四个类型「在源码里长什么样」的直观印象。

**操作步骤**：
1. 在 `src/base.rs` 里搜索 `Shared<`、`Atomic<`、`&Guard`、`epoch::` 这些字样，统计它们各出现多少次、分别用在什么场景（加载指针？投递垃圾？标记 tag？）。
2. 特别留意 `epoch::unprotected()` 出现的地方（如 [base.rs:333-337](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L333-L337)），它是一种「不需要 `Guard` 保护」的特殊用法（前提是确信没有并发访问，详见 u5-l17）。

**预期结果**：你能用一句话说出 `Guard` 在跳表里同时承担的两个职责（临界区凭证 + 垃圾投递句柄）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Shared` 要带一个生命周期 `'g`，而 `Atomic` 不带？

**答案**：`Atomic` 是长期存在的「槽位」，生命周期跟跳表一样长；`Shared` 是在某个 `Guard`（`'g`）存活期间从槽位里**读出来**的快照，只能在该 `Guard` 保护下使用——一旦 `Guard` 释放，对应 epoch 的垃圾可能被回收，`Shared` 就可能悬空。所以把它的生命周期绑到 `Guard` 上，交给借用检查器保证它不会被用过头。

---

### 4.3 引用计数 acquire：`Node::try_increment`

#### 4.3.1 概念说明

一个节点被「长期持有」（即跨越 `Guard` 的生命周期，比如包进 `RefEntry` 返回给用户）之前，必须先把它的引用计数加一，确保在持有期间它不会被回收。这件事由 `Node::try_increment` 完成。

但「加一」并不总是成功：如果节点的引用计数**已经是 0**，说明它已经被某次 `decrement` 安排进了垃圾袋、即将销毁。此时再去加一、再去持有它，就等于从垃圾袋里抢回一个即将被 `finalize` 的节点——最终会导致 double-free。所以 `try_increment` 必须在「计数非零」时才允许加一，否则返回 `false`，让调用方重试搜索。

#### 4.3.2 核心流程

`try_increment` 是一个 CAS（compare-and-swap）循环：

```
读 refs_and_height（Relaxed）
loop {
    若高位（引用计数）== 0  → return false   // 已进垃圾袋，拒绝
    new = old + (1 << HEIGHT_BITS)            // 给高位加一（不碰低位的高度）
    若 new 溢出 → panic
    CAS(old → new):
        成功 → return true
        失败 → old = 读到的新值, 重试
}
```

注意它只动**高位**（加 `1 << HEIGHT_BITS`），低位的高度字段原封不动——回顾 u2-l5，引用计数和高度共享同一个 `AtomicUsize`。

#### 4.3.3 源码精读

[base.rs:214-249](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L214-L249) —— `Node::try_increment`。三处要点：

- [base.rs:229](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L229) —— `if refs_and_height & !HEIGHT_MASK == 0` 把高位（引用计数）取出来判零；为 0 就直接返回 `false`，注释明确写道「Incrementing it again could lead to a double-free」。
- [base.rs:234-236](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L234-L236) —— `checked_add(1 << HEIGHT_BITS).expect(...)`，用 `checked_add` 防止引用计数溢出。
- [base.rs:239-247](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L239-L247) —— `compare_exchange_weak` 在并发下可能失败，失败就用读到的新值重试。

它的两个核心调用点也值得一看：

- [base.rs:1670-1682](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1670-L1682) —— `RefEntry::try_acquire`，把一个 `NodeRef` 升级成长期持有的 `RefEntry`；`try_increment` 失败就返回 `None`，让上层（如 `remove`、`get`）重试搜索。
- [base.rs:1713-1724](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1713-L1724) —— `RefEntry::clone`，克隆一个 `RefEntry` 时调用 `try_increment`；注释指出此时必定成功（因为「我自己已经持有一份引用」，计数必然 > 0）。

#### 4.3.4 代码实践

**实践目标**：手算一遍 `try_increment` 的 CAS，确认你理解「拒绝归零节点」的逻辑。

**操作步骤**：
1. 假设某节点 `refs_and_height = 34`（即 `(34 >> 5) = 1` 份引用、低位 `34 & 31 = 2` 即高度 3）。
2. 手动模拟一次成功的 `try_increment`：旧值是多少？新值是多少？返回什么？
3. 再假设 `refs_and_height = 2`（引用计数为 0、高度 3），模拟一次 `try_increment`，返回什么？为什么？
4. （进阶）打开 `tests/map.rs` 或 `tests/base.rs`，找到任何一个并发 `insert` 的测试，确认它在并发下依赖 `try_increment` 的「失败即重试」来保证拿到有效节点。

**预期结果**：第 2 步得到新值 `34 + 32 = 66`、返回 `true`；第 3 步直接返回 `false`。

**待本地验证**：第 4 步的具体测试名以你本地仓库为准。

#### 4.3.5 小练习与答案

**练习 1**：`try_increment` 用的两个 `Ordering` 都是 `Relaxed`（[base.rs:242-243](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L242-L243)），为什么这里不需要更强的内存序？

**答案**：因为引用计数的正确性靠「CAS 本身的原子性 + `decrement` 那一侧的 `Release`/`Acquire` 栅栏」共同保证，而不靠单次 `load`/`CAS` 的强内存序。这里只要保证「我看到的那一份 old 值与 CAS 比较的那一份是同一个」即可，`Relaxed` 足够；真正的回收同步发生在 `decrement` 归零那一刻（见 4.4 节）。这正是 `Arc` 里同样的写法。

**练习 2**：为什么 `RefEntry::clone` 调用 `try_increment` 时「必定成功」，而 `RefEntry::try_acquire` 却要处理失败？

**答案**：`clone` 的前提是「已经存在一个存活的 `RefEntry`」，说明计数此刻必然 ≥ 1（否则它自己就不该存在），所以从非零加到非零一定成功。而 `try_acquire` 接收的是一个**刚从跳表里搜出来的、尚未持有长期引用**的 `NodeRef`——在我们拿到它和调用 `try_increment` 之间，别的线程可能已经把它递减到 0 并丢进垃圾袋，所以必须处理失败。

---

### 4.4 引用计数 release 与延迟回收：`NodeRef::decrement` / `decrement_with_pin` 与 `Node::finalize`

#### 4.4.1 概念说明

这是本讲最核心的一节。当一份长期引用（`RefEntry`、或节点在某一层被链接占用的那份计数）被释放时，要调用 `decrement` 把计数减一。如果这次递减恰好把计数从 1 减到 0，本线程就「赢得了回收权」，需要把这个节点**投递到垃圾袋**，让它在 epoch 推进后被析构。

注意：**归零并不立即析构**。归零只是说「没有长期句柄持有它了」，但还可能有「正在临界区里的临时读者」捏着它的 `Shared`。所以归零后调用的是 `guard.defer_unchecked(|| Node::finalize(ptr))`——**延迟**到 epoch 安全时才真正 `finalize`。

`decrement_with_pin` 是 `decrement` 的「懒 pin」变体：绝大多数递减都不会归零，也就不需要 `Guard`；只有归零那一刻才现场 `pin()` 一个 `Guard` 来投递垃圾。这能省掉大量无谓的 pin 开销。

#### 4.4.2 核心流程

`decrement` 的流程（也是 `decrement_with_pin` 的前半段）：

```
old = refs_and_height.fetch_sub(1 << HEIGHT_BITS, Release)
if (old >> HEIGHT_BITS) == 1:          // 这次把它从 1 减到 0
    fence(Acquire)                      // 与所有此前的 Release 配对
    guard.defer_unchecked(|| Node::finalize(ptr))   // 投递垃圾，不立即析构
```

回收链路串起来就是规格里那一句：

\[\texttt{decrement} \;\to\; \texttt{fetch\_sub(Release)} \;\to\; \texttt{fence(Acquire)} \;\to\; \texttt{defer\_unchecked(finalize)}\]

`Node::finalize` 则在 epoch 推进后被调用：

```
drop_in_place(key)      // 析构 key
drop_in_place(value)    // 析构 value
Node::dealloc(ptr)      // 归还内存（不析构，只还内存）
```

**为什么是 `Release` + `Acquire` 这一对？** 这是引用计数安全回收的经典序（与 `Arc::drop` 同源）：

- 每次 `fetch_sub(Release)` 保证「这次递减」及之前对本节点的所有写入，对其他线程的递减可见。
- 归零那一刻的 `fence(Acquire)` 保证：赢得回收权的这个线程，能看到**所有**此前持有过引用的线程对该节点做过的全部写入（key/value 的初始化、修改等）。

合起来，当 `finalize` 真正运行时，它看到的一定是一个**内容完整、一致**的节点，析构 `key`/`value` 不会读到半截数据。如果漏掉 `Acquire` 栅栏，理论上可能出现「析构时还看不到别的线程刚写进去的值」的数据竞争。

#### 4.4.3 源码精读

[base.rs:292-304](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L292-L304) —— `NodeRef::decrement`。三行主体一目了然：`fetch_sub(1 << HEIGHT_BITS, Release)`、判断 `>> HEIGHT_BITS == 1`、`fence(Acquire)` + `defer_unchecked`。

[base.rs:306-324](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L306-L324) —— `NodeRef::decrement_with_pin`。与 `decrement` 的唯一区别在归零分支：它调用闭包 `pin()` 现场 `pin` 一个 `Guard`，再 `parent.check_guard(guard)` 校验来源，最后才 `defer_unchecked`。这样「计数没归零」的常见路径完全不用 `pin`。

[base.rs:251-262](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L251-L262) —— `Node::finalize`。先 `drop_in_place` 析构 `key` 和 `value`，再 `Self::dealloc(ptr)` 归还内存。注意它是 `#[cold]`（冷路径，编译器会优化掉对它的内联预期）。

`decrement` 的两个典型调用场景，体现了「谁会递减」：

- [base.rs:1322](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1322) 与 [base.rs:1332](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1332) —— `remove` 路径：成功把节点从某层物理摘除后 `n.decrement(guard)`（节点少占一层链接，计数减一）；发现节点已被别人标记删除时也递减自己那份。
- [base.rs:1653-1657](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1653-L1657) —— `RefEntry::release`：用户主动释放一份长期引用。

`decrement_with_pin` 的核心调用方是高层 `Entry` 的析构：[base.rs:1659-1666](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1659-L1666) 的 `RefEntry::release_with_pin`，它正是高层 `map::Entry` 在 `Drop` 时调用的（`map.rs` 里 `ManuallyDrop<base::RefEntry>` 配 `release_with_pin(epoch::pin)`，详见 u4-l14）。

#### 4.4.4 代码实践

**实践目标**：跟着一次 `remove` 把回收链路完整走一遍，确认「归零 → 延迟 → 析构」三段你都看得见。

**操作步骤**：
1. 从 [base.rs:1270-1334](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1270-L1334) 的 `SkipList::remove` 出发，标注出它在哪里调用了 `n.decrement(guard)`（[base.rs:1322](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1322)、[base.rs:1332](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1332)）。
2. 点进 `decrement`（[base.rs:292-304](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L292-L304)），确认归零分支投递的是 `Node::finalize`。
3. 点进 `finalize`（[base.rs:251-262](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L251-L262)），确认它析构 key/value 后只 `dealloc`，不再做并发相关的事。
4. 用一句话回答：为什么 `finalize` 里完全不需要任何原子操作或 `Guard`？

**预期结果**：因为 `finalize` 是在 epoch 已经推进、确定无人再持有该节点之后才被调用的，此时它对节点拥有**独占**访问，所以不需要任何并发保护——一切并发问题都已经在 `decrement` + `defer_unchecked` + epoch 推进这三步里解决了。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `decrement` 判断归零用的是 `old >> HEIGHT_BITS == 1`（旧值），而不是 `new >> HEIGHT_BITS == 0`（新值）？

**答案**：多个线程可能并发递减。用「我这次 fetch_sub 返回的旧值右移后 == 1」，意味着「这次递减恰好把它从 1 减到 0」，能保证**恰好一个线程**进入归零分支、赢得投递垃圾（以及后续析构）的权限，其余线程看到的旧值都 ≥ 2，不会重复 `finalize`——从而避免 double-free。看新值无法区分「我减到 0」还是「别人减到 0」。

**练习 2**：`decrement_with_pin` 相比 `decrement` 节省了什么？为什么这个优化是安全的？

**答案**：节省了「计数没归零时也调一次 `epoch::pin()`」的开销——绝大多数递减都不会归零，pin/unpin 又有不算零的成本。安全性在于：只有归零分支才需要投递垃圾，而投递垃圾才需要 `Guard`；非归零分支什么都不投递，自然不需要 `Guard`。所以「只在归零时才 pin」既省时又不破坏正确性。

---

### 4.5 `collector` 字段与 `check_guard`：Guard 必须来自同一个 Collector，以及为何要 `'static`

#### 4.5.1 概念说明

最后一个关键点：`SkipList` 投递的垃圾是挂在**它自己的那个 `Collector`** 上的，只有这个 `Collector` 的 epoch 推进，才会把垃圾倒掉。因此，所有传给 `SkipList` 方法的 `Guard`，**必须来自同一个 `Collector`**。这件事由 `check_guard` 在运行时断言保证。

同时，因为全局 `Collector` 可能在**任意晚**的时候才真正销毁垃圾（甚至晚于 `SkipList` 自身被 drop），所以被延迟析构的 `K`/`V` 不能持有任何非 `'static` 的借用——这就是 `insert` / `remove` 等方法要求 `K: 'static + V: 'static` 的根本原因。

#### 4.5.2 核心流程

两件事：

```
check_guard(guard):
    if guard.collector() == Some(c):
        assert!(c == &self.collector)   // Guard 必须来自本 SkipList 的 Collector

insert / remove / ... 的签名:
    K: Send + 'static
    V: Send + 'static
```

`'static` 的根源（来自源码注释）：当前的 `Collector` 是全局的，垃圾可能「arbitrarily late in the future」才被销毁。若将来把一个**本地 `Collector`** 内嵌进 `SkipList`，让它在 `SkipList` drop 时一并清空垃圾，就能去掉这两个 `'static` 约束。

#### 4.5.3 源码精读

[base.rs:468-480](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L468-L480) —— `SkipList` 结构体，其中 [base.rs:472-473](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L472-L473) 的 `collector: Collector` 字段就是「本跳表专属的时钟 + 垃圾袋」。构造时由外部传入（[base.rs:487-505](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L487-L505)）。

[base.rs:524-530](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L524-L530) —— `check_guard`：若 `guard.collector()` 存在，就 `assert!(c == &self.collector)`。几乎所有接收 `Guard` 的公开方法（`front`、`back`、`get`、`remove`、`RefEntry::release`、`RefEntry::remove` 等）开头都会先调用它（如 [base.rs:539](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L539)、[base.rs:1275](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1275)、[base.rs:1655](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1655)）。

[base.rs:455-467](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L455-L467) —— 源码注释解释了 `'static` 约束的来源与未来优化方向（内嵌本地 `Collector` 即可放宽约束）。

[base.rs:1237-1242](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1237-L1242) —— `insert` / `compare_insert` / `remove` 所在的 `impl` 块，带 `K: Send + 'static, V: Send + 'static` 约束。（同样的约束也出现在 [base.rs:1524-1525](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1524-L1525) 的 `Entry::remove` 和 [base.rs:1688-1689](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1688-L1689) 的 `RefEntry::remove`。）

一个值得对比的细节：当 `SkipList` 自身被 drop 时，它**独占**访问整个结构，因此完全绕开 epoch，直接用 `epoch::unprotected()` 逐个 `finalize`——见 [base.rs:1413-1437](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1413-L1437) 的 `Drop for SkipList`。这与并发的 `defer_unchecked(finalize)` 形成对照：独占时无需延迟，直接析构；并发时必须延迟到 epoch 安全。

#### 4.5.4 代码实践

**实践目标**：验证 `check_guard` 的存在，并理解它的「防御」作用。

**操作步骤**：
1. 在 [base.rs:524-530](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L524-L530) 读 `check_guard`，再数一下有多少个公开方法开头调用了它（用编辑器跳转/搜索 `check_guard`）。
2. 思考：如果某天源码去掉 `check_guard`，用 `epoch::default_collector()` 拿到的 `Guard` 去操作一个**自定义 `Collector`** 构造的 `base::SkipList`，会发生什么？（提示：垃圾投到了错误的 `Collector`，可能永远不被回收 → 内存泄漏；或在更糟的情况下产生悬空。）
3. 阅读高层 `SkipMap::new`（[map.rs:42-45](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/map.rs#L42-L45) 一带，以本地为准）如何用 `epoch::default_collector().clone()` 构造底层跳表，确认高层用户「永远在同一 `Collector` 上」，从而 `check_guard` 总是通过。

**预期结果**：你理解了 `check_guard` 是一道「防御性断言」，专门拦住「跨 `Collector` 误用」这类编程错误。

**待本地验证**：第 1 步的调用计数以你本地搜索结果为准。

#### 4.5.5 小练习与答案

**练习 1**：`check_guard` 里写的是 `if let Some(c) = guard.collector()`，为什么有的 `Guard` 会没有 `collector()`？

**答案**：`epoch::unprotected()` 产生的「伪 Guard」没有关联任何 `Collector`（它表示「当前线程不在任何 epoch 临界区里，但调用者保证此时没有并发」）。对这种 `Guard`，`guard.collector()` 返回 `None`，`check_guard` 直接放行——典型场景就是 `Drop for SkipList` 和 `IntoIter` 里的独占遍历（[base.rs:1413-1437](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1413-L1437)）。

**练习 2**：为什么把 `Collector` 改成「内嵌本地 `Collector`」（注释里的未来优化）就能去掉 `K: 'static + V: 'static`？

**答案**：因为内嵌的本地 `Collector` 会在 `SkipList` 被 drop 时**一并清空自己的垃圾袋**，保证所有延迟析构在 `SkipList` 的生命周期内全部完成。这样被延迟析构的 `K`/`V` 就不必活得比 `SkipList` 更久，自然不要求 `'static`。当前的「全局 `Collector`」没有这个保证——它可能在 `SkipList` drop 之后很久才回收，所以 `K`/`V` 必须 `'static` 以免其内部借用悬空。

---

## 5. 综合实践

把本讲的知识串起来，做一个**可运行**的观测实验，亲眼看到「引用计数 + epoch」如何防止 use-after-free。

**场景**：线程 A 用 `get` 拿到一个高层 `map::Entry`（内部是 `base::RefEntry`，持有引用计数）并长期持有；与此同时线程 B 把这个 key 删掉。我们要验证：**只要 A 还没释放句柄，对应节点就不会被回收**，A 仍然能安全读到旧值；只有等 A 释放句柄、且 epoch 推进后，节点才被析构。

**操作步骤**（在你的工程里新建一个示例或 test，例如 `examples/gc_observe.rs`，依赖 `crossbeam-skiplist` 与 `std::sync::Arc`）：

```rust
// 示例代码：观察引用计数对节点回收的推迟作用
use crossbeam_skiplist::SkipMap;
use std::sync::Arc;
use std::thread;
use std::time::Duration;

fn main() {
    // 用 Arc 包装 value，便于从外部观察「节点是否已被析构」
    let val = Arc::new(100u64);
    let map = SkipMap::new();
    map.insert(1u64, val.clone());
    // 此时 Arc 强引用计数 = 2（外部 val 一份，map 内部节点一份）

    // SkipMap 不实现 Clone，但它所有方法都接收 &self，故用 std::thread::scope
    // 让两个线程共享同一个 &SkipMap（MSRV 1.74 已支持 thread::scope）。
    thread::scope(|s| {
        // 线程 A：尽快拿到 Entry 句柄并长期持有（持有期间引用计数 > 0）
        s.spawn(|| {
            let entry = map.get(&1).expect("key 1 此刻应仍存在");
            // 模拟 A 慢慢使用这个句柄，期间 B 会删除 key=1
            thread::sleep(Duration::from_millis(200));
            // 关键断言：即便 B 已经删除 key=1，A 的句柄仍能安全读到旧值（无 use-after-free）
            assert_eq!(*entry.value(), 100);
            println!("A: 仍能安全读到已删除节点的旧值 = {}", entry.value());
            // entry 在此 drop → 引用计数减一；若归零则节点被投递进垃圾袋
        });

        // 线程 B：在 A 已拿到句柄之后删除 key=1
        s.spawn(|| {
            thread::sleep(Duration::from_millis(50));
            let removed = map.remove(&1);
            println!("B: remove 返回了一个 Entry 句柄？ {}", removed.is_some());
            // remove 返回的 Entry 也持有一份引用；它 drop 后引用计数再减一
        });
    });
    // 到这里 A/B 两个句柄都已 drop；但节点何时真正析构取决于 epoch 何时推进。

    println!("外部 Arc 强引用计数 = {}", Arc::strong_count(&val)); // 仍可能 > 1
    println!("（节点真正析构的时机由 epoch 推进决定，不保证在此时已完成）");
}
```

**需要观察的现象**：

1. 线程 A 的断言 `*entry.value() == 100` **不会**触发 use-after-free——即便 B 已经删除了 key=1，A 的句柄仍然安全。这正是引用计数（`try_increment` 撑住计数 > 0）+ epoch（B 的删除只是把节点「逻辑摘除 + 投递垃圾」，并未立即 `finalize`）共同保证的。
2. `Arc::strong_count(&val)` 在主线程结束时**可能仍大于 1**——因为节点的 `finalize`（会 drop 掉内部那份 `Arc`）尚未被 epoch 触发。多运行几次，或在该处之后再做几次 `SkipMap` 操作（触发 pin/epoch 推进），强引用计数最终会回落到 1。

**预期结果**：A 安全读到旧值；外部 `Arc` 强引用计数的回落是「延迟」的，且不保证在固定时刻完成。

**待本地验证**：第 2 点中「强引用计数何时回落到 1」是非确定性的（取决于线程调度与 epoch 推进时机），请以你本地多次运行的结果为准；其确定性结论只有一条——「在 A 释放句柄之前，节点绝不会被析构」。

**用文字解释 Guard 生命周期与引用计数各负责保证什么**：

- **`Guard`（epoch）**负责「临时读者」的安全：只要某个线程正处在一次 `epoch::pin()` 的临界区里，它从 `Atomic` 加载到的 `Shared` 所指节点，就**一定不会被 `finalize`**（哪怕此刻别的线程已经把该节点投进了垃圾袋）。`Guard` 的生命周期通常很短（一次方法调用），它确保「加载指针 → 顺指针读节点」这一小段不会踩空。
- **引用计数**负责「长期句柄」的安全：用户拿到的 `Entry`/`RefEntry` 可能跨过任意多次 `Guard` 临界区、甚至长期存活。只要句柄还活着（`try_increment` 加上去的计数还没被 `release`/`decrement` 减回来），节点就**不允许进入垃圾袋**（`decrement` 归零前不会 `defer_unchecked`）。

二者缺一不可：只有 `Guard` 挡不住长期句柄；只有引用计数挡不住「正在临界区里、但没拿到句柄」的临时读者。`decrement` 归零 → `defer_unchecked(finalize)` → 等 epoch 推进 → `finalize`，这条链路正是两道闸门**依次打开**的完整过程。

> 提示：本实践用的是高层 `SkipMap`，它的 `get`/`remove` 返回的 `map::Entry` 内部包着 `base::RefEntry`（靠引用计数存活），并在 `Drop` 时调用 `release_with_pin(epoch::pin)`（[base.rs:1659-1666](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1659-L1666)）。高层如何把这些细节藏起来，会在 **u4-l12（Entry 与 RefEntry：双生命周期句柄）** 与 **u4-l14（SkipMap 高层封装）** 详述。

## 6. 本讲小结

- 无锁跳表**不能直接 `free`** 被摘除的节点，否则会 use-after-free 或 double-free；这两类 UB 在编译期无法被借用检查器发现，必须由运行时机制兜底。
- 防护分两层：**epoch（`Guard`）**保护临界区内的临时读者，**引用计数**保护跨临界区的长期 `Entry`/`RefEntry` 句柄；节点真正销毁需要两道闸门依次打开（引用计数归零 + epoch 推进）。
- `Node::try_increment`（[base.rs:222-249](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L222-L249)）用 CAS 给高位计数加一，但**拒绝已经归零**的节点，从而防止 double-free。
- `NodeRef::decrement`（[base.rs:292-304](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L292-L304)）/ `decrement_with_pin`（[base.rs:306-324](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L306-L324)）用 `fetch_sub(Release)` + 归零时 `fence(Acquire)` 的经典序保证「赢得回收权者看到完整节点」，再 `defer_unchecked(Node::finalize)` 把析构延迟到 epoch 安全时。
- `Node::finalize`（[base.rs:251-262](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L251-L262)）在 epoch 推进后独占执行：析构 key/value 再 `dealloc`，因此自身不需要任何并发保护。
- `SkipList` 把 `Collector` 存为字段（[base.rs:472-473](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L472-L473)），`check_guard`（[base.rs:524-530](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L524-L530)）强制所有 `Guard` 来自同一 `Collector`；全局 `Collector` 可能很晚才回收垃圾，这是 `insert`/`remove` 要求 `K: 'static + V: 'static` 的根本原因（[base.rs:455-467](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L455-L467)、[base.rs:1237-1242](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1237-L1242)）。

## 7. 下一步学习建议

本讲讲清了「节点什么时候被安全释放」，但还没有讲「节点在结构上是怎么被找到、被摘除的」。建议接下来：

1. **u3-l8（搜索算法）**：本讲的 `decrement` 调用点都在 `remove` / `search` 路径上，届时你会看到 `search_position` / `search_bound` 如何定位节点、并在搜索过程中顺带 `help_unlink` 协助清理——理解了搜索，回收链路就有了完整的上下文。
2. **u3-l9（标记指针与逻辑删除）**：本讲反复提到「节点被摘除」但没说摘除的具体机制；`mark_tower`（[base.rs:327-348](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L327-L348)）用 `Atomic` 指针的 tag 位做「逻辑删除」，与 `decrement` 的「物理回收」是两回事，下一讲会把二者衔接起来。
3. **u4-l12（Entry 与 RefEntry：双生命周期句柄）**：本讲提到 `Entry`（绑定 `Guard`）与 `RefEntry`（靠引用计数）两种句柄，届时会精读 `Entry::pin` / `RefEntry::try_acquire` / `release_with_pin` 如何在两者间转换。
4. **u5-l17（内存序分析）**：本讲只解释了 `decrement` 里的 `Release`/`Acquire` 序；`base.rs` 中其它 `Relaxed`/`SeqCst`/`unprotected()` 的取舍留到那一讲系统梳理。
