# 快速上手：HeapRb 的创建、拆分与 push/pop

## 1. 本讲目标

本讲带你跑通 ringbuf 最经典的一条链路：**创建一个缓冲区 → 拆成生产端和消费端 → 写入 → 读取**。

读完本讲，你应当能够：

- 用 `HeapRb::new(capacity)` 在堆上创建一个指定容量的环形缓冲区。
- 用 `split()` 把缓冲区拆成一对 `Producer`（生产端）和 `Consumer`（消费端）。
- 用 `try_push` 写入元素，并能解释「缓冲区满时返回 `Err(元素)`」的语义。
- 用 `try_pop` 读取元素，验证先进先出（FIFO）顺序，并解释「缓冲区空时返回 `None`」的语义。

本讲只关心「能不能跑起来、行为是什么」，**不**深入无锁原子、`MaybeUninit` 内存安全等底层原理（那是后续单元的主题）。

---

## 2. 前置知识

在开始前，先用大白话建立三个直觉（上一讲已经铺垫过，这里再强化）：

1. **环形缓冲区（ring buffer）是什么**
   它是一块固定大小的内存，配合 `read`（读）和 `write`（写）两个「指针」反复复用。写端把新元素放进 `write` 指向的槽，读端从 `read` 指向的槽取走最旧的元素。因为内存是「环形」复用的，所以不会像 `Vec` 那样无限增长。

2. **SPSC 是什么**
   单生产者单消费者（Single-Producer Single-Consumer）。规则是：**至多只有一个写端、一个读端**同时操作。ringbuf 在运行时用「hold 标志」强制这个规则。本讲我们在单线程里用，所以感受不到并发，但要记住这套设计。

3. **无锁（lock-free）是什么**
   操作要么立刻成功，要么立刻失败，**绝不阻塞等待**。所以写入方法叫 `try_push`（「尝试 push」），读出方法叫 `try_pop`：满了就退回元素，空了就返回 `None`，不会卡住你的线程。

> 关键术语对照：本讲出现的「容量 capacity」= 缓冲区最多能放多少个元素；「生产端 Producer」= 写入方；「消费端 Consumer」= 读取方；「FIFO」= 先进先出，先写进去的先被读出来。

---

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [examples/simple.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/examples/simple.rs) | 官方最小示例，串联 create→split→push→pop 全流程，是本讲的「主干」。 |
| [src/alias.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/alias.rs) | 类型别名，定义了 `HeapRb`、`HeapProd`、`HeapCons` 这些常用名字。 |
| [src/rb/macros.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/macros.rs) | `rb_impl_init!` 宏，为 `HeapRb` 生成 `new(capacity)` 构造器。 |
| [src/storage.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/storage.rs) | `Heap<T>` 存储后端，`HeapRb` 的元素就放在它分配的堆内存里。 |
| [src/traits/split.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/split.rs) | `Split` trait，定义「把缓冲区拆成两端」的接口。 |
| [src/rb/shared.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs) | `SharedRb` 实现 `Split`，给出 `split()` 的真实行为。 |
| [src/traits/producer.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/producer.rs) | `Producer` trait，定义 `try_push`。 |
| [src/traits/consumer.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/consumer.rs) | `Consumer` trait，定义 `try_pop`。 |
| [src/traits/observer.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/observer.rs) | `Observer` trait，定义 `is_empty` / `is_full` 等状态判断。 |

---

## 4. 核心概念与源码讲解

先看一眼本讲要逐行理解的官方示例，它只有十几行，却是整篇讲义的骨架：

```rust
use ringbuf::{traits::*, HeapRb};

fn main() {
    let rb = HeapRb::<i32>::new(2);
    let (mut prod, mut cons) = rb.split();

    prod.try_push(0).unwrap();
    prod.try_push(1).unwrap();
    assert_eq!(prod.try_push(2), Err(2));   // 满了，元素 2 被退回

    assert_eq!(cons.try_pop().unwrap(), 0); // 取走最旧的 0

    prod.try_push(2).unwrap();              // 现在有空位了，2 写入成功

    assert_eq!(cons.try_pop().unwrap(), 1); // FIFO：1 比 2 先进，先出
    assert_eq!(cons.try_pop().unwrap(), 2);
    assert_eq!(cons.try_pop(), None);       // 空了
}
```

> 来源：[examples/simple.rs:1-18](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/examples/simple.rs#L1-L18)

我们按「创建 → 拆分 → 写入 → 读取」四个最小模块拆开讲解。

> 关于 `use ringbuf::{traits::*, HeapRb};`：`split`、`try_push`、`try_pop` 这些方法都定义在 `ringbuf::traits` 模块下的 trait（`Split` / `Producer` / `Consumer`）里，所以必须把这些 trait 引入作用域才能调用。`HeapRb` 则是具体的缓冲区类型。这一行是几乎所有 ringbuf 程序的开头。

### 4.1 创建堆缓冲区 HeapRb

#### 4.1.1 概念说明

`HeapRb<T>` 是「在堆上分配的环形缓冲区」，存放类型为 `T` 的元素。它是 ringbuf 官方推荐的默认选择（见 README：「Recommended for use in most cases」）。

它其实不是「新发明」的类型，而是一个**类型别名**：

```rust
pub type HeapRb<T> = SharedRb<Heap<T>>;
```

> 来源：[src/alias.rs:27](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/alias.rs#L27)

读法是：`HeapRb` = `SharedRb`（可在多线程间共享的环形缓冲区骨架）+ `Heap<T>`（堆存储后端）。`Heap<T>` 决定了「元素放在哪」——堆内存。换个存储后端就能得到别的缓冲区（比如 `StaticRb` 用静态数组），这些在后续单元讲。

**容量（capacity）在创建时就固定下来，之后不会变。**

#### 4.1.2 核心流程

创建一个 `HeapRb<T>` 的过程：

1. 调用 `HeapRb::<T>::new(capacity)`，传入期望容量。
2. 内部用 `Heap::<T>::new(capacity)` 在堆上分配一块能放 `capacity` 个 `MaybeUninit<T>` 的连续内存。
3. 把这块内存交给 `SharedRb`，并把 `read`、`write` 两个索引都初始化为 0（表示空缓冲区）。

> 注意：`new(0)` 会 **panic**（容量不能为 0）；分配失败也会 panic。需要避免 panic 可改用 `try_new`（返回 `Result`）。

#### 4.1.3 源码精读

`HeapRb::new` 并不是手写的，而是由 `rb_impl_init!` 宏为 `SharedRb`（`HeapRb` 的真身）统一生成的：

```rust
impl<T> $type<crate::storage::Heap<T>> {
    /// Creates a new instance of a ring buffer.
    ///
    /// *Panics if allocation failed or `capacity` is zero.*
    pub fn new(capacity: usize) -> Self {
        unsafe { Self::from_raw_parts(crate::storage::Heap::<T>::new(capacity), usize::default(), usize::default()) }
    }
    ...
}
```

> 来源：[src/rb/macros.rs:17-23](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/macros.rs#L17-L23)。`$type` 在 `HeapRb` 这里被替换为 `SharedRb`，`usize::default()` 即 `0`，分别作为初始 `read`、`write` 索引。

而 `Heap::new` 真正负责分配堆内存：

```rust
impl<T> Heap<T> {
    pub fn new(capacity: usize) -> Self {
        let mut data = Vec::<MaybeUninit<T>>::with_capacity(capacity);
        // 用 set_len + boxed slice 保证长度精确等于 capacity
        unsafe { data.set_len(capacity) };
        Self::from(data.into_boxed_slice())
    }
}
```

> 来源：[src/storage.rs:155-163](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/storage.rs#L155-L163)

> 这里出现 `MaybeUninit<T>`，意思是「这块内存尚未初始化」。缓冲区一开始所有槽都是空的、未初始化的，只有真正 `try_push` 进去的槽才算「有数据」。`MaybeUninit` 的细节属于 unsafe 内存安全范畴，本讲只需知道「它代表还没填的槽」即可。

#### 4.1.4 代码实践

**目标**：体会「容量在创建时固定」和「容量为 0 会 panic」。

**步骤**（在仓库根目录）：

1. 用 `cargo run --example simple` 先确认环境正常。
2. 自己写一个临时 example（或改本地副本观察），尝试 `HeapRb::<i32>::new(3)`、`new(10)`，并用 `prod.capacity()`（来自 `Observer` trait）打印容量。

**示例代码**（非项目原有，仅作演示）：

```rust
use ringbuf::{traits::*, HeapRb};

fn main() {
    let rb = HeapRb::<i32>::new(3);
    let (prod, _cons) = rb.split();
    println!("capacity = {}", prod.capacity()); // 期望输出 3
}
```

**需要观察的现象**：`capacity()` 返回的值等于你传给 `new` 的值。

**预期结果**：打印 `capacity = 3`。

> 「`new(0)` 会 panic」属于破坏性验证，可读源码注释确认，不必真的去触发。

#### 4.1.5 小练习与答案

**练习 1**：`HeapRb<T>` 的「真身」是哪个类型？存储后端是什么？
**答案**：真身是 `SharedRb<Heap<T>>`，存储后端是 `Heap<T>`（堆内存）。

**练习 2**：为什么 `HeapRb::new` 需要 `alloc` feature？
**答案**：因为 `Heap` 后端要在堆上分配内存（内部用了 `Vec` / `Box<[MaybeUninit<T>]>`），这依赖 `alloc`。去掉 `alloc` 后 `HeapRb` 不可用，只能改用 `StaticRb`（静态数组）。

---

### 4.2 split：把缓冲区拆成 HeapProd 与 HeapCons

#### 4.2.1 概念说明

一个刚创建好的 `HeapRb` 还不能直接用来跨「两端」读写——它是一整个对象。我们需要把它**拆（split）成两半**：

- **Producer（生产端）**：负责写入元素。
- **Consumer（消费端）**：负责读取元素。

`split()` 的签名是 `fn split(self) -> (Prod, Cons)`——注意它**拿走 `self` 的所有权**。对于 `HeapRb`，拆分后两端会各自持有一个指向同一块底层缓冲区的 `Arc`（原子引用计数智能指针），因此两端可以分别移动到不同线程里使用（虽然本讲在单线程演示）。

拆分得到的两个类型也有别名：

```rust
pub type HeapProd<T> = CachingProd<Arc<HeapRb<T>>>;
pub type HeapCons<T> = CachingCons<Arc<HeapRb<T>>>;
```

> 来源：[src/alias.rs:31](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/alias.rs#L31) 与 [src/alias.rs:35](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/alias.rs#L35)

也就是说，对 `HeapRb<i32>` 调 `split()`，返回的是 `(HeapProd<i32>, HeapCons<i32>)`。`CachingProd` / `CachingCons` 是包装器（决定何时与底层同步），本讲把它当作「带 push/pop 方法的生产/消费端」即可，内部机制在第四单元详讲。

#### 4.2.2 核心流程

```text
rb.split()
   │  （rb 被移动，所有权交出）
   ▼
Arc::new(rb)          // 包一层 Arc，让两端能共享所有权
   │
   ▼  再调 Arc 上的 split()
(CachingProd::new(arc.clone()), CachingCons::new(arc))
   │                        │
   ▼                        ▼
HeapProd                 HeapCons
```

两端通过同一个 `Arc<HeapRb<T>>` 看到同一块内存，于是写端写入的元素，读端能看到，反之亦然。

#### 4.2.3 源码精读

`Split` trait 定义了「拆分」这件事的接口，核心就一行：

```rust
pub trait Split {
    type Prod: Producer;
    type Cons: Consumer;
    fn split(self) -> (Self::Prod, Self::Cons);
}
```

> 来源：[src/traits/split.rs:3-12](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/split.rs#L3-L12)。它要求 `Prod` 实现 `Producer`、`Cons` 实现 `Consumer`。

`SharedRb`（也就是 `HeapRb`）对它的实现：

```rust
#[cfg(feature = "alloc")]
impl<S: Storage> Split for SharedRb<S> {
    type Prod = CachingProd<Arc<Self>>;
    type Cons = CachingCons<Arc<Self>>;

    fn split(self) -> (Self::Prod, Self::Cons) {
        Arc::new(self).split()
    }
}
```

> 来源：[src/rb/shared.rs:154-162](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs#L154-L162)。它先把 `self` 包进 `Arc`，再委托给 `Arc<SharedRb>` 上的 `split()`（见 [src/rb/shared.rs:164-171](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs#L164-L171)），后者用同一个 `Arc` 的克隆分别构造生产端和消费端。

#### 4.2.4 代码实践

**目标**：确认 `split()` 之后原变量已被移动，两端共享同一缓冲区。

**步骤**：

1. 照搬 simple.rs 的前两行，在 `split()` 之后再尝试使用 `rb`（例如 `rb.capacity()`）。
2. 观察编译器报错。

**示例代码**（非项目原有，故意触发编译错误以加深理解）：

```rust
use ringbuf::{traits::*, HeapRb};

fn main() {
    let rb = HeapRb::<i32>::new(2);
    let (prod, cons) = rb.split();
    // let _ = rb.capacity(); // 取消注释会编译失败：rb 已被 split 移走
    println!("split ok");
    let _ = (prod, cons);
}
```

**需要观察的现象**：`split()` 之后 `rb` 不再可用（所有权已转移）。

**预期结果**：保留注释时正常编译运行，打印 `split ok`；取消注释时报「value borrowed here after move」之类的错误。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `HeapRb::split` 要用 `Arc`，而不是直接给两端各一个普通引用 `&HeapRb`？
**答案**：因为 `split(self)` 拿走了所有权，没有外部的「拥有者」来提供引用；用 `Arc` 让两端各自持有一份引用计数，缓冲区的生命周期由两端共同管理，任何一端都可以独立存活（甚至跨线程移动）。引用 `&` 的方案对应另一个接口 `split_ref`（见 [src/traits/split.rs:14-27](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/split.rs#L14-L27)），它借用 `&mut self`，不转移所有权。

**练习 2**：`split()` 返回的元组里，第一个元素是写端还是读端？
**答案**：第一个是生产端 `Prod`（写），第二个是消费端 `Cons`（读）。记忆法：先生产、后消费。

---

### 4.3 try_push：写入与「满则拒绝」

#### 4.3.1 概念说明

`try_push` 是 `Producer` trait 的方法，签名是：

```rust
fn try_push(&mut self, elem: Self::Item) -> Result<(), Self::Item>
```

> 来源：[src/traits/producer.rs:60](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/producer.rs#L60)

返回值是关键：

- `Ok(())`：写入成功。
- `Err(elem)`：**缓冲区已满**，元素 `elem` 原样退回给你。

这个设计的好处是：**写入失败时元素不会丢失**——它被放进 `Err` 里还给你，你可以稍后再试或做别的处理。这正是「无锁」的体现：满了不等，立刻告诉你失败。

#### 4.3.2 核心流程

`try_push` 内部三步：

1. 检查 `is_full()`：缓冲区满了吗？
2. 若不满：把元素写进 `write` 索引指向的空槽，再把 `write` 索引前进一步，返回 `Ok(())`。
3. 若满：直接返回 `Err(elem)`，什么都不改。

`is_full()` 的判定基于 `read` / `write` 两个索引（具体是 `vacant_len() == 0`）。索引如何区分空和满是下一单元的重点（双索引 + `2*capacity` 模运算），本讲先记住结论：**元素数等于容量时即为满**。

#### 4.3.3 源码精读

`try_push` 的默认实现：

```rust
fn try_push(&mut self, elem: Self::Item) -> Result<(), Self::Item> {
    if !self.is_full() {
        unsafe {
            self.vacant_slices_mut().0.get_unchecked_mut(0).write(elem);
            self.advance_write_index(1)
        };
        Ok(())
    } else {
        Err(elem)
    }
}
```

> 来源：[src/traits/producer.rs:60-70](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/producer.rs#L60-L70)。`vacant_slices_mut()` 拿到空闲槽，`.0...write(elem)` 把元素写进第一个空槽，`advance_write_index(1)` 推进写索引「提交」这次写入。

`is_full` 与空闲长度来自 `Observer` trait：

```rust
fn is_full(&self) -> bool {
    self.vacant_len() == 0
}
```

> 来源：[src/traits/observer.rs:74-76](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/observer.rs#L74-L76)

回到 simple.rs，容量是 2，所以前两次 push 成功，第三次返回被退回的元素：

```rust
prod.try_push(0).unwrap();           // Ok
prod.try_push(1).unwrap();           // Ok
assert_eq!(prod.try_push(2), Err(2)); // 满，2 被退回
```

> 来源：[examples/simple.rs:7-9](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/examples/simple.rs#L7-L9)

#### 4.3.4 代码实践

**目标**：亲手验证「满则退回元素」，并观察「取走一个后又能继续写」。

**步骤**：

1. 创建容量 2 的 `HeapRb`，连续 `try_push(0)`、`try_push(1)`、`try_push(2)`，用 `println!("{:?}", ...)` 打印每次返回值。
2. 用消费端 `try_pop()` 取走 1 个，再 `try_push(2)`，打印返回值。

**需要观察的现象**：第 3 次 `try_push` 打印 `Err(2)`；pop 之后再 push 打印 `Ok(())`。

**预期结果**：

```text
try_push(0) = Ok(())
try_push(1) = Ok(())
try_push(2) = Err(2)
after pop, try_push(2) = Ok(())
```

#### 4.3.5 小练习与答案

**练习 1**：为什么 `try_push` 满了不阻塞等待，而是返回 `Err`？
**答案**：ringbuf 是无锁设计，操作要么立刻成功要么立刻失败，绝不阻塞线程。想要「满了就等」的语义，要用派生 crate `async-ringbuf`（await）或 `ringbuf-blocking`（阻塞）。

**练习 2**：`try_push` 返回 `Err(elem)` 时，元素有没有真的进入缓冲区？
**答案**：没有。失败路径（`else { Err(elem) }`）完全不碰底层内存和索引，元素原封不动还给你。

---

### 4.4 try_pop：读取、FIFO 顺序与「空则返回 None」

#### 4.4.1 概念说明

`try_pop` 是 `Consumer` trait 的方法，签名是：

```rust
fn try_pop(&mut self) -> Option<Self::Item>
```

> 来源：[src/traits/consumer.rs:106](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/consumer.rs#L106)

返回值：

- `Some(item)`：取走**最旧**的一个元素并返回。
- `None`：缓冲区为空。

由于总是从「最旧」的一端取，`try_pop` 天然保证 **FIFO（先进先出）**：先 `push` 进来的元素，一定先被 `pop` 出去。

#### 4.4.2 核心流程

`try_pop` 内部三步：

1. 检查 `is_empty()`：缓冲区空吗？
2. 若不空：从 `read` 索引指向的槽读出（搬走）最旧元素，再把 `read` 索引前进一步，返回 `Some(item)`。
3. 若空：返回 `None`。

`is_empty()` 的判定是 `read_index() == write_index()`。

#### 4.4.3 源码精读

`try_pop` 的默认实现：

```rust
fn try_pop(&mut self) -> Option<Self::Item> {
    if !self.is_empty() {
        let elem = unsafe { self.occupied_slices().0.get_unchecked(0).assume_init_read() };
        unsafe { self.advance_read_index(1) };
        Some(elem)
    } else {
        None
    }
}
```

> 来源：[src/traits/consumer.rs:106-114](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/consumer.rs#L106-L114)。`occupied_slices().0` 是「已写入区域的第一段」，`.0...assume_init_read()` 取出最旧元素，`advance_read_index(1)` 推进读索引，释放该槽。

`is_empty` 来自 `Observer` trait：

```rust
fn is_empty(&self) -> bool {
    self.read_index() == self.write_index()
}
```

> 来源：[src/traits/observer.rs:66-68](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/observer.rs#L66-L68)

回到 simple.rs 的读取部分，能清楚看到 FIFO 与「空返回 None」：

```rust
assert_eq!(cons.try_pop().unwrap(), 1); // 先于 2 写入的 1，先被取出
assert_eq!(cons.try_pop().unwrap(), 2);
assert_eq!(cons.try_pop(), None);        // 空
```

> 来源：[examples/simple.rs:15-17](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/examples/simple.rs#L15-L17)

#### 4.4.4 代码实践

**目标**：验证 FIFO 顺序与空返回 `None`。

**步骤**：

1. 创建容量 3 的 `HeapRb`，依次 `try_push(10)`、`try_push(20)`、`try_push(30)`。
2. 连续 `try_pop()` 四次，打印每次结果。

**需要观察的现象**：取出顺序是 `10 → 20 → 30`（与写入顺序一致），第四次是 `None`。

**预期结果**：

```text
pop = Some(10)
pop = Some(20)
pop = Some(30)
pop = None
```

#### 4.4.5 小练习与答案

**练习 1**：如果先 push 了 `3, 1, 2`，再连续 pop，取出顺序是什么？
**答案**：`3, 1, 2`。FIFO 保证按写入顺序出队，与值的大小无关。

**练习 2**：`try_pop` 取走元素后，那个槽立刻就能被再次写入吗？
**答案**：可以。`advance_read_index(1)` 释放了该槽，它重新变为空闲；之后写端的 `vacant_len` 会增加，`try_push` 就能复用这个槽。这也是「环形复用」的体现。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面的任务（这是本讲规格要求的实践）。

**任务**：照 `examples/simple.rs` 的写法，写一个程序：创建**容量为 3** 的 `HeapRb<i32>`，`split` 后连续 `try_push(0)`、`try_push(1)`、`try_push(2)`、`try_push(3)`（共 4 个），打印每次返回值并解释**为什么第 4 次失败**；然后 `try_pop` 验证 FIFO 顺序。

**示例代码**（非项目原有；建议放进仓库的 `examples/` 或独立小工程运行）：

```rust
use ringbuf::{traits::*, HeapRb};

fn main() {
    let rb = HeapRb::<i32>::new(3);
    let (mut prod, mut cons) = rb.split();

    // 连续尝试写入 0,1,2,3 四个元素
    for v in 0..4 {
        println!("try_push({v}) = {:?}", prod.try_push(v));
    }

    // 读出全部，验证 FIFO
    while let Some(item) = cons.try_pop() {
        println!("try_pop() = {item}");
    }
    println!("try_pop() on empty = {:?}", cons.try_pop());
}
```

**操作步骤**：

1. 在仓库根目录用 `cargo run --example simple` 确认环境可用。
2. 把上面的代码存为一个新的 example（例如 `examples/quickstart.rs`），或放进自己的小 crate。
3. 用 `cargo run --example quickstart`（命名自拟）运行。

**需要观察的现象与预期结果**：

```text
try_push(0) = Ok(())
try_push(1) = Ok(())
try_push(2) = Ok(())
try_push(3) = Err(3)   ← 第 4 次失败
try_pop() = 0
try_pop() = 1
try_pop() = 2
try_pop() on empty = None
```

**解释为什么第 4 次失败**：容量是 3，前 3 次 `try_push` 已经把 3 个槽填满，`is_full()` 为真；第 4 次 `try_push(3)` 走满分支，返回 `Err(3)`，元素 3 原样退回，**没有进入缓冲区**。因此后续 `try_pop` 只能取到 `0, 1, 2`，符合 FIFO，且最后为空返回 `None`。

> 注意：往仓库 `examples/` 加文件属于「新增源码」。本讲义的约定是不修改项目源码；如果你只是本地实验，请在一个独立的目录或临时副本里操作，实验后删除，避免污染仓库。

---

## 6. 本讲小结

- `HeapRb<T>` 是堆分配的环形缓冲区，真身是 `SharedRb<Heap<T>>`，容量在 `new(capacity)` 时固定（容量为 0 会 panic）。
- `split()` **拿走所有权**，把缓冲区包进 `Arc`，返回共享同一底层的 `(HeapProd, HeapCons)`；调用前需 `use ringbuf::{traits::*, HeapRb}` 引入相关 trait。
- `try_push(elem)` 返回 `Result<(), Item>`：成功为 `Ok(())`，**满时返回 `Err(elem)`**，元素不丢失、不阻塞。
- `try_pop()` 返回 `Option<Item>`：取走最旧元素为 `Some`，**空时返回 `None`**，天然保证 FIFO。
- 「满则退回、空则 None」正是无锁 SPSC 的行为契约；想要「等待」语义需要派生 crate（async / blocking）。
- 本讲全程在单线程内演示，但 `HeapRb` 通过 `Arc` 共享，本质上已为多线程 SPSC 做好准备（并发安全的细节在第五单元）。

---

## 7. 下一步学习建议

本讲你跑通了「创建→拆分→push→pop」，但还有几个「为什么」没有展开：

- `read` / `write` 两个索引到底怎么区分「空」和「满」而不浪费槽位？→ 下一讲 **u2-l1 核心原理：双索引与 2\*capacity 模运算**。
- 除了堆存储，还有哪些存储后端，怎么在 `no_std` / 无 `alloc` 下用？→ **u2-l2 Storage 抽象**。
- `split` 和 `split_ref` 有什么区别？→ **u2-l4 Split 与 SplitRef**。

建议先读 [src/traits/observer.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/observer.rs) 里 `occupied_len` / `vacant_len` 的实现，带着「它怎么算出剩余空间」的疑问进入下一讲。
