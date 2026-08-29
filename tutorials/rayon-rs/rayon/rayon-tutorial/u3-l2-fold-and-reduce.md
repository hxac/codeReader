# 聚合：fold 与 reduce（u3-l2）

## 1. 本讲目标

学完本讲，你应该能够：

- 说出 `fold` 与 `reduce` 在**签名、执行时机、产出形态**上的三重差异：`fold` 是惰性适配器、产出「多个中间值」、累积器类型可以与元素类型不同；`reduce` 是立即执行的消费者、产出单值、操作数类型必须与元素同型。
- 解释为什么 `fold`/`reduce` 的合并操作要求**可结合（associative）**、单位元要求是**真单位元**，违反后正确性会发生什么。
- 知道 `sum`、`product`、`min`、`max` 这些内建归约分别如何复用 `reduce` 与标准库的 `Sum`/`Product` trait。
- 理解 `map_with`/`map_init` 与 `fold` 共享同一套「每个任务一份状态」的切分时机语义。
- 掌握 `try_fold` 的本地短路行为，以及它与 `try_reduce` 全局短路的分工。

## 2. 前置知识

本讲建立在 u3-l1 的适配器骨架之上，先回顾三个会反复用到的概念：

- **惰性适配器 vs 立即执行消费者**：返回「新迭代器类型」的方法（如 `map`）是惰性的，什么都不做，等下游消费；返回普通值的方法（如 `sum`、`reduce`）是立即执行的，由它触发任务切分与线程派发。判别方法就是看返回类型。`fold` 在 rayon 中是**惰性的**——这一点与标准库的 `Iterator::fold`（立即求值）正好相反，是初学者最常见的混淆点。
- **plumbing 四角色**：`Producer`（可分裂的生产者）、`Consumer`（可分裂的消费者）、`Folder`（真正逐元素吃数据的文件夹）、`Reducer`（把多个任务的部分结果两两合并的归并器）。u3-l1 重点讲了前三个，本讲的 `fold`/`reduce` 正是 `Reducer` 大显身手的地方。
- **包装消费者模式**：适配器在 `drive` 时把下游消费者 `C` 包一层（如 `FoldConsumer { base: C, ... }`）再转发给上游。数据自生产者流出，变换落在 `Folder::consume`。

还有一个贯穿全讲的数学概念——**可结合性**。运算 \(\otimes\) 满足可结合性，指的是对任意 \(a, b, c\)：

\[(a \otimes b) \otimes c = a \otimes (b \otimes c)\]

单位元 \(e\) 则满足 \(e \otimes x = x \otimes e = x\)。整数加法与字符串拼接都是可结合的；浮点加法**不严格**可结合（`(0.1 + 0.2) + 0.3 ≠ 0.1 + (0.2 + 0.3)`）；减法完全不可结合。并行聚合不保证运算顺序，所以这两条性质直接决定结果是否正确。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/iter/mod.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs) | `ParallelIterator` trait 上 `fold`/`reduce`/`sum`/`min` 等方法的签名、文档与默认实现 |
| [src/iter/fold.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/fold.rs) | `Fold`/`FoldWith` 适配器及 `FoldConsumer`/`FoldFolder` |
| [src/iter/reduce.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/reduce.rs) | `reduce` 的入口函数与 `ReduceConsumer`/`ReduceFolder` |
| [src/iter/sum.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/sum.rs) | `sum` 消费者，复用标准库 `Sum` trait |
| [src/iter/product.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/product.rs) | `product` 消费者，与 sum 同构 |
| [src/iter/map_with.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/map_with.rs) | `MapWith`/`MapInit`：携带每任务状态的映射适配器 |
| [src/iter/try_fold.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/try_fold.rs) | 可失败的 `TryFold`/`TryFoldWith` |

## 4. 核心概念与源码讲解

### 4.1 fold 语义：先分组局部聚合，产出多个中间值

#### 4.1.1 概念说明

`fold` 要解决的问题：**聚合类型与元素类型不同，且聚合开销大（比如要分配堆内存）时，如何避免为每个元素做一次昂贵操作**。

把序列 \(x_1, x_2, \dots, x_n\) 切成 \(k\) 个连续段 \(S_1, \dots, S_k\)（\(k\) 由运行时负载决定，不确定），每段独立折叠：

\[F_i = \text{fold\_op}(\dots\text{fold\_op}(\text{identity}(),\; S_{i,1})\dots)\]

`fold` 产出的不是单个 \(F\)，而是序列 \(F_1, F_2, \dots, F_k\) 本身——所以它是一个**惰性适配器**，返回的新并行迭代器以累积器类型 `T` 为元素类型。典型用法是再接一个 `reduce`/`sum`/`collect` 把这些中间值合起来。

官方文档用一张图说明这一点（[src/iter/mod.rs:L1130-L1160](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L1130-L1160)）：对 `22 3 77 89 46` 做并行 `fold(0, |a,b| a+b)`，可能得到两个数 `102 135`，也可能得到三个数 `102 89 46`——分组点不确定，「结果的个数」也不确定。

与 `reduce` 相比有两个关键自由度（文档 [L1162-L1194](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L1162-L1194)）：

1. **累积器类型自由**：`fold(|| 0_u32, |a: u32, b: u8| a + b as u32)` 可以把 `u8` 聚合成 `u32`，天然规避溢出；`reduce` 的操作数必须与元素同型。
2. **参数含义更严格**：`fold_op` 的左值永远是累积器，右值永远来自原始序列；而 `reduce` 的 `op` 两个参数可能都是「从未在原始迭代器中出现过的中间值」。这让 `fold_op` 可以放心地写成非对称形式（如 `push_str`）。

#### 4.1.2 核心流程

```text
fold(identity, fold_op) 被下游消费时：
1. drive_unindexed 把下游消费者 C 包装成 FoldConsumer { base: C, &identity, &fold_op }
2. 任务切分 → base.split_at → 左右各得一份 FoldConsumer（共享同一对引用）
3. 每个任务创建文件夹时调用 identity() —— 每任务一份独立初始值
4. FoldFolder::consume 逐元素执行 fold_op(acc, item)
5. 任务结束：complete() 把最终 acc 作为「一个元素」喂给下游文件夹
6. 各任务的多个 acc 由下游的 Reducer 合并（fold 自己不做归并！）
```

注意第 6 步：`FoldConsumer` 的 `type Reducer = C::Reducer`——它把归并职责完全推给下游，这正是「fold 产出多个值」在类型层面的体现。

#### 4.1.3 源码精读

先看 trait 签名（[src/iter/mod.rs:L1263-L1270](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L1263-L1270)）：返回类型是 `Fold<Self, ID, F>` 而不是某个值——惰性适配器：

```rust
fn fold<T, ID, F>(self, identity: ID, fold_op: F) -> Fold<Self, ID, F>
where
    F: Fn(T, Self::Item) -> T + Sync + Send,
    ID: Fn() -> T + Sync + Send,
    T: Send,
```

`Fold` 结构体就是 u3-l1 见过的「上游 + 两个闭包」骨架（[src/iter/fold.rs:L19-L25](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/fold.rs#L19-L25)）。它的驱动方法只做一次包装转发（[src/iter/fold.rs:L42-L52](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/fold.rs#L42-L52)）：

```rust
fn drive_unindexed<C>(self, consumer: C) -> C::Result
where
    C: UnindexedConsumer<Self::Item>,
{
    let consumer1 = FoldConsumer {
        base: consumer,
        fold_op: &self.fold_op,
        identity: &self.identity,
    };
    self.base.drive_unindexed(consumer1)
}
```

每个任务的文件夹创建时，用 `identity()` 生成自己的初始累积器（[src/iter/fold.rs:L84-L90](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/fold.rs#L84-L90)）：

```rust
fn into_folder(self) -> Self::Folder {
    FoldFolder {
        base: self.base.into_folder(),
        item: (self.identity)(),
        fold_op: self.fold_op,
    }
}
```

逐元素累积（[src/iter/fold.rs:L129-L136](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/fold.rs#L129-L136)）：`consume` 返回新文件夹（文件夹按值传递、不可变复用，这是 plumbing 的通用约定）：

```rust
fn consume(self, item: T) -> Self {
    let item = (self.fold_op)(self.item, item);
    FoldFolder { base: self.base, fold_op: self.fold_op, item }
}
```

任务结束时，局部聚合值作为**单个元素**下推给下游（[src/iter/fold.rs:L163-L165](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/fold.rs#L163-L165)）：

```rust
fn complete(self) -> C::Result {
    self.base.consume(self.item).complete()
}
```

（小提示：`FoldFolder` 的定义在 [src/iter/fold.rs:L116-L120](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/fold.rs#L116-L120)，其中累积器字段的类型参数写作 `ID`——它其实就是累积器类型 `U`，命名是历史遗留，读源码时不要与「identity 闭包的类型」混淆。）

`FoldFolder` 还实现了 `consume_iter` 批量快路径（[src/iter/fold.rs:L138-L161](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/fold.rs#L138-L161)）：直接用标准库的 `Iterator::fold` 一次吃完一段，并用 `take_while(not_full(&base))` 在下游报告「已满」时提前停手——这是给 `try_*` 短路留的钩子，4.4 节会用到。

最后看变体 `fold_with`（[src/iter/mod.rs:L1291-L1297](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L1291-L1297)）：它接受一个现成的初始值而非闭包，代价是 `T: Clone`——切分时左半边克隆一份（[src/iter/fold.rs:L242-L256](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/fold.rs#L242-L256)）。文档明确说明它「 essentially like `fold(|| init.clone(), fold_op)`，但不需要 init 类型是 `Sync`」（[src/iter/mod.rs:L1272-L1277](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L1272-L1277)）：每份克隆被移动进各自任务的文件夹、以 `&mut` 独占使用，天然线程安全。

#### 4.1.4 代码实践

**实践目标**：亲眼看到「fold 产出多个中间值，且个数不确定」。

操作步骤（示例代码，在自己的 Cargo 项目中运行，建议 `--release`）：

```rust
use rayon::prelude::*;

fn main() {
    // 1) 不控制切分粒度：收集所有「部分和」
    let partials: Vec<i32> = (0..10_000)
        .into_par_iter()
        .fold(|| 0, |a, b| a + b)
        .collect();
    println!("部分和个数 = {}, 总和 = {}",
             partials.len(),
             partials.iter().sum::<i32>());

    // 2) 强制最小任务长度为整个区间：无法再切分
    let one: Vec<i32> = (0..10_000)
        .into_par_iter()
        .with_min_len(10_000)
        .fold(|| 0, |a, b| a + b)
        .collect();
    println!("min_len=10000 时部分和个数 = {}", one.len());
}
```

需要观察的现象：

1. 第一个 `partials.len()` 每次运行都可能不同（与机器核数、当时负载有关），通常接近核数或其倍数——「待本地验证，具体数值依机器而定」。
2. 第二个恒为 `1`：`with_min_len(10_000)` 使长度 10 000 的区间不再满足二分条件，只剩一个任务、一个文件夹、一个部分和。
3. 两种情况下 `partials.iter().sum::<i32>()` 都等于 `49_995_000`（0..10_000 的整数和），因为整数加法可结合，分组方式不影响总和。

预期结果：理解 `fold` 的输出是「任务级」的，任务数由调度器决定。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `(0..1000).into_par_iter().fold(|| 0, |a, b| a + b)` 单独写在一行不会得到总和，也没有任何计算发生？

答案：`fold` 是惰性适配器，返回 `Fold<Self, ID, F>` 类型的并行迭代器，其元素是各任务的部分和。不接 `sum`/`reduce`/`collect` 等消费者，整个管道不会执行。

**练习 2**：`fold` 的 `identity` 闭包在整个计算过程中被调用几次？

答案：每创建一个任务文件夹调用一次（`into_folder` 里），次数等于实际任务数，与元素个数无关。这也是它适合放昂贵初始化（如新建 `String`、`HashMap`）的原因。

**练习 3**：用 `fold` 改写「把 `u8` 数组求和到 `u32`」，避免溢出。

答案：`bytes.into_par_iter().fold(|| 0_u32, |a, b| a + b as u32).sum::<u32>()`。累积器类型从一开始就是 `u32`，每一步加法都在 `u32` 上进行。

### 4.2 reduce 语义：树状两两合并成单值

#### 4.2.1 概念说明

`reduce` 是立即执行的消费者，把整个迭代器压成**一个**与元素同类型的值。它需要两个东西：

- `identity`：产生单位元的闭包。它会被**插入**到序列的任意位置以制造并行机会——所以必须是真单位元，这是正确性的硬性要求；
- `op`：二元合并操作。由于合并发生在归约树的内部节点上，`op` 的两个操作数可能都是中间值（例如两半各自的部分和），调用顺序与分组方式都不确定，所以 `op` 必须可结合。

形式化地说，reduce 计算的是某棵二叉归约树上的结果：叶子是各段的局部折叠（从 `identity()` 起步），内部节点执行 `op(left, right)`。只要 \(e\) 是真单位元且 \(\otimes\) 可结合，任意树形的值都等于从左到右顺序折叠的值：

\[\mathrm{reduce} = x_1 \otimes x_2 \otimes \cdots \otimes x_n\]

官方文档对这两条约定的原文（[src/iter/mod.rs:L981-L987](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L981-L987)）：

> **Note:** unlike a sequential `fold` operation, the order in which `op` will be applied to reduce the result is not fully specified. So `op` should be associative or else the results will be non-deterministic. And of course `identity()` should produce a true identity.

还有一个常见误区值得点破：**reduce 不是 fold 的「完成版」**。reduce 的操作数与元素同型，这一限制在类型层面杜绝了「换个宽的累积器」这种最自然的溢出解法——文档特意用 `[128_u8, 64_u8, 64_u8]` 举例（[src/iter/mod.rs:L1167-L1185](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L1167-L1185)）：`reduce(|| 0_u8, |a, b| a + b)` 会溢出，要么先 `map` 提宽，要么改用 `fold`。

`reduce_with` 是免单位元版本，空迭代器返回 `None`。文档注明它「simple but somewhat less efficient」（[src/iter/mod.rs:L996-L1002](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L996-L1002)）——它的实现恰好是本讲两个主角的合奏（[src/iter/mod.rs:L1041-L1043](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L1041-L1043)）：

```rust
self.fold(<_>::default, opt_fold(&op))
    .reduce(<_>::default, opt_reduce(&op))
```

即先把每个元素包成 `Option<T>` 用 `fold` 局部聚合，再用 `reduce` 合并这些 `Option`——多出来的 `Some/None` 匹配与单位元 `None`（`Option` 的 `default`）就是它的效率代价。

#### 4.2.2 核心流程

```text
ParallelIterator::reduce(identity, op)          // mod.rs L988-L994
→ reduce::reduce(pi, identity, op)              // reduce.rs L4-L16
→ pi.drive_unindexed(ReduceConsumer { &identity, &op })
→ plumbing 递归切分，每个任务：
    into_folder()  → ReduceFolder { item: identity() }   // 从单位元起步
    consume/consume_iter → 对本段元素做 op 折叠
    complete()     → 段结果 T
→ 各段结果沿归约树自底向上由 Reducer::reduce(left, right) 合并
→ 根节点结果即最终返回值
```

`ReduceConsumer` 自身实现了 `Reducer`——在 reduce 里，消费者和归并器是同一个东西。

#### 4.2.3 源码精读

入口函数（[src/iter/reduce.rs:L4-L16](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/reduce.rs#L4-L16)）构造消费者后交给 `drive_unindexed`：

```rust
pub(super) fn reduce<PI, R, ID, T>(pi: PI, identity: ID, reduce_op: R) -> T
where
    PI: ParallelIterator<Item = T>,
    R: Fn(T, T) -> T + Sync,
    ID: Fn() -> T + Sync,
    T: Send,
{
    let consumer = ReduceConsumer {
        identity: &identity,
        reduce_op: &reduce_op,
    };
    pi.drive_unindexed(consumer)
}
```

`ReduceConsumer` 只持有两个共享引用，因此可以整体 `Copy`（[src/iter/reduce.rs:L18-L29](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/reduce.rs#L18-L29)）。切分时「左消费者、右消费者、归并器」就是同一份的三次复制（[src/iter/reduce.rs:L41-L43](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/reduce.rs#L41-L43)）：

```rust
fn split_at(self, _index: usize) -> (Self, Self, Self) {
    (self, self, self)
}
```

对比 4.1.3 里 `FoldConsumer::split_at` 要拆包重组 base，这里的 `split_at` 甚至不需要看切分下标——reduce 对「在哪个位置切」毫不在意，这正是可结合性带来的自由。每个任务的文件夹从单位元起步（[src/iter/reduce.rs:L45-L50](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/reduce.rs#L45-L50)），逐元素合并（[src/iter/reduce.rs:L92-L97](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/reduce.rs#L92-L97)）：

```rust
fn consume(self, item: T) -> Self {
    ReduceFolder {
        reduce_op: self.reduce_op,
        item: (self.reduce_op)(self.item, item),
    }
}
```

段间合并发生在 `Reducer` 实现里，直接调用用户给的 `op`（[src/iter/reduce.rs:L72-L79](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/reduce.rs#L72-L79)）：

```rust
fn reduce(self, left: T, right: T) -> T {
    (self.reduce_op)(left, right)
}
```

注意 `ReduceFolder::full` 恒返回 `false`（[src/iter/reduce.rs:L52-L54](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/reduce.rs#L52-L54)）：普通 reduce 没有「提前完成」的概念，必须吃完全部元素——对照 4.4 节 `try_fold` 的 `full` 就能看出短路是如何接入的。另外 `consume_iter` 直接复用标准库 `Iterator::fold`（[src/iter/reduce.rs:L99-L107](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/reduce.rs#L99-L107)），段内退化为一次串行折叠——并行只发生在任务之间。

#### 4.2.4 代码实践

**实践目标**：验证「reduce 操作数与元素同型」导致的溢出，以及两种正确改法。

操作步骤（示例代码）：

```rust
use rayon::prelude::*;

fn main() {
    let bytes = [128_u8, 64, 64];

    // ① 反面教材：debug 模式下这一行会因加法溢出而 panic
    // let overflow = bytes.par_iter().copied().reduce(|| 0_u8, |a, b| a + b);

    // ② 正确做法一：map 提宽后 reduce
    let wide: u32 = bytes.par_iter().copied().map(|b| b as u32).sum();

    // ③ 正确做法二：fold 直接换累积器类型
    let folded: u32 = bytes
        .par_iter()
        .copied()
        .fold(|| 0_u32, |a, b| a + b as u32)
        .sum();

    assert_eq!(wide, folded);
    println!("sum = {wide}");
}
```

需要观察的现象：

1. 取消 ① 的注释在 **debug** 模式运行：`attempt to add with overflow` panic；在 **release** 模式下不 panic，得到回绕后的 `0`（\(256 \bmod 256\)）。无论任务怎么分组，只要在 `u8` 上做加法，结果都是回绕的。
2. ②③ 输出 `256`。

预期结果：体会「reduce 的类型约束是硬性的，溢出防护要么 map 提宽、要么 fold 换累积器」。

#### 4.2.5 小练习与答案

**练习 1**：假设用户写了 `reduce(|| 1_i32, |a, b| a * b)` 对 `[2, 3, 5]` 求积，结果一定正确吗？

答案：不一定错但埋雷。`1` 对乘法是真单位元，本例碰巧正确；但单位元闭包的作用是「插入序列制造并行机会」，一旦某段为空，该段返回 `1`，若单位元不真（比如写 `|| 2`），空段会污染最终乘积。正确性依赖 \(e \otimes x = x\) 严格成立。

**练习 2**：为什么 `reduce_with` 比 `reduce` 慢？

答案：它把每个元素包成 `Option<T>` 再做 fold+reduce（见 mod.rs L1041-L1043），增加了包装、分支判断，且空段单位元 `None` 也要参与一次合并。`reduce` 直接在裸 `T` 上工作。

**练习 3**：`(0..1000).into_par_iter().map(|x| x as f64).sum::<f64>()` 多次运行的结果总相同吗？

答案：不保证。浮点加法不严格可结合，任务分组与归约树形状由运行时决定，不同的结合顺序可能产生不同的舍入误差（多数情况相同，但不是确定性保证）。文档在 [src/iter/mod.rs:L1362-L1369](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L1362-L1369) 明确提示了这一点。

### 4.3 内建归约：sum、product、min、max 与 map_with 家族

#### 4.3.1 概念说明

rayon 没有为 `sum`/`min` 等另造一套算术体系，而是在 `reduce` 与标准库 trait 之上做薄封装：

- `sum`/`product` 复用标准库的 `std::iter::Sum`/`Product` trait。好处是任何实现过标准库 `Sum` 的类型（`i32`、`f64`、`String`、`Duration`……）自动可用，行为与串行 `sum()` 完全一致；
- `min`/`max` 是 `reduce_with(Ord::min)` 的一行封装；`min_by_key` 用「键值元组」技巧避免在每次合并时重复计算键函数；
- `map_with`/`map_init` 不是归约，但与 `fold` 共享同一个核心思想——**状态的生命周期是任务级而非元素级**：切分时克隆或构造，任务内以 `&mut` 独占，任务结束丢弃。`fold` 在任务结束时把状态交给下游合并，`map_with` 只用状态变换元素。理解了其中一个，另一个的约束（`T: Send + Clone`、无需 `Sync`）也就自然理解了。

#### 4.3.2 核心流程

```text
sum()  → SumConsumer → 每任务 SumFolder 从 iter::empty().sum() 起步
                        → 段结果由 add（复用 std Sum）合并
min()  → reduce_with(Ord::min)
min_by_key(f) → map(|x| (f(&x), x)) → reduce_with(比较键的元组) → 取回 .1
map_with(init, op) → 切分时左半 clone 一份 init → 任务内 next() 拿 &mut init 调 op
```

#### 4.3.3 源码精读

`sum` 的入口与合并函数（[src/iter/sum.rs:L7-L17](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/sum.rs#L7-L17)）——`add` 干脆把两个值放进数组再交给标准库的 `sum()`，加法语义零重复：

```rust
pub(super) fn sum<PI, S>(pi: PI) -> S
where
    PI: ParallelIterator,
    S: Send + Sum<PI::Item> + Sum,
{
    pi.drive_unindexed(SumConsumer::new())
}

fn add<T: Sum>(left: T, right: T) -> T {
    [left, right].into_iter().sum()
}
```

初始值的获取方式很巧妙（[src/iter/sum.rs:L45-L49](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/sum.rs#L45-L49)）：对空迭代器调用标准库 `sum()`，拿到的正是该类型的单位元（整数的 `0`、`String` 的空串、`Duration::ZERO`……）：

```rust
fn into_folder(self) -> Self::Folder {
    SumFolder { sum: iter::empty::<T>().sum() }
}
```

顺带一读 `SumConsumer` 的定义（[src/iter/sum.rs:L19-L23](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/sum.rs#L19-L23)）：消费者里根本没有 `S` 类型的值，只有 `PhantomData<*const S>` 类型标记，而裸指针默认 `!Send`，所以需要 `unsafe impl Send` 手动担保——实际持有的 `S` 值都在各任务的 `SumFolder` 里按值移动，不存在共享。`product` 完全同构，单位元来自 `iter::empty::<T>().product()`（[src/iter/product.rs:L49-L53](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/product.rs#L49-L53)）。段间合并同样走 `Reducer`（[src/iter/sum.rs:L69-L76](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/sum.rs#L69-L76)）。

`min` 只有一行（[src/iter/mod.rs:L1449-L1454](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L1449-L1454)）：

```rust
fn min(self) -> Option<Self::Item>
where
    Self::Item: Ord,
{
    self.reduce_with(Ord::min)
}
```

`min_by_key` 的元组技巧（[src/iter/mod.rs:L1504-L1521](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L1504-L1521)）：先 `map` 成 `(键, 元素)` 让键**只计算一次**并随元素流过整棵归约树，最后解包取回元素：

```rust
let (_, x) = self.map(key(f)).reduce_with(min_key)?;
Some(x)
```

再看 `map_with` 的状态复制点。生产者切分时，左半克隆状态、右半拿走原值（[src/iter/map_with.rs:L148-L162](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/map_with.rs#L148-L162)）：

```rust
fn split_at(self, index: usize) -> (Self, Self) {
    let (left, right) = self.base.split_at(index);
    (
        MapWithProducer { base: left, item: self.item.clone(), map_op: self.map_op },
        MapWithProducer { base: right, item: self.item, map_op: self.map_op },
    )
}
```

任务内逐元素使用时以 `&mut` 独占状态（[src/iter/map_with.rs:L191-L194](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/map_with.rs#L191-L194)）：

```rust
fn next(&mut self) -> Option<R> {
    let item = self.base.next()?;
    Some((self.map_op)(&mut self.item, item))
}
```

若克隆本身昂贵，可改用 `map_init`：它接受 `INIT: Fn() -> T`，每个任务现造一份，连 `Clone` 都不需要（[src/iter/mod.rs:L672-L679](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L672-L679)），文档示例就是用它给每个任务造一个局部 RNG（[src/iter/mod.rs:L644-L671](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L644-L671)）。`fold` 的 `identity` 与 `map_init` 的 `init` 在这里完全同构。

#### 4.3.4 代码实践

**实践目标**：验证 `sum` 对标准库 `Sum` 类型的泛化能力，以及 `min_by_key` 的用法。

操作步骤（示例代码）：

```rust
use rayon::prelude::*;

fn main() {
    // 1) String 也实现了 std::iter::Sum —— sum 不限于数字
    let joined: String = ["alpha", "beta", "gamma"]
        .par_iter()
        .sum();
    println!("{joined}"); // 期望 alphabetagamma

    // 2) min_by_key 找最长单词
    let words = ["apple", "hi", "banana", "ok"];
    let longest = words.par_iter().min_by_key(|w| w.len());
    println!("{longest:?}");

    // 3) 与串行版对照
    let seq: String = ["alpha", "beta", "gamma"].iter().sum();
    assert_eq!(joined, seq);
}
```

需要观察的现象：

1. `joined` 与串行 `sum()` 相等——`String` 拼接可结合，且归约树保持左右段的先后关系。
2. `longest` 为 `Some("banana")`。
3. 若把 1 中的元素换成 `f64` 并累加大数量浮点数，多次运行可能末位有细微差异（结合性问题）。

预期结果：掌握「内建归约 = reduce + 标准库 trait」的复用模式。（具体输出待本地验证。）

#### 4.3.5 小练习与答案

**练习 1**：`min_by_key` 为什么先把元素映射成 `(K, T)` 元组，而不是在合并函数里调用键函数？

答案：`reduce_with` 的合并发生在归约树的每个内部节点上，若在比较时才算键，同一个元素的键会被重复计算 \(O(\log n)\) 次；映射成元组后键只算一次、随值流动。

**练习 2**：`map_with` 的状态 `T` 为什么不要求 `Sync`，而普通 `map` 的闭包要求 `Sync + Send`？

答案：`map_with` 在每次切分时克隆/构造一份独立状态，任务内以 `&mut` 独占使用，没有任何跨线程共享，所以只需 `Send + Clone`；普通 `map` 的闭包以共享引用 `&F` 同时供给多个任务调用，必须 `Sync`。

**练习 3**：`sum::<String>()` 和 `fold`+`reduce` 拼接字符串，谁的分配次数少？

答案：`sum` 的文件夹从空 `String` 起步、逐元素 `add`（内部也是 `push_str` 式合并），分配次数约等于任务数加上合并时的容量增长，与 `fold`+`reduce` 同量级；两者都远少于「每元素一个 String」的 `map`+`reduce`。

### 4.4 try_fold：可失败的聚合与短路

#### 4.4.1 概念说明

`try_fold` 是 `fold` 的可失败版本，面向 `Result`/`Option`。u2-l5 已经从**使用视角**讲过 try 家族，本节从**实现视角**补上它与本讲主题的衔接：`try_fold` 的短路是**本地**的——某个任务遇到第一个 `Err`/`None` 就停止消费本组剩余元素，但其他任务的 fold 照常进行；要让整条管道全局停下，需要消费端的 `try_reduce`（它用共享 `AtomicBool` + `full()` 钩子广播停止信号）。

其类型参数 `U: Try` 是 rayon 私有的 `Try` trait，把 `Result`/`Option` 统一成 `ControlFlow` 风格的分支语义。

#### 4.4.2 核心流程

```text
TryFoldFolder 内部维护 control: ControlFlow<Residual, Output>
consume(item):
    若 control 仍是 Continue(acc) → 调用 fold_op(acc, item)，把结果 branch() 回 ControlFlow
    若已 Break → 直接跳过（本地短路，剩余元素不再触碰 fold_op）
complete():
    Continue(c) → 还原为 Ok(c)/Some(c) 喂给下游
    Break(r)   → 还原为 Err(r)/None 喂给下游
full():
    control.is_break() || base.full()   // 向 plumbing 汇报「别再喂我了」
```

#### 4.4.3 源码精读

短路的核心就在 `consume` 的这个 `if let`（[src/iter/try_fold.rs:L138-L144](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/try_fold.rs#L138-L144)）：

```rust
fn consume(mut self, item: T) -> Self {
    let fold_op = self.fold_op;
    if let Continue(acc) = self.control {
        self.control = fold_op(acc, item).branch();
    }
    self
}
```

`full` 向上汇报停止条件（[src/iter/try_fold.rs:L154-L156](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/try_fold.rs#L154-L156)）——它正是 4.1.3 里 `FoldFolder::consume_iter` 用 `take_while(not_full(...))` 检查的那个钩子；`complete` 把 `ControlFlow` 还原成 `Result`/`Option`（[src/iter/try_fold.rs:L146-L152](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/try_fold.rs#L146-L152)）。文档对其作用范围的限定写在 trait 定义处（[src/iter/mod.rs:L1299-L1304](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L1299-L1304)）：「The first such failure stops processing the local set of items, without affecting other folds in the iterator's subdivisions」。

#### 4.4.4 代码实践

**实践目标**：观察 `try_fold` 的本地短路——部分组提前停止，其余组继续。

操作步骤（示例代码）：

```rust
use rayon::prelude::*;

fn main() {
    // 每组累加，只在撞到「毒丸」值 500 时放弃本组
    let outcomes: Vec<Option<i32>> = (0..1000)
        .into_par_iter()
        .try_fold(
            || 0,
            |acc: i32, x: i32| {
                if x == 500 {
                    None
                } else {
                    Some(acc + x)
                }
            },
        )
        .collect();

    let stopped = outcomes.iter().filter(|o| o.is_none()).count();
    let finished = outcomes.iter().filter(|o| o.is_some()).count();
    println!("放弃的组 = {stopped}, 坚持到最后的组 = {finished}");
}
```

需要观察的现象：

1. 恰好**一个**组返回 `None`——只有分到包含 500 的那一段的任务会失败，且它在消费到 500 时立即停止处理本组剩余元素；其余所有组正常返回 `Some(部分和)`。
2. 多次运行，失败组数始终是 1，但 `Some(_)` 的个数（即组数）会随负载浮动。

预期结果：直观感受「短路只作用于本地组」。若换成 `try_reduce`（消费端），整条管道会在第一个 `None` 出现后尽快全线停止，而不是只有一组放弃。

#### 4.4.5 小练习与答案

**练习 1**：`try_fold` 与 `try_reduce` 的短路范围有何不同？

答案：`try_fold` 是适配器，短路只影响它所在的本地任务组（通过 `Folder::full` 停止本组喂数）；`try_reduce` 是消费者，通过共享原子标志让所有任务都观察到失败并尽快停止。

**练习 2**：`ReduceFolder::full` 为什么恒为 `false`，而 `TryFoldFolder::full` 要检查 `is_break`？

答案：普通 `reduce` 必须消费全部元素才能得到正确结果，没有提前完成的说法；`try_fold` 一旦 `Break`，本组继续消费毫无意义，`full()` 返回 `true` 让 plumbing 停止向该文件夹输送元素（`consume_iter` 的 `take_while` 也会随之截断）。

## 5. 综合实践

**任务：实现并行字符串拼接，并对比两种策略的性能。**

背景：官方文档在「Fold vs Map/Reduce」一节（[src/iter/mod.rs:L1196-L1245](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L1196-L1245)）已经给出结论——直接用 `map` + `reduce` 拼字符串会「为每个元素创建一个 String，不划算」，`fold` 则「每组只建一个 String，组数约等于 CPU 数」。我们要用基准计时验证这个结论。

操作步骤（示例代码，在自己的 Cargo 项目中运行，务必用 `--release`）：

```rust
use rayon::prelude::*;
use std::time::Instant;

fn main() {
    let words: Vec<String> = (0..200_000).map(|i| format!("word{i},")).collect();
    // 预热线程池，排除首次初始化的干扰
    (0..1000).into_par_iter().for_each(|_| std::hint::black_box(()));

    // 策略 A：fold 每任务局部拼接，再 reduce 合并
    let t0 = Instant::now();
    let a: String = words
        .par_iter()
        .fold(
            || String::new(),
            |mut s, w| {
                s.push_str(w);
                s
            },
        )
        .reduce(
            || String::new(),
            |mut l, r| {
                l.push_str(&r);
                l
            },
        );
    let d_a = t0.elapsed();

    // 策略 B：reduce 直接合并 —— 但操作数必须与元素同型（String），
    // 而 par_iter() 的元素是 &String，只能先克隆成 String
    let t1 = Instant::now();
    let b: String = words
        .par_iter()
        .cloned() // 每个元素一次完整堆分配 + 拷贝
        .reduce(
            || String::new(),
            |mut l, r| {
                l.push_str(&r);
                l
            },
        );
    let d_b = t1.elapsed();

    assert_eq!(a, b);
    println!("fold+reduce : {d_a:?}");
    println!("clone+reduce: {d_b:?}");
}
```

需要观察的现象与预期结果：

1. `assert_eq!` 通过：字符串拼接可结合，且切分产生的是**连续段**、归约树保持左右段的先后关系，所以两种策略（乃至任意分组）都得到与串行一致的字符串——这正是官方文档示例 `assert_eq!(s, "abcde")` 所依赖的事实。
2. 策略 A 明显快于策略 B（具体倍数依机器而定，待本地验证）。原因有三：
   - **分配次数**：A 的堆分配约等于「任务数」（几个到几十个 `String`，各自摊销增长）；B 的 `.cloned()` 为 20 万个元素各做一次完整的堆分配与释放，随后又立刻被 `push_str` 吸收、丢弃——纯粹的浪费；
   - **容量复用**：A 的组内 `push_str` 在同一个 `String` 上摊销扩容，写内存接近顺序写；
   - **合并开销**：两者合并阶段都要搬运全部字节，但 B 额外背负了每元素的分配器往返。
3. 若把 `.cloned()` 换成 `.map(|w| w.to_string())`，本质相同——文档所说「one string per element」指的就是这一步。
4. 进一步实验：给 A 加上 `.with_min_len(1)`（切得更碎、任务更多）观察耗时的变化，体会「组数≈CPU 数才是甜点」——切太碎会让 reduce 合并次数上升，切太粗则并行度不足（粒度控制将在 u3-l3 展开）。

## 6. 本讲小结

- `fold` 是**惰性适配器**：产出「每个任务一份局部聚合值」的序列，个数不确定；累积器类型可以与元素类型不同，`fold_op` 的左值永远是累积器、右值永远是原始元素。
- `reduce` 是**立即执行的消费者**：操作数与元素同型，`identity` 会被插入序列制造并行机会，因此必须是真单位元；合并顺序不确定，因此 `op` 必须可结合。
- `reduce_with` 的实现就是 `fold(Option)` + `reduce(Option)`，多出的包装是它效率略低的原因；`min`/`max` 是 `reduce_with(Ord::min/max)` 的一行封装，`min_by_key` 用键值元组让键只算一次。
- `sum`/`product` 完全复用标准库 `Sum`/`Product` trait，单位元取自 `iter::empty().sum()`，因此 `String` 等类型也能直接 `sum`。
- `map_with`/`map_init` 与 `fold` 共享「状态生命周期 = 任务级」的语义：切分时克隆或构造、任务内 `&mut` 独占、结束丢弃（`fold` 则交给下游合并）。
- `try_fold` 的短路是本地的（`Folder::full` + `consume_iter` 的 `take_while`），全局短路要靠消费端的 `try_reduce`。

## 7. 下一步学习建议

- 下一讲 **u3-l3（有索引适配器与长度控制）**：本讲综合实践中埋下的 `with_min_len` 伏笔将在那里展开——`LengthSplitter` 如何根据 `min_len`/`max_len` 决定切分深度，直接决定 `fold` 的「组数」。
- 若想彻底搞清本讲反复出现的 `Reducer`、`split_at`、`bridge` 的协作机制，进入 **u4-l1（plumbing 总览）** 与 **u4-l3（Consumer 与驱动流程）**，阅读 [src/iter/plumbing/README.md](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/README.md)。
- 延伸阅读源码：[src/iter/mod.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs) 中 `try_reduce`/`try_reduce_with`（L1045 起）的共享 `AtomicBool` 实现，与本讲 4.4 节对照；以及 `rayon-demo` 中基于分治的基准程序，体会 fold/reduce 与手写 `join` 递归的等价性。
