# 有索引适配器与长度控制

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `zip`、`enumerate`、`rev`、`skip`、`take`、`step_by` 这六个适配器**为什么**只能用于 `IndexedParallelIterator`，而不像 `map`/`filter` 那样对任意 `ParallelIterator` 开放。
2. 读懂每个适配器的 `Producer::split_at` 实现：它们本质上都是在「元素下标」上做算术。
3. 使用 `with_min_len` / `with_max_len` 控制任务切分粒度，理解粒度如何影响任务数量与总开销，并能实测三种粒度下的耗时差异。

本讲承接近的脉络：u2-l1 已经建立了「索引能力在类型层面传播」的结论（`map` 保留索引、`filter` 丢失索引），u3-l1 已经解剖过 map/filter 这类「逐元素变换」适配器的骨架。本讲专门研究**依赖位置信息**的适配器——它们是索引能力最直接的受益者。

## 2. 前置知识

- **IndexedParallelIterator**：`ParallelIterator` 的子 trait，额外承诺「长度已知（`len()`）且可按下标切分（`with_producer` → `Producer::split_at`）」。没有这两个信息，`zip` 这类操作无从谈起。
- **Producer（生产者）**：迭代器交出的「可切分数据源」。契约是：`split_at(index)` 把自己拆成产出 `0..index` 的左半与产出 `index..N` 的右半两份。这是 plumbing 层的核心抽象之一（详见 [src/iter/plumbing/mod.rs:95-97](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L95-L97)）。
- **bridge（桥接）**：把带索引的迭代器与消费者接起来的驱动函数，内部递归地「切生产者 + 切消费者 + `join_context` 两半」，直到切分策略说「够了」，然后串行消费。
- **任务粒度（granularity）**：每个并行任务最终负责的元素个数。粒度太细 → 任务数暴涨，调度与同步开销超过计算本身；粒度太粗 → 线程负载不均。`with_min_len` / `with_max_len` 就是调节这个旋钮的官方入口。
- **工作窃取与切分预算**：rayon 的切分不是一次做完的，而是「边执行边切」——当前线程执行左半，右半入队等待，可能被别的线程偷走。被偷过（stolen）的任务会重置切分预算，继续细分。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/iter/zip.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/zip.rs) | `zip` 适配器与 `ZipProducer`：双侧同下标切分 |
| [src/iter/zip_eq.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/zip_eq.rs) | `zip_eq`：长度必须相等的 zip，纯委托 `Zip` |
| [src/iter/enumerate.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/enumerate.rs) | `enumerate` 适配器与 `EnumerateProducer`：切分时维护 offset |
| [src/iter/rev.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/rev.rs) | `rev` 适配器：镜像下标切分 |
| [src/iter/skip.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/skip.rs) | `skip` 适配器：跳过前 n 个（被跳过部分仍要跑副作用） |
| [src/iter/take.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/take.rs) | `take` 适配器：切一刀丢右半 |
| [src/iter/step_by.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/step_by.rs) | `step_by` 适配器：在「步长空间」与「元素空间」之间换算 |
| [src/iter/len.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/len.rs) | `MinLen` / `MaxLen` 适配器：本讲的粒度控制旋钮 |
| [src/iter/mod.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs) | 上述方法在 `IndexedParallelIterator` 上的定义（L2584 起的 `zip` 到 L3220 的 `len`） |
| [src/iter/plumbing/mod.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs) | `Producer` trait、`Splitter` / `LengthSplitter` 切分策略、`bridge` 递归 |

> 注意：这些方法全部定义在 `IndexedParallelIterator` 的 impl 块中，而不是 `ParallelIterator` 上——这就是「必须有索引」在 API 层面的直接体现（对照 [src/iter/mod.rs:2917-2934](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L2917-L2934) 的 `enumerate` 与 [src/iter/mod.rs:3148-3150](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L3148-L3150) 的 `rev`）。

## 4. 核心概念与源码讲解

### 4.1 zip 与 enumerate：为什么必须「有索引」

#### 4.1.1 概念说明

`zip` 要把两个迭代器「按位置配对」。串行世界（`std::iter::Zip`）只需要拉着一个走到头；但并行世界必须先回答一个问题：**在哪一刀切开？** 切开之后，左侧生产者产出 `0..mid` 的元素，那配对的另一半也必须是 `0..mid`——两侧必须在**同一个下标**上切分。这就要求两侧都提供 `len()` 与 `split_at`，也就是 `IndexedParallelIterator`。这也是为什么 `zip` 的入参约束写的是 `Z: IntoParallelIterator<Iter: IndexedParallelIterator>`（[src/iter/mod.rs:2584-2589](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L2584-L2589)）。

`enumerate` 同理：它给每个元素配上它在**全局序列中的下标**。切分之后右半的第一个元素下标不是 0 而是 `切分点`，这个偏移量必须在切分时被计算并携带。

#### 4.1.2 核心流程

`Zip` 的执行流程：

```text
zip(a, b)
  └─ bridge(self, consumer)              # drive / drive_unindexed 都走 bridge
       └─ with_producer(回调)
            ├─ a.with_producer(CallbackA)   # 先取出 a 的生产者
            │    └─ b.with_producer(CallbackB)  # 再取出 b 的生产者
            │         └─ 组装 ZipProducer { a, b }
            └─ bridge_producer_consumer(len, ZipProducer, consumer)
                 └─ 递归：splitter 说还能切？
                      ├─ 是 → mid = len/2
                      │       ZipProducer.split_at(mid)：a、b 在同一 mid 切开
                      │       consumer.split_at(mid) 同步切开
                      │       join_context(左半, 右半)   ← 工作窃取发生处
                      └─ 否 → 生产者顺序产出，消费者串行吃进
```

关键不变量：**任意时刻 ZipProducer 的 a、b 两半长度相等**（对 `zip_eq` 而言恒等；对普通 `zip`，先把两侧按 `min(len)` 对齐——见下面的 `take`/`skip` 分析）。

#### 4.1.3 源码精读

**长度取两侧最小值**——[src/iter/zip.rs:54-56](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/zip.rs#L54-L56)：`zip` 的 `len()` 是 `Ord::min(self.a.len(), self.b.len())`，与标准库 `Zip` 语义一致：多出的部分被忽略。

```rust
fn len(&self) -> usize {
    Ord::min(self.a.len(), self.b.len())
}
```

**双层回调取出两个生产者**——[src/iter/zip.rs:58-112](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/zip.rs#L58-L112)：`with_producer` 是一个「把生产者交给你」的回调协议。`Zip` 需要**两个**生产者，于是嵌套了两层回调：`CallbackA` 先拿到 a 的生产者，再发起 b 的 `with_producer`，`CallbackB` 拿到 b 的生产者后把两者打包成 `ZipProducer` 交给最终回调。这是 rayon 里「组合多个索引迭代器」的标准手法，`interleave`、`zip_eq` 等都复用或委托它。

**同一个下标切两侧**——[src/iter/zip.rs:138-151](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/zip.rs#L138-L151)：

```rust
fn split_at(self, index: usize) -> (Self, Self) {
    let (a_left, a_right) = self.a.split_at(index);
    let (b_left, b_right) = self.b.split_at(index);
    // 重新组装成左右两个 ZipProducer
}
```

这一段就是 4.1.1 说的不变量的落地：`index` 同时喂给 a、b 两个生产者。

**min_len 取大、max_len 取小**——[src/iter/zip.rs:130-136](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/zip.rs#L130-L136)：`ZipProducer::min_len` 取两侧 min_len 的**最大值**，`max_len` 取两侧 max_len 的**最小值**——即取两个生产者「都愿意接受」的切分窗口的交集。文档也明确说了这一点（[src/iter/mod.rs:3156-3159](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L3156-L3159)）。

**enumerate 的 offset**——[src/iter/enumerate.rs:114-126](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/enumerate.rs#L114-L126)：

```rust
fn split_at(self, index: usize) -> (Self, Self) {
    let (left, right) = self.base.split_at(index);
    (
        EnumerateProducer { base: left,  offset: self.offset },
        EnumerateProducer { base: right, offset: self.offset + index },
    )
}
```

左半保持原偏移，右半偏移加上切分点。初始 offset 为 0（[src/iter/enumerate.rs:71](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/enumerate.rs#L71)）。任务真正执行时生产器转成 `(offset..end).zip(base)` 这个普通迭代器（[src/iter/enumerate.rs:93-105](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/enumerate.rs#L93-L105)），注释里还解释了为什么范围终点要用精确值：否则 `rev()` 之后从尾部回退要走很久。

**zip_eq：断言 + 委托**——`ZipEq` 内部就是一个 `Zip`（[src/iter/zip_eq.rs:12-14](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/zip_eq.rs#L12-L14)），所有方法原样转发（[src/iter/zip_eq.rs:60-65](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/zip_eq.rs#L60-L65)）。等长检查发生在**构造时**而非消费时：`assert_eq!(self.len(), zip_op_iter.len(), "iterators must have the same length")`（[src/iter/mod.rs:2610-2622](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L2610-L2622)）。

#### 4.1.4 代码实践

**实践目标**：验证 `zip` 的长度截断语义与 `enumerate` 的偏移正确性，亲眼看到「zip 长度 = min」。

**操作步骤**（示例代码，独立 Cargo 工程，依赖 `rayon = "1"`）：

```rust
use rayon::prelude::*;

fn main() {
    // 1. zip 长度截断
    let a: Vec<i32> = (0..100).collect();
    let b: Vec<i32> = vec![7; 10];
    let z: Vec<_> = a.par_iter().zip(&b).collect();
    println!("zip len = {}", z.len()); // 期待 10
    println!("first = {:?}, last = {:?}", z.first(), z.last());

    // 2. enumerate 下标在切分后仍正确
    let e: Vec<_> = (0..1000).into_par_iter().enumerate().collect();
    assert!(e.iter().all(|(i, v)| i == v)); // 元素值 == 全局下标

    // 3. zip_eq 不等长立即 panic（构造期断言）
    let r: Vec<_> = std::panic::catch_unwind(|| {
        rayon::scope(|s| {
            s.spawn(|_| {
                let _ = a.par_iter().zip_eq(&b).collect::<Vec<_>>();
            });
        });
    });
    println!("zip_eq panicked? {}", r.is_err());
}
```

**需要观察的现象**：`zip len = 10`；`enumerate` 断言通过（说明无论切成多少段，`(i, v)` 的 i 恰是全局下标）；`zip_eq panicked? true`。

**预期结果**：与上述一致。即便把第 2 步加上 `.with_max_len(8)` 强制切成上百段，断言依然成立——offset 由 `split_at` 自动维护，这正是索引适配器的价值。具体运行数值待本地验证。

#### 4.1.5 小练习与答案

1. **问**：如果把 `zip` 的泛型约束从 `IndexedParallelIterator` 放宽为 `ParallelIterator`，最先坏掉的是哪段代码？
   **答**：[zip.rs 的 split_at（L138-151）](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/zip.rs#L138-L151)。它必须对 a、b 调用 `Producer::split_at(index)`，而 `Producer` 只能从 `IndexedParallelIterator::with_producer` 拿到；无索引迭代器只有 `UnindexedProducer::split()`，切分点由数据自己决定，无法保证两侧对齐。
2. **问**：`(0..100).into_par_iter().zip(vec![0; 10])` 的 `len()` 是多少？为什么文档示例（[mod.rs:3214-3215](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L3214-L3215)）敢断言它 collect 出 10 个元素？
   **答**：10。因为 `Zip::len()` 取 `Ord::min(100, 10)`，且切分不变量保证两侧永远等长消费，多出的 90 个元素根本不会被产出。
3. **问**：`zip_eq` 在什么时机检查长度？这带来什么好处？
   **答**：在 `zip_eq()` 方法被调用的瞬间用 `assert_eq!` 检查（[mod.rs:2616-2620](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L2616-L2620)），并标了 `#[track_caller]` 让 panic 指向用户代码。好处：错误尽早暴露、栈指向调用点，而不是等到某个并行任务执行到一半才炸出难以定位的越界。

### 4.2 skip、take、step_by、rev：在下标上做算术

#### 4.2.1 概念说明

如果说 `zip` 证明「索引是并行配对的前提」，那这一组适配器展示的是索引的另一面：**知道了长度和切分点，很多「看起来需要顺序执行」的操作都能变成纯下标运算**。

- `take(n)`：只要前 n 个 → 在 `n` 处切一刀，**丢掉右半**。
- `skip(n)`：不要前 n 个 → 在 `n` 处切一刀，**丢掉左半**（但左半的副作用仍要执行，见下）。
- `step_by(k)`：每 k 个取一个 → 把「产出空间」的下标乘以 k 换算成「底层元素空间」的下标。
- `rev()`：倒序 → 把下标 `i` 镜像成 `len - i`。

它们全都保留 `IndexedParallelIterator`（长度要么截断、要么做除法，仍然精确可知），所以可以继续接 `zip`、`collect` 快速路径等。

#### 4.2.2 核心流程

以 `step_by` 为例的换算关系。设底层长度为 \(N\)、步长为 \(k\)，则产出个数（即 `len()`）为：

\[
\text{len} = \left\lceil N / k \right\rceil
\]

当切分策略要求在产出下标 \(i\) 处切开时，需要先换算到底层元素下标：

\[
\text{elem\_index} = \min(i \times k,\ N)
\]

于是左半是「下标 `0..i*k` 的底层元素、步长 k」，右半从 `i*k` 继续以步长 k 产出——由于 `i*k` 恰是 k 的倍数，两半各自的产出仍然落在全局正确的格点上。`min` 是防止越界的保护。

`rev` 的镜像则更简单：在产出下标 `i` 切开，等价于在底层下标 `len - i` 切开，然后**左右互换**。

#### 4.2.3 源码精读

**take：构造时钳制 + 一刀丢右半**——[src/iter/take.rs:19-22](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/take.rs#L19-L22) 先把 `n` 钳制到 `base.len()`（所以 `take(99999)` 不 panic、只是「全要」）；[src/iter/take.rs:78-80](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/take.rs#L78-L80)：

```rust
let (producer, _) = base.split_at(self.n);
self.callback.callback(producer)
```

拿到左半直接交给下游，右半用一个 `_` 扔掉——对有索引生产者来说「取前 n 个」就是一次 `split_at`。

**skip：被跳过的部分仍要「跑一遍」**——[src/iter/skip.rs:79-88](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/skip.rs#L79-L88)：

```rust
crate::in_place_scope(|scope| {
    let (before_skip, after_skip) = base.split_at(n);
    // Run the skipped part separately for side effects.
    // We'll still get any panics propagated back by the scope.
    scope.spawn(move |_| bridge_producer_consumer(n, before_skip, NoopConsumer));
    callback.callback(after_skip)
})
```

这是本组最有意思的实现：rayon **没有**把前 n 个元素直接丢弃，而是把「跳过段」作为一个独立任务交给线程池，用 `NoopConsumer`（把元素统统丢进黑洞的消费者）消费。为什么？源码注释给了答案：**副作用与 panic**。如果上游适配器（比如 `inspect` 或用户的 `map` 闭包）在被跳过的元素上会写日志、发信号，或者会 panic，直接跳过会改变可观察行为。注意 `skip` 的语义是跳过**元素产出**，不是跳过**上游计算**——如果上游计算本身有副作用，`skip` 不会帮你省掉它（想省开销应在上游用 `take`/`skip` 作用于数据源本身，或改写管道）。

**rev：镜像切分**——[src/iter/rev.rs:105-117](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/rev.rs#L105-L117)：

```rust
fn split_at(self, index: usize) -> (Self, Self) {
    let (left, right) = self.base.split_at(self.len - index);
    (
        RevProducer { base: right, len: index },          // 反转后左半 = 原来的右半
        RevProducer { base: left,  len: self.len - index },
    )
}
```

在 `self.len - index` 处切底层，再交换左右，配上的新 `len` 分别是 `index` 与 `self.len - index`。任务内执行时则是普通的 `base.into_iter().rev()`（[src/iter/rev.rs:94-96](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/rev.rs#L94-L96)）——这就是 `Producer::IntoIter` 要求 `DoubleEndedIterator` 的原因之一。

**step_by：两套坐标系换算**——切分见 [src/iter/step_by.rs:109-125](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/step_by.rs#L109-L125)（即 4.2.2 的公式落地，`elem_index = Ord::min(index * self.step, self.len)`）；长度见 [src/iter/step_by.rs:48-50](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/step_by.rs#L48-L50)（`div_ceil`）；粒度窗口同样要换算——[src/iter/step_by.rs:127-133](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/step_by.rs#L127-L133)：

```rust
fn min_len(&self) -> usize {
    self.base.min_len().div_ceil(self.step)
}
fn max_len(&self) -> usize {
    self.base.max_len() / self.step
}
```

底层「每段至少 min_len 个元素」翻译到产出空间要除以步长；max 方向同理（向下取整保守处理）。

**len 同样是纯算术**——`skip` 的 `len = base.len() - n`（[src/iter/skip.rs:48-50](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/skip.rs#L48-L50)，`n` 已在构造时钳制故不会下溢）、`take` 的 `len = n`（[src/iter/take.rs:47-49](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/take.rs#L47-L49)）、`rev` 的 `len = base.len()`（[src/iter/rev.rs:47-49](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/rev.rs#L47-L49)）。

#### 4.2.4 代码实践

**实践目标**：用串行迭代器做「金标准」，验证四个适配器在并行切分下产出与串行完全一致；并观察 `skip` 的副作用语义。

**操作步骤**（示例代码）：

```rust
use rayon::prelude::*;

fn main() {
    let n = 100;
    // 强制切成很多小段，检验切分正确性
    let tiny = |it| it.with_max_len(3);

    // take / skip / step_by / rev 与串行对照
    let serial_take: Vec<i32>     = (0..n).take(7).collect();
    let parallel_take: Vec<i32>   = tiny((0..n).into_par_iter()).take(7).collect();
    assert_eq!(serial_take, parallel_take);

    let serial_skip: Vec<i32>     = (0..n).skip(90).collect();
    let parallel_skip: Vec<i32>   = tiny((0..n).into_par_iter()).skip(90).collect();
    assert_eq!(serial_skip, parallel_skip);

    let serial_step: Vec<i32>     = (0..n).step_by(7).collect();
    let parallel_step: Vec<i32>   = tiny((0..n).into_par_iter()).step_by(7).collect();
    assert_eq!(serial_step, parallel_step);

    let serial_rev: Vec<i32>      = (0..n).rev().collect();
    let parallel_rev: Vec<i32>    = tiny((0..n).into_par_iter()).rev().collect();
    assert_eq!(serial_rev, parallel_rev);
    println!("all consistent");

    // skip 不跳过副作用：inspect 会对 0..=89 全部执行
    let mut seen = 0usize;
    (0..100).into_par_iter()
        .inspect(|_| { seen += 1; })       // 注意：非原子计数仅为演示
        .skip(90)
        .count();
    println!("inspect executed for {} elements (expect 100)", seen);
}
```

**需要观察的现象**：四组 `assert_eq!` 全部通过——即使 `with_max_len(3)` 把底层切成三十多段，take/skip/step_by/rev 的产出顺序与串行逐一相等；最后一段打印 `100` 而不是 `10`。

**预期结果**：如上。若把 `inspect(|_| seen += 1)` 换成 `std::sync::atomic::AtomicUsize` 计数更严谨（`fetch_add`）。副作用计数为 100 正是 4.2.3 说的「skip 不省上游计算」的证据。具体数值待本地验证。

#### 4.2.5 小练习与答案

1. **问**：`(0..100).into_par_iter().take(99999)` 会 panic 吗？`len()` 是多少？
   **答**：不会。`Take::new` 里 `n = Ord::min(base.len(), n)` 把 99999 钳制成 100（[take.rs:19-22](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/take.rs#L19-L22)），随后 `len()` 返回钳制后的 `n`，即 100。
2. **问**：`skip` 为什么不像 `take` 那样直接把不要的那半扔掉，而要 spawn 一个 `NoopConsumer` 任务？
   **答**：为了保持可观察行为：上游元素仍会被完整「消费」，`inspect`/`map` 的副作用照常发生，panic 也会经 scope 传播回调用点（[skip.rs:83-85](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/skip.rs#L83-L85) 的注释原文）。直接丢弃会让 `x.par_iter().inspect(f).skip(k)` 与串行版本行为不一致。
3. **问**：`rev` 之后接 `enumerate`，下标是 0 起始的还是倒着的？
   **答**：0 起始。`enumerate` 的 offset 从 0 开始、跟的是**它自己这一层**的产出序列（反转后的顺序），与底层顺序无关。`(0..5).into_par_iter().rev().enumerate()` 产出 `(0,4), (1,3), (2,2), (3,1), (4,0)`。

### 4.3 with_min_len / with_max_len：控制切分粒度

#### 4.3.1 概念说明

前两节的所有切分都由一套**自适应策略**驱动：它想保证「大致每个线程一个任务」，并在任务被窃取后追加切分。这套策略对多数负载够用，但当你的每个元素处理成本极低（如纯求和）或极高（如昂贵的 IO），默认粒度可能不划算：

- 元素成本极低 → 任务切得太细，调度开销（创建 Job、入队、窃取、归并）反而成为大头；
- 元素成本极高且不均 → 任务切得太粗，某线程抱着一个大任务不放，其他线程围观。

`with_min_len(min)` 与 `with_max_len(max)`（[src/iter/mod.rs:3174-3176](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L3174-L3176) 与 [src/iter/mod.rs:3202-3204](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L3202-L3204)）就是给这个策略加两条硬边界：

- `min`：**下界**。切到「每半长度 < min」就停，宁可少并行也不再切；
- `max`：**上界**。强制至少切到「每段 ≤ max」，保证任务足够多。

两者可同时使用；`min` 优先级更高——文档举的例子：min=10、max=15 时，长度 16 不会再切（因为切成 8+8 违反 min），见 [src/iter/mod.rs:3178-3182](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L3178-L3182)。

#### 4.3.2 核心流程

粒度控制的完整链路是：**适配器把 min/max 写进 Producer → bridge 读取 → LengthSplitter 决定每一刀切不切**。

`Producer` trait 自带两个带默认值的粒度方法（[src/iter/plumbing/mod.rs:68-93](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L68-L93)）：`min_len()` 默认 1（可一路切到单元素），`max_len()` 默认 `usize::MAX`（可以完全不切）。`with_min_len` / `with_max_len` 做的事，只是包一层 Producer 去改写这两个返回值。

策略本体是 `LengthSplitter`，它包裹了自适应的 `Splitter`：

```text
bridge_producer_consumer(len, producer, consumer):
  splitter = LengthSplitter::new(producer.min_len(), producer.max_len(), len)
  helper(len, stolen=false, splitter, producer, consumer):
    若 consumer.full() → 短路完成
    若 splitter.try_split(len, stolen)：
        mid = len / 2
        (左生产者, 右生产者) = producer.split_at(mid)
        (左消费者, 右消费者, reducer) = consumer.split_at(mid)
        join_context(递归左半, 递归右半)      # 右半可被窃取
        reducer.reduce(左结果, 右结果)
    否则：
        producer.fold_with(consumer.into_folder()).complete()   # 串行吃完
```

`try_split` 只有一条判断（[src/iter/plumbing/mod.rs:328-332](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L328-L332)）：

\[
\text{try\_split}(len) \iff len / 2 \ge min \;\wedge\; \text{inner.try\_split}(stolen)
\]

`Splitter::new` 的切分预算初始为线程数（[src/iter/plumbing/mod.rs:258-264](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L258-L264)），每切一刀预算减半，归零即停——所以不设任何参数时任务数与线程数同一量级。而 `LengthSplitter::new` 用 `max` 计算强制切分下限（[src/iter/plumbing/mod.rs:308-326](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L308-L326)）：

\[
\text{min\_splits} = \lfloor len / \max(max, 1) \rfloor
\]

若 `min_splits` 超过现有预算则抬高预算。由于预算逐层减半，实际任务数会取到不小于它的下一个 2 的幂——源码注释的原例：len 12345、max 100 → min_splits 123 → 实际 128 份。窃取会再次抬高预算（`stolen` 分支把预算重置为 `max(current_num_threads(), splits/2)`，[src/iter/plumbing/mod.rs:267-284](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L267-L284)），所以粒度上限（min_len）才是真正的硬保证，任务只会更多不会更少。

#### 4.3.3 源码精读

**MinLen / MaxLen 适配器本体**——[src/iter/len.rs:9-19](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/len.rs#L9-L19) 与 [src/iter/len.rs:139-149](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/len.rs#L139-L149)：与 map 一样是「包一层」的惰性适配器，`len()` 原样转发（[len.rs:47-49](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/len.rs#L47-L49)），它们**不改元素、不改长度，只改切分行为**。

**改写粒度窗口的 Producer**——[src/iter/len.rs:103-109](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/len.rs#L103-L109)：

```rust
fn min_len(&self) -> usize {
    Ord::max(self.min, self.base.min_len())   // 只能抬高，不会放松底层约束
}
fn max_len(&self) -> usize {
    self.base.max_len()
}
```

`MaxLenProducer` 对称（[src/iter/len.rs:233-239](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/len.rs#L233-L239)：`max_len` 取 `Ord::min`，`min_len` 原样转发）。两者的 `split_at` 只是原样转发并复制参数（[len.rs:111-123](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/len.rs#L111-L123)），`fold_with` 也直接委托底层（[len.rs:125-130](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/len.rs#L125-L130)）——它们对切分点本身毫无意见，只是给决策者递了话。

**消费这两条边界的决策者**——[src/iter/plumbing/mod.rs:308-332](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L308-L332)。`min` 先做 `Ord::max(min, 1)` 防止用户传 0；`try_split` 的 `len / 2 >= self.min` 就是 min_len 的全部执行力。

**递归驱动**——[src/iter/plumbing/mod.rs:385-434](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L385-L434)：`bridge_producer_consumer` 构造 splitter 后进入 `helper` 递归；`mid = len / 2` 的整除意味着段长分布是 2 的幂附近的值；不切的那一支走 `producer.fold_with(consumer.into_folder()).complete()` 串行收尾。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：对 `0..10_000_000` 求和，实测三种切分粒度的耗时差异，理解「任务切分开销」不是玄学；同时用 `fold` 直接数出每种配置下真正产生了多少个任务。

**操作步骤**（示例代码，独立工程；务必用 `cargo run --release`，debug 模式的数值没有参考价值）：

```rust
use rayon::prelude::*;
use std::time::Instant;

const N: u64 = 10_000_000;

fn main() {
    // 四个待测管道：仅粒度不同（default 与 with_min_len(1) 理论上等价）
    let cases: &[(&str, fn() -> u64)] = &[
        ("default",               || (0..N).into_par_iter().sum::<u64>()),
        ("with_min_len(1)",       || (0..N).into_par_iter().with_min_len(1).sum::<u64>()),
        ("with_min_len(1_000_000)", || (0..N).into_par_iter().with_min_len(1_000_000).sum::<u64>()),
        ("with_max_len(100)",     || (0..N).into_par_iter().with_max_len(100).sum::<u64>()),
    ];

    for &(name, f) in cases {
        f(); // 预热线程池，排除首次启动开销
        let start = Instant::now();
        let s = f();
        println!("{name:28} sum={s} time={:?}", start.elapsed());
    }

    // 数任务：fold 每段产出"该段元素个数"，collect 后即得每段长度清单
    let segment_report = |name: &str, lens: Vec<u64>| {
        println!("{name}: {} 段, 最小段 {}, 最大段 {}",
                 lens.len(), lens.iter().min().unwrap(), lens.iter().max().unwrap());
    };
    segment_report("default",
        (0..N).into_par_iter().fold(|| 0u64, |acc, _| acc + 1).collect());
    segment_report("with_max_len(100)",
        (0..N).into_par_iter().with_max_len(100).fold(|| 0u64, |acc, _| acc + 1).collect());
}
```

**需要观察的现象**：

1. `default` 与 `with_min_len(1)` 耗时应几乎相同——因为 `Producer::min_len()` 的默认值本来就是 1（[plumbing/mod.rs:78-80](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L78-L80)），`LengthSplitter::new` 还会再做一次 `Ord::max(min, 1)`；
2. `with_max_len(100)` 的段数清单约为 131072 段（\(10^7/100 = 10^5\) 向上取 2 的幂 \(= 2^{17}\)），单元素只做一次加法，任务创建/调度/归并的开销远超计算本身，耗时显著变长；
3. `with_min_len(1_000_000)` 强制最多切成十来段，对这种轻量求和通常最快，与 default 接近；
4. 四种配置的 `sum` 完全一致（应为 49999995000000），粒度只影响性能不影响结果。

**预期结果**：一张「配置 → 段数 → 耗时」的对照表，典型形态是 default ≈ with_min_len(1) ≈ with_min_len(10^6) ≪ with_max_len(100)（耗时上后者可能是前者的数倍）。把这个表画成柱状图即可。具体毫秒数与机器核数强相关，待本地验证；若结果与预期形态不符，优先检查是否忘了 `--release`、是否在共享 CI 机器上跑（噪声大）。

#### 4.3.5 小练习与答案

1. **问**：`with_min_len(1)` 与不调用它有区别吗？
   **答**：没有。`min_len` 默认就是 1，`LengthSplitter::new` 里还会 `Ord::max(min, 1)` 兜底（[plumbing/mod.rs:311](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L311)）。想做「更粗」的实验应传远大于 1 的值。
2. **问**：`with_min_len(10).with_max_len(15)` 作用在长度 16 的迭代器上，会切成两段 8+8 吗？
   **答**：不会。`try_split` 要求 `len / 2 >= min`，而 16/2 = 8 < 10，直接拒绝切分，整段串行处理。这正是文档（[mod.rs:3178-3182](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L3178-L3182)）举的例子：min 优先于 max。
3. **问**：为什么说「min_len 是硬保证，max_len 不是」？
   **答**：min_len 直接出现在 `try_split` 的否决条件里，任何切分都不能越过后在窃取中放松；而 max_len 只在 `LengthSplitter::new` 时一次性折算成初始切分预算，之后 `Splitter::try_split` 的 stolen 分支（[plumbing/mod.rs:270-274](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L270-L274)）会在任务被窃取时重置/抬高预算，任务可能比 `next_power_of_two(len/max)` 更多——方向上只多不少，仍满足「至少切到 max 以下」的承诺。

## 5. 综合实践

把本讲三块内容串成一个**并行点积（dot product）调优**任务：

1. **实现基线**：`a.par_iter().zip(b).map(|(x, y)| x * y).sum::<i64>()`，其中 `a`、`b` 是各 1000 万个 `i32` 的随机向量（`zip` 依赖两侧索引对齐，正好用上 4.1 的知识）。用串行 `a.iter().zip(b).map(...).sum()` 验证结果一致。
2. **加位置信息**：改用 `a.par_iter().enumerate()` 版本，在 `map` 里断言 `i` 与消费到的位置一致（抽样断言即可），确认 `enumerate` 的 offset 在切分后依然正确。
3. **粒度扫描**：对 `zip` 管道分别套 `with_min_len(1)`、默认、`with_min_len(100_000)`、`with_max_len(1024)`、`with_max_len(16)` 五种配置，`--release` 下各跑 5 次取中位数，记录耗时与 `fold` 段数（方法见 4.3.4）。
4. **分析**：画出「段数—耗时」曲线，找到本机最优点，回答两个问题：(a) 每元素成本约 1ns 级的乘加，任务开销在多少段时开始主导？(b) 把元素计算改重（`map` 里加 100 次循环），最优点向哪个方向移动？为什么？

**验收标准**：所有配置结果相同且等于串行值；能用自己的话解释「粒度太细伤在调度、太粗伤在负载均衡」，并指出 `min_len`/`max_len` 分别防的是哪一头。

## 6. 本讲小结

- `zip`/`zip_eq`/`enumerate`/`rev`/`skip`/`take`/`step_by` 只对 `IndexedParallelIterator` 开放，根源是它们需要**在同一坐标系里讨论下标**：要么两侧同点切分（zip），要么切分后维护偏移（enumerate），要么对下标做镜像/截断/倍乘（rev/skip/take/step_by）。
- 这些适配器的 `Producer::split_at` 全是几行下标算术：take 丢右半、skip 丢左半（但左半副作用照跑）、rev 取 `len - index` 且左右互换、step_by 把产出下标乘以步长换算回元素下标。
- 粒度由两层共同决定：`Producer::min_len()/max_len()`（默认 1 / `usize::MAX`）给出窗口，`LengthSplitter` 在 `bridge` 递归里逐刀询问 `len/2 >= min` 且预算未耗尽才切。
- `with_min_len` 是硬下界（阻止过切），`with_max_len` 通过抬高初始切分预算强制细分（只多不少）；`zip` 这类组合生产者取两侧窗口的交集。
- 粒度只影响性能不影响结果——四种配置的求和值完全一致，切分协议（`split_at` 契约 + offset 维护）保证了这一点。

## 7. 下一步学习建议

- **顺序无关操作**：下一讲 u3-l4 讲 `find_any`/`take_any`/`interleave`，其中 `interleave` 同样是索引适配器，可与本讲 `zip` 对照阅读；`find_any` 则展示「放弃顺序换性能」的另一条路线。
- **深入 plumbing**：如果 `with_producer` 双层回调、`bridge` 递归、`fold_with` 这些词还似懂非懂，进入 u4-l1（plumbing 总览）与 u4-l2（Producer 契约），那里会完整拆解 Producer/Consumer/Folder 三者如何咬合。
- **性能维度**：u9-l3 会用 rayon-demo 的 matmul/quicksort 等基准系统性讨论粒度与缓存对加速比的影响，可作为本讲综合实践的延伸。
- **顺带一提**：`positions`、`interleave_shortest`（[src/iter/mod.rs:2638-2661](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L2638-L2661)）也是索引家族成员，读完本讲后可直接按同样方法自行拆读这两个文件。
