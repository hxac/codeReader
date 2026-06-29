# Observer trait：观察缓冲区状态而不触碰数据

## 1. 本讲目标

学完本讲后，你应该能够：

- 说出 `Observer` trait 在 ringbuf trait 体系中的定位——它是「只读观测」能力的集合，是 `Producer`/`Consumer`/`RingBuffer` 等其他 trait 的基础。
- 列出 `Observer` 提供的状态查询方法（容量、读写索引、占用数、空闲数、空/满判断、hold 标志查询），并知道哪些是必须实现的「底层方法」、哪些是 trait 提供默认实现的「派生方法」。
- 用模运算手算 `occupied_len`/`vacant_len`/`is_empty`/`is_full`，并解释为什么这些公式要「先加模数再取余」。
- 理解并标注：在多线程并发下，这些观测值为什么只是「瞬时快照」，可能在下一刻就已失效。
- 看懂 `Based` trait 与 `DelegateObserver` 的委托机制，明白 `Prod`/`Cons`/`Obs` 等包装器为什么几乎不写代码就能拥有 `capacity()`、`occupied_len()` 等方法。

## 2. 前置知识

本讲假设你已经掌握以下内容（来自前置讲义）：

- **双索引与 2\*capacity 模数**（u2-l1）：ringbuf 用落在 `0..2*capacity` 区间的 `read`/`write` 两个索引描述状态。`read % capacity` 指向最旧元素，`write % capacity` 指向下一个空槽；空 ⟺ `read == write`，满 ⟺ `(write - read) % (2*capacity) == capacity`。
- **ranges()**（u2-l1）：把环形区间 `[start, end)` 映射成最多两段线性切片。
- **Storage 抽象**（u2-l2）：元素以 `MaybeUninit<T>` 形式存放在连续内存区，初始化状态由读写索引管理，而非由存储本身管理。
- **LocalRb 与 SharedRb**（u2-l3）：前者用 `Cell` 存索引（单线程），后者用 `CachePadded<AtomicUsize>` 存索引（多线程、无锁），二者实现同一套环形缓冲区算法。
- **Split / SplitRef**（u2-l4）：缓冲区可拆成 `Producer`/`Consumer` 两个句柄，二者共享同一底层存储与索引；`SharedRb::split()` 默认产出 `CachingProd/CachingCons`。

**本讲新引入的术语：**

- **观测（observe）**：只读取缓冲区的「元状态」（有多少元素、是否空/满、读写到哪了），而不安全地读写具体元素值。
- **委托（delegate）**：包装器不重复实现方法，而是把调用转发给内部被包装的对象。
- **快照（snapshot）**：在并发场景下，一次观测拿到的值只反映「观测那一瞬间」的事实，不代表它永远成立。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/traits/observer.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/observer.rs) | 定义 `Observer` trait 本体、`DelegateObserver` 委托标记 trait，以及把 `Observer` 委托给 `Base` 的 blanket impl。本讲的核心文件。 |
| [src/traits/utils.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/utils.rs) | 定义 `Based` trait（包装器指向内部实现的「基座」）和 `modulus()` 辅助函数（返回 `2 * capacity`）。 |
| [src/rb/shared.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs) | 多线程 `SharedRb` 对 `Observer` 的具体实现：索引用原子 `Acquire` 读，hold 标志也用原子读。 |
| [src/rb/local.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/local.rs) | 单线程 `LocalRb` 对 `Observer` 的具体实现：索引与 hold 标志用 `Cell` 读取，无原子开销。 |
| [src/rb/utils.rs](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/utils.rs) | `ranges()` 函数，`unsafe_slices` 的底层依赖。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **4.1** `Observer` trait 的整体职责——只读地「观测」缓冲区。
2. **4.2** 占用数、空闲数、空/满的计算，以及并发下的「瞬时性」。
3. **4.3** `Based` 与 `DelegateObserver`——观测能力如何传播到包装器。

### 4.1 Observer trait：只读地「观测」缓冲区

#### 4.1.1 概念说明

前面几讲你已经会 `try_push` 写、`try_pop` 读。但很多场景下，你只想**问缓冲区几个问题**而不改动它：

- 这个缓冲区能装多少个元素？（容量）
- 现在装了多少个？还有多少空位？
- 它现在空了吗？满了吗？
- 读写指针分别走到哪里了？
- 这个读端 / 写端目前有没有被某个 consumer / producer 占用？

这些「只读查询」能力被抽象成一个 trait：`Observer`。它的文档注释一句话点明了定位：

> Can observe ring buffer state but cannot safely access its data.
> （可以观测缓冲区状态，但无法**安全地**访问它的数据。）

注意后半句「无法安全地访问数据」。`Observer` 里确实有一个能拿到元素内存的方法 `unsafe_slices`，但它返回的是 `&[MaybeUninit<Item>]`（未初始化的内存），并且方法本身标记为 `unsafe`——这意味着「读取具体元素」这件事被刻意推到了 unsafe 的领域。`Observer` 提供的**安全**方法，只让你看到「状态」，看不到「数据内容」。这是 ringbuf 把「观测」与「读写」分离的核心设计：**把最基础、最通用、最不可能出错的能力，单独抽成最小公约数 trait**，让所有更高级的 trait（`Producer`、`Consumer`、`RingBuffer`）和所有包装器（`Prod`/`Cons`/`Obs`/`CachingProd`/`CachingCons`/`FrozenProd`/`FrozenCons`）都能共享它。

#### 4.1.2 核心流程

`Observer` 的方法可以分成三组：

1. **基础元信息（必须由具体实现提供）**
   - `capacity()`：容量，返回 `NonZeroUsize`，缓冲区生命周期内恒定。
   - `read_index()`：最旧元素的索引（取值 `0..2*capacity`）。
   - `write_index()`：下一个空槽的索引（取值 `0..2*capacity`）。

2. **直接内存访问（必须提供，但标记 `unsafe`）**
   - `unsafe_slices(start, end)`：返回 `[start, end)` 区间对应的（最多两段）`&[MaybeUninit<Item>]`。
   - `unsafe_slices_mut(start, end)`：可变版本。

3. **派生状态查询（trait 给默认实现，基于上面方法算出来）**
   - `occupied_len()`：当前已存元素数。
   - `vacant_len()`：当前空位数。
   - `is_empty()` / `is_full()`：空 / 满。
   - `read_is_held()` / `write_is_held()`：读端 / 写端是否被占用（hold 标志，必须由具体实现提供）。

也就是说，**一个类型只要实现了 `capacity`、两个 `*_index`、两个 `unsafe_slices*`、两个 `*_is_held` 这 7 个底层方法，就自动拥有了 `occupied_len`/`vacant_len`/`is_empty`/`is_full`**——它们是 trait 提供的默认实现。

#### 4.1.3 源码精读

**`Observer` trait 的定义**——注意每个方法上方文档里对取值范围和并发语义的提示：

[src/traits/observer.rs:7-77](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/observer.rs#L7-L77) 定义了整个 trait。其中 `capacity`、`read_index`、`write_index`、`unsafe_slices`、`unsafe_slices_mut`、`read_is_held`、`write_is_held` 是没有默认实现、必须被具体类型实现的「底层方法」；而 `occupied_len`、`vacant_len`、`is_empty`、`is_full` 带 `{ ... }` 默认实现，是「派生方法」。

关键几点：

- `capacity` 返回 `NonZeroUsize`，注释明确「**It is constant during the whole ring buffer lifetime**」（缓冲区生命周期内恒定）——见 [src/traits/observer.rs:10-13](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/observer.rs#L10-L13)。
- `read_index`/`write_index` 的注释写明取值范围 `0..(2 * capacity)`，见 [src/traits/observer.rs:15-22](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/observer.rs#L15-L22)。这正是 u2-l1 讲过的「2\*capacity 模数」设计。
- `unsafe_slices` 带 `# Safety` 文档，要求「切片不得与任何同时存在的可变切片重叠」「非 `Sync` 的元素不得被多线程同时访问」，见 [src/traits/observer.rs:24-31](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/observer.rs#L24-L31)。底层依赖 `ranges()` 把环形区间切成两段线性切片（u2-l1）。

**`SharedRb` 如何实现这些底层方法**——多线程版本用原子操作：

[src/rb/shared.rs:87-121](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs#L87-L121) 是 `SharedRb` 的 `Observer` 实现。重点：

- `capacity()` 直接取 `storage.len()`，见 [src/rb/shared.rs:90-93](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs#L90-L93)。
- `read_index()` / `write_index()` 用 `Ordering::Acquire` 从原子变量加载，见 [src/rb/shared.rs:96-102](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs#L96-L102)。`Acquire` 读是为了和写端的 `Release` 写配合，建立跨线程的 happens-before（详见 u5-l1）。
- `read_is_held` / `write_is_held` 也用 `Acquire` 读原子布尔，见 [src/rb/shared.rs:114-120](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs#L114-L120)。

**`LocalRb` 如何实现同样的底层方法**——单线程版本用 `Cell`，无原子开销：

[src/rb/local.rs:71-105](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/local.rs#L71-L105) 是 `LocalRb` 的 `Observer` 实现。对比 `SharedRb`：

- `read_index()` 是 `self.read.index.get()`（`Cell` 读取），见 [src/rb/local.rs:80-82](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/local.rs#L80-L82)，没有 `Acquire`、没有缓存同步，因此「略快」（对应 lib.rs 里对 `LocalRb` 的描述）。
- `read_is_held` 也是 `Cell<bool>` 读取，见 [src/rb/local.rs:98-100](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/local.rs#L98-L100)。

两者**算术完全相同**（都是同一套 `Observer` 派生方法），差别只在索引的「存储介质」与「是否跨核同步」。这正是 u2-l3 讲过的结论在本讲的再次印证。

#### 4.1.4 代码实践

**实践目标**：跑通一个最小程序，对 split 出来的 `Producer`/`Consumer` 调用 `Observer` 的各种查询方法，观察它们返回什么。

**操作步骤**：

1. 在 `examples/` 目录下新建一个示例文件（或写进一个 `cargo new` 出来的二进制里），内容如下（**示例代码**）：

   ```rust
   use ringbuf::{traits::*, HeapRb};

   fn main() {
       let rb = HeapRb::<i32>::new(4);
       let (mut prod, mut cons) = rb.split();

       println!("capacity = {}", prod.capacity());
       println!("初始: read={} write={} occupied={} vacant={} empty={} full={}",
           prod.read_index(), prod.write_index(),
           prod.occupied_len(), prod.vacant_len(),
           prod.is_empty(), prod.is_full());

       prod.try_push(1).unwrap();
       prod.try_push(2).unwrap();
       prod.try_push(3).unwrap();
       println!("push 1,2,3 后: read={} write={} occupied={} vacant={}",
           cons.read_index(), cons.write_index(),
           cons.occupied_len(), cons.vacant_len());

       cons.try_pop();
       println!("pop 1 个后: read={} write={} occupied={} vacant={}",
           cons.read_index(), cons.write_index(),
           cons.occupied_len(), cons.vacant_len());
   }
   ```

   注意 `use ringbuf::{traits::*, HeapRb};` 会把 `Observer`（连同 `Producer`/`Consumer`）一并引入作用域，所以 `capacity()`、`read_index()` 等方法可直接调用。

2. 运行（在仓库根目录）：

   ```bash
   cargo run --example <你的示例名>
   ```

   > 待本地验证：实际输出取决于你把它放成 example 还是独立 crate。

**需要观察的现象**：

- `capacity` 全程为 `4`，不变。
- 初始 `read=0 write=0`，`occupied=0 vacant=4`，`empty=true full=false`。
- push 三个后 `read=0 write=3`，`occupied=3 vacant=1`。
- pop 一个后 `read=1 write=3`，`occupied=2 vacant=2`。

**预期结果**：`prod` 上调用的查询值与 `cons` 上一致（因为它们共享同一底层 `SharedRb`），且都与手动按索引计数的结果一致。这说明 `Observer` 的查询不依赖你从哪一端调用。

#### 4.1.5 小练习与答案

**练习 1**：`Observer` trait 里哪些方法有默认实现、哪些必须由具体类型实现？

**答案**：`capacity`、`read_index`、`write_index`、`unsafe_slices`、`unsafe_slices_mut`、`read_is_held`、`write_is_held` 必须实现；`occupied_len`、`vacant_len`、`is_empty`、`is_full` 有默认实现（由前者算出）。

**练习 2**：为什么说 `Observer`「无法安全地访问数据」？

**答案**：唯一能触及元素内存的 `unsafe_slices(_mut)` 返回的是 `&[MaybeUninit<Item>]`，且方法本身标记 `unsafe`，调用者必须保证切片不重叠、非 `Sync` 元素不多线程访问等前提。安全的 `Observer` 方法只暴露「状态」（容量、索引、占用数），不暴露「数据内容」。

**练习 3**：`capacity` 返回 `NonZeroUsize` 而非 `usize`，有什么好处？

**答案**：它在类型层面保证容量不可能为 0，从而 `modulus = 2 * capacity` 也非零，后续取模运算不会除零；同时调用方拿到一个「编译期保证非零」的值，免去了运行期判空。

### 4.2 占用 / 空闲的计算与并发下的「瞬时性」

#### 4.2.1 概念说明

`occupied_len`（已存元素数）和 `vacant_len`（空位数）是写 `try_push`/`try_pop` 前最常查的两个值。它们并不独立存储，而是**用读写索引通过模运算实时算出来**。这一节我们精确地看清这套算术，并讲清一个对并发程序至关重要的警告：**这些值在多线程下只是瞬时快照**。

为什么要专门讲这个警告？因为新手很容易写出这样的错误代码：「先 `if !cons.is_empty()`，再 `cons.try_pop()`」，并以为这是安全的。在单线程里它确实成立；但在 `SharedRb` 多线程场景下，`is_empty()` 返回 `false` 之后、`try_pop()` 之前的那个瞬间，另一个线程的 producer 可能根本没来得及写入——或者反过来说，`is_empty()` 返回 `true` 之后，producer 可能立刻又写入了一个元素。源码的文档注释反复强调这一点，我们必须理解它的根源。

#### 4.2.2 核心流程

设容量为 \( c \)，模数 \( m = 2c \)，读索引 \( r \)，写索引 \( w \)。所有索引都在 \( 0..m \) 范围内，且合法状态要求占用数不超过 \( c \)。

**占用数**（`occupied_len`）：

\[
\text{occupied} = (m + w - r) \bmod m
\]

**空闲数**（`vacant_len`）：

\[
\text{vacant} = (c + r - w) \bmod m
\]

**为什么「先加模数 / 容量再取余」**：因为 \( w - r \) 或 \( r - w \) 在「已经绕过一圈」的情况下可能是负数（例如 \( r = 5, w = 1 \)，此时实际占用是绕回来的）。先加上 \( m \)（或 \( c \)）再取余，可以把差值整体抬到非负区间，避免无符号整数下溢。最终取余 \( m \) 把结果归一到 \( 0..c \)。

**空**（`is_empty`）：\( r = w \)。
此时 \( \text{occupied} = (m + 0) \bmod m = 0 \) ✓。

**满**（`is_full`）：\( \text{vacant} = 0 \)。
即 \( (c + r - w) \bmod m = 0 \iff (w - r) \bmod m = c \) ✓，正是 u2-l1 讲过的满判据。

**并发下的瞬时性**：`occupied_len` 内部要分别调用 `write_index()` 和 `read_index()`——这是**两次独立的原子读取**。在两次读取之间，另一个线程可能恰好修改了它那一端的索引。所以返回值是「两个时刻拼出来的近似值」，可能比真实值偏大或偏小。`is_empty`/`is_full` 同理。

#### 4.2.3 源码精读

**`modulus` 辅助函数**——返回 \( 2c \)，即取模运算的模数：

[src/traits/utils.rs:20-22](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/utils.rs#L20-L22) 用 `new_unchecked` 直接构造 `NonZeroUsize`，因为 `2 * capacity` 必非零：

```rust
pub fn modulus<O: Observer + ?Sized>(this: &O) -> NonZeroUsize {
    unsafe { NonZeroUsize::new_unchecked(2 * this.capacity().get()) }
}
```

**`occupied_len` 与 `vacant_len` 的默认实现**：

[src/traits/observer.rs:46-60](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/observer.rs#L46-L60)：

```rust
fn occupied_len(&self) -> usize {
    let modulus = modulus(self);
    (modulus.get() + self.write_index() - self.read_index()) % modulus
}
fn vacant_len(&self) -> usize {
    let modulus = modulus(self);
    (self.capacity().get() + self.read_index() - self.write_index()) % modulus
}
```

- `occupied_len`：先 `modulus.get() + write - read` 再 `% modulus`（即 `% 2c`）。对照公式 \( (m + w - r) \bmod m \)。
- `vacant_len`：先 `capacity + read - write` 再 `% modulus`。注意它加的是 `capacity`（\( c \)）而不是 `modulus`，因为空闲数最多就是 \( c \)，加 \( c \) 已足够防下溢；最后对 \( m \) 取余。对照公式 \( (c + r - w) \bmod m \)。

> 小知识：`% modulus` 的右操作数是 `NonZeroUsize`。Rust 为 `usize` 实现了 `Rem<NonZeroUsize>`，编译器由此知道这次取余**不会 panic**（除数非零），可省去运行期检查。

**并发警告写在文档注释里**——这是本节最该记住的点：

[src/traits/observer.rs:46-60](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/observer.rs#L46-L60) 中 `occupied_len` 注释写：

> Actual number may be greater or less than returned value due to concurring activity of producer or consumer respectively.

`vacant_len` 注释写：

> Actual number may be greater or less than returned value due to concurring activity of consumer or producer respectively.

`is_empty`（[src/traits/observer.rs:65-68](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/observer.rs#L65-L68)）注释写：

> The result may become irrelevant at any time because of concurring producer activity.

`is_full`（[src/traits/observer.rs:73-76](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/observer.rs#L73-L76)）注释写：

> The result may become irrelevant at any time because of concurring consumer activity.

**`is_empty` / `is_full` 的实现**：

[src/traits/observer.rs:65-76](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/observer.rs#L65-L76)：

```rust
fn is_empty(&self) -> bool {
    self.read_index() == self.write_index()
}
fn is_full(&self) -> bool {
    self.vacant_len() == 0
}
```

`is_empty` 直接比较两个索引——但这同样是两次原子读，所以结果可能「瞬间过期」。

#### 4.2.4 代码实践

**实践目标**：手动用公式算出 `occupied_len`/`vacant_len`/`is_empty`/`is_full`，再和程序实际打印的值对照，验证一致。

**操作步骤**：

1. 取 capacity \( c = 4 \)，则模数 \( m = 8 \)。
2. 对下表每一行，**先用本节公式手算**，再写程序验证（**示例代码**）：

   ```rust
   use ringbuf::{traits::*, HeapRb};

   fn dump<T: Observer>(label: &str, o: &T) {
       println!("{label}: occupied={} vacant={} empty={} full={}",
           o.occupied_len(), o.vacant_len(), o.is_empty(), o.is_full());
   }

   fn main() {
       let rb = HeapRb::<i32>::new(4);
       let (mut prod, mut cons) = rb.split();
       dump("初始", &cons);                      // r=0 w=0
       prod.try_push(1).unwrap(); prod.try_push(2).unwrap();
       prod.try_push(3).unwrap(); prod.try_push(4).unwrap();
       dump("push 4 个(满)", &prod);             // r=0 w=4
       assert_eq!(prod.try_push(5), Err(5));      // 满，拒绝
       cons.try_pop(); cons.try_pop();
       dump("pop 2 个", &cons);                  // r=2 w=4
       prod.try_push(5).unwrap(); prod.try_push(6).unwrap();
       dump("再 push 2 个(绕回填满)", &prod);     // r=2 w=6
   }
   ```

3. 运行 `cargo run --example <示例名>`。

**需要观察的现象 / 预期结果（与手算对照）**：

| 时刻 | r | w | occupied=(8+w-r)%8 | vacant=(4+r-w)%8 | empty | full |
| --- | --- | --- | --- | --- | --- | --- |
| 初始 | 0 | 0 | 0 | 4 | true | false |
| push 4 个 | 0 | 4 | 4 | 0 | false | true |
| pop 2 个 | 2 | 4 | 2 | 2 | false | false |
| 再 push 2 个 | 2 | 6 | (8+6-2)%8=4 | (4+2-6)%8=0 | false | true |

> 待本地验证：实际输出应与上表一致。最后一行 `write_index=6` 已超过 `capacity=4`，说明索引确实在 `0..2*capacity` 区间内持续增长，物理槽位由 `index % capacity` 决定。

注意 `push 4 个(满)` 那行：`try_push(5)` 返回 `Err(5)`，印证 `is_full()` 为真时写入被拒绝。

#### 4.2.5 小练习与答案

**练习 1**：capacity \( c = 3 \)，当前 `r = 1, w = 5`，手算 `occupied_len` 和 `vacant_len`。

**答案**：模数 \( m = 6 \)。`occupied = (6 + 5 - 1) % 6 = 10 % 6 = 4`？—— 等等，占用不能超过 capacity。这里 `r=1, w=5`，`(w - r) % 6 = 4 > 3`，是**非法索引组合**（ringbuf 不允许存超过 capacity 个元素）。合法情况下占用最多 3。这说明索引组合有约束，并非任意值都合法。若改成合法的 `r=1, w=4`：`occupied = (6+4-1)%6 = 9%6 = 3`，`vacant = (3+1-4)%6 = 0`，满。

**练习 2**：为什么 `is_empty()` 在多线程下「可能不准确」，但在 `LocalRb` 单线程下完全可靠？

**答案**：`is_empty` 比较 `read_index()` 和 `write_index()` 两次读取。`SharedRb` 是两次原子读，两次之间另一线程可改写索引，故结果可能瞬间过期；`LocalRb` 单线程下没有并发修改者，`Cell` 两次读到的值一致，结果完全可靠。

**练习 3**：有人写 `if cons.occupied_len() > 0 { cons.try_pop() }`，在多线程下安全吗？问题出在哪？

**答案**：调用本身安全（不会 UB），但逻辑可能出问题：`occupied_len()` 返回 `> 0` 后、`try_pop()` 前，另一个 consumer（如果违反 SPSC 同时存在）可能已经把元素取走，导致 `try_pop()` 返回 `None`。正确做法是**直接 `try_pop()` 并处理 `None`**，而不是先查再读。

### 4.3 Based 与 DelegateObserver：观测能力如何传播到包装器

#### 4.3.1 概念说明

当你写 `let (prod, cons) = rb.split();` 后，拿到的 `prod`/`cons` 并不是 `SharedRb` 本身，而是包装器（`HeapProd` = `CachingProd<Arc<HeapRb<T>>>`，详见 u2-l3 / u2-l4）。但你照样能在 `prod` 上调用 `capacity()`、`occupied_len()`、`is_empty()`……这些 `Observer` 方法从哪来的？`CachingProd` 难道把 `Observer` 的所有方法重新实现了一遍？

并没有。ringbuf 用一套**委托（delegation）机制**，让任何「内部包着一个 `Observer`」的包装器，**自动获得**全部 `Observer` 方法，而方法体只是把调用转发给内部对象。这套机制的三个零件是：

1. **`Based` trait**：声明「我是一个包装器，我内部有一个 `Base`，可以通过 `base()` / `base_mut()` 拿到它」。
2. **`DelegateObserver`**：一个**空**的标记 trait，约束 `Self: Based` 且 `Self::Base: Observer`。
3. **blanket impl**：为所有 `D: DelegateObserver` 自动实现 `Observer`，方法体一律 `self.base().xxx()`。

于是包装器只需：实现 `Based`（指向内部）+ 写一行 `impl DelegateObserver for MyWrapper {}`，就白嫖了全部 `Observer` 方法。本模块只讲清这条链路里和 `Observer` 直接相关的部分；更完整的委托机制（含 `DelegateProducer`/`DelegateConsumer` 等）留待 u3-l5 展开。

#### 4.3.2 核心流程

委托链路（以 `prod.capacity()` 为例）：

```
prod.capacity()                          // 调用者
  → CachingProd::capacity()              // 来自 blanket impl（因为 CachingProd: DelegateObserver）
    → self.base().capacity()             // 转发给内部 CachingProd 持有的 RbRef
      → ... 最终落到 SharedRb::capacity() // 真正的实现
```

要点：

1. 包装器（`Prod`/`Cons`/`Obs`/`CachingProd`/`CachingCons`/`FrozenProd`/`FrozenCons`）实现 `Based`，其 `Base` 指向「真正干活」的对象。
2. 包装器再实现空的 `DelegateObserver`。
3. 编译器套用 blanket impl，给包装器生成转发版的 `Observer` 实现。
4. 对 `Observer` 的派生方法（`occupied_len` 等），由于它们是 trait 默认实现，包装器连「转发体」都不用写——它们会调用本类型上的 `write_index()`/`read_index()`/`capacity()`，而这些又被 blanket impl 转发到 `base`。于是派生方法也自然继承。

#### 4.3.3 源码精读

**`Based` trait**——包装器的「基座」声明：

[src/traits/utils.rs:4-14](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/utils.rs#L4-L14)：

```rust
pub trait Based {
    type Base: ?Sized;
    fn base(&self) -> &Self::Base;
    fn base_mut(&mut self) -> &mut Self::Base;
}
```

文档明确「should be implemented by ring buffer wrappers / Used for automatically delegating methods」。`type Base: ?Sized` 允许 `Base` 是未定大小类型（如 `dyn Observer` 或 `SharedRb<S>` 在 `S: ?Sized` 时）。

**`DelegateObserver`**——空的标记 trait：

[src/traits/observer.rs:79-84](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/observer.rs#L79-L84)：

```rust
pub trait DelegateObserver: Based
where
    Self::Base: Observer,
{
}
```

trait 体里**什么都没有**——它只是一个「我具备委托资格」的标记，要求 `Self: Based` 且其 `Base` 是 `Observer`。

**blanket impl**——自动生成转发实现：

[src/traits/observer.rs:86-143](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/observer.rs#L86-L143) 为 `impl<D: DelegateObserver> Observer for D where D::Base: Observer`。每个方法都只是 `self.base().同名方法()`。例如：

[src/traits/observer.rs:92-104](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/observer.rs#L92-L104)：

```rust
#[inline]
fn capacity(&self) -> NonZeroUsize {
    self.base().capacity()
}
#[inline]
fn read_index(&self) -> usize {
    self.base().read_index()
}
```

注意 `#[inline]`，让转发在编译期被消掉，做到「零成本抽象」——包装器调用 `capacity()` 与直接在底层 `SharedRb` 上调用，运行期开销一致。

**`Based` 的导出位置**：

[src/traits/mod.rs:17](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/mod.rs#L17) 通过 `pub use utils::Based;` 把 `Based` 从 `traits` 顶层导出，所以你可以写 `use ringbuf::traits::Based;`。

#### 4.3.4 代码实践

**实践目标**：通过「源码阅读型实践」，确认 `Based` + `DelegateObserver` + blanket impl 这条委托链路在真实代码中成立。

**操作步骤**：

1. 打开 `src/traits/observer.rs`，确认 `DelegateObserver`（[L80-84](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/observer.rs#L80-L84)）是空 trait，以及 blanket impl（[L86-143](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/observer.rs#L86-L143)）把每个方法转发到 `self.base()`。
2. 打开 `src/wrap/` 目录（如 `src/wrap/caching.rs`、`src/wrap/direct.rs`），找到包装器实现 `Based` 的位置——它们的 `type Base` 指向内部持有的 `RbRef`（即指向 `SharedRb` 的智能指针，如 `Arc<SharedRb<S>>` 或 `&SharedRb<S>`），并实现空的 `DelegateObserver`。
3. 写一个最小验证程序（**示例代码**），在 split 得到的 `prod` 上调用 `Observer` 方法，确认它能编译通过：

   ```rust
   use ringbuf::{traits::*, HeapRb};

   fn describe<O: Observer>(label: &str, o: &O) {
       println!("{label}: capacity={} occupied={} vacant={}",
           o.capacity(), o.occupied_len(), o.vacant_len());
   }

   fn main() {
       let rb = HeapRb::<i32>::new(3);
       let (mut prod, mut cons) = rb.split();
       prod.try_push(7).unwrap();
       // prod 是 CachingProd<Arc<HeapRb<i32>>>，不是 HeapRb 本身，
       // 但它能调用 capacity()/occupied_len() —— 因为 DelegateObserver 的委托。
       describe("prod", &prod);
       describe("cons", &cons);
   }
   ```

4. 运行 `cargo run --example <示例名>`。

**需要观察的现象**：程序能编译通过——这本身就证明了委托链路成立：`prod`（类型 `CachingProd<...>`）本身没有手写 `capacity`/`occupied_len`，却拥有这些方法，唯一可能就是来自 blanket impl 的转发。

**预期结果**：`prod` 和 `cons` 都打印 `capacity=3 occupied=1 vacant=2`（已 push 一个），二者一致。

#### 4.3.5 小练习与答案

**练习 1**：`DelegateObserver` 的 trait 体是空的，那它存在的意义是什么？

**答案**：它是一个标记 trait，作用是「声明委托资格 + 给 blanket impl 提供约束边界」。blanket impl 写成 `impl<D: DelegateObserver> Observer for D`，靠这个空 trait 限定「哪些类型会被自动实现 `Observer`」，避免为所有 `Based` 类型无差别实现而引发冲突。

**练习 2**：为什么 blanket impl 的每个方法都加了 `#[inline]`？

**答案**：包装器方法只是把调用转发给 `self.base()`，没有实质逻辑。`#[inline]` 提示编译器把这一层转发消除，使「在包装器上调用」与「在底层对象上直接调用」运行期开销一致，实现零成本抽象。

**练习 3**：一个包装器只实现了 `DelegateObserver`（以及 `Based`），它会自动拥有 `occupied_len()` 吗？为什么？

**答案**：会。blanket impl 为它实现了完整的 `Observer`，包括 `read_index()`/`write_index()`/`capacity()` 的转发；而 `occupied_len()` 是 `Observer` 的默认实现，它会调用本类型上的这三个方法——它们又被转发到 `base`。所以派生方法也被一并继承，无需包装器额外写代码。

## 5. 综合实践

把本讲三个模块串起来，完成一个「带断言的观测器」小任务。

**任务**：写一个函数 `assert_invariants<O: Observer>(o: &O)`，对任意 `Observer` 校验以下不变量恒成立，并用 `HeapRb` 在一系列 push/pop 操作后调用它。

**要求校验的不变量**（用本讲学到的关系推导）：

1. `occupied_len() + vacant_len() == capacity()`（占用 + 空闲 = 容量）。
2. `is_empty() == (occupied_len() == 0)`（空 ⟺ 占用为 0）。
3. `is_full() == (vacant_len() == 0)`（满 ⟺ 空闲为 0）。

**参考实现（示例代码）**：

```rust
use ringbuf::{traits::*, HeapRb};

fn assert_invariants<O: Observer>(label: &str, o: &O) {
    let cap = o.capacity().get();
    let occ = o.occupied_len();
    let vac = o.vacant_len();
    assert_eq!(occ + vac, cap, "{label}: occupied+vacant != capacity");
    assert_eq!(o.is_empty(), occ == 0, "{label}: is_empty mismatch");
    assert_eq!(o.is_full(), vac == 0, "{label}: is_full mismatch");
    println!("{label}: cap={cap} occ={occ} vac={vac} empty={} full={}",
        o.is_empty(), o.is_full());
}

fn main() {
    let rb = HeapRb::<i32>::new(5);
    let (mut prod, mut cons) = rb.split();

    assert_invariants("初始", &cons);
    for i in 0..5 { prod.try_push(i).unwrap(); }
    assert_invariants("满", &prod);                 // full, occupied=5
    assert!(prod.try_push(99).is_err());            // 满则拒绝
    while cons.try_pop().is_some() {}
    assert_invariants("空", &cons);                 // empty, occupied=0
}
```

**操作步骤**：

1. 把上面的代码放进一个 example，运行 `cargo run --example <示例名>`。
2. 在 push/pop 之间多次插入 `assert_invariants(...)` 调用。

**预期结果**：所有断言通过，证明 `Observer` 的派生方法彼此自洽、与底层算术一致。

**进阶思考**：如果把 `HeapRb` 换成跨线程的 `SharedRb` 并在两个线程里并发 push/pop，这些断言还一定成立吗？结合 4.2 节「瞬时性」思考：在多线程下，`occupied_len()` 和 `vacant_len()` 是两次独立原子读拼出来的近似值，它们的和**可能瞬时**不等于 capacity（例如一端在两次读之间动了）。所以上述不变量只在**单线程**或**观测期间无并发修改**时严格成立——这正是源码文档反复警告的。在多线程下，不要用 `Observer` 的查询做正确性决策，而应直接 `try_push`/`try_pop` 并处理返回值。

## 6. 本讲小结

- `Observer` 是 ringbuf trait 体系里最基础的「只读观测」trait：提供容量、读写索引、`unsafe_slices`、占用/空闲数、空/满判断、hold 标志查询；它的安全方法只暴露「状态」、不暴露「数据内容」。
- `capacity`、两个 `*_index`、两个 `unsafe_slices*`、两个 `*_is_held` 是必须实现的底层方法；`occupied_len`/`vacant_len`/`is_empty`/`is_full` 是 trait 提供默认实现的派生方法。
- `occupied_len = (2c + w - r) % 2c`，`vacant_len = (c + r - w) % 2c`，「先加模数/容量再取余」是为了防止无符号下溢；空 ⟺ `r == w`，满 ⟺ `vacant == 0`。
- 在 `SharedRb` 多线程下，这些值只是**瞬时快照**：它们来自两次独立原子读，两次读之间另一端可改写索引，故结果可能瞬间过期——做决策时应直接 `try_push`/`try_pop`，不要「先查后做」。
- `Based` trait + 空的 `DelegateObserver` 标记 trait + blanket impl，构成委托机制：任何「内部包着 `Observer`」的包装器（`Prod`/`Cons`/`Obs`/`CachingProd`/`CachingCons` 等）只需实现 `Based` 并声明 `DelegateObserver`，就自动获得全部 `Observer` 方法，且转发被 `#[inline]` 消除，是零成本抽象。
- `SharedRb`（原子 `Acquire` 读）与 `LocalRb`（`Cell` 读）实现同一套 `Observer` 算术，差别只在索引的存储介质与是否跨核同步。

## 7. 下一步学习建议

- **下一步学 u3-l2（Producer trait）**：`Observer` 是地基，`Producer` 在它之上加了写入能力。注意 `Producer` 的 `vacant_slices`/`vacant_slices_mut` 正是建立在 `Observer::unsafe_slices_mut` 之上的安全封装，本讲讲的 `unsafe_slices` 是它们的前置。
- **同时学 u3-l3（Consumer trait）**：`Consumer` 的 `as_slices`/`occupied_slices` 同样基于 `Observer::unsafe_slices`。
- **稍后学 u3-l5（委托机制）**：本讲只讲了 `DelegateObserver`。`DelegateProducer`/`DelegateConsumer`/`DelegateRingBuffer` 的完整委托体系、以及它们如何让 `BlockingProd`/`AsyncProd` 等复用核心逻辑，在 u3-l5 系统展开。
- **并发安全深入看 u5-l1 / u5-l2**：本讲提到 `Acquire` 读、hold 标志、并发快照的瞬时性，其背后的内存顺序与 SPSC 不变量细节在第五单元专家层深入。
- **建议阅读的源码**：重读 `src/traits/observer.rs` 全文（很短），再扫一眼 `src/wrap/caching.rs` 中 `CachingProd` 实现 `Based` + `DelegateObserver` 的位置，亲眼看一次委托链路的两端。
