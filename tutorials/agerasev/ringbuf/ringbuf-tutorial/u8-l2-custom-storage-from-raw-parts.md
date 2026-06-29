# 自定义 Storage 与 from_raw_parts：实现自己的存储后端

## 1. 本讲目标

本讲面向「二次开发」：当 ringbuf 内置的四种存储后端（`Array` / `Slice` / `Heap` / `Ref`）都不满足你的需求时——例如要把环形缓冲区建在**内存映射文件**、**外设 DMA 缓冲**、**一块既有的 `'static` 静态区**、或一段由别的分配器管理的裸内存上——你该如何接入。

学完后你应该能够：

- 说清 `unsafe trait Storage` 的三条安全契约，并解释每条为何必须成立。
- 知道为自定义存储实现 `Send` / `Sync` 时为什么用 `T: Send` 而非 `T: Sync` 作约束。
- 会用 `SharedRb::from_raw_parts(storage, read, write)`（以及 `LocalRb` 版本）从一段已有内存构造环形缓冲区，并理解 `read` / `write` 两个初始索引的语义与取值范围。
- 会用 `SharedRb::into_raw_parts()` 把缓冲区拆回「存储 + 两个索引」，并明确此时**谁负责析构已初始化的元素**。
- 能列出自己实现的 `Storage` 必须满足的全部 `unsafe` 前提。

---

## 2. 前置知识

本讲建立在两篇已有讲义之上，只做承接、不重复展开：

- **u2-l2（Storage 抽象）**：你已经知道 `unsafe trait Storage` 把「一段连续存放 `MaybeUninit<T>`、长度恒定的内存区」抽象成统一接口，元素用 `MaybeUninit<T>` 存储而非 `Option<T>`，初始化状态交给 `read` / `write` 索引管理。`read..write` 区间恒为已初始化区、`write..read+capacity` 恒为未初始化区。
- **u2-l3（LocalRb / SharedRb）**：你已经知道 `SharedRb<S>` 是泛型结构，把存储后端 `S` 作为类型参数，换 `S` 即得不同缓冲区；`HeapRb = SharedRb<Heap<T>>`、`StaticRb = SharedRb<Array<T,N>>` 都是别名。

补充几个本讲会反复用到、需要先建立直觉的概念：

- **二次开发（extension）**：在不改库源码的前提下，通过实现库暴露的 `unsafe` trait 或调用库的 `unsafe` 构造函数，让库操作你自己提供的资源。
- **裸指针 + 长度（ptr + len）**：自定义存储最常见的形式就是存一个 `*mut MaybeUninit<T>` 指针和一个 `usize` 长度，这正是库内置 `Ref` / `Heap` 的做法。
- **`ManuallyDrop`**：一个禁止编译器自动调用 `Drop` 的包装器，`into_raw_parts` 靠它「搬走」内部存储而不触发析构。
- **`ptr::read`**：按位复制搬走一个值，原始位置既不失效也不重复 drop——配合 `ManuallyDrop` 实现所有权转移。

如果上述术语有陌生的，先记住「它是一种绕过 Rust 默认所有权/析构机制、由人工接管安全责任的工具」，读完源码精读段就会明白。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/storage.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/storage.rs) | 定义 `unsafe trait Storage` 及内置四种后端 `Ref` / `Owning` / `Array` / `Slice` / `Heap`，是自定义存储的「契约书」。 |
| [src/rb/shared.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs) | 多线程 `SharedRb<S>`，含 `from_raw_parts` / `into_raw_parts` 两个 `unsafe` 构造与析构函数。 |
| [src/rb/local.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/local.rs) | 单线程 `LocalRb<S>`，结构与 `SharedRb` 完全对称的 `from_raw_parts` / `into_raw_parts`。 |
| [src/rb/macros.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/macros.rs) | `rb_impl_init!` 宏，用 `from_raw_parts` 统一生成 `new` / `Default` / `From<[T;N]>` 等构造器——是学习「正确的初始索引取值」的现成范例。 |
| [src/utils.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/utils.rs) | `array_to_uninit` / `uninit_array` 等辅助函数，构造器内部靠它们把已初始化数组转成 `MaybeUninit` 形态。 |

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：先讲 `Storage` 契约（自定义存储必须满足什么），再讲 `from_raw_parts`（怎么用外部内存建缓冲区），最后讲 `into_raw_parts`（怎么把缓冲区拆回外部内存）。

### 4.1 Storage trait：自定义存储后端的契约

#### 4.1.1 概念说明

ringbuf 把「数据存在哪」完全抽象成 `unsafe trait Storage`。内置的 `Heap`（堆）、`Array`（静态数组）、`Ref`（借用）只是几种现成实现；要接入一块 ringbuf 不知道的外部内存，你就自己写一个类型、为它 `unsafe impl Storage`。一旦实现成功，这块内存就能被 `SharedRb` / `LocalRb` 当成普通缓冲区使用，享受 push / pop / split / `io::Read` 等全部能力——存储后端对上层完全透明。

这是一切二次开发的入口：**实现一个 trait，换回一整套数据结构能力。**

#### 4.1.2 核心流程

实现一个自定义存储的步骤是固定的：

1. 定义一个结构体，通常持有「裸指针 + 长度」（`*mut MaybeUninit<T>` + `usize`），代表那段外部内存。
2. 实现 `unsafe trait Storage`：只需手写两个必需方法——`as_mut_ptr(&self)` 和 `len(&self)`；`slice` / `slice_mut` 有正确的默认实现，无需重写。
3. 视需要实现 `Send` / `Sync`（跨线程用 `SharedRb` 时必需），约束写在 `T: Send` 上（理由见 4.1.3）。
4. 用 `SharedRb::from_raw_parts(storage, read, write)` 把它包成环形缓冲区。

关键在于第 2 步那两个方法背后，trait 已经替你写好的默认 `slice` / `slice_mut`：它们用裸指针直接造出 `&[MaybeUninit<T>]` / `&mut [MaybeUninit<T>]`，因此「`&self` 却能产出 `&mut`」的非常规能力成立。这一切的合法性，全靠 trait 文档里那三条 `# Safety` 契约兜底。

#### 4.1.3 源码精读

先看契约本身——`Storage` 的安全说明与必需方法：

[storage.rs:11-34](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/storage.rs#L11-L34) 列出了三条安全契约，并定义了唯一的必需指针方法 `as_mut_ptr`（`len` 也是必需方法，紧随其后）。三条契约逐条解读：

- **「Must not alias with its contents」**：存储对象自身不能和它指向的数据重叠，且必须能同时安全地持有「指向存储本身的 `&mut`」和「指向其数据的 `&mut`」。这是允许 `slice_mut` 在 `&self` 上返回 `&mut` 的前提。
- **「`as_mut_ptr` must point to underlying data」**：返回的指针必须真正指向数据起点，且连续 `len` 个 `MaybeUninit<T>` 都可访问、对齐正确。
- **「`len` must always return the same value」**：容量在存储生命周期内恒定不变。

再看默认实现的 `slice_mut`，理解「`&self` 产 `&mut`」是如何靠上述契约合法化的：

[storage.rs:46-54](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/storage.rs#L46-L54) 用 `slice::from_raw_parts_mut` 从 `as_mut_ptr` 直接造出可变切片。注意签名上 `#[allow(clippy::mut_from_ref)]`——这正是「`&self` 返回 `&mut`」的告警，库靠契约而非借用检查器保证安全，所以必须手工允许。

最值得模仿的现成范例是 `Ref`（借用既有切片）：它就是一个「裸指针 + 长度 + 生命周期幻影」的结构体。

[storage.rs:57-74](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/storage.rs#L57-L74) 是 `Ref` 的定义与 `Storage` 实现，结构体只存 `ptr` / `len` / `_ghost`，`as_mut_ptr` 原样返回指针、`len` 原样返回长度。你的自定义存储可以照抄这个骨架。

最后看一个容易被忽略但跨线程时**必须处理**的细节——`Send` / `Sync`：

[storage.rs:62-63](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/storage.rs#L62-L63) 为 `Ref` 手写 `unsafe impl<T> Send for Ref<'_, T> where T: Send` 与 `Sync` 同样以 `T: Send` 为条件。[storage.rs:93](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/storage.rs#L93) `Owning`（即 `Array` / `Slice` 的本体）的 `Sync` 同样只要求 `T: Send`。

> **为什么 `Sync` 的约束是 `T: Send` 而不是 `T: Sync`？**
> 因为 `&Storage` 跨线程共享时，不同线程会经 `slice_mut` 拿到**不重叠**的 `&mut [MaybeUninit<T>]`（不相交由 SPSC 不变量保证），所以并不需要元素自己 `Sync`；但一个元素被线程 A 写入、由线程 B 读走，本质是把一个 `T` 的所有权「发送」给 B，这需要 `T: Send`。这与 `SharedRb` 文档注释 [shared.rs:29-30](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs#L29-L30) 的说法一致：「不显式要求 `T: Send`，直到你真的把 producer / consumer 发到另一个线程」。

#### 4.1.4 代码实践

**实践目标**：照着 `Ref` 的骨架，亲手为一段外部内存写一个最小的 `Storage`，验证它能让 `SharedRb` 正常 push / pop。

**操作步骤**（示例代码，需 `std`，可用 `cargo new --bin extstore` 后把 `ringbuf` 加为依赖）：

```rust
// 示例代码：用「裸指针 + 长度」实现一个最小自定义 Storage
use core::marker::PhantomData;
use core::mem::MaybeUninit;
use ringbuf::{storage::Storage, SharedRb, traits::*};

/// 持有一段「外部内存」的自定义存储（不负责分配/释放）。
struct ExtStorage<T> {
    ptr: *mut MaybeUninit<T>,
    len: usize,
    _marker: PhantomData<T>,
}

// 与内置 Ref/Heap 一致：跨线程安全以 T: Send 为条件。
unsafe impl<T: Send> Send for ExtStorage<T> {}
unsafe impl<T: Send> Sync for ExtStorage<T> {}

// 只需手写两个必需方法，slice/slice_mut 用默认实现。
unsafe impl<T> Storage for ExtStorage<T> {
    type Item = T;
    fn as_mut_ptr(&self) -> *mut MaybeUninit<T> {
        self.ptr
    }
    fn len(&self) -> usize {
        self.len
    }
}

fn main() {
    // 借 Box::leak 得到一段 'static 内存，模拟「外部 DMA / 内存映射」缓冲区。
    let buf: &'static mut [MaybeUninit<u8>] =
        Box::leak(Box::new([MaybeUninit::uninit(); 4]));

    let storage = ExtStorage {
        ptr: buf.as_mut_ptr(),
        len: buf.len(),
        _marker: PhantomData,
    };

    // 4.2 节会讲清这两个 0 的含义：空缓冲区 read=write=0。
    let mut rb = unsafe { SharedRb::from_raw_parts(storage, 0, 0) };
    let (mut prod, mut cons) = rb.split_ref();

    assert_eq!(prod.try_push(b'A'), Ok(()));
    assert_eq!(prod.try_push(b'B'), Ok(()));
    assert_eq!(cons.try_pop(), Some(b'A'));
    assert_eq!(cons.try_pop(), Some(b'B'));
    assert_eq!(cons.try_pop(), None);
    println!("custom storage works!");
}
```

**需要观察的现象**：程序应打印 `custom storage works!` 且全部断言通过，说明你的 `ExtStorage` 已被 `SharedRb` 当成合法存储后端。

**预期结果**：把堆内存（`Box::leak` 得到的 `'static` 切片）接入环形缓冲区成功；若把 `ExtStorage` 的 `Send`/`Sync` impl 删掉，跨线程用 `split()`（基于 `Arc`）会编译失败。

> 若你无法运行（例如缺 `ringbuf` 依赖或网络受限），这属于「待本地验证」；可改为「源码阅读型实践」：对照 [storage.rs:57-74](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/storage.rs#L57-L74) 逐行确认 `ExtStorage` 与内置 `Ref` 的字段、`as_mut_ptr`、`len` 一一对应即可。

#### 4.1.5 小练习与答案

**练习 1**：如果把上面 `ExtStorage` 的 `len` 改成运行期可变（例如每次返回不同值），会违反 `Storage` 的哪条契约？后果是什么？

> **答案**：违反「`len` must always return the same value」。容量变化会让 `2*capacity` 模数变化，`ranges()` 切片越界、空/满判定错乱，`advance_*_index` 推进后可能写入数组范围外的内存——属于未定义行为。

**练习 2**：为什么示例中 `Sync` impl 写成 `where T: Send` 而不是 `where T: Sync`？

> **答案**：见 4.1.3。跨线程共享时各线程拿到的是不相交切片（由 SPSC 不变量保证），不要求元素自身可并发读（`Sync`）；但元素会从一个线程「搬」到另一个线程，需要 `T: Send`。

---

### 4.2 from_raw_parts：用外部内存构造环形缓冲区

#### 4.2.1 概念说明

内置构造器（`HeapRb::new`、`StaticRb::default`、`From<[T;N]>` 等）都假定「从一块全新、全未初始化的内存」开始。但二次开发场景里，内存往往已经存在（DMA 缓冲、内存映射文件、外部静态区），甚至**里面已经有数据**（比如一段还没消费完的历史日志）。`from_raw_parts(storage, read, write)` 就是为这种场景准备的逃生舱：你提供存储 + 两个初始索引，它把它们组装成一个现成的环形缓冲区，跳过所有内置构造器。

#### 4.2.2 核心流程

`from_raw_parts` 的逻辑极其简单——本质是结构体字段赋值：

1. 校验 `storage` 非空（容量不能为 0）。
2. 把 `read` / `write` 直接存进原子索引（`SharedRb`）或 `Cell` 索引（`LocalRb`），**不做任何取模**。
3. 把两个 hold 标志初始化为 `false`（未占用）。
4. 把 `storage` 按值搬进结构体——从此这块内存由缓冲区「接管」。

关键认知有两个：

- **`read` / `write` 是「原始索引」，取值在 \([0,\,2\cdot\text{capacity})\) 区间**，不是物理槽位下标。空缓冲区传 `(0, 0)`；从第 0 槽起预填了 `n` 个元素则传 `(0, n)`。物理槽位永远等于 `索引 % capacity`（见 u2-l1）。
- **初始化边界由这两个索引划定**：`read..write` 区间内的元素必须**已初始化**，区间外必须**未初始化**。这条由调用者保证，库无法检查。

其正确性条件可用集合语言描述：令 \(c = \text{capacity}\)、\(r,w\in[0,2c)\)，则要求

\[
\text{物理槽位集合}\{r\bmod c,\ldots,(w-1)\bmod c\}\ \text{中的元素全部已初始化，其余全部未初始化，}
\]

且占用数 \((w-r+2c)\bmod 2c \le c\)。

#### 4.2.3 源码精读

`SharedRb::from_raw_parts` 是本模块主角：

[shared.rs:59-75](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs#L59-L75) 接收 `storage`、`read`、`write` 三参。注意 [shared.rs:67](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs#L67) 的 `assert!(!storage.is_empty())`——容量为 0 会让模数 \(2c=0\) 导致除零/取模异常，因此直接断言拒绝。其余三行只是把索引装进 `CachePadded<AtomicUsize>`、把 hold 标志置 `false`。安全契约写在 [shared.rs:60-65](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs#L60-L65)：`read..write` 内已初始化、外未初始化，且索引位置合法。

`LocalRb` 版本结构完全对称：

[local.rs:45-59](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/local.rs#L45-L59) 做同样三件事，只是索引存进 `Cell`、没有 hold 字段（`LocalRb` 的 hold 也是 `Cell<bool>`，藏在 `Endpoint` 里）。两者的安全注释措辞完全相同。

学「正确的初始索引取值」最好的范例是构造器宏——它演示了 `from_raw_parts` 在两种典型初态下该怎么传参：

[macros.rs:9-14](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/macros.rs#L9-L14) 是 `From<[T; N]>`：数组 `value` 全部已初始化，所以传 `(read, write) = (0, value.len())`——`read..write = 0..N` 正好覆盖整个已初始化数组。对照 [macros.rs:3-7](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/macros.rs#L3-L7) 的 `Default`：全未初始化，传 `(0, 0)`；[macros.rs:17-23](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/macros.rs#L17-L23) 的 `Heap::new` 同样传 `(0, 0)`。这三处就是「该传什么初始索引」的权威参照。

还要注意存储到容量的换算关系——`capacity()` 直接读 `storage.len()`：

[shared.rs:90-93](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs#L90-L93) 说明 `capacity == storage.len()`，所以索引合法区间 \([0,2\cdot\text{capacity})\) 完全由你的存储长度决定。

#### 4.2.4 代码实践

**实践目标**：用 `from_raw_parts` 构造一个**预填了数据**的缓冲区（模拟「外部内存里已有历史数据」），验证 `read` / `write` 初始索引的含义。

**操作步骤**（示例代码，承接 4.1.4 的 `ExtStorage`）：

```rust
// 示例代码：构造一个「半满」缓冲区，证明初始索引划定了初始化边界
use core::marker::PhantomData;
use core::mem::MaybeUninit;
use ringbuf::{storage::Storage, SharedRb, traits::*};

struct ExtStorage<T> {
    ptr: *mut MaybeUninit<T>,
    len: usize,
    _marker: PhantomData<T>,
}
unsafe impl<T: Send> Send for ExtStorage<T> {}
unsafe impl<T: Send> Sync for ExtStorage<T> {}
unsafe impl<T> Storage for ExtStorage<T> {
    type Item = T;
    fn as_mut_ptr(&self) -> *mut MaybeUninit<T> { self.ptr }
    fn len(&self) -> usize { self.len }
}

fn main() {
    // 准备一段外部内存，并预先把前 2 个槽写成有效数据。
    let mut raw: [MaybeUninit<i32>; 4] = [MaybeUninit::uninit(); 4];
    raw[0] = MaybeUninit::new(100);
    raw[1] = MaybeUninit::new(200);

    let storage = ExtStorage {
        ptr: raw.as_mut_ptr(),
        len: raw.len(),
        _marker: PhantomData,
    };

    // read=0, write=2 → 槽 0..2 已初始化（100, 200），槽 2..4 未初始化。
    let mut rb = unsafe { SharedRb::from_raw_parts(storage, 0, 2) };
    let (mut prod, mut cons) = rb.split_ref();

    assert_eq!(cons.occupied_len(), 2);      // 已有 2 个
    assert_eq!(cons.try_pop(), Some(100));   // 先进先出：先取到 100
    assert_eq!(cons.try_pop(), Some(200));
    assert_eq!(prod.try_push(300), Ok(()));  // 现在可继续写
    assert_eq!(cons.try_pop(), Some(300));
}
```

**需要观察的现象**：消费者不 push 就能立刻 pop 出 `100`、`200`，证明 `read=0, write=2` 让库把前两个槽识别为「已占用」。

**预期结果**：全部断言通过，`occupied_len()` 一开始就是 2。

> 同样，若无法本地运行，标为「待本地验证」，可改为阅读 [macros.rs:9-14](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/macros.rs#L9-L14) 与 [tests/init.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/tests/init.rs) 中 `from_array` 测试：后者断言 `From<[123,321]>` 后 `occupied_len()==2`，与本实践同理。

#### 4.2.5 小练习与答案

**练习 1**：调用 `SharedRb::from_raw_parts(storage, 0, 0)` 时，`storage.len() == 0` 会发生什么？为什么？

> **答案**：[shared.rs:67](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs#L67) 的 `assert!(!storage.is_empty())` 触发 panic。因为模数 \(2c=0\) 会让取模/除法异常，容量为 0 的环形缓冲区无意义。

**练习 2**：一个 `capacity=4` 的缓冲区，想表示「槽 2、槽 3 已有数据」，应该如何传 `read` / `write`？给出一组合法取值。

> **答案**：物理槽 2、3 对应原始索引 `2..4`，故传 `read=2, write=4`（均落在 \([0, 8)\) 内，占用数 2 ≤ 4）。也可传等价的 `read=6, write=0`（绕一圈），但 `(2, 4)` 最直观。前提是槽 2、3 确实已初始化、其余未初始化。

---

### 4.3 into_raw_parts：析构与初始化责任回收

#### 4.3.1 概念说明

`from_raw_parts` 是「拼装」，`into_raw_parts` 是它的逆运算——把一个环形缓冲区**拆回**「存储 + 两个索引」，把内存的控制权还给调用者。它解决的典型需求是：

- 回收底层存储（从 `HeapRb` 取回 `Heap<T>`，或从自定义存储取回你的外部内存句柄）。
- 读取当前 `read` / `write` 进度，便于持久化、重启后用 `from_raw_parts` 重建。
- 把存储迁移到另一个进程/设备，或换一种 RB 包装（如 `SharedRb` ↔ `LocalRb`）。

它的难点不在逻辑，而在**析构责任**：环形缓冲区本应在 `Drop` 时 `clear()` 掉所有占用元素，但 `into_raw_parts` 要把存储「完好地」交还给你，就必须**绕过这次自动清理**——于是「drop 已初始化元素」的责任就转移到了调用者肩上。

#### 4.3.2 核心流程

`into_raw_parts` 的执行可以理解为三步：

1. 用 `ManuallyDrop::new(self)` 包住自己，阻止编译器在函数末尾自动 `Drop`（否则会触发 `SharedRb::drop` → `clear()`，把占用元素全 drop 掉，存储里的数据就没了）。
2. 用 `ptr::read(&this.storage)` **按位搬走**存储字段，不触发任何析构；此时存储里的数据原封不动。
3. 通过 `Observer` 的 `read_index()` / `write_index()` 读出当前两个索引，与存储一起作为三元组返回。

返回后，调用者拥有这个三元组，必须保证：`read..write` 区间内那些已初始化的元素**被正确 drop**（除非你确实想接手这些值）。最常见的稳妥做法是：取出值后立即用 `mem::take` / 显式 drop，或把存储再喂回 `from_raw_parts` 重建一个会自动清理的缓冲区。

#### 4.3.3 源码精读

`SharedRb::into_raw_parts`：

[shared.rs:76-84](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs#L76-L84) 先 `ManuallyDrop::new(self)` 防止 [shared.rs:148-152](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs#L148-L152) 的 `Drop::drop`（它会 `self.clear()`）运行；再用 `ptr::read` 搬出 `storage`；最后用 `this.read_index()` / `this.write_index()`（即 `Observer` 方法，见 [shared.rs:96-102](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs#L96-L102)）读出当前进度。安全契约 [shared.rs:78-80](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs#L78-L80) 一句话点明：「Initialized contents of the storage must be properly dropped」——已初始化内容必须被正确析构，责任在调用者。

`LocalRb` 版本完全对称：

[local.rs:60-68](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/local.rs#L60-L68) 同样 `ManuallyDrop` + `ptr::read` + 两个索引，安全注释措辞一致。

注意它与 `Drop` 的关系——正因为绕过了 `Drop`，析构责任才转移：

[shared.rs:148-152](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs#L148-L152) 是 `SharedRb` 正常 `Drop` 时调用的 `clear()`。走 `into_raw_parts` 时这段**不会执行**，所以你拿回的存储里，`read..write` 区间的元素仍然「活着」，必须由你善后。

#### 4.3.4 代码实践

**实践目标**：用 `into_raw_parts` 把一个**含有数据**的缓冲区拆回三元组，亲眼看到「存储里还留着数据」「析构责任落到调用者」这两点。

**操作步骤**（示例代码）：

```rust
// 示例代码：into_raw_parts 回收存储与索引，并手动 drop 已初始化元素
use core::mem::MaybeUninit;
use ringbuf::{storage::Heap, SharedRb, traits::*};

fn main() {
    // 建一个堆缓冲区并写入 2 个元素。
    let mut rb = SharedRb::<Heap<String>>::new(4);
    {
        let (mut prod, _cons) = rb.split_ref();
        prod.try_push("hello".to_string()).unwrap();
        prod.try_push("world".to_string()).unwrap();
    }
    // 此时 read=0, write=2，槽 0、1 各持有一个 String。

    // 拆回三元组：绕过 Drop，存储里的 String 原封不动。
    let (storage, read, write) = unsafe { rb.into_raw_parts() };
    println!("read={read}, write={write}, capacity={}", storage.len());

    // ---- 析构责任在这里交接 ----
    // read..write = 0..2 内的两个 String 必须被正确 drop，否则内存泄漏。
    // 最稳妥：重建一个会自动清理的缓冲区，让它自然 Drop。
    let mut recovered = unsafe { SharedRb::from_raw_parts(storage, read, write) };
    let (_prod, mut cons) = recovered.split_ref();
    assert_eq!(cons.try_pop(), Some("hello".to_string()));
    assert_eq!(cons.try_pop(), Some("world".to_string()));
    // recovered 离开作用域时 Drop 会 clear()，元素被安全回收。
}
```

**需要观察的现象**：打印 `read=0, write=2, capacity=4`；随后从 `recovered` 能 pop 出 `hello`、`world`，证明数据在 `into_raw_parts` 时确实「原封不动」地保留在存储里。

**预期结果**：程序无内存泄漏地结束（可用 `valgrind` 或 Miri 验证无泄漏）。若你在 `into_raw_parts` 后**直接丢弃** `storage` 而不处理其中的 `String`，就会泄漏这两个堆字符串——这正是「析构责任转移」的体现。

> 标注：本实践涉及 `String`（需 drop 的非 `Copy` 类型），最能体现析构责任。若无法本地运行，标「待本地验证」，并阅读 [shared.rs:76-84](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs#L76-L84) 与 [shared.rs:148-152](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs#L148-L152) 对照理解：`into_raw_parts` 用 `ManuallyDrop` 跳过的，正是 `Drop` 里的 `clear()`。

#### 4.3.5 小练习与答案

**练习 1**：如果 `into_raw_parts` 返回的存储里 `read..write` 区间含有需要 drop 的元素（如 `String`），而调用者拿到存储后什么都不做就让它离开作用域，会发生什么？

> **答案**：那些元素不会被 drop（存储类型如 `Heap<String>` 只 drop 自身的分配，不会逐个 drop 其中的 `String` 内容），导致**内存泄漏**。这正是安全注释要求「Initialized contents must be properly dropped」的原因——析构责任已从 `SharedRb::drop` 转移到调用者。

**练习 2**：为什么 `into_raw_parts` 必须用 `ManuallyDrop::new(self)`，而不能让 `self` 自然结束？

> **答案**：若不阻止，函数末尾会执行 `SharedRb::Drop::drop` → `clear()`，把 `read..write` 区间的元素全部 drop 掉并推进索引，你拿回的存储里就只剩未初始化槽，数据丢失。`ManuallyDrop` 跳过这次 `Drop`，才能把「带数据的存储」原样交还。

---

## 5. 综合实践

把三个模块串起来，完成一个完整的「外部内存环形缓冲区」二次开发任务：

**任务**：模拟一块外设 DMA 缓冲区（一段 `'static` 的 `MaybeUninit<u8>` 内存），用自定义 `Storage` 接入 ringbuf，完成「写入 → 读出 → 回收」的全生命周期，并严格列出全部 `unsafe` 前提。

**要求**：

1. 实现 `ExtStorage<u8>`（4.1.4 骨架），包装 `Box::leak` 得到的 `'static mut [MaybeUninit<u8>; N]`，`N=8`。
2. 用 `SharedRb::from_raw_parts(storage, 0, 0)` 构造**空**缓冲区，`split_ref` 后写入字节序列 `b"RING"`。
3. 在写入后、消费前，用 `into_raw_parts` 把缓冲区拆成 `(storage, read, write)`，打印 `read` / `write`（应分别是 `0` 和 `4`），确认数据仍在；再 `from_raw_parts` 重建，继续消费验证得到 `R I N G`。
4. **列出你的 `ExtStorage` 与这次构造/析构必须满足的全部 `unsafe` 前提**（这是本任务的核心交付物）。

**参考答案——全部 unsafe 前提清单**：

实现 `ExtStorage<T>` 必须满足：

1. **不别名**：结构体自身与其指向的数据不重叠；持有 `&self` 时通过 `slice_mut` 产出 `&mut` 切片是安全的（我们只存 `ptr`+`len`，数据在别处，满足）。
2. **`as_mut_ptr` 有效**：指针指向连续 `len` 个 `MaybeUninit<T>`，对齐正确、可读写（`Box::leak` 得到的切片满足）。
3. **`len` 恒定**：容量在生命周期内不变（`ExtStorage` 字段不可变，满足）。
4. **`Send`/`Sync` 正确**：跨线程用时以 `T: Send` 为约束手动实现（`u8: Send`，满足）。
5. **内存独占**：这块内存同一时刻只被这一个 `Storage` 使用，不能同时挂到两个环形缓冲区（调用者保证）。
6. **生命周期**：内存存活不短于缓冲区（`'static` 满足）。

调用 `from_raw_parts(storage, 0, 0)` 必须满足：

7. `storage.is_empty() == false`（`N=8>0`，满足）。
8. `read..write = 0..0` 内无元素需初始化、整个缓冲区视为未初始化——与「全新 DMA 缓冲」一致（满足）。
9. `read`、`write` 落在 \([0, 2\cdot\text{capacity}) = [0,16)\) 内（`0,0` 满足）。

调用 `into_raw_parts` 必须满足：

10. 拆出的存储中，`read..write` 区间内已初始化的元素（本任务里是 4 个 `u8`，`Copy` 无需 drop）必须被正确处理——`u8` 无析构，故直接丢弃即可；若是 `String` 等则必须显式 drop 或重建缓冲区让其 `clear()`。

> 通过本任务你应体会到：`from_raw_parts` 与 `into_raw_parts` 是一对**对称的逃生舱**，把「内存从哪来、到哪去、初始化到哪、谁负责析构」的控制权完全交还给二次开发者；`Storage` 则是这一切的统一契约。

---

## 6. 本讲小结

- 自定义存储只需实现 `unsafe trait Storage` 的两个必需方法 `as_mut_ptr` / `len`，骨架可照抄内置 `Ref`（裸指针 + 长度）。
- `Storage` 有三条安全契约：不与自身数据别名、`as_mut_ptr` 指向有效数据、`len` 恒定；跨线程时还要手写以 `T: Send` 为条件的 `Send`/`Sync`（用 `Send` 而非 `Sync`，因为元素是跨线程「搬运」而非并发共享）。
- `from_raw_parts(storage, read, write)` 用外部内存构造缓冲区，`read`/`write` 是 `[0,2*capacity)` 内的原始索引，**划定了初始化边界**（区间内已初始化、外未初始化），容量为 0 会 panic。
- `into_raw_parts()` 是其逆运算，靠 `ManuallyDrop` + `ptr::read` 把「带数据的存储」原样交还，**析构责任随之转移给调用者**。
- 构造器宏 `rb_impl_init!` 里 `Default` 传 `(0,0)`、`From<[T;N]>` 传 `(0,N)`，是「初始索引该怎么填」的权威范例。

---

## 7. 下一步学习建议

- **学宏系统**：本讲反复引用的 `rb_impl_init!`、`impl_producer_traits!` 等宏，在 **u8-l3（宏系统：构造器与 io/fmt trait 的批量实现）** 会系统讲解——它们正是把 `from_raw_parts` 包装成各类安全构造器的关键。
- **学 no_std/no-alloc**：本讲的 `Box::leak` 依赖堆；若你想在嵌入式裸机上接入真正的 DMA 静态区，请结合 **u8-l1（feature flags、portable-atomic 与 no_std/no-alloc 支持）**，改用 `StaticRb` + `split_ref`（无堆拆分）路线。
- **继续阅读源码**：建议精读 [src/storage.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/storage.rs) 中 `Heap` 的 `Drop`（[storage.rs:193-198](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/storage.rs#L193-L198)），理解「存储自身的析构」与「元素析构」的层次区别——这正是 `into_raw_parts` 安全契约的背景知识。
- **测试保障**：写完自定义存储后，建议参考 **u8-l5（测试体系与 Miri）** 用 `scripts/miri.sh` 跑一遍，让 Miri 帮你检查裸指针/`MaybeUninit` 操作是否存在未定义行为。
