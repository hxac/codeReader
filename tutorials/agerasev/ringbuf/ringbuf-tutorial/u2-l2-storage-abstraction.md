# Storage 抽象：Array / Heap / Ref / Owning 四种存储后端

## 1. 本讲目标

学完本讲后，你应该能够：

- 说出 `unsafe trait Storage` 抽象的是什么——一段**连续存放 `MaybeUninit<T>` 的内存区**，以及它需要满足的三条安全约定。
- 区分 `Array<T, N>`、`Heap<T>`、`Ref<'a, T>`（以及作为「拥有型」基础的 `Owning` / `Slice`）这几种存储后端，知道各自的内存归属、生命周期与适用场景。
- 解释为什么 ringbuf 用 `MaybeUninit<T>` 而不是 `Option<T>` 来存放元素。
- 理解存储后端的选择如何决定一个缓冲区能否用于 `no_std`、甚至无堆（no-alloc）环境。

本讲只聚焦「数据到底存在哪、用什么类型表达」，不涉及读写索引的并发与同步（那是后续讲义的内容）。

## 2. 前置知识

在进入源码前，先用大白话建立两个直觉。

**直觉一：环形缓冲区 = 索引 + 一块固定长度的连续内存。**

上一讲（u2-l1）我们已经知道，ringbuf 用 `read` / `write` 两个索引来描述「哪些槽有数据、哪些槽是空的」。索引只是两个数字，真正装元素的那块内存是独立的。你可以把这块内存想象成一排固定数量的小格子，索引告诉你「从第几个格子开始读 / 写」。本讲要回答的正是：**这排格子用什么类型来表示？格子里的元素又用什么类型来装？**

**直觉二：有些格子「有内容」，有些格子「是空的」，但内存里每个格子始终存在。**

环形缓冲区的容量在创建时就固定了，运行期不会扩缩容。也就是说，无论缓冲区是空是满，那块内存的「物理格子」一直都在，只是有些格子当前装着有效元素、有些格子是未初始化的垃圾。Rust 里表达「可能是垃圾、也可能是有效值，且和 `T` 占同样大小」的类型，正是 `core::mem::MaybeUninit<T>`。这也是 ringbuf 不用 `Option<T>` 的关键原因，我们稍后在源码里展开。

**直觉三：存储后端 = 这块内存「从哪来」。**

- 内存可以来自栈上的定长数组（编译期就知道大小）。
- 可以来自堆（运行期才分配）。
- 可以是借用别人已有的一块切片（自己不拥有）。
- 也可以是「拥有」语义（自己负责析构）。

`Storage` trait 就是把这些「来源各异、但都是一段连续 `MaybeUninit` 内存」的东西，抽象成同一个接口，让上层的 `LocalRb` / `SharedRb` 不用关心内存到底从哪来。

> 名词速查：`MaybeUninit<T>`——Rust 标准库提供的类型，布局与 `T` 完全相同，但表示「这块内存可能尚未初始化」，读写它需要 `unsafe`。`UnsafeCell<T>`——Rust 内部可变性的最底层原语，告诉编译器「这块内存可能被别名修改，不要做某些优化」。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| `src/storage.rs` | 定义 `Storage` trait 与 `Ref` / `Owning` / `Array` / `Slice` / `Heap` 五个存储类型，是本讲的主战场。 |
| `src/utils.rs` | 一组操作 `MaybeUninit` 切片的 `unsafe` 辅助函数（如把 `[T;N]` 转成 `[MaybeUninit<T>;N]`），存储后端在构造时会用到。 |
| `src/alias.rs` | 把存储后端与 `SharedRb` 组合成对外别名 `StaticRb` / `HeapRb`，让你看清「换存储后端 = 换缓冲区类型」。 |
| `src/rb/shared.rs` | `SharedRb<S: Storage>` 的定义，展示存储 `S` 如何作为字段被嵌入缓冲区。 |
| `src/rb/macros.rs` | `rb_impl_init!` 宏，为不同存储后端统一生成构造器（如 `StaticRb::default()`、`HeapRb::new(cap)`）。 |
| `examples/static.rs` | 一个 `#![no_std]` 示例，只用 `StaticRb`（即 `Array` 存储），是本讲综合实践的参照。 |

## 4. 核心概念与源码讲解

### 4.1 Storage trait：把「连续 MaybeUninit 内存区」抽象成统一接口

#### 4.1.1 概念说明

`Storage` 要解决的问题是：上层缓冲区逻辑（读写、切片访问、批量搬运）对所有存储后端都是一样的，它只关心「给我一段连续的、长度固定的、元素类型为 `MaybeUninit<T>` 的内存，并能按区间取出切片」。至于这块内存是栈上数组、堆分配、还是借来的切片，上层根本不在乎。

于是 ringbuf 把这套共性抽成一个 trait：`Storage`。任何实现了它的类型，都能被塞进 `SharedRb<S>` / `LocalRb<S>` 当作存储后端。

这里有一个关键设计抉择需要你先记住：**`Storage` 是一个 `unsafe trait`**。它不是普通的 trait——实现者必须遵守一套「安全约定」，否则上层基于它写的 `unsafe` 代码会触发未定义行为（UB）。换句话说，`Storage` 把「保证内存布局正确」的责任交给了实现者。

#### 4.1.2 核心流程

`Storage` trait 对外暴露的契约可以归纳为三件事：

1. **告诉上层「元素类型是什么」**：通过关联类型 `type Item`。
2. **告诉上层「内存起点在哪、多长」**：通过 `as_mut_ptr()`（返回指向第一个 `MaybeUninit<Item>` 的裸指针）和 `len()`（返回槽位数量）。
3. **按区间取出只读 / 可变切片**：通过默认方法 `slice(range)` / `slice_mut(range)`，它们基于 `as_mut_ptr` 用 `slice::from_raw_parts` 构造。

其中最「反直觉」的一点是：`slice_mut` 的签名是 `fn slice_mut(&self, ...) -> &mut [MaybeUninit<Item>]`——**用 `&self`（不可变借用）却返回 `&mut` 切片**。这在普通 Rust 里是非法的，之所以能成立，靠的是 `Owning` 内部的 `UnsafeCell`（见 4.2）以及 ringbuf 上层对「切片永不重叠」的 SPSC 保证。这正是 `Storage` 必须是 `unsafe trait` 的核心原因。

#### 4.1.3 源码精读

先看 trait 定义本身及其安全约定（[src/storage.rs:7-55](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/storage.rs#L7-L55)）：

```rust
/// Abstract storage for the ring buffer.
///
/// Storage items must be stored as a contiguous array.
///
/// # Safety
///
/// Must not alias with its contents
/// (it must be safe to store mutable references to storage itself and to its data at the same time).
///
/// [`Self::as_mut_ptr`] must point to underlying data.
///
/// [`Self::len`] must always return the same value.
pub unsafe trait Storage {
    type Item: Sized;

    fn len(&self) -> usize;
    fn is_empty(&self) -> bool { self.len() == 0 }

    fn as_ptr(&self) -> *const MaybeUninit<Self::Item> {
        self.as_mut_ptr().cast_const()
    }
    fn as_mut_ptr(&self) -> *mut MaybeUninit<Self::Item>;

    unsafe fn slice(&self, range: Range<usize>) -> &[MaybeUninit<Self::Item>] { /* ... */ }
    unsafe fn slice_mut(&self, range: Range<usize>) -> &mut [MaybeUninit<Self::Item>] { /* ... */ }
}
```

三条 `# Safety` 约定逐条解读：

- **「Must not alias with its contents」**：存储对象自身和它内部的数据，可以被同时以可变引用持有。这就是允许「`&self` 产出 `&mut` 数据」的根本前提，实现者必须保证这一点不会引发别名 UB。
- **「`as_mut_ptr` must point to underlying data」**：返回的指针必须真指向数据起点，且（隐含地）在对象存活期间稳定有效。
- **「`len` must always return the same value」**：容量恒定不变——这是环形缓冲区「不扩缩容」的底层保证。

`as_mut_ptr`（[src/storage.rs:34](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/storage.rs#L34)）是唯一没有默认实现、必须由具体存储后端提供的方法。`slice` / `slice_mut`（[src/storage.rs:43-54](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/storage.rs#L43-L54)）则是带默认实现的 `unsafe` 方法——它们标注 `# Safety`：调用者必须保证取出的多个切片互不重叠，且非 `Sync` 元素不得被并发访问。

**为什么用 `MaybeUninit` 而不是 `Option`？** 看 trait 里处处出现的 `MaybeUninit<Self::Item>`：每个槽位都用 `MaybeUninit<T>` 表示。原因有二：

1. **零开销**：`MaybeUninit<T>` 与 `T` 布局完全一致，不引入 `Option` 那样的判别式（discriminant）开销。对一个可能装上百万个元素的缓冲区，每个槽省一个 tag 意义重大。
2. **初始化状态由索引描述，而非逐槽标记**：ringbuf 已经用 `read`/`write` 两个索引精确知道了「哪段是已初始化的」（即 `read..write` 区间）。既然索引已经表达了初始化状态，再给每个槽挂一个 `Option` 的 `Some/None` 就是冗余。`MaybeUninit` 把「是否初始化」这件事完全交给上层用索引来管理。

> 这一点在 `SharedRb::from_raw_parts` 的安全注释里也能印证（[src/rb/shared.rs:60-65](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs#L60-L65)）：它要求「`read..write` 区间内的项必须已初始化、区间外的项必须未初始化」——初始化边界正是用索引划定的。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**，目标是把 `Storage` 的契约在脑子里走一遍。

1. **实践目标**：确认你理解了「`&self` 产出 `&mut`」为何安全、以及三条 `# Safety` 约定的含义。
2. **操作步骤**：
   - 打开 [src/storage.rs:43-54](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/storage.rs#L43-L54)，阅读 `slice_mut` 的实现，注意它用 `slice::from_raw_parts_mut(self.as_mut_ptr().add(range.start), range.len())` 从裸指针造出一个 `&mut` 切片。
   - 思考：如果上层（环形缓冲区）把两个**重叠**的 `range` 分别交给两次 `slice_mut` 调用，会得到两个指向同一块内存的 `&mut`——这正是 Rust 最严重的别名 UB。所以 `slice_mut` 标注 `unsafe`、并把「不重叠」的责任推给调用方。
3. **需要观察的现象**：trait 把安全性拆成了两层——`Storage` 实现者负责「不自别名」（第一条约定），`slice_mut` 调用者负责「取出的切片不重叠」（方法级 `# Safety`）。
4. **预期结果**：你能用自己的话讲清「为什么 `Storage` 必须是 `unsafe trait`」——因为它要支撑一个违反常规借用规则的接口。
5. 运行结果：无需运行，纯阅读。

#### 4.1.5 小练习与答案

**练习 1**：`Storage` trait 里，哪两个方法是「没有默认实现、必须由存储后端亲自实现」的？  
**答案**：`as_mut_ptr`（[src/storage.rs:34](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/storage.rs#L34)）和 `len`（[src/storage.rs:24](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/storage.rs#L24)）。其余如 `is_empty`、`as_ptr`、`slice`、`slice_mut` 都有默认实现。

**练习 2**：为什么 ringbuf 不用 `Vec<Option<T>>` 来当存储？  
**答案**：一是 `Option<T>` 比 `T` 多判别式开销，不零开销；二是初始化状态已由 `read`/`write` 索引精确描述，逐槽再挂 `Some/None` 是冗余。`MaybeUninit<T>` 与 `T` 布局一致，把「是否初始化」交给索引管理，兼顾性能与正确性。

---

### 4.2 Owning 与 Array：静态数组存储后端

#### 4.2.1 概念说明

`Array<T, N>` 是 ringbuf 里最「朴素」的存储后端：它就是一段长度为 `N`、元素为 `MaybeUninit<T>` 的定长数组，**编译期就知道大小、不依赖堆分配**。这让它成为 `no_std` 甚至无堆（no-alloc）嵌入式场景的主力。

但在源码里你会发现 `Array` 并不是一个独立 `struct`，而是一个**类型别名**：

```rust
pub type Array<T, const N: usize> = Owning<[MaybeUninit<T>; N]>;
```

真正的 `struct` 是 `Owning<T>`——一个通用的「拥有型」包装器。`Array` 只是 `Owning` 套上一个定长数组；后面要讲的 `Slice<T>` 则是 `Owning` 套上一个不定长切片 `[MaybeUninit<T>]`。理解了 `Owning`，就同时理解了 `Array` 和 `Slice`。

#### 4.2.2 核心流程

`Owning<T>` 的构造流程：

1. 把任意 `T`（对 `Array` 来说就是 `[MaybeUninit<T>; N]`）包进 `UnsafeCell`，得到内部可变性。
2. 实现 `Storage`：`as_mut_ptr` 直接返回 `UnsafeCell::get()` 得到的裸指针；`len` 返回 `N`（编译期常量）。
3. 由于 `Owning` 拥有内部数据，它会在自身被 drop 时连带 drop 掉里面的 `T`——但注意，`T` 是 `[MaybeUninit<T>; N]`，`MaybeUninit` **永远不会**被自动 drop 其内容，所以这里析构的只是「数组容器」本身，元素的生命周期完全由上层用索引管理。

对外，`Array` 通过 `rb_impl_init!` 宏获得 `Default` 实现，于是 `StaticRb::<T, N>::default()` 就能造出一个空缓冲区（`read == write == 0`）。

#### 4.2.3 源码精读

先看 `Owning` 与 `Array` 的定义（[src/storage.rs:90-113](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/storage.rs#L90-L113)）：

```rust
pub struct Owning<T: ?Sized> {
    data: UnsafeCell<T>,
}
unsafe impl<T: ?Sized> Sync for Owning<T> where T: Send {}

impl<T> From<T> for Owning<T> {
    fn from(value: T) -> Self {
        Self { data: UnsafeCell::new(value) }
    }
}

pub type Array<T, const N: usize> = Owning<[MaybeUninit<T>; N]>;

unsafe impl<T, const N: usize> Storage for Array<T, N> {
    type Item = T;
    fn as_mut_ptr(&self) -> *mut MaybeUninit<T> { self.data.get().cast() }
    fn len(&self) -> usize { N }
}
```

要点：

- `Owning` 唯一字段是 `data: UnsafeCell<T>`（[src/storage.rs:91](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/storage.rs#L91)）。`UnsafeCell` 就是 4.1 里「`&self` 却能产出 `&mut`」的合法依据——它向编译器声明这块内存可能被别名修改。
- `as_mut_ptr` 用 `self.data.get().cast()` 拿到数组首元素指针（[src/storage.rs:107](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/storage.rs#L107)）。`.cast()` 把 `*mut [MaybeUninit<T>; N]` 衰退成 `*mut MaybeUninit<T>`。
- `len` 直接返回编译期常量 `N`（[src/storage.rs:111](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/storage.rs#L111)），完美满足 `Storage` 的「`len` 恒定」约定。

再看宏如何为 `Array` 生成构造器（[src/rb/macros.rs:3-13](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/macros.rs#L3-L13)）：

```rust
impl<T, const N: usize> Default for $type<crate::storage::Array<T, N>> {
    fn default() -> Self {
        unsafe { Self::from_raw_parts(crate::utils::uninit_array().into(), usize::default(), usize::default()) }
    }
}
```

`default()` 调用了 `utils::uninit_array()`（[src/utils.rs:9-11](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/utils.rs#L9-L11)）生成一个**未初始化**的 `[MaybeUninit<T>; N]`，`.into()` 经 `Owning::from` 包成 `Array`，再连同 `read=0, write=0` 喂给 `from_raw_parts`。于是 `StaticRb::<i32, 4>::default()` 就得到一个容量为 4 的空缓冲区。

`StaticRb` 本身只是 `SharedRb<Array<T, N>>` 的别名（[src/alias.rs:17](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/alias.rs#L17)），它完全不带 `alloc` 门控——这正是它能用于 `no_std` / no-alloc 的原因。

#### 4.2.4 代码实践

这是本讲的核心动手实践，参照 `examples/static.rs` 写一个不依赖堆的 `#![no_std]` 程序。

1. **实践目标**：亲手用 `StaticRb`（底层 `Array` 存储）跑通 push/pop，并验证它**不需要堆分配**。
2. **操作步骤**：
   - 阅读官方示例 [examples/static.rs:1-15](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/examples/static.rs#L1-L15)。它的关键几行：
     ```rust
     #![no_std]
     use ringbuf::{traits::*, StaticRb};
     fn main() {
         const RB_SIZE: usize = 1;
         let mut rb = StaticRb::<i32, RB_SIZE>::default();
         let (mut prod, mut cons) = rb.split_ref();
         assert_eq!(prod.try_push(123), Ok(()));
         assert_eq!(prod.try_push(321), Err(321)); // 满了，原样退回
         assert_eq!(cons.try_pop(), Some(123));
         assert_eq!(cons.try_pop(), None);
     }
     ```
   - 运行：`cargo run --example static`。
   - **验证不依赖堆**：执行 `cargo build --example static --no-default-features`。`--no-default-features` 会关闭默认的 `std`（连带关闭 `alloc`）。由于 `static` 示例在 `Cargo.toml` 里**没有** `required-features` 门控，它能在此模式下编译；而用 `HeapRb` 的 `simple` / `overwrite` 示例需要 `alloc`，会被跳过。
3. **需要观察的现象**：
   - 程序按 FIFO 顺序输出，`try_push` 在满时返回 `Err(321)`、`try_pop` 在空时返回 `None`。
   - `--no-default-features` 下 `static` 仍编译通过，说明 `Array` 存储确实不依赖 `alloc`。
4. **预期结果**：编译成功、断言全部通过。
5. 运行结果：待本地验证（请在你本机执行上述命令确认）。

#### 4.2.5 小练习与答案

**练习 1**：`Array<T, N>` 的真实类型是什么？为什么 `len()` 能在编译期确定？  
**答案**：`Array<T, N> = Owning<[MaybeUninit<T>; N]>`（[src/storage.rs:102](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/storage.rs#L102)）。因为 `N` 是 `const` 泛型，`len()` 直接返回常量 `N`（[src/storage.rs:111](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/storage.rs#L111)）。

**练习 2**：`StaticRb::default()` 造出的内存里，每个槽是已初始化还是未初始化？为什么安全？  
**答案**：全部未初始化（由 `utils::uninit_array()` 生成）。安全是因为它们被包成 `MaybeUninit`，而 `MaybeUninit` 不会自动被读或被 drop；上层只在 `read..write` 区间内读取，初始时 `read==write==0`，区间为空，所以绝不会读到未初始化内存。

---

### 4.3 Heap：堆分配存储后端

#### 4.3.1 概念说明

`Heap<T>` 把存储内存放到**堆**上。它适合「容量在运行期才能决定、或元素很多、放栈上不现实」的场景——这也是日常 `HeapRb` 最常用的原因。

与 `Array` 不同，`Heap` 的容量在运行期通过 `Heap::new(capacity)` 确定，分配发生在堆上。代价是：它依赖 Rust 的全局分配器，因此**必须开启 `alloc` feature**；在没有堆分配器的环境（裸机嵌入式）里，`Heap` 根本不存在（整个类型被 `#[cfg(feature = "alloc")]` 门控掉）。

#### 4.3.2 核心流程

`Heap<T>` 的构造流程：

1. 用 `Vec::<MaybeUninit<T>>::with_capacity(capacity)` 预留容量。
2. 由于 `Vec::with_capacity` 不保证 `capacity() == capacity`（实际可能更大），用 `set_len` 强制把长度对齐到请求的容量，再 `into_boxed_slice()` 转成定长的 `Box<[MaybeUninit<T>]>`。
3. 用 `Box::into_raw` 把 boxed slice 拆成裸指针 `ptr` + 长度 `len`，存进 `Heap` 结构体；drop 时再用 `Box::from_raw` 重建并释放。

这样 `Heap` 自身就只是一个「裸指针 + 长度」的瘦结构，真正的大块内存由全局分配器管理。

#### 4.3.3 源码精读

`Heap` 的定义与 `Storage` 实现（[src/storage.rs:133-153](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/storage.rs#L133-L153)）：

```rust
#[cfg(feature = "alloc")]
pub struct Heap<T> {
    ptr: *mut MaybeUninit<T>,
    len: usize,
}
#[cfg(feature = "alloc")]
unsafe impl<T> Storage for Heap<T> {
    type Item = T;
    fn as_mut_ptr(&self) -> *mut MaybeUninit<T> { self.ptr }
    fn len(&self) -> usize { self.len }
}
```

注意三个细节：

- 整个 `Heap` 被 `#[cfg(feature = "alloc")]` 门控（[src/storage.rs:133](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/storage.rs#L133)）——关掉 `alloc`，这个类型连同 `HeapRb` 一起消失。这就是「Heap storage 需要 alloc feature」的直接证据。
- `as_mut_ptr` 直接返回存储的裸指针 `self.ptr`（[src/storage.rs:148](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/storage.rs#L148)）。
- `len` 返回 `self.len`（[src/storage.rs:150](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/storage.rs#L150)），恒定不变（构造后再不改），满足 `Storage` 约定。

构造器 `Heap::new`（[src/storage.rs:155-164](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/storage.rs#L155-L164)）：

```rust
pub fn new(capacity: usize) -> Self {
    let mut data = Vec::<MaybeUninit<T>>::with_capacity(capacity);
    unsafe { data.set_len(capacity) };
    Self::from(data.into_boxed_slice())
}
```

这里 `set_len` 是 `unsafe` 的，安全前提是「这些槽虽然长度被算进 Vec，但内容是 `MaybeUninit`，不会被当成已初始化的 `T` 读取或 drop」——`MaybeUninit` 正好满足这一前提。

析构（[src/storage.rs:193-198](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/storage.rs#L193-L198)）用 `Box::from_raw` 把裸指针还原成 `Box` 再 drop，从而把内存归还分配器：

```rust
impl<T> Drop for Heap<T> {
    fn drop(&mut self) {
        drop(unsafe { Box::from_raw(ptr::slice_from_raw_parts_mut(self.ptr, self.len)) });
    }
}
```

最后，`HeapRb` 的别名同样带 `#[cfg(feature = "alloc")]`（[src/alias.rs:26-27](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/alias.rs#L26-L27)），`HeapRb::new(capacity)` 由宏生成（[src/rb/macros.rs:17-23](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/macros.rs#L17-L23)）。

#### 4.3.4 代码实践

这是一个**源码阅读 + 推理型实践**。

1. **实践目标**：说清「为什么 Heap storage 必须开 `alloc` feature」。
2. **操作步骤**：
   - 在 [src/storage.rs:133](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/storage.rs#L133) 看到 `Heap` 整体被 `#[cfg(feature = "alloc")]` 包住；文件开头 [src/storage.rs:1-2](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/storage.rs#L1-L2) 有 `#[cfg(feature = "alloc")] use alloc::{boxed::Box, vec::Vec};`。
   - 对比：`Array` / `Owning` / `Ref` 都**没有** `alloc` 门控，它们只用 `core` 里的类型。
   - 推理：`Heap::new` 用到 `Vec` 和 `Box`，二者来自 `alloc` crate，而 `alloc` crate 在没有堆分配器的目标上不可用。
3. **需要观察的现象**：用 `cargo build --no-default-features` 时，凡引用 `HeapRb` 的代码都会因类型不存在而编译失败；而引用 `StaticRb` 的代码照常通过。
4. **预期结果**：你能复述「`Heap` 依赖 `Vec`/`Box` → 依赖 `alloc` crate → 必须开 `alloc` feature」这条因果链。
5. 运行结果：无需运行，纯阅读推理。（若想验证，可写一个引用 `HeapRb` 的程序在 `--no-default-features` 下编译，应报「cannot find type `HeapRb`」之类错误，待本地验证。）

#### 4.3.5 小练习与答案

**练习 1**：`Heap::new(capacity)` 里为什么要调用 `unsafe { data.set_len(capacity) }`？  
**答案**：`Vec::with_capacity` 不保证 `capacity()` 等于请求的 `capacity`（可能更大）。为了让缓冲区容量精确等于请求值，代码先 `set_len(capacity)` 再 `into_boxed_slice()`，从而截出长度恰为 `capacity` 的 boxed slice（[src/storage.rs:157-163](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/storage.rs#L157-L163)）。这是安全的，因为元素是 `MaybeUninit`，不会被当作已初始化的 `T` 读取或 drop。

**练习 2**：`Heap<T>` 和 `Array<T, N>` 在「容量确定时机」上有何区别？  
**答案**：`Array` 的容量 `N` 是 `const` 泛型，编译期确定；`Heap` 的容量在运行期由 `Heap::new(capacity)` 的参数决定。

---

### 4.4 Ref 与 Slice：借用与不定长的存储后端

#### 4.4.1 概念说明

到目前为止的三种存储（`Array`、`Heap`、以及作为基础的 `Owning`）都**拥有**内部内存。还有一类常见需求：你已经有了一块 `MaybeUninit<T>` 的切片（比如来自别处分配、或一个静态缓冲区），只想让 ringbuf **借用**它，而不转移所有权。这正是 `Ref<'a, T>` 的用途。

另外，`Owning` 还能包装**不定长**切片 `[MaybeUninit<T>]`，这就是 `Slice<T>` 类型别名——它与 `Array` 同源（都是 `Owning<...>`），但长度在运行期才知道（通过切片自身的 `len()`），适合「拥有、但长度运行期才定」的场景。

| 存储后端 | 真实类型 | 是否拥有内存 | 容量确定时机 | 是否需 `alloc` |
| --- | --- | --- | --- | --- |
| `Array<T, N>` | `Owning<[MaybeUninit<T>; N]>` | 是 | 编译期（`const N`） | 否 |
| `Slice<T>` | `Owning<[MaybeUninit<T>]>` | 是 | 运行期 | 否 |
| `Heap<T>` | 自定义 `struct`（裸指针+len） | 是（堆） | 运行期 | **是** |
| `Ref<'a, T>` | 自定义 `struct`（裸指针+len+生命周期） | 否（借用） | 运行期 | 否 |

#### 4.4.2 核心流程

`Ref<'a, T>` 的流程：

1. 从一个 `&'a mut [MaybeUninit<T>]` 借用，记下它的裸指针和长度，并用 `PhantomData<&'a mut [T]>` 把生命周期 `'a` 绑定到 `Ref` 上。
2. 实现 `Storage`：`as_mut_ptr` 返回记录的指针；`len` 返回记录的长度。
3. 由于 `Ref` 不拥有内存，它 drop 时**不会**释放底层切片——内存归还由真正的所有者负责。

`Slice<T>` 的流程更简单：它就是 `Owning<[MaybeUninit<T>]>`，`as_mut_ptr` 用 `UnsafeCell::get()` 取指针，`len` 则通过 `NonNull` 从 DST（动态大小类型）切片头部读出运行期长度。

#### 4.4.3 源码精读

`Ref` 的定义与实现（[src/storage.rs:57-74](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/storage.rs#L57-L74)）：

```rust
pub struct Ref<'a, T> {
    _ghost: PhantomData<&'a mut [T]>,
    ptr: *mut MaybeUninit<T>,
    len: usize,
}
unsafe impl<T> Send for Ref<'_, T> where T: Send {}
unsafe impl<T> Sync for Ref<'_, T> where T: Send {}
unsafe impl<T> Storage for Ref<'_, T> {
    type Item = T;
    fn as_mut_ptr(&self) -> *mut MaybeUninit<T> { self.ptr }
    fn len(&self) -> usize { self.len }
}
```

要点：

- `_ghost: PhantomData<&'a mut [T]>`（[src/storage.rs:58](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/storage.rs#L58)）是个「幽灵字段」，运行期占 0 字节，纯粹为了让编译器把生命周期 `'a` 纳入 `Ref` 的借用检查——保证 `Ref` 活着期间，原切片不会被别人以可变方式同时借用。
- 构造通过 `From<&'a mut [MaybeUninit<T>]>`（[src/storage.rs:75-83](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/storage.rs#L75-L83)）完成，记录指针与长度。
- `Ref` 没有 `Drop` 实现——它只是借用，不负责释放。

再看 `Slice`（[src/storage.rs:120-131](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/storage.rs#L120-L131)）：

```rust
pub type Slice<T> = Owning<[MaybeUninit<T>]>;
unsafe impl<T> Storage for Slice<T> {
    type Item = T;
    fn as_mut_ptr(&self) -> *mut MaybeUninit<T> { self.data.get().cast() }
    fn len(&self) -> usize {
        unsafe { NonNull::new_unchecked(self.data.get()) }.len()
    }
}
```

它与 `Array` 的唯一差别在 `len`：`Array` 返回编译期 `N`，`Slice` 运行期通过 `NonNull::len()` 从 DST 切片头部读长度。

> 小贴士：`Ref` 与 `Slice` 都不带 `alloc` 门控，所以它们同样能用于 `no_std`。`Ref` 特别适合「把一块已有的静态/外部缓冲区接入 ringbuf」——这与 u8 单元（自定义 Storage、`from_raw_parts`）的二次开发主题紧密相关，这里先建立直觉。

#### 4.4.4 代码实践

这是一个**调用链追踪型实践**。

1. **实践目标**：理解 `Ref` 如何从外部切片借用而来，并与 `split_ref` 配合。
2. **操作步骤**：
   - 阅读 [src/storage.rs:75-83](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/storage.rs#L75-L83) 的 `From<&'a mut [MaybeUninit<T>]> for Ref<'a, T>`，确认它只是把切片的指针与长度抄进 `Ref`。
   - 阅读官方示例 [examples/global_static.rs:1-18](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/examples/global_static.rs#L1-L18)：它把一个 `static` 的 `StaticRb` 用 `OnceMut` 取出 `&mut`，再 `split_ref()` 拆成两端。这里 `split_ref` 返回的是**引用型**两端（不拿走所有权），正是因为缓冲区本身是 `static`、不能被 move。
   - 思考：如果这里用 `split()`（消耗所有权、包进 `Arc`）会怎样？——`static` 变量无法被 move，所以必须用 `split_ref`。这也是 `Ref` 借用语义的价值所在：在不拥有内存的前提下复用一块既有缓冲区。
3. **需要观察的现象**：`split_ref` 不触发任何堆分配，两端只是持有一个 `&'a StaticRb` 的引用。
4. **预期结果**：你能讲清「`Ref` 存的是指针+长度+生命周期，不拥有内存，drop 时不释放」。
5. 运行结果：`cargo run --example global_static`（依赖 dev-dependency `lock-free-static`），待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：`Ref<'a, T>` 里的 `_ghost: PhantomData<&'a mut [T]>` 起什么作用？去掉会怎样？  
**答案**：它把生命周期 `'a` 纳入 `Ref` 的类型，让借用检查器保证：`Ref` 存活期间，原 `&'a mut [MaybeUninit<T>]` 不会被别名。去掉后，编译器就不知道 `Ref` 与原切片的借用关系，`Ref` 就能逃逸出原切片的生命周期，导致悬垂指针 UB。

**练习 2**：`Slice<T>` 和 `Array<T, N>` 都基于 `Owning`，它们的 `len()` 实现区别是什么？  
**答案**：`Array` 的 `len` 直接返回编译期常量 `N`；`Slice` 的 `len` 运行期通过 `NonNull::len()` 从 DST 切片头部读取（[src/storage.rs:128-130](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/storage.rs#L128-L130)）。

---

## 5. 综合实践

把本讲四种存储后端串起来，做一个对比型小任务。

**任务**：在同一个测试程序里，分别用 `Array`（经 `StaticRb`）和 `Heap`（经 `HeapRb`）两种存储，各创建一个容量为 4 的 `i32` 缓冲区，写入 `0..4`、再读出，验证两者行为完全一致（FIFO、满则拒绝）。然后回答：

1. 两者的 `capacity()` 是否都等于 4？
2. 哪一种能在 `cargo build --no-default-features` 下编译？为什么？
3. 如果你要在一段已有的 `&mut [MaybeUninit<u8>; 64]` 上直接跑环形缓冲区，应该选哪种存储后端（提示：`Ref` 或自定义 `Storage` + `from_raw_parts`，详见后续 u8 单元）？

**操作提示**：

- `StaticRb::<i32, 4>::default()` 走 `Array`；`HeapRb::<i32>::new(4)` 走 `Heap`。
- 两者都 `use ringbuf::{traits::*, StaticRb, HeapRb};`（`HeapRb` 需 `alloc` feature，默认开启）。
- 用 `prod.try_push(x)` 写、`cons.try_pop()` 读，对比返回值。

**预期结果**：两种存储后端对外行为一致，差异只在「内存从哪来、是否需要 `alloc`、容量何时确定」。这正是 `Storage` 抽象的价值——**上层逻辑完全复用，只换底层存储**。

运行结果：待本地验证。

## 6. 本讲小结

- `Storage` 是一个 `unsafe trait`，抽象「一段连续存放 `MaybeUninit<T>`、长度恒定的内存区」，对外只暴露 `Item` 关联类型、`as_mut_ptr`、`len`，以及按区间取切片的默认方法 `slice` / `slice_mut`。
- 它的三条安全约定（不自别名、`as_mut_ptr` 指向数据、`len` 恒定）支撑了「`&self` 产出 `&mut` 切片」这一非常规接口，是整个缓冲区 `unsafe` 抽象的地基。
- ringbuf 用 `MaybeUninit<T>` 而非 `Option<T>` 存元素：零开销、布局与 `T` 一致，初始化状态交由 `read`/`write` 索引管理，不逐槽冗余标记。
- 四种存储后端各司其职：`Array<T,N>`（`Owning<[MaybeUninit<T>;N]>`，编译期容量、无需堆）、`Slice<T>`（`Owning<[MaybeUninit<T>]>`，运行期容量、无需堆）、`Heap<T>`（堆分配、需 `alloc`）、`Ref<'a,T>`（借用既有切片、不拥有内存、无需堆）。
- 存储后端决定了缓冲区能否用于 `no_std` / no-alloc：只有 `Heap` 依赖 `alloc`；`Array` / `Slice` / `Ref` 全程只用 `core`，可用于裸机。

## 7. 下一步学习建议

- 下一讲 **u2-l3「LocalRb vs SharedRb 与 HeapRb/StaticRb 别名」** 会把本讲的存储后端 `S` 放进 `SharedRb<S: Storage>` / `LocalRb<S: Storage>`，看索引如何与存储组合出完整的缓冲区，并解释 `alias.rs` 里 `HeapRb` / `StaticRb` 的类型定义。
- 想提前理解「初始化边界」如何与存储配合，可先读 [src/rb/shared.rs:60-84](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs#L60-L84) 的 `from_raw_parts` / `into_raw_parts` 安全约定。
- 对自定义存储后端感兴趣（如把外部 DMA 缓冲接入 ringbuf），可预留 u8 单元 **「自定义 Storage 与 from_raw_parts」**，那里会完整讲解如何亲自实现 `Storage`。
