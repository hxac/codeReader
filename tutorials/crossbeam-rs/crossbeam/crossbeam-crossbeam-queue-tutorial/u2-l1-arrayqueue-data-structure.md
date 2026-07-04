# ArrayQueue 的数据结构：stamp、lap 与 Slot 模型

> 本讲属于「进阶层：ArrayQueue 有界队列核心机制」的第一讲。
> 在进入无锁 `push`/`pop` 的 CAS 循环之前，我们先把 `ArrayQueue` 的「骨架」看清楚：
> 它由哪些字段组成、`head`/`tail` 是怎么用一个 `usize` 同时编码「数组下标」和「第几圈」的、`Slot` 长什么样、以及为什么字段要用 `CachePadded` 包起来。

## 1. 本讲目标

学完本讲后，你应当能够：

1. 说出 `ArrayQueue<T>` 的四个字段（`head`、`tail`、`buffer`、`one_lap`）各自的作用。
2. 解释什么是 **stamp（戳）**：如何用单个 `usize` 的低位表示 `index`、高位表示 `lap`。
3. 手算给定 `capacity` 时 `one_lap` 的值，并解释为什么它必须是「严格大于 `cap` 的 2 的幂」。
4. 画出 `new` 之后每个 `Slot` 的初始 `stamp`，以及 `head`/`tail` 的初值。
5. 说明 `Slot<T>` 为什么用 `MaybeUninit<T>`，以及 `CachePadded` 在这里起什么作用。

> 本讲只讲「数据结构与初始化」，**不**展开 `push`/`pop`/`force_push` 的 CAS 主链路——那是下一讲 `u2-l2` 的内容。把骨架看清，下一讲的算法会非常顺。

## 2. 前置知识

本讲假设你已经掌握 `u1-l2`（crate 入口与 `lib.rs`）的内容，并了解以下基础概念。不熟悉的术语下面都做了通俗解释。

- **有界队列（bounded queue）**：容量固定的队列，构造时就分配好一块固定大小的缓冲区，之后不再扩容。`ArrayQueue` 就是有界的。
- **MPMC**：Multiple Producer Multiple Consumer，多个生产者线程同时入队、多个消费者线程同时出队。
- **环形缓冲（ring buffer）**：一块固定大小的数组，写满后「绕回」开头继续写，像一个环。`ArrayQueue` 内部就是一个环形缓冲。
- **原子类型 `AtomicUsize`**：可以被多个线程安全读写的整数，配合 `Ordering` 控制内存可见性。
- **`usize` 的位运算**：按位与 `&`、按位或 `|`、按位取反 `!`、左移 `<<`。本讲会用到「用低位当 index、高位当计数器」的位打包技巧。
- **`MaybeUninit<T>`**：Rust 标准库里表示「可能还没初始化」的内存。它允许我们拥有一块「还没写入有效值」的槽位，而不会违反 Rust 的「值必须有效」规则。
- **缓存行（cache line）与伪共享（false sharing）**：CPU 缓存以「行」（通常 64 字节）为单位加载内存；如果两个线程频繁写各自位于同一行的变量，会让缓存行反复失效，拖慢性能。`CachePadded` 就是用来把变量「撑开」到独占一行的。

> 本讲引用的算法原型是 Dmitry Vyukov 的 bounded MPMC queue，源码开头给出了出处：
> [src/array_queue.rs:1-4](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L1-L4)——说明本实现基于 Vyukov 的方案。

## 3. 本讲源码地图

本讲几乎全部围绕一个文件展开：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [src/array_queue.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L1-L629) | `ArrayQueue` 的全部实现 | `Slot`、`ArrayQueue` 结构体定义与 `new` |

补充：`CachePadded` 与 `Backoff` 来自 `crossbeam-utils`，在文件顶部导入：
[src/array_queue.rs:15](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L15)——`use crossbeam_utils::{Backoff, CachePadded};`。本讲只把 `CachePadded` 当作「把数据撑到一个缓存行」的黑盒来用，深入原理留到 `u4-l2`。

---

## 4. 核心概念与源码讲解

本讲拆成 5 个最小模块：

1. `ArrayQueue<T>` 的字段构成与 `new` 初始化
2. `head`/`tail` 的 stamp 编码
3. `one_lap` 与 `next_power_of_two`
4. `Slot<T>`：stamp + `MaybeUninit`
5. `CachePadded` 包装

### 4.1 `ArrayQueue<T>` 的字段构成与 `new` 初始化

#### 4.1.1 概念说明

`ArrayQueue<T>` 是一个**有界的 MPMC 队列**：构造时一次性分配一块能容纳 `cap` 个元素的缓冲区，之后这个大小永远不变。它对外只暴露 `push`/`pop`/`force_push` 等方法（下一讲讲），但其内部状态由四个字段共同维护。

把队列想象成一条环形跑道：

- `head` 是「读取指针」，消费者从这里取元素。
- `tail` 是「写入指针」，生产者往这里放元素。
- `buffer` 是跑道本身，一格格的槽位。
- `one_lap` 是「跑一整圈的步长」，用来区分「现在是第几圈」。

#### 4.1.2 核心流程

`new(cap)` 的初始化流程（伪代码）：

```text
assert cap > 0
head = 0              // { lap: 0, index: 0 }
tail = 0              // { lap: 0, index: 0 }
buffer = 分配 cap 个槽，第 i 个槽的 stamp 初始化为 i   // { lap: 0, index: i }
one_lap = (cap + 1).next_power_of_two()   // 严格大于 cap 的最小 2 的幂
组装并返回 Self
```

要点：

- `head` 与 `tail` 初值都是 `0`（既不是空也不是满，而是「还没开始」）。
- 每个 `Slot` 的 `stamp` 初值设为它自己的下标 `i`，这一步非常关键，下一模块会解释。
- `one_lap` 不是随便取的，它由 `cap` 决定，模块 4.3 专门讲。

#### 4.1.3 源码精读

结构体定义见 [src/array_queue.rs:52-74](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L52-L74)。四个字段：

- `head: CachePadded<AtomicUsize>`（[L59](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L59)）——出队指针，用 `CachePadded` 包裹以避免伪共享。
- `tail: CachePadded<AtomicUsize>`（[L67](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L67)）——入队指针。
- `buffer: Box<[Slot<T>]>`（[L70](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L70)）——堆上分配的槽位数组，长度等于 `cap`。
- `one_lap: usize`（[L73](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L73)）——注释写得很清楚：「一个值为 `{ lap: 1, index: 0 }` 的 stamp」。

`new` 的实现见 [src/array_queue.rs:96-125](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L96-L125)。几个关键点：

- [L97](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L97)：`assert!(cap > 0, ...)`——容量为 0 会 panic（注释在 [L85-L87](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L85-L87) 的 `# Panics` 段落里写明了）。
- [L99-L102](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L99-L102)：`head = 0; tail = 0;`。
- [L106-L114](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L106-L114)：用迭代器为每个槽设置 `stamp = i`、`value` 为未初始化。
- [L116-L117](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L116-L117)：计算 `one_lap`。

另外，`capacity()` 直接返回缓冲区长度，见 [src/array_queue.rs:440-443](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L440-L443)，说明运行期容量恒等于 `buffer.len()`，不会再变。

#### 4.1.4 代码实践

**实践目标**：亲手构造一个队列，观察它的容量与初值。

**操作步骤**（源码阅读型 + 可选运行）：

1. 阅读文档示例 [src/array_queue.rs:42-51](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L42-L51)，注意 `ArrayQueue::new(2)` 之后连续 `push('a')`、`push('b')` 成功、`push('c')` 因满返回 `Err('c')`。
2. （可选运行）在仓库内写一个临时 binary（示例代码，非项目原有）：

   ```rust
   use crossbeam_queue::ArrayQueue;
   fn main() {
       let q: ArrayQueue<i32> = ArrayQueue::new(3);
       println!("capacity = {}", q.capacity()); // 预期 3
       println!("is_empty = {}", q.is_empty()); // 预期 true
       println!("len = {}", q.len());           // 预期 0
   }
   ```

**需要观察的现象**：`capacity()` 恒等于构造时传入的 `3`，再次印证「容量在构造时固定」。

**预期结果**：`capacity = 3`，`is_empty = true`，`len = 0`。运行结果待本地验证（命令为 `cargo run`）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `new(0)` 会 panic，而不是返回一个空队列？

**参考答案**：源码 [L97](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L97) 显式 `assert!(cap > 0)`。容量为 0 时 `one_lap = (0+1).next_power_of_two() = 1`，会导致 `one_lap - 1 = 0`，位掩码失效、lap 与 index 无法分离，无锁算法的正确性前提被破坏；同时一个永远放不进任何元素的队列也没有实用意义，因此直接 panic。

**练习 2**：`ArrayQueue` 的容量能在运行期改变吗？

**参考答案**：不能。`buffer: Box<[Slot<T>]>` 在 `new` 中一次性分配（[L106-L114](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L106-L114)），之后只读不扩；`capacity()` 直接返回 `buffer.len()`（[L441](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L441)），恒定不变。这正是「有界」「预分配」的含义。

---

### 4.2 `head`/`tail` 的 stamp 编码

#### 4.2.1 概念说明

环形缓冲有个经典难题：当 `head == tail` 时，到底是「空」还是「满」？线性队列里指针只能往前走，没法区分这两种状态。

Vyukov 的解法是给指针加一个 **lap（圈数）** 维度：除了记录「在数组里的第几格（index）」，还记录「已经绕了几圈（lap）」。这样即使 `index` 相同，只要 `lap` 不同，就是不同的状态，空和满就不会混淆。

于是 `head` 和 `tail` 都不再是单纯的「下标」，而是一个叫 **stamp（戳）** 的复合值，把 `index` 和 `lap` 打包进一个 `usize` 里。

#### 4.2.2 核心流程

位打包规则（设 `one_lap` 为 2 的幂）：

```text
stamp 的低位（共 log2(one_lap) 位）= index     // 数组下标
stamp 的高位（其余位）              = lap × one_lap  // 圈数 × 步长
```

拆解时用两条位运算（`MASK = one_lap - 1`）：

```text
index = stamp & MASK          // 取低位
lap   = stamp & !MASK         // 取高位（清掉低位）
```

「加一圈」就是把高位加上 `one_lap`（低位清零回到 0）；「在同一圈里前进一步」就是 `stamp + 1`。这正是源码里反复出现的两种推进方式。

#### 4.2.3 源码精读

字段注释把编码规则讲得很明白，见 [src/array_queue.rs:53-59](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L53-L59)（head）与 [src/array_queue.rs:61-67](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L61-L67)（tail），原文都写着：

> This value is a "stamp" consisting of an index into the buffer and a lap, but packed into a single `usize`. The lower bits represent the index, while the upper bits represent the lap.

拆解逻辑在 `push`/`pop` 中出现多次（下一讲精读），这里只看写法。以 `push_or_else` 为例，[src/array_queue.rs:135-137](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L135-L137)：

```rust
let index = tail & (self.one_lap - 1);   // 取 index：低位
let lap = tail & !(self.one_lap - 1);    // 取 lap：高位
```

而「同圈前进一步」与「跨圈绕回」两种推进方式，见 [src/array_queue.rs:139-147](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L139-L147)：

```rust
let new_tail = if index + 1 < self.capacity() {
    tail + 1                                  // 同圈，index + 1
} else {
    lap.wrapping_add(self.one_lap)            // 跨圈，lap += 1，index 回 0
};
```

#### 4.2.4 代码实践

**实践目标**：用一个独立小程序（示例代码）验证位打包与拆解，建立对 `index`/`lap` 的直觉。

**操作步骤**：

1. 阅读上面两段源码，确认 `MASK = one_lap - 1`。
2. （可选运行）写一段示例代码模拟拆解（不依赖 crate，纯数学）：

   ```rust
   fn main() {
       let one_lap: usize = 4;            // 对应 cap=3
       let mask = one_lap - 1;            // 0b011
       for stamp in [0usize, 2, 4, 6] {
           let index = stamp & mask;
           let lap = (stamp & !mask) / one_lap;
           println!("stamp={stamp:>2} -> index={index}, lap={lap}");
       }
   }
   ```

**需要观察的现象**：`stamp=4` 时 `index=0, lap=1`（绕了一圈回到第 0 格，但圈数变了）；`stamp=2` 时 `index=2, lap=0`。

**预期结果**（确定性数学，可直接给出）：

```text
stamp= 0 -> index=0, lap=0
stamp= 2 -> index=2, lap=0
stamp= 4 -> index=0, lap=1
stamp= 6 -> index=2, lap=1
```

#### 4.2.5 小练习与答案

**练习 1**：给定 `one_lap = 8`，`stamp = 19`，求 `index` 与 `lap`。

**参考答案**：`MASK = 8 - 1 = 7 = 0b0111`。`index = 19 & 7 = 19 - 16 = 3`；`lap = (19 & !7) / 8 = 16 / 8 = 2`。即 `{ lap: 2, index: 3 }`。

**练习 2**：为什么「跨圈」时是 `lap.wrapping_add(self.one_lap)` 而不是 `lap + 1`？

**参考答案**：`lap` 不是「圈数 1、2、3」，而是「圈数乘以步长」后存在高位里的值（见 [L55-L56](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L55-L56)）。要让高位代表「多了一圈」，必须加上 `one_lap`。用 `wrapping_add` 是为了在理论上圈数极大、高位溢出时也能回绕，避免 debug 溢出 panic。

---

### 4.3 `one_lap` 与 `next_power_of_two`

#### 4.3.1 概念说明

`one_lap` 这个名字很容易让人困惑。它的字面意义见字段注释 [src/array_queue.rs:72-73](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L72-L73)：「一个值为 `{ lap: 1, index: 0 }` 的 stamp」。也就是说，**`one_lap` 本身就是一个 stamp**——它表示「往前恰好一圈、index 归零」的那个值。所以代码里 `stamp + one_lap` 就是「往前一圈」，非常直观。

`one_lap` 必须同时满足两个条件：

1. 它是 **2 的幂**——这样 `one_lap - 1` 就是一串连续的 `1`，能直接当位掩码用（`stamp & (one_lap - 1)` 等价于 `stamp % one_lap`，但快得多）。
2. 它 **严格大于 `cap`**——给 stamp 留出比槽位数更多的取值空间，让「满」和「空」、以及「待写」「待读」等状态不发生歧义（下一讲会看到 `head.wrapping_add(one_lap) == tail` 这种满判定）。

#### 4.3.2 核心流程

计算公式见 [src/array_queue.rs:117](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L117)：

```text
one_lap = (cap + 1).next_power_of_two()
```

`usize::next_power_of_two()` 返回「≥ 自身的最小 2 的幂」。加 1 是为了保证结果严格大于 `cap`。例如：

| `cap` | `cap + 1` | `next_power_of_two` | `one_lap` | index 位数（log2） |
| --- | --- | --- | --- | --- |
| 1 | 2 | 2 | 2 | 1 |
| 2 | 3 | 4 | 4 | 2 |
| 3 | 4 | 4 | 4 | 2 |
| 4 | 5 | 8 | 8 | 3 |
| 5 | 6 | 8 | 8 | 3 |
| 8 | 9 | 16 | 16 | 4 |

可以看到 `one_lap > cap` 恒成立，且永远是 2 的幂。

> 一个细节：因为 `one_lap > cap`，index 字段能表示的值（`0..one_lap-1`）会比实际槽位数多。这意味着 **slot 的 `stamp` 可能瞬时取到 `index >= cap` 的值**（例如 `cap=3` 时 stamp 可能等于 3）。这种值只是一个「标记」，不会被用来索引 `buffer`——只有 `head`/`tail` 解出的 index 才会用来访问数组，而它们的 index 会被「跨圈」逻辑限制在 `0..cap`。下一讲讲 `push` 时你会看到 `stamp = tail + 1` 正是这类瞬态值。

#### 4.3.3 源码精读

- 字段定义与含义：[src/array_queue.rs:72-73](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L72-L73)。
- 计算位置：[src/array_queue.rs:116-117](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L116-L117)，注释写「One lap is the smallest power of two greater than `cap`」。
- 「跨圈加 `one_lap`」的用法：[src/array_queue.rs:146](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L146)（`lap.wrapping_add(self.one_lap)`）。
- 「满判定 `head + one_lap == tail`」的用法：[src/array_queue.rs:208](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L208) 与 [src/array_queue.rs:491](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L491)（`is_full`），说明 `one_lap` 正是用来度量「一圈」的步长。

#### 4.3.4 代码实践

**实践目标**：手算 `capacity=5` 时的 `one_lap`，并验证公式。

**操作步骤**：

1. 套公式：`one_lap = (5 + 1).next_power_of_two() = 6.next_power_of_two()`。
2. 6 之后的第一个 2 的幂是 8，故 `one_lap = 8`。
3. 推出 index 字段位宽 = `log2(8) = 3` 位（能表示 `0..7`），掩码 `MASK = 8 - 1 = 0b000111`。

**需要观察的现象**：`one_lap = 8 > cap = 5`，满足「严格大于 cap」且为 2 的幂。

**预期结果**：`capacity=5` 时 `one_lap = 8`，index 占低 3 位，lap 占高位。

#### 4.3.5 小练习与答案

**练习 1**：为什么公式是 `(cap + 1).next_power_of_two()` 而不是 `cap.next_power_of_two()`？

**参考答案**：当 `cap` 本身就是 2 的幂（如 `cap=4`）时，`cap.next_power_of_two()` 会等于 `cap` 本身（4），导致 `one_lap == cap`，不满足「严格大于 cap」的要求，会破坏满/空判定。加 1 后 `(4+1)=5 → 8`，保证 `one_lap > cap`。

**练习 2**：`cap = 7` 时 `one_lap` 等于多少？index 字段几位？

**参考答案**：`one_lap = (7+1).next_power_of_two() = 8.next_power_of_two() = 8`；index 字段 `log2(8) = 3` 位。注意此时 `one_lap = 8` 仅比 `cap = 7` 大 1，index 字段能表示 `0..7` 共 8 个值，恰好覆盖 7 个槽位加 1 个瞬态标记位。

---

### 4.4 `Slot<T>`：stamp + `MaybeUninit`

#### 4.4.1 概念说明

`buffer` 是一格格的 `Slot<T>`。每个槽要同时回答两个问题：

1. **这一格现在处于什么状态？**——是「马上要被写入」「有数据可读」还是「已读完可回收」？这个状态由 `stamp` 记录。
2. **这一格里到底存了什么？**——一个 `T` 类型的值。但队列运行中，某些时刻这一格「还没有有效值」（比如刚分配、或已被读出还没被覆盖）。Rust 不允许存在「无效的 `T`」，所以用 `MaybeUninit<T>` 来合法地持有「可能未初始化」的内存。

这就是 `Slot<T>` 的两个字段：一个 `stamp`，一个 `value`。

#### 4.4.2 核心流程

`Slot` 的定义见 [src/array_queue.rs:17-27](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L17-L27)。配合 `stamp` 的语义，源码注释（[L19-L22](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L19-L22)）给出了两条判读规则：

```text
若 stamp == tail     → 这个槽是「下一个该被写入」的
若 stamp == head + 1 → 这个槽是「下一个该被读出」的
```

也就是说，`stamp` 同时承担了「状态机」的职责：生产者写完会把 stamp 推进到「可读」状态；消费者读完会把 stamp 推进到「可写」状态。这种「用 stamp 当状态位」的设计避免了额外加锁。

`MaybeUninit<T>` 的生命周期配合：

- 分配时：`MaybeUninit::uninit()`——内存存在但内容未定义（[L111](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L111)）。
- 写入时：`write(MaybeUninit::new(value))`——把值放进去（如 [L166](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L166)）。
- 读出时：`read().assume_init()`——取出并断言「此刻它一定已初始化」（如 [L353](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L353)）。
- 释放时：`assume_init_drop()`——在 `Drop` 里销毁残留值（如 [L567](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L567)）。

`value` 之所以还要再套一层 `UnsafeCell`，是因为多个线程要通过共享引用 `&Slot` 去写内部值，这需要「内部可变性」；这部分的安全性论证属于 `u4-l3`。

#### 4.4.3 源码精读

- `Slot` 结构：[src/array_queue.rs:17-27](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L17-L27)。
- `stamp` 字段及其判读注释：[src/array_queue.rs:18-24](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L18-L24)。
- `value` 字段：[src/array_queue.rs:25-26](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L25-L26)，类型是 `UnsafeCell<MaybeUninit<T>>`。
- 初始化：[src/array_queue.rs:106-114](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L106-L114)，每个槽 `stamp = i`、`value = MaybeUninit::uninit()`。

#### 4.4.4 代码实践

**实践目标**：弄清 `cap=3` 时三个槽各自的初始 `stamp`。

**操作步骤**：

1. 阅读 [L106-L114](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L106-L114)，确认迭代变量 `i` 取 `0..cap`。
2. 对 `cap=3` 列出：`slot[0].stamp=0`、`slot[1].stamp=1`、`slot[2].stamp=2`，三者 `value` 均未初始化。

**需要观察的现象**：初始时每个槽的 `stamp` 恰好等于自己的下标；这与「`stamp == tail` 即可写」配合——刚开始 `tail=0`，所以 `slot[0]` 是第一个可写槽。

**预期结果**（确定性，直接给出）：

| 槽 | 初始 stamp | 解读 `{lap, index}` | value |
| --- | --- | --- | --- |
| `slot[0]` | 0 | `{0, 0}` | 未初始化 |
| `slot[1]` | 1 | `{0, 1}` | 未初始化 |
| `slot[2]` | 2 | `{0, 2}` | 未初始化 |

#### 4.4.5 小练习与答案

**练习 1**：为什么 `value` 用 `MaybeUninit<T>` 而不是直接用 `T`？

**参考答案**：队列运行中，某个槽在某些时刻确实「没有有效值」（刚分配、或刚被 `pop` 走但还没被新 `push` 覆盖）。直接用 `T` 要求每一刻内存里都是合法的 `T`，无法表达这种「空」状态；`MaybeUninit<T>` 则合法地允许「暂时未初始化」，由 `stamp` 来保证「只有已初始化的槽才会被 `assume_init` 读取」。

**练习 2**：初始时为什么要把 `slot[i].stamp` 设成 `i`，而不是全设成 0？

**参考答案**：若全设成 0，则三个槽的 stamp 都是 0，而判读规则是「`stamp == tail` 即可写」——会出现「多个槽同时声称自己可写」的歧义。设成各自的 `i` 后，只有 `slot[0]`（stamp=0）等于初始 `tail=0`，生产者能唯一确定第一个该写的就是 `slot[0]`；写完后 stamp 被推进，再轮到下一个。

---

### 4.5 `CachePadded` 包装

#### 4.5.1 概念说明

`head` 和 `tail` 是两个会被高频原子读写的字段。如果它们恰好落在 CPU 的**同一个缓存行**里，就会出现 **伪共享（false sharing）**：

- 生产者线程频繁写 `tail`，让该缓存行失效；
- 消费者线程频繁写 `head`，也让该缓存行失效；
- 两个线程本来各写各的变量，却因为共用一行而互相把对方的缓存「打飞」，导致缓存频繁在核心间同步，吞吐暴跌。

`CachePadded` 的作用就是把内部数据「填充」到独占一整个缓存行（通常是 64 或 128 字节），让 `head` 和 `tail` 物理上分开，互不干扰。这是一个纯性能优化，不改变逻辑正确性。

> 伪共享的成因、`CachePadded` 的内部实现，以及 `Backoff` 退避策略，是 `u4-l2` 的主题；本讲只需把它当作「撑开缓存行」的黑盒。

#### 4.5.2 核心流程

`CachePadded` 的使用出现在两处：

1. 字段类型：`head: CachePadded<AtomicUsize>`、`tail: CachePadded<AtomicUsize>`（[L59](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L59)、[L67](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L67)）。
2. 构造：`CachePadded::new(AtomicUsize::new(head))`（[L122-L123](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L122-L123)）。

值得注意的对照：`buffer` 和 `one_lap` **没有**用 `CachePadded`。因为：

- `one_lap` 在构造后就只读，多核读取不会互相 invalidate（只读数据不引发伪共享）。
- `buffer` 的访问是「按 index 分散到不同槽」的，不同线程通常访问不同槽，冲突本就小；且整个数组太大，整体填充不现实。

只有 `head`/`tail` 这两个「单一热点、被所有线程高频写」的字段才值得填充。

#### 4.5.3 源码精读

- 导入：[src/array_queue.rs:15](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L15)。
- 字段：[src/array_queue.rs:59](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L59) 与 [src/array_queue.rs:67](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L67)。
- 构造：[src/array_queue.rs:122-123](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L122-L123)。

#### 4.5.4 代码实践

**实践目标**：通过阅读源码，理解「为什么只有 `head`/`tail` 被包装」。

**操作步骤**：

1. 打开 [src/array_queue.rs:52-74](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L52-L74)，逐字段判断「它会被多核高频写吗？」。
2. 记录判断结果：`head`/`tail` 是 → 包装；`buffer`（分散访问）/`one_lap`（只读）否 → 不包装。

**需要观察的现象**：`CachePadded` 只出现在「单一热点、全员高频写」的字段上，体现「按需优化」的工程取舍。

**预期结果**：能用自己的话解释「`one_lap` 为什么不需要 `CachePadded`」——因为它是只读的，不会引发伪共享。

#### 4.5.5 小练习与答案

**练习 1**：如果把 `head` 和 `tail` 的 `CachePadded` 去掉，功能还正确吗？

**参考答案**：逻辑仍然正确——`CachePadded` 只改变内存布局（多了一些填充字节），不改变读写语义。代价是性能下降：`head`/`tail` 大概率落到同一缓存行，伪共享让高并发吞吐明显降低。

**练习 2**：`buffer` 为什么不适合用 `CachePadded` 包装每个 `Slot`？

**参考答案**：`CachePadded` 会把每个元素撑到一整个缓存行（64+ 字节）。`Slot` 数组可能有成百上千个，整体填充会浪费大量内存；而且不同线程通常访问不同的 `index`，本就较少撞到同一行。只有 `head`/`tail` 这种「两个变量、全员必争」的场景才值得付出填充的内存代价。

---

## 5. 综合实践

把本讲的五个模块串起来，完成规格要求的实践任务：**手算 `capacity=5` 时的 `one_lap`，并画出 `cap=3` 的队列在 `new` 之后的状态，标注 index 与 lap 各占哪些比特。**

### 第 1 步：手算 `capacity=5` 的 `one_lap`

套用 [src/array_queue.rs:117](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L117) 的公式：

\[
\text{one\_lap} = (\text{cap} + 1).\text{next\_power\_of\_two}() = (5+1).\text{next\_power\_of\_two}() = 6 \rightarrow 8
\]

故 **`one_lap = 8`**（二进制 `0b1000`）。

- index 掩码 `MASK = one_lap - 1 = 7 = 0b0111`，index 占**低 3 位**（bit 0–2），可表示 `0..7`。
- lap 占**高位**（bit 3 及以上），其值为「圈数 × 8」。

> 可用下面这段示例代码（非项目原有）快速核对手算结果：

```rust
fn main() {
    for cap in 1..=8 {
        let one_lap = (cap + 1).next_power_of_two();
        let bits = (cap + 1).next_power_of_two().trailing_zeros();
        println!("cap={cap:>2} -> one_lap={one_lap:>2}, index 位数={bits}");
    }
}
```

预期会打印 `cap=5 -> one_lap=8, index 位数=3`（运行结果待本地验证，但公式是确定性数学）。

### 第 2 步：画出 `cap=3` 队列在 `new` 之后的状态

对 `cap=3`：`one_lap = (3+1).next_power_of_two() = 4`（二进制 `0b100`）。故 index 占**低 2 位**（bit 0–1），lap 占 bit 2 及以上；`MASK = one_lap - 1 = 3 = 0b011`。

为直观，用 4 位二进制展示（高位补 0）：

| 对象 | 十进制 | 二进制（4 位） | index（低 2 位） | lap（≥bit2） | 解读 |
| --- | --- | --- | --- | --- | --- |
| `head` | 0 | `0000` | `00` = 0 | 0 → lap 0 | `{lap:0, index:0}` |
| `tail` | 0 | `0000` | `00` = 0 | 0 → lap 0 | `{lap:0, index:0}` |
| `slot[0].stamp` | 0 | `0000` | `00` = 0 | lap 0 | 下一个该写（stamp==tail==0） |
| `slot[1].stamp` | 1 | `0001` | `01` = 1 | lap 0 | 等待轮到 |
| `slot[2].stamp` | 2 | `0010` | `10` = 2 | lap 0 | 等待轮到 |
| （参考）`one_lap` | 4 | `0100` | `00` = 0 | lap 1 | `{lap:1, index:0}` |

可以画成一张「跑道俯视图」：

```text
              lap 由高位（bit≥2）记录
              index 由低 2 位（bit 0–1）记录

     ┌──────┬──────┬──────┐
     │  s0  │  s1  │  s2  │     三个 Slot
     │stamp │stamp │stamp │
     │  =0  │  =1  │  =2  │     初始 stamp = 各自下标
     │ uninit│uninit│uninit│     value 全部未初始化
     └──▲───┴──────┴──────┘
        │
   head=0, tail=0  （都是 {lap:0, index:0}）
   ⇒ stamp==tail 的只有 slot[0]
   ⇒ 第一个 push 会写入 slot[0]
```

### 第 3 步：自检清单

完成上面两步后，确认你能回答：

- [ ] `cap=5` 时 `one_lap=8`，index 占 3 位。
- [ ] `cap=3` 时 `one_lap=4`，index 占 2 位，`head=tail=0`。
- [ ] 初始 `slot[i].stamp = i`，所以第一个被写入的是 `slot[0]`。
- [ ] `one_lap` 本身就是 `{lap:1, index:0}` 的 stamp，「加一圈」=「加 `one_lap`」。
- [ ] `head`/`tail` 用 `CachePadded` 是为了避开伪共享，不影响逻辑。

如果全部能答上来，你已经完全掌握了 `ArrayQueue` 的骨架，可以进入 `u2-l2` 看真正的 `push`/`pop` CAS 主链路了。

---

## 6. 本讲小结

- `ArrayQueue<T>` 由 `head`、`tail`、`buffer`、`one_lap` 四个字段组成，容量在 `new` 时一次性固定，运行期不变。
- `head`/`tail` 不是普通下标，而是 **stamp**：低位是 `index`、高位是 `lap`，用 `stamp & (one_lap-1)` 与 `stamp & !(one_lap-1)` 拆解。
- `one_lap = (cap + 1).next_power_of_two()`，它**既是 2 的幂、又严格大于 cap**；它本身代表「往前一圈、index 归零」的 stamp，所以「加一圈」就是「加 `one_lap`」。
- `Slot<T>` = `stamp`（状态机）+ `UnsafeCell<MaybeUninit<T>>`（可持有未初始化内存的值槽）；初始 `slot[i].stamp = i` 保证只有 `slot[0]` 一开始可写。
- `head`/`tail` 用 `CachePadded` 包装以避免伪共享，是纯性能优化；`buffer`、`one_lap` 不需要。

## 7. 下一步学习建议

- **下一讲 `u2-l2`「push 与 pop 的无锁主链路」**：本讲只搭好了骨架，下一讲会让它「动」起来——逐行走读 `push_or_else` 的 CAS 循环（`stamp` 匹配、`compare_exchange_weak`、写入值并推进 stamp）以及 `pop` 的对称逻辑，看清满/空到底怎么判定。建议带着本讲的「stamp = index + lap」心智模型去读，会非常顺。
- **横向对比**：如果你已经看过或打算看 `u3`（SegQueue），可以对照两者如何用「一个 stamp/状态字段」表达「待写/待读」状态——ArrayQueue 用 `stamp`，SegQueue 用 WRITE/READ/DESTROY 状态位，思路相通但实现不同。
- **安全性深入**：本讲提到 `MaybeUninit` 与 `UnsafeCell`，但没论证「为什么读出来一定已初始化」「为什么 `Send/Sync` 安全」——这是 `u4-l3`「unsafe 的安全性论证与 MaybeUninit」的主题，读下一讲前可以先去 [src/array_queue.rs:76-77](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-queue/src/array_queue.rs#L76-L77) 的 `unsafe impl Send/Sync` 处留个疑问。
