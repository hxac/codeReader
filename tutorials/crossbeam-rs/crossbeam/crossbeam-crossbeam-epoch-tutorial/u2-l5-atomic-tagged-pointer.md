# Atomic：共享原子指针与标记位（tagged pointer）

## 1. 本讲目标

本讲聚焦 `crossbeam-epoch` 中最常被打交道的类型 `Atomic<T>`。读完本讲，你应当能够：

- 说清楚 `Atomic<T>` 内部到底存了什么、为什么它只占「一个机器字」。
- 解释 `Send`/`Sync` 是如何为 `Atomic<T>` 手动推导出来的，以及为什么需要手动推导。
- 用数学和位运算的语言，讲明白「对齐低位存 tag」的原理：哪些类型能存 tag、能存几位、能存多大值。
- 熟练使用 `load` / `load_consume` / `store` / `swap`，并能回答一个关键问题——为什么 `load` 和 `swap` 强制要求传 `&Guard`，而 `store` 却不需要。

本讲只讲 `Atomic` 本身的「结构与基本操作」，比较交换（CAS）、`fetch_update`、`fetch_and/or/xor` 等读写改写操作留给下一讲（u2-l8）。

## 2. 前置知识

在进入源码前，先建立两个直觉。

### 2.1 为什么必须用「一个字」表示对象

并发数据结构里的核心操作（读、写、比较交换）都建立在硬件的原子指令之上，而常见的原子指令一次只能动**一个机器字**（一个 `usize`，64 位平台上是 8 字节）。

这就带来了一个硬约束：如果某个共享状态要用原子指令来更新，它就必须能塞进一个字里。这正是上一讲 u2-l4 引入 `Pointable` 的动机——把任意 `T`（甚至动态大小的 `[MaybeUninit<T>]`）都变成「可以用一个字的指针指向」的对象。

### 2.2 对齐的低位是「空闲的」

堆上分配的对象地址总是**按对齐对齐**的。例如 `u64` 的对齐是 8，那么指向 `u64` 的指针其地址一定是 8 的倍数，地址的最低 3 位永远是 `000`。

这 3 位「永远为 0」的空闲位，刚好可以拿来存一点额外的元数据——这就是 **tag（标记位）**。把「指针 + tag」塞进同一个字，就能用**单条原子指令**同时更新二者，避免「先改指针、再改 tag」之间被其他线程撕裂（torn write）。

```
一个 Atomic<u64> 内部的字（64 位平台）：
高 61 位：堆地址                低 3 位：tag
┌─────────────────────────────────┬─────┐
│        指向 u64 的堆地址         │ tag │
└─────────────────────────────────┴─────┘
                                   ↑
                          因为 align=8，这 3 位原本必为 0
```

这就是所谓的 **tagged pointer（带标记的指针）**。本讲的核心就是讲清楚 crossbeam-epoch 如何优雅地实现它。

> 名词解释：**机器字（word）** 指 CPU 一次原生处理的位数，在 Rust 里通常对应 `usize`。**对齐（alignment）** 指一个类型在内存中存放地址必须是某个 2 的幂的倍数，由 `mem::align_of::<T>()` 给出。**tag** 指塞进指针空闲低位里的少量附加数据。

## 3. 本讲源码地图

本讲几乎全部内容都集中在一个文件里：

| 文件 | 作用 |
|------|------|
| [src/atomic.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs) | 定义 `Pointable`、`Atomic`、`Owned`、`Shared`、`Pointer` 以及所有标记位工具函数和基本原子操作 |

`Atomic::null` 还借助了 [src/lib.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/lib.rs) 中的 `const_fn!` 宏来在「常量函数」与「普通函数」间切换，本讲会顺带点一句，深入的可移植性抽象留到 u6-l22。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. `Atomic<T>` 的结构与构造（`new` / `null` / `init` / `from_ptr`）
2. 标记位原理（`low_bits` / `compose_tag` / `decompose_tag` / `map_addr`）
3. 基本操作（`load` / `load_consume` / `store` / `swap` 与 Guard 的关系）

---

### 4.1 `Atomic<T>` 的结构与构造

#### 4.1.1 概念说明

`Atomic<T>` 是一个**可以被多线程共享的原子指针**。你可以把它理解成一个线程安全的 `Cell<*mut T>`：内部只存一个指向堆对象 `T` 的指针，但所有读写都走原子指令，因此可以在 `&Atomic<T>`（共享引用）上安全地修改。

它和 `Owned<T>`（独占所有权，类 `Box`，下一讲 u2-l6 详讲）、`Shared<'g, T>`（受 Guard 保护的借用，u2-l7 详讲）并称「指针三剑客」。区别在于：

- `Owned<T>`：我一个人拥有它，像 `Box`。
- `Shared<'g, T>`：我从某个 `Atomic` 里 load 出来的只读快照，生命周期绑在 Guard 上。
- `Atomic<T>`：那是**共享的存储位置本身**，多线程通过它来读写同一个指针。

#### 4.1.2 核心流程

构造一个 `Atomic<T>` 通常有这几条路径：

```
Atomic::new(value)         // 对 sized 类型：分配堆 + 装箱，最常用
   └─> Atomic::init(init)  // 通用入口，调用 T::Pointable::init
         └─> Atomic::from(Owned::init(init))
               └─> Atomic::from_ptr(data)   // 内部私有构造器

Atomic::<T>::null()        // 构造一个空指针（常量函数）
Atomic::from(owned)        // 从 Owned 转过来
Atomic::from(box)          // 从 Box 转过来
Atomic::from(shared)       // 从 Shared 克隆出一个新的 Atomic
```

`from_ptr` 是私有内部构造器，几乎所有公开入口最终都汇聚到它——把一个已经打包好（可能带 tag）的 `*mut ()` 塞进 `AtomicPtr`。

#### 4.1.3 源码精读

先看结构定义本身：

[src/atomic.rs:L274-L280](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L274-L280) —— `Atomic<T>` 只有两个字段：一个类型擦除的原子裸指针 `AtomicPtr<()>`，加一个 `PhantomData<*mut T>`。`data` 才是真正存「指针 + tag」的那个字；`_marker` 不占运行时空间，只为类型系统服务。

注意 `_marker: PhantomData<*mut T>` 这个选择很关键。裸指针 `*mut T` 既非 `Send` 也非 `Sync`，于是 `Atomic<T>` **默认也是非 `Send`/非 `Sync`**；接下来再手动、按需地把它「打开」：

[src/atomic.rs:L279-L280](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L279-L280) —— 当且仅当 `T: Pointable + Send + Sync` 时，才为 `Atomic<T>` 实现 `Send` 和 `Sync`。这是合理的：只有当被指向的对象本身能跨线程安全共享与传递时，指向它的原子指针才能跨线程共享。这种「先 `PhantomData` 关掉、再 `unsafe impl` 按条件打开」是 Rust 里表达「线程安全取决于泛型参数」的标准套路。

再看构造器。最常用的 `new`：

[src/atomic.rs:L293-L296](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L293-L296) —— `new(init: T)` 只是转调更通用的 `init`。

[src/atomic.rs:L309-L311](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L309-L311) —— `init` 接收 `T::Init`（对 sized 类型就是 `T` 本身），转调 `Owned::init` 拿到一个 `Owned<T>`，再通过 `From<Owned<T>>` 装进 `Atomic`。这里复用了 u2-l4 讲过的 `Pointable::init` 来做真正的堆分配。

[src/atomic.rs:L314-L319](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L314-L319) —— 私有的 `from_ptr`：把一个 `*mut ()` 包进 `AtomicPtr::new`。它是所有 `From` 实现的共同终点。

`null` 稍特殊，它被 `const_fn!` 宏包了一层：

[src/atomic.rs:L321-L338](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L321-L338) —— `null()` 把空指针塞进去。`const_fn!` 宏（定义在 [src/lib.rs:L138-L151](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/lib.rs#L138-L151)）的作用是：在非 loom 编译时它是 `const fn`（可以在 `static` 里用，见测试 `const_null`），在 loom 模型测试编译时退化为普通 `fn`（因为 loom 的 `AtomicPtr::new` 不是 const）。这是可移植性的一处伏笔，u6-l22 会系统讲。

最后是一组 `From` 转换，它们都汇聚到 `from_ptr`：

[src/atomic.rs:L875-L879](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L875-L879) —— `From<Owned<T>>`：把 `Owned` 的 `data` 拿过来，`mem::forget(owned)` 避免 `Owned::drop` 释放对象（所有权转移给 `Atomic`）。

[src/atomic.rs:L882-L892](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L882-L892) —— `From<Box<T>>` 和 `From<T>`：层层转成 `Owned` 再转成 `Atomic`，所以 `Atomic::new(1234)` 与 `Atomic::from(1234)` 等价。

[src/atomic.rs:L894-L907](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L894-L907) —— `From<Shared<'g, T>>`：注意 `Shared` 是 `Copy`，这里只是拷出它的 `data` 造一个新 `Atomic`，原 `Shared` 仍有效。

#### 4.1.4 代码实践

**实践目标**：用多种方式构造 `Atomic`，并通过 `Debug` 输出观察它内部的 `raw`（地址）与 `tag` 两个字段。

**操作步骤**（示例代码，可放进一个依赖了 `crossbeam-epoch` 的 `examples/build.rs` 里运行）：

```rust
// 示例代码
use crossbeam_epoch::{Atomic, Owned, Shared};
use std::sync::atomic::Ordering::SeqCst;

fn main() {
    // 1) 最常用：new
    let a1 = Atomic::new(1234i64);
    // 2) 从 Owned
    let a2 = Atomic::<i64>::from(Owned::new(5678));
    // 3) 空指针
    let a3 = Atomic::<i64>::null();
    // 4) 从 Shared 克隆
    let guard = &crossbeam_epoch::pin();
    let s = Shared::<i64>::null();
    let a4 = Atomic::from(s);

    println!("{:?}", a1); // Debug 会打印 raw 与 tag
    println!("{:?}", a2);
    println!("{:?}", a3);
    println!("{:?}", a4);
    drop(guard);

    // 别泄漏：Atomic 没有 Drop，必须手动回收
    unsafe {
        drop(a1.into_owned());
        drop(a2.into_owned());
    }
}
```

**需要观察的现象**：`Debug` 打印形如 `Atomic { raw: 0x..., tag: 0 }`。前两个的 `tag` 都是 0（我们没设置 tag），地址不同；`a3` 的 `raw` 是空指针。

**预期结果**：四条 `Atomic { ... tag: 0 }` 输出，`a3` 的 `raw` 为 `0x0`。

> 提醒：`Atomic<T>` **没有实现 `Drop`**，它不会自动释放指向的对象。你必须用 `into_owned()` 取回所有权、或在并发场景里用 `defer_destroy`（见 u3-l10）来回收。上面的 `into_owned()` 就是为此而写，否则会内存泄漏。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Atomic<T>` 要用 `PhantomData<*mut T>` 而不是 `PhantomData<T>`？

**参考答案**：用 `PhantomData<T>` 会让 `Atomic<T>` 在 `T: Send/Sync` 时自动获得 `Send/Sync`（因为 `&T` 是 `Sync` 的前提），但这并不能正确反映「裸指针语义」。用 `*mut T` 可以让 `Atomic<T>` **默认既非 `Send` 也非 `Sync`**，强制库作者用 `unsafe impl` 显式声明线程安全契约（即 L279-280 的条件实现），更精确、更安全。

**练习 2**：`Atomic::new(1234)` 和 `Atomic::from(Owned::new(1234))` 在行为上等价吗？

**参考答案**：等价。`new` 转调 `init`，`init` 又转调 `Owned::init` 后通过 `From<Owned<T>>` 装进 `Atomic`；而 `Atomic::from(Owned::new(...))` 走的也是同一条 `From<Owned<T>>` 路径。两者最终都汇聚到私有的 `from_ptr`，分配与所有权转移完全一致。

---

### 4.2 标记位原理：`low_bits` / `compose_tag` / `decompose_tag` / `map_addr`

#### 4.2.1 概念说明

这是本讲的「数学核心」。我们要回答：给定一个类型 `T`，它的指针里到底有几位能用来存 tag？

答案完全取决于 `T` 的对齐。因为对齐总是 2 的幂，设

\[
\text{ALIGN}_T = 2^{k}
\]

那么指向 `T` 的任何合法地址都是 \(2^{k}\) 的倍数，地址的最低 \(k\) 位恒为 0。这 \(k\) 位就是可用的 tag 空间，tag 的取值范围是

\[
\text{tag} \in [0,\; 2^{k} - 1]
\]

其中 \(k = \text{trailing\_zeros}(\text{ALIGN}_T)\)，即 `ALIGN` 二进制末尾 0 的个数。

举几个常见例子（64 位平台）：

| 类型 `T` | `ALIGN` | \(k\) | 可用 tag 范围 | 说明 |
|----------|---------|-------|---------------|------|
| `u8` / `i8` | 1 | 0 | `[0, 0]` | 对齐为 1，没有任何空闲位 |
| `u16` / `i16` | 2 | 1 | `[0, 1]` | 1 位 tag |
| `u32` / `i32` | 4 | 2 | `[0, 3]` | 2 位 tag |
| `u64` / `i64` | 8 | 3 | `[0, 7]` | 3 位 tag |

这正是本讲实践任务里「u64 能存 3、i8 只能存 0」的根源。

#### 4.2.2 核心流程

整套标记位机制由四个自由函数（注意不是方法，是模块级函数）协作完成：

```
low_bits::<T>()           : usize      // 算出 tag 掩码（k 个 1）
ensure_aligned::<T>(raw)  : ()         // 调试用：断言指针真的对齐
compose_tag::<T>(ptr, tag): *mut ()    // 把 tag 写进指针的低位
decompose_tag::<T>(ptr)   : (*mut (), usize) // 把指针拆成 (地址, tag)
map_addr(ptr, f)          : *mut T     // 安全地修改指针地址（保留 provenance）
```

`compose_tag` 和 `decompose_tag` 互为逆操作，分别用于「写入 tag」和「读出 tag」。`map_addr` 是底层的「按地址改指针」工具，专门绕开 strict-provenance 问题。

#### 4.2.3 源码精读

先看 `low_bits`：

[src/atomic.rs:L58-L62](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L58-L62) —— 计算 tag 掩码。`T::ALIGN.trailing_zeros()` 给出 \(k\)，`(1 << k) - 1` 就是 \(k\) 个连续的 1。例如 `u64`：`(1 << 3) - 1 = 0b111 = 7`；`i8`：`(1 << 0) - 1 = 0`。

`ensure_aligned` 是调试用的断言：

[src/atomic.rs:L64-L68](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L64-L68) —— 检查指针地址与 `low_bits` 按位与是否为 0；不为 0 说明指针没对齐，会 panic。它在 `Owned::from_raw`、`Shared::from(*const T)` 等入口被调用，防止外部喂进一个非法的、低位带数据的指针。

`compose_tag` 负责把 tag 写进去：

[src/atomic.rs:L70-L76](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L70-L76) —— 数学上即

\[
\text{compose\_tag}(p, t) = (p \,\&\, \neg\,\text{low\_bits}) \;\|\; (t \,\&\, \text{low\_bits})
\]

先把指针低 \(k\) 位清零（`a & !low_bits`），再或上被截断到 \(k\) 位以内的 tag（`tag & low_bits`）。`tag & low_bits` 这步截断意味着：传一个超出范围的 tag 不会 panic，而是被静默截断——所以「i8 传 with_tag(3)」不会报错，但存进去的会是 `3 & 0 = 0`。

`decompose_tag` 是逆操作：

[src/atomic.rs:L78-L85](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L78-L85) —— 同时取出「清掉 tag 的纯地址」和「tag 本身」。注意取 tag 用的是 `ptr as usize & low_bits`（直接按位与），而清地址用的是 `map_addr(ptr, |a| a & !low_bits)`——两者都依赖 `low_bits`，但后者走 `map_addr` 是为了保留指针的 provenance（见下）。

`map_addr` 是最微妙的一步：

[src/atomic.rs:L87-L95](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L87-L95) —— 注释直白地说明：这等价于标准库尚在 nightly 的 `pointer::map_addr`，只因 MSRV 不够才自己实现。它没有用 `f(ptr as usize) as *mut T` 这种「指针转 usize 改完再转回指针」的写法（那种写法在 strict-provenance / CHERI 这类用「能力（capability）」追踪指针的架构上会丢失指针合法性），而是用 `wrapping_add` / `wrapping_sub` 做**指针运算**：先算出地址增量 `new_addr - old_addr`，再让字节指针 `wrapping_add` 这个增量，从而在「改地址」的同时保留了原指针的 provenance。这是可移植性的关键技巧，u6-l22 会展开。

> 名词解释：**provenance** 指 Rust/LLVM 内存模型里「一个指针的来源合法性」。在 CHERI 这类硬件上，裸地址只是一个能力的一部分，直接 `as usize` 再 `as *mut T` 会丢掉能力位，导致解引用未定义。`wrapping_add` 保持指针语义，更安全。

#### 4.2.4 代码实践

**实践目标**：验证 u64 能存 tag=3、i8 只能存 0，并用项目自带的测试佐证你的判断。

**操作步骤**：

1. 写下面这段示例代码并运行：

```rust
// 示例代码
use crossbeam_epoch::{Atomic, Shared};
use std::sync::atomic::Ordering::SeqCst;

fn main() {
    let guard = &crossbeam_epoch::pin();

    // u64: align=8, low_bits=0b111=7, tag 范围 [0,7]，3 合法
    let a = Atomic::<u64>::from(Shared::null().with_tag(3));
    let p = a.load(SeqCst, guard);
    println!("u64 tag = {}", p.tag()); // 期望 3
    assert_eq!(p.tag(), 3);

    // i8: align=1, low_bits=0, tag 范围 [0,0]，3 被截断成 0
    let b = Atomic::<i8>::from(Shared::null().with_tag(3));
    let q = b.load(SeqCst, guard);
    println!("i8 tag = {}", q.tag()); // 期望 0（被截断）
    assert_eq!(q.tag(), 0);
}
```

2. 对照项目自带的两个测试，它们正是这件事的「官方证词」：

[src/atomic.rs:L1596-L1604](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L1596-L1604) —— `valid_tag_i8` 只敢传 `with_tag(0)`，而 `valid_tag_i64` 敢传 `with_tag(7)`。这两个测试就是 u64 与 i8 tag 容量差异的最小回归保护。

**需要观察的现象**：u64 的 `tag()` 返回 3，i8 的 `tag()` 返回 0。

**预期结果**：打印 `u64 tag = 3` 与 `i8 tag = 0`，两个断言通过。

**待本地验证**：上面这段示例程序的精确打印行未在本讲环境中实跑，请在你自己的实验 crate 中确认输出与断言一致；项目自带测试可用 `cargo test --test '' valid_tag`（在 `crossbeam-epoch` 目录下）直接验证。

#### 4.2.5 小练习与答案

**练习 1**：`Shared::<i8>::null().with_tag(5)` 会不会 panic？之后 `tag()` 返回什么？

**参考答案**：不会 panic。`with_tag` 内部调 `compose_tag`，其中 `tag & low_bits::<i8>()` 即 `5 & 0 = 0`，tag 被静默截断为 0，`tag()` 返回 0。这正是为什么库要把「i8 只能用 tag 0」作为测试固定下来——它不是编译期错误，而是运行时静默截断。

**练习 2**：为什么 `decompose_tag` 取 tag 时直接用 `ptr as usize & low_bits`，而清地址时却要走 `map_addr`？

**参考答案**：取 tag 只需要一个 `usize` 数值，不在乎指针合法性，所以直接转 usize 按位与即可。但「清掉低位后的地址」还要作为**指针**继续使用（要解引用、要参与后续原子操作），必须保留 provenance，所以必须走 `map_addr` 的指针运算，而不能 `as usize` 再 `as *mut ()`。

---

### 4.3 基本操作：`load` / `load_consume` / `store` / `swap` 与 Guard

#### 4.3.1 概念说明

有了结构和 tag 机制，现在看怎么读写。`Atomic<T>` 提供四个最基本操作：

| 操作 | 是否需要 `&Guard` | 是否返回 `Shared<'g, T>` | 说明 |
|------|------------------|------------------------|------|
| `load` | **是** | 是 | 读出当前指针快照 |
| `load_consume` | **是** | 是 | 用 consume 序读，弱内存模型上更快 |
| `store` | **否** | 否 | 写入新指针（`Shared` 或 `Owned`） |
| `swap` | **是** | 是 | 原子地换入新值，返回旧值 |

这里有一个初学者最容易困惑的点：**为什么 `load` 和 `swap` 非要传一个 `&Guard`，而 `store` 不要？** 这个问题的答案分两层，是本模块的重点。

#### 4.3.2 核心流程

```
load(order, &Guard)        ──> Shared<'g, T>     // 读：需要 Guard 绑定生命周期
load_consume(&Guard)       ──> Shared<'g, T>     // 读：consume 语义
store(new, order)          ──> ()                // 写：不返回 Shared，无需 Guard
swap(new, order, &Guard)   ──> Shared<'g, T>     // 读写：返回旧 Shared，需 Guard
```

读操作返回的 `Shared<'g, T>` 借用自传入的 `&'g Guard`，于是编译器会保证：**只要 `Shared` 还在使用，Guard 就一定还没被 drop**。这既是生命周期安全（防悬垂使用），也是 EBR 安全（Guard 存在意味着线程已 pin，被读的对象不会被 GC 回收——这一点 u3 单元会展开）。

而 `store` 只写不读，不产生需要保护的 `Shared`，所以它不需要 Guard。

> 承接 u1-l3 的结论：`load`/`swap` 强制要求传 `&Guard`，且解引用 `Shared` 需 `unsafe`；多线程下必须用 `Release`/`Acquire`/`SeqCst` 来同步，`Relaxed` 会构成数据竞争。本讲只补充「为什么签名里有 Guard」这一层，pin 的运行时含义留给 u3-l9。

#### 4.3.3 源码精读

`load`：

[src/atomic.rs:L356-L358](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L356-L358) —— 注意参数名是 `_`（`_: &'g Guard`）：**Guard 在运行时根本没被读取**！它纯粹是给类型系统看的——把返回的 `Shared<'g, T>` 的生命周期 `'g` 与这个 Guard 绑起来。函数体只是 `self.data.load(order)` 取出那个字，再 `Shared::from_ptr` 还原成带生命周期的 `Shared`。

`load_consume`：

[src/atomic.rs:L382-L384](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L382-L384) —— 同样忽略 Guard（`_: &'g Guard`），但它调的是 `self.data.load_consume()`。这个 `load_consume` 来自 [src/atomic.rs:L12](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L12) 引入的 `crossbeam_utils::atomic::AtomicConsume` trait。consume 排序只对「依赖该 load 结果」的后续操作建立顺序，在弱内存模型（如 ARM）上通常不需要内存屏障指令，因而比 acquire 更快；x86 上两者表现接近。

`store`：

[src/atomic.rs:L403-L405](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L403-L405) —— 签名里**没有** Guard 参数！它接受任何 `P: Pointer<T>`（即 `Owned` 或 `Shared`），调 `new.into_ptr()` 把它压成 `*mut ()` 后直接 `self.data.store`。因为不返回 `Shared`，自然不需要生命周期凭证。`Pointer` trait（见 [src/atomic.rs:L925-L939](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L925-L939)）是一个 sealed trait，统一了 `Owned` 和 `Shared`「如何压成一个字」的能力，下一讲 u2-l8 会用到。

`swap`：

[src/atomic.rs:L424-L426](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L424-L426) —— 原子地换入新值并返回旧值。它又要 Guard（参数名同样是 `_`），因为返回的旧 `Shared<'g, T>` 需要生命周期绑定。底层就是 `AtomicPtr::swap`。

把这四个签名放一起看，规律很清楚：

- **凡是返回 `Shared<'g, T>` 的操作（`load` / `load_consume` / `swap`），都要求传 `&'g Guard`，且 Guard 仅用于生命周期标注、运行时不读。**
- **不返回 `Shared` 的操作（`store`），不要 Guard。**

> 关于 Ordering：`load` 接受任意 `Ordering`（但不能是 `Release`/`AcqRel`）；`store` 接受任意 `Ordering`（但不能是 `Acquire`/`AcqRel`）；`swap` 是读改写，可用任意。这些约束与标准库 `AtomicPtr` 完全一致，crossbeam 只是在其上包了一层。

#### 4.3.4 代码实践

**实践目标**：跑通「构造 → store → swap → load」的最小闭环，并亲手验证「store 不需要 Guard、load/swap 需要」。

**操作步骤**：

```rust
// 示例代码
use crossbeam_epoch::{epoch, Atomic, Owned, Shared};
use std::sync::atomic::Ordering::SeqCst;

fn main() {
    let a = Atomic::<u64>::new(10);

    // store 不需要 Guard：把值换成 Owned(20)
    a.store(Owned::new(20), SeqCst);

    {
        // load / swap 必须在 Guard 作用域内
        let guard = &epoch::pin();
        let p = a.load(SeqCst, guard);
        unsafe { println!("after store: {}", *p.deref()); } // 期望 20

        // swap 换入新值，拿回旧值
        let old = a.swap(Owned::new(30), SeqCst, guard);
        unsafe { println!("swapped out: {}", *old.deref()); } // 期望 20

        let now = a.load(SeqCst, guard);
        unsafe { println!("after swap: {}", *now.deref()); } // 期望 30

        // 回收：把旧值都交还所有权后 drop
        unsafe {
            drop(p.into_owned());
            drop(old.into_owned());
        }
    } // guard 在这里 drop（unpin）

    // Atomic 自己没有 Drop，最后取走所有权回收
    unsafe { drop(a.into_owned()); }
}
```

**需要观察的现象**：三行打印依次为 `20`、`20`、`30`。若你尝试把 `load` 写在 `pin()` 作用域之外，编译器会直接报生命周期错误——这正是 Guard 参数的安全价值。

**预期结果**：输出 `after store: 20` / `swapped out: 20` / `after swap: 30`。

**待本地验证**：上述示例未在本讲环境中实跑，请在依赖了 `crossbeam-epoch` 的实验 crate 中确认输出。

**额外探索**：尝试把上面的 `SeqCst` 全部改成 `Relaxed`，程序仍可能「碰巧」打印正确结果（因为单线程下没有竞争），但在多线程场景下这会变成数据竞争——你可以对照 `Shared::deref` 文档里 [src/atomic.rs:L1306-L1314](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L1306-L1314) 给的反例来理解为何 `Relaxed` 不安全。

#### 4.3.5 小练习与答案

**练习 1**：`load` 的参数写成 `_: &'g Guard`，运行时根本没用 Guard，那它存在的意义是什么？

**参考答案**：纯粹是**类型层面的生命周期约束**。它让返回的 `Shared<'g, T>` 借用自这个 Guard，从而编译器能保证「Shared 还在用，Guard 就不能先 drop」，杜绝悬垂使用。此外，调用者必须先 `pin()` 拿到 Guard，这隐含了「线程已进入 EBR 临界区、被读对象在 Shared 有效期内不会被回收」这一运行时安全保证（pin 的细节在 u3-l9）。两层含义叠加，才需要这个看似「无用」的参数。

**练习 2**：为什么 `store` 的签名里没有 Guard？

**参考答案**：`store` 只写入新指针，不返回任何 `Shared`，因此既不产生需要生命周期保护的借用、也不需要让调用者「在读期间保持 pin」。它的全部职责就是把一个 `Pointer`（`Owned` 或 `Shared`）压成 `*mut ()` 写进去，所以不需要 Guard。

**练习 3**：`load_consume` 相比 `load(Ordering::Acquire, ..)` 有什么潜在优势？

**参考答案**：consume 只对「数据依赖于该 load 结果」的后续操作建立顺序，而不像 acquire 那样建立全局 happens-before。在弱内存模型架构（如 ARM/POWER）上，consume 通常**不需要插入内存屏障指令**，因而更快。代价是「依赖关系」的定义较模糊、编译器优化容易破坏依赖，所以实践中 consume 用得较少，但在指针解引用这种天然存在数据依赖的场景里是安全且高效的。

---

## 5. 综合实践

把本讲三个模块串起来：构造一个**带 tag 的 `Atomic<u64>`**，走完「构造 → store 带 tag 的新值 → swap → load 读回 → 校验值与 tag」全流程，并用 `Debug` 输出交叉验证。

**实践目标**：综合运用 `new`/`from`、`with_tag`/`tag`、`store`/`swap`/`load`，亲手把「一个字里同时装下地址和 tag」可视化。

**操作步骤**：

```rust
// 示例代码
use crossbeam_epoch::{epoch, Atomic, Owned, Shared};
use std::sync::atomic::Ordering::SeqCst;

fn main() {
    // 1) 构造：从带 tag=1 的 Owned 出发
    let a = Atomic::<u64>::from(Owned::new(100u64).with_tag(1));
    println!("init        : {:?}", a); // raw=..., tag=1

    let guard = &epoch::pin();

    // 2) load 读回：值=100，tag=1
    let p = a.load(SeqCst, guard);
    unsafe { assert_eq!(*p.deref(), 100); }
    assert_eq!(p.tag(), 1);
    println!("load        : value=100, tag={}", p.tag());

    // 3) swap 换入一个带 tag=5 的新值
    let new = Owned::new(200u64).with_tag(5);
    let old = a.swap(new, SeqCst, guard);
    unsafe {
        assert_eq!(*old.deref(), 100); // 旧值
        println!("swap old    : value=100, tag={}", old.tag());
    }
    assert_eq!(old.tag(), 1);

    // 4) 再 load：值=200，tag=5
    let q = a.load(SeqCst, guard);
    unsafe { assert_eq!(*q.deref(), 200); }
    assert_eq!(q.tag(), 5);
    println!("after swap  : value=200, tag={}", q.tag());

    // 5) 回收（Atomic 无 Drop，务必手动）
    unsafe {
        drop(p.into_owned());   // 旧对象（值 100）
        drop(q.into_owned());   // 当前对象（值 200）
    }
    // 注意：old 也是值 100 的那个对象，已被 p.into_owned 取走，不能重复回收
    drop(guard);
}
```

**需要观察的现象**：每一步的 `value` 与 `tag` 都吻合；tag 与值互不干扰地共存于同一个 `Atomic` 里——这就是 tagged pointer 的威力。

**预期结果**：输出 `tag=1` → `tag=1`（swap 旧值）→ `tag=5`，对应值 `100 → 100 → 200`，所有断言通过。

**待本地验证**：综合示例未在本讲环境实跑，且涉及手动所有权回收（`old` 与 `p` 指向同一对象，不可双重 `into_owned`），请在本地用 `cargo run` 配合 `miri`（`cargo +nightly miri run`）确认无未定义行为与双重释放。

> 进阶思考：如果把上面的 `u64` 换成 `i8`，并把 `with_tag(5)` 留着，最后读回的 `tag()` 会是多少？（答：0，因为 `5 & low_bits::<i8>() = 5 & 0 = 0`。）

## 6. 本讲小结

- `Atomic<T>` 内部就是一个 `AtomicPtr<()>` 加 `PhantomData<*mut T>`；`PhantomData<*mut T>` 让它默认非 `Send`/`Sync`，再由 [src/atomic.rs:L279-L280](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L279-L280) 在 `T: Send+Sync` 时手动实现。
- 所有构造（`new`/`init`/`from(Owned)`/`from(Box)`/`from(Shared)`）最终都汇聚到私有的 `from_ptr`；`null()` 借助 `const_fn!` 宏在常量与非常量间切换。
- 可用 tag 位数 = `ALIGN.trailing_zeros()` = \(k\)，tag 范围 \([0, 2^{k}-1]\)；`low_bits` 给掩码，`compose_tag`/`decompose_tag` 互为逆操作，`map_addr` 用指针运算保留 provenance。
- tag 超出范围会被 `tag & low_bits` **静默截断**，不会 panic：这就是 i8 只能存 0 的原因（项目用 `valid_tag_i8`/`valid_tag_i64` 测试守护此契约）。
- `load`/`load_consume`/`swap` 必须传 `&Guard`，但 Guard 仅用于把返回的 `Shared<'g, T>` 绑定到生命周期 `'g`（运行时不读）；`store` 不返回 `Shared`，因此不需要 Guard。
- `Atomic<T>` **没有 `Drop`**，指向的对象必须用 `into_owned()` 或 `defer_destroy` 显式回收，否则会泄漏。

## 7. 下一步学习建议

本讲只覆盖了 `Atomic` 的「结构与基本读写」。要完整使用 `Atomic`，下一步建议：

- **u2-l6（Owned）** 和 **u2-l7（Shared）**：把另两位「剑客」补齐，理解 `Owned::into_shared`、`Shared::deref` 的 `unsafe` 契约与生命周期 `'g`。
- **u2-l8（CAS 与位运算）**：学习 `compare_exchange` / `compare_exchange_weak` 的返回值（`CompareExchangeValue`/`CompareExchangeError`）、`fetch_update` 的循环 CAS，以及 `fetch_and`/`fetch_or`/`fetch_xor` 如何只对 tag 低位做原子位运算——这是构造无锁数据结构（链表逻辑删除、队列）的核心工具。
- 之后进入 **u3 单元**：理解 `Guard` 背后的 pin 语义与 `defer_destroy` 延迟回收，把「为什么 Guard 存在时对象不会被回收」这层运行时含义彻底打通。
