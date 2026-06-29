# MaybeUninit 与 unsafe 内存管理：直接访问内部内存

## 1. 本讲目标

本讲是专家层「内存安全」主题的核心一篇。读完后你应当能够：

1. 说清楚 ringbuf 为什么用 `MaybeUninit<T>` 而不是 `Option<T>` 来存放元素，以及「初始化状态」到底由谁来追踪。
2. 看懂 `Observer::unsafe_slices` / `unsafe_slices_mut` 与 `Storage::slice_mut` 这一对「从抽象索引区间到裸内存切片」的桥梁，并列出它们各自的 unsafe 安全契约。
3. 读懂 `src/utils.rs` 里那一族 unsafe 辅助函数（`slice_assume_init_ref/mut`、`write_slice`、`move_uninit_slice`、`array_to_uninit` 等），能解释每一个「为什么必须 unsafe」。
4. 理解 `advance_read_index` / `advance_write_index` 的本质：**索引推进 = 初始化状态推进**，并能手写出 `vacant_slices_mut + advance_write_index` 手动写入一个元素时，调用者必须满足的全部安全前提。

本讲不引入新的「能做什么」，而是把前面讲过的能力（Storage、Observer、Producer/Consumer）背后的 unsafe 地基讲透——它们是 ringbuf「直接访问内部内存」这一卖点的实现代价。

## 2. 前置知识

本讲建立在两篇前置讲义之上，假定你已经掌握：

- **u2-l2 Storage 抽象**：`unsafe trait Storage` 把「一段连续存放 `MaybeUninit<T>`、长度恒定的内存区」抽象成 `as_mut_ptr` / `len` / `slice` / `slice_mut` 四个方法；其中 `slice_mut(&self) -> &mut [...]` 这种「拿 `&self` 却返回 `&mut`」的非常规签名靠 `UnsafeCell` 实现内部可变性。
- **u3-l2 Producer trait**：写端的统一三步范式——观测 → 写入 `MaybeUninit` 空闲槽 → `advance_write_index` 提交；以及 `vacant_slices_mut` 把 `[write, read+capacity)` 区间切成最多两段线性切片。

此外你需要一点 Rust unsafe 的常识（本讲会顺带复习）：

- **`MaybeUninit<T>`**：标准库类型，表示「一块可能与 `T` 布局相同、但内容可能尚未初始化的内存」。编译器假定你不会去读一个未初始化的 `MaybeUninit`。
- **`unsafe` 块 / `unsafe fn`**：`unsafe` 不是「关掉检查」，而是「我（程序员）向你（编译器）立下某些契约，请你据此生成代码」。一旦契约被违反，就是**未定义行为（Undefined Behavior, UB）**，编译器有权做出任意假设。
- **`&self` 返回 `&mut T`**：常规 Rust 借用规则禁止，必须借助 `UnsafeCell`（编译器认定的「内部可变性逃生口」）才能合法，且使用时必须自己保证不产生重叠的可变引用。

> 本讲讨论的 `utils` 模块是 **crate 内部私有**的（见 [lib.rs:169](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/lib.rs#L169) 写的是 `mod utils;` 而非 `pub mod utils;`）。也就是说 `ringbuf::utils` 里的函数外部用户调不到——它们是库内部的积木。我们读它们是为了理解公开 API 背后的实现，而不是为了直接调用。公开的 unsafe 表面是 `Storage::slice_mut` 与 `Observer::unsafe_slices*`。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| `src/utils.rs` | crate 内部 unsafe 辅助函数族 | `MaybeUninit` 切片的各种转换与搬运，每个函数为何 unsafe |
| `src/storage.rs` | `Storage` trait 与四种存储后端 | `slice_mut` 的签名怪异性、`Owning` 用 `UnsafeCell` |
| `src/traits/observer.rs` | `Observer` trait | `unsafe_slices` / `unsafe_slices_mut` 的安全契约 |
| `src/traits/consumer.rs` | `Consumer` trait | `set_read_index` / `advance_read_index` 的契约、`as_slices` 如何 assume_init |
| `src/traits/producer.rs` | `Producer` trait | `set_write_index` / `advance_write_index` 的契约、`try_push` 范式 |
| `src/rb/shared.rs` | `SharedRb` 具体实现 | `unsafe_slices` 如何落到 `Storage::slice`、`set_write_index` 的 Release store |
| `src/rb/utils.rs` | `ranges` 函数 | 把环形区间切成两段线性范围（u2-l1 已讲，本讲引用） |
| `src/traits/utils.rs` | `modulus` 函数 | 模数 `2 * capacity`（u2-l1 已讲，本讲引用） |

## 4. 核心概念与源码讲解

### 4.1 为什么用 MaybeUninit 而不是 Option 存储元素

#### 4.1.1 概念说明

环形缓冲区的内存是一块**固定大小、反复复用**的区域。任何时刻，这块区域里的一部分槽装着「有效元素」（等待被消费），另一部分槽是「空的」（等待被写入）。于是每个槽有三种可能的状态：

1. 装着一个有效的、尚未被读走的 `T`；
2. 空的，等待被写入；
3. （理论上不该出现的）既未被写入、却被当成有效数据读取。

最朴素的 Rust 写法是用 `Option<T>`：`Some(x)` 表示有数据，`None` 表示空。但它有开销：

- `Option<T>` 对许多类型会额外占用一个「判别位」（或一个字），并且每次写入/读取都要构造/解构 `Option`。
- 更关键的是，`Option<T>` **本身就承担了「有没有值」的运行期记录**。而环形缓冲区已经用 `read` / `write` 两个索引精确知道哪些槽有效、哪些空——再用 `Option` 记一遍就是重复记账，纯属浪费。

ringbuf 的选择是 **`MaybeUninit<T>`**：它的内存布局与 `T` 完全一致（零额外开销），但它对编译器「诚实地」声明：**这块内存可能还没被初始化**。于是「哪些槽有效」不再由数据本身记录，而完全交给 `read` / `write` 索引来推导——这就是 u2-l1 讲过的占用数 `occupied = (2c + w - r) % 2c`。

一句话：**`Option<T>` 把「有效性」编码进数据；`MaybeUninit<T>` 把「有效性」外移给索引，换来了零开销，代价是读写必须用 unsafe 来兑现「我知道这个槽现在有效/无效」的承诺。**

#### 4.1.2 核心流程

把缓冲区看作一条环形带子，`read` 与 `write` 把它切成两段：

```
        read            write
         │                │
         ▼                ▼
[ ▓▓▓▓▓▓▓▓ ________________ ]   <- 一圈 capacity 个槽
  ↑ occupied（已初始化）  ↑ vacant（未初始化）
  read..write            write..read+capacity
```

- `read..write` 区间内的槽**已初始化**（装着有效 `T`），可被消费者读走。
- `write..read+capacity` 区间内的槽**未初始化**（空），可被生产者写入。
- 索引一推进，初始化状态就跟着变：`advance_write_index(n)` 把 `n` 个槽从「未初始化」翻转为「已初始化」；`advance_read_index(n)` 把 `n` 个槽从「已初始化」翻转为「未初始化」（前提是元素已被移走或 drop）。

因此 unsafe 的核心铁律只有一条：**任何时刻，`&T`（或 `assume_init`）只能作用在 `read..write` 区间内的槽；写入只能作用在 `write..read+capacity` 区间内的槽。** 谁要是越过这条线读写，就是 UB。

#### 4.1.3 源码精读

`Storage` trait 把「元素类型」声明为 `MaybeUninit<Self::Item>`，从源码根上就锁定了存储介质是未初始化内存：

- `Storage::as_mut_ptr` 的返回类型就是 `*mut MaybeUninit<Self::Item>`，见 [storage.rs:33-34](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/storage.rs#L33-L34)（中文：返回指向存储区起始、元素类型为 `MaybeUninit` 的可变裸指针）。
- `slice` / `slice_mut` 的默认实现据此构造 `&[MaybeUninit<T>]` / `&mut [MaybeUninit<T>]`，见 [storage.rs:43-54](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/storage.rs#L43-L54)（中文：按 `range` 在裸指针上偏移后用 `slice::from_raw_parts(_mut)` 造切片）。

而 `Array<T, N>`（即 `StaticRb` 的存储后端）的真身是：

```rust
pub type Array<T, const N: usize> = Owning<[MaybeUninit<T>; N]>;
```

即一个长度编译期固定的 `MaybeUninit<T>` 数组，包在 `Owning` 里（[storage.rs:102](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/storage.rs#L102)，中文：`Array` 是 `Owning<[MaybeUninit<T>; N]>` 的别名）。`Owning` 用 `UnsafeCell<T>` 持有数据（[storage.rs:90-92](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/storage.rs#L90-L92)，中文：`Owning` 内部是一个 `UnsafeCell`，这是能从 `&self` 产出 `&mut` 的合法依据）。

最关键的一点是：**这块内存在构造时根本不会被初始化**。看 `uninit_array`（ringbuf 用它造未初始化数组，等标准库 `maybe_uninit_uninit_array` 稳定后会被替换）：

[utils.rs:9-11](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/utils.rs#L9-L11)（中文：`uninit_array` 造一个完全未初始化的 `[MaybeUninit<T>; N]`，靠「数组本身只是一层 MaybeUninit 外壳」这一性质 `assume_init` 整个数组而不触碰内部元素）。

```rust
pub fn uninit_array<T, const N: usize>() -> [MaybeUninit<T>; N] {
    unsafe { MaybeUninit::<[MaybeUninit<T>; N]>::uninit().assume_init() }
}
```

> 这一行很微妙但合法：`MaybeUninit::<[MaybeUninit<T>; N]>::uninit()` 得到一个未初始化的「数组外壳」，`.assume_init()` 声称「这个外壳本身已初始化」——外壳确实只是几个指针/长度级别的元数据，初始化它没问题；而数组里的每个 `MaybeUninit<T>` 元素仍然各自未初始化，因为我们从不对它们调用 `assume_init`。这正是「用类型系统把初始化责任推迟到元素级」的妙处。

#### 4.1.4 代码实践（源码阅读型）

**目标**：亲眼确认「存储区里根本没有有效 `T`，只有 `MaybeUninit<T>`，有效性完全由索引决定」。

**步骤**：

1. 打开 [storage.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/storage.rs)，搜索所有出现 `MaybeUninit` 的地方，确认四种后端（`Ref` / `Array` / `Slice` / `Heap`）的 `as_mut_ptr` 返回的都是 `*mut MaybeUninit<T>`。
2. 打开 [rb/shared.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs)，确认 `SharedRb` 的字段里**没有任何**记录「哪个槽有效」的位数组——只有 `read_index` / `write_index` 两个索引（[shared.rs:51-57](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs#L51-L57)）。

**观察 / 预期**：你会看到「有效性信息」从未被显式存储，而是隐含在 `read..write` 这个区间里。这是理解后续所有 unsafe 契约的总钥匙。

#### 4.1.5 小练习与答案

**练习 1**：如果把存储类型从 `MaybeUninit<T>` 换成 `Option<T>`，哪两个地方会产生重复开销？

**参考答案**：(1) 每个 `Option<T>` 的判别值占额外内存；(2) 每次 `try_push` 要构造 `Some`、每次 `try_pop` 要解构 `Some`，而这些「有没有值」的信息 `read/write` 索引已经能推出来，属于重复记账。`MaybeUninit<T>` 把这部分开销降到零。

**练习 2**：为什么 `uninit_array` 里的 `assume_init()` 不会立刻引发 UB？

**参考答案**：它 `assume_init` 的是「数组外壳」（一个 `[MaybeUninit<T>; N]` 类型的值），外壳本身的初始化是合法的；而数组内部的每个 `MaybeUninit<T>` 仍保持未初始化，只要后续不对它们单独 `assume_init`/读取就不会 UB。

---

### 4.2 Observer::unsafe_slices 与 Storage::slice_mut：索引区间到 MaybeUninit 切片

#### 4.2.1 概念说明

`Observer` trait（u3-l1）只负责「观测状态」，但其中有两个方法直接触及内存：`unsafe_slices` 与 `unsafe_slices_mut`。它们是**全部切片类访问的统一入口**——`Producer::vacant_slices_mut`、`Consumer::occupied_slices` 最终都调到这两个方法。它们的职责是：给定一个 `[start, end)` 索引区间，返回**最多两段**连续的 `MaybeUninit<T>` 切片（因为环形区间可能跨越数组末尾回绕到开头，由 `ranges` 拆成两段，见 u2-l1）。

这两个方法之所以叫 `unsafe_*`、返回 `MaybeUninit` 而不是 `T`，正是为了把「这块内存现在到底能不能当 `T` 用」的判断权**交还给调用者**：Observer 不替你假设任何槽已初始化，只给你原始的 `MaybeUninit` 切片，由你自己负责只读已初始化的、只写未初始化的。

而再往下，`unsafe_slices_mut` 在具体实现里调用的就是 `Storage::slice_mut`——那个「拿 `&self` 返回 `&mut`」的非常规方法。本模块讲清楚这两层各自的 unsafe 契约。

#### 4.2.2 核心流程

从高层 API 到裸内存的调用链（写端为例）：

```
Producer::vacant_slices_mut(&mut self)              [安全 fn, producer.rs:53]
        │  取区间 [write_index, read_index + capacity)
        ▼
Observer::unsafe_slices_mut(&self, start, end)      [unsafe fn, observer.rs:39]
        │  调 ranges() 拆成 (first_range, second_range)
        ▼
SharedRb::unsafe_slices_mut 实现                     [shared.rs:108-111]
        │  对每段 range 调 storage.slice_mut(range)
        ▼
Storage::slice_mut(&self, range) -> &mut [MaybeUninit<T>]   [unsafe fn, storage.rs:52]
        │  从 as_mut_ptr().add(range.start) 造 &mut 切片
        ▼
裸内存
```

每一层都有自己要守的契约，越往下越「裸」、越靠近 UB。

#### 4.2.3 源码精读

**第一层：`Observer::unsafe_slices` / `unsafe_slices_mut` 的契约。**

[observer.rs:24-39](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/observer.rs#L24-L39)（中文：定义两个 unsafe 切片方法及其安全契约）。关键签名与文档：

```rust
/// # Safety
/// Slice must not overlap with any mutable slice existing at the same time.
/// Non-`Sync` items must not be accessed from multiple threads at the same time.
unsafe fn unsafe_slices(&self, start: usize, end: usize)
    -> (&[MaybeUninit<Self::Item>], &[MaybeUninit<Self::Item>]);

/// # Safety
/// There must not exist overlapping slices at the same time.
unsafe fn unsafe_slices_mut(&self, start: usize, end: usize)
    -> (&mut [MaybeUninit<Self::Item>], &mut [MaybeUninit<Self::Item>]);
```

两条核心契约：

1. **不重叠**：同一时刻不能存在两段相互重叠的（可变）切片。否则就同时存在两个指向同一块内存的 `&mut`，违反 Rust 别名规则 → UB。这是为什么 `vacant_slices_mut` 与 `occupied_slices_mut` 取的都是 `&mut self`：用借用检查器在编译期阻止「同时拿写端和读端切片」。
2. **并发可见性**：对 `Non-Sync` 的元素，不能多线程同时访问。注意它说的是「元素本身」——`Storage` 后端（如 `Heap`）通过 `unsafe impl Sync where T: Send` 保证「存储容器可跨线程共享」，但元素能不能并发访问仍取决于 `T` 自己。在 SPSC 模型下这天然满足：一写一读、各碰各的区间，永不相交。

**第二层：具体实现如何落到 `Storage`。**

[shared.rs:104-111](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs#L104-L111)（中文：`SharedRb` 用 `ranges()` 把 `[start,end)` 拆两段，再分别调 `storage.slice` / `storage.slice_mut`）：

```rust
unsafe fn unsafe_slices_mut(&self, start: usize, end: usize)
    -> (&mut [MaybeUninit<S::Item>], &mut [MaybeUninit<S::Item>]) {
    let (first, second) = ranges(self.capacity(), start, end);
    unsafe { (self.storage.slice_mut(first), self.storage.slice_mut(second)) }
}
```

**第三层：`Storage::slice_mut` 那个「`&self` 返回 `&mut`」的怪签名。**

[storage.rs:46-54](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/storage.rs#L46-L54)（中文：`slice_mut` 从 `&self` 的 `as_mut_ptr` 偏移后造可变切片，故必须 unsafe 且标注 `#[allow(clippy::mut_from_ref)]`）：

```rust
#[allow(clippy::mut_from_ref)]
unsafe fn slice_mut(&self, range: Range<usize>) -> &mut [MaybeUninit<Self::Item>] {
    unsafe { slice::from_raw_parts_mut(self.as_mut_ptr().add(range.start), range.len()) }
}
```

它能合法地「拿 `&self` 还 `&mut`」，全靠实现方用 `UnsafeCell` 持有数据（`Owning` 的 `data: UnsafeCell<T>`，[storage.rs:90-92](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/storage.rs#L90-L92)）。`UnsafeCell` 是编译器唯一认可的「内部可变性逃生口」：它告诉优化器「这块内存可能被别名改写，别对它做越界的常数传播等假设」。`Storage` trait 的 `# Safety` 文档（[storage.rs:11-18](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/storage.rs#L11-L18)）把这三条实现约定写成铁律：

- **不自别名**：可以同时持有「指向 storage 自身的引用」和「指向其数据的可变引用」（这正是 `slice_mut` 的前提）。
- **`as_mut_ptr` 必须指向真实数据**。
- **`len` 必须恒定**（容量在缓冲区生命周期内不变，u2-l1 已强调）。

#### 4.2.4 代码实践（源码阅读型）

**目标**：沿调用链走一遍，确认「不重叠」契约如何被编译期与运行期共同守护。

**步骤**：

1. 在 [consumer.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/consumer.rs) 中找到 `occupied_slices`（[consumer.rs:45-47](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/consumer.rs#L45-L47)），确认它取的是 `[read_index, write_index)`——即只覆盖已初始化区间。
2. 在 [producer.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/producer.rs) 中找到 `vacant_slices_mut`（[producer.rs:53-55](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/producer.rs#L53-L55)），确认它取的是 `[write_index, read_index + capacity)`——即只覆盖未初始化区间。
3. 验证这两个区间**永不相交**（这是 SPSC 不变量的直接推论：occupied 与 vacant 恰好互补）。

**预期**：你会发现「不重叠」在逻辑上由 `read/write` 索引的算术保证，在类型上由 `&mut self` 借用保证——两层防线共同堵住了别名 UB。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `unsafe_slices_mut` 的签名是 `&self -> &mut [...]`，而不是更「正常」的 `&mut self -> &mut [...]`？

**参考答案**：因为同一个 `SharedRb` 要被 `Arc` 共享给生产者和消费者两端，两端都只持有 `&SharedRb`（共享引用）。若要求 `&mut self`，就无法在拆分后让两端各自拿切片了。改用 `&self` + 内部的 `UnsafeCell`/`Storage::slice_mut`，把「能不能产出可变切片」的安全责任从借用检查器转移到了 `unsafe` 契约（不重叠 + SPSC 不相交）上。

**练习 2**：`Storage::slice_mut` 上的 `#[allow(clippy::mut_from_ref)]` 是在抑制什么？去掉它会怎样？

**参考答案**：它抑制 clippy 的 `mut_from_ref` lint——该 lint 专门警告「`&self` 返回 `&mut T`」这种危险的签名。去掉只是多一条警告，不影响编译（因为 `unsafe fn` 允许这种签名），但保留它是向读者明示「这里是有意为之的内部可变性，请阅读 Safety 文档」。

---

### 4.3 utils.rs 的 unsafe 辅助函数族

#### 4.3.1 概念说明

`src/utils.rs` 是一组「在 `MaybeUninit` 切片与普通 `T` 切片之间互相转换、以及在 `MaybeUninit` 切片之间搬运」的底层工具。它们大多对应标准库里**尚未稳定**的 API（每个函数上方都有 `// TODO: Remove on ... stabilization.` 注释），所以 ringbuf 自己实现一份。

这些函数分三类：

| 类别 | 函数 | 做什么 | 为何 unsafe |
| --- | --- | --- | --- |
| 视图转换（assume_init） | `slice_assume_init_ref` / `_mut` | 把 `&[MaybeUninit<T>]` 当成 `&[T]` 看 | 断言所有元素已初始化，否则读取/drop 即 UB |
| 视图转换（反向） | `slice_as_uninit_mut` | 把 `&mut [T]` 当成 `&mut [MaybeUninit<T>]` 看 | 重新解释内存布局，需保证 `T` 与 `MaybeUninit<T>` 布局一致 |
| 批量写入/搬运 | `write_slice` / `move_uninit_slice` | 把数据搬进 `MaybeUninit` 区 | 内部用裸指针 `ptr::read`/`get_unchecked`，需保证长度匹配、不别名 |
| 所有权转交 | `array_to_uninit` / `vec_to_uninit` / `boxed_slice_to_uninit` | 把 `[T;N]`/`Vec<T>`/`Box<[T]>` 变成对应的 `MaybeUninit` 版本 | 用 `ManuallyDrop` + 指针重解释「偷」走所有权 |

记住：这整组函数对 crate 外不可见（`mod utils` 非 `pub`）。读它们是为了理解公开 API（如 `Consumer::as_slices`、`push_slice`）内部怎么兑现「assume_init」承诺。

#### 4.3.2 核心流程

最典型的链路是「消费者把 `MaybeUninit` 切片安全地呈现为 `&[T]`」：

```
Consumer::as_slices(&self) -> (&[T], &[T])              [安全 fn]
        │  调 occupied_slices() 得到 (&[MaybeUninit<T>], &[MaybeUninit<T>])
        ▼
slice_assume_init_ref(&[MaybeUninit<T>]) -> &[T]        [unsafe fn]
        │  原地把 MaybeUninit 切片重解释为 T 切片
        ▼
返回 &[T]（编译期看来是普通切片，安全性完全靠「occupied 区间确实已初始化」这一不变量支撑）
```

反向（写端）类似：`peek_slice` / `pop_slice` 要求 `T: Copy`，会先用 `slice_as_uninit_mut` 把用户的 `&mut [T]` 当成 `&mut [MaybeUninit<T>]`，再用 `move_uninit_slice` 把数据搬过去。

#### 4.3.3 源码精读

**assume_init 家族——本模块的核心。**

[utils.rs:19-25](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/utils.rs#L19-L25)（中文：`slice_assume_init_ref/mut` 用裸指针 cast 把 `MaybeUninit` 切片重解释为 `T` 切片）：

```rust
pub unsafe fn slice_assume_init_ref<T>(slice: &[MaybeUninit<T>]) -> &[T] {
    unsafe { &*(slice as *const [MaybeUninit<T>] as *const [T]) }
}
pub unsafe fn slice_assume_init_mut<T>(slice: &mut [MaybeUninit<T>]) -> &mut [T] {
    unsafe { &mut *(slice as *mut [MaybeUninit<T>] as *mut [T]) }
}
```

**它为什么是 unsafe？** 因为它对编译器撒了一个谎：「这块 `MaybeUninit<T>` 内存里现在每个元素都是一个合法的、已初始化的 `T`」。编译器无法验证这个事实——`MaybeUninit` 的设计初衷就是「不保证初始化」。一旦调用者传进来的切片里其实有未初始化的槽，那么之后任何通过返回的 `&[T]` / `&mut [T]` 去**读**（或让其在 drop 时被析构）那个槽，就是读取未初始化内存 → UB。安全性百分之百依赖调用者保证「这些槽确实处于 `read..write`（已初始化）区间」。

> 合法性依据：`MaybeUninit<T>` 与 `T` 有着**相同的内存布局**（这是标准库保证的 `#[repr(transparent)]` 性质），所以纯指针 cast 本身不违反布局规则；风险只在于「初始化状态」这一运行期事实。

**写端搬运：`write_slice`。**

[utils.rs:28-32](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/utils.rs#L28-L32)（中文：`write_slice` 把 `&[T]` 拷进 `&mut [MaybeUninit<T>]`，要求 `T: Copy`，返回已初始化的 `&mut [T]`）：

```rust
pub fn write_slice<'a, T: Copy>(dst: &'a mut [MaybeUninit<T>], src: &[T]) -> &'a mut [T] {
    let uninit_src: &[MaybeUninit<T>] = unsafe { mem::transmute(src) };
    dst.copy_from_slice(uninit_src);
    unsafe { slice_assume_init_mut(dst) }
}
```

注意它本身是**安全 fn**（无 `unsafe` 关键字），但内部用了 `transmute`（unsafe）和 `slice_assume_init_mut`（unsafe）。为何能做成安全？因为它把不安全前提封装在了不变量里：`copy_from_slice` 要求两端**长度相等**（否则 panic，非 UB），且 `T: Copy` 意味着拷贝是逐字节复制、不涉及 drop。因此对调用者而言只要传对长度就安全；内部的两处 unsafe 由函数自己的逻辑兜底。这是「用不变量把 unsafe 包成 safe API」的典型示范。

**裸指针搬运：`move_uninit_slice`。**

[utils.rs:34-39](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/utils.rs#L34-L39)（中文：逐元素用 `ptr::read` + `get_unchecked` 在两段 `MaybeUninit` 切片间搬运）：

```rust
pub fn move_uninit_slice<T>(dst: &mut [MaybeUninit<T>], src: &[MaybeUninit<T>]) {
    assert_eq!(dst.len(), src.len());
    for i in 0..dst.len() {
        unsafe { *dst.get_unchecked_mut(i) = ptr::read(src.get_unchecked(i) as *const _) };
    }
}
```

同样是安全 fn，靠开头的 `assert_eq!(dst.len(), src.len())` 把「越界」风险挡在 panic 而非 UB；`get_unchecked` 的安全性由「`i < dst.len()` 且 `src.len() == dst.len()`」保证。它用于 `peek_slice_uninit` 等「把缓冲区里的元素复制出去但不移除」的场景（[consumer.rs:130-147](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/consumer.rs#L130-L147)）。

**所有权转交：`array_to_uninit`。**

[utils.rs:41-45](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/utils.rs#L41-L45)（中文：`array_to_uninit` 用 `ManuallyDrop` 阻止原数组 drop，再 `ptr::read` 把同一块内存当作 `[MaybeUninit<T>; N]` 读走）：

```rust
pub fn array_to_uninit<T, const N: usize>(value: [T; N]) -> [MaybeUninit<T>; N] {
    let value = mem::ManuallyDrop::new(value);
    let ptr = &value as *const _ as *const [MaybeUninit<T>; N];
    unsafe { ptr.read() }
}
```

这里 `ManuallyDrop` 的作用是关键：它让原 `[T; N]` **不会被自动 drop**，否则 `ptr::read` 读走内存后，原数组离开作用域再 drop 一次就是「double free / 重复 drop」→ UB。`vec_to_uninit` / `boxed_slice_to_uninit`（[utils.rs:48-59](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/utils.rs#L48-L59)）是同一套手法的 `alloc` 版本。它们支撑了 `rb_impl_init!` 宏里的 `From<[T; N]>` / `From<Vec<T>>` 构造器（u8-l3 会展开）。

#### 4.3.4 代码实践（源码阅读型 —— 本讲规格指定任务之一）

**目标**：精读 `slice_assume_init_mut`，解释它为何是 unsafe。

**步骤**：

1. 打开 [utils.rs:23-25](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/utils.rs#L23-L25)，逐字符读这一行：`unsafe { &mut *(slice as *mut [MaybeUninit<T>] as *mut [T]) }`。
2. 找出它在 crate 内被谁调用，例如 [consumer.rs:74](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/consumer.rs#L74)（`as_mut_slices` 里）和 [producer.rs:143](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/producer.rs#L143)（`read_from` 里）。
3. 追问：调用点凭什么敢 assume_init？

**结论（为何 unsafe）**：该函数仅做指针类型 cast，**不检查任何初始化状态**，直接把「可能未初始化」的 `MaybeUninit<T>` 切片当作「必定已初始化」的 `T` 切片交出去。编译器无从验证，因此必须 `unsafe`。它之所以在 ringbuf 内部用得安全，是因为每个调用点都先把切片限定在了**已初始化区间**（`occupied_slices` 取 `[read, write)`），由索引不变量担保——但这层担保是**调用方的责任**，函数自身无法强制。

**预期观察**：你会看到「assume_init 的安全性 = 索引区间的正确性」这个等式贯穿整个 Consumer/Producer 实现。

#### 4.3.5 小练习与答案

**练习 1**：`write_slice` 内部用了 `transmute` 和 `slice_assume_init_mut` 两个 unsafe，为什么它本身却被声明为安全 fn？

**参考答案**：因为它把所有不安全前提都收敛进了可检查的不变量——`copy_from_slice` 强制两端等长（不等长 panic 而非 UB），`T: Copy` 保证逐字节复制无 drop 副作用。函数的逻辑使内部 unsafe 的前提恒成立，调用者无需承担额外义务，故可标为 safe。这正是「用不变量把 unsafe 封装成 safe」的标准做法。

**练习 2**：`array_to_uninit` 里如果去掉 `ManuallyDrop::new(value)` 这一行，会出什么问题？

**参考答案**：原 `[T; N]` 会在函数结束时被自动 drop，而 `ptr::read` 已经把同一块内存的所有权「搬」给了返回值——于是同一块内存会被析构两次（double drop），对拥有堆资源或非平凡 drop 的 `T` 即 UB。`ManuallyDrop` 阻止原值 drop，确保所有权只转移一次。

---

### 4.4 索引推进即初始化状态推进：advance_*_index 的安全契约

#### 4.4.1 概念说明

回到 4.1 的那张铁律：`read..write` 已初始化、`write..read+capacity` 未初始化。那么**谁负责翻转这两个区间的边界**？答案就是 `advance_write_index`（写端把边界往前推，把新写的槽登记为「已初始化」）和 `advance_read_index`（读端把边界往前推，把已读走的槽登记为「未初始化」）。

这两个方法把「物理上写入了内存」和「逻辑上对外可见」分离开：

- 你可以先把元素 `write` 进 `MaybeUninit` 槽（物理写入），但在 `advance_write_index` 之前，**对端消费者看不到它**——因为 `write_index` 没动，`occupied_slices` 取的 `[read, write)` 还不包含这个槽。
- 只有调用 `advance_write_index(n)` 之后，这 `n` 个槽才正式「登记」为已初始化、进入可见区。

对 `SharedRb` 而言，这次「登记」就是一次 `Release` 原子 store（u5-l1 讲过它如何建立跨线程可见性）。所以 advance 既是「初始化状态推进」，也是「跨线程发布」。

#### 4.4.2 核心流程

写一个元素的安全范式（`try_push` 的本质，[producer.rs:60-70](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/producer.rs#L60-L70)）：

```
1. 观测：if !self.is_full() { ... }            // 至少有 1 个空槽
2. 物理写入：vacant_slices_mut().0.get_unchecked_mut(0).write(elem)
3. 发布：advance_write_index(1)                 // 把这 1 个槽登记为已初始化
```

读一个元素对称（`try_pop`，[consumer.rs:106-114](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/consumer.rs#L106-L114)）：

```
1. 观测：if !self.is_empty() { ... }
2. 物理读出：occupied_slices().0.get_unchecked(0).assume_init_read()  // 把元素 move 出来
3. 发布：advance_read_index(1)                  // 把这 1 个槽登记为未初始化
```

两端的 advance 都必须**只前进、不后退**，且推进量必须与「实际搬动/初始化的元素数」一致。

#### 4.4.3 源码精读

**`set_write_index` / `advance_write_index` 的契约。**

[producer.rs:21-35](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/producer.rs#L21-L35)（中文：`set_write_index` 设定写索引、`advance_write_index` 在其基础上前进 `count`，两者都 unsafe 并带严格契约）：

```rust
/// # Safety
/// Index must go only forward, never backward.
/// All slots with index less than `value` must be initialized until write index,
/// all slots with index equal or greater - must be uninitialized.
unsafe fn set_write_index(&self, value: usize);

/// # Safety
/// First `count` items in free space must be initialized.
/// Must not be called concurrently.
unsafe fn advance_write_index(&self, count: usize) {
    unsafe { self.set_write_index((self.write_index() + count) % modulus(self)) };
}
```

读端对称，见 [consumer.rs:12-30](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/consumer.rs#L12-L30)（中文：`set_read_index` / `advance_read_index` 要求索引单调前进、被跳过的元素必须已移走或 drop、不可并发调用）。

三条不可违反的契约（违反即 UB）：

1. **单调前进**：新值只能比旧值大（在模 `2*capacity` 意义下），绝不回退。回退会让「已登记初始化」的槽重新进入未初始化区，或反之，导致读端读到未初始化内存。
2. **初始化状态与索引一致**：对写端，`advance_write_index(count)` 前，空闲区前 `count` 个槽必须**已物理初始化**；对读端，`advance_read_index(count)` 前，占用区前 `count` 个槽必须**已被移走或 drop**（不能留下「悬挂」的已初始化 `T` 被后续覆盖而不 drop，造成资源泄漏；也不能让别的代码再去读它）。
3. **不可并发**：`advance_*` 绝不能在多线程同时调用。这正是 SPSC「至多一个写端、一个读端」的体现——写端独占 `write_index` 与空闲区，读端独占 `read_index` 与占用区（u5-l2 详述 hold 标志如何强制这一点）。

**`SharedRb` 里 advance 如何落地为原子发布。**

[shared.rs:123-135](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs#L123-L135)（中文：`SharedRb` 把 `set_write_index` / `set_read_index` 实现为对原子索引的 `Release` store）：

```rust
impl<S: Storage + ?Sized> Producer for SharedRb<S> {
    unsafe fn set_write_index(&self, value: usize) {
        self.write_index.store(value, Ordering::Release);
    }
}
impl<S: Storage + ?Sized> Consumer for SharedRb<S> {
    unsafe fn set_read_index(&self, value: usize) {
        self.read_index.store(value, Ordering::Release);
    }
}
```

也就是说：对 `SharedRb`，`advance_write_index` 的「发布」动作 = 一次 `Release` store 到 `write_index`。配合消费者 `read_index`/`write_index` 的 `Acquire` load（[shared.rs:95-102](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs#L95-L102)），就建立了 u5-l1 讲过的「写数据 happens-before 读数据」传递链。**索引推进既是初始化状态的逻辑翻转，也是跨线程可见性的物理发布，两者在源码里是同一个原子操作。**

**`try_pop` 如何兑现「读端移走元素后推进」。**

[consumer.rs:106-114](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/consumer.rs#L106-L114)（中文：`try_pop` 先 `assume_init_read` 把最旧元素 move 出来，再 `advance_read_index(1)` 把该槽登记为未初始化）：

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

`assume_init_read()` 把 `MaybeUninit<T>` 里的值按位 move 出来（槽于是回到「逻辑未初始化」），紧接着 `advance_read_index(1)` 更新索引与之对齐。两步都在 `unsafe {}` 里，因为 `get_unchecked` 和 `advance_read_index` 都 unsafe；而「这个槽确实已初始化」由前面的 `!self.is_empty()` + `occupied_slices` 取 `[read, write)` 共同保证。

#### 4.4.4 代码实践（动手型 —— 本讲规格指定主任务）

**目标**：用 `vacant_slices_mut` + `advance_write_index` 手动写入一个元素，列出调用者必须满足的全部安全前提，并与 `try_push` 对照。

> 说明：本实践是**示例代码**——它模仿 `try_push` 的内部写法，用公开 unsafe API 手动完成一次写入，帮助你看清契约。`vacant_slices_mut` 是安全 fn，但 `advance_write_index` 是 unsafe fn；整体处于 `unsafe {}` 中。

**示例代码**（在未拆分的缓冲区上操作，因为它实现了 `Producer`）：

```rust
// 示例代码：手动写入一个元素，等价于 try_push(42)
use ringbuf::{LocalRb, storage::Array, traits::*};

fn main() {
    let mut rb = LocalRb::<Array<i32, 4>>::default(); // 容量 4，全空

    // 1. 观测：确认至少有一个空槽（空缓冲区当然有）
    assert!(!rb.is_full());

    let elem = 42;
    unsafe {
        // 2. 物理写入：拿到未初始化切片，写第一个槽
        //    vacant_slices_mut 返回的两段切片此刻第一段长度为 4、第二段为 0
        rb.vacant_slices_mut().0.get_unchecked_mut(0).write(elem);
        // 3. 发布：把 1 个槽登记为已初始化
        rb.advance_write_index(1);
    }

    // 验证：现在 occupied_len 应为 1，try_pop 拿到 42
    assert_eq!(rb.occupied_len(), 1);
    assert_eq!(rb.try_pop(), Some(42));
    assert_eq!(rb.try_pop(), None);
}
```

**调用者必须满足的全部安全前提**（逐条对照源码契约）：

1. **至少有一个空槽**：必须先确认 `!is_full()`（或 `vacant_len() >= 1`）。否则 `vacant_slices_mut().0` 为空切片，`get_unchecked_mut(0)` 立即越界 → UB。（对应 `try_push` 里的 `if !self.is_full()`。）
2. **从正确位置开始写**：必须从**第一段切片的起始**写起，填满第一段再写第二段（[producer.rs:46-48](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/producer.rs#L46-L48) 的 vacant_slices_mut 文档）。否则破坏 FIFO 与索引语义。
3. **每个写入的槽必须真正初始化**：用 `MaybeUninit::write` / `ptr::write` 把值放进去，不能留下半初始化的槽。
4. **advance 的 count 与实际写入数一致**：`advance_write_index(count)` 的 `count` 必须 **≤ 实际初始化的元素数**（通常取等号）。多报会让对端读到未初始化内存 → UB；这是 `set_write_index` 契约「index 以下的槽必须已初始化」的直接要求（[producer.rs:21-24](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/producer.rs#L21-L24)）。
5. **advance 之前不做其它修改性调用**：`vacant_slices_mut` 文档明确「This method must be followed by `advance_write_index` ... No other mutating calls allowed before that」（[producer.rs:49-50](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/producer.rs#L49-L50)）。
6. **不可并发调用**：`advance_write_index` 文档「Must not be called concurrently」（[producer.rs:31](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/producer.rs#L31)）——遵守 SPSC 单写端约定。
7. **索引只前进不后退**：只用 `advance_*`（它在现有值上 `+count` 再取模），不要直接 `set_*_index` 回退。

**观察 / 预期**：把上面的 `unsafe` 块和 `try_push` 的实现（[producer.rs:60-70](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/producer.rs#L60-L70)）逐行对比，你会发现二者**完全同构**——`try_push` 只是把这 7 条契约用代码固化成了安全 API。

**待本地验证**：上述程序的行为（`occupied_len` 由 0 变 1、`try_pop` 得到 42）请在本机 `cargo run` 确认；若你想体会 UB，可故意把 `count` 改成 `2`（只写了 1 个却报 2），在 debug 下可能看似正常、在优化或 Miri 下会被抓出问题（见第 5 节综合实践）。

#### 4.4.5 小练习与答案

**练习 1**：为什么「先 `write` 进内存、再 `advance_write_index`」中间不能被对端看到，而 advance 之后就能看到？

**参考答案**：可见性由 `write_index` 决定，不由物理写入决定。消费者的 `occupied_slices` 取的是 `[read_index, write_index)`；在 advance 之前 `write_index` 未变，新写的槽不在这个区间内，因此对端「看不见」。advance 之后 `write_index` 前移，该槽进入区间，对端才看得到。对 `SharedRb`，advance 就是对 `write_index` 的 `Release` store，它同时完成了「逻辑登记」和「跨线程发布」。

**练习 2**：如果只调 `vacant_slices_mut().0.get_unchecked_mut(0).write(elem)` 而**忘记**调 `advance_write_index(1)`，会出现哪两类问题？

**参考答案**：(1) 功能上元素「丢失」——数据已在内存里，但 `write_index` 没动，消费者永远 `try_pop` 不到它；(2) 若之后又往同一槽写别的值，原值会被无声覆盖（对非 `Copy` 的 `T` 不会 drop，造成资源泄漏）。记住：没有 advance，写入就等于没提交。

**练习 3**：`advance_read_index` 为什么要求「前 `count` 个元素必须先被移走或 drop」？

**参考答案**：advance 把这 `count` 个槽从「已初始化」翻转为「未初始化」，意味着它们将进入空闲区、随时可能被生产者覆盖。如果元素还没被 move 出来或 drop 掉就被覆盖，非 `Copy` 的 `T`（如 `Box`、`String`）就会泄漏资源甚至 double-free。所以必须先 `assume_init_read` 移走或 `drop_in_place`（`skip`/`clear` 正是这么做，见 [consumer.rs:218-243](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/consumer.rs#L218-L243)），再 advance。

---

## 5. 综合实践

**任务**：用本讲学到的全部知识，手工「拆解」一次 `push_slice`，并用 Miri 验证 unsafe 的正确性。

**步骤**：

1. **阅读 `push_slice`**（[producer.rs:96-118](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/producer.rs#L96-L118)），把它拆成三步：取 `vacant_slices_mut` → 用 `write_slice`（内部 `copy_from_slice` + `assume_init_mut`）批量写入 → `advance_write_index(count)`。在纸上标注每一步触发了 4.4 节 7 条安全前提中的哪几条。
2. **追踪 `write_slice` 的内部 unsafe**（[utils.rs:28-32](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/utils.rs#L28-L32)），确认它如何被不变量（等长 + `Copy`）封成安全 fn。
3. **写一个故意犯错的对照程序**（示例代码）：构造容量为 4 的 `LocalRb<Array<i32, 4>>`，写入 2 个元素后，故意 `advance_write_index(3)`（多报 1 个），再 `try_pop` 3 次。

   ```rust
   // 示例代码：故意制造「读取未初始化内存」的 UB，供 Miri 抓取
   use ringbuf::{LocalRb, storage::Array, traits::*};
   fn main() {
       let mut rb = LocalRb::<Array<i32, 4>>::default();
       unsafe {
           rb.vacant_slices_mut().0.get_unchecked_mut(0).write(10);
           rb.vacant_slices_mut().0.get_unchecked_mut(1).write(20);
           rb.advance_write_index(3); // 只写了 2 个，却报 3 个 -> 第 3 个槽未初始化
       }
       for _ in 0..3 { let _ = rb.try_pop(); } // 第 3 次 try_pop 读到未初始化内存 -> UB
   }
   ```

4. **用 Miri 跑**：执行 `scripts/miri.sh`（或在 `ringbuf` 目录下 `cargo +nightly miri run --example <你的例子>`，需要把上面程序放进 `examples/`）。

**预期结果**：正确版本（第 4.4.4 节）在 Miri 下干净通过；对照版本（第 3 步）会被 Miri 报告「reading uninitialized memory」之类的 UB。这直观印证了「`advance_write_index` 的 count 必须等于实际初始化数」这条契约的存在意义，也展示了 Miri 如何守护 ringbuf 的全部 unsafe（呼应 u8-l5 的测试体系）。

> 若本地未安装 nightly/Miri，第 4 步可标注为「待本地验证」，但务必先完成第 1–2 步的源码拆解（纯阅读，无需运行）。

## 6. 本讲小结

- ringbuf 用 `MaybeUninit<T>`（而非 `Option<T>`）存放元素，把「有效性」从数据本身外移给 `read/write` 索引，换来零开销，代价是读写须用 unsafe 兑现「我知道这个槽现在有效/无效」的承诺。
- `Observer::unsafe_slices(_mut)` 是全部切片访问的统一入口，返回 `MaybeUninit` 切片，安全契约是「不重叠」+「Non-Sync 元素不可并发访问」；它最终落到 `Storage::slice_mut`——那个靠 `UnsafeCell` 合法化的「`&self` 返回 `&mut`」方法。
- `src/utils.rs` 里的 `slice_assume_init_ref/mut` 等函数之所以 unsafe，是因为它们**断言**整段 `MaybeUninit` 已初始化而不做任何检查；`write_slice` / `move_uninit_slice` 则示范了「用不变量把 unsafe 封成 safe fn」；`array_to_uninit` 等用 `ManuallyDrop` 安全转交所有权。
- `advance_write_index` / `advance_read_index` 的本质是**初始化状态推进**：在 `SharedRb` 上它就是一次 `Release` 原子 store，同时完成「逻辑登记」与「跨线程发布」；其契约是单调前进、count 与实际搬动数一致、不可并发。
- 全部 unsafe 的总钥匙是 SPSC 不变量：`read..write` 恒为已初始化区、`write..read+capacity` 恒为未初始化区、两者永不相交——`assume_init` 的安全性 = 索引区间的正确性。

## 7. 下一步学习建议

- **向后巩固 unsafe 的并发维度**：本讲聚焦「初始化状态」与「别名」，而 u5-l1《无锁并发：原子操作、CachePadded 与内存顺序》讲的是这些 unsafe 操作在多线程下的**可见性**（Acquire/Release）。两者合起来才是 SharedRb 内存安全的完整图景，建议对照重读 `set_write_index` 的 Release store。
- **向前进入扩展实践**：u8-l2《自定义 Storage 与 from_raw_parts》会用到本讲的 `Storage` 安全约定（不自别名、`as_mut_ptr` 指向数据、`len` 恒定）去实现自己的存储后端；u8-l5《测试体系与 Miri》会教你用 Miri 系统性地验证本讲提到的所有 unsafe 契约。
- **建议继续阅读的源码**：`src/traits/consumer.rs` 的 `skip` / `clear`（[consumer.rs:218-243](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/consumer.rs#L218-L243)，看 `drop_in_place` 如何先于 `advance_read_index` 回收资源）、以及 `src/rb/shared.rs` 的 `from_raw_parts` / `into_raw_parts`（[shared.rs:59-85](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs#L59-L85)，看构造时如何约定初始初始化区间）。
