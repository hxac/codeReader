# 核心原理：双索引与 2\*capacity 模运算

## 1. 本讲目标

本讲是理解 ringbuf 一切后续行为的基石。读完本讲，你应当能够：

- 说清 `read` 与 `write` 两个索引分别指向什么，以及它们为何要「落在 `0..2*capacity`」而非 `0..capacity`。
- 用模运算严格区分「缓冲区为空」与「缓冲区已满」，并解释为什么这种做法不需要牺牲任何一个存储槽。
- 手动推导一段 push/pop 序列后 `read`/`write` 的取值，并画出哪些槽被占用。
- 看懂 `ranges()` 函数如何把一段「可能绕了一圈」的环形索引区间，切成最多两段连续的线性切片。

本讲只关心**索引算术**这一件事，不涉及原子操作、多线程、存储后端等主题（它们在后续讲义展开）。

## 2. 前置知识

在进入本讲前，请确认你已经掌握（来自 u1-l2）：

- **环形缓冲区（ring buffer）**：用一块固定大小的连续内存反复复用，靠两个游标推进，避免数据搬运。
- **read / write 游标**：一个指向「最旧的可读元素」，一个指向「下一个可写的空槽」。
- **SPSC**：至多一个生产者写、至多一个消费者读。
- **FIFO**：先进先出，先 `push` 进去的元素最先被 `pop` 出来。
- `HeapRb::new(capacity)` 创建缓冲区，`split()` 拆成 `Producer` / `Consumer`，`try_push` / `try_pop` 读写。

本讲需要一点**模运算（取余）**的直觉：

- \( a \bmod m \) 表示 \( a \) 除以 \( m \) 的非负余数，结果落在 \( 0..m \)。
- 模运算让一个不断增长的计数器「绕圈」回到固定区间，这正是环形缓冲区的数学本质。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/lib.rs` | 顶层文档的「Implementation details / Indices」一节，用文字讲清了双索引的全部语义。 |
| `src/rb/utils.rs` | 提供 `ranges()` 函数：把一段环形索引区间映射成最多两段线性切片。本讲的核心算法点。 |
| `src/traits/observer.rs` | 定义 `Observer` trait，给出 `read_index` / `write_index` / `occupied_len` / `vacant_len` / `is_empty` / `is_full` 的语义与默认实现。 |
| `src/traits/utils.rs` | 提供 `modulus()` 辅助函数，返回模数 \( 2 \times \text{capacity} \)。 |
| `src/rb/shared.rs` / `src/rb/local.rs` | 两种 RB 实现分别在 `unsafe_slices` 中调用 `ranges()`，是把算术落地的调用点。 |
| `src/traits/producer.rs` | `advance_write_index` 用 `% modulus` 推进 write，是「索引在 `0..2c` 内绕圈」的出处。 |

## 4. 核心概念与源码讲解

本讲拆成两个最小模块：

- **4.1 双索引与 2\*capacity 模数**：`read` / `write` 的语义，以及为什么模数是 \(2c\) 而不是 \(c\)。
- **4.2 `ranges()`**：把环形索引区间映射成两段线性切片。

### 4.1 双索引与 2\*capacity 模数：read/write 的语义与空满判断

#### 4.1.1 概念说明

ringbuf 用两个整数 `read` 和 `write` 来描述缓冲区状态。它们的关键性质是：

- 物理槽位 = `index % capacity`。也就是说，真正用来索引存储数组的，是索引对 `capacity` 取余后的值。
- `read % capacity` 指向**最旧的一个已存元素**所在的槽。
- `write % capacity` 指向**下一个空槽**（即最近一次写入元素的「下一个」位置）。

读一个元素：从 `read % capacity` 槽取出，然后 `read` 加 1；写一个元素：写入 `write % capacity` 槽，然后 `write` 加 1。

于是「哪些槽已存元素」就是模运算意义下从 `read % capacity`（含）到 `write % capacity`（不含）的那一段；其余槽为空。

这里立刻出现一个经典难题：**当缓冲区「满」时，`write` 绕一圈追上了 `read`，于是 `read % capacity == write % capacity`；而缓冲区「空」时同样有 `read % capacity == write % capacity`。仅凭对 `capacity` 取余的结果，无法区分空和满。**

教材里常见的两种解法：

1. **牺牲一个槽**：永远保留一个空槽不用，于是「满」可表达为 `(write + 1) % capacity == read`。代价是少存一个元素。
2. **额外维护一个计数或标志位**：用单独的整数记录元素个数。代价是多一个字段、多一次同步。

ringbuf 选了第三条路：**让索引本身多带一位「圈数」信息**——把模数从 \(c\) 改成 \(2c\)，让 `read`、`write` 取值落在 \(0..2c\)。这样：

- 空：\( \text{read} == \text{write} \)。
- 满：\( (\text{write} - \text{read}) \bmod (2c) == c \)，即 write 比 read「领先正好一圈」。

既不浪费槽位，也不需要额外字段。

#### 4.1.2 核心流程

设容量为 \(c\)，模数 \(m = 2c\)。两个索引恒有 \( \text{read}, \text{write} \in [0,\, 2c) \)。

> 推进索引时一律对 \(m\) 取余，保证索引永远不越界：

\[
\text{write}_{\text{新}} = (\text{write} + \Delta w) \bmod m,\qquad
\text{read}_{\text{新}} = (\text{read} + \Delta r) \bmod m
\]

占用元素数与空闲槽数都由两个索引推出（代码里用 `+ m` / `+ c` 先加再取余，是为了在 `write < read` 时避免下溢）：

\[
\text{occupied} = (m + \text{write} - \text{read}) \bmod m
\]

\[
\text{vacant} = (c + \text{read} - \text{write}) \bmod m
\]

于是两种临界状态判别：

\[
\text{空} \iff \text{read} == \text{write} \iff \text{occupied} == 0
\]

\[
\text{满} \iff \text{vacant} == 0 \iff (\text{write} - \text{read}) \bmod m == c
\]

整段流程可概括为伪代码：

```text
push(elem):
    if 满写失败: return Err(elem)
    slot = write % capacity          # 物理槽位
    storage[slot].write(elem)
    write = (write + 1) % (2*capacity)   # 在 0..2c 内绕圈

pop():
    if 空: return None
    slot = read % capacity
    elem = storage[slot].read()
    read = (read + 1) % (2*capacity)
    return Some(elem)
```

一个常被忽略的不变量：因为最多存 \(c\) 个元素，所以 \((\text{write} - \text{read}) \bmod m\) 永远不允许超过 \(c\)。换句话说，合法的索引组合只占 \(0..2c\) 空间的一半，剩下的一半（差距在 \(c < d < 2c\)）是「非法状态」，`from_raw_parts` 等接口要求调用者保证不产生它。

#### 4.1.3 源码精读

**双索引语义的权威说明**——顶层文档直接写明了 `read % capacity` 与 `write % capacity` 的含义，以及为何取模 \(2c\)：

> `read % capacity` 指向最旧元素；`write % capacity` 指向最近写入元素之后的空槽；索引对 \(2 \times \text{capacity}\) 取模，从而无须额外空槽即可区分空（`read == write`）与满（`(write - read) % (2*capacity) == capacity`）。

参见 [src/lib.rs:121-142](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/lib.rs#L121-L142)（「Indices」实现细节，明确写出模数是 `2 * capacity`）。

**`Observer` trait 暴露索引**——两个索引的取值范围被文档约定为 `0..(2 * capacity)`：

```rust
/// Index of the last item in the ring buffer.
/// Index value is in range `0..(2 * capacity)`.
fn read_index(&self) -> usize;
/// Index of the next empty slot in the ring buffer.
/// Index value is in range `0..(2 * capacity)`.
fn write_index(&self) -> usize;
```

参见 [src/traits/observer.rs:15-22](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/observer.rs#L15-L22)（注释里再次强调索引落在 `0..2*capacity`）。

**`modulus()` 辅助函数**——把 `2 * capacity` 封装成模数，供各处取余复用：

```rust
/// Modulus for pointers to item in ring buffer storage.
/// Equals to `2 * capacity`.
pub fn modulus<O: Observer + ?Sized>(this: &O) -> NonZeroUsize {
    unsafe { NonZeroUsize::new_unchecked(2 * this.capacity().get()) }
}
```

参见 [src/traits/utils.rs:16-22](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/utils.rs#L16-L22)。

**推进 write 时对模数取余**——`advance_write_index` 把 `(write + count) % modulus` 写回，这正是索引「在 `0..2c` 内绕圈」的代码出处：

```rust
unsafe fn advance_write_index(&self, count: usize) {
    unsafe { self.set_write_index((self.write_index() + count) % modulus(self)) };
}
```

参见 [src/traits/producer.rs:33-35](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/producer.rs#L33-L35)。

**空满判断的默认实现**——`is_empty` 直接比较两个索引，`occupied_len` / `vacant_len` 用「先加模数再取余」避免下溢：

```rust
fn occupied_len(&self) -> usize {
    let modulus = modulus(self);
    (modulus.get() + self.write_index() - self.read_index()) % modulus
}
fn vacant_len(&self) -> usize {
    let modulus = modulus(self);
    (self.capacity().get() + self.read_index() - self.write_index()) % modulus
}
fn is_empty(&self) -> bool { self.read_index() == self.write_index() }
fn is_full(&self) -> bool  { self.vacant_len() == 0 }
```

参见 [src/traits/observer.rs:46-76](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/observer.rs#L46-L76)。注意 `occupied_len` / `vacant_len` 的文档都标注了「并发下数值可能瞬时失效」，因为另一端可能在你看完值的瞬间又改了索引——本讲只看静态正确性，并发留到第五单元。

**索引的两种物理存储**——多线程版用 `CachePadded<AtomicUsize>`：

```rust
pub struct SharedRb<S: Storage + ?Sized> {
    read_index: CachePadded<AtomicUsize>,
    write_index: CachePadded<AtomicUsize>,
    read_held: AtomicBool,
    write_held: AtomicBool,
    storage: S,
}
```

参见 [src/rb/shared.rs:51-57](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs#L51-L57)；单线程版用 `Cell<usize>`（无原子、无缓存同步），见 [src/rb/local.rs:79-86](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/local.rs#L79-L86)。两者算术完全相同，只是存储介质不同。

#### 4.1.4 代码实践

**实践目标**：用一个可运行程序，把「push/pop 后 `read`/`write` 真实取值」打印出来，验证你脑海中的算术与库的行为一致。

为了能直接读到「未经缓存」的真实索引，这里刻意用**单线程的 `LocalRb`**（它 `split` 出的 `Prod`/`Cons` 是「即时同步」的直接包装，索引不缓存）。

**操作步骤**（以下为示例代码，非项目原有文件，请存为 `examples/indices_demo.rs` 后运行）：

```rust
use ringbuf::{storage::Heap, traits::*, LocalRb};

fn show(label: &str, o: &impl Observer<Item = i32>) {
    println!(
        "{:<14} read={}, write={}, occupied={}, vacant={}, empty={}, full={}",
        label,
        o.read_index(),
        o.write_index(),
        o.occupied_len(),
        o.vacant_len(),
        o.is_empty(),
        o.is_full(),
    );
}

fn main() {
    // capacity = 4, 模数 = 2*4 = 8
    let rb = LocalRb::<Heap<i32>>::new(4);
    let (mut prod, mut cons) = rb.split();

    show("初始(空)", &prod);

    for v in 0..4 {
        prod.try_push(v).unwrap(); // push 0,1,2,3
    }
    show("push 4 个(满)", &prod);

    for _ in 0..2 {
        cons.try_pop(); // 丢弃 0,1
    }
    show("pop 2 个", &cons);

    for v in 4..6 {
        prod.try_push(v).unwrap(); // push 4,5
    }
    show("再 push 2 个(满)", &prod);
}
```

运行：`cargo run --example indices_demo --features alloc`（核心 crate 默认开启 `std`/`alloc`，通常直接 `cargo run --example indices_demo` 即可）。

**需要观察的现象与预期结果**：

| 时刻 | read | write | occupied | full |
| --- | --- | --- | --- | --- |
| 初始（空） | 0 | 0 | 0 | 否（empty） |
| push 4 个 | 0 | 4 | 4 | 是 |
| pop 2 个 | 2 | 4 | 2 | 否 |
| 再 push 2 个 | 2 | 6 | 4 | 是 |

注意两个「满」状态：第一次 `read%4 == write%4 == 0`，第二次 `read%4 == write%4 == 2`，但程序都正确判为「满」而非「空」——这正是 `2c` 模数的功劳。若实现上把模数改成 `c`，这两行就会和「空」混淆。

> 若你的环境无法运行（例如缺 `alloc`），可改为 `StaticRb::<i32, 4>::default()` + `split_ref()`，结论一致——「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：容量 \(c=3\) 时模数是多少？写出「空」与「满」的判别式。

**答案**：模数 \(m = 2c = 6\)。空 \(\iff \text{read} == \text{write}\)；满 \(\iff (\text{write} - \text{read}) \bmod 6 == 3\)。

**练习 2**：为什么 `occupied_len` 里要先加 `modulus` 再取余，而不是直接 `(write - read) % modulus`？

**答案**：因为「绕圈」后可能出现 `write < read`（例如 `read=6, write=0` 时 occupied 应为 2）。直接相减会得到一个巨大的无符号下溢值；先 `+ m` 把差值平移到非负区间再取余，结果才正确。

**练习 3**：若不慎让 `(write - read) % (2c) == c + 1`，缓冲区处于什么非法状态？为什么它非法？

**答案**：这表示「想存 \(c+1\) 个元素」，超过容量；同时它会让空满判定与切片映射失真。\(0..2c\) 中只有差距 \(\le c\) 的组合才是合法索引状态。

---

### 4.2 ranges()：把环形索引区间映射到两段线性切片

#### 4.2.1 概念说明

存储区是一段**线性**内存（下标 `0..capacity`），但逻辑上的「已存元素区间」`[read, write)` 在环形意义下可能「绕过数组末尾」。例如容量 4、`read=2, write=6` 时，已存元素物理上分布在槽 2、3、0、1——中间断开了一次。

为了能用普通的 Rust 切片（`&[T]` / `&mut [T]`）直接访问这些元素，ringbuf 用 `ranges()` 把一段逻辑区间 `[start, end)` 拆成**最多两段连续的线性下标区间**：

- 不绕尾：一段 `[head_rem, tail_rem)`，第二段为空。
- 绕尾：两段 `[head_rem, capacity)` 与 `[0, tail_rem)`。

其中 `head_rem = start % capacity`、`tail_rem = end % capacity` 是落到物理槽的下标。

这个函数是 `unsafe_slices` / `unsafe_slices_mut` 的核心，也是批量读写（`push_slice`、`pop_slice`、`as_slices` 等）能一次拿到连续内存的根基。

#### 4.2.2 核心流程

输入：`capacity`、`start`、`end`，满足约束 \(0 \le (\text{start} - \text{end}) \bmod (2c) \le c\)（即 end 在 start 之后、距离不超过一圈）。

算法用「圈数奇偶」判断是否绕尾：

```text
head_quo, head_rem = divmod(start, capacity)   # start 落在第几圈 / 槽号
tail_quo, tail_rem = divmod(end,   capacity)

if (head_quo + tail_quo) 是偶数:
    # 同一圈（都不绕 / 都已绕一圈且对齐）：一段
    返回 (head_rem .. tail_rem,  0..0)
else:
    # 跨过数组末尾：两段
    返回 (head_rem .. capacity,  0 .. tail_rem)
```

直觉：索引除以 `capacity` 的商 `quo`（取值 0 或 1，因为索引 \(< 2c\)）记录了它「在第几圈」。\((\text{head\_quo} + \text{tail\_quo}) \bmod 2\) 为 0 意味着 start 与 end 处在「同一圈相位」，区间不绕尾；为 1 意味着跨圈，必然绕过数组末尾，要拆两段。

文档还保证一条性质：**若第一段为空，则第二段也为空**（即区间长度为 0，整个返回两段都空）。

#### 4.2.3 源码精读

**`ranges()` 的完整实现**——整段只有 10 行，是本讲最值得逐字读懂的代码：

```rust
pub fn ranges(capacity: NonZeroUsize, start: usize, end: usize) -> (Range<usize>, Range<usize>) {
    let (head_quo, head_rem) = (start / capacity, start % capacity);
    let (tail_quo, tail_rem) = (end / capacity, end % capacity);

    if (head_quo + tail_quo) % 2 == 0 {
        (head_rem..tail_rem, 0..0)
    } else {
        (head_rem..capacity.get(), 0..tail_rem)
    }
}
```

参见 [src/rb/utils.rs:9-18](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/utils.rs#L9-L18)。函数注释明确写了输入约束与「第一段空则第二段也空」的承诺（见 [src/rb/utils.rs:3-8](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/utils.rs#L3-L8)）。

**`unsafe_slices` 如何调用 `ranges()`**——把 `(start, end)` 映射成两段物理切片，再交给存储后端取实际内存：

```rust
unsafe fn unsafe_slices(&self, start: usize, end: usize) -> (&[MaybeUninit<S::Item>], &[MaybeUninit<S::Item>]) {
    let (first, second) = ranges(self.capacity(), start, end);
    unsafe { (self.storage.slice(first), self.storage.slice(second)) }
}
```

参见 [src/rb/shared.rs:104-111](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs#L104-L111)（`SharedRb` 的实现）；`LocalRb` 的对应实现一字不差，见 [src/rb/local.rs:88-95](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/local.rs#L88-L95)。

**「已存区间」与「空闲区间」的端点如何取**——这是理解 `ranges()` 实参来源的关键：

- 已存元素区间：`unsafe_slices(read_index, write_index)`。
- 空闲槽区间：`unsafe_slices(write_index, read_index + capacity)`（空闲区长度恰为 `capacity - occupied`，故 end 端补一个 `capacity`）。

```rust
fn vacant_slices_mut(&mut self) -> (...) {
    unsafe { self.unsafe_slices_mut(self.write_index(), self.read_index() + self.capacity().get()) }
}
```

参见 [src/traits/producer.rs:53-55](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/producer.rs#L53-L55)（`vacant_slices_mut`，给出空闲区端点约定），同模式见 `vacant_slices` [src/traits/producer.rs:40-42](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/traits/producer.rs#L40-L42)。

#### 4.2.4 代码实践

**实践目标**：给定 `capacity=4`，手推一段操作后 `ranges()` 应返回的两段范围，再用前面 4.1.4 的程序状态对照验证。

**操作步骤**：在纸上按 FIFO 推演 `capacity=4`（模数 8）下「push 4 个 → pop 2 个 → 再 push 2 个」的全过程。我们追踪每步的 `read`、`write`、各槽内容（槽号 0..3）：

| 步骤 | read | write | 槽0 | 槽1 | 槽2 | 槽3 | 说明 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 初始 | 0 | 0 | – | – | – | – | 空 |
| push 0 | 0 | 1 | **0** | – | – | – | write%4=0 |
| push 1 | 0 | 2 | 0 | **1** | – | – | |
| push 2 | 0 | 3 | 0 | 1 | **2** | – | |
| push 3 | 0 | 4 | 0 | 1 | 2 | **3** | 满；write%4=0 |
| pop（取0） | 1 | 4 | – | 1 | 2 | 3 | read%4=0 取走槽0 |
| pop（取1） | 2 | 4 | – | – | 2 | 3 | read%4=1 取走槽1 |
| push 4 | 2 | 5 | **4** | – | 2 | 3 | write%4=0 写入槽0 |
| push 5 | 2 | 6 | 4 | **5** | 2 | 3 | 满；write%4=2 写入槽1 |

最终态：`read=2, write=6`。占用的物理槽依次为 2、3、0、1，对应 FIFO 内容 `2,3,4,5`。

**对「已存区间」调用 `ranges(4, read=2, write=6)`**：

- `head_quo = 2/4 = 0, head_rem = 2`
- `tail_quo = 6/4 = 1, tail_rem = 2`
- \((0+1)\bmod 2 = 1\)（奇，绕尾）→ 返回 `(2..4, 0..2)`

即两段切片：第一段槽 `[2,3]` = 内容 `[2,3]`；第二段槽 `[0,1]` = 内容 `[4,5]`。拼起来正好是 FIFO 顺序 `2,3,4,5`。

**对「push 4 个后」的已存区间调用 `ranges(4, 0, 4)`**（验证不绕尾的特殊满态）：

- `head_quo=0, head_rem=0`；`tail_quo=1, tail_rem=0`
- \((0+1)\bmod 2 = 1\)（奇）→ 返回 `(0..4, 0..0)`

虽然奇偶判定为「绕尾分支」，但因为 `head_rem == tail_rem == 0`，结果是一段完整的 `0..4`、第二段空——恰好覆盖全部 4 个槽，没有真正断开。这就是「满态从槽 0 开始」时不产生第二段的原因。

**需要观察的现象**：把 4.1.4 程序最后两步的 `read`/`write` 与上表对照，应完全一致；`ranges()` 在最终态返回 `(2..4, 0..2)`，正是「绕了一圈」的两段。

**预期结果**：手推值与程序输出一致；你能在纸上画出环形示意（槽 0..3 围成一圈，`read` 指向槽 2，`write` 指向槽 2 但领先一圈），并标出两段连续切片。

#### 4.2.5 小练习与答案

**练习 1**：`capacity=4`、`read=0, write=2` 时，`ranges(4, 0, 2)` 返回什么？是几段？

**答案**：`head_quo=0, head_rem=0`；`tail_quo=0, tail_rem=2`；\((0+0)\bmod 2=0\) → `(0..2, 0..0)`。一段，覆盖槽 0、1，不绕尾。

**练习 2**：为什么 `vacant_slices` 的 `end` 端是 `read_index + capacity` 而不是 `read_index`？

**答案**：空闲区在环形意义上是「从 write 到 read」的**补集**，长度为 `capacity - occupied`。把 end 写成 `read + capacity` 相当于在模 \(2c\) 意义下把 read 「往后推一圈」去对齐 write 之后，使 `[write, read+capacity)` 恰好覆盖所有空闲槽，从而能直接喂给 `ranges()`。

**练习 3**：若 `ranges()` 改成只返回一段（不拆两段），`push_slice` / `pop_slice` 会出什么问题？

**答案**：绕尾时已存/空闲元素物理上不连续，单段切片无法表达；批量拷贝会越界或漏掉绕到数组头部的那些槽。拆成两段后，调用方对两段分别处理即可覆盖全部元素。

## 5. 综合实践

把 4.1、4.2 串起来，完成一次「纸上推导 + 机器验证」的全流程：

1. **选参数**：取 `capacity = 5`（模数 10），自己设计一条至少包含「变满 → 部分弹出 → 再次变满，且第二次满时 `read != 0`」的操作序列。
2. **纸上推导**：逐步填出每一步的 `read`、`write`、各槽内容；标出最终态「已存区间」与「空闲区间」分别落在哪些槽。
3. **算 `ranges()`**：对最终态手算 `ranges(5, read, write)` 与 `ranges(5, write, read+5)` 的两段返回值。
4. **机器验证**：把 4.1.4 的示例程序改成 `LocalRb::<Heap<i32>>::new(5)` 并按你的序列改写 push/pop，打印每步 `read_index` / `write_index` / `occupied_len` / `is_full`。
5. **对照结论**：确认手推值与程序输出一致；确认两次「满」态虽 `read%5 == write%5` 却都被判为满（而非空）——这是 `2c` 模数存在的根本证据。

> 若想进一步看到 `ranges()` 的真实返回，可阅读 `as_slices`（`Consumer`）或 `unsafe_slices`（`Observer`）对应的测试，断言中往往直接比较两段切片的内容，参见 `src/tests/slice.rs`。

## 6. 本讲小结

- ringbuf 用 `read` / `write` 两个索引描述状态，**物理槽位 = 索引 % capacity**；`read%capacity` 指最旧元素，`write%capacity` 指下一个空槽。
- 模数取 \(2c\) 而非 \(c\)，是为了在「不牺牲槽位、不额外加字段」的前提下区分空（`read == write`）与满（`(write-read) % (2c) == c`）。
- 索引推进一律 `% (2*capacity)`，恒落在 \(0..2c\)；合法状态要求占用不超过 \(c\)。
- `occupied_len` / `vacant_len` 用「先加模数再取余」避免绕圈下溢；`is_empty` 直接比索引，`is_full` 看 `vacant_len == 0`。
- `ranges()` 用圈数奇偶判断是否绕尾，把环形区间 `[start, end)` 拆成最多两段线性切片，是所有批量/切片访问的根基。
- 这些算术对 `LocalRb`（`Cell`）和 `SharedRb`（原子）完全相同，差别只在存储介质与是否需要跨核同步。

## 7. 下一步学习建议

- **下一讲（u2-l2）** 将进入「存储后端」：`Storage` trait 与 `Array` / `Heap` / `Ref` 的区别，理解 `ranges()` 返回的两段切片最终是如何落到真实内存（堆、静态数组、借用切片）上的。
- 想提前体会批量访问如何复用 `ranges()`，可先读 `src/traits/producer.rs` 的 `push_slice` 与 `src/traits/consumer.rs` 的 `pop_slice` / `as_slices`。
- 对「并发下索引可能瞬时失效」感兴趣的话，可先扫一眼 `Observer` 各方法的文档注释，正式讲解在第五单元（u5-l1 无锁原子与内存顺序）。
