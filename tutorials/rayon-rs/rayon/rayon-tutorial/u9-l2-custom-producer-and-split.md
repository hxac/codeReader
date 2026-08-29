# 自定义 Producer 与 Split 扩展

## 1. 本讲目标

上一讲（u9-l1）我们以 `RepeatN` 为范本，走通了「索引并行迭代器」的全链路实现：长度已知、`split_at(index)` 按下标二分、`bridge` 精确切分。但很多真实数据并没有廉价的中点下标——链表的中点要走 O(n) 步、树的子树根本没有「下标」、按分隔符切字符串时切分点必须落在分隔符边界上。

学完本讲，你应该能够：

1. **区分两种切分世界观**：按索引切分（`Producer::split_at`，切分点由框架给定）与按值切分（`UnindexedProducer::split`，切分点由数据自己决定）。
2. **为无索引数据结构实现 `UnindexedProducer`**：写出 `split()`（掰成两半、分不动返回 `None`）与 `fold_with`（串行吃掉自己那份元素），并配上 `drive_unindexed` + `bridge_unindexed` 让自定义类型获得 `into_par_iter`。
3. **用测试套住自定义实现**：读懂 `tests/producer_split_at.rs` 的「三刀四段」穷举为何只适用于索引生产者，并为无索引生产者设计等价的递归穷举测试。

先澄清一个容易混淆的命名（u3-l6 已提过，这里正式展开）：**Rayon 中没有一个叫 `Split` 的 trait**。`rayon::iter::split` 是一个函数、`Split` 是它返回的迭代器结构体；真正的扩展点是 `UnindexedProducer` trait 加上一个 `Fn(D) -> (D, Option<D>)` 形状的切分闭包。本讲会把这两层都拆开看。

## 2. 前置知识

- **Producer 契约（u4-l2、u9-l1）**：`Producer` 是「可切分的 IntoIterator」，核心成员 `into_iter`、`split_at(index)`、粒度窗口 `min_len`/`max_len`；长度由框架从 `IndexedParallelIterator::len()` 记账，生产者与消费者在同一 `mid` 上对齐切分。
- **bridge 递归（u4-l1、u4-l3）**：引擎先查 `full()` 短路，再由切分预算裁决是否再切，用 `join_context` 并行两半（工作窃取在此发生），叶子处生产者转串行迭代器喂给 `Folder`。
- **bridge_unindexed 与 Splitter（u3-l6、u4-l3）**：无索引路径的递归骨架是 `split → join_context → reduce`；`Splitter` 预算从线程数起步、逐刀减半、被窃取时重置，因此最终碎片数约为「不小于线程数的最小 2 的幂」。
- **Folder（u4-l3）**：串行折叠器，三个必需成员 `consume`、`complete`、`full`，另带默认实现的 `consume_iter`。
- **plumbing 是公开 API**：[src/iter/mod.rs:91](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L91) 里 `pub mod plumbing` 把这些 trait 暴露为「水管工程」接口——官方不承诺与标准库迭代器那样稳定，但它是自定义并行迭代器的唯一正规入口。集成测试第一行 `use rayon::iter::plumbing::*;` 就是证明。

术语速查：**按值切分（split by value / by capability）**——不借助下标，把数据本身掰成两个各自完整的碎片；**frontier（待探索前沿）**——walk_tree 中尚未访问的节点栈；**穷举切分测试**——把所有可能的切分组合都跑一遍、断言拼接后等于原序列的测试手法。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [src/iter/plumbing/mod.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs) | 定义 `Producer`、`UnindexedProducer`、`Folder`、`Splitter` 与 `bridge_unindexed` 引擎，是本讲的契约源头 |
| [src/iter/splitter.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/splitter.rs) | 公共函数 `rayon::iter::split`（数据 + 切分闭包 → 并行迭代器）及其内部 `SplitProducer`，最通用的按值切分模板 |
| [src/split_producer.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/split_producer.rs) | `str`/`[T]` 按**分隔符**并行的内部生产器（`par_split` 的后端），领域特定按值切分的第一个样本：切分点被数据约束 |
| [src/iter/walk_tree.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/walk_tree.rs) | 树遍历生产器（prefix/postfix），第二个样本：带状态、还要维持输出顺序的按值切分 |
| [tests/producer_split_at.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/tests/producer_split_at.rs) | 索引生产者的「三刀四段」穷举测试骨架，本讲参照它为无索引生产者设计测试 |

## 4. 核心概念与源码讲解

### 4.1 两种切分世界观：`split_at(index)` 与 `split()`

#### 4.1.1 概念说明

上一讲实现的 `RepeatN` 走索引路线：框架知道总长 `N`，每次取中点 `mid`，然后把 `mid` **同时**交给生产者（`split_at(mid)`）和消费者（`Consumer::split_at(mid)`），两边切在同一个位置，结果才能按序归并。

但对链表、树、按分隔符切分的字符串这类数据，「给定下标切一刀」要么做不到 O(1)，要么根本没有意义（比如分隔符不在这刀上）。于是 Rayon 提供第二条路线：**你不需要知道长度，也不需要接受下标——框架只问一句「能不能把自己掰成两半？」**能掰就返回 `(左, Some(右))`，掰不动就返回 `(自己, None)`，框架随即放弃切分、让这个碎片串行执行完。

这就是 `UnindexedProducer` 文档注释里那句 "you just ask them to split 'somewhere'" 的含义——切分点由数据自身的能力决定（这也是 u3-l6 称之为「按能力切分」的原因）。

#### 4.1.2 核心流程

两条路线在引擎层的分岔：

```
索引路线（bridge）                    无索引路线（bridge_unindexed）
─────────────────────                ─────────────────────
len = par_iter.len()                 splitter = Splitter::new()   // 预算=线程数
mid = 选定切分点(考虑 min/max_len)     splitter.try_split(migrated)?
producer.split_at(mid)               ├─ 是 → producer.split()
consumer.split_at(mid)               │      ├─ Some(right) → join_context(左, 右) → reduce
join_context(左, 右) → reduce        │      └─ None → fold_with(into_folder).complete()
                                     └─ 否 → fold_with(into_folder).complete()
```

关键差异：索引路线的切分点由框架单方面决定，生产者必须服从；无索引路线框架只有「要不要再切」的预算决策，**切在哪里完全由生产者的 `split()` 说了算**。两侧对照表：

| 维度 | `Producer`（索引） | `UnindexedProducer`（无索引） |
| --- | --- | --- |
| 切分方法 | `split_at(self, index: usize) -> (Self, Self)` | `split(self) -> (Self, Option<Self>)` |
| 切分点 | 框架给定，必须服从 | 自己决定，返回 `None` 即「分不动」 |
| 长度 | 固定且已知（框架记账） | 未知，或无法用 `usize` 表示 |
| 串行消费 | `into_iter()` → 双端 + 精确长度迭代器 | `fold_with(folder)` 直接喂 `Folder` |
| 粒度控制 | `min_len` / `max_len` | 无，粒度只由 `Splitter` 预算控制 |
| 驱动引擎 | `bridge` | `bridge_unindexed` |
| 上层 trait | `IndexedParallelIterator`（`drive`/`len`/`with_producer`） | `ParallelIterator`（`drive_unindexed`/`opt_len`） |
| 代价 | 可用 `zip`/`enumerate`，`collect` 走精确预分配快速路径 | 无索引能力，`collect` 走「分块收集再拼接」路径 |

#### 4.1.3 源码精读

先看索引侧的 `Producer` 契约（上讲已精读，这里只看签名对照）：

> [src/iter/plumbing/mod.rs:56-66](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L56-L66) 定义 `Producer` trait：关联类型 `Item` 与 `IntoIter`（必须 `Iterator + DoubleEndedIterator + ExactSizeIterator`），以及「不再并行切分」时转成串行迭代器的 `into_iter`。

> [src/iter/plumbing/mod.rs:95-97](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L95-L97) `split_at(self, index)`：「一个生产 `0..index`，另一个生产 `index..N`」，两半**必须**存在，`index ≤ N`。

再看无索引侧的完整契约，只有两个必需方法：

```rust
pub trait UnindexedProducer: Send + Sized {
    type Item;

    /// Split midway into a new producer if possible, otherwise return `None`.
    fn split(self) -> (Self, Option<Self>);

    /// Iterate the producer, feeding each element to `folder`, and
    /// stop when the folder is full (or all elements have been consumed).
    fn fold_with<F>(self, folder: F) -> F
    where
        F: Folder<Self::Item>;
}
```

> [src/iter/plumbing/mod.rs:223-243](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L223-L243) `UnindexedProducer` 的定义。文档注释点破设计动机：这些生产器「不知道或无法用 `usize` 表示自己的精确长度」，所以不能被要求在指定点切分，只能被要求「在某处切一刀」；并解释了为什么 `Producer` 不去继承这个 trait——那会要求生产器自带长度。

实现者的两条义务：

1. `split()`：把 `self` 按值消费，返回左半与「可能的右半」。**左半的元素在迭代顺序上必须排在右半之前**（这是 `reduce` 能保持顺序的前提，4.4 节测试就靠它）。
2. `fold_with()`：把**自己所有**元素喂给 `folder`（可用 `consume` 逐个喂，或用 `consume_iter` 批量喂），中途若 `folder.full()` 应及时停止。

再看引擎怎么用它：

> [src/iter/plumbing/mod.rs:437-476](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L437-L476) `bridge_unindexed` 创建初始 `Splitter`（预算 = 线程数）后进入递归：先查 `consumer.full()` 短路；预算允许再切时调 `producer.split()`——拿到 `Some(right)` 就 `consumer.split_off_left()` 克隆出左消费者、用 `join_context` 并行两半（`context.migrated()` 把「这一半是否被窃取」反馈给 `Splitter` 重置预算）、最后 `reduce` 合并；拿到 `None` 或预算耗尽就 `fold_with(into_folder()).complete()` 串行收尾。

> [src/iter/plumbing/mod.rs:251-284](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L251-L284) `Splitter` 的 thief-splitting 策略：`splits` 字段记录「大约还想切几刀」，初始为 `current_num_threads()`；被窃取时重置为 `max(线程数, splits/2)`，未被窃取且预算有余则减半继续切。所以从实现者视角看：`split()` 总共被调用约 \(\lceil \log_2 P \rceil\) 轮（\(P\) 为线程数），最终碎片数是不小于 \(P\) 的最小 2 的幂——**你不必数着线程数写切分逻辑，只需保证分不动时诚实返回 `None`**。

#### 4.1.4 代码实践

**实践目标**：直观感受「`split()` 返回 `None` 时框架不再纠缠」，理解切分次数与线程数同阶、与元素数无关。

**操作步骤**（示例代码，写在独立 Cargo 项目的 `main.rs` 中）：

```rust
use rayon::iter::plumbing::{Folder, UnindexedProducer};
use rayon::iter::plumbing::bridge_unindexed; // 实际请用一行 use rayon::iter::plumbing::*;

// 一个只会打印日志的假生产器：切分时打印，串行消费时打印
struct LoggingProducer {
    items: Vec<u32>,
}

impl UnindexedProducer for LoggingProducer {
    type Item = u32;

    fn split(self) -> (Self, Option<Self>) {
        if self.items.len() < 2 {
            println!("split -> None (len={})", self.items.len());
            (self, None) // 分不动：只有 0 或 1 个元素
        } else {
            let mid = self.items.len() / 2;
            let mut left = self;
            let right_items = left.items.split_off(mid);
            println!("split -> {} | {}", left.items.len(), right_items.len());
            (left, Some(LoggingProducer { items: right_items }))
        }
    }

    fn fold_with<F>(self, mut folder: F) -> F
    where
        F: Folder<Self::Item>,
    {
        for i in self.items {
            folder = folder.consume(i);
        }
        folder
    }
}

fn main() {
    // 直接驱动需要手写消费者，这里改用现成的并行迭代器观察等价行为：
    // bridge_unindexed 的完整用法见 4.4 节与综合实践
    let _ = LoggingProducer { items: (0..1024).collect() };
    println!("本实践的实际观测版见下方 with_max_len 实验");
}
```

直接调 `bridge_unindexed` 需要一个 `UnindexedConsumer`，手写它超出本模块目标。更省事的观测方法是借用现成迭代器的粒度控制（示例代码）：

```rust
use rayon::prelude::*;

fn main() {
    let n = (0..1_000_000_u64).into_par_iter()
        .inspect(|_| {})      // 占位闭包，便于打断点
        .with_max_len(1)      // 强制切到底，制造海量碎片
        .map(|x| x)
        .sum::<u64>();
    println!("{n}");
}
```

**需要观察的现象**：用 `RAYON_NUM_THREADS=4 cargo run --release` 与 `RAYON_NUM_THREADS=8 cargo run --release` 分别运行（环境变量须在程序外设置才能赶在全局线程池初始化之前，见 u5-l3），再去掉 `with_max_len(1)` 对比——程序的**结果**不变，只有耗时变化。

**预期结果**：切分粒度与线程数只影响性能、不影响正确性——这正是 `split()` 实现者「只需诚实返回 `None`」的底层保证。切分次数的精确观测（打日志统计 `split()` 调用次数）请做综合实践，那里会真正实现 `ParallelIterator`。日志版输出为「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：`split_at` 必须返回两个生产器，`split` 却允许返回 `None`。为什么索引路线不需要「分不动」的信号？

**答案**：索引路线中长度由框架从 `len()` 记账，框架在切分前就知道剩余长度，切到足够小自然停手（`LengthSplitter` 还会检查 `len / 2 >= min`），生产者永远能把已知下标切下去。无索引路线中框架**不知道**剩余量，「还能不能分」这条信息只存在于数据自己手里，只能由 `split()` 返回 `None` 告知（这正是 trait 文档 "otherwise return `None`" 的语义）。

**练习 2**：如果 `split()` 实现有 bug，返回的右半元素在原顺序上其实排在左半之前，哪个并行操作会立刻暴露错误？

**答案**：`collect`（以及 `collect_into_vec`）。`bridge_unindexed` 把左半结果作为 `reduce` 的左参数、右半作为右参数，顺序错了收集出的 `Vec` 顺序就错；而 `sum` 这类可交换归约不会暴露（结果碰巧仍正确）。这就是 4.4 节要用「拼接比对」而不是「求和比对」做测试的原因。

**练习 3**：`UnindexedProducer` 要求 `Self: Send`，但没要求 `Self: Sync`。为什么？

**答案**：生产器按值传递、每次切分把所有权分给两个新生产器，任一时刻每个生产器只被一个线程持有，跨线程移动只需 `Send`；而 `Sync`（允许多线程共享引用 `&T`）在这个协议里从不发生——共享只发生在切分闭包等外部借用上（见 4.2 的 `S: Sync` 约束）。

### 4.2 `rayon::iter::split`：按值切分的通用模板

#### 4.2.1 概念说明

如果不想到处手写 `UnindexedProducer`，Rayon 提供了一个「万能适配口」：`rayon::iter::split(data, splitter)`。你给出任意数据 `D` 和一个知道怎么掰开它的闭包，它就返回一个 `ParallelIterator`——**其 `Item` 就是碎片 `D` 本身**（不是碎片里的元素）。

它把「怎么切」从 trait 实现降维成了一个普通闭包 `Fn(D) -> (D, Option<D>) + Sync`：

- 输入：一个碎片；
- 输出：左碎片 + 可能的右碎片（`None` 表示分不动）；
- `+ Sync` 因为多个线程会同时拿着它的引用去切各自的碎片。

这是 Rayon 里表达「分治并行」的最短路径：切分逻辑一行闭包，递归、任务派发、负载均衡全部交给 `bridge_unindexed`。文档注释给了两个现成例子（一维范围对半切、二维块按长边切），本节就看一维版。

#### 4.2.2 核心流程

```
iter::split(data, splitter)
  └─ Split { data, splitter }                 // 惰性，什么都不做
       └─ drive_unindexed(consumer)           // 消费者到来
            └─ SplitProducer { data, splitter: &splitter }
                 └─ bridge_unindexed(producer, consumer)
                      ├─ split():  (left, right) = splitter(self.data)
                      │            self.data = left          // 自己变左半
                      │            right → 新的 SplitProducer // Some 时
                      └─ fold_with(folder): folder.consume(self.data)
                                                 // 碎片即元素：整块交给下游
```

注意两个「一行实现」：`split()` 只是转调用户闭包；`fold_with` 只 `consume` 一次——因为对 `SplitProducer` 来说「元素」就是「碎片」本身，串行消费一块数据就是把它整个交给消费者（比如 `for_each` 拿到的就是一个子范围、子树或子链表）。

#### 4.2.3 源码精读

```rust
pub fn split<D, S>(data: D, splitter: S) -> Split<D, S>
where
    D: Send,
    S: Fn(D) -> (D, Option<D>) + Sync,
{
    Split { data, splitter }
}
```

> [src/iter/splitter.rs:105-111](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/splitter.rs#L105-L111) 公共入口 `iter::split`：两个约束 `D: Send`（碎片要能流向任意线程）与 `S: Fn(D) -> (D, Option<D>) + Sync`（切分器被多线程共享调用）。返回的 `Split` 只是数据加闭包的包装（[splitter.rs:113-119](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/splitter.rs#L113-L119)）。

文档里的一维范围例子值得逐行读：

```rust
fn split_range1(r: Range1D) -> (Range1D, Option<Range1D>) {
    if r.end - r.start <= 1 { return (r, None); }
    let midpoint = r.start + (r.end - r.start) / 2;
    (r.start..midpoint, Some(midpoint..r.end))
}
```

> [src/iter/splitter.rs:22-30](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/splitter.rs#L22-L30) 这就是全部切分逻辑：只有一个点时诚实返回 `None`，否则对半切。随后 `iter::split(0..4096, split_range1).for_each(...)` 断言最终每个子范围长度都是 2 的幂——因为 `Splitter` 预算逐刀减半，恰好切出 2 的幂个碎片。

接着看它如何 fulfill `ParallelIterator`：

```rust
impl<D, S> ParallelIterator for Split<D, S>
where
    D: Send,
    S: Fn(D) -> (D, Option<D>) + Sync + Send,
{
    type Item = D;

    fn drive_unindexed<C>(self, consumer: C) -> C::Result
    where
        C: UnindexedConsumer<Self::Item>,
    {
        let producer = SplitProducer {
            data: self.data,
            splitter: &self.splitter,
        };
        bridge_unindexed(producer, consumer)
    }
}
```

> [src/iter/splitter.rs:127-144](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/splitter.rs#L127-L144) `Item = D`（碎片即元素）；`drive_unindexed` 把闭包的引用借给生产器后直接 `bridge_unindexed`。这是**所有**无索引自定义迭代器的标准驱动写法——记住这个形状，综合实践会照抄。

```rust
impl<'a, D, S> UnindexedProducer for SplitProducer<'a, D, S>
where
    D: Send,
    S: Fn(D) -> (D, Option<D>) + Sync,
{
    type Item = D;

    fn split(mut self) -> (Self, Option<Self>) {
        let splitter = self.splitter;
        let (left, right) = splitter(self.data);
        self.data = left;
        (self, right.map(|data| SplitProducer { data, splitter }))
    }

    fn fold_with<F>(self, folder: F) -> F
    where
        F: Folder<Self::Item>,
    {
        folder.consume(self.data)
    }
}
```

> [src/iter/splitter.rs:146-171](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/splitter.rs#L146-L171) 通用生产器：`split` 先取出共享的闭包引用，调用用户闭包得到两半，`self` 保留左半（`mut self` + 重新赋值实现「复用自己」），右半包成新生产器；`fold_with` 一行 `folder.consume(self.data)`。

#### 4.2.4 代码实践

**实践目标**：用 `iter::split` 让 `LinkedList` 获得并行处理能力，体会「零 plumbing 代码」的便利与「碎片即元素」的别扭。

**操作步骤**（示例代码）：

```rust
use rayon::iter;
use rayon::prelude::*;
use std::collections::LinkedList;

fn split_list(mut l: LinkedList<u64>) -> (LinkedList<u64>, Option<LinkedList<u64>>) {
    if l.len() < 2 {
        (l, None)
    } else {
        // split_off(at) 把 [at, len) 摘出去，self 留下 [0, at)
        let right = l.split_off(l.len() / 2);
        (l, Some(right))
    }
}

fn main() {
    let mut list: LinkedList<u64> = (0..1000).collect();
    // 注意：Item 是 LinkedList 碎片，不是 u64！
    let total: u64 = iter::split(list.clone(), split_list)
        .map(|chunk| chunk.into_iter().sum::<u64>()) // 碎片内部自己串行求和
        .sum();
    let expect: u64 = list.iter().sum();
    assert_eq!(total, expect);
    println!("ok {total}");
}
```

**需要观察的现象**：`map` 的闭包参数类型是 `LinkedList<u64>`（一整个碎片），不是单个 `u64`；修改 `split_list` 里的切分点（比如改成 `l.len() / 4`）结果不变。

**预期结果**：打印 `ok 499500`。碎片数取决于线程数而非元素数；内层 `into_iter().sum()` 是串行的——这正是 `iter::split` 的定位：**块间并行、块内串行**。若想让每个元素都有机会被不同线程处理，需要真正的 `UnindexedProducer`（综合实践）。

#### 4.2.5 小练习与答案

**练习 1**：`iter::split` 产出的迭代器为什么不能调用 `.enumerate()`？

**答案**：`enumerate` 定义在 `IndexedParallelIterator` 上（u3-l3），而 `Split` 只实现了 `ParallelIterator`——它的碎片在求值前根本不存在，长度无从得知。这也是 4.1 对照表最后一行的体现。

**练习 2**：把 `split_list` 的守卫条件从 `l.len() < 2` 改成 `l.len() < 8` 会发生什么？

**答案**：程序仍正确，只是并行度可能下降——碎片最少 8 个元素，若数据总量小、线程数多，`Splitter` 预算还没用完数据已经切不动（`split` 返回 `None`），部分线程拿不到独立碎片。这相当于手工版的 `with_min_len` 粒度控制，只是没有索引版本那么精确。

**练习 3**：切分闭包约束是 `Fn(D) -> (D, Option<D>) + Sync`，而 `Split<D, S>` 的 `ParallelIterator` 实现额外要求 `S: Send`。`Sync` 和 `Send` 各自为哪一步服务？

**答案**：切分发生在多个工作线程上，但每个 `SplitProducer` 只持有 `&'a S` 共享引用，多线程同时调用需要 `Sync`；而 `SplitProducer: Send`（trait 要求）意味着其字段 `&'a S` 也要能跨线程移动，引用跨线程移动需要 `S: Send`。两个约束各管一段所有权路径。

### 4.3 领域特定的按值切分样本：`par_split` 与 `walk_tree`

#### 4.3.1 概念说明

`iter::split` 的切分闭包可以随便选切分点，但真实数据源常常**没有选点自由**：

- **字符串/切片按分隔符切分**（`par_split` 的后端）：切分点必须落在「分隔符出现的位置」，否则左右两半就不再是完整的子串序列；
- **树遍历**（`walk_tree`）：切分点只能落在「待访问节点栈」的某个边界上，而且切完还得保证前序/后序的输出顺序不被打乱。

这两个仓库内样本展示 `UnindexedProducer` 的两门进阶功课：**受限切分点**（在中点附近搜索合法位置，找不到就回退）与**带状态的切分**（生产器携带 frontier/seen 两个栈，切分时要正确分家）。

#### 4.3.2 核心流程

`par_split` 的 `SplitProducer::split()` 决策树：

```
mid = data.midpoint(tail)            // 先瞄准中点附近
index = data.find(分隔符, mid, tail)  // 向前找最近的分隔符
      ?? data.rfind(分隔符, mid)      // 找不到 → 向后回退找
if let Some(index) = index:
    在 index 处 split_once → (left, right)
    维护两侧的 tail（已确认无分隔符的尾部边界）
    返回 (left, Some(right))
else:
    返回 (自己但 tail=0, None)        // 整段无分隔符，分不动
```

`walk_tree` 的 `WalkTreePrefixProducer::split()` 三步：

```
1. while to_explore 只剩 1 个节点:        // 展开「独子」
      弹出该节点，其孩子压回 to_explore（逆序，保前序）
      该节点移入 seen（已产出，等待消费）
2. split_vec(&mut to_explore)             // 前沿对半分：右半出走
      Some(右半) → 右生产器 = { to_explore: 右半, seen: 空 }
                   左生产器保留 seen + 左半（前序：祖先先产出，归左）
3. 前沿分不动(len≤1) → 退而求其次 split_vec(&mut seen)
      Some(右半 seen) → 右生产器 = { to_explore: 空, seen: 右半 }
```

postfix 变体的差别在第 2 步：祖先**最后**产出，所以右生产器要**带走** `seen`（`std::mem::take`），保证右半整体排在左半之后——同一个「左前右后」契约，方向相反的搬运。

#### 4.3.3 源码精读

```rust
pub(super) struct SplitProducer<'p, P, V, const INCL: bool = false> {
    data: V,
    separator: &'p P,
    /// Marks the endpoint beyond which we've already found no separators.
    tail: usize,
}
```

> [src/split_producer.rs:8-16](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/split_producer.rs#L8-L16) 生产器持有数据 `V`、共享的分隔符引用，以及关键字段 `tail`：**「已确认不含分隔符的尾部起点」**。这是给「按能力切分」做的记忆优化——一旦确认某段尾部没有分隔符，后续切分就不再搜索它。

> [src/split_producer.rs:19-29](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/split_producer.rs#L19-L29) 辅助 trait `Fissile<P>`：让 `&str`、`&[T]`、`&mut [T]` 三种数据共用同一个 `SplitProducer`，各自实现 `length`/`midpoint`/`find`/`rfind`/`split_once`。这是「泛型生产器 + 小trait 抽象数据操作」的复用手法。

```rust
fn split(self) -> (Self, Option<Self>) {
    // Look forward for the separator, and failing that look backward.
    let mid = self.data.midpoint(self.tail);
    let index = match self.data.find(self.separator, mid, self.tail) {
        Some(i) => Some(mid + i),
        None => self.data.rfind(self.separator, mid),
    };

    if let Some(index) = index {
        // ...在 index 处 split_once，并维护两侧 tail...
        (left, Some(right))
    } else {
        // The search is exhausted, no more separators...
        (SplitProducer { tail: 0, ..self }, None)
    }
}
```

> [src/split_producer.rs:104-144](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/split_producer.rs#L104-L144) 完整的 `split` 实现：先在中点向前找分隔符、找不到向后回退（注释点明 "Look forward ... and failing that look backward"），找到就在该处一分为二并按「向前找到/向后回退」两种情形维护左右 `tail`（向后回退时右侧已确认无分隔符，`right_tail = 0`）；整段找不到分隔符时返回 `None` 且把 `tail` 清零——注意此时**数据原封不动**，只是宣告「分不动」，`fold_with` 会把它当一整段消费。

> [src/split_producer.rs:63-94](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/split_producer.rs#L63-L94) `fold_with` 的串行收尾：若 `tail == length`（从未发现过分隔符），整段交给 `fold_splits`；否则先反向定位最后一个分隔符，把尾段单独 `consume`，前缀继续分治——这段展示了「串行消费也可以很讲究」，`tail` 记忆让叶子任务避免重复扫描。

再看树的生产器：

```rust
struct WalkTreePrefixProducer<'b, S, B> {
    to_explore: Vec<S>, // nodes (and subtrees) we have to process
    seen: Vec<S>,       // nodes which have already been explored
    children_of: &'b B, // function generating children
}
```

> [src/iter/walk_tree.rs:5-9](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/walk_tree.rs#L5-L9) 双栈结构：`to_explore` 是待访问前沿，`seen` 是已访问但尚未交给消费者的节点。生产器**有状态**，切分就是把这两个栈正确地分家。

> [src/iter/walk_tree.rs:19-48](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/walk_tree.rs#L19-L48) `split` 的三步：`while to_explore.len() == 1` 循环展开独子节点（孩子 `rev()` 压回以保前序）；然后 `split_vec(&mut to_explore)` 把前沿对半分，右半成为新生产器（`seen` 从空开始）；前沿分不动时降级去分 `seen`。两步取右半都失败才返回 `None`——**树深再大、只要宽度足够就还能切**，这是它对付退化成链的树的关键。

> [src/iter/walk_tree.rs:329-336](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/walk_tree.rs#L329-L336) 工具函数 `split_vec`：长度 ≤1 返回 `None`，否则 `v.split_off(n)` 把后半摘走——「按值对半」的最小实现，可直接抄进自己的代码。

> [src/iter/walk_tree.rs:232-263](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/walk_tree.rs#L232-L263) postfix 版的 `split`：结构相同，唯一差别是成功分出右半时 `std::mem::take(&mut self.seen)` 让**右**生产器带走全部祖先（后序中祖先最后产出，必须整体归右），对照 prefix 版右半 `seen: Vec::new()`——同一契约的方向感知实现，u3-l4 讲过的「方向感知 Reducer」在数据源侧的镜像。

> [src/iter/walk_tree.rs:50-69](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/walk_tree.rs#L50-L69) `fold_with` 串行收尾：先 `consume_iter(self.seen)` 把已产出节点交给消费者，再循环弹出 `to_explore` 逐个访问（每弹一个就展开其孩子），两处 `folder.full()` 检查保证短路传播。

#### 4.3.4 代码实践

**实践目标**：观察「受限切分点」对并行度的实际影响，理解 `split()` 返回 `None` 不是失败而是常态。

**操作步骤**（示例代码，在 `--release` 下运行）：

```rust
use rayon::prelude::*;

fn main() {
    // 构造 100_000 个数字、每 1000 个插一个逗号：合法切分点很多
    let many: String = (0..100_000)
        .map(|i| if i % 1000 == 999 { ',' } else { 'x' })
        .collect();
    // 只有一个分隔符在正中间：合法切分点只有 1 个
    let one: String = format!("{}{}", "x".repeat(50_000), ",");
    // 完全没有分隔符：一个合法切分点都没有
    let none: String = "x".repeat(100_000);

    for (name, s) in [("many", &many[..]), ("one", &one[..]), ("none", &none[..])] {
        let t = std::time::Instant::now();
        let parts: Vec<&str> = s.par_split(',').collect();
        println!("{name}: {} parts in {:?}", parts.len(), t.elapsed());
    }
}
```

**需要观察的现象**：三个用例的 `parts` 长度分别为 101、2、1；耗时上 `many` 明显最快（多个线程各切一段），`none` 就是纯串行扫描——因为它的 `split()` 第一次调用就返回 `None`。

**预期结果**：`many` 切出多个碎片并行扫描；`one` 只能在唯一的分隔符处切一刀；`none` 一次都切不动。具体耗时数字「待本地验证」，但**三者的碎片数**可以从输出直接推断。这解释了 4.1 节的契约设计：切分能力是数据赋予的，框架只负责利用。

#### 4.3.5 小练习与答案

**练习 1**：`SplitProducer` 找切分点时「先向前、失败再向后」，为什么不直接用 `find` 的第一个结果？

**答案**：向前找是**从 mid 向尾**找（`find(separator, mid, tail)` 的区间是 `[mid, tail)`），保证切分点尽量靠近中点、两半大小均衡；直接从头找第一个分隔符会让左半极小、右半极大，负载失衡。向后回退（`rfind(separator, mid)` 在 `[0, mid)` 找**最靠近 mid** 的）同样是为了贴着中点切。

**练习 2**：`WalkTreePrefixProducer::split` 里，为什么右生产器的 `seen` 是空的，而左生产器保留全部 `seen`？

**答案**：前序遍历中 `seen` 里是被「独子展开」推下去的祖先，它们的产出顺序先于所有 `to_explore` 中的节点。契约要求左半元素全部先于右半，所以祖先必须留在左半；右半从零开始积累自己的 `seen`。postfix 正相反（祖先最后产出、归右半），印证这不是随意选择。

**练习 3**：一棵深度 1_000_000、每节点一个孩子的「链形树」，`walk_tree` 还能并行吗？

**答案**：不能有效并行。`split` 第一步的 `while to_explore.len() == 1` 循环会一路展开独子直到叶子（前沿始终长度 1，无法 `split_vec`），`seen` 倒是攒了一百万已访问节点，第 3 步可以分 `seen`——所以理论上还能切几刀，但每刀都要先做 O(深度) 的独子展开。walk_tree 文档自己说「平衡树获得最佳并行化」，链形树是它的最坏情形。

### 4.4 切分正确性测试：从「三刀四段」到递归穷举

#### 4.4.1 概念说明

自定义生产器写完只是开始——切分逻辑是并行正确性的命门，必须穷举验证。仓库为**索引**生产器准备了现成骨架 `tests/producer_split_at.rs`：用 `with_producer` 钩子拿到裸生产器，在**所有可能的三刀位置**（i ≤ j ≤ k）上切四段，断言四段长度精确、正序拼接等于原序列、反序拼接等于原序列反转。

但它**不能直接用于无索引生产器**：`with_producer` 是 `IndexedParallelIterator` 的方法，`UnindexedProducer` 根本没有「按下标切」的能力。所以本节做两件事：读懂官方骨架的思路，然后把它翻译成无索引版本——**递归调用 `split()` 直到耗尽，把每个碎片的元素按「左前右后」拼接，断言等于原数据的串行遍历**。

#### 4.4.2 核心流程

官方骨架（索引侧）的覆盖策略：

```
对每个 i ≤ j ≤ k（共 C(n+3, 3) 组合）:
    producer.split_at(k)      → (left, d)     第一刀切外层
    left.split_at(i)          → (a, mid)      第二刀
    mid.split_at(j - i)       → (b, c)        第三刀
    断言: a.len == i, b.len == j-i, c.len == k-j（d 的长度由总长推出）
    断言: a ++ b ++ c ++ d == 原序列（正序）
    断言: 同上取 rev == 原序列反转
```

无索引侧的等价策略：

```
pieces(producer):
    match producer.split():
        (left, Some(right)) → pieces(left) ++ pieces(right)   // 递归分治
        (whole, None)       → fold_with(CollectFolder).complete()  // 串行收尾
断言: pieces(初始生产器) == 原数据的串行遍历
```

其中 `CollectFolder` 是我们手写的最小 `Folder`：`consume` 压栈、`complete` 返回 `Vec`、`full` 恒 `false`。终止性依赖生产器保证「每次 `Some` 切分都让数据规模严格变小」——`ListProducer` 满足（每刀至少把长度减半），测试规模要保持适度以免递归过深。

#### 4.4.3 源码精读

```rust
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
```

> [tests/producer_split_at.rs:6-15](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/tests/producer_split_at.rs#L6-L15) 测试入口 `check`：接收期望序列与「构造被测迭代器」的工厂，对所有三元组 (i, j, k) 执行正反两个方向的检查。注意泛型约束 `Iter: IndexedParallelIterator`——这行就是它只服务索引生产器的类型层面证据。

> [tests/producer_split_at.rs:17-28](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/tests/producer_split_at.rs#L17-L28) `map_triples` 枚举所有 `0 ≤ i ≤ j ≤ k ≤ n+1`——「三刀四段」的穷举来源，n 个元素产生 O(n³) 个组合，所以官方测试都用 10 个元素左右的小数据。

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

> [tests/producer_split_at.rs:66-97](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/tests/producer_split_at.rs#L66-L97) 核心回调：通过 `with_producer(Split { i, j, k, reverse })` 拿到裸 `Producer`，先切外层 `k` 得到任意中段，再在中段切 `i`、`j - i`（注释说明「先切外层得到任意中段、再细分以获得完整覆盖」）；四段 `into_iter` 后先做精确长度断言，再正序或反序 `chain` 收集。`reverse` 路径顺带验证了 `IntoIter: DoubleEndedIterator` 契约。

> [tests/producer_split_at.rs:99-102](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/tests/producer_split_at.rs#L99-L102) `check_len`：`size_hint` 与 `len()` 双重断言，对应 `ExactSizeIterator` 契约——索引侧连「长度诚实」都要逐段验证。

> [tests/producer_split_at.rs:139-143](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/tests/producer_split_at.rs#L139-L143) 上一讲的 `RepeatN` 也在被测名单里（`rayon::iter::repeat_n`）——说明 u9-l1 的实现正是被这套骨架套住的，本讲的综合实践会给 `ListProducer` 配上无索引版。

而手写无索引测试需要的 `Folder` 契约只有三个必需方法：

> [src/iter/plumbing/mod.rs:148-159](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L148-L159) `Folder` trait 定义：`consume`（吃一个元素返回新状态）、`complete`（收尾产出最终值）、`full`（是否想提前停）——加上这个trait，`CollectFolder` 十几行就能写完（见下方实践）。

#### 4.4.4 代码实践

**实践目标**：为无索引生产器写出「三刀四段」的等价物——递归穷举切分 + 拼接比对。

**操作步骤**（示例代码，可放进项目的 `#[cfg(test)]` 模块或集成测试）：

```rust
use rayon::iter::plumbing::{Folder, UnindexedProducer};

/// 最小收集折叠器：把元素按到达顺序攒进 Vec
struct CollectFolder<T>(Vec<T>);

impl<T> Folder<T> for CollectFolder<T> {
    type Result = Vec<T>;

    fn consume(mut self, item: T) -> Self {
        self.0.push(item);
        self
    }

    fn complete(self) -> Self::Result {
        self.0
    }

    fn full(&self) -> bool {
        false // 永不短路：测试要看到每一个元素
    }
}

/// 递归穷举：不断 split 直到耗尽，返回「左前右后」拼接的全部元素
/// 前置条件：P::split 返回 Some 时数据规模必须严格变小（否则死递归）
fn exhaustive_pieces<P>(producer: P) -> Vec<P::Item>
where
    P: UnindexedProducer,
{
    match producer.split() {
        (left, Some(right)) => {
            let mut v = exhaustive_pieces(left);
            v.extend(exhaustive_pieces(right));
            v
        }
        (whole, None) => whole.fold_with(CollectFolder(Vec::new())).complete(),
    }
}
```

再对上一节实践里的链表生产器（综合实践会完整实现，这里只看测试形状）：

```rust
#[test]
fn list_producer_split_exhaustive() {
    for n in [0usize, 1, 2, 3, 5, 8, 40, 257] {
        let list: std::collections::LinkedList<i32> = (0..n as i32).collect();
        let expected: Vec<i32> = list.iter().copied().collect();
        // ListProducer 的定义见综合实践
        assert_eq!(exhaustive_pieces(ListProducer { list: list.clone() }), expected);
    }
}
```

**需要观察的现象**：测试对每个 n 都通过；故意把 `ListProducer::split` 写错（例如交换左右两半：`Some(ListProducer { list: self.list })` 配错误的 left）再跑，断言立刻失败。

**预期结果**：穷举到单元素碎片后拼接的结果与串行遍历完全一致——这同时验证了 `split` 的「不重不漏」与「左前右后」两项契约。注意递归深度约为切分刀数（本例 O(n)，因为穷举会切到单元素），生产测试别用百万级数据，几百以内即可。运行结果「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：官方骨架用 O(n³) 组合穷举三刀；无索引版为什么不需要（也没法）枚举切分点？

**答案**：索引生产器可以被**指定**在任意下标切，所以每个下标组合都要验证其服从性；无索引生产器的切分点由 `split()` 内部逻辑唯一决定，外部无法指定——穷举的维度从「切在哪」变成「切多少轮后耗尽」，递归自然覆盖所有可达碎片。能人为改变切分行为的只剩数据本身（比如练习里不同 n、不同形状的树）。

**练习 2**：`exhaustive_pieces` 的文档注释写了前置条件「`Some` 切分必须让数据规模严格变小」。若被测生产器违反它会发生什么？如何防御？

**答案**：死递归直到栈溢出（或测试挂死）。防御办法：给递归加深度上限参数，超过即 `panic!` 报「split 未收敛」——测试工具自身也要防御性编程。`walk_tree` 这类切分点受限的生产器尤其要小心（前沿分不动时退而分 `seen`，两处都分不动才返回 `None`，收敛性由 `split_vec` 的 `len ≤ 1 → None` 保证）。

**练习 3**：为什么测试断言用 `Vec` 拼接比对而不是对 `exhaustive_pieces` 的结果求和？

**答案**：求和是可交换归约，切分「不重不漏」错了（元素丢失/重复）能暴露，但「左前右后」错了求和仍相等（见 4.1 练习 2）。`Vec` 比对同时检验多重集与顺序两项契约，是最强的观测。同理 `CollectFolder::full` 必须恒 `false`——任何短路都会让观测不完整。

## 5. 综合实践

**任务**：给 `std::collections::LinkedList` 实现完整的按值并行迭代——不走 `iter::split` 的「碎片即元素」路线，而是让 `into_par_iter()` 直接产出单个元素 `T`，并用第 4.4 节的测试套住它。这就是本讲规格里要求的「为链表实现 `UnindexedProducer` 的 split（按值对半拆分），收集全部元素并断言与串行遍历一致」。

**三层结构**（对照 u9-l1 的索引三件套：迭代器结构体 → Producer → Consumer；本讲是两件半：迭代器结构体 → `UnindexedProducer`，Consumer 用现成的）：

```rust
// 示例代码：独立 Cargo 项目，Cargo.toml 加 rayon = "1"
use rayon::iter::plumbing::{Folder, UnindexedConsumer, UnindexedProducer, bridge_unindexed};
use rayon::prelude::*;
use std::collections::LinkedList;

// ── 第 1 层：生产器 ──────────────────────────────────────────
struct ListProducer<T> {
    list: LinkedList<T>,
}

impl<T: Send> UnindexedProducer for ListProducer<T> {
    type Item = T;

    fn split(mut self) -> (Self, Option<Self>) {
        if self.list.len() < 2 {
            (self, None) // 0/1 个元素：分不动，诚实上报
        } else {
            // split_off(at)：把 [at, len) 摘成新链表，self 留下 [0, at)
            // len() 是 O(1)，split_off 需要走链——链表注定不如切片（u8-l1）
            let right = self.list.split_off(self.list.len() / 2);
            (self, Some(ListProducer { list: right }))
        }
    }

    fn fold_with<F>(self, folder: F) -> F
    where
        F: Folder<Self::Item>,
    {
        folder.consume_iter(self.list) // 默认实现会逐个 consume 并检查 full
    }
}

// ── 第 2 层：并行迭代器 ──────────────────────────────────────
struct ParList<T> {
    list: LinkedList<T>,
}

impl<T: Send> ParallelIterator for ParList<T> {
    type Item = T;

    fn drive_unindexed<C>(self, consumer: C) -> C::Result
    where
        C: UnindexedConsumer<Self::Item>,
    {
        // 标准驱动写法：照抄 src/iter/splitter.rs 的 Split::drive_unindexed
        bridge_unindexed(ListProducer { list: self.list }, consumer)
    }
}

// ── 第 3 层：入口 trait ──────────────────────────────────────
impl<T: Send> IntoParallelIterator for LinkedList<T> {
    type Iter = ParList<T>;
    type Item = T;

    fn into_par_iter(self) -> Self::Iter {
        ParList { list: self }
    }
}

// 参考入口：&LinkedList → 产出 &T（可选，展示 blanket 规则的另一半）
impl<'a, T: Sync> IntoParallelIterator for &'a LinkedList<T> {
    type Iter = ParRefList<'a, T>;
    type Item = &'a T;

    fn into_par_iter(self) -> Self::Iter {
        ParRefList { list: self }
    }
}

struct ParRefList<'a, T> {
    list: &'a LinkedList<T>,
}

impl<'a, T: Sync> ParallelIterator for ParRefList<'a, T> {
    type Item = &'a T;

    fn drive_unindexed<C>(self, consumer: C) -> C::Result
    where
        C: UnindexedConsumer<Self::Item>,
    {
        // 复用按值实现：迭代引用，收集后归还——「借用转按值」的惯用法
        let list: LinkedList<&'a T> = self.list.iter().collect();
        let v: Vec<&'a T> = bridge_unindexed(ListProducer { list }, consumer);
        unreachable!("借用版需要消费端配合，此处仅示意接口形状")
    }
}

fn main() {
    let list: LinkedList<u64> = (0..10_000).collect();
    let expected: Vec<u64> = list.iter().copied().collect();

    // ① 基本正确性：收集全部元素
    let got: Vec<u64> = list.clone().into_par_iter().collect();
    assert_eq!(got, expected);

    // ② 稳定性：多重运行，顺序必须每次都一致（左前右后契约）
    for _ in 0..100 {
        assert_eq!(list.clone().into_par_iter().collect::<Vec<_>>(), expected);
    }

    // ③ 适配器兼容：无索引迭代器的全套下游
    let sum: u64 = list.clone().into_par_iter().sum();
    assert_eq!(sum, expected.iter().sum());

    // ④ 粒度无关性：等价于默认行为（with_min/max_len 不适用于无索引迭代器，
    //    编译期就会拒绝——可自行取消注释验证）
    // list.clone().into_par_iter().with_max_len(1); // ❌ 不是 IndexedParallelIterator

    println!("all ok");
}
```

上面 `ParRefList` 里留了个 `unreachable!`——它揭示了一个真实困难：`bridge_unindexed` 的结果类型由**消费者**决定（`collect` 的消费者返回 `Vec<&T>`，`sum` 的返回 `u64`），借用版无法偷懒复用按值版。正确的做法是为 `&'a LinkedList<T>` 写一个独立的 `RefListProducer`（`split` 时对引用计数型结构做切分，或干脆像 rayon 的 collections 模块那样先搬进 `Vec` 再委托——见 u8-l3 的 `into_par_vec!` 路线）。**推荐做法**：删掉 `ParRefList`，借用需求直接 `list.iter().collect::<Vec<_>>()` 后走切片并行——这也正是 rayon 自己对哈希/B 树家族的选择。顺带一提，只要 `&LinkedList<T>: IntoParallelIterator` 实现存在，`list.par_iter()` 就会通过 blanket 规则（[src/iter/mod.rs:290-300](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L290-L300)）自动可用；而任何实现了 `ParallelIterator` 的类型自动获得 `into_par_iter`（[src/iter/mod.rs:2433-2440](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L2433-L2440)）。

**验证清单**：

1. 把 4.4 节的 `exhaustive_pieces` 测试接上 `ListProducer`，对 n ∈ {0, 1, 2, 3, 5, 8, 40, 257} 全部通过。
2. `main` 里 ② 的 100 次循环全部断言成功——工作窃取改变任务完成顺序，但**不改变元素落点**（u4-l4 的结论在无索引路径同样成立）。
3. 取消 ④ 的注释，确认编译器报「`with_max_len` 找不到该方法」——无索引迭代器的类型层身份。
4. 用 `RAYON_NUM_THREADS=1` 与 `=16` 分别运行，结果一致、耗时不同。

**思考题**（选做）：`ListProducer::split` 每刀都要 `split_off` 走链 O(n/2)，而切片是指针运算 O(1)。据此解释：为什么 rayon 的 collections 模块对 `LinkedList` 的 `into_par_iter` 选择「先串行搬进 `Vec` 再委托切片生产器」（一次 O(n) 换后续全 O(1) 切分），而不是像本实践这样原地把链表对半切？（提示：本实践每次切分都是 O(n/2)，切 \(\log P\) 轮的总代价是 O(n·log P)；搬 Vec 是一次性 O(n)。）

## 6. 本讲小结

- **两种切分世界观**：`Producer::split_at(index)` 切分点由框架给定、两半必须存在；`UnindexedProducer::split()` 切分点由数据自己决定、`None` 表示分不动。后者的串行收尾是 `fold_with(folder)`，没有 `into_iter`。
- **Rayon 没有 `Split` trait**：`rayon::iter::split` 是函数，`Split` 是迭代器结构体，扩展点是 `UnindexedProducer` trait + `Fn(D) -> (D, Option<D>) + Sync` 切分闭包；`Split::drive_unindexed` 的「构造生产器 + `bridge_unindexed`」是所有无索引自定义迭代器的标准驱动写法。
- **受限与带状态的切分**：`par_split` 的后端示范「切分点必须贴着中点找分隔符、找不到诚实返回 `None`、用 `tail` 记忆免重复扫描」；`walk_tree` 示范「双栈分家 + 独子展开」，prefix/postfix 用相反的 `seen` 搬运方向维持「左前右后」契约。
- **切分次数与元素数无关**：`Splitter` 预算从线程数起步逐刀减半，`split()` 约被调 \(\lceil \log_2 P \rceil\) 轮；实现者只需保证分不动时返回 `None`、切得动时不重不漏且左前右后。
- **测试要套两份契约**：官方 `producer_split_at.rs` 的「三刀四段」只服务索引生产器（`with_producer` 在 `IndexedParallelIterator` 上）；无索引版用「递归 `split()` 到耗尽 + `CollectFolder` 收集 + 拼接比对」同时验证不重不漏与顺序，`full` 必须恒 `false`。
- **链表是反面教材**：`split_off` 每刀 O(n/2)，这正是 collections 模块宁肯先搬 `Vec` 也要借切片生产器的原因——自定义数据源选路线时，切分成本与内存布局优先于「原地切分」的洁癖。

## 7. 下一步学习建议

本讲补全了「自定义并行源」的另一半：u9-l1 的索引路线 + 本讲的按值路线。接下来：

- **u9-l3（性能调优与基准测试）**：用 `rayon-demo` 的 matmul/quicksort 度量并行加速比，观察线程数、任务粒度、缓存友好性如何影响吞吐——把本讲「切分成本决定路线选择」的结论量化。
- **重读 `src/split_producer.rs` 的 `fold_with`**：本讲只读了它的 `split`，串行收尾里「先定位最后分隔符、尾段单独消费」的分治细节值得作为「叶子任务也要讲究」的进阶材料，并可对照 `src/str.rs` 的 `par_split` 看抽象 `Fissile` 的三个落点。
- **给综合实践补上树形数据源**：把本讲的骨架套到一颗 `enum Tree { Leaf, Node(Box<Tree>, Box<Tree>) }` 上（`split` 返回两个子树），对照 `walk_tree` 的「独子展开」思考：你的 `split` 在链形树上表现如何？要不要加同样的展开逻辑？
- **u9-l4（测试体系与平台可移植性）**：本讲的测试只覆盖切分契约，完整的二次开发还需要集成测试与 compile_fail 用例（比如验证 `ParList` 在 `T: !Send` 时编译失败）。
