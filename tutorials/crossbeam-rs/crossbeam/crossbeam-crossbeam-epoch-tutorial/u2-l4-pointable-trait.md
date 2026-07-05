# Pointable 与指针抽象：把任意类型变成「一个字」

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚**为什么并发数据结构必须用「一个字（one word）」来表示一个对象**，以及这个约束从何而来。
- 读懂 [`Pointable`] trait 的五个成员（`ALIGN`、`Init`、`init`、`as_ptr`、`as_mut_ptr`、`drop`）各自承担的职责与 unsafe 契约。
- 解释编译器为所有 `Sized` 类型自动生成的 `Pointable` 默认实现是如何用 `Box` 完成工作的。
- 理解 `[MaybeUninit<T>]` 这个动态大小数组是如何被「塞进一个字」的：关键是把长度 `len` 藏进堆内存头部，而不是放进指针。
- 动手验证 `Owned<[MaybeUninit<T>]>` 与普通 `Box<[T]>` 在内存布局上的根本区别。

> 承接：本讲在 u1-l3（你会用 `Atomic::new(1234)` 创建一个指向单个 `i32` 的指针）的基础上，往下挖一层——回答「为什么那个 `1234` 能被一个指针指向、并被放进 `Atomic`」，以及「换成数组该怎么办」。

## 2. 前置知识

本讲用到的几个基础概念，先用大白话过一遍：

- **机器字（word）**：CPU 一次能原子读写的固定大小单位，在 64 位机器上通常是 8 字节（即一个 `usize`/`*mut T` 的大小）。
- **原子操作（atomic operation）**：硬件保证「不可分割」地完成读/写/读改写。Rust 里 `std::sync::atomic` 系列就是它的抽象。**关键限制：原子操作一次只能动一个字。**
- **`Box<T>`**：堆上独占所有权指针。对 sized 类型 `T`，`Box<T>` 内部就一个字（数据指针）；但对 `Box<[T]>` 这种切片，它是**胖指针（fat pointer）**——两个字：一个数据指针、一个长度。
- **DST（dynamically sized type，动态大小类型）**：编译期不知道大小的类型，比如 `[T]`、`str`。它们不能直接放在栈上，只能藏在引用/指针背后，而引用/指针会因此变「胖」。
- **`MaybeUninit<T>`**：一块「还没初始化」的 `T` 大小的内存。它允许你先占位、稍后再写入，跳过「必须立刻给初值」的限制。
- **`Layout`**：描述一块堆内存「多大、按几字节对齐」的类型，是手动分配/释放内存时的必备参数。

一句话总结本讲要解决的问题：**原子操作只认一个字，但有些对象（比如数组）天然需要两个字才能描述；`Pointable` 就是用来把这个矛盾化解掉的 trait。**

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/atomic.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs) | 本讲的主战场。定义了 `Pointable` trait、为所有 sized 类型写的默认实现、`Array<T>` 与 `[MaybeUninit<T>]` 的实现，以及建立其上的 `Atomic`/`Owned`/`Shared` 三类指针。 |
| [src/alloc_helper.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/alloc_helper.rs) | 一个极简的「全局分配器」封装 `Global`，`Array<T>::init/drop` 就是调它来申请/释放堆内存。 |

> 本讲只读上面两个文件。`Atomic`/`Owned`/`Shared` 三类指针本身的细节留给 u2-l5 ~ u2-l8，本讲只在用到时点到为止。

## 4. 核心概念与源码讲解

### 4.1 Pointable trait 定义与语义

#### 4.1.1 概念说明

无锁数据结构（如 Treiber 栈、Michael-Scott 队列）的节点要在多个线程之间用**原子操作**交换。而原子操作一次只能动一个字。这意味着：**一个「可被原子地发布与读取」的对象，必须能被一个字所指代。**

对普通 sized 类型，这没问题——`Box<T>` 内部就是一个字的数据指针。但对外暴露一个统一抽象会更方便：无论是单个 `i32`，还是一个动态长度的数组，上层 `Atomic<T>` 都用同一套「单字指针 + 标记位」机制来管理。这个统一抽象就是 `Pointable`。

trait 文档注释把意图说得很直白（[src/atomic.rs:L97-L102](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L97-L102)）：它「generalizes `Box<T>`」，让对象在堆里、由单字指针拥有；同时还为 `[MaybeUninit<T>]` 提供了实现——通过把「数组长度 + 元素」一起放在堆里，再用指针指向这对组合。

#### 4.1.2 核心流程

`Pointable` 本质上是一套**资源管理协议**，描述一个堆对象从生到死的四个阶段：

```text
init(init_value)  -> *mut ()    // 1. 在堆上分配并（按需）初始化，返回不透明单字指针
as_ptr(ptr)       -> *const T   // 2. 把指针解成「只读访问」的真实地址（可能要做偏移）
as_mut_ptr(ptr)   -> *mut T     // 3. 把指针解成「可变访问」的真实地址
drop(ptr)                        // 4. 析构并释放堆内存
```

注意 `init` 返回的是 `*mut ()`（裸的、类型擦除的单字指针），而 `as_ptr`/`as_mut_ptr` 才把它「翻译」回真正指向 `T`（或 `[T]`）的地址。这个「间接层」正是为动态数组留的转圜空间——指针指向的位置未必就是 `T` 本身的起始（见 4.3）。

此外还有一个常量 `ALIGN`（[src/atomic.rs:L118-L119](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L118-L119)）。它记录「指向该类型的指针对齐到几字节」。对齐保证了地址的最低若干位一定是 0，于是这些「空闲低位」可以用来塞一个 tag（标记位），这就是后续 `Atomic` 能做 tagged pointer 的前提。可用 tag 位数与对齐的关系是：

\[
\text{可用 tag 位数} = \log_2(\texttt{ALIGN}) = \texttt{ALIGN.trailing\_zeros()}
\]

\[
\texttt{low\_bits}(T) = (1 \ll \texttt{ALIGN.trailing\_zeros()}) - 1
\]

> 这里的数学只是给你一个直觉：`ALIGN` 越大，能塞的 tag 越多。tag 的具体玩法属于 u2-l5 的内容，本讲只需记住「`ALIGN` 是 `Pointable` 提供给上层标记位机制的输入」。

#### 4.1.3 源码精读

trait 定义在 [src/atomic.rs:L117-L159](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L117-L159)，关键成员：

```rust
pub trait Pointable {
    const ALIGN: usize;          // 指针对齐字节数，决定可塞几位 tag
    type Init;                   // 「初始化参数」的类型（单值用 T，数组用 usize 长度）

    unsafe fn init(init: Self::Init) -> *mut ();         // 分配
    unsafe fn as_ptr(ptr: *mut ()) -> *const Self;       // 解指针（只读）
    unsafe fn as_mut_ptr(ptr: *mut ()) -> *mut Self;     // 解指针（可变）
    unsafe fn drop(ptr: *mut ());                         // 释放
}
```

每个方法的 `# Safety` 注释都约定了同一组前提（[src/atomic.rs:L131-L158](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L131-L158)）：

- `ptr` 必须由 `init` 产生；
- `ptr` 尚未被 `drop`；
- 不能并发地既读又写（`as_ptr` 与 `as_mut_ptr` 互斥）。

这套契约把「单字指针 + unsafe」的安全责任边界划清楚了：调用方负责保证指针有效、不双重释放、不并发冲突。

`ALIGN` 如何被上层消费，可以看工具函数 `low_bits`（[src/atomic.rs:L58-L62](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L58-L62)）：它用 `T::ALIGN` 算出「空闲低位掩码」，正是上面公式的直接落地。

#### 4.1.4 代码实践（源码阅读型）

**目标**：理解「单字」这个硬约束为什么逼出了 `Pointable`。

1. 打开 [src/atomic.rs:L97-L116](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L97-L116)，阅读 `Pointable` 的文档注释。
2. 想一想：如果线程 A 想原子地发布一个 `Box<[i32; 3]>` 给线程 B，但 `Box` 里藏着两个字的信息，单字原子操作装不下，该怎么办？
3. 把你的答案用一句话写下来（提示：把多余的信息挪进堆里）。

**预期结果**：你会得出「长度等附加信息必须放进堆、而不是放进指针」的结论——这正是 4.3 节 `Array<T>` 的设计动机。

#### 4.1.5 小练习与答案

**练习 1**：`Pointable::init` 为什么返回 `*mut ()` 而不是 `*mut Self`？

> **答案**：因为对于 `[MaybeUninit<T>]` 这类 DST，`*mut Self` 是胖指针（两个字），没法塞进 `Atomic` 的单字字段。返回 `*mut ()` 强制只保留「一个字」，附加信息（如数组长度）改由堆内的头部承载，需要时再由 `as_ptr` 重新拼回去。

**练习 2**：`ALIGN` 常量除了「记录对齐」，对上层还有什么用？

> **答案**：它告诉上层「指针对齐到多少字节」，从而推出地址最低几位一定是 0，可以把这几位挪用作 tag。`low_bits::<T>()` 就是据此算出 tag 掩码。

---

### 4.2 为 T: Sized 的默认实现（Box 包装）

#### 4.2.1 概念说明

如果每个类型都要手写 `Pointable`，会很繁琐。好在 sized 类型有「天然单字」的特性，于是库提供了一个**覆盖所有 sized 类型的 blanket 实现**（`impl<T> Pointable for T`）。它的策略极其朴素：**把所有脏活都转交给标准库的 `Box`。**

#### 4.2.2 核心流程

```text
init(value):  Box::new(value)  ->  Box::into_raw  ->  *mut ()
as_ptr(ptr):  ptr as *const T                          // 单字就是真实地址，无需偏移
as_mut_ptr:   ptr.cast::<T>()
drop(ptr):    Box::from_raw(ptr)  ->  drop(Box)         // Box 析构会释放内存并运行 T::drop
```

这里没有花招：指针指向的位置就是 `T` 本身，所以 `as_ptr`/`as_mut_ptr` 只是简单的类型转换，不需要做地址偏移。

#### 4.2.3 源码精读

默认实现见 [src/atomic.rs:L161-L181](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L161-L181)：

```rust
impl<T> Pointable for T {
    const ALIGN: usize = mem::align_of::<T>();
    type Init = T;

    unsafe fn init(init: Self::Init) -> *mut () {
        Box::into_raw(Box::new(init)).cast::<()>()      // 装箱 -> 取裸指针
    }
    unsafe fn as_ptr(ptr: *mut ()) -> *const Self {
        ptr as *const T
    }
    unsafe fn as_mut_ptr(ptr: *mut ()) -> *mut Self {
        ptr.cast::<T>()
    }
    unsafe fn drop(ptr: *mut ()) {
        drop(unsafe { Box::from_raw(ptr.cast::<T>()) }); // 交还 Box，让它负责释放
    }
}
```

要点：

- `ALIGN` 直接取 `mem::align_of::<T>()`，与 `Box` 的布局完全一致。
- `Init = T`：初始化参数就是值本身。
- `drop` 用 `Box::from_raw` 把裸指针「复活」成一个 `Box`，再立刻 `drop`——这样 `T` 的析构和内存释放都由 `Box` 标准流程处理，**正确性转嫁给标准库**。

正因为有这个 blanket 实现，u1-l3 里 `Atomic::new(1234)` 才能直接工作。其调用链是：

```text
Atomic::new(1234)                            // src/atomic.rs:L293
  -> Atomic::init(1234)                      // src/atomic.rs:L309
     -> Owned::init(1234)                    // src/atomic.rs:L1046
        -> T::init(1234)  // 即 <i32 as Pointable>::init，走 Box::new
```

#### 4.2.4 代码实践（阅读型）

**目标**：跟踪一次 `Atomic::new` 是如何最终落到 `Box::new` 的。

1. 从 [src/atomic.rs:L293-L295](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L293-L295) 的 `Atomic::new` 出发。
2. 跳到 [L309-L311](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L309-L311) `Atomic::init`，它调用 `Owned::init`。
3. 再看 [L1046-L1048](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L1046-L1048) `Owned::init`，它调用 `T::init(init)`。

**预期结果**：你能画出 `Atomic::new → Atomic::init → Owned::init → <T as Pointable>::init → Box::new` 这条链，并解释「为什么 `Atomic::new` 对任意 sized 类型都能用」——因为有 4.2 的 blanket 实现兜底。

#### 4.2.5 小练习与答案

**练习 1**：为什么默认实现的 `as_ptr` 只是 `ptr as *const T`，而 4.3 节数组版的 `as_ptr` 要做一堆偏移运算？

> **答案**：sized 类型的指针直接指向 `T` 的起始，无需调整；数组版指针指向的是堆里的 `Array<T>` 头部，真正的元素位于 `len` 之后，需要偏移并重新拼出胖指针 slice。

**练习 2**：`<T as Pointable>::drop` 里为什么用 `Box::from_raw` 而不是直接 `dealloc`？

> **答案**：因为除了释放内存，还必须运行 `T` 自身的析构（如 `T` 含堆分配时）。`Box::from_raw` + `drop` 能一并完成「运行 `T::drop` + 释放 `Box` 内存」，比自己手动 `dealloc` 更安全、更不容易漏掉析构。

---

### 4.3 Array\<T\> 与 [MaybeUninit\<T\>] 的 Pointable 实现

#### 4.3.1 概念说明

这是本讲的「核心机关」。我们要让一个**动态长度的数组**也能用单字指针指代。

普通 `Box<[T]>` 是胖指针（数据指针 + 长度），占两个字，没法塞进 `Atomic`。解决办法：**把「长度」从指针里挪走，藏进堆内存的头部。** 这样指针只需一个字——指向「头部」；长度则随对象一起待在堆里，要用时再从头里读出来。

为此库定义了一个内部类型 `Array<T>`（[src/atomic.rs:L202-L207](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L202-L207)）：一个 `repr(C)` 的结构，第一个字段是长度 `len`，紧随其后是零长数组 `elements: [MaybeUninit<T>; 0]` 作为「元素区起点」的占位标记。真实元素紧接在 `len` 之后追加分配。

源码注释画的布局图（[src/atomic.rs:L189-L196](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L189-L196)）非常清楚：

```text
         elements
         |
         |
------------------------------------
| size | 0 | 1 | 2 | 3 | 4 | 5 | 6 |
------------------------------------
```

注释还点明了它与 `Box<[T]>` 的本质区别（[src/atomic.rs:L198-L201](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L198-L201)）：`size` 在**分配内部**，而不是像 `Box<[T]>` 那样跟指针一起存在。

#### 4.3.2 核心流程

数组的完整生命周期（实现见 [src/atomic.rs:L219-L263](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L219-L263)）：

```text
init(len):
    1. layout = Array::<T>::layout(len)      // 算出 [len 头 + len 个元素] 的 Layout
    2. Global.allocate(layout)               // 申请一块连续堆内存
    3. 把 len 写入头部                        // ptr::addr_of_mut!((*ptr).len).write(len)
    4. 返回指向头部的 *mut ()

as_ptr(ptr):
    1. len = (*ptr).len                      // 从头部读回长度
    2. elements = &(*ptr).elements           // 元素区起点（在 len 之后）
    3. slice_from_raw_parts(elements, len)   // 现场拼出 &[MaybeUninit<T>]

as_mut_ptr(ptr): 同上，但拼出 &mut [MaybeUninit<T>]

drop(ptr):
    1. len = (*ptr).len
    2. layout = Array::<T>::layout(len)
    3. Global.deallocate(ptr, layout)        // 一次性释放整块
```

注意 `as_ptr`/`as_mut_ptr` 每次都要**先读 `len`、再现场拼胖指针 slice** 返回。也就是说「胖指针」是在使用时临时重建的，并不长期占用存储——这正是「单字存储 + 动态重建」的精髓。

`Layout` 怎么算？看 [src/atomic.rs:L209-L217](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L209-L217)：

```rust
fn layout(len: usize) -> Layout {
    Layout::new::<Self>()                              // 头部 Array<T>（= 一个 usize 的 len）
        .extend(Layout::array::<MaybeUninit<T>>(len).unwrap())  // 后接 len 个元素
        .unwrap().0
        .pad_to_align()                                // 末尾补齐对齐
}
```

`Layout::extend` 保证「头部 + 元素区」紧凑相邻且满足各自对齐；`pad_to_align` 把整体大小补到对齐倍数，确保 `dealloc` 时拿到的 `Layout` 与 `alloc` 时一致。

#### 4.3.3 源码精读

`init`（[src/atomic.rs:L224-L235](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L224-L235)）：分配后用 `addr_of_mut!` 写入 `len`，避免触发未初始化元素的 UB；分配失败则走 `handle_alloc_error`（让进程 abort，符合 `Box::new` 默认「分配失败即终止」的语义）。

```rust
unsafe fn init(len: Self::Init) -> *mut () {
    let layout = Array::<T>::layout(len);
    match Global.allocate(layout) {
        Some(ptr) => unsafe {
            let ptr = ptr.as_ptr().cast::<Array<T>>();
            ptr::addr_of_mut!((*ptr).len).write(len);   // 只写头部长度，元素区留空
            ptr.cast::<()>()
        },
        None => alloc::alloc::handle_alloc_error(layout),
    }
}
```

`as_ptr`（[src/atomic.rs:L237-L245](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L237-L245)）：注释特意提到用 `addr_of_mut` 是为了「stacked borrows / miri」的合规性——不创建中间可变引用，直接取字段地址。

```rust
unsafe fn as_ptr(ptr: *mut ()) -> *const Self {
    unsafe {
        let len = (*ptr.cast::<Array<T>>()).len;
        let elements =
            ptr::addr_of_mut!((*ptr.cast::<Array<T>>()).elements).cast::<MaybeUninit<T>>();
        ptr::slice_from_raw_parts(elements, len)         // 现场重建 slice 胖指针
    }
}
```

`drop`（[src/atomic.rs:L256-L262](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L256-L262)）：注意它**只 `deallocate`，不逐个 drop 元素**。这是因为元素类型是 `MaybeUninit<T>`（本身没有 `Drop` 副作用），且语义上元素可能尚未初始化。如果元素已初始化、需要析构，那是调用方的责任——这套数组抽象是「裸内存 + 长度」级别的。

```rust
unsafe fn drop(ptr: *mut ()) {
    unsafe {
        let len = (*ptr.cast::<Array<T>>()).len;
        let layout = Array::<T>::layout(len);
        Global.deallocate(NonNull::new_unchecked(ptr.cast::<u8>()), layout);
    }
}
```

这里的 `Global` 就是 [src/alloc_helper.rs:L7-L66](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/alloc_helper.rs#L7-L66) 那个对 `alloc::alloc::alloc/dealloc` 的薄封装。

库自身在 [src/atomic.rs:L1615-L1620](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L1615-L1620) 有一个最小测试 `array_init`，正好演示用法：

```rust
#[test]
fn array_init() {
    let owned = Owned::<[MaybeUninit<usize>]>::init(10);
    let arr: &[MaybeUninit<usize>] = &owned;
    assert_eq!(arr.len(), 10);
}
```

#### 4.3.4 代码实践（可运行）

**目标**：用 `Owned::<[MaybeUninit<i32>]>::init(10)` 分配数组，读取长度与元素地址，并从「指针大小」上证明它和 `Box<[i32]>` 的布局确实不同。

**操作步骤**：新建一个二进制 crate（`cargo new pointable_lab && cd pointable_lab`），在 `Cargo.toml` 加上路径依赖：

```toml
[dependencies]
crossbeam-epoch = { path = "../work/crossbeam-rs-crossbeam/crossbeam-epoch" }
```

把 `src/main.rs` 写成（示例代码，非项目原有）：

```rust
use crossbeam_epoch::Owned;
use std::mem::MaybeUninit;

fn main() {
    // 1. 分配一个 10 元素的 [MaybeUninit<i32>]。
    //    长度信息被写进了堆里 Array<i32> 的头部，Owned 本身只是一个字。
    let owned = Owned::<[MaybeUninit<i32>]>::init(10);
    let arr: &[MaybeUninit<i32>] = &owned;          // Deref 触发 as_ptr，现场重建 slice
    println!("len = {}", arr.len());                 // 预期：10

    // 2. 打印每个元素的地址，观察它们是否连续排布、步长是否为 4 字节（= size_of::<i32>()）
    let base = arr.as_ptr();                         // 指向 elements 区起点（len 之后）
    for i in 0..arr.len() {
        let p = unsafe { base.add(i) };
        println!("elem[{i}] @ {p:p}");
    }

    // 3. 关键对照：指针本身的大小
    println!(
        "size_of::<Owned<[MaybeUninit<i32>]>>() = {}",     // 预期：8（单字）
        std::mem::size_of::<Owned<[MaybeUninit<i32>]>>()
    );
    println!(
        "size_of::<Box<[i32]>>() = {}",                     // 预期：16（胖指针：ptr+len）
        std::mem::size_of::<Box<[i32]>>()
    );

    // owned 离开作用域时，Owned::drop -> <[MaybeUninit<i32>] as Pointable>::drop 释放堆内存。
}
```

**需要观察的现象**：

1. `len` 打印 `10`，证明 `as_ptr` 从堆头部正确读回了长度。
2. 相邻元素地址差恰好是 `4` 字节（`size_of::<i32>()`），证明元素区是紧凑连续的。
3. `arr.as_ptr()`（元素区起点）与 `Owned` 持有的「堆起始地址」之间相差 `8` 字节——那正是藏在堆头部、被指针「隐藏」掉的 `len: usize`。（你可以用 `&owned as *const _` 与 `arr.as_ptr()` 对比来佐证，但访问头部本身属于内部细节，不建议依赖其具体偏移。）
4. `size_of::<Owned<[MaybeUninit<i32>]>>>()` 远小于 `size_of::<Box<[i32]>>()`:**单字 vs 双字**。

**预期结果**（64 位平台）：

```text
len = 10
elem[0] @ 0x...b0
elem[1] @ 0x...b4   （相差 4）
...
size_of::<Owned<[MaybeUninit<i32>]>>() = 8
size_of::<Box<[i32]>>() = 16
```

> 具体地址值因运行而异；若你在本机得到不同的 `size_of`（例如 32 位平台为 4 与 8），说明指针宽度不同，结论「单字 vs 双字」依然成立。地址/大小的具体数值「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`Array<T>` 的 `#[repr(C)]`（[src/atomic.rs:L202](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L202)）去掉会怎样？

> **答案**：`repr(C)` 保证字段顺序为「`len` 在前、`elements` 紧随」，`as_ptr`/`drop` 都依赖这个固定偏移。去掉后编译器可能重排字段，偏移不再确定，`addr_of_mut!((*ptr).elements)` 取到的地址就和真实元素区错位，立即产生未定义行为。

**练习 2**：为什么 `elements` 字段写成 `[MaybeUninit<T>; 0]`（长度 0 的数组）？

> **答案**：这是一个「零大小占位符」，只用来锚定「元素区从这里开始」的偏移地址，本身不占空间。真正的元素是 `init` 时通过 `Layout::extend` 在 `len` 之后**额外追加分配**的（注释 [src/atomic.rs:L201](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L201)：「Elements are not present in the type, but they will be in the allocation」）。

**练习 3**：`Pointable::drop` 对 `[MaybeUninit<T>]` 只调用 `deallocate`、不逐个析构元素。这样安全吗？

> **答案**：对 `MaybeUninit<T>` 这种「可能未初始化、本身无 `Drop` 副作用」的元素是安全的。语义上这块数组是「`len` + 裸内存」，调用方若写入过需要析构的值，需自行负责回收——这套抽象不替你管 `T` 的析构。

## 5. 综合实践

把本讲三个模块串起来，完成一个「对照实验」小程序：

1. **part A（sized 路径）**：用 `Atomic::new(42i32)` 创建指针，`pin()` 后 `load` 出 `Shared`，`unsafe` 解引用读出 `42`。对照 [src/atomic.rs:L161-L181](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L161-L181) 解释：这条路径走的是 blanket 实现，指针直接指向 `i32`，无偏移。
2. **part B（DST 路径）**：用 `Owned::<[MaybeUninit<i32>]>::init(5)` 创建数组，`Deref` 读出 `len == 5`，打印元素区步长。对照 [src/atomic.rs:L219-L263](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/atomic.rs#L219-L263) 解释：这条路径走的是数组实现，指针指向 `Array<i32>` 头部，`as_ptr` 临时重建胖指针 slice。
3. 用 `size_of` 分别测量 part A 的 `Owned<i32>` 和 part B 的 `Owned<[MaybeUninit<i32>]>`，确认**两者都只占一个字**——这正是它们都能被 `Atomic` 单字原子操作管理的根本原因。

> 这个练习把「为什么 sized 与 DST 能统一在 `Pointable` 之下」「为什么两者都是单字」「长度信息去了哪里」三件事一次性具象化。运行结果中具体的地址/大小数值「待本地验证」，但「都占单字」这一结论应稳定成立。

## 6. 本讲小结

- 并发数据结构要求对象能用**一个字**指代，因为原子操作一次只认一个字——这是 `Pointable` 存在的根本理由。
- `Pointable` 是一套资源协议：`init`（分配）→ `as_ptr`/`as_mut_ptr`（解指针访问）→ `drop`（释放），外加 `ALIGN` 常量与 `Init` 关联类型。
- 对所有 sized 类型，库用 `impl<T> Pointable for T`（基于 `Box`）做了 blanket 兜底，指针直接指向 `T`，无需偏移。
- 动态数组 `[MaybeUninit<T>]` 的实现是机关所在：把长度藏进堆内 `Array<T>` 头部（`repr(C)`，`len` + 零长 `elements` 占位），用 `Layout::extend` 把元素区追加分配在 `len` 之后。
- 因此 `Owned<[MaybeUninit<T>]>` 是**单字**，而 `Box<[T]>` 是**双字胖指针**——这是两者布局上的本质区别。
- `as_ptr` 在使用时才「临时重建」胖指针 slice；`drop` 只释放裸内存，不管 `T` 的析构。

## 7. 下一步学习建议

- 接下来读 **u2-l5（Atomic 与 tagged pointer）**：本讲的 `ALIGN` 是怎么被 `low_bits`/`compose_tag`/`decompose_tag` 消费、变成可用的 tag 位的。
- 之后再进 **u2-l6（Owned）** 与 **u2-l7（Shared）**，看它们如何把 `Pointable` 的单字指针包装成三种语义不同的智能指针。
- 想加深对布局的理解，可顺带读 [src/alloc_helper.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-epoch/src/alloc_helper.rs)，理解 `Global::allocate/deallocate` 与 `Layout` 的配合方式。
