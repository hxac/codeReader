# 项目定位与 epoch-based 内存回收思想

## 1. 本讲目标

本讲是 crossbeam-epoch 学习手册的第一篇。学完后你应该能够：

- 说清楚并发数据结构里「移除一个对象后为什么不能立刻 `drop`」这个核心难题。
- 用自己的话讲明白 epoch-based memory reclamation（基于纪元的内存回收，简称 EBR）的核心思想：**延迟回收**与 **grace period（宽限期）**。
- 看懂 `crossbeam-epoch` 的 crate 级文档（`src/lib.rs` 顶部模块注释），建立起「指针 / pin / 垃圾 / API」的整体心智地图。
- 了解这个 crate 在 `no_std` 与 `std` 下的定位差异。

本讲**不**要求你已经懂无锁编程或 Rust 的 `unsafe`。我们会从最直觉的场景出发。

---

## 2. 前置知识

在读本讲之前，你只需要具备：

- 基本的 Rust 知识：知道 `Box`、`Drop`、原子操作 `AtomicUsize` 是什么概念即可。
- 对「多线程同时读写共享数据」有一个模糊印象。

下面这些术语会在本讲中反复出现，先用一句话解释：

| 术语 | 通俗解释 |
| --- | --- |
| 并发数据结构（concurrent data structure） | 可以被多个线程同时读写的容器，例如并发栈、并发队列、并发 Map。 |
| 无锁（lock-free） | 不用互斥锁，而是用原子操作（如 CAS）来协调多线程。 |
| EBR（epoch-based reclamation） | 用「纪元」编号来判断「现在回收某个对象是否安全」的一种内存回收策略。 |
| grace period（宽限期） | 一个时间窗口，保证在这个窗口之后，没有任何线程还在用某个旧对象。 |
| `drop` | Rust 里对象离开作用域时被销毁、内存被回收的动作。 |

> 提示：如果你已经熟悉 RCU（Read-Copy-Update）或 Java/Go 的垃圾回收，可以把 EBR 理解成「手动的、为单个数据结构定制的、极轻量的 RC」。但本讲不假设你有这些背景。

---

## 3. 本讲源码地图

本讲只读两个文件，但它们是理解整个 crate 的「地图」：

| 文件 | 作用 |
| --- | --- |
| `README.md` | 一句话定位：这个 crate 是做什么的、给谁用。 |
| `src/lib.rs` | crate 的根模块。它的**顶部模块注释**（前 49 行）是全 crate 最重要的一段说明，把 EBR 的原理讲了一遍。文件后半部分是模块组织与公开导出，让我们能看到整个库的「骨架」。 |

另外，为了把 epoch 的直觉讲准确，本讲会**少量**引用 `src/epoch.rs` 顶部的注释（不深入实现，实现细节留给第 5 单元）。

永久链接统一使用当前 HEAD `6195355e`。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. EBR 要解决的问题：为什么移除后不能立刻释放。
2. epoch 与 grace period：延迟回收的直觉模型。
3. crate 级文档导读：指针、pin、垃圾与公开 API。

---

### 4.1 EBR 要解决的问题：移除后为什么不能立刻释放

#### 4.1.1 概念说明

设想一个**无锁链表**。多个线程同时在它上面 `push` / `pop`。`pop` 的典型实现是：

1. 读取链表头指针 `head`。
2. 用 CAS（compare-and-swap）把链表头从 `head` 改成 `head.next`，等于「把这个节点摘下来」。
3. 销毁这个被摘下来的节点。

危险藏在第 2 步和第 3 步之间。线程 A 在第 1 步读到 `head` 指向节点 X 之后、还没来得及做 CAS 的时候，**线程 B 可能已经把 X 摘下来并 `drop` 掉了**。这时线程 A 手里那个指向 X 的指针就成了**悬垂指针（dangling pointer）**，再去访问就是经典的 **use-after-free**。

所以核心难题是：

> 在无锁数据结构里，**「逻辑上把一个对象从结构里移除」**和**「物理上释放它的内存」**绝对不能是同一件事。你必须等所有可能还拿着旧指针的线程都用完了，才能安全释放。

这就是 EBR（以及 hazard pointer、引用计数等其他方案）要解决的根本问题。

#### 4.1.2 核心流程

我们先把「会出问题」的场景抽象成伪代码（**示例代码**，不是项目源码，仅用于说明）：

```text
// 线程 B：pop 一个节点
node = head.load()          // 1. 读到指向 X 的指针
if CAS(head, node, node.next) {   // 2. 把 X 从链表摘下
    drop(node)              // 3. 立刻销毁 X  ← 危险！
}

// 与此同时，线程 A：
node = head.load()          // A 也在第 1 步拿到了指向 X 的指针
node.data.read()            // A 还没走到 CAS，X 就被 B drop 了 → 悬垂访问
```

如果线程 B 在第 3 步「立刻 `drop`」，线程 A 就会踩到被释放的内存。带 GC 的语言（Java、Go）天然没这个问题——GC 会等没有任何引用指向 X 时才回收它。Rust 没有全局 GC，于是需要 EBR 这样的机制来**模拟「等大家都放手了再回收」**。

#### 4.1.3 源码精读

README 开头一句话点明了这个 crate 的定位：

> [README.md:L15-L20](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/README.md#L15-L20) —— **说明**：crossbeam-epoch 提供基于 epoch 的垃圾回收，用于构建并发数据结构。当一个线程从并发结构里移除对象时，别的线程可能**同时还在用**指向它的指针，所以不能立刻销毁。epoch-based GC 的作用就是把共享对象的销毁**推迟**到「不可能再有指针指向它」的时候。

crate 根模块注释也用几乎同样的话开篇，并立刻给出「带 GC 的语言如何轻松解决」的对照：

> [src/lib.rs:L1-L10](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/lib.rs#L1-L10) —— **说明**：并发集合的 `remove` 操作带来了一个有趣的问题——一个线程从无锁 Map 里移除元素时，另一个线程可能正在读同一个元素，前者必须等后者停止读取才能安全销毁。带 GC 的语言轻而易举地解决了它，因为 GC 会在没有线程再持有引用时回收被移除的元素。

这两段是整个 crate 的「为什么」。本讲剩下的内容，都是回答「那 crossbeam-epoch 具体怎么做到『等大家都放手』」。

#### 4.1.4 代码实践

这是一道**源码阅读 + 思考型**练习，目的是让你把「问题」刻进脑子。

1. **实践目标**：用自己的语言复述 EBR 要解决的问题，并构造一个会出问题的并发场景。
2. **操作步骤**：
   - 打开 [src/lib.rs:L1-L10](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/lib.rs#L1-L10)，逐句读懂前 10 行。
   - 假装你是这个 crate 的作者，向一位没接触过并发的同事解释：**为什么删除链表节点后不能直接 `drop`，而要走 epoch 回收？** 写一段不超过 200 字的中文。
3. **需要观察的现象**：你能否在不提「悬垂指针 / use-after-free」这种术语的情况下，用一个具体的时间线讲清楚危险。
4. **预期结果**：你能讲出一个「线程 A 拿指针、线程 B 释放内存、线程 A 踩空」的两步交错时序。这道题没有「运行结果」，属于**待本地验证**之外的纯理解练习。
5. **参考答案要点**：移除 ≠ 释放；别的线程可能正握着旧指针；必须延迟到没人再用才能释放。

#### 4.1.5 小练习与答案

**练习 1**：如果一个数据结构**永远只有一个线程**访问，还需要 EBR 吗？

> **答案**：不需要。EBR 解决的是「多线程同时持有同一对象的指针」问题。单线程下不存在别人还握着旧指针的情况，直接 `drop` 就安全。

**练习 2**：把对象放进 `Rc`/`Arc`，是不是就等于解决了上面这个问题？

> **答案**：部分解决，但代价不同。`Arc` 通过引用计数保证「还有人用就不释放」，确实安全，但每次读写指针都要原子地增减计数，开销大且在无锁结构里容易产生缓存争用。EBR 的目标是**比引用计数更便宜**地达到同样的安全性——它不数「有几个人在用」，而是用 epoch 判断「这段时间内有没有人可能在用」。

---

### 4.2 epoch 与 grace period：延迟回收的直觉模型

#### 4.2.1 概念说明

EBR 的核心想法是给时间打上「**纪元（epoch）**」编号，就像给胶卷盖时间戳：

- 全局有一个**单调递增（回绕）**的整数，叫**全局 epoch**。
- 每隔一段时间，全局 epoch 会「前进（advance）」一格。
- 当一个对象被移除（成为垃圾）时，给它盖上**当时的 epoch 编号**。
- 垃圾不会被立刻销毁，而是先攒着。只有当全局 epoch 已经**远远超过**垃圾身上的编号时，才认定「不可能还有线程在用这个对象」，这时才真正销毁它。

这个「远远超过」的距离，就是 **grace period（宽限期）**。在 crossbeam-epoch 里，这个距离被精确定为 **2 次 epoch 前进**。

为了让「还有线程在用」这件事可观测，每个线程在访问共享数据之前要先 **pin（钉住）**自己，访问完再 **unpin**。pin 的时候，线程会把当前的全局 epoch **快照**到自己身上。于是：

> 只要全局 epoch 还没前进够 2 次，就一定还有 pin 在旧 epoch 里的线程「可能」握着旧指针；前进够 2 次之后，就可以断定没人还握着了。

#### 4.2.2 核心流程

把上面这套机制画成一条时间线（`G` 表示全局 epoch，每个线程有一份本地快照）：

```text
时间 ──────────────────────────────────────────────────────►
G:        0        2        4        6        8   (每次前进 +2)
          │        │        │        │        │
线程A pin ─┤  (快照 G=0，正在读)        │        │
        unpin ────────────────────┘  │        │
线程B:             移除对象 X，盖戳 G=2          │
                   (X 进入垃圾袋)               │
                                       │        │
                                  G 前进到 6    │
                                  (相对 G=2 已前进 2 次) → X 可安全销毁
```

关键不变量（invariant）：

- 一个对象若在 epoch \(g\) 成为垃圾，那么在全局 epoch 相对 \(g\) **至少前进 2 次**之后，销毁它是安全的。
- 用距离表示，若 `dist(垃圾epoch, 当前epoch) >= 2`（epoch 单位下），即可回收。

写成公式（grace period 判定）：

\[
\text{reclaimable}(g,\ e_{\text{now}}) \iff e_{\text{now}} - g \geq 2
\]

其中 \(g\) 是对象成为垃圾时盖的 epoch，\(e_{\text{now}}\) 是当前全局 epoch，距离以「前进次数」计。这就是「宽限期 = 2 次 epoch 前进」。

> 为什么是 2 而不是 1？直觉上：第 1 次前进用于「让所有正在临界区里的线程都进入更新的 epoch」，第 2 次前进之后，才能保证**没有任何**线程的快照还停留在「可能见过这个对象」的旧 epoch。第 5 单元会给出严格的证明，这里先记住「2 次」这个数。

#### 4.2.3 源码精读

crate 注释把这套机制用一段话讲清楚了：

> [src/lib.rs:L12-L16](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/lib.rs#L12-L16) —— **说明**：本 crate 实现了一种基于 epoch 的内存回收机制。元素被移除时，会被放进一堆垃圾里，并盖上当前 epoch。线程每次访问集合时，都会查看当前 epoch 并尝试推进它，然后销毁那些「老到不可能还有线程引用」的垃圾。细节更复杂，但对使用者来说是全自动的。

而 epoch 模块的开头注释，把「2 次前进」这个关键数字点破了：

> [src/epoch.rs:L1-L8](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/epoch.rs#L1-L8) —— **说明**：全局 epoch 是一个整数，最低位始终为 0（全局 epoch 永不「pinned」）。它会被周期性地「前进」。一个 pinned 的参与者，只有当所有当前 pinned 的参与者都 pin 在当前 epoch 时，才被允许推进全局 epoch。**关键结论：如果一个对象在某个 epoch 成为垃圾，那么经过 2 次前进之后，我们可以确信没有参与者还会持有指向它的引用——这就是安全回收的核心。**

再补两处实现「长相」（不深究，只看结构）：

> [src/epoch.rs:L28-L36](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/epoch.rs#L28-L36) —— **说明**：`Epoch` 内部就是一个整数 `data`，**最低位**当 pinned 标志位，其余位存 epoch 编号。这就是「一个字」同时编码 epoch 和 pin 状态的技巧。

> [src/epoch.rs:L78-L86](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/epoch.rs#L78-L86) —— **说明**：`successor()` 每次给 epoch **加 2**。之所以加 2 而不是加 1，正是为了让最低位（pinned 标志）的语义不被破坏：全局 epoch 始终是偶数（低位恒 0），而 pin/unpin 只翻转线程本地副本的最低位。

把这两段连起来，你就理解了「epoch 怎么表示、怎么前进」的最小骨架——具体的 pin 屏障与回收链路留给第 5 单元。

#### 4.2.4 代码实践

1. **实践目标**：亲手推演一遍 epoch 时间线，验证「2 次前进」grace period。
2. **操作步骤**：
   - 假设全局 epoch 从 0 开始，每次前进加 2（`successor` 语义）。
   - 在一张表里记录：第 0 步 G=0；线程 B 在 G=4 时移除对象 X（盖戳 4）。
   - 问：G 前进到几时，X 才首次满足 `e_now - g >= 2`（以「前进次数」计，即 G 编号差 ≥ 4）？
3. **需要观察的现象**：你是否算出「X 在 G=8 时才可回收」。
4. **预期结果**：X 盖戳 4，需相对它前进 2 次（每次 +2），即 G 到 8。中间的 G=6 时 X 仍不可回收，因为可能还有线程 pin 在 4 或 6。
5. **说明**：这是纯推理练习，**待本地验证**——你可以在第 5 单元学完 `try_advance`/`collect` 后，用真实代码确认这个窗口。

#### 4.2.5 小练习与答案

**练习 1**：为什么 grace period 是「前进 2 次」而不是「前进 1 次」？

> **答案**：1 次前进只能保证「新进入的线程看不到这个对象」，但无法保证「之前已经 pin、正在临界区里的线程」都已离开。需要第 2 次前进来覆盖那些在旧 epoch 里 pin 的线程，确保它们的临界区一定已经结束。

**练习 2**：如果一个线程**长期不 unpin**（一直 pin 着），会发生什么？

> **答案**：全局 epoch 的前进要求「所有 pinned 参与者都 pin 在当前 epoch」。一个一直不 unpin 的线程会卡在旧 epoch，于是全局 epoch 无法越过它继续前进，垃圾袋里的对象也就永远等不到 grace period 到来，**回收被阻塞**。这也是后面 `Guard::repin` 想解决的问题（第 3 单元）。

---

### 4.3 crate 级文档导读：指针、pin、垃圾与公开 API

#### 4.3.1 概念说明

知道了「为什么」和「epoch 直觉」之后，我们需要一张「这个库到底暴露了哪些东西给我用」的地图。`src/lib.rs` 顶部的模块注释用四个小节给出了这张地图：

- **Pointers（指针）**：并发结构用**原子指针**搭起来。`Atomic<T>` 是一个指向堆对象的共享原子指针；`load` 它得到一个 `Shared<'g, T>`——一个受 epoch 保护的指针，通过它才能安全读。
- **Pinning（钉住）**：`load` 一个 `Atomic` 之前，参与者必须先 `pin`。pin 的语义是「从现在起被移除的对象先别销毁，等我 unpin」。
- **Garbage（垃圾）**：被移除的对象先存进线程本地或全局的垃圾袋，等到安全时刻再统一销毁。最常用的动作是 `Guard::defer`——把任意一个函数（典型场景是「释放某个对象」）**推迟**到全局 epoch 推进足够远之后执行。
- **APIs（接口）**：大多数场景直接用默认收集器的 `pin()`；想自建收集器则用 `Collector` API。

#### 4.3.2 核心流程

把上面四个小节串成一次典型的「使用闭环」：

```text
1. pin()                         → 拿到一个 Guard（进入临界区，记下当前 epoch）
2. atomic.load(&guard)           → 得到 Shared<'g, T>（受 epoch 保护的指针）
3. 通过 Shared 读数据 / 做计算
4. 若移除了某对象 → guard.defer_destroy(旧指针)  → 旧对象进垃圾袋，延后销毁
5. guard 离开作用域（drop）      → unpin（离开临界区）
   （后台）全局 epoch 前进够 2 次后，垃圾袋里的对象被真正销毁
```

这套闭环里每一步对应一个公开类型/函数，下一节我们把它们在源码里一一找出来。

#### 4.3.3 源码精读

模块注释的四个小节：

> [src/lib.rs:L22-L27](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/lib.rs#L22-L27) —— **说明**：`# Pointers` 小节。并发集合用原子指针构建；`Atomic` 是指向堆对象的共享原子指针，`load` 它得到受 epoch 保护的 `Shared`，借此安全读取。

> [src/lib.rs:L29-L34](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/lib.rs#L29-L34) —— **说明**：`# Pinning` 小节。`load` 之前必须先 `pin`：pin 声明「从现在起被新移除的对象暂时别销毁」，对新垃圾的回收会挂起，直到该参与者 unpin。

> [src/lib.rs:L36-L44](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/lib.rs#L36-L44) —— **说明**：`# Garbage` 小节。被移除的对象先暂存，等所有当前 pinned 的参与者 unpin 后再销毁。有一个全局共享的垃圾队列，可用 `Guard::defer` 把任意函数推迟到全局 epoch 推进足够远后执行——最典型就是推迟「释放某个对象」。

> [src/lib.rs:L46-L49](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/lib.rs#L46-L49) —— **说明**：`# APIs` 小节。多数场景直接用默认收集器的 `pin()`；想自建收集器就用 `Collector` API。

接下来看 crate 的「骨架」。首先，整个 crate 是 `#![no_std]` 的：

> [src/lib.rs:L51](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/lib.rs#L51) —— **说明**：`#![no_std]`，意味着核心功能不依赖标准库，可在嵌入式/内核等 `no_std` 环境使用（需要 `alloc` 特性提供堆分配）。

源码被拆成几个内部模块，每个模块负责一类职责：

> [src/lib.rs:L153-L168](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/lib.rs#L153-L168) —— **说明**：声明内部模块 `alloc_helper`、`atomic`、`collector`、`deferred`、`epoch`、`guard`、`internal`、`sync`。这套划分正是本学习手册各单元的依据（指针→`atomic`，Guard→`guard`，回收链路→`internal`+`epoch`，无锁结构→`sync`）。

对外公开的类型与函数集中在这里导出：

> [src/lib.rs:L170-L177](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/lib.rs#L170-L177) —— **说明**：公开导出指针族（`Atomic`/`Owned`/`Shared`/`Pointable`/`Pointer`/`CompareExchangeError`/`CompareExchangeValue`）、收集器（`Collector`/`LocalHandle`）、以及 `Guard` 和 `unprotected`。这是普通使用者最常接触的 API 面。

而最常用的 `pin()` / `is_pinned()` / `default_collector()` 只在启用 `std` 时才提供：

> [src/lib.rs:L184-L187](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/lib.rs#L184-L187) —— **说明**：`default` 模块及 `pin`/`is_pinned`/`default_collector` 仅在 `feature = "std"` 下导出——它们依赖线程局部存储（thread-local），因此只在 `std` 下可用。

关于 `no_std` 与 `std` 的定位，README 也明确说明：

> [README.md:L22-L23](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/README.md#L22-L23) —— **说明**：除「全局 GC（默认收集器）」之外，本 crate 的所有内容都能在 `no_std` 下使用，前提是开启 `alloc` 特性。换句话说：指针、Guard、`Collector` 都是 `no_std` 友好的；只有「默认收集器 + `pin()` 便捷函数」需要 `std`。

特性开关集中在 `Cargo.toml`：

> [Cargo.toml:L26-L43](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/Cargo.toml#L26-L43) —— **说明**：默认特性是 `std`；`std` 隐含开启 `alloc`；`alloc` 可单独使用；`loom` 用于并发模型检验测试。注释特别强调「同时关闭 `std` 和 `alloc` 是不支持的」。特性细节会在下一讲（u1-l2）展开。

#### 4.3.4 代码实践

这是一道**源码阅读 + 可运行验证**型练习。

1. **实践目标**：建立「模块 ↔ 概念」的映射，并确认默认收集器能跑起来。
2. **操作步骤**：
   - 打开 [src/lib.rs:L153-L168](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/lib.rs#L153-L168)，把每个内部模块名填到下表对应概念上：

     | 概念 | 对应模块 |
     | --- | --- |
     | 指针族（Atomic/Owned/Shared） | `atomic` |
     | pin 的凭证 | ？ |
     | 全局/本地参与者、回收链路 | ？ |
     | epoch 表示 | ？ |
     | 无锁链表/队列 | ？ |

   - 新建一个最小二进制 crate 验证默认收集器（**示例代码**，需本机安装 Rust）：
     ```toml
     # Cargo.toml
     [dependencies]
     crossbeam-epoch = "0.9"
     ```
     ```rust
     // src/main.rs
     fn main() {
         // pin() 进入临界区，guard 离开作用域时自动 unpin
         let _guard = crossbeam_epoch::pin();
         println!("pinned: {}", crossbeam_epoch::is_pinned());
         // guard 在此处 drop → unpin
     }
     ```
3. **需要观察的现象**：`cargo run` 能编译通过，并打印 `pinned: true`。
4. **预期结果**：程序正常编译运行，输出 `pinned: true`，说明默认收集器在 `std` 下开箱即用。如果你暂时没有 Rust 环境，可以只完成表格部分，运行结果**待本地验证**。
5. **表格参考答案**：`guard` / `internal` / `epoch` / `sync`。

#### 4.3.5 小练习与答案

**练习 1**：在 `no_std` + `alloc`（不开 `std`）环境下，下列哪个函数**不可用**？`pin()`、`Atomic::load`、`Guard::defer`、`Collector::new`。

> **答案**：`pin()` 不可用。根据 [src/lib.rs:L184-L187](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/lib.rs#L184-L187)，`pin`/`is_pinned`/`default_collector` 只在 `feature = "std"` 下导出。其余三者属于指针族/`Guard`/`Collector`，是 `no_std` 友好的（`no_std` 下需自己 `Collector::new()` 再 `register().pin()` 来代替全局 `pin()`）。

**练习 2**：用一句话区分 `Atomic` 和 `Shared`。

> **答案**：`Atomic<T>` 是**被多个线程共享**的原子指针（存在数据结构里）；`Shared<'g, T>` 是 `load` 出来的、**绑定到某个 Guard 生命周期 `'g`** 的受保护指针，只有在 guard 存活期间读它才安全。两者关系是「存储 vs 读取结果」。

---

## 5. 综合实践

把本讲三个最小模块串起来，完成下面这个综合小任务。

**任务**：你正在给团队做一次 5 分钟的分享，介绍「为什么我们的并发链表要引入 crossbeam-epoch」。请产出两样东西：

1. **一段不超过 200 字的中文说明**（对应规格里的实践任务），要求：
   - 解释「为什么删除链表节点后不能直接 `drop`，而要走 epoch 回收」；
   - 举一个**不使用 EBR 会出问题**的具体并发场景（写出线程 A / 线程 B 的交错动作）。
2. **一张 epoch 时间线草图**：画出「对象 X 在 epoch 4 成为垃圾 → 全局 epoch 前进 → X 在 epoch 8 被回收」的过程，并标注 grace period。

**参考写作思路**（你可以用自己的话改写）：

> 无锁链表里，移除一个节点（CAS 把 head 指向 next）和释放它的内存不能是同一步。因为线程 A 可能在 CAS 之前已经读到指向节点 X 的指针，正准备读 X 的数据；如果此时线程 B 把 X 摘下并立刻 `drop`，A 手里的指针就悬空了，访问即 use-after-free。所以必须延迟回收：用 epoch 给垃圾盖时间戳，等全局 epoch 相对它前进满 2 次（grace period），确保没有任何线程还握着旧指针，再真正销毁。

**验收标准**：

- 你的说明里出现了「悬垂/use-after-free」「延迟」「epoch」「grace period/2 次前进」这些关键概念中的至少 3 个。
- 时间线草图里能清楚标出「垃圾盖戳 4」和「可回收于 8」两点，并能解释为什么 6 还不行。

> 选做（可运行）：用 4.3.4 的最小程序确认默认收集器可用，作为「它真的开箱即跑」的证据。运行结果**待本地验证**。

---

## 6. 本讲小结

- **核心难题**：在并发/无锁数据结构里，移除一个对象后**不能立刻 `drop`**，因为别的线程可能还握着指向它的旧指针（[src/lib.rs:L1-L10](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/lib.rs#L1-L10)）。
- **EBR 思想**：给时间打 epoch 编号，给垃圾盖戳，把销毁**推迟**到「不可能还有人引用」的时候（[src/lib.rs:L12-L16](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/lib.rs#L12-L16)）。
- **grace period = 2 次前进**：对象在 epoch \(g\) 成为垃圾，全局 epoch 相对它前进满 2 次后才安全回收（[src/epoch.rs:L1-L8](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/epoch.rs#L1-L8)）。
- **pin 是关键动作**：访问共享数据前先 `pin`（记下当前 epoch、挂起新垃圾的回收），用完再 `unpin`；长期不 unpin 会拖慢全局回收。
- **API 地图**：`Atomic`（共享原子指针）→ `load` 得到 `Shared`（受 epoch 保护）→ `Guard::defer`/`defer_destroy` 延后销毁 → 默认收集器 `pin()` 一行上手。
- **定位**：核心是 `#![no_std]` 的，`no_std`+`alloc` 下除「默认收集器」外都可用；最便捷的 `pin()` 只在 `std` 下提供（[src/lib.rs:L51](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/lib.rs#L51)、[src/lib.rs:L184-L187](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/lib.rs#L184-L187)）。

---

## 7. 下一步学习建议

本讲只建立了「为什么」和「整体地图」。接下来建议按顺序：

1. **u1-l2 项目结构、构建配置与特性开关**：搞清楚 `Cargo.toml` 的 `std`/`alloc`/`loom` 特性、`build.rs` 与条件编译，让你能自己把 crate 配进项目并切换特性。
2. **u1-l3 快速上手：pin 与 Atomic 的最小例子**：把本讲的「使用闭环」真正写出来——`pin()` + `Atomic::load/swap` + `defer_destroy`，亲手让一个对象经历「分配 → 替换 → 延迟回收」。

读完 u1 的三篇，你就具备了动手写第一个基于 crossbeam-epoch 的小程序的能力。之后再进入第 2 单元深入「指针三剑客」。
