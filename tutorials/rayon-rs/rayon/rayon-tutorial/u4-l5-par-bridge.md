# par_bridge：桥接顺序迭代器

## 1. 本讲目标

学完本讲，你应该能够：

- 说出 `ParallelBridge` 这个 blanket 实现的 trait 如何让**任意** `std` 迭代器一行接入 Rayon。
- 精读 `IterBridge` 与 `IterParallelProducer` 的源码，理解「一把 `Mutex` + 逐元素 `next()`」这套取元素协议，以及它为什么天然无索引（`UnindexedProducer`）。
- 解释 `par_bridge` 的性能画像：锁竞争何时是瓶颈、何时无所谓，为什么不缓冲反而能安全处理无限迭代器。
- 理解 `fold_with` 开头那段 `threads_started` 标志检查防范的「工作窃取诱发的递归锁死」，以及 `tests/par_bridge_recursion.rs` 如何构造出这种险境。

本讲是单元四的最后一讲，承接 u4-l3 已建立的「`drive_unindexed` 走 `bridge_unindexed`」驱动路径，把 plumbing 知识用在一个真实而极端的生产者上：一个**根本不切分数据、只发放取元素资格**的生产者。

## 2. 前置知识

- **顺序迭代器（`std::iter::Iterator`）**：Rust 标准库的惰性序列，靠反复调 `next()` 拉取元素。它是单线程协议——`next(&mut self)` 需要独占借用，多线程无法直接共享。
- **`Mutex` 与临界区**：互斥锁保证同一时刻只有一个线程进入临界区。本讲涉及两点关键常识：标准库 `Mutex` **不可重入**（同一线程二次 lock 会死锁或未定义）；临界区越短，竞争开销越小。
- **`UnindexedProducer`（u4-l2、u3-l6）**：无索引生产者的契约只有两个方法——`split()`「把自己掰成两半，掰不动返回 `None`」和 `fold_with(folder)`「把元素推给折叠器」。切分点由数据自身决定。
- **`bridge_unindexed` 与 `Splitter`（u3-l6、u4-l3）**：无索引桥接的递归引擎：凭 `Splitter` 的「窃取自适应预算」裁决是否继续二分，用 `join_context` 并行执行两半（工作窃取在此发生），`Reducer` 合并结果。
- **`current_thread_index` / `current_num_threads`**：rayon-core 提供的两个查询 API——当前代码是否跑在池的工作线程上（返回 `Option<usize>` 线程编号）与线程池规模。
- **`.fuse()`**：标准库适配器，保证迭代器一旦返回过 `None`，之后所有 `next()` 永远返回 `None`。默认情况下 `Iterator` 并不承诺这一点（耗尽后再调 `next()` 行为未指定），而 `par_bridge` 的多个线程会共享同一个迭代器、反复试探 `next()`，所以必须先 fuse。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/iter/par_bridge.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/par_bridge.rs) | 本讲主战场：`ParallelBridge` trait、`IterBridge` 迭代器、`IterParallelProducer` 生产者，全文不足 160 行 |
| [tests/par_bridge_recursion.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/tests/par_bridge_recursion.rs) | 递归防护的回归测试：在顺序迭代器内部调用 rayon，诱发窃取递归 |
| [src/iter/plumbing/mod.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs) | 复习 `UnindexedProducer` 契约、`Splitter` 预算与 `bridge_unindexed` 递归引擎 |
| [src/iter/mod.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs) / [src/prelude.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/prelude.rs) | `IterBridge`、`ParallelBridge` 的再导出位置（prelude 也包含 `ParallelBridge`） |

## 4. 核心概念与源码讲解

### 4.1 ParallelBridge trait：一行接入并行世界

#### 4.1.1 概念说明

u2-l2 梳理数据源时我们看到：切片、范围、集合都为 Rayon **专门实现了**并行入口。但世界上还有大量「只有串行形态」的迭代器——`mpsc` channel 的接收端、`BufReader::lines()`、网络流解析器、某个第三方库返回的迭代器。为它们逐一写 `Producer` 不现实。

`ParallelBridge` 解决的就是这个「最后一公里」：它不把数据切开，而是把**取元素的动作**并行化——多个工作线程轮流从同一个顺序迭代器里拉元素，各自处理。文档注释把它定位为「万能但次优」：

> This has the advantage of being able to parallelize just about anything, but the resulting `ParallelIterator` can be less efficient than if you started with `par_iter` instead.

两个必须刻进脑子的官方告诫（都写在 trait 文档里）：

1. 元素由各线程经 `next()` 逐个拉取、每次都同步，若顺序迭代器供不上并行需求，它会成为瓶颈；
2. **不保证保持原迭代器的顺序**——哪个线程抢到哪个元素由调度决定。

#### 4.1.2 核心流程

```text
任意迭代器 iter
    │  iter.par_bridge()
    ▼
IterBridge { iter }          ← 只是包装，惰性
    │  消费者触发 drive_unindexed
    ▼
共享一个 Mutex<Fuse<Iter>> 的生产者
    │  bridge_unindexed 递归切分
    ▼
若干个工作线程同时在 fold_with 里
    lock → next() → unlock → 处理元素 → 循环
```

要点：切分发生在「资格」层面而不是「数据」层面——每次 `split()` 只是允许再多一个线程去拉，没有任何一段数据被划走。

#### 4.1.3 源码精读

trait 本体只有一个方法，且用 blanket impl 覆盖了所有满足约束的类型：

[src/iter/par_bridge.rs:53-65](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/par_bridge.rs#L53-L65)

```rust
pub trait ParallelBridge: Sized {
    /// Creates a bridge from this type to a `ParallelIterator`.
    fn par_bridge(self) -> IterBridge<Self>;
}

impl<T> ParallelBridge for T
where
    T: Iterator<Item: Send> + Send,
{
    fn par_bridge(self) -> IterBridge<Self> {
        IterBridge { iter: self }
    }
}
```

这段代码做了两件事：

- 定义一个单方法 trait，并**为所有 `Iterator` 实现**（blanket impl，u8-l3 的委托宏之外另一种「一次覆盖全体」的手法）。约束 `Iterator<Item: Send> + Send` 是数据竞争自由的最小要求：元素要能跨线程发送，迭代器本身要能移交给别的工作线程。
- `par_bridge` 是零成本的纯包装，生成的 `IterBridge` 也只是个两个字段都没有的小结构体（[src/iter/par_bridge.rs:71-74](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/par_bridge.rs#L71-L74)），派生了 `Debug` 与 `Clone`。

顺带一提可移植性细节：文件开头的条件导入让 wasm 目标改用 `wasm_sync::Mutex`（[src/iter/par_bridge.rs:1-5](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/par_bridge.rs#L1-L5)），这是 u9-l4 会展开的平台适配话题在本模块的唯一露面点。

文档里自带的 channel 示例值得一看（[src/iter/par_bridge.rs:33-52](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/par_bridge.rs#L33-L52)）：把 `rx.into_iter().par_bridge().collect()` 的结果 `sort_unstable()` 后再断言——正是因为顺序不保证，测试必须先排序。

#### 4.1.4 代码实践

1. **实践目标**：验证 blanket impl 的覆盖面——给三个风马牛不相及的迭代器都调 `par_bridge`。
2. **操作步骤**：新建独立 Cargo 项目（依赖 `rayon = "1"`），写入：

   ```rust
   use rayon::iter::ParallelBridge; // 或 use rayon::prelude::*;
   use rayon::prelude::ParallelIterator;

   fn main() {
       // 1. channel 接收端
       let (tx, rx) = std::sync::mpsc::channel();
       for i in 0..10 { tx.send(i).unwrap(); }
       drop(tx);
       let sum: i32 = rx.into_iter().par_bridge().sum();
       println!("channel sum = {sum}");

       // 2. 生成器迭代器
       let s2: i64 = std::iter::repeat_with(|| 2)
           .take(1_000_000)
           .par_bridge()
           .sum();
       println!("repeat sum = {s2}");

       // 3. 字符串 split 的迭代器
       let words: usize = "a b c d".split_whitespace()
           .par_bridge()
           .map(|w| w.len())
           .sum();
       println!("letters = {words}");
   }
   ```

3. **需要观察的现象**：三类迭代器无需任何自定义实现即可并行；`sum` 这类顺序无关消费者结果确定。
4. **预期结果**：输出 `channel sum = 45`、`repeat sum = 2000000`、`letters = 4`（待本地验证——若机器线程数不同，元素分布会变，但 `sum` 不变）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ParallelBridge` 要求 `Item: Send`，而切片的 `par_iter` 只要求 `T: Sync`？

**答案**：`par_bridge` 的多个工作线程各自**拿到元素的所有权**（`next()` 按值返回）并带回家处理，元素必须能跨线程移动，故 `Send`。切片共享迭代只发 `&T` 引用，多线程共享读取只需 `Sync`（u2-l2 的约束规则）。

**练习 2**：下面代码能否编译？为什么。

```rust
let v: Vec<std::rc::Rc<i32>> = vec![Rc::new(1)];
let s: usize = v.iter().map(|x| **x).par_bridge().sum();
```

**答案**：能编译且能运行。`Rc` 不 `Send` 只是说 **元素** 不能跨线程，但这里 `map` 已把元素转成 `i32`（`Send`），且 `Map<VecIter, closure>` 本身满足 `Iterator<Item: Send> + Send`，符合 blanket impl 约束。若去掉 `.map(|x| **x)` 则 `Item = &Rc<i32>` 不是 `Send`，编译失败。

**练习 3**：`par_bridge` 之后能接 `.enumerate()` 吗？

**答案**：不能。`enumerate` 定义在 `IndexedParallelIterator` 上（u3-l3），而 `IterBridge` 只实现 `ParallelIterator`（走 `drive_unindexed`），没有长度信息。想要编号得先 `collect::<Vec<_>>()` 再 `into_par_iter().enumerate()`——那时编号与原始顺序一致，而 `par_bridge` 本身连顺序都不保证。

### 4.2 IterBridge 与 IterParallelProducer：锁保护的取元素协议

#### 4.2.1 概念说明

`IterBridge` 是面向用户的迭代器壳；真正干活的是 `drive_unindexed` 里临时构造的 `IterParallelProducer`。它的设计浓缩成一句话：

> 一个被 `Mutex` 保护的 `Fuse<Iter>`，加上一个记录「还允许几个并发拉取者」的原子预算。

这里没有数据切分。所谓「并行」是 P 个线程同时围着一口锅捞元素——锅只有一把勺子（`Mutex`）。文档特意指出不缓冲（not buffered）是特性而非缺陷：正因为不攒批，无限或超长迭代器也不会撑爆内存。

#### 4.2.2 核心流程

`drive_unindexed` 的准备阶段：

```text
num_threads = current_num_threads()
threads_started = [false; num_threads]     ← 每线程一个 AtomicBool（递归防护，见 4.3）
split_count   = num_threads                ← 并发拉取预算
producer = IterParallelProducer { split_count, iter: Mutex::new(iter.fuse()), threads_started }
bridge_unindexed(&producer, consumer)      ← 注意传的是 &producer（共享引用！）
```

运行期每个工作线程在 `fold_with` 中的主循环：

```text
loop:
    lock(iter)                 ← 拿到勺子
    it = iter.next()           ← 临界区内只做这一件事
    unlock                     ← 立刻放勺子
    若 it = None     → 返回 folder（本线程的份额结束）
    folder = folder.consume(it)← 用户代码在锁外执行
    若 folder.full() → 返回 folder（下游短路，见 u2-l5）
```

关于性能直觉：设拉取（锁 + `next()`）串行部分占单元素总耗时比例为 \( s \)，则无论多少线程，加速比上限约为 \( 1/s \)——`s` 大（`next()` 是纯内存自增）时几乎无并行收益，`s` 小（`next()` 里是磁盘/网络 I/O，处理又重）时接近线性加速。这解释了文档「channels or file or network I/O」的推荐场景。

#### 4.2.3 源码精读

驱动入口：[src/iter/par_bridge.rs:82-97](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/par_bridge.rs#L82-L97)

```rust
fn drive_unindexed<C>(self, consumer: C) -> C::Result
where
    C: UnindexedConsumer<Self::Item>,
{
    let num_threads = current_num_threads();
    let threads_started: Vec<_> = (0..num_threads).map(|_| AtomicBool::new(false)).collect();

    bridge_unindexed(
        &IterParallelProducer {
            split_count: AtomicUsize::new(num_threads),
            iter: Mutex::new(self.iter.fuse()),
            threads_started: &threads_started,
        },
        consumer,
    )
}
```

四处细节：

- `self.iter.fuse()`：把迭代器 fuse 后再放进 `Mutex`，杜绝「耗尽后再调 `next()` 行为未指定」的坑——多个线程都会在见到 `None` 前反复试探。
- `split_count` 初始为线程数：并发拉取者的目标数量就是「一人一份」，切得更多只会加剧锁竞争。
- `bridge_unindexed` 接到的生产者类型是 `&IterParallelProducer`——共享引用。对比 u4-l2 的结论「`split_at` 按值消费 self、返回两个独立生产者」，这里**切分根本不复制数据**，复制的是指向同一块共享状态的引用。
- `threads_started` 借自栈上 `Vec`，生命周期 `'a` 由生产者结构体携带（[src/iter/par_bridge.rs:100-104](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/par_bridge.rs#L100-L104)）。

切分逻辑：[src/iter/par_bridge.rs:106-116](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/par_bridge.rs#L106-L116)

```rust
impl<Iter: Iterator + Send> UnindexedProducer for &IterParallelProducer<'_, Iter> {
    type Item = Iter::Item;

    fn split(self) -> (Self, Option<Self>) {
        #[allow(deprecated, reason = "TODO (MSRV 1.95): use try_update")]
        let update = self
            .split_count
            .fetch_update(Ordering::Relaxed, Ordering::Relaxed, |c| c.checked_sub(1));
        (self, update.is_ok().then_some(self))
    }
```

读法：

- `fetch_update(..., |c| c.checked_sub(1))` 尝试把预算原子地减一；减成功（`is_ok`）返回 `(self, Some(self))`——两个「新生产者」其实是同一个引用，含义是「批准再进场一位拉取者」；预算已到 0 则返回 `(self, None)`。
- 对照引擎侧：`bridge_unindexed` 拿到 `None` 就放弃切分、落到串行 `fold_with`（[src/iter/plumbing/mod.rs:460-471](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L460-L471)）。注意 `Relaxed` 内存序就够——预算只是个计数建议，不影响正确性。
- 源码里 `split` 上方那句注释写着 "Check if the iterator is exhausted"，但实现检查的其实是 `split_count`：**耗尽信号并不在此处探测**，而是等某个线程在 `fold_with` 里 `next()` 到 `None` 自然收工。预算与耗尽是两条独立的终止路径。

拉取主循环：[src/iter/par_bridge.rs:118-157](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/par_bridge.rs#L118-L157)

```rust
fn fold_with<F>(self, mut folder: F) -> F
where
    F: Folder<Self::Item>,
{
    // （递归防护，4.3 节详解，此处略）
    loop {
        if let Ok(mut iter) = self.iter.lock() {
            if let Some(it) = iter.next() {
                drop(iter);
                folder = folder.consume(it);
                if folder.full() {
                    return folder;
                }
            } else {
                return folder;
            }
        } else {
            // any panics from other threads will have been caught by the pool,
            // and will be re-thrown when joined - just exit
            return folder;
        }
    }
}
```

三个关键点：

- **临界区极小**：锁内只有 `next()`；`drop(iter)` 显式在 `consume` 之前放锁，用户闭包（可能很重）完全在锁外跑。这是「锁保护的取元素协议」的精髓——护住共享迭代器，不护计算。
- `folder.full()` 让 u2-l5 学过的短路家族（`find_any`、`try_reduce`、`panic_fuse`…）在 `par_bridge` 上照常生效：命中即返回，剩余元素不再拉取。
- `lock()` 返回 `Err`（锁中毒）说明**别的线程**在持锁期间 panic——按 u2-l5/u6-l4 的机制该 panic 已被池捕获并将在 join 点重放，本线程直接带着现有结果退出即可，无需重复传播。

#### 4.2.4 代码实践

1. **实践目标**：用 `std::io::Lines` 体验「I/O 慢、锁不慢」的推荐场景，再用极廉价的无穷迭代器观察锁竞争。
2. **操作步骤**：

   ```rust
   use rayon::prelude::*;
   use std::fs::File;
   use std::io::{BufRead, BufReader};
   use std::time::Instant;

   fn main() -> std::io::Result<()> {
       // 场景 A：文件行迭代器（next() 内含 I/O）
       let file = File::open("/usr/share/dict/words")?; // 没有就换个大文本文件
       let lines = BufReader::new(file).lines();
       let t = Instant::now();
       let n: usize = lines.par_bridge()
           .map(|l| l.unwrap().len())
           .map(|len| len.saturating_mul(3))
           .sum();
       println!("lines chars≈{n}, elapsed {:?}", t.elapsed());

       // 场景 B：廉价 next() 的无限迭代器 take 后接 par_bridge
       let t = Instant::now();
       let s: u64 = (0..)                     // 无限范围
           .take(10_000_000)                  // take 之后仍是普通迭代器
           .par_bridge()
           .map(|i| (i as u64).wrapping_mul(2654435761))
           .sum();
       println!("infinite-take sum={s}, elapsed {:?}", t.elapsed());

       // 对照组：先收集成 Vec 再走有索引路径
       let v: Vec<u64> = (0..10_000_000u64).collect();
       let t = Instant::now();
       let s2: u64 = v.into_par_iter()
           .map(|i| i.wrapping_mul(2654435761))
           .sum();
       println!("vec sum={s2}, elapsed {:?}", t.elapsed());
       assert_eq!(s, s2);
       return Ok(());
   }
   ```

   用 `cargo run --release` 运行（务必 release，理由见 u1-l3）。
3. **需要观察的现象**：场景 B（`par_bridge` + 廉价 `next()`）的耗时应显著高于对照组（`Vec` + 有索引切分、零锁）；场景 A 若文件够大且处理够重，差距会小得多。
4. **预期结果**：断言 `s == s2` 通过；两条路径耗时差异明显（待本地验证，具体倍数随核数与机器而变）。差异来源正是每元素一次的 `Mutex::lock` + `next()`：对照组里元素被 `bridge` 按区间成批划走，锁次数为零。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `split_count` 初始值是 `current_num_threads()` 而不是更大（比如 `1024`）或 1？

**答案**：预算决定同时拉取的线程数。目标是「每线程一位」——再多人只会排队抢同一把锁，纯粹增加竞争；少于线程数则有空闲工人没活干。另注意 `Splitter` 的窃取自适应（u3-l6）在 `bridge_unindexed` 侧还可能请求更多切分，但预算耗尽后 `split` 返回 `None`，引擎自动退化为串行拉取，不会出错。

**练习 2**：`par_bridge` 能否安全用于 `std::iter::repeat(1)` 这种无限迭代器？配合什么消费者会出问题？

**答案**：能安全启动——不缓冲意味着不会内存爆炸，但有界的消费者才有出口：`.take(...)` 不行（无索引不能用 take，u3-l3），应选 `sum` 有条件？也不行，无限求和不终止。正确姿势是配短路消费者（如 `find_any`）或让迭代器本身有界（`repeat(1).take(n)` 后再 `par_bridge`）。

**练习 3**：`fold_with` 里为什么必须 `drop(iter)` 之后才 `folder.consume(it)`？把 `drop` 挪到 `consume` 之后会怎样？

**答案**：`consume` 执行用户闭包，可能任意慢甚至阻塞；若持锁执行，其余全部线程的 `next()` 都被堵住，并行度塌缩为 1。挪动后功能仍正确（`Mutex` 保证互斥），但「锁只护 `next()`」的设计被破坏，最坏情况退化为串行。

### 4.3 递归防护：threads_started 与 par_bridge_recursion 测试

#### 4.3.1 概念说明

本模块回答一个隐蔽的问题：**如果顺序迭代器的 `next()` 内部自己调用了 rayon，会发生什么？**

设想 `next()` 里跑了一个 `rayon::join`。`join` 把一半工作入队后，本线程先干自己的活，而**别的空闲线程可能窃走入队的那半**——这没问题；危险的是另一种路径：`join` 的执行又触发调度，最终让**同一个线程**递归回到这个 `par_bridge` 的 `fold_with`，再次 `self.iter.lock()`。标准库 `Mutex` 不可重入——同线程二次加锁即死锁。

防护思路：给每个工作线程配一个 `AtomicBool`「本线程已进场」标志。`fold_with` 开头先 `swap(true)`：若发现已是 `true`，说明自己是嵌套递归调用，**直接返回 folder 空手而归**，让最外层那个循环继续拉取即可。同一时刻每个线程只有最外层一份在拉，递归死锁无从发生。

#### 4.3.2 核心流程

```text
fold_with 入口:
  i = current_thread_index()
  若 i = None        → 非池线程（用户主线程 drive），不存在窃取递归，直接进主循环
  若 i = Some(i)     → 查 threads_started[i % len]
      swap 前为 false → 我是本线程最外层，正常拉取
      swap 前为 true  → 我是递归嵌套层，立刻返回，外层继续
```

注意取模 `i % len`：若未来线程池动态扩容，标志数组可能小于实际线程数，多个线程共享一个标志会**误判递归**而提前退出——注释明确说明这「对正确性无碍，只是不够并行」。

#### 4.3.3 源码精读

防护代码：[src/iter/par_bridge.rs:122-138](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/par_bridge.rs#L122-L138)

```rust
// Guard against work-stealing-induced recursion, in case `Iter::next()`
// calls rayon internally, so we don't deadlock our mutex. We might also
// be recursing via `folder` methods, which doesn't present a mutex hazard,
// but it's lower overhead for us to just check this once, rather than
// updating additional shared state on every mutex lock/unlock.
// (If this isn't a rayon thread, then there's no work-stealing anyway...)
if let Some(i) = current_thread_index() {
    let thread_started = &self.threads_started[i % self.threads_started.len()];
    if thread_started.swap(true, Ordering::Relaxed) {
        // We can't make progress with a nested mutex, so just return and let
        // the outermost loop continue with the rest of the iterator items.
        return folder;
    }
}
```

注释披露了两个设计取舍：

- 除了 `next()` 调 rayon，**folder（用户消费者代码）调 rayon** 同样可能递归回到这里。那条路没有锁危险（递归层根本走不到 `lock`），但统一在这里拦一次，比「每次锁操作都更新额外共享状态」便宜——一次检查覆盖两类递归。
- 非池线程（`current_thread_index()` 返回 `None`，`rayon-core/src/thread_pool/mod.rs:438` 定义了该查询）不存在被窃取的可能，直接放行。

对应的回归测试：[tests/par_bridge_recursion.rs:6-31](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/tests/par_bridge_recursion.rs#L6-L31)

```rust
#[test]
#[cfg_attr(any(target_os = "emscripten", target_family = "wasm"), ignore)]
fn par_bridge_recursion() {
    let pool = rayon::ThreadPoolBuilder::new()
        .num_threads(10)
        .build()
        .unwrap();

    let seq: Vec<_> = (0..N).map(|i| (i, i.to_string())).collect();

    pool.broadcast(|_| {
        let mut par: Vec<_> = (0..N)
            .into_par_iter()
            .flat_map(|i| {
                once_with(move || {
                    // Using rayon within the serial iterator creates an opportunity for
                    // work-stealing to make par_bridge's mutex accidentally recursive.
                    rayon::join(move || i, move || i.to_string())
                })
                .par_bridge()
            })
            .collect();
        par.par_sort_unstable();
        assert_eq!(seq, par);
    });
}
```

这个测试是「构造事故现场」的教科书示范，逐层拆解：

- **10 线程池 + `pool.broadcast`**：让每个工作线程都执行一遍外层主体，最大化各线程起点错位，给窃取制造机会。
- **外层 `(0..N).into_par_iter()`**：正常的有索引并行源。
- **`flat_map` 的闭包返回 `once_with(...).par_bridge()`**：这正是 u3-l5 讲过的展平家族——`flat_map` 要求内层是 `ParallelIterator`，于是每个外层元素都造出一个 `par_bridge` 实例。此处 `once_with` 让「串行迭代器的 `next()`」延迟到被拉取时才执行闭包体。
- **串行迭代器的 `next()` 内部调用 `rayon::join`**：关键毒饵。`join` 入队一半工作后，池中其他线程（包括**正持有某个 `IterParallelProducer` 锁的线程**）可能窃取执行；被窃的任务链若又绕回同一线程的另一个（或同一个）`par_bridge`，就会在持锁状态下再次 `lock`。
- **`par_sort_unstable` 后断言**：因为 `par_bridge` 顺序不保证，先排序再与串行基准比对（`N = 100_000`）。
- 顶部 `cfg_attr` 在 emscripten/wasm 上跳过（wasm 单线程环境无窃取可言，且 u1-l2 提过该平台走 `web_spin_lock` 路径）。

测试通过即证明：递归嵌套层被 `threads_started` 拦截后空手返回，最外层循环继续消化剩余元素，最终结果完整、无死锁。

#### 4.3.4 代码实践

1. **实践目标**：亲手制造一次「迭代器内部调 rayon」的递归场景，验证不卡死。
2. **操作步骤**：把下面代码放进示例项目运行：

   ```rust
   use rayon::prelude::*;
   use std::iter::once_with;

   fn main() {
       let n = 20_000;
       let mut par: Vec<(usize, String)> = (0..n)
           .into_par_iter()
           .flat_map(|i| {
               once_with(move || {
                   // next() 被调用时才执行：内部又用了 rayon
                   rayon::join(move || i, move || format!("{i}!"))
               })
               .par_bridge()
           })
           .collect();
       par.par_sort_unstable();
       let seq: Vec<(usize, String)> =
           (0..n).map(|i| (i, format!("{i}!"))).collect();
       assert_eq!(seq, par);
       println!("ok, {} items", par.len());
   }
   ```

3. **需要观察的现象**：程序正常结束、断言通过；若把 `once_with` 中的 `rayon::join` 换成 `(i, format!("{i}!"))` 直接元组（即去掉内部 rayon 调用），行为不变——防护是「允许存在」而非「要求存在」。
4. **预期结果**：打印 `ok, 20000 items`（待本地验证）。思考题自测：试着口头论证「若删掉 `fold_with` 开头的防护段，本程序**可能**死锁但**不必然**死锁」——死锁与否取决于窃取时机，这正是该测试要 10 线程 × 大 N 反复撒网的原因。

#### 4.3.5 小练习与答案

**练习 1**：防护检查用 `swap(true, Relaxed)` 而不是先 `load` 再 `store`，为什么？

**答案**：`swap` 是原子的「读旧值并写新值」，返回修改前的值。若拆成 `load` + `store` 两步，两个嵌套层可能都 `load` 到 `false` 而双双放行，防护失效。`Relaxed` 足够，因为标志本身不护卫其他数据的可见性——正确性只依赖这一个变量的原子修改。

**练习 2**：非池线程（例如在 `main` 里直接驱动消费者）需要防护吗？源码如何区分？

**答案**：不需要——窃取只发生在池的工作线程之间，用户线程不会被窃走而重入。源码用 `current_thread_index()` 返回 `Option` 区分：`Some(i)` 是池线程走防护，`None` 直接进主循环（[src/iter/par_bridge.rs:128](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/par_bridge.rs#L128)）。

**练习 3**：测试里为什么用 `once_with` 包住 `rayon::join`，而不是直接 `std::iter::once((i, i.to_string()))`？

**答案**：`once(...)` 的参数在**构造迭代器时**就求值，`rayon::join` 会在 `flat_map` 闭包里立刻执行，那时 `par_bridge` 尚未开始拉取、锁未持有，构不成递归。`once_with` 把求值推迟到 `next()` 被调用的瞬间——也就是恰好在 `fold_with` 持锁路径上执行，才真正踩中危险窗口。

## 5. 综合实践

把规格里的实践任务完整落地——「文件行迭代器 + 无限迭代器 take」双场景对比，并解释锁竞争来源：

1. **实践目标**：量化两类负载下 `par_bridge` 的锁竞争影响，并用线程 ID 观察多线程同时拉取。
2. **操作步骤**：

   ```rust
   use rayon::prelude::*;
   use std::io::{BufRead, BufReader};
   use std::time::Instant;

   fn main() -> std::io::Result<()> {
       // 准备一个较大的文本（没有词典就用程序生成临时文件）
       let path = "/usr/share/dict/words";
       let f = std::fs::File::open(path)
           .or_else(|_| {
               let p = "/tmp/rayon_words.txt";
               std::fs::write(p, "hello rayon\n".repeat(200_000))?;
               std::fs::File::open(p)
           })?;

       // 场景一：io::Lines —— next() 含 I/O，锁占比小
       let t = Instant::now();
       let (total, seen): (usize, usize) = BufReader::new(f)
           .lines()
           .par_bridge()
           .map(|l| {
               let l = l.unwrap();
               // 故意做点重活，模拟真实处理
               (l.bytes().map(|b| b as usize).sum::<usize>(), 1)
           })
           .reduce(|| (0, 0), |a, b| (a.0 + b.0, a.1 + b.1));
       println!("[lines ] chars={total} lines={seen} {:?}", t.elapsed());

       // 场景二：无限迭代器 take —— next() 极廉价，锁占比大
       let t = Instant::now();
       let s: u64 = std::iter::successors(Some(1u64), |x| Some(x.wrapping_add(1)))
           .take(20_000_000)
           .par_bridge()
           .filter(|x| x % 2 == 0)
           .sum();
       println!("[take  ] sum={s} {:?}", t.elapsed());

       // 场景三：同样的计算走有索引路径做对照
       let t = Instant::now();
       let s2: u64 = (1..=20_000_000u64)
           .into_par_iter()
           .filter(|x| x % 2 == 0)
           .sum();
       println!("[range ] sum={s2} {:?}", t.elapsed());
       assert_eq!(s, s2);

       // 附加观察：谁在拉元素？
       let ids: Vec<_> = std::iter::repeat_with(|| {
           let id = rayon::current_thread_index();
           std::thread::sleep(std::time::Duration::from_micros(50));
           id
       })
       .take(64)
       .par_bridge()
       .collect();
       let mut distinct: Vec<_> = ids.into_iter().flatten().collect();
       distinct.sort_unstable();
       distinct.dedup();
       println!("threads seen: {distinct:?}");
       Ok(())
   }
   ```

   `cargo run --release` 运行多次。
3. **需要观察的现象**：
   - 场景二（廉价 `next()`）明显慢于场景三（有索引、无锁）——每元素一次 `Mutex::lock` 的串行化是唯一差异来源；
   - 场景一因为 `next()` 含 I/O、处理较重，锁在总耗时中占比小，惩罚不明显；
   - 附加观察里出现多个不同的线程编号，证明多个工作线程确实同时在拉。
4. **预期结果**：`assert_eq!` 通过；「take 慢于 range」的差距随核数增加而扩大（核越多抢锁越凶）（待本地验证）。解释要点：`par_bridge` 的临界区虽只含 `next()`，但每个元素都要进一次；场景三中元素按区间整批划给线程，同步次数为零。当 \( s = \frac{\text{锁与 next 开销}}{\text{单元素总开销}} \) 趋近 1 时，加速比上限 \( 1/s \) 趋近 1。

## 6. 本讲小结

- `ParallelBridge` 以 blanket impl 覆盖一切 `Iterator<Item: Send> + Send`，`par_bridge()` 是零成本包装——这是「任意数据源接入并行世界」的统一后门。
- `IterParallelProducer` 是极简的 `UnindexedProducer`：不切分数据，只维护「并发拉取预算」（`split_count`，初始为线程数）；真正的耗尽信号由 `fold_with` 里 `next()` 返回 `None` 给出，`.fuse()` 保证多线程反复试探的安全。
- 「锁保护的取元素协议」：临界区内只做 `next()`，用户代码（`folder.consume`）在锁外执行；`folder.full()` 让短路消费者照常工作；锁中毒直接退出，panic 由池在 join 点重放。
- 性能画像是双刃：无缓冲使其可安全处理无限迭代器，但每元素一次锁 + `next()` 使廉价迭代器成为瓶颈——适合 channel、文件、网络 I/O 这类 `next()` 本身就慢的源，不适合纯内存海量小元素（先 `collect` 成 `Vec` 再 `par_iter` 更优）。
- 顺序不保证、无索引能力：不能 `enumerate`/`zip`，`collect` 走无长度慢路径（u2-l4）。
- `fold_with` 开头的 `threads_started` 检查防范「`next()` 或消费者内部调 rayon + 工作窃取」导致的同线程重入死锁；嵌套层空手返回，最外层继续拉取。`tests/par_bridge_recursion.rs` 用 `once_with` 把 `rayon::join` 延迟到 `next()` 时刻构造出这种现场。

## 7. 下一步学习建议

单元四（plumbing 内核）到此完结。你已经掌握了 Producer/Consumer 卺议的全貌与两个极端案例（精确切分的 `collect`、完全不切分的 `par_bridge`）。接下来两条路径任选：

- **走向调度内核（单元五）**：本讲反复出现的 `join_context`、工作窃取、`current_thread_index` 都在 rayon-core 中实现。建议从 u5-l1（`join` 原语）开始，再到 u5-l4（工作窃取队列）——你会看到「窃取」如何具体发生，从而更深刻理解本讲的递归风险从何而来。
- **先做扩展实践（单元九）**：若想趁热打铁写代码，u9-l2 将带你实现自定义 `UnindexedProducer` 的按值切分，与本讲的「不切分生产者」形成对照。

延伸阅读：`src/iter/par_bridge.rs` 的文档注释本身值得一字不读漏；`bridge_unindexed` 引擎（`src/iter/plumbing/mod.rs:438-476`）建议配合 u3-l6 的 `Splitter` 预算分析再读一遍。
