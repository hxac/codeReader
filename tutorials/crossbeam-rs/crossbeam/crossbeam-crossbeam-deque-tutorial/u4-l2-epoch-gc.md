# 无锁内存回收：crossbeam-epoch 与 Buffer 生命周期

## 1. 本讲目标

本讲专讲 crossbeam-deque 里一个「看不见但决定生死」的问题：**当一个无锁队列把底层 buffer 换掉以后，旧的那块内存到底什么时候、由谁、用什么机制安全地释放掉？**

学完本讲你应该能够：

- 说清楚「swap 掉旧 buffer 后不能立刻 free」的根本原因（其他线程手里可能还攥着旧指针）。
- 描述 crossbeam-epoch 的延迟回收模型：`epoch::pin()` 进入临界区、`defer_unchecked` 登记延迟回收、`guard.flush()` 尽早回收大块、`epoch::unprotected()` 用于单线程 Drop。
- 读懂 `Worker::resize` 里「`pin` → `swap` → `defer_unchecked` → `flush`」这一套组合拳。
- 读懂 `Stealer::steal` / `steal_batch*` 里 `epoch::pin()` 如何保护它加载到的 buffer 指针，以及 `epoch::is_pinned()` 那段「可重入补 fence」的逻辑。
- 理解 `Inner::drop` 为什么敢用 `epoch::unprotected()` 直接释放，而不走 GC。

本讲是 **u2-l5（resize 与 reserve）** 的延伸：u2-l5 讲了「容量如何变」，本讲讲「被换掉的旧 buffer 如何安全退役」。

## 2. 前置知识

在进入源码前，先用一个通俗的比喻建立直觉。

### 2.1 无锁数据结构的「悬挂指针」困境

普通带锁数据结构里，释放内存很简单：拿一把锁，确认没人访问，再 free。但 crossbeam-deque 是 **lock-free（无锁）** 的——偷取操作（`steal`）只做几次原子读 + 一次 CAS，**绝不加锁**。这带来一个麻烦：

考虑 `resize` 把底层 buffer 从旧的换成新的。换完之后，能不能立刻 `free` 旧 buffer？

**不能。** 因为可能正有一个 stealer 线程执行到这一步：

1. 它已经原子地 `load` 出了**旧** buffer 的指针（准备读某个槽位）；
2. 但还没真正去读那个槽位里的字节；
3. 就在这两步之间，owner 把 buffer 换掉并 free 了旧的。

此时 stealer 手里攥着一个**已经被释放的指针**，接下来去读槽位就是典型的 **use-after-free**（释放后使用）。

加锁能解决，但会毁掉无锁的性能优势。**epoch-based reclamation（EBR，基于纪元的内存回收）** 就是为这类场景设计的折中方案。

### 2.2 用「博物馆」比喻理解 EBR

把共享数据结构想象成一座博物馆：

| crossbeam-epoch 概念 | 博物馆比喻 | 作用 |
|---|---|---|
| `epoch::pin()` | 游客在门口领一个手环（`Guard`） | 标记「我在馆内参观」，进入临界区 |
| `Guard` 被 drop（函数返回） | 游客归还手环离馆 | 退出临界区，不再受保护 |
| 全局 epoch 计数 | 馆方墙上翻页的日期 | 全局单调推进的「版本号」 |
| `defer_unchecked(closure)` | 馆方把某展柜贴「待撤」标签，记下当天日期 | 登记「这块旧内存以后再回收」 |
| 实际执行 closure | 等日期翻过两页且确认无在册游客停留在旧日期，才搬走展柜 | 两个 epoch 推进后安全释放 |
| `guard.flush()` | 馆方立刻清点本地仓库，能搬走的就搬 | 尽早回收，降低内存占用 |
| `epoch::unprotected()` | 一张「假手环」 | 用于确认此刻无其他游客的独占场景（如 Drop） |
| `epoch::is_pinned()` | 查「我手上是不是已经戴着手环」 | 判断是否可重入 |

关键直觉：**只要还有一个游客戴着旧日期的手环在馆里，馆方的日期就翻不动**（因为 epoch 推进要求所有参与者都更新到新日期）。所以只要日期翻了两页，就说明「当年可能看到那个旧展柜的游客，早就都离开了」——这时搬走旧展柜绝对安全。

> 数学上，回收条件可粗略表述为：若旧 buffer 在全局纪元 \(e\) 时被退役（retire），则只有当全局纪元已推进到至少 \(e+2\)，且回收线程观测到所有参与者都已离开 \(e\) 之后，才真正 dealloc。这个「两页」的安全裕度就是 EBR 不发生 use-after-free 的保证。

### 2.3 你需要先掌握的术语

- **临界区（critical section）**：线程持有 `Guard` 的那段代码，期间它访问的内存受 epoch 保护。
- **retire / 退役**：标记一块内存「以后要释放」，但不当场 free。
- **Atomic\<T\>**：crossbeam-epoch 提供的原子智能指针类型，本 crate 用它包住 `Buffer<T>`，使「换 buffer」成为一次原子 `swap`。
- 这些前置概念在 **u2-l1（Buffer/Inner 结构）**、**u2-l5（resize）**、**u4-l1（内存序）** 已建立，本讲承接它们。

## 3. 本讲源码地图

本讲只涉及两个文件，但只盯其中与「内存回收」相关的几处：

| 文件 | 本讲关注的代码点 |
|---|---|
| `src/deque.rs` | 顶部 epoch 导入（L12）、`FLUSH_THRESHOLD_BYTES` 常量（L23）、`Buffer::dealloc`（L55-L62）、`Inner::drop`（L125-L145）、`Worker::resize`（L289-L322）、`Stealer::steal`（L641-L683）、`Stealer::steal_batch_with_limit`（L746-L925）、`Stealer::steal_batch_with_limit_and_pop`（L989-L1178） |
| `Cargo.toml` | `crossbeam-epoch` 依赖声明（L36）、`std` feature 对 epoch/std 的开启（L33） |

一句话地图：**epoch 的「退役侧」（retire）在 `Worker::resize`，epoch 的「保护侧」（pin）在 `Stealer::steal` 系列，而 epoch 的「绕过侧」（unprotected）在 `Inner::drop`。** 把这三处串起来，本讲的全部内容就清晰了。

> 注：`Injector` 用的是另一套完全不同的回收机制（基于 `READ`/`DESTROY` 状态位的协作式 `Block::destroy`，**不用 epoch**），那是 u3-l3 的内容，本讲不展开。epoch 只服务于 Chase-Lev 的 `Buffer` 生命周期。

## 4. 核心概念与源码讲解

### 4.1 延迟回收的动机与 crossbeam-epoch API 模型

#### 4.1.1 概念说明

无锁队列里，owner 线程在 `resize` 时会原子地**换掉**共享的底层 buffer（`swap`），换下来的旧 buffer 不能立刻 `dealloc`——因为 stealer 可能正持着旧指针读槽位。需要一种机制，把旧 buffer 标记为「退役」，等到**确认没有任何线程再持有它**时才真正释放。

crossbeam-epoch 就是这套机制。本 crate 在文件顶部把它引入，并只用到其中 5 个 API：

[crossbeam-deque/src/deque.rs:12-12](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L12-L12) —— 把 `crossbeam_epoch` 起别名 `epoch`，并导入 `Atomic`（原子智能指针）与 `Owned`（ Owned 形态，用于创建新指针）。

```rust
use crossbeam_epoch::{self as epoch, Atomic, Owned};
```

依赖在 Cargo.toml 里声明（注意 `default-features = false`，再由 `std` feature 把 `crossbeam-epoch/std` 开回来）：

[crossbeam-deque/Cargo.toml:36-36](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/Cargo.toml#L36-L36) —— 本 crate 的核心外部依赖之一。

```toml
crossbeam-epoch = { version = "0.9.17", path = "../crossbeam-epoch", default-features = false }
```

#### 4.1.2 核心流程

把本 crate 用到的 5 个 API 及其角色列出来：

| API | 调用位置 | 角色 |
|---|---|---|
| `epoch::pin()` | `resize`、`steal`、`steal_batch*` | 领手环，进入临界区；返回的 `Guard` 在函数末尾自动 drop（退馆） |
| `Guard::defer_unchecked(closure)` | `resize` | 登记旧 buffer 的回收闭包（退役） |
| `Guard::flush()` | `resize`（大 buffer 时） | 尽早清点并回收本线程的退役垃圾 |
| `epoch::unprotected()` | `Inner::drop` | 拿一个「假手环」，用于确认无并发的单线程场景 |
| `epoch::is_pinned()` | `steal`、`steal_batch*` | 判断当前线程是否已 pin（可重入判断） |

一条贯穿全讲的「生命周期时间线」：

1. owner 在 `resize` 中分配新 buffer，`pin` 自己，用 `swap` 把共享指针换成新的，拿到旧 buffer；
2. owner 调 `defer_unchecked`，把「释放旧 buffer」的闭包登记到本线程的垃圾袋，记下当前 epoch；
3. 与此同时，stealer 在 `steal` 里 `pin` 自己后，`load` 出（可能是旧的）buffer 指针并读槽位——只要它还 pin 着，旧 buffer 就不会被释放；
4. stealer 函数返回，`Guard` drop，该线程退出临界区；
5. 当全局 epoch 推进足够（两页）且回收点观测到所有相关线程都已退出，旧 buffer 的退役闭包才真正执行 `dealloc`。

第 3、4 步是「保护侧」，第 1、2、5 步是「退役侧」。

#### 4.1.3 源码精读

`Buffer` 是一个「指针 + 容量」的薄壳，**它的 `Drop` 不会释放内存**（结构体上没有实现释放语义，且它被标记为 `Copy`）。唯一释放途径是显式调用 `dealloc`：

[crossbeam-deque/src/deque.rs:55-62](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L55-L62) —— 把裸指针还原成 `Box<[MaybeUninit<T>]>` 再 drop，从而释放堆内存。

```rust
unsafe fn dealloc(self) {
    drop(unsafe {
        Box::from_raw(ptr::slice_from_raw_parts_mut(
            self.ptr.cast::<MaybeUninit<T>>(),
            self.cap,
        ))
    });
}
```

正因为 `dealloc` 是「直接释放」，它必须在**确认安全**后才能调用——这正是 epoch 要解决的问题。本讲后面三节（4.2 / 4.3 / 4.4）分别讲退役侧、保护侧、绕过侧如何安全地走到 `dealloc`。

#### 4.1.4 代码实践

**实践目标**：在脑中把 5 个 API 与「博物馆比喻」对上号。

**操作步骤**：

1. 打开 [crossbeam-deque/src/deque.rs:12](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L12)，确认本 crate 只导入了 `Atomic` 和 `Owned`，其余 API（`pin`、`is_pinned`、`unprotected`）都通过 `epoch::` 路径调用。
2. 在文件里搜索 `epoch::` ，统计它出现的次数和位置（退役侧、保护侧、绕过侧各几处）。

**需要观察的现象**：你会发现 `epoch::pin()` 出现在 `resize` 与所有 `steal*` 方法里；`epoch::unprotected()` 只出现在 `Inner::drop`；`epoch::is_pinned()` 出现在三个 `steal*` 方法里。

**预期结果**：epoch 的使用点恰好分布在「退役 / 保护 / 绕过」三类语义上，分布与本节表格一致。

#### 4.1.5 小练习与答案

**练习 1**：为什么不直接用一把 `Mutex` 保护 buffer 指针、在 `swap` 之后立即释放旧 buffer？

> **参考答案**：那样就不是 lock-free 了，`steal` 的快路径会退化为「加锁—读—解锁」，违背了 Chase-Lev 队列「偷取只读原子 + 一次 CAS、绝不阻塞」的设计目标。EBR 的价值正是让偷取保持无锁的同时安全回收内存。

**练习 2**：`epoch::pin()` 返回的 `Guard` 在 `steal` 函数末尾会发生什么？为什么这很重要？

> **参考答案**：`Guard` 离开作用域被 drop，该线程从 epoch 参与者中注销（「退馆」）。这很重要：只要它不注销，全局 epoch 就因它而无法推进，旧 buffer 的退役闭包就一直得不到执行——所以「及时退馆」是内存能被回收的前提。

---

### 4.2 Worker::resize：swap + defer_unchecked + flush 的三连击

#### 4.2.1 概念说明

`resize` 是本 crate 里**唯一**会换掉底层 buffer 的地方（`push` 满时翻倍、`pop` 到 1/4 时缩半，详见 u2-l5）。它是 epoch 的「退役侧」：在这里把旧 buffer 标记为退役，但不当场释放。

回忆 u2-l1 的不变式：**只有 owner 线程会改 buffer**（单写者），所有 stealer 都是只读。所以「换 buffer」这件事天然是单线程发起的；难点不在换，而在换完之后如何安全地处理旧的。

#### 4.2.2 核心流程

`resize` 的完整五步（前两步在 u2-l5 讲过，本讲聚焦后三步）：

1. 读窗口：`front`/`back`（`Relaxed`）与本地 buffer 快照；
2. 分配新 buffer，`copy_nonoverlapping` 逐槽拷贝；
3. **`epoch::pin()` 进入临界区**；
4. **`buffer.replace`（换本地缓存）+ `inner.buffer.swap`（原子换共享指针），拿到旧 buffer**；
5. **`defer_unchecked` 登记旧 buffer 的回收；若 buffer 大，再 `flush`**。

后三步的时序（与一个并发 stealer 的视角对照）：

```
owner (resize)                          stealer (steal)
─────────────                           ──────────────
                                        load front  (Acquire)
                                        [is_pinned? 补 fence]
                                        epoch::pin()  ← 进临界区
                                        load back    (Acquire)
pin()
swap 共享指针 → 拿到 old buffer
defer_unchecked( || old.dealloc() )
                                        load buffer  (Acquire, guard) ← 可能拿到旧指针
                                        read slot f  (volatile)
                                        ... CAS front ...
                                        Guard drop   ← 退临界区
（两个 epoch 推进后，确认无人持有旧指针）
执行 old.dealloc()  ← 真正释放
```

关键点：只要 stealer 在「load buffer」和「read slot」之间还 pin 着，`dealloc` 就执行不到——epoch GC 会被这个 pin 卡住，直到 stealer 退馆。

#### 4.2.3 源码精读

[crossbeam-deque/src/deque.rs:305-321](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L305-L321) —— `resize` 的 epoch 三连击。

```rust
let guard = &epoch::pin();                       // [1] 进临界区（领手环）

// Replace the old buffer with the new one.
self.buffer.replace(new);                        // [2a] 换 owner 私有缓存
let old =                                        // [2b] 原子换共享指针，拿到旧 buffer
    self.inner
        .buffer
        .swap(Owned::new(new).into_shared(guard), Ordering::Release, guard);

// Destroy the old buffer later.
unsafe { guard.defer_unchecked(move || old.into_owned().into_box().dealloc()) } // [3] 退役登记

// If the buffer is very large, then flush ...
if mem::size_of::<T>() * new_cap >= FLUSH_THRESHOLD_BYTES {  // [4] 大块则 flush
    guard.flush();
}
```

逐行说明：

- **[1] `epoch::pin()`**：owner 自己也领手环进临界区。这里的 pin 主要有两个用途——一是满足 epoch API 的签名要求（`swap`、`into_shared`、`defer_unchecked` 都需要一个 `Guard` 引用）；二是让本线程参与 epoch 计数。
- **[2a] `self.buffer.replace(new)`**：`self.buffer` 是 owner 的私有 `Cell<Buffer<T>>` 快速副本（u2-l1 讲过），这里换成新 buffer。注意它**不是**原子操作——因为只有 owner 会读写这个 `Cell`。
- **[2b] `inner.buffer.swap(...)`**：这是关键的**原子换指针**操作。`Owned::new(new).into_shared(guard)` 把新 buffer 转成 epoch 校验过的 `Shared` 指针；`swap` 用 `Release` 序把共享 `Atomic<Buffer<T>>` 换成新的，**返回值 `old` 就是旧 buffer**。`swap`（而非 `store`）正是为了拿回旧指针去登记回收。
- **[3] `guard.defer_unchecked(...)`**：闭包 `move || old.into_owned().into_box().dealloc()` 被**登记但不立即执行**。它会进入本线程的退役垃圾袋，等到 epoch 安全后才跑——这就是「延迟回收」。`old.into_owned().into_box()` 把 epoch 指针还原回 `Box`，再调本讲 4.1.3 的 `dealloc` 真正释放。
- **[4] `guard.flush()`**：当本次涉及的大缓冲区满足阈值时，主动清点本线程垃圾袋，能释放的尽早释放。

阈值常量定义在文件顶部：

[crossbeam-deque/src/deque.rs:21-23](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L21-L23) —— `FLUSH_THRESHOLD_BYTES = 1 << 10 = 1024` 字节。

```rust
// If a buffer of at least this size is retired, thread-local garbage is flushed so that it gets
// deallocated as soon as possible.
const FLUSH_THRESHOLD_BYTES: usize = 1 << 10;
```

为什么要 `flush`？退役垃圾默认是攒在**线程本地**的，等到该线程下次 `pin` 且全局 epoch 推进两次时才批量清理。如果刚退役的是一块大 buffer（比如 `size_of::<T>() * new_cap ≥ 1024` 字节），一直攥着它会很占内存，所以 `flush` 主动触发一次清点，让它在安全时尽快归还系统。注意 `flush` 只是在「安全的前提下」尽早回收，它不会绕过 epoch 的安全检查。

#### 4.2.4 代码实践

**实践目标**：用一个具体场景，说明 epoch GC 如何避免 use-after-free。（这是本讲的核心实践，属于「源码阅读型 / 推理型」实践——epoch 的回收时机无法用断言直接观测，需要靠推理把因果链讲清楚。）

**场景**：

- 线程 A（owner）持续 `push`，长度超过容量触发 `resize`；
- 线程 B（stealer）此刻正用旧 buffer 读槽位。

**操作步骤**：请你在纸面 / 注释里按下面的提纲写出推理，把每一步对应到源码行号：

1. 线程 B 执行 `Stealer::steal`，先 `epoch::pin()`（[src/deque.rs:654](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L654)），然后 `load` 出 buffer 指针（[L665](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L665)）——此时它拿到的可能是**旧** buffer。
2. 线程 A 在 `resize` 里 `swap` 掉共享指针（[L309-L312](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L309-L312)），拿到旧 buffer，并 `defer_unchecked`（[L315](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L315)）登记回收。
3. 关键问题：线程 B 在 `read(f)`（[L666](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L666)）时，旧 buffer 还在吗？
4. 回答：**在**。因为线程 B 还 pin 着（它的 `Guard` 活着），它参与了当前 epoch 计数。只要它不退馆，全局 epoch 就推不动，退役闭包 `old.dealloc()` 的执行条件（epoch 推进两页 + 无人在旧 epoch）永不满足。
5. 线程 B 读完槽位、CAS 推进 front，函数返回，`Guard` drop，退馆。
6. 此后全局 epoch 才可能推进两页，最终 `old.dealloc()` 被调用，旧 buffer 才真正释放——而此刻绝无任何线程还持有它。

**需要观察的现象**：推理过程中，你应该能指出「线程 B 的 pin 是卡住 dealloc 的唯一原因」。

**预期结果**：得出结论——**epoch 把「释放」推迟到「所有可能持有旧指针的线程都已退馆且全局 epoch 翻了两页」之后**，从而杜绝 use-after-free。

> 本实践为推理型，无法用单次运行断言验证「dealloc 的精确时机」。若想得到可观测的间接证据，可在本地用 nightly 跑 `MIRIFLAGS="-Zmiri-tag-raw-memory" cargo +nightly miri test`（Miri 能检测此类内存误用）；若 Miri 不报错，则间接佐证回收时序正确。具体能否复现取决于本地工具链，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`resize` 里为什么用 `swap` 而不是 `store` 来换 buffer？

> **参考答案**：因为需要**拿回旧 buffer 指针**才能为它登记回收闭包。`store` 只写入新值、丢弃旧值；`swap` 既换入新值、又返回旧值（`old`），退役登记才有目标。

**练习 2**：`defer_unchecked` 里的闭包 `old.into_owned().into_box().dealloc()` 是在 `resize` 调用结束时立刻执行的吗？

> **参考答案**：不是。它只是被**登记**到本线程的退役垃圾袋。真正执行发生在「两个 epoch 推进后、确认无线程持有旧指针」时，由 epoch GC 触发。`resize` 返回时旧 buffer 通常还没被释放。

**练习 3**：如果删掉末尾的 `if ... { guard.flush(); }`，旧 buffer 还会被释放吗？为什么还要写这句？

> **参考答案**：仍会被释放，只是更晚（等本线程下次 `pin` 且 epoch 推进两次时批量清理）。`flush` 是**内存占用优化**：当退役的是大 buffer 时，尽早清点本地垃圾袋，避免长时间占用大块内存。它不改变安全性，只改变「多快释放」。

---

### 4.3 Stealer::steal：epoch::pin 临界区与 is_pinned 重入 fence

#### 4.3.1 概念说明

`steal` 是 epoch 的「保护侧」。stealer 在偷取时要 `load` 出 buffer 指针并读槽位——这正是 4.2 里那个「线程 B 攥着旧指针」的场景。因此 stealer 必须先用 `epoch::pin()` 把自己钉在临界区里，保证读槽位期间 buffer 不会被释放。

本节还顺带讲一段看起来「跟内存回收无关」、实则与正确性强相关的代码：`epoch::is_pinned()` 配合手动 `SeqCst fence`。它解决的是 u4-l1 提到的弱内存模型正确性问题，在这里与 epoch 的可重入性耦合在一起。

#### 4.3.2 核心流程

`Stealer::steal` 的内存回收相关流程：

1. `Acquire` 加载 `front`；
2. **检查 `epoch::is_pinned()`**：若已 pin（可重入），手动补一个 `SeqCst fence`；
3. `epoch::pin()` 进入临界区（这一步本身也会顺带发一个 `SeqCst fence`）；
4. `Acquire` 加载 `back`，判空；
5. **`load` buffer 指针（受 guard 保护）**，读槽位；
6. CAS 推进 `front`，二次校验 buffer 是否被换；
7. 函数返回，`Guard` drop 退馆。

其中第 2、3 步是「补 fence + pin」的组合，第 5 步是「受保护地读 buffer」。

`steal_batch_with_limit` 与 `steal_batch_with_limit_and_pop` 的开头完全同构，本节一并说明。

#### 4.3.3 源码精读

[crossbeam-deque/src/deque.rs:643-666](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L643-L666) —— `steal` 的 pin 与受保护的 buffer 读取。

```rust
// Load the front index.
let f = self.inner.front.load(Ordering::Acquire);

// A SeqCst fence is needed here.
//
// If the current thread is already pinned (reentrantly), we must manually issue the
// fence. Otherwise, the following pinning will issue the fence anyway, so we don't
// have to.
if epoch::is_pinned() {
    atomic::fence(Ordering::SeqCst);
}

let guard = &epoch::pin();

// Load the back index.
let b = self.inner.back.load(Ordering::Acquire);
...
// Load the buffer and read the task at the front.
let buffer = self.inner.buffer.load(Ordering::Acquire, guard);  // 受 guard 保护
let task = unsafe { buffer.deref().read(f) };
```

逐段说明：

- **`epoch::pin()`（[L654](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L654)）**：领手环进临界区。此后 `guard` 活着期间，本线程参与 epoch 计数——这正是它能「卡住」退役 buffer 释放的原因（见 4.2.4）。
- **`buffer.load(Ordering::Acquire, guard)`（[L665](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L665)）**：epoch 的 `Atomic::load` **强制要求传一个 `Guard`**。这个 guard 参数的作用，正是让这次 load 出来的指针在 guard 存活期间「安全可解引用」。没有 guard，这步根本编译不过（API 层面强制）。
- **随后 `read(f)`（[L666](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L666)）**：实际读槽位字节。只要 guard 还活着，这块 buffer 就不会被 `dealloc`，所以读得安全。

**关于 `is_pinned()` 那段注释（[L645-L652](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L645-L652)）——这是本节最容易困惑处，重点讲**：

u4-l1 讲过，Le 等人的弱内存模型论文要求 `steal` 在「读 front」与「读 back」之间插一道 `SeqCst fence`。巧的是，`epoch::pin()` 内部**会顺带发一道 `SeqCst fence`**——所以正常情况下，紧跟 `pin()` 就白捡了这道 fence，无需额外手写。

但有一种**可重入**情形：当前线程**已经 pin 过了**（外层调用者已经 `pin`，又调进了 `steal`）。此时内层的 `epoch::pin()` 是个近乎 no-op 的轻量操作，**不会再发 fence**（避免重复）。于是那道本该存在的 `SeqCst fence` 就丢了。代码用 `epoch::is_pinned()` 检测这种情况——「如果我已经戴着手环了，就手动补一道 fence」：

```rust
if epoch::is_pinned() {
    atomic::fence(Ordering::SeqCst);   // 可重入时 pin() 不发 fence，这里补上
}
```

简言之：`pin()` 正常会发 fence；只有「重入 pin」时才漏发，于是用 `is_pinned()` 兜底手补。这是一处把「内存序正确性」与「epoch 可重入优化」优雅结合的工程细节。

同样的模式在两个批量偷取方法里一字不差地重复：

[crossbeam-deque/src/deque.rs:759-768](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L759-L768) —— `steal_batch_with_limit` 的 pin 与重入 fence。

[crossbeam-deque/src/deque.rs:1001-1010](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1001-L1010) —— `steal_batch_with_limit_and_pop` 的 pin 与重入 fence。

两处的注释与 `steal` 完全一致，说明这是贯穿三个偷取方法的统一范式。

#### 4.3.4 代码实践

**实践目标**：亲眼看到「resize（退役）」与「steal（保护）」同时发生时，所有任务仍被正确取走，无丢失无重复。

**操作步骤**：把下面的测试存为 `tests/epoch_resize_race.rs`（需要 `crossbeam-utils` 作 dev-dependency，本 crate 已有）：

```rust
// 示例代码：演示 resize（退役旧 buffer）与多线程 steal（pin 保护）并发
use crossbeam_deque::{Steal, Worker};
use crossbeam_utils::thread;
use std::sync::atomic::{AtomicUsize, Ordering};

#[test]
fn resize_and_steal_concurrent() {
    const N: usize = 200; // > MIN_CAP=64，主线程 push 必然触发多次 resize（→ defer_unchecked）

    let w = Worker::new_lifo();
    for i in 0..N {
        w.push(i); // 长度超过 64 时触发 resize：swap 旧 buffer + defer_unchecked
    }

    let s = w.stealer();
    let consumed = AtomicUsize::new(0);

    thread::scope(|scope| {
        for _ in 0..4 {
            let s = s.clone();
            let consumed = &consumed;
            scope.spawn(move |_| {
                // 每个 steal 内部 epoch::pin()，保护其 load 到的 buffer 指针
                loop {
                    match s.steal() {
                        Steal::Success(_) => {
                            consumed.fetch_add(1, Ordering::Relaxed);
                        }
                        Steal::Empty => break,
                        Steal::Retry => continue,
                    }
                }
            });
        }
    })
    .unwrap();

    assert_eq!(consumed.load(Ordering::Relaxed), N);
}
```

**需要观察的现象**：测试稳定通过，4 个偷取线程合计恰好取走 200 个任务，无丢失无重复、无 panic、无崩溃。

**预期结果**：`consumed == 200`。即使主线程在 push 过程中多次 resize（旧 buffer 被退役登记），stealer 也始终能安全地读到自己加载到的 buffer——这正是 epoch 在背后保证的。如果 epoch 逻辑有 bug（旧 buffer 被过早释放），这段并发测试大概率会崩溃或读出脏数据。

> 实践运行结果取决于本地工具链与并发调度，**待本地验证**；但其设计意图正是验证本节「pin 保护 buffer 读取」的语义。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `epoch::is_pinned()` 为真时，需要手动补一道 `SeqCst fence`？

> **参考答案**：弱内存模型要求 steal 在「读 front」与「读 back」之间有 `SeqCst fence`。正常 `epoch::pin()` 会顺带发这道 fence；但当线程**已经 pin**（可重入）时，内层 `pin()` 是近乎 no-op 的轻量操作，**不再发 fence**。`is_pinned()` 检测到这种重入情形，于是手动补上缺失的 fence，保证内存序正确。

**练习 2**：如果把 `let guard = &epoch::pin();` 这一行整个删掉，`steal` 还能编译通过吗？即便强行编译通过，安全性会出什么问题？

> **参考答案**：不能——`self.inner.buffer.load(Ordering::Acquire, guard)`（[L665](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L665)）的 `guard` 参数是强制的，删掉就没东西可传。即便假设能绕过，安全性也会崩溃：没有 pin，本线程就不参与 epoch 计数，退役的旧 buffer 可能在该线程「load buffer 之后、read slot 之前」被释放，造成 use-after-free。

**练习 3**：`steal_batch_with_limit` 里的 `is_pinned()` 检查（[L764-L766](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L764-L766)）和 `steal` 里的是同一份逻辑吗？为什么要在三个方法里重复？

> **参考答案**：是同一份逻辑（注释都一字不差）。因为 `steal`、`steal_batch_with_limit`、`steal_batch_with_limit_and_pop` 三个偷取入口都独立地 `load front` 后才 `pin`，都需要在重入时补 fence；它们没有共用一个公共前置函数，所以这段「is_pinned 补 fence + pin」的范式被显式重复了三遍。

---

### 4.4 Inner::drop：unprotected() 单线程释放

#### 4.4.1 概念说明

前面两节，退役侧（`resize`）要 `defer_unchecked` 延迟回收，保护侧（`steal`）要 `pin` 自己——都很谨慎。但 `Inner::drop` 却「大刀阔斧」地直接 `dealloc` buffer，连延迟都不延迟。为什么它敢？

答案在签名：`fn drop(&mut self)`。`&mut self` 意味着**独占引用**。而 `Inner` 被包在 `Arc` 里共享（见 u2-l1），能拿到 `&mut Inner` 的前提是 **`Arc` 的强引用计数已归零**——即所有 `Worker` 和 `Stealer` 都已析构。既然没有任何其他句柄存在，此刻**绝无并发访问**，自然不需要 epoch 保护，可以当场释放。

这就是 epoch 提供的第三种用法：`epoch::unprotected()`——一个「假手环」，用于确认无并发的单线程场景。

#### 4.4.2 核心流程

`Inner::drop` 的流程：

1. 用 `get_mut()` 直接拿出 `front`/`back` 的非原子值（独占，无需原子操作）；
2. `self.buffer.load(Ordering::Relaxed, epoch::unprotected())` 拿到最终存活的 buffer；
3. 从 `front` 到 `back` 逐个 `drop_in_place` 残留任务；
4. `buffer.into_owned().into_box().dealloc()` 释放这块最终 buffer 的内存。

全程没有 `pin`、没有 `defer_unchecked`、没有「延迟」——直接当场释放。

#### 4.4.3 源码精读

[crossbeam-deque/src/deque.rs:125-145](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L125-L145) —— `Inner` 的析构。

```rust
impl<T> Drop for Inner<T> {
    fn drop(&mut self) {
        // Load the back index, front index, and buffer.
        let b = *self.back.get_mut();
        let f = *self.front.get_mut();

        unsafe {
            let buffer = self.buffer.load(Ordering::Relaxed, epoch::unprotected()); // [1] 假手环

            // Go through the buffer from front to back and drop all tasks in the queue.
            let mut i = f;
            while i != b {
                buffer.deref().at(i).drop_in_place();                              // [2] drop 残留任务
                i = i.wrapping_add(1);
            }

            // Free the memory allocated by the buffer.
            buffer.into_owned().into_box().dealloc();                              // [3] 直接释放
        }
    }
}
```

逐点说明：

- **[1] `epoch::unprotected()`（[L132](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L132)）**：epoch 的 `Atomic::load` 强制要求传 `Guard`，但这里无需真正进入临界区（没有并发），于是用 `unprotected()` 拿一个**不做任何同步**的假 guard 来满足 API。注意 `Ordering::Relaxed`——因为没有其他线程，连内存序都不需要保证。
- **[2] `drop_in_place`（[L137](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L137)）**：队列里尚未被消费的任务（`front..back` 之间）需要逐个析构，避免 `T` 的资源泄漏。
- **[3] `dealloc()`（[L142](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L142)）**：直接释放**此刻仍安装在 `Inner.buffer` 里的最终存活 buffer**。

**重要辨析：退役 buffer 与最终 buffer 是两条不同的释放路径**——

| buffer 的命运 | 释放路径 | 时机 |
|---|---|---|
| 运行期被 `resize` 换掉的旧 buffer | `resize` 里 `defer_unchecked` → epoch GC | 延迟（epoch 推进两页后） |
| `Inner` 析构时仍安装着的最终 buffer | `Inner::drop` 里 `unprotected()` + `dealloc` | 当场 |

一条 `Inner` 在生命周期内可能换过很多次 buffer（多次 resize），那些退役的旧 buffer 全走 epoch GC；等 `Inner` 真正销毁时，还剩**一个**最终 buffer 挂在 `Atomic` 上，由 `Inner::drop` 当场释放。两条路径分工明确，互不重叠。

#### 4.4.4 代码实践

**实践目标**：对比 `Inner::drop`（绕过 epoch）与 `resize`（走 epoch），理解「为什么 drop 敢、resize 不敢」。

**操作步骤**：

1. 阅读 [src/deque.rs:125-145](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L125-L145)，注意 `&mut self` 这个独占保证。
2. 阅读本讲 4.2 的 `resize`，注意它是 `&self`（共享引用），且发生在并发可能存在的运行期。
3. 在注释里写下：`Inner::drop` 拿到 `&mut self` 的前提条件是什么？这个前提为什么排除了并发？

**需要观察的现象**：你应该意识到 `drop` 的「独占性」是编译期 + 运行期（`Arc` 计数）共同保证的。

**预期结果**：得出结论——`Inner::drop` 时所有 `Worker`/`Stealer` 已析构（`Arc` 强引用归零），无任何并发访问，故可用 `unprotected()` 跳过 epoch、当场释放；而 `resize` 是运行期 `&self` 操作，并发 stealer 可能正持有旧指针，故必须走 `defer_unchecked` 延迟回收。

#### 4.4.5 小练习与答案

**练习 1**：`Inner::drop` 为什么能用 `epoch::unprotected()` 而不用 `epoch::pin()`？

> **参考答案**：`drop(&mut self)` 的独占引用意味着 `Arc` 强引用计数已归零，所有 `Worker`/`Stealer` 都已析构，此刻没有任何并发访问。`pin` 的作用是卡住 epoch 以保护并发读，既然没有并发，就不需要 pin——`unprotected()` 提供一个不做同步的「假手环」来满足 `Atomic::load` 的 API 签名即可。

**练习 2**：运行期被 `resize` 退役的旧 buffer，和 `Inner::drop` 里 `dealloc` 的 buffer，是同一块吗？

> **参考答案**：通常不是。运行期退役的旧 buffer 走 `defer_unchecked` → epoch GC 延迟释放；`Inner::drop` 里 `dealloc` 的是**析构时刻仍安装在 `Inner.buffer` 里的最终存活 buffer**。一条 `Inner` 在生命周期里可能换过多次 buffer，那些旧的全走 GC，只有最后挂着的那个走 `drop`。两者是互补的两条释放路径。

**练习 3**：`Inner::drop` 里加载 buffer 用的是 `Ordering::Relaxed`，而 `Stealer::steal` 里用的是 `Ordering::Acquire`。为什么 drop 可以用最弱的 `Relaxed`？

> **参考答案**：`Relaxed` 不提供跨线程同步，但 `drop` 是单线程独占场景（无其他线程在写或读 `buffer`），不需要任何 happens-before 保证，`Relaxed` 足矣。`steal` 是多线程并发场景，必须用 `Acquire` 与 owner 的 `Release`（`swap` 时）配对，才能保证读到完整的 buffer 内容。

---

## 5. 综合实践

**任务**：把本讲三处 epoch 用法（退役 / 保护 / 绕过）串成一个完整的「buffer 一生」推理，并用一个并发测试佐证整体正确性。

### 5.1 推理部分（纸面）

请按顺序描述一个 buffer 从出生到死亡的完整过程，每一步标注源码行号：

1. **出生**：`Worker::new_fifo`/`new_lifo` 里 `Buffer::alloc(MIN_CAP)`（[L226](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L226)、[L254](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L254)）。
2. **服役**：被 `push`/`pop`/`steal` 读写，stealer 每次 `steal` 都 `pin` 保护它（[L654](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L654)）。
3. **退役**：`resize` 把它 `swap` 掉并 `defer_unchecked` 登记（[L309-L315](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L309-L315)），epoch GC 在安全后释放它。
4. **（最终 buffer 的）死亡**：`Inner::drop` 用 `unprotected()` 当场 `dealloc`（[L132-L142](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L132-L142)）。

### 5.2 可运行部分

把 4.3.4 的测试 `resize_and_steal_race` 跑通（`cargo test --test epoch_resize_race`）。这个测试同时触发：

- 主线程多次 `resize`（**退役侧**：旧 buffer 被 `defer_unchecked`）；
- 4 个偷取线程并发 `steal`（**保护侧**：每个 `steal` 内部 `epoch::pin`）；
- 测试结束时所有句柄析构（**绕过侧**：`Inner::drop` 用 `unprotected()` 释放最终 buffer）。

**预期结果**：测试稳定通过，`consumed == 200`，无崩溃。若 epoch 三处用法中任何一处出错，这段并发测试都极易触发 use-after-free 或任务丢失。

> 并发测试的稳定性与本地调度相关，**待本地验证**；可多跑几次（如 `cargo test -- --test-threads=4` 反复运行）观察是否稳定。

## 6. 本讲小结

- crossbeam-deque 用 **crossbeam-epoch（EBR）** 解决无锁队列换 buffer 后的「悬挂指针」问题：旧 buffer 不能当场 free，因为 stealer 可能正攥着旧指针。
- **退役侧（`Worker::resize`）**：`pin` → `swap` 换指针拿回旧 buffer → `defer_unchecked` 登记延迟回收 → 大 buffer 时 `flush` 尽早回收（阈值 `FLUSH_THRESHOLD_BYTES = 1024` 字节）。
- **保护侧（`Stealer::steal` / `steal_batch*`）**：`epoch::pin()` 让本线程参与 epoch 计数，使其加载到的 buffer 指针在 guard 存活期间不被释放；`epoch::is_pinned()` 在可重入时补发缺失的 `SeqCst fence`。
- **绕过侧（`Inner::drop`）**：`&mut self` 的独占保证（`Arc` 计数归零）意味着无并发，故用 `epoch::unprotected()` 跳过 epoch、当场 `dealloc` 最终存活 buffer。
- 退役的旧 buffer 走 epoch GC（延迟），最终存活 buffer 走 `Inner::drop`（当场），两条路径互补、不重叠。
- 本 crate 只用到 epoch 的 5 个 API：`pin` / `defer_unchecked` / `flush` / `unprotected` / `is_pinned`，分别对应进临界区、登记退役、尽早回收、单线程绕过、重入判断。

## 7. 下一步学习建议

- **横向对照 Injector 的回收机制**： Injector 完全不用 epoch，而是用 `READ`/`DESTROY` 状态位做协作式 `Block::destroy`（见 u3-l3）。对比「epoch 延迟回收」与「状态位协作销毁」两种无锁回收思路，能加深对 EBR 取舍的理解。
- **深入 crossbeam-epoch 本身**：本讲把 epoch 当黑盒用，只讲了 5 个 API。若想理解「两页 epoch 安全裕度」「全局 epoch 如何推进」「垃圾袋如何在 participant 间转移」，建议直接读 `../crossbeam-epoch/src/` 的 `collector.rs` / `guard.rs` / `atomic.rs`。
- **衔接 u4-l3（ThreadSanitizer 兼容）**：本讲提到的 `SeqCst fence` 在 tsan 模式下会被改写成 `Release store`（tsan 不理解 fence）。下一讲会解释 build.rs 如何探测 `sanitize=thread` 并点亮 `crossbeam_sanitize_thread` cfg，从而影响这些 fence 的代码生成。
- **动手实验（可选）**：用 `cargo +nightly miri test` 跑本讲的并发测试，Miri 对 use-after-free 极为敏感，是验证 epoch 正确性的有力工具（运行较慢，建议缩小 `N`）。
