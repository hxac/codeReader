# AtomicCell 的 unsafe 与内存安全深析

## 1. 本讲目标

在前面的讲义里，我们已经知道 `AtomicCell<T>` 「做了什么」：

- u2-l1 讲清了它的公共 API 与 `UnsafeCell<MaybeUninit<T>>` 的字段布局。
- u2-l2 讲清了 `atomic!` 宏如何在编译期于「无锁路径」与「全局锁回退」之间二选一。
- u2-l3 讲清了回退路径中 67 把 `SeqLock` 的锁池选址与印戳（stamp）机制。

本讲要回答一个更深的问题：**这些 `unsafe` 凭什么是 sound（内存安全）的？**

读完本讲，你应当能够：

1. 为 `atomic_cell.rs` 里每一处 `unsafe` 块写出它成立所需的**安全性前提（SAFETY 契约）**，而不是停留在「能编译就行」。
2. 理解 `MaybeUninit<T>` 与 `mem::needs_drop::<T>()` 是如何**分工协作**，同时避免 double-drop（重复释放）与 leak（泄漏）的。
3. 理解 `ptr::read_volatile` 在乐观读路径里「读取可能未初始化、甚至可能被并发撕裂的值」为什么不会立刻引发 UB。
4. 理解 `compare_exchange` 为什么要求 `T: Eq`，以及「语义相等但字节不等」时为什么要重试。

本讲**不再重复** `SeqLock` 印戳机制的运作流程（那是 u2-l3 的内容），而是把 SeqLock 当作一个已知工具，专门分析它**在 unsafe 安全性论证里扮演的角色**。

## 2. 前置知识

本讲假设你已经读过 u2-l3，熟悉以下概念。这里只做最小回顾，不展开。

- **`unsafe` 与 soundness**：Rust 的 `unsafe` 块是「程序员向编译器立契约」——你在块内做一些编译器无法验证的操作（解裸指针、调用 `transmute`、访问 `union` 字段……），作为交换，你必须保证一组**安全性前提**成立。如果前提被违反，就是 unsound，程序可能产生未定义行为（UB）。一段 `unsafe` 代码是否 sound，取决于**所有可能的调用方式**是否都能满足它的前提。
- **`UnsafeCell<T>`**：Rust 标准库提供的「内部可变性」原语。它是唯一合法地通过 `&T` 修改内部数据的方式，也是 `AtomicCell`、`Cell`、`RefCell` 的基石。
- **`MaybeUninit<T>`**：一个「可能尚未初始化」的容器。它和 `T` 有相同的大小与对齐，但编译器**不会假设**它的字节是合法的 `T`。读取它必须显式 `assume_init()`（unsafe），由调用者担保里面确实是一个合法的 `T`。
- **`repr(transparent)`**：一种布局保证——被标注的结构体在内存里和它唯一的非零大小字段**完全等同**，可安全地互相 `transmute`。
- **SeqLock 印戳**：`SeqLock` 用单个 `AtomicUsize` 同时编码「锁位（最低位）+ 版本号」。读者先记下版本号，读完数据后再核对版本号；若期间写者改动过数据，版本号必然变化，读作废重试。详见 u2-l3。
- **`mem::needs_drop::<T>()`**：一个 const fn，返回 `T` 是否需要显式析构（即 `T` 或其字段是否实现了非 trivial 的 `Drop`）。`Copy` 类型恒为 `false`。

> 关键直觉：本讲讨论的所有 `unsafe`，安全性都建立在「**某些不变量（invariant）由其它安全代码维护**」之上。例如 `AtomicCell` 的字段永远是已初始化的合法 `T`，这个不变量由 `new`/`store`/`swap` 等安全 API 共同维护；`Drop` 与乐观读里的 `unsafe` 全都依赖它。理解这一点，比记住每条 SAFETY 注释更重要。

## 3. 本讲源码地图

| 文件 | 在本讲的作用 |
| --- | --- |
| `src/atomic/atomic_cell.rs` | 唯一主角。本讲剖析的全部 `unsafe` 都在这里：`transmute_copy_by_val` const hack、`Drop`、`store` 的 `needs_drop` 分流、`atomic_load`/`atomic_store`/`atomic_swap`/`atomic_compare_exchange_weak` 四个自由函数的回退与无锁分支。 |
| `src/atomic/seq_lock.rs` | 提供 `SeqLock` 与 `SeqLockWriteGuard`。本讲不重讲它的运作流程，只引用它的 `optimistic_read`/`validate_read`/`write`/`abort`，说明印戳如何在乐观读 unsafe 论证里充当「同步保证」。 |

补充说明：在 16/32 位指针宽度的目标上，`mod.rs` 会用 [`src/atomic/seq_lock_wide.rs`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/seq_lock_wide.rs) 替换 `seq_lock.rs`（用两个 `AtomicUsize` 拼出宽计数器防 wrap）。这是 u5-l3 的主题；本讲引用的 `seq_lock.rs` 是 64 位指针宽度的常规版本，两者对外的 `optimistic_read`/`validate_read`/`write`/`abort` 语义一致。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块，对应 spec 要求：

- **4.1 `transmute_copy_by_val` const hack**：解释那个为 const 上下文做的「按值 transmute」union hack，以及它依赖的 `repr(transparent)` 布局保证。
- **4.2 `Drop` 与 `needs_drop`**：解释 `MaybeUninit` + `needs_drop` 如何同时防 double-drop 与防泄漏，并连带讲清 `Send`/`Sync` 的契约。
- **4.3 `read_volatile` 与 `compare_exchange` 的安全论证**：剖析乐观读路径与 CAS 路径里最微妙的几处 unsafe，以及 SeqLock 印戳在其中扮演的「同步替身」角色。

### 4.1 `transmute_copy_by_val` const hack

#### 4.1.1 概念说明

`AtomicCell::into_inner(self) -> T` 要把 `AtomicCell<T>` 拆箱成内部的 `T`。这件事的本质是一次**类型转换**：从 `AtomicCell<T>`（也就是 `UnsafeCell<MaybeUninit<T>>`）取出底层 `T` 的所有权。

Rust 标准库做这类「按位复制并转移所有权」的正规手段是 `mem::transmute_copy`。但这里有两个障碍：

1. **`into_inner` 是 `const fn`**（见 [src/atomic/atomic_cell.rs:87](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L87)），而 `const fn` 里的 `transmute_copy` 直到 Rust 1.74 才稳定。`AtomicCell` 整体 MSRV 是 1.56，不能直接用。
2. 即便退一步用「先取引用再 copy」，由于字段类型是 `UnsafeCell<MaybeUninit<T>>`，在 const 上下文里**对它取引用**会触发「cannot borrow here, since the borrowed element may contain interior mutability」错误——这个限制直到 `const_refs_to_cell` 稳定（Rust 1.83）才解除。

于是作者写了一个**手写的 const 版 `transmute_copy`**：用一个 `#[repr(C)] union` 在 const 上下文里完成「按值」的类型重解释。这就是源码注释里说的 *“HACK: This is equivalent to transmute_copy by value, but available in const context”*。

> 为什么强调「**按值**（by-value）」？因为按值的转换**不创建对源值的引用**，从而绕开了上面第 2 个障碍。它直接「吞掉」`src` 的所有权，复制比特到 `dst`，再 forget 原值——语义上和 `mem::transmute` 完全一致。

#### 4.1.2 核心流程

`transmute_copy_by_val<Src, Dst>` 的执行流程：

1. 定义一个 `#[repr(C)]` 的 `union ConstHack<Src, Dst>`，两个字段都包 `ManuallyDrop`（防止 union 自动 drop 任一字段）。
2. 把入参 `src: Src` 放进 `src` 字段，构造 union 实例。
3. 读取 union 的 `.dst` 字段——这是 `unsafe`，因为编译器无法判断当前内存里到底是 `Src` 还是 `Dst` 的合法表示。
4. 用 `ManuallyDrop::into_inner` 把读出的 `Dst` 解出来返回。
5. 由于 `Src` 被包在 `ManuallyDrop` 里且从未被读取，它的析构**不会**被调用——这等价于 `mem::forget(src)`，正是 `transmute` 的语义。

它成立的前提（SAFETY 契约）和 `core::mem::transmute_copy` 完全一致：

- `size_of::<Src>() >= size_of::<Dst>()`（代码里用 `assert!` 在 const 上下文里强制检查）。
- `Src` 与 `Dst` 的比特布局兼容——即「把 `Src` 的比特重解释为 `Dst`」本身是合法的，不会产生非法的 `Dst` 值。

#### 4.1.3 源码精读

先看 union hack 本体（[src/atomic/atomic_cell.rs:103-118](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L103-L118)）：

```rust
#[inline]
#[must_use]
const unsafe fn transmute_copy_by_val<Src, Dst>(src: Src) -> Dst {
    #[repr(C)]
    union ConstHack<Src, Dst> {
        src: ManuallyDrop<Src>,
        dst: ManuallyDrop<Dst>,
    }
    assert!(mem::size_of::<Src>() >= mem::size_of::<Dst>());
    // SAFETY: ConstHack is #[repr(C)] union, and the caller must guarantee that
    // transmuting Src to Dst is safe.
    ManuallyDrop::into_inner(unsafe {
        ConstHack::<Src, Dst> {
            src: ManuallyDrop::new(src),
        }
        .dst
    })
}
```

要点逐条解读：

- **`union` 读取是 unsafe**：`ConstHack { src: ... }.dst` 这一步「写 `src`、读 `dst`」是访问 `union` 的非常规字段，编译器无法证明内存里是 `Dst` 的合法表示，因此必须 `unsafe`。SAFETY 注释把责任转嫁给调用者：「caller must guarantee that transmuting Src to Dst is safe」。
- **`#[repr(C)]`**：保证 union 的两个字段从同一地址开始（C 语言 union 语义）。没有它，布局不保证，按位重解释就不成立。
- **`ManuallyDrop` 双向包裹**：`src` 包 `ManuallyDrop` 防止 union 析构时 drop 掉源值（保留 `transmute` 的 forget 语义）；`dst` 包 `ManuallyDrop` 是因为 union 字段若含 `Drop` 类型会编译失败，包一层绕开该限制，读出后再 `into_inner` 还原。
- **`assert!` 而非运行期判断**：`assert!` 在常量求值里能直接触发编译期错误，等价于 `transmute_copy` 内置的大小检查。

再看唯一的调用点 `into_inner`（[src/atomic/atomic_cell.rs:120-127](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L120-L127)）：

```rust
// SAFETY:
// - Self is repr(transparent) over `UnsafeCell<MaybeUninit<T>>` and
//   `UnsafeCell<MaybeUninit<T>>` and `T` has the same layout.
// - passing `self` by value guarantees that no other threads are concurrently
//   accessing the atomic data
unsafe { transmute_copy_by_val(self) }
```

这里把 `Self = AtomicCell<T>` 当作 `Src`、`T` 当作 `Dst`。SAFETY 契约的两条前提：

1. **布局等价链**：`AtomicCell<T>` 是 `#[repr(transparent)]`（[src/atomic/atomic_cell.rs:32](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L32)），它的唯一字段是 `UnsafeCell<MaybeUninit<T>>`；而 `UnsafeCell<T>` 与 `MaybeUninit<T>` 各自都与其内部类型布局等同。于是 `AtomicCell<T>` 与 `T` 大小、对齐完全一致，满足 transmute 的布局前提。
2. **独占所有权**：`self` 按值传入，调用者交出了所有权，保证此刻没有其它线程在并发访问这份数据。这是 `into_inner` 之所以**整体安全**的关键——所有并发问题被「按值传入」这一签名保证消除了。

> 顺带注意：无锁路径上的 `atomic_load`/`atomic_store`/`atomic_swap` 也用 `mem::transmute_copy`（标准库版，非 const hack），见 [src/atomic/atomic_cell.rs:1041](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L1041)、[src/atomic/atomic_cell.rs:1080](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L1080)。它们走的是 `atomic!` 宏判定的原生原子类型分支，其 SAFETY 由 `can_transmute`（[src/atomic/atomic_cell.rs:949-953](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L949-L953)）在编译期保证 size 相等、align 足够，本质上是同一套「按位重解释」论证。

#### 4.1.4 代码实践

**实践目标**：亲手复现 const union hack，理解它和 `mem::transmute_copy` 的等价性，并验证「按值转换不取引用」是绕开 interior-mutability 借用错误的关键。

**操作步骤**（这是一段**示例代码**，可在 nightly 或较新 stable 上运行观察）：

```rust
// 示例代码：仿写 const transmute_copy_by_val
use std::mem::ManuallyDrop;

const unsafe fn my_transmute_copy_by_val<Src, Dst>(src: Src) -> Dst {
    #[repr(C)]
    union H<Src, Dst> {
        s: ManuallyDrop<Src>,
        d: ManuallyDrop<Dst>,
    }
    assert!(std::mem::size_of::<Src>() >= std::mem::size_of::<Dst>());
    ManuallyDrop::into_inner(unsafe {
        H::<Src, Dst> { s: ManuallyDrop::new(src) }.d
    })
}

const PAIR: (u16, u16) = (0x0102, 0x0304);
// 把 (u16,u16) 按位重解释为 u32（小端机上得到 0x03040102）
const AS_U32: u32 = unsafe { my_transmute_copy_by_val::<(u16, u16), u32>(PAIR) };

fn main() {
    println!("{:#010x}", AS_U32);
}
```

> 留意 `H::<Src, Dst> { s: ... }.d` 这一行：先写 `s` 字段、再读 `d` 字段，正是 `union` 按「写一个、读另一个」做按位重解释的写法，也是源码 `transmute_copy_by_val` 的核心手法。

**需要观察的现象**：

1. 修正后程序打印出一个固定的 `u32` 值（小端机为 `0x03040102`）。
2. 把 `my_transmute_copy_by_val` 改成「先 `&src` 再 `ptr::read`」的版本，并在 const 上下文里对一个含 `Cell` 字段的类型调用——你会复现源码注释里描述的 *“cannot borrow here, since the borrowed element may contain interior mutability”* 错误。

**预期结果**：你能解释为什么源码作者非要用 union + 按值 的写法，而不是简单地 `UnsafeCell::into_inner`（它在 const 上下文里当时还不稳定，见 [src/atomic/atomic_cell.rs:88-94](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L88-L94) 的注释）。

> 待本地验证：不同 Rust 版本对 const union 的支持边界略有差异。如果你在低于 1.56 的工具链上尝试，可能在别处先报错；以你本地工具链的实际编译器诊断为准。

#### 4.1.5 小练习与答案

**练习 1**：`transmute_copy_by_val` 里为什么要把 `dst` 字段也包一层 `ManuallyDrop`，明明我们想读出的就是 `Dst` 本身？

**参考答案**：因为 Rust 规定 `union` 的字段若实现了 `Drop`，则该 `union` 不可定义。`Dst` 是泛型，可能含 `Drop` 类型。包 `ManuallyDrop` 后字段不再「带 drop」，union 定义合法；读出后再用 `ManuallyDrop::into_inner` 还原所有权。`src` 包 `ManuallyDrop` 则是为了实现 `transmute` 的 forget 语义（源值不被 drop）。

**练习 2**：`into_inner` 的 SAFETY 注释写了「passing `self` by value guarantees that no other threads are concurrently accessing the atomic data」。请解释：为什么 `self` 按值传入就能消除并发？如果改成 `&self` 会怎样？

**参考答案**：`self: AtomicCell<T>` 按值传入意味着调用者必须拥有该 `AtomicCell` 的所有权，而所有权是独占的——既然我能 move 它，就不可能有别的线程还持有它的引用在做并发读写。若改成 `&self`，则可能多线程共享引用，此时把内部 `T` move 出来会与并发的 `load`/`store` 数据竞争，不再安全。

**练习 3**：`assert!(mem::size_of::<Src>() >= mem::size_of::<Dst>())` 这条断言能保证 transmute 一定安全吗？举一个「断言通过但 transmute 仍 UB」的例子。

**参考答案**：不能。它只检查了大小（`Src` 装得下 `Dst`），但没检查「比特重解释合法性」。例如把 `Src = u64 = 0xFFFF_FFFF_FFFF_FFFF` 转成 `Dst = char`，大小够、对齐够，但 `char` 必须是合法 Unicode 标量值，`0xFFFFFFFF` 不是，于是得到非法 `char`，属于 UB。这正是为什么函数标了 `unsafe`：布局前提由 `assert!` 兜底，**语义合法性**只能交给调用者担保。

---

### 4.2 `Drop` 与 `needs_drop`

#### 4.2.1 概念说明

`AtomicCell<T>` 的字段是 `UnsafeCell<MaybeUninit<T>>`。`MaybeUninit<T>` 有一个重要副作用：**它不会自动 drop 内部的 `T`**（因为编译器不假定里面是合法的 `T`，自然不敢 drop）。这对一个「值容器」是危险的——如果 `T` 是非 `Copy` 类型（例如 `Box<u32>`），`AtomicCell` 被 drop 时内部的 `Box` 就不会被释放，造成**内存泄漏**。

反过来，如果直接给字段类型换成 `UnsafeCell<T>`（不套 `MaybeUninit`），又会撞上另一个坑——在旧版 rustc 里，外部代码可能观察到「部分初始化」状态（见 [src/atomic/atomic_cell.rs:40-43](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L40-L43) 引用的 issue #833，已在 1.64 修复）。所以字段必须是 `MaybeUninit`。

于是矛盾出现：

- 想防泄漏 → 必须**自己** drop 内部 `T`。
- 想防 double-drop → 又不能让编译器**也**去 drop `T`。

`AtomicCell` 的解法是组合两件工具：

- `MaybeUninit<T>` 关掉编译器的自动 drop（防 double-drop）。
- 手写 `Drop for AtomicCell<T>`，在析构时**只对真正需要析构的类型**调用 `drop_in_place`（防泄漏）。
- 是否「需要析构」由 `mem::needs_drop::<T>()` 在编译期判定，`Copy` 类型零开销跳过。

这套机制同时还贯穿 `store`：非 `Copy` 类型的写入必须经 `swap` 回收旧值，否则旧值会泄漏。

#### 4.2.2 核心流程

把 `AtomicCell<T>` 的析构与写入画成一张「needs_drop 分流图」：

```
                  ┌───────────────────────┐
   drop(self) ───►│ needs_drop::<T>() ?   │
                  └───────────┬───────────┘
                  no(Copy)    │     yes(非Copy)
                   │          │     ┌─────────────────────────┐
                   ▼          └────►│ as_ptr().drop_in_place()│  ← 手动回收，防泄漏
              什么都不做              └─────────────────────────┘
              （MaybeUninit 已防 double-drop）

                  ┌───────────────────────┐
   store(val) ───►│ needs_drop::<T>() ?   │
                  └───────────┬───────────┘
                  no(Copy)    │     yes(非Copy)
                   │          │     ┌─────────────────────────┐
                   ▼          └────►│ drop(self.swap(val))    │  ← swap 返回旧值并立即 drop
              atomic_store          │  = 原子换入新值 + 回收旧值│
              （零开销直写）         └─────────────────────────┘
```

两条路径背后的同一个直觉：**只要 `T` 有析构语义，凡是「被替换下来的旧值」都必须有人显式回收**。`MaybeUninit` 保证编译器不会偷偷多做一次，`needs_drop` 保证我们自己不会漏掉一次。

#### 4.2.3 源码精读

**字段定义与 `MaybeUninit` 的理由**（[src/atomic/atomic_cell.rs:39-47](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L39-L47)）：

```rust
/// Using MaybeUninit to prevent code outside the cell from observing partially initialized state:
/// <https://github.com/crossbeam-rs/crossbeam/issues/833>
/// (This rustc bug has been fixed in Rust 1.64.)
///
/// Note:
/// - we'll never store uninitialized `T` due to our API only using initialized `T`.
/// - this `MaybeUninit` does *not* fix <https://github.com/crossbeam-rs/crossbeam/issues/315>.
value: UnsafeCell<MaybeUninit<T>>,
```

注意注释里两条**不变量**，它们是本模块其它 unsafe 成立的基石：

1. *“we'll never store uninitialized `T` due to our API only using initialized `T`”*——尽管字段是 `MaybeUninit`，但在 `AtomicCell` 的整个生命周期里，这块内存**始终装着一个合法的 `T`**。`new` 用 `MaybeUninit::new(val)` 初始化；`store`/`swap` 写入的也都是合法 `T`。这个不变量由安全 API 共同维护，是乐观读里 `assume_init` 敢调用的前提。

**`Send`/`Sync` 契约**（[src/atomic/atomic_cell.rs:50-51](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L50-L51)）：

```rust
unsafe impl<T: Send> Send for AtomicCell<T> {}
unsafe impl<T: Send> Sync for AtomicCell<T> {}
```

为什么只要求 `T: Send` 而不是 `T: Send + Sync`？因为 `AtomicCell` 的所有操作（`load`/`store`/`swap`）都是**把值搬进搬出**，而不是通过共享引用 `&T` 让多线程同时观察内部。也就是说线程间传递的是 `T` 的**所有权转移**（语义上），这只需 `Send`。要求 `Sync` 反而过度限制——例如 `AtomicCell<Cell<u32>>` 没意义，但 `AtomicCell<Box<u32>>` 应当合法（`Box: Send`）。这两行 `unsafe impl` 的 SAFETY 契约就是「所有跨线程访问都退化为值的转移，且转移本身由 `T: Send` 担保」。

**`store` 的 `needs_drop` 分流**（[src/atomic/atomic_cell.rs:177-185](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L177-L185)）：

```rust
pub fn store(&self, val: T) {
    if mem::needs_drop::<T>() {
        drop(self.swap(val));
    } else {
        unsafe {
            atomic_store(self.as_ptr(), val);
        }
    }
}
```

- 非 `Copy` 类型走 `swap`：`swap` 原子换入 `val` 并返回旧值，`drop(...)` 立即回收旧值，杜绝泄漏。
- `Copy` 类型走 `unsafe { atomic_store(...) }`：直接覆盖写，无需回收（旧值的比特被覆盖无副作用）。这里的 unsafe 完全委托给 `atomic_store`，其 SAFETY 见 4.3。

**`Drop` 实现**（[src/atomic/atomic_cell.rs:317-329](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L317-L329)）：

```rust
// `MaybeUninit` prevents `T` from being dropped, so we need to implement `Drop`
// for `AtomicCell` to avoid leaks of non-`Copy` types.
impl<T> Drop for AtomicCell<T> {
    fn drop(&mut self) {
        if mem::needs_drop::<T>() {
            // SAFETY:
            // - the mutable reference guarantees that no other threads are concurrently accessing the atomic data
            // - the raw pointer passed in is valid because we got it from a reference
            // - `MaybeUninit` prevents double dropping `T`
            unsafe {
                self.as_ptr().drop_in_place();
            }
        }
    }
}
```

逐条解读 SAFETY 注释里的三条前提：

1. **`&mut self` 排除并发**：`drop(&mut self)` 拿到的是独占可变引用，说明此刻没有其它线程访问该 cell（否则拿不到 `&mut`）。`drop_in_place` 安全。
2. **裸指针有效**：`self.as_ptr()` 由 `&mut self` 的字段经 `.get().cast::<T>()` 得来（[src/atomic/atomic_cell.rs:216-218](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L216-L218)），指针指向一块**已初始化的合法 `T`**（由 4.2.3 开头那条不变量保证），`drop_in_place` 读出并析构它合法。
3. **`MaybeUninit` 防 double-drop**：因为字段是 `MaybeUninit<T>`，编译器**不会**在 `AtomicCell` 析构链上自动 drop 内部 `T`。于是这里手动 `drop_in_place` 是**唯一**一次析构，不会与编译器的自动析构叠加成 double-drop。

把这三条与「`needs_drop` 守卫」合起来看：`needs_drop::<T>()` 为假（`Copy` 类型）时直接跳过，连手动析构都不做，于是对 `AtomicCell<usize>` 这类常见用法**零开销**；为真时才进入 unsafe 路径做一次受控析构。

#### 4.2.4 代码实践

**实践目标**：直观验证「不写 `Drop` 会泄漏、写了 `Drop` 且配合 `MaybeUninit` 不会 double-drop」。

**操作步骤**（**示例代码**）：

```rust
use std::sync::atomic::{AtomicUsize, Ordering};
use std::alloc::{alloc, Layout};

// 一个带 Drop 计数的类型，模拟 Box
struct Tracked {
    _id: usize,
}
static DROP_COUNT: AtomicUsize = AtomicUsize::new(0);
impl Drop for Tracked {
    fn drop(&mut self) {
        DROP_COUNT.fetch_add(1, Ordering::SeqCst);
    }
}

fn main() {
    {
        // 用 crossbeam 的 AtomicCell（正确实现 Drop）
        use crossbeam_utils::atomic::AtomicCell;
        let cell = AtomicCell::new(Tracked { _id: 1 });
        cell.store(Tracked { _id: 2 }); // 旧值应被 drop
        drop(cell);                      // 内部值应被 drop
    }
    println!("DROP_COUNT = {}", DROP_COUNT.load(Ordering::SeqCst));
    // 预期：2（store 时回收 id=1，cell drop 时回收 id=2）
}
```

**需要观察的现象**：

1. `DROP_COUNT` 应为 **2**：一次来自 `store` 内部 `swap` 返回旧值后的 `drop`，一次来自 `AtomicCell` 自身 `Drop`。
2. 把 `AtomicCell` 换成你自己写的「字段直接是 `UnsafeCell<T>`、不套 `MaybeUninit`、且手写 `Drop`」的版本，正常情况下会触发 double-drop（或编译器拒绝）。

**预期结果**：你能用一句话解释「`MaybeUninit` 负责**不做**，`needs_drop` 负责**判断要不要做**，二者一起精确地把析构次数钉死为 1」。

> 待本地验证：第二条「自己写错版本」的具体表现依你如何写错而定（可能是 panic、可能是 double-free 检测告警），以本地实测为准。

#### 4.2.5 小练习与答案

**练习 1**：如果 `AtomicCell<T>` 的字段直接写成 `UnsafeCell<T>`（不套 `MaybeUninit`），并且**不**手写 `Drop`，对 `AtomicCell<Box<u32>>` 会发生什么？

**参考答案**：这种情况下编译器会按常规规则在 `AtomicCell` 析构时自动 drop 内部的 `T`（即 `Box`），不会泄漏——但会失去 `MaybeUninit` 带来的「防止外部观察到部分初始化状态」的保护（issue #833 描述的旧 rustc bug）。作者选择 `MaybeUninit` + 手写 `Drop`，是为了同时满足「规避旧 bug」与「不泄漏」两个目标。

**练习 2**：`store` 对非 `Copy` 类型为什么必须走 `swap`，而不能像 `Copy` 类型那样直接 `atomic_store` 覆盖？

**参考答案**：直接覆盖会让旧值「凭空消失」——它的比特被新值覆盖，但没有任何人调用旧值的析构，造成泄漏（对 `Box` 即泄漏堆内存）。`swap` 原子地把新值换入、把旧值换出并返回，`drop(...)` 随即回收旧值，是唯一能在原子写入的同时正确回收旧值的写法。

**练习 3**：`AtomicCell<T>: Sync` 要求 `T: Send` 而非 `T: Sync`。请用「`AtomicCell<Box<u32>>`」这个例子说明为什么这样设计是对的。

**参考答案**：`Box<u32>: Send` 但 `Box<u32>` 并不 `Sync`（多线程共享 `&Box` 同时读写 `*Box` 不安全）。`AtomicCell` 的语义是「通过原子操作转移/拷贝值」，线程间并不通过 `&T` 共享访问内部，而是把 `T` 整个搬进搬出，这只需 `T: Send`。所以 `AtomicCell<Box<u32>>` 合法且有用；若强行要求 `T: Sync`，这类常见用法就被无谓地禁止了。

---

### 4.3 `read_volatile` 与 `compare_exchange` 的安全论证

#### 4.3.1 概念说明

这是本讲最微妙、也最能体现「unsafe 安全性论证」功夫的部分。它涉及回退路径里两个最棘手的操作：

1. **乐观读 `ptr::read_volatile`**：在 `atomic_load` 的回退分支里，读者**不加锁**地读取可能正被写者并发修改的内存。按 Rust 的内存模型，**数据竞争（data race）本身就是 UB**——哪怕你用 `volatile` 读、哪怕你读完检查不通过就丢弃，理论上都已经 UB 了。源码注释对此心知肚明（见下面 4.3.3 的注释原文）。为什么还要这么写？因为这是 stable Rust 在没有「任意大小原子类型」时的**务实折中**，配合 SeqLock 印戳把「读到撕裂值」的概率与危害压到最小。
2. **`compare_exchange` 的语义相等重试**：`AtomicCell::compare_exchange` 要求 `T: Copy + Eq`（[src/atomic/atomic_cell.rs:257](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L257)）。注意是 `Eq`（语义相等）而不是「字节相等」。底层原生原子 CAS 是**逐字节**比较的，但两个 `T: Eq` 相等的值，其二进制表示未必相同（典型例子是含有 padding 字节的结构体，或带冗余位的类型）。于是可能出现「CAS 报告失败、但拿回的旧值语义上等于 `current`」的假失败——此时必须重试，否则会向调用者误报 `Err`。

SeqLock 印戳在这两处都扮演「**同步替身**」的角色：它本身不消除数据竞争的 UB 性质，但它在**实践层面**保证「只要印戳没变，读到的就是一个完整、一致、写入已发布的值」，从而让 `assume_init` 与后续使用在「印戳校验通过」的前提下站得住脚。

#### 4.3.2 核心流程

**乐观读流程**（`atomic_load` 回退分支，已在 u2-l3 讲过流程，这里只标注 unsafe）：

```
let lock = lock(src);                      // 选一把 SeqLock（安全）
if let Some(stamp) = lock.optimistic_read() {   // 记下版本号（安全）
    let val = unsafe {                     // ← unsafe#A：read_volatile
        ptr::read_volatile(src as *mut MaybeUninit<T>)
    };                                     // 读到一个 MaybeUninit<T>（可能撕裂！）
    if lock.validate_read(stamp) {         // 印戳没变？（安全）
        return unsafe { val.assume_init() }; // ← unsafe#B：只有印戳不变才 assume_init
    }
}
let guard = lock.write();                  // 升级为持锁读（安全）
let val = unsafe { ptr::read(src) };       // ← unsafe#C：持锁读，无并发
guard.abort();                             // 没改数据，还原印戳（安全）
val
```

**CAS 流程**（`atomic_compare_exchange_weak`，无锁与回退两条分支都有 unsafe）：

- 无锁分支：先 `transmute_copy` 把 `current`/`new` 转成底层原子类型的表示，调原生 CAS；若失败，把返回值转回 `T`，用 `T::eq` 判断是「真失败」还是「语义相等的假失败」，后者更新 `current` 重试。
- 回退分支：取写锁 → `ptr::read` 读旧值 → `T::eq` 判断 → 相等则 `ptr::write` 写入并正常 drop guard（印戳 +2）；不等则 `guard.abort()`（印戳还原，不 +2）。

#### 4.3.3 源码精读

**乐观读路径**（[src/atomic/atomic_cell.rs:1043-1067](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L1043-L1067)）：

```rust
{
    let lock = lock(src as usize);

    // Try doing an optimistic read first.
    if let Some(stamp) = lock.optimistic_read() {
        // We need a volatile read here because other threads might concurrently modify the
        // value. In theory, data races are *always* UB, even if we use volatile reads and
        // discard the data when a data race is detected. The proper solution would be to
        // do atomic reads and atomic writes, but we can't atomically read and write all
        // kinds of data since `AtomicU8` is not available on stable Rust yet.
        // Load as `MaybeUninit` because we may load a value that is not valid as `T`.
        let val = unsafe { ptr::read_volatile(src.cast::<MaybeUninit<T>>()) };

        if lock.validate_read(stamp) {
            return unsafe { val.assume_init() };
        }
    }

    // Grab a regular write lock so that writers don't starve this load.
    let guard = lock.write();
    let val = unsafe { ptr::read(src) };
    // The value hasn't been changed. Drop the guard without incrementing the stamp.
    guard.abort();
    val
}
```

逐处 unsafe 论证：

- **unsafe#A：`ptr::read_volatile(src.cast::<MaybeUninit<T>>())`（[src/atomic/atomic_cell.rs:1054](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L1054)）**
  - **读成 `MaybeUninit<T>` 而非 `T`**：这是关键。`read_volatile` 把比特读进一个**不假定合法**的容器。即使读到的是两个写操作拼出来的「撕裂值」或非法 `T` 表示，由于类型是 `MaybeUninit<T>`，**不会**立刻产生「持有非法 `T`」的 UB——它只是一坨未解释的比特。
  - **`volatile`**：阻止编译器把这次读优化掉、重排或合并。它**不**消除数据竞争的 UB 性质——注释对此完全坦诚（*“In theory, data races are *always* UB”*）。这是 stable Rust 没有任意大小原子读取能力时的**务实妥协**，不是教科书式的完美方案。
  - **指针有效**：`src` 来自 `as_ptr()`，指向一块在 `AtomicCell` 生命周期内始终有效的内存（且按不变量始终是合法 `T`）。

- **unsafe#B：`val.assume_init()`（[src/atomic/atomic_cell.rs:1057](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L1057)）**
  - `assume_init` 的安全性前提是「`MaybeUninit<T>` 里确实装着一个合法的 `T`」。
  - **SeqLock 印戳扮演的角色**：`validate_read(stamp)` 通过意味着从 `optimistic_read` 到现在，没有任何写者进入临界区（否则 `state` 的 release store 会让印戳变化，见 [src/atomic/seq_lock.rs:85-93](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/seq_lock.rs#L85-L93)）。于是我们读到的比特属于「某个完整写操作发布后的稳定快照」，结合 4.2.3 的「字段恒为合法 `T`」不变量，`assume_init` 站得住。**印戳就是这里唯一的同步保证**。
  - 若 `validate_read` 不通过，则**不**调用 `assume_init`，撕裂的比特被丢弃，转入持锁读分支——这正是印戳机制「过滤撕裂读」的功能。

- **unsafe#C：`ptr::read(src)`（[src/atomic/atomic_cell.rs:1063](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L1063)）**
  - 此时持有写锁 `guard`，写者被排除，`ptr::read` 读到一个**确定无并发**的合法 `T`，安全性直接由锁保证。
  - 紧接着 `guard.abort()`（[src/atomic/atomic_cell.rs:1065](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L1065)，对应 [src/atomic/seq_lock.rs:76-82](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/seq_lock.rs#L76-L82)）：因为这次读**没有改动数据**，不能让印戳 +2，否则会无谓地作废其它正在乐观读的读者。`abort` 把 `state` 还原成加锁前的值（不递增版本号）。

把 A/B/C 串起来看：**SeqLock 印戳承担了「读到一个合法、一致、已发布的 `T`」的全部同步责任**。乐观读 unsafe 的安全性论证，本质是「印戳不变 ⇒ 读到的比特来自某个已完成的写 ⇒ 配合恒合法不变量 ⇒ `assume_init` 合法」。注释里坦白的「数据竞争理论上仍是 UB」是这条论证在 Rust 形式内存模型下的已知瑕疵，实践中靠印戳 + 持锁回退兜底。

**CAS 路径**（[src/atomic/atomic_cell.rs:1117-1168](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L1117-L1168)）。先看签名，注意 `T: Copy + Eq`：

```rust
unsafe fn atomic_compare_exchange_weak<T>(dst: *mut T, mut current: T, new: T) -> Result<T, T>
where
    T: Copy + Eq,
```

无锁分支（[src/atomic/atomic_cell.rs:1122-1153](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L1122-L1153)）：

```rust
{
    a = unsafe { &*(dst as *const _ as *const _) };
    let mut current_raw = unsafe { mem::transmute_copy(&current) };
    let new_raw = unsafe { mem::transmute_copy(&new) };

    loop {
        match a.compare_exchange_weak(current_raw, new_raw, AcqRel, Acquire) {
            Ok(_) => break Ok(current),
            Err(previous_raw) => {
                let previous = unsafe { mem::transmute_copy(&previous_raw) };
                if !T::eq(&previous, &current) {
                    break Err(previous);
                }
                // 语义相等但字节不等：用 previous 更新 current 重试
                current = previous;
                current_raw = previous_raw;
            }
        }
    }
}
```

理解「语义相等重试」的关键：

- 底层 `a.compare_exchange_weak` 是**逐字节**比较 `current_raw` 与目标内存。
- 假设 `current` 与内存里的值「语义相等」（`T::eq` 为真）但「字节不等」（例如结构体 padding 不同、或某种带冗余位的表示）。底层 CAS 会因为字节不同而返回 `Err(previous_raw)`，但 `previous` 与 `current` 用 `T::eq` 比是相等的。
- 这种「假失败」必须被吸收：把 `current` 更新成 `previous`、`current_raw` 更新成 `previous_raw`，重新尝试。否则会向 `AtomicCell::compare_exchange` 的调用者误报「值不匹配」，违反其文档承诺（见 [src/atomic/atomic_cell.rs:258-262](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L258-L262)：「If the current value equals `current`, stores `new`」——是语义相等）。
- 三处 `transmute_copy` 的 SAFETY 由 `can_transmute` 在编译期保证 `T` 与底层原子类型 size 相等、align 足够（u2-l2 已讲），布局重解释合法。

回退分支（[src/atomic/atomic_cell.rs:1154-1166](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L1154-L1166)）：

```rust
{
    let guard = lock(dst as usize).write();

    let old = unsafe { ptr::read(dst) };
    if T::eq(&old, &current) {
        unsafe { ptr::write(dst, new) }
        Ok(old)
    } else {
        // The value hasn't been changed. Drop the guard without incrementing the stamp.
        guard.abort();
        Err(old)
    }
}
```

- 持有写锁，无并发，`ptr::read`/`ptr::write` 安全性由锁保证。
- **直接用 `T::eq` 比较**（语义相等），不走字节比较，因此回退分支**不会**出现「语义相等却报失败」的假失败——这是它比无锁分支简单的地方。
- **印戳的角色再次出现**：成功时 guard 正常 drop，印戳 +2（[src/atomic/seq_lock.rs:85-93](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/seq_lock.rs#L85-L93)），让所有正并发乐观读的读者作废重试；失败时调 `guard.abort()` 还原印戳（[src/atomic/seq_lock.rs:76-82](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/seq_lock.rs#L76-L82)），因为数据没变，不该连累乐观读者。**是否 +2，取决于这次临界区是否真的改了数据**——这正是 `abort` 存在的全部理由。

#### 4.3.4 代码实践

这是 spec 指定的本讲核心实践：**审计 `atomic_load` 与 `atomic_compare_exchange_weak` 的回退路径，为每条 unsafe 列出 SAFETY 前提，并指出 SeqLock 印戳的角色**。

**实践目标**：把本讲的「安全性论证」内化为可执行的检查清单。

**操作步骤**：

1. 打开 [src/atomic/atomic_cell.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs)，定位 `atomic_load`（[L1033-L1069](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L1033-L1069)）的回退分支（`L1043-L1067`）和 `atomic_compare_exchange_weak`（[L1118-L1168](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L1118-L1168)）的回退分支（`L1154-L1166`）。
2. 为每一条 `unsafe` 语句，在本地新建一个文本文件，按下表填写 SAFETY 前提清单（参考格式如下）：

   | 行号 | unsafe 表达式 | SAFETY 前提 | 印戳角色 |
   | --- | --- | --- | --- |
   | 1054 | `ptr::read_volatile(src.cast::<MaybeUninit<T>>())` | (a) 指针来自 `as_ptr`，指向有效内存；(b) 读成 `MaybeUninit<T>`，故即便撕裂或非法也不立即 UB；(c) `volatile` 防优化消除。注：数据竞争理论上仍 UB，属已知瑕疵。 | 提供乐观读窗口；配合 validate 决定比特是否可用 |
   | 1057 | `val.assume_init()` | `validate_read(stamp)` 通过 ⇒ 自 optimistic_read 起无写者进入 ⇒ 读到的比特是某完整写的发布快照 ⇒ 配合「字段恒合法 T」不变量 ⇒ 合法 T。 | **唯一**同步保证 |
   | 1063 | `ptr::read(src)` | 持有 `lock.write()` 返回的 guard，写者被排除，无并发。 | 持锁提供排他；随后 `abort` 不递增印戳 |
   | 1157 | `ptr::read(dst)` | 持有写锁，无并发。 | 持锁 |
   | 1159 | `ptr::write(dst, new)` | 同上，持有写锁；`old` 已被读出供返回/比较。 | 成功则 guard drop 印戳+2；失败走 abort |

3. 针对 CAS 的无锁分支（L1122-L1153）做同样练习：列出三处 `transmute_copy` 的 SAFETY（`can_transmute` 编译期保证），并解释 `T::eq` 重试为什么是文档承诺（[L258-L262](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L258-L262)）所必需的。
4. （可选）用 `cargo +nightly miri test` 跑 `tests/atomic_cell.rs`，观察 miri 对回退路径数据竞争的报告——这会让你直观体会注释里 *“data races are always UB”* 那句话的分量（miri 下通常需配合 `crossbeam_atomic_cell_force_fallback` 强制走回退路径，见 u1-l3）。

**需要观察的现象 / 预期结果**：

- 你应能发现，回退路径里**所有**「无锁地读可能被并发修改的内存」的安全性，最终都**收敛到 SeqLock 印戳**这一个机制上——乐观读靠 `validate_read` 把关、持锁读靠 `write()` 排他。
- 你应能解释：`abort()` 与正常 `drop()` 的区别，正是「是否递增印戳」；而是否递增印戳，取决于临界区是否真的改了数据。这条规则同时服务于「乐观读者一致性」与「正确性」两个目标。

> 待本地验证：miri 是否报告、报告哪些位置，取决于 miri 版本与是否开启 `force_fallback`；以你本地诊断为准。本实践是「源码阅读 + 文档编写」型，不强求运行。

#### 4.3.5 小练习与答案

**练习 1**：`ptr::read_volatile` 为什么要把目标类型显式 cast 成 `*mut MaybeUninit<T>` 而不是直接 `*mut T`？

**参考答案**：因为读到的比特可能是被并发写者撕裂的、不构成合法 `T` 的值。若直接读成 `T`，则在「持有非法 `T` 表示」的瞬间就可能触发 UB（`T` 的某些位模式非法，如 `bool` 必须是 0 或 1、`char` 必须是合法 Unicode）。读成 `MaybeUninit<T>` 则把比特当作「未解释的原始字节」，不假定合法性，从而把「确认合法」的职责推迟到 `validate_read` 通过后的 `assume_init`，安全余地更大。

**练习 2**：在 CAS 的无锁分支里，假设把 `if !T::eq(&previous, &current) { break Err(previous) }` 这段判断去掉、直接 `break Err(previous)`，会带来什么语义问题？举一个会暴露该问题的场景。

**参考答案**：会向调用者误报「CAS 失败、当前值不等于 `current`」，但其实底层只是因为「`current` 与内存值字节不同（尽管语义相等）」而假失败。暴露场景：`T` 是一个含 padding 字节的 `#[derive(PartialEq, Eq)]` 结构体，两实例 `a == b` 但 padding 字节不同。调用者用 `current = a` 发起 CAS，内存里是 `b`（语义相等），本应成功写入；去掉 `T::eq` 判断后却返回 `Err(b)`，违反 `compare_exchange` 文档「If the current value equals `current`, stores `new`」的语义承诺。

**练习 3**：为什么 `atomic_compare_exchange_weak` 的回退分支在「比较失败」时必须调 `guard.abort()` 而不是让 guard 正常 drop？两者对并发乐观读者的区别是什么？

**参考答案**：比较失败意味着这次临界区**没有修改数据**。正常 drop 会让印戳 `wrapping_add(2)`（[src/atomic/seq_lock.rs:89-91](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/seq_lock.rs#L89-L91)），使所有正在乐观读的读者在 `validate_read` 处白白作废、被迫重试——虽然结果仍正确，但制造了不必要的重试开销。`abort()` 把 `state` 还原成加锁前的旧值（不递增版本号，[src/atomic/seq_lock.rs:76-82](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/seq_lock.rs#L76-L82)），让乐观读者「察觉不到」这次只读临界区，避免无谓作废。区别：正常 drop = 「数据改了，通知读者重读」；abort = 「数据没改，别打扰读者」。

## 5. 综合实践

把本讲三个最小模块串起来，完成一份**`AtomicCell` 回退路径 unsafe 安全性审计报告**。

任务：你是一名 reviewer，有人提交了一份「去掉 `MaybeUninit`、改用裸 `UnsafeCell<T>`，并删除手写 `Drop`、把乐观读改成 `ptr::read` 直接读成 `T`」的简化 PR。请基于本讲所学，写出一份 review 意见，至少覆盖：

1. **`transmute_copy_by_val` 维度**：删除 `MaybeUninit` 后，`into_inner` 的布局等价链（4.1.3 的两条 SAFETY）是否仍成立？字段类型变化是否影响 `repr(transparent)` 的 transmute 安全性？
2. **`Drop`/`needs_drop` 维度**：删掉手写 `Drop` 会导致 `AtomicCell<Box<u32>>` 出现什么问题（泄漏还是 double-drop）？`store` 的 `needs_drop` 分流是否仍合理？
3. **`read_volatile`/CAS 维度**：把乐观读改成 `ptr::read(src)`（直接读成 `T`）会引入什么新的 UB 风险？SeqLock 印戳能否像原来那样为 `assume_init` 担保？CAS 里去掉 `T::eq` 重试会违反哪条文档承诺？
4. 给出结论：该 PR 是否 sound？至少应要求作者保留哪些机制？

参考结论要点：该 PR 在三处均**不 sound**。

- 删 `MaybeUninit`：`Drop` 自动析构 + 显式析构会 double-drop；同时失去对 issue #833 部分初始化观察问题的防护。
- 删手写 `Drop`：对非 `Copy` 类型会泄漏（`MaybeUninit` 不自动析构，旧 PR 字段变了要重新分析）。
- 乐观读直接读成 `T`：读到撕裂值时立刻持有非法 `T` 表示（如非法 `bool`/`char`），即便印戳校验通过也无法挽回——印戳只能在「读成 `MaybeUninit`」的前提下担保合法性。
- CAS 去掉 `T::eq`：违反 `compare_exchange` 文档对「语义相等即视为匹配」的承诺。
- 至少应保留：`MaybeUninit` 字段、手写 `Drop` + `needs_drop`、乐观读的 `read_volatile`→`MaybeUninit`→`validate`→`assume_init` 三段式、CAS 的 `T::eq` 重试。

## 6. 本讲小结

- `into_inner` 用一个 `#[repr(C)] union` 的 **const transmute hack** 完成「按值」类型重解释，绕开 `const transmute_copy`（1.74 才稳定）与 const 上下文对 `UnsafeCell` 取引用（1.83 才稳定）两道限制；其 SAFETY 依赖 `repr(transparent)` 带来的布局等价链与「按值传入排除并发」。
- `MaybeUninit<T>` 与 `mem::needs_drop::<T>()` **分工协作**：前者关掉自动析构防 double-drop，后者判定是否需要手动析构防泄漏。`Drop` 与 `store` 共享这套分流，`Copy` 类型零开销。
- `AtomicCell<T>: Send + Sync where T: Send` 的契约是：所有跨线程操作都退化为值的转移，只需 `Send`，无需 `Sync`。
- 乐观读的 `ptr::read_volatile` 把比特读成 `MaybeUninit<T>`，**不假定合法性**；`assume_init` 的安全性**完全依赖 SeqLock 印戳**——`validate_read` 通过 ⇒ 读到的是某完整写的发布快照 ⇒ 配合「字段恒合法」不变量 ⇒ 合法 `T`。注释坦诚承认数据竞争理论上仍 UB，是 stable Rust 缺任意大小原子读取能力时的务实折中。
- `compare_exchange` 要求 `T: Copy + Eq`：底层原生 CAS 逐字节比较，会因「语义相等但字节不等」（如 padding）产生假失败，必须用 `T::eq` 吸收并重试，才能兑现文档「语义相等即匹配」的承诺；回退分支直接用 `T::eq` 比较故无此问题。
- `SeqLockWriteGuard::abort()` 与正常 `drop()` 的唯一区别是「是否让印戳 +2」；它服务于「只读临界区不应打扰乐观读者」这一正确性与效率目标。

## 7. 下一步学习建议

- **u5-l2 AtomicCell 算术运算的宏生成**：本讲只分析了「读/写/换/CAS」四个基础自由函数。`fetch_add`/`fetch_max` 等算术方法由 `impl_arithmetic!` 宏批量生成，每个方法都同时具备「原生原子 / `fetch_update` 回退 / 全局锁回退」**三条路径**，是本讲 unsafe 论证在「读改写」场景的延伸。
- **u5-l3 跨平台 cfg、loom 抽象与宽 SeqLock**：本讲多处提到「印戳 wrap」「stable Rust 没有任意大小原子」「miri/loom/force_fallback 强制回退」，这些 cfg 的来源（`build.rs`）与窄架构下用 `seq_lock_wide.rs` 双计数器防 wrap 的对策，在 u5-l3 系统讲解。
- **直接继续阅读源码**：带着本讲的 SAFETY 清单，回头重读 [src/atomic/atomic_cell.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs) 的 `atomic_store`（[L1075-L1088](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L1075-L1088)）与 `atomic_swap`（[L1094-L1108](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L1094-L1108)），尝试为它们的回退分支 `ptr::write`/`ptr::replace` 各写一份 SAFETY 注释——这是检验你是否真正掌握本讲的最佳自测。
