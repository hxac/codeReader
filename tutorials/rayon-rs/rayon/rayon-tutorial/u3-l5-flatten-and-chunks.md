# 展平与分块：flat_map 家族与 chunks/blocks 分块模式

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `flat_map`/`flatten` 与 `flat_map_iter`/`flatten_iter` 这两组「展平」适配器在**并行深度**上的本质区别：前者嵌套并行（内层还会再切分），后者只在内外层之间并行、内层严格串行。
2. 读懂 `FlatMapFolder::consume` 如何用 `split_off_left` + `drive_unindexed` 实现嵌套并行，以及 `FlatMapIterFolder` 如何用 `consume_iter` 把内层串行迭代器逐元素灌给下游。
3. 掌握三种「分块」处理模式：`chunks`（产出 `Vec`，有分配开销）、`fold_chunks`/`fold_chunks_with`（块内串行 fold，零分配）、`by_exponential_blocks`/`by_uniform_blocks`（不改变元素，只改变调度顺序）。
4. 理解 `ChunkProducer` 作为公共底座如何同时服务 `chunks` 与 `fold_chunks` 家族——只差一个把「块的顺序迭代器」变成「产出值」的闭包。

## 2. 前置知识

本讲建立在单元三前几讲的基础上，先回顾几个会反复用到的概念：

- **惰性适配器与立即执行消费者**（u2-l1、u3-l1）：`flat_map`、`flatten`、`chunks`、`fold_chunks` 都是惰性适配器——调用它们只做一层包装，真正的并行执行由 `sum`/`collect`/`for_each` 等消费者触发。
- **plumbing 三角色**（u3-l1）：`Producer` 生产数据，`Consumer` 消费数据，`Folder` 是消费者在某个任务内的「工作状态」，核心方法是 `consume`（吃一个元素）、`complete`（交出结果）、`full`（是否可以提前收工）。
- **包装消费者模式**（u3-l1）：适配器不改数据源，而是把下游消费者包一层再转发给上游。本讲的 `FlatMapConsumer`、`FlattenConsumer` 都是这一模式的后代，但它们的 `Folder` 会做出与前几讲截然不同的动作——**递归进入内层并行执行**。
- **`UnindexedConsumer::split_off_left`**（u2-l1）：无索引消费者可以把自己「分裂出左半份」，返回一个可用于消费一段数据的同型副本，配合 `to_reducer` 归并各段结果。本讲会看到它在嵌套并行中的关键用法。
- **`IntoParallelIterator` 与 `IntoIterator`**（u2-l2）：前者是「能转成并行迭代器」的入口约定（`Vec`、`&Vec`、范围等实现它），后者是标准库的串行版本。两组 flat_map 的差异正是约束在这两者之间二选一。
- **`div_ceil`**：整数向上取整除法 \(\lceil a/b \rceil\)，本讲中「n 个元素按每块 c 个分组，共几块」全靠它计算。

如果对以上任何一项感到陌生，建议先回看 u3-l1（适配器骨架与 delegate 宏）和 u3-l3（Producer 契约与 `split_at`）。

## 3. 本讲源码地图

本讲涉及的关键文件如下（行号基于当前 HEAD `ee0a00b`）：

| 文件 | 作用 |
| --- | --- |
| [src/iter/mod.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs) | 所有适配器的**方法入口**：`flat_map`(L860)、`flat_map_iter`(L906)、`flatten`(L928)、`flatten_iter`(L951)、`by_exponential_blocks`(L2483)、`by_uniform_blocks`(L2509)、`chunks`(L2686)、`fold_chunks`(L2722)、`fold_chunks_with`(L2760)，以及 `chunk_size == 0` 的断言 |
| [src/iter/flat_map.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/flat_map.rs) | `FlatMap` 适配器：闭包产出**并行**迭代器，嵌套并行展平 |
| [src/iter/flat_map_iter.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/flat_map_iter.rs) | `FlatMapIter` 适配器：闭包产出**串行**迭代器，仅外层并行 |
| [src/iter/flatten.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/flatten.rs) | `Flatten`：无闭包版 `flat_map`，要求元素本身可转并行迭代器 |
| [src/iter/flatten_iter.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/flatten_iter.rs) | `FlattenIter`：无闭包版 `flat_map_iter`，要求元素本身是串行迭代器 |
| [src/iter/chunks.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/chunks.rs) | `Chunks` 适配器与**公共底座** `ChunkProducer`/`ChunkSeq` |
| [src/iter/fold_chunks.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/fold_chunks.rs) | `FoldChunks`：块内串行 fold（identity 为闭包），复用 `ChunkProducer` |
| [src/iter/fold_chunks_with.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/fold_chunks_with.rs) | `FoldChunksWith`：块内串行 fold（init 为值），复用 `ChunkProducer` |
| [src/iter/blocks.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/blocks.rs) | `ExponentialBlocks`/`UniformBlocks`：块间串行、块内并行的调度模式 |
| [src/iter/plumbing/mod.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs) | `Folder::consume_iter` 默认实现(L169)、`split_off_left`(L216)、`bridge_producer_consumer`(L385) |

阅读建议：先读 mod.rs 里各方法的文档注释（官方对 `flat_map_iter` 与 `flat_map` 的对比说明非常清楚），再进入各适配器文件；`chunks.rs` 是本讲第三模块的枢纽，`fold_chunks*.rs` 只需精读与 `chunks.rs` 不同的那一小段回调。

## 4. 核心概念与源码讲解

### 4.1 flat_map 家族：嵌套并行的展平

#### 4.1.1 概念说明

假设你有 100 个「外层」元素，每个元素经闭包映射出 10000 个「内层」元素。现在要并行处理这 100×10000 个结果，有两个选择：

- **只在 100 个外层元素之间并行**：每个任务拿到一个外层元素后，串行处理它的 10000 个内层元素。任务数少、调度开销小，但若外层只有 2 个元素，再多线程也只能用 2 个核。
- **嵌套并行**：每个任务处理一个外层元素时，其 10000 个内层元素还能继续切分给其他线程。负载更均衡，但切分本身有开销，内层计算太轻时反而亏。

Rayon 把两种策略做成了两个方法：

- [`flat_map`](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L860-L866)：闭包必须返回 `IntoParallelIterator`（并行迭代器），内层会被**继续并行切分**；
- [`flat_map_iter`](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L906-L912)：闭包只需返回 `IntoIterator`（普通串行迭代器），只在**外层之间并行**，内层元素顺序处理。

官方文档对两者的取舍有一段直接了当的说明：内层计算量小、或内层长度远小于外层时，用 `flat_map_iter` 避免并行开销通常更快；内层计算重（甚至超过外层）时，`flat_map` 的嵌套并行才值得（见 [src/iter/mod.rs:871-884](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L871-L884)，这段文档对比了两种场景）。

两条约束链也值得对比：

| | `flat_map` | `flat_map_iter` |
| --- | --- | --- |
| 闭包返回值 | `PI: IntoParallelIterator` | `SI: IntoIterator<Item: Send>` |
| 内层执行方式 | 嵌套并行，可再切分 | 串行 |
| 闭包自身约束 | `Fn + Sync + Send` | `Fn + Sync + Send` |
| 串行迭代器是否要求线程安全 | ——（不存在串行迭代器） | **不要求**，只有产出的元素要 `Send` |

最后一行是 `flat_map_iter` 的一个实用福利：串行迭代器本身可以是非线程安全的。官方文档给出的示例用 `RefCell` 构造了一个完全不适合同步的迭代器（[src/iter/mod.rs:890-905](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L890-L905) 的 doctest：`RefCell::new(a.iter().cloned())` 包在 `std::iter::from_fn` 里），照样能进并行管道——因为它只在单个任务内部被使用，从不跨线程共享。

#### 4.1.2 核心流程

两个适配器的 `drive_unindexed` 结构与 u3-l1 讲过的「包装消费者」完全同构：把下游消费者包成 `FlatMapConsumer`/`FlatMapIterConsumer`，转发给上游。差异全部藏在 `Folder::consume` 里。

`flat_map` 的 `consume`（伪代码）：

```text
FlatMapFolder::consume(item):
    par_iter = map_op(item).into_par_iter()        # 内层是并行迭代器
    consumer = base.split_off_left()               # 复制一份下游消费者给内层用
    result   = par_iter.drive_unindexed(consumer)  # 递归进入内层的并行执行
    previous = reducer.reduce(previous, result)    # 把内层结果并入累计值
```

注意第三步：`drive_unindexed` 正是 `ParallelIterator` 的内部驱动入口。也就是说，**消费一个外层元素的动作，就是完整地跑一遍内层并行迭代器**——这就是「嵌套并行」的落地方式。外层任务与内层任务共享同一个线程池，内层切分出的子任务同样可能被空闲线程窃取。

`flat_map_iter` 的 `consume` 则朴素得多：

```text
FlatMapIterFolder::consume(item):
    base = base.consume_iter(map_op(item))         # 内层是串行迭代器，逐元素灌给下游
```

`consume_iter` 是 `Folder` 的默认方法（[src/iter/plumbing/mod.rs:169-180](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L169-L180)），默认实现就是一个 `for` 循环逐个 `consume`，并在每次消费后检查 `full()` 以支持短路：

```rust
fn consume_iter<I>(mut self, iter: I) -> Self
where
    I: IntoIterator<Item = Item>,
{
    for item in iter {
        self = self.consume(item);
        if self.full() {
            break;
        }
    }
    self
}
```

此外 `FlatMapIterFolder` 还重写了 `consume_iter`（[src/iter/flat_map_iter.rs:127-135](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/flat_map_iter.rs#L127-L135)）：当上游本身送来一批元素时，它先用标准库的 `Iterator::flat_map` 把这批元素的串行迭代器在**任务内部**拼接成一条，再一次 `consume_iter` 灌给下游，减少中间状态转换。

最后一点重要的类型事实：**flat_map 家族全都没有索引**。`FlatMap`/`FlatMapIter` 只实现了 `ParallelIterator::drive_unindexed`（分别见 flat_map.rs L37 与 flat_map_iter.rs L39），没有实现 `IndexedParallelIterator`，也不覆写 `opt_len`（于是取默认值 `None`，见 [src/iter/mod.rs:2428-2430](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L2428-L2430)）。原因直观：输出总长度 = 各内层长度之和，而内层长度在切分前不可知（闭包返回什么长度都行）。这与 u3-l1 讲过的 `filter` 丢索引不同——`filter` 是「长度不可知」，`flat_map` 是「长度在求值前根本不存在」。下游的 `collect` 因此只能走 u2-l4 讲过的「无长度」路径：各任务分块收集后按序拼接。

#### 4.1.3 源码精读

**FlatMap 结构体与 ParallelIterator 实现**（[src/iter/flat_map.rs:29-44](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/flat_map.rs#L29-L44)）：老配方的「上游 + 闭包」小结构体；约束 `F: Fn(I::Item) -> PI + Sync + Send` 与 `PI: IntoParallelIterator` 决定了内层的并行身份；`drive_unindexed` 把消费者包成 `FlatMapConsumer` 转给上游：

```rust
impl<I, F, PI> ParallelIterator for FlatMap<I, F>
where
    I: ParallelIterator,
    F: Fn(I::Item) -> PI + Sync + Send,
    PI: IntoParallelIterator,
{
    type Item = PI::Item;

    fn drive_unindexed<C>(self, consumer: C) -> C::Result
    where
        C: UnindexedConsumer<Self::Item>,
    {
        let consumer = FlatMapConsumer::new(consumer, &self.map_op);
        self.base.drive_unindexed(consumer)
    }
}
```

**嵌套并行的核心：FlatMapFolder::consume**（[src/iter/flat_map.rs:121-140](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/flat_map.rs#L121-L140)）：每消费一个外层元素，就把闭包的结果转成并行迭代器，用 `split_off_left` 从下游消费者分裂出一份新消费者，递归 `drive_unindexed`，最后把结果并入 `previous`：

```rust
fn consume(self, item: T) -> Self {
    let map_op = self.map_op;
    let par_iter = map_op(item).into_par_iter();
    let consumer = self.base.split_off_left();
    let result = par_iter.drive_unindexed(consumer);

    let previous = match self.previous {
        None => Some(result),
        Some(previous) => {
            let reducer = self.base.to_reducer();
            Some(reducer.reduce(previous, result))
        }
    };

    FlatMapFolder {
        base: self.base,
        map_op,
        previous,
    }
}
```

三个细节值得咀嚼：

1. `split_off_left()` 是 `UnindexedConsumer` 的方法（定义于 [src/iter/plumbing/mod.rs:216](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L216)）。它借用 `&self` 分裂出一份新消费者，且分裂出的消费者**接过「左段」的责任**——本例中它将独占这个内层迭代器的全部产出，原消费者保留下来供下一个外层元素再次分裂。这是无索引世界里的「可无限次分身的消费者」。
2. `previous: Option<R>` 在单个 Folder 内部**串行**累积各内层结果（`reducer.reduce(previous, result)`）。嵌套并行发生在每个内层内部，而「多个内层的结果如何合并」在这个 Folder 层面是顺序的——最终顺序与外层元素顺序一致。
3. `complete` 里 `None` 分支（[src/iter/flat_map.rs:142-147](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/flat_map.rs#L142-L147)）：若一个元素都没消费，就用 `self.base.into_folder().complete()` 取下游消费者的「空结果」——这是向下游要单位元，而不是自己凭空造一个，因此用户无需为 flat_map 提供任何 identity。

**FlatMapIterFolder：内层串行的对照实现**（[src/iter/flat_map_iter.rs:108-144](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/flat_map_iter.rs#L108-L144)）：约束换成 `U: IntoIterator`（普通迭代器）；`consume` 只有一行实质逻辑 `self.base.consume_iter(map_op(item))`；没有 `previous`、没有 `split_off_left`、没有递归驱动：

```rust
fn consume(self, item: T) -> Self {
    let map_op = self.map_op;
    let base = self.base.consume_iter(map_op(item));
    FlatMapIterFolder { base, map_op }
}
```

对比之下，「嵌套并行 vs 内层串行」在源码上的距离就是 `drive_unindexed(split_off_left())` 与 `consume_iter` 的距离。

#### 4.1.4 代码实践

**实践目标**：用同一份数据、同样的轻量内层计算，对比 `flat_map`（嵌套并行）与 `flat_map_iter`（内层串行）的耗时差异，直观感受官方文档所说的「内层计算轻时避免并行开销」。

**操作步骤**：

1. 新建独立 Cargo 项目（示例代码，非仓库源码）：

```bash
cargo new flatmap-lab
cd flatmap-lab
cargo add rayon@1.12
```

2. 把 `src/main.rs` 替换为（**示例代码**）：

```rust
use rayon::prelude::*;
use std::time::Instant;

fn main() {
    // 外层 200 个元素，每个映射出 50_000 个内层元素
    let outer: Vec<usize> = (0..200).collect();
    let work = |i: usize| (0..50_000).map(move |j| i + j);

    // 预热线程池，排除首次初始化的干扰
    (0..1000).into_par_iter().for_each(|_| {});

    let t0 = Instant::now();
    let s1: u64 = outer
        .par_iter()
        .flat_map_iter(|&i| work(i)) // 内层串行
        .map(|x| x as u64)
        .sum();
    let d_iter = t0.elapsed();

    let t1 = Instant::now();
    let s2: u64 = outer
        .par_iter()
        .flat_map(|&i| work(i).collect::<Vec<_>>().into_par_iter()) // 内层并行
        .map(|x| x as u64)
        .sum();
    let d_par = t1.elapsed();

    assert_eq!(s1, s2);
    println!("flat_map_iter: {d_iter:?}");
    println!("flat_map     : {d_par:?}");
}
```

3. 用 `cargo run --release` 运行（务必 `--release`，调试构建的结论没有意义）。

**需要观察的现象**：两个版本结果相等（`assert_eq` 通过）；典型情况下 `flat_map_iter` 不慢于甚至明显快于 `flat_map`——因为这里的内层只是整数加法，嵌套切分的调度开销大于收益。

**预期结果**：打印两行耗时，`flat_map_iter` 通常更短。具体数值随机器与线程数而变，**待本地验证**；若在你的机器上差距不明显，可把内层 `map` 换成更重的计算（如 `|x| (0..x % 100).sum::<usize>()`），观察 `flat_map` 反超。

#### 4.1.5 小练习与答案

**练习 1**：`flat_map` 的闭包约束写的是 `PI: IntoParallelIterator` 而不是 `PI: ParallelIterator`，为什么？

**答案**：`IntoParallelIterator` 是「能转换成并行迭代器」的入口约定，由 `Vec`、`&Vec`、数组、范围等集合实现，与标准库 `IntoIterator` 对应；用户闭包最自然的返回值是集合而非迭代器。同时 [src/iter/mod.rs:2433-2440](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L2433-L2440) 有 blanket 实现 `impl<T: ParallelIterator> IntoParallelIterator for T`，任何现成的并行迭代器也自动满足该约束，两边都不亏。

**练习 2**：把官方文档中 `flat_map_iter` 的 `RefCell` 示例（[src/iter/mod.rs:890-905](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L890-L905)）改成 `flat_map`，会发生什么？

**答案**：编译失败。`std::iter::from_fn` 产生的迭代器不是 `IntoParallelIterator`，且 `RefCell` 不是 `Sync`，无法满足 `flat_map` 对闭包与返回值的要求；而 `flat_map_iter` 只要求 `SI: IntoIterator<Item: Send>`——串行迭代器本身不跨线程共享，线程安全约束只落在产出的元素上。

**练习 3**：`FlatMapFolder` 的 `previous` 字段为什么是 `Option<R>`，而不是直接存一个 `R`？

**答案**：Folder 可能在没有消费任何外层元素时就被 `complete`（例如上游为空、或元素全被上游的 `filter` 拦下）。`None` 时走 `self.base.into_folder().complete()`（[src/iter/flat_map.rs:142-147](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/flat_map.rs#L142-L147)）向下游消费者索取「空结果」，相当于向下游借单位元，从而免去要求用户为 flat_map 显式提供 identity 闭包。

### 4.2 flatten 家族：无闭包的展平

#### 4.2.1 概念说明

`flatten` 与 `flatten_iter` 是 `flat_map`/`flat_map_iter` 的「无闭包特化版」：不提供映射函数，直接要求**迭代器的元素本身**可迭代：

- [`flatten`](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L928-L933) 要求 `Self::Item: IntoParallelIterator`——比如 `Vec<Vec<i32>>` 按值并行迭代时元素是 `Vec<i32>`，天然满足；
- [`flatten_iter`](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L951-L956)（[src/iter/mod.rs:951-956](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L951-L956)）要求 `Self::Item: IntoIterator<Item: Send>`——元素是任何串行可迭代的东西即可。

语义上 `x.flatten()` 约等于 `x.flat_map(|v| v)`，`x.flatten_iter()` 约等于 `x.flat_map_iter(|v| v)`；但既然没有闭包，实现里也确实少了一个 `map_op` 字段，Folder 更简单，还顺带实现了 `Debug`（可 `#[derive(Debug)]`，而闭包不实现 `Debug`，见 [src/iter/flatten.rs:8](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/flatten.rs#L8) 的 derive 与 [src/iter/flat_map.rs:16-20](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/flat_map.rs#L16-L20) 的手写 `Debug` 对比）。

一个常被忽视的实际差异：`flatten` 对**借用**数据特别好使。`v.par_iter()` 的元素是 `&Vec<T>`，而 `&Vec<T>: IntoParallelIterator`（u2-l2 讲过切片家族的覆盖实现），所以 `v.par_iter().flatten()` 直接可用；`flatten_iter` 同理（`&Vec<T>: IntoIterator`）。这让「不移动数据的二层展平」成为一行代码。

#### 4.2.2 核心流程

`Flatten` 的执行流程与 4.1 的 `FlatMap` 完全同构，只是 `map_op(item).into_par_iter()` 换成了 `item.into_par_iter()`：

```text
FlattenFolder::consume(item):
    par_iter = item.into_par_iter()                # 元素自己就是并行迭代器的原料
    consumer = base.split_off_left()
    result   = par_iter.drive_unindexed(consumer)  # 嵌套并行
    previous = reducer.reduce(previous, result)
```

`FlattenIter` 同理，`consume` 退化为 `self.base.consume_iter(item)`——元素本身就是串行迭代器，直接灌给下游；其 `consume_iter` 重写则用标准库的 `Iterator::flatten()`（[src/iter/flatten_iter.rs:107-114](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/flatten_iter.rs#L107-L114)）拼接一批元素的迭代器。

两者同样只有 `drive_unindexed`、没有索引、`opt_len` 为默认 `None`。

#### 4.2.3 源码精读

**Flatten 的约束与实现**（[src/iter/flatten.rs:20-33](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/flatten.rs#L20-L33)）：注意约束写在 `I` 的 `Item` 上——`I: ParallelIterator<Item: IntoParallelIterator>`，输出元素类型取内层的 `Item`：

```rust
impl<I> ParallelIterator for Flatten<I>
where
    I: ParallelIterator<Item: IntoParallelIterator>,
{
    type Item = <I::Item as IntoParallelIterator>::Item;

    fn drive_unindexed<C>(self, consumer: C) -> C::Result
    where
        C: UnindexedConsumer<Self::Item>,
    {
        let consumer = FlattenConsumer::new(consumer);
        self.base.drive_unindexed(consumer)
    }
}
```

**FlattenFolder：与 FlatMapFolder 逐行对照**（[src/iter/flatten.rs:97-133](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/flatten.rs#L97-L133)）：结构体只剩 `base` 与 `previous` 两个字段；`consume` 的第一行是 `item.into_par_iter()`，其余（`split_off_left` → `drive_unindexed` → `reducer.reduce`）与 `FlatMapFolder` 一字不差。读者应能从这里看出：**flat_map 家族的四个适配器共享同一套骨架，差异只在「内层从哪来」**。

**FlattenIterFolder**（[src/iter/flatten_iter.rs:91-123](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/flatten_iter.rs#L91-L123)）：连 `previous` 都没有了——`consume` 直接 `self.base.consume_iter(item)`，`complete` 直接 `self.base.complete()`。这是四个展平适配器中最轻的一个：

```rust
fn consume(self, item: T) -> Self {
    let base = self.base.consume_iter(item);
    FlattenIterFolder { base }
}
```

**顺带一提的对照**：`FlatMapIterFolder`（4.1.3）持有 `base: C`（消费者），`FlattenIterFolder` 持有 `base: C::Folder`（已转化好的 Folder）。前者在 `into_folder` 时才把消费者转成 Folder（[src/iter/flat_map_iter.rs:81-86](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/flat_map_iter.rs#L81-L86)），后者直接持有（[src/iter/flatten_iter.rs:66-70](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/flatten_iter.rs#L66-L70)）。这是实现细节上的自由度：`FlatMapIterFolder` 需要在 `split_off_left` 时复制消费者，而 `FlattenIterFolder` 无此需求。

#### 4.2.4 代码实践

**实践目标**：验证三种写法（`flatten`、`flatten_iter`、`flat_map(|v| v)`）结果一致，并亲手确认展平后**失去索引能力**这一类型层面的事实。

**操作步骤**（**示例代码**，接 4.1.4 的项目即可）：

```rust
use rayon::prelude::*;

fn main() {
    let nested = vec![vec![1, 2], vec![3], vec![4, 5, 6]];

    let a: Vec<i32> = nested.par_iter().flatten().cloned().collect();
    let b: Vec<i32> = nested.par_iter().flatten_iter().cloned().collect();
    let c: Vec<i32> = nested.par_iter().flat_map(|v| v).collect();

    assert_eq!(a, vec![1, 2, 3, 4, 5, 6]);
    assert_eq!(b, a);
    assert_eq!(c, a);
    println!("三种展平全部一致: {a:?}");
}
```

然后在 `a` 的那一行之后尝试加上 `.len()` 或 `.enumerate()`（例如 `nested.par_iter().flatten().enumerate().collect::<Vec<_>>()`）。

**需要观察的现象**：三种写法编译通过且结果一致；但 `flatten().enumerate()` 编译失败。

**预期结果**：错误信息指出 `enumerate` 找不到——`enumerate` 定义在 `IndexedParallelIterator` 上（u3-l3），而 `Flatten` 只实现了 `ParallelIterator`。这正是 4.1.2 说的「输出长度在求值前不存在」。同时注意 `par_iter().flatten()` 的元素是 `&i32`，所以需要 `.cloned()`；而 `flat_map(|v| v)` 返回 `&Vec<i32>`，其 `IntoParallelIterator` 的产出同样是 `&i32`——两种入口殊途同归。

#### 4.2.5 小练习与答案

**练习 1**：`x.into_par_iter().flatten()` 与 `x.into_par_iter().flat_map(|v| v)` 完全等价吗？

**答案**：对外语义等价（产出元素、顺序、并行行为都一致），但类型不同：前者构造 `Flatten<X>`，后者构造 `FlatMap<X, 闭包>`。前者还多两个小好处：可 `derive(Debug, Clone)`，且少一层闭包间接。选择上，元素天然可迭代时用 `flatten` 更地道。

**练习 2**：为什么 `Flatten` 的 `Item` 写成 `<I::Item as IntoParallelIterator>::Item` 这种形式？

**答案**：输出元素是「内层并行迭代器的元素」，而内层类型是外层的 `Item`（它实现了 `IntoParallelIterator`）。`type Item = <I::Item as IntoParallelIterator>::Item` 用全路径限定语法把这个两层投影写进关联类型（[src/iter/flatten.rs:24](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/flatten.rs#L24)），让类型系统能沿管道继续推导。

**练习 3**：`Vec<Vec<i32>>` 分别接 `par_iter()` 与 `into_par_iter()` 后调用 `flatten()`，产出的 `Item` 类型分别是什么？

**答案**：`par_iter()` 的元素是 `&Vec<i32>`，`&Vec<i32>: IntoParallelIterator` 产出 `&i32`，所以展平后是 `&i32`；`into_par_iter()` 的元素是 `Vec<i32>`，展平后是 `i32`（内层按值消费，u2-l2 讲过的 drain 路线）。需要拥有所有权时用后者，想借用数据时用前者。

### 4.3 chunks / fold_chunks / blocks：分块处理模式

#### 4.3.1 概念说明

前两个模块解决「把嵌套结构摊平」，本模块反过来解决「把一长串元素**捆成块**再处理」。Rayon 提供了三类语义不同的「分块」：

1. **[`chunks(chunk_size)`](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L2686-L2689)**：把索引迭代器切成固定大小的块，**产出的元素就是块本身**——类型是 `Vec<I::Item>`。注意每块都是一次堆分配；官方文档明确指向切片版的 `par_chunks()` 作为「免分配」替代（见 [src/iter/mod.rs:2669-2673](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L2669-L2673) 的提示）。
2. **`fold_chunks` / `fold_chunks_with`**（[mod.rs:2722](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L2722-L2735)、[mod.rs:2760](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L2760-L2772)）：等价于 `chunks(n).map(|块| 块.fold(...))`，但**没有每块的 Vec 分配**（官方原话见 [src/iter/mod.rs:2707](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L2707)）。两者的区别在于初始值的给法：`fold_chunks` 收一个 `identity: Fn() -> T` 闭包，`fold_chunks_with` 收一个 `init: T` 值（要求 `T: Clone`，每块 clone 一份），后者因此不要求 `T: Sync`。
3. **`by_exponential_blocks` / `by_uniform_blocks`**（[mod.rs:2483](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L2483-L2485)、[mod.rs:2509](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L2509-L2512)）：**不改变产出的元素**（`Item` 与底层相同），只改变**调度顺序**——把迭代器变成「一段一段顺序处理的块，块内部并行」。这就是本讲标题里「blocks 提供的原始块访问语义」：块对管道下游完全透明，你感知到的只是执行顺序的变化。

三类能力的对比表：

| | 产出元素 | 保持索引 | 每块分配 | 典型用途 |
| --- | --- | --- | --- | --- |
| `chunks(n)` | `Vec<T>`（块本身） | 是 | 是（每块一个 Vec） | 需要拿到整块数据再处理 |
| `fold_chunks(n, ...)` | 每块的 fold 结果 | 是 | 否 | 块内聚合（块和、块平均） |
| `fold_chunks_with(n, init, ...)` | 每块的 fold 结果 | 是 | 否 | 同上，init 非 `Sync` 时 |
| `by_uniform_blocks(n)` | 原元素 | 否（只剩 `ParallelIterator`） | 否 | 缓存友好的归约、可中断计算 |
| `by_exponential_blocks()` | 原元素 | 否（同上） | 否 | `find_first` 类左偏搜索 |

块数计算都是同一条公式（`chunks.rs` L52、`fold_chunks.rs` L71、`fold_chunks_with.rs` L70 三处一致）：

\[ \text{块数} = \left\lceil \frac{n}{c} \right\rceil \quad \text{即 Rust 的 } n.\text{div\_ceil}(c) \]

`by_exponential_blocks` 的块大小从线程数 \(p\) 起步、每块翻倍（\(p, 2p, 4p, \dots\)），所以处理完 \(k\) 块后覆盖的元素数为：

\[ p + 2p + 4p + \cdots + 2^{k-1}p = p\,(2^k - 1) \]

覆盖 \(n\) 个元素大约需要 \(\lceil \log_2(n/p + 1) \rceil\) 块——块数是对数级的，这是官方文档所说「避免产生太多块」的数学依据（文档见 [src/iter/mod.rs:2450-2482](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L2450-L2482)，其中给出 `find_first` 的动机：普通对半切分会把右半段的功夫全浪费掉，指数块则保证在「包含目标的第一个块」处停下）。

#### 4.3.2 核心流程

**chunks / fold_chunks 的公共底座**：三者都只实现 `with_producer`（把一个回调挂到上游生产者上），回调里做的事一模一样——把上游生产者包进 `ChunkProducer`，唯一的差别是传给 `ChunkProducer` 的 `map` 闭包：

```text
chunks(n)          → map = Vec::from_iter          # 块的迭代器 → Vec（分配）
fold_chunks        → map = |iter| iter.fold(identity(), fold_op)
fold_chunks_with   → map = |iter| iter.fold(init.clone(),   fold_op)
```

`ChunkProducer` 是一个「元素空间 ⇄ 块空间」的换算器：

- `split_at(index)`：`index` 是**块的下标**，换算成元素下标 `index * chunk_size`（封顶在 `len`），再切底层的生产者（[chunks.rs:124-141](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/chunks.rs#L124-L141)）；
- `min_len`/`max_len`：把 u3-l3 讲过的粒度窗口从「元素数」换算成「块数」（`div_ceil` 与整除，[chunks.rs:143-149](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/chunks.rs#L143-L149)），于是 `with_min_len` 等粒度控制在块空间里继续生效；
- `into_iter`：产出 `ChunkSeq`——一个**用切分实现 next 的串行迭代器**：每次 `next` 都把当前生产者 `split_at(chunk_size)` 剥出左边一小块（[chunks.rs:164-176](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/chunks.rs#L164-L176)），块的迭代器再交给 `map` 闭包变成产出值。

**blocks 的执行流程**（[blocks.rs:16-48](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/blocks.rs#L16-L48)）：

```text
BlocksCallback::callback(producer):
    leftmost_res = 用 split_at(0) 的手法从消费者处取「单位元」
    while 还有剩余元素 且 消费者未 full():
        size = 尺寸序列的下一个（指数版翻倍 / 均匀版恒定），封顶在剩余量
        (left_producer, producer) = producer.split_at(size)
        (left_consumer, consumer) = consumer.split_at(size)
        leftmost_res = reduce(leftmost_res,
                              bridge_producer_consumer(size, left_producer, left_consumer))
    返回 leftmost_res
```

关键在最后一行：每块用 `bridge_producer_consumer` **在块内部正常地并行切分执行**（u3-l3 讲过的 `bridge` 的底层函数），而**块与块之间由这个循环串行推进**，每块的结果立刻 `reduce` 进累计值。于是：

- 对 `find_any` 这类可中断计算，`while` 条件里的 `consumer.full()`（u2-l5 讲过的短路钩子）让后续块根本不会被启动；
- 对 `by_uniform_blocks` 的缓存友好归约，每块的数据量被控制在 L1/L2 缓存内，块内 fold 中间结果不落内存。

一个精巧的细节：循环开头用 `consumer.split_at(0)` 拿「单位元」（[blocks.rs:22-24](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/blocks.rs#L22-L24)）——左边消费者分到 0 个元素，`into_folder().complete()` 的结果就是该消费者对「空输入」的答案，代码注释直言这是「借用 reducer 的单位元」。与 4.1.3 练习 3 的手法同源：**向消费者要单位元，而不是让用户提供**。

#### 4.3.3 源码精读

**Chunks：产出 Vec 的索引适配器**（[src/iter/chunks.rs:22-53](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/chunks.rs#L22-L53)）：`type Item = Vec<I::Item>`；`len()` 就是 `div_ceil`。注意它**保持索引**——`chunks` 之后仍可 `enumerate`/`zip`（与 flat_map 家族形成对照），因为块数是精确已知的：

```rust
impl<I> IndexedParallelIterator for Chunks<I>
where
    I: IndexedParallelIterator,
{
    fn len(&self) -> usize {
        self.i.len().div_ceil(self.size)
    }
    // drive / with_producer ...
}
```

**with_producer 的回调与 `Vec::from_iter`**（[src/iter/chunks.rs:55-87](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/chunks.rs#L55-L87)）：挂到上游生产者上，把 `Vec::from_iter` 作为 `map` 传入 `ChunkProducer`——这一行就是「chunks 的每块分配」的源头，也是三个分块适配器唯一分道扬镳的地方：

```rust
fn callback<P>(self, base: P) -> CB::Output
where
    P: Producer<Item = T>,
{
    let producer = ChunkProducer::new(self.size, self.len, base, Vec::from_iter);
    self.callback.callback(producer)
}
```

**ChunkProducer：块空间的 Producer**（[src/iter/chunks.rs:107-150](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/chunks.rs#L107-L150)）：`split_at` 做块下标→元素下标的换算并切底层生产者；`min_len`/`max_len` 把粒度窗口换算到块空间。`map: F` 字段是 `Fn(P::IntoIter) -> T + Send + Clone`——「把一块的串行迭代器变成产出值」的函数，切分时 clone 一份给左半：

```rust
fn split_at(self, index: usize) -> (Self, Self) {
    let elem_index = Ord::min(index * self.chunk_size, self.len);
    let (left, right) = self.base.split_at(elem_index);
    (
        ChunkProducer { chunk_size: self.chunk_size, len: elem_index, base: left, map: self.map.clone() },
        ChunkProducer { chunk_size: self.chunk_size, len: self.len - elem_index, base: right, map: self.map },
    )
}
```

**ChunkSeq：靠切分实现的 next**（[src/iter/chunks.rs:152-182](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/chunks.rs#L152-L182)）：每次 `next` 从剩余生产者上剥出恰好一块；它同时实现 `ExactSizeIterator`（`len()` 同样是 `div_ceil`）与 `DoubleEndedIterator`（`next_back` 处理好「尾块可能不足整块」的边界，见 L198-214）。

**fold_chunks：换一个 map 闭包**（[src/iter/fold_chunks.rs:110-120](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/fold_chunks.rs#L110-L120)）：回调里构造的 `fold_iter` 闭包在**块的串行迭代器上直接 `fold`**，不经过任何 `Vec`：

```rust
fn callback<P>(self, base: P) -> CB::Output
where
    P: Producer<Item = T>,
{
    let identity = &self.identity;
    let fold_op = &self.fold_op;
    let fold_iter = move |iter: P::IntoIter| iter.fold(identity(), fold_op);
    let producer = ChunkProducer::new(self.chunk_size, self.len, base, fold_iter);
    self.callback.callback(producer)
}
```

**fold_chunks_with：初始值版**（[src/iter/fold_chunks_with.rs:109-119](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/fold_chunks_with.rs#L109-L119)）：唯一差别是 `iter.fold(item.clone(), fold_op)`——每块 clone 一份初始值。文档说明这等价于 `fold_chunks(chunk_size, || init.clone(), fold_op)`，但免去 `init` 的 `Sync` 约束（[src/iter/mod.rs:2743-2745](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L2743-L2745)）。两个文件末尾各有一组内联测试（`fold_chunks.rs` L124-223、`fold_chunks_with.rs` L123-219），覆盖了空输入、不整除块、`len()` 计算、`rev()` 反向、`chunk_size == 0` panic 等边界，是理解语义的最好材料。

**blocks：ExponentialBlocks 与 UniformBlocks**（[src/iter/blocks.rs:68-90](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/blocks.rs#L68-L90) 与 [blocks.rs:111-128](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/blocks.rs#L111-L128)）：两者共用 `BlocksCallback`，只差尺寸序列——指数版用 `std::iter::successors(Some(current_num_threads()), |s| Some(s.saturating_mul(2)))` 从线程数起步翻倍，均匀版用 `std::iter::repeat(block_size)`：

```rust
fn drive_unindexed<C>(self, consumer: C) -> C::Result
where
    C: UnindexedConsumer<Self::Item>,
{
    let first = crate::current_num_threads();
    let callback = BlocksCallback {
        consumer,
        sizes: std::iter::successors(Some(first), exponential_size),
        len: self.base.len(),
    };
    self.base.with_producer(callback)
}
```

**入口处的断言**：`chunks`/`fold_chunks`/`fold_chunks_with`/`by_uniform_blocks` 都在 mod.rs 的方法体里 `assert!(size != 0, "... must not be zero")`（分别在 [mod.rs:2687](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L2687)、[mod.rs:2733](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L2733)、[mod.rs:2770](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L2770)、[mod.rs:2510](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L2510)），并标注了 `#[track_caller]` 让 panic 指向调用处。零块大小会在 `div_ceil` 处除零，所以入口即拦。

#### 4.3.4 代码实践

**实践目标**：用 `fold_chunks_with` 实现「每 4 个元素求平均」，并与 `chunks(4).map(...)` 的写法对照，验证语义一致；同时跑通仓库内自带的 `fold_chunks` 单元测试。

**操作步骤**：

1. 在你的示例项目里加入（**示例代码**）：

```rust
use rayon::prelude::*;

fn main() {
    let nums: Vec<i32> = (0..10).collect(); // [0,1,...,9]

    // 写法一：chunks + map —— 每块先分配一个 Vec，再求平均
    let a: Vec<f64> = nums
        .par_iter()
        .chunks(4)
        .map(|chunk| chunk.iter().sum::<i32>() as f64 / chunk.len() as f64)
        .collect();

    // 写法二：fold_chunks_with —— 块内零分配地折成 (和, 个数) 元组
    let b: Vec<f64> = nums
        .par_iter()
        .fold_chunks_with(4, (0i64, 0usize), |(sum, n), &x| (sum + x as i64, n + 1))
        .map(|(sum, n)| sum as f64 / n as f64)
        .collect();

    assert_eq!(a, vec![1.5, 5.5, 8.5]); // (0+1+2+3)/4, (4+5+6+7)/4, (8+9)/2
    assert_eq!(a, b);
    println!("分块平均: {a:?}");
}
```

2. 运行仓库内的官方测试，验证 `ChunkProducer` 换算逻辑（在本仓库根目录）：

```bash
cargo test -p rayon fold_chunks
```

**需要观察的现象**：两种写法输出一致 `[1.5, 5.5, 8.5]`（最后一块只有 2 个元素，`div_ceil(10,4)=3` 块）；官方测试输出中 `check_fold_chunks_even_size`、`check_fold_chunks_uneven`（含 `rev()` 反向收集）等用例全部通过。

**预期结果**：断言全部通过；`cargo test -p rayon fold_chunks` 会同时跑起 `fold_chunks.rs` 与 `fold_chunks_with.rs` 两个文件的测试模块（每组 6 个左右用例）。注意写法二里 `fold_chunks_with` 之后仍能 `.map(...).collect()` 精确收集——因为它保持索引；而 `.rev()` 也可用（官方 `check_fold_chunks_uneven` 测试正是这么验证的）。

#### 4.3.5 小练习与答案

**练习 1**：长度为 10 的迭代器接 `chunks(3)`，产出几个元素？`Chunks::len()` 怎么算？

**答案**：4 个（最后一块只有 1 个元素）。`len()` 是 `self.i.len().div_ceil(self.size)` 即 \(\lceil 10/3 \rceil = 4\)（[src/iter/chunks.rs:51-53](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/chunks.rs#L51-L53)）。

**练习 2**：`ChunkProducer::split_at(2)`（`chunk_size = 3`）在元素空间的哪里切分？为什么 `map` 字段要求 `Clone`？

**答案**：在元素下标 `2 * 3 = 6` 处（封顶在 `len`，见 [src/iter/chunks.rs:125](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/chunks.rs#L125)）。切分要把同一个 `map` 闭包给左右两个子生产者各一份，所以 `F: Clone`（约束在 [chunks.rs:110](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/chunks.rs#L110)）。

**练习 3**：`by_exponential_blocks()` 之后还能调用 `.len()` 吗？它的 `Item` 是什么？

**答案**：不能。`ExponentialBlocks` 只实现 `ParallelIterator::drive_unindexed`（[src/iter/blocks.rs:68-86](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/blocks.rs#L68-L86)），没有 `IndexedParallelIterator`。它的 `Item = I::Item` 与底层完全相同（[blocks.rs:72](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/blocks.rs#L72)）——块只改变调度顺序（块间串行、块内并行），不改变产出元素。有趣的是它**必须**作用于 `IndexedParallelIterator`（约束 `I: IndexedParallelIterator`），因为 `BlocksCallback` 需要拿到精确长度与可按下标切分的生产者来切块。

## 5. 综合实践

本综合实践把本讲两条主线串起来：**展平（flatten vs flatten_iter）** 与 **分块（fold_chunks_with）**。

**任务**：给定 `Vec<Vec<i32>>`，第一步分别用 `flatten` 与 `flatten_iter` 求总和并计时对比；第二步把展平结果每 4 个元素求平均，收集成 `Vec<f64>`。

**完整示例程序**（**示例代码**，新建项目 `cargo new flatten-lab && cd flatten-lab && cargo add rayon@1.12` 后替换 `src/main.rs`）：

```rust
use rayon::prelude::*;
use std::time::Instant;

fn main() {
    // 4000 × 5000 = 两千万个元素
    let data: Vec<Vec<i32>> = (0..4000)
        .map(|i| (0..5000).map(|j| ((i + 1) * (j % 97)) as i32).collect())
        .collect();

    // 预热线程池，避免首次调度污染计时
    data.par_iter().for_each(|_| {});

    // ---- 第一部分：flatten vs flatten_iter 求和计时 ----
    let t0 = Instant::now();
    let s1: i64 = data.par_iter().flatten().map(|&x| x as i64).sum();
    let d_par = t0.elapsed();

    let t1 = Instant::now();
    let s2: i64 = data.par_iter().flatten_iter().map(|&x| x as i64).sum();
    let d_iter = t1.elapsed();

    assert_eq!(s1, s2);
    println!("flatten      求和: {s1}，耗时 {d_par:?}");
    println!("flatten_iter 求和: {s2}，耗时 {d_iter:?}");

    // ---- 第二部分：展平后每 4 个元素求平均（分块聚合）----
    let flat_len = 4_000 * 5_000;
    let avgs: Vec<f64> = (0..flat_len as i32)
        .into_par_iter()
        .fold_chunks_with(4, (0i64, 0usize), |(sum, n), x| (sum + x as i64, n + 1))
        .map(|(sum, n)| sum as f64 / n as f64)
        .collect();

    println!("平均块数: {}（div_ceil({}, 4)），首块平均 = {}",
             avgs.len(), flat_len, avgs[0]);
    assert_eq!(avgs.len(), flat_len.div_ceil(4));
}
```

**操作步骤**：

1. `cargo run --release` 运行（不要用调试构建）。
2. 记录两种展平的耗时；再用 `RAYON_NUM_THREADS=2 cargo run --release` 与 `RAYON_NUM_THREADS=16 cargo run --release` 各跑一次，观察线程数对两者差距的影响（u1-l2 讲过该环境变量）。
3. 对照源码解释观察：`flatten` 的 `Folder::consume` 里每消费一个 `&Vec<i32>` 就 `split_off_left` + `drive_unindexed` 递归进入内层并行（[src/iter/flatten.rs:104-107](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/flatten.rs#L104-L107)）；`flatten_iter` 只对内层 `Vec` 做一次连续内存的串行扫描（[src/iter/flatten_iter.rs:102-105](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/flatten_iter.rs#L102-L105)）。
4. 回答：本例内层是「重计算」还是「轻计算」？据此两种展平谁更快，为什么？

**预期结果**：两行求和数值相同；由于 `map(|&x| x as i64)` 是极轻的内层计算且内层是 5000 个连续 `i32`（缓存友好），典型机器上 `flatten_iter` 不慢于 `flatten`，线程数越多两者的调度开销差异越明显。具体数值**待本地验证**——这正是 4.1.1 官方文档对比的活例子：想看到 `flatten` 反超，可把 `map` 换成重计算（例如 `|&x| (0..x % 2000).sum::<i32>()`）再跑一次。分块部分应打印 `平均块数: 5000000`、`首块平均 = 1.5`——注意第二部分作用的是新造的范围 `0..20_000_000`（不是 `data` 展平的结果），其前 4 个元素是 0、1、2、3，平均恰为 1.5。

## 6. 本讲小结

- **flat_map/flatten 是嵌套并行**：`Folder::consume` 对每个外层元素调用 `into_par_iter()` + `split_off_left()` + `drive_unindexed()`，内层迭代器会被继续切分给其他线程；**flat_map_iter/flatten_iter 只在外层之间并行**，内层经 `consume_iter` 串行灌给下游。内层计算轻、或长度远小于外层时，串行内层通常更快。
- 两组展平适配器**都没有索引**：输出长度是「各内层长度之和」，求值前不存在，因此只实现 `drive_unindexed`，`collect` 走无长度的分块拼接路径。
- **`ChunkProducer` 是 chunks/fold_chunks/fold_chunks_with 三者共同的底座**：把「块下标 ⇄ 元素下标」的换算（`index * chunk_size`）、粒度窗口换算（`div_ceil`）封装一次，三个适配器只差一个把「块的串行迭代器」变成产出值的 `map` 闭包——`Vec::from_iter`（有分配）或 `iter.fold(...)`（零分配）。
- **chunks 保持索引**（块数 `div_ceil(n, c)` 精确已知，可 `enumerate`/`rev`），**blocks 丢弃索引但保持元素不变**：`by_exponential_blocks`/`by_uniform_blocks` 只改调度——块间由 `BlocksCallback` 的循环串行推进、每块经 `bridge_producer_consumer` 块内并行，指数版从线程数起步翻倍，块数为对数级 \(O(\log(n/p))\)。
- 三个漂亮的实现手法值得带走：用 `split_off_left` 给内层并行分裂消费者；用 `into_folder().complete()` / `split_at(0)` **向消费者借单位元**（免去用户提供 identity）；用「生产者反复 `split_at(chunk_size)`」实现顺序块迭代器 `ChunkSeq`。
- `chunks`/`fold_chunks*`/`by_uniform_blocks` 的 `size == 0` 都在 mod.rs 入口处以 `#[track_caller]` 断言拦截。

## 7. 下一步学习建议

- **下一讲 u3-l6《树形遍历与通用切分》**：`walk_tree` 把分治递归表达为迭代器，`Split` trait 让任意「能对半拆值」的类型获得并行能力——与本讲的 `flat_map`（按闭包展开）和 `ChunkProducer`（按下标切块）构成三种不同的切分哲学，对照学习收获最大。
- **单元四 u4-l1《plumbing 总览》**：本讲你已经在 `Folder::consume` 层面看到了嵌套并行的实现；单元四会自顶向下补齐 `Producer`/`Consumer`/`bridge` 的完整契约与数据流图，把 u3 的零散认知系统化。
- **源码延伸阅读**：`src/iter/plumbing/mod.rs` 中 `Folder::consume_iter` 与 `bridge_producer_consumer` 的实现（本讲已引用行号）；以及 `tests/` 目录下搜索 `flatten`、`chunks` 相关集成测试，观察官方如何断言展平顺序与分块边界。
- 若你对「块调度」感兴趣，可提前阅读 [src/iter/mod.rs:2450-2482](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L2450-L2482) 中 `by_exponential_blocks` 的完整文档，它解释了为何 `find_first` 与 `find_any` 在块调度下性能更可预测——这与 u3-l4 讲过的 find 家族直接呼应。
