# Producer：可分裂的生产者

## 1. 本讲目标

上一讲（u4-l1）建立了 plumbing 的整体图景：Producer（拉模式）、Consumer（推模式）、bridge（桥接驱动）。本讲把镜头对准拉模式一侧的核心角色 **Producer**，学完后你应该能够：

1. 逐条说出 `Producer` trait 的契约，理解官方文档为什么把它定义为「splittable `IntoIterator`」（可切分的 `IntoIterator`）。
2. 理解 `split_at(index)` 如何在**同一个中点**上完成生产者与消费者的对齐二分，以及长度信息在其中扮演的角色。
3. 理解 `UnindexedProducer::split` 的「按能力切分」语义：为什么返回 `Option`、为什么切分点由数据自己决定。
4. 读懂 `tests/producer_split_at.rs` 如何用「三刀四段」把 Producer 的正确性契约变成可执行断言。
5. 在综合实践中，为一个自定义区间类型 `MyRange` 从零实现完整的 Producer，并用复刻的测试骨架验证它。

## 2. 前置知识

本讲需要以下基础（不熟悉的概念会在此解释）：

- **plumbing 三角色**（u4-l1）：`Producer` 负责产出数据（拉模式），`Consumer`/`Folder` 负责消化数据（推模式），`Reducer` 负责合并两半的结果。本讲只深入第一个。
- **`ExactSizeIterator`**：标准库 trait，表示迭代器**在开始迭代前就知道自己会产出多少个元素**，提供 `len()`，且 `size_hint()` 返回精确的 `(n, Some(n))`。
- **`DoubleEndedIterator`**：标准库 trait，表示迭代器可以从**两端**推进（`next()` 与 `next_back()`）。`Producer::IntoIter` 必须同时实现这两个 trait——这是 `rev()`、反向收集等能力的基础。
- **分治与切分**：并行的来源是「把数据分成两半，两半同时处理」。对 \( N \) 个元素反复对半切，递归深度约为 \( \lceil \log_2 N \rceil \)，最终得到 \( N \) 个叶子。Rayon 的 `bridge` 正是这样一个递归，切分点取中点 \( \lfloor len/2 \rfloor \)。
- **索引坐标**：本讲反复出现「下标」这个词，它指的是**相对当前这个生产者起点的偏移量**，不是全局数组下标。`split_at(3)` 的意思是「从我这份数据的开头数 3 个元素处切一刀」。

一个值得先建立的直觉：**生产者自己不知道自己多长**。听起来很奇怪，但这是刻意设计——长度由框架（`bridge`）记账，生产者只负责「给我一个下标，我还你两半」。这个设计贯穿本讲，4.1 节会解释原因。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/iter/plumbing/mod.rs` | 定义 `Producer`、`UnindexedProducer`、`ProducerCallback` 等 trait，以及 `bridge` / `bridge_unindexed` 驱动函数 |
| `src/iter/plumbing/README.md` | 官方设计文档，解释拉/推两种模式与两种切分方式的分工 |
| `src/slice/mod.rs` | 切片生产者 `IterProducer`——最简单、最理想的 `split_at` 范本 |
| `src/range.rs` | 整数范围生产者，同时展示了索引（`split_at`）与无索引（`split`）两种实现 |
| `src/str.rs` | 字符串生产者 `CharsProducer`——「按能力切分」的典型：切分点必须落在 UTF-8 字符边界 |
| `src/iter/len.rs` | `MinLenProducer` / `MaxLenProducer`——「包装生产者」模式：只改写粒度窗口，`split_at` 纯转发 |
| `tests/producer_split_at.rs` | 对仓库所有生产者做「三刀四段」压力测试的集成测试 |

## 4. 核心概念与源码讲解

### 4.1 Producer trait：可分裂的 IntoIterator

#### 4.1.1 概念说明

`Producer` 是拉模式的数据源。官方给它的定义是「splittable `IntoIterator`」（可切分的 `IntoIterator`）：

- 像 `IntoIterator` 一样，它可以**随时**转换成一个普通迭代器，然后按需逐个产出元素——到这一步就完全是串行世界了；
- 但在转换**之前**，它还可以在指定下标处被**切分**（`split_at`），一刀变两个：一个产出切分点之前的元素，一个产出之后的元素。两个生产者还可以继续切，或各自转成迭代器。

Rayon 用这种切分把数据分给不同线程。为什么这比「给每个线程一个迭代器」更好？因为普通迭代器只能从头部一个一个拿，无法「从第 100 万个元素开始」；而 `split_at` 让任意一段数据可以**独立、完整**地交给另一个线程，无需任何中间状态。

三个关键的契约细节（都会在源码里看到）：

1. **长度固定但不可查询**：每个生产者产出的元素个数 \( N \) 是固定的，但 API 不提供查询长度的方法，由消费者侧（`bridge`）记账。
2. **`IntoIter` 必须是 `Iterator + DoubleEndedIterator + ExactSizeIterator`**：因为切分后的每一段要按串行方式消化，且要支持反向与精确长度。
3. **`min_len` / `max_len` 是粒度窗口**：分别默认 1 和 `usize::MAX`，可被 `with_min_len()` / `with_max_len()` 改写（u3-l3 讲过用户侧视角，本讲看生产者侧的实现）。

#### 4.1.2 核心流程

一个生产者的一生：

```
with_producer(callback)          // 迭代器把所有权交给生产者，交给回调
        │
        ▼
bridge_producer_consumer(len, producer, consumer)
        │  递归：还能切吗？（LengthSplitter 裁决）
        │  ├── 能切：mid = len / 2
        │  │        producer.split_at(mid)   → (左生产者, 右生产者)
        │  │        consumer.split_at(mid)   → (左消费者, 右消费者, reducer)
        │  │        join_context(左半递归, 右半递归)   ← 工作窃取发生在这里
        │  │        reducer.reduce(左结果, 右结果)
        │  └── 不能切：producer.into_iter() → 串行迭代喂给 Folder
        ▼
    最终单值结果
```

要点：**生产者与消费者在同一个 `mid` 上切分**（下面源码精读会看到这两行紧挨在一起）。这就是长度信息的用处——`bridge` 持有 `len`，每次算出 `mid = len/2`，把 `mid` 同时告诉生产者（切数据）和消费者（切写入位置/结果分区），两边永远对齐。

#### 4.1.3 源码精读

**trait 定义**。先看 [src/iter/plumbing/mod.rs:56-109](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L56-L109)——完整的 `Producer` trait，只有六个成员：

```rust
pub trait Producer: Send + Sized {
    type Item;
    type IntoIter: Iterator<Item = Self::Item> + DoubleEndedIterator + ExactSizeIterator;

    fn into_iter(self) -> Self::IntoIter;

    fn min_len(&self) -> usize { 1 }
    fn max_len(&self) -> usize { usize::MAX }

    /// Split into two producers; one produces items `0..index`, the
    /// other `index..N`. Index must be less than or equal to `N`.
    fn split_at(self, index: usize) -> (Self, Self);

    fn fold_with<F>(self, folder: F) -> F
    where F: Folder<Self::Item>,
    {
        folder.consume_iter(self.into_iter())
    }
}
```

- trait 整体要求 `Send + Sized`：生产者要能被移动到别的线程（工作窃取的前提），按值消费。
- `split_at(self, index)` **按值拿走 `self`、返回两个新的 `Self`**——切分是所有权的切分，没有借用、没有共享计数。返回的左半产出下标 `0..index` 的元素，右半产出 `index..N`。
- `fold_with` 有默认实现：转成迭代器后灌给 `folder`。绝大多数生产者不需要覆写它。

trait 上方的文档注释（[src/iter/plumbing/mod.rs:32-55](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L32-L55)）值得细读两段。一段解释了「长度不可查询」：

> Note that each producer will always produce a fixed number of items N. However, this number N is not queryable through the API; the consumer is expected to track it.

另一段解释了为什么不直接继承 `IntoIterator`：受 [rust-lang/rust#20671](https://github.com/rust-lang/rust/issues/20671) 限制，无法在继承 `IntoIterator` 的同时追加 `DoubleEndedIterator + ExactSizeIterator` 约束，所以把 `into_iter` 内联进了 trait。

**最简范本：切片生产者**。[src/slice/mod.rs:859-875](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/mod.rs#L859-L875)：

```rust
struct IterProducer<'data, T: Sync> {
    slice: &'data [T],
}

impl<'data, T: 'data + Sync> Producer for IterProducer<'data, T> {
    type Item = &'data T;
    type IntoIter = ::std::slice::Iter<'data, T>;

    fn into_iter(self) -> Self::IntoIter {
        self.slice.iter()
    }

    fn split_at(self, index: usize) -> (Self, Self) {
        let (left, right) = self.slice.split_at(index);
        (IterProducer { slice: left }, IterProducer { slice: right })
    }
}
```

整个实现就是把 `split_at` 委托给切片自带的 `[T]::split_at`——O(1)，只调整指针和长度。切片是模范数据源（u2-l2 讲过），在这里体现得最直白：**`split_at` 越接近 O(1)，切分开销越低，并行收益越好**。注意它连 `min_len`/`max_len`/`fold_with` 都没写，全用默认值。

它是如何被创建的？看切片迭代器的 `with_producer`（[src/slice/mod.rs:851-856](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/slice/mod.rs#L851-L856)）：把借用的切片直接装进生产器，交给回调。这是所有索引数据源的通用写法——**迭代器结构体负责保存用户可见的状态，生产者结构体只保存切分所需的裸数据**。

**带算术细节的范本：范围生产者**。[src/range.rs:177-193](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/range.rs#L177-L193)：

```rust
impl Producer for IterProducer<$t> {
    type Item = <Range<$t> as Iterator>::Item;
    type IntoIter = Range<$t>;
    fn into_iter(self) -> Self::IntoIter {
        self.range
    }

    fn split_at(self, index: usize) -> (Self, Self) {
        assert!(index <= self.range.len());
        // For signed $t, the length and requested index could be greater than $t::MAX, and
        // then `index as $t` could wrap to negative, so wrapping_add is necessary.
        let mid = self.range.start.wrapping_add(index as $t);
        let left = self.range.start..mid;
        let right = mid..self.range.end;
        (IterProducer { range: left }, IterProducer { range: right })
    }
}
```

两处细节：入口先 `assert!(index <= self.range.len())`——契约要求下标不越过 \( N \)，越界直接 panic 而不是返回错误；计算中点用 `wrapping_add`，因为对有符号类型，把 `usize` 下标转型成 `i8`/`i16` 这类窄类型时可能回绕成负数（例如 `Range<i8>` 的长度可以超过 `i8::MAX` 吗？不会，但中间量 `index as $t` 在窄类型上可能溢出），回绕在这里恰好给出正确结果。这提醒我们：**`split_at` 是纯算术，但它必须在自己的整数域里是安全的**。

**包装生产者：只改粒度，不改切分**。[src/iter/len.rs:87-131](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/len.rs#L87-L131) 的 `MinLenProducer`（`with_min_len()` 的内部实现）：

```rust
impl<P> Producer for MinLenProducer<P>
where P: Producer,
{
    fn min_len(&self) -> usize {
        Ord::max(self.min, self.base.min_len())
    }
    // max_len、into_iter、fold_with 均直接转发……

    fn split_at(self, index: usize) -> (Self, Self) {
        let (left, right) = self.base.split_at(index);
        (
            MinLenProducer { base: left, min: self.min },
            MinLenProducer { base: right, min: self.min },
        )
    }
}
```

`split_at` 纯转发给内部生产者，自己只在 `min_len` 上取 `max`——把粒度下限抬到用户要求。这展示了「包装生产者」模式：**适配器叠加时，切分逻辑层层委托到底层数据源，只有策略（粒度窗口）在包装层被改写**。`MaxLenProducer`（[src/iter/len.rs:222-261](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/len.rs#L222-L261)）对称，只是把 `min_len` 换成了 `Ord::min(self.max, ...)`。

**长度信息在切分中的作用**。最后看 `bridge` 的递归核心 [src/iter/plumbing/mod.rs:385-435](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L385-L435)，尤其这三行：

```rust
} else if splitter.try_split(len, migrated) {
    let mid = len / 2;
    let (left_producer, right_producer) = producer.split_at(mid);
    let (left_consumer, right_consumer, reducer) = consumer.split_at(mid);
```

（[src/iter/plumbing/mod.rs:406-409](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L406-L409)）

`len` 来自最外层 `bridge()` 里的一句 `let len = par_iter.len();`（[src/iter/plumbing/mod.rs:346-352](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L346-L352)），也就是说**长度来自 `IndexedParallelIterator::len()`，而不是生产者**。框架拿着 `len` 算出中点 \( mid = \lfloor len/2 \rfloor \)，再让生产者与消费者**在同一个 mid 上对齐切分**——生产者据此切数据，消费者据此划分写入区间（collect 的分段写入，u4-l4 会详述）。递归左右两半时，长度分别传 `mid` 与 `len - mid`，这样每条递归路径始终知道「我这段还有多长」，即便生产者自己从不保存这个数。

#### 4.1.4 代码实践

**实践目标**：直观感受「切分是纯算术」以及粒度测试的真实规模。

**操作步骤**：

1. 运行仓库的生产者压力测试，`--nocapture` 让测试里的 `println!` 输出可见：

   ```bash
   cargo test -p rayon --test producer_split_at -- --nocapture range
   ```

2. 观察输出中大量形如 `Split { i: 3, j: 3, k: 7, reverse: false }` 的行——每行对应一次完整的「三刀四段」验证（4.3 节拆解）。

**需要观察的现象**：`range` 这一个测试对 10 个元素枚举了全部满足 \( i \le j \le k \)、三者取值于 \( 0..=10 \) 的组合。组合数为 \( \binom{13}{3} = 286 \) 组，每组正反两个方向，共约 572 次打印。

**预期结果**：测试通过，且打印行数与上面的估算同量级（精确行数待本地验证）。这个实验的目的是让你体会：**生产者契约的验证是穷举式的**——对 10 个元素就把所有切分方式全试了一遍。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `split_at` 的参数是 `usize` 下标，而不是「比例」（如 0.5）或者 `&mut self` 借用？

**参考答案**：因为消费者（例如 collect 的分段写入）需要**精确知道边界在哪个元素上**，才能把自己的状态（写入位置）对齐切分；比例无法给出精确边界。而按值 `self -> (Self, Self)` 让切分成为所有权操作：两半完全独立、无共享状态，天然线程安全，也使生产者可以 `Send` 地被窃取到别的线程。

**练习 2**：`IntoIter` 为什么要求数 `DoubleEndedIterator`？

**参考答案**：切分出的每一段最终会转成迭代器串行消化；`rev()` 等适配器要求底层能从两端推进（`next_back`）。`producer_split_at` 测试的反向分支正是用 `chain.rev()` 验证四段的 `DoubleEnded` 行为与正向严格互逆。

### 4.2 UnindexedProducer：按能力切分

#### 4.2.1 概念说明

并非所有数据源都知道自己的长度：字符串的字符数需要数过才知道（UTF-8 变长）、`Range<u64>` 在 32 位平台上长度可能塞不进 `usize`。对这些数据，框架无法报出「第 \( mid \) 个元素在哪」，于是换一种协议：**「请你自己看着办，大约对半分」**——这就是 `UnindexedProducer::split`。

「按能力切分」有两层含义：

1. **切分点由数据自身结构决定**。框架不指定下标，数据在「自己能力允许的地方」切：字符串必须落在字符边界上，树可以在任意子节点处掰开。
2. **数据有权说「分不动了」**。返回类型是 `(Self, Option<Self>)`，`None` 表示无法再切（比如只剩 0 或 1 个字符），框架收到 `None` 后就放弃并行、直接串行消化。

对比记忆：`split_at(index)` 是「命令式」——框架拿着长度指挥刀落在哪里；`split()` 是「声明式」——框架只表达意图（想并行），数据自己决定怎么配合。

一个常被问到的设计问题：**为什么 `Producer` 不继承 `UnindexedProducer`**？源码注释（[src/iter/plumbing/mod.rs:228-230](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L228-L230)）直接回答了：原则上可以，但那要求生产者**随身携带自己的长度**（才能实现 `split` = `split_at(len/2)`），而设计选择了把长度留在框架侧。

#### 4.2.2 核心流程

`bridge_unindexed` 的递归（与 `bridge` 平行的无索引版本）：

```
bridge_unindexed(producer, consumer):
    若 consumer.full()：短路，直接完成
    若 Splitter 还有切分预算：
        (left, opt_right) = producer.split()
        ├── opt_right = Some(right)：
        │       join_context(递归(left), 递归(right))，reducer 合并
        └── opt_right = None：
                producer 串行 fold_with(consumer.into_folder())
    否则：producer 串行 fold_with(consumer.into_folder())
```

注意与索引版本的两处差异：没有 `len`，所以裁决切分的不是 `LengthSplitter` 而是 `Splitter`（纯预算，不含长度条件，见 [src/iter/plumbing/mod.rs:250-284](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L250-L284)，其「被窃取就重置预算」的机制 u3-l3 已讲过）；消费者切分用的是 `split_off_left`（无下标，见 [src/iter/plumbing/mod.rs:208-221](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L208-L221)）。

#### 4.2.3 源码精读

**trait 定义**。[src/iter/plumbing/mod.rs:231-243](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L231-L243)：

```rust
pub trait UnindexedProducer: Send + Sized {
    type Item;

    /// Split midway into a new producer if possible, otherwise return `None`.
    fn split(self) -> (Self, Option<Self>);

    fn fold_with<F>(self, folder: F) -> F
    where
        F: Folder<Self::Item>;
}
```

只有三个成员，比 `Producer` 简单：没有 `IntoIter` 关联类型约束（不需要支持按下标随机进入，也就不强求 `ExactSizeIterator`），没有粒度窗口（没有长度，无从谈起）。

**按能力切分的典型：字符串**。`CharsProducer`（[src/str.rs:470-504](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/str.rs#L470-L504)）：

```rust
struct CharsProducer<'ch> {
    chars: &'ch str,
}

impl<'ch> UnindexedProducer for CharsProducer<'ch> {
    type Item = char;

    fn split(self) -> (Self, Option<Self>) {
        match split(self.chars) {
            Some((left, right)) => (
                CharsProducer { chars: left },
                Some(CharsProducer { chars: right }),
            ),
            None => (self, None),
        }
    }

    fn fold_with<F>(self, folder: F) -> F
    where
        F: Folder<Self::Item>,
    {
        folder.consume_iter(self.chars.chars())
    }
}
```

切分委托给模块内的辅助函数 `split`（[src/str.rs:49-56](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/str.rs#L49-L56)）：先找字符中点，`index > 0` 才返回两半，否则返回 `None`（整段串短得只有一个/零个字符，切不动）。找中点的算法（[src/str.rs:27-45](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/str.rs#L27-L45)）体现了「按能力」：先按**字节**长度取 `len / 2`，然后从该字节向后找第一个 UTF-8 字符边界，找不到再向前找，都不行返回 0：

```rust
let mid = chars.len() / 2;   // 字节中点，未必是字符边界
let (left, right) = chars.as_bytes().split_at(mid);
match right.iter().copied().position(is_char_boundary) {
    Some(i) => mid + i,
    None => left.iter().copied().rposition(is_char_boundary).unwrap_or(0),
}
```

**返回 `None` 的典型：短范围**。`Range<u64>` 等宽类型走无索引路径时的 `split`（[src/range.rs:240-253](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/range.rs#L240-L253)）：

```rust
impl UnindexedProducer for IterProducer<$t> {
    type Item = $t;

    fn split(mut self) -> (Self, Option<Self>) {
        let index = self.range.unindexed_len() / 2;
        if index > 0 {
            let mid = self.range.start.wrapping_add(index as $t);
            let right = mid..self.range.end;
            self.range.end = mid;
            (self, Some(IterProducer { range: right }))
        } else {
            (self, None)
        }
    }
    // fold_with: folder.consume_iter(self)
}
```

长度为 0 或 1 时 `index == 0`，返回 `(self, None)`——「我分不动，请直接串行消化我」。这正是 `Option` 存在的意义：**切分能力是数据的一种属性，不是框架可以强求的**。

顺带一提，同一个 `IterProducer` 结构体在这份文件里同时实现了 `Producer`（4.1 节）与 `UnindexedProducer`——同一个数据源，窄整数类型（如 `i32`，走 [src/range.rs:177-193](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/range.rs#L177-L193)）享受精确切分，宽整数类型（如 `u64`，走上面这段）退化为按能力切分。

**驱动侧的对应处理**。[src/iter/plumbing/mod.rs:459-472](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L459-L472)：

```rust
} else if splitter.try_split(migrated) {
    match producer.split() {
        (left_producer, Some(right_producer)) => {
            let (reducer, left_consumer, right_consumer) =
                (consumer.to_reducer(), consumer.split_off_left(), consumer);
            // join_context 两侧递归，reducer 合并
        }
        (producer, None) => producer.fold_with(consumer.into_folder()).complete(),
    }
}
```

`match` 的两个分支就是协议的全部：分得动就并行两半，分不动就当场串行消化。

#### 4.2.4 代码实践

**实践目标**：验证「字符边界切分」不会丢字、不会切出半个字符。

**操作步骤**（以下为示例代码，新建一个依赖 `rayon` 的 Cargo 项目运行）：

```rust
use rayon::prelude::*;

fn main() {
    // 构造一个多字节字符密集的字符串（每个汉字 3 字节）
    let s: String = "汉字与rayon交错混合的字符串".repeat(1000);

    // 并行逐字符收集
    let parallel: Vec<char> = s.par_chars().collect();
    // 串行基准
    let serial: Vec<char> = s.chars().collect();

    assert_eq!(parallel, serial);
    assert_eq!(parallel.len(), s.chars().count());
    println!("ok: {} chars", parallel.len());
}
```

**需要观察的现象**：无论字符串里的多字节字符落在哪个位置，`split` 都只在字符边界下刀，两个断言都成立。

**预期结果**：打印 `ok: ... chars`，断言全部通过（待本地验证）。可以进一步把 `repeat(1000)` 换成不同数值，观察结论不变——切分正确性与字符串内容无关。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `split` 返回 `(Self, Option<Self>)`，而 `split_at` 不需要 `Option`？

**参考答案**：`split_at(index)` 的契约是 `index <= N`，在这个前提下切分**总是可行**的（包括 0 和 N 这两个退化位置），所以直接返回二元组。`split` 的语义是「尽量对半分」，而数据可能小到不可分（0/1 个元素）或结构上不可分，框架无法预知，所以把「分不动」编码为 `None`，由驱动侧回退到串行。

**练习 2**：如果把 `CharsProducer` 的切分改成「永远在字节中点 `len/2` 处切」，会发生什么？

**参考答案**：多字节 UTF-8 字符会被从中间切断，产生非法的字符串切片，`str::split_at` 在字节中点不是字符边界时会 panic。这也是字符串在 u2-l2 被归为「无索引数据源」的根本原因：它不存在廉价的「第 k 个元素」寻址，`find_char_midpoint` 的前后扫描就是为绕开这一点付出的代价。

### 4.3 producer_split_at 测试：把契约变成断言

#### 4.3.1 概念说明

`Producer` 的契约可以用一句话概括：**任意一串合法的切分之后，把各段按切分顺序串行迭代再拼接，必须严格等于原始数据**。`tests/producer_split_at.rs` 把这句话变成了机器可执行的穷举测试，约束仓库里**每一个**生产者实现（包括所有适配器生产者）。

它的聪明之处在于测试结构的选择：不是模拟 `bridge` 的中点切分（那只会覆盖对称情形），而是**三刀四段**——取任意 \( i \le j \le k \)，把数据切成 `a | b | c | d` 四段，然后断言：

1. `a.chain(b).chain(c).chain(d) == expected`（正向：切分不丢不重不乱序）；
2. `a.chain(b).chain(c).chain(d).rev() == expected 反转`（反向：每段的 `DoubleEndedIterator` 行为正确）；
3. 每段转成迭代器后 `size_hint() == (len, Some(len))` 且 `iter.len() == len`（每段的 `ExactSizeIterator` 精确反映段长）。

这三条分别对应 `Producer` trait 的三处要求：`split_at` 的顺序契约、`IntoIter: DoubleEndedIterator`、`IntoIter: ExactSizeIterator`。

#### 4.3.2 核心流程

```
check(expected, || 数据源)
  └── map_triples(N+1, |i, j, k|)          // 穷举 0 ≤ i ≤ j ≤ k ≤ N
        ├── Split::forward(f(), i, j, k)   // 正向验证
        └── Split::reverse(f(), i, j, k)   // 反向验证
              └── into_par_iter().with_producer(Split { i, j, k, reverse })
                    └── callback<P>(producer)      // 拿到裸生产者
                          ├── producer.split_at(k)         → (left, d)
                          ├── left.split_at(i)             → (a, mid)
                          ├── mid.split_at(j - i)          → (b, c)
                          ├── 四段 into_iter() + check_len
                          └── chain 收集 → 与 expected 比对
```

注意测试**完全绕开了 `bridge`**：`with_producer` 直接把裸生产者交到测试手里，想怎么切就怎么切。这是 `with_producer` 回调机制（u4-l1 讲过的 `ProducerCallback`）的另一个用途——它不只是框架的驱动入口，也是用户检验生产者的窗口。

#### 4.3.3 源码精读

**入口与穷举**。[tests/producer_split_at.rs:5-28](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/tests/producer_split_at.rs#L5-L28)：

```rust
/// Stress-test indexes for `Producer::split_at`.
fn check<F, I>(expected: &[I::Item], mut f: F)
where
    F: FnMut() -> I,
    I: IntoParallelIterator<Iter: IndexedParallelIterator, Item: PartialEq + Debug>,
{
    map_triples(expected.len() + 1, |i, j, k| {
        Split::forward(f(), i, j, k, expected);
        Split::reverse(f(), i, j, k, expected);
    });
}

fn map_triples<F>(end: usize, mut f: F)
where
    F: FnMut(usize, usize, usize),
{
    for i in 0..end {
        for j in i..end {
            for k in j..end {
                f(i, j, k);
            }
        }
    }
}
```

`check` 的约束写得很讲究：`f` 是 `FnMut() -> I`——**每次验证都重新构造一个全新的数据源**（切分按值消费了旧的那个）；`I` 的关联类型约束 `Iter: IndexedParallelIterator` 保证能走 `with_producer`。

**回调主体：三刀四段**。[tests/producer_split_at.rs:66-97](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/tests/producer_split_at.rs#L66-L97)：

```rust
impl<T> ProducerCallback<T> for Split {
    type Output = Vec<T>;

    fn callback<P>(self, producer: P) -> Self::Output
    where
        P: Producer<Item = T>,
    {
        println!("{self:?}");

        // Splitting the outer indexes first gets us an arbitrary mid section,
        // which we then split further to get full test coverage.
        let (left, d) = producer.split_at(self.k);
        let (a, mid) = left.split_at(self.i);
        let (b, c) = mid.split_at(self.j - self.i);

        let a = a.into_iter();
        let b = b.into_iter();
        let c = c.into_iter();
        let d = d.into_iter();

        check_len(&a, self.i);
        check_len(&b, self.j - self.i);
        check_len(&c, self.k - self.j);

        let chain = a.chain(b).chain(c).chain(d);
        if self.reverse {
            chain.rev().collect()
        } else {
            chain.collect()
        }
    }
}
```

三刀的顺序值得琢磨（注释也点明了）：**先切外层 `k`** 把数据分成 `left | d`，再对 `left` 切 `i` 得到 `a | mid`，最后对 `mid` 切 `j - i` 得到 `b | c`。最终四段长度是 \( (i,\ j - i,\ k - j,\ N - k) \)。虽然换一种切分顺序得到的划分集合相同，但这种「对切分产物继续切分」的写法确保测试覆盖的是**嵌套切分**——即「已经被切过一半的生产者必须还能正确地切」。这正是 `split_at` 按值返回 `Self` 所承诺的能力。

`Split::forward` / `Split::reverse`（[tests/producer_split_at.rs:39-64](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/tests/producer_split_at.rs#L39-L64)）只是把 `Split` 结构体（`i/j/k/reverse` 四个字段，[tests/producer_split_at.rs:30-36](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/tests/producer_split_at.rs#L30-L36)）塞进 `with_producer` 并比对结果，反向分支用 `result.iter().eq(expected.iter().rev())`。

**精确长度校验**。[tests/producer_split_at.rs:99-102](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/tests/producer_split_at.rs#L99-L102)：

```rust
fn check_len<I: ExactSizeIterator>(iter: &I, len: usize) {
    assert_eq!(iter.size_hint(), (len, Some(len)));
    assert_eq!(iter.len(), len);
}
```

`size_hint` 的上下界都被断言为段长——防止出现「上界是 `None`」这类松弛实现。

**被测对象清单**：文件其余部分（[tests/producer_split_at.rs:104-393](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/tests/producer_split_at.rs#L104-L393)）把仓库里所有索引数据源（`array`、`empty`、`once`、`option`、`range`、切片家族 `par_chunks/par_windows/...`、`vec`）和所有索引适配器（`chain`、`cloned`、`enumerate`、`step_by`、`interleave`、`map`、`rev`、`zip`……）逐一注册进这套骨架。**你以后给仓库贡献新的索引迭代器，配套动作就是在这里加一个测试函数**。

#### 4.3.4 代码实践

**实践目标**：亲手运行这套穷举测试，并观察其中一个数据源的全部切分组合。

**操作步骤**：

1. 只跑切片数据源的测试：

   ```bash
   cargo test -p rayon --test producer_split_at slice_iter
   ```

2. 再加上 `--nocapture` 查看 `callback` 里的 `println!("{self:?}")`：

   ```bash
   cargo test -p rayon --test producer_split_at -- --nocapture slice_iter
   ```

3. 打开 [tests/producer_split_at.rs:146-150](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/tests/producer_split_at.rs#L146-L150) 对照：`slice_iter` 用 10 个元素、期望值是 `&0..&10` 的引用切片。

**需要观察的现象**：终端滚动输出全部 `Split { i: .., j: .., k: .., reverse: .. }` 三元组，从 `i: 0, j: 0, k: 0` 开始到 `i: 10, j: 10, k: 10` 结束，`reverse` 字段在 `false`/`true` 间交替。

**预期结果**：测试通过；输出行数与 4.1.4 的估算（约 572 行，精确数待本地验证）一致。若把测试中的切片换短（自己复制一份骨架试验），穷举规模按 \( \binom{n+3}{3} \) 立方级缩减。

#### 4.3.5 小练习与答案

**练习 1**：为什么测试要穷举「全部」\( i \le j \le k \)，而不是只随机抽几个组合？

**参考答案**：切分是纯算术，错误往往只在边界组合暴露——比如 `i == j == k`（中间两段为空）、`k == N`（右段为空）、`i == 0`（最左段为空）。空段是 `split_at` 契约（`index <= N`，允许 0 和 N）最容易写错的地方：漏判会导致 panic 或丢元素。10 个元素的穷举只有几百组，成本可忽略，却能覆盖全部边界形态。

**练习 2**：`check_len` 为什么同时断言 `size_hint()` 和 `len()` 两个方法，其中一个不够吗？

**参考答案**：`ExactSizeIterator::len()` 的默认实现就是 `let (lower, upper) = self.size_hint(); upper.expect(...)`——理论上二者一致。但 `size_hint` 是 `Iterator` 的方法、允许返回松弛区间，`len` 是 `ExactSizeIterator` 的方法、承诺精确；同时断言两者等于「既要求实现者提供了精确上界（而非 `None`），又要求默认实现链路给出的长度正确」。对契约测试来说这是双保险，能区分「恰好返回了正确 len 但 size_hint 松弛」这类侥幸实现。

## 5. 综合实践：为 MyRange 实现完整的 Producer

本实践把本讲三个模块串起来：实现一个自定义区间类型 `MyRange`，走完「数据源 → `IndexedParallelIterator` → `Producer`」全链路，然后用复刻自 `tests/producer_split_at.rs` 的三刀四段骨架验证它。完成后你的 `MyRange` 将自动获得 `map`/`sum`/`collect`/`zip`/`enumerate` 等全部索引能力。

**注意**：仓库的 `tests/producer_split_at.rs` 是集成测试，只覆盖仓库自己的类型；我们不动源码，把测试骨架**复刻**到自己的项目里。

### 步骤 1：新建项目

```bash
cargo new myrange_producer
cd myrange_producer
cargo add rayon
```

### 步骤 2：实现三层结构（示例代码，写入 src/main.rs）

```rust
use rayon::iter::plumbing::*;
use rayon::prelude::*;

/// 第一层：面向用户的数据源
#[derive(Debug, Clone)]
struct MyRange {
    start: u32,
    end: u32,
}

impl IntoParallelIterator for MyRange {
    type Iter = MyRangeIter;
    type Item = u32;

    fn into_par_iter(self) -> Self::Iter {
        MyRangeIter { range: self }
    }
}

/// 第二层：并行迭代器，核心是 with_producer
#[derive(Debug, Clone)]
struct MyRangeIter {
    range: MyRange,
}

impl ParallelIterator for MyRangeIter {
    type Item = u32;

    fn drive_unindexed<C>(self, consumer: C) -> C::Result
    where
        C: UnindexedConsumer<Self::Item>,
    {
        bridge(self, consumer) // 我们有索引能力，直接走索引桥
    }

    fn opt_len(&self) -> Option<usize> {
        Some(self.len())
    }
}

impl IndexedParallelIterator for MyRangeIter {
    fn drive<C: Consumer<Self::Item>>(self, consumer: C) -> C::Result {
        bridge(self, consumer)
    }

    fn len(&self) -> usize {
        (self.range.end - self.range.start) as usize
    }

    fn with_producer<CB>(self, callback: CB) -> CB::Output
    where
        CB: ProducerCallback<Self::Item>,
    {
        // 把迭代器的状态移交生产者，交给回调
        callback.callback(MyRangeProducer {
            start: self.range.start,
            end: self.range.end,
        })
    }
}

/// 第三层：生产者，本讲的主角
struct MyRangeProducer {
    start: u32,
    end: u32,
}

impl Producer for MyRangeProducer {
    type Item = u32;
    type IntoIter = std::ops::Range<u32>; // 天生满足 Iterator + DoubleEnded + ExactSize

    fn into_iter(self) -> Self::IntoIter {
        self.start..self.end
    }

    fn split_at(self, index: usize) -> (Self, Self) {
        assert!(index <= self.len());
        let mid = self.start + index as u32;
        (
            MyRangeProducer { start: self.start, end: mid },
            MyRangeProducer { start: mid, end: self.end },
        )
    }
}

fn main() {
    let r = MyRange { start: 0, end: 10 };

    // 得益于 IndexedParallelIterator，全套适配器立即生效：
    let sum: u32 = r.clone().into_par_iter().map(|x| x * x).sum();
    assert_eq!(sum, (0u32..10).map(|x| x * x).sum());

    let collected: Vec<u32> = r.clone().into_par_iter().collect();
    assert_eq!(collected, (0..10).collect::<Vec<_>>());

    let zipped: Vec<(u32, char)> = r
        .clone()
        .into_par_iter()
        .zip(('a'..='j').into_par_iter()) // zip 也要求两侧 indexed
        .collect();
    assert_eq!(zipped.len(), 10);

    println!("all ok, current threads = {}", rayon::current_num_threads());
}
```

三个实现要点：

- `with_producer` 是**唯一**把迭代器变成生产者的入口（对照 `src/slice/mod.rs:851-856` 的写法）：迭代器把所有权拆给生产者，再调 `callback.callback(producer)`；
- `drive` / `drive_unindexed` 都直接调 `bridge(self, consumer)`——与 `MinLen` 的写法一致（[src/iter/len.rs:27-45](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/len.rs#L27-L45)）。`bridge` 会用 `self.len()` 记账并驱动 `split_at` 递归，我们一行调度代码都不用写；
- `split_at` 里 `assert!(index <= self.len())` 对齐 `src/range.rs:184-185` 的契约检查；选 `u32` 是为了让 `Range<u32>` 在所有平台上都满足 `ExactSizeIterator`。

### 步骤 3：复刻三刀四段测试（示例代码，写入 tests/check_split.rs）

注意：集成测试只能引用库代码，请先把三层结构放进 `src/lib.rs` 并给 `MyRange` 加 `pub`（`main.rs` 保留调用演示），测试文件头部这样导入：

```rust
use rayon::iter::plumbing::*;
use rayon::prelude::*;

use myrange_producer::MyRange; // crate 名按你的项目名调整
```

```rust
/// 复刻 tests/producer_split_at.rs 的回调（示例代码）
struct Split {
    i: usize,
    j: usize,
    k: usize,
    reverse: bool,
}

impl<T: Send> ProducerCallback<T> for Split {
    type Output = Vec<T>;

    fn callback<P>(self, producer: P) -> Self::Output
    where
        P: Producer<Item = T>,
    {
        let (left, d) = producer.split_at(self.k);
        let (a, mid) = left.split_at(self.i);
        let (b, c) = mid.split_at(self.j - self.i);

        let a = a.into_iter();
        let b = b.into_iter();
        let c = c.into_iter();
        let d = d.into_iter();

        assert_eq!(a.len(), self.i);
        assert_eq!(b.len(), self.j - self.i);
        assert_eq!(c.len(), self.k - self.j);

        let chain = a.chain(b).chain(c).chain(d);
        if self.reverse {
            chain.rev().collect()
        } else {
            chain.collect()
        }
    }
}

#[test]
fn my_range_split_at() {
    let expected: Vec<u32> = (0..10).collect();
    let n = expected.len() + 1;
    for i in 0..n {
        for j in i..n {
            for k in j..n {
                for reverse in [false, true] {
                    let result = MyRange { start: 0, end: 10 }
                        .into_par_iter()
                        .with_producer(Split { i, j, k, reverse });
                    let want: Vec<_> = if reverse {
                        expected.iter().rev().copied().collect()
                    } else {
                        expected.clone()
                    };
                    assert_eq!(result, want, "i={i} j={j} k={k} reverse={reverse}");
                }
            }
        }
    }
}
```

### 步骤 4：运行并观察

```bash
cargo test          # 三刀四段测试 + main 里的断言（cargo run 亦可）
cargo run           # 查看适配器演示输出
```

**预期结果**：`my_range_split_at` 通过——对全部 286 组 \( (i,j,k) \) 与正反两向，四段拼接都严格等于原序列；`main` 打印 `all ok, current threads = ...`（具体输出待本地验证）。

**失败时的排查方向**：如果断言在 `i == j == k` 或 `k == N` 的组合上失败，几乎总是 `split_at` 的边界算术问题——回头对照 `src/range.rs:184-192` 检查 `mid` 的计算与 `assert`。

### 步骤 5（选做）：把 MyRange 降级为无索引

把 `impl IndexedParallelIterator for MyRangeIter` 与 `impl Producer for MyRangeProducer` 整段注释掉，仿照 [src/range.rs:240-261](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/range.rs#L240-L261) 为 `MyRangeProducer` 改实现 `UnindexedProducer`（`split` 里 `index > 0` 才分），`drive_unindexed` 改调 `bridge_unindexed(producer, consumer)`。然后观察：

- `map`/`sum`/`for_each` 仍可用（无索引消费者照常工作）；
- `zip`、`collect_into_vec`、`with_min_len` 编译报错——它们要求 `IndexedParallelIterator`。

这正是 u2-l1 讲过的「索引能力在类型层面传播」在你自己类型上的重现。

## 6. 本讲小结

- `Producer` 的定义是「splittable `IntoIterator`」：随时可转成迭代器（`into_iter`），转换前可在指定下标二分（`split_at`）；`IntoIter` 必须同时是 `DoubleEndedIterator + ExactSizeIterator`。
- **长度留在框架侧**：生产者不携带、不查询自己的长度；`bridge` 从 `IndexedParallelIterator::len()` 取长度，每次算 \( mid = \lfloor len/2 \rfloor \)，让生产者与消费者在**同一个中点**对齐切分。
- `split_at` 是所有权操作（`self -> (Self, Self)`），通常是 O(1) 纯算术（切片切指针、范围切端点）；「包装生产者」（如 `MinLenProducer`）只改写粒度窗口 `min_len`/`max_len`，切分纯转发。
- `UnindexedProducer::split` 是「按能力切分」：框架不指定下标，数据在能力允许处切（字符串必须落在字符边界），分不动时返回 `None`，驱动侧回退串行。
- `tests/producer_split_at.rs` 用「三刀四段 + 正反两向 + 精确长度」的穷举把 Producer 契约变成断言；它通过 `with_producer` 直接操作裸生产者，是我们验证自定义生产者的现成模板。
- 实现自定义索引数据源的完整路径：数据源 → `IndexedParallelIterator`（`len` + `with_producer` + `drive` 调 `bridge`）→ `Producer`（`split_at` + `into_iter`），之后全套索引适配器自动生效。

## 7. 下一步学习建议

下一讲 **u4-l3「Consumer 与驱动流程」**是本讲的镜像：`split_at` 的另一半——消费者如何切分、`Folder` 如何逐元素消化、`for_each` 从用户调用到 plumbing 的完整调用链。学完后，「同一中点切两侧」的另一半图景就完整了。

在进入下一讲之前，推荐两个热身阅读：

1. [src/iter/zip.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/zip.rs) 中的 `ZipProducer`——它持有两个内部生产者，`split_at` 时**让两侧在同一个 `index` 上各切一刀**，是「组合生产者」的典型样本（u3-l3 从用户侧讲过 zip，现在可以读实现了）。
2. 回头精读 [src/iter/plumbing/README.md:16-31](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/README.md#L16-L31) 关于「任何生产者都能驱动无索引消费者，只有索引生产者能驱动索引消费者」的论述，结合本讲 5.3 的「降级实验」加深理解。
