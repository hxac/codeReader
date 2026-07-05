# 项目概览与定位：crossbeam-queue 是什么

## 1. 本讲目标

本讲是整个 `crossbeam-queue` 学习手册的第一篇，目的是让你「先看到森林，再看树」。读完本讲，你应当能够：

1. 用一句话说清楚 `crossbeam-queue` 这个 crate 到底解决什么问题。
2. 区分它的两个核心类型 `ArrayQueue` 与 `SegQueue`：一个有界、一个无界，各自的定位、代价与适用场景。
3. 看懂这个 crate 的版本演进，理解它为什么是现在这个样子（例如为什么用 `MaybeUninit`、为什么支持 `no_std`）。
4. 在真实业务里（任务池、日志缓冲、流水线）做出「该选哪一个」的判断。

本讲**不**深入无锁算法、原子内存序、`unsafe` 安全性论证等硬核内容——这些会放到第二、三、四单元。本讲只建立「整体地图」。

## 2. 前置知识

在开始之前，请确认你理解以下几个通俗概念。如果你已经熟悉，可以跳过本节。

### 2.1 队列（Queue）

队列是一种「先进先出（FIFO）」的数据结构：先放进去的元素，先被取出来。生活中排队买饭就是队列——先到的人先打到饭。Rust 标准库里的 `std::collections::VecDeque` 就是一个普通的（单线程）队列。

### 2.2 生产者 / 消费者

- **生产者（producer）**：往队列里「放」元素的角色。
- **消费者（consumer）**：从队列里「取」元素的角色。

经典例子：Web 服务器收到请求（生产者），把请求塞进队列；后台一组工作线程（消费者）从队列里取请求来处理。

### 2.3 Spsc / Mpsc / SPMC / MPMC

根据生产者和消费者的数量，并发队列常用缩写分类：

| 缩写 | 含义 | 典型场景 |
|------|------|----------|
| SPSC | 单生产者、单消费者 | 两个线程之间传数据 |
| MPSC | 多生产者、单消费者 | 多个线程汇总日志到一个写文件线程 |
| SPMC | 单生产者、多消费者 | 一个分发器派发给多个 worker |
| **MPMC** | 多生产者、多消费者 | 任务池、消息总线 |

`crossbeam-queue` 提供的两个队列都是 **MPMC**，也就是最通用的那种：任意数量的线程都能同时往里放、同时从里取。

### 2.4 有界 vs 无界

- **有界（bounded）**：队列容量是固定的，满了就放不进去。
- **无界（unbounded）**：理论上想放多少放多少，会按需增长。

### 2.5 并发（concurrent）与无锁（lock-free）

多个线程同时操作同一个数据结构时，必须保证不出现「数据竞争」（两个线程同时改同一块内存导致结果错乱）。最朴素的办法是加互斥锁（`Mutex`），但锁有上下文切换、争用等开销。`crossbeam-queue` 采用**无锁（lock-free）**技术，主要靠 CPU 提供的「原子指令（atomic）」和精心设计的算法来实现线程安全，目标是高并发下比加锁更快。这些原理后面单元会讲，现在你只要记住「它能安全地在多线程间共享」即可。

## 3. 本讲源码地图

本讲涉及的关键文件如下。本讲只读它们的「文档注释 / 元信息」部分来建立定位，真正的算法源码留到后续单元。

| 文件 | 作用 |
|------|------|
| [README.md](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/README.md) | crate 的对外说明：定位、用法、依赖、license。 |
| [CHANGELOG.md](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/CHANGELOG.md) | 版本演进历史，是理解「为什么现在长这样」的最佳线索。 |
| [Cargo.toml](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/Cargo.toml) | crate 元信息：名称、版本、feature 开关、依赖。 |
| [src/lib.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/lib.rs) | crate 根，只有 35 行，声明 `no_std`、feature 守卫、对外导出。 |
| [src/array_queue.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs) | `ArrayQueue` 实现（本讲只看它的文档注释和算法出处）。 |
| [src/seg_queue.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs) | `SegQueue` 实现（本讲只看它的结构体文档注释）。 |

> 提示：本讲引用的所有链接都指向当前 HEAD `6195355`，可以直接点击查看对应行。

## 4. 核心概念与源码讲解

### 4.1 MPMC 并发队列概念与版本演进

#### 4.1.1 概念说明

`crossbeam-queue` 是 [crossbeam-rs](https://github.com/crossbeam-rs/crossbeam) 项目下的一个**独立 crate**，整个仓库用一句话就能概括它的定位。crate 根的文档注释这样写：

```rust
//! Concurrent queues.
//!
//! This crate provides concurrent queues that can be shared among threads:
//!
//! * [`ArrayQueue`], a bounded MPMC queue that allocates a fixed-capacity buffer on construction.
//! * [`SegQueue`], an unbounded MPMC queue that allocates small buffers, segments, on demand.
```

这段话出现在 [src/lib.rs:1-6](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/lib.rs#L1-L6)，是理解整个 crate 的钥匙。它告诉我们三件事：

1. **这个 crate 只做一件事**：提供「能在多线程间共享的并发队列」。
2. **它只有两个对外类型**：`ArrayQueue` 和 `SegQueue`。
3. **两个类型都是 MPMC**（多生产者、多消费者），区别在于一个有界、一个无界。

`Cargo.toml` 的元信息进一步印证了这一定位，它把 crate 描述为 `"Concurrent queues"`，并用关键词标明了它的特征：

```toml
description = "Concurrent queues"
keywords = ["queue", "mpmc", "lock-free", "producer", "consumer"]
categories = ["concurrency", "data-structures", "no-std"]
```

见 [Cargo.toml:14-16](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/Cargo.toml#L14-L16)。注意关键词里的 `lock-free` 和 `no-std`——这暗示了它的两大技术特色。

#### 4.1.2 核心流程：crate 是怎么组织起来的

虽然本讲不深入算法，但你应该先建立一个「目录心智模型」。整个 crate 的源码只有三个实质文件：

```text
crossbeam-queue/
├── Cargo.toml
├── README.md
├── CHANGELOG.md
├── src/
│   ├── lib.rs          # crate 根：声明 no_std、feature 守卫、导出
│   ├── array_queue.rs  # ArrayQueue：有界 MPMC 队列
│   ├── seg_queue.rs    # SegQueue：无界分段 MPMC 队列
│   └── alloc_helper.rs # 自定义分配器封装（支撑 no_std + alloc 下的堆分配）
└── tests/
    ├── array_queue.rs  # ArrayQueue 的并发测试
    └── seg_queue.rs    # SegQueue 的并发测试
```

crate 根 [src/lib.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/lib.rs) 只做四件事：声明 `#![no_std]`（[第 8 行](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/lib.rs#L8)）、按需引入 `alloc`/`std`（[第 21-24 行](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/lib.rs#L21-L24)）、用 `target_has_atomic = "ptr"` 守卫编译内部模块（[第 26-31 行](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/lib.rs#L26-L31)），最后把两个队列导出（[第 33-34 行](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/lib.rs#L33-L34)）：

```rust
#[cfg(all(feature = "alloc", target_has_atomic = "ptr"))]
pub use crate::{array_queue::ArrayQueue, seg_queue::SegQueue};
```

这句话的含义是：**只有在「启用了 `alloc` feature」且「目标平台支持指针级原子操作」的前提下，`ArrayQueue` 和 `SegQueue` 才会被编译并对外可见**。原因很简单——这两个队列本质上就是建立在「原子操作」和「堆分配」之上的，没有这两样东西就无法实现。这些 feature 开关的细节会在「u1-l2」讲，现在你只需知道「对外只有两个类型」。

#### 4.1.3 源码精读：版本演进

理解一个项目「为什么长这样」，最有效的办法是读它的 `CHANGELOG`。下面是从 [CHANGELOG.md](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/CHANGELOG.md) 中提炼的几个关键里程碑：

| 版本 | 关键变化 | 意义 |
|------|----------|------|
| [0.1.0](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/CHANGELOG.md#L86-L88) | 初始版本，含 `ArrayQueue` 与 `SegQueue` | 奠定「两类队列」的基本格局 |
| [0.2.1](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/CHANGELOG.md#L68-L70) | 增加 `no_std` 支持 | 让 crate 能在裸机 / 嵌入式上用 |
| [0.2.2](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/CHANGELOG.md#L65-L66) | 采用 `MaybeUninit` 修复「不健全（unsoundness）」问题 | 安全性大改：未初始化内存从此用 `MaybeUninit` 表达 |
| [0.3.0](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/CHANGELOG.md#L56-L59) | 移除 `PushError` / `PopError` | API 简化：满/空改为返回 `Option` |
| [0.3.4](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/CHANGELOG.md#L39-L40) | 实现 `IntoIterator` | 队列可以被 `for` 循环消费 |
| [0.3.5](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/CHANGELOG.md#L36-L37) | 增加 `ArrayQueue::force_push` | `ArrayQueue` 可当环形缓冲用 |
| [0.3.10](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/CHANGELOG.md#L9-L13) | 实现 `UnwindSafe`；优化 `ArrayQueue` 的 `Drop` | panic 安全性 + 性能 |
| [0.3.11](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/CHANGELOG.md#L5-L6) | 移除 `cfg-if` 依赖 | 减少依赖、瘦身 |
| [0.3.12](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/CHANGELOG.md#L1-L3)（当前版本） | 修复向 `SegQueue` push 大元素时的栈溢出 | 把 `Block` 分配到堆上，避免大元素栈溢出 |

当前版本号 [Cargo.toml:7](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/Cargo.toml#L7) 写着 `version = "0.3.12"`，与 CHANGELOG 顶部一致。

这几个里程碑串起来，就能解释本手册后续为什么这么安排：

- 0.2.2 引入 `MaybeUninit` → 所以第四单元专门讲「unsafe 与 MaybeUninit 安全性」。
- 0.2.1 引入 `no_std` → 所以第四单元讲「no_std / alloc 支持」。
- 0.3.12 的栈溢出修复 → 所以第三单元讲 `SegQueue` 时会专门看 `tests/seg_queue.rs` 里的 `stack_overflow` 测试。

#### 4.1.4 代码实践：画出 crate 的「对外 API 树」

**实践目标**：通过阅读源码，确认这个 crate 对外到底暴露了什么，建立「它很小」的直觉。

**操作步骤**：

1. 打开 [src/lib.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/lib.rs)。
2. 找到唯一的 `pub use`（[第 33-34 行](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/lib.rs#L33-L34)），确认它只导出了 `ArrayQueue` 和 `SegQueue`。
3. 在两个源文件里搜索 `pub fn`，把所有公开方法列成一张表。

**需要观察的现象**：你会发现两个类型的公开 API 都非常精简，大致都包含 `new`、`push`（或 `force_push`/`pop`）、`len`、`is_empty`、`is_full`、`capacity` 等。

**预期结果**：你会得到类似下面这样的对照表（具体方法在后续单元逐一精读）：

| 方法类别 | `ArrayQueue` | `SegQueue` |
|----------|--------------|------------|
| 构造 | `new(capacity)` | `new()` |
| 入队 | `push` / `force_push` / `push_mut` | `push` |
| 出队 | `pop` / `pop_mut` | `pop` |
| 容量查询 | `capacity` / `len` / `is_empty` / `is_full` | `len` / `is_empty` |
| 消费 | `IntoIterator` | `IntoIterator` |

> 注：上表为「源码阅读型实践」的预期产出，方法签名细节建议你亲自到 [src/array_queue.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs) 与 [src/seg_queue.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs) 里 `grep` `pub fn` 核对；如果某方法名记不准，标注「待确认」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `crossbeam-queue` 的关键词里有 `mpmc`，而不是 `mpsc`？请用一句话回答。

> **参考答案**：因为它提供的两个队列都允许多个线程同时入队、多个线程同时出队，属于最通用的 MPMC 场景；MPSC 只允许多生产者单消费者，覆盖面更窄。

**练习 2**：0.3.0 版本移除了 `PushError` 和 `PopError`，这对使用者的 API 体验有什么影响？

> **参考答案**：满队列 push 不再返回错误类型，而是返回 `false`（`push`）或返回被挤掉的旧值（`force_push`）；空队列 pop 直接返回 `Option<T>`（`None` 表示空）。这让调用方不再需要 `match` 错误枚举，写起来更轻量。

---

### 4.2 ArrayQueue 的有界 MPMC 定位

#### 4.2.1 概念说明

`ArrayQueue<T>` 是一个**有界（bounded）的 MPMC 队列**。它的核心特征是：**在构造时就分配好一块固定容量的缓冲区**，之后容量永不改变。放满了就放不进去。

它的结构体文档注释把定位说得很清楚（[src/array_queue.rs:29-38](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L29-L38)）：

```rust
/// A bounded multi-producer multi-consumer queue.
///
/// This queue allocates a fixed-capacity buffer on construction, which is used to store pushed
/// elements. The queue cannot hold more elements than the buffer allows. Attempting to push an
/// element into a full queue will fail. Alternatively, [`force_push`] makes it possible for
/// this queue to be used as a ring-buffer. Having a buffer allocated upfront makes this queue
/// a bit faster than [`SegQueue`].
```

提取关键点：

1. **有界**：构造时确定容量，之后不可超过。
2. **满则失败**：`push` 到满队列会失败（返回 `false`）。
3. **可当环形缓冲**：`force_push` 在满时会挤掉最旧的元素，于是它能像「环形缓冲（ring buffer）」一样用。
4. **比 SegQueue 略快**：因为缓冲区是预先一次性分配的，运行时不需要动态申请内存。

它的算法出处也在文件顶部注明（[src/array_queue.rs:1-4](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L1-L4)）：

```rust
//! The implementation is based on Dmitry Vyukov's bounded MPMC queue.
//!
//! Source:
//!   - <http://www.1024cores.net/home/lock-free-algorithms/queues/bounded-mpmc-queue>
```

也就是说，`ArrayQueue` 是 Dmitry Vyukov 的「有界 MPMC 队列」算法的 Rust 实现。本讲你只需记住「它有界、预分配、略快」，算法细节（stamp / lap / CAS 循环）放到第二单元。

#### 4.2.2 核心流程：ArrayQueue 的生命周期

从使用者视角，`ArrayQueue` 的生命周期可以概括为：

```text
构造：ArrayQueue::new(capacity)
        └─ 一次性分配 capacity 个槽（slot），每个槽带一个 stamp

入队：push(value)
        └─ 队列未满 → 写入槽，返回 true
        └─ 队列已满 → 返回 false
     force_push(value)
        └─ 队列已满 → 覆盖最旧元素，返回 Some(被覆盖的旧值)

出队：pop()
        └─ 队列非空 → 返回 Some(值)
        └─ 队列为空 → 返回 None

销毁：Drop
        └─ 释放所有还在队列里的值和底层缓冲
```

关键直觉是：**内存布局从构造那一刻就固定下来了**，运行时不再向操作系统要内存，这正是它「略快」的原因。

#### 4.2.3 源码精读

定位相关的源码点：

- [src/array_queue.rs:1-4](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L1-L4)：标注算法出处（Vyukov bounded MPMC）。
- [src/array_queue.rs:17-27](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L17-L27)：`Slot<T>` 的定义——一个 `stamp` 加一个 `MaybeUninit<T>` 值。这告诉我们「有界」是靠「固定数量的槽」实现的，每个槽里存一个值和一个状态戳。
- [src/array_queue.rs:29-38](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L29-L38)：上面引用的结构体文档，定义了「有界、预分配、可环形缓冲、略快」四大定位。

#### 4.2.4 代码实践：感受「有界」

**实践目标**：通过一个最小示例，亲眼看到「容量固定、满了 push 返回 false」。

**操作步骤**（这是示例代码，不是项目原有代码）：

```rust
// 示例代码：放在一个临时 binary 里运行
use crossbeam_queue::ArrayQueue;

fn main() {
    let q = ArrayQueue::new(2);           // 容量固定为 2
    assert!(q.push('a'));                  // 成功
    assert!(q.push('b'));                  // 成功，队列现在满了
    assert!(!q.push('c'));                 // 失败：已满，返回 false
    assert_eq!(q.len(), 2);
    assert!(q.is_full());

    // force_push 当环形缓冲用：挤掉最旧的 'a'
    assert_eq!(q.force_push('c'), Some('a'));
    assert_eq!(q.pop(), Some('b'));
    assert_eq!(q.pop(), Some('c'));
    assert!(q.pop().is_none());            // 空了
}
```

**需要观察的现象**：`push('c')` 在满队列上返回 `false`，而 `force_push('c')` 返回被挤掉的旧值 `Some('a')`。

**预期结果**：程序正常运行、所有断言通过，直观印证「有界 + 可环形缓冲」两个定位。

> 注：若你尚未把依赖加进项目，运行方式见下一节「综合实践」。如果暂时不方便运行，可标注「待本地验证」，但断言逻辑可以直接从 [src/array_queue.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs) 的文档示例（[第 40-44 行起](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L40-L44)）推导出来。

#### 4.2.5 小练习与答案

**练习 1**：`ArrayQueue::new(capacity)` 的容量在构造后还能改变吗？这对内存分配意味着什么？

> **参考答案**：不能改变。这意味着构造时就一次性分配好整块缓冲区，运行期间不再发生堆分配，因此高并发下不会有「因动态分配导致的延迟尖峰」，这正是它比 `SegQueue` 快的原因之一。

**练习 2**：`push` 和 `force_push` 在「队列已满」时的行为区别是什么？

> **参考答案**：`push` 满了直接失败、返回 `false`，新元素被丢弃；`force_push` 满了会覆盖最旧的元素，把被覆盖的旧值用 `Some(...)` 返回（队列未满时则正常入队、返回 `None`）。所以 `force_push` 让 `ArrayQueue` 能当环形缓冲用。

---

### 4.3 SegQueue 的无界分段 MPMC 定位

#### 4.3.1 概念说明

`SegQueue<T>` 是一个**无界（unbounded）的 MPMC 队列**。它没有容量上限，元素越多，它就分配越多的「分段（segment）」。每个分段是一小块固定容量的缓冲区，多个分段用链表串起来。

它的结构体文档注释这样定位（[src/seg_queue.rs:138-145](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L138-L145)）：

```rust
/// An unbounded multi-producer multi-consumer queue.
///
/// This queue is implemented as a linked list of segments, where each segment is a small buffer
/// that can hold a handful of elements. There is no limit to how many elements can be in the queue
/// at a time. However, since segments need to be dynamically allocated as elements get pushed,
/// this queue is somewhat slower than [`ArrayQueue`].
```

提取关键点：

1. **无界**：理论上能装无限多元素（受限于内存）。
2. **分段链表**：内部是「小缓冲区组成的链表」，而不是一整块大数组。
3. **按需动态分配**：元素多了就分配新分段，因此比 `ArrayQueue` 慢一些。

为什么「分段」而不是「一整块可扩容的数组」？因为无锁并发场景下，给一个大数组扩容（搬家）非常难做对；而「追加一个固定大小的新块」就容易得多，每个生产者只需在链表尾部接一块即可。这是无锁算法的一个常见权衡。

`SegQueue` 内部用的几个关键常量定义在文件顶部（[src/seg_queue.rs:21-32](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L21-L32)）：

```rust
const WRITE: usize = 1;
const READ: usize = 2;
const DESTROY: usize = 4;

const LAP: usize = 32;
const BLOCK_CAP: usize = LAP - 1;   // 每个块最多装 31 个元素
const SHIFT: usize = 1;
const HAS_NEXT: usize = 1;
```

本讲你只需记住一个数字：**每个分段（block）最多容纳 31 个元素**（`BLOCK_CAP = LAP - 1 = 31`）。当第 32 个元素要入队时，就会新分配一个块挂到链表上。这些常量的位运算含义会放到第三单元详解。

#### 4.3.2 核心流程：SegQueue 的生命周期

从使用者视角，`SegQueue` 的生命周期可以概括为：

```text
构造：SegQueue::new()
        └─ 创建一个空的头/尾块指针（首个块在首次 push 时分配）

入队：push(value)
        └─ 当前块没满 → 写入当前块的一个槽
        └─ 当前块满了 → 分配一个新块，挂到链表尾部，再写入

出队：pop()
        └─ 当前块还有可读元素 → 返回 Some(值)
        └─ 当前块读空且后面还有块 → 转到下一块继续读
        └─ 没有任何可读元素 → 返回 None（空队列）

内存回收：destroy / Drop
        └─ 某个块被完全消费后，安全地释放它的内存
```

关键直觉是：**容量随元素数量增长，内存是「边跑边分配/释放」的**。

#### 4.3.3 源码精读

定位相关的源码点：

- [src/seg_queue.rs:21-32](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L21-L32)：状态位（`WRITE/READ/DESTROY`）与分段常量（`LAP/BLOCK_CAP/SHIFT/HAS_NEXT`）。
- [src/seg_queue.rs:53-62](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L53-L62)：`Block<T>` 的定义——一个 `next` 指针加一个 `slots` 数组。这就是「分段链表」节点。
- [src/seg_queue.rs:138-145](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L138-L145)：上面引用的结构体文档，定义了「无界、分段链表、按需分配、略慢」四大定位。

#### 4.3.4 代码实践：感受「无界 + 按需增长」

**实践目标**：用一个最小示例，看到 `SegQueue` 可以一直 push、不需要预先指定容量。

**操作步骤**（示例代码，不是项目原有代码）：

```rust
// 示例代码
use crossbeam_queue::SegQueue;

fn main() {
    let q = SegQueue::new();              // 构造时不需要容量
    for i in 0..100_000 {                 // 远超单个块的 31 容量
        q.push(i);
    }
    // 依次弹出，应当严格保持 0,1,2,... 的顺序
    for i in 0..100_000 {
        assert_eq!(q.pop(), Some(i));
    }
    assert!(q.pop().is_none());
}
```

**需要观察的现象**：尽管单个块只能装 31 个元素，但你 push 了 10 万个也没问题——内部自动分配了约 `ceil(100_000 / 31)` 个块。

**预期结果**：所有断言通过，印证「无界、按需分配、FIFO 有序」。

> 注：运行方式见下一节。如果暂不便运行，标注「待本地验证」；顺序保证的依据来自 `SegQueue` 的 MPMC FIFO 语义。

#### 4.3.5 小练习与答案

**练习 1**：`SegQueue::new()` 为什么不需要传容量参数？

> **参考答案**：因为它是无界的，容量由「当前块数 × 31」动态决定，会随元素增长而增长，所以构造时没有「固定容量」的概念可填。

**练习 2**：为什么 `SegQueue` 会比 `ArrayQueue`「略慢」？从内存分配角度回答。

> **参考答案**：`SegQueue` 在运行时需要不断地分配新分段（堆分配是相对昂贵的操作），还要维护链表指针、在块之间跳转；而 `ArrayQueue` 在构造时就一次性分配好整块缓冲区，运行时不再分配。多出来的动态分配和间接寻址，就是 `SegQueue` 较慢的主要原因。

---

## 5. 综合实践

把本讲的「定位」知识串起来，完成下面这个综合任务。

### 任务：为两个真实业务场景选型，并写出依据

**背景**：你正在为两个业务模块挑选队列。请基于本讲学到的 `ArrayQueue` / `SegQueue` 定位差异做决策。

**场景 A：固定大小的任务池（worker pool）**
- 一组工作线程从一个共享队列里取任务执行。
- 你希望严格控制「积压任务数」，避免内存被无限撑大。
- 任务是短小的、来一个处理一个。

**场景 B：异步日志缓冲（logging buffer）**
- 多个线程往队列里塞日志，一个后台线程负责写文件。
- 日志的来源速率无法预测，偶尔会突发大量日志。
- 你不希望因为「队列满了」而丢弃日志（宁可多占点内存）。

**操作步骤**：

1. 阅读本讲引用的 README 段落 [README.md:15-24](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/README.md#L15-L24)，以及两个结构体的文档注释（[ArrayQueue](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L29-L38)、[SegQueue](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L138-L145)）。
2. 为场景 A 和场景 B 各选一个队列类型。
3. 用 200 字左右写出你的选型理由，要点包含：是否有界、是否需要控制内存、对「丢弃 vs 阻塞 vs 增长」的取舍。

**预期结果（参考答案，请你先自己想再对照）**：

- **场景 A 选 `ArrayQueue`**：任务池需要「有界」来防止积压把内存撑爆；任务短小、要求低延迟，`ArrayQueue` 预分配带来的「略快」优势正好契合；满了时可以让生产者稍后重试或施以背压（backpressure）。
- **场景 B 选 `SegQueue`**：日志不能丢，需要无界增长来吸收突发流量；宁可牺牲一点速度换取「永不因满而拒绝写入」。`SegQueue` 的按需增长正好满足。

> 如果你想把决策落地成代码，可以参考 [README.md:26-33](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/README.md#L26-L33) 的依赖写法，在 `Cargo.toml` 加入 `crossbeam-queue = "0.3"`，再用本讲 4.2.4 / 4.3.4 的示例代码起一个最小验证。是否实际编译运行可标注「待本地验证」。

## 6. 本讲小结

- `crossbeam-queue` 是一个**只做一件事**的 crate：提供可在多线程间共享的**并发队列**，对外只有 `ArrayQueue` 和 `SegQueue` 两个类型（见 [src/lib.rs:33-34](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/lib.rs#L33-L34)）。
- 两者都是 **MPMC**（多生产者、多消费者），采用**无锁（lock-free）**实现（见 [Cargo.toml:15](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/Cargo.toml#L15)）。
- **`ArrayQueue` 有界、构造时预分配固定缓冲区、满了 push 失败、可用 `force_push` 当环形缓冲、比 `SegQueue` 略快**（见 [src/array_queue.rs:29-38](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L29-L38)）。
- **`SegQueue` 无界、用分段链表实现、按需动态分配、比 `ArrayQueue` 略慢**（见 [src/seg_queue.rs:138-145](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs#L138-L145)）。
- 整个 crate 支持 `no_std`（只要有 `alloc`），关键词 `no-std` 体现在 [Cargo.toml:16](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/Cargo.toml#L16)，最早在 0.2.1 引入。
- 版本演进（`MaybeUninit` 安全修复、`force_push`、`IntoIterator`、栈溢出修复等）解释了后续单元为什么要那样安排——见 [CHANGELOG.md](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/CHANGELOG.md)。

## 7. 下一步学习建议

本讲建立了「整体地图」，下一讲 **u1-l2《目录结构与 crate 入口：lib.rs、feature 与条件编译》** 会带你精读 [src/lib.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/lib.rs) 这 35 行，搞清楚：

- `#![no_std]` 到底意味着什么；
- `std` / `alloc` 两个 feature 开关如何控制编译；
- `target_has_atomic = "ptr"` 这个守卫为什么是必须的。

如果你急于看到队列「动起来」，可以先跳到 **u1-l3《快速上手》** 跑通第一个 `push/pop` 程序，再回头读 u1-l2。等到第二、三单元，我们才会真正打开 [src/array_queue.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs) 和 [src/seg_queue.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/seg_queue.rs) 去看无锁算法的内部实现。
