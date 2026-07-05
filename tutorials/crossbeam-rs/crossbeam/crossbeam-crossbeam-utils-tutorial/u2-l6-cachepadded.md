# CachePadded 缓存行填充

## 1. 本讲目标

本讲讲解 `crossbeam-utils` 提供的 `CachePadded<T>` 类型。读完本讲，你应当能够：

- 用通俗语言说清楚「缓存行」「false sharing（虚假共享）」「内存对齐」三者的关系，并理解为什么 false sharing 会让并发程序变慢。
- 读懂 [src/cache_padded.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/cache_padded.rs) 里那一大段按 `target_arch` 切换的 `repr(align(N))`，理解每个 `N` 的来源以及「悲观假设」是什么意思。
- 掌握 `CachePadded` 的 `new` / `into_inner` / `Deref` / `DerefMut` 用法，并知道它在 `Send`/`Sync`、`Copy` 等方面的行为只是「原样转发」内部值 `T`。
- 看懂 `CachePadded` 在本 crate 内部的两个真实用法：`AtomicCell` 的全局锁池与 `ShardedLock` 的分片，理解为什么它们「必须」用 `CachePadded`。

## 2. 前置知识

阅读本讲前，需要先建立以下几个直觉。后面所有源码都是为了实现这些直觉。

### 2.1 CPU 缓存与缓存行

CPU 比内存快几个数量级。为了弥补这个差距，CPU 内部有多级缓存（L1/L2/L3）。**数据在内存与缓存之间不是「按字节」搬运的，而是按一个固定大小的块搬运，这个块就叫缓存行（cache line）**。当今绝大多数架构一条缓存行是 64 字节。

要点：哪怕你只想读 1 个字节，CPU 也会把包含它的那一整条 64 字节缓存行加载进缓存；写一个字节，也会让整条缓存行失效。

### 2.2 每个核有自己的 L1 缓存，缓存一致性协议维护「谁是有效的」

多核机器里每个核各有自己的 L1 缓存。为了保证多个核看到的同一地址数据一致，硬件用一套协议（最著名的是 MESI 系列）来跟踪「某条缓存行在哪几个核里有副本、是否被改过」。

一条核心规则是：**当一个核写了某条缓存行里的任意一个字节，这条缓存行在所有其他核里的副本都会被标记为「失效（invalid）」**，下次别的核再读就得重新从内存或更高层缓存搬运。

### 2.3 False Sharing（虚假共享）

把上面两条合起来，就会得到一个反直觉的后果：

> 两个线程各自只读写**不同的变量**，本来互不相干，但如果这两个变量**恰好落在同一条 64 字节的缓存行里**，它们就会互相把对方的缓存行顶爆。

具体过程（设核 0 自增变量 `a`，核 1 自增变量 `b`，且 `a`、`b` 同处一条缓存行）：

1. 核 0 读 `a` → 缓存行进入核 0 的 L1。
2. 核 1 读 `b` → 同一条缓存行也进入核 1 的 L1（两核都持有副本，状态「共享」）。
3. 核 0 写 `a` → 这条行在核 1 失效；核 0 持有唯一副本。
4. 核 1 写 `b` → 这条行在核 0 失效；核 1 持有唯一副本。
5. ……周而复始，缓存行在两个核之间像皮球一样被踢来踢去。

结果是：逻辑上毫无关系的两个变量，让两核都不停地 cache miss。这就是 **false sharing**。它不会导致结果错误，只会让程序显著变慢，而且在「加锁」「换数据结构」层面都看不出来——必须从「数据在缓存行里的布局」入手。

### 2.4 对策：缓存行对齐

解决办法是把每个「热点」变量各自对齐到一条缓存行的起点，并占用一整条缓存行。这样两个变量就绝无可能落在同一条缓存行里，false sharing 消失。这正是 `CachePadded<T>` 要做的事。

### 2.5 Rust 的 `repr(align(N))`

`#[repr(align(N))]` 是 Rust 的属性，要求该类型的对齐量**至少**是 `N` 字节（`N` 必须是 2 的幂）。一旦类型对齐量变大、且内部只有一个字段，编译器会自动在尾部补齐 padding，使整个类型的大小也是 `N` 的整数倍。这是「让一个值独占一条缓存行」的语言级手段。

> 关键：对齐量越大，类型就越大。`CachePadded<u8>` 在 x86-64 上是 128 字节而不是 1 字节——为的就是把那 1 字节「撑」到一整条缓存行。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/cache_padded.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/cache_padded.rs) | `CachePadded<T>` 的全部定义：分架构 `repr(align)`、构造、解引用、trait 转发。本讲主角。 |
| [src/lib.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/lib.rs) | 用私有 `mod cache_padded` + `pub use ...::CachePadded` 把类型重导出到 crate 根。 |
| [src/atomic/atomic_cell.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs) | `AtomicCell` 全局锁池 `static LOCKS: [CachePadded<SeqLock>; 67]`，是内部用 `CachePadded` 的第一处。 |
| [src/atomic/seq_lock.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/seq_lock.rs) | `SeqLock` 只是一个 `AtomicUsize`（8 字节），解释了为什么锁池非填不可。 |
| [src/sync/sharded_lock.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/sharded_lock.rs) | `ShardedLock` 的 `shards: Box<[CachePadded<Shard>]>`，是内部用 `CachePadded` 的第二处。 |
| [tests/cache_padded.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/tests/cache_padded.rs) | 公开测试，验证对齐、大小、Clone、Drop 等行为。 |

---

## 4. 核心概念与源码讲解

### 4.1 缓存行与 false sharing：类型为何而存在

#### 4.1.1 概念说明

`CachePadded` 解决的问题只有一个：**false sharing**。前一节的「前置知识」已经讲清了它的物理成因。这里把它浓缩成一句话：

> 多个线程高频访问的、彼此独立的变量，若挤在同一条缓存行里，会互相把对方的缓存顶爆；把它们各自撑满一条缓存行即可消除。

`CachePadded<T>` 就是把任意 `T` 包装成一个「独占一条缓存行」的容器，对外仍像 `T` 一样使用（靠 `Deref`/`DerefMut`），但内存布局上保证不会和邻居共享缓存行。

#### 4.1.2 核心流程

典型使用流程：

1. 把「会被某个线程高频独占访问」的字段类型从 `T` 改成 `CachePadded<T>`。
2. 用 `CachePadded::new(value)` 构造。
3. 之后像 `T` 一样用：`*padded` 取内部引用，`padded.fetch_add(...)` 等方法靠解引用自动转发。
4. 需要取回原值时用 `into_inner()`。

#### 4.1.3 源码精读

类型的顶层文档注释把 false sharing 讲得很清楚，这是理解整个文件的钥匙：

[src/cache_padded.rs:6-13](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/cache_padded.rs#L6-L13) —— 说明更新一个原子值会让整条缓存行失效，拖慢其他核，于是需要 `CachePadded` 把不同数据隔到不同缓存行。

文档紧接着给出了**尺寸与对齐的承诺**（N 表示目标架构的「猜测缓存行长度」）：

[src/cache_padded.rs:29-32](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/cache_padded.rs#L29-L32) —— 大小是「能容纳 `T` 的最小 N 整数倍」；对齐是 `max(N, align_of::<T>())`。

用公式表达（设 `s = size_of::<T>()`，`a = align_of::<T>()`，`N` 为该架构缓存行长度）：

\[
\text{size}(\text{CachePadded}\langle T\rangle)=\left\lceil \frac{s}{N}\right\rceil \cdot N
\]

\[
\text{align}(\text{CachePadded}\langle T\rangle)=\max(N, a)
\]

注意 \(\lceil s/N\rceil \cdot N\) 保证了大小是对齐的整数倍——这是 Rust 类型系统的硬要求：任何类型的大小都必须是其对齐的整数倍。

文档里还给了两个 doctest。第一个用 `i8` 演示对齐与间距：

[src/cache_padded.rs:38-48](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/cache_padded.rs#L38-L48) —— 两个 `CachePadded<i8>` 在数组里相邻，它们的地址差至少 32、且各自地址都是 32 的整数倍（在 N≥32 的架构上）。

第二个是最经典的真实场景——并发队列的 head/tail 各自独占缓存行：

[src/cache_padded.rs:54-63](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/cache_padded.rs#L54-L63) —— 队列把 `head`、`tail` 各包成 `CachePadded<AtomicUsize>`，让入队线程（改 tail）和出队线程（改 head）不互相干扰。

#### 4.1.4 代码实践

**实践目标：** 直观看到 false sharing 的存在（用源码阅读 + 跑一个最小例子）。

**操作步骤：**

1. 阅读 [src/cache_padded.rs:50-63](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/cache_padded.rs#L50-L63) 的注释，理解「并发队列 head/tail 分行」这个动机。
2. 在本地新建一个依赖了 `crossbeam-utils` 的 binary crate，把官方 doctest 改成可运行的小程序，打印两个相邻 `CachePadded<i8>` 的地址差与对齐：

```rust
// 示例代码：验证 CachePadded 的地址间距
use crossbeam_utils::CachePadded;

fn main() {
    let array = [CachePadded::new(1i8), CachePadded::new(2i8)];
    let addr1 = &*array[0] as *const i8 as usize;
    let addr2 = &*array[1] as *const i8 as usize;
    println!("addr1 = {:#x}", addr1);
    println!("addr2 = {:#x}", addr2);
    println!("delta = {}", addr2 - addr1);
    println!("addr1 % 128 = {}", addr1 % 128); // 在 x86-64 上应为 0
}
```

**需要观察的现象：** 在 x86-64 机器上，`delta` 应为 128（一条 128 字节「猜测缓存行」），两个地址都是 128 的整数倍。

**预期结果：** `delta == 128`、`addr1 % 128 == 0`。如果你在 32 字节缓存行的架构（如 `arm`）上跑，`delta` 会是 32。若本地无法运行，标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1：** 假设把两个 `AtomicUsize`（各 8 字节）紧挨着放进一个 `struct`，两个线程各只自增其中一个。为什么即使没有加锁、也没有数据竞争，性能也会很差？

**参考答案：** 因为两个 8 字节字段几乎必然落在同一条 64 字节缓存行内，产生 false sharing：每核每次写都让对方的缓存行副本失效，导致两核都不停 cache miss。

**练习 2：** `CachePadded` 能不能防止「真正的数据竞争」（两线程同时无锁写同一变量）？

**参考答案：** 不能。`CachePadded` 只改变**内存布局**（对齐与填充），不引入任何同步。它防的是 false sharing，不是 data race。后者仍需 `AtomicXxx`、`Mutex` 等同步原语。

---

### 4.2 `repr(align)` 分架构分支：缓存行长度的「悲观猜测」

#### 4.2.1 概念说明

要给 `T` 对齐到「一条缓存行」，就必须知道目标机器的缓存行长度。但 Rust 在编译期并不知道运行机器的真实缓存行长度——它只知道 **`target_arch`（目标架构）**。所以 `CachePadded` 的做法是：**为每个架构硬编码一个「合理猜测值」**。

这一节的关键认知是：

- `N` 是**编译期常量**，不是运行期探测。
- `N` 是「悲观猜测（pessimistic guess）」——宁可多填一点，也不要低估导致 false sharing 死灰复燃。
- 原文明确说：`N` 不保证等于真实缓存行长度。

#### 4.2.2 核心流程

文件用一连串互斥的 `#[cfg_attr(target_arch = "...", repr(align(N)))]` 来选 `N`，最后用一个 `not(any(...))` 兜底。各架构取值汇总如下：

| 架构 | N | 来源依据 |
| --- | --- | --- |
| `x86_64`、`aarch64`、`arm64ec`、`powerpc64` | 128 | Intel 空间预取器成对拉取两条 64B 行；aarch64 big 核 128B 行；powerpc64 128B 行 |
| `arm`、`mips`、`mips32r6`、`mips64`、`mips64r6`、`sparc`、`hexagon` | 32 | 这些架构 32B 缓存行 |
| `m68k` | 16 | 16B 缓存行 |
| `s390x` | 256 | 256B 缓存行 |
| 其余（`x86`、`wasm`、`riscv32/64`、`sparc64` 等） | 64 | 64B 缓存行，作为兜底 |

> 为什么 x86-64 明明物理缓存行是 64 字节，却对齐到 **128**？这是本节最重要的一点，原文有专门说明（见 4.2.3）。

#### 4.2.3 源码精读

结构体定义前面是一长串 `cfg_attr`，每段都带详细注释和外部引用（Intel 优化手册、Linux 内核源码、Go 运行时的 CPU 探测代码）。

**128 字节分支**（x86_64 / aarch64 / arm64ec / powerpc64）：

[src/cache_padded.rs:65-90](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/cache_padded.rs#L65-L90) —— 注释解释从 Intel Sandy Bridge 起，空间预取器会**成对**拉取相邻的两条 64B 行，所以即使两个变量各占一条 64B 行也可能被预取到一起，必须对齐到 128B 才安全。

**32 字节分支**（arm / mips / sparc / hexagon 等）：

[src/cache_padded.rs:91-111](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/cache_padded.rs#L91-L111) —— 这些架构缓存行只有 32B。

**16 / 256 字节**（m68k / s390x）：

[src/cache_padded.rs:112-122](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/cache_padded.rs#L112-L122) —— 两个边角架构分别取 16 与 256。

**64 字节兜底分支**（用 `not(any(...))` 列出所有上面已处理的架构，剩下的都走 64B）：

[src/cache_padded.rs:123-149](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/cache_padded.rs#L123-L149) —— x86、wasm、riscv、sparc64 等都是 64B；其余未知架构也乐观假设 64B。

最后才是真正的结构体——**只有一个字段 `value: T`**：

[src/cache_padded.rs:150-152](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/cache_padded.rs#L150-L152) —— `pub struct CachePadded<T> { value: T }`，靠上面的 `repr(align(N))` 撑大。

> 注意它**不是** `#[repr(transparent)]`。`CachePadded` 有意改变大小和对齐（这是它存在的全部意义），所以不能用 transparent。

#### 4.2.4 代码实践

**实践目标：** 用编译期断言体会「N 是按架构固定的常量」。

**操作步骤：**

阅读公开测试 `distance`，它用 `align_of::<CachePadded<()>>()` 读出对齐量并断言 `>= 32`：

[tests/cache_padded.rs:24-32](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/tests/cache_padded.rs#L24-L32) —— 用 `CachePadded<()>`（`size_of::<()> == 0`）测出「纯对齐量」，因为零大小类型不掺入尺寸影响。

在你的 binary 里加这段（示例代码）：

```rust
use std::mem;

fn main() {
    // CachePadded<()> 的对齐就是该架构的 N（无尺寸干扰）
    println!("align CachePadded<()> = {}", mem::align_of::<crossbeam_utils::CachePadded<()>>());
    println!("size  CachePadded<u8> = {}", mem::size_of::<crossbeam_utils::CachePadded<u8>>());
    println!("size  CachePadded<[u64;9]> = {}", mem::size_of::<crossbeam_utils::CachePadded<[u64;9]>>());
}
```

**需要观察的现象：**

- 在 x86-64 上：`align_of::<CachePadded<()>>()` 应为 128。
- `size_of::<CachePadded<u8>>()` 应为 128（1 字节撑到 128）。
- `size_of::<CachePadded<[u64;9]>>()` 应为 192：`[u64;9]` 是 72 字节，向上取整到 128 的整数倍即 192。

**预期结果：** 上述三个值在 x86-64 上分别约等于 128 / 128 / 192。若架构不同，按 4.2.2 表格里的 N 自行换算。待本地验证。

#### 4.2.5 小练习与答案

**练习 1：** 为什么 `x86_64` 不用更「精确」的 64，而要悲观地用 128？少填一半内存不好吗？

**参考答案：** 现代 Intel 处理器的空间预取器会成对预取相邻的两条 64B 缓存行。即使两个变量各占一条 64B 行，只要地址相邻，预取器仍可能把它们一起拉进缓存，重新引发 false sharing。对齐到 128B 是为堵住这个硬件行为，是「宁可浪费内存也要保证正确性」的悲观取舍。

**练习 2：** 假如出现一种全新的 `target_arch`，本文件没有任何 `cfg_attr` 匹配它，会怎样？

**参考答案：** 会落到最后的 `not(any(...))` 兜底分支，取 `repr(align(64))`。也就是说未知架构被乐观假设为 64B 缓存行。如果该架构真实缓存行更长，就可能仍有 false sharing；这正是文档强调「N 只是合理猜测、不保证匹配真实机器」的原因。

**练习 3：** `align(N)` 里的 `N` 必须满足什么约束？为什么 `CachePadded` 能做到「大小也是 N 的整数倍」？

**参考答案：** `N` 必须是 2 的幂。Rust 类型系统要求「任何类型的大小必须是其对齐的整数倍」，所以编译器会自动在 `value` 后补 padding，让 `size_of` 向上取整为 N 的倍数——这正是 4.1.3 里公式的来源。

---

### 4.3 `new` / `into_inner` / `Deref` / `DerefMut` 与 trait 转发

#### 4.3.1 概念说明

`CachePadded<T>` 的 API 设计哲学是「**透明包装**」：它只在内存布局上做文章（对齐 + 填充），在类型层面的行为尽量与 `T` 保持一致。具体表现为：

- 构造：`new(t)` / `From<T>` / `Default`（要求 `T: Default`）。
- 取值：`into_inner(self) -> T`。
- 像引用一样用：实现了 `Deref<Target = T>` 与 `DerefMut`，所以 `&*padded`、`padded.method()` 都直接作用在内部 `T` 上。
- 自动 trait 转发：`Send`/`Sync` 按 `T` 决定；`Clone`/`Copy`/`Debug`/`Display`/`Hash`/`PartialEq`/`Eq`/`Default` 也都派生自 `T`。

#### 4.3.2 核心流程

一条「装取用」链路：

```
T ──new/from──▶ CachePadded<T> ──deref──▶ &T ──调用 T 的方法──▶ 结果
                                   │
                                   └──into_inner──▶ T
```

#### 4.3.3 源码精读

结构体上面派生了一组 trait：

[src/cache_padded.rs:64](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/cache_padded.rs#L64) —— `#[derive(Clone, Copy, Default, Hash, PartialEq, Eq)]`。注意派生出的实现都带 `T: 该 trait` 约束，例如 `Copy` 只有在 `T: Copy` 时才成立。所以 `CachePadded<AtomicUsize>` 是 `Clone`（`AtomicUsize: Clone`），但**不是** `Copy`（`AtomicUsize: !Copy`）。

`Send`/`Sync` 用 unsafe 手写，但只是「原样转发」`T` 的对应 bound：

[src/cache_padded.rs:154-155](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/cache_padded.rs#L154-L155) —— `impl<T: Send> Send`、`impl<T: Sync> Sync`。`CachePadded` 没有引入任何新的线程安全约束，它的「线程安全性」完全等同于 `T`。

构造与取回：

[src/cache_padded.rs:167-169](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/cache_padded.rs#L167-L169) —— `new` 是 `const fn`，可以在 `const` / `static` 上下文里构造（这点对 4.4 节的全局锁池至关重要）。

[src/cache_padded.rs:182-184](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/cache_padded.rs#L182-L184) —— `into_inner` 消费自身、交还内部 `T`。

解引用（这是「像 `T` 一样用」的关键）：

[src/cache_padded.rs:187-199](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/cache_padded.rs#L187-L199) —— `Deref::Target = T`，`deref` 返回 `&self.value`，`deref_mut` 返回 `&mut self.value`。于是 `padded.fetch_add(1, Ordering::Relaxed)` 这种调用会自动解引用到内部 `AtomicUsize`。

另外两个便利 trait：

[src/cache_padded.rs:209-213](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/cache_padded.rs#L209-L213) —— `From<T>` 让 `T.into()` 即可得到 `CachePadded<T>`。

[src/cache_padded.rs:201-207](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/cache_padded.rs#L201-L207) 与 [src/cache_padded.rs:215-219](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/cache_padded.rs#L215-L219) —— `Debug` 输出形如 `CachePadded { value: ... }`，`Display` 直接转发内部值的 `Display`。

#### 4.3.4 代码实践

**实践目标：** 体验「`CachePadded<T>` 像 `T` 一样用」。

**操作步骤：** 阅读公开测试 `drops`，它验证自定义 `Drop` 的类型被 `CachePadded` 包装后，drop 仍正确发生（说明 `CachePadded` 不会吞掉内部值的析构）：

[tests/cache_padded.rs:65-85](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/tests/cache_padded.rs#L65-L85) —— 两次 `drop` 让计数器从 0 涨到 2。

再在你的 binary 里写一段（示例代码）：

```rust
use crossbeam_utils::CachePadded;
use std::sync::atomic::{AtomicUsize, Ordering};

fn main() {
    let counter = CachePadded::new(AtomicUsize::new(0));
    // 直接调用 AtomicUsize 的方法，靠 Deref 转发：
    counter.fetch_add(1, Ordering::Relaxed);
    counter.fetch_add(41, Ordering::Relaxed);
    assert_eq!(counter.load(Ordering::Relaxed), 42);

    // into_inner 取回原值：
    let inner: AtomicUsize = counter.into_inner();
    assert_eq!(inner.into_inner(), 42);
}
```

**需要观察的现象：** `counter.fetch_add(...)` 无需任何 `*` 解引用就能编译通过——证明 `Deref` 自动生效。

**预期结果：** 程序无断言失败地结束。

#### 4.3.5 小练习与答案

**练习 1：** `CachePadded::new` 被声明为 `const fn`。这一点对后续哪个用法是必须的？

**参考答案：** 对 4.4 节里 `AtomicCell` 的全局锁池 `static LOCKS: [CachePadded<SeqLock>; 67] = [L; LEN];` 是必须的——`static` 数组的初始值必须是常量表达式，所以 `new` 必须 `const`。同理 doctest 里的 `const`/`static` 也依赖这一点。

**练习 2：** 下面代码能编译吗？为什么？
```rust
let a = CachePadded::new(std::sync::atomic::AtomicUsize::new(0));
let b = a; // 试图 Copy
```

**参考答案：** 不能。`AtomicUsize: !Copy`，而 `CachePadded` 的 `Copy` 派生要求 `T: Copy`，所以 `CachePadded<AtomicUsize>: !Copy`。`let b = a;` 会移动 `a` 而非复制；若之后再用 `a` 就会编译失败。要复制得显式 `a.clone()`。

---

### 4.4 与原子锁池、ShardedLock 的结合：为什么内部也离不开它

#### 4.4.1 概念说明

`CachePadded` 不只是给用户用的——`crossbeam-utils` 自己内部有两处「必须」用它，否则自家原语就会被 false sharing 拖垮。这两处共同的特征是：**「一个数组/切片里装了很多把独立的锁，不同线程会同时操作不同的锁」**。如果这些锁在内存里挤在一起，明明用的是不同的锁，却仍会因为落在同一条缓存行里而互相干扰。

两处分别是：

1. `AtomicCell` 的**全局锁池**：当 `T` 无法原子化时，所有操作回退到 `[CachePadded<SeqLock>; 67]` 这 67 把锁组成的全局锁池（详见 [u2-l3](./u2-l3-atomiccell-global-lock-seqlock.md)）。
2. `ShardedLock` 的**分片**：内部是 `Box<[CachePadded<Shard>]>`，读操作只锁其中一个分片（详见 [u3-l3](./u3-l3-shardedlock.md)）。

#### 4.4.2 核心流程

**锁池场景**（`AtomicCell` 回退路径）：

```
线程 A 操作 addr1 ──▶ lock(addr1) = LOCKS[addr1 % 67]  ──┐
                                                          ├─ 两把锁若同处一条缓存行 → false sharing
线程 B 操作 addr2 ──▶ lock(addr2) = LOCKS[addr2 % 67]  ──┘
```

每个 `SeqLock` 内部只有一个 `AtomicUsize`（8 字节）。若不加 `CachePadded`，67 把锁会紧密排列，一条 64B 缓存行能塞下 8 把锁——于是 67 把锁只占 ~9 条缓存行，线程们极易撞到同一条行上。包成 `CachePadded<SeqLock>` 后，每把锁独占 128B（x86-64），各自一条缓存行，互不干扰。

#### 4.4.3 源码精读

**第一处：`AtomicCell` 的全局锁池。** 先看 `SeqLock` 的大小，理解为什么非填不可：

[src/atomic/seq_lock.rs:9-15](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/seq_lock.rs#L9-L15) —— `SeqLock` 只有一个 `state: AtomicUsize` 字段，8 字节。这正是它需要被撑大的原因。

再看锁池定义与 `lock()` 选锁逻辑：

[src/atomic/atomic_cell.rs:965-994](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L965-L994) —— `lock(addr)` 函数按地址取模从静态数组里选一把锁。

其中关键三行：

[src/atomic/atomic_cell.rs:988-990](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L988-L990) —— `LEN = 67`（素数，避免按 2 的幂对齐退化成只用一半桶），`const L: CachePadded<SeqLock> = CachePadded::new(SeqLock::new());`，`static LOCKS: [CachePadded<SeqLock>; LEN] = [L; LEN];`。

> 这里 `CachePadded::new` 必须是 `const fn`（见 4.3.5 练习 1），因为 `L` 和 `LOCKS` 都是 `const`/`static`。也正因 `SeqLock::new()` 是 `const fn`、且 `CachePadded` 派生了 `Copy`（对 `SeqLock: Copy`，因为它只有一个 `AtomicUsize` 字段），`[L; LEN]` 这种数组语法才能用。

[src/atomic/atomic_cell.rs:994](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L994) —— `&LOCKS[addr % LEN]` 返回 `&'static SeqLock`。注意返回的是 `&SeqLock` 而非 `&CachePadded<SeqLock>`——因为 `CachePadded` 实现了 `Deref<Target = T>`，外层只需看见内部的 `SeqLock` API。

**第二处：`ShardedLock` 的分片。**

[src/sync/sharded_lock.rs:82-88](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/sharded_lock.rs#L82-L88) —— `ShardedLock` 的 `shards: Box<[CachePadded<Shard>]>`。每个 `Shard` 包一把 `RwLock<()>`，多个读者会各自锁不同的分片，所以分片之间必须分行。

构造时逐个 `CachePadded::new(Shard { ... })`：

[src/sync/sharded_lock.rs:106-118](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/sharded_lock.rs#L106-L118) —— 在 `new` 里用 `(0..NUM_SHARDS).map(|_| CachePadded::new(Shard { ... })).collect()` 建立分片数组。

这两处共同印证了 `CachePadded` 的设计价值：**它把「缓存行对齐」这件事做得足够轻量（一个 `const fn`、两个 `Deref`），以至于内部数据结构可以毫无负担地把它当作「防止 false sharing 的标准构件」反复使用。**

#### 4.4.4 代码实践

**实践目标：** 通过源码阅读 + 时序推理，理解锁池为何「必须」填充。

**操作步骤：**

1. 打开 [src/atomic/atomic_cell.rs:988-990](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L988-L990)，确认 `LOCKS` 的元素类型是 `CachePadded<SeqLock>`。
2. 打开 [src/atomic/seq_lock.rs:9-15](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/seq_lock.rs#L9-L15)，确认 `SeqLock` 仅 8 字节。
3. 做一道「纸上推演」：假设把 `CachePadded` 去掉、`LOCKS` 变成 `[SeqLock; 67]`，在 x86-64（64B 缓存行，悲观 128B）上：
   - 67 个 `SeqLock` 紧密排列，前 8 个落在第 0 条缓存行（按 64B 算），第 9~16 个落在第 1 条，依此类推。
   - 线程 A 选到 `LOCKS[3]`、线程 B 选到 `LOCKS[5]`——两把**不同的**锁，却落在**同一条**缓存行。
   - 于是 A 持锁时改 `state`（写 `LOCKS[3].state`）会让 B 那条缓存行失效，B 接下来的 `optimistic_read`（读 `LOCKS[5].state`）就 cache miss。

**需要观察的现象：** 把上述推演画成「地址轴」草图，标出哪几把锁共用一条缓存行。

**预期结果：** 不填充时，相邻 8 把锁共用一条 64B 缓存行；填充后每把锁独占一条，false sharing 消失。这是源码阅读型实践，无需运行命令。

#### 4.4.5 小练习与答案

**练习 1：** `lock()` 返回 `&'static SeqLock`，但 `LOCKS` 的元素类型是 `CachePadded<SeqLock>`，这个类型转换是怎么发生的？

**参考答案：** 通过 `Deref`。`CachePadded<SeqLock>` 实现了 `Deref<Target = SeqLock>`，在 `&LOCKS[addr % LEN]`（类型 `&CachePadded<SeqLock>`）被当作 `&SeqLock` 返回时，Rust 自动插入解引用。返回类型在 [src/atomic/atomic_cell.rs:965](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/atomic_cell.rs#L965) 写的是 `&'static SeqLock`。

**练习 2：** `ShardedLock` 的卖点之一是「多个读者可真正并行」。如果它的 `shards` 不用 `CachePadded`，这个卖点会打几折？

**参考答案：** 会大打折扣。`ShardedLock` 让不同读者锁不同分片来达到并行，但若多个分片挤在同一条缓存行，读者改写自己分片的锁状态（如 `RwLock` 内部计数）会让其他读者所在核的缓存行失效，退化为类似 false sharing 的频繁 cache miss——「分片」的并行收益被吃掉。这正是 `shards: Box<[CachePadded<Shard>]>` 必须填充的原因。

---

## 5. 综合实践

本讲的综合实践是一个端到端的小基准，把「false sharing 真实存在」「`CachePadded` 能消除它」「测量可见」三件事一次跑通。

**任务：** 实现两个版本的多线程自增计数器，对比耗时。

- 版本 A（有 false sharing）：两个 `AtomicUsize` 紧挨着放在 `struct` 里。
- 版本 B（无 false sharing）：用 `CachePadded` 各自包装。

每版本都开两个线程，分别只疯狂自增其中一个字段 `N` 次，主线程 join 后测量总耗时。示例代码：

```rust
// 示例代码：对比 false sharing 与 CachePadded
// Cargo.toml: crossbeam-utils = "0.8"
use crossbeam_utils::CachePadded;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::thread;
use std::time::Instant;

const N: usize = 50_000_000;

// 版本 A：两字段紧密相邻，大概率同处一条缓存行
struct Counters {
    a: AtomicUsize,
    b: AtomicUsize,
}

// 版本 B：两字段各自独占缓存行
struct CountersPadded {
    a: CachePadded<AtomicUsize>,
    b: CachePadded<AtomicUsize>,
}

fn main() {
    for (label, run) in [
        ("plain  ", run_plain as fn() -> usize),
        ("padded ", run_padded as fn() -> usize),
    ] {
        let t = Instant::now();
        let sum = run();
        println!("{} {:?}  sum={}", label, t.elapsed(), sum);
    }
}

fn run_plain() -> usize {
    let c = Box::leak(Box::new(Counters {
        a: AtomicUsize::new(0),
        b: AtomicUsize::new(0),
    }));
    let t1 = thread::spawn(move || {
        for _ in 0..N { c.a.fetch_add(1, Ordering::Relaxed); }
    });
    let t2 = thread::spawn(move || {
        for _ in 0..N { c.b.fetch_add(1, Ordering::Relaxed); }
    });
    t1.join().unwrap();
    t2.join().unwrap();
    c.a.load(Ordering::Relaxed) + c.b.load(Ordering::Relaxed)
}

fn run_padded() -> usize {
    let c = Box::leak(Box::new(CountersPadded {
        a: CachePadded::new(AtomicUsize::new(0)),
        b: CachePadded::new(AtomicUsize::new(0)),
    }));
    let t1 = thread::spawn(move || {
        for _ in 0..N { c.a.fetch_add(1, Ordering::Relaxed); }
    });
    let t2 = thread::spawn(move || {
        for _ in 0..N { c.b.fetch_add(1, Ordering::Relaxed); }
    });
    t1.join().unwrap();
    t2.join().unwrap();
    c.a.load(Ordering::Relaxed) + c.b.load(Ordering::Relaxed)
}
```

**操作步骤：**

1. 新建 binary crate，加 `crossbeam-utils` 依赖。
2. 贴入上述代码，`cargo run --release`（务必用 release，否则自旋本身的开销会盖过缓存效应）。
3. 多跑几次取稳定值；可尝试把 `N` 调大/调小观察差异。

**需要观察的现象：** `padded` 版本通常明显快于 `plain` 版本（在双核以上、且两核被调度到不同物理核时尤其显著）。

**预期结果：** `padded` 比 `plain` 快（具体倍数依赖机器，常见为 1.5x~数倍）。两版本的 `sum` 都应等于 `2 * N`，证明结果正确、差异纯粹来自缓存布局。若你机器上差异不明显（如单核、或调度到同一核的 SMT 兄弟核），标注「待本地验证」并解释可能原因。

> 进阶：用 `cargo asm` 或 `println!("{:p}", &c.a as *const _)` 打印两个字段地址，确认 `plain` 版两地址差 8 字节（同缓存行）、`padded` 版差 128 字节（不同缓存行）。

---

## 6. 本讲小结

- `CachePadded<T>` 只解决一个问题——**false sharing**：多个线程高频访问的、彼此独立的变量若挤在同一条缓存行里，会互相顶爆对方缓存。
- 它通过 `#[repr(align(N))]` 把 `T` 撑大到「一整条缓存行」；`N` 是**按 `target_arch` 编译期硬编码的悲观猜测**（x86-64/aarch64/powerpc64=128，arm/mips/sparc/hexagon=32，m68k=16，s390x=256，其余=64），不保证等于真实机器。
- 尺寸是「容纳 `T` 的最小 N 整数倍」，对齐是 `max(N, align_of::<T>())`；因此 `CachePadded<u8>` 在 x86-64 上是 128 字节。
- API 是「透明包装」：`new`（`const fn`）/ `into_inner` / `Deref` / `DerefMut` / `From`，外加原样转发的 `Send`/`Sync`/`Clone`/`Copy` 等 trait——`CachePadded` 的行为对 `T` 几乎透明。
- `crossbeam-utils` 自己内部两处依赖它：`AtomicCell` 的全局锁池 `[CachePadded<SeqLock>; 67]` 和 `ShardedLock` 的 `Box<[CachePadded<Shard>]>`——两者都是「数组里装多把独立锁」的典型反 false sharing 场景。

## 7. 下一步学习建议

- **横向承接（推荐先做）：** 本讲是 [u3-l3 ShardedLock](./u3-l3-shardedlock.md) 的硬前置。学完本讲后再读 ShardedLock，重点观察它如何用 `ThreadId` 选分片，并把「分片 + 缓存行填充」二者结合起来实现「读快写慢」。
- **回看锁池：** 对照 [u2-l3 AtomicCell 全局锁回退与 SeqLock](./u2-l3-atomiccell-global-lock-seqlock.md)，本讲 4.4 节解释了锁池「为何要 `CachePadded`」，那篇讲义则解释锁池「`SeqLock` 印戳如何工作」。两者拼起来才是完整的回退路径。
- **延伸阅读：** 想深入理解 false sharing 与硬件预取，可读 `src/cache_padded.rs` 注释里引用的 Intel 64 与 IA-32 架构优化手册，以及 Go 运行时的 `internal/cpu` 探测代码——它们正是各架构 `N` 取值的来源。
- **动手验证：** 把综合实践的基准改成「3 个字段、3 个线程」或「字段间手工插入 `[u64; 15]` 填充」，看不同填充策略下的耗时曲线，体会「手工填充」与「`CachePadded`」的等价性。
