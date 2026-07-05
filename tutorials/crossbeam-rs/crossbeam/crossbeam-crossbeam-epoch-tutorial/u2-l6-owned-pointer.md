# Owned：所有权指针（类 Box）

## 1. 本讲目标

本讲聚焦「指针三剑客」中的第二位——`Owned<T>`。上一讲 u2-l5 讲完了共享存储位置 `Atomic<T>`，本讲讲「**独占所有权**」的那一个。读完本讲，你应当能够：

- 说清楚 `Owned<T>` 内部存了什么、它和标准库 `Box<T>` 到底哪里一样、哪里不一样。
- 在 `Owned`、`Box`、裸指针三者之间熟练互转（`new` / `init` / `from_raw` / `into_box` / `from(Box)`），并指出每条转换路径对 **tag（标记位）** 的影响。
- 解释 `into_shared` 如何把独占所有权「交给」受 Guard 保护的 `Shared<'g, T>`，以及为什么它的签名里要带一个看似没用的 `&'g Guard`。
- 理解 `Owned` 的 `Drop` 为什么会真正释放对象（调用 `T::drop`），以及这与「没有 `Drop` 的 `Atomic`」形成的关键对照。

本讲只讲 `Owned` 本身。tag 的位运算原理（`low_bits` / `compose_tag` / `decompose_tag`）已在 u2-l5 详细推导过，本讲直接复用其结论；`Shared` 的完整语义留到 u2-l7。

## 2. 前置知识

进入源码前，先建立三个直觉。

### 2.1 `Owned` 就是「带 tag 的 Box」

在 crossbeam-epoch 里，堆上对象的**独占所有权**用 `Owned<T>` 表示。你可以把它理解成「一个 `Box<T>`，但是它的指针低位还能夹带一点 tag」。

它和 `Box<T>` 的关键区别只有两点：

1. `Owned<T>` 的指针可以带 tag（复用 u2-l5 讲过的 tagged pointer 机制）；
2. `Owned<T>` 实现了内部的 `Pointer` trait，因而可以被**压成一个 `*mut ()`** 塞进 `Atomic` 里（`store` / `compare_exchange` / `from(Owned)`），这是 `Box` 做不到的。

除此之外，`Owned` 同样独占对象、同样在 `Drop` 时释放对象、同样支持 `Deref`/`DerefMut`。

### 2.2 所有权转移靠 `mem::forget` 而不是拷贝

`Owned` 内部就一个 `*mut ()` 字段，且**没有自定义 `Clone`（除非 `T: Clone`）**。把所有权从 `Owned` 转移到 `Atomic` / `Box` / `Shared` 时，不能「复制指针」——那会导致两个所有者、双重释放。正确做法是：把 `data` 字段拿走，然后 `mem::forget(self)` 阻止 `Owned::drop` 运行。这个模式在本讲源码里反复出现，是理解所有「`into_*`」方法的关键。

### 2.3 tag 是 crossbeam 私有的夹带，`Box` 没有

回顾 u2-l5：堆地址按对齐对齐，地址最低的 \(k\) 位恒为 0（\(k = \text{trailing\_zeros}(\text{ALIGN})\)），这 \(k\) 位就是 tag 的藏身处。

\[
\text{tag} \in [0,\; 2^{k} - 1], \qquad k = \text{trailing\_zeros}(\text{ALIGN}_T)
\]

但**标准库的 `Box<T>` 对此一无所知**——它的指针永远干净对齐（低位全 0）。所以一旦对象经由 `Box` 中转，tag 就会丢失。这个事实决定了本讲综合实践里「tag 是否保留」的答案，也解释了为什么 crossbeam 要自己造一个 `Owned` 而不是直接用 `Box`。

> 名词解释：**所有权（ownership）** 指「谁负责最终释放这个对象」。**tag（标记位）** 指塞进指针空闲低位里的少量附加数据（u2-l5 详述）。**`mem::forget`** 是标准库函数，消费一个值但**跳过其 `Drop`**，常用于所有权转移时防止重复释放。

## 3. 本讲源码地图

本讲全部内容集中在一个文件里：

| 文件 | 作用 |
|------|------|
| [src/atomic.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs) | 定义 `Owned<T>` 及其全部方法、`Drop`、`Deref`/`DerefMut`、与 `Box`/`T` 的互转、`Pointer` trait 实现，以及它依赖的标记位工具函数 `decompose_tag` / `compose_tag` / `ensure_aligned` |

`into_shared` 会顺带提到 `Shared` 与 `Guard`，但这两者的完整语义分别在 u2-l7 和 u3 单元展开，本讲只用到「`Shared<'g, T>` 借用自 `&'g Guard`」这一结论。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. `Owned<T>` 的结构与构造，以及它与 `Box` 的互转（`new` / `init` / `from_raw` / `into_box` / `from(Box)`）
2. `into_shared`：把独占所有权交给 `Shared<'g, T>`，以及背后的生命周期 `'g`
3. `tag` / `with_tag` 与 `Drop`：tag 的读写，以及 `Owned::drop` 如何真正释放对象

---

### 4.1 `Owned<T>` 的结构与构造，及与 `Box` 的互转

#### 4.1.1 概念说明

`Owned<T>` 是一个**堆上独占对象的所有权指针**。结构体上方的文档注释说得很直白：

[src/atomic.rs:L941-L946](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L941-L946) —— 「An owned heap-allocated object. This type is very similar to `Box<T>.`」并且强调「指针必须正确对齐，因此可以在地址的空闲最低位里存 tag」。

它和另两位「剑客」的分工是：

- `Owned<T>`：我一个人独占这个对象，像 `Box`，**可带 tag**。
- `Shared<'g, T>`：从 `Atomic` 里 load 出来的借用快照，生命周期绑在 Guard 上（u2-l7）。
- `Atomic<T>`：共享的存储位置本身，多线程通过它读写同一个指针（u2-l5）。

一个最直观的对照：**`Atomic<T>` 没有 `Drop`，对象不会自动释放；而 `Owned<T>` 有 `Drop`，离开作用域就自动释放对象。** 这正是为什么数据结构的析构函数常把 `Atomic` 转成 `Owned` 再 drop（见 u2-l5 提到的 `Atomic::into_owned`）。

#### 4.1.2 核心流程

构造一个 `Owned<T>` 有几条路径，最终几乎都汇聚到私有的 `from_ptr`：

```
Owned::new(value)          // sized 类型最常用
   └─> Owned::init(init)   // 通用入口
         └─> Owned::from_ptr( T::Pointable::init(init) )   // 分配堆 + 装成 *mut ()

Owned::from_raw(*mut T)    // 从裸指针构造（unsafe），会校验对齐
Owned::from(Box<T>)        // 从 Box 转过来（内部走 from_raw）
Owned::from(T)             // 从值构造（内部走 new）
```

反向把 `Owned` 变成 `Box` 只有一条路：

```
Owned::into_box(self) ──> Box<T>     // 消费 Owned，丢弃 tag，重建 Box
```

注意三条与 `Box` 相关的路径（`from(Box)`、`into_box`、`from_raw`）**都只认 sized 的 `T`**，因为它们最终都要 `Box::from_raw` / `Box::into_raw`，而 `Box` 要求 `T: Sized`。动态大小类型（`[MaybeUninit<T>]`）只能走 `init`。

#### 4.1.3 源码精读

先看结构定义：

[src/atomic.rs:L947-L950](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L947-L950) —— `Owned<T>` 只有两个字段：一个类型擦除的裸指针 `data: *mut ()`（真正存「指针 + tag」的那个字），加一个 `PhantomData<Box<T>>`。注意这里用的是 `PhantomData<Box<T>>` 而非 `PhantomData<*mut T>`——因为 `Owned` 表示的是**独占所有权**（语义同 `Box`），用 `Box<T>` 作为幻影类型能正确表达「drop 时释放、拥有一个 `T`」的所有权语义。

再看构造器。最常用的 `new`：

[src/atomic.rs:L1031-L1033](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L1031-L1033) —— `new(init: T)` 只转调更通用的 `init`。注意它在 `impl<T> Owned<T>`（仅 sized）里，因此 `Owned::new(1234)` 里 `T` 被推断为 `i32`。

[src/atomic.rs:L1046-L1048](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L1046-L1048) —— `init(init: T::Init)` 才是真正干活的：调 u2-l4 讲过的 `T::init(init)`（对 sized 类型就是 `Box::into_raw(Box::new(init))`）完成堆分配，拿到 `*mut ()`，再用 `from_ptr` 包成 `Owned`。它写在 `impl<T: ?Sized + Pointable>` 里，因此对 `[MaybeUninit<T>]` 这种 DST 也适用——这是 `Owned::<[MaybeUninit<i32>]>::init(10)` 能工作的原因。

`from_raw` 用于从外部裸指针构造，是 `unsafe` 的：

[src/atomic.rs:L977-L1003](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L977-L1003) —— 它先把 `*mut T` 转成 `*mut ()`，调 `ensure_aligned::<T>` 校验指针真的对齐（低位没有夹带脏数据），再走 `from_ptr`。其 `# Safety` 契约明确：`raw` 必须来自一个 `Owned`，且同一个 `raw` 不能被 `from_raw` 两次——否则双重释放。

`ensure_aligned` 本身是个调试断言：

[src/atomic.rs:L64-L68](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L64-L68) —— 检查 `raw & low_bits` 是否为 0；不为 0 说明指针没对齐，panic。它在 `from_raw`、`Shared::from(*const T)` 等入口把关，防止外部喂进低位带 tag 的「脏指针」冒充干净指针。

把 `Owned` 转回 `Box`：

[src/atomic.rs:L1005-L1020](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L1005-L1020) —— `into_box` 先 `decompose_tag` 把 tag **丢弃**（`let (raw, _) = ...`），再 `mem::forget(self)` 阻止 `Owned::drop`，最后 `Box::from_raw(raw.cast::<T>())` 重建 `Box`。**关键点：tag 在这里被剥掉了**，因为 `Box` 不认识 tag。

反向的 `From<Box<T>>`：

[src/atomic.rs:L1148-L1165](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L1148-L1165) —— `Owned::from(b: Box<T>)` 内部调 `from_raw(Box::into_raw(b))`。由于 `Box::into_raw` 返回的指针总是干净对齐（低位全 0），所以**从 `Box` 构造出的 `Owned` 永远是 tag=0**。这条路径不会、也无法恢复任何 tag。

把上述两条结合，就得到一个重要结论：

> **`Owned` 与 `Box` 之间的互转会丢失 tag。** `into_box` 主动丢弃 tag；`from(Box)` 因为 `Box` 本身不带 tag 而必然得到 tag=0。tag 只有在 `Owned`/`Shared`/`Atomic` 这三者构成的「crossbeam 世界」里流转时才得以保留。

`Deref` / `DerefMut` 让 `Owned` 用起来像 `Box`：

[src/atomic.rs:L1126-L1140](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L1126-L1140) —— 两者都先 `decompose_tag` 拿到干净地址（丢弃 tag），再走 u2-l4 的 `T::as_ptr` / `T::as_mut_ptr` 取引用。所以 `*owned` 和 `owned.as_ref()` 取到的都是「值本身」，与 tag 无关。后续 `Borrow`/`BorrowMut`/`AsRef`/`AsMut`（[L1167-L1189](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L1167-L1189)）都只是转调它们。

`From<T>`：

[src/atomic.rs:L1142-L1146](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L1142-L1146) —— `Owned::from(t)` 即 `Owned::new(t)`，于是 `Owned::new(1234)` 与 `Owned::from(1234)` 等价。

最后顺带看一眼 `Owned` 如何满足 `Pointer` trait，这是它能被塞进 `Atomic` 的原因：

[src/atomic.rs:L952-L974](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L952-L974) —— `Owned` 先 `impl Sealed`（sealed trait，外部无法实现），再 `impl Pointer<T>`：`into_ptr(self)` 取出 `data` 并 `mem::forget(self)`（所有权让渡给那个字），`from_ptr(data)` 则把字包回 `Owned`（带 `debug_assert!(!data.is_null())`）。`Pointer` trait（[L925-L939](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L925-L939)）统一了 `Owned` 和 `Shared`「压成一个字」的能力，u2-l8 会用到它在 `compare_exchange` 上的作用。

#### 4.1.4 代码实践

**实践目标**：用多种方式构造 `Owned`，并通过 `into_box` / `from(Box)` 在 `Owned` 与 `Box` 之间互转，观察值与 tag 的命运。

**操作步骤**（示例代码，放进依赖了 `crossbeam-epoch` 的实验 crate 的 `examples/owned_basic.rs` 运行）：

```rust
// 示例代码
use crossbeam_epoch::Owned;

fn main() {
    // 1) 最常用：new
    let o1 = Owned::new(1234i32);
    assert_eq!(*o1, 1234);
    assert_eq!(o1.tag(), 0); // 默认无 tag

    // 2) 从 Box 转：from(Box) 必然得到 tag=0
    let o2 = Owned::from(Box::new(5678i32));
    assert_eq!(*o2, 5678);
    assert_eq!(o2.tag(), 0);

    // 3) 从值转：From<T> 等价于 new
    let o3 = Owned::from(90i32);
    assert_eq!(*o3, 90);

    // 4) 反向：Owned -> Box（into_box 会丢弃 tag）
    let o4 = Owned::new(100i32);
    let b: Box<i32> = o4.into_box();
    assert_eq!(*b, 100);
    // 现在 b 是普通 Box，没有 tag 概念；o4 已被消费，不能再访问

    println!("{:?}", Owned::new(7i32)); // Debug 打印 raw 与 tag
}
```

**需要观察的现象**：前三个 `Owned` 的 `tag()` 都是 0；`into_box` 后得到普通 `Box<i32>`，值不变；`Debug` 输出形如 `Owned { raw: 0x..., tag: 0 }`。

**预期结果**：所有断言通过，打印一行 `Owned { raw: 0x..., tag: 0 }`。

**待本地验证**：上述示例未在本讲环境实跑，请在依赖了 `crossbeam-epoch` 的实验 crate 中用 `cargo run --example owned_basic` 确认输出。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Owned` 用 `PhantomData<Box<T>>` 而不是像 `Atomic` 那样用 `PhantomData<*mut T>`？

**参考答案**：两者的「所有权语义」不同。`Owned` 表示**独占所有权**（拥有一个 `T`，drop 时释放），与 `Box<T>` 语义一致，因此用 `PhantomData<Box<T>>` 表达「drop 时释放、影响 drop 检查」的所有权语义。而 `Atomic` 表示「共享存储位置」（不拥有、不 drop），用 `*mut T` 让它默认非 `Send`/`Sync` 再按条件打开。两者选择不同幻影类型，正是为了精确反映各自的所有权与线程安全语义。

**练习 2**：`Owned::from(Box::new(5))` 得到的 `Owned` 的 `tag()` 是多少？为什么？

**参考答案**：是 0。`From<Box<T>>` 内部调 `from_raw(Box::into_raw(b))`，而 `Box::into_raw` 返回的指针总是按 `T` 的对齐干净对齐、低位全 0。`from_raw` 不人为添加任何 tag，所以结果必然是 tag=0。这也说明 `Box` 这条中转路径无法承载 crossbeam 的 tag。

---

### 4.2 `into_shared`：把独占所有权交给 `Shared<'g, T>`

#### 4.2.1 概念说明

`Owned` 拥有对象，`Atomic` 是共享存储位置。但在并发数据结构里，我们经常需要把一个**新分配的** `Owned`（比如链表的新节点）装进 `Atomic` 里供其他线程读取。装进去之后，这个对象就不再是「我独占」了——其他线程会通过 `Atomic::load` 拿到指向它的 `Shared<'g, T>`。

`Owned::into_shared` 就是这一步「交付」的桥梁：它消费 `Owned`，返回一个 `Shared<'g, T>`。从这一刻起，原 `Owned` 不复存在（不会重复释放），对象以 `Shared` 的形式进入「受 Guard 保护的借用世界」。

#### 4.2.2 核心流程

```
Owned::into_shared(self, &'g Guard) ──> Shared<'g, T>
        │
        ├─ self.into_ptr()    // 取出 data，mem::forget(self)，所有权让渡
        ├─ Shared::from_ptr(data)
        └─ 生命周期 'g 来自传入的 &'g Guard
```

这里和 u2-l5 讲过的 `Atomic::load` 如出一辙：参数 `&'g Guard` 在运行时**根本不被读取**，它纯粹是给类型系统看的——把返回的 `Shared<'g, T>` 的生命周期 `'g` 绑到这个 Guard 上。编译器由此保证：**只要 `Shared` 还在使用，Guard 就还没被 drop**，也就是线程仍处于 pin 状态（pin 的运行时含义见 u3-l9）。

#### 4.2.3 源码精读

[src/atomic.rs:L1050-L1065](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L1050-L1065) —— `into_shared<'g>(self, _: &'g Guard) -> Shared<'g, T>`。三个细节：

1. 参数名是 `_`（`_: &'g Guard`）：Guard 在运行时不被使用，仅用于把返回类型的生命周期钉成 `'g`。这正是 u2-l5 里「Guard 参数只为生命周期」的同款设计。
2. `self.into_ptr()`：消费 `self`，取走 `data` 并 `mem::forget(self)`（见 4.1.3 的 `Pointer::into_ptr`）。所以原 `Owned` 的 `Drop` **不会**运行——对象没有被释放，所有权转移给了即将诞生的 `Shared`。
3. `Shared::from_ptr(self.into_ptr())`：把那个字（指针 + tag）包成一个 `Shared<'g, T>`。tag 也随之带过去，因为 `into_ptr`/`from_ptr` 都是原样搬运那个字。

`#[allow(clippy::needless_lifetimes)]` 这个属性值得注意：clippy 会认为显式写出 `'g` 是多余的（编译器本可推导），但作者刻意保留它，是为了让签名里「`Shared<'g, T>` 的生命周期来自这个 Guard」这件事一目了然——这是一种文档化的取舍。

反向操作在 `Shared` 那边：

[src/atomic.rs:L1440-L1443](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L1440-L1443) —— `Shared::into_owned(self) -> Owned<T>`（`unsafe`）：当调用者确认「没有其他线程再持有该对象」时，可以把 `Shared` 重新收归为 `Owned`，从而恢复独占所有权、让对象在 `Owned::drop` 时被正常释放。这两个方法互为逆操作，构成了「`Owned` ⇄ `Shared`」的所有权流转闭环。

> 关键不变量：`into_shared` 让 `Owned` 的 `Drop` 失效（通过 `mem::forget`），对象不会被释放。对象的「最终释放责任」随之转移到新的所有者——要么是某个 `Atomic`（需 `into_owned` 或 `defer_destroy` 回收），要么是被 `Shared::into_owned` 收回后再 `drop`。

#### 4.2.4 代码实践

**实践目标**：亲手完成一次「`Owned` → `into_shared` → 在 Guard 作用域内使用 → `Shared::into_owned` 收回 → drop 释放」的完整所有权流转，并体会 Guard 作用域对 `Shared` 生命周期的约束。

**操作步骤**：

```rust
// 示例代码
use crossbeam_epoch::{self as epoch, Atomic, Owned};
use std::sync::atomic::Ordering::SeqCst;

fn main() {
    // 把一个带 tag=1 的 Owned 装进 Atomic
    let a = Atomic::<u64>::from(Owned::new(100u64).with_tag(1));

    let guard = &epoch::pin(); // Guard 存在 ⇒ 线程已 pin

    // 演示 into_shared：新分配一个 Owned，转成交付用的 Shared
    let prepared = Owned::new(200u64).with_tag(2);
    let shared = prepared.into_shared(guard); // prepared 被消费，不再可用
    unsafe { assert_eq!(*shared.deref(), 200); }
    assert_eq!(shared.tag(), 2); // tag 被带过来了

    // 同样地，从 Atomic load 出的也是 Shared，借用自同一个 guard
    let snap = a.load(SeqCst, guard);
    unsafe { assert_eq!(*snap.deref(), 100); }
    assert_eq!(snap.tag(), 1);

    // 确认无人再持有后，把 Shared 收回归 Owned 再 drop（释放对象）
    unsafe {
        drop(shared.into_owned()); // 释放值 200 的对象
        drop(snap.into_owned());   // 收回值 100 的对象
    }
    // 注意：a 仍指向值 100 的对象——但那块已被 snap.into_owned 取走，不能再动 a
    drop(guard);
}
```

**需要观察的现象**：`into_shared` 之后 `prepared` 不可再用（编译期保证：它被 `self` 消费）；`shared.tag()` 为 2，说明 tag 随所有权一起转移；若把任何 `Shared` 的使用语句挪到 `drop(guard)` 之后，会直接编译失败（生命周期报错）。

**预期结果**：所有断言通过，程序无泄漏、无双重释放地结束。

**待本地验证**：本示例涉及手动所有权回收与 `unsafe`，建议在本地用 `cargo +nightly miri run` 确认无未定义行为。本讲环境未实跑。

#### 4.2.5 小练习与答案

**练习 1**：`into_shared` 的参数写成 `_: &'g Guard`，运行时没用 Guard，为什么要它？

**参考答案**：纯粹是**类型层面的生命周期约束**。它让返回的 `Shared<'g, T>` 借用自这个 Guard，编译器从而保证「`Shared` 还在用，Guard 就不能先 drop」。Guard 的存在还隐含了「线程处于 pin 状态、被读对象在 `Shared` 有效期内不会被 EBR 回收」这层运行时安全（u3-l9 详述）。两层含义叠加，才需要这个看似无用的参数。

**练习 2**：调用 `owned.into_shared(guard)` 之后，原对象会不会被释放？为什么？

**参考答案**：不会。`into_shared` 内部走 `self.into_ptr()`，后者 `mem::forget(self)` 跳过了 `Owned::drop`。对象的所有权被让渡给了返回的 `Shared`（以及它最终流向的 `Atomic`），因此此刻既不会被双重释放、也不会被提前释放。最终释放责任落在「下一个所有者」身上。

---

### 4.3 `tag` / `with_tag` 与 `Drop` 实现

#### 4.3.1 概念说明

本模块讲三件收尾的事：

1. **读 tag**：`Owned::tag()` 取出当前夹带的 tag。
2. **写 tag**：`Owned::with_tag(self, tag)` 消费自身，返回一个带新 tag 的 `Owned`。
3. **释放对象**：`Owned::drop` 调用 `T::drop(raw)`，真正运行析构并归还内存——这是 `Owned` 区别于 `Atomic`（无 `Drop`）的核心。

这三者合在一起，回答了一个贯穿本讲的问题：**tag 存在哪里、怎么读写、谁负责在何时释放对象。**

#### 4.3.2 核心流程

```
tag(&self)                ──> usize            // 读：decompose_tag 取低位
with_tag(self, tag)       ──> Owned<T>         // 写：into_ptr + compose_tag + from_ptr
Drop::drop(&mut self)     ──> ()               // 释放：decompose_tag 丢 tag，调 T::drop(raw)
Clone::clone(&self)       ──> Owned<T>         // 克隆值，并保留 tag
```

`with_tag` 是消费式的（`self` 而非 `&self`），因为它要走 `into_ptr`（即 `mem::forget`）那条所有权转移路径；这与 `Shared::with_tag(&self, tag)`（u2-l7，借用的、`Copy` 的）形成对照。

#### 4.3.3 源码精读

读 tag：

[src/atomic.rs:L1067-L1079](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L1067-L1079) —— `tag(&self)` 调 `decompose_tag::<T>(self.data)` 取第二元。`decompose_tag`（[L78-L85](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L78-L85)，u2-l5 详述）用 `ptr as usize & low_bits::<T>()` 把低位那 \(k\) 位抠出来。

写 tag：

[src/atomic.rs:L1081-L1097](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L1081-L1097) —— `with_tag(self, tag)` 先 `self.into_ptr()`（取字 + `forget`），再用 `compose_tag::<T>(data, tag)` 把 tag 写进低位，最后 `from_ptr` 包回 `Owned`。`compose_tag`（[L70-L76](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L70-L76)）做的是：

\[
\text{compose\_tag}(p, t) = (p \,\&\, \neg\,\text{low\_bits}) \;\|\; (t \,\&\, \text{low\_bits})
\]

即「清低位、再或上被截断的 tag」。`t & low_bits` 这步截断意味着：传一个超出范围的 tag 不会 panic，而是被静默截断（u2-l5 已举例：`i8` 的 `low_bits = 0`，任何 tag 都被截成 0）。另外 `from_ptr` 内有 `debug_assert!(!data.is_null())`，所以不能对一个「空 `Owned`」调 `with_tag`——不过正常的 `Owned::new` 不会产生空指针。

`Drop` 是本模块的重头戏：

[src/atomic.rs:L1100-L1107](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L1100-L1107) —— `Owned::drop` 先 `decompose_tag` **丢弃 tag**（`let (raw, _) = ...`），再 `unsafe { T::drop(raw) }`。这里的 `T::drop` 是 u2-l4 讲过的 `Pointable::drop`：

- 对 sized `T`，它是 `drop(Box::from_raw(ptr.cast::<T>()))`（[L178-L180](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L178-L180)）：重建 `Box` 再 drop，**既运行 `T` 的析构函数、又归还堆内存**。
- 对 `[MaybeUninit<T>]`，它是 `Global.deallocate(...)`（[L256-L262](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L256-L262)）：直接归还裸内存（元素本身是 `MaybeUninit`，无需析构）。

这正是文档承诺的「`Owned` 像 `Box`」的兑现：**离开作用域自动释放对象**。对照之下，`Atomic<T>` 没有 `Drop`（u2-l5），指向的对象必须靠 `into_owned()` 或 `defer_destroy` 显式回收——这一对照是无锁数据结构析构设计的核心动机。

`Clone` 体现了「值复制 + tag 保留」：

[src/atomic.rs:L1120-L1124](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L1120-L1124) —— `Clone` 要求 `T: Clone`（且 sized）。它先 `(**self).clone()` 克隆出**值**（`Deref` 会先丢掉 tag、取出值），再用 `Self::new(...)` 分配新堆对象，最后 `.with_tag(self.tag())` 把原 tag 补回去。**注意它和 `into_box` 的对比**：`Clone` 刻意保留 tag，而 `into_box` 刻意丢弃 tag——因为 `Clone` 的产物还是 `Owned`（仍在 crossbeam 世界里），而 `into_box` 的产物是 `Box`（不认识 tag）。

最后是 `Debug`：

[src/atomic.rs:L1109-L1118](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L1109-L1118) —— `Debug` 用 `decompose_tag` 把字拆成 `(raw, tag)` 两个字段分别打印，形如 `Owned { raw: 0x..., tag: N }`，便于调试时直接看到 tag。

> 汇总「谁保留 tag、谁丢弃 tag」：
>
> | 操作 | tag 命运 |
> |------|----------|
> | `with_tag` / `into_ptr` / `into_shared` | **保留**（原样搬运那个字） |
> | `Clone` | **保留**（显式 `.with_tag(self.tag())` 补回） |
> | `tag()` / `Deref` / `Drop` | **读取或丢弃**（`decompose_tag` 后只取地址） |
> | `into_box` / `from(Box)` / `from_raw` | **丢弃 / 无**（`Box` 通道不承载 tag） |

#### 4.3.4 代码实践

**实践目标**：验证 `with_tag` 能正确写入 tag、`Clone` 能保留 tag，而一旦经过 `Box` 中转 tag 就会丢失。

**操作步骤**：

```rust
// 示例代码
use crossbeam_epoch::Owned;

fn main() {
    // i32: align=4, low_bits=0b11=3, tag 范围 [0,3]，2 合法
    let o = Owned::new(1234i32);
    assert_eq!(o.tag(), 0);

    let o = o.with_tag(2); // 消费旧的，返回带 tag=2 的新 Owned
    assert_eq!(o.tag(), 2);
    assert_eq!(*o, 1234); // 值不受影响

    // Clone 保留 tag
    let o2 = o.clone();
    assert_eq!(o2.tag(), 2);
    assert_eq!(*o2, 1234);

    // 但经过 Box 中转：into_box 丢 tag，from(Box) 也只有 tag=0
    let b = o.into_box();      // tag=2 被丢弃
    assert_eq!(*b, 1234);
    let o3 = Owned::from(b);   // Box 不带 tag ⇒ tag=0
    assert_eq!(o3.tag(), 0);   // 期望 0：tag 没有保留下来
    assert_eq!(*o3, 1234);
}
```

**需要观察的现象**：`with_tag(2)` 后 `tag()` 为 2，值仍是 1234；`o.clone()` 的 tag 也是 2；但 `into_box` → `from(Box)` 这一轮回之后 `tag()` 变回 0。

**预期结果**：所有断言通过。tag 在 `Owned` 内部的 `with_tag`/`clone` 中保留，在 `Box` 中转中丢失。

**待本地验证**：本示例未在本讲环境实跑，请在实验 crate 中确认断言。

#### 4.3.5 小练习与答案

**练习 1**：`Owned::new(0i8).with_tag(3).tag()` 的结果是什么？为什么？

**参考答案**：结果是 0。`i8` 的 `ALIGN = 1`，故 `low_bits::<i8>() = (1 << 0) - 1 = 0`。`with_tag` 内部 `compose_tag` 会做 `tag & low_bits = 3 & 0 = 0`，tag 被静默截断为 0。这与 u2-l5 里 `Shared::<i8>::null().with_tag(...)` 的行为完全同源——tag 容量由对齐决定，与指针类型无关。

**练习 2**：为什么 `Owned::with_tag` 是 `self`（消费）而 `Shared::with_tag` 是 `&self`（借用）？

**参考答案**：因为两者的所有权模型不同。`Owned` 是独占所有权，改 tag 必须把对象「拿走再还回」，于是走 `self.into_ptr()`（含 `mem::forget`）+ `from_ptr` 的所有权转移路径，签名只能是 `self`。而 `Shared` 是 `Copy` 的借用快照（u2-l7），复制一个带新 tag 的快照无需触动原对象，所以是 `&self` 并返回新的 `Self`。

**练习 3**：`Owned::drop` 为什么要先 `decompose_tag` 丢掉 tag，再调 `T::drop(raw)`？

**参考答案**：因为 `T::drop`（即 `Pointable::drop`）接收的是「干净地址」，它要拿这个地址去重建 `Box`（`Box::from_raw`）或调用 `deallocate`。如果直接把带 tag 的字传进去，地址就不是合法的堆分配起始地址（低位被 tag 污染），`Box::from_raw` / `deallocate` 会在错误的地址上操作，导致未定义行为。所以必须先剥离 tag、还原纯地址，再交给 `Pointable::drop`。

---

## 5. 综合实践

把本讲三个模块串起来，完成规格里指定的实践任务：用 `Owned::new(1234)` 创建对象，演示 `into_box` 转成 `Box`、`with_tag(2)` 设置标记位、并通过 `from(Box)` 反向构造 `Owned`，观察 tag 是否保留。

**实践目标**：综合运用 `new` / `with_tag` / `tag` / `into_box` / `from(Box)` / `into_shared`，亲手验证「tag 在 `Owned` 内部保留、在 `Box` 中转丢失」这一结论，并体会 `Owned` 与 `Box` 的异同。

**操作步骤**：

```rust
// 示例代码
use crossbeam_epoch::{self as epoch, Owned};
use std::sync::atomic::Ordering::SeqCst;

fn main() {
    // 1) 创建对象
    let o = Owned::new(1234i32);
    println!("init        : value={}, tag={}", *o, o.tag()); // 1234, 0

    // 2) with_tag(2) 设置标记位（i32 的 tag 范围 [0,3]，2 合法）
    let o = o.with_tag(2);
    println!("with_tag(2) : value={}, tag={}", *o, o.tag()); // 1234, 2

    // 3) into_box 转成 Box：tag 在此被丢弃
    let b: Box<i32> = o.into_box();
    println!("into_box    : value={}", *b);                  // 1234（Box 无 tag 概念）

    // 4) from(Box) 反向构造 Owned：Box 不带 tag ⇒ 必然得到 tag=0
    let o2 = Owned::from(b);
    println!("from(Box)   : value={}, tag={}", *o2, o2.tag()); // 1234, 0

    // 结论：tag 没有保留！into_box 丢 tag，from(Box) 也只能给出 tag=0。
    assert_eq!(o2.tag(), 0);

    // 5) 额外：用 into_shared 把 Owned 交给 Shared（tag 随所有权保留）
    let o3 = Owned::new(5678i32).with_tag(1);
    let guard = &epoch::pin();
    let shared = o3.into_shared(guard);
    unsafe { println!("into_shared : value={}, tag={}", *shared.deref(), shared.tag()); } // 5678, 1
    assert_eq!(shared.tag(), 1); // tag 保留
    unsafe { drop(shared.into_owned()); } // 收回所有权并释放
    drop(guard);

    // o2 在作用域结束时会由 Owned::drop 自动释放（这点和 Box 一样）
}
```

**需要观察的现象**：
- 步骤 2：`with_tag(2)` 成功，`tag()` 为 2，值不变。
- 步骤 3：`into_box` 得到的 `Box<i32>` 值为 1234，但 tag 概念已不存在。
- 步骤 4：`from(Box)` 反向得到的 `o2` 的 `tag()` 为 **0**——**tag 没有保留**。这就是「`Owned` 与 `Box` 互转会丢 tag」的直观证据。
- 步骤 5：`into_shared` 这条 crossbeam 内部通道则保留了 tag=1。
- 程序结束时 `o2` 被 `Owned::drop` 自动释放，无需手动回收。

**预期结果**：所有断言通过；输出依次为 `tag=0 → tag=2 →（Box 无 tag）→ tag=0 → tag=1`，对应值始终为 1234 或 5678。

**待本地验证**：本综合示例涉及 `unsafe` 与跨线程语义（`epoch::pin`），未在本讲环境实跑。建议在本地实验 crate 中用 `cargo run` 运行，并可选地用 `cargo +nightly miri run` 检查无未定义行为。

> 进阶思考：如果上面的 `i32` 换成 `i8`，步骤 2 的 `with_tag(2)` 还能让 `tag()` 变成 2 吗？（答：不能，`i8` 的 `low_bits = 0`，`2 & 0 = 0`，`tag()` 仍是 0——这与练习 4.3.5 第 1 题同源。）

## 6. 本讲小结

- `Owned<T>` 内部就是一个 `*mut ()` 加 `PhantomData<Box<T>>`；用 `Box<T>` 作幻影类型是为了精确表达「独占所有权、drop 时释放」的语义，区别于 `Atomic` 的 `*mut T`。
- 构造路径 `new` → `init` → `from_ptr(T::Pointable::init(...))` 最终都汇聚到私有 `from_ptr`；`from_raw` / `from(Box)` / `from(T)` 是额外的入口，其中与 `Box` 相关的三条只认 sized `T`。
- **`Owned` 与 `Box` 互转会丢 tag**：`into_box`（[L1005-L1020](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L1005-L1020)）主动剥离 tag；`from(Box)`（[L1148-L1165](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L1148-L1165)）因 `Box` 不带 tag 而必然得到 tag=0。tag 只在 `Owned`/`Shared`/`Atomic` 内部流转时保留。
- `into_shared`（[L1050-L1065](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L1050-L1065)）通过 `self.into_ptr()`（`mem::forget`）把所有权让渡给 `Shared<'g, T>`，参数 `&'g Guard` 仅用于把返回值的生命周期钉成 `'g`（运行时不读），与 u2-l5 的 `load` 同款设计。
- `tag()` / `with_tag(self, tag)` 读写 tag，依赖 u2-l5 的 `decompose_tag` / `compose_tag`；超出范围的 tag 被 `tag & low_bits` **静默截断**（如 `i8` 只能存 0）。`Clone`（[L1120-L1124](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L1120-L1124)）会显式补回 tag，是与 `into_box`（丢 tag）的关键对照。
- `Owned::drop`（[L1100-L1107](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L1100-L1107)）先剥离 tag 再调 `T::Pointable::drop`（对 sized `T` 即重建 `Box` 再 drop），**既运行析构又归还内存**；这是 `Owned`（有 `Drop`、自动释放）区别于 `Atomic`（无 `Drop`、需手动回收）的核心，也是无锁数据结构析构设计的基石。

## 7. 下一步学习建议

本讲把「指针三剑客」里的 `Owned` 讲完了。下一步建议：

- **u2-l7（Shared）**：补齐最后一位剑客。重点理解 `Shared<'g, T>` 的生命周期 `'g` 如何与 Guard 绑定、`deref`/`deref_mut`/`as_ref` 的 `unsafe` 契约（数据竞争与 Ordering），以及 `into_owned`/`try_into_owned` 如何把借用收回归所有权——它正是本讲 `into_shared` 的逆操作。
- **u2-l8（CAS 与位运算）**：学习 `compare_exchange` / `compare_exchange_weak` 的返回值（`CompareExchangeValue`/`CompareExchangeError`）、`fetch_update` 的循环 CAS，以及 `fetch_and`/`fetch_or`/`fetch_xor` 如何只对 tag 低位做原子位运算。届时 `Pointer` trait（本讲 4.1.3 提及）会让 `Owned` 和 `Shared` 在 CAS 中互换角色。
- **u3 单元（Guard 与延迟回收）**：理解 `Guard` 背后的 pin 语义、`defer_destroy` 延迟回收，彻底打通「为什么 `into_shared` 出去的对象，在并发下要靠 epoch 而不是 `Owned::drop` 来安全释放」这层运行时含义。
