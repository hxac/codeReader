# 无状态适配器：map 与 filter 家族

## 1. 本讲目标

学完本讲，你应该能够：

1. 独立读懂 `map`、`filter` 等适配器的源码：结构体定义、`drive`/`drive_unindexed` 实现、Consumer/Folder 的包装方式。
2. 理解 `src/delegate.rs` 中两个委托宏如何用十几行宏代码替集合模块消除成片的转发样板。
3. 说清 `map_with` 与 `map_init` 中「共享状态」的生命周期：它何时被克隆、何时被独占、为什么不需要 `Sync`。
4. 具备亲手实现一个自定义并行适配器的动手能力（本讲综合实践）。

本讲是单元三的第一讲，视角从「使用并行迭代器」（单元二）切换到「阅读适配器源码」。我们从最简单的一类适配器——**无状态适配器**——入手：它们对每个元素独立处理，不涉及任务间的归约协议（那是 fold/reduce 的事，见下一讲）。

## 2. 前置知识

### 2.1 惰性适配器与立即消费者（回顾）

u2-l1 已建立的核心结论：`ParallelIterator` 上的方法分两类，看返回类型即可判别——

- 返回一个新迭代器类型（如 `Map<I, F>`）的是**惰性适配器**，调用它什么都不做；
- 返回普通值（如 `sum` 返回整数、`for_each` 返回 `()`）的是**立即消费者**，由它触发真正的切分与执行。

本讲的所有适配器都是前者。每个适配器结构体都标着 `#[must_use = "iterator adaptors are lazy and do nothing unless consumed"]`，漏接消费者时编译器会直接警告。

### 2.2 plumbing 三剑客一览

适配器源码里反复出现 `Producer` / `Consumer` / `Folder` 三个 trait（定义于 [src/iter/plumbing/mod.rs:56](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L56)、[L123](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L123)、[L154](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs#L154)）。本讲只需要记住一句话版本的分工：

| 角色 | 职责 | 关键方法 |
| --- | --- | --- |
| `Producer` | 数据的提供方，可按下标二分 | `split_at`、`fold_with` |
| `Consumer` | 结果的消费方，可按下标二分 | `split_at`、`into_folder`、`full` |
| `Folder` | Consumer 的串行执行形态 | `consume`、`complete`、`full` |

单元四会系统剖析 plumbing；本讲把它们当作「适配器必须对接的接口」来用。

### 2.3 Send 与 Sync

- `Send`：类型可以**按值**跨线程移动；
- `Sync`：类型可以**以共享引用 `&T`** 被多个线程同时使用。

这条区分是理解本讲所有 trait 约束的钥匙：适配器的闭包会以 `&F` 的形式同时交给多个任务，所以要求 `F: Sync + Send`；而 `map_with` 的状态每个任务独占一份可变副本，所以不要求 `Sync`。

### 2.4 macro_rules! 基础

阅读 `delegate.rs` 需要能看懂声明宏：`macro_rules!` 按 token 模式匹配并展开成代码，`$($args:tt)*` 表示「把剩下所有 token 原样吞下、原样吐出」。不需要更深的宏知识。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/iter/mod.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs) | `ParallelIterator` trait 定义 `map`/`filter` 等方法入口（本讲只看方法签名，trait 全貌见 u2-l1） |
| [src/iter/map.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/map.rs) | `Map` 适配器完整实现：本讲的精读样板 |
| [src/iter/filter.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/filter.rs) | `Filter`：丢失索引信息的代表 |
| [src/iter/filter_map.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/filter_map.rs) | `FilterMap`：一步完成过滤 + 变换 |
| [src/iter/inspect.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/inspect.rs) | `Inspect`：只读旁观每个元素 |
| [src/iter/update.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/update.rs) | `Update`：原地修改每个元素 |
| [src/iter/cloned.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/cloned.rs) | `Cloned`：没有闭包字段的适配器 |
| [src/iter/map_with.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/map_with.rs) | `MapWith` 与 `MapInit`：带共享状态的 map |
| [src/delegate.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/delegate.rs) | `delegate_iterator!` / `delegate_indexed_iterator!` 两个委托宏 |

另外会零星引用 [src/iter/plumbing/mod.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/mod.rs)（trait 定义处）。

## 4. 核心概念与源码讲解

### 4.1 适配器统一骨架：Map 源码精读

#### 4.1.1 概念说明

Rayon 里每个适配器都是同一个模式的实例：

> **适配器 = 一个只装「上游迭代器 + 用户闭包」的小结构体 + 把下游消费者包一层再转发给上游的两个 drive 方法。**

「无状态」指适配器自己不持有任何跨元素的状态——处理第 n 个元素不需要知道第 n−1 个元素的结果。`map` 是这个模式最纯粹的样本：它甚至不改变元素个数与顺序，因此能完整保留上游的索引能力（`len` 不变、切分位置不变）。读懂 `map.rs`，仓库里 `src/iter/` 下几十个适配器文件就都有了参照系。

#### 4.1.2 核心流程

关键洞察：**适配器不搬运数据，它包装消费者**。数据始终从上游生产者流出；适配器把「用户眼中的最终消费者」层层包裹成洋葱，再整串交给上游。以 `(0..1_000).into_par_iter().map(f).sum()` 为例：

```text
用户调用 .map(f)          →  Map::new(range, f)，仅构造结构体，无任何执行
用户调用 .sum()           →  sum 生成一个求和消费者 SumConsumer
                             │
Map::drive_unindexed(SumConsumer)
  ├─ MapConsumer::new(SumConsumer, &f)     ← 把消费者包一层（变换逻辑注入在此）
  └─ range.drive_unindexed(MapConsumer)    ← 转发给上游
                             │
Range 收到 MapConsumer     →  bridge_unindexed 切分生产者与消费者，
                               在每个叶子任务里：元素 → MapFolder::consume
                               → 先 (map_op)(item) 再 SumFolder::consume
```

切分发生时（无论 indexed 的 `split_at(index)` 还是 unindexed 的 `split_off_left`），`MapConsumer` 也跟着二分：左右两个新消费者共享同一个 `&f`。这就是闭包必须 `Sync` 的原因——任意多个任务可能同时调用它。

#### 4.1.3 源码精读

**入口方法。** `map` 定义在 `ParallelIterator` trait 上（[src/iter/mod.rs:598-604](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L598-L604)），注意约束 `F: Fn(Self::Item) -> R + Sync + Send`——方法体只有一行 `Map::new(self, map_op)`，再次印证「适配器是纯构造」：

```rust
fn map<F, R>(self, map_op: F) -> Map<Self, F>
where
    F: Fn(Self::Item) -> R + Sync + Send,
    R: Send,
{
    Map::new(self, map_op)
}
```

**结构体。** [src/iter/map.rs:11-16](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/map.rs#L11-L16)：两个字段，一个上游 `base`，一个闭包 `map_op`，加上 `Clone` 与「必须被消费」的警告标注。全文件 255 行，这个结构体就是适配器的全部「数据」：

```rust
#[must_use = "iterator adaptors are lazy and do nothing unless consumed"]
#[derive(Clone)]
pub struct Map<I, F> {
    base: I,
    map_op: F,
}
```

**无索引驱动。** [src/iter/map.rs:39-49](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/map.rs#L39-L49)：`drive_unindexed` 是 `ParallelIterator` 唯一的必需方法（trait 定义见 [src/iter/mod.rs:2410-2412](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L2410-L2412)）。注意 `map_op` 是**按共享引用**借出去的（`&self.map_op`），随后 `self` 被消费、闭包本体留在栈上活着直到整轮执行结束：

```rust
fn drive_unindexed<C>(self, consumer: C) -> C::Result
where
    C: UnindexedConsumer<Self::Item>,
{
    let consumer1 = MapConsumer::new(consumer, &self.map_op);
    self.base.drive_unindexed(consumer1)
}

fn opt_len(&self) -> Option<usize> {
    self.base.opt_len()
}
```

`opt_len` 原样转发——`map` 不改变长度，所以长度信息透传给下游（`opt_len` 有默认实现返回 `None`，见 [src/iter/mod.rs:2428-2430](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L2428-L2430)，`Map` 主动覆盖它以保留信息）。

**索引驱动。** [src/iter/map.rs:52-68](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/map.rs#L52-L68) 是 `map` 保留索引能力的直接证据：`IndexedParallelIterator` 的 `drive` 与 `len` 都只是换了个包装转发，`len` 等于上游长度：

```rust
impl<I, F, R> IndexedParallelIterator for Map<I, F>
where
    I: IndexedParallelIterator,
    F: Fn(I::Item) -> R + Sync + Send,
    R: Send,
{
    fn drive<C>(self, consumer: C) -> C::Result
    where
        C: Consumer<Self::Item>,
    {
        let consumer1 = MapConsumer::new(consumer, &self.map_op);
        self.base.drive(consumer1)
    }

    fn len(&self) -> usize {
        self.base.len()
    }
    // with_producer 见下
}
```

`with_producer`（[src/iter/map.rs:70-103](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/map.rs#L70-L103)）走的是「生产者包装」路线：把上游生产者包成 `MapProducer` 再回调给外层。文件内定义的局部 `Callback` 结构体是跨越「生产者类型未知」这一障碍的标准手法——回调必须对任意 `P: Producer` 泛型。

**消费者侧的切分。** [src/iter/map.rs:183-190](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/map.rs#L183-L190)：消费者分裂时，先把**内层**消费者切开，再用同一个 `&map_op` 把左右两半分别包起来；归约器直接复用内层的（`map` 不改变结果语义，自然也不需要新的归约规则）：

```rust
fn split_at(self, index: usize) -> (Self, Self, Self::Reducer) {
    let (left, right, reducer) = self.base.split_at(index);
    (
        MapConsumer::new(left, self.map_op),
        MapConsumer::new(right, self.map_op),
        reducer,
    )
}
```

**逐元素消费。** 真正执行变换的地方在 `MapFolder::consume`（[src/iter/map.rs:231-237](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/map.rs#L231-L237)）：先应用闭包，再把结果喂给内层 folder——两行代码就是 `map` 的全部语义：

```rust
fn consume(self, item: T) -> Self {
    let mapped_item = (self.map_op)(item);
    MapFolder {
        base: self.base.consume(mapped_item),
        map_op: self.map_op,
    }
}
```

`consume_iter`（[src/iter/map.rs:239-245](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/map.rs#L239-L245)）是批量优化路径：直接复用标准库 `Iterator::map` 串行处理整段，省去逐元素走一遍 `consume` 的函数调用开销。

**生产者侧。** `MapProducer`（[src/iter/map.rs:108-157](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/map.rs#L108-L157)）镜像了同样的结构：`split_at` 把上游生产者切开、两个新生产者共享 `&map_op`；`into_iter` 直接返回 `std::iter::Map`（[L122-124](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/map.rs#L122-L124)）——到了串行世界就复用标准库，Rayon 从不重复造轮子。

#### 4.1.4 代码实践：验证「map 保留索引、filter 丢失索引」

1. **实践目标**：用编译器亲手验证 u2-l1 的结论「索引能力在类型层面传播：`map` 保留、`filter` 丢失且不可恢复」。
2. **操作步骤**：
   - 新建（或复用 u1-l3 的）Cargo 工程，依赖 `rayon`；
   - 写入下面程序（示例代码）：

   ```rust
   use rayon::prelude::*;

   fn main() {
       let v: Vec<i32> = (0..8).collect();
       // map 之后 zip：可以编译
       let zipped: Vec<(i32, i32)> = (0..8)
           .into_par_iter()
           .map(|x| x + 1)
           .zip(&v)
           .collect();
       assert_eq!(zipped.len(), 8);
   }
   ```

   - 运行 `cargo run`，确认通过；
   - 再把 `.map(|x| x + 1)` 改成 `.filter(|x| x % 2 == 0)`，重新 `cargo build`。
3. **需要观察的现象**：改用 `filter` 后编译失败，错误形如 `the trait bound Filter<...>: IndexedParallelIterator is not satisfied`（`zip` 要求 `Self: IndexedParallelIterator`）。
4. **预期结果**：`map` 版本编译运行成功；`filter` 版本无法通过编译。错误编号与措辞随编译器版本略有差异，具体输出待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Map` 的两个 drive 方法里，闭包以 `&self.map_op`（共享引用）传给消费者，而不是把闭包 move 进去？
**答案**：消费者在执行中会被 `split_at` 切成任意多份，每一份都需要使用同一个闭包。按值持有就要求闭包满足 `Clone`，凭空增加约束；共享引用只需 `F: Sync` 即可让所有拷贝共用一个实例。

**练习 2**：如果把 `Map::opt_len` 的实现改成返回 `None`，程序还正确吗？有什么代价？
**答案**：仍然正确——正确性不依赖 `opt_len`。但下游会退回「无长度」路径，例如 `collect` 无法精确预分配目标缓冲（u2-l4 的结论），性能受损。`opt_len` 是纯优化信息。

**练习 3**：`Map` 同时实现了 `ParallelIterator` 和 `IndexedParallelIterator`，两个 `drive` 方法体几乎一样，为什么不合并？
**答案**：两者对接的消费者协议不同：`drive` 的 `C: Consumer` 支持按精确下标的 `split_at`，`drive_unindexed` 的 `C: UnindexedConsumer` 只支持无参数的 `split_off_left`。方法体相似只是因为 `map` 恰好对两种协议都透明；对 `zip` 这类适配器两条路径差异巨大。

### 4.2 Filter 家族与同族变体

#### 4.2.1 概念说明

`map` 的一族亲戚共享同一骨架，差别只在三点：**闭包拿什么样的参数**、**元素去留**、**是否还能保留索引**。先看总表（全部结论均可在对应源码中验证）：

| 适配器 | 闭包签名 | Item 变化 | 实现 IndexedParallelIterator？ |
| --- | --- | --- | --- |
| `Map` | `Fn(Item) -> R` | `Item → R` | 是（[map.rs:52](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/map.rs#L52)） |
| `Filter` | `Fn(&Item) -> bool` | 不变（可能丢弃） | 否 |
| `FilterMap` | `Fn(Item) -> Option<R>` | `Item → R`（可能丢弃） | 否 |
| `Inspect` | `Fn(&Item)` | 不变 | 是（[inspect.rs:52](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/inspect.rs#L52)） |
| `Update` | `Fn(&mut Item)` | 不变（原地修改） | 是（[update.rs:51](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/update.rs#L51)） |
| `Cloned` | 无闭包 | `&T → T` | 是（[cloned.rs:43](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/cloned.rs#L43)） |

**为什么 `filter` 丢索引而 `inspect` 不丢**：`inspect` 对每个元素只旁观、不删除，任何分段的元素个数与顺序都不变，`len` 依旧准确；`filter` 过滤后每段剩多少元素事先不可知，而索引路径的整套协议——`collect` 按段直写、`zip` 按下标对位——都建立在「长度精确已知」之上，所以 `Filter` 根本没有 `IndexedParallelIterator` 实现（[filter.rs:29-43](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/filter.rs#L29-L43) 是它唯一的驱动实现）。

#### 4.2.2 核心流程

`Filter` 的执行流程与 `Map` 完全同构，唯一的分岔在 `Folder::consume`：

```text
FilterFolder::consume(item):
    if filter_op(&item)      ← 谓词以 &Item 借用，不夺走所有权
        → base.consume(item)  ← 通过：交给内层
    else
        → self                ← 拦下：原样返回自身，元素被丢弃
```

注意被丢弃的判断发生在**folder 层**而不是驱动层——切分逻辑对 `Filter` 完全无感知，这就是它如此简单的代价与好处：实现简单，但切分只能按「输入段」进行，无法按「输出量」平衡负载。

`FilterMap` 把两步合成一步：`Fn(Item) -> Option<R>` 按值拿走元素、返回 `Option`，`Some` 拆包后下传（[filter_map.rs:119-127](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/filter_map.rs#L119-L127)）。相比 `filter` + `map` 链，它少一轮消费者包装与一次中间所有权转移。

#### 4.2.3 源码精读

**Filter 结构体与驱动。** [src/iter/filter.rs:11-14](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/filter.rs#L11-L14) 与 [src/iter/filter.rs:36-42](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/filter.rs#L36-L42)。与 `Map` 的 `drive_unindexed` 逐行对照只差包装类型名；整个文件没有 `IndexedParallelIterator` 这个词：

```rust
pub struct Filter<I, P> {
    base: I,
    filter_op: P,
}

fn drive_unindexed<C>(self, consumer: C) -> C::Result
where
    C: UnindexedConsumer<Self::Item>,
{
    let consumer1 = FilterConsumer::new(consumer, &self.filter_op);
    self.base.drive_unindexed(consumer1)
}
```

**过滤的落点。** [src/iter/filter.rs:115-123](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/filter.rs#L115-L123)。谓词借用 `&item` 判断，通过后才把 `item` 按值移入内层：

```rust
fn consume(self, item: T) -> Self {
    let filter_op = self.filter_op;
    if filter_op(&item) {
        let base = self.base.consume(item);
        FilterFolder { base, filter_op }
    } else {
        self
    }
}
```

紧随其后的注释（[src/iter/filter.rs:125-127](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/filter.rs#L125-L127)）值得一看：`filter` 故意**不**覆盖 `consume_iter`，因为批量优化需要在迭代中途检查 `base.full()`（短路支持），而默认实现恰好逐元素做了这件事（issue #632）。这是「默认实现反而更对」的罕见案例。

**Inspect：只读旁观。** [src/iter/inspect.rs:227-233](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/inspect.rs#L227-L233)。先调用观察闭包，再把**同一个未动的元素**传下去；`inspect` 不参与数据变换，是调试并行管道的首选挂钩：

```rust
fn consume(self, item: T) -> Self {
    (self.inspect_op)(&item);
    InspectFolder {
        base: self.base.consume(item),
        inspect_op: self.inspect_op,
    }
}
```

**Update：原地修改。** [src/iter/update.rs:235-242](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/update.rs#L235-L242)。闭包拿 `&mut item`，改完照常下传——元素类型和个数都不变，所以 `update` 保留索引能力：

```rust
fn consume(self, mut item: T) -> Self {
    (self.update_op)(&mut item);

    UpdateFolder {
        base: self.base.consume(item),
        update_op: self.update_op,
    }
}
```

文件里还有个灵巧的辅助函数 `apply`（[src/iter/update.rs:221-226](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/update.rs#L221-L226)），把 `Fn(&mut T)` 适配成 `Fn(T) -> T`，供 `consume_iter` 与配套的串行迭代器 `UpdateSeq`（[src/iter/update.rs:267-303](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/update.rs#L267-L303)）复用标准库 `Iterator::map`/`collect` 的特化实现。

**Cloned：没有闭包的适配器。** [src/iter/cloned.rs:12-14](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/cloned.rs#L12-L14) 的结构体只有一个字段——变换（clone）是类型内在能力，无需用户注入。但 Consumer/Folder/Producer 三件套一个不少（如 [src/iter/cloned.rs:197-201](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/cloned.rs#L197-L201) 的 `ClonedFolder::consume`），因为类型变化 `&T → T` 同样必须沿 plumbing 协议传递：

```rust
fn consume(self, item: &'a T) -> Self {
    ClonedFolder {
        base: self.base.consume(item.clone()),
    }
}
```

#### 4.2.4 代码实践：一条链上观察六个适配器中的四个

1. **实践目标**：把 `cloned → filter_map → inspect` 串成一条管道，观察「打印顺序乱、收集结果稳」这一并行管道的典型行为。
2. **操作步骤**（示例代码）：

   ```rust
   use rayon::prelude::*;

   fn main() {
       let words = ["hi", "hello", "rust", "rayon"];
       let out: Vec<String> = words
           .par_iter()                      // Item = &&str
           .copied()                        // &&str -> &str（与 cloned 同族的近亲）
           .filter_map(|w| {
               if w.len() % 2 == 0 {
                   Some(w.to_uppercase())   // 长度为偶数的词保留并大写
               } else {
                   None
               }
           })
           .inspect(|w| {
               println!(
                   "[线程 {:?}] 产出 {}",
                   rayon::current_thread_index(),
                   w
               );
           })
           .collect();
       println!("最终顺序: {:?}", out);
   }
   ```

   - 用 `cargo run --release` 跑若干次；
   - 再用 `RAYON_NUM_THREADS=1 cargo run --release` 跑若干次。
3. **需要观察的现象**：多线程下 `inspect` 的打印顺序在多次运行间可能不同；但最后一行 `最终顺序` 每次都是 `["HI", "RUST"]`。单线程下打印通常固定。
4. **预期结果**：打印（副作用）顺序不确定，收集结果（数据流）顺序确定——这正是 u1-l1「竞态条件与副作用顺序不在保证之列」的直观体现。多线程下本机是否真会出现乱序取决于元素量与调度，待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`filter` 之后想恢复索引能力有办法吗？
**答案**：在适配器层面没有。过滤后长度未知是本质信息缺失，任何包装都无法凭空补出精确 `len`。工程上的替代是 `collect` 成 `Vec` 再开新的 `into_par_iter`（重新获得精确长度），或改用 `filter_map` 之外能保留索引的表达（例如先 `enumerate` 记录下标）。

**练习 2**：`inspect` 和 `update` 的闭包签名差一个 `mut`，各自适合什么场景？
**答案**：`inspect` 拿 `&Item`，只读旁观，适合日志、计数、断言，绝不干扰数据；`update` 拿 `&mut Item`，原地改写元素本身（如规格化、去重标记），适合「元素需要加工但不多不少」的场景。两者都不改变元素个数与类型，都保留索引。

**练习 3**：`Cloned` 没有闭包字段，为什么还要完整实现 Consumer/Folder？
**答案**：plumbing 的执行单位是 Consumer/Folder，`collect` 等消费者只认这套协议。类型变化（`&T → T`）和逻辑变化一样都要经由 `consume` 逐元素完成，`ClonedFolder::consume` 里的 `item.clone()` 就是全部变换逻辑。

### 4.3 delegate! 宏：消除转发样板

#### 4.3.1 概念说明

4.1 里 `Map` 的两个 drive 方法「包一层再转发」只有三四行，但 **`len`/`opt_len`/`with_producer` 这些纯转发方法**如果一个适配器写一遍，几十个适配器累计就是数百行零营养代码。`src/delegate.rs` 的两个宏把这层样板收敛成一次宏调用。

它的适用场景与 `Map` 不同：`Map` 要**注入变换逻辑**（包装消费者），而委托宏服务的对象是**纯包装类型**——自己就是一层壳，所有行为完全等于内部迭代器。典型用户是 `src/collections/` 与 `src/option.rs`、`src/result.rs`：整个 `src/` 下共 25 处调用（用 `Grep` 统计 `delegate_iterator!|delegate_indexed_iterator!` 可复核，另有 3 处在 delegate.rs 自身的定义展开与测试中）。

#### 4.3.2 核心流程

两个宏是层层叠加的关系：

```text
delegate_indexed_iterator! { MyIter<T> => T, impl<T: Send> }
        │
        ├─ 第一步：展开 delegate_iterator!     → 实现 ParallelIterator
        │    · drive_unindexed → self.inner.drive_unindexed(consumer)
        │    · opt_len         → self.inner.opt_len()
        │
        └─ 第二步：追加 IndexedParallelIterator 实现
             · drive          → self.inner.drive(consumer)
             · len            → self.inner.len()
             · with_producer  → self.inner.with_producer(callback)
```

使用约定：结构体必须已经声明了一个名为 **`inner`** 的字段（宏生成的代码直接写死 `self.inner`）；`IntoParallelIterator` 的实现不含在内，需要另行添加。

#### 4.3.3 源码精读

**无索引版宏。** [src/delegate.rs:11-29](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/delegate.rs#L11-L29)。模板里 `$iter` 是迭代器类型、`$item` 是元素类型、`$($args:tt)*` 把 `impl<T: Send>` 这类泛型约束整段吞下再原样吐给 `impl`：

```rust
macro_rules! delegate_iterator {
    ($iter:ty => $item:ty ,
     impl $( $args:tt )*
     ) => {
        impl $( $args )* ParallelIterator for $iter {
            type Item = $item;

            fn drive_unindexed<C>(self, consumer: C) -> C::Result
                where C: UnindexedConsumer<Self::Item>
            {
                self.inner.drive_unindexed(consumer)
            }

            fn opt_len(&self) -> Option<usize> {
                self.inner.opt_len()
            }
        }
    }
}
```

文件头两行注释（[src/delegate.rs:3-4](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/delegate.rs#L3-L4)）解释了为什么约束只能放在末尾：`macro_rules!` 没有解析任意 where 子法的语法手段，按 token 序列「囫囵吞枣」是唯一简单的做法。

**索引版宏。** [src/delegate.rs:34-61](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/delegate.rs#L34-L61)。第一步直接递归调用上面的宏（[L38-41](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/delegate.rs#L38-L41)），第二步追加三个方法（[L43-59](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/delegate.rs#L43-L59)）——对照 4.1.3 读过的 `Map` 手写实现，正是同一批方法：

```rust
macro_rules! delegate_indexed_iterator {
    ($iter:ty => $item:ty ,
     impl $( $args:tt )*
     ) => {
        delegate_iterator!{
            $iter => $item ,
            impl $( $args )*
        }

        impl $( $args )* IndexedParallelIterator for $iter {
            fn drive<C>(self, consumer: C) -> C::Result
                where C: Consumer<Self::Item>
            {
                self.inner.drive(consumer)
            }

            fn len(&self) -> usize {
                self.inner.len()
            }

            fn with_producer<CB>(self, callback: CB) -> CB::Output
                where CB: ProducerCallback<Self::Item>
            {
                self.inner.with_producer(callback)
            }
        }
    }
}
```

**官方自带的两个用法示范。** [src/delegate.rs:63-86](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/delegate.rs#L63-L86) 包装 `BTreeMap` 的 `IntoIter`（无索引），[src/delegate.rs:88-109](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/delegate.rs#L88-L109) 包装 `Vec` 的 `IntoIter`（索引版，还演示了 `collect_into_vec` 快速路径可用）。注意：这两个宏是 `macro_rules!` 定义的**内部宏，未通过 `#[macro_export]` 导出**，只能在 rayon crate 内部使用——外部项目想复用这个模式，得把宏抄过去（这合法且常见，宏本体只有几十行）。

#### 4.3.4 代码实践：手工展开一次宏

1. **实践目标**：确认你真的理解宏展开了什么，而不只是「大概知道」。
2. **操作步骤**：
   - 阅读 [src/delegate.rs:88-109](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/delegate.rs#L88-L109) 的 `indexed_example` 测试；
   - 在纸上（或注释里）写出 `delegate_indexed_iterator! { MyIntoIter<T> => T, impl<T: Send> }` 展开后的**完整** Rust 代码——共两个 `impl` 块、五个方法；
   - 把写出的展开与 [src/iter/map.rs:31-68](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/map.rs#L31-L68) 逐行对照，标出两处本质差异。
3. **需要观察的现象**：差异恰好有两处——`Map` 的 `drive`/`drive_unindexed` 会先构造 `MapConsumer` 包装（注入逻辑），而宏版本原样转发；`Map` 的 `with_producer` 有 `Callback`/`MapProducer` 包装 machinery，宏版本直接透传。
4. **预期结果**：能完整默写展开并说清「注入逻辑的适配器手写、纯转发的壳用宏」这条分工线，即为达标。

#### 4.3.5 小练习与答案

**练习 1**：委托宏为什么能安全地把 `opt_len` 转发出去，而 4.1 练习 2 里说改掉 `Map::opt_len` 只是性能问题？
**答案**：两者一致。`opt_len` 的契约（[src/iter/mod.rs:2414-2423](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L2414-L2423)）是「返回 `Some(n)` 就必须真的产出 n 个且走 indexed 协议」。纯包装类型的行为与内层完全相同，转发即守约；错误的做法是返回不准确的 `Some`。

**练习 2**：给自己的类型用 `delegate_indexed_iterator!` 需要满足什么前提？
**答案**：结构体有名为 `inner` 的字段且其类型已实现 `IndexedParallelIterator`（因为宏会调用 `inner.drive`/`inner.len`/`inner.with_producer`）；另外还要自己补 `IntoParallelIterator` 实现。且宏仅限 rayon crate 内部使用，外部项目需复制宏定义。

**练习 3**：`Filter` 适合用委托宏实现吗？
**答案**：不适合。`Filter` 需要在消费者里注入谓词逻辑（`FilterConsumer`/`FilterFolder`），不是纯转发；而且它根本不实现 `IndexedParallelIterator`。委托宏只覆盖「壳」的情况。

### 4.4 map_with / map_init：带共享状态的 map

#### 4.4.1 概念说明

`map` 的闭包是无状态的：处理每个元素只依赖元素本身。但很多真实计算需要**伴随状态**——一个发送端、一个缓冲区、一个随机数发生器。直接在闭包里捕获可变状态行不通：闭包要求 `Fn`（只读捕获）且要 `Sync`（多任务共享）。

Rayon 给出两个递进的方案：

- **`map_with(init, op)`**：你提供一个初始值 `T: Send + Clone`。切分出的**每个任务各拿一份克隆**，任务内闭包以 `&mut T` 独占修改它。
- **`map_init(init_fn, op)`**：连初始值都不给，改给构造函数 `INIT: Fn() -> T`。每个任务**现造一份**，`T` 因此不需要 `Clone`——官方文档直言「对返回类型没有任何约束」（[src/iter/mod.rs:647-649](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L647-L649)）。

两者的文档都强调同一个语义：状态只与「每个 rayon 任务的元素组」配对，按需克隆/构造（[src/iter/mod.rs:609-611](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L609-L611)），因此**不要求 `T: Sync`**——这是它们相对「闭包直接捕获 `&T`」的根本优势。

#### 4.4.2 核心流程

状态的生命周期完全由切分事件驱动：

```text
map_with(init_value, op)
    │
    ├─ 不切分：一个任务从头用到尾，0 次克隆，op(&mut state, item) 串行累计
    │
    └─ 每次 split_at：
         左半 ← state.clone()        ← 克隆发生在这一刻
         右半 ← state（原值移入）
         此后两半各自演化，互不相干
```

设一轮执行中切分树有 \( k \) 个叶子任务，则 `init` 值至多被克隆内部节点数次：

\[ C_{\text{clone}} = k - 1 \]

关键点：**克隆次数与任务数同阶，与元素总数无关**。一千万个元素的管道若切成 64 个任务，`init` 只克隆 63 次。代价模型是「每任务一次克隆」，不是「每元素一次」。

`map_init` 把克隆换成构造：`into_folder` 与 `fold_with` 进入叶子任务时调用一次 `(self.init)()`（分别见 [src/iter/map_with.rs:537-543](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/map_with.rs#L537-L543)、[L489-499](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/map_with.rs#L489-L499)），此后与 `map_with` 的路径汇合（`MapInitProducer` 复用 `MapWithIter`/`MapWithFolder`）。

还有个容易忽略的推论：**各任务的状态演化结果最后会被丢弃**——`map_with` 的 `T` 只影响各元素产出 `R` 的过程，任务结束时状态本身不参与归约。若需要把状态也归并回来，那是 `fold` + `reduce` 的职责（下一讲 u3-l2）。

#### 4.4.3 源码精读

**入口与约束。** [src/iter/mod.rs:635-642](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L635-L642)。闭包签名是 `Fn(&mut T, Item) -> R`——状态以独占可变引用进入；`T: Send + Clone`，注意**没有 Sync**：

```rust
fn map_with<F, T, R>(self, init: T, map_op: F) -> MapWith<Self, T, F>
where
    F: Fn(&mut T, Self::Item) -> R + Sync + Send,
    T: Send + Clone,
    R: Send,
{
    MapWith::new(self, init, map_op)
}
```

**结构体。** [src/iter/map_with.rs:12-16](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/map_with.rs#L12-L16)：比 `Map` 多一个 `item: T` 字段存放状态初始值。

**切分即克隆。** 生产者侧 [src/iter/map_with.rs:148-162](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/map_with.rs#L148-L162) 与消费者侧 [src/iter/map_with.rs:247-254](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/map_with.rs#L247-L254) 是生命周期论断的直接证据——左半克隆、右半拿原值：

```rust
fn split_at(self, index: usize) -> (Self, Self, Self::Reducer) {
    let (left, right, reducer) = self.base.split_at(index);
    (
        MapWithConsumer::new(left, self.item.clone(), self.map_op),
        MapWithConsumer::new(right, self.item, self.map_op),
        reducer,
    )
}
```

**消费时独占。** [src/iter/map_with.rs:298-302](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/map_with.rs#L298-L302)：`&mut self.item`——同一时刻一个状态副本只属于一个 folder，这正是无需 `Sync` 的原因：

```rust
fn consume(mut self, item: T) -> Self {
    let mapped_item = (self.map_op)(&mut self.item, item);
    self.base = self.base.consume(mapped_item);
    self
}
```

批量路径 `consume_iter`（[src/iter/map_with.rs:304-320](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/map_with.rs#L304-L320)）用局部函数 `with` 把「带状态闭包」临时适配成普通 `FnMut`，借给标准库 `Iterator::map`——状态始终被独占借用，借用检查器全程盯着。

**MapInit：按需构造。** [src/iter/map_with.rs:333-357](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/map_with.rs#L333-L357) 定义 `MapInit`，它不存 `T` 而存 `INIT: Fn() -> T`。消费者变成 folder 的那一刻才调用构造函数（[src/iter/map_with.rs:537-543](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/map_with.rs#L537-L543)）：

```rust
fn into_folder(self) -> Self::Folder {
    MapWithFolder {
        base: self.base.into_folder(),
        item: (self.init)(),
        map_op: self.map_op,
    }
}
```

**`Sync` 约束去哪了**：`map_with`/`map_init` 的 `T` 不需要 `Sync`，但**闭包 `F` 与 `INIT` 仍然需要**（`F: Fn(..) + Sync + Send`、`INIT: Fn() -> T + Sync + Send`），因为切分后多个任务共享的是这两个函数本身；状态则靠克隆/重构造实现隔离。

#### 4.4.4 代码实践：数一数 init 被调用了几次

1. **实践目标**：用原子计数器实证「`map_init` 的构造函数按任务调用、与元素数无关」，并观察任务粒度调节的影响。
2. **操作步骤**（示例代码）：

   ```rust
   use rayon::prelude::*;
   use std::sync::atomic::{AtomicUsize, Ordering};

   fn main() {
       let inits = AtomicUsize::new(0);

       let sum: i64 = (0..1_000_000)
           .into_par_iter()
           .map_init(
               || {
                   inits.fetch_add(1, Ordering::SeqCst);
                   0i64 // 每个任务自己的局部累加器（此处仅用于演示状态存在）
               },
               |acc: &mut i64, x: i64| {
                   *acc += 1; // 记录本任务处理了多少元素
                   x
               },
           )
           .sum();

       println!("sum = {sum}, init 调用次数 = {}", inits.load(Ordering::SeqCst));
   }
   ```

   - `cargo run --release` 记录 `init 调用次数`；
   - 在 `into_par_iter()` 之后插入 `.with_max_len(10_000)` 再跑一次；
   - 再换 `.with_min_len(1_000_000)`（强迫几乎不切分）跑一次。
3. **需要观察的现象**：三次运行的 `init 调用次数` 相差悬殊：默认值与机器核数同量级；`with_max_len(10_000)` 后升至约 100（一百万除以一万）；`with_min_len(1_000_000)` 后跌到 1 左右。而无论哪种，`sum` 恒等于 499999500000。
4. **预期结果**：`init` 次数 ≈ 叶子任务数，随粒度参数变化；元素总数一千万级也只与任务数相关。具体数值随机器与调度波动，待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：`map_with` 里的 `init` 值什么时候被克隆？
**答案**：只在消费者或生产者 `split_at` 的瞬间——左半克隆一份、右半持有原值（[src/iter/map_with.rs:247-254](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/map_with.rs#L247-L254)）。一个叶子任务内部从头到尾零克隆。

**练习 2**：为什么 `map_with` 不要求 `T: Sync`，而 `map` 的闭包要求 `Sync`？
**答案**：`map` 的闭包以 `&F` 同时共享给任意多个任务调用，共享引用跨线程即需 `Sync`；`map_with` 的 `T` 每任务独占一份（切分时克隆隔离），任何时刻只有一个任务以 `&mut T` 访问自己的副本，共享的只有克隆能力，因此只需 `Send + Clone`。

**练习 3**：`map_init` 相比 `map_with` 放宽了什么、新增了什么约束？
**答案**：放宽——`T` 不再需要 `Clone`（每任务用 `INIT` 现造，官方文档明说返回类型无任何约束，[src/iter/mod.rs:647-649](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L647-L649)）；新增——要提供 `INIT: Fn() -> T + Sync + Send` 构造函数，且构造的代价发生在每个任务启动时。

**练习 4**：想在并行结束后拿到「所有任务的状态汇总」（比如各任务累计的计数），`map_with` 做得到吗？
**答案**：做不到。任务结束时 `T` 的演化值被直接丢弃（`MapWithFolder::complete` 只调 `base.complete()`）。这类需求应使用 `fold` 产出中间状态、`reduce` 归并——正是 u3-l2 的主题。

## 5. 综合实践：手写 Double 适配器

这是本讲的收官任务：**不使用 `map`**，模仿 `Map` 的源码手写一个把元素乘 2 的 `Double` 适配器，直接实现 `ParallelIterator` 的 `drive_unindexed` 与 `IndexedParallelIterator` 的 `drive`，完整走一遍「包装消费者」的模式。

### 5.1 实践目标

1. 把 4.1 的骨架知识转化为可编译的代码；
2. 亲手体验 `ParallelIterator` / `IndexedParallelIterator` 各必需方法的分工；
3. 验证自定义适配器与官方 `map` 输出完全一致。

### 5.2 操作步骤

**第一步**：新建工程并添加依赖。

```bash
cargo new double-adapter
cd double-adapter
cargo add rayon
# 或者手工在 Cargo.toml 的 [dependencies] 写下：rayon = "1"
```

**第二步**：确认外部 crate 可以使用 plumbing——Rayon 将其公开导出（[src/lib.rs:91](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/lib.rs#L91) 的 `pub mod iter` 与 [src/iter/mod.rs:91](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L91) 的 `pub mod plumbing`），因此 `rayon::iter::plumbing::*` 中的 `Consumer`、`Folder`、`Producer` 等 trait 对外可用。

**第三步**：写入 `src/main.rs`（以下为完整参考实现，示例代码；为简化泛型，固定 `Item = i32`）：

```rust
use rayon::iter::plumbing::{
    Consumer, Folder, Producer, ProducerCallback, UnindexedConsumer,
};
use rayon::prelude::*;

/// 与 src/iter/map.rs 的 Map<I, F> 同构，只是变换固定为「乘 2」
#[must_use = "iterator adaptors are lazy and do nothing unless consumed"]
#[derive(Clone, Debug)]
struct Double<I> {
    base: I,
}

/// 模拟 ParallelIterator::map 的入口（自由函数形式）
fn double<I>(base: I) -> Double<I>
where
    I: ParallelIterator<Item = i32>,
{
    Double { base }
}

// ---------- 无索引路径：对应 map.rs:31-50 ----------

impl<I> ParallelIterator for Double<I>
where
    I: ParallelIterator<Item = i32>,
{
    type Item = i32;

    fn drive_unindexed<C>(self, consumer: C) -> C::Result
    where
        C: UnindexedConsumer<Self::Item>,
    {
        // 与 Map::drive_unindexed 相同：把消费者包一层，交给上游
        self.base.drive_unindexed(DoubleConsumer { base: consumer })
    }

    fn opt_len(&self) -> Option<usize> {
        self.base.opt_len()
    }
}

// ---------- 索引路径：对应 map.rs:52-104 ----------

impl<I> IndexedParallelIterator for Double<I>
where
    I: IndexedParallelIterator<Item = i32>,
{
    fn drive<C>(self, consumer: C) -> C::Result
    where
        C: Consumer<Self::Item>,
    {
        self.base.drive(DoubleConsumer { base: consumer })
    }

    fn len(&self) -> usize {
        self.base.len()
    }

    fn with_producer<CB>(self, callback: CB) -> CB::Output
    where
        CB: ProducerCallback<Self::Item>,
    {
        // 对应 map.rs:70-103 的 Callback 模式：
        // 先把上游生产者拿到手，包成 DoubleProducer 再回调
        self.base.with_producer(DoubleCallback { callback })
    }
}

struct DoubleCallback<CB> {
    callback: CB,
}

impl<CB> ProducerCallback<i32> for DoubleCallback<CB>
where
    CB: ProducerCallback<i32>,
{
    type Output = CB::Output;

    fn callback<P>(self, base: P) -> CB::Output
    where
        P: Producer<Item = i32>,
    {
        self.callback.callback(DoubleProducer { base })
    }
}

// ---------- 生产者：对应 map.rs:108-157 ----------

struct DoubleProducer<P> {
    base: P,
}

fn double_val(x: i32) -> i32 {
    x * 2
}

impl<P> Producer for DoubleProducer<P>
where
    P: Producer<Item = i32>,
{
    type Item = i32;
    type IntoIter = std::iter::Map<P::IntoIter, fn(i32) -> i32>;

    fn into_iter(self) -> Self::IntoIter {
        // 到串行世界就复用标准库（对应 map.rs:122-124）
        self.base.into_iter().map(double_val as fn(i32) -> i32)
    }

    fn split_at(self, index: usize) -> (Self, Self) {
        let (left, right) = self.base.split_at(index);
        (
            DoubleProducer { base: left },
            DoubleProducer { base: right },
        )
    }

    fn fold_with<G>(self, folder: G) -> G
    where
        G: Folder<Self::Item>,
    {
        // 对应 map.rs:147-156：把 folder 包一层交给上游
        self.base.fold_with(DoubleFolder { base: folder }).base
    }
}

// ---------- 消费者：对应 map.rs:162-217 ----------

struct DoubleConsumer<C> {
    base: C,
}

impl<C> Consumer<i32> for DoubleConsumer<C>
where
    C: Consumer<i32>,
{
    type Folder = DoubleFolder<C::Folder>;
    type Reducer = C::Reducer;
    type Result = C::Result;

    fn split_at(self, index: usize) -> (Self, Self, Self::Reducer) {
        let (left, right, reducer) = self.base.split_at(index);
        (
            DoubleConsumer { base: left },
            DoubleConsumer { base: right },
            reducer, // 归约器直接复用内层（对应 map.rs:183-190）
        )
    }

    fn into_folder(self) -> Self::Folder {
        DoubleFolder {
            base: self.base.into_folder(),
        }
    }

    fn full(&self) -> bool {
        self.base.full()
    }
}

impl<C> UnindexedConsumer<i32> for DoubleConsumer<C>
where
    C: UnindexedConsumer<i32>,
{
    fn split_off_left(&self) -> Self {
        DoubleConsumer {
            base: self.base.split_off_left(),
        }
    }

    fn to_reducer(&self) -> Self::Reducer {
        self.base.to_reducer()
    }
}

// ---------- 串行执行形态：对应 map.rs:219-254 ----------

struct DoubleFolder<C> {
    base: C,
}

impl<C> Folder<i32> for DoubleFolder<C>
where
    C: Folder<i32>,
{
    type Result = C::Result;

    fn consume(self, item: i32) -> Self {
        // 全部语义浓缩在这两行（对应 map.rs:231-237）
        DoubleFolder {
            base: self.base.consume(item * 2),
        }
    }

    fn complete(self) -> C::Result {
        self.base.complete()
    }

    fn full(&self) -> bool {
        self.base.full()
    }
}

fn main() {
    // 1) 无索引消费者：sum 走 drive_unindexed
    let s: i32 = double((0..100).into_par_iter()).sum();
    assert_eq!(s, 2 * (0..100).sum::<i32>());

    // 2) 索引消费者：collect 走 with_producer 快速路径
    let v: Vec<i32> = double((0..5).into_par_iter()).collect();
    assert_eq!(v, vec![0, 2, 4, 6, 8]);

    // 3) 与官方 map 对拍：一条管道两万次变换逐元素一致
    let a: Vec<i32> = (0..20_000).into_par_iter().map(|x| x * 2).collect();
    let b: Vec<i32> = double((0..20_000).into_par_iter()).collect();
    assert_eq!(a, b);

    println!("全部断言通过");
}
```

**第四步**：`cargo run --release`。

### 5.3 需要观察的现象

1. 程序编译通过并打印 `全部断言通过`——三条断言分别覆盖无索引路径、索引路径、与官方实现的对拍；
2. 对照 [src/iter/map.rs](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/map.rs)，本实现几乎是它的逐段翻写：把「泛型闭包 + `&map_op` 共享」换成了「写死的乘 2」，其余结构一一对应；
3. 若删掉 `opt_len` 的覆盖（返回默认 `None`），断言仍应通过——印证 4.1 练习 2「`opt_len` 是优化信息而非正确性依赖」。

### 5.4 预期结果

- 三条断言全部成立；对拍一致说明自定义适配器语义正确；
- 若出现编译错误，最常见的原因是漏实现某个必需方法（`ParallelIterator` 必需 `drive_unindexed`，`IndexedParallelIterator` 必需 `drive`/`len`/`with_producer`，见 [src/iter/mod.rs:2410](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L2410)、[L3220](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L3220)、[L3236](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L3236)、[L3253](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/mod.rs#L3253)），编译器会逐个指出；
- 扩展挑战：把 `DoubleFolder::consume` 加一行 `println!`（注意会打印多次、顺序不定），或把固定乘 2 泛型化为 `Mul` 工程量更大的版本。

## 6. 本讲小结

- **适配器 = 小结构体 + 包装消费者**：无状态适配器只装「上游 + 闭包」，执行时把下游消费者包一层再转发给上游；数据自生产者流出、穿过层层「洋葱」，变换发生在 `Folder::consume` 里。
- **`map` 是完美样本**：不改变个数与顺序，因此 `len`/切分位置全部透传，完整保留 `IndexedParallelIterator`；`filter`/`filter_map` 因输出长度不可知而天然无索引，且过滤逻辑落在 folder 层、切分逻辑无感知。
- **闭包约束的规律**：以 `&F` 共享给多任务的闭包必须 `Sync + Send`（map/filter/inspect/update 一致）；`Cloned` 证明即使没有闭包，类型变化也必须走完整的 Consumer/Folder 协议。
- **委托宏消除纯转发样板**：`delegate_iterator!` / `delegate_indexed_iterator!` 为「壳类型」生成全部转发实现（共 5 个方法、两个 impl 块），`src/` 下 25 处调用；注入逻辑的适配器仍需手写。
- **`map_with` / `map_init` 的状态生命周期**：克隆/构造只发生在任务切分时（克隆次数 = 叶子任务数 − 1，与元素总数无关），任务内 `&mut T` 独占，因此 `T` 无需 `Sync`；任务结束时状态被丢弃，需要归并状态应使用 fold/reduce。

## 7. 下一步学习建议

本讲只覆盖了「逐元素独立变换」的适配器。顺着依赖关系，建议：

1. **u3-l2（fold 与 reduce）**：补上「带状态的归约」视角——本讲练习 4 留下的问题（状态如何参与最终结果）在那里解决；`map_with.rs` 的 `MapWithFolder` 与 `fold.rs` 的 `FoldFolder` 恰好形成对照。
2. **u3-l3（有索引适配器与长度控制）**：本讲实践中用到的 `with_max_len`/`with_min_len` 在那里展开；结合 `zip`/`enumerate` 理解为什么它们必须建立在 `IndexedParallelIterator` 之上。
3. **先行阅读**（可选）：[src/iter/plumbing/README.md](https://github.com/rayon-rs/rayon/blob/ee0a00bdb1ab039e178a215ad5712fb7fa58e58f/src/iter/plumbing/README.md)——官方对 Producer/Consumer/bridge 协议的完整阐述，读完后再回看本讲的洋葱图会有全局感；这也是 u4-l1 的预习材料。
