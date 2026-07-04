# Stealer、Injector 与 Steal 结果工作流

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 `Stealer` 和 `Worker` 的关系：同一个底层队列的「只读偷取视图」，可以跨线程共享、可以 `Clone`。
- 说清 `Injector` 的角色：一个全局共享的 FIFO、多生产多消费（MPMC）任务入口。
- 牢记 `Steal<T>` 枚举的三种取值 `Empty / Success(T) / Retry` 以及它们各自的语义。
- 会用 `Steal::or_else`、`FromIterator`（即 `collect::<Steal<_>>()`）、`success`、`is_retry` 这些组合子，把多个偷取操作串成 `find_task` 风格的回退链。

本讲只停留在「会用公开 API、能读懂接口契约」的层面，**不深入无锁算法、CAS、内存序与 epoch 回收**——那些留给进阶层（u2）与专家层（u4）。本讲承接 u1-l3 建立的 `Worker` / `front` / `back` 心智模型。

## 2. 前置知识

在进入本讲前，请确认你已经理解以下概念（来自 u1-l1 ～ u1-l3）：

- **work-stealing 拓扑**：每个工作线程拥有一个私有的 `Worker` 本地队列；线程之间通过 `Stealer` 互相偷任务；还有一个全局的 `Injector` 用于从外部注入任务。
- **`Worker` 是单线程私有**：通过 `_marker: PhantomData<*mut ()>` 被标记为 `!Send + !Sync`（作者再用 `unsafe impl Send` 只放开 `Send`），所以 `Worker` 可 move、不可共享 `&`。
- **`front` / `back` 双游标**：本地队列用两个游标刻画，`len = back - front`；`push` 总是在 `back` 端写入，`pop` 在 FIFO 里取 `front`、在 LIFO 里取 `back-1`。

本讲新引入两个术语：

- **偷取（steal）**：与 `pop` 相对。`pop` 是拥有者从自己的队列「自己端」取任务；`steal` 是别的线程从该队列「对端」取任务。偷取可能**伪失败**，需要重试。
- **MPMC**：Multiple Producer Multiple Consumer，多生产者多消费者。`Injector` 任意线程都能 `push`、任意线程都能 `steal`。

## 3. 本讲源码地图

本讲涉及两个源码文件：

| 文件 | 作用 |
| --- | --- |
| `src/lib.rs` | crate 门面。顶部一段 crate 级文档，其中包含 `find_task` 的完整 doctest 示例——本讲的「回退链」范本就在这里。 |
| `src/deque.rs` | 全部实现。本讲关注其中三个公开类型：`Stealer<T>`、`Injector<T>`、`Steal<T>`。 |

公开导出在 `src/lib.rs` 末尾一行：

```rust
pub use crate::deque::{Injector, Steal, Stealer, Worker};
```

这四个类型是 crate 对外的全部公开符号（见 [src/lib.rs:108-109](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/lib.rs#L108-L109)，中文说明：把实现模块 `deque` 里的四个类型提升到 crate 根）。

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块，按「先认识结果类型 → 再认识两个生产者 → 最后用组合子把它们串起来」的顺序推进：

1. `Steal` 结果枚举
2. `Stealer`：可共享的偷取句柄
3. `Injector`：全局 FIFO 入口
4. `Steal` 组合子与 `find_task` 回退链

### 4.1 Steal 结果枚举：Empty / Success / Retry

#### 4.1.1 概念说明

任何偷取操作（`Stealer::steal`、`Injector::steal`、各种 `steal_batch*`）的返回值都不是 `Option<T>`，而是 `Steal<T>`。原因在于无锁偷取存在一种**伪失败**：操作本身没出错，但因为别的线程同时也在偷、或者底层缓冲区正好被换掉，本次没偷到，但**重试一次很可能就成功了**。

`Steal<T>` 用三个变体精确表达这三种结局：

- `Empty`：队列此刻是空的，确实没有任务可偷。
- `Success(T)`：成功偷到一个任务。
- `Retry`：发生了伪失败，请重试。

区分 `Empty` 和 `Retry` 至关重要：调度器遇到 `Empty` 可以转而去别处找任务，遇到 `Retry` 则通常应该原地重试或自旋。把它们都塞进 `None` 会让调度器丢失「该重试」这个关键信息。

#### 4.1.2 核心流程

`Steal<T>` 本身只是一个普通枚举，它的威力来自附带的查询方法与组合子：

| 方法 | 返回 | 语义 |
| --- | --- | --- |
| `is_empty()` | `bool` | 仅当 `Empty` 为真 |
| `is_success()` | `bool` | 仅当 `Success(_)` 为真 |
| `is_retry()` | `bool` | 仅当 `Retry` 为真 |
| `success()` | `Option<T>` | `Success(t)` → `Some(t)`，其余 → `None` |
| `or_else(f)` | `Steal<T>` | 见下方真值表 |
| `collect()`（`FromIterator`） | `Steal<T>` | 聚合一组结果 |

`or_else` 的真值表（`self` 为左、`f()` 为右）：

| self ＼ f() | Empty | Success(x) | Retry |
| --- | --- | --- | --- |
| **Empty** | Empty | Success(x) | Retry |
| **Success(s)** | Success(s) | Success(s) | Success(s) |
| **Retry** | Retry | Success(x) | Retry |

口诀：**`Success` 一票否决（优先返回成功的那个）；两个都失败时，只要有任一个是 `Retry` 就返回 `Retry`，否则返回 `Empty`。** 这保证「该重试」的信号不会在回退链中丢失。

#### 4.1.3 源码精读

枚举定义见 [src/deque.rs:2085-2094](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L2085-L2094)，中文说明：`Steal<T>` 的三个变体及其文档注释。

```rust
pub enum Steal<T> {
    /// The queue was empty at the time of stealing.
    Empty,
    /// At least one task was successfully stolen.
    Success(T),
    /// The steal operation needs to be retried.
    Retry,
}
```

`or_else` 的实现精读，见 [src/deque.rs:2185-2200](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L2185-L2200)（中文说明：`Success` 直接保留；`Empty` 转而采用备选 `f()` 的结果；`Retry` 时只有备选也成功才返回成功，否则维持 `Retry`）：

```rust
pub fn or_else<F>(self, f: F) -> Self
where F: FnOnce() -> Self,
{
    match self {
        Self::Empty => f(),
        Self::Success(_) => self,
        Self::Retry => {
            if let Self::Success(res) = f() {
                Self::Success(res)
            } else {
                Self::Retry
            }
        }
    }
}
```

`FromIterator` 实现见 [src/deque.rs:2213-2233](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L2213-L2233)，中文说明：遍历一组 `Steal` 结果，遇到第一个 `Success` 立即返回；否则只要有任一 `Retry` 就返回 `Retry`，全为 `Empty` 才返回 `Empty`。这就是 `stealers.iter().map(|s| s.steal()).collect()` 的行为依据。

```rust
let mut retry = false;
for s in iter {
    match &s {
        Self::Empty => {}
        Self::Success(_) => return s,
        Self::Retry => retry = true,
    }
}
if retry { Self::Retry } else { Self::Empty }
```

`success()` 见 [src/deque.rs:2157-2162](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L2157-L2162)，把 `Steal<T>` 收拢成 `Option<T>`；`is_retry()` 见 [src/deque.rs:2141-2143](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L2141-L2143)。

#### 4.1.4 代码实践

**实践目标**：在不涉及任何并发的前提下，单文件验证 `Steal` 组合子的真值表。

**操作步骤**：新建一个二进制 crate，添加 `crossbeam-deque = "0.8"` 依赖，写下下面的 `main`：

```rust
// 示例代码：纯单线程，验证 Steal 组合子语义
use crossbeam_deque::Steal::{self, Empty, Retry, Success};

fn main() {
    // Success 一票否决
    assert_eq!(Success(1).or_else(|| Success(2)), Success(1));
    // 左失败时采用右
    assert_eq!(Empty::<i32>.or_else(|| Success(2)), Success(2));
    // 任一 Retry 且无 Success → Retry
    assert_eq!(Empty.or_else(|| Retry), Retry::<i32>);
    assert_eq!(Retry.or_else(|| Empty), Retry::<i32>);
    // 全 Empty → Empty
    assert_eq!(Empty.or_else(|| Empty), Empty::<i32>);

    // collect 聚合一组
    let v: Vec<Steal<i32>> = vec![Empty, Retry, Empty];
    assert_eq!(v.into_iter().collect::<Steal<_>>(), Retry::<i32>);

    // success 收拢成 Option
    assert_eq!(Success(7).success(), Some(7));
    assert_eq!(Retry::<i32>.success(), None);
    println!("all Steal combinator assertions passed");
}
```

**需要观察的现象**：所有断言通过，打印 `all Steal combinator assertions passed`。

**预期结果**：上面给出的真值表与代码行为完全一致。

#### 4.1.5 小练习与答案

**练习 1**：`vec![Success(1), Success(2), Empty].into_iter().collect::<Steal<_>>()` 的结果是什么？

**答案**：`Success(1)`。`FromIterator` 遇到第一个 `Success` 立即返回，后续元素不再消费。

**练习 2**：为什么 `Steal` 不直接设计成 `Option<T>` 加一个 `retry` 标志位？

**答案**：因为回退链需要把「多个偷取结果」聚合。`Option` 只能表达「有/无」，无法在 `collect` 时区分「全空」与「有人让我重试」——后者必须重新尝试。`Retry` 作为独立变体，使得 `or_else` 和 `collect` 能精确传递重试信号。

---

### 4.2 Stealer：可跨线程共享的偷取句柄

#### 4.2.1 概念说明

`Worker` 是单线程私有的，那别的线程怎么偷它的任务？答案是：拥有者调用 `Worker::stealer()` 派生出一个 `Stealer` 句柄，把它（的克隆）分发给其他线程。

`Stealer` 与 `Worker` **共享同一份底层队列**（同一个 `Arc<CachePadded<Inner<T>>>`），但 `Stealer` 只能「偷」（从对端取），不能 `push`、不能 `pop`。与 `Worker` 的关键区别在并发属性：

- `Stealer<T>: Send + Sync`（当 `T: Send`），可以跨线程共享 `&Stealer`。
- `Stealer` 实现了 `Clone`——克隆只是增加 `Arc` 的引用计数，多个线程各持一份指向同一队列。

#### 4.2.2 核心流程

派生与使用流程：

1. 拥有者创建 `let w = Worker::new_fifo();`，push 若干任务。
2. 调用 `let s = w.stealer();`，得到一个共享句柄；可 `s.clone()` 多份。
3. 其他线程通过 `s.steal()` 偷一个任务，或 `s.steal_batch(&dest)` / `s.steal_batch_and_pop(&dest)` 批量偷到另一个 `Worker` 里。
4. 偷取方向始终是**从 push 的对端**：FIFO Worker 偷到的顺序与 push 顺序一致；LIFO Worker 偷到的是最早 push 的那些（也是对端）。

三个偷取方法（每个都有 `_with_limit` 变体）：

| 方法 | 返回 | 行为 |
| --- | --- | --- |
| `steal()` | `Steal<T>` | 偷单个任务 |
| `steal_batch(&dest)` | `Steal<()>` | 偷约半数（上限 `MAX_BATCH`）批量移入 `dest`，不弹出 |
| `steal_batch_and_pop(&dest)` | `Steal<T>` | 批量移入 `dest` 并顺手弹出一个返回 |

#### 4.2.3 源码精读

`Stealer` 结构体见 [src/deque.rs:574-580](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L574-L580)，中文说明：只有两个字段——与 `Worker` 共享的 `inner`（`Arc`）和队列口味 `flavor`。

```rust
pub struct Stealer<T> {
    inner: Arc<CachePadded<Inner<T>>>,
    flavor: Flavor,
}
```

并发属性声明见 [src/deque.rs:582-583](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L582-L583)（中文说明：`unsafe impl Send` 和 `unsafe impl Sync`，都要求 `T: Send`）。这与 `Worker` 的 `!Send + !Sync` 形成鲜明对比——同样是底层队列，`Worker` 用 `PhantomData<*mut ()>` 自我封闭，`Stealer` 则显式开放共享。

派生方法 `Worker::stealer()` 见 [src/deque.rs:282-287](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L282-L287)，中文说明：克隆 `inner` 的 `Arc`、复制 `flavor`，构造一个共享句柄。

```rust
pub fn stealer(&self) -> Stealer<T> {
    Stealer {
        inner: self.inner.clone(),
        flavor: self.flavor,
    }
}
```

`Stealer::steal()` 的核心见 [src/deque.rs:641-683](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L641-L683)，中文说明：加载 `front` → `epoch::pin()` → 加载 `back` 判空 → 读槽位 → 用 CAS 推进 `front`，若期间 buffer 被换或 CAS 失败则返回 `Retry`。本讲只看「接口契约」：判空返回 `Empty`，竞争失败返回 `Retry`，成功返回 `Success`。算法细节（为什么需要 `epoch::pin`、为什么读后还要再校验 buffer）留到 u2-l3。

```rust
// 关键骨架（已裁剪，省略 fence / pin 细节）
let f = self.inner.front.load(Ordering::Acquire);
// ... epoch::pin() 与可能的 SeqCst fence ...
let b = self.inner.back.load(Ordering::Acquire);
if b.wrapping_sub(f) <= 0 {
    return Steal::Empty;            // 队列空
}
let buffer = self.inner.buffer.load(Ordering::Acquire, guard);
let task = unsafe { buffer.deref().read(f) };
// 若 buffer 被换或 CAS front 失败 → Retry
if /* buffer 变了 */ || /* CAS 失败 */ {
    return Steal::Retry;
}
Steal::Success(unsafe { task.assume_init() })
```

`Stealer::steal_batch` 委托给 `steal_batch_with_limit`，见 [src/deque.rs:708-710](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L708-L710)；`steal_batch_and_pop` 委托给 `steal_batch_with_limit_and_pop`，见 [src/deque.rs:949-951](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L949-L951)。批量大小按「约一半、且不超过 `limit`」计算（u2-l4 精读）：

```rust
let batch_size = cmp::min((len as usize).div_ceil(2), limit);
```

`Clone` 实现见 [src/deque.rs:1181-1188](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1181-L1188)，中文说明：克隆 `inner`（`Arc` 引用计数 +1）、复制 `flavor`，两份 `Stealer` 指向同一队列。

#### 4.2.4 代码实践

**实践目标**：验证 `Stealer` 可克隆、可被「当作」跨线程共享，并观察偷取顺序。

**操作步骤**：

```rust
// 示例代码：单线程验证 Stealer 行为
use crossbeam_deque::{Steal, Worker};

fn main() {
    let w = Worker::new_lifo();
    w.push(1);
    w.push(2);
    w.push(3);

    // 派生两个 Stealer，指向同一队列
    let s1 = w.stealer();
    let s2 = s1.clone();

    // LIFO 的 push 在 back 端，偷取从对端（front，最早 push 的）取
    assert_eq!(s1.steal(), Steal::Success(1));
    assert_eq!(s2.steal(), Steal::Success(2)); // s2 看到的是同一个队列
    assert_eq!(s1.steal(), Steal::Success(3));
    assert_eq!(s1.steal(), Steal::Empty);

    // 验证 Send + Sync：把 &Stealer 送到另一个线程
    std::thread::scope(|scope| {
        let w2 = Worker::new_fifo();
        w2.push(10);
        let s = w2.stealer();
        scope.spawn(move || {
            assert_eq!(s.steal(), Steal::Success(10));
        });
    });
    println!("stealer observations OK");
}
```

**需要观察的现象**：第一段中 `s1` 与 `s2` 看到的是同一份队列（偷走一个就少一个）；LIFO 队列被偷的顺序是 `1, 2, 3`（最早 push 的先被偷）。第二段能编译通过，证明 `Stealer: Send + Sync`。

**预期结果**：打印 `stealer observations OK`。注意 `Worker` 本身不能这样送进 `spawn`（它是 `!Sync`），但 `Stealer` 可以。

#### 4.2.5 小练习与答案

**练习 1**：对同一个 `Worker`，`stealer()` 调用两次得到的两个 `Stealer`，是否指向同一队列？

**答案**：是。两次调用都 `clone` 同一个 `Arc<Inner>`，引用同一份底层数据。同理 `s.clone()` 也是同一队列。

**练习 2**：为什么 `Stealer` 的 `steal` 从「push 的对端」取？

**答案**：work-stealing 的设计意图是：拥有者优先处理自己刚 push 的「热」任务（LIFO 端），偷取者拿走「冷」的、最老的任务（对端）。这样双方操作的数据尽量不重叠，减少缓存竞争与冲突。

---

### 4.3 Injector：全局共享的 FIFO 任务入口

#### 4.3.1 概念说明

`Injector<T>` 是一个全局唯一的、所有线程共享的 **FIFO** 队列，扮演「外部任务入口」：调度器把新提交的任务 `push` 进去，空闲线程来 `steal`。它是 MPMC 的——任意线程都能 `push`、任意线程都能 `steal`。

与 `Stealer`（偷别人的本地队列）不同，`Injector` 不从属于任何 `Worker`，它独立存在，通常整个调度器只有一份。

> 实现上 `Injector` 与 `Worker/Stealer` 完全不同：后者是 Chase-Lev 环形缓冲区，前者是 block 链表式队列。**本讲只讲它的公开行为**，链表与 block 结构留到 u3。

#### 4.3.2 核心流程

`Injector` 的对外接口非常小：

- `Injector::new()`：创建空队列。
- `push(task)`：尾部追加一个任务。
- `steal()`：从头偷一个任务（FIFO，最早 push 的先出）。
- `steal_batch(&dest)` / `steal_batch_and_pop(&dest)`：批量偷到某个 `Worker`。
- `is_empty()` / `len()`：查询状态。

典型用法：

```text
外部线程: global.push(new_task)
工作线程: 循环 { find_task(local, global, &stealers) }
```

#### 4.3.3 源码精读

`Injector` 结构体见 [src/deque.rs:1332-1341](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1332-L1341)，中文说明：由 `head`、`tail` 两个 `Position` 组成（都包了 `CachePadded` 防伪共享），加一个 `_marker: PhantomData<T>` 表示 Drop 时可能丢弃 `T`。

```rust
pub struct Injector<T> {
    head: CachePadded<Position<T>>,
    tail: CachePadded<Position<T>>,
    _marker: PhantomData<T>,
}
```

并发属性同样 `Send + Sync`（`T: Send`），见 [src/deque.rs:1343-1344](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1343-L1344)。

`new()` 委托 `Default`，见 [src/deque.rs:1346-1375](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1346-L1375)（中文说明：分配第一个 `Block`，`head` 和 `tail` 都指向它、`index` 都为 0）。

`push` 的核心见 [src/deque.rs:1388-1446](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1388-L1446)，中文说明：循环里用 `compare_exchange_weak` 乐观推进 `tail.index`，成功后把任务写入槽位并置 `WRITE` 位；到块尾时安装下一个 block。本讲只关心接口契约：`push` 不会失败、不返回值。

```rust
// 关键骨架（裁剪）
loop {
    let offset = (tail >> SHIFT) % LAP;
    if offset == BLOCK_CAP { backoff.snooze(); /* 等下一个 block */ continue; }
    match self.tail.index.compare_exchange_weak(tail, new_tail, SeqCst, Acquire) {
        Ok(_) => {
            // 写槽位 + fetch_or(WRITE) 发布任务
            return;
        }
        Err(t) => { tail = t; backoff.spin(); }
    }
}
```

`steal` 的核心见 [src/deque.rs:1464-1540](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1464-L1540)，中文说明：加载 `head` → 判空（与 `tail` 比较）返回 `Empty` → CAS 推进 `head`，失败返回 `Retry`，成功读取任务返回 `Success`。可见 `Injector::steal` 与 `Stealer::steal` 的**返回类型与三态语义完全一致**，这也是它们能用同一套 `Steal` 组合子的原因。

`is_empty` 见 [src/deque.rs:1967-1971](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1967-L1971)，比较 `head` 与 `tail` 的高位是否相等；`len` 见 [src/deque.rs:1988](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L1988) 起，用一个重读循环保证读到一致的一对索引（u3-l4 精读）。

#### 4.3.4 代码实践

**实践目标**：验证 `Injector` 是 FIFO，且 `steal` 同样可能返回 `Empty`。

**操作步骤**：

```rust
// 示例代码：单线程验证 Injector 的 FIFO 行为
use crossbeam_deque::{Injector, Steal};

fn main() {
    let q = Injector::new();
    q.push(10);
    q.push(20);
    q.push(30);

    // FIFO：先 push 的先被偷出
    assert_eq!(q.steal(), Steal::Success(10));
    assert_eq!(q.steal(), Steal::Success(20));
    assert_eq!(q.steal(), Steal::Success(30));
    assert_eq!(q.steal(), Steal::Empty);

    println!("len before push = {}", q.len()); // 0
    q.push(99);
    assert!(!q.is_empty());
    println!("len after push  = {}", q.len()); // 1
}
```

**需要观察的现象**：偷取顺序 `10, 20, 30` 与 push 顺序一致（FIFO）；空队列 `steal` 返回 `Empty` 而非 `Retry`（单线程无竞争不会伪失败）。

**预期结果**：两次 `len` 打印分别为 `0` 和 `1`。`steal` 在空时稳定返回 `Empty`。

#### 4.3.5 小练习与答案

**练习 1**：`Injector` 和 `Stealer` 都有 `steal()`，二者返回类型相同。为什么 `Injector` 不需要先有一个 `Worker`？

**答案**：`Injector` 本身就是完整的共享队列（自带 `head`/`tail`），不依附于任何 `Worker`；而 `Stealer` 只是某个 `Worker` 的「偷取视图」，必须由 `Worker::stealer()` 派生。

**练习 2**：把 `Injector` 换成普通 `Mutex<VecDeque<T>>` 也能实现全局 FIFO，为什么调度器更愿意用 `Injector`？

**答案**：`Injector` 是无锁（lock-free）实现，热路径上不持有互斥锁、不阻塞，多线程偷取时延迟更低、伸缩性更好；代价是实现复杂（block 链表、CAS、内存回收）。这正是 crossbeam-deque 存在的意义。

---

### 4.4 Steal 组合子与 find_task 回退链

#### 4.4.1 概念说明

有了 `Steal` 三态、`Stealer`、`Injector`，最后一个问题：**一个空闲线程如何按优先级依次尝试多个任务来源**？这就是 `find_task` 模式。

它的优先级链是：

```text
1. 本地 pop            （Worker::pop，返回 Option<T>）
2. 全局批量偷并弹一个    （Injector::steal_batch_and_pop，返回 Steal<T>）
3. 遍历别的线程的 stealer（Stealer::steal，返回 Steal<T>）
```

难点在于：步骤 2、3 都可能返回 `Retry`，而且我们想把「2 失败就试 3」「3 里任意一个成功就成功」「全失败但有人要重试就整体重试」这些逻辑写得简洁又正确。这正是 `Steal::or_else` 和 `FromIterator` 的用武之地。

#### 4.4.2 核心流程

`find_task` 的回退链可以这样读：

```text
local.pop()  ──Some(t)──→  直接返回 Some(t)
       │
       None
       ▼
repeat_with(|| {
   global.steal_batch_and_pop(local)      // 先试全局
       .or_else(|| stealers.iter()        // 失败就遍历 stealer 列表
           .map(|s| s.steal())
           .collect())                    // 任意成功→Success；任一 Retry→Retry
})
.find(|s| !s.is_retry())                  // 跳过所有 Retry，停在首个 Empty/Success
.and_then(|s| s.success())                // Success→Some(task)，Empty→None
```

三个组合子各司其职：

- `or_else`：把「全局偷」和「遍历 stealer」串成一条**带 Retry 传播的回退链**。
- `collect`（`FromIterator`）：把一组 `Stealer::steal` 结果聚合成单个 `Steal`。
- `find(|s| !s.is_retry())` 配合 `repeat_with`：**自动重试**直到结果不再是 `Retry`（是 `Success` 或 `Empty`）。
- `success()`：把最终的 `Steal<T>` 收拢成 `Option<T>`，与 `local.pop()` 的返回类型对齐。

#### 4.4.3 源码精读

这段范本完整出现在 crate 级文档里，见 [src/lib.rs:56-75](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/lib.rs#L56-L75)（中文说明：`find_task` 的 doctest 示例，浓缩了整个 work-stealing 回退链）：

```rust
fn find_task<T>(
    local: &Worker<T>,
    global: &Injector<T>,
    stealers: &[Stealer<T>],
) -> Option<T> {
    // 1. 先从本地队列 pop
    local.pop().or_else(|| {
        // 2. 本地为空，去别处找
        iter::repeat_with(|| {
            // 2a. 从全局批量偷并弹一个
            global.steal_batch_and_pop(local)
                // 2b. 否则从其他线程偷一个
                .or_else(|| stealers.iter().map(|s| s.steal()).collect())
        })
        // 只要还需要重试就继续循环
        .find(|s| !s.is_retry())
        // 取出偷到的任务
        .and_then(|s| s.success())
    })
}
```

注意一个关键细节：`repeat_with` 产生的是**无限迭代器**，但 `.find(|s| !s.is_retry())` 会在第一个非 `Retry` 结果上短路停止。当全局与所有 stealer 都空时，`or_else`/`collect` 会给出 `Empty`（非 `Retry`），`find` 立即返回，不会死循环。这就是「`Retry` 与 `Empty` 必须区分」在实际代码里的回报。

> 提示：`steal_batch_and_pop` 偷多少个任务是「未指定」的实现细节（约半数、上限 `MAX_BATCH`），所以 `find_task` 一次调用可能既弹回一个任务、又顺带把一批任务搬进了 `local`——后续几次 `local.pop()` 都能命中，分摊了偷取成本。

#### 4.4.4 代码实践

**实践目标**：用「源码阅读 + 手动追踪」的方式，确认你对回退链每一步返回值的理解。

**操作步骤**：

1. 打开 [src/lib.rs:56-75](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/lib.rs#L56-L75) 的 `find_task`。
2. 假设当前状态：`local` 非空（有一个任务 `t0`），`global` 空，`stealers` 空。
3. 手动追踪：`local.pop()` 返回 `Some(t0)`，`or_else` 的闭包**根本不会执行**（因为 `Option::or_else` 在 `Some` 时直接返回）。
4. 再假设：`local` 空，`global` 有 1 个任务，`stealers` 为空，且 `global.steal_batch_and_pop` 这次返回 `Success(g0)`。追踪 `or_else`：左侧是 `Success`，直接返回 `Success(g0)`，**不会**遍历 `stealers`。
5. 最后假设：`local` 空、`global` 空、`stealers` 中第一个返回 `Retry`、第二个返回 `Empty`。追踪：`global...` 给 `Empty` → `or_else` 执行右侧 → `collect` 聚合 `[Retry, Empty]` → 结果 `Retry` → `repeat_with` 再来一轮……直到某轮全局/stealer 不再 `Retry`。

**需要观察的现象**：你能准确说出每一支 `Option`/`Steal` 的取值，以及 `or_else`、`collect`、`find` 各自在哪一步短路。

**预期结果**：追踪结论与 4.1.2 的真值表、4.4.2 的流程图完全吻合。这一步是「待本地验证」的纸面练习，目的是为下一节的综合实践打底。

#### 4.4.5 小练习与答案

**练习 1**：如果把 `.find(|s| !s.is_retry())` 改成 `.find(|s| s.is_success())`，会有什么后果？

**答案**：当所有来源都空时，结果会是 `Empty`，`find` 会跳过它继续无限重试，`repeat_with` 永不停止 → 死循环。原写法用 `!is_retry()` 保证遇到 `Empty` 也停下来，再由 `success()` 把 `Empty` 映射成 `None`。

**练习 2**：`stealers.iter().map(|s| s.steal()).collect()` 这一行，`collect` 的目标类型是什么？

**答案**：`Steal<T>`。因为 `Steal<T>` 实现了 `FromIterator<Self>`（见 [src/deque.rs:2213-2233](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-deque/src/deque.rs#L2213-L2233)），所以迭代器 `collect` 出来就是单个 `Steal<T>`，可直接喂给 `or_else`。

---

## 5. 综合实践

**任务**：实现 `find_task`，并写一个**单线程**驱动：把若干任务 `push` 进 `Injector`，然后反复调用 `find_task` 把它们全部取回，验证「无丢失、无重复」。

**操作步骤**：

1. 新建二进制 crate，`Cargo.toml` 加依赖：

   ```toml
   [dependencies]
   crossbeam-deque = "0.8"
   ```

2. 把 `src/lib.rs` 中的 `find_task` 抄进 `main.rs`，并写驱动：

   ```rust
   // 示例代码：单线程驱动 find_task 工作流
   use crossbeam_deque::{Injector, Stealer, Worker};
   use std::iter;

   fn find_task<T>(
       local: &Worker<T>,
       global: &Injector<T>,
       stealers: &[Stealer<T>],
   ) -> Option<T> {
       local.pop().or_else(|| {
           iter::repeat_with(|| {
               global
                   .steal_batch_and_pop(local)
                   .or_else(|| stealers.iter().map(|s| s.steal()).collect())
           })
           .find(|s| !s.is_retry())
           .and_then(|s| s.success())
       })
   }

   fn main() {
       let local = Worker::new_fifo();
       let global = Injector::new();
       let stealers: Vec<Stealer<i32>> = vec![]; // 单线程，没有别的 worker

       for i in 0..5i32 {
           global.push(i);
       }

       let mut got = Vec::new();
       while let Some(t) = find_task(&local, &global, &stealers) {
           got.push(t);
       }

       // 关键不变量：5 个任务一个不少、一个不多地被取回
       let mut sorted = got.clone();
       sorted.sort();
       assert_eq!(sorted, vec![0, 1, 2, 3, 4]);
       println!("retrieved (in order): {:?}", got);
       println!("count = {}", got.len());
   }
   ```

3. `cargo run`。

**需要观察的现象**：

- 程序正常退出（不死循环）——这验证了「全局空 + stealers 空 → `Empty` → `find` 停止 → `success()` 给 `None` → 循环结束」。
- 排序后正好是 `[0,1,2,3,4]`，证明无丢失、无重复。
- 打印的 `got` 顺序可能因 `steal_batch_and_pop` 的批量大小（「未指定」的实现细节）而呈现一定批量特征——这是**待本地验证**的观察点：你看到的顺序未必就是 `[0,1,2,3,4]`，但元素集合一定一致。

**预期结果**：打印类似 `count = 5`，排序断言通过。若你把 `local` 换成 `new_lifo()`，`got` 的顺序会变化（批量搬入 LIFO 后再 pop 会反转），但 `sorted` 仍是 `[0,1,2,3,4]`。

> 进阶自测：把 `stealers` 填上一个真实 `Worker` 的 stealer 并预先 push 几个任务，观察 `find_task` 是否能在 `global` 空后继续从 `stealers` 偷到任务。

---

## 6. 本讲小结

- 偷取操作的返回值是 `Steal<T>` 而非 `Option<T>`：三态 `Empty / Success / Retry` 中，`Retry` 表达「伪失败、请重试」，是与 `Option` 的本质区别。
- `Stealer` 是 `Worker` 的「可共享偷取视图」：共享同一 `Arc<Inner>`，`Send + Sync + Clone`，而 `Worker` 是单线程私有的 `!Send + !Sync`。
- `Injector` 是全局唯一的 FIFO、MPMC 任务入口，`push` 不返回值，`steal` 同样返回 `Steal<T>`——三者（`Stealer`/`Injector`）共用同一套 `Steal` 语义，是组合子能统一处理的基础。
- `Steal::or_else` 把多个偷取串成带 `Retry` 传播的回退链；`FromIterator`（`collect`）把一组结果聚合；`is_retry` + `repeat_with` + `find` 实现「自动重试到非 Retry」。
- `find_task` 范本（`local.pop → global.steal_batch_and_pop → stealers.steal`）是整个 work-stealing 调度循环的缩影，下一讲起我们将逐层拆开它的内部实现。

## 7. 下一步学习建议

本讲把三个公开类型「用起来」了，但刻意回避了它们**怎么实现**。接下来建议：

- **进入 u2（Chase-Lev 实现）**：从 u2-l1 的 `Buffer` / `Inner` 环形缓冲区数据结构开始，理解 `Stealer::steal` 里那句 `compare_exchange` 到底在和谁竞争、为什么读槽位后还要再校验 buffer。
- **重点先看 u2-l3（单任务偷取）**：它会逐行拆解本讲 4.2.3 里被我们「裁剪掉」的 `epoch::pin`、`SeqCst fence`、CAS 失败返回 `Retry` 的全过程。
- **u3 再回头啃 `Injector`**：本讲把 `Injector` 当黑盒用了，u3-l1 ～ u3-l4 会打开它的 block 链表、`Slot` 状态机与跨块推进机制。
- **u4-l5 综合实战**：届时你会用真正的多线程 scoped threads 把 `find_task` 跑成一个迷你 work-stealing 调度器，本讲的回退链就是它的核心。
