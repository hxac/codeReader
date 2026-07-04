# 内存序与 volatile 读写 hack 深入

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 Rust/C11 内存模型里「happens-before」「数据竞争（data race）」「未定义行为（UB）」三个概念的关系。
- 指出 `src/deque.rs` 中 `Worker::push` 的 `fence(Release)` 与 `Stealer::steal` 的 `fence(SeqCst)` 如何配对，建立跨线程的 happens-before。
- 解释 `Buffer::write` / `Buffer::read` 为什么用 `ptr::write_volatile` / `read_volatile` 而不是原子操作，以及为什么注释坦承这「技术上是数据竞争、是 UB」。
- 理解 `Inner` 结构体注释引用的三篇论文（Chase-Lev、弱内存模型、CDSchecker）各自对实现给出了什么指导。
- 读懂 `LIFO pop` 与 `steal` 中 `SeqCst fence` 为什么被精确放在「预约自己这一端」与「读对端」之间。

本讲是专家层的「横向串讲」：前面 u2-l3、u3-l3 已经分别讲过 `Stealer::steal` 和 `Injector::steal` 的算法流程，本讲不再重复流程，而是把分散在全文件里的内存序、fence、volatile 统一拎出来讲清「为什么这样放」。

## 2. 前置知识

### 2.1 happens-before 与数据竞争

在 Rust 的并发内存模型（与 C11 一致）里，两个线程访问同一块内存时：

- 只要其中至少一个是**写**，且它们之间**没有 happens-before 关系**，就构成**数据竞争（data race）**。
- 有数据竞争的普通（非原子、非 `unsafe` 内部）访问是**未定义行为（UB）**。

要让两个线程安全地协作访问同一个槽位，必须用以下三种手段之一建立 happens-before：

1. **原子操作**：`AtomicXxx` 的 `load` / `store` / `fetch_add` / `compare_exchange` 等。
2. **fence（内存屏障）**：`atomic::fence(Ordering::…)`，本身不读写数据，只约束前后操作的可见顺序。
3. **释放-获取（release-acquire）配对**：A 线程的 `Release` 写 / fence 与 B 线程的 `Acquire` 读 / fence 配对，把 A 在配对点之前的所有写都「发布」给 B。

### 2.2 四种 Ordering 速查

| Ordering | 含义 | 典型用法 |
|---|---|---|
| `Relaxed` | 只保证操作本身的原子性，不约束可见性顺序 | 计数、不参与同步的索引读写 |
| `Acquire` | 读操作：本读之后的访问不会重排到它之前 | 读取「发布」标记 |
| `Release` | 写操作：本写之前的访问不会重排到它之后 | 写入数据后发布标记 |
| `SeqCst` | 全局存在一个所有 SeqCst 操作都认同的总顺序；最强 | 关键仲裁点、与 fence 配套 |

`fence(Ordering::Release)` / `fence(Ordering::SeqCst)` 的作用：fence 把两侧的（含非原子的）普通读写也纳入排序约束，相当于在当前位置打了一道「不可穿越」的墙。

### 2.3 volatile 不等于原子

`ptr::write_volatile` / `read_volatile` 只保证「这次访问确实发生、不被编译器优化掉或合并」，**不提供任何跨线程排序保证，也不改变数据竞争的判定**。它是为操作内存映射 IO（MMIO）设计的，被 crossbeam-deque 借用来做并发槽位读写——这是一处刻意的「hack」，是本讲的重点之一。

如果你对上述概念还不熟，建议先读 u2-l1（`Buffer`/`Inner` 数据结构）和 u2-l3（`Stealer::steal` 流程），本讲默认你已经知道 `front` / `back` 双游标和「偷取两步走」的流程。

## 3. 本讲源码地图

本讲只涉及一个文件，但会横向引用其中多个区段：

| 源码位置 | 作用 |
|---|---|
| `src/deque.rs` L101-L123 | `Inner` 结构体及其注释中引用的三篇论文 |
| `src/deque.rs` L64-L90 | `Buffer::at` / `write` / `read` 的 volatile 实现 |
| `src/deque.rs` L399-L433 | `Worker::push`：写槽位 + Release fence + store back |
| `src/deque.rs` L450-L545 | `Worker::pop`：FIFO 的 `fetch_add(SeqCst)` 与 LIFO 的 `fence(SeqCst)` |
| `src/deque.rs` L598-L683 | `Stealer::is_empty` / `len` / `steal` 中的 fence + Acquire 配对 |
| `src/deque.rs` L911-L921 | `steal_batch_with_limit` 末尾的 tsan 双路径（fence vs Release store） |
| `src/deque.rs` L1196-L1230 | `Slot` 的原子 `state` 与 `wait_write`（Injector 的对照实现） |
| `src/deque.rs` L1464-L1530 | `Injector::steal` 中的 `fence(SeqCst)` |

## 4. 核心概念与源码讲解

### 4.1 三篇论文：实现的正确性依据

#### 4.1.1 概念说明

`crossbeam-deque` 的 Worker/Stealer 队列是一个**无锁 work-stealing 双端队列**。这类数据结构的正确性极难手工论证，尤其是放到 ARM、POWER 这类「弱内存模型」CPU 上时，大量在 x86（强内存模型）上「凑巧正确」的代码会出问题。因此源码没有「自己重新发明」算法，而是直接建立在三篇经过同行评审的学术论文之上，并在 `Inner` 结构体的文档注释里点名引用。

#### 4.1.2 核心流程

三篇论文各自负责「正确性的一个层面」：

1. **Chase & Lev (SPAA 2005)**：原始算法。提出「单生产者双端、环形缓冲区、owner 从一端 push/pop、stealer 从另一端 steal」的结构，并假设**顺序一致性（SeqCst）**。
2. **Le, Pop, Cohen, Nardelli (PPoPP 2013)**：弱内存模型修正。证明原 Chase-Lev 在弱内存模型下有 bug，给出**在 steal 里插入 SeqCst fence** 的修复，并允许把大部分操作从 SeqCst 降级为 Acquire/Release 以提升性能。**本讲的 fence 放置就是这篇论文的直接产物。**
3. **Norris & Demsky (OOPSLA 2013, CDSchecker)**：模型检验工具。用来在开发期自动验证「用 C/C++ atomics 写的并发数据结构」在弱内存模型下是否正确——相当于给上面两条结论做了机器验证背书。

#### 4.1.3 源码精读

`Inner` 结构体上方紧贴一段引用三篇论文的注释：

[src/deque.rs:101-123](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L101-L123) —— 注释列出三篇论文（带 ACM 链接），紧接着是 `Inner<T>`：`front`、`back`（`AtomicIsize`）加 `buffer: CachePadded<Atomic<Buffer<T>>>`。这三篇论文不是装饰：它们精确对应本讲后面要讲的 fence 放置（论文 2）和 volatile hack 的存在理由（论文 1 的原算法用 SeqCst，论文 2 降级后必须靠 fence 补回）。

读者要建立的心智模型是：**本 crate 的并发正确性不是凭空写出来的，而是「把论文里的算法搬过来 + 用 Rust 的 atomic/fence 原语忠实复现」**。当你对某条 fence 的存在感到疑惑时，第一反应应该是「去论文 2 里找对应」，而不是「删掉试试」。

#### 4.1.4 代码实践

**实践目标**：确认这三篇论文与本 crate 行为的对应关系。

**操作步骤**：

1. 打开 [src/deque.rs:101-113](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L101-L113)，记下三个链接。
2. 用浏览器打开论文 2 的链接（`dl.acm.org/citation.cfm?id=2442524`），阅读摘要，重点找它提到在 `steal` 操作中插入的 fence。
3. 在本文件里搜索 `A SeqCst fence is needed here`（出现在 `steal`、`steal_batch_with_limit`、`steal_batch_with_limit_and_pop` 三处），对照论文确认这些 fence 就是论文 2 要求的修正。

**需要观察的现象**：源码里凡是「先 Acquire 读 front、再 Acquire 读 back」的偷取操作，中间都夹着一处 `SeqCst fence`（或可重入时手动补发），且每处都带注释解释——这正是论文 2 修复点的 Rust 翻译。

**预期结果**：你能用一句话说出「这些 fence 不是程序员拍脑袋加的，是论文 2 证明弱内存模型下必需的」。

#### 4.1.5 小练习与答案

**练习 1**：为什么源码不直接把所有原子操作都设成 `SeqCst`（像原 Chase-Lev 那样），而要降级成 Acquire/Release + fence？

**参考答案**：全 `SeqCst` 在弱内存模型 CPU 上代价高（要在关键路径插更重的屏障）。论文 2 证明：只要在 steal 的 front-load 与 back-load 之间插一道 `SeqCst fence`，其余操作就能安全降级为 Acquire/Release，从而把昂贵的 SeqCst 从热路径上挪走，只在必要的「仲裁点」保留。

**练习 2**：CDSchecker（论文 3）在这个项目里扮演什么角色？

**参考答案**：它是开发期使用的模型检验工具，用来枚举弱内存模型下所有可能的交错执行，自动检查数据结构是否违反不变式。它为「论文 1 + 论文 2 的组合确实正确」提供了机器验证层面的信心，而不是运行时组件。

---

### 4.2 Buffer 的 volatile 读写 hack：一次「技术性数据竞争」的折中

#### 4.2.1 概念说明

理想的 work-stealing 队列里，push 线程写某个槽位、steal 线程读同一槽位，应当用**原子 load/store** 来避免数据竞争。但 Rust 的原子类型只支持指针宽度（最多 8 字节）的类型，且要求类型按位可拷贝。`crossbeam-deque` 的队列是**泛型 `T`** 的——`T` 可能是几十字节的结构体、可能包含指针——根本没法用一个原子操作整体读写。

于是作者做了一个**有意识的折中**：槽位仍用 `MaybeUninit<T>`（非原子）存储，读写改用 `ptr::write_volatile` / `read_volatile`。注释坦白承认：这「技术上是数据竞争、是 UB」，但比起「为每种 `T` 实现一套原子读写」更通用、更快。可见性/排序的保证不靠 volatile，而是靠 push/steal 两侧的 fence + 原子索引（见 4.3）。

volatile 在这里的实际作用是：

- 保证槽位读写**确实发生**、不被编译器消除或与其他访问合并。
- 在主流硬件上，对齐的、不超过机器字宽的写本身是原子的（硬件层面），volatile 让编译器生成「单条 load/store 指令」而非拆分。

#### 4.2.2 核心流程

`Buffer` 是「裸指针 + 容量」的薄壳，容量恒为 2 的幂：

- `at(index)`：用 `index & (cap - 1)` 做 O(1) 环形寻址（2 的幂取模技巧）。
- `write(index, task)`：`ptr::write_volatile(self.at(index), task)`。
- `read(index)`：`ptr::read_volatile(self.at(index))`。

注意 `Buffer` 被标记为 `Copy`，它的 `Drop` **不释放内存**——唯一释放途径是显式调用 `dealloc`（见 u2-l5、u4-l2 的 epoch 回收）。

#### 4.2.3 源码精读

`at` 的环形寻址与注释里「按 MaybeUninit 加载」的说明：

[src/deque.rs:64-70](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L64-L70) —— 「我们可能在加载之后才发现自己其实无权访问这块内存」，所以一律按 `MaybeUninit` 处理。

`write` 与 `read` 的 volatile 实现及 UB 说明注释：

[src/deque.rs:72-90](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L72-L90) —— 注释原文：*"technically speaking a data race and therefore UB. We should use an atomic store here, but that would be more expensive and difficult to implement generically for all types T. Hence, as a hack, we use a volatile write instead."*

**对照 Injector 的「干净」做法**：Injector 的 `Slot` 额外带一个 `state: AtomicUsize` 字段（`WRITE`/`READ`/`DESTROY` 位），任务体虽也在 `UnsafeCell<MaybeUninit<T>>` 里，但读写被原子 `state` 门控——生产者写完任务再 `fetch_or(WRITE, Release)`，消费者 `wait_write` 先 `state.load(Acquire)` 自旋等到 `WRITE` 才读任务：

[src/deque.rs:1196-1230](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1196-L1230) —— Injector 用「原子状态位 + Release/Acquire 配对」让任务体的非原子读写被同步，**没有** Buffer 那种「技术性数据竞争」。代价是每个槽位多一个原子字段。两种设计各有取舍：Worker/Stealer 走极致性能（owner 单线程独占、抢竞争窗口极小），Injector 走严格正确（MPMC 高竞争，必须用原子门控）。

#### 4.2.4 代码实践

**实践目标**：直观感受「为什么不能用普通原子替换 volatile」。

**操作步骤**：

1. 阅读 [src/deque.rs:72-90](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L72-L90) 的注释。
2. 思考：如果 `T` 是 `struct Task { data: [u64; 4] }`（32 字节），Rust 的 `AtomicU64`/`AtomicPtr` 能直接整体原子读写它吗？
3. 在自己的草稿里写下「若强行用 `AtomicPtr` 指向堆上 `Task`」的方案会带来什么额外开销（多一次堆分配 + 间接寻址 + epoch 要回收 `Task` 本体）。

**需要观察的现象 / 预期结果**：你会得出结论——对泛型 `T`，volatile 内联写是「在通用性与性能之间最实用的折中」，代价是接受一处良性 UB。**待本地验证**：若你用 Miri（`cargo +nightly miri test`）跑涉及 Worker/Stealer 的并发测试，Miri 是否报告这处数据竞争？记录其行为（注意 Miri 对 volatile 并发的判定与版本相关）。

#### 4.2.5 小练习与答案

**练习 1**：既然 volatile 是 UB，为什么这个 crate 还能声称「正确」？

**参考答案**：它的「正确」分两层含义：(1) 在抽象内存模型层面，槽位读写的可见性由 fence + 原子索引保证（4.3），volatile 只负责「访问确实发生」；(2) 在工程层面，它针对具体硬件（对齐的、字宽内的访问硬件原子）有效，并用论文 2 的 fence 修复 + 论文 3 的模型检验背书。「技术性 UB」是一种已知的、被审慎接受的工程债，而非隐藏缺陷。

**练习 2**：把 `Buffer::write` 的 `ptr::write_volatile` 换成普通 `ptr::write` 会出什么问题？

**参考答案**：普通非 volatile 写，编译器可能把它与相邻访问合并、重排，或在判定「值未被使用」时消除它。在并发偷取场景下，这会破坏「push 写槽位 → fence → store back」的发布顺序，让 steal 侧读到未写入或被重排掉的槽位，产生随机腐败。volatile 锁死访问点，是这套 fence 方案能成立的前提。

---

### 4.3 happens-before 的桥梁：fence + Acquire/Release 配对

#### 4.3.1 概念说明

「数据竞争」之所以危险，本质是缺少 happens-before。`crossbeam-deque` 用一条经典的「release 发布 / acquire 接收 + SeqCst fence 仲裁」链，把 push 线程对槽位的写安全地传递给 steal 线程。本模块把这条链拆成两端对照看。

#### 4.3.2 核心流程

**push 侧（生产者发布）**，伪代码：

```
b = back.load(Relaxed)
f = front.load(Acquire)        // 读个较新的 front 用于判满
if len >= cap { resize(2*cap) }
slot[b].write(task)            // 非原子写（volatile）
fence(Release)                 // ← 关键：把上面的写「封进」发布
back.store(b+1, Relaxed)       // 用 Relaxed 即可，fence 已承担 Release 语义
```

注意：`back.store` 用的是 `Relaxed`，**真正承担「发布」的是它前面的 `fence(Release)`**。fence(Release) 保证：fence 之前的所有写（含 `slot[b].write`）都不会被重排到随后的 `back.store` 之后。于是任何通过 Acquire 读到新 `back` 值的线程，都必然能看到那条 slot 写。

**steal 侧（消费者接收）**，伪代码：

```
f = front.load(Acquire)        // ① 读 front
if is_pinned() { fence(SeqCst) } // ② 可重入时手动补 fence
guard = epoch::pin()           //   pin() 自带 SeqCst fence（首次进入时）
b = back.load(Acquire)         // ③ 读 back
if b - f <= 0 { return Empty }
buffer = inner.buffer.load(Acquire, guard)
task = buffer.read(f)          // 读槽位
if buffer 变了 || CAS(front: f→f+1, SeqCst) 失败 {
    return Retry                // 槽位被换/被抢，丢弃 task（不 drop）
}
return Success(task.assume_init())
```

`front.load(Acquire)` 与 `back.load(Acquire)` 之间**必须夹一道 SeqCst fence**（论文 2 的核心修复）。fence 保证「读 front」严格早于「读 back」，防止弱内存模型下构造出一个「自相矛盾的 (front, back) 快照」从而读到正被并发覆盖的槽位。

#### 4.3.3 源码精读

push 的发布顺序（写槽位 → fence(Release) → store back，含 tsan 双路径）：

[src/deque.rs:399-433](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L399-L433) —— 第 418-420 行写槽位，423-429 行是 tsan 双路径：普通模式 `fence(Release)` + `Relaxed` store；tsan 模式跳过 fence、改用 `Release` store（因为 ThreadSanitizer 不理解 fence，详见 u4-l3）。第 432 行 `back.store(b+1, store_order)`。

steal 的接收顺序（Acquire 读 front → SeqCst fence → Acquire 读 back → 读槽位 → CAS 仲裁）：

[src/deque.rs:641-683](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L641-L683) —— 第 643 行 `front.load(Acquire)`；650-652 行 `if epoch::is_pinned() { fence(SeqCst) }`（注释解释：若当前线程已被 epoch pin（可重入），`pin()` 不会再补 fence，所以手动补）；654 行 `epoch::pin()`（首次 pin 时自带 SeqCst fence）；657 行 `back.load(Acquire)`；665 行 `buffer.load(Acquire)`；666 行读槽位；670-675 行「buffer 是否被换 + CAS front」二次校验。

`is_empty` / `len` 复用同一范式（无条件补 fence）：

[src/deque.rs:598-624](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L598-L624) —— 这两个查询方法不经过 epoch pin，所以无条件 `fence(SeqCst)`（600、621 行），同样的「Acquire front → SeqCst fence → Acquire back」三段式，返回的是大致快照而非精确点查询。

**配对关系总览**：

| 发布侧（push / FIFO pop / LIFO pop） | 接收侧（steal） | 建立的保证 |
|---|---|---|
| `slot.write` → `fence(Release)` → `back.store` | `back.load(Acquire)` 看到 b+1 | 接收方看到 slot 写 |
| `back.store(b)`（LIFO pop 减 back）→ `fence(SeqCst)` | `front.load` → `fence(SeqCst)` | 仲裁「最后一个任务」归属 |
| `front.fetch_add(SeqCst)`（FIFO pop） | `front` CAS(SeqCst)（steal） | SeqCst 全序下唯一认领 |

#### 4.3.4 代码实践

**实践目标**：把 `Stealer::steal`（[src/deque.rs:641-683](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L641-L683)）里的每一次原子操作与它的 Ordering 列成表，并画出它与 `Worker::push` 的 happens-before 同步图，说明「为什么不会读到未初始化的槽位」。这是本讲的主实践。

**操作步骤**：

1. 列表（按下表填写）。打开源码逐行核对 Ordering：

   | 行号 | 操作 | Ordering | 作用 |
   |---|---|---|---|
   | 643 | `front.load` | Acquire | 读当前队头 |
   | 650-652 | `fence`（仅 `is_pinned()` 时） | SeqCst | 与 push 的发布配对，锁住 front/back 读取顺序 |
   | 654 | `epoch::pin()` | （内部 SeqCst fence） | 进入临界区 + 补 fence |
   | 657 | `back.load` | Acquire | 读队尾，判空 + 看到已发布 slot 写 |
   | 665 | `buffer.load` | Acquire | 读当前 buffer 快照 |
   | 670 | `buffer.load`（二次） | Acquire | 校验 buffer 是否被 resize 换掉 |
   | 674 | `front.compare_exchange` | SeqCst / Relaxed | 认领槽位，失败返回 Retry |

2. 在纸上画同步图（文本示意）：

   ```
   push 线程                          steal 线程
   ----------                         ----------
   slot[b].write(task)                 f = front.load(Acquire)   ──┐
       │                               fence(SeqCst)  ◄────────── │ 配对
   fence(Release)  ◄═══════════════╗   b = back.load(Acquire) ◄───┘
       │                            ║       │ 看到新 back ⇒ 看到 slot 写
   back.store(b+1) ─────────────────╝   task = buffer.read(f)
                                        CAS front(f→f+1, SeqCst)
   ```

   - `fence(Release)`（push）与 `back.load(Acquire)`（steal）之间由 `back` 的 store/load 牵线，形成 release-acquire 配对。
   - `fence(SeqCst)`（steal）确保「读 front 先于读 back」，与 push 侧共同防止弱内存模型下的快照错乱。

3. 写出「不会读到未初始化槽位」的论证：

   - 假设 steal 决定非空（`b - f > 0`），即它读到了某个 `back ≥ f+1`。
   - 该 `back` 值只可能由一次 push 在写完 `slot` 并执行 `fence(Release)` 后 `store` 出来。
   - 由 release-acquire：steal 的 `back.load(Acquire)` 看到该值 ⇒ 它也看到了那次 push 的 `slot.write`（以及更早的所有 slot 写）。
   - 因此 `buffer.read(f)` 读到的必定是被 push 写入过的有效任务，而非 `MaybeUninit` 的垃圾。
   - 即使 push 之后发生 resize 换了 buffer，670 行的二次 `buffer.load` 会发现 `buffer != buffer`，直接返回 `Retry`，不会冒然 `assume_init`。

**需要观察的现象**：你能把这张图讲给一个不熟悉本 crate 的人听，并且能指出「若删掉 650-652 的 SeqCst fence，在 ARM 上可能出什么问题」（答：可能读到 front 旧值、back 新值，导致读到一个已被并发覆盖的槽位）。

**预期结果**：得到一张标注完整的操作表 + 一张同步关系图 + 一段三句话论证。**本步骤为纯源码阅读型实践，无需运行命令。**

#### 4.3.5 小练习与答案

**练习 1**：`push` 里 `back.store` 用 `Relaxed`，会不会导致 steal 看不到新任务？

**参考答案**：不会。可见性由 `back.store` **之前**的 `fence(Release)` 提供——fence 保证 `slot.write` 在 store 之前对其他线程可见，且 store 本身的「值更新」即便 Relaxed 也会被 steal 的 `Acquire` load 观察到（store/load 的值传播不需要强 ordering，需要的是两侧的 fence/load 来约束周边访问顺序）。

**练习 2**：为什么 `steal` 在 `is_pinned()` 为真时要手动补 `fence(SeqCst)`，而首次 `epoch::pin()` 时却不用？

**参考答案**：`epoch::pin()` 在线程首次进入临界区时内部会发一道 SeqCst fence；但若当前线程已经处于 pinned 状态（可重入），再次 `pin()` 是轻量的，不再发 fence。此时 steal 仍需要那道 fence 来锁住 front/back 顺序，所以必须手动补。注释 [src/deque.rs:645-652](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L645-L652) 明确解释了这一点。

---

### 4.4 fence 的精确定位：LIFO pop 与 steal 中的 SeqCst fence

#### 4.4.1 概念说明

`SeqCst fence` 不是「想加就加」——加错位置要么无效，要么拖慢热路径。本模块专门讲「为什么 fence 恰好放在那里」。规律是：**fence 总是夹在「预约自己这一端」与「读对端」之间**。两端各自只信自己的索引，fence 负责把两端的视图对齐到一个一致的全局时刻。

#### 4.4.2 核心流程

**LIFO pop 的 fence 定位**（[src/deque.rs:490-543](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L490-L543)）：

```
b = back - 1
back.store(b, Relaxed)     // ① 预约「我要从 back 端拿 slot b」
fence(SeqCst)              // ② ← fence 夹在「预约 back」与「读 front」之间
f = front.load(Relaxed)    // ③ 读对端
if b - f < 0 { 恢复 back; None }   // 队列其实空了（被 stealer 抢光）
else {
    read slot[b]
    if b - f == 0 {         // 只剩最后一个任务：owner 的 back 端与 stealer 的 front 端撞上
        CAS(front: f→f+1, SeqCst)  // 用 CAS 仲裁唯一赢家
    }
}
```

fence 在 ① 和 ③ 之间的作用：让 owner 对 `back` 的「减 1 预约」先于它对 `front` 的读被全局可见。这样，若此刻一个 stealer 正要从 front 端偷这最后一个任务，stealer 侧的 `fence(SeqCst)` + `front.load` 会和 owner 的 `fence(SeqCst)` 落入同一个 SeqCst 全序，谁先认领 `front` 由后续 CAS 决定，保证「最后一个任务恰好被消费一次」。

**steal 的 fence 定位**（已在 4.3 讲过）：夹在 `front.load`（预约/读队头）与 `back.load`（读对端判空）之间，结构完全对称。

**FIFO pop 为何不需要单独 fence**：FIFO pop 用 `front.fetch_add(1, SeqCst)`（[src/deque.rs:467](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L467)）——这是一个**单条 SeqCst RMW 操作**，SeqCst 已内建其中，无需额外 fence。

#### 4.4.3 源码精读

LIFO pop 的 fence 与最后元素 CAS：

[src/deque.rs:490-543](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L490-L543) —— 第 493 行 `back.store(b, Relaxed)`（预约），495 行 `fence(SeqCst)`，498 行 `front.load(Relaxed)`（读对端），515-528 行最后元素的 `compare_exchange(SeqCst)` 仲裁。

FIFO pop 的 SeqCst 内建式：

[src/deque.rs:465-487](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L465-L487) —— 第 467 行 `fetch_add(1, SeqCst)` 直接抢占 front；若越界（470 行）则 `store` 回退返回 `None`。

Injector::steal 里的同类 fence（对照参考）：

[src/deque.rs:1487-1500](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1487-L1500) —— 第 1488 行 `fence(SeqCst)`，夹在「读 head」与「读 tail 判空」之间，结构与 Worker/Stealer 的 steal 同构：fence 用于在弱内存模型下安全地比较 head 与 tail。

把四处放一起看，规律一致：

| 操作 | 「预约/读自己这端」 | fence | 「读对端」 |
|---|---|---|---|
| LIFO pop | `back.store(b)` | `fence(SeqCst)` (L495) | `front.load` (L498) |
| steal | `front.load(Acquire)` | `fence(SeqCst)` (L651) | `back.load(Acquire)` (L657) |
| is_empty/len | `front.load(Acquire)` | `fence(SeqCst)` (L600) | `back.load(Acquire)` (L601) |
| Injector steal | `head.load(Acquire)` | `fence(SeqCst)` (L1488) | `tail.load` (L1489) |

#### 4.4.4 代码实践

**实践目标**：验证「fence 必须夹在预约与读对端之间」这条规律。

**操作步骤**：

1. 对照上面的表格，在源码里逐处确认 fence 的「左邻」是不是一次对自己端索引的写/读、「右邻」是不是对对端索引的读。
2. 思考实验：假设把 LIFO pop 的 `fence(SeqCst)`（L495）移到 `back.store`（L493）**之前**，会发生什么？（提示：owner 还没把「我占了 slot b」发布出去就读 front，可能和 stealer 同时认为自己是最后一个任务的主人。）
3. 把你对步骤 2 的分析写成一两句结论。

**需要观察的现象 / 预期结果**：你会确认 fence 只能放在「发布自己端的预约」**之后**、「读对端」**之前**——放早了预约没生效，放晚了读对端已失去同步。这正是论文 2 给出的精确位置。

#### 4.4.5 小练习与答案

**练习 1**：FIFO pop 没有 `fence(SeqCst)`，它怎么保证正确性？

**参考答案**：FIFO pop 的抢占动作是 `front.fetch_add(1, SeqCst)`——这是一条 SeqCst 的读-改-写操作，本身就参与 SeqCst 全序，等价于把 fence「内嵌」进原子操作里。它若发现抢占越界（任务其实已被偷走），就 `store` 回退 front 并返回 `None`。

**练习 2**：LIFO pop 在「最后一个任务」时为什么用 CAS 而不是直接拿？

**参考答案**：当 `len == 0`（即 `b == f`）时，owner 从 back 端看到的最后任务，正是 stealer 从 front 端可能正在偷的同一个任务。两方都可能认为自己有权拿它。用 `compare_exchange(front: f→f+1, SeqCst)` 做仲裁：CAS 成功者拿走任务，失败者说明被 stealer 抢先，于是 `task.take()` 丢弃本地副本、返回 `None`。CAS 的「恰好成功一次」语义保证了任务既不丢失也不被消费两次。

---

## 5. 综合实践

把本讲四条主线串起来做一次「源码审计」：

1. **三篇论文**：打开 [src/deque.rs:101-113](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L101-L113)，确认论文 2 对应全文件所有 `fence(SeqCst)`。
2. **volatile hack**：打开 [src/deque.rs:72-90](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L72-L90)，对照 Injector 的 [src/deque.rs:1196-1230](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1196-L1230)，写一段 5 行以内的对比：「Buffer 用 volatile（技术性 UB）换取泛型 `T` 的内联写；Injector 用原子 state 门控换取严格无 UB，代价是每槽多一个原子字段」。
3. **配对链**：完成 4.3.4 的操作表与同步图。
4. **fence 定位**：完成 4.4.4 的表格核对。

最终交付：一份不超过一页的「内存序审计报告」，包含——

- 一张 push↔steal 的 happens-before 同步图；
- 一张「fence 位置规律」表（4.4.3 那张）；
- 一段话回答：「为什么删掉 `Stealer::steal` 里的 SeqCst fence（L651）在 ARM 上可能读到未初始化槽位？」

参考答案要点：删掉后，steal 可能读到 front 的旧值、back 的新值，从而读到一个尚未被 push 写入（或正被并发覆盖）的槽位；fence 强制 front 读先于 back 读，使快照自洽，再由 release-acquire 保证看到该 back 值就看到对应 slot 写。

## 6. 本讲小结

- **正确性靠论文**：`Inner` 注释引用的三篇论文（Chase-Lev、弱内存模型、CDSchecker）分别给出原始算法、弱内存修复、模型检验；全文件所有 `fence(SeqCst)` 都是论文 2 的直接产物。
- **volatile 是泛型折中**：`Buffer::write/read` 用 `ptr::write_volatile/read_volatile`，注释坦承是「技术性数据竞争、UB」，因为对任意 `T` 无法做整体原子读写；可见性靠 fence + 原子索引保证，volatile 只保证「访问确实发生」。
- **发布-接收配对**：push 用「写槽位 → `fence(Release)` → `back.store(Relaxed)`」发布；steal 用「`front.load(Acquire)` → `fence(SeqCst)` → `back.load(Acquire)`」接收，形成 happens-before。
- **Injector 是对照**：`Slot` 用显式 `state: AtomicUsize`（WRITE/READ/DESTROY）门控任务体的非原子读写，靠 `fetch_or(WRITE, Release)` 与 `wait_write` 的 `load(Acquire)` 配对，做到严格无 UB。
- **fence 位置有规律**：所有 SeqCst fence 都夹在「预约/读自己这一端」与「读对端」之间；FIFO pop 因用 `fetch_add(SeqCst)` 而无需单独 fence。
- **tsan 是特例**：ThreadSanitizer 不理解 fence，故 push/steal_batch 末尾在 tsan 模式下改用 `Release` store（详见 u4-l3）。

## 7. 下一步学习建议

- **u4-l2（epoch GC）**：本讲只讲了「可见性」，没讲「旧 buffer 何时能安全释放」——那是 crossbeam-epoch 的职责。`Stealer::steal` 里的 `epoch::pin()`、`resize` 里的 `defer_unchecked` 正是为此存在。
- **u4-l3（ThreadSanitizer 兼容）**：本讲多次提到「tsan 模式下用 Release store 替代 fence」，下一讲专门讲 `build.rs` 如何探测 tsan 并切换代码路径。
- **延伸阅读**：若想深究 fence 的形式化证明，直接读 `Inner` 注释里的论文 2（Le et al., PPoPP 2013）；想看机器验证，了解 CDSchecker（论文 3）。
- **动手**：用 `cargo +nightly miri test` 跑 `tests/fifo.rs`、`tests/lifo.rs`，观察 Miri 对这套 volatile + fence 方案的判定，作为对本讲「技术性 UB」讨论的实证补充。
