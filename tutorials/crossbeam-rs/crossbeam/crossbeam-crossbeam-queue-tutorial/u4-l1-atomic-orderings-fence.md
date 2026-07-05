# 原子内存序与 fence 的运用

## 1. 本讲目标

本讲是「专家层」的第一篇，专门回答一个在 u2-l2、u3-l2 里被刻意推迟的问题：

> `ArrayQueue` 与 `SegQueue` 里每一个原子操作的 `Ordering` 到底为什么这样选？能不能换成更弱的序？

学完后你应该能够：

1. 看到任一处 `load` / `store` / `compare_exchange` / `fetch_or` / `fence`，能立刻说出它选这个 `Ordering` 的原因，以及换成更弱的序会引入什么竞态。
2. 理解 **Acquire / Release 的「发布—订阅」配对**如何同时保障「数据可见性」与「数据有序性」。
3. 理解 **SeqCst** 在哪些位置不可替代（CAS 抢占指针、`is_empty`/`is_full`/`len` 的一致性快照），以及 **Relaxed** 为什么在另一些位置足够安全。
4. 理解 `compare_exchange` 的「成功序」与「失败序」是两件事，以及 `SegQueue` 为何要把失败序写成 `Acquire` 而不是 `Relaxed`。
5. 理解三个 `atomic::fence(Ordering::SeqCst)` 在「满 / 空二次确认」中到底拦住了什么。

本讲**不重述算法流程**（push/pop 主链路见 u2-l2、u3-l2，stamp/状态位编码见 u2-l1、u3-l1），只聚焦「内存序的正确性论证」。

## 2. 前置知识

阅读本讲前，请确保你已经掌握以下概念（不会的话先看 u2-l2 与 u3-l2）：

- **原子操作与内存序（Ordering）**：Rust 的原子类型对每次读写都要指定一个 `Ordering`。`Ordering` 不影响「这一次读写是否原子」，它影响的是「这一次读写**周围的普通内存访问**能不能被 CPU / 编译器重排，以及多线程之间何时能看到彼此的写」。
- **数据竞争的根源——重排**：在你写出的源码顺序之外，CPU（乱序执行、store buffer）和编译器都会把互相独立的指令重排。无锁算法的正确性依赖于用恰当的 `Ordering` 在关键点上「钉住」顺序。
- **happens-before**：如果一个操作 A 「happens-before」 操作 B，那么 A 之前的所有内存写，对 B 之后的读都可见。`Ordering` 的本质就是「制造 happens-before 关系」。
- **stamp / 状态位充当状态机**：`ArrayQueue` 用 `slot.stamp` 当状态机（`stamp==tail` 可写、`stamp==head+1` 可读）；`SegQueue` 用 `slot.state` 的 `WRITE/READ/DESTROY` 位当状态机。本讲要论证的核心，就是**「状态的发布」与「数据的发布」如何用同一对 Acquire/Release 绑定在一起**。

五种 `Ordering` 的直觉（从弱到强）：

| Ordering | 直觉 | 代价 |
|---|---|---|
| `Relaxed` | 只保证这一次读写本身原子，**不约束任何周围访问的顺序**，也不建立跨线程 happens-before | 最低 |
| `Release` | **「我写完了」**：本次 store 之前的所有读写，对随后做 `Acquire` 读到这个值的线程可见 | 低 |
| `Acquire` | **「我要开始读了」**：本次 load 之后的所有读写，不会被重排到这次 load 之前；与上一个 `Release` store 配对 | 低 |
| `AcqRel` | 读改写（`fetch_or` 等）= `Acquire` + `Release` 合体 | 中 |
| `SeqCst` | 在 Acquire/Release 之上，**额外提供一个所有 `SeqCst` 操作的全局单一全序**（single total order） | 最高 |

一个最关键、也最容易被初学者忽略的事实：

> **`Release` 与 `Acquire` 必须成对、且作用于「同一个原子变量」上才能建立 happens-before。** 生产者写完数据用 `Release` 发布一个标记，消费者用 `Acquire` 读到这个标记之后，才被允许去读那份数据。

本讲几乎每一处 `Ordering` 的选择，都可以还原成上面这句话的某个变体。

## 3. 本讲源码地图

本讲只精读两个文件（算法本身已在 u2/u3 讲过，这里只盯内存序）：

| 文件 | 作用 | 本讲关注 |
|---|---|---|
| [src/array_queue.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs) | 有界 MPMC 队列实现 | `push_or_else` / `pop` / `force_push` 里全部 `Ordering`、两个 `fence(SeqCst)`、`is_empty`/`is_full`/`len` 的快照序 |
| [src/seg_queue.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs) | 无界分段 MPMC 队列实现 | `push` / `pop` / `Block::destroy` / `wait_write` / `wait_next` 里全部 `Ordering`、一个 `fence(SeqCst)` |

测试侧用到一个文件作为「如何用实验抓住错误内存序」的依据：

| 文件 | 作用 |
|---|---|
| [tests/array_queue.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/tests/array_queue.rs) | 含 `spsc` / `mpmc` / `drops` / `linearizable` 等并发测试，并提供 `cfg!(miri)` 规模缩放 |

---

下面先把两个文件里**所有**原子操作及其 `Ordering` 汇总成两张表。这两张表就是本讲的「地图」，后续四个最小模块都会反复回指它们。建议你做综合实践时直接对照这两张表逐行论证。

### 表 A：`ArrayQueue` 全部原子操作与内存序

| 行号 | 操作 | Ordering | 作用 / 配对 |
|---|---|---|---|
| [L132](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L132) | `tail.load`（循环前） | Relaxed | 取一个起点值，无所谓可见性 |
| [L152](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L152) | `slot.stamp.load` | Acquire | 与 L168/L293 的 Release 配对 |
| [L157-L162](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L157-L162) | `tail.compare_exchange_weak` | 成功 SeqCst / 失败 Relaxed | 抢占 tail |
| [L168](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L168) | `slot.stamp.store(tail+1)` | Release | 把「值已写好」发布给消费者 |
| [L177](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L177) | `atomic::fence` | SeqCst | 「疑似满」的二次确认 |
| [L205](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L205) | `head.load`（push 闭包内） | Relaxed | 满判定读 head，序已由 L177 fence 保证 |
| [L281-L283](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L281-L283) | `head.compare_exchange_weak` | 成功 SeqCst / 失败 Relaxed | `force_push` 抢占覆盖权 |
| [L287](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L287) | `tail.store` | SeqCst | `force_push` 推进 tail |
| [L293](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L293) | `slot.stamp.store(tail+1)` | Release | 发布覆盖后的新值 |
| [L330](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L330) | `slot.stamp.load` | Acquire | 与 L168/L293 Release 配对 |
| [L345-L349](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L345-L349) | `head.compare_exchange_weak` | 成功 SeqCst / 失败 Relaxed | 抢占 head |
| [L354-L355](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L354-L355) | `slot.stamp.store(head+one_lap)` | Release | 把槽归还给下一圈的生产者 |
| [L364](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L364) | `atomic::fence` | SeqCst | 「疑似空」的二次确认 |
| [L459-L460](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L459-L460) | `head/tail.load` | SeqCst | `is_empty` 一致快照 |
| [L484-L485](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L484-L485) | `tail/head.load` | SeqCst | `is_full` 一致快照 |
| [L513-L517](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L513-L517) | `tail/head.load` | SeqCst | `len` 的 seqlock 风格快照 |

### 表 B：`SegQueue` 全部原子操作与内存序

| 行号 | 操作 | Ordering | 作用 / 配对 |
|---|---|---|---|
| [L47](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L47) | `state.load`（`wait_write`） | Acquire | 等 WRITE 位，与 L281 Release 配对 |
| [L96](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L96) | `next.load`（`wait_next`） | Acquire | 等 next 指针，与 L275 Release 配对 |
| [L112](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L112) | `state.load`（`destroy`） | Acquire | 读 READ 位 |
| [L113](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L113) | `state.fetch_or(DESTROY)` | AcqRel | 与 L436 `fetch_or(READ, AcqRel)` 协调交接 |
| [L216](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L216) | `tail.index.load` | Acquire | 进入循环 |
| [L217](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L217) | `tail.block.load` | Acquire | 读当前块，与 L273 Release 配对 |
| [L242-L246](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L242-L246) | `tail.block.compare_exchange` | 成功 Release / 失败 Relaxed | 选举首个块 |
| [L248](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L248) | `head.block.store` | Release | 把首块镜像给消费者 |
| [L261-L266](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L261-L266) | `tail.index.compare_exchange_weak` | 成功 SeqCst / 失败 Acquire | 推进 tail；失败需 Acquire 看到新块 |
| [L273-L275](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L273-L275) | `tail.block/index.store`、`block.next.store` | Release | 安装并串联下一块 |
| [L281](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L281) | `state.fetch_or(WRITE)` | Release | 发布值，与 L47 Acquire 配对 |
| [L366-L367](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L366-L367) | `head.index/block.load` | Acquire | 进入循环 |
| [L384](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L384) | `atomic::fence` | SeqCst | 「疑似空」的二次确认 |
| [L408-L413](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L408-L413) | `head.index.compare_exchange_weak` | 成功 SeqCst / 失败 Acquire | 推进 head；失败需 Acquire 看到新块 |
| [L419](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L419) | `next.next.load` | Relaxed | 仅判 HAS_NEXT 元数据，无数据依赖 |
| [L423-L424](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L423-L424) | `head.block/index.store` | Release | 推进 head 块给其他消费者 |
| [L436](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L436) | `state.fetch_or(READ)` | AcqRel | 标记已读，与 L113 AcqRel 协调 |
| [L542-L543](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L542-L543) | `head/tail.index.load` | SeqCst | `is_empty` 一致快照 |
| [L566-L570](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L566-L570) | `tail/head.index.load` | SeqCst | `len` 的 seqlock 快照 |

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块，正好对应上面两张表里的四类 `Ordering`：

- 4.1 Acquire / Release 的「发布—订阅」配对
- 4.2 SeqCst 与 Relaxed 的取舍
- 4.3 `compare_exchange` 的成功序与失败序
- 4.4 `atomic::fence(SeqCst)` 的二次确认用途

### 4.1 Acquire/Release 的发布—订阅配对

#### 4.1.1 概念说明

无锁队列有一个根本难点：**生产者写值、消费者读值，它们访问的是同一块 `UnsafeCell<MaybeUninit<T>>`，而这块内存在「值是否已初始化」上是共享可变状态。** 如果没有任何同步，消费者可能读到生产者**还没写完**的半个值，或者两次读到同一个值。

crossbeam 的解法是**把「数据的发布」和「状态的发布」绑在同一个原子变量的一对 Release/Acquire 上**：

- 生产者先写值（普通非原子写），再用一次 `Release` 写把一个「标记」（`ArrayQueue` 的 `stamp`、`SegQueue` 的 `WRITE` 位）置成「就绪」。
- 消费者先用 `Acquire` 读这个标记；只有读到「就绪」，才被允许去读那块值内存。

依据 happens-before 规则：**当消费者的 `Acquire` 读到了生产者那次 `Release` 写的值，生产者 `Release` 之前的所有写（包括值本身）都对消费者可见。** 于是消费者读到的值一定是完整的。这就是「发布—订阅」配对。

> 关键直觉：**Release/Acquire 不是为了让那个原子变量本身可见（原子变量本来就最终一致），而是为了把它「附带护送」的那块普通内存（值）变得可见且有序。**

#### 4.1.2 核心流程

`ArrayQueue` 的发布—订阅配对（围绕 `slot.stamp`）：

```
生产者 push_or_else：
  CAS 占到 tail            // 抢到槽
  slot.value.write(value)  // 普通写：写入值（此刻消费者绝不能读）
  slot.stamp.store(tail+1, Release)   // 发布：「这个槽的值写好了，stamp 也变成了 tail+1」

消费者 pop：
  stamp = slot.stamp.load(Acquire)    // 订阅：读到 tail+1 才证明值已就绪
  if head + 1 == stamp {              // 这个槽可读
      value = slot.value.read()       // 此时读到的值必完整
  }
```

`SegQueue` 的发布—订阅配对（围绕 `slot.state` 的 `WRITE` 位）：

```
生产者 push：
  CAS 占到 offset
  slot.value.write(value)             // 普通写
  slot.state.fetch_or(WRITE, Release) // 发布：置 WRITE 位

消费者 pop：
  slot.wait_write()                   // 内部：state.load(Acquire) 直到 WRITE 位为 1
  value = slot.value.read()           // 此时读到的值必完整
```

两套配对结构完全同构：**「写值 → Release 标记」对「Acquire 标记 → 读值」**。

#### 4.1.3 源码精读

`ArrayQueue` 中，发布端在 `push_or_else` 写完值后立刻 `Release` 推进 stamp：

```rust
// src/array_queue.rs  push_or_else 内，CAS 成功分支
unsafe {
    slot.value.get().write(MaybeUninit::new(value));   // L166：先写值
}
slot.stamp.store(tail + 1, Ordering::Release);         // L168：再 Release 发布
```

[src/array_queue.rs:164-L168](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L164-L168) — 注意顺序：**先 `write`，后 `Release`**。`Release` 语义保证 L166 的写不会被重排到 L168 之后；消费者只有 `Acquire` 读到 L168 写入的 `tail+1`，才能「解锁」对 L166 写入值的读取。

订阅端在 `pop` 里用 `Acquire` 读 stamp（与上面 Release 配对），再据此决定能否 `read`：

```rust
// src/array_queue.rs  pop 内
let stamp = slot.stamp.load(Ordering::Acquire);   // L330：Acquire 订阅
if head + 1 == stamp {                            // L333：stamp 已被推到 head+1 ⇒ 值就绪
    ...
    let msg = unsafe { slot.value.get().read().assume_init() }; // L353：可安全读
```

[src/array_queue.rs:330-L333](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L330-L333)。`force_push` 覆盖最旧元素时同样遵守这套配对：先 `replace` 替换值，再 `Release` 推进 stamp，见 [src/array_queue.rs:290-L293](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L290-L293)。

`SegQueue` 的发布端是 `fetch_or(WRITE, Release)`：

```rust
// src/seg_queue.rs  push 内，CAS 成功分支
let slot = (*block).slots.get_unchecked(offset);
slot.value.get().write(MaybeUninit::new(value));     // L280：先写值
slot.state.fetch_or(WRITE, Ordering::Release);       // L281：再 Release 置 WRITE 位
```

[src/seg_queue.rs:279-L281](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L279-L281)。订阅端是 `wait_write`：

```rust
fn wait_write(&self) {
    let backoff = Backoff::new();
    while self.state.load(Ordering::Acquire) & WRITE == 0 {  // L47：Acquire 等 WRITE 位
        backoff.snooze();
    }
}
```

[src/seg_queue.rs:45-L50](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L45-L50)。`pop` 在 `read` 之前必须先 `wait_write`，正是为了保证「读到 `WRITE` 位 ⇒ 值已就绪」，见 [src/seg_queue.rs:429-L430](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L429-L430)。

`SegQueue` 还有一组「块指针」的发布—订阅配对：生产者用 `Release` 安装下一块的 `next` 指针，消费者用 `wait_next` 里的 `Acquire` 等到非空指针。发布端 [src/seg_queue.rs:275](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L275)，订阅端：

```rust
fn wait_next(&self) -> *mut Self {
    let backoff = Backoff::new();
    loop {
        let next = self.next.load(Ordering::Acquire);   // L96
        if !next.is_null() { return next; }
        backoff.snooze();
    }
}
```

[src/seg_queue.rs:93-L102](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L93-L102)。这里的 `Acquire` 保证消费者拿到 `next` 指针后，对新块里所有字段（slots、state）的读，都能看到生产者在 `Release` 之前完成的初始化。

#### 4.1.4 代码实践

**实践目标**：亲手验证「发布—订阅配对缺一不可」。

**操作步骤**（源码阅读 + 思想实验，不修改上游源码）：

1. 打开 `src/seg_queue.rs`，找到 L280–L281（写值 + `fetch_or(WRITE, Release)`）和 L47（`wait_write` 的 `Acquire`）。
2. 假设把 L281 的 `Release` 改成 `Relaxed`（其余不变），在纸上推演下面这个交错：
   - 线程 P：执行 `write(value)`，**正准备**执行 `fetch_or(WRITE, Relaxed)`。
   - 线程 C：恰好已通过 `wait_write`（在弱内存模型下，`WRITE` 位可能因前一圈的残留或重排被提前看到？），进入 `read()`。
3. 推演：P 的 `write` 与 C 的 `read` 之间没有任何 happens-before，C 可能读到未初始化内存。
4. 在本地的 fork 里实际把 L281 改成 `Relaxed`，运行 `cargo test -p crossbeam-queue --test seg_queue`，并尝试 `MIRIFLAGS="-Zmiri-many-seeds" cargo +nightly miri test seg_queue`。

**需要观察的现象**：在弱内存模型（miri 多种子）下，测试可能偶发读到垃圾值或触发 UB；在强内存模型（x86）下很可能「测不出问题」——这正是无锁内存序 bug 的危险之处。

**预期结果**：`Release` 不可弱化为 `Relaxed`。若 miri 不便运行，请标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：`ArrayQueue` 的 `pop` 在 L353 `read()` 之后，L354–L355 用 `Release` 把 stamp 置成 `head+one_lap`。这次 `Release` 是在和谁配对？

**答案**：和**下一圈**的生产者配对。消费者把槽归还后，stamp 变成 `head+one_lap`；当 tail 绕一圈回到这个槽时，生产者的 `Acquire`（L152）会读到这个 stamp 并判定 `tail == stamp`，从而知道这个槽可以安全复用。L355 的 `Release` 保证了「消费者对这块内存的读」happens-before「下一圈生产者对它的写」。

**练习 2**：`SegQueue` 的 `wait_write` 用的是「忙等 + `Acquire`」而不是「让出 CPU」。如果只把 `Acquire` 改成 `Relaxed`，但保留忙等循环，会出现什么问题？

**答案**：循环仍会终止（最终能读到 `WRITE==1` 的位），但 `Relaxed` 不建立 happens-before，循环退出后 C 立刻 `read()`，可能读到 P 在 `Release`（被你改弱了）之前的、尚未完成的写。即「等到了标志，却没等到数据」。

---

### 4.2 SeqCst 与 Relaxed 的取舍

#### 4.2.1 概念说明

读者常有的两个极端误区：

- 误区 A：「无锁算法难，我全用 `SeqCst` 最安全。」——能跑，但在每个 CAS 和每次快照上都付全序的代价，吞吐被拖垮。
- 误区 B：「全用 `Relaxed`，反正变量是原子的。」——立刻数据竞争，读到半成品值、丢数据、重复释放。

crossbeam 的取舍原则可以归纳成三条：

1. **凡是要「发布数据 / 订阅数据」的，用 Release / Acquire**（已在 4.1 论证）。
2. **凡是要在多个线程间对「队列是否满 / 是否空 / 元素个数」做一致性裁决的，用 `SeqCst`**——因为这类判定需要所有线程对 head/tail 看到一个**单一全序**，否则会出现「A 线程判满、B 线程同时判不满」的自相矛盾。
3. **凡是「只取一个值、随后还会被 CAS 或 Acquire 重新校验」的初始/重读，用 `Relaxed`**——可见性由后续更强的操作兜底，这里不必再花钱。

> 核心直觉：**`SeqCst` 的成本换来的是「跨线程的全局一致性裁决」；凡是属于这种裁决的位置（CAS 推进游标、`is_empty`/`is_full`/`len` 快照），就必须用 `SeqCst`。`Relaxed` 只能用在「我读这个值只为了给后面更强的操作当输入」的地方。**

#### 4.2.2 核心流程

**该用 SeqCst 的两类位置**：

1. **推进 head/tail 游标的 CAS 的「成功序」**（表 A 的 L157、L281、L345；表 B 的 L261、L408）。CAS 成功意味着「我修改了队列的结构」，这个修改必须进入全局全序，否则满/空判定会错乱。
2. **`is_empty` / `is_full` / `len` 这类「双指针快照」**（表 A 的 L459–460、L484–485、L513–517；表 B 的 L542–543、L566–570）。它们要同时读 head 和 tail 并比较，需要 `SeqCst` 来让两次读尽量落在同一个全局时刻。

**该用 Relaxed 的位置**：

- 循环开始前的「取起点值」（表 A L132、L320；表 B 通过 L216/L366 的 Acquire 进入，但 ArrayQueue 用 Relaxed 即可）。
- `fence(SeqCst)` 已经钉住顺序之后的「二次重读」（表 A L180、L205、L365；表 B L385）——序由 fence 保证，重读本身用 Relaxed 即可。

#### 4.2.3 源码精读

**SeqCst 在 CAS 成功序上**。`push_or_else` 推进 tail：

```rust
match self.tail.compare_exchange_weak(
    tail,
    new_tail,
    Ordering::SeqCst,      // L160：成功序——修改队列结构，进全局全序
    Ordering::Relaxed,     // L161：失败序（4.3 单独讲）
) { ... }
```

[src/array_queue.rs:157-L162](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L157-L162)。`pop` 的 head CAS 同理用 `SeqCst` 成功序，见 [src/array_queue.rs:345-L349](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L345-L349)。

**SeqCst 在双指针快照上**。`is_empty` 同时读 head 与 tail：

```rust
pub fn is_empty(&self) -> bool {
    let head = self.head.load(Ordering::SeqCst);   // L459
    let tail = self.tail.load(Ordering::SeqCst);   // L460
    tail == head
}
```

[src/array_queue.rs:458-L468](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L458-L468)。源码注释点明了为什么这里宁可「偏向乐观」：如果 head 刚好在 load tail 之前变了，那只说明「队列曾经非空」，返回 `false` 是安全的。`SeqCst` 让这两次读在全序里相邻，避免读到「自相矛盾的 (head, tail)」。`is_full` 见 [src/array_queue.rs:483-L492](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L483-L492)，`len` 的 seqlock 三连读见 [src/array_queue.rs:513-L517](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L513-L517)。

**Relaxed 用在「起点值」与「fence 后重读」上**。`push_or_else` 循环前：

```rust
let mut tail = self.tail.load(Ordering::Relaxed);   // L132：起点值
```

[src/array_queue.rs:132](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L132)。这个值随后会被 L152 的 `Acquire`（读 stamp）和 L157 的 `SeqCst` CAS 重新校验，本身不需要可见性保证，`Relaxed` 足够且最快。`pop` 中的对称起点见 [src/array_queue.rs:320](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L320)。

`SegQueue` `pop` 里判定空时读 tail：

```rust
atomic::fence(Ordering::SeqCst);                 // L384：先钉住顺序（4.4 讲）
let tail = self.tail.index.load(Ordering::Relaxed); // L385：fence 已保证序，这里 Relaxed 即可
if head >> SHIFT == tail >> SHIFT { return None; }  // L388
```

[src/seg_queue.rs:384-L388](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L384-L388)。注意 L385 能用 `Relaxed`，正是因为有 L384 的 `SeqCst` fence 兜底——这是 4.4 节的核心。

#### 4.2.4 代码实践

**实践目标**：把表 A 中所有 `SeqCst` 与 `Relaxed` 的位置分类，并尝试弱化。

**操作步骤**：

1. 对照表 A，把 `ArrayQueue` 里的 `SeqCst` 分成两类：CAS 成功序（L160、L282、L347）与快照序（L459、L460、L484、L485、L513、L514、L517）。
2. 在本地 fork 里，**只**把 `is_empty` 的两个 `SeqCst`（L459–460）降级为 `Acquire`/`Relaxed`，编译并运行：
   ```
   cargo test -p crossbeam-queue --test array_queue -- --release
   cargo +nightly miri test -p crossbeam-queue --test array_queue
   ```
3. 再把 `push_or_else` 里 CAS 的成功序 L160 从 `SeqCst` 降为 `AcqRel`，重跑上面的命令。

**需要观察的现象**：x86 上很可能「测不出问题」（强内存模型掩盖了 bug）；miri 多种子或 Loom 下才有机会暴露。重点不是「能不能复现」，而是**论证为什么 x86 测不出不代表正确**。

**预期结果**：`is_empty`/`len` 的双指针比较需要全局全序，弱化后理论上可读到自相矛盾的快照。**若本地无法稳定复现，请明确标注「待本地验证」**，并写明「x86 强模型下不易复现」。

#### 4.2.5 小练习与答案

**练习 1**：`ArrayQueue::len`（L513–517）读了三次 `SeqCst`。为什么这里不能用一次 `Acquire` + 一次 `Relaxed`？

**答案**：`len` 要拿一对**真实共存过**的 (tail, head) 做环形算术（见 u2-l3）。它用「读 tail → 读 head → 复查 tail」的 seqlock 写法，三次都必须是 `SeqCst`，这样三次读才能落在同一个全局全序里、从而判断 tail 是否在中途变了。换成 `Acquire`/`Relaxed` 会丢失全局全序，复查本身不再可信。

**练习 2**：`SegQueue::push` 里推进 tail.index 的 CAS 成功序是 `SeqCst`（L263），但推进 tail.block 的 CAS 成功序却只是 `Release`（L244）。为什么前者要 `SeqCst` 而后者 `Release` 就够？

**答案**：`tail.block` 只承担「发布新块给其他线程」（4.1 的发布—订阅），`Release` 足矣；而 `tail.index` 的推进直接参与「满 / 空 / 元素数」的全局一致性裁决（与 head.index 的 `SeqCst` CAS、`is_empty`/`len` 的 `SeqCst` 快照共同决定队列状态），必须进全局全序，所以用 `SeqCst`。

---

### 4.3 compare_exchange 的成功序与失败序

#### 4.3.1 概念说明

`compare_exchange` / `compare_exchange_weak` 接收**两个** `Ordering` 参数：`(success_ordering, failure_ordering)`。初学者经常只关注成功序，忽略失败序。要记住：

- **成功序**：CAS 成功（即当前值 == 期望值，并已被写成新值）时生效的内存序。
- **失败序**：CAS 失败（当前值 != 期望值）时生效的内存序。**失败时变量没有被修改**，所以失败序只可能是 `Acquire` 或更弱（不能是 `Release`/`AcqRel`，因为没有写发生）。

两条经验法则：

1. **失败序通常可以比成功序弱**。因为失败只是「我拿到了别人更新后的值、要重试」，多数情况下这次失败不需要建立任何 happens-before。
2. **但当「失败」本身就意味着「我需要重新看到别人刚发布的关联数据」时，失败序必须强到能读到那些数据**。`SegQueue` 正是这种情况——它的 CAS 失败序用的是 `Acquire` 而非 `Relaxed`。

#### 4.3.2 核心流程

`ArrayQueue` 的所有 CAS 失败序都是 `Relaxed`：

```
compare_exchange_weak(expected, new, SeqCst /*成功*/, Relaxed /*失败*/)
```

理由：CAS 失败后，我们只把返回的新值塞回局部变量 `tail`/`head`，下一轮循环会重新 `Acquire` 读 stamp、重新 `SeqCst` CAS。失败这一下不需要任何可见性。

`SegQueue` 的 tail.index / head.index CAS 失败序是 `Acquire`：

```
compare_exchange_weak(expected, new, SeqCst /*成功*/, Acquire /*失败*/)
```

理由：失败意味着「别的生产者/消费者推进了 index，**可能还顺带安装了一个新块**」。CAS 返回的新 index 只告诉我们游标变了，但**当前块的裸指针缓存可能已经过期**。于是失败分支必须 `Acquire` 重新读 `block` 指针（与安装块的 `Release` store 配对），否则会用到陈旧甚至悬空的块指针。

#### 4.3.3 源码精读

`ArrayQueue` 失败序 `Relaxed` 的典型例子——`push_or_else` 抢 tail：

```rust
match self.tail.compare_exchange_weak(
    tail,
    new_tail,
    Ordering::SeqCst,    // 成功
    Ordering::Relaxed,   // L161：失败——只回收新 tail 值，重试即可
) {
    Ok(_) => { ... slot.stamp.store(tail + 1, Ordering::Release); ... }
    Err(t) => { tail = t; backoff.spin(); }   // L172：拿新值重试
}
```

[src/array_queue.rs:157-L175](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L157-L175)。`force_push` 抢 head（[L281-L283](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L281-L283)）、`pop` 抢 head（[L345-L349](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L345-L349)）都是同一模式：成功 `SeqCst`、失败 `Relaxed`。`ArrayQueue` 的容量固定、槽位不会动态新增，所以失败时无需重新加载任何关联结构，`Relaxed` 安全。

`SegQueue` 失败序 `Acquire` 的关键例子——`push` 抢 tail.index：

```rust
match self.tail.index.compare_exchange_weak(
    tail,
    new_tail,
    Ordering::SeqCst,    // 成功
    Ordering::Acquire,   // L266：失败——必须重新看到可能的新块
) {
    Ok(_) => unsafe { ... 安装下一块、写值、fetch_or(WRITE, Release) ... },
    Err(t) => {
        tail = t;
        block = self.tail.block.load(Ordering::Acquire); // L287：重读块指针！
        backoff.spin();
    }
}
```

[src/seg_queue.rs:261-L289](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L261-L289)。注意 L287 紧跟着一次 `Acquire` 重读 `tail.block`——这就是失败序必须是 `Acquire` 的直接证据：CAS 失败可能是因为别的线程在 L273 用 `Release` 安装了新块，我们必须用 `Acquire` 才能看到那个新块。`pop` 抢 head.index 的失败分支同理重读 `head.block`，见 [src/seg_queue.rs:408-L445](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L408-L445)（成功 `SeqCst`、失败 `Acquire`，L444 重读块）。

还有一个「成功序不是 SeqCst」的特例：首次块选举的 `tail.block.compare_exchange(block, new, Release, Relaxed)`，见 [src/seg_queue.rs:242-L246](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L242-L246)。这里成功序用 `Release`（不是 `SeqCst`），因为这一步**只负责发布首块**（与 L252–253 / L217 的 `Acquire` 读块配对），不参与满/空裁决；失败序 `Relaxed`，因为失败者只是「选举输了」，会丢弃自建块并重读。

#### 4.3.4 代码实践

**实践目标**：体会「失败序为什么有时必须是 Acquire」。

**操作步骤**：

1. 在 `src/seg_queue.rs` 的 L261–266，把 `push` 里 tail.index CAS 的**失败序**从 `Acquire` 改成 `Relaxed`（保持成功序 `SeqCst` 不变，**保留** L287 的 `Acquire` 重读 block）。
2. 编译，运行 `cargo test -p crossbeam-queue --test seg_queue` 与 miri。
3. 再做对照：恢复失败序为 `Acquire`，但把 L287 的 `block.load(Acquire)` 改成 `Relaxed`。

**需要观察的现象**：观察多生产者高并发下是否出现「使用了已释放/未安装的块」导致的崩溃、读到错值或泄漏。重点推演：线程 P 的 CAS 失败了，意味着另一个 P' 刚把 index 从「块 0 末尾」推进到「块 1 起点」并 `Release` 安装了块 1；如果 P 的失败序是 `Relaxed`，P 看不到块 1 的安装，会拿着陈旧的 `block`（块 0）继续往一个已被读空的块里写——竞态成立。

**预期结果**：失败序 `Acquire` 不可弱化为 `Relaxed`。x86 上因 TSO 模型可能仍然测不出，请标注「待本地验证」，并写明 miri/Loom 是更可靠的验证手段。

#### 4.3.5 小练习与答案

**练习 1**：`compare_exchange` 失败时，`failure_ordering` 能不能填 `Release`？

**答案**：不能。`Release` 只在有「写」发生时才有意义；CAS 失败时原子变量没有被修改，标准库因此禁止失败序为 `Release`/`AcqRel`（编译期会被拒绝或降级）。失败序只能取 `Relaxed` / `Acquire` / `SeqCst`。

**练习 2**：`ArrayQueue` 的 CAS 失败序用 `Relaxed`，而 `SegQueue` 的同类 CAS 失败序用 `Acquire`。这种差异的根因是什么？

**答案**：根因是「CAS 失败是否伴随关联结构的变更」。`ArrayQueue` 容量固定、buffer 指针恒定不变，CAS 失败只是游标竞争，无需重读任何关联数据，`Relaxed` 够；`SegQueue` 的 CAS 失败可能伴随「新块被安装」（block 指针变了），失败方必须用 `Acquire` 重读块指针才能看到新块，所以失败序是 `Acquire`。

---

### 4.4 atomic::fence 的二次确认用途

#### 4.4.1 概念说明

两个队列里共有**三处** `atomic::fence(Ordering::SeqCst)`：

- `ArrayQueue::push_or_else` 的 L177（「疑似满」二次确认）；
- `ArrayQueue::pop` 的 L364（「疑似空」二次确认）；
- `SegQueue::pop` 的 L384（「疑似空」二次确认）。

它们解决的同一个问题：**用一个廉价的局部提示（slot 的 stamp / HAS_NEXT 位）怀疑「可能满 / 可能空」后，需要去读「另一个游标」（head 或 tail）做权威确认；而这两次读是两个不同的原子变量、且周围大量是 `Relaxed`，必须插一道 `SeqCst` fence 把它们的相对顺序钉死，并拉进全局全序，否则会读到自相矛盾的 (head, tail) 快照。**

> 关键直觉：fence 不是同步原语本身，它是「顺序的锚点」。它不读写任何变量，但强迫 fence 之前的读和 fence 之后的读在全序里保持程序顺序，并让这次裁决参与所有线程的 `SeqCst` 全序。

为什么要 `SeqCst` fence 而不是 `Acquire` fence？因为这里的判定要和**别的线程的 `SeqCst` CAS / 快照**对齐到一个全序里；`Acquire` fence 只能和 `Release` store 配对，给不出「单一全序」，无法消除下面要讲的竞态。

#### 4.4.2 核心流程

`ArrayQueue::pop` 的「空」判定（最典型的二次确认）：

```
head = self.head.load(Relaxed)          # 循环顶部，Relaxed 起点
loop {
    stamp = slot.stamp.load(Acquire)
    if head + 1 == stamp { ...正常 pop... }
    else if stamp == head {             # 廉价提示：这个槽还没被生产者填 ⇒ 可能空
        fence(SeqCst)                   # ★ 钉住「上面那次 head 读」与「下面那次 tail 读」的相对顺序
        tail = self.tail.load(Relaxed)  # 权威读 tail
        if tail == head { return None } # 确认空
    }
}
```

被 fence 拦住的竞态（没有 fence 时）：因为 `head`（循环顶部）和 `tail`（怀疑空时）是两次独立的 `Relaxed` 读，CPU/编译器可能把它们重排，或让它们落在不一致的全局时刻。最危险的读法是「读到**陈旧的 tail** 恰好等于**当前的 head**」，于是错误地判定队列空、返回 `None`——**丢掉一个本可以 pop 的元素**。fence(SeqCst) 强制「先读 head、后读 tail」的程序顺序进入全序，保证如果 `tail == head` 成立，那么在某个全局时刻队列真的为空。

`push_or_else` 的「满」判定是对称的：先用 stamp 怀疑「可能满」，再 `fence(SeqCst)`，再 `Relaxed` 读 head 做权威确认（在 `push` 的闭包 L205 里），见 4.4.3。

`SegQueue::pop` 的「空」判定利用了 `HAS_NEXT` 这个**元数据位**做廉价提示：当 `new_head & HAS_NEXT == 0`（当前块没有下一块）时，才需要去读 tail 确认是否真空，于是先 `fence(SeqCst)` 再 `Relaxed` 读 tail。

#### 4.4.3 源码精读

`ArrayQueue::push_or_else` 的「疑似满」分支：

```rust
} else if stamp.wrapping_add(self.one_lap) == tail + 1 {
    atomic::fence(Ordering::SeqCst);                       // L177
    value = f(value, tail, new_tail, slot)?;               // L178：f 内会 Relaxed 读 head 判满
    backoff.spin();
    tail = self.tail.load(Ordering::Relaxed);              // L180
}
```

[src/array_queue.rs:176-L180](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L176-L180)。L177 的 fence 钉住「L132 读 tail」与「`f` 内 L205 读 head」的相对顺序。`f`（即 `push` 的闭包）里读 head 用的是 `Relaxed`：

```rust
self.push_or_else(value, |v, tail, _, _| {
    let head = self.head.load(Ordering::Relaxed);   // L205：满判定读 head
    if head.wrapping_add(self.one_lap) == tail {
        Err(v)   // 确认满
    } else {
        Ok(v)
    }
})
```

[src/array_queue.rs:203-L214](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L203-L214)。L205 之所以敢用 `Relaxed`，正是**因为 L177 已经插了 `SeqCst` fence**——可见性与顺序由 fence 兜底，这里再花 `SeqCst` 就重复付费了。

`ArrayQueue::pop` 的「疑似空」分支：

```rust
} else if stamp == head {
    atomic::fence(Ordering::SeqCst);                  // L364
    let tail = self.tail.load(Ordering::Relaxed);     // L365
    if tail == head {                                 // L368：确认空
        return None;
    }
    backoff.spin();
    head = self.head.load(Ordering::Relaxed);         // L373
}
```

[src/array_queue.rs:363-L373](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L363-L373)。fence 钉住「L320 读 head」与「L365 读 tail」的顺序，确保 `tail == head` 这个「空」结论落在某个真实存在的全局时刻。

`SegQueue::pop` 的「疑似空」分支（用 HAS_NEXT 位做廉价提示）：

```rust
if new_head & HAS_NEXT == 0 {
    atomic::fence(Ordering::SeqCst);                          // L384
    let tail = self.tail.index.load(Ordering::Relaxed);       // L385
    if head >> SHIFT == tail >> SHIFT {                       // L388：块号相等 ⇒ 空
        return None;
    }
    if (head >> SHIFT) / LAP != (tail >> SHIFT) / LAP {       // L393：不在同一块 ⇒ 置 HAS_NEXT
        new_head |= HAS_NEXT;
    }
}
```

[src/seg_queue.rs:383-L396](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L383-L396)。这里 fence 的作用完全一致：把「循环顶部对 head 的 Acquire 读」与「L385 对 tail 的 Relaxed 读」拉进全序，使空判定可信。

> 提醒：fence 在「正确性」上的形式化证明是整个 crate 里最微妙的部分，涉及 C11/Rust 内存模型的全序论证。本讲给出的是「为什么需要它」的工程直觉与「去掉它会怎样」的竞态预测，完整的可线性化证明由 [tests/array_queue.rs:349-L375](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/tests/array_queue.rs#L349-L375) 的 `linearizable` 测试在经验层面兜底（见 u4-l4）。

#### 4.4.4 代码实践

**实践目标**：验证「去掉 fence 会破坏满/空二次确认」。

**操作步骤**：

1. 在 `src/array_queue.rs` 注释掉 L364 的 `atomic::fence(Ordering::SeqCst);`（即 pop 的空判定）。
2. 编译，运行：
   ```
   cargo test -p crossbeam-queue --test array_queue linearizable --release
   cargo test -p crossbeam-queue --test array_queue mpmc --release
   cargo test -p crossbeam-queue --test array_queue drops --release
   ```
3. 如果 x86 上不报错，再跑 miri 多种子：
   ```
   MIRIFLAGS="-Zmiri-many-seeds" cargo +nightly miri test -p crossbeam-queue --test array_queue
   ```

**需要观察的现象**：去掉 fence 后，`pop` 可能在队列实际非空时误返回 `None`，导致 `mpmc`/`linearizable` 里出现「消费者提前结束循环」「`drops` 计数对不上」「死锁（生产者以为空、消费者以为满）」之类的症状。注意：这类 bug 在 x86 的 TSO 模型下极难复现，miri 多种子 / Loom 才是正确性验证的主战场。

**预期结果**：fence 不可移除。**若本地无法稳定复现，请明确标注「待本地验证」**，并指出「x86 强模型下不易复现，需弱内存模型工具」。

#### 4.4.5 小练习与答案

**练习 1**：`pop` 里 L364 的 fence 如果改成 `fence(Acquire)`，安全吗？

**答案**：不安全。`Acquire` fence 只能和某个 `Release` store 配对、提供 happens-before，但它**不提供全局单一全序**。这里的空判定需要和别的线程的 `SeqCst` CAS（推进 head/tail）对齐到同一个全序里，才能保证 `tail == head` 是某个真实时刻的快照。只有 `SeqCst` fence 能做到。

**练习 2**：为什么 fence 之后那次读 tail（L365）可以放心用 `Relaxed`，而不必用 `Acquire` 或 `SeqCst`？

**答案**：因为 fence 已经把「读 head → fence → 读 tail」这个程序顺序钉进了全局全序，并保证了两次读之间不被重排、且相对其他线程的 `SeqCst` 操作一致。tail 那次读的「顺序与可见性」已由 fence 兜底，本身再要更强的序就是重复付费。这正是 fence 的价值——**用一次重的 fence，换两次轻的 `Relaxed` 读**。

---

## 5. 综合实践

本讲的综合实践就是把本讲的「代码实践任务」做完，并把结论沉淀成一张论证表。

**任务**：对照 **表 A（`ArrayQueue`）**，逐处论证「为什么不能用更弱的序」，并预测弱化后的竞态。产出一份下表（示例已填两行，请补全其余各行）：

| 行号 | 当前序 | 能否弱化？ | 若弱化为 Relaxed 的竞态预测 |
|---|---|---|---|
| L132 tail.load | Relaxed | 已最弱 | — |
| L152 stamp.load | Acquire | ❌ 不能 | 读到生产者未写完的值（与 L168 Release 失配） |
| L157-162 tail CAS | 成功 SeqCst / 失败 Relaxed | 成功序 ❌ 不能 | （请补：满/空裁决失去全局全序） |
| L168 stamp.store | Release | ❌ 不能 | （请补） |
| L177 fence | SeqCst | ❌ 不能 | （请补） |
| ... | ... | ... | ... |

**操作步骤**：

1. 把表 A 的每一行抄下来，按「该序属于哪类作用」（发布—订阅配对 / 全局裁决 CAS / 双指针快照 / fence 后重读 / 失败序）归到 4.1–4.4 的某一类。
2. 对每一行写下「若改成更弱的序，破坏的是哪条 happens-before / 哪个全序裁决，竞态长什么样」。
3. 选 2–3 处你认为「最可疑、可能可以弱化」的位置，在本地 fork 实际弱化，运行 `cargo test -p crossbeam-queue`，再跑 miri，记录是否复现。
4. 用一句话总结：「crossbeam 的内存序是否每一处都已是最弱可行的？」

**验收标准**：

- 表 A 每一行都有归类与论证。
- 至少完成 2 处「弱化实验」，并如实记录「x86 是否复现 / miri 是否复现 / 是否标注待本地验证」。
- 能清楚说出 `SeqCst` 在本 crate 里只出现在「CAS 成功序」和「双指针快照」两类位置，且各有不可替代的理由。

> 提示：这套论证与 u4-l4 的并发测试（`mpmc`/`drops`/`linearizable` + `cfg!(miri)`）正好互为表里——本讲讲「为什么这样写才对」，u4-l4 讲「怎样用实验抓住写错了的情况」。

## 6. 本讲小结

- **Acquire/Release 配对**是两个队列的骨架：生产者「写值 → Release 标记」，消费者「Acquire 标记 → 读值」，把数据的可见性与有序性绑定在同一对操作上（`ArrayQueue` 的 `slot.stamp`、`SegQueue` 的 `WRITE` 位与块指针 `next`）。
- **`SeqCst` 只出现在两类不可替代的位置**：推进 head/tail 游标的 CAS「成功序」，以及 `is_empty`/`is_full`/`len` 这类需要「单一全序」的双指针快照。
- **`Relaxed` 用在两类「有更强操作兜底」的位置**：循环前的起点值、以及 `fence(SeqCst)` 之后已被钉住顺序的二次重读。
- **`compare_exchange` 的失败序通常可比成功序弱**：`ArrayQueue` 全用 `Relaxed`（容量固定、无关联结构变更）；`SegQueue` 的 index CAS 用 `Acquire`，因为失败可能伴随新块安装，必须重读块指针。
- **`atomic::fence(SeqCst)` 的三处二次确认**钉住了「先读游标 A、后读游标 B」的程序顺序并拉入全局全序，使「满 / 空」判定落在某个真实存在的时刻；去掉它会读到自相矛盾的 (head, tail) 快照而丢数据或死锁。
- 验证内存序错误的可靠手段是 **miri 多种子 / Loom**，而非 x86 上的普通测试——x86 的强内存模型会掩盖大量 bug。

## 7. 下一步学习建议

- **接 u4-l2（CachePadded 与 Backoff）**：本讲的 `head`/`tail` 都用 `CachePadded` 包裹，那是为了避免本讲这些高频原子操作落在同一缓存行上造成伪共享；`Backoff::spin`/`snooze` 则出现在本讲提到的「CAS 失败」「等 stamp/等 WRITE」两条退避路径上。
- **接 u4-l3（unsafe 与 MaybeUninit 安全性）**：本讲反复说「Acquire 读到标记 ⇒ 值已就绪 ⇒ 可安全 `read().assume_init()`」，那正是 `unsafe` 安全性论证的核心；u4-l3 会把这段 `SAFETY` 注释写完整。
- **接 u4-l4（并发测试与可线性化）**：本讲多次提到「用 miri/`linearizable` 测试抓住错误内存序」，u4-l4 会系统讲解这些测试如何设计、`cfg!(miri)` 如何缩规模。
- **延伸阅读**：Dmitry Vyukov 的有界 MPMC 队列原文（`src/array_queue.rs` 文档注释里有链接），以及 C11/Rust 内存模型中关于「SC fence 与全局全序」的形式化定义——这是彻底读懂本讲三处 `fence(SeqCst)` 的最终依据。
