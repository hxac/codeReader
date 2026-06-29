# Split 与 SplitRef：把缓冲区拆成生产/消费两端

## 1. 本讲目标

学完本讲后，你应该能够：

1. 说清楚「为什么一个环形缓冲区需要被拆成 Producer 与 Consumer 两个句柄」，以及拆分这件事在所有权层面要解决的核心矛盾。
2. 区分 `Split::split(self)`（消耗所有权，返回基于 `Rc`/`Arc` 的两端）与 `SplitRef::split_ref(&mut self)`（借用 `&mut self`，返回引用两端）这两种拆分方式。
3. 解释为什么 `LocalRb` 的 split 用 `Rc`、`SharedRb` 的 split 用 `Arc`，以及为什么 `split()` 依赖 `alloc` 而 `split_ref()` 不依赖。
4. 根据自己的场景（是否拥有所有权、是否需要跨线程、是否在 `no_std`/无堆环境）正确选择拆分方式，并说清 `examples/global_static.rs` 为什么只能用 `split_ref`。

---

## 2. 前置知识

在继续之前，请确认你已经理解下面这些来自前几讲的概念：

- **环形缓冲区的双索引**：缓冲区内部用 `read`、`write` 两个索引描述「最旧元素位置」与「下一个空槽位置」。这是 [u2-l1](u2-l1-indices-modular-arithmetic.md) 的内容。
- **LocalRb 与 SharedRb**：`LocalRb` 单线程、用 `Cell` 存索引、默认产出 `Direct`（即时同步）包装；`SharedRb` 多线程、用 `CachePadded<AtomicUsize>` 存索引、默认产出 `Caching`（按需同步）包装。这是 [u2-l3](u2-l3-local-shared-and-aliases.md) 的内容。
- **HeapRb / StaticRb 是类型别名**：`HeapRb<T> = SharedRb<Heap<T>>`、`StaticRb<T, N> = SharedRb<Array<T, N>>`。
- **Producer / Consumer / Observer trait**：`try_push`、`try_pop`、`is_empty` 等方法都来自这些 trait，调用前需 `use ringbuf::{traits::*, ...}`。
- **hold 标志**（粗略了解即可，细节留待 [u5-l2](u5-l2-hold-flags-spsc-invariant.md)）：缓冲区内部有两个布尔标志 `read_held`/`write_held`，运行时强制「至多一个生产者、至多一个消费者」的 SPSC 不变量。

本讲会用到的两个 Rust 语言点，对初学者可能较陌生，这里先做一句通俗解释：

- **`self` 按值传参（move）**：`fn f(self)` 表示调用者把 `self` 的所有权「交出去」，调用后调用者就再也不能用了。
- **`&mut self` 借用**：`fn f(&mut self)` 表示只借用可变引用，所有权仍归调用者，但借用期间原对象不能被别人同时访问。

本讲几乎不涉及新数学，核心是**所有权与生命周期**的推理。

---

## 3. 本讲源码地图

本讲涉及的文件及其作用：

| 文件 | 作用 |
| --- | --- |
| `src/traits/split.rs` | 定义 `Split` 与 `SplitRef` 两个 trait 本身，是本讲的「主角」。 |
| `src/rb/local.rs` | 为 `LocalRb` 实现 `Split`（用 `Rc`）与 `SplitRef`（用 `&`），并配 `Direct` 包装。 |
| `src/rb/shared.rs` | 为 `SharedRb` 实现 `Split`（用 `Arc`）与 `SplitRef`（用 `&`），并配 `Caching` 包装。 |
| `src/rb/traits.rs` | 定义 `RbRef` trait，并为 `&B`、`Rc<B>`、`Arc<B>` 三种「指向缓冲区的指针」实现它。拆分两端正是靠它指向底层缓冲区。 |
| `src/wrap/direct.rs` | `Direct` 包装器，`Prod`/`Cons`/`Obs` 是它的类型别名；`new()` 里用 hold 标志断言「不重复拆分」。 |
| `src/wrap/caching.rs` | `Caching` 包装器，`CachingProd`/`CachingCons` 是它的别名。 |
| `src/alias.rs` | `HeapProd`/`HeapCons`（基于 `Arc`，拥有）与 `StaticProd`/`StaticCons`（基于 `&'a`，借用）的别名，直观对照两种拆分。 |
| `examples/global_static.rs` | `#![no_std]` 示例：缓冲区放在 `static` 中，只能用 `split_ref`。 |

---

## 4. 核心概念与源码讲解

### 4.1 拆分的本质：一个缓冲区，两个句柄

#### 4.1.1 概念说明

一个环形缓冲区（`LocalRb` 或 `SharedRb`）在内存里是**一个对象**：它同时持有存储区 `storage`、读索引、写索引。但是在 SPSC（单生产者单消费者）的使用场景里，我们希望**生产者只负责写、消费者只负责读**，两者甚至可能运行在不同线程。

这就出现了一个矛盾：

- 数据结构上，读写索引属于同一个对象；
- 使用上，我们想把「写的能力」和「读的能力」拆成两个**可以独立移动、独立持有**的句柄（handle）。

**拆分（split）** 就是解决这个矛盾的接口：它接收一个完整的缓冲区，吐出一个 `(Producer, Consumer)` 二元组。此后，生产者只能 push、消费者只能 pop，双方各持一份句柄，但底层是同一块缓冲区。

这里有一个随之而来的关键问题：**缓冲区对象本身归谁所有？谁负责让它「活着」？**

- 如果拆分时把缓冲区的所有权「交出去」（`split(self)`），那缓冲区对象本身就得被放进一个**共享所有权的智能指针**里（`Rc` 或 `Arc`），让两个句柄各持一份引用计数，谁后销毁谁负责收尾。
- 如果拆分时只**借用**缓冲区（`split_ref(&mut self)`），那缓冲区对象仍归调用者所有，两个句柄只是借用它，借走期间调用者暂时不能动它。

这正对应两个 trait：`Split`（move）和 `SplitRef`（借用）。

#### 4.1.2 核心流程

不论哪种拆分，流程都可概括为：

```text
[一个完整缓冲区 rb]
        │
        │  split / split_ref
        ▼
[Producer 句柄] ──┐  两者底层指向  ┌── [同一块存储区 + read/write 索引]
                  │  同一个缓冲区  │
[Consumer 句柄] ──┘                ┘
```

具体步骤：

1. 创建句柄时，`new()` 会把对应的 hold 标志（`write_held` 或 `read_held`）置位，**断言它原本未被持有**——若已被持有就 panic。这从机制上保证「不重复拆分」。
2. 两个句柄各自只暴露读或写的能力（由包装器的 const generic `P`/`C` 控制）。
3. 句柄销毁（`drop`）时，`close()` 会把对应的 hold 标志复位，从而**释放**对应权限。

#### 4.1.3 源码精读

hold 标志的置位/断言逻辑在 `Direct::new` 中（`Caching` 包装最终也走到这里）。注意 `new()` 接收的 `rb` 是一个实现了 `RbRef` 的「指向缓冲区的指针」，它可以是 `Rc<B>`、`Arc<B>` 或 `&B`：

[src/wrap/direct.rs:38-50](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/direct.rs#L38-L50) —— `Direct::new`：当 `P`（写权限）为真时，调用 `hold_write(true)` 并断言其返回值为 `false`（即之前没人持有写权）；读端同理。**这就是「重复拆分会 panic」的根源。**

而 `RbRef` 抽象了「指向缓冲区的指针」，三种指针类型都有实现：

[src/rb/traits.rs:19-29](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/traits.rs#L19-L29) —— 为 `&B`、`Rc<B>`、`Arc<B>` 三种指针实现 `RbRef`。这三种指针正是「借用拆分」和「拥有拆分」所用的载体。

#### 4.1.4 代码实践

**实践目标**：从源码层面确认「拆分」就是把一个缓冲区对象转成两个共享底层的句柄。

**操作步骤**：

1. 打开 `examples/simple.rs`，阅读前两行：创建 `HeapRb`，然后 `rb.split()` 得到 `(prod, cons)`。
2. 在 `src/traits/split.rs` 中找到 `Split` trait 的定义，确认它的签名是 `fn split(self)`（按值消费）。

**需要观察的现象**：`simple.rs` 里 `rb` 在调用 `split()` 之后，原变量 `rb` 是否还能被继续使用？（不能——它被 move 走了。）

**预期结果**：你能用自己的话复述「拆分 = 一个对象 → 两个共享底层的句柄」，并指出 `split()` 之后原 `rb` 变量已失效。

#### 4.1.5 小练习与答案

**练习 1**：既然 `split` 把缓冲区「交出去」了，那底层缓冲区对象靠什么保持存活，避免在两个句柄还在用时就被销毁？

> **参考答案**：靠共享所有权的智能指针（`Rc` 或 `Arc`）。两个句柄各持一份引用计数，只要还有一个句柄活着，底层缓冲区就不会被销毁；最后一个句柄 drop 时引用计数归零，缓冲区才被清理。

**练习 2**：`Direct::new` 里的 `assert!(!unsafe { rb.rb().hold_write(true) })` 中的感叹号 `!` 表示什么？为什么重复拆分会触发它？

> **参考答案**：`hold_write(true)` 返回的是**旧值**。`!` 取反后断言「旧值为 false」，即「之前没有生产者持有写权」。如果已经拆过一次，旧值就是 `true`，取反为 `false`，`assert!` 失败 → panic。这正是 SPSC「至多一个生产者」不变量的运行时强制。

---

### 4.2 Split：消耗所有权的拆分（move + Rc/Arc）

#### 4.2.1 概念说明

`Split` trait 提供的 `split(self)` 是**按值消费**：调用者把缓冲区的所有权整个交出去。既然所有权交出去了，缓冲区对象就不能再靠调用者的栈帧存活，必须被搬到一个「能被两个句柄共同引用」的地方——这就是 `Rc`（单线程，引用计数）或 `Arc`（多线程，原子引用计数）。

选哪一个由缓冲区类型决定：

- `LocalRb`（单线程）→ `Rc`。`Rc` 不是 `Send`，正好和 `LocalRb` 不能跨线程的特性匹配。
- `SharedRb`（多线程）→ `Arc`。`Arc` 可跨线程共享，配合 `SharedRb` 的原子索引，两端可分别移动到不同线程。

正因为要用 `Rc`/`Arc`，`Split` 的实现都被 `#[cfg(feature = "alloc")]` 门控——**`split()` 依赖堆分配**。

#### 4.2.2 核心流程

以 `SharedRb` 为例，`split()` 的实际调用链是「分两跳」：

```text
HeapRb::new(cap)            // SharedRb<Heap<T>>
      │
      │  Split::split(self) for SharedRb<S>
      ▼
Arc::new(self)              // 先把自己搬进 Arc
      │
      │  Split::split(self) for Arc<SharedRb<S>>
      ▼
(CachingProd::new(self.clone()), CachingCons::new(self))
//                └─ clone Arc ─┘         └─ 移走 Arc ─┘
```

第一跳把「裸的 `SharedRb`」搬进 `Arc`；第二跳是 `Arc<SharedRb>` 上的 `split`，它**克隆两份 `Arc`**，分别交给生产端和消费端。两份 `Arc` 引用计数相同、指向同一块缓冲区，所以两端天然共享底层。

类型结果（在 `shared.rs` 里写死）：

```
SharedRb<S>::split  →  (CachingProd<Arc<SharedRb<S>>>, CachingCons<Arc<SharedRb<S>>>)
LocalRb<S>::split   →  (Prod<Rc<LocalRb<S>>>,            Cons<Rc<LocalRb<S>>>)
```

注意 `SharedRb` 用 `Caching` 包装、`LocalRb` 用 `Direct` 包装——**包装器类型由缓冲区类型决定**，与 split/split_ref 无关。

#### 4.2.3 源码精读

先看 trait 定义。注意 `Prod`/`Cons` 是关联类型，且 `fn split(self)` 把 `self` 按值吃掉：

[src/traits/split.rs:3-12](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/split.rs#L3-L12) —— `Split` trait：`type Prod: Producer`、`type Cons: Consumer`，方法签名 `fn split(self) -> (Self::Prod, Self::Cons)`。

再看 `SharedRb` 上的两跳实现。注意两个 `impl` 都带 `#[cfg(feature = "alloc")]`：

[src/rb/shared.rs:154-162](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs#L154-L162) —— 第一跳：`Split for SharedRb<S>`，把 `self` 搬进 `Arc` 后委托给 `Arc` 上的 split。结果类型是 `CachingProd<Arc<Self>>`。

[src/rb/shared.rs:163-171](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs#L163-L171) —— 第二跳：`Split for Arc<SharedRb<S>>`，克隆两份 `Arc` 分别构造 `CachingProd` 与 `CachingCons`。两端共享同一 `Arc`、同一缓冲区。

作为对照，`LocalRb` 用 `Rc` 与 `Direct` 包装：

[src/rb/local.rs:138-155](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/local.rs#L138-L155) —— `LocalRb` 的 split：先 `Rc::new(self)`，再在 `Rc<LocalRb>` 上克隆两份 `Rc`，构造 `Prod`/`Cons`（`Direct` 包装）。

#### 4.2.4 代码实践

**实践目标**：亲手用 `split()` 拿到基于 `Arc` 的两端，并验证消费端可以跨线程使用。

**操作步骤**（参考 `shared.rs` 顶部 doc 示例）：

```rust
// 示例代码：依赖 ringbuf 的 default features（含 std/alloc）
use ringbuf::{traits::*, HeapRb};
use std::thread;

let rb = HeapRb::<i32>::new(8);
let (mut prod, mut cons) = rb.split(); // self 被 move

prod.try_push(123).unwrap();
let popped = thread::spawn(move || {  // 把 cons move 到另一线程
    cons.try_pop().unwrap()
})
.join().unwrap();
assert_eq!(popped, 123);
```

**需要观察的现象**：`rb` 在 `split()` 之后不可再用（已被 move）；`cons` 能被 `move` 进 `thread::spawn`，说明它拥有 `Arc` 句柄、与生产端线程隔离。

**预期结果**：程序编译通过并打印/断言成功，说明 `split()` 产出的两端基于 `Arc`、可跨线程。

**待本地验证**：若你机器上未配置好工具链，可只做源码阅读——在 `src/rb/shared.rs` 的 doc 注释里就有这段几乎一样的示例。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `SharedRb` 的 split 用 `Arc` 而不是 `Rc`？

> **参考答案**：`SharedRb` 设计为可跨线程共享，而 `Rc` 不是线程安全的（非 `Send`/`Sync`）。`Arc` 用原子操作维护引用计数，可安全跨线程。`LocalRb` 单线程才用 `Rc`。

**练习 2**：`Split for SharedRb<S>` 的 `fn split(self)` 体只有一句 `Arc::new(self).split()`。为什么不直接在这里构造 `CachingProd`/`CachingCons`？

> **参考答案**：因为「裸 `SharedRb`」还没有 `Arc` 包裹，而两端句柄需要一个共享指针。先 `Arc::new(self)` 升级成 `Arc<SharedRb>`，再复用 `Split for Arc<SharedRb>` 的实现（克隆两份 `Arc`），避免重复逻辑。这是典型的「两跳委托」。

---

### 4.3 SplitRef：借用的拆分（&mut self + 引用句柄）

#### 4.3.1 概念说明

`SplitRef` trait 提供的 `split_ref(&mut self)` 是**借用**版本：调用者只交出一个 `&mut self`，**缓冲区的所有权仍归调用者**。两个句柄不再持有 `Rc`/`Arc`，而是持有对缓冲区的**引用** `&'a Self`。

这样做的最大好处是：**完全不需要堆分配**。没有 `Rc`/`Arc`，自然也不依赖 `alloc` feature。这就是为什么 `split_ref` 的实现**没有** `#[cfg(feature = "alloc")]` 门控——它在 `no_std`、无堆环境（典型如嵌入式）下依然可用。

代价是借用语义带来的限制：两个句柄**不能比缓冲区活得更久**（生命周期 `'a` 绑定在 `self` 上）；在句柄存活期间，原缓冲区被可变借用，调用者不能直接动它。

#### 4.3.2 核心流程

```text
let mut rb: SharedRb<...> = ...;   // 调用者拥有 rb
let (prod, cons) = rb.split_ref(); // 借用 &mut rb
//   └─ prod, cons 的生命周期 ⊂ rb 的借用期 ─┘
prod.try_push(...); cons.try_pop();
// 句柄 drop 后，rb 的借用结束，调用者重新可访问 rb
```

类型结果（注意生命周期参数 `'a`）：

```
SharedRb<S>::split_ref  →  (CachingProd<&'a Self>, CachingCons<&'a Self>)
LocalRb<S>::split_ref   →  (Prod<&'a Self>,         Cons<&'a Self>)
```

`'a` 由 trait 的 GAT（泛型关联类型）声明，并在 `split_ref` 返回值里用 `'_` 与 `&mut self` 的借用期绑定。这两个句柄拿的是**共享引用** `&'a Self`（而非 `&'a mut Self`），因为底层缓冲区内部用 `UnsafeCell`/原子实现了内部可变性。

#### 4.3.3 源码精读

trait 定义用到了 GAT（带生命周期的关联类型）。初学者不必深究语法，重点理解「返回类型的生命周期跟着 `&mut self` 走」：

[src/traits/split.rs:14-27](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/split.rs#L14-L27) —— `SplitRef` trait：关联类型 `RefProd<'a>`/`RefCons<'a>` 带 `'a` 且 `where Self: 'a`；方法 `fn split_ref(&mut self) -> (Self::RefProd<'_>, Self::RefCons<'_>)`。

`SharedRb` 上的实现——**注意没有 `#[cfg(feature = "alloc")]` 门控**：

[src/rb/shared.rs:181-194](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs#L181-L194) —— `SplitRef for SharedRb<S>`：返回 `CachingProd<&'a Self>` 与 `CachingCons<&'a Self>`，用 `self`（即 `&mut self` 重借用得到的共享引用）构造两端。

`LocalRb` 上的实现同理：

[src/rb/local.rs:165-178](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/local.rs#L165-L178) —— `SplitRef for LocalRb<S>`：返回 `Prod<&'a Self>`/`Cons<&'a Self>`（`Direct` 包装）。

对照看 `src/alias.rs` 里的两类别名，能直观感受到「拥有 vs 借用」在类型层面的区别：

[src/alias.rs:29-35](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/alias.rs#L29-L35) —— `HeapProd<T> = CachingProd<Arc<HeapRb<T>>>`（拥有，来自 `split()`）；

[src/alias.rs:19-23](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/alias.rs#L19-L23) —— `StaticProd<'a, T, N> = CachingProd<&'a StaticRb<T, N>>`（借用，来自 `split_ref()`）。注意 `StaticProd` 多了一个生命周期参数 `'a`，正因为它来自借用拆分。

#### 4.3.4 代码实践

**实践目标**：用 `split_ref()` 拿到引用两端，并体会借用限制。

**操作步骤**（参考 README 的「No heap」示例与 `examples/static.rs`）：

```rust
// 示例代码
use ringbuf::{traits::*, StaticRb};

const RB_SIZE: usize = 1;
let mut rb = StaticRb::<i32, RB_SIZE>::default();
let (mut prod, mut cons) = rb.split_ref(); // 借用 rb

assert_eq!(prod.try_push(123), Ok(()));
assert_eq!(prod.try_push(321), Err(321));  // 满了，原样退回
assert_eq!(cons.try_pop(), Some(123));
assert_eq!(cons.try_pop(), None);
// 句柄 drop 后，rb 的借用结束
```

**需要观察的现象**：在 `split_ref()` 之后、句柄还活着时，若尝试再访问 `rb`（比如再 `rb.split_ref()` 一次）会编译失败——因为 `rb` 被可变借用了。

**尝试跨线程（预期失败）**：把 `cons` `move` 进 `thread::spawn` 试试。由于 `cons` 持有的是 `&'a StaticRb`，它的生命周期绑定在当前函数的 `rb` 上，编译器会拒绝把它移到别的线程（借用的对象不能保证比线程活得久）。

**预期结果**：编译失败，报与生命周期/借用相关的错误。这正是 `split_ref` 与 `split` 的本质区别——引用两端不跨线程。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `split_ref` 的实现没有 `#[cfg(feature = "alloc")]`，而 `split` 有？

> **参考答案**：`split_ref` 返回的句柄持有的是 `&'a Self`（普通引用），不创建任何 `Rc`/`Arc`，不需要堆分配，因此不依赖 `alloc`。`split` 要把缓冲区搬进 `Rc`/`Arc`，需要堆分配，所以门控在 `alloc`。

**练习 2**：`split_ref` 的两个句柄拿到的是 `&'a Self`（共享引用），却能分别 push 和 pop（修改缓冲区）。这为什么不会违反「一个可变引用」的借用规则？

> **参考答案**：因为缓冲区内部通过 `UnsafeCell`（`LocalRb` 的 `Cell`）和原子类型（`SharedRb`）实现了**内部可变性**（interior mutability）。`&self` 在这里允许修改内部状态，由 hold 标志等机制在运行时保证 SPSC 安全。这是 ringbuf 一系列 `unsafe` 抽象的核心，细节见 [u5-l3](u5-l3-maybeuninit-unsafe-memory.md)。

---

### 4.4 别名连线：split 返回什么，以及跨线程与 no_alloc 的选择

#### 4.4.1 概念说明

把前两节拼起来，可以得到一张清晰的「拆分结果类型」对照表。需要特别记住的规律是：

- **包装器类型（Direct / Caching）由缓冲区类型决定**：`LocalRb` → `Direct`，`SharedRb` → `Caching`。
- **指针类型（Rc/Arc / &）由拆分方式决定**：`split` → `Rc`/`Arc`（拥有），`split_ref` → `&'a`（借用）。

这两个维度互相独立，组合出四种结果。

#### 4.4.2 核心流程

下表汇总四种组合（以 `HeapRb`/`StaticRb` 等别名为例）：

| 缓冲区 | 拆分方式 | 结果类型（Prod / Cons） | 指针 | 是否需 `alloc` | 能否跨线程 |
| --- | --- | --- | --- | --- | --- |
| `LocalRb` | `split()` | `Prod<Rc<_>>` / `Cons<Rc<_>>` | `Rc` | 是 | 否（`Rc` 非 `Send`） |
| `LocalRb` | `split_ref()` | `Prod<&'_>` / `Cons<&'_>` | `&` | 否 | 否（借用） |
| `SharedRb`（如 `HeapRb`） | `split()` | `HeapProd` / `HeapCons` = `CachingProd<Arc<_>>` / `CachingCons<Arc<_>>` | `Arc` | 是 | 是 |
| `SharedRb`（如 `StaticRb`） | `split_ref()` | `StaticProd` / `StaticCons` = `CachingProd<&'_>` / `CachingCons<&'_>` | `&` | 否 | 否（借用） |

#### 4.4.3 源码精读

`examples/global_static.rs` 是「只能用 `split_ref`」的典型例子。它把缓冲区放进一个 `static`：

[examples/global_static.rs:6-11](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/examples/global_static.rs#L6-L11) —— `static RB: OnceMut<StaticRb<i32, 1>>`，通过 `RB.get_mut()` 拿到 `&mut StaticRb` 后调用 `split_ref()`。

为什么它「只能」用 `split_ref`，不能用 `split`？有两层原因：

1. **`split(self)` 要按值消费 `self`，但 `static` 里的对象不能被 move 出来**——它是全局唯一的、固定在静态存储区的，你没有它的所有权，只有一个 `&mut`。
2. **`split()` 依赖 `alloc`，而本例是 `#![no_std]`**（见文件首行 `#![no_std]`），根本不开启 `alloc`，`Split` 的实现不存在。

因此唯一可行的路径是 `split_ref(&mut self)`：借走 `&mut`，返回引用两端，零分配、不移动 `static` 对象。

`src/alias.rs` 中的别名恰好对应这两条路径：

[src/alias.rs:17-35](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/alias.rs#L17-L35) —— `HeapProd<T> = CachingProd<Arc<HeapRb<T>>>`（来自 `split()`，`Arc` 拥有，可跨线程）；`StaticProd<'a, T, N> = CachingProd<&'a StaticRb<T, N>>`（来自 `split_ref()`，`&'a` 借用，不跨线程、不分配）。

#### 4.4.4 代码实践

**实践目标**：用源码阅读的方式，把「别名 → 拆分方式 → 指针」三者对应起来。

**操作步骤**：

1. 在 `src/alias.rs` 中分别找到 `HeapProd` 与 `StaticProd` 的定义。
2. 对每个别名回答三个问题：它包的是 `Arc` 还是 `&`？因此对应 `split()` 还是 `split_ref()`？是否带生命周期参数？

**需要观察的现象**：`StaticProd<'a, ...>` 有生命周期参数 `'a`，`HeapProd<T>` 没有。这直接反映了「借用 vs 拥有」。

**预期结果**：你能不查表地画出本节那张对照表，并解释 `StaticProd` 为何带 `'a`。

#### 4.4.5 小练习与答案

**练习 1**：`split()` 和 `split_ref()`，哪个能在 `#![no_std]` 且不开启 `alloc` 的环境下使用？

> **参考答案**：`split_ref()`。它的实现不被 `alloc` 门控，返回引用两端，不分配堆内存。`split()` 需要 `Rc`/`Arc`，依赖 `alloc`，在无堆环境下不可用。这正是 `StaticRb` 在 `no_std` 里都用 `split_ref()` 的原因。

**练习 2**：假设你想在两个线程之间用一个 `StaticRb` 传递数据，应该用 `split()` 还是 `split_ref()`？

> **参考答案**：要用 `split()`。`split_ref()` 的两端是借用（`&'a`），生命周期绑定在创建处的局部变量或 `static` 上，不能 `move` 到其他线程。但注意：`split()` 要求开启 `alloc`（因为要 `Arc`）；而 `StaticRb` 常用于 `no_std` 无堆场景，此时两难——若必须在无堆环境跨线程，需要更底层的手段（如 `from_raw_parts` 配合自定义存储与手动共享），这已超出本讲范围。

---

## 5. 综合实践

设计一个贯穿本讲的对比任务：**对同一个 `SharedRb`（用 `HeapRb`）分别演示 `split()` 与 `split_ref()`，并把现象对照起来。**

**实践目标**：亲手验证「拥有拆分可跨线程、借用拆分不跨线程」，并理解 `global_static.rs` 的约束。

**操作步骤**：

1. **`split()` 版本（拥有、可跨线程）**：

   ```rust
   // 示例代码
   use ringbuf::{traits::*, HeapRb};
   use std::thread;

   let rb = HeapRb::<i32>::new(8);
   let (mut prod, mut cons) = rb.split();
   prod.try_push(42).unwrap();
   let got = thread::spawn(move || cons.try_pop().unwrap()).join().unwrap();
   assert_eq!(got, 42);
   ```

   预期：编译通过、断言成功。`cons` 被成功 move 进子线程。

2. **`split_ref()` 版本（借用、不跨线程）**：

   ```rust
   // 示例代码
   use ringbuf::{traits::*, HeapRb};

   let mut rb = HeapRb::<i32>::new(8);
   let (mut prod, mut cons) = rb.split_ref();
   prod.try_push(42).unwrap();
   assert_eq!(cons.try_pop(), Some(42));
   // 取消下面注释会编译失败：cons 借用了 rb，不能 move 进线程
   // std::thread::spawn(move || { let _ = cons.try_pop(); });
   ```

   预期：本段能编译运行；若放开注释的线程代码，则编译报错（借用对象不能跨线程 move）。

3. **解释 `global_static.rs`**：阅读 `examples/global_static.rs`，回答——为什么它用 `split_ref()` 而非 `split()`？

   参考答案要点：(a) 缓冲区在 `static` 里，调用者只有 `&mut`、没有所有权，无法满足 `split(self)` 的 move 要求；(b) 该示例是 `#![no_std]`，不开启 `alloc`，`Split` 实现根本不存在；(c) `split_ref()` 零分配、借用语义，正好契合 `static` 单一存储点的场景。

**需要观察的现象**：两段代码在「能否把句柄 move 进线程」这一行为上正好相反，且这种差异完全由**返回类型的指针载体**（`Arc` vs `&'a`）决定。

**待本地验证**：若本地未配置工具链，可只做源码阅读——以上代码片段与 `src/rb/shared.rs` doc 注释、`examples/static.rs`、README 的两个示例高度一致，可作为参照。

---

## 6. 本讲小结

- 拆分的本质是把**一个缓冲区对象**转成 **Producer / Consumer 两个句柄**，两者共享同一块底层存储与索引；底层的存活由 hold 标志和引用计数共同维护。
- `Split::split(self)` **按值消费**缓冲区，必须把它搬进 `Rc`（`LocalRb`）或 `Arc`（`SharedRb`）以保持存活，因此**依赖 `alloc`**。
- `SplitRef::split_ref(&mut self)` **借用**缓冲区，两端持有 `&'a Self`，**不需要 `alloc`**，是 `no_std`/无堆环境下的唯一选择。
- 包装器类型（`Direct` / `Caching`）由缓冲区类型决定；指针类型（`Rc`/`Arc` / `&`）由拆分方式决定——这两个维度互相独立。
- `HeapProd/Cons` 基于 `Arc`（拥有、可跨线程），`StaticProd/Cons` 基于 `&'a`（借用、带生命周期、不跨线程）。
- `examples/global_static.rs` 只能用 `split_ref`：`static` 对象无法 move、且该例 `#![no_std]` 无 `alloc`，两条原因叠加排除了 `split()`。

---

## 7. 下一步学习建议

本讲讲清了「怎么把缓冲区拆成两端」，但句柄内部的**包装器**（`Direct`/`Caching`/`Frozen`）如何控制同步时机、以及它们如何通过 `Based`/`Delegate*` 的 blanket impl 复用方法，还没展开。建议：

1. 学习 **u4-l1（Wrap 与 RbRef）** 与 **u4-l2（Direct 包装器）**：弄清 `Wrap`、`RbRef` 抽象与 `Direct::new` 的 hold 标志机制。
2. 学习 **u4-l3（Frozen）** 与 **u4-l4（Caching）**：理解 `split()` 默认产出的 `Caching` 包装为何「按需同步」。
3. 想深入理解 hold 标志如何强制 SPSC 不变量，以及重复拆分为何 panic，可预习 **u5-l2（Hold flags）**。

本讲涉及的核心 trait 是 `Split` 与 `SplitRef`，它们只是「装配」包装器的入口；真正决定读写行为的，是它们产出的 `Producer`/`Consumer` 句柄与底层包装器。
